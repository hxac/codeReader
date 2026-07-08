# Fork/Join/Branch 分流与合流

## 1. 本讲目标

本讲承接 u10-l1（Skid Buffer 与 COTTC FSM），把 ready/valid 握手从「点对点」推广到「一对多、多对一、按地址选路」。

学完本讲，你应该能够：

1. 说清 Fork 家族三个变体（Lazy / Eager / Blocking）各自的语义、缓冲位置与适用场景。
2. 解释「为什么 Fork_Blocking 会阻塞上游」——即所有输出必须「同时」握手这一约束带来的代价与死锁风险。
3. 理解 Join 的会合（rendez-vous）语义：N 路输入必须全部到齐，才能合成一路更宽的输出。
4. 读懂 Branch_One_Hot 如何用「一个 mux + 两个 demux」把一路数据按独热地址送到 N 路之一。
5. 用 Fork + Join 拼出一个「一分为二、各自处理、再合二为一」的并行流水线骨架。

## 2. 前置知识

阅读本讲前，请确认你已掌握以下概念（来自前置讲义）：

- **ready/valid 握手**（u9-l1）：source 驱动 `valid`/`data` 指向 destination，destination 驱动 `ready` 指回 source；同一拍 `valid && ready` 即握手完成。接口内**不得有组合环路**。
- **handshake_complete 门控**（u9-l2）：凡是影响接口的内部状态，只能在握手完成的那一拍改变，标准式 \(\text{handshake\_complete} = (\text{ready} \land \text{valid})\)。
- **会合（rendez-vous）**（u11-l2）：相关方各自拉高 OK 信号，在同一拍同时见到 `OK_IN && OK_OUT` 才一起前进。Muller C 元素是其硬件原语。
- **Skid Buffer**（u10-l1）：用一个缓冲寄存器切断 ready/valid 接口的组合路径，用一拍延迟换掉输入到输出的组合直通；本书用它给「纯组合」的握手模块加缓冲。

本讲的关键反差是：**Fork 和 Join 用「与（AND）归约」实现会合（全部到齐才走），Branch 用「或（OR）归约」实现非会合（谁先就绪谁先走）**。记住这条，三个模块就贯通了。

## 3. 本讲源码地图

| 文件 | 作用 | 是否含状态 |
| --- | --- | --- |
| `Pipeline_Fork_Lazy.v` | Fork 家族的「叶子」：纯组合，把一路握手复制成 N 路同步握手 | 否（纯组合） |
| `Pipeline_Fork_Eager.v` | Lazy Fork + 每路输出各加一个 Skid Buffer，各路独立完成 | 是（每路一个 skid 寄存器） |
| `Pipeline_Fork_Blocking.v` | Lazy Fork + 输入侧一个 Skid Buffer，仍要求各路同时完成 | 是（输入一个 skid 寄存器） |
| `Pipeline_Join.v` | Fork 的对偶：N 路输入先各加 Skid Buffer，全部到齐后合成一路更宽输出 | 是（每路一个 skid 寄存器） |
| `Pipeline_Branch_One_Hot.v` | 按独热 selector 把一路数据送到 N 路之一（一个 mux + 两个 demux） | 否（纯组合） |

辅助模块（被上述模块实例化，本讲只作引用）：`Pipeline_Skid_Buffer`、`Multiplexer_One_Hot`、`Demultiplexer_One_Hot`。

## 4. 核心概念与源码讲解

### 4.1 Fork 变体（Lazy / Eager / Blocking）

#### 4.1.1 概念说明

Fork 解决的问题是：**一份输入数据，要原样发给 N 个下游**。例如一个控制信号要同时广播给多个处理引擎，或一路视频数据要分给显示与录制两条通路。

Fork 看起来只是「把 data 复制 N 份」，难点全在握手语义上：**输入的这一次事务（transaction），到底什么时候算完成？** 本书给出三种回答，对应三个变体：

- **Lazy（懒惰）**：所有 N 路输出必须**在同一拍同时**完成握手，输入才算完成。完全锁步（lockstep），无任何缓冲，纯组合。
- **Eager（急切）**：每路输出各自独立完成（可以先后、可以乱序），但输入仍要等**所有**输出都完成后才进入下一笔。给每路输出加 Skid Buffer。
- **Blocking（阻塞）**：仍要求各路**同时**完成（同 Lazy），但在输入侧加一个 Skid Buffer 切断组合路径。

这三者都建立在同一个「叶子」`Pipeline_Fork_Lazy` 之上：Eager = Lazy + N 个输出缓冲；Blocking = Lazy + 1 个输入缓冲。这本身就是 u4-l1「构建块库」自底向上复用的范例——不重写握手逻辑，只在外面包缓冲。

#### 4.1.2 核心流程

Lazy Fork 的全部语义可以浓缩成三句布尔等式（\(\bigwedge\) 表示「全部为 1」即 AND 归约）：

\[
\text{input\_ready} = \bigwedge_{j=0}^{N-1} \text{output\_ready}_j
\]

\[
\text{output\_valid}_j = \text{input\_valid} \land \text{input\_ready} \quad (\text{对所有 } j \text{ 相同})
\]

\[
\text{output\_data}_j = \text{input\_data} \quad (\text{对所有 } j \text{ 相同})
\]

用人话说：

1. **输入就绪 ⟸ 所有输出都就绪**。只要有一路下游还没准备好（`ready=0`），输入就不能收下一笔。这是「与归约会合」。
2. **每路输出有效 ⟸ 输入握手完成**。只有当 `input_valid` 与 `input_ready` 同高（即握手完成）的那一拍，所有 N 路输出才同时拉高 `valid`。
3. **数据原样广播**：每一路都拿到完全相同的 `input_data` 副本。

于是必然出现「全有或全无」的效果：要么 N 路输出**同一拍**全部完成握手，要么一笔都完成不了。这正是 Lazy/Blocking 的「锁步」本质。

**三种变体的缓冲差异**（决定何时该用哪个）：

| 变体 | 缓冲位置 | 输出完成方式 | 输入何时前进 | 主要风险/代价 |
| --- | --- | --- | --- | --- |
| Lazy | 无 | 同时（同拍） | 所有输出同拍就绪 | 纯组合，可能与下游成环 |
| Blocking | 输入侧 1 个 skid | 同时（同拍） | 所有输出同拍就绪 | 慢者拖全局；ready 可撤销则死锁 |
| Eager | 输出侧 N 个 skid | 独立（先后/乱序） | 所有输出各自完成 | 不再锁步，下游可跑在前 |

#### 4.1.3 源码精读

先看叶子 `Pipeline_Fork_Lazy`。它的端口很标准：一路输入握手，N 路输出握手（`OUTPUT_COUNT` 个），数据按 `[TOTAL_WIDTH-1:0]` 拼成宽向量：

[Pipeline_Fork_Lazy.v:15-31](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Fork_Lazy.v#L15-L31) —— 模块端口声明。注意 `OUTPUT_COUNT` 默认 0（本书约定），未实例化设参则 `TOTAL_WIDTH` 退化为非法位宽，elaboration 阶段吵闹失败。

核心组合逻辑只有两个 `always @(*)` 块，正好对应上面三句等式：

[Pipeline_Fork_Lazy.v:48-56](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Fork_Lazy.v#L48-L56) —— 全部语义所在。第一块给出 `input_ready = (output_ready == OUTPUT_ONES)`（与归约）、`output_valid = {OUTPUT_COUNT{output_valid_gated}}`（广播同一 valid）、`output_data = {OUTPUT_COUNT{input_data}}`（广播同一数据）；第二块定义 `output_valid_gated = (input_valid && input_ready)`，即 handshake_complete（u9-l2）。

这里有两点值得对照前置讲义：

- `output_ready == OUTPUT_ONES` 是 N 位的**全等比较**而非按位与，等价于 AND 归约，且能正确处理 X 值（全 1 才成立）。
- `output_valid_gated` 用了一个独立的 `reg` 中间量分两块写，是 u3-l1「把复杂逻辑拆成几行中间变量」的典型写法，也避免了组合块内向后依赖。

注意 Lazy 内部**没有**组合环：`valid` 只往下游流（`input_valid → output_valid`），`ready` 只往上游流（`output_ready → input_ready`），两者方向相反。组合环的风险来自**外部**：若下游模块存在 `valid → ready` 的组合路径（u9-l2 明确禁止），接上 Lazy 后就形成 `Fork.output_valid → 下游 → 下游.ready → Fork.output_ready → Fork.input_ready → Fork.output_valid` 的闭环。这就是注释反复警告「小心组合路径」的原因。

再看 Blocking，它的实现就是「Skid Buffer + Lazy Fork」两层：

[Pipeline_Fork_Blocking.v:49-82](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Fork_Blocking.v#L49-L82) —— 先用一个 `Pipeline_Skid_Buffer` 缓冲输入（L49-66），再把缓冲后的输入喂给 `Pipeline_Fork_Lazy`（L68-82）。输出侧不再加缓冲，所以各路仍要同时完成。

Blocking 顶部那段 NOTE 是理解「阻塞」的关键，必须细读：

[Pipeline_Fork_Blocking.v:10-16](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Fork_Blocking.v#L10-L16) —— 警告：若下游可以先拉高 `ready`、又在对应的 `valid` 到来前把 `ready` 撤回，就会出现「持续时间未知」的死锁，直到所有 `ready` 恰好同时拉高。解决办法是改用 Eager Fork（但代价是失去锁步保证）。

最后看 Eager 的对照实现，缓冲位置正好相反——包在每路**输出**上：

[Pipeline_Fork_Eager.v:41-79](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Fork_Eager.v#L41-L79) —— 先实例化一个 Lazy Fork 产生 N 路未缓冲输出（L41-55），再用 `generate for` 给每路输出各接一个 `Pipeline_Skid_Buffer`（L57-79）。于是每路可以各自握手完成、互不等待。

#### 4.1.4 代码实践

**实践目标**：亲手验证「Fork_Blocking 为何可能阻塞上游」，并体会三种缓冲选择的差异。

**操作步骤**：

1. 打开 `Pipeline_Fork_Lazy.v`，定位 L48-56 的两个组合块，确认 `input_ready` 只依赖 `output_ready`、`output_valid` 只依赖 `input_valid` 与 `input_ready`。
2. 打开 `Pipeline_Fork_Blocking.v`，确认它只是「Skid Buffer（输入） + Lazy Fork」两层组合，输出侧无缓冲。
3. 构造一个思想实验：`OUTPUT_COUNT = 3`，三路下游的 `ready` 在某拍分别为 `1, 1, 0`。

**需要观察的现象（按源码逻辑推理，待本地仿真验证）**：

- 第 3 路 `ready=0` ⇒ `output_ready != OUTPUT_ONES` ⇒ `input_ready = 0`。于是即便上游有 `valid`，这一笔也**无法完成**，输入被阻塞。
- 把第 3 路 `ready` 改回 `1`，三路同时为 `1` 的那一拍，`input_ready` 才拉高，三路 `output_valid` 同拍拉高，**一笔事务在三个输出上同拍完成**。
- 进一步：假设第 1 路下游「先拉高 ready 又在 valid 到来前撤回」（注释警告的情形），那么三路 `ready` 很难在同一拍凑齐，吞吐会塌陷甚至长期死锁。

**预期结果**：Blocking 模式下，**最慢的那一路决定整体吞吐**；若存在 ready 撤回，可能死锁。这正是「阻塞」二字的由来——也是工程上需要 Eager 的理由。

> 说明：上述现象是依据源码逻辑的推理结论；若要在仿真器里亲见，可用 `tests/` 下 `Simulation_Clock` + `Synthesis_Harness`（见 u18-l2）搭一个最小测试台驱动 `output_ready`，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：Lazy Fork 内部有没有组合环？为什么注释还反复警告组合路径？

**答案**：模块内部没有组合环——`valid` 向下游流、`ready` 向上游流，方向相反不闭环。警告针对的是**外部连接**：若下游模块存在 `valid → ready` 的组合路径，与 Lazy 的 `ready` 上游路径拼接就会形成闭环。这正是 Blocking（输入加缓冲）/ Eager（输出加缓冲）存在的理由。

**练习 2**：把 Lazy Fork 包成 Eager 时，缓冲加在输出侧；包成 Blocking 时，缓冲加在输入侧。这一「位置」差别如何改变下游各路的行为？

**答案**：Eager 的每路输出都有自己的 skid 寄存器，所以每路可以**独立、乱序**完成握手，快路不必等慢路，下游可以跑在前面。Blocking 的输出侧无缓冲，所有路仍须**同拍**完成，输入缓冲只用来切断输入侧组合路径；因此慢路照样拖住全局，这也是它叫「阻塞」的原因。

**练习 3**：`output_valid = {OUTPUT_COUNT{output_valid_gated}}` 为什么要用复制 `{N{...}}` 而不是直接写 `output_valid = output_valid_gated`？

**答案**：`output_valid` 是 `OUTPUT_COUNT` 位宽的向量（每路一个 valid），而 `output_valid_gated` 是 1 位标量。`{OUTPUT_COUNT{...}}` 把这个标量复制 N 份填满向量，是 u2-l2「用复制构造定宽常量」惯用法的直接应用，保证位宽严格匹配。

### 4.2 Join 会合合流

#### 4.2.1 概念说明

Join 是 Fork 的**对偶**：Fork 是「一进多出、复制」，Join 是「多进一出、拼接」。它把 N 路独立的 ready/valid 输入，在「全部到齐」后合成**一路更宽**的 ready/valid 输出（输出数据是 N 路数据的拼接）。

典型用途：多个并行计算通道各自产出结果，必须等**所有**通道都算完，才能把拼好的完整结果送往下游。这就是 u11-l2 讲过的「会合（rendez-vous）」——只是从 Muller C 元素那种「双方对等前进」抬到了「N 路数据流」层面。

#### 4.2.2 核心流程

Join 的语义同样可浓缩成两句布尔等式（与 Fork 方向相反）：

\[
\text{output\_valid} = \bigwedge_{j=0}^{N-1} \text{input\_valid}_j
\]

\[
\text{input\_ready}_j = \text{output\_valid} \land \text{output\_ready} \quad (\text{对所有 } j \text{ 相同})
\]

\[
\text{output\_data} = \{\text{input\_data}_{N-1}, \ldots, \text{input\_data}_0\}
\]

用人话说：

1. **输出有效 ⟸ 所有输入都有效**。N 路输入的 `valid` 做 AND 归约，少一路都不行（会合）。
2. **每路输入就绪 ⟸ 输出握手完成**。只有当输出本身 `valid` 且下游 `ready` 时，才回告所有输入「可以收下一笔」；否则**所有**输入的 `ready` 一律拉低，谁也别想前进。这保证「要么一起完成，要么都不完成」，不会出现某路偷跑、数据丢失。
3. **数据拼接**：N 路输入数据首尾相接，拼成 `TOTAL_WIDTH = N × WORD_WIDTH` 的宽输出。

注意 Join 与 Fork 的对称美：Fork 在输出侧做 AND 归约（所有输出就绪输入才就绪），Join 在输入侧做 AND 归约（所有输入有效输出才有效）。两者都是「会合」，方向相反。

#### 4.2.3 源码精读

Join 顶部有一段关于「避免组合环」的设计约定，值得先读：

[Pipeline_Join.v:9-19](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Join.v#L9-L19) —— 说明：接口内 valid 与 ready 之间不得有组合路径，否则两端一接就成环、无法做时序分析也无法可靠仿真。因此 Join **主动**给每路输入都加 Skid Buffer 切断组合路径，即便看起来冗余也要加——「不值得为省一点缓冲去冒仿真/综合出错的风险」。

这正是本书的工程哲学：宁可多花几个寄存器，也要把组合环的隐患在构件内部堵死。实现上是一个 `generate for`：

[Pipeline_Join.v:60-82](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Join.v#L60-L82) —— 为每路输入实例化一个 `Pipeline_Skid_Buffer`，用 `WORD_WIDTH*j +: WORD_WIDTH` 切位段（u5-l1 介绍过的 `base +: width` 索引）把宽向量里的第 j 路数据喂给第 j 个缓冲。

核心组合逻辑同样只有两块，对应上面的等式：

[Pipeline_Join.v:87-94](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Join.v#L87-L94) —— 第一块：`output_valid = (input_valid_buffered == INPUT_ONES)`（AND 归约会合），`output_data = input_data_buffered`（直接拼接，因为缓冲后的数据本就是按路拼好的宽向量）。第二块：`input_ready_buffered = output_valid ? {INPUT_COUNT{output_ready}} : INPUT_ZERO`——输出握手成立时，所有输入同享一个 `output_ready`；否则全部清零，谁都不前进。

这里有一个 Join 与普通「合并」的关键区别：**所有输入的 ready 永远同时为 0 或同时为 output_ready**，绝不会出现「某路先放行」。这就保证了 N 路数据在同一拍一起被消费、不会错位。

> 补充：仓库里还有一个 `Pipeline_Join_Lazy.v`（不加输入缓冲的纯组合版），与 `Pipeline_Fork_Lazy` 对偶，适用场景与风险也类似——可用但需自行确保不构成组合环。本讲以加缓冲的 `Pipeline_Join` 为主。

#### 4.2.4 代码实践

**实践目标**：体会 Join 的「N 路到齐才放行」会合行为。

**操作步骤**：

1. 打开 `Pipeline_Join.v`，定位 L87-90 的 `output_valid` 表达式，确认它是「所有缓冲后的输入 valid 全 1」才成立。
2. 定位 L92-94 的 `input_ready_buffered`，确认它是「要么全 0、要么全等于 output_ready」，不存在某路单独放行。
3. 思想实验：`INPUT_COUNT = 2`，两路输入分别在第 5 拍、第 9 拍才拉高 `valid`，下游一直 `ready=1`。

**需要观察的现象（按源码逻辑推理）**：

- 第 5 拍：只有第 1 路 valid，`input_valid_buffered != INPUT_ONES` ⇒ `output_valid = 0` ⇒ 两路 `ready` 都为 0。第 1 路的数据被自己的 skid 缓冲**冻结**等待。
- 第 9 拍：第 2 路 valid 到达，两路 valid 同拍凑齐 ⇒ `output_valid = 1`，下游 `ready=1` ⇒ 同拍完成；`output_data` 是两路数据的拼接。
- 结论：第 1 路虽早就绪，却必须空等到第 2 路到齐——这正是「会合」的代价，也是 Join 区别于 Merge（下讲 u12-l3）的根本点：Merge 是「谁先到谁先走」，Join 是「全部到齐一起走」。

**预期结果**：输出有效时刻 = max(各路 valid 到达时刻)；早到的那路被缓冲挂起。完整波形**待本地仿真验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 Join 要给**每路输入**都加 Skid Buffer，哪怕看起来多余？

**答案**：为了从构件内部就切断 valid↔ready 的组合路径。如果某路上游存在 `ready → valid` 的组合反馈，与 Join 的 `output_valid → input_ready` 路径拼接就会成环。统一加缓冲把这个隐患在构件内部堵死，是 u9-l2「接口内不得有组合环」的工程化落地。

**练习 2**：Join 的 `input_ready_buffered` 为什么是「全 0 或全等于 output_ready」，而不是各路独立？

**答案**：因为 Join 是会合——所有路必须在同一拍一起完成。若各路 ready 独立、某路先放行，就会把那路数据消费掉而其余路还没到齐，导致拼接错位、数据丢失。统一门控保证「要么一起完成、要么都不完成」。

**练习 3**：Join 和下讲的 Merge（u12-l3）都把多路合成一路，本质区别是什么？

**答案**：Join 要求**所有**输入到齐（AND 归约、会合），输出数据是各路**拼接**成更宽的字，各路地位对等且必须同时；Merge 是**仲裁**多路请求，**任一**路有效即可输出（OR/优先/轮询），输出宽度与单路相同，各路按仲裁顺序先后通过。Join 是「同步拼接」，Merge 是「异步仲裁」。

### 4.3 Branch 分流（One-Hot）

#### 4.3.1 概念说明

Fork 是「一份发给所有」，Branch 是「一份发给其中之一」。Branch 接收一路输入握手，按一个**独热**（one-hot）selector 把它送到 N 路输出中的某一路（需要二进制地址时，先用 `Binary_to_One_Hot` 转独热，见 u5-l2）。

典型用途：按地址把数据分发到不同的存储体、不同的处理引擎、或不同的缓存通道。相比 Fork 的「广播」，Branch 是「选路」。

Branch 的关键反差在于归约方式：Fork/Join 用 **AND**（会合、全部到齐），Branch 用 **OR**（非会合、谁先就绪谁先走）。

#### 4.3.2 核心流程

Branch_One_Hot 由三件已学过的组合构件拼成，分工明确：

\[
\text{input\_ready} = \bigvee_{j=0}^{N-1}(\text{selector}_j \land \text{output\_ready}_j)
\]

\[
\text{output\_valid}_j = \text{selector}_j \land \text{input\_valid}
\]

\[
\text{output\_data}_j = \text{selector}_j \;\? \text{input\_data} : 0
\]

用人话说：

1. **输入就绪 ⟸ 被选中的输出里至少有一个就绪**（OR 归约）。用一个 `Multiplexer_One_Hot`（OPERATION="OR"）把被选中各路的 `ready`「或」起来，回告输入。
2. **输出有效 ⟸ 解复用**：用一个 `Demultiplexer_One_Hot` 把 `input_valid` 按 selector 分发——被选中的那路 valid 跟随输入，其余路为 0。
3. **输出数据 ⟸ 解复用**：再用一个 `Demultiplexer_One_Hot` 把 `input_data` 按 selector 分发——被选中的那路拿到数据，其余路清零。

三条信号（ready / valid / data）各用一件构件，是 u4-l1「数据/控制/接口分离」「把无关连接移入子模块」的范本。

**独热约束与多选行为**：正常情况下 selector 每拍只有一位为 1。但源码注释指出，若多于一位为 1，Branch 会表现得更像一个「不同步的 Eager Fork」——所有被选中的输出都拿到 valid 和数据副本，而 `input_ready` 取它们 ready 的 OR（先就绪者先得）。若没有一位为 1，则输入与所有输出全部断开，无法完成任何握手。

#### 4.3.3 源码精读

先看顶部对「多选/无选」行为的说明：

[Pipeline_Branch_One_Hot.v:15-29](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Branch_One_Hot.v#L15-L29) —— 说明：selector 正常应稳定保持；小心操作下可每拍改 selector 做 demux（解复用）分发；多位置位时行为如「不同步的 Eager Fork」（OR 归约 ready），无位置位则接口断开、无法握手。`IMPLEMENTATION` 参数默认 "AND"，控制内部 Annuller 实现，一般无需改。

三件构件的实现，第一件是回告就绪的 ready mux：

[Pipeline_Branch_One_Hot.v:56-68](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Branch_One_Hot.v#L56-L68) —— 用 `Multiplexer_One_Hot`（`WORD_WIDTH=1`、`OPERATION="OR"`）把被选中输出的 `ready` 做 OR 归约得到 `input_ready`。这里 OR 是关键——它让 Branch 成为「非会合」的：任何一个被选中的下游就绪，输入就能完成。

第二、三件是分发 valid 与数据的两个 demux，结构相同，只是位宽不同（1 位 vs WORD_WIDTH 位）：

[Pipeline_Branch_One_Hot.v:72-87](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Branch_One_Hot.v#L72-L87) —— `valid_demux`：`Demultiplexer_One_Hot`（`BROADCAST=0`）把 `input_valid` 送到被选中那路的 `output_valid`，其余路为 0；`valids_out` 端口悬空（`lint_off PINCONNECTEMPTY`）。

[Pipeline_Branch_One_Hot.v:92-107](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Branch_One_Hot.v#L92-L107) —— `data_demux`：同样 `BROADCAST=0`，把 `input_data` 送到被选中那路的 `output_data`，其余路被 `Annuller` 清零（见 u5-l1）。`BROADCAST=0` 意味着未选中的输出拿不到数据，更安全也更易追踪仿真。

把三件合起来：输入握手只在「被选中的下游里有就绪者」时完成，数据只流向被选中的那路。这与 Fork_Lazy（必须所有下游就绪）形成鲜明对比——Branch 是 OR，Fork 是 AND。

#### 4.3.4 代码实践

**实践目标**：验证 Branch 的「按独热地址选路」与「OR 归约就绪」行为。

**操作步骤**：

1. 打开 `Pipeline_Branch_One_Hot.v`，确认三件构件分别处理 ready（L56-68）、valid（L72-87）、data（L92-107）。
2. 打开 `Demultiplexer_One_Hot.v`，确认 `BROADCAST=0` 时未选中输出被 `Annuller` 清零（即未选中路拿不到数据）。
3. 思想实验：`OUTPUT_COUNT = 4`，selector = `4'b0010`（选中第 1 路），输入 `valid=1, data=D`，四路下游 `ready` 分别为 `0,1,0,0`。

**需要观察的现象（按源码逻辑推理）**：

- `input_ready = OR(selector[j] && ready[j]) = selector[1] && ready[1] = 1` ⇒ 输入握手成立，数据 D 流向第 1 路。
- `output_valid = 4'b0010`（仅第 1 路），`output_data` 第 1 路为 D、其余路为 0。
- 若把 selector 改为 `4'b0011`（同时选中第 0、1 路），则第 0、1 路都拿到 valid 与数据 D，`input_ready = ready[0] || ready[1]`（任一就绪即可）——退化为「不同步的 Eager Fork」。

**预期结果**：单选时数据精确送达一路；多选时广播到所有被选路且 OR 归约就绪。**待本地仿真验证**。

#### 4.3.5 小练习与答案

**练习 1**：Branch 用 OR 归约 `input_ready`，Fork_Lazy 用 AND 归约。这一差别在语义上意味着什么？

**答案**：Branch 是「非会合」——被选中的下游里**任一**就绪，输入即可完成（先就绪者先得）。Fork_Lazy 是「会合」——**所有**下游必须**同时**就绪输入才能完成。OR 意味着并行可选、吞吐不被最慢者拖死；AND 意味着锁步、保证一致性但易被慢者拖累。

**练习 2**：Branch 为什么把 ready、valid、data 拆成三个独立构件，而不是写在一个 always 块里？

**答案**：因为三类信号本就职责不同（ready 上行、valid/data 下行），且复用了现成的 `Multiplexer_One_Hot`/`Demultiplexer_One_Hot`。拆开后每个子模块都有清晰意图（模块即文档，见 u5-l1），CAD 工具也能分别优化，符合 u4-l1「把无关连接移入子模块」的分解原则。

**练习 3**：如果把 `data_demux` 的 `BROADCAST` 从 0 改成 1，行为会如何变化？为什么本书默认用 0？

**答案**：`BROADCAST=1` 会把 `input_data` 复制广播到所有输出（不论是否选中），只是用 `valid` 标识哪路该接收。默认 `BROADCAST=0` 则只把数据送到被选中那路、其余清零，未选中的下游无法窥探或误收数据，更安全、仿真更易追踪（详见 `Demultiplexer_One_Hot.v` 顶部说明）。

## 5. 综合实践

把 Fork 与 Join 串起来，搭一个「一分为二、各自处理、再合二为一」的并行流水线骨架。这是 Fork/Join 最经典的用法：把一份工作拆成两个并行子任务，各自走一条支路，最后用 Join 把两路结果拼回去。

**任务**：用 `Pipeline_Fork_Blocking`（OUTPUT_COUNT=2）+ 两条支路 + `Pipeline_Join`（INPUT_COUNT=2）实现 `1 → 2 → 1`，数据位宽 `WORD_WIDTH = 8`。

下面是**示例代码**（不在仓库中，仅作骨架演示，需自行补全时钟/复位与支路逻辑后仿真）：

```verilog
// 示例代码：1 -> 2 -> 1 的 Fork + Join 骨架
// 假设已有 clock / clear，WORD_WIDTH = 8

// ---- 输入侧 ----
wire        in_valid, in_ready;
wire [7:0]  in_data;

// ---- Fork 输出（两路）----
wire [1:0]  fork_valid;
wire [1:0]  fork_ready;
wire [15:0] fork_data;          // 2 * 8 = 16 bit，[7:0]=路0，[15:8]=路1

Pipeline_Fork_Blocking #(.WORD_WIDTH(8), .OUTPUT_COUNT(2)) fork_inst (
    .clock(clock), .clear(clear),
    .input_valid (in_valid),  .input_ready (in_ready),  .input_data (in_data),
    .output_valid(fork_valid), .output_ready(fork_ready), .output_data(fork_data)
);

// ---- 两条支路：这里各打 1 拍延迟，模拟两段并行处理 ----
// （实际可替换为任意 ready/valid 处理模块）
wire [7:0]  b0_data = fork_data[7:0];
wire [7:0]  b1_data = fork_data[15:8];
wire [1:0]  b_valid = fork_valid;
wire [1:0]  b_ready = fork_ready;   // 直通就绪，便于先看通握手

// ---- Join 输入（两路）----
wire        out_valid, out_ready;
wire [15:0] out_data;               // 两路 8 bit 拼成 16 bit

Pipeline_Join #(.WORD_WIDTH(8), .INPUT_COUNT(2)) join_inst (
    .clock(clock), .clear(clear),
    .input_valid (b_valid),  .input_ready (b_ready),  .input_data ({b1_data, b0_data}),
    .output_valid(out_valid), .output_ready(out_ready), .output_data(out_data)
);
```

**请完成并思考**：

1. **接线自洽**：`fork_ready`（Fork 的输出就绪）正好由 Join 的输入就绪 `b_ready` 驱动，确认数据通路闭合：`Fork → 支路 → Join`。
2. **跟踪一笔事务**：设上游发来 `in_valid=1, in_data=0xAB`，下游 `out_ready=1`。按源码推理：
   - Fork_Lazy 要求两路输出同时就绪 ⇒ Fork 把 `0xAB` 复制到两路（`fork_data = 16'hABAB`）；
   - 两条支路各自把 `0xAB` 送到 Join 的两路输入；
   - Join 见两路 valid 同高 ⇒ `out_valid=1`，`out_data = {0xAB, 0xAB} = 16'hABAB`，同拍完成。
3. **解释阻塞**：把第 1 条支路换成「偶尔停顿」的处理模块（`b_ready[1]` 有时为 0）。由于 Fork_Blocking 要求两路**同时**就绪，只要支路 1 停顿，`fork_ready` 凑不齐全 1，`in_ready` 即为 0，**上游被阻塞**——这就是本讲核心结论：Fork_Blocking 里，最慢的支路拖住整条流水线。
4. **改进**：若希望两条支路能各自独立前进、不被彼此拖累，应把 `Pipeline_Fork_Blocking` 换成哪个模块？为什么？（提示：缓冲位置。）

**预期结果**：直通支路时，输出 `out_data` 等于输入数据的两份拼接，握手每拍可完成一笔；任一支路停顿则上游立即停顿。换用 `Pipeline_Fork_Eager` 后，支路可独立缓冲、上游吞吐不再被单条支路绑死（但失去严格同步）。完整时序波形**待本地仿真验证**。

## 6. 本讲小结

- Fork 把一路握手复制成 N 路；Lazy 是纯组合叶子，Eager 在每路输出加 Skid Buffer（各路独立完成），Blocking 在输入侧加 Skid Buffer（仍要求各路同时完成）。
- Fork 的握手本质是**输出侧 AND 归约**：所有输出同时就绪，输入才前进。这是 u11-l2「会合」在数据流层面的体现。
- **Fork_Blocking 会阻塞上游**，因为最慢的一路决定整体；若下游可撤销 `ready`，甚至可能死锁——这是改用 Eager 的根本动机。
- Join 是 Fork 的对偶，**输入侧 AND 归约**：所有输入到齐才合成一路更宽输出；它主动给每路输入加 Skid Buffer 以杜绝组合环，且各路 ready 永远同时为 0 或同时等于 output_ready（不会某路偷跑）。
- Branch_One_Hot 用「OR mux + 两个 demux」按独热地址选路；**OR 归约**使其成为非会合的「谁先就绪谁先走」，与 Fork/Join 的 AND 会合形成对照。
- 三类构件共享同一个设计哲学：把 ready/valid/data 三类信号交给已测试的组合子模块（Skid Buffer、mux、demux），构件本身只做连线与归约，模块即文档、模块即设计意图。

## 7. 下一步学习建议

- 下一讲 **u12-l3（Merge 仲裁合流与控制门）**：讲解 `Pipeline_Merge_*` 与 `Pipeline_Gate`。建议重点对比 **Merge 与 Join**——同是「多路合成一路」，Merge 用仲裁（任一路有效即可，OR/优先/轮询），Join 用会合（全部到齐）。理解了本讲的 AND vs OR 归约，Merge 会非常自然。
- 若对「会合」原语意犹未尽，回看 **u11-l2（Muller C 元素与流水线同步）**，把本章的 Fork/Join AND 归约与 Muller C 元素的「全员一致才翻转」对应起来。
- 想亲手仿真本讲的模块，先学 **u18-l2（仿真、测试台与综合验证）**：用 `Simulation_Clock` 与 `Synthesis_Harness` 搭测试台，驱动 `output_ready` 观察本讲描述的阻塞/会合波形。
- 继续阅读源码：`Pipeline_Join_Lazy.v`（Join 的纯组合对偶）、`Pipeline_Fork_Eager.v`（体会「缓冲位置」如何改变吞吐），并与本讲的 Blocking/Lazy 对照，巩固「同一叶子、不同缓冲、不同语义」的设计模式。
