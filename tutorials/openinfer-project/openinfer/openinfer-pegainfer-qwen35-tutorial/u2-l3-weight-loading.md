# 权重加载：safetensors、mmap 与 Qwen35Model 组装

## 1. 本讲目标

上一讲（u2-l2）我们把 TP 几何讲完了：`LocalGeometry` 告诉每个 rank「我拥有全局张量的哪一片」。本讲回答下一个问题：**磁盘上的 safetensors 分片，如何变成 GPU 上一台可以前向的 `Qwen35Model`？**

学完本讲，你应该能够：

1. 描述权重加载管线的四个阶段：分片索引 → mmap → `SafeTensors` 视图 → 按名字上传到 `DeviceMatrix` / `DeviceVec`。
2. 解释 `ModelRuntimeConfig` 四个字段（CUDA Graph、TP、设备序号、快照预算）各自影响加载的哪一步。
3. 说出 `decode_admission_batch`（调度准入上限）与 `reserved_decode_slots`（物理桶容量）为什么必须是两个数（#470）。
4. 解释 `prefix_snapshot_bytes` 如何被换算成 `prefix_snapshot_slots`（快照槽数），以及加载期的一次性显存预算流水线（scratch / recurrent / snapshot / KV 池）。
5. 找出 `tie_word_embeddings` 两条分支各自的 lm_head 来源。

## 2. 前置知识

本讲需要以下概念，前两讲已建立，这里只做一句话回顾加少量新名词：

- **safetensors**：Hugging Face 的权重序列化格式。一个 `.safetensors` 文件 = 文件头（JSON 元数据，记录每个张量的名字、dtype、形状、字节偏移）+ 紧随其后的连续原始数据。大模型通常切成多个分片（shard），外加一个 `model.safetensors.index.json` 索引文件，里面的 `weight_map` 表格记录「张量名 → 所在分片文件」。
- **mmap（内存映射文件）**：把文件按页映射进进程地址空间，访问内存即是读文件。加载权重时用只读 mmap，可以零拷贝地拿到张量数据的字节视图，不必先把整个文件 `read` 进 `Vec<u8>`。
- **`DeviceMatrix` / `DeviceVec`**：`pegainfer-core::tensor` 提供的 GPU 侧张量包装（行数 × 列数的 bf16 矩阵 / 一维向量），底层是 `CudaSlice`。加载的终点就是把 CPU 侧字节变成它们。
- **row-parallel / column-parallel（行并行 / 列并行）**：TP 下切权重矩阵的两种方式——行切（每个 rank 拿 `[rows/W, cols]` 的一段连续行）配列并行输入，列切（每个 rank 拿 `[rows, cols/W]`）配行并行输入。u2-l2 的 `shard_range(total) = (rank × total/W, total/W)` 是它们的公共切分公式。
- **CUDA Graph 桶**：解码路径用 CUDA Graph 重放，图的批大小只能是 `BATCH_BUCKETS = [1, 2, 4, 8, 16, 32, 64]` 里的值，所以物理缓冲要按「下一个 ≥ 请求值的桶」来分配。
- **recurrent 状态**：Gated DeltaNet 线性注意力层（u1-l1、u3-l2 会展开）不靠 KV cache 记历史，而是维护一个固定大小的递推状态（f32 `[H,K,V]` 状态 + bf16 卷积状态）。「快照」就是把某个请求某一步的这套状态整体存下来，供前缀缓存复用（u6 展开）。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `src/weights.rs` | 本讲主角：`ModelRuntimeConfig`、`Qwen35Model` 结构体、`from_safetensors*` 三个构造函数、加载后的显存预算流水线 |
| `src/weights/layers.rs` | 权重名 → 层结构体字段的映射：`WeightSource`、`Shard`、`TransformerBlock35::load` 及两种注意力层 / MLP 的加载器 |
| `../pegainfer-core/src/weight_loader.rs` | 共享加载原语：分片索引解析、mmap、`SafeTensors` 反序列化、按名字查找张量、整卡 / 行切 / 列切 / 拼接上传 |
| `src/batch_decode_graph.rs` | `BATCH_BUCKETS` / `MAX_BATCH` / `bucket_for`——准入与物理容量分离的另一半 |
| `src/recurrent_state.rs` | `per_layer_dims` 与 `bytes_per_request`：一个请求的递推状态占多少字节（快照槽换算的分母） |
| `src/prefill.rs`、`src/prefill_buffers.rs` | `SCRATCH_ESTIMATE_SEQ` 与 `GdrChunkwiseScratch35::estimate_bytes`：预填充 scratch 预留的来源 |
| `src/lib.rs`、`src/model_line.rs` | `--qwen35-prefix-cache-mib` / `--max-batch` 两个 CLI 旗标如何变成 `prefix_snapshot_bytes` 与 `max_batch` 参数 |
| `src/scheduler/backend.rs` | 加载结果的消费方：`prefix_snapshot_slots()` 建缓存、`decode_admission_batch` 决定准入 |
| `Cargo.toml` | 依赖边界（`safetensors`、`cudarc`、`pegainfer-core`）与 `qwen35` feature 转发（u1-l2 已讲） |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：**safetensors mmap 管线**、**运行时配置与构造函数族**、**权重名映射与 TP 分片**、**显存预算**。

### 4.1 safetensors mmap 管线：从分片目录到 SafeTensors 视图

#### 4.1.1 概念说明

加载的第一阶段完全不碰 GPU：把模型目录里的分片文件变成一组「按名字取张量」的视图。这一段全部复用 `pegainfer-core::weight_loader` 的共享原语，本 crate 只贡献两样东西——一个针对 Qwen3.5 发布物的文件名修补（`load_shard_info_fixed`），和一个「锦上添花」的并行预取（`WeightPrefetch`）。

为什么用 mmap 而不是 `fs::read`？权重动辄数 GB，`read` 要先在堆上分配同等大小的缓冲再由内核拷贝一次；mmap 让张量数据直接以字节切片的形式暴露在进程地址空间里，加载器按张量偏移切片上传即可，省一次全量拷贝。

`WeightPrefetch` 是个有趣的性能附件：它开 8 个线程用 16 MiB 为单位把整个检查点预读进内核页缓存，让后续 mmap 缺页不用等磁盘。但它是**建议性**的——加载路径从不依赖它，读错了只汇总成一条 warning。

#### 4.1.2 核心流程

`from_safetensors_with_runtime_and_capacity` 开头的加载序列：

1. `load_shard_info_fixed(model_path)` → 返回 `(分片文件路径列表, weight_map: 张量名 → 分片序号)`。
   - 目录里有单个 `model.safetensors` → 一条路：直接返回它，`weight_map` 为空。
   - 否则解析 `model.safetensors.index.json` 的 `weight_map`。
   - Qwen3.5 特有修补：index 里写的文件名可能是 `model.safetensors-00001-of-00002.safetensors`，而磁盘上的文件实际叫 `model-00001-of-00002.safetensors`——逐个检查路径，不存在就把前缀替换掉再试一次，仍不存在才报错。
2. 仅单卡（`world_size == 1`）时 `WeightPrefetch::spawn` 启动预取；TP 下每个 rank 各自加载，不开预取。
3. `mmap_shards`：对每个分片文件做只读 `Mmap::map`。
4. `deserialize_shards`：对每段 mmap 字节跑 `SafeTensors::deserialize`，得到只解析文件头的 `SafeTensors` 视图——数据本身仍在 mmap 里，零拷贝。
5. 之后所有「按名字取张量」都走 `find_tensor`：`weight_map` 命中就直接索引对应分片；未命中（单文件情形 `weight_map` 为空）就线性扫所有分片。

#### 4.1.3 源码精读

本 crate 侧的四行调用序列，位于构造函数中段：

[加载序列：索引 → 预取 → mmap → 视图](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs#L186-L190)

```rust
let (shard_paths, weight_map) = load_shard_info_fixed(model_path)?;
let prefetch = (geometry.world_size() == 1).then(|| WeightPrefetch::spawn(&shard_paths));
let mmaps = mmap_shards(&shard_paths)?;
let shards = deserialize_shards(&mmaps)?;
```

这四行是整条管线的骨架：修补过的索引 → 单卡才有预取 → mmap → 头部视图。注意 `geometry` 在此之前已经构造好（L161-164：`Config35::from_file` + `LocalGeometry::try_new`），所以「要不要预取」这个决定用的是已验证的 TP 几何。

分片索引的解析与 Qwen3.5 文件名修补在 core：

[load_shard_info：单文件直通或解析 index.json 的 weight_map](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-core/src/weight_loader.rs#L57-L89) —— 先探测 `model.safetensors` 是否存在；不存在就读 `model.safetensors.index.json`，把 `weight_map` 的每个表项转成「分片文件列表下标 + 名字 → 下标」映射。

[load_shard_info_fixed：Qwen3.5 的分片文件名修补](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-core/src/weight_loader.rs#L1050-L1087) —— 文档注释写明动机：Qwen3.5 的 index.json 引用 `model.safetensors-00001-of-00002.safetensors` 这种名字，实际文件却是 `model-00001-of-00002.safetensors`。修补只做一次前缀替换，替换目标不存在则原样报「Shard file not found」。

[mmap_shards：只读映射每个分片并统计总字节](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-core/src/weight_loader.rs#L301-L319) —— `unsafe { Mmap::map(&file) }` 的 SAFETY 注释承诺「映射在加载期间保持存活且文件不会被并发修改」。

[deserialize_shards：mmap 字节 → SafeTensors 视图](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-core/src/weight_loader.rs#L322-L329) —— 只解析头部；张量数据等到按名访问时才以切片形式暴露。

[find_tensor：先查 weight_map，未命中线性扫全部分片](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-core/src/weight_loader.rs#L331-L349) —— 这就是「多分片走索引、单分片走扫描」两条查名路径的汇合点，找不到时报 `Tensor '{}' not found in any shard`。

预取的实现要点（不需要逐行读）：

- [WeightPrefetch::spawn：8 线程、16 MiB 块、RWF_NOWAIT 常驻探测](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-core/src/weight_loader.rs#L141-L257) —— 先用 `preadv2(RWF_NOWAIT)` 探测某块是否已在页缓存（已常驻就跳过不读）；读完 512 MiB 后测速，若源设备低于 1 GB/s 就主动「让路」（standing down），避免和加载路径抢 IO。
- [Drop for WeightPrefetch：取消、join、汇总一条 warning](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-core/src/weight_loader.rs#L260-L292) —— 失败被聚合成「几个不可读分片 / 几个读错误 / 首个错误原因」，绝不向上抛。

预取的生命周期由本 crate 控制：

[ctx.sync 之后才 drop 预取](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs#L250-L255) —— 权重全部上传并 `ctx.sync()` 之后才 `drop(prefetch)`，让预读覆盖整个上传窗口；「GPU model loaded in ...ms」的计时也在这里收口。

#### 4.1.4 代码实践

**实践目标**：亲手看一遍「模型目录 → 分片路径列表」这条链，理解修补逻辑何时触发。

**操作步骤**：

1. 若本地有 Qwen3.5 检查点目录（如 `models/Qwen3.5-4B`），列出其中的 `*.safetensors*` 文件名，并打开 `model.safetensors.index.json` 看 `weight_map` 的前几个表项，核对 index 引用的文件名与磁盘文件名是否一致。
2. 对照 [load_shard_info_fixed 的修补分支](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-core/src/weight_loader.rs#L1058-L1084)，回答：如果 index 引用的名字既不存在、又没有 `model.safetensors-` 前缀，函数行为是什么？
3. 无检查点时，纯读代码回答：单文件模型（只有 `model.safetensors`）时 `weight_map` 是什么？此时 `find_tensor` 走哪条分支？

**需要观察的现象**：真实加载（需 GPU + 权重，待本地验证）时日志应依次出现 `Loading N safetensor shard(s)`（本 crate）、`Prefetching N weight shard(s) (x.x GB) on 8 threads` 与 `Memory-mapped N shard(s) (xxxx.x MB)`（core）。若 index 名字被修补，还会出现 `Fixed shard path: ... -> ...`。

**预期结果**：

- 步骤 2：路径不存在且不能修补 → 直接 `Err("Shard file not found: {path}")`，修补只有一次机会。
- 步骤 3：`weight_map` 为空 `HashMap`；`find_tensor` 走「逐分片尝试 `shard.tensor(name)`」的线性扫描分支。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `WeightPrefetch` 只在 `world_size == 1` 时启动？

**答案**：见 [weights.rs:L188](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs#L188)。TP 下每个 rank 是独立进程/线程各自构造自己的 `Qwen35Model`、各自 mmap 并只切自己的分片；多 rank 同时全量预读同一组文件只会互相争抢 IO 带宽，预取带来的收益递减，干脆不开。

**练习 2**：`deserialize_shards` 之后，张量数据在内存里有几份拷贝？

**答案**：一份——还在内核页缓存支持的 mmap 页里。`SafeTensors` 视图只持头部解析结果，`tensor.data()` 返回的是指向 mmap 字节的切片；只有当某个张量字节未按 dtype 对齐（safetensors 允许）时，`tensor_bf16_cow` / `tensor_f32_cow` 才会解码进一个临时 owned 缓冲再上传。

**练习 3**：预取线程把某一块读失败会发生什么？

**答案**：什么都不发生——错误计数进 `PrefetchStats`，`Drop` 时若总数非零就 `warn!` 一条汇总（首个原因保留）。加载路径完全不受影响，因为预取是建议性的。

### 4.2 运行时配置与构造函数族：ModelRuntimeConfig 与 #470 的「准入 ≠ 物理容量」

#### 4.2.1 概念说明

`Config35`（u2-l1）描述模型的**几何**，`ModelRuntimeConfig` 描述这一次加载的**运行时形态**：开不开 CUDA Graph、是不是 TP 的一份、落在哪块 GPU、留多少显存给前缀快照。它不进 `config.json`，由启动链（单 GPU 走 `lib.rs`，TP 走 `tp_executor.rs`）拼出来。

本模块的第二个主题是 #470 引入的分离：用户请求的并发上限 `--max-batch` 可以是任意 `1..=64` 的数，但 CUDA Graph 只能按 `[1,2,4,8,16,32,64]` 这七个桶捕获。于是模型里有两个数：

- `reserved_decode_slots`：**物理**容量——recurrent 状态槽、解码缓冲、图槽都按「下一个 ≥ 请求值的桶」分配；
- `decode_admission_batch`：**调度准入**上限——调度器最多同时接这么多请求，可以小于物理桶。

`--max-batch 5` 就是典型例子：物理按桶 8 分配（浪费 3 个槽），但准入仍是 5。不这么拆的话，要么强制用户只能填 2 的幂，要么让准入偷偷高于用户要求。

#### 4.2.2 核心流程

三个构造函数是一个漏斗：

```
from_safetensors_with_options(model_path, enable_cuda_graph, max_batch)   // 公开：测试/bench 用
        │  补全 ModelRuntimeConfig 其余字段（默认 TP=None、设备 0、快照预算 0）
        ▼
from_safetensors(model_path, device_ordinal, max_batch, prefix_snapshot_bytes)  // crate 内：单 GPU 启动链用
        │  补全 runtime 其余字段（cuda graph 默认开）
        ▼
from_safetensors_with_runtime_and_capacity(model_path, runtime, max_batch)     // 唯一真身：全部逻辑在这
```

`from_safetensors_with_runtime_and_capacity` 内部的顺序（本模块只看前半）：

1. 校验 `1 <= max_batch <= 64`；
2. `decode_admission_batch = max_batch`（记住请求值），`max_batch = bucket_for(max_batch)`（向上取桶）；
3. 建 `DeviceContext`、读 `Config35`、派生 `LocalGeometry`（TP 几何校验在这步失败）；
4. 用 tokenizer 实际可解码宽度夹紧 `selection_vocab`（u2-l1 的 `bound_selection_vocab`，在权重加载前执行）；
5. 走 4.1 的 mmap 管线、按名加载权重（4.3）；
6. 预算并分配 KV 池等大缓冲（4.4）。

#### 4.2.3 源码精读

[ModelRuntimeConfig：四个运行时字段](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs#L36-L43) —— `enable_cuda_graph`、`tensor_parallel: Option<TensorParallelConfig>`、`device_ordinal`、`prefix_snapshot_bytes`（每 rank 留给完整 recurrent/conv 快照的 GPU 预算，单位字节）。`Default` 是「单卡、开图、零预算」（[L45-L54](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs#L45-L54)）。

[结构体上两个批容量字段的注释](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs#L73-L81) —— `reserved_decode_slots`「实际分配的物理解码容量……总是 BATCH_BUCKETS 里的值」；`decode_admission_batch`「加载时请求的调度并发上限（--max-batch），可以低于物理容量，见 #470」。

[准入与取桶的三行核心](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs#L146-L156) —— 先 `ensure!` 请求值在 `1..=MAX_BATCH`；然后注释写明「下面所有尺寸按物理桶算，只有 `decode_admission_batch` 保留请求值」：

```rust
let decode_admission_batch = max_batch;
let max_batch = super::batch_decode_graph::bucket_for(max_batch);
```

之后整个函数里的 `max_batch` 都是桶值。

[桶表与取整函数](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode_graph.rs#L14-L31) —— `BATCH_BUCKETS = &[1,2,4,8,16,32,64]`，`MAX_BATCH = 64`；`bucket_for` 返回第一个 ≥ 请求值的桶，超过 64 直接 panic（前置 `ensure!` 保证走不到这）。

[tie_word_embeddings 的两条分支](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs#L208-L215) —— `tie_word_embeddings == true`：`lm_head = None`，日志「output projection: tied embed_tokens」；`false`：加载**顶层名字** `lm_head.weight`（注意它不带 `model.language_model.` 前缀），形状 `[vocab_size, hidden_size]`。

[output_projection：tied 时借 embed_tokens 当输出投影](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs#L374-L378) —— `self.lm_head.as_ref().unwrap_or(&self.embed_tokens)`：两条分支在运行期汇合到同一个 GEMM 入口 `output_logits_into`（L387-L405，输出投影 GEMM 与 pad 行 -inf 抑制绑在一起，任何 logits 都只能从这里出）。

[Self 构造：两个字段各归各位](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs#L362-L364) —— `reserved_decode_slots: max_batch`（此时已是桶值）、`decode_admission_batch`（原请求值）。

预算从 CLI 到构造函数的路径：

[CLI 旗标声明](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/model_line.rs#L26-L33) —— `--qwen35-prefix-cache-mib`（每 rank MiB 预算，0 关闭）与 `--max-batch`（注释里就写明「内部取整到下一个图桶，但只准入这么多调度槽」）。

[MiB → 字节](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/lib.rs#L250-L252) —— `prefix_cache_mib.checked_mul(1024 * 1024)`，溢出直接报错；随后单 GPU 路径把它作为 `prefix_snapshot_bytes` 传给 `from_safetensors`（[L294-L299](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/lib.rs#L294-L299)）。

消费侧（调度后端）如何用回准入值：

[max_batch()：准入值与物理槽取 min](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/scheduler/backend.rs#L273-L280) —— `decode_admission_batch.min(slot_states.len()).max(1)`，注释再次点名 #470「5 在桶 8 上」。而图状态本身按物理容量创建：[create_batch_decode_graph_state 用 reserved_decode_slots](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs#L533-L542)。

另外注意 TP 路径：每个 worker 在自己线程里各自调 `from_safetensors_with_runtime_and_capacity` 构造自己的 rank 本地模型（[tp_executor.rs:L703](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/tp_executor.rs#L703)），`prefix_snapshot_bytes` 原样透传——这正是 `ModelRuntimeConfig.prefix_snapshot_bytes` 注释里「per-rank」的含义。

#### 4.2.4 代码实践

**实践目标**：把「请求值 → 桶值 → 准入值」三段变换在纸面走一遍，并核对消费侧。

**操作步骤**：

1. 对 `--max-batch` 取 1、3、5、8、64 五个值，分别写出 `bucket_for` 结果、`reserved_decode_slots`、`decode_admission_batch`，以及 `backend.rs` 里 `max_batch()` 的返回值（假设图槽数足够）。
2. 读 [weights.rs:L544-L553](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs#L544-L553) 的 `create_batch_decode_graph_state_with_capacity`，回答：如果有人请求 `max_batch=9` 的图容量但模型按 `--max-batch 8` 加载，会发生什么？
3. 运行仓库现成单测验证分片几何（无需 GPU，但需要一次 `--features qwen35` 构建）：
   ```bash
   cargo test --release -p pegainfer-qwen35 --features qwen35 --lib weights::layers
   ```

**需要观察的现象**：步骤 3 应看到 `linear_qkv_shard_segments_stitch_head_local_slices` 与 `gated_q_shard_keeps_matching_q_and_gate_rows` 两个测试通过——它们只用 tempfile 里的合成 config 做纯几何计算，不碰 CUDA（运行前提是构建环境带 Python + Triton 的 AOT 工具链，见 u1-l2；结果待本地验证）。

**预期结果**：

| `--max-batch` | `bucket_for` | `reserved_decode_slots` | `decode_admission_batch` | `max_batch()` |
|---:|---:|---:|---:|---:|
| 1 | 1 | 1 | 1 | 1 |
| 3 | 4 | 4 | 3 | 3 |
| 5 | 8 | 8 | 5 | 5 |
| 8 | 8 | 8 | 8 | 8 |
| 64 | 64 | 64 | 64 | 64 |

步骤 2：`ensure!` 失败，报「requested graph capacity 9 exceeds loaded capacity 8」——物理容量是硬上限，请求只能往下压不能往上顶。

#### 4.2.5 小练习与答案

**练习 1**：为什么不直接把 `BATCH_BUCKETS` 作为 `--max-batch` 的合法取值（ clap 的 `value_parser` 限定 2 的幂），从而删掉 `decode_admission_batch`？

**答案**：那会把实现细节泄漏给用户：CUDA Graph 桶是本引擎当前实现的产物，今天改成 3 的幂或连续捕获，CLI 语义就变了。#470 的做法让用户表达真实意图（「我最多要 5 路并发」），取整是实现内部的自由。代价是物理槽可能多于准入数（5/8 浪费 3 个 recurrent 状态槽），这是显式记录在注释里的已知取舍。

**练习 2**：`tie_word_embeddings == true` 时，输出投影 GEMM 的权重矩阵是哪个？它在 TP 下被切分吗？

**答案**：`embed_tokens`（见 `output_projection` 的 `unwrap_or` 分支）。不切分——embed 与 lm_head 都是用 `src.tensor_2d`（整卡加载）而非任何 `*_shard_if_needed` 路径加载的，每个 rank 各持全份。TP 下每步采样集中在 rank 0，输出投影也在 rank 0 上完成（u7 展开）。

**练习 3**：`from_safetensors_with_options` 是 `pub` 的，而另两个是 `pub(crate)`。谁在用前者？

**答案**：crate 的 `runtime` 公开面（本 crate 的测试、调试工具与 criterion 基准，见 u1-l3 的「三层公开面」）——它们只需要最简单的「给个路径和批大小就能起模型」，不必关心设备序号或快照预算，所以默认值补全放在这一层。

### 4.3 权重名到结构体字段的映射：WeightSource 与 TP 分片

#### 4.3.1 概念说明

这是本讲的主菜。检查点里的张量名是 HF 风格的长字符串，Rust 侧是强类型结构体字段；把两边接起来的代码全在 `src/weights/layers.rs`。设计上有三个要点：

1. **名字前缀统一**。所有文本权重都挂在 `wp = "model.language_model"` 前缀下（Qwen3.5 是多模态骨架，语言模型是其中一个子模块）；`lm_head.weight` 例外，在顶层。
2. **`WeightSource` 一次算好所有切分表**。TP 的 `(offset, len)` 不在每层加载时现算，而是在构造 `WeightSource` 时按 config + geometry 一次性算出七张 shard 表（gated q、o_proj 列、k/v 行、MLP 中间维、线性 value 头、线性 z 维、qkv 三段）。每个加载器只「报名字」，不关心自己处于几路 TP。
3. **`Shard` 把「本 rank 的范围」与「全局尺寸」绑在一起**。切分加载前必须先对拍检查点的完整形状（`ensure_global_shape`），防止拿 config A 的范围去切 config B 的权重；`Shard { range, global }` 让这两个数只能成对出现。

加载时还做两类**形状对拍**：整卡加载的 `load_tensor_2d` 断言张量形状等于 config 期望；切分加载先断言全局形状再检查范围越界。任何不一致都在加载边界报错，而不是等到 GEMM 里越界。

另一类变换是**加载期融合**：检查点里分开存的投影，加载时拼成一个矩阵，让运行期一个 GEMM 顶多个——`gate_proj + up_proj → gate_up_proj`（vstack）、`in_proj_qkv(切分后) + in_proj_z → in_proj_qkvz`、`in_proj_b + in_proj_a → in_proj_ba`。

#### 4.3.2 核心流程

一层 `TransformerBlock35` 的加载流程（`prefix = model.language_model.layers.{i}`）：

```
TransformerBlock35::load(src, prefix, layer_type)
├─ 按 layer_types[i] 分发：
│   ├─ FullAttention  → FullAttentionLayer::load(src, "{prefix}.self_attn")
│   └─ LinearAttention→ LinearAttentionLayer::load(src, "{prefix}.linear_attn")
├─ input_layernorm.weight          （1D，整卡复制）
├─ post_attention_layernorm.weight （1D，整卡复制）
└─ MLP35::load
    ├─ gate_proj.weight → row_shard(intermediate) ┐ vstack → gate_up_proj
    ├─ up_proj.weight   → row_shard(intermediate) ┘
    └─ down_proj.weight → col_shard(intermediate)
```

两种注意力层的名字与切分见 4.3.4 的总表（那张表就是本模块实践的参考答案）。

#### 4.3.3 源码精读

[权重前缀与 WeightSource 的构造](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs#L192-L232) —— `wp = "model.language_model"`；`WeightSource::new` 之后，embed_tokens 用 `src.tensor_2d("{wp}.embed_tokens.weight", vocab_size, hidden_size)` 整卡加载；然后按 `config.layer_types[i]` 逐层调 `TransformerBlock35::load`。

[embed_tokens 与 lm_head 的加载点](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs#L197-L215) —— embed 的行数用 `config.vocab_size`（检查点的物理行数，包括 padding 行），lm_head 同样按 `[vocab_size, hidden]` 对拍。

[TransformerBlock35::load：按 LayerType 分发 + 两个 norm + MLP](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights/layers.rs#L111-L133) —— 注意子前缀是 `self_attn` 还是 `linear_attn` 由 `layer_type` 决定，与 u2-l1 讲过的 `layer_types` 字符串一一对应。

[FullAttentionLayer::load：五个名字 + norm](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights/layers.rs#L135-L146) —— `q_proj` 走专用 `gated_q_proj`（带门控，按头切），`k/v_proj` 行切，`o_proj` 列切，`q_norm/k_norm` 整卡 1D。

[LinearAttentionLayer::load：融合 + 三类特殊向量](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights/layers.rs#L148-L189) —— 注释写明动机：「检查点分四个投影发货，vLLM 在加载时融合成 in_proj_qkvz / in_proj_ba；同样融合让每组一次 GEMM 而不是四次 launch」。`A_log` 与 `norm.weight` 走 f32 通道（检查点存 bf16 也在加载点精确加宽）。

[MLP35::load：gate/up 各自行切再 vstack，down 列切](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights/layers.rs#L86-L101) —— `vstack` 后立刻 drop 两个临时矩阵，避免峰值显存翻倍。

[Shard：range 与 global 成对](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights/layers.rs#L191-L211) —— 文档注释直说设计意图：「切分加载在切片前断言检查点的全局形状，所以 range 和 global 必须一起构造，调用点没法把一个维度的 range 配上另一个维度的 global」。这是 u2-l2「不可表示非法状态」风格在权重层的翻版。

[WeightSource：七张 shard 表](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights/layers.rs#L224-L275) —— 字段即切分表：`gated_q`、`q_cols`（o_proj 列）、`kv_rows`、`intermediate`、`linear_value_heads`、`linear_z`、`linear_qkv`（三段）+ `linear_conv1d`。`hidden` 字段记录「每个不切分投影必须携带的模型宽度」，让与 config 不符的检查点死在加载器而不是 GEMM。

四条通用加载通道（`is_sharded()` 即 `world_size > 1` 时才切，否则全部退化为整卡加载）：

[row_shard_if_needed / col_shard_if_needed](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights/layers.rs#L285-L317) —— 行切拿 `[range, hidden]`，列切拿 `[hidden, range]`；`Shard.global` 提供对拍用的完整形状。

[tensor_1d_shard_if_needed / tensor_1d_f32_shard_if_needed](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights/layers.rs#L319-L334) —— 1D bf16 与 1D f32（bf16 加宽）两条通道。

两条特殊切分（都不再是「一段连续范围」）：

[linear_in_proj_qkv：三段拼接而不是一刀切](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights/layers.rs#L336-L350) 与 [linear_qkv_shard 的构造](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights/layers.rs#L386-L407) —— 检查点把 qkv 存成 `[全部 q 行 | 全部 k 行 | 全全部 v 行]`；TP 下每个 rank 要从**每段里各抽自己那截**再按序拼回 `[q_local | k_local | v_local]`。注释里的警告值得抄下来：「绝不能跨段重排——q 行按 key 头计，v 行按 value 头计」。v 段行数复用 `linear_attn_z_dim()`（= `linear_num_value_heads × linear_value_head_dim`，见 [config/model.rs:L154-L157](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/config/model.rs#L154-L157)）。

[gated_q_proj：按头切而不是按行切](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights/layers.rs#L367-L383) 与 [full_attention_gated_q_shard](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights/layers.rs#L417-L431) —— HF/内核把 q_proj 行解释为「每个头先 `head_dim` 行 q、紧跟 `head_dim` 行 gate」；平分一半行会把某个头的 q 和隔壁头的 gate 拼到一起。所以范围按 `头数 × head_dim × 2` 对齐：rank1 从「自己的第一个头的 q 行」开始，而不是全局行数的中点。

[conv1d 通道镜像 qkv 行，尺度换成卷积 tap](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights/layers.rs#L409-L415) —— 展平的 conv 权重每通道 `kernel_dim` 个 tap 连续，所以三段范围原样乘上 `kernel_dim`。

core 侧的底层保证（对拍发生在任何切片之前）：

[load_tensor_2d_row_shard：先 ensure_global_shape 再检查范围再切片](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-core/src/weight_loader.rs#L817-L834) 与 [ensure_global_shape](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-core/src/weight_loader.rs#L800-L812) —— 「拒绝一个完整张量形状与 shard 范围来源 config 不符的检查点，在信任它的任何切片之前」。

[load_tensor_2d_row_stitch：多段范围按序拼接](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-core/src/weight_loader.rs#L885-L908) —— qkv 三段拼接的底层实现：逐段校验范围、逐段 extend 进 host 缓冲再整体上传。

[load_tensor_1d_f32_shard：f32 一维切片，bf16 源在加载点加宽](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-core/src/weight_loader.rs#L941-L976) —— 供 `A_log` 这类「内核按 f32 消费」的向量使用（`tensor_f32_cow` 保证 bf16→f32 精确）。

[load_tensor_2d：整卡加载也做形状断言](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-core/src/weight_loader.rs#L763-L778) —— 「Tensor has shape {actual}, config expects [rows, cols]」，config 与检查点的一切分歧在这里暴露。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：在 `from_safetensors_with_runtime_and_capacity` 与 `layers.rs` 中定位每一条「权重名 → 结构体字段」的映射，整理成清单，并标注 TP 切分方式。

**操作步骤**：

1. 从 [weights.rs:L192-L234](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs#L192-L234) 出发，抄下 `wp` 前缀；再进入 [layers.rs 的四个 load 函数](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights/layers.rs#L86-L189)，把每个 `format!("{prefix}.xxx.weight")` 展开成完整名字。
2. 对每个名字记录：目标结构体字段（含层类型）、形状约定、TP 下的加载通道（整卡复制 / row shard / col shard / 三段 row stitch / 1D shard / 1D f32）。
3. 标出哪些名字在 TP 下**不**切分，并想想为什么。

**参考答案**（即权重名清单总表；形状记号 `[rows, cols]`，均针对 rank 本地视角）：

| 检查点张量名（`{wp}` = `model.language_model`） | 字段 | 形状 | TP 切分 |
|---|---|---|---|
| `{wp}.embed_tokens.weight` | `Qwen35Model.embed_tokens` | `[vocab_size, hidden]` | 不切，全量复制 |
| `lm_head.weight`（顶层，仅 untied） | `lm_head: Option<DeviceMatrix>` | `[vocab_size, hidden]` | 不切，全量复制 |
| `{wp}.norm.weight` | `Qwen35Model.norm` | `[hidden]` | 不切 |
| `layers.{i}.input_layernorm.weight` | `TransformerBlock35.input_layernorm` | `[hidden]` | 不切 |
| `layers.{i}.post_attention_layernorm.weight` | `TransformerBlock35.post_attention_layernorm` | `[hidden]` | 不切 |
| `layers.{i}.self_attn.q_proj.weight`（full） | `FullAttentionLayer.q_proj` | `[heads×head_dim×2, hidden]`（含 gate） | **按头 row shard**（每头 q+gate 相邻） |
| `layers.{i}.self_attn.k_proj.weight` | `k_proj` | `[kv_heads×head_dim, hidden]` | row shard（`kv_rows`） |
| `layers.{i}.self_attn.v_proj.weight` | `v_proj` | 同上 | row shard（`kv_rows`） |
| `layers.{i}.self_attn.o_proj.weight` | `o_proj` | `[hidden, heads×head_dim]` | col shard（`q_cols`） |
| `layers.{i}.self_attn.q_norm.weight` / `k_norm.weight` | `q_norm` / `k_norm` | `[head_dim]` | 不切（头间广播） |
| `layers.{i}.linear_attn.in_proj_qkv.weight`（linear） | 与 z 融合成 `in_proj_qkvz` 下半部 | `[q\|k\|v 三段, hidden]` | **三段 row stitch**（每段抽本 rank 头） |
| `layers.{i}.linear_attn.in_proj_z.weight` | 融合成 `in_proj_qkvz` 上部 | `[z_dim, hidden]` | row shard（`linear_z`） |
| `layers.{i}.linear_attn.in_proj_b.weight` / `in_proj_a.weight` | 融合成 `in_proj_ba`（b 在下、a 在上） | `[value_heads, hidden]` | row shard（`linear_value_heads`），再 vstack |
| `layers.{i}.linear_attn.conv1d.weight` | `conv1d_weight` | `[qkv_dim × kernel_dim]` 展平 | 1D stitch（三段 × kernel_dim） |
| `layers.{i}.linear_attn.dt_bias` | `dt_bias` | `[value_heads]` | 1D shard（bf16） |
| `layers.{i}.linear_attn.A_log` | `a_log: CudaSlice<f32>` | `[value_heads]` | 1D f32 shard（bf16 加宽） |
| `layers.{i}.linear_attn.norm.weight` | `norm_weight: CudaSlice<f32>` | `[value_head_dim]` | 不切（头共享，注释明言每 rank 复制） |
| `layers.{i}.linear_attn.out_proj.weight` | `out_proj` | `[hidden, z_dim]` | col shard（`linear_z`；行并行，层内 all-reduce） |
| `layers.{i}.mlp.gate_proj.weight` | 与 up 融合成 `gate_up_proj` 下/上半 | `[intermediate, hidden]` | row shard（`intermediate`），再 vstack |
| `layers.{i}.mlp.up_proj.weight` | 融合见上 | 同上 | 同上 |
| `layers.{i}.mlp.down_proj.weight` | `down_proj` | `[hidden, intermediate]` | col shard（`intermediate`） |

3 的答案要点：embed / lm_head / 各 norm 不切——norm 沿特征维逐元素乘，切了就得再 all-reduce；embed 与输出投影每 rank 全持是为了采样与 logits 的局部性。所有投影类权重（q/k/v/o、in_proj*、out_proj、mlp）都切：这正是 u2-l2 整除门保证 `shard_range` 平分的地方。

**需要观察的现象 / 预期结果**：表格里的每个「融合」都应在源码中找到对应的 `DeviceMatrix::vstack` 调用；每个「row shard / col shard」都能追到 `load_tensor_2d_row_shard` / `load_tensor_2d_col_shard`，且调用参数里的全局形状来自 `Shard.global`。本实践纯源码阅读，无需 GPU。

#### 4.3.5 小练习与答案

**练习 1**：假设 `num_attention_heads = 16`、`head_dim = 256`、TP=2。`q_proj` 全局 8192 行，rank 1 的行范围是 `(4096, 4096)` 吗？为什么？

**答案**：是 `(4096, 4096)`，但**不是**因为「8192 的一半」这么简单——是 `full_attention_gated_q_shard` 按 `gated_rows(heads) = heads × head_dim × 2` 计算：rank 1 拥有后 8 个头，从第 8 个头的 q 行（`gated_rows(8) = 8×256×2 = 4096`）开始，长度 `gated_rows(8) = 4096`。数值上碰巧等于平分，但这依赖「每头恰好 q+gate 两段等长」的结构；[对应单测](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights/layers.rs#L493-L508) 的注释专门强调「rank 1 从自己的第一个头的 q 行开始，不是全局中点」。

**练习 2**：`in_proj_qkv` 在 TP=2、（fixture 几何：key heads 16、head dim 128、value 段 4096 行）下，rank 0 拿到哪些行？

**答案**：三段各抽前半并按序拼接：q 段 `(0, 1024)`、k 段 `(2048, 1024)`、v 段 `(4096, 2048)`，全局 8192 行。这正是 [linear_qkv_shard_segments_stitch_head_local_slices](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights/layers.rs#L473-L491) 断言的内容，测试还验证两 rank 的三段「无缝且不重叠地铺满 [0, qkv)」。

**练习 3**：`A_log` 在检查点里是 bf16，为什么加载成 `CudaSlice<f32>`？`norm_weight` 也是 f32，为什么不切分？

**答案**：GDN 递推内核按 f32 精度消费这些向量，加载点用 `tensor_f32_cow` 做 bf16→f32 的**精确**加宽（bf16 是 f32 的截尾子集，加宽无损），运行期就不必反复转换。`norm_weight` 长度是 `value_head_dim` 且头间共享（[字段注释](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights/layers.rs#L49-L51)：「head-shared, so replicated on every rank」），不随 value 头数切，TP 下每 rank 全份。

### 4.4 显存预算：快照槽、递推状态、scratch 与 KV 池

#### 4.4.1 概念说明

权重上完之后，构造函数还要一次性分配所有「运行期大缓冲」：分页 KV 池、递推状态槽、CUDA Graph 槽、预填充 scratch、前缀快照槽。这一步的顺序是本模块的灵魂：

> **先扣掉一切「必须活到服务结束」的预留，再把剩余的 85% 给 KV 池。**

为什么 KV 池最后分、而且只拿 85%？因为 KV 池是唯一「多多益善」的弹性项——页越多能同时服务的上下文越长，但其他四项少一个字节服务就起不来。反过来，如果先把空闲显存全给 KV，一条长提示词的 prefill scratch 就会把进程 OOM 掉。85% 的余量（而不是 100%）给运行期的临时分配（解码缓冲、采样缓冲等）留头。

「快照槽数」的换算是本讲学习目标里点名的公式：预算是**字节**（`--qwen35-prefix-cache-mib` × 1 MiB），而缓存要的是**整槽数**——一槽存一个请求某一步的完整 recurrent + conv 状态。槽是整数除法向下取整，预算除不满一槽就要 fail-fast（给钱买不到任何东西，宁可报错）。

#### 4.4.2 核心流程

权重与 RoPE 缓存就绪后的预算流水线（[weights.rs:L261-L334](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs#L261-L334)）：

```
free = cuMemGetInfo().free
① scratch_reserve   = GdrChunkwiseScratch35::estimate_bytes(config, geometry, 20_000)
② recurrent_reserve = 2 × bucket(max_batch) × bytes_per_request(config, geometry)
③ snapshot_slots    = prefix_snapshot_bytes / bytes_per_request        （整除向下取整）
   snapshot_reserve = snapshot_slots × bytes_per_request
④ 校验 free ≥ ①+②+③+64 页 KV 的下限，不足即报错（提示降 --max-batch / 减预算 / 换小模型）
⑤ kv_budget = 0.85 × (free − ① − ② − ③)
   num_pages  = max(kv_budget / bytes_per_page, 64)
⑥ 分配 KvBuffer（full-attn 层数 × 本地 KV 头 × head_dim × 16 token/页）
⑦ 上传 pad 行 -inf 抑制表（selection_vocab 有 tile 对齐 padding 时）
```

其中 `bytes_per_request`（一个请求的完整递推状态字节数）：

\[
\text{bytes\_per\_request} = L_{\text{linear}} \times \big( \underbrace{H_v \times d_k \times d_v \times 4}_{\text{f32 state}} + \underbrace{d_{qkv} \times (k_{conv}-1) \times 2}_{\text{bf16 conv state}} \big)
\]

（\(L_{\text{linear}}\) = 线性注意力层数，\(H_v\) = 本 rank value 头数，\(d_k/d_v\) = 128/128，\(d_{qkv}\) = 本 rank qkv 维，\(k_{conv}\) = 卷积核宽 4。）

#### 4.4.3 源码精读

[两个常量：每槽两份状态与 KV 页下限](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs#L96-L99) —— `STATES_PER_DECODE_SLOT = 2`（一个图槽状态 + 每解码槽一份在途 prefill 瞬态）、`MIN_KV_PAGES = 64`（KV 池地板，兼低显存 fail-fast 阈值）。

[KV 布局与每页字节](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs#L261-L278) —— 页大小 16 token；`KvLayout` 只覆盖 **full-attn 层**（GDN 层不进 KV 池！这是混合架构的直接后果：`num_full_layers = config.num_full_attention_layers()`）；`bytes_per_page = layout.page_stride × 2`（bf16）；`cudarc::driver::result::mem_get_info` 问驱动要空闲显存。

[预留顺序的注释直说动机](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs#L279-L289) —— 「在分配 KV 池**之前**为 prefill scratch（GDR chunkwise + 每层瞬态）预留，免得 prefill OOM」。scratch 按 `SCRATCH_ESTIMATE_SEQ = 20_000` token 估（[prefill.rs:L12-L20](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefill.rs#L12-L20)：分块长度与预留长度刻意相等，预留恰好覆盖一个 chunk）；recurrent 预留 = 每槽 2 份 × **物理桶** `max_batch` × 每请求字节数。

[快照槽换算与「买不到一槽」的 fail-fast](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs#L290-L299) —— `snapshot_slots = prefix_snapshot_bytes / snapshot_bytes_per_slot`（整除）；`ensure!(prefix_snapshot_bytes == 0 || snapshot_slots > 0)`，报错文案把预算 MiB 与单快照 MiB 都打出来，用户一眼看出该加多少钱。`snapshot_reserve = slots × per_slot`（整除的零头不占）。

[总量校验：不足即拒，附降级建议](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs#L300-L314) —— 四项预留 + 64 页 KV 的最小需求与空闲显存对拍，错误信息逐项列 MB 并建议「降 --max-batch、降 prefix-cache 预算或换小模型」。

[85% 与页数](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs#L315-L326) —— `kv_budget = (available × 0.85)`，`num_pages = max(kv_budget / bytes_per_page, 64)`；info 日志把页数 / 各项 MB / 占空闲百分比一次打全，这份日志就是核对预算推导的对账单。

[KvBuffer 分配](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs#L327-L334) —— 分页 KV 池本体，落在 `pegainfer-kv-cache`。

[pad 行 -inf 抑制表](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs#L335-L348) —— `selection_vocab > decodable_vocab` 时（u2-l1 讲过的 tile 对齐 padding），把 `[decodable_vocab, selection_vocab)` 的 id 打包成 `SuppressIds` 上传；这些行是真实的检查点嵌入但**不是可解码 token**，「绝不能赢得选择」。

每请求字节数的出处：

[per_layer_dims：每层元素数](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/recurrent_state.rs#L53-L62) —— `(线性层数, f32 state 元素数 = H_v×d_k×d_v, bf16 conv 元素数 = qkv 维 × (kernel−1))`，被分配与预留两处共享。

[bytes_per_request：分配与快照共用的分母](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/recurrent_state.rs#L229-L235) —— 线性层数 × (state×4B + conv×2B)。

槽数的消费方在调度后端：

[Qwen35PrefixCache 与 RecurrentStateStore 都用 prefix_snapshot_slots()](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb81f93188c82d2583df18509/pegainfer-qwen35/src/scheduler/backend.rs#L237-L244) —— KV 侧快照目录与 recurrent 状态仓库用**同一个**槽数构造，还 `debug_assert_eq!` 两边长度一致（快照必须成对：KV 页 + recurrent 状态一起恢复才有意义，u6 展开）。TP 下更进一步：[tp_executor.rs:L744-L748](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/tp_executor.rs#L744-L748) 校验所有 rank 的槽数一致——每 rank 预算相同、几何相同，槽位编号才谈得上对齐。

#### 4.4.4 代码实践

**实践目标**：手算一份快照预算表，验证你对换算公式的掌握。

**操作步骤**：

1. 读 [per_layer_dims](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/recurrent_state.rs#L55-L62) 与 [layers.rs 测试夹具的合成 config](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights/layers.rs#L437-L466)（value heads 32、key/value head dim 128、conv kernel 4、qkv 全局 8192 行）。
2. 按 TP=1 推导 `bytes_per_request`：state = 32×128×128 = 524 288 个 f32 = 2 MiB；conv = 8192×(4−1) = 24 576 个 bf16 = 48 KiB；每层合计 2 146 304 B。乘以 24 个线性层（Qwen3.5-4B 的层排布，u1-l1）得每请求 ≈ 49.13 MiB。
3. 对 `--qwen35-prefix-cache-mib` 取 1024、256、32 三个值，算 `snapshot_slots` 与 `snapshot_reserve`。
4. 核对：哪个值会触发 4.4.3 的「买不到一槽」错误？

**需要观察的现象 / 预期结果**（纸面推导；真实数字以本地 `config.json` 为准——夹具是单层合成几何，若真实 4B 的头数/层数不同，代入即可，方法不变，待本地验证）：

| 预算 | 字节数 | 槽数（÷51 511 296 B） | 实际占用 |
|---:|---:|---:|---:|
| 1024 MiB | 1 073 741 824 | 20（20.84 向下取整） | ≈ 984 MiB |
| 256 MiB | 268 435 456 | 5（5.21） | ≈ 246 MiB |
| 32 MiB | 33 554 432 | 0 | —— 报错 |

32 MiB < 49.13 MiB：`snapshot_slots = 0` 且预算非零 → `ensure!` 失败，错误信息形如「prefix-cache budget is 32 MiB, but one recurrent/conv snapshot requires 49.125 MiB」。另外注意整除的零头（1024 MiB 时约 40 MiB）不会被占用，留给的是空闲显存而非保留。

#### 4.4.5 小练习与答案

**练习 1**：`recurrent_reserve` 用的是请求的 `--max-batch` 还是取整后的桶值？为什么必须是它？

**答案**：桶值（4.4.3 第二段引用处用的是取整后的 `max_batch`）。因为物理缓冲按桶分配：`--max-batch 5` 时 recurrent 状态槽、CUDA Graph 槽都要按 8 份建（图重放时 5 个真实请求 + 3 个 padding 行，padding 行也有状态槽）。若按 5 份预留，图捕获到第 6~8 槽时就会踩到未分配的显存。

**练习 2**：KV 池为什么只按 full-attn 层（4B 是 8 层）算，而不是全部 32 层？

**答案**：混合架构里只有 full-attention 层用分页 KV 记历史；GDN 线性层的历史全压缩在固定大小的递推状态里，不随上下文增长（u1-l1 的核心动机）。所以 `KvLayout::new(num_full_layers, …)` 与 `KvBuffer::new(num_full_layers, …)` 都只传 `config.num_full_attention_layers()`——这也是本 crate 与纯 full-attention 模型（如 qwen3 crate）在预算公式上最大的差异。

**练习 3**：把 85% 改成 100% 会发生什么？

**答案**：KV 页多吃约 1/6 的剩余显存，但运行期还需要临时分配——解码/采样缓冲、unified step 的拼接缓冲、NCCL warm-up scratch（[warmup_tp_collective](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs#L437-L459) 就要临时 alloc 一块 `最大桶 × hidden` 的 bf16）。这些分配在启动后期或服务期发生，会把「启动成功、跑两步 OOM」这类最难排查的失败引进来；15% 是买确定性的一笔保险。

## 5. 综合实践

**任务：为一次真实（或纸面模拟的）启动写一份「加载对账单」。**

假设目标是 Qwen3.5-4B、单 GPU、`--max-batch 5 --qwen35-prefix-cache-mib 1024`。请产出三样东西：

1. **权重清单**：以 4.3.4 的总表为底，补上数量列——embed 1 份、每层 8 份 full-attn 名字组 × 8 层、linear 名字组 × 24 层、顶层 4 份（norm、可选 lm_head），并标注哪些会因 `tie_word_embeddings` 而不出现（读你手头 config.json 的该字段决定答案）。
2. **预算推演**：从显卡空闲显存出发（无 GPU 就设一个假设值，如 24 GB 卡剩 22 GB），依次算 scratch（读 `GdrChunkwiseScratch35::estimate_bytes` 的实现确认它随什么线性伸缩）、recurrent（2 × 8 × bytes_per_request）、snapshot（1024 MiB → 20 槽）、剩余 × 85% → 页数。每一步标注对应的源码行。
3. **对账**：有 GPU + 权重的读者（待本地验证）实际启动一次：
   ```bash
   cargo run --release --features qwen35 -- --model-path models/Qwen3.5-4B \
       --max-batch 5 --qwen35-prefix-cache-mib 1024
   ```
   把启动日志里的 `Qwen3.5 KV cache: N pages (X MB), prefill scratch reserve: …, recurrent-state reserve: …, prefix snapshots: … slots` 与你的推演逐项对比，解释每一处偏差的可能来源（页取整、驱动保留显存、估计函数的保守系数）。

这个任务串起本讲全部四个模块：名字映射（4.3）、准入/物理分离（4.2 的 5→8）、预算公式（4.4）、以及日志即对账单的工程习惯。

## 6. 本讲小结

- 权重加载分四段：`load_shard_info_fixed`（含 Qwen3.5 分片文件名修补）→ `mmap_shards` → `deserialize_shards`（只解析头部的零拷贝视图）→ `WeightSource` 按名上传；预取是单卡专属的建议性优化，失败只汇总成 warning。
- `ModelRuntimeConfig`（CUDA Graph / TP / 设备 / 快照预算）与 `Config35`（几何）正交；`from_safetensors_with_runtime_and_capacity` 是唯一真身，另外两个构造函数只是补默认值的漏斗。
- #470 把「请求的准入上限」（`decode_admission_batch`，任意 1..=64）与「物理桶容量」（`reserved_decode_slots`，七个 2 的幂桶）拆成两个字段：一切缓冲按桶分配，调度器只准入请求值。
- `WeightSource` 预计算七张 shard 表，加载器只报名字；`Shard{range, global}` 保证切分前必先对拍检查点全局形状；两类特殊切分——gated q 按「头」切、linear qkv 按三段拼——都不能用一刀平分代替。
- `prefix_snapshot_bytes ÷ bytes_per_request = snapshot_slots`（整除，零头不占；买不到一槽即 fail-fast）；显存预算先扣 scratch（20 000 token）+ recurrent（2 × 桶 × 每请求）+ 快照，再把剩余的 85% 给只覆盖 full-attn 层的分页 KV 池。
- `tie_word_embeddings` 决定 lm_head 来源：tied 时输出投影直接复用 `embed_tokens`，untied 时加载顶层 `lm_head.weight`；两条分支在 `output_logits_into` 汇合成唯一的 logits 出口（GEMM + pad 行 -inf 抑制绑定在一起）。

## 7. 下一步学习建议

本讲结束，模型已经完整地站在 GPU 上了，但每一层「怎么算」还是黑盒。下一讲 **u2-l4（层结构：TransformerBlock35 与两种注意力层）** 正好接上：本讲你只看到这些层的字段被填充，u2-l4 讲它们的前向数据流（norm → attn/mlp → 残差）与 `LayerKind` 分发。之后：

- 想深挖 GDN 状态布局（本讲的 `bytes_per_request` 分母），读 [src/recurrent_state.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/recurrent_state.rs) 并等 u3-l2。
- 想看 scratch 预留的用途，进入 u4-l1 的 prefill 分块链路（`PREFILL_CHUNK_LEN` 与 `SCRATCH_ESTIMATE_SEQ` 相等的设计会在那里兑现）。
- 想看快照槽怎么被用掉，跳到 u6（联合前缀缓存）——记得带上本讲的换算公式。
- 建议顺手通读 [src/weights/layers.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights/layers.rs) 的两个分片单测：它们是「无 GPU 也能验证 TP 几何」的最好样板。
