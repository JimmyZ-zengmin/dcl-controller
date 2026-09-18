#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dclc_rate_selftest.py —— **证明 dclc 的"跨档速率检查"能失败**（结构性修复的对照）

## 为什么必须有这个文件
本项目纪律：**结构性修复必须配"能失败的对照构建"** ——
修完只看到"0 违规"不构成证据，你无法排除"这条检查根本不会红"。

而这条检查**有真血证**：写 `examples/h723_step_stall_recover.dcl` 时，我把判据链降到 1ms 档，
固件用 `NAK: rate mismatch`（`src/main.c:1838`）**拒了两次** —— 每次都要烧写往返。
⇒ 现在它被镜像到**编译期**（`tools/dclc.py` 的 `compile_stmts`），本脚本证明它真的会红。

## 判据
  C1 ★ **违规样例必须被拒**（0.1ms 的消费者读 1ms 的生产者）—— 红不出 ⇒ 本条判据无效
  C2 **合法样例必须通过**（整条链同档）—— 否则这条检查是"见谁都拦"的假闸门
  C3 **既有 examples 全部仍能编过**（回归：新检查不许误伤）

用法: python tools/dclc_rate_selftest.py
"""
import io
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DCLC = os.path.join(HERE, "dclc.py")

# C1 违规：`slow` 跑 1ms 档、而它的生产者 `fast` 是每拍 ⇒ 固件判 "rate mismatch"
BAD = """CONST   one = 1.0
ADD     fast  FROM one BY=one
CONST   two = 2.0
ADD     slow  FROM fast BY=two PERIOD=1ms
OUTPUT  o     TO wire[12] FROM slow
"""

# C2 合法：整条链同档（消费者与生产者同 div，或消费者更快）
GOOD = """CONST   one = 1.0
ADD     fast  FROM one BY=one
CONST   two = 2.0
ADD     slow  FROM fast BY=two
OUTPUT  o     TO wire[12] FROM slow
"""


def compile_text(text, tag):
    """返回 (rc, 输出)；rc==0 表示编过。★ 用**真子进程**调 dclc: 与用户路径完全一致。

    ★★★ 2026-09-18 修: 原来没给 `encoding=` ⇒ Windows 上 subprocess 用 **GBK** 解 dclc 的
      **UTF-8** 输出 ⇒ 要么抛 UnicodeDecodeError、要么错码 ⇒ 下面比对中文报错文本
      (`"跨档速率不合法" in out`) **恒不命中** ⇒ **C1 从来就是假 FAIL**。
      这不是小毛病: 它意味着"这条检查能红"的**唯一证明**一直是无效的
      （而本文件开头的理由恰恰是"必须有能失败的对照"）。"""
    fd, path = tempfile.mkstemp(suffix=".dcl", prefix="_ratetest_")
    os.close(fd)
    try:
        io.open(path, "w", encoding="utf-8").write(text)
        r = subprocess.run([sys.executable, DCLC, path, "--dump"],
                           capture_output=True, text=True, cwd=ROOT,
                           encoding="utf-8", errors="replace")
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def main():
    n_ok = 0
    print("=== dclc 跨档速率检查 自检（对照：这条判据必须能红）===")
    rc, out = compile_text(BAD, "bad")
    hit = "跨档速率不合法" in out
    print("  [%s] C1 违规样例**被拒**（这是本条判据存在的唯一证明）" % ("PASS" if (rc != 0 and hit) else "FAIL"))
    if rc == 0:
        print("       ★ rc=0 ⇒ **检查是空的**（没红）—— 判据无效，不是「通过」")
    elif not hit:
        print("       报错原因不是跨档检查（rc=%d）⇒ 样例本身有别的错" % rc)
        print("       " + out.strip().splitlines()[0] if out.strip() else "")
    else:
        n_ok += 1
        print("       拒因: " + [l for l in out.splitlines() if "跨档" in l][0].strip())

    rc2, out2 = compile_text(GOOD, "good")
    print("  [%s] C2 合法样例（同档）**编过**（否则就是见谁都拦的假闸门）"
          % ("PASS" if rc2 == 0 else "FAIL"))
    if rc2 != 0:
        print("       " + (out2.strip().splitlines()[0] if out2.strip() else ""))
    else:
        n_ok += 1

    ex = sorted(f for f in os.listdir(os.path.join(ROOT, "examples")) if f.endswith(".dcl"))
    bad = []
    for f in ex:
        r = subprocess.run([sys.executable, DCLC, os.path.join(ROOT, "examples", f), "--dump"],
                           capture_output=True, text=True, cwd=ROOT,
                           encoding="utf-8", errors="replace")
        if r.returncode != 0:
            bad.append(f)
    print("  [%s] C3 既有 examples 全部仍能编过（%d 个，回归）"
          % ("PASS" if not bad else "FAIL", len(ex)))
    if bad:
        print("       误伤: %s" % ", ".join(bad))
    else:
        n_ok += 1

    print("=== %d/3 通过 ===" % n_ok)
    return 0 if n_ok == 3 else 1


if __name__ == "__main__":
    sys.exit(main())
