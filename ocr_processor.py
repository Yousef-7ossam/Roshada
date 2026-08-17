"""Egyptian National-ID OCR pipeline.

Improvements over the original:
- Configuration (tesseract binary, YOLO weights, confidence) comes from the
  environment instead of hard-coded Windows paths.
- Detected fields are cropped **in memory** from the YOLO result boxes, removing
  the previous disk round-trip (rmtree + save_crop + per-class imread) and the
  race condition on the shared ``runs/detect/predict`` directory.
- The YOLO model is loaded once and cached.
"""
import os
from functools import lru_cache

import cv2
import pytesseract
import arabic_reshaper
from bidi.algorithm import get_display
from datetime import datetime
from ultralytics import YOLO

# ---- Configuration (env-overridable) --------------------------------------
pytesseract.pytesseract.tesseract_cmd = os.environ.get(
    "TESSERACT_CMD", r"C:\Program Files\Tesseract-OCR\tesseract.exe")

YOLO_WEIGHTS_PATH = os.environ.get("EGY_ID_WEIGHTS", "egyId_weights.pt")
YOLO_CONF = float(os.environ.get("OCR_CONF", "0.6"))
YOLO_IMGSZ = int(os.environ.get("OCR_IMGSZ", "640"))

REQUIRED_FIELDS = ("firstname", "national_id", "second name")

# Tesseract language packs this pipeline needs. "ara_number_id" is a custom
# traineddata file that must be installed into the Tesseract tessdata directory;
# it does not ship with tesseract-ocr-ara.
REQUIRED_TESSERACT_LANGS = ("ara", "ara_number_id")


class OCRNotConfigured(Exception):
    """A required OCR asset (YOLO weights / language pack) is not installed.

    This is an operator/deployment fault, not a problem with the uploaded image,
    so callers must surface it as "service unavailable" rather than blaming the
    request.
    """

PLACE_OF_BIRTH = {
    '01': "Cairo", '02': "Alexandria", '03': "Port Said", '04': "Suez",
    '11': "Damietta", '12': "Dakahlya", '13': "El Sharkya", '14': "El Kalyobia",
    '15': "Kafr Elsheikh", '16': "El Gharbia", '17': 'El Menofya', '18': "El Behira",
    '19': "El Esmaalyaa", '21': "Giza", '22': "Beni Suef", '23': "Fayium",
    '24': "El Menya", '25': "Assiut", '26': "Sohag", '27': "Qena", '28': "Aswan",
    '29': "Luxor", '31': "Red Sea", '32': "Wadi El-Gadid", '33': "Matrouh",
    '34': "North Sinai", '35': "South Sinai", '88': "Outside Egypt",
}


@lru_cache(maxsize=1)
def get_yolo_model(weights_path=YOLO_WEIGHTS_PATH):
    """Load the YOLO detector once and reuse it across requests."""
    if not os.path.exists(weights_path):
        raise OCRNotConfigured(
            f"YOLO weights not found at '{weights_path}'. Install the National-ID "
            f"detector weights and set EGY_ID_WEIGHTS to their path."
        )
    try:
        return YOLO(weights_path)
    except Exception as exc:
        raise OCRNotConfigured(f"Could not load YOLO weights: {exc}") from exc


def check_configuration():
    """Verify every external OCR asset is installed.

    Raises OCRNotConfigured listing what is missing. Used by the readiness probe
    and before processing an upload, so a deployment gap is reported as such
    instead of as a failed image read.
    """
    missing = []
    if not os.path.exists(YOLO_WEIGHTS_PATH):
        missing.append(f"YOLO weights '{YOLO_WEIGHTS_PATH}' (set EGY_ID_WEIGHTS)")

    try:
        installed = set(pytesseract.get_languages(config=""))
    except Exception as exc:
        raise OCRNotConfigured(
            f"Tesseract is not usable at '{pytesseract.pytesseract.tesseract_cmd}' "
            f"({exc}). Install Tesseract and set TESSERACT_CMD."
        ) from exc

    for lang in REQUIRED_TESSERACT_LANGS:
        if lang not in installed:
            missing.append(f"Tesseract language pack '{lang}.traineddata'")

    if missing:
        raise OCRNotConfigured("Missing OCR assets: " + "; ".join(missing))


# ---- Image preprocessing helpers ------------------------------------------
def arabic_to_english(text):
    if text is None:
        return None
    digits = {"٠": "0", "١": "1", "٢": "2", "٣": "3", "٤": "4",
              "٥": "5", "٦": "6", "٧": "7", "٨": "8", "٩": "9"}
    return "".join(digits.get(ch, ch) for ch in text)


def resize(image, flag):
    if flag == "id":
        return cv2.resize(image, (650, 800))
    elif flag == "firstname":
        return cv2.resize(image, (600, 200))
    elif flag == "secondname":
        return cv2.resize(image, (500, 200))
    return image


def processing(image, flag):
    resized = resize(image, flag)
    inverted = cv2.bitwise_not(resized)
    grayed = cv2.cvtColor(inverted, cv2.COLOR_BGR2GRAY)
    _, thresh = cv2.threshold(grayed, 160, 195, cv2.THRESH_BINARY)
    return thresh


# ---- Detection + cropping (in memory) -------------------------------------
def _detect_field_crops(image, uploaded_file_path):
    """Run YOLO and return {class_name: best-confidence crop (ndarray)}."""
    model = get_yolo_model()
    results = model(uploaded_file_path, conf=YOLO_CONF, imgsz=YOLO_IMGSZ,
                    verbose=False, save=False)
    result = results[0]
    names = result.names

    crops, best_conf = {}, {}
    for box in result.boxes:
        cls_name = names[int(box.cls[0])]
        conf = float(box.conf[0])
        if conf <= best_conf.get(cls_name, 0.0):
            continue
        x1, y1, x2, y2 = (int(v) for v in box.xyxy[0])
        crop = image[y1:y2, x1:x2]
        if crop.size:
            crops[cls_name] = crop
            best_conf[cls_name] = conf
    return crops


def run_ocr_on_file(uploaded_file_path):
    # Fail fast and unambiguously when the deployment is incomplete: this used to
    # surface as a generic Exception that the API reported as 400 Bad Request,
    # blaming the user's photo for a missing weights file.
    check_configuration()

    image = cv2.imread(uploaded_file_path)
    if image is None:
        raise ValueError("Could not read the uploaded image.")

    try:
        crops = _detect_field_crops(image, uploaded_file_path)
    except OCRNotConfigured:
        raise
    except Exception as e:
        raise OCRNotConfigured(f"National-ID detector failed to run: {e}") from e

    missing = [f for f in REQUIRED_FIELDS if f not in crops]
    if missing:
        raise FileNotFoundError(f"ID fields not detected: {', '.join(missing)}")

    firstname = pytesseract.image_to_string(
        processing(crops["firstname"], "firstname"), lang="ara").strip()
    secondname = pytesseract.image_to_string(
        processing(crops["second name"], "secondname"), lang="ara").strip()

    id_tokens = pytesseract.image_to_string(
        processing(crops["national_id"], "id"), lang="ara_number_id").split()
    national_id = id_tokens[0] if id_tokens else ""
    if len(national_id) != 14:
        raise ValueError("National ID extraction failed or ID length is incorrect.")

    english_id = arabic_to_english(national_id)
    try:
        century = english_id[0]
        year_born = english_id[1:3]
        governorate = english_id[7:9]
        gender_digit = english_id[12]

        place_of_birth = PLACE_OF_BIRTH.get(governorate, "Unknown")
        birth_year = int(f"{20 if century == '3' else 19}{year_born}")
        age = datetime.now().year - birth_year
        gender = "Male" if int(gender_digit) % 2 != 0 else "Female"
    except Exception as e:
        raise Exception(f"Error during ID data analysis: {e}")

    full_name = (f"{get_display(arabic_reshaper.reshape(firstname))} "
                 f"{get_display(arabic_reshaper.reshape(secondname))}").strip()

    return {
        "name": full_name,
        "age": age,
        "gender": gender,
        "address": place_of_birth,
        "national_id": english_id,
    }
