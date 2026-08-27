#!/usr/bin/env python3
"""Extract DMA SxCR register definitions."""
from pypdf import PdfReader

PDF_PATH = r"D:\STM\work\dcl-controller\STM32H723 docs\rm0468-stm32h723733-stm32h725735-and-stm32h730-value-line-advanced-armbased-32bit-mcus-stmicroelectronics.pdf"

reader = PdfReader(PDF_PATH)

# DMA register section is around page 640-690 based on register map
# Search for SxCR
for i in range(635, 695):
    text = reader.pages[i].extract_text()
    if text and "SxCR" in text:
        print(f"\n{'='*100}")
        print(f"PAGE {i}")
        print(f"{'='*100}")
        print(text)
