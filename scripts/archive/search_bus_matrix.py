#!/usr/bin/env python3
"""
深入搜索 STM32H723 总线矩阵和 DMA2 访问能力
"""
import os
import re
import sys

import PyPDF2

DOCS_DIR = r"D:\STM\work\dcl-controller\STM32H723 docs"
PDF_FILE = "rm0468-stm32h723733-stm32h725735-and-stm32h730-value-line-advanced-armbased-32bit-mcus-stmicroelectronics.pdf"
PDF_PATH = os.path.join(DOCS_DIR, PDF_FILE)

def extract_text_from_pdf(pdf_path, page_start=1, page_end=None):
    """从 PDF 提取指定范围的文本"""
    text_pages = {}
    try:
        with open(pdf_path, 'rb') as f:
            reader = PyPDF2.PdfReader(f)
            end = len(reader.pages) if page_end is None else min(page_end, len(reader.pages))
            for i in range(page_start-1, end):
                try:
                    page_text = reader.pages[i].extract_text()
                    if page_text:
                        text_pages[i+1] = page_text
                except Exception:
                    pass
    except Exception as e:
        print(f"错误: {e}")
    return text_pages

def main():
    print("=" * 80)
    print("深入搜索: STM32H723 总线矩阵 & DMA2 访问能力")
    print("=" * 80)
    
    # 1. 提取总线矩阵相关页面 (106-120)
    print("\n[1] 提取总线矩阵章节 (第106-125页)...")
    bus_pages = extract_text_from_pdf(PDF_PATH, 106, 125)
    for page_num, text in bus_pages.items():
        print(f"\n{'='*60}")
        print(f"第 {page_num} 页:")
        print(f"{'='*60}")
        print(text[:3000])  # 每页显示前3000字符
    
    # 2. 提取 GPIOE 相关页面
    print("\n\n" + "=" * 80)
    print("[2] 搜索 GPIOE 和 AHB4 的关联...")
    print("=" * 80)
    
    # 先搜索 AHB4 和 GPIOE 相关的页面
    all_pages = extract_text_from_pdf(PDF_PATH, 1, 100)  # 先检查前100页
    
    ahb4_gpio_pages = {}
    for page_num, text in all_pages.items():
        text_lower = text.lower()
        if ("ahb4" in text_lower and ("gpio" in text_lower or "gpioe" in text_lower)):
            ahb4_gpio_pages[page_num] = text
    
    # 扩展到更多页面
    all_pages_full = extract_text_from_pdf(PDF_PATH, 1, 600)
    for page_num, text in all_pages_full.items():
        text_lower = text.lower()
        if "ahb4" in text_lower and ("gpioe" in text_lower or " gpio " in text_lower):
            if page_num not in ahb4_gpio_pages:
                ahb4_gpio_pages[page_num] = text
    
    for page_num in sorted(ahb4_gpio_pages.keys())[:10]:
        text = ahb4_gpio_pages[page_num]
        print(f"\n{'='*60}")
        print(f"第 {page_num} 页 (AHB4 + GPIO):")
        print(f"{'='*60}")
        print(text[:2500])
    
    # 3. 专门搜索 "bus matrix" 章节
    print("\n\n" + "=" * 80)
    print("[3] 搜索总线矩阵完整信息...")
    print("=" * 80)
    
    # 搜索包含 "Bus matrix" 或 "matrix" 的页面
    matrix_pages = {}
    all_pages_100_200 = extract_text_from_pdf(PDF_PATH, 100, 200)
    for page_num, text in all_pages_100_200.items():
        text_lower = text.lower()
        if "bus matrix" in text_lower or ("matrix" in text_lower and ("ahb" in text_lower or "dma" in text_lower)):
            matrix_pages[page_num] = text
    
    for page_num in sorted(matrix_pages.keys()):
        print(f"\n{'='*60}")
        print(f"第 {page_num} 页 (总线矩阵相关):")
        print(f"{'='*60}")
        print(text[:3000])
    
    # 4. 搜索 DMA2 的 slave 端口列表
    print("\n\n" + "=" * 80)
    print("[4] 搜索 DMA2 slave/peripheral port 列表...")
    print("=" * 80)
    
    dma_pages = extract_text_from_pdf(PDF_PATH, 570, 620)
    for page_num, text in dma_pages.items():
        if "DMA2" in text or "dma2" in text:
            print(f"\n{'='*60}")
            print(f"第 {page_num} 页:")
            print(f"{'='*60}")
            print(text[:3000])
    
    # 5. 搜索 GPIO 的 ODR 寄存器描述
    print("\n\n" + "=" * 80)
    print("[5] 搜索 GPIO ODR 寄存器描述...")
    print("=" * 80)
    
    odr_pages = extract_text_from_pdf(PDF_PATH, 518, 530)
    for page_num, text in odr_pages.items():
        print(f"\n{'='*60}")
        print(f"第 {page_num} 页:")
        print(f"{'='*60}")
        print(text[:2500])

if __name__ == "__main__":
    main()
