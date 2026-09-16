#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dcl_static_check.py —— ③层 DCL 程序"**不得出现物理引脚号 / 总线地址 / 寄存器名**"的静态判据

契约出处：`docs/REF-program-contract.md` §3.1（本文自评"最重要的一条"）与 §1.5 红线 1，
状态：GAP-10（原为 ⬜ open）。

## 为什么这条边界是"必须"的（三边权威独立确认，不是我们的口味）
- **IEC 61131-3**：控制动作分**逻辑运算**与**硬件动作**两部分，逻辑用统一描述格式标准化，
  **硬件动作由平台专属固件函式库承载**。
- **CODESYS**：可移植应用层用**内部符号**，唯一的平台相关文件是 I/O Mapping POU；
  *"The application POUs **never** reference physical I/O addresses."*
- **Zephyr**：*"设备树是硬件信息的唯一真相源。驱动代码不应该包含任何具体的引脚号、地址"*。

⇒ 结论：**③层出现引脚号 = 这份"资产"绑死在这块板子上**，换板/换脚就得改程序，
而这恰恰是分层要消除的成本（本项目 I2C 那次就是走了"改固件"的弯路，见契约 GAP-6）。

## ★★ 判据必须**先剥注释**（这条是标定出来的，不是想出来的）
标定时发现 `examples/h723_di_demo.dcl` 里出现 `PC0..PC3` —— 但逐行看：
它们**全部在 `#` 注释里**（如 `SENSOR di1 FROM sensor[3]   # PC0 (上拉: 悬空=1)`），
**真实代码是 `sensor[3]`，完全符号化**。
⇒ 不剥注释的判据会把**合法的接线文档**打成违规（误报），而误报会让人开始忽略这条判据。
⇒ 本工具**先按与解析器同一规则剥注释**（`#` 与 `//`），再扫**代码**部分。
（注释里的引脚号是**好文档**，我们保留它的合法性。）

## 判据（三条，都要能失败）
| # | 形态 | 例 |
|---|---|---|
| A | **引脚名** | `PA6` `PE8` `PC0` … `P[A-K]` + `0..15` |
| B | **地址字面量** | `0x40020410` `0x58021014` `0x20003EA0`（8+ 位十六进制，或 `0x4`/`0x5` 起 6+ 位）|
| C | **寄存器/外设名** | `GPIOE` `TIM3` `USART1` `MDMA` `DMAMUX` `IWDG` `SDMMC` `BSRR` `MODER` … |

## 用法
    python tools/dcl_static_check.py <文件或目录> [更多...]
    python tools/dcl_static_check.py --selftest
退出码：0 干净 · 2 发现违规 · 3 覆盖不足（**判据无效**）· 1 用法错误。
★ 有 `--selftest`：造好的红必红、造好的绿必绿；其中第三例（引脚名只在注释里）**必须绿**
  —— 它是"误报防线"，缺了它这工具会被自己人关掉。
"""
import os
import re
import sys

# ── 与 dclc.py 的 strip_comment 同规则：`#` 或 `//` 到行尾（DSL 无字符串字面量）──
RE_COMMENT = re.compile(r'#|//')

RE_PIN = re.compile(r'\bP[A-K](?:[0-9]|1[0-5])\b')
RE_ADDR = re.compile(r'\b0x(?:[45][0-9A-Fa-f]{5,}|[0-9A-Fa-f]{8,})\b')
RE_PERIPH = re.compile(
    r'\b(?:'
    r'GPIO[A-K]|TIM\d{1,2}|USART\d|UART\d|SPI\d|I2C\d|'
    r'MDMA|DMAMUX|DMA\d|IWDG|WWDG|SDMMC|SAI\d|ETH|FMC|'
    r'BSRR|MODER|OTYPER|PUPDR|AFR[LH]|ODR|IDR|CCMR\d|CCER|CIFR|CISR|CTCR|CBNDTR|CSAR|CDAR'
    # ★ 尾部用 `(?![A-Za-z0-9])` 而不是 `\b`：`\b` 在 `GPIOE_ODR` 里遇到 `_`
    #   （`_` 是词字符）就不成立 ⇒ **`GPIOE_ODR` 会漏报**。这是自检夹具当场抓出来的
    #   （第一版这个夹具判 FAIL）—— 印证"判据必须自带能失败的夹具"。
    r')(?![A-Za-z0-9])'
)

RULES = (
    ('A 引脚名', RE_PIN),
    ('B 地址字面量', RE_ADDR),
    ('C 寄存器/外设名', RE_PERIPH),
)


def strip_comment(line):
    """剥掉行内注释，保留代码部分（与 tools/dclc.py:80 的规则一致）"""
    m = RE_COMMENT.search(line)
    return line[:m.start()] if m else line


def check_text(text, name="<text>"):
    """→ (findings, stats)。findings = [(行号, 规则名, 命中文本, 该行代码部分)]"""
    findings = []
    n_lines = 0
    for lineno, raw in enumerate(text.split("\n"), 1):
        code = strip_comment(raw)
        if not code.strip():
            continue
        n_lines += 1
        for kind, rx in RULES:
            for m in rx.finditer(code):
                findings.append((lineno, kind, m.group(0), code.strip()))
        # 一行只报一次（同一行多个命中已在上面按规则收集）
    return findings, {"lines": n_lines, "name": name}


# ── 自检夹具 ────────────────────────────────────────────────────────────
GOOD_SYMBOLIC = """\
# 纯符号化程序: 引脚信息只出现在注释里 (合法文档用法)
SENSOR  di1   FROM sensor[3]      # PC0 (上拉: 悬空=1)
PID     heat  FROM di1  SP=60 KP=1.0
OUTPUT  pwm   TO wire[20] FROM heat
LOGIC   both  = di1 AND di2
"""

BAD_PIN = """\
SENSOR  di1  FROM sensor[3]
GPIO_PIN PA6 = 1
OUTPUT  pwm  TO wire[20] FROM di1
"""

BAD_ADDR = """\
SENSOR  a  FROM sensor[0]
LOGIC   b  = a AND 0x40020410
"""

BAD_REG = """\
SENSOR  a  FROM sensor[0]
LOGIC   b  = GPIOE_ODR
"""


def selftest():
    cases = [
        (GOOD_SYMBOLIC, False, "纯符号化 + 注释里有 PC0 —— **必须绿**(误报防线)"),
        (BAD_PIN, True, "代码里出现 PA6 —— 必须红"),
        (BAD_ADDR, True, "代码里出现 0x40020410 —— 必须红"),
        (BAD_REG, True, "代码里出现 GPIOE_ODR —— 必须红"),
    ]
    print("=== 自检: 造好的红必红 / 造好的绿必绿 ===")
    bad = 0
    for text, want_red, desc in cases:
        f, st = check_text(text)
        got_red = len(f) > 0
        mark = "OK " if got_red == want_red else "FAIL"
        if got_red != want_red:
            bad += 1
        hits = ", ".join(f"{k}:{t}" for _, k, t, _ in f) or "-"
        print(f"  [{mark}] {desc}\n        命中={hits}")
    if bad:
        print(f"  [FAIL] {bad} 个夹具行为不符 ⇒ **判据无效**, 不可信")
        return 4
    print("  [OK] 判据有效")
    return 0


def iter_files(targets):
    out = []
    for t in targets:
        if os.path.isdir(t):
            for root, _dirs, files in os.walk(t):
                out += [os.path.join(root, f) for f in sorted(files) if f.endswith(".dcl")]
        elif os.path.isfile(t):
            out.append(t)
    return out


def main():
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:                                   # noqa: BLE001
        pass
    args = [a for a in sys.argv[1:]]
    if not args:
        print(__doc__)
        return 1
    if "--selftest" in args:
        return selftest()

    files = iter_files(args)
    print(f"=== ③层静态判据: 扫描 {len(files)} 个 .dcl ===")
    total = 0
    t_lines = 0
    for path in files:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError as exc:
            print(f"  ! 读不了 {path}: {exc}", file=sys.stderr)
            continue
        findings, st = check_text(text, path)
        t_lines += st["lines"]
        for lineno, kind, tok, code in findings:
            total += 1
            print(f"  [X] {path}:{lineno}  {kind} 命中 `{tok}`")
            print(f"        {code}")
    print(f"--- 覆盖度: {len(files)} 个文件 / {t_lines} 行代码(已剥注释) ---")
    if not files:
        print("  [!!] 一个 .dcl 都没扫到 ⇒ **判据无效**(0 发现与'没扫'不可区分)")
        return 3
    if total == 0:
        print("  [OK] 未发现物理引脚号/地址/寄存器名 (且覆盖度达标 ⇒ 这个 OK 有意义)")
        return 0
    print(f"合计 {total} 处违规。")
    print("  修法: 用**符号槽**表达 —— 输入走 `sensor[i]`/`wire[j]`，输出走 `wire[j]`；")
    print("        要接一个新器件 ⇒ 走②层外设能力（契约 §3.2-C），**不是**在程序里写引脚。")
    return 2


if __name__ == "__main__":
    sys.exit(main())
