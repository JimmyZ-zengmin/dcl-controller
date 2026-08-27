#!/usr/bin/env python3
"""
搜索 DMA 访问权限、MPU 默认配置、D2/D3 访问限制
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

def find_pages(pdf_path, keywords_func, start=1, end=None):
    if end is None:
        with open(pdf_path, 'rb') as f:
            reader = PyPDF2.PdfReader(f)
            end = len(reader.pages)
    matching_pages = {}
    pages = extract_pages(pdf_path, start, end)
    for page_num, text in pages.items():
        if keywords_func(text):
            matching_pages[page_num] = text
    return matching_pages

def main():
    print("=" * 80)
    print("专项搜索: DMA 访问 AHB4 前置条件 + MPU 默认配置")
    print("=" * 80)
    
    # 1. 在 RM 中搜索 "Access rights" 或 "Bus access" 或 "security"
    print("\n[1] RM: 搜索访问权限/安全属性...")
    results = find_pages(RM_PATH, lambda t: 
        "access" in t.lower() and ("right" in t.lower() or "security" in t.lower() or "trustzone" in t.lower()),
        100, 200)
    for p in sorted(results.keys())[:15]:
        print(f"\n{'='*70}")
        print(f"RM 第 {p} 页:")
        print(f"{'='*70}")
        print(results[p][:4000])
    
    # 2. 在 RM 中搜索 "DMA2" + "AHB" 或 "DMA2" + "domain"
    print("\n\n[2] RM: 搜索 DMA2 访问限制...")
    results2 = find_pages(RM_PATH, lambda t: 
        "DMA2" in t and ("AHB" in t or "domain" in t.lower() or "bridge" in t.lower() or "matrix" in t.lower()),
        100, 150)
    for p in sorted(results2.keys())[:15]:
        print(f"\n{'='*70}")
        print(f"RM 第 {p} 页:")
        print(f"{'='*70}")
        print(results2[p][:5000])
    
    # 3. 搜索 "privileged" + "DMA" 或 "unprivileged"
    print("\n\n[3] RM/RDMA 搜索权限级别...")
    results3 = find_pages(RM_PATH, lambda t: 
        "privileged" in t.lower() or "unprivileged" in t.lower(),
        1, 200)
    for p in sorted(results3.keys())[:10]:
        print(f"\n{'='*70}")
        print(f"RM 第 {p} 页:")
        print(f"{'='*70}")
        print(results3[p][:3000])
    
    # 4. 搜索 "memory type" + "region" 或 "device" + "region"
    print("\n\n[4] 搜索内存区域类型...")
    results4 = find_pages(RM_PATH, lambda t: 
        ("0x5802" in t or "GPIO" in t or "0x58000" in t) and ("region" in t.lower() or "type" in t.lower() or "attribute" in t.lower()),
        130, 150)
    for p in sorted(results4.keys())[:10]:
        print(f"\n{'='*70}")
        print(f"RM 第 {p} 页:")
        print(f"{'='*70}")
        print(results4[p][:4000])
    
    # 5. 在 PM0253 中搜索 MPU 详细寄存器说明
    print("\n\n[5] PM0253: MPU 寄存器详情...")
    results5 = find_pages(PM_PATH, lambda t: 
        "MPU_RNR" in t or "MPU_RBAR" in t or "MPU_RASR" in t or "MPU_CTRL" in t or "ENABLE" in t,
        100, 200)
    for p in sorted(results5.keys())[:15]:
        print(f"\n{'='*70}")
        print(f"PM 第 {p} 页:")
        print(f"{'='*70}")
        print(results5[p][:4000])
    
    # 6. 搜索 BusFault + DMA 或 AHB bus fault
    print("\n\n[6] 搜索 AHB bus fault 或 DMA 触发 fault...")
    results6 = find_pages(RM_PATH, lambda t: 
        ("bus fault" in t.lower() or "BusFault" in t or "aborted" in t.lower()) and 
        ("DMA" in t or "AHB" in t or "write" in t.lower() or "access" in t.lower()),
        1, 200)
    for p in sorted(results6.keys())[:15]:
        print(f"\n{'='*70}")
        print(f"RM 第 {p} 页:")
        print(f"{'='*70}")
        print(results6[p][:4000])
    
    # 7. 搜索 "AHB4" + "peripheral" + "list"
    print("\n\n[7] 搜索 AHB4 外设列表...")
    results7 = find_pages(RM_PATH, lambda t: 
        "AHB4" in t and ("peripheral" in t.lower() or "list" in t.lower() or "register" in t.lower()),
        130, 150)
    for p in sorted(results7.keys())[:10]:
        print(f"\n{'='*70}")
        print(f"RM 第 {p} 页:")
        print(f"{'='*70}")
        print(results7[p][:4000])
    
    # 8. 搜索 "power-on" 或 "after reset" 相关内存配置
    print("\n\n[8] 搜索上电默认内存配置...")
    results8 = find_pages(PM_PATH, lambda t: 
        ("after reset" in t.lower() or "power-on" in t.lower() or "default" in t.lower()) and 
        ("MPU" in t or "memory" in t.lower() or "region" in t.lower()),
        100, 250)
    for p in sorted(results8.keys())[:15]:
        print(f"\n{'='*70}")
        print(f"PM 第 {p} 页:")
        print(f"{'='*70}")
        print(results8[p][:4000])

if __name__ == "__main__":
    main()
