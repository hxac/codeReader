# Proc、Channel 与状态化通信

## 1. 本讲目标

学完本讲，你应该能够：

- 说清楚 **Proc（进程）** 与前面讲的 **Function（函数）** 的本质区别：为什么纯组合的 Function 不够用，必须有 Proc 来表达「带状态、能随时间迭代、能与外部通信」的硬件。
- 读懂一个完整的 DSLX Proc：`config`（接口配置）、`init`（初始状态）、`next`（每次激活的行为）三件套，以及它如何被 `spawn` 成网络。
- 理解 **Channel（通道）** 作为 Proc 之间通信的唯一手段：通道的方向、类型、流控（`FlowControl`）、严格性（`ChannelStrictness`），以及 `recv`/`send` 在 IR 层对应的节点。
- 理解 **状态寄存器（StateElement）**：状态如何用初值 + 递推关系建模「随时间归纳」的计算，并在 IR 层对应 `state_read` / `next_value` 节点。
- 能把一个 `.x` 里的 Proc 翻译成 `.ir` 文本，并指出其中每个状态元素与每次收发。

## 2. 前置知识

本讲建立在 **u3-l1（IR 总览）** 之上，假定你已经知道：

- XLS IR 是一张「数据流图 + 类型化值」的 SSA 图，由 `Package` 容器装着若干可计算单元。
- `Function` 是一种可计算单元：`f(输入) → 输出`，**一次性、无状态、纯组合**，对应一段组合逻辑电路。
- IR 的每个顶点是一个 `Node`，靠 `Op` 枚举标识语义，靠「操作数/users」表达数据依赖。

本讲要回答的问题是：现实硬件里到处是**寄存器（状态）**和**数据流（一个像素、一个采样、一个网络包接一个）**，纯组合的 `Function` 表达不了「上一个时钟周期的结果影响这一个周期」「从流水线上一级收数据再发给下一级」。XLS 用 **Proc** 来填补这个缺口。

补两个本讲要用到的概念：

- **Kahn 进程网络（Kahn Process Network, KPN）**：一种并发计算模型，由若干独立计算单元通过单向通道通信，接收方在数据就绪前阻塞。XLS 的 Proc 语义以 KPN 为基础，强调「与时序无关（timing-insensitive）」——只要输入的*顺序*不变，结果就与具体到达时刻无关，这能让综合出的硬件更容易做正确性保证。
- **激活（activation）**：Proc 不是「被调用一次」，而是「被反复激活」。在硬件里每个时钟周期最多激活一次；每次激活读输入、算一拍、写输出、更新状态，然后等待下一次激活。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `docs_src/tutorials/what_is_a_proc.md` | 官方入门教程，用通俗语言讲清 Proc/Channel/State/Token 概念，是本讲概念部分的主要依据。 |
| `xls/examples/proc_iota.x` | 一个最小的「生产者—通道—消费者」Proc 网络，演示 `spawn`、`chan`、`recv`/`send`、状态递推。 |
| `xls/ir/proc.h` | IR 层的 `Proc` 类定义，`Proc` 继承 `FunctionBase`，持有状态元素、通道接口、子 Proc 实例化。 |
| `xls/ir/channel.h` | `Channel` 及其子类（`StreamingChannel`/`SingleValueChannel`）、`ChannelInterface`、`FlowControl`、`ChannelStrictness` 等通道相关数据结构。 |
| `xls/ir/state_element.h` | `StateElement`：一个状态寄存器的「形状 + 初值」。 |
| `xls/ir/nodes.h` | `StateRead`、`Next`、`Receive`、`Send` 四个 Proc 专用 IR 节点。 |

本讲的三个最小模块：**4.1 Proc 定义**、**4.2 Channel 通信**、**4.3 状态寄存器**，分别对应 Proc 的「骨架」「通信」「记忆」。

## 4. 核心概念与源码讲解

### 4.1 Proc 定义

#### 4.1.1 概念说明

前面所有讲义里的可计算单元都是 `Function`——给它一组输入，算出一组输出，**不记得过去，也不影响未来**。这对应一段纯组合逻辑。

但很多硬件不是这样的：

- 一个**累加器**要记得「上一次累加到了多少」。
- 一个**有限脉冲响应（FIR）滤波器**要记得过去 N 个输入样本。
- 一个**流水线级**要从上一级收数据，处理完发给下一级。

这些都共同需要两样东西：**状态**（寄存器）和**通信**（数据流）。XLS 用 **Proc** 同时提供这两者。

官方教程开宗明义：

> **Procs**, short for "communicating sequential processes", are the means by which DSLX models sequential and stateful modules. … Each proc has a fixed set of I/O interfaces (aka *channels*, usually FIFO queues), a fixed amount of memory (aka *state*), and the ability to carry out a bounded amount of computation on their state & inputs whenever they activate.

翻译过来就是：Proc = 通信顺序进程，是一个「有固定 I/O 接口（通道）、固定容量记忆（状态）、每次激活做有界计算」的单元。它是 DSLX 表达时序与状态化模块的手段。参见 [what_is_a_proc.md:7-16](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/docs_src/tutorials/what_is_a_proc.md#L7-L16)。

理解 Proc 的关键心智模型是 **「随时间归纳」**：把每个状态元素看作数学归纳法里的归纳变量 \( s \)，每次激活就是一步归纳

\[
s_{n+1} = f(s_n,\ \text{in}_n), \qquad \text{out}_n = g(s_n,\ \text{in}_n)
\]

`init` 给出归纳起点 \( s_0 \)，`next` 给出归纳步 \( f \)。整段 Proc 就是在对「时间」这个维度做归纳——这正是它能表达时序电路的根本原因。

Function 与 Proc 的对照：

| 维度 | Function（函数） | Proc（进程） |
|------|----------------|-------------|
| 状态 | 无 | 有若干状态元素（寄存器） |
| 执行 | 调用一次，返回一次 | 反复激活，永不「返回」 |
| 通信 | 仅靠参数/返回值 | 靠 Channel 收发数据流 |
| 对应电路 | 纯组合逻辑 | 时序逻辑（含寄存器与握手） |
| IR 中基类 | `Function : FunctionBase` | `Proc : FunctionBase` |

#### 4.1.2 核心流程

一个 DSLX Proc 由四部分组成，看 `proc_iota.x` 里的 `producer` 就一目了然：

```dslx
proc producer {
    s: chan<u32> out;                              // ① 通道声明（proc 作用域内）

    config(input_s: chan<u32> out) { (input_s,) }  // ② 接口配置

    init { u32:0 }                                 // ③ 初始状态

    next(i: u32) {                                 // ④ 每次激活的行为
        let foo = i + u32:1;
        let tok = send(join(), s, foo);
        foo                                        // 最后一行 = 下一次激活看到的状态
    }
}
```

四个要件的含义：

1. **通道声明**：`s: chan<u32> out;` 声明这个 Proc 对外有一条「输出 u32」的通道。新式（new-style）Proc 把通道声明在 Proc 作用域里。
2. **`config(...)`**：当这个 Proc 被 `spawn`（实例化）时调用一次，用来把外部传入的通道接线连到本 Proc 的通道上。它返回一个元组，把形参绑定到声明里同名通道——这就是「接线」。
3. **`init { ... }`**：状态元素的初值。这里只有一个状态元素，初值是 `u32:0`。第一次激活看到的 `i` 就是这个初值。
4. **`next(st) { ... }`**：每次激活执行的代码。形参 `st` 是「上一次激活结束时写入的状态值」；函数体最后一个表达式就是「本次激活结束时要写入的新状态值」，下一拍会被读回来。

注意 `next` 不像 `Function` 有 `return`——它「返回」的不是给调用者的结果，而是**给未来自己的下一状态**。函数体中间的 `send`/`recv` 产生的对外效果（发数据、收数据）才是它对世界的「输出」。

**Proc 网络与 `spawn`**：Proc 不是孤立运行的。`proc_iota.x` 的 `main` 展示了如何把多个 Proc 连成网络：

```dslx
proc main {
    config() {
        let (s, r) = chan<u32, u32:1>("my_chan");   // 创建一条通道，拿到发送端 s 和接收端 r
        spawn producer(s);                          // 实例化 producer，把 s 接给它
        spawn consumer<u32:2>(r);                   // 实例化 consumer，把 r 接给它
        ()
    }
    init { () }
    next(state: ()) { () }
}
```

`chan<u32, u32:1>(...)` 创建一条类型为 `u32`、FIFO 深度为 1 的通道，返回一对 `(发送端, 接收端)`。`spawn` 把通道端点作为 `config` 的实参传给子 Proc，从而把 `producer` 和 `consumer` 用一条通道串起来。`main` 本身没有状态也不做计算，只起「组网」作用。

#### 4.1.3 源码精读

**IR 层的 `Proc` 类。** 在内存里，Proc 和 Function 是「同源兄弟」——都继承自 `FunctionBase`，靠 `Kind` 区分。`proc.h` 的类注释点明了 Proc 的定位：

> Abstraction representing an XLS Proc. Procs (from "processes") are stateful blocks which iterate indefinitely over mutable state of a fixed type. Procs communicate to other components via channels.

见 [proc.h:49-52](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/proc.h#L49-L52)，类声明见 [proc.h:53-57](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/proc.h#L53-L57)。`kind()` 直接返回 `FunctionBase::Kind::kProc`，这是 IR 中区分 Function/Proc/Block 的依据（[proc.h:294](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/proc.h#L294)）。

**状态与通道的存放。** `Proc` 比 `Function` 多出来的核心字段就是「状态元素表」和「通道/实例化表」（[proc.h:432-458](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/proc.h#L432-L458)）。状态元素用「按名哈希表 + 保序向量」双重索引（`state_elements_` / `state_vec_`），既支持按名查找又支持稳定遍历；通道接口、子 Proc 实例化（对应 DSLX 的 `spawn`）各用一组 `unique_ptr` 容器持有。

**新旧两种 Proc 风格。** XLS 正在从「全局通道」迁移到「Proc 作用域通道」（new-style proc）。`is_new_style_proc()` 标志位（[proc.h:296-297](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/proc.h#L296-L297)）区分二者；新式 Proc 才能用 `channels()`、`interface()`、`AddChannel()` 等 API（[proc.h:305-321](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/proc.h#L305-L321)）。DSLX 前端生成的 Proc 都是新式 Proc，`config` 里那些「通道形参」就是新式 Proc 的接口。

**`spawn` 在 IR 中。** DSLX 的 `spawn producer(s)` 在 IR 层对应一个 `ProcInstantiation`（子 Proc 实例化），由 `AddProcInstantiation` 添加（[proc.h:378-380](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/proc.h#L378-L380)）。它记录「被实例化的 Proc + 传入的通道实参」，是 Proc 层级（proc hierarchy）的来源。

#### 4.1.4 代码实践

**目标**：把 `proc_iota.x` 翻译成 IR 文本，亲眼看到一个 Proc 在 `.ir` 里长什么样。

**步骤**：

1. 用 IR 转换器把示例转成 IR（在仓库根目录）：

   ```bash
   bazel run -c opt //xls/dslx/ir_convert:ir_converter_main -- \
       $(pwd)/xls/examples/proc_iota.x
   ```

2. 在输出里找到三段 `proc ...` 开头的块，分别对应 `producer`、`consumer`、`main`。

3. 重点观察 `main`：它应该没有 `send`/`receive`/`next_value` 之类的计算节点，只有 `spawn`（实例化）和通道声明——印证「`main` 只负责组网」。

**需要观察的现象**：
- 每个 `proc` 的签名形如 `proc 名字(token, state... , init={...}) { ... }`，括号里第一个是 token，后面是状态元素及其初值。
- `producer`/`consumer` 里会出现 `send`、`receive`、`state_read`、`next_value`（或旧式 `next`）节点。

**预期结果**：能从 `.ir` 文本中区分出三个 Proc，并指出每个 Proc 的状态元素名与初值。

**说明**：本实践的命令是否能在你的环境一次跑通取决于 Bazel 构建状态，输出格式「待本地验证」；即便不运行，按上面步骤阅读 `ir_converter_main` 产出的 `.ir` 也能完成对照。

#### 4.1.5 小练习与答案

**练习 1**：`producer` 的 `next` 里，最后一行 `foo` 没有任何关键字（如 `return`）。它的作用是什么？

**参考答案**：它是「下一状态值」。`next(i)` 的形参 `i` 是上一拍写入的状态，函数体最后一个表达式 `foo` 会被写入状态寄存器，成为下一拍 `i` 读到的值。所以 `i` 每拍递增 1（`foo = i + 1`），形成 0→1→2→… 的计数序列。

**练习 2**：`main` 这个 Proc 有 `init { () }` 和 `next(state: ()) { () }`，它真的「什么都不做」吗？它存在的意义是什么？

**参考答案**：从计算上看它确实没有状态、不收发数据；它的意义是**组网**——在 `config` 里创建通道、`spawn` 子 Proc 并接线。它充当整个 Proc 网络的「顶层壳」，让 `producer` 和 `consumer` 通过 `my_chan` 连起来。

---

### 4.2 Channel 通信

#### 4.2.1 概念说明

Proc 要协作就得通信，而**通信的唯一手段是 Channel（通道）**。可以把通道想象成一条连接两个 Proc 的单向传送带：一端 `send`（放上去），另一端 `recv`（取走）。

通道有两个关键属性：**方向**与**种类**。

- **方向**：站在某个 Proc 的视角，通道要么是 `in`（只能 `recv`），要么是 `out`（只能 `send`）。同一条物理通道对发送方是 `out`、对接收方是 `in`。`proc_iota.x` 里 `producer` 的 `s: chan<u32> out` 与 `consumer` 的 `r: chan<u32> in` 其实是同一条 `my_chan` 的两端。

- **种类（`ChannelKind`）**：教程说「目前只讲标准的 streaming 通道」。XLS 支持两种：
  - `kStreaming`（默认）：FIFO 语义，`send` 入队、`recv` 出队，先进先出，可带深度。
  - `kSingleValue`：只存「最近一次写入的值」，`recv` 是非破坏性读取，常用于配置寄存器一类的「总是反映最新值」的场景。

**阻塞语义**：这是理解 Proc 行为的要点（[what_is_a_proc.md:64-83](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/docs_src/tutorials/what_is_a_proc.md#L64-L83)）：

- `recv` **默认阻塞（blocking）**：通道里没数据时，本次激活会**停顿（stall）**，直到数据就绪才继续。这正是 KPN「数据就绪前等待」的体现。
- `send` **默认非阻塞**：XLS 在高层把通道建模成「无限深队列」，所以 `send` 总能立刻完成。
- 但生成 RTL 时 FIFO 是有限深的，于是存在**背压（backpressure）**：接收方来不及取时，发送方的 `send` 也会 stall。默认 XLS 允许这种背压以保证正确性。

#### 4.2.2 核心流程

一次典型的「收—算—发」激活流程（以教程里的 `adder` 为例，[what_is_a_proc.md:95-117](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/docs_src/tutorials/what_is_a_proc.md#L95-L117)）：

```
开始激活
  │
  ├─ recv(join(), A)  ──等 A 有数据──▶ 得到 (tok_A, data_A)
  ├─ recv(tok_A, B)   ──等 B 有数据──▶ 得到 (tok_B, data_B)
  │        （第二个 recv 依赖第一个的 token，故保证顺序）
  ├─ sum = data_A + data_B
  └─ send(tok, C, sum) ──把 sum 发到 C──▶ 得到新 token
结束激活，下一拍重复
```

这里出现的 `join()`、`tok_A`、`tok` 是 **token（令牌）**。它不是真实数据，而是一根「顺序依赖」的线：当一个操作必须排在另一个操作之后、但二者之间又没有真实数据流动时（比如「先发请求再收响应」），就用 token 串起先后。`join(tok...)` 把多个 token 合成一个，表示「等这些全完成」。`join()`（空参）则是一个「不依赖任何东西」的起始 token。详见 [what_is_a_proc.md:291-318](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/docs_src/tutorials/what_is_a_proc.md#L291-L318)。

> 小贴士：`send`/`recv` 当前都要求显式带 token，这是过渡期的语法要求；未来 XLS 计划只在必要时才用 token。

除无条件收发外，XLS 还支持**条件收发**：`recv_if(tok, ch, pred, default)` 仅当 `pred` 为真才真正接收，否则返回 `default`；`send_if(tok, ch, pred, data)` 仅当 `pred` 为真才发送。这让你能写出「根据输入决定要不要从某通道取数」的逻辑（见 [what_is_a_proc.md:130-184](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/docs_src/tutorials/what_is_a_proc.md#L130-L184) 的 `fallback` 例子）。

#### 4.2.3 源码精读

**`ChannelKind` 枚举**：明确两种通道语义（[channel.h:43-51](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/channel.h#L43-L51)）。`kStreaming` 注释强调 FIFO，`kSingleValue` 注释强调「覆盖式写、非破坏性读」。

**`Channel` 抽象基类**：它描述「通信如何发生」。类注释说得很清楚——send/receive 节点关联到某个 channel，channel 携带「通信如何进行」的信息（[channel.h:214-219](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/channel.h#L214-L219)）。核心字段：`name_`（通道名）、`id_`（包内唯一 ID）、`supported_ops_`（仅发/仅收/可发可收）、`kind_`、`type_`（承载的数据类型）、`initial_values_`（初始预置值）。`CanSend()`/`CanReceive()` 据 `supported_ops_` 判断（[channel.h:238-245](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/channel.h#L238-L245)）。

**两个具体子类**：

- `StreamingChannel`（[channel.h:385-417](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/channel.h#L385-L417)）：在 `Channel` 基础上多了 `ChannelConfig`（含 `FifoConfig` 深度等）、`FlowControl`、`ChannelStrictness`。
- `SingleValueChannel`（[channel.h:419-428](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/channel.h#L419-L428)）：无初始值、无状态。

**流控 `FlowControl`**（[channel.h:299-323](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/channel.h#L299-L323)）：决定通道如何 lower 成 Verilog 的握手协议。`kReadyValid`（默认）是标准的 ready/valid 双线握手；`kValidData` 只有 valid、假定接收方永远就绪（不支持背压）；`kNone` 不做流控、由外部保证。默认值 `kDefaultChannelFlowControl = kReadyValid`（[channel.h:322-323](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/channel.h#L322-L323)）。

**严格性 `ChannelStrictness`**（[channel.h:337-359](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/channel.h#L337-L359)）：当**同一通道**在一个激活里被多次操作（目前 DSLX 默认要求「每通道每激活至多一次」，但可 opt-in 放开）时，调度器需要合法化这些操作——靠形式化证明（`kProvenMutuallyExclusive`，默认）、运行时断言、或静态指定优先级。默认值 `kProvenMutuallyExclusive`（[channel.h:358-359](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/channel.h#L358-L359)）。

**方向与「通道接口」**：`ChannelDirection` 只有 `kSend`/`kReceive` 两值，`InvertChannelDirection` 取反（[channel.h:436-449](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/channel.h#L436-L449)）。新式 Proc 里，`send`/`receive` 节点引用的是 **`ChannelInterface`**（通道接口）而非 `Channel` 对象本身——接口是「Proc 看到的那一端」，在实例化（elaboration）时才绑定到具体 `Channel`（[channel.h:451-462](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/channel.h#L451-L462)）。`SendChannelInterface`（[channel.h:522-534](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/channel.h#L522-L534)）与 `ReceiveChannelInterface`（[channel.h:536-548](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/channel.h#L536-L548)）分别实现 `direction()` 返回 `kSend`/`kReceive`。一条通道两端的接口由 `ChannelWithInterfaces` 聚拢（[channel.h:550-555](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/channel.h#L550-L555)）——这正是 DSLX `chan(...)` 返回 `(发送端, 接收端)` 的来源。

**IR 节点 `Receive` / `Send`**：在 IR 数据流图里，收发就是两个普通节点。

- `Receive`（[nodes.h:1156-1177](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/nodes.h#L1156-L1177)）：操作数是入口 token，可带 `predicate`（条件接收），产出类型是 `(token, payload)` 元组，`is_blocking_` 标记是否阻塞。
- `Send`（[nodes.h:1179-1199](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/nodes.h#L1179-L1199)）：操作数是 token 和 data（`kDataOperand = 1`），可带 `predicate`，产出新 token。

它们在 `.ir` 文本里长这样（取自 IR 解析器测试中的真实文本语法）：

```text
chan hbo(bits[32], id=0, kind=streaming, flow_control=none, ops=receive_only, fifo_depth=42)
chan mtv(bits[32], id=1, kind=streaming, flow_control=none, ops=send_only)

proc my_proc(my_token: token, my_state: bits[32], init={token, 42}) {
  receive.1: (token, bits[32]) = receive(my_token, channel=hbo)       # 从 hbo 收，返回 (token, 数据)
  tuple_index.2: token   = tuple_index(receive.1, index=0)            # 取出 token
  tuple_index.3: bits[32] = tuple_index(receive.1, index=1)           # 取出数据
  add.4: bits[32] = add(my_state, tuple_index.3)                      # 用收到的数据计算
  send.5: token = send(tuple_index.2, add.4, channel=mtv)             # 把结果发到 mtv
}
```

要点：`receive` 的产出是元组，要靠 `tuple_index` 拆出 token 和 payload；`send` 的第一个操作数是 token、第二个是要发的数据；通道在 `chan` 行里声明，`ops=receive_only`/`send_only` 对应方向。

#### 4.2.4 代码实践

**目标**：在 `proc_iota.x` 里跟踪一次完整的 `send`→`recv` 数据流动。

**步骤**：

1. 打开 [xls/examples/proc_iota.x](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/examples/proc_iota.x)。
2. 读 `producer.next`（[第 26-30 行](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/examples/proc_iota.x#L26-L30)）：算出 `foo = i + 1`，`send(join(), s, foo)` 把它发到 `s`，返回 `foo` 作为下一状态。
3. 读 `consumer.next`（[第 40-43 行](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/examples/proc_iota.x#L40-L43)）：`recv(join(), r)` 从 `r` 收到 `e`，返回 `i + e + N` 作为下一状态。
4. 回到 `main.config`（[第 47-52 行](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/examples/proc_iota.x#L47-L52)）：确认 `s`（producer 的 out）和 `r`（consumer 的 in）其实是同一条 `my_chan` 的两端。

**需要观察的现象**：数据 `foo` 从 producer 的 `send` 流出，经过通道 `my_chan`，被 consumer 的 `recv` 读为 `e`。

**预期结果**：你能画出一句话的因果链——「producer 发 `i+1` → 通道 → consumer 收为 `e` 并算 `i+e+N`」。

**说明**：本实践是源码阅读型，无需运行即可完成。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `recv` 默认阻塞而 `send` 默认非阻塞？把两者反过来（recv 非阻塞、send 阻塞）会带来什么问题？

**参考答案**：阻塞 `recv` 保证了「计算只在实际有数据时推进」，符合 KPN 的时序无关性，让综合出的硬件结果稳定。`send` 在高层建模为无限深队列所以总能完成；若反过来让 `recv` 非阻塞，则通道空时会读到无意义值，结果依赖时序；若让 `send` 强制阻塞且无背压机制，发送方会卡死。实际上 RTL 里 FIFO 有限深，`send` 也会因背压而 stall，这正是 `FlowControl::kReadyValid` 要解决的问题。

**练习 2**：`Channel`（通道对象）与 `ChannelInterface`（通道接口）有什么区别？为什么新式 Proc 要引入后者？

**参考答案**：`Channel` 是「通道本身」，携带类型、深度、流控等完整属性，全局唯一；`ChannelInterface` 是「某个 Proc 看到的那一端」（发送端或接收端）。新式 Proc 里 `send`/`receive` 引用的是接口而非对象，因为同一个 Proc 定义可被 `spawn` 多次、每次接到不同的实际通道——接口在实例化（elaboration）时才绑定到具体 `Channel`，从而支持 Proc 层级复用。

**练习 3**：下列 IR 节点 `receive.1: (token, bits[32]) = receive(my_token, channel=hbo)` 的产出为什么是元组而不是直接 `bits[32]`？

**参考答案**：因为 `receive` 既要返回收到的**数据**，又要返回一个**新 token**（供后续操作建立顺序依赖）。把两者打包成元组，接收方再用 `tuple_index` 分别取出 token 和 payload。

---

### 4.3 状态寄存器

#### 4.3.1 概念说明

状态是 Proc 区别于 Function 的灵魂。官方教程的 State 一节（[what_is_a_proc.md:190-200](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/docs_src/tutorials/what_is_a_proc.md#L190-L200)）说：

- 每个 Proc 可有若干**状态元素**，每个元素是任意允许类型的一个值。
- 每个状态元素有一个**初值**，这是第一次激活看到的值。
- 第 \(N\) 次激活设置的状态值，会被第 \(N+1\) 次激活读到；想「保持不变」就显式把旧值写回。

这等价于一组寄存器：初值是复位值，`next` 的返回值是寄存器下一拍的输入。回到 4.1 的归纳公式：

\[
s_{n+1} = f(s_n,\ \text{in}_n)
\]

`init` 给 \(s_0\)，`next` 给 \(f\)，状态元素就是寄存器 \(s\)。

**一个状态元素的「生命周期」**：

```
复位 ──▶ s = init_value       （第 0 次激活读到的初值）
         │
   激活0: 读 s (=init), 计算, 写回 s₁ = f(init, in₀)
         │
   激活1: 读 s (=s₁),  计算, 写回 s₂ = f(s₁, in₁)
         │
   激活2: 读 s (=s₂),  计算, 写回 s₃ = f(s₂, in₂)
         ⋮
```

#### 4.3.2 核心流程

**单状态元素**：`saturating_accumulator`（[what_is_a_proc.md:207-225](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/docs_src/tutorials/what_is_a_proc.md#L207-L225)）展示了一个带状态的累加器：

```dslx
pub proc saturating_accumulator {
    ch_in: chan<u32> in;
    result: chan<u32> out;

    config(ch_in: chan<u32> in, result: chan<u32> out) { (ch_in, result) }

    init { u32:0 }                       // 状态 accumulated 初值 = 0

    next(accumulated: u32) {             // 形参 = 上一次写回的值
        let (tok, data) = recv(join(), ch_in);
        let sum = (data as u33) + (accumulated as u33);
        let new_val = if sum > all_ones!<u32>() as u33 { all_ones!<u32>() } else { sum as u32 };
        send(tok, result, new_val);
        new_val                          // 下一状态 = new_val（饱和后）
    }
}
```

每一拍：读输入 `data`，与历史累加值 `accumulated` 相加并饱和，把结果发出去，**同时**把 `new_val` 写回状态——下一拍的 `accumulated` 就是它。这就是「随时间归纳」：当前结果依赖历史。

**多状态元素**：当一个 Proc 需要同时维护多个寄存器，DSLX 里把状态写成**元组**。例如把累加值和计数器一起演化，`init { (u32:0, u32:0) }`，`next((acc, cnt): (u32, u32)) { ...; (new_acc, new_cnt) }`。`proc_iota.x` 的 `consumer<N: u32>` 则展示了一个**带参数化（parametric）常数 N** 叠加进状态递推的例子（`i + e + N`）。

**状态与吞吐（throughput）**：状态会形成「反馈环」——下一拍的状态依赖这一拍的状态。如果这个反馈环算不完一拍（比如饱和加法太复杂，放不进一个流水线级），就无法达到**满吞吐**。XLS 用 **最坏情况吞吐（Worst-Case Throughput, WCT）** 描述：两次相邻激活之间至少间隔多少周期。WCT=1 即每周期一拍（满吞吐）。教程给出了典型报错（[what_is_a_proc.md:228-254](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/docs_src/tutorials/what_is_a_proc.md#L228-L254)）：

```shell
Error: ... cannot achieve full throughput. Try `--worst_case_throughput=5`
```

与之对照，`clamped_diff`（[what_is_a_proc.md:261-283](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/docs_src/tutorials/what_is_a_proc.md#L261-L283)）的新状态值能在读到输入后**立刻**确定（直接把 `val` 写回），所以即使后续计算跨级，也能满吞吐——因为下一拍不必等本拍算完就能读到状态。这条「状态更新路径的长度决定吞吐」是连接本讲（状态）与下一单元（调度）的桥梁。

#### 4.3.3 源码精读

**`StateElement` 数据结构**：IR 层用一个很小的类表示「一个状态寄存器的形状」（[state_element.h:29-45](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/state_element.h#L29-L45)）。它只有四个字段：`name_`（名字）、`type_`（类型，决定寄存器位宽）、`initial_value_`（复位值）、`non_synthesizable_`（标记某些仅供分析、不可综合的状态）。构造函数还做了一致性检查：初值必须符合类型（`ValueConformsToType`）。

**`Proc` 如何持有状态元素**：`Proc` 用「按名表 + 保序向量」双重索引管理一组 `StateElement`（[proc.h:78-95](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/proc.h#L78-L95)）。`AppendStateElement` / `InsertStateElement` / `RemoveStateElement` 等方法让优化 Pass 能增删状态（例如 4.4 提到的状态窄化、拆分）。`GetStateFlatBitCount()` 给出所有状态寄存器加起来的总位数——这是评估硬件状态开销的常用指标。

**读写状态的 IR 节点**：在数据流图里，状态不是变量，而是通过两个节点访问。

- `StateRead`（[nodes.h:754-816](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/nodes.h#L754-L816)：「读当前状态」）。它关联一个 `StateElement`，可带 `predicate`（条件读）和 `label`。一个状态元素可被多次读，所以 `Proc` 里有一张 `state_element → 多个 StateRead*` 的映射（[proc.h:437-438](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/proc.h#L437-L438)）。
- `Next`（[nodes.h:818-863](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/nodes.h#L818-L863)：「写下一状态」）。它指向某个 `StateElement`，带一个 `value`（要写入的新值，`value()` 在 [nodes.h:836-838](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/nodes.h#L836-L838)），可带 `predicate`（条件写）。一条状态可以挂多个 `Next`，每个带不同谓词——这就是 `clamped_diff` 那种「在不同条件下写不同下一值」的机制。

**`.ir` 文本中的状态**。在文本格式里，状态元素的初值写在 `proc` 签名的 `init={...}` 里，状态读写用专门的节点串：

```text
proc decoupled_proc(__token: token, __state: bits[32], init={token, 42}) {
  ...
  add: bits[32] = add(...)                       # 计算下一状态值
  next_state: () = next_value(state_element=__state, value=add)   # 把 add 写回 __state
}
```

`next_value(state_element=..., value=...)` 是现代写法（显式指明写哪个状态元素、写什么值）；旧式写法是在 `proc` 末尾用一行 `next (...)` 按状态元素顺序列出所有下一值。两种写法不能混用，解析器会报错（这正是 IR 解析器错误测试里覆盖的场景）。

#### 4.3.4 代码实践

**目标**：手动模拟 `saturating_accumulator` 的前几次激活，验证「随时间归纳」的语义。

**步骤**：

1. 假设状态 `accumulated` 初值 `init = 0`，输入序列 `in = [100, 200, 5]`（u32，远未饱和）。
2. 逐拍套用 `next` 的逻辑：`new_val = accumulated + data`（先忽略饱和分支）。
   - 激活 0：读 `accumulated=0`、`data=100` → `new_val=100`，写回状态。
   - 激活 1：读 `accumulated=100`、`data=200` → `new_val=300`。
   - 激活 2：读 `accumulated=300`、`data=5` → `new_val=305`。
3. 把上述过程画成时序表格（见下方）。

**需要观察的现象**：每一拍读到的 `accumulated` 恰是上一拍写回的 `new_val`，体现「状态在激活间传递」。

**预期结果**（输入不触发饱和时）：

| 激活 | 读 accumulated | 读 data | 计算 new_val | 写回/下一拍 accumulated |
|------|----------------|---------|--------------|--------------------------|
| 0    | 0              | 100     | 100          | 100                      |
| 1    | 100            | 200     | 300          | 300                      |
| 2    | 300            | 5       | 305          | 305                      |

4. **延伸**：把某次输入改到会让 `sum` 超过 `u32::MAX`（如上一拍累加值已很大），观察 `new_val` 被「夹」到 `all_ones!<u32>()`，体会饱和分支的作用。

**说明**：上表是按 `next` 的语义手工推导的结果。若想用工具复核，可用 `eval_proc_main`（见 u6-l1）向 Proc 喂若干输入并观察每拍状态/输出，具体命令的可用性「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`consumer<N: u32>` 的 `next` 是 `i + e + N`（[proc_iota.x:42](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/examples/proc_iota.x#L42)），其中 `N` 来自 `spawn consumer<u32:2>(r)`。如果把 `u32:2` 改成 `u32:10`，状态序列会如何变化？

**参考答案**：`N` 是参数化常量，在实例化时确定。每拍的下一状态 = `i + e + N`，所以 `N` 越大，累加值每拍多涨 `(10-2)=8`。状态序列整体被抬高一个与 `N` 相关的偏移，但「随时间归纳」的结构不变。

**练习 2**：一条 `StateElement` 可以挂多个 `Next` 节点，每个带不同 `predicate`。这种设计为什么比「每个状态只能写一次」更灵活？

**参考答案**：它让一个状态元素能在不同条件下写不同值，而不必先用 `select`/`priority_sel` 把多个候选值汇成一个再写——后者会插入额外逻辑到「状态更新路径」上，可能拉长关键路径、损害吞吐（参见 4.3.2 的 WCT 讨论）。多 `Next` + 谓词让调度器能更自由地安排条件写。

**练习 3**：`StateElement` 有一个 `non_synthesizable` 标志。请猜测它的用途。

**参考答案**：它标记某些「仅供分析、不应被综合成实际寄存器」的状态（例如仅用于形式化验证或调试的可观测变量）。codegen 阶段会跳过带此标志的状态元素，不为其生成硬件寄存器。

---

## 5. 综合实践

把三个模块串起来，完成本讲规格里的核心任务：**阅读 `proc_iota.x`，画出 Proc 的状态变量与 `recv`/`send` 交互时序，并解释为何 Proc 能表达「随时间归纳」的计算。**

### 实践目标

- 把 `producer`、`consumer`、`main` 三个 Proc 的职责说清楚。
- 画出「状态随激活演化」和「数据经通道流动」两条线。
- 用归纳公式解释 Proc 为何天然适合时序电路。

### 操作步骤

1. **组网图**。读 [proc_iota.x:46-57](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/examples/proc_iota.x#L46-L57) 的 `main`，画出网络：

   ```
   producer ──send──▶ my_chan(u32, depth=1) ──recv──▶ consumer
     state i: u32                                       state i: u32
   ```

2. **状态变量表**。列出两个工作 Proc 的状态元素：

   | Proc | 状态元素 | 类型 | 初值 | 下一状态（next 返回） |
   |------|----------|------|------|------------------------|
   | producer | `i` | u32 | 0 | `i + 1` |
   | consumer | `i` | u32 | 0 | `i + e + N`（N=2） |

3. **时序图**。手算前 3 拍（`main` 不参与计算，省略）。设通道初值为空、深度 1：

   ```
   激活   producer.i   producer.send(foo)   my_chan   consumer.recv(e)   consumer.i
    0      0            foo=1 → 发1          [1]        收 e=1             0+1+2 = 3
    1      1            foo=2 → 发2          [2]        收 e=2             3+2+2 = 7
    2      2            foo=3 → 发3          [3]        收 e=3             7+3+2 = 12
   ```

   （上表为按 `next` 语义的理想化推导；通道深度与阻塞/时序的精确交错「待本地验证」——实际可用 `eval_proc_main` 喂激活数后观察。）

4. **归纳解释**。对照写出两个归纳式：

   \[
   \text{producer: } i_{n+1} = i_n + 1,\quad \text{发送 } i_{n+1}
   \]

   \[
   \text{consumer: } i_{n+1} = i_n + e_n + N,\quad e_n = i^{(\text{producer})}_{n+1}
   \]

   `init` 给归纳基，`next` 给归纳步，状态元素就是寄存器。正因为下一拍的状态是本拍状态的函数，Proc 才能「记得过去、影响未来」，从而表达 FIR、累加器、状态机这类 Function 表达不了的时序电路。

### 预期结果

- 一张组网图（producer→chan→consumer）。
- 一张状态变量表（名称/类型/初值/下一值）。
- 一张前几拍时序表或时序图。
- 用自己的话给出结论：**Proc = 状态归纳 + 通道通信**，二者分别对应硬件里的「寄存器」和「数据流握手」。

## 6. 本讲小结

- **Proc 是带状态、可迭代、能通信的可计算单元**，与纯组合无状态的 `Function` 相对；二者在 IR 层同属 `FunctionBase`，靠 `Kind::kProc` 区分。
- **Proc 的三件套**：`config`（接口/组网）、`init`（状态初值）、`next`（每次激活的行为，最后一行是下一状态）。`spawn` 把若干 Proc 连成网络。
- **Channel 是 Proc 通信的唯一手段**，有方向（`in`/`out`）与种类（`kStreaming` FIFO / `kSingleValue` 覆盖式）；`recv` 默认阻塞、`send` 默认非阻塞，RTL 中靠 `FlowControl::kReadyValid` 等握手实现背压。
- **Token 用于在没有数据依赖时建立操作顺序**，`join` 合并多个 token；`send`/`recv` 在 IR 中是普通节点，引用 `ChannelInterface`。
- **状态元素（`StateElement`）即寄存器**：初值是复位值，`StateRead` 读当前值、`Next`/`next_value` 写下一值；多个带谓词的 `Next` 支持条件写。
- **状态形成反馈环**，其更新路径长度决定 Proc 能否满吞吐（WCT）；这条线把本讲的状态概念与下一单元的流水线调度连起来。

## 7. 下一步学习建议

- **进入第四单元「优化与调度」**：特别是 u4-l5（流水线调度）和 u7-l1（Proc 进阶）。调度会把 Proc 的节点分配到流水线级并处理状态反馈环带来的 WCT 约束；u7-l1 会讲 `proc_state_legalization_pass` 如何合法化状态、`proc_runtime` 如何驱动多 Proc 并发执行。
- **阅读运行时侧源码**：`xls/interpreter/proc_interpreter.h` 和 `serial_proc_runtime.h`（u6-l1）展示 Proc 在主机上如何被「一拍一拍」地推进，能加深对「激活」概念的理解。
- **动手扩展**：仿照 `proc_iota.x`，写一个带两个状态元素（元组）的 Proc，比如一个「滑动窗口求和」（保留最近 N 个样本并输出它们的和），亲手体会状态归纳与通道收发的配合。
