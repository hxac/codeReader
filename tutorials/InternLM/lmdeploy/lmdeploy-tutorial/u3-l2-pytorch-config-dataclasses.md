# PyTorch 引擎配置数据类 config.py

## 1. 本讲目标

本讲聚焦 PyTorch 引擎的「内部配置层」——`lmdeploy/pytorch/config.py`。读完本讲你应当能够：

1. 区分**用户侧** `PytorchEngineConfig`（你创建 pipeline 时传的）与**引擎内部** `ModelConfig`/`CacheConfig`/`SchedulerConfig`/`DistConfig`（引擎真正消费的），并说清为什么要分两层。
2. 读懂 `CacheConfig` 里 `block_size`、`num_cpu_blocks`、`num_gpu_blocks`、`cache_max_entry_count` 等字段的物理含义，能据此估算 KV 缓存能装多少 token。
3. 读懂 `DistConfig` 如何从 `tp/dp/ep` 推导出 `attn_tp/mlp_tp/moe_tp/world_size`，并理解 `TPMode.DEFAULT` 与 `TPMode.DP_TP` 的区别。
4. 看懂 `ModelConfig.from_pretrained` 如何把一份 HuggingFace `config.json` 变成引擎可用的模型配置，以及 `QuantizationConfig` 如何描述权重量化。
5. 能用 `ConfigBuilder` 把一个 `PytorchEngineConfig` 翻译成一整套内部配置并打印观察。

## 2. 前置知识

- **数据类（dataclass）**：Python 的 `@dataclass` 装饰器会自动生成 `__init__`、`__repr__` 等方法，让你用「字段列表」声明一个配置容器，省去手写样板代码。本讲里几乎所有配置都是 dataclass。
- **`__post_init__` 校验**：dataclass 支持一个特殊钩子 `__post_init__`，在 `__init__` 完成字段赋值后立即执行。LMDeploy 用它在「构造阶段」做参数合法性断言，让错误配置在引擎启动前就暴露。
- **用户侧配置回顾**：在 [u2-l3](u2-l3-engine-configs.md) 我们已经讲过 `PytorchEngineConfig`（位于 `lmdeploy/messages.py`），它的字段如 `tp`、`cache_max_entry_count`、`block_size`、`max_batch_size` 是「用户能调的旋钮」。本讲讲的是这些旋钮被翻译后、引擎内部实际使用的那一份数据结构。
- **持续批处理与 Paged Attention**：参见 [u1-l1](u1-l1-project-overview.md)。`CacheConfig` 的 `block_size` 等字段就是 Paged Attention 把 KV 缓存切块的依据。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [lmdeploy/pytorch/config.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/config.py) | **本讲主角**。定义引擎内部所有配置数据类与枚举：`BackendConfig`、`SchedulerConfig`、`CacheConfig`、`DistConfig`、`ModelConfig`、`QuantizationConfig` 等。 |
| [lmdeploy/pytorch/engine/config_builder.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/config_builder.py) | **翻译层**。`ConfigBuilder` 把用户侧 `PytorchEngineConfig` 逐字段搬运、组装成内部配置对象。 |
| [lmdeploy/messages.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/messages.py) | 用户侧 `PytorchEngineConfig`、`QuantPolicy` 定义于此（对照阅读）。 |
| [lmdeploy/pytorch/disagg/config.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/disagg/config.py) | `EngineRole`、`MigrationBackend` 等 PD 分离相关枚举（被 `CacheConfig` 引用）。 |

## 4. 核心概念与源码讲解

### 4.1 两层配置体系与翻译层 ConfigBuilder

#### 4.1.1 概念说明

先回答一个关键疑问：**我们不是已经有 `PytorchEngineConfig` 了吗，为什么还要一套内部 config？**

因为两者的「读者」不同：

- `PytorchEngineConfig`（用户侧）的读者是**你**。它只暴露「值得让用户操心」的旋钮，字段稳定、向后兼容、默认值友好。
- `config.py` 里的内部 config（引擎侧）的读者是**引擎代码自己**。它包含很多用户不该手动填、也无法在创建 pipeline 时就知道的量，例如：
  - `num_gpu_blocks`（GPU 上一共能分出多少个 KV 块）——必须等模型权重加载后、测完剩余显存才能算出来；
  - `num_key_value_heads`、`head_dim`——必须读 HF `config.json` 才知道；
  - `attn_tp`、`mlp_tp`、`moe_tp`——由 `tp/dp/ep` 推导出来的派生量。

所以引擎在启动时做了一次**翻译**：读取用户侧 `PytorchEngineConfig` + 模型目录，产出一整套内部配置对象，之后所有调度、算子、权重加载代码都只认内部配置。这带来了三个好处：

1. **职责隔离**：用户接口可以保持精简稳定，内部字段可以自由演进。
2. **构造即校验**：内部 dataclass 的 `__post_init__` 在翻译阶段就把非法组合（如 `world_size % dp != 0`）断言掉。
3. **派生量集中计算**：`DistConfig` 这种「由几个输入推出十几个输出」的逻辑被收拢在一处。

#### 4.1.2 核心流程

```
用户侧                          翻译层                                  引擎内部
PytorchEngineConfig ──► ConfigBuilder.update_engine_config() ──► 补全/修正后的 PytorchEngineConfig
  (tp, dp, block_size,        (填默认值、规整 cudagraph sizes)         │
   cache_max_entry_count,                                              │
   max_batch_size ...)                                                 ▼
                              ConfigBuilder.build_scheduler_config() ─► SchedulerConfig
                              ConfigBuilder.build_cache_config()     ─► CacheConfig
                              ConfigBuilder.build_backend_config()   ─► BackendConfig
                              ConfigBuilder.build_dist_config()      ─► DistConfig
                              ConfigBuilder.build_misc_config()      ─► MiscConfig
                              ModelConfig.from_pretrained(model_path) ─► ModelConfig (+ QuantizationConfig)
```

注意两条不同的翻译路径：

- **SchedulerConfig / CacheConfig / BackendConfig**：字段基本一一对应，用「直接构造」搬运（见 `build_*_config`）。
- **DistConfig / MiscConfig**：逻辑较重，用 dataclass 上的类方法 `from_engine_config` 完成翻译（`DistConfig.from_engine_config`、`MiscConfig.from_engine_config`）。

#### 4.1.3 源码精读

翻译层的总入口 `ConfigBuilder` 是一组静态方法。以「直接构造」为例，`build_cache_config` 把用户侧字段逐一映射到 `CacheConfig`：

[lmdeploy/pytorch/engine/config_builder.py:65-86](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/config_builder.py#L65-L86) —— 把 `PytorchEngineConfig` 的 `block_size`、`cache_max_entry_count` 等字段搬进 `CacheConfig`，并补一个引擎内部才需要的 `num_reserved_gpu_blocks=1`（预留 1 块给 dummy/padding 输入）。

[lmdeploy/pytorch/engine/config_builder.py:88-95](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/config_builder.py#L88-L95) —— `BackendConfig` 只有两个字段 `eager_mode` 与 `device_type`，直接搬。

[lmdeploy/pytorch/engine/config_builder.py:97-107](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/config_builder.py#L97-L107) —— `DistConfig` 与 `MiscConfig` 走 `from_engine_config` 类方法，逻辑较重故封装在各自 dataclass 内。

而 `update_engine_config` 是翻译前的「预处理」：在搬运之前先修正用户配置的若干不一致：

[lmdeploy/pytorch/engine/config_builder.py:20-55](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/config_builder.py#L20-L55) —— 例如当 `dp!=1` 但 `tp==1 and ep==1` 时，打印告警并把 `dp` 强制改回 1（数据并行必须有张量/专家并行配合才有意义）；以及规整 `cudagraph_capture_batch_sizes`。

#### 4.1.4 代码实践

**实践目标**：用 `grep` 列出 `config.py` 里全部 `@dataclass`，并把每个内部配置对应到它的 `ConfigBuilder.build_*` 方法，亲手验证「两层翻译」。

**操作步骤**：

1. 在仓库根目录执行统计：
   ```bash
   grep -n '^@dataclass\|^class .*:' lmdeploy/pytorch/config.py
   ```
2. 阅读上一节的 `config_builder.py` 链接，给每个内部配置找到它的来源方法。

**预期结果**（`config.py` 内共 9 个 dataclass + 2 个枚举）：

| dataclass | 行号 | 对应翻译方法 |
| --- | --- | --- |
| `BackendConfig` | L88 | `build_backend_config` |
| `SchedulerConfig` | L95 | `build_scheduler_config` |
| `CacheConfig` | L107 | `build_cache_config` |
| `DistConfig` | L157 | `build_dist_config` → `DistConfig.from_engine_config` |
| `ModelConfig` | L341 | `ModelConfig.from_pretrained` |
| `DLLMConfig` | L556 | （被 `MiscConfig` 包含） |
| `MiscConfig` | L564 | `build_misc_config` → `MiscConfig.from_engine_config` |
| `SpecDecodeConfig` | L600 | `build_specdecode_config` → `SpecDecodeConfig.from_config` |
| `QuantizationConfig` | L662 | `QuantizationConfig.from_config`（由 `ModelConfig.from_pretrained` 调用） |

枚举：`TPMode`（L151）、`UnmaskingStrategy`（L532）。

> 说明：以上行号基于当前 HEAD `b56ddfb`，若代码演进请以实际 `grep` 输出为准。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `num_gpu_blocks` 不放在用户侧 `PytorchEngineConfig` 让用户填，而要放到内部 `CacheConfig`？
**答案**：因为它依赖「模型权重加载后剩余多少显存」这一运行时才能测得的量，用户在创建 pipeline 时无从知晓；它是引擎测算后回填的派生字段，故属于内部配置。

**练习 2**：`build_cache_config` 里有 `num_reserved_gpu_blocks=1`，而 `CacheConfig` 字段默认值是 0。这说明了一个什么设计？
**答案**：内部 dataclass 的默认值偏「单测友好」（0 便于单元测试），真实引擎路径由翻译层显式注入正确值（1）。两层默认值服务不同场景。

---

### 4.2 SchedulerConfig：调度器配置

#### 4.2.1 概念说明

`SchedulerConfig` 描述**调度器**（scheduler，详见 [u4-l4](u4-l4-scheduler-prefill-decode.md)）需要的运行参数：最多同时跑多少请求、单会话最长多少 token、块被驱逐后怎么恢复、多久插入一次 prefill。它直接决定「持续批处理」的吞吐与延迟权衡。

#### 4.2.2 核心流程

调度器在每个推理 step 都要决定「这一步做 prefill 还是 decode、把哪些 sequence 组进 batch」。`SchedulerConfig` 给这些决策提供上界与节奏：

- `max_batches`：一个 batch 最多容纳多少条 sequence，是并发的硬上界。
- `prefill_interval`：decode 阶段每跑 `prefill_interval` 步，就让等待中的新请求有机会插入做 prefill（避免长 decode 把新请求饿死）。
- `eviction_type='recompute'`：当 KV 块不够需要驱逐某条 sequence 的块时，用「重算」方式恢复（之后该请求续写时重新 prefill 被驱逐的部分）。

#### 4.2.3 源码精读

[lmdeploy/pytorch/config.py:95-104](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/config.py#L95-L104) —— `SchedulerConfig` 定义。注意 `max_batches` 与 `max_session_len` 没有默认值（必填），其余字段都有默认值。

字段速查表：

| 字段 | 默认值 | 含义 |
| --- | --- | --- |
| `max_batches` | （必填） | 最大并发 batch 大小，来自用户侧 `max_batch_size` |
| `max_session_len` | （必填） | 单条会话最大 token 长度，来自 `session_len` |
| `max_request_output_len` | 512 | 单次请求最大输出 token 数 |
| `eviction_type` | `'recompute'` | 块驱逐后的恢复方式 |
| `prefill_interval` | 16 | decode 中插入 prefill 调度的步间隔 |
| `max_active_adapters` | 64 | 最大同时活跃的 LoRA 适配器数 |

它的翻译非常直白：

[lmdeploy/pytorch/engine/config_builder.py:57-63](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/config_builder.py#L57-L63) —— `max_batch_size → max_batches`、`session_len → max_session_len`、`prefill_interval` 同名直传。注意这里**没有**传 `max_request_output_len` 与 `eviction_type`，它们走 dataclass 默认值——这正体现了「用户侧不必关心、内部用默认」的分层。

#### 4.2.4 代码实践

**实践目标**：验证 `SchedulerConfig` 三个带默认值的字段在翻译后确实是默认值。

**操作步骤**：

```python
# 示例代码：仅依赖 CPU 可导入 lmdeploy 的环境
from lmdeploy.messages import PytorchEngineConfig
from lmdeploy.pytorch.engine.config_builder import ConfigBuilder

ec = PytorchEngineConfig(max_batch_size=32, session_len=4096)
ec = ConfigBuilder.update_engine_config(ec)
sc = ConfigBuilder.build_scheduler_config(ec)
print(sc)
```

**需要观察的现象**：打印结果里 `max_batches=32`、`max_session_len=4096`（来自用户输入），而 `max_request_output_len=512`、`eviction_type='recompute'`、`prefill_interval=16`、`max_active_adapters=64`（来自 dataclass 默认值）。

**预期结果**：`SchedulerConfig(max_batches=32, max_session_len=4096, max_request_output_len=512, eviction_type='recompute', prefill_interval=16, max_active_adapters=64)`。若你的环境 `PytorchEngineConfig` 默认 `prefill_interval` 不同，以实际输出为准——待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：把 `prefill_interval` 调大（比如 64）会对系统行为产生什么影响？
**答案**：decode 阶段会更久不被新 prefill 打断，吞吐可能上升（batch 更稳定），但新请求的排队等待变长、首 token 延迟升高。这是吞吐-延迟的经典权衡。

**练习 2**：`eviction_type='recompute'` 相比「直接丢弃」有什么代价与好处？
**答案**：好处是请求可被恢复、不丢失上下文；代价是被驱逐请求续写时需要重新 prefill 已生成部分，消耗算力。

---

### 4.3 CacheConfig：KV 缓存的物理布局

#### 4.3.1 概念说明

`CacheConfig` 是本讲信息量最大的配置，它描述 **Paged Attention 的 KV 缓存如何切块、装在哪、装多少**。理解它的字段是读懂 [u4-l5](u4-l5-block-manager-kv-cache.md) BlockManager 的前提。

#### 4.3.2 核心流程

KV 缓存的容量估算有一个清晰公式。对一个解码阶段的模型，每个 token 需要为 K 和 V 各存一份，跨所有层、所有 KV 头：

\[
\text{bytes\_per\_token} = 2 \times \text{num\_layers} \times \text{num\_kv\_heads} \times \text{head\_dim} \times \text{dtype\_bytes}
\]

其中系数 2 表示 K 与 V 两份。于是给定可用显存 \(M_{\text{kv}}\)：

\[
\text{num\_gpu\_blocks} = \left\lfloor \frac{M_{\text{kv}}}{\text{block\_size} \times \text{bytes\_per\_token}} \right\rfloor
\]

而 \(M_{\text{kv}}\) 由 `cache_max_entry_count`（默认 0.8）决定：它表示「权重加载后剩余显存中划给 KV cache 的比例」。`num_gpu_blocks` 由引擎在启动时测算后回填进 `CacheConfig`（用户侧默认 0）。

#### 4.3.3 源码精读

[lmdeploy/pytorch/config.py:107-148](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/config.py#L107-L148) —— `CacheConfig` 全貌，包含字段定义与 `__post_init__` 校验。

关键字段表：

| 字段 | 默认值 | 含义 |
| --- | --- | --- |
| `max_batches` | （必填） | 最大并发 batch |
| `block_size` | （必填） | KV 块的逻辑大小（每块含多少 token），用户侧默认 64 |
| `num_cpu_blocks` | （必填） | CPU 侧块数（用于 swap/offload），默认 0 表示不启用 |
| `num_gpu_blocks` | （必填） | GPU 侧块数，默认 0，引擎测算后回填 |
| `kernel_block_size` | -1 | kernel 实际访问块大小；-1 表示等于 `block_size` |
| `window_size` | -1 | 滑动窗口注意力窗口，-1 表示禁用 |
| `cache_max_entry_count` | 0.8 | KV cache 占剩余显存比例 |
| `max_prefill_token_num` | 8192 | 单次 prefill 处理的最大 token 数 |
| `cudagraph_capture_batch_sizes` | None | CUDA Graph 捕获的 batch 列表 |
| `enable_prefix_caching` | False | 是否启用前缀缓存 |
| `quant_policy` | `NONE` | KV cache 量化策略（注意：是 KV 量化，不是权重量化） |
| `num_reserved_gpu_blocks` | 0 | 预留块数（翻译层注入为 1） |
| `role` | `EngineRole.Hybrid` | PD 分离时本引擎角色 |
| `migration_backend` | `MigrationBackend.DLSlime` | PD 分离 KV 迁移后端 |

`__post_init__` 里有几条重要校验，值得逐条读懂：

[lmdeploy/pytorch/config.py:135-148](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/config.py#L135-L148) —— 四件事：(1) 校验 `prefix_cache_state_budget` 与 `prefix_cache_decode_state_interval` 非负；(2) **滑动窗口注意力与前缀缓存互斥**——当 `window_size > 1` 且开了 `enable_prefix_caching` 时打告警并强制关闭前缀缓存（窗口注意力的 KV 不能被复用）；(3) `kernel_block_size==-1` 时回退为 `block_size`；(4) 调 `normalize_cudagraph_capture_batch_sizes` 规整 CUDA Graph 捕获列表。

辅助函数 `normalize_cudagraph_capture_batch_sizes`：

[lmdeploy/pytorch/config.py:17-32](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/config.py#L17-L32) —— 去重、排序、丢弃超过 `max_batches` 的尺寸，并保证列表末尾一定包含 `max_batches` 本身（确保最大 batch 一定被 CUDA Graph 捕获）。

#### 4.3.4 代码实践

**实践目标**：用一个公式估算一个 8B 模型在 1GB KV 预算下能容纳多少 token，并对照 `CacheConfig` 字段理解。

**操作步骤**：

1. 取一个典型 8B 模型的结构参数（以 Llama 类为例）：`num_layers=32`、`num_kv_heads=8`、`head_dim=128`、fp16（`dtype_bytes=2`）。
2. 代入公式：
   \[
   \text{bytes\_per\_token} = 2 \times 32 \times 8 \times 128 \times 2 = 131072 \text{ B} = 128 \text{ KB}
   \]
3. 1GB = \(2^{30}\) B ≈ 1073741824 B，则可容纳 token 数 ≈ \(1073741824 / 131072 = 8192\) token。
4. 若 `block_size=64`，则对应的 `num_gpu_blocks ≈ 8192 / 64 = 128` 块。

**需要观察的现象**：每 token 的 KV 开销（128 KB）远比想象中大——这正是 Paged Attention 把缓存切块管理的根本动机。1GB 只能装约 8192 token，说明 KV cache 是显存大头。

**预期结果**：上面是手算结果。你可以写一段 Python 验证：
```python
# 示例代码
bytes_per_token = 2 * 32 * 8 * 128 * 2
budget = 1024**3
tokens = budget // bytes_per_token
block_size = 64
num_gpu_blocks = tokens // block_size
print(tokens, num_gpu_blocks)  # 预期 8192 128
```
> 待本地验证：不同 8B 模型 `num_kv_heads`/`head_dim` 不同，结果会变。

#### 4.3.5 小练习与答案

**练习 1**：`block_size` 调大（比如 128）和调小（比如 16）各有什么影响？
**答案**：调大→块内碎片浪费增加（最后一个块常填不满），但管理开销小、kernel 调度效率高；调小→碎片浪费减少、前缀缓存命中更精细，但块表更长、调度开销上升。LMDeploy 默认 64 是折中。

**练习 2**：为什么 `window_size > 1` 时要禁用前缀缓存？
**答案**：滑动窗口注意力只看最近 `window_size` 个 token，旧 KV 会被主动丢弃，缓存的前缀无法被复用，开了反而浪费且语义错误。

---

### 4.4 DistConfig 与 TPMode：并行配置

#### 4.4.1 概念说明

`DistConfig` 描述**多卡并行拓扑**。LMDeploy 同时支持三种并行：

- **张量并行 TP**：把每一层的权重切到多卡，每卡算一部分，最后 all-reduce。`tp` 字段。
- **数据并行 DP**：每卡跑完整副本处理不同请求，`dp` 字段。
- **专家并行 EP**：MoE 模型把不同专家分到不同卡，`ep` 字段。

复杂之处在于：attention、MLP、MoE 三种层可以**各自**有不同的并行度（`attn_tp`/`mlp_tp`/`moe_tp`），它们由 `tp/dp/ep` 推导而来。`TPMode` 枚举则描述 MLP/MoE 层具体采用哪种 TP 实现。

#### 4.4.2 核心流程

`DistConfig.__post_init__` 是一个「拓扑求解器」，从 `dp/tp/ep` 推导出完整的并行布局。用伪代码表示：

```
输入: dp, tp, ep (可选: attn_tp, mlp_tp, moe_tp)

1. 若 dp == 1: mlp_tp = attn_tp = moe_tp = None   # 单 dp 组无需细分
2. mlp_tp = mlp_tp or tp
3. moe_tp = moe_tp or (1 if ep > 1 else mlp_tp)   # 开了 EP 则 MoE 不再做 TP
4. world_size = ep if ep > 1 else max(mlp_tp, moe_tp)
5. attn_tp = attn_tp or (world_size // dp)
6. tp = attn_tp
7. 一致性断言: world_size 必须能被 dp / ep / mlp_tp / moe_tp / attn_tp 整除
8. mlp_tp_mode = DEFAULT if mlp_tp in {1, attn_tp} else DP_TP
   moe_tp_mode = DEFAULT if moe_tp in {1, attn_tp} else DP_TP
```

`TPMode` 两个取值的含义（[lmdeploy/pytorch/config.py:151-154](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/config.py#L151-L154)）：

- `TPMode.DEFAULT`：标准张量并行，层后做 all-reduce。
- `TPMode.DP_TP`：当 `mlp_tp`/`moe_tp` 大于 `attn_tp` 时启用——相当于在 attention 的 TP 组之上再叠一层 DP，避免 MLP/MoE 切得过细导致通信开销过大。

#### 4.4.3 源码精读

[lmdeploy/pytorch/config.py:157-180](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/config.py#L157-L180) —— `DistConfig` 字段定义与 `__post_init__` 开头。

字段表：

| 字段 | 默认值 | 含义 |
| --- | --- | --- |
| `dp` | 1 | 数据并行度 |
| `ep` | 1 | 专家并行度（仅 MoE） |
| `dp_rank` | 0 | 当前进程在 DP 维的 rank |
| `enable_microbatch` | False | 是否启用 microbatch |
| `enable_eplb` | False | 是否启用专家负载均衡（eplb，见 [u5-l3](u5-l3-moe-modules.md)） |
| `tp` | 1 | 默认/attn 张量并行度 |
| `attn_tp` | None | attention 专用 TP（派生） |
| `mlp_tp` | None | MLP 专用 TP（派生） |
| `moe_tp` | None | MoE 专用 TP（派生） |
| `mlp_tp_mode` / `moe_tp_mode` | DEFAULT | 对应层的 TP 实现模式（派生） |

核心推导逻辑：

[lmdeploy/pytorch/config.py:181-219](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/config.py#L181-L219) —— 即 4.4.2 伪代码的真实实现，包含一长串 `assert` 保证拓扑一致性。

派生结果的使用者：

[lmdeploy/pytorch/config.py:221-233](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/config.py#L221-L233) —— `get_tp_by_layer(layer_type)` 按 `'attn'/'mlp'/'moe'` 返回该层应使用的 `(tp, tp_mode)`，供模型层在 forward 时查询自己该按几路切分。

翻译入口：

[lmdeploy/pytorch/config.py:235-249](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/config.py#L235-L249) —— `DistConfig.from_engine_config` 从用户侧 `PytorchEngineConfig` 取 `dp/ep/tp/attn_tp_size/mlp_tp_size/moe_tp_size` 等字段构造。注意用户侧字段名带 `_size` 后缀（`attn_tp_size`），内部不带——这是两层命名差异的一个实例。

#### 4.4.4 代码实践

**实践目标**：构造不同 `tp/dp/ep` 组合，观察 `DistConfig.__post_init__` 推导出的 `attn_tp/mlp_tp/moe_tp/world_size`，亲手验证 4.4.2 的求解器。

**操作步骤**：

```python
# 示例代码
from lmdeploy.messages import PytorchEngineConfig
from lmdeploy.pytorch.config import DistConfig

# 场景 A: 单卡，全部默认
print(DistConfig.from_engine_config(PytorchEngineConfig(tp=1)))
# 场景 B: tp=2
print(DistConfig.from_engine_config(PytorchEngineConfig(tp=2)))
# 场景 C: dp=2, tp=2 （数据并行 + 张量并行）
print(DistConfig.from_engine_config(PytorchEngineConfig(dp=2, tp=2)))
```

**需要观察的现象**：
- 场景 A：`dp=1` 时 `mlp_tp/attn_tp/moe_tp` 被置 None，`world_size=1`。
- 场景 B：`attn_tp=mlp_tp=moe_tp=2`，`world_size=2`，`mlp_tp_mode/moe_tp_mode=DEFAULT`（因为等于 `attn_tp`）。
- 场景 C：`world_size=2`（注意不是 4！），`attn_tp = world_size//dp = 1`——这揭示了「DP 下 attention 不再做 TP」的关键行为。

**预期结果**：场景 C 打印出的 `attn_tp` 应为 1、`tp` 也为 1。这正是 `ConfigBuilder.update_engine_config` 不强制要求 `dp` 必须配 `tp>1` 的内在原因。若行为有出入，以本地实际版本为准——待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：当 `ep > 1` 时，`moe_tp` 为什么被设为 1？
**答案**：见第 3 步 `moe_tp = moe_tp or (1 if ep > 1 else mlp_tp)`。专家已经按 EP 维度切到不同卡，同一专家内部再做 TP 收益小、通信开销大，故 MoE 层用 EP 取代 TP。

**练习 2**：什么条件下 `mlp_tp_mode` 会变成 `DP_TP`？
**答案**：当 `mlp_tp` 既不等于 1 也不等于 `attn_tp` 时（见第 8 步），即 MLP 比 attention 切得更细，此时启用 `DP_TP` 模式以避免过细 TP 的通信开销。

---

### 4.5 ModelConfig 与 QuantizationConfig：模型本身

#### 4.5.1 概念说明

`ModelConfig` 描述**模型结构本身**：隐藏层维度、层数、注意力头数、KV 头数、头维度、词表大小、dtype 等。它不是用户手填的，而是由 `from_pretrained` 读 HuggingFace `config.json` 自动提取。`QuantizationConfig` 则描述模型的**权重量化**信息（AWQ/SmoothQuant/FP8），挂在 `ModelConfig.quant_config` 上。

`ModelConfig` 是模型重写（patch，见 [u3-l3](u3-l3-model-patch-mechanism.md)）、权重加载（[u3-l5](u3-l5-weight-loading.md)）、算子选择（[u5-l4](u5-l4-op-backend-dispatch.md)）的共同数据来源。

#### 4.5.2 核心流程

`ModelConfig.from_pretrained` 的构造链：

```
from_pretrained(model_path, dtype, dist_config, ...)
   │
   ├─ config_from_pretrained(model_path)        # 读 HF config.json → hf_config
   ├─ _patch_quantization_config(hf_config)     # 若 model_format='fp8'，注入量化配置
   ├─ cls.from_hf_config(hf_config, ...)        # 调 AutoModelConfigBuilder.build() 填充结构字段
   │     ├─ 推导 k_head_dim / v_head_dim
   │     ├─ TP 头数整除性校验
   │     └─ _update_torch_dtype(...)            # 决定最终 torch.dtype
   ├─ override_hf_config(hf_config, hf_overrides)  # 用户自定义覆盖
   ├─ QuantizationConfig.from_config(hf_config)    # 解析权重量化
   └─ 返回填好的 ModelConfig
```

dtype 解析 `_update_torch_dtype` 有几条优先级规则：AWQ 模型强制 float16；否则按 `hf_config.dtype` → `text_config.dtype` → `torch_dtype` 三级兜底；`bfloat16` 在设备不支持时回退 float16；用户 `dtype!='auto'` 时以用户为准。

#### 4.5.3 源码精读

[lmdeploy/pytorch/config.py:341-394](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/config.py#L341-L394) —— `ModelConfig` 字段定义。前 7 个是必填的结构参数（`hidden_size`/`num_layers`/`num_attention_heads`/`num_key_value_heads`/`bos_token_id`/`eos_token_id`/`head_dim`），其余是可选的高级字段。

注意几个特殊字段：
- `hf_config` / `llm_config`：保留原始 HF 配置对象，供模型重写层查询任意字段。
- `dist_config`：挂载 4.4 的 `DistConfig`，让模型层能查到自己的 TP 度。
- `quant_config`：挂载 `QuantizationConfig`。
- `cache_shapes` / `states_shapes`：为 DeepSeek V3.2 NSA、Qwen3-Next 等 SSM/混合模型预留的额外缓存形状。

TP 下头数切分的工具方法：

[lmdeploy/pytorch/config.py:400-411](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/config.py#L400-L411) —— `get_num_qkv_head_by_tp` 返回当前 rank 上的 `(num_q_heads, num_kv_heads)`，是 GQA（分组查询注意力）在 TP 下切头的依据。注意 `num_kv_heads` 用 `max(... // tp, 1)` 保证 KV 头少于 TP 度时至少保留 1 个（复制而非切分）。

构造链：

[lmdeploy/pytorch/config.py:413-477](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/config.py#L413-L477) —— `from_pretrained`：读 HF config → 补量化 → 调 `from_hf_config` → 应用 `hf_overrides` → 挂 `quant_config`。

[lmdeploy/pytorch/config.py:479-529](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/config.py#L479-L529) —— `from_hf_config`：调 `AutoModelConfigBuilder.build()`（位于 `lmdeploy/pytorch/configurations`，按模型 arch 选择对应 builder）填充结构字段，再补 `k/v_head_dim`、做 TP 整除校验、解析 dtype。

dtype 解析：

[lmdeploy/pytorch/config.py:35-85](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/config.py#L35-L85) —— `_update_torch_dtype`：AWQ 强制 float16（L46-50），bfloat16 不支持时回退（L71-72），最终把字符串名转成 `torch.dtype` 并校验。

权重量化配置：

[lmdeploy/pytorch/config.py:662-747](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/config.py#L662-L747) —— `QuantizationConfig.from_config`：从 HF `quantization_config` 解析出 `quant_method`（awq/smooth_quant/fp8）、`bits`、`group_size`、`weight_block_size`、`fp8_quant_scope` 等。注意 fp8 的 `fmt`（e4m3/e5m2）会被映射到具体 `torch.dtype`。

[lmdeploy/pytorch/config.py:749-762](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/config.py#L749-L762) —— `get_quant_method(prefix, module_kind)`：按模块路径与类型（linear/moe/norm）决定该模块是否量化、用哪种方法。它支持 `fp8_quant_scope='moe_only'`（仅量化 MoE，其它层不量化，见近期提交 `Support fp8 moe only for qwen3.5`）。

#### 4.5.4 代码实践

**实践目标**：跟踪 `ModelConfig.from_pretrained` 的调用点，理解它如何被引擎创建。

**操作步骤**：

1. 在 `lmdeploy/pytorch` 下搜索 `ModelConfig.from_pretrained` 的调用点：
   ```bash
   grep -rn 'ModelConfig.from_pretrained' lmdeploy/pytorch/
   ```
2. 阅读 [lmdeploy/pytorch/engine/executor/__init__.py:73](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/executor/__init__.py#L73) 附近的调用，确认 `model_path`、`dist_config` 等参数如何传入。

**需要观察的现象**：`from_pretrained` 不是被 `Pipeline` 直接调用的，而是经由 `executor`（引擎执行器）调用——这印证了「ModelConfig 属于引擎内部、由引擎在自己初始化阶段创建」，用户永远不直接构造它。

**预期结果**：至少能在 `executor/__init__.py` 找到一处 `ModelConfig.from_pretrained(model_path, ...)` 调用。具体参数列表以本地源码为准。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `eos_token_id` 的类型是 `list[int]` 而非 `int`？
**答案**：一个模型可能有多个结束符（如普通 EOS、特定角色标记等）。`from_hf_config` 在 L526-527 把单个 int 也包成 list，统一后续处理。

**练习 2**：`QuantizationConfig` 与 `CacheConfig.quant_policy` 描述的是同一种量化吗？
**答案**：不是。`QuantizationConfig` 描述**权重**量化（AWQ/GPTQ/FP8，作用于模型权重）；`CacheConfig.quant_policy` 描述 **KV cache** 量化（作用于运行时的键值缓存）。两者独立，可分别配置。

---

## 5. 综合实践

把本讲全部知识串起来：**用 `ConfigBuilder` 把一个用户侧 `PytorchEngineConfig` 完整翻译成一整套内部配置，并解读打印结果**。

```python
# 示例代码：CPU 环境可运行（无需 GPU），需已 pip install lmdeploy
from lmdeploy.messages import PytorchEngineConfig
from lmdeploy.pytorch.engine.config_builder import ConfigBuilder

# 1) 用户侧配置：模拟一个 tp=2、带前缀缓存、自定义 block_size 的部署
ec = PytorchEngineConfig(tp=2,
                         cache_max_entry_count=0.5,
                         block_size=32,
                         enable_prefix_caching=True,
                         max_batch_size=64,
                         session_len=4096)

# 2) 翻译前预处理（填默认、规整 cudagraph、修正 dp/tp 不一致）
ec = ConfigBuilder.update_engine_config(ec)

# 3) 逐个产出内部配置
print('Scheduler:', ConfigBuilder.build_scheduler_config(ec))
print('Cache    :', ConfigBuilder.build_cache_config(ec))
print('Backend  :', ConfigBuilder.build_backend_config(ec))
print('Dist     :', ConfigBuilder.build_dist_config(ec))
print('Misc     :', ConfigBuilder.build_misc_config(ec))
```

**需要观察并解释的现象**：

1. **Scheduler**：`max_batches=64`、`max_session_len=4096` 来自用户输入；`eviction_type='recompute'` 等来自默认值。
2. **Cache**：`block_size=32` 来自用户；`num_cpu_blocks=0`、`num_gpu_blocks=0`（尚未测算回填）；`num_reserved_gpu_blocks=1`（翻译层注入）；`enable_prefix_caching=True`。
3. **Dist**：重点看 `attn_tp=mlp_tp=moe_tp=2`、`world_size=2`、`mlp_tp_mode=moe_tp_mode=DEFAULT`。
4. **Backend**：`eager_mode`、`device_type` 两字段。

**预期结果**：你能为打印出的每一个字段说出「它来自哪个用户侧字段 / 还是默认值 / 还是派生量」。这就证明你掌握了「两层配置 + 翻译层」的完整图景。

> 待本地验证：`PytorchEngineConfig` 的某些默认值（如 `eager_mode`、`device_type`）可能随版本调整；`update_engine_config` 在 `dp!=1` 时可能修改 `dp`。以本地实际输出为准。

## 6. 本讲小结

- LMDeploy 的 PyTorch 引擎有**两层配置**：用户侧 `PytorchEngineConfig`（`messages.py`）面向用户、字段精简；引擎内部 `config.py` 的各 dataclass 面向引擎、包含派生量与运行时测算值。
- **翻译层** `ConfigBuilder`（`config_builder.py`）负责把用户侧配置搬运/组装成内部配置：`SchedulerConfig`/`CacheConfig`/`BackendConfig` 用直接构造，`DistConfig`/`MiscConfig` 用各自的 `from_engine_config`。
- `SchedulerConfig` 管「跑多少、跑多久、何时 prefill、如何驱逐」；`CacheConfig` 管「KV 缓存如何切块、装多少、装在哪」，含 Paged Attention 的核心参数 `block_size`/`num_gpu_blocks`/`cache_max_entry_count`。
- `DistConfig` 是一个拓扑求解器，从 `dp/tp/ep` 推导 `attn_tp/mlp_tp/moe_tp/world_size`，并用 `TPMode`（`DEFAULT`/`DP_TP`）描述 MLP/MoE 层的 TP 实现模式。
- `ModelConfig` 由 `from_pretrained` 读 HF `config.json` 自动构造，挂载 `dist_config` 与 `quant_config`，是 patch/权重加载/算子选择的共同数据源。
- 每个 dataclass 的 `__post_init__` 在构造阶段做断言，让非法配置（如 `world_size % dp != 0`、滑动窗口与前缀缓存冲突）在引擎启动前就暴露。

## 7. 下一步学习建议

- 下一讲 [u3-l3 模型 Patch 重写机制](u3-l3-model-patch-mechanism.md) 会用到本讲的 `ModelConfig`（尤其是 `hf_config` 与 `custom_module_map`），建议先回顾 4.5 节。
- 想深入 KV 缓存管理，直接跳到 [u4-l5 分块 KV 缓存与 BlockManager](u4-l5-block-manager-kv-cache.md)，那里会消费本讲 `CacheConfig` 的 `block_size`/`num_gpu_blocks`。
- 想深入并行，看 [u9-l4 张量并行与分布式](u9-l4-tensor-parallelism-distribution.md)，它会用到本讲 `DistConfig.get_tp_by_layer` 与 `distributed.py` 的通信初始化。
- 想深入量化，看 [u7-2 AWQ](u7-l2-awq-quantization.md) 与 [u5-l2 线性层量化变体](u5-l2-linear-quant-variants.md)，它们消费 `QuantizationConfig.get_quant_method` 的分发结果。
