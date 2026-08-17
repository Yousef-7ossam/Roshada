"""Notification and messaging endpoints.

Thin like the rest of the API: parse, delegate to the service, translate the
domain exception into the project's error envelope. Every list is built from a
queryset the service scoped to ``request.user`` — no view filters by an id
taken from the request, which is what makes an IDOR attempt land on an empty
queryset rather than on somebody's data.
"""
import logging

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from appointments.exceptions import api_error

from . import channels, messaging, notifications, types
from .serializers import (
    ConversationSerializer, MessageSerializer, NotificationSerializer,
    SendMessageSerializer, StartConversationSerializer,
)

logger = logging.getLogger("appointments")

MAX_LIMIT = 100
DEFAULT_LIMIT = 20


class _CommsView(APIView):
    permission_classes = [IsAuthenticated]

    def handle_exception(self, exc):
        # 404 rather than 403 for something the caller may not see: a 403 would
        # confirm the conversation exists.
        if isinstance(exc, messaging.NotFound):
            return api_error("Not found.", status.HTTP_404_NOT_FOUND)
        if isinstance(exc, messaging.NotAuthorized):
            return api_error(str(exc) or "Not permitted.",
                             status.HTTP_403_FORBIDDEN)
        if isinstance(exc, messaging.InvalidMessage):
            return api_error(str(exc), status.HTTP_400_BAD_REQUEST)
        return super().handle_exception(exc)


def _paging(request):
    try:
        limit = int(request.query_params.get("limit") or DEFAULT_LIMIT)
        offset = int(request.query_params.get("offset") or 0)
    except (TypeError, ValueError):
        raise ValueError("limit and offset must be whole numbers.")
    return max(min(limit, MAX_LIMIT), 1), max(offset, 0)


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------
class NotificationTypes(APIView):
    """The vocabulary and its filter categories, so no client hardcodes them."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response({
            "categories": [
                {"value": category, "label": types.category_label(category)}
                for category in types.CATEGORIES],
            "types": [
                {"value": value, "label": types.label(value),
                 "category": types.category_of(value),
                 # Honest about what nothing can raise yet, rather than
                 # offering a filter that can only ever return nothing.
                 "available": value not in types.UNPRODUCIBLE}
                for value in types.ALL],
            # Declared channels, and which of them actually have a backend.
            "channels": [
                {"value": name, "label": channels.LABELS[name],
                 "enabled": name in channels.available()}
                for name in channels.ALL],
        }, status=status.HTTP_200_OK)


class Notifications(_CommsView):
    """The caller's own notifications, paginated and filterable."""

    def get(self, request):
        try:
            limit, offset = _paging(request)
        except ValueError as exc:
            return api_error(str(exc), status.HTTP_400_BAD_REQUEST)

        category = request.query_params.get("category")
        if category and category not in types.CATEGORIES:
            return api_error(f"Unknown category {category!r}.",
                             status.HTTP_400_BAD_REQUEST)
        notification_type = request.query_params.get("type")
        if notification_type and not types.is_valid(notification_type):
            return api_error(f"Unknown notification type "
                             f"{notification_type!r}.",
                             status.HTTP_400_BAD_REQUEST)

        unread_only = request.query_params.get("unread") == "true"
        queryset = notifications.for_user(
            request.user, category=category,
            notification_type=notification_type, unread_only=unread_only)

        total = queryset.count()
        page = list(queryset[offset:offset + limit])
        return Response({
            "results": NotificationSerializer(page, many=True).data,
            "count": len(page),
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(page) < total,
            "unread": notifications.unread_count(request.user),
        }, status=status.HTTP_200_OK)


class UnreadCount(_CommsView):
    """The badge. One indexed COUNT, plus the per-tab breakdown."""

    def get(self, request):
        return Response({
            "unread": notifications.unread_count(request.user),
            "by_category": notifications.unread_by_category(request.user),
            "unread_messages": messaging.unread_message_count(request.user),
        }, status=status.HTTP_200_OK)


class NotificationRead(_CommsView):
    """Mark one of the caller's own notifications read (or unread again)."""

    def post(self, request, notification_id):
        read = request.data.get("read", True)
        updated = notifications.mark_read(request.user, notification_id,
                                          read=bool(read))
        if updated is None:
            # Scoped by recipient, so somebody else's id is simply not found.
            return api_error("Not found.", status.HTTP_404_NOT_FOUND)
        return Response(NotificationSerializer(updated).data,
                        status=status.HTTP_200_OK)


class MarkAllRead(_CommsView):
    def post(self, request):
        category = request.data.get("category")
        if category and category not in types.CATEGORIES:
            return api_error(f"Unknown category {category!r}.",
                             status.HTTP_400_BAD_REQUEST)
        updated = notifications.mark_all_read(request.user, category=category)
        return Response({"marked_read": updated,
                         "unread": notifications.unread_count(request.user)},
                        status=status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# Messaging
# ---------------------------------------------------------------------------
class Conversations(_CommsView):
    """GET the caller's threads · POST open one with an authorized person."""

    def get(self, request):
        queryset = list(messaging.conversations_for(request.user))
        unread = messaging.unread_counts(request.user)
        previews = self._previews(queryset)
        return Response(ConversationSerializer(
            queryset, many=True,
            context={"viewer": request.user, "unread": unread,
                     "previews": previews}).data,
            status=status.HTTP_200_OK)

    def _previews(self, conversations):
        """Last message per thread, in one query rather than one per row."""
        from .models import Message

        previews = {}
        latest = (Message.objects
                  .filter(conversation__in=[c.id for c in conversations])
                  .order_by("conversation_id", "-created_at")
                  .values("conversation_id", "body"))
        for row in latest:
            previews.setdefault(row["conversation_id"], row["body"])
        return previews

    def post(self, request):
        serializer = StartConversationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        conversation, created = messaging.start_conversation(
            request.user, serializer.validated_data["user_id"],
            serializer.validated_data.get("subject", ""))
        return Response(
            ConversationSerializer(
                conversation, context={"viewer": request.user}).data,
            status=(status.HTTP_201_CREATED if created else status.HTTP_200_OK))


class Contacts(_CommsView):
    """Who the caller may open a conversation with.

    Built from the same care relationship the backend enforces, so the list a
    client offers and the rule the server applies cannot disagree.
    """

    def get(self, request):
        from accounts.services import role_of
        return Response([
            {"id": person.id,
             "name": notifications.display_name(person),
             "username": person.username,
             "role": role_of(person)}
            for person in messaging.contacts_for(request.user)
        ], status=status.HTTP_200_OK)


class ConversationMessages(_CommsView):
    """GET a thread's messages · POST a new one."""

    def get(self, request, conversation_id):
        try:
            limit, offset = _paging(request)
        except ValueError as exc:
            return api_error(str(exc), status.HTTP_400_BAD_REQUEST)

        conversation, page, total, has_more = messaging.messages_in(
            request.user, conversation_id, limit=limit, offset=offset)
        return Response({
            "conversation": ConversationSerializer(
                conversation, context={"viewer": request.user}).data,
            "results": MessageSerializer(
                page, many=True, context={"viewer": request.user}).data,
            "count": len(page),
            "total": total,
            "has_more": has_more,
        }, status=status.HTTP_200_OK)

    def post(self, request, conversation_id):
        serializer = SendMessageSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        message = messaging.send_message(request.user, conversation_id,
                                         serializer.validated_data["body"])
        return Response(
            MessageSerializer(message, context={"viewer": request.user}).data,
            status=status.HTTP_201_CREATED)


class ConversationRead(_CommsView):
    """Mark the other party's messages in this thread as read."""

    def post(self, request, conversation_id):
        updated = messaging.mark_conversation_read(request.user,
                                                   conversation_id)
        return Response({"marked_read": updated},
                        status=status.HTTP_200_OK)
