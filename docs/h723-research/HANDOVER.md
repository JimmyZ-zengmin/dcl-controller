# 核心0 — 真空间架构 交接文档

> 写给下一个 AI session。当前 token 已满，需要在新的对话中继续。

## 一、项目目标

在 STM32H723ZGT6 @ 544MHz 上实现 **1μs 零抖动** 下运行大型控制程序。

**架构跃迁思路**：将 DTCM 里的路由表（数据，被 ISR 逐条解释）→ 编译成 ITCM 里的 Thumb-2 指令序列（代码，PC 直接走）。从"假空间"（时间顺序扫描数据）到"真空间"（PC 在空间中走过指令序列）。

## 二、项目结构

```
D:\STM\work\core0_h723\         ← 基线项目 (100μs, 稳定)
D:\STM\work\core0_h723_1us\     ← 前沿项目 (1μs, 正在开发)
  ├── Src/main.c                ← 全部代码：编码器 + 编译器 + ISR + 测试
  ├── STM32H723ZGTX_FLASH.ld    ← 链接脚本 (ITCM section)
  ├── Startup/startup_stm32h723zgtx.s  ← ITCM Flash→RAM 拷贝
  ├── B4-汇编优化规划.md         ← 原始汇编优化方案
  └── HANDOVER.md               ← 本文件
```

**CubeIDE 项目名**：`core0_h723_1us`（不是 core0_h723）

## 三、操作手册

### 3.1 编译

```
工具: STM32CubeIDE 1.5.1
步骤:
  1. 打开 CubeIDE, workspace = D:\STM\work\
  2. 右键 core0_h723_1us → Clean Project
  3. 右键 core0_h723_1us → Build Project
  4. 产物: D:\STM\work\core0_h723_1us\Debug\core0_h723_1us.elf
```

**注意**：不要用命令行编译，用 CubeIDE。`.cproject` 里的路径曾经有过 bug（指向 `core0_h723` 而非 `core0_h723_1us`），如果编译报错先检查 `.cproject` 里的路径。

### 3.2 烧录

```bash
# pyocd 路径 (用户环境):
/c/Espressif/tools/python/v6.0.1/venv/Scripts/pyocd.exe

# 烧录命令 (必须 under-reset, 否则 ISR 跑起来后 SWD 锁死):
/c/Espressif/tools/python/v6.0.1/venv/Scripts/pyocd.exe flash \
  -t stm32h723xx \
  -O connect_mode=under-reset \
  /d/STM/work/core0_h723_1us/Debug/core0_h723_1us.elf
```

**RESET 引脚必须接调试器**，否则 `under-reset` 无效。

### 3.3 读取硬件诊断

全部用 `pyocd commander`，无需重新编译烧录：

```bash
PYOCD="/c/Espressif/tools/python/v6.0.1/venv/Scripts/pyocd.exe"
TGT="-t stm32h723xx"

# ── 时序诊断区 (DTCM 0x20000000, 256B) ──
$PYOCD commander $TGT -c "read32 0x20000000 64; exit"
# 关键字段:
#   +0x00 EXEC_MIN      ISR 执行时间最小值 (cycles)
#   +0x04 EXEC_MAX      ISR 执行时间最大值
#   +0x08 PERIOD_MIN    周期最小值 (应为 ~544)
#   +0x0C PERIOD_MAX    周期最大值
#   +0x10 SAMPLES       ISR 触发次数 (0=ISR没启动)
#   +0x18 HEARTBEAT     心跳
#   +0x24 EXEC_TOTAL    累计执行时间
#   +0x28 DEV_ABS_MAX   调试标记 (编译块大小等)
#   +0x2C DEV_ABS_MAX_SMP  调试标记
#   +0x30 DEV_POS_MAX   调试标记
#   +0x34 DEV_NEG_MAX   ★ 最重要: 测试进度标记
#   +0x38 PERIOD_EXACT  周期精确命中次数
#   +0x3C PERIOD_FAR    周期偏差次数

# ── Fault 诊断区 (DTCM 0x20000040, 56B) ──
$PYOCD commander $TGT -c "read32 0x20000040 16; exit"
#   +0x00 FAULT_CFSR      (可配故障状态寄存器)
#   +0x04 FAULT_HFSR      (硬故障状态)
#   +0x14 FAULT_EXC_RET   (异常返回码)
#   +0x18 FAULT_STACKED_R0
#   +0x1C FAULT_STACKED_R1
#   +0x30 FAULT_STACKED_PC  ← 崩在哪条指令
#   +0x34 FAULT_STACKED_PSR

# ── SCB 寄存器 (直接读) ──
$PYOCD commander $TGT -c "read32 0xE000ED28 12; exit"
#   +0x00 CFSR   (UsageFault: bit16=NOCP, bit17=INVPC, bit18=INVSTATE, bit19=UNDEFINSTR)
#   +0x04 HFSR   (HardFault: bit30=FORCED)
#   +0x08 MMFAR
#   +0x0C BFAR

# ── ITCM 内容 (编译块在 0x00000800) ──
$PYOCD commander $TGT -c "read32 0x00000800 64; exit"
# 读 64 个 32-bit word = 256 字节

# ── 单地址读写 ──
$PYOCD commander $TGT -c "read32 0x20000034; exit"    # 读 DEV_NEG_MAX
$PYOCD commander $TGT -c "write32 0x200000E0 3; exit" # 写 TEST_SELECT=3
```

### 3.4 反汇编验证

```bash
OBJDUMP="/c/ST/STM32CubeIDE_1.5.1/STM32CubeIDE/plugins/com.st.stm32cube.ide.mcu.externaltools.gnu-tools-for-stm32.7-2018-q2-update.win32_1.5.0.202011040924/tools/bin/arm-none-eabi-objdump.exe"

# 反汇编 ISR
$OBJDUMP -d /d/STM/work/core0_h723_1us/Debug/core0_h723_1us.elf | grep -A120 "TIM1_UP_IRQHandler"

# 反汇编 prim_handler
$OBJDUMP -d /d/STM/work/core0_h723_1us/Debug/core0_h723_1us.elf | grep -A30 "prim_handler"

# 反汇编某个地址范围
$OBJDUMP -d /d/STM/work/core0_h723_1us/Debug/core0_h723_1us.elf | grep -A60 "800:"
```

## 四、内存布局 (重要地址)

```
DTCM (0x20000000, 128KB, 零等待):
  0x20000000  TIMING_BASE     时序/诊断 (256B)
  0x20000034  DEV_NEG_MAX     ★ 测试进度标记
  0x20000040  FAULT_BASE      故障上下文 (56B)
  0x200000F0  ACTIVE_ROUTES   活跃路由数 (4B)
  0x20000100  SENSOR_MAP      64ch × 4B
  0x20000200  ACTUATOR_STATUS 32ch × 4B
  0x20000300  WIRE_MAP        1024ch × 4B
  0x20001300  LUT_DATA        LUT 数据
  0x20001700  ROUTE_TABLE     1024条 × 16B
  0x20005700  PARAM_TABLE     512ch × 16B
  0x20007700  STATE_TABLE     256ch × 16B
  0x20009000  SIN_LUT         4096点 × 4B (Flash→DTCM)
  0x2000E000+ (之前预留, 现在用作编译块)

ITCM (0x00000000, 64KB, 零等待):
  0x00000000  prim_handler    原语处理器 (GCC编译, 从Flash加载)
  0x00000040  TIM1_UP_IRQHandler  ISR (GCC编译, 从Flash加载)
  0x00000800  CMP_BLK_BASE    编译块输出区 (运行时写入, 未用到 0x0800)
  0x00000900  临时测试区 (Test3/Test4 用)
  0x00001000+ (空闲)

SRAM (0x24000000, 320KB):
  .data, .bss, heap, stack
```

## 五、当前状态

### 5.1 已完成 ✅

- **解释器模式**：1μs 周期，8 路由，PERIOD=516~562 cycles，零抖动。（设置 `USE_COMPILED_ISR=0` 即可跑）
- **指令编码器**：所有 Thumb-2 编码函数（emit_vldr, emit_vstr, emit_movw, emit_movt, emit_addw, emit_blx, emit_ret）已验证，与 objdump 输出一致。
- **路由编译器**：`compile_routes(8)` 在 ITCM 0x800 生成了 232 字节的指令序列。Preamble 正确（MOVW+MOVT 设置 r4=SENSOR_BASE, r5=WIRE_BASE, r6=PARAM_BASE, r7=STATE_BASE, r8=prim_handler|1）。每条路由的 VLDR + MOVW + ADDW + BLX r8 + VSTR 序列正确。
- **非 VFP 指令从运行时写入的 ITCM 可执行**：NOP+NOP+BX LR、纯整数指令序列都能执行。
- **GCC 编译的 VFP 指令从 ITCM 可执行**：prim_handler、ISR 里的 VLDR/VSTR/VFMA/VSUB 等全部正常。

### 5.2 当前卡点 🔴

**运行时写入的 VFP 指令从 ITCM 执行 → UNDEFINSTR (CFSR=0x00020000)**

具体现象：
```
Test5: 从 main() 直接调用 compile_routes 输出 (ITCM 0x800)
  → DEV_NEG_MAX = 0xD500 (开始) → 0xD5FF 未到达 (崩溃)

崩溃现场:
  CFSR           = 0x00020000     UNDEFINSTR
  FAULT_PC       = 0x20005700     PARAM_BASE (= R1 的值!)
  FAULT_LR       = 0x0000083B     BLX 已执行, LR=返回地址正确
  FAULT_R0       = 0x000E         OP_SCALE (MOVW r0,#14 正确执行)
  FAULT_R1       = 0x20005700     PARAM_BASE
  FAULT_R8       = ?              (未保存到 FAULT 区, 因为 r8 不在 basic frame)
```

**核心谜团**：BLX r8 的编码是 0x4788（已验证）。r8 的值是 `0x00000001`（prim_handler | 1）。但 CPU 却跳到了 `0x20005700`（PARAM_BASE），即 R1 的值。

这看起来像是 BLX 读错了寄存器——读到 R1 而不是 R8。但 0x4788 的编码确实指向 R8，不是 R1（BLX R1 是 0x4789，但我们写入的是 0x4788）。

**已知事实**：
- prim_handler 在 ITCM 0x00000000 完好（pyocd 读回验证，objdump 反汇编验证）
- prim_handler 第一行是 `subs r0, #1`，不碰 r8
- ITCM preamble 区的 BLX 0x4788 被读回验证正确
- ISR 没启动（PERIOD=0xFFFFFFFF, SAMPLES=0），排除了 ISR 干扰
- 触发 UNDEFINSTR 而不是 INVSTATE，说明 CPU 取到了一条未定义的指令
- PARAM_BASE (0x20005700) 是数据不是代码 → 4B 全是 0x42C80000 (float=100.0f) → CPU 把数据当指令执行 → UNDEFINSTR

### 5.3 DEV_NEG_MAX 测试标记速查

```
0xB100   compile_routes() 返回
0xB200   所有测试完成, 准备启动 TIM1
0xBEEF   compile_routes() 内部完成 (DSB/ISB/ICIALLU 后)
0xD001-0xD006  compile_routes() 内部每步标记
0xD100   Test1 开始 (VLDR+BX LR 在 GCC ITCM 区)
0xD1FF   Test1 通过
0xD200   Test2 开始 (手写 VLDR+VSTR+BX LR @0x200)
0xD2FF   Test2 通过
0xD400   Test4 开始 (emit VLDR+ret → 编译块格式 @0x900)
0xD4FF   Test4 通过
0xD500   Test5 开始 (compile_routes 编译块调用)
0xD5FF   Test5 通过 ← 当前到不了这里
```

## 六、未探索方向

1. **Cortex-M7 ITCM 写入粒度**：ITCM 接口是 64-bit。 32-bit store 写 ITCM 可能不被指令侧看到。需要查 ARM TRM。
2. **调试口 (AHB-AP) 写 ITCM vs CPU store 写 ITCM**：pyocd 通过调试口写内存走的是 AHB-AP，CPU 的 store 指令走的是 D-side。两者对 ITCM 的可见性可能不同。
3. **FPU 惰性上下文**：FPCCR (0xE000EF34) 的 LSPEN/ASPEN 位。
4. **纯整数方案**：如果 ITCM 运行时 VFP 确实不可行，用 LDR 替代 VLDR，用 VMOV 在整数/VFP 寄存器间传数据。这需要编译块只用整数 load/store。

## 七、下一步方案 (三个可选方向)

### 方向 A: pyocd Python API → 硬件 REPL

不编译不烧录，直接用 Python 脚本通过 pyocd 调试接口往目标板写指令、设寄存器、单步执行、读结果。

```python
# 参考: pyocd 的 Python API
from pyocd.core.helpers import ConnectHelper
from pyocd.flash.file_programmer import FileProgrammer

with ConnectHelper.session_with_chosen_probe(target_override='stm32h723xx') as session:
    # halt CPU
    session.target.halt()
    # 写 ITCM
    session.target.write_memory(0x00000800, [0x1400F240, 0x0400F2C2, ...])
    # 写 r4=SENSOR_BASE
    session.target.write_core_register('r4', 0x20000100)
    # 设 PC
    session.target.write_core_register('pc', 0x00000801)  # +thumb bit
    # 执行
    session.target.resume()
    time.sleep(0.01)
    session.target.halt()
    # 读结果
    wire0 = session.target.read_memory(0x20000300, 4)
```

**优势**：迭代周期 = 秒级。可以快速测试不同指令序列。调试口写 ITCM 路径不同，可能绕开 VFP 问题。

### 方向 B: pyocd GDB Server → 交互式调试

```bash
# 启动 GDB server
pyocd gdb -t stm32h723xx

# 另一个终端
arm-none-eabi-gdb
(gdb) target extended-remote :3333
(gdb) monitor reset halt
(gdb) set $r4=0x20000100
(gdb) set $pc=0x00000801
(gdb) stepi        # 单步!
(gdb) info registers
```

**优势**：可以单步执行编译块，精确看到每条指令执行后 PC 和寄存器的变化。直接定位是哪条指令导致了问题。

### 方向 C: 一烧多测 (GCC 编译测试块)

在 `main.c` 里写多个 `__attribute__((naked, section(".itcm_code")))` 测试函数（由 GCC 编译，启动代码加载到 ITCM）。DTCM 里 `TEST_SELECT`（0x200000E0）选测试号。闪一次，pyocd 写不同测试号+reset 就能跑不同测试。

**优势**：VFP 指令由 GCC 编码，走 Flash→ITCM 路径（已验证可行）。每个测试隔离一个变量。

## 八、本 session 对 main.c 的修改

1. 添加了 `TEST_SELECT` / `TEST_RESULT` 宏（TIMING_BASE + 0xE0/E4）
2. 清理了 ISR 里意外粘贴的测试结果注释块
3. （未完成）测试函数和测试循环的插入——这部分需要下一个 session 继续

## 九、快速启动命令 (copy-paste 用)

```bash
# 设置别名
PYOCD="/c/Espressif/tools/python/v6.0.1/venv/Scripts/pyocd.exe"
TGT="-t stm32h723xx"

# 烧录
$PYOCD flash $TGT -O connect_mode=under-reset /d/STM/work/core0_h723_1us/Debug/core0_h723_1us.elf

# 快速诊断 — DEV_NEG_MAX + FAULT
$PYOCD commander $TGT -c "read32 0x20000034 4; read32 0x20000040 16; read32 0xE000ED28 12; exit"

# 检查 ISR 是否在跑 (SAMPLES=0 说明没启动)
$PYOCD commander $TGT -c "read32 0x20000010 2; exit"

# 读 ITCM 编译块
$PYOCD commander $TGT -c "read32 0x00000800 64; exit"

# 读 prim_handler (前 64B)
$PYOCD commander $TGT -c "read32 0x00000000 16; exit"
```
