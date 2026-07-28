"""OCR + media re-upload forwarding (defeats protected-chat forward restrictions).

Extracted and cleaned from telbot.py/bota.py: download -> OCR-rename -> re-upload
to targets. Keeps the multi-format OCR (image/pdf/video/document) you already wrote.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytesseract
from PIL import Image
from telethon.tl.types import DocumentAttributeFilename

try:
    from pdf2image import convert_from_path
except Exception:  # pragma: no cover - optional heavy dep
    convert_from_path = None
try:
    import cv2
except Exception:  # pragma: no cover
    cv2 = None
try:
    import docx2txt
except Exception:  # pragma: no cover
    docx2txt = None

pytesseract.pytesseract.tesseract_cmd = os.environ.get("TESSERACT_CMD", "/usr/bin/tesseract")


def _suggested_name(text: str, ext: str) -> str:
    return "_".join(text.split()[:5]).replace("/", "_") + ext


def extract_text(file_path: str) -> tuple[str | None, str | None]:
    """Run OCR/extraction by extension. Returns (text, suggested_filename)."""
    ext = Path(file_path).suffix.lower()
    processors = {
        ".png": _process_image, ".jpg": _process_image, ".jpeg": _process_image,
        ".gif": _process_image, ".pdf": _process_pdf, ".mp4": _process_video,
        ".avi": _process_video, ".mov": _process_video, ".mkv": _process_video,
        ".docx": _process_doc, ".doc": _process_doc,
    }
    proc = processors.get(ext)
    if not proc:
        return None, None
    return proc(file_path)


def _process_image(fp: str):
    try:
        img = Image.open(fp)
        if img.mode != "RGB":
            img = img.convert("RGB")
        text = pytesseract.image_to_string(img, config="--oem 3 --psm 6")
        if text.strip():
            return text.strip(), _suggested_name(text, Path(fp).suffix)
    except Exception:
        return None, None
    return None, None


def _process_pdf(fp: str):
    if convert_from_path is None:
        return None, None
    try:
        pages = convert_from_path(fp, first_page=1, last_page=1)
        if pages:
            text = pytesseract.image_to_string(pages[0])
            if text.strip():
                return text.strip(), _suggested_name(text, ".pdf")
    except Exception:
        return None, None
    return None, None


def _process_video(fp: str):
    if cv2 is None:
        return None, None
    try:
        video = cv2.VideoCapture(fp)
        total = int(video.get(cv2.CAP_PROP_FRAME_COUNT))
        texts = []
        for p in (0.0, 0.25, 0.5, 0.75):
            video.set(cv2.CAP_PROP_POS_FRAMES, int(total * p))
            ok, frame = video.read()
            if ok:
                text = pytesseract.image_to_string(Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)))
                if text.strip():
                    texts.append(text.strip())
        video.release()
        if texts:
            joined = "\n".join(texts)
            return joined, _suggested_name(joined, Path(fp).suffix)
    except Exception:
        return None, None
    return None, None


def _process_doc(fp: str):
    if docx2txt is None:
        return None, None
    try:
        text = docx2txt.process(fp)
        if text.strip():
            return text.strip(), _suggested_name(text, Path(fp).suffix)
    except Exception:
        return None, None
    return None, None


def original_filename(message) -> str:
    if message.media and hasattr(message.media, "document"):
        for attr in message.media.document.attributes:
            if isinstance(attr, DocumentAttributeFilename):
                return attr.file_name
    return f"temp_{message.id}"
