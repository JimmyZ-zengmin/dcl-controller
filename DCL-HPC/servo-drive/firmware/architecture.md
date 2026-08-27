# 伺服固件架构

> 目标平台：SVD-2100 (STM32H723VGT6, LQFP100)
> 固件根目录：`servo-drive/firmware/`

---

## 1. 文件结构

```
firmware/
├── main.c                  # 主循环 + 启动 + 初始化
├── foc.c / foc.h           # FOC 变换 (Park/Clark/SVPWM)
├── pi.c / pi.h             # PI 控制器 (dt 自适应)
├── motor.c / motor.h        # 电机状态机 (启动/运行/故障)
├── encoder.c / encoder.h    # 编码器读取 (SPI DMA / TIM 模式)
├── adc.c / adc.h           # ADC 配置 + DMA 触发
├── dma.c / dma.h           # DMA Stream 初始化 + 内存映射
├── modbus.c / modbus.h      # Modbus RTU 通讯
├── params.c / params.h      # 参数系统 (PI/DTC)
├── safety.c / safety.h      # 安全逻辑 (过流/欠压/温度)
├── startup_stm32h723zgtx.s  # 启动文件
├── STM32H723ZGTX_FLASH.ld   # 链接脚本 (DTCM 定义)
├── build.bat                # 编译脚本
└── flash.bat                # 烧录脚本
```

---

## 2. DMA 配置

### 2.1 DMA1 Stream 分配

```
DMA1_Stream0: ADC 注入组 → DTCM_ADC_RAW (CIRC, 优先级 VERY_HIGH)
  → 每次 ADC 注入组转换完成 → DMA 搬 JDR1/JDR2/JDR3/Vbus → DTCM
  → CIRC 模式：DMA 写完最后一字后自动回到起始地址
  → CPU 读：*DTCM_ADC_RAW 总是最新的电流值
  → 带宽：32kHz (PWM 量次采样)
  → 数据量：4 × 32-bit = 16 bytes/传输

DMA1_Stream1: SPI1_RX → DTCM_ENCODER (CIRC, 优先级 HIGH)
  → TIM2_CH1 触发 → SPI1 时钟输出 → 编码器返回位置数据
  → DMA 将 SPI1_DR 收到的数据写到 DTCM_ENCODER
  → CPU 读：*DTCM_ENCODER 总是最新编码器位置
  → 触发率：1μs (TIM2_CH1 @ 100ns)
  → 数据量：1 × 32-bit = 4 bytes/传输

DMA1_Stream2: SPI1_TX ← SHADOW_ENCODER_CMD (CIRC, 优先级 HIGH)
  → 与 Stream1 配对使用
  → 每次 SPI 传输前 CPU 可能需要写命令 (BiSS-C 需要发送请求)
  → 简化版：Stream2 从固定的 SHADOW_ENCODER_CMD 地址读数据发出去
```

### 2.2 DMA2 Stream 分配

```
DMA2_Stream5: SHADOW_DUTY → TIM1_CCR123 (CIRC, 优先级 VERY_HIGH)
  → TIM2_CH4 触发 → DMA 读 SHADOW_CCR1/CCR2/CCR3 → 写 TIM1_CCR1/2/3
  → CPU 写：*SHADOW_CCR1 = duty_U (随时写, 影子寄存器安全)
  → DMA 在子周期触发点更新 CCR
  → 触发率：1μs × 3 = 3μs 一次完整传输
  → 数据量：3 × 32-bit = 12 bytes/传输
```

### 2.3 DMAMUX 路由表

| 请求 ID | DMAMUX 通道 | DMA 流 | 触发源 | 作用 |
|---------|------------|--------|-------|------|
| 1 | DMAMUX1_CH0 | DMA1_S0 | TIM1_TRGO (OC4REF) | ADC 注入组 → DTCM |
| 2 | DMAMUX1_CH1 | DMA1_S1 | TIM2_CH1 | SPI 读编码器 → DTCM |
| 3 | DMAMUX1_CH2 | DMA1_S2 | TIM2_CH1 (同源) | SPI 写编码器命令 |
| 4 | DMAMUX1_CH3 | DMA2_S5 | TIM2_CH4 | SHADOW → TIM1_CCR |

**评估：** [C] 4 个 DMAMUX 通道，DMAMUX1 总共 16 通道，只用了 4 个，余量充足。

### 2.4 DMA 配置代码 (要点)

```c
static void dma_init(void) {
    // ADC → DTCM (不关持续搬运)
    DMA1_Stream0->
        CR = DMA_SxCR_CIRC        // CIRC 模式
           | DMA_SxCR_PL_VERY_HIGH  // 最高优先级
           | DMA_SxCR_MSIZE_32BIT   // 源数据宽度 32-bit
           | DMA_SxCR_PSIZE_32BIT   // 外设数据宽度 32-bit
           | DMA_SxCR_DIR_P2M;      // 外设→内存
    DMA1_Stream0->
        PAR = (uint32_t)&ADC1->JDR1;  // ADC 注入组数据寄存器
    DMA1_Stream0->
        M0AR = (uint32_t)DTCM_ADC_RAW; // DTCM 目标地址
    DMA1_Stream0->
        NDTR = 4;  // 每次传输 4 字 (Iu/Iv/Iw/Vbus)

    // SHADOW → TIM1_CCR (不关持续搬运)
    DMA2_Stream5->
        CR = DMA_SxCR_CIRC
           | DMA_SxCR_PL_VERY_HIGH
           | DMA_SxCR_MSIZE_32BIT
           | DMA_SxCR_PSIZE_32BIT
           | DMA_SxCR_DIR_M2P;        // 内存→外设
    DMA2_Stream5->
        PAR = (uint32_t)&TIM1->CCR1;   // TIM1 目标地址
    DMA2_Stream5->
        M0AR = (uint32_t)DTCM_SHADOW_CCR1; // SHADOW 区域
    DMA2_Stream5->
        NDTR = 3;  // 每次传输 3 字 (CCR1/2/3)
}
```

**评估：** ⚠️ [R] DMA2_Stream5 的 M2P + CIRC 是否支持连续不中断？CIRC 模式下 NDTR 到达 0 后自动重载。问题在于重载期间是否丢触发——当 DMA 在重载的 1-2 个时钟周期内收到触发请求时，请求会被忽略。TIM2_CH4 触发间隔 1μs >> DMA 重载时间（<8ns），不会丢。

---

## 3. FOC 核心

### 3.1 代码结构

```c
// FOC 电流环 — 一次调用完成 Park + PI + InvPark + SVPWM
// 所有运算使用 IQ24 定点 (避免浮点运算延迟)
// 输入: Iu/Iv/Iw 原始 ADC 值, 编码器位置
// 输出: 写入 SHADOW_DUTY (DMA 自动搬运到 TIM1_CCR)

static inline void foc_current_loop(
    int32_t Iu, int32_t Iv, int32_t Iw,   // ADC 原始值 (12-bit)
    int32_t theta_raw,                      // 编码器位置 (0-65535 对应 0-360°)
    int32_t Id_ref, int32_t Iq_ref,         // 电流指令
    int32_t *shadow_ccr1,                   // 输出: U 相占空比
    int32_t *shadow_ccr2,                   // 输出: V 相占空比
    int32_t *shadow_ccr3)                   // 输出: W 相占空比
{
    // 1. Clark 变换 (Ia,Ib,Ic → Iα,Iβ) [~10 cyc]
    int32_t Ialpha = Iu;                    // Iα = Ia = Iu
    int32_t Ibeta  = (Iv - Iw) * INV_SQRT3; // Iβ = (Iv-Iw)/√3

    // 2. Park 变换 (Iα,Iβ → Id,Iq) [~20 cyc]
    int32_t cos_t = cos_lookup(theta_raw);
    int32_t sin_t = sin_lookup(theta_raw);
    int32_t Id = (Ialpha * cos_t + Ibeta * sin_t) >> 24;
    int32_t Iq = (-Ialpha * sin_t + Ibeta * cos_t) >> 24;

    // 3. PI 电流控制器 × 2 [~40 cyc]
    int32_t Vd = pi_update(&pi_d, Id_ref - Id);
    int32_t Vq = pi_update(&pi_q, Iq_ref - Iq);

    // 4. InvPark (Vd,Vq → Vα,Vβ) [~15 cyc]
    int32_t Valpha = (Vd * cos_t - Vq * sin_t) >> 24;
    int32_t Vbeta  = (Vd * sin_t + Vq * cos_t) >> 24;

    // 5. SVPWM (Vα,Vβ → 三相互补占空比) [~25 cyc]
    svpwm(Valpha, Vbeta, shadow_ccr1, shadow_ccr2, shadow_ccr3);
    
    // 总计 ~110 cyc ≈ 230ns @480MHz
    // 基础版 240MHz → ~460ns — 仍远快于 1-2kHz (=500μs) 的需求
}
```

### 3.2 PI 控制器 (dt 自适应)

```c
typedef struct {
    int32_t Kp;       // 比例增益 (IQ24)
    int32_t Ki;       // 积分增益 (IQ24, per ns)
    int32_t integral;  // 积分项
    int32_t out_max;   // 输出限幅
    int32_t i_max;     // 积分限幅 (anti-windup)
    uint32_t last_t;   // 上次 TIM2_CNT
    int32_t prev_err;  // 上次误差 (用于可选的微分项)
} PI_Controller;

static inline int32_t pi_update(PI_Controller *pi, int32_t err) {
    uint32_t now = TIM2_CNT;
    uint32_t dt  = now - pi->last_t;      // 实际经过的 ns (TIM2 1 tick = 4.16ns)
    pi->last_t = now;

    // 保护: dt 不可能为 0
    if (dt == 0) return pi->integral;

    // 核心: Ki 乘以实际 dt (ns)
    pi->integral += (err * pi->Ki * dt) >> 24;  // Ki_ns × dt_ns

    // 积分限幅 (anti-windup)
    if (pi->integral > pi->i_max)  pi->integral = pi->i_max;
    if (pi->integral < -pi->i_max) pi->integral = -pi->i_max;

    int32_t out = (err * pi->Kp) >> 24 + pi->integral;

    // 输出限幅
    if (out > pi->out_max)  out = pi->out_max;
    if (out < -pi->out_max) out = -pi->out_max;

    return out;
}
```

**评估：** [C] dt 自适应是正确的。关键在 Ki 的单位是 "per ns"——Ki_ns = Ki_Hz / 1e9。增益范围必须留够余量：在 480MHz 下 dt 最小 ~2ns (两次连续迭代)，最差情况下 Ki×2ns 不会导致溢出（IQ24 格式有足够动态范围）。

---

## 4. 启动状态机

### 4.1 状态定义

```c
typedef enum {
    MOTOR_IDLE       = 0,  // 等待使能
    MOTOR_ALIGN      = 1,  // 转子对齐 (输出固定矢量, 100ms)
    MOTOR_OPENLOOP   = 2,  // 开环加速 (0→10Hz, 500ms)
    MOTOR_CLOSEDLOOP = 3,  // 闭环运行 (正常)
    MOTOR_FAULT      = 4,  // 故障
} MotorState;
```

### 4.2 状态切换逻辑 (TIM5 被动计时)

```c
MotorState motor_state = MOTOR_IDLE;
uint32_t   state_start = 0;  // 状态开始时的 TIM5_CNT

void motor_fsm(void) {
    uint32_t now = TIM5_CNT;

    switch (motor_state) {
    case MOTOR_IDLE:
        // 检查使能信号 (Modbus 或硬件使能引脚)
        if (digit_enable_pin && *DTCM_VBUS > VBUS_OK) {
            motor_state = MOTOR_ALIGN;
            state_start = now;
            // 输出对齐矢量: Ud = 额定电流 10%, Uq = 0
            *SHADOW_CCR1 = align_duty_x;
            *SHADOW_CCR2 = align_duty_y;
            *SHADOW_CCR3 = align_duty_z;
        }
        break;

    case MOTOR_ALIGN:
        // 等待 100ms
        if (now - state_start > ALIGN_TIME_CYCLES) {  // 100ms = 50M cyc @500MHz
            motor_state = MOTOR_OPENLOOP;
            state_start = now;
            openloop_speed = 0;
        }
        break;

    case MOTOR_OPENLOOP:
        // 线性加速: 0Hz → 10Hz, 耗时 500ms
        speed_ramp = (now - state_start) * 20 / OPENLOOP_TIME_CYCLES; // 0→20Hz
        if (speed_ramp > 20) speed_ramp = 20;

        // 输出开环旋转磁场
        openloop_theta += speed_ramp * delta_t;
        foc_current_loop(align_I, align_I, align_I,
                        openloop_theta, 0, Iq_openloop,
                        &d1, &d2, &d3);

        // 检查编码器是否有效
        if (*DTCM_ENCODER != ENCODER_INVALID
            && (now - state_start) > MIN_OPENLOOP_CYCLES) {
            motor_state = MOTOR_CLOSEDLOOP;
            state_start = now;
            pi_d.integral = 0;
            pi_q.integral = 0;
        }
        break;

    case MOTOR_CLOSEDLOOP:
        // 正常闭环 — 见上文 foc_current_loop
        break;

    case MOTOR_FAULT:
        // 输出关闭, 等待复位信号
        *SHADOW_CCR1 = 0;
        *SHADOW_CCR2 = 0;
        *SHADOW_CCR3 = 0;
        if (fault_reset_condition) { motor_state = MOTOR_IDLE; }
        break;
    }
}
```

**评估：** ⚠️ [R] ALIGN 阶段 100ms 内 CPU 什么都没做（只是空检查状态），浪费算力。但可以接受——100ms 内 CPU 可以做自检、校准 ADC、初始化通讯。这些任务可以在 MOTOR_ALIGN 的 case 分支里并行做。

---

## 5. Modbus 通讯

### 5.1 寄存器映射

| 地址 | 访问 | 类型 | 含义 | 范围 |
|------|------|------|------|------|
| 0x0000 | R/W | int16 | 状态控制 (使能/复位) | 0-1 |
| 0x0001 | R/W | int16 | 速度指令 (RPM) | -3000~3000 |
| 0x0002 | R/W | int16 | 转矩指令 (%) | -100~100 |
| 0x0003 | R/W | int16 | 位置指令 (encoder counts) | 0~65535 |
| 0x0010 | R | int16 | 实际速度 (RPM) | - |
| 0x0011 | R | int16 | 实际转矩 (%) | - |
| 0x0012 | R | int32 | 实际位置 (编码器计数值) | - |
| 0x0014 | R | int16 | 母线电压 (V) | - |
| 0x0015 | R | int16 | Iu (mA) | - |
| 0x0016 | R | int16 | Iv (mA) | - |
| 0x0017 | R | int16 | Iw (mA) | - |
| 0x0020 | R/W | int16 | 故障码 | 0=正常 |
| 0x0100 | R/W | int32 | Kp 电流 (×1000) | - |
| 0x0102 | R/W | int32 | Ki 电流 (×1000) | - |

### 5.2 通讯调度

```c
void modbus_poll(void) {
    // 只在主循环的 "通讯间隙" 调用 (每 ~1000 次迭代)
    // 一次调用最多处理一个帧
    if (UART2_bytes_available() >= 8) {  // Modbus RTU 最小帧 8 字节
        uint8_t frame[256];
        int len = read_uart2_frame(frame);
        if (len > 0) {
            modbus_process_frame(frame, len);
        }
    }
}
```

**评估：** [C] 1000 次迭代 @ ~50ns/次 ≈ 每 50μs 检查一次通讯。Modbus RTU 帧长 ~20 字节 @ 115200bps ≈ 1.7ms 传输时间。50μs 的轮询间隔远快于 1.7ms，不会丢帧。

---

## 6. 编译与部署

### 6.1 编译脚本

```makefile
# build.bat
arm-none-eabi-gcc -mcpu=cortex-m7 -mfpu=fpv5-d16 -mfloat-abi=hard \
    -O2 -g3 -ffunction-sections -fdata-sections \
    -DSTM32H723ZGTx \
    -c main.c -o bld/main.o

arm-none-eabi-gcc -mcpu=cortex-m7 -mfpu=fpv5-d16 -mfloat-abi=hard \
    -O2 -g3 \
    -T STM32H723ZGTX_FLASH.ld \
    -Wl,--gc-sections \
    bld/*.o -o bld/servo.elf

arm-none-eabi-objcopy -O binary bld/servo.elf bld/servo.bin
```

### 6.2 烧录脚本

```bash
# flash.bat (使用 pyocd)
pyocd flash -t stm32h723xx bld/servo.bin
```

---

## 7. 未固化的设计决策

| # | 决策 | 选项 | 推荐 | 依据 |
|---|------|------|------|------|
| D1 | FOC 定点 vs 浮点 | 定点 IQ24 / 硬件浮点 | **定点 IQ24** | FPU 延迟 14 cyc × 每次 FOC 调用，浮点总时间反超定点 |
| D2 | sin/cos 查表 vs 计算 | 2KB 表 / CORDIC 计算 | **查表 + 线性插值** | 1° 分辨率表 360 项 × 4 byte = 1.4KB，查表 1 cyc，CORDIC 30+ cyc |
| D3 | 编码器 SPI 时钟 | 1MHz / 5MHz / 10MHz | **5MHz** | SSI/BiSS-C 标准支持 5MHz，1MHz 太慢(5μs/读)，10MHz 不稳定 |
| D4 | PWM 载波频率 | 8kHz/10kHz/16kHz/20kHz | **16kHz** | 工业标准，样时兼顾噪声和开关损耗 |
| D5 | 电流环更新时机 | 每次 ADC 完成 / 每 N 次主循环 | **ADC 完成即算** | DMA 写 DTCM 后 CPU 下次迭代自动读到新值，不需要额外同步 |
