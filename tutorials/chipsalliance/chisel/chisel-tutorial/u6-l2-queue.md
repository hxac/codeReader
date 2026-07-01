# Queue：FIFO 队列生成器

## 1. 本讲目标

学完本讲，你应当能够：

- 用 `Module(new Queue(gen, entries))` 实例化一个硬件 FIFO，并把它的 `enq`/`deq`/`count` 端口正确连进自己的设计。
- 说清 `QueueIO` 里 `enq`/`deq` 为什么是「翻转过的」Decoupled 接口，从而不会再被方向绕晕。
- 读懂 Queue 的环形缓冲核心：`ram` + 双指针（`enq_ptr`/`deq_ptr`）+ 一个 `maybe_full` 满空标志位，以及它们如何被 `io.enq.fire`/`io.deq.fire` 驱动。
- 区分 `pipe` 与 `flow` 两个布尔选项对**吞吐**与**延迟**的不同影响，并理解它们各自引入的组合逻辑耦合代价。
- 理解 `useSyncReadMem` 如何用一个「提前一拍读下一个出队地址」的小技巧，让同步读 RAM 也能扮演回压 FIFO。

本讲承接 [u6-l1（Decoupled / ReadyValidIO 握手协议）](u6-l1-decoupled-readyvalid.md) 和 [u3-l6（Mem / SyncReadMem）](u3-l6-mem.md)：Queue 本质上就是把「握手协议」加在「一块读写存储」上做出来的参数化生成器。

## 2. 前置知识

在进入源码前，先建立三个直觉。

**直觉一：FIFO 是「带节流阀的水管」。** 想象一根水管中间接了一个水桶：上游（生产者 producer）随时可能倒水进来，下游（消费者 consumer）不一定每拍都能接走。桶的作用是「削峰填谷」——上游快、下游慢时把水暂存，避免丢失。Queue 就是这个水桶的硬件实现：`entries` 是桶能装多少「格」数据。

**直觉二：握手决定「这一拍到底有没有成交」。** 回顾 u6-l1：一笔数据真正发生传输，当且仅当同一时钟沿 `ready` 与 `valid` 同时为高，这个条件叫 **fire**（`ready && valid`）。Queue 内部一切动作（写存储、推指针）都由 `fire` 触发，而不是单独看 `valid` 或 `ready`。

**直觉三：环形缓冲（ring buffer）。** 用一块固定大小的存储 + 两个指针即可做 FIFO：

- `enq_ptr`：下一个该写入的位置（生产者追着跑）。
- `deq_ptr`：下一个该读出的位置（消费者追着跑）。
- 写一格 → `enq_ptr` 前进一格；读一格 → `deq_ptr` 前进一格。
- 当两个指针**相等**时，队列可能「空」也可能「满」（两种状态指针长得一样），所以额外用一个比特 `maybe_full` 来区分。

记号上，设队列深度为 \(n\)、指针宽度为 \(k=\lceil\log_2 n\rceil\)。空与满的判定为：

\[
\text{empty} \iff \text{enq\_ptr}=\text{deq\_ptr} \land \lnot\,\text{maybe\_full}
\]

\[
\text{full} \iff \text{enq\_ptr}=\text{deq\_ptr} \land \text{maybe\_full}
\]

当前元素数（`count`）在 \(n\) 为 2 的幂时可写成：

\[
\text{count} =
\begin{cases}
n & \text{若 full} \\
(\text{enq\_ptr}-\text{deq\_ptr}) \bmod 2^{k} & \text{否则}
\end{cases}
\]

非 2 的幂时取模不再「天然干净」，需要额外比较，源码里有两套分支（见 4.2）。

**术语速查：**

| 术语 | 含义 |
|------|------|
| fire | `ready && valid`，这一拍真正成交一次传输 |
| enq（enqueue） | 入队，从生产者角度看是「把数据塞进桶里」 |
| deq（dequeue） | 出队，从消费者角度看是「从桶里取走数据」 |
| 反压（backpressure） | 桶满了，向上游拉低 `ready`，要求上游别再塞 |
| 组合耦合 | 一个信号「同一拍」直接依赖另一个信号（不经过寄存器），影响时序 |

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/main/scala/chisel3/util/Queue.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/Queue.scala) | Queue 模块本体、QueueIO 接口、Queue 伴生对象工厂（`apply`/`irrevocable`/`withShadow`） |
| [src/main/scala/chisel3/util/Decoupled.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/Decoupled.scala) | `DecoupledIO` 类、`Decoupled` 工厂、`EnqIO`/`DeqIO` 两个方向别名 |
| [src/main/scala/chisel3/util/ReadyValidIO.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/ReadyValidIO.scala) | `ReadyValidIO` 抽象基类（`ready`/`valid`/`bits`）与 `fire` 等便捷方法 |
| [src/main/scala/chisel3/util/Counter.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/Counter.scala) | `Counter`：Queue 用来当指针的内联计数器（含 `value`/`inc()`/`reset()`） |
| integration-tests/src/test/scala-2/chiselTest/QueueSpec.scala | 真实可跑的 Queue 行为测试，是最好的连线范例 |

## 4. 核心概念与源码讲解

本讲按「接口 → 核心 → 选项 → 工厂」四个最小模块展开。

### 4.1 QueueIO：FIFO 的对外接口与方向约定

#### 4.1.1 概念说明

Queue 对外暴露的接口是一个 `QueueIO` Bundle。它把「上游握手」和「下游握手」打包在一起，外加一个反映当前水位的 `count` 和一个可选的 `flush`。关键设计点在于：`enq` 和 `deq` 的命名是站在**使用 Queue 的人（client）**的视角——「我要往 `enq` 里塞数据，从 `deq` 里取数据」。但 Queue 模块本身恰好站在接口的「对面」，所以源码里这俩字段都被 `Flipped` 了一次。

#### 4.1.2 核心流程

`QueueIO` 的字段构成：

- `enq = Flipped(EnqIO(gen))`：入队端，类型来自 `EnqIO`。
- `deq = Flipped(DeqIO(gen))`：出队端，类型来自 `DeqIO`。
- `count = Output(UInt(log2Ceil(entries + 1).W))`：当前元素数，宽度是 `log2Ceil(entries+1)`（`+1` 是为了能表示「满」的值 `entries` 本身）。
- `flush = if (hasFlush) Some(Input(Bool())) else None`：可选的清空信号，用 `Option[Bool]` 表达「可能没有」。

要理解 `enq`/`deq` 的方向，先把两个别名拆开（它们都定义在 Decoupled.scala）：

```
EnqIO(gen) = Decoupled(gen)           // 生产者视角：驱动 valid/bits，读 ready
DeqIO(gen) = Flipped(Decoupled(gen))  // 消费者视角：驱动 ready，读 valid/bits
```

而 `Decoupled(gen)`（即 `ReadyValidIO`）的原始方向是：`ready=Input`、`valid=Output`、`bits=Output`（详见 u6-l1）。于是：

| 字段 | 表达式 | 从 Queue 模块内部看 |
|------|--------|---------------------|
| `enq` | `Flipped(EnqIO(gen))` = `Flipped(Decoupled(gen))` | `enq.valid`/`enq.bits` 是 **Input**（读上游），`enq.ready` 是 **Output**（Queue 决定能否收） |
| `deq` | `Flipped(DeqIO(gen))` = `Flipped(Flipped(Decoupled(gen)))` = `Decoupled(gen)` | `deq.valid`/`deq.bits` 是 **Output**（Queue 驱动），`deq.ready` 是 **Input**（读下游） |

翻两次等于没翻——`deq` 从 Queue 内部看就是「标准生产者方向」。这套设计使得 Queue 内部代码可以「自然地」把 `io.enq` 当上游、`io.deq` 当下游来写，而不必关心外部 client 怎么连。

#### 4.1.3 源码精读

QueueIO 类定义与字段（注意 19–22 行的方向注释，它正是上面那张表的源头）：

[Queue.scala:16-43](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/Queue.scala#L16-L43) —— `QueueIO` 的全部字段：`enq`/`deq` 用 `Flipped`、`count` 是 `Output`、`flush` 是 `Option`。

`EnqIO`/`DeqIO` 这两个方向别名：

[Decoupled.scala:72-81](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/Decoupled.scala#L72-L81) —— `EnqIO` 就是 `Decoupled`，`DeqIO` 是 `Flipped(Decoupled)`，二者只差一个翻转。

`count` 的位宽推导（`log2Ceil(entries + 1)`，例如深度 4 → `log2Ceil(5)=3` 位，能表示 0..4）：

[Queue.scala:36](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/Queue.scala#L36) —— `count` 的 Output 声明。

`fire` 的定义（u6-l1 已讲，这里复用以串起后续）：

[ReadyValidIO.scala:48](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/ReadyValidIO.scala#L48) —— `def fire: Bool = target.ready && target.valid`。

#### 4.1.4 代码实践

**目标**：用源码阅读验证方向表，不靠记忆。

**步骤**：

1. 打开 `Queue.scala`，定位 `class QueueIO`（约第 16 行）。
2. 追踪 `EnqIO` → `Decoupled.scala` 第 73 行，确认它是 `Decoupled(gen)`，未翻转。
3. 追踪 `DeqIO` → `Decoupled.scala` 第 80 行，确认它是 `Flipped(Decoupled(gen))`。
4. 回到 `QueueIO`，把 `enq = Flipped(EnqIO(gen))`、`deq = Flipped(DeqIO(gen))` 各展开一次翻转，推出「从 Queue 内部看」的方向。

**预期**：你应当亲手推出 `io.enq.ready` 是 Output、`io.deq.valid` 是 Output，与 4.1.2 的表一致。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `count` 用 `log2Ceil(entries + 1)` 而不是 `log2Ceil(entries)` 位？

**答案**：因为队列最多有 `entries` 个元素，需要能表示 `entries` 这个值本身。例如深度 4 时，`log2Ceil(4)=2` 位只能表示 0..3，无法表达「4 个（满）」，所以要 `log2Ceil(5)=3` 位。

**练习 2**：如果 client 直接 `q.io.enq <> producer.io.out`（producer 也是 `Decoupled` 输出），方向会不会接反？

**答案**：不会。`<>` 是双向连接，会自动按方向匹配。`q.io.enq` 从 client 角度是「我要往里塞」，与 producer 的输出方向正好互补，故可直连。

---

### 4.2 环形缓冲核心：ram、双指针、maybe_full 与 fire

#### 4.2.1 概念说明

这是 Queue 的心脏。存储体 `ram` 是一块深度为 `entries`、宽度为 `gen` 的内存（默认 `Mem`，组合读，见 u3-l6）；两个 `Counter` 当指针用；一个 `RegInit(false.B)` 叫 `maybe_full` 专门记录「两指针相等时到底是空还是满」。所有读写动作都不直接看 `valid`/`ready`，而是看 **`fire`**——只有真正成交的这一拍，才推进指针、写存储、更新满空位。

#### 4.2.2 核心流程

每一拍（无 pipe/flow 时）的逻辑：

```
do_enq := io.enq.fire      // 这一拍上游真塞进来一个
do_deq := io.deq.fire      // 这一拍下游真取走一个

when(do_enq): ram(enq_ptr) := io.enq.bits;  enq_ptr.inc()
when(do_deq):                               deq_ptr.inc()
when(do_enq != do_deq): maybe_full := do_enq   // 只有一个方向在动时更新满标志

io.deq.valid := !empty      // 不空就有东西可出
io.enq.ready := !full       // 不满才能再收
io.deq.bits  := ram(deq_ptr)   // 组合读：给地址当拍即出数据
```

满空判定（回顾前置公式）：

- `ptr_match := enq_ptr.value === deq_ptr.value`
- `empty := ptr_match && !maybe_full`
- `full  := ptr_match && maybe_full`

`maybe_full` 的更新只发生在「恰好一个方向 fire」时：只入队 → 指针即将追平 → 置满；只出队 → 指针即将拉开 → 置空；同时入出或都不动 → 满空不变。

`count`（当前元素数）分两套：当 `entries` 是 2 的幂时，指针是 \(k\) 位、减法天然按 \(2^k\) 回绕，故 `count = (full ? entries : 0) | (enq_ptr - deq_ptr)`；非 2 的幂时回绕点不在 \(2^k\)，需用 `deq_ptr > enq_ptr` 判断是否已经绕了一圈。

#### 4.2.3 源码精读

存储体与状态声明（注意第 73 行的 `Mem`/`SyncReadMem` 二选一，这正是 u3-l6 的知识落点）：

[Queue.scala:73-82](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/Queue.scala#L73-L82) —— `ram`、`enq_ptr`、`deq_ptr`、`maybe_full` 的声明，以及 `ptr_match`/`empty`/`full`/`do_enq`/`do_deq`/`flush` 的派生。

读写主体逻辑（「只登记不施工」的 when 块，记录的是这一拍的状态更新）：

[Queue.scala:86-100](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/Queue.scala#L86-L100) —— 入队写 ram 并推 `enq_ptr`、出队推 `deq_ptr`、`do_enq≠do_deq` 时更新 `maybe_full`、`flush` 时复位指针与满标志。

对外握手信号赋值（空/满直接驱动 valid/ready）：

[Queue.scala:102-111](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/Queue.scala#L102-L111) —— `io.deq.valid := !empty`、`io.enq.ready := !full`，以及默认（非 SyncReadMem）的 `io.deq.bits := ram(deq_ptr.value)` 组合读。

`count` 的两套计算（2 的幂走快速按位或，非 2 的幂走显式比较）：

[Queue.scala:126-136](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/Queue.scala#L126-L136) —— `isPow2(entries)` 分支与一般分支。

指针本身：`Counter(entries)` 是内联计数器（不产生子模块），其 `value` 是 `RegInit`、`inc()` 推进并返回是否回绕、`reset()` 复位：

[Counter.scala:61](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/Counter.scala#L61) —— `value` 是一个 `RegInit` 寄存器（这就是 Verilog 里会看到 `enq_ptr_value`/`deq_ptr_value` 的原因）。

[Counter.scala:71-94](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/Counter.scala#L71-L94) —— `inc()`：到达末值时回绕（对 2 的幂且从 0 开始的计数器，直接利用寄存器自然溢出，省掉一个 mux）。

#### 4.2.4 代码实践

**目标**：跟踪「一拍入队」时 Builder 大致记录了哪些动作。

**步骤**：

1. 假设某拍 `io.enq.valid` 与 `io.enq.ready`（即 `!full`）同时为真 → `do_enq` 为真。
2. 在源码中依次指出这一拍会触发：第 87 行 `ram(enq_ptr) := io.enq.bits`、第 88 行 `enq_ptr.inc()`、第 93–94 行 `maybe_full` 更新。
3. 写下结论：**一次入队 = 一次存储写 + 一次写指针自增 + 一次满标志条件更新**，全部由 `fire` 触发。

**预期**：你能用一句话说清「为什么 Queue 用 `fire` 而不是 `valid` 来触发写入」——因为只有下游也准备好（`ready`）这一拍数据才真正被收下，否则会丢失或重复。

#### 4.2.5 小练习与答案

**练习 1**：两个指针相等（`ptr_match`）时，队列一定是空的吗？

**答案**：不一定。`ptr_match` 为真时，可能是空（`maybe_full=false`）也可能是满（`maybe_full=true`），所以才需要 `maybe_full` 这个额外比特来区分。

**练习 2**：为什么 `maybe_full` 只在 `do_enq =/= do_deq` 时更新，而不是每拍都算？

**答案**：当入出都 fire 或都不 fire 时，两个指针同步前进或同步静止，它们「是否相等」的关系不变，满空状态也不变；只有单独入队或单独出队时，满空状态才会翻转。这样省掉了冗余计算。

---

### 4.3 pipe 与 flow：吞吐与延迟的权衡

#### 4.3.1 概念说明

默认的 Queue 是「保守」的：满了才反压、空了才无效。这会带来吞吐或延迟损失。`pipe` 和 `flow` 是两个布尔选项，分别用**组合耦合**为代价换取更好的性能：

- **`pipe=true`**：让一个「看起来满了」的队列，在下游这拍正好要出队时，仍允许上游这拍入队。数据「像流水线一样」从入口直达出口，单元素队列也能做到每拍吞吐 1。代价：`io.enq.ready` 组合依赖于 `io.deq.ready`。
- **`flow=true`**：让队列空时，上游当拍有效数据直接「流」到下游出口，**延迟为 0**（不必先存进去再下拍读出来）。代价：`io.deq.valid` 组合依赖于 `io.enq.valid`。

二者都可能在你的设计里引入跨端口的组合路径，影响时序（最大频率），需按需开启。

#### 4.3.2 核心流程

`flow` 分支（空时直通）：

```
when(io.enq.valid): io.deq.valid := true.B        // 上游有效 → 下游也有效
when(empty):
  io.deq.bits := io.enq.bits                       // 直接把入口数据接给出口
  do_deq := false.B                                // 别让下游这次"出队"重复扣减
  when(io.deq.ready): do_enq := false.B            // 若下游真接走，这次"入队"也不重复计入
```

`pipe` 分支（满时放行）：

```
when(io.deq.ready): io.enq.ready := true.B         // 下游这拍要取 → 即使满也允许上游塞
```

注意 `flow` 之所以要 `do_deq := false.B` / `do_enq := false.B`，是因为数据是「绕过 ram 直通」的，不能让 ram 的写指针 / 读指针再为这次直通动作推进，否则水位计数会错。这是把 4.2 的核心机制与 flow 协调起来的关键细节。

#### 4.3.3 源码精读

`flow` 实现：

[Queue.scala:113-120](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/Queue.scala#L113-L120) —— 上游有效即拉高 `deq.valid`；空时把 `deq.bits` 直接连到 `enq.bits`，并抑制 `do_deq`/`do_enq` 避免重复计数。

`pipe` 实现：

[Queue.scala:122-124](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/Queue.scala#L122-L124) —— 下游 ready 即拉高 `enq.ready`，使满队列也能同时入出。

构造参数默认值（`pipe`/`flow`/`useSyncReadMem`/`hasFlush` 默认全是 `false`）：

[Queue.scala:60-67](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/Queue.scala#L60-L67) —— Queue 类签名，可见 `pipe`、`flow` 默认关闭。

#### 4.3.4 代码实践

**目标**：对比「默认 / pipe / flow」三种 Queue 对单元素队列（`entries=1`）的行为差异。

**步骤**：

1. 阅读集成测试 `QueueSinglePipeTester`（QueueSpec.scala 第 104–128 行），它专门构造 `new Queue(UInt(bitWidth.W), 1, pipe = true, ...)` 并断言 `q.io.enq.ready || (q.io.count === 1.U && !q.io.deq.ready)`。
2. 解释这条断言的含义：**只有当队列恰好满（count=1）且下游这拍不取（!deq.ready）时，上游才不允许入队**；否则上游随时可入——这正是 `pipe` 带来的「单元素队列满吞吐」。
3. 推断：若不开 `pipe`（默认），单元素队列在满时即使下游 ready 也不会放行上游，于是无法每拍都入+出。

**预期**：你能说清「`pipe` 把『满』的判定从『无条件满』放宽为『满且下游不取』」，从而打通流水线。

> 本步为源码阅读型实践；若想亲跑，见第 5 节综合实践。

#### 4.3.5 小练习与答案

**练习 1**：`flow=true` 的队列，在空且上游有效时，数据从入到出的延迟是几拍？

**答案**：0 拍（同一拍直通），因为空时 `io.deq.bits := io.enq.bits`、`io.deq.valid := io.enq.valid`，数据不经过 ram。

**练习 2**：为什么说 `pipe` 和 `flow` 会「损害时序」？

**答案**：`pipe` 让 `enq.ready` 同拍依赖 `deq.ready`，`flow` 让 `deq.valid` 同拍依赖 `enq.valid`；这构成跨端口的组合路径，关键路径变长，最高时钟频率可能下降。是否开启要在吞吐/延迟与频率之间权衡。

---

### 4.4 useSyncReadMem 与伴生对象 Queue

#### 4.4.1 概念说明

**存储体的两种选择。** 第 73 行 `val ram = if (useSyncReadMem) SyncReadMem(...) else Mem(...)` 决定存储体是组合读（`Mem`，给地址当拍出数据）还是同步读（`SyncReadMem`，读数据延迟一拍，对应真实 BRAM）。回顾 u3-l6：`SyncReadMem` 的读数据要下一拍才到。但 Queue 希望 `deq.bits` 与 `deq.valid` 同拍一致、对下游表现为「组合可见」——这就产生了矛盾。

**Queue 的解法：提前一拍读「下一个出队地址」。** 既然这一拍若出队，下一拍要读的就是 `deq_ptr` 自增后的地址，那就**现在**就把这个地址送给 `SyncReadMem`，这样数据正好在需要的当拍出来。

**伴生对象工厂。** 除了 `new Queue(...)`，更常用的是 `Queue(enq, entries)` 工厂方法：它接收一个 `ReadyValidIO` 输入，内部实例化 Queue、连好线，直接返回 `DecoupledIO` 输出——一行就能在数据通路上插一个 FIFO。它还处理了 `entries=0` 的特殊情形（零深队列退化为纯组合直通）。

#### 4.4.2 核心流程

SyncReadMem 读路径：

```
deq_ptr_next := (deq_ptr == entries-1) ? 0 : deq_ptr + 1   // 出队后指针将到的值
r_addr       := do_deq ? deq_ptr_next : deq_ptr            // 这拍若出队，就提前读下一个地址
io.deq.bits  := ram.read(r_addr)                           // 同步读：现在给地址，下拍出数据
```

效果：无论这拍是否真的出队，`deq.bits` 都呈现「当前/下一个有效槽位」的数据，使同步读 RAM 对外行为与组合读一致。

`Queue.apply` 工厂：

```
if (entries == 0):
   纯组合直通：deq.valid:=enq.valid; deq.bits:=enq.bits; enq.ready:=deq.ready
else:
   q = Module(new Queue(chiselTypeOf(enq.bits), entries, pipe, flow, useSyncReadMem, flush.isDefined))
   手工用 := 连线（不用 <>，以便允许覆盖）
   返回 q.io.deq
```

注意第 202 行注释「not using <> so that override is allowed」：工厂刻意用单向 `:=` 而非双向 `<>`，这样上层若对返回的 `deq` 做额外覆盖（如 `deq.ready := ...`）不会被 `<>` 的双向语义冲突。

#### 4.4.3 源码精读

存储体二选一：

[Queue.scala:73](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/Queue.scala#L73) —— `Mem` vs `SyncReadMem` 的 `if` 选择（`SyncReadMem` 用 `WriteFirst` 读写下文优先策略）。

SyncReadMem 的「提前读下一地址」：

[Queue.scala:105-111](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/Queue.scala#L105-L111) —— `deq_ptr_next` 计算、`r_addr` 选择、`ram.read(r_addr)` 同步读。

`Queue.apply` 工厂全貌：

[Queue.scala:185-207](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/Queue.scala#L185-L207) —— 零深直通分支与正常实例化分支，注意 202 行为何用 `:=` 而非 `<>`。

零深队列的纯组合直通：

[Queue.scala:193-198](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/Queue.scala#L193-L198) —— `entries==0` 时根本不实例化 Queue 模块，只做 `valid/bits/ready` 的组合转发。

模块命名（影响生成的 Verilog 模块名）：

[Queue.scala:141](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/Queue.scala#L141) —— `desiredName = s"Queue${entries}_${gen.typeName}"`，故 `new Queue(UInt(8.W), 4)` 的模块名是 `Queue4_UInt<8>`。

`irrevocable` 工厂（把 `DecoupledIO` 安全升级为更强的 `IrrevocableIO`，因为 Queue 本身满足不可撤回语义）：

[Queue.scala:318-333](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/Queue.scala#L318-L333) —— `Queue.irrevocable` 复用 `apply`，再外接一个 `IrrevocableIO` 线（u6-l1 提过 Irrevocable 承诺更强）。

#### 4.4.4 代码实践

**目标**：体会 `useSyncReadMem` 与 `Mem` 两种实现的 Verilog 差异。

**步骤**：

1. 阅读集成测试 `ThingsPassThroughTester`（QueueSpec.scala 第 15–45 行），注意第 23 行 `new Queue(UInt(bitWidth.W), queueDepth, useSyncReadMem = useSyncReadMem, ...)`——同一份测试同时覆盖两种存储体。
2. 读第 30–41 行的驱动：`enq.valid`/`enq.bits` 由 `inCnt` 控制，`deq.ready` 用 LFSR 随机化（模拟不规则下游），在 `q.io.deq.fire` 时断言「出==入」。
3. 推断：无论 `useSyncReadMem` 真假，数据都能正确「先进先出」穿过——这正是 4.4.2 那个「提前读」技巧的功劳。

**预期**：你理解了「`useSyncReadMem=true` 让存储体可映射到真实 BRAM，但 Queue 对外行为不变」这一关键设计目标。

> 两种实现的 Verilog 形态对比见第 5 节综合实践。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `useSyncReadMem=true` 时要读 `deq_ptr_next` 而不是 `deq_ptr`？

**答案**：因为 `SyncReadMem` 读数据延迟一拍。若这拍发生出队（`do_deq`），下一拍需要的数据位于「出队后」的新 `deq_ptr`（即 `deq_ptr_next`）；提前这一拍把 `deq_ptr_next` 送进去，数据才能在需要的当拍出现，使同步读 RAM 对外表现得像组合读。

**练习 2**：`Queue(enq, 0)` 会生成什么样的硬件？

**答案**：一个不实例化任何 Queue 模块的纯组合直通：`deq.valid`/`deq.bits` 直接由 `enq` 驱动，`enq.ready` 直接接 `deq.ready`。零深队列 = 零存储的穿线管。

---

## 5. 综合实践

把本讲四个模块串起来，做一个可运行的小任务：实例化深度 4 的 `Queue(UInt(8.W))`，连好 `enq`/`deq`，生成 SystemVerilog，并在产物里找出 `ram` 与指针寄存器。

**实践目标**：亲手用 `Module(new Queue(...))`、观察环形缓冲在 Verilog 中的落地形态。

**操作步骤**：

1. 写一个把上游 `Decoupled` 缓冲 4 项再交给下游的顶层模块（**示例代码**）：

   ```scala
   // 示例代码
   import chisel3._
   import chisel3.util._

   class QueueDemo(entries: Int = 4) extends Module {
     // 上游：生产者驱动 valid/bits，Queue 读 ready → 用 Flipped(Decoupled) 当输入
     val enq = IO(Flipped(Decoupled(UInt(8.W))))
     // 下游：Queue 驱动 valid/bits，消费者读 ready → 直接 Decoupled 当输出
     val deq = IO(Decoupled(UInt(8.W)))
     val cnt = IO(Output(UInt(3.W)))   // log2Ceil(4+1)=3 位

     val q = Module(new Queue(UInt(8.W), entries))
     q.io.enq <> enq
     q.io.deq <> deq
     cnt := q.io.count
   }
   ```

2. 直接对 Queue 本体生成 SystemVerilog（也可换成上面的 `QueueDemo`）：

   ```scala
   // 示例代码
   import circt.stage.ChiselStage
   println(ChiselStage.emitSystemVerilog(new Queue(UInt(8.W), 4)))
   ```

3. 在产物中按名字定位以下结构：
   - 指针寄存器：`enq_ptr_value`、`deq_ptr_value`（来自 `Counter.value`）。
   - 满空标志：`maybe_full`（`RegInit(false.B)`）。
   - 存储体：名为 `ram`（默认 `Mem(4, UInt(8.W))`，firtool 通常推断为 `reg [7:0] ram [0:3];` 一类的数组；深度 4、位宽 8 的小存储是否映射为独立 RAM 取决于 firtool 选项）。

4. 改一处参数对比：把 `new Queue(UInt(8.W), 4)` 换成 `new Queue(UInt(8.W), 4, useSyncReadMem = true)`，重新生成，对比 `ram` 的读端口是否变成「先寄存地址、下拍出数据」的形式。

**需要观察的现象**：

- `enq_ptr_value`/`deq_ptr_value` 是 2 位寄存器（深度 4 → 2 位），随入队/出队各自递增并在到 3 后回绕。
- `maybe_full` 在两个指针相等时区分空/满。
- `io_enq_ready` 与 `!full`（及可能的 pipe 条件）一致；`io_deq_valid` 与 `!empty` 一致。

**预期结果**：你能在 Verilog 中明确指出「存储 + 双指针 + 一个满标志」三件套，并验证它们与 4.2 的源码一一对应。

**关于确切产物**：上述 Verilog 信号名与存储体形态（数组 vs 寄存器堆、是否黑盒化为 SRAM）会随 firtool 版本与选项变化，**具体文本待本地验证**；但 `enq_ptr_value`/`deq_ptr_value`/`maybe_full`/`ram` 这几个名字由源码的 Scala `val` 名决定，是稳定的。

> 想直接跑：可参照 `integration-tests/src/test/scala-2/chiselTest/QueueSpec.scala` 里的 `ThingsPassThroughTester`，它用 `ChiselSim` 把数据灌进 Queue 并断言「先进先出」，是现成的可运行范例。

## 6. 本讲小结

- `QueueIO` 把入队/出队两个 `Decoupled` 握手、一个 `count` 水位、一个可选 `flush` 打包；`enq`/`deq` 都被 `Flipped`，是因为命名站在 client 视角、而 Queue 模块站在接口对面。
- Queue 的核心是环形缓冲：`ram`（默认 `Mem` 组合读）+ `enq_ptr`/`deq_ptr`（两个 `Counter`）+ `maybe_full`（区分空/满的 1 比特）；一切读写动作都由 `fire` 触发，而非单独看 `valid`/`ready`。
- `pipe=true` 让满队列在下游取数时仍可入队，换来满吞吐，代价是 `enq.ready` 组合依赖 `deq.ready`；`flow=true` 让空队列当拍直通、延迟为 0，代价是 `deq.valid` 组合依赖 `enq.valid`。
- `useSyncReadMem=true` 把存储体换成同步读 RAM，靠「提前一拍读下一个出队地址」保持对外行为不变，便于映射真实 BRAM。
- 伴生对象 `Queue.apply(enq, entries)` 是最常用入口：内部实例化并连线、返回 `DecoupledIO`，并对 `entries=0` 做纯组合直通退化；`Queue.irrevocable` 可安全升级为更强的 Irrevocable 接口。
- 模块名由 `desiredName = s"Queue${entries}_${gen.typeName}"` 决定，因此同一设计里不同深度/类型的 Queue 会有可区分的名字。

## 7. 下一步学习建议

- 继续 u6-l3（**Arbiter 仲裁器**）：Arbiter 同样建立在 `Decoupled`/`ReadyValidIO` 之上，把多个入队源汇成一个出队，是 Queue 的天然搭档（如「N 个源 → Arbiter → Queue → 单出口」是常见片上互联模式）。
- 回到 u3-l6（**Mem / SyncReadMem**）对照阅读 Queue 第 73 行与第 105–111 行，巩固「同步读 RAM 如何被包装成组合可见」这一通用技巧。
- 进阶可阅读 `Queue.scala` 中的 `withShadow` / `shadow`（第 158–162、286–296 行）与 `ShadowQueueFactoryTester`，了解如何用 Layer + Probe + BoringUtils 构造与主队列「锁步」的影子队列，用于设计验证——这会自然引出 u8-l3（Layers 与 Probe）。
