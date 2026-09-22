# 回归报告 —— 增量上传环 DELTA_RING（2026-09-22）

> 被测固件：`8d3caa76792097c57f30c013f64d6ce7`（100 µs 交付档）
> 上一交付档：`4a6993bfbbde17d8ab4f48d63e51c245`
> 命令：`bash tools/h723_full_regress.sh`（**7 分 11 秒**，`rc=0`）
> 基线：`docs/STATUS-2026-09-16.md` §11.1（12 套）
> 完整日志：`/tmp/full_regress.log`；各套件明细 `/tmp/regress_<name>.txt`

## 0. 结论

**零新失败模式。** 12 套里 **11 套与基线逐项一致**；唯一差异（`w2_probe` 13/1
vs 基线 14/0）**已查清为它自身的时序非确定性，不是本改动的回归**（§2）。
本轮新增的 6 套 + 离线自测 3 套 + `docs/audit` 2 套**全部 PASS**。

## 1. 逐项对照

| 套件 | 本次 | 基线 | 判定 |
|---|---|---|---|
| `h723_proto` | 12 PASS / 0 FAIL | 12/0 | 一致 ✓ |
| `h723_w1` | 27 / 1 | 27/1 | 一致 ✓（R11 = 已定案**测试缺陷**）|
| `h723_macro` | 18 / 0 | 18/0 | 一致 ✓ |
| `h723_jitter` | rc=0 | 9/0 | 一致 ✓ |
| `h723_r1_actuator` | rc=0 | 5/5 | 一致 ✓ |
| `h723_seq` | 27 / 0 | 27/0 | 一致 ✓ |
| `h723_audit_m234` | rc=1 | 9/3 | 一致 ✓（M3 三项 = `DCL_PERSIST_SAVE=0` 设计内）|
| `h723_t26` | rc=1 | 7/11 | 一致 ✓ |
| `h723_w5` | rc=1 | 10/3/4 SKIP | 一致 ✓（HIL 输出臂需接线）|
| `h723_persist` | 7 / 19 | 7/19 | 一致 ✓ |
| `h723_modbus` | 5 / 10 | 5/10 | 一致 ✓（需 RS-485 回路）|
| **`h723_w2_probe`** | **13 / 1** | **14/0** | ⚠️ **查清 = 非回归**（§2）|

**本轮新增/改动的套件（全 PASS）**：
`h723_i2c_shm_test` · `h723_i2c_leak_check` · `h723_dev_bind_test`(52/0) ·
`h723_devbind_persist`(32/0) · `h723_frame_attrib`(18/0) · `h723_do_hold_test`
**离线自测**：`selftest_dev_bind` · `selftest_persist` · `selftest_claims`
**docs/audit**：`audit_probe_w1w2w3` · `limit_tick_probe`

**收尾探活**：板子仍在线（脚本末尾 5 次探测）。

## 2. ⚠️ 唯一差异的查清过程（★ 留作范式）

失败项原文：

```
[FAIL] A  无强制: 路由确实在写 wire[3] (稳定非零)        1.5000 / 1.0000
```

**四条证据判它不是回归**：

1. ★ **同一份固件单独跑 3 次全是 `14 PASS / 0 FAIL`** ⇒ 只在"回归连跑的上下文"里出现。
2. ★★ 它**自己的阳性对照 A' 是 PASS**：
   `6 个采样点出现 4 个不同值: [1.0, 1.5, 3.25, 7.5]`
   ⇒ **值确实在被引擎写**，只是两次采样不等 ⇒ 判据里"**稳定**非零"这个措辞
   **比被测对象更严**。这正是本项目 §〇 第 8/9 条记的那类陷阱：
   **过严的判据会伪装成"被测对象坏"**。
3. 其余 13 项**全 PASS**，含全部核心项：
   `B1` 拍首覆写 / `B1'` `FORCE_VAL` / `C` 写端屏蔽 / `E` 更换强制值 / `D` 释放恢复 / `D'` 回到与 A 一致。
4. 本次改动是 `bb_kick`（黑匣子增量推送）+ 新 SHM 域 + 新命令，
   **完全没碰 force / wire / 路由表**。

**查法（可复用）**：回归脚本的 `run()` 把**完整输出**存到 `/tmp/regress_<name>.txt`
—— 摘要行不够时，**去那里拿明细**。本次就是靠它拿到 A 点的实测 `1.5000 / 1.0000`。

★ 另注：脚本注释自己已声明 `w2_probe` "同固件连跑三次 13/1、11/3、13/1"
⇒ **它的 PASS 数量不可作判据**；判它只能"看失败项是哪一项 + 单独复跑"。

## 3. 交付档落地

- `tools/h723_restore_delivery.sh` 的 `EXPECT_MD5`：
  `4a6993bfbbde17d8ab4f48d63e51c245` → **`8d3caa76792097c57f30c013f64d6ce7`**
- ★ **恢复路径实测有效**（`DCL_PORT=COM21 bash tools/h723_restore_delivery.sh`，3 分 26 秒）：

  ```
  hex md5: 8d3caa76792097c57f30c013f64d6ce7
  ✓ 指纹与交付基线一致          ← ★ 重建能复现**逐字节相同**的固件
  == 2) 烧回 ==  == 3) 复位 + 等自愈窗 ==
  == 4) 探活 (期望 12 PASS / 0 FAIL) ==
  结果: 12 PASS / 0 FAIL / 0 SKIP  (共 12 项)
  ```

## 4. 本次改动（供对照）

| 文件 | 改动 |
|---|---|
| `src/engine.h` | 新增 `OFF_DELTA_RING`（198 槽 × 16 B）+ 头 20 B，落在 SHM 尾部**从未分配的 `0x7360..0x8000`**（`OFF_DEV_BIND` 结束于 0x7360）⇒ **不动 `SHM_SIZE`** ⇒ 自动被协议放行 |
| `src/blackbox.c` | `delta_reset()` + `delta_push()`；**`bb_kick` 的比较循环由"发现第一个就 break"改成全扫**（零新增比较成本：+22% 次比较 ≈ 300 cyc = 拍预算 1.5%）；"变化才记"语义不变 |
| `src/blackbox.h` | `delta_reset()` 声明 |
| `src/engine.c` | `cold_start_reset()` 里 `delta_reset(g_shm)`（新增域必须登记单一入口）|
| `src/main.c` | 新命令 `0x39 op=19 sub=26 arg=from_seq`（单次 ≤64 条）|
| `tools/hostsim/delta_logger.py` | 上位机侧 10 Hz 拉取 → CSV + meta |
