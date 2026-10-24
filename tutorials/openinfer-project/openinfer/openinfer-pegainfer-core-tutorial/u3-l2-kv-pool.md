# kv_pool.rs：page-first KV 布局、两段式准入与生命周期

## 1. 本讲目标

本讲是整本手册最核心的一讲。读完本讲，你应该能够：

1. **手算偏移**：给定 `(page, layer, token)` 坐标和模型几何，手算 page-first 布局下的元素偏移，并解释「为什么一页里要连续放下所有层」。
2. **解释两段式准入**：说清 `try_reserve`（reserve 半段）→ `commit_reservation`（commit 半段）这套协议解决了什么问题——尤其是多池场景下「占了 A 池却等不到 B 池」的部分资源占用。
3. **梳理完整生命周期**：跟踪一个请求从 `try_reserve` → `commit` → `ensure_capacity` → `advance` → `drop` 的全部状态迁移。
4. **说清 padding 页**：解释 `padding_page_id()` 对应的预留页为什么必须在池构造期就留出来，以及它与 CUDA Graph 的关系。

前置讲义 u3-l1 回答了「宿主侧由谁记录页的所有权」（`PagePool` + RAII 许可）。本讲在其上回答三个新问题：**页里装什么（布局）、几何怎么算（stride）、一个请求如何拿页和还页（生命周期）**。

## 2. 前置知识

本讲用到的前置概念，均已在前面讲义建立，这里快速回顾：

- **KV cache**（u1-l1）：注意力机制中，每个 token 在每一层产生的 Key/Value 向量需要缓存起来供后续 token 注意力计算复用。推理的 decode 阶段每生成一个 token，就要把它的 K/V 追加进缓存。
- **分页 KV（paged KV）**（u2-l4）：把 KV 缓存切成固定大小的「页」（page），一个请求持有一串页号（页表），而不是一整块连续显存。好处是显存碎片可控、请求间可以共享管理。
- **PagePool 三件套**（u3-l1）：`PagePool` 是锁保护 free-list 的固定页分配器；`PageId(usize)` 是不透明页号；`OwnedPagePermit` 是 RAII 租约，`Drop` 是唯一归还入口，另有 `absorb`（合并租约）与 `try_grow`（原地扩张）。
- **tensor 门面与 DeviceContext**（u2-l1）：`tensor.rs` 只有一行 `pub use pegainfer_kernels::tensor::*`；`ctx.stream.alloc_zeros(n)` 在流上分配 `CudaSlice<bf16>`，`CudaSlice` 持有设备侧 RAII 所有权。
- **Rust 小工具**：`div_ceil` 是向上取整除法（\( \lceil a/b \rceil \)）；`Arc` 让多个句柄共享同一份内部状态；`anyhow::bail!` 立即返回错误。
- **u2-l4 的契约**：core 用 `KvDesc`（页表 + `last_page_len`）描述请求状态，kernels 只消费扁平页表——「计划与数据分离」。

## 3. 本讲源码地图

| 文件 | 角色 |
|------|------|
| `pegainfer-core/src/kv_pool.rs`（563 行） | 本讲主角：`KvLayout` / `KvPool` / `KvReservation` / `KvState` / `KvDesc` 五个类型与内嵌测试 |
| `pegainfer-core/src/page_pool.rs`（195 行） | u3-l1 已精读；本讲引用其 `absorb` / `try_grow` / `Drop` 作为机制地基 |
| `pegainfer-core/src/tensor.rs`（1 行） | 门面；本讲只用到 `DeviceContext` 的流上分配 |
| `pegainfer-kernels/src/paged_kv.rs` | stride 推导的唯一事实源 `derive_strides`，以及 `KvFormat` / `KvStorage` / `PagedKvLayout` 的定义（core 只再出口与调用） |
| `pegainfer-gemma4/src/kv.rs` | 两段式准入的真实多池消费者（本讲作为例证引用） |
| `pegainfer-qwen3/src/batch_decode_buffers.rs` | `padding_page_id` 的真实消费者 |

## 4. 核心概念与源码讲解

### 4.1 KvLayout：page-first 几何与唯一的 stride 推导

#### 4.1.1 概念说明

`KvLayout` 描述「一页长什么样」：纯值类型（`Copy`），不碰 GPU、不做分配，被 `KvPool`、`KvState`、`KvDesc` 三方共享。它回答的问题是：给定 `page_size`（每页 token 数）、`num_layers`、`num_kv_heads`、`head_dim`，一页里 K/V 数据怎么排。

**page-first（页优先）**是关键设计：一页不是「某一层的一段缓存」，而是「`page_size` 个 token 在**所有层**上的 K 和 V 的连续块」。源码文档注释画出了布局：

```text
page_i: [L0_K | L0_V | L1_K | L1_V | ... | Ln_K | Ln_V]
         └─ page_size × num_kv_heads × head_dim each ─┘
```

为什么页内要跨层连续？直觉是：**页是以 token 为单位分配的，而 token 在每一层都有 K/V**。如果把布局反过来（layer-first，先放满第 0 层所有页、再放第 1 层……），那么每分配一页只覆盖一层，同一批 token 在 36 层上就要 36 次独立的页管理，页表也得每层一份。page-first 让「一个 `PageId` 同时服务所有层」——kernel 拿层号乘 `layer_stride` 即可定位，请求的页表与层无关。这正是 `KvState::page_indices_i32()` 只有一份页表的原因。

#### 4.1.2 核心流程

三个 stride 的推导公式只有一个来源——kernels 的 `derive_strides`（core 侧不自己算）：

```text
输入: page_size, num_kv_heads, head_dim, num_layers, format
  ↓
layer_stride = page_size × num_kv_heads × values_per_token(head_dim)
kv_block_len = layer_stride ÷ blocks_per_layer
page_stride  = num_layers × layer_stride
  ↓ 任一步 checked_mul 溢出 或 format 不适配 head_dim → None → anyhow 报错
KvLayout { 全部字段 } （Copy 值类型）
```

对默认的 `KvFormat::Split`（K、V 各占 `head_dim` 宽、每层两个块）：`values_per_token = 2 × head_dim`、`blocks_per_layer = 2`，因此：

\[ \text{kv\_block\_len} = \text{page\_size} \cdot H \cdot D,\quad \text{layer\_stride} = 2 \cdot \text{kv\_block\_len},\quad \text{page\_stride} = L \cdot \text{layer\_stride} \]

其中 \( H \) 为 KV 头数、\( D \) 为头维、\( L \) 为层数。

于是任意坐标的 K 元素偏移为：

\[ \text{offset}(p,\ell,t,h,d) = p \cdot \text{page\_stride} + \ell \cdot \text{layer\_stride} + (t \cdot H + h) \cdot D + d \]

同层 V 块起点 = 同层 K 块起点 + `kv_block_len`。前两项（页、层寻址）由 core 侧文档与测试直接证实；括号内的「行视图」（每行 `head_dim` 宽、按 token×head 编址）来自 kernels 的 `RowGeometry` 注释，行内更细的编序以 kernels 实现为准（不影响本讲 stride 级的理解）。

#### 4.1.3 源码精读

- [src/kv_pool.rs:L15-L33](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/kv_pool.rs#L15-L33) — `KvLayout` 结构体。文档注释明确「Page-first geometry」「Pure value type — no GPU, no allocation. Shared by `KvPool`, `KvState`, `KvDesc`」。九个字段中四个是输入维度（`page_size`/`num_layers`/`num_kv_heads`/`head_dim`），三个是推导 stride，两个是枚举开关（`storage`/`format`）。
- [src/kv_pool.rs:L68-L100](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/kv_pool.rs#L68-L100) — `with_storage_and_format` 是唯一真正干活的构造器：第 76–82 行调用 `pegainfer_kernels::paged_kv::derive_strides(...)`，返回 `None` 时（stride 溢出 usize 或该 format 装不下这个 `head_dim`）转成带完整参数的 `anyhow` 错误。
- [src/kv_pool.rs:L36-L66](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/kv_pool.rs#L36-L66) — `new` 与 `with_storage` 只是逐层补默认值的便捷入口：`new` 固定 `KvStorage::Bf16`，`with_storage` 再固定 `KvFormat::Split`。
- [src/kv_pool.rs:L102-L114](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/kv_pool.rs#L102-L114) — `kernel_layout()` 把 `KvLayout` 逐字段翻译成 kernels 的 `PagedKvLayout`。这是 u2-l4 已经见过的模式：core 的类型在边界处被译成 kernel 侧类型，两边字段一一对应。
- [pegainfer-kernels/src/paged_kv.rs:L111-L133](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/src/paged_kv.rs#L111-L133) — stride 公式的唯一事实源。文档注释说得很清楚：「The one derivation of a page's strides... Both the kernel-facing layout and the runtime's own copy call this, so the arithmetic exists once」——kernel 侧和运行时侧共用同一套算术，保证两边对布局的理解永远一致。
- [src/kv_pool.rs:L7-L8](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/kv_pool.rs#L7-L8) — `KvFormat` 与 `KvStorage` 直接从 kernels 再出口，core 不自定义副本。

关于两个枚举（定义在 kernels 侧）：

- `KvStorage` 是**元素宽度**轴：`Bf16` 每元素 2 字节，`E4m3`（fp8）每元素 1 字节（[pegainfer-kernels/src/paged_kv.rs:L1-L14](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/src/paged_kv.rs#L1-L14)）。
- `KvFormat` 是**形状**轴：`Split` 是常规「K 一块、V 一块」；`Folded { rotary }` 服务「K 和 V 出自同一个投影」的模型族（\(K = \text{RoPE}(V \cdot w)\)，把 K 的范数折进 query，只存被旋转触碰的列），每 token 存 `head_dim + rotary` 个值（[pegainfer-kernels/src/paged_kv.rs:L16-L53](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/src/paged_kv.rs#L16-L53)）。

#### 4.1.4 代码实践

**实践目标**：用手算验证 stride 公式，并与源码测试对拍。

**操作步骤**：

1. 取 Qwen3-4B 的几何（见源码测试注释）：\(L=36,\ H=4,\ D=128,\ \text{page\_size}=16\)，手算三个 stride 和每页字节数。
2. 再取 Qwen3.5-4B 的几何：\(L=8,\ H=4,\ D=256,\ \text{page\_size}=16\)，同样手算。
3. 打开 [src/kv_pool.rs:L473-L493](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/kv_pool.rs#L473-L493) 的两个 stride 测试，核对答案。
4. 用 Qwen3.5 几何手算坐标 \((p,\ell,t,h)=(3,5,7,2)\) 的 K 块起点偏移。

**需要观察的现象 / 预期结果**：

- Qwen3-4B：`kv_block_len = 16×4×128 = 8192`，`layer_stride = 16384`，`page_stride = 36×16384 = 589824`，每页 589824×2 = 1179648 字节 ≈ 1.125 MB——与测试断言 `assert_eq!(l.page_stride, 589_824)` 和注释「~1.125 MB per page」一致。
- Qwen3.5-4B：`kv_block_len = 16384`、`layer_stride = 32768`、`page_stride = 262144`、每页 512 KB——与测试断言逐项一致。
- 偏移：\(3 \times 262144 + 5 \times 32768 + (7\times4+2)\times256 = 786432 + 163840 + 7680 = 957952\)，落在第 3 页区间 \([786432,\ 1048576)\) 内。

这两个测试是**纯 CPU 测试**（只构造 `KvLayout`，不建 `DeviceContext`），可在任何机器上运行：`cargo test --release -p pegainfer-core --lib stride_geometry`。

#### 4.1.5 小练习与答案

**练习 1**：`KvLayout` 为什么是 `Copy`？如果给它加一个 `CudaSlice` 字段会破坏什么？
**答案**：`Copy` 意味着布局信息可以零成本到处传（`KvPool`、`KvState`、计划构建都要读它）。`CudaSlice` 持有设备内存的 RAII 所有权、不可 `Copy`；加进来 `KvLayout` 就从「纯几何描述」变成了「资源所有者」，没法随意复制，也就失去了作为共享值类型的意义。

**练习 2**：`kv_block_len` 的文档说它是「one K (or V) block」的大小。为什么 K 和 V 的块大小天然相等？
**答案**：K 和 V 都是每个 token 每 KV 头 `head_dim` 个元素，同一页的 token 数和头数相同，所以两块尺寸必然一样，一个 `kv_block_len` 同时描述两者；`layer_stride = 2 × kv_block_len` 正是 K 块 + V 块。

**练习 3**：把布局从 page-first 改成 layer-first（每页只装一层），`KvState` 的哪个 API 会最先失去意义？
**答案**：`page_indices_i32()` 返回的单一页表。layer-first 下同一批 token 在不同层落在不同页里，一份页表无法服务所有层，页表必须变成按层的二维结构，u2-l4 建立的「扁平 i32 页表」契约也随之失效。

### 4.2 KvPool：单块 buffer、三档构造与 padding 页

#### 4.2.1 概念说明

`KvPool` 是共享的 KV 后备存储：**一整块** `CudaSlice<bf16>` 按 page-first 布局切页，配一个 `PagePool` 管页的租借。它用 `Arc` 包裹内部状态，克隆廉价——所有 `KvState`（每个请求一个）都是同一块 buffer 的视图，靠各自的页表指向自己的页。

三档构造器与 `KvLayout` 的三档一一对应：`new`（默认 Bf16）、`with_storage`（可换 E4m3，仍默认 Split 布局）、`with_storage_and_format`（布局也可换）。这 answering 了本讲实践任务的第三问（详见 4.2.4）。

`padding_page_id()` 暴露的是构造期就永久扣下的一个专用页：batch decode 的 CUDA Graph 需要用固定形状重放，batch 里的空槽（padding 槽）也得填一个合法页号，且这个页号在图捕获和每次重放之间必须不变——所以它在池构造时就被取走、永不释放。

#### 4.2.2 核心流程

`with_storage_and_format` 的完整构造流程（另两档只是它的默认值包装）：

```text
1. KvLayout::with_storage_and_format(...)   → 几何 + stride 级溢出检查
2. total_elements = num_pages × page_stride   （checked_mul，元素域）
3. total_bytes    = total_elements × storage.elem_bytes()
                                                 （checked_mul，字节域）
   # 注释原文：分配器对元素大小是不检查地相乘的，
   # 所以先在字节域把好关再交给它
4. backing_slots  = total_bytes.div_ceil(size_of::<bf16>())
   # buffer 恒为 CudaSlice<bf16>；fp8 时每 slot 装两个元素
5. ctx.stream.alloc_zeros(backing_slots)      → 单块 GPU buffer
6. PagePool::new(num_pages)
7. pool.try_acquire_many(1) → padding_permit  （永久扣下 0 号页）
```

之后所有分配都从这块 buffer 上按页号切片：`page_indices_i32()` 给模型 crate 拼页表，`buffer()` 给 executor 直接持有底层指针（CUDA Graph 指针稳定性的前提）。

#### 4.2.3 源码精读

- [src/kv_pool.rs:L117-L125](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/kv_pool.rs#L117-L125) — `KvPoolInner` 四件套：`pool`（页分配器）、`buffer`（单块 `CudaSlice<bf16>`）、`layout`、`padding_permit`。padding 页的文档注释是理解它的钥匙：「Reserved page for bucket CUDA Graph padding. FlashInfer metadata for padding slots points here with seq_len=1. KV append writes harmless garbage; attention output is discarded.」——padding 槽指向这页、长度按 1 算，写入的是无害垃圾，注意力输出被丢弃。
- [src/kv_pool.rs:L127-L141](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/kv_pool.rs#L127-L141) — `KvPool` 的文档注释给出 page-first 布局图，并写明「Cheaply cloneable (Arc). Multiple `KvState`s share the same pool.」
- [src/kv_pool.rs:L185-L239](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/kv_pool.rs#L185-L239) — 构造主体。L204–L209 检查 `num_pages × page_stride` 元素域溢出；L210–L218 的注释解释了为何要抢在分配器之前在字节域再查一次；L219 用 `div_ceil` 把字节数折算成 bf16 slot 数。
- [src/kv_pool.rs:L221-L229](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/kv_pool.rs#L221-L229) — 流上分配 buffer；随后立刻 `try_acquire_many(1)` 扣下 padding 页，`expect("pool must have at least 1 page for padding")`——池连一页都给不出 padding 是构造期就不可原谅的错误。由于 u3-l1 已知 `PagePool::new` 的 free-list 是逆序入栈（[src/page_pool.rs:L35-L43](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/page_pool.rs#L35-L43)），最先 pop 出来的就是 0 号页，所以 padding 页恒为 0 号。
- [src/kv_pool.rs:L269-L272](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/kv_pool.rs#L269-L272) — `padding_page_id()` 把这个永久页号以 `i32` 暴露出去。真实消费者：qwen3 的 batch decode 在补齐 batch 时 `all_page_indices.push(self.padding_page_id)`（[pegainfer-qwen3/src/batch_decode_buffers.rs:L358](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-qwen3/src/batch_decode_buffers.rs#L358)），gemma4 的两个池各自有 padding 行（[pegainfer-gemma4/src/serve.rs:L2207-L2210](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-gemma4/src/serve.rs#L2207-L2210)）。
- [src/kv_pool.rs:L254-L259](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/kv_pool.rs#L254-L259) — `buffer()` 的文档注释点出设计意图：「Every state in this pool is a view into this one buffer, so an executor can hold the pool and take page metadata from a plan rather than from a request's state.」executor 只要拿住池和页表，不必追着请求状态要数据。
- [src/kv_pool.rs:L241-L248](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/kv_pool.rs#L241-L248) — `alloc()` 创建空的 per-request `KvState`：先 `try_acquire_many(0)` 拿一个零页许可（u3-l1 已证零页申请永不失败）。

#### 4.2.4 代码实践

**实践目标**：看懂 fp8 池的字节折算，并回答「三档构造器分别服务什么场景」。

**操作步骤**：

1. 阅读 [src/kv_pool.rs:L458-L471](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/kv_pool.rs#L458-L471) 的 `storage_width_sets_backing_capacity` 测试：同一几何分别用 `KvStorage::E4m3` 和 `Bf16` 建池，断言 fp8 池的 `buffer().len()` 是 `(pages × page_stride).div_ceil(2)`，而 bf16 池恰等于 `pages × page_stride`。
2. 对照 L219 的 `backing_slots = total_bytes.div_ceil(size_of::<bf16>())`，解释为什么 buffer 类型永远是 `CudaSlice<bf16>` 而总量却能减半。
3. 写一页笔记回答：`new` / `with_storage` / `with_storage_and_format` 各服务什么场景。

**需要观察的现象 / 预期结果**：

- fp8 每元素 1 字节、bf16 每元素 2 字节，所以同样元素数的 fp8 池字节数减半；buffer 以 bf16（2 字节）为 slot 单位分配，slot 数就是 `div_ceil(2)`，kernel 侧按 `KvStorage` 的元素宽度去解释同一段内存。该测试需要 GPU（要建 `DeviceContext`），运行方式：`cargo test --release -p pegainfer-core --lib storage_width`；无 GPU 环境则结论以测试断言为准，待本地验证。
- 三档分工：`new` = Bf16 + Split，覆盖 qwen3 这类标准 full-attention 模型；`with_storage` = 换元素宽度（如 gemma4 的 fp8 本地 KV，对应 CLAUDE.md 的 `PEGAINFER_KV_FP8=local` 即 e4m3、scale 1.0），布局不变；`with_storage_and_format` = 再换布局（`Folded { rotary }` 服务「K 折叠进 query」的模型族——CLAUDE.md 里 gemma4 的 `PEGAINFER_GLOBAL_ATTN=tilelang640` 折叠池「folds K's norm weight into the query」正是同一思路的落地，具体数值对应关系属模型 crate，超出本讲范围）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `total_bytes` 的溢出检查不能省，交给分配器不行吗？
**答案**：L210–L212 的注释原文说明：分配器对元素大小是「unchecked」相乘的。若 `total_elements × elem_bytes` 静默回绕，分配出的 buffer 会比几何要求小，后续按 `page_stride` 切页就会越界写。core 抢在分配前用 `checked_mul` 在字节域把关，失败转成带数字的 `anyhow` 错误而不是运行期内存越界。

**练习 2**：padding 页如果不在构造期预留、而是运行期第一次需要时再申请，会发生什么？
**答案**：CUDA Graph 捕获时 padding 槽的页号被写进了图内的页表元数据；运行期再申请意味着页号可能每次不同（别的请求可能先拿走那一页），图重放时 padding 槽指向的页就可能是某个活跃请求的数据页——KV append 会把垃圾写进别人的 KV，注意力读错数据。构造期永久扣下（permit 存在 `KvPoolInner` 里随池存活）保证这个页号从出生到销毁恒定且永远不属于任何请求。

**练习 3**：`KvPool` 克隆两次后 `capacity_pages()` 各返回多少？页会变多吗？
**答案**：返回值相同，页数不变。`KvPool` 是 `Arc<KvPoolInner>` 的克隆，两个句柄指向同一个 free-list 和同一块 buffer；克隆只增加引用计数，是「多一个观察者」而不是「多一个池」。

### 4.3 KvReservation：两段式准入的 reserve 半段

#### 4.3.1 概念说明

`KvReservation` 是从 `KvPool` 借来的**一袋页**，还没有绑定到任何请求：它包着一个 `OwnedPagePermit`，drop 时自动还页，除非被 `KvState::commit_reservation` 吸收。

为什么需要这个中间态？看 `try_reserve` 的文档注释（L274–L277，原文）：

> The schedule half of an atomic multi-pool admission: reserve from every pool first, commit only when all reservations hold, and let a failure roll the rest back by drop. Growing a request's own permit instead cannot be undone.

多池场景（如 gemma4 同时有 local 滑窗池和 global 全注意力池）下一个请求要同时从两个池拿页。如果直接往请求自己的 permit 里 `try_grow`：先从 A 池长成功了、B 池却不够——A 池的页已经进了请求的 permit，回滚只能靠手动拆，任何遗漏都是泄漏。两段式把「拿页」变成可以整体丢弃的中间物：**reserve 半段**每个池都先试，任一失败就把已拿的全部 drop（RAII 自动归还）；**commit 半段**在所有预约都成立后才把页吸收进请求状态。失败路径的回滚成本是零行代码。

#### 4.3.2 核心流程

```text
调度器（schedule 半段）                     请求状态（apply 半段）
─────────────────────────                  ─────────────────────────
for pool in 所有涉及的池:                   若所有预约都成立:
  rᵢ = pool.try_reserve(nᵢ)                  kv.commit_reservation(rᵢ)
  失败 → 已拿的 rⱼ 随作用域结束被 drop        （permit.absorb，页并入请求）
       → 页全部自动回到各自 free-list       否则: drop 残余预约 → 自动回滚
```

真实多池消费者（gemma4）的用法还展示了一个细节：local 池**逐页** reserve（因为滑窗状态每步释放最前面的页，页不能共享一张 permit），global 池**一次性** reserve，两者都成才 commit。

#### 4.3.3 源码精读

- [src/kv_pool.rs:L274-L283](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/kv_pool.rs#L274-L283) — `try_reserve`：一行委托 `PagePool::try_acquire_many`，成功才包成 `KvReservation`。注意它返回 `Option` 而非 `Result`——「页不够」不是错误而是调度器要处理的常态（排队等下个调度步）。
- [src/kv_pool.rs:L286-L299](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/kv_pool.rs#L286-L299) — `KvReservation` 结构与 `extend_page_indices_i32`。文档注释解释了为何往「调用方拥有的行」追加：「A sliding state rebuilds that row every step, so the pages land in one buffer instead of one `Vec` per reservation.」——滑窗状态每步重建页表行，预约的页直接落进调用方的缓冲，避免每张预约一个 `Vec`。
- [src/kv_pool.rs:L380-L384](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/kv_pool.rs#L380-L384) — `commit_reservation`：一行 `permit.absorb(reservation.permit)`。文档写明「The apply half of an atomic multi-pool admission. The reservation must come from this state's pool, which the permit merge enforces.」
- [src/page_pool.rs:L89-L97](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/page_pool.rs#L89-L97) — u3-l1 读过的 `absorb`：`assert!(Arc::ptr_eq(...), "absorb requires permits from the same pool")`——跨池合并会还错 free-list，直接断言拦下，这就是上一条注释里「permit merge enforces」的落点。
- [src/page_pool.rs:L120-L130](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/page_pool.rs#L120-L130) — `OwnedPagePermit::Drop`：失败回滚的引擎。预约被 drop 时页逆序回到 free-list，零手工代码。
- [pegainfer-gemma4/src/kv.rs:L381-L401](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-gemma4/src/kv.rs#L381-L401) — 真实多池准入：local 逐页 `try_reserve(1)` 收集（注释解释滑窗前部逐页释放所以不能共享 permit），local 全成才去 global `try_reserve(global_need)`；`(true, Some(global_r))` 分支里 `kv.global.commit_reservation(global_r)`，其余分支 drop 残余自动回滚。

#### 4.3.4 代码实践

**实践目标**：亲手验证「池耗尽时 `try_reserve` 返回 `None`」这一准入语义。

**操作步骤**（示例代码，非项目原有；写进 `kv_pool.rs` 的 `mod tests` 中即可）：

```rust
#[test]
fn kv_pool_try_reserve_returns_none_when_exhausted() {
    // 3 页总量：1 页 padding + 2 页可用
    let pool = test_pool(16, 3);
    assert_eq!(pool.available_pages(), 2);

    let r = pool.try_reserve(2).expect("恰好拿走剩余全部页");
    // 池已空：再要一页只能是 None（排队信号，而非错误）
    assert!(pool.try_reserve(1).is_none());

    drop(r); // RAII 归还
    // 全有或全无：可用 2 页 < 需求 3 页，仍为 None
    assert!(pool.try_reserve(3).is_none());
    assert_eq!(pool.available_pages(), 2);
}
```

运行：`cargo test --release -p pegainfer-core --lib kv_pool_try_reserve -- --nocapture`。

**需要观察的现象 / 预期结果**：三条断言全部通过——耗尽后单页申请为 `None`、drop 后页恢复、超量申请（部分占用不可接受）也为 `None`。注意 `test_pool` 要建 `DeviceContext`（测试助手注释写着 "GPU required for kv_pool tests"），本测试需要 GPU；无 GPU 环境时标记**待本地验证**，可先在纯 CPU 层用 u3-l1 的 `PagePool` 直接验证同语义（`reports_insufficient_capacity_with_none` 已覆盖）。

#### 4.3.5 小练习与答案

**练习 1**：`try_reserve` 为什么返回 `Option<KvReservation>` 而 `ensure_capacity` 返回 `Result<()>`？
**答案**：`try_reserve` 面向调度器，「页暂时不够」是预期内的常态，`None` 就是「这次别放行」的排队信号；`ensure_capacity` 面向请求已放行后的执行路径，页不够意味着调度承诺被打破，是带上下文的错误（`bail!("KvState: out of pages ...")`）。

**练习 2**：如果不引入 `KvReservation`、直接用 `try_grow` 实现多池准入，写出一条会出问题的执行序列。
**答案**：请求需要 A 池 1 页 + B 池 1 页。先 `kv_a.try_grow(1)` 成功（页进入 `kv_a` 的 permit），再 `kv_b.try_grow(1)` 失败。此时 A 池那页已挂在请求状态上，要回滚只能手动从 permit 里拆出最后一页还回去——任何 early-return、panic 或漏拆都变成永久泄漏。两段式下同样的序列只是 drop 掉 A 池的预约，回滚零代码。

**练习 3**：`commit_reservation` 之后预约对象去哪了？
**答案**：被 `absorb` 消费——`OwnedPagePermit` 按值传入，其 `pages` 被 `append` 进请求自己的 permit 后原对象立即失效 drop（此时 pages 已空，drop 是空操作）。页的所有权无缝转移到请求状态，仍由同一套 RAII 保证最终归还。

### 4.4 KvState：一个请求的 KV 生命周期

#### 4.4.1 概念说明

`KvState` 是 per-request 的 KV 状态：一个 `OwnedPagePermit`（这个请求租的页）+ `seq_len`（已确认写入的 token 数）+ 池的克隆。文档注释点明它「Parallels `RecurrentState` for linear attention」——线性注意力模型（如 Qwen3.5 的 Gated DeltaNet）用递归状态，full attention 用分页 KV 状态，两者在调度器眼里是平行的抽象。

生命周期里有两个容易混淆的动作，本讲目标里的完整状态迁移把它们串起来：

- **扩容**（`ensure_capacity` / `commit_reservation`）：页数增加，`seq_len` 不变——为「将要写入」准备空间。
- **推进**（`advance`）：`seq_len` 增加，页数不变——「已经写入」的确认。

两者分离是因为写入由 GPU kernel 异步完成：宿主侧先把页备好，kernel 追加 K/V 之后宿主才 `advance`。`advance` 甚至不检查容量（`ensure_capacity(seq_len + 1)` 必须先于每次 `advance(1)`），职责单一到不会撒谎。

#### 4.4.2 核心流程

一个请求从生到死的页数/长度迁移（以 page_size=16、池有 4 可用页为例）：

```text
pool.alloc()        pages=0   seq_len=0     （零页许可，永不失败）
  │ 调度放行: try_reserve(n) ─→ commit_reservation(r)   或 ensure_capacity(10)
  ▼
                    pages=1   seq_len=0     （prefill 10 token 前备好 1 页）
advance(10)         pages=1   seq_len=10    （prefill 写入完成）
  │ decode 循环 × 6: ensure_capacity(seq_len+1); advance(1)
  ▼
                    pages=1   seq_len=16    （页满，last_page_len=16）
ensure_capacity(17) pages=2   seq_len=16    （跨页边界，长出第 2 页）
advance(1)          pages=2   seq_len=17    （last_page_len=1）
  │
  ▼ drop(KvState)
                    pages=0                  （permit.Drop 逆序归还全部页）
```

页数换算的两个纯函数：

\[ \text{pages\_needed}(n) = \lceil n / \text{page\_size} \rceil \]

\[ \text{last\_page\_len}(n) = \begin{cases} 0 & n = 0 \\ \text{page\_size} & n \bmod \text{page\_size} = 0,\ n > 0 \\ n \bmod \text{page\_size} & \text{否则} \end{cases} \]

注意整除时 `last_page_len` 是 `page_size`（满页）而不是 0——最后一页装满了也是一种「最后一页长度」。

#### 4.4.3 源码精读

- [src/kv_pool.rs:L301-L313](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/kv_pool.rs#L301-L313) — `KvState` 三字段 + `pages_needed`（一行 `div_ceil`）。
- [src/kv_pool.rs:L315-L322](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/kv_pool.rs#L315-L322) — `last_page_len_for`，公式与上文一致。
- [src/kv_pool.rs:L359-L374](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/kv_pool.rs#L359-L374) — `ensure_capacity`：`needed <= held` 直接返回；否则 `try_grow(grow)`，失败时 `bail!("KvState: out of pages (need {grow} more, {} available)")`——错误信息里带缺口和余量，可诊断。
- [src/kv_pool.rs:L380-L388](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/kv_pool.rs#L380-L388) — `commit_reservation`（4.3 已读）与 `advance`（一行加法）。两条扩容路径（预约提交 / 原地生长）在这里汇合。
- [src/kv_pool.rs:L349-L353](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/kv_pool.rs#L349-L353) — `belongs_to`：`Arc::ptr_eq` 判断状态与池是否同源。文档点出动机：「Page ids only mean something against the pool that issued them」——页号只在发它的池里有意义，executor 持多个池时必须先验同源再索引 buffer。
- [src/kv_pool.rs:L324-L343](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/kv_pool.rs#L324-L343) — 只读访问器群：`seq_len` / `last_page_len` / `page_indices_i32`（新建 Vec）/ `extend_page_indices_i32`（追加进调用方的 Vec，与 `KvReservation` 同款「往调用方缓冲里写」的约定）。
- [src/kv_pool.rs:L495-L538](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/kv_pool.rs#L495-L538) — `kv_state_lifecycle` 测试：**本讲生命周期目标的标准答案**。5 页总量（1 padding + 4 可用）；prefill 10 → 1 页；decode 到 16 仍是 1 页（`last_page_len=16`）；第 17 个 token 跨页 → 2 页（`last_page_len=1`，`available_pages` 从 3 到 2）；`reset()`（cfg(test) 专用，见 [L410-L420](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/kv_pool.rs#L410-L420)）归还全部页。
- [src/kv_pool.rs:L540-L562](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/kv_pool.rs#L540-L562) — `kv_state_out_of_pages`（3 页池恰装 32 token，33 报错）与 `kv_state_drop_returns_pages`（作用域结束页回来）。

#### 4.4.4 代码实践

**实践目标**：把「一个请求的一生」从测试里读出来，画成自己的状态迁移图。

**操作步骤**：

1. 逐行阅读 [src/kv_pool.rs:L495-L538](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/kv_pool.rs#L495-L538)，在纸上为每条语句后记录 `(pages, seq_len, available)` 三元组。
2. 画出状态迁移图（节点是三元组，边是 `ensure_capacity`/`advance`/`reset`）。
3. 回答：`for _ in 10..16` 循环里每轮都调 `ensure_capacity(kv.seq_len() + 1)`，但页数为什么只在循环外的第 17 步增加？

**需要观察的现象 / 预期结果**：迁移图与本节 4.4.2 的文字版一致；循环内 `seq_len+1` 从 11 到 16，`pages_needed` 恒为 1（`needed <= held` 提前返回，`try_grow` 根本不会被调），第 17 步 `pages_needed(17)=2 > 1` 才触发生长。运行该测试需 GPU：`cargo test --release -p pegainfer-core --lib kv_state_lifecycle`；无 GPU 环境则以测试断言为预期，待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：`advance` 为什么不做容量检查？
**答案**：调用契约是先 `ensure_capacity` 再 `advance`，容量责任已在扩容方落实；`advance` 只记录「写入已完成」这一事实。把检查塞进来会造成双重校验，且在 kernel 异步写入的场景下宿主此刻也无法核实真实写入量——保持单一职责让每层都可推理。

**练习 2**：`seq_len=16`、`page_size=16` 时 `last_page_len()` 返回什么？为什么不是 0？
**答案**：返回 16（满页）。公式在整除且非零时取 `page_size`。若取 0，kernel 会以为最后一页是空页、丢掉整页 16 个 token 的注意力——「恰好装满」和「一页没有」必须区分。

**练习 3**：`belongs_to` 检查的是什么风险？给出一个没有它会出错的具体场景。
**答案**：页号是池内偏移，跨池无意义。executor 同时持 gemma4 的 local/global 两个池时，若把 global 池发的页号 7 当成 local 池的页 7 去 `buffer()` 索引，读到的是 local 池里完全不相干的页——注意力静默算错而不是崩溃。`Arc::ptr_eq` 把这种错配拦在索引之前。

### 4.5 KvDesc：交给 kernel 的最小元数据

#### 4.5.1 概念说明

`KvDesc<'a>` 是 kernel 面向的请求级元数据：一个页表切片（`&[PageId]`，借自 permit）加 `last_page_len`。文档注释定位它：「Kernel-facing metadata describing this request's paged KV layout... Forward code passes this opaquely to FFI calls.」——前向代码把它**不透明地**传给 FFI，kernel 侧配合 `PagedKvLayout`（池级几何）就能算出所有寻址。

这延续了 u2-l4 的「计划与数据分离」契约：**core 描述状态（哪几页、最后一页多满），kernels 消费状态（按 stride 切页寻址）**。`KvDesc` 只带请求特有的部分，池级不变量（stride）不重复携带——五类信息里它是最小充分集。

两个出口的差异是本模块的考点：

- `desc()`：照实汇报当前状态，`last_page_len` 由 `seq_len` 推出。
- `desc_for_len(seq_len)`：**假想长度**版本，且强校验「持有的页数必须恰好等于该长度需要的页数」——多一页少一页都报错。

#### 4.5.2 核心流程

```text
desc()          → KvDesc { pages: permit.pages(), last_page_len: 由 seq_len 推出 }
desc_for_len(n) → 先 ensure!(held == pages_needed(n))     ← 强校验
                → KvDesc { pages: permit.pages(), last_page_len: 由 n 推出 }
```

为什么 `desc_for_len` 要求**恰好**相等而不是「至少够用」？因为它服务的场景是调度/计划构建期的假想提问（「如果这个请求长度是 n，页表长什么样」）：多出来的页不在假想长度覆盖范围内，把它交给 kernel 会让按页表推算的长度与 `last_page_len` 互相矛盾；少一页则是直接越界。错误信息也把三个数都打出来：`"KvState holds {held} pages where {seq_len} tokens need exactly {needed}"`。

#### 4.5.3 源码精读

- [src/kv_pool.rs:L423-L430](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/kv_pool.rs#L423-L430) — `KvDesc` 结构：两个字段，`pages` 借用 `&'a [PageId]`（零拷贝，生命周期挂在 permit 上），`last_page_len`。
- [src/kv_pool.rs:L432-L445](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/kv_pool.rs#L432-L445) — 实现：公开的 `last_page_len()`；`num_pages()` 仅 `#[cfg(test)]`（测试断言用）；`page_indices()` 是 `pub(crate)`——页号切片不流出 crate，外部世界只能拿到 `page_indices_i32()` 的 i32 拷贝。
- [src/kv_pool.rs:L390-L408](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/kv_pool.rs#L390-L408) — `desc` 与 `desc_for_len` 的对照实现。后者先 `anyhow::ensure!` 强校验再构造。
- 消费端（u2-l4 已读）：`ops/paged_plan.rs` 的 `build_split_kv_csr` 直接 `use crate::kv_pool::KvDesc`，把各请求的 `KvDesc` 展开成 kernel 可消费的 CSR 分页计划——`KvDesc` 是 core 状态层与 core 调度层的交接面。

#### 4.5.4 代码实践

**实践目标**：用断言理解 `desc()` 与 `desc_for_len()` 的语义边界。

**操作步骤**：

1. 阅读 [src/kv_pool.rs:L495-L538](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/kv_pool.rs#L495-L538) 中每次 `desc()` 后的断言（`num_pages` 与 `last_page_len` 的组合）。
2. 思考并在笔记里回答：`seq_len=16`、持有 2 页时，`desc()` 与 `desc_for_len(16)` 各返回什么/是否报错？`desc_for_len(17)` 呢？

**需要观察的现象 / 预期结果**：`seq_len=16` 且持有 2 页是一个「已为下一步备好页但尚未推进」的合法中间态：`desc()` 返回 2 页、`last_page_len=16`（照实）；`desc_for_len(16)` 报错（16 个 token 恰需 1 页，held=2 ≠ 1）；`desc_for_len(17)` 成功（17 token 恰需 2 页，`last_page_len=1`）。这组对照说明 `desc_for_len` 不是「更严格的 desc」，而是「另一个问题」——问的是假想长度。推理以源码 L397–L408 的 `ensure!` 为准，待本地验证（可在 GPU 环境用 `kv_state_lifecycle` 的中间态补一条断言）。

#### 4.5.5 小练习与答案

**练习 1**：`KvDesc` 为什么借用 `&'a [PageId]` 而不是持有 `Vec<i32>`？
**答案**：借用让 `desc()` 零拷贝——页表本来就在 permit 里，kernel 只在本次调用内读它。持有 `Vec` 则每次 `desc()` 都分配并转换一次，decode 循环里每步都调，浪费且增加分配抖动；真正需要 i32 拷贝的场景（H2D 上传）由调用方按需走 `page_indices_i32`/`extend_page_indices_i32`。

**练习 2**：`KvDesc` 不带 stride，kernel 怎么知道每页多大？
**答案**：stride 是**池级**不变量，随 `KvPool::kernel_layout()` 翻译成 `PagedKvLayout` 一次性交给 kernel 侧；`KvDesc` 只带**请求级**变量（页表、末页长度）。两层信息在 kernel 侧会合，各不重复携带——这正是「最小充分信息」的含义。

**练习 3**：`page_indices()` 为什么是 `pub(crate)` 而 `page_indices_i32()` 是 `pub`？
**答案**：`PageId` 是不透明页号，只在 core 内部有意义（u3-l1 已建立此纪律）；`pub(crate)` 保证外部无法绕过类型系统拿到裸页号去别的池乱用。对外的 i32 版本是为 GPU 上传准备的扁平拷贝，`PageId` 类型信息刻意不外泄。

## 5. 综合实践

**任务：为一个假想请求走完全程，并补上缺失的一块测试证据。**

1. **手算对拍**（纯 CPU，必做）：以 Qwen3-4B 几何（36 层、4 KV 头、head_dim=128、page_size=16）手算 `kv_block_len`/`layer_stride`/`page_stride`/每页字节，再算 Qwen3.5-4B（8 层、4 头、256、16）；与 [src/kv_pool.rs:L473-L493](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/kv_pool.rs#L473-L493) 的两个测试逐项对拍，运行 `cargo test --release -p pegainfer-core --lib stride_geometry`。
2. **补一条准入测试**（需 GPU，条件允许时做）：把 4.3.4 的 `kv_pool_try_reserve_returns_none_when_exhausted` 示例代码写进 `kv_pool.rs` 的 `mod tests`，运行 `cargo test --release -p pegainfer-core --lib kv_pool_try_reserve`；它填补的空档是：现有 `kv_state_out_of_pages` 只测了 `ensure_capacity` 的报错路径，没人直接锁住 `try_reserve` 的 `None` 语义。
3. **构造器分工笔记**（必做，纯阅读）：用不超过 200 字写清 `new` / `with_storage` / `with_storage_and_format` 的默认值链（Bf16 → +Split → 全显式），并各配一个真实场景（标准模型 / fp8 KV / Folded 布局），场景证据引用 4.2.4 第 2 步的结论。
4. **生命周期叙事**（必做，纯阅读）：不看本讲 4.4.2 的图，仅凭 [src/kv_pool.rs:L495-L538](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/kv_pool.rs#L495-L538) 重写一遍「一个请求的一生」，标注每步的 `(pages, seq_len, available)` 与触发 API，然后与 4.4.2 对勘。

完成标志：第 1、3、4 步的产物全部能只用源码行号佐证；第 2 步若无 GPU 环境，在测试旁注明「待本地验证」并保留代码。

## 6. 本讲小结

- **page-first 布局**：一页连续容纳 `page_size` 个 token 在所有层的 K/V，`offset = p·page_stride + ℓ·layer_stride + (t·H+h)·D + d`；一个 `PageId` 服务所有层，请求只需一份扁平页表。
- **stride 单一事实源**：`KvLayout::with_storage_and_format` 委托 kernels 的 `derive_strides`，core 与 kernel 永远共享同一套几何算术；`KvStorage`（元素宽度）与 `KvFormat`（形状）是两条独立的轴。
- **三档构造**：`new`（Bf16）→ `with_storage`（+可换 E4m3）→ `with_storage_and_format`（+可换 Folded 布局），溢出在元素域和字节域各查一次后才 `alloc_zeros`。
- **padding 页**：构造期永久扣下 0 号页，`padding_page_id()` 供 CUDA Graph 的 padding 槽使用——页号恒定、内容无害垃圾、输出被丢弃，是图回放确定性的地基之一。
- **两段式准入**：`try_reserve`（schedule 半段，`Option` 语义）→ `commit_reservation`（apply 半段，`absorb` 强制同池）；失败回滚由 `OwnedPagePermit::Drop` 零代码完成，防住多池部分占用。
- **生命周期与最小元数据**：`KvState` 把扩容（页数变）与推进（`seq_len` 变）分离；`KvDesc` 只带页表 + `last_page_len`，`desc()` 照实、`desc_for_len()` 强校验恰好匹配，kernel 靠它 + 池级 `PagedKvLayout` 完成全部寻址。

## 7. 下一步学习建议

- **u3-l3（weight_loader.rs 与 staging.rs）**：同属「状态与装配层」——KV 池管运行时状态，权重加载管启动装配；注意两者显存预算的协调点（`with_storage` 系构造发生在权重落位之后还是之前，以模型 crate 调用序为准）。
- **u4-l1（cuda_graph.rs）**：本讲的 padding 页在那里得到完整闭环——图捕获为什么禁止分配、padding 槽如何进入 FlashInfer 元数据、`buffer()` 的单块设计如何支撑指针稳定性。
- **u5-l1（并发正确性）**：把本讲的两段式准入与 RAII 纪律上升到不变量层面，对照 CLAUDE.md 的 EP free-running 三条不变量（每步集体链固定、launch 次数即全局时钟、padding 行是协议面），看单卡纪律如何延伸到多卡。
- **延伸阅读**：`pegainfer-kernels/src/paged_kv.rs` 的 `RowGeometry` 与 `KvFormat::permute`（Folded 布局的行级细节），以及 `pegainfer-gemma4/src/kv.rs` 的完整准入函数——本讲只引用了其中一段。
