# Sampling 层：Top-K 与 Top-P

## 1. 本讲目标

在 u8-l1 中我们看到，当 `beam_width == 1` 时，`DynamicDecodeLayer` 会把解码后端切到「采样」——也就是 Top-K 与 Top-P 两条路径串行接力。本讲就钻进这两条路径，把「每一步如何在词表分布上随机选一个 token」彻底讲清。Top-K 和 Top-P 是大模型解码最常用的两种随机采样策略，理解它们的差异也是理解 FT 解码性能取舍的关键。

读完本讲你应当：

- 说清 `BaseSamplingLayer` 的统一接口与公共管线：为什么温度 / 重复惩罚 / 最小长度这些「采样前对 logits 的修正」被抽到基类，而「真正选 token」的工作留给纯虚函数 `runSampling`。
- 复述 `BaseSamplingLayer::setup` 与 `::forward` 的协作，以及 `skip_decode` / `skip_any` 两个开关如何让 Top-K 与 Top-P 在同一步里串行而不互相污染 logits。
- 讲透 Top-K 的选择机制：它不需要排序，而是用 CUB 的 block reduction 反复提取前若干大值，两阶段把候选收缩到固定个数。
- 讲透 Top-P（nucleus sampling）的本质：它必须先把整个词表按概率降序排序，再做一次前缀和（累积概率）截断，因为「候选集合的大小」本身是数据相关的。
- 能写出两者在并行 kernel 实现上的关键差异点（是否排序、工作区规模、复杂度量级）。

## 2. 前置知识

本讲默认你已读过 u8-l1（`DynamicDecodeLayer` 的统一外观与 runtime_arg 机制）与 u5-l2（生成主循环每一步的动作）。回顾几个关键概念：

- **logits**：模型在每一步对词表每个词输出的原始分数，形状 `[local_batch_size, vocab_size_padded]`。采样层接收的输入实际是 `log(softmax(logits))`（对数概率），由上游 `invokeAddBiasSoftMax` 转换。
- **采样（sampling）**：与 beam search「同时维护多条候选序列」不同，采样每步只随机选一个 token，靠随机性带来多样性。`beam_width == 1` 是采样与 beam search 的分水岭。
- **runtime_arg**：u8-l1 引入的概念——指那些「同一次推理、不同请求可不同」、由每次 `setup` 注入而非构造期固定的参数。本讲的 `runtime_top_k` / `runtime_top_p` / `temperature` 等都是 runtime_arg。
- **end_id / finished**：当某条序列生成出结束符 `end_id` 时标记为 `finished`，后续步骤要跳过它的采样计算（靠 `skip_decode` 实现）。
- **curandState_t**：CUDA 的随机数生成器状态。FT 为 batch 中每条序列维护一个独立的 curand 状态，保证可复现且各序列独立。

需要的几个数学事实：

- 概率分布归一化：\(\sum_i p_i = 1\)，其中 \(p_i\) 是第 \(i\) 个 token 的概率。
- 按概率采样：若在离散分布 \(\{p_i\}\) 上均匀投点 \(r \in [0,1)\)，落到第 \(k\) 个「累积区间」就选 token \(k\)。即找到最小的 \(k\) 使累积概率 \(P_k = \sum_{i \le k} p_i \ge r\)。
- 对数概率与概率的转换：\(p_i = \exp(\text{logp}_i)\)，softmax 保证 \(\sum_i \exp(\text{logp}_i) = 1\)。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [BaseSamplingLayer.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/sampling_layers/BaseSamplingLayer.h) | 采样层抽象基类，持有 `vocab_size_`、curand 状态与各 penalty 的 device/host 缓冲，声明纯虚 `runSampling` 与公共 `setup` / `forward`。 |
| [BaseSamplingLayer.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/sampling_layers/BaseSamplingLayer.cc) | 基类实现：`allocateBuffer`（公共缓冲）、`setup`（初始化 curand + 拷贝 penalty）、`forward`（公共管线：skip 判定 → 温度 → 重复惩罚 → 最小长度 → `runSampling`）。 |
| [TopKSamplingLayer.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/sampling_layers/TopKSamplingLayer.h) / [.cu](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/sampling_layers/TopKSamplingLayer.cu) | Top-K 采样层：重写 `setup`（规整 top_k/top_p、计算 `runtime_max_top_k_`）与 `runSampling`（调 `invokeBatchTopKSampling`）。 |
| [TopPSamplingLayer.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/sampling_layers/TopPSamplingLayer.h) / [.cu](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/sampling_layers/TopPSamplingLayer.cu) | Top-P（核采样）层：重写 `setup`（含 `top_p_decay`/`top_p_min`/`top_p_reset_ids` 退火参数）与 `runSampling`（调 `invokeBatchTopPSampling` + `invokeComputeToppDecay`）。 |
| [sampling_topk_kernels.cu](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/sampling_topk_kernels.cu) / [.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/sampling_topk_kernels.h) | Top-K kernel：`topk_stage1`（多 block 扫描词表取局部候选）+ `topk_stage2_sampling`（合并候选并采样），以及 `addBiasEndMask`、curand 初始化。 |
| [sampling_topp_kernels.cu](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/sampling_topp_kernels.cu) / [.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/sampling_topp_kernels.h) | Top-P kernel：`topp_beam_topk_kernel`（尖峰早退优化）、`topp_sampling`（排序后前缀和截断采样），底层可选 CUB 分段基数排序 `DeviceSegmentedRadixSort` 或自定义单遍实现 `topPPerSegment`。 |

## 4. 核心概念与源码讲解

### 4.1 BaseSamplingLayer：采样的统一接口与公共管线

#### 4.1.1 概念说明

FT 的采样层与 beam search 层一样是「模板方法」结构，但切分方式不同：beam search 把「选 token 的具体策略」留给子类，而采样层把**所有采样策略共有的「选 token 前对 logits 的修正」抽到基类**，只把「真正如何从分布里抽一个 token」这一个动作留给纯虚函数 `runSampling`。

继承关系如下：

```
DynamicDecodeBaseLayer                       // 最底层，提供 stream/allocator/cublas/cuda_device_prop 环境
      ▲
      │
BaseSamplingLayer<T>                         // 模板方法基类：setup + forward 公共管线
      ▲
      ├── TopKSamplingLayer<T>               // Top-K：固定候选个数
      └── TopPSamplingLayer<T>               // Top-P：核采样，按累积概率动态截断
```

为什么这样切？因为无论是 Top-K 还是 Top-P，采样前的准备工作几乎完全一样：给 logits 加偏置、按温度缩放、施加重复惩罚、强制最小长度。把这些公共流程放进基类的 `forward`，子类就只需专注「在（修正后的）分布上抽 token」这一件事。基类 [`BaseSamplingLayer`](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/sampling_layers/BaseSamplingLayer.h#L27-L88) 持有 `vocab_size_` / `vocab_size_padded_`、curand 状态缓冲、各 penalty 的 device 与 host 镜像，以及一块采样工作区 `sampling_workspace_`，并声明纯虚函数 `runSampling`。

这里有一个承上启下的关键设计：u8-l1 讲过，`DynamicDecodeLayer` 在 `beam_width==1` 时会**同时构造** TopK 与 TopP 两个采样层，并在每一步**串行**调用它们——靠 `runtime_top_k==0` / `runtime_top_p==0` 触发 `skip_decode` 来决定哪个真正干活。这意味着两个采样层在同一步会读到**同一块 logits 内存**。由于采样过程会就地改写 logits（加偏置、softmax、掩码 end_id），基类必须防止先跑的层把后跑的层的输入污染掉——这正是 `skip_any_` 与 `runtime_logits_buf_` 存在的原因（见 4.1.3）。

#### 4.1.2 核心流程

`BaseSamplingLayer` 的两个核心方法是 `setup`（每步前调，注入 runtime 参数）与 `forward`（每步调，执行采样）。它们与子类的 `runSampling` 协作如下：

```
每次推理 setup(batch_size, beam_width, runtime_args):
  1. allocateBuffer(batch_size, runtime_top_k, runtime_top_p)   // 分配/复用公共缓冲 + 子类工作区
  2. 初始化 curand 状态（按 random_seed，单值或逐序列）
  3. 把 temperature / repetition_penalty / presence_penalty / min_length
     从 runtime_args 拷到 device 缓冲与 host 镜像
  ↓ （子类 setup 接着规整 top_k/top_p 并算 runtime_max_top_k_/runtime_max_top_p_）

每一步 forward(output_tensors, input_tensors):
  1. 若本 local_batch 全部 skip_decode → 直接 return
  2. 若部分 skip（skip_any_）→ 把 logits 拷到内部 runtime_logits_buf_，避免污染
  3. 温度 + 偏置修正（invokeBatchApplyTemperaturePenalty）
  4. 重复 / presence 惩罚（invokeBatchApplyRepetitionPenalty，仅 step>1）
  5. 最小长度惩罚（invokeMinLengthPenalty）
  6. runSampling(...)                          // ← 纯虚，由 TopK/TopP 子类实现
  7. 若 is_free_buffer_after_forward_ → freeBuffer()
```

注意 `setup` 与 `forward` 的分工：`setup` 负责「把 runtime 参数搬上 device」，`forward` 负责「真正用这些参数改 logits 并采样」。这承接了 u8-l1 的 `setup`/`forward` 配对约定——构造期参数都已废弃，真正生效的值由每次 `setup` 注入。

#### 4.1.3 源码精读

**公共缓冲分配。** [`BaseSamplingLayer::allocateBuffer`](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/sampling_layers/BaseSamplingLayer.cc#L28-L53) 申请两类缓冲：device 上的 curand 状态、各 penalty 缓冲、`runtime_logits_buf_`（用于 skip_any 时拷贝 logits）、`skip_decode_buf_`；以及 host 上的镜像（`temperature_` 等，用于在 CPU 上判断「是否全为默认值」以跳过无用的 kernel 启动）。子类的 `allocateBuffer(batch_size, top_k, top_p)` 会先调这个基类版本，再申请自己专属的工作区。

**setup：初始化随机数 + 拷贝 penalty。** [`BaseSamplingLayer::setup`](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/sampling_layers/BaseSamplingLayer.cc#L111-L206) 先调 `allocateBuffer`，然后处理三件事：

```cpp
// 1. 按 random_seed 初始化 curand；未提供则用默认种子 0（保证可复现）
if (runtime_args->isExist("random_seed")) {
    // 单值种子：所有序列共用；[batch_size] 种子：逐序列不同
    invokeCurandInitialize(curandstate_buf_, batch_size, seed, stream_);
}
// 2. temperature：单值→deviceFill 广播；向量→cudaAutoCpy 拷贝
// 3. repetition_penalty / presence_penalty（互斥）/ min_length 同理
```

这里有个细节承接 u8-l1：`repetition_penalty` 与 `presence_penalty` 互斥，FT 用 `FT_CHECK_WITH_INFO` 强制只能传一个，并用枚举 `RepetitionPenaltyType`（`Multiplicative` / `Additive` / `None`）记录是哪种，供 `forward` 时选择 kernel。

**forward：公共管线。** [`BaseSamplingLayer::forward(TensorMap*, TensorMap*)`](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/sampling_layers/BaseSamplingLayer.cc#L254-L359) 是本模块最关键的方法。它的前半段是采样前的 logits 修正，用一组 `ALL_OF` 宏判断「是否全为默认值」来跳过无谓的 kernel 启动：

```cpp
bool* skip_decode = skip_decode_ + ite * local_batch_size;
if (ALL_OF(skip_decode, local_batch_size, bool, true)) {
    return;  // 本批全部 skip，什么都不做
}
skip_any_ = std::any_of(...);  // 是否「部分」序列 skip
if (skip_any_) {
    // 关键：把 logits 拷到内部缓冲，避免污染另一个采样层的输入
    cudaD2Dcpy(runtime_logits_buf_, logits, ...);
    logits = runtime_logits_buf_;
}
// 温度 + 偏置
if (embedding_bias != nullptr || !ALL_OF(temperature_+..., 1.0f)) {
    invokeBatchApplyTemperaturePenalty(logits, embedding_bias, temperature_buf_+..., ...);
}
// 重复惩罚（仅 step>1）
if (step > 1 && repetition_penalty_type_ != None) { invokeBatchApplyRepetitionPenalty(...); }
// 最小长度惩罚
if (invoke_min_length_penalty) { invokeMinLengthPenalty(...); }

runSampling(output_tensors, input_tensors);   // ← 交给子类
```

理解 `skip_any_` 是理解 Top-K/Top-P 串行协作的钥匙：当 batch 里有的请求走 Top-K、有的走 Top-P 时，两个层都会被调，但它们读的是同一块 logits。先执行的那个会就地改写 logits，所以只要 `skip_any_` 为真，基类就先把 logits 复制到 `runtime_logits_buf_`，让本层在副本上操作，原始 logits 留给后一个层。每个修正 kernel 都先用 `ALL_OF` 在 host 镜像上判默认值——若整批 temperature 都是 1.0，连 kernel 都不启动，这是 FT 减少 kernel launch 开销的惯用手法。

`runSampling` 是纯虚函数，声明在 [`BaseSamplingLayer.h:55`](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/sampling_layers/BaseSamplingLayer.h#L55-L58)，由 TopK/TopP 子类实现。接下来两节分别展开。

#### 4.1.4 代码实践

**实践目标**：通过阅读 `BaseSamplingLayer::forward`，确认「采样前的 logits 修正全部在基类、选 token 在 `runSampling`」这一分工，并验证 `skip_any_` 的防污染逻辑。

**操作步骤**：

1. 打开 [`BaseSamplingLayer.cc` 的 forward](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/sampling_layers/BaseSamplingLayer.cc#L254-L359)。
2. 在第 287–300 行附近，找到 `skip_decode` 与 `skip_any_` 的判断分支。
3. 对比 [`TopKSamplingLayer::runSampling`](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/sampling_layers/TopKSamplingLayer.cu#L188-L268) 与 [`TopPSamplingLayer::runSampling`](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/sampling_layers/TopPSamplingLayer.cu#L255-L343)，确认它们都**没有**再做温度/重复惩罚的修正——这些只在基类做一次。

**需要观察的现象**：两个子类的 `runSampling` 入口都有一行 `T* logits = !skip_any_ ? input_tensors->at("logits").getPtr<T>() : runtime_logits_buf_;`——它们直接复用基类已选好的 logits 指针，自己不再决定用哪块缓冲。

**预期结果**：你能用一句话概括——「公共管线在基类、采样策略在子类、两者通过 `logits` 指针与 `runSampling` 虚函数衔接」。无需运行命令（属源码阅读型实践）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `temperature_` 等参数要同时维护 device 缓冲（`temperature_buf_`）和 host 镜像（`temperature_`）两份？

**参考答案**：device 缓冲供 GPU kernel 读取（采样修正 kernel 在 GPU 上跑）；host 镜像供 CPU 端用 `ALL_OF` 快速判断「整批是否全为默认值」，从而决定是否跳过 kernel 启动。省去「为了判断要不要启动 kernel 而先做一次 D2H 拷贝」的开销。

**练习 2**：若 batch 中一半请求 `runtime_top_k=0`（走 Top-P）、一半 `runtime_top_p=0`（走 Top-K），Top-K 层的 `forward` 会被调用吗？它会污染 Top-P 层的输入吗？

**参考答案**：会被调用（`DynamicDecodeLayer` 总是两个都调），但因为存在部分 skip，`skip_any_==true`，Top-K 层会先在 `runtime_logits_buf_` 副本上操作，不污染原始 logits，故 Top-P 层仍能读到干净的输入。

---

### 4.2 TopKSamplingLayer：按固定个数选候选的 Top-K

#### 4.2.1 概念说明

**Top-K 采样**的思想很简单：每一步只从概率最高的 K 个 token 里按概率随机抽一个，其余全部丢弃。它把候选集合的大小**固定**为 K，因此叫「固定个数」采样。直觉上，K 越大生成越多样、越不可控；K=1 退化为贪心解码（greedy）。

它的关键性质是：**「选出概率最高的 K 个」是一个固定基数的选择问题，不需要对整个词表排序**。你只需要反复做「找当前最大值并标记为已选」，K 次即可。这正是 FT 的 Top-K kernel 不用排序、而用迭代式 block reduction 的根本原因。

[`TopKSamplingLayer`](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/sampling_layers/TopKSamplingLayer.h#L25-L72) 继承 `BaseSamplingLayer<T>`，只重写 `setup` 与 `runSampling`，核心状态是 `runtime_max_top_k_`（本批各序列 top_k 的最大值，决定用哪档 kernel 配置）和 `runtime_top_k_buf_` / `runtime_top_p_buf_`（逐序列的 top_k/top_p）。

注意一个历史包袱：FT 5.0 后把原来的 `TopKSamplingLayer`（纯 top-k）与 `TopKTopPSamplingLayer`（top-k + top-p）**合并**成了现在的 `TopKSamplingLayer`。所以现在的 Top-K kernel 其实同时接受 `top_ks` 和 `top_ps`——`top_p` 在这里扮演「在已选出的 K 个候选里再按累积概率缩放随机数」的角色，并不是独立的核采样。这一点在 4.2.3 的源码里能看到。

#### 4.2.2 核心流程

Top-K 的运行分 `setup`（规整参数）与 `runSampling`（两阶段 kernel）：

```
setup(batch_size, beam_width, runtime_args):
  1. 调基类 setup（curand + penalty）
  2. 从 runtime_args 取 runtime_top_k / runtime_top_p（可为 [1] 或 [batch_size]）
  3. 启动 setup_topk_runtime_args kernel：
       - 若 k==0 且 p==0 → 视作贪心，置 k=1
       - 若 k>0 且 p==0 → 兼容旧版，置 p=1.0
       - 把 k 裁剪到 [0, 1024]（kernel 模板上限），p 裁剪到 [0,1]
       - 置 skip_decode[i] = (k==0)        // k==0 表示本序列不走 Top-K
  4. D2H 拷贝 top_ks 回 host，取最大值得 runtime_max_top_k_

runSampling(output_tensors, input_tensors):
  1. invokeAddBiasEndMask        // 把 padding 位置掩成 -INF；finished 序列只留 end_id
  2. （可选）invokeAddBiasSoftMax // 仅当需要 cum_log_probs / output_log_probs 时做 softmax
  3. invokeBatchTopKSampling     // ← 两阶段 kernel 真正选 token
```

两阶段 kernel 是 Top-K 的核心，画成数据流：

```
log_probs [B, V]
     │  topk_stage1：BLOCKS_PER_BEAM 个 block 并行扫描词表
     │  每个 block 用 BlockReduce 反复提取前 k 个 → 局部候选 [B, k×BLOCKS_PER_BEAM]
     ▼
topk_tmp_id_buf / topk_tmp_val_buf   （候选 id 与值）
     │  topk_stage2_sampling：1 个 block/序列，把 k×BLOCKS_PER_BEAM 个候选再归约
     │  排序成降序、做 softmax 归一、按随机数在累积分布上采样
     ▼
output_ids[B], sequence_length[B], finished[B]
```

#### 4.2.3 源码精读

**setup：规整参数。** [`setup_topk_runtime_args` kernel](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/sampling_layers/TopKSamplingLayer.cu#L28-L77) 把 `[1]` 或 `[batch_size]` 的输入广播/裁剪成逐序列的 `top_ks` / `top_ps`，并处理几条兼容规则：

```cpp
uint  k = top_ks_size > 1 ? top_ks[i] : top_k;
float p = top_ps_size > 1 ? top_ps[i] : top_p;
if (k == 0 && p == 0.0f) { k = 1; }        // 等价贪心
if (k > 0 && p == 0.0f)  { p = 1.0f; }     // 兼容 <=FT5.0 的纯 top-k
top_ks[i] = k > TOP_K_MAX ? TOP_K_MAX : k; // 裁剪到 1024
top_ps[i] = p < 0.0f ? 0.0f : (p > 1.0f ? 1.0f : p);
skip_decode[i] = k == 0;                   // k==0 → 本序列不走 Top-K（留给 Top-P）
```

注意 `setup` 模板参数是 `<1024>`，但 kernel 内部 `TOP_K_MAX` 裁剪——这承接 u8-l1 提到的「Top-K 上限 1024」。`skip_decode[i]=k==0` 是 Top-K 与 Top-P 串行接力的关键：当某序列 `top_k==0` 时，它在 Top-K 层被 skip，转而由 Top-P 层处理。

[`TopKSamplingLayer::setup`](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/sampling_layers/TopKSamplingLayer.cu#L133-L186) 在 kernel 之后，把 `top_ks` 拷回 host 取最大值得到 `runtime_max_top_k_`——它决定 `invokeBatchTopKSampling` 内部 switch 到哪一档 kernel 配置（见下）。

**runSampling：两阶段 kernel。** [`TopKSamplingLayer::runSampling`](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/sampling_layers/TopKSamplingLayer.cu#L188-L268) 先做掩码与（可选）softmax，然后调 [`invokeBatchTopKSampling`](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/sampling_topk_kernels.cu#L345-L396)。注意它同时传了 `runtime_top_k_buf_` 和 `runtime_top_p_buf_`——印证了「合并后的 Top-K kernel 兼容 top-p 缩放」。

`invokeBatchTopKSampling` 按 `max_top_k` 用 [`CASE_K` 宏](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/sampling_topk_kernels.cu#L314-L343) 选不同的 `(BLOCK_SIZE, BLOCKS_PER_BEAM)` 配置：

```cpp
switch (max_top_k) {
    CASE_K(1, 16,  128, 128, 8);   // k∈[1,16]:  stage1 block=128, 每 beam 8 个 block
    CASE_K(17, 32, 256, 128, 8);
    CASE_K(33, 64, 256, 256, 8);
    CASE_K(65, 1024, 256, 256, 8);
}
```

K 越大，需要的 block 越大、每 beam 并行的 block 越多——这是为不同 K 量身定制并行度的自调优。

**stage1：扫描词表取局部候选。** [`topk_stage1`](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/sampling_topk_kernels.cu#L130-L203) 的核心是「用 CUB 的 `BlockReduce` 配合只保留 2 个最大值的 `TopK_2` 结构，重复 k 轮，每轮取出当前最大值并把它改成 `-INF`」：

```cpp
for (int ite = 0; ite < k; ite++) {
    partial.init();
    for (int elem_id = tid + block_lane*BLOCK_SIZE; elem_id < vocab_size; elem_id += ...) {
        partial.insert(tmp_log_probs[index], index);   // 每线程维护自己的 top-2
    }
    // block 级归约：把所有线程的 top-2 合成全局 top-2
    TopK_2<T> total = BlockReduce(temp_storage).Reduce(partial, reduce_topk_op_2<T>);
    if (tid == 0) {
        topk_tmp_id_buf[...]  = total.p;               // 记下本轮最大值的 id
        tmp_log_probs[total.p] = -MAX_T_VAL;           // 标记已选，下一轮跳过
    }
    __syncthreads();
}
```

每 beam 用 `BLOCKS_PER_BEAM_=8` 个 block 并行扫描词表的不同区段，每个 block 独立产出 k 个候选，故候选总数为 `k × 8`。这就是「不需要排序」的精髓——用迭代归约把 top-k 一个个抠出来，复杂度约 \(O(k \cdot V / \text{BLOCKS\_PER\_BEAM})\)，远低于排序的 \(O(V \log V)\)。

**stage2：合并候选并采样。** [`topk_stage2_sampling`](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/sampling_topk_kernels.cu#L205-L312) 每序列一个 block，把 `k×8` 个候选再用一轮迭代归约排成降序，求和 `s_sum`，然后采样：

```cpp
// 生成随机数，缩放到 [0, top_p × s_sum)
rand_num = curand_uniform(curandstate + blockIdx.x) * prob_threshold * s_sum;
for (int i = 0; i < k; i++) {
    rand_num -= s_val2[i];                 // 沿降序候选依次扣减概率
    if (rand_num <= 0.0f || i == k - 1) {
        ids[batch_id] = ... s_id[i];       // 命中即选
        break;
    }
}
```

这里的 `prob_threshold`（即 `top_p`）就是把随机数上界缩放——若 `top_p<1`，相当于在 top-k 候选里再做一次概率截断（合并自旧的 TopKTopP 行为）。最后更新 `sequence_length` 与 `finished`（命中 `end_id` 即结束）。

#### 4.2.4 代码实践

**实践目标**：理解 Top-K kernel 为何「不排序、用迭代归约」选候选，并看清 `runtime_max_top_k_` 如何决定 kernel 配置。

**操作步骤**：

1. 打开 [`topk_stage1`](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/sampling_topk_kernels.cu#L130-L203)，找到第 184 行的 `for (int ite = 0; ite < k; ite++)` 循环。
2. 观察循环体内：`partial.insert(...)` 每线程局部收集 → `BlockReduce(...).Reduce(...)` block 级归约 → `tmp_log_probs[total.p] = -MAX_T_VAL` 把刚找到的最大值「挖掉」。
3. 打开 [`CASE_K` 宏](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/sampling_topk_kernels.cu#L388-L395)，对照 `runtime_max_top_k_` 的取值（在 [`TopKSamplingLayer::setup` 第 184 行](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/sampling_layers/TopKSamplingLayer.cu#L182-L185)）。
4. 思考：若把 `k` 从 8 改成 40，会命中哪一档 `CASE_K`？block 配置如何变化？

**需要观察的现象**：`topk_stage1` 里没有任何排序调用，只有反复的 `BlockReduce` 与「置 -INF」；候选数随 `k` 线性增长。

**预期结果**：`k=40` 命中 `CASE_K(33,64,256,256,8)`，即 stage1 的 block 从 128 升到 256、stage2 的 block 也升到 256。这说明 K 越大，FT 用更大的 block 来摊销更多候选的归约开销。属源码阅读型实践，无需运行。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `topk_stage1` 要为每条 beam 启动 `BLOCKS_PER_BEAM_=8` 个 block，而不是 1 个？

**参考答案**：词表 V 通常很大（数万），单个 block 扫描整个词表串行度高。用 8 个 block 并行扫描词表的不同区段，每个 block 各自产出 k 个局部候选，再由 stage2 合并。这是把「扫描 V」的并行度从 block 内的线程扩展到 block 间，提升 GPU 占用率。

**练习 2**：`topk_stage1` 每轮 `ite` 只能找出 1 个最大值，找 k 个要 k 轮。这个复杂度对大 k（如 k=1024）是否划算？

**参考答案**：对很大的 k 不太划算（k 轮归约，每轮扫一遍词表）。所以 FT 对 `k∈[65,1024]` 这一档用了更大的 block（256）和同样的 8 个并行 block 来摊销，但本质上 Top-K 更适合中小 k；需要极大候选集时核采样（Top-P）往往是更好的选择。这也解释了为何 FT 默认上限是 1024。

---

### 4.3 TopPSamplingLayer：核采样的排序与累积截断

#### 4.3.1 概念说明

**Top-P 采样**（又称 **nucleus sampling** / 核采样）的思想是：每一步选出**概率累积起来刚好超过阈值 P 的最小 token 集合**，再在这个「核」里按概率随机抽一个。用数学语言：找到最小的整数 \(m\)，使

\[
\sum_{i=1}^{m} p_{(i)} \ge P
\]

其中 \(p_{(1)} \ge p_{(2)} \ge \dots\) 是把词表概率降序排列后的序列。这个集合 \(\{p_{(1)}, \dots, p_{(m)}\}\) 就是「核」（nucleus）。与 Top-K 不同，**核的大小 \(m\) 是数据相关的**——分布很尖（一个 token 占了 90% 概率）时 \(m\) 可能是 1；分布很平时 \(m\) 可能很大。

这就引出了本节的核心结论：**Top-P 之所以必须排序，是因为「核的边界」只有在概率降序排列后才能确定。** Top-K 只要「第 K 大」这一个次序统计量，可以用迭代归约不求全序；而 Top-P 要的是「累积概率跨越阈值的位置」，必须知道从最大值开始累加到哪一项才达标——这要求至少把高概率段排好序。

[`TopPSamplingLayer`](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/sampling_layers/TopPSamplingLayer.h#L24-L82) 比 Top-K 多了一组「退火」参数（`top_p_decay` / `top_p_min` / `top_p_reset_ids`），支持在生成过程中逐步收紧 top_p（类似 simulated annealing），以及一个 `runtime_max_top_p_` 决定走哪条 kernel 路径。

#### 4.3.2 核心流程

Top-P 的运行同样分 `setup` 与 `runSampling`，但 runSampling 的内部结构与 Top-K 截然不同：

```
setup(batch_size, beam_width, runtime_args):
  1. 调基类 setup
  2. 若未提供 runtime_top_p → 整批 skip_decode=true，直接 return（本层不干活）
  3. set_topp_runtime_args kernel：广播/裁剪 top_p，拷贝 decay/min/reset_ids
  4. D2H 拷贝 top_ps 取最大值得 runtime_max_top_p_

runSampling(output_tensors, input_tensors):
  1. invokeTopPInitialize        // 初始化 id_vals/offset 缓冲
  2. invokeAddBiasSoftMax        // 把 logits 转成概率（排序前必须归一化）
  3. invokeBatchTopPSampling     // ← 排序 + 累积截断采样（核心）
  4. invokeComputeToppDecay      // 按退火规则更新下一步的 top_p
```

`invokeBatchTopPSampling` 内部有两条路径，由 `runtime_max_top_p` 与编译选项 `ENABLE_SINGLE_PASS_TOP_P` / `SINGLE_PASS_THRESHOLD` 决定：

```
invokeBatchTopPSampling:
  do_radix_sort = (ENABLE_SINGLE_PASS_TOP_P==0 || max_top_p >= SINGLE_PASS_THRESHOLD)
  if (!do_radix_sort):
      先用 topp_beam_topk_kernel 试探：若 top-1 概率已 ≥ top_p → 尖峰，跳过排序直接选
      否则用自定义单遍实现 topPPerSegment（依赖共享内存，受 sm 限制）
  if (do_radix_sort):
      用 CUB DeviceSegmentedRadixSort 把每行词表降序排序 → sorted_log_probs/sorted_id_vals
  最后用 topp_sampling 在排序后的数组上做前缀和截断采样
```

#### 4.3.3 源码精读

**setup：退火参数。** [`set_topp_runtime_args` kernel](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/sampling_layers/TopPSamplingLayer.cu#L30-L109) 与 Top-K 的对应 kernel 类似，但多处理三个退火参数，并注意它的 skip 判定与 Top-K 相反：

```cpp
if (k == 0 && p == 0.0f) { k = 1; }   // 等价贪心
top_ks[i] = k;
top_ps[i] = p < 0.0f ? 0.0f : (p > 1.0f ? 1.0f : p);
skip_decode[i] = k > 0;               // ← 注意：k>0 才 skip（与 Top-K 的 k==0 互补）
initial_top_p_buf[i] = top_ps[i];
top_p_decay_buf[i]   = (top_p_decay == nullptr) ? 1.0f : top_p_decay[i];
top_p_min_buf[i]     = (top_p_min == nullptr) ? 1e-6f : top_p_min[i];   // 防止 top_p 跌到 0
top_p_reset_ids_buf[i] = (top_p_reset_ids == nullptr) ? -1 : (int32_t)top_p_reset_ids[i];
```

`skip_decode[i] = k > 0` 与 Top-K 的 `skip_decode[i] = k == 0` **正好互补**——这正是 u8-l1 所说的「Top-K 与 Top-P 靠 `top_k==0`/`top_p==0` 串行接力」的实现：当某序列既给了 `top_k>0` 又给了 `top_p>0` 时，它在 Top-P 层会被 skip（因为 `k>0`），只走 Top-K 层的 top-p 缩放。`top_p_min=1e-6` 防止退火把 top_p 压到 0 导致无候选。

[`TopPSamplingLayer::setup`](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/sampling_layers/TopPSamplingLayer.cu#L183-L253) 取 `runtime_max_top_p_`，它决定 `invokeBatchTopPSampling` 走单遍还是 radix 排序路径。

**runSampling：排序 + 截断。** [`TopPSamplingLayer::runSampling`](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/sampling_layers/TopPSamplingLayer.cu#L255-L343) 调 [`invokeBatchTopPSampling`](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/sampling_topp_kernels.cu#L1007-L1161)。该方法先决定 `do_radix_sort`，若走 radix 路径，核心是用 CUB 分段基数排序把每个序列的词表降序排好：

```cpp
do_radix_sort = (ENABLE_SINGLE_PASS_TOP_P == 0 || max_top_p >= SINGLE_PASS_THRESHOLD);
if (!do_radix_sort) {
    // 尝试自定义单遍实现（受共享内存大小限制，getSmemSizeAndCheck 返回 <0 则回退）
    segmented_topp_impl::topPPerSegment(context, params, ...);
}
if (do_radix_sort) {
    // 1) 尖峰早退：若 top-1 概率已 >= top_p，跳过排序直接选（见下）
    topp_beam_topk_kernel<T,1,256><<<batch_size,256>>>(...);
    // 2) 否则 CUB 分段降序排序
    cub::DeviceSegmentedRadixSort::SortPairsDescending(cub_temp_storage,
        log_probs, sorted_log_probs, id_vals, sorted_id_vals,
        vocab_size*batch_size, batch_size, begin_offset_buf, offset_buf+1,
        0, sizeof(T)*8, stream);
}
```

注意 Top-P 的工作区规模：`sorted_log_probs` 与 `sorted_id_vals` 各是 `[batch, vocab]` 大小（见 [`invokeBatchTopPSampling` 第 1034-1037 行](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/sampling_topp_kernels.cu#L1034-L1042)），还要加上 CUB 排序的临时存储。这比 Top-K 的工作区（候选数正比于 `k`）大得多——这是 Top-P 比 Top-K 更吃显存的根本原因。

**尖峰早退优化。** [`topp_beam_topk_kernel`](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/sampling_topp_kernels.cu#L801-L860) 是一个聪明的前置优化：先快速求出 top-1 的概率，若它已经 ≥ top_p（分布极度尖峰），说明核里只有这一个 token，直接选它并设置 `begin_offset_buf == offset_buf` 通知后续 `topp_sampling` 跳过排序结果。

```cpp
// 求本序列 top-MAX_K（这里 MAX_K=1）的总概率
TopK<T, MAX_K> total = BlockReduce(...).Reduce(partial, reduce_topk_op<T, MAX_K>);
if (thread_id == 0) {
    T sum_prob = ...; // top-1 概率
    if ((float)sum_prob >= p_threshold) {
        begin_offset_buf[batch_id] += vocab_size;  // 标记：跳过排序段
        // 直接把 top-1 写入候选
    }
}
```

**累积截断采样。** 真正体现「核采样」思想的是 [`topp_sampling`](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/sampling_topp_kernels.cu#L877-L1004) kernel。它在**已排序**的概率数组上做一次 block 级前缀和（累积概率），找到第一个使累积概率超过随机数的位置：

```cpp
// 随机数缩放到 [0, top_p)
rand_num_s = curand_uniform(curandstate + blockIdx.x) * prob_threshold;
// 在排序后的概率上做 inclusive prefix sum（CUB BlockScan）
for (int i = tid; i < end; i += BLOCK_SIZE) {
    float thread_count = sorted_log_probs[offset + i];
    BlockScan(temp_storage).InclusiveSum(thread_count, thread_offset, prefix_op);
    // 哪个 warp 里累积概率首次 >= 随机数？
    uint32_t active_mask = __ballot_sync(0xFFFFFFFF, rand_num_s <= thread_offset);
    if (active_mask != 0) { atomicAdd(&stop_shared, 1); ... }
    if (stop_shared > 0) break;
}
// 在首个命中的 warp 里，定位到具体 lane，取该 token
ids[batch_id] = sorted_id_vals[offset + i_active];
```

这里有一个精妙的等价：随机数 \(r\) 取自 \([0, P)\)，而排序后的累积概率从 \(p_{(1)}\) 单调递增到 1。找到首个累积概率 \(\ge r\) 的位置，由于 \(r < P\)，该位置必然落在累积概率刚跨过 \(P\) 之前或之上的核内——这就等价于「在核 \(\{p_{(1)},\dots,p_{(m)}\}\) 里按概率成比例抽样」。核采样的数学定义被一行 `rand_num_s * prob_threshold` + 一次前缀和就实现了。

最后 [`invokeComputeToppDecay`](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/sampling_layers/TopPSamplingLayer.cu#L332-L340) 按退火规则更新下一步的 `runtime_top_p_buf_`（`top_p *= decay`，但不低于 `top_p_min`，遇到 `reset_id` 则重置为初始值），供下一步采样使用。

#### 4.3.4 代码实践

**实践目标**：亲眼看清「Top-P 必须排序、Top-K 不排序」这一关键差异，并理解累积截断为何只需一次前缀和。

**操作步骤**：

1. 打开 [`invokeBatchTopPSampling`](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/sampling_topp_kernels.cu#L1007-L1161)，找到 `cub::DeviceSegmentedRadixSort::SortPairsDescending` 调用（第 1107 行附近）——这是 Top-P 独有、Top-K 完全没有的「全词表排序」步骤。
2. 打开 [`topp_sampling`](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/sampling_topp_kernels.cu#L877-L1004)，找到第 958–975 行的 `BlockScan(...).InclusiveSum(...)` 循环。
3. 与 [`topk_stage1`](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/sampling_topk_kernels.cu#L130-L203) 对比：确认 Top-K 里没有任何 `Sort` / `Scan` 调用，只有 `BlockReduce`。

**需要观察的现象**：

- Top-P 工作区里有 `sorted_log_probs`、`sorted_id_vals` 两个 `[batch, vocab]` 大缓冲（第 1034–1037 行），外加 CUB 排序临时存储；Top-K 工作区只有 `temp_log_probs` + `topk_tmp_id_buf` + `topk_tmp_val_buf`，大小正比于 `k` 而非 `vocab`。
- `topp_sampling` 的核心是前缀和 `InclusiveSum` 配合 `__ballot_sync` 找首个跨阈值位置；`topk_stage2_sampling` 的核心是迭代归约 + 沿降序扣减随机数。

**预期结果**：你能填出下表（这也是本讲的综合实践之一）。属源码阅读型实践，无需运行。

| 维度 | Top-K | Top-P |
| --- | --- | --- |
| 是否排序 | 否（迭代 block reduction） | 是（CUB 分段基数排序 / 单遍） |
| 候选集合大小 | 固定 K | 数据相关（累积概率达 P 为止） |
| 核心并行原语 | `BlockReduce`（top-2 归约） | `DeviceSegmentedRadixSort` + `BlockScan`（前缀和） |
| 工作区规模 | \(O(k)\) | \(O(\text{vocab})\)（排序缓冲 + CUB 临时存储） |
| 复杂度量级 | \(O(k \cdot V / B)\) | \(O(V \log V)\)（排序主导） |

#### 4.3.5 小练习与答案

**练习 1**：为什么 `topp_sampling` 里把随机数缩放到 \([0, \text{top\_p})\) 而不是 \([0, 1)\)？

**参考答案**：因为「核」定义为累积概率首次超过 top_p 的最小集合。把随机数限制在 \([0, \text{top\_p})\)，再在单调递增的累积概率上找首个 \(\ge r\) 的位置，就保证选中的 token 一定落在核内；且在核内各 token 被选中的概率正比于其自身概率。这等价于先截断出核、再在核内归一化采样，但只需一次前缀和即可完成。

**练习 2**：`topp_beam_topk_kernel` 这个「尖峰早退」优化在什么情况下生效？省掉了什么？

**参考答案**：当某序列 top-1 token 的概率已经 ≥ top_p 时生效（分布极度尖峰，核里只有一个 token）。机制是一个巧妙的 offset 技巧：`topp_beam_topk_kernel` 把该序列的 `begin_offset_buf` 设成 `offset + vocab_size`（即等于该序列的段尾偏移），并把 top-1 直接写进 `sorted_id_vals[offset]`。这样后续 CUB 分段排序对该序列的段是空的（`begin == end`，不做事），而 `topp_sampling` 在 [第 918 行](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/sampling_topp_kernels.cu#L918-L939) 检测到 `begin_offset_buf == offset_buf`（注意这里的 `offset_buf` 是调用方传入的 `offset_buf+1`，即段尾）后直接取 top-1 并返回。省掉的是该序列对整个词表的扫描与前缀和；排序 kernel 虽然仍会为整批启动一次，但对尖峰序列是空操作。

**练习 3**：`top_p_min_buf` 默认 `1e-6`，为什么不能是 0？

**参考答案**：退火机制每步把 `top_p *= decay`（decay<1），若不加下界，top_p 会衰减到 0，导致核为空、采样无候选。`top_p_min` 给了一个正下界，保证核至少有一个 token。

## 5. 综合实践

**任务**：完成一份《Top-K vs Top-P 并行实现差异分析报告》，把本讲散落各处的对比串成一篇连贯的源码阅读笔记。

**具体要求**：

1. **为什么 Top-P 必须排序而 Top-K 不必**——用你自己的话写一段（不超过 200 字），关键词必须包括「固定基数」「累积概率」「数据相关」。参考 4.2.1 与 4.3.1。
2. **画出两者的 kernel 数据流对比图**——并排画两张：左边 Top-K（`topk_stage1` → `topk_stage2_sampling`），右边 Top-P（`topp_beam_topk_kernel` 早退 / `SortPairsDescending` → `topp_sampling`）。标注每一步用的是 `BlockReduce`、`Sort` 还是 `BlockScan`。
3. **填出 4.3.4 的对比表**，并为每一行给出**源码证据**（文件 + 行号的永久链接）。例如「工作区规模」一行的证据：Top-K 见 [`invokeBatchTopKSampling` 第 369-371 行](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/sampling_topk_kernels.cu#L369-L371)（缓冲正比于 `max_top_k`），Top-P 见 [`invokeBatchTopPSampling` 第 1034-1037 行](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/sampling_topp_kernels.cu#L1034-L1042)（缓冲正比于 `vocab_size`）。
4. **回答一个设计题**：若你要为一个**极小词表**（如 V=128）的模型选采样策略，Top-K 和 Top-P 谁的开销更低？为什么？提示：考虑 Top-P 的排序开销在 V 很小时几乎可忽略，而 Top-K 的多轮归约与 block 并行度反而可能因 V 小而无法打满 GPU。

**预期结果**：一份带源码链接的分析报告，能清晰说明「Top-K 用归约选固定个数、Top-P 用排序 + 前缀和截断动态个数」这一本质差异，并能据此判断不同场景下的策略选择。这是把本讲三个最小模块（BaseSamplingLayer 公共管线、Top-K 归约、Top-P 排序截断）融会贯通的练习。

## 6. 本讲小结

- FT 采样层用「模板方法」把公共的 logits 修正（温度 / 偏置 / 重复惩罚 / 最小长度）抽到 `BaseSamplingLayer::forward`，只把「选 token」留给纯虚 `runSampling`，由 Top-K / Top-P 子类实现。
- `skip_decode`（Top-K 用 `k==0`、Top-P 用 `k>0`，两者互补）让 Top-K 与 Top-P 在同一步串行接力；`skip_any_` + `runtime_logits_buf_` 防止先执行的层污染后执行层的 logits 输入。
- **Top-K 不排序**：用 `topk_stage1`（多 block 并行扫描词表，迭代 `BlockReduce` 取 top-2、置 -INF 挖掉已选）+ `topk_stage2_sampling`（合并候选、沿降序扣减随机数）选出固定 K 个候选再采样，复杂度 \(O(k\cdot V/B)\)，工作区正比于 K。
- **Top-P 必须排序**：因为「核」的边界（累积概率首次超过 top_p）只有在概率降序后才能确定。它用 CUB `DeviceSegmentedRadixSort`（或自定义单遍 `topPPerSegment`）排全词表，再由 `topp_sampling` 用一次 `BlockScan` 前缀和 + 首个跨阈值定位完成核内采样，工作区正比于词表大小。
- Top-P 的累积截断有个精妙等价：随机数取 \([0,\text{top\_p})\)、在单调累积概率上找首个 \(\ge r\) 的位置，等价于「截断出核 + 核内按概率抽样」，只需一次前缀和。
- Top-P 额外支持退火（`top_p_decay` / `top_p_min` / `top_p_reset_ids`），在生成过程中逐步收紧采样分布；并有尖峰早退优化（`topp_beam_topk_kernel`）在分布极度集中时跳过扫描。

## 7. 下一步学习建议

- **回到 u8-l1** 复盘：现在你已看清 Top-K / Top-P 的内部，可以重读 `DynamicDecodeLayer::forward` 中 `beam_width==1` 分支，确认它如何用 `skip_decode` 把两个采样层串起来，以及 `hasDiffRuntimeArgs` 为何只对 beam search 生效而对采样无影响。
- **阅读采样前的 penalty kernel**：本讲多次提到 `invokeBatchApplyTemperaturePenalty` / `invokeBatchApplyRepetitionPenalty` / `invokeMinLengthPenalty`，建议打开 `src/fastertransformer/kernels/sampling_penalty_kernels.cu` 看它们具体如何改写 logits，补全「采样前修正」的细节。
- **对比 beam search（u8-l2）**：beam search 的 topk 是「跨 beam 收缩回 beam_width 条序列」，而 Top-K 采样是「单序列选 K 个候选再抽一个」——两者都用 topk kernel 但语义不同，对比阅读 `beam_search_topk_kernels.cu` 与 `sampling_topk_kernels.cu` 能加深理解。
- **运行单元测试**：`tests/unittests/test_sampling.cu` 同时测了 Top-K、Top-P 与 `DynamicDecodeLayer`，包含「同批混用 Top-K / Top-P」的用例（见第 1368 行附近的 `runtime_top_k`/`runtime_top_p` 构造）。阅读这些用例能验证你对 `skip_decode` 串行机制的理解，是本讲最佳的「可运行实践」入口（结合 u11-l1 的测试章节）。
