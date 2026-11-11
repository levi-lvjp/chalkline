import importlib.util
import shutil
from pathlib import Path


def test_demo_seed_uses_real_core_and_resets_only_plugin_data(tmp_path, monkeypatch):
    workspace = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location("demo_seed", Path(__file__).resolve().parents[1] / "scripts" / "demo_seed.py")
    demo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(demo)
    monkeypatch.setattr(demo, "WORKSPACE", tmp_path)
    profile = tmp_path / ".runtime" / "profiles" / "chalkline-dev"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text("{}", encoding="utf-8")
    fixtures = tmp_path / "plugin" / "fixtures"
    fixtures.mkdir(parents=True)
    shutil.copyfile(Path(__file__).resolve().parents[1] / "fixtures" / "receipt.png", fixtures / "receipt.png")
    other = profile / "plugin-data" / "other-plugin" / "keep.txt"
    other.parent.mkdir(parents=True)
    other.write_text("keep", encoding="utf-8")
    monkeypatch.setenv("CHALKLINE_TEST_NOW", demo.DEFAULT_CLOCK)
    monkeypatch.setenv("HERMES_HOME", str(profile))
    summary = demo.seed(reset=True, profile_name='chalkline-dev')
    assert summary["jobs"] == 12 and summary["expenses"] == 5
    assert summary["pending_approvals"] == 3 and summary["duplicate_receipt_deduped"]
    assert all(summary["states"][state] for state in demo.STATES)
    assert demo.seed(profile_name='chalkline-dev') == summary
    assert demo.seed(reset=True, profile_name='chalkline-dev') == summary
    assert other.read_text() == "keep"
    from chalkline.core import queries
    details = [queries.job_detail(row["id"]) for row in queries.jobs()]
    assert sum(len(row["appointments"]) for row in details) == 2
    assert any(len(row["quotes"]) > 1 for row in details)
    extras = [extra["status"] for row in details for extra in row["variations"]]
    assert sorted(extras) == ["agreed", "proposed"]
    overdue = queries.money_owed()
    assert any(row["due_at"] == "2026-11-02" for row in overdue)
    assert {row["kind"] for row in queries.pending_approvals()} == {"quote", "payment_chaser", "confirm_expense"}
