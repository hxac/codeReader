# u4-l1 · cuda_graph.rs：图捕获、指针稳定性与 padding 页

> 依赖：u2-l3（ops.rs 算子门面）、u3-l2（kv_pool 两段式准入与生命周期）。本讲是 u4 单元「执行与集成机制」的第一讲：走进 core 的执行层，看 decode 热路径如何被录制成一张可反复回放的 CUDA Graph，以及这套机制对内存纪律提出的全部要求。

## 1. 本讲目标

学完本讲，你应该能够：

1. 复述 `CudaGraphState` 的完整状态机：空态 → 流捕获 → 实例化 → 首次启动 → 纯回放，以及捕获中途失败时的「排水」路径。
2. 解释 CUDA Graph 回放为什么要求「指针稳定」，并把它与预分配 buffer、单块 KV 大缓冲、「数据不动、索引动」三条既有设计串成因果链。
3. 讲清 padding 页是什么：为什么它必须在池构造期就预留、为什么它的页号与内容必须是确定性的，以及它和两段式准入如何拼成同一个内存纪律故事。
4. 说出 dump 子模块能把一张已捕获的图暴露成哪些信息（节点/边/kernel 统计、demangled 内核名、grid/block/动态共享内存、边的端口与依赖类型、折叠重复块的 DOT/PNG），以及它的四项前置检查。

## 2. 前置知识

### 2.1 为什么要 CUDA Graph

GPU 上每启动一个 kernel，宿主侧都要走一次驱动调用（launch 开销）。decode 阶段每步只跑十几个小 kernel，算力远没吃满，launch 开销反而成了瓶颈。CUDA Graph 的思路：把一整步的 kernel 序列**录制**成一张 DAG（节点是 kernel，边是依赖），之后每步只需一次 `cuGraphLaunch` 就把整张图递交给 GPU，把 N 次启动摊成 1 次。CLAUDE.md 的架构一节对此有既定事实：**"CUDA Graph — decode path captured inside model executors with pre-allocated buffers to preserve pointer stability"**（decode 路径在模型 executor 内捕获，用预分配 buffer 保持指针稳定）。

### 2.2 流捕获（stream capture）

建图有两条路：手工逐节点搭，或者**流捕获**——把一条流切进捕获模式，期间向它提交的 kernel 不立即执行，而是被录进图。本 crate 用流捕获，三个关键调用：

- `cuStreamBeginCapture_v2(stream, CU_STREAM_CAPTURE_MODE_THREAD_LOCAL)` 开始捕获。`THREAD_LOCAL` 模式向驱动声明「可能不安全的跨界操作只来自本线程」，其他线程照常工作——对单线程推理引擎是干扰最小的选择。
- `cuStreamEndCapture(stream, &graph)` 结束捕获，拿回 `CUgraph` 句柄。
- `cuGraphInstantiateWithFlags(..., CUDA_GRAPH_INSTANTIATE_FLAG_AUTO_FREE_ON_LAUNCH)` 把 `CUgraph` 编译成可启动的 `CUgraphExec`。这个标志让驱动为图执行临时分配的内存在每次启动后自动释放，而不是留着供下次复用——对逐次回放、显存预算吃紧的解码路径更省心。

### 2.3 指针稳定性：回放重放的是「录制时刻的参数」

图节点里记录的不是「Rust 变量」，而是录制那一瞬间每个 kernel 的**参数值**：函数句柄、grid/block 形状、动态共享内存大小、以及所有指针实参。回放时驱动原样重发这组参数，**不会**回头去读你 Rust 结构体里的字段。

由此推出本讲的核心约束：图中出现过的每块显存，地址在整个图生命周期内必须不变；变了，回放就会读写一块已释放或被复用的内存。core 里已为这条约束铺好了三块地基（分别在 u2-l4、u3-l2 讲过）：

- **单块 KV 大缓冲**：`KvPool` 持有一块 `CudaSlice<bf16>`，所有请求的 KV 都是它的视图，构造后永不重分配。
- **页号寻址**：KV 数据地址 \( \text{addr}(\text{page}) = \text{base} + \text{page\_id} \times \text{page\_stride} \times \text{elem\_bytes} \)，页换手只改 `page_id`，不改地址。
- **数据不动、索引动**：批次的请求差异全部编码进 i32 页表数组，数组预分配后原地重填、指针不变。

### 2.4 bucket（桶）与 padding 行

decode 的批大小每步都在变，但图是按固定形状录制的。工程折中是**按批大小分桶**：每个桶录一张图，桶的槽位数固定（如 1/2/4/8）。请求不足一桶时空槽必须填「假行」——padding 行。padding 行的 KV 必须落在一个**页号永远相同、永远有效**的页上，否则空槽无处可靠地指。这个页就是本讲另一位主角：padding 页。它同时是 CLAUDE.md「EP free-running 纪律」里「padding 行是协议面」这条不变量的单卡版本。

### 2.5 与前几讲的衔接

- u2-l1/u2-l3：`crate::tensor` 暴露 `active_cu_stream`（「当前激活流」）；decode 一步调用的全部算子都从 `crate::ops` 走——它们就是被录进图的 kernel 序列。
- u2-l4：`PrefillPagedPlan` 的「预分配 + 原地重填、指针不变」正是图回放需要的形态。
- u3-l2：`KvPool`/`KvState`/`KvDesc` 与两段式准入（`try_reserve` → `commit_reservation`）；本讲把 `padding_page_id()` 补进这个故事。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `pegainfer-core/src/cuda_graph.rs` | 本讲主文件（235 行）：`CudaGraphState` 状态机——捕获、实例化、上传、回放、销毁，以及失败排水 |
| `pegainfer-core/src/cuda_graph/dump.rs` | 图检视：`dump_png`/`validate_graph_dump_request`，用驱动 API 读出节点/边，外部工具 demangle + 渲染 |
| `pegainfer-core/src/cuda_graph/dump/render.rs` | dump 的渲染半边：`human_dot`——折叠重复的 Transformer 层块、给节点着色、压缩内核名 |
| `pegainfer-core/src/kv_pool.rs` | padding 页的家：构造期预留 `padding_permit`，暴露 `padding_page_id()` |
| `pegainfer-qwen3/src/batch_decode.rs` | （消费方示例）`DecodeGraphUse::Serve/CaptureOnly/Replay` 的三路分派 |
| `pegainfer-qwen3/src/batch_decode_buffers.rs` | （消费方示例）每步重填页表数组，空槽填 padding 页 |
| `pegainfer-qwen3/tests/tp_concurrent_decode.rs` | （实践锚点）唯一现成的 dump 端到端测试：导出 PNG 并断言文件存在 |

## 4. 核心概念与源码讲解

### 4.1 图捕获与回放入口：CudaGraphState 的状态机

#### 4.1.1 概念说明

`CudaGraphState` 是 core 提供给模型 crate 的最小图封装：一个结构体持有 `CUgraph`（录制产物）与 `CUgraphExec`（可执行实例）两个裸句柄，外加一个保活的 `Arc<CudaContext>` 锚。模型 executor 的 decode 路径不直接碰驱动 API，只对它说一句话：

> 「这是我的 kernel 序列，第一次请录下来，以后请原样回放。」

它的文档注释交代了关键设计（[cuda_graph.rs:L30-L41](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/cuda_graph.rs#L30-L41)）：

- 第一次 decode 调用捕获图，之后的调用回放它。
- 捕获与回放都跑在 `active_cu_stream`（当前激活流）上。流捕获会把每个 kernel 节点**绑定到捕获流的执行上下文**（注释引 CUDA Programming Guide §4.6.5）：在 Green Context 解码流上捕获的图，无论从哪条流启动都回放在那个分区的 SM 上。因此 **`CudaGraphState` 与一条流绑定**——在多条流上解码（全 SM + green 分区）的调用方必须每条流各持一个状态。这正是 CLAUDE.md 里 gemma4 的 `PEGAINFER_ASYNC_PREFILL`（green:NN 流量隔离通道）能成立的底层前提。

#### 4.1.2 核心流程

```text
new()                        exec == null（空态）
   │  run_or_capture_synchronized(ctx, sync, kernels)
   ▼
capture_and_instantiate:
   sync(BeforeBeginCapture) → cuStreamBeginCapture_v2(THREAD_LOCAL)
   sync(AfterBeginCapture)  → 执行 kernels 闭包（不执行，只录制）
        │ 失败 → cuStreamEndCapture 丢弃 + cuGraphDestroy(aborted) → 返回原错误（排水）
        ▼
   sync(BeforeEndCapture)   → cuStreamEndCapture → CUgraph
   cuGraphInstantiateWithFlags(AUTO_FREE_ON_LAUNCH) → CUgraphExec
   存句柄 + ctx_anchor = Some(ctx.ctx.clone())
   sync(AfterEndCapture)
   sync(BeforeLaunch) → cuGraphLaunch（首次启动） → sync(AfterLaunch)
   ▼
exec != null（已捕获）
   │  之后每一步
   ▼
launch_captured / run_or_capture_synchronized 的快路径：
   sync(BeforeLaunch) → cuGraphLaunch(exec, stream) → sync(AfterLaunch)
```

`CudaGraphPhase`（[cuda_graph.rs:L20-L28](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/cuda_graph.rs#L20-L28)）把这六观测点做成枚举，供 `run_or_capture_synchronized` 的回调消费——用于在捕获/回放的缝隙里插入同步或度量，而不污染被录制的闭包本身。

#### 4.1.3 源码精读

**状态字段与上下文锚**（[cuda_graph.rs:L42-L53](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/cuda_graph.rs#L42-L53)）：裸 `CUgraph`/`CUgraphExec` 句柄自身不持有任何所有权；`ctx_anchor: Option<Arc<CudaContext>>` 在首次捕获实例化时才置为 `Some`（[L210](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/cuda_graph.rs#L210)），保证 CUDA 主上下文比 `Drop` 里的两个 destroy 调用活得久——注释特意指出 `Drop::drop` 先于字段析构运行，靠字段声明顺序是靠不住的。

**Send 的安全论证**（[cuda_graph.rs:L55-L57](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/cuda_graph.rs#L55-L57)）：图句柄只被拥有模型的单推理线程触碰，因此 `unsafe impl Send` 成立。这是「每个模型一个线程」这条引擎形态假设在 core 的落点之一。

**回放入口**（[cuda_graph.rs:L81-L94](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/cuda_graph.rs#L81-L94)）：`launch_captured` 先 `ensure!(self.is_captured(), ...)`，再取 `active_cu_stream(ctx)` 并 `cuGraphLaunch`。`is_captured()` 的判据很简单——`exec` 非空（[L75-L79](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/cuda_graph.rs#L75-L79)）。

**捕获 + 首启的全过程**（[cuda_graph.rs:L128-L159](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/cuda_graph.rs#L128-L159)）：`run_or_capture_synchronized` 先查快路径——`exec` 已存在就直接 `cuGraphLaunch` 返回；否则走 `capture_and_instantiate`，然后照常做一次**首次启动**。注意首次启动是真启动：捕获期间 kernel 并未执行，数据要等到这次 launch 才产出。

**捕获与实例化**（[cuda_graph.rs:L161-L214](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/cuda_graph.rs#L161-L214)），三个细节值得逐行看：

- [L173-L178](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/cuda_graph.rs#L173-L178)：`THREAD_LOCAL` 模式开始捕获。
- [L180-L189](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/cuda_graph.rs#L180-L189)：**失败排水**。闭包出错时，先 `cuStreamEndCapture` 把卡在捕获态的流解救出来、销毁废弃图，再向上传播原始错误——否则流永远停在 capturing 状态，后续任何提交都会失败。这与 u3-l3 权重加载的 `drain_or_abort` 是同一条「宁可控败、不留僵局」纪律。
- [L197-L207](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/cuda_graph.rs#L197-L207)：`cuGraphInstantiateWithFlags` 带 `CUDA_GRAPH_INSTANTIATE_FLAG_AUTO_FREE_ON_LAUNCH`。

**捕获不上传**（[cuda_graph.rs:L106-L126](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/cuda_graph.rs#L106-L126)）：`capture_only` 只捕获 + 实例化 + `cuGraphUpload`，**不启动**。它先 `ensure!(!is_captured())`——对已捕获的图重复 capture 会泄漏现役 exec（[L116-L119](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/cuda_graph.rs#L116-L119)）。注释解释了上传语义：上传是流序的，同流后续 launch 会被驱动排在其后；只有**跨流** launch 才需要在中间补一次同步。

**析构**（[cuda_graph.rs:L223-L234](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/cuda_graph.rs#L223-L234)）：`Drop` 先 `cuGraphExecDestroy` 再 `cuGraphDestroy`，空句柄跳过——`ctx_anchor` 里的 `Arc` 随字段自然析构，恰好排在两个 destroy 之后。

消费方的三路分派在 qwen3：[batch_decode.rs:L227-L232](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-qwen3/src/batch_decode.rs#L227-L232) 把 `DecodeGraphUse` 映射到 `Serve → run_or_capture`（边服务边捕获/回放）、`CaptureOnly → capture_only`（预热期预捕获）、`Replay → launch_captured`（纯回放）。

#### 4.1.4 代码实践

1. **实践目标**：不看讲义，独立把状态机的每次驱动调用与 `CudaGraphPhase` 观测点对上。
2. **操作步骤**：
   - 打开 [cuda_graph.rs:L161-L214](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/cuda_graph.rs#L161-L214)，画一张两列表：左列「synchronize 回调收到的 Phase」，右列「该回调前后紧邻的驱动调用」。
   - 再看 [pegainfer-kimi-k2/src/runner/worker/state.rs:L313](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/runner/worker/state.rs#L313)，找出 kimi-k2 传给 `run_or_capture_synchronized` 的回调在捕获期做了什么。
3. **需要观察的现象**：六个 Phase 在捕获路径全部出现，但在回放快路径只出现 `BeforeLaunch`/`AfterLaunch` 两个；kimi-k2 的回调应该只在图相关时点动一下（以源码为准）。
4. **预期结果**：一张完整的 Phase ↔ 驱动调用对照表。带 GPU 环境可另跑 `cargo test --release -p pegainfer-qwen3 --test tp_concurrent_decode` 观察 TP 解码图的捕获（需模型权重，**待本地验证**）；无 GPU 则本实践纯读码即可完成。

#### 4.1.5 小练习与答案

**练习 1**：`is_captured()` 用 `exec` 判空而不是 `graph`，为什么？

**答案**：实例化成功后才算「可回放」。`CUgraph` 只是录制产物，`launch_captured` 需要的是 `CUgraphExec`；两个句柄在 [L208-L209](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/cuda_graph.rs#L208-L209) 同时赋值，所以用任一个都行，但 `exec` 才是回放的直接依据，语义上更准。

**练习 2**：如果 `capture_and_instantiate` 在闭包失败时不做 [L180-L189](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/cuda_graph.rs#L180-L189) 的排水，会发生什么？

**答案**：流停留在 capturing 状态。此后向这条流提交的任何 kernel 都会报 capture 冲突/非法状态错误，decode 循环从此每次失败——一个局部的 kernel 错误升级成永久性流损坏。排水让「失败的捕获」与「未捕获」在流的状态上等价。

**练习 3**：`capture_only` 为什么拒绝在已捕获的图上再次调用，而 `run_or_capture` 不需要这个检查？

**答案**：`run_or_capture_synchronized` 在 `exec` 非空时直接走回放快路径，闭包根本不会执行，天然不会重复捕获；`capture_only` 的语义就是「无条件重捕」，若 `exec` 已存在且不销毁就覆盖，旧 exec 句柄泄漏且无法再 destroy。

### 4.2 指针稳定性约束：热路径零分配、零同步

#### 4.2.1 概念说明

流捕获是一台「复印机」：闭包里提交的每个 kernel 连同当场的参数值被复印进图。回放是照着复印件重发——复印件不会更新。所以被录进图的闭包必须满足一条契约，写在 `run_or_capture` 的文档注释里（[cuda_graph.rs:L96-L104](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/cuda_graph.rs#L96-L104)）：

> `kernels` must be a pure GPU kernel sequence — no CPU-GPU sync, no allocation.
> （kernels 必须是纯 GPU kernel 序列——无 CPU-GPU 同步、无分配。）

两类操作被禁止的原因都在捕获语义本身：

- **分配**：捕获期间普通 `cudaMalloc` 不是流序操作，会把捕获变成 invalid；即便成功分配，回放也不会重演这次分配，新地址也不会出现在图里。
- **CPU-GPU 同步**（D2H 读回、`cuStreamSynchronize`）：捕获模式下同步语义未定义/报错；更重要的是，回放时「读回结果再做宿主决策」这条路根本不存在——回放只有一次 `cuGraphLaunch`，宿主没有插手机会。

于是「变化」必须全部走**内容可变、地址不变**的通道：常驻显存的页表数组每步原地重填（u2-l4 的 `PrefillPagedPlan`、qwen3 的 `BatchDecodeBuffers`），KV 数据靠页号索引（u3-l2 的单块 buffer），输出落在预分配 buffer。CLAUDE.md 说的「用预分配 buffer 保持指针稳定」就是这套约定的总纲。

#### 4.2.2 核心流程

把一次 decode 步骤按「图生命周期」切开：

```text
【捕获前，一次性】
  预分配：KV 池单块 buffer、页表/CSR 的 i32 设备数组、输出 logits buffer
  首次 decode：闭包内的 kernel 序列被录进图（参数值 = 此刻的指针与形状）

【每步，宿主侧（图外）】
  调度：组批、选桶 → 重填 i32 页表数组（原地，指针不变）
  H2D：小体量元数据上传到预分配数组

【每步，GPU 侧（图内）】
  cuGraphLaunch(exec, stream)  ←  唯一的一次驱动调用
  图内 kernel 按录制时的指针参数读写预分配 buffer
```

判断某段代码能不能进闭包的检查单：

| 操作 | 能否进闭包 | 原因 |
|------|-----------|------|
| `crate::ops` 里的 kernel 启动 | ✅ | 纯流序 GPU 工作 |
| 原地写 i32 页表数组的 D2H/H2D 小拷贝 | ⚠️ 仅捕获期 | 回放不重演拷贝，靠每步图外重填 |
| `DeviceContext` 新分配显存 | ❌ | 捕获失效 + 回放无此分配 |
| `clone_dtoh` 读回结果做分支 | ❌ | 同步违禁 + 回放无宿主决策点 |

#### 4.2.3 源码精读

- 契约原文：[cuda_graph.rs:L96-L104](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/cuda_graph.rs#L96-L104)。
- 捕获绑定执行上下文（Green Context 场景）的注释：[cuda_graph.rs:L30-L41](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/cuda_graph.rs#L30-L41)。
- `active_cu_stream` 的真身在 kernels 侧（[pegainfer-kernels/src/tensor.rs:L76](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/tensor.rs#L76)），配套的 `has_stream_override`（[L64](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/tensor.rs#L64)）回答「当前是否处于流覆盖（green 通道）」。
- 消费方如何保证「闭包纯」：qwen3 把 `graphs` 从 `bufs` 里 `std::mem::take` 出来再放回，正是为了让闭包借用不与批缓冲冲突（[pegainfer-qwen3/src/batch_decode.rs:L208-L238](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-qwen3/src/batch_decode.rs#L208-L238)）；闭包体只调 `self.batch_decode_kernels(...)`，kernel 序列的每一步都从 u2-l3 讲过的 `crate::ops` 门面走。

#### 4.2.4 代码实践

1. **实践目标**：亲手验证「decode 的 kernel 序列里没有任何同步/分配」。
2. **操作步骤**：从 [pegainfer-qwen3/src/batch_decode.rs:L215-L226](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-qwen3/src/batch_decode.rs#L215-L226) 的闭包出发，跳进 `batch_decode_kernels`，沿调用链列出它触碰的每个 `crate::ops`/`crate::tensor` 符号；在列表里检索 `alloc`、`clone_dtoh`、`sync`。
3. **需要观察的现象**：命中项应该只出现在捕获前的准备阶段或每步的图外重填阶段，闭包链内为零。
4. **预期结果**：一张「闭包内 vs 图外」的操作清单。若发现疑似违禁项，先看它是否被 `#[cfg]` 或调用点隔离，再下结论（**待本地验证**，纯读码即可完成）。

#### 4.2.5 小练习与答案

**练习 1**：批大小变了（4 → 8），为什么不能靠回放同一张图解决？

**答案**：图按桶录制成固定形状：grid/block、数组长度都是录制值。批 4 的图回放时 kernel 仍按 4 行算。解法是分桶：批 8 用另一张按 8 录的图（qwen3 的 `BATCH_BUCKETS` + `DecodeGraphUse`），或空槽用 padding 行凑满（见 4.3）。

**练习 2**：如果闭包里偷偷做了一次 `cudaMemcpy` D2H 并按结果分支，捕获会发生什么？回放又会怎样？

**答案**：捕获大概率直接报错（捕获模式禁同步）；即便驱动容忍录制，回放时这个分支决策也不会重演——图会照录制的分支走，宿主读到的「每步决策」在回放里根本不存在。两阶段都坏，所以契约直接写死「no CPU-GPU sync」。

**练习 3**：为什么「每步重填 i32 页表数组」不违反指针稳定性？

**答案**：重填发生在图外（宿主/小拷贝阶段），改的是数组**内容**；图内 kernel 持有的是数组**指针**，数组是预分配的、地址不变。内容动、地址不动，恰是回放语义允许的唯一变化通道。

### 4.3 padding 页：构造期预留的确定性页

#### 4.3.1 概念说明

桶图回放时，空槽也要「演得像真请求」：注意力 kernel 逐行处理，padding 行必须有合法的页表项——一个真实存在、内容无害的页。这就是 padding 页：

- **永远不被分配给任何请求**：构造期一次性签发一张永不释放的 `OwnedPagePermit`，被 `KvPoolInner` 持有到池销毁。
- **页号确定**：`padding_page_id()` 是常量（池生命周期内），空槽每步都指同一个页。
- **内容无害**：源码注释写明「KV append 写入无害垃圾；注意力输出被丢弃」——kernel 对 padding 行照常读写，但结果不进入任何真实请求。

它与两段式准入的关系要放在一个故事里看：准入管「真实页怎么安全地易主」（`try_reserve` → `commit_reservation`，失败靠 Drop 回滚），padding 页管「有一页永远不易主、永远可指」。两者共同保证：**图录制那一刻页表数组里出现的任何页号，在图整个生命周期内都指向合法内存**。

#### 4.3.2 核心流程

```text
KvPool 构造（一次性）:
  alloc_zeros(total)            ← 单块 buffer，永不重分配
  PagePool::new(num_pages)
  try_acquire_many(1) → padding_permit   ← 构造期即预留，永不释放
  之后 available_pages() = num_pages - 1

每步 decode（宿主侧，图外）:
  for 真实请求:   all_page_indices ← kv.page_indices()   （来自 KvState/KvDesc）
  for 空槽:       all_page_indices ← padding_page_id()   （seq_len=1, last_page_len=1）
  原地重填设备端页表数组 → cuGraphLaunch
```

源码注释里的承诺（[kv_pool.rs:L121-L124](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/kv_pool.rs#L121-L124)）原文是：「为桶 CUDA Graph padding 预留的页。padding 槽的 FlashInfer 元数据以 seq_len=1 指向这里。KV 追加写无害垃圾；注意力输出被丢弃。」

#### 4.3.3 源码精读

- **预留时机**：[kv_pool.rs:L226-L229](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/kv_pool.rs#L226-L229) —— 池构造的最后一步 `try_acquire_many(1).expect("pool must have at least 1 page for padding")`。注意用的是 `expect`：零页池直接构造失败，绝不带病运行。
- **页号出口**：[kv_pool.rs:L269-L272](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/kv_pool.rs#L269-L272) —— `padding_page_id()` 返回 permit 里第一页的下标。
- **测试佐证容量账**：[kv_pool.rs:L496-L538](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/kv_pool.rs#L496-L538) 的 `kv_state_lifecycle` 注释直说「5 页总量：1 页 padding 预留，4 页供请求」。
- **消费方填充空槽**：qwen3 在 [batch_decode_buffers.rs:L356-L362](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-qwen3/src/batch_decode_buffers.rs#L356-L362) 对 `real_bs..padded_bs` 的每个空槽压入 `padding_page_id`，并配 `last_page_len=1`、`chunk_size=1` 的「一 token 窗口」元数据；真实请求的页号则来自 `kv.page_indices()`（[L349](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-qwen3/src/batch_decode_buffers.rs#L349)）。
- **双池版本**：gemma4 有滑动窗/全局两个 KV 池，各自预留各自的 padding 页，空槽两行同时填（[pegainfer-gemma4/src/serve.rs:L2203-L2213](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-gemma4/src/serve.rs#L2203-L2213)，注释明确「绝不用真实请求的 KV」）。
- **两段式准入对照**（复习 u3-l2）：[kv_pool.rs:L274-L290](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/kv_pool.rs#L274-L290) 的 `try_reserve`/`KvReservation` 与 [L382-L384](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/kv_pool.rs#L382-L384) 的 `commit_reservation`。

#### 4.3.4 代码实践

1. **实践目标**：算清 padding 页的容量账，并核对消费方的空槽数。
2. **操作步骤**：
   - 读 [kv_pool.rs:L226-L229](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/kv_pool.rs#L226-L229) 与测试注释 [L497](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/kv_pool.rs#L497)，回答：`num_pages=5` 的池，构造完成那一刻 `available_pages()` 是多少？
   - 读 [batch_decode_buffers.rs:L356-L362](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-qwen3/src/batch_decode_buffers.rs#L356-L362)，回答：`real_bs=3`、`padded_bs=8` 的一步里，`padding_page_id` 会被压进数组几次？
3. **需要观察的现象/预期结果**：前者 4（5 − 1 预留），后者 5（8 − 3）。两问都能只靠读码得出确定答案；有 GPU 时可加一行打印验证（改源码仅限本地实验，**待本地验证**）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 padding 页用 `expect` 而不是返回 `Result`？

**答案**：预留只可能因「池配置成 0 页」而失败，这是调用方的构造参数错误，属于编程错误而非运行时资源竞争；对编程错误 fail-fast（panic）比把错误往运行时路径传播更符合本 crate 的纪律（对照 u1-l3 的「GPU 边界靠运行时 expect 显式红灯」）。

**练习 2**：假设把预留从构造期挪到「第一次需要 padding 行时」懒申请，列出至少两个会破的性质。

**答案**：(1) 页号不再恒定——懒申请到的页号取决于当时的 free-list 状态，跨步可能不同；虽然 i32 数组可重填，但任何在捕获期按页号烘焙的元数据（以及「空槽永远指向同一页」的可推理性）被破坏。(2) 申请可能失败——某一步池恰好耗尽时 padding 无页可用，桶图回放直接失去合法页表项；构造期预留则把这次失败提前到启动时。(3) 语义上还可能把一个「曾经发给请求又归还」的脏页当 padding，其内容不再是「无害垃圾」。

**练习 3**：gemma4 为什么要两个 padding 页而 qwen3 只要一个？

**答案**：gemma4 的解码同时消费滑动窗池与全局池两份 KV（两套页表行），空槽在两行里各需一个合法页号，且两池页号空间独立（u2-l4 讲过「页号只在签发它的池内有效」），所以每池各预留一页（serve.rs:L2207/L2210 分别是 `local_pool` 与 `global_pool` 的 `padding_page_id()`）。

### 4.4 dump 子模块：把已捕获的图变成人能读的图

#### 4.4.1 概念说明

一张 decode 图可能有几百个节点（36 层 × 每层十几个 kernel），出问题时既看不见也猜不着。`dump.rs` + `render.rs` 把**已捕获**的图检视成两种产物：

- **detailed DOT**：全保真——每个节点的 id、类型、demangled 内核名、raw 符号、grid/block/动态共享内存；每条边的 from/to 端口与依赖类型。落盘为 `.dot` 文件（与 PNG 同名）。
- **human PNG**：Graphviz 渲染的可读版——自动**折叠重复的 Transformer 层块**（「Repeated physical block ×36 · 14 kernels/instance」一个子图代替 504 个节点）、按入/出度着色、压缩内核名。

入口挂在 core 的公开面上：`dump_png` 与 `validate_graph_dump_request` 从 [cuda_graph.rs:L17-L18](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/cuda_graph.rs#L17-L18) 再出口，qwen3 的 CLI 在启动时校验 `--dump-graph-png` 参数（[pegainfer-qwen3/src/lib.rs:L286](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-qwen3/src/lib.rs#L286)），executor 在捕获后导出（[executor.rs:L3402](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-qwen3/src/executor.rs#L3402)）。

#### 4.4.2 核心流程

```text
validate_graph_dump_request(png_path)     ← 前置检查（启动期，四道门）
  ├─ 1. 扩展名必须是 .png
  ├─ 2. 输出目录可建
  ├─ 3. 驱动 API ≥ 12.3（12_030，取内核名 cuFuncGetName 的需要）
  └─ 4. 外部工具在场：dot -Tpng:cairo（Graphviz Cairo 渲染器）、c++filt（demangler）

dump_png(png_path, title)                 ← 捕获后
  ├─ ensure!(is_captured())
  ├─ inspect():
  │    cuGraphGetNodes        → 全部节点句柄（先数后取两段式）
  │    cuGraphNodeGetType     → 非内核节点记类型字符串
  │    cuGraphKernelNodeGetParams_v2 + cuFuncGetName → 内核名/grid/block/共享内存
  │    c++filt 子进程         → demangled 名（批量、一一对应）
  │    cuGraphGetEdges_v2     → 边 + CUgraphEdgeData（端口、依赖类型）
  │    句柄 → 下标 映射        → GraphDescription { nodes, edges }
  ├─ detailed_dot() 写盘（png 同名 .dot）
  └─ human_dot(title) → dot -Tpng:cairo 渲染 PNG
       └─ render.rs：检测重复块（签名相同 + 连接模式相同，≥3 次重复），
          折叠成子图或单摘要节点；收益不足（省不够一半节点）则不折
```

折叠判定的核心是**保守**：候选块要求「节点签名序列（符号+grid+block+smem）完全一致」且「块内边与块间边在各拷贝间完全一致」([render.rs:L234-L275](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/cuda_graph/dump/render.rs#L234-L275))——防止把「恰好长得像」的不同子图误折叠；选块用动态规划求不重叠的最大节省 ([L304-L334](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/cuda_graph/dump/render.rs#L304-L334))，且只有节省的节点数两倍于总节点数才启用 ([L226-L231](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/cuda_graph/dump/render.rs#L226-L231))。

#### 4.4.3 源码精读

- **返回的摘要**：`CudaGraphDumpSummary { nodes, edges, kernels, dot_path, png_path }`（[dump.rs:L23-L30](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/cuda_graph/dump.rs#L23-L30)）——节点数、边数、其中内核节点数，以及两个产物路径。
- **节点模型**：内核节点带 `raw_symbol`/`demangled`/`grid`/`block`/`dynamic_shared_mem_bytes`，其余节点只记类型名（[dump.rs:L63-L74](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/cuda_graph/dump.rs#L63-L74)）。
- **四道前置门**：[dump.rs:L76-L99](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/cuda_graph/dump.rs#L76-L99)；驱动版本门在 [L101-L115](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/cuda_graph/dump.rs#L101-L115)（`MIN_DRIVER_API_VERSION = 12_030`）。
- **主入口**：[dump.rs:L134-L156](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/cuda_graph/dump.rs#L134-L156)。
- **检视内核节点**：[dump.rs:L299-L340](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/cuda_graph/dump.rs#L299-L340) —— `cuGraphKernelNodeGetParams_v2` 取参数，`cuFuncGetName` 取符号名。**这里就是「图里烘焙了什么」的直接证据**：取到的 grid/block/函数正是回放时原样重发的值。
- **边带端口与依赖类型**：`CUgraphEdgeData` 的 `from_port`/`to_port`/`type_`（[dump.rs:L251-L297](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/cuda_graph/dump.rs#L251-L297)），类型名映射 default/programmatic 在 [L461-L467](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/cuda_graph/dump.rs#L461-L467)。
- **子进程防死锁**：`communicate` 用写线程喂 stdin、同时 `wait_with_output` 排空 stdout/stderr（[dump.rs:L395-L414](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/cuda_graph/dump.rs#L395-L414)），注释点明「整步图比 OS 管道大」。
- **折叠与压缩**：`human_dot`（[render.rs:L54-L114](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/cuda_graph/dump/render.rs#L54-L114)）、重复块摘要节点（[L137-L171](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/cuda_graph/dump/render.rs#L137-L171)，宽度超过 `MAX_VISIBLE_REPEAT_WIDTH = 32` 时折成一个组件形节点）、内核名压缩去模板参数（[L409-L437](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/cuda_graph/dump/render.rs#L409-L437)，cuBLAS GEMV 有特判）。测试 [L564-L579](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/cuda_graph/dump/render.rs#L564-L579) 用「36 层 × 14 kernel」的 qwen 形状验证折叠——这正是 Qwen3-4B decode 图的真实形态（36 层全注意力，对照 [kv_pool.rs:L486-L493](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/kv_pool.rs#L486-L493) 的几何测试）。

#### 4.4.4 代码实践

1. **实践目标**：在不装 GPU 的机器上把 dump 的行为链与前置条件摸清。
2. **操作步骤**：
   - 通读 [pegainfer-qwen3/tests/tp_concurrent_decode.rs:L75-L112](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-qwen3/tests/tp_concurrent_decode.rs#L75-L112)：它把 PNG 路径指到临时目录、调用 `validate_graph_dump_request`、以 `dump_graph_png` 选项启动引擎，最后断言 PNG 与 DOT 文件都存在。
   - 列出在你机器上跑通它需要的全部条件（GPU/模型权重/驱动版本/graphviz/c++filt/扩展名）。
   - 有条件时运行：`PEGAINFER_TEST_MODEL_PATH=models/Qwen3-4B cargo test --release -p pegainfer-qwen3 --test tp_concurrent_decode -- --nocapture`（**待本地验证**）。
3. **需要观察的现象**：测试产出的 `.dot` 是全保真文本，可直接打开搜索某个内核名；PNG 中重复层块应显示为一个虚线框摘要。
4. **预期结果**：能回答「dump 产物有哪两个文件、分别给谁看」：`.dot` 给程序/严格对拍用（含 raw_symbol 与全部参数），PNG 给人看（折叠重复块）。

#### 4.4.5 小练习与答案

**练习 1**：dump 能在捕获前调用吗？依据是哪一行？

**答案**：不能。`dump_png` 第一行就是 `ensure!(self.is_captured(), "CUDA graph dump requested before capture")`（[dump.rs:L135-L139](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/cuda_graph/dump.rs#L135-L139)）——没有图就没有可检视的东西。

**练习 2**：为什么驱动版本门定在 12.3？

**答案**：取内核名用的 `cuFuncGetName` 需要驱动 API ≥ 12.3（常量 `12_030`，[dump.rs:L101-L115](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/cuda_graph/dump.rs#L101-L115)）；没有内核名，dump 就只剩节点计数，诊断价值大打折扣，所以直接在启动期拒绝。

**练习 3**：`detailed_dot` 与 `human_dot` 的受众差异体现在哪里？

**答案**：`detailed_dot`（dump.rs:L416-L454）逐节点保留 raw_symbol、demangled 名、grid/block/smem 与边端口——机器可对拍、可 diff；`human_dot`（render.rs）压缩内核名、折叠重复块、按度着色——36 层的图从 500+ 节点缩成「1 个头 + 36 份摘要 + 1 个尾」，人一眼能看出结构异常（如某层多了一个 kernel、某处出现意外的分支）。

## 5. 综合实践

写一篇结构化笔记（建议 1–2 页），回答三个问题，然后完成一次交汇点排查：

**问题 1：如果 decode 步骤的 KV 页地址每步都变，图回放会坏在哪一行语义上？**

提示：回放的语义是 [cuda_graph.rs:L140-L148](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/cuda_graph.rs#L140-L148) 的 `cuGraphLaunch(exec, stream)` 原样重发**录制时刻烘焙进节点的参数**；参数里有什么、可以在 [dump.rs:L299-L340](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/cuda_graph/dump.rs#L299-L340) 看到证据（那是同一份 `cuGraphKernelNodeGetParams` 的只读视角）。请精确指出：坏的不是「这一行 Rust 代码」，而是图内注意力 kernel 节点烘焙的指针实参指向已释放/被复用的显存——读写的是旧地址，而 Rust 侧已无任何引用。再对照 [kv_pool.rs:L221-L224](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/kv_pool.rs#L221-L224) 说明 core 的设计为什么天然免疫（单块 buffer + 页号索引，地址集合在构造期封闭）。

**问题 2：padding 页为什么必须在池构造期就预留，而不是运行期再申请？**

提示：从三个角度作答——(a) 确定性：构造期签发使页号成为池生命周期常量（[kv_pool.rs:L226-L229](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/kv_pool.rs#L226-L229)、[L269-L272](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/kv_pool.rs#L269-L272)）；(b) 可用性：运行期申请可能因池耗尽而失败，桶图回放失去合法页表项，而构造期失败会被 `expect` 提前拦下；(c) 内容无害性：永不外借的页只会被 padding 行写入「无害垃圾」（[L121-L124](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/kv_pool.rs#L121-L124) 注释原文），这是 EP free-running 纪律「padding 行是协议面」的前提。

**问题 3：dump.rs 能暴露图的哪些信息（以原文为准）？**

提示：以 `CudaGraphDumpSummary` 的五个字段（[dump.rs:L23-L30](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/cuda_graph/dump.rs#L23-L30)）为纲，展开到 detailed DOT 的每类标签与 render.rs 的折叠产物，并注明四项前置检查。

**交汇点排查**：在源码中找到「图捕获」与「KvState/KvDesc」的交汇处并引用行号。core 侧的锚点是 `padding_page_id()`（kv_pool.rs:L269-L272，连同 L121-L124 的注释——这是 kv_pool 全文件唯一提及 CUDA Graph 的地方）；消费侧把两条线拧在一起：qwen3 [batch_decode_buffers.rs:L348-L362](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-qwen3/src/batch_decode_buffers.rs#L348-L362)（真实页来自请求的页表、空槽来自 padding 页，拼成同一份 CSR）与 [batch_decode.rs:L227-L232](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-qwen3/src/batch_decode.rs#L227-L232)（拼好的 kernel 序列交给 `run_or_capture`/`capture_only`）。把这条链画成时序：构造期预留 → 每步图外重填 → 图内回放消费。

## 6. 本讲小结

- `CudaGraphState` 是一个两态状态机：`exec` 为空时走「begin capture（THREAD_LOCAL）→ 闭包 → end capture → instantiate（AUTO_FREE_ON_LAUNCH）→ 首次 launch」，非空时只做一次 `cuGraphLaunch`；闭包失败有专门的排水路径，保证流不滞留在捕获态。
- 回放重发的是录制时刻烘焙的参数值，因此闭包契约是「纯 GPU kernel 序列——无同步、无分配」；一切变化必须走「内容动、地址不动」的通道。
- padding 页是构造期一次性预留、永不外借的确定性页：空槽以 seq_len=1 指向它，写入无害垃圾、输出被丢弃。它与两段式准入（try_reserve → commit，Drop 回滚）合起来构成「图录制时见过的每个页号在其生命周期内永远合法」的完整内存纪律。
- 图捕获绑定捕获流的执行上下文，`CudaGraphState` 与流一一对应——这是 green 通道（`PEGAINFER_ASYNC_PREFILL`）多图并存的基础。
- dump 子模块通过驱动 API 读出已捕获图的节点/边全量信息，经 `c++filt` demangle 与 Graphviz 渲染成全保真 DOT 与折叠重复块的人读 PNG，前置四道检查（.png 扩展名、目录、驱动 ≥ 12.3、dot/c++filt 在场）。

## 7. 下一步学习建议

- **本单元下一讲（u4-l2）**：cpu_topology、logging、tracing——宿主侧运行时服务，继续补齐 core 执行环境的另一半。
- **顺藤摸瓜读模型 crate**：从 [pegainfer-qwen3/src/batch_decode.rs:L227-L232](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-qwen3/src/batch_decode.rs#L227-L232) 出发读桶图的完整装配；再看 [pegainfer-kimi-k2/src/runner/worker/state.rs:L313](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/runner/worker/state.rs#L313) 里 `run_or_capture_synchronized` 的回调用法（`CudaGraphPhase` 的真实消费者）。
- **为 u5-l1 预习**：把本讲的 padding 确定性、排水纪律对照 CLAUDE.md 的「EP Free-Running Discipline」三条不变量（永不等待、每步集体链固定、launch 次数即全局时钟），并打开 `docs/models/glm52/free-running-dp.md` 通读一遍。
- **动眼不如动手**：在有 GPU 的机器上用 `--dump-graph-png` 导出一张真实 decode 图，对照 4.4 的折叠规则数一数「36 层 × 每层 kernel 数」是否与模型层数吻合（**待本地验证**）。
