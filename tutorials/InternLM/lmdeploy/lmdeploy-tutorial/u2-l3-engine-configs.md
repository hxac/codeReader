# 引擎配置 TurbomindEngineConfig 与 PytorchEngineConfig

## 1. 本讲目标

学完本讲后，你应该能够：

- 区分「采样配置」`GenerationConfig` 与「引擎配置」`TurbomindEngineConfig` / `PytorchEngineConfig` 的职责边界。
- 列举两套引擎配置的共有字段与差异字段，并能解释 `tp` / `dp` / `ep`、`session_len`、`max_batch_size`、`cache_max_entry_count`、`enable_prefix_caching` 等关键字段的含义。
- 看懂配置类是如何用 dataclass + `__post_init__` 做字段校验的，以及它如何被 `pipeline()` 传递到底层引擎。
- 学会根据自己的显存预算与并发需求，合理调整引擎配置。

## 2. 前置知识

本讲承接 [u2-l1 核心消息与响应类型](u2-l1-core-message-types.md) 与 [u2-l2 生成配置 GenerationConfig 详解](u2-l2-generation-config.md)。在进入引擎配置前，先厘清三个基础概念。

### 2.1 两种「配置」不要混淆

LMDeploy 的配置分两层：

| 配置 | 位置 | 关心什么 | 何时确定 |
| --- | --- | --- | --- |
| `GenerationConfig` | [lmdeploy/messages.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/messages.py#L35-L36) | **采样**：temperature、top_p、max_new_tokens…… | 每次推理调用时 |
| `TurbomindEngineConfig` / `PytorchEngineConfig` | [lmdeploy/messages.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/messages.py#L208-L209) | **引擎**：并行度、显存、批大小、KV 缓存…… | 创建 pipeline 时，只配一次 |

一句话：`GenerationConfig` 管「这一次怎么生成」，引擎配置管「这个引擎长什么样」。

### 2.2 KV 缓存与显存

推理时，模型不仅要存权重，还要为每条正在处理的序列保存历史注意力向量，称为 **KV cache**。LMDeploy 采用「分块 KV 缓存」（Paged Attention 思想，见 [u1-l1](u1-l1-project-overview.md)）：把 token 序列切成固定大小的 **block**，按需分配。

显存因此被分成两大块：

1. **模型权重**（weights）：加载时固定，大小 ≈ 参数量 × dtype 字节数。
2. **KV cache**：随并发序列数和上下文长度增长，是引擎配置控制的核心。

一个粗略的 KV cache 显存估算公式（单 GPU）：

\[
\text{KV 显存} \approx 2 \times L \times \frac{H_{kv}}{tp} \times D_h \times N_{token} \times b
\]

其中 \(L\) 是层数、\(H_{kv}\) 是 KV 头数、\(D_h\) 是头维度、\(N_{token}\) 是总 token 数、\(b\) 是每个元素的字节数（FP16=2），因子 2 表示 K 与 V 两份。注意张量并行 `tp` 会把 KV 头数切分到各卡，从而降低单卡 KV 显存。

### 2.3 三种并行度

- **tp（Tensor Parallelism，张量并行）**：把单层权重矩阵切到多卡上，适合单机多卡放大单条请求能跑的模型。
- **dp（Data Parallelism，数据并行）**：复制多份完整模型，各自处理不同请求，适合提高吞吐。
- **ep（Expert Parallelism，专家并行）**：仅在 PyTorch 后端出现，把 MoE 的专家分到不同卡。

> 提示：`tp × dp` 通常应等于可用 GPU 数。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [lmdeploy/messages.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/messages.py#L1-L17) | 定义两套引擎配置类、`QuantPolicy` 枚举；本讲的主战场。 |
| [lmdeploy/\_\_init\_\_.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/__init__.py#L1-L13) | 把两个配置类导出为公开 API（`lmdeploy.PytorchEngineConfig` 等）。 |
| [lmdeploy/pytorch/disagg/config.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/disagg/config.py#L21-L42) | 定义 `EngineRole`、`MigrationBackend` 枚举，被 `PytorchEngineConfig` 引用。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：4.1 讲两个配置类的共同骨架（dataclass + 校验），4.2 精讲 `TurbomindEngineConfig`，4.3 精讲 `PytorchEngineConfig`，最后 4.4 做对比与选型。

### 4.1 引擎配置类的共同骨架

#### 4.1.1 概念说明

无论是 TurboMind 还是 PyTorch 引擎，用户配置引擎的方式都是「new 一个配置对象，传给 `pipeline()`」。这两个类都用 Python 的 `@dataclass` 风格来写：字段名 + 类型 + 默认值，构造时只写关心的字段即可。

TurboMind 的配置类更进一步，用的是 **pydantic 的 dataclass**：

[lmdeploy/messages.py:9](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/messages.py#L9-L9) —— 导入 pydantic 提供的 dataclass 装饰器。

```python
from pydantic.dataclasses import dataclass as pydantic_dataclass
```

这使 `TurbomindEngineConfig` 在被序列化（例如 serve 启动时）和类型校验上更严格。两个类都额外定义了 `__post_init__` 方法，在对象构造完成后立即检查字段合法性，把「参数错误」尽早暴露在创建 pipeline 阶段，而不是推理中途崩溃。

#### 4.1.2 核心流程

配置对象的典型生命周期：

```text
用户 new 配置对象
   └─ dataclass __init__ 给字段赋默认值
        └─ __post_init__ 校验字段、做单位换算
             └─ 传给 pipeline(model_path, backend_config=cfg)
                  └─ Pipeline.__init__ 选择后端（见 u2-l5）
                       └─ 交给 TurboMind / PyTorch 引擎内部 config 数据类
```

> 承接 [u1-l4](u1-l4-pipeline-quickstart.md)：`pipeline()` 的第二个位置参数 `backend_config` 接收的就是本讲这两个类之一；不传则由 `autoget_backend_config` 自动选择。

#### 4.1.3 源码精读

两个类都被导出为公开 API，所以你可以直接 `from lmdeploy import PytorchEngineConfig`：

[lmdeploy/\_\_init\_\_.py:4](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/__init__.py#L4-L4) —— 从 messages 导入两个配置类。

[lmdeploy/\_\_init\_\_.py:12](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/__init__.py#L12-L12) —— 写入 `__all__`，对外暴露。

两套配置都引用了一个关键枚举 `QuantPolicy`，它描述的是 **KV cache 的量化策略**（不是权重量化，详见 [u2-l2](u2-l2-generation-config.md)）：

[lmdeploy/messages.py:20-27](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/messages.py#L20-L27) —— `QuantPolicy` 取值：NONE=0、INT4=4、INT8=8、FP8=16、FP8_E5M2=17、TURBO_QUANT=42。

```python
class QuantPolicy(enum.IntEnum):
    NONE = 0
    INT4 = 4
    INT8 = 8
    FP8 = 16        # float8_e4m3fn
    FP8_E5M2 = 17
    TURBO_QUANT = 42  # K=4bit QJL4 + V=2bit MSE
```

#### 4.1.4 代码实践

**目标**：验证两个配置类都能正常构造，并观察 `__post_init__` 的校验行为。

**步骤**：

1. 在已安装 lmdeploy 的环境中运行下面这段「示例代码」。

```python
# 示例代码
from lmdeploy import PytorchEngineConfig, TurbomindEngineConfig

# 只写关心的字段，其余用默认值
pt = PytorchEngineConfig(tp=2, cache_max_entry_count=0.5)
print('PyTorch:', pt.tp, pt.cache_max_entry_count, pt.block_size)

tm = TurbomindEngineConfig(tp=2)
print('TurboMind:', tm.tp, tm.cache_max_entry_count, tm.cache_block_seq_len)

# 故意传非法值，观察 __post_init__ 如何拦截
try:
    bad = PytorchEngineConfig(cache_max_entry_count=1.5)
except AssertionError as e:
    print('被拦截:', e)
```

**需要观察的现象**：

- 前两行能正常打印出 `tp=2`、`cache_max_entry_count` 等字段。
- 第三段会抛出 `AssertionError: invalid cache_max_entry_count`，说明校验在构造阶段就生效了。

**预期结果**：合法配置可构造，非法配置在 `new` 的瞬间被拒。

**待本地验证**：若你没有 GPU，这一步仍可在纯 CPU 环境 import（`PytorchEngineConfig` 只是数据类，构造不依赖 GPU），`AssertionError` 部分一定能复现。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `TurbomindEngineConfig` 用 `pydantic_dataclass`，而 `PytorchEngineConfig` 用普通 `@dataclass`？至少说出一种可能的原因。

> **参考答案**：TurboMind 后端常用于服务化部署，配置需要被序列化（如写入 JSON、通过 CLI 传递），pydantic dataclass 提供更强的类型校验与序列化能力；PyTorch 后端更偏研究/灵活，普通 dataclass 足够且更轻。

**练习 2**：`QuantPolicy.FP8` 的整数值是多少？它量化的是什么？

> **参考答案**：值是 16，量化的是 **KV cache**（推理时存的历史 K/V 向量），不是模型权重。

---

### 4.2 TurbomindEngineConfig 字段精讲

#### 4.2.1 概念说明

`TurbomindEngineConfig` 是 **TurboMind 后端**（C++ 高性能引擎，见 [u1-l1](u1-l1-project-overview.md)）的配置类。它的字段可以分成五组：

1. **并行与拓扑**：`tp`、`dp`、`cp`、`nnodes`、`devices`……
2. **KV 缓存显存**：`cache_max_entry_count`、`cache_block_seq_len`、`enable_prefix_caching`。
3. **批处理与会话**：`max_batch_size`、`session_len`、`max_prefill_token_num`。
4. **量化与精度**：`dtype`、`model_format`、`quant_policy`。
5. **杂项**：`download_dir`、`revision`、`empty_init`、`enable_metrics` 等。

定义位置：[lmdeploy/messages.py:208-209](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/messages.py#L208-L209)。

#### 4.2.2 核心流程

TurboMind 引擎配置如何影响运行：

```text
TurbomindEngineConfig
   ├─ tp/dp/devices   → 决定用哪些卡、怎么切分权重
   ├─ cache_max_entry_count → 决定 KV cache 能吃多少「剩余显存」
   │     └─ cache_block_seq_len → 每个 KV block 装多少 token（默认 64）
   ├─ max_batch_size  → 单次 forward 最多多少条序列
   ├─ session_len     → 单条序列最大上下文长度（None 则用模型上限）
   ├─ enable_prefix_caching → 是否开启前缀复用（block 级别）
   └─ quant_policy    → KV cache 是否 int4/int8 量化
```

#### 4.2.3 源码精读

**并行字段**（[lmdeploy/messages.py:281-282](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/messages.py#L281-L282)）：`tp=1`、`dp=1` 是张量/数据并行的默认值。

```python
tp: int = 1
dp: int = 1
```

**会话与批处理**（[lmdeploy/messages.py:295-296](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/messages.py#L295-L296)）：`session_len=None` 表示沿用模型配置的最大长度；`max_batch_size=None` 表示让引擎按设备自动决定。

**KV 缓存核心字段**（[lmdeploy/messages.py:297-300](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/messages.py#L297-L300)）：

```python
cache_max_entry_count: float = 0.8     # 占「剩余显存」的比例
cache_chunk_size: int = -1
cache_block_seq_len: int = 64          # 每个 KV block 的 token 数
enable_prefix_caching: bool = False    # 是否开启前缀缓存
```

> 重点：`cache_max_entry_count` 默认 **0.8**。其 docstring（[L234-L243](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/messages.py#L234-L243)）说明：在 v0.2.1 之后，它表示「**模型权重加载后剩余的空闲显存**中划给 KV cache 的比例」。当传一个 **整数 > 0** 时，含义变为「KV block 的总数量」。这是 TurboMind 特有的二义写法。

**量化与 prefill**（[lmdeploy/messages.py:301](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/messages.py#L301-L301)、[L306](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/messages.py#L306-L306)）：`quant_policy=0` 默认不量化 KV；`max_prefill_token_num=8192` 限制每次 prefill 最多处理多少 token。

**校验逻辑** `__post_init__`（[lmdeploy/messages.py:317-334](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/messages.py#L317-L334)）做了几件关键的事：

```python
assert self.tp >= 1, 'tp must be a positive integer'
assert self.cache_max_entry_count > 0, 'invalid cache_max_entry_count'   # 允许小数或整数
...
assert self.quant_policy not in (QuantPolicy.FP8, QuantPolicy.FP8_E5M2), \
    'invalid quant_policy for TurboMind, FP8 quantization is not supported'
```

这正对应 [u2-l2](u2-l2-generation-config.md) 提到的结论：**TurboMind 拒绝 FP8 KV 量化**，只支持 int4/int8。

#### 4.2.4 代码实践

**目标**：阅读 `cache_max_entry_count` 的默认值，并理解它对显存的影响。

**步骤**：

1. 打开源码 [lmdeploy/messages.py:297](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/messages.py#L297-L297)，确认默认值 `0.8`。
2. 阅读其上方的 docstring（[L234-L243](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/messages.py#L234-L243)），记录三段语义：v0.2.0~v0.2.1 是「占总显存比例」、v0.2.1 后是「占剩余显存比例」、整数 > 0 是「KV block 总数」。
3. 运行下面「示例代码」自检：

```python
# 示例代码
from lmdeploy import TurbomindEngineConfig
print(TurbomindEngineConfig().cache_max_entry_count)   # 期望 0.8
```

**需要观察的现象**：打印结果为 `0.8`。

**预期结果**：默认值 0.8，含义是「模型权重加载后，把 80% 的剩余显存划给 KV cache」。

**待本地验证**：实际 KV 显存取决于具体 GPU 型号与模型，此处只验证配置语义。

#### 4.2.5 小练习与答案

**练习 1**：如果一台 80GB 显存的卡，模型权重占了 20GB，`cache_max_entry_count=0.8` 时 KV cache 大致能用多少？

> **参考答案**：剩余显存约 60GB，80% ≈ **48GB** 留给 KV cache（其余留给激活、临时张量等）。

**练习 2**：TurboMind 的 `quant_policy` 能否设成 `16`（FP8）？为什么？

> **参考答案**：不能。`__post_init__` 中明确断言拒绝 `QuantPolicy.FP8` 和 `FP8_E5M2`，会抛出 `AssertionError`。

---

### 4.3 PytorchEngineConfig 字段精讲

#### 4.3.1 概念说明

`PytorchEngineConfig` 是 **PyTorch 后端**（纯 Python，见 [u1-l1](u1-l1-project-overview.md)）的配置类。它比 TurboMind 配置多了不少字段，因为 PyTorch 后端更灵活、特性更前沿（MoE 专家并行、CUDA Graph、PD 分离角色等）。定义位置：[lmdeploy/messages.py:337-338](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/messages.py#L337-L338)。

字段同样可分五组：

1. **并行**：`tp`、`dp`、`ep`（专家并行）、`dp_rank`，以及混合并行的 `attn_tp_size`/`mlp_tp_size`/`moe_tp_size`。
2. **KV 缓存显存**：`cache_max_entry_count`、`block_size`、`num_gpu_blocks`、`num_cpu_blocks`、`enable_prefix_caching`。
3. **批处理**：`max_batch_size`、`session_len`、`max_prefill_token_num`、`prefill_interval`。
4. **执行优化**：`eager_mode`、`cudagraph_capture_batch_sizes`、`thread_safe`。
5. **进阶**：`role`（PD 分离角色）、`migration_backend`、`adapters`（LoRA）、`quant_policy`、`device_type`。

#### 4.3.2 核心流程

PyTorch 引擎配置如何映射到运行时（这些字段会被翻译成引擎内部 config 数据类，详见 u3-l2）：

```text
PytorchEngineConfig
   ├─ tp/dp/ep → PyTorch 分布式进程组（见 u9-l4）
   ├─ cache_max_entry_count → 「剩余显存」比例（必须是 (0,1) 小数！）
   │     ├─ block_size (64) → 分页缓存块大小
   │     └─ num_gpu_blocks / num_cpu_blocks → 手动指定块数（0=自动）
   ├─ enable_prefix_caching → BlockTrie 前缀命中（见 u9-l3）
   ├─ eager_mode / cudagraph_capture_batch_sizes → 是否用 CUDA Graph 加速
   ├─ role → Hybrid / Prefill / Decode（PD 分离，见 u9-l5）
   └─ device_type → cuda/ascend/maca/camb
```

#### 4.3.3 源码精读

**并行字段**（[lmdeploy/messages.py:426-429](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/messages.py#L426-L429)）：

```python
tp: int = 1
dp: int = 1
dp_rank: int = 0
ep: int = 1        # 专家并行，仅 PyTorch 后端有
```

**KV 缓存字段**（[lmdeploy/messages.py:435-440](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/messages.py#L435-L440)）：

```python
cache_max_entry_count: float = 0.8   # 剩余显存比例
prefill_interval: int = 16
block_size: int = 64                 # 分页缓存块大小
kernel_block_size: int = -1          # -1 表示跟随 block_size
num_cpu_blocks: int = 0              # 0 = 自动
num_gpu_blocks: int = 0              # 0 = 自动
```

**关键差异**：PyTorch 后端的 `cache_max_entry_count` 校验更严格——**必须是 (0,1) 区间内的小数**，不接受 TurboMind 那种「整数表示块数」的写法。见 [lmdeploy/messages.py:486-487](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/messages.py#L486-L487)：

```python
assert 0 < self.cache_max_entry_count < 1, \
    'invalid cache_max_entry_count'
```

**前缀缓存**（[lmdeploy/messages.py:445](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/messages.py#L445-L445)）：`enable_prefix_caching: bool = False`，开启后基于 `BlockTrie` 做 token 级前缀命中（详见 u9-l3）。

**执行优化**（[lmdeploy/messages.py:449](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/messages.py#L449-L449)）：`eager_mode: bool = False`，关闭后默认启用 CUDA Graph 加速 decode；`cudagraph_capture_batch_sizes`（[L443](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/messages.py#L443-L443)）指定要捕获的 batch size 列表。

**PD 分离角色**（[lmdeploy/messages.py:475-476](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/messages.py#L475-L476)）：

```python
role: EngineRole = EngineRole.Hybrid
migration_backend: MigrationBackend = MigrationBackend.DLSlime
```

这两个枚举定义在 [lmdeploy/pytorch/disagg/config.py:21-42](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/disagg/config.py#L21-L42)。`role` 默认 `Hybrid`（prefill/decode 同处一个引擎），可设为 `Prefill` 或 `Decode` 用于 PD 分离部署（详见 u9-l5）。

**设备校验**（[lmdeploy/messages.py:498](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/messages.py#L498-L498)）：`device_type` 仅允许 `cuda/ascend/maca/camb`。KV 量化只支持 cuda 与 ascend（[L510-L512](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/messages.py#L510-L512)），承接 [u2-l2](u2-l2-generation-config.md) 的结论。

#### 4.3.4 代码实践（本讲主实践）

**目标**：构造 `PytorchEngineConfig(tp=2, cache_max_entry_count=0.5)`，逐字段解释对显存的影响，并确认 `cache_max_entry_count` 的默认值。

**步骤**：

1. 运行下面这段「示例代码」：

```python
# 示例代码
from lmdeploy import PytorchEngineConfig

cfg = PytorchEngineConfig(tp=2, cache_max_entry_count=0.5)
print('默认 cache_max_entry_count =', PytorchEngineConfig().cache_max_entry_count)  # 0.8
print('本例 cache_max_entry_count =', cfg.cache_max_entry_count)                  # 0.5
print('tp =', cfg.tp, '| block_size =', cfg.block_size, '| role =', cfg.role)
```

2. 对照下表，逐字段解释显存影响。

**字段对显存的影响**：

| 字段 | 本例取值 | 对显存的影响 |
| --- | --- | --- |
| `tp=2` | 2 | 权重与 KV cache 都切分到 2 张卡，**单卡显存需求减半**（但卡间通信增加）。 |
| `cache_max_entry_count=0.5` | 0.5 | 只把「剩余显存的 50%」划给 KV cache（默认 0.8）。**降低它 → KV 容量变小、可并发序列变少，但给其他程序留更多显存**；升高则相反。 |
| `block_size`（默认 64） | 64 | KV 显存总量不变，只改变块粒度；块越小越灵活、管理开销略增。 |
| `num_gpu_blocks=0` | 0（自动） | 引擎根据 `cache_max_entry_count` 自动算出 block 数；若手动设非 0 值，则覆盖比例策略。 |

**需要观察的现象**：第一行打印 `0.8`（默认值），第二行打印 `0.5`（本例）。

**预期结果**：成功构造配置；`cache_max_entry_count` 默认值确认为 `0.8`。

**待本地验证**：显存实际占用需在有 GPU 的机器上用 `nvidia-smi` 观察；本实践只验证配置语义。

#### 4.3.5 小练习与答案

**练习 1**：`PytorchEngineConfig(cache_max_entry_count=2)` 能构造成功吗？

> **参考答案**：不能。PyTorch 后端要求 `0 < cache_max_entry_count < 1`（[L486-L487](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/messages.py#L486-L487)），`2` 会被 `AssertionError` 拦截。（对比 TurboMind 可以传整数表示块数。）

**练习 2**：要让一个 PyTorch 引擎只做 prefill 阶段（PD 分离场景），该改哪个字段？

> **参考答案**：把 `role` 从默认 `EngineRole.Hybrid` 改为 `EngineRole.Prefill`（配合 `migration_backend` 指定 KV 迁移后端）。

**练习 3**：`ep` 字段是干什么的？为什么 TurboMind 配置里没有？

> **参考答案**：`ep` 是专家并行（Expert Parallelism），用于 MoE 模型把专家分到多卡。TurboMind 的 MoE 并行策略在配置层面不暴露 `ep`，而 PyTorch 后端把它作为一等公民字段。

---

### 4.4 两套配置的对比与选型

#### 4.4.1 共有字段与差异字段

| 字段类别 | 共有（语义基本一致） | 仅 TurboMind | 仅 PyTorch |
| --- | --- | --- | --- |
| 并行 | `tp`、`dp`、`devices` | `cp`（上下文并行）、`nnodes`、`node_rank` | `ep`、`dp_rank`、`attn/mlp/moe_tp_size` |
| 显存 | `cache_max_entry_count`、`enable_prefix_caching` | `cache_block_seq_len`、`cache_chunk_size` | `block_size`、`num_gpu_blocks`、`num_cpu_blocks` |
| 批处理 | `max_batch_size`、`session_len`、`max_prefill_token_num` | `num_tokens_per_iter`、`max_prefill_iters` | `prefill_interval`、`cudagraph_capture_batch_sizes` |
| 量化/精度 | `dtype`、`quant_policy` | `model_format` 更丰富（awq/gptq/fp8/mxfp4） | `quant_policy` 支持 FP8 |
| 进阶 | `download_dir`、`revision`、`empty_init`、`enable_metrics` | `rope_scaling_factor`、`use_logn_attn`、`async_` | `eager_mode`、`role`、`adapters`(LoRA)、`enable_eplb` |

#### 4.4.2 cache_max_entry_count 的关键差异

这是最容易踩坑的字段，务必记住：

| | TurboMind | PyTorch |
| --- | --- | --- |
| 默认值 | 0.8 | 0.8 |
| 校验 | `> 0`（[L321](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/messages.py#L321-L321)） | `0 < x < 1`（[L486](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/messages.py#L486-L487)） |
| 整数含义 | 整数 > 0 = **KV block 总数** | 不接受整数 |
| 语义 | v0.2.1 后为「剩余显存比例」 | 「剩余显存比例」 |

#### 4.4.3 quant_policy 的差异

承接 [u2-l2](u2-l2-generation-config.md)：TurboMind 的 `__post_init__` 显式拒绝 `FP8`/`FP8_E5M2`（[L326-L329](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/messages.py#L326-L329)），PyTorch 后端则支持（但要求 device 是 cuda/ascend，[L510-L512](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/messages.py#L510-L512)）。

#### 4.4.4 选型建议

- **追求极致吞吐、模型已被 TurboMind 支持**：用 `TurbomindEngineConfig`，开 `enable_prefix_caching=True`，按需调 `cache_max_entry_count`。
- **需要 FP8 KV 量化 / MoE 专家并行 / LoRA / PD 分离 / 自定义算子**：用 `PytorchEngineConfig`，它特性更全。
- **显存吃紧**：降低 `cache_max_entry_count`，或开 `quant_policy=4/8`（KV 量化），或增大 `tp`。

## 5. 综合实践

把本讲知识串起来：为一个「8B 模型、单机 2 卡、希望尽量多并发、并复用 system prompt」的场景，分别写出 TurboMind 与 PyTorch 两套配置，并说明每个字段的选择理由。

**示例答案（示例代码）**：

```python
# 示例代码
from lmdeploy import TurbomindEngineConfig, PytorchEngineConfig

# TurboMind 版：tp=2 切权重到两卡，开前缀缓存复用 system prompt
tm_cfg = TurbomindEngineConfig(
    tp=2,
    cache_max_entry_count=0.8,   # 80% 剩余显存给 KV，最大化并发
    enable_prefix_caching=True,  # 复用相同前缀的 block
    max_batch_size=64,           # 限制单次 forward 批大小
)

# PyTorch 版：同样 tp=2，开 BlockTrie 前缀缓存
pt_cfg = PytorchEngineConfig(
    tp=2,
    cache_max_entry_count=0.8,
    enable_prefix_caching=True,
    max_batch_size=64,
)
```

**自检要点**：

1. 解释为什么 `tp=2` 能降低单卡显存（权重 + KV 都切分）。
2. 解释 `enable_prefix_caching=True` 在「相同 system prompt 反复请求」时省的是什么（省掉前缀部分的 prefill 计算，命中已有 KV block）。
3. 思考：如果把 `cache_max_entry_count` 调到 `0.95`，会有什么风险？（留给激活/临时张量的显存过少，可能 OOM。）

## 6. 本讲小结

- `GenerationConfig` 管采样、`TurbomindEngineConfig` / `PytorchEngineConfig` 管引擎，二者职责分明、分别在不同阶段确定。
- 两套引擎配置都用 `@dataclass` + `__post_init__` 在构造时校验字段，TurboMind 额外用 pydantic 增强序列化与校验。
- `tp`/`dp`/`ep` 控制并行度，`cache_max_entry_count`（默认 **0.8**）控制「剩余显存中划给 KV cache 的比例」，是调节显存/并发权衡的核心旋钮。
- TurboMind 的 `cache_max_entry_count` 可传整数表示「KV block 总数」，PyTorch 后端严格要求 `(0,1)` 小数——这是最易踩的差异。
- `quant_policy` 描述 **KV cache** 量化：TurboMind 拒 FP8，PyTorch 支持（限 cuda/ascend）。
- PyTorch 配置更「前沿」：多出 `ep`、`eager_mode`、CUDA Graph、LoRA `adapters`、PD 分离 `role` 等字段。

## 7. 下一步学习建议

- 想知道配置如何决定「走哪个后端」？继续读 [u2-l5 架构注册与后端自动选择](u2-l5-arch-registry-and-backend-selection.md) 与 [u3-l1 Pipeline 如何选择并实例化后端](u3-l1-pipeline-backend-selection.md)。
- 想看这些用户面字段如何翻译成引擎内部数据类？读 [u3-l2 PyTorch 引擎配置数据类 config.py](u3-l2-pytorch-config-dataclasses.md)（`ModelConfig`/`CacheConfig`/`SchedulerConfig`）。
- 想深入 `block_size` / `num_gpu_blocks` 背后的物理块管理？读 [u4-l5 分块 KV 缓存与 BlockManager](u4-l5-block-manager-kv-cache.md)。
- 想理解 `enable_prefix_caching` 的实现？读 [u9-l3 Prefix 缓存与 BlockTrie](u9-l3-prefix-cache-blocktrie.md)。
- 想了解 PD 分离 `role` 的全貌？读 [u9-l5 PD 分离部署 disagg](u9-l5-pd-disaggregation.md)。
