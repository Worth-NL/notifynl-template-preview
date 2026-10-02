import unicodedata
import uuid
from io import BytesIO
from unittest.mock import MagicMock

import pymupdf
import pytest
from flask import url_for
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

from app import ValidationFailed
from app.precompiled import (
    ADDRESS_LEFT_FROM_LEFT_OF_PAGE,
    NOTIFY_TAG_BOUNDING_BOX,
    _other_letter_address_placement,
    check_notify_tag_area_for_encroachment,
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


NL_LETTERS_WITH_PLACEMENT = [
    (address_50mm_no_retouradres, "50mm"),
    (address_60mm_no_retouradres, "60mm"),
    (address_50mm_with_retouradres, "50mm"),
    (address_60mm_with_retouradres, "60mm"),
]


@pytest.mark.parametrize("pdf, letter_address_placement", NL_LETTERS_WITH_PLACEMENT)
def test_check_notify_tag_area_for_encroachment_passes_nl_letters(pdf, letter_address_placement):
    assert check_notify_tag_area_for_encroachment(BytesIO(pdf)) is None


@pytest.mark.parametrize("pdf, letter_address_placement", NL_LETTERS_WITH_PLACEMENT)
def test_sanitise_precompiled_nl_letter_does_not_log_notify_tag_area_encroachment(
    client, auth_header, caplog, pdf, letter_address_placement
):
    response = client.post(
        url_for("precompiled_blueprint.sanitise_precompiled_letter")
        + f"?upload_id={uuid.uuid4()}&letter_address_placement={letter_address_placement}",
        data=pdf,
        headers={"Content-type": "application/json", **auth_header},
    )

    assert response.status_code == 200
    assert not [message for message in caplog.messages if "encroaching on the Notify tag area" in message]


@pytest.mark.parametrize(
    "encroaching_character, expected_logged_result",
    [
        ("Jan de Vries", "J"),  # only the first character is logged, to avoid logging PII
        (" ", " "),
        ("  ", " "),
        ("\t", "\\t"),
    ],
)
def test_sanitise_precompiled_nl_letter_with_invisible_characters_encroaching_on_notify_tag_area_logging(
    client, auth_header, caplog, encroaching_character, expected_logged_result
):
    filename = str(uuid.uuid4())
    letter = pymupdf.open(stream=address_50mm_no_retouradres, filetype="PDF")
    letter[0].insert_text(
        (NOTIFY_TAG_BOUNDING_BOX.x0 + 5, NOTIFY_TAG_BOUNDING_BOX.y0 + 10),
        encroaching_character,
        fontsize=10,
        fontname="helv",
        render_mode=3,  # invisible text
    )
    letter_data = letter.tobytes()
    letter.close()

    response = client.post(
        url_for("precompiled_blueprint.sanitise_precompiled_letter")
        + f"?upload_id={filename}&letter_address_placement=50mm",
        data=letter_data,
        headers={"Content-type": "application/json", **auth_header},
    )

    assert response.status_code == 200
    assert (
        f"precompiled pdf:({filename}) has character:('{expected_logged_result}'), encroaching on the Notify tag area."
        in caplog.messages
    )


SIX_LINE_DUTCH_ADDRESS = [
    "Mevrouw A.B. van den Berg-de Jong",
    "Stichting Wijkcentrum De Linde",
    "t.a.v. de financiële administratie",
    "Kamer 2.14",
    "Lange Voorhout 12 A",
    "2514 EE  DEN HAAG",
]


def _six_line_address_letter(letter_address_placement, split_first_line=None, split_second_line_font_sizes=False):
    """A letter with SIX_LINE_DUTCH_ADDRESS in the address window for the given placement.

    split_first_line draws "Mevrouw" and the rest of line 1 as two separate text pieces, that many mm
    apart (like mail-merge fields). split_second_line_font_sizes draws line 2 in two font sizes.
    """
    buffer = BytesIO()
    letter = canvas.Canvas(buffer, pagesize=A4)
    x = ADDRESS_LEFT_FROM_LEFT_OF_PAGE * mm
    top = float(letter_address_placement.removesuffix("mm"))
    for i, line in enumerate(SIX_LINE_DUTCH_ADDRESS):
        y = A4[1] - (top + 5 + i * 5) * mm
        letter.setFont("Helvetica", 9)
        if i == 0 and split_first_line is not None:
            first, rest = line.split(" ", 1)
            letter.drawString(x, y, first)
            letter.drawString(x + letter.stringWidth(f"{first} ", "Helvetica", 9) + split_first_line * mm, y, rest)
        elif i == 1 and split_second_line_font_sizes:
            first, rest = line.split(" De ")
            letter.drawString(x, y, f"{first} ")
            letter.setFont("Helvetica-Bold", 11)
            letter.drawString(x + letter.stringWidth(f"{first} ", "Helvetica", 9), y, f"De {rest}")
        else:
            letter.drawString(x, y, line)
    letter.save()
    return buffer.getvalue()


@pytest.mark.parametrize("letter_address_placement", ["50mm", "60mm"])
def test_extract_address_block_reads_six_line_dutch_address(client, letter_address_placement):
    address = extract_address_block(
        BytesIO(_six_line_address_letter(letter_address_placement)),
        letter_address_placement=letter_address_placement,
    )

    assert address.error_code is None
    # extraction normalises to NFKD, which decomposes "ë" into "e" + a combining diaeresis
    assert address.normalised_lines == [unicodedata.normalize("NFKD", line) for line in SIX_LINE_DUTCH_ADDRESS]


@pytest.mark.parametrize("letter_address_placement", ["50mm", "60mm"])
def test_extract_address_block_keeps_line_with_mixed_font_sizes_together(client, letter_address_placement):
    address = extract_address_block(
        BytesIO(_six_line_address_letter(letter_address_placement, split_second_line_font_sizes=True)),
        letter_address_placement=letter_address_placement,
    )

    assert address.error_code is None
    assert address.normalised_lines[1] == "Stichting Wijkcentrum De Linde"


def test_extract_address_block_line_drawn_as_separate_pieces_is_split_and_logged(client, caplog):
    # Documents the known weakness of grouping by line: an address line drawn as separate text pieces
    # (e.g. mail-merge fields) becomes two lines, which pushes a 6-line address over the limit.
    # Grouping by y2 keeps it together, so the log line lets us see how often this happens.
    address = extract_address_block(
        BytesIO(_six_line_address_letter("50mm", split_first_line=10)),
        letter_address_placement="50mm",
    )

    assert address.error_code == "too-many-address-lines"
    assert "Address extraction different when splitting address by y2 vs by line" in caplog.messages
