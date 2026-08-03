import base64
from io import BytesIO

import pytest
from flask import current_app

from app import ValidationFailed
from app.letter_attachments import add_attachments_to_letter, get_adhoc_attachment_pdfs
from app.preview import get_page_count_for_pdf
from tests.pdf_consts import blank_page, valid_letter


def _page_count(pdf_bytes):
    return get_page_count_for_pdf(BytesIO(pdf_bytes))


def _sanitise_result(page_count, file_bytes):
    return {
        "recipient_address": None,
        "page_count": page_count,
        "message": None,
        "invalid_pages": None,
        "file": base64.b64encode(file_bytes).decode("utf-8"),
    }


def test_add_attachments_to_letter_with_fixed_only(mocker):
    mock_get_attachment = mocker.patch("app.letter_attachments.get_attachment_pdf", return_value=BytesIO(blank_page))

    response = add_attachments_to_letter(
        "1234", BytesIO(valid_letter), fixed_attachment={"page_count": 1, "id": "5678"}, adhoc_attachment_pdfs=[]
    )

    mock_get_attachment.assert_called_once_with("1234", "5678")
    assert get_page_count_for_pdf(response) == _page_count(valid_letter) + _page_count(blank_page)


def test_add_attachments_to_letter_with_adhoc_only(mocker):
    mock_get_attachment = mocker.patch("app.letter_attachments.get_attachment_pdf")

    response = add_attachments_to_letter(
        "1234", BytesIO(valid_letter), fixed_attachment=None, adhoc_attachment_pdfs=[BytesIO(blank_page)]
    )

    assert not mock_get_attachment.called
    assert get_page_count_for_pdf(response) == _page_count(valid_letter) + _page_count(blank_page)


def test_add_attachments_to_letter_with_fixed_and_adhoc_preserves_order(mocker):
    mocker.patch("app.letter_attachments.get_attachment_pdf", return_value=BytesIO(blank_page))

    response = add_attachments_to_letter(
        "1234",
        BytesIO(valid_letter),
        fixed_attachment={"page_count": 1, "id": "5678"},
        adhoc_attachment_pdfs=[BytesIO(blank_page), BytesIO(blank_page)],
    )

    # order: generated letter -> fixed attachment -> ad-hoc attachments, in submission order
    assert get_page_count_for_pdf(response) == _page_count(valid_letter) + 3 * _page_count(blank_page)


def test_add_attachments_to_letter_with_neither_is_a_passthrough(mocker):
    mock_get_attachment = mocker.patch("app.letter_attachments.get_attachment_pdf")

    response = add_attachments_to_letter("1234", BytesIO(valid_letter), fixed_attachment=None, adhoc_attachment_pdfs=[])

    assert not mock_get_attachment.called
    assert get_page_count_for_pdf(response) == _page_count(valid_letter)


def test_get_adhoc_attachment_pdfs_skips_sanitisation_for_test_key(mocker, client):
    mock_download = mocker.patch("app.letter_attachments.s3download", return_value=BytesIO(b"raw-bytes"))
    mock_sanitise = mocker.patch("app.precompiled.sanitise_file_contents")

    result = get_adhoc_attachment_pdfs(
        ["abc-123/attachment-1.pdf"], is_test_key=True, allow_international_letters=False
    )

    mock_download.assert_called_once_with(current_app.config["LETTERS_SCAN_BUCKET_NAME"], "abc-123/attachment-1.pdf")
    assert not mock_sanitise.called
    assert result[0].read() == b"raw-bytes"


def test_get_adhoc_attachment_pdfs_sanitises_for_real_key(mocker, client):
    mocker.patch("app.letter_attachments.s3download", return_value=BytesIO(b"raw-bytes"))
    mock_sanitise = mocker.patch(
        "app.precompiled.sanitise_file_contents",
        return_value=_sanitise_result(1, blank_page),
    )

    result = get_adhoc_attachment_pdfs(
        ["abc-123/attachment-1.pdf"], is_test_key=False, allow_international_letters=True
    )

    mock_sanitise.assert_called_once_with(
        b"raw-bytes",
        allow_international_letters=True,
        filename="abc-123/attachment-1.pdf",
        is_an_attachment=True,
    )
    assert result[0].read() == blank_page


def test_get_adhoc_attachment_pdfs_processes_multiple_keys_in_order(mocker, client):
    mocker.patch(
        "app.letter_attachments.s3download",
        side_effect=[BytesIO(b"raw-1"), BytesIO(b"raw-2")],
    )
    mocker.patch(
        "app.precompiled.sanitise_file_contents",
        side_effect=[_sanitise_result(1, valid_letter), _sanitise_result(1, blank_page)],
    )

    result = get_adhoc_attachment_pdfs(
        ["abc-123/attachment-1.pdf", "abc-123/attachment-2.pdf"],
        is_test_key=False,
        allow_international_letters=False,
    )

    assert result[0].read() == valid_letter
    assert result[1].read() == blank_page


def test_get_adhoc_attachment_pdfs_raises_validation_failed_on_sanitisation_failure(mocker, client):
    mocker.patch("app.letter_attachments.s3download", return_value=BytesIO(b"raw-bytes"))
    mocker.patch(
        "app.precompiled.sanitise_file_contents",
        return_value={
            "recipient_address": None,
            "page_count": 2,
            "message": "content-outside-printable-area",
            "invalid_pages": [1],
            "file": None,
        },
    )

    with pytest.raises(ValidationFailed) as exc_info:
        get_adhoc_attachment_pdfs(["abc-123/attachment-1.pdf"], is_test_key=False, allow_international_letters=False)

    assert exc_info.value.message == "content-outside-printable-area"
    assert exc_info.value.invalid_pages == [1]
    assert exc_info.value.page_count == 2


def test_get_adhoc_attachment_pdfs_stops_at_first_failing_attachment(mocker, client):
    mock_download = mocker.patch(
        "app.letter_attachments.s3download",
        side_effect=[BytesIO(b"raw-1"), BytesIO(b"raw-2")],
    )
    mocker.patch(
        "app.precompiled.sanitise_file_contents",
        return_value={
            "recipient_address": None,
            "page_count": 1,
            "message": "content-outside-printable-area",
            "invalid_pages": [1],
            "file": None,
        },
    )

    with pytest.raises(ValidationFailed):
        get_adhoc_attachment_pdfs(
            ["abc-123/attachment-1.pdf", "abc-123/attachment-2.pdf"],
            is_test_key=False,
            allow_international_letters=False,
        )

    # the second attachment is never even downloaded, since the first already failed
    mock_download.assert_called_once_with(current_app.config["LETTERS_SCAN_BUCKET_NAME"], "abc-123/attachment-1.pdf")
