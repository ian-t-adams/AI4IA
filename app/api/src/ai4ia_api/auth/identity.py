"""Who counts as an admin, decided in one place.

Deliberately a *leaf* module: it imports only ``config`` and ``base``, never
``dependencies``. Two callers need this predicate and they sit on opposite sides
of an import cycle —

* ``auth.admin`` gates the entitlement API, and imports ``auth.dependencies`` for
  ``get_current_user``.
* ``gateway.priority`` maps a principal to a proxy priority band, and is imported
  *by* ``auth.dependencies``.

Putting the predicate in ``auth.admin`` would make that a cycle; copying it into
``gateway.priority`` would let the two definitions of "admin" drift apart, so
adding an admin could silently grant the entitlement API but not the priority
band (or worse, the reverse). Neither is acceptable for an authorization rule, so
it lives here and both import it.

Membership is *identity* only. Callers that also require a second factor (the
entitlement API requires ``X-Admin-Secret`` under spoofable dev auth) layer that
on top; see ``auth.admin.evaluate_admin``.
"""
from __future__ import annotations

from ..config import Settings
from .base import AuthenticatedUser


def has_admin_role(user: AuthenticatedUser) -> bool:
    """True when the token itself carries an ``admin`` app role."""
    roles = user.claims.get("roles")
    return isinstance(roles, list) and "admin" in roles


def identity_is_admin(user: AuthenticatedUser, settings: Settings) -> bool:
    """True when the principal is an admin by subject, email, or app role.

    This says nothing about whether the *identity* can be trusted — under
    spoofable auth it cannot. Check ``settings.auth_provider_is_spoofable``
    before acting on the result.
    """
    if user.subject in settings.admin_subject_set:
        return True
    if user.email and user.email.lower() in settings.admin_email_set:
        return True
    return has_admin_role(user)
