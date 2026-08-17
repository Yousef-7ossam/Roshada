"""Patient↔doctor messaging use-cases.

Every read is scoped to the caller and every write checks membership on the
row, so a conversation id taken from a URL can only ever address a thread the
caller is in. That is the whole rule, and it is enforced here rather
than in the view — a second view that forgot the check would still get nothing.

A conversation may only exist where the platform's **existing care
relationship** does: an appointment between the two people, the same rule that
gates medical-record access, imaging orders and prescribing. Reused rather than
restated, so messaging cannot drift into a looser definition of "my doctor"
than the rest of the product uses — and so this never becomes an open chat
system.
"""
import logging

from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.utils import timezone

from accounts import roles
from accounts.services import role_of
from appointments.services import care

from . import notifications, types
from .models import Conversation, Message

logger = logging.getLogger("appointments")


class NotFound(Exception):
    """No such conversation — also raised instead of a permission error where
    telling the two apart would confirm that an id exists."""


class NotAuthorized(Exception):
    """The caller may not do this."""


class InvalidMessage(Exception):
    """The message cannot be sent as given."""


_CONVERSATION_RELATED = ("patient", "doctor", "doctor__doctor_profile")


def conversations_for(user):
    """Threads this user is in. Never anybody else's."""
    role = role_of(user)
    queryset = Conversation.objects.select_related(*_CONVERSATION_RELATED)
    if role == roles.PATIENT:
        return queryset.filter(patient=user)
    if role == roles.DOCTOR:
        return queryset.filter(doctor=user)
    # Facilities and administrators have no messaging surface: the product's
    # messaging is patient↔doctor, and an empty queryset is the honest answer
    # rather than a partial one.
    return Conversation.objects.none()


def get_conversation(user, conversation_id):
    conversation = conversations_for(user).filter(pk=conversation_id).first()
    if conversation is None:
        raise NotFound()
    return conversation


def start_conversation(user, other_user_id, subject=""):
    """Open (or reopen) the thread between a patient and a doctor.

    Idempotent: the pair is unique, so "start" on an existing thread returns
    it instead of creating a second one that would hold half the history.
    """
    role = role_of(user)
    if role not in (roles.PATIENT, roles.DOCTOR):
        raise NotAuthorized("Only patients and doctors can use messaging.")

    try:
        other = User.objects.select_related("account").get(pk=int(other_user_id))
    except (User.DoesNotExist, ValueError, TypeError):
        raise NotFound()

    patient, doctor = ((user, other) if role == roles.PATIENT
                       else (other, user))
    if role_of(patient) != roles.PATIENT or role_of(doctor) != roles.DOCTOR:
        raise NotFound()

    # The care relationship is the authorization. Without it there is no
    # conversation to have.
    if not care.treats_patient(doctor, patient):
        raise NotAuthorized(
            "You can only message a doctor you have an appointment with."
            if role == roles.PATIENT else
            "You can only message patients you have appointments with.")

    try:
        with transaction.atomic():
            conversation, created = Conversation.objects.get_or_create(
                patient=patient, doctor=doctor,
                defaults={"subject": subject or ""})
    except IntegrityError:
        # Lost a race against a concurrent "start"; the winner's row is the
        # one everybody uses.
        conversation = Conversation.objects.get(patient=patient, doctor=doctor)
        created = False

    if not conversation.is_active:
        conversation.is_active = True
        conversation.save(update_fields=["is_active", "updated_at"])
    return conversation, created


def contacts_for(user):
    """Who this user may open a conversation with.

    Built from the same care relationship the backend enforces, so the list a
    client shows and the rule the server applies cannot disagree.
    """
    from appointments.models import Appointment

    role = role_of(user)
    if role == roles.PATIENT:
        ids = (Appointment.objects.filter(patient=user)
               .values_list("provider_id", flat=True).distinct())
        candidates = User.objects.filter(pk__in=ids, account__role=roles.DOCTOR)
    elif role == roles.DOCTOR:
        ids = (Appointment.objects.filter(provider=user)
               .values_list("patient_id", flat=True).distinct())
        candidates = User.objects.filter(pk__in=ids).exclude(
            account__role__in=[r for r in roles.ALL_ROLES if r != roles.PATIENT])
    else:
        return User.objects.none()
    return candidates.select_related("account", "doctor_profile").order_by(
        "first_name", "username")


def messages_in(user, conversation_id, limit=50, offset=0):
    """One thread's messages, newest page first but returned in reading order.

    Paginated from the *end*: opening a conversation should show the latest
    exchange, not its first page from months ago.
    """
    conversation = get_conversation(user, conversation_id)
    queryset = (Message.objects.filter(conversation=conversation)
                .select_related("sender"))
    total = queryset.count()
    # Walk backwards from the newest, then flip so the client renders top-down.
    start = max(total - offset - limit, 0)
    end = max(total - offset, 0)
    page = list(queryset[start:end])
    has_more = start > 0
    return conversation, page, total, has_more


def send_message(user, conversation_id, body):
    """Post a message, and tell the other person about it.

    The notification carries the sender's name and nothing else — never the
    message text. A notification is delivered to a bell that may be read over
    someone's shoulder; the content stays behind the conversation, which
    requires being a participant.
    """
    body = (body or "").strip()
    if not body:
        raise InvalidMessage("A message cannot be empty.")
    if len(body) > 4000:
        raise InvalidMessage("That message is too long (4000 characters max).")

    conversation = get_conversation(user, conversation_id)
    if not conversation.includes(user):
        # Belt and braces: the queryset above already guarantees this.
        raise NotFound()
    if not conversation.is_active:
        raise NotAuthorized("This conversation is closed.")

    with transaction.atomic():
        message = Message.objects.create(conversation=conversation,
                                         sender=user, body=body)
        conversation.last_message_at = message.created_at
        conversation.save(update_fields=["last_message_at", "updated_at"])

    recipient = conversation.counterparty(user)
    notifications.notify(
        recipient, types.MESSAGE_RECEIVED, "New message",
        f"You have a new message from {notifications.display_name(user)}.",
        source="comms.Conversation", reference=conversation.id,
        # One unread badge per conversation: a ten-message exchange is one
        # thing to look at, not ten.
        dedupe_unread=True)
    return message


def mark_conversation_read(user, conversation_id):
    """Mark the *other* party's messages as read.

    Only theirs: marking your own messages read would make "read" mean
    nothing, since the sender has obviously seen what they wrote.
    """
    conversation = get_conversation(user, conversation_id)
    updated = (Message.objects
               .filter(conversation=conversation, read_at__isnull=True)
               .exclude(sender=user)
               .update(read_at=timezone.now()))
    # The conversation is open in front of them, so its badge is stale.
    (notifications.for_user(user, notification_type=types.MESSAGE_RECEIVED)
     .filter(source="comms.Conversation", reference=conversation.id,
             read_at__isnull=True)
     .update(read_at=timezone.now()))
    return updated


def unread_counts(user):
    """conversation id -> messages waiting for this user, in one query."""
    from django.db.models import Count

    rows = (Message.objects
            .filter(conversation__in=conversations_for(user),
                    read_at__isnull=True)
            .exclude(sender=user)
            .values("conversation_id")
            .annotate(total=Count("id")))
    return {row["conversation_id"]: row["total"] for row in rows}


def unread_message_count(user):
    return (Message.objects
            .filter(conversation__in=conversations_for(user),
                    read_at__isnull=True)
            .exclude(sender=user).count())
