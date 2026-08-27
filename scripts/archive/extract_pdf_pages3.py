#!/usr/bin/env python3
"""Extract full text from key pages - RCC_AHB1ENR and DMA SxCR."""
from pypdf import PdfReader

PDF_PATH = r"D:\STM\work\dcl-controller\STM32H723 docs\rm0468-stm32h723733-stm32h725735-and-stm32h730-value-line-advanced-armbased-32bit-mcus-stmicroelectronics.pdf"

reader = PdfReader(PDF_PATH)

# Extract pages 433-434 (RCC_AHB1ENR) and 614-620 (DMA SxCR)
PAGES = [433, 434, 435, 436, 437, 438, 614, 615, 616, 617, 618, 619, 620, 621, 622, 623, 624, 625]

for page_num in PAGES:
    if page_num >= len(reader.pages):
        continue
    text = reader.pages[page_num].extract_text()
    if not text:
        continue
    print(f"\n{'='*100}")
    print(f"PAGE {page_num}")
    print(f"{'='*100}")
    print(text)
