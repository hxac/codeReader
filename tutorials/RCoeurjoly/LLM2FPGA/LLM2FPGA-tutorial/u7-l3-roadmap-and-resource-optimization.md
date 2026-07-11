# 后续路线与资源优化方向

## 1. 本讲目标

本讲是整本手册的收束篇。前面六单元我们一路追着降级链跑：PyTorch → torch-MLIR → CIRCT → SystemVerilog → Yosys，最后在 u6-l4 得到一个冰冷的结论——TinyStories-1M 能降级、能综合，但比目标芯片大约 141 倍，装不下。

那么接下来项目往哪走？本讲只回答这一个问题。读完本讲你应该能够：

1. 说清 `project-plan_v2.md` 里 Task 4、Task 5、Task 6 各自要解决什么、彼此什么依赖关系。
2. 理解为什么当前阶段跳过 Task 4（上板）直接做 Task 6（资源最小化）是一个工程判断，而不是偷懒。
3. 列出至少三类资源优化候选策略（模型层 / MLIR 层 / RTL 层），并能讨论它们的预期收益与实现难度。
4. 把「141 倍超配」这个瓶颈数字，映射到具体的优化策略上——哪一条策略直接对应「换掉 Handshake 方言」。

本讲几乎没有新代码，主要读的是项目规划文档。但正因为它把前面的技术细节收敛成「下一步做什么」，它对想参与二次开发的人格外重要。

## 2. 前置知识

本讲默认你已经读过 u6-l4（CIRCT 补丁栈与瓶颈结论）。为了衔接，先快速回顾几个关键事实与术语：

- **降级链（lowering chain）**：把 PyTorch 模型一步步翻译成硬件的过程，每一站换一种中间表示（dialect，方言）。
- **Handshake 方言**：CIRCT 里的「弹性数据流」表示，靠 `valid`/`ready` 握手传递数据。它在 u3-l2 引入，在 u6-l2 被点名为资源大户。
- **CLB LUT**：FPGA 的基本逻辑单元（查找表）。LUT 数量是判断「装不装得下」最直观的指标。
- **141 倍超配**：TinyStories-1M 的 shell 设计约需 42,123,250 个 CLB LUT，而目标芯片 XC7K480T 只有 298,600 个，比值约 141。
- **nextpnr-xilinx OOM**：开源布局布线工具因为设计太大，内存耗尽跑不完。所以资源数只能靠 Yosys 估算，而不是真正的 PnR 报告。

一个核心直觉先建立起来：**141 倍是一个数量级问题，不是细节问题**。打磨一两个 pass、修一两个 bug，省不下两个数量级。所以下一步必须是结构性的资源削减，而不是上板调参。这就是本讲全部论证的起点。

## 3. 本讲源码地图

本讲涉及的关键文件都是规划/报告类文档，而非可执行代码：

| 文件 | 作用 |
| --- | --- |
| `docs/project-plan_v2.md` | 项目总计划，定义 Task 1–6 的目标、子任务、交付物。本讲主要读 Task 4/5/6。 |
| `deliverables/3e-tiny-stories-1m-resource-report.md` | Task 3 的最终交付物，给出 141 倍超配结论与「下一步该走 Task 6」的判断，并列出候选优化方向。 |
| `README.md` | 仓库门面，用一段话总结当前状态（能降级但超配），并把后续方向点名为 Task 6。 |

这三个文件共同构成项目的「自我认知」：计划说要做什么（project-plan），做完 Task 3 发现了什么（3e 报告），对外怎么宣告（README）。本讲就是把这三处串起来读。

## 4. 核心概念与源码讲解

### 4.1 Task 4/5/6 路线图：项目下一步往哪走

#### 4.1.1 概念说明

`project-plan_v2.md` 把整个项目切成 6 个 Task。前三个已经讲过：Task 1 选路线，Task 2 在最小 matmul 核上验证降级链通，Task 3 在 TinyStories-1M 上证明「能降级」。剩下三个是未来工作：

- **Task 4 — FPGA integration and hardware validation**：把降级出来的 RTL 真的烧进 FPGA 板子（YPCB-00338-1P1），跑一组固定输入，比对硬件输出与 PyTorch 参考是否一致。这是「上板」。
- **Task 5 — Scaling and resource usage analysis**：把同一条流水线跑在 TinyStories 整个家族（1M/3M/8M/28M/33M）上，看资源用量和流水线健壮性随模型规模怎么变，画 scaling 曲线。
- **Task 6 — Resource usage reduction strategies**：如果（大概率）更大的模型装不下，调研一批资源削减技术（量化、换方言、用板载内存等），逐一测量它们省了多少 LUT/FF/BRAM/DSP。

这三者不是线性顺序，而是**有条件分支**：Task 3 的结论决定了先做谁。理解这个分支是本模块的核心。

#### 4.1.2 核心流程

三个 Task 的依赖关系可以用下面这张图概括：

```
        Task 3 (已完成): 证明可降级, 但 141x 超配
                 |
        +--------+---------+
        |                  |
   Task 4 上板          Task 6 资源最小化
   (需要装得下)          (把 141x 压下来)
        ^                  |
        |                  |
        +------------------+
   Task 6 成功后, 才轮到 Task 4

   Task 5 (scaling) 可与 Task 6 并行:
   给出"模型变大, 资源怎么变"的曲线, 为 Task 6 提供基线
```

关键判断点：**Task 4 的前提是设计装得下**。Task 4 的目标里有「完成 full FPGA synthesis and place-and-route」「loading the design on hardware」——这些动作在 141 倍超配时根本无法发生（nextpnr 直接 OOM）。所以工程上合理的顺序是：先用 Task 6 把资源压到能装下，再回头做 Task 4。Task 5 则提供「基线模型」给 Task 6 做对比（Task 6 的 baseline 是「the largest successfully lowered model from the scaling task」）。

#### 4.1.3 源码精读

先看 Task 4 的目标定义，注意它对「装得下」的隐含假设：

[docs/project-plan_v2.md:277-293](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/docs/project-plan_v2.md#L277-L293) —— Task 4 的标题与目标。这段说要在 YPCB-00338-1P1 板上做 FPGA 集成，包括「Completing full FPGA synthesis and place-and-route」和「Loading the design on hardware」。每一项都默认设计能通过综合与布线——而这正是当前被 141 倍超配卡住的地方。

再看 Task 5，它本身不削减资源，只测量：

[docs/project-plan_v2.md:377-391](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/docs/project-plan_v2.md#L377-L391) —— Task 5 的标题与目标。注意两点：一是它明确说「No new model architectures are introduced」，只跑现有 TinyStories 家族；二是它要产出一个「per-model pipeline results matrix」，记录每个模型在每一站的 pass/fail、编译时间、资源用量。这个矩阵就是 Task 6 的基线数据来源。

最后是 Task 6，本讲的主角：

[docs/project-plan_v2.md:440-449](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/docs/project-plan_v2.md#L440-L449) —— Task 6 的标题与目标。原文「Evaluate if mitigation techniques can reduce resource usage if the scaling task shows that larger models do not fit the target device」，并且「Baseline is the largest successfully lowered model from the scaling task」。这句话把 Task 5 和 Task 6 的依赖关系钉死了：Task 6 的基线来自 Task 5。

Task 6 的子任务 a 直接列出了要调研的策略层次：

[docs/project-plan_v2.md:464-466](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/docs/project-plan_v2.md#L464-L466) —— Task 6a。原文「Survey 5 to 10 mitigation strategies, at model-level, MLIR, and RTL: quantization at PyTorch level, eqmap at verilog level, mlir optimizations etc」。这三个层次（model-level / MLIR / RTL）就是下一节要展开的策略分类骨架。

Task 6b 还规定了每条策略必须记录的指标，并且有一条硬约束：

[docs/project-plan_v2.md:474-482](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/docs/project-plan_v2.md#L474-L482) —— Task 6b。每条策略要记录 Delta LUT/FF/BRAM/DSP、Delta max clock frequency、Delta toolchain runtime + peak memory，并且「Each strategy must work without blackboxes or stubs」。最后这句很关键：它排除了「把算子黑盒掉来假装省资源」这种作弊路径，与 Task 3「Final success must not rely on operator stubbing or blackboxes」一脉相承。

#### 4.1.4 代码实践

这是一个源码阅读型实践，目标是让你亲手确认上面的依赖关系图。

1. **实践目标**：把 Task 4/5/6 的依赖关系从文档里挖出来，画成一张图。
2. **操作步骤**：
   - 打开 `docs/project-plan_v2.md`，分别找到 Task 4（约第 277 行）、Task 5（约第 377 行）、Task 6（约第 440 行）的 `### Goal:` 段。
   - 在 Task 6 的目标里找到「Baseline is the largest successfully lowered model from the scaling task」这句，确认 Task 6 依赖 Task 5。
   - 在 Task 4 的目标里数一下有几个动作（接口集成、综合布线、上板、比对）必须「设计装得下」才能进行。
3. **需要观察的现象**：你会看到 Task 4 的每一个子任务都隐含假设综合与布线能跑通；而 Task 6 的存在前提正是「larger models do not fit」。
4. **预期结果**：你画出的依赖图应该和本节 4.1.2 的图一致——Task 6 是 Task 4 的前置条件，Task 5 为 Task 6 提供基线。
5. 由于这是纯文档阅读，结果可本地直接验证，无需标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：Task 6 的目标里说 baseline 是「the largest successfully lowered model from the scaling task」。如果 Task 5 还没做，Task 6 能开始吗？

**参考答案**：严格按计划不能，因为 baseline 没有定义。但项目实际情况是：Task 3 已经把 TinyStories-1M 跑通并给出了资源报告，所以当前可以先用 TinyStories-1M 作为 baseline 启动 Task 6 的策略调研（Task 6a），等 Task 5 给出更完整的家族矩阵后再修正 baseline。这也正是 README 里「We now move to Task 6」的现实依据。

**练习 2**：Task 4 的风险列表里有一条「Auto-generated interfaces may be impractical for real hardware integration」。这条风险和我们 u6-l1 讲的自动生成自测外壳有什么联系？

**参考答案**：u6-l1 的 `gen_tiny_stories_selftest_top.py` 正是「自动生成接口」的一个实例——它解析 `main.sv` 端口来生成外壳。Task 4 这条风险在说：这种自动生成的接口（ESI 的 valid/ready 通道、外部存储抽象）在真上板时可能不实用，需要换成「documented FPGA designs for the same board」那种面向主机的接口。也就是说，Task 4 阶段大概率要替换掉自测外壳，重新定义 host-facing interface。

### 4.2 资源优化候选策略：把 141 倍压下来

#### 4.2.1 概念说明

Task 6a 要求调研 5 到 10 条策略，并按三个层次分类：模型层（model-level）、MLIR 层、RTL 层。结合 3e 报告与 README 里点名的方向，本节把它们整理成一张候选清单。

先明确一点：这些策略目前**都还没实施**（Task 6 状态是 TODO），所以下面讨论的「预期收益」是基于前面几讲源码分析的推断，真实数字要等 Task 6b 测出来。我们在这里做的是「预排序」，帮读者建立直觉。

#### 4.2.2 核心流程

按 Task 6a 的三层分类，候选策略如下表。每条标注它主要削减哪类资源、对应 3e/README 的哪句话：

| 层次 | 候选策略 | 主要削减资源 | 对应文档依据 |
| --- | --- | --- | --- |
| 模型层 | **量化**（PyTorch 层 int8/int4） | LUT/FF/DSP（按位宽成比例降） | README 第 39 行「cut resource usage」、Task 6a「quantization at PyTorch level」 |
| MLIR 层 | **换掉 Handshake 方言** | LUT/FF/BRAM（握手缓冲是资源大户） | 3e 报告第 84-86 行「Use a MLIR dialect other than handshake」 |
| MLIR 层 | **通用 MLIR 优化**（CSE、折叠、共享、更激进的 bufferize） | LUT/FF（局部死代码/冗余） | Task 6a「mlir optimizations」 |
| RTL 层 | **更直接用板载内存 / DDR3 卸载** | BRAM/LUT（把超大存储移出片上） | 3e 报告第 82-83 行、README 第 39 行「DDR3 memory offload」 |
| RTL 层 | **eqmap**（Verilog 层算子等价映射/复用） | LUT/DSP（算子复用、时分共享） | Task 6a「eqmap at verilog level」 |

为什么这三层能覆盖主要瓶颈？因为 141 倍超配不是单一来源，而是几个叠加因素：

- **位宽**：浮点 32 位运算撑大了所有数据通路与算子 → 量化。
- **握手缓冲**：Handshake 方言每个通道插缓冲，缓冲吃 FF/LUT/BRAM（u3-l2 讲过）→ 换方言。
- **片上存储**：超大 Handshake 存储占满 BRAM（u6-l2 讲过 externalize 的 128 kbit 阈值）→ DDR3 卸载。

把这三个因素各自对应到一条策略，就构成了「瓶颈到策略」的骨架（下一节详述）。

#### 4.2.3 源码精读

3e 报告在结论段直接列出了从 Task 3 学到的两条路：

[deliverables/3e-tiny-stories-1m-resource-report.md:79-86](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/deliverables/3e-tiny-stories-1m-resource-report.md#L79-L86) —— Task 6 的两条候选路径。原文列出「Use board memory more directly」和「Use a MLIR dialect other than handshake」，后者还附了 Handshake 方言文档链接，并明确「uses a lot of resources in this pipeline」。这两条是项目作者基于亲手跑 Task 3 后得出的第一手判断，权重最高。

值得注意的是，`all-memory` 这个目标段本身就已经做了一半「用板载内存」的工作。看目标名解释：

[deliverables/3e-tiny-stories-1m-resource-report.md:32-38](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/deliverables/3e-tiny-stories-1m-resource-report.md#L32-L38) —— `all-memory` 段含义。原文「blackboxes all oversized Handshake memory modules found in the model, treating them as external-memory candidates」，并且「The use of Handshake dialect is one of the biggest burdens in the current pipeline and removing it is a goal of task 6」。这段同时点出了两个事实：外部化存储已经在做（对应 u6-l2 的 `externalize_large_memories.py`），而 Handshake 方言是最大负担之一（对应换方言策略）。

README 用更口语的方式总结了后续方向，并点出了第三条路——量化类：

[README.md:37-40](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/README.md#L37-L40) —— README 对后续方向的概括。原文「We now move to Task 6 to cut resource usage (DDR3 memory offload, pipeline changes, etc.)」。这里「DDR3 memory offload」对应板载内存策略，「pipeline changes」可涵盖换方言与量化。注意 README 没有直接用 quantization 这个词，但 Task 6a 子任务明确列了「quantization at PyTorch level」。

Task 6a 则把策略层次正式定义下来：

[docs/project-plan_v2.md:464-466](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/docs/project-plan_v2.md#L464-L466) —— Task 6a 的三层策略。model-level（quantization）/ MLIR（mlir optimizations）/ RTL（eqmap at verilog level）。注意这里 eqmap 被归到 verilog（RTL）层，与 3e 的「board memory」同属 RTL 层的两个不同方向。

#### 4.2.4 代码实践

这是本讲的主实践，直接对应规格要求：列出至少 3 条资源优化策略，按「预期收益/实现难度」排序，并指出哪一条对应「换掉 Handshake 方言」。

1. **实践目标**：基于 3e 报告与 Task 6a，给出一套自己的策略预排序。
2. **操作步骤**：
   - 重读 3e 报告第 79-86 行与 Task 6a（第 464-466 行）。
   - 从 4.2.2 的表里挑出至少 3 条策略。
   - 对每条策略估计「预期收益」（能砍掉多少比例的 LUT）和「实现难度」（要改降级链的哪一段、是否破坏等价性）。
   - 给一个排序，并明确指出哪条对应「换掉 Handshake 方言」。
3. **需要观察的现象**：你会发现在 3e 报告里被作者亲口点名的两条（板载内存、换 Handshake 方言）天然排在前面，因为它们有第一手证据支撑。
4. **预期结果**（参考答案，真实数字待 Task 6b 验证）：

   **参考排序（按「预期收益高且证据强」优先）**：

   | 排序 | 策略 | 预期收益 | 实现难度 | 依据 |
   | --- | --- | --- | --- | --- |
   | 1 | 换掉 Handshake 方言 | 高（3e 点名「biggest burdens」之一，缓冲遍布全图） | 高（要改 u3-l2/u3-l3 一大段降级链，可能换静态调度数据流） | 3e 第 84-86 行 |
   | 2 | 量化（int8/int4） | 高（位宽直接砍 4×～8×，影响所有数据通路与 DSP） | 中（PyTorch 层 PTQ 或微调，但要重做 u6-l3 的定点近似） | Task 6a「quantization」 |
   | 3 | 更直接用板载内存 / DDR3 卸载 | 中（释放片上 BRAM/LUT，但计算逻辑不变） | 中（u6-l2 的 externalize 已搭好框架，需对接真 DDR3 控制器） | 3e 第 82-83 行、README 第 39 行 |
   | 4 | eqmap（RTL 层算子复用） | 中低（针对算子级冗余，全局收益有限） | 中（在已综合 RTLIL 上做等价变换） | Task 6a「eqmap」 |
   | 5 | 通用 MLIR 优化 | 低（局部清理，省不下两个数量级） | 低（多为现成 pass） | Task 6a「mlir optimizations」 |

   **直接对应「换掉 Handshake 方言」的是第 1 条**：它就是 3e 报告第 84 行「Use a MLIR dialect other than handshake」的字面对应，属于 MLIR 层策略。

5. 上述收益估计是推断，标注「真实 delta 待 Task 6b 测量」。

#### 4.2.5 小练习与答案

**练习 1**：量化（int8/int4）能省 LUT，但它和我们 u6-l3 讲的 Q16.16 定点近似是什么关系？

**参考答案**：方向一致但层次不同。u6-l3 的 Q16.16 是在浮点 extern 已经生成之后，用定点 SV 代码去近似实现浮点算子——这是「事后补救」，且 Q16.16 范围/精度都太差。而 Task 6a 的量化是在 PyTorch 层就把权重和激活压成 int8/int4，整条降级链从头就走窄位宽，数据通路、算子、存储全部变小。前者是 RTL/SV 层的近似，后者是模型层的根治。真正谈精度，得靠后者。

**练习 2**：为什么「通用 MLIR 优化」排在最后？

**参考答案**：因为 141 倍是数量级差距，而 MLIR 通用优化（CSE、常量折叠、canonicalize）只能做局部清理，省不下两个数量级。降级链里每一站其实已经跑了 `-canonicalize`/`-cse`（见 u2-l3、u3-l1），容易摘的果子早摘过了。它难度低、值得做，但收益天花板低，所以排在策略清单末位。

### 4.3 瓶颈到策略的映射：为什么是 Task 6 而不是 Task 4

#### 4.3.1 概念说明

本模块回答一个看似简单但很关键的问题：**既然项目目标是上板跑 LLM，为什么不直接上板（Task 4），而要先绕去削资源（Task 6）？**

答案是：141 倍超配让 Task 4 的前提条件不成立。Task 4 的每一步（综合、布线、烧板）都默认设计能装进芯片；而一个比芯片大 141 倍的设计，连综合都勉强、布线直接 OOM，根本到不了「烧板」那一步。所以不是「不想上板」，而是「上不了板」。

这个判断的依据写在 3e 报告和 README 里，是一段非常清晰的工程推理。本模块把它拆给你看。

#### 4.3.2 核心流程

把瓶颈数字算清楚。LUT 维度的超配倍数：

\[
\text{overage} = \frac{\text{used}}{\text{capacity}} = \frac{42{,}123{,}250}{298{,}600} \approx 141
\]

这个比值意味着什么？把它翻译成「需要多少片目标芯片」更直观：

\[
\text{chips needed} \approx \lceil 141 \rceil = 141 \text{ 片 XC7K480T}
\]

也就是说，当前设计要约 141 片当前目标芯片才装得下。而且 XC7K480T 已经是项目支持的「the largest supported FPGA」（3e 报告原话）。换更大的芯片这条路也堵死了。

于是推理链是：

1. 设计比最大目标芯片大 141 倍。
2. 换更大的芯片 → 已经是最大，无路可换。
3. 直接上板（Task 4）→ 综合/布线跑不通（nextpnr OOM），前提不成立。
4. 唯一出路 → 先把设计本身变小（Task 6）。

这就是「下一步是 Task 6 而不是 Task 4」的完整逻辑。

#### 4.3.3 源码精读

3e 报告把这段推理写得最清楚：

[deliverables/3e-tiny-stories-1m-resource-report.md:63-77](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/deliverables/3e-tiny-stories-1m-resource-report.md#L63-L77) —— 3e 报告结论段。原文「even after externalizing oversized handshake memory modules ..., the shell design for tiny stories 1M does not fit in the target FPGA」，并且「Since the design is about 141x bigger (in terms of LUTs) than the target FPGA, the next task should be task 6 and not task 4」。注意「even after externalizing」——即使做了 u6-l2 的外部化（已经削掉了一部分存储），shell 仍然超配，说明问题不只在存储。

紧接着 3e 报告还解释了为什么放弃 nextpnr：

[deliverables/3e-tiny-stories-1m-resource-report.md:88-97](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/deliverables/3e-tiny-stories-1m-resource-report.md#L88-L97) —— 关于 nextpnr 的说明。原文说作者先试了 nextpnr-xilinx 做 PnR，但 OOM；本来写了些补丁想修，但发现设计比目标大 141 倍，而目标已经是支持的最大 FPGA，「so it seems unreasonable to me to expect nextpnr-xilinx to support that design」。这段话把「放弃 nextpnr 路线」的根因说得很透：不是工具 bug，而是规模不合理。

README 用更精炼的版本同步了这个结论与具体数字：

[README.md:48-54](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/README.md#L48-L54) —— README 的当前状态段。原文「The Yosys estimate reports 42,123,250 CLB LUTs for a device capacity of 298,600 CLB LUTs, i.e. about 141x over the LUT budget」，并解释 nextpnr 每次 OOM 与 141 倍超配一致。这段是项目对外的「自我宣告」：当前结果只能当 bottleneck report，不能烧板。

#### 4.3.4 代码实践

1. **实践目标**：把三个瓶颈来源（位宽、握手缓冲、片上存储）分别映射到一条优化策略，确认「换 Handshake 方言」对应的是哪个瓶颈。
2. **操作步骤**：
   - 列出三个瓶颈来源：浮点 32 位位宽、Handshake 握手缓冲、超大 Handshake 片上存储。
   - 对照 4.2.2 的策略表，给每个瓶颈配一条策略。
   - 特别标注：哪条策略直接命中「换掉 Handshake 方言」。
3. **需要观察的现象**：你会看到「换 Handshake 方言」同时命中两个瓶颈（握手缓冲 + 片上存储都源于 Handshake 方言的选择），这解释了为什么 3e 把它列为「biggest burdens」。
4. **预期结果**（参考答案）：

   | 瓶颈来源 | 对应策略 | 命中「换 Handshake」？ |
   | --- | --- | --- |
   | 浮点 32 位位宽撑大数据通路 | 量化（int8/int4） | 否 |
   | Handshake 握手缓冲吃 FF/LUT/BRAM | 换掉 Handshake 方言 | **是** |
   | 超大 Handshake 片上存储占满 BRAM | 换掉 Handshake 方言 + DDR3 卸载 | **是**（部分） |

   注意片上存储这一行：换方言能从根上减少 Handshake 存储的产生，而 DDR3 卸载（u6-l2 的 externalize）是「即使保留 Handshake 也能把大存储移出片上」的补救。两者针对同一瓶颈的不同层面。

5. 此为文档映射练习，可本地直接验证。

#### 4.3.5 小练习与答案

**练习 1**：如果有一天 nextpnr-xilinx 修好了 OOM，能跑完 PnR，那是不是就可以跳过 Task 6 直接做 Task 4？

**参考答案**：不能。nextpnr 能跑完只意味着「工具不崩」，不意味着「设计装得下」。一个比芯片大 141 倍的设计，即使 PnR 跑完，结果也是 99%+ 的逻辑无法布线。3e 报告第 88-97 行说得很清楚：问题不是 nextpnr 的 bug，而是规模本身不合理。所以无论 nextpnr 修没修好，Task 6（把设计变小）都是 Task 4 的前置。

**练习 2**：3e 报告第 65-66 行说「even after externalizing oversized handshake memory modules ..., does not fit」。这说明了 externalize（u6-l2）的什么性质？

**参考答案**：说明 externalize 是「必要但不充分」的手段。它把超大存储移出片上（释放 BRAM/LUT），但 shell 的计算逻辑、握手缓冲、其余存储仍然超配 141 倍。这正好印证了 4.3.4 的映射——存储只是瓶颈之一，光解决存储不够，还得靠换方言和量化去砍计算与缓冲。

## 5. 综合实践

设计一个贯穿本讲的小任务：**为 Task 6 写一份一页纸的执行草案**。

把本讲三块内容（路线图、策略清单、瓶颈映射）串成一份论证：

1. **从瓶颈出发**：引用 3e 报告第 76-77 行的 141 倍结论，说明为什么必须先做 Task 6 而非 Task 4（用 4.3 的推理链）。
2. **选策略**：从 4.2.4 的排序里挑出你认为最该先做的两条策略，给出理由（必须引用 3e 报告或 README 的原话作依据）。
3. **定基线与指标**：参考 Task 6b（第 474-482 行），说明你要用哪个模型当 baseline、要记录哪些 delta（LUT/FF/BRAM/DSP、Fmax、编译时间/峰值内存）。
4. **给硬约束**：引用 Task 6b 的「Each strategy must work without blackboxes or stubs」，说明你的策略不能靠黑盒作弊。
5. **风险预案**：引用 Task 6 的 risks（第 452-456 行），说明某条策略「breaks the pipeline」时怎么办（测试多条、记录中性/负面结果）。

**预期产出**：一份不超过一页的 Markdown，包含上述五点，每点至少一处对 `project-plan_v2.md` 或 3e 报告的行号引用。这份草案同时就是你向项目维护者提案「我来做 Task 6 的某一条策略」时的论据。

> 说明：本实践不要求跑任何命令，产出是论证文档。策略的真实效果数字标注「待 Task 6b 测量」即可，不要编造。

## 6. 本讲小结

- 项目剩余三个 Task 是：Task 4（上板验证）、Task 5（TinyStories 家族 scaling 分析）、Task 6（资源削减策略）。Task 6 的 baseline 来自 Task 5，Task 4 的前提是设计装得下。
- 当前结论是跳过 Task 4、先做 Task 6：TinyStories-1M 比 XC7K480T 大约 141 倍（42,123,250 vs 298,600 CLB LUT），而该芯片已是支持的最大 FPGA，nextpnr-xilinx 因 OOM 跑不完 PnR。
- 141 倍是数量级问题，不是细节问题，必须靠结构性削减（量化、换方言、DDR3 卸载），而非调参或修 bug。
- Task 6a 把策略分三层：模型层（quantization）、MLIR 层（mlir optimizations、换方言）、RTL 层（eqmap、board memory）。3e 报告亲口点名的两条是「更直接用板载内存」和「换掉 Handshake 方言」。
- 「换掉 Handshake 方言」直接对应 3e 报告第 84 行「Use a MLIR dialect other than handshake」，它同时命中两个瓶颈（握手缓冲 + 片上存储），是预期收益最高、也是实现难度最高的策略。
- Task 6b 要求每条策略记录 LUT/FF/BRAM/DSP、Fmax、编译时间/峰值内存的 delta，且「must work without blackboxes or stubs」——禁止用黑盒作弊。

## 7. 下一步学习建议

本讲是手册的终点，但也是项目实战的起点。按你的兴趣，有三个方向可以继续：

1. **想做 Task 6（资源优化）**：从量化入手最直接。回头读 u2-l1（适配器契约）与 u7-l1（注册新模型），尝试写一个 int8 量化的 TinyStories adapter，复用 `registerModel` 接入流水线，对比 `summary.txt` 里 `clb_luts` 的变化。这是本讲 4.2.4 排序第 2 的策略，难度适中、收益高。
2. **想做 Task 5（scaling）**：参照 u7-l1 的「两步法」，把 TinyStories 家族的 3M/8M/28M/33M 依次注册进 `nix/models.nix`，复用 u3-l5 的 `mkPipeline` 跑全链，用 u5-l3 的 `write_utilization_report.py` 出资源曲线。重点观察「哪一站先失败」（Task 5c 的 bottleneck summary）。
3. **想深挖降级链、挑战「换 Handshake 方言」**：这是收益最高也最难的方向。先把 u3-l2（CF→Handshake）和 u3-l3（Handshake→HW/ESI）吃透，再读 u6-l4 的 CIRCT 补丁栈，理解 Handshake 在哪几站被引入、要替换它需要重写哪些 pass。可以从小处着手——比如调研 CIRCT 是否有「静态调度数据流」方言能替代弹性 Handshake。

无论选哪条，记住 3e 报告留下的那条工程纪律：**每条策略都必须在不靠 blackbox/stub 的前提下跑通全链**。这是 LLM2FPGA 作为「全开源证明」项目的底线，也是它与那些靠闭源 IP 凑出 demo 的方案的根本区别。
