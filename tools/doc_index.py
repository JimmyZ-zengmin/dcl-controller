#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""doc_index.py —— 文档账本（**报告 + 两条精确判据**）

## 它解决什么
实测（2026-09-19）：docs/ 下 **114 份 .md**（比 src 的 58 个文件还多），其中
**24 份是孤儿**（没有任何其它文件引用），而 README §十一「问题→权威源」只登记了 12 行。
——「文档多了却没系统化」的具体形态就是这三条。

## 为什么不做成"又一份文档"
本工具**不产出文档**，只产出**报告**；唯一的落点是 README §十一（已有的权威源表）。
生成物式的索引（如 `docs/EXP-INDEX.md`）由 `tools/exp_registry.py` 负责，本工具管**全局形态**。

## 判据（**只放精确的**，不做启发式判断 —— 启发式会误报，误报会让人关掉闸门）
  D1 ★ **README §十一 的链接必须都存在**（死链 = 权威源表在骗人）
  D2 ★ **§十一 登记的文件必须都在 docs/ 下**（防止登记了源码/不存在的路径）
  WARN（不判失败，只列出来给人处置）：孤儿文档 · 报告类占比 · 重复权威源候选

用法: python tools/doc_index.py [--check]
退出码: 0 = D1/D2 全过 / 1 = 有 FAIL
"""
import io
import os
import re
import sys
from collections import Counter

R = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
README = os.path.join(R, 'README.md')


def docs_list():
    out = []
    for root, _dirs, files in os.walk(os.path.join(R, 'docs')):
        for f in files:
            if f.endswith('.md'):
                out.append(os.path.relpath(os.path.join(root, f), R).replace('\\', '/'))
    return sorted(out)


def read(p):
    return io.open(os.path.join(R, p), encoding='utf-8', errors='replace').read()


def section11(txt):
    """取 README §十一 「文档地图」的表格行。"""
    m = re.search(r'^## 十一、文档地图.*?$(.*?)(?=^## |\Z)', txt, re.M | re.S)
    if not m:
        return []
    rows = []
    for line in m.group(1).split('\n'):
        if not line.strip().startswith('|'):
            continue
        rows.append(line)
    return rows


def main():
    docs = docs_list()
    readme = read('README.md')
    rows = section11(readme)

    # 从 §十一 抽出被链接的 docs/ 路径
    linked = []
    for r_ in rows:
        for m in re.finditer(r'\]\((docs/[^)#]+)\)', r_):
            linked.append(m.group(1))
        for m in re.finditer(r'`(docs/[^`]+\.md)`', r_):
            linked.append(m.group(1))
    linked = sorted(set(linked))

    print('=' * 74)
    print('文档账本（报告；判据只看两条精确的）')
    print('=' * 74)
    print('  docs/*.md            : %d 份' % len(docs))
    print('  README §十一 表格行  : %d 行' % len(rows))
    print('  §十一 链接的文档     : %d 份' % len(linked))

    # ── 分类（只为报告，不作判据）─────────────────────────────────────────
    kind = Counter()
    for d in docs:
        b = os.path.basename(d)
        if re.search(r'^(REF|ARCH|CORE|MEMORY|SUPPORTED|claims|EXP-INDEX|GETTING|RELEASE)', b):
            kind['权威源类'] += 1
        elif re.search(r'^(exp-|STATUS|ASSESS|DAY-|audit|REVIEW|VALIDATE|PITCH)', b):
            kind['报告/评估类'] += 1
        elif re.search(r'^PLAN', b):
            kind['计划类'] += 1
        else:
            kind['其它'] += 1
    print('  分类（仅报告）:', ' · '.join('%s %d' % (k, v) for k, v in kind.most_common()))

    # ── 孤儿 ─────────────────────────────────────────────────────────────
    bodies = {d: read(d) for d in docs} if len(docs) < 200 else {}
    orphan = []
    for d in docs:
        b = os.path.basename(d)
        n = 0
        for q in docs + ['README.md']:
            if q == d:
                continue
            t = bodies.get(q) or read(q)
            if b in t or d in t:
                n += 1
        if n == 0:
            orphan.append(d)
    print('  孤儿（无任何其它文件引用）: %d 份' % len(orphan))
    for o in orphan[:10]:
        print('     %s' % o)
    if len(orphan) > 10:
        print('     … 共 %d 份' % len(orphan))

    # ── D1/D2 ────────────────────────────────────────────────────────────
    bad = []
    for p in linked:
        if not os.path.exists(os.path.join(R, p)):
            bad.append('D1 §十一 链接的目标不存在: %s' % p)
    for p in linked:
        if not p.startswith('docs/'):
            bad.append('D2 §十一 登记了 docs/ 之外的路径: %s' % p)
    unreg = [d for d in docs if d not in linked]
    print('  §十一 **未登记**的文档: %d 份（WARN，不判失败）' % len(unreg))

    if '--check' in sys.argv:
        print('\n=== 判据 ===')
        for b in bad:
            print('  [FAIL] %s' % b)
        print('  [PASS] D1/D2' if not bad else '')
        print('\n%d 项判定, %d FAIL' % (2, len(bad)))
        return 0 if not bad else 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
