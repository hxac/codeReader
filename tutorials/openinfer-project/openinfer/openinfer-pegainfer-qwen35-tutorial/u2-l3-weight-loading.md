# 权重加载：safetensors、mmap 与 Qwen35Model 组装

## 1. 本讲目标

学完本讲，你应该能够：

1. 完整描述一条权重从磁盘上的 safetensors 分片，到 GPU 上 `DeviceMatrix` / `DeviceVec` 的加载管线，并说出每一步由哪个函数负责。
2. 解释 `ModelRuntimeConfig` 的四个运行时旋钮（CUDA Graph、TP、设备序号、前缀快照预算）如何传入加载过程，以及 `decode_admission_batch` 与物理桶容量 `reserved_decode_slots` 为何要分开保存（issue #470）。
3. 手算 `prefix_snapshot_bytes` 如何除以单请求快照字节数得到快照槽数，并复现 Qwen3.5-4B「每请求 49.125 MiB」这个数。
4. 找出 `tie_word_embeddings` 两条分支各自的 lm_head 来源：Untied 时加载独立的 `lm_head.weight`，Tied 时直接复用 `embed_tokens`。
5. 整理出一份「权重名 → 结构体字段 → 是否按 shard 切分」的映射表（本讲综合实践）。

## 2. 前置知识

本讲假设你已读过前几讲，特别是：

- **u2-l1**：`config.json` 如何经 `RawConfig` → `Config35` 一次性校验；三个词表宽度（`vocab_size` / `decodable_vocab` / `selection_vocab`）各司其职。
- **u2-l2**：`LocalGeometry` 如何从 `Config35` + `TensorParallelConfig` 派生本 rank 的 `local_*` 维度，以及 `shard_range` 切分。
- **u1-l1**：Qwen3.5 是「全注意力 + Gated DeltaNet 线性注意力」混合架构，crate 内并存 KV cache 与 recurrent 状态两套系统。

再补三个本讲要用到的背景概念：

- **safetensors**：Hugging Face 推出的权重序列化格式。文件头部是一段 JSON 元数据（记录每个张量的名字、dtype、形状、字节偏移），之后是连续的原始数据。它不做压缩、布局可预测，所以适合直接 `mmap`。
- **mmap（内存映射文件）**：把文件「映射」进进程地址空间，访问内存就是访问磁盘内容，由操作系统按页懒加载。加载大权重时不必一次性 `read` 整个文件进堆内存。
- **显存预算（memory budgeting）**：加载完权重后，剩余显存要在「预填充 scratch、recurrent 状态、前缀快照、KV 池」四者之间分配。分配顺序本身就是一条正确性约定：先为会 OOM 的消费方预留，再把余量给 KV 池。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `src/weights.rs` | 本讲主线。`Qwen35Model` 结构体、`from_safetensors*` 三个构造函数、显存预算计算、KV 池与 pad 抑制的创建 |
| `src/weights/layers.rs` | 层容器类型（`TransformerBlock35`、`FullAttentionLayer`、`LinearAttentionLayer`、`MLP35`）与 `WeightSource`——权重名到结构体字段的映射全部在这里 |
| `pegainfer-core/src/weight_loader.rs` | 跨模型共享的加载底座：分片索引解析、mmap、反序列化、预取线程、逐张量（含分片/拼接）装载 |
| `src/recurrent_state.rs` | `bytes_per_request`：一个请求的 recurrent/conv 快照字节数，是快照槽数换算的分母 |
| `src/batch_decode_graph.rs` | `BATCH_BUCKETS` / `MAX_BATCH` / `bucket_for`：物理解码容量的取整规则 |
| `src/lib.rs` | `Qwen35LaunchOptions.prefix_cache_mib` → `prefix_snapshot_bytes` 的 MiB 换算，以及对 `from_safetensors` 的调用点 |
| `Cargo.toml` | 依赖声明：`safetensors`、`pegainfer-core` 等；`qwen35 = ["pegainfer-kernels/qwen35"]` 的 feature 转发 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**safetensors mmap**（权重怎么进来）、**运行时配置**（加载时受什么控制）、**显存预算**（剩余显存怎么分）。

### 4.1 模块一：safetensors mmap——从分片文件到 GPU 张量

#### 4.1.1 概念说明

一个 Qwen3.5 checkpoint 通常被切成若干个 `*.safetensors` 分片，外加一个 `model.safetensors.index.json` 索引文件，说明「每个张量住在哪个分片里」。加载要解决三件事：

1. **找到张量**：名字 → 分片文件的定位；
2. **不撑爆内存**：用 mmap 让操作系统按需把文件页搬进来，而不是把整个文件读进堆；
3. **校验形状**：张量的真实形状必须与 `config.json` 声明一致，且分片切片范围合法——错误要在装载时爆掉，而不是留到 GEMM 里。

#### 4.1.2 核心流程

加载管线的五步（对应下文源码精读）：

```text
① load_shard_info_fixed   读 index.json → (分片路径列表, weight_map: 名字→分片序号)
        │                 （单文件 checkpoint 直接返回那一个文件）
② WeightPrefetch::spawn   仅单 GPU：开 8 个线程做 advisory 预取，把文件页提前读进 page cache
③ mmap_shards             每个分片 mmap 进地址空间
④ deserialize_shards      在每个 mmap 上解析 safetensors 头 → SafeTensors 视图（零拷贝借用 mmap）
⑤ WeightSource 逐张量装载  按 config 声明的形状整取 / 行切 / 列切 / 多段拼接 → DeviceMatrix/DeviceVec
```

其中第 ⑤ 步有四种装载路径：

| 路径 | 函数 | 语义 |
|------|------|------|
| 整取 | `tensor_2d` / `tensor_1d` | 不切分，全量上传（embed、lm_head、norm 等） |
| 行切 | `row_shard_if_needed` | 取全局矩阵的第 `[offset, offset+len)` 行（TP 下输出维度分片） |
| 列切 | `col_shard_if_needed` | 取列分片（TP 下 o_proj / down_proj / out_proj 这类「输入侧」分片） |
| 拼接 | `linear_in_proj_qkv` / `linear_conv1d` / `load_tensor_2d_row_stitch` | 从全局张量的**多个段**各取本 rank 的局部行，拼成一个连续矩阵 |

#### 4.1.3 源码精读

**第一步：解析分片索引。** 单文件 checkpoint 走捷径，返回空的 `weight_map`；分片 checkpoint 解析 `index.json` 的 `weight_map`，把每个张量名映射到分片列表下标：

- [pegainfer-core/src/weight_loader.rs:L57-L89](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-core/src/weight_loader.rs#L57-L89) —— `load_shard_info`：先探测 `model.safetensors` 单文件是否存在；否则读 `model.safetensors.index.json`，遍历 `weight_map` 构建「张量名 → 分片下标」。`load_shard_info_fixed`（[L1055 起](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-core/src/weight_loader.rs#L1055)）在其上追加一层文件名修正：索引里写的 `model.safetensors-00001-...` 在磁盘上不存在时，尝试改成 `model-00001-...` 再探一次。
- 查询侧的兜底在 [pegainfer-core/src/weight_loader.rs:L331-L349](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-core/src/weight_loader.rs#L331-L349) —— `find_tensor`：`weight_map` 命中就直接取对应分片；否则（单文件场景 `weight_map` 为空）逐个分片试一遍。这解释了为什么单文件路径可以返回空 map。

**第二、三、四步：预取、mmap、反序列化。** 在模型构造函数里按顺序发生：

- [src/weights.rs:L186-L190](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs#L186-L190) —— `weights.rs` 的调用点：`load_shard_info_fixed` 拿到 `(shard_paths, weight_map)`；`WeightPrefetch::spawn` **只在 `world_size == 1` 时启动**；随后 `mmap_shards` + `deserialize_shards`。
- [pegainfer-core/src/weight_loader.rs:L301-L319](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-core/src/weight_loader.rs#L301-L319) —— `mmap_shards`：对每个分片 `unsafe { Mmap::map(&file) }`。安全性注释写明约定：**Mmap 在整个加载期间保持存活，且文件不会被并发修改**。
- [pegainfer-core/src/weight_loader.rs:L322-L329](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-core/src/weight_loader.rs#L322-L329) —— `deserialize_shards`：在每个 mmap 上 `SafeTensors::deserialize`，得到借用 mmap 字节的视图集合，后续按张量名取 `TensorView` 都是零拷贝。
- [pegainfer-core/src/weight_loader.rs:L91-L100](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-core/src/weight_loader.rs#L91-L100) 与 [L142-L149](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-core/src/weight_loader.rs#L142-L149) —— `WeightPrefetch`：文档注释明说这是 **advisory（建议性）** 预取——loader 从不依赖它；Drop 时取消并 join 工作线程，失败聚合成一条警告。它按 16 MiB 一个 chunk 把文件分给 8 个线程读，还有「预取速率低于 1 GiB/s 就放弃」的自适应门槛（`FLOOR_BYTES_PER_SEC`）。在 [src/weights.rs:L250-L255](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs#L250-L255)，`ctx.sync()` 之后 `drop(prefetch)` —— 权重全部上卡了，预取才功成身退。

**第五步：WeightSource 逐张量装载。** `WeightSource` 是「一次模型加载的张量来源 + 各层 loader 共用的 TP 切分范围」：

- [src/weights/layers.rs:L224-L249](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights/layers.rs#L224-L249) —— `WeightSource` 结构体：持有 `ctx`、`shards`、`weight_map`、`geometry`，以及一组预计算好的 `Shard`（`gated_q`、`q_cols`、`kv_rows`、`intermediate`、`linear_value_heads`、`linear_z`）。注释点明设计意图：**config 与 checkpoint 不符时，要在 loader 处停下，而不是让错误流到一个按 config 尺寸分配的 GEMM 里**。
- [src/weights/layers.rs:L196-L211](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights/layers.rs#L196-L211) —— `Shard` 把「本 rank 的 `(offset, len)`」和「切分前的全局长度」绑成一对，调用方无法拿 A 维度的范围去配 B 维度的全局长度。
- [src/weights/layers.rs:L285-L300](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights/layers.rs#L285-L300) —— `row_shard_if_needed`：TP 分片时走 `load_tensor_2d_row_shard`（带行偏移与全局形状断言），TP1 时退化为整取。
- [pegainfer-core/src/weight_loader.rs:L817-L834](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-core/src/weight_loader.rs#L817-L834) —— `load_tensor_2d_row_shard`：`ensure_global_shape`（对拍 checkpoint 与 config）→ `check_row_range`（切片不越界）→ 在宿主侧切出 `[start..end]` 的 bf16 元素 → `DeviceMatrix::from_host` 一次上传。这就是「先校验后变更」：任何不符都在这一步报错。
- [src/weights/layers.rs:L338-L350](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights/layers.rs#L338-L350) —— `linear_in_proj_qkv`：融合 qkv 不能一刀切。checkpoint 里是 `[全部 q 行 | 全部 k 行 | 全部 v 行]` 三段，本 rank 要从**每段里**各取自己的头部局部行，再拼成 `[q_local | k_local | v_local]`，用 `load_tensor_2d_row_stitch` 完成。为什么不能跨段重排？[L386-L407](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights/layers.rs#L386-L407) 的注释一针见血：**q 行按 key heads 计数、v 行按 value heads 计数，两者的 shard 边界不一样**（gated-q 教训）。单测 [L474-L491](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights/layers.rs#L474-L491) 给了 TP2 下的具体数字：q=k=2048、v=4096、全局 8192 行，rank0 取 `[(0,1024),(2048,1024),(4096,2048)]`，rank1 取 `[(1024,1024),(3072,1024),(6144,2048)]`，两 rank 的段拼起来严丝合缝覆盖 `[0,8192)`。
- [src/weights/layers.rs:L369-L383](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights/layers.rs#L369-L383) 与 [L421-L431](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights/layers.rs#L421-L431) —— `gated_q_proj`：全注意力的 q_proj 每个头带一个输出门，行布局是「每个头先 `head_dim` 行 q、紧跟 `head_dim` 行 gate」。所以 TP 切分按**头块**切而不是平切行区间，保证本 rank 每个头的 q 行与 gate 行相邻。

**装载时的两处融合。** Qwen3.5 的 checkpoint 把线性注意力的四个投影分开存，加载时 fusion 成两个矩阵，注释明说这是对齐 vLLM 的做法、让每组一次 GEMM 而不是四次 launch：

- [src/weights/layers.rs:L153-L168](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights/layers.rs#L153-L168) —— `in_proj_qkv` 与 `in_proj_z` 各自装载后 `DeviceMatrix::vstack` 成 `in_proj_qkvz`；`in_proj_b` 与 `in_proj_a` 同样拼成 `in_proj_ba`（beta 占低行、alpha 占高行，见 [L36-L39](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights/layers.rs#L36-L39) 结构体注释）。
- [src/weights/layers.rs:L88-L99](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights/layers.rs#L88-L99) —— MLP 同理：`gate_proj` 与 `up_proj` 行切后 vstack 成 `gate_up_proj`（SwiGLU 的经典融合），`down_proj` 列切。

#### 4.1.4 代码实践：读索引、走一遍名字定位

**实践目标**：把「张量名 → 分片 → TensorView」这条链在纸面上走通。

**操作步骤**：

1. 打开任一个本地 safetensors checkpoint 目录（没有的话，直接读下面代码也行）：看是否存在 `model.safetensors`（单文件）或 `model.safetensors.index.json` + 多个分片。
2. 若有 `index.json`，用 `jq '.weight_map | to_entries[:5]' model.safetensors.index.json`（或文本编辑器搜索）看前 5 条映射，确认张量名的格式是 `model.language_model.layers.0.*`。
3. 对照 [pegainfer-core/src/weight_loader.rs:L331-L349](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-core/src/weight_loader.rs#L331-L349) 的 `find_tensor` 回答：名字 `model.language_model.layers.0.self_attn.q_proj.weight` 在分片场景下经过几次哈希查找、几次文件访问？（答案：一次 map 查找拿到分片下标，一次 `shards[idx].tensor(name)`。）
4. 再看 [src/weights.rs:L186-L190](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs#L186-L190)，注意 `WeightPrefetch::spawn` 外面套着 `(geometry.world_size() == 1).then(...)`。

**需要观察的现象 / 预期结果**（无 checkpoint 时为纸面推导，标注待本地验证的部分）：

- 单文件 checkpoint：`weight_map` 为空，`find_tensor` 走逐分片扫描的兜底分支（本例只有一个分片，等价于直接取）。
- 分片 checkpoint：每个名字先查 map，O(1) 定位分片。
- 预取线程只在单 GPU 启动；TP 场景下这一步是 `None`。预取失败不会让加载失败，只会打一条警告。`jq` 命令的输出格式：待本地验证（取决于是否有本地 checkpoint）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `mmap_shards` 里的 `Mmap` 对象必须活到所有权重装载完成？如果提前 drop 会怎样？

**答案**：`deserialize_shards` 返回的 `SafeTensors` 视图是**零拷贝借用** mmap 字节的——`TensorView` 指向的就是映射页里的数据。mmap 对象被 drop 后映射解除，后续任何通过视图读数据的操作都会解引用失效映射（未定义行为/崩溃）。代码用生命周期 `SafeTensors<'a>` 让编译器强制这一点，`weights.rs` 中 `mmaps` 与 `shards` 在整个构造函数里同存活域，`ctx.sync()` 之后才一起释放。

**练习 2**：TP=2 时，`in_proj_qkv` 为什么不能像普通行切那样取一个连续区间 `[offset, offset+len)`？

**答案**：因为 checkpoint 布局是 `[q 全部行 | k 全部行 | v 全部行]` 三段拼接，而 q/k 行数按 key heads 计算、v 行数按 value heads 计算，TP 切分后每段里本 rank 的局部起点各不相同。若取一个平切区间，会横跨段边界，把别的 rank 的 q 行混进本 rank 的 v 区。正确做法是每段各取局部行再拼接（`load_tensor_2d_row_stitch` + 三段 range），单测 `linear_qkv_shard_segments_stitch_head_local_slices` 正是验证三段拼起来无缝覆盖全局。

**练习 3**：`gated_q_proj` 的行切分与 `k_proj` 的行切分有什么本质区别？

**答案**：`k_proj` 是平切——全局 `kv_dim` 行均分给各 rank，一个 `(offset, len)` 就够。`gated_q_proj` 的行是「每头 `[q; gate]` 成对相邻」，rank 边界必须落在头边界上，所以 offset 按 `rank × local_heads × head_dim × 2` 计算，保证每个头的 q 行和 gate 行不被拆开。

---

### 4.2 模块二：运行时配置——ModelRuntimeConfig 与构造函数家族

#### 4.2.1 概念说明

权重装载不是纯函数：同一份 checkpoint，根据「要不要 CUDA Graph、跑在哪个 GPU、TP 第几个 rank、给前缀快照留多少显存」会装载出不同的 `Qwen35Model`。这些旋钮打包成 `ModelRuntimeConfig`。本模块还要讲清一个容易被忽略的分离：**调度器准入上限（`decode_admission_batch`）与物理解码容量（`reserved_decode_slots`）不是同一个数**——前者是用户请求的 `--max-batch`，后者向上取整到 CUDA Graph 批次桶。

#### 4.2.2 核心流程

```text
CLI / 启动选项
    │  --max-batch=N（1..=64）
    ▼
from_safetensors_with_runtime_and_capacity(model_path, runtime, max_batch)
    ├─ 校验 max_batch ∈ 1..=MAX_BATCH(64)，否则报错
    ├─ decode_admission_batch = max_batch          ← 保留用户请求
    ├─ max_batch = bucket_for(max_batch)           ← 取整到 {1,2,4,8,16,32,64}
    │    （例：--max-batch 5 → 物理桶 8，准入仍是 5）
    ├─ DeviceContext::new_with_device(runtime.device_ordinal)
    ├─ Config35::from_file + LocalGeometry::try_new   ← u2-l1 / u2-l2 的边界
    ├─ bound_selection_vocab(decodable_vocab)          ← u2-l1 的词表宽度
    ├─ … 权重装载（模块一）…
    └─ 显存预算与 KV 池（模块三）
```

`bucket_for` 的取整规则：从小到大扫 `{1,2,4,8,16,32,64}`，返回第一个 `>= bs` 的桶。

#### 4.2.3 源码精读

**运行时配置结构体：**

- [src/weights.rs:L36-L43](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs#L36-L43) —— `ModelRuntimeConfig`：`enable_cuda_graph`（默认 true）、`tensor_parallel: Option<TensorParallelConfig>`（None 即 TP1）、`device_ordinal`（第几块卡）、`prefix_snapshot_bytes`（每 rank 给完整 recurrent/conv 快照预留的显存字节数）。[L45-L54](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs#L45-L54) 的 `Default` 给出「单卡、开图、零快照预算」的基线。

**构造函数家族（三层收口到一个实现）：**

- [src/weights.rs:L103-L117](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs#L103-L117) —— `from_safetensors_with_options`：公开入口，只填 `enable_cuda_graph`，其余默认。
- [src/weights.rs:L124-L139](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs#L124-L139) —— `from_safetensors`：crate 内部入口，单 GPU 调度路径用（调用点在 [src/lib.rs:L294-L299](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/lib.rs#L294-L299)），填 `device_ordinal` 与 `prefix_snapshot_bytes`。
- [src/weights.rs:L141-L156](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs#L141-L156) —— `from_safetensors_with_runtime_and_capacity`：唯一真实实现。开头两件事：① `ensure!(1..=MAX_BATCH)` 拒绝越界批次；② **`decode_admission_batch = max_batch`（留存请求值）→ `max_batch = bucket_for(max_batch)`（取整到物理桶）**。注释直接引用 #470：`--max-batch 5` 分配桶 8 但最多准入 5 个请求。后续所有缓冲按**物理桶**尺寸分配，只有 `decode_admission_batch` 保留用户请求。

**两个字段的分工**（结构体注释也写明了）：

- [src/weights.rs:L74-L81](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs#L74-L81) —— `reserved_decode_slots`：实际分配的物理解码容量（recurrent 状态槽、decode 缓冲、CUDA Graph 槽），恒为 `BATCH_BUCKETS` 里的值；`decode_admission_batch`：调度器准入上限（`--max-batch`），可以小于物理容量。
- [src/batch_decode_graph.rs:L14-L30](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode_graph.rs#L14-L30) —— 桶的定义：`BATCH_BUCKETS = [1,2,4,8,16,32,64]`、`MAX_BATCH = 64`、`bucket_for` 取最小满足桶。桶是 2 的幂，因为 CUDA Graph 按桶捕获、重放时多出的行填 padding。

**tie_word_embeddings 的两条分支：**

- [src/weights.rs:L208-L215](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs#L208-L215) —— 装载点：`tie_word_embeddings == true` 时 `lm_head` 字段保持 `None`（日志「tied embed_tokens」）；否则加载独立的 `lm_head.weight`（注意名字没有 `model.language_model` 前缀，形状 `[vocab_size, hidden_size]`）。
- [src/weights.rs:L62](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs#L62) 与 [L376-L378](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs#L376-L378) —— `lm_head: Option<DeviceMatrix>`；`output_projection()` 用 `unwrap_or(&self.embed_tokens)` 实现回退：**Tied 分支的 lm_head 就是嵌入矩阵本身**（转置使用：`gemm_rows_into_checked` 把它当 `[vocab, hidden]` 的投影用）。
- [src/weights.rs:L387-L405](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs#L387-L405) —— 补充一个装载期就埋下的约束：`output_logits_into` 是产生 logits 的**唯一**通道——输出投影 GEMM 与 pad 行 \(-\infty\) 抑制必须绑在一起，任何绕过它单独跑 GEMM 的调用点都会让 tile 对齐的 padding 行变得可被采样（详见 u2-l1 的 selection_vocab 与本讲 4.3.3 的 `pad_logit_suppress`）。这也是为什么 `output_projection()` 是私有方法、注释写明只有 GEMM 调参助手直接采样它。

**embed 与 lm_head 都不参与 TP 切分**：[src/weights.rs:L198-L215](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs#L198-L215) 里两者都走整取 `tensor_2d`（全 `vocab_size` 行），每个 rank 持有完整副本；TP 下 hidden 状态经 all-reduce 聚合后，各 rank 本地都能算出完整 logits（采样集中在 rank 0，细节在 u7）。

#### 4.2.4 代码实践：追踪 `--max-batch` 的两个化身

**实践目标**：确认「请求值」与「物理桶」在代码里的分叉点与汇合点。

**操作步骤**：

1. 读 [src/weights.rs:L146-L156](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs#L146-L156)，抄下三行：`ensure!`、`decode_admission_batch = max_batch`、`max_batch = bucket_for(max_batch)`。
2. 手算下面五个输入在两个字段上的取值：`--max-batch` 为 1、5、8、64、65。
3. 对 `--max-batch 65`，沿调用链上游再找一道闸门：[src/lib.rs:L234-L237](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/lib.rs#L234-L237) 的 `start_engine_with_capacity_policy_and_overlap` 也检查了 `1..=MAX_DECODE_BATCH`。思考：`bucket_for(65)` 里的 `panic!`（[src/batch_decode_graph.rs:L26-L29](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode_graph.rs#L26-L29)）实际可达吗？
4. 在 [src/weights.rs:L363-L364](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs#L363-L364) 确认两个字段最终分别存进 `reserved_decode_slots` 与 `decode_admission_batch`。

**需要观察的现象 / 预期结果**（纸面推导即可完成）：

| `--max-batch` | `decode_admission_batch` | `reserved_decode_slots`（物理桶） |
|---|---|---|
| 1 | 1 | 1 |
| 5 | 5 | 8 |
| 8 | 8 | 8 |
| 64 | 64 | 64 |
| 65 | 启动即报错（`ensure!` 拒绝，不进 loader） | — |

`bucket_for` 的 `panic!` 理论上不可达：构造函数入口的 `ensure!` 已把 `>64` 挡在门外，`bucket_for` 的 panic 是对内部调用者的防御性断言。

#### 4.2.5 小练习与答案

**练习 1**：`from_safetensors_with_options`、`from_safetensors`、`from_safetensors_with_runtime_and_capacity` 三个函数是什么关系？为什么要这样设计？

**答案**：前两个都是薄包装，按调用方身份填 `ModelRuntimeConfig` 的不同子集（前者是外部用户只关心 CUDA Graph；后者是 crate 内单 GPU 启动路径，关心设备序号与快照预算），最终都收口到 `from_safetensors_with_runtime_and_capacity` 这个唯一实现。收益：装载主流程只有一份代码，校验与预算逻辑不会出现分叉副本；调用方只暴露自己需要的参数面。

**练习 2**：`--max-batch 5` 时分配了桶 8，多出的 3 个槽位在解码时怎么处理？浪费在哪里、换来了什么？

**答案**：CUDA Graph 按 64/32/…/1 的固定桶捕获，重放时槽 `i` 与批位置 `i` 一一对应（u4-l3 会展开）。多出的 3 行以 padding 行参与同一次图重放——内核照算但结果被丢弃。代价是这 3 行的白算与相应 recurrent 状态槽的显存；换来的是图数量有限（7 个桶而非 64 种批次）、指针稳定、免于按任意批次重新捕获。准入上限 5 保证调度器不会让第 6 个请求进批，语义上用户得到的就是 5。

**练习 3**：Tied 与 Untied 两种 checkpoint 下，产生下一个 token logits 的矩阵分别从哪来？

**答案**：都经 `output_logits_into` → `output_projection()`。Untied：装载期加载的独立 `lm_head.weight`（`[vocab_size, hidden_size]`，名字无 `model.language_model` 前缀）。Tied：`lm_head` 为 `None`，`unwrap_or(&self.embed_tokens)` 回退到嵌入矩阵——嵌入查表（行索引）与输出投影（GEMM）共用同一份 `[vocab, hidden]` 权重，省掉一份与词表等宽的大矩阵显存。

---

### 4.3 模块三：显存预算——四笔预留与 prefix_snapshot_bytes → 槽数

#### 4.3.1 概念说明

权重上卡后，加载器要回答：**剩下的显存够不够跑？够的话怎么分？** Qwen3.5 的答案是一段顺序固定的预算计算，消费方有四个，优先级从高到低：

1. **预填充 scratch**（GDR 分块内核的工作区）——不预留，长 prompt 预填充时会 OOM；
2. **recurrent 状态预留**——每个解码槽 2 份完整状态（图槽 + 一个在途 prefill 瞬态）；
3. **前缀快照槽**——由 `prefix_snapshot_bytes` 预算换算出的完整快照物理槽；
4. **KV 池**——拿剩余量的 85%，且不低于 64 页下限。

「先扣除会 OOM 的，再分 KV」这个顺序本身就是注释里写明的正确性约定。任何一项不满足都在启动时 fail-fast，而不是服务中途崩溃。

#### 4.3.2 核心流程

预算计算的流程（全部发生在权重装载之后、KV 池分配之前）：

```text
free_bytes = cuMemGetInfo()                        ← 权重上卡后的真实剩余
scratch_reserve    = GdrChunkwiseScratch35::estimate_bytes(config, geometry, 20_000)
recurrent_reserve  = 2 × 物理桶 × bytes_per_request(config, geometry)
snapshot_slots     = prefix_snapshot_bytes / bytes_per_request        ← 整数除法向下取整
snapshot_reserve   = snapshot_slots × bytes_per_request
门 1: prefix_snapshot_bytes > 0 时必须 snapshot_slots ≥ 1，否则拒绝启动
门 2: free ≥ scratch + recurrent + snapshot + 64 页最小 KV，否则拒绝启动
available = free − scratch − recurrent − snapshot
kv_budget = available × 0.85                       ← 留 15% 余量
num_pages  = max(kv_budget / bytes_per_page, 64)
```

快照槽数换算的字节来源（分母）：

\[
\text{bytes\_per\_request} = L_{\text{linear}} \times \big( \underbrace{H_v \times D_k \times D_v \times 4}_{\text{f32 递推状态}} + \underbrace{D_{qkv} \times (k_{\text{conv}} - 1) \times 2}_{\text{bf16 conv 状态}} \big)
\]

其中 \(L_{\text{linear}}\) 是线性注意力层数，\(H_v\) 是本 rank 的 value 头数，\(D_k = D_v = 128\)，\(D_{qkv}\) 是本 rank 融合 qkv 维宽，\(k_{\text{conv}}\) 是卷积核宽（Qwen3.5 为 4，所以 conv 状态存 3 个历史输入）。

#### 4.3.3 源码精读

**预算主体：**

- [src/weights.rs:L276-L299](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs#L276-L299) —— 逐段精读：
  - `bytes_per_page = layout.page_stride × sizeof(bf16)`：一页 KV 的字节数（`KvLayout` 按 8 个全注意力层 × 本 rank KV 头 × head_dim × page_size=16 组织，见 [L261-L275](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs#L261-L275)）；
  - `cuMemGetInfo` 拿真实剩余显存（[L277-L278](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs#L277-L278)）；
  - 注释「**Reserve space for prefill scratch … before allocating KV pool, so prefill doesn't OOM**」——预留顺序的正确性约定（[L279-L286](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs#L279-L286)）：scratch 按 `SCRATCH_ESTIMATE_SEQ = 20_000` token 估计（[src/prefill.rs:L12](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefill.rs#L12)）；
  - `recurrent_reserve = STATES_PER_DECODE_SLOT × max_batch × bytes_per_request`，其中 `STATES_PER_DECODE_SLOT = 2`（[src/weights.rs:L96-L99](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs#L96-L99)：一个图槽状态 + 每解码槽一个在途 prefill 瞬态；`MIN_KV_PAGES = 64` 也在这里）；注意此处的 `max_batch` 已是**物理桶**值；
  - **快照槽数换算**（[L290-L299](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs#L290-L299)）：`snapshot_bytes_per_slot = bytes_per_request(config, geometry)`——**一个快照槽就是一份完整的单请求 recurrent+conv 状态**；`snapshot_slots = prefix_snapshot_bytes / snapshot_bytes_per_slot`（整数除法）；门 1 要求非零预算下至少换得出 1 个槽，报错文案直接给出预算 MiB 与单槽 MiB，方便排查。
- [src/weights.rs:L300-L326](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs#L300-L326) —— 门 2 与分配：`available = free − 三笔预留`，`kv_budget = available × 0.85`（留 15% 运行余量），`num_pages = max(kv_budget / bytes_per_page, MIN_KV_PAGES)`；info 日志把页数、四笔 MB、占比一次打全——这份日志就是排查「KV 池为什么这么小」的第一手材料。
- [src/weights.rs:L327-L334](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs#L327-L334) —— `KvBuffer::new` 按算出的页数分配分页 KV 池；`prefix_snapshot_slots` 存进结构体（[L362](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs#L362)），后续前缀缓存模块（u6）按它建物理槽。

**分母的定义：**

- [src/recurrent_state.rs:L53-L62](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/recurrent_state.rs#L53-L62) —— `per_layer_dims`：f32 状态元素数 = 本 rank value 头数 × key_head_dim(128) × value_head_dim(128)；bf16 conv 元素数 = 本 rank 融合 qkv 维 × (卷积核宽 − 1)。
- [src/recurrent_state.rs:L229-L235](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/recurrent_state.rs#L229-L235) —— `bytes_per_request`：层数 × (状态元素 × 4B + conv 元素 × 2B)。模块头注释（[L1-L9](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/recurrent_state.rs#L1-L9)）说明布局：状态 [H,K,V] 连续、f32；conv 状态存最近 `kernel−1` 个输入、bf16；TP 下按 value 头分片、**从不 all-reduce**。
- [src/recurrent_state.rs:L245-L251](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/recurrent_state.rs#L245-L251) —— 单测 `qwen35_4b_recurrent_allocation_is_49_125_mib` 把 4B 的数字钉死：\(24 \times (32 \times 128 \times 128 \times 4 + 8192 \times 3 \times 2) = 51{,}511{,}296\ \text{B} = 49.125\ \text{MiB}\)。这是纯算术测试，不需要 GPU。

**预算从 CLI 一路传下来的链路：**

- [src/lib.rs:L128-L133](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/lib.rs#L128-L133) 与 [L250-L252](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/lib.rs#L250-L252) —— `Qwen35LaunchOptions.prefix_cache_mib`（对应 CLI 旗标 `--qwen35-prefix-cache-mib`，见 u1-l4）经 `checked_mul(1024*1024)` 换算成字节并防溢出，得到 `prefix_snapshot_bytes`。
- 之后单 GPU 路径把它随 `from_safetensors` 传入（[src/lib.rs:L294-L299](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/lib.rs#L294-L299)），TP 路径把它随 `ModelRuntimeConfig` 传给每个 rank 的模型构造（[src/tp_executor.rs:L703-L712](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/tp_executor.rs#L703-L712)）——预算是**每 rank** 的（字段注释：Per-rank GPU budget）。

**收尾：pad 行抑制也在装载期备好。**

- [src/weights.rs:L337-L348](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs#L337-L348) —— 当 `selection_vocab > decodable_vocab`（u2-l1 的 tile 对齐 padding），把 \([decodable, selection)\) 区间的行 id 打包成 `SuppressIds` 上传；运行时 `output_logits_into` 在 GEMM 后把这些行写成 \(-\infty\)（[L387-L405](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs#L387-L405)）。无 padding 时为 `None`，零开销。

#### 4.3.4 代码实践：手算 4B 的快照槽数并核对单测

**实践目标**：把「MiB 预算 → 槽数」的换算亲手算一遍，并与仓库钉死的常数对上。

**操作步骤**：

1. 用 Qwen3.5-4B 单 GPU 的数字代入 `bytes_per_request`：线性层数 24、value 头 32、\(D_k = D_v = 128\)、融合 qkv 维 8192（\(16 \times 128 + 16 \times 128 + 32 \times 128\)）、卷积核宽 4。
2. 算出单槽数后再算三个预算的槽数：`--qwen35-prefix-cache-mib` 取 512、1024、16。
3. 打开 [src/recurrent_state.rs:L245-L251](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/recurrent_state.rs#L245-L251)，核对你的单槽数与单测断言的 49.125 MiB。
4. （可选，需可构建环境）运行该纯算术单测：`cargo test --release -p pegainfer-qwen35 --features qwen35 --lib recurrent_allocation`。它不需要真实 GPU，但编译本 crate 需要 CUDA 工具链；输出待本地验证。

**需要观察的现象 / 预期结果**：

- 单槽：\(24 \times (524288 \times 4 + 24576 \times 2) = 24 \times 2{,}146{,}304 = 51{,}511{,}296\) B \(= 49.125\) MiB。
- 512 MiB = 536,870,912 B → \(536870912 / 51511296 = 10.42\) → **10 个槽**（整数除法向下取整），实际占用 \(10 \times 51511296 \approx 491.25\) MiB，零头 21.7 MiB 不建槽。
- 1024 MiB → 20 个槽。
- 16 MiB → 0 个槽 → **门 1 触发，启动被拒**，报错文案会写明「预算 16 MiB，但一个快照需要 49.125 MiB」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 scratch 预留要放在 KV 池分配**之前**？如果反过来会发生什么？

**答案**：KV 池按「剩余显存的 85%」贪心分配，几乎总能吃掉大头。若先分 KV，剩下的显存可能不够一次长 prompt 的 GDR 分块预填充工作区，服务会在运行中期预填充时 OOM 崩溃。先扣除 scratch（按 `SCRATCH_ESTIMATE_SEQ = 20_000` token 估算）再分 KV，把「启动时显式报错」替换掉「服务中途隐性崩溃」——fail-fast 原则在显存预算上的落点。

**练习 2**：`STATES_PER_DECODE_SLOT = 2` 里的第二份状态是给谁的？

**答案**：一份是解码槽自身的图槽状态（CUDA Graph 重放时 slot `i` 读写固定地址的那份），另一份是该解码槽在途 prefill 的瞬态状态——一个正在预填充的请求在分块推进期间需要一份独立工作状态，完成后才搬进图槽。预留按「每解码槽 2 份」计算，保证任何组合都不超额。

**练习 3**：KV 池预算为什么取 `available × 0.85` 而不是 100%？

**答案**：`cuMemGetInfo` 只报告当前空闲字节，运行期还有装载期未统计到的零散分配（CUDA Graph 捕获的工作区、NCCL 缓冲、驱动内部结构等）。留 15% 余量是对这些「看不见的消费者」的工程缓冲；同时 `MIN_KV_PAGES = 64` 保证极小显存下池子仍能开张（也是门 2 的 fail-fast 阈值的一部分）。

## 5. 综合实践：权重名 → 结构体字段 → TP 切分方式映射表

这是本讲规格指定的核心实践：在 `from_safetensors_with_runtime_and_capacity` 的装载代码里定位**每一条权重名到结构体字段的映射**，整理成表，并标注 TP 下哪些权重被切分、怎么切。

**实践目标**：产出一 cheat-sheet 表，给定任意 `index.json` 里的张量名，能立刻说出它落到哪个字段、是否切分、走哪条装载路径。

**操作步骤**：

1. 从 [src/weights.rs:L194](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs#L194) 记住权重前缀 `wp = "model.language_model"`（HF 的多模态命名，文本权重都在它下面）。
2. 逐个读三处装载代码：顶层（embed/lm_head/final norm，[L197-L234](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs#L197-L234)）、`TransformerBlock35::load` 与两个注意力层 loader（[src/weights/layers.rs:L114-L189](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights/layers.rs#L114-L189)）、`MLP35::load`（[L87-L99](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights/layers.rs#L87-L99)）。
3. 对每个名字记四列：**checkpoint 名**（注意 `lm_head` 无 `wp` 前缀、线性层子前缀是 `linear_attn` 而全注意力是 `self_attn`）、**目标字段**、**TP 切分**（整取 / 行切 / 列切 / 三段拼接 / 1D 分片）、**装载后是否被融合**。
4. 用 `grep -o '"[a-z_.]*\.weight"' src/weights/layers.rs src/weights.rs` 自查有没有漏掉的名字。

**参考答案**（节选，请自行补全到全部名字）：

| checkpoint 权重名（`{wp}` = `model.language_model`） | 目标字段 | TP 切分 | 装载后处理 |
|---|---|---|---|
| `{wp}.embed_tokens.weight` | `embed_tokens` | 整取（每 rank 全量） | — |
| `lm_head.weight`（顶层，无前缀） | `lm_head`（仅 untied） | 整取 | tied 时为 `None`，回退 embed |
| `{wp}.layers.{i}.input_layernorm.weight` / `post_attention_layernorm.weight` | 同名字段 | 整取 1D | — |
| `{wp}.layers.{i}.self_attn.q_proj.weight` | `FullAttentionLayer::q_proj` | **行切·按头块**（`gated_q`，保持每头 [q,gate] 相邻） | — |
| `{wp}.layers.{i}.self_attn.k_proj.weight` / `v_proj.weight` | `k_proj` / `v_proj` | **行切·平切**（`kv_rows`） | — |
| `{wp}.layers.{i}.self_attn.o_proj.weight` | `o_proj` | **列切**（`q_cols`） | — |
| `{wp}.layers.{i}.self_attn.q_norm.weight` / `k_norm.weight` | `q_norm` / `k_norm` | 整取 1D | — |
| `{wp}.layers.{i}.linear_attn.in_proj_qkv.weight` | `LinearAttentionLayer::in_proj_qkvz` 的低段 | **三段拼接**（q/k/v 段各取局部行） | 与 `in_proj_z` vstack 融合 |
| `{wp}.layers.{i}.linear_attn.in_proj_z.weight` | `in_proj_qkvz` 的高段 | **行切**（`linear_z`） | 同上 |
| `{wp}.layers.{i}.linear_attn.in_proj_b.weight` / `in_proj_a.weight` | `in_proj_ba` | **行切**（`linear_value_heads`，按头） | 两者 vstack 融合（b 低 a 高） |
| `{wp}.layers.{i}.linear_attn.conv1d.weight` | `conv1d_weight` | **1D 三段拼接**（qkv 段 × 核宽） | 通道布局镜像融合 qkv 行 |
| `{wp}.layers.{i}.linear_attn.dt_bias` | `dt_bias` | **1D 行分片**（`linear_value_heads`） | — |
| `{wp}.layers.{i}.linear_attn.A_log` | `a_log` | **1D 行分片** | bf16 → **f32 加宽**（`tensor_1d_f32_shard`） |
| `{wp}.layers.{i}.linear_attn.norm.weight` | `norm_weight` | **整取**（头共享，每 rank 复制） | bf16 → f32 |
| `{wp}.layers.{i}.linear_attn.out_proj.weight` | `out_proj` | **列切**（`linear_z`） | — |
| `{wp}.layers.{i}.mlp.gate_proj.weight` / `up_proj.weight` | `mlp.gate_up_proj` | **行切**（`intermediate`） | 两者 vstack 融合 |
| `{wp}.layers.{i}.mlp.down_proj.weight` | `mlp.down_proj` | **列切**（`intermediate`） | — |
| `{wp}.norm.weight` | `norm`（最终 norm） | 整取 1D | — |

验证方法：表中每个「切分」结论都应能在 [src/weights/layers.rs:L136-L189](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights/layers.rs#L136-L189) 与 `WeightSource` 的方法（[L285-L383](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights/layers.rs#L285-L383)）里找到一一对应的调用。**待本地验证**：若你手上有本地 checkpoint 的 `index.json`，用步骤 4 的 grep 结果与真实名字清单对拍，确认无遗漏。

## 6. 本讲小结

- 加载管线五步：分片索引（`load_shard_info_fixed`）→ advisory 预取（仅单 GPU）→ mmap → `SafeTensors` 零拷贝反序列化 → `WeightSource` 逐张量装载；四种装载路径（整取 / 行切 / 列切 / 多段拼接）覆盖全部权重。
- 装载即校验：每个分片装载都先对拍 checkpoint 与 config 的全局形状再切片，配置与权重不符在 loader 处 fail-fast，绝不流到 GEMM。
- 装载期融合：`in_proj_qkvz`（qkv 三段拼接 + z）、`in_proj_ba`、`gate_up_proj` 都在加载时 vstack 成形，把多次小 GEMM 合成一次；gated q_proj 的行切按头块对齐，保持每头 [q, gate] 相邻。
- `ModelRuntimeConfig` 携带四个运行时旋钮；`decode_admission_batch`（用户 `--max-batch`）与 `reserved_decode_slots`（`bucket_for` 取整的物理桶）分离是 #470 的结果——按物理桶分配缓冲、按请求值控制准入。
- `tie_word_embeddings` 两分支：tied 时 `lm_head = None` 且 `output_projection()` 回退 `embed_tokens`；untied 时加载顶层 `lm_head.weight`；两者都不参与 TP 切分。
- 显存预算四笔预留有固定顺序（prefill scratch → recurrent 状态 → 前缀快照 → KV 池 85%），`snapshot_slots = prefix_snapshot_bytes / bytes_per_request` 向下取整，非零预算换不出一个槽（4B 单槽 49.125 MiB）即拒绝启动。

## 7. 下一步学习建议

本讲完成后，`Qwen35Model` 已在 GPU 上组装完毕，但每个 `TransformerBlock35` 内部的前向还没展开。下一讲 **u2-l4「层结构：TransformerBlock35 与两种注意力层」**正好接上：`LayerKind` 如何按 `config.layer_types[i]` 分发、两种注意力层各自持有本讲已装载的哪些权重与几何维度。之后再进入 u3 单元深读两种层的前向与内核。建议顺带重读 [src/weights.rs:L322-L326](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs#L322-L326) 的启动日志格式——以后每次拉起服务，这行日志就是检查显存预算是否合理的第一入口。
