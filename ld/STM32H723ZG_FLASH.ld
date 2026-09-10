/*
 * STM32H723ZG_FLASH.ld — DCL 引擎 H723 链接脚本
 *
 * 与 ST 模板的关键差异: **显式定义 DTCM / ITCM 段**。
 * 这是本次迁移的核心价值所在 (见 MIGRATE-H723.md §4):
 *   - DTCM 128KB @0x20000000: 零等待无 cache, 放**所有数据表** → 访问时间恒定
 *   - ITCM  64KB @0x00000000: 零等待无 cache, 放 **ISR 热代码** → 执行时间恒定
 *   放 AXI SRAM 的代价是双重的: ① cache 命中不可预测 ② HCLK = CPU/2, 每次
 *   AXI 访问要花 2 个 CPU 周期 (见 §3.3)。
 *
 * 内存地图 (STM32H723ZG):
 *   FLASH 1024K @0x08000000     代码 + 常量 (经 L1 cache)
 *   ITCM    64K @0x00000000     热代码 (零等待)
 *   DTCM   128K @0x20000000     栈 + .data/.bss + 数据表 (零等待)
 *   AXI    320K @0x24000000     大缓冲 (非热路径)
 */

ENTRY(Reset_Handler)

_estack = ORIGIN(DTCM) + LENGTH(DTCM);   /* 栈顶 = DTCM 末端 */

_Min_Heap_Size  = 0x200;
_Min_Stack_Size = 0x1000;                /* 4KB: ISR 也在这条栈上 */

MEMORY
{
  ITCM  (xrw) : ORIGIN = 0x00000000, LENGTH = 64K
  DTCM  (xrw) : ORIGIN = 0x20000000, LENGTH = 128K
  FLASH (rx)  : ORIGIN = 0x08000000, LENGTH = 1024K
  AXI   (xrw) : ORIGIN = 0x24000000, LENGTH = 320K
}

SECTIONS
{
  /* ---- 向量表 + 代码: 留在 FLASH (由 L1 I-cache 加速) ---- */
  .isr_vector : ALIGN(4)
  {
    . = ALIGN(4);
    KEEP(*(.isr_vector))
    . = ALIGN(4);
  } >FLASH

  .text : ALIGN(4)
  {
    . = ALIGN(4);
    *(.text)
    *(.text*)
    *(.glue_7)
    *(.glue_7t)
    *(.eh_frame)
    KEEP(*(.init))
    KEEP(*(.fini))
    . = ALIGN(4);
    _etext = .;
  } >FLASH

  .rodata : ALIGN(4)
  {
    . = ALIGN(4);
    *(.rodata)
    *(.rodata*)
    . = ALIGN(4);
  } >FLASH

  .ARM.extab : ALIGN(4)
  {
    *(.ARM.extab* .gnu.linkonce.armextab.*)
  } >FLASH

  .ARM : ALIGN(4)
  {
    __exidx_start = .;
    *(.ARM.exidx*)
    __exidx_end = .;
  } >FLASH

  .preinit_array : ALIGN(4)
  {
    PROVIDE_HIDDEN(__preinit_array_start = .);
    KEEP(*(.preinit_array*))
    PROVIDE_HIDDEN(__preinit_array_end = .);
  } >FLASH

  .init_array : ALIGN(4)
  {
    PROVIDE_HIDDEN(__init_array_start = .);
    KEEP(*(SORT(.init_array.*)))
    KEEP(*(.init_array*))
    PROVIDE_HIDDEN(__init_array_end = .);
  } >FLASH

  .fini_array : ALIGN(4)
  {
    PROVIDE_HIDDEN(__fini_array_start = .);
    KEEP(*(SORT(.fini_array.*)))
    KEEP(*(.fini_array*))
    PROVIDE_HIDDEN(__fini_array_end = .);
  } >FLASH

  /* ---- ITCM: 热代码 (用 __attribute__((section(".itcm_text"))) 放置) ----
   * 符号名必须与 ST 启动文件的 LoopCopyItcmInit 一致: _siitcm/_sitcm/_eitcm
   * 现在为空, 阶段 2 把拍 ISR 搬进来 */
  .itcm_text : ALIGN(4)
  {
    . = ALIGN(4);
    _siitcm = LOADADDR(.itcm_text);
    _sitcm = .;
    *(.itcm_text)
    *(.itcm_text*)
    . = ALIGN(4);
    _eitcm = .;
  } >ITCM AT>FLASH

  /* ---- DTCM: 已初始化数据 (.data), 装载地址在 FLASH ---- */
  .data : ALIGN(4)
  {
    . = ALIGN(4);
    _sidata = LOADADDR(.data);
    _sdata = .;
    *(.data)
    *(.data*)
    . = ALIGN(4);
    _edata = .;
  } >DTCM AT>FLASH

  /* ---- DTCM: 未初始化数据 (.bss) ---- */
  .bss (NOLOAD) : ALIGN(4)
  {
    . = ALIGN(4);
    _sbss = .;
    __bss_start__ = _sbss;
    *(.bss)
    *(.bss*)
    *(COMMON)
    . = ALIGN(4);
    _ebss = .;
    __bss_end__ = _ebss;
  } >DTCM

  /* ---- DTCM: 数据表区 (SHM) — 引擎的路由/参数/状态表放这里
   * NOLOAD: 不上电初始化为 0, 由固件在冷启动时显式清零 (cold_start_reset 语义) */
  .dtcm_shm (NOLOAD) : ALIGN(8)
  {
    . = ALIGN(8);
    _shm_start = .;
    KEEP(*(.dtcm_shm))
    . = ALIGN(8);
    _shm_end = .;
  } >DTCM

  /* ---- AXI SRAM: 大缓冲 (非热路径) ---- */
  .axi_buf (NOLOAD) : ALIGN(32)
  {
    . = ALIGN(32);
    _axi_start = .;
    KEEP(*(.axi_buf))
    . = ALIGN(32);
    _axi_end = .;
  } >AXI

  /* ---- 栈/堆检查 ---- */
  ._user_heap_stack (NOLOAD) : ALIGN(8)
  {
    . = ALIGN(8);
    PROVIDE(end = .);
    PROVIDE(_end = .);
    . = . + _Min_Heap_Size;
    . = . + _Min_Stack_Size;
    . = ALIGN(8);
  } >DTCM

  /* 注: 不写 /DISCARD/ { libc.a(*) ... } —— ST 模板里有这一段, 但它会把
   * newlib 的函数段一并丢弃, 一旦用到 printf/memcpy 之外的库函数就会静默
   * 链接失败或行为异常。交给 -Wl,--gc-sections 做正常的死代码回收即可。 */

  .ARM.attributes 0 : { *(.ARM.attributes) }
}

/* 提供 DTCM 段符号给固件做自检 (阶段 2 会加 SHM 布局断言) */
ASSERT(_shm_end <= ORIGIN(DTCM) + LENGTH(DTCM), "DTCM overflow: 数据表放不下")
ASSERT(_eitcm <= ORIGIN(ITCM) + LENGTH(ITCM), "ITCM overflow: 热代码放不下")
