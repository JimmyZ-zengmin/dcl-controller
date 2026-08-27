/**
 * DCL 引擎: ISR 路由扫描 + 原语执行
 *
 * 核心循环:
 *   1. 读 ADC (VREFINT → SENSOR_MAP[0])
 *   2. 扫描 ROUTE_TABLE, 执行原语, 写 WIRE_MAP
 *   3. ACTUATOR_STATUS → PWM/GPIO 物理输出
 */
#ifndef DCL_ENGINE_H
#define DCL_ENGINE_H

#include <stdint.h>
#include "../registers.h"
#include "../dtcm_layout.h"

#ifdef __cplusplus
extern "C" {
#endif

/* ── 源/目标类型 ── */
typedef enum {
    SRC_SENSOR = 0,
    SRC_WIRE   = 1,
    SRC_CONST  = 2
} SourceType_t;

typedef enum {
    DST_WIRE = 3
} OutputType_t;

/* ── 原语操作码 ── */
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
    OP_SR       = 0x1B,
    OP_RS       = 0x1C,
    OP_COUNTER  = 0x1D,
    OP_LIMIT    = 0x1E,
    OP_MAX      = 0x1F,
    OP_MIN      = 0x20,
    OP_ABS      = 0x21,
    OP_EQ       = 0x22,
    OP_NE       = 0x23,
};

/**
 * 执行单个原语
 */
float execute_primitive(uint8_t op, float src,
                        const ParamEntry_t *p, StateEntry_t *s, float dt);

/**
 * TIM1_UP ISR: 路由扫描引擎 (100us 周期)
 */
void TIM1_UP_IRQHandler(void);

/**
 * 停止 ISR 引擎: 禁用 UIE + 清 SR + 停 CEN
 */
void engine_stop(void);

/** 启动/恢复 ISR 引擎 */
void engine_start(void);

/** 查询引擎是否运行中 */
uint8_t engine_is_running(void);

#ifdef __cplusplus
}
#endif

#endif /* DCL_ENGINE_H */
