# 贡献指南（Contributing）

谢谢你有兴趣让这块确定性引擎变得更好。在动手前，请先了解这个项目的**定位与边界**——这能避免我们双方都白费力气。

## 项目定位
这是一个**研究项目**，不是工业 PLC 产品。它的价值在于验证了"在通用 Cortex-M7 上，用架构（而非调度器运气）消除控制不确定性"这条路线可行，并提供了可复现的测量方法。请基于此定位提 PR。

## 我们欢迎的 PR
- 确定性引擎 ISR 的健壮性 / 测量方法改进（`firmware/h723-core0`）
- DCL 编译器的新原语、语法糖、错误提示（`ide/compiler`）
- 部署 / 监控工具链的可用性修复（`ide/server`、`ide/shell`、`tools`）
- `docs/` 里理论、架构、抖动测量的修正与补充
- 真实被控对象（电机 / 功率电子 / 测试测量）的长期稳定性数据

## 我们**不**接受的 PR
- 引入 ST 官方参考手册（RM0468 等）PDF / zip 等**版权资料**
- 夹带私有 AI 工具缓存（`.mimocode` / `.qoder` / `.workbuddy`）
- 大型生成物（监控 CSV dump、构建目录 `build/` / `bld/`）
- 把项目重新包装成"国产 PLC 替代 / 工业产品"的叙事性改动（与本研究定位冲突）
- 未经验证就声称"对标 / 超过 Beckhoff、B&R、FPGA"的夸大表述

## 开发环境
- MCU：STM32H723ZG（Cortex-M7），编译仅依赖 `main.c` + `startup_stm32h723zgtx.s`
- 部署：SWD（`pyocd`）或 UART（CH340 @ 115200）
- 编译器：`python ide/compiler/dcl_compiler.py program.dcl -o program.bin`

## 提交规范
- 默认分支 `main`，请基于 `main` 开 feature 分支再提 PR
- 提交信息用中文或英文均可，但请描述**为什么**而非只写**做了什么**
- PR 模板里的"测试"项必须填，固件改动需附 bench 实测证据

## 许可证
所有贡献默认以仓库 `LICENSE`（MIT）发布。你提交的代码若含第三方片段，请保留其原许可声明。
