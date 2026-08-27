#!/usr/bin/env python3
"""Search STM32H723 reference manual PDF for RCC_AHB1ENR, DMA2 Stream5, and DMAMUX1 info."""

import sys
from pypdf import PdfReader

PDF_PATH = r"D:\STM\work\dcl-controller\STM32H723 docs\rm0468-stm32h723733-stm32h725735-and-stm32h730-value-line-advanced-armbased-32bit-mcus-stmicroelectronics.pdf"

# Keywords to search for
KEYWORDS = [
    "AHB1ENR",
    "DMAMUX1",
    "DMA2",
    "Stream 5",
    "Stream5",
    "S5CR",
    "SxCR",
    "EN bit",
    "DMA_SxCR",
    "DMAMUX",
    "DMA request",
    "TIM1_CH4",
    "DMAREQ",
    "SYNC",
    "Request mapping",
    "DMAMUX_CxCR",
    "DMA stream",
]

# Specific search queries
SEARCH_TERMS = [
    "RCC_AHB1ENR",
    "AHB1ENR",
    "DMAMUX1EN",
    "DMA stream",
    "SxCR",
    "EN bit",
    "DMAMUX1",
    "DMAMUX_CxCR",
    "DMAMUX_CCR",
    "DMA request mapping",
    "TIM1_CH4",
    "TIM1_TRIG",
    "request ID",
    "sync",
    "peripheral request",
    "Stream 5",
    "DMA2 Stream",
]

def main():
    print(f"Loading PDF: {PDF_PATH}")
    reader = PdfReader(PDF_PATH)
    total_pages = len(reader.pages)
    print(f"Total pages: {total_pages}")
    
    # First pass: find pages containing our keywords
    hits = {}  # page_num -> set of matched terms
    
    for i in range(total_pages):
        page = reader.pages[i]
        text = page.extract_text()
        if not text:
            continue
        
        matched = set()
        for term in SEARCH_TERMS:
            if term.lower() in text.lower():
                matched.add(term)
        
        if matched:
            hits[i] = matched
        
        if (i + 1) % 100 == 0:
            print(f"  Scanned {i+1}/{total_pages} pages, {len(hits)} hits so far...", file=sys.stderr)
    
    print(f"\nTotal pages with matches: {len(hits)}")
    
    # Print summary of which pages matched which terms
    print("\n=== PAGE MATCHES ===")
    for page_num in sorted(hits.keys()):
        print(f"  Page {page_num}: {sorted(hits[page_num])}")
    
    # Extract full text from the most relevant pages
    # Prioritize pages that match multiple terms
    priority_pages = sorted(hits.items(), key=lambda x: -len(x[1]))
    
    print("\n=== EXTRACTED TEXT FROM TOP PAGES ===")
    
    # Extract text from top 20 pages
    for page_num, matched_terms in priority_pages[:25]:
        text = reader.pages[page_num].extract_text()
        if not text:
            continue
        
        print(f"\n{'='*80}")
        print(f"PAGE {page_num} (matched: {sorted(matched_terms)})")
        print(f"{'='*80}")
        print(text)

if __name__ == "__main__":
    main()
