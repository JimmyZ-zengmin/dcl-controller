#!/usr/bin/env bash
# build.sh — H723 工程一键构建
#
# 用法:  bash build.sh                 # 默认构建
#        bash build.sh clean           # 清掉 build/ (含 CMakeCache)
#        bash build.sh -DDCL_BOOT_GATE=1 -DDCL_BOOT_PROFILE=1
#
# 依赖 (本机已有, 无需安装):
#   cmake  4.0.3   C:/Espressif/tools/cmake/4.0.3/bin/cmake.exe
#   ninja  1.12.1  C:/Espressif/tools/ninja/1.12.1/ninja.exe
#   GCC    7.3.1   STM32CubeIDE 自带 (见 cmake/arm-none-eabi.cmake)
#
# ★ CMake 与 ninja 都是 Windows 程序: 必须传 Windows 风格路径 (正斜杠可以),
#   不能用 MSYS 的 /c/... 形式 —— 会报 "no such file or directory"。
#
# ★★ 纪律 (本项目的真事故): CMake 的 `-D` 是**缓存**的 —— 上一次
#   `-DDCL_DEPLOY_SELFTEST=1` 会被记住, 下一次不带该参数**不会**回到默认 0。
#   本项目真的因此把"自检版"当成交付固件烧进板子, 并据此读了一轮数据
#   (症状: nak_count=7 / n_routes=0, 而交付固件不该有这些)。且因为该构建
#   在 SWD 读回时看起来"一切正常", 极难察觉。
#   修法: 本脚本**每次显式传全部开关的默认值**, 再把用户参数追加在后面
#   (CMake 以最后一次 -D 为准) ⇒ 缓存永远被覆盖, 构建结果只由命令行决定。
set -e

HERE="$(cd "$(dirname "$0")" && pwd)"
# 转成 Windows 风格路径 (D:/...)
WIN_HERE="$(cd "$HERE" && pwd -W 2>/dev/null || echo "$HERE")"
BUILD="$WIN_HERE/build"

CMAKE="/c/Espressif/tools/cmake/4.0.3/bin/cmake.exe"
NINJA="C:/Espressif/tools/ninja/1.12.1/ninja.exe"

# ★★ Windows 下 GCC 的**中间文件** (.s/.o) 走 TMP/TEMP。若环境里 TEMP 指向
#   `C:\WINDOWS` (普通权限写不进去), 构建会以**与代码完全无关**的症状失败:
#       cc1.exe: fatal error: can't open 'C:\WINDOWS\ccXXXXXX.s' for writing: Permission denied
#   这个症状极易被误判成"我刚改坏了代码" (2026-09-11 真的踩到一次, 排查方向被带偏)。
#   ⇒ 显式把 TMP/TEMP 钉到工程 build/ 下: 构建结果不再依赖使用者环境的 TEMP 设置。
#      (与本项目"每次显式传全部开关默认值"同一条纪律: 别依赖外部状态的默认值。)
mkdir -p "$BUILD/tmp"
export TMP="$WIN_HERE/build/tmp"
export TEMP="$WIN_HERE/build/tmp"

if [ "$1" = "clean" ]; then
    rm -rf "$HERE/build"
    echo "cleaned: $HERE/build  (含 CMakeCache —— 下次构建回到全默认)"
    exit 0
fi

# ── 全部开关的默认值 (必须与 CMakeLists.txt 的 set(... CACHE ...) 默认值一致) ──
DEFAULTS=(
    -DDCL_BOOT_PROFILE=0
    -DDCL_BOOT_GATE=1
    -DDCL_BOOT_SEL=1
    -DDCL_BOOT_SCAN_MODE=0
    -DDCL_VTOR_ITCM=1
    -DSCAN_FLASH_PAD=0
    -DDCL_PA9_MODE=1
    -DDCL_MB_UART_ALT=1
    -DDCL_MB_BUILD_BUDGET=32
    -DDCL_MB_FAST_FRAME=1
    -DDCL_WDT=1
    -DDCL_WDT_MS=200
    -DDCL_WDT_FEED_GATE=1
    -DDCL_WDT_SYNC_CYC=40000000
    -DDCL_WDT_START_FIRST=1
    -DDCL_WDT_PR=4
    -DDCL_WDT_PERSIST_WINDOW=8000
    -DDCL_LOOP_RESET=1
    -DDCL_PERSIST_SAVE=0
    -DDCL_BOOT_BANNER=1
    -DDCL_BANNER_PERIOD=0
    -DDCL_UART_SELFTEST=0
    -DDCL_UART_BAUD=115200
    -DDCL_DEPLOY_SELFTEST=0
    -DDCL_MIN_UART=0
    -DDCL_MIN_UART2=0
    -DDCL_HIL_SAFE=1
    -DDCL_IO_IN_ISR=1
    -DDCL_DO_LATCH=0          # DO 输出路径: 0=CPU 直写(交付) 1=影子+MDMA锁存(对照)
    -DDCL_DO_MASK_UNION=1
    -DDCL_STEP_RAMP_FIX=1     # ★ 斜坡 dt 修复: 1=按真实 dt 算(交付) / 0=改前"向上取整到1ms"(对照)
                              #   病灶: 主循环 0.37ms 而 dt_ms 算成 1ms ⇒ 多爬 2.7 倍 ⇒ 声明12000实测~32kHz/s     # ★ DO 掩码保留位: 1=PE8..PE11 不可被上位机剔出掩码(交付)
                              #   /0=掩码说了算(对照档)。见 CMakeLists 的长注释:
                              #   `mismatch_n` 涨到 327616 的真因就是掩码把 PE9 剔掉了。
    -DDCL_STEP_ENA_POL=1      # ★ 步进 ENA 极性: -1=未配置(→fail-closed, 拒绝使能)/0=拉低使能
                              #   /1=拉高使能 ← **本台实机**(光耦正端接 3.3V, 实测)。见 PLAN-device-config-v1
    -DDCL_STEP_STOP_HOLD=0    # ★ 停止/上电是否保力矩。**本台 = 0（休息态断电）**
                              #   ★★★ 依据（实测）: ① 本台是**水平、空载**（审计 §7 声明空载）；
                              #     ② 审计 §1.5 实测"失能后 3 s 漂移 **0.000°**" ⇒ 靠齿槽力矩就够，
                              #        不需要通电保持；③ 而 `=1` 的实际后果是"**上电即通电**"：
                              #        `sub=6`/上电后 PE9 被驱成 **不导通 = 使能** ⇒ 电机被磁化、
                              #        **嗡嗡响 + 发热 + 轴不动**（用户现场观察到的三个现象，全部对得上）。
                              #   ★★ **垂直轴必须改回 1**（去使能 = 掉力滑车）—— 这就是它按轴的原因。
                              #   判据: `h723_stepper_motion.py stophold`（两臂必须给出相反电平）。
)

"$CMAKE" -S "$WIN_HERE" -B "$BUILD" -G Ninja \
    -DCMAKE_TOOLCHAIN_FILE="$WIN_HERE/cmake/arm-none-eabi.cmake" \
    -DCMAKE_MAKE_PROGRAM="$NINJA" \
    "${DEFAULTS[@]}" "$@"

# ── 构建 (同时落日志, 供下面的"零警告闸门"检查) ──
# ★★ 这是一道**硬闸门**, 起因是真事故: uart.c 的 `NVIC_ISER = (1u << 37)` 位移溢出,
#    GCC 报了 -Wshift-count-overflow, 但同次构建还有 24 条无害警告 (newlib 桩的
#    unused-parameter 等) 把真警告淹没 → 我们只 grep 了 error, 于是带病固件上线,
#    USART1 中断从未使能、协议层整轮不可用, 而所有"配置类"寄存器检查全绿。
#    两道防线: ① CMakeLists 里 -Werror (警告即编译失败)
#              ② 这里再扫一遍完整日志 (含链接器/汇编的警告)
set -o pipefail
LOG="$HERE/build/buildlog.txt"
# ★★ 已知间歇性失败 (2026-09-12 一天踩 3 次, 且**不一定重跑一次就收敛**;
#    2026-09-17 又踩: 单次重试**也失败**了):
#     cc1.exe: fatal error: can't open '...\build\tmp\ccXXXXXX.s' for writing: Permission denied
#   随机文件、随机名、且失败的文件里包含**从未改动过**的 .c ⇒ 与代码无关 (已知族)。
#   ⇒ 改成**最多 3 轮重试 + 退避**; 每一轮都**显式打印**, 不静默 ——
#     静默重试会把"真的编译失败"掩盖成"重试后还是失败", 丢掉第一次的错误信息。
# ★★★ 2026-09-17 修掉一处真缺陷: 原写法是
#       if ! build; then …重试…; "$CMAKE" --build … | tee; else …; exit 1; fi
#     重试那一句是分支里的**最后一条命令, 退出码没人看** ⇒ 重试也失败时脚本照样往下走,
#     而后面几道闸门是**读日志/grep**的、不依赖刚刚是否链接成功
#     ⇒ 结果是 **"构建失败 → 门过 → 烧了旧镜像"**（本项目 2026-09-17 真发生过一次:
#        `pyocd flash` 报 `programmed 0 bytes, skipped 68608` —— 板子上还是上一版固件,
#        而后续判据把"旧固件的错误行为"当成新固件的结论）。
#     ⇒ 现在: **循环 + 每轮都判退出码 + 最终失败 exit 1**。
ATTEMPT=0
while true; do
    if "$CMAKE" --build "$BUILD" 2>&1 | tee "$LOG"; then
        break
    fi
    ATTEMPT=$((ATTEMPT + 1))
    if grep -q "Permission denied" "$LOG" && grep -q "can't open" "$LOG" && [ "$ATTEMPT" -lt 3 ]; then
        echo
        echo "★★ 命中已知症状 (build/tmp Permission denied, 与代码无关) ⇒ 清 tmp 后重试 (第 $ATTEMPT/3 次)。"
        rm -rf "$BUILD/tmp"
        mkdir -p "$BUILD/tmp"
        sleep 2
        continue
    fi
    echo
    echo "★★ 构建失败 (非已知症状, 或已重试 3 次仍失败) ⇒ 中止, **不要烧旧镜像**。"
    echo "   ⚠ 此时 build/dcl_h723.hex 是**上一次**的产物; 任何 flash/验收都无意义。"
    exit 1
done

NW=$(grep -c "warning:" "$LOG" || true)
if [ "$NW" != "0" ]; then
    echo
    echo "★★ 构建产生 $NW 条警告 —— 拒绝通过。"
    echo "   本项目不接受'无害警告': 噪声会把真警告埋掉 (A1 事故就是这么发生的)。"
    echo "   要么修掉, 要么对该文件显式关掉**那一种**警告并写明理由。"
    echo "── 警告清单 ──"
    grep "warning:" "$LOG"
    exit 1
fi
echo "✓ 零警告 (本工程源文件 + 链接器 + 汇编)"

# ── ★★ ISR 调用树闸门 (2026-09-13 正式接入) ──────────────────────────────
# ★ 为什么必须有这一步: `src/itcm.h` 早就**宣称**过"构建期的 ISR 调用树闸门会把
#   '忘了加 DCL_ITCM'变成'构建不过'" —— 但脚本写好后**从未接进构建**。
#   宣称落空的代价就是本次事故: `hil_out_apply` 漏在 flash ⇒ 擦 flash 时拍 ISR
#   取指被 stall ⇒ ISR 不返回 ⇒ 喂狗停 ⇒ 看门狗复位 ⇒ "保存配置"变成"重启机器",
#   而且**配置从未落盘**。这条路径活过了 4 轮外部审计。
#   ★ 不变量本身见 src/itcm.h; 判据与 6 个已修仪器 bug 见 tools/gate_isr_itcm.py。
# ★ 找不到 python 时**显式警告并跳过**, 不静默 (静默跳过 = 闸门有洞, 本项目最恨的形态)。
PY_BIN="${DCL_PYTHON:-$(command -v python || command -v python3 || true)}"
if [ -z "$PY_BIN" ]; then
    echo
    echo "⚠️ 未找到 python ⇒ **跳过** ISR 调用树闸门 —— src/itcm.h 的不变量本次未被校验。"
    echo "   恢复方式: 装 python, 或设 DCL_PYTHON=<解释器绝对路径>。"
else
    echo
    echo "── ISR 调用树闸门 (src/itcm.h 的不变量: ISR 可达 ⇒ 必须住 ITCM) ──"
    # ★ 必须用 WIN_HERE (Windows 风格): python.exe 是 Windows 程序, 传 MSYS 风格
    #   的 `/d/STM/...` 会被它当成"相对当前盘符"⇒ 报 `D:\d\STM\...` 找不到文件。
    if ! "$PY_BIN" "$WIN_HERE/tools/gate_isr_itcm.py" "$WIN_HERE/build/dcl_h723"; then
        echo
        echo "★★ ISR 调用树闸门失败 ⇒ 拒绝通过。"
        echo "   上表列出的函数**擦 flash 时会让拍 ISR 卡死**(取指/读常量被 stall)。"
        echo "   修法: 给它们加 DCL_ITCM (见 src/itcm.h); 只读常量表用 .itcm_rodata 段。"
        exit 1
    fi
fi

# ── ★★ 应答缓冲区越界闸门 (2026-09-16 正式接入) ──────────────────────────────
# ★ 为什么必须有这一步: 同一族缺陷**已犯两次**, 而且两次都"运行期完全看不出来" ——
#   ① `h_pin_pattern()` : `uint8_t r[40]` 却 `ack(r, 68)` ⇒ 写坏调用者栈 28 字节
#   ② `h_engine_status()`: `uint8_t r[40]` 却 `ack(r, 51)` ⇒ 越界 11 字节
#      （把 `0x38` 从 39 扩到 51 字节时**只改了写的偏移, 没同步改缓冲声明**）
#   共同点: **不报错、不崩溃**, 读回来的字段**全是合理值** ⇒ **动态判据永远发现不了**。
#   ⇒ 只能是**静态**判据, 且必须进构建闸门（同 ISR 闸门的理由: 光有工具不进闸门 = 没做）。
#   ★ 判据本身也踩过"空判据"坑（第一版一个函数都没解析到却打印 OK）——
#     故本闸门要求工具**自报覆盖度**, 覆盖不足即判无效。见 tools/h723_ackbuf_check.py。
if [ -n "$PY_BIN" ]; then
    echo
    echo "── 应答缓冲区越界闸门 (静态: ack/put32 写入长度 vs 局部缓冲声明) ──"
    if ! "$PY_BIN" "$WIN_HERE/tools/h723_ackbuf_check.py" "$WIN_HERE/src"; then
        echo
        echo "★★ 应答缓冲区越界闸门失败 ⇒ 拒绝通过。"
        echo "   修法: 用**同一个常量**同时定缓冲大小与发送长度, 不要在两处各写一遍字面量"
        echo "         （这是本项目'一个语义两处存放 ⇒ 静默失效'族的根因）。"
        exit 1
    fi
fi

# ── ★★ ③层静态判据闸门 (2026-09-16 接入): 程序面不得出现引脚号/地址/寄存器名 ──
# ★ 契约 §3.1 自评"最重要的一条", GAP-10 原为 open; 三边权威独立确认:
#   IEC 61131-3 / CODESYS("application POUs never reference physical I/O addresses") / Zephyr。
# ★ 这里扫的是 **examples/ 的示例程序** —— 因为示例是"③层该怎么写"的**事实标准**,
#   它们一旦漂移, 后来的人就会照着错的学。真正的强制点在 `tools/dclc.py`(拒绝编译)。
# ★ 该判据自带 `--selftest`(造好的红必红/绿必绿) 与**覆盖度自报**(0 文件即判无效) ——
#   原因见 tools/dcl_static_check.py 的说明: 它第一版就因为"引脚名只在注释里"的标定问题
#   差点变成误报机器, 而误报会被自己人关掉。
if [ -n "$PY_BIN" ]; then
    echo
    echo "── ③层静态判据闸门 (契约 §3.1: 程序不得出现引脚号/总线地址/寄存器名) ──"
    if ! "$PY_BIN" "$WIN_HERE/tools/dcl_static_check.py" "$WIN_HERE/examples"; then
        echo
        echo "★★ ③层静态判据闸门失败 ⇒ 拒绝通过。"
        echo "   修法: 用符号槽表达(sensor[i]/wire[j]); 引脚/地址属于②层外设能力, 不进程序。"
        exit 1
    fi
fi

# ── ★★ 契据可机检闸门 (2026-09-16 接入): 契据 ⇄ 代码/脚本 的一致性 ──────────
# ★ 为什么必须有这一步 (与上面三道闸门是同一个理由: **光有工具不进闸门 = 没做**):
#   2026-09-16 的两轮独立审计共查出 **5 处"契据 vs 实现不一致"**, 全部是**人能发现、机器发现不了**的
#   (§3.8.5 只写不做 / §3.8.4 两条规则**字面自相矛盾** / §3.7 未同步令牌 /
#    §3.8.3 漏写"接受时也写 reject=0" / 工具侧 expect_len 只说不做)。
#   ★★ 而"契据写了、代码没做"**比完全没做更危险** —— 下一个人会照契据去改, 从而**改坏**。
#   ⇒ 人工审计的成本随规模上升; 这件事必须由机器每次构建都做。见 docs/PLAN-consistency-v1.md C 线。
# ★ 判据五类 (登记在 docs/claims.md 的 ```claims 块): A 结构 / B 拒绝码 / C 能力位 /
#   D 文档 file:line 漂移 / E 判据存在性。
# ★ 规格与上面两道**相同**: 自带 `--selftest`(造好的红必红、绿必绿) 与**覆盖度自报**
#   (某类登记数低于下限 ⇒ 判**无效**, 不是"干净")。
# ★ 未覆盖项**必须显式声明**: `--allow-uncovered <理由>` —— "不允许沉默地留着"(PLAN 的 DoD 第 4 条)。
if [ -n "$PY_BIN" ]; then
    echo
    echo "── 契据可机检闸门 (docs/claims.md ⇄ 代码/脚本: 结构/拒绝码/能力位/文档漂移/判据存在性) ──"
    if ! "$PY_BIN" "$WIN_HERE/tools/ref_claims_check.py" --root "$WIN_HERE"; then
        echo
        echo "★★ 契据可机检闸门失败 ⇒ 拒绝通过。"
        echo "   修法二选一: ① 让代码/脚本跟上契据; ② 在 docs/claims.md 里为那一条"
        echo "   --allow-uncovered <理由> 显式声明(不允许沉默地留着)。"
        exit 1
    fi
fi

# ── ★★ 内存账本闸门 (2026-09-19 接入 / 内存宪法 B 期): 四区账 + 六条判据 + 文档对账 ──
# ★ 为什么必须有 (血证): `build.sh` 以前**全文不报尺寸** ⇒ "ITCM 64/64 KB 已满" 这句话在
#   **6 份文档**里活了数月, 还被 README 当成"多轴的三个真阻塞"之一 ——
#   而 `arm-none-eabi-size -A` 实测只用了 **14.32 KB / 64 KB（22.4%）**。
#   ⇒ 过期数字会去支撑产品决策; 账本必须由**构建派生**, 且文档里的容量宣称要**对得上账**。
# ★ 判据六条 (见 tools/mem_report.py 的文件头):
#   C2 链接器不得往 AXI 放段 (M2) · C3 SHM 段长 == SHM_SIZE (M4) ·
#   C4 memmap.h 的 heapstack == .ld 的 heap+stack (M4 跨文件) · C5 AXI 恰好铺满 320 KB (M2) ·
#   C6 ITCM 使用率 < 80% (M3, >60% 告警) · C7 栈余量 ≥ 8 KB (M3) · C8 文档容量宣称对账 (M4)
# ★ 规格与上面几道相同: 自带 `--selftest`（合成数据证明 C2/C3 会红）。
if [ -n "$PY_BIN" ]; then
    echo
    echo "── 内存账本闸门 (四区账 + C2~C8; 权威源 src/memmap.h + 构建产物) ──"
    if ! "$PY_BIN" "$WIN_HERE/tools/mem_report.py" --check-docs; then
        echo
        echo "★★ 内存账本闸门失败 ⇒ 拒绝通过。"
        echo "   修法三选一: ① 改代码/地图让性质与用途匹配; ② 改文档里的容量宣称(或加成'更正'横幅);"
        echo "   ③ 若确属有意变更地图 ⇒ 改 src/memmap.h 并重跑本工具核对账本。"
        exit 1
    fi
fi

# ── ★★ 实验登记 + 文档死链闸门 (2026-09-19 接入 / 文档系统化) ──────────────
# ★ 为什么必须有: 实测 docs/ 下 **114 份 .md**（比 src 的 58 个文件还多），其中 **24 份是孤儿**，
#   而 README §十一「问题→权威源」只登记 12 行 —— 这就是"文档多了却没系统化"。
#   两条**精确**判据（不放启发式，避免误报把闸门关掉）:
#     D1 README §十一 的每个链接都必须存在（权威源表不能骗人）
#     D2 §十一 只能登记 docs/ 下的路径
#   另加: 实验登记表与 docs/EXP-INDEX.md 必须一致（工具/文档存在、状态在枚举里）。
if [ -n "$PY_BIN" ]; then
    echo
    echo "── 实验登记 + 文档死链闸门 (exp_registry ⇄ EXP-INDEX · README §十一 链接) ──"
    if ! "$PY_BIN" "$WIN_HERE/tools/exp_registry.py" --check; then
        echo "★★ 实验登记闸门失败 ⇒ 拒绝通过（跑 --index 重新生成，或修登记表）。"
        exit 1
    fi
    if ! "$PY_BIN" "$WIN_HERE/tools/doc_index.py" --check; then
        echo "★★ 文档死链闸门失败 ⇒ 拒绝通过（README §十一 里指向了不存在的文件）。"
        exit 1
    fi
fi

# ── 打印**实际生效**的开关 (不是"我以为传了什么") ──
echo
echo "── 生效开关 (读自 CMakeCache) ──"
# ★ 正则必须含数字: 原来写 `^DCL_[A-Z_]*:` —— `DCL_MIN_UART2` 因为结尾是 2 而**不在
#   这个清单里**, 也就是说"看上去列全了, 其实漏了一个"。这一段的唯一目的就是
#   "别被缓存骗", 漏列一个开关等于给它留了一个静默洞。⇒ 改成 [A-Z0-9_]*。
grep -E "^(DCL_[A-Z0-9_]*|SCAN_FLASH_PAD):(STRING|BOOL)=" "$HERE/build/CMakeCache.txt" \
    | sed 's/:[A-Z]*=/ = /' | sed 's/^/   /'
echo
echo "产物: $HERE/build/dcl_h723 (ELF, 无扩展名) + .bin + .hex + .map"
echo "烧录: pyocd flash -t stm32h723xx -O connect_mode=under-reset \"$HERE/build/dcl_h723.hex\""
