# Copyright (c) 2026, AgriTheory and Contributors
# See license.txt

"""
Tests for background order processing error handling.

The mirror of test_shipment_processing.py, for orders. These tests verify that
failures in create_erpnext_order are:
  - caught without crashing the background job
  - rolled back cleanly via savepoint
  - logged to the Error Log with a consistent title prefix
  - isolated to the one order, so list_orders() finishes the rest of the batch

That last one is the point. Before this, a single unprocessable order aborted
list_orders() outright, so every *remaining* order in the window silently went
uncreated on every run, for as long as the bad record existed.
"""

from unittest.mock import MagicMock, patch

import frappe
import pytest
from shipstation.models import ShipStationOrder

ORDER_ID = "test-shipstation-order-88888"
ERROR_LOG_TITLE = f"Shipstation: order {ORDER_ID}"


@pytest.fixture
def mock_order():
	order = MagicMock(spec=ShipStationOrder)
	order.order_id = ORDER_ID
	order.order_number = "TEST-88888"
	return order


@pytest.fixture
def mock_store():
	store = MagicMock()
	store.customer = None
	store.store_name = "Test Store"
	return store


@pytest.fixture
def mock_settings():
	settings = MagicMock()
	settings.shipstation_user = None
	return settings


@pytest.fixture(autouse=True)
def cleanup_error_log():
	"""Remove any Error Log entries created by these tests after each test."""
	yield
	for name in frappe.get_all("Error Log", filters={"method": ERROR_LOG_TITLE}, pluck="name"):
		frappe.delete_doc("Error Log", name, force=True)


def test_create_erpnext_order_returns_none_on_failure(mock_order, mock_store, mock_settings):
	"""Failed order processing returns None instead of raising."""
	with patch(
		"shipstation_integration.orders._create_erpnext_order",
		side_effect=frappe.ValidationError("Sales UOM not found"),
	):
		result = create_erpnext_order(mock_order, mock_store, mock_settings)

	assert result is None


def test_create_erpnext_order_logs_error_with_order_id(mock_order, mock_store, mock_settings):
	"""Error log entry is created with the order ID in the title."""
	with patch(
		"shipstation_integration.orders._create_erpnext_order",
		side_effect=frappe.ValidationError("Sales UOM not found"),
	):
		create_erpnext_order(mock_order, mock_store, mock_settings)

	error_log_name = frappe.db.get_value("Error Log", {"method": ERROR_LOG_TITLE}, "name")
	assert error_log_name, f"Expected Error Log entry with title '{ERROR_LOG_TITLE}'"


def test_create_erpnext_order_survives_a_released_savepoint(mock_order, mock_store, mock_settings):
	"""
	_create_erpnext_order() commits once the Sales Order is submitted, and a
	commit releases every open savepoint. A failure after that point (an
	after-submit hook, the tag loop) therefore has no savepoint to roll back
	to, and the rollback itself raises. That must not escape and kill the
	batch — the whole reason this guard exists.
	"""

	def rollback(save_point=None):
		# Only the savepoint form fails, which is what a released savepoint
		# actually does; the plain rollback still works.
		if save_point:
			raise Exception("SAVEPOINT create_erpnext_order does not exist")

	with (
		patch(
			"shipstation_integration.orders._create_erpnext_order",
			side_effect=frappe.ValidationError("failed after commit"),
		),
		patch.object(frappe.db, "rollback", side_effect=rollback) as mock_rollback,
	):
		result = create_erpnext_order(mock_order, mock_store, mock_settings)

	assert result is None
	# Tried the savepoint, then fell back to a plain rollback.
	assert mock_rollback.call_count == 2
	assert mock_rollback.call_args_list[0].kwargs == {"save_point": "create_erpnext_order"}
	assert mock_rollback.call_args_list[1].args == ()


def create_erpnext_order(order, store, settings):
	from shipstation_integration.orders import create_erpnext_order as _fn

	return _fn(order, store, settings)
