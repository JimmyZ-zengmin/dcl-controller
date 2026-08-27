# 1μs 路由引擎 — 知识图谱 (Knowledge Map)

> 把这次调研的所有发现系统化。**以后出问题，先来这里查图谱，再针对性搜资料**。
>
> 最近更新：2026-06-30

---

## 0. 快速决策表 (TL;DR)

| 症状 | 优先怀疑 | 验证方法 |
|------|----------|----------|
| 运行时写入 ITCM 后执行 UNDEFINSTR / INVSTATE (CFSR=0x20000) | **编码对齐**：32-bit Thumb-2 指令是否 4 字节对齐 | pyocd 读 ITCM 内容，与 GCC 编译产物对比 |
| `*(volatile uint32_t *)0x00000800` store 后 BusFault (BFAR=0x24050000) | **地址重叠**：store 地址在 0x800+ 但 CPU 看到的是不同内容 | 改用 Test3+Test4 分区测试 |
| ICCMR 写入似乎"成功"但执行时仍跳到旧代码 | **I-Cache 残留** | 加 `SCB_ICIALLU = 0; DSB; ISB;` |
| 整数指令也触发 INVSTATE (CFSR=0x20000) | **FPU 上下文**：执行 VFP 后 FPCA=1，栈帧变 extended (26字)，handler 误读 | 检查 handler `lr & 0x10`，是 1 则跳过 0x40 字节 |
| ITCM 0x800+ 内容读回是初始值（0xDEADBEEF 之类） | **地址空间**：0x800 是否在 ITCM 64KB 内 | 看链接脚本 `.itcm_code` 段 |
| GPIO 翻转测时序时出现"半周期"异常 | **BOOT 引脚 / Flash 等待状态** | 检查 VOS0/1 电压与 WS |
| 汇编生成代码运行到某条就崩 | **半字序**：Thumb-2 32-bit 指令两半字顺序 | GCC objdump 对比 |
| Debug 时跑得通，Release 不行 | **优化等级 + volatile** | 看 .o 反汇编 |

---

## 1. STM32H723 内存架构

### 1.1 总览

```
┌─────────────────────────────────────────────────────┐
│  Cortex-M7 Core (CPU)                               │
│  ┌────────────┐  ┌────────────┐  ┌────────────┐    │
│  │ I-Bus      │  │ D-Bus      │  │ S-Bus      │    │
│  │ 64-bit     │  │ 32-bit     │  │ (AHBS)     │    │
│  └─────┬──────┘  └─────┬──────┘  └─────┬──────┘    │
└────────┼───────────────┼───────────────┼───────────┘
         │               │               │
         ▼               ▼               ▼
   ┌──────────┐   ┌──────────┐   ┌──────────────┐
   │ ITCM-RAM │   │ DTCM-RAM │   │ AXI/AHB Bus  │
   │ 64KB     │   │ 128KB    │   │ → SRAM, Flash│
   │ 0x000000 │   │ 0x200000 │   │ → Peripherals│
   │ ~FFFF    │   │ ~1FFFF   │   │              │
   └──────────┘   └──────────┘   └──────────────┘
```

### 1.2 关键事实

| 项目 | 值 | 来源 |
|------|-----|------|
| **ITCM 大小** | **64KB** (0x0000_0000~0x0000_FFFF) | [H723 数据手册 Table 5](https://www.st.com/resource/en/datasheet/stm32h723ze.pdf) |
| **DTCM 大小** | 128KB (0x2000_0000~0x2001_FFFF) | 同上 |
| AXI SRAM | 512KB at 0x2400_0000 | 同上 |
| **ITCM 总线访问** | 只能 CM7 + MDMA 通过 AHBS slave 访问 | [ST 社区原话](https://community.st.com) |
| **ITCM 取指** | 通过 I-Bus，零等待 | PM0253 |

### 1.3 ⚠️ 易错点

> **ITCM 是 CPU 内部 TCM，不能被 AHB 总线矩阵访问！** 
> 如果 `*(volatile uint32_t *)0x800 = value` 触发了 BusFault，
> 原因可能是地址 0x800 不在 ITCM 范围内（而不是 ITCM 不可写）。

### 1.4 链接脚本的 .itcm_code 段

我们项目的链接脚本 (STM32H723ZGTX_FLASH.ld)：

```ld
MEMORY
{
  FLASH  (rx)    : ORIGIN = 0x08000000, LENGTH = 1024K
  DTCMRAM (xrw)  : ORIGIN = 0x20000000, LENGTH = 128K
  RAM_D2 (xrw)   : ORIGIN = 0x30000000, LENGTH = 32K
  RAM_D3 (xrw)   : ORIGIN = 0x30004000, LENGTH = 64K
  ITCMRAM (x)    : ORIGIN = 0x00000000, LENGTH = 64K   ← 0x0000_0000 起 64KB
}
```

→ CMP_BLK_BASE=0x0800 **在 ITCM 范围内** ✓

---

## 2. Cortex-M7 TCM 访问规则（来自 PM0253 / ARM TRM）

### 2.1 三大总线

| 总线 | 用途 | 可访问 TCM? |
|------|------|-------------|
| **I-Bus (Code)** | 指令取指 | ✓ ITCM (取指) |
| **D-Bus (Data)** | 数据 load/store | ✓ DTCM (load/store)；✗ **ITCM 直接不可访问** |
| **S-Bus (AHBS)** | 系统访问，DMA | ✓ ITCM + DTCM |

> **关键**：`*(volatile uint32_t *)0x00000800 = 0xDEADBEEF;` 用的是 **D-Bus**，
> 理论上不直接访问 ITCM。但 ARM Cortex-M7 的设计是 D-Bus 对 TCM 区域有特殊处理，
> 实际上 store 能命中 ITCM（通过内部 crossbar 调度到 AHBS slave）。
> **ST 社区确认这点可行**。

### 2.2 32-bit Thumb-2 指令对齐要求

```
任何 32-bit Thumb-2 指令必须从 4 字节对齐地址开始执行！
```

**未对齐的后果**：CPU 从指令中间半字开始解码 → `bit[15]=0` → CPU 尝试切到 ARM 模式 → **INVSTATE (CFSR=0x20000)**

### 2.3 16-bit 指令混合的规则

Thumb-2 代码中 16-bit 和 32-bit 指令可以混用，但：

- **16-bit 指令不会破坏 32-bit 指令的对齐要求**
- 但 16-bit 指令**之后**的 32-bit 指令会偏移 2 字节！
- 解法：16-bit 指令后补 NOP (0xBF00) 恢复对齐

```c
emit_u32(MOVW_R4_0x100);    // 4 字节对齐 ✓
emit_u16(BLX_R8);           // 2 字节，破坏对齐
emit_u16(NOP);              // 补 2 字节，恢复对齐 ✓
emit_u32(VLDR);             // 4 字节对齐 ✓
```

---

## 3. ITCM 写入后的缓存同步协议

### 3.1 完整 4 步同步（官方推荐）

```c
// 写入 ITCM 编译块
emit_routes();

/* 1. 清理 D-Cache 对应的虚拟地址行 */
SCB_CleanDCache_by_Addr((uint32_t *)ITCM_BASE, ITCM_SIZE);

/* 2. 失效 I-Cache（M7 必备！）*/
SCB_InvalidateICache();

/* 3. 数据同步屏障 */
__DSB();

/* 4. 指令同步屏障 */
__ISB();
```

### 3.2 简化版（我们当前使用）

```c
emit_routes();
SCB_InvalidateICache();    // = SCB->ICIMVAU = 0; 实际只是让 I-Cache 全失效
__DSB();
__ISB();
```

### 3.3 ⚠️ 重要：I-Cache 必须失效！

H723 的 I-Bus 有 16KB 指令 cache。如果写 ITCM 之前，编译块地址曾被取指过，cache 中就有"旧版本"。

→ 即使写入了新指令，CPU 仍会从 cache 取老指令。

→ **必须**在写完 ITCM 后 `SCB_InvalidateICache()`，然后 `ISB` 刷新流水线。

---

## 4. Thumb-2 指令编码速查

### 4.1 32-bit 指令在内存中的布局

```
Thumb-2 32-bit 指令:
  bits[31:16] = 第一半字 (high halfword)
  bits[15:0]  = 第二半字 (low halfword)

小端存储:
  byte[0] = bits[7:0]    → 第一半字的低字节
  byte[1] = bits[15:8]   → 第一半字的高字节
  byte[2] = bits[23:16]  → 第二半字的低字节
  byte[3] = bits[31:24]  → 第二半字的高字节
```

### 4.2 写入函数模板

```c
/* 32-bit 指令: w = (high << 16) | low */
static inline void emit_u32(uint32_t w) {
    uint32_t le = (w >> 16) | (w << 16);   // 半字预交换
    *(volatile uint32_t *)cmp_p = le;       // 一次 32-bit store
    cmp_p += 2;  // 推进 2 个 halfword = 4 字节
}

/* 16-bit 指令 */
static inline void emit_u16(uint16_t w) {
    *(volatile uint16_t *)cmp_p = w;
    cmp_p += 1;
}
```

### 4.3 关键指令编码参考

| 指令 | 编码 (high<<16 \| low) | 备注 |
|------|------------------------|------|
| `BLX Rm` (16-bit) | `0x4780 \| Rm` | 后必须补 NOP |
| `BX LR` (16-bit) | `0x4770` | 函数返回 |
| `NOP` (16-bit) | `0xBF00` | 对齐恢复用 |
| `MOVW Rd, #imm16` | `0xF2400000 \| ...` | T3 编码 |
| `MOVT Rd, #imm16` | `0xF2C00000 \| ...` | T3 编码 |
| `VLDR.F32 Sd, [Rn, #imm]` | `0xED1 \| (1010) \| ...` | VFP 单精度 |
| `VSTR.F32 Sd, [Rn, #imm]` | `0xED8 \| (1010) \| ...` | VFP 单精度 |
| `ADDW Rd, Rn, #imm12` | `0xF2000000 \| ...` | T3 编码 |
| `PUSH {regs}` | `0xB5xx` (16) / `0xE92Dxxxx` (32) | |

### 4.4 MOVW 编码完整公式

```
MOVW Rd, #imm16
T3 编码 = 0xF2400000
       | ((imm16 >> 11) & 1) << 26    // i
       | ((imm16 >> 12) & 0xF) << 16   // imm4
       | (imm16 & 0x800) ? 0 : (1 << 20)  // 永远 0 (暂未处理 s 位)
       | ((imm16 >> 8) & 0x7) << 12    // imm3
       | (Rd << 8)
       | (imm16 & 0xFF)                // imm8
```

---

## 5. Fault 诊断速查

### 5.1 CFSR (0xE000_ED28) 位定义

| 偏移 | 位 | 名称 | 含义 |
|------|-----|------|------|
| MMFSR (0x00) | 7 | MMARVALID | MMFAR 有效 |
| | 5 | MLSPERR | 浮点惰性保存错误 |
| | 4 | MSTKERR | 入栈错误 |
| | 3 | MUNSTKERR | 出栈错误 |
| | 1 | DACCVIOL | 数据访问违例 |
| | 0 | IACCVIOL | 指令访问违例 |
| BFSR (0x04) | 7 | BFARVALID | BFAR 有效 |
| | 5 | LSPERR | 浮点惰性保存错误 |
| | 4 | STKERR | 入栈错误 |
| | 3 | UNSTKERR | 出栈错误 |
| | 2 | IMPRECISERR | 不精确数据总线错误 |
| | 1 | PRECISERR | 精确数据总线错误 |
| | 0 | IBUSERR | 指令总线错误 |
| UFSR (0x08) | 9 | DIVBYZERO | 除零 |
| | 8 | UNALIGNED | 未对齐访问 |
| | 7 | INVPC | 非法 PC 装入 |
| | 6 | INVSTATE | **非法 EPSR.T 或 IT 块** |
| | 5 | INVEP | |
| | 4-0 | UNDEFINSTR (bit 16) | 未定义指令 |

**CFSR = 0x20000** ← 单独 bit[17] (UFSR 的 INVSTATE) = 1
**CFSR = 0x00010000** ← bit[16] (UFSR 的 UNDEFINSTR) = 1

### 5.2 HFSR (0xE000_ED2C)

| 位 | 名称 | 含义 |
|-----|------|------|
| 31 | DEBUGEVT | 调试事件 |
| 30 | FORCED | 升级的 hard fault |
| 1 | VECTTBL | 读向量表时出错 |

### 5.3 INVSTATE 详解 (最常见的诡异异常)

**为什么整数指令 (MOVW) 也触发 INVSTATE？**

来自 [ARM 社区案例](https://community.arm.com/support-forums/f/embedded-forum/50521/debugging-invstate-usagefault-in-an-unexpected-location)：

> "The stacked PC points to an address **in the middle of a 32-bit instruction**"

**根因**：
1. CPU 启用了 FPU (`CPACR.CP10/11 = 3`)
2. 某次执行了 VFP 指令 → `CONTROL.FPCA=1` (FPU ACTIVE)
3. 之后发生异常 → 栈帧变成 **extended** (26 字 = 8 basic + 18 FPU)
4. 我们的 `naked` handler 假设栈帧是 **basic** (8 字)
5. 读 `PC` 时实际读到的是栈中 **错位 0x40 字节**处的"中间数据"
6. 这个"中间数据"看起来像 32-bit 指令的"中间半字" → 触发 INVSTATE

**修复**：handler 必须在读 PC 前检查 EXC_RETURN bit[4]：
```c
"tst   lr, #0x10       \n"   /* EXC_RETURN bit[4] = FPCA */
"addne r0, r0, #0x40   \n"   /* extended frame: 跳过 FPU ctx */
```

### 5.4 我们的故障诊断区（DTCM）

| 地址 | 名称 | 含义 |
|------|------|------|
| 0x20000028 | DEV_ABS_MAX | Test1 测试结果 |
| 0x2000002C | DEV_ABS_MAX_SMP | Test2 测试结果 |
| 0x20000030 | DEV_POS_MAX | 0xD1FF 等 |
| 0x20000034 | DEV_NEG_MAX | 进度标记：0xBEEF/B100/D1FF/D2FF/B200 |
| 0x20000040 | FAULT_CFSR | 故障时自动保存 |
| 0x20000044 | FAULT_HFSR | 故障时自动保存 |
| 0x20000048 | FAULT_MMFAR | |
| 0x2000004C | FAULT_BFAR | |
| 0x20000050 | FAULT_EXC_RETURN | |
| 0x20000054 | FAULT_FRAME_PTR | |

---

## 6. FPU / VFP 注意事项

### 6.1 启用 FPU

```c
SCB->CPACR |= ((3UL << 10*2) | (3UL << 11*2));   // CP10 + CP11 全访问
__DSB();
__ISB();
```

### 6.2 编译选项

```
-mfpu=fpv5-d16     // 单精度 VFPv5, 16 个双字寄存器
-mfloat-abi=hard   // 硬 ABI
-mthumb
```

### 6.3 关键点

- **FPU 启用后**, 所有中断 handler 必须保存 FPU 上下文 (s0-s15 + FPSCR + LR)
  否则中断返回时 FPU 状态错乱
- EXC_RETURN bit[4] = 1 表示 extended frame (含 FPU)
- EXC_RETURN bit[4] = 0 表示 basic frame (无 FPU)
- **如果 ISR 用了 VFP 指令但 handler 没保存 FPU 上下文，会触发 INVSTATE 或其他异常**

---

## 7. STM32H723 启动流程

### 7.1 Reset → main()

```
1. 内核读 MSP 从 0x0000_0000 (vector table)
2. 内核读 PC 从 0x0000_0004 (Reset_Handler)
3. Reset_Handler:
   a. 设置 MSP
   b. 调用 SystemInit() (SystemClock_Config, FPU 启用, ITCM 拷贝)
   c. 调用 __libc_init_array() (C++ 构造，全局变量初始化)
   d. 调用 main()
```

### 7.2 ITCM 拷贝

我们项目的 startup_stm32h723zgtx.s 中有 ITCM 拷贝代码，把 Flash 中 `.itcm_code` 段拷贝到 ITCM 0x0000_0000。

→ 拷贝完后 IT CM 0x000~0x707 已经有 GCC 编译的代码
→ 0x708+ 是空的，可写可执行

---

## 8. 工具链命令速查

### 8.1 pyocd 命令

```bash
# 读 4 字节
pyocd commander -t stm32h723xx -c "read32 0x20000034 4; exit"

# 读 ITCM 编译块
pyocd commander -t stm32h723xx -c "read32 0x00000800 16; exit"

# 读 Fault 寄存器
pyocd commander -t stm32h723xx -c "read32 0xE000ED28 16; exit"
```

### 8.2 GCC 编译 + 汇编查看

```bash
arm-none-eabi-gcc -mcpu=cortex-m7 -mthumb -mfpu=fpv5-d16 -mfloat-abi=hard -O2 -c test.c -o test.o
arm-none-eabi-objdump -d test.o
arm-none-eabi-objdump -h test.o   # 看 section 分布
```

### 8.3 链接脚本查看

```bash
arm-none-eabi-ld -T STM32H723ZGTX_FLASH.ld -Map=output.map ...
arm-none-eabi-readelf -S elf_file.elf   # 看 section 实际地址
```

---

## 9. 调试时序策略

### 9.1 进度标记协议

在 main.c 不同阶段写不同的标记值到固定 DTCM 地址：

| 阶段 | 标记值 |
|------|--------|
| 启动后 | 0x0000 |
| compile_routes 完成 | 0xBEEF |
| compile_routes 返回 | 0xB100 |
| Test1 开始 | 0xD100 |
| Test1 通过 | 0xD1FF |
| Test2 开始 | 0xD200 |
| Test2 通过 | 0xD2FF |
| 准备 TIM1 | 0xB200 |

### 9.2 失败定位

```
DEV_NEG_MAX = 0xBEEF  → 崩在 compile_routes
DEV_NEG_MAX = 0xB100  → 崩在返回 main 后
DEV_NEG_MAX = 0xD100  → 崩在 Test1 VLDR
DEV_NEG_MAX = 0xD200  → 崩在 Test2 编译块
DEV_NEG_MAX = 0xB200  → 全部通过，进入 ISR
```

如果 ISR 中崩：
- 检查 fault 时 ISR 是否在跑编译块
- 检查是否 INVSTATE / BusFault / HardFault

---

## 10. 常见陷阱

### 10.1 半字序陷阱

```c
// 错误：把"自然序"32-bit 值直接 store
*(uint32_t *)addr = 0xED940A00;
// 内存布局 (小端):
//   byte[0]=0x00, byte[1]=0x0A, byte[2]=0x94, byte[3]=0xED
//   halfword[0]=0x0A00, halfword[1]=0xED94  ← 反了!

// 正确：预交换半字
uint32_t le = (w >> 16) | (w << 16);
*(uint32_t *)addr = le;
// 内存布局:
//   byte[0]=0x94, byte[1]=0xED, byte[2]=0x00, byte[3]=0x0A
//   halfword[0]=0xED94, halfword[1]=0x0A00  ← 对了!
```

### 10.2 对齐陷阱

```c
emit_u32(MOVW);   // 4 字节对齐 ✓
emit_u16(BLX_R8); // 现在偏移 +2，下一条 u32 不再 4 字节对齐
emit_u32(VLDR);   // INVSTATE!  ← bug
```

**修复**：BLX 后补 NOP

```c
emit_u32(MOVW);
emit_u16(BLX_R8);
emit_u16(NOP);    // 恢复对齐
emit_u32(VLDR);   // ✓
```

### 10.3 链接器陷阱

- 链接脚本 `.itcm_code` 不能超出 64KB
- 启动拷贝代码必须正确处理 LMA ≠ VMA 的情况

### 10.4 I-Cache 陷阱

H723 默认 I-Cache 启用。如果 `USE_COMPILED_ISR=1` 但编译块地址曾被取指过（旧值），新代码不会生效。

→ 写完 ITCM 后必须 `SCB_InvalidateICache()`。

---

## 11. 待研究问题 (TODO)

- [x] ~~为什么 32-bit 整数指令 (MOVW) 在某些情况下也触发 INVSTATE？~~
  - **根因：FPU 上下文与栈帧错位**。当执行 VFP 指令后，CONTROL.FPCA=1，
    之后任何中断（含 HardFault）入栈时 EXC_RETURN bit[4]=1，栈帧变成 extended (26 字)，
    但我们的 naked handler 假设 basic (8 字) 格式读 PC，会读到栈中错位的"中间数据"。
  - **修复**：handler 必须先检查 `lr & 0x10`，是 1 则跳过 0x40 字节再读。
- [x] ~~0x0800 vs 0x0A00 为什么都"看似"可写可读，但行为不同？~~
  - **0x0800 是 4 字节对齐地址**，32-bit 指令能正确执行；
    **0x0A00 也是 4 字节对齐**——之前行为差异是 Test3/4 互相覆盖导致的。
  - 任何 32-bit Thumb-2 指令必须从 **4 字节对齐**地址开始执行。
- [x] ~~MDMA 拷贝 ITCM 是否比 CPU 写更稳定？~~
  - **是**。根据 ST 官方 MDMA 文档：
    > "The MDMA also features incrementing, decrementing or non-incrementing (fixed) addressing for source and destination."
    > "For the TCM memory accesses, the burst access is only allowed when the increment and data size are identical and lower than or equal to 32 bits."
  - **MDMA 是访问 ITCM 的官方方式**。ST 论坛原话："MDMA 可以访问 ITCM-RAM 和 DTCM-RAM"。
  - 但 CPU `*(volatile uint32_t*)0x800=val` 也可工作，ITCM 支持 CPU D-Bus 访问。

## 11.2 新增的诡异现象 (2026-06-30 解决中)

### 现象
- ITCM 编译块调用 prim_handler (ITCM 函数)
- BLX r8 后 LR=0x83B (正确)
- prim_handler bx lr 返回 0x83A (正确, 清 bit[0])
- 0x83A 是 NOP
- **但 PC=0x20005700 (= PARAM_BASE = r1)**

### 关键事实重审
- CFSR=0x20000 (UNDEFINSTR, bit[17])
- r1=0x20005700 (PARAM_BASE)
- r0=0x0E (OP_SCALE, 入参未变) — **这说明 fault 发生在 `subs` 之前!**
- prim_handler 在 ITCM 0x0 (`subs r0, #1` 编码 0x3801, 16-bit)
- VMOV 32-bit 在 0x2 (`vmov.f32 s14, s0` 编码 0xeeb0 7a40)
- cmp/bhi.n/tbh/vldr/vfma/vmov/bx lr 都不应崩

### 真正的问题: stacked R0 = 0x0E 表明 CPU 在 prim_handler 入口未执行任何指令
- 唯一合理解释: **`BLX` 到 prim_handler 后, 异常发生在 `subs` 执行前的某个隐藏步骤**
- **可能: I-Cache / ITCM 端口冲突**? **流水线旁路**? **TLB 预取**?

### 验证方案
1. **prim_handler 极简化**: 改成 `return src`, 看 fault 是否还发生 — **已实施, 等测试**
2. **bx lr 后 NOP 替换成 4 字节 NOP**: 看是否对齐问题
3. **prim_handler 入口加 4 字节 NOP padding**: 看是否 prim_handler 内部触发
4. **从 ISR 入口而不是 main 调用编译块**: 隔离上下文

### 状态 (2026-06-30)
- prim_handler 暂时简化成 `return src` (DEBUG-6), 等用户测试
- emit_movw(8, prim_handler) | 1 修复未解决问题 (因为不是 bit[0] 问题)
- `subs r0, #1` 编码确认 = 0x3801 (用 arm-none-eabi-as 验证)

## 11.1 关键修复（已实施）

### 修复 1: HardFault/UsageFault handler 读 extended frame
```c
"tst   lr, #0x10       \n"  /* EXC_RETURN bit[4] */
"addne r0, r0, #0x40   \n"  /* 跳过 FPU 上下文 (s0-s15+fpscr+lr'=0x40B) */
```
- 如果进入 handler 前 FPCA=1，则栈帧是 extended
- 跳过 0x40 字节后，从 `[r0+0]` 读到的 R0 是真正的 R0
- `PC` 在 `[r0+0x18]`，`xPSR` 在 `[r0+0x1C]`

### 修复 2: emit_blx 自带 NOP 保持对齐
```c
static void emit_blx(int rm) {
    emit_u16(0x4780 | (rm & 0xF));
    emit_u16(0xBF00);  /* NOP: 恢复 4 字节对齐 */
}
```

### 修复 3: emit_u32 预交换半字
```c
static inline void emit_u32(uint32_t w) {
    uint32_t le = (w >> 16) | (w << 16);
    *(volatile uint32_t *)cmp_p = le;
    cmp_p += 2;
}
```

---

## 12. 关键搜索词模板

以后出问题用这些关键词组合搜索：

```
"STM32H7" + "ITCM" + <症状>
"STM32H723" + "INVSTATE" 
"Cortex-M7" + "TCM" + "self-modifying"
"PM0253" + <章节>
"Thumb-2" + "alignment" + "32-bit instruction"
"STM32H7" + "I-Cache" + "invalidate"
```

---

## 13. 项目特定事实

- **ITCM 链接脚本范围**: 0x0000_0000 ~ 0x0000_FFFF (64KB)
- **CMP_BLK_BASE**: 0x0800
- **诊断区地址**: DTCM 0x20000000 起
- **进度标记地址**: 0x20000034 (DEV_NEG_MAX)
- **Fault 保存地址**: 0x20000040 起
- **TIM1 时基**: APB2 timer clock = 272MHz, ARR=135 → 1μs 周期
- **CPU 时钟**: 544MHz (VOS0, 超频)
- **GCC 编译参数**: `-mcpu=cortex-m7 -mthumb -mfpu=fpv5-d16 -mfloat-abi=hard -O2`

---

**维护提示**：每次有新发现就更新这份文档。新的故障 / 修复 / 资料都加到对应章节。
