/**
 * DCL Engine — DTCM Memory Map
 *
 * 系统引擎 vs 运行程序 严格分离
 * 详见 docs/MEMORY-MAP.md
 */

#ifndef DCL_MEMORY_MAP_H
#define DCL_MEMORY_MAP_H

#include <stdint.h>

// ══════════════════════════════════════════════════════════
// 基础定义
// ══════════════════════════════════════════════════════════

#define DTCM_BASE           0x20000000UL
#define DTCM_SIZE           (128 * 1024)   // 128KB

#define PROGRAM_MAGIC_VALID 0x50523047UL   // "PR0G"
#define ENGINE_MAGIC_VALID  0x454E4731UL   // "ENG1"

#define PROGRAM_FORMAT_VERSION 1

// ══════════════════════════════════════════════════════════
// 系统引擎区 (ENGINE REGION) — 0x20000000 ~ 0x200016FF
// ══════════════════════════════════════════════════════════

// ── TIMING (64B) ──
#define ENGINE_TIMING_BASE      (DTCM_BASE + 0x0000)

// 地址常量
#define ENGINE_MAGIC_ADDR       (ENGINE_TIMING_BASE + 0x00)
#define ENGINE_STATE_ADDR       (ENGINE_TIMING_BASE + 0x04)
#define PERIOD_MIN_ADDR         (ENGINE_TIMING_BASE + 0x08)
#define PERIOD_MAX_ADDR         (ENGINE_TIMING_BASE + 0x0C)
#define SAMPLES_ADDR            (ENGINE_TIMING_BASE + 0x10)
#define CYCLES_ADDR             (ENGINE_TIMING_BASE + 0x14)
#define EXEC_MIN_ADDR           (ENGINE_TIMING_BASE + 0x18)
#define EXEC_MAX_ADDR           (ENGINE_TIMING_BASE + 0x1C)
#define FRAME_IDX_ADDR          (ENGINE_TIMING_BASE + 0x20)
#define LAST_ENTRY_ADDR         (ENGINE_TIMING_BASE + 0x24)
#define HEARTBEAT_ADDR          (ENGINE_TIMING_BASE + 0x28)
#define CLOCK_HZ_ADDR           (ENGINE_TIMING_BASE + 0x2C)
#define TIMER_HZ_ADDR           (ENGINE_TIMING_BASE + 0x30)
#define EXEC_TOTAL_ADDR         (ENGINE_TIMING_BASE + 0x34)
#define DEV_ABS_MAX_ADDR        (ENGINE_TIMING_BASE + 0x38)
#define DEV_ABS_MAX_SMP_ADDR    (ENGINE_TIMING_BASE + 0x3C)

// 直接访问宏
#define ENGINE_MAGIC        (*(volatile uint32_t *)ENGINE_MAGIC_ADDR)
#define ENGINE_STATE        (*(volatile uint32_t *)ENGINE_STATE_ADDR)
#define PERIOD_MIN          (*(volatile uint32_t *)PERIOD_MIN_ADDR)
#define PERIOD_MAX          (*(volatile uint32_t *)PERIOD_MAX_ADDR)
#define SAMPLES             (*(volatile uint32_t *)SAMPLES_ADDR)
#define CYCLES              (*(volatile uint32_t *)CYCLES_ADDR)
#define EXEC_MIN            (*(volatile uint32_t *)EXEC_MIN_ADDR)
#define EXEC_MAX            (*(volatile uint32_t *)EXEC_MAX_ADDR)
#define FRAME_IDX           (*(volatile uint32_t *)FRAME_IDX_ADDR)
#define LAST_ENTRY          (*(volatile uint32_t *)LAST_ENTRY_ADDR)
#define HEARTBEAT           (*(volatile uint32_t *)HEARTBEAT_ADDR)
#define CLOCK_HZ            (*(volatile uint32_t *)CLOCK_HZ_ADDR)
#define TIMER_HZ            (*(volatile uint32_t *)TIMER_HZ_ADDR)
#define EXEC_TOTAL          (*(volatile uint32_t *)EXEC_TOTAL_ADDR)
#define DEV_ABS_MAX         (*(volatile uint32_t *)DEV_ABS_MAX_ADDR)
#define DEV_ABS_MAX_SMP     (*(volatile uint32_t *)DEV_ABS_MAX_SMP_ADDR)

// ── N_ENGINE (16B) ──
#define N_ENGINE_BASE           (DTCM_BASE + 0x0040)

#define N_ROUTES_ADDR           (N_ENGINE_BASE + 0x00)      // uint32: 当前路由数
#define N_PARAMS_ADDR           (N_ENGINE_BASE + 0x04)      // uint32
#define N_STATES_ADDR           (N_ENGINE_BASE + 0x08)      // uint32
#define PROGRAM_MAGIC_ADDR      (N_ENGINE_BASE + 0x0C)      // uint32: "PR0G" if valid

// ── ADC/Sensor (112B) ──
#define ADC_RAW_BASE            (DTCM_BASE + 0x0060)
#define ADC_DMA_STATUS_ADDR     (DTCM_BASE + 0x0070)

#define SENSOR_MAP_BASE         (DTCM_BASE + 0x0100)         // 64×float32 = 256B
#define SENSOR_MAP              ((volatile float *)SENSOR_MAP_BASE)

// ── Actuator output (256B) ──
#define ACTUATOR_STATUS_BASE    (DTCM_BASE + 0x0200)         // 64×float32 = 256B
#define ACTUATOR_STATUS         ((volatile float *)ACTUATOR_STATUS_BASE)

// ── GPIO shadow (4B) ──
#define SHADOW_GPIO_ADDR        (DTCM_BASE + 0x00E0)

// ── Engine signal bus (4KB) ──
#define WIRE_MAP_BASE           (DTCM_BASE + 0x0300)         // 1024×float32 = 4KB
#define WIRE_MAP                ((volatile float *)WIRE_MAP_BASE)

// ── Engine LUT (1KB) ──
#define LUT_DATA_BASE           (DTCM_BASE + 0x1300)         // 256×float32 = 1KB
#define LUT_DATA                ((volatile float *)LUT_DATA_BASE)

#define ENGINE_REGION_END       (DTCM_BASE + 0x1700)

// ══════════════════════════════════════════════════════════
// 运行程序区 (PROGRAM REGION) — 0x20001700 ~ 0x200087FF
// ══════════════════════════════════════════════════════════

#define PROGRAM_BASE            (DTCM_BASE + 0x1700)

// ── Program Header (16B) ──
#define PROG_HEADER_BASE        (PROGRAM_BASE + 0x0000)
#define PROG_MAGIC_ADDR         (PROG_HEADER_BASE + 0x00)
#define PROG_VERSION_ADDR       (PROG_HEADER_BASE + 0x04)

// ── Route Table (16KB) ──
#define ROUTE_TABLE_BASE        (PROGRAM_BASE + 0x0010)      // 1024 × 16B = 16KB
#define MAX_ROUTES              1024
#define ROUTE_ENTRY_SIZE        16
#define ROUTE_TABLE             ((volatile RouteEntry_t *)ROUTE_TABLE_BASE)

// ── Param Table (8KB) ──
#define PARAM_TABLE_BASE        (PROGRAM_BASE + 0x4010)      // 512 × 16B = 8KB
#define MAX_PARAMS              512
#define PARAM_ENTRY_SIZE        16
#define PARAM_TABLE             ((volatile ParamEntry_t *)PARAM_TABLE_BASE)

// ── State Table (4KB) ──
#define STATE_TABLE_BASE        (PROGRAM_BASE + 0x6010)      // 256 × 16B = 4KB
#define MAX_STATES              256
#define STATE_ENTRY_SIZE        16
#define STATE_TABLE             ((volatile StateEntry_t *)STATE_TABLE_BASE)

#define PROGRAM_REGION_END      (DTCM_BASE + 0x8800)

// ══════════════════════════════════════════════════════════
// 诊断区 (DIAGNOSTICS) — 0x20008800 ~ 0x2000DFFF
// ══════════════════════════════════════════════════════════

#define RTT_CB_ADDR             (DTCM_BASE + 0x8800)
#define RTT_UP0_BUF             (DTCM_BASE + 0x8900)
#define RTT_UP0_BUF_SIZE        1024
#define RTT_DOWN0_BUF           (DTCM_BASE + 0x8A00)
#define RTT_DOWN0_BUF_SIZE      16

#define LOG_BASE                (DTCM_BASE + 0xD000)
#define LOG_RING_SIZE           (128 * 32)

#define ALARM_BUF_ADDR          (DTCM_BASE + 0xD800)
#define ALARM_BUF_SIZE          2048
#define ALARM_MAX_ENTRIES      (ALARM_BUF_SIZE / 8)

#define REC_BUF_ADDR            (DTCM_BASE + 0xE000)
#define REC_BUF_SIZE            8192


// Dev tracking (used in ISR)
#define DEV_POS_MAX         (*(volatile uint32_t *)((DTCM_BASE + 0x0000) + 0x30))
#define DEV_NEG_MAX         (*(volatile uint32_t *)((DTCM_BASE + 0x0000) + 0x34))
#define PERIOD_EXACT        (*(volatile uint32_t *)((DTCM_BASE + 0x0000) + 0x38))
#define PERIOD_FAR          (*(volatile uint32_t *)((DTCM_BASE + 0x0000) + 0x3C))

// SHADOW_GPIO direct write macro
#define SHADOW_GPIO         (*(volatile uint32_t *)SHADOW_GPIO_ADDR)
#define SIN_LUT_SIZE            4096
#define SIN_LUT                 ((volatile float *)(DTCM_BASE + 0x9000))

// ══════════════════════════════════════════════════════════
// 结构体定义
// ══════════════════════════════════════════════════════════

// Program Header (16 bytes)
typedef struct __attribute__((packed, aligned(4))) {
    uint32_t magic;          // PROGRAM_MAGIC_VALID
    uint32_t version;        // format version
    uint32_t n_routes;       // number of routes
    uint32_t reserved;
} ProgramHeader_t;

// Route Entry (16 bytes) — 与固件现有 RouteEntry_t 保持一致
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
    uint16_t wire2_idx;
} RouteEntry_t;

// Param Entry (16 bytes)
typedef struct __attribute__((packed, aligned(4))) {
    float value_a;
    float value_b;
    float value_c;
    float value_d;
} ParamEntry_t;

// State Entry (16 bytes)
typedef struct __attribute__((packed, aligned(4))) {
    float state_a;
    float state_b;
    float state_c;
    float state_d;
} StateEntry_t;

// 引擎运行状态
typedef enum {
    ENGINE_IDLE = 0,
    ENGINE_RUNNING,
    ENGINE_PAUSED,
    ENGINE_ERROR
} EngineState_t;

#endif // DCL_MEMORY_MAP_H
