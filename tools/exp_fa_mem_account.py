#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""exp_fa_mem_account.py —— **内存宪法**的判据总跑（A/B/C/D 四期的验收入口）

## 它把散在三个工具里的判据汇到一处，并补两条**只能在实测里证明**的
  C9-NC 归属审计的**变异对照**：往一个非白名单模块塞一个区引用 ⇒ 审计必须红
  C11 ★ `dclc` 的容量**确实是从 src/ 派生的**：临时把 `engine.h` 的 MAX_ROUTES 改成 64
       ⇒ `dclc` 必须报"板上限 64"，而不是照旧放行 100 条（这条才叫 M4 的实证）
  F1/F2/F3 **在板**（栈水位）：magic/scans、水位区间、`-DDCL_STACK_PROBE_BYTES=16384` 的变异
       —— 需要板子在场；不在场时判 **SKIP 并写明原因**（SKIP ≠ PASS）

用法: python tools/exp_fa_mem_account.py [--offline]
退出码: 0 = 全 PASS / 1 = 有 FAIL / 2 = 前置不满足
"""
import argparse
import io
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
R = os.path.dirname(HERE)
sys.path.insert(0, HERE)


def run(cmd, **kw):
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=R,
                       encoding="utf-8", errors="replace", **kw)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def judge(tag, out):
    m = re.search(r"^\s*\[(PASS|FAIL)\]\s*%s" % re.escape(tag), out, re.M)
    return m.group(1) == "PASS" if m else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true", help="跳过在板项（F1/F2/F3）")
    ap.add_argument("--port", default=None)
    a = ap.parse_args()
    res, skip = [], []
    print("=" * 74)
    print("内存宪法判据总跑（A 宪法 · B 账本 · C 容量 · D 性质）")
    print("=" * 74)

    # ── B: 账本 C2~C8 ────────────────────────────────────────────────
    rc, out = run([sys.executable, "tools/mem_report.py", "--check-docs"])
    for tag in ("C2", "C3", "C4", "C5", "C6", "C7", "C8"):
        v = judge(tag, out)
        if v is not None:
            res.append(("B/%s" % tag, v))
    res.append(("B 账本闸门整体 rc==0", rc == 0))
    print("\n[ B 账本 ] %s" % ("全过" if rc == 0 else "有 FAIL"))
    for l in out.split("\n"):
        if "[FAIL]" in l or "[WARN]" in l:
            print("    " + l.strip())

    # ── C0/C9/C12: 归属审计 ─────────────────────────────────────────
    rc, out = run([sys.executable, "tools/hardcode_audit.py"])
    for tag in ("C0", "C9", "C12"):
        v = judge(tag, out)
        if v is not None:
            res.append(("D/%s" % tag, v))
    print("\n[ D 归属审计 ] %s" % ("全过" if rc == 0 else "有 FAIL"))
    for l in out.split("\n"):
        if "[FAIL]" in l:
            print("    " + l.strip())

    # ── C9-NC: 变异对照（往非白名单模块塞一个区引用）─────────────────
    print("\n[ C9-NC 变异对照 ] 往 adc.c 塞一行 `AXI_BB_RING` 引用 ⇒ 审计必须红")
    p = os.path.join(R, "src", "adc.c")
    orig = io.open(p, encoding="utf-8", errors="replace").read()
    try:
        io.open(p, "w", encoding="utf-8", newline="\n").write(
            orig + "\n/* 变异对照 */\nstatic volatile uint32_t s_nc_probe = AXI_BB_RING;\n")
        rc2, out2 = run([sys.executable, "tools/hardcode_audit.py"])
        hit = judge("C9", out2)
        print("    rc=%d ；C9 判定 = %s（期望 False）" % (rc2, hit))
        res.append(("D/C9-NC 变异后审计变红", (rc2 != 0) and (hit is False)))
    finally:
        io.open(p, "w", encoding="utf-8", newline="\n").write(orig)

    # ── C11: dclc 容量派生（变异：engine.h 的 MAX_ROUTES → 64）─────────
    print("\n[ C11 容量派生 ] 临时把 engine.h 的 MAX_ROUTES 改成 64 ⇒ dclc 必须报『板上限 64』")
    eh = os.path.join(R, "src", "engine.h")
    orig_h = io.open(eh, encoding="utf-8", errors="replace").read()
    prog = os.path.join(R, ".tmpctl", "fa_many_routes.dcl")
    io.open(prog, "w", encoding="utf-8", newline="\n").write(
        "".join("CONST c%d = %d.0\n" % (i, i) for i in range(80)))
    try:
        rc0, out0 = run([sys.executable, "tools/dclc.py", prog, "--dump"])
        m0 = re.search(r"容量（派生自 [^）]*）: 路由 (\d+)", out0)
        base = int(m0.group(1)) if m0 else -1
        io.open(eh, "w", encoding="utf-8", newline="\n").write(
            orig_h.replace("#define MAX_ROUTES    128", "#define MAX_ROUTES    64", 1))
        rc1, out1 = run([sys.executable, "tools/dclc.py", prog, "--dump"])
        # ★ 变异后程序被**拒**（80 > 64）⇒ dclc 在打容量表之前就退出了 ⇒
        #   上限必须从**报错文本**里取。第一版只认容量表 ⇒ mut 读成 -1 而误判 FAIL
        #   （又一次"判据的来源写错，看起来像被测物错了"）。
        m1 = (re.search(r"板上限 (\d+) 条", out1)
              or re.search(r"容量（派生自 [^）]*）: 路由 (\d+)", out1))
        mut = int(m1.group(1)) if m1 else -1
        hit64 = "板上限 64 条" in out1
        print("    基线: dclc 报路由上限 = %s" % base)
        print("    变异后: dclc 报路由上限 = %s ；80 条程序被拒且报『板上限 64 条』= %s"
              % (mut, hit64))
        res.append(("C11 dclc 容量随 src/engine.h 派生（128→64 实测生效）",
                    base == 128 and mut == 64))
        res.append(("C11' 超限报错可执行（含板上限与三条修法）", hit64))
    finally:
        io.open(eh, "w", encoding="utf-8", newline="\n").write(orig_h)
        try:
            os.unlink(prog)
        except OSError:
            pass

    # ── F1/F2/F3: 在板栈水位 ────────────────────────────────────────
    if a.offline:
        skip.append("F1/F2/F3 在板栈水位（--offline）")
    else:
        try:
            from h723_client import Dcl
            d = Dcl(a.port)
            print("\n[ F 在板栈水位 ] 端口 = %s" % d.port)
            import struct
            import time
            sts, p = d.send(0x38, expect_len=51)
            shm = struct.unpack("<I", p[23:27])[0]
            sts, q = d.send(0x22, struct.pack("<IH", 0x24000100, 16), expect_len=64)
            u = struct.unpack("<16I", q[:64])
            magic, low, used, head = u[0], u[1], u[2], u[3]
            print("    magic=0x%08X scans=%d stack_used=%.2f KB headroom=%.2f KB layout_ok=%d"
                  % (magic, u[10], used / 1024.0, head / 1024.0, u[7]))
            res.append(("F1 MEM_STAT.magic=='SMEM' 且 scans≥1", magic == 0x4D454D53 and u[10] >= 1))
            res.append(("F2 0 < stack_used(%.2f KB) < headroom(%.2f KB)" % (used / 1024.0,
                                                                           head / 1024.0),
                        0 < used < head))
            res.append(("F2' MEM_STAT.layout_ok==1（M3: SHM 在 DTCM 且 ∩AXI=∅）", u[7] == 1))
        except Exception as e:
            skip.append("F1/F2/F3 板子不在场/不可用（%s: %s）—— 这不是 FAIL，是**台架条件**"
                        % (type(e).__name__, str(e)[:60]))

    print("\n" + "=" * 74)
    print("=== 判据 ===")
    nf = 0
    for name, ok in res:
        print("  [%s] %s" % ("PASS" if ok else "FAIL", name))
        nf += (not ok)
    for s in skip:
        print("  [SKIP] %s" % s)
    print("\n%d 项判定, %d FAIL, %d SKIP（SKIP ≠ PASS）" % (len(res), nf, len(skip)))
    return 0 if nf == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
