/**
 * DCL 引擎实现: 原语 + ISR
 *
 * 原语函数在 ITCM 执行 (零等待), 保证 100us 周期内完成。
 * 输出: SHADOW_GPIO (由 DMA Stream 5 自动搬运至 GPIOE_ODR)
 */
#include "engine.h"

/* ── 原语实现 ── */
float execute_primitive(uint8_t op, float src,
                        const ParamEntry_t *p, StateEntry_t *s, float dt)
{
    switch (op) {
    case OP_DIRECT:
        return src;
    case OP_SCALE:
        return p->value_a * src + p->value_b;
    case OP_CLAMP: {
        float clamped;
        if (src < p->value_a) clamped = p->value_a;
        else if (src > p->value_b) clamped = p->value_b;
        else clamped = src;
        if (s->state_c == 0.0f && p->value_b > 0.0f)
            s->state_c = p->value_b;
        return clamped;
    }
    case OP_CMP: {
        int cm = (int)p->value_b;
        if (cm == 0) return (src >  p->value_a) ? 1.0f : 0.0f;
        if (cm == 1) return (src >= p->value_a) ? 1.0f : 0.0f;
        if (cm == 2) return (src <  p->value_a) ? 1.0f : 0.0f;
        return (src <= p->value_a) ? 1.0f : 0.0f;
    }
    case OP_HYST:
        if (src > p->value_a) s->state_a = 1.0f;
        else if (src < p->value_b) s->state_a = 0.0f;
        return s->state_a;
    case OP_LPF: {
        float alpha = p->value_a;
        float out = s->state_a * (1.0f - alpha) + src * alpha;
        s->state_a = out;
        return out;
    }
    case OP_PID: {
        float err = p->value_d - src;
        float i_limit = (s->state_c != 0.0f) ? s->state_c : 100.0f;
        float acc = s->state_a + p->value_b * err;
        acc = (acc >  i_limit) ?  i_limit : acc;
        acc = (acc < -i_limit) ? -i_limit : acc;
        s->state_a = acc;
        float out = p->value_a * err + acc
                  + p->value_c * (err - s->state_b);
        s->state_b = err;
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
    case OP_TIMER: {
        int mode    = (int)p->value_a;
        float pt    = p->value_b;
        int out_sel = (int)p->value_c;
        int prev_in = (s->state_b > 0.5f);
        int cur_in  = (src > 0.5f);
        s->state_b  = src;
        int fsm     = (int)s->state_a;
        float et    = s->state_c;
        float q     = 0.0f;
        float dt_ms = dt * 1000.0f;
        if (mode == 0) {
            if (cur_in) {
                if (fsm == 0 && !prev_in) { fsm = 1; et = 0.0f; }
                if (fsm == 1) { et += dt_ms; if (et >= pt) { et = pt; fsm = 2; } }
                q = (fsm == 2) ? 1.0f : 0.0f;
            } else { fsm = 0; et = 0.0f; q = 0.0f; }
        } else if (mode == 1) {
            if (cur_in) { fsm = 0; et = 0.0f; q = 1.0f; }
            else {
                if (prev_in && !cur_in) { fsm = 1; et = 0.0f; }
                if (fsm == 1) { et += dt_ms; if (et >= pt) { et = pt; fsm = 2; } else q = 1.0f; }
            }
        } else {
            if (!prev_in && cur_in) { fsm = 1; et = 0.0f; }
            if (fsm == 1) { et += dt_ms; if (et >= pt) { et = pt; fsm = 2; q = 0.0f; } else q = 1.0f; }
            if (fsm == 2 && !cur_in) { fsm = 0; et = 0.0f; }
        }
        s->state_a = (float)fsm; s->state_c = et;
        return (out_sel == 0) ? q : et;
    }
    case OP_COUNTER: {
        int mode    = (int)p->value_a;
        float pv    = p->value_b;
        int out_sel = (int)p->value_c;
        float aux   = WIRE_MAP[(int)p->value_d];
        float prev  = s->state_b; s->state_b = src;
        float cv    = s->state_a; float thr = 0.5f;
        if (mode == 0) {
            if (aux > thr) cv = 0.0f;
            else if (prev <= thr && src > thr && cv < pv) cv += 1.0f;
        } else if (mode == 1) {
            if (aux > thr) cv = pv;
            else if (prev > thr && src <= thr && cv > 0.0f) cv -= 1.0f;
        } else {
            float cd  = WIRE_MAP[(int)p->value_d];
            float r   = aux;
            int prev_cd = (s->state_d > thr); int cur_cd = (cd > thr);
            s->state_d = cd;
            if (r > thr) cv = 0.0f;
            else {
                if (prev <= thr && src > thr && !(prev_cd <= thr && cur_cd > thr) && cv < pv) cv += 1.0f;
                if (prev_cd <= thr && cur_cd > thr && !(prev <= thr && src > thr) && cv > 0.0f) cv -= 1.0f;
            }
        }
        s->state_a = cv;
        if (out_sel == 1) return (cv >= pv) ? 1.0f : 0.0f;
        if (out_sel == 2) return (cv <= 0.0f) ? 1.0f : 0.0f;
        return cv;
    }
    case OP_MUX:
        return (src > 0.5f) ? WIRE_MAP[(int)p->value_b] : WIRE_MAP[(int)p->value_a];
    case OP_SR: {
        float r = WIRE_MAP[(int)p->value_a]; float q1 = s->state_a;
        if (src > 0.5f) q1 = 1.0f; else if (r > 0.5f) q1 = 0.0f;
        s->state_a = q1; return q1;
    }
    case OP_RS: {
        float r1 = WIRE_MAP[(int)p->value_a]; float q1 = s->state_a;
        if (r1 > 0.5f) q1 = 0.0f; else if (src > 0.5f) q1 = 1.0f;
        s->state_a = q1; return q1;
    }
    case OP_BITAND:
        return (float)((uint32_t)src & (uint32_t)WIRE_MAP[(int)p->value_a]);
    case OP_BITOR:
        return (float)((uint32_t)src | (uint32_t)WIRE_MAP[(int)p->value_a]);
    case OP_BITXOR:
        return (float)((uint32_t)src ^ (uint32_t)WIRE_MAP[(int)p->value_a]);
    case OP_BITNOT:
        return (float)(~(uint32_t)src);
    case OP_LIMIT:
        if (src < p->value_a) return p->value_a;
        if (src > p->value_b) return p->value_b;
        return src;
    case OP_MAX: {
        float b = WIRE_MAP[(int)p->value_a];
        return (src > b) ? src : b;
    }
    case OP_MIN: {
        float b = WIRE_MAP[(int)p->value_a];
        return (src < b) ? src : b;
    }
    case OP_ABS:
        return (src < 0.0f) ? -src : src;
    case OP_EQ:
        return (src == p->value_a) ? 1.0f : 0.0f;
    case OP_NE:
        return (src != p->value_a) ? 1.0f : 0.0f;
    default:
        return src;
    }
}

/* ── DWT CYCCNT 辅助 ── */
static inline uint32_t ccnt(void) { return DWT_CYCCNT; }

/* ═══════════════════════════════════════════════════════════
 * ISR: 核心扫描引擎 (100us 周期)
 * ═══════════════════════════════════════════════════════════ */
__attribute__((section(".itcm_code")))
void TIM1_UP_IRQHandler(void) {
    uint32_t t0 = ccnt();
    TIM1_SR = 0;

    /* ✨ ADC 输入: VREFINT 反算 (当前 #if 0, 允许 pyocd 注入) */
    #if 0
    {
        uint32_t raw = ADC1_DR & 0xFFF;
        uint16_t *cal = (uint16_t *)0x1FF1E860;
        float vref_cal = (float)(*cal & 0xFFF);
        float vref_actual = 3.3f * vref_cal / (float)raw;
        SENSOR_MAP[0] = (float)raw * (vref_actual / 4095.0f);
    }
    #endif

    /* ── Period ── */
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

    /* ── Route table 扫描 ── */
    uint32_t n = N_ROUTES;
    uint8_t *rp = (uint8_t *)ROUTE_TABLE;
    uint8_t *end = rp + n * 16;
    do {
        if (!(rp[5] & ROUTE_ENABLED)) continue;

        float src;
        uint8_t st = rp[0];
        if (st == SRC_SENSOR)
            src = SENSOR_MAP[rp[1]];
        else if (st == SRC_WIRE)
            src = WIRE_MAP[rp[1]];
        else
            src = PARAM_TABLE[*(uint16_t *)(rp+6)].value_d;

        uint16_t pi = *(uint16_t *)(rp + 6);
        uint16_t so = *(uint16_t *)(rp + 8);
        float out;
        uint8_t op = rp[4];
        if (op == OP_DIRECT)
            out = src;
        else if (op == OP_SCALE)
            out = PARAM_TABLE[pi].value_a * src + PARAM_TABLE[pi].value_b;
        else if (op == OP_CMP) {
            int cm = (int)PARAM_TABLE[pi].value_b;
            if (cm == 0) out = (src >  PARAM_TABLE[pi].value_a) ? 1.0f : 0.0f;
            else if (cm == 1) out = (src >= PARAM_TABLE[pi].value_a) ? 1.0f : 0.0f;
            else if (cm == 2) out = (src <  PARAM_TABLE[pi].value_a) ? 1.0f : 0.0f;
            else out = (src <= PARAM_TABLE[pi].value_a) ? 1.0f : 0.0f;
        }
        else
            out = execute_primitive(op, src,
                      (const ParamEntry_t *)&PARAM_TABLE[pi],
                      (StateEntry_t *)&STATE_TABLE[so], 0.0001f);
        WIRE_MAP[rp[3]] = out;

        uint16_t ai = *(uint16_t *)(rp + 10);
        if (ai > 0 && ai < MAX_ACTUATORS)
            ACTUATOR_STATUS[ai] = out;
    } while ((rp += 16) < end);

    /* ── Execution time ── */
    uint32_t t1 = ccnt();
    uint32_t exec_time = t1 - t0;
    if (exec_time < EXEC_MIN) EXEC_MIN = exec_time;
    if (exec_time > EXEC_MAX) EXEC_MAX = exec_time;

    SAMPLES++;
    HEARTBEAT++;
    extern uint32_t canopen_ticks;
    canopen_ticks++; /* 100us per tick, 10 ticks=1ms */

    /* ✨ 输出映射: ACTUATOR_STATUS → 物理硬件 ── */
    {
        volatile float *ap = ACTUATOR_STATUS;
        if (ap[1] >= 0.0f) {
            float v = ap[1]; if (v > 100.0f) v = 100.0f;
            TIM1_CCR1 = (uint16_t)(v * 135.99f);
        }
        if (ap[2] >= 0.0f) {
            float v = ap[2]; if (v > 100.0f) v = 100.0f;
            TIM1_CCR2 = (uint16_t)(v * 135.99f);
        }
        if (ap[3] >= 0.0f) {
            float v = ap[3]; if (v > 100.0f) v = 100.0f;
            TIM1_CCR3 = (uint16_t)(v * 135.99f);
        }
        TIM1_CCR4 = 13399;  /* CH4 保持 ADC 触发点 */

        /* 数字输出: actuator_idx 32~63 → GPIOE bit0~31 */
        uint32_t gpio_out = 0;
        for (int i = 32; i < 64 && i < MAX_ACTUATORS; i++) {
            if (ap[i] > 0.5f) gpio_out |= (1u << (i - 32));
        }
        SHADOW_GPIO = gpio_out;   /* DMA Stream 5 自动搬运至 GPIOE_ODR */
    }
}

/* ── 停止引擎 ── */
static volatile uint8_t engine_running_flag = 1;

void engine_stop(void) {
    TIM1_DIER &= ~(1u << 0);  /* 禁用 UIE */
    TIM1_SR = 0xFFFF;          /* 清除所有 SR 标志 */
    TIM1_CR1 = 0;              /* CEN=0 */
    engine_running_flag = 0;
}

void engine_start(void) {
    engine_running_flag = 1;
}

uint8_t engine_is_running(void) {
    return engine_running_flag;
}
