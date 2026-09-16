#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ref_claims_check.py —— **契据可机检化**（`docs/PLAN-consistency-v1.md` C 线，2026-09-16）

# 它解决什么问题

2026-09-16 的两轮独立审计共查出 **5 处"契据 vs 实现不一致"**，全部是**人能发现、机器发现不了**的：
`§3.8.5` 只写不做 · `§3.8.4` 两条规则**字面自相矛盾** · `§3.7` 未同步令牌 ·
`§3.8.3` 漏写"接受时也写 `reject=0`" · 工具侧 `expect_len` 只说不做。

★★ 而"**契据写了、代码没做**"比"完全没做"**更危险** ——
因为**下一个人会照契据去改，从而改坏**（本项目原话："契据漏写等于下一次有人照契据改就会改坏"）。
⇒ 本工具把这件事从人身上搬到机器上，并接进 `build.sh` 作为**第 5 道闸门**。

# 判据（五类，登记在 `docs/claims.md` 的 ```claims 代码块里）

| 类 | 机检 |
|---|---|
| **A 结构** | 宏在被指文件 `#define`；若名字以 `OFF_` 开头，**还须**在 `src/` 某处被 `_Static_assert` 引用 |
| **B 拒绝码** | ① 定义 ② 在 `src/*.c` 被**赋值**（不是只定义）③ 验收脚本里有判据读到它；缺 ③ 须显式 `--allow-uncovered <理由>` |
| **C 能力位** | ① 定义 ② 在 `DCL_CAP_H723_IMPL` 里 ③ **不在** `DCL_CAP_H723_NOTYET`（"宣称 = 实现"）|
| **D 文档引用** | **全局**扫描 `docs/**` 的 `文件:行号`：被引符号是否还在该文件；行号漂移 > 阈值则告警 |
| **E 判据存在性** | 契据"能失败的判据表"每一条，在被指脚本里真能 grep 到同名判据 |

# ★★ 空判据与覆盖度（本项目最恨的两种形态，这里都堵住）

1. **覆盖度自报**：每类都有下限 `MIN_*`。某类登记数为 0（或低于下限）⇒ **判无效**，不是"干净"。
   （与 `h723_ackbuf_check.py` / `dcl_static_check.py` 同规格；理由：那两个工具都曾因"扫到 0 个却
   打印 OK"而变成装饰。）
2. **`--allow-uncovered` 必须给理由**：B 类里"没有判据读到"的码**不许沉默地留着** ——
   要么补判据，要么在登记表里写明"为什么可以留着"。这条直接来自 `PLAN-consistency-v1` 的 DoD 第 4 条。

# 用法

    python tools/ref_claims_check.py [--root <仓库根>] [--verbose]
    python tools/ref_claims_check.py --selftest      # 造好的红必红 / 绿必绿（不碰真实仓库）

退出码：0 = 全通过（含显式允许的 uncovered）；2 = 有 FAIL；1 = 环境错误 / **覆盖不足 ⇒ 判据无效**。
"""
import argparse
import os
import re
import shutil
import sys
import tempfile

sys.stdout.reconfigure(errors="replace")   # ★ GBK 控制台上 print 一个 ⇒ 就崩（本项目踩过）

# ── 覆盖度下限（**低于它 ⇒ 判据无效**，不是"干净"）─────────────────────────
MIN_A = 8      # 结构类：SHM 域至少有这些
MIN_B = 4      # 拒绝码
MIN_C = 5      # 能力位
MIN_N = 2      # 留位能力位（`DCL_CAP_STATE_COLD` / `DCL_CAP_HMI`）
MIN_D = 20     # 文档里的 文件:行号 引用
MIN_E = 6      # 判据存在性
# ── D 类行号漂移阈值（超过则告警；符号消失才是 FAIL）──────────────────────
D_LINE_TOL = 40

CLAIMS_REL = os.path.join("docs", "claims.md")


# ══════════════════════════ 读取与解析 ══════════════════════════
def read_text(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def load_claims(root):
    """解析 docs/claims.md 里 ```claims 围栏内的行。→ (claims, err)"""
    p = os.path.join(root, CLAIMS_REL)
    txt = read_text(p)
    if txt is None:
        return None, "读不到 %s" % p
    out, inside = [], False
    for ln, line in enumerate(txt.splitlines(), 1):
        s = line.strip()
        if s.startswith("```claims"):
            inside = True
            continue
        if inside and s.startswith("```"):
            inside = False
            continue
        if not inside or not s or s.startswith("#"):
            continue
        # ★★ 字段分隔：**优先用 `|`**（多词字段——如"条款描述""判据名片段"——必须能表达）。
        #   空格分隔只作退化形式（只适合单词字段）。★ 本格式第一次跑就因它踩坑：
        #   `E 3.8.8-3 回退 req_seq 被拒   tools/...  回退` 被空格拆开 ⇒ 脚本名取成了"回退" ⇒ 10 条假红。
        if "|" in s:
            toks = [t.strip() for t in s.split("|")]
            toks = [t for t in toks if t]
        else:
            toks = s.split()
        kind = toks[0]
        # ★ 选项是**字段前缀**（`… | --allow-uncovered <理由>`）—— 不是整字段。
        #   第一次实现按"整字段相等"找 ⇒ 带理由的那种**根本没被认出来** ⇒ 该放行的照旧红。
        allow, reason = None, None
        for i, t in enumerate(toks):
            if t.startswith("--allow-uncovered"):
                allow = True
                reason = (t[len("--allow-uncovered"):].strip()
                          or " ".join(toks[i + 1:]).strip() or "(未给理由!)")
                toks = toks[:i] + toks[i + 1:]
                break
        out.append({"kind": kind, "args": toks[1:], "line": ln,
                    "allow": allow, "reason": reason, "raw": s})
    return out, None


def src_files(root, exts=(".c", ".h")):
    out = []
    sd = os.path.join(root, "src")
    for name in sorted(os.listdir(sd)) if os.path.isdir(sd) else []:
        if name.endswith(exts):
            out.append(os.path.join(sd, name))
    return out


def doc_files(root):
    out = []
    for dirpath, _dirs, files in os.walk(os.path.join(root, "docs")):
        for f in sorted(files):
            if f.endswith(".md"):
                out.append(os.path.join(dirpath, f))
    return out


# ══════════════════════════ 各类判据 ══════════════════════════
def chk_A(root, c):
    """A <宏> <头文件>"""
    if len(c["args"]) < 2:
        return "FAIL", "A 类参数不足（需要 <宏> <头文件>）"
    macro, rel = c["args"][0], c["args"][1]
    txt = read_text(os.path.join(root, rel))
    if txt is None:
        return "FAIL", "被指文件不存在: %s" % rel
    if not re.search(r"#\s*define\s+%s\b" % re.escape(macro), txt):
        return "FAIL", "契据声称的 %s 在 %s 里**没有定义**" % (macro, rel)
    if not macro.startswith("OFF_"):
        return "PASS", "已定义（非 OFF_* ⇒ 不要求 _Static_assert）"
    # OFF_* ⇒ 还须被 _Static_assert 引用（纪律：新 SHM 域 = 偏移宏 + _Static_assert）
    for f in src_files(root):
        t = read_text(f) or ""
        for m in re.finditer(r"_Static_assert\s*\(", t):
            window = t[m.start(): m.start() + 400]        # 断言可跨行 ⇒ 取窗口（已文档化此近似）
            if re.search(r"\b%s\b" % re.escape(macro), window):
                return "PASS", "已定义 + 被 _Static_assert 守住（%s）" % os.path.basename(f)
    return "FAIL", "%s 已定义但**没有任何 _Static_assert 引用它** —— 布局漂移不会被构建拦下" % macro


def chk_B(root, c):
    """B <拒绝码> <头文件> <验收脚本>"""
    if len(c["args"]) < 3:
        return "FAIL", "B 类参数不足（需要 <拒绝码> <头文件> <验收脚本>）"
    name, rel, script = c["args"][0], c["args"][1], c["args"][2]
    htxt = read_text(os.path.join(root, rel))
    if htxt is None:
        return "FAIL", "被指头文件不存在: %s" % rel
    if not re.search(r"#\s*define\s+%s\b" % re.escape(name), htxt):
        return "FAIL", "%s 未在 %s 定义" % (name, rel)
    # ② 被赋值（在 src/*.c 里出现，且不是仅仅 #define）
    assigned_in = None
    for f in src_files(root, (".c",)):
        t = read_text(f) or ""
        if re.search(r"\b%s\b" % re.escape(name), t):
            assigned_in = os.path.basename(f)
            break
    if assigned_in is None:
        if c["allow"]:
            return "PASS", "★ 声明了**未赋值**（该码永不出现）：%s" % c["reason"]
        return ("FAIL", "%s 只定义、**在 src/*.c 里从未被赋值** ⇒ 这个码永远不可能出现" % name)
    # ③ 验收脚本里有判据读到它
    st = read_text(os.path.join(root, script))
    if st is None:
        return "FAIL", "被指验收脚本不存在: %s" % script
    # ③ 判据读到它：三种证据任一即可 —— 按常量名 / 按数值(在含 reject 的行里) / 按**显式证据串**
    #    （第 4 个字段）。★ 为什么要"显式证据串"：验收脚本有自己的码名映射（`1(CRC)`），
    #      不写字面常量名 ⇒ 只按名/按值会**误报**（本项目最恨的第二形态：误报会被自己人关掉）。
    #      于是把"指认证据"这件事交回给写登记表的人。
    evid = c["args"][3] if len(c["args"]) > 3 else None
    hit_evid = bool(evid) and (evid in st)
    hit_name = name in st
    val = None
    m = re.search(r"#\s*define\s+%s\s+(0x[0-9A-Fa-f]+|\d+)u?" % re.escape(name), htxt)
    if m:
        val = str(int(m.group(1), 0))
    hit_val = False
    if val is not None:
        for line in st.splitlines():
            if "reject" in line.lower() and re.search(r"\b%s\b" % re.escape(val), line):
                hit_val = True
                break
    if hit_name or hit_val or hit_evid:
        how = "按名" if hit_name else ("按值 %s" % val if hit_val else "按证据串 %r" % evid)
        return "PASS", "定义(%s) + 赋值(%s) + 判据(%s)" % (rel, assigned_in, how)
    if c["allow"]:
        return "PASS", "★ uncovered 但**已显式声明**：%s" % c["reason"]
    return ("FAIL", "%s 有定义/赋值但**验收脚本里没有任何判据会读到它** ⇒ "
                    "要么补判据, 要么在 docs/claims.md 里 --allow-uncovered <理由>" % name)


def chk_C(root, c):
    """C <能力位> <头文件>"""
    if len(c["args"]) < 2:
        return "FAIL", "C 类参数不足（需要 <能力位> <头文件>）"
    name, rel = c["args"][0], c["args"][1]
    txt = read_text(os.path.join(root, rel))
    if txt is None:
        return "FAIL", "被指头文件不存在: %s" % rel
    if not re.search(r"#\s*define\s+%s\b" % re.escape(name), txt):
        return "FAIL", "%s 未定义" % name
    m = re.search(r"#\s*define\s+DCL_CAP_H723_IMPL\b(.*?)(?:\n\n|\Z)", txt, re.S)
    impl = m.group(1) if m else ""
    if not re.search(r"\b%s\b" % re.escape(name), impl):
        return "FAIL", "%s 定义了但**没并入 DCL_CAP_H723_IMPL** ⇒ 报了但不生效" % name
    m2 = re.search(r"#\s*define\s+DCL_CAP_H723_NOTYET\b(.*?)(?:\n\n|\Z)", txt, re.S)
    notyet = m2.group(1) if m2 else ""
    if re.search(r"\b%s\b" % re.escape(name), notyet):
        return "FAIL", "%s 同时在 NOTYET 清单里 ⇒ 自相矛盾" % name
    return "PASS", "定义 + 并入 IMPL + 不在 NOTYET"


def chk_N(root, c):
    """N <能力位> <头文件> —— **留位**：断言该位在 `DCL_CAP_H723_NOTYET` 里。
    ★ 为什么需要它: C2 完整性要求"定义了的位都必须被显式分类"。若只有 C 类,
      那 `DCL_CAP_STATE_COLD` / `DCL_CAP_HMI`（**故意留位**的两位）就只能被被迫声明成"已实现"
      —— 那是**让契据说谎**。⇒ 给出第二个分类: N = "留位, 未实现"。"""
    if len(c["args"]) < 2:
        return "FAIL", "N 类参数不足（需要 <能力位> <头文件>）"
    name, rel = c["args"][0], c["args"][1]
    txt = read_text(os.path.join(root, rel)) or ""
    if not re.search(r"#\s*define\s+%s\b" % re.escape(name), txt):
        return "FAIL", "%s 未定义" % name
    m2 = re.search(r"#\s*define\s+DCL_CAP_H723_NOTYET\b(.*?)(?:\n\n|\Z)", txt, re.S)
    notyet = m2.group(1) if m2 else ""
    if not re.search(r"\b%s\b" % re.escape(name), notyet):
        return "FAIL", ("%s 被登记为**留位**, 但它**不在** NOTYET 清单里 ⇒ 要么补进 NOTYET, "
                        "要么改成 C 类（已实现）" % name)
    m = re.search(r"#\s*define\s+DCL_CAP_H723_IMPL\b(.*?)(?:\n\n|\Z)", txt, re.S)
    impl = m.group(1) if m else ""
    if re.search(r"\b%s\b" % re.escape(name), impl):
        return "FAIL", "%s 同时在 IMPL 里 ⇒ 自相矛盾（既说留位又说实现了）" % name
    return "PASS", "留位（在 NOTYET 里且不在 IMPL 里）"


def chk_E(root, c):
    """E <契据条款> <脚本> <判据名片段>"""
    if len(c["args"]) < 3:
        return "FAIL", "E 类参数不足（需要 <条款> <脚本> <判据名片段>）"
    clause, script, frag = c["args"][0], c["args"][1], " ".join(c["args"][2:])
    st = read_text(os.path.join(root, script))
    if st is None:
        return "FAIL", "被指脚本不存在: %s" % script
    for line in st.splitlines():
        if "record(" in line and frag in line:
            return "PASS", "判据存在（%s）" % frag
    if c["allow"]:
        return "PASS", "★ 已声明未覆盖：%s" % c["reason"]
    return ("FAIL", "契据 %s 声称的判据「%s」在被指脚本里**找不到** ⇒ "
                    "要么补判据, 要么 --allow-uncovered" % (clause, frag))


REF_RE = re.compile(r"\b((?:src|tools|docs)/[\w./\-]+\.(?:c|h|py|md)):(\d+)\b")
TOKEN_RE = re.compile(r"`([A-Za-z_][A-Za-z0-9_]{2,})`")
# ★ 裸符号（没加反引号）也要查 —— 否则"文档里写了个已经删掉的宏名"这类漂移检不出来。
#   只认"像符号"的形态：全大写/下划线且（含 '_' 或长度 >= 6），避免把普通大写词当符号。
#   （本判据第一次跑就被自测抓出这个洞 —— 见 --selftest 用例 5。）
BARE_RE = re.compile(r"\b([A-Z][A-Z0-9_]{2,})\b")


def chk_D(root):
    """全局：文档里的 文件:行号 引用是否仍指向同一个符号。→ (rows, stats)"""
    rows, n_refs, n_sym_gone, n_drift = [], 0, 0, 0
    for df in doc_files(root):
        raw = read_text(df) or ""
        # ★ 项目给过时文档加过"过时/已修正"横幅 —— **尊重那个标记**：
        #   否则 D 类会把"历史文档引用了当年的文件名"报成漂移（那是噪声，不是漂移）。
        head40 = "\n".join(raw.splitlines()[:40])
        # ★ 跳过规则（三条，都是**尊重项目自己的标记**，不是我们自作主张）：
        #   ① 路径在 archive/ 下 ⇒ 历史文档；
        #   ② 头部横幅写着 过时/已作废/已修正/HISTORICAL/归档（项目用这些横幅标记陈旧）；
        #   ③ 该行引用**显式限定为 S3**（如 "S3 `tools/test_dcl.py:540`"）⇒ 那是另一个仓库的文件，
        #      在本仓当然"不存在" —— 报它是噪声，而噪声会让判据被关掉（本项目既有教训）。
        if "archive" in df.replace(os.sep, "/") or "archive" in head40:
            continue
        if any(k in head40 for k in ("过时", "已作废", "已修正", "HISTORICAL", "归档")):
            continue
        # ④ 该文档**自称是对照外部范本（`esp32-core0` / S3）**写的 ⇒ 它引用范本仓库里的文件是
        #    正当的，而本仓当然没有 ⇒ 这类"文件不存在"降为 WARN（**不是**缺陷）。
        #    判据：只看文档**开头 12 行**的自述（范围窄，避免把普通文档也放过去）。
        head12 = "\n".join(raw.splitlines()[:12])
        ext_frame = any(k in head12 for k in ("esp32-core0", "S3 范本", "范本", "ESP32-S3"))
        lines = raw.splitlines()
        for i, line in enumerate(lines, 1):
            if re.search(r"\bS3\b", line):        # ③ 显式 S3 限定的引用（另一个仓库）
                continue
            for m in REF_RE.finditer(line):
                rel, ln = m.group(1), int(m.group(2))
                n_refs += 1
                tgt = os.path.join(root, rel.replace("/", os.sep))
                ttxt = read_text(tgt)
                where = "%s:%d → %s:%d" % (os.path.basename(df), i, rel, ln)
                if ttxt is None:
                    if ext_frame:
                        # ★ 外部范本引用：文档开头就声明了"对照 esp32-core0 范本"⇒ 该文件本仓没有是正常的
                        rows.append(("WARN", where + "  外部范本引用（本文开篇声明对照 esp32-core0）"))
                    else:
                        rows.append(("FAIL", where + "  被引文件不存在"))
                        n_sym_gone += 1
                    continue
                tlines = ttxt.splitlines()
                toks = TOKEN_RE.findall(line)
                toks = [t for t in toks if not t.endswith(".md")]
                if not toks:                       # 没有反引号 ⇒ 退回"裸符号"启发式
                    toks = [t for t in BARE_RE.findall(line)
                            if ("_" in t) or len(t) >= 6]
                    toks = [t for t in toks if t not in rel.upper()]
                if not toks:
                    if ln < 1 or ln > len(tlines):
                        rows.append(("FAIL", where + "  行号超出文件长度(%d)" % len(tlines)))
                        n_sym_gone += 1
                    continue
                found = None
                for idx, tl in enumerate(tlines, 1):
                    if all(re.search(r"\b%s\b" % re.escape(t), tl) for t in toks):
                        found = idx
                        break
                if found is None:
                    for t in toks:
                        if any(re.search(r"\b%s\b" % re.escape(t), tl) for tl in tlines):
                            found = next(idx for idx, tl in enumerate(tlines, 1)
                                         if re.search(r"\b%s\b" % re.escape(t), tl))
                            break
                if found is None:
                    rows.append(("FAIL", where + "  被引符号(%s)在该文件里**已不存在**" % ",".join(toks)))
                    n_sym_gone += 1
                elif abs(found - ln) > D_LINE_TOL:
                    rows.append(("WARN", where + "  行号漂移：符号现在在 L%d" % found))
                    n_drift += 1
    return rows, {"refs": n_refs, "gone": n_sym_gone, "drift": n_drift}


# ══════════════════════════ 主流程 ══════════════════════════
def run(root, verbose=False):
    claims, err = load_claims(root)
    if err:
        print("✗ %s" % err)
        return 1
    by_kind = {}
    for c in claims:
        by_kind.setdefault(c["kind"], []).append(c)

    print("契据可机检闸门 (docs/claims.md → 代码/脚本)  root=%s" % root)
    fails, warns = [], []
    per = {}
    for kind, fn, minn, title in (
            ("A", chk_A, MIN_A, "结构（契据声称的符号必须存在；OFF_* 须被 _Static_assert 守住）"),
            ("B", chk_B, MIN_B, "拒绝码（定义 + 被赋值 + 有判据读到 / 或显式声明未覆盖）"),
            ("C", chk_C, MIN_C, "能力位（定义 + 并入 IMPL + 不在 NOTYET）"),
            ("N", chk_N, MIN_N, "留位能力位（定义 + 在 NOTYET 里 + 不在 IMPL 里）"),
            ("E", chk_E, MIN_E, "判据存在性（契据的判据表 ⇄ 脚本里的 record()）")):
        items = by_kind.get(kind, [])
        npass = nfail = 0
        print("\n── %s 类：%s ──  登记 %d 条（下限 %d）" % (kind, title, len(items), minn))
        for c in items:
            st, why = fn(root, c)
            if st == "PASS":
                npass += 1
                if verbose:
                    print("  [PASS] L%-4d %-28s %s" % (c["line"], c["args"][0][:28], why))
            else:
                nfail += 1
                fails.append("  [%s类] L%d %s\n        → %s" % (kind, c["line"], c["raw"][:90], why))
                print("  [FAIL] L%-4d %-28s %s" % (c["line"], c["args"][0][:28], why))
        per[kind] = (len(items), npass, nfail, minn)

    # ★★★ C2 类：**完整性** —— "定义了却没登记"的洞（2026-09-16 我自己踩的）
    #   起因：本次新增了 `DCL_CAP_DEVBIND_PERSIST`，而闸门**没红** —— 因为它只查"登记了的"，
    #   不查"代码里新增了却没登记"。那正是本项目最恨的"闸门有洞"（它会被当成'干净'）。
    #   ⇒ 对**能力位**这类封闭小集合做反向核对：`transport.h` 里每个 `DCL_CAP_<名>`
    #     都必须在 claims 里有一条 C 登记。★ 为什么只对能力位做：集合小、语义清晰、
    #     且"宣称=实现"本来就是硬要求；`OFF_*` 数量大且部分是本文件内部的宏，反向核对会噪声化。
    print("\n── C2 类：能力位完整性（transport.h 里定义了的**都必须登记**）──")
    thr_rel = "src/transport.h"
    thr = read_text(os.path.join(root, thr_rel)) or ""
    defined_caps, registered = [], set()
    for m in re.finditer(r"#\s*define\s+(DCL_CAP_[A-Z0-9_]+)\b", thr):
        nm = m.group(1)
        if nm in ("DCL_CAP_H723_IMPL", "DCL_CAP_H723_NOTYET"):
            continue
        defined_caps.append(nm)
    # ★ "已分类"= C（已实现）∪ N（留位）—— **每个定义了的位都必须被显式分类**,
    #   既不登记又不分类 = 闸门看不见它 = 有洞。
    for c in by_kind.get("C", []) + by_kind.get("N", []):
        if c["args"]:
            registered.add(c["args"][0])
    missing = [n for n in defined_caps if n not in registered]
    print("   代码里定义 %d 个能力位, claims 登记 %d 个" % (len(defined_caps), len(registered)))
    for n in missing:
        msg = ("  [C2类] %s **在 %s 里定义了, 但 docs/claims.md 没有登记它** ⇒ "
               "闸门看不见它（= 闸门有洞）⇒ 请补一条 `C | %s | %s`" % (n, thr_rel, n, thr_rel))
        fails.append(msg)
        print(msg)
    per["C2"] = (len(defined_caps), len(defined_caps) - len(missing), len(missing), 1)

    print("\n── D 类：文档 file:line 引用（被引符号是否还在该文件 + 行号漂移）──")
    rows, dstat = chk_D(root)
    shown = 0
    for st, why in rows:
        if st == "FAIL":
            fails.append("  [D类] " + why)
            print("  [FAIL] " + why)
        else:
            warns.append("  [D类] " + why)
            if shown < 8:
                print("  [warn] " + why)
                shown += 1
    if len(rows) > shown:
        print("  …（另有 %d 条同类告警已折叠）" % (len(rows) - shown))
    per["D"] = (dstat["refs"], dstat["refs"] - dstat["gone"], dstat["gone"], MIN_D)

    print("\n--- 覆盖度（★ 覆盖不足 ⇒ 判据**无效**，不是'干净'）---")
    invalid = False
    for k in ("A", "B", "C", "N", "C2", "D", "E"):
        n, np_, nf, mn = per.get(k, (0, 0, 0, 0))
        ok = n >= mn
        invalid = invalid or not ok
        print("   %s 类: 登记 %3d（下限 %3d）%s   PASS %d / FAIL %d" %
              (k, n, mn, "OK" if ok else "★ 覆盖不足", np_, nf))
    print("   D 类明细: 引用 %d 处, 符号消失 %d, 行号漂移 %d（阈值 %d 行）" %
          (dstat["refs"], dstat["gone"], dstat["drift"], D_LINE_TOL))
    print("   ★ 自证: 若上面每类都是 0 ⇒ 本闸门是空的（这正是它要防的第一种形态）")

    if fails:
        print("\n★★ 契据可机检闸门失败 ⇒ 拒绝通过（%d 条）:" % len(fails))
        for f in fails:
            print(f)
        print("   修法二选一: ① 让代码/脚本跟上契据; ② 在 docs/claims.md 里 "
              "--allow-uncovered <理由> 显式声明（**不允许沉默地留着**）。")
        return 2
    if invalid:
        print("\n★★ 覆盖不足 ⇒ 本次'通过'**无效**（判据是空的，不是干净）。")
        return 1
    print("\n✓ 契据与实现一致，且覆盖度达标（这个 OK 是有意义的）")
    return 0


# ══════════════════════════ 自测（红必红 / 绿必绿）══════════════════════════
# ★ 规格同既有两个闸门: **造好的红必红、绿必绿**; 且"覆盖不足"必须与"有 FAIL"区分开。
#   本自测第一次跑就抓到了夹具自身的不一致（判据表 6 条 vs 脚本 3 条）—— 见下方 FIX_CLEAN 注释。
N_A, N_B, N_C, N_E = 8, 4, 5, 6


def _clean_claims():
    L = []
    for i in range(N_A):
        L.append("A | OFF_GOOD%s | src/engine.h" % chr(ord('A') + i))
    for i in range(N_B):
        L.append("B | RC_OK%d | src/x.h | tools/t.py" % (i + 1))
    for i in range(N_C):
        L.append("C | DCL_CAP_A%d | src/transport.h" % (i + 1))
    for i in range(2):
        L.append("N | DCL_CAP_N%d | src/transport.h" % (i + 1))
    for i in range(N_E):
        L.append("E | 1.%d | tools/t.py | 判据%s" % (i + 1, "甲乙丙丁戊己"[i]))
    return "# t\n```claims\n" + "\n".join(L) + "\n```\n"


def _mk_fixture(root, claims=None, corrupt=None):
    def w(rel, txt):
        p = os.path.join(root, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(txt)

    # ── src/engine.h: 8 个 OFF_* + 每个都被一条 _Static_assert 守住 ──
    hdr = "".join("#define OFF_GOOD%s 0x%02X\n" % (chr(ord('A') + i), i) for i in range(N_A))
    hdr += "".join('_Static_assert(OFF_GOOD%s == %d, "x");\n' % (chr(ord('A') + i), i)
                   for i in range(N_A))
    if corrupt == "no_assert":      # 抹掉最后一个断言 ⇒ A 类必须红
        hdr = hdr.replace('_Static_assert(OFF_GOODH == 7, "x");\n', "")
    w("src/engine.h", hdr)

    # ── src/x.h + x.c: 4 个拒绝码, 全部被赋值 ──
    w("src/x.h", "".join("#define RC_OK%d %du\n" % (i + 1, i + 1) for i in range(N_B)))
    w("src/x.c", "".join("rc = RC_OK%d;\n" % (i + 1) for i in range(N_B)))

    # ── src/transport.h: 能力位全部并入 IMPL、且不在 NOTYET ──
    thr = "".join("#define DCL_CAP_A%d 0x%04Xu\n" % (i + 1, (i + 1) * 0x10) for i in range(N_C))
    # ★ 夹具也要有**留位**位：C2 完整性要求"定义了的位都被显式分类",
    #   而没有留位位时 N 类会覆盖不足 ⇒ 干净夹具自己就返回 1（这也是自测抓到的）。
    thr += "#define DCL_CAP_N1 0x9000u\n#define DCL_CAP_N2 0x9001u\n"
    thr += "#define DCL_CAP_H723_IMPL   (" + " | ".join(
        "DCL_CAP_A%d" % (i + 1) for i in range(N_C)) + ")\n\n"
    # ★ 故意写成**跨两行**（与真实 transport.h 同形）—— 单行夹具会让"截断在第一个换行"的
    #   正则 bug 检不出来（本判据第一次就是因为这个 bug 漏判了 HMI）。
    thr += "#define DCL_CAP_H723_NOTYET (DCL_CAP_N1 | \\\n                             DCL_CAP_N2)\n"
    if corrupt == "cap_notyet":     # 把 A5 同时列进 NOTYET ⇒ C 类必须红
        thr = thr.replace("(DCL_CAP_N1 | \\\n                             DCL_CAP_N2)",
                          "(DCL_CAP_N1 | \\\n                             DCL_CAP_N2 | DCL_CAP_A5)")
    if corrupt == "cap_notimpl":    # 从 IMPL 里剔除 A5 ⇒ C 类必须红
        thr = thr.replace(" | DCL_CAP_A5", "")
    w("src/transport.h", thr)

    # ── tools/t.py: 6 条判据, 且 4 个拒绝码全都被读到 ──
    lines = ['record("判据%s", 1, "")' % ch for ch in "甲乙丙丁戊己"]
    lines += ['if s["reject"] == %d: pass' % (i + 1) for i in range(N_B)]
    lines += ["use = %s" % " | ".join("RC_OK%d" % (i + 1) for i in range(N_B))]
    w("tools/t.py", "\n".join(lines) + "\n")

    # ── docs/t.md: ≥20 处 file:line 引用, 全部有效（符号仍在）──
    # ★ 引用数必须 **>= MIN_D(20)**，否则干净夹具自己就会因"覆盖不足"返回 1 ——
    #   这一点也是自测第一次跑就抓到的（当时只写了 13 处）。
    doc = ("见 src/x.c:1 的 RC_OK1\n"
           + "".join("见 src/engine.h:%d 行 OFF_GOOD%s\n" % (i + 1, chr(ord('A') + i))
                     for i in range(N_A))
           + "".join("见 src/x.c:1 行 RC_OK%d\n" % (i + 1) for i in range(N_B))
           + "".join("补充引用 src/engine.h:1 行 OFF_GOODA（第 %d 处）\n" % i
                     for i in range(MIN_D)))
    if corrupt == "docsym_gone":    # 引用一个不存在的符号 ⇒ D 类必须红
        doc += "见 src/x.c:3 的 NO_SUCH_SYMBOL_HERE\n"
    w("docs/t.md", doc)

    w(CLAIMS_REL, claims if claims is not None else _clean_claims())
    return root


def selftest():
    print("── ref_claims_check --selftest（造好的红必红 / 绿必绿）──")
    ok = True
    tmp = tempfile.mkdtemp(prefix="refclaims_")
    cases = [
        ("干净夹具",                None,                          None,            0),
        ("A: OFF_* 缺 _Static_assert", None,                       "no_assert",     2),
        ("C: 能力位同时在 NOTYET",   None,                          "cap_notyet",    2),
        ("C: 能力位未并入 IMPL",     None,                          "cap_notimpl",   2),
        ("D: 文档引用已消失的符号",  None,                          "docsym_gone",   2),
        # E: 契据的判据表 ⊃ 脚本实际有的判据（把最后一条判据名改成不存在的）
        ("E: 契据判据在脚本里不存在", _clean_claims().replace("判据己", "判据不存在XYZ"), None, 2),
        # 覆盖不足: 只留 2 条 A ⇒ 必须判"无效"(rc=1), 而不是"有 FAIL"(2) 或"通过"(0)
        # C2: 代码里新增了能力位但 claims 没登记 ⇒ 必须红（本判据存在的原因: 我自己踩过）
        ("C2: 能力位定义了却没登记", _clean_claims().replace(
            "C | DCL_CAP_A1 | src/transport.h\n", ""), None, 2),
        ("覆盖不足（A 类只剩 2 条）",
         "\n".join([l for l in _clean_claims().splitlines() if not l.startswith("A | OFF_GOOD")
                    or l.startswith("A | OFF_GOODA") or l.startswith("A | OFF_GOODB")]) + "\n",
         None, 1),
    ]
    try:
        for i, (name, claims, corrupt, expect) in enumerate(cases, 1):
            root = _mk_fixture(os.path.join(tmp, "c%d" % i), claims=claims, corrupt=corrupt)
            print("\n════ 用例 %d: %s（期望 rc=%d）════" % (i, name, expect))
            rc = run(root, verbose=bool(os.environ.get("RC_VERBOSE")))
            tag = "OK" if rc == expect else "★不符★"
            print("  ⇒ rc=%d 期望=%d  [%s]" % (rc, expect, tag))
            if rc != expect:
                ok = False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("\n%s" % ("✓ selftest 通过：四类判据都能红、且'覆盖不足'与'有 FAIL'能区分"
                    if ok else "✗ selftest 失败（见上面 ★不符★）"))
    return 0 if ok else 2


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    return run(a.root, a.verbose)


if __name__ == "__main__":
    sys.exit(main())
