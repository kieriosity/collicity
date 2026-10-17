"""The beta's grant evaluator (CON-02, CON-03; technical design §5.1 and §5.2).

``evaluate`` is a pure function over typed values::

    (manifest, grants, admin policy, consented scopes, request) -> Allow | Deny(reason)

The gateway calls it before the token broker releases any credential, so a call outside the
permission ceiling never holds a token. It denies by default: anything not explicitly granted
is refused, checks run in a fixed order, and the first failure decides. ``filter_response``
applies the read side afterwards.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

from collicity_grants.models import (
    ActionKind,
    ActionSpec,
    AdminPolicy,
    Allow,
    CallRequest,
    CapabilityGrant,
    ConnectorManifest,
    Decision,
    Deny,
    DenyCode,
    FieldLimit,
)
from collicity_grants.tiers import impact_tier
from collicity_grants.values import InvalidValueError, as_number, typed_value


class _Denied(Exception):  # noqa: N818 (control flow inside this module only)
    def __init__(self, code: DenyCode, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


def _require(condition: bool, code: DenyCode, detail: str) -> None:
    if not condition:
        raise _Denied(code, detail)


def _present[T](value: T | None, code: DenyCode, detail: str) -> T:
    if value is None:
        raise _Denied(code, detail)
    return value


def evaluate(
    manifest: ConnectorManifest,
    grants: Sequence[CapabilityGrant],
    policy: AdminPolicy,
    consented_scopes: frozenset[str],
    request: CallRequest,
    *,
    read_only: bool = False,
) -> Decision:
    """Allow ``request`` only if every element of the permission ceiling allows it.

    ``read_only`` is set for a dry run's step grants (ASG-05): only reads are allowed.
    """
    try:
        return _evaluate(manifest, grants, policy, consented_scopes, request, read_only=read_only)
    except _Denied as denied:
        return Deny(code=denied.code, detail=denied.detail)


def _evaluate(
    manifest: ConnectorManifest,
    grants: Sequence[CapabilityGrant],
    policy: AdminPolicy,
    consented_scopes: frozenset[str],
    request: CallRequest,
    *,
    read_only: bool,
) -> Allow:
    connector = manifest.connector
    _require(
        connector in policy.enabled_connectors,
        DenyCode.CONNECTOR_DISABLED,
        f"{connector} isn't enabled for this tenant",
    )
    _require(
        request.connector == connector and request.manifest_version == manifest.version,
        DenyCode.MANIFEST_MISMATCH,
        "the request names a different connector or manifest version",
    )
    action = _present(
        manifest.actions.get(request.action),
        DenyCode.UNKNOWN_ACTION,
        f"{request.action} isn't in the manifest",
    )
    _require(
        action.kind is not ActionKind.SECURITY,
        DenyCode.SECURITY_CLASS,
        "security and access changes are refused in the beta (SEC-16)",
    )
    _require(
        not read_only or action.kind is ActionKind.READ,
        DenyCode.READ_ONLY,
        "a dry run reads but changes nothing (ASG-05)",
    )
    _require(
        action.kind not in policy.disabled_kinds.get(connector, frozenset())
        and request.action not in policy.disabled_actions.get(connector, frozenset()),
        DenyCode.DISABLED_BY_POLICY,
        f"an admin turned off {request.action}",
    )

    # A compensation runs under the grant of the write it reverses (CON-02, RUN-06).
    granted_action = request.compensating.action if request.compensating else request.action
    grant = _present(
        next((g for g in grants if g.connector == connector and g.action == granted_action), None),
        DenyCode.NOT_GRANTED,
        f"the task wasn't granted {granted_action}",
    )
    _require(
        grant.manifest_version == manifest.version,
        DenyCode.MANIFEST_MISMATCH,
        "the grant pins a different manifest version",
    )

    missing = action.scopes - consented_scopes
    _require(
        not missing,
        DenyCode.SCOPE_NOT_CONSENTED,
        f"the person hasn't consented to {sorted(missing)}",
    )
    allowed_scopes = policy.allowed_scopes.get(connector)
    _require(
        allowed_scopes is None or action.scopes <= allowed_scopes,
        DenyCode.SCOPE_NOT_ALLOWED,
        "an admin narrowed this connector's scopes below what the action needs",
    )
    _require(
        action.cost is not None,
        DenyCode.NO_COST_MODEL,
        f"{request.action} declares no cost model (BUD-03)",
    )

    known = set(action.recipient_params) | set(action.other_params)
    if action.write_param:
        known.add(action.write_param)
    unknown = set(request.params) - known
    _require(not unknown, DenyCode.UNKNOWN_PARAM, f"unknown params {sorted(unknown)}")

    per_call_approval = _check_resources(action, grant, request)
    _check_recipients(action, grant, request)
    params = dict(request.params)

    if request.compensating is not None:
        params = _check_compensation(manifest, action, request)
    elif action.write_param is not None:
        params[action.write_param] = _check_writes(action, action.write_param, grant, request)

    if request.compensating is None and grant.max_records_per_run is not None:
        _require(
            request.volume_used + request.record_count <= grant.max_records_per_run,
            DenyCode.VOLUME_EXCEEDED,
            f"at most {grant.max_records_per_run} records per run",
        )

    return Allow(
        params=params,
        impact=impact_tier(action, request.record_count),
        per_call_approval=per_call_approval,
        counts_toward_volume=request.compensating is None,
    )


def _check_resources(action: ActionSpec, grant: CapabilityGrant, request: CallRequest) -> bool:
    """Every resource param must resolve to a granted stable ID. Returns per-call approval."""
    extra = set(request.resources) - set(action.resource_params)
    _require(not extra, DenyCode.UNKNOWN_PARAM, f"unknown resource params {sorted(extra)}")
    if not action.resource_determinable:
        return True
    for param in action.resource_params:
        resolved = request.resources.get(param)
        _require(
            resolved is not None,
            DenyCode.RESOURCE_UNRESOLVED,
            f"{param} wasn't resolved to a stable ID before the call",
        )
        _require(
            resolved in grant.resources.get(param, frozenset()),
            DenyCode.RESOURCE_NOT_GRANTED,
            f"{param} {resolved} is outside the granted resources",
        )
    return False


def _normalize_address(address: str) -> str:
    return address.strip().casefold()


def _addresses(value: object) -> list[str] | None:
    """A recipient param as a list of addresses, or None if it isn't one."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        items = cast(list[object], value)
        if all(isinstance(item, str) for item in items):
            return cast(list[str], items)
    return None


def _check_recipients(action: ActionSpec, grant: CapabilityGrant, request: CallRequest) -> None:
    recipients: list[str] = []
    for param in action.recipient_params:
        recipients += _present(
            _addresses(request.params.get(param, [])),
            DenyCode.RECIPIENT_NOT_ALLOWED,
            f"{param} must be addresses",
        )
    if not action.recipient_params:
        return
    _require(
        grant.recipient_rule == "original_sender_only",
        DenyCode.RECIPIENT_NOT_ALLOWED,
        "the grant allows no recipients",
    )
    sender = _normalize_address(
        _present(
            request.original_sender,
            DenyCode.RECIPIENT_NOT_ALLOWED,
            "the original sender's identity isn't verified (ASG-15)",
        )
    )
    _require(
        bool(recipients) and {_normalize_address(r) for r in recipients} == {sender},
        DenyCode.RECIPIENT_NOT_ALLOWED,
        "replies may go only to the original sender",
    )


def _check_writes(
    action: ActionSpec, write_param: str, grant: CapabilityGrant, request: CallRequest
) -> dict[str, Any]:
    raw = request.params.get(write_param)
    if not isinstance(raw, dict) or not raw:
        raise _Denied(DenyCode.INVALID_VALUE, f"{write_param} must map fields to values")
    writes = cast(dict[str, Any], raw)
    typed: dict[str, Any] = {}
    for name, value in writes.items():
        spec = _present(
            action.fields.get(name) if name in grant.write_fields else None,
            DenyCode.FIELD_NOT_WRITABLE,
            f"{name} isn't a writable field",
        )
        try:
            typed[name] = typed_value(spec, value)
        except InvalidValueError as error:
            raise _Denied(DenyCode.INVALID_VALUE, f"{name}: {error}") from error
        limit = grant.limits.get(name)
        if limit is not None:
            _check_limit(name, limit, value, request.current_values)
    return typed


def _check_limit(name: str, limit: FieldLimit, value: Any, current: Mapping[str, Any]) -> None:
    try:
        new = as_number(value)
    except InvalidValueError as error:
        raise _Denied(
            DenyCode.INVALID_VALUE, f"{name}: a limited field must be a number"
        ) from error
    _require(limit.min is None or new >= limit.min, DenyCode.LIMIT_EXCEEDED, f"{name} below min")
    _require(limit.max is None or new <= limit.max, DenyCode.LIMIT_EXCEEDED, f"{name} above max")
    if limit.max_change is not None:
        _require(
            name in current,
            DenyCode.CURRENT_VALUE_UNKNOWN,
            f"{name}'s current value must be read before a bounded change",
        )
        try:
            old = as_number(current[name])
        except InvalidValueError as error:
            raise _Denied(DenyCode.CURRENT_VALUE_UNKNOWN, f"{name}: {error}") from error
        _require(
            abs(new - old) <= limit.max_change,
            DenyCode.LIMIT_EXCEEDED,
            f"{name} may change by at most {limit.max_change}",
        )


def _check_compensation(
    manifest: ConnectorManifest, action: ActionSpec, request: CallRequest
) -> dict[str, Any]:
    """A compensation reverses one applied effect, restoring exactly its recorded values."""
    effect = _present(request.compensating, DenyCode.COMPENSATION_INVALID, "no effect named")
    original = manifest.actions.get(effect.action)
    _require(
        original is not None and original.compensation == request.action,
        DenyCode.COMPENSATION_INVALID,
        f"{request.action} isn't the declared compensation for {effect.action}",
    )
    _require(not effect.reversed, DenyCode.COMPENSATION_INVALID, "the effect was already reversed")
    _require(
        request.resources == effect.resources,
        DenyCode.COMPENSATION_INVALID,
        "a compensation must target exactly the resources its effect changed",
    )
    params = dict(request.params)
    if action.write_param is not None:
        _require(
            request.params.get(action.write_param) == effect.prior_values,
            DenyCode.COMPENSATION_INVALID,
            "a compensation may only restore the values recorded when the effect ran",
        )
        typed: dict[str, Any] = {}
        for name, value in effect.prior_values.items():
            spec = _present(
                action.fields.get(name), DenyCode.COMPENSATION_INVALID, f"{name} isn't a field"
            )
            try:
                typed[name] = typed_value(spec, value)
            except InvalidValueError as error:
                raise _Denied(DenyCode.INVALID_VALUE, f"{name}: {error}") from error
        params[action.write_param] = typed
    return params


@dataclass(frozen=True, slots=True)
class Record:
    """One item of a read response, as an adapter normalizes it."""

    resources: Mapping[str, str]
    fields: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class Filtered:
    records: list[Record]
    dropped: int = 0  # items outside the granted resources; worth an audit note
    hidden_fields: frozenset[str] = field(default_factory=frozenset[str])


def filter_response(
    action: ActionSpec, grant: CapabilityGrant, records: Iterable[Record]
) -> Filtered:
    """Keep only granted resources, and only granted fields of them (CON-02, read side)."""
    kept: list[Record] = []
    dropped = 0
    hidden: set[str] = set()
    readable = grant.read_fields & set(action.fields)
    for record in records:
        # A record must name exactly the action's resources, each one granted.
        if set(record.resources) != set(action.resource_params) or any(
            resource not in grant.resources.get(param, frozenset())
            for param, resource in record.resources.items()
        ):
            dropped += 1
            continue
        hidden |= set(record.fields) - readable
        kept.append(
            Record(
                record.resources,
                {name: value for name, value in record.fields.items() if name in readable},
            )
        )
    return Filtered(kept, dropped, frozenset(hidden))
