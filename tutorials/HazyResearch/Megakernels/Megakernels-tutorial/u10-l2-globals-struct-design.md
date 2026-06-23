# globals 结构设计：kittens gl 与 TMA

## 1. 本讲目标

本讲聚焦 Megakernels 的低延迟 LLaMA demo 中那个「把整个推理状态打包成一个结构体」的核心设计。学完后你应当能够：

1. 说清楚 `globals_t` 这个模板结构体里都装了什么：模型权重、激活缓冲、KV cache、虚拟机（VM）的控制结构、以及几个标量参数。
2. 理解 `kittens::gl<...>` 全局张量类型是如何用「数据类型 + 多个维度 + 若干个视图（view）」来描述一块显存的，并能区分静态维度（编译期固定）与动态维度（`-1`，运行期才知道大小）。
3. 理解 `weights_t`、`norm_weights_t`、`activations_t`、`kv_cache_t` 等 `gl` 别名各自的形状与含义，特别是为什么 `weights_t` 要用 `st_bf<16, 512>` 作为子块类型。
4. 理解 `kv_cache_t` 内嵌的 TMA descriptor（`tma::descriptor<...>`）解决了什么问题。
5. 理解 `grid()`、`block()`、`dynamic_shared_memory()` 三个方法为何要挂在 `globals_t` 上，它们如何成为内核启动器（launcher）的接口契约。
6. 理解 `llama_1b_globals` 这个特化是如何把一组 `LLAMA_1B_*` 常量填进模板，得到一个具体可用的 globals 类型的。

## 2. 前置知识

阅读本讲前，建议你已经了解：

- **CUDA 的 grid / block / shared memory 启动模型**：一次 kernel launch 由 `grid`（多少个线程块）、`block`（每个块多少线程）和动态共享内存大小三个量决定。本讲的 `grid()/block()/dynamic_shared_memory()` 正是提供这三个量。
- **bf16 与 matvec（矩阵-向量乘）**：LLaMA 推理一次只处理一个 token，所以权重矩阵乘法退化为「向量 × 矩阵」，即 matvec。权重大多以 bf16 存储。
- **TMA（Tensor Memory Accelerator）**：Hopper/Blackwell GPU 上的硬件单元，可以用一个 descriptor 描述一块多维显存区域，然后由单条指令异步搬动一个「瓦片（tile）」到共享内存，绕开普通的全局加载。Megakernels 大量依赖 TMA。
- **共享内存分页（paging）**：Megakernels 把动态共享内存切成固定大小的「页（page）」，loader 把权重/激活按页搬进来，consumer 按页读出去。这一机制是「为什么用 `st_bf<16,512>`」的答案基础。
- **前置讲义**：本讲依赖 u5-l1（共享内存分页与 page 的概念）与 u9-l3（VM 的指令/时序布局 `instructions`/`timings`）。如果你还没读，至少要记得 `instructions` 和 `timings` 是控制器用来驱动整个 megakernel 的两块表。

> 说明：`kittens::gl`、`kittens::st_bf`、`kittens::sv_bf`、`kittens::tma::descriptor` 等都来自 ThunderKittens 子模块（`.gitmodules` 指向 `ThunderKittens`，在本检出中未实际拉取，目录为空）。本讲不编造这些类型的内部实现，而是依据 Megakernels demo 对它们的**使用方式**来讲解其语义契约。凡是无法从本仓库源码确证之处，都会明确标注。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [`demos/low-latency-llama/llama.cuh`](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cuh) | **本讲主角**。定义 `globals_t` 模板、各 `gl` 别名、`grid/block/dynamic_shared_memory`，以及 `llama_1b_globals` 特化。 |
| [`demos/low-latency-llama/llama.cu`](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cu) | 用 `kittens::py::bind_kernel` 把 globals 的各字段地址暴露给 Python，是理解 globals 字段如何被「逐个绑定」的入口。 |
| [`include/config.cuh`](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/config.cuh) | `default_config`：线程数、共享内存页大小、静态/动态共享内存切分等。`block()` 与 `dynamic_shared_memory()` 都最终回到这里。 |
| [`include/megakernel.cuh`](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/megakernel.cuh) | 真正的内核函数 `mk`，用 `__grid_constant__ globals g` 接收 globals，是理解「globals 以常量形式进入内核」的依据。 |
| [`demos/low-latency-llama/matvec_pipeline.cuh`](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh) | matvec 的加载流水线：把权重页 `reinterpret` 成 `st_bf<16,512>`，证明「一页 = 一个权重瓦片」。 |
| [`demos/low-latency-llama/attention_partial.cu`](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_partial.cu) | 用 `g.k_cache`/`g.v_cache` 做 TMA 加载，证明 `kv_cache_t` 的 4 维索引语义与 descriptor 用法。 |
| [`demos/low-latency-llama/rms_matvec_rope_append.cu`](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/rms_matvec_rope_append.cu) | `load_iter` 接收 `st_bf<16,512>&` 并对其做 TMA 加载，是权重 gl 用法的最短证据。 |

## 4. 核心概念与源码讲解

### 4.1 globals_t：把整条推理流水线塞进一个 grid constant

#### 4.1.1 概念说明

Megakernels 的内核 `mk` 只接收**一个参数**：globals（见 4.4 节）。这个 globals 不是某一块数据，而是「一次推理所需的全部状态的集合」——所有权重、所有激活缓冲、KV cache、VM 控制结构、以及 `pos_id`/`attn_scale` 之类的标量。

把它打包成单个结构体有两点好处：

1. **一次传递**：内核用 `__grid_constant__` 语义接收它，整块数据以只读方式进入每个 SM 的常量缓存/寄存器，避免在热路径上拆开几十个参数。
2. **模板化即配置化**：把模型尺寸（层数、hidden_dim、SM 数……）作为模板参数传入，编译器就能在编译期算出所有的 `gl` 形状、页数、tile 大小，做到「零运行期开销的配置」。

#### 4.1.2 核心流程

`globals_t` 的生命周期可以概括为：

1. **编译期**：模板参数 `_num_layers/_hidden_dim/...` 决定所有 `gl` 的静态维度与 tile 类型；`constexpr static` 字段把同样的尺寸暴露成可在内核里直接用的常量。
2. **宿主侧（Python）**：`llama.cu` 的 `bind_kernel` 把 globals 的**每个字段**（`qkv_weights`、`k_cache`、`pos_id`……）的成员指针登记给 Python，Python 侧给它们分配真实显存并填写 descriptor。
3. **启动时**：launcher 读取 `grid()/block()/dynamic_shared_memory()` 三个方法，据此调用 `cudaLaunchKernel`。
4. **内核内**：所有 worker（loader/storer/launcher/controller + consumer warps）通过同一个 `g` 引用访问自己需要的权重与缓冲。

#### 4.1.3 源码精读

`globals_t` 是一个 9 参数模板，开头把模板参数原样存成 `constexpr static` 字段，供内核内随时取用：

模板声明与静态字段（[llama.cuh:48-71](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cuh#L48-L71)）：模板参数即模型几何尺寸，存成静态常量后，后面的 `gl` 类型与内核逻辑都能直接引用 `head_dim`、`hidden_dim` 等。

接着是 VM 相关的三块控制结构（[llama.cuh:104-110](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cuh#L104-L110)）：`Bar` 是 barrier 表（`kittens::gl<uint, 1, -1, -1, num_attention_heads + 2*num_kv_heads>`，每个 head/Q/K/V 一个槽），`instructions` 与 `timings` 是控制器读取的指令流与时序表（来自 u9-l3）。

然后是模型权重字段（[llama.cuh:112-124](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cuh#L112-L124)）：`qkv_weights`、`o_weights`、`up_weights`、`gate_weights`、`lm_head_weights` 是 `weights_t`；`down_weights` 是 `weights_big_indim_t`（intermediate 维更宽）；三个 norm 权重是 `norm_weights_t`；KV cache 是两个 `kv_cache_t`。

其余是激活缓冲与标量参数（[llama.cuh:126-142](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cuh#L126-L142)）：`hidden_states`、`q_post_rope`、`attn_out`、`silu_out`、`logits` 等，加上 `pos_id`、`attn_scale`、`rms_norm_eps`、`skip_attn_reduction` 四个标量。

最后是启动三件套（[llama.cuh:144-146](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cuh#L144-L146)）：`grid()` 返回 `dim3(sm_count)`（每个 SM 一个 block），`block()` 返回 `dim3(config::NUM_THREADS)`，`dynamic_shared_memory()` 返回 `config::DYNAMIC_SHARED_MEMORY`。这三个方法会在 4.4 节展开。

#### 4.1.4 代码实践

**目标**：从 globals 的字段清单反推「一次 LLaMA 前向需要哪些张量」。

1. 打开 `demos/low-latency-llama/llama.cu`，看 `bind_kernel` 一口气登记了哪些 `&llama_1b_globals::XXX`（[llama.cu:28-52](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cu#L28-L52)）。
2. 把它们分成三类：**权重**（`*_weights`）、**KV cache**（`k_cache`/`v_cache`）、**激活/中间结果**（`hidden_states`/`attn_*`/`silu_out`/`logits`）、**标量**（`pos_id` 等）。
3. 对照标准 LLaMA 结构，确认：qkv 投影、o 投影、up/gate/down 三个 MLP 投影、两个 RMSNorm、最后的 lm_head 都有对应权重字段。

**预期结果**：你会得到一张「字段 → LLaMA 算子」的对照表，这是后续阅读每个 `*.cu` 算子文件的索引。

#### 4.1.5 小练习与答案

**练习 1**：globals 里同时有 `attn_norm_weights`、`mlp_norm_weights`、`lm_head_norm_weights` 三个 norm 权重，它们为什么是三个而不是一个？

**答案**：它们对应 LLaMA 中三个不同的 RMSNorm 位置——attention 块前、MLP 块前、以及（本 demo 特有的）lm_head 前的最终 norm。每个 norm 的权重是独立参数，所以各占一个 `norm_weights_t` 字段。

**练习 2**：为什么 `pos_id`、`attn_scale` 这些标量也要放进 globals，而不是作为单独的 kernel 参数？

**答案**：因为 `mk` 内核只接收**一个** `__grid_constant__ globals g` 参数（见 4.4.3）。所有运行期变化的量都必须能从这个结构里取到，因此标量也被收纳其中，保持「单参数启动」的简洁与一致。

---

### 4.2 gl 全局张量类型：weights_t / norm_weights_t / activations_t

> 本节对应最小模块「globals_t 字段与 gl 类型」。

#### 4.2.1 概念说明

`kittens::gl<...>`（global layout）是 ThunderKittens 用来描述「一块多维显存 + 如何用 tile 访问它」的类型。在本 demo 里它承担两个职责：

- **声明一块显存的形状**：用一组维度参数描述这块显存是几维、每维多大。
- **声明访问粒度**：末尾附带若干「视图（view）」类型（如 `st_bf<R,C>`、`sv_bf<N>`、`tma::descriptor<...>`），告诉框架「我打算用什么形状的 tile 把这块显存搬进共享内存」。

理解 `gl` 的关键是看清它的模板参数顺序。本 demo 里的写法统一是：

```
kittens::gl<元素类型, dim0, dim1, dim2, dim3, view0, view1, ...>
```

其中：

- **维度**：正数表示「编译期固定的大小」；`-1` 表示「运行期才知道大小」（动态维度）。`1` 通常是把 batch 这一维钉死为 1（单 token 推理）。
- **视图**：`st_bf<R,C>` 是「一个 R×C 的 bf16 共享内存 tile」；`sv_bf<N>` 是「一个 N 元素的 bf16 共享内存向量」；`tma::descriptor<tile, box>` 显式给出一个 TMA 描述符所对应的 tile 形状。

> 关于 gl 的精确索引语义（传几个下标、下标对应哪一维），ThunderKittens 内部有完整的定义，但子模块在本检出中未拉取，无法逐行引用。本讲以下方「实际 load/store 调用」为准来推断语义，涉及不可确证处会标注。

#### 4.2.2 核心流程

把 `gl` 当成一个「带形状与访问策略的显存句柄」：

1. 宿主侧为其分配显存并（若需要）填充 TMA descriptor；
2. 内核内用 `kittens::tma::load_async(tile, g.xxx, {下标...}, sem)` 把一个 tile 异步搬进共享内存；
3. 下标里出现的维数对应 gl 的「非 batch」维度，最后一个下标通常落在最内层（contiguous）维度的 tile 块上。

下面用 demo 里的几个核心 `gl` 别名说明形状含义（`matvec_block_size=16`、`hidden_dim=2048`、`head_dim=64`、`intermediate_dim=8192` 取自 4.5 节）。

| 别名 | gl 定义（形状） | 含义 |
| --- | --- | --- |
| `weights_t` | `gl<bf16, 1, -1, -1, hidden_dim, st_bf<16,512>>` | batch=1；两维运行期大小（层、输出块）；最内层是 `hidden_dim=2048` 的连续维；用 `st_bf<16,512>` 瓦片访问。源码注释「assumed to be N by 2048 (X@W.T)」。 |
| `weights_big_indim_t` | `gl<bf16, 1, -1, -1, intermediate_dim, st_bf<16,512>>` | 同上，但最内层是 `intermediate_dim=8192`，用于 down 投影（输出回到 hidden 维，但权重行宽为 intermediate）。 |
| `activations_t` | `gl<bf16, 1, 1, 1, hidden_dim, sv_bf<2048>, sv_bf<64>, sv_bf<16>>` | 单个 hidden 向量（1×1×1×2048）；附带三种向量视图（整段、head_dim 段、matvec 段）。 |
| `logits_t` | `gl<bf16, 1, 1, 1, -1, sv_bf<16>>` | vocab 维运行期大小（128256）；用 16 元素向量段访问。 |
| `norm_weights_t` | `gl<bf16, 1, 1, -1, hidden_dim, sv_bf<2048>, sv_bf<16>>` | 一组 RMSNorm 权重：每层一行 hidden 维向量。 |
| `rope_table_t` | `gl<float, 1, 1, -1, head_dim, sv_fl<64>>` | RoPE 的 cos/sin 表：每行 head_dim=64 个 float。 |

定义见 [llama.cuh:77-93](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cuh#L77-L93)。

#### 4.2.3 源码精读

**weights_t 的定义**（[llama.cuh:77-79](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cuh#L77-L79)）：注意末尾的 `kittens::st_bf<matvec_block_size, 512>` 即 `st_bf<16, 512>`，这正是 matvec 流水线按页加载时的 tile 形状。

**norm_weights_t / activations_t / logits_t**（[llama.cuh:84-92](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cuh#L84-L92)）：注意它们末尾大多是 `sv_*`（向量视图），因为 norm 权重和激活本质是一维向量，访问粒度是「整段」或「head_dim 段」或「matvec 段」。

**实际用法（最短证据链）**：`rms_matvec_rope_append.cu` 的 `load_iter` 把一块共享内存页当作 `st_bf<16,512>&` 接收，然后对 `g.qkv_weights` 做 TMA 加载（[rms_matvec_rope_append.cu:62-70](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/rms_matvec_rope_append.cu#L62-L70)）。这直接证明：`weights_t` 末尾的 `st_bf<16,512>` 就是它每次 TMA 加载的瓦片形状。

#### 4.2.4 代码实践

**目标**：验证 `weights_t` 为何用 `st_bf<16, 512>` 作为子块类型。

1. 打开 [`demos/low-latency-llama/matvec_pipeline.cuh`](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh)。
2. 注意 loader 对每个 iter 循环 4 次（`for (int i = 0; i < 4; i++)`），把第 `i` 个权重页 `reinterpret` 成 `kittens::st_bf<16, 512>&`（[matvec_pipeline.cuh:135-141](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh#L135-L141)）。
3. 注意 `tma::expect_bytes(sem, sizeof(bf16) * 2048 * 16)`（[matvec_pipeline.cuh:133](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh#L133)）——一次 iter 预期 4 页共 \(2048\times16\) 个 bf16。
4. 打开 [`include/config.cuh`](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/config.cuh)，看 `PAGE_SIZE = 16384`（[config.cuh:42](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/config.cuh#L42)）。

现在做关键计算：

- 一个 `st_bf<16,512>` tile 的大小：\(16 \times 512 \times 2\,\text{B} = 16384\,\text{B}\)。
- 这**正好等于 `PAGE_SIZE`**，所以「一个权重 tile = 一个共享内存页」。loader 按页搬，consumer 按页读，类型天然对齐，无需任何额外偏移换算。
- 行数 `16 = matvec_block_size`，是 matvec 输出瓦片的行粒度；列数 `512` 使得 4 个 tile 拼出 \(4\times512 = 2048 = \text{hidden\_dim}\)，正好覆盖一整条 hidden 维的规约轴。

**预期结果**：你能用一句话回答——`st_bf<16,512>` 同时满足「输出行粒度 = matvec 块（16）」「4 块拼出 hidden 维（2048）」「单 tile 恰好一页（16384 B）」三个约束。

#### 4.2.5 小练习与答案

**练习 1**：`activations_t` 末尾跟了三个视图 `sv_bf<2048>`、`sv_bf<64>`、`sv_bf<16>`，分别可能在什么场景下被使用？

**答案**：`sv_bf<2048>` 用于把整段 hidden 向量搬进/搬出共享内存；`sv_bf<64>` 对应 `head_dim`，用于按 head 切片（如 RoPE、attention 的 q/k/v）；`sv_bf<16>` 对应 `matvec_block_size`，用于按 16 元素的小段累加结果。同一块显存挂多个视图，是为了按不同访问粒度复用同一份指针。

**练习 2**：为什么 `logits_t` 的最内层维度是 `-1`（动态），而 `activations_t` 是 `hidden_dim`（静态）？

**答案**：激活的 hidden 维是模型结构决定的常数（2048），编译期就能定；而 logits 的 vocab 维（128256）在这里被声明成动态 `-1`，留给运行期（宿主侧分配）填入，保持 gl 对词表大小的通用性。

---

### 4.3 kv_cache_t 与内嵌的 TMA descriptor

> 本节对应最小模块「kv_cache_t TMA descriptor」。

#### 4.3.1 概念说明

KV cache 存的是每个层、每个序列位置、每个 KV head 的 K/V 向量。它有两个特点让 TMA 特别合适：

1. **多维且规则**：可以看作一个 `[层数, 序列块, kv_head, head_dim]` 的 4 维张量，TMA descriptor 天生擅长描述这种多维规则区域。
2. **大量小瓦片异步加载**：attention 要反反复复取一小块（一个 `kv_block_size × head_dim` 的瓦片），用 TMA 的「单指令搬一个瓦片」能省掉普通全局加载的开销。

`kv_cache_t` 的特别之处在于：它把一个 **TMA descriptor** 作为 gl 的一个视图直接「内嵌」进来——`kittens::tma::descriptor<kittens::st_bf<kv_block_size, head_dim>, 1>`。这等于在类型层面声明：「这块显存除了能按向量段访问，还配了一个 TMA 描述符，它描述的瓦片形状是 `st_bf<kv_block_size, head_dim>`（即 `16×64`）」。

#### 4.3.2 核心流程

attention 使用 KV cache 的流程：

1. loader 用 `tma::load_async` 给出目标共享内存 tile、`g.k_cache`/`g.v_cache`、一组 4 维下标、以及一个完成信号量；
2. TMA 硬件依据内嵌的 descriptor 异步把对应瓦片搬进共享内存；
3. consumer 等 `K_arrived`/`V_arrived` 信号量后即可使用。

descriptor 里的 `1`（第二个模板参数）是 TMA 的 box/box-dim 相关配置，具体语义属于 ThunderKittens 内部，本仓库未拉取子模块，故标「待确认」；但其效果是明确的：一次 `load_async` 搬动一个 `16×64` 的 bf16 瓦片。

#### 4.3.3 源码精读

**kv_cache_t 的定义**（[llama.cuh:94-95](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cuh#L94-L95)）：

```
kittens::gl<kittens::bf16, -1, -1, -1, head_dim,
            kittens::sv_bf<matvec_block_size>,
            kittens::tma::descriptor<kittens::st_bf<kv_block_size, head_dim>, 1>>
```

读法：4 维全是动态/部分动态——`[层数(-1), 序列块(-1), kv_head(-1), head_dim(64)]`；附带一个向量视图 `sv_bf<16>`（用于按小块写回）和一个 TMA descriptor，其瓦片形状是 `st_bf<16,64>`（`kv_block_size=16`, `head_dim=64`）。

**实际加载调用**（[attention_partial.cu:373-382](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_partial.cu#L373-L382)）：先 `tma::expect` 声明字节数与信号量，再 `tma::load_async<dim::DEPTH, EVICT_FIRST>(K_smem, g.k_cache, {inst.layer_idx, cur_blk_idx, inst.kv_head_idx, 0}, K_arrived(...))`。这里传了 4 个下标 `{layer, block, kv_head, 0}`，与 gl 的 4 个维度一一对应（最后一维 0 落在 head_dim 的瓦片起点，因为整个 head_dim=64 恰好是一个瓦片宽）。`g.v_cache` 用法完全对称。

**写入 KV cache**（追加新 token 的 K/V）：在 `rms_matvec_rope_append.cu` 里用 `tma::store_async<EVICT_LAST>(g.k_cache, ...)` 写回（[rms_matvec_rope_append.cu:139](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/rms_matvec_rope_append.cu#L139) 与 [:149](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/rms_matvec_rope_append.cu#L149)）。这说明同一个 `kv_cache_t` 既能被 attention 读取、又能被投影算子追加写入。

#### 4.3.4 代码实践

**目标**：对照「gl 的 4 个维度」与「加载调用里的 4 个下标」，验证它们一一对应。

1. 读 `kv_cache_t` 定义（[llama.cuh:94-95](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cuh#L94-L95)），写下它的 4 维：`[layer, block, kv_head, head_dim]`。
2. 读 attention 的加载调用（[attention_partial.cu:374-377](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_partial.cu#L374-L377)），写下下标：`{inst.layer_idx, cur_blk_idx, inst.kv_head_idx, 0}`。
3. 两相对照，确认每个下标对应一个维度。

**预期结果**：你会看到 attention 是「按层、按序列块、按 KV head」遍历 cache 的，而 `head_dim` 这一维因为正好等于一个瓦片宽（64），下标恒为 0。

> 关于「最后一维下标恒为 0 是否等价于整段 head_dim」这一点，依赖 ThunderKittens 的 gl 索引实现；本检出未含子模块，故「待本地验证」：若你能在装有 ThunderKittens 的环境编译，可在 `load_async` 前打印 `inst.layer_idx/cur_blk_idx/kv_head_idx`，确认它们取值范围与 `[num_layers, seq_len/16, num_kv_heads]` 一致。

#### 4.3.5 小练习与答案

**练习 1**：`kv_cache_t` 为什么有「两个」视图（`sv_bf<16>` 和 `tma::descriptor<...>`），而 `weights_t` 只有一个 `st_bf<16,512>`？

**答案**：权重只用一种访问方式（按 `16×512` tile 加载），所以只挂一个 tile 视图；KV cache 既要按 `16×64` 瓦片用 TMA 加载（attention 读），又要按 16 元素向量段写回（投影算子追加新 token），所以挂了「向量视图 + TMA descriptor」两种访问策略。

**练习 2**：descriptor 里写的是 `st_bf<kv_block_size, head_dim>`，而 attention 里又定义了 `q_st = st_bf<16, LLAMA_1B_HEAD_DIM>`（[attention_partial.cu:18](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_partial.cu#L18)）。这两个 `16×64` tile 是什么关系？

**答案**：它们形状一致（都是 `16×64` 的 bf16 共享内存 tile），所以 cache 的 K/V 瓦片搬进来后可以直接放进同形状的 `q_st`/`k_st`/`v_st` 里参与 attention 计算。`kv_block_size=16` 与 `head_dim=64` 共同决定了 attention 一次处理的「序列块 × 头维」瓦片大小。

---

### 4.4 grid / block / dynamic_shared_memory：内核启动契约

> 本节对应最小模块「grid/block/shm」。

#### 4.4.1 概念说明

CUDA 启动一个内核需要三个量：grid（多少个 block）、block（每个 block 多少线程）、动态共享内存字节数。Megakernels 把这三个量的**提供方式**也收敛进了 `globals_t`，写成三个成员方法：

- `dim3 grid()`：返回 grid 大小。
- `dim3 block()`：返回 block 大小。
- `int dynamic_shared_memory()`：返回动态共享内存字节数。

这是一种**接口契约**：`kittens::py::bind_kernel`（在 ThunderKittens 中）会去调用 globals 的这三个方法，据此完成真正的 `cudaLaunchKernel` 并设置动态共享内存上限。这样，「模型几何」与「启动配置」就被绑在同一个类型上，换模型只需换一个 globals 特化。

#### 4.4.2 核心流程

启动一条 megakernel 的链路：

1. 宿主侧准备好 globals 各字段的显存；
2. `bind_kernel` 取 `g.grid()` 得到 `dim3(sm_count)`——**每个 SM 恰好一个 block**；
3. 取 `g.block()` 得到 `dim3(config::NUM_THREADS)`；
4. 取 `g.dynamic_shared_memory()` 得到动态共享字节数，并用 `cudaFuncSetAttribute` 放开对应上限；
5. 内核 `mk` 以 `__grid_constant__ globals g` 的形式被启动，所有 block 共享同一份只读 globals。

#### 4.4.3 源码精读

**三个方法**（[llama.cuh:144-146](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cuh#L144-L146)）：`grid()` 返回 `dim3(sm_count)`；`block()` 返回 `dim3(config::NUM_THREADS)`；`dynamic_shared_memory()` 返回 `config::DYNAMIC_SHARED_MEMORY`。

**block 大小的来源**：在 `config.cuh` 中，`NUM_CONSUMER_WARPS = 16`，`NUM_WARPS = 4 + NUM_CONSUMER_WARPS = 20`，`NUM_THREADS = NUM_WARPS * WARP_THREADS`（[config.cuh:25-27](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/config.cuh#L25-L27)）。其中 4 个非 consumer warp 分别是 loader/storer/launcher/controller（见 [megakernel.cuh:123-139](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/megakernel.cuh#L123-L139) 的 switch），`WARP_THREADS=32`，所以 `NUM_THREADS = 20 × 32 = 640`。

**动态共享内存的来源**：`STATIC_SHARED_MEMORY` 是指令/时序/信号量等静态声明部分，`DYNAMIC_SHARED_MEMORY = MAX_SHARED_MEMORY - STATIC_SHARED_MEMORY`（[config.cuh:34-39](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/config.cuh#L34-L39)）。动态部分再被切成 `NUM_PAGES` 个 `PAGE_SIZE` 页，且编译期断言 `NUM_PAGES == 13`（[config.cuh:42-44](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/config.cuh#L42-L44)）——这就是 u5-l1 讲过的分页系统的总页数。

**内核如何接收 globals**：`mk` 的签名是 `__global__ void mk(const __grid_constant__ globals g)`，并用 `__launch_bounds__(config::NUM_THREADS, 1)` 与 `__cluster_dims__(config::CLUSTER_BLOCKS)` 装饰（[megakernel.cuh:166-171](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/megakernel.cuh#L166-L171)）。`__grid_constant__` 让整块 globals 以只读常量形式进入内核，正是「单参数传递」能高效的前提。

#### 4.4.4 代码实践

**目标**：手算 `llama_1b_globals` 的启动配置。

1. 由 `config.cuh` 算 `NUM_THREADS`：\(20 \times 32 = 640\)，即 `block() = dim3(640)`。
2. 由 `grid() = dim3(sm_count)`：在 H100 上 `sm_count = 132`（非 Blackwell），在 B200 上为 148（见 4.5 节）。
3. 由 `config.cuh` 的公式，`STATIC_SHARED_MEMORY = 512 + 2 × (4096 + (32+128)×4 + 32×8)`（`INSTRUCTION_PIPELINE_STAGES=2`），据此推 `DYNAMIC_SHARED_MEMORY = MAX - STATIC`，并验证 `NUM_PAGES = DYNAMIC / 16384 == 13`（[config.cuh:44](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/config.cuh#L44)）。

**预期结果**：得到一组具体数字——block=640 线程，grid=132（H100）或 148（B200）个 block，动态共享被切成 13 页。其中 `MAX_SHARED_MEMORY` 的确切字节值来自 ThunderKittens（H100 上通常为 228 KiB），本检出未含子模块，故 STATIC 之外的确切字节数「待本地验证」，但 `NUM_PAGES==13` 的断言已由源码保证。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `grid()` 用 `sm_count` 而不是随便一个大数字？

**答案**：Megakernels 的设计是「每个 SM 跑一个常驻 block」，SM 数量即并行 block 数。多于 SM 数不会带来更多并行度，只会排队；少于 SM 数则浪费 SM。所以 `grid = sm_count` 是让「每 SM 恰好一个 block」的最自然取值。

**练习 2**：如果把这三个方法从 `globals_t` 删掉、改成在 `bind_kernel` 里硬编码，会损失什么？

**答案**：会损失「换模型即换启动配置」的一致性。当前 `block()` 依赖 `config::NUM_THREADS`、`dynamic_shared_memory()` 依赖 `config::DYNAMIC_SHARED_MEMORY`、`grid()` 依赖 `sm_count`，三者都随 globals 特化而变；硬编码会让模型配置与启动配置分家，容易写不一致。

---

### 4.5 llama_1b_globals：把常量填进模板的特化

> 本节对应最小模块「llama_1b_globals 特化」。

#### 4.5.1 概念说明

`globals_t` 只是一个模板。要得到一个真正能编译、能启动的类型，需要给它一组具体的模板参数。`llama_1b_globals` 就是把 LLaMA-1B 的几何尺寸与目标 GPU 的 SM 数填进去后得到的**具体类型**，整个 demo 的所有算子（`rms_qkv_rope_append`、`attention_partial`……）都以它作为默认 `globals`（见 [llama.cuh:160-176](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cuh#L160-L176) 的默认模板参数）。

#### 4.5.2 核心流程

1. 在 `llama.cuh` 顶部用 `#define` 定义一组 `LLAMA_1B_*` 常量与两个 SM 数常量；
2. 用 `typedef globals_t<...>` 把这些常量按位置填入模板；
3. SM 数通过 `#ifdef KITTENS_BLACKWELL` 在 H100（132）与 B200（148）之间二选一；
4. 得到的 `llama_1b_globals` 即被算子模板与 Python 绑定统一使用。

#### 4.5.3 源码精读

**关键常量定义**（[llama.cuh:15-26](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cuh#L15-L26)）：

| 宏 | 值 | 含义 |
| --- | --- | --- |
| `LLAMA_1B_NUM_LAYERS` | 16 | 层数 |
| `LLAMA_1B_HIDDEN_DIM` | 2048 | hidden 维 |
| `LLAMA_1B_INTERMEDIATE_DIM` | 8192 | MLP intermediate 维 |
| `LLAMA_1B_HEAD_DIM` | 64 | 每个 head 的维数 |
| `LLAMA_1B_NUM_ATTENTION_HEADS` | 32 | attention head 数 |
| `LLAMA_1B_NUM_KV_HEADS` | 8 | KV head 数（GQA，32:8 = 4:1） |
| `LLAMA_1B_KV_BLOCK_SIZE` | 16 | KV cache 序列方向瓦片 |
| `LLAMA_1B_MATVEC_BLOCK_SIZE` | 16 | matvec 输出行瓦片 |
| `LLAMA_1B_LM_HEAD_BLOCK_SIZE` | 32 | lm_head 瓦片 |
| `LLAMA_1B_VOCAB_SIZE` | 128256 | 词表大小 |
| `H100_SM_COUNT` | 132 | H100 的 SM 数 |
| `B200_SM_COUNT` | 148 | B200 的 SM 数 |

**特化 typedef**（[llama.cuh:149-158](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cuh#L149-L158)）：把上表常量按顺序填进 `globals_t<...>`，其中最后一个参数 `sm_count` 用 `#ifndef KITTENS_BLACKWELL` 在 `H100_SM_COUNT(132)` 与 `B200_SM_COUNT(148)` 之间选择。这就同时决定了 `grid()` 的大小。

**它如何被使用**：`llama.cu` 里所有算子都写 `xxx_op = xxx<default_config, llama_1b_globals>`（[llama.cu:15-24](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cu#L15-L24)），`bind_kernel` 也以 `mk<default_config, llama_1b_globals, ...>` 启动（[llama.cu:28-31](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cu#L28-L31)）。即整个推理的「模型形状」与「目标硬件」都由这一个 typedef 锁定。

#### 4.5.4 代码实践

**目标**：列出 `llama_1b` 的关键常量，并理解 GQA 比例。

1. 读 [llama.cuh:15-26](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cuh#L15-L26)，填出下表。

| 量 | 值 |
| --- | --- |
| 层数 | 16 |
| hidden 维 | 2048 |
| intermediate 维 | 8192 |
| head_dim | 64 |
| attention heads | 32 |
| KV heads | 8 |
| vocab | 128256 |
| SM 数（H100 / B200） | 132 / 148 |

2. 计算 GQA 分组：\(32 / 8 = 4\)，即每 4 个 attention head 共享一组 KV。
3. 确认 hidden 维与 head 的关系：\(32 \times 64 = 2048\)，与 `hidden_dim` 一致。

**预期结果**：你能脱口说出 LLaMA-1B 在本 demo 里的几何——16 层、2048/8192、32 个 attention head（head_dim 64）、8 个 KV head、词表 128256，并知道 SM 数随编译目标在 132/148 间切换。

#### 4.5.5 小练习与答案

**练习 1**：为什么 SM 数要用 `#ifdef KITTENS_BLACKWELL` 在编译期选择，而不是运行期读取？

**答案**：因为 `sm_count` 是 `globals_t` 的模板参数（进而 `constexpr static`），它参与决定 `grid()` 以及 `attn_lse_intermediates_t` 里 `((sm_count+15)/16)*16` 的形状（[llama.cuh:100-101](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cuh#L100-L101)）。这些都必须编译期确定，所以 SM 数也得编译期选定。一份二进制针对一种架构。

**练习 2**：如果要支持一个 hidden=4096、layers=32 的新模型，需要改哪里？

**答案**：不必动 `globals_t` 模板本身。新增一组宏（如 `LLAMA_7B_*`），再 `typedef globals_t<...> llama_7b_globals;` 即可得到新类型；只要它的字段布局与算子期望一致，就能复用所有算子模板。这正是把 `globals_t` 做成模板的价值。

## 5. 综合实践

把本讲的三块知识串起来，完成下面这个「字段 → 形状 → 启动」的端到端追踪任务：

1. **字段清单**：打开 [`llama.cu` 的 `bind_kernel`](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cu#L28-L52)，抄下全部被绑定的 `llama_1b_globals::XXX` 字段。
2. **归类与形状**：对每个字段，在 [`llama.cuh`](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cuh) 里找到它的类型别名（`weights_t` / `norm_weights_t` / `kv_cache_t` / `activations_t` …），写出它的 gl 形状与视图。
3. **tile 体积核验**：对 `weights_t` 算出 `st_bf<16,512>` 的字节数，确认等于 `PAGE_SIZE`；对 `kv_cache_t` 算出 descriptor 瓦片 `st_bf<16,64>` 的字节数（\(16\times64\times2 = 2048\) B）。
4. **启动配置**：写出 `llama_1b_globals` 的 `grid()/block()/dynamic_shared_memory()` 三个返回值（132 或 148 个 block、640 线程、`MAX-STATIC` 字节且切成 13 页）。
5. **画一张图**：用方框画出 `globals_t`，里面分四区——VM 控制区（`Bar/instructions/timings`）、权重区、KV cache 区、激活/标量区，并在权重区旁标注「每个 `st_bf<16,512>` tile = 1 页 = 16384 B」。

**预期结果**：一张「globals 结构图 + tile/页对照 + 启动配置」的完整笔记，它能作为你阅读后续每个 `*.cu` 算子（它们都从 `g` 取数据）时的总索引。

## 6. 本讲小结

- `globals_t` 是一个 9 参数模板结构体，把一次 LLaMA 推理所需的**全部状态**（权重、激活、KV cache、VM 控制表、标量）打包成单个内核参数，以 `__grid_constant__` 形式进入 `mk`。
- 每个张量字段都是 `kittens::gl<元素类型, 多维, 视图...>`：正数维度编译期固定、`-1` 维度运行期决定；末尾视图（`st_bf`/`sv_bf`/`tma::descriptor`）声明访问粒度。
- `weights_t` 用 `st_bf<16,512>` 作为子块，是因为它同时满足「行=matvec 块（16）」「4 块拼出 hidden 维（2048）」「单 tile 恰好一页（16384 B）」三个约束。
- `kv_cache_t` 内嵌 `tma::descriptor<st_bf<16,64>, 1>`，把一块 4 维 `[层, 序列块, kv_head, head_dim]` 显存以 `16×64` 瓦片交给 TMA 硬件异步搬运，attention 按四维下标读取、投影算子按向量段追加写入。
- `grid()/block()/dynamic_shared_memory()` 三个方法是 launcher 的接口契约：grid=`sm_count`（每 SM 一 block）、block=`NUM_THREADS`（=640）、动态共享=`MAX-STATIC`（切成 13 页）。
- `llama_1b_globals` 把 `LLAMA_1B_*` 常量与架构相关的 SM 数（H100=132 / B200=148）填进模板，得到具体类型，被所有算子与 Python 绑定复用——换模型只需新增一个 typedef。

## 7. 下一步学习建议

- **读 VM 控制区**：`Bar`、`instructions`、`timings` 三块在内核里如何被 controller 驱动，参见 u9-l3 与 [`include/controller/controller.cuh`](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh)。
- **读分页系统**：动态共享内存如何被切成 13 页并由 loader/consumer 共享，参见 u5-l1 与 [`include/controller/page_allocator.cuh`](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/page_allocator.cuh)，并结合本讲的「一 tile = 一页」结论。
- **逐个算子精读**：以本讲的「字段清单」为索引，按 `rms_matvec_rope_append.cu` → `attention_partial.cu` → `attention_reduction.cu` → `matvec_adds.cu` → `upgate.cu` → `rms_lm_head.cu` 的顺序，看每个算子如何从 `g` 取权重与激活、如何用 TMA 加载/写回。
- **动手特化**：尝试仿照 `llama_1b_globals`，为一组假想参数（如 `NUM_LAYERS=8, HIDDEN_DIM=1024`）写一个 typedef，并思考哪些 `gl` 形状、哪些 tile/页关系会随之改变（注意 `st_bf<16,512>` 对 hidden_dim=2048 的依赖，换 hidden 时 512 这个数是否仍成立值得思考）。
