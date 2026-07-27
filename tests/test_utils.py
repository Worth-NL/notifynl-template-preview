from io import BytesIO

from notifications_utils.pdf import pdf_page_count

from app.utils import merge_letter_parts
from tests.pdf_consts import blank_page, multi_page_pdf, valid_letter


def _page_count(pdf_bytes):
    return pdf_page_count(BytesIO(pdf_bytes))


def test_merge_letter_parts_single_part_is_a_passthrough():
    result = merge_letter_parts([BytesIO(valid_letter)])

    assert pdf_page_count(result) == _page_count(valid_letter)


def test_merge_letter_parts_combines_pages_in_order():
    result = merge_letter_parts([BytesIO(valid_letter), BytesIO(blank_page)])

    assert pdf_page_count(result) == _page_count(valid_letter) + _page_count(blank_page)


def test_merge_letter_parts_preserves_order_for_three_parts():
    result = merge_letter_parts([BytesIO(valid_letter), BytesIO(blank_page), BytesIO(multi_page_pdf)])

    expected_page_count = _page_count(valid_letter) + _page_count(blank_page) + _page_count(multi_page_pdf)
    assert pdf_page_count(result) == expected_page_count
