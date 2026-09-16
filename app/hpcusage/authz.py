"""Authorization hook. Kept deliberately tiny so roles/allowlists can grow here later."""

from .settings import get_settings


def is_allowed(username: str) -> bool:
    allowed = get_settings().allowed_user_set
    return not allowed or username in allowed


def is_admin(username: str) -> bool:
    # Placeholder for a future admin role; today every allowed user is an admin.
    return is_allowed(username)
