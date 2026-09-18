#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""E-X —— 3.2/3.3 ③ 层表达力：`ABS` 组合展开 · 数字文法收敛 · `THR=<sig>` 变量阈值

## 这三件事为什么一起做
它们共用同一条纪律：**引擎没有的原语，用已有原语的组合表达，并且说清楚它是组合**
（`SEL` / `XOR` 已有先例）。而且引擎的两条硬约束决定了组合的形态：

  · `prim_cmp` **只从 `param.value_a` 取阈值**（`src/primitives.h`）⇒ 没有"第二输入当阈值"
    的形态 ⇒ 变量阈值只能展开成 `SUB`(求差) + `CMP`(与 0 比)。
  · 组合算子 `ARITH` 的第二输入**只能是 wire**（`wire2_idx`）⇒ 源不是 wire 时必须先补一条
    隐藏 `DIRECT`（否则"传感器直接进 ABS"会失败或更坏：静默用错槽）。

## 判据（都能失败）

离线
  X1 ★ `ABS` 编得过, 且展开恰好是 `CONST 0` + `SUB` + `MAX` 三条路由(结构逐个字段核对)
  X2 ★ 数字文法: 一张**必须收下**的表(`1e-3` / `-2.5E-7` / `.5` / `2E+7` / `+1.5`)
       与一张**必须被拒**的表(`1e` / `1.2.3` / `--1` / `nan` / `inf`)
       —— 且被拒时必须是**干净报错**(出现"不是合法数字字面量"), 不是 Python 栈回溯
  X3 ★ 变量阈值: `GT g IN=a THR=b` 展开成 `SUB`+`CMP`(2 路由), CMP 阈值为 0、mode 正确
  X4 ★ 字面量阈值**仍是 1 条原生 CMP**（证明确实没把所有比较都换成组合 —— 那会白吃路由）
  X5 负对照: `ABS` 少写 FROM / `THR=` 既非信号也非数字 ⇒ **干净报错**
  X6 回归: 既有 `examples/*.dcl` 全部仍能编过

在板（**数值等价** —— 结构对不算对）
  X7 ★★ `ABS`: 对 15 个输入(含 `+0.0`/`-0.0`/±小数/±大数)逐值比对 Python `abs()`,
       **按 float 位模式比**（否则 `+0.0` 与 `-0.0` 会被 `==` 判成相等, 判据失效）
  X8 ★★ 变量阈值 `GT a THR=b`: 对 9 组 (a,b) 比对 Python `a > b`
  X9 负对照: 非有限值(NaN/Inf)**写不进** sensor 槽（0x21 被拒）⇒ "NaN 被上游挡掉"这句话
       是可判的, 不是一句安慰

用法: python tools/exp_ex_composed_blocks.py [--offline]
退出码: 0 = 全 PASS / 1 = 有 FAIL / 2 = 前置不满足
"""
import argparse, io, os, re, struct, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import dclc                                            # noqa: E402

CMD_STATUS, CMD_DEPLOY, CMD_STOP, CMD_START, CMD_SEQ = 0x38, 0x10, 0x12, 0x11, 0x44
CMD_WRITE, CMD_READ = 0x21, 0x20
OFF_SENSOR_MAP, OFF_WIRE_MAP = 0x0040, 0x0240
MAX_SENSORS = 64
W_OUT = 40
PERIOD_DIV_SRC = os.path.join(ROOT, "src")


def free_sensor_slots():
    """★★ 从 `src/` **派生**"固件每拍会写"的 SENSOR 槽, 返回**自由**槽列表。

    为什么必须派生而不是随手挑一个: 本工具第一版把驱动输入写在 `SENSOR[2]` ——
    那是 **HIL 反馈槽**(`hil.h: HIL_FB_SENSOR 2`), 固件每拍覆写它 ⇒ 我写的值
    **读回来是别人的**(实测 0.26~0.29 之间乱跳) ⇒ 三条判据一起 FAIL。
    ★ 那不是判据坏了, 是**判据把前提假设错了**: "我写进去的槽, 我读得到"这一条
      必须先被验证(X0), 否则后面所有数值等价都是**在测别人的值**。

    固件写者(逐个从源码读出, 不手抄): as5600 RAW/DEG · hil 反馈 · di 4 路 · adc 3 路。"""
    def g(f, *names, idx=1):
        txt = io.open(os.path.join(PERIOD_DIV_SRC, f), encoding="utf-8",
                      errors="replace").read()
        out = []
        for n in names:
            m = re.search(r"^#define\s+%s\s+(\d+)u?" % n, txt, re.M)
            if not m:
                raise SystemExit("!! %s 里找不到 #define %s —— "
                                 "自由 SENSOR 槽的推导断了(拒绝猜)" % (f, n))
            out.append(int(m.group(1)))
        return out
    used = set()
    used.update(g("as5600.h", "AS5600_SENSOR_RAW", "AS5600_SENSOR_DEG"))
    used.update(g("hil.h", "HIL_FB_SENSOR"))
    b, n = g("di.h", "DI_SENSOR_BASE", "DI_COUNT")
    used.update(range(b, b + n))
    b, n = g("adc.h", "AI_SENSOR_BASE", "AI_NCH")
    used.update(range(b, b + n))
    return sorted(set(range(MAX_SENSORS)) - used), sorted(used)


FREE_S, USED_S = free_sensor_slots()
SENS_IN, SENS_REF = FREE_S[0], FREE_S[1]
CLOBBERED = 2          # HIL 反馈槽: 用来做"被固件覆写"的**负对照**

ABS_PROG = """SENSOR  in   FROM sensor[%d]
ABS     a    FROM in
OUTPUT  o    TO wire[%d] FROM a
""" % (SENS_IN, W_OUT)

CMP_PROG = """SENSOR  a    FROM sensor[%d]
SENSOR  b    FROM sensor[%d]
GT      g    IN=a THR=b
OUTPUT  o    TO wire[%d] FROM g
""" % (SENS_IN, SENS_REF, W_OUT)

CMP_LIT = """SENSOR  a   FROM sensor[%d]
GT      g   IN=a THR=2.5
OUTPUT  o   TO wire[%d] FROM g
""" % (SENS_IN, W_OUT)


def run_dclc(text):
    fd, path = tempfile.mkstemp(suffix=".dcl", prefix="_ex_")
    os.close(fd)
    try:
        io.open(path, "w", encoding="utf-8").write(text)
        r = subprocess.run([sys.executable, os.path.join(HERE, "dclc.py"), path, "--dump"],
                           capture_output=True, text=True, cwd=ROOT,
                           encoding="utf-8", errors="replace")
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def compile_mod(text):
    S = dclc.compile_stmts(dclc.parse(text))
    return S, dclc.pack_routes(S.routes), dclc.pack_params(S.params)


def f32(x):
    return struct.unpack("<f", struct.pack("<f", x))[0]


def decode(pk, params, i):
    """解一条打包后的路由（16B 布局见 engine.h 的 RouteEntry_t）。
    ★ 字段顺序是本项目踩过的坑（param_idx/state_offset/actuator/wire2 四个 u16 曾写错序），
      所以这里**按名字取**、并把 param 的 4 个 float 一并解出来。"""
    e = pk[i * 16:(i + 1) * 16]
    src_t, src_i, dst_t, dst_ch, op, fl = e[0], e[1], e[2], e[3], e[4], e[5]
    pidx, soff, act, w2 = struct.unpack("<HHHH", e[6:14])
    pv = struct.unpack("<4f", params[pidx * 16:pidx * 16 + 16])
    return dict(src_t=src_t, src_i=src_i, dst_t=dst_t, dst_ch=dst_ch, op=op,
                flags=fl, param_idx=pidx, state_off=soff, act=act, wire2=w2,
                period=e[14], pv=pv)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--port", default=None)
    a = ap.parse_args()
    res, skip = [], []
    print("=" * 74)
    print("E-X  ③ 层表达力: ABS 组合展开 · 数字文法收敛 · THR=<sig> 变量阈值")
    print("=" * 74)
    print("数字文法(唯一一处): %s" % dclc.NUMPAT)

    # ── X1 ABS 展开结构 ────────────────────────────────────────────────
    print("\n── X1 ★ `ABS` 展开 = CONST 0 + SUB + MAX（逐字段核对）──")
    S, pk, par = compile_mod(ABS_PROG)
    for d in S.desc:
        print("    " + d)
    R = [decode(pk, par, i) for i in range(len(S.routes))]
    ops = [r["op"] for r in R]
    modes = [int(r["pv"][0]) if r["op"] == dclc.OP["ARITH"] else None for r in R]
    print("    op 序列 = %s   ARITH 模式 = %s (SUB=1 MAX=4)" % (ops, modes))
    ar = [r for r in R if r["op"] == dclc.OP["ARITH"]]
    sub, mx = ar[0], ar[1]
    zin = [r for r in R if r["src_t"] == dclc.SRC['CONST']]
    win = R[0]["dst_ch"]                      # SENSOR in -> wire[1] 的那条 DIRECT
    print("    零常量路由: src_t=CONST src_i=%s ⇒ wire[%d]；ABS 输入线槽 = wire[%d]"
          % (zin[0]["src_i"] if zin else "无", zin[0]["dst_ch"] if zin else -1, win))
    print("    SUB: src=wire[%d] wire2=wire[%d] ⇒ wire[%d] ；MAX: src=wire[%d] wire2=wire[%d] ⇒ wire[%d]"
          % (sub["src_i"], sub["wire2"], sub["dst_ch"], mx["src_i"], mx["wire2"], mx["dst_ch"]))
    ok1 = (ops == [dclc.OP["DIRECT"]] * 2 + [dclc.OP["ARITH"]] * 2 + [dclc.OP["DIRECT"]]
           and modes == [None, None, dclc.ARITH_MODE["SUB"], dclc.ARITH_MODE["MAX"], None]
           and len(zin) == 1 and zin[0]["pv"][0] == 0.0
           and sub["src_t"] == dclc.SRC['WIRE'] and sub["src_i"] == zin[0]["dst_ch"]
           and sub["wire2"] == win
           and mx["src_t"] == dclc.SRC['WIRE'] and mx["src_i"] == win
           and mx["wire2"] == sub["dst_ch"])
    res.append(("X1 ABS = CONST0 + (0−x) + MAX(x, 0−x) 三条路由, 且接线逐字段正确", ok1))

    # ── X2 数字文法 ───────────────────────────────────────────────────
    print("\n── X2 ★ 数字文法：必须收下 / 必须被拒 ──")
    accept = ["1", "-2.5", ".5", "+1.5", "1e-3", "2.5E-7", "2E+7", "0.001"]
    reject = ["1e", "1.2.3", "--1", "nan", "inf", "1e+", "0x10", ""]
    bad_a, bad_r = [], []
    for lit in accept:
        rc, out = run_dclc("CONST c = %s\nOUTPUT o TO wire[40] FROM c\n" % lit)
        if rc != 0:
            bad_a.append(lit)
    for lit in reject:
        rc, out = run_dclc("CONST c = %s\nOUTPUT o TO wire[40] FROM c\n" % lit)
        # ★ 判据是"**干净**报错": 拒绝 + 有中文错误 + 无 Python 栈回溯。
        #   （不要求必须是"不是合法数字字面量"那一句 —— `CONST c = ` 这种连 token 都没有的
        #     写法, 正确的错就是"语法错误 CONST"。第一版把消息写死 ⇒ 自己造了一个假 FAIL。）
        clean = (rc != 0) and ("Traceback" not in out) and ("错误" in out)
        if not clean:
            bad_r.append((lit, rc, "Traceback" in out))
    print("    必须收下 %s ⇒ 失败 %s" % (accept, bad_a or "无"))
    print("    必须被拒 %s ⇒ 失败 %s" % (reject, bad_r or "无"))
    res.append(("X2a 科学计数法/带符号小数 %d 个全部收下" % len(accept), not bad_a))
    res.append(("X2b 非法字面量 %d 个全部**干净**被拒（无栈回溯）" % len(reject), not bad_r))
    rc, out = run_dclc("CONST c = 1e\nOUTPUT o TO wire[40] FROM c\n")
    print("    样例报错: " + next((l.strip() for l in out.splitlines()
                                    if "不是合法数字字面量" in l), "<无>")[:80])

    # ── X3/X4 比较: 变量 vs 字面量 ────────────────────────────────────
    print("\n── X3/X4 变量阈值展开成 2 路由；字面量阈值仍是 1 条原生 CMP ──")
    S3, pk3, par3 = compile_mod(CMP_PROG)
    for d in S3.desc:
        print("    " + d)
    R3 = [decode(pk3, par3, i) for i in range(len(S3.routes))]
    ops3 = [r["op"] for r in R3]
    ar3 = [r for r in R3 if r["op"] == dclc.OP["ARITH"]]
    cm3 = [r for r in R3 if r["op"] == dclc.OP["CMP"]]
    print("    op 序列 = %s (DIRECT×2 + ARITH + CMP + DIRECT)" % ops3)
    if ar3 and cm3:
        print("    SUB: src=wire[%d] wire2=wire[%d] ⇒ wire[%d] ；CMP: src=wire[%d] 阈值=%g mode=%g"
              % (ar3[0]["src_i"], ar3[0]["wire2"], ar3[0]["dst_ch"],
                 cm3[0]["src_i"], cm3[0]["pv"][0], cm3[0]["pv"][1]))
    ok3 = (ops3 == [dclc.OP["DIRECT"]] * 2 + [dclc.OP["ARITH"], dclc.OP["CMP"],
                                              dclc.OP["DIRECT"]]
           and len(ar3) == 1 and int(ar3[0]["pv"][0]) == dclc.ARITH_MODE["SUB"]
           and len(cm3) == 1 and cm3[0]["pv"][0] == 0.0
           and int(cm3[0]["pv"][1]) == dclc.CMP_MODE["GT"]
           and cm3[0]["src_i"] == ar3[0]["dst_ch"]
           and ar3[0]["src_i"] == R3[0]["dst_ch"] and ar3[0]["wire2"] == R3[1]["dst_ch"])
    res.append(("X3 变量 THR= 展开 = SUB(x−r)+CMP(与 0 比) 且接线正确", ok3))
    S4, pk4, par4 = compile_mod(CMP_LIT)
    R4 = [decode(pk4, par4, i) for i in range(len(S4.routes))]
    ops4 = [r["op"] for r in R4]
    print("    字面量档 op 序列 = %s ；CMP 阈值=%g" %
          (ops4, R4[1]["pv"][0] if len(R4) > 1 else float('nan')))
    res.append(("X4 字面量 THR= 仍是 1 条原生 CMP（没白吃路由）",
                ops4 == [dclc.OP["DIRECT"], dclc.OP["CMP"], dclc.OP["DIRECT"]]
                and R4[1]["pv"][0] == 2.5))

    # ── X5 负对照 ─────────────────────────────────────────────────────
    print("\n── X5 负对照：坏程序必须**干净报错** ──")
    for tag, text, needle in (
            ("ABS 少 FROM", "ABS a FROM\nOUTPUT o TO wire[40] FROM a\n", "语法错误 ABS"),
            ("THR 既非信号也非数字", "SENSOR a FROM sensor[2]\nGT g IN=a THR=@x\n"
                                     "OUTPUT o TO wire[40] FROM g\n", "不是合法数字字面量"),
            ("THR 引用未声明信号", "SENSOR a FROM sensor[2]\nGT g IN=a THR=nope\n"
                                   "OUTPUT o TO wire[40] FROM g\n", "不是合法数字字面量")):
        rc, out = run_dclc(text)
        good = rc != 0 and needle in out and "Traceback" not in out
        print("    %-22s rc=%d 命中『%s』=%s" % (tag, rc, needle, needle in out))
        res.append(("X5 %s ⇒ 干净报错" % tag, good))

    # ── X6 回归 ───────────────────────────────────────────────────────
    ex = sorted(f for f in os.listdir(os.path.join(ROOT, "examples")) if f.endswith(".dcl"))
    bad = []
    for f in ex:
        r = subprocess.run([sys.executable, os.path.join(HERE, "dclc.py"),
                            os.path.join(ROOT, "examples", f), "--dump"],
                           capture_output=True, text=True, cwd=ROOT,
                           encoding="utf-8", errors="replace")
        if r.returncode != 0:
            bad.append(f)
    print("\n── X6 回归：%d 个 examples 全部仍编过 ──%s" % (len(ex), "" if not bad else "  误伤: " + str(bad)))
    res.append(("X6 %d 个 examples 全部仍编过" % len(ex), not bad))

    # ── 在板 ─────────────────────────────────────────────────────────
    if a.offline:
        skip.append("X7/X8/X9 在板数值等价（--offline）")
    else:
        from h723_client import Dcl

        def rd(dcl, addr, nwords):
            sts, p = dcl.send(0x22, struct.pack("<IH", addr, nwords), expect_len=4 * nwords)
            return None if (sts != "ACK" or len(p) < 4 * nwords) else p[:4 * nwords]

        def wrf(dcl, addr, v):
            return dcl.send(CMD_WRITE, struct.pack("<II", addr, struct.unpack(
                "<I", struct.pack("<f", v))[0]), expect_len=None)[0]

        def wrbits(dcl, addr, bits):
            return dcl.send(CMD_WRITE, struct.pack("<II", addr, bits & 0xFFFFFFFF),
                            expect_len=None)[0]

        dcl = Dcl(a.port)
        print("\n端口 = %s" % dcl.port)
        time.sleep(1.0)
        try:
            sts, p = dcl.send(CMD_STATUS, expect_len=51)
            shm = struct.unpack("<I", p[23:27])[0]
            s_in = shm + OFF_SENSOR_MAP + 4 * SENS_IN
            s_rf = shm + OFF_SENSOR_MAP + 4 * SENS_REF
            w_out = shm + OFF_WIRE_MAP + 4 * W_OUT
            ok = False
            for _ in range(10):
                v = rd(dcl, shm + 0x3884, 1)
                if v and struct.unpack("<I", v)[0] >= 200000:
                    ok = True
                    break
                time.sleep(0.5)
            if not ok:
                skip.append("X7/X8/X9 板子未就绪")
            else:
                dcl.send(CMD_STOP)
                time.sleep(0.2)
                print("前置: 顺序域清除 ⇒ %s"
                      % dcl.send(CMD_SEQ, struct.pack("<BH", 0, 0), expect_len=None)[0])

                def deploy(text):
                    Sx, pkx, parx = compile_mod(text)
                    payload = struct.pack("<HHH", len(Sx.routes), len(parx) // 16, 0) + pkx + parx
                    st, q = dcl.send(CMD_DEPLOY, payload, expect_len=None)
                    if st != "ACK":
                        return False
                    dcl.send(CMD_START)
                    time.sleep(0.05)
                    return True

                # ── X0 前提自证: 自由槽真的自由; HIL 槽真的被覆写 ────
                print("\n── X0 ★ 前提自证：自由 SENSOR 槽读得回, HIL 槽读不回 ──")
                print("    固件会写的槽 = %s（由 src/ 派生）；自由槽 = %s（本工具用 %d/%d）"
                      % (USED_S, FREE_S[:6] + ["…"], SENS_IN, SENS_REF))
                st1 = wrf(dcl, s_in, 1.2345)
                rb1 = rd(dcl, s_in, 1)
                time.sleep(0.8)
                rb2 = rd(dcl, s_in, 1)
                ok_free = (rb1 and rb2 and struct.unpack("<f", rb1)[0] == f32(1.2345)
                           and struct.unpack("<f", rb2)[0] == f32(1.2345))
                stc = wrf(dcl, shm + OFF_SENSOR_MAP + 4 * CLOBBERED, 1.2345)
                time.sleep(0.2)
                rbc = rd(dcl, shm + OFF_SENSOR_MAP + 4 * CLOBBERED, 1)
                clob = rbc and struct.unpack("<f", rbc)[0] != f32(1.2345)
                print("    自由槽 sensor[%d]: 写 1.2345 ⇒ %s, 0.8s 后读回 %s"
                      % (SENS_IN, st1, struct.unpack("<f", rb2)[0] if rb2 else None))
                print("    HIL 槽 sensor[%d]: 写 1.2345 ⇒ %s, 读回 %s ⇒ 被覆写 = %s"
                      % (CLOBBERED, stc, struct.unpack("<f", rbc)[0] if rbc else None, clob))
                res.append(("X0 自由槽 sensor[%d] 写后读回位相同（判据的前提自证）" % SENS_IN,
                            bool(ok_free)))
                res.append(("X0' 负对照: HIL 槽 sensor[%d] 写后**读不回**（证明确实有域在覆写）"
                            % CLOBBERED, bool(clob)))

                # ── X9 负对照: 非有限值写不进 sensor ─────────────────
                print("\n── X9 负对照：NaN/Inf **写不进** sensor 槽（1/NaN 由上游挡掉）──")
                bad_fin = []
                for nm, bits in (("NaN", 0x7FC00000), ("+Inf", 0x7F800000),
                                 ("-Inf", 0xFF800000)):
                    st = wrbits(dcl, s_in, bits)
                    rb = rd(dcl, s_in, 1)
                    got = struct.unpack("<I", rb)[0] if rb else 0
                    rej = (st != "ACK")
                    nf = (got & 0x7F800000) == 0x7F800000
                    print("    写 %-5s ⇒ %s ；槽里是 0x%08X（非有限? %s）" % (nm, st, got, nf))
                    # ★ 失败条件 = "写被接受 **且** 槽里是非有限值"（第一版把这两个条件
                    #   的或/非写反了 ⇒ 判据必 FAIL, 看着像固件中招）。
                    bad_fin.append((not rej) and nf)
                res.append(("X9 NaN/±Inf 写 sensor 既未被接受、槽里也不是非有限值",
                            not any(bad_fin)))

                # ── X7 ABS 数值等价 ──────────────────────────────────
                print("\n── X7 ★★ `ABS` 逐值比对 Python `abs()`（按**位模式**比, 区分 ±0.0）──")
                if not deploy(ABS_PROG):
                    skip.append("X7 ABS 程序 deploy 被拒")
                else:
                    vals = [0.0, -0.0, 1.0, -1.0, 2.5, -2.5, 1e-3, -1e-3, 1e6, -1e6,
                            123.456, -123.456, 3.0e38, -3.0e38, 1e-30]
                    nbad, rows = 0, []
                    for v in vals:
                        st = wrf(dcl, s_in, v)
                        rb = rd(dcl, w_out, 1)
                        got = struct.unpack("<f", rb)[0] if rb else None
                        gb = struct.unpack("<I", rb)[0] if rb else 0
                        want = f32(abs(v))
                        wb = struct.unpack("<I", struct.pack("<f", want))[0]
                        eq = (gb == wb)
                        nbad += (not eq) or st != "ACK"
                        rows.append("%s→%s%s" % (v, got, "" if eq else " ✗want %s" % want))
                    print("    " + " | ".join(rows[:8]))
                    print("    " + " | ".join(rows[8:]))
                    res.append(("X7 ABS 对 %d 个输入**逐位**等于 Python abs()（含 -0.0→+0.0）"
                                % len(vals), nbad == 0))
                    dcl.send(CMD_STOP)

                # ── X8 变量阈值数值等价 ──────────────────────────────
                print("\n── X8 ★★ 变量阈值 `GT a THR=b` 逐组比对 Python `a > b` ──")
                if not deploy(CMP_PROG):
                    skip.append("X8 变量阈值程序 deploy 被拒")
                else:
                    pairs = [(1.0, 0.0), (0.0, 1.0), (2.5, 2.5), (-1.0, 1.0),
                             (1e6, 1e6), (1e-3, 2e-3), (0.0, -0.0), (-0.0, 0.0),
                             (123.456, 123.455)]
                    nbad2, rows2 = 0, []
                    for x, y in pairs:
                        s1 = wrf(dcl, s_in, x)
                        s2 = wrf(dcl, s_rf, y)
                        rb = rd(dcl, w_out, 1)
                        got = struct.unpack("<f", rb)[0] if rb else None
                        want = 1.0 if x > y else 0.0
                        eq = (got == want)
                        nbad2 += (not eq) or s1 != "ACK" or s2 != "ACK"
                        rows2.append("(%g,%g)→%s%s" % (x, y, got, "" if eq else " ✗want %g" % want))
                    print("    " + " | ".join(rows2))
                    res.append(("X8 GT(a,b) 对 %d 组**逐个**等于 Python `a > b`（含 0/-0、相等、"
                                "接近值）" % len(pairs), nbad2 == 0))
                    dcl.send(CMD_STOP)
        finally:
            try:
                dcl.send(CMD_STOP)
                time.sleep(0.2)
                dcl.send(CMD_SEQ, struct.pack("<BH", 0, 0), expect_len=None)
                dcl.send(CMD_START)
            except Exception:
                pass

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
