# 取指模块族：prefetch / dblfetch / pffifo / pfcache

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清「取指模块」在 ZipCPU 流水线中的位置：它处在内核 `zipcore` 与总线之间，把 CPU 想要的指令地址翻译成一次总线读交易，再把读回的指令喂回 CPU。
- 读懂四个 Wishbone 取指实现 `prefetch`、`dblfetch`、`pffifo`、`pfcache` 各自的策略，并理解它们为何是「逐步压榨总线带宽」的一组演进。
- 掌握外壳 `zipwb.v` 如何用单一参数 `OPT_LGICACHE` 在这四个模块之间做综合期选择，并知道默认值会选中哪一个。
- 能够根据是否流水线、是否宽总线、分支密度，为一个具体设计挑选合适的取指模块。

本讲承接 [u3-l1](u3-l1-zipcore-structure-pipeline.md)：那里我们建立了 `zipcore` 的五级流水线地图，并强调「取指缓存与访存控制器不在内核内，仅在外壳里实例化」。本讲就钻进这些被挂在外面的取指模块。

## 2. 前置知识

在进入源码前，先用通俗语言建立两个直觉。

**第一个直觉：取指就是「提前把下一条指令从内存里捞回来」。** CPU 执行一条指令需要先拿到这条指令的 32 位编码。如果每条指令都等到要用时才去内存里现取，CPU 就会被内存的延迟拖死。所以我们在 CPU 和总线之间放一个小模块，让它**预判 CPU 接下来要哪条指令、提前去总线取、把结果暂存好**，等 CPU 一伸手就能立刻拿到。这个小模块就是「取指 / 预取（prefetch）」模块。

**第二个直觉：预取的策略决定了流水线的上限。** 最朴素的做法是「要一条、取一条」；更好一点是「既然下一条大概率就在相邻地址，不如一次多取几条存起来」；再进一步是「把取过的指令缓存下来，循环执行时根本不用再去总线」。这就是本讲四个模块 `prefetch → dblfetch → pffifo → pfcache` 的演进脉络：**单条取指 → 双取 → FIFO 预取缓冲 → 带标签的指令缓存**。

理解这条脉络需要一点 Wishbone 总线的基础词汇（若已学过 [u1-l3](u1-l3-rtl-top-wrappers.md) 可跳过）：

| 信号 | 含义 |
|---|---|
| `o_wb_cyc` | 总线周期有效，主设备声明「我要发起一笔交易」 |
| `o_wb_stb` | 选通，表示「本拍地址/数据有效，请从设备处理」 |
| `o_wb_addr` | 本次访问的字地址（已折算成字宽单位） |
| `i_wb_stall` | 从设备反压，「我这一拍处理不了，别换地址」 |
| `i_wb_ack` | 从设备应答，「这一笔读完了，数据有效」 |
| `i_wb_err` | 总线错误，「这个地址读不了」，用来触发非法指令陷阱 |

一个关键概念是**「在途请求（outstanding requests）」**：已经发出 `stb`、但还没收到 `ack` 的请求数。朴素模块在途请求最多 1 个；流水线友好的模块允许多个在途请求，从而把地址发送和数据处理重叠起来——这正是提速的核心。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [rtl/core/README.md](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/README.md) | 作者对取指/访存模块族的一手说明，标明哪些是推荐、哪些已弃用 |
| [rtl/core/prefetch.v](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/prefetch.v) | 最朴素的单条取指状态机，在途请求最多 1 个 |
| [rtl/core/dblfetch.v](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/dblfetch.v) | 利用总线流水线一次取两条，带一个字的单字缓存 |
| [rtl/core/pffifo.v](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/pffifo.v) | 基于 FIFO 的预取缓冲，适合宽总线、少分支的代码 |
| [rtl/core/pfcache.v](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/pfcache.v) | 当前推荐的指令缓存，带标签、整行突发填充 |
| [rtl/core/zipwb.v](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipwb.v) | Wishbone 外壳，用 `OPT_LGICACHE` 在上述四个模块间做选择并实例化 |

此外，`bench/formal/` 下有四个对应的形式化证明配置（`prefetch.sby` / `dblfetch.sby` / `pffifo.sby` / `pfcache.sby`），它们的存在本身就是「每个模块都有契约保证」的佐证。

## 4. 核心概念与源码讲解

### 4.1 取指模块的位置与统一接口

#### 4.1.1 概念说明

取指模块不是 `zipcore` 的一部分，而是被外壳 `zipwb.v`（Wishbone 封装）或 `zipaxil.v` / `zipaxi.v`（AXI 封装）实例化在内核之外。这呼应了 [u3-l1](u3-l1-zipcore-structure-pipeline.md) 的结论：「取指缓存与访存控制器不在内核内」。这样做的好处是**同一份内核可以搭配不同的取指/访存策略和不同的总线协议**，互不干扰。

取指模块是一个「夹心层」，对内（CPU 侧）和对外（总线侧）各有一套接口：

```
   ┌──────────┐   CPU 侧握手    ┌────────────┐   Wishbone   ┌──────┐
   │  zipcore │ ───────────────▶│ 取指模块    │ ────────────▶│ 总线 │
   │ (流水线)  │◀───────────────│ prefetch/  │◀─────────────│ RAM  │
   └──────────┘  o_insn/o_valid └────────────┘  i_wb_ack/... └──────┘
```

四个模块的 **CPU 侧端口完全一致**，这才是它们可以互相替换的根本原因。CPU 侧契约是：

- `i_pc`：CPU 想要的下一条指令地址（只是「建议」，分支时才强制）。
- `i_new_pc`：CPU 发生分支，要求跳到 `i_pc`，必须放弃当前在途的预取。
- `i_clear_cache`：内容可能已变（如自修改代码或 DMA 改写了指令区），要求作废缓存。
- `i_ready`：CPU 这一拍吃掉了输出指令，取指模块可以推进到下一条。
- 返回给 CPU：`o_valid`（输出有效）、`o_insn`（指令字）、`o_pc`（该指令地址）、`o_illegal`（本次读取发生总线错误，用来触发非法指令陷阱）。

这组信号在四个模块里一字不差，因此本讲后续只讲各模块「内部」如何不同。

#### 4.1.2 核心流程

无论哪种实现，取指模块都在循环执行同一个抽象流程：

1. **听 CPU**：CPU 给出 `i_pc` / `i_new_pc` / `i_ready`。
2. **判断是否需要总线**：如果手头没有可用的指令（既不在缓存里、也不在 FIFO 里、也不是上一拍刚取回的），就发起一次 Wishbone 读。
3. **驱动总线**：拉高 `o_wb_cyc` / `o_wb_stb`，给出 `o_wb_addr`，等 `i_wb_ack`。
4. **收纳结果**：把 `i_wb_data` 暂存（单条直接给 / 单字缓存 / FIFO / cache 行）。
5. **喂回 CPU**：在 `o_valid` 上给出 `o_insn`，等 `i_ready` 后推进。
6. **处理异常**：`i_new_pc` 要能立即中止在途交易并丢弃结果；`i_wb_err` 要变成 `o_illegal` 而不是把脏数据当指令。

各模块的差异，全部集中在第 2、3、4 步「收纳多少、如何复用」上。

#### 4.1.3 源码精读

这组统一接口可以从外壳的实例化代码直接看到。下面是 `zipwb.v` 里把 CPU 侧信号接到取指模块的片段（以 `prefetch` 为例，其余三个端口名完全相同）：

[rtl/core/zipwb.v:333-349](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipwb.v#L333-L349) —— CPU 侧 `i_new_pc` / `i_clear_cache` / `i_ready` / `i_pc` 与返回的 `o_valid` / `o_illegal` / `o_insn` / `o_pc`，以及总线侧 `o_wb_cyc` / `o_wb_stb` / `i_wb_ack` / `i_wb_err` / `i_wb_data`。注意三个 CPU 控制输入其实来自内核的输出：`pf_new_pc`、`clear_icache`、`pf_ready && clk_gate`（见 [zipwb.v:336-337](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipwb.v#L336-L337)），这说明「要不要取、取哪条、缓存作不作废」全由 CPU 决定，取指模块只是执行者。

> 小贴士：所有四个模块的 CPU 侧端口一模一样，是它们能被一个 `generate` 分支互相替换的前提。

### 4.2 prefetch：单条取指状态机（基线）

#### 4.2.1 概念说明

`prefetch` 是最朴素的实现：**每条指令发起一次完整的 Wishbone 读交易，取回一条、交出一条，在途请求最多 1 个**。它足够简单、容易验证，是让 ZipCPU「先跑起来」的版本。作者在 README 里直言它「一次只取一条，因而妨碍了流水线（prevented pipelining）」——这是理解它为何被取代的关键。

#### 4.2.2 核心流程

```
        ┌──────────────┐
        │ 空闲(IDLE)    │◀──── i_reset / 上一条被 CPU 吃掉 / i_new_pc
        └──────┬───────┘
               │ 需要新指令：拉高 o_wb_cyc, o_wb_stb, 给 o_wb_addr
               ▼
        ┌──────────────┐
        │ 等待应答       │──── 若 i_new_pc 到来：置 invalid，中止
        └──────┬───────┘
               │ i_wb_ack：拿到 i_wb_data
               │   ─ 或 i_wb_err：置 o_illegal
               ▼
        ┌──────────────┐
        │ 输出有效       │ o_valid=1, o_insn=…
        └──────┬───────┘
               │ i_ready：CPU 吃掉 → 回到空闲，准备下一条
               ▼
```

它在任意时刻只允许一笔交易在途。后面的形式化证明里把这一约束写成 `F_MAX_REQUESTS(1)`，是「单条取指」最直接的数学表达。

#### 4.2.3 源码精读

总线周期的发起与结束逻辑在：

[rtl/core/prefetch.v:116-160](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/prefetch.v#L116-L160) —— 这段 `always @(posedge i_clk)` 是模块的核心状态机：复位 / 收到 `ack` 或 `err` 时结束周期（拉低 `o_wb_cyc` / `o_wb_stb`）；当上一条指令被 CPU 接受（`i_ready && !r_valid`）或收到 `i_new_pc` 时发起新周期；请求被从设备接受后（`!i_wb_stall`）就放下 `o_wb_stb`。注意 `i_new_pc` 会在交易进行中强行中止（154–158 行），这正是「分支要能立刻打断预取」的保证。

「分支打断在途请求」还配合一个 `invalid` 标志位，用来丢弃「已经被发出去、但 CPU 不再需要」的结果：

[rtl/core/prefetch.v:170-176](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/prefetch.v#L170-L176) —— 一旦在交易期间收到 `i_new_pc`，`invalid` 置 1；等总线 `ack` 回来时，即使数据到了也不交给 CPU，而是立刻重新发起一次指向新地址的交易。

输出有效与非法标志的产生：

[rtl/core/prefetch.v:386-419](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/prefetch.v#L386-L419) —— 收到 `i_wb_ack` 时 `o_valid<=1`，收到 `i_wb_err` 时同时 `o_illegal<=1`；CPU 接受（`i_ready`）后清掉 `o_valid`，避免同一条指令被送两次。

最后，形式化证明用 `fwb_master` 把「在途请求 ≤ 1」钉死：

[rtl/core/prefetch.v:494-512](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/prefetch.v#L494-L512) —— 注意参数 `.F_MAX_REQUESTS(1)`。这正是 `prefetch` 无法流水线的根源：一笔交易没 `ack`，下一笔就不能发出。

#### 4.2.4 代码实践

**目标**：亲手验证「`prefetch` 在途请求最多 1 个」这一性质。

**步骤**：

1. 打开 `rtl/core/prefetch.v`，定位到 [L494](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/prefetch.v#L494) 的 `fwb_master` 实例，确认 `.F_MAX_REQUESTS(1)`。
2. 对照 [L116-L160](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/prefetch.v#L116-L160) 的状态机，找出发起新周期的条件里为什么必须包含 `!o_wb_cyc`（提示：只有不在总线周期里才能开始新周期，从而保证只有一笔在途）。
3. 若已按 [u1-l2](u1-l2-repo-layout-and-build.md) 装好 SymbiYosys，可在 `bench/formal/` 下运行子证明：
   ```bash
   cd bench/formal && sby --noprog prefetch.sby
   ```
   预期结果：`prefetch.sby` 的 `PASS`（通过），说明「任意时刻在途请求 ≤ 1」被形式化验证成立。**待本地验证**（取决于本机是否装了 SymbiYosys 与求解器）。

**预期现象**：无论总线多快，`prefetch` 发出第 1 个 `stb` 后必须等 `ack` 才能发第 2 个；CPU 想要连续 N 条指令时，至少要付出 N 次独立的总线往返。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `prefetch` 的 README 说明里说它「妨碍了流水线（prevented pipelining）」？
> **参考答案**：因为它在途请求最多 1 个（`F_MAX_REQUESTS(1)`），一笔总线读没收到 `ack` 之前不能发出下一笔，地址发送与数据处理无法重叠，CPU 因此吃不满每拍一条指令。

**练习 2**：若总线读进行到一半 CPU 才发出 `i_new_pc`，`prefetch` 如何避免把「旧地址的脏结果」当成新指令交给 CPU？
> **参考答案**：置 `invalid=1`（[L174-L175](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/prefetch.v#L174-L175)），并立即中止在途周期（[L154-L158](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/prefetch.v#L154-L158)）；待总线应答回来时丢弃结果，重新发起指向新 `i_pc` 的交易。

### 4.3 dblfetch 与 pffifo：用预取缓冲压榨总线带宽

这两个模块都在 `prefetch` 的基础上「多取一点、存起来」，但缓冲的形态不同：`dblfetch` 是一个字的单字缓存，`pffifo` 是一个真正的 FIFO。

#### 4.3.1 概念说明

**dblfetch 的直觉**：很多存储器对「连续第二次访问」比「第一次」快（第一次要建立连接 / 行激活，第二次命中已激活的行）。所以它**在一个总线周期里发两个（或更多）读请求**，第一个结果立刻交 CPU，第二个结果先存进一个单字缓存 `cache_word`，CPU 下次要时直接给——只要 CPU 吃得够快，就能「白赚」一次访问。

**pffifo 的直觉**：如果总线比指令宽（例如 `BUS_WIDTH=128` 位、一次能回 4 条 32 位指令），并且代码不怎么分支，那就该**用一个 FIFO 把突发回来的宽字都攒起来**，让 CPU 慢慢取。FIFO 把「总线突发」和「CPU 消费」解耦，CPU 哪怕偶发停顿也不会立刻让总线空转。代价是：一旦分支，FIFO 里攒的全是错路指令，只能整体冲掉。

#### 4.3.2 核心流程

**dblfetch** 维护两个量：在途计数 `inflight`（已发未应答的请求数，最多 2）和单字缓存 `cache_valid` / `cache_word`：

```
发请求 → inflight++ ；收到 ack → inflight--
  第 1 个 ack 的数据 → 立刻 o_insn 给 CPU
  第 2 个 ack 的数据 → 存 cache_word，cache_valid=1
CPU 再要一条 → 若 cache_valid：直接给，免一次总线；否则再发
```

**pffifo** 维护 FIFO 填充度与两个计数器 `wb_pending`（总线在途）和 `pipe_fill`（从总线发出但还没被 CPU 消费的总量）：

```
当 FIFO 快空（sfifo_fill 高位为 0）→ 发起新突发，持续 stb 直到 pipe_full
每个 ack 的宽字 → 写入 sfifo（含一位错误标志）
CPU 要指令 → 从 sfifo 读出宽字，按字节序拆成一条条 o_insn
i_new_pc / i_clear_cache → sfifo_reset，FIFO 清空，地址回到 i_pc
```

#### 4.3.3 源码精读

`dblfetch` 的总线周期与在途计数：

[rtl/core/dblfetch.v:107-143](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/dblfetch.v#L107-L143) —— 注意它和 `prefetch` 不同：交易进行中可以继续发 `stb`（`END_CYCLE` 块里 `o_wb_stb <= (!last_stb)`），从而在一个周期里塞进两个请求。

[rtl/core/dblfetch.v:147-161](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/dblfetch.v#L147-L161) —— `inflight` 计数器：`stb` 被接受则 +1，收到 `ack` 则 -1，刻画了「在途请求数」。形式化证明里 `dblfetch` 用 `.F_MAX_REQUESTS(0)`（见 [L565-L581](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/dblfetch.v#L565-L581)），`0` 表示「不限」，即可流水线发送——这正是它比 `prefetch` 快的来源。

`dblfetch` 的单字缓存：

[rtl/core/dblfetch.v:428-466](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/dblfetch.v#L428-L466) —— `cache_valid` / `cache_word` / `cache_illegal` 三者把「第二个 ack 的结果」暂存。CPU 再要时若 `cache_valid` 为真就免一次总线访问。

`pffifo` 的 FIFO 与冲刷：

[rtl/core/pffifo.v:235-258](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/pffifo.v#L235-L258) —— `sfifo_reset = i_reset || i_clear_cache || i_new_pc`（[L235](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/pffifo.v#L235)）：分支即清空 FIFO。实例化的 `sfifo` 宽度为 `BUS_WIDTH+1`（多 1 位存错误标志），深度 `LGFIFO`（默认 4，即最多 16 项）。每个总线应答 `i_wb_ack` 连同 `i_wb_err` 一起入队，CPU 侧再按需拆包。

[rtl/core/pffifo.v:196-221](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/pffifo.v#L196-L221) —— `wb_pending` 跟踪总线在途、`pipe_fill` 跟踪「已发但 CPU 还没消费」的总量；`pipe_full` 一旦置位就停止发 `stb`，防止 FIFO 溢出。

#### 4.3.4 代码实践

**目标**：理解 `dblfetch` 为什么「快一点」、`pffifo` 为什么适合宽总线。

**步骤**：

1. 在 `dblfetch.v` 里找到 `inflight` 的声明类型 `reg [1:0]`（[L93](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/dblfetch.v#L93)），解释为什么只需 2 位宽。
2. 在 `pffifo.v` 里找到 `parameter BUS_WIDTH = 128`（[L51](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/pffifo.v#L51)），计算当 `BUS_WIDTH=128`、`INSN_WIDTH=32` 时，一个总线宽字能拆出几条指令（答案：4）。
3. 对照 `pffifo` 的 `sfifo_reset`，说明为什么「分支密集的代码」会让 `pffifo` 表现很差。

**预期结果**：`dblfetch` 在途计数 2 位 = 最多 3，但因逻辑上最多 2 个在途，所以「快一点但非成倍」；`pffifo` 一次突发能填多条指令，但每次分支都清空 FIFO、之前的预取全浪费，因此适合「直线代码、长循环体」。

#### 4.3.5 小练习与答案

**练习 1**：`dblfetch` 的形式化证明里 `F_MAX_REQUESTS(0)`，而 `prefetch` 是 `F_MAX_REQUESTS(1)`。`0` 在这里意味着什么？
> **参考答案**：`0` 表示「不限制在途请求数」（即允许流水线式连续发送），与 `prefetch` 的「最多 1 个在途」形成对比，是 `dblfetch` 能在一个周期里发两个请求、从而更快的根因。

**练习 2**：为什么 `pffifo` 的 README 描述说它「在低分支负载下工作良好，在高分支负载下表现糟糕」？
> **参考答案**：`pffifo` 靠 FIFO 攒突发回来的指令获利；而 `i_new_pc` 会触发 `sfifo_reset` 清空整个 FIFO，攒下的预取全部作废。分支越密，冲刷越频繁，预取命中率越低。

### 4.4 pfcache：带标签的指令缓存（推荐方案）

#### 4.4.1 概念说明

`pfcache` 是 README 明确点名的「current/best（当前最佳）」Wishbone 指令缓存。它不再像前三个那样「现要现取」或「小幅预取」，而是**真正把取过的指令存进一块带标签（tag）的缓存阵列里**：命中就直接给、零总线流量；缺失才发起一次**整行突发**把一整条缓存行从内存搬回来。对于循环，第一次迭代付行填充代价，之后每次迭代几乎全是命中——这是它在吞吐上甩开前三个的根本原因。

#### 4.4.2 核心流程

`pfcache` 把指令地址拆成三段：

```
[ Tag 标签 ][ Cache line 行号 ][ Line position 行内位置 ]
```

- 读缓存时只用后两段（行号定位、行内位置选字）。
- 填缓存时，一次突发固定 Tag 和行号、行内位置从 0 走到行末。

命中/缺失判定靠两个东西：每行的 `cache_tags[]`（这行当前存的是哪个 Tag）和 `valid_mask[]`（这行的内容是否有效）。一次「命中」要求：地址的 Tag == 该行存的 Tag，且该行 `valid_mask` 为 1。

由于读块 RAM 要花一拍、比较 Tag 又要花一拍，作者用了一个**双读**技巧：每拍同时读 `cache[i_pc]` 和 `cache[lastpc]`（当前地址和上一拍地址），下一拍再用选择信号 `isrc` 决定取哪个，从而把「分支命中」的延迟压到 1 拍。

缺失时的行填充是一次 Wishbone 突发，行大小为 \(2^{LS}\) 个字（默认 8 个字）。形式化证明里把「一次周期最多发 \(1 \ll LS\) 个请求」写成 `F_MAX_REQUESTS(1<<LS)`。

#### 4.4.3 源码精读

参数与缓存阵列：

[rtl/core/pfcache.v:63-81](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/pfcache.v#L63-L81) —— 关键参数：`LGCACHELEN`（缓存总字数的对数，非形式化默认 12）、`LGLINES`（行数的对数，默认 `LGCACHELEN-3`）、由此派生 `LS = LGCACHELEN-LGLINES`（行大小的对数）。

[rtl/core/pfcache.v:137-140](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/pfcache.v#L137-L140) —— 三块内部存储：`cache[0:CACHELEN-1]`（指令数据）、`cache_tags[]`（每行的 Tag）、`valid_mask`（每行是否有效的位图）。注意：这三个是**内部存储**，并不出现在模块端口上——这正是「pfcache 多出来的『缓存』」所在（见 4.4.4 实践）。

双读与输出选择：

[rtl/core/pfcache.v:179-216](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/pfcache.v#L179-L216) —— 每拍同时读 `cache[i_pc]` 和 `cache[lastpc]` 到 `r_pc_cache` / `r_last_cache`，并登记 `isrc` 表示下一拍该信哪一个；`o_pc` 与 `o_insn` 都据此选择。注释解释了为什么要双读：`i_pc` 会在我们知道命中与否之前就自增，所以必须同时保留「按上一拍地址读」的结果。

Tag 查找与命中判定：

[rtl/core/pfcache.v:300-366](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/pfcache.v#L300-L366) —— `w_v_from_pc` / `w_v_from_last` 分别判断「按当前地址」「按上一拍地址」是否命中（Tag 相等且 `valid_mask` 为 1）；再用 `rvsrc` 在两者间选择，得到最终的 `r_v`（有效）。

行填充的总线状态机：

[rtl/core/pfcache.v:431-450](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/pfcache.v#L431-L450) —— 只有 `needload`（缺行）且不在总线周期时才发起 `o_wb_cyc`/`o_wb_stb`；收到最后一个 `ack`（`last_ack`）或 `i_wb_err` 时结束周期。

[rtl/core/pfcache.v:454-497](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/pfcache.v#L454-L497) —— 每个 `ack` 把 `i_wb_data` 写进 `cache[wraddr]`，同时把 `o_wb_addr`（请求地址）和 `wraddr`（写地址）逐拍递增，并登记新 Tag。

[rtl/core/pfcache.v:505-519](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/pfcache.v#L505-L519) —— `valid_mask` 的更新被延迟一拍（`svmask`），确保数据真正落盘后才声明该行有效，避免读到上一行的残留。

最后，「一次周期最多发一整行请求」的形式化约束：

[rtl/core/pfcache.v:698-714](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/pfcache.v#L698-L714) —— `fwb_master` 的 `.F_MAX_REQUESTS(1<<LS)`，即一个行填充周期恰好发出 \(2^{LS}\) 个请求。

#### 4.4.4 代码实践

**目标**：对比 `prefetch.v` 与 `pfcache.v` 的端口，搞清「pfcache 多出来的缓存」到底在哪；并估算 `pfcache` 相对 `prefetch` 的吞吐提升来源。

**步骤**：

1. 并排打开两个模块的端口声明：
   - [prefetch.v:68-93](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/prefetch.v#L68-L93)
   - [pfcache.v:82-115](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/pfcache.v#L82-L115)
2. 逐项核对 CPU 侧（`i_new_pc` / `i_clear_cache` / `i_ready` / `i_pc` / `o_valid` / `o_illegal` / `o_insn` / `o_pc`）和总线侧（`o_wb_cyc` / `o_wb_stb` / `o_wb_we` / `o_wb_addr` / `o_wb_data` / `i_wb_stall` / `i_wb_ack` / `i_wb_err` / `i_wb_data`）。
3. 翻到 [pfcache.v:137-140](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/pfcache.v#L137-L140)，找到 `cache` / `cache_tags` / `valid_mask`。

**预期结果（关键发现）**：`pfcache` 的**对外端口与 `prefetch` 几乎一致**——它并没有多出任何「缓存端口」。所谓「多出来的缓存」是**内部存储阵列**（`cache`、`cache_tags`、`valid_mask`），由参数 `LGCACHELEN` / `LGLINES` 决定大小。`prefetch` 根本没有这些存储。两者端口能保持一致，正是 4.1 说的「可替换性」的体现。

**吞吐提升来源估算**：对一段连续 N 条指令（且后续会循环）：

- `prefetch`：每条指令一次独立总线读。若单次访问平均耗时 \(L\) 拍，取 N 条指令约需 \(\approx N \cdot L\) 拍，且**每条都要碰总线**。
- `pfcache`：第一次碰某行时，发起一次 \(2^{LS}\) 个请求的突发把整行填满（约 \(\approx 2^{LS} \cdot L\) 拍的建连 + 流水 `ack`），此后**行内每个字都是命中、零总线、每拍一条**；循环回到已填行时**完全不碰总线**。

所以提升来自两点：**(a) 突发把每次访问的建连/地址开销摊薄到整行；(b) 命中时彻底绕过总线**。这正是 `F_MAX_REQUESTS` 从 `1`（prefetch）变成 `1<<LS`（pfcache）所反映的——同一周期里能塞进一整行的请求。具体拍数随存储器时序而变，**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`pfcache` 为什么要「双读」（同时读 `cache[i_pc]` 和 `cache[lastpc]`）？
> **参考答案**：因为读块 RAM 需要一拍、比较 Tag 又需要一拍，而 `i_pc` 会在我们判定命中与否之前就自增。双读同时保留「按当前地址」和「按上一拍地址」两个结果，下一拍用 `isrc` 选择，从而把分支命中的代价压到约 1 拍（见 [L179-L216](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/pfcache.v#L179-L216)）。

**练习 2**：`valid_mask` 的更新为什么要先用 `svmask` 延迟一拍？
> **参考答案**：数据写进 `cache[]` 也要到下一拍才真正可见。若在写数据同一拍就把 `valid_mask` 置 1，CPU 可能在数据落盘前读到旧行残留；用 `svmask` 延一拍，保证「声明有效」发生在「数据可用」之后（见 [L505-L519](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/pfcache.v#L505-L519)）。

**练习 3**：一次 `pfcache` 行填充周期最多会发出多少个 Wishbone 请求？由哪个式子保证？
> **参考答案**：最多 \(2^{LS}\) 个，即一整行。形式化证明里 `fwb_master` 的 `.F_MAX_REQUESTS(1<<LS)` 把它钉死（见 [L698-L714](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/pfcache.v#L698-L714)）。

### 4.5 选型机制：OPT_LGICACHE 与模块演进

#### 4.5.1 概念说明

四个取指模块不是「四选一」的散件，而是被外壳 `zipwb.v` 用**一个参数 `OPT_LGICACHE`** 自动串成一条演进阶梯。`OPT_LGICACHE` 是「指令缓存大小的对数」，但它的取值范围同时编码了「用哪种取指策略」——这是一个非常典型的 ZipCPU 式「综合期剪刀」设计（见 [u3-l1](u3-l1-zipcore-structure-pipeline.md) 关于 `OPT_*` 的讨论）：你调一个数，综合出来的就是完全不同的电路。

此外还有两个旁支：历史弃用的 `pipefetch`，以及 AXI 总线侧的类比模块。

#### 4.5.2 核心流程

`zipwb.v` 用一个 `generate if … else if … else` 链按 `OPT_LGICACHE` 选择：

| `OPT_LGICACHE` 取值 | 选中的模块 | block 名 | 策略 |
|---|---|---|---|
| `<= 1` | `prefetch` | `SINGLE_FETCH` | 单条取指，在途 ≤ 1 |
| `<= 2` | `dblfetch` | `DBLFETCH` | 双取 + 单字缓存 |
| `<= 6` | `pffifo` | `PFFIFO` | FIFO 预取缓冲 |
| `> 6`（默认 12） | `pfcache` | `PFCACHE` | 带标签指令缓存 |

默认 `OPT_LGICACHE=12`，所以**开箱即用的 ZipCPU 用的是 `pfcache`**。

#### 4.5.3 源码精读

`zipwb.v` 的选择链：

[rtl/core/zipwb.v:319-353](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipwb.v#L319-L353) —— `generate if (OPT_LGICACHE <= 1) begin : SINGLE_FETCH` 实例化 `prefetch`。

[rtl/core/zipwb.v:354-387](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipwb.v#L354-L387) —— `end else if (OPT_LGICACHE <= 2) begin : DBLFETCH` 实例化 `dblfetch`。

[rtl/core/zipwb.v:389-423](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipwb.v#L389-L423) —— `end else if (OPT_LGICACHE <= 6) begin : PFFIFO` 实例化 `pffifo`。

[rtl/core/zipwb.v:424-441](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipwb.v#L424-L441) —— `end else begin : PFCACHE` 实例化 `pfcache`，并把 `OPT_LGICACHE-WBLSB` 传给 `pfcache` 的 `LGCACHELEN`。

默认值与 `pfcache` 的实际规模：

[rtl/core/zipwb.v:106](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipwb.v#L106) —— `parameter ... OPT_LGICACHE=12`。

默认 `OPT_LGICACHE=12`、`BUS_WIDTH=32`（故 `WBLSB=$clog2(32/8)=2`）时，`pfcache` 的 `LGCACHELEN = 12-2 = 10`，即缓存 \(2^{10}=1024\) 个字；`LGLINES = LGCACHELEN-3 = 7`，即 128 行；`LS = LGCACHELEN-LGLINES = 3`，即每行 \(2^3=8\) 个字。这就是默认配置下一个 ZipCPU 的指令缓存画像（由默认参数推导，**待本地验证**）。

弃用与 AXI 类比（来自作者一手说明）：

[rtl/core/README.md:7-35](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/README.md#L7-L35) —— 这段把四个 Wishbone 取指模块的定位讲得清清楚楚：`prefetch`「妨碍流水线」、`dblfetch`「比 prefetch 快一点」、`pfcache`「current/best」；并点名 `pipefetch`「已弃用（abandonware），仅为历史保留」。同一段还指明 AXI 侧的对应关系：`axilfetch` 在 `FETCH_LIMIT<=1` 时类比 `prefetch`、`==2` 时类比 `dblfetch`、`>2` 时走 FIFO；`axiicache` 是 `pfcache` 的 AXI 版本——这是与 [u4-l3](u4-l3-axi-axilite-wrappers.md) AXI 封装的衔接点。

#### 4.5.4 代码实践

**目标**：用「调一个数」改变取指策略，并预测综合结果。

**步骤**：

1. 在 `rtl/core/zipwb.v:106` 把 `OPT_LGICACHE=12` 改成 `OPT_LGICACHE=1`。
2. 对照 4.5.2 的表格，预测：综合后会实例化哪个取指模块？（答案：`prefetch`，`SINGLE_FETCH` block。）
3. 进一步思考：若把 `OPT_LGICACHE` 设为 `8`，会落到哪一档？缓存会有多大？（答案：`> 6`，走 `pfcache`，`LGCACHELEN=8-2=6`，即 \(2^6=64\) 字、`LGLINES=3` 即 8 行、每行 8 字。）
4. （可选）运行 `make rtl`，对比改前改后 `rtl/obj_dir/` 下生成的模型规模变化。

> 注意：本实践只读源码、最多临时改一个参数做对比；按本手册约定**不要把改动提交到源码**。改完观察完即还原。

**预期结果**：`OPT_LGICACHE` 的阈值 `1 / 2 / 6` 正好对应「单条 / 双取 / FIFO / 缓存」四档；越过 6 就从「缓冲式预取」跨入「真正缓存」，电路规模和性能都跳一个台阶。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `OPT_LGICACHE` 的阈值恰好是 1、2、6？
> **参考答案**：1 对应「连一个字都不缓存」（单条取指）；2 对应「只够缓存一个额外字」（双取的单字缓存）；6 对应 `pffifo` 的 FIFO 上限（`LGFIFO=4` 即最多 16 项，对应的对数尺度落在 6 这一档以下）；超过 6 才值得上带标签的完整缓存。这是作者按「缓冲越小越像预取、越大越像缓存」划的经验门槛。

**练习 2**：`pipefetch` 为什么还留在仓库里？
> **参考答案**：README 说明它「是作者第一次尝试做带缓存的预取，把缓存实现成内存中的滚动窗口」，现已弃用（abandonware），仅为历史原因保留，不再使用。读它可作了解，但新设计应选 `pfcache`。

## 5. 综合实践

**任务**：为三种典型场景各选一个取指模块，并给出依据。

场景如下：

1. **资源极紧的小型 FPGA**：你只想要一个能跑、面积最小的 ZipCPU，代码以直线为主、偶尔分支。
2. **宽总线 DSP 代码**：总线宽 128 位、代码里有长而直的计算循环、分支很少。
3. **含紧凑内核循环的控制程序**：循环体很小、被高频反复执行，对延迟敏感。

**操作步骤**：

1. 对每个场景，依据本讲的「策略—适用性」表选择模块，并指出对应的 `OPT_LGICACHE` 取值。
2. 对场景 3，进一步估算：若循环体 16 条指令、缓存行 8 字（默认 `LS=3`），需要几行才能装下？首次迭代与第 2 次迭代的总线访问次数大致分别是多少？
3. 把你的选型写进一张表，列出「模块 / `OPT_LGICACHE` / 理由」。

**参考答案**：

| 场景 | 模块 | `OPT_LGICACHE` | 理由 |
|---|---|---|---|
| 极紧面积 | `prefetch` | `<=1`（如 1） | 无任何缓存存储，面积最小，足够「能跑」 |
| 宽总线少分支 | `pffifo` | `3~6` | FIFO 能消化 128 位突发、每拍拆多条，直线代码获利最大 |
| 紧凑高频循环 | `pfcache` | `>6`（默认 12） | 命中零总线，循环回访不碰总线，延迟最低 |

场景 3 估算：16 条指令、每行 8 字 → 需 2 行；首次迭代填这两行（约各一次 8 请求突发）；第 2 次起每次迭代**零**总线访问（全命中）。这正是 `pfcache` 对循环延迟敏感场景的价值。具体拍数与存储器时序相关，**待本地验证**。

## 6. 本讲小结

- 取指模块是 `zipcore` 与总线之间的「夹心层」，CPU 侧四个模块端口完全一致，因而可互换。
- `prefetch` 是基线：一次一条、在途请求 ≤ 1（`F_MAX_REQUESTS(1)`），简单但「妨碍流水线」。
- `dblfetch` 用单字缓存 + 最多 2 个在途请求，比 `prefetch`「快一点」；`pffifo` 用 FIFO 消化宽总线突发，适合直线代码、怕分支。
- `pfcache` 是当前推荐方案：带 Tag 的真缓存，命中零总线、缺失整行突发（`F_MAX_REQUESTS(1<<LS)`），对循环最优。
- 外壳 `zipwb.v` 用 `OPT_LGICACHE` 一个参数把四档串成阶梯，默认 `12` → `pfcache`（默认约 1024 字、128 行、每行 8 字）。
- `pipefetch` 已弃用仅作历史保留；AXI 侧有 `axilfetch`（按 `FETCH_LIMIT` 类比 prefetch/dblfetch/FIFO）与 `axiicache`（类比 pfcache）与之对应。

## 7. 下一步学习建议

- 想看「取指之后的指令如何被解析」→ 下一讲 [u3-l3 指令译码 idecode](u3-l3-instruction-decode.md)。
- 想看「数据访存侧的同类模块族」→ [u3-l6 访存模块族：memops/pipemem/dcache](u3-l6-memory-access-family.md)，那里的 `memops/pipemem/dcache` 与本讲的 `prefetch/dblfetch?/pfcache` 是对称的「单次 / 流水 / 缓存」三件套。
- 想看 AXI 版取指与 AXI 封装如何衔接 → [u4-l3 AXI 与 AXI-Lite 封装](u4-l3-axi-axilite-wrappers.md)。
- 想亲手跑一个取指模块的形式化证明，理解「契约如何被数学保证」→ [u5-l2 形式化验证体系（SymbiYosys）](u5-l2-formal-verification.md)，可直接用 `bench/formal/pfcache.sby` 等练手。
