# weight_loader.rs 与 staging.rs：权重从磁盘到显存

## 1. 本讲目标

模型推理前必须先把几百 MB 到几百 GB 的权重从磁盘搬进 GPU 显存。这一步做错了（形状对不上、精度不对、拷贝越界），后面的推理全是垃圾输出。学完本讲，你应该能够：

1. 画出权重从 safetensors 文件到 device buffer 的完整数据流图，标出每一跳的同步点与拷贝类型。
2. 说清 `StagedWeightLoader` 的「记录 → finish → 赎回」三段状态机，以及为什么所有校验都要发生在任何字节上传之前。
3. 解释 pinned 双缓冲（`WeightStager`）如何把宿主侧填充与 H2D 拷贝重叠起来，以及路径上的流一致性检查点在哪里。
4. 说明权重加载与 KV 池共享显存预算时的真实协调方式（提示：core 里没有协商代码，协调发生在模型 crate 的装配顺序上）。

## 2. 前置知识

- **safetensors 格式**：Hugging Face 的安全张量格式。文件头部是一段 JSON（记录每个张量的名字、dtype、shape 和 payload 的字节区间），随后紧跟原始字节数据。解析 header 后，每个张量的数据就是文件里一段连续字节，**不需要反序列化整个文件**。
- **mmap（内存映射文件）**：把文件「映射」进进程地址空间，读它就像读内存，由操作系统按需把磁盘页调入页缓存。`memmap2::Mmap` 提供这个能力，是零拷贝读取大文件的标准做法。
- **H2D / D2H**：Host-to-Device（宿主内存 → 显存）与 Device-to-Host 的拷贝。权重加载的主拷贝是 H2D。
- **pageable 内存 vs pinned 内存**：普通 `Vec` 分配的宿主内存是可换页的，CUDA 异步拷贝用它时驱动会在内部先拷进一块临时 pinned 缓冲，既慢又无法真正与计算重叠；**pinned（页锁定）内存**是 DMA 引擎可以直接访问的物理内存，异步 H2D 从它发起才真正快。`DeviceContext.ctx.alloc_pinned`（u2-l1 讲过）分配的就是这种内存。
- **CUDA stream 与 event**：stream 是 GPU 上的操作队列；event 是队列里的一个标记点，可以用来确认「排到这个 event 之前的所有工作都完成了」。`CU_EVENT_BLOCKING_SYNC` 表示宿主线程可以在 event 上阻塞等待。
- **bf16**：brain float 16，2 字节。本讲权重的主 dtype；字节数 = 元素数 × 2。
- **双缓冲**：两块缓冲交替使用——宿主线程往 A 填数据时，DMA 还在搬 B；反过来亦然。总耗时从「填充+拷贝」的串行叠加变成两者的最大值。

## 3. 本讲源码地图

| 文件 | 行数约 | 作用 |
|------|------|------|
| [pegainfer-core/src/weight_loader.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/weight_loader.rs#L1) | 1169 | core 内最大的源文件。上半部：分片索引解析、mmap、SafeTensors 视图、对齐处理；中部：`StagedWeightLoader` 状态机；尾部：一批不走暂存器的直接加载函数（`load_tensor_*` 系列）与宿主侧张量读取。 |
| [pegainfer-core/src/weight_loader/staging.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/weight_loader/staging.rs#L1) | 517 | pinned 双缓冲 `WeightStager`、并行填充线程池 `FillPool`、上传前的校验（`prepare`/`prepare_cols`）与失败时的排水（`drain_or_abort`）。 |
| [pegainfer-core/tests/weight_loader_gpu.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/tests/weight_loader_gpu.rs#L1) | 77 | 需 GPU 的集成测试，验证 loader 的状态机契约与非对齐 payload 的 round-trip。 |
| [pegainfer-core/src/tensor.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/tensor.rs#L1-L2) | 2 | 一行再出口门面（u2-l1 已精读），`DeviceContext`/`DeviceMatrix`/`DeviceVec` 的真身在 kernels。 |
| pegainfer-qwen3/src/weights/load.rs | — | （消费方）qwen3 用本讲 API 装配权重的现场，综合实践会用到。 |

记忆锚点：**weight_loader.rs 负责「解析与校验」，staging.rs 负责「搬运与重叠」**。前者决定「搬什么、搬到哪」，后者决定「怎么搬得快、怎么搬得安全」。

## 4. 核心概念与源码讲解

### 4.1 safetensors H2D 路径：从磁盘到 SafeTensors 视图

#### 4.1.1 概念说明

一个 checkpoint 可能是一个 `model.safetensors` 单文件，也可能被切成多个分片（shard）并由 `model.safetensors.index.json` 索引。加载的第一步是搞清楚「有哪些文件、每个张量在哪个文件里」，然后 mmap 打开、解析 header，得到一组 `SafeTensors` 视图——注意这一步**不拷贝任何 payload**，张量数据仍然躺在 mmap 的文件页里。

#### 4.1.2 核心流程

```text
model_path/
├── model.safetensors            ──(存在?)──> 单文件路径：直接返回该文件
└── model.safetensors.index.json ──(否则)──> 解析 weight_map: {张量名: 分片文件名}
                                                去重得到分片文件列表
                                                       │
mmap_shards(分片路径)     -> Vec<Mmap>          （零拷贝映射）
deserialize_shards(mmaps) -> Vec<SafeTensors>   （只解析 header）
find_tensor(name)         -> TensorView         （零拷贝字节切片）
        │
        ├─ 指针对齐  -> Cow::Borrowed(&[bf16])  （零拷贝借用）
        └─ 指针非对齐 -> Cow::Owned(Vec<bf16>)  （按小端解码一次，避免 UB）
```

非对齐分支值得强调：safetensors 格式允许 payload 出现在任意字节偏移上，而 Rust 的 `&[bf16]` 要求 2 字节对齐——把非对齐字节强转成 bf16 切片是未定义行为。所以这里用 `Cow` 分两路：对齐就零拷贝借用，非对齐就老老实实按字节解码。

#### 4.1.3 源码精读

[pegainfer-core/src/weight_loader.rs:57-89](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/weight_loader.rs#L57-L89) — `load_shard_info`：先探测单文件 `model.safetensors` 是否存在；不存在就读 `model.safetensors.index.json` 的 `weight_map`，把「张量名 → 分片文件名」翻译成「张量名 → 分片下标」，返回 `(分片路径列表, 映射表)`。

[pegainfer-core/src/weight_loader.rs:1055-1087](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/weight_loader.rs#L1055-L1087) — `load_shard_info_fixed`：兼容性补丁。某些模型（注释点名 Qwen3.5）的 index.json 写的是 `model.safetensors-00001-of-00002.safetensors`，而磁盘上实际叫 `model-00001-of-00002.safetensors`，这里检测并修正路径。

[pegainfer-core/src/weight_loader.rs:294-329](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/weight_loader.rs#L294-L329) — `mmap_shards` 与 `deserialize_shards`：前者逐个 `Mmap::map`（SAFETY 注释承诺映射在加载期间存活、文件不会被并发修改），并打印总 MB 数；后者对每个 mmap 调 `SafeTensors::deserialize`，**只解析文件头 JSON**，payload 不动。

[pegainfer-core/src/weight_loader.rs:331-349](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/weight_loader.rs#L331-L349) — `find_tensor`：优先查 `weight_map` 精确定位分片；查不到（单文件场景映射表为空）就线性扫所有分片兜底；都没有则报「张量不存在」。

[pegainfer-core/src/weight_loader.rs:369-394](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/weight_loader.rs#L369-L394) — `tensor_bf16_cow`：先由 `tensor_bf16_bytes`（L351-367）校验 dtype 必须是 BF16、字节数必须是 2 的整数倍；再按指针是否 2 字节对齐走借用或小端解码两条路。头注释明说「misaligned bf16 view 是 UB」。

#### 4.1.4 代码实践

1. **实践目标**：验证对齐分支逻辑在纯 CPU 下可测、可跑。
2. **操作步骤**：运行内嵌单测
   ```bash
   cargo test --release -p pegainfer-core --lib tensor_bf16_cow
   ```
3. **需要观察的现象**：`tensor_bf16_cow_borrows_aligned_and_decodes_unaligned` 测试通过。阅读该测试（[pegainfer-core/src/weight_loader.rs:1141-1168](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/weight_loader.rs#L1141-L1168)）：它先构造一个 `Vec<u8>`，算出使其 payload 指针对齐/非对齐的两个偏移，分别强制走两条分支，再比对解码结果与原始位模式。
4. **预期结果**：两个用例均通过；且该测试完全不触碰 GPU（`TensorView` 是纯宿主类型）——这正是它能进 `--lib` 快车道的原因（回顾 u1-l3 的判据：看它构造了什么）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `deserialize_shards` 之后内存占用并不等于整个 checkpoint 的大小？
答案：mmap 是惰性的——只有 header 解析发生在进程里；payload 页在第一次被读到时才由操作系统调入页缓存，且可以被换出。

**练习 2**：如果 index.json 里某个张量名拼错了，`find_tensor` 会怎么失败？
答案：`weight_map` 查不到会走「扫所有分片」兜底；兜底也找不到时返回 `Err("Tensor '...' not found in any shard")`，错误发生在任何分配/上传之前——fail fast。

**练习 3**：`tensor_bf16_cow` 为什么必须在 `to_bits` 层面（`u16`）而不是 `f32` 值层面做断言？
答案：位级比对才能证明「解码是逐字节忠实的」；若比对浮点值，NaN/非规格数之类的位模式差异可能被浮点比较掩盖。

### 4.2 StagedWeightLoader：先记录、后执行、再赎回

#### 4.2.1 概念说明

`StagedWeightLoader` 是核心加载器，头注释概括了它的契约：「**Validating** BF16 checkpoint loader backed by pinned staging: every method checks dtype and config-derived dimensions at the load boundary; uploads run in `finish`, after every tensor has been validated.」

它是一个三段状态机：

1. **记录阶段**（`matrix` / `fused_rows` / `col_shard` / `vector`）：对每个权重张量做 shape/dtype 校验、在设备上分配目标 buffer、把「上传计划」存进 `pending`——但**不真正上传**。
2. **执行阶段**（`finish`）：所有张量都校验通过后，一次性执行全部 pending 上传并同步流。
3. **赎回阶段**（`take` / `take_vec`）：上传成功后，凭 `SlotId`/`VecSlotId` 拿走 `DeviceMatrix`/`DeviceVec`。

为什么要推迟到 `finish`？两个理由：其一，任何一个张量与 config 推导的维度不符都会**在任何字节离开宿主之前**失败，不会留下半装配的模型权重和一堆要回收的显存；其二，`PendingUpload` 借用 mmap 里的源数据（`&'d [u8]`），集中执行让生命周期管理收拢在一个函数里。

#### 4.2.2 核心流程

```text
new(ctx, shards, weight_map)
   │
   ├─ matrix(name, rows, cols)      校验 2D shape == [rows, cols]
   │      alloc_timed(rows*cols) → prepare() → push PendingUpload::Contiguous
   ├─ fused_rows(cols, parts)       多个源张量的行区间拼接（如 QKV 融合装载）
   ├─ col_shard(name, ..., offset)  取源张量的列区间（TP 列切分）
   │      prepare_cols() → PendingUpload::ColShard{src, plan}
   ├─ vector(name, len)             小张量，pageable 直传 → PendingUpload::Vector
   │
finish()
   ├─ failed = true（先毒化；成功才解除）
   ├─ execute_uploads()：逐个执行 pending，最后 ctx.sync()
   └─ 成功：finished = true；失败：drain_or_abort + 清 retained + 返回 Err
   │
take(SlotId) → DeviceMatrix      （断言 finished；slot 只能取一次）
take_vec(VecSlotId) → DeviceVec
```

三类 `PendingUpload` 的差别（[pegainfer-core/src/weight_loader.rs:462-466](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/weight_loader.rs#L462-L466)）：

| 变体 | 源 | 执行方式 | 典型用途 |
|------|-----|---------|---------|
| `Contiguous` | mmap 里的连续字节 | 走 `WeightStager::upload_at`（pinned 暂存） | 整块矩阵、fused_rows 的每一段 |
| `ColShard` | mmap 字节 + `ColShardPlan` | `upload_at_cols`（暂存时逐行 gather 列区间） | TP 列并行权重 |
| `Vector` | `Cow<[bf16]>` | 直接 `memcpy_htod_async`（pageable） | norm 权重等小向量 |

#### 4.2.3 源码精读

[pegainfer-core/src/weight_loader.rs:484-500](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/weight_loader.rs#L484-L500) — `StagedWeightLoader` 结构体：持有 `&DeviceContext`、`WeightStager`、SafeTensors 分片引用、slots/vec_slots（已分配的设备 buffer 暂存处）、`pending`（上传计划队列）、`retained`（owned Cow 源的保活区）、`finished`/`failed` 两个状态位。

[pegainfer-core/src/weight_loader.rs:613-644](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/weight_loader.rs#L613-L644) — `tensor_2d` 与 `matrix`：`tensor_2d` 强制 shape 恰好等于调用方（模型 crate）从 config 算出的 `[rows, cols]`，否则报「config expects …」；`matrix` 依次做校验 → `alloc_timed`（流上分配，计时累进 `alloc_ms`）→ `prepare`（校验并换算目标设备地址）→ 记录 `PendingUpload::Contiguous` → 返回 `SlotId`。**注意顺序：校验失败时一个字节都没分配。**

[pegainfer-core/src/weight_loader.rs:646-679](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/weight_loader.rs#L646-L679) — `fused_rows`：把多个源张量各自的行区间（`FusedPart { name, src_rows, row_offset, rows }`）拼成一个目标矩阵，每个 part 先按 `(src_rows, cols)` 整体校验再切行区间，逐段记录 `Contiguous` 上传并累加 `dst_offset`。这是「把检查点里的 q_proj/k_proj/v_proj 装成一个 qkv_proj」这类融合装载的机制。

[pegainfer-core/src/weight_loader.rs:683-711](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/weight_loader.rs#L683-L711) — `col_shard`：先按 `(src_rows, src_cols)` 校验源张量全貌，再校验列区间 `[col_offset, col_offset+take)` 在界内，分配 `src_rows × take` 的目标，用 `prepare_cols` 生成执行计划。

[pegainfer-core/src/weight_loader.rs:713-731](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/weight_loader.rs#L713-L731) — `vector`：小张量路径，头注释直言「uploaded as plain pageable copies」——不值得动用暂存器，直接 pageable 拷贝。`Cow::Owned` 的源会在执行时被推进 `retained` 保活。

[pegainfer-core/src/weight_loader.rs:540-568](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/weight_loader.rs#L540-L568) — `finish`：先把 `failed` 置真（毒化——失败的 finish 不能在已抽干的 op 列表上空跑重试）；`execute_uploads` 出错则 `drain_or_abort` 清理后返回；成功才翻转 `finished = true`，并打一行统计日志（上传条数、分配 API 累计耗时、执行+排水耗时）。

[pegainfer-core/src/weight_loader.rs:570-600](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/weight_loader.rs#L570-L600) — `execute_uploads`：`std::mem::take` 抽干 pending 队列逐个执行；SAFETY 注释说明为什么 unsafe 成立（目标由 slots 持有、范围在记录时校验过、owned 源已 retained 保活）；结尾 `ctx.sync()` 保证所有异步拷贝落地后才返回。

[pegainfer-core/src/weight_loader.rs:602-611](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/weight_loader.rs#L602-L611) — `take`：`assert!(self.finished)` 防止在成功 finish 前赎回；`slot.data.take()` 保证每个 slot 只能被取走一次（二次取会 panic「slot already taken」）。

[pegainfer-core/src/weight_loader.rs:744-751](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/weight_loader.rs#L744-L751) — `Drop`：第二层防线。健全性边界在 `finish` 内闭合（注释指出 Drop 可被 `mem::forget` 跳过，不能作为唯一防线）；Drop 只在 `failed` 时补一次排水。

#### 4.2.4 代码实践

1. **实践目标**：通过 GPU 集成测试理解状态机契约。
2. **操作步骤**：阅读 [pegainfer-core/tests/weight_loader_gpu.rs:12-37](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/tests/weight_loader_gpu.rs#L12-L37)（`record_after_finish_is_rejected`）：它在内存里 `safetensors::serialize` 一个 2×2 的 BF16 张量，走完 `matrix → finish → take` 全流程，然后断言 finish 之后再调 `matrix`/`vector` 必须报错。有 GPU 时可运行：
   ```bash
   cargo test --release -p pegainfer-core --test weight_loader_gpu
   ```
3. **需要观察的现象**：两条测试通过；`record_after_finish_is_rejected` 里的两个 `is_err()` 断言成立。
4. **预期结果**：无 GPU 环境下标记**待本地验证**；但测试代码本身证明了契约——`ensure_recording`（[pegainfer-core/src/weight_loader.rs:523-529](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/weight_loader.rs#L523-L529)）拒绝 `finished || failed` 状态下的一切记录。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `finish` 一进来就置 `failed = true`，而不是失败时才置？
答案：毒化设计。`execute_uploads` 用 `mem::take` 抽干了 `pending`，若中途失败而状态仍是「未完成」，再次调用 `finish` 会以为无事发生、在空列表上「成功」返回——事实上有一半上传没有落地。先毒化、成功才解除，保证失败的 finish 永远不能被重试成假成功。

**练习 2**：`take` 为什么用 `assert!` 而 `matrix` 用 `Result`？
答案：`matrix` 的失败是外部输入问题（检查点与 config 不符），属于可预期错误，用 `anyhow` 报告；`take` 在 finish 之前被调用是**调用方的编程错误**（违反状态机），用 panic 直接暴露 bug 而不是返回可被忽略的 Err。

**练习 3**：`vector` 明明也可以走 pinned 暂存，为什么选择 pageable 直传？
答案：暂存器的收益在于大块数据上重叠「填充」与「DMA」；norm 这类几 KB 的小向量，走暂存器的调度开销比拷贝本身还贵。头注释「Small tensors; uploaded as plain pageable copies」就是这条取舍的记录。

### 4.3 staging.rs：pinned 双缓冲 WeightStager

#### 4.3.1 概念说明

大矩阵（几百 MB）不能也不该一次性塞进一块 pinned 缓冲，于是 `WeightStager` 把源数据切成 32 MiB 的块，用**两块** pinned 缓冲交替工作：

```text
时间 ───────────────────────────────────────────────────>
缓冲 A:  [填 chunk0] ...... [DMA chunk0 ..............]
缓冲 B:               [填 chunk1] ...... [DMA chunk1 ......]
                              ↑ 填 chunk1 与 DMA chunk0 并行
```

如果只有一块缓冲，填充 chunk i+1 必须等 DMA chunk i 结束（宿主不能改一块正在被 DMA 读的内存）。两块缓冲加各自的 `dma_done` 事件，让宿主填充与设备搬运重叠，单块耗时从 \(t_{\text{fill}} + t_{\text{dma}}\) 变成 \(\max(t_{\text{fill}},\, t_{\text{dma}})\)。

头注释给出关键参数的来历：「measured 32 MiB geometry improves overlap and limits the two pinned buffers to 64 MiB」——32 MiB 是实测调优值，两块合计 64 MiB 封顶 pinned 内存开销。

#### 4.3.2 核心流程

`stage_chunk`（每 32 MiB 块执行一次）：

```text
idx = next; next = (next + 1) % 2          // 交替选缓冲
buf.dma_done.synchronize()                  // 等这块缓冲上一次 DMA 真正结束
fill(pool, staged)                          // FillPool 把源字节填进 pinned 缓冲
memcpy_htod_async(dst_at, staged, stream)   // 异步 H2D，立即返回
buf.dma_done.record(stream)                 // 在流上打点：拷贝完成时事件触发
```

填充本身也可能是重活（col_shard 要逐行 gather 列区间），所以 `FillPool` 是一个最多 8 线程的 rayon 线程池，只有源 ≥ 1 MiB 才值得并行分发。

#### 4.3.3 源码精读

[pegainfer-core/src/weight_loader/staging.rs:19-26](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/weight_loader/staging.rs#L19-L26) — 常量区：`BF16_SIZE`、`STAGE_BYTES = 32 << 20`（32 MiB，附实测注释）、`FILL_THREADS = 8`、`PARALLEL_FILL_MIN_BYTES = 1 << 20`（小于 1 MiB 的尾部不值得并行分发）。

[pegainfer-core/src/weight_loader/staging.rs:120-123](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/weight_loader/staging.rs#L120-L123) — `StagingBuf { pinned: PinnedHostSlice<bf16>, dma_done: CudaEvent }`：一块 pinned 缓冲配一个完成事件。

[pegainfer-core/src/weight_loader/staging.rs:143-162](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/weight_loader/staging.rs#L143-L162) — `WeightStager::new`：`ctx.ctx.alloc_pinned::<bf16>(STAGE_BYTES / BF16_SIZE)` 分配 pinned 缓冲（SAFETY 注释：DMA 读到的每个字节都由 fill 回调先初始化，复用以 `dma_done` 为门），事件带 `CU_EVENT_BLOCKING_SYNC`（宿主可在其上阻塞等待）。

[pegainfer-core/src/weight_loader/staging.rs:230-281](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/weight_loader/staging.rs#L230-L281) — `stage_chunk`：上文流程图的真身。注意三处纪律：①复用缓冲前先 `dma_done.synchronize()`；②H2D 失败时**不直接返回**，先 `drain_or_abort`（因为异步 API 的错误可能源自流上更早的工作，无法证明这次拷贝没有启动，pinned 内存不能在 DMA 飞行中被释放）；③拷贝发出后立刻 `record`，让下一轮的 `synchronize` 有据可等。

[pegainfer-core/src/weight_loader/staging.rs:164-176](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/weight_loader/staging.rs#L164-L176) — `upload_at`：把连续源切成 `STAGE_BYTES` 块循环调 `stage_chunk`，目的地址按块偏移推进。

[pegainfer-core/src/weight_loader/staging.rs:203-228](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/weight_loader/staging.rs#L203-L228) — `upload_cols_at`：col-shard 版本。每块装满尽量多的**完整行**（`rows_per_chunk = STAGE_BYTES / plan.take_b`），fill 回调里调 `FillPool::gather_cols` 逐行从源行中拷出 `[off_b, off_b+take_b)` 区间——列切分在填充阶段完成，DMA 看到的仍是连续块。

[pegainfer-core/src/weight_loader/staging.rs:48-118](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/weight_loader/staging.rs#L48-L118) — `FillPool::copy` 与 `gather_cols`：小于 1 MiB 或单线程时走标量 `copy_from_slice`；否则按 worker 均分用 `pool.scope` 并行填。`as_uninit`（L42-46）把已初始化字节安全地视作 `MaybeUninit` 交给填充目标。

[pegainfer-core/src/weight_loader/staging.rs:283-314](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/weight_loader/staging.rs#L283-L314) — `next_contiguous_chunk`：把多个源切片拼装成不超过 limit 字节的下一块（`ByteWeightStager` 用它合并多个原始字节源），其行为由纯 CPU 测试 `contiguous_chunks_preserve_source_bytes`（L496-517）锁定。

[pegainfer-core/src/weight_loader/staging.rs:427-450](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/weight_loader/staging.rs#L427-L450) — `Drop for WeightStager` 与 `drain_or_abort`：注释解释了为什么必须自己排水——`PinnedHostSlice` 的 Drop 只等它自己内嵌的从未记录过的事件，等不到我们的 `dma_done`；排水失败就 `std::process::abort()`，**宁可崩溃也不在 DMA 飞行中释放 pinned 内存**。

#### 4.3.4 代码实践

1. **实践目标**：建立「大矩阵会被切成多少块」的量化直觉。
2. **操作步骤**：
   - 手算：一个 \(8192 \times 8192\) 的 bf16 矩阵，字节数 = \(8192 \times 8192 \times 2 = 128\,\text{MiB}\)，恰好 4 个 32 MiB 块；
   - 再算一个不整除的：1 GiB 权重 = 32 个整块；1.5 GiB = 48 块（47 整块 + 1 个 16 MiB 尾块）；
   - 对照 `upload_at` 的 `src.chunks(STAGE_BYTES)` 确认最后一块是短块（`chunk.len() <= STAGE_BYTES`）。
3. **需要观察的现象**：`finish` 打印的 `weight load: N uploads` 日志里，`N` 是**记录的张量条数**（每条 `matrix` 调用一条 pending），而暂存器内部块数是另一回事——一个 128 MiB 的矩阵是 1 条 upload、4 次 `stage_chunk`。区分这两个计数。
4. **预期结果**：能写出换算公式：块数 = \(\lceil \text{rows} \times \text{cols} \times 2 / 32\,\text{MiB} \rceil\)。运行时日志**待本地验证**（需 GPU 与真实检查点）。

#### 4.3.5 小练习与答案

**练习 1**：如果 `STAGE_BYTES` 从 32 MiB 改成 4 MiB，对性能和内存各有什么影响？
答案：pinned 开销从 64 MiB 降到 8 MiB，但每块的 DMA 与填充都变小，固定开销（事件同步、kernel 启动、并行分发阈值）占比上升，重叠效率下降。32 MiB 是实测平衡点（L20-21 注释）。

**练习 2**：`stage_chunk` 里如果去掉开头的 `dma_done.synchronize()`，最坏会发生什么？
答案：宿主可能在上一轮 DMA 还在读这块缓冲时就开始填充新数据，DMA 搬走的是新旧混合的字节——权重静默损坏，且没有任何报错。这正是双缓冲必须配事件门的原因。

**练习 3**：`upload_cols_at` 为什么以「行」为单位切块而不是按字节切块？
答案：col-shard 的目标布局是「每行取 take 列」的紧凑矩阵；按字节切块会切断行边界，块内前后数据不构成完整的 gather 单元。以整行为块单位，fill 回调独立完成行级 gather，每块 DMA 仍然写连续的目的区间。

### 4.4 三道防线：prepare 校验、流一致性、失败排水与劝告式预取

#### 4.4.1 概念说明

上传是 `unsafe` 的：`memcpy_htod_async` 只认裸设备地址，不看类型、不看边界。core 把全部危险前置为三个检查点，把 unsafe 的合同写得极小：

1. **记录时校验**（`prepare`/`prepare_cols`/`prepare_bytes`）：换算目标地址的同时验证源长度、对齐、目标区间在界内。
2. **流一致性**（`ensure_uploadable`）：目标必须分配在暂存器自己的流上，且当前线程没有流覆盖。
3. **失败排水**（`drain_or_abort`）：任何拷贝错误后先同步流（或崩溃），绝不让 pinned 内存在 DMA 飞行中被释放。

另外还有一个「劝告式」组件 `WeightPrefetch`：装载前的并行预读，把检查点页提前带进页缓存，**加载路径从不依赖它**。

#### 4.4.2 核心流程

```text
prepare(src, dst, dst_offset)
  ├─ ensure_uploadable: dst 的流 == stager 的流？无线程局部流覆盖？
  ├─ src.len() 是 bf16 整数倍？
  ├─ dst_offset*2 + src.len() <= dst.len()*2 ？（无溢出地加）
  └─ 返回 dst_ptr + dst_offset*2    （裸地址交给 deferred upload_at）

ensure_uploadable 的两条检查
  ├─ Arc::ptr_eq(dst.stream(), stream)   ← 流一致性
  └─ !has_stream_override()              ← 排除线程局部流切换
```

#### 4.4.3 源码精读

[pegainfer-core/src/weight_loader/staging.rs:316-328](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/weight_loader/staging.rs#L316-L328) — `ensure_uploadable`：流一致性检查点。注释点明动机——暂存器的事件和目标的流序分配都只对**本流**上的工作有序，目标若分配在别的流上，事件与分配的先后关系就没有意义。

[pegainfer-core/src/weight_loader/staging.rs:350-377](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/weight_loader/staging.rs#L350-L377) — `prepare`：连续上传的校验。注意 `dst_offset.checked_mul(BF16_SIZE).and_then(checked_add)` 的写法——用 checked 运算保证加法溢出也会被拦下，而不是回绕成一个小数通过检查。L373-374 的注释解释了为何拿到 `device_ptr_mut` 守卫后可以立刻丢弃（运行时上下文关闭了 cudarc 事件跟踪）。

[pegainfer-core/src/weight_loader/staging.rs:379-425](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/weight_loader/staging.rs#L379-L425) — `prepare_cols`：跨步上传校验，产出 `ColShardPlan { stride_b, off_b, take_b, rows, dst_at }`。校验包括：源长度是「总列数 × 2 字节」的整数倍、列区间在界内、**单行宽度 take_b 必须落在 1..=STAGE_BYTES**（一行必须装进一块暂存缓冲）、`rows × take == dst.len()`。

[pegainfer-core/src/weight_loader.rs:120-139](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/weight_loader.rs#L120-L139) — `chunk_looks_resident`：预取侧的驻留探测。用 `preadv2` + `RWF_NOWAIT` 读一页：内核若需要真正去磁盘取页会立刻返回 `EAGAIN`，于是「一页代表一整块」——该 16 MiB 块要不要预读，一次系统调用就能判断。

[pegainfer-core/src/weight_loader.rs:141-258](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/weight_loader.rs#L141-L258) — `WeightPrefetch::spawn`：8 线程、16 MiB 块的并行预读；读满 512 MiB 后测速率，低于 1 GB/s（预读抢不过与它共享设备的加载路径）就自我撤销（置 cancel 并打日志「standing down, leaving the device to the load path」）。全部失败聚合成一条 warning，绝不上抛——它是**劝告式**的。

[pegainfer-core/src/weight_loader.rs:260-292](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/weight_loader.rs#L260-L292) — `Drop for WeightPrefetch`：置 cancel、join 所有 worker、聚合 panic/读错误为一条 warning。加载主体不需要等预取——Drop 时取消即可。

#### 4.4.4 代码实践

1. **实践目标**：理解流一致性检查防御的具体事故。
2. **操作步骤**：阅读 [pegainfer-core/src/weight_loader/staging.rs:316-328](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/weight_loader/staging.rs#L316-L328)，再回到 u2-l1 讲过的 `DeviceContext`：`ctx.stream.alloc` 做的是**流序分配**（stream-ordered allocation）。然后回答：若目的 `CudaSlice` 分配在流 S1、暂存器在流 S2 上发起 `memcpy_htod_async`，两条检查各拦住什么？
3. **需要观察的现象**：`Arc::ptr_eq` 拦住「跨流分配」——S2 上的事件管不到 S1 的分配时序，拷贝可能先于分配生效；`has_stream_override` 拦住「线程局部流切换」下地址语义被改写的情形。
4. **预期结果**：能复述两条检查各防一种「地址存在但时序错乱」的事故；无需运行即可从代码推导（源码阅读型实践）。

#### 4.4.5 小练习与答案

**练习 1**：`prepare_cols` 为什么要求 `take_b ∈ 1..=STAGE_BYTES`（一整行装进一块缓冲）？
答案：`upload_cols_at` 的分块以行为单位（`rows_per_chunk = STAGE_BYTES / take_b`）；若一行超过暂存缓冲宽度，这个算法无法装块。校验把算法前提显式化，违反时在记录期报错而不是执行期越界。

**练习 2**：预取线程读满 512 MiB 才测速率，为什么不在第 1 MiB 就测？
答案：常量旁注释写明「Read enough that the rate is the device's and not thread start-up」——太少的数据测出来的是线程启动开销，不是设备真实吞吐，会让撤销判断失真。

**练习 3**：`drain_or_abort` 为什么宁可 `abort()` 也不返回 Err？
答案：此时 DMA 可能仍在飞行，而调用路径即将释放 pinned 缓冲；一旦释放，DMA 读的是已归还的宿主内存——这是无法恢复的内存安全隐患。`Err` 意味着「调用方可以继续清理并继续跑」，恰恰是最危险的选择。abort 是 fail closed。

## 5. 综合实践

把本讲全部内容串成一条链。三个子任务：

**任务 A：画数据流图。** 通读两个文件后，画出并核对下面的全景图（每跳标注拷贝类型与同步点）：

```text
磁盘 checkpoint
  │ ①WeightPrefetch：劝告式 8 线程预读页缓存（可选、可自撤、绝不阻塞）
  ▼
model.safetensors(.index.json)
  │ ②load_shard_info[_fixed]：纯元数据，无拷贝
  ▼
Mmap（零拷贝映射）
  │ ③deserialize_shards：只解析 header，无 payload 拷贝
  ▼
SafeTensors 视图 ── find_tensor ──> TensorView（零拷贝字节片）
  │ ④对齐处理：对齐→Cow::Borrowed（零拷贝）；非对齐→Cow::Owned（宿主解码一次）
  ▼
StagedWeightLoader 记录：shape/dtype 校验 → alloc_timed（流上分配）→ prepare*（边界校验）
  │                                   [同步点：无 —— 全部推迟]
  ▼ finish → execute_uploads
WeightStager 双缓冲循环（每 32 MiB 块）：
  │ ⑤dma_done.synchronize()（同步点：等上一轮本缓冲 DMA）
  │ ⑥FillPool 填充 pinned 缓冲（宿主内存拷贝；≥1 MiB 并行）
  │ ⑦memcpy_htod_async（异步 H2D，pinned→显存）
  │ ⑧dma_done.record（流上打点）
  ▼
execute_uploads 末尾 ctx.sync()（同步点：全部拷贝落地）
  ▼
take/take_vec → DeviceMatrix / DeviceVec（所有权移交模型 crate）
```

**任务 B：round-trip 校验方式。** 阅读 [pegainfer-core/tests/weight_loader_gpu.rs:39-77](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/tests/weight_loader_gpu.rs#L39-L77)（`unaligned_vector_payload_roundtrips`）：它特意把序列化后的档案放到奇/偶两种偏移上，挑出 payload 指针为奇数的那份，强制 `vector` 走 owned-decode 路径；`finish` 后用 `ctx.stream.clone_dtoh` 把设备数据拷回宿主，**按 `to_bits()` 的 u16 位模式逐元素断言**。这就是本讲的「校验哲学」缩影：位级忠实、强制分支覆盖、D2H 回读闭环。无 GPU 时写出该测试的三步断言结构即可（**待本地验证**运行）。

**任务 C：消费方调研——谁解析 config、谁重排张量、显存怎么分账。** 在仓库根目录执行：

```bash
grep -rn "StagedWeightLoader\|WeightPrefetch" pegainfer-qwen3/src
```

对照 [pegainfer-qwen3/src/weights/load.rs:15-20](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-qwen3/src/weights/load.rs#L15-L20)（导入清单）与调用序列：

- **config 由模型 crate 解析**：[pegainfer-qwen3/src/weights/load.rs:76](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-qwen3/src/weights/load.rs#L76) 的 `Config::from_file(model_path)` 读的是模型自己的 config.json；core 完全不知道 config，它只接收调用方算好的 `(rows, cols)` 并据此校验检查点。
- **张量融合/切分由模型 crate 决定**：qkv 融合用 `loader.fused_rows`（[load.rs:166](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-qwen3/src/weights/load.rs#L166)）、gate_up 融合在 [load.rs:192](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-qwen3/src/weights/load.rs#L192)、TP 列切分用 `col_shard`（[load.rs:215](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-qwen3/src/weights/load.rs#L215)）、最终 [load.rs:273](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-qwen3/src/weights/load.rs#L273) `loader.finish()`。core 只提供「融合/切分的机械」，不决定「融合/切分的方案」。
- **显存分账是「先权重、后余量」**：core 里没有任何预算协商代码。qwen3 在权重落位后用 `cuMemGetInfo` 查询剩余显存来推导 KV 预算——见 [pegainfer-qwen3/src/weights.rs:678-689](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-qwen3/src/weights.rs#L678-L689)（kernel-call-trace 下的 0.85 启发式）与 [pegainfer-qwen3/src/weights.rs:927-931](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-qwen3/src/weights.rs#L927-L931)（`mem_info_bytes`，生产路径同样以它为输入，并在日志里报告「KV cache 占剩余显存的百分比」）。协调点就是**顺序**：先装载权重（`StagedWeightLoader`），再按剩余容量构造 KV 池——这也是 `finish` 日志里 `alloc_api_wall` 计时有意义的原因：它精确计量了权重吃掉配额的那段时间。

补充检索：`grep -rln "StagedWeightLoader" pegainfer-kimi-k2/src` 无匹配——说明 kimi-k2 不走暂存器路径（它面对的是 Marlin INT4 等更特殊的权重形态）。其他模型 crate 的使用情况留作练习，以检索结果为准。

## 6. 本讲小结

- **分工**：weight_loader.rs 管「解析与校验」（分片索引、mmap、SafeTensors 视图、对齐处理、状态机），staging.rs 管「搬运与重叠」（pinned 双缓冲、并行填充、流一致性、失败排水）。
- **状态机**：`StagedWeightLoader` 是「记录 → finish → take」三段式；所有 shape/dtype/boundary 校验发生在任何字节上传之前，`finish` 先毒化再执行，`take` 只能在成功 finish 后赎回且每槽一次。
- **双缓冲**：两块 32 MiB pinned 缓冲以 `dma_done` 事件为门交替使用，把宿主填充与异步 H2D 重叠；col-shard 的列 gather 在填充阶段完成，DMA 永远搬连续块。
- **三道防线**：`prepare*` 的边界校验（含 checked 溢出防护）、`ensure_uploadable` 的流一致性（同流分配 + 无流覆盖）、`drain_or_abort` 的 fail-closed 排水（宁可 abort 不释放飞行中 DMA 的 pinned 内存）。
- **健全性边界闭合在 `finish`，Drop 只是第二层**——因为 Drop 可被 `mem::forget` 跳过。
- **core 不做显存预算协商**：模型 crate 先装载权重、再用 `cuMemGetInfo` 的剩余读数推导 KV 预算；config 解析与张量融合/切分方案也都归模型 crate，core 提供机械并兜住校验。

## 7. 下一步学习建议

本讲装好了权重；下一讲 **u3-l4** 转向推理出口的三个小配套：`rope.rs`（RoPE cos/sin 表上载，会再次用到 `DeviceVec::from_host` 的 pageable 路径）、`decodable_vocab.rs`、`eos.rs`。随后 **u4-l1**（CUDA Graph）解释为什么 `alloc_timed` 的流上分配与 `has_stream_override` 检查在图捕获期间另有含义。若想横向扩展，可继续阅读 `weight_loader.rs` 尾部的直接加载家族（`load_tensor_2d_row_shard`/`col_shard`/`row_stitch` 等，L817-1048）与 `ByteWeightStager`（L34-54），并检索它们在 kimi-k2、deepseek-v2-lite 等模型 crate 里的消费点。
