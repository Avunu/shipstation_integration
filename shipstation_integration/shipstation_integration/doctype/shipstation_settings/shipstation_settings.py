# Copyright (c) 2020, Parsimony LLC and contributors
# For license information, please see license.txt

import json
from typing import TYPE_CHECKING, Any, cast

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils.nestedset import get_root_of
from httpx import HTTPStatusError
from shipstation import ShipStation

if TYPE_CHECKING:
    from shipstation.models import ShipStationCarrier, ShipStationStore, ShipStationWarehouse


class ShipstationSettings(Document):
    # begin: auto-generated types
    # This code is auto-generated. Do not modify anything in this block.

    from typing import TYPE_CHECKING

    if TYPE_CHECKING:
        from frappe.types import DF
        from shipstation_integration.shipstation_integration.doctype.shipstation_item_custom_field.shipstation_item_custom_field import ShipstationItemCustomField
        from shipstation_integration.shipstation_integration.doctype.shipstation_option.shipstation_option import ShipstationOption
        from shipstation_integration.shipstation_integration.doctype.shipstation_store.shipstation_store import ShipstationStore
        from shipstation_integration.shipstation_integration.doctype.shipstation_warehouse.shipstation_warehouse import ShipstationWarehouse

        api_key: DF.Password
        api_secret: DF.Password
        carrier_data: DF.Code | None
        default_item_group: DF.Link
        define_custom_fields_for_items: DF.Check
        enable_label_generation: DF.Check
        enabled: DF.Check
        hours_to_fetch: DF.Int
        item_custom_fields: DF.Table[ShipstationItemCustomField]
        shipstation_options: DF.Table[ShipstationOption]
        shipstation_stores: DF.Table[ShipstationStore]
        shipstation_user: DF.Link | None
        shipstation_warehouses: DF.TableMultiSelect[ShipstationWarehouse]
        since_date: DF.Date | None
        weight_conversion: DF.Literal["As Provided", "Convert to Gram", "Convert to Ounce"]
    # end: auto-generated types
    @property
    def store_ids(self) -> list[Any]:
        """Get list of store IDs from shipstation_stores table."""
        return [store.store_id for store in self.shipstation_stores if store.store_id]

    @property
    def active_warehouse_ids(self) -> list[str]:
        warehouse_ids = []

        for warehouse in self.shipstation_warehouses:
            warehouse_id = frappe.db.get_value(
                "Warehouse", warehouse.get("warehouse"), "shipstation_warehouse_id"
            )
            warehouse_ids.append(warehouse_id)

        return warehouse_ids

    def onload(self):
        if self.carrier_data:
            self.set_onload("carriers", self._carrier_data())

    def validate(self):
        self.validate_label_generation()
        self.validate_enabled_stores()
        if self.hours_to_fetch < 24:
            frappe.throw(
                _("Order Age must be greater than or equal to 24 hours"),
                title=_("Invalid Order Age"),
            )

    def before_insert(self):
        self.validate_api_connection()

    def after_insert(self):
        self.update_carriers_and_stores()
        self.update_warehouses()

    @frappe.whitelist()
    def get_orders(self):
        from shipstation_integration.orders import list_orders

        self.validate()
        list_orders(self)

    @frappe.whitelist()
    def get_shipments(self):
        from shipstation_integration.shipments import list_shipments

        list_shipments(self)

    def client(self):
        password = self.get_password("api_key")
        secret = self.get_password("api_secret")
        assert isinstance(password, str) and isinstance(secret, str), "API key and secret must be set"
        return ShipStation(
            key=password,
            secret=secret,
            debug=False,
            timeout=30,
        )

    def validate_label_generation(self):
        if not self.enabled and self.enable_label_generation:
            self.enable_label_generation = False

    def validate_enabled_stores(self):
        for store in self.shipstation_stores:
            if store.enable_shipments and not store.enable_orders:
                store.enable_shipments = False
                store.create_sales_invoice = False
                store.create_delivery_note = False
                store.create_shipment = False

    def validate_api_connection(self):
        try:
            client = self.client()
            client.list_carriers()
        except HTTPStatusError as e:
            if e.response.status_code == 401:
                frappe.throw(_("Invalid API key or secret"))
            else:
                frappe.throw(_(str(e)))
        except Exception as e:
            frappe.throw(_(str(e)))

    @frappe.whitelist()
    def update_carriers_and_stores(self):
        client = self.client()

        unstructured_carriers: list[dict[str, Any]] = []
        carriers = client.list_carriers()
        for carrier in carriers:
            carrier_obj = cast("ShipStationCarrier", carrier)
            carrier_dict = carrier_obj._unstructure()
            carrier_code = str(carrier_obj.code) if carrier_obj.code else ""
            services = client.list_services(carrier_code)
            carrier_dict["services"] = [s._unstructure() for s in services]  # type: ignore[union-attr]
            packages = client.list_packages(carrier_code)
            carrier_dict["packages"] = [p._unstructure() for p in packages]  # type: ignore[union-attr]
            unstructured_carriers.append(carrier_dict)

        self.carrier_data = json.dumps(unstructured_carriers)
        self.update_stores()
        self.save()
        return self

    @frappe.whitelist()
    def update_warehouses(self):
        self.shipstation_warehouses = []
        root_warehouse = get_root_of("Warehouse")

        if not frappe.db.exists("Warehouse", {"warehouse_name": "Shipstation Warehouses"}):
            ss_warehouse_doc = frappe.new_doc("Warehouse")
            ss_warehouse_doc.update(
                {
                    "warehouse_name": "Shipstation Warehouses",
                    "parent_warehouse": root_warehouse,
                    "is_group": True,
                }
            )
            ss_warehouse_doc.insert()

        parent_warehouse_name = frappe.db.get_value(
            "Warehouse", {"warehouse_name": "Shipstation Warehouses"}, "name"
        )
        warehouses = self.client().list_warehouses()

        for wh in warehouses:
            warehouse = cast("ShipStationWarehouse", wh)
            warehouse_id = warehouse.warehouse_id
            warehouse_name = warehouse.warehouse_name
            if frappe.db.exists("Warehouse", {"shipstation_warehouse_id": warehouse_id}):
                existing_name = str(frappe.db.get_value(
                    "Warehouse", {"shipstation_warehouse_id": warehouse_id}, "name"
                ) or "")
                warehouse_doc = frappe.get_doc("Warehouse", existing_name)
            else:
                warehouse_doc = frappe.new_doc("Warehouse")
                warehouse_doc.update(
                    {
                        "shipstation_warehouse_id": warehouse_id,
                        "warehouse_name": warehouse_name,
                        "parent_warehouse": parent_warehouse_name,
                    }
                )
                warehouse_doc.insert()

            self.append("shipstation_warehouses", {"warehouse": warehouse_doc.name})

        self.save()

    def update_stores(self):
        from shipstation_integration.utils import get_marketplace

        stores = self.client().list_stores(show_inactive=False)
        for st in stores:
            store = cast("ShipStationStore", st)
            store_exists = False
            store_id = store.store_id
            marketplace_name = str(store.marketplace_name) if store.marketplace_name else ""
            store_name = store.store_name
            account_name = getattr(store, "account_name", None)

            for ss_store in self.shipstation_stores:
                if store_id == ss_store.store_id:
                    ss_store.update(
                        {
                            "marketplace_name": marketplace_name,
                            "store_name": store_name,
                        }
                    )
                    store_exists = True

            if store_exists:
                continue

            if "Amazon" in marketplace_name:
                self.append(
                    "shipstation_stores",
                    {
                        "is_amazon_store": 1,
                        "amazon_marketplace": account_name,
                        "enable_orders": 1,
                        "store_id": store_id,
                        "marketplace_name": get_marketplace(id=account_name).sales_partner,
                        "store_name": store_name,
                    },
                )
            elif "Shopify" in marketplace_name:
                self.append(
                    "shipstation_stores",
                    {
                        "is_shopify_store": 1,
                        "enable_orders": 1,
                        "store_id": store_id,
                        "marketplace_name": marketplace_name,
                        "store_name": store_name,
                    },
                )
            else:
                self.append(
                    "shipstation_stores",
                    {
                        "enable_orders": 1,
                        "store_id": store_id,
                        "marketplace_name": marketplace_name,
                        "store_name": store_name,
                    },
                )

        return self

    @frappe.whitelist()
    def get_items(self):
        from shipstation.models import ShipStationItem
        from shipstation_integration.items import create_item

        products = self.client().list_products()

        if not products.results:
            return "No products found to import"

        for product in products:
            if isinstance(product, ShipStationItem):
                create_item(product, settings=self)

        return f"{len(products.results)} product(s) imported succesfully"

    def _carrier_data(self) -> list[dict[str, Any]]:
        if not self.carrier_data:
            return []
        return json.loads(self.carrier_data)

    def get_carrier_services(self, carrier):
        for ss_carrier in self._carrier_data():
            if carrier in [ss_carrier["name"], ss_carrier["nickname"]]:
                return "\n".join([s["name"] for s in ss_carrier["services"]])

    def get_codes(self, carrier, service, package):
        _carrier, _service, _package = None, None, "Package"
        for ss_carrier in self._carrier_data():
            if carrier in [ss_carrier.get("name"), ss_carrier.get("nickname")]:
                _carrier = ss_carrier["code"]

                for serv in ss_carrier["services"]:
                    if serv["name"] == service:
                        _service = serv["code"]

                for pack in ss_carrier["packages"]:
                    if pack["name"] == package:
                        _package = pack["code"]

        return _carrier, _service, _package

    # create custom fields on the Sales Order Item doctype from the item_custom_fields table (for storing Shipstation metadata)
    @frappe.whitelist()
    def update_order_item_custom_fields(self, removed_item_custom_fields: list[str] | None = None):
        # first, create any new custom fields
        item_custom_fields = self.item_custom_fields
        insert_after = "shipstation_item_notes"
        item_doctypes = ["Delivery Note Item", "Sales Order Item", "Sales Invoice Item"]

        for field in item_custom_fields:
            fieldname = getattr(field, "fieldname", None)
            if not fieldname:
                continue

            field_def: dict[str, Any] = {
                "insert_after": insert_after,
                "label": getattr(field, "label", None),
                "fieldtype": getattr(field, "fieldtype", None),
                "fieldname": fieldname,
                "length": getattr(field, "length", None),
                "reqd": getattr(field, "reqd", None),
                "hidden": getattr(field, "hidden", None),
                "read_only": getattr(field, "read_only", None),
                "options": getattr(field, "options", None),
                "default": getattr(field, "default", None),
                "fetch_from": getattr(field, "fetch_from", None),
                "fetch_if_empty": getattr(field, "fetch_if_empty", None),
            }

            for dt in item_doctypes:
                if not frappe.db.exists("Custom Field", {"dt": dt, "fieldname": fieldname}):
                    custom_field = frappe.new_doc("Custom Field")
                    custom_field.update({"dt": dt})
                    custom_field.update(field_def)
                    custom_field.insert()
                else:
                    custom_field_name = str(frappe.db.get_value(
                        "Custom Field", {"dt": dt, "fieldname": fieldname}, "name"
                    ) or "")
                    custom_field = frappe.get_doc("Custom Field", custom_field_name)
                    custom_field.update(field_def)
                    custom_field.save()

                if frappe.db.exists("Custom Field", {"dt": dt, "fieldname": fieldname}):
                    insert_after = fieldname

        # delete any removed custom fields
        if removed_item_custom_fields:
            # make sure that the removed field is not in the item_custom_fields variable
            current_fieldnames = [getattr(f, "fieldname", None) for f in item_custom_fields]
            removed_item_custom_fields = [
                field
                for field in removed_item_custom_fields
                if field not in current_fieldnames
            ]

            for fieldname in removed_item_custom_fields:
                for dt in item_doctypes:
                    if frappe.db.exists("Custom Field", {"dt": dt, "fieldname": fieldname}):
                        frappe.db.delete("Custom Field", {"dt": dt, "fieldname": fieldname})
