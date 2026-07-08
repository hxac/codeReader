# Merge 仲裁合流与控制门

## 1. 本讲目标

本讲承接 u12-l2（Fork/Join/Branch），继续把 ready/valid 握手从「点对点」推广到「多路合成一路」与「按条件放行」。区别在于合流的**策略**不同：

- Join 是「**会合**」——所有输入到齐才合成一路（AND 归约）；
- Merge 是「**仲裁**」——多路输入里**任一**路有效即可输出，由仲裁器/选择器决定这一拍放谁过（OR/优先/轮询）。

学完本讲，你应该能够：

1. 说清 **Merge 与 Join 的本质区别**：同为「多进一出」，Merge 输出宽度等于单路（不拼接）、按仲裁顺序逐笔放行；Join 输出更宽（拼接）、要求全部到齐。
2. 读懂三类 Merge（One-Hot / Priority / Round-Robin）如何复用「Skid Buffer + 仲裁器/选择器 + 独热 mux + 独热 demux」拼装而成，并说清三者选路与缓冲位置的差异。
3. 理解 `Pipeline_Gate` 为何**必须同时门控 valid 与 ready**（只堵一个会丢数据或吃垃圾），以及它如何用一个 `Annuller` 同时处理控制信号甚至数据。
4. 读懂 `Pipeline_Credit_Gate` 如何用「信用计数器 + Pipeline_Gate」实现**信用制流控**（credit-based flow control），并解释其增减信用的组合巧解。
5. 用 `Pipeline_Merge_Round_Robin` 合流两路数据流，用 `Pipeline_Gate` 实现按使能放行。

## 2. 前置知识

阅读本讲前，请确认你已掌握以下概念（来自前置讲义）：

- **ready/valid 握手**（u9-l1）：source 驱动 `valid`/`data` 指向 destination，destination 驱动 `ready` 指回 source；同一拍 `valid && ready` 即握手完成；接口内**不得有组合环路**。
- **handshake_complete 门控**（u9-l2）：凡影响接口的内部状态只能在握手完成拍改变，标准式 \(\text{handshake\_complete} = (\text{ready} \land \text{valid})\)。
- **Fork/Join/Branch 与 AND/OR 归约**（u12-l2）：Fork/Join 用 AND 归约（会合、全部到齐），Branch 用 OR 归约（非会合、谁先就绪谁先走）。本讲的 Merge 属于「OR/仲裁」一族，与 Branch 同类、与 Join 相对。
- **优先/轮询仲裁器**（u11-l1）：`Arbiter_Priority` 固定优先级（最低位优先、保持授权至请求释放、可能饥饿），`Arbiter_Round_Robin` 掩码法轮询（按活动比例公平分配、空闲时冻结进度）。两者都输出**独热 grant**。
- **Annuller 与独热 mux/demux**（u5-l1、u5-l2）：`Annuller` 按 `annul` 把一个字清零（MUX/AND 两种实现）；`Multiplexer_One_Hot` 用「annul 未选中路 + OR 归约」选路；`Demultiplexer_One_Hot` 是其镜像。本讲的 Merge/Gate 几乎完全由这些零件拼成。
- **Skid Buffer**（u10-l1）：用一个缓冲寄存器切断 ready/valid 接口的组合路径，是本书给纯组合握手模块加缓冲的标准件。

本讲的核心反差只有一句：**Merge 是「仲裁合流」，与 Join 的「会合拼接」相对**；而 Gate / Credit Gate 是「条件放行」，是把一个 Annuller 提升成完整的握手门控。记住这条，全讲就贯通了。

## 3. 本讲源码地图

| 文件 | 作用 | 是否含状态 |
| --- | --- | --- |
| `Pipeline_Merge_One_Hot.v` | 由**外部独热 selector** 选定合流哪一路；多位/无位时可退化为 Join 或 Gate | 是（输出侧 1 个 skid 寄存器） |
| `Pipeline_Merge_Priority.v` | 由 `Arbiter_Priority` 仲裁，最低位优先、保持授权 | 是（每路输入 1 个 skid 寄存器） |
| `Pipeline_Merge_Round_Robin.v` | 由 `Arbiter_Round_Robin` 仲裁，等优先级轮询、切换时有一拍空拍 | 是（每路输入 1 个 skid 寄存器） |
| `Pipeline_Gate.v` | 按 `enable` 同时门控 valid 与 ready（可选连数据一起清零），纯组合 | 否（纯组合） |
| `Pipeline_Credit_Gate.v` | 用信用计数器门控：信用为零则关 Gate，每笔握手消耗一个信用 | 是（信用累加器） |

辅助模块（被上述模块实例化，本讲只作引用）：`Pipeline_Skid_Buffer`、`Arbiter_Priority`、`Arbiter_Round_Robin`、`Multiplexer_One_Hot`、`Demultiplexer_One_Hot`、`Annuller`、`Accumulator_Binary_Saturating`。

> 说明：规格里列出的合流模块是 Priority 与 Round-Robin，但「三类 Merge」还包含 `Pipeline_Merge_One_Hot`（仓库中确有此文件），它最能揭示 Merge 的本质——「选哪一路」可以是外部给定的，也可以是内部仲裁出来的。本讲把它一并讲透。

## 4. 核心概念与源码讲解

### 4.1 Merge 合流（One-Hot / Priority / Round-Robin）

#### 4.1.1 概念说明

Merge 解决的问题是：**N 路数据流要轮流汇入一条输出**。例如多个 DMA 通道抢一条总线、多个生产者往一个 FIFO 里写、多路传感器数据复用一条处理通路。同一时刻输出只能容纳一笔数据，所以必须**逐一**放行，不能像 Join 那样把 N 路拼成一个宽字。

这就引出 Merge 与 Join（u12-l2）的根本区别：

| 维度 | Join（会合） | Merge（仲裁） |
| --- | --- | --- |
| 放行条件 | **所有**输入到齐（AND 归约） | **任一**输入有效即可（OR/优先/轮询） |
| 输出宽度 | N × WORD_WIDTH（各路拼接） | WORD_WIDTH（与单路相同） |
| 各路关系 | 对等、必须同时 | 按仲裁顺序先后 |
| 典型场景 | 并行通道结果拼成完整包 | 多路争用单一资源 |

那么「这一拍放哪一路？」Merge 给出三种回答：

- **One-Hot**：由**外部**给的独热 `selector` 决定。最通用——选路权交给调用方，模块自身不做任何仲裁决策。
- **Priority**：由内部 `Arbiter_Priority` 决定，最低位（bit 0）优先级最高，授权保持到请求释放。固定优先级，简单快但**不公平**（高频高位请求会饿死低位）。
- **Round-Robin**：由内部 `Arbiter_Round_Robin` 决定，等优先级轮询，按各路活动比例公平分配。代价是切换到不同输入时有**一拍空拍**（dead cycle）。

三者都建立在 u4-l1「构建块库」思想之上：不重写握手与选路逻辑，而是复用 `Skid Buffer` + 仲裁器 + 独热 mux/demux。

#### 4.1.2 核心流程

Priority 与 Round-Robin 的内部结构几乎一致，可抽象成五步（设当前被授权路为独热向量 \(g\)，N 路缓冲后的 valid 为 \(v_b\)）：

\[
g = \mathrm{Arbiter}(v_b) \quad \text{（独热 grant，仅一位置 1）}
\]

\[
\text{output\_valid} = (g \neq 0) \quad \text{（有任一路被授权即输出有效）}
\]

\[
\text{output\_data} = \mathrm{MuxOneHot}(g,\ \text{input\_data\_buffered}) \quad \text{（选授权路的数据）}
\]

\[
\text{input\_ready} = \mathrm{DemuxOneHot}(g,\ \text{output\_ready}) \quad \text{（把输出就绪回告授权路）}
\]

\[
\text{input\_data\_buffered}, v_b = \mathrm{SkidBuffer}_j(\text{input}_j) \quad \text{（每路先缓冲）}
\]

用人话说：

1. **每路输入先过 Skid Buffer**，切断 valid↔ready 的组合路径（u9-l2 的工程化落地，与 Join 同理）。
2. **仲裁器**把缓冲后的 N 路 valid 转成一个独热 grant——这一拍只放行一路。
3. **output_valid**：只要有任一路被授权就拉高（OR 语义）。
4. **选数据**：用独热 mux 从 N 路数据里挑出被授权那一笔。
5. **回告就绪**：用独热 demux 把下游的 `output_ready` 只送到被授权那一路的 `input_ready`，其余路的 ready 为 0——于是**未被授权的路被反压（backpressure）**，数据冻结在自己的 skid 寄存器里等待轮到自己。

One-Hot 变体的结构略有不同：选路信号是外部 `selector` 而非内部仲裁器，且它把缓冲加在**输出侧**而非输入侧（原因见 4.1.3）。三者的「选路来源」与「缓冲位置」差异是本小节的关键：

| 变体 | 选路来源 | 缓冲位置 | 公平性 |
| --- | --- | --- | --- |
| One-Hot | 外部 `selector` | 输出侧 1 个 skid | 由调用方决定 |
| Priority | `Arbiter_Priority` | 每路输入 1 个 skid | 不公平（固定优先级） |
| Round-Robin | `Arbiter_Round_Robin` | 每路输入 1 个 skid | 公平（轮询） |

#### 4.1.3 源码精读

**先看 Priority 版**，它结构最干净。顶部说明了仲裁语义与「为何要缓冲输入」：

[Pipeline_Merge_Priority.v:9-14](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Merge_Priority.v#L9-L14) —— 仲裁语义：最低位优先级最高，被授权后保持到 valid 释放；释放后若更高优先级有效则转授。反压通过对应 ready 生效。

[Pipeline_Merge_Priority.v:20-30](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Merge_Priority.v#L20-L30) —— 设计约定：接口内 valid↔ready 不得有组合路径，否则两端一接就成环。因此**每路输入都加 Skid Buffer** 切断组合路径，即便冗余也加——「不值得为省一点缓冲去冒仿真/综合出错的风险」（与 Join 的做法一致）。

端口声明很标准：N 路 `input_valid/ready` 各 1 位，`input_data` 拼成 `TOTAL_WIDTH = WORD_WIDTH * INPUT_COUNT` 的宽向量，输出是单路 `WORD_WIDTH`：

[Pipeline_Merge_Priority.v:34-54](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Merge_Priority.v#L34-L54) —— 端口声明。注意所有参数默认 0（本书约定），未实例化设参则位宽非法、elaboration 吵闹失败。`IMPLEMENTATION` 默认 "AND"，控制内部 Annuller 实现，一般无需改。

第一步，给每路输入套 Skid Buffer，用 `WORD_WIDTH*j +: WORD_WIDTH` 切位段（u5-l1 的 `base +: width` 索引）：

[Pipeline_Merge_Priority.v:70-92](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Merge_Priority.v#L70-L92) —— `generate for` 为每路实例化一个 `Pipeline_Skid_Buffer`，把缓冲后的 valid/ready/data 分别汇成 `input_valid_buffered`、`input_ready_buffered`、`input_data_buffered` 三个宽向量。

第二步，output_valid 直接由「是否有任一路有效」推出（OR 语义，组合的）：

[Pipeline_Merge_Priority.v:94-98](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Merge_Priority.v#L94-L98) —— `output_valid = (input_valid_buffered != INPUT_ZERO)`：只要有任一路缓冲后有效，输出就有效。具体放哪一路由下一步仲裁决定。

第三步，用 `Arbiter_Priority` 把 N 路 valid 压成一个独热 grant。`requests_mask` 接全 1（`INPUT_ONES`）表示不屏蔽任何请求：

[Pipeline_Merge_Priority.v:106-120](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Merge_Priority.v#L106-L120) —— 仲裁器实例。`grant_previous` 端口悬空（`lint_off PINCONNECTEMPTY`），`grant` 即独热授权向量。回顾 u11-l1：Arbiter_Priority 的 grant 是**组合地**由当前 requests 算出，请求一释放就立刻转授，无延迟。

第四、五步，独热 mux 选数据、独热 demux 回告就绪：

[Pipeline_Merge_Priority.v:125-137](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Merge_Priority.v#L125-L137) —— `Multiplexer_One_Hot`（`OPERATION="OR"`）用 grant 作 selector，从 `input_data_buffered` 选出授权路数据送 `output_data`。

[Pipeline_Merge_Priority.v:143-158](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Merge_Priority.v#L143-L158) —— `Demultiplexer_One_Hot`（`BROADCAST=0`、`WORD_WIDTH=1`）用 grant 把 1 位的 `output_ready` 只送到授权路的 `input_ready_buffered`，其余路 ready 为 0（被反压）。

**再看 Round-Robin 版**，整体骨架与 Priority 完全相同，只有两处不同。第一处是顶部强调的「原子性（Atomicity）」：

[Pipeline_Merge_Round_Robin.v:9-19](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Merge_Round_Robin.v#L9-L19) —— 原子性说明：只要某路持续保持 valid（连续数据流），它就持续独占输出（一旦被授权），整块数据**原子地**通过、不被其他路穿插；反压照常经 ready 生效。若该路无法连续供应，则其他路可能在间隙抢走输出——此时应附上源 ID 等元数据以便下游重组。

第二处、也是最关键的实现差异：**Round-Robin 仲裁器换状态需要一拍**，于是多了一步「用原始 valid 屏蔽 grant」。这是 Priority 版没有的：

[Pipeline_Merge_Round_Robin.v:101-111](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Merge_Round_Robin.v#L101-L111) —— `Arbiter_Round_Robin` 实例，输入 `input_valid_buffered`，输出独热 `input_valid_granted`。回顾 u11-l1：轮询仲裁器的 grant 依赖上一拍的 `grant_previous`（有状态），切换需要一拍。

[Pipeline_Merge_Round_Robin.v:113-128](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Merge_Round_Robin.v#L113-L128) —— **关键屏蔽步骤**：`input_valid_granted_masked = input_valid_buffered & input_valid_granted`，再用它推出 `output_valid = (input_valid_granted_masked != INPUT_ZERO)`。注释解释：因为轮询仲裁器换状态要一拍，若不屏蔽，当某路 valid 拉低后，grant 会多滞留一拍，导致**多传一笔错误数据**。用原始 valid 与 grant 相「与」，valid 一落就把 grant 同步清掉。

理解了这一步，Priority 版为何不需要就清楚了：`Arbiter_Priority` 的 grant 是纯组合的、请求一释放立刻转授，不存在滞留，所以直接用 `input_valid_buffered != INPUT_ZERO` 即可。Round-Robin 版的其余部分（数据 mux、ready demux）与 Priority 版逐字相同，只是 selector 用 `input_valid_granted_masked`：

[Pipeline_Merge_Round_Robin.v:133-166](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Merge_Round_Robin.v#L133-L166) —— 独热 mux 选数据（L133-145）、独热 demux 回告就绪（L151-166），结构与 Priority 版一致。

**最后看 One-Hot 版**，它的选路来源是外部 `selector`，缓冲位置也与前两者相反——加在**输出侧**。顶部说明了它在「多位/无位」时的退化行为，非常值得读：

[Pipeline_Merge_One_Hot.v:9-33](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Merge_One_Hot.v#L9-L33) —— 关键说明：正常 selector 每拍仅一位置 1；若小心操作可每拍改 selector 把多路数据穿插（interleave）进输出。**多于一位**置 1 时，多路被选输入做布尔归约（`HANDSHAKE_MERGE` 归约 valid、`DATA_MERGE` 归约数据），若两者都设 "OR" 且保证同时只有一路有效，则退化为「不同步的 Join」（呼应 u12-l2）；**无位**置 1 时，输入全部断开、无法完成输入握手，退化为「多输入的 Pipeline_Gate」（本讲 4.2）。这段把 Merge / Join / Gate 三者统一在一个模块里，是理解它们同源的最佳切入点。

它的实现把「选 valid / 选 data / 回告 ready」拆成三个独立构件，最后在输出加一个 Skid Buffer：

[Pipeline_Merge_One_Hot.v:81-95](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Merge_One_Hot.v#L81-L95) —— `valid_mux`：`Multiplexer_One_Hot`（`WORD_WIDTH=1`、`OPERATION=HANDSHAKE_MERGE`）按 selector 选出被选路的 valid。

[Pipeline_Merge_One_Hot.v:101-113](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Merge_One_Hot.v#L101-L113) —— `data_out_mux`：`Multiplexer_One_Hot`（`OPERATION=DATA_MERGE`）按 selector 选出被选路的数据。

[Pipeline_Merge_One_Hot.v:121-136](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Merge_One_Hot.v#L121-L136) —— `ready_in_demux`：`Demultiplexer_One_Hot`（`BROADCAST=0`）把 `input_ready_buffered` 按 selector 送回被选路的 `input_ready`。

[Pipeline_Merge_One_Hot.v:141-158](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Merge_One_Hot.v#L141-L158) —— 输出侧的 `Pipeline_Skid_Buffer`：把选好的 valid/data 喂进去，输出 valid/data/ready 全部由它驱动。注意注释「output interface is buffered to break the combinational path」——One-Hot 版担心的是**输出侧**的组合路径（selector 组合地影响 valid 与 ready，可能经下游成环），故缓冲输出；而 Priority/RR 担心的是**输入侧**（仲裁器组合地吃 valid 吐 grant 再回 ready），故缓冲输入。缓冲位置不同，但「切断组合路径」的目的是同一个。

> 小结：三类 Merge 共享「独热选路 + mux 取数 + demux 回 ready」的骨架，区别只在「谁产生独热选择信号」（外部 selector vs 内部仲裁器）与「组合路径在哪一侧被切断」（输出缓冲 vs 输入缓冲）。

#### 4.1.4 代码实践

**实践目标**：用 `Pipeline_Merge_Round_Robin` 合流两路数据流，亲手验证「轮询公平放行 + 未授权路被反压」。

**操作步骤**：

1. 打开 `Pipeline_Merge_Round_Robin.v`，确认五步骨架：输入缓冲（L74-95）→ 仲裁（L101-111）→ 屏蔽与 output_valid（L113-128）→ 数据 mux（L133-145）→ ready demux（L151-166）。
2. 思想实验：`INPUT_COUNT = 2`、`WORD_WIDTH = 8`，两路同时持续 `valid=1`，路 0 发 `0x11,0x12,0x13...`，路 1 发 `0x21,0x22,0x23...`，下游一直 `output_ready=1`。
3. 追踪授权向量 `input_valid_granted` 的变化（它来自 u11-l1 的轮询仲裁器）。

**需要观察的现象（按源码逻辑推理，待本地仿真验证）**：

- 两路都有效时，授权在路 0、路 1 之间**轮流**（轮询公平），于是输出数据序列里 `0x11`、`0x21` 交替出现，没有哪一路被长期饿死——这是选 Round-Robin 而非 Priority 的根本理由。
- **未被授权的那一路** `input_ready` 为 0（demux 只把 `output_ready` 送到授权路），于是它的数据被自己的 Skid Buffer 冻结，等轮到自己才放行——这正是「反压」。
- 当某路 valid 拉低后，由于 L121 的屏蔽 `input_valid_buffered & input_valid_granted`，grant 不会多滞留一拍，避免多传一笔错误数据。
- 当仲裁器从授权路 0 切换到路 1 时，会有**一拍空拍**（dead cycle）——这是轮询仲裁器换状态的一拍代价（注释 L7-8 已点明）。

**预期结果**：两路持续有效时，输出按轮询公平交替；切路时偶有空拍。完整时序波形**待本地仿真验证**（可用 `tests/` 下 `Simulation_Clock` + `Synthesis_Harness` 搭测试台，见 u18-l2）。

#### 4.1.5 小练习与答案

**练习 1**：Merge 和 Join 都把多路合成一路，本质区别是什么？为什么 Merge 的输出宽度等于单路、而 Join 的输出更宽？

**答案**：Join 是「会合」（AND 归约），要求**所有**输入到齐才放行，因此可以把各路数据**拼接**成一个更宽的字一次性输出；Merge 是「仲裁」（OR/优先/轮询），同一时刻只放行**任一**路，输出宽度自然等于单路 WORD_WIDTH。Join 是「同步拼接」，Merge 是「逐笔仲裁」。

**练习 2**：Round-Robin 版为什么要做 `input_valid_granted_masked = input_valid_buffered & input_valid_granted` 这一步屏蔽？Priority 版为什么不需要？

**答案**：轮询仲裁器（u11-l1）的 grant 依赖上一拍的 `grant_previous`，是有状态的，换状态需要一拍；若不屏蔽，当某路 valid 拉低后 grant 会多滞留一拍，导致多传一笔错误数据。Priority 版的 grant 是**纯组合**地从当前 requests 算出（请求一释放立刻转授），不存在滞留，所以直接用 `input_valid_buffered != INPUT_ZERO` 即可。

**练习 3**：One-Hot Merge 在「无位置 1」时退化为哪种构件？在「多位置 1 且归约参数为 OR」时又退化为哪种？

**答案**：无位置 1 时，输入全部断开、无法完成任何输入握手，但已在输出的待完成握手仍可完成——这等价于一个「多输入的 `Pipeline_Gate`」（见 4.2）。多位置 1 且 `HANDSHAKE_MERGE`/`DATA_MERGE` 都为 "OR"、且保证同时只有一路有效时，等价于「不同步的 `Pipeline_Join`」（u12-l2）。这说明 Merge 是比 Join/Gate 更一般的构件。

### 4.2 Gate（条件放行）

#### 4.2.1 概念说明

`Pipeline_Gate` 解决的问题是：**按条件阻断一条流水线的握手与数据**。典型用法如顶部所举：往一个 FIFO 里装载一个由多个字组成的包（如一个数据包）时，不希望 FIFO 在装载期间往外送数据，于是用 Gate 暂时堵住输出口。

Gate 看起来只是「`enable=0` 时不让数据过」，难点在于一个反直觉的结论：**只堵 valid 或只堵 ready 都不行，必须同时堵住两者**。这是本小节的核心：

- **只堵 valid（output_valid 强制 0）**：发送方仍能看到接收方的 `ready`，于是它以为握手完成、把这一笔数据丢掉了——**数据丢失**。
- **只堵 ready（input_ready 强制 0）**：接收方仍能看到 `valid`，于是它以为有有效数据、把陈旧或垃圾数据吃进去——**吃进垃圾**。

所以 Gate 必须把 valid 与 ready **一起**门控：`enable=0` 时既不让 valid 下行、也不让 ready 上行，握手彻底无法完成，数据原地质押。

Gate 的另一特性是**纯组合、无缓冲**：它不存任何状态，只是一组门。因此 `enable` 必须与 `clock` 同步变化（注释明确要求），否则组合逻辑里会出现不可控的毛刺。

#### 4.2.2 核心流程

Gate 的语义可写成（\(e\) 为 enable）：

\[
\text{input\_ready} = e \land \text{output\_ready}
\]

\[
\text{output\_valid} = e \land \text{input\_valid}
\]

\[
\text{output\_data} = \begin{cases} \text{input\_data} & (GATE\_DATA = 0,\ \text{始终直通}) \\ (e\,?\ \text{input\_data} : 0) & (GATE\_DATA \neq 0,\ \text{关时清零}) \end{cases}
\]

用人话说：

1. **enable=1**：valid、ready、data 全部直通，Gate 像不存在。
2. **enable=0**：valid 与 ready 同时被强制为 0，握手无法完成；数据是否清零取决于 `GATE_DATA` 参数。

本书的实现巧在一个 `Annuller`（u5-l1）就把活干了：把要门控的信号**拼接成一个字**，统一用一个 `Annuller` 按 `annul = (enable==0)` 清零。`GATE_DATA` 参数决定这个拼接的字里**包不包含数据**：

- `GATE_DATA=0`（默认）：只拼 `{output_ready, input_valid}` 两位，数据另用一根 `assign output_data = input_data` 直通。
- `GATE_DATA!=0`：把数据也拼进去 `{input_data, output_ready, input_valid}`，关 Gate 时数据一并清零（更安全，下游不会看到残留数据）。

#### 4.2.3 源码精读

顶部注释把「必须同时门控 valid 与 ready」讲得最清楚，必读：

[Pipeline_Gate.v:4-16](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Gate.v#L4-L16) —— 核心说明：纯组合无缓冲，`enable` 必须同步于时钟；**只堵 valid 会让发送方误判握手完成而丢数据，只堵 ready 会让接收方吃进陈旧/垃圾数据**；典型用途是装包时临时封住 FIFO 输出口。

`GATE_DATA` 参数的配置说明：

[Pipeline_Gate.v:18-24](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Gate.v#L18-L24) —— `GATE_DATA!=0` 时关 Gate 会把数据也清零；否则数据始终直通，只门控 ready/valid 握手。`IMPLEMENTATION` 控制 Annuller 用 MUX 还是 AND 实现，默认即可。

整个模块体就是一个 `generate if/else`，两个分支分别对应「门控数据」与「不门控数据」。先看门控数据的分支——把数据与两个控制位拼成一个 `WORD_WIDTH+1+1` 位的字，一个 Annuller 统一处理：

[Pipeline_Gate.v:48-62](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Gate.v#L48-L62) —— `GATE_DATA!=0` 分支：`Annuller` 的 `annul = (enable==0)`，`data_in = {input_data, output_ready, input_valid}`，`data_out = {output_data, input_ready, output_valid}`。注意拼接顺序：高位是数据、低两位是 `{output_ready, input_valid}`，输出端对应拆成 `{output_data, input_ready, output_valid}`——同一个 Annuller 同时完成了「数据清零 + ready 门控 + valid 门控」三件事。

再看不门控数据的分支——数据直接 `assign` 直通，Annuller 只管两位控制信号：

[Pipeline_Gate.v:63-79](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Gate.v#L63-L79) —— `GATE_DATA=0` 分支：`assign output_data = input_data`（数据恒直通），`Annuller`（`WORD_WIDTH=1+1`）只门控 `{output_ready, input_valid}` → `{input_ready, output_valid}`。这是更省资源的默认形态：握手被堵住，但数据线上始终是输入的原值（下游因 valid=0 不会取用它）。

把两段合起来理解：Gate 的本质就是「用 Annuller 把 enable 反相后，乘到 valid 与 ready（可选还有 data）上」。`enable=1` 时 `annul=0`，信号原样通过；`enable=0` 时 `annul=1`，相关信号被清零，握手无法成立。这是 u5-l1「Annuller + 选择性清零」哲学的最纯粹应用。

#### 4.2.4 代码实践

**实践目标**：用 `Pipeline_Gate` 实现按使能放行，并亲验「只堵一个信号会出问题」。

**操作步骤**：

1. 打开 `Pipeline_Gate.v`，对比两个分支：门控数据（L48-62）与不门控数据（L63-79），确认两者的差别只在「数据是否进 Annuller」。
2. 思想实验 A（正确用法）：`enable=0` 时，上游 `valid=1, data=D`，下游 `ready=1`。
3. 思想实验 B（错误用法对照）：假设只堵 valid（`output_valid=0` 但 `input_ready=output_ready=1`），同样上游 `valid=1, data=D`，下游 `ready=1`。

**需要观察的现象（按源码逻辑推理）**：

- 实验 A：`enable=0` ⇒ `annul=1` ⇒ `output_valid=0` 且 `input_ready=0`。于是上游看到 `ready=0`（不完成握手、数据 D 不丢失），下游看到 `valid=0`（不吃数据）——数据 D 被原地质押，**安全**。
- 实验 B：`output_valid=0` 但 `input_ready=1` ⇒ 上游见 `valid&&ready` 成立 ⇒ 误以为握手完成 ⇒ **数据 D 被吞掉丢失**。这正是注释 L9-12 警告的情形。
- 把 `enable` 拉回 1：`output_valid` 跟随 `input_valid`、`input_ready` 跟随 `output_ready`，被质押的数据 D 立即放行。

**预期结果**：Gate 正确门控时，`enable=0` 期间既不丢数据也不吃垃圾；只堵一个信号则必出其一。完整波形**待本地仿真验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `Pipeline_Gate` 必须同时门控 valid 与 ready，只门控一个会怎样？

**答案**：只门控 valid（强制 `output_valid=0`）时，发送方仍能看到接收方的 `ready`，误判握手完成而把数据丢掉；只门控 ready（强制 `input_ready=0`）时，接收方仍能看到 `valid`，把陈旧或垃圾数据吃进。只有同时把两者置 0，握手才彻底无法完成，数据原地质押，既不丢也不吃垃圾。

**练习 2**：`GATE_DATA=0` 与 `GATE_DATA!=0` 两种配置下，`output_data` 的行为有何不同？默认用哪种？

**答案**：`GATE_DATA=0`（默认）时 `output_data = input_data` 恒直通，数据线上始终是输入原值，但因 `output_valid=0` 下游不会取用，更省一个 Annuller；`GATE_DATA!=0` 时关 Gate 会把 `output_data` 也清零，下游连残留数据都看不到，更安全。选哪种取决于下游是否会对「valid=0 但 data 非零」敏感。

**练习 3**：`Pipeline_Gate` 是纯组合的，为什么注释强调 `enable` 必须同步于时钟？

**答案**：因为 Gate 内部全是组合逻辑（Annuller），若 `enable` 异步变化（如来自另一个时钟域或含组合毛刺），门控信号上会出现不可控的毛刺，可能恰好让一次握手在错误时刻完成。要求 `enable` 同步于 `clock`，保证门控在时钟边沿之间稳定。（若 `enable` 来自另一时钟域，应先用 u13 的同步器处理。）

### 4.3 Credit Gate（信用制门控）

#### 4.3.1 概念说明

`Pipeline_Credit_Gate` 在 `Pipeline_Gate` 之上加了一层**信用（credit）制流控**：内部维护一个信用计数器，初始为 0（Gate 关闭）；外部每发一个 `add_credit_pulse` 脉冲就加一个信用；每完成一次输入到输出的握手就消耗一个信用；信用降到 0 时 Gate 自动关闭。

这就是经典的 **credit-based flow control**（信用制流控）：发送方每得到一个信用才能发一笔数据，接收方处理完一笔就发回一个信用（这里用 `add_credit_pulse` 模拟）。它天然实现了「发送方不会超前接收方处理能力」的反压，且信用数就是「允许在途（in-flight）的最大笔数」。

为什么需要它？单纯的 `Pipeline_Gate` 需要**外部**告诉它何时开关（`enable`）；而很多场景下，开关时机本身是「下游还能收几笔」的函数——这正是信用计数器要表达的。把计数器与 Gate 封在一起，外部只需脉冲式地「发信用」，无需自己维护计数与门控逻辑。

`MAX_CREDIT_COUNT` 设定信用上限，加超会**饱和**（不溢出回绕）：饱和时下一拍拉高 `current_credit_count_max`，试图继续加信用则下一拍拉高 `add_credit_fail`。

#### 4.3.2 核心流程

Credit Gate 由三部分组成（\(c\) 为信用计数，\(h\) 为本拍是否完成握手，\(a\) 为本拍是否有加信用脉冲）：

\[
\text{open\_gate} = (c \neq 0) \quad \text{（有信用就开门）}
\]

\[
h = \text{input\_data\_ready} \land \text{input\_data\_valid} \quad \text{（握手完成，u9-l2）}
\]

\[
\Delta c = \begin{cases} +1 & a \land \lnot h \quad \text{（只加信用）} \\ -1 & h \land \lnot a \quad \text{（只消费）} \\ 0 & (a \land h) \lor (\lnot a \land \lnot h) \quad \text{（同时加且消费，互相抵消；或都没有）} \end{cases}
\]

\[
c_{t+1} = \mathrm{saturate}(c_t + \Delta c,\ [0, \text{MAX\_CREDIT\_COUNT}])
\]

用人话说：

1. **Pipeline_Gate** 在核心，`enable = open_gate`：信用非零时放行，为零时关闭。
2. **信用计数器**（一个饱和累加器）持有当前信用数：加信用脉冲 +1、握手完成 -1，并饱和在 `[0, MAX]` 之间。
3. **控制逻辑**用一句巧解同时表达「加、减、不动」：只有当「加」与「减」**恰好发生一个**时才更新计数器；两个同时发生则 +1-1 抵消、不更新；两个都没有也不更新。

关键工程约束：累加器**不能加流水级**（`EXTRA_PIPE_STAGES` 必须为 0），否则加/减信用要花多拍，既需要更多流水逻辑、又把吞吐按流水级数打折扣。

#### 4.3.3 源码精读

顶部说明了信用语义与饱和行为：

[Pipeline_Credit_Gate.v:4-13](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Credit_Gate.v#L4-L13) —— 信用为零则关 Gate；`add_credit_pulse` 每脉冲加一信用、允许多周期连续脉冲；每次完成的输入到输出握手消耗一信用；信用数对外可见（通常驱动另一个 `Pipeline_Gate`）；超过 `MAX_CREDIT_COUNT` 会饱和，饱和与加超分别用 `current_credit_count_max`、`add_credit_fail` 标识。

端口声明里，信用位宽 `CREDIT_WIDTH = clog2(MAX_CREDIT_COUNT) + 1`（+1 是为精确表示 2 的幂，呼应 u8-l2 的位数陷阱）：

[Pipeline_Credit_Gate.v:17-42](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Credit_Gate.v#L17-L42) —— 端口声明。注意它**没有** `enable` 输入——门控完全由内部信用数决定。输入握手是 `input_data_*`，加信用是 `add_credit_pulse`，对外报告 `current_credit_count` 及其 zero/max 标志与 `add_credit_fail`。

第一部分，核心的 Pipeline_Gate，`enable` 由「信用是否非零」组合地决定：

[Pipeline_Credit_Gate.v:49-74](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Credit_Gate.v#L49-L74) —— `open_gate = (current_credit_count_zero == 0)`，把它接成 `Pipeline_Gate`（`GATE_DATA=0`，只门控握手、数据直通）的 `enable`。于是信用非零时 Gate 开、握手可过；信用为零时 Gate 关。

第二部分，饱和累加器作信用计数器。顶部注释强调为何不能加流水级：

[Pipeline_Credit_Gate.v:76-90](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Credit_Gate.v#L76-L90) —— 说明：累加器内不能加流水级，否则加/减信用要花多拍、既需更多流水逻辑又把吞吐按级数打折。实例化 `Accumulator_Binary_Saturating`，`EXTRA_PIPE_STAGES=0`（注释「DO NOT CHANGE」）、上下限为 `MAX_CREDIT_COUNT` 与 0。

[Pipeline_Credit_Gate.v:85-127](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Credit_Gate.v#L85-L127) —— 累加器实例：`increment_value = CREDIT_ONE`（每次 ±1）、`increment_add_sub = credit_incr_decr`（0 加 1 减）、`increment_valid = credit_incr_decr_valid`；`limit_max = MAX_CREDIT_COUNT`、`limit_min = 0`；输出 `accumulated_value = current_credit_count`，`at_limit_min` 即 `current_credit_count_zero`，`over_limit_max` 即 `add_credit_fail`，`at_limit_max` 即 `current_credit_count_max`。饱和语义全由 u8-l1 介绍过的饱和加减法器保证。

第三部分，控制逻辑——本模块最精巧的一小段，用三行组合表达式同时表达「加、减、不动」：

[Pipeline_Credit_Gate.v:129-142](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Credit_Gate.v#L129-L142) —— 控制逻辑。`handshake_done = (input_data_ready && input_data_valid)`（标准 handshake_complete）；`credit_incr_decr` 默认取「握手完成则减、否则加」，再用 `add_credit_pulse` 覆盖为「加」；最关键的是 `credit_incr_decr_valid = (handshake_done != add_credit_pulse)`——**只有当握手与加信用恰好发生一个时才更新计数**。若两者同拍发生（+1 与 -1 抵消）或都不发生，则 `valid=0`，计数器不动。

这段逻辑的妙处在于：它没有用 `if/else` 罗列四种情形，而是用一个 XOR（`!=`）一句话表达「加与减是否互相抵消」——这正是 u3-l1「布尔式写成等式比较」「链式三元不嵌套」的典范。因为 Pipeline_Gate 无缓冲（组合的），输入握手完成与否可以直接组合地观测到，无需额外打拍。

#### 4.3.4 代码实践

**实践目标**：用信用计数器控制放行，验证「信用为零即关 Gate、加信用即开门、握手即消耗」。

**操作步骤**：

1. 打开 `Pipeline_Credit_Gate.v`，确认三部分：Pipeline_Gate（L57-74）、累加器（L85-127）、控制逻辑（L129-142）。
2. 思想实验：初始 `current_credit_count=0`（Gate 关），上游 `valid=1, data=D`，下游 `ready=1`，随后发 2 个 `add_credit_pulse`。

**需要观察的现象（按源码逻辑推理，待本地仿真验证）**：

- 初始信用为 0 ⇒ `current_credit_count_zero=1` ⇒ `open_gate=0` ⇒ Gate 关 ⇒ `input_data_ready=0`，上游握手无法完成，数据 D 被质押（不丢失）。
- 第 1 个 `add_credit_pulse`：`credit_incr_decr_valid = (handshake_done != add_credit_pulse) = (0 != 1) = 1`，加法 ⇒ 信用变 1 ⇒ `current_credit_count_zero=0` ⇒ `open_gate=1` ⇒ Gate 开。
- Gate 一开，上游握手完成：`handshake_done=1` ⇒ `credit_incr_decr_valid = (1 != 0) = 1`，减法 ⇒ 信用回到 0 ⇒ Gate 又关。**每完成一笔握手恰好消耗一个信用**。
- 若某一拍 `add_credit_pulse` 与 `handshake_done` **同时**发生：`credit_incr_decr_valid = (1 != 1) = 0` ⇒ 计数器不更新（+1 与 -1 抵消）——这是 L141 巧解的直接体现。
- 持续发信用到 `MAX_CREDIT_COUNT` 后再发：`add_credit_fail` 下一拍拉高（饱和，不溢出）。

**预期结果**：信用为零则关 Gate、上游被反压；每笔握手消耗一信用；加信用脉冲开门；饱和不溢出。完整时序波形**待本地仿真验证**。

#### 4.3.5 小练习与答案

**练习 1**：`credit_incr_decr_valid = (handshake_done != add_credit_pulse)` 这一行为什么能同时表达「加、减、不动」三种情况？

**答案**：`handshake_done` 为真表示要 -1，`add_credit_pulse` 为真表示要 +1。用 `!=`（XOR）判断两者是否恰好发生一个：只有一个发生时 `valid=1`，按当前 `credit_incr_decr`（加或减）更新；两个同时发生时 +1 与 -1 抵消、`valid=0` 不更新；两个都没有时 `valid=0` 也不更新。一句话覆盖三种情形，是 u3-l1「等式比较 + 链式三元」的典范。

**练习 2**：为什么累加器的 `EXTRA_PIPE_STAGES` 必须为 0，不能加流水级来修时序？

**答案**：因为信用计数器处在握手的关键反馈环上（信用决定 Gate 开关、Gate 决定握手、握手又改信用）。若给累加器加流水级，加/减信用就要花多拍才能反映到 `current_credit_count`，这既需要额外处理「done」的延迟、又会让握手吞吐按流水级数打折（注释明言「divides the throughput by the number of pipe stages」）。所以宁可让进位链长一点，也不加级。

**练习 3**：`Pipeline_Credit_Gate` 与 `Pipeline_Gate` 的关系是什么？为什么说 Credit Gate 把「开关时机」内化了？

**答案**：Credit Gate 内部实例化了一个 `Pipeline_Gate`，把它当核心，只是把 `enable` 从「外部输入」改成「内部信用数非零」的组合输出。于是「何时开关」不再需要外部逻辑决定，而是由信用计数器自动表达「下游还能收几笔」——把原本属于外部控制流的开关决策内化成了模块内部状态。这正是把简单构件（Gate）组合成有语义构件（信用流控）的构建块库范本。

## 5. 综合实践

把 Merge 与 Gate 串起来，搭一个「两路数据合流，按使能/信用放行」的小系统。这是 Merge/Gate 最常见的搭配：多路数据先合流成一路，再用一个 Gate 控制整体是否对外输出（例如装包期间封住输出口）。

**任务**：用 `Pipeline_Merge_Round_Robin`（INPUT_COUNT=2）合流两路 8 位数据流，再用 `Pipeline_Gate`（GATE_DATA=0）按 `enable` 控制合流结果是否送往下游，数据位宽 `WORD_WIDTH = 8`。

下面是**示例代码**（不在仓库中，仅作骨架演示，需自行补全时钟/复位与上游驱动后仿真）：

```verilog
// 示例代码：两路 Merge -> Gate -> 下游
// 假设已有 clock / clear，WORD_WIDTH = 8

// ---- 两路上游 ----
wire [1:0]  in_valid;   // 两路各自的 valid
wire [1:0]  in_ready;   // 两路各自的 ready
wire [15:0] in_data;    // 2 * 8 = 16 bit：[7:0]=路0，[15:8]=路1

// ---- Merge 输出（单路 8 位）----
wire        merged_valid;
wire        merged_ready;
wire [7:0]  merged_data;

Pipeline_Merge_Round_Robin #(.WORD_WIDTH(8), .INPUT_COUNT(2)) merge_inst (
    .clock(clock), .clear(clear),
    .input_valid (in_valid),  .input_ready (in_ready),  .input_data (in_data),
    .output_valid(merged_valid), .output_ready(merged_ready), .output_data(merged_data)
);

// ---- Gate：按 enable 放行 ----
wire        enable;       // 例：装包期间拉 0，平时拉 1
wire        out_valid;
wire        out_ready;    // 下游就绪
wire [7:0]  out_data;

Pipeline_Gate #(.WORD_WIDTH(8), .GATE_DATA(0)) gate_inst (
    .enable      (enable),
    .input_valid (merged_valid), .input_ready (merged_ready), .input_data (merged_data),
    .output_valid(out_valid),    .output_ready(out_ready),    .output_data(out_data)
);
```

**请完成并思考**：

1. **接线自洽**：Merge 的 `output_ready` 由 Gate 的 `input_ready` 驱动，Gate 的 `input_valid` 是 Merge 的 `output_valid`——确认数据通路闭合：`两路 → Merge → Gate → 下游`。
2. **跟踪两路数据**：设两路同时持续 `valid=1`，路 0 发 `0x11,0x12,...`，路 1 发 `0x21,0x22,...`，`enable=1`、下游 `out_ready=1`。按源码推理：Merge 轮询放行，`merged_data` 交替出现 `0x11`、`0x21`、`0x12`、`0x22`...（切路时偶有空拍）；Gate 直通，`out_data` 跟随 `merged_data`。
3. **验证 Gate 的门控**：在装包期间把 `enable` 拉低 3 拍。此时 Gate 同时把 `out_valid` 与 `merged_ready` 置 0——于是下游看到 `valid=0`（不吃数据），Merge 看到 `ready=0`（合流暂停、两路被反压）。`enable` 拉回 1 后，被质押的数据立即恢复流出。确认整个过程**既不丢数据也不吃垃圾**。
4. **进阶**：把 `Pipeline_Gate` 换成 `Pipeline_Credit_Gate`，用 `add_credit_pulse` 控制放行数量。思考：若每发一个信用才允许合流输出一笔，两路数据会以什么节奏通过？这与单纯 `enable` 的「全开/全关」有何不同？（提示：信用制把「在途笔数」精确化。）

**预期结果**：`enable=1` 时两路按轮询公平合流输出；`enable=0` 时整条通路被冻结、数据不丢失。完整时序波形**待本地仿真验证**。

## 6. 本讲小结

- Merge 是「**仲裁合流**」：多路输入里任一路有效即可输出，由仲裁器/选择器决定这一拍放谁过；输出宽度等于单路 WORD_WIDTH（不拼接）。这与 Join 的「会合拼接」（AND 归约、全部到齐、输出更宽）形成根本对比。
- 三类 Merge 共享「独热选路 + mux 取数 + demux 回 ready」骨架，区别在选路来源：**One-Hot** 用外部 selector（缓冲输出侧），**Priority** 用 `Arbiter_Priority`（固定优先、缓冲输入侧），**Round-Robin** 用 `Arbiter_Round_Robin`（公平轮询、缓冲输入侧、切换有一拍空拍）。
- Round-Robin 版多了一步 `input_valid_granted_masked = input_valid_buffered & input_valid_granted`，因为轮询仲裁器换状态要一拍，不屏蔽会多传一笔错误数据；Priority 版的 grant 是纯组合的、不需要屏蔽。
- Merge 复用仲裁器（u11-l1）与独热 mux/demux（u5-l2），是「构建块库」自底向上拼装的范本：Merge 本身不写选路逻辑，只做连线。
- `Pipeline_Gate` 是纯组合的条件门控，核心结论是**必须同时门控 valid 与 ready**——只堵一个会丢数据或吃垃圾；它用一个 `Annuller` 把控制位（可选连同数据）拼接成一字统一清零，是 u5-l1 Annuller 哲学的最纯应用。
- `Pipeline_Credit_Gate` 在 Gate 之上加信用计数器（饱和累加器），把「开关时机」从外部 `enable` 内化为「信用是否为零」；其增减信用用一句 `credit_incr_decr_valid = (handshake_done != add_credit_pulse)` 同时表达加、减、不动，是组合逻辑的巧解；累加器不能加流水级，否则吞吐按级数打折。

## 7. 下一步学习建议

- 下一单元进入 **u13（时钟域穿越 CDC 理论与基础同步）**。建议带着本讲的两个问题去读：其一，`Pipeline_Gate` 注释要求 `enable` 同步于时钟——若 `enable` 或 `add_credit_pulse` 来自另一个时钟域该怎么办？（答案在 u13-1/u13-2 的同步器与标志同步。）其二，要把本讲的 Merge/Gate 用到跨时钟域，握手信号如何穿越？（答案在 u14 的 CDC_Word_Synchronizer 与 CDC_FIFO。）
- 回看 **u12-l2（Fork/Join/Branch）**，把本讲的 Merge（OR/仲裁）与 Join（AND/会合）对照巩固：同是「多进一出」，策略截然不同。注意 One-Hot Merge 在多位/无位时正好退化成 Join/Gate，三者同源。
- 回看 **u11-l1（优先编码与仲裁器）**，确认 Priority/RR 仲裁器的 grant 语义（保持至释放、轮询公平、饥饿与空拍），这是 Merge 选路行为的根。
- 想亲手仿真本讲模块，先学 **u18-l2（仿真、测试台与综合验证）**：用 `Simulation_Clock` 与 `Synthesis_Harness` 搭测试台，驱动两路 `input_valid` 与 `enable`/`add_credit_pulse`，观察轮询合流、反压、信用开门的波形。
- 继续阅读源码：`Pipeline_Merge_One_Hot_Lazy.v`（One-Hot Merge 的纯组合对偶，不加输出缓冲，风险与 Fork_Lazy 类似）、`Pipeline_Credit_Buffer.v`（u12-l1 讲过，与本讲 Credit Gate 的信用思想对照——前者用信用切长路径并平滑，后者用信用做门控）。
