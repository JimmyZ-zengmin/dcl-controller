/**
 * DTCM 内存布局 (STM32H723, 128KB DTCM @ 0x20000000)
 *
 * 分区设计 (地址从低到高, 留足间隙避免隐形冲突):
 *   0x0000-0x003F  TIMING          64B   时序/诊断变量
 *   0x0040-0x00DF  保留            160B   间隙 (防止踩踏)
 *   0x00E0         SHADOW_GPIO     4B     GPIO 数字输出影子
 *   0x00E4-0x00FF  保留            28B   间隙
 *   0x0100-0x01FF  SENSOR_MAP      256B   64 × float 传感器寄存器
 *   0x0200-0x02FF  ACTUATOR_STATUS 256B   64 × float 执行器寄存器
 *   0x0300-0x12FF  WIRE_MAP        4096B  1024 × float 内部线
 *   0x1300-0x16FF  LUT_DATA        1024B  256 × float 查找表
 *   0x1700-0x56FF  ROUTE_TABLE     16384B 1024 × 16B 路由表
 *   0x5700-0x76FF  PARAM_TABLE     8192B  512 × 16B 参数表
 *   0x7700-0x8FFF  STATE_TABLE     4096B  256 × 16B 状态表
 *   0x9000-...     SIN_LUT         16384B 4096 × float 正弦表
 *
 * 历史教训:
 *   - SHADOW_GPIO 绝不可放在 0x280 (与 ACTUATOR_STATUS[32] 冲突)
 *   - ADC_RAW 绝不可放在 0x290 (在 ACTUATOR_STATUS 范围内)
 */
#ifndef DCL_DTCM_LAYOUT_H
#define DCL_DTCM_LAYOUT_H

#include <stdint.h>

#define DTCM_BASE       0x20000000UL
#define DTCM_SIZE       (128 * 1024)

/* ── TIMING (DTCM + 0x0000, 64B) ── */
#define TIMING_BASE     (DTCM_BASE + 0x0000)
#define SAMPLES         (*(volatile uint32_t *)(TIMING_BASE + 0x00))
#define PERIOD_MIN      (*(volatile uint32_t *)(TIMING_BASE + 0x04))
#define PERIOD_MAX      (*(volatile uint32_t *)(TIMING_BASE + 0x08))
#define EXEC_MIN        (*(volatile uint32_t *)(TIMING_BASE + 0x0C))
#define EXEC_MAX        (*(volatile uint32_t *)(TIMING_BASE + 0x10))
#define LAST_ENTRY      (*(volatile uint32_t *)(TIMING_BASE + 0x14))
#define HEARTBEAT       (*(volatile uint32_t *)(TIMING_BASE + 0x18))
#define CLOCK_HZ        (*(volatile uint32_t *)(TIMING_BASE + 0x1C))
#define TIMER_HZ        (*(volatile uint32_t *)(TIMING_BASE + 0x20))
#define EXEC_TOTAL      (*(volatile uint32_t *)(TIMING_BASE + 0x24))
#define DEV_ABS_MAX     (*(volatile uint32_t *)(TIMING_BASE + 0x28))
#define DEV_ABS_MAX_SMP (*(volatile uint32_t *)(TIMING_BASE + 0x2C))
#define DEV_POS_MAX     (*(volatile uint32_t *)(TIMING_BASE + 0x30))
#define DEV_NEG_MAX     (*(volatile uint32_t *)(TIMING_BASE + 0x34))
#define PERIOD_EXACT    (*(volatile uint32_t *)(TIMING_BASE + 0x38))
#define PERIOD_FAR      (*(volatile uint32_t *)(TIMING_BASE + 0x3C))

/* ── SHADOW_GPIO (DTCM + 0x00E0) ── */
#define SHADOW_GPIO     (*(volatile uint32_t *)(DTCM_BASE + 0x00E0))

/* ── ADC_RAW (DTCM + 0x00F0) ── */
/* 仅 4B, 放在 SHADOW_GPIO 与 SENSOR_MAP 之间的空闲区 (之前错误地放在 0x0290) */
#define ADC_RAW         (*(volatile uint32_t *)(DTCM_BASE + 0x00F0))

/* ── 寄存器空间 ── */
#define SENSOR_MAP       ((volatile float *)(DTCM_BASE + 0x0100))   /* 64 ch */
#define ACTUATOR_STATUS  ((volatile float *)(DTCM_BASE + 0x0200))   /* 64 ch */
#define WIRE_MAP         ((volatile float *)(DTCM_BASE + 0x0300))   /* 1024 ch */
#define LUT_DATA         ((volatile float *)(DTCM_BASE + 0x1300))   /* 256 ch */

#define ROUTE_TABLE      ((volatile RouteEntry_t *)(DTCM_BASE + 0x1700))
#define PARAM_TABLE      ((volatile ParamEntry_t *)(DTCM_BASE + 0x5700))
#define STATE_TABLE      ((volatile StateEntry_t *)(DTCM_BASE + 0x7700))

#define SIN_LUT_SIZE     4096
#define SIN_LUT          ((volatile float *)(DTCM_BASE + 0x9000))

/* ── 数据结构 ── */
typedef struct __attribute__((packed, aligned(4))) {
    uint8_t  src_type;      /* SRC_SENSOR / SRC_WIRE / SRC_CONST */
    uint8_t  src_index;
    uint8_t  dst_type;      /* DST_WIRE */
    uint8_t  dst_channel;
    uint8_t  op;            /* OP_xxx */
    uint8_t  flags;         /* ROUTE_ENABLED */
    uint16_t param_idx;
    uint16_t state_offset;
    uint16_t actuator_idx;
    uint16_t wire2_idx;
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

/* ── 容量限制 ── */
#define MAX_ROUTES      1024
#define MAX_SENSORS     64
#define MAX_ACTUATORS   64
#define MAX_WIRES       1024
#define MAX_PARAMS      512
#define MAX_STATES      256
#define MAX_LUT         256
#define ROUTE_ENABLED   0x01

/* ── 活跃路由数 (ISR 读取, main 写入) ── */
#define N_ROUTES       (*(volatile uint32_t *)(DTCM_BASE + 0xF0))

/* ── 寄存器地址 (用于运行时装载到 DTCM) ── */
#define SCRATCH         ((volatile uint32_t *)(DTCM_BASE + 0xF8))

#endif /* DCL_DTCM_LAYOUT_H */
