/*
 * macro.c — MACRO 字节码 VM (W5 外设域) — H723 移植版
 *
 * 语义来源: esp32-core0/components/macro/macro_exec.h (S3)。字节码集合、栈语义、
 * NaN/Inf 防护、索引掩码 (P2d)、SHM 数据区判定 (N4) 全部**按 S3 保留**;
 * 只换平台原语 (见 macro.h 顶部说明)。
 *
 * ★ 保留的 S3 设计点 (逐条在此说明, 避免"移植时被简化掉"):
 *   · 索引一律 & (MAX_x-1): 各区只有 64/128 项, uint8_t idx 不掩码会越界写到
 *     相邻区 (MACRO 可绕过引擎直接改运行中的路由表 → 与 ISR 并发读撕裂)。
 *   · store (0x31/0x33/0x11) 拒 NaN/Inf: 非有限值经 prim 传播会污染控制链 (P1b)。
 *   · 0x11 store 对"目标是 SHM float 区"额外套同一规则 (堵绕过 0x33 的注入路径)。
 *   · 无跳转指令 ⇒ 任何程序都是直线、必然终止 (不会无限循环把主循环拖死)。
 *
 * H723 加固 (相对 S3 的有意差异, 见 macro.h):
 *   · 裸地址 load/store 限定 SHM 窗口 (无 MMU, 越界 = HardFault);
 *     ★ 写额外收窄为"SENSOR / ACTUATOR / WIRE 三个数据区"(读宽写窄, 见 m_waddr_ok
 *     上方注释) —— 收紧前它能写任意 SHM 槽, 会破坏"单写者"红线。
 *   · SPI op (0x20-0x23) 不迁移 → 返回 -4 (S3 里它只服务 display)。
 *
 * 返回码: -1 截断/栈错 · -2 裸地址越界 · -3 非有限 float · -4 未迁移 op · -5 未知 op
 */
#include "macro.h"
#include "engine.h"
#include "regs.h"
#include "clock.h"

#define M_PTR(base, off) ((void *)((base) + (off)))
static inline MacroCtrl_t *m_ctrl(uint8_t *base) { return (MacroCtrl_t *)M_PTR(base, OFF_MACRO_CTRL); }

static inline uint32_t m_cyc(void) { return DWT_CYCCNT; }

/* ══════════ 定位自述 (B 档: 调试工具, 非实时) ══════════
 * 依据: docs/REF-program-contract.md §1.2 · docs/PLAN-dcl-standardization.md §4。
 *   `0` = 本 VM **不保证实时** —— 它的循环挂在**主循环** macro_tick 上 (不是 ISR 拍),
 *         指令集里含忙等 (0x06 忙等 N 周期 / 0x07 忙等 N ms), 且**没有标价、没有
 *         静态门、也没有动态门**;
 *   `0` 还表示它**不参与持久化** (持久化开关 DCL_PERSIST_SAVE 与 macro 无关);
 *   以上合起来 = 它**不得用于控制回路**。
 * ★ 为什么必须 `volatile` 且在代码里真被读一次 (见 macro_reset):
 *   本工程开了 -fdata-sections + -Wl,--gc-sections, "固件内无人读也无人写"的全局
 *   会被**整段回收** ⇒ 符号从 ELF 里消失、外部就再也读不到 (main.c 的 OBS 族为此
 *   踩过两次, 见其注释)。`volatile` 让这次读成为**不可消除的副作用** ⇒ 段被引用而
 *   存活。实测落位: `.data.g_macro_realtime` @ 0x2000_0034 (与 g_wdt_armed /
 *   g_persist_target 这些"外部可读"的 OBS 族同段), 且值恒为 0。 */
volatile const uint32_t g_macro_realtime = 0u;

/* ---- 引脚编码: 0..15=PA, 16..31=PB, 32..47=PC, 48..63=PD ---- */
#define M_PORT_OF(pin) (((uint32_t)(pin)) >> 4)
#define M_BIT_OF(pin)  (((uint32_t)(pin)) & 15u)

static void m_port_clk(uint32_t port)
{
    /* RCC_AHB4ENR: GPIOAEN=bit0 … GPIOEEN=bit4 (与本项目 uart.c 同源) */
    if (port <= 4u) RCC_AHB4ENR |= (1u << port);
}

/* mode: 0=输入, 1=推挽输出, 2=开漏(带上拉) */
static void m_pin_cfg(uint32_t pin, uint32_t mode)
{
    uint32_t port = M_PORT_OF(pin), bit = M_BIT_OF(pin);
    m_port_clk(port);
    uint32_t mod = GPIO_MODER(port);
    mod &= ~(3u << (bit * 2u));
    mod |= ((mode == 0u) ? 0u : 1u) << (bit * 2u);   /* 00=输入 01=输出 */
    GPIO_MODER(port) = mod;

    uint32_t oty = GPIO_OTYPER(port);
    if (mode == 2u) oty |=  (1u << bit); else oty &= ~(1u << bit);
    GPIO_OTYPER(port) = oty;

    uint32_t pup = GPIO_PUPDR(port);
    pup &= ~(3u << (bit * 2u));
    if (mode != 1u) pup |= (1u << (bit * 2u));       /* 输入/开漏: 上拉 (同 S3) */
    GPIO_PUPDR(port) = pup;
}

static void m_pin_write(uint32_t pin, uint32_t v)
{
    GPIO_BSRR(M_PORT_OF(pin)) = v ? (1u << M_BIT_OF(pin))
                                  : (1u << (M_BIT_OF(pin) + 16u));
}

static uint32_t m_pin_read(uint32_t pin)
{
    return (GPIO_IDR(M_PORT_OF(pin)) >> M_BIT_OF(pin)) & 1u;
}

/* ---- 裸地址访问守卫 (H723 加固) ----
 * ★ 为什么要把"写"收紧 (2026-09-15, 依据 docs/PLAN-dcl-standardization.md §4):
 *   收紧前 load(0x10) / store(0x11) 共用同一个"4B 对齐 + 落在 [shm, shm+SHM_SIZE-4]"
 *   的判据 ⇒ 字节码可以 store 到 SHM 里**任意**一个槽 —— 包括别人的路由表、桶表、
 *   控制块、持久化登记区。这直接打破 MEMORY.md §9.5 红线 2"同一 SHM 槽只允许一个
 *   权威写者"。
 *   收紧后 store 只能落进下面三个**数据区** (SENSOR_MAP / ACTUATOR_STATUS /
 *   WIRE_MAP), 其余一律沿用 -2 ⇒ 与"macro 本轮定为 B 档: 只作调试工具、不得用于
 *   控制回路"这个定位一致。
 * ★ **读宽、写窄 —— 这是有意的**: load(0x10) 仍允许读整个 SHM 窗口 (只读无害, 且
 *   调试时有用, 收紧它没有收益); 只有 store(0x11) 被限制到三个数据区。 */

/* 读守卫: 4B 对齐 + 落在 SHM 窗口内 (有意不收紧, 见上) */
static int m_addr_ok(uint8_t *base, uint32_t a)
{
    uint32_t b = (uint32_t)(uintptr_t)base;
    if (a & 3u) return 0;
    if (a < b || a > (uint32_t)(b + SHM_SIZE - 4u)) return 0;
    return 1;
}

/* 写守卫: 先过读守卫, 再要求偏移落在下面三个数据区之一。
 * 每个区都写成 **[起点宏, 起点宏 + 项数×4)** 的"区间 + 长度"形式 —— 长度由
 * MAX_x 推出, 不写魔数; 布局的真源仍是 engine.h, 这里只引用它的偏移宏。 */
static int m_waddr_ok(uint8_t *base, uint32_t a)
{
    if (!m_addr_ok(base, a)) return 0;
    uint32_t off = a - (uint32_t)(uintptr_t)base;
    if (off >= OFF_SENSOR_MAP      && off < OFF_SENSOR_MAP      + MAX_SENSORS   * 4u) return 1;
    if (off >= OFF_ACTUATOR_STATUS && off < OFF_ACTUATOR_STATUS + MAX_ACTUATORS * 4u) return 1;
    if (off >= OFF_WIRE_MAP        && off < OFF_WIRE_MAP        + MAX_WIRES     * 4u) return 1;
    return 0;
}

/* ---- NaN/Inf 与 float 区判定 (S3 的 _me_finite32 / _me_shm_off_is_float) ---- */
static int m_finite32(uint32_t v) { return ((v & 0x7F800000u) != 0x7F800000u); }

/* SHM 中的 float 数据区 (按 engine.h 布局): [SENSOR_MAP, ROUTE_TABLE) ∪
 * [PARAM_TABLE, STATE_STAGING)。与 S3 的清单等价 (S3 把它拆成逐段列举)。 */
static int m_is_float_off(uint32_t off)
{
    if (off >= OFF_SENSOR_MAP && off < OFF_ROUTE_TABLE)   return 1;
    if (off >= OFF_PARAM_TABLE && off < OFF_STATE_STAGING) return 1;
    return 0;
}

/* ══════════ VM 主体 ══════════ */
int macro_exec(uint8_t *base, const uint8_t *code, uint16_t clen,
               uint32_t *out, uint16_t *outn)
{
    uint32_t stk[MACRO_STACK_DEPTH];
    int sp = 0;
    uint16_t pc = 0;

#define PUSH(v) do { if (sp >= MACRO_STACK_DEPTH) return -1; stk[sp++] = (uint32_t)(v); } while (0)
#define POP()   (sp > 0 ? stk[--sp] : 0u)

    while (pc < clen) {
        uint8_t op = code[pc++];
        switch (op) {
        case 0x00: break;                                            /* nop */
        case 0x01:                                                   /* 配置为输出 */
        case 0x02:                                                   /* 配置为输入 */
        case 0x03:                                                   /* 配置为开漏 */
            if ((uint32_t)pc + 1u > clen) return -1;
            m_pin_cfg(code[pc++], (op == 0x01) ? 1u : ((op == 0x02) ? 0u : 2u));
            break;
        case 0x04:                                                   /* 写电平 */
            if ((uint32_t)pc + 2u > clen) return -1;
            { uint8_t p = code[pc++], v = code[pc++]; m_pin_write(p, v); }
            break;
        case 0x05:                                                   /* 读电平 → push */
            if ((uint32_t)pc + 1u > clen) return -1;
            PUSH(m_pin_read(code[pc++]));
            break;
        case 0x06: {                                                 /* 忙等 N 周期 */
            if ((uint32_t)pc + 2u > clen) return -1;
            uint16_t cyc = (uint16_t)(code[pc] | (code[pc + 1] << 8)); pc += 2;
            uint32_t s = m_cyc();
            while ((uint32_t)(m_cyc() - s) < cyc) { }
            break;
        }
        case 0x07: {                                                 /* 忙等 N ms */
            if ((uint32_t)pc + 2u > clen) return -1;
            uint16_t ms = (uint16_t)(code[pc] | (code[pc + 1] << 8)); pc += 2;
            /* ★ DWT 计的是 **CPU 时钟** (CLK_CPU_HZ=400MHz), 不是 HCLK(200MHz)。
             *   用 CLK_CPU_HZ 换算 —— 用错会让延时长/短 2 倍 (本项目 BRR 事故的
             *   "单位错但公式自洽"同类陷阱, 故此处显式钉住单位)。 */
            uint32_t need = (uint32_t)ms * (CLK_CPU_HZ / 1000u);
            uint32_t s = m_cyc();
            while ((uint32_t)(m_cyc() - s) < need) { }
            break;
        }
        case 0x08: {                                                 /* push u32 */
            if ((uint32_t)pc + 4u > clen) return -1;
            uint32_t v = (uint32_t)code[pc] | ((uint32_t)code[pc + 1] << 8)
                       | ((uint32_t)code[pc + 2] << 16) | ((uint32_t)code[pc + 3] << 24);
            pc += 4; PUSH(v);
            break;
        }
        case 0x09: (void)POP(); break;                               /* drop */
        case 0x10: {                                                 /* load(addr) */
            if ((uint32_t)pc + 4u > clen) return -1;
            uint32_t a = (uint32_t)code[pc] | ((uint32_t)code[pc + 1] << 8)
                       | ((uint32_t)code[pc + 2] << 16) | ((uint32_t)code[pc + 3] << 24);
            pc += 4;
            if (!m_addr_ok(base, a)) return -2;   /* 读宽 (有意): 整个 SHM 窗口都允许读 */
            PUSH(*(volatile uint32_t *)a);
            break;
        }
        case 0x11: {                                                 /* store(addr, val) */
            if (sp < 2) return -1;
            uint32_t v = POP(), a = POP();
            if (!m_waddr_ok(base, a)) return -2;  /* 写窄 (有意): 只许三个数据区 */
            /* N4: 目标是 SHM float 区且写非有限 → 拒 (与 0x33 同规则) */
            if (!m_finite32(v) && m_is_float_off(a - (uint32_t)(uintptr_t)base)) return -3;
            *(volatile uint32_t *)a = v;
            break;
        }
        /* ---- SPI (0x20-0x23): 未迁移。S3 里只为 ST7735 屏服务 (display 不迁),
         *      遇到即报错, 不静默跳过 (静默 = 宣称>实现)。 ---- */
        case 0x20: case 0x21: case 0x22: case 0x23:
            return -4;
        /* ---- 传感器/执行器/WIRE 读写 (索引掩码见文件头说明) ---- */
        case 0x30:
            if ((uint32_t)pc + 1u > clen) return -1;
            { uint8_t i = code[pc++] & (MAX_SENSORS - 1);
              PUSH(*(volatile uint32_t *)(base + OFF_SENSOR_MAP + (uint32_t)i * 4u)); }
            break;
        case 0x31:
            if (sp < 1) return -1;
            if ((uint32_t)pc + 1u > clen) return -1;
            { uint8_t i = code[pc++] & (MAX_ACTUATORS - 1); uint32_t v = POP();
              if (!m_finite32(v)) return -3;
              *(volatile uint32_t *)(base + OFF_ACTUATOR_STATUS + (uint32_t)i * 4u) = v; }
            break;
        case 0x32:
            if ((uint32_t)pc + 1u > clen) return -1;
            { uint8_t i = code[pc++] & (MAX_WIRES - 1);
              PUSH(*(volatile uint32_t *)(base + OFF_WIRE_MAP + (uint32_t)i * 4u)); }
            break;
        case 0x33:
            if (sp < 1) return -1;
            if ((uint32_t)pc + 1u > clen) return -1;
            { uint8_t i = code[pc++] & (MAX_WIRES - 1); uint32_t v = POP();
              if (!m_finite32(v)) return -3;
              *(volatile uint32_t *)(base + OFF_WIRE_MAP + (uint32_t)i * 4u) = v; }
            break;
        case 0xFF: goto done;                                        /* 结束标记 */
        default:  return -5;                                         /* 未知 op */
        }
    }
done:
    *outn = (uint16_t)sp;
    for (int i = 0; i < sp; i++) out[i] = stk[i];
    return 0;
}

/* ══════════ 主循环驱动的周期执行 (替代 S3 的 FreeRTOS 任务) ══════════ */
int macro_tick(uint8_t *base, uint32_t tick_now)
{
    MacroCtrl_t *c = m_ctrl(base);
    if (!c->run) return 0;
    if (c->len == 0) { c->run = 0; return 0; }      /* 无程序 → 自停 */

    uint16_t lms = (c->loop_ms < 10) ? 10 : c->loop_ms;   /* S3: 最小 10ms */
    uint32_t interval = (uint32_t)lms * 10u;              /* 100μs/拍 → 1ms = 10 拍 */
    if ((uint32_t)(tick_now - c->last_tick) < interval) return 0;
    c->last_tick = tick_now;

    uint32_t out[MACRO_STACK_DEPTH];
    uint16_t on = 0;
    int rc = macro_exec(base, (const uint8_t *)M_PTR(base, OFF_MACRO_CODE),
                        c->len, out, &on);
    if (rc != 0) {
        c->err = (uint8_t)(-rc);                    /* 错误可见 (S3: OFF_MACRO_ERR) */
        c->run = 0;                                 /* 出错即停, 不反复刷错误 */
        return 0;
    }
    c->err = 0;
    c->loop_cnt++;
    return 1;
}

/* ══════════ 上传 / 控制 / 复位 ══════════ */
int macro_upload(uint8_t *base, uint16_t loop_ms, const uint8_t *code, uint16_t clen)
{
    if (clen > OFF_MACRO_CODE_SZ) return -1;
    MacroCtrl_t *c = m_ctrl(base);
    c->run = 0; c->err = 0; c->loop_cnt = 0; c->last_tick = 0;
    uint8_t *dst = (uint8_t *)M_PTR(base, OFF_MACRO_CODE);
    for (uint16_t i = 0; i < clen; i++) dst[i] = code[i];
    c->len = clen;
    c->loop_ms = loop_ms;                            /* 存原值 (回显给 PC), 运行时再钳 */
    __asm__ volatile("dsb" ::: "memory");
    return 0;
}

int macro_ctrl(uint8_t *base, uint8_t action)
{
    MacroCtrl_t *c = m_ctrl(base);
    if (action == 0u) { c->run = 0; __asm__ volatile("dsb" ::: "memory"); return 0; }
    if (action == 1u) {
        if (c->len == 0) return -1;                  /* 无程序不能启动 (比 S3 严: 不空转) */
        c->run = 1; c->err = 0; c->loop_cnt = 0; c->last_tick = 0;
        __asm__ volatile("dsb" ::: "memory");
        return 0;
    }
    return -2;
}

void macro_reset(uint8_t *base)
{
    MacroCtrl_t *c = m_ctrl(base);
    uint8_t *p = (uint8_t *)c;
    for (uint32_t i = 0; i < sizeof(MacroCtrl_t); i++) p[i] = 0;
    __asm__ volatile("dsb" ::: "memory");
    /* ★ 真实读一次定位自述常量: 这里是"新域登记入口"(cold_start_reset 调用), 在此
     *   把 g_macro_realtime 钉住, 它才不会因"无人引用"被 --gc-sections 回收
     *   (volatile 使这次读成为不可消除的副作用 —— 理由见该常量的定义处)。 */
    (void)g_macro_realtime;
}
