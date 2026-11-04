"""Extra permissions a super admin can grant (issue #132). Not RBAC: the three roles stay, and a
permission is one named extra a person holds on top of their role.

Adding a permission later is one line in PERMISSIONS (plus the route guard that uses it).
Super admins hold every permission implicitly and never have rows. Only the roles in ASSIGNABLE_ROLES
may hold one: agents only (decision), so an end user is never given admin pages. A grant is a row in
UserPermission; if the person's role later stops being assignable the row is ignored (and removed
when the role is changed on Admin > Users).

Phase 6 (Entra app roles): the check goes through `has_permission` only, so the source can move
later (an Entra app-role claim mapped to a key) without touching the routes.

Who may do what is decided by `has_permission` (the check itself lives in app/visibility.py with
the other access rules) and `require_permission` in app/auth.py.
"""

from dataclasses import dataclass

from app.models import UserRole

MANAGE_REQUEST_TEMPLATES = "manage_request_templates"


@dataclass(frozen=True)
class Permission:
    label: str
    description: str


PERMISSIONS: dict[str, Permission] = {
    MANAGE_REQUEST_TEMPLATES: Permission(
        "Manage request templates",
        "Admin > Request types: the request types, their form questions and sub-request templates.",
    ),
}

ASSIGNABLE_ROLES = (UserRole.AGENT,)
