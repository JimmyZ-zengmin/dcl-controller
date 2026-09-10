# ST 官方 H723 时钟配置参考（原文留档）

来源：`STM32CubeH7/Projects/NUCLEO-H723ZG/Templates/Src/main.c`
仓库：https://github.com/STMicroelectronics/STM32CubeH7
抓取日期：2026-09-10

---

## 一、官方声明的工作点（文件头注释原文）

```
System Clock source            = PLL (HSE BYPASS)
SYSCLK(Hz)                     = 520000000 (CPU Clock)
HCLK(Hz)                       = 260000000 (AXI and AHBs Clock)
AHB Prescaler                  = 2
D1 APB3 Prescaler              = 2 (APB3 Clock  130MHz)
D2 APB1 Prescaler              = 2 (APB1 Clock  130MHz)
D2 APB2 Prescaler              = 2 (APB2 Clock  130MHz)
D3 APB4 Prescaler              = 2 (APB4 Clock  130MHz)
HSE Frequency(Hz)              = 8000000
PLL_M                          = 4
PLL_N                          = 260
PLL_P                          = 1
PLL_Q                          = 4
PLL_R                          = 2
VDD(V)                         = 3.3
Flash Latency(WS)              = 3
```

**★ 关键：ST 官方模板的工作点是 520MHz，不是 550MHz。**
这与社区结论一致：`CPUFREQ_BOOST` 选项字节未开时，VOS0 下 CPU 上限为 **520MHz**
（betaflight 注释 + ST 工程师答复）。550MHz 必须开 `CPUFREQ_BOOST`，
其代价是 **关闭 ITCM/DTCM 的 ECC**（RM0468: "The ECC is always active except when the
CPU frequency boost feature is used. In that case the ECC is no more active on TCM RAMs."）。

---

## 二、`SystemClock_Config()` 原文

```c
static void SystemClock_Config(void)
{
  RCC_ClkInitTypeDef RCC_ClkInitStruct;
  RCC_OscInitTypeDef RCC_OscInitStruct;
  HAL_StatusTypeDef ret = HAL_OK;

  /* The voltage scaling allows optimizing the power consumption when the device is
     clocked below the maximum system frequency, to update the voltage scaling value
     regarding system frequency refer to product datasheet.  */
  __HAL_PWR_VOLTAGESCALING_CONFIG(PWR_REGULATOR_VOLTAGE_SCALE0);

  while(!__HAL_PWR_GET_FLAG(PWR_FLAG_VOSRDY)) {}

  /* Enable HSE Oscillator and activate PLL with HSE as source */
  RCC_OscInitStruct.OscillatorType = RCC_OSCILLATORTYPE_HSE;
  RCC_OscInitStruct.HSEState = RCC_HSE_BYPASS;
  RCC_OscInitStruct.HSIState = RCC_HSI_OFF;
  RCC_OscInitStruct.CSIState = RCC_CSI_OFF;
  RCC_OscInitStruct.PLL.PLLState = RCC_PLL_ON;
  RCC_OscInitStruct.PLL.PLLSource = RCC_PLLSOURCE_HSE;

  RCC_OscInitStruct.PLL.PLLM = 4;
  RCC_OscInitStruct.PLL.PLLN = 260;
  RCC_OscInitStruct.PLL.PLLFRACN = 0;
  RCC_OscInitStruct.PLL.PLLP = 1;
  RCC_OscInitStruct.PLL.PLLR = 2;
  RCC_OscInitStruct.PLL.PLLQ = 4;

  RCC_OscInitStruct.PLL.PLLVCOSEL = RCC_PLL1VCOWIDE;
  RCC_OscInitStruct.PLL.PLLRGE = RCC_PLL1VCIRANGE_1;
  ret = HAL_RCC_OscConfig(&RCC_OscInitStruct);
  if(ret != HAL_OK)
  {
    Error_Handler();
  }

/* Select PLL as system clock source and configure  bus clocks dividers */
  RCC_ClkInitStruct.ClockType = (RCC_CLOCKTYPE_SYSCLK | RCC_CLOCKTYPE_HCLK | RCC_CLOCKTYPE_D1PCLK1 | RCC_CLOCKTYPE_PCLK1 | \
                                 RCC_CLOCKTYPE_PCLK2  | RCC_CLOCKTYPE_D3PCLK1);

  RCC_ClkInitStruct.SYSCLKSource = RCC_SYSCLKSOURCE_PLLCLK;
  RCC_ClkInitStruct.SYSCLKDivider = RCC_SYSCLK_DIV1;
  RCC_ClkInitStruct.AHBCLKDivider = RCC_HCLK_DIV2;
  RCC_ClkInitStruct.APB3CLKDivider = RCC_APB3_DIV2;
  RCC_ClkInitStruct.APB1CLKDivider = RCC_APB1_DIV2;
  RCC_ClkInitStruct.APB2CLKDivider = RCC_APB2_DIV2;
  RCC_ClkInitStruct.APB4CLKDivider = RCC_APB4_DIV2;
  ret = HAL_RCC_ClockConfig(&RCC_ClkInitStruct, FLASH_LATENCY_3);
  if(ret != HAL_OK)
  {
    Error_Handler();
  }
}
```

---

## 三、与本项目对照

| 项 | ST 官方 | 本项目（h723/src/clock.c）| 判定 |
|---|---|---|---|
| VOS | `SCALE0` + 等 **VOSRDY** | 同 | ✅ 一致 |
| Flash WS | **3** | 3 | ✅ 一致 |
| SYSCLK 源 | PLL1P | 同 | ✅ 一致 |
| SYSCLKDivider | /1（CPU = SYSCLK） | 同 | ✅ 一致 |
| AHB / APB | /2 / /2 | 同 | ✅ 一致 |
| PLL VCO 范围 | `RCC_PLL1VCOWIDE` | VCOSEL=0 | ✅ 一致 |
| PLL 输出使能 | HAL 自动置 DIVP1EN | 手动置 **bit16** | ✅ 一致（此位曾漏 → SWS 永不跟随）|
| VOS 切换方式 | H72x/73x **直接写 D3CR** | 同 | ✅ 一致 |
| PLL 输入 | HSE 8MHz / 4 = **2MHz** → RGE_1 | HSE 25MHz / 5 = **5MHz** → RGE_2 | 都合法（各自匹配输入）|
| HSE 模式 | `HSE_BYPASS`（外部时钟源） | 复位默认 HSEBYP=1 | ✅ 形态一致 |
| HSI/CSI | 显式 **OFF** | 未显式关闭 | ⚠️ 差异（待验证是否有关）|

**结论：本项目的时钟配置方法学与 ST 官方一致，已排除"做法错误"。**

---

## 四、HAL 源码佐证（VOS 处理）

`Drivers/STM32H7xx_HAL_Driver/Src/stm32h7xx_hal_pwr_ex.c` —
`HAL_PWREx_ControlVoltageScaling()` 中对 H72x/73x 的分支：

```c
#else  /* STM32H72xxx and STM32H73xxx lines */
  /* Set the voltage range */
  MODIFY_REG(PWR->D3CR, PWR_D3CR_VOS, VoltageScaling);
#endif /* defined (SYSCFG_PWRCR_ODEN) */
```

而 `SYSCFG_PWRCR_ODEN` 分支（"逐级 Scale3→2→1→0"、overdrive）**明确标注
只属于 H74x/H75x**。本机实测 `SYSCFG_PWRCR`(0x5800042C) 写入无效、恒 0 —— 印证 H723
无此位，**不需要逐级切换**。

---

## 五、本项目实测频率上限（固件自证法）

判据：`g_stage == 7`（TIM2 ISR 在跑）+ `g_tick_count > 0` = 完整跑通。

| CPU | 结果 |
|---|---|
| 300 / 340 / 380 / 400 / 420 / **450 MHz** | ✅ 完整跑通 |
| 500 / 520 / 550 MHz | ❌ 崩（500 崩在 clock_init 之后，550 崩在其内部）|

**真实断崖在 450~500MHz 之间。**

已实测排除：FLASH 等待态（3/7/15 无差别）、HCLK 带宽（HPRE=/4 也崩）、
VOS 写入与否、PWR_CR3 供电配置、SYSCFG ODEN、VCO 加倍（1100MHz/DIVP=2）。

**★ 与官方 520MHz 可用对照 → 本板尚存在未定位的差异（优先怀疑板级硬件：
VCAP 电容 / 电源质量）。**
