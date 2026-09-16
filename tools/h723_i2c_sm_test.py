#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
h723_i2c_sm_test.py —— **G6-1 验收**：拍内 I2C 事务状态机 与 阻塞路径 的对照判据

契约出处：`docs/REF-program-contract.md` §3.6（四条硬约束）/ §3.7（实施与验收）。
被测对象：`src/i2c_sm.c`（拍内每拍推进一步的状态机）与 `src/i2c_bb.c`（既有阻塞位翻转，**对照路径**）。

## 判据（每条都**能失败**）
| # | 判据 | 失败长什么样 |
|---|---|---|
| J1 | ★ **两条独立路径读数一致**：状态机读 AS5600 RAW ANGLE == 阻塞路径读的 | 值不等 ⇒ 状态机搬错了数据（本项目纪律：*两条独立路径对上才算数*）|
| J2 | ★ **每拍只推进一步**：`tick_cnt == 6 + n`（START+TX×3+RX×n+STOP 共 6+n 个相位）| 一拍跑完整个事务 ⇒ 该值会 ≪ 6+n ⇒ "有界"就不成立 |
| J3 | **信道干净**：`nak`/`stuck` **本次运行期间不得新增**（★ 增量判据，不是"累计为 0"）| 新增 NAK ⇒ 400 kHz 时序太快（tLOW 不足）或器件不在；新增 STUCK ⇒ 时钟被拉死 |
|    | ★ 原判据是"开机累计 `nak == 0`"，**顺序依赖**且失败文案误导（见 `main()` 里那段注释）| |
| J4 | **就绪门**：`status` 在完成前不是 OK、完成后 `result_len == n` | 完成前就 len>0 ⇒ 使用者会拿到半截数据 |
| J5 | **可重复**：连续 N 次全部 J1~J4 通过 | 偶发失败 ⇒ 时序边缘（比"一次通过"更值钱）|

## 用法
    python tools/h723_i2c_sm_test.py [--port COM21] [--n 10]
退出码：0 = 全通过；2 = 有判据失败；1 = 环境/协议错误。
★ AS5600: 7 位地址 **0x36**，RAW ANGLE 在 reg **0x0C**(高 4 位) 与 **0x0D**(低 8 位)。
"""
import argparse
import struct
import sys
import time

sys.path.insert(0, "tools")
from h723_client import Dcl                                    # noqa: E402

CMD_PIN_PATTERN = 0x39
OP_SM = 20                # 0x39 op=20: G6-1 诊断面
SUB_FIRE = 0              # 发起一次读
SUB_QUERY = 1             # 只读回
SUB_PING = 2              # ping 0x36
OP_AS5600_RT = 18         # 0x39 op=18: AS5600 运行态（阻塞路径的读数）


def sm(d, sub=SUB_QUERY):
    sts, p = d.send(CMD_PIN_PATTERN, struct.pack("<BBI", OP_SM, sub, 0))
    if sts != "ACK" or len(p) < 48:
        return None
    f = struct.unpack("<12I", p[:48])
    return dict(status=f[0], phase=f[1], pcnt=f[2], tcnt=f[3], req=f[4], ok=f[5],
                nak=f[6], stuck=f[7], gate=f[8], ticks=f[9], ln=f[10], data=f[11])


def as5600_blocking(d):
    """阻塞路径(i2c_bb)读到的 raw —— 它的 tx/ok/nak 计数也在这里"""
    sts, p = d.send(CMD_PIN_PATTERN, struct.pack("<BBI", OP_AS5600_RT, 0, 0))
    if sts != "ACK" or len(p) < 40:
        return None
    f = struct.unpack("<10I", p[:40])
    return dict(raw=f[0], deg_milli=f[1], status=f[2], mag_ok=f[3],
                ok=f[4], err=f[5], last_err=f[6], tx=f[7], i2c_ok=f[8], nak=f[9])


def sm_raw(v):
    """data 字节 0=reg0x0C(高 4 位), 1=reg0x0D(低 8 位)"""
    return ((v & 0xFF) << 8) | ((v >> 8) & 0xFF)


def main():
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:                                          # noqa: BLE001
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default=None)
    ap.add_argument("--n", type=int, default=10)
    a = ap.parse_args()

    d = Dcl(a.port)
    print(f"端口 = {d.port}")
    print(f"AS5600 基准(阻塞路径 op=18): {as5600_blocking(d)}")

    fails = []
    n_ok = 0
    # ★★ J3 的**基线**（2026-09-16 修：原判据断言"开机累计 nak == 0"，是**顺序依赖**缺陷）。
    #   原判据为什么错（实测抓到的三点）：
    #     ① `nak`/`stuck` 是**跨测试累计**的 C 侧计数，`0x13 RESET` **不清**它们（实测 49→49）；
    #     ② 于是**任何**先前产生过 NAK 的动作（例如 G6-4 验收里故意读不存在的从机 0x50、
    #        或板子上本来就没插 AS5600）会让本判据**永久变红**；
    #     ③ 更糟的是它的失败文案说的是"400 kHz 时序太快（tLOW 不足）" —— 一个**误导性诊断**，
    #        会把人推向改时序参数。本项目对同族缺陷的既有处置就是这句：
    #        **判据要看"有没有出现新的失败模式"，不要看累计值。**
    #   ⇒ 改成**增量判据**：本次运行期间 `nak`/`stuck` **一个都不许新增**。
    #     它比原来更强（原判据在 nak 初值非 0 时**什么都测不出来**）、且与跑测顺序无关。
    base = sm(d, SUB_QUERY)
    nak0 = base["nak"] if base else 0
    stuck0 = base["stuck"] if base else 0
    if nak0 or stuck0:
        print(f"  （基线: nak={nak0} stuck={stuck0} —— 开机至今的累计值, 不作为本判据的失败依据）")
    for i in range(a.n):
        # 先读一次基线（阻塞路径）—— 与状态机的读尽量贴近
        blk = as5600_blocking(d)
        if blk is None:
            print("[FAIL] op=18 无应答 ⇒ 环境错误"); return 1

        sts, _ = d.send(CMD_PIN_PATTERN, struct.pack("<BBI", OP_SM, SUB_FIRE, 0))
        if sts != "ACK":
            print(f"[FAIL] 第{i}次: 发起失败 {sts}"); return 1

        # ★ 就绪门: 立刻(不睡)读一次, 期望**还没完成**（窗口 8 拍 = 800µs, 串口往返常 >1ms
        #   ⇒ 多数情况这里已经 OK; 这本身是"窗口比观察者快"的诚实记录, 不假装测到 BUSY）
        time.sleep(0.004)                      # 4ms ≫ 800µs ⇒ 必须已完成
        r = sm(d, SUB_QUERY)
        if r is None:
            print("[FAIL] op=20 无应答"); return 1

        # J4 就绪门: 完成后长度必须是 2
        if not (r["status"] == 2 and r["ln"] == 2):
            fails.append(f"J4 #{i}: status={r['status']} len={r['ln']}（期望 2/2）")
            continue
        # J2 每拍一步: 6+n
        if r["tcnt"] != 6 + 2:
            fails.append(f"J2 #{i}: tick_cnt={r['tcnt']}（期望 {6+2}）⇒ 不是'每拍一步'")
            continue
        # J3 信道干净 —— ★ **增量判据**（本次运行期间不得新增 NAK/STUCK；不是"累计为 0"）
        if r["nak"] != nak0 or r["stuck"] != stuck0:
            fails.append(f"J3 #{i}: 本次新增 nak {nak0}->{r['nak']} / stuck {stuck0}->{r['stuck']}"
                         f"（新增 NAK ⇒ 400 kHz 时序太快或器件不在；新增 STUCK ⇒ 时钟被拉死）")
            continue
        # J1 ★ 两条独立路径读数一致
        v_sm = sm_raw(r["data"])
        if v_sm != blk["raw"]:
            fails.append(f"J1 #{i}: 状态机 raw={v_sm} != 阻塞路径 raw={blk['raw']}")
            continue
        n_ok += 1
        print(f"  [{i:2d}] OK  raw={v_sm} (阻塞={blk['raw']})  tcnt={r['tcnt']} pcnt={r['pcnt']} "
              f"ticks={r['ticks']} nak={r['nak']} stuck={r['stuck']} gate={r['gate']}")

    # ping 也走一遍（证明"通用事务"不是只读一种）。
    # ★ 相位口径（实测修正, 我第一版写成 2 是错的）: ping = START + TX(addr|W) + STOP = **3** 个相位。
    sts, _ = d.send(CMD_PIN_PATTERN, struct.pack("<BBI", OP_SM, SUB_PING, 0))
    time.sleep(0.004)
    rp = sm(d)
    ping_ok = (rp is not None and rp["status"] == 2 and rp["tcnt"] == 3)
    print(f"ping 0x36: status={rp['status'] if rp else None} tick_cnt={rp['tcnt'] if rp else None}"
          f"  (期望 3 = START+TX+STOP)  {'OK' if ping_ok else '**FAIL**'}")
    if not ping_ok:
        fails.append("ping: status/tick_cnt 不符（期望 2/3）")

    print()
    print(f"结果: J1..J4 通过 {n_ok}/{a.n}" + (f"; 失败 {len(fails)} 条" if fails else ""))
    for f in fails[:10]:
        print("  ✗ " + f)
    if fails:
        print("\n[FAIL] G6-1 未通过")
        return 2
    print("[PASS] G6-1 通过: 两条独立路径读数一致 + 每拍一步 + 信道干净 + 可重复 + ping 可用")


if __name__ == "__main__":
    sys.exit(main())
