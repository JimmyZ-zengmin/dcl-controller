# seq_demo.dcl — Sequencer v0 文本组态示例 (顺序域 DSL)
# 语义: 4 步循环机 — 条件快步 1/3 (ok 恒真立即过), 停留步 2/4 (各 1s)
#   → 步号节律: 1 →(1s) 2 → 3 →(1s) 4 → 回卷 1 (loop)
# 步号镜像: wire[30]; 译码: 步号>=3.5 (第 4 步进行中) → wire[31]=1
# 注: SEQ 块须在文件末尾 (v0.2 语法约束)

CONST   ok      = 1.0                   # 恒真条件源 (演示; 真实条件接传感器/路由)

# 译码走路由网 (B1: wire[30] 由 SEQ 声明为生产者, 路由只读它 → 输出到 wire[31])
GE      phase4  IN=wire[30] THR=3.5      # 步号 >= 4 (第 4 步进行中)
OUTPUT  act     TO wire[31] FROM phase4  # "完成段"输出镜像

# ---- 顺序域 (文件尾) ----
SEQ     cycle   TO wire[30] PERIOD=10ms  # 顺序实例: 步号写 wire[30], 10ms 档
  UNTIL ok > 0.5                         # step0: 条件满足 → 进 step1
  DWELL 1s                               # step1: 停留 1 秒 → 强推
  UNTIL ok > 0.5                         # step2: 立即过
  DWELL 1s                               # step3: 停留 1 秒 (末步)
  LOOP                                   # 末步回卷到 step0
