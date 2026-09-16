#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dclc.py — DCL 文本组态编译器 v0 (DSL → 引擎路由表 → deploy)

把"可读的文本程序"编译成 esp32-core0 的路由表并下发(0x10 deploy + 0x11 start)。
语法移植自老项目 DCL语言规范 v1.0 (dcl-controller-excluded), 内核对齐 esp32-core0 17 原语。

v0/v0.1 支持语句:
  SENSOR <name> FROM sensor[<n>]                # 读 SENSOR_MAP[n] (如 hil 写 [2])
  SCALE  <name> FROM <sig> K=<k> B=<b>          # y = k*x + b
  PID    <name> FROM <sig> SP=<v> KP=<v> KI=<v> [KD=<v>]   # 输出自带 0..100 限幅
  ALARM  <name> FROM <sig> <op> <thr>           # op: > >= < <=  输出 1/0
  LOGIC  <name> = <a> AND <b> | <a> OR <b> | NOT <a>
  OUTPUT <name> TO wire[<n>] FROM <sig>         # DIRECT sig→固定槽 wire[n], name 是其别名
  CONST/GT-GE-LT-LE-EQ-NE/TON-TOF-TP/CTU-CTD/CTUD/SR-RS/R_TRIG-F_TRIG/
  LIMIT/ADD-SUB-MUL-DIV-MAX-MIN/SEL   (FBD 标准块, 见下方)
  SEQ <name> TO wire[<n>] [PERIOD=<t>]          # 顺序域 (Sequencer v0.2)
      UNTIL <sig> > <thr>                       #   该步: 条件推进
      DWELL <time>                              #   该步: 超时强推
      LOOP                                      #   末步行为: 回卷 (缺省=停完成态)
    例:
      SEQ heat TO wire[30] PERIOD=10ms
        UNTIL fb > 60.0      # 等温度到 60
        DWELL 30s            # 保温 30s
        LOOP                 # 回卷重来
  SEQ 块须放文件末尾 (其下不得再有其他语句)
注释: # 或 // (整行); 信号名=小写/数字/下划线, FB 大写

编译规则(与固件一致):
  - 每个 FB 输出一个信号, 自动分配 WIRE 槽(跳过 OUTPUT 固定的槽), 上限 64
  - 每个信号恰一个写者(B1); 有状态原语(PID)占独立 state 槽(≥1)
  - param 槽连续分配, PID=Kp,Ki,Kd,SP / SCALE=K,B / CMP=阈值

用法:
  python dclc.py prog.dcl            # 编译并下发到 COM7 (需板子在线, 引擎表可覆盖)
  python dclc.py prog.dcl --dump     # 只编译, 打印信号/路由分配, 不连板
  python dclc.py prog.dcl COM9       # 指定串口
"""
# ══════════════════════════════════════════════════════════════════════════
# ★ 本文件从 esp32-core0/tools/ 迁入 H723 线 (2026-09-11)。**只改了两处**:
#   ① `from test_dcl import ...` → `from h723_client import ...`
#      (test_dcl.py 是 S3 的验收脚本体, 不搬; 客户端接口由 h723_client.py 实现)
#   ② 默认串口 'COM7' → None (= 自动找 CH340, 免得用户以为要改代码)
#   其余**一行未动** —— 编译规则/校验规则/帧格式全部逐字保留。
#   语义等价性的证据: 同一套协议下 S3 的 test_dcl.py **一行不改** 能打 H723 (22/30)。
# ══════════════════════════════════════════════════════════════════════════

import sys, re, struct, time

# ---------- 常量 (与 shared_mem.h 对齐) ----------
OP = dict(DIRECT=0x00, CMP=0x01, HYST=0x02, CLAMP=0x03, LPF=0x04, PID=0x05,
          RATE=0x06, DEADBAND=0x07, MUX=0x08, EDGE=0x09, LUT=0x0A, CNT=0x0B,
          TIMER=0x0C, SCALE=0x0E, AND=0x0F, OR=0x10, NOT=0x11,
          ARITH=0x0D, SR=0x12)      # FBD 第二批
ARITH_MODE = dict(ADD=0, SUB=1, MUL=2, DIV=3, MAX=4, MIN=5)
# PERIOD= 时间字面量 → div 档索引 (与 shared_mem.h PERIOD_DIV_IDX_* 一致)
PERIOD_DIV = {0.0001: 0, 0.001: 1, 0.01: 2}
SRC = dict(SENSOR=0, WIRE=1, CONST=2, HMI=3)   # HMI=3: 通信域设定区 (40065-40128)
DST_WIRE = 2
MAX_WIRES, MAX_ROUTES, MAX_PARAMS, MAX_STATES = 128, 128, 128, 128
MB_NREG = 64      # 通信域设定区寄存器数 (shared_mem.h MB_NREG — 改容量须三处同步)
MAX_SEQ_INST, MAX_SEQ_STEPS = 8, 64      # 与 shared_mem.h 一致 (Sequencer v0)

# 原语模式参数 (与 primitives.h 一致)
CMP_MODE   = dict(GT=0, GE=1, LT=2, LE=3, EQ=4, NE=5)   # prim_cmp: value_b
TIMER_MODE = dict(TON=0, TOF=1, TP=2)                   # prim_timer: value_b
EDGE_MODE  = dict(R_TRIG=0, F_TRIG=1)                   # prim_edge: value_a
CNT_MODE   = dict(CTU=0, CTD=1)                         # prim_cnt: value_a
TIME_UNIT  = dict(us=1e-6, ms=1e-3, s=1.0, m=60.0, h=3600.0)

def parse_time(tok):
    """时间字面量 → 秒 (引擎 TIMER 阈值单位): 3s / 500ms / 100us / 2m / 1h / 裸数字=秒"""
    m = re.fullmatch(r'(-?[\d.]+(?:[eE][-+]?\d+)?)(us|ms|s|m|h)?', tok)
    if not m:
        raise SystemExit(f"错误: 时间字面量非法 '{tok}' (例: 3s / 500ms)")
    return float(m.group(1)) * TIME_UNIT[m.group(2) or 's']

# ---------- 解析 ----------
def strip_comment(line):
    # 去掉 # 与 // 注释(不处理字符串, DSL 无字符串)
    for m in re.finditer(r'#|//', line):
        return line[:m.start()]
    return line

def parse(text):
    """返回语句列表, 每句为 (kw, args...) 原样 token 化后的列表"""
    stmts = []
    for raw in text.splitlines():
        line = strip_comment(raw).strip()
        if not line:
            continue
        toks = line.split()
        kw = toks[0].upper()
        body = ' '.join(toks[1:])
        stmts.append((kw, body))
    return stmts

# ---------- 编译 ----------
class Sym:
    def __init__(self):
        self.slot = {}       # name -> wire slot
        self.pinned = set()  # 已固定的 wire 槽 (OUTPUT)
        self.auto_next = 1        # wire[0] 是"无第二源"哨兵 (core0_isr: wire2_idx=0 判空)
                                  # 自动分配必须避开, 否则 AND/OR 引用首个信号恒假 (审计 M1)
        self.params = []     # 每项 4 floats
        self.routes = []     # 每项 route 参数字典
        self.state_next = 1  # state 槽从 1 起 (0 保留/非法)
        self.cur_period = 0  # 当前语句的 div 档 (PERIOD= 后缀设置, 缺省 0=每拍)
        # 顺序域 (Sequencer v0.2): 单实例, SEQ 块须文件尾
        self.seq = None          # dict: out_wire, div, steps[(cond_t,cond_i,thr,dwell,loop_last)]
        self.desc = []       # 人读描述
        self._ct_zero = None # 惰性: "恒 0 的 wire" 槽 (给没接复位端的 CTU/CTD 当第二输入)

    def zero_wire(self):
        """惰性分配一根**恒 0 的 wire**, 供"没接复位端"的 CTU/CTD 当第二输入。

        ★★★ 为什么需要 (2026-09-11 端到端验收 `tools/h723_demo_e2e.py` 实测发现):
          固件对**双输入原语** (AND/OR/ARITH/SR/**CNT**) 要求第二输入必须有效 ——
          `op_needs_wire2() && !wire2_valid()` ⇒ NAK
          "this op needs wire2 source"。这是 H723 的 **A3 审计故意收紧**的判据,
          用来拦"漏给第二输入"这类**静默语义错误** (AND 恒假 / ARITH 加 0 / SR 永不复位...)。
          而 `CTU c CU=one PV=1000` (不写 `R=`) 原先被打包成"无第二输入"
          ⇒ **整份程序被拒** —— S3 侧碰不到, 因为 S3 的双输入校验只拦 AND/OR (A3 补全的)。
          ⇒ 这是被"校验器收紧"照出来的**潜藏编译器缺口**, 属于好事。

        ★ 修法选在**编译器**而不是放宽固件判据:
          IEC 语义里"不接复位端"= 复位输入恒为假 ⇒ 用一根恒 0 的 wire 接上**语义等价**。
          不放宽固件, 因为放宽会同时牺牲 AND/OR/ARITH 的保护 —— 收紧 > 放宽。
        ★ 全程序共用一根 (只多 1 条路由 + 1 个参数)。
        """
        if self._ct_zero is None:
            ch = self.alloc_wire("_ct_zero")
            p = self.add_param([0.0, 0.0, 0.0, 0.0])
            self.add_route(SRC['CONST'], p, OP['DIRECT'], ch)
            self._ct_zero = ch
        return self._ct_zero

    def alloc_wire(self, name, pin=None):
        if name in self.slot:
            raise SystemExit(f"错误: 信号 '{name}' 重复声明 (B1 唯一写者)")
        if pin is not None:
            if pin in self.pinned:
                raise SystemExit(f"错误: wire[{pin}] 被多个 OUTPUT 固定")
            self.slot[name] = pin; self.pinned.add(pin)
            return pin
        while self.auto_next in self.pinned:
            self.auto_next += 1
        if self.auto_next >= MAX_WIRES:
            raise SystemExit(f"错误: WIRE 槽耗尽 (>{MAX_WIRES})")
        s = self.auto_next; self.auto_next += 1
        self.slot[name] = s
        return s

    def ref(self, token):
        """把引用 token 解析为 (src_type, idx). token: 信号名 或 sensor[n]"""
        m = re.fullmatch(r'sensor\[(\d+)\]', token)
        if m:
            n = int(m.group(1))
            if n >= MAX_WIRES:
                raise SystemExit(f"错误: sensor[{n}] 越界")
            return SRC['SENSOR'], n
        m = re.fullmatch(r'wire\[(\d+)\]', token)   # 裸 wire 槽引用 (SEQ 步号镜像等)
        if m:
            n = int(m.group(1))
            if n >= MAX_WIRES:
                raise SystemExit(f"错误: wire[{n}] 越界")
            return SRC['WIRE'], n
        m = re.fullmatch(r'hmi\[(\d+)\]', token)    # HMI 设定区 (上位机可写, 程序读)
        if m:
            n = int(m.group(1))
            if n >= MB_NREG:
                raise SystemExit(f"错误: hmi[{n}] 越界 (设定区 0..{MB_NREG - 1})")
            return SRC['HMI'], n
        if token not in self.slot:
            raise SystemExit(f"错误: 未定义信号 '{token}' (必须先声明再引用)")
        return SRC['WIRE'], self.slot[token]

    def add_route(self, src_t, src_i, op, ch, param_idx=0, state_off=0,
                  wire2=None, period=None):
        # wire2=None → 无第二输入 (打包不带 ROUTE_FLAG_WIRE2, 固件 wb=0);
        # wire2=槽号 → 有第二输入 (槽号可为 0 = 引 wire[0], 打包必须带标志 —
        #   N-A 审计修复: 原实现用 wire2 的值判标志, 引 wire[0] 时值=0 丢标志,
        #   第二输入静默失效. 现在用显式布尔, 彻底区分"无输入"与"输入=wire[0]")
        # period=None → 继承当前语句的 PERIOD= 档 (缺省 div0 每拍)
        self.routes.append(dict(src_t=src_t, src_i=src_i, op=op, ch=ch,
                                param_idx=param_idx, state_off=state_off,
                                wire2=wire2, wire2_valid=wire2 is not None,
                                period=self.cur_period if period is None else period))
        if len(self.routes) > MAX_ROUTES:
            raise SystemExit(f"错误: 路由数超限 (>{MAX_ROUTES})")

    def alloc_state(self):
        """有状态原语 (TIMER/CNT/EDGE/LPF/PID/HYST/RATE/DEADBAND) 必须挂 state 槽;
        槽 0 固件判为'无槽' → ISR 解引用 NULL (T22 曾 panic), 故从 1 起分配"""
        so = self.state_next
        self.state_next += 1
        if so >= MAX_STATES:
            raise SystemExit("错误: STATE 槽耗尽")
        return so

    def add_param(self, vals):
        if len(self.params) >= MAX_PARAMS:
            raise SystemExit(f"错误: PARAM 槽耗尽 (>{MAX_PARAMS})")
        for v in vals:
            if not (isinstance(v, (int, float)) and v == v):  # NaN 检查
                raise SystemExit(f"错误: 参数含 NaN/非法值")
        self.params.append(vals)
        return len(self.params) - 1

    # ---- 顺序域 (Sequencer v0.2) ----
    def seq_begin(self, out_wire, div):
        if self.seq is not None:
            raise SystemExit("错误: 只支持一个 SEQ 块")
        if out_wire >= MAX_WIRES:
            raise SystemExit(f"错误: SEQ out wire[{out_wire}] 越界")
        self.seq = dict(out_wire=out_wire, div=div, steps=[])

    def seq_add_step(self, cond_t, cond_i, thr, dwell, is_loop=False):
        """推进依据: cond_t/cond_i (条件源) + thr (阈值) 或 dwell (超时秒, >0 启用);
        返回 param_idx"""
        p = self.add_param([thr, dwell if dwell > 0 else 0.0, 0.0, 0.0])
        self.seq["steps"].append(dict(cond_t=cond_t, cond_i=cond_i,
                                      thr=thr, dwell=dwell, param_idx=p,
                                      is_loop=is_loop))
        if len(self.seq["steps"]) > MAX_SEQ_STEPS:
            raise SystemExit(f"错误: SEQ 步数超限 (>{MAX_SEQ_STEPS})")

    def kv(self, body, key, default=None, required=False, cast=float):
        m = re.search(r'\b' + key + r'=(-?[\d.eE+]+)', body)
        if not m:
            if required:
                raise SystemExit(f"错误: 缺少必填参数 {key}=")
            return default
        return cast(m.group(1))


def compile_stmts(stmts):
    S = Sym()
    # 第一遍: 先处理 OUTPUT 的固定槽, 避免自动槽占用
    # (M5 审计: set.add 幂等对"两个不同名 OUTPUT 钉同一 wire"静默 — 编译期给清晰错误,
    # 不靠固件 NAK 兜底)
    pin_owner = {}   # wire 槽 -> OUTPUT 名 (唯一写者预检)
    for kw, body in stmts:
        if kw == 'OUTPUT':
            m = re.fullmatch(r'(\S+)\s+TO\s+wire\[(\d+)\]\s+FROM\s+(\S+)', body)
            if not m:
                raise SystemExit(f"语法错误 OUTPUT: {body}")
            name, n, _src = m.group(1), int(m.group(2)), m.group(3)
            if n >= MAX_WIRES:
                raise SystemExit(f"错误: wire[{n}] 越界")
            if n in pin_owner:
                raise SystemExit(f"错误: wire[{n}] 被 OUTPUT '{pin_owner[n]}' 与 '{name}' 同时固定 — 双写者 (M5)")
            pin_owner[n] = name
            S.pinned.add(n)
    # 第二遍: 生成 (SEQ 块须在文件尾 — 用 enumerate 以便收集其后所有块行)
    for _seq_pos, (kw, body) in enumerate(stmts):
        # PERIOD=<t> 后缀: 该语句跑在哪个 div 档 (引擎仅三档: 100us / 1ms / 10ms)
        S.cur_period = 0
        mp = re.search(r'\s+PERIOD=(\S+)\s*$', body)
        if mp:
            t = round(parse_time(mp.group(1)), 6)
            if t not in PERIOD_DIV:
                raise SystemExit(f"错误: PERIOD={mp.group(1)} 不是引擎支持的档 "
                                 f"(仅 100us / 1ms / 10ms)")
            S.cur_period = PERIOD_DIV[t]
            body = body[:mp.start()]
        # ---- SEQ 顺序域块 (Sequencer v0.2): 独立 if (非 elif 链, break 提前收尾) ----
        if kw == 'SEQ':
            m = re.fullmatch(r'(\S+)\s+TO\s+wire\[(\d+)\]', body)
            if not m:
                raise SystemExit(f"语法错误 SEQ: {body}  (应为: SEQ <name> TO wire[<n>])")
            name, out_n = m.group(1), int(m.group(2))
            # 唯一写者 (审计 OA8): SEQ 名绕过 alloc_wire — setdefault 对重名静默,
            # 名字断裂 (两个信号同名指向不同 wire, desc/引用混淆)。与 alloc_wire 同款拦截
            if name in S.slot:
                raise SystemExit(f"错误: 信号 '{name}' 重复声明 (SEQ)")
            # 唯一写者 (审计 OA3): 不能只查显式 OUTPUT 固定 (pinned) — auto 分配
            # 的路由也占了槽, 生产者全集 = slot.values()。SEQ 在文件尾声明, 前面
            # 路由的槽都已登记, 此刻查必然不漏。撞槽 = 双写者 (seq 步号镜像被路由
            # 每拍覆盖, 静默逻辑错, OA3 实锤)。wire[0] 是"无第二源"哨兵, 镜像禁写。
            if out_n == 0:
                raise SystemExit("错误: SEQ out wire[0] 保留为哨兵 (勿写, M1/F2/N-A)")
            if out_n in S.slot.values() or out_n in S.pinned:
                owner = next((nm for nm, s in S.slot.items() if s == out_n), '?')
                raise SystemExit(f"错误: SEQ out wire[{out_n}] 与信号 '{owner}' 撞槽 — 双写者 (OA3)")
            S.pinned.add(out_n)
            S.seq_begin(out_n, S.cur_period)
            S.slot.setdefault(name, out_n)
            block = stmts[_seq_pos + 1:]
            if not block:
                raise SystemExit("错误: SEQ 块为空 (至少一步)")
            n_line = len(block)
            for li, (bkw, bbody) in enumerate(block):
                if bkw not in ('UNTIL', 'DWELL', 'LOOP'):
                    raise SystemExit(f"错误: SEQ 块须在文件末尾, '{bkw}' 不能出现在块内")
                if bkw == 'LOOP':
                    if li != n_line - 1:
                        raise SystemExit("错误: LOOP 只能作为 SEQ 末行")
                    if not S.seq["steps"]:
                        raise SystemExit("错误: LOOP 前至少一步")
                    S.seq["steps"][-1]["is_loop"] = True
                    continue
                cond_t = cond_i = thr = None
                dwell = 0.0
                rest = bbody
                if bkw == 'DWELL':                    # 纯停留行: body = "1s"
                    dwell = parse_time(rest.strip())
                    rest = ""
                else:                                 # UNTIL 行: body = "sig > thr [DWELL t]"
                    mu = re.match(r'(\S+)\s*>\s*(-?[\d.eE+]+)', rest)
                    if mu:
                        cond_t, cond_i = S.ref(mu.group(1))
                        thr = float(mu.group(2))
                        rest = rest[mu.end():].strip()
                    md = re.match(r'DWELL\s+(\S+)', rest)
                    if md:
                        dwell = parse_time(md.group(1))
                        rest = rest[md.end():].strip()
                if rest:
                    raise SystemExit(f"语法错误 SEQ 步: {bbody}")
                if cond_t is None and dwell <= 0:
                    raise SystemExit(f"错误: SEQ 步需条件或超时: {bbody}")
                S.seq_add_step(cond_t if cond_t is not None else 2,
                               cond_i if cond_i is not None else 0,
                               thr if thr is not None else 0.0, dwell)
            S.desc.append(f"SEQ     {name} = wire[{out_n}]  <- {len(S.seq['steps'])} 步"
                          + (" [LOOP]" if S.seq['steps'][-1]['is_loop'] else " [完成停]"))
            break   # SEQ 须文件尾

        if kw == 'OUTPUT':
            m = re.fullmatch(r'(\S+)\s+TO\s+wire\[(\d+)\]\s+FROM\s+(\S+)', body)
            name, n, src = m.group(1), int(m.group(2)), m.group(3)
            st, si = S.ref(src)
            if name in S.slot:          # 唯一写者
                raise SystemExit(f"错误: 信号 '{name}' 重复声明")
            S.slot[name] = n            # 别名固定 (pin 冲突已在第一遍检查)
            S.add_route(st, si, OP['DIRECT'], n)
            S.desc.append(f"OUTPUT  {name} = wire[{n}]  <- {src}")
        elif kw == 'SENSOR':
            m = re.fullmatch(r'(\S+)\s+FROM\s+sensor\[(\d+)\]', body)
            if not m:
                raise SystemExit(f"语法错误 SENSOR: {body}")
            name, n = m.group(1), int(m.group(2))
            ch = S.alloc_wire(name)
            st, si = S.ref(f'sensor[{n}]')
            S.add_route(st, si, OP['DIRECT'], ch)
            S.desc.append(f"SENSOR  {name} = wire[{ch}]  <- SENSOR[{n}]")
        elif kw == 'HMI':
            # HMI <name> FROM hmi[<n>] — 读通信域设定区 (上位机写 40065+n 即生效)
            m = re.fullmatch(r'(\S+)\s+FROM\s+hmi\[(\d+)\]', body)
            if not m:
                raise SystemExit(f"语法错误 HMI: {body}")
            name, n = m.group(1), int(m.group(2))
            ch = S.alloc_wire(name)
            st, si = S.ref(f'hmi[{n}]')
            S.add_route(st, si, OP['DIRECT'], ch)
            S.desc.append(f"HMI     {name} = wire[{ch}]  <- HMI[{n}] (40065+{n})")
        elif kw == 'SCALE':
            m = re.fullmatch(r'(\S+)\s+FROM\s+(\S+)\s+K=(-?[\d.eE+]+)\s+B=(-?[\d.eE+]+)', body)
            if not m:
                raise SystemExit(f"语法错误 SCALE: {body}")
            name, src, k, b = m.group(1), m.group(2), float(m.group(3)), float(m.group(4))
            ch = S.alloc_wire(name)
            st, si = S.ref(src)
            p = S.add_param([k, b, 0.0, 0.0])
            S.add_route(st, si, OP['SCALE'], ch, param_idx=p)
            S.desc.append(f"SCALE   {name} = wire[{ch}]  <- {k:.4f}*{src}{b:+.4f}")
        elif kw == 'PID':
            m = re.fullmatch(r'(\S+)\s+FROM\s+(\S+)\s+SP=(-?[\d.eE+]+)\s+KP=(-?[\d.eE+]+)\s+KI=(-?[\d.eE+]+)(?:\s+KD=(-?[\d.eE+]+))?', body)
            if not m:
                raise SystemExit(f"语法错误 PID: {body}")
            name, src = m.group(1), m.group(2)
            sp, kp, ki = float(m.group(3)), float(m.group(4)), float(m.group(5))
            kd = float(m.group(6)) if m.group(6) else 0.0
            ch = S.alloc_wire(name)
            st, si = S.ref(src)
            p = S.add_param([kp, ki, kd, sp])
            so = S.state_next; S.state_next += 1
            if so >= MAX_STATES:
                raise SystemExit("错误: STATE 槽耗尽")
            S.add_route(st, si, OP['PID'], ch, param_idx=p, state_off=so)
            S.desc.append(f"PID     {name} = wire[{ch}]  <- {src} (SP={sp} KP={kp} KI={ki} KD={kd} st@{so})")
        elif kw == 'ALARM':
            # 审计 M3/M4 修正: 旧版 '>=' 实际编译成 '>' (静默语义错), '<'/'<=' 直接报错。
            # prim_cmp 现在支持模式 value_b, 六种比较全部真实编译。
            m = re.fullmatch(r'(\S+)\s+FROM\s+(\S+)\s+(>=|<=|==|!=|>|<)\s+(-?[\d.eE+]+)', body)
            if not m:
                raise SystemExit(f"语法错误 ALARM: {body}")
            name, src, op_s, thr = m.group(1), m.group(2), m.group(3), float(m.group(4))
            mode = {'>': 0, '>=': 1, '<': 2, '<=': 3, '==': 4, '!=': 5}[op_s]
            ch = S.alloc_wire(name)
            st, si = S.ref(src)
            p = S.add_param([thr, float(mode), 0.0, 0.0])
            S.add_route(st, si, OP['CMP'], ch, param_idx=p)
            S.desc.append(f"ALARM   {name} = wire[{ch}]  <- ({src} {op_s} {thr})")
        elif kw in ('GT', 'GE', 'LT', 'LE', 'EQ', 'NE'):
            # FBD 标准比较族 (等价 ALARM, 标准名写法)
            m = re.fullmatch(r'(\S+)\s+IN=(\S+)\s+THR=(-?[\d.eE+]+)', body)
            if not m:
                raise SystemExit(f"语法错误 {kw}: {body}  (应为: {kw} <name> IN=<sig> THR=<v>)")
            name, src, thr = m.group(1), m.group(2), float(m.group(3))
            ch = S.alloc_wire(name)
            st, si = S.ref(src)
            p = S.add_param([thr, float(CMP_MODE[kw]), 0.0, 0.0])
            S.add_route(st, si, OP['CMP'], ch, param_idx=p)
            S.desc.append(f"{kw:<7} {name} = wire[{ch}]  <- ({src} {kw} {thr})")
        elif kw == 'LPF':
            # 一阶惯性滤波 (Filter_PT1): y += α·(src-y), α=dt/(τ+dt). τ 秒
            m = re.fullmatch(r'(\S+)\s+FROM\s+(\S+)\s+T=(-?[\d.eE+]+)', body)
            if not m:
                raise SystemExit(f"语法错误 LPF: {body}  (应为: LPF <name> FROM <sig> T=<τ秒>)")
            name, src = m.group(1), m.group(2)
            tau = float(m.group(3))
            if not (tau > 0.0):
                raise SystemExit(f"错误: LPF {name} 的 T 必须 > 0 (τ=0 直通无意义, 固件同拒)")
            ch = S.alloc_wire(name)
            st, si = S.ref(src)
            p = S.add_param([tau, 0.0, 0.0, 0.0])
            so = S.alloc_state()
            S.add_route(st, si, OP['LPF'], ch, param_idx=p, state_off=so)
            S.desc.append(f"LPF     {name} = wire[{ch}]  <- FROM {src} T={tau}s (st@{so})")
        elif kw == 'HYST':
            # 滞回比较: src>ON 置 1, src<OFF 清 0 (中间带内保持). 要求 ON>OFF 才有滞回带
            m = re.fullmatch(r'(\S+)\s+FROM\s+(\S+)\s+ON=(-?[\d.eE+]+)\s+OFF=(-?[\d.eE+]+)', body)
            if not m:
                raise SystemExit(f"语法错误 HYST: {body}  (应为: HYST <name> FROM <sig> ON=<a> OFF=<b>)")
            name, src = m.group(1), m.group(2)
            on, off = float(m.group(3)), float(m.group(4))
            if not (on > off):
                raise SystemExit(f"错误: HYST {name} 需 ON > OFF (否则滞回带不存在, 语义退化)")
            ch = S.alloc_wire(name)
            st, si = S.ref(src)
            p = S.add_param([on, off, 0.0, 0.0])
            so = S.alloc_state()
            S.add_route(st, si, OP['HYST'], ch, param_idx=p, state_off=so)
            S.desc.append(f"HYST    {name} = wire[{ch}]  <- FROM {src} ON={on} OFF={off} (st@{so})")
        elif kw == 'RATE':
            # 变化率 (微分近似): 输出 = (src-上拍src)/dt, 单位 每秒
            m = re.fullmatch(r'(\S+)\s+FROM\s+(\S+)', body)
            if not m:
                raise SystemExit(f"语法错误 RATE: {body}  (应为: RATE <name> FROM <sig>)")
            name, src = m.group(1), m.group(2)
            ch = S.alloc_wire(name)
            st, si = S.ref(src)
            p = S.add_param([0.0, 0.0, 0.0, 0.0])
            so = S.alloc_state()
            S.add_route(st, si, OP['RATE'], ch, param_idx=p, state_off=so)
            S.desc.append(f"RATE    {name} = wire[{ch}]  <- FROM {src} (/s, st@{so})")
        elif kw == 'DEADBAND':
            # 死区 (变化量过带才更新输出, 带内保持上一有效值)
            m = re.fullmatch(r'(\S+)\s+FROM\s+(\S+)\s+B=(-?[\d.eE+]+)', body)
            if not m:
                raise SystemExit(f"语法错误 DEADBAND: {body}  (应为: DEADBAND <name> FROM <sig> B=<带宽>)")
            name, src = m.group(1), m.group(2)
            band = float(m.group(3))
            if band < 0:
                raise SystemExit(f"错误: DEADBAND {name} 的 B 必须 >= 0")
            ch = S.alloc_wire(name)
            st, si = S.ref(src)
            p = S.add_param([band, 0.0, 0.0, 0.0])
            so = S.alloc_state()
            S.add_route(st, si, OP['DEADBAND'], ch, param_idx=p, state_off=so)
            S.desc.append(f"DEADBAND {name} = wire[{ch}]  <- FROM {src} B={band} (st@{so})")
        elif kw == 'CONST':
            m = re.fullmatch(r'(\S+)\s*=\s*(-?[\d.eE+]+)', body)
            if not m:
                raise SystemExit(f"语法错误 CONST: {body}  (应为: CONST <name> = <值>)")
            name, val = m.group(1), float(m.group(2))
            ch = S.alloc_wire(name)
            p = S.add_param([val, 0.0, 0.0, 0.0])
            S.add_route(SRC['CONST'], p, OP['DIRECT'], ch)   # SRC_CONST: src_index=param 槽
            S.desc.append(f"CONST   {name} = wire[{ch}]  <- {val}")
        elif kw in ('TON', 'TOF', 'TP'):
            m = re.fullmatch(r'(\S+)\s+IN=(\S+)\s+PT=(\S+)', body)
            if not m:
                raise SystemExit(f"语法错误 {kw}: {body}  (应为: {kw} <name> IN=<sig> PT=<时间>)")
            name, src = m.group(1), m.group(2)
            pt = parse_time(m.group(3))
            if pt <= 0:
                raise SystemExit(f"错误: {kw} {name} 的 PT 必须 > 0")
            ch = S.alloc_wire(name)
            st, si = S.ref(src)
            p = S.add_param([pt, float(TIMER_MODE[kw]), 0.0, 0.0])
            so = S.alloc_state()
            S.add_route(st, si, OP['TIMER'], ch, param_idx=p, state_off=so)
            S.desc.append(f"{kw:<7} {name} = wire[{ch}]  <- IN={src} PT={pt}s (st@{so})")
        elif kw in ('CTU', 'CTD'):
            # CTU: CU 上升沿累加, R 异步清零; CTD: CD 上升沿递减, LD 装载 PV
            # 复位端走 wire2 (F2 的第二输入管道), 输出 CV; Q 由 GE x IN=cv THR=PV 另取
            m = re.fullmatch(r'(\S+)\s+(?:CU|CD)=(\S+)(?:\s+(?:R|LD)=(\S+))?\s+PV=(\d+)', body)
            if not m:
                raise SystemExit(f"语法错误 {kw}: {body}  (应为: {kw} <name> CU=<sig> [R=<sig>] PV=<n>)")
            name, src, rst, pv = m.group(1), m.group(2), m.group(3), int(m.group(4))
            ch = S.alloc_wire(name)
            st, si = S.ref(src)
            # 无 R/LD → 接**恒 0 wire** (不是 None!)。见 zero_wire() 的长注释:
            # 固件的双输入校验要求 OP_CNT 必须有有效第二输入 (A3 审计),
            # 而 IEC 语义里"不接复位端"= 复位恒假 ⇒ 恒 0 wire 语义等价。
            # ★ OA1 的老坑仍要避开: 不能直接给 0 (0 = "无第二输入"哨兵, 会丢标志)。
            w2 = S.ref(rst)[1] if rst else S.zero_wire()
            p = S.add_param([float(CNT_MODE[kw]), float(pv), 0.0, 0.0])
            so = S.alloc_state()
            S.add_route(st, si, OP['CNT'], ch, param_idx=p, state_off=so, wire2=w2)
            S.desc.append(f"{kw:<7} {name} = wire[{ch}]  <- {src} PV={pv}"
                          + (f" 复位={rst}(w2={w2})" if rst else "") + f" (st@{so})")
        elif kw in ('R_TRIG', 'F_TRIG'):
            m = re.fullmatch(r'(\S+)\s+CLK=(\S+)', body)
            if not m:
                raise SystemExit(f"语法错误 {kw}: {body}  (应为: {kw} <name> CLK=<sig>)")
            name, src = m.group(1), m.group(2)
            ch = S.alloc_wire(name)
            st, si = S.ref(src)
            p = S.add_param([float(EDGE_MODE[kw]), 0.0, 0.0, 0.0])
            so = S.alloc_state()
            S.add_route(st, si, OP['EDGE'], ch, param_idx=p, state_off=so)
            S.desc.append(f"{kw:<7} {name} = wire[{ch}]  <- CLK={src} (st@{so})")
        elif kw == 'LIMIT':
            m = re.fullmatch(r'(\S+)\s+IN=(\S+)\s+MN=(-?[\d.eE+]+)\s+MX=(-?[\d.eE+]+)', body)
            if not m:
                raise SystemExit(f"语法错误 LIMIT: {body}  (应为: LIMIT <name> IN=<sig> MN=<v> MX=<v>)")
            name, src, mn, mx = m.group(1), m.group(2), float(m.group(3)), float(m.group(4))
            if mn > mx:
                raise SystemExit(f"错误: LIMIT {name} 的 MN > MX")
            ch = S.alloc_wire(name)
            st, si = S.ref(src)
            p = S.add_param([mn, mx, 0.0, 0.0])
            S.add_route(st, si, OP['CLAMP'], ch, param_idx=p)
            S.desc.append(f"LIMIT   {name} = wire[{ch}]  <- clamp({src}, {mn}, {mx})")
        elif kw in ('ADD', 'SUB', 'MUL', 'DIV', 'MAX', 'MIN'):
            # FBD 第二批: 二元算术/极值, 第二操作数走 wire2
            m = re.fullmatch(r'(\S+)\s+FROM\s+(\S+)\s+BY=(\S+)', body)
            if not m:
                raise SystemExit(f"语法错误 {kw}: {body}  (应为: {kw} <name> FROM=<sig> BY=<sig>)")
            name, src, by = m.group(1), m.group(2), m.group(3)
            ch = S.alloc_wire(name)
            st, si = S.ref(src)
            wb_t, wb_i = S.ref(by)
            if wb_t != SRC['WIRE']:
                raise SystemExit(f"{kw} 的第二操作数须为信号")
            p = S.add_param([float(ARITH_MODE[kw]), 0.0, 0.0, 0.0])
            S.add_route(st, si, OP['ARITH'], ch, param_idx=p, wire2=wb_i)
            op_sym = dict(ADD='+', SUB='-', MUL='*', DIV='/', MAX='max', MIN='min')[kw]
            S.desc.append(f"{kw:<7} {name} = wire[{ch}]  <- {src} {op_sym} {by} (w2={wb_i})")
        elif kw in ('SR', 'RS'):
            # 双稳态: S1=置位端, R=复位端; SR 置位优先 / RS 复位优先
            m = re.fullmatch(r'(\S+)\s+S1=(\S+)\s+R=(\S+)', body)
            if not m:
                raise SystemExit(f"语法错误 {kw}: {body}  (应为: {kw} <name> S1=<sig> R=<sig>)")
            name, setp, rstp = m.group(1), m.group(2), m.group(3)
            ch = S.alloc_wire(name)
            st, si = S.ref(setp)
            wb_t, wb_i = S.ref(rstp)
            if wb_t != SRC['WIRE']:
                raise SystemExit(f"{kw} 的 R 端须为信号")
            p = S.add_param([0.0 if kw == 'SR' else 1.0, 0.0, 0.0, 0.0])
            so = S.alloc_state()
            S.add_route(st, si, OP['SR'], ch, param_idx=p, state_off=so, wire2=wb_i)
            S.desc.append(f"{kw:<7} {name} = wire[{ch}]  <- S1={setp} R={rstp}"
                          f" ({'置位' if kw=='SR' else '复位'}优先, st@{so})")
        elif kw == 'CTUD':
            # 加减计数: CU 上升沿 +1, CD 上升沿 -1 (双输入管道已占满, 无 R/LD 端)
            m = re.fullmatch(r'(\S+)\s+CU=(\S+)\s+CD=(\S+)\s+PV=(\d+)', body)
            if not m:
                raise SystemExit(f"语法错误 CTUD: {body}  (应为: CTUD <name> CU=<sig> CD=<sig> PV=<n>)")
            name, cu, cd, pv = m.group(1), m.group(2), m.group(3), int(m.group(4))
            ch = S.alloc_wire(name)
            st, si = S.ref(cu)
            wb_t, wb_i = S.ref(cd)
            if wb_t != SRC['WIRE']:
                raise SystemExit("CTUD 的 CD 端须为信号")
            p = S.add_param([2.0, float(pv), 0.0, 0.0])   # mode=2 → CTUD
            so = S.alloc_state()
            S.add_route(st, si, OP['CNT'], ch, param_idx=p, state_off=so, wire2=wb_i)
            S.desc.append(f"CTUD    {name} = wire[{ch}]  <- CU={cu} CD={cd} PV={pv} (st@{so})")
        elif kw == 'SEL':
            # 二选一: G=0 → IN0, G=1 → IN1。无原生原语, 展开为
            #   IN0*(NOT G) + IN1*G  (4 路由: NOT + 2×MUL + ADD)
            m = re.fullmatch(r'(\S+)\s+G=(\S+)\s+IN0=(\S+)\s+IN1=(\S+)', body)
            if not m:
                raise SystemExit(f"语法错误 SEL: {body}  (应为: SEL <name> G=<sig> IN0=<sig> IN1=<sig>)")
            name, g, in0, in1 = m.group(1), m.group(2), m.group(3), m.group(4)
            sg, ig = S.ref(g); s0, i0 = S.ref(in0); s1, i1 = S.ref(in1)
            w_ng = S.alloc_wire(f"_sel_ng_{name}")
            w_a0 = S.alloc_wire(f"_sel_a0_{name}")
            w_b1 = S.alloc_wire(f"_sel_b1_{name}")
            ch = S.alloc_wire(name)
            p_not = S.add_param([0.0, 0.0, 0.0, 0.0])
            p_mul = S.add_param([float(ARITH_MODE['MUL']), 0.0, 0.0, 0.0])
            p_add = S.add_param([float(ARITH_MODE['ADD']), 0.0, 0.0, 0.0])
            S.add_route(sg, ig, OP['NOT'], w_ng, param_idx=p_not)
            S.add_route(s0, i0, OP['ARITH'], w_a0, param_idx=p_mul, wire2=w_ng)
            S.add_route(s1, i1, OP['ARITH'], w_b1, param_idx=p_mul, wire2=ig)
            S.add_route(SRC['WIRE'], w_a0, OP['ARITH'], ch, param_idx=p_add, wire2=w_b1)
            S.desc.append(f"SEL     {name} = wire[{ch}]  <- G={g} ? {in1} : {in0} (4 路由组合)")
        elif kw == 'LOGIC':
            # 语法: <name> = NOT <sig>                     (一元)
            #        <name> = a XOR b                       (二元, 4 路由组合)
            #        <name> = <sig> AND <sig> [AND <sig>...]   (N 输入链式归约)
            #        <name> = <sig> OR  <sig> [OR  <sig>...]   (op 一致, 混用报错)
            m2 = re.fullmatch(r'(\S+)\s*=\s*NOT\s+(\S+)', body)
            if m2:
                name, src = m2.group(1), m2.group(2)
                ch = S.alloc_wire(name)
                st, si = S.ref(src)
                S.add_route(st, si, OP['NOT'], ch)
                S.desc.append(f"LOGIC   {name} = wire[{ch}]  <- NOT {src}")
            else:
                # 通用解析: name = t0 op1 t1 op2 t2 ... (op ∈ AND/OR/XOR)
                parts = body.split()
                if len(parts) < 4 or parts[1] != '=' or len(parts) % 2 == 0:
                    raise SystemExit(f"语法错误 LOGIC: {body}")
                name = parts[0]
                toks = parts[2:]
                segs = []
                cur = toks[0]
                for i in range(1, len(toks), 2):
                    op = toks[i]
                    if op not in ('AND', 'OR', 'XOR'):
                        raise SystemExit(f"语法错误 LOGIC op '{op}': {body}")
                    segs.append((op, cur, toks[i + 1]))
                    cur = toks[i + 1]   # 链尾: cur=最后操作数 (校验用)
                if len(segs) == 1 and segs[0][0] == 'XOR':
                    # 二元 XOR: a XOR b = (a OR b) AND NOT(a AND b) — 4 路由/3 中间 wire
                    a, b = segs[0][1], segs[0][2]
                    sa, ia = S.ref(a)
                    sb, ib = S.ref(b)
                    if sb != SRC['WIRE']:
                        raise SystemExit("LOGIC XOR 第二输入须为信号")
                    w_or   = S.alloc_wire(f"_xor_or_{name}")
                    w_and  = S.alloc_wire(f"_xor_and_{name}")
                    w_nand = S.alloc_wire(f"_xor_nand_{name}")
                    ch = S.alloc_wire(name)
                    S.add_route(sa, ia, OP['OR'],  w_or,   wire2=ib)
                    S.add_route(sa, ia, OP['AND'], w_and,  wire2=ib)
                    S.add_route(SRC['WIRE'], w_and, OP['NOT'], w_nand)
                    S.add_route(SRC['WIRE'], w_or,  OP['AND'], ch, wire2=w_nand)
                    S.desc.append(f"LOGIC   {name} = wire[{ch}]  <- {a} XOR {b} (4 路由组合)")
                elif any(op == 'XOR' for op, _, _ in segs):
                    raise SystemExit("XOR 仅支持二元 (a XOR b), 链式请拆行")
                else:
                    ops = {op for op, _, _ in segs}
                    if len(ops) != 1:
                        raise SystemExit(f"错误: 逻辑链 op 混用 ({body}) — 请拆成多行")
                    op_s = ops.pop()
                    if len(segs) == 1:
                        # 二元 2 输入 (最常见): 直连, 不产生临时 wire
                        a, b = segs[0][1], segs[0][2]
                        ch = S.alloc_wire(name)
                        st, si = S.ref(a)
                        wb_t, wb_i = S.ref(b)
                        if wb_t != SRC['WIRE']:
                            raise SystemExit(f"LOGIC {op_s} 第二输入须为信号")
                        S.add_route(st, si, OP[op_s], ch, wire2=wb_i)
                        S.desc.append(f"LOGIC   {name} = wire[{ch}]  <- {a} {op_s} {b} (w2={wb_i})")
                    else:
                        prev_t, prev_i = S.ref(segs[0][1])
                        for k in range(1, len(segs)):
                            wb_t, wb_i = S.ref(segs[k][2])
                            if wb_t != SRC['WIRE']:
                                raise SystemExit(f"LOGIC {op_s} 第 {k+1} 输入须为信号")
                            last = (k == len(segs) - 1)
                            ch = S.alloc_wire(name if last else f"_lgc_{name}_{k}")
                            S.add_route(prev_t, prev_i, OP[op_s], ch, wire2=wb_i)
                            prev_t, prev_i = SRC['WIRE'], ch
                        S.desc.append(f"LOGIC   {name} = wire[{ch}]  <- "
                                      + " ".join(f"{a} {op}" for op, a, _ in segs)
                                      + f" {segs[-1][2]} ({op_s} 链, {len(segs)} 路由)")
        else:
            raise SystemExit(f"未知语句 {kw}")
        # div 档标注 (宣称必须等于实现: dump 里必须看得出这条跑在哪个档)
        if S.cur_period:
            S.desc[-1] += "   [div%d=%s]" % (
                S.cur_period, ('100us', '1ms', '10ms')[S.cur_period])
    return S


# ---------- 打包与下发 ----------
def pack_routes(routes):
    out = b''
    for r in routes:
        # N-A 修复: 用 wire2_valid 布尔设 WIRE2 标志 (原用 wire2 的值,
        # 引 wire[0] 时丢标志 → 第二输入静默失效); wire2=None 则不带标志
        fl = 1 | (2 if r['wire2_valid'] else 0)   # ACTIVE | WIRE2(第二输入有效)
        w2 = r['wire2'] if r['wire2_valid'] else 0
        out += struct.pack('<BBBBBBHHHHB',
                           r['src_t'], r['src_i'], DST_WIRE, r['ch'], r['op'], fl,
                           r['param_idx'], r['state_off'], 0, w2,
                           r['period']) + b'\x00'
    return out

def pack_params(params):
    return b''.join(struct.pack('<ffff', *p) for p in params)

def pack_seq(seq):
    """0x44 SEQ_DEPLOY 帧 (单实例 v0): [n_seq u8][n_total u16][目录6B][步表×16B]"""
    steps = seq["steps"]
    out = struct.pack("<BH", 1, len(steps))
    out += struct.pack("<BBBBH", len(steps), seq["out_wire"], seq["div"], 0, 0)
    last = len(steps) - 1
    for i, st in enumerate(steps):
        ct = st["cond_t"] if st["cond_t"] is not None else 2
        ci = st["cond_i"] if st["cond_i"] is not None else 0
        fl = 0
        if st["dwell"] > 0:
            fl |= 2                                   # timeout_en
        if i == last and st.get("is_loop"):
            fl |= 1                                   # 末步 loop 回卷
        out += struct.pack("<BBBBHHHI", ct, ci, fl, 0,
                           st["param_idx"], 0, 0, 0) + b"\x00\x00"
    return out


def main():
    if len(sys.argv) < 2:
        print(__doc__); sys.exit(1)
    path = sys.argv[1]
    dump = '--dump' in sys.argv
    port = next((a for a in sys.argv[2:] if not a.startswith('-')), None)

    text = open(path, encoding='utf-8').read()
    stmts = parse(text)
    S = compile_stmts(stmts)

    print(f"=== dclc: {path} → {len(S.routes)} 路由 / {len(S.params)} 参数 ===")
    for d in S.desc:
        print("  " + d)

    if dump:
        print("\n[--dump 仅编译, 未下发]")
        return

    from h723_client import Dcl, engine_status
    dcl = Dcl(port)
    s = engine_status(dcl)
    if not s:
        print(f"错误: 引擎无响应 ({port}); 先复位/检查串口"); sys.exit(1)
    # RESET 清旧表 (安全)
    dcl.send(0x13); time.sleep(0.3)
    payload = struct.pack('<HHH', len(S.routes), len(S.params), 0)
    payload += pack_routes(S.routes) + pack_params(S.params)
    sts, msg = dcl.send(0x10, payload)
    if sts != 'ACK':
        reason = msg.decode('utf-8', 'replace').strip() if isinstance(msg, (bytes, bytearray)) else ''
        print(f"deploy 被拒: {sts} — 固件校验不通过: {reason}")
        sys.exit(1)
    # Sequencer (v0.2): SEQ 块 → 0x44. 等 ISR reload 让 param 进 ACTIVE 表再校验
    if S.seq is not None:
        time.sleep(0.05)
        sts, msg = dcl.send(0x44, pack_seq(S.seq))
        if sts != 'ACK':
            reason = msg.decode('utf-8', 'replace').strip() if isinstance(msg, (bytes, bytearray)) else ''
            print(f"seq deploy 被拒: {sts} — {reason}")
            sys.exit(1)
    if '--nostart' in sys.argv:
        # 只部署不启动: 供验收脚本精确控制 START 时刻 (计时基准), 也符合
        # "上电不自动运行" 的安全语义 (引擎保持 STOP, 由上位机显式启动)
        print(f"[OK] deploy 完成 ({dcl.port}) — 引擎 STOP (--nostart)")
        return
    sts, msg = dcl.send(0x11)
    if sts != 'ACK':
        # ★ 2026-09-16 修: 原来**只发不判**。而 deploy 成功、START 被拒 是一个真实的可能状态
        #   (例如 F11 毒药表兜底: 历史持久化的超预算表会被 START 拒绝) —— 那时脚本照样打
        #   "[OK] ... 引擎 RUN", **而引擎其实没跑** ⇒ 正是本项目"宣称 > 实现"那一族。
        reason = msg.decode('utf-8', 'replace').strip() if isinstance(msg, (bytes, bytearray)) else ''
        print(f"START 被拒: {sts} — {reason}")
        print("  (deploy 已成功; 引擎仍在 STOP —— 常见原因: F11 预算门拦下了历史毒药表)")
        sys.exit(1)
    # ★ 另外修: 原来印的是**请求**端口 (自动找板时它是 None, 于是打出 "(None)") —— 印解析后的。
    print(f"[OK] deploy+START 完成 ({dcl.port}) — 引擎 RUN")


if __name__ == '__main__':
    main()
