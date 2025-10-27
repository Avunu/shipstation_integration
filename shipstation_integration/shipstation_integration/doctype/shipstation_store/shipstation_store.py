# Copyright (c) 2020, Parsimony LLC and contributors
# For license information, please see license.txt


from frappe.model.document import Document


class ShipstationStore(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		amazon_marketplace: DF.Data | None
		company: DF.Link | None
		cost_center: DF.Link | None
		create_delivery_note: DF.Check
		create_sales_invoice: DF.Check
		create_shipment: DF.Check
		enable_orders: DF.Check
		enable_shipments: DF.Check
		expense_account: DF.Link | None
		is_amazon_store: DF.Check
		marketplace_name: DF.Data | None
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		sales_account: DF.Link | None
		shipping_expense_account: DF.Link | None
		shipping_income_account: DF.Link | None
		store_id: DF.Data | None
		store_name: DF.Data | None
		tax_account: DF.Link | None
		warehouse: DF.Link | None
	# end: auto-generated types
	pass
