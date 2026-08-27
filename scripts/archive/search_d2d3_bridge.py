#!/usr/bin/env python3
"""
DMA2 -> D3 domain 访问能力分析
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

def main():
    print("=" * 80)
    print("搜索 DMA2 到 D3 domain 的访问路径")
    print("=" * 80)
    
    # 1. 搜索 "Bus matrix" 完整表格 - 通常在 108-120 页
    print("\n[1] 提取总线矩阵详细表格 (第105-120页)...")
    pages = extract_pages(PDF_PATH, 105, 120)
    for p in sorted(pages.keys()):
        text = pages[p]
        if "AHB" in text or "DMA" in text or "slave" in text.lower() or "master" in text.lower():
            print(f"\n{'='*70}")
            print(f"第 {p} 页:")
            print(f"{'='*70}")
            print(text[:5000])
    
    # 2. 专门搜索 "DMA1 and DMA2" 或 "DMA controllers" 相关说明
    print("\n\n" + "=" * 80)
    print("[2] 搜索 DMA1/DMA2 控制器连接说明...")
    pages2 = extract_pages(PDF_PATH, 108, 115)
    for p in sorted(pages2.keys()):
        text = pages2[p]
        if "DMA1" in text or "DMA2" in text:
            print(f"\n{'='*70}")
            print(f"第 {p} 页:")
            print(f"{'='*70}")
            print(text[:5000])
    
    # 3. 搜索 "peripheral port" 或 "memory port"
    print("\n\n" + "=" * 80)
    print("[3] 搜索 DMA2 peripheral port 连接...")
    pages3 = extract_pages(PDF_PATH, 100, 130)
    for p in sorted(pages3.keys()):
        text = pages3[p]
        if "peripheral port" in text.lower() or "memory port" in text.lower():
            print(f"\n{'='*70}")
            print(f"第 {p} 页:")
            print(f"{'='*70}")
            print(text[:4000])
    
    # 4. 关键：搜索 STM32H7 系列中 DMA 到 GPIO 的例程或说明
    print("\n\n" + "=" * 80)
    print("[4] 搜索任何关于 DMA 到 GPIO 的说明...")
    # 搜索整个文档中 DMA + GPIO + ODR 的关联
    keywords_dma_gpio = ["DMA", "GPIO", "ODR"]
    with open(PDF_PATH, 'rb') as f:
        reader = PyPDF2.PdfReader(f)
        count = 0
        for i in range(len(reader.pages)):
            try:
                text = reader.pages[i].extract_text()
                if text:
                    text_lower = text.lower()
                    if "dma" in text_lower and "gpio" in text_lower and "odr" in text_lower:
                        print(f"\n第 {i+1} 页 (DMA + GPIO + ODR):")
                        print(text[:2000])
                        count += 1
                        if count >= 5:
                            break
            except:
                pass
    
    # 5. 搜索 "GPIO" + "D3" 或 "GPIO" + "domain" 看 GPIO 在哪个 domain
    print("\n\n" + "=" * 80)
    print("[5] 确认 GPIO 所属 domain...")
    pages5 = extract_pages(PDF_PATH, 134, 140)
    for p in sorted(pages5.keys()):
        text = pages5[p]
        if "GPIO" in text and ("D3" in text or "domain" in text.lower() or "AHB4" in text):
            print(f"\n{'='*70}")
            print(f"第 {p} 页:")
            print(f"{'='*70}")
            print(text[:4000])

if __name__ == "__main__":
    main()
