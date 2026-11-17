import json
import uuid
from io import BytesIO
from unittest.mock import Mock, patch

import pytest
from flask import url_for
from freezegun import freeze_time
from notifications_utils.s3 import S3ObjectNotFound
from weasyprint.pdf import fonts as weasyprint_fonts

from app.preview import get_html
from tests.conftest import cache_response_body
from tests.pdf_consts import cmyk_and_rgb_images_in_one_pdf, multi_page_pdf

# upstream's tests use 50 sentences for a two-page letter, but the NL letter layout fits those on one page.
# Anything from 60 to 150 sentences makes two pages; 100 leaves room for small layout changes.
TWO_PAGE_SENTENCE_COUNT = 100


@pytest.fixture
def view_letter_template_pdf(client, auth_header, view_letter_template_request_data):
    # same as the fixture in test_preview_templated.py
    return lambda data=view_letter_template_request_data, headers=auth_header: client.post(
        url_for("preview_blueprint.view_letter_template_pdf", filetype="pdf"),
        data=json.dumps(data),
        headers={"Content-type": "application/json", **headers},
    )


@pytest.fixture
def view_letter_template_png(client, auth_header, view_letter_template_request_data):
    # same as the fixture in test_preview_templated.py
    return lambda data=view_letter_template_request_data, headers=auth_header: client.post(
        url_for("preview_blueprint.view_letter_template_png"),
        data=json.dumps(data),
        headers={"Content-type": "application/json", **headers},
    )


@freeze_time("2012-12-12")
def test_get_pdf_caches_with_correct_keys(
    app,
    mocker,
    view_letter_template_pdf,
    mock_weasyprint_logos,
    mocked_cache_get,
    mocked_cache_set,
):
    expected_cache_key = "templated/104a3aacb32d72405c4ac36ed4a20ec86692051d.pdf"
    resp = view_letter_template_pdf()

    assert resp.status_code == 200
    assert resp.headers["Content-Type"] == "application/pdf"
    assert resp.get_data().startswith(b"%PDF-1.7")
    mocked_cache_get.assert_called_once_with("test-template-preview-cache", expected_cache_key)
    assert mocked_cache_set.call_count == 1
    mocked_cache_set.call_args[0][0].seek(0)
    assert mocked_cache_set.call_args[0][0].read() == resp.get_data()
    assert mocked_cache_set.call_args[0][1] == "eu-west-1"
    assert mocked_cache_set.call_args[0][2] == "test-template-preview-cache"
    assert mocked_cache_set.call_args[0][3] == expected_cache_key


@freeze_time("2012-12-12")
def test_get_png_caches_with_correct_keys(
    app,
    mocker,
    view_letter_template_png,
    mock_weasyprint_logos,
    mocked_cache_get,
    mocked_cache_set,
):
    if weasyprint_fonts.harfbuzz_subset is None:
        pytest.fail(
            "WeasyPrint is using the fontTools fallback; run the tests in the Dockerfile_nl test image "
            "(libharfbuzz-subset0)"
        )

    # the key hashes the rendered PDF, including font subsets made by the Dockerfile_nl image's HarfBuzz,
    # so a HarfBuzz or font package change in that image changes it
    expected_cache_key = "pngs/1953faafc5949b0ca76a7d25d5ece53097539b4b.png"
    resp = view_letter_template_png()

    assert resp.status_code == 200
    assert resp.headers["Content-Type"] == "image/png"
    assert resp.get_data().startswith(b"\x89PNG")

    assert mocked_cache_get.call_count == 2
    assert mocked_cache_get.call_args_list[1][0][0] == "test-template-preview-cache"
    assert mocked_cache_get.call_args_list[1][0][1] == expected_cache_key
    assert mocked_cache_set.call_count == 2
    mocked_cache_set.call_args_list[1][0][0].seek(0)
    assert mocked_cache_set.call_args_list[1][0][0].read() == resp.get_data()
    assert mocked_cache_set.call_args_list[1][0][1] == "eu-west-1"
    assert mocked_cache_set.call_args_list[1][0][2] == "test-template-preview-cache"
    assert mocked_cache_set.call_args_list[1][0][3] == expected_cache_key


@pytest.mark.parametrize(
    "page_number, expected_response_code",
    [
        (2, 200),
        (3, 400),
    ],
)
def test_view_letter_template_png_route_gets_png_for_page(
    client,
    auth_header,
    mock_weasyprint_logos,
    page_number,
    expected_response_code,
    mocker,
):
    mocked_hide_notify = mocker.patch("app.preview.hide_notify_tag")
    response = client.post(
        url_for(
            "preview_blueprint.view_letter_template_png",
            page=page_number,
        ),
        data=json.dumps(
            {
                "letter_contact_block": "123",
                "template": {
                    "id": str(uuid.uuid4()),
                    "template_type": "letter",
                    "subject": "letter subject",
                    "content": ("All work and no play makes Jack a dull boy. " * TWO_PAGE_SENTENCE_COUNT),
                    "version": 1,
                },
                "values": {},
                "filename": "hm-government",
            }
        ),
        headers={"Content-type": "application/json", **auth_header},
    )
    assert response.status_code == expected_response_code
    assert not mocked_hide_notify.called


def test_letter_template_constructed_properly_for_pdf(view_letter_template_request_data, view_letter_template_pdf):
    with patch("app.preview.LetterPreviewTemplate", __str__=Mock(return_value="foo")) as mock_template:
        resp = view_letter_template_pdf()
        assert resp.status_code == 200

    mock_template.assert_called_once_with(
        view_letter_template_request_data["template"],
        values=view_letter_template_request_data["values"],
        contact_block=view_letter_template_request_data["letter_contact_block"],
        admin_base_url="https://static-logos.notify.tools/letters",
        logo_file_name="hm-government.svg",
        date=None,
        language="english",
        includes_first_page=True,
        letter_address_placement="60mm",
    )


def test_POST_page_count(client, mock_weasyprint_logos, auth_header):
    response = client.post(
        url_for("preview_blueprint.page_count"),
        data=json.dumps(
            {
                "letter_contact_block": "123",
                "template": {
                    "id": str(uuid.uuid4()),
                    "template_type": "letter",
                    "subject": "letter subject",
                    "content": ("All work and no play makes Jack a dull boy. " * TWO_PAGE_SENTENCE_COUNT),
                    "letter_welsh_content": None,
                    "letter_welsh_subject": None,
                    "letter_languages": "english",
                    "version": 1,
                    "letter_attachment": None,
                },
                "values": {},
                "filename": "hm-government",
            }
        ),
        headers={"Content-type": "application/json", **auth_header},
    )
    assert response.status_code == 200

    assert json.loads(response.get_data(as_text=True)) == {
        "count": 2,
        "attachment_page_count": 0,
        "welsh_page_count": 0,
    }


@freeze_time("2012-12-12")
def test_page_count_from_cache(client, auth_header, mocker, mocked_cache_get):
    mocked_cache_get.side_effect = [cache_response_body(multi_page_pdf)]
    mocker.patch(
        "app.preview.HTML",
        side_effect=AssertionError("Uncached method shouldn’t be called"),
    )
    response = client.post(
        url_for("preview_blueprint.page_count"),
        data=json.dumps(
            {
                "letter_contact_block": "123",
                "template": {
                    "id": str(uuid.uuid4()),
                    "template_type": "letter",
                    "subject": "letter subject",
                    "content": " letter content",
                    "letter_attachment": None,
                },
                "values": {},
                "filename": "hm-government",
            }
        ),
        headers={"Content-type": "application/json", **auth_header},
    )
    assert mocked_cache_get.call_args[0][0] == "test-template-preview-cache"
    assert mocked_cache_get.call_args[0][1] == "templated/d91e4d3c06dcb24b8ff7af0aefbdc3bb86343ed4.pdf"
    assert response.status_code == 200
    assert json.loads(response.get_data(as_text=True)) == {
        "count": 10,
        "attachment_page_count": 0,
        "welsh_page_count": 0,
    }


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
