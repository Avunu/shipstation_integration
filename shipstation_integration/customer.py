from typing import TYPE_CHECKING

import frappe
import re
from frappe.exceptions import DuplicateEntryError
from frappe.utils import getdate, parse_addr
from nameparser import HumanName
from shipstation.api import ShipStation
from frappe.query_builder import DocType
from frappe.query_builder.functions import Lower

if TYPE_CHECKING:
    from erpnext.selling.doctype.sales_order.sales_order import SalesOrder
    from frappe.contacts.doctype.address.address import Address
    from frappe.contacts.doctype.contact.contact import Contact
    from erpnext.selling.doctype.customer.customer import Customer
    from shipstation.models import ShipStationCustomer, ShipStationAddress, ShipStationOrder

    from shipstation_integration.shipstation_integration.doctype.shipstation_store.shipstation_store import (
        ShipstationStore,
    )
    from shipstation_integration.shipstation_integration.doctype.shipstation_settings.shipstation_settings import (
        ShipstationSettings,
    )


def update_customer_details(
    existing_so: str, customer: "ShipStationCustomer", store: "ShipstationStore"
):
    existing_so_doc: "SalesOrder" = frappe.get_doc("Sales Order", existing_so)

    email_id = customer.email
    if email_id:
        contact = create_contact_from_customer(customer, existing_so_doc.customer)
        existing_so_doc.contact_person = contact.name if contact else None

    existing_so_doc.update(
        {
            "customer_name": customer.name or customer.email,
            "has_pii": True,
            "integration_doctype": "Shipstation Settings",
            "integration_doc": store.parent,
        }
    )

    # Handle addresses if present
    if customer.street1:
        if existing_so_doc.customer_address:
            bill_address = update_address(
                customer, existing_so_doc.customer_address, customer.email, "Billing"
            )
        else:
            bill_address = create_address(customer, customer.name, customer.email, "Billing")
            existing_so_doc.customer_address = bill_address.name

    existing_so_doc.flags.ignore_validate_update_after_submit = True
    existing_so_doc.run_method("set_customer_address")
    existing_so_doc.save()
    return existing_so_doc


def create_address(address: "ShipStationAddress", customer: str, email: str, address_type: str):
    addr: "Address" = frappe.new_doc("Address")
    addr.append("links", {"link_doctype": "Customer", "link_name": customer})
    _update_address(address, addr, email, address_type)
    return addr


def update_address(
    address: "ShipStationAddress", address_name: str, email: str, address_type: str
):
    addr: "Address" = frappe.get_doc("Address", address_name)
    _update_address(address, addr, email, address_type)
    return addr


def _update_address(address: "ShipStationAddress", addr: "Address", email: str, address_type: str):
    addr.address_title = address.company or address.name
    addr.address_type = address_type
    addr.address_line1 = address.street1
    addr.address_line2 = address.street2
    addr.city = address.city
    addr.state = address.state
    addr.pincode = address.postal_code
    addr.country = frappe.get_cached_value("Country", {"code": address.country.lower()}, "name")
    addr.phone = address.phone
    addr.email_id = email
    try:
        addr.save()
        return addr
    except Exception as e:
        frappe.log_error(title="Error saving Shipstation Address", message=e)


def create_customer(
    order: "ShipStationOrder", settings: "ShipstationSettings" = None
) -> "Customer":
    """Create or update a customer from ShipStation data"""
    if not settings:
        settings = frappe.get_cached_doc("Shipstation Settings", {"enabled": 1})

    customer_id = order.customer_id
    if customer_id:
        # Check if customer exists with this ID
        existing_customer = frappe.db.get_value(
            "Customer", {"shipstation_customer_id": customer_id}, "name"
        )
        if existing_customer:
            return frappe.get_doc("Customer", existing_customer)

        # Fetch full customer data from ShipStation
        client = settings.client()
        ss_customer = client.get_customer(customer_id)
    else:
        ss_customer = None

    # Check if customer exists with same email
    customer_email = order.customer_email.strip().lower()
    if customer_email:
        Customer = DocType("Customer")
        customer_query = (
            frappe.qb.from_(Customer)
            .select(Customer.name)
            .where(Lower(Customer.customer_name) == customer_email)
            .limit(1)
        )
        existing_customer = customer_query.run(as_dict=True)

        if not existing_customer:
            ContactEmail = DocType("Contact Email")
            DynamicLink = DocType("Dynamic Link")

            contact_query = (
                frappe.qb.from_(ContactEmail)
                .inner_join(DynamicLink)
                .on(ContactEmail.parent == DynamicLink.parent)
                .select(DynamicLink.link_name)
                .where(Lower(ContactEmail.email_id) == customer_email)
                .where(DynamicLink.link_doctype == "Customer")
                .limit(1)
            )
            existing_customer = contact_query.run(as_dict=True)

        if existing_customer:
            existing_customer = existing_customer[0].get(
                "name" if "name" in existing_customer[0] else "link_name"
            )

    if existing_customer:
        cust = frappe.get_doc("Customer", existing_customer)
        if customer_id and not cust.get("shipstation_customer_id"):
            cust.shipstation_customer_id = customer_id
            cust.save()
        return cust

    # Create new customer
    cust = frappe.new_doc("Customer")
    cust.name = (
        order.customer_email
        or order.customer_id
        or order.ship_to.name
        or frappe.generate_hash("", 10)
    )
    cust.shipstation_customer_id = customer_id
    cust.customer_name = ss_customer.name if ss_customer else customer_email
    cust.customer_type = "Individual"
    cust.customer_group = "ShipStation"
    cust.territory = "United States"

    try:
        cust.save()
        frappe.db.commit()
    except DuplicateEntryError:
        return frappe.get_doc("Customer", {"customer_name": cust.customer_name})
    except Exception as e:
        frappe.log_error(title="Error creating Shipstation Customer", message=e)
        raise e

    # Create contact and addresses
    if ss_customer:
        contact = create_contact_from_customer(ss_customer, cust.name)
        if contact:
            cust.customer_primary_contact = contact.name
    else:
        # Fallback to order data if no customer data available
        ss_customer = (
            order.get("ship_to") if order.get("ship_to", {}).get("name") else order.get("bill_to")
        )
        contact = create_contact_from_customer(ss_customer, cust.name)
        if contact:
            cust.customer_primary_contact = contact.name

    try:
        cust.save()
        frappe.db.commit()
        return cust
    except Exception as e:
        frappe.log_error(title="Error saving Shipstation Customer", message=e)


def create_contact_from_customer(customer: "ShipStationCustomer", customer_name: str = None):
    """Create a contact from ShipStation customer data"""
    contact = None
    if customer.email:
        email = customer.email.strip().lower()
        ContactEmail = DocType("Contact Email")
        contact_query = (
            frappe.qb.from_(ContactEmail)
            .select(ContactEmail.parent)
            .where(Lower(ContactEmail.email_id) == email)
            .limit(1)
        )
        contact_result = contact_query.run(as_dict=True)
        contact = contact_result[0].get("parent") if contact_result else None

    if not contact:
        return contact

    if contact:
        cont = frappe.get_doc("Contact", contact)
    else:
        cont = frappe.new_doc("Contact")

    # Parse the name using HumanName
    name = HumanName(customer.name)

    title = re.sub(r"[^\w\s]", "", name.title.strip().title())
    if title:
        title_exists = frappe.db.exists("Salutation", title)
        if not title_exists:
            salutation = frappe.new_doc("Salutation")
            salutation.salutation = title
            salutation.save()
            frappe.db.commit()
        cont.salutation = title

    cont.first_name = name.first
    cont.middle_name = name.middle
    cont.last_name = name.last
    cont.designation = name.suffix

    if customer.company:
        cont.company_name = customer.company
    if customer.phone:
        cont.append("phone_nos", {"phone": customer.phone})
    if customer.email:
        cont.append("email_ids", {"email_id": customer.email})
    if customer_name:
        cont.append("links", {"link_doctype": "Customer", "link_name": customer_name})

    try:
        cont.save()
        frappe.db.commit()
        return cont
    except Exception as e:
        frappe.log_error(title="Error saving Shipstation Contact", message=e)


def overwrite_validate_phone_number(data, throw=False):
    return True


def get_billing_address(customer_name: str):
    billing_address = frappe.db.sql(
        """
            SELECT `tabAddress`.name
            FROM `tabDynamic Link`, `tabAddress`
            WHERE `tabDynamic Link`.link_doctype = 'Customer'
            AND `tabDynamic Link`.link_name = %(customer_name)s
            AND `tabAddress`.address_type = 'Billing'
            LIMIT 1
        """,
        {"customer_name": customer_name},
        as_dict=True,
    )
    return billing_address[0].get("name") if billing_address else None


def match_or_create_address(
    address: "ShipStationAddress", customer: str, email: str, address_type: str
) -> "Address":
    """Match existing address or create new one based on ShipStation address data"""
    if not address or not address.street1:
        return None

    Address = DocType("Address")

    # Case insensitive match on core address fields
    query = (
        frappe.qb.from_(Address)
        .select(Address.name)
        .where(Lower(Address.address_line1) == address.street1.lower())
        .where(Lower(Address.city) == address.city.lower())
        .limit(1)
    )

    existing_address = query.run(as_dict=True)

    if existing_address and existing_address[0]:
        addr = frappe.get_doc("Address", existing_address[0].get("name"))
        # Check if this customer is already linked
        has_customer_link = frappe.db.exists(
            "Dynamic Link",
            {"parent": addr.name, "link_doctype": "Customer", "link_name": customer},
        )

        if not has_customer_link:
            addr.append("links", {"link_doctype": "Customer", "link_name": customer})

        # Update address type and other details
        addr.address_type = address_type
        _update_address(address, addr, email, address_type)
        return addr

    # Create new address if no match found
    return create_address(address, customer, email, address_type)
