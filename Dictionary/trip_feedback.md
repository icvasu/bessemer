# Data Dictionary — `trip_feedback`

**File:** `trip_feedback.csv` (512,873 rows)
**Grain:** one row = one employee's feedback for one trip leg

The employee-experience view — how riders rated the route, driver, cab, safety,
and marshal for each leg (0–5). One row per rider per leg, so multiple rows can
share a `trip_id`. Join to trips on `trip_id` and to riders on `stwid`.

| Column | Type | Meaning | Values (observed) |
|---|---|---|---|
| `business_unit` | object | Client account / business unit the trip belongs to | 5 values: `orbit-Slc`, `vanta-Aus`, `catalyst-Sac`, `pinnacle-Slc`, `vanta-Sea` |
| `trip_id` | object | Trip being rated — join key to other files | 298,321 unique, comma-formatted, e.g. `"1,258,207"` |
| `trip_type` | object | Leg the feedback is for | `LOGIN`, `LOGOUT` |
| `trip_date` | object | Calendar date + time of the trip, free-text | e.g. `"June 3, 2026, 11:00 AM"`; 5,716 distinct values |
| `stwid` | object | Employee/rider giving the feedback | 13,258 unique, comma-formatted for larger IDs, e.g. `"149,530"` |
| `route_rating` | int64 | Rating for the route (0–5) | 0, 1, 2, 3, 4, 5 |
| `driver_rating` | int64 | Rating for the driver (0–5) | 0, 1, 2, 3, 4, 5 |
| `cab_rating` | int64 | Rating for the cab (0–5) | 0, 1, 2, 3, 4, 5 |
| `safety_rating` | int64 | Rating for trip safety (0–5) | 0, 1, 2, 3, 4, 5 |
| `marshal_rating` | int64 | Rating for the marshal/escort (0–5) | 0, 1, 2, 3, 4, 5 |
| `creation_time` | object | Timestamp the feedback was submitted | e.g. `"June 3, 2026, 10:44 AM"` |

**Heads-up for this file**

- `trip_id` and `stwid` are comma-formatted strings — strip commas before
  joining (see the [dataset guide](./README.md)).
- `trip_date` includes a time-of-day here, unlike `ride_data_trip`'s date-only
  format — parse accordingly.
- Ratings run 0–5; check whether `0` means a genuine low score or an unrated leg
  before you average.

**Ideas to explore:** driver / cab CSAT by vendor, safety-rating dips against an
alert spike, low-rating clustering by route or shift, an experience trend a
transport & facilities head could take to leadership.
