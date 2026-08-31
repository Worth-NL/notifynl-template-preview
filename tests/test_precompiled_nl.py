from io import BytesIO
from unittest.mock import MagicMock

import pytest
from flask import url_for

from app import ValidationFailed
from app.precompiled import (
    _other_letter_address_placement,
    extract_address_block,
    rewrite_address_block,
)
from tests.pdf_consts import (
    address_50mm_no_retouradres,
    address_50mm_with_retouradres,
    address_60mm_no_retouradres,
    address_60mm_with_retouradres,
    address_with_retouradres_missing_city,
    address_with_retouradres_missing_postcode,
    bad_postcode,
)


@pytest.mark.parametrize(
    "letter_address_placement, expected_other",
    (
        ("50mm", "60mm"),
        ("60mm", "50mm"),
        # Unrecognised/None falls back to "50mm", matching what the PRIMARY extraction
        # would have actually used ("60mm", via DEFAULT_LETTER_ADDRESS_PLACEMENT) - see
        # _other_letter_address_placement's docstring.
        (None, "50mm"),
        ("not-a-real-placement", "50mm"),
    ),
)
def test_other_letter_address_placement(letter_address_placement, expected_other):
    assert _other_letter_address_placement(letter_address_placement) == expected_other


def _mock_address(error_code):
    address = MagicMock()
    address.error_code = error_code
    return address


def test_rewrite_address_block_does_not_retry_when_primary_extraction_succeeds(mocker):
    mock_extract = mocker.patch("app.precompiled.extract_address_block", return_value=_mock_address(None))
    pdf = BytesIO(b"pdf")

    rewrite_address_block(
        pdf,
        page_count=1,
        allow_international_letters=False,
        filename="file",
        letter_address_placement="50mm",
    )

    mock_extract.assert_called_once_with(pdf, letter_address_placement="50mm")


def test_rewrite_address_block_does_not_retry_for_a_non_candidate_error_code(mocker):
    mock_extract = mocker.patch(
        "app.precompiled.extract_address_block", return_value=_mock_address("too-many-address-lines")
    )
    pdf = BytesIO(b"pdf")

    with pytest.raises(ValidationFailed) as error:
        rewrite_address_block(
            pdf,
            page_count=1,
            allow_international_letters=False,
            filename="file",
            letter_address_placement="50mm",
        )

    assert error.value.message == "too-many-address-lines"
    mock_extract.assert_called_once_with(pdf, letter_address_placement="50mm")


@pytest.mark.parametrize("candidate_error_code", ("not-enough-address-lines", "address-is-empty"))
def test_rewrite_address_block_raises_placement_mismatch_when_retry_at_other_placement_is_fully_valid(
    mocker, candidate_error_code
):
    mocker.patch(
        "app.precompiled.extract_address_block",
        side_effect=[_mock_address(candidate_error_code), _mock_address(None)],
    )

    with pytest.raises(ValidationFailed) as error:
        rewrite_address_block(
            BytesIO(b"pdf"),
            page_count=1,
            allow_international_letters=False,
            filename="file",
            letter_address_placement="50mm",
        )

    assert error.value.message == "address-placement-mismatch"


def test_rewrite_address_block_keeps_original_error_code_when_retry_also_fails(mocker):
    mocker.patch(
        "app.precompiled.extract_address_block",
        side_effect=[_mock_address("not-enough-address-lines"), _mock_address("not-a-real-uk-postcode")],
    )

    with pytest.raises(ValidationFailed) as error:
        rewrite_address_block(
            BytesIO(b"pdf"),
            page_count=1,
            allow_international_letters=False,
            filename="file",
            letter_address_placement="50mm",
        )

    assert error.value.message == "not-enough-address-lines"


def test_sanitise_precompiled_letter_with_bad_address_returns_placement_mismatch_or_original_code(client, auth_header):
    # NL version of tests/test_precompiled.py::test_sanitise_precompiled_letter_with_bad_address_returns_400,
    # which is skipped upstream (reason="[NOTIFYNL] Broken by validation change") - its fixtures never passed
    # an explicit letter_address_placement, so they now default to "60mm" against fixtures laid out at
    # "50mm". Explicitly passing "50mm" here (matching the fixtures' actual layout) restores real coverage.
    response = client.post(
        url_for("precompiled_blueprint.sanitise_precompiled_letter", letter_address_placement="50mm"),
        data=bad_postcode,
        headers={"Content-type": "application/json", **auth_header},
    )

    assert response.status_code == 400
    assert response.json["message"] == "not-a-real-uk-postcode"


def test_sanitise_precompiled_letter_returns_address_placement_mismatch_end_to_end(client, auth_header, mocker):
    mocker.patch(
        "app.precompiled.extract_address_block",
        side_effect=[_mock_address("not-enough-address-lines"), _mock_address(None)],
    )

    response = client.post(
        url_for("precompiled_blueprint.sanitise_precompiled_letter", letter_address_placement="50mm"),
        data=bad_postcode,
        headers={"Content-type": "application/json", **auth_header},
    )

    assert response.status_code == 400
    assert response.json["message"] == "address-placement-mismatch"


# Note: tests/test_precompiled.py also has a second test skipped for the same stated reason,
# test_rewrite_address_block_end_to_end (line ~736, reason="[NOTIFYNL] Broken by validation
# change"). Investigated during this session: passing letter_address_placement="50mm" explicitly
# does NOT fix it - both its fixtures (example_dwp_pdf, valid_letter) still fail with
# "not-a-real-uk-postcode" even at the placement matching their actual layout, e.g.
# example_dwp_pdf extracts a plausibly-formatted postcode ("TS7 1NG") that the real-UK-postcode
# validator nonetheless rejects. That's a genuine, pre-existing, placement-unrelated bug, out of
# scope for this fix - left skipped upstream rather than "fixed" here with a misleading test.


@pytest.mark.parametrize(
    "pdf, letter_address_placement",
    [
        (address_50mm_no_retouradres, "50mm"),
        (address_60mm_no_retouradres, "60mm"),
        (address_50mm_with_retouradres, "50mm"),
        (address_60mm_with_retouradres, "60mm"),
    ],
)
def test_extract_address_block_valid_with_and_without_retouradres_line(pdf, letter_address_placement):
    address = extract_address_block(BytesIO(pdf), letter_address_placement=letter_address_placement)

    assert address.error_code is None
    assert address.normalised_lines == ["Persoonlijk", "Coolsingel 40", "3011 AD  ROTTERDAM"]


def test_extract_address_block_retouradres_does_not_mask_missing_city():
    address = extract_address_block(BytesIO(address_with_retouradres_missing_city), letter_address_placement="50mm")

    assert address.error_code is not None


def test_extract_address_block_retouradres_does_not_mask_missing_postcode():
    address = extract_address_block(BytesIO(address_with_retouradres_missing_postcode), letter_address_placement="50mm")

    assert address.error_code is not None


def test_add_address_to_precompiled_letter_with_retouradres_extracts_untouched_raw_text():
    # The Retouradres line is only stripped during PostalAddress parsing, not at extraction -
    # .raw_address should still contain it verbatim.
    address = extract_address_block(BytesIO(address_50mm_with_retouradres), letter_address_placement="50mm")

    assert address.raw_address.startswith("Retouradres: Postbus 70013, 3000 KR ROTTERDAM")
