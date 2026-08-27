#!/usr/bin/env python3
"""
搜索 STM32H723 MPU 默认配置和 DMA2 访问 AHB4 的前置条件
"""
import os
import sys
import PyPDF2

DOCS_DIR = r"D:\STM\work\dcl-controller\STM32H723 docs"
PDF_FILE = "rm0468-stm32h723733-stm32h725735-and-stm32h730-value-line-advanced-armbased-32bit-mcus-stmicroelectronics.pdf"
PDF_PATH = os.path.join(DOCS_DIR, PDF_FILE)

def extract_pages(pdf_path, start, end):
    pages = {}
    with open(pdf_path, 'rb') as f:
        reader = PyPDF2.PdfReader(f)
        for i in range(start-1, min(end, len(reader.pages))):
            try:
                text = reader.pages[i].extract_text()
                if text:
                    pages[i+1] = text
            except:
                pass
    return pages

def find_pages_with_keywords(pdf_path, keywords, start=1, end=None):
    matching_pages = {}
    if end is None:
        with open(pdf_path, 'rb') as f:
            reader = PyPDF2.PdfReader(f)
            end = len(reader.pages)
    pages = extract_pages(pdf_path, start, end)
    for page_num, text in pages.items():
        text_lower = text.lower()
        matched_kws = [kw for kw in keywords if kw.lower() in text_lower]
        if matched_kws:
            matching_pages[page_num] = (text, matched_kws)
    return matching_pages

def main():
    print("=" * 80)
    print("搜索: MPU 默认配置 + DMA2 访问 AHB4 前置条件")
    print("=" * 80)
    
    # 1. 搜索 MPU 相关章节
    print("\n[1] 搜索 Memory Protection Unit (MPU)...")
    results = find_pages_with_keywords(PDF_PATH, ["MPU", "Memory Protection", "Region"], 1, 200)
    for p in sorted(results.keys())[:20]:
        text, kws = results[p]
        # 搜索 region、GPIO、DMA 相关内容
        if "region" in text.lower() or "dma" in text.lower() or "gpio" in text.lower() or "ahb4" in text.lower():
            print(f"\n{'='*70}")
            print(f"第 {p} 页 (匹配: {kws}):")
            print(f"{'='*70}")
            print(text[:4000])
    
    # 2. 搜索 HardFault 和 bus fault
    print("\n\n" + "=" * 80)
    print("[2] 搜索 HardFault / BusFault...")
    results2 = find_pages_with_keywords(PDF_PATH, ["HardFault", "BusFault", "fault", "Hard Fault"], 1, 200)
    for p in sorted(results2.keys())[:15]:
        text, kws = results2[p]
        if "dma" in text.lower() or "memory" in text.lower() or "bus" in text.lower():
            print(f"\n{'='*70}")
            print(f"第 {p} 页 (匹配: {kws}):")
            print(f"{'='*70}")
            print(text[:3500])
    
    # 3. 搜索 "Access" + "DMA" + "AHB" 相关说明
    print("\n\n" + "=" * 80)
    print("[3] 搜索 DMA 访问权限和 AHB 相关...")
    results3 = find_pages_with_keywords(PDF_PATH, ["DMA", "access", "AHB4", "peripheral", "region"], 100, 200)
    for p in sorted(results3.keys())[:20]:
        text, kws = results3[p]
        if "DMA2" in text or "master" in text.lower() or "slave" in text.lower():
            print(f"\n{'='*70}")
            print(f"第 {p} 页 (匹配: {kws}):")
            print(f"{'='*70}")
            print(text[:4000])
    
    # 4. 搜索 "default" + "reset" + "memory"
    print("\n\n" + "=" * 80)
    print("[4] 搜索上电默认配置...")
    results4 = find_pages_with_keywords(PDF_PATH, ["default", "reset", "power", "boot", "region"], 100, 150)
    for p in sorted(results4.keys())[:15]:
        text, kws = results4[p]
        if "memory" in text.lower() or "mpu" in text.lower() or "protection" in text.lower():
            print(f"\n{'='*70}")
            print(f"第 {p} 页 (匹配: {kws}):")
            print(f"{'='*70}")
            print(text[:3500])
    
    # 5. 搜索 "D2" + "D3" + "bridge" + "access"
    print("\n\n" + "=" * 80)
    print("[5] 搜索 D2/D3 domain 访问限制...")
    results5 = find_pages_with_keywords(PDF_PATH, ["D2", "D3", "domain", "bridge", "permission", "access"], 100, 200)
    for p in sorted(results5.keys())[:20]:
        text, kws = results5[p]
        print(f"\n{'='*70}")
        print(f"第 {p} 页 (匹配: {kws}):")
        print(f"{'='*70}")
        print(text[:4000])
    
    # 6. 搜索 "memory map" + "region" + "attribute"
    print("\n\n" + "=" * 80)
    print("[6] 搜索内存映射和区域属性...")
    results6 = find_pages_with_keywords(PDF_PATH, ["memory map", "region", "attribute", "0x58", "0x4002"], 130, 140)
    for p in sorted(results6.keys()):
        text, kws = results6[p]
        print(f"\n{'='*70}")
        print(f"第 {p} 页 (匹配: {kws}):")
        print(f"{'='*70}")
        print(text[:3500])
    
    # 7. 搜索 Cortex-M7 相关 (不在 RM 中，但在 PM 中)
    print("\n\n[7] 搜索 Cortex-M7 相关议题...")
    PDF_PM = "pm0253-stm32f7-series-and-stm32h7-series-cortexm7-processor-programming-manual-stmicroelectronics.pdf"
    PM_PATH = os.path.join(DOCS_DIR, PDF_PM)
    if os.path.exists(PM_PATH):
        results7 = find_pages_with_keywords(PM_PATH, ["MPU", "region", "default", "reset"], 1, 150)
        for p in sorted(results7.keys())[:15]:
            text, kws = results7[p]
            print(f"\n{'='*70}")
            print(f"PM 第 {p} 页 (匹配: {kws}):")
            print(f"{'='*70}")
            print(text[:3500])

if __name__ == "__main__":
    main()
