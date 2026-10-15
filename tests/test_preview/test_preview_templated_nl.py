import json
import uuid
from io import BytesIO

import pytest
from flask import url_for
from notifications_utils.s3 import S3ObjectNotFound

from app.preview import get_html
from tests.conftest import cache_response_body
from tests.pdf_consts import cmyk_and_rgb_images_in_one_pdf, multi_page_pdf


@pytest.fixture
def view_letter_template_pdf(client, auth_header, view_letter_template_request_data):
    # same as the fixture in test_preview_templated.py
    return lambda data=view_letter_template_request_data, headers=auth_header: client.post(
        url_for("preview_blueprint.view_letter_template_pdf", filetype="pdf"),
        data=json.dumps(data),
        headers={"Content-type": "application/json", **headers},
    )


@pytest.mark.parametrize(
    "attachment_cache,number_of_cache_get_calls,number_of_cache_set_calls",
    [
        # attachment not cached
        (S3ObjectNotFound({}, ""), 2, 1),
        # attachment is cached
        (cache_response_body(), 2, 0),
    ],
)
def test_view_letter_template_png_with_attachment_hits_cache_correct_number_of_times(
    client,
    mocker,
    auth_header,
    mocked_cache_get,
    mocked_cache_set,
    attachment_cache,
    number_of_cache_get_calls,
    number_of_cache_set_calls,
):
    mocked_cache_get.side_effect = [cache_response_body(data=b"\x00"), attachment_cache]

    mocker.patch("app.templated.add_attachments_to_letter", return_value=BytesIO(multi_page_pdf))

    response = client.post(
        url_for(
            "preview_blueprint.view_letter_template_png",
            page=2,
        ),
        data=json.dumps(
            {
                "letter_contact_block": "123",
                "template": {
                    "id": str(uuid.uuid4()),
                    "template_type": "letter",
                    "subject": "letter subject",
                    "content": "All work and no play makes Jack a dull boy. ",
                    "version": 1,
                    "letter_attachment": {"page_count": 1, "id": "1234"},
                    "service": "5678",
                },
                "values": {},
                "filename": "hm-government",
            }
        ),
        headers={"Content-type": "application/json", **auth_header},
    )

    assert response.status_code == 200
    assert response.headers["Content-Type"] == "image/png"
    assert mocked_cache_get.call_count == number_of_cache_get_calls
    assert mocked_cache_set.call_count == number_of_cache_set_calls


@pytest.mark.parametrize("letter_attachment, requested_page", [(None, 2), ({"page_count": 1, "id": "1234"}, 3)])
def test_view_letter_template_png_when_requested_page_out_of_range(
    client, auth_header, mock_weasyprint_logos, mocker, mocked_cache_get, letter_attachment, requested_page
):
    mocker.patch("app.preview.hide_notify_tag")
    mocker.patch(
        "app.templated.add_attachments_to_letter", return_value=BytesIO(cmyk_and_rgb_images_in_one_pdf)
    )  # 2-page PDF
    response = client.post(
        url_for(
            "preview_blueprint.view_letter_template_png",
            page=requested_page,
        ),
        data=json.dumps(
            {
                "letter_contact_block": "123",
                "template": {
                    "id": str(uuid.uuid4()),
                    "template_type": "letter",
                    "subject": "letter subject",
                    "content": ("All work and no play makes Jack a dull boy. "),
                    "version": 1,
                    "letter_attachment": letter_attachment,
                    "service": "5678",
                },
                "values": {},
                "filename": "hm-government",
            }
        ),
        headers={"Content-type": "application/json", **auth_header},
    )
    assert response.status_code == 400
    assert response.json["message"] == f"400 Bad Request: Letter does not have a page {requested_page}"


def test_view_letter_template_pdf_adds_attachment(mocker, view_letter_template_request_data, view_letter_template_pdf):
    mock_get_pdf = mocker.patch("app.preview.get_pdf", return_value=BytesIO(b"templated letter pdf"))
    mock_add_attachments_to_letter = mocker.patch(
        "app.templated.add_attachments_to_letter", return_value=BytesIO(b"combined pdf")
    )

    view_letter_template_request_data["template"]["letter_attachment"] = {"page_count": 1, "id": "5678"}

    resp = view_letter_template_pdf(data=view_letter_template_request_data)

    assert resp.status_code == 200
    assert resp.get_data() == b"combined pdf"
    mock_add_attachments_to_letter.assert_called_once_with(
        service_id="1234",
        templated_letter_pdf=mock_get_pdf.return_value,
        fixed_attachment={"page_count": 1, "id": "5678"},
        adhoc_attachment_pdfs=[],
    )


@pytest.mark.parametrize(
    "letter_address_placement, expected_class",
    [
        ("60mm", "pingen"),
        ("50mm", ""),
        # None (unspecified) now defaults to "60mm", matching Service.letter_address_placement's
        # own DB default - see notifynl-utils' LetterPreviewTemplate default.
        (None, "pingen"),
    ],
)
def test_get_html_applies_letter_address_placement(
    view_letter_template_request_data, letter_address_placement, expected_class, client
):
    view_letter_template_request_data["filename"] = None
    view_letter_template_request_data["letter_address_placement"] = letter_address_placement

    output_html = get_html(view_letter_template_request_data)

    assert f'class="recipient-address align-with-envelope-window {expected_class}">' in output_html


def test_get_html_defaults_letter_address_placement_to_60mm_when_key_absent(view_letter_template_request_data, client):
    view_letter_template_request_data["filename"] = None
    assert "letter_address_placement" not in view_letter_template_request_data

    output_html = get_html(view_letter_template_request_data)

    assert 'class="recipient-address align-with-envelope-window pingen">' in output_html
