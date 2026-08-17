# ROSHDA — shared image for both the Django API and the Streamlit UI.
# The two services run from the same image with different commands (see
# docker-compose.yml). Tesseract + OpenCV system libs are installed for OCR.
FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# System dependencies: OpenCV runtime, Tesseract OCR (+ Arabic language data).
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 libglib2.0-0 \
        tesseract-ocr tesseract-ocr-ara \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first for better layer caching.
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY . .

# Tesseract lives on PATH inside the image.
ENV TESSERACT_CMD=/usr/bin/tesseract

# Create a non-root user.
RUN useradd --create-home appuser && chown -R appuser /app
USER appuser

EXPOSE 8000 8501

# Default command runs the API; the UI service overrides this in compose.
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3"]
