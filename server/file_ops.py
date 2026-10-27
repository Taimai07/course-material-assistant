"""Atomic copies and Windows origin marking for externally supplied attachments."""
import hashlib
import os
import re
from pathlib import Path
import shutil
import tempfile

SAFE_OPEN_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".txt", ".csv", ".docx", ".xlsx", ".pptx", ".mp3", ".wav", ".mp4", ".m4a", ".mov"}


def safe_filename(value):
    name = re.sub(r'[\x00-\x1f\x7f<>:"/\\|?*\u200b-\u200f\u202a-\u202e\u2066-\u2069]', "_", str(value))[:180].strip(" .") or "attachment.bin"
    if re.match(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", name, re.I):
        name = "_" + name
    return name


def file_hash(path):
    with Path(path).open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def mark_internet_file(path):
    if os.name != "nt":
        return True
    try:
        Path(str(path) + ":Zone.Identifier").write_text("[ZoneTransfer]\nZoneId=3\n", encoding="ascii")
        return True
    except OSError:
        return False


def atomic_copy(source, target, expected_hash=None, mark=True):
    source, target = Path(source), Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file() and expected_hash and file_hash(target) == expected_hash:
        if mark:
            mark_internet_file(target)
        return
    descriptor, temporary = tempfile.mkstemp(prefix=".copy-", dir=target.parent)
    os.close(descriptor)
    try:
        shutil.copyfile(source, temporary)
        if expected_hash and file_hash(temporary) != expected_hash:
            raise ValueError("附件校验失败，未替换已有文件")
        os.replace(temporary, target)
        if mark:
            mark_internet_file(target)
    finally:
        Path(temporary).unlink(missing_ok=True)
