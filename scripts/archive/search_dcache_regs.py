#!/usr/bin/env python3
"""
搜索 D-Cache 操作寄存器详细信息
"""
import os
import sys
import PyPDF2

DOCS_DIR = r"D:\STM\work\dcl-controller\STM32H723 docs"
PDF_PM = "pm0253-stm32f7-series-and-stm32h7-series-cortexm7-processor-programming-manual-stmicroelectronics.pdf"
PM_PATH = os.path.join(DOCS_DIR, PDF_PM)

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

def find_pages(pdf_path, func, start=1, end=None):
    if end is None:
        with open(pdf_path, 'rb') as f:
            reader = PyPDF2.PdfReader(f)
            end = len(reader.pages)
    matching_pages = {}
    pages = extract_pages(pdf_path, start, end)
    for page_num, text in pages.items():
        if func(text):
            matching_pages[page_num] = text
    return matching_pages

def main():
    print("=" * 80)
    print("搜索: D-Cache 操作寄存器 (DCCISW/DCISW/DCCMVAC)")
    print("=" * 80)
    
    # 搜索 cache maintenance 寄存器表
    print("\n[1] 搜索 cache maintenance 寄存器表...")
    results = find_pages(PM_PATH, lambda t: 
        "Table 103" in t or "Table 104" in t or "Table 105" in t or
        ("cache maintenance" in t.lower() and ("register" in t.lower() or "address" in t.lower() or "operation" in t.lower())),
        240, 260)
    for p in sorted(results.keys())[:15]:
        print(f"\n{'='*70}")
        print(f"PM 第 {p} 页:")
        print(f"{'='*70}")
        print(results[p][:6000])
    
    # 搜索 DCCMVAC / DCIMVAC / DCCIMVAC
    print("\n\n[2] 搜索 DCCMVAC / DCIMVAC / DCCIMVAC...")
    results2 = find_pages(PM_PATH, lambda t: 
        "DCCMVAC" in t or "DCIMVAC" in t or "DCCIMVAC" in t or
        "MVA" in t or "by MVA" in t.lower() or "by address" in t.lower(),
        240, 260)
    for p in sorted(results2.keys())[:15]:
        print(f"\n{'='*70}")
        print(f"PM 第 {p} 页:")
        print(f"{'='*70}")
        print(results2[p][:6000])
    
    # 搜索 "Invalidate entire data cache" 或 "Clean entire data cache"
    print("\n\n[3] 搜索 Clean/Invalidate 整个 cache 的代码示例...")
    results3 = find_pages(PM_PATH, lambda t: 
        ("Invalidate entire" in t or "Clean entire" in t or "invalidate the entire" in t.lower() or "clean the entire" in t.lower()) and
        ("data cache" in t.lower() or "D-Cache" in t),
        240, 260)
    for p in sorted(results3.keys())[:10]:
        print(f"\n{'='*70}")
        print(f"PM 第 {p} 页:")
        print(f"{'='*70}")
        print(results3[p][:6000])
    
    # 搜索 "DSB" + "ISB" + "cache"
    print("\n\n[4] 搜索 DSB/ISB 与 cache 的关系...")
    results4 = find_pages(PM_PATH, lambda t: 
        ("DSB" in t and "ISB" in t) and ("cache" in t.lower() or "maintenance" in t.lower()),
        240, 260)
    for p in sorted(results4.keys())[:10]:
        print(f"\n{'='*70}")
        print(f"PM 第 {p} 页:")
        print(f"{'='*70}")
        print(results4[p][:5000])

if __name__ == "__main__":
    main()
