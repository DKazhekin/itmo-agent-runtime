"""Durable, conservative spending reservations in integer micro-USD."""

from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from pathlib import Path
import re
import sqlite3
import uuid


_MAX_MICRO = 2**63 - 1
_MAX_USD = Decimal("9223372036854.775807")


class BudgetError(ValueError):
    """Budget configuration, storage, or authorization prevents an operation."""


class _AmountOverflow(BudgetError):
    pass


def _usd_to_micro(value, *, exact=False):
    """Parse without binary arithmetic; observed costs round up, caps are exact."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise BudgetError("USD amount must be a finite nonnegative decimal")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, OverflowError):
        raise BudgetError("USD amount must be a finite nonnegative decimal") from None
    if not amount.is_finite() or amount < 0:
        raise BudgetError("USD amount must be a finite nonnegative decimal")
    if amount > _MAX_USD:
        raise _AmountOverflow("USD amount exceeds supported storage range")
    if not amount:
        return 0
    _, digits, exponent = amount.as_tuple()
    # This conversion is exact and independent of the caller's Decimal context.
    coefficient = 0
    for digit in digits:
        coefficient = coefficient * 10 + digit
    shift = exponent + 6
    if shift >= 0:
        return coefficient * 10**shift
    if -shift >= len(digits):
        whole, remainder = 0, coefficient
    else:
        whole, remainder = divmod(coefficient, 10**(-shift))
    if exact and remainder:
        raise BudgetError("budget limit must use whole micro-USD units")
    return whole + bool(remainder)


class BudgetLedger:
    """Keep reservations across crashes and never release an unknown charge.

    A budget ID has an immutable approved cap. Each operation uses its own
    SQLite connection; BEGIN IMMEDIATE serializes authorization across workers.
    No release API exists: only a valid provider-reported cost settles a hold.
    Servers use initialize=False; only explicit operator setup creates budgets.
    """

    def __init__(self, path, budget_id: str, limit_usd: str, *, initialize: bool = True):
        if type(initialize) is not bool:
            raise BudgetError("budget initialization must be explicitly true or false")
        if not isinstance(budget_id, str) or not budget_id.strip():
            raise BudgetError("budget ID must be explicitly configured")
        if not isinstance(limit_usd, str) or not limit_usd.strip():
            raise BudgetError("budget limit must be explicitly configured as decimal USD")
        self.limit_micro = _usd_to_micro(limit_usd, exact=True)
        self.budget_id = budget_id
        if not isinstance(path, (str, Path)) or not str(path).strip() or str(path) == ":memory:":
            raise BudgetError("budget storage must be a persistent file")
        self.path = str(Path(path).absolute())
        with self._transaction(create=initialize) as db:
            if initialize:
                db.execute("""CREATE TABLE IF NOT EXISTS budgets (
                    budget_id TEXT PRIMARY KEY,
                    limit_micro INTEGER NOT NULL CHECK (limit_micro >= 0)
                )""")
                db.execute("""CREATE TABLE IF NOT EXISTS reservations (
                    reservation_id TEXT PRIMARY KEY,
                    budget_id TEXT NOT NULL REFERENCES budgets(budget_id),
                    amount_micro INTEGER NOT NULL CHECK (amount_micro > 0),
                    charged_micro INTEGER CHECK (charged_micro >= 0),
                    status TEXT NOT NULL CHECK (status IN ('reserved', 'unknown', 'settled')),
                    violation_reason TEXT
                )""")
                db.execute("CREATE INDEX IF NOT EXISTS reservations_budget ON reservations(budget_id)")
                db.execute("""CREATE TABLE IF NOT EXISTS budget_blocks (
                    budget_id TEXT NOT NULL REFERENCES budgets(budget_id),
                    reason TEXT NOT NULL,
                    PRIMARY KEY (budget_id, reason)
                )""")
            row = db.execute("SELECT limit_micro FROM budgets WHERE budget_id = ?", (budget_id,)).fetchone()
            if row is None:
                if not initialize:
                    raise BudgetError("approved budget is missing from storage")
                db.execute("INSERT INTO budgets VALUES (?, ?)", (budget_id, self.limit_micro))
            elif row["limit_micro"] != self.limit_micro:
                raise BudgetError("approved limit cannot change for an existing budget ID")

    @contextmanager
    def _transaction(self, *, create=False):
        db = None
        try:
            # mode=rw fails atomically if storage disappeared; it cannot silently
            # replace a deleted ledger with a fresh file during normal operation.
            uri = Path(self.path).as_uri() + ("?mode=rwc" if create else "?mode=rw")
            db = sqlite3.connect(uri, uri=True, timeout=30, isolation_level=None)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys = ON")
            db.execute("PRAGMA synchronous = FULL")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except sqlite3.Error:
            if db is not None:
                db.rollback()
            raise BudgetError("budget storage is unavailable") from None
        except BaseException:
            if db is not None:
                db.rollback()
            raise
        finally:
            if db is not None:
                db.close()

    @staticmethod
    def _record(row):
        return {
            "reservation_id": row["reservation_id"],
            "budget_id": row["budget_id"],
            "status": row["status"],
            "amount_micro": row["amount_micro"],
            "reserved_micro": row["amount_micro"] if row["charged_micro"] is None else 0,
            "charged_micro": row["charged_micro"],
            "violation": row["violation_reason"] is not None,
            "violation_reason": row["violation_reason"],
        }

    def _snapshot(self, db):
        budget = db.execute("SELECT limit_micro FROM budgets WHERE budget_id = ?", (self.budget_id,)).fetchone()
        if budget is None:
            raise BudgetError("approved budget is missing from storage")
        limit_micro = budget["limit_micro"]
        rows = db.execute("SELECT * FROM reservations WHERE budget_id = ?", (self.budget_id,)).fetchall()
        # Python sums remain exact even if unexpected provider charges exceed int64 in aggregate.
        reserved = sum(row["amount_micro"] for row in rows if row["charged_micro"] is None)
        charged = sum(row["charged_micro"] for row in rows if row["charged_micro"] is not None)
        violations = [self._record(row) for row in rows if row["violation_reason"] is not None]
        blocks = db.execute("SELECT reason FROM budget_blocks WHERE budget_id = ? ORDER BY reason",
                            (self.budget_id,)).fetchall()
        violations.extend({"reservation_id": None, "violation": True, "violation_reason": row["reason"]}
                          for row in blocks)
        return {
            "budget_id": self.budget_id,
            "limit_micro": limit_micro,
            "reserved_micro": reserved,
            "charged_micro": charged,
            "available_micro": max(0, limit_micro - reserved - charged),
            "blocked": bool(violations),
            "violations": violations,
            "unknown_count": sum(row["status"] == "unknown" for row in rows),
        }

    def snapshot(self):
        """Return accounting state without request contents or provider credentials."""
        with self._transaction() as db:
            return self._snapshot(db)

    def block(self, reason: str):
        """Permanently block authorization after an external contract violation.

        Reasons are safe constant identifiers, never provider bodies or errors.
        Blocking changes no recorded charge or outstanding reservation.
        """
        if not isinstance(reason, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", reason):
            raise BudgetError("budget block reason must be a safe constant identifier")
        with self._transaction() as db:
            db.execute("INSERT OR IGNORE INTO budget_blocks (budget_id, reason) VALUES (?, ?)",
                       (self.budget_id, reason))
            return self._snapshot(db)

    def reserve(self, amount_micro: int) -> str:
        """Durably authorize a positive upper bound before any paid request."""
        if type(amount_micro) is not int or not 0 < amount_micro <= _MAX_MICRO:
            raise BudgetError("reservation must be a positive integer micro-USD amount")
        with self._transaction() as db:
            state = self._snapshot(db)
            if state["blocked"]:
                raise BudgetError("budget is blocked by a recorded violation")
            if amount_micro > state["available_micro"]:
                raise BudgetError("approved budget is exhausted")
            reservation_id = uuid.uuid4().hex
            db.execute("""INSERT INTO reservations
                (reservation_id, budget_id, amount_micro, status) VALUES (?, ?, ?, 'reserved')""",
                (reservation_id, self.budget_id, amount_micro))
            return reservation_id

    def settle(self, reservation_id: str, cost_usd=None):
        """Record a cost, or retain the full hold if its outcome is unknown.

        Identical settlements are idempotent. A later trustworthy cost may settle
        an unknown hold; conflicting costs for an already settled hold fail.
        A cost exceeding its reservation permanently blocks this budget ID.
        """
        if not isinstance(reservation_id, str) or not reservation_id:
            raise BudgetError("reservation ID is required")
        overflow = False
        try:
            charged = _usd_to_micro(cost_usd)
        except _AmountOverflow:
            charged, overflow = None, True
        except BudgetError:
            charged = None
        with self._transaction() as db:
            row = db.execute("SELECT * FROM reservations WHERE reservation_id = ? AND budget_id = ?",
                             (reservation_id, self.budget_id)).fetchone()
            if row is None:
                raise BudgetError("reservation does not belong to this budget")
            if row["charged_micro"] is not None:
                if (charged is not None and charged != row["charged_micro"]) or overflow:
                    raise BudgetError("reservation is already settled with a different cost")
                return self._record(row)
            reason = row["violation_reason"]
            if overflow:
                reason = "provider_cost_exceeds_storage_range"
            elif charged is not None and charged > row["amount_micro"]:
                reason = "provider_cost_exceeded_reservation"
            db.execute("""UPDATE reservations SET charged_micro = ?, status = ?, violation_reason = ?
                WHERE reservation_id = ? AND budget_id = ?""",
                (charged, "unknown" if charged is None else "settled", reason, reservation_id, self.budget_id))
            row = db.execute("SELECT * FROM reservations WHERE reservation_id = ? AND budget_id = ?",
                             (reservation_id, self.budget_id)).fetchone()
            return self._record(row)
