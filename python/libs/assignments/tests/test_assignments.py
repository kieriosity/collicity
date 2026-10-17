"""Versions, acceptance, step grants and the kill switch (CON-04, ASG-13, SEC-12, RUN-03)."""

import json
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from uuid import UUID

import psycopg
import pytest
from sqlalchemy import Connection, insert, text
from sqlalchemy.exc import IntegrityError, ProgrammingError

from collicity_assignments import (
    EPOCH_CHANNEL,
    CheckStatus,
    IssueStatus,
    accept,
    check_step_grant,
    create_assignment,
    create_version,
    end_step_grant,
    issue_step_grant,
    pause,
    resume,
    version_grants,
)
from collicity_core import TenantContext
from collicity_db import Role, tenant_connection
from collicity_db.models import RoleGrant
from collicity_db.roles import libpq_url
from collicity_grants.models import CapabilityGrant
from collicity_testing.postgres import MigratedDatabase, SeededTenant, seed_tenant

READ = CapabilityGrant(
    connector="graph",
    manifest_version="2026.10.1",
    action="excel.read_rows",
    resources={"workbook": frozenset({"w1"}), "table": frozenset({"t1"})},
    read_fields=frozenset({"Item", "Qty"}),
)
WRITE = CapabilityGrant(
    connector="graph",
    manifest_version="2026.10.1",
    action="excel.update_row",
    resources={"workbook": frozenset({"w1"}), "table": frozenset({"t1"})},
    write_fields=frozenset({"Qty"}),
    max_records_per_run=20,
)


@dataclass(frozen=True)
class Scene:
    database: MigratedDatabase
    tenant: SeededTenant
    owner: UUID
    other: UUID
    admin: UUID

    @contextmanager
    def as_(self, role: Role, person: UUID | None = None) -> Generator[Connection]:
        ctx = TenantContext(self.tenant.id, person)
        with tenant_connection(self.database.engine(role), ctx) as conn:
            yield conn

    def accepted_assignment(self, grants: list[CapabilityGrant]) -> tuple[UUID, UUID]:
        with self.as_(Role.APP_USER, self.owner) as conn:
            assignment = create_assignment(conn, owner_id=self.owner, name="Stock requests")
            version = create_version(
                conn, assignment_id=assignment, number=1, grants=grants, created_by=self.owner
            )
            accept(conn, version)
        return assignment, version


@pytest.fixture
def scene(database: MigratedDatabase) -> Scene:
    tenant = seed_tenant(database, principals=3)
    owner, other, admin = tenant.principal_ids
    with database.engine(Role.MIGRATOR).begin() as conn:
        conn.execute(
            insert(RoleGrant).values(tenant_id=tenant.id, principal_id=admin, role="admin")
        )
    return Scene(database, tenant, owner, other, admin)


# ---- Versions and acceptance -----------------------------------------------------------------


def test_an_owner_drafts_and_accepts_a_version_which_becomes_current(scene: Scene):
    assignment, version = scene.accepted_assignment([READ])
    with scene.as_(Role.APP_WORKER) as conn:
        current = conn.execute(
            text("SELECT current_version_id FROM assignment WHERE id = :a"), {"a": assignment}
        ).scalar_one()
        assert current == version
        assert version_grants(conn, version) == [READ]
    with scene.database.admin_engine().connect() as conn:
        audited = conn.execute(
            text(
                "SELECT actor_id, agent_id, detail ->> 'version_id' FROM audit.record "
                "WHERE kind = 'version_accepted' AND tenant_id = :t"
            ),
            {"t": scene.tenant.id},
        ).one()
    assert tuple(audited) == (scene.owner, assignment, str(version))


def test_only_the_owner_drafts_versions_of_an_assignment(scene: Scene):
    assignment, _ = scene.accepted_assignment([READ])
    with (
        pytest.raises(ProgrammingError, match="row-level security"),
        scene.as_(Role.APP_USER, scene.other) as conn,
    ):
        create_version(
            conn, assignment_id=assignment, number=2, grants=[READ, WRITE], created_by=scene.other
        )


def test_nobody_creates_an_assignment_in_someone_elses_name(scene: Scene):
    with (
        pytest.raises(ProgrammingError, match="row-level security"),
        scene.as_(Role.APP_USER, scene.other) as conn,
    ):
        create_assignment(conn, owner_id=scene.owner, name="Not mine")


def test_only_the_owner_accepts_and_only_once(scene: Scene):
    with scene.as_(Role.APP_USER, scene.owner) as conn:
        assignment = create_assignment(conn, owner_id=scene.owner, name="Stock")
        version = create_version(
            conn, assignment_id=assignment, number=1, grants=[READ], created_by=scene.owner
        )
    with (
        pytest.raises(ProgrammingError, match="only the assignment's owner"),
        scene.as_(Role.APP_USER, scene.admin) as conn,
    ):
        accept(conn, version)
    with scene.as_(Role.APP_USER, scene.owner) as conn:
        accept(conn, version)
    with (
        pytest.raises(IntegrityError, match="already accepted"),
        scene.as_(Role.APP_USER, scene.owner) as conn,
    ):
        accept(conn, version)


def test_acceptance_records_what_the_new_version_widens(scene: Scene):
    assignment, _ = scene.accepted_assignment([READ])
    wider = READ.model_copy(update={"read_fields": frozenset({"Item", "Qty", "Cost"})})
    with scene.as_(Role.APP_USER, scene.owner) as conn:
        version = create_version(
            conn,
            assignment_id=assignment,
            number=2,
            grants=[wider, WRITE],
            created_by=scene.owner,
        )
        reasons = accept(conn, version)
        stored = conn.execute(
            text("SELECT widening FROM version_acceptance WHERE version_id = :v"), {"v": version}
        ).scalar_one()
    assert reasons == stored
    assert any("reads ['Cost']" in r for r in reasons)
    assert any("excel.update_row: new capability" in r for r in reasons)


def test_equal_content_has_an_equal_hash_whatever_the_set_order(scene: Scene):
    shuffled = READ.model_copy(update={"read_fields": frozenset(["Qty", "Item"])})
    with scene.as_(Role.APP_USER, scene.owner) as conn:
        assignment = create_assignment(conn, owner_id=scene.owner, name="Stock")
        a = create_version(
            conn, assignment_id=assignment, number=1, grants=[READ], created_by=scene.owner
        )
        b = create_version(
            conn, assignment_id=assignment, number=2, grants=[shuffled], created_by=scene.owner
        )
        hashes = conn.execute(
            text("SELECT id, content_hash FROM assignment_version WHERE id IN (:a, :b)"),
            {"a": a, "b": b},
        ).all()
    assert len({bytes(h) for _, h in hashes}) == 1


def test_versions_are_immutable_even_for_migrator(scene: Scene):
    scene.accepted_assignment([READ])
    with (
        pytest.raises(ProgrammingError, match="immutable"),
        scene.database.engine(Role.MIGRATOR).begin() as conn,
    ):
        conn.execute(text("UPDATE assignment_version SET grants = '[]'"))


def test_the_current_version_is_always_one_of_the_assignments_own(scene: Scene):
    first, _ = scene.accepted_assignment([READ])
    _, foreign = scene.accepted_assignment([WRITE])
    with (
        pytest.raises(IntegrityError, match="current_version_id_fkey"),
        scene.database.engine(Role.MIGRATOR).begin() as conn,
    ):
        conn.execute(
            text("UPDATE assignment SET current_version_id = :v WHERE id = :a"),
            {"v": foreign, "a": first},
        )


# ---- Step grants (CON-04) ---------------------------------------------------------------------


def _issue(scene: Scene, assignment: UUID, version: UUID, **kw: object) -> IssueStatus | UUID:
    with scene.as_(Role.APP_WORKER) as conn:
        issued = issue_step_grant(
            conn,
            assignment_id=assignment,
            version_id=version,
            step_key="read-stock",
            **kw,  # type: ignore[arg-type]
        )
    return issued.grant_id if issued.status is IssueStatus.ISSUED else issued.status


def _check(scene: Scene, grant_id: UUID) -> CheckStatus:
    with scene.as_(Role.GW) as conn:
        return check_step_grant(conn, grant_id).status


def test_a_step_grant_carries_only_the_requested_capabilities_of_the_accepted_version(
    scene: Scene,
):
    assignment, version = scene.accepted_assignment([READ, WRITE])
    grant_id = _issue(scene, assignment, version, actions=["excel.read_rows"], dry_run=True)
    assert isinstance(grant_id, UUID)
    with scene.as_(Role.GW) as conn:
        step = check_step_grant(conn, grant_id)
    assert step.valid
    assert step.grants == [READ]
    assert step.dry_run
    assert (step.owner_id, step.assignment_id, step.version_id) == (
        scene.owner,
        assignment,
        version,
    )


def test_no_grant_for_a_draft_or_for_capabilities_outside_the_version(scene: Scene):
    assignment, version = scene.accepted_assignment([READ])
    with scene.as_(Role.APP_USER, scene.owner) as conn:
        draft = create_version(
            conn, assignment_id=assignment, number=2, grants=[READ, WRITE], created_by=scene.owner
        )
    assert _issue(scene, assignment, draft) is IssueStatus.NOT_ACCEPTED
    assert _issue(scene, assignment, version, actions=["excel.update_row"]) is (
        IssueStatus.NOT_IN_VERSION
    )


def test_a_pause_stops_every_earlier_grant_and_a_resume_does_not_revive_them(scene: Scene):
    assignment, version = scene.accepted_assignment([READ])
    before = _issue(scene, assignment, version)
    assert isinstance(before, UUID)

    with scene.as_(Role.APP_USER, scene.admin) as conn:
        pause(conn, "tenant")
    assert _check(scene, before) is CheckStatus.TENANT_PAUSED
    assert _issue(scene, assignment, version) is IssueStatus.TENANT_PAUSED

    with scene.as_(Role.APP_USER, scene.admin) as conn:
        resume(conn, "tenant")
    assert _check(scene, before) is CheckStatus.TENANT_PAUSED  # its epoch is gone for good
    after = _issue(scene, assignment, version)
    assert isinstance(after, UUID)
    assert _check(scene, after) is CheckStatus.VALID


def test_only_admins_pause_everyone(scene: Scene):
    with (
        pytest.raises(ProgrammingError, match="not allowed"),
        scene.as_(Role.APP_USER, scene.owner) as conn,
    ):
        pause(conn, "tenant")


def test_people_pause_their_own_runs_but_cannot_lift_an_admins_pause(scene: Scene):
    assignment, version = scene.accepted_assignment([READ])
    grant_id = _issue(scene, assignment, version)
    assert isinstance(grant_id, UUID)

    with scene.as_(Role.APP_USER, scene.owner) as conn:
        pause(conn, "principal", scene.owner)
    assert _check(scene, grant_id) is CheckStatus.OWNER_PAUSED
    with scene.as_(Role.APP_USER, scene.owner) as conn:
        resume(conn, "principal", scene.owner)

    with scene.as_(Role.APP_USER, scene.admin) as conn:
        pause(conn, "principal", scene.owner)
    with (
        pytest.raises(ProgrammingError, match="not allowed"),
        scene.as_(Role.APP_USER, scene.owner) as conn,
    ):
        resume(conn, "principal", scene.owner)
    with (
        pytest.raises(ProgrammingError, match="not allowed"),
        scene.as_(Role.APP_USER, scene.other) as conn,
    ):
        pause(conn, "principal", scene.owner)
    with scene.as_(Role.APP_USER, scene.admin) as conn:
        resume(conn, "principal", scene.owner)


def test_an_owner_pauses_their_assignment_and_others_cannot(scene: Scene):
    assignment, version = scene.accepted_assignment([READ])
    grant_id = _issue(scene, assignment, version)
    assert isinstance(grant_id, UUID)
    with (
        pytest.raises(ProgrammingError, match="not allowed"),
        scene.as_(Role.APP_USER, scene.other) as conn,
    ):
        pause(conn, "assignment", assignment)
    with scene.as_(Role.APP_USER, scene.owner) as conn:
        pause(conn, "assignment", assignment)
    assert _check(scene, grant_id) is CheckStatus.ASSIGNMENT_PAUSED
    assert _issue(scene, assignment, version) is IssueStatus.ASSIGNMENT_PAUSED


def test_a_suspended_owner_or_archived_assignment_ends_the_grant(scene: Scene):
    assignment, version = scene.accepted_assignment([READ])
    first = _issue(scene, assignment, version)
    assert isinstance(first, UUID)
    with scene.database.engine(Role.MIGRATOR).begin() as conn:
        conn.execute(
            text("UPDATE principal SET status = 'suspended' WHERE id = :p"), {"p": scene.owner}
        )
    assert _check(scene, first) is CheckStatus.OWNER_INACTIVE
    assert _issue(scene, assignment, version) is IssueStatus.OWNER_INACTIVE

    with scene.database.engine(Role.MIGRATOR).begin() as conn:
        conn.execute(
            text("UPDATE principal SET status = 'active' WHERE id = :p"), {"p": scene.owner}
        )
        conn.execute(
            text("UPDATE assignment SET archived_at = now() WHERE id = :a"), {"a": assignment}
        )
    assert _check(scene, first) is CheckStatus.ARCHIVED


def test_a_grant_ends_with_its_step_or_when_it_expires(scene: Scene):
    assignment, version = scene.accepted_assignment([READ])
    ending = _issue(scene, assignment, version)
    expiring = _issue(scene, assignment, version)
    assert isinstance(ending, UUID)
    assert isinstance(expiring, UUID)
    with scene.as_(Role.APP_WORKER) as conn:
        assert end_step_grant(conn, ending)
        assert not end_step_grant(conn, ending)
    assert _check(scene, ending) is CheckStatus.ENDED
    with scene.database.engine(Role.MIGRATOR).begin() as conn:
        conn.execute(
            text("UPDATE step_grant SET expires_at = now() - interval '1 second' WHERE id = :g"),
            {"g": expiring},
        )
    assert _check(scene, expiring) is CheckStatus.EXPIRED


def test_another_tenant_cannot_use_a_grant(scene: Scene, database: MigratedDatabase):
    assignment, version = scene.accepted_assignment([READ])
    grant_id = _issue(scene, assignment, version)
    assert isinstance(grant_id, UUID)
    other = seed_tenant(database)
    with tenant_connection(database.engine(Role.GW), TenantContext(other.id)) as conn:
        assert check_step_grant(conn, grant_id).status is CheckStatus.NOT_FOUND


def test_pauses_are_audited_and_announced(scene: Scene):
    assignment, _ = scene.accepted_assignment([READ])
    with psycopg.connect(libpq_url(scene.database.admin_url), autocommit=True) as listener:
        listener.execute(f"LISTEN {EPOCH_CHANNEL}")
        with scene.as_(Role.APP_USER, scene.owner) as conn:
            pause(conn, "assignment", assignment)
        deadline = time.monotonic() + 5
        notices = []
        while not notices and time.monotonic() < deadline:
            notices = list(listener.notifies(timeout=0.5, stop_after=1))
    payload = json.loads(notices[0].payload)
    assert payload == {
        "tenant": str(scene.tenant.id),
        "scope": "assignment",
        "target": str(assignment),
    }
    with scene.database.admin_engine().connect() as conn:
        audited = conn.execute(
            text(
                "SELECT actor_id, outcome, detail ->> 'scope' FROM audit.record "
                "WHERE kind = 'kill_switch' AND tenant_id = :t"
            ),
            {"t": scene.tenant.id},
        ).one()
    assert tuple(audited) == (scene.owner, "pause", "assignment")


@pytest.mark.parametrize(
    ("scope", "paused"),
    [("principal", CheckStatus.OWNER_PAUSED), ("assignment", CheckStatus.ASSIGNMENT_PAUSED)],
)
def test_lifting_a_person_or_assignment_pause_does_not_revive_earlier_grants(
    scene: Scene, scope: str, paused: CheckStatus
):
    assignment, version = scene.accepted_assignment([READ])
    before = _issue(scene, assignment, version)
    assert isinstance(before, UUID)
    target = scene.owner if scope == "principal" else assignment
    with scene.as_(Role.APP_USER, scene.owner) as conn:
        pause(conn, scope, target)  # type: ignore[arg-type]
        resume(conn, scope, target)  # type: ignore[arg-type]
    assert _check(scene, before) is paused
    after = _issue(scene, assignment, version)
    assert isinstance(after, UUID)
    assert _check(scene, after) is CheckStatus.VALID
