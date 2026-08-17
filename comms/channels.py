"""Delivery channels for a notification.

The architecture section of the brief asks for in-app, email, push and SMS.
Roshada has infrastructure for exactly one of them: it stores rows in its own
database. There is no email backend configured, no push service and no SMS
provider, so those three are **declared and unregistered** — naming them
without shipping a provider that does not exist.

A channel is ``fn(notification) -> None``. Registering one is how a future
email or push integration is added: one registration here, nothing changed at
any call site.

    @channels.register(channels.EMAIL)
    def send_email(notification): ...
"""
import logging

IN_APP = "in_app"
EMAIL = "email"
PUSH = "push"
SMS = "sms"

#: Every channel the platform has a name for. Being listed here is not a claim
#: that it works — ``available()`` answers that.
ALL = (IN_APP, EMAIL, PUSH, SMS)

LABELS = {
    IN_APP: "In-app",
    EMAIL: "Email",
    PUSH: "Push notification",
    SMS: "SMS",
}

logger = logging.getLogger("appointments")

#: channel name -> the callable that delivers through it.
_DELIVERY = {}


def register(channel):
    """Register the backend for a channel. Used as a decorator."""
    def wrap(callback):
        _DELIVERY[channel] = callback
        return callback
    return wrap


def available():
    """The channels that actually have a backend behind them right now."""
    return tuple(name for name in ALL if name in _DELIVERY)


def deliver(notification, wanted=None):
    """Push one notification through every registered channel.

    Unregistered channels are skipped silently — that is the honest behaviour
    for "email is not configured", as opposed to recording a delivery that did
    not happen. A channel that raises is logged and does not stop the others,
    and never propagates: a notification is a side effect of some clinical
    action, and failing to deliver one must not fail the action.
    """
    for name in (wanted or ALL):
        callback = _DELIVERY.get(name)
        if callback is None:
            continue
        try:
            callback(notification)
        except Exception:                                   # noqa: BLE001
            logger.exception("Notification channel %s failed for %s",
                             name, notification.pk)


# ---------------------------------------------------------------------------
# The one channel that exists.
#
# In-app delivery is the database row itself, which ``notify`` has already
# written by the time this runs — so this is a no-op that exists to keep the
# registry honest: ``available()`` reports in_app because in_app genuinely
# works, and it would be wrong for that list to be empty.
# ---------------------------------------------------------------------------
@register(IN_APP)
def _in_app(notification):
    return None
