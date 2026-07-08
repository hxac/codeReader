# Pipeline_Iterator 与握手乘法器

## 1. 本讲目标

前面十几讲，我们一直在攒「零件」：寄存器、流水线寄存器、加减法器、计数器、各类弹性缓冲（Skid/Half/FIFO）、Fork/Join/Merge/Gate/Sink、握手与 CDC 同步器。本讲第一次把这些零件**拼成一台完整的小机器**——一台能自己跑循环、自己管握手的「计算引擎」。

学完本讲后，你应该能够：

1. 说清 `Pipeline_Iterator` 是一个**硬件 for 循环**：它把一组初始数据装进 FIFO，然后驱动一个外挂模块跑 `iteration_count` 轮，每轮把模块输出（或原始数据）回填进 FIFO，最后一轮把结果送到输出接口——并解释 `feedback_type` 如何决定是「反复变换同一份数据」还是「反复送同一份数据」。
2. 画出 `Pipeline_Iterator` 的数据通路（输入选择器 → FIFO → 门 → 两个 Blocking Fork + 三个 Sink）与控制通路（6 态 FSM + 三个计数器），并追踪一次「带反馈的迭代」在状态间如何流转。
3. 说清 `Pipeline_Handshake_Multiplier` 是另一种「重复」引擎——它不变换数据，而是把一笔输入数据**原样重复 N 次**输出，并精确描述它「启动—重复—完成」的握手时序：输入握手一次，之后要等 N 次输出握手才肯再吃下一笔输入。
4. 把一个迭代算法（如恢复余数除法）在概念上映射到 `Pipeline_Iterator` 的三个旋钮（`data_count` / `iteration_count` / `feedback_type`）上。
5. 体会「引擎组装」这条主线：两个模块自身都**几乎不写时序逻辑**，全部靠实例化已有构建块拼出——这是 u4-l1「构建块库」哲学在大型模块上的兑现，也是 u10-l1「COTTC FSM + 数据/控制分离」方法的一次完整实战。

## 2. 前置知识

本讲是「复合流水线引擎」单元首篇，站在很多前序讲义的肩膀上。你需要这几块基础（都已在前序讲义建立）：

- **u10-l1（Skid Buffer 与 COTTC FSM）**：这是最重要的前置。你已经知道 ready/valid 握手的规矩（`handshake_complete = ready && valid`，接口内不得有组合环），知道「数据通路 / 控制通路分离」的范式，也知道 COTTC 状态机设计法——**先声明约束与数据通路变换（transformation），再由变换推出状态转移**，而不是手工枚举转移边。本讲的 `Pipeline_Iterator` 把这套方法用到了极致：它的 `state_next` 就是一长串「最后赋值胜出」的三元链。
- **u12-l1（缓冲与停顿平滑）**：知道 `Pipeline_FIFO_Buffer` 的用法（任意深度、`input_valid/ready` ↔ `output_valid/ready`），它是 Iterator 的数据蓄水池。
- **u12-l2（Fork/Join/Branch）**：知道 `Pipeline_Fork_Blocking` 的语义——一进多出、**所有输出必须同时完成握手**上游才前进（AND 归约会合），也知道它「最慢的一路拖全局」会阻塞上游。本讲 Iterator 用两个 Blocking Fork 来「边送模块边回填 FIFO」「边出结果边回填 FIFO」，并靠这个「同步」性质保证两个分支的传输笔数恒相等，从而**只需一个计数器**。
- **u12-l3（Merge 仲裁合流与控制门）**：知道 `Pipeline_Merge_One_Hot`（外部独热 selector 选路合流）、`Pipeline_Gate`（必须同时门控 valid 与 ready）。
- **u8-l2（计数器）**：知道 `Counter_Binary` 是「加法器 + 寄存器」组合，有 `run`/`load`/`clear`，`load` 优先于 `run`。
- **u15-l1（脉冲分频）**：知道 `Pulse_Divider` 每收到 `divisor` 个输入脉冲发一个输出脉冲、`restart` 重装分频值。本讲握手乘法器正是用它来「数到 N 次输出就放行」。
- **u3-l1（链式三元）**：知道「链式三元 + 阻塞赋值 = 最后赋值胜出」是写 FSM 转移的自然范式。

> 一个贯穿全讲的直觉：**「重复」在硬件里有两种根本不同的含义**。一种是**迭代（iterate）**——每轮把上一轮的结果再变一次：\(D \to f(D) \to f(f(D)) \to \cdots \to f^{N}(D)\)，数据被一步步变换。另一种是**复制（replicate）**——把同一笔数据原样发 N 遍：\(D, D, D, \ldots, D\)，数据不变。本讲的两个模块恰好各代表一种：`Pipeline_Iterator` 是迭代，`Pipeline_Handshake_Multiplier` 是复制。分清这二者，是理解它们为何长成完全不一样样子的钥匙。

## 3. 本讲源码地图

本讲涉及的关键文件（均在仓库根目录）：

| 文件 | 作用 | 规模 |
| --- | --- | --- |
| [Pipeline_Iterator.v](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Iterator.v) | **本讲主角之一**：一个硬件 for 循环，驱动外挂模块多轮迭代，自带 ready/valid 输入输出，可嵌套 | ~836 行（含大量注释） |
| [Pipeline_Handshake_Multiplier.v](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Handshake_Multiplier.v) | **本讲主角之二**：一笔输入握手 → 原样重复 N 次输出握手 | ~110 行 |
| 辅助构建块（前序讲义已学，本讲直接实例化） | `Pipeline_Merge_One_Hot`、`Pipeline_FIFO_Buffer`、`Pipeline_Gate`、`Pipeline_Fork_Blocking`、`Pipeline_Sink`、`Pipeline_Half_Buffer`、`Pulse_Divider`、`Counter_Binary`、`Register`、`Arbiter_Priority` | — |

两个主角的对比：

| | `Pipeline_Iterator` | `Pipeline_Handshake_Multiplier` |
| --- | --- | --- |
| 「重复」的含义 | **迭代**：每轮变换数据 | **复制**：原样重复同一笔数据 |
| 重复次数来源 | 配置接口的 `iteration_count`（运行前设定） | 每笔输入自带的 `input_data_repeat_count`（逐笔可变） |
| 是否外挂模块 | 是（`to_module_*` / `from_module_*`） | 否（只搬运数据） |
| 规模 | 大（多态 FSM + 数据通路） | 小（一个 Half Buffer + 一个 Pulse_Divider） |

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**4.1** 钻进 `Pipeline_Iterator`（迭代引擎），**4.2** 钻进 `Pipeline_Handshake_Multiplier`（复制引擎），**4.3** 抬高视角讲「引擎组装」这条贯穿主线——两个模块是如何由构建块拼出来的，以及其中那个精妙的「隐式门控」技巧。

### 4.1 迭代引擎：Pipeline_Iterator

#### 4.1.1 概念说明

`Pipeline_Iterator` 把自己描述成一个 [「硬件 for 循环」](Pipeline_Iterator.v#L1-L23)（A hardware for-loop）。它的工作模型可以用一段伪代码概括：

```text
// iteration_count: 循环多少轮
// data_count:      每轮处理多少个数据字
// feedback_type:   每轮拿什么回填 FIFO（原始数据 / 模块输出）
load data_count 个字进 FIFO;          // 初始装载，不算一轮
for (i = 0; i < iteration_count-1; i++) {
    把 FIFO 里的 data_count 个字逐个送给外挂模块;
    收模块回的 data_count 个字;
    if (feedback_type == MODULE)  把模块输出回填 FIFO;   // 迭代：f(D)
    else                          把 FIFO 输出回填 FIFO;   // 复读：D
}
// 最后一轮（不回填）：
把 FIFO 里的 data_count 个字逐个送给外挂模块;
收模块回的 data_count 个字, 直接送往 Iterator 输出;     // 输出 f^N(D)
// FIFO 此时恰好清空
```

这里有三条硬约束，都写在文件头部的注释里：

1. **模块输出必须与输入等宽、等数量**（[Pipeline_Iterator.v:14-16](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Iterator.v#L14-L16)）：因为要回填进同一个 FIFO，状态向量的「形状」每轮不能变。这正是迭代算法的特征——状态在每轮被映射成同形的新状态。
2. **FIFO 深度必须 ≥ 最大数据集大小**（[Pipeline_Iterator.v:18-24](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Iterator.v#L18-L24)）：否则初始装载会塞爆 FIFO、永久挂死，必须拉 `clear` 才能恢复。
3. **它本身也是一个带 ready/valid 输入输出的模块**（[Pipeline_Iterator.v:10-12](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Iterator.v#L10-L12)）：所以理论上可以**嵌套**多个 Iterator 来实现硬件的嵌套循环。

> **为什么 `feedback_type` 有两档？** 如果回填「模块输出」，那么每轮数据都被 `f` 变换一次，N 轮后得到 \(f^{N}(D)\)——这是真正的**迭代**（如反复倍增、反复累加）。如果回填「FIFO 输出」（即原始数据自己），那么每轮模块吃到的都是**同一份**输入，只有最后一轮的结果被送出——这适合「外挂模块自身带内部状态、每被调用一次就累进一步」的场景（例如一个内部计数器）。绝大多数迭代算法用的是前者（`FEEDBACK_MODULE`）。

#### 4.1.2 核心流程

`Pipeline_Iterator` 严格遵循 u10-l1 的「数据通路 / 控制通路分离」范式。先看数据通路（只认控制信号、不含状态）：

```text
                         ┌─────────────┐
  input_data ─────────▶ │             │
  fifo_feedback ───────▶│ Merge_One_Hot│──▶ selector ──▶ [FIFO_Buffer] ──▶ fifo_data
  module_feedback ─────▶│  (3 选 1)    │                                      │
                         └─────────────┘                            [Gate: 数够 data_count 就关]
                                                                    fifo_data_gated
                                                                          │
                                                          ┌─── Fork_Blocking (2) ───┐
                                                   to_module (送外挂模块)          fifo_sink ──▶ [Sink] ──▶ fifo_feedback (回填 FIFO)
                                                                                          (不需要回填时 sunk)

  from_module ──▶ ┌── Fork_Blocking (2) ───┐
                  │                        │
            module_fork ──▶ [Sink] ──▶ module_feedback (回填 FIFO，仅 MODULE 态)
            output_fork  ──▶ [Sink] ──▶ output (Iterator 输出，仅最后一轮)
```

四个关键动作：

- **选源**（`Merge_One_Hot`）：决定这一刻往 FIFO 里灌的是「新输入」「FIFO 自身输出」还是「模块输出」——由当前状态决定。
- **蓄水**（`FIFO_Buffer`）：初始装载与每轮回填的数据都暂存于此。
- **分流**（两个 `Fork_Blocking`）：第一个 Fork 把 FIFO 输出同时送给「外挂模块」和「回填 FIFO 的支路」；第二个 Fork 把模块输出同时送给「Iterator 输出」和「回填 FIFO 的支路」。Blocking 保证两条支路传输笔数恒等。
- **弃流**（三个 `Sink`）：不需要回填 FIFO 时把对应支路 sink 掉（丢弃 + 永远 ready，避免反压阻塞 Blocking Fork 的另一条支路）。

再看控制通路（集中所有状态）：一个 **6 态 FSM**（[Pipeline_Iterator.v:561-567](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Iterator.v#L561-L567)）配 **三个计数器**（迭代轮数、本轮已处理字数、模块已产出字数）：

```text
EMPTY  ──(首次配置)──▶ IDLE  ──(开始装数据)──▶ LOAD ──(装满 data_count 字)──▶ FIFO / MODULE
                                                                          │
                                          ┌─────────────────────────────────┘
                                          ▼ (iteration_count-1 轮带反馈地跑)
                          FIFO / MODULE  ──(轮数用完前最后一轮)──▶  OUTPUT
                                                                  │
                                          OUTPUT ──(送完最后字)──▶ IDLE
```

状态含义：

| 状态 | 含义 | 能否改配置 |
| --- | --- | --- |
| `EMPTY` | 上电后、首次配置前，不能装数据 | 能 |
| `IDLE` | 等待装初始数据，配置可改 | 能 |
| `LOAD` | 正往 FIFO 装初始数据集 | 不能（直到回到 IDLE） |
| `FIFO` | 带反馈地迭代，反馈源 = FIFO 输出 | 不能 |
| `MODULE` | 带反馈地迭代，反馈源 = 模块输出 | 不能 |
| `OUTPUT` | 最后一轮，**不**反馈，结果送输出 | 不能 |

这套 FSM 的设计方法是 u10-l1 的 COTTC 法：**先声明「数据通路变换」（在哪个状态、依赖哪些计数器，发生什么事件），再由变换推出状态转移**。下面 4.1.3 会看到这两段代码的原文。

#### 4.1.3 源码精读

**(a) 输入选择器：3 路合流选源**

[Pipeline_Iterator.v:96-140](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Iterator.v#L96-L140)——用一个 `Pipeline_Merge_One_Hot` 在「新输入 / FIFO 反馈 / 模块反馈」三路里独热选一，灌进 FIFO。`input_select_one_hot` 是 3 位独热码（`100`=新输入、`010`=FIFO 反馈、`001`=模块反馈、`000`=都不选）。这里 `HANDSHAKE_MERGE="OR"`/`DATA_MERGE="OR"` 配 `IMPLEMENTATION="AND"` 是 Merge 的标准用法（回顾 u12-l3：独热选路 + mux 取数 + demux 回 ready）。

**(b) FIFO + 输出门：蓄水与「数够就停」**

[Pipeline_Iterator.v:154-173](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Iterator.v#L154-L173) 是 `Pipeline_FIFO_Buffer`（`CIRCULAR_BUFFER=0`，普通模式）。紧跟着 [Pipeline_Iterator.v:189-206](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Iterator.v#L189-L206) 是一个 `Pipeline_Gate`，它的 `enable = (gate_fifo_output == 1'b0)`，而 [Pipeline_Iterator.v:832](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Iterator.v#L832) 设定 `gate_fifo_output = (data_count_remaining == DATA_COUNT_ZERO)`——**本轮已送够 `data_count` 个字给模块，就关掉 FIFO 输出**。注释（[Pipeline_Iterator.v:175-180](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Iterator.v#L175-L180)）解释了为什么必须这样：否则当外挂模块内部流水线比数据集还深时，会重复送太多份。

**(c) 两个 Blocking Fork + 三个 Sink：分流与弃流**

[Pipeline_Iterator.v:221-269](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Iterator.v#L221-L269) 是 FIFO 输出的 Blocking Fork（一进二出：一路送模块、一路回填 FIFO），后接 `Pipeline_Sink`。[Pipeline_Iterator.v:286-351](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Iterator.v#L286-L351) 是模块输出的 Blocking Fork（一路回填 FIFO、一路送 Iterator 输出），后接两个 Sink。

这里有一段极其精妙的设计说明，值得逐字读 [Pipeline_Iterator.v:271-277](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Iterator.v#L271-L277) 和 [Pipeline_Iterator.v:817-826](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Iterator.v#L817-L826)：Blocking Fork 的两条支路必须同时完成握手，所以**两个支路的传输笔数永远相等**——这意味着回填 FIFO 的笔数与送给模块的笔数天然一致，**只需一个计数器**就能管住「本轮数据」。Sink 的作用是「不需要这条支路时把它丢弃并永远 ready」，否则一条闲置支路会反压住 Blocking Fork、把另一条真正在用的支路也卡死（回顾 u12-l2 的 Fork_Blocking 阻塞问题）。

**(d) COTTC FSM：变换 → 转移**

数据通路变换（事件）声明在 [Pipeline_Iterator.v:705-735](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Iterator.v#L705-L735)：每行形如「在某某状态 && 某握手完成 && 某计数器到某值」就置某个 `load_*` / `run_*` 为真。例如 `run_output_last`（[Pipeline_Iterator.v:734](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Iterator.v#L734)）= 在 `OUTPUT` 态 && 模块输出握手完成 && 已产出字数到 1（最后一个字）。

状态转移则由这些变换推出，写在 [Pipeline_Iterator.v:742-767](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Iterator.v#L742-L767)——这就是 u3-l1 讲过的「链式三元 + 最后赋值胜出」FSM 范式：每行是一条 `state_next = (某变换) ? 目标态 : state_next`，越往下优先级越高，最后命中的胜出。

**(e) 控制/输入握手仲裁：一个简化技巧**

[Pipeline_Iterator.v:585-652](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Iterator.v#L585-L652) 处理一个麻烦：IDLE 态下「配置握手」和「输入数据握手」可能同时来，会让计数器更新陷入复杂的角落情况。解法是用一个 `Arbiter_Priority` 给两者仲裁——**让配置先走、数据后走**，这样后续控制逻辑就完全不用考虑二者的先后。配套的 `Pipeline_Gate`（[Pipeline_Iterator.v:611-628](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Iterator.v#L611-L628)）在非 IDLE/EMPTY 态把配置握手门控掉，否则一个悬而未决的配置 `valid` 会永远堵住数据装载。注释 [Pipeline_Iterator.v:596-598](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Iterator.v#L596-L598) 点明这个简化「只在配置握手总是单周期完成时成立」。

#### 4.1.4 代码实践：把恢复余数除法映射到 Iterator

> 本实践是**源码阅读 + 设计映射型**任务：仓库里没有现成的除法器模块，我们不编造代码，而是练「如何用 Iterator 的三个旋钮表达一个真实迭代算法」。

**实践目标**：用一个外挂的「单步恢复除法」模块 + 一个 `Pipeline_Iterator`，搭出「输入被除数/除数 → 输出商/余数」的除法器，并定出三个旋钮的取值。

**操作步骤**：

1. **回忆恢复除法（restoring division）的一步**：维护一个状态向量 `{partial_remainder, quotient_acc, divisor}`。每一步把 `partial_remainder` 左移一位（吃进一位被除数），减去除数；若结果非负则商位 = 1 并保留，否则商位 = 0 并恢复（加回除数）。一步产生一位商。
2. **确认「状态同形」**：每一步前后状态向量都是同样的三个字、同样宽度——满足 Iterator 「模块输出与输入等宽等数量」的硬约束（[Pipeline_Iterator.v:14-16](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Iterator.v#L14-L16)）。✅
3. **定三个旋钮**：
   - `data_count = 3`（状态向量有 3 个字）；
   - `iteration_count = WORD_WIDTH`（每步出一位商，跑满字宽位）；
   - `feedback_type = FEEDBACK_MODULE`（每步把新状态回填 FIFO，是真正的迭代）。
4. **外挂模块的接口形状**：`to_module_data` 收 `{remainder, quotient, divisor}`，`from_module_data` 回同样形状的下一步状态。注意若你的单步模块输入输出宽度不一致，要按 [Pipeline_Iterator.v:36-43](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Iterator.v#L36-L43) 的注释在外围做位宽/字数对齐。
5. **追踪一次运行**：初始装载 `{R0, 0, divisor}` 三字进 FIFO → 进入 `MODULE` 态 → 每轮单步模块把状态推进一位 → `WORD_WIDTH-1` 轮后进入 `OUTPUT` 态 → 最后一轮结果（完整商与最终余数）送输出，FIFO 清空。

**需要观察的现象**：在 `MODULE` 态，FIFO 的内容每轮都在变（被单步模块的输出替换）；在 `OUTPUT` 态，`sink_module_feedback` 拉起（不再回填）、`sink_output` 放下（开始送输出）——对照 [Pipeline_Iterator.v:828-833](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Iterator.v#L828-L833) 验证这两个 Sink 控制信号在各态的取值。

**预期结果**（待本地验证）：给出一个被除数与除数，`iteration_count` 轮后 Iterator 输出端应得到与软件恢复除法一致的商与余数。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `feedback_type` 设成 `FEEDBACK_FIFO`（回填 FIFO 自身输出），外挂模块是一个「内部计数器、每被调用一次自增 1」的模块，`iteration_count = 5`，那么 Iterator 最终输出什么？输出是输入数据的函数吗？

> **答案**：每轮模块吃到的都是**同一份原始数据**（因为 FIFO 被自己的输出回填），但模块**内部计数**累加了 5 次。最后一轮送出的是模块第 5 次被调用时的输出——它反映的是模块内部状态，而**不是输入数据的函数**（输入每次都一样）。这印证了 4.1.1 里「`FEEDBACK_FIFO` 适合模块自带内部状态」的说法。

**练习 2**：[Pipeline_Iterator.v:828-833](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Iterator.v#L828-L833) 里，`sink_module_feedback = (state != MODULE)`、`sink_output = (state != OUTPUT)`。为什么这两个 Sink 必须存在？去掉 `sink_output` 会怎样？

> **答案**：Blocking Fork 的两条支路必须同时完成握手。若不 sink 掉闲置支路，那条支路（例如非 `OUTPUT` 态时的输出支路）下游永远不 ready，会反压住 Blocking Fork，进而卡死「回填 FIFO」的另一条支路，迭代直接停摆。去掉 `sink_output` 后，在 `FIFO`/`MODULE` 态模块输出无法被回填，Iterator 会死锁。

**练习 3**：为什么 Iterator 的 FIFO 输出门（4.1.3 (b)）是必须的？提示：读 [Pipeline_Iterator.v:175-180](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Iterator.v#L175-L180) 的注释。

> **答案**：若外挂模块内部流水线比 `data_count` 还深，模块会延迟多拍才吐完一批结果。若不门控 FIFO 输出，FIFO 会在等模块吐完期间继续往外送数据，导致同一批 `data_count` 字被重复送给模块。门控保证「本轮送够 `data_count` 字就停」，再配合「已产出字数」计数器等模块吐够 `data_count` 字才进下一轮。

---

### 4.2 握手乘法器：Pipeline_Handshake_Multiplier

#### 4.2.1 概念说明

`Pipeline_Handshake_Multiplier` 是另一种「重复」引擎——**复制**，不是迭代。它的合同写在文件头 [Pipeline_Handshake_Multiplier.v:1-13](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Handshake_Multiplier.v#L1-L13)：

- 收一笔 ready/valid 输入握手（带一个 `repeat_count`）；
- 在输出侧**原样重复 `repeat_count` 次**这笔数据（每次都是一个完整的输出握手，内容相同）；
- 在这 `repeat_count` 次输出握手全部完成前，**不再接受**新的输入握手；
- `repeat_count = 0` 是特例：立即清空缓冲，吞掉这笔输入、不产生任何输出，立刻可以收下一笔。

它**不外挂任何模块**，数据不被变换——只是把一笔数据「放大」成 N 笔。典型用途：把一个「启动脉冲」式的配置项扇出成 N 份送给 N 个下游，或把一笔广播数据重复灌入一个需要多次采样的接口。

> **与 Iterator 的对照**：Iterator 的 `iteration_count` 是**运行前**在配置接口设好的、整个运行期间不变；Multiplier 的 `repeat_count` 是**逐笔**跟着数据进来的、每笔可以不同。前者驱动变换，后者驱动复制。

#### 4.2.2 核心流程

整个模块只有两个构建块 + 一段组合控制：

```text
                    ┌──────────────────────────────┐
  input_valid ────▶ │                              │ ◀── input_ready
  input_data ─────▶ │   Pipeline_Half_Buffer       │
                    │   (CIRCULAR_BUFFER = 0)       │
                    │                              │
                    │   output_data_valid ─────────┼──▶ output_data_valid ──▶ (下游)
                    │   output_data      ──────────┼──▶ output_data
                    │   output_ready     ◀─────────┼ ◀── output_data_ready_divided  (来自下方 Pulse_Divider)
                    └──────────────────────────────┘

   output_data_ready (下游真 ready) ──┐
   output_data_valid                  ├──▶ module_output_handshake_done ──▶ [Pulse_Divider] ──▶ output_data_ready_divided
                                      │       .restart = module_input_handshake_done           (数到 repeat_count 次发一拍)
                                      │       .divisor  = input_data_repeat_count
   input handshake done ──────────────┘  (输入握手时重装分频器)
```

「启动—重复—完成」的握手时序（本讲的核心实践点）：

1. **启动**：输入握手完成（`module_input_handshake_done = input_valid && input_ready`，见 [Pipeline_Handshake_Multiplier.v:81](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Handshake_Multiplier.v#L81)）。Half Buffer 把数据锁存、拉高 `output_data_valid`；同时这一拍也作为 `restart` 重装 Pulse_Divider，把 `repeat_count` 装为分频值。
2. **重复**：Half Buffer 的 `output_ready` 不是下游的 ready，而是 Pulse_Divider 的 `pulse_out`。所以只要没数够 `repeat_count` 次，Half Buffer 的输出握手就**不会完成**，`output_data_valid` 与 `output_data` 保持不变——下游每次握手（`output_data_valid && output_data_ready`）看到的都是同一笔数据，Pulse_Divider 把这些握手当作输入脉冲逐个计数。
3. **完成**：数到第 `repeat_count` 次输出握手时，Pulse_Divider 发出一拍 `output_data_ready_divided`。这一拍同时满足 Half Buffer 的 `output_ready`，Half Buffer 的输出握手**这才完成**、缓冲清空，`input_ready` 重新拉高，准备收下一笔。

`repeat_count = 0` 的快捷通道：[Pipeline_Handshake_Multiplier.v:82](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Handshake_Multiplier.v#L82) 检测到 `module_input_handshake_done && (repeat_count == 0)`，置 `clear_input_buffer` 直接清 Half Buffer——数据被吞、无输出、立刻可收下一笔（注释 [Pipeline_Handshake_Multiplier.v:10-13](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Handshake_Multiplier.v#L10-L13) 与 [Pipeline_Handshake_Multiplier.v:86-89](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Handshake_Multiplier.v#L86-L89)）。

#### 4.2.3 源码精读

**(a) Half Buffer 锁存输入**

[Pipeline_Handshake_Multiplier.v:51-68](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Handshake_Multiplier.v#L51-L68) 实例化 `Pipeline_Half_Buffer`（`CIRCULAR_BUFFER=0`）。回顾 u10-l2：Half Buffer 用一个数据寄存器 + 一个满/空位，**切断输入到输出的组合路径**，且「必须先读出才能再写入」——这正好满足「重复完 N 次才收下一笔」的要求。它的 `clear` 接 `clear | clear_input_buffer`，实现 `repeat_count=0` 的即时清空。

**(b) 用 Pulse_Divider「数到 N 就放行」**

[Pipeline_Handshake_Multiplier.v:91-106](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Handshake_Multiplier.v#L91-L106) 实例化 `Pulse_Divider`：`pulses_in` 接每次输出握手（`module_output_handshake_done`），`divisor` 接 `repeat_count`，`restart` 接输入握手。回顾 u15-l1：Pulse_Divider 是个下计数器，「数到第 `divisor` 个输入脉冲时发一拍输出脉冲」——这里恰好就是「第 `repeat_count` 次输出握手时放行一次」。它的 `pulse_out` 与输入脉冲**同拍**出现（[Pulse_Divider.v:16-20](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pulse_Divider.v#L16-L20)），所以第 N 次输出握手当拍 Half Buffer 就完成。注释 [Pipeline_Handshake_Multiplier.v:70-73](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Handshake_Multiplier.v#L70-L73) 还点明一个细节：分频值在输出 ready 拉高**前一拍**就被存进 Pulse_Divider，保证计数基准正确。

**(c) 一段组合逻辑汇齐三个握手事件**

[Pipeline_Handshake_Multiplier.v:80-84](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Handshake_Multiplier.v#L80-L84) 用一个 `always @(*)` 算出三个事件：输入握手完成、清缓冲（输入握手 && repeat_count=0）、输出握手完成。这是 u9-l2 的标准纪律——**所有影响接口的内部动作都门控在「握手完成」上**。

#### 4.2.4 代码实践：手算启动/完成握手时序

**实践目标**：给定一组输入，手工推演 `Pipeline_Handshake_Multiplier` 逐拍的 `input_ready` / `output_data_valid` / 输出握手计数，验证「一笔输入 → N 笔输出」的时序。

**操作步骤**：

1. 设 `WORD_WIDTH=8`、`MAX_REPEAT_COUNT=4`（故 `REPEAT_COUNT_WIDTH = clog2(4)+1 = 3`，见 [Pipeline_Handshake_Multiplier.v:23](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Handshake_Multiplier.v#L23)）。
2. 假设下游从第 2 拍起每拍都 `output_data_ready=1`。
3. 第 0 拍：上游送 `input_valid=1, input_data=0x5A, repeat_count=3`。此时 Half Buffer 空，`input_ready=1` → 输入握手完成 → 数据 `0x5A` 锁存、`restart` 重装 Pulse_Divider（分频值=3）、`clear_input_buffer=0`（repeat_count 非 0）。
4. 第 1 拍：`output_data_valid=1, output_data=0x5A`，`input_ready=0`（Half Buffer 满，未读出）。下游未 ready，无输出握手。Pulse_Divider 计数仍为初值。
5. 第 2 拍起：下游 ready。第 2、3、4 拍各发生一次输出握手（`0x5A` 被下游收三次）。Pulse_Divider 数到第 3 次（第 4 拍）发 `output_data_ready_divided=1` → Half Buffer 输出握手完成 → 清空。
6. 第 5 拍：`output_data_valid=0`，`input_ready=1`，可收下一笔。
7. 再送一笔 `repeat_count=0`：第 0 拍输入握手完成且 `clear_input_buffer=1` → Half Buffer 立即被清 → 下一拍 `input_ready` 又为 1，全程无输出。

**需要观察的现象**：`output_data` 在第 2~4 拍**保持 `0x5A` 不变**（Half Buffer 未完成输出握手，数据不动）；`input_ready` 在重复期间为 0、完成后才回 1。

**预期结果**：下游共收到 3 份 `0x5A`；从输入握手到下一次可输入，至少经过「重复次数」个输出握手周期。具体拍数待本地仿真确认（取决于下游 ready 的时序）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 Multiplier 用 Half Buffer 而不是 Skid Buffer？

> **答案**：Half Buffer「必须先读出才能再写入」的半双工特性，正好就是「重复 N 次前不许收新数据」的语义——天然挡住新输入。Skid Buffer 满吞吐、允许并发读写，反而需要额外逻辑去阻止新输入提前进来。此外 Half Buffer 组合输出（u10-l2）、面积小，足够此用。

**练习 2**：[Pipeline_Handshake_Multiplier.v:46-47](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Handshake_Multiplier.v#L46-L47) 有一对 `verilator lint_off UNOPTFLAT` / `lint_on`，围住 `output_data_ready_divided`。这条线为什么会被 linter 怀疑是「假组合环」？它真的是环吗？

> **答案**：`output_data_ready_divided` 既喂给 Half Buffer 的 `output_ready`（影响 `output_data_valid`），又由 Pulse_Divider 根据 `module_output_handshake_done`（含 `output_data_valid`）算出——形成 `output_data_valid → module_output_handshake_done → output_data_ready_divided → output_data_valid` 的逻辑环。但这是**握手电路里有意的反馈**（ready 依赖本侧 valid 是允许的，违法的只有 source 的 `ready→valid` 与 destination 的 `valid→ready`——见 u9-l2），不是组合振荡，故用 lint 抑制告警。

**练习 3**：如果 `repeat_count` 在重复期间被上游改了（同一笔输入已握手，上游又送新的 `repeat_count`），会怎样？

> **答案**：不影响本次重复。`repeat_count` 只在**输入握手那一拍**作为 `restart` 被装进 Pulse_Divider（[Pipeline_Handshake_Multiplier.v:99](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Handshake_Multiplier.v#L99)）。之后 Pulse_Divider 自带分频值副本，重复期间上游 `repeat_count` 的任何变化都被忽略，直到下一笔输入握手才重新装载。

---

### 4.3 引擎组装：用构建块搭出大型引擎

#### 4.3.1 概念说明

第三块「最小模块」不是某个具体模块，而是一条贯穿两个主角的**方法论**：本书的复杂引擎几乎不写「随机逻辑」，而是**全部用已测试的构建块实例化拼出**。这是 u4-l1「构建块库」哲学在大型模块上的兑现，也是 u10-l1「数据/控制分离」+「COTTC FSM」的一次完整实战。

对照看两个主角的「自写逻辑」占比：

| 模块 | 实例化的构建块 | 自写的逻辑 |
| --- | --- | --- |
| `Pipeline_Iterator` | `Merge_One_Hot`、`FIFO_Buffer`、`Gate`×2、`Fork_Blocking`×2、`Sink`×3、`Register`×4、`Counter_Binary`×3、`Arbiter_Priority` | 几个 `always @(*)`：算握手事件、算变换、算状态转移、算选源/Sink/Gate 控制 |
| `Pipeline_Handshake_Multiplier` | `Half_Buffer`、`Pulse_Divider` | 一个 `always @(*)` 算三个握手事件 |

注意：两个模块都**没有自己写任何触发器/时序**——所有寄存都在构建块（`Register`/`Counter_Binary`/各缓冲）里。自写的全是组合 `always @(*)`。这正是 u4-l1 主张的「把数据存储与控制交给构建块，主体只做连线与归约」。

#### 4.3.2 核心流程

「引擎组装」有四个反复出现的手法：

1. **握手事件先行**：先在一个 `always @(*)` 里把所有 `*_handshake_done = (valid && ready)` 算出来（Iterator 的 [Pipeline_Iterator.v:664-669](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Iterator.v#L664-L669)、Multiplier 的 [Pipeline_Handshake_Multiplier.v:80-84](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Handshake_Multiplier.v#L80-L84)）。后续所有控制都建立在「握手完成」上，满足 u9-l2「影响接口的状态只在握手完成拍改变」。
2. **Blocking Fork 平衡笔数**：用 Blocking Fork 把一路数据同时送两个去处，靠「两条支路必须同时完成」保证两边笔数恒等，从而**省掉一个计数器**（Iterator 的两个 Fork 各管一组「送给模块 vs 回填 FIFO」的平衡）。
3. **Sink 切断闲置反压**：Blocking Fork 的闲置支路必须 Sink 掉，否则反压住整条 Fork。
4. **隐式门控（Implicit Gating）**：这是 Iterator 里最巧妙的一招——见 4.3.3。

#### 4.3.3 源码精读：隐式门控技巧

[Pipeline_Iterator.v:817-826](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Iterator.v#L817-L826) 的注释描述了一个非显然的技巧。完整逻辑是 [Pipeline_Iterator.v:828-833](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Iterator.v#L828-L833)：

```verilog
sink_fifo_feedback   = (state == MODULE) || (state == OUTPUT);
sink_module_feedback = (state != MODULE);
sink_output          = (state != OUTPUT);
gate_fifo_output     = (data_count_remaining == DATA_COUNT_ZERO);
```

「隐式门控」指的是：在某些状态下，我们**故意不 sink** 某条 Blocking Fork 的支路，因为我们知道那条支路通向 `input_selector` 一个**当前没被选中**的输入。Merge_One_Hot 未被选中的输入其 `ready` 恒为 0，于是那条支路无法完成握手 → 反压住 Blocking Fork → 进而**堵住整条 FIFO 输出**。这就等于在 FIFO 输出处免费得到了一个「门」。

注释 [Pipeline_Iterator.v:245-248](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Iterator.v#L245-L248) 给出具体场景：初始装载（IDLE/LOAD）时不 sink FIFO 反馈支路，它通向当时选中「新输入」的 `input_selector`，于是反馈支路被堵 → Blocking Fork 被堵 → FIFO 输出被有效门控，防止装载期间数据提前漏给外挂模块。

这个技巧说明一个深刻的设计直觉：**在 ready/valid 体系里，「不 ready」本身就是一种控制信号**。与其到处插显式的 `Pipeline_Gate`，不如利用既有的「未选中输入不 ready」性质，让数据通路的拓扑替你做门控。代价是控制逻辑变得隐晦——所以作者特意写长注释提醒读者。

> 复用对照：这个「用 Blocking Fork + Sink 拓扑做隐式控制」的思路，与 u12-l3 里 `Pipeline_Gate`「必须同时门控 valid 与 ready」是同一个原理的不同化身——两者都是靠同时切断两条握手线来干净地冻结一笔传输。

#### 4.3.4 代码实践：盘点 Iterator 的构建块清单

**实践目标**：通过阅读源码，把 `Pipeline_Iterator` 用到的所有构建块列成一张「装配图」，体会「自写逻辑极少、实例化极多」的组装式风格。

**操作步骤**：

1. 打开 [Pipeline_Iterator.v](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Iterator.v)，用搜索定位每一个实例化（关键字是各模块名 + `#(`）。
2. 填下面这张表（实例名 → 模块 → 作用 → 对应章节）：

   | 实例名 | 模块 | 作用 | 前序讲义 |
   | --- | --- | --- | --- |
   | `input_selector` | `Pipeline_Merge_One_Hot` | 3 路选源灌 FIFO | u12-l3 |
   | `data_buffer` | `Pipeline_FIFO_Buffer` | 蓄水 | u12-l1 |
   | `fifo_output_gate` | `Pipeline_Gate` | 数够字数就关 FIFO | u12-l3 |
   | `fifo_fork` | `Pipeline_Fork_Blocking` | FIFO 输出分流 | u12-l2 |
   | `fifo_feedback_sink` | `Pipeline_Sink` | 丢弃 FIFO 反馈 | 本讲 |
   | `module_fork` | `Pipeline_Fork_Blocking` | 模块输出分流 | u12-l2 |
   | `module_feeback_sink` / `output_sink` | `Pipeline_Sink` | 丢弃模块反馈 / 控制输出 | 本讲 |
   | `feedback_type_storage` 等 4 个 | `Register` | 存配置与状态 | u6-l1 |
   | `iteration_counter` / `data_counter` / `data_counter_processed` | `Counter_Binary` | 三个计数器 | u8-l2 |
   | `gate_control` | `Pipeline_Gate` | 门控配置握手 | u12-l3 |
   | `control_goes_first` | `Arbiter_Priority` | 配置/数据握手仲裁 | u11-l1 |

3. 数一数 `always @(*)` 块的数量与职责（算事件、算变换、算转移、算选源、算 Sink/Gate）。确认：**没有任何一个 `always @(posedge clock)`**——所有时序都在构建块里。

**需要观察的现象**：实例化数量约 16 个，自写组合块约 7 个，自写时序块为 0。

**预期结果**：你应当得出「这是个纯组装式设计」的结论——这正是本书大型模块的典型面貌，也是 4.3.1 立论的依据。

#### 4.3.5 小练习与答案

**练习 1**：`Pipeline_Iterator` 里为什么需要**三个** `Counter_Binary`（`iteration_counter`、`data_counter`、`data_counter_processed`）？两两各管什么？

> **答案**：`iteration_counter` 管还剩几轮迭代（驱动 FSM 进 `OUTPUT`）；`data_counter` 管本轮还要从 FIFO 送几个字给模块（驱动 `gate_fifo_output`）；`data_counter_processed` 管模块已经吐回几个字（驱动「本轮结束、进下一轮」）。之所以要把「送出」与「产出」分开计数，是因为外挂模块可能有内部流水线延迟，送完不等价于收完——必须各数各的。

**练习 2**：4.3.3 的「隐式门控」利用了 `Merge_One_Hot` 未选中输入的 ready 恒为 0。这个性质从哪来？为什么它是安全的（不构成违法组合环）？

> **答案**：来自 Merge_One_Hot 的 demux 回 ready 逻辑（u12-l3）：未选中的输入不被授权，ready 为 0。它安全，因为这条「堵」是**单向**的——未选中输入的 ready=0 反压上游 Blocking Fork，是 destination 向 source 施加的正常反压，不构成 source 的 `ready→valid` 或 destination 的 `valid→ready` 违法环（u9-l2）。

**练习 3**：如果让你给 `Pipeline_Handshake_Multiplier` 加一个「重复期间允许提前收下一笔输入并缓存」的功能，你会换掉哪个构建块？为什么？

> **答案**：把 `Pipeline_Half_Buffer` 换成 `Pipeline_FIFO_Buffer`（深度 ≥ 2）或 `Pipeline_Skid_Buffer`。Half Buffer 是半双工（重复完才能收），不支持并发；FIFO/Skid 允许在重复期间继续收输入缓存起来，从而把多笔重复流水化。代价是面积增大、且要重新审视 Pulse_Divider 的 `restart` 时序（每笔输入要重装分频值，不能互相干扰）。

---

## 5. 综合实践

设计一个「**指数计算器**」\(Y = X^{N}\)（N 为正整数，用迭代乘法实现），把本讲的迭代引擎与本书的构建块库串起来。

**场景**：输入一笔被乘数 `X`，输出 `X` 自乘 N 次的结果。用一个「单拍无符号乘法」外挂模块（可直接用 `Adder_Subtractor_Binary` 配合移位，或假定厂商乘法器）作为 `f`。

**任务**：

1. **选引擎并定旋钮**：用 `Pipeline_Iterator` 还是 `Pipeline_Handshake_Multiplier`？给出 `data_count` / `iteration_count` / `feedback_type` 的取值与理由。（提示：每轮把当前累积值乘以 `X`，是迭代还是复制？）
2. **状态向量设计**：Iterator 要求模块输出与输入等宽等数量（[Pipeline_Iterator.v:14-16](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Iterator.v#L14-L16)）。设计你的外挂乘法模块的输入/输出端口形状，使其满足「同形」约束——常数 `X` 怎么在每轮保持可用？（提示：让状态向量包含 `{accumulator, X}` 两个字，每轮 `accumulator_next = accumulator * X`、`X` 原样回传。）
3. **握手时序叙述**：假设 N=5，下游每拍都 ready。从输入握手到 Iterator 输出第一笔结果，大致经过哪些状态（IDLE→LOAD→MODULE×…→OUTPUT→IDLE）？引用 [Pipeline_Iterator.v:742-767](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Iterator.v#L742-L767) 的转移边说明。
4. **边界情形**：N=1 时应当输出 `X` 本身。检查 `iteration_count=1` 时 FSM 是否直接进 `OUTPUT`（看 [Pipeline_Iterator.v:713](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Iterator.v#L713) 与 [Pipeline_Iterator.v:748](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Iterator.v#L748)）。
5. **与 Multiplier 对比**：如果你的需求改成「把 `X` 原样发 N 份给 N 个下游通道」，应该改用哪个模块？为什么 Iterator 不合适？

**参考要点**：

1. 用 **Iterator**。每轮 `accumulator = accumulator * X` 是**迭代**（数据被变换），不是复制。`data_count=2`（`{accumulator, X}`）、`iteration_count = N`、`feedback_type = FEEDBACK_MODULE`。
2. 外挂模块：`to_module_data = {accumulator, X}`，`from_module_data = {product(accumulator*X), X}`——两字同形。`X` 原样回传保证下一轮仍可乘。初始装载 `{1, X}`（累乘器初值 1，乘法单位元）。
3. IDLE（装首字）→ LOAD（装第二字，进 MODULE）→ MODULE 跑 N−1 轮带反馈 → 第 N 轮进 OUTPUT（[Pipeline_Iterator.v:763](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Iterator.v#L763) 的 `run_module_last_processed` 边）→ 送完结果回 IDLE（[Pipeline_Iterator.v:766](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Iterator.v#L766) 的 `run_output_last` 边）。具体拍数待本地仿真确认。
4. `iteration_count=1` 时 [Pipeline_Iterator.v:713](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Iterator.v#L713) 的 `load_data_output` 命中（`iteration_count_remaining == ITER_COUNT_ONE`），直接从 IDLE 进 OUTPUT（[Pipeline_Iterator.v:748](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Iterator.v#L748)），跳过 MODULE 反馈轮——恰好「乘 1 次」出 `X`。✅
5. 改用 **`Pipeline_Handshake_Multiplier`**：它把一笔数据原样重复 N 次，正是「扇出 N 份」。Iterator 会反复变换数据（乘法），不是原样复制，语义不符。

## 6. 本讲小结

- **两种「重复」引擎**：`Pipeline_Iterator` 是**迭代**——每轮用 `f` 变换数据，N 轮得 \(f^{N}(D)\)；`Pipeline_Handshake_Multiplier` 是**复制**——把一笔数据原样发 N 次。前者外挂模块、运行前设重复次数；后者不外挂、逐笔带重复次数。
- **Iterator = 硬件 for 循环**：初始数据装 FIFO → 带反馈地迭代 N−1 轮 → 最后一轮送输出。三个旋钮 `data_count`/`iteration_count`/`feedback_type` 分别定「每轮字数」「轮数」「回填 FIFO 用原始数据还是模块输出」。硬约束：模块输出必须与输入等宽等数量（状态同形）；FIFO 深度必须 ≥ 最大数据集。
- **Iterator 的架构**是 u10-l1 范式的完整实战：数据通路（`Merge_One_Hot`→`FIFO`→`Gate`→两个 `Fork_Blocking`+三个 `Sink`）只认控制信号、不含状态；控制通路（6 态 FSM + 三计数器）用 COTTC 法——先声明数据通路变换（[Pipeline_Iterator.v:705-735](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Iterator.v#L705-L735)），再由变换推出状态转移（[Pipeline_Iterator.v:742-767](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Iterator.v#L742-L767)，链式三元最后赋值胜出）。
- **Multiplier 的机关**：用 `Pipeline_Half_Buffer`（半双工，天然挡住新输入）锁存数据，用 `Pulse_Divider`「数到第 `repeat_count` 次输出握手就放行一拍」回送给 Half Buffer 的 `output_ready`——于是数据在重复期间保持不变、数够才完成输出握手。`repeat_count=0` 走 `clear_input_buffer` 快捷通道。
- **引擎组装主线**：两个主角都**零自写时序**，全部寄存都在构建块里，自写的只是组合 `always @(*)`。四个组装手法：握手事件先行、Blocking Fork 平衡笔数省计数器、Sink 切断闲置反压、**隐式门控**（不 sink 一条通向未选中输入的支路，靠其 ready=0 免费门控 FIFO 输出）。这是 u4-l1「构建块库」与 u10-l1「数据/控制分离」在大型模块上的兑现。
- 这两个引擎本身都带 ready/valid 接口，可被当作更大系统的零件继续组装、乃至嵌套（Iterator 可实现嵌套循环），把本书从「零件库」推向「能跑算法的机器」。

## 7. 下一步学习建议

- 想看「由构建块组装的更大型高速接口引擎」，进入 **u17-l2（串并转换与差分反串行化）**：那里的 `Deserializer_Differential_1toN` 把多个本书构建块（寄存器流水线、IDELAYCTRL）组装成 Xilinx 高速 I/O 接收引擎，是组装思想的另一实战。
- 想夯实「为什么 Half Buffer/Skid Buffer/FIFO 各自适合不同场景」，回看 **u10-l1（Skid Buffer）**、**u10-l2（Half Buffer）**、**u12-l1（FIFO/Credit/Stall Smoother）**——本讲 Multiplier 选 Half Buffer、Iterator 选 FIFO 的理由都建立在那里。
- 想重新理解 Iterator 的 COTTC FSM 设计法，回看 **u10-l1** 的 Skid Buffer 范例与 `fsm.html`：本讲的状态转移链就是把那套方法用到一个更复杂状态机上的结果。
- 若要亲手验证本讲引擎，参考 **u18-l2（仿真、测试台与综合验证）**：用 `Simulation_Clock` 与 `Synthesis_Harness` 给 Iterator 配上控制接口、喂初始数据、外挂一个简单模块（如 `Adder_Subtractor_Binary` 做累加），在仿真里观察 6 个状态的流转与输出波形；也可仿照 `tests/` 用 cocotb 写自检。
