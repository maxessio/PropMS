import frappe
from frappe import _
from frappe.query_builder import DocType
from frappe.utils import add_days, add_months, get_first_day, get_last_day, getdate, today

from propms.auto_custom import app_error_log, makeInvoiceSchedule


def get_aligned_invoice_date(date):
	"""Returns the first day of the month for the given date"""
	return get_first_day(date)


def get_schedule_key(row):
	"""A lease position is identified by its item and the unit it bills."""
	return (row.get("lease_item"), row.get("property_unit") or "")


def group_lease_items(lease_items):
	"""Group the Lease Item rows by position, keeping the table order."""
	groups = {}
	for row in lease_items:
		groups.setdefault(get_schedule_key(row), []).append(row)
	return groups


def get_effective_lease_item(rows, on_date, use_valid_from_date=True):
	"""Return the row of one position that applies on on_date, or None before its first valid_from.

	A row without valid_from applies from the start of the lease. Without the
	"use valid from date" setting the first row of the position always applies.
	"""
	if not rows:
		return None
	if not use_valid_from_date:
		return rows[0]
	effective = None
	for row in rows:
		valid_from = getdate(row.get("valid_from")) if row.get("valid_from") else None
		if valid_from and valid_from > getdate(on_date):
			continue
		if effective is None or (valid_from and (effective[0] is None or valid_from >= effective[0])):
			effective = (valid_from, row)
	return effective[1] if effective else None


def get_legacy_owner_key(lease_items, lease_item):
	"""Position that owns schedule rows written before the unit was stored on them.

	The position without a unit if there is one, otherwise the first position of the item.
	"""
	keys = [key for key in group_lease_items(lease_items) if key[0] == lease_item]
	if (lease_item, "") in keys:
		return (lease_item, "")
	return keys[0] if keys else None


def schedule_row_exists(existing_rows, key, invoice_date, covers_legacy_rows=False):
	"""True if the lease already has a schedule row for this position and date."""
	for row in existing_rows:
		if row.get("lease_item") != key[0] or getdate(row.get("date_to_invoice")) != getdate(invoice_date):
			continue
		unit = row.get("property_unit") or ""
		if unit == key[1] or (covers_legacy_rows and not unit):
			return True
	return False


@frappe.whitelist()
def make_lease_invoice_schedule():
	# First check if make_invoice_schedule_up_to_tomorrow_only is enabled
	settings = frappe.get_single("Property Management Settings")
	make_schedule_up_to_tomorrow = settings.get("make_invoice_schedule_up_to_tomorrow_only", 0)
	if not make_schedule_up_to_tomorrow:
		return

	# Proceed with other computations only if the setting is enabled
	use_valid_from_date = settings.use_valid_from_date
	today_date = getdate(today())
	next_month_end = get_last_day(add_months(today_date, 1))  # End of next month
	invoice_start_date = getdate(settings.get("invoice_start_date", None))

	if not invoice_start_date:
		frappe.throw(_("Please set Invoice Start Date in Property Management Settings"))

	Lease = DocType("Lease")
	query = (
		frappe.qb.from_(Lease)
		.select(Lease.name)
		.where(
			(Lease.start_date <= today_date)
			& (Lease.lease_status.isin(["Active", "Renewal to Previous Lease"]))
		)
	)
	lease_names = [row[0] for row in query.run()]

	for lease_name in lease_names:
		try:
			lease = frappe.get_doc("Lease", lease_name)
			lease_start = getdate(lease.start_date)
			schedule_end = getdate(lease.end_date) if lease.end_date else next_month_end

			# Use the later date between lease_start and invoice_start_date
			schedule_start = max(lease_start, invoice_start_date) if lease_start else invoice_start_date
			if not schedule_start:
				continue  # skip if no start_date

			# Clean up schedule entries for removed lease items
			lease_item_names = [li.lease_item for li in lease.lease_item]
			schedule_items = frappe.get_all(
				"Lease Invoice Schedule", filters={"parent": lease.name}, fields=["name", "lease_item"]
			)
			for s in schedule_items:
				if s.lease_item not in lease_item_names:
					frappe.delete_doc("Lease Invoice Schedule", s.name)

			# Frequency map
			freq_map = {
				"Monthly": 1.0,
				"Bi-Monthly": 2.0,
				"Quarterly": 3.0,
				"6 months": 6.0,
				"Annually": 12.0,
			}

			existing_rows = frappe.get_all(
				"Lease Invoice Schedule",
				filters={"parent": lease.name},
				fields=["lease_item", "property_unit", "date_to_invoice"],
			)

			idx = 1
			for key, rows in group_lease_items(lease.lease_item).items():
				# Rows without a unit predate the unit-aware schedule: they stay with one position.
				covers_legacy_rows = key == get_legacy_owner_key(lease.lease_item, key[0])

				# Get the first day of the month in which schedule_start falls
				invoice_date = get_first_day(schedule_start)

				# Generate invoice schedules up to next month
				while schedule_end >= invoice_date and invoice_date <= next_month_end:
					# The position that applies in this period: latest valid_from on or before it
					item = get_effective_lease_item(rows, invoice_date, use_valid_from_date)
					freq = freq_map.get(item.frequency) if item else None

					if item and item.frequency and not freq:
						frappe.log_error(
							f"Invalid frequency '{item.frequency}' for item {item.lease_item} in lease {lease.name}",
							"Invalid Frequency",
						)
						break

					if not freq:
						# Position not started yet (valid_from in the future) or without frequency
						invoice_date = add_months(invoice_date, 1)
						continue

					# Calculate period end as last day of the month after freq months
					invoice_period_end = get_last_day(add_months(invoice_date, freq - 1))

					# Only create schedule if date_to_invoice is between invoice start date and today + next month
					if schedule_start <= invoice_date <= next_month_end:
						if not schedule_row_exists(existing_rows, key, invoice_date, covers_legacy_rows):
							makeInvoiceSchedule(
								invoice_date,
								item.lease_item,
								item.paid_by,
								item.lease_item,
								lease.name,
								float(freq),
								item.amount,
								idx,
								item.currency_code,
								item.witholding_tax,
								lease.days_to_invoice_in_advance,
								item.invoice_item_group,
								item.document_type,
								property_unit=item.property_unit,
							)
							frappe.db.commit()
							idx += 1

					# Move to first day of next period
					invoice_date = add_days(invoice_period_end, 1)

			frappe.msgprint(_(f"Completed invoice schedule for Lease: {lease.name}"))

		except Exception as e:
			frappe.msgprint(_(f"Error in {lease_name}. Check app error log."))
			app_error_log(frappe.session.user, f"{lease_name}: {str(e)}")
