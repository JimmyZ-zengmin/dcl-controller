#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
h723_audit_m234.py — 外部审计 W4 的 **M2 / M3 / M4** 三项回归

M2 (P2): `0x10` 写 qty≥60 → 请求帧 9+2qty ≥ 129 > 旧 MB_MAX_FRAME(128) 被拒/截断。
         修: MB_MAX_FRAME 128 → **255** (选 255 因 rx_len 是 uint8_t, 256 会回绕;
         而 qty≤123 的最长合法请求正是 255B)。
M3 (P2): `0x43` 报的"持久化条数"固定取 A 副本 → 两份条数不同时报错。
         修: persist_probe 取 **seq 较大**(最新)那份的条数。
M4 (P2): pyocd 工具跑完把核留在 **halt** → 后续串口测试全部假性失败
         (且"用 pyocd 查为什么串口没响应"本身会让串口没响应)。
         修: 各 pyocd 工具收尾链加 `go`; 本脚本额外**行为验证**这一点。

★ 判据都能失败: M2 的 qty=60 在旧常量下必 NAK; M3 的期望值 8 在旧实现下会读到 3;
  M4 的"halt 后串口应死、go 后应活"两半都断言。

用法:  python tools/h723_audit_m234.py
"""

# ★ Windows 控制台默认 GBK: 脚本自己 print 出来的个别字符 (⇒ / ✓ 等) 会以
#   UnicodeEncodeError **直接崩掉整个脚本** —— 数据都量到了, 却崩在"打印结论"这一步,
#   症状看起来像"脚本坏了"而不是"编码问题"。⇒ 统一在入口把 stdout 的错误策略改成
#   "永不抛" (换成 ?), 让验收脚本不可能因为自己的输出而失败。
#   (2026-09-11 实测: audit_m234 / w1 真的这么崩过一次, 整份结果都没打出来。)
import sys as _sys_enc
try:
    _sys_enc.stdout.reconfigure(errors="replace")
    _sys_enc.stderr.reconfigure(errors="replace")
except Exception:
    pass

import struct
import subprocess
import sys
import time

try:
    import serial
except ImportError:
    print("!! 需要 pyserial"); sys.exit(2)

_HERE = __import__("os").path.dirname(__import__("os").path.abspath(__file__))
sys.path.insert(0, _HERE)
from h723_modbus import Link, find_port, mb_frame, mb_check
from h723_seq import rt_route, SRC_WIRE, OP_DIRECT

CMD_GET_VERSION = 0x01
CMD_DEPLOY = 0x10
CMD_START = 0x11
CMD_STOP = 0x12
CMD_RESET = 0x13
CMD_ENGINE_STATUS = 0x38
CMD_READ_BURST = 0x22
CMD_PERSIST = 0x43
CMD_MB_INJECT = 0x60
CMD_MB_RESP = 0x61

OFF_SENSOR_MAP = 0x0040

PYOCD = ["pyocd", "cmd", "-t", "stm32h723xx", "-O", "connect_mode=under-reset"]

RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print("  [%s] %-52s %s" % ("PASS" if ok else "FAIL", name, detail))


def deploy_payload(nr):
    """最简合法程序: nr 条 DIRECT 路由 (dst_ch 必须互不相同 —— 固件查 dst 唯一写者)。
    参数/状态表留空 (DIRECT 不需要参数)。"""
    body = struct.pack("<HHH", nr, 0, 0)
    for i in range(nr):
        body += rt_route(SRC_WIRE, 0, i, OP_DIRECT, 0, div=0)
    return body


def persist_cmd(L, mode=None, timeout=6.0):
    pl = b"" if mode is None else bytes([mode])
    return L.xact(CMD_PERSIST, pl, timeout=timeout)


def serial_alive(port, tries=3):
    """链路活性判据本体 (M4 用它验"halt 时死 / go 后活")。

    ★★ 审计轴1 F1 修复 (2026-09-11): 原实现是 `d = s.read(64); if d: return True` ——
       **用"有没有字节"判活性**, 违反本项目铁律"判据用内容匹配, 不用字节数"。
       后果: 噪声、波特率不匹配、打开端口的瞬态**都会产生字节** ⇒ 一条时基全错的链路
       会被判成"活" (BRR 事故就是这么被带偏一整轮的)。
       现在复用 `h723_client.link_alive`: 必须**解析出 ACK 帧**且 **cap 与期望一致**
       (cap 一致还额外证明对端就是这台固件, 不是别的设备在回话)。
       ★ 对 M4 的语义没有削弱: 核被 halt 时不会有任何合法帧 ⇒ 仍判"死"。
    """
    from h723_client import link_alive
    return link_alive(port, tries=tries)


def main():
    port = find_port(None)
    if not port:
        print("!! 找不到串口"); return 2
    print("端口: %s @ 115200\n" % port)

    # ══════════ M4-① 先确认链路活着 (否则后面所有判据都无意义) ══════════
    print("── T0 前置 ──")
    # ★ M4 哨兵 (与 5 个串口套件同款): 无响应就先按"核被 pyocd 留 halt"处理并解卡 ——
    #   别把"上一个工具把核留在暂停"误判成"固件挂了"。本工具自己就撞过一次:
    #   它末尾的 wipe 走 erase, 而 **erase 之后光 `go` 放不开核** (实测), 于是下一轮 T0 直接挂。
    try:
        from h723_w1 import revive_if_dead as _rv
        _rv(port)
    except Exception:
        pass
    # ★ 审计轴1 F1 修复: 判据本身参与判定 (而不是"检查完再无条件记一行 True")。
    alive = serial_alive(port)
    record("T0 链路活性 (0x01 → ACK 帧 + cap 内容匹配)", alive,
           "已确认 (ACK 帧 + cap=0x0DF7)" if alive else "无有效应答帧")
    if not alive:
        print("  !! 链路不活 —— 先跑 python tools/h723_revive.py 看诊断, 再重试。")
        return 2

    with serial.Serial(port, 115200, timeout=0.05) as ser:
        L = Link(ser)
        time.sleep(0.2)
        sts, st = L.xact(CMD_ENGINE_STATUS)
        shm = struct.unpack("<I", st[23:27])[0] if sts == 0 and len(st) >= 27 else None
        record("T0b 读 SHM 基址", shm is not None, "shm=0x%08X" % (shm or 0))

        # ══════════ M2: 合法大 qty 写 (0x10) ══════════
        print("\n── M2: 0x10 写请求帧 > 128B (旧 MB_MAX_FRAME) ──")
        L.xact(CMD_RESET); time.sleep(0.2)

        def inject(fr, wait=0.35):
            s, _ = L.xact(CMD_MB_INJECT, fr)
            if s != 0:
                return s, None
            time.sleep(wait)
            s2, r = L.xact(CMD_MB_RESP)
            if s2 != 0 or len(r) < 2:
                return s2, None
            tl = r[1]
            return r[0], (bytes(r[2:2 + tl]) if tl else b"")

        # ★★★ 2026-09-16 修 (M2 一直"全红"的真因): **必须先切成"缓冲模式"。**
        #   实测根因: `c->tx_uart` **默认 = 1 = 响应从物理口发**（`modbus.c:798`），
        #   而物理口模式下发完就把 TX 缓冲清掉（`modbus.c:560/569`）⇒ `0x61` 的 `tx_len` 恒 0
        #   ⇒ 本测试拿到的 `resp` 永远是空 ⇒ 判 BAD。
        #   ⇒ 它验的其实是"0x61 在默认配置下读不到"，**不是** Modbus 的 qty 边界。
        #   ★ 切成缓冲模式后实测: qty=10/59/60/64 都得 8B 正常响应（`0110 9c81 00xx …`）、
        #     qty=65 得 5B 异常 02（`0190 02 …`）—— **`MB_MAX_FRAME=255` 的修复完全成立**。
        #   ⇒ 这是**测试缺陷**，固件正确。
        L.xact(0x62, bytes([0, 0]))        # src=0, tx_uart=0 → 响应留缓冲供 0x61 读回
        time.sleep(0.1)

        # qty 59 (len 127) / 60 (129, M2 边界) / 64 (137, 地址空间允许的上限)
        m2_ok, det = True, []
        for q in (59, 60, 64):
            body = struct.pack(">HHB", 40065, q, q * 2) + b"\x12\x34" * q
            st_, resp = inject(mb_frame(1, 0x10, body))
            ok = resp is not None and len(resp) == 8 and resp[1] == 0x10 and mb_check(resp)
            m2_ok = m2_ok and ok
            det.append("q%d(len%d)=%s" % (q, 9 + q * 2, "OK" if ok else "BAD"))
        record("M2-1 ★合法大 qty 写 59/60/64 → 回显 + CRC (旧代码 60/64 必 NAK)", m2_ok,
               " ".join(det))

        # qty=65 → 超 MB_SET 地址空间 (idx 64+65 > 128) ⇒ 现在应回**异常 02**
        # (旧代码是"帧太长"NAK —— 说明帧根本没被接收; 现在证明它被正常解析了)
        body = struct.pack(">HHB", 40065, 65, 130) + b"\x12\x34" * 65
        st_, resp = inject(mb_frame(1, 0x10, body))
        record("M2-2 ★qty=65: 帧被正常接收 → 异常 02 (地址越界, 而非长度拒绝)",
               resp is not None and len(resp) == 5 and resp[1] == 0x90 and resp[2] == 0x02
               and mb_check(resp), "resp=%s" % (resp.hex() if resp else None))
        L.xact(0x62, bytes([0, 1]))       # ★ 用完恢复默认 (tx_uart=1 = 物理口发) —— 好公民

        # ══════════ M3: 0x43 报最新副本的条数 ══════════
        print("\n── M3: 0x43 持久化条数必须取[最新副本] ──")
        L.xact(CMD_RESET); time.sleep(0.2)
        s1, p1 = L.xact(CMD_DEPLOY, deploy_payload(3))
        record("M3-a deploy 3 条 → ACK", s1 == 0, "sts=%s payload=%s"
               % (s1, p1.hex()[:16] if p1 else None))
        s2, _ = persist_cmd(L, mode=1)                # 落盘 #1 → A(seq=1, nr=3)
        record("M3-b 落盘 #1 (nr=3) → ACK", s2 == 0, "sts=%s" % s2)

        s3, _ = L.xact(CMD_DEPLOY, deploy_payload(8))
        record("M3-c deploy 8 条 → ACK", s3 == 0, "sts=%s" % s3)
        s4, _ = persist_cmd(L, mode=1)                # 落盘 #2 → B(seq=2, nr=8)
        record("M3-d 落盘 #2 (nr=8) → ACK", s4 == 0, "sts=%s" % s4)

        s5, q = persist_cmd(L)                        # 查询
        nr = struct.unpack_from("<H", q, 1)[0] if q and len(q) >= 3 else None
        ab = q[12] if q and len(q) >= 13 else None
        seqv = struct.unpack_from("<I", q, 8)[0] if q and len(q) >= 12 else None
        record("M3-e ★0x43 报最新副本条数 = 8 (旧代码固定取 A 会报 3)",
               s5 == 0 and nr == 8, "n_routes=%s ab_valid=%s seq=%s" % (nr, ab, seqv))

        # ══════════ P3: 0x38 r[22] 语义 = S3 的 run ══════════
        print("\n── P3: 0x38 r[22] 必须是 S3 语义的 run (gate 已挪到尾部 r[37]) ──")

        def st38():
            s, p = L.xact(CMD_ENGINE_STATUS)
            return (p[22], p[37]) if (s == 0 and len(p) >= 38) else (None, None)

        L.xact(CMD_START); time.sleep(0.2)
        run_on, gate_on = st38()
        L.xact(CMD_STOP); time.sleep(0.2)
        run_off, gate_off = st38()
        record("P3 ★r[22] 跟随 START/STOP (run), r[37]=gate 不受影响",
               run_on == 1 and run_off == 0 and gate_on == 1 and gate_off == 1,
               "START: r22=%s r37=%s | STOP: r22=%s r37=%s"
               % (run_on, gate_on, run_off, gate_off))

        L.xact(CMD_RESET); time.sleep(0.1)

    # ══════════ M4: halt 会让串口假死, go 能救回来 ══════════
    # ★★ 必须放在 with 块**之外**: Windows 上 COM 口是独占的, 外层 serial 不关闭,
    #    serial_alive() 就打不开第二个句柄 —— 那样"无响应"是**测试自己造成的**,
    #    判据会变成假的 (M4-1 会假阳性 PASS, M4-2 会假阴性 FAIL)。本轮实际踩到。
    print("\n── M4: pyocd 留 halt → 串口假性失败 ──")
    print("   [模拟一个'忘记 go'的 pyocd 工具] …")
    subprocess.run(PYOCD + ["-c", "reset halt"], capture_output=True, text=True, timeout=60)
    time.sleep(0.5)
    dead = not serial_alive(port, tries=2)
    record("M4-1 ★复现: pyocd 留 halt ⇒ 串口确实无响应 (假性失败)", dead,
           "无响应 = %s" % dead)

    print("   [用收尾 go 解卡] …")
    subprocess.run(PYOCD + ["-c", "reset", "-c", "go", "-c", "sleep 400"],
                   capture_output=True, text=True, timeout=60)
    time.sleep(0.4)
    alive = serial_alive(port, tries=3)
    record("M4-2 ★修复: 收尾 go ⇒ 串口恢复 (证明根因就是'没 go')", alive,
           "链路: %s" % ("活" if alive else "仍无响应"))

    # ══════════ 收尾: 把持久化配置擦掉, 回到 bench 默认 ══════════
    print("\n── 收尾: 擦除持久化配置 (本测试写过 flash) ──")
    r = subprocess.run([sys.executable, _HERE + "/h723_persist.py", "--wipe"],
                       capture_output=True, text=True, timeout=300)
    print("   h723_persist.py --wipe → rc=%d" % r.returncode)
    if r.returncode != 0:
        print("   !! wipe 失败, 请手动跑 python tools/h723_persist.py --wipe")
        print((r.stdout + r.stderr)[-400:])

    print("\n" + "=" * 74)
    npass = sum(1 for _, ok, _ in RESULTS if ok)
    nfail = len(RESULTS) - npass
    print("审计 M2/M3/M4 + P3 回归: %d PASS / %d FAIL" % (npass, nfail))
    print("=" * 74)
    return 0 if nfail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
