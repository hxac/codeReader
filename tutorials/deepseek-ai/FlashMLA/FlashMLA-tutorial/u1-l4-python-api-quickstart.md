# Python 接口与最小运行示例

## 1. 本讲目标

学完本讲，你应当能够：

- 说出 `flash_mla` 这个 Python 包**导出了哪些函数**，以及它们各自对应哪一类 kernel（dense decode / sparse decode / sparse prefill / dense MHA prefill）。
- 理解 `FlashMLASchedMeta` 这个调度元数据对象「**首次调用时初始化、后续调用复用**」的设计模式，并知道哪些参数在多次调用间必须保持一致。
- 看懂 `flash_mla_with_kvcache` 的完整参数签名（张量形状、dtype、`head_dim_v` 约束等），并能照着 `tests/test_flash_mla_dense_decoding.py` 拼出一个最小可运行的解码脚本。
- 区分三条不同的 Python 入口：解码 `flash_mla_with_kvcache`、稀疏 prefill `flash_mla_sparse_fwd`、稠密 MHA prefill `flash_attn_varlen_func` 系列。

本讲只讲 **Python 接口层**（`flash_mla/`），不进入 `csrc/` 的 CUDA 实现——那是第 2 单元之后的内容。

## 2. 前置知识

在阅读本讲前，你应当已经了解（来自 u1-l1 / u1-l3）：

- **MLA 的两种模式**：MQA 模式（`head_dim_k = 576`、`head_dim_v = 512`，用于 decode 与 sparse prefill）与 MHA 模式（`head_dim_k` / `head_dim_v` 为 128，用于 dense prefill）。
- **支持矩阵的非对称**：Dense Decoding 仅 SM90、Dense Prefill 仅 SM100、Sparse 两架构都有。
- **目录结构**：`flash_mla/` 是面向用户的纯 Python 壳，真正的算力在 `csrc/`；`flash_mla.cuda` 是 pybind 编译出的扩展模块。

本讲会用到几个术语，先在这里统一解释：

- **Paged KV cache（分页 KV 缓存）**：把一条长序列的 KV 切成固定大小的「页（page block）」，用一个 `block_table` 记录「逻辑块号 → 物理块号」的映射。这样不同请求可以共享一块大显存池，避免为每条请求连续分配。
- **varlen（变长）**：把一个 batch 里多条长度不一的序列**拼接**成一条长张量，再用一个累加长度数组 `cu_seqlens` 标记每条序列的起止位置。这是 FlashAttention 系列处理变长 batch 的标准做法。
- **lse（log-sum-exp）**：attention 分数在 softmax 前的归一化常数 `log(Σ exp(score))`，返回它便于后续做 Split-KV 合并等操作。

## 3. 本讲源码地图

本讲只涉及两个 Python 文件和一份文档：

| 文件 | 作用 |
| :--- | :--- |
| [flash_mla/__init__.py](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/flash_mla/__init__.py) | 包入口，决定 `from flash_mla import ...` 能拿到哪些名字。 |
| [flash_mla/flash_mla_interface.py](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/flash_mla/flash_mla_interface.py) | 全部用户接口的实现：参数校验、`FlashMLASchedMeta` 管理、向 `flash_mla.cuda` 的 C++ 扩展转发。 |
| [README.md](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/README.md) | 官方用法说明（注意：其中的代码片段部分已过时，本讲会指出）。 |

实践环节会参照 [tests/test_flash_mla_dense_decoding.py](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/test_flash_mla_dense_decoding.py) 中的 `generate_test_data`。

> **架构边界提示**：`flash_mla_interface.py` 里所有函数最终都调用 `flash_mla.cuda.xxx`（如 `dense_decode_fwd`、`sparse_decode_fwd`、`sparse_prefill_fwd`、`dense_prefill_fwd`、`dense_prefill_bwd`）。这些 `flash_mla.cuda.*` 是 pybind 绑定出来的 C++ 函数，属于下一讲（u2-l1）的调用链内容，本讲只需把它们当成「黑盒 kernel 入口」即可。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **包导出 `__init__`**——门面层，决定对外暴露哪些函数。
2. **解码接口 `flash_mla_with_kvcache` 与 `FlashMLASchedMeta`**——本讲重点，含「首次初始化、后续复用」模式。
3. **sparse prefill 与 dense MHA varlen 接口**——另两条相对独立的入口。

---

### 4.1 包导出 `__init__`

#### 4.1.1 概念说明

`flash_mla/__init__.py` 是整个包的「门面（facade）」。当用户写 `import flash_mla` 或 `from flash_mla import flash_mla_with_kvcache` 时，Python 解释器执行的就是这个文件。它的职责很单一：把 `flash_mla_interface.py` 里实现好的几个函数**重新导出**到包的顶层，让用户不必写全 `flash_mla.flash_mla_interface.xxx` 这么长的路径。

一个 Python 包是否「好用」，很大程度上取决于 `__init__.py` 暴露的 API 是否简洁、是否有清晰的 `__all__`。

#### 4.1.2 核心流程

1. 声明版本号 `__version__`。
2. 从子模块 `flash_mla.flash_mla_interface` 导入若干函数。
3. 用 `__all__` 显式列出公开名字（便于 `from flash_mla import *` 与文档工具识别）。

#### 4.1.3 源码精读

整个文件非常短：

[flash_mla/__init__.py:1-19](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/flash_mla/__init__.py#L1-L19)：声明版本号、从 `flash_mla_interface` 导入 6 个函数、用 `__all__` 列出其中 6 个公开名字。

```python
__version__ = "1.0.0"

from flash_mla.flash_mla_interface import (
    get_mla_metadata,
    flash_mla_with_kvcache,
    flash_attn_varlen_func,
    flash_attn_varlen_qkvpacked_func,
    flash_attn_varlen_kvpacked_func,
    flash_mla_sparse_fwd
)
```

把这 6 个名字按「对应哪类 kernel」归类，就能一眼看清 FlashMLA 的对外能力：

| 导出函数 | 对应 kernel 家族 | 典型场景 |
| :--- | :--- | :--- | 
| `get_mla_metadata` | （辅助）生成解码调度元数据 | 解码前调用一次 |
| `flash_mla_with_kvcache` | Dense / Sparse **Decoding** | 自回归生成每一步 |
| `flash_mla_sparse_fwd` | Sparse **Prefill**（token 级稀疏） | DSA 稀疏注意力的 prefill |
| `flash_attn_varlen_func` | Dense **MHA Prefill**（前向 + 反向） | 标准 MHA，类似 `flash_attn` 包 |
| `flash_attn_varlen_qkvpacked_func` | 同上（qkv 打包变体） | q/k/v 拼成一个张量时 |
| `flash_attn_varlen_kvpacked_func` | 同上（kv 打包变体） | k/v 拼成一个张量时 |

可以看到，导出清单正好对应 u1-l1 讲过的「四类 kernel」：解码（dense+sparse 共用一个函数，靠参数区分）、sparse prefill、dense prefill（三种打包形式）。

#### 4.1.4 代码实践

**目标**：确认你能在本地把包导入并打印导出名字。

**步骤**：

1. 按 u1-l2 完成编译安装（`pip install -v .`），确保 `flash_mla.cuda` 扩展已生成。
2. 在仓库根目录启动 Python，执行：

```python
import flash_mla
print(flash_mla.__version__)
print([n for n in dir(flash_mla) if not n.startswith("_")])
```

**预期结果**：第一行打印 `1.0.0`；第二行应包含上表中的 6 个函数名。

3. 如果环境**没有 GPU**（无法编译 `flash_mla.cuda`），`import flash_mla` 会在 [flash_mla/flash_mla_interface.py:6](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/flash_mla/flash_mla_interface.py#L6) 的 `import flash_mla.cuda as flash_mla_cuda` 处失败。此时本步骤**待本地验证**——你只能阅读源码，无法真正 import。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `__all__` 里没有 `FlashMLASchedMeta` 这个类？它不是解码必需的吗？

> **参考答案**：`FlashMLASchedMeta` 是**返回值类型**，用户不需要直接构造它——`get_mla_metadata()` 会帮你创建。因此它被刻意排除在公开导出之外，减少用户需要关心的名字；想用类型注解的用户仍可通过 `flash_mla.flash_mla_interface.FlashMLASchedMeta` 访问。

**练习 2**：`flash_attn_varlen_qkvpacked_func` 与 `flash_attn_varlen_func` 的本质区别是什么？

> **参考答案**：前者假设 q/k/v 已经被**打包进同一个张量** `qkv`（沿 head_dim 维拼接），内部会把它切回 q、k、v 三份再调用底层；后者要求用户**分别传入** q、k、v 三个张量。底层都走同一个 `FlashAttnVarlenFunc`，只是入参布局不同。

---

### 4.2 解码接口 `flash_mla_with_kvcache` 与 `FlashMLASchedMeta`

这是本讲的核心。理解它，就理解了 FlashMLA 解码阶段的全部用法。

#### 4.2.1 概念说明

解码（decoding）是自回归生成的每一步：每产生一个新 token，就用它的 q 去和**整条历史的 KV cache** 做注意力。这个过程有两个特点：

1. **KV cache 很长**（几千到几万 token），需要把它切分成多段并行处理（Split-KV，见 u4）。
2. **每一步的形状高度相似**——batch 大小、q 序列长度、头数、KV 分页大小在连续多步里几乎不变。

第 2 点引出了一个关键设计：**调度元数据（tile scheduler metadata）**。它描述了「把 batch 的请求/块均衡地切给若干 SM part」的方案（详见 u4-l3）。这个方案只依赖于上面那些「每步基本不变」的形状，所以可以**算一次、复用多步**，省去每步都重新规划的 CPU 开销。

FlashMLA 用 `FlashMLASchedMeta` 这个对象承载这套机制，并采用「**首次调用初始化、后续调用复用**」的模式：

- 你先调 `get_mla_metadata()` 拿到一个**空壳** `FlashMLASchedMeta`（内部 `have_initialized=False`）。
- 第一次调 `flash_mla_with_kvcache` 时，kernel 发现还没初始化，就**顺手**把调度元数据生成出来，写回这个对象。
- 之后的每一步，kernel **直接复用**对象里已有的元数据，跳过生成步骤。

#### 4.2.2 核心流程

解码循环的伪代码如下：

```
sched_meta, num_splits = get_mla_metadata()   # 空壳，num_splits 恒为 None
for step in decoding_loop:
    q = ...                                    # (b, s_q, h_q, d)
    out, lse = flash_mla_with_kvcache(
        q, k_cache, block_table, cache_seqlens, head_dim_v,
        sched_meta, num_splits,                # 同一个 sched_meta 反复传
        ...
    )
```

进入 `flash_mla_with_kvcache` 后，函数本体做四件事：

1. **派生少量参数**：从 `indices` 推 `topk`、从 `extra_k_cache` 推 `extra_topk`、给 `softmax_scale` 兜底默认值。
2. **首次初始化 / 后续一致性校验**：根据 `sched_meta.have_initialized` 二选一。
3. **按是否传 `indices` 二分派发**：有 `indices` → sparse decode；否则 → dense decode。两条路各调一个 `flash_mla.cuda.*` 绑定。
4. **把 kernel 新生成的元数据写回** `sched_meta`，返回 `(out, lse)`。

#### 4.2.3 源码精读

**(a) `FlashMLASchedMeta` 的结构**

[flash_mla/flash_mla_interface.py:8-34](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/flash_mla/flash_mla_interface.py#L8-L34)：定义了 `FlashMLASchedMeta` 与其内嵌的 `Config`。

```python
@dataclasses.dataclass
class FlashMLASchedMeta:
    @dataclasses.dataclass
    class Config:
        b: int; s_q: int; h_q: int; page_block_size: int; h_k: int
        causal: bool; is_fp8_kvcache: bool; topk: Optional[int]
        extra_page_block_size: Optional[int]; extra_topk: Optional[int]

    have_initialized: bool = False
    config: Optional[Config] = None
    tile_scheduler_metadata: Optional[torch.Tensor] = None  # (num_sm_parts, ...), int32
    num_splits: Optional[torch.Tensor] = None               # (1,), int32
```

注意 `Config` 记录的正是「多次调用间必须保持一致」的那组形状/开关；`tile_scheduler_metadata` 与 `num_splits` 是 kernel 写回的实际张量。

**(b) `get_mla_metadata`：返回空壳**

[flash_mla/flash_mla_interface.py:37-50](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/flash_mla/flash_mla_interface.py#L37-L50)：函数签名是 `(*args, **kwargs)`，但**实际上不使用任何参数**，直接返回一个空的 `FlashMLASchedMeta()`。

```python
def get_mla_metadata(*args, **kwargs) -> Tuple[FlashMLASchedMeta, None]:
    return FlashMLASchedMeta(), None
```

> ⚠️ **重要提醒**：README 的「Usage」代码片段里写的是 `get_mla_metadata(cache_seqlens, s_q * h_q // h_kv, h_kv, h_q, is_fp8, topk)`——这是**旧版接口**的写法，已过时。当前 HEAD 的真实签名不需要任何参数。保留 `*args, **kwargs` 只是为了「向后兼容老调用代码」不报错。以 `tests/test_flash_mla_dense_decoding.py:153` 的真实用法为准：

```python
tile_scheduler_metadata, num_splits = flash_mla.get_mla_metadata()   # 无参！
```

**(c) 参数派生与默认 softmax_scale**

[flash_mla/flash_mla_interface.py:109-113](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/flash_mla/flash_mla_interface.py#L109-L113)：从 `indices` 的最后一维推出 `topk`，并给 `softmax_scale` 一个默认值。

```python
topk = indices_in_kvcache.shape[-1] if indices_in_kvcache is not None else None
...
if softmax_scale is None:
    softmax_scale = q.shape[-1] ** (-0.5)
```

默认缩放即标准 attention 的 \( \text{scale} = \frac{1}{\sqrt{d}} \)，其中 \( d \) 是 q 的 head_dim（DeepSeek-V3 为 576）。注意这里取的是 `q.shape[-1]`（576），而不是 `head_dim_v`（512）。

**(d) 首次初始化 vs 后续一致性校验**

[flash_mla/flash_mla_interface.py:115-149](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/flash_mla/flash_mla_interface.py#L115-L149)：这是「首次初始化、后续复用」模式的实现核心。

- **首次**（`not have_initialized`）：做一次轻量 sanity check（sparse 时禁止 causal），然后把当前形状/开关**快照**进 `sched_meta.config`，置 `have_initialized = True`。真正生成元数据张量的工作由随后调用的 kernel 完成。

```python
if not sched_meta.have_initialized:
    if indices_in_kvcache is not None:
        assert not causal, "causal must be False when sparse ..."
    sched_meta.have_initialized = True
    sched_meta.config = FlashMLASchedMeta.Config(
        q.shape[0], q.shape[1], q.shape[2], k_cache.shape[1], k_cache.shape[2],
        causal, is_fp8_kvcache, topk, extra_k_page_block_size, extra_topk,
    )
```

- **后续**：逐项断言当前参数与首次快照的 `config` 一致，任何一项不符都会抛出带提示的 `AssertionError`。这解释了 docstring 里那句「You may reuse the same sched_meta across different invocations, but only when the tensor shapes and the values of cache_seqlens, topk_length, and extra_topk_length remain the same」。

```python
else:
    assert sched_meta.config.b == q.shape[0], "..."
    assert sched_meta.config.s_q == q.shape[1], "..."
    # ... 其余字段同理
```

> **设计直觉**：把「形状不变」的假设从隐式约定变成显式断言，能在第一时间把「形状变了却没重新建 meta」的 bug 暴露出来，而不是让 kernel 跑出难以解释的错误结果。

**(e) 二分派发：sparse vs dense**

[flash_mla/flash_mla_interface.py:151-170](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/flash_mla/flash_mla_interface.py#L151-L170)：根据是否传入 `indices` 选择两条不同的 kernel 路径。

```python
if topk is not None:                       # Sparse attention
    assert not causal
    assert is_fp8_kvcache
    out, lse, new_meta, new_splits = flash_mla_cuda.sparse_decode_fwd(...)
else:                                      # Dense attention
    assert indices_in_kvcache is None and attn_sink is None and ...
    assert block_table is not None and cache_seqlens is not None
    out, lse, new_meta, new_splits = flash_mla_cuda.dense_decode_fwd(...)
```

两个关键约束值得记住：

- **sparse 解码必须用 FP8 KV cache**（`is_fp8_kvcache` 必须为 `True`）。这与 u1-l1 讲的「Sparse Decoding 采用 FP8 KV cache」一致。
- **dense 解码必须提供 `block_table` 与 `cache_seqlens`**；sparse 解码则不需要 `block_table`（分页信息已编码进 `indices`）。

**(f) 写回元数据并返回**

[flash_mla/flash_mla_interface.py:171-173](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/flash_mla/flash_mla_interface.py#L171-L173)：把 kernel 新算出的元数据写回对象，供下一步复用；返回 `(out, lse)`。

```python
sched_meta.tile_scheduler_metadata = new_tile_scheduler_metadata
sched_meta.num_splits = new_num_splits
return (out, lse)
```

#### 4.2.4 代码实践

**目标**：参照 `tests/test_flash_mla_dense_decoding.py` 的 `generate_test_data`，构造 `q / block_table / blocked_k / cache_seqlens` 并调用 `flash_mla_with_kvcache`（dense decode），打印输出形状。

**步骤**：

1. 阅读测试里的数据构造逻辑：[tests/test_flash_mla_dense_decoding.py:29-70](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/test_flash_mla_dense_decoding.py#L29-L70)。重点关注四个张量的形状：`cache_seqlens (b,)`、`q (b, s_q, h_q, d)`、`block_table (b, max_num_blocks_per_seq)`、`blocked_k (num_blocks, block_size, h_kv, d)`。
2. 把下面的最小脚本存为 `minimal_dense_decode_demo.py`（放在仓库根目录即可）：

```python
# 示例代码：minimal_dense_decode_demo.py
# 参照 tests/test_flash_mla_dense_decoding.py 的 generate_test_data，
# 构造最小输入并调用 dense decode kernel。
import math
import torch
import flash_mla

# ---- 1. 配置（DeepSeek-V3 典型 MLA decode 配置）----
b, s_q, s_k = 2, 1, 4096
h_q, h_kv   = 128, 1     # MQA：kv 头数为 1
d, dv       = 576, 512   # Q/K head_dim 含 64 维 RoPE；V head_dim 必须为 512
block_size  = 64

# ---- 2. cache_seqlens: (b,) int32 ----
cache_seqlens = torch.full((b,), s_k, dtype=torch.int32, device="cuda")

# ---- 3. q: (b, s_q, h_q, d) bf16 ----
q = (torch.randn(b, s_q, h_q, d, dtype=torch.bfloat16, device="cuda") / 10).clamp(-1, 1)

# ---- 4. paged KV cache：block_table + blocked_k ----
max_seqlen_pad = math.ceil(s_k / 256) * 256                 # 对齐到 256（仿照测试的 kk.cdiv 风格）
num_blocks_per_seq = max_seqlen_pad // block_size
# 这里简化为顺序映射（测试里会做 randperm 打散，模拟真实分页池）
block_table = torch.arange(b * num_blocks_per_seq, dtype=torch.int32, device="cuda").view(b, -1)
blocked_k = (torch.randn(block_table.numel(), block_size, h_kv, d, dtype=torch.bfloat16, device="cuda") / 10).clamp(-1, 1)

# ---- 5. 调度元数据（空壳，首次调用 kernel 时填充）----
tile_scheduler_metadata, num_splits = flash_mla.get_mla_metadata()   # 注意：当前版本无需传参

# ---- 6. 调用解码 kernel ----
out, lse = flash_mla.flash_mla_with_kvcache(
    q, blocked_k, block_table, cache_seqlens, dv,
    tile_scheduler_metadata, num_splits,
    causal=False,
)

print("out.shape:", tuple(out.shape))   # 期望 (2, 1, 128, 512)
print("lse.shape:", tuple(lse.shape))   # 期望 (2, 128, 1)
```

**需要观察的现象**：

- `out` 的形状为 `(b, s_q, h_q, head_dim_v) = (2, 1, 128, 512)`，与 [flash_mla_interface.py:101](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/flash_mla/flash_mla_interface.py#L101) 的 docstring 一致。
- `lse` 的形状为 `(b, h_q, s_q) = (2, 128, 1)`，注意头维 `h_q` 在序列维 `s_q` 之前。
- 把 `out, lse` 与测试里的 `reference_torch`（[tests/test_flash_mla_dense_decoding.py:73-141](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/test_flash_mla_dense_decoding.py#L73-L141)）对照，应在 `abs_tol=8e-4` 量级内一致（测试用的是 `kk.check_is_allclose`）。

**预期结果 / 无 GPU 情形**：

- 在 SM90（Hopper）+ bf16 下应能正常打印上述两个形状。该 kernel 仅支持 sm90，测试里有断言 `cc_major == 9`（[test_flash_mla_dense_decoding.py:201-202](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/test_flash_mla_dense_decoding.py#L201-L202)）。
- **若环境没有 GPU**：上面的脚本无法运行（`import flash_mla` 与 `"cuda"` 张量都会失败）。此时可把 `device="cuda"` 全部改成 `device="cpu"`、并**注释掉第 5、6 步对 kernel 的调用**，仅验证四个输入张量的形状构造是否正确——这一部分**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：如果把第二次调用的 `cache_seqlens` 改成不同的值（但仍传同一个 `sched_meta`），会发生什么？为什么？

> **参考答案**：会触发 `AssertionError`。注意 `cache_seqlens` 的**值**并不在 `Config` 里（`Config` 只存形状/开关），所以改变其值不会在 Python 层被断言拦下；但调度元数据（tile scheduler metadata）是在第一次调用时按当时的 `cache_seqlens` 生成的，后续复用就会导致 kernel 用错误的切分方案处理新长度。docstring 因此明确要求 `cache_seqlens` 的值也必须保持一致——这是「逻辑约定」而非「代码强制」。

**练习 2**：为什么 `flash_mla_with_kvcache` 的签名里 `num_splits` 参数被标注成 `None = None`，并且函数体里 `assert num_splits is None`？

> **参考答案**：这是为兼容旧接口保留的位置参数。旧版本里 `num_splits` 由 `get_mla_metadata` 计算后传入；新版本里它完全不起作用（真实值由 kernel 写回 `sched_meta.num_splits`）。保留参数位置但强制为 `None`，既能让老代码 `flash_mla_with_kvcache(..., num_splits)` 不报签名错误，又能确保没人误传非 None 值。

**练习 3**：解码时 `head_dim_v` 为什么必须是 512？它和 q 的 head_dim（576）不一致是怎么回事？

> **参考答案**：MLA 的 V 只取 KV 潜在向量的前 512 维（不含 64 维 RoPE），所以 q/k 的 head_dim 是 576（512 NoPE + 64 RoPE），而 V 的 head_dim 是 512。kernel 内部针对这个固定维度做了特化，因此 docstring 写明 `head_dim_v: Must be 512`。

---

### 4.3 sparse prefill 与 dense MHA varlen 接口

前两个模块讲的都是「解码」。FlashMLA 还有两类入口：稀疏 prefill 与稠密 MHA prefill。它们的接口风格与解码很不一样——**不需要 `FlashMLASchedMeta`**，调用更「直接」。

#### 4.3.1 概念说明

- **`flash_mla_sparse_fwd`（Sparse Prefill）**：实现 token 级稀疏注意力，服务于 DeepSeek Sparse Attention（DSA）。每个 q token 只去 attend `indices` 指定的若干个 KV token，而不是整条序列。注意它**没有 batch 维**——多 batch 要靠把序列拼起来 + 调整 indices 模拟。
- **`flash_attn_varlen_func` 系列（Dense MHA Prefill）**：实现标准的稠密多头注意力前向 + 反向，用法类似开源 `flash_attn` 包，用 `cu_seqlens` 处理变长 batch。它是 FlashMLA 里唯一带 `torch.autograd.Function`（支持反向传播）的接口，主要用于训练 / prefill。

#### 4.3.2 核心流程

**Sparse prefill**：直接把 `q / kv / indices / sm_scale` 传进去，返回 `(output, max_logits, lse)`。

```
out, max_logits, lse = flash_mla_sparse_fwd(q, kv, indices, sm_scale, d_v=512)
```

**Dense MHA prefill**：用 varlen 拼接格式，可前可反。

```
out, lse = flash_attn_varlen_func(q, k, v, cu_seqlens_qo, cu_seqlens_kv,
                                  max_seqlen_qo, max_seqlen_kv, causal=...)
# out 参与 loss 后，反向会自动走 FlashAttnVarlenFunc.backward
```

#### 4.3.3 源码精读

**(a) `flash_mla_sparse_fwd`**

[flash_mla/flash_mla_interface.py:176-211](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/flash_mla/flash_mla_interface.py#L176-L211)：一个很薄的包装，几乎只做参数透传。

```python
def flash_mla_sparse_fwd(q, kv, indices, sm_scale, d_v=512, attn_sink=None, topk_length=None):
    results = flash_mla_cuda.sparse_prefill_fwd(q, kv, indices, sm_scale, d_v, attn_sink, topk_length)
    return results
```

参数与返回（摘自其 docstring）：

| 参数 | 形状 | 说明 |
| :--- | :--- | :--- |
| `q` | `[s_q, h_q, d_qk]` bf16 | 无 batch 维 |
| `kv` | `[s_kv, h_kv, d_qk]` bf16 | `h_kv` 必须为 1 |
| `indices` | `[s_q, h_kv, topk]` int32 | 无效索引设为 `-1` 或 `>= s_kv` |
| `sm_scale` | float | 缩放因子 |
| 返回 `output` | `[s_q, h_q, d_v]` bf16 | 注意力输出 |
| 返回 `max_logits` | `[s_q, h_q]` float | 每行 logits 最大值 |
| 返回 `lse` | `[s_q, h_q]` float | log-sum-exp（**以 2 为底**，见 README 的等价 PyTorch 代码） |

> **与解码的 `lse` 区别**：解码返回 `(out, lse)` 两项；sparse prefill 返回 `(out, max_logits, lse)` 三项，多了一个 `max_logits`，且其 lse 是 base-2 的（README 等价代码用了 `log2sumexp2` / `exp2`）。

**(b) `flash_attn_varlen_func` 与 autograd**

[flash_mla/flash_mla_interface.py:372-392](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/flash_mla/flash_mla_interface.py#L372-L392)：`flash_attn_varlen_func` 是 `FlashAttnVarlenFunc.apply` 的薄封装，对外签名与 `flash_attn` 包相似。

```python
def flash_attn_varlen_func(q, k, v, cu_seqlens_qo, cu_seqlens_kv,
                          max_seqlen_qo, max_seqlen_kv, dropout_p=0.0,
                          softmax_scale=None, causal=False,
                          deterministic=False, is_varlen=True):
    assert dropout_p == 0.0
    assert not deterministic
    return FlashAttnVarlenFunc.apply(q, k, v, cu_seqlens_qo, cu_seqlens_kv,
                                     max_seqlen_qo, max_seqlen_kv,
                                     causal, softmax_scale, is_varlen)
```

[flash_mla/flash_mla_interface.py:328-369](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/flash_mla/flash_mla_interface.py#L328-L369)：`FlashAttnVarlenFunc` 继承 `torch.autograd.Function`，`forward` 里调 `_flash_attn_varlen_forward` 并 `save_for_backward`，`backward` 里调 `_flash_attn_varlen_backward`。

```python
class FlashAttnVarlenFunc(torch.autograd.Function):
    def forward(ctx, q, k, v, cu_seqlens_qo, cu_seqlens_kv, ...):
        out, lse = _flash_attn_varlen_forward(...)
        ctx.save_for_backward(q, k, v, out, lse, cu_seqlens_qo, cu_seqlens_kv)
        ...
        return out, lse
    def backward(ctx, do, dlse):
        del dlse                       # LSE 暂不支持反向
        q, k, v, out, lse, *_ = ctx.saved_tensors
        dq, dk, dv = _flash_attn_varlen_backward(do, q, k, v, out, lse, ...)
        return dq, dk, dv, None, None, None, None, None, None, None
```

两个关键限制值得记住（后续 u7-l4 会深入）：

- **LSE 不支持反向**：`backward` 收到 `dlse` 直接 `del` 掉，返回值里 lse 对应位置是 `None`（见上 `return` 里 10 个值中后 7 个 `None`）。
- **bwd 暂不支持 GQA**：[flash_mla_interface.py:283-284](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/flash_mla/flash_mla_interface.py#L283-L284) 明确 `if num_qo_heads != num_kv_heads: raise ValueError(...)`。

**(c) 前向里的固定 workspace**

[flash_mla/flash_mla_interface.py:241-256](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/flash_mla/flash_mla_interface.py#L241-L256)：dense prefill 前向会预分配一块 32 MiB 的 workspace 传给 kernel。

```python
workspace_buffer = torch.empty(32 * 1024 * 1024, dtype=torch.uint8, device=q.device)
flash_mla_cuda.dense_prefill_fwd(workspace_buffer, q, k, v, ...)
```

这和反向 workspace「按公式动态计算大小」([flash_mla_interface.py:299-304](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/flash_mla/flash_mla_interface.py#L299-L304)) 形成对比——前向 workspace 固定 32 MiB 即可。

#### 4.3.4 代码实践

**目标**：阅读并理解 sparse prefill 的「等价 PyTorch 参考」，体会 `indices` 的语义。

**步骤**：

1. 阅读 README 给出的等价 PyTorch 代码：[README.md:154-170](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/README.md#L154-L170)。核心是 `focused_kv = kv[indices]` 这步 gather，再用 base-2 的 softmax 求输出。
2. 在纸上（或 Python 里）跟踪一个 2×3 的小 `indices` 张量，写出每个 q token 实际会 attend 哪几个 KV token，以及把某个索引设成 `-1` 后会发生什么（该位置被当作无效，不参与）。
3. **可选运行验证**（需要 SM90/SM100）：参照 [tests/test_flash_mla_sparse_prefill.py](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/test_flash_mla_sparse_prefill.py) 跑通 sparse prefill 用例，对比 kernel 输出与上述等价 PyTorch 实现。无 GPU 时本步**待本地验证**。

**预期结果**：你会清楚 `indices[i, j, k] = 页块号 * block_size + 块内偏移` 这一编码方式，以及为什么 sparse 模式不需要 `block_table`（分页信息已融进 indices）。

#### 4.3.5 小练习与答案

**练习 1**：`flash_mla_sparse_fwd` 为什么没有 batch 维？多 batch 推理时怎么办？

> **参考答案**：kernel 内部按单条序列设计。多 batch 时，把各 batch 的 q / kv **沿序列维拼接**成一条，并相应调整 `indices` 让不同 batch 的 q 只指向各自 batch 的 KV 区段（README「Note on batching」明确说明）。这本质上是用 varlen 风格模拟 batch。

**练习 2**：`flash_attn_varlen_func` 的 `cu_seqlens_qo` 是什么？它和 `cache_seqlens`（解码用的）有何不同？

> **参考答案**：`cu_seqlens_qo` 是**累加长度数组**，形如 `[0, len_1, len_1+len_2, ...]`，标记拼接张量里每条序列的起止位置；而 `cache_seqlens` 是**每条序列的实际长度**（非累加）。前者服务于 varlen 拼接格式（dense prefill），后者服务于 paged 分页格式（解码）。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个综合任务：

**任务**：写一个 60 行以内的脚本 `flashmla_api_tour.py`，依次演示三条入口的「调用骨架」（不要求都能跑通，重在签名正确、形状对齐）：

1. **Dense decode**：按 4.2.4 的最小脚本，构造 paged KV cache 并调用 `flash_mla_with_kvcache`，演示「`get_mla_metadata()` 一次 + 解码循环多次复用同一个 `sched_meta`」。
2. **Sparse decode**：在 dense 的基础上，额外构造一个 `indices` 张量（形状 `(b, s_q, topk)`，含若干 `-1` 无效项），把 `is_fp8_kvcache=True` 并传入 `indices`，观察它如何走 `flash_mla_cuda.sparse_decode_fwd` 分支（注意：sparse decode 还需要 FP8 量化的 KV cache，KV 量化细节见 u5-l1，本步可只搭形状骨架）。
3. **Sparse prefill**：构造无 batch 的 `q (s_q, h_q, d_qk)`、`kv (s_kv, 1, d_qk)`、`indices (s_q, 1, topk)`，调用 `flash_mla_sparse_fwd`，打印 `(output, max_logits, lse)` 三个返回值的形状。

**要求**：

- 每段调用前用注释写清各张量的形状与 dtype。
- 对「当前环境无法运行」的部分（如非 SM90/SM100、或未做 FP8 量化），明确标注 `# 待本地验证`，不要假装跑过。
- 在脚本末尾用一段注释回答：这三条入口里，哪一条**不需要** `FlashMLASchedMeta`？哪一条**支持反向传播**？

**参考答案要点**：

- 不需要 `FlashMLASchedMeta` 的是 `flash_mla_sparse_fwd` 与 `flash_attn_varlen_func` 系列（只有解码 `flash_mla_with_kvcache` 需要）。
- 支持反向传播的是 `flash_attn_varlen_func` 系列（通过 `FlashAttnVarlenFunc`）；两条 decode/sparse prefill 入口都只做前向。

## 6. 本讲小结

- `flash_mla/__init__.py` 导出 **6 个函数**，正好对应「解码 + sparse prefill + dense MHA prefill（三种打包形式）」四类 kernel 的对外入口。
- 解码走 `flash_mla_with_kvcache`，配套的 `FlashMLASchedMeta` 采用「**首次调用初始化、后续调用复用**」模式：`get_mla_metadata()` 返回空壳，kernel 在第一次调用时填充元数据，之后每步直接复用——但要求形状与 `cache_seqlens` / `topk_length` 等的值在多步间保持一致。
- ⚠️ README「Usage」里的 `get_mla_metadata(...)` 多参数写法**已过时**，当前真实签名是无参的；以 `tests/` 里的真实用法为准。
- `flash_mla_with_kvcache` 内部按**是否传 `indices`** 二分派发：有 `indices` 走 sparse decode（强制 FP8 KV cache），否则走 dense decode（需 `block_table` + `cache_seqlens`）。
- `flash_mla_sparse_fwd` 是无 batch 维的薄封装，返回 `(out, max_logits, lse)`（base-2 lse）。
- `flash_attn_varlen_func` 系列基于 `torch.autograd.Function`，是唯一支持反向的入口；当前限制：LSE 不支持反向、bwd 不支持 GQA。

## 7. 下一步学习建议

本讲只看了 Python 壳。要理解这些函数背后真正发生了什么，建议继续：

- **u2-l1 调用链全景**：把本讲里反复出现的 `flash_mla.cuda.dense_decode_fwd` 等 5 个绑定，逐一连到 `csrc/api/*.h` 的 C++ 接口与最终 kernel 命名空间，建立 Python→C++→CUDA 的完整调用图。
- **u2-l2 统一参数结构 `params.h`**：本讲里 `flash_mla_with_kvcache` 收到的张量，在 C++ 侧会被打包成 `DenseAttnDecodeParams` / `SparseAttnDecodeParams` 等结构，下一讲会逐字段拆解。
- **想先动手的读者**：可以在有 GPU 的环境下跑通 `tests/test_flash_mla_dense_decoding.py`、`tests/test_flash_mla_sparse_prefill.py`、`tests/test_fmha_sm100.py` 三个测试，分别对应本讲的三条入口，作为「能跑起来」的最直接验证。
