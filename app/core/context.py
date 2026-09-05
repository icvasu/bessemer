"""Benchmarking: what turns a number into a judgement.

The brief's central complaint is that "a metric without context is just a
number". Nine agents late means nothing on its own. Nine against a typical
five, on the weekday that is reliably the worst, when the plan only ever
allowed five minutes of slack, is a situation a manager can act on.

Four reference points, all computed from the same three months of history the
rest of the system reads:

1. **This team's own normal.** The strongest comparison available, because it
   controls for site, shift and roster in one move.
2. **Weekday.** Lateness at this site swings from 32% on Friday to 53% on
   Tuesday. Without it, a bad Tuesday looks like a crisis and a bad Friday
   looks routine, when the truth is the reverse.
3. **Peer sites on the same shift.** The nearest thing the dataset offers to
   an industry benchmark, and the one that tells a manager whether the problem
   is theirs or the operator's.
4. **Structural slack.** The plan leaves a median five minutes between the
   planned drop and shift start, against roughly thirteen minutes of journey
   noise. Half of all arrivals are late by construction. This is the fact that
   reframes the whole conversation: no amount of daily escalation fixes a
   schedule built inside its own error bar.

Every fact carries the sample it was computed from, so nothing is quoted
without the evidence attached.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from typing import Any

from app.db import query


@dataclass(frozen=True)
class Fact:
    """One benchmark, phrased for a human and carrying its own provenance."""

    key: str
    text: str
    value: float | None = None
    reference: float | None = None
    sample: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "text": self.text,
            "value": self.value,
            "reference": self.reference,
            "sample": self.sample,
        }


def _pct(x: float) -> str:
    return f"{round(x * 100)}%"


@lru_cache(maxsize=32)
def team_baseline(office: str, shift_type: str, queue: str | None = None) -> dict[str, float]:
    """How many of this team are normally late, and how often anyone is absent.

    Restricted to the rostered team rather than everyone on the shift, so the
    comparison is like for like: these are the same people, on the same
    commute, on ordinary days.
    """
    row = query(
        """
        SELECT
            COUNT(DISTINCT trip_date)                                         AS days,
            COUNT(*)                                                          AS legs,
            AVG(CASE WHEN late_for_shift_min > 5 THEN 1.0 ELSE 0.0 END)       AS late_share,
            AVG(CASE WHEN boarding_status = 'Not Boarded' THEN 1.0 ELSE 0.0 END) AS absent_share
        FROM v_roster_day
        WHERE office = %(office)s
          AND rostered_shift = %(shift)s
          AND role = 'primary'
          AND (%(queue)s::text IS NULL OR queue = %(queue)s::text)
        """,
        {"office": office, "shift": shift_type, "queue": queue},
    )[0]
    days = row["days"] or 1
    legs = row["legs"] or 0
    late_share = float(row["late_share"] or 0)
    return {
        "days": float(days),
        "legs": float(legs),
        "late_share": late_share,
        "absent_share": float(row["absent_share"] or 0),
        "typical_late_per_day": late_share * legs / days,
        "team_size": legs / days,
    }


@lru_cache(maxsize=32)
def weekday_baseline(office: str, shift_type: str) -> dict[str, dict[str, float]]:
    """Late share by weekday for this site and shift."""
    rows = query(
        """
        SELECT dow, days, legs, late_share
        FROM v_shift_baseline
        WHERE office = %s AND shift_type = %s
        """,
        (office, shift_type),
    )
    return {
        r["dow"]: {
            "days": float(r["days"]),
            "legs": float(r["legs"]),
            "late_share": float(r["late_share"] or 0),
        }
        for r in rows
    }


@lru_cache(maxsize=32)
def peer_baseline(office: str, shift_type: str) -> list[dict[str, Any]]:
    """The same shift at other sites, busiest first. The peer comparison."""
    rows = query(
        """
        SELECT office,
               SUM(legs)                                        AS legs,
               SUM(late_share * legs) / NULLIF(SUM(legs), 0)    AS late_share
        FROM v_shift_baseline
        WHERE shift_type = %s
        GROUP BY office
        HAVING SUM(legs) > 500
        ORDER BY legs DESC
        """,
        (shift_type,),
    )
    return [
        {"office": r["office"], "legs": int(r["legs"]), "late_share": float(r["late_share"] or 0)}
        for r in rows
    ]


@lru_cache(maxsize=32)
def structural_slack(office: str, shift_type: str) -> dict[str, float]:
    """Median minutes the plan leaves between the planned drop and shift start."""
    row = query(
        """
        SELECT
            COUNT(*)                                                          AS legs,
            PERCENTILE_CONT(0.5) WITHIN GROUP (
                ORDER BY EXTRACT(EPOCH FROM (shift_start - planned_drop)) / 60.0) AS median_slack
        FROM v_roster_day
        WHERE office = %s AND rostered_shift = %s AND role = 'primary'
          AND planned_drop IS NOT NULL
        """,
        (office, shift_type),
    )[0]
    return {
        "legs": float(row["legs"] or 0),
        "median_slack_min": float(row["median_slack"] or 0),
    }


@lru_cache(maxsize=64)
def same_weekday_history(
    office: str, shift_type: str, on: date, queue: str | None = None, weeks: int = 4
) -> dict[str, float]:
    """How the last few same-weekdays went, so today has a near neighbour."""
    row = query(
        """
        SELECT COUNT(DISTINCT trip_date) AS days,
               AVG(CASE WHEN late_for_shift_min > 5 THEN 1.0 ELSE 0.0 END) AS late_share
        FROM v_roster_day
        WHERE office = %(office)s
          AND rostered_shift = %(shift)s
          AND role = 'primary'
          AND (%(queue)s::text IS NULL OR queue = %(queue)s::text)
          AND trip_date < %(on)s
          AND trip_date >= %(on)s::date - (%(weeks)s * 7)
          AND EXTRACT(DOW FROM trip_date) = EXTRACT(DOW FROM %(on)s::date)
        """,
        {"office": office, "shift": shift_type, "queue": queue, "on": on, "weeks": weeks},
    )[0]
    return {"days": float(row["days"] or 0), "late_share": float(row["late_share"] or 0)}


def context_facts(
    office: str,
    shift_type: str,
    on: date,
    queue: str | None = None,
    late_today: int | None = None,
) -> list[Fact]:
    """Assemble the benchmarks worth putting in front of a manager.

    Ordered by how much they change the reading of the morning. The narrative
    layer takes the first one or two; the board can show them all.
    """
    facts: list[Fact] = []
    dow = on.strftime("%a")

    team = team_baseline(office, shift_type, queue)
    if late_today is not None and team["days"] >= 5:
        typical = team["typical_late_per_day"]
        verdict = "worse than" if late_today > typical + 1 else (
            "better than" if late_today < typical - 1 else "about"
        )
        # Careful with the wording. This counts riders who have already arrived
        # late plus those currently heading that way, so mid-morning it is a
        # running total rather than a final score. Calling it "tracking towards"
        # keeps it from reading as a settled number next to a coverage figure
        # that is still moving.
        facts.append(
            Fact(
                key="team_typical",
                text=(
                    f"tracking towards {late_today} late, {verdict} the usual "
                    f"{typical:.0f} for this team"
                ),
                value=float(late_today),
                reference=round(typical, 1),
                sample=f"{int(team['days'])} days of history",
            )
        )

    weekdays = weekday_baseline(office, shift_type)
    today = weekdays.get(dow)
    if today and len(weekdays) > 1:
        best = min(weekdays.items(), key=lambda kv: kv[1]["late_share"])
        if best[0] != dow:
            facts.append(
                Fact(
                    key="weekday",
                    text=(
                        f"{dow} runs {_pct(today['late_share'])} late at this site "
                        f"against {_pct(best[1]['late_share'])} on {best[0]}"
                    ),
                    value=round(today["late_share"], 3),
                    reference=round(best[1]["late_share"], 3),
                    sample=f"{int(today['days'])} {dow}s",
                )
            )

    recent = same_weekday_history(office, shift_type, on, queue)
    if recent["days"] >= 2:
        facts.append(
            Fact(
                key="recent_weekday",
                text=(
                    f"the last {int(recent['days'])} {dow}s ran "
                    f"{_pct(recent['late_share'])} late for this team"
                ),
                value=round(recent["late_share"], 3),
                sample=f"{int(recent['days'])} recent {dow}s",
            )
        )

    peers = peer_baseline(office, shift_type)
    mine = next((p for p in peers if p["office"] == office), None)
    others = [p for p in peers if p["office"] != office]
    if mine and others:
        pooled = sum(p["late_share"] * p["legs"] for p in others) / sum(p["legs"] for p in others)
        direction = "worse" if mine["late_share"] > pooled else "better"
        facts.append(
            Fact(
                key="peer",
                text=(
                    f"{office} runs {_pct(mine['late_share'])} late on the {shift_type} "
                    f"shift, {direction} than the {_pct(pooled)} across "
                    f"{len(others)} peer sites"
                ),
                value=round(mine["late_share"], 3),
                reference=round(pooled, 3),
                sample=f"{len(others)} sites",
            )
        )

    slack = structural_slack(office, shift_type)
    if slack["legs"] > 100:
        facts.append(
            Fact(
                key="structural_slack",
                text=(
                    f"the plan leaves a median {slack['median_slack_min']:.0f} min "
                    f"between drop and shift start, inside the ~13 min spread of "
                    f"journey times, so late arrivals are partly designed in"
                ),
                value=round(slack["median_slack_min"], 1),
                reference=13.0,
                sample=f"{int(slack['legs'])} legs",
            )
        )

    return facts


def clear_cache() -> None:
    """Drop cached baselines. Needed after a reload, mainly in tests."""
    team_baseline.cache_clear()
    weekday_baseline.cache_clear()
    peer_baseline.cache_clear()
    structural_slack.cache_clear()
    same_weekday_history.cache_clear()
