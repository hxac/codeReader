# NPUModelRunner v1 主链路

## 1. 本讲目标

本讲深入 `vllm_ascend/worker/model_runner_v1.py` 中的 `NPUModelRunner`，它是 vllm-ascend 在每张 NPU 卡上真正「跑模型」的对象。

学完后你应该能够：

- 说清 `NPUModelRunner` 继承上游 `GPUModelRunner` 后，重写了哪些关键方法、为什么这样重写。
- 描述一次推理的完整数据流：`_update_states`（更新状态）→ `_prepare_inputs`（准备输入）→ `_build_attention_metadata`（构建注意力元数据）→ `_model_forward`（前向）→ `_sample`（采样）。
- 理解 `execute_model` 与 `sample_tokens` 的拆分，以及采样与投机解码（speculative decoding）草稿生成如何衔接。
- 看懂 NPU 相对于 GPU 的几处关键适配：Ascend 采样器、`AscendCommonAttentionMetadata`、`AscendAttentionState` 状态机、ACL Graph 调度。
- 知道「稀疏 KV 卸载（Sparse KV Offload）」特性在 runner 里的几个挂载点（本讲只点出位置，数据面细节见 u10-l6）。

## 2. 前置知识

在阅读本讲前，建议你已经了解：

- **vLLM 的 ModelRunner 概念**：每个 worker 子进程持有一个 ModelRunner，负责把调度器（Scheduler）产出的 `SchedulerOutput` 转化为一次模型前向，并产出采样结果。本讲只讲 **v1 runner**（默认架构）；v2 runner 见 u4-l3。
- **NPUWorker（u4-l1）**：`NPUWorker` 负责设备初始化、HCCL 分布式、权重加载与生命周期编排，而「真正跑前向」这件事被它**委托**给 `NPUModelRunner`。
- **Patch 机制（u3）**：vllm-ascend 不改上游源码，而用 monkey-patch 把上游类替换为 Ascend 版本。`NPUModelRunner` 则是「**继承 + 重写**」的另一种集成手段。
- **注意力后端（u5）**：前向过程会构建 attention metadata，交给具体的注意力后端（FA / MLA / SFA / DSA 等）。本讲只讲 metadata 的「构建入口」，后端细节在 u5。
- **ACL Graph（u8）**：NPU 上对应 CUDA Graph 的图捕获/回放机制，本讲只涉及「runner 如何选择图模式」。

> 关键直觉：`GPUModelRunner` 是一套很成熟的「输入准备 → 前向 → 采样」框架。vllm-ascend 的策略是**尽量复用**这套框架，只在「NPU 和 GPU 行为不一样」的地方做最小改写。所以本讲你会看到大量「先调 `super().xxx()`，再做 NPU 特化处理」的模式。

## 3. 本讲源码地图

本讲主要涉及一个核心文件，并引用少量周边文件：

| 文件 | 作用 |
| --- | --- |
| `vllm_ascend/worker/model_runner_v1.py` | 本讲主角，约 4970 行，定义 `NPUModelRunner` 及其全部执行主链路 |
| `vllm_ascend/ascend_forward_context.py` | `set_ascend_forward_context`：每次前向往「前向上下文」注入 Ascend 专属运行期字段（MoE 通信方式、图模式等），u2-l3 已讲 |
| `vllm_ascend/attention/attention_v1.py` | `AscendAttentionState` 枚举（PrefillNoCache / DecodeOnly / …），驱动不同注意力路径 |
| `vllm_ascend/attention/utils.py` | `AscendCommonAttentionMetadata`：NPU 专属公共注意力元数据（携带 CPU 端 seq_lens 等） |
| `vllm_ascend/sample/sampler.py` | `AscendSampler`：在 NPU 上做 top-k/top-p 采样 |
| `vllm_ascend/worker/worker.py` | `NPUWorker.execute_model / sample_tokens`，展示 runner 如何被 worker 调用（承接 u4-l1） |
| `vllm_ascend/distributed/kv_transfer/sparse_kv_offload/sparse_kv_offload_manager.py` | 稀疏 KV 卸载管理器：runner 在初始化与每次构建元数据时调用它，详见 u10-l6 |

> 上游对照：`vllm_ascend/worker/model_runner_v1.py` 文件头部明确写着 `Adapted from vllm-project/vllm/vllm/worker/gpu_model_runner.py`，即它是从上游 GPU runner 改编而来。

## 4. 核心概念与源码讲解

本讲按 4 个最小模块组织：先认识 runner 的身份与初始化，再依次拆解「输入准备」「注意力元数据 + 图调度」「执行主链路」。

### 4.1 NPUModelRunner 的继承关系与初始化

#### 4.1.1 概念说明

`NPUModelRunner` 直接继承上游 `GPUModelRunner`：

```python
class NPUModelRunner(GPUModelRunner):
```

这是一个值得反复强调的设计：**vllm-ascend 没有从头写一个 runner**，而是把上游 GPU runner 拿过来继承。这样做的好处是上游 runner 里成百上千行「状态管理、调度对接、KV cache 规划、采样框架」逻辑可以直接复用，vllm-ascend 只需要重写「与 NPU 行为不一致」的方法。

那么「哪些行为不一致」呢？在初始化阶段就能看到几条主线：

1. **采样器不同**：用 `AscendSampler` 而非 GPU 采样器。
2. **注意力后端不同**：通过 `get_attn_backend` 选中 Ascend 的注意力后端。
3. **输入批（input batch）不同**：用 NPU 专属的 `NPUInputBatch`。
4. **图机制不同**：用 ACL Graph（`_use_aclgraph`）替代 CUDA Graph。
5. **NPU 不认识 CUDA API**：上游父类初始化里会用到 `torch.cuda.*`，需要先用 `_torch_cuda_wrapper()` 临时把 `torch.cuda.*` 重定向到 `torch.npu.*`。
6. **稀疏 KV 卸载（Sparse KV Offload）**：若开启，runner 会持有 `sparse_kv_offload_manager` 与若干 per-request 元数据缓冲，为把稀疏注意力的主 KV 卸载到主机做准备（详见 u10-l6）。

#### 4.1.2 核心流程

`__init__` 的执行流程（简化版伪代码）：

```text
NPUModelRunner.__init__:
    1. 提前设置 self.use_compress   # 必须在 super().__init__() 之前，因为父类初始化会访问它
    2. with _torch_cuda_wrapper():  # 把 torch.cuda.* 临时换成 torch.npu.*
           super().__init__()       # 复用上游 GPU runner 的全部初始化
    3. set_offloader(...)           # 装上 Ascend 的 offloader
    4. 分配 query_start_loc / group_len 等 NPU 专属缓冲区（比上游多 +2 填充）
    5. self.sampler = AscendSampler()       # 换 NPU 采样器
    6. self.attn_backend = get_attn_backend(...)  # 选 Ascend 注意力后端
    7. self._set_up_drafter()        # 如果开了投机解码，初始化草稿器
    8. 计算 self.use_aclgraph        # 是否启用 ACL Graph
    9. （可选）初始化 EPLB 进程      # 专家负载均衡
    10. self.input_batch = NPUInputBatch(...)  # 换 NPU 输入批
    11. 读 sparse_kv_offload_config，置 self.sparse_kv_offload_manager = None
        （开启时额外分配 per-request 元数据缓冲；manager 在 initialize_kv_cache 阶段才真正创建）
```

「第 1 步必须在 super().__init__() 之前」是一个容易被忽视的细节，源码里有明确注释说明原因。第 11 步是本次 #13026（稀疏 KV 卸载）新增的初始化挂载点，它只做准备：真正的管理器对象要等到 KV cache 配置就绪后（`initialize_kv_cache`）才创建。

#### 4.1.3 源码精读

**类定义与初始化前置（第 287–298 行）**：注意 `self.use_compress` 必须先于父类初始化设置，因为父类初始化过程中可能调用 `_allocate_kv_cache_tensors`，而后者会访问 `self.use_compress`：

[vllm_ascend/worker/model_runner_v1.py:287-298](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/worker/model_runner_v1.py#L287-L298) — 定义类，并在调用父类初始化前先算出 `self.use_compress`（与 DeepSeek-V4 压缩注意力相关）。

**CUDA→NPU 的兼容包装**：父类 `GPUModelRunner` 的初始化代码里直接用了 `torch.cuda.Event`、`torch.cuda.Stream` 等 CUDA API。为了让同样的代码在 NPU 上跑通，runner 用一个上下文管理器把这些符号临时重定向到 `torch.npu.*`：

[vllm_ascend/worker/model_runner_v1.py:4893-4937](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/worker/model_runner_v1.py#L4893-L4937) — `_torch_cuda_wrapper()` 把 `torch.cuda.Event / Stream / synchronize` 等替换成 NPU 对应实现，让上游 CUDA 代码在 NPU 上透明运行。

**换 NPU 采样器与注意力后端**：

[vllm_ascend/worker/model_runner_v1.py:337](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/worker/model_runner_v1.py#L337) — `self.sampler = AscendSampler()`，把采样换成 NPU 实现。

[vllm_ascend/worker/model_runner_v1.py:385-393](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/worker/model_runner_v1.py#L385-L393) — 通过 `get_attn_backend(...)`，用 `use_mla / use_sparse / use_mm_prefix` 等标记选中 Ascend 注意力后端。

**ACL Graph 开关**：`_use_aclgraph` 决定是否启用 ACL Graph，条件是「图模式非 NONE」且「编译模式为 VLLM_COMPILE」且「非 eager」：

[vllm_ascend/worker/model_runner_v1.py:661-666](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/worker/model_runner_v1.py#L661-L666) — `_use_aclgraph()` 返回是否启用 ACL Graph。

[vllm_ascend/worker/model_runner_v1.py:513-538](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/worker/model_runner_v1.py#L513-L538) — 构造 `NPUInputBatch`，这是 NPU 专属的输入批对象，承载每条请求的 token、block table、采样元数据等。

**稀疏 KV 卸载的初始化挂载点（#13026 新增）**：runner 在 `__init__` 末尾读取配置、占位管理器，并按需分配 per-request 元数据缓冲。注意此时 manager 仍是 `None`，真正的对象在 `initialize_kv_cache` 阶段才创建：

[vllm_ascend/worker/model_runner_v1.py:569-579](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/worker/model_runner_v1.py#L569-L579) — 读取 `sparse_kv_offload_config`，置 `self.sparse_kv_offload_manager = None`，记录 `self.tp_rank`；开启时分配 `_offload_req_ids_tensor`（每条请求一个 id）与 `_offload_token_to_req`（每个 token 映射到所属请求）两个缓冲，供后续 resident LRU 使用。

> 补充：稀疏 KV 卸载还改写了 KV cache 的「生命周期」方法——`profile_run` 调 `allocate_kv_offload_topk_profile_buffers`、`initialize_kv_cache` 调 `init_sparse_kv_offload_manager` 并 `register_kv_caches`、`_allocate_kv_cache_tensors` / `_reshape_kv_cache_tensors` 各有一条稀疏卸载专用分支（断言必须 `use_sparse`、不能是 sparse SFA C8、不支持 HMA），`get_kv_cache_spec` 给 indexer cache spec 传 `store_on_host=True`。这些都属于「初始化/建图阶段」的挂载点，数据面细节在 u10-l6。

#### 4.1.4 代码实践

**实践目标**：理解「为什么 `self.use_compress` 必须在父类初始化前设置」。

**操作步骤**：

1. 打开 `vllm_ascend/worker/model_runner_v1.py`，定位到第 288–298 行。
2. 阅读注释 `Must be set before super().__init__() because parent init may call _allocate_kv_cache_tensors which accesses self.use_compress.`。
3. 用编辑器搜索 `self.use_compress`，观察它在文件里被哪些方法读取（例如 `_allocate_kv_cache_tensors`、`_build_attention_metadata`）。

**需要观察的现象**：你会发现 `use_compress` 在多个下游方法里参与 KV cache 分配与注意力元数据的分支判断；如果它没有被正确提前赋值，父类初始化阶段就会读到未定义属性而报错。

**预期结果**：能用自己的话说明「上游父类初始化会触发某些依赖 `use_compress` 的初始化路径，因此子类必须在调用 `super().__init__()` 前先把这个属性算出来」。这是一个典型的「继承重写时的初始化顺序陷阱」。

#### 4.1.5 小练习与答案

**练习 1**：`NPUModelRunner` 为什么选择「继承 `GPUModelRunner`」而不是「monkey-patch 上游 runner」？

> 参考答案：因为 runner 的逻辑非常庞大且需要大量复用（状态管理、调度对接、采样框架等），继承能直接复用父类上千行正确代码，只在必要处重写；而 monkey-patch 更适合「替换上游某个孤立函数/类」的场景（如 u3 的各种 patch）。对 runner 这种「整体框架 + 局部特化」的对象，继承更自然。

**练习 2**：`_torch_cuda_wrapper()` 解决了什么问题？如果不用它会怎样？

> 参考答案：上游 `GPUModelRunner.__init__` 直接调用 `torch.cuda.Event`、`torch.cuda.Stream` 等 CUDA 专有 API，在 NPU 环境里这些符号会出错或行为不符。`_torch_cuda_wrapper()` 在父类初始化期间把这些符号临时重定向到 `torch.npu.*`，让 CUDA 代码在 NPU 上透明运行；不用它会导致父类初始化在第一处 CUDA 调用就抛异常。

**练习 3**：为什么 `self.sparse_kv_offload_manager` 在 `__init__` 里被置为 `None`，而不是直接创建？

> 参考答案：因为管理器的创建依赖 KV cache 的形状与配置（block 大小、层数、KV 头维度等），而这些信息在 `__init__` 阶段尚未就绪，要等到 `initialize_kv_cache(kv_cache_config)` 被调用、拿到 `KVCacheConfig` 之后才能正确初始化。所以 `__init__` 只占位 `None` 并分配好不依赖 KV 形状的 per-request 元数据缓冲，把真正的创建推迟到 `initialize_kv_cache`。

---

### 4.2 状态更新与输入准备

#### 4.2.1 概念说明

每次前向的第一步，是把调度器送来的 `SchedulerOutput`「翻译」成模型能吃的张量。这一步分两个动作：

- **`_update_states`**：把调度器对批的增删改（新增请求、删除请求、更新已计算 token 数等）应用到 runner 持久维护的 `input_batch` 上。
- **`_prepare_inputs`**：从更新后的 `input_batch` 提取出本次前向真正需要的输入张量（`input_ids`、`positions`、`query_start_loc`、`seq_lens`、block table 的 slot mapping 等），并算出本次是哪种「注意力状态」。

NPU 在这里的特化主要是两点：

1. **`AscendAttentionState` 状态机**：NPU 注意力后端需要知道「这一批是纯 prefill、纯 decode、还是 chunked prefill、还是投机解码」，因为不同状态会走不同的算子路径。`_build_attn_state` 负责判定。
2. **CPU/NPU 协作的输入准备**：为了尽量减少「GPU→CPU 同步」（这在 NPU 上代价高），runner 维护了一批 pinned CPU 缓冲区，先在 CPU 上算好索引，再异步拷到 NPU。

#### 4.2.2 核心流程

`_update_states` → `_prepare_inputs` 的简化流程：

```text
_update_states(scheduler_output):
    （异步调度的回退保护）
    self._apply_pp_sampled_tokens_from_scheduler_output(scheduler_output)
    return super()._update_states(scheduler_output)   # 复用上游状态更新

_prepare_inputs(scheduler_output, num_scheduled_tokens):
    1. 先把 block table 提交拷贝（与后续 CPU 操作重叠，优化点）
    2. 算 attn_state = self._build_attn_state(...)      # 判定 PrefillNoCache/DecodeOnly/...
    3. 算 positions（在 CPU pinned buffer 上做 cumsum + add）
    4. 用 token_indices 从 token_ids_cpu 里 index_select 出 input_ids
    5. 填充 query_start_loc、optimistic_seq_lens_cpu
    6. 把 input_ids / positions / seq_lens 异步拷到 NPU
    7. 算 block table 的 slot_mapping
    8. 若有投机解码：算 spec_decode_metadata（logits_indices 等）
    return (logits_indices, spec_decode_metadata, total_num_scheduled_tokens)
```

`AscendAttentionState` 有五种取值，对应不同场景：

```text
PrefillNoCache   # 所有请求都还没算过任何 token（首次 prefill）
PrefillCacheHit  # prefill 但部分 token 已命中 cache
DecodeOnly       # 所有请求每条只算 1 个新 token（纯 decode）
ChunkedPrefill   # 分块 prefill（开 chunked_prefill）
SpecDecoding     # 投机解码验证阶段
```

#### 4.2.3 源码精读

**`_update_states`**：先做一些 NPU 特有的回退保护，再调用上游父类的状态更新：

[vllm_ascend/worker/model_runner_v1.py:782-798](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/worker/model_runner_v1.py#L782-L798) — `_update_states` 在异步调度下做 KV-load-failure 回退保护，再委托 `super()._update_states()` 完成实际状态更新。

**`_build_attn_state` 状态机**：这是 NPU 特有的判定逻辑，根据「已计算 token 数」和「每条请求调度 token 数」推断当前批处于哪种注意力状态：

[vllm_ascend/worker/model_runner_v1.py:1286-1313](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/worker/model_runner_v1.py#L1286-L1313) — `_build_attn_state` 返回 `AscendAttentionState`，并存到 `self.attn_state` 供注意力元数据构建使用。

对应的枚举定义在注意力后端文件：

[vllm_ascend/attention/attention_v1.py:142-147](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/attention/attention_v1.py#L142-L147) — `AscendAttentionState` 五种状态定义。

**`_prepare_inputs` 主干**：函数很长，下面摘取几个关键段落。

[vllm_ascend/worker/model_runner_v1.py:886-892](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/worker/model_runner_v1.py#L886-L892) — 调 `_build_attn_state` 判定注意力状态，并据此判断本批是否含 prefill（`with_prefill`）。

[vllm_ascend/worker/model_runner_v1.py:894-903](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/worker/model_runner_v1.py#L894-L903) — 在 CPU pinned buffer 上算 `positions`（`cumsum + 已计算 token 数`），避免直接在 NPU 上算。

[vllm_ascend/worker/model_runner_v1.py:953-958](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/worker/model_runner_v1.py#L953-L958) — 用 `token_indices` 从 `token_ids_cpu_tensor` 中 `index_select` 出本次前向的 `input_ids`（注释说明用 `torch.index_select` 而非 `np.take` 是因为大张量上更快）。

[vllm_ascend/worker/model_runner_v1.py:1193-1196](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/worker/model_runner_v1.py#L1193-L1196) — 计算 `seq_lens = num_computed_tokens + num_scheduled_tokens`，这是注意力的关键输入。

[vllm_ascend/worker/model_runner_v1.py:1259-1269](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/worker/model_runner_v1.py#L1259-L1269) — 投机解码分支：若本批含 spec decode token，则计算 `spec_decode_metadata`（含 `logits_indices`、`bonus_logits_indices` 等）。

#### 4.2.4 代码实践

**实践目标**：理解 `_build_attn_state` 的判定规则，并预测不同输入下的状态。

**操作步骤**：

1. 打开 `vllm_ascend/worker/model_runner_v1.py` 第 1286–1313 行，阅读 `_build_attn_state`。
2. 准备一张纸，针对下面三种假设输入，写出 `attn_state` 的值：
   - **场景 A**：3 条请求，`num_computed_tokens = [0, 0, 0]`，`num_scheduled_tokens = [128, 128, 128]`。
   - **场景 B**：3 条请求，`num_computed_tokens = [100, 200, 300]`，`num_scheduled_tokens = [1, 1, 1]`，未开投机解码。
   - **场景 C**：3 条请求，`num_scheduled_tokens = [1, 1, 1]`，开了 `method="mtp"` 的投机解码。

**需要观察的现象**：注意每个 `if/elif` 分支的判断顺序——先判 `num_computed_tokens == 0`，再判 `num_scheduled_tokens == 1`，再判 `num_valid_tokens == 1`，再判 `enable_chunked_prefill`。

**预期结果**：

- 场景 A → `PrefillNoCache`（所有请求还没算过 token）。
- 场景 B → `DecodeOnly`（每条只算 1 个 token，且非首次 prefill）。
- 场景 C → `SpecDecoding`（`num_scheduled_tokens == 1` 且开了 mtp，命中第 1292–1295 行的特例，会从 `DecodeOnly` 改判为 `SpecDecoding`）。

> 这是纯源码阅读型实践，无需 NPU；如果你愿意，可在第 1308–1311 行附近看到「非 mtp 的 SpecDecoding 会被改判为 ChunkedPrefill」这一有趣细节（待本地验证其触发条件）。

#### 4.2.5 小练习与答案

**练习 1**：`_prepare_inputs` 为什么先 `commit_block_table` 再做后续 CPU 操作？

> 参考答案：源码注释（第 870–872 行）说明这是为了「让 block table 的拷贝与后续 CPU 计算重叠」。block table 拷贝是 H2D（主机到设备）操作，耗时长，先发起它，后续 CPU 索引计算可以与之并行，隐藏拷贝延迟。

**练习 2**：`positions` 为什么在 CPU pinned buffer 上算，而不是直接在 NPU 上算？

> 参考答案：positions 的计算是「cumsum + 已计算 token 数」这种轻量但依赖 CPU 端调度信息的运算；在 pinned CPU buffer 上算好后一次性异步拷到 NPU，比「在 NPU 上算 + 频繁 GPU↔CPU 同步」更高效，也符合 vllm-ascend「尽量减少 GPU→CPU 同步」的设计取向。

---

### 4.3 注意力元数据构建与 ACL Graph 调度

#### 4.3.1 概念说明

输入准备好后，下一步是构建**注意力元数据（attention metadata）**。注意力后端需要知道：这一批有多少请求、每个请求的 query/seq 长度、KV cache 的 block table 与 slot mapping、是 prefill 还是 decode 等。这些信息被打包成一个元数据对象，传给注意力算子。

NPU 的关键特化是使用 **`AscendCommonAttentionMetadata`**（而非上游 `CommonAttentionMetadata`）。它多带了一些字段，最关键的是 **CPU 端的 seq_lens**——目的是让 NPU 注意力后端能直接从 CPU 读 seq_lens，避免一次 GPU→CPU 同步。开启稀疏 KV 卸载后，它还会多带 `req_ids_tensor` / `token_to_req` 两个映射，供常驻 LRU 把每个 token 关联到所属请求（详见 u10-l6）。

另一个重要概念是 **图模式调度**。runner 通过 `_determine_batch_execution_and_padding` 决定本批用哪种图模式：

- `CUDAGraphMode.NONE`：eager 模式，不回放图。
- `CUDAGraphMode.PIECEWISE`：分段图（把模型切成若干段分别捕获/回放）。
- `CUDAGraphMode.FULL`：整图模式（整个前向一个图，需要 padding 到固定 batch）。

不同模式对 `num_tokens` 和 `num_reqs` 的 padding 要求不同，`_pad_query_start_loc_for_fia` 就是专门为 FULL 模式补 padding 的。

#### 4.3.2 核心流程

`_build_attention_metadata` 的核心流程：

```text
_build_attention_metadata(num_tokens, num_reqs, num_tokens_padded, num_reqs_padded, ...):
    1. 取 max_seq_len（捕获时用 max_model_len，运行时用 optimistic_seq_lens 的最大值）
    2. （开启稀疏 KV 卸载时）update_sparse_kv_offload_metadata(...)  # 维护 per-request/per-token 映射
    3. 构造 AscendCommonAttentionMetadata（cm_base）：query_start_loc / seq_lens /
       block_table_tensor / slot_mapping / attn_state / positions …（开启卸载时额外带 req_ids_tensor / token_to_req）
    4. 对每个 kv_cache_group：
         a. 浅拷贝 cm_base，按 group 调整 block_table / slot_mapping / encoder_seq_lens
         b. 取该 group 的 metadata builder
         c. builder.build(common_attn_metadata=cm) → 得到 per-layer attention metadata
         d. 把同一 group 内所有 layer 名都指向这个 metadata
    5. 若开了投机解码：额外产出 spec_decode_common_attn_metadata 供草稿器用
    return (attn_metadata, spec_decode_common_attn_metadata)
```

`_determine_batch_execution_and_padding` 的职责：

```text
_determine_batch_execution_and_padding(num_tokens, num_reqs, ...):
    1. num_tokens_padded = _pad_for_sequence_parallelism(num_tokens)  # SP 需对齐 TP
    2. 判 uniform_decode（是否所有请求 query 长度一致）
    3. dispatch_cudagraph → 通过 cudagraph_dispatcher 决定 NONE/PIECEWISE/FULL 与 BatchDescriptor
    4. 若 DP > 1：跨 DP rank all_reduce 同步 num_tokens 与 cudagraph_mode
    5. return (cudagraph_mode, batch_descriptor, should_ubatch, num_tokens_across_dp, cudagraph_stats)
```

#### 4.3.3 源码精读

**稀疏 KV 卸载的元数据更新（#13026 新增）**：在构造公共元数据之前，先调用管理器更新 per-request / per-token 映射，使后续 resident 注意力能定位每个 token 属于哪条请求：

[vllm_ascend/worker/model_runner_v1.py:2820-2830](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/worker/model_runner_v1.py#L2820-L2830) — 开启 `sparse_kv_offload_enabled` 时调 `update_sparse_kv_offload_metadata(...)`，把本次 batch 的请求 id、query_start_loc 写入 `_offload_req_ids_tensor` 与 `_offload_token_to_req` 两个缓冲。

**构造 Ascend 公共注意力元数据**：注意 `_seq_lens_cpu` 字段，它把 optimistic seq_lens 透传给 NPU 后端，让后端无需 GPU→CPU 同步；开启稀疏卸载时还会带上 `req_ids_tensor` / `token_to_req`：

[vllm_ascend/worker/model_runner_v1.py:2917-2960](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/worker/model_runner_v1.py#L2917-L2960) — 构造 `AscendCommonAttentionMetadata`（`cm_base`），含 `query_start_loc`、`seq_lens`、`block_table_tensor`、`slot_mapping`、`attn_state`、`positions`、`decode_token_per_req` 等。

[vllm_ascend/worker/model_runner_v1.py:2950-2959](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/worker/model_runner_v1.py#L2950-L2959) — `req_ids_tensor` 与 `token_to_req` 两个字段：开启稀疏卸载时取自 `_offload_req_ids_tensor` / `_offload_token_to_req`，否则为 `None`。

**按 group 构建 per-layer 元数据**：

[vllm_ascend/worker/model_runner_v1.py:2966-3034](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/worker/model_runner_v1.py#L2966-L3034) — `_build_attn_group_metadata`：为每个 attention group 取 builder，调用 `builder.build(...)`（或捕获专用 `build_for_cudagraph_capture`），再把结果挂到 group 内所有 layer 名上。

**FULL 模式的 query_start_loc 补 padding**：当图模式为 FULL（或开了 SP）时，需要把 `query_start_loc` 补齐到与 padding 后的 token 数一致（TND 布局要求 hidden_states 第一维等于 `actual_seq_lengths_q` 最后一个元素）：

[vllm_ascend/worker/model_runner_v1.py:800-847](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/worker/model_runner_v1.py#L800-L847) — `_pad_query_start_loc_for_fia` 处理 FULL 模式与混合 batch 的 padding 逻辑。

**图模式调度**：

[vllm_ascend/worker/model_runner_v1.py:2698-2795](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/worker/model_runner_v1.py#L2698-L2795) — `_determine_batch_execution_and_padding`：决定图模式、batch 描述符、是否 ubatch、跨 DP 同步 num_tokens。

[vllm_ascend/worker/model_runner_v1.py:2736-2747](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/worker/model_runner_v1.py#L2736-L2747) — `dispatch_cudagraph` 内部函数，通过 `cudagraph_dispatcher.dispatch(...)` 决定本批用哪种图模式。

#### 4.3.4 代码实践

**实践目标**：理解 `AscendCommonAttentionMetadata` 相比上游多了哪些字段、为什么。

**操作步骤**：

1. 在终端执行（只读查看）：

   ```bash
   grep -n "class AscendCommonAttentionMetadata" -A 60 vllm_ascend/attention/utils.py
   ```

2. 阅读该 dataclass 的字段，重点关注以 `_cpu` 结尾或与 seq_lens 相关的字段（如 `_seq_lens_cpu`、`seq_lens_cpu_upper_bound`、`num_computed_tokens_cpu`），以及稀疏卸载相关的 `req_ids_tensor` / `token_to_req`。

**需要观察的现象**：你会看到它同时携带了 GPU 张量（如 `seq_lens`、`block_table_tensor`）和对应的 CPU 版本（`_seq_lens_cpu`、`num_computed_tokens_cpu`）。

**预期结果**：能说明「NPU 注意力后端可以直接从 CPU 字段读取 seq_lens 等信息，从而避免一次 device→host 同步」——这是 NPU 上降低同步开销的关键设计。如果你没法运行环境，可标注为「待本地验证」其性能收益。

#### 4.3.5 小练习与答案

**练习 1**：为什么 FULL 图模式需要 `_pad_query_start_loc_for_fia`，而 eager 模式不需要？

> 参考答案：FULL 模式把整个前向捕获成一个固定形状的图并回放，要求 `hidden_states` 第一维（TND 布局的 T）严格等于 `actual_seq_lengths_q` 的最后一个元素。因此当 batch 被 padding 时，必须同步把 `query_start_loc` 补上对应的虚拟请求，使两者匹配。eager 模式不回放固定图，形状可动态变化，故无需此 padding。

**练习 2**：`_determine_batch_execution_and_padding` 中 `uniform_decode` 这个标志的作用是什么？

> 参考答案：`uniform_decode` 表示「本批所有请求的 query 长度完全一致」（典型场景是纯 decode 或投机 decode）。这个标志会传给 `cudagraph_dispatcher`，让它能识别并选用更优化的图例程（例如专门的 decode 图）。只有 uniform batch 才适合用 FULL 模式整图回放。

**练习 3**：稀疏 KV 卸载往 `AscendCommonAttentionMetadata` 里加了哪两个字段？它们在未开启卸载时是什么值？

> 参考答案：加了 `req_ids_tensor`（每条请求一个 id）和 `token_to_req`（每个 token 映射到所属请求）。它们用于常驻 LRU 在 decode 期把 top-k 命中的 token 关联到正确的请求。未开启卸载时这两个字段为 `None`，即对普通路径完全透明、不影响既有注意力后端。

---

### 4.4 execute_model / sample_tokens 拆分：前向、采样与投机解码衔接

#### 4.4.1 概念说明

现在进入本讲最核心的部分：**一次完整前向是怎么被驱动起来的**。

vLLM v1 的一个重要设计是 **异步调度（async scheduling）**：把「前向」和「采样」拆成两个方法，使上一批的采样可以与下一批的前向重叠执行。vllm-ascend 完全遵循这个设计：

- **`execute_model`**：负责「更新状态 → 准备输入 → 构建 attention metadata → 前向 → 算 logits」，然后**返回 `None`**，并把中间结果暂存到 `self.execute_model_state`。
- **`sample_tokens`**：从 `self.execute_model_state` 取出 logits，完成采样、记账、（若开了投机解码）生成草稿 token，最终产出 `ModelRunnerOutput`。

这两个方法由 `NPUWorker` 依次调用（见 4.4.3 的 worker 代码）。

投机解码的衔接点也在这里：`sample_tokens` 在采样完成后，会调用 `propose_draft_token_ids` 让草稿器（eagle/ngram/mtp 等）生成下一批的 draft token，供下一轮 target 模型验证。

#### 4.4.2 核心流程

`execute_model` 的完整数据流：

```text
execute_model(scheduler_output, intermediate_tensors):
    1. （预处理：ngram_gpu copy、async deepcopy、KV connector 抢占处理）
    2. with record_function("prepare input"):
         a. deferred = self._update_states(scheduler_output)        # 更新状态
         b. (logits_indices, spec_decode_metadata, total) =
                self._prepare_inputs(scheduler_output, num_scheduled_tokens_np)  # 准备输入
         c. (cudagraph_mode, batch_desc, ..., num_tokens_across_dp, ...) =
                self._determine_batch_execution_and_padding(...)    # 图模式调度
         d. 若 FULL/SP：_pad_query_start_loc_for_fia(...)
         e. (attn_metadata, spec_decode_common_attn_metadata) =
                self._build_attention_metadata(...)                 # 构建注意力元数据
         f. (input_ids, ..., model_kwargs, ec_connector_output) = self._preprocess(...)
         g. update_cos_sin(positions)
    3. （EPLB forward_before、KV scales 处理）
    4. with set_ascend_forward_context(attn_metadata, ...):         # 注入运行期上下文
            hidden_states = self._model_forward(...)                # 真正前向
    5. 后处理：取 sample_hidden_states = hidden_states[logits_indices]
              logits = self.model.compute_logits(sample_hidden_states)
    6. self.execute_model_state = ExecuteModelState(...)            # 暂存中间结果
    7. return None                                                   # 等待 sample_tokens

sample_tokens(grammar_output):
    1. 从 self.execute_model_state 解包出 logits / attn_metadata / ...
    2. 若有 grammar：apply_grammar_bitmask(...)
    3. sampler_output = self._sample(logits, spec_decode_metadata)  # 采样
    4. 若开了投机解码：
         propose_draft_token_ids(sampled)  # 让草稿器生成 draft token
         （eagle 路径用 GPU token；ngram 路径用 CPU token）
    5. _bookkeeping_sync(...)              # 记账：把采样 token 写回 input_batch
    6. 构造 ModelRunnerOutput 并返回
```

**采样（`_sample`）**：若 `spec_decode_metadata is None`，直接用 `AscendSampler` 采样；否则走 `AscendRejectionSampler` 做拒绝采样（投机解码验证）。

**前向（`_model_forward`）**：真正调用 `self.model(...)`。此外它会在 FULL 图模式下调用 `update_full_graph_params` 更新图工作区参数，并在开了 flash_comm1 序列并行时做 hidden states 的 all-gather。

#### 4.4.3 源码精读

**worker 如何调用 runner**（承接 u4-l1）：`NPUWorker.execute_model` 把请求转给 `model_runner.execute_model`，`NPUWorker.sample_tokens` 转给 `model_runner.sample_tokens`：

[vllm_ascend/worker/worker.py:651](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/worker/worker.py#L651) — `output = self.model_runner.execute_model(scheduler_output, intermediate_tensors)`。

[vllm_ascend/worker/worker.py:688-689](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/worker/worker.py#L688-L689) — `NPUWorker.sample_tokens` 委托 `self.model_runner.sample_tokens(grammar_output)`。

**`execute_model` 主干**：

[vllm_ascend/worker/model_runner_v1.py:1795-1872](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/worker/model_runner_v1.py#L1795-L1872) — 在 `record_function("prepare input")` 与 `synchronize_input_prep()` 内，依次执行 `_update_states` → `_prepare_inputs`，这是「更新状态 → 准备输入」的衔接处。

[vllm_ascend/worker/model_runner_v1.py:1885-1899](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/worker/model_runner_v1.py#L1885-L1899) — `_determine_batch_execution_and_padding` 决定图模式与 padding。

[vllm_ascend/worker/model_runner_v1.py:2001-2013](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/worker/model_runner_v1.py#L2001-L2013) — 调 `_build_attention_metadata` 构建 attention metadata。

**前向 + 注入上下文**：这是「前向」的核心，`set_ascend_forward_context` 把 attn_metadata、图模式、batch 描述符、MoE 通信方式等注入「前向上下文」，深层算子（如 MoE）无需参数透传即可读取：

[vllm_ascend/worker/model_runner_v1.py:2055-2082](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/worker/model_runner_v1.py#L2055-L2082) — 在 `set_ascend_forward_context(...)` 上下文里调用 `self._model_forward(...)`。

`set_ascend_forward_context` 的签名（详细机制见 u2-l3）：

[vllm_ascend/ascend_forward_context.py:97-114](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/ascend_forward_context.py#L97-L114) — 接收 attn_metadata、图模式、batch 描述符等，写入 forward context，并在其中按 `num_tokens` 选定 MoE 通信方式。

**暂存中间状态并返回 None**：

[vllm_ascend/worker/model_runner_v1.py:2131-2144](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/worker/model_runner_v1.py#L2131-L2144) — 把 logits、hidden_states、attn_metadata、spec metadata 等打包进 `self.execute_model_state`，然后 `return None`，等待 `sample_tokens`。

`ExecuteModelState` 的定义（一个 NamedTuple，作为 `execute_model` 与 `sample_tokens` 之间的「接力棒」）：

[vllm_ascend/worker/model_runner_v1.py:269-284](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/worker/model_runner_v1.py#L269-L284) — `ExecuteModelState` 字段定义。

**`sample_tokens` 主干**：

[vllm_ascend/worker/model_runner_v1.py:2202-2203](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/worker/model_runner_v1.py#L2202-L2203) — 调 `self._sample(logits, spec_decode_metadata)` 完成采样。

[vllm_ascend/worker/model_runner_v1.py:2214-2229](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/worker/model_runner_v1.py#L2214-L2229) — 定义内部函数 `propose_draft_token_ids`，调用 `self.propose_draft_token_ids(...)` 让草稿器生成 draft token。

[vllm_ascend/worker/model_runner_v1.py:2273-2280](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/worker/model_runner_v1.py#L2273-L2280) — 投机解码分支：padded batch（eagle/draft_model）用 GPU 采样 token 跑草稿器，非 padded（ngram 等）用 CPU token 跑草稿器。

**采样实现 `_sample`**：无 spec 时直接采样，有 spec 时走拒绝采样：

[vllm_ascend/worker/model_runner_v1.py:2387-2418](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/worker/model_runner_v1.py#L2387-L2418) — `_sample`：`spec_decode_metadata is None` 用 `self.sampler`；否则用 `self.rejection_sampler`。

**前向 `_model_forward`**：

[vllm_ascend/worker/model_runner_v1.py:2607-2639](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/worker/model_runner_v1.py#L2607-L2639) — `_model_forward`：组装 `model_inputs` 后调用 `self.model(...)`，并在 FULL 图模式下调用 `update_full_graph_params` 更新图参数；开 flash_comm1 SP 时做 hidden states all-gather。

**草稿器分发 `propose_draft_token_ids`**：根据 `self.drafter` 的具体类型（ngram / eagle / mtp / medusa …）走不同分支生成 draft token：

[vllm_ascend/worker/model_runner_v1.py:1448-1657](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/worker/model_runner_v1.py#L1448-L1657) — `propose_draft_token_ids`：用一连串 `isinstance(self.drafter, ...)` 分发到各草稿器。

#### 4.4.4 代码实践

**实践目标**：画出一次 `execute_model` 内部「更新状态 → 准备输入 → 构建 attention metadata → 前向 → 采样」的完整数据流图（这是本讲的核心实践任务）。

**操作步骤**：

1. 在纸上（或用任意画图工具）画一条从左到右的流水线，包含 5 个主节点：`_update_states`、`_prepare_inputs`、`_build_attention_metadata`、`_model_forward`、`_sample`。
2. 在 `_update_states` 与 `_prepare_inputs` 之间，标注输入是 `SchedulerOutput`、输出是更新后的 `input_batch`。
3. 在 `_prepare_inputs` 与 `_build_attention_metadata` 之间，标注中间产物：`input_ids`、`positions`、`seq_lens`、`query_start_loc`、`attn_state`、`logits_indices`、`spec_decode_metadata`。
4. 在 `_build_attention_metadata` 与 `_model_forward` 之间，标注产物：`attn_metadata`（per-layer dict）与 `spec_decode_common_attn_metadata`。
5. 在 `_model_forward` 与 `_sample` 之间，标注产物：`hidden_states` → `compute_logits` → `logits`，并标注 `ExecuteModelState` 是「execute_model 与 sample_tokens 之间的接力棒」。
6. 用虚线标出 `set_ascend_forward_context` 把 `attn_metadata` / 图模式 / MoE 通信方式注入「前向上下文」，使深层算子可读。
7. 用另一条虚线标出：`sample_tokens` 之后若开投机解码，`propose_draft_token_ids` 会产出 draft token，回灌给下一轮。

**需要观察的现象**：你会清晰地看到两个「跨方法」的衔接点——(a) `SchedulerOutput` 经 `_update_states` 落到 `input_batch`，再由 `_prepare_inputs` 抽出张量；(b) `execute_model` 把中间结果暂存进 `self.execute_model_state`，`sample_tokens` 取出来继续。

**预期结果**：得到一张能解释「为什么 vLLM 要把 execute_model 与 sample_tokens 拆开」的数据流图——拆分让采样（含草稿生成）可以与下一批的前向重叠，实现异步调度。这是纯源码阅读型实践，无需 NPU。

> 进阶（可选）：在 `execute_model` 第 2055–2077 行的 `set_ascend_forward_context(...)` 调用处，逐个对照它传入的参数与 `ascend_forward_context.py:97` 的形参，理解每个参数流向了哪个运行期字段（待本地验证其运行时取值）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `execute_model` 最后返回 `None` 而不是直接返回采样结果？

> 参考答案：这是异步调度（async scheduling）的要求。把「前向 + 算 logits」放在 `execute_model`、把「采样 + 记账 + 草稿生成」放在 `sample_tokens`，可以让上一批的 `sample_tokens` 与下一批的 `execute_model` 在时间上重叠，提升吞吐。`execute_model` 用 `self.execute_model_state`（`ExecuteModelState`）把中间结果暂存，作为两方法之间的「接力棒」。

**练习 2**：`_sample` 在有/无投机解码时分别走哪条路径？

> 参考答案：`spec_decode_metadata is None`（无投机解码）时，直接调用 `self.sampler`（`AscendSampler`）对 logits 采样；否则调用 `self.rejection_sampler`（`AscendRejectionSampler`），它会把 draft token 与 target logits 对齐做拒绝采样验证，决定接受哪些 draft token。

**练习 3**：`propose_draft_token_ids` 是在 `execute_model` 里调用，还是在 `sample_tokens` 里调用？为什么？

> 参考答案：在 `sample_tokens` 里调用（第 2214–2229 行定义、2273–2280 行调用）。因为草稿器需要先拿到「已采样的 token」作为输入来预测下一个 draft token，而采样发生在 `sample_tokens`。同时，把草稿生成放在采样之后，能让它与下一批前向重叠。

---

## 5. 综合实践

**综合任务：用一段伪代码 + 注释，复述 `NPUModelRunner` 从被 worker 调用到产出 `ModelRunnerOutput` 的完整过程，并标注每一处 NPU 特化点。**

要求：

1. 写出 `NPUWorker.execute_model` → `NPUModelRunner.execute_model` → `NPUModelRunner.sample_tokens` → `NPUWorker` 返回输出的调用顺序。
2. 在伪代码中用注释标出至少 6 处「NPU 特化」点，例如：
   - `AscendSampler`（采样器）
   - `get_attn_backend(...)`（注意力后端选择）
   - `AscendAttentionState`（状态机）
   - `AscendCommonAttentionMetadata`（带 CPU seq_lens；开启卸载时带 req_ids_tensor / token_to_req）
   - `set_ascend_forward_context`（注入 MoE 通信方式、图模式）
   - `_torch_cuda_wrapper`（CUDA→NPU 兼容）
   - `NPUInputBatch`（输入批）
   - ACL Graph（`_use_aclgraph` / FULL/PIECEWISE）
   - `sparse_kv_offload_manager`（稀疏 KV 卸载，仅当配置开启）
3. 标注 `execute_model` 返回 `None`、`ExecuteModelState` 作为接力棒的那一步。

**示例框架（请补全注释）**：

```python
# 示例代码：仅用于说明调用结构，非项目原代码
def worker_execute_model(scheduler_output):
    output = model_runner.execute_model(scheduler_output, intermediate_tensors)
    # execute_model 返回 None（异步调度），中间结果存在 model_runner.execute_model_state
    ...
    return model_runner.sample_tokens(grammar_output)

def model_runner_execute_model(scheduler_output, intermediate_tensors):
    deferred = self._update_states(scheduler_output)          # TODO: 标注 NPU 特化
    logits_indices, spec_meta, total = self._prepare_inputs(...)
    cudagraph_mode, batch_desc, ... = self._determine_batch_execution_and_padding(...)
    attn_metadata, spec_common = self._build_attention_metadata(...)  # TODO: 标注稀疏卸载挂载点
    with set_ascend_forward_context(...):                     # TODO: 标注 NPU 特化
        hidden_states = self._model_forward(...)
    logits = self.model.compute_logits(hidden_states[logits_indices])
    self.execute_model_state = ExecuteModelState(...)         # TODO: 标注接力棒
    return None

def model_runner_sample_tokens(grammar_output):
    # 从 execute_model_state 解包
    sampler_output = self._sample(logits, spec_meta)          # TODO: 标注采样器
    if self.speculative_config:
        propose_draft_token_ids(sampled)                      # TODO: 标注草稿生成
    self._bookkeeping_sync(...)
    return ModelRunnerOutput(...)
```

**预期结果**：你能脱离源码，用自己的话向他人讲清「一次推理在 NPUModelRunner 内部经历了哪些阶段、哪些地方是 NPU 特有」。如果某些运行时取值无法确认，标注「待本地验证」。

## 6. 本讲小结

- `NPUModelRunner` **继承上游 `GPUModelRunner`** 而非从头实现，只在采样器、注意力后端、输入批、图机制、CUDA→NPU 兼容处做最小改写。
- 一次前向的主链路是：`_update_states`（更新状态）→ `_prepare_inputs`（准备输入）→ `_build_attention_metadata`（构建注意力元数据）→ `_model_forward`（前向）→ `_sample`（采样），由 `execute_model` 与 `sample_tokens` 两方法承载。
- NPU 用 **`AscendAttentionState`** 状态机（PrefillNoCache / DecodeOnly / ChunkedPrefill / SpecDecoding 等）驱动不同注意力路径；用 **`AscendCommonAttentionMetadata`** 携带 CPU 端 seq_lens，避免 GPU→CPU 同步。
- **`execute_model` 返回 `None`**，把中间结果暂存进 `ExecuteModelState`，由 `sample_tokens` 接力——这是 vLLM v1 异步调度的关键拆分。
- **`set_ascend_forward_context`** 在每次前向往「前向上下文」注入图模式、MoE 通信方式等，深层算子无需参数透传即可读取。
- **投机解码**在 `sample_tokens` 内衔接：采样后调 `propose_draft_token_ids` 让草稿器生成 draft token，供下一轮验证；eagle 路径用 GPU token，ngram 路径用 CPU token。
- **稀疏 KV 卸载（#13026）** 在 runner 里有两组挂载点：初始化/KV-cache 生命周期（`__init__` 占位、`profile_run` / `initialize_kv_cache` / `_allocate_kv_cache_tensors` / `_reshape_kv_cache_tensors` / `get_kv_cache_spec`）与每次前向的元数据构建（`_build_attention_metadata` 里调 `update_sparse_kv_offload_metadata` 并往公共元数据塞 `req_ids_tensor` / `token_to_req`）；未开启时这些路径对普通流程透明。

## 7. 下一步学习建议

- **u5（注意力后端）**：本讲只讲到 attention metadata 的「构建入口」，下一站应深入 `AscendAttentionBackend`、MLA / SFA / DSA 等具体后端如何消费这些 metadata。
- **u8（图编译与 ACL Graph）**：本讲提到 `_use_aclgraph`、FULL/PIECEWISE 模式与 `update_full_graph_params`，ACL Graph 的捕获与回放细节在 u8。
- **u4-l3（v2 ModelRunner）**：对比 v1 与 v2 的状态管理差异（`model_states`、`input_batch`、`pcp_manager`），理解为什么要有 v2 架构。
- **u10-l4（投机解码）**：本讲的 `propose_draft_token_ids` 涉及多种 proposer（eagle/ngram/mtp/dspark），它们的实现细节在 u10-l4。
- **u10-l6（稀疏 KV 卸载）**：本讲只点出了 runner 侧的挂载点，稀疏 KV 卸载的数据面（prefill 时 D2H 提交 KV 行、decode 时 top-k miss 由 H2D 回载到 resident 缓冲）、`SparseKVOffloadManager` 与 C++ 内核、`SparseKVOffloadConfig` 的约束，全部在 u10-l6 详解。
- 建议继续阅读 `vllm_ascend/worker/model_runner_v1.py` 的 `_dummy_run`（第 3133 行起）与 `capture_model`（第 4816 行起），理解图捕获如何复用 `_dummy_run` 走一遍前向来录制 ACL Graph。
