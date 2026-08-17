"""Token authentication with an expiry window.

DRF's stock ``Token`` never expires: the value handed out at signup stayed valid
forever, so a leaked token was a permanent credential. That is a poor fit for an
application holding medical data.

This subclass rejects tokens older than ``AUTH_TOKEN_TTL_HOURS`` (default 24).
``services.accounts.login_user`` rotates an expired token on the next successful
login, so the flow for a user is simply "sign in again".

Set ``AUTH_TOKEN_TTL_HOURS=0`` to restore the non-expiring behaviour.
"""
from django.conf import settings
from django.utils import timezone
from rest_framework.authentication import TokenAuthentication
from rest_framework.exceptions import AuthenticationFailed


def token_ttl():
    """Configured token lifetime as a timedelta, or None when expiry is off."""
    hours = getattr(settings, "AUTH_TOKEN_TTL_HOURS", 24)
    if not hours or hours <= 0:
        return None
    return timezone.timedelta(hours=hours)


def is_expired(token):
    """True when the token is older than the configured lifetime."""
    ttl = token_ttl()
    if ttl is None:
        return False
    return timezone.now() - token.created > ttl


class ExpiringTokenAuthentication(TokenAuthentication):
    """TokenAuthentication that also enforces a maximum token age."""

    def authenticate_credentials(self, key):
        user, token = super().authenticate_credentials(key)
        if is_expired(token):
            # Drop it so the value can never be reused, then ask for a re-login.
            token.delete()
            raise AuthenticationFailed("Token has expired. Please sign in again.")
        return user, token
