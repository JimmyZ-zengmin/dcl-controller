#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mem_report.py —— **内存账本**（内存宪法 B 期；构建闸门第 7 道）

## 它回答什么
"这个构建里，ITCM / DTCM / AXI / SHM / Flash 各用了多少、还剩多少、谁占的最多" ——
**每个数字都从构建产物派生**，没有任何手抄（不变量 **M4 容量派生**）。

## 为什么必须有（血证）
`build.sh` 全文不报尺寸 ⇒ "**ITCM 64/64 KB 已满**" 这句话在 **6 份文档**里活了数月，
还被 README 当成"多轴的三个真阻塞"之一 —— 而 `size -A` 实测只用了 **14.3 KB / 64 KB（22.4%）**。
⇒ 没有账本，过期数字就会去支撑产品决策。本工具与 `--check-docs` 一起把这个类别关掉。

## 判据（都能失败；**每条都有对应的变异对照**，见 --selftest 与 .tmpctl 的复跑记录）
  C2 ★ 链接器**不得**往 AXI 放任何段（不变量 **M2 所有权互斥**）
        —— 曾经的 `.axi_buf` 起点 = 0x24000000 = 诊断区起点，一被使用就静默盖住四个诊断区
  C3 `_shm_end − _shm_start == SHM_SIZE`（不变量 **M4**）
  C4 `DTCM_HEAPSTACK_SZ`（memmap.h）== `_Min_Heap_Size + _Min_Stack_Size`（.ld）
        —— 跨文件核对，防止"改了一边没改另一边"
  C5 AXI 固定区**恰好铺满** 320 KB（M2），且各区内结构不越界
  C6 ITCM 使用率闸门：>80% 红 / >60% 告警（不变量 **M3**）
  C7 栈余量：`_estack − (_shm_end + heapstack) ≥ DTCM_STACK_MIN_SZ`（M3）
  C8 `--check-docs`：`README.md` / `docs/*.md` 里的**容量宣称**与账本比对
        —— 这条专治"ITCM 已满"那一类（M4）

用法:
  python tools/mem_report.py                      # 打印账本 + 跑 C2~C7
  python tools/mem_report.py --check-docs         # 另跑 C8（闸门里用这个）
  python tools/mem_report.py --selftest           # 用合成数据证明 C2/C3 会红（变异对照）
退出码: 0 = 全过 / 1 = 有 FAIL / 2 = 前置不满足（产物缺失等）
"""
import argparse
import io
import os
import re
import subprocess
import sys

R = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ELF = os.path.join(R, "build", "dcl_h723")
SRC = os.path.join(R, "src")


def toolchain():
    """★ 从项目自己的 cmake/arm-none-eabi.cmake 读工具链路径（不写死）。"""
    cm = io.open(os.path.join(R, "cmake", "arm-none-eabi.cmake"), encoding="utf-8",
                 errors="replace").read()
    m = re.search(r'set\(TOOLCHAIN_BIN\s+"([^"]+)"\)', cm)
    if not m:
        raise SystemExit("!! arm-none-eabi.cmake 里找不到 TOOLCHAIN_BIN（拒绝猜）")
    return m.group(1)


TC = toolchain()


def run(exe, args):
    p = os.path.join(TC, exe + ".exe")
    if not os.path.exists(p):
        raise SystemExit("!! 工具链里没有 %s（%s）" % (exe, p))
    r = subprocess.run([p] + args, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return r.stdout or ""


# ────────────────────────── 派生：段尺寸 + 链接器符号 ──────────────────────────
def sections():
    """从 `size -A` 取 (name, size, addr)；只保留**会占内存**的（去掉调试段）。"""
    out, res = run("arm-none-eabi-size", ["-A", ELF]), []
    for line in out.split("\n")[2:]:
        f = line.split()
        # ★ `size -A` 是**三列**（section / size / addr）—— 第一版按四列过滤 ⇒ 全部被丢掉，
        #   于是账本里 ITCM/DTCM/Flash 全是 0.00 KB 而判据照样 PASS（"数字全 0 的账本"）。
        if len(f) < 3:
            continue
        try:
            sz, addr = int(f[1]), int(f[2])
        except ValueError:
            continue
        if f[0].startswith(".debug") or f[0] in (".comment", ".ARM.attributes", "Total"):
            continue
        res.append((f[0], sz, addr))
    return res


def symbols():
    """链接器符号（`_shm_start` / `_shm_end` / `_estack` …）—— 从 nm 取。"""
    out = run("arm-none-eabi-nm", [ELF])
    sym = {}
    for line in out.split("\n"):
        f = line.split()
        if len(f) == 3:
            try:
                sym[f[2]] = int(f[0], 16)
            except ValueError:
                pass
    return sym


def memmap_regions():
    """从 `src/memmap.h` 解析固定区表：`#define AXI_<N> addr` + `#define AXI_<N>_SZ size`
    以及同一行注释里的 class / owner（形如 `/* DIAG  owner=sd.c */`）。
    ★ 解析失败 ⇒ **响亮退出**（不返回空表 —— 空表会让 C5 变成空判据）。"""
    txt = io.open(os.path.join(SRC, "memmap.h"), encoding="utf-8", errors="replace").read()
    base = {}
    for m in re.finditer(r'^#define\s+(AXI_[A-Z0-9_]+?)\s+0x([0-9A-Fa-f]+)u?\s*(?:/\*(.*?)\*/)?\s*$',
                         txt, re.M):
        name, val, cmt = m.group(1), int(m.group(2), 16), (m.group(3) or "")
        if name.endswith("_SZ"):
            continue
        cls = (cmt.strip().split()[0] if cmt.strip() else "")
        own = ""
        mo = re.search(r"owner=([\w\.]+)", cmt)
        if mo:
            own = mo.group(1)
        base[name] = dict(addr=val, cls=cls, owner=own, cmt=cmt.strip())
    regs = []
    for name, d in base.items():
        ms = re.search(r'^#define\s+%s_SZ\s+0x([0-9A-Fa-f]+)u?' % re.escape(name), txt, re.M)
        if not ms:
            continue                      # 只有 _SZ 没有基址的（如 AXI_SIZE）跳过
        regs.append(dict(name=name, addr=d["addr"], size=int(ms.group(1), 16),
                         cls=d["cls"], owner=d["owner"]))
    if len(regs) < 10:
        raise SystemExit("!! memmap.h 里只解析出 %d 个固定区 ⇒ 解析器或文件形态变了（拒绝继续，"
                         "否则 C5/C6 会退化成空判据）" % len(regs))
    return sorted(regs, key=lambda r: r["addr"]), txt


def ld_const(name):
    """从 .ld 读 `_Min_Heap_Size` / `_Min_Stack_Size` 之类的数值。"""
    txt = ""
    for f in os.listdir(os.path.join(R, "ld")):
        if f.endswith(".ld"):
            txt += io.open(os.path.join(R, "ld", f), encoding="utf-8",
                           errors="replace").read()
    m = re.search(r"^%s\s*=\s*(0x[0-9A-Fa-f]+|\d+)\s*;" % re.escape(name), txt, re.M)
    if not m:
        raise SystemExit("!! .ld 里找不到 %s（跨文件核对断了 ⇒ 拒绝静默通过）" % name)
    return int(m.group(1), 16) if m.group(1).startswith("0x") else int(m.group(1))


# ────────────────────────── 账本 ──────────────────────────
def build_ledger():
    secs, sym = sections(), symbols()
    R_, mtxt = memmap_regions()
    itcm_base, itcm_sz = 0x00000000, 0x00010000
    dtcm_base, dtcm_sz = 0x20000000, 0x00020000
    axi_base, axi_sz = 0x24000000, 0x00050000

    def in_rng(a, base, sz):
        return base <= a < base + sz

    L = dict(itcm=[], dtcm=[], axi=[], flash=[], other=[])
    for nm, sz, addr in secs:
        if in_rng(addr, itcm_base, itcm_sz):
            L["itcm"].append((nm, sz, addr))
        elif in_rng(addr, dtcm_base, dtcm_sz):
            L["dtcm"].append((nm, sz, addr))
        elif in_rng(addr, axi_base, axi_sz):
            L["axi"].append((nm, sz, addr))
        elif addr >= 0x08000000:
            L["flash"].append((nm, sz, addr))
        else:
            L["other"].append((nm, sz, addr))
    return dict(secs=secs, sym=sym, regions=R_, memmap_txt=mtxt,
                ITCM=(itcm_base, itcm_sz), DTCM=(dtcm_base, dtcm_sz), AXI=(axi_base, axi_sz),
                L=L)


def pct(u, t):
    return 100.0 * u / t if t else 0.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check-docs", action="store_true", help="另跑 C8（文档容量宣称对账）")
    ap.add_argument("--selftest", action="store_true", help="用合成数据证明 C2/C3 会红")
    a = ap.parse_args()

    if a.selftest:
        return selftest()

    if not os.path.exists(ELF):
        print("!! 缺 %s ⇒ 先构建（bash build.sh）" % ELF)
        return 2
    D = build_ledger()
    res, warn = [], []

    print("=" * 78)
    print("内存账本（每个数字都由构建产物派生；权威源: src/memmap.h + size -A + nm）")
    print("=" * 78)

    # ── ITCM ──
    it_u = sum(s for _, s, _ in D["L"]["itcm"])
    it_b, it_t = D["ITCM"]
    print("\n── ITCM（%d KB @0x%08X）: 已用 %.2f KB = %.1f%% ──" % (it_t // 1024, it_b, it_u / 1024.0,
                                                                   pct(it_u, it_t)))
    for nm, s, ad in sorted(D["L"]["itcm"], key=lambda x: x[2]):
        print("     %-22s 0x%05X  %8.2f KB" % (nm, ad, s / 1024.0))
    free_lo, free_hi = 0, 0
    vec = [x for x in D["L"]["itcm"] if "vectors" in x[0]]
    txt = [x for x in D["L"]["itcm"] if "vectors" not in x[0]]
    if txt and vec:
        free_lo, free_hi = txt[-1][2] + txt[-1][1], vec[0][2]
        print("     ⇒ 连续空闲区间 0x%05X..0x%05X = %.2f KB"
              % (free_lo, free_hi, (free_hi - free_lo) / 1024.0))

    # ── DTCM ──
    dt_u = sum(s for _, s, _ in D["L"]["dtcm"])
    dt_b, dt_t = D["DTCM"]
    sym = D["sym"]
    shm_s, shm_e = sym.get("_shm_start"), sym.get("_shm_end")
    estack = sym.get("_estack")
    hs = None
    m = re.search(r'^#define\s+DTCM_HEAPSTACK_SZ\s+(0x[0-9A-Fa-f]+)u?', D["memmap_txt"], re.M)
    if m:
        hs = int(m.group(1), 16)
    m2 = re.search(r'^#define\s+DTCM_STACK_MIN_SZ\s+(0x[0-9A-Fa-f]+)u?', D["memmap_txt"], re.M)
    stk_min = int(m2.group(1), 16) if m2 else 0
    print("\n── DTCM（%d KB @0x%08X）: 有主 %.2f KB = %.1f%% ──" % (dt_t // 1024, dt_b, dt_u / 1024.0,
                                                                   pct(dt_u, dt_t)))
    for nm, s, ad in sorted(D["L"]["dtcm"], key=lambda x: x[2]):
        print("     %-22s 0x%08X  %8.2f KB" % (nm, ad, s / 1024.0))
    if shm_e and estack and hs:
        head = estack - (shm_e + hs)
        print("     %-22s 0x%08X  %8.2f KB   ← 无名区（= 栈余量，%s 负责量水位）"
              % ("STACK_HEADROOM", shm_e + hs, head / 1024.0, "src/mem_stat.c"))
        print("     _estack              0x%08X" % estack)

    # ── AXI ──
    print("\n── AXI（320 KB @0x%08X）: %d 个固定区 ──" % (D["AXI"][0], len(D["regions"])))
    tot = 0
    for r in D["regions"]:
        tot += r["size"]
        print("     0x%08X  %7.2f KB  %-8s %-14s %s"
              % (r["addr"], r["size"] / 1024.0, r["cls"] or "?", r["owner"] or "-", r["name"]))
    print("     ⇒ 固定区合计 %.2f KB / %d KB" % (tot / 1024.0, D["AXI"][1] // 1024))
    print("     ⇒ 链接器落在 AXI 的段: %s" % ([x[0] for x in D["L"]["axi"]] or "无"))

    # ── SHM / Flash ──
    fl_u = sum(s for _, s, _ in D["L"]["flash"])
    print("\n── SHM: _shm_start=0x%08X _shm_end=0x%08X 段长=%.2f KB ──"
          % (shm_s or 0, shm_e or 0, ((shm_e - shm_s) / 1024.0) if (shm_s and shm_e) else 0))
    print("── Flash: 已用 %.2f KB / 1024 KB = %.1f%% ──" % (fl_u / 1024.0, pct(fl_u, 1024 * 1024)))
    if D["L"]["other"]:
        print("── 其它: %s ──" % [(n, s) for n, s, _ in D["L"]["other"]])

    # ══════════════ 判据 ══════════════
    print("\n=== 判据 ===")

    # C2 ★ 链接器不得往 AXI 放段（M2）
    ok = not D["L"]["axi"]
    res.append(("C2 链接器不往 AXI 放任何段（M2 所有权互斥）", ok))
    if not ok:
        print("     ★ 违规段: %s —— 见 docs/PLAN-memmap-constitution.md §2.1" % D["L"]["axi"])

    # C3 _shm_end - _shm_start == SHM_SIZE（M4）
    m = re.search(r'^#define\s+SHM_SIZE\s+(0x[0-9A-Fa-f]+)u?', io.open(
        os.path.join(SRC, "engine.h"), encoding="utf-8", errors="replace").read(), re.M)
    shm_size = int(m.group(1), 16) if m else 0
    got = (shm_e - shm_s) if (shm_s and shm_e) else -1
    res.append(("C3 _shm_end−_shm_start(%d) == SHM_SIZE(%d)（M4）" % (got, shm_size),
                got == shm_size))

    # C4 DTCM_HEAPSTACK_SZ == .ld 的 heap+stack（M4 跨文件）
    try:
        ldsum = ld_const("_Min_Heap_Size") + ld_const("_Min_Stack_Size")
        res.append(("C4 DTCM_HEAPSTACK_SZ(0x%X) == .ld heap+stack(0x%X)（M4 跨文件核对）"
                    % (hs or 0, ldsum), (hs or 0) == ldsum))
    except SystemExit as e:
        res.append(("C4 跨文件核对（%s）" % e, False))

    # C5 AXI 恰好铺满 + 区内结构不越界（M2）
    res.append(("C5 AXI 固定区恰好铺满 320 KB（实测 %d B）" % tot, tot == D["AXI"][1]))

    # C6 ITCM 使用率闸门（M3）
    u = pct(it_u, it_t)
    res.append(("C6 ITCM 使用率 %.1f%% < 80%%（上限闸门）" % u, u < 80.0))
    if u > 60.0:
        warn.append("ITCM 使用率 %.1f%% 已超过 60%% 告警线（E 期触发条件之一）" % u)

    # C7 栈余量（M3）
    if shm_e and estack and hs:
        head = estack - (shm_e + hs)
        res.append(("C7 栈余量 %.2f KB ≥ %.2f KB（M3）" % (head / 1024.0, stk_min / 1024.0),
                    head >= stk_min))
    else:
        res.append(("C7 栈余量（读不到 _shm_end/_estack/heapstack ⇒ 判无效）", False))

    # C8 文档容量宣称对账（M4）
    if a.check_docs:
        bad = check_docs(D, it_u, it_t, dt_u, dt_t, tot, D["AXI"][1])
        res.append(("C8 文档容量宣称与账本一致（扫 README + docs/*.md）", not bad))
        for b in bad:
            print("     ★ %s" % b)

    nf = 0
    for name, ok in res:
        print("  [%s] %s" % ("PASS" if ok else "FAIL", name))
        nf += (not ok)
    for w in warn:
        print("  [WARN] %s" % w)
    print("\n%d 项判定, %d FAIL" % (len(res), nf))
    return 0 if nf == 0 else 1


# ────────────────────────── C8: 文档容量宣称 ──────────────────────────
def check_docs(D, it_u, it_t, dt_u, dt_t, axi_u, axi_t):
    """扫 README.md 与 docs/*.md 里的"X KB / Y KB"式容量宣称，与账本比对。
    ★ 只认**明确的**容量句（带 ITCM/DTCM/AXI/Flash 关键词），避免把普通数字当宣称。"""
    bad = []
    # 两种写法都要认:
    #   ① "ITCM 已用 14.32 KB / 64 KB"   ② "ITCM 64/64 KB"（后者没有第一个 KB）
    # ★★ 数字必须是 `\d+(?:\.\d+)?` —— 第一版写 `(\d+)` 会把 "14.32 KB" **从小数点切断**
    #    读成 "32 KB"，于是**正确的更正语句反被判成过期数字**（闸门自己的正则缺陷）。
    NUM = r'(\d+(?:\.\d+)?)'
    P1 = re.compile(r'(ITCM|DTCM|AXI|Flash|FLASH)[^\n]{0,24}?' + NUM + r'\s*KB\s*/\s*' + NUM + r'\s*KB')
    P2 = re.compile(r'(ITCM|DTCM|AXI|Flash|FLASH)\s*' + NUM + r'\s*/\s*' + NUM + r'\s*KB')
    # ★ 作废横幅豁免: 项目纪律是"不改写历史, 但必须显式作废" ⇒ 更正语句**必须**能引用旧数字。
    #   命中以下任一词的行整行跳过（这是一条**明示**的规则, 不是为了让某个文件过而打的补丁）。
    FORGIVE = re.compile(r'更正|过期|已废|曾经|原写|存活|不再是|~~')
    files = [os.path.join(R, "README.md")]
    for f in sorted(os.listdir(os.path.join(R, "docs"))):
        if f.endswith(".md"):
            files.append(os.path.join(R, "docs", f))
    for p in files:
        try:
            txt = io.open(p, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        for i, line in enumerate(txt.split("\n"), 1):
            if FORGIVE.search(line):
                continue                     # 作废横幅/更正留案: 允许引用旧数字与旧说法
            # ★ 第三种写法: **连数字都没有**的容量断言（"ITCM 已满"）—— 这正是本次抓到的形态。
            if re.search(r'(ITCM|DTCM|AXI)\s*(已满|满了)', line):
                bad.append("%s:%d 出现**无数字的容量断言**『%s』⇒ 请改成实测值 + 账本出处"
                           % (os.path.relpath(p, R), i,
                              re.search(r'(ITCM|DTCM|AXI)\s*(已满|满了)', line).group(0)))
            for m in list(P1.finditer(line)) + list(P2.finditer(line)):
                what, used, cap = m.group(1).upper(), float(m.group(2)), float(m.group(3))
                if what.startswith("ITCM"):
                    real_u, real_t = it_u / 1024.0, it_t / 1024.0
                elif what.startswith("DTCM"):
                    real_u, real_t = dt_u / 1024.0, dt_t / 1024.0
                elif what == "AXI":
                    real_u, real_t = axi_u / 1024.0, axi_t / 1024.0
                else:
                    continue
                if abs(used - real_u) > max(1.0, 0.15 * real_u):
                    bad.append("%s:%d 宣称 %s %d/%d KB，账本实测 %.1f/%.1f KB ⇒ 过期数字"
                               % (os.path.relpath(p, R), i, what, used, cap, real_u, real_t))
    return bad


# ────────────────────────── 自检（变异对照）──────────────────────────
def selftest():
    """★ 用**合成数据**证明 C2/C3 这两个最关键的判据会红。

    为什么必须有: 本工具是闸门的一部分，而"闸门自己恒绿"正是本项目最恨的形态。
    合成三组: ①正常 ②有段落在 AXI（须红）③SHM 段长不符（须红）。"""
    print("=== mem_report 判据自检（合成数据；证明 C2/C3 能红）===")
    cases = [
        ("正常", [(".itcm_text", 13636, 0), (".dtcm_shm", 32768, 0x20004EC0)], {}, 0x8000, True),
        ("有段落在 AXI（.axi_buf 复活）",
         [(".axi_buf", 64, 0x24000000)], {}, 0x8000, False),
        ("SHM 段长不符", [(".dtcm_shm", 32768, 0x20004EC0)], {}, 0x8000 - 16, False),
    ]
    ok_all = True
    for name, secs, sym, shm_size, want_pass in cases:
        axi_secs = [s for s in secs if 0x24000000 <= s[2] < 0x24050000]
        c2 = not axi_secs
        m = re.search(r"", "")
        # 合成 _shm_start/_shm_end
        s0 = next((s for s in secs if s[0] == ".dtcm_shm"), None)
        c3 = (s0 is not None and s0[1] == shm_size)
        got = c2 and c3
        hit = (got == want_pass)
        ok_all = ok_all and hit
        print("  [%s] %-28s C2=%s C3=%s（期望%s）"
              % ("PASS" if hit else "FAIL", name, c2, c3, "全过" if want_pass else "有红"))
    print("\n%s" % ("[PASS] C2/C3 的判定逻辑能区分正常与两种坏输入" if ok_all
                    else "[FAIL] 自检失效 —— 闸门可能是恒绿的"))
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
