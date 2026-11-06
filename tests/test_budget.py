"""Exercise durable authorization without contacting any model provider."""

from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from decimal import Decimal, localcontext
import multiprocessing
import sqlite3
import threading

import pytest

from runtime.budget import BudgetError, BudgetLedger


def _process_reservations(path):
    ledger = BudgetLedger(path, "parallel", "0.000010")
    approved = []
    for _ in range(10):
        try:
            approved.append(ledger.reserve(1))
        except BudgetError as exc:
            assert str(exc) == "approved budget is exhausted"
    return approved


@pytest.fixture
def ledger(tmp_path):
    return BudgetLedger(tmp_path / "budget.sqlite", "approved-run", "1")


def test_reserved_before_call_and_survives_restart(tmp_path):
    path = tmp_path / "budget.sqlite"
    first = BudgetLedger(path, "run", "1")
    reservation = first.reserve(700_000)
    restarted = BudgetLedger(path, "run", "1.000000")
    assert restarted.snapshot()["reserved_micro"] == 700_000
    with pytest.raises(BudgetError, match="exhausted"):
        restarted.reserve(300_001)
    assert restarted.settle(reservation, "0.3")["charged_micro"] == 300_000
    restarted.reserve(700_000)
    state = BudgetLedger(path, "run", "1").snapshot()
    assert state["charged_micro"] + state["reserved_micro"] == state["limit_micro"]
    assert state["available_micro"] == 0


@pytest.mark.parametrize("cost", [None, "", "not-a-price", -1, "-0.000001", True, False,
                                  float("nan"), float("inf"), "-Infinity", Decimal("sNaN"), {}, []])
def test_unknown_cost_never_refunds_reservation(tmp_path, cost):
    path = tmp_path / "budget.sqlite"
    ledger = BudgetLedger(path, "run", "1")
    reservation = ledger.reserve(1_000_000)
    result = ledger.settle(reservation, cost)
    assert result["status"] == "unknown"
    assert result["charged_micro"] is None
    assert result["reserved_micro"] == 1_000_000
    restarted = BudgetLedger(path, "run", "1")
    assert restarted.snapshot()["unknown_count"] == 1
    with pytest.raises(BudgetError, match="exhausted"):
        restarted.reserve(1)


def test_explicit_zero_cost_is_distinct_from_unknown(ledger):
    reservation = ledger.reserve(1_000_000)
    ledger.settle(reservation)
    result = ledger.settle(reservation, 0)
    assert result["status"] == "settled"
    assert result["charged_micro"] == 0
    assert ledger.snapshot()["available_micro"] == 1_000_000


def test_cost_rounds_up_without_decimal_context_or_float_error(ledger):
    first, second = ledger.reserve(500_000), ledger.reserve(500_000)
    with localcontext() as ctx:
        ctx.prec = 2
        assert ledger.settle(first, Decimal("0.1234560000001"))["charged_micro"] == 123_457
        assert ledger.settle(second, 0.1)["charged_micro"] == 100_000
    assert ledger.snapshot()["charged_micro"] == 223_457


def test_tiny_nonzero_cost_does_not_round_to_zero(ledger):
    reservation = ledger.reserve(1)
    assert ledger.settle(reservation, "1e-1000000")["charged_micro"] == 1


def test_duplicate_settlement_does_not_double_charge_or_refund(ledger):
    reservation = ledger.reserve(1_000_000)
    expected = ledger.settle(reservation, "0.25")
    assert ledger.settle(reservation, "0.250000") == expected
    assert ledger.settle(reservation, None) == expected
    with pytest.raises(BudgetError, match="different cost"):
        ledger.settle(reservation, "0.01")
    assert ledger.snapshot()["charged_micro"] == 250_000


def test_overspend_is_persisted_and_blocks_even_when_total_is_below_cap(tmp_path):
    path = tmp_path / "budget.sqlite"
    ledger = BudgetLedger(path, "run", "1")
    reservation = ledger.reserve(100_000)
    result = ledger.settle(reservation, "0.10000001")
    assert result["violation"] is True
    assert result["charged_micro"] == 100_001
    restarted = BudgetLedger(path, "run", "1")
    state = restarted.snapshot()
    assert state["blocked"] is True
    assert state["charged_micro"] == 100_001
    assert state["violations"] == [result]
    with pytest.raises(BudgetError, match="violation"):
        restarted.reserve(1)


def test_overspend_above_entire_budget_does_not_claim_cap_recovery(ledger):
    reservation = ledger.reserve(1_000_000)
    ledger.settle(reservation, "1.2")
    state = ledger.snapshot()
    assert state["charged_micro"] == 1_200_000
    assert state["reserved_micro"] == 0
    assert state["available_micro"] == 0
    assert state["blocked"] is True


def test_unrepresentable_positive_charge_keeps_hold_and_blocks(ledger):
    reservation = ledger.reserve(100_000)
    result = ledger.settle(reservation, "1e100")
    assert result["status"] == "unknown"
    assert result["reserved_micro"] == 100_000
    assert result["charged_micro"] is None
    assert result["violation_reason"] == "provider_cost_exceeds_storage_range"
    ledger.settle(reservation, "0.1")
    with pytest.raises(BudgetError, match="violation"):
        ledger.reserve(1)


@pytest.mark.parametrize("limit", ["0.5", "2"])
def test_existing_budget_cap_cannot_be_changed(tmp_path, limit):
    path = tmp_path / "budget.sqlite"
    ledger = BudgetLedger(path, "run", "1")
    ledger.reserve(900_000)
    with pytest.raises(BudgetError, match="cannot change"):
        BudgetLedger(path, "run", limit)
    assert BudgetLedger(path, "run", "1").snapshot()["reserved_micro"] == 900_000


def test_budget_ids_have_separate_approvals_and_cannot_settle_each_other(tmp_path):
    path = tmp_path / "budget.sqlite"
    first = BudgetLedger(path, "first", "1")
    second = BudgetLedger(path, "second", "2")
    reservation = first.reserve(1_000_000)
    with pytest.raises(BudgetError, match="does not belong"):
        second.settle(reservation, 0)
    assert first.snapshot()["reserved_micro"] == 1_000_000
    assert second.snapshot()["reserved_micro"] == 0
    second.reserve(2_000_000)
    assert first.snapshot()["limit_micro"] == 1_000_000
    assert second.snapshot()["limit_micro"] == 2_000_000


def test_parallel_processes_cannot_overauthorize(tmp_path):
    path = str(tmp_path / "budget.sqlite")
    with ProcessPoolExecutor(max_workers=4, mp_context=multiprocessing.get_context("spawn")) as pool:
        batches = list(pool.map(_process_reservations, [path] * 4))
    approved = [reservation for batch in batches for reservation in batch]
    assert len(approved) == len(set(approved)) == 10
    ledger = BudgetLedger(path, "parallel", "0.000010")
    assert ledger.snapshot()["reserved_micro"] == 10
    assert ledger.snapshot()["charged_micro"] == 0


def test_simultaneous_settlement_is_idempotent(ledger):
    reservation = ledger.reserve(900_000)
    barrier = threading.Barrier(8)

    def settle_once(_):
        barrier.wait(timeout=10)
        return ledger.settle(reservation, "0.3")

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(settle_once, range(8)))
    assert all(outcome == outcomes[0] for outcome in outcomes)
    assert ledger.snapshot()["charged_micro"] == 300_000
    assert ledger.snapshot()["reserved_micro"] == 0


@pytest.mark.parametrize("limit", ["", " ", "NaN", "Infinity", "-1", "0.0000001", "1e100", None, 1, True])
def test_invalid_caps_fail_closed(tmp_path, limit):
    with pytest.raises(BudgetError):
        BudgetLedger(tmp_path / "budget.sqlite", "run", limit)


@pytest.mark.parametrize("budget_id", [None, "", "  ", True])
def test_explicit_budget_id_required(tmp_path, budget_id):
    with pytest.raises(BudgetError):
        BudgetLedger(tmp_path / "budget.sqlite", budget_id, "1")


@pytest.mark.parametrize("amount", [0, -1, True, False, 1.1, "1", None, 2**63])
def test_invalid_reservations_never_mutate_budget(ledger, amount):
    with pytest.raises(BudgetError):
        ledger.reserve(amount)
    assert ledger.snapshot()["reserved_micro"] == 0


def test_zero_cap_authorizes_no_requests(tmp_path):
    ledger = BudgetLedger(tmp_path / "budget.sqlite", "run", "0")
    with pytest.raises(BudgetError, match="exhausted"):
        ledger.reserve(1)


@pytest.mark.parametrize("path", ["", " ", ":memory:"])
def test_nonpersistent_storage_rejected(path):
    with pytest.raises(BudgetError, match="persistent"):
        BudgetLedger(path, "run", "1")


def test_storage_error_is_sanitized(tmp_path):
    path = tmp_path / "private-token-value" / "missing.sqlite"
    with pytest.raises(BudgetError, match="^budget storage is unavailable$"):
        BudgetLedger(path, "run", "1")


def test_stored_amounts_are_sqlite_integers(tmp_path):
    path = tmp_path / "budget.sqlite"
    ledger = BudgetLedger(path, "run", "1")
    ledger.settle(ledger.reserve(1_000_000), "0.1234567")
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT typeof(limit_micro) FROM budgets").fetchone() == ("integer",)
        assert db.execute("SELECT typeof(amount_micro), typeof(charged_micro) FROM reservations").fetchone() == (
            "integer", "integer")


def test_external_contract_block_survives_restart_without_changing_money(tmp_path):
    path = tmp_path / "budget.sqlite"
    ledger = BudgetLedger(path, "run", "1")
    reservation = ledger.reserve(500_000)
    ledger.settle(reservation, "0.1")
    remaining = ledger.reserve(200_000)
    state = ledger.block("model_identity_mismatch")
    assert ledger.block("model_identity_mismatch") == state
    restarted = BudgetLedger(path, "run", "1")
    assert restarted.snapshot() == state
    assert state["charged_micro"] == 100_000
    assert state["reserved_micro"] == 200_000
    assert state["blocked"] is True
    assert state["violations"][0]["violation_reason"] == "model_identity_mismatch"
    restarted.settle(remaining, "0.1")
    with pytest.raises(BudgetError, match="violation"):
        restarted.reserve(1)


def test_existing_only_mode_does_not_create_missing_file(tmp_path):
    path = tmp_path / "budget.sqlite"
    with pytest.raises(BudgetError, match="storage is unavailable"):
        BudgetLedger(path, "approved-run", "1", initialize=False)
    assert not path.exists()
    assert list(tmp_path.iterdir()) == []


def test_existing_only_mode_does_not_register_missing_budget(tmp_path):
    path = tmp_path / "budget.sqlite"
    approved = BudgetLedger(path, "approved-run", "1")
    approved.reserve(500_000)
    before = approved.snapshot()
    with pytest.raises(BudgetError, match="approved budget is missing"):
        BudgetLedger(path, "unapproved-run", "1", initialize=False)
    assert approved.snapshot() == before
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT budget_id FROM budgets").fetchall() == [("approved-run",)]


def test_existing_only_mode_reads_and_uses_approved_budget(tmp_path):
    path = tmp_path / "budget #1?.sqlite"
    approved = BudgetLedger(path, "approved-run", "1")
    reservation = approved.reserve(500_000)
    reopened = BudgetLedger(path, "approved-run", "1", initialize=False)
    assert reopened.snapshot() == approved.snapshot()
    reopened.settle(reservation, "0.2")
    reopened.reserve(800_000)
    assert reopened.snapshot()["available_micro"] == 0


def test_deleted_storage_is_not_recreated_by_existing_only_mode(tmp_path):
    path = tmp_path / "budget.sqlite"
    approved = BudgetLedger(path, "approved-run", "1")
    approved.reserve(1_000_000)
    path.unlink()
    with pytest.raises(BudgetError, match="storage is unavailable"):
        BudgetLedger(path, "approved-run", "1", initialize=False)
    with pytest.raises(BudgetError, match="storage is unavailable"):
        approved.reserve(1)
    assert not path.exists()
    assert list(tmp_path.iterdir()) == []


def test_existing_only_mode_does_not_initialize_empty_database(tmp_path):
    path = tmp_path / "budget.sqlite"
    path.touch()
    with pytest.raises(BudgetError, match="storage is unavailable"):
        BudgetLedger(path, "approved-run", "1", initialize=False)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT name FROM sqlite_master").fetchall() == []
