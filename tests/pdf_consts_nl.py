from io import BytesIO

from pypdf import PdfWriter, Transformation
from reportlab.lib.units import mm

from tests.pdf_consts import address_with_large_space_in_a_line, address_with_multiple_unusual_coordinates, file


def moved_down(pdf, distance_mm):
    """Moves everything on every page of `pdf` down, keeping the text exactly as it is drawn."""
    writer = PdfWriter(clone_from=BytesIO(pdf))
    for page in writer.pages:
        page.add_transformation(Transformation().translate(0, -distance_mm * mm))
    moved = BytesIO()
    writer.write(moved)
    return moved.getvalue()


# NL "Retouradres" return-address line inside the address window, at both letter_address_placements
address_50mm_no_retouradres = file("tests/test_pdfs/address_50mm_no_retouradres.pdf")
address_60mm_no_retouradres = file("tests/test_pdfs/address_60mm_no_retouradres.pdf")
address_50mm_with_retouradres = file("tests/test_pdfs/address_50mm_with_retouradres.pdf")
address_60mm_with_retouradres = file("tests/test_pdfs/address_60mm_with_retouradres.pdf")
address_with_retouradres_missing_city = file("tests/test_pdfs/address_with_retouradres_missing_city.pdf")
address_with_retouradres_missing_postcode = file("tests/test_pdfs/address_with_retouradres_missing_postcode.pdf")

# Upstream's address extraction fixtures have their address at 43-64mm from the top, in the UK address window. 20mm
# lower it sits at 63-84mm, inside both NL address windows (50-90mm and 60-100mm), and the explanatory text below it
# moves to 123mm, still outside them.
address_with_multiple_unusual_coordinates_nl = moved_down(address_with_multiple_unusual_coordinates, 20)
address_with_large_space_in_a_line_nl = moved_down(address_with_large_space_in_a_line, 20)
