# Data Dictionary — `emp_data`

**File:** `emp_data.csv` (1,637,906 rows)
**Grain:** one row = one employee's leg of a trip

The rider-level view: every employee's pickup, drop, boarding status, no-shows,
and planned-vs-actual distance. Because it's one row *per employee per trip*,
expect many rows under a single `trip_id`. It's also the **cleanest** file for
join keys — `trip_id` and `stwid` are already proper numeric types here.

| Column | Type | Meaning | Values (observed) |
|---|---|---|---|
| `business_unit` | object | Client account / business unit operating the fleet | 5 values: `catalyst-Sac`, `pinnacle-Slc`, `vanta-Aus`, `vanta-Sea`, `orbit-Slc` |
| `office` | object | Office or site the trip serves | 19 values, e.g. `Fairview Commons` |
| `product_type` | object | Trip service type | `CAB`, `BUS`, `SPOT_2.0` |
| `trip_date` | object | Calendar date of the trip, ISO `"YYYY-MM-DD"` | 92 distinct dates, e.g. `"2026-07-09"` |
| `shift_type` | object | Employee shift this trip is scheduled for | 100 values, `HH:MM` shift-start times, e.g. `"21:30"` |
| `trip_id` | int64 | Unique trip identifier — join key to the other files | 1,097,349 – 5,134,140 |
| `planned_pickup_epoch` | float64 | Planned pickup time (Unix epoch seconds) | 1,777,593,600 – 1,785,541,500; 112,943 nulls |
| `planned_drop_epoch` | float64 | Planned drop time (Unix epoch seconds) | 1,777,594,380 – 1,785,549,960; 112,943 nulls |
| `actual_pickup_epoch` | float64 | Actual pickup time (Unix epoch seconds); null if not picked up | 1,777,593,631 – 1,785,543,821; 190,009 nulls |
| `actual_drop_epoch` | float64 | Actual drop time (Unix epoch seconds); null if not dropped | 1,777,594,720 – 1,785,549,446; 190,010 nulls |
| `planned_km` | float64 | Planned distance for this leg, in km | **-2.0** – 1,092.56 — negatives are invalid |
| `traveled_km` | float64 | Actual distance for this leg, in km | **-6.63** – 829.21 — negatives are invalid |
| `stwid` | int64 | Employee/rider identifier — join key to `alerts_data` and `trip_feedback` | 0 – 800,995; `0` is a placeholder, not a real employee |
| `signintype` | object | How the trip request was created | `Planned`, `Adhoc`, `Guest`; 190,009 nulls |
| `gender` | object | Employee gender | `MALE`, `FEMALE`; 1,559 nulls |
| `emp_role` | object | Role of the person in the system | 16 values, mostly `employee`; 1,414 nulls |
| `boarding_status` | object | Whether the employee boarded | `Boarded`, `Not Boarded` |
| `not_boarding_reason` | object | Reason the employee didn't board; populated only for no-shows | `NO_SHOW`, `TRIP_CANCELLED_FROM_DASHBOARD`, `NON_COMMUNICATING`; 1,447,897 nulls |
| `is_no_show` | bool | Whether this employee was a no-show | `True`, `False` |

**Heads-up for this file**

- A raw `employee_id` column was removed during anonymisation — `stwid` is your
  rider key, and `stwid = 0` is a placeholder, not a real person.
- Join keys are clean `int64` here, but they're comma-formatted strings in
  `ride_data_trip`, `alerts_data`, and `trip_feedback` — normalise the *other*
  side when joining (see the [dataset guide](./README.md)).
- `trip_date` is ISO `YYYY-MM-DD` here; every other file uses free-text dates.
- The `*_epoch` columns are floats here but comma-formatted strings in
  `ride_data_trip`.
- `planned_km` / `traveled_km` go **negative**, which can't happen physically —
  drop, clip, or flag them. (A tidy "handle messy data gracefully" win.)

**Ideas to explore:** no-show patterns by shift / office / gender, on-time
pickup rates, planned-vs-actual distance drift, ripple of late pickups into
shift readiness for a line manager.
