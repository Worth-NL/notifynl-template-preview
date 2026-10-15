import base64
import logging
from datetime import UTC, datetime
from io import BytesIO

import pymupdf
import pytest
from botocore.exceptions import ClientError as BotoClientError
from flask import current_app
from notifications_utils.template import LetterPrintTemplate
from pypdf import PdfReader
from reportlab.lib.units import mm

from app import ValidationFailed
from app.celery.tasks import (
    create_pdf_for_templated_letter,
    sanitise_and_merge_letter_parts,
    sanitise_and_upload_letter,
)
from tests.celery.test_tasks import _with_message_group_id
from tests.pdf_consts import blank_page, blank_with_address, multi_page_pdf, no_colour, valid_letter
from tests.pdf_consts_nl import address_60mm_no_retouradres

DUTCH_ADDRESS = {
    "address_line_1": "Persoonlijk",
    "address_line_2": "Coolsingel 40",
    "address_line_3": "3011 AD  Rotterdam",
}


def test_sanitise_and_upload_valid_letter(mocker, client):
    valid_file = BytesIO(address_60mm_no_retouradres)

    mocker.patch("app.celery.tasks.s3download", return_value=valid_file)
    mock_upload = mocker.patch("app.celery.tasks.s3upload")
    mock_celery = mocker.patch("app.celery.tasks.notify_celery.send_task")
    mock_backup_original = mocker.patch("app.celery.tasks.copy_s3_object")

    with _with_message_group_id("test-message-group-id"):
        sanitise_and_upload_letter("abc-123", "filename.pdf")

    mock_upload.assert_called_once_with(
        filedata=mocker.ANY,
        region=current_app.config["AWS_REGION"],
        bucket_name=current_app.config["SANITISED_LETTER_BUCKET_NAME"],
        file_location="filename.pdf",
    )

    encoded_task_args = current_app.signing_client.encode(
        {
            "page_count": 1,
            "message": None,
            "invalid_pages": None,
            "validation_status": "passed",
            "filename": "filename.pdf",
            "notification_id": "abc-123",
            "address": "Persoonlijk\nCoolsingel 40\n3011 AD  ROTTERDAM",
        }
    )

    mock_celery.assert_called_once_with(
        args=(encoded_task_args,),
        name="process-sanitised-letter",
        queue="letter-tasks",
        MessageGroupId="test-message-group-id",
    )

    mock_backup_original.assert_called_once_with(
        current_app.config["LETTERS_SCAN_BUCKET_NAME"],
        "filename.pdf",
        current_app.config["PRECOMPILED_ORIGINALS_BACKUP_LETTER_BUCKET_NAME"],
        "abc-123.pdf",
    )


@pytest.mark.parametrize("letter_address_placement", ["50mm", "60mm"])
@pytest.mark.parametrize(
    "logo_filename, expected_logo_filename_value",
    (
        ("hm-government", "hm-government.svg"),
        (None, None),
    ),
)
@pytest.mark.parametrize(
    "key_type,bucket_name",
    [
        ("test", "TEST_LETTERS_BUCKET_NAME"),
        ("normal", "LETTERS_PDF_BUCKET_NAME"),
    ],
)
@pytest.mark.parametrize(
    "date_argument, expected_date_value",
    (
        ({}, None),
        ({"date": None}, None),
        ({"date": "2026-02-06T01:02:03.000000+00:00"}, datetime(2026, 2, 6, 1, 2, 3, tzinfo=UTC)),
    ),
)
def test_create_pdf_for_templated_letter_happy_path(
    mocker,
    client,
    data_for_create_pdf_for_templated_letter_task,
    mock_weasyprint_logos,
    key_type,
    bucket_name,
    logo_filename,
    expected_logo_filename_value,
    date_argument,
    expected_date_value,
    letter_address_placement,
    caplog,
):
    # create a pdf for templated letter using data from API, upload the pdf to the final S3 bucket,
    # and send data back to API so that it can update notification status and billable units.
    mock_upload = mocker.patch("app.celery.tasks.s3upload")
    mock_celery = mocker.patch("app.celery.tasks.notify_celery.send_task")
    mock_utils_template = mocker.patch("app.celery.tasks.LetterPrintTemplate", wraps=LetterPrintTemplate)

    data_for_create_pdf_for_templated_letter_task["logo_filename"] = logo_filename
    data_for_create_pdf_for_templated_letter_task["key_type"] = key_type
    data_for_create_pdf_for_templated_letter_task["values"] |= DUTCH_ADDRESS
    data_for_create_pdf_for_templated_letter_task["letter_address_placement"] = letter_address_placement
    data_for_create_pdf_for_templated_letter_task |= date_argument

    encoded_data = current_app.signing_client.encode(data_for_create_pdf_for_templated_letter_task)

    with (
        caplog.at_level(logging.INFO),
        _with_message_group_id("test-message-group-id"),
    ):
        create_pdf_for_templated_letter(encoded_data)

    mock_utils_template.assert_called_once_with(
        {
            "id": 1,
            "template_type": "letter",
            "letter_languages": "english",
            "subject": "letter subject",
            "content": "letter content with ((placeholder))",
            "letter_welsh_subject": None,
            "letter_welsh_content": None,
            "updated_at": "2017-08-01",
            "version": 1,
            "service": "1234",
        },
        values={
            "placeholder": "abc",
            **DUTCH_ADDRESS,
        },
        contact_block="123",
        admin_base_url="https://static-logos.notify.tools/letters",
        logo_file_name=expected_logo_filename_value,
        language="english",
        includes_first_page=True,
        date=expected_date_value,
        letter_address_placement=letter_address_placement,
    )

    mock_upload.assert_called_once_with(
        filedata=mocker.ANY,
        region=current_app.config["AWS_REGION"],
        bucket_name=current_app.config[bucket_name],
        file_location="MY_LETTER.PDF",
        metadata=None,
    )

    mock_celery.assert_called_once_with(
        kwargs={"notification_id": "abc-123", "page_count": 1},
        name="update-billable-units-for-letter",
        queue="letter-tasks",
        MessageGroupId="test-message-group-id",
    )
    assert "Creating a pdf for notification with id abc-123" in caplog.messages
    assert (
        f"Uploaded letters PDF MY_LETTER.PDF to {current_app.config[bucket_name]} for notification id abc-123"
        in caplog.messages
    )

    assert not any(r.levelname == "ERROR" for r in caplog.records)
    assert "NOTIFY" in PdfReader(mock_upload.call_args_list[0][1]["filedata"]).pages[0].extract_text()

    # the address moves with the envelope window: its first line starts in the top 10mm of the window
    first_page = pymupdf.open(stream=mock_upload.call_args.kwargs["filedata"].getvalue(), filetype="pdf")[0]
    first_address_line_top = first_page.search_for("Persoonlijk")[0].y0 / mm
    window_top = float(letter_address_placement.removesuffix("mm"))
    assert window_top <= first_address_line_top < window_top + 10


def _sanitise_result(page_count, file_bytes, address=None):
    # Matches the return shape of the real (unmocked) sanitise_file_contents on success.
    return {
        "recipient_address": address,
        "page_count": page_count,
        "message": None,
        "invalid_pages": None,
        "file": base64.b64encode(file_bytes).decode("utf-8"),
    }


def test_sanitise_and_merge_letter_parts_merges_two_parts_in_order(mocker, client):
    # sanitise_file_contents is mocked here (rather than exercised for real via Ghostscript/CMYK
    # conversion) so this test is deterministic and doesn't depend on real PDF fixture content -
    # the abort-on-first-failing-part test below exercises the real function instead.
    mocker.patch("app.celery.tasks.s3download", side_effect=[BytesIO(b"raw-part-1"), BytesIO(b"raw-part-2")])
    mock_sanitise = mocker.patch(
        "app.celery.tasks.sanitise_file_contents",
        side_effect=[
            _sanitise_result(1, valid_letter, address="Queen Elizabeth\nBuckingham Palace\nLondon\nSW1 1AA"),
            _sanitise_result(1, blank_page),
        ],
    )
    mock_upload = mocker.patch("app.celery.tasks.s3upload")
    mock_celery = mocker.patch("app.celery.tasks.notify_celery.send_task")
    mock_copy_s3_object = mocker.patch("app.celery.tasks.copy_s3_object")
    mock_boto3 = mocker.patch("app.celery.tasks.boto3")

    sanitise_and_merge_letter_parts("abc-123", ["filename.pdf", "filename.PART2.pdf"])

    assert [call.kwargs["is_an_attachment"] for call in mock_sanitise.call_args_list] == [False, True]

    # sibling part (part 2) is backed up and removed from the scan bucket once merged
    mock_copy_s3_object.assert_any_call(
        current_app.config["LETTERS_SCAN_BUCKET_NAME"],
        "filename.PART2.pdf",
        current_app.config["PRECOMPILED_ORIGINALS_BACKUP_LETTER_BUCKET_NAME"],
        "abc-123.part2.pdf",
    )
    mock_boto3.resource.return_value.Object.assert_called_once_with(
        current_app.config["LETTERS_SCAN_BUCKET_NAME"], "filename.PART2.pdf"
    )
    mock_boto3.resource.return_value.Object.return_value.delete.assert_called_once()

    merged_pdf = PdfReader(BytesIO(mock_upload.call_args.kwargs["filedata"]))
    assert len(merged_pdf.pages) == 2

    mock_upload.assert_called_once_with(
        filedata=mocker.ANY,
        region=current_app.config["AWS_REGION"],
        bucket_name=current_app.config["SANITISED_LETTER_BUCKET_NAME"],
        file_location="filename.pdf",
    )

    encoded_task_args = current_app.signing_client.encode(
        {
            "page_count": 2,
            "message": None,
            "invalid_pages": None,
            "validation_status": "passed",
            "filename": "filename.pdf",
            "notification_id": "abc-123",
            "address": "Queen Elizabeth\nBuckingham Palace\nLondon\nSW1 1AA",
        }
    )
    mock_celery.assert_called_once_with(
        args=(encoded_task_args,),
        name="process-sanitised-letter",
        queue="letter-tasks",
    )


def test_sanitise_and_merge_letter_parts_backs_up_all_parts_on_success(mocker, client):
    mocker.patch(
        "app.celery.tasks.s3download",
        side_effect=[BytesIO(b"raw-part-1"), BytesIO(b"raw-part-2"), BytesIO(b"raw-part-3")],
    )
    mocker.patch(
        "app.celery.tasks.sanitise_file_contents",
        side_effect=[
            _sanitise_result(1, valid_letter, address="Queen Elizabeth\nBuckingham Palace\nLondon\nSW1 1AA"),
            _sanitise_result(1, blank_page),
            _sanitise_result(1, blank_page),
        ],
    )
    mocker.patch("app.celery.tasks.s3upload")
    mocker.patch("app.celery.tasks.notify_celery.send_task")
    mock_copy = mocker.patch("app.celery.tasks.copy_s3_object")
    mock_boto3 = mocker.patch("app.celery.tasks.boto3")

    sanitise_and_merge_letter_parts("abc-123", ["filename.pdf", "filename.PART2.pdf", "filename.PART3.pdf"])

    assert mock_copy.call_args_list == [
        mocker.call(
            current_app.config["LETTERS_SCAN_BUCKET_NAME"],
            "filename.pdf",
            current_app.config["PRECOMPILED_ORIGINALS_BACKUP_LETTER_BUCKET_NAME"],
            "abc-123.pdf",
        ),
        mocker.call(
            current_app.config["LETTERS_SCAN_BUCKET_NAME"],
            "filename.PART2.pdf",
            current_app.config["PRECOMPILED_ORIGINALS_BACKUP_LETTER_BUCKET_NAME"],
            "abc-123.part2.pdf",
        ),
        mocker.call(
            current_app.config["LETTERS_SCAN_BUCKET_NAME"],
            "filename.PART3.pdf",
            current_app.config["PRECOMPILED_ORIGINALS_BACKUP_LETTER_BUCKET_NAME"],
            "abc-123.part3.pdf",
        ),
    ]
    assert mock_boto3.resource.return_value.Object.call_args_list == [
        mocker.call(current_app.config["LETTERS_SCAN_BUCKET_NAME"], "filename.PART2.pdf"),
        mocker.call(current_app.config["LETTERS_SCAN_BUCKET_NAME"], "filename.PART3.pdf"),
    ]
    assert mock_boto3.resource.return_value.Object.return_value.delete.call_count == 2


def test_sanitise_and_merge_letter_parts_aborts_on_first_failing_part(mocker, client):
    mock_download = mocker.patch("app.celery.tasks.s3download", side_effect=[BytesIO(no_colour)])
    mock_upload = mocker.patch("app.celery.tasks.s3upload")
    mock_celery = mocker.patch("app.celery.tasks.notify_celery.send_task")
    mocker.patch("app.celery.tasks.copy_s3_object")
    mocker.patch("app.celery.tasks.boto3")

    sanitise_and_merge_letter_parts("abc-123", ["filename.pdf", "filename.PART2.pdf"])

    # the second part is never even downloaded, since the first part already failed
    mock_download.assert_called_once_with(current_app.config["LETTERS_SCAN_BUCKET_NAME"], "filename.pdf")
    assert not mock_upload.called

    encoded_task_args = current_app.signing_client.encode(
        {
            "page_count": 2,
            "message": "content-outside-printable-area",
            "invalid_pages": [1, 2],
            "validation_status": "failed",
            "filename": "filename.pdf",
            "notification_id": "abc-123",
            "address": None,
        }
    )
    mock_celery.assert_called_once_with(
        args=(encoded_task_args,),
        name="process-sanitised-letter",
        queue="letter-tasks",
    )


def test_sanitise_and_merge_letter_parts_moves_sibling_scan_objects_to_invalid_bucket_on_failure(mocker, client):
    mocker.patch(
        "app.celery.tasks.s3download",
        side_effect=[BytesIO(blank_with_address), BytesIO(no_colour), BytesIO(blank_page)],
    )
    mocker.patch("app.celery.tasks.s3upload")
    mocker.patch("app.celery.tasks.notify_celery.send_task")
    mock_copy = mocker.patch("app.celery.tasks.copy_s3_object")
    mock_boto3 = mocker.patch("app.celery.tasks.boto3")

    sanitise_and_merge_letter_parts("abc-123", ["filename.pdf", "filename.PART2.pdf", "filename.PART3.pdf"])

    # part 2 failed its own sanitisation and part 3 was never even downloaded (part 2 already
    # failed) - neither is seen by the (unmodified) process-sanitised-letter task downstream,
    # which only ever knows about the canonical (part 0) filename, so both of their raw
    # scan-bucket objects need to be moved out by this task itself so they aren't orphaned there
    assert mock_copy.call_args_list == [
        mocker.call(
            current_app.config["LETTERS_SCAN_BUCKET_NAME"],
            "filename.PART2.pdf",
            current_app.config["INVALID_PDF_BUCKET_NAME"],
            "filename.PART2.pdf",
        ),
        mocker.call(
            current_app.config["LETTERS_SCAN_BUCKET_NAME"],
            "filename.PART3.pdf",
            current_app.config["INVALID_PDF_BUCKET_NAME"],
            "filename.PART3.pdf",
        ),
    ]
    assert mock_boto3.resource.return_value.Object.call_args_list == [
        mocker.call(current_app.config["LETTERS_SCAN_BUCKET_NAME"], "filename.PART2.pdf"),
        mocker.call(current_app.config["LETTERS_SCAN_BUCKET_NAME"], "filename.PART3.pdf"),
    ]
    assert mock_boto3.resource.return_value.Object.return_value.delete.call_count == 2


def test_sanitise_and_merge_letter_parts_combined_page_count_over_limit_after_merge(mocker, client):
    # 1 page + 10 pages = 11 pages, over LETTER_MAX_PAGE_COUNT (10), even though each part
    # individually passes its own per-part page count check
    mocker.patch("app.celery.tasks.s3download", side_effect=[BytesIO(b"raw-part-1"), BytesIO(b"raw-part-2")])
    mocker.patch(
        "app.celery.tasks.sanitise_file_contents",
        side_effect=[
            _sanitise_result(1, valid_letter, address="Queen Elizabeth\nBuckingham Palace\nLondon\nSW1 1AA"),
            _sanitise_result(10, multi_page_pdf),
        ],
    )
    mock_upload = mocker.patch("app.celery.tasks.s3upload")
    mock_celery = mocker.patch("app.celery.tasks.notify_celery.send_task")
    mocker.patch("app.celery.tasks.copy_s3_object")
    mocker.patch("app.celery.tasks.boto3")

    sanitise_and_merge_letter_parts("abc-123", ["filename.pdf", "filename.PART2.pdf"])

    assert not mock_upload.called

    encoded_task_args = current_app.signing_client.encode(
        {
            "page_count": 11,
            "message": "letter-too-long",
            "invalid_pages": None,
            "validation_status": "failed",
            "filename": "filename.pdf",
            "notification_id": "abc-123",
            # part 0 individually passed sanitisation (with a known address) before the
            # post-merge page count check failed, so the address is still known here
            "address": "Queen Elizabeth\nBuckingham Palace\nLondon\nSW1 1AA",
        }
    )
    mock_celery.assert_called_once_with(
        args=(encoded_task_args,),
        name="process-sanitised-letter",
        queue="letter-tasks",
    )


def test_sanitise_and_merge_letter_parts_raises_a_boto_error(mocker, client, caplog):
    mocker.patch("app.celery.tasks.s3download", side_effect=BotoClientError({}, "operation-name"))
    mock_upload = mocker.patch("app.celery.tasks.s3upload")
    mock_celery = mocker.patch("app.celery.tasks.notify_celery.send_task")

    with caplog.at_level(logging.ERROR):
        sanitise_and_merge_letter_parts("abc-123", ["filename.pdf", "filename.PART2.pdf"])

    assert not mock_upload.called
    assert not mock_celery.called
    assert any("Error downloading" in message for message in caplog.messages)


def test_create_pdf_for_templated_letter_adds_letter_attachment_if_provided(
    mocker,
    client,
    data_for_create_pdf_for_templated_letter_task,
):
    # create a pdf for templated letter using data from API, upload the pdf to the final S3 bucket,
    # and send data back to API so that it can update notification status and billable units.
    mock_upload = mocker.patch("app.celery.tasks.s3upload")
    mock_celery = mocker.patch("app.celery.tasks.notify_celery.send_task")
    mock_convert_pdf_to_cmyk = mocker.patch("app.templated.convert_pdf_to_cmyk")
    mock_add_attachments = mocker.patch(
        "app.templated.add_attachments_to_letter",
        return_value=BytesIO(multi_page_pdf),
    )

    data_for_create_pdf_for_templated_letter_task["template"]["letter_attachment"] = {"page_count": 1, "id": "5678"}

    encoded_data = current_app.signing_client.encode(data_for_create_pdf_for_templated_letter_task)

    create_pdf_for_templated_letter(encoded_data)

    mock_add_attachments.assert_called_once_with(
        service_id="1234",
        templated_letter_pdf=mock_convert_pdf_to_cmyk.return_value,
        fixed_attachment=data_for_create_pdf_for_templated_letter_task["template"]["letter_attachment"],
        adhoc_attachment_pdfs=[],
    )

    assert mock_upload.call_args.kwargs["filedata"] == mock_add_attachments.return_value
    # make sure we're recalculating the page count from the return value of add_attachments_to_letter
    # rather than just adding the letter_attachment["page_count"] value or anything
    # (multi_page_pdf is 10 pages long)
    assert mock_celery.call_args.kwargs["kwargs"]["page_count"] == 10
    assert mock_celery.call_args.kwargs["name"] == "update-billable-units-for-letter"


def test_create_pdf_for_templated_letter_sanitises_adhoc_attachments_for_test_key_too(
    mocker, client, data_for_create_pdf_for_templated_letter_task
):
    mock_upload = mocker.patch("app.celery.tasks.s3upload")
    mocker.patch("app.celery.tasks.notify_celery.send_task")
    mock_sanitise = mocker.patch(
        "app.precompiled.sanitise_file_contents",
        return_value=_sanitise_result(1, blank_page),
    )
    mocker.patch("app.letter_attachments.s3download", return_value=BytesIO(blank_page))
    mock_boto3 = mocker.patch("app.celery.tasks.boto3")

    data_for_create_pdf_for_templated_letter_task["key_type"] = "test"
    data_for_create_pdf_for_templated_letter_task["attachments"] = ["abc-123/attachment-1.pdf"]

    encoded_data = current_app.signing_client.encode(data_for_create_pdf_for_templated_letter_task)

    create_pdf_for_templated_letter(encoded_data)

    # test-key sends are sanitised the same as any other send, so service users see
    # consistent results regardless of key type; the scanned attachment is still
    # cleaned up from the scan bucket afterwards either way
    mock_sanitise.assert_called_once()
    merged_pdf = PdfReader(mock_upload.call_args.kwargs["filedata"])
    assert len(merged_pdf.pages) == 2  # templated letter (1 page) + ad-hoc attachment (1 page)
    mock_boto3.resource.return_value.Object.assert_called_once_with(
        current_app.config["LETTERS_SCAN_BUCKET_NAME"], "abc-123/attachment-1.pdf"
    )
    mock_boto3.resource.return_value.Object.return_value.delete.assert_called_once()


def test_create_pdf_for_templated_letter_sanitises_merges_and_cleans_up_adhoc_attachments(
    mocker, client, data_for_create_pdf_for_templated_letter_task
):
    mock_upload = mocker.patch("app.celery.tasks.s3upload")
    mocker.patch("app.celery.tasks.notify_celery.send_task")
    mock_sanitise = mocker.patch(
        "app.precompiled.sanitise_file_contents",
        side_effect=[_sanitise_result(1, blank_page), _sanitise_result(1, blank_page)],
    )
    mocker.patch("app.letter_attachments.s3download", return_value=BytesIO(b"raw-bytes"))
    mock_boto3 = mocker.patch("app.celery.tasks.boto3")

    data_for_create_pdf_for_templated_letter_task["attachments"] = [
        "abc-123/attachment-1.pdf",
        "abc-123/attachment-2.pdf",
    ]

    encoded_data = current_app.signing_client.encode(data_for_create_pdf_for_templated_letter_task)

    create_pdf_for_templated_letter(encoded_data)

    assert mock_sanitise.call_count == 2
    merged_pdf = PdfReader(mock_upload.call_args.kwargs["filedata"])
    assert len(merged_pdf.pages) == 3  # templated letter (1 page) + 2 ad-hoc attachments (1 page each)

    assert mock_boto3.resource.return_value.Object.call_args_list == [
        mocker.call(current_app.config["LETTERS_SCAN_BUCKET_NAME"], "abc-123/attachment-1.pdf"),
        mocker.call(current_app.config["LETTERS_SCAN_BUCKET_NAME"], "abc-123/attachment-2.pdf"),
    ]
    assert mock_boto3.resource.return_value.Object.return_value.delete.call_count == 2


def test_create_pdf_for_templated_letter_dispatches_validation_failed_for_bad_adhoc_attachment(
    mocker, client, data_for_create_pdf_for_templated_letter_task, caplog
):
    mocker.patch(
        "app.celery.tasks._prepare_pdf",
        side_effect=ValidationFailed("content-outside-printable-area", [1], page_count=2),
    )
    mock_upload = mocker.patch("app.celery.tasks.s3upload")
    mock_celery = mocker.patch("app.celery.tasks.notify_celery.send_task")
    mock_copy = mocker.patch("app.celery.tasks.copy_s3_object")
    mock_boto3 = mocker.patch("app.celery.tasks.boto3")

    data_for_create_pdf_for_templated_letter_task["attachments"] = ["abc-123/attachment-1.pdf"]

    encoded_data = current_app.signing_client.encode(data_for_create_pdf_for_templated_letter_task)

    with caplog.at_level(logging.WARNING), _with_message_group_id("test-message-group-id"):
        create_pdf_for_templated_letter(encoded_data)

    assert not mock_upload.called
    mock_celery.assert_called_once_with(
        kwargs={"notification_id": "abc-123", "page_count": 2},
        name="update-validation-failed-for-templated-letter",
        queue="letter-tasks",
        MessageGroupId="test-message-group-id",
    )
    assert any("Ad-hoc attachment validation failed" in message for message in caplog.messages)

    # the ad-hoc attachment's raw scan-bucket object must not be orphaned - it's moved to
    # the invalid-pdf bucket (inspectable) rather than left behind or silently deleted
    mock_copy.assert_called_once_with(
        current_app.config["LETTERS_SCAN_BUCKET_NAME"],
        "abc-123/attachment-1.pdf",
        current_app.config["INVALID_PDF_BUCKET_NAME"],
        "abc-123/attachment-1.pdf",
    )
    mock_boto3.resource.return_value.Object.assert_called_once_with(
        current_app.config["LETTERS_SCAN_BUCKET_NAME"], "abc-123/attachment-1.pdf"
    )
    mock_boto3.resource.return_value.Object.return_value.delete.assert_called_once()
