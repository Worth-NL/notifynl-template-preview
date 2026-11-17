from app.address_window_nl import Area, LetterAddressWindow

# the window notifynl-api sends for a service whose letters go to Pingen (its app/letters_nl/address_windows.py)
PINGEN_WINDOW = {
    "id": "pingen",
    "provider": "pingen",
    "address_placement": "60mm",
    "address_area": {"x": 22, "y": 60, "width": 85.5, "height": 25.5},
    "postage_area": {"x": 20, "y": 40, "width": 89.5, "height": 47.5},
}

PINGEN_LETTER_ADDRESS_WINDOW = LetterAddressWindow(
    id="pingen",
    address_placement="60mm",
    address_area=Area(x=22, y=60, width=85.5, height=25.5),
    postage_area=Area(x=20, y=40, width=89.5, height=47.5),
)
