# u4-l1 CUDA 图友好的分阶段贪心路径

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚「解码路径被 CUDA 图捕获」对采样层提出的三条硬约束：指针稳定、核参数固定、主机阻塞不能进图。
2. 说出四件套的分工：`greedy_argmax_ids`（图内 argmax + dtod）、`greedy_stage_readback`（图外排队 pinned 回读）、`greedy_stage_resident`（前两者的一次调用组合）、`greedy_collect_resident`（纯主机、只阻塞在拷贝自身事件上的收集）。
3. 解释 `identity_rows`（`0..max_rows` 恒等映射）为什么让贪心路径复用 indexed argmax 核，而不需要第二个「连续行」专用核。
4. 理解 `has_stream_override` 防护守护的 base-stream-only 约束：跨流失序会让回读读到 argmax 写完之前的数据。

本讲承接 u2-l3（`select_batch` 三路分发）。`select_batch` 必须同步返回 `Vec<u32>`，所以它的贪心分支排完 D2H 后立刻自旋等待；本讲要讲的是它的反面——当贪心解码整批被 CUDA 图捕获、且允许 token 「晚一点交付」时，同一个 argmax 核如何被拆成三段延展成一条两级流水线。

## 2. 前置知识

### CUDA Graph（CUDA 图）

把一段 GPU 工作录制成一张可一键重放的「任务清单」。录制（捕获）之后，清单里每个核的参数——指针、行数、网格形状——都固定为捕获那一刻的值；重放只是重新发射同一串工作，省去逐核的启动开销。代价是两条铁律：

- **指针稳定**：图里引用的每块缓冲，地址终身不能变。这就是 u2-l2 讲过的 `SampleScratch` 一次分配、每步只写不配的原因。
- **参数固定**：传给核的行数也被烧进图里。行数一变就得换一张图（本讲会看到 gemma4 按 1、2、4、8… 分桶捕获）。

此外，**图是纯设备工作序列**：主机侧的阻塞等待（「等拷贝完成再读」）不可能出现在图里。

### pinned 内存与流顺序

- **pinned（页锁定）内存**：主机内存中被钉住不换页的缓冲，设备到主机（D2H）的异步拷贝只能落进它。u4-l2 会展开它的 #704 教训，本讲只需知道：向 pinned 缓冲的 `memcpy_dtoh` 是**异步排队**的，之后的读取方只阻塞在这次拷贝自己的事件上。
- **流（stream）**：GPU 上的任务队列。同一条流上的工作严格按入队顺序执行——这是本讲所有正确性论证的基石。

### 三个方向的数据搬运

| 记号 | 含义 | 本讲中的例子 |
|------|------|-------------|
| H2D | 主机 → 设备 | 上传贪心行号表（`select_batch` 用） |
| DtoD | 设备 → 设备 | `argmax_out`(i32) → `ids_out`(u32) |
| D2H | 设备 → 主机 | `argmax_out` → pinned 槽位 |

### 一个关键视角：选出的 token 有两个消费者

贪心解码每步选出的 token，同时被两方需要：

1. **设备**：它是下一步 embedding 查表的输入 id，最好全程留在显存里；
2. **主机**：引擎要把它交付给请求、判断 EOS。

`select_batch` 把两个消费者捆在一起（必须立刻返回 `Vec<u32>`）；分阶段路径把它们拆开——设备消费者留在图内（argmax + dtod 直达 `ids_out`），主机消费者延后一步再收集。

## 3. 本讲源码地图

| 文件 | 角色 |
|------|------|
| `pegainfer-sample/src/lib.rs` | 本讲主角：四件套函数（L344-434）、`STAGED_READBACK_SLOTS`、`SampleScratch` 中 staged 路径专用的缓冲（`argmax_host`/`argmax_host_alt`/`identity_rows`） |
| `pegainfer-kernels/src/ops/sampling.rs` | 底层 indexed argmax 核包装 `argmax_batch_bf16_split_indexed_into`——staged 路径与 `select_batch` 贪心分支共用它 |
| `pegainfer-kernels/src/tensor.rs` | `has_stream_override`、`active_cu_stream`、`memcpy_dtod_u32_from_i32`、`stream_spin_wait`——防护与搬运原语 |
| `pegainfer-gemma4/src/engine.rs` | 仓库内四件套的**唯一消费点**：CUDA 图捕获（L1917-1935）、`launch_staged`/`collect_pending`（L2828-2903）、两级流水线 `decode_round`（L3093-3103） |
| `pegainfer-gemma4/src/serve.rs` | `decode_bucket_slot`：行数 → 图桶下标的映射（L598-600） |
| `pegainfer-sample/tests/select_batch.rs` | 不直接测四件套；它测的 `select_batch` 贪心分支与 staged 路径共用同一 argmax 核与 pinned 落地，用作对比参照 |

> 跨 crate 的永久链接与 `pegainfer-sample` 同 HEAD（`af9e40e1`），只是把路径中的 crate 目录名换掉。

## 4. 核心概念与源码讲解

### 4.1 greedy_argmax_ids：留在图内的 argmax + dtod

#### 4.1.1 概念说明

`greedy_argmax_ids` 是 staged 路径的「图内段」：对 logits arena 的**连续前 `rows` 行**做批式 argmax，并把结果直接拷进「下一步 embedding 的 id 缓冲」`ids_out`。它与 `select_batch` 的贪心分支（u2-l3）做的是同一件事——同一个 indexed argmax 核、同一套 partials/top1/argmax_out 缓冲——差别只在两点：

1. **行号映射**：`select_batch` 每步上传一张「第 k 个贪心行 → arena 行号」的紧凑表；staged 路径整批全贪心、arena 行恰好连续，于是用一张构造时上传一次的恒等表 `identity_rows`。
2. **结果的去向**：`select_batch` 立刻 D2H 回读、自旋等待、散写进 `Vec<u32>`；staged 路径用一次 **DtoD** 把 `argmax_out`（i32）搬进 `ids_out`（u32），全程留在设备上，主机完全不参与。

为什么 DtoD 而不是「D2H 再 H2D」？因为设备消费者（下一步的输入 id）不需要主机；把主机从环路里拿掉，这段工作才能整个被 CUDA 图捕获。

#### 4.1.2 核心流程

```text
greedy_argmax_ids(ctx, logits, rows, ids_out, scratch):
    1. ensure! 没有 stream override            # base-stream-only，见 4.4
    2. validate_greedy_shape(logits, rows, scratch)
         rows > 0；rows <= scratch.max_rows；
         logits.seq_len >= rows 且 hidden_dim == scratch.vocab
    3. argmax_batch_bf16_split_indexed_into(
           logits, scratch.identity_rows, rows,
           partials…, top1…, scratch.argmax_out)   # 核只读 identity_rows 前 rows 项
    4. memcpy_dtod_u32_from_i32(ctx, argmax_out, ids_out, rows)  # i32→u32 设备内拷贝
```

identity_rows 的复用逻辑可以形式化为：indexed argmax 核的输入是一个映射

\[ \text{row\_indices}: \{0,\dots,rows-1\} \to \{0,\dots,seq\_len-1\}, \quad k \mapsto \text{arena 行号} \]

`select_batch` 传的是「压缩后的贪心行集合」；staged 路径传的是恒等映射 \( k \mapsto k \)。核的实现不需要任何改动——它在两种调用里都只是「按表取行、逐行 argmax、最低下标破并列」。于是**一个核服务两种映射**，既不用写第二个「连续行」专用核，staged 路径也省掉了每步上传行号表的 H2D。

#### 4.1.3 源码精读

四件套的图内段本体，[src/lib.rs:344-368](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L344-L368)——先拒绝 stream override，再校验形状，然后一次 indexed argmax（行号表用 `identity_rows`）加一次 DtoD：

```rust
pub fn greedy_argmax_ids(ctx: &DeviceContext, logits: &HiddenStates,
    rows: usize, ids_out: &mut CudaSlice<u32>, scratch: &mut SampleScratch,
) -> Result<()> {
    ensure!(!has_stream_override(), "the staged sampler path runs on the base stream only");
    validate_greedy_shape(logits, rows, scratch)?;
    argmax_batch_bf16_split_indexed_into(
        ctx, logits, &scratch.identity_rows, rows, /* partials… */ &mut scratch.argmax_out)?;
    pegainfer_kernels::tensor::memcpy_dtod_u32_from_i32(ctx, &scratch.argmax_out, ids_out, rows)
}
```

形状校验集中在一个辅助函数里，[src/lib.rs:323-342](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L323-L342)——三个 `ensure!` 分别守护「空批拒绝」「行数不超 scratch 容量（超了报错、绝不扩容，指针稳定优先）」「arena 行数与词表宽度匹配」。

恒等表在 scratch 构造时上传一次，[src/lib.rs:142-145](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L142-L145)——注意它是 `0..max_rows` 而不是 `0..rows`，长度按容量烧死，之后任何 `rows <= max_rows` 的调用都只读它的前缀：

```rust
identity_rows: ctx.stream.clone_htod(&(0..max_rows as i32).collect::<Vec<_>>())?,
```

底层核的契约，[pegainfer-kernels/src/ops/sampling.rs:446-459](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/src/ops/sampling.rs#L446-L459)——文档注释点名「两阶段、tile 并行 partials、最低下标破并列」；签名里 `row_indices` 只被要求 `len() >= rows`（[同文件 L465-471](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/src/ops/sampling.rs#L465-L471)），即核只读前 `rows` 项，这正是恒等表可以按 max_rows 定长的原因。

DtoD 原语自带同款流防护与长度校验，[pegainfer-kernels/src/tensor.rs:108-126](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/src/tensor.rs#L108-L126)。

真实消费点：gemma4 在启动时捕获采样图，[pegainfer-gemma4/src/engine.rs:1917-1935](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-gemma4/src/engine.rs#L1917-L1935)——三处细节值得对照源码读：

- 注释「The warm pass lands lazy module loads outside capture」：捕获前先在图外跑一遍全行数的 `greedy_argmax_ids`，把惰性模块加载等一次性成本留在捕获之外；
- `bucket` 从 1 开始翻倍到 `arena_rows`，每个桶捕获一张图——因为 `rows` 是核参数，被烧进图里，行数变了必须换图；
- 捕获闭包里只有 `suppress + greedy_argmax_ids`，**没有回读**——那是下一节图外段的事。

桶下标映射在 [pegainfer-gemma4/src/serve.rs:598-600](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-gemma4/src/serve.rs#L598-L600)：`rows.next_power_of_two().trailing_zeros()`，即「不小于 rows 的最小 2 的幂」的指数——回放 bucket=8 的图服务 rows=5 时，多算的 3 行是对 arena 里陈旧 logits 的无害 argmax，结果写进 `argmax_out[5..8]`，收集时被 `landed[..rows]` 丢弃。

#### 4.1.4 代码实践

**实践目标**：确认自己能从 gemma4 的捕获代码推出「为什么分桶、为什么 warm pass 在图外、多余行去哪了」。

**操作步骤**：

1. 打开上面 engine.rs L1917-1935 的永久链接，逐行读捕获循环；
2. 回答三个问题（答案见下方练习，先自己写再对照）；
3. 再读 `greedy_collect_resident`（4.3）里的 `landed[..rows]`，验证「多余行的结果被收集截断丢弃」这条链是闭合的。

**需要观察的现象 / 预期结果**：这是纯源码阅读实践，不运行命令。预期你能不看讲义复述：图内段 = suppress + argmax(identity 映射) + dtod，行数参数导致分桶，warm pass 隔离一次性成本。

#### 4.1.5 小练习与答案

**练习 1**：`greedy_argmax_ids` 与 `select_batch` 的贪心分支在「行号映射」和「结果去向」上各差在哪？
**答案**：行号映射——前者用构造时上传一次的恒等表 `identity_rows`（k→k，按 max_rows 定长，任何 rows 只读前缀）；后者每步 H2D 上传「第 k 个贪心行 → arena 行号」的紧凑表。结果去向——前者 DtoD 进 `ids_out`（u32，下一步 embedding 输入，全程在设备）；后者 D2H 进 pinned、自旋等待、按 `greedy[k]` 散写进 `Vec<u32>`。

**练习 2**：为什么结果是 DtoD 而不是「D2H 回主机再 H2D 传回去」？
**答案**：设备消费者（下一步 embedding 的输入 id）不需要主机；DtoD 让 argmax→id 缓冲整段留在设备、留在基流上，因此才能被 CUDA 图捕获，也避免了主机往返的阻塞。

**练习 3**：`rows` 可以小于 `scratch.max_rows` 吗？小于 `identity_rows.len()` 时核会读到越界行号吗？
**答案**：可以。kernels 侧只校验 `row_indices.len() >= rows`，核只读前 `rows` 项、也只写 `argmax_out[0..rows)`，恒等表长度是容量 max_rows，永远满足。图回放时行数问题由「按 2 的幂分桶、选不小于 rows 的桶」解决。

### 4.2 greedy_stage_readback：图外排队、双槽 pinned 落地

#### 4.2.1 概念说明

`greedy_stage_readback` 把 `argmax_out` 异步拷进**两个 pinned 主机槽位之一**（`argmax_host` / `argmax_host_alt`，u2-l2 讲过它们的 #704 来历，u4-l2 展开），然后**立即返回，不等拷贝完成**。它的文档注释只有一句但位置说明了全部：「Queue the base-stream-only pinned readback **outside** the sampler graph」——排队发生在采样图之外。

为什么需要**两个**槽？因为流水线允许「上一步的回读还在飞，下一步已经发射」。若只有一个槽，第 N+1 步的 D2H 会覆盖第 N 步尚未收集的数据，被迫先同步收集再发射——流水线退化成串行。槽位数就是允许的在飞（outstanding）步数：

\[ \text{slot}(n) = n \bmod 2, \qquad \text{在飞步数} \le 2 \]

#### 4.2.2 核心流程

同一条基流上的入队顺序（这是正确性的全部依据）：

```text
… → argmax(N) → dtod(N) → dtoh(N, 槽 N%2) → argmax(N+1) → dtod(N+1) → dtoh(N+1, 槽 (N+1)%2) → …
```

- `dtoh(N)` 读到的一定是第 N 步的 argmax 结果：它入队在第 N+1 步的 argmax **之前**，而同流严格按序执行，`argmax_out` 在它之后才被第 N+1 步改写。
- 第 N 步的主机收集（4.3）可以拖到第 N+1 步发射之后：它只阻塞在「槽 N%2 的那次拷贝」自己的事件上，此时 GPU 可能正在执行第 N+1 步的核——这正是流水的重叠收益。

函数本身极简：校验（无 override、`slot < STAGED_READBACK_SLOTS`）→ 按槽选 pinned 缓冲 → `memcpy_dtoh` 排队 → 返回。

#### 4.2.3 源码精读

双槽数量是一个具名常量而非魔法数字，[src/lib.rs:54](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L54)：

```rust
const STAGED_READBACK_SLOTS: usize = 2;
```

两个槽在 `SampleScratch` 里的声明与注释，[src/lib.rs:71-81](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L71-L81)——`argmax_host` 是主落地槽，`argmax_host_alt` 的文档只有一句「前一次回读还在被收集时使用的备用落地槽」，`identity_rows` 紧随其后。它们的 pinned 分配（unsafe `alloc_pinned`，write-combined 页）在 [src/lib.rs:138-141](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L138-L141)。

图外排队本体，[src/lib.rs:370-392](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L370-L392)——两个 `ensure!` 之后按 `slot` 选缓冲，`memcpy_dtoh` 排到 `ctx.stream`（基流）上，错误只可能来自排队本身：

```rust
pub fn greedy_stage_readback(ctx: &DeviceContext, slot: usize, scratch: &mut SampleScratch) -> Result<()> {
    ensure!(!has_stream_override(), "the staged sampler path runs on the base stream only");
    ensure!(slot < STAGED_READBACK_SLOTS, "greedy_stage_readback: slot {slot} out of range");
    let host = if slot == 0 { &mut scratch.argmax_host } else { &mut scratch.argmax_host_alt };
    ctx.stream.memcpy_dtoh(&scratch.argmax_out, host)
}
```

组合封装，[src/lib.rs:394-410](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L394-L410)——`greedy_stage_resident` 是「图内段 + 图外排队」的一次调用版本，供不走 CUDA 图的调用方使用；文档注释「queue their pinned readback **without waiting for it**」点明了与 `select_batch` 的本质区别。gemma4 的非图道走的正是它（engine.rs L2867-2875），图道则在 `launch_captured` 之后单独调 `greedy_stage_readback`（engine.rs L2855-2861），见 [pegainfer-gemma4/src/engine.rs:2828-2878](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-gemma4/src/engine.rs#L2828-L2878)。

#### 4.2.4 代码实践

**实践目标**：用一张表格证明自己理解双槽轮转与流顺序。

**操作步骤**：

1. 假设连续三个解码步 N=0,1,2，每步 `rows=4`，仿照下面格式填表（`—` 处自己补全）：

| 步 | 发射内容（入队顺序） | dtoh 落进哪个槽 | 该次 dtoh 读到的 argmax_out 是哪一代 | 最早可收集时机 |
|----|---------------------|----------------|-------------------------------------|----------------|
| 0 | argmax(0)→dtod(0)→dtoh | slot 0 | 第 0 步结果 | dtoh(0) 完成后任意时刻（须在第 2 步 dtoh 前） |
| 1 | — | — | — | — |
| 2 | — | — | — | — |

2. 追问两个变体：若 `STAGED_READBACK_SLOTS = 1`，上表哪一格的假设被破坏？若 = 3 而 gemma4 的 `DECODE_PIPELINE_DEPTH` 仍是 2（[engine.rs:1128](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-gemma4/src/engine.rs#L1128)），第三个槽会被用到吗？

**需要观察的现象 / 预期结果**：纸面推演，无需运行。预期结论：单槽时第 1 步 dtoh 与第 0 步未收集数据冲突，必须先收集再发射（串行化）；三槽时第三槽闲置——槽位数 ≥ 流水深度即可，gemma4 的轮转 `next_slot = (pending.slot + 1) % 2`（[engine.rs:3096](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-gemma4/src/engine.rs#L3096)）只会在两槽间打转。

#### 4.2.5 小练习与答案

**练习 1**：`dtoh(N)` 和 `argmax(N+1)` 都碰 `argmax_out`，为什么没有竞态？
**答案**：两者都排在同一条基流上，同流严格按入队序执行。`dtoh(N)` 先入队，读走第 N 代数据之后，`argmax(N+1)` 才被允许改写 `argmax_out`。跨流才会失序——那正是 4.4 的防护要拦的。

**练习 2**：槽号为什么由调用方传入，而不是藏在 scratch 里自动递增？
**答案**：staged 路径刻意保持无状态——scratch 只知缓冲布局，不知流水节奏；「第几步、几行、哪个槽」由调用方记账（gemma4 用 `PendingDecode { rows, slot }`，[engine.rs:1130-1134](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-gemma4/src/engine.rs#L1130-L1134)）。这样同一份 scratch 的轮转策略可以随引擎流水深度调整，函数签名也不必携带隐含的全局步数。

**练习 3**：`greedy_stage_resident` 与「先调 `greedy_argmax_ids` 再调 `greedy_stage_readback`」等价吗？
**答案**：行为等价——它就是这两行的顺序组合，外加开头同样的 stream-override `ensure!`。存在这个封装是因为非图道调用方每次解码都要说完整两句话，组合成一个入口减少样板并保证两段永远按正确顺序发出（[src/lib.rs:396-410](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L396-L410)）。

### 4.3 greedy_collect_resident：只阻塞在拷贝自身事件上的收集

#### 4.3.1 概念说明

`greedy_collect_resident` 是四件套里唯一的「等待」，但它等待的方式与 `select_batch` 截然不同：

- `select_batch` 贪心分支：D2H 排队后调 `stream_spin_wait` **整流自旋**，然后读 pinned——因为函数契约要求同步返回 token（[src/lib.rs:248-262](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L248-L262)，注释解释了等拷贝事件如何传递覆盖同流先前排队的 argmax）。
- `greedy_collect_resident`：**没有 `ctx` 参数**——它不发射任何 GPU 工作，只是对指定槽的 pinned 缓冲调 `as_slice()`；按 u2-l2/#704 注释的表述（[src/lib.rs:71-77](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L71-L77)），pinned 让 D2H 保持异步，「读取方只阻塞在拷贝自身的事件上」。等待范围从「整条流」缩小到「这一次拷贝」。

收集时机完全交给调用方，于是产生了流水重叠：gemma4 在 `decode_round` 里**先发射第 N+1 步、再收集第 N 步**（[engine.rs:3093-3103](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-gemma4/src/engine.rs#L3093-L3103)）——主机在等槽 N%2 的拷贝事件时，GPU 已经在跑第 N+1 步的核。若收集调用永远不来，槽位被后续步骤覆盖、token 丢失——所以流水深度约束由调用方（`DECODE_PIPELINE_DEPTH = 2`）负责，本函数只做最小校验。

#### 4.3.2 核心流程

```text
greedy_collect_resident(rows, slot, scratch) -> Vec<u32>:
    1. ensure! slot < 2
    2. ensure! rows <= scratch.max_rows
    3. landed = 槽位 pinned 缓冲.as_slice()      # 阻塞在该缓冲在飞拷贝的事件上
    4. return landed[..rows] 逐项 as u32          # 尾部（桶回放多算的行、旧数据）直接截断丢弃
```

第 4 步的 `landed[..rows]` 同时完成两件事：丢弃分桶图回放多算的尾部行（4.1），以及丢弃 pinned 缓冲中上一代残留。

#### 4.3.3 源码精读

收集本体，[src/lib.rs:412-434](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L412-L434)——注意签名里没有 `ctx`，也没有 `has_stream_override` 检查（练习 3 解释为什么不需要）：

```rust
pub fn greedy_collect_resident(rows: usize, slot: usize, scratch: &mut SampleScratch) -> Result<Vec<u32>> {
    ensure!(slot < STAGED_READBACK_SLOTS, "greedy_collect_resident: slot {slot} out of range");
    ensure!(rows <= scratch.max_rows, "…exceeds scratch capacity…");
    let landed = if slot == 0 { scratch.argmax_host.as_slice() } else { scratch.argmax_host_alt.as_slice() }
        .map_err(|e| anyhow!("greedy_collect_resident D2H sync failed: {e}"))?;
    Ok(landed[..rows].iter().map(|&token| token as u32).collect())
}
```

消费方的收集与交付，[pegainfer-gemma4/src/engine.rs:2880-2903](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-gemma4/src/engine.rs#L2880-L2903)——`collect_pending` 先 `ensure!` 行数未变（流水中的批不允许中途换行序），再收集、逐行 `settle_staged` 交付：

```rust
let picked = pegainfer_sample::greedy_collect_resident(pending.rows, pending.slot, &mut self.scratch)?;
for (entry, token) in active.iter_mut().zip(picked) {
    entry.settle_staged(&self.policy, token, ledger);
}
```

流水线的发起点，[pegainfer-gemma4/src/engine.rs:3093-3103](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-gemma4/src/engine.rs#L3093-L3103)——顺序是本讲的点睛之处：`launch_staged(第 N+1 步, next_slot)` 在前，`collect_pending(第 N 步)` 在后：

```rust
let next_slot = (pending.slot + 1) % DECODE_PIPELINE_DEPTH;
match self.launch_staged(active, true, next_slot) {
    Ok(rows) => {
        if let Err(err) = self.collect_pending(active, &pending, ledger) { /* … */ }
```

对比 `select_batch` 的同步式贪心分支（用于印证差异），[src/lib.rs:248-255](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L248-L255)：D2H 排队后紧跟 `stream_spin_wait(ctx)?`，注释写明等这个拷贝的事件传递覆盖了同 stream 上更早排队的 argmax 核，且自旋把调度器唤醒从步骤循环里拿掉。

#### 4.3.4 代码实践

**实践目标**：把「先发射后收集」的重叠画成时序图，验证等待窗口被 GPU 工作填满。

**操作步骤**：

1. 画两条时间轴（主机线程 / GPU 基流），按 engine.rs L3093-3103 的真实顺序标出：`launch_captured(N+1)`（或 `greedy_stage_resident`）、`greedy_stage_readback(N+1)`、进入 `greedy_collect_resident(N)` 后的阻塞窗口、GPU 侧第 N+1 步的 suppress/argmax/dtod 执行区间；
2. 标出主机阻塞窗口与 GPU 执行区间的交集；
3. 写出结论：主机等待被什么计算覆盖了。

**需要观察的现象 / 预期结果**：纸面推演。预期：主机的阻塞窗口与「第 N+1 步的核执行」重叠——这正是收集只等单次拷贝事件（而非整流同步）买来的收益。若想真机验证时序，可在 gemma4 侧加日志对比「立即收集」与「先发射再收集」的单步延迟——**待本地验证**（需要 GPU 与 gemma4 模型权重）。

#### 4.3.5 小练习与答案

**练习 1**：`greedy_collect_resident` 为什么不接收 `ctx`、也不检查 `has_stream_override`？
**答案**：它不发射任何 GPU 工作——只是读取主机侧 pinned 内存并触发对其在飞拷贝的等待。override 防护守护的是「核与拷贝排到不同流导致失序」的发射路径（4.4）；纯主机读不存在这个危害，所以两个发射入口（`greedy_argmax_ids`/`greedy_stage_readback`，以及组合它们的 `greedy_stage_resident`）各有防护，收集没有也不需要。

**练习 2**：如果第 N 步排队了回读但一直不收集，会发生什么？
**答案**：第 N+2 步会复用槽 N%2，其 D2H 覆盖第 N 步尚未读取的数据——token 静默丢失。因此「在飞步数 ≤ 槽位数」是调用方必须维持的不变量；gemma4 用 `DECODE_PIPELINE_DEPTH = 2` 与 `next_slot = (pending.slot + 1) % 2` 恰好匹配两个槽。

**练习 3**：`collect_pending` 为什么开头要 `ensure!(pending.rows == active.len())`？
**答案**：staged 收集按「输出第 k 项 = 批内第 k 行」的恒等对应交付（`zip`），且分桶图回放要求行数在发射与收集之间不变。若中途有行结束/换序，收集到的 token 会错位到别的请求上——所以行序变化必须等流水排空（drain）之后进行，这正是 engine.rs 中 `drain_pipeline` 与「Finished rows stay pinned until the speculative successor drains」注释的职责。

### 4.4 base-stream-only 防护：has_stream_override 拦住跨流失序

#### 4.4.1 概念说明

三个发射入口的第一行都是同一个 `ensure!(!has_stream_override(), …)`，错误信息统一为「the staged sampler path runs on the base stream only」。这不是防御式编程的装饰，而是对一个具体数据竞态的封堵：

- argmax 核通过 `active_cu_stream(ctx)` 发射——**override 激活时落到 override 流**（[pegainfer-kernels/src/ops/sampling.rs:497-509](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/src/ops/sampling.rs#L497-L509)，launch 参数取 `active_cu_stream`）；
- 而 `greedy_stage_readback` 的 `memcpy_dtoh` 写死走 `ctx.stream`——**基流**（u2-l3 讲过 override 的存在意义：拆分并发 prefill 借一条副流跑大块工作）。

两条流之间没有顺序保证。若放行：argmax 排在 override 流上、回读排在基流上，回读完全可能**先于 argmax 写完**执行，读到上一代的 `argmax_out`——错误 token，且不报错。所以 staged 路径整体声明「只在基流上运行」，三个发射入口各自把门，连底层 DtoD 原语 `memcpy_dtod_u32_from_i32` 都自带同款检查（纵深防御，[pegainfer-kernels/src/tensor.rs:117-120](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/src/tensor.rs#L117-L120)）。u3-l2 已见过同款思想：`token_logprobs_batch` 的「buffer traffic is ordered on the primary stream」拒绝。

#### 4.4.2 核心流程

```text
has_stream_override() == true   （线程局部 override 被设置，如拆分并发 prefill 期间）
    └─ greedy_argmax_ids / greedy_stage_readback / greedy_stage_resident
         └─ ensure! 失败 → 返回 Err("the staged sampler path runs on the base stream only")
has_stream_override() == false
    └─ active_cu_stream(ctx) == ctx.stream（基流）
         └─ 核、DtoD、D2H 全部排在同一条流上 → 顺序保证成立
```

#### 4.4.3 源码精读

override 的定义与查询，[pegainfer-kernels/src/tensor.rs:64-71](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/src/tensor.rs#L64-L71)——线程局部单元格，隔壁的文档注释点明设置者是「split-concurrent prefill」：

```rust
pub fn has_stream_override() -> bool {
    STREAM_OVERRIDE.with(|c| c.get().is_some())
}
```

有效流的解析，[pegainfer-kernels/src/tensor.rs:73-80](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/src/tensor.rs#L73-L80)——`active_cu_stream` 在有 override 时返回副流，否则返回 `ctx.stream`；argmax 核正是用它决定发射目的地。

staged 三入口的防护，[src/lib.rs:352-355](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L352-L355)、[src/lib.rs:376-379](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L376-L379)、[src/lib.rs:404-407](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L404-L407)——三处 `ensure!` 文案一字不差。

同理由的姊妹防护（印证这是 crate 级纪律而非局部补丁），[src/lib.rs:541-545](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L541-L545)——`token_logprobs_batch` 在 override 下直接拒绝，理由即「缓冲流量排主流、内核落覆盖流，跨流无序」。

#### 4.4.4 代码实践

**实践目标**：验证「防护是纵深式的」——同一约束在多个层面各自把门。

**操作步骤**：

1. 在仓库里检索 `has_stream_override`（用 Grep），统计并分类所有调用点：拒绝型 `ensure!`（本 crate、kernels 的 DtoD、其他 crate）与查询型（`active_cu_stream` 内部）；
2. 对每个拒绝点写一句话：它守护的缓冲流量是什么、失序后果是什么；
3. 思考题：假设删掉 `greedy_stage_readback` 的防护、保留 `greedy_argmax_ids` 的，图道（`launch_captured` + `greedy_stage_readback`）还安全吗？

**需要观察的现象 / 预期结果**：检索实践，无需 GPU。预期发现：图道里 argmax 已在**捕获时**落进图（捕获发生在无 override 的启动阶段），回放本身不经 `greedy_argmax_ids` 的 Rust 入口，所以唯一在运行期于 override 窗口内发射的流敏感操作就是 `greedy_stage_readback` 的 D2H——删掉它的防护，图道在 prefill override 窗口内会排队一次基流 D2H，读到哪一代 `argmax_out` 取决于副流进度，竞态真实存在。结论：每个入口自己的防护都不可省。

#### 4.4.5 小练习与答案

**练习 1**：override 是谁设置的？为什么恰好是它？
**答案**：拆分并发 prefill（tensor.rs L68-71 的文档注释：「Returns true while split-concurrent prefill owns the stream override」）。prefill 的大块计算借副流与解码重叠，是唯一长时间占用 override 的场景；staged 解码路径的缓冲流量依赖同流顺序，因此在这个窗口内必须拒绝而非适配。

**练习 2**：`select_batch` 为什么没有这层防护，staged 三入口却有？
**答案**：`select_batch` 的所有工作（核、DtoD 之外的 H2D/D2H）也依赖流顺序，但它是「当场排队、当场自旋等完」的同步路径——即使在副流上，等完整条流之后数据也必然就绪，只是性能差。staged 路径的卖点是「排队后立刻返回、之后才收集」，一旦跨流失序就没有任何后续同步能兜底（收集只等单次拷贝事件），所以必须在入口拒绝。`token_logprobs_batch` 同理按需拒绝（u3-l2）。

**练习 3**：防护错误为什么是 `Err` 而不是静默降级到 `select_batch`？
**答案**：静默降级会改变语义——`select_batch` 是同步路径，在解码热点上突然出现一次整流同步可能造成尾延迟抖动，而且调用方（gemma4 流水线）已经按「staged 返回即不阻塞」安排了后续发射。让失败显式冒泡，把「何时可以跑 staged」的决定留给引擎层（它在没有 override 的解码线程上调用），是更可诊断的选择。

## 5. 综合实践

**任务**：亲手设计一条「CUDA 图 + 双槽流水」的贪心解码循环伪代码，并把每一操作标注为图内 / 图外、所在流、是否阻塞。这是本讲四件套的总装配。

**参考答案（示例伪代码，非项目代码）**：

```text
# ── 启动阶段（图外，基流）─────────────────────────────
scratch = SampleScratch::new(ctx, vocab, max_rows)      # 一次分配，指针终身稳定
ids_out = arena 的 id 缓冲（CudaSlice<u32>）             # 下一步 embedding 输入
若启用 CUDA 图:
    图外 warm: suppress + greedy_argmax_ids(rows=arena_rows)   # 惰性模块加载留在捕获外
    bucket = 1
    while bucket <= arena_rows:
        capture graph[bucket] = suppress + greedy_argmax_ids(bucket)
        bucket *= 2                                      # rows 是核参数，被烧进图 → 只能分桶

# ── 解码循环（第 N 步，N = 0,1,2,…）───────────────────
loop:
    rows  = 当前活跃行数
    slot  = N % 2                                        # 双槽轮转，在飞步数 ≤ 2

    # ① 发射第 N 步
    若启用图:
        graph[不小于 rows 的最小桶].launch_captured()     # 图内: suppress + argmax(identity 映射) + dtod
        greedy_stage_readback(ctx, slot, scratch)        # 图外: 异步 D2H → pinned 槽 slot，不等
    否则:
        suppress_logits(...)
        greedy_stage_resident(ctx, logits, rows, ids_out, slot, scratch)
        #   ├─ 图内语义段: argmax(identity_rows) + dtod i32→u32 → ids_out
        #   └─ 图外段:     dtoh → pinned 槽 slot，立即返回

    # ② 收集第 N-1 步（先发射后收集 → 等待被第 N 步的 GPU 工作覆盖）
    若 N > 0:
        tokens = greedy_collect_resident(rows[N-1], (N-1) % 2, scratch)
                                   # 纯主机: 只阻塞在槽 (N-1)%2 那次拷贝的事件上
        for (行, token) in zip(第 N-1 步的活跃行, tokens):
            交付 token、判 EOS                          # 主机侧，与 GPU 执行第 N 步并行

    # ③ 记账并推进
    pending = PendingDecode { rows, slot }
    N += 1
```

**标注表（自测用）**：

| 操作 | 图内/图外 | 所在流 | 是否阻塞主机 |
|------|-----------|--------|--------------|
| suppress_logits（图道） | 图内 | 基流 | 否 |
| argmax（读 `identity_rows` 前 rows 项） | 图内 | 基流（`active_cu_stream` 在无 override 时即基流） | 否 |
| DtoD `argmax_out` → `ids_out` | 图内 | 基流 | 否 |
| `greedy_stage_readback` 的 D2H → pinned 槽 | 图外 | 基流 | 否（异步排队） |
| `greedy_collect_resident` 的 `as_slice()` | 图外 | 仅主机线程 | 是，仅阻塞该拷贝自身事件 |

**identity_rows 之问（务必能独立回答）**：indexed argmax 核的输入是一张「输出行 k → arena 行号」的映射表。`select_batch` 每步上传一张紧凑贪心行号表（因为贪心行散布在批的任意位置）；staged 路径整批全贪心，arena 行恰好就是连续的 `0..rows`，于是一张**构造时上传一次、按 max_rows 定长**的恒等表 `0..max_rows` 就够了——核只读它的前 `rows` 项。同一个核因此同时服务「任意行子集」（select_batch）与「连续前缀」（staged）两种映射，不需要第二个连续行专用核，staged 路径还省掉了每步上传行号表的 H2D，也让这次省略后的段（argmax + dtod）恰好是纯设备工作、可被捕获进图。

**对照真实实现**：把自己的伪代码与 [pegainfer-gemma4/src/engine.rs:2828-2903](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-gemma4/src/engine.rs#L2828-L2903) 和 [L3093-3103](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-gemma4/src/engine.rs#L3093-L3103) 逐行核对，确认三处你大概率漏掉的细节：① 收集在第 N 步的发射**之后**（不是发射前）；② 图道与非图道是两条不同入口（`launch_captured` + `greedy_stage_readback` vs `greedy_stage_resident`）；③ 收集前 `ensure!` 行数与发射时一致。若想跑通真实流水，需要 GPU 与 gemma4 权重——**待本地验证**。

## 6. 本讲小结

- CUDA 图的三条硬约束塑造了整个设计：缓冲指针稳定（`SampleScratch` 一次分配）、核参数固定（gemma4 按 2 的幂分桶捕获）、主机阻塞不能进图（回读与收集全部留在图外）。
- 四件套分工：`greedy_argmax_ids`＝图内段（identity 映射的 indexed argmax + DtoD 进 `ids_out`）；`greedy_stage_readback`＝图外异步排队 D2H 到双 pinned 槽之一；`greedy_stage_resident`＝前两者的组合入口；`greedy_collect_resident`＝纯主机收集，只阻塞在该拷贝自身的事件上。
- `identity_rows`（`0..max_rows` 恒等表）让同一个 indexed argmax 核既服务 `select_batch` 的任意行子集、又服务 staged 路径的连续前缀——一个核、两种映射，还省掉每步 H2D。
- 双槽（`STAGED_READBACK_SLOTS = 2`）＝允许两个在飞步骤；配合 gemma4 的「先发射第 N+1 步、再收集第 N 步」，主机等待窗口被 GPU 的下一步执行覆盖，形成两级流水。
- 同流顺序是全部正确性的基石：`argmax(N) → dtod(N) → dtoh(N) → argmax(N+1)` 串在一条基流上，既保证每次回读读到正确的一代数据，也解释了 `select_batch` 里「等拷贝事件＝传递等到 argmax」的注释。
- base-stream-only 防护（三个发射入口 + 底层 DtoD 各自 `ensure!(!has_stream_override())`）封堵的是真实竞态：核落 override 流、回读写死基流，跨流无序会让回读读到 argmax 写完之前的 `argmax_out`。

## 7. 下一步学习建议

- **下一讲 u4-l2（固定内存、流同步与 #704 教训）**：本讲反复出现的 pinned 双槽、`as_slice` 事件级等待、`stream_spin_wait`，其内存与同步纪律的完整来龙去脉（pageable 回读被 KV 恢复大块拷贝拖成 23.6ms 的事故）在下一讲展开。
- **建议顺读的消费方源码**：[pegainfer-gemma4/src/engine.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-gemma4/src/engine.rs#L2828-L2903) 的 `launch_staged`/`collect_pending`/`decode_round`，以及 `CudaGraphState` 的捕获与回放实现——看引擎如何把本讲的四件套嵌进「投机的后继步 + 排空后换行序」的完整调度。
- **收束篇 u4-l3（模型无关的测试策略与基准）**：回看 `tests/select_batch.rs`，思考为什么四件套不在这个文件里出现（提示：它们的行为契约由唯一消费方 gemma4 的引擎承担），并完成「为 staged 路径补一个合成 arena 测试」的练习。
