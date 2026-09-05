"""Seed the queues and roster.

Riders are real: every stwid below comes from the dataset, chosen because the
person actually rode the shift on at least MIN_RIDE_DAYS of the 64 weekdays in
May to July 2026. What is synthetic is only the org structure the dataset does
not carry:

  * which queue a rider serves        (roster.queue)
  * handle time and call forecast     (queues.aht_min, calls_per_30min)
  * manager names                     (queues.*_manager / *_lead)

Three groups go into the roster:

  primary  the 09:00 day shift, the team we watch. Real riders.
  cover    the 08:30 shift at the same site. Real riders, and by 09:00 their
           own `actual_drop` proves they are already on the floor.
  night    the 01:00-09:00 shift the day team relieves. Wholly synthetic and
           flagged as such, because Clearwater's trip log has no outbound legs
           near 09:00 and therefore no night shift to read. See `night_shift`
           below for why we assert one anyway.

Run:  uv run python -m db.seed_roster [--reset]
"""

from __future__ import annotations

import argparse

from app.config import (
    BUSINESS_UNIT,
    COVER_POOL_SIZE,
    COVER_SHIFT_TYPE,
    MIN_RIDE_DAYS,
    NIGHT_POOL_SIZE,
    NIGHT_SHIFT_ENDS,
    NIGHT_SHIFT_TYPE,
    OFFICE,
    SHIFT_TYPE,
    TEAM_SIZE,
)

from app.db import connect

# Synthetic stwids for the night shift, kept in a reserved band well clear of
# the dataset's real range (max observed: 800,995) so nothing can collide and
# any row in this range is instantly recognisable as fabricated.
SYNTHETIC_STWID_BASE = 9_000_000

# Forecast volumes are synthetic, but not arbitrary. They are chosen so a full
# roster of twelve sits near 90% service level against an 80/20 target, with
# roughly one agent of headroom. That is what a workforce team would actually
# staff: comfortable, not lavish.
#
# The choice matters more than it looks. An earlier draft used a lighter
# forecast that left the queue at 50% occupancy, where losing a third of the
# roster barely moved the service level, and the whole staffing argument fell
# flat. A heavier one put the queue over capacity at full strength, which is
# worse: it would have reported a breach every morning regardless of anyone's
# commute.
QUEUES = [
    {
        "queue": "billing",
        "display_name": "Billing Support",
        "aht_min": 5.0,
        "calls_per_30min": 46,
        "sl_target": "80/20",
    },
    {
        "queue": "techsupport",
        "display_name": "Technical Support",
        "aht_min": 7.0,
        "calls_per_30min": 32,
        "sl_target": "80/20",
    },
]

MANAGERS = {
    "line_manager": "Priya Raghavan",
    "early_shift_lead": "Daniel Osei",
    "transport_manager": "Meera Krishnan",
}

REGULARS_SQL = """
SELECT stwid, COUNT(DISTINCT trip_date) AS days
FROM v_login_legs
WHERE business_unit = %(bu)s
  AND office = %(office)s
  AND shift_type = %(shift)s
GROUP BY stwid
HAVING COUNT(DISTINCT trip_date) >= %(min_days)s
ORDER BY days DESC, stwid
LIMIT %(limit)s
"""


def pick_regulars(conn, shift: str, limit: int) -> list[int]:
    """The most consistent riders on a shift. Deterministic, so reruns match."""
    with conn.cursor() as cur:
        cur.execute(
            REGULARS_SQL,
            {
                "bu": BUSINESS_UNIT,
                "office": OFFICE,
                "shift": shift,
                "min_days": MIN_RIDE_DAYS,
                "limit": limit,
            },
        )
        return [row["stwid"] for row in cur.fetchall()]


def assign(
    stwids: list[int],
    role: str,
    shift: str,
    start_index: int,
    prefix: str = "Agent",
    synthetic: bool = False,
    shift_ends: str | None = None,
) -> list[dict]:
    """Alternate riders between the two queues so both get a full complement."""
    rows = []
    for i, stwid in enumerate(stwids):
        queue = QUEUES[i % len(QUEUES)]["queue"]
        rows.append(
            {
                "stwid": stwid,
                "display_name": f"{prefix} {start_index + i + 1:02d}",
                "business_unit": BUSINESS_UNIT,
                "office": OFFICE,
                "shift_type": shift,
                "queue": queue,
                "role": role,
                "synthetic": synthetic,
                "shift_ends": shift_ends,
            }
        )
    return rows


def night_shift() -> list[dict]:
    """The outgoing night shift.

    Wholly synthetic, and flagged as such in the roster. Clearwater's real trip
    log has no outbound legs anywhere near 09:00, so there is no night shift to
    read from the data. We assert one because a 24/7 desk is what makes a late
    arrival cost something beyond a thin queue: under positional handover, the
    night agent stays until their relief is seated.

    These people need no commute record. They have been at their desks since
    01:00, so their presence is a given rather than something to project. The
    scarce resource here is not their availability but their willingness to
    stay past eight hours.
    """
    ids = [SYNTHETIC_STWID_BASE + i for i in range(NIGHT_POOL_SIZE)]
    return assign(
        ids,
        role="night",
        shift=NIGHT_SHIFT_TYPE,
        start_index=0,
        prefix="Night",
        synthetic=True,
        shift_ends=NIGHT_SHIFT_ENDS,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reset", action="store_true", help="clear queues and roster first")
    args = parser.parse_args()

    with connect() as conn:
        if args.reset:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM roster")
                cur.execute("DELETE FROM queues")

        with conn.cursor() as cur:
            for q in QUEUES:
                cur.execute(
                    """
                    INSERT INTO queues (queue, display_name, business_unit, office,
                                        shift_type, aht_min, calls_per_30min, sl_target,
                                        line_manager, early_shift_lead, transport_manager)
                    VALUES (%(queue)s, %(display_name)s, %(bu)s, %(office)s,
                            %(shift)s, %(aht_min)s, %(calls_per_30min)s, %(sl_target)s,
                            %(line_manager)s, %(early_shift_lead)s, %(transport_manager)s)
                    ON CONFLICT (queue) DO UPDATE SET
                        display_name = EXCLUDED.display_name,
                        aht_min = EXCLUDED.aht_min,
                        calls_per_30min = EXCLUDED.calls_per_30min
                    """,
                    {**q, "bu": BUSINESS_UNIT, "office": OFFICE, "shift": SHIFT_TYPE, **MANAGERS},
                )

        primary = pick_regulars(conn, SHIFT_TYPE, TEAM_SIZE)
        cover = pick_regulars(conn, COVER_SHIFT_TYPE, COVER_POOL_SIZE)

        if len(primary) < TEAM_SIZE:
            print(f"warning: only {len(primary)} regulars on the {SHIFT_TYPE} shift")
        if len(cover) < COVER_POOL_SIZE:
            print(f"warning: only {len(cover)} regulars on the {COVER_SHIFT_TYPE} shift")

        # A rider could in principle ride both shifts on different days; the
        # primary roster wins so nobody covers for themselves.
        cover = [s for s in cover if s not in set(primary)][:COVER_POOL_SIZE]

        rows = assign(primary, "primary", SHIFT_TYPE, 0)
        rows += assign(cover, "cover", COVER_SHIFT_TYPE, len(primary))
        rows += night_shift()

        with conn.cursor() as cur:
            for row in rows:
                cur.execute(
                    """
                    INSERT INTO roster (stwid, display_name, business_unit, office,
                                        shift_type, queue, role, synthetic, shift_ends)
                    VALUES (%(stwid)s, %(display_name)s, %(business_unit)s, %(office)s,
                            %(shift_type)s, %(queue)s, %(role)s, %(synthetic)s,
                            %(shift_ends)s::time)
                    ON CONFLICT (stwid) DO UPDATE SET
                        display_name = EXCLUDED.display_name,
                        shift_type = EXCLUDED.shift_type,
                        queue = EXCLUDED.queue,
                        role = EXCLUDED.role,
                        synthetic = EXCLUDED.synthetic,
                        shift_ends = EXCLUDED.shift_ends
                    """,
                    row,
                )
        conn.commit()

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT role, queue, COUNT(*) AS n
                FROM roster GROUP BY 1, 2 ORDER BY 1 DESC, 2
                """
            )
            print(f"seeded {len(QUEUES)} queues and {len(rows)} roster rows")
            for r in cur.fetchall():
                print(f"  {r['role']:<8} {r['queue']:<12} {r['n']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
