"""Consistent error responses + DRF exception handling.

Every error the API returns has the shape::

    {"error": "<human message>", "code": "<machine code>", "details": {...}?}

Success responses keep their existing raw shapes (they are part of the client
contract). ``api_error`` is used for explicit errors in views; the exception
handler reshapes DRF/uncaught exceptions into the same envelope and logs them.
"""
import logging

from rest_framework import status as http_status
from rest_framework.response import Response
from rest_framework.views import exception_handler

logger = logging.getLogger("appointments")

_STATUS_CODE = {
    400: "invalid",
    401: "not_authenticated",
    403: "permission_denied",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    429: "throttled",
    500: "server_error",
    503: "service_unavailable",
}


def code_for(status_code):
    return _STATUS_CODE.get(status_code, "error")


def api_error(message, status_code=http_status.HTTP_400_BAD_REQUEST, code=None, details=None):
    """Build a standardized error Response."""
    payload = {"error": message, "code": code or code_for(status_code)}
    if details:
        payload["details"] = details
    return Response(payload, status=status_code)


def _extract(data):
    """Reduce a DRF error body to (message, details)."""
    if isinstance(data, dict):
        if "detail" in data:
            return str(data["detail"]), None
        # field errors: {"username": ["msg", ...], ...}
        for value in data.values():
            if isinstance(value, (list, tuple)) and value:
                return str(value[0]), data
            return str(value), data
        return "Invalid request.", None
    if isinstance(data, (list, tuple)) and data:
        return str(data[0]), None
    return str(data), None


def custom_exception_handler(exc, context):
    response = exception_handler(exc, context)
    view = context.get("view")
    view_name = view.__class__.__name__ if view else "unknown"

    if response is not None:
        message, details = _extract(response.data)
        payload = {"error": message, "code": code_for(response.status_code)}
        if details:
            payload["details"] = details
        response.data = payload
        logger.warning("API %s in %s: %s", response.status_code, view_name, message)
        return response

    # DRF did not handle it -> unexpected 500. Log full traceback.
    logger.exception("Unhandled exception in %s", view_name)
    return api_error("Internal server error.",
                     http_status.HTTP_500_INTERNAL_SERVER_ERROR,
                     code="server_error")
