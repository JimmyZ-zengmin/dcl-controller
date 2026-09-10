/**
 * primitives.h — 19 原语 (H723 移植版)
 *
 * ★ 来源: esp32-core0 `components/core0/primitives.h`, **算法逐字保留**
 *   (含审计修复: M3 的 CMP 六模式 / M8 的 PID 条件积分防 windup / P3 的 D 参与
 *   判据 / v0.2 的 dt 感知秒体系 / ARITH 除零返回 0 不产 NaN)。
 *
 * 与 S3 的唯一差异: 去掉 IRAM_ATTR (H723 靠链接段 .itcm_text 决定放置),
 * 改为 always_inline —— 保证原语被**内联进两份扫描实例**, 这样 FLASH 版与
 * ITCM 版是逐指令相同的序列, A/B 对比才成立 (若某个原语被 GCC 提到公共
 * .text 里, ITCM 版会回调进 FLASH, 实验作废)。
 *
 * ★ 本阶段不实现 SRC_HMI (通信域写区) —— 阶段 4 落地; 这里留位返回 0。
 */
#ifndef DCL_PRIMITIVES_H
#define DCL_PRIMITIVES_H

#include <stdint.h>
#include "engine.h"

/* ★ H10: 必须带 static (S3 是 static inline)。缺 static 时, 一旦某个原语因体积
 *   无法内联, 报的是 **undefined reference**(而不是重复定义), 排查方向完全相反。 */
#define AINLINE static __attribute__((always_inline)) inline

/* 位级有限性检查 (不依赖 math.h): exponent 全 1 = ±Inf/NaN */
AINLINE int _finite_f(float x)
{
    union { float f; uint32_t u; } cv;
    cv.f = x;
    return ((cv.u & 0x7F800000u) != 0x7F800000u);
}

/* ================= Stateless ================= */
AINLINE float prim_direct(float src, const ParamEntry_t *p, const StateEntry_t *s)
{ (void)p; (void)s; return src; }

AINLINE float prim_cmp(float src, const ParamEntry_t *p, const StateEntry_t *s)
{
    (void)s;
    float t = p->value_a;
    switch ((int)p->value_b) {
        case 1: return (src >= t) ? 1.0f : 0.0f;
        case 2: return (src <  t) ? 1.0f : 0.0f;
        case 3: return (src <= t) ? 1.0f : 0.0f;
        case 4: return (src == t) ? 1.0f : 0.0f;
        case 5: return (src != t) ? 1.0f : 0.0f;
        default: return (src > t) ? 1.0f : 0.0f;
    }
}

AINLINE float prim_clamp(float src, const ParamEntry_t *p, const StateEntry_t *s)
{ (void)s; float lo = p->value_a, hi = p->value_b; if (src < lo) return lo; if (src > hi) return hi; return src; }

AINLINE float prim_scale(float src, const ParamEntry_t *p, const StateEntry_t *s)
{ (void)s; return p->value_a * src + p->value_b; }

AINLINE float prim_and(float src, const ParamEntry_t *p, const StateEntry_t *s, float wb)
{ (void)p; (void)s; return (src > 0.5f && wb > 0.5f) ? 1.0f : 0.0f; }

AINLINE float prim_or(float src, const ParamEntry_t *p, const StateEntry_t *s, float wb)
{ (void)p; (void)s; return (src > 0.5f || wb > 0.5f) ? 1.0f : 0.0f; }

AINLINE float prim_not(float src, const ParamEntry_t *p, const StateEntry_t *s)
{ (void)p; (void)s; return (src > 0.5f) ? 0.0f : 1.0f; }

AINLINE float prim_mux(float src, const ParamEntry_t *p, const StateEntry_t *s, const float *wm)
{ (void)src; (void)s; int i = ((int)p->value_a) & (MAX_WIRES - 1); return wm[i]; }

AINLINE float prim_lut(float src, const ParamEntry_t *p, const StateEntry_t *s, const float *lut)
{
    (void)s; (void)p;
    float f = src;
    if (f < 0) f = 0;
    if (f > (float)(MAX_LUT - 2)) f = (float)(MAX_LUT - 2);
    int i = (int)f;
    float frac = f - (float)i;
    return lut[i] + frac * (lut[i + 1] - lut[i]);
}

/* ================= Stateful ================= */
/* LPF: 一阶惯性, τ 秒; α = dt/(τ+dt) (西门子 Filter_PT1 同款) */
AINLINE float prim_lpf(float src, const ParamEntry_t *p, StateEntry_t *st, float dt)
{
    float tau = p->value_a, al;
    if (tau > 0.0f)       al = dt / (tau + dt);
    else if (tau == 0.0f) al = 1.0f;
    else                  al = 0.0f;
    float o = st->state_a * (1.0f - al) + src * al;
    st->state_a = o;
    return o;
}

/* PID: 位置式 + 梯形积分 + 微分; Ki: /s, Kd: s (dt 感知)
 * ★ M8 条件积分 (anti-windup): P+积分已达输出界且误差同向时冻结积分 */
AINLINE float prim_pid(float src, const ParamEntry_t *p, StateEntry_t *st, float dt)
{
    float sp = p->value_d, Kp = p->value_a, Ki = p->value_b, Kd = p->value_c, err = sp - src;
    float P = Kp * err;
    float D = Kd * (err - st->state_b) / dt;
    float u_now = P + st->state_a + D;
    if (!((u_now >= 100.0f && err > 0.0f) || (u_now <= 0.0f && err < 0.0f)))
        st->state_a += Ki * (err + st->state_b) * 0.5f * dt;
    if (st->state_a >  100) st->state_a =  100;
    if (st->state_a < -100) st->state_a = -100;
    st->state_b = err;
    float o = P + st->state_a + D;
    if (o > 100) o = 100;
    if (o <   0) o =   0;
    return o;
}

AINLINE float prim_hyst(float src, const ParamEntry_t *p, StateEntry_t *st)
{
    if (st->state_a > 0.5f) { if (src < p->value_b) st->state_a = 0; }
    else                    { if (src > p->value_a) st->state_a = 1; }
    return st->state_a;
}

/* RATE: 输出 = 每秒变化率 (/s) */
AINLINE float prim_rate(float src, const ParamEntry_t *p, StateEntry_t *st, float dt)
{ (void)p; float r = (src - st->state_a) / dt; st->state_a = src; return r; }

AINLINE float prim_deadband(float src, const ParamEntry_t *p, StateEntry_t *st)
{ float b = p->value_a, d = src - st->state_a; if (d > b || d < -b) st->state_a = src; return st->state_a; }

AINLINE float prim_edge(float src, const ParamEntry_t *p, StateEntry_t *st)
{
    int t = (int)p->value_a;
    float prev = st->state_a;
    st->state_a = src;
    int r = (prev <= 0.5f && src > 0.5f), f = (prev > 0.5f && src <= 0.5f);
    if (t == 0) return r ? 1 : 0;
    if (t == 1) return f ? 1 : 0;
    return (r || f) ? 1 : 0;
}

/* CNT (CTU/CTD/CTUD): value_a=模式, value_b=PV; wb = R/LD/CD 端 */
AINLINE float prim_cnt(float src, const ParamEntry_t *p, StateEntry_t *st, float wb)
{
    int mode = (int)p->value_a;
    if (mode == 2) {
        if (st->state_b <= 0.5f && src > 0.5f) st->state_a += 1.0f;
        if (st->state_c <= 0.5f && wb  > 0.5f) st->state_a -= 1.0f;
        st->state_b = src; st->state_c = wb;
        return st->state_a;
    }
    if (wb > 0.5f) { st->state_a = (mode == 1) ? p->value_b : 0.0f; st->state_b = src; return st->state_a; }
    if (st->state_b <= 0.5f && src > 0.5f) { if (mode == 1) st->state_a -= 1.0f; else st->state_a += 1.0f; }
    st->state_b = src;
    return st->state_a;
}

/* ARITH: value_a=模式; 第二操作数 = wb; DIV 除零返回 0 (不产 NaN/Inf) */
AINLINE float prim_arith(float src, const ParamEntry_t *p, const StateEntry_t *s, float wb)
{
    (void)s;
    switch ((int)p->value_a) {
        case 1: return src - wb;
        case 2: return src * wb;
        case 3: return (wb != 0.0f) ? src / wb : 0.0f;
        case 4: return (src > wb) ? src : wb;
        case 5: return (src < wb) ? src : wb;
        default: return src + wb;
    }
}

/* SR/RS 双稳态: value_a=0 SR(置位优先) 1 RS(复位优先); src=SET, wb=RESET */
AINLINE float prim_sr(float src, const ParamEntry_t *p, StateEntry_t *st, float wb)
{
    int s = (src > 0.5f), r = (wb > 0.5f);
    if ((int)p->value_a == OP_SR_RESET_DOM) { if (r) st->state_a = 0.0f; else if (s) st->state_a = 1.0f; }
    else                                    { if (s) st->state_a = 1.0f; else if (r) st->state_a = 0.0f; }
    return st->state_a;
}

/* TIMER (TON/TOF/TP): value_a=PT(秒), value_b=模式 */
AINLINE float prim_timer(float src, const ParamEntry_t *p, StateEntry_t *st, float dt)
{
    int mode = (int)p->value_b;
    if (mode == 1) {
        if (src > 0.5f) { st->state_a = 0; return 1; }
        st->state_a += dt;
        return (st->state_a >= p->value_a) ? 0 : 1;
    }
    if (mode == 2) {
        if (st->state_a <= 0.0f && st->state_b <= 0.5f && src > 0.5f) st->state_a = 1e-6f;
        st->state_b = src;
        if (st->state_a > 0.0f) {
            st->state_a += dt;
            if (st->state_a >= p->value_a) { st->state_a = 0.0f; return 0; }
            return 1;
        }
        return 0;
    }
    if (src > 0.5f) { st->state_a += dt; return (st->state_a >= p->value_a) ? 1 : 0; }
    st->state_a = 0;
    return 0;
}

/* ================= 分发 (与 S3 core0_isr.c 的 exec 同构) ================= */
AINLINE float prim_exec(uint8_t op, float src, const ParamEntry_t *p, StateEntry_t *s,
                        const float *wm, const float *lu, float wb, float dt)
{
    switch (op) {
        case OP_DIRECT:   return prim_direct(src, p, s);
        case OP_CMP:      return prim_cmp(src, p, s);
        case OP_CLAMP:    return prim_clamp(src, p, s);
        case OP_SCALE:    return prim_scale(src, p, s);
        case OP_AND:      return prim_and(src, p, s, wb);
        case OP_OR:       return prim_or(src, p, s, wb);
        case OP_NOT:      return prim_not(src, p, s);
        case OP_MUX:      return prim_mux(src, p, s, wm);
        case OP_LUT:      return prim_lut(src, p, s, lu);
        case OP_LPF:      return prim_lpf(src, p, s, dt);
        case OP_PID:      return prim_pid(src, p, s, dt);
        case OP_HYST:     return prim_hyst(src, p, s);
        case OP_RATE:     return prim_rate(src, p, s, dt);
        case OP_DEADBAND: return prim_deadband(src, p, s);
        case OP_EDGE:     return prim_edge(src, p, s);
        case OP_CNT:      return prim_cnt(src, p, s, wb);
        case OP_TIMER:    return prim_timer(src, p, s, dt);
        case OP_ARITH:    return prim_arith(src, p, s, wb);
        case OP_SR:       return prim_sr(src, p, s, wb);
        default:          return src;
    }
}

#endif /* DCL_PRIMITIVES_H */
