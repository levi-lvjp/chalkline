"""Allowed lifecycle transitions for ledger records."""

from sqlite3 import Connection


TRANSITIONS = {
    "jobs": {
        "enquiry": {"quoted", "done", "declined", "cancelled"},
        "quoted": {"accepted", "declined", "cancelled"},
        "accepted": {"scheduled", "done", "cancelled"},
        "scheduled": {"done", "cancelled"},
        "done": {"invoiced"},
        "invoiced": {"paid"},
        "paid": set(),
        "declined": set(),
        "cancelled": set(),
    },
    "quotes": {
        "draft": {"awaiting_approval", "superseded"},
        "awaiting_approval": {"sent", "superseded", "draft", "expired"},
        "sent": {"accepted", "declined", "superseded", "expired"},
        "accepted": set(), "declined": set(), "superseded": set(), "expired": set(),
    },
    "invoices": {
        "draft": {"awaiting_approval", "void"},
        "awaiting_approval": {"sent", "draft", "void"},
        "sent": {"paid", "on_hold", "void"},
        "on_hold": {"paid", "sent", "void"},
        "paid": set(), "void": set(),
    },
    "variations": {"proposed": {"agreed", "declined"}, "agreed": set(), "declined": set()},
    "appointments": {"scheduled": {"cancelled", "completed"}, "cancelled": set(), "completed": set()},
    "expenses": {"proposed": {"confirmed"}, "confirmed": set()},
}


def transition(
    db: Connection, entity: str, record_id: int, target: str, **fields: str | int | None,
) -> None:
    """Change an existing record only along an allowed edge in its lifecycle."""
    graph = TRANSITIONS[entity]
    column = "state" if entity == "jobs" else "status"
    row = db.execute(f"SELECT {column} FROM {entity} WHERE id = ?", (record_id,)).fetchone()
    if row is None:
        raise ValueError(f"Unknown {entity} ID {record_id}.")
    if target not in graph[row[column]]:
        raise ValueError(f"Cannot move {entity} from {row[column]} to {target}.")
    assignments = ", ".join(f"{key} = ?" for key in (column, *fields))
    db.execute(f"UPDATE {entity} SET {assignments} WHERE id = ?", (target, *fields.values(), record_id))
