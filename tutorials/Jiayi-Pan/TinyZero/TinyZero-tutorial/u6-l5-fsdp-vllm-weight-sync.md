# FSDP↔vLLM 权重同步

## 1. 本讲目标

本讲解决一个关键工程问题：**veRL 的「混合引擎」里，训练态（FSDP）和推理态（vLLM）用的是同一份模型权重吗？如果不是，它们怎么保持一致？**

读完本讲你应该能够：

1. 说清为什么每一次 `generate_sequences` 都必须重新把 FSDP 的训练权重「同步」到 vLLM，以及生成完为什么又要「卸载」回去。
2. 区分 `FULL_STATE_DICT` 与 `SHARDED_STATE_DICT`，并解释配置项 `full_params` 如何在这两者间取舍。
3. 看懂 `preprocess_data` / `postprocess_data` 在 tensor-parallel（TP）组上做的 `allgather` / `broadcast` / `chunk`，理解它们为什么是「先扩张、再收缩」的一对操作。
4. 解释 TP 组内随机状态（random states）为什么必须对齐。

本讲是 u6-l4（vLLM Rollout 生成）的直接续篇：u6-l4 讲的是「vLLM 怎么生成一批回答」，本讲讲的是「vLLM 在生成之前/之后，它的权重和分布式状态是怎么和训练侧对齐的」。两者合起来，才是完整的 `generate_sequences`。

## 2. 前置知识

在进入源码前，先建立三个直觉。

### 2.1 FSDP 与 vLLM 是两套独立的权重副本

- **FSDP（Fully Sharded Data Parallel）** 是训练态：模型权重被切成碎片，分散在每个 GPU 上；前向/反向时按需 all-gather 拼出完整层，算完立刻丢掉。它跟着优化器一起被梯度更新。
- **vLLM** 是推理态：它是一个**独立的推理引擎**，内部维护着**自己的一份完整模型权重**（按 TP 切分），用来做高效的自回归生成（PagedAttention、CUDA graph、KV cache）。

关键点：**vLLM 不会去读 FSDP 的张量**。两者是两份物理上不共享的权重。因此 actor 每被 `update_actor` 更新一次（走了一个梯度步），FSDP 权重变了，但 vLLM 那份还停留在上一轮的旧值。必须有人负责把「新权重」搬运过去——这就是 `FSDPVLLMShardingManager` 的职责。

### 2.2 上下文管理器（context manager）协议

Python 的 `with obj: ...` 会在进入 `with` 块时调用 `obj.__enter__()`，离开时调用 `obj.__exit__(...)`。veRL 用这个协议把「同步权重 → 生成 → 卸载权重」包成一段对称的代码块。`BaseShardingManager` 定义了这个协议的空实现：

```python
class BaseShardingManager:
    def __enter__(self): ...
    def __exit__(self, exc_type, exc_value, traceback): ...
    def preprocess_data(self, data): return data
    def postprocess_data(self, data): return data
```

HF rollout 用这个「什么都不做」的空实现；vLLM rollout 用本讲的 `FSDPVLLMShardingManager` 覆盖它。

### 2.3 data parallel（DP）与 tensor parallel（TP）

- **DP**：不同 GPU 处理**不同的数据**，模型权重相同（或镜像）。数据按 batch 维（dim=0）切开。
- **TP**：不同 GPU 处理**同样的数据**，但各自只持有模型权重的**一部分**（按某个维度切）。因此同一个 TP 组内的所有 rank 必须**看到同一份输入 batch**。

混合引擎里，rollout 阶段用 TP（`tensor_model_parallel_size`，简称 `infer_tp` / `tp_size`），训练阶段用 DP。这两套并行方式对 batch 的要求相反，正是 `preprocess_data` / `postprocess_data` 要解决的问题。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [verl/workers/sharding_manager/fsdp_vllm.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/sharding_manager/fsdp_vllm.py) | **本讲主角**。`FSDPVLLMShardingManager` 实现 FSDP↔vLLM 权重同步与 TP 组数据搬运。 |
| [verl/workers/sharding_manager/base.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/sharding_manager/base.py) | `BaseShardingManager` 空实现，定义上下文管理器协议。 |
| [verl/workers/fsdp_workers.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py) | `_build_rollout` 里**构造** sharding manager，`generate_sequences` 里**使用**它。 |
| [verl/third_party/vllm/vllm_v_0_6_3/worker.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/third_party/vllm/vllm_v_0_6_3/worker.py) | vLLM 侧的 `sync_model_weights` / `offload_model_weights` 真正落地：把权重灌进/搬出 vLLM 模型。 |
| [verl/utils/torch_functional.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/torch_functional.py) | `allgather_dict_tensors` / `broadcast_dict_tensor` 两个分布式通信工具。 |
| [verl/workers/rollout/vllm_rollout/vllm_rollout.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/rollout/vllm_rollout/vllm_rollout.py) | vLLMRollout：构造推理引擎、`enforce_eager`/`free_cache_engine` 约束（u6-l4 已讲，本讲引用其显存约束）。 |

## 4. 核心概念与源码讲解

### 4.1 FSDPVLLMShardingManager：为什么每次生成都要重新同步权重

#### 4.1.1 概念说明

回顾 u6-l1 的混合引擎设计：actor 和 rollout **共享同一份 FSDP 模块** `actor_module_fsdp`，目的是省一份显存。但到了 rollout 阶段，真正去生成 token 的是 **vLLM 推理引擎**，而 vLLM 有自己的权重副本（见 2.1）。

这就带来一个时序问题。一个 PPO 训练步里，rollout 和 update 的先后是：

```
... → generate_sequences（用 vLLM 采样）→ ... → update_actor（梯度更新 FSDP）→ 下一步的 generate_sequences → ...
```

也就是说，**本轮生成结束后，actor 才被更新**；那么**下一轮生成开始时**，vLLM 里那份权重已经是「上一轮更新前」的旧值了，落后于 FSDP。如果不同步，vLLM 会一直用一个越来越过时的策略去采样，PPO 的 importance ratio 也会失真。

因此 `FSDPVLLMShardingManager` 的核心使命是：**每次进入 `generate_sequences` 之前，把最新的 FSDP 权重推送给 vLLM；生成结束之后，把 vLLM 权重卸载回 CPU，把显存还给训练侧。**

#### 4.1.2 核心流程

`FSDPVLLMShardingManager` 实现了上下文管理器协议，`__enter__` / `__exit__` 形成一对对称操作：

```
with self.rollout_sharding_manager:          # __enter__: 把 FSDP 权重同步进 vLLM
    prompts = ...preprocess_data(prompts)    # TP 组 allgather（见 4.3）
    output = self.rollout.generate_sequences(prompts=prompts)
    output = ...postprocess_data(output)     # TP 组 broadcast + chunk（见 4.3）
# __exit__: 把 vLLM 权重 offload 回 CPU，恢复 train 模式
```

`__enter__` 做三件事：① 取出 FSDP 的 state_dict；② 调 `sync_model_weights` 灌进 vLLM；③ 释放临时张量、切换到生成用随机状态。`__exit__` 做对应的三件事：① `offload_model_weights` 把 vLLM 权重搬走；② `self.module.train()` 把 FSDP 模型切回训练模式；③ 恢复训练用随机状态。

#### 4.1.3 源码精读

先看构造点——`fsdp_workers.py` 的 `_build_rollout` 如何决定用哪个 sharding manager：

[verl/workers/fsdp_workers.py:L250-L282](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py#L250-L282) —— rollout 选 `hf` 时配 `BaseShardingManager`（空操作）；选 `vllm` 时配 `FSDPVLLMShardingManager`。注意构造参数里 `full_params='hf' in self.config.rollout.load_format`（第 278 行），这行决定了 state_dict 类型，4.2 节细讲。

再看使用点——`generate_sequences` 里 sharding manager 包住了整段生成：

[verl/workers/fsdp_workers.py:L415-L423](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py#L415-L423) —— `with self.rollout_sharding_manager:` 之内才调用 `generate_sequences`，且 `preprocess_data` / `postprocess_data` 成对出现。

现在看 `__enter__` 本体：

[verl/workers/sharding_manager/fsdp_vllm.py:L69-L91](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/sharding_manager/fsdp_vllm.py#L69-L91) —— 关键两行：

```python
params = self.module.state_dict()                         # 取出 FSDP 权重（按 4.2 的类型）
self.inference_engine.sync_model_weights(params, load_format=load_format)  # 灌进 vLLM
```

注意注释明确写着 `# Copy, not share memory`——vLLM 拿到的是一份**拷贝**，这也是为什么同步需要发生、也解释了为什么两套权重天然会分叉。同步后立刻 `del params; torch.cuda.empty_cache()` 回收临时显存。

再看 `__exit__`：

[verl/workers/sharding_manager/fsdp_vllm.py:L93-L110](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/sharding_manager/fsdp_vllm.py#L93-L110) —— 调 `offload_model_weights()` 把 vLLM 权重挪走，再 `self.module.train()`（生成阶段 vLLM 推理不影响 FSDP 的 eval/train 状态，这里显式切回 train，为下一轮 `update_actor` 做准备），最后 `empty_cache`。

最后落到 vLLM 侧的真正实现：

[verl/third_party/vllm/vllm_v_0_6_3/worker.py:L274-L291](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/third_party/vllm/vllm_v_0_6_3/worker.py#L274-L291) —— `sync_model_weights` 按 `load_format` 分派到 `load_hf_weights`（整份权重）或 `load_dtensor_weights`（按 TP 分片）；`offload_model_weights` 则把 vLLM 模型每个参数的 `.data` 指向一块预分配的 **CPU** 张量（`torch.empty_like(params, device="cpu")`），从而让出 GPU 显存。

#### 4.1.4 代码实践

**实践目标**：用自己的话讲清「为什么每次 `generate_sequences` 都要重新进 sharding manager 同步权重」，并能在日志里定位同步发生的时刻。

**操作步骤**（源码阅读 + 日志观察）：

1. 打开 [verl/workers/fsdp_workers.py:L415-L423](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py#L415-L423)，确认 `with self.rollout_sharding_manager:` 包住了 `generate_sequences`。
2. 打开 [verl/workers/fsdp_workers.py:L355-L359](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py#L355-L359) 附近的 `update_actor`（它会把 FSDP 权重更新），确认调用顺序是「先 `generate_sequences`、后 `update_actor`」（参见 u4-l3 的 `fit()` 主循环）。
3. 在训练日志里搜索以下 `log_gpu_memory_usage` 打点（来自 `__enter__`/`__exit__`），按时间顺序排列：
   - `Before state_dict() in sharding manager memory`
   - `After sync model weights in sharding manager`
   - `After vllm offload in sharding manager`

**需要观察的现象**：`After state_dict()` 会比 `Before` 多出一份完整/分片权重的显存峰值；`After sync` 之后 vLLM 那侧权重被填上；`After vllm offload` 之后显存回落，把空间留给训练。

**预期结果**：你能画出一个 step 内「FSDP 权重 →（state_dict）→ 同步给 vLLM → vLLM 生成 → offload vLLM 权重 → FSDP 训练更新」的时序，并指出**下一次** `generate_sequences` 进 `__enter__` 时，同步的就是 `update_actor` **刚更新过**的新权重。

**关于运行结果**：完整训练需要多卡 GPU 与 vLLM 环境。如果你本地暂不具备，可只做日志/源码阅读部分；涉及显存数值处标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：假如把 `with self.rollout_sharding_manager:` 这一层去掉（直接调 `generate_sequences`），训练会出现什么问题？

**参考答案**：第一次生成还能用（vLLM 构造时虽然用 dummy 权重，但构造后会 offload；严格说连第一次都需要同步才能拿到真实权重）。更关键的是，`update_actor` 更新 FSDP 后，vLLM 仍持有旧权重，采样策略与当前 actor 不一致，PPO 的 importance ratio 失去意义，训练会发散或停滞。

**练习 2**：`__exit__` 里为什么要 `self.module.train()`？去掉会怎样？

**参考答案**：把 FSDP actor 显式切回训练模式（启用 dropout/梯度等训练行为），为紧随其后的 `update_actor` 反向传播做准备。若去掉，模型可能停留在非训练状态，影响梯度计算的正确性。

---

### 4.2 full_params：FULL 与 SHARDED state_dict 的取舍

#### 4.2.1 概念说明

FSDP 取 state_dict 有两种主流姿势，对应 PyTorch 的两种 `StateDictType`：

- **`SHARDED_STATE_DICT`**：每个 rank 只物化**自己那份分片**，返回的是 `DTensor`（分布式张量）。省显存、不用在单个 rank 上拼出整模型，是**多卡**下的首选。
- **`FULL_STATE_DICT`**：在（每个/指定）rank 上物化**完整的、未分片的**模型权重（普通 `torch.Tensor`）。简单直观，但要在单卡上放下整份模型，**单卡**或调试场景才合适。

`FSDPVLLMShardingManager.__init__` 用 `full_params` 这个布尔开关选其一，并且这个开关由 rollout 的 `load_format` 配置反推而来。顺带要理解：vLLM 构造时用的 `dummy_dtensor` / `dummy_hf` 这类 `dummy_*` 格式表示「**用随机值初始化，不去磁盘读权重**」——因为真实权重根本不从磁盘来，而是随后由 `sync_model_weights` 从 FSDP 推过来。

#### 4.2.2 核心流程

构造时的决策链：

```
load_format = config.rollout.load_format        # 默认 'dummy_dtensor'
full_params = 'hf' in load_format               # 'dummy_dtensor' → False；'dummy_hf' → True

if full_params:  → FULL_STATE_DICT   → 同步时 load_format='hf'  （load_hf_weights，整份权重）
else:            → SHARDED_STATE_DICT → 同步时 load_format='dtensor'（load_dtensor_weights，按 TP 分片）
```

`__enter__` 里那行 `load_format = 'hf' if self.full_params else 'dtensor'` 就是把上面的选择翻译给 vLLM。注意 `dummy_hf` 含子串 `'hf'`，所以会走 `full_params=True` 分支；`dummy_dtensor` 不含，走 `False` 分支。

单卡特例：`_build_rollout` 在 `world_size == 1` 时把 `load_format` 强制改成 `'dummy_hf'`（第 273–274 行），从而走 `FULL` 分支——因为单卡没有分片可言，直接给整份权重最简单。

#### 4.2.3 源码精读

构造函数里根据 `full_params` 设置 FSDP state_dict 类型：

[verl/workers/sharding_manager/fsdp_vllm.py:L47-L56](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/sharding_manager/fsdp_vllm.py#L47-L56) —— `full_params=True` 配 `FullStateDictConfig`，否则配 `ShardedStateDictConfig`。

`__enter__` 里把同一个布尔值映射成 vLLM 的 `load_format`：

[verl/workers/sharding_manager/fsdp_vllm.py:L74-L75](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/sharding_manager/fsdp_vllm.py#L74-L75) —— `load_format = 'hf' if self.full_params else 'dtensor'`，再传给 `sync_model_weights`。

`full_params` 的来源在 worker 构造处：

[verl/workers/fsdp_workers.py:L273-L279](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py#L273-L279) —— 单卡改成 `dummy_hf`，并用 `'hf' in self.config.rollout.load_format` 算出 `full_params`。

vLLM 配置侧定义了这些格式名，注释说明 `dummy` 是「用随机值初始化」：

[verl/third_party/vllm/vllm_v_0_6_3/config.py:L34-L41](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/third_party/vllm/vllm_v_0_6_3/config.py#L34-L41) —— `DUMMY_HF = "dummy_hf"`、`DUMMY_DTENSOR = "dummy_dtensor"` 等。

默认配置值在 yaml：

[verl/trainer/config/ppo_trainer.yaml:L74](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/config/ppo_trainer.yaml#L74) —— `load_format: dummy_dtensor`，即多卡默认走 SHARDED 分支。

#### 4.2.4 代码实践

**实践目标**：把 `full_params` 的两条路径在脑中跑通，并验证单卡会强制走 FULL。

**操作步骤**：

1. 在 [verl/workers/fsdp_workers.py:L273-L279](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py#L273-L279) 找到两处：单卡覆盖 `load_format='dummy_hf'` 的 `if`，以及 `full_params=...` 表达式。
2. 对 `load_format` 分别取 `dummy_dtensor` / `dummy_hf`，手算 `'hf' in load_format` 与 `full_params`，再推出 state_dict 类型与同步用的 `load_format`。
3. （选做）把 `N_GPUS=1` 跑起来（若本地有单卡），在 `__enter__` 的 `log_gpu_memory_usage('After state_dict() ...')` 处观察 `FULL` 模式下单卡峰值显存会接近「整份模型大小」。

**需要观察的现象 / 预期结果**：

| `config.rollout.load_format` | `full_params` | FSDP state_dict 类型 | 同步 load_format | vLLM 落地函数 |
| --- | --- | --- | --- | --- |
| `dummy_dtensor`（多卡默认） | `False` | `SHARDED_STATE_DICT` | `dtensor` | `load_dtensor_weights` |
| `dummy_hf`（或 `world_size==1`） | `True` | `FULL_STATE_DICT` | `hf` | `load_hf_weights` |

显存数值处若无法实跑，标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么不在多卡场景下也统一用 `FULL_STATE_DICT`？

**参考答案**：`FULL_STATE_DICT` 会在每个 rank 上拼出整份未分片模型，显存峰值 = 整个模型大小，多卡下容易 OOM；`SHARDED_STATE_DICT` 每个 rank 只持有自己的分片（DTensor），配合 `load_dtensor_weights` 直接灌进对应 TP 分片，既省显存又避免无意义的全收集。

**练习 2**：vLLM 构造时为什么用 `dummy_*` 而不是 `auto`？

**参考答案**：`auto` 会去磁盘读 HuggingFace 权重，但真实训练权重在 FSDP 里、不在磁盘上。用 `dummy_*` 让 vLLM 先用随机值占位建好模型骨架，真正的权重随后由 sharding manager 的 `sync_model_weights` 推过来，避免一次多余的磁盘加载。

---

### 4.3 preprocess_data / postprocess_data：TP 组上的 allgather 与 broadcast

#### 4.3.1 概念说明

这是本讲的「数据搬运」半边。回想 2.3：rollout 用 TP，要求**同一 TP 组内所有 rank 看到同一份 batch**；而训练用 DP，driver 把 `DataProto` 按 `world_size` **等分**下发，每个物理 rank 拿到的是**不同的**那 1/world_size。

`world_size = dp_size × tp_size`，所以 driver 下发后，同一个 TP 组内的 tp_size 个 rank，各自拿到的其实是**不同的**数据片。这违反 TP 的要求。`preprocess_data` 和 `postprocess_data` 就是用来在 TP 组内「拉平再还原」这对矛盾的一对操作。

#### 4.3.2 核心流程

```
                         driver 按 world_size 切分下发
   每个 rank 持有 1/(dp*tp) 的 batch
                         ↓ preprocess_data: allgather(dim=0, size=tp_size)
   每个 rank 持有 1/dp 的 batch（TP 组内各 rank 相同）  ← 满足 TP 要求
                         ↓ vLLM 生成
                         ↓ postprocess_data:
                           1) broadcast(TP src rank)   ← 保证 TP 组内结果一致
                           2) chunk(tp_size) 取本 rank 那块  ← 还原成 1/(dp*tp)
   每个 rank 持有 1/(dp*tp) 的 batch                   ← 回到 driver 期望的切分
```

- **preprocess（allgather）**：在 TP 组内，把 tp_size 个 rank 各自的那块沿 batch 维拼接，于是每个 rank 都拿到「本 DP 组的完整份额」。这一步把 batch 放大了 tp_size 倍，但保证了 TP 组内一致。
- **postprocess（broadcast + chunk）**：先把 src rank 的结果广播给整个 TP 组（vLLM 的采样结果可能只在某个 rank 上完整），再把放大的 batch 按 tp_size 切回，每个 rank 只保留属于自己的那 1/tp。这一步把 batch 缩回原大小，恢复 DP 的等分。

这两步严格对称：先扩张 tp_size 倍、再收缩 tp_size 倍。

#### 4.3.3 源码精读

`preprocess_data`：

[verl/workers/sharding_manager/fsdp_vllm.py:L112-L119](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/sharding_manager/fsdp_vllm.py#L112-L119) —— 对 `data.batch` 调 `allgather_dict_tensors`，`size=tp_size`、`group=TP 组`、`dim=0`（batch 维）。

`postprocess_data`：

[verl/workers/sharding_manager/fsdp_vllm.py:L121-L133](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/sharding_manager/fsdp_vllm.py#L121-L133) —— 先 `broadcast_dict_tensor`（src = TP 组的源 rank），再 `if tp_size > 1: data.chunk(chunks=tp_size)` 取 `dp_rank % tp_size` 那块。

底层通信工具 `allgather_dict_tensors`：逐个张量做 `torch.distributed.all_gather` 再 `torch.cat`：

[verl/utils/torch_functional.py:L169-L200](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/torch_functional.py#L169-L200) —— 注意它把 batch_size 放大了 `size` 倍（第 198 行），正是 preprocess 让 batch 变大的来源。

`broadcast_dict_tensor`：逐个张量做 `torch.distributed.broadcast`：

[verl/utils/torch_functional.py:L160-L166](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/torch_functional.py#L160-L166) —— 用于让 TP 组内所有 rank 对齐生成结果。

`chunk` 来自 `DataProto`（u3-l1 已讲），按 batch 维竖切。

> 说明：源码注释 `# TODO: Current impl doesn't consider FSDP with torch micro-dp` 表示当前实现没有处理「FSDP 内嵌 micro-DP」的情形，目前按全局 rank/world_size 计算。

#### 4.3.4 代码实践

**实践目标**：说清 `postprocess_data` 里按 `tp_size` chunk 的目的（本讲指定实践任务的后半问）。

**操作步骤**：

1. 读 [verl/workers/sharding_manager/fsdp_vllm.py:L112-L133](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/sharding_manager/fsdp_vllm.py#L112-L133)，对照 4.3.2 的流程图。
2. 用下面这段**示例代码**（非项目代码，仅用于演示 allgather/chunk 的对称性）在单进程里模拟一次「扩张—收缩」，体会 batch 维的变化。**待本地验证**实际数值。

```python
# 示例代码：仅模拟 preprocess(allgather) 与 postprocess(chunk) 的 batch 维变化
# 真实环境是分布式多卡，这里用 torch.cat / chunk 近似
import torch

tp_size = 2
# 假设 TP 组内每个 rank 各有 1 条样本（bs=1）
rank_shards = [torch.tensor([[100 + i]]) for i in range(tp_size)]   # rank0:[100], rank1:[101]

# preprocess: allgather(dim=0) —— 每个 rank 都拿到 2 条
gathered = torch.cat(rank_shards, dim=0)
print("after preprocess bs =", gathered.shape[0])   # 预期 2（tp_size 倍）

# postprocess: chunk(tp_size) 还原 —— 每个 rank 只取自己那 1/tp
chunks = torch.chunk(gathered, chunks=tp_size, dim=0)
my_rank = 0
mine = chunks[my_rank % tp_size]
print("after postprocess bs =", mine.shape[0])      # 预期 1（还原）
```

**需要观察的现象**：preprocess 之后 batch 维 `×tp_size`，postprocess 之后又缩回原值。

**预期结果**：你能回答——`postprocess_data` 里 `chunk(tp_size)` 的目的是**撤销 preprocess 的 allgather 带来的 batch 放大**，让每个物理 rank 只保留属于自己的 1/(dp·tp) 数据，从而让返回给 driver 的 `DataProto` 仍然是按 `world_size` 等分的，和训练侧 DP 的切分保持一致；否则 batch 会凭空变大 tp_size 倍，破坏后续 `compute_advantage` / `update_actor` 对 batch_size 的预期。

#### 4.3.5 小练习与答案

**练习 1**：`tp_size == 1` 时，preprocess/postprocess 实际做了什么？

**参考答案**：`allgather(size=1)` 等于不变；postprocess 里 `if tp_size > 1` 不进入 chunk 分支，broadcast 到大小为 1 的组也不变。即单 TP 下这俩函数基本是 no-op，符合「没有 TP 就不需要拉平/还原」。

**练习 2**：为什么 postprocess 先 broadcast 再 chunk，而不是反过来？

**参考答案**：vLLM 采样（如 top-k/top-p）的最终结果可能只在 TP 组的某个源 rank 上完整/权威，必须先 broadcast 让全组对齐一致；对齐之后再各自 chunk 取自己的份额才有意义。若先 chunk 再 broadcast，会把不一致的状态固化下来。

---

### 4.4 TP 组随机状态对齐

#### 4.4.1 概念说明

「随机状态」（RNG state）指 CUDA 的随机数发生器状态。生成阶段的采样（temperature>0 的 top-k/top-p）以及模型内部的随机操作（如 dropout）都依赖它。

TP 组的要求是：**同一 TP 组内所有 rank 行为应当一致**——它们合起来才是「一个模型」在处理「同一条输入」。如果组内各 rank 的 RNG 不一致，本该对齐的随机操作就会分叉。所以 veRL 在进入生成前，会把整个 TP 组的随机状态**对齐成同一个值**。

同时，又不能让生成阶段的随机性污染训练阶段的随机性。于是 sharding manager 维护了两套随机状态：一套给训练用、一套给生成用，进出 `with` 块时做「保存—交换—恢复」。

#### 4.4.2 核心流程

构造时准备「生成用随机状态」，关键是按 **DP rank**（而非全局 rank）播种：

```
torch_random_states = get_rng_state()        # 备份当前（训练）RNG
manual_seed(gen_dp_rank + 1000)              # 同一 DP 组内 TP 各 rank 的 gen_dp_rank 相同 → 同种子
gen_random_states = get_rng_state()          # 生成用 RNG
set_rng_state(torch_random_states)           # 立刻还原训练 RNG
```

为什么用 `gen_dp_rank`？因为同一 DP 组里的多个 TP rank 拿到**相同**的 `gen_dp_rank`，于是 `gen_random_states` 相同 → TP 组对齐；不同 DP 组的 `gen_dp_rank` 不同 → 各自探索不同样本（保证多样性）又可复现。

进出 `with` 块的双向交换：

```
__enter__:  torch_random_states = get_rng_state()   # 存训练 RNG
            set_rng_state(gen_random_states)        # 切到生成 RNG → TP 组一致
__exit__:   gen_random_states = get_rng_state()     # 存（已前进的）生成 RNG，供下次续用
            set_rng_state(torch_random_states)      # 恢复训练 RNG
```

这是一个标准的「双缓冲交换」：训练 RNG 与生成 RNG 互不干扰，且生成 RNG 跨 step 连续、可复现。

#### 4.4.3 源码精读

构造函数里准备 `gen_random_states`：

[verl/workers/sharding_manager/fsdp_vllm.py:L58-L67](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/sharding_manager/fsdp_vllm.py#L58-L67) —— 注释明说 `make sure all tp ranks have the same random states`；用 `device_mesh['dp'].get_local_rank()` 作为种子偏移。

`__enter__` 末尾切到生成 RNG：

[verl/workers/sharding_manager/fsdp_vllm.py:L88-L91](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/sharding_manager/fsdp_vllm.py#L88-L91) —— 保存训练 RNG、装入 `gen_random_states`。

`__exit__` 末尾切回训练 RNG：

[verl/workers/sharding_manager/fsdp_vllm.py:L107-L110](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/sharding_manager/fsdp_vllm.py#L107-L110) —— 把前进后的生成 RNG 存回 `gen_random_states`，再恢复 `torch_random_states`。

注意整套机制以 `device_mesh is not None` 为前提；无 device mesh（如未启用 TP mesh）时 `gen_random_states = None`，跳过交换。

#### 4.4.4 代码实践

**实践目标**：验证「同 DP 组内 TP rank 种子相同、不同 DP 组种子不同」。

**操作步骤**：

1. 读 [verl/workers/sharding_manager/fsdp_vllm.py:L58-L67](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/sharding_manager/fsdp_vllm.py#L58-L67)，确认种子是 `gen_dp_rank + 1000`。
2. 假设 `dp_size=2, tp_size=2`，列出 4 个 rank 的 `(global_rank, dp_rank, tp_rank)` 与对应种子（**示例推演，待本地验证**）：

| global rank | dp rank | tp rank | 种子 = dp_rank+1000 | 与组内 tp rank 是否相同 |
| --- | --- | --- | --- | --- |
| 0 | 0 | 0 | 1000 | 是（与 rank1 同） |
| 1 | 0 | 1 | 1000 | 是（与 rank0 同） |
| 2 | 1 | 0 | 1001 | 是（与 rank3 同） |
| 3 | 1 | 1 | 1001 | 是（与 rank2 同） |

**预期结果**：rank0/rank1（同一 TP 组）种子都是 1000；rank2/rank3 都是 1001；而两组之间不同。这正是「TP 组对齐、DP 组多样」的效果。

#### 4.4.5 小练习与答案

**练习 1**：如果用**全局 rank**（而非 dp_rank）作为种子偏移，会出什么问题？

**参考答案**：同一 TP 组内不同 TP rank 的全局 rank 不同，种子就不同，TP 组内 RNG 不再对齐，本该一致的随机操作会分叉，破坏 tensor 并行的正确性。

**练习 2**：为什么 `__enter__` 切到生成 RNG、`__exit__` 又切回训练 RNG？一直停在生成 RNG 不行吗？

**参考答案**：不行。后续 `update_actor` 是训练，它依赖训练 RNG 的连续流；若被生成 RNG 污染，训练侧的随机性（如 dropout）会被打乱且不可复现。双缓冲交换保证两条 RNG 流各自独立、各自连续。

## 5. 综合实践

把本讲四块内容串起来，完成下面这个**端到端追踪任务**（源码阅读型，无需 GPU）：

**任务**：画一张「一次 `generate_sequences` 期间，FSDPVLLMShardingManager 的完整时间线」，要求：

1. 横轴是时间，标注 `__enter__` → `preprocess_data` → vLLM 生成 → `postprocess_data` → `__exit__` 五个节点。
2. 在 `__enter__` 节点上标出三件事：①取 state_dict（FULL 还是 SHARDED？取决于你的 `load_format` 假设）；②`sync_model_weights`（用 `hf` 还是 `dtensor`？）；③切换到 `gen_random_states`。
3. 在 `preprocess`/`postprocess` 节点上标出 batch 维的变化（`×tp_size` 与还原），并指出通信原语（allgather / broadcast）。
4. 在 `__exit__` 节点上标出：①`offload_model_weights`（权重去 CPU）；②`self.module.train()`；③恢复训练 RNG。
5. 在图的最左侧加一个箭头注释：「上一轮 `update_actor` 刚改过 FSDP 权重 → 所以本次 `__enter__` 必须重新同步」。

**自检问题**（答得出说明本讲通了）：

- 为什么同步要用拷贝而非共享内存？（提示：见 4.1.3 的注释。）
- 多卡默认走 FULL 还是 SHARDED？为什么？（4.2）
- `postprocess_data` 的 `chunk(tp_size)` 删掉会怎样？（4.3.4）
- TP 组为什么必须共享 RNG？（4.4）

## 6. 本讲小结

- **FSDP 与 vLLM 是两份独立权重副本**，`FSDPVLLMShardingManager` 负责在每次 `generate_sequences` 前把最新训练权重同步进 vLLM（`__enter__`），生成后再卸载回 CPU（`__exit__`），因为上一轮 `update_actor` 已经改过 FSDP 权重。
- **`full_params` 开关**在 `FULL_STATE_DICT`（整份，单卡/`dummy_hf`）与 `SHARDED_STATE_DICT`（分片 DTensor，多卡默认 `dummy_dtensor`）之间取舍，并决定同步时用 `load_hf_weights` 还是 `load_dtensor_weights`。
- **`preprocess_data` / `postprocess_data`** 是一对对称操作：前者在 TP 组 allgather 把 batch 放大 tp_size 倍以满足 TP 对「组内同输入」的要求；后者先 broadcast 对齐结果，再 `chunk(tp_size)` 还原成 driver 期望的 world_size 等分。
- **TP 组随机状态对齐**：构造时按 DP rank 播种使同组 TP rank 共享 RNG，进出 `with` 块做双缓冲交换，保证生成可复现且不污染训练 RNG。
- sharding manager 通过 Python 上下文管理器协议把「同步—生成—卸载」封装成对称代码块，`BaseShardingManager` 是它的空实现基类。

## 7. 下一步学习建议

- 至此 u6 单元（Worker 与混合引擎实现）的 rollout 侧已闭环。接下来建议进入 **u7-l1（FSDP 模型构建与并行策略）**，看 `_build_model_optimizer` 如何用 meta tensor、FSDP wrap policy、device mesh 搭建训练态模型——它会补充本讲只「引用」的 FSDP 那一侧细节。
- 如果对另一条后端感兴趣，可读 **u7-l4（Megatron 后端与奖励模型 Worker）**，对照 `megatron_vllm.py` 里 Megatron↔vLLM 的 sharding manager，理解不同训练后端下权重同步的差异。
- 想动手实验的读者，可以尝试在 `__enter__`/`__exit__` 的 `log_gpu_memory_usage` 打点处收集一次真实训练的显存曲线，验证本讲对「同步峰值、offload 回落」的描述（需多卡 + vLLM 环境）。
