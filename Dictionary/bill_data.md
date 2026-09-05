# Data Dictionary — `bill_data`

**File:** `bill_data.csv` (620,942 rows)
**Grain:** one row = one billed trip line item

The money view — what each trip cost, how far it was billed for, under which
vendor, contract, and slab. Join to the rest of the dataset on `trip_id`. Your
go-to table for cost analytics, budget tracking, and vendor spend.

| Column | Type | Meaning | Values (observed) |
|---|---|---|---|
| `business_unit` | object | Client account / business unit being billed | 5 values: `vanta-Aus`, `catalyst-Sac`, `orbit-Slc`, `vanta-Sea`, `pinnacle-Slc` |
| `office` | object | Office or site the trip served | 19 values, e.g. `Cedar Ridge Office` |
| `vendor` | object | Cab vendor being billed | 24 vendor names, e.g. `"Priya Mikhailov Travel"` |
| `cycle_start` | object | Billing cycle start date/time | 6 semi-monthly cycles, e.g. `"May 1, 2026, 12:00 AM"`, `"May 16, 2026, 12:00 AM"` |
| `cycle_end` | object | Billing cycle end date/time | 6 semi-monthly cycles, e.g. `"May 15, 2026, 12:00 AM"`, `"May 31, 2026, 12:00 AM"` |
| `trip_id` | object | Trip identifier — join key to the other files | 613,784 unique, **no commas** here, e.g. `"1123974"` |
| `contract` | object | Contract type/name governing the billing rate | 47 values, e.g. `"4S-EV-Z"`, `"6S-HYD"`; 11 nulls |
| `slab_name` | object | Billing slab/tier applied to the trip; null for ~20% of rows | 28 values, e.g. `Medium`, `Long`; 124,912 nulls |
| `total_trip_km` | float64 | Distance billed for the trip, in kilometers | 0.0 – 980.5 (0.0 appears for a meaningful share of rows) |
| `trip_cost` | object | Billed cost for the trip; comma-formatted — strip commas before math | 2,050 distinct values, e.g. `"1,200"`, `"1,800"` |

**Heads-up for this file**

- Unlike every other file, `trip_id` here is a **plain numeric string with no
  commas** (`"1123974"`). When you join `bill_data` to the comma-formatted files,
  normalise both sides first (see the [dataset guide](./README.md)).
- `trip_cost` is comma-formatted (`"1,200"`) — strip commas before casting to
  numeric.
- `total_trip_km = 0.0` shows up on a meaningful share of rows, and `slab_name`
  is null ~20% of the time. Decide how to treat these rather than assuming clean.

**Ideas to explore:** cost per km by vendor / contract, spend trend across the
six billing cycles, outlier trips (high cost, zero km), slab-mix shifts,
budget-vs-actual for a facilities head.
