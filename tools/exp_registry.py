#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""exp_registry.py —— 实验登记表（唯一源）+ docs/EXP-INDEX.md 的生成器

## 为什么要有它（这就是「系统化」而不是「再加一份文档」）
实测现状（2026-09-19）：21 份实验文档 · 3 个实验目录 · 31 个实验工具，而
doc 与 tool 不是一一对应：
  · E-A/E-B/E-C/E-E1/E-O/E-T 有工具但没有专文（结论散在别的文里或只在 MEMORY）
  · 同一实验有多个工具（E-I 两个、E-O 两个）
  · 文档名与实验号不齐（exp-Cscan-decomposition.md 其实是 E-T 的一部分）
=> 于是「某个实验做过什么、用什么跑的、结论是什么」只能靠人翻目录。
本工具把这个映射收成一份可校验的数据，并生成索引 => 索引永远不会与事实漂移。

## 三个模式（都能失败 —— 这是本项目的入场券）
  --index   生成 docs/EXP-INDEX.md（标「生成物，勿手改」）
  --check   校验：每个登记项的 tool/doc 文件必须存在；--index 的产物必须与登记表
            逐字节一致（否则判「索引过期」）；状态字段必须取自枚举
  --list    只打印一张表（不写盘）
退出码: 0 = 全过 / 1 = 有 FAIL
"""
import io
import os
import sys

R = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(R, 'docs', 'EXP-INDEX.md')
STATUS = ('完成', '完成（含负结果）', '完成（含撤回）', '完成（在板项挂台架）')

# ══════════════════════════════════════════════════════════════════════════════
# 登记表 —— 本表是「实验」这件事的唯一源（改这里，然后跑 --index）
#   goal    验证目标（一句话，可判定）
#   steps   实验流程（3~4 步，可复跑的顺序）
#   tools   实验工具（tools/ 下，必须存在）
#   docs    实验文档（docs/ 下；空 = 如实标「无专文」）
#   result  结果（只写能从文档/台账引证的；写不出数字就写定性结论）
# ══════════════════════════════════════════════════════════════════════════════
EXPS = [
    dict(id='E-A/B/C', name='div0 的 2.1 倍之谜破除 · 桶开销销案 · 19 原语 m_op 全表',
         goal='成本模型第一版在 div0 上差 2.1 倍：是模型错还是实现错？',
         steps=['两点法逐原语标定（n=0 与 n=128 做差分）',
                '把「每拍固定开销」与「每条路由成本」分离',
                '19 个原语各测一遍，与 Python 预测逐项对账'],
         tools=['tools/exp_ea.py', 'tools/exp_ea_fit.py', 'tools/exp_eb.py', 'tools/exp_ec.py'],
         docs=[],
         result='m_op 全表拿到；2.1 倍来自「每条成本随 op 变」而不是实现缺陷',
         status='完成（含撤回）'),
    dict(id='E-Cscan', name='C_scan 分解：扫描段只由两项构成',
         goal='扫描段（ISR 里的路由扫描）由哪些项决定？',
         steps=['程序条数扫 0/32/64/128', '每个规模夹取扫描段（调用前后各读一次时基）',
                '对条数做线性拟合，看残差能否被 op 混合解释'],
         tools=['tools/exp_cscan_split.py'],
         docs=['docs/exp-Cscan-decomposition.md'],
         result='两项闭合（固定项 + 逐条项）；m_op 被两条独立方法交叉验证（差 1.0%）',
         status='完成'),
    dict(id='E-D', name='扫描段已从整段 ISR 中夹出',
         goal='能不能只量「扫描段」而不是整段 ISR？',
         steps=['在 engine_tick 调用前后各读一次时基', '把差值写进 OFF_SCAN_CYC_*',
                '空程序与满程序对照，确认夹取区间只含扫描'],
         tools=['tools/exp_ed.py'],
         docs=['docs/exp-ED-scan-isolation.md'],
         result='拿到「拍内其它工作 = 474 TB」这个新常数',
         status='完成'),
    dict(id='E-E1', name='口径修好 —— 模型第一次三项闭合',
         goal='统一口径后，模型能否三项同时闭合？',
         steps=['修口径（源成本与扫描成本的分子分母同口径）', '用同一程序复算三项',
                '与实测差分逐项比对'],
         tools=['tools/exp_ee1.py'],
         docs=[],
         result='模型第一次三项闭合；m_op 逐位复现；并纠正了我自己的判据',
         status='完成（含撤回）'),
    dict(id='E-F', name='m_op 不能从指令级吞吐下界推导（负结果）',
         goal='能不能不靠实测、纯从反汇编结构推出 m_op？',
         steps=['反汇编扫描体', '按指令级吞吐算下界', '与实测 m_op 比'],
         tools=['tools/exp_ef_deducible.py'],
         docs=['docs/exp-EF-mop-deducibility.md'],
         result='负结果：推不出来（证据本身也不够干净，已如实记）=> 「可算」是标定，不是纯计算',
         status='完成（含负结果）'),
    dict(id='E-G', name='m_op 的程序无关性验证 + 一次失败的测试设计',
         goal='m_op 是「原语属性」还是「程序属性」？',
         steps=['构造让 m_op 可能随程序变的排列', '对照同一 op 在不同程序里的成本',
                '撤回一次设计失败的测试'],
         tools=['tools/exp_eg_mop_validity.py'],
         docs=['docs/exp-EG-mop-validity.md'],
         result='有效证据汇总 + 撤回一次失败的测试设计 + 登记一个桶表矛盾',
         status='完成（含撤回）'),
    dict(id='E-H', name='桶表矛盾判定（实测 vs 自相矛盾的固件状态）',
         goal='桶表分桶与路由表 period 是否一致？矛盾在哪？',
         steps=['从路由表逐条取 period，自己算相位直方图', '与固件桶表逐槽对照',
                '定位「同一语义三处存放（100/64/63）」的根因'],
         tools=['tools/exp_eh_bucket_probe.py'],
         docs=['docs/exp-EH-bucket-contradiction.md'],
         result='根因 = 一次「改了一半」的修正；同一个语义三处存放',
         status='完成'),
    dict(id='E-I', name='重标定：m_op 在两代固件上逐位复现',
         goal='修完 div2 相位缺陷后，m_op 还是同一张表吗？',
         steps=['在修复后的固件上重跑两点法', '与上一代固件逐项比', '算最大偏差'],
         tools=['tools/exp_ei_recalib.py', 'tools/exp_ei_b.py'],
         docs=['docs/exp-EI-recalibration.md'],
         result='逐位复现（不超过 0.6%）=> 「原语属性」从假设变成事实',
         status='完成'),
    dict(id='E-J', name='分档吞吐：分档真的把每条路由都轮到了吗',
         goal='行为判据：分档调度的实际吞吐与结构预测是否一致？',
         steps=['按桶表预测 nrun 的均值', '实测 Δroutes_total 除以 Δticks',
                '与预测比（偏差即缺陷证据）'],
         tools=['tools/exp_ej_throughput.py'],
         docs=['docs/exp-EJ-throughput.md'],
         result='偏差 0.00%（结构对等于行为对）；并发现残留代价是 dt 偏 56%',
         status='完成'),
    dict(id='E-K', name='端到端留点验证：模型能算一个程序的执行时间',
         goal='模型能否在没见过的程序上算准执行时间（不只是拟合）？',
         steps=['取留点程序（不在标定集里）', '用模型算预算', '部署实测 di 并与预测比',
                '跨 division 再验一遍'],
         tools=['tools/exp_ek_holdout.py'],
         docs=['docs/exp-EK-holdout.md'],
         result='留点误差 0.04%，且跨 division 成立（3.1%）',
         status='完成'),
    dict(id='E-L', name='混合 op：可加性不成立，但上界成立',
         goal='把每条成本相加能不能当上界？',
         steps=['构造 PID 与 DIRECT 各半的排列', '只改排列不改条数，测 di',
                '拟合「相邻 op 改变次数」的斜率'],
         tools=['tools/exp_el_mixed.py'],
         docs=['docs/exp-EL-mixed-op.md'],
         result='可加性不成立（低估最多 5.3%）；但含转变项的上界成立且部署期可算',
         status='完成（含负结果）'),
    dict(id='E-M', name='把转变项加进门的预算',
         goal='门必须用上界 => 转变项怎么进公式？',
         steps=['写出含 trans 项的预算式', '与手工算术逐位对账（5 个样例）',
                '部署验证（不再放行超载）'],
         tools=['tools/exp_em_budget.py'],
         docs=['docs/exp-EM-budget-transition.md'],
         result='预算算术 5/5 逐位相等；OP_TRANS_COST=25 的判定不改',
         status='完成'),
    dict(id='E-N', name='门的两档实测（交付档与 FLASH 档）',
         goal='静态门在真板上到底会不会按预测拦住？',
         steps=['二分法找边界（DIRECT 与 PID 各一条）', '交付档与 FLASH 档各跑一遍',
                '与源码算术逐条复现'],
         tools=['tools/exp_en_gate.py'],
         docs=['docs/exp-EN-gate-both-builds.md'],
         result='FLASH 档边界 DIRECT 105/106 · PID 60/61 逐条复现；原 TIMEOUT 是烧录假象',
         status='完成'),
    dict(id='E-O', name='决定性翻转实例：纯可加会放行超载',
         goal='拿一个「可加模型 ACK、真实超载」的程序当门失效的证据',
         steps=['构造标量档 ACK 的程序', '部署并测真实 di 与 ov',
                '换含转变项的门重跑同一程序'],
         tools=['tools/exp_eo2_flip.py', 'tools/exp_eo_transition_in_gate.py'],
         docs=['docs/exp-2026-09-18-overload/README.md'],
         result='标量档 ACK 了实跑 30988 cyc（77.5% 拍）的程序且 ov 未触发 => 静态门是承重的',
         status='完成（含撤回）'),
    dict(id='E-P', name='转变代价 k 跟什么有关',
         goal='k 是常数？还是两个 op 的成本差？',
         steps=['六对 op 相邻排列，各测 di', '对 k 做线性拟合与残差检查',
                '与「k 等于两个 op 成本差」的预测比'],
         tools=['tools/exp_ep_op_pair_k.py'],
         docs=['docs/exp-EP-op-pair-k.md'],
         result='k 等于两个 op 的成本差（R²=1.0000，六对零残差）',
         status='完成'),
    dict(id='E-Q', name='「秒」语义端到端验证（声明的秒 vs 跑出来的秒）',
         goal='LPF τ / PID Ki / TIMER PT / SEQ 超时 在三档上是不是声明的那个秒？',
         steps=['判据里不出现被测常量，只测「声明的物理量」', 'div0/1/2 三档各跑一遍',
                '另加 Q0a：tick 速率与上位机墙钟比（与源码无关的独立测量）'],
         tools=['tools/exp_eq_dt_semantics.py'],
         docs=['docs/exp-EQ-dt-semantics.md'],
         result='30 判据 0 FAIL / 2 SKIP；顺带修顺序档第二份 dt 表（div2 超时早 36%）',
         status='完成'),
    dict(id='E-R', name='运动域的「声明时间量」实现了吗（限时与斜坡斜率）',
         goal='把 E-Q 的方法搬到运动域：声明的限时与斜率，跑出来是不是那个数？',
         steps=['差分与最小二乘测真实流逝率', '与声明值比', '修余数丢失后复测'],
         tools=['tools/exp_er_motion_time.py'],
         docs=['docs/exp-ER-motion-time.md'],
         result='抓出限时把拍转 ms 的余数丢掉（部署条件流逝 747/1000 ms/s，且依赖有没有人在读）；修后 11 判据 0 FAIL',
         status='完成（含撤回）'),
    dict(id='E-S', name='「走 N 步」的时长与过冲的界',
         goal='声明的 N/f 时长与实际是否一致？到点自停的过冲界是多少？',
         steps=['用固件拍号直读起止时刻', '对过冲多次取样并报区间（不宣称方向）',
                '注入一次大块读，测阻塞对过冲的影响'],
         tools=['tools/exp_es_step_duration.py'],
         docs=['docs/exp-ES-step-duration.md'],
         result='11 判据 0 FAIL / 0 SKIP；实测时长 0.9916 s 对声明 1.0000 s；并更正三处注释里的假界（注入阻塞后过冲 282 倍）',
         status='完成（含撤回）'),
    dict(id='E-T', name='成本拆解：C_other / C_scan / m_op / k 四项分摊',
         goal='把整段 ISR 拆成可以分开算的四项，并给出每一项的实测值',
         steps=['空程序（n=0）测 di(0) 得 C_other', '19 个原语各测 n=1 得 C_scan(op)',
                '变条数测 m_op、变排列测 k', '与历史值对账（总和与拆分分别比）'],
         tools=['tools/exp_et_cost_decomposition.py'],
         docs=['docs/exp-Cscan-decomposition.md'],
         result='干净 di(0) = 542.4 TB；C_scan(op) = 32.5~54.5 TB；历史值 474 + 100~122 总和对、拆分错',
         status='完成（含撤回）'),
    dict(id='E-U', name='收尾七项：拍长可配 · C_other 拆解 · 门边界 · 判据自我更正',
         goal='把上一轮列出的全部未完成项做掉（含拍长可配这一结构性改动）',
         steps=['拍长一处定义 + 派生 + 断言（-DDCL_TICK_US）', 'C_other 内部拆解',
                '门边界逐条复现', '两处判据自我更正'],
         tools=['tools/exp_eu_wrapup_measure.py'],
         docs=['docs/exp-EU-tick-and-cost.md'],
         result='拍长可配（加 6 处硬耦合）；SEQ 清除语义实测（每拍 735.8 降到 545.4 TB）；交付档指纹链加一环',
         status='完成（含撤回）'),
    dict(id='E-V', name='闸门摊薄口径的隐含依赖（它凭什么成立）',
         goal='闸门按摊薄计价而运行期约束是每拍 —— 这是不是一个真洞？',
         steps=['把 128 条 PID 全写 phase 0 部署，看扫描段会不会爆',
                '读桶表看相位到底怎么分的', '对照 div0-only 程序做恒等性回归'],
         tools=['tools/exp_ev_gate_worst_phase.py'],
         docs=['docs/exp-EV-gate-phase-dependency.md'],
         result='不是洞（实测扫描段最大 262 cyc）：staging 覆写载荷相位 => 摊薄式成立，但依赖未声明；我改的那版会误拒合法程序 64 倍，已回退',
         status='完成（含撤回）'),
    dict(id='E-W', name='三层能真的把路由放到 div1/div2',
         goal='OUTPUT ... PERIOD= 能不能用？三档周期是不是编译器承诺的那个数？',
         steps=['编译期：档位表与固件 DT_* 各自派生后比对',
                '部署三档程序，读 OFF_TICK_STATS 看增量落在哪一档',
                '再加「他档增量恒 0」的负对照'],
         tools=['tools/exp_ew_output_period.py'],
         docs=['docs/exp-EW-output-period.md'],
         result='18 项 0 FAIL / 0 SKIP；在板三档速率 2.0000 / 0.2000 / 0.03125（偏 +0.0 / +0.1 / +0.2%）；并拆掉 PERIOD=10ms 这个假承诺（真实 6.4 ms）',
         status='完成'),
    dict(id='E-X', name='三层表达力：ABS 组合展开 · 数字文法收敛 · 变量阈值',
         goal='引擎没有的原语，能不能用已有原语的组合表达且语义等价？',
         steps=['结构核对（展开后的接线逐字段）', '在板数值等价（按 float 位模式比）',
                '负对照：非法字面量必须干净报错'],
         tools=['tools/exp_ex_composed_blocks.py'],
         docs=['docs/exp-EX-composed-blocks.md'],
         result='14 项 0 FAIL / 0 SKIP；ABS 对 15 个输入逐位等于 abs()（含 -0.0 变 +0.0）；数字文法 11 份收敛成 1 份',
         status='完成'),
    dict(id='E-Y', name='LUT 表到底住在哪（先量，再决定架构）',
         goal='0x23 写得进 LUT 区，那 deploy 与持久化与复位会不会动它？',
         steps=['源码级：deploy 生效路径是否碰 LUT', '在板：写表、部署、复位，逐步读回',
                '查 fill_tables 是否覆盖它'],
         tools=['tools/exp_ey_lut_provenance.py'],
         docs=['docs/exp-EY-lut-provenance.md'],
         result='8 项 0 FAIL / 1 SKIP：表住在无主的易失内存里（deploy 不碰、复位清零、reinit 会覆盖）=> 据此定了归属方案 A',
         status='完成'),
    dict(id='E-Z', name='运行期预算随拍长缩放（关掉耦合 #6）',
         goal='EXEC_BUDGET_CYCLES 是手写的绝对周期数 —— 换拍长后它还成立吗？',
         steps=['用编译器当预言机读编译期常量（探针 TU 加静态断言）',
                '负对照：200 µs 档期望 32000 必须编不过',
                '变异对照：把派生式换回手写数，200 µs 档必须红'],
         tools=['tools/exp_ez_exec_budget_scale.py'],
         docs=['docs/exp-EZ-exec-budget-scale.md'],
         result='8 项 0 FAIL；改为派生加四条断言；交付档指纹不变（逐位无回归）；顺带查出恢复脚本基线过期（P32）',
         status='完成'),
    dict(id='E-FA', name='内存宪法判据总跑（账本 · 归属 · 容量 · 性质）',
         goal='内存的四类判据能不能一起跑、且每条都有变异对照？',
         steps=['账本 C2~C8（含文档容量对账）', '归属 C0/C9/C12 加 C9 变异对照',
                '容量 C11 变异（改上限看编译器是否跟上）', '在板栈水位 F1/F2/F3'],
         tools=['tools/exp_fa_mem_account.py'],
         docs=['docs/PLAN-memmap-constitution.md', 'docs/MEMORY-LAYOUT.md'],
         result='离线 14 项 0 FAIL / 1 SKIP（在板项因板子不在场，已登记 RIG-3）；账本第一次运行就抓出 7 处过期数字（ITCM 已满，见 P33）',
         status='完成（在板项挂台架）'),
    dict(id='E-FB', name='LUT 表随程序走（方案 A：尾部追加 LUTT 段）',
         goal='表能不能随 deploy/程序包走，与路由**同一次 reload**生效？',
         steps=['dclc 产段 → 按固件算法独立重算 FNV 并解包，逐位比对',
                '尾部段组合四态：无段 / 只有 LUT / 只有 DB / LUT+DB',
                '变异对照：magic 错 / seg_len 错 / n_used 越界 / fnv 错 / 表含 NaN ⇒ 必须判 BAD',
                '在板：部署读回、无段不动表、RESET 清 pending、上电从 SD 包装载表回来'],
         tools=['tools/exp_fb_lut_deploy.py'],
         docs=['docs/PLAN-memmap-constitution.md'],
         result='离线 20 项 0 FAIL / 1 SKIP；段格式与固件**同源派生**（magic/seg_len/fnv 都从 src/lut_seg.h 现读）；'
                '在板四项待板子（RIG-3 同族）',
         status='完成（在板项挂台架）'),
    dict(id='TCM-1', name='拍级确定性的决定性问题（换仪器后精度 0.06 cyc）',
         goal='拍级执行时间到底有多确定？用什么仪器才量得准？',
         steps=['换仪器（避开调试单元被 pyocd 关掉的坑）', '重复测同一程序',
                '看分布与最小可分辨差'],
         tools=['tools/h723_tick_determinism.py'],
         docs=['docs/exp-TCM-cycles/README.md'],
         result='换仪器后精度 0.06 cyc —— 决定性问题第一次被回答',
         status='完成'),
    dict(id='MDMA-1', name='MDMA 锁存链为什么没按设计工作',
         goal='设计承诺的「输出由硬件定时器锚定」到底成不成立？',
         steps=['扫 MDMA CTBR.TSEL 全部 16 个取值', '看引脚时刻是否随触发源变',
                '与「CPU 直写」档对照'],
         tools=['tools/h723_latch_sweep.py', 'tools/h723_latch_attrib.py'],
         docs=['docs/exp-2026-09-14-mdma-trigger/README.md',
               'docs/ARCH-TIMELINE-CPU-MDMA.md'],
         result='未达成：TSEL 全扫都照常工作 => 通道在自循环、不消费请求源 => 引脚时刻等于 CPU 最后写影子加 0.12 µs（决策：默认走 CPU 直写档）',
         status='完成（含负结果）'),
]


# ══════════════════════════════════════════════════════════════════════════════
def render():
    L = []
    L.append('# EXP-INDEX —— 实验索引（由 `tools/exp_registry.py` **生成**，勿手改）')
    L.append('')
    L.append('> **唯一源 = `tools/exp_registry.py` 的 `EXPS` 表**（改那里，再跑 '
             '`python tools/exp_registry.py --index`）。')
    L.append('> 为什么这样做：实测 **21 份实验文档 / 3 个实验目录 / 31 个实验工具**，而 '
             '`doc` 与 `tool` **不是一一对应**')
    L.append('> （有的实验有工具无专文、有的一个实验多个工具、有的文档名与实验号不齐）'
             '=> 靠翻目录找不到「做过什么、用什么跑的、结论是什么」。')
    L.append('> ★ 登记表被 `--check` 校验（工具与文档必须存在、索引必须与表一致、状态必须在枚举里）'
             '=> 本页不会像手写索引那样悄悄过期。')
    L.append('')
    L.append('| # | 实验 | 验证目标 | 实验流程 | 实验工具 | 实验结果 | 实验文档 | 状态 |')
    L.append('|---|---|---|---|---|---|---|---|')
    for i, e in enumerate(EXPS, 1):
        tools = '<br>'.join('`%s`' % t.split('/', 1)[1] for t in e['tools'])
        docs = ('<br>'.join('[`%s`](%s)' % (os.path.basename(d), d.replace('docs/', '', 1))
                            for d in e['docs']) if e['docs'] else '**（无专文）**')
        steps = '<br>'.join('%d. %s' % (k, s) for k, s in enumerate(e['steps'], 1))
        L.append('| %d | **%s** %s | %s | %s | %s | %s | %s | %s |'
                 % (i, e['id'], e['name'], e['goal'], steps, tools, e['result'], docs, e['status']))
    L.append('')
    n_tool = len(set(t for e in EXPS for t in e['tools']))
    n_doc = len(set(d for e in EXPS for d in e['docs']))
    L.append('**统计**：登记实验 **%d** 个 · 涉及工具 **%d** 个 · 涉及文档 **%d** 份 · '
             '无专文的实验 **%d** 个。' % (len(EXPS), n_tool, n_doc,
                                           sum(1 for e in EXPS if not e['docs'])))
    L.append('')
    return '\n'.join(L) + '\n'


def check():
    import glob
    bad = []
    for e in EXPS:
        for t in e['tools']:
            if not os.path.exists(os.path.join(R, t)):
                bad.append('%s: 工具不存在 %s' % (e['id'], t))
        for d in e['docs']:
            if not os.path.exists(os.path.join(R, d)):
                bad.append('%s: 文档不存在 %s' % (e['id'], d))
        if e['status'] not in STATUS:
            bad.append('%s: 状态 %s 不在枚举里' % (e['id'], e['status']))
        if not e['goal'] or not e['result'] or not e['steps']:
            bad.append('%s: goal/result/steps 有空项' % e['id'])
    want = render()
    have = io.open(OUT, encoding='utf-8').read() if os.path.exists(OUT) else ''
    if have != want:
        bad.append('docs/EXP-INDEX.md 与登记表不一致 => 跑 --index 重新生成')
    reg = set(t for e in EXPS for t in e['tools'])
    orphans = [os.path.basename(p) for p in sorted(glob.glob(os.path.join(R, 'tools', 'exp_*.py')))
               if 'tools/' + os.path.basename(p) not in reg
               and os.path.basename(p) != 'exp_registry.py']   # 本工具自身不是实验
    print('=== 实验登记表自检（%d 个实验）===' % len(EXPS))
    for b in bad:
        print('  [FAIL] %s' % b)
    if orphans:
        print('  [WARN] 未被任何实验登记的 exp_*.py: %s' % ', '.join(orphans))
    print('\n%d 项检查, %d FAIL' % (len(EXPS), len(bad)))
    return 0 if not bad else 1


def main():
    if '--index' in sys.argv:
        io.open(OUT, 'w', encoding='utf-8', newline='\n').write(render())
        print('已生成 %s（%d 个实验）' % (os.path.relpath(OUT, R), len(EXPS)))
        return 0
    if '--list' in sys.argv:
        for e in EXPS:
            print('%-10s %-44s %s' % (e['id'], e['name'][:44], e['status']))
        return 0
    return check()


if __name__ == '__main__':
    sys.exit(main())
