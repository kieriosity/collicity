"""Typed connector manifests, capability grants, admin policy, call requests and decisions.

Manifests, grants and policy arrive as JSON (signed manifests, accepted assignment versions,
admin settings), so they are validated Pydantic models. All are immutable.
"""

from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer, model_validator

# Sets serialize sorted, so equal grants always produce the same JSON and the same hash.
SortedSet = Annotated[frozenset[str], PlainSerializer(sorted, return_type=list[str])]


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# ---- Connector manifest (CON-12) -------------------------------------------------------------


class ActionKind(StrEnum):
    READ = "read"
    WRITE = "write"
    DESTRUCTIVE = "destructive"
    SECURITY = "security"  # security and access changes; refused in the beta (SEC-16)


class FieldType(StrEnum):
    NUMBER = "number"
    TEXT = "text"
    DATE = "date"
    CHOICE = "choice"


class FieldSpec(_Model):
    type: FieldType
    choices: frozenset[str] = frozenset()

    @model_validator(mode="after")
    def _choices_only_for_choice(self) -> Self:
        if (self.type is FieldType.CHOICE) != bool(self.choices):
            raise ValueError("a choice field, and only a choice field, lists its choices")
        return self


class Recovery(StrEnum):
    COMPENSABLE = "compensable"
    IRREVERSIBLE = "irreversible"


class CostModel(_Model):
    """How the action is billed (BUD-03). ``free`` must be declared, never assumed."""

    free: bool = False
    sku: str | None = None
    meter: str | None = None

    @model_validator(mode="after")
    def _free_or_priced(self) -> Self:
        if self.free == bool(self.sku and self.meter):
            raise ValueError("a cost model is either free or names a sku and meter")
        return self


class ActionSpec(_Model):
    kind: ActionKind
    scopes: frozenset[str]
    # Parameters that identify resources, by resource type. The gateway resolves each to a
    # stable ID before evaluation; they never travel as ordinary params.
    resource_params: dict[str, str] = Field(default_factory=dict)
    # False when the resource can only be known once the call runs; such calls need a per-call
    # approval showing the resolved resource, even for reads (CON-02).
    resource_determinable: bool = True
    recipient_params: tuple[str, ...] = ()
    # The param holding {field: value} for writes.
    write_param: str | None = None
    # Every other param the action accepts. Anything unlisted is refused.
    other_params: frozenset[str] = frozenset()
    fields: dict[str, FieldSpec] = Field(default_factory=dict)
    duplicate_prevention: Literal["a", "b", "c", "d"] | None = None  # RUN-07
    recovery: Recovery | None = None  # RUN-06
    compensation: str | None = None  # the action that reverses this one
    publishes: bool = False  # publishes or sends outside the tenant
    cost: CostModel | None = None


class ConnectorManifest(_Model):
    connector: str
    version: str
    scopes: frozenset[str]
    hosts: frozenset[str]
    actions: dict[str, ActionSpec]

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        for name, action in self.actions.items():
            if not action.scopes <= self.scopes:
                raise ValueError(f"{name} needs scopes the manifest doesn't request")
            if action.kind in (ActionKind.WRITE, ActionKind.DESTRUCTIVE):
                if action.duplicate_prevention is None or action.recovery is None:
                    raise ValueError(f"{name} must declare duplicate prevention and recovery")
                if action.recovery is Recovery.COMPENSABLE and (
                    action.compensation not in self.actions
                ):
                    raise ValueError(f"{name} is compensable but names no known compensation")
            if action.write_param is not None and not action.fields:
                raise ValueError(f"{name} takes writes but declares no fields")
        return self


# ---- Capability grant (CON-02) ---------------------------------------------------------------


class FieldLimit(_Model):
    min: Decimal | None = None
    max: Decimal | None = None
    max_change: Decimal | None = None  # largest allowed |new - current|


class CapabilityGrant(_Model):
    """One capability of a task, as an accepted assignment version declares it."""

    connector: str
    manifest_version: str
    action: str
    # Resource param -> the stable IDs it may name (resolved at acceptance, so a rename can't
    # widen it).
    resources: dict[str, SortedSet] = Field(default_factory=dict)
    read_fields: SortedSet = frozenset()
    write_fields: SortedSet = frozenset()
    # None allows no recipients at all.
    recipient_rule: Literal["original_sender_only"] | None = None
    limits: dict[str, FieldLimit] = Field(default_factory=dict)
    max_records_per_run: int | None = Field(default=None, ge=0)


# ---- Admin policy (ADM-05) -------------------------------------------------------------------


class AdminPolicy(_Model):
    enabled_connectors: frozenset[str]
    disabled_kinds: dict[str, frozenset[ActionKind]] = Field(default_factory=dict)
    disabled_actions: dict[str, frozenset[str]] = Field(default_factory=dict)
    # Where present, the scopes an admin allows for a connector (narrowing the manifest).
    allowed_scopes: dict[str, frozenset[str]] = Field(default_factory=dict)


# ---- Request and decision --------------------------------------------------------------------


class AppliedEffect(_Model):
    """An effect from the run's journal, which a compensation may reverse (RUN-06)."""

    action: str
    resources: dict[str, str]
    prior_values: dict[str, Any]
    reversed: bool = False


class CallRequest(_Model):
    connector: str
    manifest_version: str
    action: str
    params: dict[str, Any] = Field(default_factory=dict)
    # Resource param -> stable ID, resolved by the gateway.
    resources: dict[str, str] = Field(default_factory=dict)
    record_count: int = Field(default=1, ge=0)
    # The run's count so far, for the volume pre-check. The atomic increment happens in the
    # write transaction (§5.1 step 4).
    volume_used: int = Field(default=0, ge=0)
    # The authenticated From address at medium or high identity confidence (ASG-15); None when
    # confidence is lower, so a recipient rule can't be satisfied.
    original_sender: str | None = None
    # The target's current values of the fields being written, read before the call.
    current_values: dict[str, Any] = Field(default_factory=dict)
    compensating: AppliedEffect | None = None


class DenyCode(StrEnum):
    CONNECTOR_DISABLED = "connector_disabled"
    MANIFEST_MISMATCH = "manifest_mismatch"
    UNKNOWN_ACTION = "unknown_action"
    SECURITY_CLASS = "security_class"
    READ_ONLY = "read_only"
    DISABLED_BY_POLICY = "disabled_by_policy"
    NOT_GRANTED = "not_granted"
    SCOPE_NOT_CONSENTED = "scope_not_consented"
    SCOPE_NOT_ALLOWED = "scope_not_allowed"
    NO_COST_MODEL = "no_cost_model"
    UNKNOWN_PARAM = "unknown_param"
    RESOURCE_UNRESOLVED = "resource_unresolved"
    RESOURCE_NOT_GRANTED = "resource_not_granted"
    RECIPIENT_NOT_ALLOWED = "recipient_not_allowed"
    FIELD_NOT_WRITABLE = "field_not_writable"
    INVALID_VALUE = "invalid_value"
    LIMIT_EXCEEDED = "limit_exceeded"
    CURRENT_VALUE_UNKNOWN = "current_value_unknown"
    VOLUME_EXCEEDED = "volume_exceeded"
    COMPENSATION_INVALID = "compensation_invalid"


class ImpactTier(StrEnum):
    LOW = "low"
    HIGH = "high"


class Allow(_Model):
    # The params to send: writes typed and formula-escaped.
    params: dict[str, Any]
    impact: ImpactTier
    per_call_approval: bool = False
    counts_toward_volume: bool = True
    allowed: Literal[True] = True


class Deny(_Model):
    code: DenyCode
    detail: str
    allowed: Literal[False] = False


Decision = Allow | Deny
