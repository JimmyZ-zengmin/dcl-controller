#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
h723_ackbuf_check.py —— 应答缓冲区越界**静态**扫描（本项目第 2 次踩同一个坑）

## 为什么需要它（本工具的存在理由）
本项目已**两次**踩同一族缺陷，而且两次都"运行期完全看不出来"：
  ① `h_pin_pattern()` : `uint8_t r[40]` 却 `ack(r, 68)` —— 写坏调用者栈 28 字节
  ② `h_engine_status()`: `uint8_t r[40]` 却 `ack(r, 51)`（`0x38` 39→51 字节扩展时漏改缓冲区）

共同特征（也正是本工具要抓的东西）：
- **不报错、不崩溃**，只写坏调用者的栈（"静默失效"族）；
- 读回来的字段**全是合理值** ⇒ **动态判据永远发现不了**；
- 改协议长度时只改"写的偏移"，**没同步改"缓冲声明"** ⇒ 典型的"半个对齐"。
⇒ 必须是**静态**判据，进构建闸门。

## ★★ 本工具的第一版是个**空判据**（留档，因为它是本项目最贵的教训）
第一版把"函数注册"放在签名行，而本仓库的函数**左花括号都在下一行** ⇒ depth==0 ⇒
**一个函数都没注册** ⇒ 对任何输入都打印 `[OK] 未发现越界`、退出码 0。
**"0 发现"与"根本没扫"长得一模一样** —— 这就是"判据不能失败 = 判据不存在"。
⇒ 因此本版强制**自报覆盖度**，并且**覆盖不足即判无效**（`MIN_FUNCS`）。

## 判据（能失败的）
对每个函数：设其局部缓冲 `uint8_t <name>[N]`，
令 `need = max( ack(name,K) 的 K, put32(name+off)+4, put16(name+off)+2, name[i]= +1 )`；
**need > N ⇒ 报错**。并校验扫描覆盖度。

## 用法
    python tools/h723_ackbuf_check.py [src 目录，默认 ./src]
退出码：0 = 干净；2 = 发现越界；3 = **扫描覆盖不足（判据无效）**；1 = 用法/读文件错误。

## 自检（证明它真的会红）
    python tools/h723_ackbuf_check.py --selftest
在内存里造一个 `uint8_t r[40] … ack(r, 51)` 的夹具，**必须报出越界**；
报不出来 ⇒ 退出码 4（判据无效）。
"""
import os
import re
import sys

# 覆盖度下限：本仓库 src/ 下含应答函数的规模远大于此。
# 若解析到的函数数低于它 ⇒ 一定是解析器坏了, 而不是"代码很干净"。
MIN_FUNCS = 10

RE_FUNC = re.compile(
    r'^(?:static\s+)?(?:DCL_ITCM\s+)?'
    r'(?:void|int|unsigned|uint\d+_t|int\d+_t|size_t|float|double|bool)'
    r'[\s\*]+(\w+)\s*\([^;]*$'
)
RE_BUF = re.compile(r'\buint8_t\s+(\w+)\s*\[\s*(\d+)\s*\]')


def parse_funcs(lines):
    """把源码切成 (函数名, 起始行, [(行号, 文本)]) 列表。

    ★ 关键：**注册推迟到真正见到 '{' 时** —— 本仓库的函数左花括号在签名下一行，
      若在签名行按 depth>0 注册，会把所有函数漏掉（第一版的空判据就是这么来的）。
    """
    funcs = []
    cur = None
    depth = 0
    for i, line in enumerate(lines, 1):
        if cur is None:
            m = RE_FUNC.match(line)
            if m:
                cur = {"name": m.group(1), "start": i, "body": [], "reg": False}
                depth = line.count("{") - line.count("}")
                if depth > 0:
                    cur["reg"] = True
                    funcs.append(cur)
                continue
            continue
        # 已在某个候选函数体内
        cur["body"].append((i, line))
        depth += line.count("{") - line.count("}")
        if not cur["reg"]:
            if depth > 0:
                cur["reg"] = True
                funcs.append(cur)
            elif i - cur["start"] > 6:
                cur = None            # 多行原型/声明, 放弃
                continue
        if cur["reg"] and depth <= 0 and i > cur["start"]:
            cur = None
    return funcs


def scan_lines(lines, tag):
    """返回 (发现列表, 统计)。发现项 = (函数名, 缓冲名, 声明大小, 声明行, 需要字节, 触发行)"""
    funcs = parse_funcs(lines)
    findings = []
    n_buf = 0
    for fn in funcs:
        bufs = {}
        for ln, line in fn["body"]:
            for m in RE_BUF.finditer(line):
                bufs[m.group(1)] = (int(m.group(2)), ln)
        if not bufs:
            continue
        n_buf += 1
        need = {k: (0, 0) for k in bufs}

        def bump(name, n, ln):
            if name in need and n > need[name][0]:
                need[name] = (n, ln)

        for ln, line in fn["body"]:
            for m in re.finditer(r'\back\s*\(\s*(\w+)\s*,\s*(\d+)\s*\)', line):
                bump(m.group(1), int(m.group(2)), ln)
            for m in re.finditer(r'\bput32\s*\(\s*(\w+)\s*\+\s*(\d+)', line):
                bump(m.group(1), int(m.group(2)) + 4, ln)
            for m in re.finditer(r'\bput16\s*\(\s*(\w+)\s*\+\s*(\d+)', line):
                bump(m.group(1), int(m.group(2)) + 2, ln)
            for m in re.finditer(r'\b(\w+)\s*\[\s*(\d+)\s*\]\s*=(?!=)', line):
                # ★ 排除"声明即初始化"（`uint8_t phase_cnt[3] = {0,0,0};`）——
                #   那不是"写到 name[3]", 而是初始化 3 个元素。第一版没排除,
                #   于是 frame_build_selftest/h_seq_deploy 被误报 (已核实是误报)。
                if re.search(r'\b(?:uint8_t|int8_t|char|int|unsigned)\s+' + re.escape(m.group(1)) + r'\s*\[', line):
                    continue
                bump(m.group(1), int(m.group(2)) + 1, ln)

        for name, (size, decl_ln) in bufs.items():
            if need[name][0] > size:
                findings.append(
                    (fn["name"], name, size, decl_ln, need[name][0], need[name][1])
                )
    stats = {"funcs": len(funcs), "with_buf": n_buf, "tag": tag}
    return findings, stats


SELFTEST_SRC = """#include <x.h>
static void known_bad(void)
{
    uint8_t r[40];
    put32(r + 0, 1u);
    put32(r + 47, 2u);
    ack(r, 51);
}
static void known_good(void)
{
    uint8_t r[8];
    put32(r + 0, 1u);
    ack(r, 8);
}
"""


def selftest():
    findings, stats = scan_lines(SELFTEST_SRC.split("\n"), "selftest")
    print("=== 自检: 必须报出 known_bad 的越界 ===")
    for fn, name, size, decl_ln, need, trig_ln in findings:
        print(f"  [X] {fn}()  uint8_t {name}[{size}] -> 需 {need} (L{trig_ln})")
    print(f"  覆盖: 解析 {stats['funcs']} 个函数 / {stats['with_buf']} 个带缓冲")
    bad_hit = any(f[0] == "known_bad" for f in findings)
    good_hit = any(f[0] == "known_good" for f in findings)
    if not bad_hit:
        print("  [FAIL] 判据无效: 造好的越界夹具没被报出来 ⇒ 本工具不可信")
        return 4
    if good_hit:
        print("  [FAIL] 误报: 干净夹具被报为越界")
        return 4
    print("  [OK] 判据有效: 造好的红必红, 造好的绿必绿")
    return 0


def main():
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:                                    # noqa: BLE001
        pass
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        return selftest()

    root = sys.argv[1] if len(sys.argv) > 1 else "src"
    if not os.path.isdir(root):
        print(f"用法: {sys.argv[0]} [src 目录]  —— '{root}' 不是目录")
        return 1

    files = sorted(
        os.path.join(root, f) for f in os.listdir(root) if f.endswith(".c")
    )
    print(f"=== 应答缓冲区越界扫描: {len(files)} 个 .c ===")
    total = 0
    t_funcs = 0
    t_buf = 0
    for path in files:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                lines = fh.read().split("\n")
        except OSError as exc:
            print(f"  ! 读不了 {path}: {exc}", file=sys.stderr)
            continue
        findings, stats = scan_lines(lines, path)
        t_funcs += stats["funcs"]
        t_buf += stats["with_buf"]
        for fn, name, size, decl_ln, need, trig_ln in findings:
            total += 1
            print(f"  [X] {path}:{decl_ln}  {fn}()  uint8_t {name}[{size}]"
                  f"  -> 最大写到 {need} 字节 (L{trig_ln})  ** 越界 {need - size} 字节 **")

    print(f"--- 覆盖度: 解析函数 {t_funcs} 个, 其中带局部缓冲 {t_buf} 个 ---")
    if t_funcs < MIN_FUNCS:
        print(f"  [!!] 覆盖不足 (需要 ≥{MIN_FUNCS} 个函数) ⇒ **判据无效**, 不构成'干净'的证据")
        return 3
    if total == 0:
        print("  [OK] 未发现越界 (且覆盖度达标 ⇒ 这个'OK'是有意义的)")
        return 0
    print(f"合计 {total} 处越界 —— 这类缺陷运行期不可见, 必须静态判掉")
    return 2


if __name__ == "__main__":
    sys.exit(main())
