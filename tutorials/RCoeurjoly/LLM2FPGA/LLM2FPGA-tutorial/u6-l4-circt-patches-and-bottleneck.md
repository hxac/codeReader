# CIRCT 补丁栈与瓶颈结论

## 1. 本讲目标

本讲是单元 6（TinyStories 实战与瓶颈分析）的收束篇。前面几讲我们一直把 CIRCT 当作「上游给什么用什么」的黑盒：u3-l2 把 CF 降成 Handshake、u3-l3 把 Handshake 降成 HW、u3-l4 把 HW 导出成 SystemVerilog。但**真实跑 TinyStories-1M 时，上游 CIRCT 根本跑不通**——必须打一组补丁才能把这条降级链走完。

学完本讲你应该能：

1. 理解「上游工具不够用时，用补丁栈定制上游」这一工程方法，以及它在本项目里的具体落地方式。
2. 看懂 `patches/circt-task3-rfp/` 这 11 个补丁的命名约定，并能按文件名归纳出它们要解决的三大类降级问题。
3. 精读浮点 extern 补丁（0015）与 memref 扁平化补丁（0003），理解每类补丁在改 CIRCT 的哪一段降级逻辑。
4. 牢记 3e 报告的核心结论：**设计比目标 FPGA 大约 141 倍、nextpnr-xilinx 因 OOM 跑不到布局布线**，并据此说清楚为什么下一步是 Task 6（资源最小化）而不是 Task 4（硬件集成）。

## 2. 前置知识

本讲假设你已经读过：

- **u3-l2 / u3-l3**：CF → Handshake → HW 的弹性数据流降级链，以及「Handshake 是本项目最大的资源负担」这一结论。
- **u3-l4**：HW → SystemVerilog 导出里的「禁止裸 extern」安全门，以及浮点 extern 的产生背景。
- **u6-l3**：浮点算子被 CIRCT 补丁 0015 降级为 `hw.module.extern` 后，由 `circt_fp_primitives.sv` 用 Q16.16 定点近似提供可综合实现。
- **u5-l3**：资源利用报告如何从 Yosys mapped JSON 估算 LUT/FF/DSP/BRAM 用量，并与目标芯片容量对比。

需要先建立的几个概念：

- **补丁（patch）**：对上游源码的一段差异（diff）。一个补丁只改一个明确的点；一组补丁按顺序叠加，称为**补丁栈（patch stack）**。
- **fork**：把上游仓库复制一份到自己名下，在副本上自由修改。本项目的 CIRCT fork 分支叫 `task3`。
- **`git format-patch` / `am`**：Git 把每次提交导出成一个 `.patch` 文件（带作者、日期、Subject），别人可以 `git am` 重新应用。本仓库里的 `.patch` 文件就是这么导出的留档。
- **上游（upstream）**：工具的官方主线仓库。本项目用上游 CIRCT 的某个 commit 作为基线，再把补丁叠上去。
- **OOM（Out Of Memory）**：进程申请的内存超过系统可用量，被操作系统强制杀死。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [patches/circt-task3-rfp/](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/patches/circt-task3-rfp/) | Task 3 的 CIRCT 补丁栈目录，11 个 `.patch` 文件，是 fork 与上游差异的留档。 |
| [patches/circt-task3-rfp/0015-lower-float-ops-as-externs-in-handshake-to-hw.patch](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/patches/circt-task3-rfp/0015-lower-float-ops-as-externs-in-handshake-to-hw.patch) | 把 18 类浮点算子在 Handshake→HW 步骤降级为 `hw.module.extern` 黑盒——浮点 extern 的来源。 |
| [patches/circt-task3-rfp/0003-flatten-memref-shape-ops.patch](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/patches/circt-task3-rfp/0003-flatten-memref-shape-ops.patch) | 让 FlattenMemRefs 变换能正确处理 `memref.expand_shape` / `collapse_shape`。 |
| [deliverables/3e-tiny-stories-1m-resource-report.md](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/deliverables/3e-tiny-stories-1m-resource-report.md) | Task 3e 的综合资源报告 + 瓶颈报告，给出 141 倍超配与 nextpnr OOM 的最终结论。 |
| [deliverables/1c-selected_route.md](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/deliverables/1c-selected_route.md) | Task 1c：选定路线与预设风险，含「最小 LLM 装不下」这条被 3e 证实的风险。 |
| [flake.nix](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix) | 用 `circt-src` 指向 `RCoeurjoly/circt` 的 `task3` 分支，并在注释里说明补丁栈的定位。 |

## 4. 核心概念与源码讲解

### 4.1 补丁栈定位与命名约定

#### 4.1.1 概念说明

理想的编译器流水线应该是：上游工具「开箱即用」，把任何合法输入一路降到底。现实是：上游 CIRCT 的 Handshake 数据流降级（`lower-cf-to-handshake`、`lower-handshake-to-hw`）主要面向**小规模、学术性的数据流图**，从未被喂过一个真实的 LLM。一旦把 TinyStories-1M 这种带巨大 memref、CFG 内嵌内存访问、大量浮点算子的真实图喂进去，降级就会在各种各样的边角情况上崩溃。

面对「上游不够用」，工程上有两条路：

1. **改自己的代码绕开**：在降级链每站之间插自己的转换，把上游处理不了的 IR 先改写成它能吃的样子。代价是降级链变长、可维护性差。
2. **改上游源码**：直接修 CIRCT 的降级逻辑，让它能正确处理这些情况。代价是要维护一个上游 fork，并跟踪上游更新。

本项目选了**第 2 条**，原因是这些问题大多出在 CIRCT 降级逻辑本身的缺陷（如「该处理的算子没处理」「该合法化的类型没合法化」），绕不开，只能改源头。改完之后，用 `git format-patch` 把每次修改导出成 `.patch` 文件，按顺序编号放进 `patches/circt-task3-rfp/`，这就是**补丁栈**。

> 关键事实：构建实际编译的是 `circt-src` 指向的 `RCoeurjoly/circt` 的 `task3` 分支——这个 fork 已经把所有补丁合进了它的提交历史。而 `patches/circt-task3-rfp/` 里的 `.patch` 文件是这组改动的**留档**，让审查者不必克隆 fork 历史就能读到与上游的精确差异。flake.nix 顶部的注释把这一点说得很直白（见 4.1.3）。

#### 4.1.2 核心流程

补丁栈的工作流程可以画成：

```
上游 CIRCT (upstream commit)
        │  apply 0003, 0004, 0005, 0008, 0009,
        │         0010, 0011, 0012, 0013, 0014, 0015   (已合入 task3 分支)
        ▼
RCoeurjoly/circt  ref=task3   ← flake.nix 的 circt-src 指向这里
        │  nix 编译
        ▼
circt-opt / circt 二进制（能吃下 TinyStories-1M 的 IR）
        │  跑降级链 u3-l2 → u3-l3 → u3-l4
        ▼
main.sv（含浮点 extern）+ sources.f
```

补丁的**命名约定**是 `<编号>-<动宾短语>.patch`，编号决定应用顺序（数字小者先应用）。编号从 `0003` 开始、到 `0015` 结束，中间不连续（缺 0001/0002/0006/0007），说明作者在迭代过程中删掉或合并了若干早期补丁。带 `Subject:` 头的补丁还能看到 `[PATCH X/N]` 标记，其中 N 取过 7、8、9 三个值——说明这组补丁至少经历了三轮重排（先 7 个、后 8 个、最后 9 个），现存的 11 个文件是这几次迭代的合并留档。

按文件名的动宾短语，可以把 11 个补丁归纳成**三大类问题**（这是本讲代码实践要你做的归纳）：

| 类别 | 补丁 | 解决的问题 |
|---|---|---|
| **A. memref 扁平化** | 0003, 0009 | 让 FlattenMemRefs 变换能处理形状变换算子与 dense resource 全局量。 |
| **B. Handshake 降级正确性** | 0004, 0005, 0008, 0010, 0011, 0012, 0013 | 让 CF→Handshake、Handshake→HW 能吃下真实 LLM IR（CFG 内嵌 memref、额外前端算子、func 降级时机与合法性等）。 |
| **C. 浮点算子外部化** | 0015 | 把 18 类浮点算子降级为 `hw.module.extern` 黑盒。 |
| （支撑性） | 0014 | 只改一个测试文件，修正常量顺序导致的 CHECK 行错位。 |

#### 4.1.3 源码精读

先看 fork 是怎么接进构建的。`circt-src` 指向作者自己的 CIRCT fork 的 `task3` 分支：

[flake.nix:L15-L21](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L15-L21) —— 这段把 `circt-src` 钉到 `github:RCoeurjoly/circt` 的 `task3` 分支，`flake = false` 表示只拉源码、由 `circt-nix` 来编译。`task3` 分支就是已合入全部补丁的 fork。

接着看顶部那段关键注释，它说清楚了补丁栈的定位：

[flake.nix:L56-L59](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L56-L59) —— 「Keep reviewer builds on a pinned upstream CIRCT plus the checked-in Task 3 patch stack」，即审查者构建用的是「钉死的上游 CIRCT + 已签入的 Task 3 补丁栈」。同一段里 `circtBase` 用 `patches = old.patches or [ ]` 显式清空了 circt-nix 自带的补丁，确保构建只依赖 `task3` fork 里已合入的改动，不被别的补丁污染。

再看一个补丁文件的标准头，理解它的信息密度：

[patches/circt-task3-rfp/0003-flatten-memref-shape-ops.patch:L1-L6](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/patches/circt-task3-rfp/0003-flatten-memref-shape-ops.patch#L1-L6) —— 一个标准补丁头：`From`（作者）、`Date`（2026-03-05）、`Subject: [PATCH 3/7] Flatten memref shape ops after memref flattening`（这是 7 补丁系列里的第 3 个，主题是「memref 扁平化之后再平坦化 memref 形状算子」），以及它要改的源文件 `lib/Transforms/FlattenMemRefs.cpp`。光读这五行，你就知道这个补丁动的是哪个 CIRCT 源文件、解决什么问题、在系列里的位置。

#### 4.1.4 代码实践

**实践目标**：用文件名归纳补丁栈要解决的问题类别，并理解补丁编号即应用顺序。

**操作步骤**：

1. 列出 `patches/circt-task3-rfp/` 下所有文件（共 11 个）。
2. 对每个文件名，切出 `<编号>` 和 `<动宾短语>` 两部分。
3. 按「动宾短语里出现的关键词」分桶：含 `flatten`/`memref`/`dense-resource` 的归一类；含 `handshake`/`cfg`/`frontend`/`func`/`cast` 的归一类；含 `float`/`extern` 的归一类；只含 `test` 的单列。
4. 给带 `Subject:` 头的补丁统计 `[PATCH X/N]` 里的 N，看看有几种取值。

**需要观察的现象**：你会得到一张与上文 4.1.2 那张表一致的三类划分；并且 `Subject` 里的 N 至少出现 7、8、9 三个值，印证补丁栈经历过重排。

**预期结果**：A 类（memref 扁平化）= {0003, 0009}；B 类（Handshake 降级正确性）= {0004, 0005, 0008, 0010, 0011, 0012, 0013}；C 类（浮点外部化）= {0015}；支撑性测试补丁 = {0014}。

#### 4.1.5 小练习与答案

**练习 1**：为什么补丁要按数字编号顺序应用，而不能乱序？

**参考答案**：补丁之间存在依赖——后一个补丁可能改的是前一个补丁新增的代码，或者前一个补丁先把某段逻辑合法化、后一个补丁才能在上面加新模式。乱序应用会让 `git am` 找不到上下文行而失败。编号 `0003 → 0015` 就是约定的应用顺序。

**练习 2**：补丁栈放在这个仓库里，但构建编译的却是 fork 的 `task3` 分支。这两者是什么关系？

**参考答案**：`task3` 分支已经把全部补丁合进了提交历史，是构建实际编译的代码；`patches/circt-task3-rfp/` 是这组改动的 `git format-patch` 导出留档，给审查者读「与上游的精确差异」用，不必克隆 fork 历史就能审计。

---

### 4.2 浮点算子降为 extern 的处理：补丁 0015

#### 4.2.1 概念说明

u3-l4 讲过，CIRCT 把 Handshake 降到 HW 时，正常做法是把每个算子翻译成具体的硬件逻辑（比较器、加法器等）。但浮点算子（`arith.addf`、`math.exp`、`math.tanh` 等）在硬件里**又大又复杂**——一个完整的 IEEE-754 浮点加法器就是几百个 LUT，更不用说 `exp`/`tanh` 这类超越函数。

上游 CIRCT 的 Handshake→HW 降级**根本没有浮点算子的硬件实现**。于是补丁 0015 采取了「外部化」策略：在降级时，把这些浮点算子**不翻译成具体硬件，而是降级成一个 `hw.module.extern`（外部模块黑盒）**——只声明它的输入输出端口和握手接口，具体实现留给后面的 SystemVerilog 文件去补（就是 u6-l3 讲的 `circt_fp_primitives.sv` 用 Q16.16 定点近似提供的实现）。

这相当于把「我现在还不知道怎么综合实现浮点」这个问题**延后**：先让降级链能跑通、能导出 SV，浮点的精确实现以后再换（比如换成真浮点 IP、或换成更高精度的定点、或直接量化掉）。

#### 4.2.2 核心流程

补丁 0015 改的是 `lib/Conversion/HandshakeToHW/HandshakeToHW.cpp`，做两件事：

```
1. 给 18 类浮点算子注册「外部模块转换模式」（ExtModuleConversionPattern）：
   arith.addf / subf / mulf / divf / maximumf / cmpf
   arith.sitofp / uitofp / fptosi / fptoui
   arith.extf / truncf
   math.exp / fpowi / rsqrt / tanh / absf / roundeven
        │  lower-handshake-to-hw 遇到这些算子
        ▼
   不生成具体硬件，而是生成一个 hw.module.extern 实例
   （名字按算子与类型 mangle，如 arith_addf_in_f32_f32_out_f32）

2. 调整降级目标的「合法性」判定，让上述外部化能通过合法性检查。
```

什么是「转换模式（ConversionPattern）」和「合法性（legal/illegal）」？MLIR 的降级框架是这样工作的：

- 你声明一个**转换目标（ConversionTarget）**，规定哪些算子/方言是**合法（legal）**的、哪些是**非法（illegal）**的。降级的目标就是「把所有非法的都变成合法的」。
- 你给一组**转换模式（ConversionPattern）**，每条模式说「遇到算子 X，就改写成 Y」。
- 框架反复扫描：只要还有非法算子，就尝试用某条模式改写它，直到全部合法或卡死报错。

补丁 0015 做的就是：①新增 18 条「把浮点算子 X 改写成 extern 实例」的模式；②调整合法性判定，让这些 extern 实例被认作合法。

#### 4.2.3 源码精读

先看新增的 18 条 extern 转换模式：

[patches/circt-task3-rfp/0015-lower-float-ops-as-externs-in-handshake-to-hw.patch:L7-L25](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/patches/circt-task3-rfp/0015-lower-float-ops-as-externs-in-handshake-to-hw.patch#L7-L25) —— 这段在 `HandshakeToHW.cpp` 的模式列表里，给 18 类浮点算子各注册一个 `ExtModuleConversionPattern<算子>`。注释一句「Floating-point arith operations are lowered as extern modules」点明了意图：这些算子不降到具体硬件，而是降成 extern 模块。`ExtModuleConversionPattern` 是 CIRCT 已有的模板——它把一个算子包成一个外部模块实例，自动生成按算子名和操作数类型 mangle 的 extern 名字（这正是 u6-l3 里那个 `arith_addf_in_f32_f32_out_f32` 命名的由来）。

再看合法性调整：

[patches/circt-task3-rfp/0015-lower-float-ops-as-externs-in-handshake-to-hw.patch:L33-L46](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/patches/circt-task3-rfp/0015-lower-float-ops-as-externs-in-handshake-to-hw.patch#L33-L46) —— 这段改的是 `ConversionTarget target`：从合法算子列表里**移除** `UnrealizedConversionCastOp`，并**删除**原先对 `handshake::FuncOp` 的动态合法性判定（那段判定要求函数体里每个算子都已合法，会阻碍分阶段外部化）。配合补丁 0008 里新增的 `target.addDialect<math::MathDialect>()` 把整个 `math` 方言标为非法，整个机制就闭环了：浮点算子被声明为非法 → 框架被迫用 0015 新加的 extern 模式改写它们 → 改写后变成合法的 extern 实例 → 降级成功推进。

> 注意补丁之间的相互作用：0011 曾把 `UnrealizedConversionCastOp` 加进合法列表，而 0015 又把它移除。这是补丁栈迭代的真实痕迹——同一类问题在不同迭代里尝试过不同修法，最终版以 0015 为准。读补丁栈时要接受这种「后补丁覆盖前补丁」的演进。

#### 4.2.4 代码实践

**实践目标**：数清 0015 外部化了哪些浮点算子，并把它和 `circt_fp_primitives.sv` 对应起来。

**操作步骤**：

1. 打开 [patches/circt-task3-rfp/0015-lower-float-ops-as-externs-in-handshake-to-hw.patch:L8-L25](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/patches/circt-task3-rfp/0015-lower-float-ops-as-externs-in-handshake-to-hw.patch#L8-L25)。
2. 统计 `ExtModuleConversionPattern<...>` 的条目数，并分成 `arith::*` 和 `math::*` 两组列出。
3. 回顾 u6-l3 讲过的 [rtl/fp/circt_fp_primitives.sv](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/rtl/fp/circt_fp_primitives.sv)，确认里面是否为每个 extern 名字都提供了实现。

**需要观察的现象**：条目数应为 18，其中 `arith::*` 12 个（addf/subf/mulf/divf/maximumf/cmpf/sitofp/uitofp/fptosi/fptoui/extf/truncf）、`math::*` 6 个（exp/fpowi/rsqrt/tanh/absf/roundeven）。

**预期结果**：18 类浮点算子被外部化；`circt_fp_primitives.sv` 里能找到与这些算子对应的 module 名（按相同 mangle 规则命名）。如果某个 extern 在 `circt_fp_primitives.sv` 里找不到实现，u3-l4 讲的「禁止裸 extern」安全门就会在导出 SV 时 `exit 1` 失败——这就是 FP extern 挂接的失败路径。

#### 4.2.5 小练习与答案

**练习 1**：为什么选「降为 extern + 用定点近似补实现」，而不是直接在 CIRCT 里写一个真浮点加法器的降级？

**参考答案**：①工程优先级——Task 3 的目标是「证明能降级、能综合」，浮点精度是 Task 6 之后的事；extern 让降级链立刻能跑通。②资源代价——真 IEEE-754 浮点单元极大，而设计已经超配 141 倍，先不谈精度。③可替换性——extern 是黑盒，以后换成真浮点 IP 或量化都不用改降级链，只换那个 SV 文件。

**练习 2**：补丁 0008 把 `math` 方言整个标为 `illegal`，和 0015 的 extern 模式是什么关系？

**参考答案**：0008 是「驱动力」，0015 是「出路」。0008 告诉降级框架「`math` 方言里的算子不允许留在结果里」；框架于是去找能改写它们的模式，正好命中 0015 新加的 extern 模式，把它们改写成合法的 extern 实例。没有 0008 的非法声明，框架没有动力去改写这些算子；没有 0015 的模式，框架又找不到改写方法会卡死报错。两者配合才闭环。

---

### 4.3 141 倍超配与 nextpnr OOM：瓶颈结论

#### 4.3.1 概念说明

前面两节讲的都是「怎么把降级链跑通」。但跑通只是手段，**真正要回答的工程问题是：跑出来的设计，装得进目标 FPGA 吗？** 3e 报告就是对这个问题的最终回答，而答案是令人清醒的「装不下，差得很远」。

这里要厘清三个层面：

1. **目标芯片**：Xilinx Kintex-7 **XC7K480T**，是开源工具链支持的最大 FPGA 之一，约 298,600 个 CLB LUT（见 u5-l3 的 `fpgaCapacities`）。它本身就是「能用的最大芯片」——没有更大的开源目标可换。
2. **设计规模**：TinyStories-1M 浮点基线经整条降级链综合后，**约需 4212 万个 CLB LUT**（见 u1-l4 的 `summary.txt`）。
3. **超配倍数**：`42123250 / 298600 ≈ 141`。设计比芯片大**约 141 倍**。

更棘手的是第二道墙：**nextpnr-xilinx（开源布局布线工具）在这么大的设计上直接 OOM**，连布局布线都跑不到。所以资源数字只能靠 **Yosys 综合后的估算**，而不是真正的 PnR 报告。这两点合起来，构成了 Task 3 的核心瓶颈结论，也直接决定了下一步走 Task 6 而不是 Task 4。

#### 4.3.2 核心流程

得到这个结论的流程，本身就是一个值得学习的「失败分析」工程方法：

```
尝试 nextpnr-xilinx 做布局布线
        │
        ▼  OOM（退出码 137/9），无 PnR 报告
退而求其次：只用 Yosys 综合出的 mapped JSON 估算资源
        │  write_utilization_report.py 递归统计叶单元（u5-l3）
        ▼
summary.txt:  clb_luts used ≈ 42,123,250
              clb_luts capacity = 298,600 (XC7K480T)
        │  相除
        ▼
超配倍数 ≈ 141x
        │  工程判断
        ▼
结论：下一步做 Task 6（资源最小化），而非 Task 4（硬件集成/上板）
```

超配倍数的数学表达：

\[
\text{oversubscribe ratio} = \frac{\text{clb\_luts}_{\text{used}}}{\text{clb\_luts}_{\text{capacity}}}
= \frac{42{,}123{,}250}{298{,}600} \approx 141.1
\]

这个比例的意义不在于「141.1 还是 141.0」，而在于它的**量级**：哪怕估算有几倍误差、哪怕 externalize 掉一部分大存储，设计也远远装不下。这是一个「数量级意义上的否决」，不是「再优化一下就行」。

> 一个易混点：externalize（u6-l2）把超大 Handshake 存储 blackbox 当外部存储候选，能显著减小 shell 逻辑的资源量，但 3e 报告明确指出「**即使 externalize 之后**，shell 设计仍然约 141 倍超配」。也就是说 externalize 是「必要不充分」——它让资源报告只度量计算逻辑、把巨型存储当板载 DDR 候选，但计算逻辑本身就已远远超标。

#### 4.3.3 源码精读

先看 3e 报告对「这不是 nextpnr 报告」的明确声明：

[deliverables/3e-tiny-stories-1m-resource-report.md:L59-L62](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/deliverables/3e-tiny-stories-1m-resource-report.md#L59-L62) —— 「This is not a nextpnr-xilinx place-and-route utilization report. It is a Yosys estimate. nextpnr-xilinx was attempted, but it runs out of memory on this route and does not produce a final PnR result.」这句话直接交代了报告性质：是 Yosys 估算，不是 PnR；原因是 nextpnr-xilinx 在这条路线上 OOM。

接着是全文最关键的工程判断：

[deliverables/3e-tiny-stories-1m-resource-report.md:L76-L77](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/deliverables/3e-tiny-stories-1m-resource-report.md#L76-L77) —— 「Since the design is about 141x bigger (in terms of LUTs) than the target FPGA, the next task should be task 6 and not task 4.」这是 Task 3 的最终结论句：因为设计比芯片大 141 倍，下一步必须是 Task 6（资源最小化），而不是 Task 4（硬件集成）。本讲的标题「瓶颈结论」就是这一句。

报告还给了下一步可选的两条优化方向：

[deliverables/3e-tiny-stories-1m-resource-report.md:L83-L86](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/deliverables/3e-tiny-stories-1m-resource-report.md#L83-L86) —— 「Use board memory more directly.」（更直接地用板载内存，承接 u6-l2 的 externalize 方向）与「Use a MLIR dialect other than handshake. The handshake dialect uses a lot of resources in this pipeline.」（换掉 Handshake 方言，因为它在本流水线里耗资源极大——这正是 u3-l2 早就埋下的伏笔）。

最后，报告结尾那段「为 nextpnr 打过补丁但最终放弃」的诚实自述，呼应了本讲的补丁栈主题：

[deliverables/3e-tiny-stories-1m-resource-report.md:L90-L96](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/deliverables/3e-tiny-stories-1m-resource-report.md#L90-L96) —— 作者说他曾为 nextpnr 的 OOM 打过一些补丁，但发现设计比芯片大 141 倍、而 XC7K480T 已经是支持的最大 FPGA 之后，认为指望 nextpnr 支持这么大的设计是不合理的，于是丢弃了那些补丁。这是一个非常重要的工程判断示范：**当问题出在「数量级」而非「实现细节」时，停止在细节上打补丁，转而改变策略。**

回头看 Task 1c 当初预设的三大风险，会发现 3e 正好证实了其中一条：

[deliverables/1c-selected_route.md:L66-L68](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/deliverables/1c-selected_route.md#L66-L68) —— 「Even the smallest LLM does not fit in our Kintex 480k FPGA」。立项时就列出的风险，被 Task 3 的实测坐实。这种「预设风险 → 实测验证 → 据此转向」的闭环，正是本讲想强调的方法论。

#### 4.3.4 代码实践

**实践目标**：亲手从 `summary.txt` 复算 141 倍这个数字，理解它不是 nextpnr 报的。

**操作步骤**：

1. 执行 u1-l4 给出的 gate 命令：`nix build .#tiny-stories-1m-baseline-float-selftest-all-memory-utilization -L`。
2. 打开 `result/summary.txt`，找到 `clb_luts` 行，记录 `used` 与 `capacity`。
3. 用计算器算 `used / capacity`。
4. 打开 `result/stat.json`，确认它是叶单元原料（u5-l3 讲的 `leaf_counts` 产出），而非 nextpnr 的 PnR 数据。

**需要观察的现象**：`used` 约 4200 万级，`capacity` 为 298,600，比值约 141；`stat.json` 里只有 Yosys 综合出的叶单元计数，找不到任何布局布线坐标、连线、时序信息——证明这绝非 PnR 报告。

**预期结果**：比值约 141。如果你本地构建资源紧张跑不到这一步，可改为阅读 u1-l4 讲义里记录的数字 `42,123,250 / 298,600` 自行复算；结果应与 3e 报告的「about 141x」一致。**待本地验证**：实际 `used` 数字会随 CIRCT fork 与综合脚本版本微动，但量级稳定在 141 倍。

#### 4.3.5 小练习与答案

**练习 1**：既然 nextpnr OOM 拿不到 PnR 报告，凭什么相信 Yosys 的 LUT 估算足以支撑「141 倍」这个结论？

**参考答案**：因为这个结论是**数量级判断**，不依赖精确数字。Yosys 的 mapped JSON 已把设计映射到 7 系原语（LUT/FF/DSP/BRAM），其 LUT 计数是综合后的确定量；即便估算有几倍误差，从 4212 万降到 29.8 万需要削减 141 倍——没有任何估算误差能把这个数量级抹平。所以「Yosys 估算不够精确」并不动摇「装不下」的结论。

**练习 2**：作者为什么丢弃了为 nextpnr OOM 打的补丁？这体现了什么工程原则？

**参考答案**：因为他发现根因是设计比芯片大 141 倍、而 XC7K480T 已是开源支持的最大 FPGA。再怎么优化 nextpnr 的内存管理，也跑不动一个比芯片大两个数量级的设计——那是物理上不可能的。这体现的原则是：**先判断问题是「数量级」还是「实现细节」，前者要改策略（转向 Task 6 削减资源），后者才值得打补丁。**

---

## 5. 综合实践

本综合实践把本讲三块内容串起来：**补丁栈（让降级链跑通）→ 资源报告（看跑出来多大）→ 工程判断（下一步做什么）**。

**任务**：写一份不超过 400 字的「Task 3 瓶颈分析备忘」，包含以下四点，并尽量引用本讲给出的永久链接作为依据：

1. **补丁栈的三大类问题**：浏览 `patches/circt-task3-rfp/` 的文件名，归纳出 A（memref 扁平化）、B（Handshake 降级正确性）、C（浮点外部化）三类，每类各举一个补丁编号与一句中文说明。
2. **浮点 extern 的来龙去脉**：说明补丁 0015 把哪些算子外部化、外部化后由哪个 SV 文件补实现、以及「禁止裸 extern」安全门如何保证每个 extern 都有实现。
3. **141 倍的来历**：说明这个数字是 Yosys 估算而非 nextpnr PnR、nextpnr 为何 OOM、externalize 为何没改变结论。
4. **下一步的选择**：引用 3e 报告原文，说明为什么是 Task 6 而不是 Task 4，并列出 3e 给出的两条优化方向。

**操作建议**：第 1 点可以直接对照本讲 4.1.2 的表来核对你的归纳；第 2 点要回看 u6-l3 与 u3-l4；第 3、4 点直接引用 3e 报告 [L59-L62](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/deliverables/3e-tiny-stories-1m-resource-report.md#L59-L62) 与 [L76-L86](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/deliverables/3e-tiny-stories-1m-resource-report.md#L76-L86)。

**预期结果**：你应当能用一段连贯的文字说清「为了让 TinyStories-1M 跑通 Handshake→HW，作者打了 11 个 CIRCT 补丁（分三类）；跑通后发现设计比最大目标 FPGA 大 141 倍、nextpnr 还 OOM；于是按数量级判断转向 Task 6 资源最小化，方向是更直接用板载内存或换掉 Handshake 方言」。

## 6. 本讲小结

- **补丁栈是「上游不够用」时的工程方法**：本项目通过维护 `RCoeurjoly/circt` 的 `task3` fork 来定制上游 CIRCT，`patches/circt-task3-rfp/` 里的 11 个 `.patch` 文件是这组改动的留档，文件名 `<编号>-<动宾短语>` 既编码应用顺序也点明改什么。
- **补丁要解决的问题可归三大类**：A 类 memref 扁平化（0003/0009）、B 类 Handshake 降级正确性（0004/0005/0008/0010/0011/0012/0013）、C 类浮点算子外部化（0015），外加一个测试支撑补丁 0014。
- **补丁 0015 把 18 类浮点算子降为 `hw.module.extern` 黑盒**，由 `circt_fp_primitives.sv` 用 Q16.16 定点近似补实现；「禁止裸 extern」安全门保证每个 extern 都有对应实现，否则导出 SV 时 `exit 1`。
- **核心瓶颈结论是 141 倍超配**：TinyStories-1M 浮点基线约需 4212 万 CLB LUT，目标 XC7K480T 容量仅 298,600，比值约 141。
- **资源数字是 Yosys 估算而非 nextpnr PnR**：nextpnr-xilinx 在这个设计上 OOM，跑不到布局布线；externalize 大存储后 shell 仍约 141 倍超配。
- **下一步是 Task 6 而非 Task 4**：因为这是数量级问题（且 XC7K480T 已是最大开源目标），3e 给出的方向是「更直接用板载内存」与「换掉 Handshake 方言」，作者也因此丢弃了为 nextpnr OOM 打的补丁。

## 7. 下一步学习建议

- **接下来读 u7-l1（注册一个新模型进入流水线）**：当你自己想接一个更小的模型试水时，需要走完「写 adapter → 在 `nix/models.nix` 用 `registerModel` 注册 → 复用 `pipeline.nix` 全链」的流程，并决定要不要打开 `allowHwExterns`/`fpPrimsSv` 等开关——本讲的浮点 extern 知识正好帮你判断 `fpPrimsSv` 何时必须开。
- **接着读 u7-l3（后续路线与资源优化方向）**：它把 3e 报告里「换掉 Handshake 方言」「更直接用板载内存」这两条方向展开成 Task 6 的具体策略清单（量化、换方言、复用板载 DDR），是本讲瓶颈结论的直接延续。
- **想深入补丁本身**：可以克隆 `RCoeurjoly/circt` 的 `task3` 分支，对照本讲的 `.patch` 留档，用 `git log` 找到对应的提交，结合 u3-l2/u3-l3 的降级链脚本，在本地改一个补丁、重跑某一段降级，观察「不打这个补丁会怎样」——这是理解补丁栈最扎实的方式。
