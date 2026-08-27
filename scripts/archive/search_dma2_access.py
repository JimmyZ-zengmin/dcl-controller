#!/usr/bin/env python3
"""
专门搜索 DMA2 能访问的 slave 端口和 AHB4 外设列表
"""
import os
import re
import sys

import PyPDF2

DOCS_DIR = r"D:\STM\work\dcl-controller\STM32H723 docs"
PDF_FILE = "rm0468-stm32h723733-stm32h725735-and-stm32h730-value-line-advanced-armbased-32bit-mcus-stmicroelectronics.pdf"
PDF_PATH = os.path.join(DOCS_DIR, PDF_FILE)

def extract_text_from_pdf(pdf_path, page_start=1, page_end=None):
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

def find_pages_with_keywords(pdf_path, keywords, start=1, end=None):
    """查找包含指定关键词的页面"""
    matching_pages = {}
    all_pages = extract_text_from_pdf(pdf_path, start, end)
    for page_num, text in all_pages.items():
        text_lower = text.lower()
        matched_kws = [kw for kw in keywords if kw.lower() in text_lower]
        if matched_kws:
            matching_pages[page_num] = (text, matched_kws)
    return matching_pages

def main():
    print("=" * 80)
    print("专攻: DMA2 能访问哪些 slave? AHB4 上有什么外设?")
    print("=" * 80)
    
    # 1. 搜索 "AHB4 peripheral" 或 "AHB4 外设"
    print("\n[1] 搜索 AHB4 外设列表...")
    results = find_pages_with_keywords(PDF_PATH, ["AHB4", "peripheral", "domain"], 100, 150)
    for page_num in sorted(results.keys())[:15]:
        text, kws = results[page_num]
        print(f"\n第 {page_num} 页 (匹配: {kws}):")
        print(text[:3000])
    
    # 2. 搜索 DMA2 的 slave 列表或端口列表
    print("\n\n" + "=" * 80)
    print("[2] 搜索 DMA2 slave/peripheral 端口列表...")
    results2 = find_pages_with_keywords(PDF_PATH, ["DMA2", "slave", "port", "target"], 100, 150)
    for page_num in sorted(results2.keys())[:15]:
        text, kws = results2[page_num]
        print(f"\n第 {page_num} 页 (匹配: {kws}):")
        print(text[:3000])
    
    # 3. 搜索 "Bus matrix" 或 "Bus slave" 章节
    print("\n\n" + "=" * 80)
    print("[3] 搜索总线矩阵表格 - DMA2 能访问的 slave...")
    results3 = find_pages_with_keywords(PDF_PATH, ["DMA2", "slave", "TARG", "matrix"], 100, 200)
    for page_num in sorted(results3.keys())[:20]:
        text, kws = results3[page_num]
        print(f"\n第 {page_num} 页 (匹配: {kws}):")
        print(text[:4000])
    
    # 4. 搜索 GPIO 和总线连接
    print("\n\n" + "=" * 80)
    print("[4] 搜索 GPIO 总线连接...")
    results4 = find_pages_with_keywords(PDF_PATH, ["GPIO", "AHB4", "bus", "connected"], 130, 140)
    for page_num in sorted(results4.keys()):
        text, kws = results4[page_num]
        print(f"\n第 {page_num} 页 (匹配: {kws}):")
        print(text[:3500])
    
    # 5. 搜索 D2-to-D3 bridge 或 domain 间访问
    print("\n\n" + "=" * 80)
    print("[5] 搜索 D2-to-D3 桥接和 domain 间访问...")
    results5 = find_pages_with_keywords(PDF_PATH, ["D2-to-D3", "bridge", "domain", "access", "interconnect"], 100, 150)
    for page_num in sorted(results5.keys())[:15]:
        text, kws = results5[page_num]
        print(f"\n第 {page_num} 页 (匹配: {kws}):")
        print(text[:3500])
    
    # 6. 搜索 STM32H7 GPIO 在哪个 domain
    print("\n\n" + "=" * 80)
    print("[6] 搜索 GPIOE 地址映射和寄存器基地址...")
    results6 = find_pages_with_keywords(PDF_PATH, ["GPIOE", "base", "address", "0x", "register"], 130, 145)
    for page_num in sorted(results6.keys())[:10]:
        text, kws = results6[page_num]
        print(f"\n第 {page_num} 页 (匹配: {kws}):")
        print(text[:3000])

if __name__ == "__main__":
    main()
