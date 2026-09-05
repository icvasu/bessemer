# Data Dictionary — `ride_data_trip`

**Files:** `Ride_data _trip-may_2026.csv` (188,992 rows), `Ride_data _trip-June_2026.csv` (210,669 rows), `Ride_data _trip-July_2026.csv` (215,885 rows)
**Grain:** one row = one cab trip

The trip spine of the dataset — one row per trip across three monthly files
(May–July 2026), covering timing, delays, distance, vendor, cab details,
compliance flags, and planned-vs-actual headcount. This is usually your starting
table; the other files hang off its `trip_id`.

| Column | Type | Meaning | Values (observed, May file) |
|---|---|---|---|
| `business_unit` | object | Client account / business unit operating the fleet | 5 values: `vanta-Aus`, `catalyst-Sac`, `orbit-Slc`, `vanta-Sea`, `pinnacle-Slc` |
| `office` | object | Office or site the trip serves | 17 values, e.g. `Cedar Ridge Office` |
| `product_type` | object | Trip service type | `CAB`, `BUS`, `SPOT_2.0` |
| `trip_date` | object | Calendar date of the trip, free-text `"Month D, YYYY"` | 31 distinct dates, e.g. `"May 1, 2026"` |
| `shift_type` | object | Employee shift this trip is scheduled for | 99 values, `HH:MM` shift-start times, e.g. `"00:15"`, `"04:15"` |
| `trip_id` | object | Unique trip identifier (comma-formatted) | 188,992 unique, e.g. `"1,097,662"` |
| `trip_direction` | object | Login (pickup) or logout (drop) leg | `LOGIN`, `LOGOUT` |
| `actual_escort` | bool | Whether an escort was actually present on the trip | `True`, `False` |
| `vendor_id` | object | Cab vendor identifier | 23 vendor names, e.g. `"Sneha Mikhailov Travel"` |
| `planned_cab_registration` | object | Registration plate of the planned cab | 3,701 unique plates, e.g. `"TSC 921 GP"`; 51 nulls |
| `actual_cab_registration` | object | Registration plate of the cab that actually ran | 3,497 unique plates, no nulls |
| `actual_cab_capacity` | int64 | Seating capacity of the cab that ran the trip | 3, 4, 5, 6, 12 |
| `planned_km` | float64 | Planned trip distance in km | 0.0 – 65.53 |
| `traveled_km` | float64 | Actual distance traveled in km | 0.0003 – 148.05 |
| `planned_start_epoch` | object | Planned trip start (Unix epoch seconds), comma-formatted | e.g. `"1,777,595,400"` |
| `planned_end_epoch` | object | Planned trip end (Unix epoch seconds), comma-formatted | e.g. `"1,777,598,280"` |
| `actual_start_epoch` | object | Actual trip start (Unix epoch seconds), comma-formatted | e.g. `"1,777,594,061"` |
| `actual_end_epoch` | object | Actual trip end (Unix epoch seconds), comma-formatted | e.g. `"1,777,597,937"` |
| `delay_reason` | object | Reason recorded for the delay, if any | `NODELAY`, `TRAFFIC`, `DRIVER`, `EMPLOYEE` |
| `delay_minutes` | object | Delay duration in minutes, comma-formatted | 248 distinct values, `"0"` up to `"10,644"` |
| `route_source` | object | System/source that generated the route | `AUTO`, `MANUAL`, `RENTLZ`, `SHUTTLE_SERVICE` |
| `actual_cab_fuel_type` | object | Fuel type of the cab that ran the trip | `Diesel`, `Electric`, `Petrol` |
| `is_driver_nc` | bool | Whether the driver was flagged non-compliant | `True`, `False`; 4 nulls in May |
| `is_cab_nc` | bool | Whether the cab was flagged non-compliant | `True`, `False`; 4 nulls in May |
| `trip_nodal` | object | Nodal point/hub associated with the trip | `NODAL`, `HOME`, `SHUTTLE`; 106,536 nulls (non-nodal trips) |
| `plannedemployee_cnt` | int64 | Number of employees planned on the trip | 0 – 13 |
| `actualemployee_cnt` | int64 | Number of employees who actually boarded | 1 – 14 |
| `noshow_cnt` | int64 | Number of employees who did not show up | 0 – 12 |

**Heads-up for this file**

- **Types drift across the three months** — if you concat May + June + July,
  reconcile these first:
  - `is_driver_nc` / `is_cab_nc`: `bool` in June/July, `object` (with nulls) in
    May.
  - `planned_km`: `float` in May/June, `object` in July (one comma-formatted
    value sneaks in).
- `trip_id`, the four `*_epoch` columns, and `delay_minutes` are comma-formatted
  strings in **all three** files — strip commas before numeric use or joining
  (see the [dataset guide](./README.md)).
- `trip_nodal` is null for non-nodal (home) trips — expected, not missing.

**Ideas to explore:** on-time arrival (OTA) vs an SLA target, delay-reason
breakdown by vendor / office, fuel-mix and sustainability trends, capacity
utilisation (`actualemployee_cnt` vs `actual_cab_capacity`), month-over-month
delay trend.
