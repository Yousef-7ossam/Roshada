"""Input and file-upload validation helpers.

Centralises the security-sensitive validation used by the API views:
usernames, passwords, ages, emails and uploaded image files.
"""
import re

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import validate_email
from rest_framework.exceptions import ValidationError

# Usernames: 3-30 chars, letters/digits/._- only. Prevents header/log injection
# and keeps values safe to render.
USERNAME_RE = re.compile(r'^[A-Za-z0-9._-]{3,30}$')

# Allowed image uploads.
ALLOWED_IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png'}
ALLOWED_IMAGE_CONTENT_TYPES = {'image/jpeg', 'image/png'}
# Formats PIL must actually detect in the file body, whatever the client claims.
ALLOWED_IMAGE_FORMATS = {'JPEG', 'PNG'}
MAX_IMAGE_BYTES = 5 * 1024 * 1024  # 5 MB
# Upper bound on decoded pixels — a small file can expand to gigabytes of RAM
# (decompression bomb), which Image.verify() alone does not prevent.
MAX_IMAGE_PIXELS = 50_000_000

# Allowed Knowledge Base document uploads. Kept in step with
# ``rag.parsing.SUPPORTED_SUFFIXES`` — accepting a format the pipeline cannot
# parse would mean storing files that can never be indexed.
ALLOWED_DOCUMENT_EXTENSIONS = {'.txt', '.text', '.md', '.markdown'}
MAX_DOCUMENT_BYTES = 2 * 1024 * 1024  # 2 MB of prose is a very long document.


def validate_username(username):
    if not username or not USERNAME_RE.match(username):
        raise ValidationError(
            "Username must be 3-30 characters and contain only letters, "
            "digits, dots, underscores or hyphens."
        )
    return username


def validate_password_strength(password, user=None):
    try:
        validate_password(password, user=user)
    except DjangoValidationError as e:
        raise ValidationError(list(e.messages))
    return password


def validate_age(age, default=0):
    if age is None or age == "":
        return default
    try:
        age = int(age)
    except (TypeError, ValueError):
        raise ValidationError("Age must be an integer.")
    if not (0 <= age <= 120):
        raise ValidationError("Age must be between 0 and 120.")
    return age


def validate_optional_email(email):
    if not email:
        return ""
    try:
        validate_email(email)
    except DjangoValidationError:
        raise ValidationError("Invalid email address.")
    return email


def validate_text(value, field, max_length=255):
    """Trim and length-check a short free-text field."""
    if value is None:
        return ""
    value = str(value).strip()
    if len(value) > max_length:
        raise ValidationError(f"{field} must be at most {max_length} characters.")
    return value


def validate_image_upload(uploaded):
    """Validate an uploaded image by size, filename extension, declared content
    type and — authoritatively — the real decoded format.

    Every check is mandatory. They used to be skipped whenever the client simply
    omitted the filename or Content-Type, which meant a caller could bypass them
    by sending raw bytes; only the decode check ever ran in practice.
    """
    import os
    from PIL import Image

    if uploaded is None:
        raise ValidationError("An image file is required.")

    # Size
    size = getattr(uploaded, "size", None)
    if size is not None and size > MAX_IMAGE_BYTES:
        raise ValidationError("Image too large (max 5 MB).")

    # Extension — required, so an unnamed upload cannot skip the check.
    name = getattr(uploaded, "name", "") or ""
    ext = os.path.splitext(name)[1].lower()
    if ext not in ALLOWED_IMAGE_EXTENSIONS:
        raise ValidationError(
            "Only JPG and PNG images are allowed (send the file with its "
            "original .jpg/.jpeg/.png filename)."
        )

    # Declared content type — required and must agree with the allow-list.
    content_type = (getattr(uploaded, "content_type", "") or "").split(";")[0].strip().lower()
    if content_type not in ALLOWED_IMAGE_CONTENT_TYPES:
        raise ValidationError("Unsupported image content type.")

    # Authoritative check: decode the body and confirm the real format and that
    # it will not explode in memory. verify() invalidates the object, so the
    # dimensions are read from the same handle before verifying.
    try:
        uploaded.seek(0)
        image = Image.open(uploaded)
        image_format = (image.format or "").upper()
        width, height = image.size
        image.verify()
    except ValidationError:
        raise
    except Exception:
        raise ValidationError("Uploaded file is not a valid image.")
    finally:
        try:
            uploaded.seek(0)
        except Exception:
            pass

    if image_format not in ALLOWED_IMAGE_FORMATS:
        raise ValidationError("Only JPG and PNG images are allowed.")
    if width * height > MAX_IMAGE_PIXELS:
        raise ValidationError("Image resolution is too large to process.")

    return uploaded


def validate_knowledge_document(uploaded):
    """Validate a Knowledge Base document upload.

    Deliberately narrow, and narrow for the same reason ``rag.parsing`` is: the
    corpus can only be parsed from plain text and Markdown, so accepting a PDF
    here would store a file that ingestion then refuses — a worse experience
    than refusing it at the door with a reason.

    The decode is the authoritative check. An extension and a Content-Type are
    both client claims; whether the bytes are text is not.
    """
    import os

    if uploaded is None:
        raise ValidationError("A document file is required.")

    size = getattr(uploaded, "size", None)
    if size is not None and size > MAX_DOCUMENT_BYTES:
        raise ValidationError("Document too large (max 2 MB).")

    name = getattr(uploaded, "name", "") or ""
    ext = os.path.splitext(name)[1].lower()
    if ext not in ALLOWED_DOCUMENT_EXTENSIONS:
        raise ValidationError(
            "Only .txt and .md documents can be ingested. Roshada has no PDF "
            "or DOCX extraction, so those would be stored but never indexed.")

    try:
        uploaded.seek(0)
        raw = uploaded.read()
    finally:
        try:
            uploaded.seek(0)
        except Exception:
            pass

    if not raw.strip():
        raise ValidationError("That document is empty.")

    # Authoritative: it must actually decode as text. A renamed binary fails
    # here rather than being embedded as mojibake and retrieved as prose.
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise ValidationError(
                "That file is not UTF-8 text. Re-save it as UTF-8 .txt or .md.")

    # A NUL byte is the clearest signal that "text" is really a binary blob
    # that happened to decode.
    if "\x00" in text:
        raise ValidationError("That file is binary, not a text document.")

    return text


def validate_number(value, field, default, minimum, maximum):
    """Parse and bound-check a numeric model-input field."""
    if value is None or value == "":
        value = default
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise ValidationError(f"{field} must be a number.")
    if not (minimum <= value <= maximum):
        raise ValidationError(f"{field} must be between {minimum} and {maximum}.")
    return value
