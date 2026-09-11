#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
h723_la_modbus.py — W4 Modbus **物理段**验证: LA 抓 PA2 波形, 核对是否合法 Modbus RTU

★ 这一环证明什么 / 不证明什么 (必须说清, 否则就是"宣称>实现"):

  【证明】固件的**发送路径**真的工作 —— USART2 的 RCC/GPIO AF/BRR/CR1 全对,
          发出的电平波形经硬件解码后是**合法的 Modbus RTU 帧** (字节/CRC/间隔)。
          "配置全对 ≠ 功能可用"的反面证据: 这一条才是功能可用的证据。

  【不证明】从真实串口**接收**请求的能力。本测试的请求是经 **0x60 隧道**注入的
          (走 USB 协议口 COM14), 不经过 USART2_RX(PA3)。
          ⇒ PA3 的接收路径**代码已实现但从未被真实数据验证过**。
            要补这一环, 需要一个能主动发 Modbus 请求的对端 (USB 转 485 / 第二个 TTL)。

  ★ LA 只接一根线 (CH0←PA2) 是**够的**: 因为在这个测试里, "发"是全部被测行为。
    完整 Modbus 从站当然是双向的, 但"收"这一半在本测试中由隧道替代了。

接线: LA CH0 ← PA2 (USART2_TX) ; LA GND ← 板子 GND  (必须共地)

用法:
    python tools/h723_la_modbus.py                 # 抓一次并核对
    python tools/h723_la_modbus.py --port COM14 --secs 3
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

import argparse
import json
import os
import struct
import subprocess
import sys
import time
import urllib.request

LA = "http://127.0.0.1:10530/"
DEVICE = "F397589430559C0B"
HERE = os.path.dirname(os.path.abspath(__file__))

CMD_MB_INJECT = 0x60
CMD_MB_RESP   = 0x61
CMD_MB_CFG    = 0x62
CMD_RESET     = 0x13

_rpc_id = [0]


def rpc(method, params=None, timeout=60):
    _rpc_id[0] += 1
    body = {"jsonrpc": "2.0", "id": _rpc_id[0], "method": method}
    if params is not None:
        body["params"] = params
    req = urllib.request.Request(LA, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read().decode())
    if "error" in d:
        raise RuntimeError("LA error: %s" % d["error"])
    return d.get("result", {})


def tool(name, args, timeout=120):
    res = rpc("tools/call", {"name": name, "arguments": args}, timeout=timeout)
    if "structuredContent" in res:
        return res["structuredContent"]
    # 退回到解析 text
    txt = res.get("content", [{}])[0].get("text", "{}")
    try:
        return json.loads(txt)
    except Exception:
        return {"_raw": txt}


# ══════════ Modbus 帧工具 (独立实现 = 对端视角) ══════════

def mb_crc16(buf):
    crc = 0xFFFF
    for b in buf:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if (crc & 1) else (crc >> 1)
    return crc


def mb_frame(addr, func, body):
    p = bytes([addr, func]) + body
    c = mb_crc16(p)
    return p + bytes([c & 0xFF, (c >> 8) & 0xFF])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="COM14")
    ap.add_argument("--secs", type=float, default=3.0)
    a = ap.parse_args()

    sys.path.insert(0, HERE)
    from h723_w1 import Link
    import serial

    devs = tool("get_devices", {})
    devlist = (devs or {}).get("devices") or []
    if not devlist:
        print("!! 找不到 Logic 分析仪 —— 检查:")
        print("   ① LA 是否插好 ② Saleae 桥是否在跑 (MCP over HTTP @ 127.0.0.1:10530)")
        print("   (本工具需要 LA 抓 PA2 波形; 没有 LA 时请用 tools/h723_modbus.py 验协议栈)")
        return 2
    did = devlist[0]["deviceId"]
    print("LA 设备: %s" % did)

    # ★ 采集前先确认"探针真的接着"(skill 坑 7): 无法用软件判定接线,
    #   所以用**对照法** —— 先采一次"固件不发任何东西"的窗口, 应 0 跳变。
    # ★ M4 哨兵: 若核被 pyocd 留在 halt, 串口必然无响应 —— 先解卡, 别误判成"固件挂了"
    try:
        from h723_w1 import revive_if_dead
        revive_if_dead(a.port)
    except Exception:
        pass

    # ★ P3 修复: timeout 0.05 → 0.25。旧值偏紧, 本机实测偶发读不到响应 ——
    #   那会把"工具太急"误报成"协议链路不活", 与"配置全对≠功能可用"同族的判据噪声。
    with serial.Serial(a.port, 115200, timeout=0.25) as ser:
        L = Link(ser, False)
        time.sleep(0.3)
        sts, _ = L.xact(0x01)
        if sts != 0:
            print("!! 协议链路不活 (COM 口), 无法触发 —— 先解决这个")
            return 2
        # ★ P3 修复: 补 T0c 工具自检 (与 h723_modbus.py 同款闸门) ——
        #   未实现命令必须回 NAK; 否则"sts 恒 0 / 判据恒真"这类**工具侧** bug 会让
        #   后续所有判据假通过 (本项目反复吃亏的那一族)。
        s_bad, _ = L.xact(0x7F)
        if s_bad != 0xFF:
            print("!! T0c 工具自检失败: 0x7F 应回 NAK, 实得 sts=%s ⇒ 判据不可信" % s_bad)
            return 2
        print("  [PASS] T0 链路活性 + T0c 工具自检 (未实现命令 0x7F → NAK)")
        L.xact(CMD_RESET); time.sleep(0.2)

        # ★★ 先把 40065/40066 写成**已知值**, 否则读到的是 RESET 后的 0,
        #    "期望响应帧"就无法预先确定 —— 而一个无法预先确定的期望值
        #    只能退化成"结构匹配", 那就丧失了"可失败判据"的强度。
        #    此处先在**缓冲模式**下写入 (响应不占 PA2 波形), 再切物理口。
        wr = mb_frame(1, 0x10, struct.pack(">HHB", 40065, 2, 4)
                      + struct.pack(">HH", 0x1111, 0x2222))
        L.xact(CMD_MB_INJECT, wr); time.sleep(0.08)
        L.xact(CMD_MB_RESP)                      # 取走写入的回显, 清缓冲

        # 切物理口发送 (响应从 PA2 真发)
        s, r = L.xact(CMD_MB_CFG, bytes([1, 1]))
        print("0x62 [1][1] → %s (src=%d tx_uart=%d)"
              % ("ACK" if s == 0 else "NAK", r[0] if len(r) > 0 else -1,
                 r[1] if len(r) > 1 else -1))

        req = mb_frame(1, 0x03, struct.pack(">HH", 40065, 2))
        # ★ 期望的是**响应帧**, 不是请求帧 —— 第一版这里搞错了, 拿 req 去匹配
        #   响应流, 必然为 0 (而当时判据③的 CRC 核对已全过, 说明固件本来是对的)。
        want = bytearray([1, 0x03, 4])
        want += struct.pack(">HH", 0x1111, 0x2222)
        c = mb_crc16(bytes(want))
        want += bytes([c & 0xFF, (c >> 8) & 0xFF])
        want = bytes(want)

        # ── 采集窗口 (定时), 期间重复注入 ──
        cap = tool("start_capture", {
            "deviceId": did,
            "logicDeviceConfiguration": {
                "logicChannels": {"digitalChannels": [0]},
                "digitalSampleRate": 16000000,
            },
            "captureConfiguration": {"timedCaptureMode": {"durationSeconds": a.secs}},
        })
        cid = cap.get("captureId", 1)
        print("captureId=%s, 采集 %.1fs 期间注入..." % (cid, a.secs))
        t0 = time.time()
        n_inj = 0
        while time.time() - t0 < a.secs - 0.4:
            # 每次注入前确认前一帧已被取走 (缓冲模式清空), 物理口模式无需
            L.xact(CMD_MB_INJECT, req)
            n_inj += 1
            time.sleep(0.25)
        print("注入 %d 次" % n_inj)

        try:
            tool("wait_capture", {"captureId": cid}, timeout=int(a.secs) + 30)
        except Exception as e:
            print("wait_capture: %s" % e)

        # ── 加 UART 解码器 ──
        an = tool("add_analyzer", {
            "captureId": cid,
            "analyzerName": "Async Serial",
            "analyzerLabel": "UART_PA2",
            "settings": {
                "Input Channel": {"numberValue": 0},
                "Bit Rate (Bits/s)": {"numberValue": 115200},
                "Bits per Frame": {"numberValue": 8},
                "Stop Bits": {"numberValue": 1},
                "Parity Bit": {"stringValue": "No Parity Bit (Standard)"},
            },
        })
        aid = an.get("analyzerId")
        print("analyzerId=%s" % aid)

        # ── 导出 (legacy + hex; 不用 export_data_table_csv —— 控制字符会丢) ──
        out = os.path.join(HERE, "_la_modbus.txt")
        if os.path.exists(out):
            os.remove(out)
        tool("legacy_export_analyzer", {
            "captureId": cid, "analyzerId": aid, "filepath": out, "radixType": 3,
        }, timeout=120)

        # 恢复缓冲模式 (测试隔离: 否则后续按缓冲读响应的套件全失败)
        L.xact(CMD_MB_CFG, bytes([1, 0]))
        L.xact(CMD_RESET)
        time.sleep(0.1)

    # ══════════ 核对 ══════════
    print("\n期望响应帧: %s   (请求 %s)" % (want.hex(), req.hex()))
    if not os.path.exists(out):
        print("!! 导出文件不存在 —— 可能没有解码出任何字节 (没接线 / 没信号)")
        return 1

    raw = open(out, encoding="utf-8", errors="replace").read()
    lines = [l for l in raw.splitlines() if l and not l.lower().startswith("time")]
    print("LA 解码出 %d 个 UART 字节" % len(lines))

    # 解析 hex 列 + 错误列
    toks, errs = [], []
    for l in lines:
        parts = [p.strip() for p in l.split(",")]
        if len(parts) >= 2 and parts[1].startswith("0x"):
            toks.append(int(parts[1], 16))
        if len(parts) >= 3 and (parts[2] or parts[3] if len(parts) > 3 else ""):
            if (len(parts) > 2 and parts[2]) or (len(parts) > 3 and parts[3]):
                errs.append(l)

    print("\n── 判据 ──")
    ok_all = True

    print("  [%s] ① LA 解出字节 > 0 (探针确实接在 PA2 且固件在发)"
          % ("PASS" if toks else "FAIL"))
    ok_all &= bool(toks)
    if not toks:
        print("      ⇒ 0 字节。先查: 探针是否真在 PA2? 共地? 0x62 是否生效?")
        return 1

    # ② 在字节流里找期望的**响应帧** (多次注入 → 多份)
    want = list(want)
    hits = 0
    i = 0
    while i + len(want) <= len(toks):
        if toks[i:i + len(want)] == want:
            hits += 1
            i += len(want)
        else:
            i += 1
    print("  [%s] ② 波形中匹配期望响应帧 %s 的次数 = %d"
          % ("PASS" if hits > 0 else "FAIL", want.hex() if isinstance(want, bytes) else bytes(want).hex(), hits))
    ok_all &= hits > 0

    # ③ 每份响应 CRC 独立复算
    crc_ok = 0; crc_tot = 0
    j = 0
    while j + 9 <= len(toks):
        if toks[j] == 1 and toks[j + 1] == 0x03 and toks[j + 2] == 4:
            frame = bytes(toks[j:j + 9])
            crc_tot += 1
            if mb_crc16(frame[:-2]) == (frame[-2] | (frame[-1] << 8)):
                crc_ok += 1
            j += 9
        else:
            j += 1
    print("  [%s] ③ 响应帧 CRC 独立复算: %d/%d 通过"
          % ("PASS" if (crc_tot and crc_ok == crc_tot) else "FAIL", crc_ok, crc_tot))
    ok_all &= bool(crc_tot) and crc_ok == crc_tot

    # ④ Parity/Framing Error 必须为空
    print("  [%s] ④ 无 Parity/Framing Error (时序/配置正确)"
          % ("PASS" if not errs else "FAIL"))
    ok_all &= not errs

    print("\n结果: %s" % ("全绿" if ok_all else "有 FAIL"))
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
