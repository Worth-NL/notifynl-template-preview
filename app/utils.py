from base64 import b64decode, b64encode
from enum import StrEnum, auto
from functools import lru_cache
from io import BytesIO

import dateutil.parser
import sentry_sdk
from notifications_utils.s3 import s3download
from pypdf import PdfReader, PdfWriter


@sentry_sdk.trace
def stitch_pdfs(first_pdf: BytesIO, second_pdf: BytesIO) -> BytesIO:
    output = PdfWriter()
    output.append_pages_from_reader(PdfReader(first_pdf))
    output.append_pages_from_reader(PdfReader(second_pdf))

    pdf_bytes = BytesIO()
    output.write(pdf_bytes)
    pdf_bytes.seek(0)
    return pdf_bytes


@sentry_sdk.trace
def merge_letter_parts(pdfs: list[BytesIO]) -> BytesIO:
    """
    Merges an ordered list of precompiled letter parts into a single PDF,
    preserving submission order (pdfs[0]'s pages first, then pdfs[1]'s, etc).
    """
    merged = pdfs[0]
    for pdf in pdfs[1:]:
        merged = stitch_pdfs(merged, pdf)
    return merged


class PDFPurpose(StrEnum):
    PREVIEW = auto()
    PRINT = auto()


def caching_s3download(bucket_name, filename) -> BytesIO:
    cached = _cached_s3_download(bucket_name, filename)
    return BytesIO(b64decode(cached))


@lru_cache(maxsize=2_000)
def _cached_s3_download(bucket_name, filename):
    return b64encode(s3download(bucket_name, filename).read())


def get_transient_letter_file_location(service_id, upload_id):
    return f"service-{service_id}/{upload_id}.pdf"


def get_datetime_from_json(request_json):
    return dateutil.parser.parse(request_json["date"]) if request_json.get("date") else None
