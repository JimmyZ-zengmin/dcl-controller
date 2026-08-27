#!/usr/bin/env python3
"""
搜索 DTCM cacheable 和 D-Cache clean/invalidate 方法
"""
import os
import sys
import PyPDF2

DOCS_DIR = r"D:\STM\work\dcl-controller\STM32H723 docs"
PDF_RM = "rm0468-stm32h723733-stm32h725735-and-stm32h730-value-line-advanced-armbased-32bit-mcus-stmicroelectronics.pdf"
PDF_PM = "pm0253-stm32f7-series-and-stm32h7-series-cortexm7-processor-programming-manual-stmicroelectronics.pdf"
RM_PATH = os.path.join(DOCS_DIR, PDF_RM)
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
    print("搜索: DTCM Cacheable + D-Cache Clean/Invalidate")
    print("=" * 80)
    
    # 1. 搜索 "DTCM" + "cache" 或 "TCM" + "cache"
    print("\n[1] RM: 搜索 DTCM 缓存属性...")
    results = find_pages(RM_PATH, lambda t: 
        ("DTCM" in t or "TCM" in t) and ("cache" in t.lower() or "cachable" in t.lower() or "memory map" in t.lower() or "region" in t.lower() or "attribute" in t.lower()),
        100, 250)
    for p in sorted(results.keys())[:20]:
        print(f"\n{'='*70}")
        print(f"RM 第 {p} 页:")
        print(f"{'='*70}")
        print(results[p][:5000])
    
    # 2. 搜索 "memory map" + "region" + "0x200"
    print("\n\n[2] 搜索内存映射区域属性...")
    results2 = find_pages(RM_PATH, lambda t: 
        "0x2000" in t and ("memory" in t.lower() or "region" in t.lower() or "cache" in t.lower() or "type" in t.lower()),
        130, 160)
    for p in sorted(results2.keys())[:15]:
        print(f"\n{'='*70}")
        print(f"CM 第 {p} 页:")
        print(f"{'='*70}")
        print(results2[p][:5000])
    
    # 3. 搜索 "data cache" + "invalidate" 或 "clean"
    print("\n\n[3] PM: 搜索 D-Cache 操作...")
    results3 = find_pages(PM_PATH, lambda t: 
        ("data cache" in t.lower() or "D-Cache" in t or "DCISW" in t or "DCCISW" in t or "clean" in t.lower() or "invalidate" in t.lower()) and
        ("set/way" in t.lower() or "register" in t.lower() or "0xE000" in t or "register" in t.lower()),
        150, 350)
    for p in sorted(results3.keys())[:20]:
        print(f"\n{'='*70}")
        print(f"PM 第 {p} 页:")
        print(f"{'='*70}")
        print(results3[p][:5000])
    
    # 4. 搜索 "Strongly-ordered" 或 "Device" + "memory type"
    print("\n\n[4] 搜索内存类型 (Strongly-ordered/Device)...")
    results4 = find_pages(PM_PATH, lambda t: 
        ("Strongly-ordered" in t or "Device memory" in t or "memory type" in t.lower() or "Shareable" in t or "cacheable" in t.lower()),
        200, 260)
    for p in sorted(results4.keys())[:15]:
        print(f"\n{'='*70}")
        print(f"PM 第 {p} 页:")
        print(f"{'='*70}")
        print(results4[p][:4500])
    
    # 5. 搜索 "CMSIS" + "cache" 或 "SCB" + "cache"
    print("\n\n[5] PM: 搜索 CMSIS cache 函数...")
    results5 = find_pages(PM_PATH, lambda t: 
        ("CMSIS" in t or "SCB" in t) and ("cache" in t.lower() or "CleanDCache" in t or "InvalidateDCache" in t or "CCISW" in t),
        240, 300)
    for p in sorted(results5.keys())[:15]:
        print(f"\n{'='*70}")
        print(f"PM 第 {p} 页:")
        print(f"{'='*70}")
        print(results5[p][:5000])
    
    # 6. 搜索 "RM0468" 中关于 cache 的章节
    print("\n\n[6] RM: 搜索 cache 相关章节...")
    results6 = find_pages(RM_PATH, lambda t: 
        ("data cache" in t.lower() or "instruction cache" in t.lower() or "D-cache" in t or "I-cache" in t) and
        ("maintenance" in t.lower() or "maintenance operations" in t.lower() or "cache maintenance" in t.lower()),
        1, 200)
    for p in sorted(results6.keys())[:10]:
        print(f"\n{'='*70}")
        print(f"CM 第 {p} 页:")
        print(f"{'='*70}")
        print(results6[p][:5000])
    
    # 7. 搜索 "Table 77" 或 "default memory map" 在 PM 中
    print("\n\n[7] PM: 搜索默认内存映射和区域属性...")
    results7 = find_pages(PM_PATH, lambda t: 
        ("Table 77" in t or "default memory map" in t.lower() or "memory regions" in t.lower() or "Outer" in t or "Inner" in t),
        210, 230)
    for p in sorted(results7.keys())[:10]:
        print(f"\n{'='*70}")
        print(f"PM 第 {p} 页:")
        print(f"{'='*70}")
        print(results7[p][:5000])

if __name__ == "__main__":
    main()
