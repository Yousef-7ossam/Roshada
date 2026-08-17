"""Communication routes.

Included under the same ``/api/`` prefix as the rest of the platform, following
the existing convention: nouns, trailing slashes, actions as a sub-path, and
the caller's own resources reachable without an id in the URL.
"""
from django.urls import path

from .views import (
    Contacts, ConversationMessages, ConversationRead, Conversations,
    MarkAllRead, NotificationRead, NotificationTypes, Notifications,
    UnreadCount,
)

app_name = "comms"

urlpatterns = [
    # ---- Notifications ----
    path("notifications/", Notifications.as_view(), name="notifications"),
    path("notifications/types/", NotificationTypes.as_view(), name="types"),
    path("notifications/unread/", UnreadCount.as_view(), name="unread"),
    path("notifications/read-all/", MarkAllRead.as_view(), name="read-all"),
    path("notifications/<int:notification_id>/read/",
         NotificationRead.as_view(), name="notification-read"),

    # ---- Messaging ----
    path("conversations/", Conversations.as_view(), name="conversations"),
    path("conversations/contacts/", Contacts.as_view(), name="contacts"),
    path("conversations/<int:conversation_id>/messages/",
         ConversationMessages.as_view(), name="conversation-messages"),
    path("conversations/<int:conversation_id>/read/",
         ConversationRead.as_view(), name="conversation-read"),
]
