#!/usr/bin/env python3
"""
搜索 DMA2 访问 D3 domain 的具体说明和限制
"""
import os
import sys
import PyPDF2

DOCS_DIR = r"D:\STM\work\dcl-controller\STM32H723 docs"
PDF_RM = "rm0468-stm32h723733-stm32h725735-and-stm32h730-value-line-advanced-armbased-32bit-mcus-stmicroelectronics.pdf"
RM_PATH = os.path.join(DOCS_DIR, PDF_RM)

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
    print("专项搜索: DMA2 到 D3 domain 的访问能力")
    print("=" * 80)
    
    # 1. 搜索 "DMA1 and DMA2" 完整段落
    print("\n[1] 搜索 DMA1/DMA2 控制器完整说明...")
    results = find_pages(RM_PATH, lambda t: 
        "DMA1 and DMA2 controllers" in t or "DMA1 and DMA2" in t,
        105, 120)
    for p in sorted(results.keys()):
        print(f"\n{'='*70}")
        print(f"RM 第 {p} 页:")
        print(f"{'='*70}")
        print(results[p][:6000])
    
    # 2. 搜索 "system bus matrices" 完整上下文
    print("\n\n[2] 搜索 'system bus matrices' 上下文...")
    results2 = find_pages(RM_PATH, lambda t: 
        "system bus matrices" in t.lower() or "system bus matrix" in t.lower(),
        105, 130)
    for p in sorted(results2.keys()):
        print(f"\n{'='*70}")
        print(f"RM 第 {p} 页:")
        print(f"{'='*70}")
        print(results2[p][:5000])
    
    # 3. 搜索 "AHB4" + "peripheral" 或 "AHB4" + "DMA"
    print("\n\n[3] 搜索 AHB4 外设与 DMA 的关系...")
    results3 = find_pages(RM_PATH, lambda t: 
        "AHB4" in t and ("DMA" in t or "peripheral" in t.lower() or "bus" in t.lower()),
        100, 150)
    for p in sorted(results3.keys())[:10]:
        print(f"\n{'='*70}")
        print(f"RM 第 {p} 页:")
        print(f"{'='*70}")
        print(results3[p][:5000])
    
    # 4. 搜索 "D3 domain" 完整说明
    print("\n\n[4] 搜索 D3 domain 完整说明...")
    results4 = find_pages(RM_PATH, lambda t: 
        "D3 domain" in t and ("peripheral" in t.lower() or "memory" in t.lower() or "bus" in t.lower()),
        100, 200)
    for p in sorted(results4.keys())[:10]:
        print(f"\n{'='*70}")
        print(f"RM 第 {p} 页:")
        print(f"{'='*70}")
        print(results4[p][:5000])
    
    # 5. 搜索 "DMA transfer" + "between" 或 "DMA" + "not allowed"
    print("\n\n[5] 搜索 DMA 传输限制...")
    results5 = find_pages(RM_PATH, lambda t: 
        "DMA" in t and ("not allowed" in t.lower() or "cannot" in t.lower() or "limited" in t.lower() or "restriction" in t.lower()),
        100, 200)
    for p in sorted(results5.keys())[:10]:
        print(f"\n{'='*70}")
        print(f"RM 第 {p} 页:")
        print(f"{'='*70}")
        print(results5[p][:4000])
    
    # 6. 搜索 "GPIO" + "DMA" 示例
    print("\n\n[6] 搜索 GPIO + DMA 相关示例...")
    results6 = find_pages(RM_PATH, lambda t: 
        "GPIO" in t and "DMA" in t and ("example" in t.lower() or "transfer" in t.lower() or "write" in t.lower()),
        550, 700)
    for p in sorted(results6.keys())[:10]:
        print(f"\n{'='*70}")
        print(f"RM 第 {p} 页:")
        print(f"{'='*70}")
        print(results6[p][:5000])

if __name__ == "__main__":
    main()
