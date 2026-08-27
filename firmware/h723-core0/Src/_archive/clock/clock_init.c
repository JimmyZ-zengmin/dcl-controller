/**
 * 时钟树初始化: VOS0 + PLL 544MHz
 *
 * 流程:
 *   1. 使能 FPU (CP10/CP11)
 *   2. VOS0 电压等级
 *   3. 临时切到 288MHz (VCOSEL=1, 稳定过渡)
 *   4. 关 PLL, 切 HSI
 *   5. 重配 544MHz (VCOSEL=0)
 *   6. Flash 等待周期 (3WS)
 *   7. VTOR = 0x08000000
 */
#include "clock_init.h"
#include "dtcm_layout.h"

/* 编码辅助 */
static uint32_t hpre_enc(uint32_t d) {
    if (d<=1) return 0; if (d==2) return 8;
    if (d==4) return 9; if (d==8) return 10;
    if (d==16) return 11; return 12;
}

static uint32_t d2ppre_enc(uint32_t d) {
    if (d<=1) return 0; if (d==2) return 4;
    if (d==4) return 5; return 6;
}

void SystemInit(void) {
    uint32_t tout;

    /* ── FPU ── */
    SCB_CPACR |= (0x0F << 20);
    __asm__ volatile("dsb; isb");

    /* ── Step 1: VOS0 (H723: PWR_CR3 @ 0x0C, VOS[5:4], VOSRDY[6])
     * H723 LDO 自动运行, 无需手动使能。VOS0 支持最高 550MHz ── */
    PWR_CR3 = (PWR_CR3 & ~(3UL << 4)) | (0UL << 4);   /* VOS[5:4]=00 → VOS0 */
    tout = TIMEOUT;
    while (!(PWR_CR3 & (1UL << 6)) && --tout) {}        /* 等待 VOSRDY */
    if (!tout) { while(1) {} }

    /* ── Step 2: Disable PLL1 ── */
    RCC_CR &= ~(1 << 24);
    tout = TIMEOUT; while ((RCC_CR & (1<<25)) && --tout) {}

    /* ── Step 3: PLL 200MHz VCOSEL=1 过渡 (Scale 2 safe) ── */
    /* PLL1 input = HSI/4 = 16MHz, VCO = 16*25 = 400MHz, DIVP=2 → 200MHz */
    RCC_PLLCKSELR = (0 << 0) | (4 << 4);
    RCC_PLL1DIVR  = (0 << 24) | (0 << 16) | (1 << 9) | (25 << 0);
    RCC_PLLCFGR   = (1 << 1) | (1 << 16);       /* VCOSEL=1, DIVP1EN=1 */
    RCC_CR |= (1 << 24);
    tout = TIMEOUT; while (!(RCC_CR & (1<<25)) && --tout) {}

    /* D1CFGR: HPRE=/2; D2CFGR: D2PPRE2=/4 */
    { uint32_t v = *(volatile uint32_t *)(RCC_BASE+0x1C);
      v &= ~((7<<4)|(7<<8)); v |= (5<<4)|(5<<8);
      *(volatile uint32_t *)(RCC_BASE+0x1C) = v; }
    { uint32_t v = *(volatile uint32_t *)(RCC_BASE+0x18);
      v &= ~0xF; v |= (8 << 0);
      *(volatile uint32_t *)(RCC_BASE+0x18) = v; }
    FLASH_ACR = 0x324;
    __asm__ volatile("dsb; isb"); (void)FLASH_ACR;
    RCC_CFGR = (RCC_CFGR & ~0x07) | 0x03;
    __asm__ volatile("dsb; isb");
    tout = TIMEOUT; while (((RCC_CFGR>>3)&7) != 3 && --tout) {}

    /* ── Step 4: 关 PLL, 回 HSI ── */
    RCC_CR &= ~(1 << 24);
    tout = TIMEOUT; while ((RCC_CR & (1<<25)) && --tout) {}
    RCC_CFGR = (RCC_CFGR & ~0x07) | 0x00;
    __asm__ volatile("dsb; isb");
    tout = TIMEOUT; while (((RCC_CFGR>>3)&7) != 0 && --tout) {}

    /* ── Step 5: PLL 400MHz VCOSEL=0 (Scale 2 max freq) ── */
    /* PLL1 input = HSI/4 = 16MHz, VCO = 16*50 = 800MHz, DIVP=2 → 400MHz */
    RCC_PLLCKSELR = (0 << 0) | (4 << 4);
    RCC_PLL1DIVR  = (0 << 24) | (0 << 16) | (1 << 9) | (50 << 0);  /* DIVN=50, DIVP=2 */
    RCC_PLLCFGR   = (0 << 1) | (1 << 16);        /* VCOSEL=0, DIVP1EN=1 */
    RCC_CR |= (1 << 24);
    tout = TIMEOUT; while (!(RCC_CR & (1<<25)) && --tout) {}
    RCC_CFGR = (RCC_CFGR & ~0x07) | 0x03;
    __asm__ volatile("dsb; isb");
    tout = TIMEOUT; while (((RCC_CFGR>>3)&7) != 3 && --tout) {}

    /* ── 记录频率 ── */
    CLOCK_HZ = 400000000;
    TIMER_HZ = 100000000;

    /* ── VTOR ── */
    SCB_VTOR = 0x08000000UL;
}
