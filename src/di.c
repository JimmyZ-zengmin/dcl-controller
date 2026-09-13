/*
 * di.c — DI 数字量输入 (W5 外设域)。见 di.h 说明 (语义逐字保留 S3)。
 */
#include "di.h"
#include "itcm.h"   /* ★ ISR 调用树必须住 ITCM —— 见该头文件 */
#include "engine.h"
#include "regs.h"

static const uint32_t DI_PINS[DI_COUNT] = { DI_PIN_PC0, DI_PIN_PC1, DI_PIN_PC2, DI_PIN_PC3 };
static uint8_t s_last[DI_COUNT];
static uint8_t s_cnt[DI_COUNT];
static uint8_t s_stable[DI_COUNT];

static void di_pin_input_pullup(uint32_t pin)
{
    uint32_t port = pin >> 4, bit = pin & 15u;
    RCC_AHB4ENR |= (1u << port);
    uint32_t m = GPIO_MODER(port);
    m &= ~(3u << (bit * 2u));                 /* 00 = 输入 */
    GPIO_MODER(port) = m;
    uint32_t p = GPIO_PUPDR(port);
    p &= ~(3u << (bit * 2u));
    p |=  (1u << (bit * 2u));                 /* 01 = 上拉 (悬空=1, 接 GND=0) */
    GPIO_PUPDR(port) = p;
}

static inline uint32_t di_pin_read(uint32_t pin)
{
    return (GPIO_IDR(pin >> 4) >> (pin & 15u)) & 1u;
}

void di_init(uint8_t *base)
{
    for (int i = 0; i < DI_COUNT; i++) {
        di_pin_input_pullup(DI_PINS[i]);
        s_last[i] = 1; s_cnt[i] = DI_DEBOUNCE; s_stable[i] = 1;
    }
    /* 上电默认值入槽 (引擎在 START 前就能读到真实输入, 同 S3) */
    for (int i = 0; i < DI_COUNT; i++)
        *(volatile float *)(base + OFF_SENSOR_MAP + (uint32_t)(DI_SENSOR_BASE + i) * 4u) = 1.0f;
    __asm__ volatile("dsb" ::: "memory");
}

/* ══════════ 采样体 (两个入口共用) ══════════
 * ★ 抽出来是为了让"主循环版"与"拍内版"**逐字相同地**跑同一段逻辑 ——
 *   这样 A/B 对照时唯一的变量才真的是**触发方式**, 而不是"我顺手又改了什么"。
 *   (本项目纪律: 对照实验里出现的每一处差异都必须是有意为之且写在注释里。) */
/* ★★ 2026-09-13: 加 DCL_ITCM —— 闸门证明它"从 ISR 可达却落在 FLASH"(0x08005A28)。
 *   原先只把入口 `di_poll` 搬了 ITCM, 而它调的**采样体**留在 flash ⇒ 同 hil_out_apply
 *   那类"只搬一层"的漏网 (见 itcm.h: 判据是**传递闭包**, 不是直接被调者)。 */
DCL_ITCM static void di_sample_all(uint8_t *base)
{
    for (int i = 0; i < DI_COUNT; i++) {
        uint8_t lv = (uint8_t)di_pin_read(DI_PINS[i]);
        if (lv == s_last[i]) {
            if (s_cnt[i] < DI_DEBOUNCE) s_cnt[i]++;
        } else {
            s_last[i] = lv;
            s_cnt[i] = 0;                               /* 抖动 → 重新计数 (S3 原语义) */
        }
        if (s_cnt[i] >= DI_DEBOUNCE) s_stable[i] = lv;   /* 去抖确认 */
        /* ★ 每轮**无条件回填** SENSOR —— 这是本项目实测踩出来的:
         *   cold_start_reset() 整段 memset(SHM), 而 DI 的去抖状态住在 C 静态里
         *   (不受 memset 影响)。若沿用 S3 "只在变化时写", 一次 0x13 RESET 之后
         *   槽被清 0、而电平"没变化"⇒ 永远不再回填, SENSOR[3..6] 停在 0 (实测到)。
         *   每轮 4 次 float 存储代价可忽略, 换来"任何清零路径后都自愈"。 */
        *(volatile float *)(base + OFF_SENSOR_MAP + (uint32_t)(DI_SENSOR_BASE + i) * 4u) =
            s_stable[i] ? 1.0f : 0.0f;
    }
    __asm__ volatile("dsb" ::: "memory");
}

/* 主循环版 (A/B 对照档 = 改前行为): **相对节流** ⇒ 实际间隔 100~101 拍 */
void di_tick(uint8_t *base, uint32_t tick_now)
{
    static uint32_t last = 0;
    if ((uint32_t)(tick_now - last) < (uint32_t)DI_SAMPLE_DIV) return;
    last = tick_now;
    di_sample_all(base);
}

/* ★ P1 拍内版 (交付): **拍相位锚定** ⇒ 严格每 DI_SAMPLE_DIV 拍一次, 见 di.h 的说明 */
DCL_ITCM void di_poll(uint8_t *base, uint32_t tick_now)
{
    if ((tick_now % (uint32_t)DI_SAMPLE_DIV) != 0u) return;
    di_sample_all(base);
}

void di_selftest(uint8_t *out)
{
    for (int i = 0; i < DI_COUNT; i++) {
        uint32_t pin = DI_PINS[i], port = pin >> 4, bit = pin & 15u;
        for (int k = 0; k < 2; k++) {                   /* k=0 上拉, k=1 下拉 */
            uint32_t p = GPIO_PUPDR(port);
            p &= ~(3u << (bit * 2u));
            p |= ((k == 0) ? 1u : 2u) << (bit * 2u);
            GPIO_PUPDR(port) = p;
            for (volatile int d = 0; d < 100000; d++) { }   /* 建立时间 */
            out[i * 2 + k] = (uint8_t)di_pin_read(pin);
        }
        di_pin_input_pullup(pin);                       /* 恢复上拉 */
    }
}
