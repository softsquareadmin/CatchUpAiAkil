import json

import pytest

from app.audit import AuditLog
from app.ledger import BudgetExceeded, Ledger, LedgerRow, compute_cost, load_prices
from tests.fakes import FAKE_PRICES


def make_ledger(tmp_path, cap=1.0):
    return Ledger(tmp_path, "s1", "exp-a", cap, prices=FAKE_PRICES)


def test_compute_cost_tokens_and_audio():
    assert compute_cost(FAKE_PRICES, "fake", "fake", 1_000_000, 500_000) == pytest.approx(2.0)
    assert compute_cost(FAKE_PRICES, "fake", "fake", audio_seconds=3600) == pytest.approx(0.36)


def test_unknown_model_price_raises():
    with pytest.raises(KeyError, match="nope:x"):
        compute_cost(FAKE_PRICES, "nope", "x", 1)


def test_shipped_prices_have_as_of_and_source():
    for key, entry in load_prices().items():
        if key.startswith("_"):
            continue
        assert entry["as_of"] and entry["source"].startswith("https://")


def test_row_written_with_schema_and_label(tmp_path):
    led = make_ledger(tmp_path)
    led.record("gate", "fake", "fake", input_tokens=1000, output_tokens=100, latency_ms=42)
    led.record("cue", "fake", "fake", provider_cost_usd=0.5)
    rows = [LedgerRow.model_validate_json(l) for l in (tmp_path / "ledger.jsonl").read_text().splitlines()]
    assert len(rows) == 2
    assert rows[0].experiment == "exp-a" and rows[0].cost_source == "computed"
    assert rows[0].cost_usd == pytest.approx(0.0012)
    assert rows[1].cost_source == "provider" and rows[1].cost_usd == 0.5
    assert str(tmp_path) not in (tmp_path / "ledger.jsonl").read_text()  # no absolute paths in rows


def test_budget_cap_stops_paid_calls(tmp_path):
    led = make_ledger(tmp_path, cap=0.10)
    led.check_budget()
    led.record("gate", "fake", "fake", provider_cost_usd=0.11)
    with pytest.raises(BudgetExceeded, match="passed the cap"):
        led.check_budget()


def test_audit_row(tmp_path):
    AuditLog(tmp_path, "s1").write("worker", "consent_noted", by="parent")
    row = json.loads((tmp_path / "audit.jsonl").read_text())
    assert row["actor"] == "worker" and row["action"] == "consent_noted"
    assert row["details"] == {"by": "parent"} and row["session_id"] == "s1"
