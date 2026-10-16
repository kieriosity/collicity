"""Invariant 4 (BUD-10): no limit is exceeded under concurrency, and balances always reconcile.

Three suites. One forces the race that the row locks in ``ledger.reserve`` exist for; one
stresses shared budgets from many threads; the stateful one drives every operation in random
orders against a model and checks the database agrees after each step.
"""

import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import timedelta
from uuid import UUID

from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, invariant, rule
from sqlalchemy import Connection, text

from collicity_core import TenantContext
from collicity_db import Role, make_engine, tenant_connection
from collicity_ledger import ledger
from collicity_ledger.ledger import ReserveStatus, SettleStatus, StartStatus
from collicity_ledger.pricing import PriceList
from collicity_testing.ledger import (
    Balance,
    balances,
    journal_matches_balances,
    ledger_scenario,
    make_due,
)
from collicity_testing.postgres import MigratedDatabase

TTL = timedelta(minutes=15)
SETTLE = timedelta(hours=1)


def test_a_reservation_rechecks_headroom_after_waiting_for_a_concurrent_hold(
    database: MigratedDatabase, prices: PriceList
):
    """Forces the race the row locks in ``ledger.reserve`` close.

    Another reservation (A, played here by migrator) has locked the organization's account and
    is about to add its hold. B arrives in that moment. B must wait for A, then see A's hold and
    be refused; reading the balance from before A's hold would grant both and overspend.
    """
    s = ledger_scenario(database, org=100_000, user=10**9, workspace=10**9)
    ctx = TenantContext(s.tenant.id, s.person)
    engine = make_engine(database.urls[Role.APP_WORKER], pool_size=1, max_overflow=0)

    def reserve(conn: Connection, amount: int) -> ledger.ReserveResult:
        return ledger.reserve(
            conn,
            principal_id=s.person,
            amount_micro_usd=amount,
            price_version_id=prices.version_id,
            purpose="race",
            ttl=TTL,
        )

    # Create this period's accounts, so the race is on an existing row.
    with tenant_connection(engine, ctx) as conn:
        ledger.release(conn, reserve(conn, 1).reservation_id)

    b_pid: list[int] = []
    b_result: list[ledger.ReserveResult] = []

    def b() -> None:
        with tenant_connection(engine, ctx) as conn:
            b_pid.append(conn.execute(text("SELECT pg_backend_pid()")).scalar_one())
            b_result.append(reserve(conn, 60_000))

    try:
        with database.engine(Role.MIGRATOR).begin() as a:
            a.execute(
                text("SELECT 1 FROM ledger_account WHERE budget_id = :b FOR UPDATE"),
                {"b": s.org_budget},
            )
            thread = threading.Thread(target=b)
            thread.start()
            _wait_until_blocked_on_a_lock(database, b_pid)
            a.execute(
                text(
                    "UPDATE ledger_account SET reserved_micro_usd = reserved_micro_usd + 60000 "
                    "WHERE budget_id = :b"
                ),
                {"b": s.org_budget},
            )
        thread.join(timeout=30)
    finally:
        engine.dispose()

    assert b_result[0].status is ReserveStatus.INSUFFICIENT
    assert b_result[0].blocking_budget_ids == (s.org_budget,)
    assert balances(database, [s.org_budget])[s.org_budget].reserved == 60_000


def _wait_until_blocked_on_a_lock(database: MigratedDatabase, pid: list[int]) -> None:
    deadline = time.monotonic() + 15
    with database.admin_engine().connect() as conn:
        while time.monotonic() < deadline:
            if (
                pid
                and conn.execute(
                    text("SELECT wait_event_type = 'Lock' FROM pg_stat_activity WHERE pid = :pid"),
                    {"pid": pid[0]},
                ).scalar_one_or_none()
            ):
                return
            conn.rollback()
            time.sleep(0.02)
    raise AssertionError("the reservation never waited on the concurrent lock")


def test_concurrent_reservations_never_exceed_a_shared_limit(
    database: MigratedDatabase, prices: PriceList
):
    # Demand is several times the organization limit, so most attempts must be refused.
    limit = 200_000
    s = ledger_scenario(database, org=limit, user=10**9, workspace=10**9, assignment=10**9)
    people = [s.person]
    threads, attempts = 12, 40
    worker = make_engine(database.urls[Role.APP_WORKER], pool_size=threads, max_overflow=0)
    gateway = make_engine(database.urls[Role.GW], pool_size=threads, max_overflow=0)
    granted: list[int] = []
    lock = threading.Lock()
    start_line = threading.Barrier(threads)

    def run(seed: int) -> None:
        rng = random.Random(seed)
        ctx = TenantContext(s.tenant.id, people[0])
        start_line.wait()
        for _ in range(attempts):
            amount = rng.randint(1_000, 20_000)
            with tenant_connection(worker, ctx) as conn:
                result = ledger.reserve(
                    conn,
                    principal_id=s.person,
                    amount_micro_usd=amount,
                    price_version_id=prices.version_id,
                    purpose="race",
                    ttl=TTL,
                )
            if result.status is not ReserveStatus.RESERVED:
                continue
            with lock:
                granted.append(amount)
            with tenant_connection(gateway, ctx) as conn:
                if rng.random() < 0.3:
                    ledger.release(conn, result.reservation_id)
                    continue
                assert (
                    ledger.start(
                        conn,
                        result.reservation_id,
                        principal_id=s.person,
                        required_micro_usd=amount,
                        settle_within=SETTLE,
                    )
                    is StartStatus.STARTED
                )
                ledger.settle(conn, result.reservation_id, rng.randint(0, amount))

    try:
        with ThreadPoolExecutor(threads) as pool:
            list(pool.map(run, range(threads)))
    finally:
        worker.dispose()
        gateway.dispose()

    org = balances(database, [s.org_budget])[s.org_budget]
    assert org.reserved == 0
    assert org.spent <= limit
    assert len(granted) < threads * attempts, "the limit never bit; the test proves nothing"
    assert journal_matches_balances(database, s.tenant.id)


@dataclass
class _Held:
    amount: int
    state: str = "active"  # active, started, released, charged, settled
    actual: int = 0
    assignment: bool = False


@dataclass
class _Model:
    holds: dict[UUID, _Held] = field(default_factory=dict[UUID, _Held])

    def balance(self, *, assignment: bool | None) -> tuple[int, int]:
        reserved = spent = 0
        for held in self.holds.values():
            if assignment is not None and held.assignment != assignment:
                continue
            if held.state in ("active", "started"):
                reserved += held.amount
            elif held.state == "charged":
                spent += held.amount
            elif held.state == "settled":
                spent += held.actual
        return reserved, spent


@settings(
    max_examples=15,
    stateful_step_count=25,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
class LedgerMachine(RuleBasedStateMachine):
    """Random sequences of reserve, start, settle, release and expiry against one tenant."""

    database: MigratedDatabase
    prices: PriceList

    def __init__(self) -> None:
        super().__init__()
        self.model = _Model()

    @initialize()
    def setup(self) -> None:
        self.s = ledger_scenario(
            self.database, org=150_000, user=120_000, workspace=60_000, assignment=80_000
        )
        self.ctx = TenantContext(self.s.tenant.id, self.s.person)
        self.worker = self.database.engine(Role.APP_WORKER)
        self.gw = self.database.engine(Role.GW)

    def _pick(self, data: st.DataObject, *states: str) -> UUID | None:
        ids = sorted(i for i, h in self.model.holds.items() if h.state in states)
        return data.draw(st.sampled_from(ids)) if ids else None

    @rule(amount=st.integers(1, 50_000), assignment=st.booleans())
    def reserve(self, amount: int, assignment: bool) -> None:
        reserved, spent = self.model.balance(assignment=None)
        leaf_reserved, leaf_spent = self.model.balance(assignment=assignment)
        leaf_limit = 80_000 if assignment else 60_000
        fits = (
            reserved + spent + amount <= 120_000  # user (tighter than organization)
            and leaf_reserved + leaf_spent + amount <= leaf_limit
        )
        with tenant_connection(self.worker, self.ctx) as conn:
            result = ledger.reserve(
                conn,
                principal_id=self.s.person,
                assignment_id=self.s.assignment if assignment else None,
                amount_micro_usd=amount,
                price_version_id=self.prices.version_id,
                purpose="model",
                ttl=TTL,
            )
        assert (result.status is ReserveStatus.RESERVED) == fits
        if fits:
            self.model.holds[result.reservation_id] = _Held(amount, assignment=assignment)

    @rule(data=st.data())
    def start(self, data: st.DataObject) -> None:
        if (rid := self._pick(data, "active")) is None:
            return
        held = self.model.holds[rid]
        with tenant_connection(self.gw, self.ctx) as conn:
            status = ledger.start(
                conn,
                rid,
                principal_id=self.s.person,
                assignment_id=self.s.assignment if held.assignment else None,
                required_micro_usd=held.amount,
                settle_within=SETTLE,
            )
        assert status is StartStatus.STARTED
        held.state = "started"

    @rule(data=st.data(), fraction=st.floats(0, 1.5))
    def settle(self, data: st.DataObject, fraction: float) -> None:
        if (rid := self._pick(data, "started", "charged")) is None:
            return
        held = self.model.holds[rid]
        actual = int(held.amount * fraction)
        with tenant_connection(self.gw, self.ctx) as conn:
            status = ledger.settle(conn, rid, actual)
        overrun = actual > held.amount
        assert status is (SettleStatus.SETTLED_OVERRUN if overrun else SettleStatus.SETTLED)
        held.state, held.actual = "settled", actual

    @rule(data=st.data())
    def release(self, data: st.DataObject) -> None:
        if (rid := self._pick(data, "active")) is None:
            return
        with tenant_connection(self.worker, self.ctx) as conn:
            ledger.release(conn, rid)
        self.model.holds[rid].state = "released"

    @rule(data=st.data())
    def expire(self, data: st.DataObject) -> None:
        if (rid := self._pick(data, "active", "started")) is None:
            return
        make_due(self.database, rid)
        with tenant_connection(self.worker, self.ctx) as conn:
            ledger.expire(conn, rid)
        held = self.model.holds[rid]
        held.state = "released" if held.state == "active" else "charged"

    @invariant()
    def balances_match_the_model(self) -> None:
        if not hasattr(self, "s"):
            return
        db = balances(
            self.database,
            [
                self.s.org_budget,
                self.s.user_budget,
                self.s.workspace_budget,
                self.s.assignment_budget,
            ],
        )
        total_reserved, total_spent = self.model.balance(assignment=None)
        assert db[self.s.org_budget] == Balance(150_000, total_spent, total_reserved)
        assert db[self.s.user_budget] == Balance(120_000, total_spent, total_reserved)
        reserved, spent = self.model.balance(assignment=False)
        assert db[self.s.workspace_budget] == Balance(60_000, spent, reserved)
        reserved, spent = self.model.balance(assignment=True)
        assert db[self.s.assignment_budget] == Balance(80_000, spent, reserved)
        assert journal_matches_balances(self.database, self.s.tenant.id)


def test_ledger_state_machine(database: MigratedDatabase, prices: PriceList):
    LedgerMachine.database = database
    LedgerMachine.prices = prices
    LedgerMachine.TestCase().runTest()
