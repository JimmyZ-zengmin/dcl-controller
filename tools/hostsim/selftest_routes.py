#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""selftest_routes.py —— 上位机 HTTP 面的**结构自检**（能失败的判据）

════════════════════════════════════════════════════════════════════════════
为什么需要它（2026-09-22 事故）
════════════════════════════════════════════════════════════════════════════
一次普通的 Edit `bridge.py` 时，**把 `do_GET` 的方法头连带吃掉了** ——
它原本的 body（`/wave` / `/state` / index.html 回退）被并进了 `_ctl`。

后果链：
  1. **`py_compile` / `ast.parse` 完全无话可说** —— 语法合法、类合法、函数合法，
     缺的只是"**被框架回调的那个名字**"。⇒ 静态语法检查**原理上抓不到**这类缺陷。
  2. 跑着的**旧进程一切正常**（`/state` 照常出数据）⇒ 现场看起来"系统好着呢"。
  3. 只有**新加的路由**（`/stop`）返回 index.html ——
     症状酷似"路由写错了/前缀没匹配上"，而真因是"**这份代码根本没上线**"。
     实测为此白查了一轮（去核对 `/stop` 的分支条件、CMD 队列顺序……）。

★ 与 §〇 第 6 条（"设了就算"必错）同族：
  **"文件改过了" ≠ "改动生效了"**；**"语法过了" ≠ "结构在"**。

════════════════════════════════════════════════════════════════════════════
判据设计（照 §〇 第 8/9 条）
════════════════════════════════════════════════════════════════════════════
· 正向：真实文件上，`H.do_GET` 必须存在、必须**真的调用** `_ctl`、路由字符串必须齐。
· ★★ **反向（正对照）**：脚本内联一份**故意删掉 `do_GET` 的坏样本**，
  断言"检查函数对坏样本必须报 FAIL"。
  ⇒ 否则"脚本总说 OK"这件事**不构成证据** —— 它可能只是永远返回 True。
  （`--selftest` 就是跑这一半；不带参数跑真实文件时也会先跑它。）

用法:
    python tools/hostsim/selftest_routes.py              # 检查真实 bridge.py
    python tools/hostsim/selftest_routes.py --selftest   # 只跑反向正对照
"""
import ast
import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TARGET = os.path.join(HERE, "bridge.py")

# 必须存在的路由（少了哪条，对应功能就整条哑掉）
REQUIRED_ROUTES = ("/stop", "/motion", "/zero", "/mgmtpoll",
                   "/wave", "/state", "/pin", "/wire")

# ★ 层级对账字段（2026-09-22 用户指正"层级没处理好、上位机与板子做的不系统"的落点）
#   ★ 末尾三条 = 管理面（"把藏在代码里、要 pyocd 看的任务搬到上位机"）
REQUIRED_FIELDS = ("src_intent", "src_dev", "src_match",
                   "wire_ena", "wire_req", "stop_t", "stop_ok", "stop_msg",
                   "mgmt_manifest", "mgmt_poll", "MGMT_NAMES")


def _code_only(src):
    """剥掉注释后的源码。

    ★★ 为什么必须剥（2026-09-22 实测踩到）: 判据 ⑥ 用**字符串搜索**找
    `elif kind == "wire" and shm:`，而这条字符串**恰好写在我的注释里**（用来解释这个坑）
    ⇒ 判据对着**已经修好的**文件报 FAIL。**假阳性**。
    ⇒ 教训: 字符串型判据会命中注释与字符串字面量 ⇒ **判据必须在"代码"上跑**，
      否则"注释里描述缺陷"这件事本身会让判据变红（进而训练人去忽略它）。
    ★ 反过来看，这次假阳性也**证明了判据是能失败的**（不是永远 True）——
      所以修法是"修准"，不是"删掉判据"。"""
    out = []
    for ln in src.splitlines():
        # 去掉行内注释（本文件里没有 '#' 出现在字符串字面量中的情况）
        i = ln.find("#")
        out.append(ln[:i] if i >= 0 else ln)
    return "\n".join(out)


def check(src, label="bridge.py"):
    """返回 (ok, [问题...])。★ 纯函数 —— 好让反向正对照能喂坏样本进来。

    ★ 所有**字符串型**判据都跑在 `_code_only(src)` 上；**符号型**判据（ast）跑在原文上。"""
    bad = []
    code = _code_only(src)

    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        return False, ["语法错误: %s" % e]

    # ── ① 类 H 必须存在，且必须有 do_GET ──
    cls = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "H"]
    if not cls:
        return False, ["找不到 class H（HTTP 处理器）"]
    c = cls[0]
    meth = {n.name: n for n in c.body if isinstance(n, ast.FunctionDef)}

    if "do_GET" not in meth:
        bad.append("★★★ **缺 `do_GET`** —— HTTP 面整个哑掉，"
                   "而 `py_compile` 抓不到（缺的是回调名，不是语法）")
    if "_ctl" not in meth:
        bad.append("★★★ 缺 `_ctl`（路由实现）")

    # ── ② do_GET 必须**真的**调用 _ctl（光有名字不够 —— 空函数一样有名字）──
    if "do_GET" in meth:
        body = ast.get_source_segment(src, meth["do_GET"]) or ""
        if "_ctl(self.path)" not in _code_only(body):
            bad.append("★★ `do_GET` 存在但**没有调用 `_ctl(self.path)`** "
                       "⇒ 名字在、路由不通（比缺名字更隐蔽）")

    # ── ③ 路由字符串必须齐 ──
    for r in REQUIRED_ROUTES:
        if r not in code:
            bad.append("缺路由 `%s`" % r)

    # ── ④ 层级对账字段必须齐 ──
    for f in REQUIRED_FIELDS:
        if f not in code:
            bad.append("缺层级对账字段 `%s`" % f)

    # ── ⑤ 停机必须走 `wire` **和** `pin` 两条路（缺一条 = "停了又跑"复发）──
    #     ★ 这条是"能失败的判据"在业务上的落点：/stop 少了 wire[10] 就必然复发。
    m = re.search(r'path\.startswith\("/stop"\)(.*?)(?=\n        if |\n        p = )',
                  code, re.S)
    if not m:
        bad.append("找不到 `/stop` 的实现段")
    else:
        seg = m.group(1)
        if '("wire", 10, 0.0)' not in seg:
            bad.append("★★ `/stop` **没有打 `wire[10]=0`** "
                       "⇒ 程序面模式下必然「停了又跑」（用户 2026-09-22 实测现象）")
        if '("pin", 1, 0)' not in seg:
            bad.append("`/stop` 没有打脚手架 `sub=1 0` ⇒ 脚手架模式停不下来")
        if "stop_t" not in seg:
            bad.append("`/stop` 没有记 `stop_t` ⇒ 停机验证不会启动（"
                       "「发了命令」与「停下了」就分不开）")

    # ── ⑥ 不许存在"静默丢弃"型写法（事故家族的成员）──
    if 'elif kind == "wire" and shm:' in code:
        bad.append('★ 有 `elif kind == "wire" and shm:` —— `shm` 未知时**静默丢弃**命令；'
                   '改成「就地补读 0x38 或响亮报错」')

    return (len(bad) == 0), bad


# ══════════════════════════════════════════════════════════════════════
# 前端（index.html）的结构检查 —— 与 do_GET 同族缺陷的第二道闸
# ══════════════════════════════════════════════════════════════════════
# ★★ 为什么查这个（2026-09-22 实测踩到）: 我在控制面板加了 `<select id="src">`，
#   而 HUD 的 `<h3>` 里**早就有一个** `id="src"`（显示数据源 live/csv）。
#   `document.getElementById` 只返回**第一个** ⇒ `$('src').value` = `undefined`
#   ⇒ 请求变成 `/motion?src=undefined` ⇒ 后端落到 `scaffold` 分支
#   ⇒ **运动源选择完全失效**，而**语法合法、运行不报错、控制台干净**。
#   ⇒ 症状只有一行"层级源=scaffold"，看起来无关紧要 —— 与 `do_GET` 那次
#     （语法合法、缺回调名）是**同一族**：**结构性缺陷不会自己喊**。
FRONT_F = os.path.join(HERE, "index.html")


def _html_ids(src):
    """只取**真实标签内**的 id。

    ★★ 为什么不能直接 `re.findall(r'\\bid="([^"]+)"')`（2026-09-22 实测踩到）:
    我在注释里写了「HUD 的 `<h3>` 里已经有一个 ``id="src"``」来解释这个坑，
    而那行文字**被正则当成了第二个 id** ⇒ 判据对着**已经修好的**文件报"id 重复"。
    **假阳性**（与判据 ⑥ 命中注释是同一次教训）。
    ⇒ 修法: 只认**标签内部**的 id（`<tag ... id="x" ...>`）。
      注释里的 `id="src"` 前后不是标签语法，自然被排除。
    ★ 一般规律: **字符串型判据必须限定在"语法位置"上**，否则"注释里描述缺陷"
      这件事本身就会让判据变红 —— 而变红的次数一多，人就开始忽略它（判据就死了）。"""
    return re.findall(r'<[a-zA-Z][^>]*?\bid="([^"]+)"', src)


def _js_code(src):
    """剥掉 HTML 注释与 JS 行注释，供"引用检查"用。"""
    s = re.sub(r"<!--.*?-->", "", src, flags=re.S)
    s = re.sub(r"//[^\n]*", "", s)
    return s


def check_frontend(src=None):
    """返回 (ok, [问题...])。src 不给则读磁盘上的 index.html。"""
    if src is None:
        src = io.open(FRONT_F, encoding="utf-8").read()
    bad = []

    ids = _html_ids(src)
    dup = sorted({i for i in ids if ids.count(i) > 1})
    if dup:
        bad.append("★★ HTML id 重复: %s —— `getElementById` 只返回**第一个**，"
                   "后面的元素**静默失效**（语法合法、控制台无声）" % ", ".join(dup))

    # JS 里 `$('x')` / `getElementById('x')` 引用的 id 必须真的存在
    js = _js_code(src)
    refs = set(re.findall(r"\$\('([A-Za-z_][\w-]*)'\)", js))
    refs |= set(re.findall(r'getElementById\("([A-Za-z_][\w-]*)"\)', js))
    missing = sorted(r for r in refs if r not in ids)
    if missing:
        bad.append("★★ JS 引用了**不存在**的 id: %s ⇒ 那些行会在运行期抛 TypeError，"
                   "而**上游的 HUD 更新会整块中断**（一个坏 id 拖垮整段）" % ", ".join(missing))

    # 急停按钮必须真的走 /stop（而不是退回 /motion?on=0）
    # ★ 允许两种写法: 直接 `fetch('/stop')`，或赋值给 `stopAll()`（后者内部打 /stop）
    m = re.search(r"\$\('stp'\)\.onclick\s*=\s*([^;]{0,120});", js)
    if not m:
        bad.append("★ `$('stp').onclick` 找不到 ⇒ 急停按钮可能没接线")
    else:
        rhs = m.group(1)
        direct = "/stop" in rhs
        via_fn = "stopAll" in rhs and "fetch('/stop'" in js
        if not (direct or via_fn):
            bad.append("★★ 急停按钮**没有走 `/stop`**（右侧= %s）⇒ 会退回「只打脚手架」的老路，"
                       "程序面模式下必然「停了又跑」" % rhs.strip())

    # 行程归零必须真的打 /zero（否则在 HIL 下是"只改本地显示"的空操作）
    if "fetch('/zero'" not in js:
        bad.append("★★ 行程归零**没有打 `/zero`** ⇒ 只改本地显示偏移，而板子的 `wire[50]` "
                   "与③层程序的目标坐标系都没动 ⇒ HIL 下是空操作（实测：PC 位置 1921 mm "
                   "而程序目标 0 ⇒ 误差 −1800 ⇒ 电机满速跑 95 秒）")
    return (len(bad) == 0), bad


# 反向正对照：坏前端样本（必须被判 FAIL）
FRONT_INJ = (
    # 第 1 种坏法：id 重复（本次真实踩到的）
    ('<select id="msrc">', '<select id="src">'),
    # 第 2 种坏法：急停退回 /motion?on=0
    ("$('stp').onclick = stopAll;", "$('stp').onclick = () => fetch('/motion?on=0');"),
    # 第 3 种坏法：JS 引用不存在的 id
    ("$('stopv').textContent", "$('stopv_typo').textContent"),
    # 第 4 种坏法：行程归零退回"只改本地显示"（HIL 下是空操作 —— 实测让电机满速跑）
    ("fetch('/zero', {cache:'no-store'})", "void 0"),
)


def selftest_frontend():
    src = io.open(FRONT_F, encoding="utf-8").read()
    ok, bad = check_frontend(src)
    if not ok:
        print("真实 index.html 先就不过，无法做反向对照:", bad)
        return 1
    n_ids = len(re.findall(r'\bid="([^"]+)"', src))
    print("真实 index.html: PASS（%d 个 id 唯一，JS 引用全部存在）" % n_ids)
    fails = 0
    for i, (old, new) in enumerate(FRONT_INJ, 1):
        if old not in src:
            print("  注入 %d: ⚠ 锚点没找到 —— 正对照已失效（源码结构变了）" % i)
            fails += 1
            continue
        ok2, bad2 = check_frontend(src.replace(old, new, 1))
        if ok2:
            print("  注入 %d: ✗ **判据失效** —— 坏前端被判成 PASS！" % i)
            fails += 1
        else:
            print("  注入 %d: ✓ 正确报 FAIL —— %s" % (i, bad2[0][:80]))
    if fails:
        print("\n★★ 前端反向对照失败 %d 项 ⇒ '检查通过'不构成证据。" % fails)
        return 1
    print("前端反向对照全部通过。")
    return 0


# ══════════════════════════════════════════════════════════════════════
# 反向正对照：把真实文件**做一次"事故复现"**，检查函数**必须**报 FAIL
# ══════════════════════════════════════════════════════════════════════
INJ = (
    # 复现事故（第 1 种坏法）：`do_GET` 这个名字**整个消失** ⇒ HTTP 面全哑。
    #   ★ 锚点只取方法头一行 —— 因为它下面挂着长 docstring 与 `return`，
    #     锚点越长越容易在源码演化后失配（而"锚点失配"会被判成 FAIL，见下面）。
    ("    def do_GET(self):", "    def _do_GET_renamed_away(self):"),
    # 第 2 种坏法：do_GET 还在，但不调 _ctl（"名字在、路由不通"）—— 更隐蔽。
    ("return self._ctl(self.path)", "return None"),
    # 第 3 种坏法：/stop 少了 wire[10]=0（"停了又跑"复发）
    ('CMDQ.append(("wire", 10, 0.0))', "# removed"),
)


def selftest():
    src = io.open(TARGET, encoding="utf-8").read()
    ok, bad = check(src)
    if not ok:
        print("★ 真实文件先就不过，无法做反向对照:", bad)
        return 1
    print("真实文件: PASS（%d 条路由 / %d 个字段）" % (len(REQUIRED_ROUTES), len(REQUIRED_FIELDS)))

    fails = 0
    for i, (old, new) in enumerate(INJ, 1):
        if old not in src:
            print("  注入 %d: ⚠ 锚点没找到（跳过）—— ★ 这本身是一条失败判据："
                  "锚点失效说明源码结构变了，正对照已失去意义" % i)
            fails += 1
            continue
        broken = src.replace(old, new, 1)
        ok2, bad2 = check(broken, "injected-%d" % i)
        if ok2:
            print("  注入 %d: ✗ **判据失效** —— 坏样本被判成 PASS！" % i)
            print("          注入内容: %r -> %r" % (old[:48], new[:24]))
            fails += 1
        else:
            print("  注入 %d: ✓ 正确报 FAIL —— %s" % (i, bad2[0][:88]))
    if fails:
        print("\n★★ 反向对照失败 %d 项 ⇒ **'检查通过'不构成证据**，必须先修判据。" % fails)
        return 1
    print("\n反向对照全部通过 ⇒ 「检查通过」是有信息量的。")
    return 0


def main():
    if "--selftest" in sys.argv:
        a = selftest()
        print()
        b = selftest_frontend()
        return a or b

    # ★ 默认路径：先跑反向对照（证明判据能失败），再判真实文件
    a = selftest()
    print()
    b = selftest_frontend()
    print("-" * 66)

    src = io.open(TARGET, encoding="utf-8").read()
    ok, bad = check(src)
    if ok:
        print("bridge.py HTTP 面: PASS")
    else:
        print("bridge.py HTTP 面: FAIL")
        for x in bad:
            print("  ·", x)

    okf, badf = check_frontend()
    if okf:
        print("index.html 前端面: PASS")
    else:
        print("index.html 前端面: FAIL")
        for x in badf:
            print("  ·", x)

    return 0 if (ok and okf and a == 0 and b == 0) else 1


if __name__ == "__main__":
    raise SystemExit(main())
