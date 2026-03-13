from . import __version__ as app_version

app_color = "grey"
app_description = "Shipstation integration for ERPNext"
app_email = "developers@parsimony.com"
app_icon = "octicon octicon-file-directory"
app_include_js = ["shipstation_integration.bundle.js"]
app_license = "MIT"
app_name = "shipstation_integration"
app_publisher = "Parsimony LLC"
app_title = "Shipstation Integration"

doctype_js = {
    "Delivery Note": "public/js/delivery_note.js",
    "Sales Order": "public/js/sales_order.js",
}

export_python_type_annotations = True

scheduler_events = {
    "hourly_long": [
        "shipstation_integration.orders.list_orders",
        "shipstation_integration.shipments.list_shipments",
    ]
}

fixtures = [
    {
        "dt": "Price List",
        "filters": {"price_list_name": "ShipStation"},
    },
    {
        "dt": "Customer Group",
        "filters": {"customer_group_name": "ShipStation"},
    },
]
