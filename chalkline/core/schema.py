"""Versioned schema for the profile-local Chalkline database."""

from contextlib import contextmanager, closing
from sqlite3 import Connection, Row
from typing import Iterator

from plugins.plugin_storage import plugin_db

from .. import NAME, SLUG


MIGRATIONS = ("""
CREATE TABLE settings (id INTEGER PRIMARY KEY CHECK (id = 1), data TEXT NOT NULL);
CREATE TABLE counters (prefix TEXT PRIMARY KEY, last INTEGER NOT NULL);
CREATE TABLE customers (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, phone TEXT UNIQUE,
    address TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT ''
);
CREATE TABLE jobs (
    id INTEGER PRIMARY KEY, number TEXT NOT NULL UNIQUE,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    site_address TEXT NOT NULL, description TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN (
        'enquiry','quoted','accepted','scheduled','done','invoiced','paid','declined','cancelled'
    )),
    completion_lines TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX jobs_customer_state ON jobs(customer_id, state);
CREATE TABLE inbound_messages (
    message_id TEXT PRIMARY KEY, job_id INTEGER NOT NULL REFERENCES jobs(id)
);
CREATE TABLE sources (
    id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL REFERENCES jobs(id),
    kind TEXT NOT NULL CHECK (kind IN ('text','photo','voice','receipt','document')),
    sender TEXT NOT NULL, gateway_message_id TEXT UNIQUE,
    file_path TEXT, sha256 TEXT, text TEXT NOT NULL, created_at TEXT NOT NULL,
    UNIQUE(job_id, sha256)
);
CREATE TABLE quotes (
    id INTEGER PRIMARY KEY, number TEXT NOT NULL,
    job_id INTEGER NOT NULL REFERENCES jobs(id), revision INTEGER NOT NULL,
    lines TEXT NOT NULL, currency TEXT NOT NULL,
    subtotal_minor INTEGER NOT NULL, tax_rate TEXT NOT NULL,
    tax_minor INTEGER NOT NULL, total_minor INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        'draft','awaiting_approval','sent','accepted','declined','superseded','expired'
    )),
    valid_until TEXT NOT NULL, created_at TEXT NOT NULL, issued_at TEXT,
    document TEXT NOT NULL, UNIQUE(number, revision), UNIQUE(job_id, revision)
);
CREATE TABLE variations (
    id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL REFERENCES jobs(id),
    description TEXT NOT NULL, amount_minor INTEGER NOT NULL CHECK (amount_minor > 0),
    status TEXT NOT NULL CHECK (status IN ('proposed','agreed','declined')),
    source_id INTEGER REFERENCES sources(id), created_at TEXT NOT NULL,
    CHECK (status = 'proposed' OR source_id IS NOT NULL)
);
CREATE TABLE appointments (
    id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL REFERENCES jobs(id),
    start TEXT NOT NULL, end TEXT NOT NULL, created_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('scheduled','cancelled','completed')),
    CHECK (start < end)
);
CREATE UNIQUE INDEX active_appointment ON appointments(job_id) WHERE status = 'scheduled';
CREATE TABLE invoices (
    id INTEGER PRIMARY KEY, number TEXT NOT NULL UNIQUE,
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    quote_id INTEGER REFERENCES quotes(id),
    lines TEXT NOT NULL, currency TEXT NOT NULL,
    subtotal_minor INTEGER NOT NULL, tax_rate TEXT NOT NULL,
    tax_minor INTEGER NOT NULL, total_minor INTEGER NOT NULL,
    created_at TEXT NOT NULL, issued_at TEXT, due_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('draft','awaiting_approval','sent','paid','on_hold','void')),
    delivery_status TEXT NOT NULL DEFAULT 'not_sent'
        CHECK (delivery_status IN ('not_sent','sent','failed','unknown')),
    document TEXT NOT NULL
);
CREATE UNIQUE INDEX active_invoice ON invoices(job_id) WHERE status <> 'void';
CREATE TABLE payments (
    id INTEGER PRIMARY KEY, invoice_id INTEGER NOT NULL REFERENCES invoices(id),
    amount_minor INTEGER NOT NULL CHECK (amount_minor > 0),
    date TEXT NOT NULL, method TEXT NOT NULL, reference TEXT NOT NULL,
    recorded_by TEXT NOT NULL, created_at TEXT NOT NULL, request_key TEXT UNIQUE
);
CREATE TABLE expenses (
    id INTEGER PRIMARY KEY, date TEXT NOT NULL, vendor TEXT NOT NULL, vendor_key TEXT NOT NULL,
    amount_minor INTEGER NOT NULL CHECK (amount_minor > 0), currency TEXT NOT NULL,
    category TEXT NOT NULL, job_id INTEGER REFERENCES jobs(id), source_id INTEGER REFERENCES sources(id),
    receipt_path TEXT NOT NULL, receipt_sha256 TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK (status IN ('proposed','confirmed')),
    confidence TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX similar_expenses ON expenses(vendor_key, date, amount_minor);
CREATE TABLE approvals (
    id INTEGER PRIMARY KEY, code TEXT NOT NULL UNIQUE, action TEXT NOT NULL,
    payload TEXT NOT NULL, payload_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL, expires_at TEXT NOT NULL, used_at TEXT,
    approved_by TEXT, status TEXT NOT NULL CHECK (status IN ('pending','approved','rejected','invalidated'))
);
CREATE TABLE outbox (
    id INTEGER PRIMARY KEY, kind TEXT NOT NULL, recipient TEXT NOT NULL, body TEXT NOT NULL,
    attachment_path TEXT, attachment_sha256 TEXT, payload_sha256 TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        'draft','awaiting_approval','approved','sending','sent','failed','unknown','cancelled'
    )),
    approval_id INTEGER REFERENCES approvals(id), stage_key TEXT NOT NULL UNIQUE,
    job_id INTEGER REFERENCES jobs(id), related_id INTEGER, standing_hash TEXT,
    created_at TEXT NOT NULL, claimed_at TEXT, finished_at TEXT, detail TEXT
);
CREATE INDEX ack_window ON outbox(kind, recipient, created_at);
CREATE TABLE events (
    id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, kind TEXT NOT NULL,
    entity TEXT NOT NULL, entity_id INTEGER, actor TEXT NOT NULL, data TEXT NOT NULL
);
CREATE TRIGGER events_no_update BEFORE UPDATE ON events BEGIN
    SELECT RAISE(ABORT, 'events are append-only');
END;
CREATE TRIGGER events_no_delete BEFORE DELETE ON events BEGIN
    SELECT RAISE(ABORT, 'events are append-only');
END;
CREATE TRIGGER quote_payload_immutable BEFORE UPDATE OF number, job_id, revision, lines, currency,
    subtotal_minor, tax_rate, tax_minor, total_minor, valid_until, document ON quotes BEGIN
    SELECT RAISE(ABORT, 'create a quote revision instead');
END;
CREATE TRIGGER invoice_payload_immutable BEFORE UPDATE OF number, job_id, quote_id, lines, currency,
    subtotal_minor, tax_rate, tax_minor, total_minor, due_at, document ON invoices BEGIN
    SELECT RAISE(ABORT, 'replace the invoice draft instead');
END;
""", """
ALTER TABLE customers ADD COLUMN gateway_sender TEXT;
CREATE UNIQUE INDEX customer_gateway_sender ON customers(gateway_sender) WHERE gateway_sender IS NOT NULL;
""",)


def migrate(db: Connection) -> None:
    """Apply ordered migrations under a single writer lock."""
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version == len(MIGRATIONS):
        return
    with db:
        db.execute("BEGIN IMMEDIATE")
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version > len(MIGRATIONS):
            raise RuntimeError(f"{NAME} database was created by a newer plugin version.")
        for index, migration in enumerate(MIGRATIONS[version:], version + 1):
            # executescript commits an open transaction, so execute complete statements individually.
            import sqlite3

            statement = ""
            for line in migration.splitlines(keepends=True):
                statement += line
                if sqlite3.complete_statement(statement):
                    db.execute(statement)
                    statement = ""
            db.execute(f"PRAGMA user_version = {index}")


@contextmanager
def connect(*, write: bool = False) -> Iterator[Connection]:
    """Open the active profile database and start a coherent read or write transaction."""
    with closing(plugin_db(SLUG)) as db:
        db.row_factory = Row
        migrate(db)
        with db:
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
