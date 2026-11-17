from base64 import b64encode
from hashlib import sha1
from io import BytesIO

import pytest
from flask import url_for

from tests.pdf_consts import blank_with_address, valid_letter


@pytest.mark.parametrize(
    "pdf_file, expected_cache_key",
    (
        (valid_letter, "pngs/60152acab0bd269d382658c52306030eb0716f35.png"),
        (blank_with_address, "pngs/fcb425959a33f1447079125de5dd8685b1a67929.png"),
    ),
    ids=[
        "valid_letter",
        "blank_with_address",
    ],
)
def test_precompiled_pdf_returns_png_from_cache(
    app,
    client,
    auth_header,
    mocked_cache_get,
    mocked_cache_set,
    pdf_file,
    expected_cache_key,
):
    mocked_cache_get.side_effect = None
    mocked_cache_get.return_value = BytesIO(b"\x00")

    response = client.post(
        url_for("preview_blueprint.view_precompiled_letter"),
        data=b64encode(pdf_file),
        headers={"Content-type": "application/json", **auth_header},
    )

    assert response.status_code == 200
    assert response.headers["Content-Type"] == "image/png"
    assert response.get_data() == b"\x00"
    mocked_cache_get.assert_called_once_with(
        "test-template-preview-cache",
        expected_cache_key,
    )
    assert mocked_cache_set.call_args_list == []


def test_precompiled_pdf_caches_entire_contents_of_page(
    app,
    client,
    auth_header,
    mocked_cache_get,
    mocker,
):
    mock_sha1 = mocker.patch("app.sha1", wraps=sha1)

    client.post(
        url_for("preview_blueprint.view_precompiled_letter"),
        data=b64encode(valid_letter),
        headers={"Content-type": "application/json", **auth_header},
    )

    data_to_be_hashed = mock_sha1.call_args_list[0][0][0]

    assert data_to_be_hashed.startswith(b"JVBERi0xL")
    assert data_to_be_hashed.endswith(b"False")  # The hide_notify argument

    assert len(valid_letter) == 23_218
    assert len(data_to_be_hashed) > len(valid_letter)  # Hashed data should still be larger


def test_precompiled_pdf_caches_png_to_s3(
    app,
    client,
    auth_header,
    mocker,
    mocked_cache_get,
    mocked_cache_set,
):
    response = client.post(
        url_for("preview_blueprint.view_precompiled_letter"),
        data=b64encode(valid_letter),
        headers={"Content-type": "application/json", **auth_header},
    )

    assert response.status_code == 200
    assert response.headers["Content-Type"] == "image/png"
    assert response.get_data().startswith(b"\x89PNG")
    mocked_cache_get.assert_called_once_with(
        "test-template-preview-cache",
        "pngs/60152acab0bd269d382658c52306030eb0716f35.png",
    )
    mocked_cache_set.call_args[0][0].seek(0)
    assert mocked_cache_set.call_args[0][0].read() == response.get_data()
    assert mocked_cache_set.call_args[0][1] == "eu-west-1"
    assert mocked_cache_set.call_args[0][2] == "test-template-preview-cache"
    assert mocked_cache_set.call_args[0][3] == "pngs/60152acab0bd269d382658c52306030eb0716f35.png"
