"""National-ID OCR use-case.

Orchestrates the (external) OCR pipeline: persist the upload to a temp file,
run extraction, and always clean up. The heavy OCR module is imported lazily so
a missing OCR dependency never blocks server startup.

Failure taxonomy (mirrors services.predictions):

``OCRUnavailable``  dependencies or assets are missing / the pipeline broke (503)
``OCRExtractionFailed``  the pipeline ran but could not read this image (400)
"""
import logging
import os
import tempfile

logger = logging.getLogger("appointments")


class OCRUnavailable(Exception):
    """Raised when the OCR pipeline/dependencies/assets cannot be loaded."""


class OCRExtractionFailed(Exception):
    """Raised when the pipeline ran but this particular image was unreadable."""


def is_available():
    """True when every OCR dependency and asset is installed (readiness probe)."""
    try:
        import ocr_processor
        ocr_processor.check_configuration()
        return True
    except Exception:
        return False


def extract_id_from_upload(uploaded):
    """Run OCR over an uploaded ID image and return the extracted fields dict."""
    try:
        import ocr_processor
        from ocr_processor import run_ocr_on_file
    except Exception as e:  # dependency/import failure -> pipeline unavailable
        logger.exception("OCR dependencies unavailable")
        raise OCRUnavailable(str(e)) from e

    suffix = os.path.splitext(getattr(uploaded, "name", "upload.jpg"))[1] or ".jpg"
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            for chunk in uploaded.chunks():
                tmp.write(chunk)
            tmp_path = tmp.name

        return run_ocr_on_file(tmp_path)
    except ocr_processor.OCRNotConfigured as e:
        # Missing weights / language packs are a deployment gap, not a bad photo.
        logger.exception("OCR assets not configured")
        raise OCRUnavailable(str(e)) from e
    except (ValueError, FileNotFoundError) as e:
        # The pipeline ran but this image was unusable (unreadable, fields not
        # detected, ID digits malformed) -> the caller can fix it by retrying.
        raise OCRExtractionFailed(str(e)) from e
    except Exception as e:
        logger.exception("OCR pipeline failed unexpectedly")
        raise OCRUnavailable(str(e)) from e
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)
