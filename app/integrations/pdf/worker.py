"""Internal parser subprocess. stdout contains only bounded JSON, never logs."""

from io import BytesIO
import json
import logging
import re
import sys
import warnings

from pypdf import PdfReader, apply_configuration
from pypdf.errors import LimitReachedError


def parse(content: bytes, limits: dict) -> dict:
    logging.disable(logging.CRITICAL)
    warnings.simplefilter("ignore")
    cap = limits["stream_bytes"]
    try:
        with apply_configuration(
            maximum_declared_stream_length=cap, array_based_stream_maximum_output_length=cap,
            zlib_maximum_output_length=cap, lzw_maximum_output_length=cap,
            run_length_maximum_output_length=cap, jbig2_maximum_output_length=cap,
            brotli_maximum_output_length=cap, image_maximum_buffer_size=cap,
            stream_decoding_work_maximum_length=cap * 4,
            page_tree_maximum_entries=max(100, limits["pages"] * 4),
        ):
            reader = PdfReader(BytesIO(content), strict=True)
            if reader.is_encrypted:
                return {"error": "encrypted_pdf"}
            if not reader.pages:
                return {"error": "empty_pdf"}
            if len(reader.pages) > limits["pages"]:
                return {"error": "page_limit"}
            pages, total = [], 0
            for number, page in enumerate(reader.pages, 1):
                stream = page.get_contents()
                if stream is not None and len(stream.get_data()) > cap:
                    return {"error": "stream_limit"}
                text = page.extract_text() or ""
                text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text).strip()
                total += len(text)
                if total > limits["text_chars"]:
                    return {"error": "text_limit"}
                pages.append({"page": number, "text": text})
            return {"pages": pages}
    except LimitReachedError:
        return {"error": "stream_limit"}
    except Exception:
        return {"error": "invalid_pdf"}


if __name__ == "__main__":
    limits = json.loads(sys.argv[1])
    result = parse(sys.stdin.buffer.read(), limits)
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=True).encode())
