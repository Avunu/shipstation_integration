import frappe
from erpnext.selling.doctype.sales_order.sales_order import SalesOrder
from shipstation_integration.orders import update_shipstation_order_status

class ShipStationSalesOrder(SalesOrder):
	def calculate_commission(self):
		commission_formula = frappe.get_cached_value(
			"Sales Partner", self.sales_partner, "commission_formula"
		)
		if not self.shipstation_order_id or not commission_formula:
			super().calculate_commission()
		elif self.shipstation_order_id and commission_formula:
			self.total_commission = get_formula_based_commission(self, commission_formula)
	
	def get_sss(self):
		sss_name, store_id = frappe.db.get_value(
				"Shipstation Store",
				{
					"store_name": self.shipstation_store_name,
					"marketplace_name": self.marketplace,
				},
				["parent","store_id"],
		)
		if sss_name:
			return frappe.get_doc("Shipstation Settings", sss_name), store_id
		else:
			return None, None
	
	# synchronize status with shipstation, depends on sync_so_status in Shipstation Settings
	def on_change(self):
		if self.shipstation_order_id and self.has_value_changed("status") and self.status not in ["Draft", "Closed"]:
			sss, store_id = self.get_sss()
			if sss and sss.enabled and sss.sync_so_status:
				billing_address = self.address_display
				shipping_address = self.shipping_address
				# we need to make a dict for each address
				# this is what ss is expecting
				# TODO: figure out how much of this stuff we can get away with not sending
				# "name": "The President",
				# "company": "US GOVT",
				# "street1": "1600 PENNSYLVANIA AVE NW",
				# "street2": "OVAL OFFICE",
				# "street3": null,
				# "city": "WASHINGTON",
				# "state": "DC",
				# "postalCode": "20500-0005",
				# "country": "US",
				# "phone": "555-555-5555",
				# "residential": null,
				# "addressVerified": null
				# to get the name, we need to go from so.customer to customer to customer.customer_primary_contact to contact to contact.full_name
				# the addresses on the so are strings, with 4 lines. split the lines and assign them to the dict. the first three lines are easy, they correlate to street1, street2, street3. the fourth line is the city, state, postal code. the city is before a comma, the state is after the comma and before the space, the postal code is after the space. the country will be so.territory
				# country will be so.territory
				update_shipstation_order_status(settings=sss, order_id=self.shipstation_order_id, status=self.status, store_id=store_id, order_date=self.transaction_date)

def get_formula_based_commission(doc, commission_formula=None):
	if not commission_formula:
		commission_formula = frappe.get_cached_value(
			"Sales Partner", doc.sales_partner, "commission_formula"
		)

	eval_globals = frappe._dict(
		{
			"frappe": frappe._dict({"get_value": frappe.db.get_value, "get_all": frappe.db.get_all}),
			"flt": frappe.utils.data.flt,
			"min": min,
			"max": max,
			"sum": sum,
		}
	)
	eval_locals = {
		"doc": doc,
	}

	try:
		return frappe.safe_eval(commission_formula, eval_globals=eval_globals, eval_locals=eval_locals)
	except Exception as e:
		print("Error evaluating commission formula:\n", e)
		e = f"{e}\n{doc.as_dict()}"
		frappe.log_error(
			title=f"Error evaluating commission formula for {doc.sales_partner or 'No Sales Partner'}",
			message=e,
		)
		return None


# flt((( doc.grand_total * 0.1325) * 0.9)  + 0.40, 2)
# flt(((
# 	(min(doc.total, 2500) * 0.1235) + (max(doc.total - 2500, 0) *.0235)
# ) * 0.9)  + 0.40, 2)
# sum([r.amount * .16 for r in doc.items if not r.is_free_item]) # 16% per item
