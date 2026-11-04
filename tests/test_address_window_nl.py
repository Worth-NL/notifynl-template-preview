import json

import pytest

from app import InvalidRequest
from app.address_window_nl import parse_letter_address_window
from app.precompiled import address_bounding_box, standard_letter_address_window
from tests.address_window_consts_nl import PINGEN_LETTER_ADDRESS_WINDOW, PINGEN_WINDOW


@pytest.mark.parametrize("value", [PINGEN_WINDOW, json.dumps(PINGEN_WINDOW)])
def test_parse_letter_address_window_from_a_dict_or_json(value):
    assert parse_letter_address_window(value) == PINGEN_LETTER_ADDRESS_WINDOW


def test_parse_letter_address_window_without_postage_area():
    window = parse_letter_address_window(
        {"id": "standard-50mm", "address_placement": "50mm", "address_area": PINGEN_WINDOW["address_area"]}
    )

    assert window.postage_area is None


@pytest.mark.parametrize("value", [None, ""])
def test_parse_letter_address_window_without_a_window(value):
    assert parse_letter_address_window(value) is None


def test_parse_letter_address_window_keeps_a_window_that_is_already_built():
    assert parse_letter_address_window(PINGEN_LETTER_ADDRESS_WINDOW) is PINGEN_LETTER_ADDRESS_WINDOW


@pytest.mark.parametrize(
    "value",
    [
        "not json",
        "[]",
        {"address_placement": "60mm", "address_area": PINGEN_WINDOW["address_area"]},
        {**PINGEN_WINDOW, "address_area": {"x": 22, "y": 60, "width": 85.5}},
        {**PINGEN_WINDOW, "address_area": {"x": 22, "y": 60, "width": "wide", "height": 25.5}},
        {**PINGEN_WINDOW, "address_area": {"x": 22, "y": 60, "width": 0, "height": 25.5}},
        {**PINGEN_WINDOW, "address_area": {"x": -1, "y": 60, "width": 85.5, "height": 25.5}},
        # json.loads reads NaN and Infinity
        '{"id": "standard-60mm", "address_placement": "60mm", '
        '"address_area": {"x": 24.6, "y": 60, "width": NaN, "height": 40}}',
        '{"id": "standard-60mm", "address_placement": "60mm", '
        '"address_area": {"x": 24.6, "y": 60, "width": 95.4, "height": Infinity}}',
        # the area has to be on an A4 page
        {**PINGEN_WINDOW, "address_area": {"x": 22, "y": 280, "width": 85.5, "height": 25.5}, "postage_area": None},
        {**PINGEN_WINDOW, "address_area": {"x": 150, "y": 60, "width": 85.5, "height": 25.5}, "postage_area": None},
        # the postage area has to contain the address area
        {**PINGEN_WINDOW, "postage_area": {"x": 30, "y": 40, "width": 89.5, "height": 47.5}},
    ],
)
def test_parse_letter_address_window_rejects_malformed_windows(value):
    with pytest.raises(InvalidRequest):
        parse_letter_address_window(value)


@pytest.mark.parametrize("letter_address_placement", ["50mm", "60mm"])
def test_standard_letter_address_window_is_the_placements_window(letter_address_placement):
    window = standard_letter_address_window(letter_address_placement)
    area = window.address_area

    assert window.id == f"standard-{letter_address_placement}"
    assert window.postage_area is None
    # the same box address_bounding_box always gave, before its 3mm margin
    box = address_bounding_box(letter_address_placement)
    assert (area.x - 3, area.y - 3, area.right + 3, area.bottom + 3) == pytest.approx(
        tuple(value / (72 / 25.4) for value in box)
    )


@pytest.mark.parametrize("letter_address_placement", [None, "", "70mm"])
def test_standard_letter_address_window_falls_back_to_60mm(letter_address_placement):
    assert standard_letter_address_window(letter_address_placement) == standard_letter_address_window("60mm")
