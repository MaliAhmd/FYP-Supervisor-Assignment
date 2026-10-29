"""Bounded PDF text extraction in a disposable process, with safe error codes."""

import json
import os
import subprocess
import sys

from app.config import ROOT, Settings


class PDFError(ValueError):
    pass


def extract_pages(content: bytes, settings: Settings) -> list[dict]:
    if not isinstance(content, bytes) or not content.startswith(b"%PDF-"):
        raise PDFError("invalid_pdf")
    if len(content) > settings.max_upload_bytes:
        raise PDFError("upload_too_large")
    limits = {"pages": settings.pdf_max_pages, "text_chars": settings.pdf_max_text_chars,
              "stream_bytes": settings.pdf_max_stream_bytes}
    # Only the parser child handles malformed content. It receives no app credentials.
    environment = {key: value for key, value in os.environ.items()
                   if key.upper() in {"SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP"}}
    environment["PYTHONIOENCODING"] = "utf-8"
    try:
        process = subprocess.run(
            [sys.executable, "-m", "app.integrations.pdf.worker", json.dumps(limits)],
            input=content, capture_output=True, timeout=settings.pdf_timeout_seconds,
            cwd=ROOT, env=environment,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except subprocess.TimeoutExpired:
        raise PDFError("pdf_timeout") from None
    except OSError:
        raise PDFError("pdf_worker_unavailable") from None
    if process.returncode != 0:
        raise PDFError("pdf_worker_failed")
    try:
        result = json.loads(process.stdout)
        if "error" in result:
            allowed = {"encrypted_pdf", "invalid_pdf", "page_limit", "text_limit", "stream_limit", "empty_pdf"}
            raise PDFError(result["error"] if result["error"] in allowed else "invalid_pdf")
        pages = result["pages"]
        if not isinstance(pages, list) or not 1 <= len(pages) <= settings.pdf_max_pages:
            raise ValueError()
        for number, page in enumerate(pages, 1):
            if page["page"] != number or not isinstance(page["text"], str):
                raise ValueError()
        if sum(len(page["text"]) for page in pages) > settings.pdf_max_text_chars:
            raise ValueError()
        return pages
    except PDFError:
        raise
    except (ValueError, KeyError, TypeError):
        raise PDFError("pdf_worker_failed") from None
