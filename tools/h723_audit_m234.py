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
    """独立开一个串口问 0x01 —— 这是 M4 的判据本体 (链路活性)。"""
    from h723_w1 import build_frame
    for _ in range(tries):
        try:
            with serial.Serial(port, 115200, timeout=0.4) as s:
                time.sleep(0.15)
                s.reset_input_buffer()
                s.write(build_frame(CMD_GET_VERSION)); s.flush()
                d = s.read(64)
                if d:
                    return True
        except Exception:
            pass
        time.sleep(0.2)
    return False


def main():
    port = find_port(None)
    if not port:
        print("!! 找不到串口"); return 2
    print("端口: %s @ 115200\n" % port)

    # ══════════ M4-① 先确认链路活着 (否则后面所有判据都无意义) ══════════
    print("── T0 前置 ──")
    if not serial_alive(port):
        print("  !! 链路不活。先跑 python tools/h723_revive.py 解卡, 再重试。")
        return 2
    record("T0 链路活性 (GET_VERSION→ACK)", True, "已确认")

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
    print("审计 M2/M3/M4 回归: %d PASS / %d FAIL" % (npass, nfail))
    print("=" * 74)
    return 0 if nfail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
