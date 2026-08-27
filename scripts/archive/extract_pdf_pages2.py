#!/usr/bin/env python3
"""Extract full text from key pages."""
from pypdf import PdfReader

PDF_PATH = r"D:\STM\work\dcl-controller\STM32H723 docs\rm0468-stm32h723733-stm32h725735-and-stm32h730-value-line-advanced-armbased-32bit-mcus-stmicroelectronics.pdf"

reader = PdfReader(PDF_PATH)

# Full text from critical pages
PAGES = [433, 434, 435, 479, 480, 481, 482, 483, 484, 485, 486, 487, 488, 489, 490, 491, 492, 493, 494, 495,
         614, 615, 616, 617, 618, 619, 620, 621, 622, 623, 624, 625, 626, 627, 628, 629, 630, 631, 632, 633,
         670, 671, 672, 673, 674, 675, 676, 677, 678, 679, 680, 681, 682, 683, 684]

for page_num in PAGES:
    if page_num >= len(reader.pages):
        continue
    text = reader.pages[page_num].extract_text()
    if not text:
        continue
    # Only print pages that contain substantive register info
    if any(k in text for k in ["AHB1ENR", "DMAMUX", "Stream", "SxCR", "EN bit", "DMA_S", "Request mapping", "sync"]):
        print(f"\n{'='*100}")
        print(f"PAGE {page_num}")
        print(f"{'='*100}")
        print(text)
