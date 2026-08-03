from collections.abc import Callable
from io import BytesIO

from app.letter_attachments import add_attachments_to_letter, get_adhoc_attachment_pdfs
from app.transformation import convert_pdf_to_cmyk
from app.utils import PDFPurpose, stitch_pdfs


def generate_templated_pdf(
    letter_details, create_pdf_lambda: Callable[[dict, str, bool], BytesIO], purpose: PDFPurpose
):
    # todo: remove `.get()` when all celery tasks are sending this key
    if letter_details["template"].get("letter_languages") == "welsh_then_english":
        welsh_pdf = create_pdf_lambda(letter_details, language="welsh", includes_first_page=True)
        english_pdf = create_pdf_lambda(letter_details, language="english", includes_first_page=False)

        pdf = stitch_pdfs(
            first_pdf=welsh_pdf,
            second_pdf=english_pdf,
        )
    else:
        pdf = create_pdf_lambda(letter_details, language="english", includes_first_page=True)

    if purpose == PDFPurpose.PRINT:
        pdf = convert_pdf_to_cmyk(pdf)
        pdf.seek(0)

    fixed_attachment = letter_details["template"].get("letter_attachment")
    adhoc_attachment_keys = letter_details.get("attachments", [])

    if fixed_attachment or adhoc_attachment_keys:
        adhoc_attachment_pdfs = (
            get_adhoc_attachment_pdfs(
                adhoc_attachment_keys,
                is_test_key=letter_details["key_type"] == "test",
                allow_international_letters=letter_details.get("allow_international_letters", False),
            )
            if adhoc_attachment_keys
            else []
        )
        # The fixed attachment is passed through `/precompiled/sanitise` endpoint at
        # upload time; ad-hoc attachments are sanitised just above; both are already in
        # CMYK by the time they reach here.
        pdf = add_attachments_to_letter(
            service_id=letter_details["template"]["service"],
            templated_letter_pdf=pdf,
            fixed_attachment=fixed_attachment,
            adhoc_attachment_pdfs=adhoc_attachment_pdfs,
        )
    return pdf
