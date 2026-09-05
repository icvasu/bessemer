"""Load the MoveInSync CSVs into Postgres.

The dataset is deliberately messy. Everything this script cleans is listed in
the dataset's own README, and each fix is logged with a count so the rejects
are visible rather than silent:

  * trip_id, stwid, epochs, delay_minutes are comma-formatted strings in some
    files and clean numerics in others
  * dates arrive in four different formats depending on the file
  * planned_km / traveled_km go negative, which is physically impossible
  * alerts_data.severity carries a stray literal "False"
  * is_driver_nc / planned_km change dtype between the monthly ride files

Run:  uv run python -m db.load [--truncate]
"""

from __future__ import annotations

import argparse
import io
import sys
import time
from dataclasses import dataclass, field

import pandas as pd

from app.config import DATASET_DIR
from app.db import connect

RIDE_FILES = [
    "Ride_data _trip-may_2026.csv",
    "Ride_data _trip-June_2026.csv",
    "Ride_data _trip-July_2026.csv",
]
EMP_FILE = "emp_Data.csv"
ALERTS_FILE = "alerts_data.csv"


@dataclass
class LoadReport:
    """What happened to each table, printed at the end."""

    table: str
    read: int = 0
    loaded: int = 0
    notes: list[str] = field(default_factory=list)

    def note(self, msg: str) -> None:
        self.notes.append(msg)

    def __str__(self) -> str:
        head = f"  {self.table:<12} read {self.read:>9,}  loaded {self.loaded:>9,}"
        if not self.notes:
            return head
        return head + "\n" + "\n".join(f"      - {n}" for n in self.notes)


# --------------------------------------------------------------- normalisers


def strip_commas(s: pd.Series) -> pd.Series:
    """'1,097,076' -> 1097076. Works whether the column is str, float or int."""
    return pd.to_numeric(
        s.astype("string").str.replace(",", "", regex=False), errors="coerce"
    )


def epoch_to_ts(s: pd.Series) -> pd.Series:
    """Unix seconds (comma-formatted or float) -> naive timestamp."""
    return pd.to_datetime(strip_commas(s), unit="s", errors="coerce")


def parse_dt(s: pd.Series, fmt: str) -> pd.Series:
    """Parse one of the dataset's free-text date formats."""
    return pd.to_datetime(s, format=fmt, errors="coerce")


def clip_negative(s: pd.Series, report: LoadReport, label: str) -> pd.Series:
    """Null out physically impossible distances, and say how many."""
    numeric = pd.to_numeric(s, errors="coerce")
    bad = int((numeric < 0).sum())
    if bad:
        report.note(f"{bad:,} negative {label} values nulled")
    return numeric.where(numeric >= 0)


def copy_frame(conn, df: pd.DataFrame, table: str, columns: list[str]) -> int:
    """Stream a DataFrame into Postgres with COPY ... FROM STDIN (CSV)."""
    buf = io.StringIO()
    df[columns].to_csv(buf, index=False, header=False, na_rep="")
    buf.seek(0)
    cols = ", ".join(columns)
    with conn.cursor() as cur:
        with cur.copy(
            f"COPY {table} ({cols}) FROM STDIN WITH (FORMAT csv, NULL '')"
        ) as copy:
            while chunk := buf.read(1 << 20):
                copy.write(chunk)
        cur.execute(f"SELECT count(*) AS n FROM {table}")
        return cur.fetchone()["n"]


# ------------------------------------------------------------------- loaders


def load_trips(conn) -> LoadReport:
    report = LoadReport("trips")
    frames = []
    for name in RIDE_FILES:
        df = pd.read_csv(DATASET_DIR / name, low_memory=False)
        report.read += len(df)
        frames.append(df)
    df = pd.concat(frames, ignore_index=True)

    df["trip_id"] = strip_commas(df["trip_id"]).astype("Int64")
    df["delay_minutes"] = strip_commas(df["delay_minutes"]).astype("Int64")
    for col in (
        "planned_start_epoch",
        "planned_end_epoch",
        "actual_start_epoch",
        "actual_end_epoch",
    ):
        df[col] = epoch_to_ts(df[col])
    # "May 1, 2026"
    df["trip_date"] = parse_dt(df["trip_date"], "%B %d, %Y").dt.date

    before = len(df)
    df = df.dropna(subset=["trip_id"])
    # trip_id is the primary key; the three monthly files overlap slightly.
    df = df.drop_duplicates(subset=["trip_id"], keep="first")
    dropped = before - len(df)
    if dropped:
        report.note(f"{dropped:,} rows dropped (null or duplicate trip_id)")

    out = pd.DataFrame(
        {
            "trip_id": df["trip_id"],
            "business_unit": df["business_unit"],
            "office": df["office"],
            "product_type": df["product_type"],
            "trip_date": df["trip_date"],
            "shift_type": df["shift_type"],
            "trip_direction": df["trip_direction"],
            "vendor_id": df["vendor_id"],
            "planned_start": df["planned_start_epoch"],
            "planned_end": df["planned_end_epoch"],
            "actual_start": df["actual_start_epoch"],
            "actual_end": df["actual_end_epoch"],
            "delay_reason": df["delay_reason"],
            "delay_minutes": df["delay_minutes"],
            "trip_nodal": df["trip_nodal"],
            "planned_cnt": df["plannedemployee_cnt"],
            "actual_cnt": df["actualemployee_cnt"],
            "noshow_cnt": df["noshow_cnt"],
        }
    )
    report.loaded = copy_frame(conn, out, "trips", list(out.columns))
    return report


def load_rider_legs(conn) -> LoadReport:
    report = LoadReport("rider_legs")
    df = pd.read_csv(DATASET_DIR / EMP_FILE, low_memory=False)
    report.read = len(df)

    df["trip_id"] = strip_commas(df["trip_id"]).astype("Int64")
    df["stwid"] = strip_commas(df["stwid"]).astype("Int64")
    for col in (
        "planned_pickup_epoch",
        "planned_drop_epoch",
        "actual_pickup_epoch",
        "actual_drop_epoch",
    ):
        df[col] = epoch_to_ts(df[col])
    # ISO here, unlike every other file.
    df["trip_date"] = parse_dt(df["trip_date"], "%Y-%m-%d").dt.date
    df["planned_km"] = clip_negative(df["planned_km"], report, "planned_km")
    df["traveled_km"] = clip_negative(df["traveled_km"], report, "traveled_km")

    before = len(df)
    df = df.dropna(subset=["trip_id", "stwid"])
    if before - len(df):
        report.note(f"{before - len(df):,} rows dropped (null trip_id or stwid)")

    placeholder = int((df["stwid"] == 0).sum())
    if placeholder:
        report.note(f"{placeholder:,} rows carry stwid=0 (placeholder, filtered by view)")

    out = pd.DataFrame(
        {
            "trip_id": df["trip_id"],
            "stwid": df["stwid"],
            "business_unit": df["business_unit"],
            "office": df["office"],
            "trip_date": df["trip_date"],
            "shift_type": df["shift_type"],
            "planned_pickup": df["planned_pickup_epoch"],
            "planned_drop": df["planned_drop_epoch"],
            "actual_pickup": df["actual_pickup_epoch"],
            "actual_drop": df["actual_drop_epoch"],
            "planned_km": df["planned_km"],
            "traveled_km": df["traveled_km"],
            "signintype": df["signintype"],
            "gender": df["gender"],
            "emp_role": df["emp_role"],
            "boarding_status": df["boarding_status"],
            "not_boarding_reason": df["not_boarding_reason"],
            "is_no_show": df["is_no_show"],
        }
    )
    report.loaded = copy_frame(conn, out, "rider_legs", list(out.columns))
    return report


def load_trip_alerts(conn) -> LoadReport:
    report = LoadReport("trip_alerts")
    df = pd.read_csv(DATASET_DIR / ALERTS_FILE, low_memory=False)
    report.read = len(df)

    df["trip_id"] = strip_commas(df["trip_id"]).astype("Int64")
    df["stwid"] = strip_commas(df["stwid"]).astype("Int64")
    # "May 1, 2026, 12:03 AM"
    df["start_time"] = parse_dt(df["start_time"], "%B %d, %Y, %I:%M %p")
    df["acknowledge_time"] = parse_dt(df["acknowledge_time"], "%B %d, %Y, %I:%M %p")

    bad_sev = df["severity"].isin(["False", "True"])
    if int(bad_sev.sum()):
        report.note(f"{int(bad_sev.sum()):,} rows had a non-severity value in severity, nulled")
    df.loc[bad_sev, "severity"] = None

    before = len(df)
    df = df.drop_duplicates(subset=["event_id"], keep="first")
    if before - len(df):
        report.note(f"{before - len(df):,} duplicate event_id rows dropped")

    out = pd.DataFrame(
        {
            "event_id": df["event_id"],
            "business_unit": df["business_unit"],
            "trip_id": df["trip_id"],
            "stwid": df["stwid"],
            "event_type": df["event_type"],
            "start_time": df["start_time"],
            "ack_time": df["acknowledge_time"],
            "state_text": df["state_text"],
            "severity": df["severity"],
            "source": df["source"],
        }
    )
    report.loaded = copy_frame(conn, out, "trip_alerts", list(out.columns))
    return report


# ---------------------------------------------------------------------- main


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--truncate",
        action="store_true",
        help="empty the dataset tables first (they are append-only otherwise)",
    )
    args = parser.parse_args()

    if not DATASET_DIR.is_dir():
        print(f"dataset not found at {DATASET_DIR}", file=sys.stderr)
        return 1

    started = time.perf_counter()
    with connect() as conn:
        with conn.cursor() as cur:
            if args.truncate:
                cur.execute("TRUNCATE trips, rider_legs, trip_alerts")
                print("truncated trips, rider_legs, trip_alerts")
            cur.execute("SELECT count(*) AS n FROM trips")
            if cur.fetchone()["n"] and not args.truncate:
                print("trips already populated; pass --truncate to reload")
                return 1

        reports = [load_trips(conn), load_rider_legs(conn), load_trip_alerts(conn)]
        conn.commit()

    elapsed = time.perf_counter() - started
    print("\nload complete")
    for report in reports:
        print(report)
    print(f"\n  {elapsed:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
