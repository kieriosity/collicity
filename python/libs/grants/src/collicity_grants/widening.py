"""Widening (RUN-03; technical design §5.2).

``widens(old, new)`` is a subset check on every grant element. Any widening means the new
assignment version needs re-acceptance before it runs, and the acceptance screen shows each
reason this returns. The guarantee, which the tests check: if ``widens`` returns nothing, every
request the new grants allow, the old grants allowed too.
"""

from collections.abc import Sequence
from decimal import Decimal

from collicity_grants.models import CapabilityGrant, FieldLimit


def widens(old: Sequence[CapabilityGrant], new: Sequence[CapabilityGrant]) -> list[str]:
    """Each way ``new`` grants more than ``old``; empty when it grants nothing more."""
    reasons: list[str] = []
    before = {(g.connector, g.action): g for g in old}
    for grant in new:
        key = (grant.connector, grant.action)
        name = f"{grant.connector} {grant.action}"
        prior = before.get(key)
        if prior is None:
            reasons.append(f"{name}: new capability")
            continue
        if grant.manifest_version != prior.manifest_version:
            reasons.append(f"{name}: manifest {prior.manifest_version} -> {grant.manifest_version}")
        for param, ids in grant.resources.items():
            added = ids - prior.resources.get(param, frozenset())
            if added:
                reasons.append(f"{name}: {param} adds {sorted(added)}")
        if added := grant.read_fields - prior.read_fields:
            reasons.append(f"{name}: reads {sorted(added)}")
        if added := grant.write_fields - prior.write_fields:
            reasons.append(f"{name}: writes {sorted(added)}")
        if grant.recipient_rule is not None and prior.recipient_rule is None:
            reasons.append(f"{name}: may now send to {grant.recipient_rule}")
        for field, prior_limit in prior.limits.items():
            limit = grant.limits.get(field)
            if limit is None:
                reasons.append(f"{name}: {field} limit removed")
                continue
            reasons += [f"{name}: {field} {r}" for r in _looser_bounds(prior_limit, limit)]
        if prior.max_records_per_run is not None and (
            grant.max_records_per_run is None
            or grant.max_records_per_run > prior.max_records_per_run
        ):
            reasons.append(
                f"{name}: volume {prior.max_records_per_run} -> {grant.max_records_per_run}"
            )
    return reasons


def _looser_bounds(prior: FieldLimit, limit: FieldLimit) -> list[str]:
    return [
        f"{bound} {old} -> {new}"
        for bound, old, new, looser in (
            ("min", prior.min, limit.min, _lower),
            ("max", prior.max, limit.max, _higher),
            ("max_change", prior.max_change, limit.max_change, _higher),
        )
        if old is not None and looser(old, new)
    ]


def _lower(old: Decimal, new: Decimal | None) -> bool:
    return new is None or new < old


def _higher(old: Decimal, new: Decimal | None) -> bool:
    return new is None or new > old
