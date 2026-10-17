"""Properties of the permission ceiling, over generated grants and requests.

- The evaluator never raises, whatever it is given; it only allows or denies.
- Not widening is sound: if ``widens(old, new)`` finds nothing, ``new`` allows no request that
  ``old`` denied. That is what lets a version skip re-acceptance (RUN-03).
- A request built inside a grant is allowed, and pushing any one element outside the grant
  denies it for that element's reason (the red-team cases, generated).
"""

import itertools
from decimal import Decimal
from typing import Any

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from collicity_grants import evaluate, widens
from collicity_grants.models import (
    Allow,
    CallRequest,
    CapabilityGrant,
    Deny,
    DenyCode,
    FieldLimit,
)
from collicity_testing import grants as sample
from collicity_testing.grants import CONNECTOR, VERSION

MANIFEST = sample.manifest()
POLICY = sample.policy()
SCOPES = sample.consented()

UNIVERSE = {
    "workbook": ["w1", "w2", "w3"],
    "table": ["t1", "t2", "t3"],
    "folder": ["f1", "f2"],
}
ACTIONS = ["excel.read_rows", "excel.update_row", "mail.list_messages", "mail.create_reply_draft"]
ROW_FIELDS = sorted(sample.ROW_FIELDS)
SENDERS = ["a@x.example", "b@y.example"]

decimals = st.decimals(min_value=-100, max_value=200, places=1, allow_nan=False)


@st.composite
def limits(draw: st.DrawFn) -> FieldLimit:
    return FieldLimit(
        min=draw(st.none() | decimals),
        max=draw(st.none() | decimals),
        max_change=draw(st.none() | st.decimals(min_value=0, max_value=100, places=1)),
    )


@st.composite
def grant_for(draw: st.DrawFn, action: str) -> CapabilityGrant:
    spec = MANIFEST.actions[action]
    return CapabilityGrant(
        connector=CONNECTOR,
        manifest_version=VERSION,
        action=action,
        resources={
            param: frozenset(draw(st.sets(st.sampled_from(UNIVERSE[param]))))
            for param in spec.resource_params
        },
        read_fields=frozenset(draw(st.sets(st.sampled_from(ROW_FIELDS)))),
        write_fields=frozenset(draw(st.sets(st.sampled_from(ROW_FIELDS)))),
        recipient_rule=draw(st.sampled_from([None, "original_sender_only"])),
        limits=draw(st.dictionaries(st.sampled_from(["Qty", "Cost"]), limits(), max_size=2)),
        max_records_per_run=draw(st.none() | st.integers(0, 30)),
    )


@st.composite
def grant_sets(draw: st.DrawFn) -> list[CapabilityGrant]:
    actions = draw(st.sets(st.sampled_from(ACTIONS)))
    return [draw(grant_for(action)) for action in sorted(actions)]


@st.composite
def narrowed(draw: st.DrawFn, grants: list[CapabilityGrant]) -> list[CapabilityGrant]:
    """A grant set that grants no more than ``grants``: some capabilities dropped, each kept
    one shrunk element by element."""
    out: list[CapabilityGrant] = []
    for grant in grants:
        if draw(st.booleans()) and draw(st.booleans()):
            continue
        tightened_limits: dict[str, FieldLimit] = {}
        for name, limit in grant.limits.items():
            tightened_limits[name] = FieldLimit(
                min=None if limit.min is None else limit.min + draw(st.sampled_from([0, 1])),
                max=None if limit.max is None else limit.max - draw(st.sampled_from([0, 1])),
                max_change=None
                if limit.max_change is None
                else max(Decimal(0), limit.max_change - draw(st.sampled_from([0, 1]))),
            )
        if draw(st.booleans()):
            tightened_limits.setdefault("Qty", FieldLimit(max_change=Decimal(5)))
        volume = grant.max_records_per_run
        out.append(
            grant.model_copy(
                update={
                    "resources": {
                        param: frozenset(draw(st.sets(st.sampled_from(sorted(ids)))))
                        if ids
                        else ids
                        for param, ids in grant.resources.items()
                    },
                    "read_fields": frozenset(
                        draw(st.sets(st.sampled_from(sorted(grant.read_fields))))
                    )
                    if grant.read_fields
                    else grant.read_fields,
                    "write_fields": frozenset(
                        draw(st.sets(st.sampled_from(sorted(grant.write_fields))))
                    )
                    if grant.write_fields
                    else grant.write_fields,
                    "recipient_rule": grant.recipient_rule if draw(st.booleans()) else None,
                    "limits": tightened_limits,
                    "max_records_per_run": draw(st.integers(0, 10))
                    if volume is None
                    else draw(st.integers(0, volume)),
                }
            )
        )
    return out


any_value = st.one_of(
    st.integers(-200, 300),
    decimals,
    st.floats(allow_nan=True, allow_infinity=True),
    st.text(max_size=8),
    st.sampled_from(["open", "ordered", "closed", "=1+1", "2026-10-15"]),
    st.booleans(),
    st.none(),
    st.lists(st.integers(), max_size=2),
)


@st.composite
def requests(draw: st.DrawFn) -> CallRequest:
    action = draw(st.sampled_from([*ACTIONS, "mail.search", "sharepoint.grant_access"]))
    spec = MANIFEST.actions[action]
    resources = {
        param: draw(st.sampled_from(UNIVERSE.get(param, ["x1", "x2"])))
        for param in spec.resource_params
        if draw(st.integers(0, 9)) > 0
    }
    params: dict[str, Any] = {}
    if spec.write_param:
        params[spec.write_param] = draw(
            st.dictionaries(st.sampled_from([*ROW_FIELDS, "Price"]), any_value, max_size=3)
        )
    for param in spec.recipient_params:
        if draw(st.booleans()):
            params[param] = draw(
                st.one_of(
                    st.lists(st.sampled_from(SENDERS), max_size=2),
                    st.sampled_from(SENDERS),
                    any_value,
                )
            )
    if draw(st.integers(0, 9)) == 0:
        params["$expand"] = "everything"
    return CallRequest(
        connector=CONNECTOR,
        manifest_version=VERSION,
        action=action,
        params=params,
        resources=resources,
        record_count=draw(st.integers(0, 12)),
        volume_used=draw(st.integers(0, 30)),
        original_sender=draw(st.none() | st.sampled_from(SENDERS)),
        current_values=draw(st.dictionaries(st.just("Qty"), any_value, max_size=1)),
    )


def allowed(grants: list[CapabilityGrant], request: CallRequest) -> bool:
    return isinstance(evaluate(MANIFEST, grants, POLICY, SCOPES, request), Allow)


@given(grant_sets(), requests())
def test_the_evaluator_only_ever_allows_or_denies(
    grants: list[CapabilityGrant], request: CallRequest
):
    decision = evaluate(MANIFEST, grants, POLICY, SCOPES, request)
    assert isinstance(decision, Allow | Deny)


@given(grant_sets())
def test_a_grant_set_does_not_widen_itself(grants: list[CapabilityGrant]):
    assert widens(grants, grants) == []


ELEMENTS = [
    "workbook",
    "table",
    "folder",
    "read",
    "write",
    "rule",
    "volume",
    "capability",
    "Qty.min",
    "Qty.max",
    "Qty.max_change",
    "Qty.remove",
]


@st.composite
def perturbed(draw: st.DrawFn, grant: CapabilityGrant, focus: str | None = None) -> CapabilityGrant:
    """``grant`` with its elements kept, narrowed or widened: all independently, or only
    ``focus``, so the change that matters can't hide behind another."""

    def changes(element: str) -> bool:
        return focus is None or focus == element

    def change_set(element: str, current: frozenset[str], universe: list[str]) -> frozenset[str]:
        if not changes(element):
            return current
        how = draw(st.sampled_from(["keep", "narrow", "widen"]))
        if how == "narrow" and current:
            return current - {draw(st.sampled_from(sorted(current)))}
        outside = [x for x in universe if x not in current]
        if how == "widen" and outside:
            return current | {draw(st.sampled_from(outside))}
        return current

    def change_limit(field: str, limit: FieldLimit | None) -> FieldLimit | None:
        step = Decimal(draw(st.integers(1, 30)))
        if focus is not None:
            if not focus.startswith(f"{field}.") or limit is None:
                return limit
            part = focus.removeprefix(f"{field}.")
            if part == "remove":
                return None
            how = draw(st.sampled_from(["tighten", "loosen"]))
            bound = part
        else:
            how = draw(st.sampled_from(["keep", "tighten", "loosen", "remove", "add"]))
            if how == "remove":
                return None
            if how == "add" or limit is None:
                return FieldLimit(max_change=step) if how == "add" else limit
            if how == "keep":
                return limit
            bound = draw(st.sampled_from(["min", "max", "max_change"]))
        # Move one bound, so a loosened bound can't hide behind another one moving.
        value = getattr(limit, bound)
        if value is None:
            return limit
        looser = how == "loosen"
        moved = value - step if (bound == "min") == looser else value + step
        return limit.model_copy(update={bound: max(Decimal(0), moved)})

    limits = {
        field: limit
        for field in ("Qty", "Cost")
        if (limit := change_limit(field, grant.limits.get(field))) is not None
    }
    volume = grant.max_records_per_run
    volume_how = (
        draw(st.sampled_from(["keep", "lower", "raise", "remove", "add"]))
        if changes("volume")
        else "keep"
    )
    if volume_how == "remove":
        volume = None
    elif volume_how == "add" and volume is None:
        volume = draw(st.integers(1, 30))
    elif volume is not None and volume_how in ("lower", "raise"):
        volume = max(0, volume + (1 if volume_how == "raise" else -1) * draw(st.integers(1, 5)))
    rule = grant.recipient_rule
    if changes("rule") and draw(st.booleans()):
        rule = None if rule else "original_sender_only"
    return grant.model_copy(
        update={
            "resources": {
                param: change_set(param, grant.resources.get(param, frozenset()), UNIVERSE[param])
                for param in MANIFEST.actions[grant.action].resource_params
            },
            "read_fields": change_set("read", grant.read_fields, ROW_FIELDS),
            "write_fields": change_set("write", grant.write_fields, ROW_FIELDS),
            "recipient_rule": rule,
            "limits": limits,
            "max_records_per_run": volume,
        }
    )


def probes(grant: CapabilityGrant) -> list[CallRequest]:
    """Requests at every edge of ``grant``: each granted resource combination, and each
    writable field at the furthest value the grant allows each way. A loosened element shows
    up in at least one of them."""
    spec = MANIFEST.actions[grant.action]
    current = Decimal(10)
    resource_sets = [
        dict(zip(grant.resources, combo, strict=True))
        for combo in itertools.product(
            *(sorted(ids) or UNIVERSE[param][:1] for param, ids in grant.resources.items())
        )
    ]
    writes: list[dict[str, Any] | None] = [None]
    if spec.write_param:
        writes = []
        for field in sorted(grant.write_fields) or ROW_FIELDS[:1]:
            field_type = sample.ROW_FIELDS[field].type.value
            if field_type != "number":
                writes.append(
                    {field: {"choice": "open", "date": "2026-10-15"}.get(field_type, "note")}
                )
                continue
            limit = grant.limits.get(field) or FieldLimit()
            ups = [b for b in (limit.max,) if b is not None]
            downs = [b for b in (limit.min,) if b is not None]
            if limit.max_change is not None:
                ups.append(current + limit.max_change)
                downs.append(current - limit.max_change)
            highest = min(ups, default=current + 1000)
            lowest = max(downs, default=current - 1000)
            writes += [{field: value} for value in sorted({current, highest, lowest})]
    volume = grant.max_records_per_run
    return [
        CallRequest(
            connector=CONNECTOR,
            manifest_version=VERSION,
            action=grant.action,
            params=({spec.write_param: write} if spec.write_param and write else {})
            | ({"to": [SENDERS[0]]} if spec.recipient_params else {}),
            resources=resources,
            volume_used=30 if volume is None else max(0, volume - 1),
            original_sender=SENDERS[0],
            current_values={"Qty": current, "Cost": current},
        )
        for resources in resource_sets
        for write in writes
    ]


@st.composite
def workable_grant_sets(draw: st.DrawFn) -> list[CapabilityGrant]:
    """Grants with room to act in every element, so requests inside them can succeed."""
    current = Decimal(10)
    room = st.none() | st.decimals(min_value=0, max_value=30, places=0)
    out: list[CapabilityGrant] = []
    bounded = st.decimals(min_value=0, max_value=30, places=0)
    others = draw(st.sets(st.sampled_from([a for a in ACTIONS if a != "excel.update_row"])))
    for action in sorted({"excel.update_row", *others}):
        spec = MANIFEST.actions[action]
        limits = {
            "Qty": FieldLimit(
                min=current - draw(bounded),
                max=current + draw(bounded),
                max_change=draw(bounded),
            )
        }
        if draw(st.booleans()):
            below, above, change = draw(room), draw(room), draw(room)
            limits["Cost"] = FieldLimit(
                min=None if below is None else current - below,
                max=None if above is None else current + above,
                max_change=change,
            )
        out.append(
            CapabilityGrant(
                connector=CONNECTOR,
                manifest_version=VERSION,
                action=action,
                resources={
                    param: frozenset(
                        draw(st.sets(st.sampled_from(UNIVERSE[param]), min_size=1, max_size=2))
                    )
                    for param in spec.resource_params
                },
                read_fields=frozenset(draw(st.sets(st.sampled_from(ROW_FIELDS), min_size=1))),
                write_fields=frozenset({"Qty"} | draw(st.sets(st.sampled_from(ROW_FIELDS)))),
                recipient_rule=draw(st.sampled_from([None, "original_sender_only"])),
                limits=limits,
                max_records_per_run=draw(st.none() | st.integers(1, 30)),
            )
        )
    return out


@pytest.mark.parametrize("focus", [*ELEMENTS, None])
@given(data=st.data(), old=workable_grant_sets())
def test_whatever_new_grants_allow_that_old_ones_denied_is_reported_as_widening(
    focus: str | None, data: st.DataObject, old: list[CapabilityGrant]
):
    new = [data.draw(perturbed(grant, focus)) for grant in old]
    extra = data.draw(st.sampled_from(ACTIONS))
    if focus in (None, "capability") and all(g.action != extra for g in old):
        new.append(data.draw(grant_for(extra)))
    reported = widens(old, new)
    for grant in new:
        for request in probes(grant):
            if allowed(new, request) and not allowed(old, request):
                assert reported, (grant, request)


@given(narrowed_from=grant_sets(), data=st.data())
def test_narrowing_is_never_reported_as_widening(
    narrowed_from: list[CapabilityGrant], data: st.DataObject
):
    assert widens(narrowed_from, data.draw(narrowed(narrowed_from))) == []


# ---- Requests built inside a grant, then pushed out one element at a time --------------------


@st.composite
def update_inside(
    draw: st.DrawFn, *, bounded_change: bool = False, volume: bool = False
) -> tuple[CapabilityGrant, CallRequest]:
    """An update_row grant and a request inside every element of it, built directly."""
    current = Decimal(10)
    if bounded_change:
        field = draw(st.sampled_from(["Qty", "Cost"]))
        write_fields = {field} | draw(st.sets(st.sampled_from(ROW_FIELDS), max_size=4))
    else:
        write_fields = draw(st.sets(st.sampled_from(ROW_FIELDS), min_size=1, max_size=5))
        field = draw(st.sampled_from(sorted(write_fields)))
    spec = sample.ROW_FIELDS[field]
    limit_map: dict[str, FieldLimit] = {}
    margin = st.decimals(min_value=0, max_value=20, places=1)
    if spec.type.value == "number":
        delta = draw(st.decimals(min_value=-20, max_value=20, places=1))
        value: Any = current + delta
        limit_map[field] = FieldLimit(
            min=None if draw(st.booleans()) else value - draw(margin),
            max=None if draw(st.booleans()) else value + draw(margin),
            max_change=abs(delta) + draw(margin) if bounded_change or draw(st.booleans()) else None,
        )
    elif spec.type.value == "choice":
        value = draw(st.sampled_from(sorted(spec.choices)))
    elif spec.type.value == "date":
        value = "2026-10-15"
    else:
        value = draw(st.text(max_size=5))
    resources = {
        param: frozenset(draw(st.sets(st.sampled_from(UNIVERSE[param]), min_size=1, max_size=2)))
        for param in ("workbook", "table")
    }
    grant = CapabilityGrant(
        connector=CONNECTOR,
        manifest_version=VERSION,
        action="excel.update_row",
        resources=resources,
        write_fields=frozenset(write_fields),
        limits=limit_map,
        max_records_per_run=draw(st.integers(1, 30))
        if volume
        else draw(st.none() | st.integers(1, 30)),
    )
    request = CallRequest(
        connector=CONNECTOR,
        manifest_version=VERSION,
        action="excel.update_row",
        params={"values": {field: value}, "row_id": "7"},
        resources={param: sorted(ids)[0] for param, ids in resources.items()},
        current_values={"Qty": current, "Cost": current},
    )
    return grant, request


def _code(grant: CapabilityGrant, request: CallRequest) -> DenyCode | None:
    decision = evaluate(MANIFEST, [grant], POLICY, SCOPES, request)
    return decision.code if isinstance(decision, Deny) else None


@given(update_inside())
def test_a_request_inside_the_grant_is_allowed(case: tuple[CapabilityGrant, CallRequest]):
    grant, request = case
    assert _code(grant, request) is None


@given(update_inside(), st.sampled_from(["workbook", "table"]))
def test_a_resource_outside_the_selector_is_denied(
    case: tuple[CapabilityGrant, CallRequest], param: str
):
    grant, request = case
    outside = [r for r in UNIVERSE[param] if r not in grant.resources[param]]
    assume(outside)
    moved = request.model_copy(update={"resources": request.resources | {param: outside[0]}})
    assert _code(grant, moved) is DenyCode.RESOURCE_NOT_GRANTED


@given(update_inside())
def test_a_field_outside_the_allowlist_is_denied(case: tuple[CapabilityGrant, CallRequest]):
    grant, request = case
    outside = [f for f in ROW_FIELDS if f not in grant.write_fields]
    assume(outside)
    values = request.params["values"] | {outside[0]: "x"}
    moved = request.model_copy(update={"params": request.params | {"values": values}})
    assert _code(grant, moved) is DenyCode.FIELD_NOT_WRITABLE


@given(update_inside(bounded_change=True))
def test_a_change_beyond_the_limit_is_denied(case: tuple[CapabilityGrant, CallRequest]):
    grant, request = case
    (field,) = request.params["values"]
    max_change = grant.limits[field].max_change
    assert max_change is not None
    beyond = request.current_values[field] + max_change + 1
    moved = request.model_copy(update={"params": request.params | {"values": {field: beyond}}})
    assert _code(grant, moved) is DenyCode.LIMIT_EXCEEDED


@given(update_inside(volume=True))
def test_one_record_past_the_volume_limit_is_denied(case: tuple[CapabilityGrant, CallRequest]):
    grant, request = case
    assert grant.max_records_per_run is not None
    moved = request.model_copy(update={"volume_used": grant.max_records_per_run})
    assert _code(grant, moved) is DenyCode.VOLUME_EXCEEDED


# ---- Every single-element widening is reported --------------------------------------------------


BASE = CapabilityGrant(
    connector=CONNECTOR,
    manifest_version=VERSION,
    action="excel.update_row",
    resources={"workbook": frozenset({"w1"}), "table": frozenset({"t1"})},
    read_fields=frozenset({"Item"}),
    write_fields=frozenset({"Qty"}),
    limits={"Qty": FieldLimit(min=Decimal(0), max=Decimal(100), max_change=Decimal(50))},
    max_records_per_run=20,
)


def _wider(**update: Any) -> list[CapabilityGrant]:
    return [BASE.model_copy(update=update)]


@pytest.mark.parametrize(
    "wider",
    [
        _wider(resources={"workbook": frozenset({"w1", "w2"}), "table": frozenset({"t1"})}),
        _wider(resources={"workbook": frozenset({"w1"}), "table": frozenset({"t1", "t2"})}),
        _wider(read_fields=frozenset({"Item", "Cost"})),
        _wider(write_fields=frozenset({"Qty", "Status"})),
        _wider(recipient_rule="original_sender_only"),
        _wider(limits={}),
        _wider(
            limits={"Qty": FieldLimit(min=Decimal(-1), max=Decimal(100), max_change=Decimal(50))}
        ),
        _wider(
            limits={"Qty": FieldLimit(min=Decimal(0), max=Decimal(101), max_change=Decimal(50))}
        ),
        _wider(
            limits={"Qty": FieldLimit(min=Decimal(0), max=Decimal(100), max_change=Decimal(51))}
        ),
        _wider(limits={"Qty": FieldLimit(min=None, max=Decimal(100), max_change=Decimal(50))}),
        _wider(max_records_per_run=21),
        _wider(max_records_per_run=None),
        _wider(manifest_version="2026.11.0"),
        [BASE, BASE.model_copy(update={"action": "excel.read_rows"})],
    ],
)
def test_each_kind_of_widening_is_reported(wider: list[CapabilityGrant]):
    assert widens([BASE], wider)
