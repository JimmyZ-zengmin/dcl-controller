#!/usr/bin/env bash
# h723_full_regress.sh —— **一键全量回归**（2026-09-16 收成产物；原来是 build/ 下的一次性脚本）
#
# 用法:
#   bash tools/h723_full_regress.sh              # 默认 COM21
#   DCL_PORT=COM7 bash tools/h723_full_regress.sh
#
# ★ 看什么: **"有没有出现新的失败模式"，不是"PASS 数不低于某值"**（本项目铁律）。
#   基线数字在 docs/STATUS-2026-09-16.md §11（12 套）与本文件末尾。
# ★ 串口独占: 本脚本串行跑，期间**不要**并发任何占口的东西（并发会让两边都读到串帧）。
# ★ 已知的"设计内失败"（**不是回归**）:
#   · `h723_persist` / `h723_t26` / `h723_audit_m234` 的落盘项 —— 内部 flash 持久化已按设计降级
#     （`DCL_PERSIST_SAVE=0`: 擦 flash 会让拍 ISR 卡死 210ms ⇒ 看门狗复位）
#   · `h723_modbus` —— 需要 485 回路（没接线 ⇒ `resp=None`）
#   · `h723_w5` 的 H-* —— HIL 输出臂（需要接线/示波）
#   · `h723_w1` R11 —— 已知测试缺陷
#   · `h723_w2_probe` **自身非确定**（同固件连跑三次 13/1、11/3、13/1）⇒ 看"新失败模式"，别看数量
# ★ 大 `0x10 DEPLOY`（~2-4KB）有**已知偶发"停答"**（会自愈；见 STATUS §17）⇒ 脚本末尾会探活。
# ★ 纪律（memory §九）：
#   · 板子先回 bench 态（0x49 ERASE + 复位 + seq --wipe）
#   · **串口独占**：本脚本串行跑，中途不并发任何东西
#   · 判据看"有没有出现新的失败模式"，不是"PASS 数不低于某值"
# 用法: bash build/_full_regress.sh   → 结果落 /tmp/full_regress.log
set -u
cd "$(dirname "$0")/.."
export PATH="/usr/bin:/bin:/mingw64/bin:/c/Windows/System32:$PATH"
# ★★★ 2026-09-17: 默认口**自动找 CH340**，而不是硬编码 COM21 ——
#   USB 重插/换口是常态（今天两次踩到：板子从 COM21 变成 COM22，
#   于是所有命令发到不存在的口 ⇒ `0x49` 失败 ⇒ 前置闸门判"无效"早退，
#   而现象看起来像"bench 建不起来"）。显式 `DCL_PORT=COMxx` 仍可覆盖。
PORT="${DCL_PORT:-}"
if [ -z "$PORT" ]; then
  PORT=$(python -c "import sys; sys.path.insert(0,'tools'); from h723_client import find_board; print(find_board())" 2>/dev/null)
fi
if [ -z "$PORT" ]; then echo "✗ 找不到 CH340（用 DCL_PORT=COMxx 显式指定）"; exit 1; fi
echo "端口 = $PORT"
# ★★★ 2026-09-17: **必须 export** —— 下面的 python 块读的是 `os.environ.get("DCL_PORT","COM21")`，
#   只设 shell 变量不导出 ⇒ 它们仍在用硬编码的 COM21 ⇒ 换口后**全部失败**，
#   而现象看起来像"bench 建不起来"（实测：20 s 早退、只输出一行闸门）。
export DCL_PORT="$PORT"
LOG=/tmp/full_regress.log
: > "$LOG"

say() { echo "$@" | tee -a "$LOG"; }

say "════════ 全量回归 $(date '+%F %T') ════════"

# ── 0. 板子回 bench 态 ──
say "── 0. bench 态准备（seq --wipe + ERASE + 复位）──"
timeout 180 python tools/h723_seq.py --wipe 2>&1 | tail -1 | tee -a "$LOG"
# ★★★ 2026-09-17 重写: 原 step 0 只是"试着做"，失败也往下跑 ⇒ 25 套件整片红，
#   而那些红**不是回归**（是 bench 态没建立）。实测两条原因:
#     ① `pyocd reset` 之后有一段**已知的"偶发停答、会自愈"**窗口
#        (SUPPORTED-SCOPE §4 就写着"静置 ~60 s 历史上会自愈"; 本次 3/3 复现)
#     ② SD 卡未被识别（`g_sd_part_ok=0` / `g_sd_rca=0` / `g_sd_init_stage=5`）⇒ `0x49` NAK "erase failed"
#   ⇒ 现在把"前置条件"做成**会响的闸门**（本项目最忌**静默降级**）。
python - <<'PY' 2>&1 | tail -6 | tee -a "$LOG"
import os, struct, subprocess, sys, time
sys.path.insert(0, "tools")
from h723_client import Dcl
PORT = os.environ.get("DCL_PORT", "COM21")

d = Dcl(PORT)
# ★ 也重试: 命令通路 p99=56.8/max=63.6ms, 且 SD 路径可能阻塞主循环 ⇒ 一次超时不算数。
sts, p = None, b""
for k in range(3):
    sts, p = d.send(0x49)                  # PROG_ERASE: 清掉 SD 上的程序 ⇒ 上电走 bench profile
    if sts == "ACK":
        break
    time.sleep(1.5)
print("0x49 PROG_ERASE ->", sts, repr(p[:32]))
if sts != "ACK":
    print("!! SD 程序区擦不掉 ⇒ bench 态**建不起来**（下面直接判无效, 不再跑套件）")
    print("   查法: pyocd commander -t stm32h723xx --connect halt -c \"read32 <g_sd_*> 0x30\" -c go")
    print("   只看 g_sd_part_ok(应为 1); g_sd_rca / g_sd_init_stage 停在早期 ⇒ **卡没插好/没被识别**")
    d.close(); sys.exit(2)
d.send(0x13); d.close(); time.sleep(0.6)   # 运行态复位

# 全复位（让板子重新读 SD / 装载）。★ 此后**全程只走协议**（本项目工具链纪律）。
subprocess.run(["pyocd", "reset", "-t", "stm32h723xx"], capture_output=True, text=True)

# ★★ 耐心轮询: 上面那次 reset 之后有一条已知的**偶发停答、会自愈**窗口
#    ⇒ "一次读失败 ≠ 链路异常"（本项目铁律）⇒ 必须重试到 ~70 s, 不能一次就判死。
d = Dcl(PORT)
run, nr, last, t0 = 0, -1, "", time.time()
while time.time() - t0 < 70.0:
    sts, p = d.send(0x38)
    if sts == "ACK" and len(p) >= 23:
        run = p[22]                        # ★ r[22] = run（代码里唯一权威处）
        s2, p2 = d.send(0x20, struct.pack("<HB", 0x0E, 2))   # SHM OFF_CTRL_N_ROUTES (u16)
        if s2 == "ACK" and len(p2) >= 2:
            nr = struct.unpack("<H", p2[:2])[0]
        if run == 1:
            break
    last = sts
    time.sleep(2.0)
d.close()
print("bench 回读: run=%d ; SHM n_routes=%d  (期望 run=1 / n_routes=128)  用时 %.1fs"
      % (run, nr, time.time() - t0))
if run != 1:
    print("!! 链路/bench 仍未就绪（最后一次 0x38 = %s）" % last)
    sys.exit(3)
PY
RC=${PIPESTATUS[0]}
if [ "$RC" = "2" ]; then
  say ""
  say "★★★ 闸门: **SD 程序区不可用** ⇒ 本轮回归判 **无效**，不跑套件。"
  say "    ★ **不许**把『前置没满足』当成『回归失败』 —— 那些红不是回归。"
  exit 1
fi
if [ "$RC" != "0" ]; then
  say ""
  say "★★★ 闸门: **bench 态未建立**（run != 1，已耐心重试 70 s）⇒ 本轮回归判 **无效**。"
  say "    ① 查 SD 卡是否插好  ② SWD 读 g_sd_part_ok(期望 1)  ③ 修好后重跑"
  say "    ★ **不许**把『前置没满足』当成『回归失败』。"
  exit 1
fi
say "   ✓ bench 态已建立（run=1）"

# ── 1. 逐套跑（串行 + 独占串口）──
run() {   # run <名字> <命令行…>
  local name="$1"; shift
  printf '%-34s ' "$name" | tee -a "$LOG"
  local out; out=$(timeout 420 "$@" 2>&1)
  local rc=$?
  local line; line=$(echo "$out" | grep -E '结果:|PASS [0-9]+ / FAIL|摘要|^\[PASS\] G6|^\[PASS\] 参数非法|变异测试' | tail -1)
  [ -z "$line" ] && line="(无摘要行, rc=$rc)"
  echo "$line  [rc=$rc]" | tee -a "$LOG"
  echo "$out" > "/tmp/regress_${name//\//_}.txt"
}

say ""
say "── 1. 12 套基线 ──"
run h723_proto              python tools/h723_proto.py --port "$PORT"
run h723_w1                 python tools/h723_w1.py --port "$PORT"
run h723_w2_probe           python tools/h723_w2_probe.py
run h723_r1_actuator        python tools/h723_r1_actuator.py --port "$PORT"
run h723_macro              python tools/h723_macro.py --port "$PORT"
run h723_jitter             python tools/h723_jitter.py --port "$PORT"
run h723_seq                python tools/h723_seq.py
run h723_audit_m234         python tools/h723_audit_m234.py
run h723_t26                python tools/h723_t26.py --port "$PORT"
run h723_w5                 python tools/h723_w5.py --port "$PORT"
run h723_persist            python tools/h723_persist.py
run h723_modbus             python tools/h723_modbus.py --port "$PORT"

say ""
say "── 2. 本轮新增/改动的套件 ──"
run h723_i2c_sm_test        python tools/h723_i2c_sm_test.py --port "$PORT"
run h723_i2c_gate_test      python tools/h723_i2c_gate_test.py --port "$PORT"
run h723_i2c_shm_test       python tools/h723_i2c_shm_test.py --port "$PORT"
run h723_i2c_leak_check     python tools/h723_i2c_leak_check.py --port "$PORT"
run h723_dev_bind_test      python tools/h723_dev_bind_test.py --port "$PORT"
run h723_devbind_persist    python tools/h723_devbind_persist_test.py --port "$PORT"
run h723_frame_attrib       python tools/h723_frame_attrib_test.py --port "$PORT"
run h723_do_hold_test       python tools/h723_do_hold_test.py --port "$PORT"

say ""
say "── 3. 离线自测（不需要板子）──"
run selftest_dev_bind       python tools/h723_dev_bind_test_selftest.py
run selftest_persist        python tools/h723_devbind_persist_sim.py
run selftest_claims         python tools/ref_claims_check.py --selftest

say ""
say "── 4. docs/audit 下的（能跑的）──"
run audit_probe_w1w2w3      python docs/audit/audit_probe_w1w2w3.py
run limit_tick_probe        python docs/audit/h723_limit_tick_probe.py

say ""
say "── 5. 收尾：板子还活着吗（大 deploy 有已知偶发'停答'缺陷，见 STATUS §17）──"
python - <<'PYEOF' 2>&1 | tail -2 | tee -a "$LOG"
import sys, os, time
sys.path.insert(0, "tools")
from h723_client import Dcl
d = Dcl(os.environ.get("DCL_PORT","COM21")); n = 0
for _ in range(5):
    if d.send(0x01)[0] == "ACK": n += 1
    time.sleep(0.5)
print("收尾探活: 5 次中应答 %d 次" % n)
if n == 0:
    print("⇒ 停答（已知偶发现象）: 先等 60 s 再试; 仍无 ⇒ pyocd reset -t stm32h723xx")
d.close()
PYEOF

say ""
say "════════ 结束 $(date '+%F %T') ════════"
