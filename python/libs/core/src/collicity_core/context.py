"""The identity a database transaction acts for."""

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class TenantContext:
    """Tenant and acting person for one transaction (SEC-09).

    Build it only from an authenticated identity: a verified sign-in session, a verified webhook,
    or the recorded owner of a run. Never build it from request input such as a path parameter,
    header or body field.

    ``principal_id`` is None only for system work that acts for no person, such as processing a
    directory-sync webhook.
    """

    tenant_id: UUID
    principal_id: UUID | None = None
