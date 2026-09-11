# h723_seq_demo.dcl — 顺序域文本组态示例 (H723 原生)
#
# 语义: 4 步循环机 —— 瞬时步 (条件恒真, 立即过) 与停留步 (各 1s) 交替
#   步号节律: 1 →(10ms) 2 →(1s) 3 →(10ms) 4 →(1s) → 回卷 1    整周期 ≈ 2.02s
#   步号镜像: wire[30]；译码: 步号 >= 3.5 (第 4 步进行中) → wire[31] = 1
#
# ★ 与 S3 的 examples/seq_demo.dcl 是**同一份程序语义**, 只改了注释口径:
#   本平台的"1s 停留"与"10ms 瞬过"都是 **PERIOD=10ms 档桶**上的节拍
#   (顺序域按 div2 档推进), 所以瞬时步的可见时长是 ~10ms 而不是 0 —— 这一点
#   在 S3 文档里没写清, 实测 (2026-09-11, tools/h723_demo_e2e.py) 是 8~13ms。
#
# 注: SEQ 块须在文件末尾 (v0.2 语法约束)

CONST   ok      = 1.0                   # 恒真条件源 (真实工程里接传感器/路由)

# 译码走路由网 (B1: wire[30] 由 SEQ 声明为生产者, 路由只读它 → 输出到 wire[31])
GE      phase4  IN=wire[30] THR=3.5     # 步号 >= 4 (第 4 步进行中)
OUTPUT  act     TO wire[31] FROM phase4

# ---- 顺序域 (文件尾) ----
SEQ     cycle   TO wire[30] PERIOD=10ms
  UNTIL ok > 0.5                        # step0: 条件立真 → 一个档桶后过
  DWELL 1s                              # step1: 停留 1 秒 → 强推
  UNTIL ok > 0.5                        # step2: 立即过
  DWELL 1s                              # step3: 停留 1 秒 (末步)
  LOOP                                  # 末步回卷到 step0
