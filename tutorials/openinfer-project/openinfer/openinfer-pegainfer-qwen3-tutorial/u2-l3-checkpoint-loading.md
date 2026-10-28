# checkpoint 装载：先记录计划，再上传

## 1. 本讲目标

上一讲（u2-l2）我们看清了权重在 GPU 上**长什么样**：融合的 `qkv_proj`、`gate_up_proj`，只装权重与配置的 `Qwen3Model`。本讲回答下一个自然的问题：**这几个 GB 的矩阵是怎么从磁盘上的 safetensors 文件走进显存的？** 学完本讲，你应该能够：

1. 解释 safetensors 分片读取的三个步骤（`load_shard_info` 探测清单 → `mmap_shards` 内存映射 → `deserialize_shards` 零拷贝视图）与"先记录整个计划、再统一上传"两阶段装载的动机——**每个张量都校验通过之前，一个字节都不上传**。
2. 推导 TP>1 时投影矩阵的切片边界：行切（q/k/v、gate/up 经 `shard_range` + `FusedPart`）与列切（`o_proj`/`down_proj` 经 `col_shard`）各自切在哪一维、为什么。
3. 理解 [src/sizing.rs](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/sizing.rs) 的 checked 乘加为何必要：**预算在配置维度通过张量校验之前就要计算**，坏配置必须在这里响亮失败，而不是回绕成一个"少预留"的小数字。

> 范围说明：Qwen3 的装载入口 [src/weights/load.rs](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/weights/load.rs) 是"用户"，真正的装载机制住在 workspace 的 `pegainfer-core` 里（[pegainfer-core/src/weight_loader.rs](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-core/src/weight_loader.rs) 及其 [staging.rs](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-core/src/weight_loader/staging.rs) 子模块）。本讲两边都读，永久链接同样指向当前 HEAD。

## 2. 前置知识

- **safetensors 格式**：HuggingFace 的权重容器。文件 = 开头一段 JSON header（张量名 → dtype、shape、字节偏移）+ 紧随其后的原始数据字节。解析 header 后即可拿到指向数据段的**零拷贝视图**（`TensorView`），不需要反序列化整个文件。大模型通常切成多个分片（shard），外加一个 `model.safetensors.index.json` 索引记录"哪个张量在哪个分片"。
- **mmap（内存映射文件）**：把文件按页映射进进程地址空间，读它就是读文件，内核按需把页从磁盘调入 **page cache**（页缓存）。对多 GB 的权重文件，mmap 避免了"先整读进 Vec"的双倍内存。
- **pinned memory（页锁定内存）与 DMA**：普通内存可能被内核换页搬走，GPU 的 DMA 引擎没法安全地直接读它；`clone_htod` 这类分页拷贝要经内核中转，与源读取**串行**。pinned（页锁定）内存不会被搬走，GPU 可以直接从它异步 DMA。装载器用两块 32 MiB 的 pinned 缓冲**双缓冲**：填 A 的同时 B 上的拷贝在飞，重叠"读源"与"上传"两段时间。
- **行切与列切（TP 术语）**：线性层权重 \( W \in \mathbb{R}^{\text{out} \times \text{in}} \)（行 = 输出维，列 = 输入维，u2-l2 已建立）。**行并行**（切输出维）让每个 rank 只算自己那部分输出的部分和；**列并行**（切输入维）要求输入已经被复制到各 rank，但输出天然是"各 rank 一份局部"。一次 transformer 块里两种切法成对交替，使每个 rank 局部自洽、只在必要处归约。
- **`usize` 溢出**：Rust 在 debug 构建里整数溢出会 panic，但 **release 构建默认静默回绕**（wrap around）。回绕出来的"小字节数"如果被当作显存预算，后果是少预留而不是报错——这正是 sizing.rs 要防的事。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [src/weights/load.rs](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/weights/load.rs) | Qwen3 侧装载入口：`ModelRuntimeConfig`、`from_safetensors_with_runtime` 全流程；模块注释一句话点题 "record the whole plan, then upload" |
| [pegainfer-core/src/weight_loader.rs](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-core/src/weight_loader.rs) | 装载机制本体：`load_shard_info`（分片清单）、`WeightPrefetch`（咨询式预取）、`StagedWeightLoader`（两阶段装载器）、`FusedPart`（融合切片描述）、`SlotId`/`VecSlotId`（兑换凭证） |
| [pegainfer-core/src/weight_loader/staging.rs](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-core/src/weight_loader/staging.rs) | pinned 双缓冲 `WeightStager` 与 `ColShardPlan`；`prepare`/`prepare_cols` 在记录时验证目标区间 |
| [src/config.rs](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/config.rs) | `TensorParallelConfig`（L12-L25）、`validate_for` 的整除闸门（L849-L883）、`shard_range` 切片公式（L885-L888）、`local_*` 本地维度（L346-L364） |
| [src/sizing.rs](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/sizing.rs) | 全部 23 行：`product` / `sum` 两个 checked 乘加函数 |
| [src/executor.rs](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/executor.rs) | 调用点：单卡装载（L1318-L1330）、TP 逐 rank 装载 + 共享一份预取（L1398-L1418）、装载后用 sizing 算 DFlash/hedge 预留（L1331-L1380） |

## 4. 核心概念与源码讲解

### 4.1 分片清单：load_shard_info 与 mmap/零拷贝视图

#### 4.1.1 概念说明

一个 HuggingFace 模型目录里，权重有两种摆法：

1. **单文件**：只有 `model.safetensors`，所有张量都在里面；
2. **分片**：`model-00001-of-0000N.safetensors` … 若干片，加一个 `model.safetensors.index.json`，其中的 `weight_map` 记录 `张量名 → 分片文件名`。

`load_shard_info` 做的第一件事就是把这两种摆法**归一化成同一个数据结构**：一组分片文件路径 + 一张 `张量名 → 分片下标` 的表。后续所有取张量的代码都不用再关心"单文件还是分片"。

#### 4.1.2 核心流程

1. 若 `{model_path}/model.safetensors` 存在 → 返回 `(单文件路径, 空 HashMap)`；空表是合法的，因为查找函数有回退逻辑（见下）。
2. 否则读 `model.safetensors.index.json`，遍历 `weight_map`：每个分片文件名第一次出现时分配一个递增下标并 push 进 `shard_files`（`file_to_idx` 去重），张量名映射到该下标。
3. 拿到清单后：`mmap_shards` 把每个文件 mmap 进地址空间（打日志报告片数与总 MB）；`deserialize_shards` 对每个 mmap 调 `SafeTensors::deserialize`——这一步只解析 header，得到的是借用了 mmap 字节的 `TensorView` 列表，**不复制任何权重数据**。
4. 取张量：`find_tensor` 先查 `weight_map` 直接定位分片；查不到（单文件情形，表为空）就对所有分片线性扫描，谁有就返回谁。

#### 4.1.3 源码精读

**清单探测**——[pegainfer-core/src/weight_loader.rs:L57-L89](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-core/src/weight_loader.rs#L57-L89)：单文件分支在 L59-61（空 HashMap），分片分支在 L63-88。注意它只读 JSON 与做路径拼接，**不打开任何张量数据**——数据打开是 mmap 阶段的事。

**mmap 与反序列化**——[pegainfer-core/src/weight_loader.rs:L301-L329](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-core/src/weight_loader.rs#L301-L329)：`mmap_shards` 的 SAFETY 注释写明前提（文件在装载期间不被并发修改）；`deserialize_shards` 把每个 mmap 变成 `SafeTensors` 视图。文档注释里的链式用法正是 load.rs 的真实写法。

**按名取张量**——[pegainfer-core/src/weight_loader.rs:L331-L349](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-core/src/weight_loader.rs#L331-L349)：先查表、后线性扫描的回退让"单文件的空表"无需特判。

**对齐陷阱**——[pegainfer-core/src/weight_loader.rs:L369-L394](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-core/src/weight_loader.rs#L369-L394)：safetensors **允许**张量数据不对齐，而未对齐的 bf16 切片在 Rust 里是 UB。`tensor_bf16_cow` 检查负载地址是否对齐 `bf16`：对齐则零拷贝借用，不对齐则逐元素小端解码进 owned 缓冲。该文件尾部的三个单测（[L1099-L1168](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-core/src/weight_loader.rs#L1099-L1168)）专门钉住这个行为——本讲综合实践会跑到它们。

**Qwen3 的调用点**——[src/weights/load.rs:L127-L132](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/weights/load.rs#L127-L132)：`load_shard_info` → （条件）预取 → `mmap_shards` → `deserialize_shards`，与上面的流程一一对应。顺带一提，core 里还有个 [load_shard_info_fixed（L1055-L1087）](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-core/src/weight_loader.rs#L1055-L1087) 修 Qwen3.5 索引文件名与实际文件名不符的问题，Qwen3 自己用的是普通版。

#### 4.1.4 代码实践

**实践目标**：亲眼确认本地模型目录属于哪种摆法，并读懂它的清单。

1. 操作步骤：对本地 `models/Qwen3-4B` 执行 `ls`，看是单文件还是 `*.index.json` + 分片；若有 index，用 `python3 -c "import json; m=json.load(open('...'))['weight_map']; print(len(m), sorted(set(m.values())))"` 数出张量总数与分片文件集合。
2. 需要观察的现象：`model.layers.0.self_attn.q_proj.weight` 这类名字在 `weight_map` 里各指向一个分片；分片大小通常是几百 MB 到几 GB。
3. 预期结果：清单条目数 ≈ 层数 × 每层张量数 + 嵌入/norm 等全局张量。无本地权重时此实践改为纸面推演，运行结果**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：单文件情形下 `weight_map` 是空的，为什么后面取张量不出错？

答案：`find_tensor` 查表失败后进入回退分支，对所有分片（此时只有一个）逐个尝试 `shard.tensor(name)`（[pegainfer-core/src/weight_loader.rs:L340-L347](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-core/src/weight_loader.rs#L340-L347)）。空表 + 单文件 = 线性扫描天然命中。

**练习 2**：为什么 `deserialize_shards` 之后内存里没有出现一份几 GB 的权重拷贝？

答案：`SafeTensors::deserialize` 只解析文件头的 JSON，返回的 `TensorView` 借用 mmap 的字节切片（零拷贝）。真正被复制的只有后续 staged 上传时装进 pinned 缓冲的 32 MiB 块，以及对齐失败时的小张量 owned 缓冲。

**练习 3**：`load_shard_info` 为什么要返回 `(shard_paths, weight_map)` 两个东西，而不是直接返回打开好的张量视图？

答案：分片清单还被第二个消费者用：`WeightPrefetch::spawn(&shard_paths)` 只需要文件路径就能开始预取（4.2）；mmap/视图的建立则在预取已经开始之后才进行。清单与数据分离让预取和装载并行起步。

### 4.2 咨询式预取：WeightPrefetch

#### 4.2.1 概念说明

mmap 的缺点是**第一次**读每一页都要缺页中断、从磁盘调入。如果装载器一边顺序上传、内核一边按需取页，上传线程就会被磁盘抖动拖住。`WeightPrefetch` 用 8 个线程把整个 checkpoint 提前读进 page cache，让后续 mmap 读全部命中内存。

它的设计信条写在文档注释里：**advisory（咨询式）——装载器从不依赖它**。预取失败（文件打不开、读错、线程起不来）绝不报错中断启动，只在 Drop 时聚合统计打一条 warning。它是性能优化，不是正确性依赖。

#### 4.2.2 核心流程

1. `spawn(shard_paths)`：把每个文件按 16 MiB 切成 chunk 列表（[L143](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-core/src/weight_loader.rs#L143) `CHUNK = 16 << 20`），起 `min(8, chunk 数)` 个 worker。
2. 每个 worker 从共享游标 `fetch_add` 领 chunk：先用 `chunk_looks_resident` 探测该 chunk 首页是否已在 page cache——用 `preadv2` + `RWF_NOWAIT` 读 4096 字节，内核需要取盘时就返回 `EAGAIN`，一页代表整个 chunk；已驻留则跳过（"A resident checkpoint reads no chunk at all"，L193-194）。
3. 未驻留则 `read_exact_at` 真读 16 MiB 进缓冲（读完即丢，目的只是把页调入 cache）。
4. **自觉退场控制器**：累计读满 512 MiB 时测一次速率，若源设备低于 1 GB/s（[L146-L149](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-core/src/weight_loader.rs#L146-L149) 的两个常量），置 cancel——"prefetch 跟装载抢一个慢设备只会互相拖累，不如把设备让给装载路径"。
5. `Drop`：置 cancel、join 所有 worker、聚合"打不开的片数 / 起线程失败数 / chunk 读错误数 / panic 数 + 首个错误"成一条 warning。

#### 4.2.3 源码精读

**驻留探测**——[pegainfer-core/src/weight_loader.rs:L120-L139](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-core/src/weight_loader.rs#L120-L139)：`RWF_NOWAIT` 的语义注释写得很清楚——内核需要取页就 `EAGAIN`，任何错误都算 miss，`got > 0` 才算命中。

**worker 主循环与退场**——[pegainfer-core/src/weight_loader.rs:L185-L232](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-core/src/weight_loader.rs#L185-L232)：领号 → 探测 → 读；速率探测在 L212-223，`done >= PROBE_BYTES` 那一次跨越点测速并决定 cancel。

**Drop 聚合**——[pegainfer-core/src/weight_loader.rs:L260-L292](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-core/src/weight_loader.rs#L260-L292)：四类失败计数相加大于零才打 warning，且保留首个错误详情。注意 `spawn` 本身**永不失败**（返回 `Self` 而非 `Result`）——这是"咨询式"的类型学体现。

**谁在什么条件下spawn**——[src/weights/load.rs:L129-L130](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/weights/load.rs#L129-L130)：`(tensor_parallel.world_size == 1).then(|| WeightPrefetch::spawn(&shard_paths))`——只有单卡在装载函数内部起预取。TP 路径并非没有预取：executor 在**逐 rank 装载循环外**起一份共享预取（[src/executor.rs:L1398-L1418](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/executor.rs#L1398-L1418)，注释"TP ranks load sequentially and suppress per-rank prefetch, so keep one whole-checkpoint prefetch alive across the loop"）——N 个 rank 顺序装、读的是同一批文件，一份预取就够，循环结束 `drop(prefetch)`。

**收尾时机**——[src/weights/load.rs:L296-L297](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/weights/load.rs#L296-L297)：`drop(loader)` 之后、打"GPU model loaded"日志之前 `drop(prefetch)`——所有上传落定时预取的使命已尽，cancel 语义让它立即收工。

#### 4.2.4 代码实践

**实践目标**：纸面推演预取的 chunk 划分。

1. 操作步骤：设某模型 4 个分片各 3.5 GiB。按 `CHUNK = 16 MiB` 计算总 chunk 数（每片 `ceil(3.5×1024/16)`），再算 worker 线程数（`min(8, chunks)`）；对照 [L143-L149](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-core/src/weight_loader.rs#L143-L149) 的常量核对。
2. 需要观察的现象：chunk 数远大于 8，故线程数恒为 8；若第二次启动同一模型（页已驻留），每个 chunk 的 `chunk_looks_resident` 都命中，实际读字节数为零。
3. 预期结果：每片 224 个 chunk、共 896 个 chunk、8 线程。启动日志应出现 "Prefetching 4 weight shard(s) (14.0 GB) on 8 threads"（[L242-L248](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-core/src/weight_loader.rs#L242-L248)）——**待本地验证**（需本地模型与 GPU 环境）。

#### 4.2.5 小练习与答案

**练习 1**：为什么用 `RWF_NOWAIT` 读一页来判断整个 16 MiB chunk 是否驻留，而不是直接读整个 chunk？

答案：探测的目的是"要不要读"，本身要几乎零成本。`RWF_NOWAIT` 在内核需要磁盘 I/O 时立即返回 `EAGAIN`，不阻塞；用一页代表整片是因为预取按 16 MiB 顺序读入，页驻留高度局部化（注释 L120-121："One page stands for the chunk"）。真读 16 MiB 来探测就把探测变成了预取本身。

**练习 2**：预取读出来的数据丢掉了（缓冲复用、读完即弃），那它图什么？

答案：图 page cache。`read` 系统调用会把页调入内核页缓存，之后 mmap 视图碰这些页时全部内存命中，缺页中断消失。预取线程与装载线程因此能流水起来。

**练习 3**：TP=8 时若每个 rank 都在自己的装载函数里 spawn 一份预取，会发生什么？

答案：8 份 × 8 线程 = 64 个线程对同一批文件做重复读，page cache 与磁盘带宽白白内耗。所以代码把内部预取 gate 在 `world_size == 1`（load.rs L129-130），TP 由 executor 在循环外起**一份**共享预取（executor.rs L1400-1403 的注释正是这个理由）。

### 4.3 先记录计划，再上传：StagedWeightLoader 两阶段与 ModelRuntimeConfig

#### 4.3.1 概念说明

naive 装载是"逐张量：找到 → 校验 → 分配 → 上传 → 存进模型字段"。它有两个问题：一是校验失败可能发生在已经上传了一半之后，浪费时间也浪费排查现场；二是上传节奏被"边找边传"打碎，pinned 双缓冲吃不满。

本 crate 的解法是把装载拆成**三个纯阶段**：

1. **记录（record）**：对每个张量，查视图、用 **config 派生的维度当尺子**校验 dtype 与形状（`tensor_2d` 要求形状**精确等于**配置推导值）、在 GPU 上分配目标缓冲、验证目标区间，然后把"待上传操作"（源字节 + 目标地址）压进 `pending` 队列，领一张 `SlotId`/`VecSlotId` 凭证。此阶段**不上传任何权重字节**。
2. **执行（execute）**：`finish()` 一次性消费整个 `pending` 队列——pinned 双缓冲逐块搬运，最后 `ctx.sync()` 排干。
3. **兑换（redeem）**：`take`/`take_vec` 用凭证把 `DeviceMatrix`/`DeviceVec` 移出槽位，组装 `TransformerBlock`。`take` 之前有 `assert!(self.finished)`——半装载的模型不可能逃出装载器。

`StagedWeightLoader` 的文档注释就是这句话："every method checks dtype and config-derived dimensions at the load boundary; **Uploads run in `finish`, after every tensor has been validated**"。

`ModelRuntimeConfig` 则回答"装载为什么需要运行时配置"：装载不只是搬字节，还做三个**依赖运行时开关的决定**——`select_decode_projection_path` 在装载期定死解码投影路径并存进模型（u3-l8 展开）；`max_loras`/`max_lora_rank` 决定 `PackedLoraRegistry` 的预分配容量；`tensor_parallel` 决定一切切片边界。

#### 4.3.2 核心流程

`from_safetensors_with_runtime` 的完整时序（对照行号见 4.3.3）：

```text
① 设备与配置   DeviceContext(device_ordinal) → Config::from_file（u2-l1 的校验阶梯）
② 词表收敛     decodable_vocab ≤ vocab_size 硬检查；logits_rows 对齐；尾部抑制表上传
③ TP 闸门      tensor_parallel.validate_for(&config)   ← 任何张量被触碰之前
④ 路径决策     计算 capability → device_sm → select_decode_projection_path(运行时旋钮参与)
⑤ 数据面       load_shard_info → (单卡) WeightPrefetch → mmap → deserialize
⑥ 记录阶段     loader.{matrix, fused_rows, col_shard, vector} × 全部权重
               （embed/lm_head → 36 层循环 → 最终 norm）；旁路：precompute_rope 在 GPU 上算表
⑦ 执行阶段     loader.finish() → 排干 pending 队列
⑧ 兑换阶段     loader.take/take_vec → 组装 TransformerBlock/Qwen3Model
⑨ 收尾         drop(loader) → drop(prefetch) → 日志 → 后台线程 unmap 分片
```

记录阶段的四种原语：

| 原语 | 用在 | 校验 | 上传方式 |
| --- | --- | --- | --- |
| `matrix(name, rows, cols)` | embed、lm_head、单卡 o_proj/down_proj | 形状 == [rows, cols]、BF16 | pinned staged 连续块 |
| `fused_rows(cols, &[FusedPart])` | qkv、gate_up | 每个 part 形状 == [src_rows, cols]、行区间不越界 | 每 part 一段连续块，顺序拼进同一目标 |
| `col_shard(name, …, col_offset, take)` | TP 下的 o_proj/down_proj | 形状 == [src_rows, src_cols]、列区间不越界 | 按**行**分块、每块内跨步gather列 |
| `vector(name, len)` | 各 norm、q_norm/k_norm | 1D 且长度 == len、（对齐时零拷贝） | 小张量，pageable 直传 |

#### 4.3.3 源码精读

**ModelRuntimeConfig 七旋钮**——[src/weights/load.rs:L31-L40](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/weights/load.rs#L31-L40)：`enable_cuda_graph` / `tensor_parallel` / `device_ordinal` / `max_loras` / `max_lora_rank` / `decode_overlap` / `dflash_enabled`；[Default（L42-L54）](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/weights/load.rs#L42-L54) 给出"单卡、开图、rank0、LoRA 默认容量、无重叠、无草稿"的基线。注意后两个旋钮在 ④ 参与路径决策（[L107-L117](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/weights/load.rs#L107-L117) 把 `runtime.decode_overlap`、`runtime.dflash_enabled` 一起喂给 `select_decode_projection_path`）——**运行时开关改变的是权重侧的决策**，这就是它出现在装载配置里的原因。

**词表收敛**——[src/weights/load.rs:L77-L100](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/weights/load.rs#L77-L100)：tokenizer 实际可解码的词表不得超 checkpoint 的 `vocab_size`（否则 `ensure!` 硬失败）；`logits_rows` 是对齐后的投影行数，多出的尾部行由 `SuppressIds` 压制（u2-l2 4.3.3 讲过消费侧）。

**两阶段装载器本体**——[pegainfer-core/src/weight_loader.rs:L484-L500](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-core/src/weight_loader.rs#L484-L500)：`StagedWeightLoader` 持有 `slots`（矩阵槽）、`vec_slots`（向量槽）、`pending`（待上传操作）、`retained`（未对齐向量的 owned 源）与 `finished/failed` 两个状态位。

**记录原语的公共骨架**——`matrix`（[L637-L644](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-core/src/weight_loader.rs#L637-L644)）：`tensor_2d` 校验 → `alloc_timed` 分配（顺带累计分配耗时）→ `prepare` 验证目标区间并返回设备地址 → push `PendingUpload::Contiguous`。`ensure_recording`（[L523-L529](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-core/src/weight_loader.rs#L523-L529)）保证 finish 之后不能再记录。

**config 是尺子**——[pegainfer-core/src/weight_loader.rs:L613-L625](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-core/src/weight_loader.rs#L613-L625)：`tensor_2d` 的错误信息 "Tensor '{name}' has shape {shape:?}, **config expects** [{rows}, {cols}]"——checkpoint 与 config 不符在这里、在上传之前被抓住。这一刻正是 sizing.rs 模块注释里"配置维度被张量校验"的时间点（4.5 呼应）。

**finish 的"毒化直到成功"**——[pegainfer-core/src/weight_loader.rs:L540-L568](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-core/src/weight_loader.rs#L540-L568)：进入即置 `failed = true`，全部上传落地才清回并置 `finished`；失败路径调 `drain_or_abort` 排干流（健全性边界在 `finish` 内闭合，注释 L552-554 解释了为什么不能依赖 Drop——`mem::forget` 可跳过 Drop）。成功日志分别报告 `alloc_api_wall` 与 `execute_and_drain_wall`——**分配与上传两段耗时被刻意分开计量**，这是两阶段结构的可观测收益。

**pinned 双缓冲**——[pegainfer-core/src/weight_loader/staging.rs:L134-L162](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-core/src/weight_loader/staging.rs#L134-L162)：注释点明动机——"Pinned double-buffering **overlaps the source read with the H2D copy** that pageable `clone_htod` would serialize"；两块 32 MiB pinned 缓冲（[L20-L22](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-core/src/weight_loader/staging.rs#L20-L22)）。核心循环 `stage_chunk`（[L233-L281](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-core/src/weight_loader/staging.rs#L233-L281)）：等本块缓冲的 `dma_done` 事件 → fill 团队（≤8 线程，≥1 MiB 才并行，[L67-L79](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-core/src/weight_loader/staging.rs#L67-L79)）把源字节填进 pinned → `memcpy_htod_async` → 记录事件。下一块用另一块缓冲，填充与 DMA 重叠。

**目标区间的记录期验证**——[pegainfer-core/src/weight_loader/staging.rs:L352-L377](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-core/src/weight_loader/staging.rs#L352-L377) 的 `prepare`（连续块：`dst_offset + src.len() ≤ dst.len()`、字节数是 bf16 整数倍）与 [L381-L425](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-core/src/weight_loader/staging.rs#L381-L425) 的 `prepare_cols`（跨步块：源字节数是 `src_cols×2` 整数倍、列区间不越界、`take×2 ≤ 32 MiB`、`rows×take == dst.len()`）。**deferred 上传的安全性在记录时就已验证完**，执行期的 `unsafe upload_*` 只是兑现。

**兑换**——[pegainfer-core/src/weight_loader.rs:L602-L611](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-core/src/weight_loader.rs#L602-L611) 的 `take`（`assert!(self.finished)`，每个槽只能取一次）。Qwen3 侧的兑换现场在 [src/weights/load.rs:L273-L295](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/weights/load.rs#L273-L295)：`finish()` → `take(embed_slot)` → 逐层 `take`/`take_vec` 组装 `Attention { qkv_proj, o_proj, q_norm, k_norm, q_dim: q_rows, kv_dim: kv_rows }` 与 `MLP`——u2-l2 见过的字段在这里出生。

**收尾三件事**——[src/weights/load.rs:L296-L308](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/weights/load.rs#L296-L308)：`drop(loader)`（触发 stager Drop 排干事件）→ `drop(prefetch)` → 打"GPU model loaded in Xms"→ 把 `mmaps` 的 drop 挪进后台线程（注释：多 GB 映射的页表拆除不值得阻塞启动）。最终模型组装（[L310-L329](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/weights/load.rs#L310-L329)）里有两个装载期预分配：`PackedLoraRegistry::empty(runtime.max_loras, num_hidden_layers)`（L326，u7-l2 展开）与 RoPE 常量表。

#### 4.3.4 代码实践

**实践目标**：给 [src/weights/load.rs:L127-L308](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/weights/load.rs#L127-L308) 的每个调用贴上"记录 / 执行 / 兑换 / 旁路"标签。

1. 操作步骤：通读该区间，制作四列清单。提示：`loader.matrix/fused_rows/col_shard/vector` = 记录；`precompute_rope` 与 `SuppressIds::upload` = 旁路（在 GPU 上**计算**小表，不走装载器）；`loader.finish()` = 执行；`loader.take/take_vec` = 兑换。
2. 需要观察的现象：`loader.finish()`（L273）之前，对权重的唯一 H2D 都藏在 `pending` 队列里；`take` 全部发生在 `finish` 成功之后；`drop(loader)` 在所有 `take` 之后。
3. 预期结果：一张约 10 行的四色清单，能明确回答"哪个张量的校验发生在哪个字节上传之前"（答：**所有张量的校验都先于所有上传**）。纯阅读实践，无需 GPU。

#### 4.3.5 小练习与答案

**练习 1**：`vector` 为什么不走 pinned staging，而是 pageable 直传？

答案：源码注释一锤定音——"Small tensors; uploaded as plain pageable copies"（[L713-L714](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-core/src/weight_loader.rs#L713-L714)）。norm 向量只有 hidden（2560）或 head_dim（128）个元素，几 KB 的拷贝摊不平双缓冲的调度成本，直接 `memcpy_htod_async`（`PendingUpload::Vector` 分支，[L584-L596](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-core/src/weight_loader.rs#L584-L596)）。

**练习 2**：`finish` 一进来就把 `failed` 置真、成功才置假，这个"先毒化"写法防什么？

答案：防"失败后的空调用"——`execute_uploads` 用 `std::mem::take` 掏空了 `pending`（[L571](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-core/src/weight_loader.rs#L571)），如果失败后允许再次 `finish`，队列已空，会"成功"得毫无意义。毒化 + `ensure_recording` 让失败后的 loader 只能被丢弃重建（注释 L546-548）。

**练习 3**：双缓冲为什么恰好是**两**块？

答案：流水线只需要"正在填的"和"正在飞的"各一块：填 A 时 B 的 DMA 在飞，下次用 B 前先同步它的 `dma_done`（[staging.rs:L239-L244](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-core/src/weight_loader/staging.rs#L239-L244)）。第三块只会增加 pinned 内存占用（32 MiB×2 = 64 MiB 的上限写在该常量注释里），不会让两级流水更快。

### 4.4 TP 切片边界：shard_range、FusedPart 与 col_shard

#### 4.4.1 概念说明

TP>1 时，每个 rank 只装载自己那份权重。切法沿袭 u2-l2 的融合布局：

- **行切（输出维）**：q/k/v（切注意力头）、gate/up（切 intermediate 维）。每个 rank 对同一段输入算自己那部分输出的部分和——这与"融合矩阵按行堆叠"天然兼容：**先在源矩阵上切行区间，再拼进融合矩阵**。
- **列切（输入维）**：`o_proj`（输入是被切的 q 维）与 `down_proj`（输入是被切的 intermediate 维）。它们的**行**（hidden）不切、**列**按 rank 取区间，恰好消费上游行切产生的局部输入——行切/列切成对出现，每个 rank 的层内计算因此自洽，无需中途归约。
- **不切**：层数。`shard_range` 从未被 `num_hidden_layers` 调用过——每个 rank 都有全部 36 层（切层是流水线并行的概念，本 crate 不做）。

一切切片只有一个公式：`shard_range(total) = (rank × total/world_size, total/world_size)`——**等分**。它能放心整除，是因为 `validate_for` 在装载最前面（load.rs L102）已经把"heads、kv_heads、intermediate 能被 world_size 整除"设成了硬闸门（u2-l1 讲过）；world_size == 1 时公式退化为 `(0, total)`，所以**一条代码路径同时服务单卡与 TP**（load.rs L161-162 的注释原话）。

#### 4.4.2 核心流程

每层五步（对照 4.4.3 的行号）：

```text
q_total  = num_attention_heads    × head_dim     # 4096 (Qwen3-4B)
kv_total = num_key_value_heads    × head_dim     # 1024
inter    = intermediate_size                      # 9728
(q_off,  q_rows)   = shard_range(q_total)         # TP=2, rank1: (2048, 2048)
(kv_off, kv_rows)  = shard_range(kv_total)        #                (512, 512)
(in_off, in_rows)  = shard_range(inter)           #                (4864, 4864)

qkv_proj     ← fused_rows(hidden, [Q(q_off,q_rows), K(kv_off,kv_rows), V(kv_off,kv_rows)])
gate_up_proj ← fused_rows(hidden, [gate(in_off,in_rows), up(in_off,in_rows)])
o_proj       ← col_shard(src_rows=hidden, src_cols=q_total,  col_offset=q_off, take=q_rows)
down_proj    ← col_shard(src_rows=hidden, src_cols=inter,    col_offset=in_off, take=in_rows)
# 向量（norm/q_norm/k_norm）不切，whole 加载
```

`fused_rows` 对每个 part 的字节切片是：

\[
\text{bytes} = \text{full}\big[\,\text{row\_offset} \times \text{cols} \times 2\ ,\ (\text{row\_offset}+\text{rows}) \times \text{cols} \times 2\,\big)
\]

行区间连续 ⇒ 字节区间连续 ⇒ 每 part 一段连续 staged 块，顺序拼进同一目标矩阵。

#### 4.4.3 源码精读

**shard_range 公式**——[src/config.rs:L885-L888](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/config.rs#L885-L888)：三行代码，整除由 [validate_for（L849-L883）](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/config.rs#L849-L883) 保证（L851-860 查 world_size/rank 合法性，L861-881 查三个整除）。`is_sharded`（[L890-L892](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/config.rs#L890-L892)）决定 o/down 走 `col_shard` 还是整矩阵。

**Qwen3 侧的三次 shard_range**——[src/weights/load.rs:L151-L157](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/weights/load.rs#L151-L157)：`q_total/kv_total/inter_total` 三个总量在循环外算一次，三对 `(offset, rows)` 对所有层通用（每层切法相同）。

**FusedPart 与 QKV 融合**——[src/weights/load.rs:L163-L188](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/weights/load.rs#L163-L188)：三个 `FusedPart` 各带 `src_rows`（**源**张量的全量行数，供形状校验）、`row_offset`、`rows`。结构体定义在 [pegainfer-core/src/weight_loader.rs:L445-L452](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-core/src/weight_loader.rs#L445-L452)："One row-consecutive part of a fused matrix"。gate/up 同构（[L190-L208](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/weights/load.rs#L190-L208)）。

**fused_rows 的记录逻辑**——[pegainfer-core/src/weight_loader.rs:L646-L679](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-core/src/weight_loader.rs#L646-L679)：逐 part `tensor_2d(name, src_rows, cols)`（源形状必须是**全量**）→ 行区间 `checked_add` 越界检查（L656-664）→ 字节切片（L665-668，即上面的公式）→ 分配 `total_rows × cols` → 逐 part `prepare` 并 push 连续上传。**切片发生在源上、融合发生在目标里**。

**col_shard 的记录逻辑**——[pegainfer-core/src/weight_loader.rs:L681-L711](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-core/src/weight_loader.rs#L681-L711)：校验源形状 `[src_rows, src_cols]`、列区间越界检查，然后 `prepare_cols` 生成 `ColShardPlan`（[staging.rs:L126-L132](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-core/src/weight_loader/staging.rs#L126-L132)：`stride_b/off_b/take_b/rows/dst_at`）。执行期 `upload_cols_at`（[L206-L228](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-core/src/weight_loader/staging.rs#L206-L228)）按行分块、每块内每行跨 `stride_b` 取 `take_b` 字节——列不连续，所以不能整段 memcpy，只能逐行 gather（gather 本身有并行 fill 团队加速）。

**o_proj/down_proj 的分叉**——[src/weights/load.rs:L214-L228](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/weights/load.rs#L214-L228)（o_proj）与 [L242-L256](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/weights/load.rs#L242-L256)（down_proj）：`is_sharded()` 时 col_shard、否则整矩阵。注意参数：o_proj 是 `(name, hidden, q_total, q_row_offset, q_rows)`——源 `[hidden × q_total]`，取列 `[q_off, q_off+q_rows)`；down_proj 是 `(name, hidden, inter_total, inter_row_offset, inter_rows)`。

#### 4.4.4 代码实践

**实践目标**：写出 TP=4 下 Qwen3-4B 每个 rank 的全部切片边界（TP=2 的完整推导放在综合实践）。

1. 操作步骤：按 4.4.2 公式代入 `world_size=4`：`q_total=4096 → shard_len=1024`，`kv_total=1024 → 256`，`inter=9728 → 2432`。写出 rank 0..3 的 `(offset, rows)` 三元组、每 rank 融合矩阵形状（`qkv_proj` 行数 = `q_rows + 2×kv_rows`）、o_proj/down_proj 形状。再用 [src/config.rs:L346-L364](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/config.rs#L346-L364) 的 `local_q_dim`/`local_kv_dim` 交叉验证（`local_q_dim = heads/world × head_dim`，应等于 `q_rows`）。
2. 需要观察的现象：rank r 的 offset 恰为 `r × shard_len`；融合矩阵行数随 world_size 等比缩小；向量（norm）不变。
3. 预期结果：rank3 的 qkv 行数 = 1024 + 2×256 = 1536；o_proj = 2560×1024；down_proj = 2560×2432。纯数值推导，无需 GPU；机器验证方式见综合实践的单测。

#### 4.4.5 小练习与答案

**练习 1**：`FusedPart.src_rows` 为什么填**全量**行数而不是切片后的行数？

答案：`tensor_2d(name, part.src_rows, cols)` 拿它校验**源张量**的形状（源在 checkpoint 里永远是全量的），而 `row_offset/rows` 描述在本 rank 要取的区间；两者合起来才能既验形状又切边界。若填切片行数，形状校验会错杀合法 checkpoint。

**练习 2**：为什么 q/k/v 行切、o_proj 却列切？

答案：q/k/v 是"行并行"：每个 rank 算自己的注意力头（输出维 = 头数×head_dim 被切），注意力输出因此是每 rank 一份**局部** q 维向量；`o_proj` 的输入正是这份局部向量，所以它的**输入维（列）**按同一区间切——`col_offset=q_off, take=q_rows`。行切与列切用同一个 `shard_range` 结果，配对天然对齐。

**练习 3**：TP=2、rank1 的 `k_proj`（源 `[1024 × 2560]`，bf16）的字节区间是多少？

答案：`kv_off=512, kv_rows=512`，代入公式得 `full[512×2560×2, 1024×2560×2)` = `full[2_621_440, 5_242_880)`（[pegainfer-core/src/weight_loader.rs:L665-L668](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-core/src/weight_loader.rs#L665-L668) 的切片表达式）。这段 2.5 MiB 连续字节随后作为 `PendingUpload::Contiguous` 拼进融合矩阵的 K 段。

### 4.5 防溢出算术：sizing.rs 为何先于张量校验

#### 4.5.1 概念说明

[src/sizing.rs](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/sizing.rs) 只有 23 行、两个函数：`product`（checked 乘法折叠）与 `sum`（checked 加法折叠）。它存在的理由写在模块注释里：

> "Budgets are computed from config-file dims **before** those dims are validated against the weight tensors, so a corrupt config must fail loudly here rather than wrap into an under-reservation."

时间线回放：启动时 `Config::from_file` 先做 schema 与语义校验（u2-l1），随后**大量字节预算直接由这些配置维度连乘出来**（KV 每块字节、DFlash 草稿预留、hedge 暂存页……），而"配置维度 vs checkpoint 张量形状"的核对要等到装载记录期 `tensor_2d` 才发生（4.3.3）。也就是说存在一个窗口：**维度还没被任何真实张量对过账，就要拿它算显存**。一个手滑的 config（比如 `intermediate_size` 多打三个 9）在 release 构建里用普通 `*` 会**静默回绕**成一个可能是小数字的 `usize`——预算"变小"比"报错"危险得多：池子被开小、服务上限被算错，问题在几小时后的运行期才爆。checked 乘加把这个失败提前到乘法现场，错误信息还带上整个因子表（`"sizing overflow in product {factors:?}"`）。

#### 4.5.2 核心流程

两条使用纪律：

1. **预算算术一律走 sizing**。真实调用形态如 [src/executor.rs:L1376-L1379](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/executor.rs#L1376-L1379)：草稿 hedge 暂存页折算成 margin 字节，`sum(&[margin, product(&[pages, page_bytes])?])?`——嵌套调用里任何一层溢出都会带着自己的因子表冒出来。
2. **"先于张量"的场景是常态**。最直接的例证：[src/executor.rs:L1331-L1373](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/executor.rs#L1331-L1373) 在主模型装载完成后、**草稿模型张量尚未装载**时，就从草稿 config 用 `DFlashMemoryReservation` 算出固定字节数并加进 margin——注释明说"loads after profiling but lives outside the paged KV pool, so reserve its footprint up front **from the draft config**"。而 [dflash/reservation.rs:L93-L148](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/dflash/reservation.rs#L93-L148) 整段预算全部由 `sizing::product/sum` 拼成。

#### 4.5.3 源码精读

**两个函数**——[src/sizing.rs:L10-L15](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/sizing.rs#L10-L15) 的 `product`：`try_fold(1usize, checked_mul)`，任一步溢出即 `Err`，错误信息打印完整因子列表；[L17-L22](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/sizing.rs#L17-L22) 的 `sum` 同构。

**全乘积支配部分乘积**——u2-l2 读过的 `kv_bytes_per_block`（[src/weights.rs:L823-L861](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/weights.rs#L823-L861)）在构造 layout **之前**先对全乘积做 `sizing::product`，注释解释：layout 内部用普通乘法推步长，但全乘积支配一切部分乘积——清零全乘积等于清零全部。这是"哪些点必须 checked"的选择标准：**看住支配项**。

**checked 与 saturating 的分工**——同一片 executor 代码里，hedge 页数上限用的是 `saturating_mul` + `min`（[src/executor.rs:L1359-L1369](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/executor.rs#L1359-L1369)）：那里算的是**封顶值**（两个上界取小），钳到饱和是语义正确的；而 margin 字节数是**精确账目**，必须 checked。两种算术并存不是不一致，而是"必须精确的 checked、允许钳制的 saturating"。

**对照组：装载路径里的普通乘法**——[pegainfer-core/src/weight_loader.rs:L665-L668](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-core/src/weight_loader.rs#L665-L668) 的 `row_offset * cols * elem` 是普通乘法，为什么可以？因为它发生在 `tensor_2d` 已验证源形状 == `[src_rows, cols]`、行区间已 `checked_add` 验证 ≤ `src_rows` **之后**——乘积被已验证的张量字节长度上界住。sizing 保护的是"张量还不存在"的阶段；张量已对账后，普通算术有前序校验兜底。**同一段启动流程里两种算术的边界，就是"校验已否发生"的边界。**

#### 4.5.4 代码实践

**实践目标**：数一数 sizing 的真实覆盖面。

1. 操作步骤：在 crate 根执行 `rg "sizing::(product|sum)" src/ -c`，按文件汇总命中数；挑 [src/dspark.rs:L807-L825](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/dspark.rs#L807-L825) 任一条链，写出它算的是哪块缓冲的字节数、维度来自哪个 config。
2. 需要观察的现象：所有命中都出现在"预算/预留"语境（reservation、budget、margin、scratch），没有一个出现在已验证张量的搬运路径——与 4.5.3 的边界论述互证。
3. 预期结果：一张"文件 × 命中数 × 用途"小表（executor.rs、weights.rs、dflash/reservation.rs、dspark.rs、dflash/selector.rs 五处为主）。只读搜索，无需 GPU。

#### 4.5.5 小练习与答案

**练习 1**：release 构建里 `4096usize * 2560 * 2 * 999_999_999_999` 会发生什么？为什么危险？

答案：release 默认关闭溢出检查，乘积回绕成某个 mod \( 2^{64} \) 的余数——很可能是个**不太大的数**。若它是 KV 预算或预留字节，系统会带着错误的小预算继续启动（少预留），在运行期才以 OOM 或服务上限异常的形式爆出来。`sizing::product` 把同一串乘法变成携带因子表的 `Err`。

**练习 2**：既然 debug 构建溢出会 panic，为什么还需要 sizing？

答案：生产部署用的是 release（CLAUDE.md 明确"Always use `--release`"，debug 对 GPU/CUDA 慢到不可用）。panic 只保护开发期；sizing 把保护带进 release。另外 `Err` 携带因子表，比 panic 的 backtrace 更直接指向坏维度。

**练习 3**：`product(&[])` 与 `sum(&[])` 分别返回什么？这合理吗？

答案：分别返回 `Ok(1)` 与 `Ok(0)`——乘法单位元与加法单位元（`try_fold` 的初值）。对空因子表这是数学上自洽的定义；调用方若传空表，通常说明维度本身缺失，值得在更高层被形状校验拦下（而不是让单位元悄悄通过预算）。

## 5. 综合实践

**任务：梳理两阶段结构的完整清单 + TP=2 QKV 切片边界推导 + 为装载纯函数补一个单测并运行。**

### 5.1 两阶段结构清单（记录 → 执行 → 兑换）

对照 [src/weights/load.rs:L127-L308](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/weights/load.rs#L127-L308) 填出下表（答案已给出，核对时关注**顺序**）：

| 阶段 | 代码点（load.rs） | 内容 |
| --- | --- | --- |
| 数据面准备 | L127-L132 | `load_shard_info` → 单卡 spawn 预取 → mmap → deserialize |
| 记录 | L137-L145 | embed `matrix`；tied ⇒ 无 lm_head 槽 |
| 记录 | L158-L257 | 36 层循环：`fused_rows`(qkv) → `fused_rows`(gate_up) → `col_shard`/`matrix`(o/down) → 五个 `vector` |
| 记录 | L260 | 最终 norm `vector` |
| 旁路 | L262-L271 | `precompute_rope` 在 GPU 上**算** RoPE 表（非上传） |
| 执行 | L273 | `loader.finish()`——此刻才发生全部权重 H2D |
| 兑换 | L274-L295 | `take`/`take_vec` → 组装 `Attention`/`MLP`/`TransformerBlock` |
| 收尾 | L296-L308 | drop loader → drop prefetch → 日志 → 后台 unmap 线程 |

关键论断（可当面试题）：**任何张量的形状校验（`tensor_2d`）都先于任何权重的上传（`finish`）**——这就是"先记录整个计划再上传"的动机：要么得到一个全部校验通过、完整上传的模型，要么在花费上传时间之前就带着精确的错误信息失败。

### 5.2 TP=2 的 QKV 切片边界推导（Qwen3-4B）

已知 `heads=32, kv_heads=8, head_dim=128, hidden=2560, intermediate=9728`，故 \( q_{total}=4096 \)、\( kv_{total}=1024 \)、\( inter=9728 \)。`world_size=2` ⇒ `shard_len = total/2`：

| 源张量（形状） | 切法 | rank 0 区间 | rank 1 区间 |
| --- | --- | --- | --- |
| `q_proj.weight` [4096×2560] | 行切 | 行 [0, 2048) | 行 [2048, 4096) |
| `k_proj.weight` [1024×2560] | 行切 | 行 [0, 512) | 行 [512, 1024) |
| `v_proj.weight` [1024×2560] | 行切 | 行 [0, 512) | 行 [512, 1024) |
| **融合 `qkv_proj`（每 rank）** | — | **3072×2560**（2048+512+512 行） | **3072×2560** |
| `o_proj.weight` [2560×4096] | **列**切 [q_off, q_off+q_rows) | 列 [0, 2048) → 2560×2048 | 列 [2048, 4096) → 2560×2048 |
| `gate/up_proj` [9728×2560] | 行切 | [0, 4864) | [4864, 9728) |
| `down_proj.weight` [2560×9728] | **列**切 [in_off, in_off+in_rows) | 列 [0, 4864) → 2560×4864 | 列 [4864, 9728) → 2560×4864 |

逐项核对源码：三对区间来自 [load.rs:L155-L157](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/weights/load.rs#L155-L157) 的三次 `shard_range`；rank1 `k_proj` 的字节区间为 `full[512×2560×2, 1024×2560×2)` = `[2_621_440, 5_242_880)`（切片公式在 [weight_loader.rs:L665-L668](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-core/src/weight_loader.rs#L665-L668)）；融合矩阵内三段的行偏移用**本地**维度：Q ∈ [0, 2048)、K ∈ [2048, 2560)、V ∈ [2560, 3072)——正是 u2-l2 偏移公式代入 `q_dim=2048, kv_dim=512` 的结果，与 [load.rs:L286-L287](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/weights/load.rs#L286-L287) 写进 `Attention` 的 `q_dim/kv_dim` 一致。

### 5.3 为装载纯函数补单测并运行

切片公式的唯一 owner `shard_range` 目前**没有**直接单测（整除性由 `validate_for` 的调用方保证，公式本身靠 4.4 的推导）。在**你自己的工作副本**里（学习练习，勿提交，与 u2-l2 综合实践同一约定）把它加进 [src/config.rs:L914](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/config.rs#L914) 起的 `#[cfg(test)] mod tests`：

```rust
// 示例代码：加入 src/config.rs 的 tests 模块（学习用，不改动生产逻辑）
#[test]
fn shard_range_covers_tp2_qkv_totals() {
    let q_total = 32 * 128;  // 4096
    let kv_total = 8 * 128;  // 1024
    let inter = 9728;

    // world_size == 1：offset 0、全行（load.rs 注释承诺的退化情形）
    let single = TensorParallelConfig { rank: 0, world_size: 1 };
    assert_eq!(single.shard_range(q_total), (0, 4096));

    // TP=2：等分，offset = rank * shard_len
    let r0 = TensorParallelConfig { rank: 0, world_size: 2 };
    let r1 = TensorParallelConfig { rank: 1, world_size: 2 };
    assert_eq!(r0.shard_range(q_total), (0, 2048));
    assert_eq!(r1.shard_range(q_total), (2048, 2048));
    assert_eq!(r0.shard_range(kv_total), (0, 512));
    assert_eq!(r1.shard_range(kv_total), (512, 512));
    assert_eq!(r1.shard_range(inter), (4864, 4864));

    // 每 rank 融合 qkv 行数 = q_rows + 2 * kv_rows（对照 5.2 的 3072）
    let (_, q_rows) = r1.shard_range(q_total);
    let (_, kv_rows) = r1.shard_range(kv_total);
    assert_eq!(q_rows + 2 * kv_rows, 3072);
}
```

（`mod tests` 已有 `use super::*;`，`TensorParallelConfig` 直接可用。）

运行与对照：

1. `cargo test --release -p pegainfer-qwen3 --lib shard_range`——只做整数断言，无需 GPU；通过即证明 5.2 表格的机器可验证。**待本地验证**。
2. 顺带跑一遍已有的装载侧纯函数测试（它们已在库里，属于"找到"的部分）：`cargo test --release -p pegainfer-core --lib weight_loader`——覆盖未对齐 bf16/f32 的 Cow 行为（[weight_loader.rs:L1099-L1168](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-core/src/weight_loader.rs#L1099-L1168)）与 staging 的并行 gather/chunk 边界（[staging.rs:L457-L516](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-core/src/weight_loader/staging.rs#L457-L516)）。测试本身不创建 CUDA 设备，但前提是本地工具链能编译该 crate。**待本地验证**。

## 6. 本讲小结

- 分片读取三步归一：`load_shard_info` 把"单文件/带索引的分片"统一成 `(路径表, 张量→分片下标)`；`mmap_shards` + `deserialize_shards` 得到零拷贝 `TensorView`，未对齐负载经 `Cow` 兜底。
- `WeightPrefetch` 是咨询式预取：8 线程 × 16 MiB chunk、`RWF_NOWAIT` 探测驻留、慢于 1 GB/s 自觉退场、失败只聚合 warning；单卡在装载函数内 spawn，TP 由 executor 在逐 rank 循环外共享一份。
- 装载是三阶段纯流水：**记录**（config 当尺子逐张量校验 + 分配 + 压 pending，不上传一个字节）→ **执行**（`finish` 走 32 MiB×2 pinned 双缓冲统一搬运）→ **兑换**（`take` 组装模型，`assert!(finished)` 挡住半装载逃逸）；`ModelRuntimeConfig` 的七个旋钮之所以出现在装载期，是因为投影路径决策与 LoRA 注册表预分配都发生在装载时。
- TP 切片只有一个公式 `shard_range(total) = (rank×total/ws, total/ws)`（整除由 `validate_for` 预先保证，ws=1 退化为全量）：q/k/v、gate/up **行切**后融合（TP=2 时每 rank `qkv_proj` 为 3072×2560），o_proj/down_proj **列切**消费被切输入，层数不切。
- [src/sizing.rs](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/sizing.rs) 的 checked 乘加守住"配置维度已读、张量未对账"的窗口：release 下普通乘法静默回绕会让坏 config 变成**少预留**而不是错误；已验证张量路径上的普通算术则由前序形状校验兜底——两种算术的边界就是校验发生与否的边界。

## 7. 下一步学习建议

权重现已落进显存，但"能装下多少 KV"的账还没算——下一讲 **u2-l4（显存画像与 KV 预算）** 把本讲的装载结果当作账本第一行：`profile_temp_blocks` 画像激活峰值、`KvBudget` 从预算里数出块数、`servable_len` 折算服务上限（你会再次遇到 `sizing::product`，这次在它最主场的位置）。往执行侧走，**u3-l1（Qwen3Executor 与 ModelExecutor 契约）** 会展示 [src/executor.rs:L1318-L1330](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/executor.rs#L1318-L1330) 这个调用点的下半场；**u9-l1（张量并行）** 则把本讲 4.4 的切片边界放进多 rank 服务的完整图景（含启动期每 rank 图预捕获）。建议重读 [src/weights/load.rs](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/weights/load.rs) 一遍并核对 5.1 清单——此时你应当能对每个调用说出它属于哪个阶段、校验了什么、何时才有字节进显存。
