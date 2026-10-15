from tests.pdf_consts import file

# NL "Retouradres" return-address line inside the address window, at both letter_address_placements
address_50mm_no_retouradres = file("tests/test_pdfs/address_50mm_no_retouradres.pdf")
address_60mm_no_retouradres = file("tests/test_pdfs/address_60mm_no_retouradres.pdf")
address_50mm_with_retouradres = file("tests/test_pdfs/address_50mm_with_retouradres.pdf")
address_60mm_with_retouradres = file("tests/test_pdfs/address_60mm_with_retouradres.pdf")
address_with_retouradres_missing_city = file("tests/test_pdfs/address_with_retouradres_missing_city.pdf")
address_with_retouradres_missing_postcode = file("tests/test_pdfs/address_with_retouradres_missing_postcode.pdf")
