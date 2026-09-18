#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""E-Z —— 2.3 `EXEC_BUDGET_CYCLES`：把**已登记的耦合 #6** 关掉（运行期预算随拍长缩放）

## 缺陷（不是新设计决定）
`engine.h` 的注释自己写着"取值: 拍长 40000 cyc，取 **80% = 32000**" ——
也就是说**意图本来就是 80%**，只是把算出来的数**手写**进去了。
E-U 之后拍长可配（`bash build.sh -DDCL_TICK_US=200`）⇒ 那个手写数在 200 µs 档上
只剩 **40% 拍** ⇒ 运行期"本拍超载"判据（`EXEC_BUDGET_CYCLES`）被**静默放宽一倍**，
而**没有任何东西会响**。这就是 PLAN 2.3 登记的**耦合 #6**。

## 处方
`EXEC_BUDGET_CYCLES` 改成**派生量**（`CLK_TICK_CYCLES × EXEC_BUDGET_PCT / 100`），
并把派生关系写成**四条能失败的断言**。100 µs 档下派生值 = 32000 ⇒ **逐位等于改动前**
（所以交付档 hex 指纹必须**不变** —— 这本身就是 Z5）。

## 判据（都能失败）
  Z1 源码级: `EXEC_BUDGET_CYCLES` 的定义是**表达式**（取 `CLK_TICK_CYCLES` 与
     `EXEC_BUDGET_PCT`），且四条断言在场
  Z2 ★ **编译期预言机**: 用真编译器编译探针 TU
     （`_Static_assert(EXEC_BUDGET_CYCLES == PROBE_EXPECT)`）
       100 µs + 期望 32000 ⇒ 通过 ； 200 µs + 期望 64000 ⇒ 通过
  Z3 ★ 负对照: 200 µs + 期望 **32000** ⇒ **必须编译失败**
     （证明"它真的随拍长缩放", 而不是我把宏读错了）
  Z4 ★ **变异对照**: 在临时副本里把定义改回**手写 32000u**, 则
       200 µs 档 **必须编译失败**（第 ④ 条断言抓住它）
       —— 这一条证明"断言是承重的", 而不是装饰
  Z5 ★ 交付档 hex 指纹 == 基线（本项**逐位无回归**）

用法: python tools/exp_ez_exec_budget_scale.py
退出码: 0 = 全 PASS / 1 = 有 FAIL / 2 = 前置不满足
"""
import io, os, re, shutil, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "src")
DELIVERY_MD5 = "6daa7e65a778ac308267785104426f17"

PROBE = '''/* E-Z 探针: 用**编译器自己**当预言机读一个编译期常量。
 * `EXEC_BUDGET_CYCLES` 只出现在 ISR 的比较里(立即数) ⇒ 既不进 map 也不进 .rodata,
 * 读 ELF 读不到。而 `_Static_assert(… == PROBE_EXPECT)` 让编译器替我们判:
 * 编译过 / 不过就是判据的输出, 而且**能失败**（Z3/Z4 就是它的失败态）。 */
#include "engine.h"
_Static_assert(EXEC_BUDGET_CYCLES == PROBE_EXPECT,
               "E-Z probe: EXEC_BUDGET_CYCLES != PROBE_EXPECT");
int dcl_probe_anchor(void) { return (int)EXEC_BUDGET_CYCLES; }
'''


def toolchain():
    """★ 从项目自己的 `cmake/arm-none-eabi.cmake` 读出工具链路径 —— 不写死。"""
    cm = io.open(os.path.join(ROOT, "cmake", "arm-none-eabi.cmake"),
                 encoding="utf-8", errors="replace").read()
    m = re.search(r'set\(TOOLCHAIN_BIN\s+"([^"]+)"\)', cm)
    if not m:
        raise SystemExit("!! arm-none-eabi.cmake 里找不到 TOOLCHAIN_BIN（拒绝猜）")
    gcc = os.path.join(m.group(1), "arm-none-eabi-gcc.exe")
    if not os.path.exists(gcc):
        raise SystemExit("!! 工具链不存在: %s（若要跑本项先装 STM32CubeIDE 工具链）" % gcc)
    return gcc


def compile_probe(gcc, tick_us, expect, incdirs):
    """返回 (rc, 输出)。★ 只编译一个 TU ⇒ 秒级, 且**只依赖 engine.h/clock.h**。"""
    fd, src = tempfile.mkstemp(suffix=".c", prefix="_ezprobe_")
    os.close(fd)
    try:
        io.open(src, "w", encoding="utf-8").write(PROBE)
        cmd = [gcc, "-c", "-std=gnu11"]
        for d in incdirs:
            cmd += ["-I", d]
        cmd += ["-DCLK_TICK_US=%d" % tick_us, "-DPROBE_EXPECT=%d" % expect,
                src, "-o", src + ".o"]
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    finally:
        for p in (src, src + ".o"):
            try:
                os.unlink(p)
            except OSError:
                pass


def main():
    res = []
    print("=" * 74)
    print("E-Z  2.3 运行期预算随拍长缩放（关掉耦合 #6）")
    print("=" * 74)
    gcc = toolchain()
    print("工具链: %s" % gcc)

    # ── Z1 源码级 ─────────────────────────────────────────────────────
    print("\n── Z1 源码级: 定义是**表达式** + 四条断言在场 ──")
    eh = io.open(os.path.join(SRC, "engine.h"), encoding="utf-8", errors="replace").read()
    md = re.search(r"^#define\s+EXEC_BUDGET_CYCLES\s+([^\n]*)", eh, re.M)
    body = md.group(1).strip() if md else ""
    has_expr = ("CLK_TICK_CYCLES" in body) and ("EXEC_BUDGET_PCT" in body)
    n_assert = len(re.findall(r"_Static_assert\([^;]*EXEC_BUDGET", eh, re.S))
    print("    #define EXEC_BUDGET_CYCLES %s" % body)
    print("    引用拍长/百分比 = %s ；与预算相关的断言 = %d 条" % (has_expr, n_assert))
    res.append(("Z1 定义是派生表达式（含 CLK_TICK_CYCLES + EXEC_BUDGET_PCT）", has_expr))
    res.append(("Z1' 预算相关断言 ≥ 4 条（含『展开式逐位相等』那条）", n_assert >= 4))

    # ── Z2 编译期预言机 ───────────────────────────────────────────────
    print("\n── Z2 ★ 编译期预言机: 真编译器判 EXEC_BUDGET_CYCLES 的值 ──")
    for tick, exp in ((100, 32000), (200, 64000)):
        rc, out = compile_probe(gcc, tick, exp, [SRC])
        print("    -DCLK_TICK_US=%-4d 期望 %-6d ⇒ rc=%d %s"
              % (tick, exp, rc, "（编译通过 = 值相等）" if rc == 0 else "（失败）"))
        res.append(("Z2 %dµs 档 EXEC_BUDGET_CYCLES == %d" % (tick, exp), rc == 0))

    # ── Z3 负对照: 期望值写死 32000 在 200µs 档必须失败 ───────────────
    print("\n── Z3 ★ 负对照: 200 µs 档期望 **32000** ⇒ 必须编译失败 ──")
    rc, out = compile_probe(gcc, 200, 32000, [SRC])
    print("    rc=%d ；报错含 static assertion = %s" % (rc, "static assertion failed" in out))
    res.append(("Z3 200µs 档 + 期望 32000 编译失败（证明它真的缩放, 不是我读错宏）",
                rc != 0))
    print("    " + next((l.strip() for l in out.splitlines() if "static assertion" in l),
                         "<无>")[:96])

    # ── Z4 变异对照: 手写 32000u 必须被断言抓住 ───────────────────────
    print("\n── Z4 ★ 变异对照: 把定义改回**手写 32000u**（临时副本）, 200 µs 档必须编不过 ──")
    mut = tempfile.mkdtemp(prefix="_ezmut_")
    try:
        shutil.copy(os.path.join(SRC, "clock.h"), mut)
        mh = eh.replace(
            "#define EXEC_BUDGET_CYCLES   ((CLK_TICK_CYCLES * EXEC_BUDGET_PCT) / 100u)",
            "#define EXEC_BUDGET_CYCLES   32000u")
        assert mh != eh, "变异没生效: 定义行没匹配上（说明定义文本变了, 请同步本工具）"
        io.open(os.path.join(mut, "engine.h"), "w", encoding="utf-8",
                newline="\n").write(mh)
        rc200, o200 = compile_probe(gcc, 200, 64000, [mut, SRC])
        rc100, o100 = compile_probe(gcc, 100, 32000, [mut, SRC])
        hit = next((l.strip() for l in o200.splitlines() if "must be" in l or "static assertion" in l),
                   "<无>")
        print("    变异体 + 200µs 期望 64000 ⇒ rc=%d（**必须非 0**）" % rc200)
        print("    变异体 + 100µs 期望 32000 ⇒ rc=%d（**必须 0** —— 变异只在换档时暴露）" % rc100)
        print("    报错: %s" % hit[:96])
        res.append(("Z4 手写 32000u 在 200µs 档被断言抓住（断言是承重的）", rc200 != 0))
        res.append(("Z4' 同一个变异体在 100µs 档**仍然编过**（说明它只错在换档场景）",
                    rc100 == 0))
    finally:
        shutil.rmtree(mut, ignore_errors=True)

    # ── Z5 交付档指纹 ─────────────────────────────────────────────────
    print("\n── Z5 ★ 交付档 hex 指纹（本项应**逐位无回归**）──")
    hexp = os.path.join(ROOT, "build", "dcl_h723.hex")
    import hashlib
    if os.path.exists(hexp):
        got = hashlib.md5(open(hexp, "rb").read()).hexdigest()
        print("    build/dcl_h723.hex md5 = %s（基线 %s）" % (got, DELIVERY_MD5))
        res.append(("Z5 交付档指纹不变（%s）" % DELIVERY_MD5, got == DELIVERY_MD5))
    else:
        res.append(("Z5 交付档指纹（缺 build/dcl_h723.hex ⇒ 先跑 h723_restore_delivery.sh）",
                    False))

    print("\n" + "=" * 74)
    print("=== 判据 ===")
    nf = 0
    for name, ok in res:
        print("  [%s] %s" % ("PASS" if ok else "FAIL", name))
        nf += (not ok)
    print("\n%d 项判定, %d FAIL" % (len(res), nf))
    return 0 if nf == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
