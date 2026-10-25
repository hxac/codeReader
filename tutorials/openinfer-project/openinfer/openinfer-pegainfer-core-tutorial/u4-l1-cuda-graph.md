# cuda_graph.rs：图捕获、指针稳定性与 padding 页

> 前置讲义：u2-l3（ops 算子门面）、u3-l2（kv_pool 两段式准入）。本讲是 u4 单元（执行与集成机制）的第一讲。

## 1. 本讲目标

学完本讲，你应该能够：

- 讲清 `CudaGraphState` 的完整生命周期：空态 → 流捕获 → 实例化 → 回放，以及捕获失败时的排水路径。
- 解释 CUDA Graph 回放为什么要求「指针稳定」，以及它与预分配 buffer、单块 KV buffer、「数据不动、索引动」三条既有设计之间的因果关系。
- 把三件事串成一个一致的内存纪律故事：padding 页（构造期预留的确定性页）、两段式准入（try_reserve → commit）、图捕获（热路径零分配）。
- 说出 dump 子模块能把一张已捕获的图暴露成哪些信息（节点/边/kernel 统计、demangled 名、grid/block、DOT 与 PNG），以及它的前置条件检查。

## 2. 前置知识

### 2.1 为什么要 CUDA Graph

GPU 上每启动一个 kernel，宿主侧都要走一次驱动调用（launch 开销）。decode 阶段每步只跑十几个小 kernel，算力还没吃满，launch 开销反而成了瓶颈。CUDA Graph 的思路是：把一整步的 kernel 序列**录制**成一张 DAG（节点是 kernel，边是依赖），之后每步只需一次 `cuGraphLaunch` 就把整张图递交给 GPU，摊平启动开销。

### 2.2 流捕获（stream capture）

CUDA 提供两条建图路线：手工逐节点建图，或者**流捕获**——把一条流切进捕获模式，期间向它提交的 kernel 不执行、而是被录进图。本 crate 用的是流捕获：

- `cuStreamBeginCapture_v2(stream, CU_STREAM_CAPTURE_MODE_THREAD_LOCAL)` 开始捕获。`THREAD_LOCAL` 模式只影响当前线程在该流上的操作，别的线程照常工作——这是单线程推理引擎里干扰最小的选择。
- `cuStreamEndCapture(stream, &graph)` 结束捕获，拿回 `CUgraph`。
- `cuGraphInstantiateWithFlags(..., CUDA_GRAPH_INSTANTIATE_FLAG_AUTO_FREE_ON_LAUNCH)` 把 `CUgraph` 编译成可启动的 `CUgraphExec`。`AUTO_FREE_ON_LAUNCH` 让图在每次启动后自动释放其内部临时内存。

### 2.3 指针稳定性：回放重放的是「录制时刻的参数」

图节点里记录的不是「Rust 变量」，而是录制那一瞬间每个 kernel 的**参数值**——grid/block 形状、共享内存大小、以及所有指针实参。回放时驱动原样重发这组参数，**不会**回头再读你 Rust 结构体里的字段。

由此推出本讲的核心约束：图中出现过的每块显存，地址在整个图生命周期内必须不变；变了，回放就会读写一块已经释放或被复用的内存。CLAUDE.md 对此有一句既定事实：**「CUDA Graph — decode path captured inside model executors with pre-allocated buffers to preserve pointer stability」**（decode 路径在模型 executor 内捕获，用预分配 buffer 保持指针稳定）。

### 2.4 与前几讲的衔接

- u2-l1 讲过 `active_cu_stream`：core 的张量模块暴露「当前激活流」；捕获与回放都跑在这条流上。
- u2-l3 讲过 ops 门面：decode 一步调用的所有算子都从 `crate::ops` 走，它们就是被录制进图的 kernel 序列。
- u2-l4 讲过「数据不动、索引动」：分页注意力的请求差异全部编码进 i32 页表数组，`PrefillPagedPlan` 用预分配加原地重填保持指针不变——这正是图回放需要的形态。
- u3-l2 讲过 `KvPool`/`KvState`/`KvDesc` 与两段式准入：本讲要把 `padding_page_id()` 补进这个故事。

### 2.5 bucket（桶）与 padding 行

decode 的批大小每步都在变，但图是按固定形状录制的。工程折中是**按批大小分桶**：每个桶录一张图，桶的槽位数固定。请求不足一桶时，空槽必须填一行「假数据」——这就是 padding 行。padding 行的 KV 必须落在一个**页号永远相同、永远有效**的页上，否则图里页表数组虽然可以原地重填，空槽却无处可靠地指。这个页就是本讲的另一主角：padding 页。
