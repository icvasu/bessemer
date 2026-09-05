# Data Dictionary — `alerts_data`

**File:** `alerts_data.csv` (51,699 rows)
**Grain:** one row = one alert/event raised during a trip

Safety and compliance events raised while trips run — SOS/panic presses,
geofence violations, over-speeding, device drop-offs, and more. Join to trips on
`trip_id` and to riders on `stwid`. Great raw material for anomaly detection,
proactive alerting, and safety dashboards.

| Column | Type | Meaning | Values (observed) |
|---|---|---|---|
| `business_unit` | object | Client account / business unit the alert belongs to | 5 values: `vanta-Aus`, `catalyst-Sac`, `orbit-Slc`, `vanta-Sea`, `pinnacle-Slc` |
| `trip_id` | object | Trip the alert relates to — join key to other files | 33,474 unique, comma-formatted, e.g. `"1,097,076"` |
| `stwid` | object | Employee/rider the alert relates to; `"0"` is a placeholder for trip-level alerts not tied to a specific employee | 9,314 unique, e.g. `"0"` (placeholder, most frequent) |
| `event_id` | object | Unique identifier for the alert/event | 51,699 unique UUIDs, e.g. `"37ceae1c-7fe7-4081-a96e-da66602024a7"` |
| `event_type` | object | Category of alert raised (e.g. route deviation, SOS) | 11 values: `DEVICE_NOT_REACHABLE`, `VEHICLE_STOPPAGE`, `WOMAN_TRAVELLING_ALONE`, `PANIC_FIXED_DEVICE`, `EMPLOYEE_GEOFENCE_VIOLATION`, `PANIC_DEVICE`, `PANIC_MOBILE`, `OVER_SPEEDING`, `FIRST_MALE_NO_SHOW`, `EMPLOYEE_SIGN_OFF_TIME_VIOLATION`, `SUPPLEMENTARY_ALERT` |
| `start_time` | object | Timestamp the alert was raised | e.g. `"May 1, 2026, 12:03 AM"` |
| `acknowledge_time` | object | Timestamp the alert was acknowledged; null if unacknowledged | e.g. `"May 1, 2026, 12:10 AM"`; 54 nulls |
| `state_text` | object | Current status text of the alert | `CLOSED`, `OPEN`, `NEW` |
| `severity` | object | Severity level (`Sev-1`/`Sev-2`/`Sev-3`) | `Sev-3`, `Sev-2`, `Sev-1`; 16,348 nulls. ⚠️ also contains a stray literal `"False"` — clean it out |
| `source` | object | System/source that raised the alert | `MOBILE`, `EXTERNAL_DEVICE`, `DEVICE`, `MOBILE_APP`; 39,350 nulls |

**Heads-up for this file**

- `trip_id` and `stwid` are comma-formatted strings — strip commas before
  joining (see the normalisation rule in the [dataset guide](./README.md)).
- `stwid = "0"` means the alert is trip-level, not tied to a real rider — filter
  it out for per-employee analysis.
- `severity` carries a stray `"False"` value plus ~16k nulls, and `source` is
  null on most rows. Both are expected messiness — handle gracefully.

**Ideas to explore:** alert-rate anomalies by vendor / office,
acknowledgement-time SLAs, clustering of safety events by shift or route,
proactive escalation when Sev-1 volume spikes.
