import base64
import io
import unicodedata
import uuid
from io import BytesIO
from unittest.mock import ANY, MagicMock, call

import pymupdf
import pytest
from flask import url_for
from reportlab.lib.colors import black, white
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

from app import ValidationFailed
from app.precompiled import (
    ADDRESS_LEFT_FROM_LEFT_OF_PAGE,
    DEFAULT_LETTER_ADDRESS_PLACEMENT,
    NOTIFY_TAG_BOUNDING_BOX,
    _other_letter_address_placement,
    add_notify_tag_to_letter,
    check_notify_tag_area_for_encroachment,
    extract_address_block,
    get_invalid_pages_with_message,
    is_notify_tag_present,
    rewrite_address_block,
)
from tests.pdf_consts import (
    address_margin,
    bad_postcode,
    example_dwp_pdf,
    multi_page_pdf,
    no_resources_on_last_page,
)
from tests.pdf_consts_nl import (
    address_50mm_no_retouradres,
    address_50mm_with_retouradres,
    address_60mm_no_retouradres,
    address_60mm_with_retouradres,
    address_where_paragraphs_do_not_match_visual_order_nl,
    address_with_large_space_in_a_line_nl,
    address_with_multiple_unusual_coordinates_nl,
    address_with_retouradres_missing_city,
    address_with_retouradres_missing_postcode,
    address_with_unusual_coordinates_nl,
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


def test_sanitise_precompiled_letter_with_bad_address_returns_400(client, auth_header):
    # Upstream's version doesn't pass a letter_address_placement, so its fixtures (laid out at the 50mm position)
    # are validated against the "60mm" default window. Passing "50mm" here matches the fixtures' actual layout.
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
def test_sanitise_precompiled_letter_with_invisible_characters_encroaching_on_notify_tag_area_logging(
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


@pytest.mark.parametrize(
    "y_from_top",
    [
        39,  # flagged as "under the logo" upstream, but NL's logo area reaches down to the address block
        # regression test for the gap between LOGO_BOTTOM_FROM_TOP_OF_PAGE and ADDRESS_TOP_FROM_TOP_OF_PAGE left
        # uncovered after the 2025-10-02 margin change - LOGO_BOTTOM_FROM_TOP_OF_PAGE now equals
        # ADDRESS_TOP_FROM_TOP_OF_PAGE, so this point between the old logo/address boundaries is covered
        45,
    ],
)
def test_get_invalid_pages_allows_text_in_the_logo_area_down_to_the_address_block(client, y_from_top):
    packet = io.BytesIO()
    cv = canvas.Canvas(packet, pagesize=A4)
    cv.setStrokeColor(white)
    cv.setFillColor(white)
    cv.rect(0, 0, 1000, 1000, stroke=1, fill=1)

    cv.setStrokeColor(black)
    cv.setFillColor(black)
    cv.drawString(24.6 * mm, (297 - y_from_top) * mm, "This is a test string used to detect non white on a page")

    cv.save()
    packet.seek(0)
    assert get_invalid_pages_with_message(packet) == ("", [])


def test_get_invalid_pages_closes_gap_between_address_bottom_and_body_top_for_50mm_placement(client):
    """Regression test for the gap between address_bottom_from_top_of_page("50mm")
    (90mm) and the old bare BODY_TOP_FROM_TOP_OF_PAGE (95mm) - previously uncovered by
    _overlay_printable_areas_of_address_block_page_with_white, letting a "Datum"/"Onderwerp"
    block just below a 50mm-placed address window get wrongly flagged
    content-outside-printable-area (Den Haag SZW letter, 2026-08-31).
    """
    packet = io.BytesIO()
    cv = canvas.Canvas(packet, pagesize=A4)
    cv.setStrokeColor(white)
    cv.setFillColor(white)
    cv.rect(0, 0, 1000, 1000, stroke=1, fill=1)

    cv.setStrokeColor(black)
    cv.setFillColor(black)
    # x=60mm is inside the address column; y=92mm sits in the old gap between
    # address_bottom_from_top_of_page("50mm")=90mm and the old bare BODY_TOP=95mm.
    cv.drawString(60 * mm, (297 - 92) * mm, "This is a test string used to detect non white on a page")

    cv.save()
    packet.seek(0)
    assert get_invalid_pages_with_message(packet, letter_address_placement="50mm") == ("", [])


@pytest.mark.parametrize(
    ["params", "expected_first_page"],
    [
        ({"page_number": "1"}, True),
        ({"page_number": "2"}, False),
        ({"is_first_page": "true"}, True),
        ({"is_first_page": "anything_else"}, False),
        ({"is_first_page": ""}, False),
        ({"page_number": 1, "is_first_page": "true"}, True),  # is_first_page takes priority
        ({"page_number": "1", "is_an_attachment": True}, False),  # attachment doesn't mandate address block
    ],
)
def test_overlay_template_png_for_page_checks_if_first_page(client, auth_header, mocker, params, expected_first_page):
    mock_png_from_pdf = mocker.patch("app.precompiled.png_from_pdf", return_value=BytesIO(b"\x00"))
    mock_colour = mocker.patch("app.precompiled._colour_no_print_areas_of_single_page_pdf_in_red")

    response = client.post(
        url_for("precompiled_blueprint.overlay_template_png_for_page", **params),
        data=b"1234",
        headers={"Content-type": "application/json", **auth_header},
    )

    assert response.status_code == 200
    mock_colour.assert_called_once_with(
        ANY, is_first_page=expected_first_page, letter_address_placement=DEFAULT_LETTER_ADDRESS_PLACEMENT
    )
    mock_png_from_pdf.assert_called_once_with(mock_colour.return_value, page_number=1)


def test_overlay_template_pdf_colours_pages_in_red(client, auth_header, mocker):
    mock_colour = mocker.patch("app.precompiled._colour_no_print_areas_of_page_in_red")
    resp = client.post(
        url_for("precompiled_blueprint.overlay_template_pdf"),
        data=multi_page_pdf,
        headers=auth_header,
    )
    assert resp.status_code == 200

    assert (
        mock_colour.call_args_list
        == [call(ANY, is_first_page=True, letter_address_placement=DEFAULT_LETTER_ADDRESS_PLACEMENT)]
        + [call(ANY, is_first_page=False, letter_address_placement=DEFAULT_LETTER_ADDRESS_PLACEMENT)] * 9
    )


def test_precompiled_sanitise_pdf_with_colour_in_address_margin_returns_400(client, auth_header, mocker):
    # fixture's address block is laid out at the 50mm position
    response = client.post(
        url_for("precompiled_blueprint.sanitise_precompiled_letter", letter_address_placement="50mm"),
        data=address_margin,
        headers={"Content-type": "application/json", **auth_header},
    )

    assert response.status_code == 400
    assert response.json == {
        "page_count": 1,
        "recipient_address": None,
        "message": "content-outside-printable-area",
        "invalid_pages": [1],
        "file": None,
    }


def test_extract_address_block(client):
    # fixture's address block is laid out at the 50mm position
    assert extract_address_block(BytesIO(example_dwp_pdf), letter_address_placement="50mm").raw_address == "\n".join(
        [
            "MR J DOE",
            "13 TEST LANE",
            "TESTINGTON",
            "TS7 1NG",
        ]
    )


def test_sanitise_file_contents_on_pdf_with_no_resources_on_one_of_the_pages_content_outside_bounds(
    client, auth_header
):
    """
    This tests to make sure that pypdf doesn't raise a KeyError when sanitising a PDF that is missing /Resources
    on one of the pages. Resources should be inferrer from one of the parent/previous pages in this case.

    The PDF under test was provided by one of our services when they encountered the error. Ideally the PDF would
    be valid and return a successful response, but some test that this returns a correct response is at least better
    than this call raising an error/500."""
    response = client.post(
        "/precompiled/sanitise?letter_address_placement=50mm",
        data=no_resources_on_last_page,
        headers={"Content-type": "application/json", **auth_header},
    )

    # message changed from "content-outside-printable-area" to "not-enough-address-lines"
    # after closing the LOGO_BOTTOM/ADDRESS_TOP overlay gap - this PDF is still correctly rejected,
    # just by a later validation step now that the gap-triggered false rejection no longer fires first.
    assert response.json == {
        "recipient_address": None,
        "page_count": 3,
        "message": "not-enough-address-lines",
        "invalid_pages": [1],
        "file": None,
    }


def test_precompiled_sanitise_pdf_without_notify_tag(client, auth_header):
    assert not is_notify_tag_present(BytesIO(address_60mm_no_retouradres))

    response = client.post(
        url_for("precompiled_blueprint.sanitise_precompiled_letter"),
        data=address_60mm_no_retouradres,
        headers={"Content-type": "application/json", **auth_header},
    )
    assert response.status_code == 200
    assert response.json == {
        "message": None,
        "file": ANY,
        "page_count": 1,
        "recipient_address": "Persoonlijk\nCoolsingel 40\n3011 AD  ROTTERDAM",
        "invalid_pages": None,
    }

    pdf = BytesIO(base64.b64decode(response.json["file"].encode()))
    assert is_notify_tag_present(pdf)
    assert extract_address_block(pdf).normalised == "Persoonlijk\nCoolsingel 40\n3011 AD  ROTTERDAM"


@pytest.mark.parametrize("pdf, letter_address_placement", NL_LETTERS_WITH_PLACEMENT)
def test_rewrite_address_block_keeps_the_pdf_and_returns_the_normalised_address(client, pdf, letter_address_placement):
    # unlike upstream, NL only validates the address block: the sender's own layout is kept, not redacted and redrawn
    new_pdf, address = rewrite_address_block(
        BytesIO(pdf),
        page_count=1,
        allow_international_letters=False,
        filename="file",
        letter_address_placement=letter_address_placement,
    )

    assert new_pdf.getvalue() == pdf
    assert address == "Persoonlijk\nCoolsingel 40\n3011 AD  ROTTERDAM"


@pytest.mark.parametrize("letter_address_placement", ["50mm", "60mm"])
def test_extract_address_block_handles_address_with_different_coordinates(client, caplog, letter_address_placement):
    # If address lines are grouped by y2 this gets split into too many lines.
    # When grouped by line, it is split correctly into 4 lines.
    address = extract_address_block(
        BytesIO(address_with_multiple_unusual_coordinates_nl), letter_address_placement=letter_address_placement
    )

    assert address.raw_address == "\n".join(
        [
            "Recipient SURNAME",
            "My Street,",
            "My Town,",
            "SW1A 1AA",
        ]
    )
    assert "Address extraction different when splitting address by y2 vs by line" in caplog.messages


@pytest.mark.parametrize("letter_address_placement", ["50mm", "60mm"])
def test_extract_address_block_handles_address_with_a_large_amount_of_whitespace_in_the_line(
    client, letter_address_placement
):
    # The PDF has a large space between the words "My" and "Recipient," on the first line.
    # These should still be considered the same line by PyMuPDF, but this is to check if there are line
    # detection differences between versions which affect us.
    address = extract_address_block(
        BytesIO(address_with_large_space_in_a_line_nl), letter_address_placement=letter_address_placement
    )

    assert address.raw_address == "\n".join(
        [
            "My Recipient,",
            "My House,",
            "My Street,",
            "My Town,",
            "SW1A 1AA",
        ]
    )


def test_precompiled_sanitise_pdf_with_notify_tag(client, auth_header):
    # upstream's fixture can't be moved into the NL address window without moving its NOTIFY tag too, so tag a Dutch
    # letter instead
    notify_tag_on_first_page = add_notify_tag_to_letter(BytesIO(address_60mm_no_retouradres)).getvalue()
    assert is_notify_tag_present(BytesIO(notify_tag_on_first_page))

    response = client.post(
        url_for("precompiled_blueprint.sanitise_precompiled_letter"),
        data=notify_tag_on_first_page,
        headers={"Content-type": "application/json", **auth_header},
    )
    assert response.status_code == 200
    assert response.json == {
        "message": None,
        "file": ANY,
        "page_count": 1,
        "recipient_address": "Persoonlijk\nCoolsingel 40\n3011 AD  ROTTERDAM",
        "invalid_pages": None,
    }

    pdf = BytesIO(base64.b64decode(response.json["file"].encode()))
    assert is_notify_tag_present(pdf)


@pytest.mark.parametrize("letter_address_placement", ["50mm", "60mm"])
def test_extract_address_block_handles_address_with_ligatures_in_different_fonts(
    client, caplog, letter_address_placement
):
    # "quick maffs defied" should be on the same line. If address lines are grouped by line, this works correctly.
    # If address lines are grouped by y2, the address is not split up correctly.
    address = extract_address_block(
        BytesIO(address_with_unusual_coordinates_nl), letter_address_placement=letter_address_placement
    )

    assert address.raw_address == "\n".join(
        [
            "First line",
            "quick maffs defied",  # note that the ﬀ ligature here has been converted into two f characters
            "SE1 1AA",
        ]
    )


@pytest.mark.parametrize("letter_address_placement", ["50mm", "60mm"])
def test_extract_address_block_when_address_paragraphs_do_not_match_visual_order(client, letter_address_placement):
    # This test documents a current rare edge case where address extraction doesn't behave as we want due.
    # Visually, the address is ordered "normally" with the postcode on the last line and user name on line 1.
    # In the underlying structure of the PDF, which PyMyPDF uses to split up the address, the order is different.
    # Each address line is its own paragraph, and these are ordered differently from the visual order.
    # The address we extract follows the structure of the PDF, not the visual order.
    address = extract_address_block(
        BytesIO(address_where_paragraphs_do_not_match_visual_order_nl),
        letter_address_placement=letter_address_placement,
    )

    assert address.raw_address == "\n".join(["County", "My User", "SW1 1AA", "42 The Parkway", "City"])
