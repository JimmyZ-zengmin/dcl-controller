# h723_di_demo.dcl — 数字量输入域 文本组态示例 (H723 原生)
#
# ★ 与 S3 版本的关键差异 (不是"简化", 是本平台的事实):
#   S3 用 GPIO1/2/3/9; **H723 是 PC0..PC3** (见 src/di.c)。照抄 S3 的注释会写出错的脚位。
#   去抖: 30ms; 每轮无条件回填 SENSOR[3..6] (不依赖"变化才写" —— 冷启动 memset 会清掉
#   那些槽, 只在变化时写会导致复位后永不回填, 这是本项目踩过的坑)。
#
# 硬件操作 (外部判据需要它, 无接线时该判据记 SKIP 而不是 FAIL):
#   PC0 悬空 = 1 (内部上拉); 用杜邦线把 PC0 短到 GND = 0
#
# 逻辑解读: "低有效" —— 按下/接地 → di1=0 → btn1=1

# ---- 输入 (DI 组件每轮扫描写入 SENSOR[3..6]) ----
SENSOR  di1     FROM sensor[3]       # PC0 (上拉: 悬空=1, 接地=0)
SENSOR  di2     FROM sensor[4]       # PC1
SENSOR  di3     FROM sensor[5]       # PC2
SENSOR  di4     FROM sensor[6]       # PC3

# ---- 逻辑: 低有效解读 ----
LOGIC   btn1    = NOT di1            # PC0 接地 → btn1=1
LOGIC   btn2    = NOT di2            # PC1 接地 → btn2=1

# ---- 联锁: 两个按钮同时动作 → 输出 ----
LOGIC   both    = btn1 AND btn2
OUTPUT  lamp    TO wire[30] FROM both

# ---- 计数: 按钮每按一次 +1 (DI 与标准计数块的连接) ----
R_TRIG  press   CLK=btn1
CTU     presses CU=press PV=1000     # 按压计数 → CV
ALARM   many    FROM presses >= 5    # 按够 5 次 → 1.0
OUTPUT  cnt     TO wire[31] FROM presses
OUTPUT  alarm   TO wire[32] FROM many
