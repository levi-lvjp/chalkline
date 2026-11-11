from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, localcontext
from pathlib import Path
import sqlite3
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from chalkline.core import money
from chalkline.core.numbering import allocate
from chalkline.core.records import event
from chalkline.core.schema import MIGRATIONS, connect
from chalkline.core.states import transition


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "profiles" / "chalkline-dev"
    home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def test_minor_units_and_flat_rate():
    assert money.minor("0.105", "GBP") == 11
    assert money.minor("55.5", "JPY") == 56
    assert money.minor("1.2345", "KWD") == 1235
    assert money.major(23, "GBP") == "0.23"
    assert money.line_total(Decimal("0.3"), 15) == 5
    assert money.totals([{"total_minor": 5}, {"total_minor": 10}], Decimal("10")) == {
        "subtotal_minor": 15, "tax_minor": 2, "total_minor": 17,
    }
    with pytest.raises(ValueError):
        money.minor("NaN", "GBP")
    with localcontext() as ctx:
        ctx.prec = 2
        assert money.minor("1000000.005", "GBP") == 100000001
        assert money.major(100000001, "GBP") == "1000000.01"


def test_concurrent_numbering_and_restart(home):
    def issue(_):
        with connect(write=True) as db:
            number = allocate(db, "INV")
            event(db, "allocated", "invoice", None, number=number)
            return number

    with ThreadPoolExecutor(max_workers=8) as pool:
        numbers = list(pool.map(issue, range(32)))
    assert sorted(numbers) == [f"INV-{i:04d}" for i in range(1, 33)]
    assert issue(0) == "INV-0033"
    with connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
        assert db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 33


def test_invalid_transition_and_append_only_events(home):
    with connect(write=True) as db:
        db.execute("INSERT INTO customers(id,name) VALUES (1,'Fictional Customer')")
        db.execute(
            """INSERT INTO jobs VALUES (1,'J-0001',1,'','Fictional repair','enquiry',NULL,'today','today')"""
        )
        with pytest.raises(ValueError, match="Cannot move"):
            transition(db, "jobs", 1, "paid")
        transition(db, "jobs", 1, "quoted")
        event(db, "quoted", "job", 1)
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            db.execute("DELETE FROM events")
    with connect() as db:
        assert db.execute("SELECT state FROM jobs").fetchone()[0] == "quoted"
