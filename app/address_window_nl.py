"""
[NotifyNL] The address window a precompiled letter's first page is checked against.

notifynl-api decides which window a service's letters need (its app/letters_nl/address_windows.py) and sends it with
every sanitise and overlay request. Without one, precompiled.py uses the window for the letter_address_placement,
which is the geometry template-preview always had.
"""

import json
import math
from dataclasses import dataclass

from app import InvalidRequest

A4_WIDTH, A4_HEIGHT = 210, 297  # mm


@dataclass(frozen=True)
class Area:
    """A rectangle in mm, measured from the top left corner of the page."""

    x: float
    y: float
    width: float
    height: float

    @property
    def right(self):
        return self.x + self.width

    @property
    def bottom(self):
        return self.y + self.height

    def contains(self, other):
        return self.x <= other.x and self.y <= other.y and other.right <= self.right and other.bottom <= self.bottom


@dataclass(frozen=True)
class LetterAddressWindow:
    """
    address_area is where the recipient's address goes. If postage_area is set, nothing but the address may be printed
    inside it: that's where a print provider's envelope window and postage marks go (Pingen's postage area, for
    example). Without it, only the address area itself is constrained.
    """

    id: str
    address_placement: str
    address_area: Area
    postage_area: Area | None = None


def parse_letter_address_window(value):
    """
    Builds the window from what a caller sent: a dict (a Celery task's kwarg), a JSON string (a query parameter) or
    nothing, which gives None. A window that is already built is returned as it is.
    """
    if not value:
        return None
    if isinstance(value, LetterAddressWindow):
        return value

    try:
        if isinstance(value, str):
            value = json.loads(value)
        window = LetterAddressWindow(
            id=str(value["id"]),
            address_placement=str(value["address_placement"]),
            address_area=_area(value["address_area"]),
            postage_area=_area(value["postage_area"]) if value.get("postage_area") else None,
        )
    except (AttributeError, KeyError, TypeError, ValueError) as e:
        raise InvalidRequest(f"Invalid letter_address_window: {e!r}") from e

    if window.postage_area and not window.postage_area.contains(window.address_area):
        raise InvalidRequest("Invalid letter_address_window: its postage area must contain its address area")

    return window


def _area(value):
    area = Area(**{key: float(value[key]) for key in ("x", "y", "width", "height")})
    # json.loads reads NaN and Infinity, which no comparison below would reject
    if not all(math.isfinite(number) for number in (area.x, area.y, area.width, area.height)):
        raise ValueError(f"not an area on the page: {value}")
    if area.x < 0 or area.y < 0 or area.width <= 0 or area.height <= 0:
        raise ValueError(f"not an area on the page: {value}")
    if area.right > A4_WIDTH or area.bottom > A4_HEIGHT:
        raise ValueError(f"not an area on the page: {value}")
    return area
