from typing import TYPE_CHECKING, Union

import frappe
import re
from frappe.exceptions import DuplicateEntryError
from frappe.types import DF
from frappe.utils import getdate, parse_addr
from nameparser import HumanName
from shipstation.api import ShipStation
from frappe.query_builder import DocType
from frappe.query_builder.functions import Lower

from erpnext.selling.doctype.sales_order.sales_order import SalesOrder
from frappe.contacts.doctype.address.address import Address
from frappe.contacts.doctype.contact.contact import Contact
from erpnext.selling.doctype.customer.customer import Customer
from shipstation.models import ShipStationCustomer, ShipStationAddress, ShipStationOrder

if TYPE_CHECKING:
    from shipstation_integration.shipstation_integration.doctype.shipstation_settings.shipstation_settings import (
        ShipstationSettings,
    )
    from shipstation_integration.shipstation_integration.doctype.shipstation_store.shipstation_store import (
        ShipstationStore,
    )

AddressType = DF.Literal[
    "Billing",
    "Shipping",
    "Office",
    "Personal",
    "Plant",
    "Postal",
    "Shop",
    "Subsidiary",
    "Warehouse",
    "Current",
    "Permanent",
    "Other",
]


def update_customer_details(
    existing_so: str, customer: "ShipStationCustomer", store: "ShipstationStore"
):
    existing_so_doc: "SalesOrder" = SalesOrder("Sales Order", existing_so)

    email_id = customer.email
    if email_id:
        contact = create_contact_from_customer(customer, existing_so_doc.customer)
        existing_so_doc.contact_person = contact.name if contact else None

    existing_so_doc.update(
        {
            "customer_name": customer.name or customer.email,
            "has_pii": True,
            "integration_doctype": "Shipstation Settings",
            "integration_doc": store.get("parent"),
        }
    )

    # Handle addresses if present
    if customer.street1:
        if existing_so_doc.customer_address:
            bill_address = update_address(
                customer, existing_so_doc.customer_address, getattr(customer, "email"), "Billing"
            )
        else:
            bill_address = create_address(
                customer, getattr(customer, "name"), getattr(customer, "email"), "Billing"
            )
            existing_so_doc.customer_address = bill_address.name

    existing_so_doc.flags.ignore_validate_update_after_submit = True
    existing_so_doc.run_method("set_customer_address")
    existing_so_doc.save()
    return existing_so_doc


def create_address(
    address: Union["ShipStationCustomer", "ShipStationAddress"],
    customer: str,
    email: str,
    address_type: AddressType,
):
    addr: "Address" = Address({"doctype": "Address"})
    addr.append("links", {"link_doctype": "Customer", "link_name": customer})
    _update_address(address, addr, email, address_type)
    return addr


def update_address(
    address: Union["ShipStationCustomer", "ShipStationAddress"],
    address_name: str,
    email: str,
    address_type: AddressType,
):
    addr: "Address" = Address("Address", address_name)
    _update_address(address, addr, email, address_type)
    return addr


def _update_address(
    address: Union["ShipStationCustomer", "ShipStationAddress"],
    addr: "Address",
    email: str,
    address_type: AddressType,
):
    # Address Title: Use company if available and a non-empty string, otherwise use name if available and a non-empty string.
    company_attr = getattr(address, "company", None)
    name_attr = getattr(address, "name", None)

    final_address_title = None
    if company_attr and isinstance(company_attr, str) and company_attr.strip():
        final_address_title = company_attr
    elif name_attr and isinstance(name_attr, str) and name_attr.strip():
        final_address_title = name_attr
    addr.address_title = final_address_title

    # Address Type (from argument, ensure it's a string)
    if isinstance(address_type, str):
        addr.address_type = address_type

    # Address Line 1
    street1_attr = getattr(address, "street1", None)
    if street1_attr and isinstance(street1_attr, str):
        addr.address_line1 = street1_attr

    # Address Line 2
    street2_attr = getattr(address, "street2", None)
    if street2_attr and isinstance(street2_attr, str):
        addr.address_line2 = street2_attr

    # City
    city_attr = getattr(address, "city", None)
    if city_attr and isinstance(city_attr, str):
        addr.city = city_attr

    # State
    state_attr = getattr(address, "state", None)
    if state_attr and isinstance(state_attr, str):
        addr.state = state_attr

    # Pincode
    postal_code_attr = getattr(address, "postal_code", None)
    if postal_code_attr and isinstance(postal_code_attr, str) or isinstance(postal_code_attr, int):
        addr.pincode = str(postal_code_attr)

    # Country
    country_code_attr = getattr(address, "country", None)
    if country_code_attr and isinstance(country_code_attr, str) and country_code_attr.strip():
        country_name = frappe.get_cached_value(
            "Country", {"code": country_code_attr.lower()}, "name"
        )
        if isinstance(country_name, str):  # Ensure the cached value is also a string
            addr.country = country_name

    # Phone
    phone_attr = getattr(address, "phone", None)
    if phone_attr and isinstance(phone_attr, str):
        addr.phone = phone_attr

    # Email ID (from argument, ensure it's a string)
    if email and isinstance(email, str):
        addr.email_id = email

    try:
        addr.save()
        return addr
    except Exception as e:
        frappe.log_error(title="Error saving Shipstation Address", message=e)


def create_customer(
    order: "ShipStationOrder", settings: "ShipstationSettings | None" = None
) -> "Customer":
    """Create or update a customer from ShipStation data"""
    if not settings:
        settings = ShipstationSettings("Shipstation Settings", {"enabled": 1})
    elif isinstance(settings, dict):
        settings = ShipstationSettings("Shipstation Settings", settings.get("name"))

    customer_id = order.customer_id
    existing_customer_name: str | None = None

    if customer_id:
        # Check if customer exists with this ID
        existing_customer_name = str(
            frappe.db.get_value("Customer", {"shipstation_customer_id": customer_id}, "name")
        )
        if (
            existing_customer_name
            and isinstance(existing_customer_name, str)
            and existing_customer_name != "None"
        ):
            print(f"existing_customer_name: {existing_customer_name}")
            return Customer("Customer", existing_customer_name)

    # Check if customer exists with same email
    customer_email = getattr(order, "customer_email", None)
    if not existing_customer_name and customer_email:
        customer_email_lower = customer_email.strip().lower()
        CustomerDocType = DocType("Customer")
        customer_query = (
            frappe.qb.from_(CustomerDocType)
            .select(CustomerDocType.name)
            .where(Lower(CustomerDocType.customer_name) == customer_email_lower)
            .limit(1)
        )
        customer_by_email_result = customer_query.run(as_dict=True)
        if customer_by_email_result:
            existing_customer_name = customer_by_email_result[0].get("name")

        if not existing_customer_name:
            ContactEmail = DocType("Contact Email")
            DynamicLink = DocType("Dynamic Link")
            contact_query = (
                frappe.qb.from_(ContactEmail)
                .inner_join(DynamicLink)
                .on(ContactEmail.parent == DynamicLink.parent)
                .select(DynamicLink.link_name)
                .where(Lower(ContactEmail.email_id) == customer_email_lower)
                .where(DynamicLink.link_doctype == "Customer")
                .limit(1)
            )
            contact_email_result = contact_query.run(as_dict=True)
            if contact_email_result:
                existing_customer_name = contact_email_result[0].get("link_name")

    # If no customer_id or email match, try to match by name from order
    potential_customer_name: str | None = None
    ship_to = getattr(order, "ship_to", None)
    bill_to = getattr(order, "bill_to", None)

    ship_to_name = getattr(ship_to, "name", None) if ship_to else None
    bill_to_name = getattr(bill_to, "name", None) if bill_to else None

    if ship_to_name:
        potential_customer_name = ship_to_name
    elif bill_to_name:
        potential_customer_name = bill_to_name

    if not existing_customer_name and potential_customer_name:
        CustomerDocType = DocType("Customer")
        customer_by_name_query = (
            frappe.qb.from_(CustomerDocType)
            .select(CustomerDocType.name)
            .where(CustomerDocType.customer_name == potential_customer_name)
            .limit(1)
        )
        customer_by_name_result = customer_by_name_query.run(as_dict=True)
        if customer_by_name_result:
            existing_customer_name = customer_by_name_result[0].get("name")

    ss_customer: "ShipStationCustomer | None" = None
    if customer_id:
        client = settings.client()
        ss_customer_data = client.get_customer(customer_id)
        if not isinstance(ss_customer_data, str):
            ss_customer = ss_customer_data

    if (
        existing_customer_name
        and isinstance(existing_customer_name, str)
        and existing_customer_name != "None"
    ):
        cust: "Customer" = Customer("Customer", existing_customer_name)
        if customer_id and not cust.get("shipstation_customer_id"):
            cust.set("shipstation_customer_id", customer_id)
            cust.save(ignore_permissions=True)
        ss_customer_name = getattr(ss_customer, "name", None)
        if ss_customer_name and cust.customer_name != ss_customer_name:
            cust.customer_name = ss_customer_name
            cust.save(ignore_permissions=True)
        return cust

    # Create new customer
    cust: "Customer" = Customer({"doctype": "Customer"})

    determined_customer_name: str
    ss_customer_name = getattr(ss_customer, "name", None)
    if ss_customer_name:
        determined_customer_name = ss_customer_name
    elif potential_customer_name:
        determined_customer_name = potential_customer_name
    elif customer_email:
        determined_customer_name = customer_email
    elif customer_id:
        determined_customer_name = str(customer_id)
    else:
        determined_customer_name = frappe.generate_hash("", 10)

    cust.customer_name = determined_customer_name
    cust.name = cust.customer_name  # type: ignore

    if customer_id:
        cust.set("shipstation_customer_id", customer_id)

    cust.customer_type = (
        "Company" if ss_customer and getattr(ss_customer, "company", None) else "Individual"
    )
    cust.customer_group = str(settings.get("customer_group", "ShipStation"))
    cust.territory = str(settings.get("territory", "United States"))

    try:
        cust.insert(ignore_permissions=True, ignore_mandatory=True)
        frappe.db.commit()
    except DuplicateEntryError:
        # If a duplicate entry occurs, fetch the existing customer by the determined name.
        # This assumes `customer_name` is the field that would cause a duplicate error if not unique.
        return Customer("Customer", {"customer_name": determined_customer_name})
    except Exception as e:
        frappe.log_error(title="Error creating Shipstation Customer", message=e)
        raise e

    contact_customer_data: "ShipStationCustomer | ShipStationAddress | None" = None
    if ss_customer:
        contact_customer_data = ss_customer
    else:
        order_ship_to = getattr(order, "ship_to", None)
        order_bill_to = getattr(order, "bill_to", None)

        if order_ship_to and getattr(order_ship_to, "name", None):
            contact_customer_data = order_ship_to
        elif order_bill_to and getattr(order_bill_to, "name", None):
            contact_customer_data = order_bill_to

        # It's important that contact_customer_data has an 'email' attribute if create_contact_from_customer expects it.
        # ShipStationAddress might not have it. This logic remains as is, assuming create_contact_from_customer handles it.
        # if contact_customer_data and customer_email:
        # setattr(contact_customer_data, 'email', customer_email) # This is risky due to type differences

    if contact_customer_data:
        contact = create_contact_from_customer(contact_customer_data, cust.name)
        if contact:
            cust.set("customer_primary_contact", contact.name)

    try:
        cust.save(ignore_permissions=True)
        frappe.db.commit()
        return cust
    except Exception as e:
        frappe.log_error(title="Error saving Shipstation Customer", message=e)
        raise e


def create_contact_from_customer(
    customer: Union["ShipStationCustomer", "ShipStationAddress"], customer_name: str | None = None
) -> "Contact | None":
    """Create a contact from ShipStation customer data"""
    contact_doc_name: str | None = None
    # Safely get email, as ShipStationAddress might not have it.
    customer_email_attr = getattr(customer, "email", None)

    if customer_email_attr and isinstance(customer_email_attr, str):
        email_lower = customer_email_attr.strip().lower()
        ContactEmail = DocType("Contact Email")
        contact_query = (
            frappe.qb.from_(ContactEmail)
            .select(ContactEmail.parent)
            .where(Lower(ContactEmail.email_id) == email_lower)
            .limit(1)
        )
        contact_result = contact_query.run(as_dict=True)
        if contact_result:
            parent_name = contact_result[0].get("parent")
            if parent_name and isinstance(parent_name, str):
                contact_doc_name = parent_name

    cont: "Contact"
    if contact_doc_name:
        cont = Contact("Contact", contact_doc_name)
    else:
        cont = Contact({"doctype": "Contact"})

    customer_name_attr = getattr(customer, "name", None)
    if not customer_name_attr and customer_email_attr and isinstance(customer_email_attr, str):
        cont.first_name = customer_email_attr
    elif customer_name_attr and isinstance(customer_name_attr, str):
        name_parser = HumanName(customer_name_attr)
        cont.first_name = name_parser.first
        cont.middle_name = name_parser.middle
        cont.last_name = name_parser.last
        cont.set("designation", name_parser.suffix)  # Use .set for potentially non-standard fields
        title = re.sub(r"[^\\w\\s]", "", name_parser.title.strip().title())
        if title:
            title_exists = frappe.db.exists("Salutation", title)
            if not title_exists:
                salutation_doc = frappe.new_doc("Salutation")
                salutation_doc.set("salutation", title)  # Use .set for new_doc as well before save
                salutation_doc.save(ignore_permissions=True)
                frappe.db.commit()
            cont.set("salutation", title)
    else:
        # If no name and no email, cannot create a meaningful contact.
        return None

    company_attr = getattr(customer, "company", None)
    if company_attr and isinstance(company_attr, str):
        cont.set("company_name", company_attr)

    phone_attr = getattr(customer, "phone", None)
    if phone_attr and isinstance(phone_attr, str):
        cont.append("phone_nos", {"phone": phone_attr})

    if customer_email_attr and isinstance(customer_email_attr, str):
        cont.append("email_ids", {"email_id": customer_email_attr})

    if customer_name:  # This is the ERPNext customer name (link)
        cont.append("links", {"link_doctype": "Customer", "link_name": customer_name})

    try:
        cont.save(ignore_permissions=True)
        frappe.db.commit()
        return cont
    except Exception as e:
        frappe.log_error(title="Error saving Shipstation Contact", message=e)
        return None


def overwrite_validate_phone_number(data, throw=False):
    return True


def get_billing_address(customer_name: str):
    Address = DocType("Address")
    DynamicLink = DocType("Dynamic Link")

    query = (
        frappe.qb.from_(Address)
        .join(DynamicLink)
        .on(DynamicLink.parent == Address.name)
        .select(Address.name)
        .where(DynamicLink.link_doctype == "Customer")
        .where(DynamicLink.link_name == customer_name)
        .where(Address.address_type == "Billing")
        .limit(1)
    )

    result = query.run(pluck="name")
    return (
        result[0]
        if result
        else frappe.db.get_value("Customer", customer_name, "customer_primary_address")
    )


def match_or_create_address(
    address: "ShipStationAddress", customer: str, email: str, address_type: AddressType
) -> "Address | None":
    """Match existing address or create new one based on ShipStation address data"""
    if not address or not address.street1:
        return None

    AddressDocType = DocType("Address")

    city_lower = address.city.lower() if address.city else ""
    street1_lower = address.street1.lower() if address.street1 else ""

    existing_address_query = (
        frappe.qb.from_(AddressDocType)
        .select(AddressDocType.name)
        .where(Lower(AddressDocType.address_line1) == street1_lower)
        .where(Lower(AddressDocType.city) == city_lower)
        .limit(1)
    )
    existing_address_result = existing_address_query.run(pluck="name")

    if existing_address_result and existing_address_result[0]:
        addr_name = existing_address_result[0]
        if isinstance(addr_name, str):
            addr: "Address" = Address("Address", addr_name)
            has_customer_link = frappe.db.exists(
                "Dynamic Link",
                {"parent": addr.name, "link_doctype": "Customer", "link_name": customer},
            )

            if not has_customer_link:
                addr.append("links", {"link_doctype": "Customer", "link_name": customer})

            updated_addr = _update_address(address, addr, email, address_type)
            return updated_addr  # _update_address now returns Address or raises error

    # Create new address if no match found
    new_addr = create_address(address, customer, email, address_type)
    return new_addr  # create_address now returns Address or raises error
