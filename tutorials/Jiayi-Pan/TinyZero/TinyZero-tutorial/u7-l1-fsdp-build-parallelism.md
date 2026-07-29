# FSDP 模型构建与并行策略

## 1. 本讲目标

本讲聚焦 `ActorRolloutRefWorker` 内部「把一份 HuggingFace 权重变成可训练的分布式 FSDP 模型」的过程。读完本讲，你应当能够：

- 说清 `_build_model_optimizer` 的完整步骤：用 meta device 加载权重、`MixedPrecision` 混合精度、`FULL_SHARD` 与 `SHARD_GRAD_OP` 两种分片策略的取舍。
- 理解 `device_mesh` 的拓扑：为什么 FSDP 用一维 mesh，而 Ulysses 序列并行需要 `(dp, sp)` 二维 mesh，二者如何共享同一组 GPU。
- 看懂 `param_offload` / `grad_offload` / `optimizer_offload` 三个显存优化开关在哪里读取、在哪里生效。
- 回答实践题：为什么 actor 用 fp32 创建、ref 用 bf16 创建。

本讲是 u6-l1（混合引擎）的下钻：u6-l1 讲「一个 Worker 如何同时扮演三种角色」，本讲讲「这个 Worker 内部的模型到底是怎么用 FSDP 搭起来的」。

## 2. 前置知识

### 2.1 数据并行、张量并行、序列并行

- **数据并行（Data Parallel, DP）**：同一份模型完整复制到每张卡，每张卡处理不同 batch，反向后用 `AllReduce` 同步梯度。显存随模型大小线性增长，单卡放不下整个模型时不可行。
- **FSDP（Fully Sharded Data Parallel）**：DP 的「省显存版」。把模型参数、梯度、优化器状态**切碎分到每张卡**，前向/反向时按需 `AllGather` 临时拼回整份，用完即释放。对应 DeepSpeed 的 ZeRO-3。TinyZero 默认走这条路。
- **张量并行（Tensor Parallel, TP）**：把单个矩阵乘法切开分到多卡，用于 vLLM rollout 推理（`tensor_model_parallel_size`）。
- **序列并行（Sequence Parallel, SP）**：不切模型，而是把**序列长度**这一维切开分到多卡（如 8192 长序列切成 4 段各 2048），在 attention 处用 `AllToAll` 重排。本项目中对应 Ulysses，是可选的「长序列加速器」。

### 2.2 meta device 与混合精度

- **meta device**：PyTorch 的「占位设备」。在 meta 上创建的张量只有 shape、不占实际显存。这样每个 rank 先建好「空壳」模型拓扑，再由 rank 0 广播真实权重，避免每张卡都把 3B 权重加载进内存。
- **MixedPrecision**：FSDP 的混合精度配置。参数主副本保持 `fp32`（供优化器更新），但前向计算时临时转成 `bf16`，兼顾训练稳定性与显存/速度。

### 2.3 承接 u6-l1 的关键认知

u6-l1 已建立：`ActorRolloutRefWorker` 用 `role` 字符串派生 `_is_actor` / `_is_rollout` / `_is_ref` 三个标志，actor 与 rollout **共享同一份 FSDP 权重**（HybridEngine 的精髓），ref **独立**构建一份冻结权重。本讲要回答的正是：这两份（或一份）FSDP 权重具体是怎么搭出来的。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [verl/workers/fsdp_workers.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py) | `ActorRolloutRefWorker` 主类。本讲重点读 `__init__`（建 mesh）、`_build_model_optimizer`（建 FSDP 模型）、`_build_rollout`（建 rollout 引擎）、`init_model`（组装与 offload）。 |
| [verl/utils/fsdp_utils.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/fsdp_utils.py) | FSDP 配套工具：`get_fsdp_wrap_policy`（切分粒度）、`init_fn` / `get_init_weight_context_manager`（meta 加载）、一整套 `offload_*` / `load_*` 函数（显存换 CPU）。 |
| [verl/utils/ulysses.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/ulysses.py) | Ulysses 序列并行核心：`SeqAllToAll`、`ulysses_pad_and_slice_inputs`。 |
| [verl/workers/sharding_manager/fsdp_ulysses.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/sharding_manager/fsdp_ulysses.py) | `FSDPUlyssesShardingManager`：在 SP 组上做数据 AllGather/chunk，连接 FSDP 数据切分与 Ulysses 序列切分。 |
| [verl/trainer/config/ppo_trainer.yaml](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/config/ppo_trainer.yaml) | `fsdp_config`（含 `param_offload` 等）的默认值来源。 |
| [scripts/train_tiny_zero.sh](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/scripts/train_tiny_zero.sh) | TinyZero 训练入口，本讲用于定位 offload 与 gradient checkpointing 配置。 |

## 4. 核心概念与源码讲解

### 4.1 device mesh：FSDP 与 Ulysses 序列并行的拓扑

#### 4.1.1 概念说明

**device mesh（设备网格）** 是 PyTorch `torch.distributed.device_mesh` 提供的「逻辑 GPU 编排视图」。它把物理上的 N 张卡，组织成一个多维网格，每一维对应一种并行策略的通信组。本项目中存在两套 mesh：

- **FSDP mesh**：一维 `(world_size,)`，整组 GPU 构成一个 FSDP 分片组，参数在这 N 张卡间切碎。
- **Ulysses mesh**：二维 `(dp, sp)`，`dp` 维是数据并行组（各自处理不同 batch），`sp` 维是序列并行组（同一条序列在这些卡间沿序列长度切开）。

二者关系是一个**除法约束**：

\[ \text{world\_size} = \text{dp} \times \text{sp} \]

`sp` 越大，能处理的序列越长，但留给数据并行的卡 `dp` 就越少。TinyZero 默认 `sp=1`（不开 Ulysses），此时 `dp = world_size`，退化为纯 FSDP。

#### 4.1.2 核心流程

`ActorRolloutRefWorker.__init__` 在初始化进程组后立刻建 mesh：

1. 读 `world_size`，无条件建一维 FSDP mesh。
2. 读配置 `ulysses_sequence_parallel_size`（默认 1）；若 `> 1`，则按 `dp = world_size // sp` 建二维 mesh，并实例化 `FSDPUlyssesShardingManager`。
3. 之后所有「数据按 DP 切分、按 SP 重排」都交给这个 sharding manager。

#### 4.1.3 源码精读

FSDP mesh 与 Ulysses mesh 的建立：[verl/workers/fsdp_workers.py:60-75](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py#L60-L75) —— 第 64 行建一维 `(world_size,)` 的 FSDP mesh；第 68-73 行在 `sp > 1` 时建二维 `(dp, sp)` 的 Ulysses mesh，维度名分别叫 `'dp'` 与 `'sp'`。第 75 行把 mesh 交给 `FSDPUlyssesShardingManager`。

随后还有一段**配置归一化**，它在 mesh 建好后把全局 batch 尺寸换算成「每 DP rank 的尺寸」：[verl/workers/fsdp_workers.py:96-101](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py#L96-L101) —— 先 `// dp`（每 rank 的量），再 `* rollout.n`（每个 prompt 采样 n 条，对应 GRPO 的多次采样）。

Ulysses 的核心通信原语是 `SeqAllToAll`：[verl/utils/ulysses.py:164-194](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/ulysses.py#L164-L194) —— forward 用 `all_to_all` 在 `scatter_dim`（序列维）与 `gather_dim`（注意力头维）间重排，使每张卡在 attention 时持有「全序列的 1/sp 头」；backward 自动做反向 AllToAll，无需手写梯度通信。

sharding manager 在每次前向前做 SP 组的 AllGather、前向后做 chunk 还原：[verl/workers/sharding_manager/fsdp_ulysses.py:58-88](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/sharding_manager/fsdp_ulysses.py#L58-L88) —— `preprocess_data` 在 `sp` 组上把数据 AllGather 放大 `sp_size` 倍（保证 SP 组内各卡输入相同），`postprocess_data` 取本 rank 对应的 `chunk` 还原成 DP 分片。

#### 4.1.4 代码实践

**实践目标**：验证 `world_size = dp × sp` 的换算。

**操作步骤**：

1. 阅读 [verl/workers/fsdp_workers.py:66-73](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py#L66-L73)。
2. 假设 `world_size = 8`，分别取 `sp = 1` 与 `sp = 4`，手算 `dp` 与 mesh 形状。

**需要观察的现象 / 预期结果**：

- `sp = 1`：`dp = 8`，mesh 形状 `(8,)`，纯 FSDP。
- `sp = 4`：`dp = 2`，mesh 形状 `(2, 4)`，2 路数据并行 × 4 路序列并行。

若 `sp` 不能整除 `world_size`，`dp = world_size // sp` 会算出错误维度（注意代码用的是整除，未做取模校验，因此使用者须自行保证可整除）。**待本地验证**：在真实多卡环境打印 `self.ulysses_device_mesh.shape` 是否与手算一致。

#### 4.1.5 小练习与答案

**练习 1**：为什么 FSDP mesh 是一维、而 Ulysses mesh 是二维？

**参考答案**：FSDP 把所有卡当作一个「分片池」，参数在整个池里切碎，所以一维 `(world_size,)` 足矣；Ulysses 需要区分「哪些卡处理同一 batch（dp 组）」和「哪些卡切同一条序列（sp 组）」两种角色，所以必须用二维 `(dp, sp)` 表达。

**练习 2**：`ulysses_sequence_parallel_size = 1` 时，`FSDPUlyssesShardingManager` 还会做通信吗？

**参考答案**：不会。其 `__enter__`/`preprocess_data`/`postprocess_data` 都以 `if self.device_mesh is not None` 守卫（`sp=1` 时 `device_mesh=None`），直接原样返回数据，等价于 no-op。

---

### 4.2 _build_model_optimizer：从 HF 权重到 FSDP 模型

#### 4.2.1 概念说明

这是本讲的主角。它把一个 HuggingFace checkpoint 路径变成一个 FSDP 包装好的可训练模块，外加可选的优化器与学习率调度器。关键设计点有三个：

1. **meta device 加载**：rank 0 真正读权重到 CPU，其余 rank 用 `init_empty_weights` 建空壳；FSDP 的 `sync_module_states=True` 再把 rank 0 的权重广播给所有 rank。这样避免每卡重复加载 3B 权重。
2. **混合精度**：actor 在 fp32 主权重 + bf16 计算；ref 直接 bf16、无混合精度。
3. **分片策略与优化器归属**：只有 actor 才建优化器（`AdamW`）；ref 与纯 rollout 不建。

#### 4.2.2 核心流程

```
_build_model_optimizer(model_path, fsdp_config, optim_config, ...)
├─ 1. 读 AutoConfig，覆盖 bos/eos/pad token，可选 monkey_patch（Ulysses+rmpad）
├─ 2. get_init_weight_context_manager(use_meta_tensor=not tie_word_embeddings)
│     └─ rank 0 → CPU；其余 rank → meta 空壳
├─ 3. AutoModelForCausalLM.from_pretrained(...)  # 加载/建壳
│     └─ enable_gradient_checkpointing → use_reentrant=False
├─ 4. 构造 MixedPrecision（ref 置 None）
├─ 5. get_fsdp_wrap_policy(...) → 决定切分粒度
├─ 6. 选 sharding_strategy：有 wrap_policy→FULL_SHARD，否则→SHARD_GRAD_OP
├─ 7. FSDP(actor_module, param_init_fn=init_fn, sync_module_states=True, ...)
└─ 8. 仅 actor：建 AdamW + warmup 常数调度器
```

#### 4.2.3 源码精读

**① 加载 dtype 的选择**——这是实践题的答案所在：[verl/workers/fsdp_workers.py:128-137](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py#L128-L137) —— 第 128 行注释直言「必须在 fp32 创建，否则优化器会变成 bf16，这是错的」；第 134 行：默认（`model_dtype` 未设）下，`_is_actor` 用 `torch.float32`，否则（ref）用 `torch.bfloat16`。

**② meta device 加载**：[verl/workers/fsdp_workers.py:159-170](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py#L159-L170) —— 第 160 行用 `not tie_word_embeddings` 决定是否走 meta（绑定词嵌入会让 meta 初始化卡死，见第 159 行注释），随后 `from_pretrained` 在该上下文里加载。

配套的两个工具函数在 `fsdp_utils.py`：

- `get_init_weight_context_manager`：[verl/utils/fsdp_utils.py:36-43](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/fsdp_utils.py#L36-L43) —— `use_meta_tensor=True` 时，rank 0 返回 CPU 设备上下文（真实权重），其余 rank 返回 `init_empty_weights`（meta 空壳）；`use_meta_tensor=False` 时所有 rank 都用 CPU。
- `init_fn`：[verl/utils/fsdp_utils.py:29-33](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/fsdp_utils.py#L29-L33) —— 作为 FSDP 的 `param_init_fn`，非 rank 0 的子模块用 `to_empty` 在 GPU 上物化空壳（参数随后由 `sync_module_states=True` 从 rank 0 广播填充）。

**③ 梯度检查点**：[verl/workers/fsdp_workers.py:172-173](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py#L172-L173) —— `enable_gradient_checkpointing=True` 时调用 HF 模型的 `gradient_checkpointing_enable`，注意强制 `use_reentrant=False`（与新版 PyTorch/FSDP 兼容）。

**④ 混合精度**：[verl/workers/fsdp_workers.py:182-195](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py#L182-L195) —— 默认 `param_dtype=bf16, reduce_dtype=fp32, buffer_dtype=fp32`；但 `_is_ref` 时第 195 行把 `mixed_precision` 置为 `None`，即 ref 完全不混合精度、用加载时的原始 dtype（bf16）计算。

**⑤ FSDP 包装**：[verl/workers/fsdp_workers.py:205-222](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py#L205-L222) —— 这是核心一步。`sharding_strategy` 的二选一在第 206-209 行（详见 4.3）；FSDP 关键参数：`param_init_fn=init_fn`（物化空壳）、`use_orig_params=False`、`sync_module_states=True`（rank0 广播权重）、`device_mesh=self.device_mesh`（绑定 4.1 的 FSDP mesh）、`forward_prefetch=False`。

**⑥ 优化器归属**：[verl/workers/fsdp_workers.py:227-244](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py#L227-L244) —— 仅 `_is_actor` 时建 `AdamW` + `get_constant_schedule_with_warmup`；ref / 纯 rollout 走 `else` 分支返回 `None`，这正是 ref「冻结不训练」的体现。

#### 4.2.4 代码实践

**实践目标**：解释「actor 用 fp32、ref 用 bf16」的根因。

**操作步骤**：

1. 阅读 [verl/workers/fsdp_workers.py:128-137](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py#L128-L137) 与第 227-244 行的优化器构建。
2. 思考：AdamW 会在 fp32 还是 bf16 下维护一阶/二阶动量？

**需要观察的现象 / 预期结果（参考答案）**：

- **actor 用 fp32**：因为 actor 要训练，AdamW 的优化器状态（动量 `m`、`v`）会继承参数的 dtype。若模型本身是 bf16，优化器状态也会是 bf16，精度损失会导致训练不稳定甚至发散（见第 128 行注释）。fp32 主权重保证优化器在 fp32 下更新；前向计算时由 `MixedPrecision(param_dtype=bf16)` 临时降精度，兼顾速度。
- **ref 用 bf16**：ref 是冻结的参考策略，**没有优化器**（走 `else` 分支），且 `mixed_precision=None`。它只做只读前向算 `ref_log_prob`，bf16 既够用又省显存。

> 进一步验证：注意 actor 的「fp32 主权重 + bf16 计算」是两层——加载 dtype（fp32）决定主权重与优化器，`MixedPrecision` 决定前向计算 dtype。二者不要混淆。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `init_context` 用 `use_meta_tensor=not tie_word_embeddings`？

**参考答案**：当模型 `tie_word_embeddings=True`（embedding 与 lm_head 共享权重）时，meta 初始化会因为共享参数的多次注册而卡死（见第 159 行注释），此时退化为所有 rank 用 CPU 加载；不共享时才用 meta 空壳节省内存。

**练习 2**：`sync_module_states=True` 解决了什么问题？

**参考答案**：非 rank 0 的卡用 meta 建了空壳（无真实值），`sync_module_states=True` 让 FSDP 在初始化时把 rank 0 的真实权重广播到所有 rank，使各 rank 的 FSDP 分片都拿到正确初值。

---

### 4.3 get_fsdp_wrap_policy：决定 FSDP 切分粒度

#### 4.3.1 概念说明

FSDP 不会把整个模型当作一个分片单元，而是把模型**按子模块切**：每个被 wrap 的子模块独立地「参数分片 → 前向 AllGather → 反向再分片」。切分粒度由 **wrap policy** 决定：

- 切得太粗（如整模型一个单元）：通信少但省显存效果差。
- 切得太细（每个 Linear 都 wrap）：省显存但通信开销激增。

verl 支持两种策略：按 Transformer 层类名 wrap（`transformer_auto_wrap_policy`），或按参数量阈值 wrap（`size_based_auto_wrap_policy`）。

#### 4.3.2 核心流程

`get_fsdp_wrap_policy(module, config)` 按配置返回一个 `auto_wrap_policy`（或 `None`）：

1. `config.disable=True` → 返回 `None`（不切分）。
2. `min_num_params > 0` → 用参数量阈值策略。
3. 否则若给了 `transformer_layer_cls_to_wrap`（或回退到模型的 `_no_split_modules`）→ 用 Transformer 层类名策略。
4. 都没有 → 返回 `None`。

而返回值直接决定 `sharding_strategy`：**有 wrap policy → `FULL_SHARD`（ZeRO-3）；无 → `SHARD_GRAD_OP`（ZeRO-2 风格）**。

#### 4.3.3 源码精读

wrap policy 的查表逻辑：[verl/utils/fsdp_utils.py:48-76](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/fsdp_utils.py#L48-L76) —— 第 55-57 行先取默认的 `_no_split_modules`（HF 模型自带的「可切分层类名」），再用配置覆盖；第 60-61 行走参数量阈值分支；第 62-75 行走 Transformer 层类名分支，用 `functools.partial` 把类集合固定进 `transformer_auto_wrap_policy`。

sharding strategy 的二选一：[verl/workers/fsdp_workers.py:205-209](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py#L205-L209) —— `auto_wrap_policy is None` → `SHARD_GRAD_OP`；否则 `FULL_SHARD`（第 218 行注释把它标为 `# zero3`）。

两种策略的取舍：

| 策略 | 切分对象 | 通信量 | 显存 | 类比 |
| --- | --- | --- | --- | --- |
| `FULL_SHARD` | 参数 + 梯度 + 优化器状态 | 多（前向/反向都 AllGather） | 最省 | ZeRO-3 |
| `SHARD_GRAD_OP` | 仅梯度 + 优化器状态；参数前向时保持聚合 | 少 | 较费 | ZeRO-2 |

一个特例：HF rollout 时强制关掉 wrap policy：[verl/workers/fsdp_workers.py:199-201](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py#L199-L201) —— 因为 auto wrap 会让 Gemma 等模型的 HFRollout 卡死，故对 `hf` rollout 置 `None`（即走 `SHARD_GRAD_OP`）。TinyZero 默认用 `vllm` rollout，不受此影响。

> 注意：Critic 始终用 `FULL_SHARD`，见 [verl/workers/fsdp_workers.py:622-630](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py#L622-L630)，没有这个二选一分支。

#### 4.3.4 代码实践

**实践目标**：理解默认配置下 wrap policy 走哪条分支。

**操作步骤**：

1. 打开 [verl/trainer/config/ppo_trainer.yaml:42-49](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/config/ppo_trainer.yaml#L42-L49)，看 actor 的 `fsdp_config.wrap_policy`：`min_num_params: 0`，且 `transformer_layer_cls_to_wrap` 被注释（为 None）。
2. 追踪 `get_fsdp_wrap_policy` 在该配置下的返回值。

**需要观察的现象 / 预期结果**：`min_num_params=0` 且未设类名 → 回退到模型的 `_no_split_modules`。Qwen2.5 等主流模型自带 `_no_split_modules`（如 `Qwen2DecoderLayer`），因此会走 Transformer 层类名分支，返回非空 policy → actor 最终用 `FULL_SHARD`。若某模型 `_no_split_modules=None`，则返回 `None` → `SHARD_GRAD_OP`。运行时打印的 `wrap_policy: {...}`（第 203 行）即可确认。**待本地验证**：在真实环境查看该打印行。

#### 4.3.5 小练习与答案

**练习 1**：把 `min_num_params` 设成一个很大的数（如 `1e9`），会发生什么？

**参考答案**：`size_based_auto_wrap_policy` 只在子模块参数量 ≥ 该阈值时才 wrap。阈值极大时几乎没有子模块达标，等价于不切分，行为接近返回 `None`，但 `auto_wrap_policy` 本身非空，因此 `sharding_strategy` 仍是 `FULL_SHARD`——只是切分粒度变成整模型，省显存效果变差。

**练习 2**：为什么 `SHARD_GRAD_OP` 通信更少？

**参考答案**：`SHARD_GRAD_OP` 在前向时不释放参数（保持聚合态），省掉了前向前的 AllGather；只在反向计算梯度时才分片，因此通信次数少于 `FULL_SHARD`，但代价是参数常驻显存，显存占用更高。

---

### 4.4 _build_rollout 与 offload 显存优化

#### 4.4.1 概念说明

本模块讲两件收尾的事：

1. **`_build_rollout`**：在 FSDP 模型建好后，再建一个 rollout 引擎（vLLM 或 HF）及其 sharding manager。它有自己的 `(dp, infer_tp)` mesh——这是**推理张量并行**，与 4.1 的 FSDP/Ulysses mesh 是不同维度的并行。
2. **offload 三开关**：`param_offload` / `grad_offload` / `optimizer_offload`。开启后，参数/梯度/优化器状态在「不用时」搬到 CPU 内存、「用时」搬回 GPU，用时间换显存。

#### 4.4.2 核心流程

`_build_rollout` 流程：

```
_build_rollout()
├─ infer_tp = config.rollout.tensor_model_parallel_size
├─ dp = world_size // infer_tp
├─ rollout_device_mesh = init_device_mesh('cuda', (dp, infer_tp), ['dp','infer_tp'])
└─ 按 config.rollout.name 分支：
   ├─ 'hf'  → HFRollout + BaseShardingManager（no-op）
   └─ 'vllm' → vLLMRollout(actor_module=actor_module_fsdp, ...)
              + FSDPVLLMShardingManager(module, inference_engine, full_params, device_mesh)
```

offload 的使用模式（在 `init_model` / `update_actor` / `generate_sequences` / `compute_ref_log_prob` 中反复出现）：

```
计算前：load_fsdp_param_and_grad / load_fsdp_optimizer   # CPU → GPU
计算...
计算后：offload_fsdp_param_and_grad / offload_fsdp_optimizer  # GPU → CPU
```

#### 4.4.3 源码精读

**① rollout mesh 与引擎选择**：[verl/workers/fsdp_workers.py:250-282](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py#L250-L282) —— 第 253-256 行建 `(dp, infer_tp)` mesh 并断言 `world_size % infer_tp == 0`；第 258-262 行是 HF 分支（直接复用 `self.actor_module_fsdp`，配 no-op 的 `BaseShardingManager`）；第 264-280 行是 vLLM 分支：把 **同一个 `actor_module_fsdp`** 交给 `vLLMRollout`（这正是 u6-l1 所说的 actor/rollout 共享物理权重），再建 `FSDPVLLMShardingManager`。注意第 278 行 `full_params='hf' in load_format`：单卡时第 274 行把 `load_format` 改成 `'dummy_hf'`，从而走整权重同步路径（权重同步细节见 u6-l5）。

**② offload 开关的读取**：[verl/workers/fsdp_workers.py:84-93](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py#L84-L93) —— 三个布尔标志 `_is_offload_param/grad/optimizer` 仅对 actor 从 `actor.fsdp_config` 读；ref 仅读 `param_offload`。

**③ offload 的搬运函数**：[verl/utils/fsdp_utils.py:93-110](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/fsdp_utils.py#L93-L110)（参数+梯度）/ [verl/utils/fsdp_utils.py:113-130](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/fsdp_utils.py#L113-L130)（优化器）。它们逐参数 `.to('cpu', non_blocking=True)` 或 `.to(device_id)`，并 `torch.cuda.empty_cache()`。注意第 95-96 行会连 FSDP 内部的 `_local_shard` 一起搬。

**④ 在生命周期里的实际调用**：

- 初始化后立刻 offload：[verl/workers/fsdp_workers.py:315-321](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py#L315-L321)（actor）/ [verl/workers/fsdp_workers.py:342-343](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py#L342-L343)（ref）。
- `update_actor`：[verl/workers/fsdp_workers.py:360-365](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py#L360-L365)（计算前 load）与 [verl/workers/fsdp_workers.py:393-396](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py#L393-L396)（计算后 offload）。

> 对比：`RewardModelWorker` 走的是 FSDP 原生的 `cpu_offload=CPUOffload(offload_params=...)`（[verl/workers/fsdp_workers.py:845](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py#L845)），由 FSDP hook 自动搬运，而非上面这套手写函数。TinyZero 默认关闭奖励模型，用不到它。

#### 4.4.4 代码实践

**实践目标**：定位 offload 与 gradient checkpointing 配置项，理解 README 的 OOM 建议。

**操作步骤**：

1. 在 [ppo_trainer.yaml](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/config/ppo_trainer.yaml) 中定位三处 `fsdp_config`：actor（L42-L49）、ref（L50-L56）、critic（L101-L108）。确认 `param_offload/grad_offload/optimizer_offload` 默认全是 `False`。
2. 定位 `enable_gradient_checkpointing`：actor 侧在 [ppo_trainer.yaml:19](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/config/ppo_trainer.yaml#L19)，critic 侧在 [ppo_trainer.yaml:99](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/config/ppo_trainer.yaml#L99)，默认也是 `False`。
3. 阅读 [README.md:52](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/README.md#L52)：OOM 时建议加 `critic.model.enable_gradient_checkpointing=True`。

**需要观察的现象 / 预期结果**：

- 这三个 offload 开关是「显存不够时的旋钮」：`param_offload` 省「参数显存」、`optimizer_offload` 省「AdamW 状态显存」（AdamW 状态通常是参数量的 2 倍，收益最大）、`grad_offload` 省「梯度显存」。代价是每次计算前后的 CPU↔GPU 搬运延迟。
- `enable_gradient_checkpointing` 是另一种省显存手段：前向时不保留中间激活，反向时重算，用计算时间换激活显存。README 把它作为 OOM 的首选建议，因为它不引入 CPU↔GPU 搬运开销。
- 之所以建议先开 `critic.model.enable_gradient_checkpointing`：TinyZero 同时 colocate 了 actor 与 critic（见 u3-l2），critic 还额外要存 value head 与 returns，往往是先 OOM 的那一侧。

> 在 [train_tiny_zero.sh](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/scripts/train_tiny_zero.sh) 里**没有**出现这些 offload / gradient_checkpointing 的覆盖，说明 TinyZero 默认配置在 3B/2 卡下不靠它们也能跑；它们是留给更紧显存环境的逃生通道。**待本地验证**：在小显存卡上分别单独开启 `critic.model.enable_gradient_checkpointing=True` 与 `actor_rollout_ref.actor.fsdp_config.optimizer_offload=True`，观察峰值显存变化。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `optimizer_offload` 通常比 `param_offload` 更「划算」？

**参考答案**：AdamW 为每个参数维护一阶动量 `m` 与二阶动量 `v`，状态量约是参数量的 2 倍，且只在反向更新时才需要。把它 offload 到 CPU 能省下最大的一块显存，而搬运频率较低（每步更新前后各一次）；参数则在前向/反向都频繁用到，`param_offload` 的搬运开销更高。

**练习 2**：rollout 的 `(dp, infer_tp)` mesh 和 FSDP 的 `(world_size,)` mesh 冲突吗？

**参考答案**：不冲突，它们是**不同阶段**的并行拓扑。训练（`update_actor`）用 FSDP mesh 把参数切碎；rollout 生成时，vLLM 用 `(dp, infer_tp)` mesh 做推理张量并行。两者通过 `FSDPVLLMShardingManager` 在每次 `generate_sequences` 时切换（详见 u6-l5）。

---

## 5. 综合实践

**任务**：为一次「显存吃紧」的 3B 训练，设计一套 FSDP 调优方案，并解释每项改动的源码依据。

**步骤**：

1. **画拓扑**：假设 `N_GPUS=2`、`ROLLOUT_TP_SIZE=2`、`ulysses_sequence_parallel_size=1`。画出三类 mesh：
   - FSDP mesh：`(2,)`
   - Ulysses mesh：`None`（sp=1）
   - rollout mesh：`dp=2//2=1`, `(1, 2)`
   说明它们分别在哪段代码生效（参考 4.1.3 与 4.4.3）。

2. **定位 OOM 旋钮**：阅读 [README.md:52](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/README.md#L52)，在 [train_tiny_zero.sh](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/scripts/train_tiny_zero.sh) 末尾追加一行 `critic.model.enable_gradient_checkpointing=True`，并说明它会触发 [fsdp_workers.py:598-599](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py#L598-L599) 的 critic 梯度检查点（注意 critic 走的是 `_build_critic_model_optimizer`，与 actor 的 4.2 路径平行）。

3. **若仍 OOM**：进一步追加 `actor_rollout_ref.actor.fsdp_config.optimizer_offload=True`，追踪它如何经 [fsdp_workers.py:88-90](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py#L88-L90) 读入、再在 [fsdp_workers.py:319-321](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py#L319-L321) 与 [393-396](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py#L393-L396) 生效。

4. **回答原理题**：解释为何在此方案下，actor 仍是 fp32 主权重 + bf16 计算（参考 4.2.4），而开启 offload 不会改变这一点——offload 只搬位置、不改 dtype。

**预期结果**：得到一份「拓扑图 + 三条调参指令 + 各自源码依据」的调优清单。开启后峰值显存应下降，代价是训练吞吐降低（gradient checkpointing 重算 + offload 搬运）。**待本地验证**：实际运行并对比 `wandb` 中显存与 MFU 指标。

## 6. 本讲小结

- veRL 用 PyTorch `device_mesh` 编排并行拓扑：FSDP 用一维 `(world_size,)` mesh，Ulysses 序列并行用二维 `(dp, sp)` mesh，二者满足 `world_size = dp × sp`。
- `_build_model_optimizer` 走「meta device 加载（rank0 真权重 + 其余空壳 + `sync_module_states` 广播）→ 混合精度 → wrap policy → FSDP 包装 → 仅 actor 建优化器」的流水线。
- actor 默认用 **fp32** 创建（保证 AdamW 状态精度），ref 用 **bf16**（无优化器、省显存）；前向计算时 actor 再由 `MixedPrecision` 降到 bf16。
- `get_fsdp_wrap_policy` 决定切分粒度；有 wrap policy 走 `FULL_SHARD`（ZeRO-3），无则走 `SHARD_GRAD_OP`（ZeRO-2）。
- `_build_rollout` 用独立的 `(dp, infer_tp)` mesh 搭建 vLLM/HF 推理引擎，与 actor 共享同一份 FSDP 权重。
- `param_offload` / `grad_offload` / `optimizer_offload` 三开关在「不用时搬 CPU、用时搬回 GPU」，配合 `enable_gradient_checkpointing`，构成 OOM 时的显存调优工具箱。

## 7. 下一步学习建议

- 阅读 [verl/workers/sharding_manager/fsdp_vllm.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/sharding_manager/fsdp_vllm.py)，理解 `_build_rollout` 返回的 `FSDPVLLMShardingManager` 如何在训练态（FSDP mesh）与推理态（`(dp, infer_tp)` mesh）间同步权重（对应 u6-l5）。
- 若想深入序列并行，对照 [verl/utils/ulysses.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/ulysses.py) 的 `SeqAllToAll` 与 attention 层的 `gather_seq_scatter_heads`，理解 SP 在 attention 内部的 AllToAll 数据重排。
- 下一讲 u7-l2 将转向「序列长度均衡与动态 batching」，讲解 driver 侧如何用 Karmarkar-Karp 算法让各 DP rank 的 token 数均衡，与 FSDP 的数据切分形成呼应。
