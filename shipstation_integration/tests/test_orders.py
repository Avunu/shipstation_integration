# Copyright (c) 2026, AgriTheory and Contributors
# See license.txt

"""
Tests for order creation helpers.
"""

from decimal import Decimal

from shipstation.models import ShipStationOrderItem

from shipstation_integration.orders import get_discount_amount


def _structured_item(quantity: int | float, unit_price: float) -> ShipStationOrderItem:
	"""Build an order item through the client's own JSON structuring.

	Going through `.json()` rather than the constructor is the point: that's the
	path that turns every numeric field into a `Decimal`, which is what the
	arithmetic below has to survive.
	"""
	raw = (
		'{"orderItemId": "1", "lineItemKey": "discount", "sku": "promo-code",'
		f' "name": "Discount", "quantity": {quantity}, "unitPrice": {unit_price}}}'
	)
	return ShipStationOrderItem(name="Discount").json(raw)


def test_shipstation_quantities_are_decimals():
	"""Guards the assumption the fix rests on."""
	item = _structured_item(quantity=2, unit_price=-5.50)
	assert isinstance(item.quantity, Decimal)
	assert isinstance(item.unit_price, Decimal)


def test_discount_amount_with_decimal_quantity():
	"""Regression: float rate x Decimal quantity used to raise TypeError.

	`rate` is coerced with `flt` by the caller but `quantity` was left as the
	Decimal the client produced, so every marketplace discount line blew up with
	"unsupported operand type(s) for *: 'float' and 'decimal.Decimal'".
	"""
	item = _structured_item(quantity=2, unit_price=-5.50)
	assert get_discount_amount(float(item.unit_price), item.quantity) == 11.0


def test_discount_amount_is_positive():
	"""Discount lines carry a negative unit price; the total is accumulated positive."""
	item = _structured_item(quantity=1, unit_price=-19.99)
	assert get_discount_amount(float(item.unit_price), item.quantity) == 19.99


def test_discount_amount_with_fractional_quantity():
	item = _structured_item(quantity=1.5, unit_price=-10.00)
	assert get_discount_amount(float(item.unit_price), item.quantity) == 15.0


def test_discount_amount_handles_missing_values():
	"""`rate` defaults to 0.0 upstream when there's no unit price; quantity may be unset."""
	assert get_discount_amount(0.0, None) == 0.0
	assert get_discount_amount(0.0, Decimal(3)) == 0.0
