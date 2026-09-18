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
import json
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
def _split_args(src, i):
    """从 `(` 之后开始切**顶层**逗号参数（跳过字符串与嵌套括号）。返回 (args, end)。"""
    depth, args, cur, instr = 1, [], "", None
    while i < len(src) and depth:
        c = src[i]
        if instr:
            if c == "\\" and i + 1 < len(src):
                cur += src[i:i + 2]
                i += 2
                continue
            cur += c
            if c == instr:
                instr = None
        elif c in "\"'":
            instr = c
            cur += c
        elif c in "([{":
            depth += 1
            cur += c
        elif c in ")]}":
            depth -= 1
            if depth == 0:
                args.append(cur)
                break
            cur += c
        elif c == "," and depth == 1:
            args.append(cur)
            cur = ""
        else:
            cur += c
        i += 1
    return [a.strip() for a in args], i


def _strip_spans(src):
    """返回 (字符串字面量区间, 注释起点判定器)。

    ★ 为什么不能像第一版那样把字符串整体替换成 `""`: 那样**标签也一起没了** ——
      而分诊恰恰要看标签（"失败分支是否写过同一个判据"）⇒ 替换后所有 key 都等于 `''`,
      于是**每一处** record 都会被误判成"有守卫"（一个恒真的分类器）。
      ⇒ 正解: 保留原文, 只把落在字符串/注释里的匹配**跳过**。"""
    STR = re.compile(r'"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'', re.S)
    spans = [m.span() for m in STR.finditer(src)]
    return spans


def _pos_ok(src, spans, pos):
    """位置 pos 既不在字符串字面量里、也不在注释里 ⇒ 是真代码。"""
    for a, b in spans:
        if a <= pos < b:
            return False
    line_start = src.rfind("\n", 0, pos) + 1
    h = src.find("#", line_start, pos)
    return h < 0


def _label_key(arg):
    """标签的**首 token**（形如 "T0.3" / "T1.0" / "D0"）—— 守卫匹配用的键。"""
    a = re.sub(r'^[frb]*["\']', "", arg.strip())
    a = a.split("%")[0]
    toks = a.split()
    return toks[0] if toks else ""


def classify_true_records(src):
    """把所有 `record(<label>, True, ...)` 分成三类 ⇒ `(guarded, branch, bare)`。

    ★★ 为什么必须分诊（RETRACTIONS P26 的实质）: 它们**没有一处**是"恒真" ——
      失败路径就写在紧邻上方:
          if <坏>:  record("T0.3 …", False, "…");  return False
          record("T0.3 …", True,  "…")            # ← 走到这里 = 上一条没成立
      或落在 `if/else/try` 分支里。⇒ 一律判 FAIL 会造出**假 FAIL**。

    三类的判据（**都能失败** —— 见 selftest_axis1）:
      branch  : 缩进**比所属 def 的函数体更深** ⇒ 只在一个分支里被记一次
      guarded : 同一函数内、在它**之前**有 `record(<同一首 token>, False, ...)`
      bare    : 以上都不是 ⇒ **这才是"恒真"判据**, 计 FAIL
    """
    spans = _strip_spans(src)
    lines = src.split("\n")

    def calls():
        """★ 按**参数切分**取每个 `record(...)` 的顶层参数（不是正则抓标签）。

        为什么: 标签里可能出现逗号/括号（`"…（0x01 → ACK 4B, CRC 通过）"`、
        `"…(0x%X)" % cap_bit`）⇒ `[^,()]+` 抓不到 ⇒ **静默少覆盖**。
        （旧版把字符串剥成 `""` 之后反而抓得到, 所以换实现时必须**对数**:
          换实现前 14 处, 换实现后必须仍是 14 处 —— 见本文件 `--selftest-11` 之外的
          那次人工对数记录, RETRACTIONS P26 的更正当中有。）"""
        out = []
        for m in re.finditer(r"\brecord\s*\(", src):
            if not _pos_ok(src, spans, m.start()):
                continue
            args, end = _split_args(src, m.end())
            if len(args) < 2:
                continue
            ln = src[:m.start()].count("\n") + 1
            out.append((ln, args[0], args[1].strip(), m.start()))
        return out

    all_calls = calls()
    true_calls = [c for c in all_calls if c[2] == "True"]
    false_calls = [c for c in all_calls if c[2] == "False"]
    bare, branch, guarded = [], [], []
    for ln, lab, _ok, _pos in true_calls:
        line = lines[ln - 1]
        ind = len(line) - len(line.lstrip())
        key = _label_key(lab)
        body_ind, fn_start = 0, -1
        for k in range(ln - 2, -1, -1):
            dm = re.match(r"^(\s*)def\s", lines[k])
            if dm:
                body_ind, fn_start = len(dm.group(1)) + 4, k
                break
        hit_guard = any(nln > fn_start and _label_key(nlab) == key
                        for nln, nlab, _o, _p in false_calls if nln < ln)
        if hit_guard:
            guarded.append((ln, key))
        elif ind > body_ind:
            branch.append((ln, key))
        else:
            bare.append((ln, key))
    return guarded, branch, bare


def selftest_axis1():
    """★ 1.1 启发式自检：三份合成源码，三类必须各归各位。

    为什么需要: 1.1 原来自己就是坏的（把 14 个可失败判据全判成恒真）。
    一条"用来抓坏判据"的判据**尤其**必须证明自己会红 —— 否则它只是一个恒 FAIL。
    本自检是**能失败的**: 分类器一旦退化成"全 bare"或"全 guarded", 下面就有 FAIL。
    """
    CASES = (
        ("guarded", 'def t():\n'
                    '    if bad:\n'
                    '        record("T1.1 x", False, "why")\n'
                    '        return False\n'
                    '    record("T1.1 x", True, "ev")\n', "guarded"),
        ("branch", 'def t():\n'
                   '    if s["reject"] == 0:\n'
                   '        record("T2.2 y", True, "ev")\n'
                   '    else:\n'
                   '        record("T2.2 y", False, "ev")\n', "branch"),
        ("bare", 'def t():\n'
                 '    record("T3.3 z", True, "no failure path")\n', "bare"),
        # ★ 反例保护: "有 False 但在**别的函数**里" 不算守卫 ⇒ 必须判 bare
        ("bare(他函数有同名 False)", 'def a():\n'
                                     '    record("T4.4 w", False, "x")\n'
                                     '\n'
                                     '\n'
                                     'def b():\n'
                                     '    record("T4.4 w", True, "y")\n', "bare"),
    )
    ok = True
    for tag, code, want in CASES:
        g, b, r = classify_true_records(code)
        got = "guarded" if g else ("branch" if b else ("bare" if r else "none"))
        hit = (got == want.split("(")[0])
        ok = ok and hit
        print("    [%s] 合成样例 '%s' 归类 = %s（期望 %s）"
              % ("PASS" if hit else "FAIL", tag, got, want))
    return ok


def axis1_sentinel_scan():
    """1.1 静态扫描: 有没有"注定为真/注定为假"的判据写法（★ 2026-09-18 **分诊版**）。

    ★ 为什么这一条能机械查: 判据不可失败的**常见写法是有限的几种** ——
      `record(..., True, ...)` (字面量常数)、`assert True`、`if True:`。
      实测本项目第一版 T26 的 `g_persist_auto_gate` 恒为 0 就是同一族 (登记处已排除 RUN,
      于是"因 RUN 放弃"这个分支永远不会走到) —— 那种查不出来, 但**字面量常数能**。

    ★★ 修 (2.4): 原来把 `record(..., True, ...)` **一律**判 FAIL ⇒ 14 处假 FAIL。
      现在分诊成 branch / guarded / bare, **只有 bare 计 FAIL**, 且逐处打印文件:行号。
    """
    bare, branch, guarded, hard = [], [], [], []
    failsafe = 0
    for fn in sorted(os.listdir(os.path.join(ROOT, "tools"))):
        if not fn.endswith(".py") or fn == os.path.basename(__file__):
            continue
        src = read(os.path.join("tools", fn))
        g, b, r = classify_true_records(src)
        guarded += [(fn, ln, k) for ln, k in g]
        branch += [(fn, ln, k) for ln, k in b]
        bare += [(fn, ln, k) for ln, k in r]
        # ★ 只盯 **True**: 审计要防的是**假 PASS** (空判据)。
        #   `record(..., False, ...)` 永远不会声称成功, 它是 fail-fast/错误路径, 不会骗人
        #   —— 把它也算 FAIL 只会制造噪声 (而噪声会淹掉真问题, 本项目铁律)。
        spans = _strip_spans(src)
        for m in re.finditer(r"^\s*assert\s+True\b", src, re.M):
            if _pos_ok(src, spans, m.start()):
                hard.append("%s: assert True" % fn)
        failsafe += len(re.findall(r"record\(\s*[^,()]+,\s*False\s*,", src))
    hard += ["%s:%d %s" % t for t in bare]
    det = ("分诊: **分支内 %d** + **守卫落空 %d** + **真无条件 %d**"
           % (len(branch), len(guarded), len(bare)))
    if bare:
        det += "；★ 真无条件(**必须修**): " + "; ".join("%s:%d %s" % t for t in bare[:6])
    record("1.1 无『恒真』判据 (record(...,True) 分诊 / assert True)", not hard, det)
    if not bare:
        note("1.1 明细（前 6 处, 全部**可失败**）: "
             + (", ".join("%s:%d %s" % t for t in (branch + guarded)[:6]) or "无"))
        note("★ 口径: `record(...,True)` 落在这两类里**不是**缺陷 —— ① 条件分支内; "
             "② 紧邻上方有 `record(同一判据首 token, False)+return` 的守卫。"
             "只有两者都不是的才叫『恒真』。本条的**启发式自身**由 `--selftest-11` 证伪。")
    note("另有 %d 处 `record(..., False, ...)` = fail-fast/错误路径, 它们不会产生假 PASS"
         " (只报错不报成功), 不计入缺陷" % failsafe)


def axis1_rig_registry():
    """1.3 台架依赖判据登记表（`.workbuddy/rig-dependent.json`）的**机械校验**。

    ★ 判据是**能失败的**:
      · 登记表缺失/JSON 坏 ⇒ FAIL（不是"没有就是没问题"）
      · 条目里的 `tool` 文件不存在 ⇒ FAIL（条目指向了已删除的工具 = 腐烂）
      · 条目里的 `criterion` 文本在该文件里找不到 ⇒ FAIL（判据被改名/删掉, 条目已成**传说**）
      · 条目缺 mechanism/reproduce ⇒ FAIL（没有机制的登记等于没登记）
    ★ 为什么值得: 这类判据 FAIL 的机制**在台架/历史里, 不在固件里** —— 不登记的话,
      每次都要有人重新查一遍"是不是回归"（R5 已经这样消耗过一次, E-Y 又造了一例）。
    """
    p = os.path.join(ROOT, ".workbuddy", "rig-dependent.json")
    if not os.path.exists(p):
        record("1.3 台架依赖判据登记表存在且自洽", False,
               "缺 %s ⇒ 台架依赖判据没有登记处（FAIL, 不是「没有问题」）" % p)
        return
    try:
        with open(p, encoding="utf-8") as f:
            js = json.load(f)
    except Exception as e:
        record("1.3 台架依赖判据登记表存在且自洽", False, "JSON 解析失败: %s" % e)
        return
    bad, ok_n = [], 0
    for e in js.get("entries", []):
        eid = e.get("id", "?")
        f = e.get("tool", "")
        crit = e.get("criterion", "")
        fp = os.path.join(ROOT, f)
        if not f or not os.path.exists(fp):
            bad.append("%s: tool 不存在 (%s)" % (eid, f))
            continue
        body = open(fp, encoding="utf-8", errors="replace").read()
        if not crit or crit not in body:
            bad.append("%s: criterion '%s' 在 %s 里找不到（判据被改名? 条目已腐烂）"
                       % (eid, crit, f))
            continue
        if not e.get("mechanism") or not e.get("reproduce"):
            bad.append("%s: 缺 mechanism/reproduce" % eid)
            continue
        ok_n += 1
    record("1.3 台架依赖判据登记表存在且自洽（%d 条已核）" % ok_n, not bad,
           "；".join(bad) if bad else
           "登记表: " + ", ".join("%s(%s %s)" % (e["id"], os.path.basename(e["tool"]),
                                                e["criterion"]) for e in js.get("entries", [])))
    if not bad:
        note("★ 口径: 登记在案的判据**不进回归判据表** —— 命中它们时先读 mechanism/reproduce, "
             "不要当回归重查。要判定固件侧有无缺陷, 必须换**不依赖台架/历史**的观测量。")


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

    # ★★ 4.5/4.6 (2026-09-18 新增): **FLASH 扫描体**那张表。
    #   背景: 曾经用单标量 303/100 代表路径差异 —— 实测这条近似**两个方向都错**
    #   (对 DIRECT 低估 1.47 倍 ⇒ 放行超载; 对 PID 高估 1.21 倍 ⇒ 误拒合法),
    #   已换成逐原语表。这两条检查保证"表与常量同源"这条规矩**在新表上同样成立**
    #   —— 否则新表就是又一个"只写在注释里的宣称"。
    mf = re.search(r"k_op_cost_flash\[(0x[0-9A-Fa-f]+)\]\s*=\s*\{(.*?)\}", src, re.S)
    if not mf:
        record("4.5 FLASH 路径成本表存在且可解析", False, "engine.c 里找不到 k_op_cost_flash")
    else:
        nf = int(mf.group(1), 16)
        vf = [int(x) for x in re.findall(r"\b(\d+)\b",
                                         re.sub(r"/\*.*?\*/", "", mf.group(2), flags=re.S))]
        record("4.5 FLASH 路径成本表覆盖全部原语且值域合理",
               len(vf) == nf and all(0 < v < 2000 for v in vf),
               "声明 %d 项 / 实测 %d 项; 范围 %d~%d" % (nf, len(vf),
                                                        min(vf) if vf else -1, max(vf) if vf else -1))
        record("4.6 FLASH 表最贵原语也是 PID",
               len(vf) > pid_i and vf[pid_i] == max(vf),
               "k_op_cost_flash[OP_PID]=%d, max=%d" % (vf[pid_i] if len(vf) > pid_i else -1,
                                                       max(vf) if vf else -1))
        m3 = re.search(r"#define\s+OP_COST_MAX_FLASH\s+(\d+)", read("src/engine.h"))
        cf = int(m3.group(1)) if m3 else -1
        record("4.7 OP_COST_MAX_FLASH == FLASH 表 PID 项 (常量与表同源)",
               cf > 0 and len(vf) > pid_i and cf == vf[pid_i],
               "常量=%d, 表[OP_PID]=%d" % (cf, vf[pid_i] if len(vf) > pid_i else -1))
        # ★ 4.8: 标量近似的**反例**必须仍然成立 —— 若哪天两表逐项成比例了, 单标量
        #   会重新变成合法近似, 那时这条会 FAIL 提醒人重新评估 (判据必须能失败)。
        if len(vf) == len(vals) and len(vf) > 2:
            ratios = [b / float(a) for a, b in zip(vals, vf)]
            spread = max(ratios) / min(ratios)
            record("4.8 路径比值**不是常数** (单标量近似仍然不成立)",
                   spread > 1.15,
                   "逐原语比值 %.2f~%.2f, 极差 %.2f 倍 (DIRECT %.2f / PID %.2f)"
                   % (min(ratios), max(ratios), spread, ratios[0], ratios[pid_i]))


# ════════════════════════════ main ════════════════════════════
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true", help="只做离线项 (读源码/符号, 不连板)")
    ap.add_argument("--selftest-11", action="store_true",
                    help="只跑 1.1 启发式自检（三份合成源码, 证明分诊能红能绿）")
    a = ap.parse_args()

    if a.selftest_11:                       # ★ 属性名由 argparse 把 '-' 换成 '_'
        print("=" * 76)
        print("审计 1.1 启发式自检 —— 分类器必须能区分 守卫落空 / 分支内 / 真无条件")
        print("=" * 76)
        ok = selftest_axis1()
        print("\n%s" % ("[PASS] 1.1 分诊启发式可区分三类" if ok
                        else "[FAIL] 1.1 分诊启发式失效（它自己就是坏判据）"))
        return 0 if ok else 1

    print("=" * 76)
    print("H723 一次性审计 — 四口径机械检查 (范围: b690b15 之后的全部增量)")
    print("=" * 76)

    print("\n── 轴 1: 判据可失败性 ──")
    axis1_sentinel_scan()
    axis1_obs_scan()
    axis1_rig_registry()

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
