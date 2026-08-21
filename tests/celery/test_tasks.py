import base64
import logging
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from io import BytesIO
from unittest.mock import patch

import boto3
import pytest
from botocore.exceptions import ClientError as BotoClientError
from celery.exceptions import Retry
from flask import current_app
from moto import mock_aws
from notifications_utils.template import LetterPrintTemplate
from pypdf import PdfReader

import app.celery.tasks
from app import ValidationFailed
from app.celery.tasks import (
    _create_pdf_for_letter,
    _prepare_pdf,
    _remove_folder_from_filename,
    create_pdf_for_templated_letter,
    recreate_pdf_for_precompiled_letter,
    recreate_pdf_for_template_letter_attachments,
    sanitise_and_merge_letter_parts,
    sanitise_and_upload_letter,
)
from app.config import QueueNames
from app.utils import get_transient_letter_file_location
from app.weasyprint_hack import WeasyprintError
from tests.pdf_consts import bad_postcode, blank_page, blank_with_address, multi_page_pdf, no_colour, valid_letter


@contextmanager
def _with_message_group_id(value):
    with patch("notifications_utils.celery.NotifyTask.message_group_id", new=value, create=True):
        yield


@pytest.mark.skip(reason="[NOTIFYNL] Bucket name issues")
def test_sanitise_and_upload_valid_letter(mocker, client):
    valid_file = BytesIO(blank_with_address)

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
            "address": "Queen Elizabeth\nBuckingham Palace\nLondon\nSW1 1AA",
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


def test_sanitise_invalid_letter(mocker, client):
    file_with_content_in_margins = BytesIO(no_colour)

    mocker.patch("app.celery.tasks.s3download", return_value=file_with_content_in_margins)
    mock_upload = mocker.patch("app.celery.tasks.s3upload")
    mock_celery = mocker.patch("app.celery.tasks.notify_celery.send_task")

    with _with_message_group_id("test-message-group-id"):
        sanitise_and_upload_letter("abc-123", "filename.pdf")

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

    assert not mock_upload.called
    mock_celery.assert_called_once_with(
        args=(encoded_task_args,),
        name="process-sanitised-letter",
        queue="letter-tasks",
        MessageGroupId="test-message-group-id",
    )


@pytest.mark.skip(reason="[NOTIFYNL] Broken by validation change")
@pytest.mark.parametrize(
    "extra_args, expected_error",
    (
        ({}, "not-a-real-uk-postcode"),
        ({"allow_international_letters": False}, "not-a-real-uk-postcode"),
        ({"allow_international_letters": True}, "not-a-real-uk-postcode-or-country"),
    ),
)
def test_sanitise_international_letters(
    mocker,
    client,
    extra_args,
    expected_error,
):
    mocker.patch("app.celery.tasks.s3download", return_value=BytesIO(bad_postcode))
    mock_upload = mocker.patch("app.celery.tasks.s3upload")
    mock_celery = mocker.patch("app.celery.tasks.notify_celery.send_task")

    with _with_message_group_id("test-message-group-id"):
        sanitise_and_upload_letter("abc-123", "filename.pdf", **extra_args)

    encoded_task_args = current_app.signing_client.encode(
        {
            "page_count": 1,
            "message": expected_error,
            "invalid_pages": [1],
            "validation_status": "failed",
            "filename": "filename.pdf",
            "notification_id": "abc-123",
            "address": None,
        }
    )

    assert not mock_upload.called
    mock_celery.assert_called_once_with(
        args=(encoded_task_args,),
        name="process-sanitised-letter",
        queue="letter-tasks",
        MessageGroupId="test-message-group-id",
    )


def test_sanitise_and_upload_letter_raises_a_boto_error(mocker, client, caplog):
    mocker.patch("app.celery.tasks.s3download", side_effect=BotoClientError({}, "operation-name"))
    mock_upload = mocker.patch("app.celery.tasks.s3upload")
    mock_celery = mocker.patch("app.celery.tasks.notify_celery.send_task")

    filename = "filename.pdf"
    notification_id = "abc-123"

    with caplog.at_level(logging.ERROR):
        sanitise_and_upload_letter(notification_id, filename)

    assert not mock_upload.called
    assert not mock_celery.called

    assert (
        "Error downloading filename.pdf from scan bucket or uploading to sanitise bucket for notification abc-123"
        in caplog.messages
    )


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


@pytest.mark.skip(reason="[NOTIFYNL] AWS permissions break test.")
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
    key_type,
    bucket_name,
    logo_filename,
    expected_logo_filename_value,
    date_argument,
    expected_date_value,
    caplog,
):
    # create a pdf for templated letter using data from API, upload the pdf to the final S3 bucket,
    # and send data back to API so that it can update notification status and billable units.
    mock_upload = mocker.patch("app.celery.tasks.s3upload")
    mock_celery = mocker.patch("app.celery.tasks.notify_celery.send_task")
    mock_utils_template = mocker.patch("app.celery.tasks.LetterPrintTemplate", wraps=LetterPrintTemplate)

    data_for_create_pdf_for_templated_letter_task["logo_filename"] = logo_filename
    data_for_create_pdf_for_templated_letter_task["key_type"] = key_type
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
        },
        contact_block="123",
        admin_base_url="https://static-logos.notify.tools/letters",
        logo_file_name=expected_logo_filename_value,
        language="english",
        includes_first_page=True,
        date=expected_date_value,
        letter_address_placement="50mm",
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


def test_create_pdf_for_templated_letter_includes_welsh_pages_if_provided(
    mocker,
    client,
    caplog,
    welsh_data_for_create_pdf_for_templated_letter_task,
):
    # create a pdf for templated letter using data from API, upload the pdf to the final S3 bucket,
    # and send data back to API so that it can update notification status and billable units.
    mock_upload = mocker.patch("app.celery.tasks.s3upload")
    mock_celery = mocker.patch("app.celery.tasks.notify_celery.send_task")
    mock_create_pdf = mocker.patch("app.celery.tasks._create_pdf_for_letter", wraps=_create_pdf_for_letter)

    encoded_data = current_app.signing_client.encode(welsh_data_for_create_pdf_for_templated_letter_task)

    with (
        caplog.at_level(logging.INFO),
        _with_message_group_id("test-message-group-id"),
    ):
        create_pdf_for_templated_letter(encoded_data)

    mock_upload.assert_called_once_with(
        filedata=mocker.ANY,
        region=current_app.config["AWS_REGION"],
        bucket_name=current_app.config["LETTERS_PDF_BUCKET_NAME"],
        file_location="MY_LETTER.PDF",
        metadata=None,
    )

    mock_celery.assert_called_once_with(
        kwargs={"notification_id": "abc-123", "page_count": 2},
        name="update-billable-units-for-letter",
        queue="letter-tasks",
        MessageGroupId="test-message-group-id",
    )
    assert "Creating a pdf for notification with id abc-123" in caplog.messages
    assert (
        f"Uploaded letters PDF MY_LETTER.PDF to {current_app.config['LETTERS_PDF_BUCKET_NAME']} for "
        "notification id abc-123" in caplog.messages
    )

    assert mock_create_pdf.call_args_list == [
        mocker.call(mocker.ANY, mocker.ANY, language="welsh", includes_first_page=True),
        mocker.call(mocker.ANY, mocker.ANY, language="english", includes_first_page=False),
    ]

    assert not any(r.levelname == "ERROR" for r in caplog.records)


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


def test_create_pdf_for_templated_letter_errors_if_attachment_pushes_over_page_count(
    mocker,
    client,
    data_for_create_pdf_for_templated_letter_task,
):
    # try stitching a 10 page attachment to a 1 page template
    mocker.patch("app.letter_attachments.get_attachment_pdf", return_value=BytesIO(multi_page_pdf))
    mock_upload = mocker.patch("app.celery.tasks.s3upload")
    mock_celery = mocker.patch("app.celery.tasks.notify_celery.send_task")

    data_for_create_pdf_for_templated_letter_task["template"]["letter_attachment"] = {"page_count": 10, "id": "5678"}

    encoded_data = current_app.signing_client.encode(data_for_create_pdf_for_templated_letter_task)

    create_pdf_for_templated_letter(encoded_data)

    assert mock_upload.call_args.kwargs["bucket_name"] == current_app.config["INVALID_PDF_BUCKET_NAME"]
    assert mock_upload.call_args.kwargs["metadata"] == {
        "validation_status": "failed",
        "message": "letter-too-long",
        "page_count": "11",
    }
    assert mock_celery.call_args.kwargs["name"] == "update-validation-failed-for-templated-letter"


def test_create_pdf_for_templated_letter_boto_error(
    mocker, client, data_for_create_pdf_for_templated_letter_task, caplog
):
    # handle boto error while uploading file
    mocker.patch("app.celery.tasks.s3upload", side_effect=BotoClientError({}, "operation-name"))
    mock_celery = mocker.patch("app.celery.tasks.notify_celery.send_task")

    encoded_data = current_app.signing_client.encode(data_for_create_pdf_for_templated_letter_task)

    with (
        caplog.at_level(logging.INFO),
        _with_message_group_id("test-message-group-id"),
    ):
        create_pdf_for_templated_letter(encoded_data)

    assert not mock_celery.called

    assert "Creating a pdf for notification with id abc-123" in caplog.messages
    assert "Error uploading MY_LETTER.PDF to pdf bucket for notification abc-123" in caplog.messages


@pytest.mark.skip(reason="[NOTIFYNL] AWS permissions break test.")
def test_create_pdf_for_templated_letter_when_letter_is_too_long(
    mocker, client, data_for_create_pdf_for_templated_letter_task, caplog
):
    # create a pdf for templated letter using data from API, upload the pdf to the final S3 bucket,
    # and send data back to API so that it can update notification status and billable units.
    mock_upload = mocker.patch("app.celery.tasks.s3upload")
    mock_celery = mocker.patch("app.celery.tasks.notify_celery.send_task")
    mocker.patch("app.celery.tasks.get_page_count_for_pdf", return_value=11)

    data_for_create_pdf_for_templated_letter_task["logo_filename"] = "hm-government"
    data_for_create_pdf_for_templated_letter_task["key_type"] = "normal"

    encoded_data = current_app.signing_client.encode(data_for_create_pdf_for_templated_letter_task)

    with caplog.at_level(logging.INFO), _with_message_group_id("test-message-group-id"):
        create_pdf_for_templated_letter(encoded_data)

    mock_upload.assert_called_once_with(
        filedata=mocker.ANY,
        region=current_app.config["AWS_REGION"],
        bucket_name=current_app.config["INVALID_PDF_BUCKET_NAME"],
        file_location="MY_LETTER.PDF",
        metadata={
            "validation_status": "failed",
            "message": "letter-too-long",
            "page_count": "11",
        },
    )

    mock_celery.assert_called_once_with(
        kwargs={"notification_id": "abc-123", "page_count": 11},
        name="update-validation-failed-for-templated-letter",
        queue="letter-tasks",
        MessageGroupId="test-message-group-id",
    )
    assert "Creating a pdf for notification with id abc-123" in caplog.messages
    assert (
        f"Uploaded letters PDF MY_LETTER.PDF to {current_app.config['INVALID_PDF_BUCKET_NAME']} "
        "for notification id abc-123"
    ) in caplog.messages
    assert not any(r.levelname == "ERROR" for r in caplog.records)


def test_create_pdf_for_templated_letter_html_error(mocker, data_for_create_pdf_for_templated_letter_task, client):
    encoded_data = current_app.signing_client.encode(data_for_create_pdf_for_templated_letter_task)

    weasyprint_html = mocker.Mock()
    expected_exc = WeasyprintError()
    weasyprint_html.write_pdf.side_effect = expected_exc

    mocker.patch("app.celery.tasks.HTML", mocker.Mock(return_value=weasyprint_html))
    mock_retry = mocker.patch("app.celery.tasks.create_pdf_for_templated_letter.retry", side_effect=Retry)

    with pytest.raises(Retry):
        create_pdf_for_templated_letter(encoded_data)

    mock_retry.assert_called_once_with(exc=expected_exc, queue=QueueNames.SANITISE_LETTERS)


@pytest.mark.skip(reason="[NOTIFYNL] Bucket name issues")
@mock_aws
def test_recreate_pdf_for_precompiled_letter(mocker, client):
    # create backup S3 bucket and an S3 bucket for the final letters that will be sent to DVLA
    conn = boto3.resource("s3", region_name=current_app.config["AWS_REGION"])
    backup_bucket = conn.create_bucket(
        Bucket=current_app.config["PRECOMPILED_ORIGINALS_BACKUP_LETTER_BUCKET_NAME"],
        CreateBucketConfiguration={"LocationConstraint": "eu-west-1"},
    )
    final_letters_bucket = conn.create_bucket(
        Bucket=current_app.config["LETTERS_PDF_BUCKET_NAME"],
        CreateBucketConfiguration={"LocationConstraint": "eu-west-1"},
    )

    # put a valid PDF in the backup S3 bucket
    valid_file = BytesIO(blank_with_address)
    s3 = boto3.client("s3", region_name="eu-west-1")
    s3.put_object(
        Bucket=current_app.config["PRECOMPILED_ORIGINALS_BACKUP_LETTER_BUCKET_NAME"],
        Key="1234-abcd.pdf",
        Body=valid_file.read(),
    )

    sanitise_spy = mocker.spy(app.celery.tasks, "sanitise_file_contents")

    recreate_pdf_for_precompiled_letter("1234-abcd", "2021-10-10/NOTIFY.REF.D.2.C.202110101330.PDF", True)

    # backup PDF still exists in the backup bucket
    assert [o.key for o in backup_bucket.objects.all()] == ["1234-abcd.pdf"]
    # the final letters bucket contains the recreated PDF
    assert [o.key for o in final_letters_bucket.objects.all()] == ["2021-10-10/NOTIFY.REF.D.2.C.202110101330.PDF"]

    # Check that the file in the final letters bucket has been through the `sanitise_file_contents` function
    sanitised_file_contents = (
        conn.Object(
            current_app.config["LETTERS_PDF_BUCKET_NAME"],
            "2021-10-10/NOTIFY.REF.D.2.C.202110101330.PDF",
        )
        .get()["Body"]
        .read()
    )
    assert base64.b64decode(sanitise_spy.spy_return["file"].encode()) == sanitised_file_contents


@mock_aws
def test_recreate_pdf_for_precompiled_letter_with_s3_error(client, caplog):
    # create the backup S3 bucket, which is empty so will cause an error when attempting to download the file
    conn = boto3.resource("s3", region_name=current_app.config["AWS_REGION"])
    conn.create_bucket(
        Bucket=current_app.config["PRECOMPILED_ORIGINALS_BACKUP_LETTER_BUCKET_NAME"],
        CreateBucketConfiguration={"LocationConstraint": "eu-west-1"},
    )

    with caplog.at_level(logging.ERROR):
        recreate_pdf_for_precompiled_letter("1234-abcd", "2021-10-10/NOTIFY.REF.D.2.C.202110101330.PDF", True)

    assert (
        "Error downloading file from backup bucket or uploading to letters-pdf bucket for notification 1234-abcd"
        in caplog.messages
    )


@mock_aws
def test_recreate_pdf_for_precompiled_letter_that_fails_validation(client, caplog):
    # create backup S3 bucket and an S3 bucket for the final letters that will be sent to DVLA
    conn = boto3.resource("s3", region_name=current_app.config["AWS_REGION"])
    backup_bucket = conn.create_bucket(
        Bucket=current_app.config["PRECOMPILED_ORIGINALS_BACKUP_LETTER_BUCKET_NAME"],
        CreateBucketConfiguration={"LocationConstraint": "eu-west-1"},
    )
    final_letters_bucket = conn.create_bucket(
        Bucket=current_app.config["LETTERS_PDF_BUCKET_NAME"],
        CreateBucketConfiguration={"LocationConstraint": "eu-west-1"},
    )

    # put an invalid PDF in the backup S3 bucket so that it fails sanitisation
    invalid_file = BytesIO(bad_postcode)
    s3 = boto3.client("s3", region_name="eu-west-1")
    s3.put_object(
        Bucket=current_app.config["PRECOMPILED_ORIGINALS_BACKUP_LETTER_BUCKET_NAME"],
        Key="1234-abcd.pdf",
        Body=invalid_file.read(),
    )

    with caplog.at_level(logging.ERROR):
        recreate_pdf_for_precompiled_letter("1234-abcd", "2021-10-10/NOTIFY.REF.D.2.C.202110101330.PDF", True)

    # the original file has not been copied or moved
    assert [o.key for o in backup_bucket.objects.all()] == ["1234-abcd.pdf"]
    assert len(list(final_letters_bucket.objects.all())) == 0

    assert "Notification failed resanitisation: 1234-abcd" in caplog.messages


@pytest.mark.parametrize(
    "filename, expected_filename",
    [
        ("2018-01-13/NOTIFY.ABCDEF1234567890.PDF", "NOTIFY.ABCDEF1234567890.PDF"),
        ("NOTIFY.ABCDEF1234567890.PDF", "NOTIFY.ABCDEF1234567890.PDF"),
    ],
)
def test_remove_folder_from_filename(filename, expected_filename):
    actual_filename = _remove_folder_from_filename(filename)
    assert actual_filename == expected_filename


@pytest.mark.parametrize("includes_first_page", (True, False))
def test_create_pdf_for_letter_notify_tagging(client, includes_first_page):
    pdf = _create_pdf_for_letter(
        task=None,
        letter_details={
            "template": {"template_type": "letter", "subject": "subject", "content": "content"},
            "values": {},
            "letter_contact_block": "",
            "logo_filename": "",
        },
        language="english",
        includes_first_page=includes_first_page,
    )

    assert ("NOTIFY" in PdfReader(pdf).pages[0].extract_text()) is includes_first_page


@pytest.mark.parametrize(
    "letter_content",
    [
        {"language": "English", "content": "My favourite animal is a cat."},
        {"language": "Urdu", "content": "میرا پسندیدہ جانور ایک بلی ہے۔"},
        {"language": "Ukrainian", "content": "Моя улюблена тварина - кіт."},
        {"language": "Polish", "content": "Moim ulubionym zwierzęciem jest kot."},
        {"language": "Romanian", "content": "Animalul meu preferat este o pisică."},
        {"language": "Latvian", "content": "Mans mīļākais dzīvnieks ir kaķis."},
        {"language": "Chinese (Simplified)", "content": "我最喜欢的动物是猫。"},
        {"language": "Arabic", "content": "حيواني المفضل هو القط."},
        {"language": "Hindi", "content": "मेरा पसंदीदा जानवर एक बिल्ली है।"},
        {"language": "Punjabi (Gurmukhi script)", "content": "ਮੇਰਾ ਮਨਪਸੰਦ ਜਾਨਵਰ ਇੱਕ ਬਿੱਲੀ ਹੈ।"},
        {"language": "Bangla", "content": "আমার প্রিয় প্রাণী একটি বিড়াল।"},
        {"language": "Tamil", "content": "எனது பிடித்த விலங்கு ஒரு பூனை."},
        {"language": "Telugu", "content": "నా ఇష్టమైన జంతువు ఒక పిల్లి."},
        {"language": "Gujarati", "content": "મારું મનપસંદ પ્રાણી એક બિલાડી છે."},
        {"language": "Marathi", "content": "माझं आवडतं प्राणी एक मांजर आहे."},
        {"language": "Kannada", "content": "ನನ್ನ ಪ್ರಿಯ ಪ್ರಾಣಿ ಒಂದು ಬೆಕ್ಕು."},
        {"language": "Hebrew", "content": "החיה האהובה עלי היא חתול."},
        {"language": "Greek", "content": "Το αγαπημένο μου ζώο είναι μια γάτα."},
        {"language": "Russian", "content": "Мое любимое животное - кот."},
    ],
    ids=lambda x: x["language"],
)
def test_cmyk_pdf_with_multiple_languages_for_letter_notify_tagging(client, letter_content):
    pdf = _prepare_pdf(
        self=None,
        letter_details={
            "template": {"template_type": "letter", "subject": "subject", "content": letter_content["content"]},
            "values": {},
            "letter_contact_block": "",
            "logo_filename": "",
        },
    )

    assert "NOTIFY" in PdfReader(pdf).pages[0].extract_text()


@mock_aws
def test_recreate_pdf_for_template_letter_attachments(mocker, client):
    # create backup S3 bucket and an S3 bucket for the sanitised attachment letters
    conn = boto3.resource("s3", region_name=current_app.config["AWS_REGION"])
    backup_bucket = conn.create_bucket(
        Bucket=current_app.config["PRECOMPILED_ORIGINALS_BACKUP_LETTER_BUCKET_NAME"],
        CreateBucketConfiguration={"LocationConstraint": "eu-west-1"},
    )
    final_letters_bucket = conn.create_bucket(
        Bucket=current_app.config["LETTER_ATTACHMENT_BUCKET_NAME"],
        CreateBucketConfiguration={"LocationConstraint": "eu-west-1"},
    )

    service_id = str(uuid.uuid4())
    attachment_id = str(uuid.uuid4())

    # put a valid PDF in the backup S3 bucket
    valid_file = BytesIO(blank_with_address)
    s3 = boto3.client("s3", region_name="eu-west-1")
    s3.put_object(
        Bucket=current_app.config["PRECOMPILED_ORIGINALS_BACKUP_LETTER_BUCKET_NAME"],
        Key=f"{attachment_id}.pdf",
        Body=valid_file.read(),
    )

    sanitise_spy = mocker.spy(app.celery.tasks, "sanitise_file_contents")

    recreate_pdf_for_template_letter_attachments(service_id, attachment_id, "1234-abcd.pdf")

    # backup PDF still exists in the backup bucket
    assert [o.key for o in backup_bucket.objects.all()] == [f"{attachment_id}.pdf"]
    # the final letters bucket contains the recreated PDF
    assert [o.key for o in final_letters_bucket.objects.all()] == [
        get_transient_letter_file_location(service_id, attachment_id)
    ]

    # Check that the file in the final letters bucket has been through the `sanitise_file_contents` function
    sanitised_file_contents = (
        conn.Object(
            current_app.config["LETTER_ATTACHMENT_BUCKET_NAME"],
            get_transient_letter_file_location(service_id, attachment_id),
        )
        .get()["Body"]
        .read()
    )
    assert base64.b64decode(sanitise_spy.spy_return["file"].encode()) == sanitised_file_contents


@mock_aws
def test_recreate_pdf_for_template_letter_attachments_with_s3_error(client, caplog):
    # create the backup S3 bucket, which is empty so will cause an error when attempting to download the file
    conn = boto3.resource("s3", region_name=current_app.config["AWS_REGION"])
    conn.create_bucket(
        Bucket=current_app.config["PRECOMPILED_ORIGINALS_BACKUP_LETTER_BUCKET_NAME"],
        CreateBucketConfiguration={"LocationConstraint": "eu-west-1"},
    )

    service_id = str(uuid.uuid4())
    attachment_id = str(uuid.uuid4())

    with caplog.at_level(logging.ERROR):
        recreate_pdf_for_template_letter_attachments(service_id, attachment_id, "1234-abcd.pdf")

    assert (
        f"Error downloading file from backup bucket or uploading to letters-attachment bucket for "
        f"attachment {attachment_id}"
    ) in caplog.messages


@mock_aws
def test_recreate_pdf_for_template_letter_attachments_that_fails_validation(client, caplog):
    # create backup S3 bucket and an S3 bucket for the sanitised attachment letters
    conn = boto3.resource("s3", region_name=current_app.config["AWS_REGION"])
    backup_bucket = conn.create_bucket(
        Bucket=current_app.config["PRECOMPILED_ORIGINALS_BACKUP_LETTER_BUCKET_NAME"],
        CreateBucketConfiguration={"LocationConstraint": "eu-west-1"},
    )
    final_letters_bucket = conn.create_bucket(
        Bucket=current_app.config["LETTER_ATTACHMENT_BUCKET_NAME"],
        CreateBucketConfiguration={"LocationConstraint": "eu-west-1"},
    )

    service_id = str(uuid.uuid4())
    attachment_id = str(uuid.uuid4())

    # put an invalid PDF in the backup S3 bucket so that it fails sanitisation
    invalid_file = BytesIO(no_colour)
    s3 = boto3.client("s3", region_name="eu-west-1")
    s3.put_object(
        Bucket=current_app.config["PRECOMPILED_ORIGINALS_BACKUP_LETTER_BUCKET_NAME"],
        Key=f"{attachment_id}.pdf",
        Body=invalid_file.read(),
    )

    with caplog.at_level(logging.ERROR):
        recreate_pdf_for_template_letter_attachments(service_id, attachment_id, "1234-abcd.pdf")

    # the original file has not been copied or moved
    assert [o.key for o in backup_bucket.objects.all()] == [f"{attachment_id}.pdf"]
    assert len(list(final_letters_bucket.objects.all())) == 0

    assert f"Attachment failed resanitisation id: {attachment_id}" in caplog.messages
