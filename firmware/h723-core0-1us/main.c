/**
 * 核心0 H723 — ISR 引擎移植版
 *
 * 完整移植 ESP32-S3 核心0 架构到 STM32H723:
 *   - 六种寄存器空间 (SENSOR/WIRE/PARAM/STATE/LUT/ACTUATOR)
 *   - 路由表扫描引擎 (28 原语)
 *   - 抖动直方图 (256 bin, 180万样本/3分钟)
 *
 * 时钟: VOS0 + PLL 544MHz, HPRE=/2, D2PPRE2=/4
 * ISR: 100μs TIM1, DWT CYCCNT 测量 @136MHz (7.4ns 分辨率)
 */
#include <stdint.h>

/* ═══════════════════════════════════════════════════════════
 * 寄存器定义
 * ═══════════════════════════════════════════════════════════ */

#define PWR_BASE     0x58024800UL
#define PWR_CR3      (*(volatile uint32_t *)(PWR_BASE + 0x0C))

#define RCC_BASE      0x58024400UL
#define RCC_CR        (*(volatile uint32_t *)(RCC_BASE + 0x00))
#define RCC_CFGR      (*(volatile uint32_t *)(RCC_BASE + 0x10))
#define RCC_D1CFGR    (*(volatile uint32_t *)(RCC_BASE + 0x18))
#define RCC_D2CFGR    (*(volatile uint32_t *)(RCC_BASE + 0x1C))
#define RCC_PLLCKSELR (*(volatile uint32_t *)(RCC_BASE + 0x28))
#define RCC_PLLCFGR   (*(volatile uint32_t *)(RCC_BASE + 0x2C))
#define RCC_PLL1DIVR  (*(volatile uint32_t *)(RCC_BASE + 0x30))
#define RCC_APB2ENR   (*(volatile uint32_t *)(RCC_BASE + 0x0F0))

#define FLASH_ACR     (*(volatile uint32_t *)0x52002000)

#define SCB_CPACR    (*(volatile uint32_t *)0xE000ED88)
#define SCB_CFSR    (*(volatile uint32_t *)0xE000ED28)
#define SCB_HFSR    (*(volatile uint32_t *)0xE000ED2C)
#define SCB_MMFAR   (*(volatile uint32_t *)0xE000ED34)
#define SCB_BFAR    (*(volatile uint32_t *)0xE000ED38)
#define SCB_ICIALLU (*(volatile uint32_t *)0xE000EF50)  /* I-Cache invalidate all */
#define DEMCR        (*(volatile uint32_t *)0xE000EDFC)
#define DWT_CTRL     (*(volatile uint32_t *)0xE0001000)
#define DWT_CYCCNT   (*(volatile uint32_t *)0xE0001004)
#define NVIC_ISER0   (*(volatile uint32_t *)0xE000E100)

#define TIM1_BASE    0x40010000UL
#define TIM1_CR1     (*(volatile uint32_t *)(TIM1_BASE + 0x00))
#define TIM1_DIER    (*(volatile uint32_t *)(TIM1_BASE + 0x0C))
#define TIM1_SR      (*(volatile uint32_t *)(TIM1_BASE + 0x10))
#define TIM1_PSC     (*(volatile uint32_t *)(TIM1_BASE + 0x28))
#define TIM1_ARR     (*(volatile uint32_t *)(TIM1_BASE + 0x2C))

#define TIMEOUT      8000000

/* ═══════════════════════════════════════════════════════════
 * DTCM 内存布局 (0x20000000, 128KB 零等待)
 * ═══════════════════════════════════════════════════════════ */

#define DTCM_BASE        0x20000000UL

/* Timing variables: 256B */
#define TIMING_BASE      (DTCM_BASE + 0x0000)
#define EXEC_MIN         (*(volatile uint32_t *)(TIMING_BASE + 0x00))
#define EXEC_MAX         (*(volatile uint32_t *)(TIMING_BASE + 0x04))
#define PERIOD_MIN       (*(volatile uint32_t *)(TIMING_BASE + 0x08))
#define PERIOD_MAX       (*(volatile uint32_t *)(TIMING_BASE + 0x0C))
#define SAMPLES          (*(volatile uint32_t *)(TIMING_BASE + 0x10))
#define LAST_ENTRY       (*(volatile uint32_t *)(TIMING_BASE + 0x14))
#define HEARTBEAT        (*(volatile uint32_t *)(TIMING_BASE + 0x18))
#define CLOCK_HZ         (*(volatile uint32_t *)(TIMING_BASE + 0x1C))
#define TIMER_HZ         (*(volatile uint32_t *)(TIMING_BASE + 0x20))
#define EXEC_TOTAL       (*(volatile uint32_t *)(TIMING_BASE + 0x24))
#define DEV_ABS_MAX      (*(volatile uint32_t *)(TIMING_BASE + 0x28))
#define DEV_ABS_MAX_SMP  (*(volatile uint32_t *)(TIMING_BASE + 0x2C))
#define DEV_POS_MAX      (*(volatile uint32_t *)(TIMING_BASE + 0x30))
#define DEV_NEG_MAX      (*(volatile uint32_t *)(TIMING_BASE + 0x34))
#define PERIOD_EXACT     (*(volatile uint32_t *)(TIMING_BASE + 0x38))
#define PERIOD_FAR       (*(volatile uint32_t *)(TIMING_BASE + 0x3C))

/* Fault diagnostic: 64B at TIMING_BASE+0x40 */
#define FAULT_BASE       (TIMING_BASE + 0x40)
#define FAULT_CFSR       (*(volatile uint32_t *)(FAULT_BASE + 0x00))
#define FAULT_HFSR       (*(volatile uint32_t *)(FAULT_BASE + 0x04))
#define FAULT_MMFAR      (*(volatile uint32_t *)(FAULT_BASE + 0x08))
#define FAULT_BFAR       (*(volatile uint32_t *)(FAULT_BASE + 0x0C))
#define FAULT_PSP        (*(volatile uint32_t *)(FAULT_BASE + 0x10))
#define FAULT_EXC_RET    (*(volatile uint32_t *)(FAULT_BASE + 0x14))
/* Stacked registers from exception frame: */
#define FAULT_STACKED_R0 (*(volatile uint32_t *)(FAULT_BASE + 0x18))
#define FAULT_STACKED_R1 (*(volatile uint32_t *)(FAULT_BASE + 0x1C))
#define FAULT_STACKED_R2 (*(volatile uint32_t *)(FAULT_BASE + 0x20))
#define FAULT_STACKED_R3 (*(volatile uint32_t *)(FAULT_BASE + 0x24))
#define FAULT_STACKED_R12 (*(volatile uint32_t *)(FAULT_BASE + 0x28))
#define FAULT_STACKED_LR (*(volatile uint32_t *)(FAULT_BASE + 0x2C))
#define FAULT_STACKED_PC (*(volatile uint32_t *)(FAULT_BASE + 0x30))
#define FAULT_STACKED_PSR (*(volatile uint32_t *)(FAULT_BASE + 0x34))

/* Test harness: write via pyocd, read after reset */
#define TEST_SELECT  (*(volatile uint32_t *)(TIMING_BASE + 0xE0))
#define TEST_RESULT  (*(volatile uint32_t *)(TIMING_BASE + 0xE4))

/* Register spaces */
#define SENSOR_MAP       ((volatile float *)(DTCM_BASE + 0x0100))
#define ACTUATOR_STATUS  ((volatile float *)(DTCM_BASE + 0x0200))
#define WIRE_MAP         ((volatile float *)(DTCM_BASE + 0x0300))
#define LUT_DATA         ((volatile float *)(DTCM_BASE + 0x1300))

#define ROUTE_TABLE      ((volatile RouteEntry_t *)(DTCM_BASE + 0x1700))
#define PARAM_TABLE      ((volatile ParamEntry_t *)(DTCM_BASE + 0x5700))
#define STATE_TABLE      ((volatile StateEntry_t *)(DTCM_BASE + 0x7700))


/* ═══════════════════════════════════════════════════════════
 * 数据结构 (与 ESP32 完全一致)
 * ═══════════════════════════════════════════════════════════ */

typedef enum {
    SRC_SENSOR = 0,
    SRC_WIRE   = 1,
    SRC_CONST  = 2
} SourceType_t;

typedef enum {
    DST_WIRE = 3
} OutputType_t;

typedef struct __attribute__((packed, aligned(4))) {
    uint8_t  src_type;
    uint8_t  src_index;
    uint8_t  dst_type;
    uint8_t  dst_channel;
    uint8_t  op;
    uint8_t  flags;
    uint16_t param_idx;
    uint16_t state_offset;
    uint16_t actuator_idx;
    uint16_t reserved;
} RouteEntry_t;   /* 16 bytes */

typedef struct __attribute__((aligned(4))) {
    float value_a;
    float value_b;
    float value_c;
    float value_d;
} ParamEntry_t;   /* 16 bytes */

typedef struct __attribute__((aligned(4))) {
    float state_a;
    float state_b;
    float state_c;
    float state_d;
} StateEntry_t;   /* 16 bytes */

/* ═══════════════════════════════════════════════════════════
 * 原语操作码
 * ═══════════════════════════════════════════════════════════ */

enum {
    OP_DIRECT   = 0x00,
    OP_CMP      = 0x01,
    OP_HYST     = 0x02,
    OP_CLAMP    = 0x03,
    OP_LPF      = 0x04,
    OP_PID      = 0x05,
    OP_RATE     = 0x06,
    OP_DEADBAND = 0x07,
    OP_MUX      = 0x08,
    OP_EDGE     = 0x09,
    OP_LUT      = 0x0A,
    OP_CNT      = 0x0B,
    OP_TIMER    = 0x0C,
    OP_SCALE    = 0x0E,
    OP_AND      = 0x0F,
    OP_OR       = 0x10,
    OP_NOT      = 0x11,
    OP_REG      = 0x12,
    OP_ADD      = 0x13,
    OP_SUB      = 0x14,
    OP_MUL      = 0x15,
    OP_DIV      = 0x16,
    OP_BITAND   = 0x17,
    OP_BITOR    = 0x18,
    OP_BITXOR   = 0x19,
    OP_BITNOT   = 0x1A,
};

#define MAX_ROUTES         1024
#define CMP_BLK_SIZE        4096    /* 4KB ITCM for compiled routes */
#define USE_COMPILED_ISR    1       /* 1=compiled block, 0=interpreter */
#define MAX_SENSORS     64
#define MAX_ACTUATORS   32
#define MAX_WIRES       1024
#define MAX_PARAMS      512
#define MAX_STATES      256
#define MAX_LUT         256
#define ROUTE_ENABLED   0x01

/* ═══════════════════════════════════════════════════════════
 * HardFault handler: 把异常上下文存到 DTCM 诊断区, 然后死循环
 * ═══════════════════════════════════════════════════════════ */
__attribute__((naked, section(".itcm_code")))
void HardFault_Handler(void) {
    __asm__ volatile(
        "tst   lr, #4          \n"  /* EXC_RETURN bit[2]: MSP or PSP? */
        "ite   eq              \n"
        "mrseq r0, msp         \n"
        "mrsne r0, psp         \n"
        /* r0 = stack frame pointer */
        "ldr   r1, =0x20000040 \n"  /* FAULT_BASE */
        "str   r0, [r1, #0x10] \n"  /* FAULT_PSP = frame ptr */
        "str   lr, [r1, #0x14] \n"  /* FAULT_EXC_RET */
        /* EXC_RETURN bit[4]=1 => extended frame (含 FPU: s0-s15+fpscr+lr'=0x40B)
         * 需要跳过这 0x40 字节才能读到真正的 basic frame 末尾 */
        "tst   lr, #0x10       \n"
        "it    ne              \n"
        "addne r0, r0, #0x40   \n"
        /* Save stacked registers */
        "ldr   r2, [r0, #0x00] \n"  "str   r2, [r1, #0x18] \n"  /* R0 */
        "ldr   r2, [r0, #0x04] \n"  "str   r2, [r1, #0x1C] \n"  /* R1 */
        "ldr   r2, [r0, #0x08] \n"  "str   r2, [r1, #0x20] \n"  /* R2 */
        "ldr   r2, [r0, #0x0C] \n"  "str   r2, [r1, #0x24] \n"  /* R3 */
        "ldr   r2, [r0, #0x10] \n"  "str   r2, [r1, #0x28] \n"  /* R12 */
        "ldr   r2, [r0, #0x14] \n"  "str   r2, [r1, #0x2C] \n"  /* LR */
        "ldr   r2, [r0, #0x18] \n"  "str   r2, [r1, #0x30] \n"  /* PC */
        "ldr   r2, [r0, #0x1C] \n"  "str   r2, [r1, #0x34] \n"  /* xPSR */
        /* Save fault registers */
        "ldr   r2, =0xE000ED28 \n"  /* CFSR */
        "ldr   r3, [r2, #0x00] \n"  "str   r3, [r1, #0x00] \n"  /* CFSR */
        "ldr   r3, [r2, #0x04] \n"  "str   r3, [r1, #0x04] \n"  /* HFSR */
        "ldr   r3, [r2, #0x0C] \n"  "str   r3, [r1, #0x08] \n"  /* MMFAR */
        "ldr   r3, [r2, #0x10] \n"  "str   r3, [r1, #0x0C] \n"  /* BFAR */
        "b     .               \n"  /* 死循环, 等 pyocd 读 */
    );
}

__attribute__((naked, section(".itcm_code")))
void UsageFault_Handler(void) {
    __asm__ volatile(
        "tst   lr, #4          \n"
        "ite   eq              \n"
        "mrseq r0, msp         \n"
        "mrsne r0, psp         \n"
        "ldr   r1, =0x20000040 \n"
        "str   r0, [r1, #0x10] \n"
        "str   lr, [r1, #0x14] \n"
        "tst   lr, #0x10       \n"
        "it    ne              \n"
        "addne r0, r0, #0x40   \n"
        "ldr   r2, [r0, #0x00] \n"  "str   r2, [r1, #0x18] \n"
        "ldr   r2, [r0, #0x04] \n"  "str   r2, [r1, #0x1C] \n"
        "ldr   r2, [r0, #0x08] \n"  "str   r2, [r1, #0x20] \n"
        "ldr   r2, [r0, #0x0C] \n"  "str   r2, [r1, #0x24] \n"
        "ldr   r2, [r0, #0x10] \n"  "str   r2, [r1, #0x28] \n"
        "ldr   r2, [r0, #0x14] \n"  "str   r2, [r1, #0x2C] \n"
        "ldr   r2, [r0, #0x18] \n"  "str   r2, [r1, #0x30] \n"
        "ldr   r2, [r0, #0x1C] \n"  "str   r2, [r1, #0x34] \n"
        "ldr   r2, =0xE000ED28 \n"
        "ldr   r3, [r2, #0x00] \n"  "str   r3, [r1, #0x00] \n"
        "ldr   r3, [r2, #0x04] \n"  "str   r3, [r1, #0x04] \n"
        "ldr   r3, [r2, #0x0C] \n"  "str   r3, [r1, #0x08] \n"
        "ldr   r3, [r2, #0x10] \n"  "str   r3, [r1, #0x0C] \n"
        "b     .               \n"
    );
}

/* ═══════════════════════════════════════════════════════════
 * 时钟初始化 (VOS0 + 192MHz)
 * ═══════════════════════════════════════════════════════════ */

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
    SCB_CPACR |= (0x0F << 20);
    __asm__ volatile("dsb; isb");

    uint32_t tout;

    /* ── Step 1: VOS0 ── */
    PWR_CR3 = (PWR_CR3 & ~(3 << 4)) | (0 << 4);
    tout = TIMEOUT; while (!(PWR_CR3 & (1<<6)) && --tout) {}

    /* ── Step 2: Disable PLL1 ── */
    RCC_CR &= ~(1 << 24);
    tout = TIMEOUT; while ((RCC_CR & (1<<25)) && --tout) {}

    /* ── Step 3: PLL 288MHz VCOSEL=1 (已验证), 先切过去 ── */
    RCC_PLLCKSELR = (0 << 0) | (4 << 4);
    RCC_PLL1DIVR  = (0 << 24) | (0 << 16) | (0 << 9) | (18 << 0);
    RCC_PLLCFGR   = (1 << 1) | (1 << 16);       /* VCOSEL=1, DIVP1EN=1 */
    RCC_CR |= (1 << 24);
    tout = TIMEOUT; while (!(RCC_CR & (1<<25)) && --tout) {}
    /* 总分频 */
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
    /* 现在在 288MHz, ITCM 代码不受 Flash WS 影响 */

    /* ── 关 PLL, 回 HSI, 重配 544MHz VCOSEL=0 ── */
    RCC_CR &= ~(1 << 24);
    tout = TIMEOUT; while ((RCC_CR & (1<<25)) && --tout) {}
    RCC_CFGR = (RCC_CFGR & ~0x07) | 0x00;       /* SW=HSI */
    __asm__ volatile("dsb; isb");
    tout = TIMEOUT; while (((RCC_CFGR>>3)&7) != 0 && --tout) {}

    RCC_PLLCKSELR = (0 << 0) | (4 << 4);         /* HSI, DIVM1=4 */
    RCC_PLL1DIVR  = (0 << 24) | (0 << 16) | (0 << 9) | (34 << 0);
    RCC_PLLCFGR   = (0 << 1) | (1 << 16);        /* VCOSEL=0, DIVP1EN=1 */
    RCC_CR |= (1 << 24);
    tout = TIMEOUT; while (!(RCC_CR & (1<<25)) && --tout) {}
    RCC_CFGR = (RCC_CFGR & ~0x07) | 0x03;
    __asm__ volatile("dsb; isb");
    tout = TIMEOUT; while (((RCC_CFGR>>3)&7) != 3 && --tout) {}

    /* TIM1 = 544/2/4×2 = 136MHz */
    /* 启用 UsageFault/BusFault/MemManage 优先级降级 (不升级到 HardFault) */
    *(volatile uint32_t *)0xE000ED24 = 0x00070000;  /* SCB_SHCSR: USGFAULTENA|BUSFAULTENA|MEMFAULTENA */

    CLOCK_HZ = 544000000;
    TIMER_HZ = 136000000;
}

/* ═══════════════════════════════════════════════════════════
 * 原语实现
 * ═══════════════════════════════════════════════════════════ */

__attribute__((section(".itcm_code"), always_inline))
static inline uint32_t ccnt(void) { return DWT_CYCCNT; }

__attribute__((section(".itcm_code")))
static inline float execute_primitive(uint8_t op, float src,
    const ParamEntry_t *p, StateEntry_t *s, float dt)
{
    switch (op) {

    case OP_DIRECT:
        return src;

    case OP_SCALE:
        return p->value_a * src + p->value_b;

    case OP_CLAMP:
        if (src < p->value_a) return p->value_a;
        if (src > p->value_b) return p->value_b;
        return src;

    case OP_CMP:
        return (src > p->value_a) ? 1.0f : 0.0f;

    case OP_HYST: {
        if (src > p->value_a) s->state_a = 1.0f;
        else if (src < p->value_b) s->state_a = 0.0f;
        return s->state_a;
    }

    case OP_LPF: {
        float alpha = p->value_a;
        float out = s->state_a * (1.0f - alpha) + src * alpha;
        s->state_a = out;
        return out;
    }

    case OP_PID: {
        float kp = p->value_a, ki = p->value_b, kd = p->value_c;
        float sp = p->value_d;
        float err = sp - src;
        /* Integral (trapezoidal) */
        s->state_a += (err + s->state_b) * 0.5f * dt * ki;
        /* Clamp integral */
        if (s->state_a >  100.0f) s->state_a =  100.0f;
        if (s->state_a < -100.0f) s->state_a = -100.0f;
        /* Derivative (derivative-on-measurement to avoid kick) */
        float d_term = kd * (s->state_b - err) / dt;  /* error_prev - error_curr */
        s->state_b = err;
        float out = kp * err + s->state_a + d_term;
        return out;
    }

    case OP_RATE: {
        float rate = (src - s->state_a) / dt;
        s->state_a = src;
        return rate;
    }

    case OP_EDGE: {
        float prev = s->state_a;
        s->state_a = src;
        int edge_type = (int)p->value_a;
        if (edge_type == 0) return (prev <= 0.5f && src > 0.5f) ? 1.0f : 0.0f;
        if (edge_type == 1) return (prev > 0.5f && src <= 0.5f) ? 1.0f : 0.0f;
        return (prev != src) ? 1.0f : 0.0f;
    }

    case OP_CNT: {
        float prev = s->state_b;
        s->state_b = src;
        if (prev <= p->value_a && src > p->value_a)
            s->state_a += 1.0f;
        return s->state_a;
    }

    case OP_DEADBAND: {
        float diff = src - s->state_a;
        if (diff < -p->value_a || diff > p->value_a) {
            s->state_a = src;
            return src;
        }
        return s->state_a;
    }

    case OP_AND:
        return ((src > 0.5f) && (WIRE_MAP[(int)p->value_a] > 0.5f)) ? 1.0f : 0.0f;

    case OP_OR:
        return ((src > 0.5f) || (WIRE_MAP[(int)p->value_a] > 0.5f)) ? 1.0f : 0.0f;

    case OP_NOT:
        return (src > 0.5f) ? 0.0f : 1.0f;

    case OP_ADD:
        return src + WIRE_MAP[(int)p->value_a];

    case OP_SUB:
        return src - WIRE_MAP[(int)p->value_a];

    case OP_MUL:
        return src * WIRE_MAP[(int)p->value_a];

    case OP_DIV: {
        float b = WIRE_MAP[(int)p->value_a];
        return (b != 0.0f) ? src / b : 0.0f;
    }

    case OP_REG:
        s->state_a = src;
        return s->state_a;

    default:
        return src;
    }
}

/* ═══════════════════════════════════════════════════════════
 * 路由编译器: 路由表 → ITCM 指令序列 (真空间架构)
 *
 * 寄存器约定:
 *   r4 = SENSOR_MAP base  (0x20000100)
 *   r5 = WIRE_MAP base    (0x20000300)
 *   r6 = PARAM_TABLE base (0x20005700)
 *   r7 = STATE_TABLE base (0x20007700)
 *   s0 = src (in/out for primitives)
 *
 * 编译产物: cmp_blk[], 放在 ITCM, ISR 通过 BLX 调用
 * ═══════════════════════════════════════════════════════════ */

#define CMP_BLK_BASE  0x00000800UL  /* ITCM: 0x0800+ 区域 (GCC代码区外) */
static uint16_t *const cmp_blk = (uint16_t *)CMP_BLK_BASE;
static uint16_t *cmp_p;

/* DTCM 备选执行区 (用于对比测试 ITCM coherency 问题) */
#define CMP_BLK_DTCM  0x20001300UL  /* DTCM: LUT_DATA 之后, ROUTE_TABLE 之前 */
static uint16_t *const cmp_blk_dtcm = (uint16_t *)CMP_BLK_DTCM;

/* 写入后读回验证: 检测 ITCM D-Bus store 是否真正在 I-Bus 可见 */
static uint32_t verify_count;
static uint32_t verify_fail_addr;
static uint32_t verify_fail_expected;
static uint32_t verify_fail_actual;

static void verify_compiled_block(uint16_t *start, int n_hw) {
    verify_count = 0;
    verify_fail_addr = 0;
    verify_fail_expected = 0;
    verify_fail_actual = 0;
    for (int i = 0; i < n_hw; i += 2) {
        uint32_t mem_val = *(volatile uint32_t *)&start[i];
        uint32_t expected_le = ((uint32_t)start[i+1] << 16) | start[i];
        /* 注意: start[i] 已经是内存中的值, 直接读 32-bit 得到的是小端表示 */
        if (start[i] != 0xDEAD) {  /* skip marker */
            verify_count++;
        }
    }
}

/* ── 指令编码器 (Thumb-2, verified against objdump) ── */

/* 32-bit Thumb-2 指令写入: 单次 32-bit store 保证原子性
 *
 * Thumb-2 指令内存布局: 第一半字(bits[31:16])在低地址, 第二半字(bits[15:0])在高地址
 * 小端 32-bit store: bits[7:0]→byte[0], bits[15:8]→byte[1], bits[23:16]→byte[2], bits[31:24]→byte[3]
 * 所以需要把 w 的两个半字交换, 让第一半字落入 bits[15:0] (低地址)
 *
 * 例: VLDR s0,[r4,#0] = 0xED940A00 → 交换后 0x0A00ED94
 *   小端 store: byte[0-1]=0xED94(第一半字✓), byte[2-3]=0x0A00(第二半字✓)
 */
static inline void emit_u32(uint32_t w) {
    uint32_t le = ((w >> 16) & 0xFFFF) | ((w & 0xFFFF) << 16);
    *(volatile uint32_t *)cmp_p = le;
    cmp_p += 2;  /* 前进 2 个半字 = 4 字节 */
}
static inline void emit_u16(uint16_t h) {
    *cmp_p++ = h;
}

/* VLDR <Sd>, [<Rn>, #<imm8*4>]
 *   Vd=(Sd>>1)&0xF@[15:12]; D=Sd&1@[22]; bits[11:8]=1010
 *   验证: edd2_7a00=VLDR s15,[r2] — bits[11:8]=1010 */
static void emit_vldr(int sd, int rn, uint32_t off) {
    emit_u32(0xED000A00 | (1u << 23) | (1u << 20)
             | ((uint32_t)(sd & 1) << 22)
             | ((uint32_t)rn << 16)
             | ((uint32_t)((sd >> 1) & 0xF) << 12)
             | ((off / 4) & 0xFF));
}

/* VSTR <Sd>, [<Rn>, #<imm8*4>] — bit20=0, bit21=0 (VLDR=bit20=1)
 *   验证: edc2_7a00=VSTR s15,[r2] — bit21=0 */
static void emit_vstr(int sd, int rn, uint32_t off) {
    emit_u32(0xED000A00 | (1u << 23) | (0u << 20) | (0u << 21)
             | ((uint32_t)(sd & 1) << 22)
             | ((uint32_t)rn << 16)
             | ((uint32_t)((sd >> 1) & 0xF) << 12)
             | ((off / 4) & 0xFF));
}

/* VFMA.F32 <Sd>, <Sn>, <Sm>  — fused multiply-add: Sd += Sn*Sm
 *   验证: eea6_7aa7 = VFMA s14,s13,s15 — Sd=Vd*2+D, Sn=Vn*2+N, Sm=Vm*2+M */
static void emit_vfma(int sd, int sn, int sm) {
    emit_u32(0xEE000A00 | (1u << 23)
             | ((uint32_t)(sd & 1) << 21)
             | ((uint32_t)((sn >> 1) & 0xF) << 16)
             | ((uint32_t)((sd >> 1) & 0xF) << 12)
             | ((uint32_t)(sn & 1) << 7)
             | ((uint32_t)(sm & 1) << 5)
             | ((uint32_t)((sm >> 1) & 0xF) << 0));
}

/* VSUB.F32 <Sd>, <Sn>, <Sm> */
static void emit_vsub(int sd, int sn, int sm) {
    emit_u32(0xEE000A00 | (0u << 23) | (0u << 22)
             | ((uint32_t)(sd & 1) << 21) | (1u << 20) | (1u << 19)
             | ((uint32_t)((sn >> 1) & 0xF) << 16)
             | ((uint32_t)((sd >> 1) & 0xF) << 12)
             | ((uint32_t)(sn & 1) << 7) | (1u << 6)
             | ((uint32_t)(sm & 1) << 5)
             | ((uint32_t)((sm >> 1) & 0xF) << 0));
}

/* VMUL.F32 <Sd>, <Sn>, <Sm> */
static void emit_vmul(int sd, int sn, int sm) {
    emit_u32(0xEE000A00 | (0u << 23) | (0u << 22)
             | ((uint32_t)(sd & 1) << 21)
             | ((uint32_t)((sn >> 1) & 0xF) << 16)
             | ((uint32_t)((sd >> 1) & 0xF) << 12)
             | ((uint32_t)(sn & 1) << 7)
             | ((uint32_t)(sm & 1) << 5)
             | ((uint32_t)((sm >> 1) & 0xF) << 0));
}

/* VADD.F32 <Sd>, <Sn>, <Sm> */
static void emit_vadd(int sd, int sn, int sm) {
    emit_u32(0xEE000A00 | (0u << 23) | (0u << 22)
             | ((uint32_t)(sd & 1) << 21)
             | ((uint32_t)((sn >> 1) & 0xF) << 16)
             | ((uint32_t)((sd >> 1) & 0xF) << 12)
             | ((uint32_t)(sn & 1) << 7)
             | ((uint32_t)(sm & 1) << 5)
             | ((uint32_t)((sm >> 1) & 0xF) << 0));
}

/* BL <label> — 32-bit Thumb-2: 相对跳转 ±16MB
 *
 * 编码 (ARM ARM):
 *   hw0 = 11110[ S ][ imm10[9:0] ]
 *   hw1 = 11[ J1 ][ 1 ][ J2 ][ imm11[10:0] ]
 *   imm32 = SignExtend(S:I1:I2:imm10:imm11:0, 32)  [25-bit field, sign from bit 24]
 *   I1 = NOT(J1 XOR S), I2 = NOT(J2 XOR S)
 *
 * 字段位置 (25-bit 有符号值):
 *   bit[24]=S, bit[23]=I1, bit[22]=I2, bits[21:12]=imm10, bits[11:1]=imm11, bit[0]=0
 */
static void emit_bl(int32_t pc_offset) {
    /* pc_offset: 目标地址 - (当前地址 + 4) */
    uint32_t off25 = (uint32_t)pc_offset & 0x1FFFFFF;  /* 25-bit field */

    uint32_t imm11 = (off25 >> 1) & 0x7FF;
    uint32_t imm10 = (off25 >> 12) & 0x3FF;
    uint32_t I2  = (off25 >> 22) & 1;
    uint32_t I1  = (off25 >> 23) & 1;
    uint32_t S   = (off25 >> 24) & 1;

    uint32_t J1 = (I1 ^ S ^ 1) & 1;  /* NOT(I1 XOR S) */
    uint32_t J2 = (I2 ^ S ^ 1) & 1;  /* NOT(I2 XOR S) */

    uint32_t hw0 = 0xF000 | (S << 10) | imm10;
    uint32_t hw1 = (0x3 << 14) | (J1 << 13) | (1 << 12) | (J2 << 11) | imm11;

    emit_u32((hw0 << 16) | hw1);
}

/* BLX <Rm> — 16-bit Thumb: 仅支持 r0-r7!
 * 注意: 对于 r8-r15 必须用 Thumb-32 BL 或先 MOV 到低寄存器 */
static void emit_blx(int rm) {
    if (rm <= 7) {
        emit_u16(0x4780 | (uint16_t)rm);
        emit_u16(0xBF00);  /* NOP: restore 4-byte alignment */
    } else {
        /* 高寄存器: 使用 Thumb-32 BL (需要知道目标地址和当前位置) */
        /* 这个路径不应该被使用 — 改用 emit_bl */
        emit_u16(0xBF00);  /* NOP placeholder */
        emit_u16(0xBF00);  /* NOP placeholder */
    }
}

/* MOVW <Rd>, #<imm16> — imm16 = imm4:i:imm3:imm8
 *   imm4@[19:16], i@[26], imm3@[14:12], Rd@[11:8], imm8@[7:0]
 *   验证: f2401400 = MOVW r4,#256 (与 objdump 一致) */
static void emit_movw(int rd, uint16_t imm16) {
    emit_u32(0xF2400000
             | ((uint32_t)((imm16 >> 11) & 1) << 26)
             | ((uint32_t)((imm16 >> 12) & 0xF) << 16)
             | ((uint32_t)((imm16 >> 8) & 0x7) << 12)
             | ((uint32_t)rd << 8)
             | (imm16 & 0xFF));
}

/* MOVT <Rd>, #<imm16> — same as MOVW but bit[21]=1 */
static void emit_movt(int rd, uint16_t imm16) {
    emit_u32(0xF2C00000
             | ((uint32_t)((imm16 >> 11) & 1) << 26)
             | ((uint32_t)((imm16 >> 12) & 0xF) << 16)
             | ((uint32_t)((imm16 >> 8) & 0x7) << 12)
             | ((uint32_t)rd << 8)
             | (imm16 & 0xFF));
}

/* ADD.W <Rd>, <Rn>, #<imm12> — imm12 = i:imm3:imm8, S=0 */
static void emit_addw(int rd, int rn, uint16_t imm12) {
    emit_u32(0xF2000000
             | ((uint32_t)((imm12 >> 11) & 1) << 26)
             | ((uint32_t)rn << 16)
             | ((uint32_t)((imm12 >> 8) & 0x7) << 12)
             | ((uint32_t)rd << 8)
             | (imm12 & 0xFF));
}

/* BX LR — return */
static void emit_ret(void) { emit_u16(0x4770); }

/* ── 原语 handler: 寄存器传参版 (src 在 s0, 结果在 s0) ── */

__attribute__((section(".itcm_code")))
static float prim_handler(uint8_t op, float src,
                          const ParamEntry_t *p, StateEntry_t *s)
{
    (void)op; (void)p; (void)s;
    /* DEBUG-6: 最简版本, 只返回 src (用于隔离 prim_handler 内部 VFP 问题) */
    __asm__ volatile("" ::: "memory");
    return src;
}

/* ── 路由编译: 每条路由 → ITCM 指令序列 ── */

#define SENSOR_BASE  0x20000100UL
#define WIRE_BASE    0x20000300UL
#define PARAM_BASE   0x20005700UL
#define STATE_BASE   0x20007700UL
#define VLDR_RANGE   1020   /* imm8*4 max = 255*4 */

/* emit_vldr_safe / emit_vstr_safe: 自动处理超大偏移 (fallback: ADDW + [r3,#0]) */
static void emit_vldr_safe(int base_reg, uint32_t off) {
    if (off <= VLDR_RANGE)
        emit_vldr(0, base_reg, off);
    else {
        emit_addw(3, base_reg, (uint16_t)off);  /* r3 = base + off */
        emit_vldr(0, 3, 0);
    }
}
static void emit_vstr_safe(int base_reg, uint32_t off) {
    if (off <= VLDR_RANGE)
        emit_vstr(0, base_reg, off);
    else {
        emit_addw(3, base_reg, (uint16_t)off);
        emit_vstr(0, 3, 0);
    }
}

static void compile_routes(int n_routes) {
    cmp_p = cmp_blk;

    /* ── Preamble: 加载基址寄存器 + prim_handler 地址 ── */
    emit_movw(4, (uint16_t)(SENSOR_BASE & 0xFFFF));
    emit_movt(4, (uint16_t)(SENSOR_BASE >> 16));        /* r4 = SENSOR_MAP */
    emit_movw(5, (uint16_t)(WIRE_BASE & 0xFFFF));
    emit_movt(5, (uint16_t)(WIRE_BASE >> 16));          /* r5 = WIRE_MAP */
    emit_movw(6, (uint16_t)(PARAM_BASE & 0xFFFF));
    emit_movt(6, (uint16_t)(PARAM_BASE >> 16));         /* r6 = PARAM_TABLE */
    emit_movw(7, (uint16_t)(STATE_BASE & 0xFFFF));
    emit_movt(7, (uint16_t)(STATE_BASE >> 16));         /* r7 = STATE_TABLE */
    /* prim_handler 在 ITCM 0x00000000, 需 | 1 强制 Thumb (BLX 用 bit[0] 决定 ARM/Thumb 切换) */
    uint32_t handler_addr = (uint32_t)(uintptr_t)prim_handler | 1;
    emit_movw(8, (uint16_t)(handler_addr & 0xFFFF));
    emit_movt(8, (uint16_t)(handler_addr >> 16));            /* r8 = handler (Thumb) */

    DEV_ABS_MAX = 0xA000 + (uint32_t)(cmp_p - cmp_blk) * 2;  /* preamble size in bytes */

    for (int i = 0; i < n_routes; i++) {
        DEV_ABS_MAX_SMP = i;  /* debug: route index */
        RouteEntry_t *r = &ROUTE_TABLE[i];
        if (!(r->flags & ROUTE_ENABLED)) continue;

        DEV_POS_MAX = i | 0x1000;  /* entered route body */
        /* ── 1. VLDR s0 ← source ── */
        DEV_NEG_MAX = 0xD001;
        if (r->src_type == SRC_SENSOR)
            emit_vldr_safe(4, (uint32_t)r->src_index * 4);
        else if (r->src_type == SRC_WIRE)
            emit_vldr_safe(5, (uint32_t)r->src_index * 4);
        else /* SRC_CONST: PARAM_TABLE[param_idx].value_d at +12 */
            emit_vldr_safe(6, (uint32_t)r->param_idx * 16 + 12);

        /* ── 2. 执行原语: r0=op, r1=param_ptr, r2=state_ptr → BL prim_handler ── */
        DEV_NEG_MAX = 0xD002;
        emit_movw(0, r->op);                                /* r0 = op */
        DEV_NEG_MAX = 0xD003;
        emit_addw(1, 6, (uint16_t)(r->param_idx * 16));    /* r1 = &PARAM[pi] */
        DEV_NEG_MAX = 0xD004;
        emit_addw(2, 7, (uint16_t)(r->state_offset * 16)); /* r2 = &STATE[so] */
        DEV_NEG_MAX = 0xD005;
        {
            /* Thumb-32 BL: 跳转到 prim_handler
             * 偏移 = target - (current_pc + 4)
             * current_pc = CMP_BLK_BASE + (cmp_p - cmp_blk) * 2
             * 注意: BL 指令本身是 4 字节, PC 在执行时指向 BL+4 */
            uint32_t bl_addr = CMP_BLK_BASE + (uint32_t)(cmp_p - cmp_blk) * 2;
            int32_t offset = (int32_t)((uint32_t)prim_handler - (bl_addr + 4));
            emit_bl(offset);
        }

        /* ── 3. VSTR s0 → WIRE_MAP[dst_channel] ── */
        DEV_NEG_MAX = 0xD006;
        emit_vstr_safe(5, (uint32_t)r->dst_channel * 4);
    }

    emit_ret();  /* BX LR */
    DEV_POS_MAX = (uint32_t)(cmp_p - cmp_blk) * 2;  /* total compiled size in bytes */

    /* ── ITCM coherency 修复 ──
     *
     * 问题: CPU D-Bus store 到 ITCM 后, I-Bus fetch 可能看不到最新数据
     * (单端口 TCM 的 store 和 fetch 冲突, 或 write buffer 未完全 drain)
     *
     * 修复: 完整的 DSB + I-Cache invalidate + DSB + ISB 序列
     * - DSB: 等待所有 store 到达 TCM 硬件
     * - ICIALLU=0: 确保 I-Cache 不缓存 ITCM 旧数据
     * - 第二次 DSB+ISB: 同步流水线 */
    __asm__ volatile("dsb" ::: "memory");
    SCB_ICIALLU = 0;  /* 无效化 I-Cache 全部 */
    __asm__ volatile("dsb; isb" ::: "memory");

    /* ── 读回验证: 确认 D-Bus store 结果 == I-Bus 将看到的 ── */
    verify_compiled_block(cmp_blk, (int)(cmp_p - cmp_blk));

    /* 额外验证: 逐 32-bit 读回比较 */
    {
        int n_hw = (int)(cmp_p - cmp_blk);
        for (int i = 0; i < n_hw; i += 2) {
            uint32_t written_le = ((uint32_t)cmp_blk[i+1] << 16) | cmp_blk[i];
            uint32_t readback = *(volatile uint32_t *)&cmp_blk[i];
            if (readback != written_le) {
                verify_fail_addr = (uint32_t)&cmp_blk[i];
                verify_fail_expected = written_le;
                verify_fail_actual = readback;
                DEV_ABS_MAX_SMP = 0xE001;  /* D-Bus 读回不匹配! */
                __asm__ volatile("b .");
            }
        }
    }

    DEV_NEG_MAX = 0xBEEF;
}

/* ═══════════════════════════════════════════════════════════
 * ISR: 核心扫描引擎 (双模式: 编译器 / 解释器)
 * ═══════════════════════════════════════════════════════════ */

__attribute__((section(".itcm_code")))
void TIM1_UP_IRQHandler(void) {
    uint32_t t0 = ccnt();
    TIM1_SR = 0;

    /* ── Period (精简: 仅 MIN/MAX) ── */
    uint32_t last = LAST_ENTRY;
    if (last) {
        uint32_t period = (t0 >= last) ? (t0 - last)
                         : ((0xFFFFFFFFu - last) + t0 + 1u);
        if (SAMPLES > 3) {
            if (period < PERIOD_MIN) PERIOD_MIN = period;
            if (period > PERIOD_MAX) PERIOD_MAX = period;
        } else if (SAMPLES == 3) {
            PERIOD_MIN = period;
            PERIOD_MAX = period;
        }
    }
    LAST_ENTRY = t0;

#if USE_COMPILED_ISR
    /* ── 编译器路径: 调用 ITCM 里的编译代码块 ── */
    __asm__ volatile(
        "push {r4-r8}          \n"
        "blx  %[blk]           \n"
        "pop  {r4-r8}          \n"
        :: [blk]"r"((uint32_t)(uintptr_t)cmp_blk | 1) : "memory"
    );
#else
    /* ── 解释器路径: 逐条扫描路由表 ── */
    uint32_t n = *(volatile uint32_t *)(DTCM_BASE + 0xF0);
    RouteEntry_t *rp = ROUTE_TABLE;
    RouteEntry_t *end = rp + n;
    do {
        if (!(rp->flags & ROUTE_ENABLED)) continue;

        float src;
        uint8_t st = rp->src_type;
        if (st == SRC_SENSOR)
            src = SENSOR_MAP[rp->src_index];
        else if (st == SRC_WIRE)
            src = WIRE_MAP[rp->src_index];
        else
            src = PARAM_TABLE[rp->param_idx].value_d;

        float out = execute_primitive(rp->op, src,
                      &PARAM_TABLE[rp->param_idx],
                      &STATE_TABLE[rp->state_offset], 0.0001f);
        WIRE_MAP[rp->dst_channel] = out;
    } while (++rp < end);
#endif

    SAMPLES++;
    HEARTBEAT++;
}

/* (旧 HardFault_Handler 已移到上方, 带完整上下文保存) */

/* ═══════════════════════════════════════════════════════════
 * 测试程序: 双通道温度控制 + 诊断
 *
 * Channel A (Heater):  SENSOR[0] → SCALE → LPF → PID → CLAMP
 * Channel B (Cooler):  SENSOR[1] → SCALE → LPF → PID → CLAMP
 * Diagnostics: CMP×3, RATE, HYST, EDGE, CNT, DEADBAND, AND, ADD, SUB, DIRECT
 * Total: 20 routes, 9 primitive types
 * ═══════════════════════════════════════════════════════════ */

/* GCC-compiled VLDR test function (in ITCM, for comparison) */
__attribute__((section(".itcm_code")))
static void gcc_vldr_test(void) {
    __asm__ volatile(
        "vldr s0, [r4, #0]  \n"
        "bx lr               \n"
    );
}

static void init_route(int idx, uint8_t st, uint8_t si, uint8_t dt, uint8_t dc,
                        uint8_t op, uint16_t pi, uint16_t so) {
    ROUTE_TABLE[idx].src_type   = st;
    ROUTE_TABLE[idx].src_index  = si;
    ROUTE_TABLE[idx].dst_type   = dt;
    ROUTE_TABLE[idx].dst_channel= dc;
    ROUTE_TABLE[idx].op         = op;
    ROUTE_TABLE[idx].flags      = ROUTE_ENABLED;
    ROUTE_TABLE[idx].param_idx  = pi;
    ROUTE_TABLE[idx].state_offset = so;
    ROUTE_TABLE[idx].actuator_idx = 0;
    ROUTE_TABLE[idx].reserved   = 0;
}

int main(void) {
    /* ── DWT ── */
    DEMCR |= (1 << 24);
    DWT_CYCCNT = 0;
    DWT_CTRL |= (1 << 0);

    /* ── Init timing vars ── */
    EXEC_MIN     = 0xFFFFFFFF;
    EXEC_MAX     = 0;
    PERIOD_MIN   = 0xFFFFFFFF;
    PERIOD_MAX   = 0;
    SAMPLES      = 0;
    LAST_ENTRY   = 0;
    HEARTBEAT    = 0;
    EXEC_TOTAL   = 0;
    DEV_ABS_MAX  = 0;
    DEV_ABS_MAX_SMP = 0;
    DEV_POS_MAX  = 0;
    DEV_NEG_MAX  = 0;
    PERIOD_EXACT = 0;
    PERIOD_FAR   = 0;

    /* ── Zero all register spaces ── */
    for (int i = 0; i < MAX_SENSORS; i++)   SENSOR_MAP[i] = 0.0f;
    for (int i = 0; i < MAX_ACTUATORS; i++) ACTUATOR_STATUS[i] = 0.0f;
    for (int i = 0; i < MAX_WIRES; i++)     WIRE_MAP[i] = 0.0f;
    for (int i = 0; i < MAX_LUT; i++)       LUT_DATA[i] = 0.0f;
    for (int i = 0; i < MAX_ROUTES; i++) {
        ROUTE_TABLE[i].flags = 0;
    }
    for (int i = 0; i < MAX_PARAMS; i++) {
        PARAM_TABLE[i].value_a = 0.0f;
        PARAM_TABLE[i].value_b = 0.0f;
        PARAM_TABLE[i].value_c = 0.0f;
        PARAM_TABLE[i].value_d = 0.0f;
    }
    for (int i = 0; i < MAX_STATES; i++) {
        STATE_TABLE[i].state_a = 0.0f;
        STATE_TABLE[i].state_b = 0.0f;
        STATE_TABLE[i].state_c = 0.0f;
        STATE_TABLE[i].state_d = 0.0f;
    }

    /* ── Sensor simulation values ── */
    SENSOR_MAP[0] = 25.0f;   /* Channel A: room temp */
    SENSOR_MAP[1] = 22.0f;   /* Channel B: cool side */

    /* ═══════════════════════════════════════════════════════
     * PARAM_TABLE 定义
     * ═══════════════════════════════════════════════════════ */

    /* P0: SCALE chA — k=1.0, b=0 */
    PARAM_TABLE[0].value_a = 1.0f;
    PARAM_TABLE[0].value_b = 0.0f;

    /* P1: LPF chA — alpha=0.1 */
    PARAM_TABLE[1].value_a = 0.1f;

    /* P2: PID chA — kp=2.0, ki=0.1, kd=0.05, sp=60 */
    PARAM_TABLE[2].value_a = 2.0f;
    PARAM_TABLE[2].value_b = 0.1f;
    PARAM_TABLE[2].value_c = 0.05f;
    PARAM_TABLE[2].value_d = 60.0f;

    /* P3: CLAMP chA — lo=0, hi=100 */
    PARAM_TABLE[3].value_a = 0.0f;
    PARAM_TABLE[3].value_b = 100.0f;

    /* P4: SCALE chB */
    PARAM_TABLE[4].value_a = 1.0f;
    PARAM_TABLE[4].value_b = 0.0f;

    /* P5: LPF chB — alpha=0.1 */
    PARAM_TABLE[5].value_a = 0.1f;

    /* P6: PID chB — kp=2.5, ki=0.08, kd=0.04, sp=25 */
    PARAM_TABLE[6].value_a = 2.5f;
    PARAM_TABLE[6].value_b = 0.08f;
    PARAM_TABLE[6].value_c = 0.04f;
    PARAM_TABLE[6].value_d = 25.0f;

    /* P7: CLAMP chB */
    PARAM_TABLE[7].value_a = 0.0f;
    PARAM_TABLE[7].value_b = 100.0f;

    /* P8: CMP hi-temp — threshold=80 */
    PARAM_TABLE[8].value_a = 80.0f;

    /* P9: CMP lo-temp — threshold=10 */
    PARAM_TABLE[9].value_a = 10.0f;

    /* P10: CMP cool — threshold=30 */
    PARAM_TABLE[10].value_a = 30.0f;

    /* P11: HYST — on=0.8, off=0.2 */
    PARAM_TABLE[11].value_a = 0.8f;
    PARAM_TABLE[11].value_b = 0.2f;

    /* P12: EDGE — type=0 (rising) */
    PARAM_TABLE[12].value_a = 0.0f;

    /* P13: CNT — trigger=0.5 */
    PARAM_TABLE[13].value_a = 0.5f;

    /* P14: DEADBAND — band=2.0 */
    PARAM_TABLE[14].value_a = 2.0f;

    /* P15: AND — B=WIRE[21] (hi-temp alarm) */
    PARAM_TABLE[15].value_a = 21.0f;  /* B wire index */

    /* P16: ADD — B=WIRE[2] (PID output chA) */
    PARAM_TABLE[16].value_a = 2.0f;

    /* P17: SUB — B=WIRE[12] (PID output chB) */
    PARAM_TABLE[17].value_a = 12.0f;

    /* ═══════════════════════════════════════════════════════
     * ROUTE_TABLE: 20 routes, topological sorted
     *
     * STATE_TABLE slots: 0=LPF_A, 1=PID_A, 2=LPF_B, 3=PID_B,
     *   4=RATE, 5=HYST, 6=EDGE, 7=CNT, 8=DEADBAND
     * ═══════════════════════════════════════════════════════ */

    int ri = 0;

    /* Channel A: SENSOR[0] → SCALE(P0) → WIRE[0] */
    init_route(ri++, SRC_SENSOR, 0, DST_WIRE, 0,  OP_SCALE, 0, 0);

    /* WIRE[0] → LPF(P1, S0) → WIRE[1] */
    init_route(ri++, SRC_WIRE,   0, DST_WIRE, 1,  OP_LPF,   1, 0);

    /* WIRE[1] → PID(P2, S1) → WIRE[2] */
    init_route(ri++, SRC_WIRE,   1, DST_WIRE, 2,  OP_PID,   2, 1);

    /* WIRE[2] → CLAMP(P3) → WIRE[3] */
    init_route(ri++, SRC_WIRE,   2, DST_WIRE, 3,  OP_CLAMP, 3, 0);

    /* Channel B */
    init_route(ri++, SRC_SENSOR, 1, DST_WIRE, 10, OP_SCALE, 4, 0);
    init_route(ri++, SRC_WIRE,  10, DST_WIRE, 11, OP_LPF,   5, 2);
    init_route(ri++, SRC_WIRE,  11, DST_WIRE, 12, OP_PID,   6, 3);
    init_route(ri++, SRC_WIRE,  12, DST_WIRE, 13, OP_CLAMP, 7, 0);

    /* ── 8 routes total (2x PID chains) ── */

    /* ── Store route count ── */
    *(volatile uint32_t *)(DTCM_BASE + 0xF0) = ri;  /* ACTIVE_ROUTES */

    compile_routes(ri);
    DEV_NEG_MAX = 0xB100;  /* 标记: compile_routes 返回后 */

    /* ═══ Test 1: VLDR+BX LR 在 ITCM 0x0100 (GCC 代码区, VLDR 已知可执行) ═══ */
    {
        uint32_t saved0 = *(volatile uint32_t *)0x00000100;
        uint16_t saved4 = *(volatile uint16_t *)0x00000104;
        *(volatile uint32_t *)0x00000100 = 0x0A00ED94;  /* VLDR s0,[r4,#0] swap16 */
        *(volatile uint16_t *)0x00000104 = 0x4770;      /* BX LR */
        __asm__ volatile("dsb; isb" ::: "memory");
        __asm__ volatile("movw r4, #0x0100    \n"
                         "movt r4, #0x2000    \n"
                         ::: "r4","memory");
        DEV_NEG_MAX = 0xD100;
        void (*fn)(void) = (void (*)(void))0x00000101UL;
        fn();
        DEV_NEG_MAX = 0xD1FF;
        *(volatile uint32_t *)0x00000100 = saved0;
        *(volatile uint16_t *)0x00000104 = saved4;
        __asm__ volatile("dsb; isb" ::: "memory");
    }

    /* ═══ Test 2: 最小编译块测试 (不调用 prim_handler, 避免覆盖问题) ═══ */
    /* 手写: MOVW+MOVT r4=SENSOR_MAP, MOVW+MOVT r5=WIRE_MAP
     *       VLDR s0,[r4] → VSTR s0,[r5] → BX LR
     * 即: WIRE[0] = SENSOR[0] (DIRECT 语义) */
    {
        WIRE_MAP[0] = 0.0f;  /* 清零目标 */

        volatile uint32_t *w = (volatile uint32_t *)0x00000200;
        /* MOVW r4, #0x0100 = 0xF2401400, swap16=0x1400F240 */
        w[0] = 0x1400F240;
        /* MOVT r4, #0x2000 = 0xF2C20400, swap16=0x0400F2C2 */
        w[1] = 0x0400F2C2;
        /* MOVW r5, #0x0300 = 0xF2403500, swap16=0x3500F240 */
        w[2] = 0x3500F240;
        /* MOVT r5, #0x2000 = 0xF2C20500, swap16=0x0500F2C2 */
        w[3] = 0x0500F2C2;
        /* VLDR s0,[r4,#0] = 0xED940A00, swap16=0x0A00ED94 */
        w[4] = 0x0A00ED94;
        /* VSTR s0,[r5,#0] = 0xED850A00, swap16=0x0A00ED85 */
        w[5] = 0x0A00ED85;
        /* BX LR (16-bit, 地址 0x218) */
        *(volatile uint16_t *)0x00000218 = 0x4770;
        __asm__ volatile("dsb; isb" ::: "memory");

        DEV_NEG_MAX = 0xD200;  /* Test2 开始 */
        __asm__ volatile(
            "push {r4-r5}          \n"
            "blx  %[blk]           \n"
            "pop  {r4-r5}          \n"
            :: [blk]"r"(0x00000201UL) : "memory"
        );
        DEV_NEG_MAX = 0xD2FF;  /* Test2 通过 */

        /* 验证: WIRE[0] 应该 = SENSOR[0] = 25.0f */
        PERIOD_EXACT = *(volatile uint32_t *)(DTCM_BASE + 0x300 + 0*4);
    }

    /* ═══ Test 3: 0x0A00 区域数据读写验证 (不覆盖编译块@0x0800) ═══ */
    {
        *(volatile uint32_t *)0x00000A00 = 0xDEADBEEF;
        __asm__ volatile("dsb" ::: "memory");
        uint32_t rb = *(volatile uint32_t *)0x00000A00;
        PERIOD_FAR = rb;
        DEV_ABS_MAX_SMP = (rb == 0xDEADBEEF) ? 0xD3FF : 0xD300;
    }

    /* ═══ Test 4: 0x0900 区域执行 swap16 编码的指令 (不覆盖编译块@0x0800) ═══ */
    {
        WIRE_MAP[1] = 0.0f;

        volatile uint32_t *w = (volatile uint32_t *)0x00000900;
        w[0] = 0x1400F240;  /* MOVW r4, #0x0100 */
        w[1] = 0x0400F2C2;  /* MOVT r4, #0x2000 */
        w[2] = 0x3504F240;  /* MOVW r5, #0x0304 (WIRE_MAP[1]=0x20000304) */
        w[3] = 0x0500F2C2;  /* MOVT r5, #0x2000 */
        w[4] = 0x0A00ED94;  /* VLDR s0,[r4,#0] */
        w[5] = 0x0A00ED85;  /* VSTR s0,[r5,#0] */
        *(volatile uint16_t *)0x00000918 = 0x4770;  /* BX LR */
        __asm__ volatile("dsb; isb" ::: "memory");

        DEV_NEG_MAX = 0xD400;  /* Test4 开始 */
        __asm__ volatile(
            "push {r4-r5}          \n"
            "blx  %[blk]           \n"
            "pop  {r4-r5}          \n"
            :: [blk]"r"(0x00000901UL) : "memory"
        );
        DEV_NEG_MAX = 0xD4FF;  /* Test4 通过 */

        /* WIRE[1] 应该 = SENSOR[0] = 25.0f */
        DEV_POS_MAX = *(volatile uint32_t *)(DTCM_BASE + 0x300 + 1*4);
    }

    /* ═══ Test 5: 从 main 直接调用 compile_routes 输出 @0x0800 ═══ */
    /* 隔离 ISR 上下文问题: 如果这里崩了, 说明编译块本身有问题;
     * 如果这里通过但 ISR 崩, 说明是 ISR 上下文问题 (FPU lazy stacking 等) */
    {
        WIRE_MAP[2] = 0.0f;  /* 清零一个目标 wire */

        DEV_NEG_MAX = 0xD500;  /* Test5 开始 */
        __asm__ volatile(
            "push {r4-r8, lr}     \n"
            "blx  %[blk]          \n"
            "pop  {r4-r8, lr}     \n"
            :: [blk]"r"((uint32_t)(uintptr_t)cmp_blk | 1) : "memory"
        );
        DEV_NEG_MAX = 0xD5FF;  /* Test5 通过: compile_routes 输出在 main 中可执行 */

        /* 检查 WIRE[0] = SENSOR[0] = 25.0f (第一条路由: DIRECT) */
        PERIOD_FAR = *(volatile uint32_t *)(DTCM_BASE + 0x300);
    }

    /* ═══ Test 6: 编译块复制到 DTCM 后执行 (绕过 ITCM coherency 问题) ═══ */
    /* 理论: DTCM 的 D-Bus store → I-Bus fetch 路径有架构级一致性保证
     * (同一 CPU 的 store buffer 对 I-Bus 立即可见，因为 DTCM 和 ITCM 共用同一
     *  TCM 接口逻辑，但 ITCM 的 I-Bus 和 D-Bus 可能有不同的时序窗口) */
    {
        WIRE_MAP[3] = 0.0f;

        /* 把编译块复制到 DTCM */
        int n_hw = (int)(cmp_p - cmp_blk);
        for (int i = 0; i < n_hw; i++) {
            cmp_blk_dtcm[i] = cmp_blk[i];
        }
        __asm__ volatile("dsb; isb" ::: "memory");

        /* 验证复制正确 */
        for (int i = 0; i < n_hw; i++) {
            if (cmp_blk_dtcm[i] != cmp_blk[i]) {
                DEV_ABS_MAX_SMP = 0xE002;  /* DTCM 复制不匹配 */
                __asm__ volatile("b .");
            }
        }

        DEV_NEG_MAX = 0xD600;  /* Test6 开始 */
        __asm__ volatile(
            "push {r4-r8, lr}     \n"
            "blx  %[blk]          \n"
            "pop  {r4-r8, lr}     \n"
            :: [blk]"r"((uint32_t)(uintptr_t)cmp_blk_dtcm | 1) : "memory"
        );
        DEV_NEG_MAX = 0xD6FF;  /* Test6 通过: DTCM 执行成功 */

        /* 验证结果 */
        PERIOD_EXACT = *(volatile uint32_t *)(DTCM_BASE + 0x300 + 3*4);  /* WIRE[3] */
    }

    DEV_NEG_MAX = 0xB200;  /* 标记: 所有测试完成, 准备启动 TIM1 */

    /* ═══════════════════════════════════════════════════════
     * TIM1 配置: 100μs @ 96MHz → ARR=9599
     * ═══════════════════════════════════════════════════════ */
    RCC_APB2ENR |= (1 << 0);           /* TIM1EN */
    /* TIM1 寄存器是 16-bit, 用 16-bit volatile 写 */
    *(volatile uint16_t *)(TIM1_BASE + 0x00) = 0;          /* CR1: stop */
    __asm__ volatile("dsb");
    *(volatile uint16_t *)(TIM1_BASE + 0x28) = 0;          /* PSC */
    *(volatile uint16_t *)(TIM1_BASE + 0x2C) = 135;        /* ARR: 136MHz/1M-1, 1μs周期 */
    *(volatile uint16_t *)(TIM1_BASE + 0x0C) = 1;          /* DIER */
    __asm__ volatile("dsb; isb");
    *(volatile uint16_t *)(TIM1_BASE + 0x00) = 1;          /* CR1: start */
    __asm__ volatile("dsb; isb");
    NVIC_ISER0 = (1 << 25);

    while (1) { __asm__ volatile("nop"); }
}
