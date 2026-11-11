"""Sequence allocation inside the same transaction as each new ledger record."""

from sqlite3 import Connection


def allocate(db: Connection, prefix: str) -> str:
    """Allocate the next ID under the caller's writer lock."""
    value = db.execute(
        """INSERT INTO counters(prefix, last) VALUES (?, 1)
           ON CONFLICT(prefix) DO UPDATE SET last = last + 1
           RETURNING last""",
        (prefix,),
    ).fetchone()[0]
    return f"{prefix}-{value:04d}"
