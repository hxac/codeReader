# 一键跑通 VTR 全流程

## 1. 本讲目标

学完本讲后，你应该能够：

- 用一行命令 `run_vtr_flow.py` 把一段 Verilog 电路完整地映射到目标 FPGA，跑通「综合 → 逻辑优化 → 打包/布局/布线」的端到端链路。
- 说出 VTR 流水线的五个阶段 `odin / parmys / abc / ace / vpr` 各自的职责，以及它们之间的数据如何在网表文件中流转。
- 用 `-start` / `-end` 在任意阶段开始或结束流程，并能解释为什么会得到「阶段未运行」的错误。
- 看懂 `temp/` 目录里那些「乱七八糟」的中间文件（`.blif` / `.net` / `.place` / `.route` / `*.out`）分别由哪个阶段产生。

本讲是入门单元的「验收点」：前几讲你已经知道了 VTR 是什么（u1-l1）、怎么编译（u1-l2）、目录长什么样（u1-l3）。这一讲把这些知识串起来，亲手把一个设计跑通，让你对 VTR 的「真实手感」有一个直观认识。

## 2. 前置知识

在开始前，用最朴素的语言理解几个概念：

- **CAD 流水线（CAD flow）**：把一段「描述了电路行为」的代码，一步步加工成「可以在真实芯片上实现的方案」的流水线。VTR 的流水线由若干「阶段（stage）」串联而成。
- **网表（netlist）**：用文本描述「有哪些器件、它们之间怎么连线」的文件。本讲会反复见到 `.blif`（Berkeley Logic Interchange Format）这种网表格式。流水线每经过一个阶段，网表就被改写一次，越来越接近最终硬件。
- **综合（synthesis）**：把行为级 Verilog（比如 `r_counter <= r_counter + 1`）翻译成「门/LUT/触发器 + 连线」的网表。这是流水线的第一段。
- **技术映射（technology mapping）**：把通用逻辑「贴」到目标 FPGA 实际拥有的硬单元上（比如 6 输入 LUT、硬加法器）。这一步必须知道目标架构，所以会读架构 XML。
- **打包 / 布局 / 布线（pack / place / route）**：VPR 负责的后三段。把原子器件打包成逻辑块、把逻辑块摆到芯片网格的合适位置、再为它们之间的连线找到可用的金属轨道。
- **通道宽度（channel width, W）**：FPGA 布线轨道的数量。布线能否成功取决于 W 是否够大。

> 承接前讲：u1-l3 已经讲过 VTR 是「多工具工作区」——`parmys/`、`abc/`、`vpr/` 是三个独立的可执行工具。本讲要回答的问题是：**谁来按顺序调用它们、又怎么把上一步的产物喂给下一步？** 答案就是 `run_vtr_flow.py` 这个 Python 编排脚本。

## 3. 本讲源码地图

本讲涉及的关键文件，全部围绕「一个设计如何被流水线加工」这一主题：

| 文件 | 作用 |
| --- | --- |
| `vtr_flow/scripts/run_vtr_flow.py` | **入口脚本**。解析命令行参数，构造 `CommandRunner`，然后把活儿整体委派给 `vtr.run()`。 |
| `vtr_flow/scripts/python_libs/vtr/flow.py` | **编排核心**。定义 `VtrStage` 枚举与 `run()` 函数，按阶段顺序调用 `odin/parmys/abc/ace/vpr` 各子模块，用 `next_stage_netlist` 串起数据流。 |
| `vtr_flow/scripts/python_libs/vtr/vpr/vpr.py` | **VPR 阶段封装**。处理「先找最小通道宽度、再在放宽后的宽度下重布线」的两次运行逻辑，以及 `.net/.place/.route` 产物。 |
| `vtr_flow/README.md` | 一句话指引，指向官方在线文档与 `run_vtr_flow.rst`。 |
| `doc/src/vtr/run_vtr_flow.rst` | `run_vtr_flow.py` 的**权威命令行手册**（选项含义、默认值）。 |
| `doc/src/quickstart/index.rst` | **官方快速上手教程**，含可直接复制的 `blink.v` 命令示例与产物清单。 |

记住一条主线：`run_vtr_flow.py` 只是个「前台接待」，真正干活的是 `vtr/flow.py` 里的 `run()`。

## 4. 核心概念与源码讲解

### 4.1 run_vtr_flow.py 命令行参数

#### 4.1.1 概念说明

`run_vtr_flow.py` 是用户面对的命令行入口。它的设计哲学是：**用户只管给「电路 + 架构 + 少量高级选项」，剩下的全由脚本自动安排**。任何它不认识的参数（比如 `--route_chan_width 100`）都不会报错，而是原样转发给底层的 VPR 工具。这样用户既有一个简洁的统一入口，又保留了直接调控 VPR 的能力。

理解它的关键有两点：

1. **两个位置参数**：第一个是电路文件，第二个是架构文件（必须是 `.xml`），顺序固定。
2. **选项分组**：选项被组织成 Power / ABC / Odin / Parmys / Vpr / House Keeping 等若干组，对应流水线的不同阶段或跨阶段配置。

#### 4.1.2 核心流程

脚本的执行流程可以概括为：

```text
用户命令行
   │
   ▼
parse_known_args()        ← 拆成「已识别选项」和「未识别参数」
   │
   ├── 已识别选项  → 组装成 abc_args / odin_args / parmys_args / vpr_args 字典
   ├── 未识别参数  → process_unknown_args() 转成 VPR 参数字典
   │
   ▼
vtr.run(architecture, circuit, start_stage, end_stage, 各阶段字典 ...)
   │  （真正的编排发生在 flow.py，见 4.2）
   ▼
成功 → 打印一行 "EArch/blink   OK (took ...)"
失败 → 打印失败阶段与错误信息
```

最关键的细节是 `parse_known_args`：它不会因为遇到陌生参数而报错，而是把陌生参数放进 `unknown_args`，随后由 `process_unknown_args()` 转成「键值对」字典传给 VPR。这就是「未识别参数转发给 VPR」机制的实现基础。

#### 4.1.3 源码精读

脚本最顶部定义了五个阶段的字符串名，这是 `-start` / `-end` 选项可选值的来源：

[vtr_flow/scripts/run_vtr_flow.py:L24](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/scripts/run_vtr_flow.py#L24) —— `VTR_STAGES` 列表，把字符串 `"odin"/"parmys"/"abc"/"ace"/"vpr"` 列为合法的阶段名。

两个必填的位置参数，顺序固定为「电路在前、架构在后」：

[vtr_flow/scripts/run_vtr_flow.py:L109-L110](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/scripts/run_vtr_flow.py#L109-L110) —— 定义 `circuit_file` 与 `architecture_file` 两个位置参数。

控制流程起止的两个选项，**默认从 `parmys` 开始、到 `vpr` 结束**（即默认不跑 ODIN，也不跑功耗阶段 ACE）：

[vtr_flow/scripts/run_vtr_flow.py:L111-L118](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/scripts/run_vtr_flow.py#L111-L118) —— `-start` 选项，默认值 `vtr.VtrStage.PARMYS`，并把字符串映射成枚举值。

[vtr_flow/scripts/run_vtr_flow.py:L133-L140](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/scripts/run_vtr_flow.py#L133-L140) —— `-end` 选项，默认值 `vtr.VtrStage.VPR`。

工作目录选项，默认在「当前工作目录下的 `temp/`」里跑：

[vtr_flow/scripts/run_vtr_flow.py:L187-L191](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/scripts/run_vtr_flow.py#L187-L191) —— `-temp_dir` 默认为 `os.getcwd() + "/temp"`。

主函数把所有组装好的字典打包传给 `vtr.run()`，这是前台接待向后台核心的「整体移交」：

[vtr_flow/scripts/run_vtr_flow.py:L573-L599](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/scripts/run_vtr_flow.py#L573-L599) —— `vtr.run(...)` 调用，传入架构、电路、起止阶段、各阶段参数字典、以及是否保留中间/结果文件。

官方手册对基本用法和「未识别参数转发给 VPR」的说明，权威且简洁：

[doc/src/vtr/run_vtr_flow.rst:L17-L32](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/doc/src/vtr/run_vtr_flow.rst#L17-L32) —— 最小用法是 `run_vtr_flow.py <circuit_file> <architecture_file>`，并警告 `temp/` 目录里的文件可能被删除。

[doc/src/vtr/run_vtr_flow.rst:L51-L60](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/doc/src/vtr/run_vtr_flow.rst#L51-L60) —— 说明 `<vpr_options>`（未被脚本识别的参数）会被转发给 VPR，例如 `--pack` `--place`。

#### 4.1.4 代码实践

**实践目标**：在不真正运行的情况下，读懂 `run_vtr_flow.py` 的命令行界面，回答几个具体问题。

**操作步骤**：

1. 打开 `vtr_flow/scripts/run_vtr_flow.py`，找到 `vtr_command_argparser` 函数（L52 起）。
2. 在其中定位 `-temp_dir`、`-start`、`-end`、`-cmos_tech` 四个选项。
3. 对照 `doc/src/vtr/run_vtr_flow.rst` 的「Detailed Command-line Options」一节核对它们的默认值。

**需要观察的现象**：

- `-start` 的默认值是 `parmys` 而不是 `odin`，说明默认综合前端是 Parmys。
- `-cmos_tech`（功耗工艺文件）默认是 `None`，说明功耗分析默认关闭（这也意味着 ACE 阶段默认不运行，见 4.2）。
- `-temp_dir` 默认是相对路径 `./temp`，所以**你在哪个目录运行脚本，`temp/` 就建在哪儿**。

**预期结果**：你能口头说出「默认情况下脚本从 parmys 阶段开始、到 vpr 阶段结束、在当前目录的 temp/ 下工作、不开启功耗」。无需运行即可确认（这些都是源码里写死的默认值）。

#### 4.1.5 小练习与答案

**练习 1**：如果用户运行 `run_vtr_flow.py a.v arch.xml --route_chan_width 100`，`--route_chan_width 100` 这段会被 `run_vtr_flow.py` 自己消费掉吗？

> **答案**：不会。`--route_chan_width` 不是脚本注册的选项，`parse_known_args` 把它放进 `unknown_args`，再由 `process_unknown_args()` 转成 VPR 的 `route_chan_width=100` 参数转发给 VPR。

**练习 2**：`-temp_dir` 选项的默认值是什么？如果你希望把所有中间产物放到 `/tmp/myrun`，应该怎么写？

> **答案**：默认值是 `os.getcwd() + "/temp"`（即当前工作目录下的 `temp/`）。指定自定义目录用 `-temp_dir /tmp/myrun`。

---

### 4.2 VTR_STAGES 阶段流转

#### 4.2.1 概念说明

五个阶段 `odin / parmys / abc / ace / vpr` 构成了 VTR 的完整流水线。需要先澄清几个**容易误解**的点：

1. **ODIN 与 PARMYS 是「二选一」的**，不是连续两段。它们都是综合前端（ODIN II 是旧版、Parmys 是默认）。`flow.py` 用 `if ... elif ...` 保证最多只运行其中一个。
2. **ACE（功耗活动性估计）默认不运行**，只有当用户通过 `-cmos_tech` 提供了功耗工艺文件时才会触发。
3. 所以「默认流水线」其实是 **Parmys → ABC → VPR** 三段，这也是 u1-l1 提到的 PARMYS→ABC→VPR 三段式流程的来源。
4. 阶段是有**顺序编号**的：`ODIN=1, PARMYS=2, ABC=3, ACE=4, VPR=5`。`-start` / `-end` 实际上是在这个编号区间里「切片」：只有编号落在 `[start, end]` 闭区间里的阶段才会运行。

#### 4.2.2 核心流程

`flow.py` 的 `run()` 函数用一个统一的判定函数 `should_run_stage(stage, start, end)` 决定每个阶段要不要跑——它就是判断 `start <= stage <= end`。

```text
        start=parmys(2)            end=vpr(5)
odin(1)  parmys(2)  abc(3)  ace(4)  vpr(5)
   ✗         ✓         ✓      ✗*      ✓
   (*ace 还需 power_tech_file 才真正运行)
```

数据在阶段间通过一个变量 `next_stage_netlist` 像接力棒一样传递：

```text
circuit_copy (= 用户输入电路)
   │  若运行 ODIN/PARMYS → 产出 *.odin.blif 或 *.parmys.blif
   ▼
next_stage_netlist ──►  ABC  ──►  *.abc.blif
   ▼
(若启用功耗) ──► ACE ──► *.ace.blif + *.act
   ▼
复制为 *.pre-vpr.blif ──►  VPR  ──►  *.net / *.place / *.route
```

每一阶段结束后，都会把自己的输出网表赋给 `next_stage_netlist`，下一阶段就拿它当输入。这就是流水线「链式」的本质。

一个**非常重要的设计取舍**体现在 VPR 阶段对通道宽度的处理上（见 4.2.3 源码）：默认（不指定 `--route_chan_width`）时，VPR 会被调用**两次**——第一次用二分搜索找到能布通的最小通道宽度 `min_w`，第二次在放宽后的宽度 `min_w × relax_w_factor`（默认 1.3）下重新布线并做时序分析，以获得更好的关键路径延迟。如果你显式给了 `--route_chan_width`，就只跑一次。

#### 4.2.3 源码精读

阶段枚举与顺序编号定义在此，`__le__` / `__ge__` 让枚举之间可以用 `<=` 比较：

[vtr_flow/scripts/python_libs/vtr/flow.py:L12-L31](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/scripts/python_libs/vtr/flow.py#L12-L31) —— `VtrStage` 枚举：`ODIN=1, PARMYS=2, ABC=3, ACE=4, VPR=5`，并定义了比较运算符。

阶段切片判定的核心函数，逻辑就一句话——「阶段是否落在闭区间内」：

[vtr_flow/scripts/python_libs/vtr/flow.py:L421-L427](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/scripts/python_libs/vtr/flow.py#L421-L427) —— `should_run_stage`：`flow_start_stage <= stage <= flow_end_stage` 时返回 `True`。

ODIN 与 PARMYS 的「二选一」结构（注意是 `if ... elif ...`）：

[vtr_flow/scripts/python_libs/vtr/flow.py:L197-L235](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/scripts/python_libs/vtr/flow.py#L197-L235) —— ODIN 阶段（且仅当输入不是 `.blif` 时）与 PARMYS 阶段互斥运行；运行后把产物赋给 `next_stage_netlist`。

ABC 阶段无条件地（只要落在区间内）对当前网表做逻辑优化与技术映射：

[vtr_flow/scripts/python_libs/vtr/flow.py:L240-L253](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/scripts/python_libs/vtr/flow.py#L240-L253) —— ABC 阶段，读入 `next_stage_netlist`，写出 `post_abc_netlist`，再更新接力棒。

ACE 阶段被 `if power_tech_file:` 守卫，所以默认不运行：

[vtr_flow/scripts/python_libs/vtr/flow.py:L258-L283](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/scripts/python_libs/vtr/flow.py#L258-L283) —— 仅当提供了功耗工艺文件时才运行 ACE，并顺带在 `vpr_args` 里打开 VPR 的 `power` 选项。

VPR 阶段是最后一段，它根据是否指定固定通道宽度选择「单次运行」或「先找最小 W 再重布线」：

[vtr_flow/scripts/python_libs/vtr/flow.py:L288-L318](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/scripts/python_libs/vtr/flow.py#L288-L318) —— VPR 阶段入口：先把当前网表复制成 `pre-vpr.blif`，判断 `route_fixed_w`，固定宽度时调用 `vtr.vpr.run(...)` 单次运行。

[vtr_flow/scripts/python_libs/vtr/flow.py:L346-L356](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/scripts/python_libs/vtr/flow.py#L346-L356) —— 未指定固定宽度时调用 `vtr.vpr.run_relax_w(...)`，即「两次运行」路径。

「两次运行」路径的具体实现，第一遍找 `min_w`、第二遍在 `relax_w(min_w, 1.3)` 下重布线并分析：

[vtr_flow/scripts/python_libs/vtr/vpr/vpr.py:L115-L123](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/scripts/python_libs/vtr/vpr/vpr.py#L115-L123) —— 用 `determine_min_w` 读出最小通道宽度，`relax_w` 放大它，然后设置 `route=True / analysis=True / route_chan_width=relaxed_w` 做第二次运行。

#### 4.2.4 代码实践

**实践目标**：通过「只跑某一段」来验证阶段切片机制，亲眼看到不同 `-start/-end` 组合下的产物差异。

**操作步骤**（前提：已按 u1-l2 完成编译，且激活了 Python 虚拟环境 `source .venv/bin/activate`）：

1. 准备工作目录并进入：
   ```bash
   export VTR_ROOT=<你的 VTR 源码根目录>
   mkdir -p ~/vtr_work/u1l4 && cd ~/vtr_work/u1l4
   ```
2. **只跑综合**（parmys→parmys）：
   ```bash
   $VTR_ROOT/vtr_flow/scripts/run_vtr_flow.py \
       $VTR_ROOT/doc/src/quickstart/blink.v \
       $VTR_ROOT/vtr_flow/arch/timing/EArch.xml \
       -start parmys -end parmys -temp_dir run_parmys_only
   ```
3. 观察输出与 `run_parmys_only/` 里的文件。
4. 再跑一次**完整流程**（默认 parmys→vpr），指定固定通道宽度，并保留所有中间文件（去掉删除开关）：
   ```bash
   $VTR_ROOT/vtr_flow/scripts/run_vtr_flow.py \
       $VTR_ROOT/doc/src/quickstart/blink.v \
       $VTR_ROOT/vtr_flow/arch/timing/EArch.xml \
       --route_chan_width 100 -temp_dir run_full
   ```

**需要观察的现象**：

- 第 2 步因为 `-end parmys`，ABC 与 VPR 都不运行；`run_parmys_only/` 里**不会**出现 `.net/.place/.route`，只有 `blink.parmys.blif` 等综合产物和日志。
- 第 2 步终端打印类似 `EArch/blink   OK (took 0.16 seconds, ...)`，耗时很短。
- 第 4 步因为固定了通道宽度，VPR 只跑一次；`run_full/` 里出现完整的 `.net/.place/.route`。

**预期结果**：你能用一句话解释「为什么只跑 parmys 时没有 `.route` 文件」——因为 VPR 阶段被 `-end parmys` 排除在区间外了（`should_run_stage(VPR=5, start=2, end=2)` 为 `False`）。

> 如果暂时无法编译/运行，这就是一个**源码阅读型实践**：直接对照 [flow.py:L288-L318](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/scripts/python_libs/vtr/flow.py#L288-L318) 推理「`-end parmys` 时 VPR 段代码根本不会进入」，结论一致。标记：**待本地验证**实际产物文件名。

#### 4.2.5 小练习与答案

**练习 1**：为什么默认流水线不运行 ODIN 阶段？

> **答案**：因为 `-start` 的默认值是 `PARMYS(=2)`，而 `ODIN(=1)` 小于它。`should_run_stage(ODIN, start=2, end=5)` 判断 `2 <= 1` 为假，所以 ODIN 被跳过；又因为 ODIN 与 PARMYS 是 `if/elif` 互斥，于是 PARMYS 运行。

**练习 2**：用户运行 `-start abc -end abc` 却没有 `*.parmys.blif` 或 `*.abc.blif` 作为前置产物，会发生什么？

> **答案**：`should_run_stage` 只决定阶段是否运行，**不保证前置产物存在**。ABC 会去读 `next_stage_netlist`（此时是用户原始输入电路的副本），若它不是 ABC 能读的 BLIF 网表，ABC 阶段会失败。这正是 `-start` 文档里那句「assumes required results have already been generated」的含义——从中间阶段开始，必须保证上游产物已存在。

**练习 3**：默认（不给 `--route_chan_width`）时 VPR 会被调用几次？为什么？

> **答案**：两次。第一次二分搜索找最小通道宽度 `min_w`，第二次在 `relax_w(min_w, 1.3)` 下重新布线并做时序分析，以获得更优的关键路径延迟。这条路径由 `run_relax_w` 实现。

---

### 4.3 中间产物文件

#### 4.3.1 概念说明

流水线每跑一个阶段，都会在 `temp/` 目录里留下两类文件：

1. **网表产物**：以 `<电路名>.<阶段>.blif` 命名，记录该阶段输出电路。它们就是 `next_stage_netlist` 在磁盘上的「快照」。
2. **日志产物**：以 `<阶段>.out` 命名，记录对应工具的 stdout/stderr。比如 `parmys.out`、`abc0.out`、`vpr.out`。

VPR 阶段还会额外产出三类「实现文件」：`.net`（打包后的网表）、`.place`（布局结果）、`.route`（布线结果）。这三个文件加上输入的 `.blif` 和架构 `.xml`，**完整定义了一个电路实现**——这也是为什么后续可以用 `--analysis` 重新加载并可视化它们。

#### 4.3.2 核心流程

文件名是在 `flow.py` 开头用「电路名 + 阶段后缀 + 扩展名」的模式统一拼接的：

```text
circuit_file.stem = "blink"
   ├── blink.odin.blif     ← ODIN 产物
   ├── blink.parmys.blif   ← PARMYS 产物（变量名是 post_yosys_netlist）
   ├── blink.abc.blif      ← ABC 产物
   ├── blink.ace.blif      ← ACE 产物（仅启用功耗时）
   ├── blink.act           ← ACE 活动性文件
   ├── blink.pre-vpr.blif  ← 喂给 VPR 的网表（通常是 abc.blif 的副本）
   └── blink.net/.place/.route  ← VPR 产物（实现文件）
```

需要注意几个**命名上的「坑」**：

- PARMYS 的产物文件变量名叫 `post_yosys_netlist`（因为 Parmys 基于 Yosys），但文件后缀是 `.parmys`，所以实际文件是 `blink.parmys.blif`。
- 默认流程下，`blink.pre-vpr.blif` 与 `blink.abc.blif` 内容通常一致——前者只是把后者复制一份重命名。
- `.blif` 还是 `.eblif`，取决于输入电路的扩展名：若输入是 `.eblif` 则全程用 `.eblif`，否则用 `.blif`。

关于「文件去留」：默认情况下脚本会**删除中间文件**（`-delete_intermediate_files` 默认为真，对应 `keep_intermediate_files` 默认为真——注意这里参数命名是「取反」的，读源码时要小心）。结果文件（`.net/.place/.route`）默认保留。

#### 4.3.3 源码精读

网表扩展名的判定——根据输入决定全程用 `.blif` 还是 `.eblif`：

[vtr_flow/scripts/python_libs/vtr/flow.py:L157](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/scripts/python_libs/vtr/flow.py#L157) —— `netlist_ext` 的三目判定。

所有产物文件名的集中定义处，是理解「为什么文件叫这个名字」的源头：

[vtr_flow/scripts/python_libs/vtr/flow.py:L160-L165](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/scripts/python_libs/vtr/flow.py#L160-L165) —— 定义 `post_odin_netlist`/`post_yosys_netlist`/`post_abc_netlist`/`post_ace_netlist`/`post_ace_activity_file`/`pre_vpr_netlist` 六个路径。

接力棒的初始化——流水线开始前，「下一个阶段的输入」就是用户原始电路的副本：

[vtr_flow/scripts/python_libs/vtr/flow.py:L192](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/scripts/python_libs/vtr/flow.py#L192) —— `next_stage_netlist = circuit_copy`。

VPR 段入口把当前网表复制成 `pre-vpr.blif`，这正是 VPR 实际读入的文件：

[vtr_flow/scripts/python_libs/vtr/flow.py:L288-L291](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/scripts/python_libs/vtr/flow.py#L288-L291) —— `shutil.copyfile(next_stage_netlist, pre_vpr_netlist)` 后再调用 VPR。

VPR 子模块在拼命令行时，会用 `--circuit_file` 显式指定输入网表，并用 `circuit_name` 决定输出 `.net/.place/.route` 的前缀名：

[vtr_flow/scripts/python_libs/vtr/vpr/vpr.py:L210-L219](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/scripts/python_libs/vtr/vpr/vpr.py#L210-L219) —— 构造 `vpr <arch> <circuit_name> --circuit_file <netlist>` 命令。

官方快速上手教程给出了**真实运行后**的产物清单，可直接作为参照：

[doc/src/quickstart/index.rst:L166-L181](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/doc/src/quickstart/index.rst#L166-L181) —— 列出 `blink.parmys.blif`、`blink.abc.blif`、`blink.pre-vpr.blif` 以及 `blink.net`、`blink.place`、`blink.route` 等产物的含义。

[doc/src/quickstart/index.rst:L143-L156](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/doc/src/quickstart/index.rst#L143-L156) —— 成功输出行 `EArch/blink   OK (took ...)`，以及 `*.out` 日志文件清单。

#### 4.3.4 代码实践

**实践目标**：跑通 `blink.v` 全流程，亲手清点 `temp/` 里的产物文件，并把每个文件对应到产生它的阶段。

**操作步骤**：

1. 编译 VTR（u1-l2）并激活虚拟环境后，执行官方推荐的命令（固定通道宽度 100）：
   ```bash
   export VTR_ROOT=<你的 VTR 源码根目录>
   mkdir -p ~/vtr_work/u1l4/blink_run && cd ~/vtr_work/u1l4/blink_run
   $VTR_ROOT/vtr_flow/scripts/run_vtr_flow.py \
       $VTR_ROOT/doc/src/quickstart/blink.v \
       $VTR_ROOT/vtr_flow/arch/timing/EArch.xml \
       --route_chan_width 100
   ```
2. 列出网表产物：`ls temp/*.blif`
3. 列出日志产物：`ls temp/*.out`
4. 列出实现产物：`ls temp/*.net temp/*.place temp/*.route`
5. 打开 `blink.v`，确认它是一个 5 位计数器（[blink.v:L10](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/doc/src/quickstart/blink.v#L10) 的 `reg[4:0] r_counter`），每个时钟自增（[blink.v:L15](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/doc/src/quickstart/blink.v#L15)）。

**需要观察的现象**：

- `temp/*.blif` 里能看到 `blink.parmys.blif`、`blink.abc.blif`、`blink.pre-vpr.blif`（默认流程下，部分中间 blif 可能因 `-delete_intermediate_files` 被清理；若想全留，可加 `-delete_intermediate_files` 的反向开关保留）。
- `temp/*.out` 里有 `parmys.out`、`abc0.out`、`vpr.out` 等日志。
- `temp/` 里有 `blink.net`、`blink.place`、`blink.route` 三个实现文件。
- 终端打印一行类似 `EArch/blink   OK (took 0.26 seconds, overall memory peak 63.71 MiB consumed by vpr run)`。

**预期结果**：你能填出下面这张对应表——

| 文件 | 产生它的阶段 |
| --- | --- |
| `blink.parmys.blif` | PARMYS（综合） |
| `blink.abc.blif` | ABC（逻辑优化 + 技术映射） |
| `blink.pre-vpr.blif` | 进入 VPR 前的复制（通常 = abc.blif） |
| `parmys.out` / `abc0.out` / `vpr.out` | 对应工具的日志 |
| `blink.net` / `blink.place` / `blink.route` | VPR（打包 / 布局 / 布线） |

> 若环境暂未就绪，可改为**源码阅读型实践**：对照 [flow.py:L160-L165](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/scripts/python_libs/vtr/flow.py#L160-L165) 与官方 [quickstart/index.rst:L166-L181](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/doc/src/quickstart/index.rst#L166-L181) 推理出上表，结论一致。标记：**待本地验证**实际文件名（不同版本可能略有差异）。

#### 4.3.5 小练习与答案

**练习 1**：变量 `post_yosys_netlist` 对应的实际文件名是什么？为什么变量名和文件名「对不上」？

> **答案**：实际文件是 `<电路名>.parmys.blif`（如 `blink.parmys.blif`）。变量名带 `yosys` 是因为 Parmys 基于 Yosys 实现，而文件后缀用 `parmys` 是为了体现这是 Parmys 阶段的产物。读源码时不要被变量名误导。

**练习 2**：`blink.pre-vpr.blif` 和 `blink.abc.blif` 在默认流程下内容通常一样，那为什么还要多复制一份？

> **答案**：为了「解耦」。`next_stage_netlist` 此刻指向的是 ABC 的输出；把它复制成稳定的 `pre-vpr.blif` 后，VPR 永远读这个固定名字的文件，便于日志、重跑和可视化（`--analysis --circuit_file .../blink.pre-vpr.blif`）时引用。

**练习 3**：为什么有时候 `temp/` 里看不到 `blink.odin.blif`？

> **答案**：默认 `-start parmys`，ODIN 阶段不运行，自然没有 `blink.odin.blif`；只有把 `-start` 设为 `odin`（且输入不是 `.blif`）时才会产生它。

---

## 5. 综合实践

**任务**：以「`blink.v` + `EArch.xml`」为对象，完成一次「分段运行 + 全流程对比 + 产物溯源」的小调查，把本讲的三个模块串起来。

**步骤**：

1. **环境**：确认已 `source $VTR_ROOT/.venv/bin/activate`，且 `make` 已成功（u1-l2）。
2. **全流程运行（固定宽度）**：
   ```bash
   mkdir -p ~/vtr_work/u1l4/final && cd ~/vtr_work/u1l4/final
   $VTR_ROOT/vtr_flow/scripts/run_vtr_flow.py \
       $VTR_ROOT/doc/src/quickstart/blink.v \
       $VTR_ROOT/vtr_flow/arch/timing/EArch.xml \
       --route_chan_width 100 -temp_dir temp
   ```
3. **分段验证**：在另一个目录用 `-start parmys -end parmys` 只跑综合，对比两者的产物差异。
4. **产物溯源**：在 `temp/` 中，为下列每个文件写一行说明，指出「它由哪个阶段产生、内容代表什么」：
   - `blink.parmys.blif`
   - `blink.abc.blif`
   - `blink.pre-vpr.blif`
   - `vpr.out`
   - `blink.route`
5. **读日志回答**：打开 `temp/vpr.out`，找到 VPR 报告的「最小通道宽度」或「关键路径周期（critical path delay）」相关行，记录数值。（若用了 `--route_chan_width 100`，则是在固定宽度 100 下的结果。）
6. **（进阶）观察「两次运行」**：去掉 `--route_chan_width 100` 重跑一次，注意终端耗时变长，并在日志里找到「第一次找最小 W、第二次在放宽 W 下重布线」的痕迹（参考 [vpr.py:L115-L123](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/scripts/python_libs/vtr/vpr/vpr.py#L115-L123)）。

**验收标准**：

- 能说清「默认流水线 = Parmys → ABC → VPR」三段，以及为什么 ODIN/ACE 默认不跑。
- 能把 `temp/` 里至少 5 个文件正确归因到产生它的阶段。
- 能解释「`-start abc` 为什么需要前置产物已存在」。

> 提示：这一步需要真实运行环境。若暂时不可用，请把步骤 4–6 改为基于源码与官方文档的「推理 + 待本地验证」版本，重点是把「文件 ↔ 阶段」的对应关系讲清楚。

## 6. 本讲小结

- `run_vtr_flow.py` 是用户入口：两个位置参数（电路、架构）+ 选项；**未识别参数自动转发给 VPR**，这是它既能简化使用、又能保留 VPR 全部能力的核心机制。
- 五个阶段 `odin(1)/parmys(2)/abc(3)/ace(4)/vpr(5)` 是有编号的；`-start/-end` 在编号闭区间里「切片」，由 `should_run_stage`（`start <= stage <= end`）决定每个阶段是否运行。
- ODIN 与 PARMYS 是**互斥二选一**的综合前端，默认走 PARMYS；ACE 默认不运行，只有提供功耗工艺文件才触发。所以**默认流水线是 Parmys → ABC → VPR**。
- 数据靠 `next_stage_netlist` 这根「接力棒」在阶段间传递，每个阶段产出 `<电路名>.<阶段>.blif`；VPR 阶段额外产出 `.net/.place/.route` 三个实现文件，它们与 `.blif`、架构 `.xml` 一起完整定义了一个电路实现。
- 默认（不给 `--route_chan_width`）时 VPR 会跑**两次**：先找最小通道宽度，再在放宽 1.3 倍后重布线做时序分析；给固定宽度则只跑一次。
- 所有产物默认落在当前目录的 `temp/` 下（`-temp_dir` 可改），且中间文件默认会被清理——调试时记得改保留开关或自定义目录。

## 7. 下一步学习建议

本讲让你「跑通了」VTR，但只是把它当成黑盒。接下来应该打开黑盒：

- **下一讲 u1-l5（VPR 命令行与参数体系）**：`run_vtr_flow.py` 转发给 VPR 的那些参数（如 `--route_chan_width`、`--pack`、`--place`）到底在 VPR 内部如何被解析。这是从「会用」到「能调」的关键一步。
- **后续第 2 单元（FPGA 架构描述与解析）**：本讲里 `EArch.xml` 只是个输入文件；下一单元会讲 VTR 如何把它解析成 `libarchfpga` 的数据结构，这正是 u1-l1 强调的「架构驱动」理念落地处。
- **第 3 单元（核心数据结构与 vpr_api 编排）**：本讲中 VPR 阶段的 `.net/.place/.route` 产物，其内部对应的是 `AtomNetlist → ClusteredNetlist` 等数据结构与 `vpr_api.cpp` 的阶段编排——建议在跑通流程后回去对照源码，理解「文件背后」发生了什么。

**推荐继续阅读的源码**：

- `vtr_flow/scripts/python_libs/vtr/flow.py` 的 `run()` 全文（本讲只精读了关键段），通读能完整建立「阶段编排」的全景。
- `vtr_flow/scripts/python_libs/vtr/vpr/vpr.py` 的 `run()`，理解 VPR 命令行是怎么逐个 `--option` 拼出来的，为 u1-l5 做铺垫。
- 官方 `doc/src/quickstart/index.rst` 的「Manually Running the VTR Flow」一节，体会脚本帮你省掉了哪些手动步骤（手动调 ABC、手动恢复时钟等）。
