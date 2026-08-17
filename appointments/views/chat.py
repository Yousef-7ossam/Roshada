"""AI-assistant chat history endpoints.

History is stored server-side and scoped to ``request.user``. It was previously
kept in one shared file on the frontend host with no identity attached, which
exposed every user's medical questions to every other user.

Access is ``CanUseAI`` rather than ``IsAuthenticated``: the assistant answers
from the caller's own clinical context, so it is reachable only by the roles
whose record that is (patient and doctor — see ``accounts.roles.AI_ROLES``).
Hiding the page from the other portals would not be enough; the refusal has to
happen here.
"""
import logging

from rest_framework import status
from rest_framework.parsers import JSONParser
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import CanUseAI

from ..serializers import (
    ChatAskSerializer, ChatExchangeSerializer, ChatMessageSerializer,
)
from ..services import chat
from ..services import ai

logger = logging.getLogger("appointments")

MAX_HISTORY = 200


class ChatHistory(APIView):
    """GET the caller's own history · DELETE to clear it."""
    permission_classes = [CanUseAI]

    def get(self, request):
        try:
            limit = min(int(request.query_params.get("limit", 50)), MAX_HISTORY)
        except (TypeError, ValueError):
            limit = 50
        messages = chat.history(request.user, limit=max(limit, 1))
        return Response(ChatMessageSerializer(messages, many=True).data,
                        status=status.HTTP_200_OK)

    def delete(self, request):
        removed = chat.clear(request.user)
        logger.info("Chat history cleared for %s (%s messages)",
                    request.user.username, removed)
        return Response({"message": "History cleared", "deleted": removed},
                        status=status.HTTP_200_OK)


class ChatExchange(APIView):
    """Append one question/answer pair to the caller's own history."""
    permission_classes = [CanUseAI]
    parser_classes = [JSONParser]

    def post(self, request):
        serializer = ChatExchangeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        question, answer = chat.record_exchange(
            request.user,
            serializer.validated_data["prompt"],
            serializer.validated_data["reply"],
        )
        return Response(ChatMessageSerializer([question, answer], many=True).data,
                        status=status.HTTP_201_CREATED)


class ChatContext(APIView):
    """Recent turns formatted for the LLM, so follow-up questions make sense."""
    permission_classes = [CanUseAI]

    def get(self, request):
        return Response(chat.context_messages(request.user),
                        status=status.HTTP_200_OK)


class ChatAsk(APIView):
    """Ask the assistant a question and get one validated, structured answer.

    This is the assistant. The provider call, the user's context, the prompt and
    the safety checks all live behind this endpoint — the client sends a
    question and renders what comes back. It previously all happened in the
    Streamlit process, which put provider keys on the frontend host and left the
    model unable to see the patient's own record.

    Always 200 when the request itself is valid: a provider outage is reported
    in the payload (``degraded``) rather than as an HTTP error, because the
    exchange is still recorded and an emergency notice may still need to be
    shown.
    """
    permission_classes = [CanUseAI]
    parser_classes = [JSONParser]
    # LLM calls cost real money and are far heavier than a normal read.
    throttle_scope = 'ai'

    def post(self, request):
        serializer = ChatAskSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        result = ai.ask(request.user, serializer.validated_data["message"])
        payload = result.as_dict()
        payload["messages"] = ChatMessageSerializer(result.messages, many=True).data
        return Response(payload, status=status.HTTP_200_OK)


class ChatStatus(APIView):
    """Whether the assistant is available, and which provider/model answers.

    Lets the UI render an honest caption without holding provider credentials.
    """
    permission_classes = [CanUseAI]

    def get(self, request):
        return Response(ai.status(), status=status.HTTP_200_OK)
