#!/usr/bin/env python3
"""Extract text from specific pages of the STM32H723 reference manual PDF."""

from pypdf import PdfReader

PDF_PATH = r"D:\STM\work\dcl-controller\STM32H723 docs\rm0468-stm32h723733-stm32h725735-and-stm32h730-value-line-advanced-armbased-32bit-mcus-stmicroelectronics.pdf"

# Key pages to extract (based on search results)
PAGES = [
    8, 12, 13,  # RCC_AHB1ENR
    73, 74, 75,  # RCC register tables
    433, 476, 479,  # More RCC pages
    575, 576, 578, 579, 580, 581, 582, 583, 586,  # DMA2/DMAMUX
    587, 589, 612, 613, 614, 615, 616, 617, 618, 619,  # DMA stream
    620, 621, 622, 623, 624, 626, 627, 628, 629, 630,
    631, 632, 636, 639, 640, 641,
    648, 649, 670, 671, 672, 673, 677, 678, 679, 680, 682, 683,
]

reader = PdfReader(PDF_PATH)
print(f"Total pages: {len(reader.pages)}")

# Deduplicate and sort
pages_to_extract = sorted(set(PAGES))

for page_num in pages_to_extract:
    if page_num >= len(reader.pages):
        continue
    text = reader.pages[page_num].extract_text()
    if not text:
        continue
    print(f"\n{'='*100}")
    print(f"PAGE {page_num}")
    print(f"{'='*100}")
    print(text)
