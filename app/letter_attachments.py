import base64
from io import BytesIO

import sentry_sdk
from flask import current_app
from notifications_utils.s3 import s3download

from app import ValidationFailed
from app.utils import caching_s3download, merge_letter_parts


@sentry_sdk.trace
def get_attachment_pdf(service_id, attachment_id) -> BytesIO:
    return caching_s3download(
        current_app.config["LETTER_ATTACHMENT_BUCKET_NAME"],
        f"service-{service_id}/{attachment_id}.pdf",
    )


def get_adhoc_attachment_pdfs(attachment_keys: list[str], *, allow_international_letters: bool) -> list[BytesIO]:
    """
    Downloads and sanitises each ad-hoc attachment submitted at send time on a
    templated letter. Raises ValidationFailed on the first attachment that fails
    sanitisation - sanitise_file_contents itself never raises, it returns a
    dict with a `message` key set on failure, so we translate that into an exception here.
    """
    # deferred import: app.precompiled imports app.preview, which imports this module
    # (for get_attachment_pdf) - importing at module level here would be circular.
    from app.precompiled import sanitise_file_contents

    pdfs = []
    for key in attachment_keys:
        raw = s3download(current_app.config["LETTERS_SCAN_BUCKET_NAME"], key).read()

        sanitisation_details = sanitise_file_contents(
            raw,
            allow_international_letters=allow_international_letters,
            filename=key,
            is_an_attachment=True,
        )
        if sanitisation_details.get("message"):
            raise ValidationFailed(
                sanitisation_details["message"],
                sanitisation_details.get("invalid_pages"),
                page_count=sanitisation_details.get("page_count"),
            )

        pdfs.append(BytesIO(base64.b64decode(sanitisation_details["file"].encode())))

    return pdfs


def add_attachments_to_letter(
    service_id,
    templated_letter_pdf: BytesIO,
    fixed_attachment: dict | None,
    adhoc_attachment_pdfs: list[BytesIO],
) -> BytesIO:
    pdfs = [templated_letter_pdf]

    if fixed_attachment:
        pdfs.append(get_attachment_pdf(service_id, fixed_attachment["id"]))

    pdfs.extend(adhoc_attachment_pdfs)

    return merge_letter_parts(pdfs)
