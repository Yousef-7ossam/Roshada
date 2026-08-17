"""Output shapes for notifications and messaging.

The notification serializer is deliberately small. A notification is a pointer:
what happened, when, and where to look. Anything clinical stays in the module
that owns it, behind that module's permission check — so there is no field here
for a result, an impression, a medication list or a message body.
"""
from rest_framework import serializers

from . import types
from .models import Conversation, Message, Notification


class NotificationSerializer(serializers.ModelSerializer):
    type_label = serializers.SerializerMethodField()
    category = serializers.SerializerMethodField()
    is_read = serializers.BooleanField(read_only=True)

    class Meta:
        model = Notification
        fields = ["id", "type", "type_label", "category", "title", "body",
                  "source", "reference", "is_read", "read_at", "created_at"]

    def get_type_label(self, obj):
        return types.label(obj.type)

    def get_category(self, obj):
        return types.category_of(obj.type)


class MessageSerializer(serializers.ModelSerializer):
    sender_name = serializers.SerializerMethodField()
    is_mine = serializers.SerializerMethodField()
    is_read = serializers.BooleanField(read_only=True)

    class Meta:
        model = Message
        fields = ["id", "sender", "sender_name", "is_mine", "body", "is_read",
                  "read_at", "created_at"]

    def get_sender_name(self, obj):
        from .notifications import display_name
        return display_name(obj.sender)

    def get_is_mine(self, obj):
        viewer = self.context.get("viewer")
        return viewer is not None and obj.sender_id == viewer.pk


class ConversationSerializer(serializers.ModelSerializer):
    counterparty = serializers.SerializerMethodField()
    counterparty_role = serializers.SerializerMethodField()
    unread = serializers.SerializerMethodField()
    last_message = serializers.SerializerMethodField()

    class Meta:
        model = Conversation
        fields = ["id", "subject", "is_active", "counterparty",
                  "counterparty_role", "unread", "last_message",
                  "last_message_at", "created_at"]

    def _other(self, obj):
        return obj.counterparty(self.context.get("viewer"))

    def get_counterparty(self, obj):
        from .notifications import display_name
        return display_name(self._other(obj))

    def get_counterparty_role(self, obj):
        from accounts.services import role_of
        other = self._other(obj)
        return role_of(other) if other is not None else None

    def get_unread(self, obj):
        # Precomputed in one grouped query by the view; never a query per row.
        return self.context.get("unread", {}).get(obj.id, 0)

    def get_last_message(self, obj):
        """A short preview, for the conversation list only.

        This is the one place a message's text is abbreviated into a list —
        and it is shown *inside* the conversation list, which already requires
        being a participant. Notifications never carry it.
        """
        preview = self.context.get("previews", {}).get(obj.id)
        return preview[:80] if preview else ""


class SendMessageSerializer(serializers.Serializer):
    body = serializers.CharField(max_length=4000, allow_blank=False,
                                 trim_whitespace=True)


class StartConversationSerializer(serializers.Serializer):
    user_id = serializers.IntegerField()
    subject = serializers.CharField(max_length=160, required=False,
                                    allow_blank=True, default="")
