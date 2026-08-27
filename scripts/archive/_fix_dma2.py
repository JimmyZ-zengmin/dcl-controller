path = r'D:\STM\work\dcl-controller\firmware\h723-core0\Src\main.c'
with open(path, 'r', encoding='utf-8-sig') as f:
    content = f.read()

# Remove the duplicated/corrupted section header for TIM1_UP_IRQHandler
# Find the pattern and remove it
old_stuff = '''/* ═══════════════════════════════════════════════════════════
 * DMA Stream5 TCIF: GPIO 输出抖动测量
 * 每次 DMA 完成 SHADOW→ODR 搬运时记录 DWT 时间戳
 * ═══════════════════════════════════════════════════════════ */
#define GPIO_JITTER_BUF_SIZE 256
static volatile uint32_t gpio_jitter_buf[GPIO_JITTER_BUF_SIZE];
static volatile uint32_t gpio_jitter_idx = 0;
static volatile int32_t gpio_jitter_max = 0;

__attribute__((section(".itcm_code")))
void DMA2_Stream5_IRQHandler(void) {
    #define DMA2_HIFCR (*(volatile uint32_t *)0x4002040C)
    DMA2_HIFCR = (1 << 22);  /* CTCIF5 */

    uint32_t now = DWT_CYCCNT;
    static uint32_t prev = 0;
    if (prev) {
        uint32_t period = now - prev;
        int32_t delta = (int32_t)period - 13600;
        if (delta < 0) delta = -delta;
        if (delta > gpio_jitter_max) gpio_jitter_max = delta;
    }
    prev = now;
    gpio_jitter_buf[gpio_jitter_idx % GPIO_JITTER_BUF_SIZE] = now;
    gpio_jitter_idx++;
}

/* ═══════════════════════════════════════════════════════════
 * ISR: 核心扫描引擎
 * ═══════════════════════════════════════════════════════════ */

__attribute__((section(".itcm_code")))
void TIM1_UP_IRQHandler(void) {'''

new_stuff = '''/* ═══════════════════════════════════════════════════════════
 * DMA Stream5 TCIF: GPIO 输出抖动测量
 * ═══════════════════════════════════════════════════════════ */
#define GPIO_JITTER_BUF_SIZE 256
static volatile uint32_t gpio_jitter_buf[GPIO_JITTER_BUF_SIZE];
static volatile uint32_t gpio_jitter_idx = 0;
static volatile int32_t gpio_jitter_max = 0;

__attribute__((section(".itcm_code")))
void DMA2_Stream5_IRQHandler(void) {
    DMA2_HIFCR = (1 << 22);  /* CTCIF5 */
    uint32_t now = DWT_CYCCNT;
    static uint32_t prev = 0;
    if (prev) {
        uint32_t period = now - prev;
        int32_t delta = (int32_t)period - 13600;
        if (delta < 0) delta = -delta;
        if (delta > gpio_jitter_max) gpio_jitter_max = delta;
    }
    prev = now;
    gpio_jitter_buf[gpio_jitter_idx % GPIO_JITTER_BUF_SIZE] = now;
    gpio_jitter_idx++;
}

/* ═══════════════════════════════════════════════════════════
 * ISR: 核心扫描引擎
 * ═══════════════════════════════════════════════════════════ */
__attribute__((section(".itcm_code")))
void TIM1_UP_IRQHandler(void) {'''

if old_stuff not in content:
    print("ERROR: old pattern not found")
    # show what's around line 1440
    lines = content.split('\n')
    for i in range(1435, min(1450, len(lines))):
        print(f"{i}: {repr(lines[i])}")
    exit(1)

content = content.replace(old_stuff, new_stuff, 1)

with open(path, 'w', encoding='utf-8') as f:
    f.write(content)

print("Fixed duplicate header + removed inner DMA2_HIFCR define")
