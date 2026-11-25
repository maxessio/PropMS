# Copyright (c) 2026, Aakvatech and Contributors
# See license.txt

import unittest
from datetime import date

from frappe import _dict

from propms.lease_invoice_schedule import (
	get_effective_lease_item,
	get_legacy_owner_key,
	group_lease_items,
	schedule_row_exists,
)


def make_item(lease_item="Parking", unit=None, amount=50.0, valid_from=None):
	return _dict(lease_item=lease_item, property_unit=unit, amount=amount, valid_from=valid_from)


def make_schedule_row(lease_item="Parking", unit=None, day=date(2026, 4, 1)):
	return _dict(lease_item=lease_item, property_unit=unit, date_to_invoice=day)


class TestGroupLeaseItems(unittest.TestCase):
	def test_same_item_on_two_units_are_two_positions(self):
		items = [make_item(unit="P-1"), make_item(unit="P-2"), make_item("Rent", amount=600.0)]
		self.assertEqual(
			list(group_lease_items(items)), [("Parking", "P-1"), ("Parking", "P-2"), ("Rent", "")]
		)

	def test_rent_steps_of_one_unit_stay_one_position(self):
		items = [
			make_item(unit="P-1", amount=50.0, valid_from=date(2026, 1, 1)),
			make_item(unit="P-1", amount=55.0, valid_from=date(2027, 1, 1)),
		]
		self.assertEqual([len(rows) for rows in group_lease_items(items).values()], [2])


class TestEffectiveLeaseItem(unittest.TestCase):
	def setUp(self):
		self.rows = [
			make_item(amount=50.0, valid_from=date(2026, 4, 1)),
			make_item(amount=55.0, valid_from=date(2027, 1, 1)),
		]

	def test_position_is_not_billed_before_its_first_valid_from(self):
		self.assertIsNone(get_effective_lease_item(self.rows, date(2026, 3, 1)))

	def test_latest_step_on_or_before_the_period_applies(self):
		self.assertEqual(get_effective_lease_item(self.rows, date(2026, 4, 1)).amount, 50.0)
		self.assertEqual(get_effective_lease_item(self.rows, date(2026, 12, 1)).amount, 50.0)
		self.assertEqual(get_effective_lease_item(self.rows, date(2027, 1, 1)).amount, 55.0)

	def test_row_without_valid_from_applies_from_lease_start(self):
		rows = [make_item(amount=40.0), make_item(amount=50.0, valid_from=date(2026, 4, 1))]
		self.assertEqual(get_effective_lease_item(rows, date(2026, 1, 1)).amount, 40.0)
		self.assertEqual(get_effective_lease_item(rows, date(2026, 4, 1)).amount, 50.0)

	def test_setting_off_keeps_the_first_row(self):
		self.assertEqual(get_effective_lease_item(self.rows, date(2026, 3, 1), False).amount, 50.0)


class TestScheduleRowExists(unittest.TestCase):
	def test_row_of_another_unit_does_not_block_the_position(self):
		existing = [make_schedule_row(unit="P-1")]
		self.assertTrue(schedule_row_exists(existing, ("Parking", "P-1"), date(2026, 4, 1)))
		self.assertFalse(schedule_row_exists(existing, ("Parking", "P-2"), date(2026, 4, 1)))

	def test_other_date_or_item_does_not_match(self):
		existing = [make_schedule_row(unit="P-1")]
		self.assertFalse(schedule_row_exists(existing, ("Parking", "P-1"), date(2026, 5, 1)))
		self.assertFalse(schedule_row_exists(existing, ("Rent", "P-1"), date(2026, 4, 1)))

	def test_legacy_row_without_unit_counts_only_for_its_owner(self):
		existing = [make_schedule_row(unit=None)]
		self.assertTrue(schedule_row_exists(existing, ("Parking", "P-1"), date(2026, 4, 1), True))
		self.assertFalse(schedule_row_exists(existing, ("Parking", "P-2"), date(2026, 4, 1), False))


class TestLegacyOwnerKey(unittest.TestCase):
	def test_position_without_unit_owns_legacy_rows(self):
		items = [make_item(unit="P-1"), make_item(unit=None)]
		self.assertEqual(get_legacy_owner_key(items, "Parking"), ("Parking", ""))

	def test_otherwise_the_first_position_of_the_item(self):
		items = [make_item("Rent"), make_item(unit="P-1"), make_item(unit="P-2")]
		self.assertEqual(get_legacy_owner_key(items, "Parking"), ("Parking", "P-1"))
