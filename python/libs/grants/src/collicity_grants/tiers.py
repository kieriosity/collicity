"""Impact tiers (RUN-04; technical design §5.2)."""

from collicity_grants.models import ActionKind, ActionSpec, ImpactTier, Recovery

BATCH_RECORD_LIMIT = 10


def impact_tier(action: ActionSpec, record_count: int = 1) -> ImpactTier:
    """High when the action can't be undone, destroys, publishes or sends, touches more than
    ten records, or changes security; otherwise low. A restorable overwrite is low.

    Purchases above a threshold and PAY-08's actions join at Home (Phase 3).
    """
    if (
        action.kind in (ActionKind.DESTRUCTIVE, ActionKind.SECURITY)
        or action.recovery is Recovery.IRREVERSIBLE
        or action.publishes
        or record_count > BATCH_RECORD_LIMIT
    ):
        return ImpactTier.HIGH
    return ImpactTier.LOW
