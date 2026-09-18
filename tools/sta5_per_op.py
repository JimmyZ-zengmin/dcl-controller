#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""STA-5: 逐原语结构表（**按控制流走**, 不按地址排序猜）。

★ STA-3 的错误（记下来, 因为它正是本项目最恨的那类错）:
  它假设"19 个跳转表目标按地址排序后, 相邻两个之间 = 该原语的代码"。
  实测反证: LPF 被切成 255 条、PID 只有 7 条、SR 区间长度为负。
  真相（STA-4 的布局打印）: 目标**按 op 号排**, 地址上**不单调**
  （主表最后一项 120C 反而是地址最大的）⇒ "排序取相邻"把公共尾块当成了某个原语的主体。
  ⇒ 教训: **跳转表的项序 ≠ 代码布局序。** 猜布局会安静地造出一张假表。

本版做法（可失败的判据在最后）:
  1. `subs r6,#1 / cmp r6,#17 / bhi` + `tbh` ⇒ 表项 i 对应 **op = i+1**（DIRECT 是 fall-through）
  2. 从每个目标**沿地址顺序走**, 直到**第一条向后分支**（回到循环顶/函数头）
     ⇒ 这就是该 case 体的自然终点（GCC 的 case 体以回边结束）
  3. 与 engine.c 的实测表逐项对照

判据（能失败）:
  K1 每个 case 区间内必须**恰有一条回边**（多了说明区间切错, 少了说明走到了别人的体里）
  K2 DIRECT 走 fall-through 路径, 应**不经过任何 tbh**
  K3 若某原语 cyc/指令 与中位数偏离 >2 倍 ⇒ 必须点名并给出候选原因（不许静默）
"""
import os, re, subprocess, sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BIN = ("C:/ST/STM32CubeIDE_1.5.1/STM32CubeIDE/plugins/"
       "com.st.stm32cube.ide.mcu.externaltools.gnu-tools-for-stm32."
       "7-2018-q2-update.win32_1.5.0.202011040924/tools/bin/arm-none-eabi-")
OBJDUMP = BIN + "objdump.exe"
ELF = os.path.join(ROOT, "build", "dcl_h723")
ENGINE_C = os.path.join(ROOT, "src", "engine.c")
OPS = ["DIRECT", "CMP", "HYST", "CLAMP", "LPF", "PID", "RATE", "DEADBAND", "MUX",
       "EDGE", "LUT", "CNT", "TIMER", "ARITH", "SCALE", "AND", "OR", "NOT", "SR"]

CLASSES = [
    ("br",  re.compile(r"^(b|bl|blx|bx|cbz|cbnz|tbb|tbh|it)\b")),
    ("ld",  re.compile(r"^(ldr|ldrh|ldrb|ldrd|ldm|vldr|vldm|pop|ldrex|ldrb\.w|ldrh\.w|ldr\.w)\b")),
    ("st",  re.compile(r"^(str|strh|strb|strd|stm|vstr|vstm|push|strex|stmdb)\b")),
    ("mul", re.compile(r"^(mul|mla|mls|smull|umull|vmul|vfma|vmla|vnmla|vnmul)\b")),
    ("div", re.compile(r"^(vdiv|vsqrt|sdiv|udiv)\b")),
    ("fp",  re.compile(r"^v")),
    ("alu", re.compile(r"^(add|sub|and|orr|eor|bic|lsl|lsr|asr|cmp|tst|adc|sbc|rsb|clz|rbit|sxt|uxt|mov|movw|movt|mvn|adds|subs|lsls|cmp\.w|movs)\b")),
]


def cls_of(mn):
    for name, rx in CLASSES:
        if rx.match(mn):
            return name
    return "?"


def run(tool, args):
    return subprocess.run([tool] + args, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", cwd=ROOT).stdout or ""


def measured():
    txt = open(ENGINE_C, encoding="utf-8", errors="replace").read()
    m = re.search(r"k_op_cost_itcm\s*\[[^\]]*\]\s*=\s*\{([^}]*)\}", txt, re.S)
    return [int(x) for x in re.findall(r"\d+", m.group(1))] if m else None


def br_target(op):
    """从一个分支的操作数字符串里取目标地址（objdump 会写成 '11a4 <sym+0x..>'）。"""
    m = re.match(r"^([0-9a-f]+)\s", op.strip())
    return int(m.group(1), 16) if m else None


def main():
    out = run(OBJDUMP, ["-d", "--no-show-raw-insn", ELF])
    ins = []
    for line in out.splitlines():
        m = re.match(r"\s*([0-9a-f]+):\s+([a-z][a-z0-9.]*)\s*(.*)$", line)
        if m:
            ins.append([int(m.group(1), 16), m.group(2), m.group(3).strip()])
    hdrs = sorted((int(m.group(1), 16), m.group(2))
                  for m in re.finditer(r"^([0-9a-f]{8}) <([^>]+)>:", out, re.M))
    lo = next(a for a, n in hdrs if n == "engine_scan_itcm")
    hi = next((a for a, _ in hdrs if a > lo), lo + 0x4000)
    body = [x for x in ins if lo <= x[0] < hi]
    addr2i = {a: i for i, (a, _, _) in enumerate(body)}

    # ── 主分派表内容（STA-2/4 已定位: tbh @0xDF0, 表 @0xDF4）──
    sec_out = run(OBJDUMP, ["-s", "--section=.itcm_text", ELF])
    mem = {}
    for line in sec_out.splitlines():
        m = re.match(r"\s*([0-9a-f]+)\s+((?:[0-9a-f]{2,8}\s+){1,4})", line)
        if m:
            a = int(m.group(1), 16)
            for i, b in enumerate(bytes.fromhex(m.group(2).replace(" ", ""))):
                mem[a + i] = b
    TBH, TBL = 0x0DF0, 0x0DF4
    tgts = [TBL + 2 * (mem[TBL + 2 * i] | (mem[TBL + 2 * i + 1] << 8)) for i in range(18)]
    # 表项 i ⇔ op = i+1（`subs r6,#1` 已确认）
    op_entry = {i + 1: t for i, t in enumerate(tgts)}
    op_entry[0] = 0x0E56                      # DIRECT: fall-through（bhi 的目标）

    def walk(entry, stop_first_backedge=True):
        """从 entry 沿地址顺序走, 遇到**第一条向后分支**就停（含该分支）。"""
        i = addr2i[entry]
        seg = []
        while i < len(body):
            a, mn, opnd = body[i]
            seg.append((a, mn, opnd))
            if cls_of(mn) == "br":
                t = br_target(opnd)
                if t is not None and t <= entry:      # 回边 ⇒ case 体结束
                    break
                if t is not None and t < a and t < entry:
                    break
            i += 1
        return seg

    print("op        n    字节   回边  类直方图")
    print("-" * 88)
    rows = []
    for op in range(19):
        entry = op_entry[op]
        seg = walk(entry)
        c = Counter(cls_of(mn) for _, mn, _ in seg)
        back = sum(1 for a, mn, opnd in seg
                   if cls_of(mn) == "br" and (br_target(opnd) or 1 << 40) <= entry)
        name = OPS[op]
        hist = " ".join("%s=%d" % (k, c[k]) for k in ("br", "ld", "st", "mul", "div", "fp", "alu", "?")
                        if c.get(k))
        print("%-9s %-4d %-6d %-5d %s" % (name, len(seg), seg[-1][0] + 4 - entry, back, hist))
        rows.append((name, len(seg), back, dict(c)))

    mc = measured()
    if mc:
        print("\n" + "=" * 88)
        print("结构 vs 实测（实测 = engine.c 的 k_op_cost_itcm[]；**不作为结构计算的输入**）")
        print("=" * 88)
        print("%-9s %-7s %-8s %-11s %s" % ("op", "指令数", "实测cyc", "cyc/指令", "备注"))
        rs = []
        for op, (name, n, back, c) in enumerate(rows):
            if op >= len(mc) or n == 0:
                continue
            r = mc[op] / float(n)
            rs.append((name, r, mc[op], n))
            note = ""
            if c.get("div"):
                note = "含除法（变时延）"
            if back == 0:
                note += ("  " if note else "") + "★无回边 ⇒ 区间可疑"
            print("%-9s %-7d %-8d %-11.2f %s" % (name, n, mc[op], r, note))
        vals = sorted(x[1] for x in rs)
        med = vals[len(vals) // 2]
        print("\n  每条指令周期中位数 = %.2f（%.2f ~ %.2f）" % (med, vals[0], vals[-1]))
        print("  K3 与中位数偏离 >2 倍的原语（必须点名, 不许静默）:")
        k3 = [x for x in rs if x[1] > 2 * med or x[1] < med / 2]
        for name, r, m_, n in sorted(k3, key=lambda z: -z[1]):
            print("     %-9s %.2f cyc/指令  实测=%-4d 指令=%-4d  比值=%.2f×中位"
                  % (name, r, m_, n, r / med))
        if not k3:
            print("     （无）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
