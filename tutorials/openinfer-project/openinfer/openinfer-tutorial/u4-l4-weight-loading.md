# 权重加载：safetensors 到显存

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清一条 checkpoint 从磁盘到显存的完整管线：shard 探测 → mmap → 反序列化 → 上传，以及每一步在源码里的位置。
2. 区分两条上传通道：`load_tensor_2d_*` 一族的 pageable 直传，与 `StagedWeightLoader` 背后的 pinned 双缓冲 staging，并解释后者为什么快。
3. 看懂张量并行（TP）下的三种切分几何：row shard、col shard、row stitch，以及 qwen3/qwen35 各自在哪些投影上用了哪种切法。
4. 理解 `WeightPrefetch` 的 advisory 并行预读设计：RWF_NOWAIT 驻留探测、速率地板自动退出、Drop 时的错误聚合。
5. 动手写一个「TP2 两种切分拼回原矩阵」的往返测试。

## 2. 前置知识

本讲建立在 u4-l1（张量与设备层）之上，先回顾并补充几个概念：

- **safetensors 格式**：HuggingFace 生态的标准权重格式。文件开头 8 字节是小端 u64 的 header 长度，随后是一段 JSON（记录每个张量的名字、dtype、shape 和数据区间偏移），再往后是连续的原始数据。反序列化只解析 header，张量数据本身是文件字节上的**零拷贝视图**。
- **mmap（内存映射文件）**：把文件按页映射进进程地址空间。第一次访问某页才触发缺页、从磁盘读入 page cache。好处是不用一次 `read` 整个几 GB 的文件进堆内存；代价是顺序扫描时缺页是「按需」的。
- **pageable 与 pinned 主机内存**：普通 Rust `Vec` 的内存在内核看来是可换页的（pageable）。GPU 的 DMA 引擎不能直接从 pageable 地址搬运，驱动只能先把数据拷进自己内部的一块锁定（pinned）缓冲再发起 DMA。而 `cudaHostAlloc` 分配的 pinned 内存可以被 DMA 直接寻址——CPU 填充与 DMA 搬运可以**重叠**。
- **bf16**：PegaInfer 权重的一统 dtype（回顾 u4-l1），每个元素 2 字节小端。`bf16 → f32` 无损，所以测试里常用「位级比对」验证搬运正确性。
- **TP（tensor parallel）切分预告**：HF 的线性层存 \( W \in \mathbb{R}^{\text{out} \times \text{in}} \)，前向是 \( y = x W^\top \)。多卡 TP 时每张卡只持 \( W \) 的一片：切**行**（输出维）还是切**列**（输入维），决定了下一层需不需要 all-reduce。本讲只讲「怎么把切好的片搬上显存」；通信与调度细节留给 u9-l4。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `pegainfer-core/src/weight_loader.rs` | 加载管线全部入口：shard 探测、mmap、反序列化、简单路径上传函数族、`StagedWeightLoader`、`WeightPrefetch` |
| `pegainfer-core/src/weight_loader/staging.rs` | pinned 双缓冲 `WeightStager`、填充线程池 `FillPool`、`prepare`/`prepare_cols` 校验 |
| `pegainfer-core/tests/weight_loader_gpu.rs` | GPU、免模型权重 的契约测试（本讲实践的样板） |
| `pegainfer-qwen3/src/weights/load.rs` | `StagedWeightLoader` 的最大真实消费者：qwen3 权重装载全流程 |
| `pegainfer-qwen35/src/weights/layers.rs` | `load_tensor_2d_row_shard/col_shard/row_stitch` 简单路径的消费者 |
| `pegainfer-kernels/src/tensor.rs` | `DeviceMatrix::from_host` / `DeviceVec::from_safetensors`——一切上传的物理终点 |

---

## 4. 核心概念与源码讲解

### 4.1 safetensors 与加载管线：shard 探测 → mmap → 反序列化

#### 4.1.1 概念说明

一个 HF checkpoint 目录里通常有两种布局：

1. **单文件**：只有 `model.safetensors`，所有张量在一个文件里。
2. **分片**：`model-00001-of-000NN.safetensors` 等多个文件，外加一个 `model.safetensors.index.json`，其中的 `weight_map` 字段记录「张量名 → 所在分片文件名」。

PegaInfer 不把这两种布局当成两套代码处理，而是统一成一对数据：`(分片路径列表, 张量名 → 分片下标的映射)`。之后所有读取函数都只面对这对数据，布局差异在入口处就被抹平了。

#### 4.1.2 核心流程

```
load_shard_info(model_path)
  ├─ 存在 model.safetensors → 单文件布局：返回 ([该文件], 空 weight_map)
  └─ 否则解析 index.json 的 weight_map
       └─ 张量名 → 分片文件名 →（去重编号）→ 张量名 → 分片下标
mmap_shards(paths)          → Vec<Mmap>：只读映射，零拷贝
deserialize_shards(mmaps)   → Vec<SafeTensors>：每个张量是 mmap 字节上的视图
find_tensor(shards, map, name) → TensorView（真正的字节访问入口）
```

注意：这条链**不搬任何权重数据**。真正的搬运发生在 4.2 的上传通道里。

#### 4.1.3 源码精读

探测入口 [pegainfer-core/src/weight_loader.rs:L57-L89](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-core/src/weight_loader.rs#L57-L89)：先试单文件路径（L58-61），命中就直接返回空 `weight_map`；否则读 index.json，遍历 `weight_map`，把分片文件名去重编号成 `Vec<String>` 与 `HashMap<String, usize>`（L71-86）。

mmap 与反序列化是一对可链式调用的函数。[mmap_shards](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-core/src/weight_loader.rs#L301-L319) 打开每个文件做只读映射，并打印总映射量日志；[deserialize_shards](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-core/src/weight_loader.rs#L322-L329) 只解析 safetensors header，返回的 `SafeTensors` 借用 mmap 的生命周期。

```rust
// weight_loader.rs:L306-L310（节选）
let file = fs::File::open(p)?;
// SAFETY: we keep the Mmap alive for the duration of model loading,
// and the file is not modified concurrently.
unsafe { Mmap::map(&file) }
```

安全性注释写明了契约：mmap 必须活过整个加载期、文件不能被并发修改。

按名取张量的 [find_tensor](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-core/src/weight_loader.rs#L331-L349) 有两条路径：weight_map 命中则直接索引对应分片；**未命中则回退为顺序扫描所有分片**（L341-347）——这正是单文件布局可以传空 map 的原因。

字节到 bf16 的翻译有两层。[tensor_bf16_bytes](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-core/src/weight_loader.rs#L351-L367) 只做 dtype 必须是 BF16、字节数必须是偶数的校验，返回原始字节；[tensor_bf16_cow](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-core/src/weight_loader.rs#L370-L394) 在其上处理对齐：载荷地址对齐 `bf16` 时零拷贝借用为 `&[bf16]`，否则逐元素 `from_le_bytes` 解码进 owned 缓冲。注释点明原因：safetensors 完全允许数据落在任意字节偏移上，而未对齐的 `&[bf16]` 视图是 UB。

最后是给「文件名对不上」准备的 [load_shard_info_fixed](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-core/src/weight_loader.rs#L1014-L1046)：某些模型（doc 注释点名 Qwen3.5）的 index.json 写的是 `model.safetensors-00001-of-00002.safetensors`，而实际文件叫 `model-00001-of-00002.safetensors`；该函数在路径不存在时尝试把前缀 `model.safetensors-` 替换成 `model-` 再探一次。

qwen3 真实调用侧把这几步串在 [pegainfer-qwen3/src/weights/load.rs:L102-L107](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-qwen3/src/weights/load.rs#L102-L107)：`load_shard_info` → `WeightPrefetch::spawn`（4.4 讲）→ `mmap_shards` → `deserialize_shards`。

#### 4.1.4 代码实践

**实践目标**：亲手看清一个真实 checkpoint 的索引结构，把 `load_shard_info` 的输出在纸上画出来。

1. **操作步骤**：找一个本地的分片模型目录（例如 `models/Qwen3-4B` 若有多个分片文件；没有的话，从 HuggingFace 任意分片模型页下载 `model.safetensors.index.json` 单个文件即可）。用 `jq '.weight_map | to_entries[:5]' model.safetensors.index.json` 查看前五条映射；统计 `jq '.weight_map | map_values(. ) | group_by(.) | length'` 得到分片个数。
2. 对照 `load_shard_info`（L71-86）在纸上写出：`shard_files` 会是哪几个路径、`weight_map` 里 `model.layers.0.self_attn.q_proj.weight` 映射到下标几。
3. 若模型是单文件布局，确认 `weight_map` 为空、`find_tensor` 走哪条分支（L336 与 L341-347）。

**需要观察的现象**：weight_map 的 value 是文件名字符串而非下标，下标化发生在 `load_shard_info` 内部的 `file_to_idx` 去重里。

**预期结果**：能不看代码复述「布局差异在入口被抹平成 `(Vec<String>, HashMap<String, usize>)`」。本实践不需要 GPU；日志现象（`Memory-mapped N shard(s)` 那行）需启动引擎才能看到，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么单文件布局可以传空 `weight_map`？

答案：`find_tensor` 对未命中的名字会回退为顺序扫描所有分片（L341-347）。单文件只有一个分片，扫描必然命中或报「不存在」。

**练习 2**：`tensor_bf16_cow` 为什么不能对未对齐载荷直接 `cast` 成 `&[bf16]`？

答案：Rust 的引用必须满足对齐，未对齐的 `&[bf16]` 是即时 UB。safetensors 的数据区间可以起于任意字节偏移，所以对齐时零拷贝借用、未对齐时复制并逐元素小端解码（L377-393）。

**练习 3**：`load_shard_info_fixed` 修的是什么 bug？谁会遇到？

答案：index.json 记录的分片文件名与磁盘实际文件名不一致（`model.safetensors-` 前缀 vs `model-` 前缀），Qwen3.5 的 checkpoint 有此现象；修复是存在性探测失败后做一次前缀替换（L1017-1043）。

---

### 4.2 两条上传通道：pageable 直传与 pinned staging 双缓冲

#### 4.2.1 概念说明

字节视图到手后，只剩一件事：搬到显存。仓库里有两条通道：

1. **简单路径**：`load_tensor_2d` / `load_tensor_2d_row_shard` / `load_tensor_2d_col_shard` / `load_tensor_2d_row_stitch` 等自由函数。每调用一次，立刻在主机侧准备好 `&[bf16]`，然后 `DeviceMatrix::from_host` 里一次 `clone_htod`（pageable 拷贝）完事。qwen35 走这条路。
2. **staged 路径**：`StagedWeightLoader`。先把**所有**权重的「上传意图」记录成待办（分配显存、校验形状、记下源字节），全部校验通过后由 `finish()` 一次性执行上传。上传本身经过 `WeightStager` 的两块 32 MiB pinned 缓冲流水化。qwen3、gemma4 走这条路。

staged 路径的动机有两层：

- **校验前置**：任何一个张量的 dtype/形状不对，都在**分配显存之前**就报错，不会留下半装状态的模型。
- **流水线重叠**：pageable 的 `clone_htod` 里，驱动要先把 pageable 数据拷进内部 pinned 缓冲才能发起 DMA，CPU 拷贝和 DMA 大致串行；staging 自己持有两块 pinned 缓冲，一块在 DMA 时另一块由 CPU 填充，两者重叠。粗略地，设每块搬运耗时 \( t_{\text{fill}} \)（CPU 填充）与 \( t_{\text{dma}} \)（H2D 搬运），\( n \) 块总量：

\[ T_{\text{serial}} \approx \sum_{i=1}^{n} \left( t_{\text{fill},i} + t_{\text{dma},i} \right), \qquad T_{\text{double}} \approx t_{\text{fill},1} + \sum_{i=2}^{n} \max\!\left( t_{\text{fill},i},\ t_{\text{dma},i-1} \right) + t_{\text{dma},n} \]

即吞吐上限从「两者之和的倒数」变成 \( \max(\text{fill 速率}, \text{DMA 速率}) \) 的倒数。

#### 4.2.2 核心流程

`StagedWeightLoader` 是一台两阶段状态机：

```
记录阶段（record）：matrix / fused_rows / col_shard / vector
  每次调用 = find_tensor → 校验 dtype/shape → alloc_timed 分配显存
           → prepare/prepare_cols 校验边界并拿到目的地址
           → pending.push(上传意图) → 返回 SlotId
        （此时显存尚未写入！）

执行阶段：finish()
  failed = true（先毒化，防中途失败后重试已清空的 pending）
  execute_uploads(): 逐条执行 pending，最后 ctx.sync()
  成功 → failed = false, finished = true；失败 → drain_or_abort + 返回 Err

兑换阶段：take(SlotId) / take_vec(VecSlotId)
  assert!(finished)；把 CudaSlice 从槽里取出组装成 DeviceMatrix/DeviceVec
```

`WeightStager` 的双缓冲循环（staging.rs）：

```
stage_chunk(bytes, dst_at, fill):
  idx = next; next = (next+1) % 2        # 轮转两块缓冲
  bufs[idx].dma_done.synchronize()       # 等这块缓冲上一次 DMA 结束
  fill(FillPool, bufs[idx] 的 MaybeUninit 视图)   # CPU 多线程填充
  memcpy_htod_async(dst_at, staged, stream)       # 发起 DMA
  bufs[idx].dma_done.record(stream)      # 记事件，供下轮等待
```

#### 4.2.3 源码精读

状态机本体 [StagedWeightLoader](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-core/src/weight_loader.rs#L482-L494)：持有 `slots`/`vec_slots`（已分配显存）、`pending`（上传意图）、`retained`（owned 解码的未对齐向量，需活到上传结束）与 `finished`/`failed` 两个标志。doc 注释写明设计：「每个方法在加载边界检查 dtype 与 config 推导的维度；载荷不要求对齐；上传在 `finish` 里、即所有张量都校验通过之后才执行」。

以最简单的 [matrix](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-core/src/weight_loader.rs#L631-L638) 为例：

```rust
// weight_loader.rs:L631-L638（节选）
let src = self.tensor_2d(name, rows, cols)?;   // 形状/dtype 校验（L607-L619）
let mut data = self.alloc_timed(rows * cols, name)?;  // 先分配显存
let dst_at = staging::prepare(&self.ctx.stream, src, &mut data, 0)?; // 校验边界
self.pending.push(PendingUpload::Contiguous { src, dst_at });        // 只记不做
Ok(self.push_slot(data, rows, cols))
```

[finish](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-core/src/weight_loader.rs#L535-L562) 的毒化顺序值得细读：入口先 `self.failed = true`，成功走完 `execute_uploads` 才翻回 `false` 并置 `finished = true`——中途失败后再次 `finish` 会被拒（pending 已被 `std::mem::take` 清空，重试是空转）。失败分支调 `staging::drain_or_abort` 保证没有悬空的在途 DMA。成功时打印一条可观测日志：`weight load: N uploads, alloc_api_wall Xms, execute_and_drain_wall Yms`（L555-560）。

[execute_uploads](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-core/src/weight_loader.rs#L564-L594) 分派三种 `PendingUpload`：`Contiguous` 走 stager 的整段上传，`ColShard` 走带 stride 的列聚集上传，`Vector` 是小张量、直接 `memcpy_htod_async` pageable 拷贝（doc 注释 L707 明说小张量不值得 staging）。结尾 `ctx.sync()` 收尾。

[take](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-core/src/weight_loader.rs#L597-L605) 用 `assert!(self.finished)` 强制兑换必须在成功 finish 之后——这是断言而非 `Result`，因为提前 take 拿到的是未填充的垃圾显存，属于程序错误而非运行时风险。

双缓冲的物理实现在 [pegainfer-core/src/weight_loader/staging.rs](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-core/src/weight_loader/staging.rs#L19-L26)。三个关键常量：每块 staging 32 MiB（注释：实测的几何，改善重叠且把两块 pinned 缓冲限制在 64 MiB）；填充线程队最多 8 线程；小于 1 MiB 的尾巴不值得并行分发。[WeightStager](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-core/src/weight_loader/staging.rs#L135-L162) 构造时就分配好两块 pinned `StagingBuf`（各配一个 `CU_EVENT_BLOCKING_SYNC` 的 `dma_done` 事件）。

核心循环 [stage_chunk](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-core/src/weight_loader/staging.rs#L233-L281)：轮转选缓冲 → 等该缓冲的 `dma_done` → `fill` 回调填充（来源是 `FillPool`）→ `memcpy_htod_async` → 记 `dma_done`。错误处理很讲究：async 拷贝失败时注释指出「async API 的错误可能源自流上更早的工作，不能证明这次拷贝没启动」，所以调 `drain_or_abort` 排空整个流而不是直接返回。

填充侧的 [FillPool](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-core/src/weight_loader/staging.rs#L48-L118) 是个 rayon 线程池，提供两个原语：`copy`（整段拷贝）和 `gather_cols`（按 stride 做列聚集，即 col shard 的填充）。大于 1 MiB 的工作切成 `div_ceil(workers)` 份并行。

校验与目的地址由三个 prepare 函数负责。[prepare](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-core/src/weight_loader/staging.rs#L352-L377) 检查源长度是 bf16 整数倍、`dst_offset + src` 不越界，返回设备地址；[ensure_uploadable](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-core/src/weight_loader/staging.rs#L318-L328) 拒绝两种情况：目的 buffer 不在 stager 的流上分配（事件与流序分配只对该流有序）、线程局部流覆盖激活时（衔接 u4-l1 的 `StreamOverrideGuard`——staging 不支持在覆盖作用域里跑）。

收尾的防御在 [Drop for WeightStager](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-core/src/weight_loader/staging.rs#L427-L441) 与 [drain_or_abort](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-core/src/weight_loader/staging.rs#L443-L450)：事件排空失败就 `std::process::abort()`——宁可整个进程死掉，也不在 DMA 还在飞行时释放 pinned 内存（那是 use-after-free 级别的内存不安全）。

顺带一提 [ByteWeightStager](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-core/src/weight_loader.rs#L36-L54)：同一套双缓冲对**原始字节**（非 bf16，例如量化权重）开放的薄封装，把多个源切片拼接后经 `upload_slices_at` 上传。

#### 4.2.4 代码实践

**实践目标**：跑通（或精读）staged 路径的契约测试，理解状态机的每条防线。

1. **操作步骤**：打开 [pegainfer-core/tests/weight_loader_gpu.rs](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-core/tests/weight_loader_gpu.rs#L13-L37)。注意它**不需要模型权重**：测试用 `safetensors::serialize` 在内存里现场造了一个 2×2 的 bf16 张量（L15-20）——这正是本讲综合实践的样板。有 GPU 的机器上运行：

   ```bash
   cargo test --release -p pegainfer-core --test weight_loader_gpu -- --nocapture
   ```

2. 同时运行纯 CPU 的模块内单测（不需要 GPU，验证对齐/分块逻辑）：

   ```bash
   cargo test --release -p pegainfer-core --lib weight_loader
   ```

3. **需要观察的现象**：`record_after_finish_is_rejected` 里 `finish` 之后再调 `matrix`/`vector` 必须报错；[unaligned_vector_payload_roundtrips](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-core/tests/weight_loader_gpu.rs#L40-L77) 用「奇偶偏移两次放置序列化档案、挑出载荷地址为奇数的那份」的技巧强制走 owned 解码路径，并最终用 `to_bits()` 位级比对往返结果。

**预期结果**：两组测试通过；能口头说出测试名对应保护的不变量（「finish 后禁止再记录」「未对齐载荷解码后位级无损」）。GPU 用例在无 GPU 环境无法运行，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `take` 之前必须 `finish`，而且用的是 `assert!` 而不是返回 `Result`？

答案：显存在记录阶段就已分配但内容未写入，上传发生在 `finish`（L535-562）；提前 take 拿到的是未定义数据。这是调用方违反协议，属于 bug 而非可恢复错误，所以 `take` 用 `assert!(self.finished)` 直接 panic（L598）。

**练习 2**：`finish` 为什么一进来就把 `failed` 置 `true`，成功后才翻回来？

答案：`execute_uploads` 用 `std::mem::take` 清空了 `pending`；如果中途失败后允许再次 `finish`，第二次调用会对着空列表「假装成功」。先毒化再执行，失败状态下的任何再次 finish 都会被入口检查拒绝（L536-542 及注释）。

**练习 3**：`ensure_uploadable` 拒绝「目标 buffer 在别的流上分配」和「线程局部流覆盖激活」，各自的理由是什么？

答案：stager 的事件和 cudarc 的流序分配只相对 stager 自己的流有序，跨流目的地的写入顺序没有保证；流覆盖（u4-l1 的 `StreamOverrideGuard`）会改变 `active_cu_stream` 的取值，而 staging 的 DMA 绑定在构造时的流上，两者不一致会写错流（staging.rs L316-328 及注释）。

---

### 4.3 TP 切分几何：row shard、col shard 与 row stitch

#### 4.3.1 概念说明

权重矩阵按 \( W \in \mathbb{R}^{R \times C} \) 行主序存储（HF 惯例：行=输出维，列=输入维，前向 \( y = xW^\top \)）。TP 世界大小为 \( N \) 时，本仓的命名**完全按存储几何**：

| 函数 | 切的是 | 每卡持有 | 典型用途 | 通信含义 |
|------|--------|----------|----------|----------|
| `load_tensor_2d_row_shard` | 行区间（输出维） | \( W[r\frac{R}{N}:(r+1)\frac{R}{N},\ :] \) | q/k/v_proj、gate/up_proj | 各卡算自己的输出切片，无需立即通信 |
| `load_tensor_2d_col_shard` | 列区间（输入维） | \( W[:,\ r\frac{C}{N}:(r+1)\frac{C}{N}] \) | o_proj、down_proj | 各卡输出是部分和，需要 all-reduce |
| `load_tensor_2d_row_stitch` | 多段行区间按序拼接 | 若干行段连接 | qwen35 融合 QKV 的头局部抽取 | 同 row shard |

直觉：**下游消费谁的输出，就切谁的行；上游喂给自己的是谁的输出，就切谁对应的列**。qwen3 里注意力头被切到各卡（q/k/v 行切），于是 o_proj 必须持有「本卡那部分头」对应的输入列（列切）；MLP 的中间维被切（gate/up 行切），down_proj 就列切。传输量上两种切法都是 \( \frac{R \cdot C}{N} \) 个元素，差别在访问模式：行切是一段连续 memcpy；列切要**逐行 gather**一小段，是跨步访问。

#### 4.3.2 核心流程

简单路径的两个切分函数（伪代码）：

```
row_shard(name, row_offset, rows):
  (total_rows, cols) = 张量实际形状，校验 row_offset+rows ≤ total_rows
  host = elems[row_offset*cols .. (row_offset+rows)*cols]   # 连续切片
  DeviceMatrix::from_host(ctx, host, rows, cols)             # 一次 pageable 拷贝

col_shard(name, col_offset, cols):
  (rows, total_cols) = 实际形状，校验 col_offset+cols ≤ total_cols
  host = 逐行 copy elems[row*total_cols + col_offset .. +cols]   # gather
  DeviceMatrix::from_host(ctx, host, rows, cols)
```

staged 路径里对应的两个方法做同样几何，但 gather 不落中间 `Vec`——`FillPool::gather_cols` 直接把跨步字节填进 pinned 缓冲（staging.rs L81-117）。

#### 4.3.3 源码精读

[row_shard](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-core/src/weight_loader.rs#L788-L803)：`tensor_2d_dims` 校验 2D（L768-778），`check_row_range` 校验行界（L780-786），然后取连续切片交给 `DeviceMatrix::from_host`。注意它经 `tensor_bf16_cow` 拿对齐安全的 `&[bf16]`——这比老一代 `load_tensor_2d`（L757-766，直接把原始字节传给 `from_safetensors`，后者假设小端主机且直接 cast，见 [pegainfer-kernels/src/tensor.rs:L427-L440](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/tensor.rs#L427-L440) 的注释）更严谨。

主机侧 gather 的 [gather_cols](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-core/src/weight_loader.rs#L805-L819) 只有一个双重循环：每行从 `row*total_cols + col_offset` 拷 `take` 个元素到 `row*take`。[col_shard](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-core/src/weight_loader.rs#L821-L843) 在其外再包一层越界检查。

[row_stitch](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-core/src/weight_loader.rs#L847-L868)：先对每个 `(row_offset, rows)` 段做行界校验，再按 `ranges` 给定顺序把各段行拼进一个 host 缓冲。与 row shard 的本质区别：**多段、可乱序、可重叠地取自同一个源张量**。

qwen3 的真实用法（staged 版本）在 [pegainfer-qwen3/src/weights/load.rs](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-qwen3/src/weights/load.rs#L130-L132)：`shard_range(total)` 按 `rank * (total/world_size)` 给出本卡区间（[pegainfer-qwen3/src/config.rs:L501-L504](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-qwen3/src/config.rs#L501-L504)，整数除法、无余数均衡——可整除由启动时的 [validate_for](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-qwen3/src/config.rs#L466-L484) 保证，它至少校验注意力头数与 KV 头数能被 world_size 整除）。三处消费：

- q/k/v 融合装载：[fused_rows](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-qwen3/src/weights/load.rs#L141-L163) 把三个独立投影矩阵的本卡行段拼成**一个** `[q_rows + 2*kv_rows, hidden]` 矩阵——行拼接的 `FusedPart` 定义见 [weight_loader.rs:L441-L446](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-core/src/weight_loader.rs#L441-L446)；gate/up 同理（L167-183）。这样一次 GEMM 就能算完 QKV。
- o_proj 列切：[load.rs:L189-L203](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-qwen3/src/weights/load.rs#L189-L203)，TP 时 `col_shard(name, hidden, q_total, q_row_offset, q_rows)` 取 `[hidden × q_total]` 矩阵中本卡头对应的列。
- down_proj 列切：[load.rs:L217-L231](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-qwen3/src/weights/load.rs#L217-L231)，列偏移取本卡中间维区间。

qwen35 则是简单路径消费者：[row_shard_if_needed / col_shard_if_needed](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-qwen35/src/weights/layers.rs#L237-L273) 在分片时切、单卡时整装。最有代表性的是 [linear_in_proj_qkv](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-qwen35/src/weights/layers.rs#L305-L318)：checkpoint 把全局 QKV 存成 `[所有 q 行 | 所有 k 行 | 所有 v 行]` 三段，本卡要的是**每段里自己的头局部切片**——三段不连续的行区间，正好是 `row_stitch` 的形状（doc 注释原话：不是切一条平坦行区间）。

#### 4.3.4 代码实践

**实践目标**：不写代码，先在纸上把一个 8×6 矩阵的 TP2 切分算对。

1. **操作步骤**：令 \( W \in \mathbb{R}^{8 \times 6} \)，元素按行主序编号 0..47（第 \( i \) 行第 \( j \) 列 = \( 6i + j \)）。分别写出 row shard（rank0/row 0-3，rank1/row 4-7）与 col shard（rank0/col 0-2，rank1/col 3-5）时每个 rank 拿到的**元素编号集合**。
2. 对每种切法，写出「拼回原矩阵」的重组规则（row shard：纵向串接；col shard：逐行交替取两卡各 3 个）。
3. 对照 qwen3 load.rs L141-163 与 L189-203，在纸上标注 q_proj 走了哪个集合、o_proj 走了哪个集合，并写一句为什么 o_proj 的输出需要 all-reduce 而 q_proj 不需要。

**需要观察的现象**：col shard 每行只拿 3 个连续元素、行与行之间在源里相距 6 个元素——这就是 gather_cols 双重循环的下标规律。

**预期结果**：row shard 两卡编号集合是 `{0..23}` 与 `{24..47}`；col shard 是每行前半 `{6i,6i+1,6i+2}` 与后半 `{6i+3,...,6i+5}`。纸面推演即可完成，无需设备；第 5 节的综合实践会把这套手算落成可执行的断言。

#### 4.3.5 小练习与答案

**练习 1**：`load_tensor_2d_row_stitch` 与 `load_tensor_2d_row_shard` 的差别是什么？qwen35 为什么需要前者？

答案：row shard 取单段连续行区间；row stitch 把同一源张量的多段行区间按给定顺序拼接（L847-868）。qwen35 的融合 QKV 权重按 `[Q|K|V]` 三段全局存放，本卡的头局部切片散在三段里，需要按段抽取再拼接（layers.rs L305-318）。

**练习 2**：`shard_range` 用整除切分，`total` 不能整除 `world_size` 时会发生什么？谁来防止？

答案：`shard_len = total / world_size` 丢余数，尾部行无卡认领（config.rs L501-504）。启动装载早期 `tensor_parallel.validate_for(&config)` 拒绝不可整除的配置（load.rs L77；config.rs L466 起校验头数整除）。切分函数自身不做余数均衡。

**练习 3**：staged 路径的 `col_shard` 与简单路径的 `load_tensor_2d_col_shard` 都做列聚集，实现差异在哪？

答案：简单路径先在堆上 gather 成 `Vec<bf16>` 再整体 pageable 上传（weight_loader.rs L805-843）；staged 路径的 `FillPool::gather_cols` 直接把跨步字节填进 pinned 缓冲（staging.rs L81-117），没有中间宿主副本，且 gather 与 DMA 双缓冲重叠。

---

### 4.4 WeightPrefetch：并行页缓存预读

#### 4.4.1 概念说明

mmap 的代价是**按需缺页**：`StagedWeightLoader` 顺序扫描权重字节时，每一页第一次被读才进内核 page cache。如果 checkpoint 在冷盘上（NVMe 甚至网络盘），缺页就是一次次小随机读，上传流水线的填充侧会被磁盘拖住。

`WeightPrefetch` 的解法是**advisory 并行预读**：装载开始前用 8 个线程把所有分片按 16 MiB 块读一遍，把页「捂热」进 page cache；装载路径自己**从不依赖**它——预读没跑完装载也照常工作，只是慢一点。（这类技术常被统称 madvise 式预取；本仓库的实现是 `preadv2(RWF_NOWAIT)` 驻留探测 + `read_exact_at` 预读，而非 `madvise` 系统调用。）

三个设计点让它是「好公民」而不是「抢跑者」：

1. **驻留探测跳过已热页**：`RWF_NOWAIT` 对需要内核真正去读的页返回 `EAGAIN`——读一页就够代表整个 16 MiB 块，出错即视为未驻留（L120-139 注释）。
2. **速率地板自律退出**：预读和装载共享同一块盘。若累计读满 512 MiB 时测得速率低于 1 GB/s，说明盘太慢、预读追不上装载只会添乱，主动 cancel（L146-149 的两条注释就是这两条阈值的原因）。
3. **失败全部聚合、绝不影响正确性**：Drop 时 cancel + join 所有工人，把不可读分片数、spawn 失败数、块读错误数、panic 数汇总成**一条**警告日志，首因保留（L260-292）。

#### 4.4.2 核心流程

```
WeightPrefetch::spawn(shard_paths):
  为每个分片建 (File, len)，按 16 MiB 步长切出 (file_idx, off) 块列表
  chunks 非空时起 min(8, 块数) 个工人线程，共享：
    AtomicUsize next     ← 工作窃取式领块
    AtomicU64  read_bytes ← 全局累计读量（供速率探针）
    AtomicBool cancel
  工人循环：
    领块 → chunk_looks_resident? 是→跳过
         → 否→read_exact_at 读整块进线程本地缓冲
         → 累计量首次越过 512 MiB 时算速率，< 1 GB/s 则置 cancel 全员撤退
Drop:
  cancel → join 全部工人 → 有任何失败/panic 则聚合成一条 warn
```

#### 4.4.3 源码精读

结构体与常量：[WeightPrefetch](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-core/src/weight_loader.rs#L94-L100) 只存 cancel 旗标、统计、工人句柄；四个常量 `CHUNK = 16 MiB`、`THREADS = 8`、`FLOOR = 1e9 B/s`、`PROBE = 512 MiB` 在 [spawn](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-core/src/weight_loader.rs#L142-L149) 顶部，每个都带一条「为什么是这个值」的注释——这是仓库注释风格的典型样本。

驻留探测 [chunk_looks_resident](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-core/src/weight_loader.rs#L122-L139)：构造一个 4096 字节 iovec，`preadv2(..., RWF_NOWAIT)` 读块首一页，`got > 0` 即认为整块已驻留。doc 注释（L120-122）解释了 EAGAIN 语义。

速率探针在工人主循环里（L208-223）：每次成功读块后 `fetch_add` 全局计数，**恰好第一次**越过 `PROBE_BYTES` 的那个线程计算速率并决定是否 cancel——用「越界前后对比」保证探针只触发一次。

Drop 的错误聚合 [L260-L292](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-core/src/weight_loader.rs#L260-L292)：drain 工人时连 panic payload 都降级成字符串计入首因；四类失败数全为 0 时不打日志。注意 `PrefetchStats::record_first_error` 的写法（L108-118）：锁中毒也 `into_inner` 取回继续，预读统计不值得为锁 panic。

消费侧的启用条件值得注意：qwen3 只在**单卡**时装载预读——[load.rs:L104-L105](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-qwen3/src/weights/load.rs#L104-L105) 用 `(tensor_parallel.world_size == 1).then(...)` 门控；而单卡模型线 gemma4 无条件启用（[gemma4/src/weights/load.rs:L491-L494](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-gemma4/src/weights/load.rs#L491-L494)）。代码没有注释解释 TP 门控的原因；一个合理推测是多进程并发读同一批文件时预读互相争抢（正是速率地板防的那种情形的跨进程版本），但这属于**推测**，以代码为准。

#### 4.4.4 代码实践

**实践目标**：做一个「防御点查检」，把三个设计点钉到行号上。

1. **操作步骤**：只读源码，在 L142-L257 里找出：（a）驻留探测在哪个循环分支跳过已热块；（b）速率探针「只触发一次」靠哪两行对比；（c）8 线程数如何与块数取 min（L176）。
2. 思考题：若把 `FLOOR_BYTES_PER_SEC` 调成 0，网络盘上会发生什么？（对照 L146-147 注释作答。）

**需要观察的现象**：有 GPU 与真实模型时，启动日志应出现 `Prefetching N weight shard(s) (X GB) on 8 threads`（L243-247）；慢盘上应出现 `Weight prefetch standing down: ... GB/s`（L218-221）。两者都需要真实装载，**待本地验证**。

**预期结果**：能指出三个防御点各自的行号；思考题答案见下面练习 3。

#### 4.4.5 小练习与答案

**练习 1**：为什么装载路径「从不依赖」预读是这个设计的正确姿势？

答案：`WeightPrefetch` 的 doc 注释（L91-93）明言 advisory：它只影响 page cache 状态（性能），不产生装载所需的数据本身；mmap 读页缺了预读只是慢，正确性由装载路径自己的 `read`（缺页）兜底。Drop 取消也随时安全。

**练习 2**：`chunk_looks_resident` 为什么只读一页就能代表 16 MiB 的块？

答案：预读的粒度单位是块、装载消费也基本顺序，页驻留高度局部相关；注释（L120-122）明说「一页代表整块，任何错误都算 miss」。这是刻意用廉价近似换探测成本。

**练习 3**：速率地板设 1 GB/s 的依据是什么？设成 0 有什么后果？

答案：注释（L146-147）说低于该速率时预读追不上与它共享同一设备的装载路径，只剩争抢；设成 0 等于永不自律退出，网络盘上预读线程会与装载的缺页读长期抢 I/O 队列，两边都变慢。

---

## 5. 综合实践

**任务**：写一个 GPU 往返测试，验证 `load_tensor_2d_row_shard` 与 `load_tensor_2d_col_shard` 两种切分在 TP2 下都能无损拼回原矩阵。这把 4.1（内存造 checkpoint）、4.2（上传通道）、4.3（切分几何）三讲内容串成一条链。

**操作步骤**（在读者的本地工作副本进行，属练习代码，不要提交回仓库）：

1. 新建 `pegainfer-core/tests/tp2_shard_roundtrip.rs`，内容如下（**示例代码**，仿照 [weight_loader_gpu.rs](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-core/tests/weight_loader_gpu.rs#L40-L77) 的写法）：

   ```rust
   //! 示例代码：TP2 row/col shard 拼回原矩阵的往返测试（需要 GPU，不需要模型权重）。
   use std::collections::HashMap;

   use half::bf16;
   use pegainfer_core::tensor::DeviceContext;
   use pegainfer_core::weight_loader::{load_tensor_2d_col_shard, load_tensor_2d_row_shard};
   use safetensors::{Dtype, SafeTensors, tensor::TensorView};

   fn bits(v: &[bf16]) -> Vec<u16> {
       v.iter().map(|b| b.to_bits()).collect()
   }

   #[test]
   fn tp2_row_and_col_shards_stitch_back() {
       let rows = 8usize; // 输出维
       let cols = 6usize; // 输入维
       let values: Vec<bf16> = (0..(rows * cols)).map(|i| bf16::from_f32(i as f32)).collect();
       let payload: Vec<u8> = values
           .iter()
           .flat_map(|v| v.to_bits().to_le_bytes())
           .collect();
       let view = TensorView::new(Dtype::BF16, vec![rows, cols], &payload).unwrap();
       let bytes = safetensors::serialize([("w".to_string(), view)], None).unwrap();
       let shards = vec![SafeTensors::deserialize(&bytes).unwrap()];
       let weight_map = HashMap::new(); // 单 shard：find_tensor 回退扫描即可命中

       let ctx = DeviceContext::new().unwrap();
       let want = bits(&values);

       // ---- row shard：rank r 拿第 r 段行，纵向串接应还原原矩阵 ----
       let mut row_parts = Vec::new();
       for r in 0..2 {
           let m = load_tensor_2d_row_shard(
               &ctx, &shards, &weight_map, "w", r * (rows / 2), rows / 2,
           )
           .unwrap();
           assert_eq!((m.rows, m.cols), (rows / 2, cols));
           row_parts.push(ctx.stream.clone_dtoh(&m.data).unwrap());
       }
       let got: Vec<u16> = row_parts.into_iter().flatten().flat_map(|v| bits(&v)).collect();
       assert_eq!(got, want, "TP2 row shard 纵向拼回应等于原矩阵");

       // ---- col shard：rank r 拿第 r 段列，逐行横向交错应还原原矩阵 ----
       let mut col_parts = Vec::new();
       for r in 0..2 {
           let m = load_tensor_2d_col_shard(
               &ctx, &shards, &weight_map, "w", r * (cols / 2), cols / 2,
           )
           .unwrap();
           assert_eq!((m.rows, m.cols), (rows, cols / 2));
           col_parts.push(ctx.stream.clone_dtoh(&m.data).unwrap());
       }
       let mut stitched: Vec<bf16> = Vec::with_capacity(rows * cols);
       for row in 0..rows {
           for part in &col_parts {
               stitched.extend_from_slice(&part[row * (cols / 2)..(row + 1) * (cols / 2)]);
           }
       }
       assert_eq!(bits(&stitched), want, "TP2 col shard 逐行横向拼回应等于原矩阵");
   }
   ```

2. 在有 CUDA 工具链的机器上构建并运行（构建需要工具链，运行需要 GPU；`pegainfer-core` 不在 default-members 里，必须 `-p` 指定）：

   ```bash
   cargo test --release -p pegainfer-core --test tp2_shard_roundtrip -- --nocapture
   ```

3. **观察要点**：两处断言的重组方式不同——row 是「串接」，col 是「逐行交错」。这正是 4.3.4 纸面推演的可执行版本。

**预期结果**：测试通过；断言失败则说明你对其中一种切分的几何理解有误。本讲义写作环境无 GPU，运行结果**待本地验证**。

**无 GPU 替代方案**：在任意目录建一个独立小工程（`cargo new --bin tp2_sim`，`Cargo.toml` 不需要任何依赖），用纯 CPU 复刻两套切片的下标几何，验证「拼回规则」本身（**示例代码**）：

```rust
fn main() {
    let (rows, cols) = (8usize, 6usize);
    let w: Vec<u32> = (0..(rows * cols) as u32).collect();

    // 复刻 load_tensor_2d_row_shard（weight_loader.rs L800-L802）：连续行切片 + 串接
    let row_concat: Vec<u32> = (0..2)
        .flat_map(|r| w[r * (rows / 2) * cols..(r + 1) * (rows / 2) * cols].to_vec())
        .collect();
    assert_eq!(row_concat, w);

    // 复刻 gather_cols（weight_loader.rs L812-L818）：逐行取 take 列 + 横向交错
    let mut col_concat = Vec::with_capacity(w.len());
    for row in 0..rows {
        for r in 0..2 {
            let off = r * (cols / 2);
            col_concat.extend_from_slice(&w[row * cols + off..row * cols + off + cols / 2]);
        }
    }
    assert_eq!(col_concat, w);
    println!("TP2 两种切分的拼回规则均无损");
}
```

这个替代方案验证的是你对几何的理解，不是仓库代码本身——两者都做完，理解才算闭环。

## 6. 本讲小结

- 加载管线三步曲 `load_shard_info → mmap_shards → deserialize_shards` 只建立**零拷贝视图**（布局差异在入口抹平成 `(分片路径, 张量名→下标)`），真正的搬运全在上传通道里。
- 两条上传通道：`load_tensor_2d_*` 自由函数一族走 pageable 直传（每函数独立完整校验+搬运）；`StagedWeightLoader` 先记录全部上传意图（校验前置、显存先分配）、`finish()` 一次性执行，小向量走 pageable、大矩阵走 pinned 双缓冲。
- pinned 双缓冲（2 × 32 MiB）把「CPU 填充」与「DMA 搬运」从串行变成重叠，吞吐上限从两者之和变为两者的 max；错误路径用 drain-or-abort 保证绝不在 DMA 飞行中释放 pinned 内存。
- TP 切分命名按存储几何：row shard 切输出维（q/k/v、gate/up，免通信），col shard 切输入维（o_proj、down_proj，输出是部分和需 all-reduce），row stitch 从同一源张量抽多段行区间再拼接（qwen35 融合 QKV 的头局部抽取）。
- `WeightPrefetch` 是 advisory 并行页缓存预读：RWF_NOWAIT 跳过已热块、1 GB/s 速率地板自律退出、Drop 聚合所有失败为一条警告；qwen3 仅单卡启用，gemma4 无条件启用。

## 7. 下一步学习建议

- **下一讲（u4-l5）**：CUDA Graph 基础设施——权重装载是「启动期」故事，解码路径的显存指针为什么必须预分配固定，正是装载期 `alloc_timed` + staging 一次性上传铺好的地基。
- **纵向深入**：读 [pegainfer-qwen3/src/weights/load.rs](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-qwen3/src/weights/load.rs#L66-L110) 的 `from_safetensors_with_runtime` 全函数，看 `Qwen3MemoryOptions`、LoRA 预留槽位如何叠加在本讲的 `fused_rows`/`col_shard` 之上（u5-l2 会正式精读）。
- **横向对照**：qwen35 的 [weights/layers.rs](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-qwen35/src/weights/layers.rs#L237-L273) 是简单路径的完整样本——两种通道在真实模型线上的取舍（gated_q_proj 的逐头切分、conv1d 的 1D stitch）值得与 qwen3 的 staged 用法并排读。
- **通信侧**：本讲反复提到「col shard 输出需要 all-reduce」，其执行机制（RankWorker、NCCL 约定）在 u9-l4 张量并行一讲展开。
