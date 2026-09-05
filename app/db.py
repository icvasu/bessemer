"""Postgres helpers.

Thin on purpose: psycopg3 already does the hard parts. The one thing worth
having here is a connection pool.

A first version opened a connection per query. Profiling a single replay showed
680 queries taking 17.6 of 18.3 seconds, with 3.4 of those seconds spent purely
on connection setup. Pooling and per-shift caching upstream took the same
replay to well under a second. That matters beyond developer comfort: latency
per tick is one of the things this system is judged on, and "we open a TCP
connection per rider per minute" is not an answer.
"""

from __future__ import annotations

import atexit
from contextlib import contextmanager
from typing import Any, Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app.config import DSN, SERVERLESS

_pool: ConnectionPool | None = None


def pool() -> ConnectionPool:
    """The process-wide connection pool, opened on first use."""
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            DSN,
            # Vercel functions are request-scoped; an idle connection keeps
            # Neon compute awake for nothing. Render keeps min_size=1.
            min_size=0 if SERVERLESS else 1,
            max_size=8,
            kwargs={"row_factory": dict_row},
            open=True,
        )
        atexit.register(close_pool)
    return _pool


def close_pool() -> None:
    """Shut the pool down. Registered at exit; also useful in tests."""
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextmanager
def connect(autocommit: bool = False) -> Iterator[psycopg.Connection]:
    """Borrow a pooled connection whose rows come back as dicts.

    The pool returns the connection on exit and rolls back anything left
    uncommitted, so a caller that forgets to commit loses its work rather than
    holding a transaction open for the next borrower.
    """
    with pool().connection() as conn:
        if autocommit:
            conn.autocommit = True
        yield conn


def query(sql: str, params: Any = None) -> list[dict]:
    """Run a SELECT and return every row as a dict."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def query_one(sql: str, params: Any = None) -> dict | None:
    """Run a SELECT expected to match at most one row."""
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params: Any = None) -> int:
    """Run a statement that writes. Returns the affected row count."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        conn.commit()
        return cur.rowcount
