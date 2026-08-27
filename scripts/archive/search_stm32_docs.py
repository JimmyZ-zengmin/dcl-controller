#!/usr/bin/env python3
"""
搜索 STM32H723 文档库，查找 DMA2 是否能访问 GPIOE_ODR 的相关信息
"""
import os
import re
import sys

try:
    import PyPDF2
except ImportError:
    print("正在安装 PyPDF2...")
    os.system(f"{sys.executable} -m pip install PyPDF2 -q")
    import PyPDF2

DOCS_DIR = r"D:\STM\work\dcl-controller\STM32H723 docs"

# 按优先级排序的搜索关键词
SEARCH_QUERIES = [
    {
        "name": "总线矩阵 / Bus Matrix",
        "keywords": ["bus matrix", "AHB matrix", "matrix", "interconnect"],
        "description": "查找总线矩阵结构，DMA2 master 能访问哪些 slave"
    },
    {
        "name": "DMA2 + AHB4",
        "keywords": ["DMA2", "AHB4", "DMA2", "domain"],
        "description": "DMA2 是否能访问 AHB4 domain 的外设"
    },
    {
        "name": "GPIO + 总线 / AHB4",
        "keywords": ["GPIOE", "GPIO", "AHB4", "bus", "peripheral"],
        "description": "GPIOE 挂在哪条总线上"
    },
    {
        "name": "DMA2 + Slave/Port",
        "keywords": ["slave", "port", "master", "DMA2"],
        "description": "DMA2 能访问的 slave 端口列表"
    },
    {
        "name": "DMA -> GPIO ODR 例程",
        "keywords": ["DMA", "GPIO_ODR", "ODR", "DMA", "output", "write"],
        "description": "DMA 搬数据到 GPIO_ODR 的例程"
    },
    {
        "name": "DMA 请求映射 / Request Mapping",
        "keywords": ["DMA request", "DMAMUX", "request mapping", "DMA2"],
        "description": "DMA2 请求映射表"
    },
    {
        "name": "RCC / AHB4 外设",
        "keywords": ["RCC", "AHB4", "ENR", "GPIOE", "enable"],
        "description": "RCC AHB4 外设使能寄存器说明"
    }
]

def extract_text_from_pdf(pdf_path, max_pages=None):
    """从 PDF 提取文本"""
    text_pages = {}
    try:
        with open(pdf_path, 'rb') as f:
            reader = PyPDF2.PdfReader(f)
            num_pages = len(reader.pages)
            if max_pages:
                num_pages = min(num_pages, max_pages)
            for i in range(num_pages):
                try:
                    page_text = reader.pages[i].extract_text()
                    if page_text:
                        text_pages[i+1] = page_text
                except Exception as e:
                    pass
    except Exception as e:
        print(f"  错误: {e}")
    return text_pages

def search_keywords(text_pages, keywords):
    """搜索关键词，返回匹配的页面"""
    results = []
    for page_num, text in text_pages.items():
        text_lower = text.lower()
        for kw in keywords:
            if kw.lower() in text_lower:
                # 找到上下文
                idx = text_lower.find(kw.lower())
                start = max(0, idx - 200)
                end = min(len(text), idx + 300)
                context = text[start:end].replace('\n', ' ').strip()
                results.append({
                    'page': page_num,
                    'keyword': kw,
                    'context': context
                })
                break  # 每个页面只记录一次
    return results

def main():
    print("=" * 80)
    print("STM32H723 文档搜索 - DMA2 是否能访问 GPIOE_ODR")
    print("=" * 80)
    
    # 列出所有 PDF 文件
    pdf_files = [f for f in os.listdir(DOCS_DIR) if f.endswith('.pdf')]
    print(f"\n发现 {len(pdf_files)} 个 PDF 文件:")
    for f in pdf_files:
        print(f"  - {f}")
    
    # 优先处理 rm0468 (参考手册)
    priority_files = [
        "rm0468-stm32h723733-stm32h725735-and-stm32h730-value-line-advanced-armbased-32bit-mcus-stmicroelectronics.pdf",
        "stm32h723zg.pdf"
    ]
    
    # 优先搜索	rm0468 (最重要的参考手册)
    target_file = priority_files[0]
    pdf_path = os.path.join(DOCS_DIR, target_file)
    
    if not os.path.exists(pdf_path):
        print(f"\n错误: 未找到 {target_file}")
        return
    
    print(f"\n{'=' * 80}")
    print(f"正在提取和搜索: {target_file}")
    print(f"{'=' * 80}")
    
    # 提取所有文本
    print("正在提取 PDF 文本...")
    text_pages = extract_text_from_pdf(pdf_path)
    print(f"成功提取 {len(text_pages)} 页文本")
    
    # 按查询搜索
    print(f"\n{'=' * 80}")
    print("搜索结果:")
    print(f"{'=' * 80}")
    
    for query in SEARCH_QUERIES:
        print(f"\n{'─' * 70}")
        print(f"查询: {query['name']}")
        print(f"关键词: {', '.join(query['keywords'])}")
        print(f"描述: {query['description']}")
        print(f"{'─' * 70}")
        
        results = search_keywords(text_pages, query['keywords'])
        if results:
            # 只显示前 5 个结果
            for r in results[:5]:
                print(f"\n  第 {r['page']} 页 (匹配: {r['keyword']}):")
                print(f"  ...{r['context']}...")
            if len(results) > 5:
                print(f"\n  ...还有 {len(results) - 5} 个匹配页面")
        else:
            print("  未找到匹配")
    
    # 特殊搜索：直接搜索 DMA 和 GPIO 相关的重要表格
    print(f"\n{'=' * 80}")
    print("直接搜索关键信息 (DMA + GPIO + ODR):")
    print(f"{'=' * 80}")
    
    special_keywords = [
        ("DMA2", "DMA控制器2"),
        ("AHB4", "AHB4总线域"),
        ("GPIOE", "GPIOE端口"),
        ("ODR", "输出数据寄存器"),
        ("DMAMUX", "DMA多路复用器"),
        ("bus matrix", "总线矩阵"),
        ("slave interface", "从机接口"),
        ("peripheral bus", "外设总线"),
    ]
    
    for kw, desc in special_keywords:
        matching_pages = []
        for page_num, text in text_pages.items():
            if kw.lower() in text.lower():
                matching_pages.append(page_num)
        
        if matching_pages:
            print(f"\n'{kw}' ({desc}) 出现在 {len(matching_pages)} 页:")
            # 显示页码范围
            if len(matching_pages) <= 20:
                print(f"  页码: {matching_pages}")
            else:
                print(f"  前20页: {matching_pages[:20]}...")
    
    # 专门搜索 "DMA2" + "AHB" 的关联
    print(f"\n{'=' * 80}")
    print("专门分析: DMA2 与 AHB 总线的关系")
    print(f"{'=' * 80}")
    
    for page_num, text in text_pages.items():
        if "DMA2" in text and ("AHB" in text or "slave" in text.lower() or "master" in text.lower()):
            # 提取包含 DMA2 和 AHB/slave/master 的段落
            sentences = text.split('.')
            for sent in sentences:
                if "DMA2" in sent and ("AHB" in sent or "slave" in sent.lower() or "master" in sent.lower()):
                    print(f"\n  第{page_num}页: {sent.strip()}")
                    break

if __name__ == "__main__":
    main()
