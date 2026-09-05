# Sample inputs and outputs

Section 10 D4. Captured on the demo morning: **pinnacle-Slc / Clearwater Campus / 09:00 / 2026-06-11**.

Every figure below is computed. The model only wrote the narrative, the drafts, and the chat reply. Full JSON lives next to this file; these are the pairs a judge can run or paste.

Reproduce:

```bash
uv run uvicorn app.api:app --port 8000
```

Default scope on every call: `business_unit=pinnacle-Slc`, `office=Clearwater Campus`, `date=2026-06-11`, `shift=09:00`.

---

## Form paste (one pair)

Use this if the portal has two boxes.

**Sample input**

```
POST /replay/start?to=08:55
GET  /board
GET  /alerts
POST /alerts/{billing_id}/act?pathway=EARLY_SHIFT_COVER
POST /chat   {"text":"Who can cover the queues that are breaching SLA?"}
```

**Sample output**

```
{"status":"positioned","clock":"2026-06-11T08:55:00"}

board @ 08:55
  24 rostered, 3 on floor, 21 at risk, coverage 67%
  Billing Support:        8/12 projected, SL 15% (target 80/20), 4 short until 09:19
  Technical Support:      8/12 projected, SL 22% (target 80/20), 4 short until 09:29
  day SLA still holds at 92% on both queues  —  do not escalate the day

Billing narrative
  Billing Support is four agents short until 09:19. Service level is 15% now,
  while the day's service level is 92% and the 80% target still holds.
  Recommend early-shift cover from Agent 27, Agent 29, Agent 35, and Agent 31.
  If nobody acts, 4 night agents are held 19 min past shift end, 4 miss the
  09:15 cab home, overtime 799.

act EARLY_SHIFT_COVER
  {"status":"recorded","pathway":"EARLY_SHIFT_COVER",
   "draft":"Billing Support is 67% staffed for the 09:00 start. Could Agent 27,
            Agent 29, Agent 35, Agent 31 move onto the queue until the 09:00
            team is in? Asking at 08:55.",
   "people":["Agent 27","Agent 29","Agent 35","Agent 31"]}

chat
  Call Agent 27 (Billing, on floor since 08:16) and Agent 28 (Tech Support,
  on floor since 08:24). They are the top same-queue cover for the two
  queues currently breaching SLA.
```

---

## 1. Sense — jump the clock

**Input**

```http
POST /replay/start?to=08:55
```

**Output** (`samples/api_board_0855.json`)

```http
HTTP 200
{"status":"positioned","clock":"2026-06-11T08:55:00"}
```

Then `GET /board` returns the floor as of that minute. Nothing downstream can see past 08:55.

```json
{
  "clock": "2026-06-11T08:55:00",
  "time": "08:55",
  "office": "Clearwater Campus",
  "business_unit": "pinnacle-Slc",
  "shift_date": "2026-06-11",
  "shift_type": "09:00",
  "totals": {
    "rostered": 24,
    "on_floor": 3,
    "at_risk": 21,
    "projected": 16,
    "coverage_pct": 67,
    "queues_breaching_sla": 2,
    "day_sla_at_risk": 0
  },
  "queues": [
    {
      "display_name": "Billing Support",
      "rostered": 12,
      "on_floor": 1,
      "projected": 8,
      "coverage_pct": 67,
      "impact": {
        "agents_missing": 4,
        "calls_range": "5 to 21",
        "recovered_by": "2026-06-11T09:19:01",
        "service_level": { "service_level_pct": 15, "target_pct": 80, "headline": "15% answered in 20s against a 80% target" },
        "day": { "day_service_level_pct": 92, "meets_target": true }
      }
    },
    {
      "display_name": "Technical Support",
      "rostered": 12,
      "on_floor": 2,
      "projected": 8,
      "coverage_pct": 67,
      "impact": {
        "agents_missing": 4,
        "recovered_by": "2026-06-11T09:29:23",
        "service_level": { "service_level_pct": 22, "target_pct": 80, "headline": "22% answered in 20s against a 80% target" },
        "day": { "day_service_level_pct": 92, "meets_target": true }
      }
    }
  ]
}
```

The same morning as a text clock: `samples/replay_2026-06-11.txt`.

```
08:55  bill:  1/12  67%  SL  15%! | tech:  2/12  67%  SL  22%!
09:19  bill: 10/12  83%  SL  71%! | tech:  8/12  67%  SL  22%!
       ! Billing Support: back to 83% strength
09:55  bill: 12/12 100%  SL  92%  | tech: 12/12 100%  SL  92%
       ! Technical Support: back to 100% strength
```

---

## 2. Reason — the alert the manager sees

**Input**

```http
GET /alerts
```

**Output** (`samples/api_alerts_narrated.json`, Billing row, trimmed)

```json
{
  "queue_name": "Billing Support",
  "status": "OPEN",
  "cause": "EN_ROUTE_DELAY",
  "coverage_pct": 67,
  "opened_at": "2026-06-11T08:25:00",
  "triggers": [
    "coverage 67% below the 75% floor",
    "1 unaccounted for after their cab passed"
  ],
  "hold_over": {
    "agents_held": 4,
    "minutes": 76,
    "cost": 799,
    "summary": "4 night agents held 19 min past shift end, 4 would miss the 09:15 cab home"
  },
  "context": [
    "tracking towards 5 late, about the usual 5 for this team",
    "Clearwater Campus runs 46% late on the 09:00 shift, worse than the 29% across 4 peer sites"
  ],
  "options": [
    { "pathway": "HOLD_OVER",          "label": "Keep the night shift on",              "recommended": false, "cost": 799 },
    { "pathway": "EARLY_SHIFT_COVER",  "label": "Move the early shift onto the queue",  "recommended": true,  "people": ["Agent 27", "Agent 29", "Agent 35", "Agent 31"] },
    { "pathway": "CONTACT_EMPLOYEE",   "label": "Call the unaccounted riders",          "recommended": false, "people": ["Agent 15"] },
    { "pathway": "ESCALATE_TRANSPORT", "label": "Escalate to transport",                "recommended": false }
  ],
  "narrative": "Billing Support is four agents short until 09:19. Service level is 15% now, while the day's service level is 92% and the 80% target still holds. The team is tracking towards 5 late, about the usual 5 for this team. Use early-shift cover from Agent 27, Agent 29, Agent 35, and Agent 31 so the night shift goes home on time. If nobody acts, 4 night agents are held 19 min past shift end, 4 would miss the 09:15 cab home, and overtime costs 799.",
  "drafts": {
    "EARLY_SHIFT_COVER": "Early-shift lead, please put Agent 27, Agent 29, Agent 35, and Agent 31 on Billing Support by 09:00.",
    "ESCALATE_TRANSPORT": "Transport manager, please review the 2 affected riders using Karan Mikhailov Travel before 09:15."
  }
}
```

Technical Support opened earlier (08:01), same shape: SL 22%, recover by 09:29, recommended cover Agent 28 / 36 / 38 / 40, hold-over 1234.

---

## 3. Act — click the green option

**Input**

```http
POST /alerts/{billing_id}/act?pathway=EARLY_SHIFT_COVER
```

**Output** (`samples/api_action.json`)

```json
{
  "status": "recorded",
  "pathway": "EARLY_SHIFT_COVER",
  "draft": "Billing Support is 67% staffed for the 09:00 start. Could Agent 27, Agent 29, Agent 35, Agent 31 move onto the queue until the 09:00 team is in? Asking at 08:55.",
  "people": [
    { "name": "Agent 27", "arrived_at": "2026-06-11T08:16:46", "note": "same queue, on floor since 08:16, no cover yet this week" },
    { "name": "Agent 29", "arrived_at": "2026-06-11T08:20:19", "note": "same queue, on floor since 08:20, no cover yet this week" },
    { "name": "Agent 35", "arrived_at": "2026-06-11T08:20:30", "note": "same queue, on floor since 08:20, no cover yet this week" },
    { "name": "Agent 31", "arrived_at": "2026-06-11T08:21:05", "note": "same queue, on floor since 08:21, no cover yet this week" }
  ],
  "sent_at": "2026-06-11T08:55:00"
}
```

The draft is what the manager forwards. The row is the audit trail; cover minutes are charged so the next alert does not pick the same four.

---

## 4. Ask — chat against the same board

**Input**

```http
POST /chat
Content-Type: application/json

{"text": "Who can cover the queues that are breaching SLA?"}
```

**Output** (`samples/api_chat.json`)

```json
{
  "clock": "2026-06-11T08:55:00",
  "reply": "Call **Agent 27** and **Agent 28** now:\n\n- **Agent 27**: Billing Support, on the floor since **08:16**, with **no cover this week**. Billing is at **15% service level against an 80% target**.\n- **Agent 28**: Technical Support, on the floor since **08:24**, with **no cover this week**. Technical Support is at **22% against an 80% target**.\n\nThey are the top available same-queue cover candidates for the two queues currently breaching SLA.",
  "usage": {
    "model": "openai/gpt-5.6-luna",
    "calls": 3,
    "prompt_tokens": 19521,
    "completion_tokens": 1595,
    "total_tokens": 21116,
    "seconds": 26.9,
    "by_reason": { "compose:OPEN": 2, "chat": 1 }
  }
}
```

`GET /usage` returns the same meter (`samples/usage.json`). Three model calls for the morning: two narrations, one question. The clock itself never calls a model.

---

## What is input vs what is output

| Input (real dataset) | Output (this system) |
|---|---|
| Rider legs for 11 Jun 2026, Clearwater, 09:00 | Rider state + ETA band at `now` |
| Planned / actual pickup and drop | Queue headcount, coverage, Erlang C service level |
| Roster of 24 riders + 24 cover (real stwids) | Open / update / resolve alerts with scored options |
| Clock `now` (replay or live) | Narrative + sendable drafts; act writes `alert_actions` |

The raw CSVs are the organiser dataset. We never invent a name or a time; tools only hand the model what the core already computed.
