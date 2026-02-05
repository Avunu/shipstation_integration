# Copyright (c) 2023, Parsimony LLC and contributors
# For license information, please see license.txt

# import frappe
from frappe.model.document import Document


class ShipstationItemCustomField(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		default: DF.SmallText | None
		fetch_from: DF.SmallText | None
		fetch_if_empty: DF.Check
		fieldname: DF.Data
		fieldtype: DF.Literal["", "Check", "Data", "Link", "Select", "Small Text", "Text"]
		hidden: DF.Check
		label: DF.Data
		length: DF.Data | None
		options: DF.SmallText | None
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		read_only: DF.Check
		reqd: DF.Check
	# end: auto-generated types
	pass
