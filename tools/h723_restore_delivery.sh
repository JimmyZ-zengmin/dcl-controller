#!/usr/bin/env bash
# 把板子恢复成交付档 —— **实验纪律**: 不许把实验档留在板子上
#
# 为什么单独一个脚本: 实验档（`BOOT_SEL=0` 等）会让板子的行为与交付**不同**,
# 而"板子上现在是哪一档"没有任何协议手段能读出来 ⇒ 只能靠**流程**保证。
# 本脚本把这个流程固化: 重建默认档 → 烧回 → 复位 → 等自愈窗 → 探活。
#
# ★ 用法: bash tools/h723_restore_delivery.sh
# ★ 探活用 tools/h723_proto.py（期望 **12 PASS / 0 FAIL**）
# ★ 自带 PATH 导出 —— 见 RULES-DETAIL §5.38
export PATH="/usr/bin:/bin:/mingw64/bin:/c/Windows/System32:$PATH"
cd "$(dirname "$0")/.." || exit 9
export DCL_PORT="${DCL_PORT:-COM22}"

echo "== 1) 重建默认档 =="
# ★★★ 2026-09-18（实测踩到）: **CMake 的选项是粘性缓存** —— `bash build.sh -DDCL_TICK_US=200`
#   之后, 再跑**无参**的 `build.sh` 仍然会构建 **200 µs 档**（缓存里存着 200）。
#   后果实测: 本脚本产出了 **200 µs 的"交付档"**, 而它照样打印 hex md5、照样探活 **12 PASS / 0 FAIL**
#     —— **探活判不出拍长**。是 `exp_eq_dt_semantics.py` 的 Q0a（tick 速率 vs 上位机墙钟）
#     把这件事抓出来的（它期望 10000 Hz, 实测 ~5000）★ 判据抓到了流程抓不到的东西。
#   ⇒ 两道修法:
#     ① 这里**显式传交付档的全部可粘性选项**（不靠默认值）;
#     ② 构建后**比指纹**（下面 EXPECT_MD5）⇒ 与基线不符就**大声失败**, 不烧。
DELIVERY_OPTS="-DDCL_TICK_US=100 -DDCL_BOOT_SEL=1 -DDCL_BOOT_SCAN_MODE=0 -DDCL_LOOP_RESET=1 -DDCL_STEP_RAMP_FIX=1"
EXPECT_MD5="fb6a0310ab9fcae1216b0f52e84bc097"     # ★ 100 µs 交付档基线（改部署期代码必然更新）
# ★★★ 2026-09-22（第四次）更新: 26b82ae15ba8719e7bdcedd6e373feef → fb6a0310ab9fcae1216b0f52e84bc097
#   原因 = **给 `UART_TX_PUMP_US` 补 CMake CACHE 声明 + `-D` 转发**（把它从"改源码才能调"
#          变成"可 `-D` 做 A/B 的旋钮"）。**源码逻辑一字未改**，宏值也完全一样（都是 10000u）。
#
#   ★★★ **本次顺带查清了一条工具链特性（重要）**：
#     **hex 只在「CMake 配置完全一致」时可复现。**
#     · 实测（正向）：同一份源码 + 同一组交付档选项，**两次真正的全量构建 ⇒ hex 完全一致**
#       （`fb6a0310…` ×2）⇒ **指纹机制本身是成立的**。
#     · 实测（反向）：只要 CMake **配置**变了 —— 哪怕只是新声明一个变量、命令行多一个 `-D`，
#       而**宏值一模一样** —— **hex 就会变**。原因是 ninja/链接的布局受配置影响
#       ⇒ 符号地址重排 ⇒ 字节不同，但**功能等价**。
#     ⇒ 纪律: **凡动 `CMakeLists.txt`（含新声明）⇒ 必须同步更新 `EXPECT_MD5`**；
#       ★ 并且**不能用"hex 没变"来证明"只改了声明"** —— 它是**会变**的。
#
#   ★★ **一次差点误判（判据要能失败）**：第一次做这个"可复现性"实验时，
#     `bash build.sh clean` 里的 `rm -rf build` 被**环境的 safe-delete 护栏拦下**，
#     而脚本**继续往下跑**并报 `ninja: no work to do` —— 于是两次"构建"其实是**同一个产物**，
#     差一点把"增量空转"读成"可复现"。
#     ⇒ 修法：**先把 `build/` 用 `mv` 移走**再构建，并**确认日志里真的在编译**（不是 no work to do）。
#     ★ 同族于 §〇 第 8 条：**判据必须能失败**，而"没编东西"的构建是**假证据**。
# ★★★ 2026-09-22（第三次）更新: 8c323959f1a1e4bd33275aad94034f04 → 26b82ae15ba8719e7bdcedd6e373feef
#   原因 = **TX 泵限时 1 ms → 10 ms**（`src/uart.h` 的 `UART_TX_PUMP_US`）
#          —— 为消除「**运动态上传丢 ~20%**」。
#
#   证据链（全部可复现，判据见 `docs/analysis-2026-09-22/ab_drop.py`）：
#     · **症状**（长稳分析，`docs/analysis-2026-09-22/report.html`）：
#       板子连续跑 4.94 h 后实测 —— 静止态 `Δdrop=0`，**运动态丢 95~97 条/s（21.6%）**。
#     · **量纲修正**（差点搞错）: `evt_rate` 是「**我读到的**条数/s」（消费率），不是产出率。
#       判据 `evt_rate/rate = 327/13.7 = 23.9 ≈ DELTA_READ_MAX(24)` ⇒ 每轮**读满了**
#       ⇒ 产出 = 消费 + 丢弃 = **441 条/s**，而消费上限 = 13.7 × 24 = **329** ⇒ 必丢。
#     · **真因**（不是批长）: 单轮里 `sub=26`（24 条 = 400 B）实测要 ~77 ms，
#       反代 `吞吐 = 11.5·P/(P+C)` 得 **C ≈ 1.21 ms**（主循环周期远大于 0.37 ms）
#       ⇒ 1 ms 的泵预算被"每圈固定代价"吃掉一半以上 ⇒ 有效吞吐只 **5.2 KB/s**（线路的 45%）。
#       ★ 而"加大批长"这条路**已实测堵死**：`DELTA_READ_MAX` n=16/24/32 = 281/297/283（非单调），
#         n=64 单次 198.7 ms ⇒ 只有 322 条/s —— 原因正是**同一个泵速率**。
#     · **修法**: `P: 1 → 10 ms` ⇒ 吞吐 `11.5×10/(10+1.21) ≈ 10.3 KB/s`（线路的 90%）。
#     · **实测结果**（同一条 `ab_drop.py`，60 s 窗口，运动态）:
#         轮询率 **14.2 → 34.7 Hz**（+144%）· 每轮读走 24.3 → **8.8 条**（不再读满）
#         · 消费上限 329 → **833 条/s** · **Δdrop 95.3 → 0 条/s** ✓
#         · 双判据 PASS（正判据 Δdrop==0；反向判据 ap 中位 375 Hz ⇒ 确实在运动）
#     · **已知副作用（必须记账）**: 主循环每圈多阻塞 9 ms ⇒ **编码器采样率 333 → 251 条/s**。
#       权衡：改前是"333 Hz 但丢 20%"（有效 ~266 且**有随机空洞**），改后是
#       "**251 条/s 完整无洞**" ⇒ 对"计算必须用全量数据"而言**完整 > 峰值**。
#       ★ 若要收回采样率：把 P 降到 5 ms（吞吐仍 ~10.7 KB/s，够用），属可调旋钮。
#     · **代价核算**: 主循环单次阻塞 1 → 10 ms（已知最大阻塞是 SD 落盘 46.7 ms ⇒ 10 ms 是它的 21%）；
#       实测主循环最大间隔 **687 拍 = 0.069 s**（改前 1135 拍），看门狗阈值 1.2 s ⇒ 余量 17×。
#       ★ 拍内实时路径不在主循环（`step_service_motion()`/`step_tick_isr()` 都在 ISR）⇒ 主循环阻塞不影响拍。
#
#   ★★ **12 套回归已跑（2026-09-22 19:00，5m48s）: 零新失败模式** ——
#     `proto 12/0` · `w2_probe 14/0` · `seq 27/0` · `macro 18/0` · `r1_actuator`/`jitter` rc=0
#     · `w1 27/1`（R11 = 脚本头部标注的**已知测试缺陷**）· `persist 7/19`（**与基线完全一致**）
#     · `audit_m234`/`t26`/`w5`/`modbus` rc=1（全是脚本头部列出的**设计内失败**）
#     · G6 系列 i2c×4 全 PASS · `dev_bind 52/0` · `devbind_persist 32/0` · `frame_attrib 18/0`
#     · 收尾探活 **5/5**。
#     ★ 判据是"**有没有出现新的失败模式**"，不是"PASS 数不低于某值"（本项目铁律）。
#   ★★ 仍挂账: **DMA 发送**（根治：零 CPU/零中断/吞吐=线路极限）；
#      `UART_TX_PUMP_US` 变成可 `-D` 的 A/B 旋钮（需在 `CMakeLists.txt` 补 CACHE 声明）；
#      若要收回编码器采样率 ⇒ 把 P 降到 5 ms（吞吐仍 ~10.7 KB/s，够用）。
# ★★ 2026-09-22 更新: 8cf12db21f57a14e0cbd40d5bdc23d08 → 8c323959f1a1e4bd33275aad94034f04
#    原因 = **TX 限时 pump + 增量流调参**（为了让"全量数据做实时演示"不丢）:
#      · `src/uart.c`: `uart1_tx_pump()` 改成**限时**推进（`UART_TX_PUMP_US=1000`）。
#        第一版"只试一次"的缺陷: 推完 1 字节 TXE 要 86.8 µs 才再置位 ⇒ **每圈只推 1 字节**
#        ⇒ 吞吐绑到主循环率 0.37 ms/字节 ⇒ 1040 B 要 230 ms（线路只该 90 ms）。
#        限时后 → 198.7 ms；而单次阻塞仅 1 ms（vs 原阻塞版 90 ms）。
#      · `src/uart.h`: 新增 `UART_TX_PUMP_US`；`src/uart.c` include `timebase.h`（tb_cyc/TB_US）。
#      · `src/engine.h`: `DELTA_READ_MAX` 64 → **24**（实测 24 是消费能力最优 297 条/s；
#        16/32 分别只有 281/283 ⇒ 瓶颈是"协议往返+主循环占用"的固定代价）。
#      · `src/engine.h`: `DELTA_MASK_DEF_LO` **再关掉槽 1（deg）** —— 它与 raw 一一对应
#        （deg=raw*360/4096）⇒ 传两份纯冗余 ⇒ 关掉**信息零损失**，产出 315→218 条/s。
#        ⇒ 关掉的槽 = [1, 8, 9, 10, 25]（掩码 0xFDFFF8FD）。
#    ★ 实测（运动中 1500 Hz，8 s）: 8 s 内 drop **478 → 1**（首次拿满环的边界）；
#      消费 242 条/s vs 产出 218 条/s ⇒ **余量 1.11×**。
#    ★ 回归: 12 套**逐项一致**（含 `w2_probe` **14/0** —— `--pre-ms` 时间窗修复生效）。
#    ★ DTCM 因 TX 队列 8 KB 涨到 66.23 KB / 51.7%（限内；账本闸门已过）。
# ★★ 2026-09-22 更新: 8d3caa76792097c57f30c013f64d6ce7 → 8cf12db21f57a14e0cbd40d5bdc23d08
#    原因 = **TX 由"逐字节死等"改成"入队 + 每圈泵"**（治"观测改变被测对象"）:
#      · `src/uart.c`: TX 线性队列（UART_TXQ_SZ=8192，定义在 uart.h）+ `uart1_write` 三段
#        （推完残余 / 拷贝入队 / 尽量推不阻塞）+ `uart1_tx_pump()` + `uart1_tx_pending()`
#      · `src/main.c`: 主循环**第一节**调 `uart1_tx_pump()`；
#        新增 `_Static_assert(FRAME_TOTAL_MAX_V2 <= UART_TXQ_SZ)`（装不下 ⇒ 构建红）
#      · `src/uart.h`: 导出 `UART_TXQ_SZ`（唯一源，供上面的断言）
#    ★ 实测收益（同一个对照实验，判据能失败）:
#        改前: 不读 sub=26 → 91.1 Hz ; 读 sub=26 → 60.4 Hz（掉 34 个百分点）
#        改后: 不读 → 93.9 Hz ; 读 → 92.8 Hz（掉 1.1 个百分点）
#      ⇒ **观察者效应基本消除**；协议探活 h723_proto 12 PASS / 0 FAIL
#    ★ 回归: **12 套零回归**（唯一差异 w2_probe 已查清 = 它自身的**时间窗缺陷**:
#        写 g_reinit 后只等 4 ms，而主循环最长阻塞(SD 落盘)是 46.7 ms ⇒ 偶发 表=0。
#        已修: 新增 `--pre-ms`(默认 150) 只加长 PRE 那一步；修后连跑 5 次全 14/0）
# ★★ 2026-09-22 更新: 4a6993bfbbde17d8ab4f48d63e51c245 → 8d3caa76792097c57f30c013f64d6ce7
#    原因 = **新增"增量上传环" DELTA_RING**（`数据变化全量上传电脑记录`）:
#      · `engine.h` : `OFF_DELTA_RING`（198 槽 × 16 B）+ 头 20 B，落在 SHM 尾部
#        **从未分配的 0x7360..0x8000**（`OFF_DEV_BIND` 结束于 0x7360）⇒ **不动 SHM_SIZE**
#        ⇒ 自动被协议放行（不用 pyocd、不用开 AXI 只读窗）。
#      · `blackbox.c`: `delta_reset()`（掩码登记）+ `delta_push()` +
#        **`bb_kick` 的比较循环由「发现第一个就 break」改成全扫** ⇒ 零新增比较成本
#        （实测 +22% 次比较 ≈ 300 cyc = 拍预算 1.5%）；"变化才记"语义不变。
#      · `engine.c` : `cold_start_reset()` 里 `delta_reset(g_shm)`（新增域必须登记单一入口）。
#      · `main.c`   : 新命令 `0x39 op=19 sub=26 arg=from_seq`（单次 ≤64 条）。
#    实测（本轮）: 静止 39 条/s · 运动 227~312 条/s · 带宽 3.55 KB/s = 115200 的 31%
#      · 稳态 `dropped=0` ⇒ ★ "全吃"成立（消费能力 640 条/s > 需求 312 条/s）。
#    ★ 回归: **12 套零回归**（逐项对照 `docs/STATUS-2026-09-16.md` §11.1 基线；
#      唯一差异 `w2_probe` 13/1 已查清 = 它**自身时序非确定**：同固件单独跑 3 次全 14/0，
#      且失败项 A 的阳性对照 A' PASS ⇒ 判据里"稳定非零"措辞过严，不是代码回归）。
# ★★ 2026-09-21 更新: c2ca850bbb019166e3d2d0821b175574 → 4a6993bfbbde17d8ab4f48d63e51c245
#    原因 = **运动控制的下半段搬进拍内**（step_service_motion / step_tick_isr）:
#      原来 到点自停 + 斜坡推进 + 限时截止 都由**主循环**调 ⇒ 有效控制周期 = 主循环周期,
#      实测 `g_step_dt_max` = 387 拍 = 38.7 ms,而拍长只有 100 µs（387×）。
#      搬进拍内后实测 `g_step_dt_max` = **1 拍**（dt 恒为 1 ⇒ 确实每拍在跑）。
#    伴随改动: `src/step.c` 新增 `DCL_ITCM` 标记 10 处（闭包由 gate_isr_itcm.py 自动抓出:
#      它先报 apply_rest_ena / step_rate_apply 两个落在 flash,补上即过）。
#      ITCM 占用 13 636 → 15 276 B,到向量表仍余 75.1%。
#    A/B: 复用既有 `-DDCL_IO_IN_ISR=0`（对照档 = 改前行为，运动也由主循环驱动）。
# 变更记录（每次改基线都必须写清"为什么"）:
#   c7c366c1… → 6daa7e65…  阶段 2.1/2.2：顺序域清除语义 + 「走 N 步」两个拍号
#   6daa7e65… → 44ce8cbb…  内存宪法 A+D 期：新增 src/mem_stat.c + 栈水位可观测量，
#                          并在 manifest 里加 MEM_STAT 条目（.rodata +20 B）
#   44ce8cbb… → c2ca850b…  LUT 方案 A：新增 src/lut_seg.c（表随 deploy 走）+ 1 KB pending 表
#                          (.bss +1 KB ⇒ SHM 基址后移) + 0x48 尾部追加 LUT 段字段（104 → 112 B）
# ★★★ 2026-09-18 第二次实测踩到（同一个机制的另一半）: 上面这行**忘更新**过一次 ——
#   812b687（E-U）把它设成 c7c366c1…, 而**下一天 2.1/2.2 动了部署期代码**
#   （顺序域清除 + `sub=16` 加两个拍号）⇒ hex 变成 6daa7e65…, 基线却还是旧的
#   ⇒ 本脚本此后**每次都中止**（fail-closed, 方向是安全的, 但**恢复路径已死**）。
#   ★ 教训: "比指纹"只挡住"**改动了却不说**", 挡不住"**基线忘了更新**" ——
#     期望值是**第二处**需要维护的地方。⇒ 所以下面把失败信息做成**可判断**的:
#     打印"最近一次改动 src/ 的提交", 让人一眼分清"预期改动, 该更新基线"与"不该变的却变了"。
echo "  显式交付档选项: $DELIVERY_OPTS"
bash build.sh $DELIVERY_OPTS > /tmp/h723_restore_build.log 2>&1
RC=$?; echo "  BUILD_RC=$RC"
if [ "$RC" -ne 0 ]; then
    echo "  ❌ 构建失败 ⇒ **中止, 不要烧旧镜像**"
    tail -20 /tmp/h723_restore_build.log
    exit "$RC"
fi
grep -E "^DCL_(BOOT_SEL|BOOT_PROFILE|TICK_US|LOOP_RESET|STEP_RAMP_FIX):" build/CMakeCache.txt
GOT_MD5="$(md5sum build/dcl_h723.hex | cut -d' ' -f1)"
echo "  hex md5: $GOT_MD5"
if [ "$GOT_MD5" != "$EXPECT_MD5" ]; then
    echo "  ❌ **指纹与交付基线不符** ⇒ 这**不是**交付档（粘性选项? 源码改动?）"
    echo "     期望 $EXPECT_MD5"
    echo "     实测 $GOT_MD5"
    echo "  ── 判断材料（**这一行就是为了让人分得清两种情况**）──"
    echo "     最近一次改动 src/ 的提交: $(git log -1 --format='%h %ad %s' --date=short -- src/ 2>/dev/null)"
    echo "     最近一次改动 src/ 的文件: $(git log -1 --format='%h' -- src/ 2>/dev/null) 之后被改的部署期文件名:"
    git diff --name-only "$(git log -1 --format='%h' -- src/ 2>/dev/null)^" -- src/ 2>/dev/null | sed 's/^/       /'
    echo "     · 若那是**有意的**部署期改动 ⇒ 更新本脚本 EXPECT_MD5（并在提交信息里写清新 hex）"
    echo "     · 若**不该变** ⇒ 查粘性选项（build/CMakeCache.txt）与未提交改动（git status）"
    echo "     ⇒ **中止, 不烧**。"
    exit 3
fi
echo "  ✓ 指纹与交付基线一致"

echo
echo "== 2) 烧回 =="
pyocd flash -t stm32h723xx -O connect_mode=under-reset build/dcl_h723.hex 2>&1 | tail -3
echo
echo "== 3) 复位 + 等自愈窗 (~12 s) =="
pyocd reset -t stm32h723xx 2>&1 | tail -2
sleep 12
echo
echo "== 4) 探活 (期望 12 PASS / 0 FAIL) =="
python tools/h723_proto.py --port "$DCL_PORT" 2>&1 | tail -5
