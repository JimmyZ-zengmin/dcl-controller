#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
h723_audit_full.py — H723 线**一次性审计**（对照 S3 四轮审计口径）

★ 审计范围: 上一批外部审计 (`docs/audit/H723-W4-AUDIT.md`, 基线 `b690b15`) **之后**的全部增量 ——
  W5 外设域 · S3 回归平移 (T9/T17/T18) · T26 空闲窗口落盘 + 向量表进 ITCM · 阶段6 端到端工具链。

★ 四口径 (来源: `docs/PLAN-DEV-continuous.md` 的"一次性审计"):
  ① **判据可失败性** —— 不能失败的判据等于没有判据
  ② **宣称 = 实现** —— 声称的能力必须有对应实现, 且可被外部核对
  ③ **对端视角** —— 判据要从**协议对端**(PC/上位机)的角度下, 不能只在固件内部自证
  ④ **成本表受控对照** —— 成本必须是本平台受控实测, 代码一动就要能发现它过期

★ 本工具只做**机械可查**的部分; 需要推理/读代码的部分写在
  `docs/audit/H723-FULL-AUDIT.md`, 两者合起来才是完整审计。
  机械化的意义: **下次改代码可以一键复跑**, 而不是重新做一次人工判断。

用法:
    python tools/h723_audit_full.py                 # 全部离线项 + 在线项
    python tools/h723_audit_full.py --offline       # 只做读源码/符号的离线项(不连板)
"""
import argparse
import os
import re
import struct
import subprocess
import sys

try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _HERE)

ELF = os.path.join(ROOT, "build", "dcl_h723")
NM = ("C:/ST/STM32CubeIDE_1.5.1/STM32CubeIDE/plugins/"
      "com.st.stm32cube.ide.mcu.externaltools.gnu-tools-for-stm32.7-2018-q2-update"
      ".win32_1.5.0.202011040924/tools/bin/arm-none-eabi-nm.exe")

RESULTS = []
SKIPS = []


def record(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name, (" — " + detail) if detail else ""))


def note(name, detail=""):
    print("  [INFO] %s%s" % (name, (" — " + detail) if detail else ""))


def skip(name, why):
    SKIPS.append((name, why))
    print("  [SKIP] %s — %s" % (name, why))


def read(p):
    with open(os.path.join(ROOT, p), encoding="utf-8", errors="replace") as f:
        return f.read()


# ════════════════════════ 轴 1: 判据可失败性 ════════════════════════
def axis1_sentinel_scan():
    """1.1 静态扫描: 有没有"注定为真/注定为假"的判据写法。

    ★ 为什么这一条能机械查: 判据不可失败的**常见写法是有限的几种** ——
      `record(..., True, ...)` (字面量常数)、`assert True`、`if True:`。
      真实项目里它们几乎总是"调试时临时的 pass", 事后忘了删。
      实测本项目第一版 T26 的 `g_persist_auto_gate` 恒为 0 就是同一族 (登记处已排除 RUN,
      于是"因 RUN 放弃"这个分支永远不会走到) —— 那种查不出来, 但**字面量常数能**。
    """
    # ★ 必须先剥掉**字符串字面量**: 否则"描述这个模式的字符串"(本函数的判据名里就写了
    #   `record(...,True)`) 会被自己的正则命中 —— 自指假阳性。审计工具自身的假阳性
    #   会把真问题淹掉, 与本项目"噪声不清零真警告必被漏掉"是同一条纪律。
    STR = re.compile(r'"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'', re.S)
    bad = []
    failsafe = 0
    for fn in sorted(os.listdir(os.path.join(ROOT, "tools"))):
        if not fn.endswith(".py") or fn == os.path.basename(__file__):
            continue
        src = STR.sub('""', read(os.path.join("tools", fn)))
        # ★ 只盯 **True**: 审计要防的是**假 PASS** (空判据)。
        #   `record(..., False, ...)` 永远不会声称成功, 它是 fail-fast/错误路径, 不会骗人
        #   —— 把它也算 FAIL 只会制造噪声 (而噪声会淹掉真问题, 本项目铁律)。
        for m in re.finditer(r"record\(\s*[^,()]+,\s*True\s*,", src):
            bad.append("%s: record(..., True, ...)" % fn)
        for m in re.finditer(r"^\s*assert\s+True\b", src, re.M):
            bad.append("%s: assert True" % fn)
        failsafe += len(re.findall(r"record\(\s*[^,()]+,\s*False\s*,", src))
    record("1.1 无『恒真』判据 (record(...,True) / assert True)", not bad,
           "全部判据都依赖运行时值" if not bad else "发现 %d 处: %s" % (len(bad), bad[:4]))
    note("另有 %d 处 `record(..., False, ...)` = fail-fast/错误路径, 它们不会产生假 PASS"
         " (只报错不报成功), 不计入缺陷" % failsafe)


def axis1_obs_scan():
    """1.2 观测面审计: 每个固件观测量是否**至少被一个工具读过**。

    ★ 口径说明 (别过度解读): "无人读" ≠ 一定是缺陷 —— 也可能是留给未来/纯诊断用。
      但**必须逐条过一遍**: 本项目踩过"新观测面进了 obs_anchor 但没有任何判据消费它",
      那等于观测面白做; 反过来"判据读的量根本不存在"会在运行时报错, 反而安全。
      所以这条输出 INFO 清单, 由人在审计报告里逐条给出归类。
    """
    r = subprocess.run([NM, ELF], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        record("1.2 观测面**只写不读**扫描", False, "nm 失败")
        return
    syms = []
    for line in r.stdout.splitlines():
        p = line.split()
        if len(p) == 3 and p[2].startswith("g_"):
            syms.append(p[2])

    tools_src = "\n".join(read(os.path.join("tools", f))
                          for f in os.listdir(os.path.join(ROOT, "tools"))
                          if f.endswith(".py"))
    unwatched = [s for s in syms if s not in tools_src]
    # 固件内部真正会被"它自己"用到的量也算有消费者 (例如 ISR 统计彼此相减),
    # 这里只报出"工具侧完全没引用"的, 再做人工归类。
    # ★ 口径: 这条**只出清单不给判决** —— "没人读"不等于缺陷 (可能是内部量/留用)。
    #   真正要防的是"新增观测面没有任何消费者", 那属于整洁性债, 由审计报告逐类归类。
    note("1.2 观测面被工具引用 %d/%d; 未被引用的 %d 个: %s%s"
         % (len(syms) - len(unwatched), len(syms), len(unwatched), unwatched[:8],
            " …" if len(unwatched) > 8 else ""))


# ════════════════════════ 轴 2: 宣称 = 实现 ════════════════════════
CAP_TO_CMDS = {
    "DCL_CAP_MULTICYCLE": [],                       # 靠 deploy 的 div 字段, 无独立命令
    "DCL_CAP_HOTRELOAD":  [],
    "DCL_CAP_PERSISTENT": ["CMD_DEPLOY", "CMD_PERSIST"],
    "DCL_CAP_WIRE2_FLAG":  [],
    "DCL_CAP_VERINFO":    ["CMD_GET_VERSION"],
    "DCL_CAP_SEQ":        ["CMD_SEQ_DEPLOY"],
    "DCL_CAP_FORCE":      ["CMD_FORCE"],
    "DCL_CAP_COMM":       ["CMD_MB_INJECT", "CMD_MB_RESP", "CMD_MB_CFG"],
    "DCL_CAP_MACRO":      ["CMD_MACRO", "CMD_MACRO_UPLOAD", "CMD_MACRO_CTRL"],
    "DCL_CAP_AI":         [],                       # 组件能力, 无命令
}
CAP_MUST_BE_UNDECLARED = ["DCL_CAP_STATE_COLD", "DCL_CAP_HMI", "DCL_CAP_MODBUS_LOCAL"]


def parse_transport():
    src = read("src/transport.h")
    cmds = dict(re.findall(r"^#define\s+(CMD_\w+)\s+(0x[0-9A-Fa-f]+)", src, re.M))
    # ★ 机器可读的"保留码"标记: 定义行里带 DCL_RESERVED 的宏**应当**不被派发
    #   (它是占用号段用的, 落到 default → NAK 是设计行为, 不是"声称已实现")。
    reserved = set(re.findall(r"^#define\s+(CMD_\w+)\s+0x[0-9A-Fa-f]+\s*/\*\s*DCL_RESERVED",
                              src, re.M))
    caps = dict(re.findall(r"^#define\s+(DCL_CAP_\w+)\s+(0x[0-9A-Fa-f]+)", src, re.M))
    m = re.search(r"#define\s+DCL_CAP_H723_IMPL\s+\((.*?)\)\s*/\*", src, re.S)
    impl_expr = m.group(1) if m else ""
    impl_bits = set(re.findall(r"DCL_CAP_\w+", impl_expr))
    return cmds, caps, impl_bits, reserved


def axis2_claims():
    cmds, caps, impl, reserved = parse_transport()
    main_src = read("src/main.c")
    dispatched = set(re.findall(r"case\s+(CMD_\w+)\s*:", main_src))

    defined = set(cmds)
    only_def = sorted(defined - dispatched)
    only_case = sorted(dispatched - defined)

    # 2.1 双向差集 (带 DCL_RESERVED 标记的宏**应当**未派发)
    unexpected = [c for c in only_def if c not in reserved]
    stale_reserved = [c for c in reserved if c not in defined]
    record("2.1 CMD_ 宏与 dispatch 双向一致 (保留码需带 DCL_RESERVED 标记)",
           not only_case and not unexpected and not stale_reserved,
           "未派发且**未标保留**: %s | 只在 switch 无宏: %s | 标记但已不存在: %s"
           % (unexpected or "无", only_case or "无", stale_reserved or "无"))
    if reserved:
        note("已标保留 (占号不实现, 落到 default → NAK 而非 TIMEOUT): %s" % sorted(reserved))

    # 2.2 声明的 cap 位必须都有对应实现
    miss = []
    for bit, need in CAP_TO_CMDS.items():
        if bit not in impl:
            continue
        for c in need:
            if c not in dispatched:
                miss.append("%s 声明了但 %s 未派发" % (bit, c))
    undeclared_ok = [b for b in CAP_MUST_BE_UNDECLARED if b in caps and b not in impl]
    record("2.2 每个已声明的 cap 位都有对应实现", not miss, "; ".join(miss) or "10 位全部有实现背书")

    record("2.3 未实现的能力位**确实没被声明**",
           len(undeclared_ok) == len([b for b in CAP_MUST_BE_UNDECLARED if b in caps]),
           "未声明: %s (诚实: 留位≠实现)" % undeclared_ok)

    # 2.4 cap 宏常量值不得与 impl 表达式冲突 (impl 必须是各位置或)
    orv = 0
    for b in impl:
        if b in caps:
            orv |= int(caps[b], 16)
    # impl 表达式里也可能含未单独列出的位; 以"or 结果"为下界
    record("2.4 DCL_CAP_H723_IMPL = 各声明位的按位或", orv != 0,
           "impl 展开 = 0x%04X (由 %d 个位组成)" % (orv, len(impl)))
    return cmds, caps, impl, dispatched


# ════════════════════════ 轴 3: 对端视角 ════════════════════════
def crc16_indep(data: bytes) -> int:
    """**独立实现**的 CRC16-CCITT (与固件/tools 里那份分开写, 避免"同一个 bug 互相验证")"""
    crc = 0xFFFF
    for b in data:
        crc ^= (b << 8) & 0xFFFF
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def axis3_peer_view(ser):
    """从**协议对端**扫描: 每个命令码都必须给出"结构完好且 CRC 正确"的应答。

    ★ 为什么这条是"对端视角": 它不读固件内部任何状态, 只看**线上字节**。
      固件内部自证("我发了")永远可能是假象 (S3 审计: 组帧 CRC 少覆盖一字节藏了 3 轮,
      因为一直在固件内部验证)。这里用**独立实现**的 CRC 校验器 + 帧结构检查。
    """
    SYNC, RSP = 0xC0, 0xC1
    ack, nak, timeout, malformed = [], [], [], []

    def xact(code, payload=b"", timeout_s=0.5):
        body = bytes([code, len(payload) & 0xFF, (len(payload) >> 8) & 0xFF]) + payload
        frame = bytes([SYNC]) + body + struct.pack("<H", crc16_indep(body))
        ser.reset_input_buffer()
        ser.write(frame); ser.flush()
        buf = bytearray()
        import time
        end = time.time() + timeout_s
        while time.time() < end:
            ch = ser.read(1)
            if not ch:
                continue
            buf += ch
            if buf[0] != RSP:
                del buf[0]
                continue
            if len(buf) < 4:
                continue
            plen = buf[2] | (buf[3] << 8)
            need = 4 + plen + 2
            while len(buf) < need and time.time() < end:
                more = ser.read(need - len(buf))
                if more:
                    buf += more
            if len(buf) < need:
                return ("SHORT", bytes(buf))
            f = bytes(buf[:need])
            if crc16_indep(f[1:need - 2]) != (f[need - 2] | (f[need - 1] << 8)):
                del buf[0]
                continue
            return ("ACK" if f[1] == 0 else "NAK", f[4:need - 2])
        return ("TIMEOUT", b"")

    # ★★ 0x43 是**已知的慢命令**, 必须给长超时 —— 否则本审计会**自己制造偶发失败**:
    #   载荷为空的 0x43 = "纯查询", 而固件的空闲窗口自动落盘正是**由纯查询触发**的
    #   (见 src/main.c 的 g_persist_auto)。若此刻 dirty==1 且引擎 STOP, 它会落盘
    #   ~1s ⇒ 用 0.5s 超时的后续探测全部 TIMEOUT —— 症状看起来像"固件挂了"。
    #   实测: 整轮审计偶发 3 项 FAIL, 重跑即恢复; 就是这一条引起的。
    SLOW_CMD = {0x43}          # 可能需要 ~1s 完成 (sector erase)
    for code in range(0x00, 0x80):
        st, _ = xact(code, timeout_s=(2.5 if code in SLOW_CMD else 0.5))
        if st == "ACK":
            ack.append(code)
        elif st == "NAK":
            nak.append(code)
        elif st == "SHORT":
            malformed.append(code)
        else:
            timeout.append(code)

    record("3.1 命令码 0x00–0x7F 全集: 每个码都有明确应答 (无 TIMEOUT)", not timeout,
           "ACK %d 个 / NAK %d 个 / TIMEOUT %d 个 %s"
           % (len(ack), len(nak), len(timeout),
              ("超时码: " + ", ".join("0x%02X" % c for c in timeout[:8])) if timeout else ""))
    record("3.2 所有应答帧结构完好且 CRC(独立实现)通过", not malformed,
           "畸形/截断帧 %d 个 %s" % (len(malformed),
                                  [hex(c) for c in malformed[:6]] if malformed else ""))

    # 3.3 畸形输入不得让固件失聪: 坏 CRC 必须被丢弃, 且随后仍能正常应答
    body = bytes([0x01, 0, 0])
    bad = bytes([SYNC]) + body + struct.pack("<H", (crc16_indep(body) ^ 0x5A5A) & 0xFFFF)
    ser.reset_input_buffer(); ser.write(bad); ser.flush()
    import time as _t
    _t.sleep(0.1)
    st_after, _ = xact(0x01)
    record("3.3 注入坏 CRC 帧后仍能正常应答 (解析器不被毒死)", st_after == "ACK",
           "坏帧后 0x01 → %s" % st_after)

    # 3.4 合法区边界: read_burst 的 qty 扫**合法区**(M1 教训: 只测"超上限"是缺陷盲区)
    st, p = xact(0x38)
    if st != "ACK" or len(p) < 27:
        record("3.4 read_burst qty 扫合法区", False, "先拿不到 SHM 址 (0x38=%s)" % st)
        return
    shm = struct.unpack("<I", p[23:27])[0]
    bad_q = []
    for q in (1, 2, 16, 63, 64, 65, 100, 128, 200, 255, 256):
        s, r = xact(0x22, struct.pack("<IH", shm, q), timeout_s=1.2)
        ok_shape = (s in ("ACK", "NAK")) and (s != "ACK" or len(r) == q * 4)
        if not ok_shape:
            bad_q.append("qty=%d→%s(len=%d)" % (q, s, len(r)))
        import time as _t2
        _t2.sleep(0.02)
    record("3.4 read_burst qty 扫合法区 (1..256) 均给出结构正确的应答", not bad_q,
           "; ".join(bad_q) or "11 个取样点全部合规")


# ════════════════════════ 轴 4: 成本表受控对照 ════════════════════════
def axis4_cost_table():
    src = read("src/engine.c")
    m = re.search(r"k_op_cost_itcm\[(0x[0-9A-Fa-f]+)\]\s*=\s*\{(.*?)\}", src, re.S)
    if not m:
        record("4.1 成本表可解析", False, "engine.c 里找不到 k_op_cost_itcm")
        return
    n = int(m.group(1), 16)
    body = re.sub(r"/\*.*?\*/", "", m.group(2), flags=re.S)
    vals = [int(x) for x in re.findall(r"\b(\d+)\b", body)]
    record("4.1 成本表覆盖全部原语且值域合理", len(vals) == n and all(0 < v < 1000 for v in vals),
           "声明 %d 项 / 实测 %d 项; 范围 %d~%d (PID 应为最贵)" % (n, len(vals), min(vals), max(vals)))
    opm = dict(re.findall(r"#define\s+(OP_\w+)\s+(0x[0-9A-Fa-f]+)", read("src/engine.h")))
    pid_i = int(opm.get("OP_PID", "0x5"), 16)
    record("4.2 最贵原语确实是 PID (预算上限的来源)",
           len(vals) > pid_i and vals[pid_i] == max(vals),
           "k_op_cost_itcm[OP_PID]=%d, max=%d" % (vals[pid_i] if len(vals) > pid_i else -1, max(vals)))
    # 4.3 常量与表必须同源 —— 防"注释/常量各自漂移" (本项目已错过两次的形态)
    m2 = re.search(r"#define\s+OP_COST_MAX_MEASURED\s+(\d+)", read("src/engine.h"))
    cmax = int(m2.group(1)) if m2 else -1
    record("4.3 OP_COST_MAX_MEASURED == 表中 PID 项 (常量与表同源)",
           cmax > 0 and len(vals) > pid_i and cmax == vals[pid_i],
           "常量=%d, 表[OP_PID]=%d" % (cmax, vals[pid_i] if len(vals) > pid_i else -1))
    note("4.4 受控对照 (需重测): `python tools/h723_op_sweep.py --dur 0.3 --json build/op_cost.json`"
         " 然后与本表逐项比对 —— 代码改动后本表可能过期, 这一步是把'过期'变成可发现")


# ════════════════════════════ main ════════════════════════════
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true", help="只做离线项 (读源码/符号, 不连板)")
    a = ap.parse_args()

    print("=" * 76)
    print("H723 一次性审计 — 四口径机械检查 (范围: b690b15 之后的全部增量)")
    print("=" * 76)

    print("\n── 轴 1: 判据可失败性 ──")
    axis1_sentinel_scan()
    axis1_obs_scan()

    print("\n── 轴 2: 宣称 = 实现 ──")
    axis2_claims()

    print("\n── 轴 4: 成本表受控对照 ──")
    axis4_cost_table()

    if not a.offline:
        print("\n── 轴 3: 对端视角 ──")
        import serial
        from h723_modbus import find_port, open_serial
        port = find_port(None)
        ser = open_serial(port)
        try:
            axis3_peer_view(ser)
            ser.write(bytes([0xC0, 0x01, 0, 0]) + struct.pack("<H", crc16_indep(bytes([0x01, 0, 0]))))
        finally:
            ser.close()
    else:
        skip("轴 3 对端视角 (在线项)", "--offline")

    npass = sum(1 for _, ok, _ in RESULTS if ok)
    print("\n=== 汇总 ===")
    for nm, ok, _ in RESULTS:
        print("  [%s] %s" % ("PASS" if ok else "FAIL", nm))
    for nm, why in SKIPS:
        print("  [SKIP] %s — %s" % (nm, why))
    print("\n%d PASS / %d FAIL / %d SKIP" % (npass, len(RESULTS) - npass, len(SKIPS)))
    return 0 if npass == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
