#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""inject_cmd.py — 免串口协议帧注入: 没有串口时, 给固件发一条**真实协议命令**。

原理 (项目既有机制, 见 main.c 里 g_cmd_req 的长注释):
  把**裸载荷** `[cmd][payload...]` 写进 SHM 的 `OFF_CMD_REQ`, 置 `g_cmd_req_len`,
  再置 `g_cmd_req = 1` ⇒ 主循环用**真实的 proto_dispatch** 处理它 ——
  与串口收到的帧**完全同一条路径** (命令分发/校验器/ACK-NAK/观测面计数都被走到),
  不是"绕过协议直接调函数"。

★ 符号地址从 `build/dcl_h723.map` **现取**, 不硬编码 (重编译会变)。
  受理与否用 `g_cmd_req_cnt` / `g_cmd_req_last` 对账 —— 地址错了当场暴露, 不会静默成功。
★ 全程在**一个 pyocd 会话**内完成: 写 → resume(核心真跑) → 等主循环消费 → halt → 回读。
  跨会话的 resume/halt 不可靠 (实测核心会被留在 halt 态)。

用法:
    python inject_cmd.py 62 00 01      # 0x62 MB_CFG: src=0(物理口) tx_uart=1(响应从 PA2 真发)
    python inject_cmd.py 61            # 0x61 MB_RESP: 读回响应帧 + 通信域状态
    python inject_cmd.py --show        # 只打印解析到的地址, 不下发
    python inject_cmd.py --wait 500 40 # 消费等待 500ms, 之后回读 40 字节 SHM 观测区

需要 pyocd (装在系统 Python 里, 不是受管 Python):
    "C:/Users/min/AppData/Local/Programs/Python/Python313/python.exe" inject_cmd.py ...
"""
import re
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

MAP = "build/dcl_h723.map"
TARGET = "stm32h723xx"
WANT = ("g_shm_addr", "g_cmd_req", "g_cmd_req_len", "g_cmd_req_cnt", "g_cmd_req_last")
OFF_CMD_REQ = 0x4DE0          # engine.h: 免串口帧暂存区 (在 SHM 内)
DEPLOY_REQ_MAX = 4096


def syms_from_map(path=MAP):
    out = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for ln in f:
                m = re.match(r"\s+0x([0-9a-fA-F]+)\s+(\w+)\s*$", ln)
                if m and m.group(2) in WANT:
                    out[m.group(2)] = int(m.group(1), 16)
    except OSError as e:
        print("[!] 读不到 %s: %s" % (path, e))
    return out


def main():
    argv = sys.argv[1:]
    if not argv:
        print(__doc__)
        return 2
    if "--show" in argv:
        for k, v in sorted(syms_from_map().items()):
            print("  %-16s = 0x%08X" % (k, v))
        return 0
    payload = bytearray(int(x, 16) & 0xFF for x in argv if not x.startswith("--"))
    wait_ms = 400
    if "--wait" in argv:
        wait_ms = int(argv[argv.index("--wait") + 1])
    if not payload:
        print("[X] 没给载荷")
        return 2
    if len(payload) > DEPLOY_REQ_MAX:
        print("[X] 载荷超过 DEPLOY_REQ_MAX")
        return 2

    s = syms_from_map()
    for k in ("g_shm_addr", "g_cmd_req", "g_cmd_req_len", "g_cmd_req_cnt", "g_cmd_req_last"):
        if k not in s:
            print("[X] map 里找不到符号 %s (先 build.sh)" % k)
            return 2

    from pyocd.core.helpers import ConnectHelper
    sess = ConnectHelper.session_with_chosen_probe(target_override=TARGET,
                                                   options={"connect_mode": "halt"})
    sess.open()
    try:
        t = sess.target
        shm = t.read32(s["g_shm_addr"])
        if shm == 0:
            print("[X] g_shm_addr = 0 (SHM 还没起来?)")
            return 1
        cnt0 = t.read32(s["g_cmd_req_cnt"])
        t.write_memory_block8(shm + OFF_CMD_REQ, payload)
        t.write32(s["g_cmd_req_len"], len(payload))
        print("载荷 %s  -> SHM+0x%04X (0x%08X), len=%d"
              % (payload.hex(" "), OFF_CMD_REQ, shm + OFF_CMD_REQ, len(payload)))
        for k, v in sorted(s.items()):
            print("   %-16s = 0x%08X" % (k, v))
        t.write32(s["g_cmd_req"], 1)
        t.resume()                     # ★ 核心真跑, 主循环才会消费
        time.sleep(wait_ms / 1000.0)
        t.halt()
        cnt1 = t.read32(s["g_cmd_req_cnt"])
        last = t.read32(s["g_cmd_req_last"])
        pend = t.read32(s["g_cmd_req"])
        ok = (cnt1 == cnt0 + 1) and (last == payload[0])
        print("受理: cnt %d -> %d, last=0x%02X (期望 0x%02X), 残留 g_cmd_req=%d  ==> %s"
              % (cnt0, cnt1, last, payload[0], pend, "OK" if ok else "FAIL"))
        if not ok:
            print("   [!] 没被受理: 地址解析错 / 主循环没跑到 / 帧长非法 —— 看上面地址对不对")
        return 0 if ok else 1
    finally:
        try:
            sess.target.resume()
        except Exception:
            pass
        sess.close()


if __name__ == "__main__":
    raise SystemExit(main())
