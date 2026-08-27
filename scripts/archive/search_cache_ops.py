#!/usr/bin/env python3
"""
搜索 D-Cache clean/invalidate 操作和 DTCM 属性
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
    print("搜索: D-Cache 操作寄存器 + DTCM 缓存属性")
    print("=" * 80)
    
    # 1. 搜索 DCCISW / DCISW / CCISW 寄存器
    print("\n[1] PM: 搜索 DCCISW / DCISW 寄存器...")
    results = find_pages(PM_PATH, lambda t: 
        "DCCISW" in t or "DCISW" in t or "CCISW" in t or "DCCIMVAC" in t or "DCIMVAC" in t or
        "data cache" in t.lower() and ("set/way" in t.lower() or "clean" in t.lower() or "invalidate" in t.lower()),
        240, 350)
    for p in sorted(results.keys())[:20]:
        print(f"\n{'='*70}")
        print(f"PM 第 {p} 页:")
        print(f"{'='*70}")
        print(results[p][:6000])
    
    # 2. 搜索 "0xE000EF6C" 或 "0xE000EF74" 等 PPB 地址
    print("\n\n[2] 搜索 PPB 地址空间的 cache 操作寄存器...")
    results2 = find_pages(PM_PATH, lambda t: 
        "0xE000EF6" in t or "0xE000EF7" in t or "0xE000EF5" in t or
        ("PPB" in t and ("cache" in t.lower() or "0xE000" in t)),
        240, 300)
    for p in sorted(results2.keys())[:15]:
        print(f"\n{'='*70}")
        print(f"PM 第 {p} 页:")
        print(f"{'='*70}")
        print(results2[p][:5000])
    
    # 3. 搜索 "Table 104" 或 "Table 105" (cache maintenance registers)
    print("\n\n[3] 搜索 cache maintenance 寄存器表...")
    results3 = find_pages(PM_PATH, lambda t: 
        "Table 104" in t or "Table 105" in t or "Table 106" in t or
        ("cache maintenance" in t.lower() and ("register" in t.lower() or "address" in t.lower())),
        240, 280)
    for p in sorted(results3.keys())[:15]:
        print(f"\n{'='*70}")
        print(f"PM 第 {p} 页:")
        print(f"{'='*70}")
        print(results3[p][:6000])
    
    # 4. RM 中搜索 "data cache" + "maintenance"
    print("\n\n[4] RM: 搜索 data cache maintenance...")
    results4 = find_pages(RM_PATH, lambda t: 
        ("data cache" in t.lower() or "D-cache" in t) and 
        ("maintenance" in t.lower() or "clean" in t.lower() or "invalidate" in t.lower() or "coherency" in t.lower()),
        1, 300)
    for p in sorted(results4.keys())[:15]:
        print(f"\n{'='*70}")
        print(f"RM 第 {p} 页:")
        print(f"{'='*70}")
        print(results4[p][:5000])
    
    # 5. 搜索 "DTCM" + "Strongly-ordered" 或 "Device" 或 "Normal"
    print("\n\n[5] 搜索 DTCM 内存类型...")
    results5 = find_pages(PM_PATH, lambda t: 
        "0x20000000" in t or "DTCM" in t or
        ("memory type" in t.lower() and ("Strongly-ordered" in t or "Device" in t or "Normal" in t)),
        25, 40)
    for p in sorted(results5.keys())[:10]:
        print(f"\n{'='*70}")
        print(f"PM 第 {p} 页:")
        print(f"{'='*70}")
        print(results5[p][:5000])
    
    # 6. 搜索 "0x00000000" + "0x20000000" 默认内存区域
    print("\n\n[6] 搜索默认内存区域映射...")
    results6 = find_pages(PM_PATH, lambda t: 
        ("0x00000000" in t and "0x20000000" in t) or
        ("region 0" in t.lower() and ("Strongly-ordered" in t or "Device" in t or "Normal" in t)),
        25, 45)
    for p in sorted(results6.keys())[:10]:
        print(f"\n{'='*70}")
        print(f"PM 第 {p} 页:")
        print(f"{'='*70}")
        print(results6[p][:5000])

if __name__ == "__main__":
    main()
