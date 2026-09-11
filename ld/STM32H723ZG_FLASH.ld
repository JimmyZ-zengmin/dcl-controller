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
  /* ---- 向量表: 实体留在 FLASH; **运行期拷进 ITCM 并把 VTOR 指过去** ----
   * ★★ 为什么不就地留在 flash (2026-09-11 实测): VTOR 原值 = 0x08000000 (flash),
   *   而 **sector erase 会 stall flash 取指** ⇒ 擦除期间**中断根本进不来** ——
   *   100μs 拍被整段吞掉。"ISR 代码在 ITCM" 不够: **取向量这一步本身也在 flash**。
   *   拷到 ITCM 后取向量零等待 ⇒ 引擎的拍与 flash 操作彻底解耦。
   * ★ A/B 实证 (tools/h723_tick_erase.py, 擦除期间每 100ms 采拍计数, 标称 1000 拍/槽):
   *     -DDCL_VTOR_ITCM=0 (留 flash): 第 7~14 槽 Δ=0, 累计丢 **8153** 拍
   *     -DDCL_VTOR_ITCM=1 (本行以下): 全程无 Δ=0, 丢 **0** 拍
   *   ⇒ 存量在 flash 的向量表就是"擦除期间整机失聪"的根因之一。 */
  .isr_vector : ALIGN(4)
  {
    . = ALIGN(4);
    _siv = .;
    KEEP(*(.isr_vector))
    . = ALIGN(4);
    _eiv = .;
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

  /* ---- ★ 落位敏感实验用的两个专区 (AUDIT-H723-stage2.md H8) ----
   * 目的: 让"engine_scan_flash 的落位"成为**唯一变量**。
   *   .scan_pad   编译期填充 (SCAN_FLASH_PAD 字节) —— 只挪地址, 不改指令
   *   .scan_flash FLASH 版扫描体独占一段, 紧跟在填充之后
   * 两者都能被 -DSCAN_FLASH_PAD=N 平移, 而 ITCM 版扫描体 (VMA 固定 0x0)
   * 完全不受影响 —— 后者就是"控制组必须纹丝不动"的对照。
   * 没有这个旋钮, "flash 落位敏感"就只能是相关性 (见审计报告 §8)。 */
  .scan_pad : ALIGN(4)
  {
    . = ALIGN(4);
    _scan_pad_start = .;
    KEEP(*(.scan_pad))
    . = ALIGN(4);
    _scan_pad_end = .;
  } >FLASH

  .scan_flash : ALIGN(4)
  {
    . = ALIGN(4);
    _scan_flash_start = .;
    KEEP(*(.scan_flash))
    . = ALIGN(4);
    _scan_flash_end = .;
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

  /* ---- ITCM: **向量表副本** (NOLOAD: 内容由 main() 在"使能任何中断之前"从 flash 拷入) ----
   * VMA 紧跟 .itcm_text 之后 (128B 对齐 —— VTOR 的要求); 占 1KB
   * (H723 有 16 系统 + ~150 IRQ 个向量 ≈ 664B, 留到 1KB 有余量)。
   * ★ 为什么用 NOLOAD 而不是 AT>FLASH: 让**拷贝这件事在代码里显式可见**
   *   (main() 里那几行), 而不是藏在启动文件的自动搬运循环里 —— 这段的顺序性
   *   ("必须先拷再开中断") 是正确性要求, 值得写在一眼能看到的地方。 */
  .itcm_vectors (NOLOAD) : ALIGN(128)
  {
    . = ALIGN(128);
    _vtor_itcm = .;
    . = . + 1024;
    _evtor_itcm = .;
  } >ITCM

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
