# Muller C 元素与流水线同步

## 1. 本讲目标

学完本讲，你应当能够：

- 用「会合（rendez-vous）」这个更底层的概念解释什么是同步，并说清楚它和 ready/valid 握手的关系。
- 读懂 `Synchronous_Muller_C_Element`：它如何用 set / hold 两个组合函数实现「全 1 则置 1、全 0 则清 0、否则保持」的会合语义，以及为什么把输入和输出一起送进流水线寄存器。
- 读懂 `Pipeline_Synchronizer_Lazy`：它如何用「先 Join 再 Fork、丢掉多余副本」把 N 条 ready/valid 流水线锁步到一起。
- 把本讲的「会合」与上一讲（u9-l2）里 OK_IN/OK_OUT、handshake_complete 的内容串起来。

## 2. 前置知识

在进入本讲前，请先确认你已经了解以下概念（它们在前面讲义中已建立）：

- **ready/valid 握手**：source 驱动 `valid`/`data`，destination 驱动 `ready`，同一拍 `valid && ready` 即为一次握手完成（u9-l1）。
- **handshake_complete 与会合**：握手只是更底层的「会合同步 + 单向数据流」的一种实现；本书用 OK_IN/OK_OUT 表示这种「双方都 OK 才一起前进」的同步（u9-l2）。
- **避免组合环**：接口内部禁止 `valid→ready` 或 `ready→valid` 的组合路径，否则两个接口对接会形成无法分析的组合环（u9-l2）。
- **阻塞/非阻塞赋值、参数默认为 0、复制构造定宽常量**（u2-l2、u3-l1）。
- **优先编码与仲裁器**：上讲（u11-l1）讲的是「多请求争一个资源时挑谁」，本讲讲的是「多方必须同时就位才能前进」，方向不同但都属于控制通路问题。

本讲的核心比喻只有一个词：**会合（rendez-vous）**——多方都到达同一状态才一起放行，谁也不「控制」谁。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [handshake.html](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/handshake.html) | 全书握手正文。本讲引用其中「Underlying Synchronization」一节，定义 OK_IN/OK_OUT 会合。 |
| [Synchronous_Muller_C_Element.v](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Synchronous_Muller_C_Element.v) | 会合的硬件原语：所有输入变 1 输出才变 1，所有输入回 0 输出才回 0。 |
| [Pipeline_Synchronizer_Lazy.v](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Synchronizer_Lazy.v) | 把 N 条 ready/valid 流水线锁步同步：全部就位才一起完成握手。 |
| [Pipeline_Join_Lazy.v](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Join_Lazy.v) | 同步器的输入侧构件：所有输入 valid 才输出 valid。 |
| [Pipeline_Fork_Lazy.v](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Fork_Lazy.v) | 同步器的输出侧构件：所有输出 ready 才回传 input ready。 |

> 本讲的源码引用都基于当前 HEAD `2450a54`。带行号的链接指向具体代码段。

## 4. 核心概念与源码讲解

本讲按「概念 → 原语 → 应用」三层展开，对应三个最小模块：

- **4.1 会合**：从 handshake.html 抽出 OK_IN/OK_OUT 的定义。
- **4.2 Muller C 元素**：会合的硬件实现。
- **4.3 流水线同步器**：会合落到 ready/valid 接口上的 N 路应用。

### 4.1 会合（rendez-vous）：握手之下的同步

#### 4.1.1 概念说明

我们已经习惯用 ready/valid 握手来描述模块间通信。但本书在 handshake.html 里明确指出：ready/valid 只是「**同步（synchronization），也叫会合（rendez-vous）**」这一更根本机制的一种实现。

会合要刻画的是这样一个场景：两个并发运行的模块，有时必须「对齐」一次——比如为了让音频流和视频流同步、为了减少缓冲、为了不让流水线停顿。每个模块都有一个叫 `OK_OUT` 的输出和一个叫 `OK_IN` 的输入，把一方的 `OK_OUT` 接到另一方的 `OK_IN`。当某个模块需要同步，它就把 `OK_OUT` 拉高并保持等待；最终两边会在**同一个时钟周期**同时看到：

\[ \text{同步完成} \;=\; (\text{OK\_IN} == 1) \,\&\&\, (\text{OK\_OUT} == 1) \]

此时双方可以同时改变状态。注意三点：

1. **没有谁是 master/slave**：没有任何一方「控制」另一方，这也就是本书抛弃 master/slave 术语的依据。
2. **没有数据流动**：纯同步。在会合之上叠加单向数据流，就重新发明了 ready/valid 接口。
3. **OK 信号同样要遵守防死锁/防活锁规则**（valid 拉高后必须保持到完成等）。

#### 4.1.2 核心流程

把两个模块的会合画成时序，就是：

```
模块 A：  ...需要同步──> OK_OUT_A=1 并保持 ───────────────> (看到 OK_IN_A=1) 同步达成，改状态
模块 B：  ..............需要同步──> OK_OUT_B=1 并保持 ────> (看到 OK_IN_B=1) 同步达成，改状态
                                        ↑ 同一拍：OK_IN_A==OK_IN_B==1
```

关键性质：**只有当所有相关方的 OK 信号同时为 1，才认为「会合达成」**。这就是后面 C 元素和流水线同步器共同实现的「全员到齐才放行」语义。

#### 4.1.3 源码精读

handshake.html 的「Underlying Synchronization」一节给出 OK_IN/OK_OUT 的定义，并把它与 ready/valid、与本讲的同步器联系起来：

- [handshake.html:203-233](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/handshake.html#L203-L233)：定义会合——两个并发模块各持一个 `OK_OUT`/`OK_IN`，需要同步的一方拉高 `OK_OUT` 并等待，直到**同一拍**双方都看到 `(OK_IN==1) && (OK_OUT==1)`，于是同时改状态。文中强调：没有数据流动、谁也不控制谁。

> 该节最后还埋了一个伏笔：把两路模块输出送进 `Pipeline_Synchronizer_Lazy` 会得到「**三方**同步 + 数据传输」，因为它把下游模块也卷了进来。这正是 4.3 节要讲的。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**，目标是把「会合」这个词和 handshake.html 的描述对上。

1. 实践目标：确认你理解 OK_IN/OK_OUT 的会合判定条件。
2. 操作步骤：打开 [handshake.html:221-233](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/handshake.html#L221-L233)，画出两个模块 A、B 的 OK_OUT 互相接到对方 OK_IN 的连线图。
3. 需要观察的现象：设想 A 在第 3 拍拉高 OK_OUT，B 在第 5 拍拉高 OK_OUT。
4. 预期结果：两人在**第 5 拍**同时看到 `OK_IN==1 && OK_OUT==1`，于是第 5 拍才是会合达成的那一拍。注意 A 在第 3、4 拍虽然自己 OK_OUT=1，但 OK_IN 还是 0，会合尚未达成。

#### 4.1.5 小练习与答案

**练习 1**：会合达成后，是否一定有数据被传输？

> **答案**：不一定。会合本身只做「同步」，不传数据。在会合之上叠加单向数据流，才变成 ready/valid 接口。

**练习 2**：为什么说 OK_IN/OK_OUT 模型是抛弃 master/slave 术语的依据？

> **答案**：因为会合里双方是对等的——一方拉高 OK_OUT 等待，另一方也拉高 OK_OUT 等待，最后同一拍双方同时前进，没有任何一方在「控制」另一方。master/slave 隐含的控制关系在这种对等同步里并不成立。

### 4.2 Muller C 元素：会合的硬件实现

#### 4.2.1 概念说明

**Muller C 元素（Muller C-Element）** 是异步电路文献里的经典原语，正好实现会合/join 语义。它的行为一句话概括：

> 输出保持低，直到**所有输入**都变高；然后输出保持高，直到**所有输入**都回到低。

也就是说，输出不会因为「部分输入变化」而抖动——它只在「全员 1」或「全员 0」这两种一致状态下才翻转，其余情况一律保持原值。这正是「大家都到齐才一起前进」的硬件化身。

本书实现的是**同步（时钟驱动）版本** `Synchronous_Muller_C_Element`，相比原始异步版多了时钟和可选流水线，便于在标准同步 FPGA 设计里使用。它有两个典型用途：

1. **Muller 流水线的控制通路**：一串 C 元素首尾相连，构成一条「有空位就自动前推」的异步风格流水线控制链。
2. **计算屏障（barrier）**：若干独立计算各自跑到某个检查点后拉高自己的输入，停在 C 元素前，等 C 元素输出变高才继续；变低后再来一轮。文档特别提示配合 `Pulse_Generator` / `Pulse_Latch` 处理状态转换。

#### 4.2.2 核心流程

C 元素的状态转移可以用「set / hold」两个布尔函数写清楚。设输入向量为 \(\mathbf{x}\)（共 \(N\) 位），当前输出为 \(q\)，下一拍输出为 \(q^+\)：

\[
\begin{aligned}
\text{set}(\mathbf{x})   &= (\mathbf{x} == \text{全 1}) \\
\text{hold}(\mathbf{x}, q) &= (\mathbf{x} \neq \text{全 0}) \,\&\&\, (q == 1) \\
q^+ &= \text{set}(\mathbf{x}) \;\|\; \text{hold}(\mathbf{x}, q)
\end{aligned}
\]

逐项验证它就是「全员 1 置 1、全员 0 清 0、否则保持」：

| 输入 \(\mathbf{x}\) | 当前 \(q\) | set | hold | 下一拍 \(q^+\) |
| --- | --- | --- | --- | --- |
| 全 1 | × | 1 | × | 1（置 1） |
| 全 0 | × | 0 | 0 | 0（清 0） |
| 混合（非全 0） | 1 | 0 | 1 | 1（保持高） |
| 混合（非全 0） | 0 | 0 | 0 | 0（保持低） |

对于 2 输入的特例，这就是一张很紧凑的真值表：\(a=b=1 \Rightarrow q^+=1\)；\(a=b=0 \Rightarrow q^+=0\)；\(a \neq b \Rightarrow q^+=q\)。

同步版的关键技巧：**把所有输入连同组合输出本身一起送进流水线寄存器**。这样输出 \(q\) 的反馈回路经过寄存器，整个 C 元素变成「输出寄存 + 可重定时」的同步电路；`PIPE_DEPTH=1` 时相当于一个输出寄存器，但反馈环内部仍可被综合器重定时（retiming）以提升频率。

#### 4.2.3 源码精读

先看端口：参数 `INPUT_COUNT`（输入路数）、`PIPE_DEPTH`（流水线深度，最小 1）；端口有 `clock`/`clear`/`clock_enable`、`lines_in`（位宽 = `INPUT_COUNT`）和单比特 `line_out`。

- [Synchronous_Muller_C_Element.v:31-43](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Synchronous_Muller_C_Element.v#L31-L43)：模块端口声明。注意 `line_out` 是 `output reg`，因为它由本模块组合逻辑驱动。

- [Synchronous_Muller_C_Element.v:45-53](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Synchronous_Muller_C_Element.v#L45-L53)：定义定宽常量 `INPUT_ZERO`/`INPUT_ONES`（用 `{N{1'b0}}` 复制构造，呼应 u2-l2 的位宽匹配规矩），以及流水线总宽 `PIPE_WIDTH = INPUT_COUNT + 1`（多出的 1 位装反馈的 `line_out`）。`initial` 把 `line_out` 初始化为 0，避免 X 传播（呼应 u2-l1）。

- [Synchronous_Muller_C_Element.v:55-83](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Synchronous_Muller_C_Element.v#L55-L83)：核心机关。实例化一个 `Register_Pipeline`（u6-l2 讲过），把**输入与输出拼接在一起**整体打 `PIPE_DEPTH` 拍：

  ```verilog
  .pipe_in  ({lines_in, line_out}),         // 当前输入 + 当前输出，一起进流水线
  .pipe_out ({lines_in_pipelined, line_out_pipelined})
  ```

  注释点明：把输出也流水线，是为了「允许前向重定时」并「保存输出状态（使本版本成为同步的）」。

- [Synchronous_Muller_C_Element.v:85-94](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Synchronous_Muller_C_Element.v#L85-L94)：组合逻辑计算 set/hold，注意它们用的是**流水线之后**的 `lines_in_pipelined` 和 `line_out_pipelined`，并且全部写成**等式比较**（`== INPUT_ONES`、`!= INPUT_ZERO`、`== 1'b1`），最后用三元/逻辑或汇出 `line_out`——这正是 u3-l1 主张的「布尔式写成等式比较、链式三元、最后赋值胜出」风格：

  ```verilog
  set_output  = (lines_in_pipelined == INPUT_ONES);
  hold_output = (lines_in_pipelined != INPUT_ZERO) && (line_out_pipelined == 1'b1);
  line_out    = (set_output == 1'b1) || (hold_output == 1'b1);
  ```

#### 4.2.4 代码实践

1. 实践目标：手算验证 set/hold 公式确实等于「全员翻转才翻转」。
2. 操作步骤：取 `INPUT_COUNT = 2`，列出 \((a,b)\) 的 4 种组合 × 当前 \(q \in \{0,1\}\) 共 8 行，按 [4.2.3 的源码公式](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Synchronous_Muller_C_Element.v#L85-L94) 计算 `set_output`、`hold_output`、`line_out`。
3. 需要观察的现象：当 \((a,b)=(1,0)\) 且 \(q=1\) 时，`hold_output` 是否为 1？当 \((a,b)=(0,0)\) 时 `hold_output` 是否被强制为 0？
4. 预期结果：除 \((1,1)\) 置 1、\((0,0)\) 清 0 外，其余两行 \(q^+=q\)（保持）。这印证了「部分输入变化不会让输出抖动」。
5. 进阶（待本地验证）：把 `INPUT_COUNT=2`、`PIPE_DEPTH=1` 的实例放进一个最小测试台，先令两输入同时从 0→1，观察 `line_out` 在下一拍变 1；再令其一回 0，确认 `line_out` 保持 1；两输入都回 0，`line_out` 才回 0。

#### 4.2.5 小练习与答案

**练习 1**：如果把 C 元素的输出直接接到自己的一个输入（自反馈），会得到什么行为？

> **答案**：会得到一个对其他输入「带锁存」的多数/一致检测。当其余输入都为 1 时 set 生效锁住 1，都为 0 时清 0，单个输入变化不影响——本质上输出会被「全员一致」锁存，单个输入抖动被滤除。这正是 C 元素能做去毛刺/事件合并的原因。

**练习 2**：为什么同步版要把 `line_out` 也送进 `Register_Pipeline`，而不是只把输入打一拍、组合输出再单独寄存？

> **答案**：把输入和输出放在同一条流水线里，反馈回路（`line_out → pipe_in → pipe_out → 组合逻辑 → line_out`）整体可被综合器重定时，便于在大输入数、组合路径较长时通过调 `PIPE_DEPTH` 满足时序；同时也让输出状态的保存与输入延迟严格对齐，行为可预测。

### 4.3 流水线同步器：N 路 ready/valid 会合

#### 4.3.1 概念说明

C 元素是「裸」会合原语（一组 `lines_in` + 一个 `line_out`）。而在 ready/valid 的世界里，我们经常需要把**多条独立流水线**锁步到一起——典型例子是「地址」和「数据」分别来自两个源，必须成对、同时写进存储器。

`Pipeline_Synchronizer_Lazy` 做的就是这件事：接收两条及以上 ready/valid 流水线，强制它们**只能同时完成握手**，并把同步后的握手转发出去。结果是数据被按 FIFO 顺序、按锁步（lock-step）方式消费。

名字里的 **Lazy** 很关键：它表示「**无缓冲、纯组合**」。与之相对的是带缓冲的版本（见 u12）。无缓冲意味着它面积小、延迟低，但也意味着你必须自己小心组合路径长度与组合环——这一点文档反复警告。

#### 4.3.2 核心流程

同步器的实现思路非常优雅，一句话：**先 Join 再 Fork，然后丢掉多余副本**。

```
N 路 ready/valid 输入
        │
        ▼
  Pipeline_Join_Lazy   ── 所有输入 valid 才输出 valid；
   (输入侧会合)            所有输入的 ready 同时 = 下游 ready
        │  （合流成一条「更宽」的握手）
        ▼
  Pipeline_Fork_Lazy   ── 所有输出 ready 才回传 input ready；
   (输出侧会合)            把这一份握手复制成 N 份输出
        │
        ▼
  丢掉 N-1 份重复数据，只留第一份 ──> output_data
```

由此得到两条核心性质：

1. **输入侧锁步**：`input_data_ready[i]` 只有在**所有** `input_data_valid` 都为 1（且下游就绪）时才会拉高。所以任何一条输入流都不能「抢先」完成握手。
2. **输出侧锁步**：同步后的 N 路输出也必须**同时**被下游接收（所有 `output_data_ready` 为 1）才推进。

这正是把 4.1 的「会合」翻译成了 ready/valid 语言：valid 全员到齐 ≈ OK_OUT 全员到齐；ready 全员到齐 ≈ 放行信号同时给出。

#### 4.3.3 源码精读

先看同步器本体如何用 Join + Fork 拼装：

- [Pipeline_Synchronizer_Lazy.v:22-44](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Synchronizer_Lazy.v#L22-L44)：参数 `WORD_WIDTH`、`PORT_COUNT`；端口是 N 路 `input_data_ready/valid` 加拼接的 `input_data`，以及对称的 N 路 `output_data_ready/valid` 与 `output_data`。注意 `PORT_WIDTH_TOTAL = WORD_WIDTH * PORT_COUNT` 是「**不要在实例化时设置**」的派生参数（IPI 除外），呼应 u2-l2。

- [Pipeline_Synchronizer_Lazy.v:46-66](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Synchronizer_Lazy.v#L46-L66)：**输入 Join**。实例化 `Pipeline_Join_Lazy`，把 N 路输入合成一个更宽的握手。这一步实现了「所有输入 valid 才有效」。

- [Pipeline_Synchronizer_Lazy.v:68-99](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Synchronizer_Lazy.v#L68-L99)：**输出 Fork**。实例化 `Pipeline_Fork_Lazy`，把合流后的握手复制成 `PORT_COUNT` 份。由于「N 份 × N 路输入」会产生重复数据，文档解释：**丢弃除第一份外的所有副本，只保留控制信号**：

  ```verilog
  output_data = output_data_with_duplicates [0 +: PORT_WIDTH_TOTAL]; // 只留第一份
  ```

  这里的 `0 +: PORT_WIDTH_TOTAL` 是 u5-l1 见过的变址部分位选，取出拼接向量的第一段。

再看 Join / Fork 内部各只有几行组合逻辑（它们就是 Lazy 的全部「肌肉」）：

- [Pipeline_Join_Lazy.v:55-62](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Join_Lazy.v#L55-L62)：`output_valid = (input_valid == INPUT_ONES)`（全员 valid 才有效）；`input_ready` 在 `output_valid` 为 1 时把下游的 `output_ready` 广播给所有输入，否则全 0——这就是「输入侧会合」。

- [Pipeline_Fork_Lazy.v:48-56](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Fork_Lazy.v#L48-L56)：`input_ready = (output_ready == OUTPUT_ONES)`（所有输出就绪才回传 ready）；`output_valid_gated = (input_valid && input_ready)` 即 handshake_complete，再广播给所有输出——这就是「输出侧会合」。

文档（[Pipeline_Synchronizer_Lazy.v:13-16](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Synchronizer_Lazy.v#L13-L16)）特别警告：`input_data_ready` 是一条「`所有 valid 都为 1` 才拉高」的**组合路径**，且没有缓冲——对接的两端如果都存在组合 ready/valid 路径，就会形成组合环。这就是 Lazy 版必须谨慎使用、否则改用缓冲版的原因。

#### 4.3.4 代码实践

1. 实践目标：用「两路」同步器把「地址」和「数据」锁步成一个写事务，验证只有两路同时 valid 才会完成握手。
2. 操作步骤：
   - 阅读 [Pipeline_Synchronizer_Lazy.v:46-99](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Synchronizer_Lazy.v#L46-L99)，确认它由一个 Join 加一个 Fork 组成。
   - 设 `PORT_COUNT = 2`：第 0 路接「地址」，第 1 路接「数据」。
   - 在脑中（或测试台里）令地址路 `valid=1`、数据路 `valid=0`。
3. 需要观察的现象：此时 `input_data_ready` 是否为 0？地址路能否单独完成握手？
4. 预期结果：因为 Join 要求 `input_valid == 全 1`，数据路 valid=0 时 `output_valid=0`，进而两路的 `input_data_ready` 都是 0，地址路被「卡住」等待数据路。只有两路都 valid（且下游 ready）才一起握手——这正是锁步写事务要的效果。
5. 进阶（待本地验证）：把下游两路的 `output_data_ready` 设成不同值（一个 1 一个 0），观察 Fork 是否也要求「全员 ready」才回传 input ready，从而把上游也卡住。

#### 4.3.5 小练习与答案

**练习 1**：同步器为什么要「先 Join 再 Fork」，反过来「先 Fork 再 Join」行不行？

> **答案**：Join 把 N 路输入收敛成一份「全员到齐才有效」的握手，是实现输入侧锁步的前提；Fork 再把这一份同步好的握手扇出给 N 路下游，实现输出侧锁步。反过来先 Fork 会把「尚未同步」的单路握手各自扇出，无法保证 N 路同时完成，锁步语义就丢了。

**练习 2**：Lazy 版同步器没有寄存器，为什么文档反复强调「当心组合环」？

> **答案**：因为 `input_data_ready` 是 `所有 input_data_valid 为 1` 的纯组合函数，且 Fork 的 `input_ready` 也是 `所有 output_ready 为 1` 的纯组合函数。如果对接的上下游接口内部也存在 `valid→ready` 或 `ready→valid` 的组合路径，连起来就会构成无法做时序分析、也无法可靠仿真的组合环（u9-l2 的铁律）。所以要么确保整条链上没有这种组合反馈，要么改用带缓冲的版本。

## 5. 综合实践

把三个最小模块串起来：**用 Muller C 元素的思路，理解 Pipeline_Synchronizer_Lazy 如何实现 N 路 ready/valid 会合**。

任务分解：

1. **对照概念**：写出「4.1 会合」的判定式 \((\text{OK\_IN} \&\& \text{OK\_OUT})\)，并指出在同步器里，`input_data_valid 全员为 1` 扮演了哪一方的 OK 信号、`input_data_ready` 又扮演了哪一方。（提示：valid 全员到齐 ≈ 发起方的 OK_OUT；ready 在全员到齐后同时给出 ≈ 接收方的 OK。）
2. **对照实现**：阅读 [Synchronous_Muller_C_Element.v:85-94](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Synchronous_Muller_C_Element.v#L85-L94) 的 `set`（`== INPUT_ONES`）与 [Pipeline_Join_Lazy.v:55-58](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Join_Lazy.v#L55-L58) 的 `output_valid = (input_valid == INPUT_ONES)`。说明二者在「全员为 1 才放行」这一点上是同一个思想的不同表达（一个是状态锁存的原语，一个是组合的握手门控）。
3. **指出差异**：C 元素是**有状态**的（输出会保持，靠 hold 函数和寄存器），而 Lazy 同步器是**无状态纯组合**的（不锁存，靠 Join/Fork 的组合逻辑当场判定）。说明这个差异带来的后果：C 元素能做异步风格的「等到齐再统一前推」并容忍输入不同时到达；Lazy 同步器则要求 valid 在等待期间持续保持，否则无法「记住」谁先到。
4. **画出三方同步**：根据 [handshake.html:235-238](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/handshake.html#L235-L238) 的提示，画出「两路上游 + 一路下游」经同步器形成的三方会合示意图，标注 valid/ready 的流向。

> 如果有本地仿真环境，可把 `PORT_COUNT=2` 的同步器接上两个简单的 valid 源和一个 ready 源，验证「两路 valid 不同时拉高时绝无握手完成」。若无法运行，明确标注「待本地验证」，仅完成上述阅读与画图分析即可。

## 6. 本讲小结

- **会合（rendez-vous）** 是 ready/valid 握手之下的更根本机制：相关方都拉高各自的 OK 信号，在同一拍同时看到 `OK_IN && OK_OUT` 才一起前进，没有 master/slave、也不必传数据。
- **Muller C 元素** 是会合的硬件原语：`set`（全 1 置 1）+ `hold`（非全 0 且当前为 1 则保持）实现「全员一致才翻转、否则锁存」；同步版把输入与输出一起送进 `Register_Pipeline`，使反馈回路可重定时。
- **Pipeline_Synchronizer_Lazy** 用「先 Join 再 Fork、丢掉多余副本」把 N 条 ready/valid 流水线锁步：输入侧靠 `Join` 要求全员 valid，输出侧靠 `Fork` 要求全员 ready。
- **Lazy = 无缓冲纯组合**：面积小但 `input_data_ready` 是组合路径，必须警惕组合环，必要时改用缓冲版。
- C 元素是**有状态**会合，Lazy 同步器是**无状态组合**会合——前者能记住「谁先到」，后者要求 valid 持续保持。
- 本讲把 u9-l2 的 OK_IN/OK_OUT 落到了具体模块，并为 u12 的 Fork/Join/Merge 家族提供了「会合」这一统一视角。

## 7. 下一步学习建议

- 继续阅读 [Pipeline_Join](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Join_Lazy.v) 与 [Pipeline_Fork](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Fork_Lazy.v) 的**带缓冲**版本（u12-l2），理解 Lazy 与 buffered 的取舍。
- 学习 u12 的 `Pipeline_Merge_*` 家族，看仲裁器（u11-l1）如何与会合结合，实现「多路合流 + 公平调度」。
- 回到 `Synchronous_Muller_C_Element` 文档提到的 **Muller 流水线**，结合 `Pulse_Generator`/`Pulse_Latch`（u15-l1）体会异步风格控制链。
- 若对 CDC 感兴趣，可对比本讲的「同时钟域会合」与 u13/u14 的「跨时钟域同步」——前者解决「对齐」，后者解决「亚稳态」，是两类不同的同步问题。
