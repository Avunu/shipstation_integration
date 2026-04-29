import datetime
from typing import cast

import frappe
from erpnext.stock.doctype.item.item import get_uom_conv_factor
from frappe.utils import flt, getdate
from httpx import HTTPError

logger = frappe.logger("shipstation", allow_site=True, file_count=10)
# logger.setLevel("DEBUG")

from shipstation_integration.customer import (
    create_customer,
    update_customer_details,
    match_or_create_address,
)
from shipstation_integration.items import create_item

from erpnext.selling.doctype.sales_order.sales_order import SalesOrder
from shipstation.models import ShipStationOrder, ShipStationOrderItem

from shipstation_integration.shipstation_integration.doctype.shipstation_settings.shipstation_settings import (
    ShipstationSettings,
)
from shipstation_integration.shipstation_integration.doctype.shipstation_store.shipstation_store import (
    ShipstationStore,
)


def list_orders(
    settings: "ShipstationSettings | str | list[str] | None" = None,
    last_order_datetime: datetime.datetime | None = None,
):
    settings_list: list[str]
    if not settings:
        settings_list = [
            s.name for s in frappe.get_all("Shipstation Settings", filters={"enabled": True})
        ]
    elif isinstance(settings, str):
        settings_list = [settings]
    elif isinstance(settings, list):
        settings_list = settings
    else:
        settings_list = [str(settings.name)]

    logger.info(f"list_orders called with settings_list: {settings_list}")

    for sss_name in settings_list:
        sss_doc: ShipstationSettings = cast(
            ShipstationSettings, frappe.get_doc("Shipstation Settings", sss_name)
        )
        if not sss_doc.enabled:
            continue

        client = sss_doc.client()
        client.timeout = 60

        if not last_order_datetime:
            # get data for the last day, Shipstation API behaves oddly when it's a shorter period
            hours_to_fetch = getattr(sss_doc, "hours_to_fetch", None) or 500
            last_order_datetime = datetime.datetime.utcnow() - datetime.timedelta(
                hours=int(hours_to_fetch)
            )

        store: "ShipstationStore"
        for store in sss_doc.shipstation_stores:
            if not store.enable_orders:
                logger.debug(
                    f"Skipping store {store.store_name} ({store.store_id}): enable_orders is False"
                )
                continue

            logger.info(f"Processing store: {store.store_name} ({store.store_id})")

            parameters = {
                "store_id": store.store_id,
                "modify_date_start": last_order_datetime,
                "modify_date_end": datetime.datetime.utcnow(),
            }

            update_parameter_hook = frappe.get_hooks("update_shipstation_list_order_parameters")
            if update_parameter_hook:
                parameters = frappe.get_attr(update_parameter_hook[0])(parameters)

            logger.info(f"Fetching orders with parameters: {parameters}")

            try:
                orders = client.list_orders(parameters=parameters)
                logger.info(
                    f"Fetched {len(orders.results) if hasattr(orders, 'results') else 'unknown'} orders"
                )
            except HTTPError as e:
                frappe.log_error(title="Error while fetching Shipstation orders", message=e)
                logger.error(f"HTTPError fetching orders: {e}")
                continue

            for order in orders:
                if not isinstance(order, ShipStationOrder):
                    logger.warning(f"Skipping invalid order object: {order}")
                    continue
                logger.info(f"Processing order {order.order_number} (ID: {order.order_id})")
                if validate_order(sss_doc, order, store):  # type: ignore[arg-type]
                    should_create_order = True
                    process_order_hook = frappe.get_hooks("process_shipstation_order")
                    if process_order_hook:
                        should_create_order = frappe.get_attr(process_order_hook[0])(order, store)
                        logger.info(f"process_order_hook returned: {should_create_order}")

                    if should_create_order:
                        logger.info(f"Creating ERPNext order for {order.order_number}")
                        create_erpnext_order(order, store, sss_doc)  # type: ignore[arg-type]
                    else:
                        logger.info(
                            f"Skipping order {order.order_number}: should_create_order is False"
                        )
                else:
                    logger.info(f"Order {order.order_number} failed validation")


def validate_order(
    settings: "ShipstationSettings",
    order: "ShipStationOrder",
    store: "ShipstationStore",
):
    if not order:
        logger.warning(f"validate_order: order is falsy")
        return False

    # if an order already exists, skip, unless the status needs to be updated
    existing_order: dict | None = frappe.db.get_value(
        "Sales Order",
        {"shipstation_order_id": order.order_id},
        fieldname=["name", "status"],  # type: ignore[arg-type]
        as_dict=True,
    )
    if existing_order:
        existing_order_name = str(existing_order.get("name", ""))
        existing_order_status = str(existing_order.get("status", ""))
        new_status, new_docstatus = get_erpnext_status(order.order_status)
        logger.info(
            f"validate_order: Order {order.order_number} already exists as {existing_order_name} (status: {existing_order_status} -> {new_status})"
        )
        if existing_order_status != new_status:
            # if the new status is canceled, cancel it
            if new_status == "Cancelled":
                try:
                    frappe.get_doc("Sales Order", existing_order_name).cancel()
                except Exception as e:
                    frappe.log_error(
                        title="Error while cancelling Shipstation order",
                        message=f"Error: {e}, Order ID: {existing_order_name}",
                    )
            frappe.db.set_value(
                "Sales Order",
                existing_order_name,
                {"status": new_status, "docstatus": new_docstatus},
                update_modified=False,
            )
        return False

    # only create orders for warehouses defined in Shipstation Settings;
    # if no warehouses are set, fetch everything
    if (
        settings.active_warehouse_ids
        and order.advanced_options
        and order.advanced_options.warehouse_id not in settings.active_warehouse_ids
    ):
        logger.info(
            f"validate_order: Order {order.order_number} rejected - warehouse_id {order.advanced_options.warehouse_id} not in {settings.active_warehouse_ids}"
        )
        return False

    # if a date filter is set in Shipstation Settings, don't create orders before that date
    if settings.since_date:
        order_date = getdate(order.create_date)
        since_date = getdate(settings.since_date)
        if order_date and since_date and order_date < since_date:
            logger.info(
                f"validate_order: Order {order.order_number} rejected - order_date {order_date} < since_date {since_date}"
            )
            return False

    logger.info(f"validate_order: Order {order.order_number} passed validation")
    return True


def create_erpnext_order(
    order: "ShipStationOrder", store: "ShipstationStore", settings: "ShipstationSettings"
) -> str | None:
    customer = create_customer(order, settings)
    customer_name = str(customer.name) if customer.name else ""
    customer_email = order.customer_email or ""

    # Get shipping and billing addresses
    shipping_address = (
        match_or_create_address(order.ship_to, customer_name, customer_email, "Shipping")
        if order.ship_to
        else None
    )
    billing_address = (
        match_or_create_address(order.bill_to, customer_name, customer_email, "Billing")
        if order.bill_to
        else None
    )
    status, docstatus = get_erpnext_status(order.order_status)
    so = cast(SalesOrder, frappe.new_doc("Sales Order"))
    so.update(
        {
            "status": status,
            "shipstation_store_name": store.store_name,
            "shipstation_order_id": order.order_id,
            "shipstation_customer_notes": getattr(order, "customer_notes", None),
            "shipstation_internal_notes": getattr(order, "internal_notes", None),
            "marketplace": store.marketplace_name,
            "marketplace_order_id": order.order_number,
            "customer": customer.name,
            "company": store.company,
            "transaction_date": getdate(order.order_date),
            "delivery_date": getdate(order.ship_date),
            "customer_address": billing_address.name if billing_address else None,
            "address_display": billing_address.get_display() if billing_address else None,
            "shipping_address_name": shipping_address.name if shipping_address else None,
            "shipping_address": shipping_address.get_display() if shipping_address else None,
            "integration_doctype": "Shipstation Settings",
            "integration_doc": store.parent,
            "has_pii": True,
        }
    )

    if store.get("is_amazon_store"):
        update_hook = frappe.get_hooks("update_shipstation_amazon_order")
        if update_hook:
            so = frappe.get_attr(update_hook[0])(store, order, so)
    elif store.get("is_shopify_store"):
        update_hook = frappe.get_hooks("update_shipstation_shopify_order")
        if update_hook:
            so = frappe.get_attr(update_hook[0])(store, order, so)

    # using `hasattr` over `getattr` to use type annotations
    order_items = order.items if hasattr(order, "items") else []
    if not order_items:
        return

    process_order_items_hook = frappe.get_hooks("process_shipstation_order_items")
    if process_order_items_hook:
        order_items = frappe.get_attr(process_order_items_hook[0])(order_items)

    discount_amount = 0.0
    for item in order_items:
        item_quantity = int(item.quantity) if item.quantity else 0
        if item_quantity < 1:
            continue

        rate = (
            float(item.unit_price)
            if hasattr(item, "unit_price") and item.unit_price is not None
            else 0.0
        )

        # the only way to identify marketplace discounts via the Shipstation API is
        # to find it using the `line_item_key` string
        if item.line_item_key == "discount":
            discount_amount += abs(rate * item_quantity)
            continue

        settings = cast(ShipstationSettings, frappe.get_doc("Shipstation Settings", store.parent))
        stock_item = create_item(item, settings=settings, store=store)
        uom = stock_item.sales_uom or stock_item.stock_uom
        conversion_factor = (
            1 if uom == stock_item.stock_uom else get_uom_conv_factor(uom, stock_item.stock_uom)
        )
        item_notes = get_item_notes(item)
        item_dict = {
            "item_code": stock_item.item_code,
            "qty": item.quantity,
            "uom": uom,
            "conversion_factor": conversion_factor,
            "rate": rate,
            "warehouse": store.warehouse,
            "shipstation_order_item_id": item.order_item_id,
            "shipstation_item_notes": item_notes,
        }

        shipstation_options = frappe.get_all(
            "Shipstation Option",
            filters={"parent": store.parent},
            fields=["shipstation_option_name", "item_field"],
        )

        # check to see if the option exists in the Options Import table, otherwise add it
        for option in item.options or []:
            option_import = next(
                (
                    ss_option
                    for ss_option in shipstation_options
                    if ss_option.shipstation_option_name == option.name
                ),
                None,
            )

            if option_import:
                if option_import.item_field:
                    item_dict[option_import.item_field] = option.value
            else:
                settings.append("shipstation_options", {"shipstation_option_name": option.name})
                settings.save()

        so.append("items", item_dict)

    if not so.get("items"):
        return

    so.dont_update_if_missing = ["customer_name", "base_total_in_words"]

    if order.tax_amount:
        so.append(
            "taxes",
            {
                "charge_type": "Actual",
                "account_head": store.tax_account,
                "description": "Shipstation Tax Amount",
                "tax_amount": order.tax_amount,
                "cost_center": store.cost_center,
            },
        )

    if order.shipping_amount:
        so.append(
            "taxes",
            {
                "charge_type": "Actual",
                "account_head": store.shipping_income_account,
                "description": "Shipstation Shipping Amount",
                "tax_amount": order.shipping_amount,
                "cost_center": store.cost_center,
            },
        )

    if discount_amount > 0:
        so.apply_discount_on = "Grand Total"
        so.discount_amount = discount_amount

    try:
        so.save()
    except Exception as e:
        frappe.log_error(
            title="Error saving Sales Order",
            message={"error": e, "traceback": frappe.get_traceback()},
            reference_doctype="Sales Order",
            reference_name=so.name,
        )
        return

    before_submit_hook = frappe.get_hooks("update_shipstation_order_before_submit")
    if before_submit_hook:
        so = frappe.get_attr(before_submit_hook[0])(store, so)
        try:
            so.save()
        except Exception as e:
            frappe.log_error(
                title="Error saving Sales Order after before_submit_hook",
                message={"error": e, "traceback": frappe.get_traceback()},
                reference_doctype="Sales Order",
                reference_name=so.name,
            )
            return

    match docstatus:
        case 1:
            so.submit()
        case 2:
            try:
                so.cancel()
            except:
                frappe.db.set_value("Sales Order", so.name, "status", "Cancelled")

    frappe.db.commit()
    return so.name


def get_item_notes(item: "ShipStationOrderItem"):
    notes = None
    item_options = item.options if hasattr(item, "options") else None
    if item_options:
        for option in item_options:
            if option.name == "Description":
                notes = option.value
                break
    return notes


def get_erpnext_status(shipstation_status):
    status_mapping = {
        "awaiting_payment": ("Draft", 0),
        "awaiting_shipment": ("To Deliver", 1),
        "shipped": ("Completed", 1),
        "on_hold": ("On Hold", 1),
        "cancelled": ("Cancelled", 2),
        "pending_fulfillment": ("To Deliver and Bill", 1),
    }

    return status_mapping.get(shipstation_status, ("Draft", 0))
