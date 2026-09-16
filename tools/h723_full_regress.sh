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
PORT="${DCL_PORT:-COM21}"
LOG=/tmp/full_regress.log
: > "$LOG"

say() { echo "$@" | tee -a "$LOG"; }

say "════════ 全量回归 $(date '+%F %T') ════════"

# ── 0. 板子回 bench 态 ──
say "── 0. bench 态准备（seq --wipe + ERASE + 复位）──"
timeout 180 python tools/h723_seq.py --wipe 2>&1 | tail -1 | tee -a "$LOG"
python - <<'PY' 2>&1 | tail -2 | tee -a "$LOG"
import sys, os, struct, time
sys.path.insert(0, "tools")
from h723_client import Dcl
d = Dcl(os.environ.get("DCL_PORT","COM21"))
sts, p = d.send(0x49)                       # PROG_ERASE: 清掉 SD 上的程序 ⇒ 上电走 bench profile
print("0x49 PROG_ERASE ->", sts)
d.send(0x13); time.sleep(0.5)               # 运行态复位
d.close()
PY
pyocd reset -t stm32h723xx 2>&1 | tail -1 | tee -a "$LOG"
sleep 2
python - <<'PY' 2>&1 | tail -1 | tee -a "$LOG"
import sys, os, struct
sys.path.insert(0, "tools")
from h723_client import Dcl
d = Dcl(os.environ.get("DCL_PORT","COM21"))
sts, p = d.send(0x38)
nr = struct.unpack("<H", p[20:22])[0] if sts == "ACK" and len(p) >= 22 else -1
cap = struct.unpack("<H", p[2:4])[0] if sts == "ACK" and len(p) >= 4 else 0
print("bench 回读: 0x38 len=%d n_routes=%d cap=0x%04X" % (len(p), nr, cap))
d.close()
PY

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
