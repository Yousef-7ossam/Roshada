"""National-ID OCR endpoint (used for pre-signup auto-fill)."""
import logging

from rest_framework import status
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from ..exceptions import api_error
from ..services import ocr
from ..validators import validate_image_upload

logger = logging.getLogger("appointments")


class OCRExtractID(APIView):
    # Public because it is used during pre-signup auto-fill; throttled to
    # mitigate abuse of the unauthenticated upload surface.
    permission_classes = [AllowAny]
    parser_classes = [MultiPartParser, FormParser]
    throttle_scope = 'ocr'

    def post(self, request):
        uploaded = request.FILES.get("file") or request.FILES.get("image")
        # Validate the upload (name, type, size, real image) before touching disk.
        uploaded = validate_image_upload(uploaded)

        try:
            result = ocr.extract_id_from_upload(uploaded)
        except ocr.OCRUnavailable:
            # Missing weights/language packs or a broken pipeline: a deployment
            # fault. Logged with a traceback by the service; the detail (which
            # includes server paths) is deliberately not echoed to the client.
            return api_error(
                "ID scanning is not available right now. You can fill the form "
                "in manually.",
                status.HTTP_503_SERVICE_UNAVAILABLE)
        except ocr.OCRExtractionFailed as exc:
            # The pipeline ran but could not read this image — the caller can
            # act on this, and the message comes from our own code.
            return api_error(str(exc), status.HTTP_400_BAD_REQUEST)

        return Response(result, status=status.HTTP_200_OK)
