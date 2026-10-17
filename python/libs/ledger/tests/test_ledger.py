"""BUD-03, BUD-10, BUD-11: reservations hold the worst case against every budget, or nothing."""

from datetime import timedelta
from uuid import UUID

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DataError, IntegrityError, ProgrammingError

from collicity_core import TenantContext, new_id
from collicity_db import Role, tenant_connection
from collicity_ledger import ledger
from collicity_ledger.ledger import (
    ExpireStatus,
    ReleaseStatus,
    ReserveStatus,
    SettleStatus,
    StartStatus,
)
from collicity_ledger.pricing import ModelCallBounds, PriceList
from collicity_testing.ledger import (
    Balance,
    LedgerScenario,
    balances,
    journal_matches_balances,
    ledger_scenario,
    make_due,
    seed_budget,
)
from collicity_testing.postgres import MigratedDatabase, seed_tenant

TTL = timedelta(minutes=15)
SETTLE = timedelta(hours=1)


def _ctx(s: LedgerScenario) -> TenantContext:
    return TenantContext(s.tenant.id, s.person)


def _reserve(
    engine: Engine,
    s: LedgerScenario,
    prices: PriceList,
    amount: int,
    *,
    assignment: bool = False,
    reservation_id: UUID | None = None,
) -> ledger.ReserveResult:
    with tenant_connection(engine, _ctx(s)) as conn:
        return ledger.reserve(
            conn,
            principal_id=s.person,
            assignment_id=s.assignment if assignment else None,
            amount_micro_usd=amount,
            price_version_id=prices.version_id,
            purpose="test",
            ttl=TTL,
            reservation_id=reservation_id,
        )


def _start(
    engine: Engine,
    s: LedgerScenario,
    reservation_id: UUID,
    required: int,
    *,
    principal_id: UUID | None = None,
    assignment_id: UUID | None = None,
) -> StartStatus:
    with tenant_connection(engine, _ctx(s)) as conn:
        return ledger.start(
            conn,
            reservation_id,
            principal_id=principal_id or s.person,
            assignment_id=assignment_id,
            required_micro_usd=required,
            settle_within=SETTLE,
        )


def _settle(engine: Engine, s: LedgerScenario, reservation_id: UUID, actual: int) -> SettleStatus:
    with tenant_connection(engine, _ctx(s)) as conn:
        return ledger.settle(conn, reservation_id, actual)


@pytest.fixture
def s(database: MigratedDatabase) -> LedgerScenario:
    return ledger_scenario(database)


@pytest.fixture
def worker(database: MigratedDatabase) -> Engine:
    return database.engine(Role.APP_WORKER)


@pytest.fixture
def gw(database: MigratedDatabase) -> Engine:
    return database.engine(Role.GW)


def _budgets(s: LedgerScenario) -> list[UUID]:
    return [s.org_budget, s.user_budget, s.workspace_budget, s.assignment_budget]


def test_workspace_work_holds_against_org_user_and_workspace(
    database: MigratedDatabase, s: LedgerScenario, worker: Engine, prices: PriceList
):
    result = _reserve(worker, s, prices, 1_000)
    assert result.status is ReserveStatus.RESERVED
    held = balances(database, _budgets(s))
    assert held[s.org_budget].reserved == 1_000
    assert held[s.user_budget].reserved == 1_000
    assert held[s.workspace_budget].reserved == 1_000
    assert held[s.assignment_budget].reserved == 0


def test_assignment_work_holds_against_the_assignment_not_the_workspace(
    database: MigratedDatabase, s: LedgerScenario, worker: Engine, prices: PriceList
):
    assert _reserve(worker, s, prices, 700, assignment=True).status is ReserveStatus.RESERVED
    held = balances(database, _budgets(s))
    assert held[s.assignment_budget].reserved == 700
    assert held[s.workspace_budget].reserved == 0
    assert held[s.org_budget].reserved == 700


def test_without_headroom_nothing_is_reserved_and_the_blocking_budget_is_named(
    database: MigratedDatabase, s: LedgerScenario, worker: Engine, prices: PriceList
):
    before = balances(database, _budgets(s))
    result = _reserve(worker, s, prices, 1_000_001)  # the workspace budget is 1,000,000
    assert result.status is ReserveStatus.INSUFFICIENT
    assert result.blocking_budget_ids == (s.workspace_budget,)
    assert balances(database, _budgets(s)) == before


def test_every_budget_without_headroom_is_named_in_lock_order(
    database: MigratedDatabase, worker: Engine, prices: PriceList
):
    s = ledger_scenario(database, org=100, user=100, workspace=100)
    result = _reserve(worker, s, prices, 101)
    assert result.blocking_budget_ids == (s.org_budget, s.user_budget, s.workspace_budget)


def test_reservation_needs_an_organization_budget_and_a_leaf_budget(
    database: MigratedDatabase, worker: Engine, prices: PriceList
):
    tenant = seed_tenant(database)
    person = tenant.principal_ids[0]
    ctx = TenantContext(tenant.id, person)

    def attempt(assignment_id: UUID | None = None) -> ReserveStatus:
        with tenant_connection(worker, ctx) as conn:
            return ledger.reserve(
                conn,
                principal_id=person,
                assignment_id=assignment_id,
                amount_micro_usd=1,
                price_version_id=prices.version_id,
                purpose="test",
                ttl=TTL,
            ).status

    seed_budget(database, tenant.id, "workspace", 1_000, principal_id=person)
    assert attempt() is ReserveStatus.NO_BUDGET  # no organization budget yet
    seed_budget(database, tenant.id, "organization", 1_000)
    assert attempt() is ReserveStatus.RESERVED
    assert attempt(new_id()) is ReserveStatus.NO_BUDGET  # an assignment with no budget


def test_a_retried_reserve_holds_once(
    database: MigratedDatabase, s: LedgerScenario, worker: Engine, prices: PriceList
):
    reservation_id = new_id()
    first = _reserve(worker, s, prices, 500, reservation_id=reservation_id)
    again = _reserve(worker, s, prices, 500, reservation_id=reservation_id)
    assert first.status is again.status is ReserveStatus.RESERVED
    assert balances(database, [s.org_budget])[s.org_budget].reserved == 500


def test_reusing_a_reservation_id_with_other_terms_fails(
    s: LedgerScenario, worker: Engine, prices: PriceList
):
    reservation_id = new_id()
    _reserve(worker, s, prices, 500, reservation_id=reservation_id)
    with pytest.raises(IntegrityError, match="other terms"):
        _reserve(worker, s, prices, 900, reservation_id=reservation_id)


def test_a_call_without_a_reservation_is_refused(s: LedgerScenario, gw: Engine, prices: PriceList):
    bounds = ModelCallBounds("anthropic/claude-sonnet-5-5", prompt_tokens=10, max_output_tokens=10)
    with tenant_connection(gw, _ctx(s)) as conn:
        status = ledger.start_model_call(
            conn, new_id(), prices=prices, bounds=bounds, principal_id=s.person
        )
    assert status is StartStatus.NOT_FOUND


def test_the_gateway_refuses_a_reservation_below_its_own_worst_case(
    database: MigratedDatabase, s: LedgerScenario, worker: Engine, gw: Engine, prices: PriceList
):
    bounds = ModelCallBounds(
        "anthropic/claude-sonnet-5-5",
        prompt_tokens=10_000,
        cache_write_tokens=8_000,
        max_output_tokens=2_000,
    )
    worst = bounds.worst_case(prices)  # 44,000 micro-USD
    short = _reserve(worker, s, prices, worst - 1)
    enough = _reserve(worker, s, prices, worst)
    with tenant_connection(gw, _ctx(s)) as conn:
        refused = ledger.start_model_call(
            conn, short.reservation_id, prices=prices, bounds=bounds, principal_id=s.person
        )
        started = ledger.start_model_call(
            conn, enough.reservation_id, prices=prices, bounds=bounds, principal_id=s.person
        )
    assert refused is StartStatus.INSUFFICIENT
    assert started is StartStatus.STARTED


def test_start_checks_whose_reservation_it_is(
    s: LedgerScenario, worker: Engine, gw: Engine, prices: PriceList
):
    held = _reserve(worker, s, prices, 100)
    other_person = s.tenant.principal_ids[1]
    assert (
        _start(gw, s, held.reservation_id, 100, principal_id=other_person) is StartStatus.MISMATCH
    )
    assert (
        _start(gw, s, held.reservation_id, 100, assignment_id=s.assignment) is StartStatus.MISMATCH
    )


def test_a_reservation_starts_once(
    s: LedgerScenario, worker: Engine, gw: Engine, prices: PriceList
):
    held = _reserve(worker, s, prices, 100)
    assert _start(gw, s, held.reservation_id, 100) is StartStatus.STARTED
    assert _start(gw, s, held.reservation_id, 100) is StartStatus.NOT_ACTIVE


def test_an_expired_reservation_cannot_start(
    database: MigratedDatabase, s: LedgerScenario, worker: Engine, gw: Engine, prices: PriceList
):
    held = _reserve(worker, s, prices, 100)
    make_due(database, held.reservation_id)
    assert _start(gw, s, held.reservation_id, 100) is StartStatus.EXPIRED


def test_settle_moves_the_hold_to_spent(
    database: MigratedDatabase, s: LedgerScenario, worker: Engine, gw: Engine, prices: PriceList
):
    held = _reserve(worker, s, prices, 1_000)
    assert _settle(gw, s, held.reservation_id, 600) is SettleStatus.NOT_STARTED
    _start(gw, s, held.reservation_id, 1_000)
    assert _settle(gw, s, held.reservation_id, 600) is SettleStatus.SETTLED
    after = balances(database, [s.org_budget, s.workspace_budget])
    assert after[s.org_budget] == Balance(10_000_000, spent=600, reserved=0)
    assert after[s.workspace_budget] == Balance(1_000_000, spent=600, reserved=0)
    assert _settle(gw, s, held.reservation_id, 600) is SettleStatus.NOT_STARTED
    assert journal_matches_balances(database, s.tenant.id)


def test_an_actual_cost_above_the_hold_is_recorded_and_flagged(
    database: MigratedDatabase, s: LedgerScenario, worker: Engine, gw: Engine, prices: PriceList
):
    held = _reserve(worker, s, prices, 1_000)
    _start(gw, s, held.reservation_id, 1_000)
    assert _settle(gw, s, held.reservation_id, 1_500) is SettleStatus.SETTLED_OVERRUN
    assert balances(database, [s.org_budget])[s.org_budget].spent == 1_500


def test_release_gives_back_only_what_never_started(
    database: MigratedDatabase, s: LedgerScenario, worker: Engine, gw: Engine, prices: PriceList
):
    unused = _reserve(worker, s, prices, 300)
    used = _reserve(worker, s, prices, 400)
    _start(gw, s, used.reservation_id, 400)
    with tenant_connection(worker, _ctx(s)) as conn:
        assert ledger.release(conn, unused.reservation_id) is ReleaseStatus.RELEASED
        assert ledger.release(conn, used.reservation_id) is ReleaseStatus.NOT_ACTIVE
        assert ledger.release(conn, new_id()) is ReleaseStatus.NOT_FOUND
    assert balances(database, [s.org_budget])[s.org_budget].reserved == 400


def test_expiry_releases_unstarted_and_charges_started_in_full(
    database: MigratedDatabase, s: LedgerScenario, worker: Engine, gw: Engine, prices: PriceList
):
    never_started = _reserve(worker, s, prices, 300)
    in_flight = _reserve(worker, s, prices, 400)
    _start(gw, s, in_flight.reservation_id, 400)
    make_due(database, never_started.reservation_id)
    make_due(database, in_flight.reservation_id)

    with database.engine(Role.APP_WORKER).connect() as conn:
        due = set(ledger.due_for_expiry(conn))
    assert (s.tenant.id, never_started.reservation_id) in due
    assert (s.tenant.id, in_flight.reservation_id) in due

    with tenant_connection(worker, _ctx(s)) as conn:
        assert ledger.expire(conn, never_started.reservation_id) is ExpireStatus.RELEASED
        assert ledger.expire(conn, in_flight.reservation_id) is ExpireStatus.CHARGED
        assert ledger.expire(conn, in_flight.reservation_id) is ExpireStatus.NOT_DUE
    assert balances(database, [s.org_budget])[s.org_budget] == Balance(
        10_000_000, spent=400, reserved=0
    )

    # The provider's usage arrives late: the full charge is corrected to the actual cost.
    assert _settle(gw, s, in_flight.reservation_id, 250) is SettleStatus.SETTLED
    assert balances(database, [s.org_budget])[s.org_budget].spent == 250
    assert journal_matches_balances(database, s.tenant.id)


def test_a_reservation_not_yet_due_is_left_alone(
    s: LedgerScenario, worker: Engine, prices: PriceList
):
    held = _reserve(worker, s, prices, 100)
    with tenant_connection(worker, _ctx(s)) as conn:
        assert ledger.expire(conn, held.reservation_id) is ExpireStatus.NOT_DUE


def test_another_tenant_cannot_touch_a_reservation(
    database: MigratedDatabase, s: LedgerScenario, worker: Engine, gw: Engine, prices: PriceList
):
    held = _reserve(worker, s, prices, 100)
    other = ledger_scenario(database)
    with tenant_connection(gw, _ctx(other)) as conn:
        assert (
            ledger.start(
                conn,
                held.reservation_id,
                principal_id=s.person,
                required_micro_usd=100,
                settle_within=SETTLE,
            )
            is StartStatus.NOT_FOUND
        )
        assert ledger.settle(conn, held.reservation_id, 1) is SettleStatus.NOT_FOUND
        assert ledger.release(conn, held.reservation_id) is ReleaseStatus.NOT_FOUND
    # Nor can it charge its work to the first tenant's budgets by naming its person.
    with tenant_connection(worker, _ctx(other)) as conn:
        result = ledger.reserve(
            conn,
            principal_id=s.person,
            amount_micro_usd=1,
            price_version_id=prices.version_id,
            purpose="test",
            ttl=TTL,
        )
    assert result.status is ReserveStatus.NO_BUDGET


def test_ledger_functions_need_a_tenant_context(
    s: LedgerScenario, worker: Engine, prices: PriceList
):
    with (
        pytest.raises(ProgrammingError, match="tenant context"),
        worker.begin() as conn,
    ):
        ledger.reserve(
            conn,
            principal_id=s.person,
            amount_micro_usd=1,
            price_version_id=prices.version_id,
            purpose="test",
            ttl=TTL,
        )


@pytest.mark.parametrize(("amount", "ttl"), [(0, TTL), (-5, TTL), (1, timedelta(days=3))])
def test_reserve_rejects_nonsense(
    s: LedgerScenario, worker: Engine, prices: PriceList, amount: int, ttl: timedelta
):
    with pytest.raises(DataError), tenant_connection(worker, _ctx(s)) as conn:
        ledger.reserve(
            conn,
            principal_id=s.person,
            amount_micro_usd=amount,
            price_version_id=prices.version_id,
            purpose="test",
            ttl=ttl,
        )


def test_journal_entries_are_append_only(
    database: MigratedDatabase, s: LedgerScenario, worker: Engine, prices: PriceList
):
    _reserve(worker, s, prices, 100)
    with (
        pytest.raises(ProgrammingError, match="immutable"),
        database.engine(Role.MIGRATOR).begin() as conn,
    ):
        conn.execute(
            text("UPDATE ledger_entry SET spent_delta = 0 WHERE tenant_id = :t"),
            {"t": s.tenant.id},
        )


@pytest.mark.parametrize(
    ("period", "at", "start"),
    [
        ("day", "2026-10-15 23:30:00-04", "2026-10-16 00:00:00+00"),
        ("week", "2026-10-15 12:00:00+00", "2026-10-12 00:00:00+00"),
        ("month", "2026-10-31 23:59:59+00", "2026-10-01 00:00:00+00"),
    ],
)
def test_periods_are_calendar_periods_in_utc(
    database: MigratedDatabase, period: str, at: str, start: str
):
    with database.engine(Role.MIGRATOR).connect() as conn:
        same = conn.execute(
            text(
                "SELECT ledger.period_start(:p, CAST(:at AS timestamptz)) = CAST(:s AS timestamptz)"
            ),
            {"p": period, "at": at, "s": start},
        ).scalar_one()
    assert same
