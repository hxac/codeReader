# 注意力机制与可插拔后端

## 1. 本讲目标

学完本讲，你应该能够：

1. 读懂 `SequenceMixer` → `Attention` 这条抽象链：知道 Transformer block 里的 `attention` 属性其实是一个「序列混合器」插槽，注意力只是其中一种实现。
2. 跟踪 `Attention.forward()` 的完整流程：投影 → QK norm → RoPE → SDPA → 门控 → 输出投影，并理解 SDPA 计算如何被委托给一个可替换的「后端」。
3. 说出 OLMo-core 提供的六种注意力后端（`torch` / `flex` / `flash_2` / `flash_3` / `flash_4` / `te`）各自的依赖、适用硬件和能力差异（滑动窗口、KV cache、上下文并行、自定义掩码）。
4. 通过 `AttentionConfig.backend` 或命令行覆盖切换后端，并验证不同后端的输出在数值容差内一致。

一个命名澄清：本讲规格里提到的 `MultiHeadAttention` 在 OLMo-core 中的实际类名是 [`Attention`](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/__init__.py#L627)，它本身就是多头自注意力（Multi-Head Self-Attention），并同时支持 MQA（多查询）与 GQA（分组查询）两种省显存变体。本讲一律使用真实类名 `Attention`。

## 2. 前置知识

### 2.1 缩放点积注意力（SDPA）

注意力是 Transformer 中让每个 token「看」其他 token 的机制。给定查询 \( Q \)、键 \( K \)、值 \( V \)，标准计算是：

\[
\mathrm{Attention}(Q, K, V) = \mathrm{softmax}\!\left(\frac{QK^\top}{\sqrt{d_k}}\right)V
\]

其中 \( d_k \) 是每个头的维度，除以 \( \sqrt{d_k} \) 是为了防止点积随维度增大而过大、导致 softmax 饱和。语言模型用**因果掩码**（causal mask）限制每个位置只能看到自己及之前的位置。

### 2.2 MHA / MQA / GQA

- **MHA**（多头注意力）：查询头数 = 键值头数，如 16 个 Q 头配 16 个 K/V 头。
- **MQA**（多查询）：所有 Q 头共享 1 个 K/V 头，KV 显存和带宽最省，但质量略降。
- **GQA**（分组查询）：折中方案，如 16 个 Q 头配 4 个 K/V 头，每 4 个 Q 头共享一组 K/V。OLMo 系列模型常用 GQA。`n_heads=16, n_kv_heads=4` 即为 GQA。

### 2.3 为什么需要「可插拔后端」

朴素实现要把整个 \( (B, H, S, S) \) 的注意力分数矩阵放进显存——序列长度 \( S \) 翻倍，显存翻 4 倍。FlashAttention 这类核函数把序列切块（tiling）、用在线 softmax（online softmax）增量归一化，显存从 \( O(S^2) \) 降到 \( O(S) \)，同时大幅提速。但这类核函数：

- 通常只支持特定 GPU 架构（如 flash_3 只支持 Hopper）；
- 功能上是超集/子集关系：有的支持 KV cache 推理，有的支持任意布尔掩码，有的支持跨卡环形通信。

所以 OLMo-core 的做法是：把「注意力数学」固定在 `Attention` 模块里，把「用哪个核函数算 SDPA」抽成可替换的 `AttentionBackend`，用配置一键切换。这与第 2 单元讲的 Registrable「一切皆 Config」思想一脉相承（不过注意：后端切换用的是普通枚举字段，不是注册子类，见 4.2.1 的说明）。

### 2.4 术语表

| 术语 | 含义 |
|---|---|
| SDPA | Scaled Dot-Product Attention，缩放点积注意力 |
| SWA | Sliding Window Attention，滑动窗口注意力（每个 token 只看最近 W 个位置） |
| GQA / MQA | 分组查询 / 多查询注意力 |
| KV cache | 推理时缓存历史 K/V，避免重复计算 |
| varlen | 变长序列 API，把 batch 内多个文档拼成一条序列（配合 `cu_doc_lens`） |
| CP | Context Parallelism，上下文并行（把序列切到多卡） |
| Ring / Ulysses | 两种 CP 风格：环形传递 K/V 分块 / 全互联交换头与序列 |
| DTensor / DeviceMesh | PyTorch 分布式张量与设备网格，用于 TP/CP |

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [src/olmo_core/nn/attention/base.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/base.py#L1) | `SequenceMixer` / `SequenceMixerConfig` 抽象基类：block 中「序列混合器」插槽的接口 |
| [src/olmo_core/nn/attention/__init__.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/__init__.py#L1) | 本讲主角：`AttentionConfig`（含 `backend` 字段）、`Attention`、`FusedAttentionV2`、`NormalizedAttention` |
| [src/olmo_core/nn/attention/backend.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/backend.py#L1) | `AttentionBackendName` 枚举与 `AttentionBackend` 基类、六种后端实现 |
| [src/olmo_core/nn/attention/flash_attn_api.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/flash_attn_api.py#L1) | 对 flash-attn / ring-flash-attn 的可选依赖守卫与统一分发函数 |
| [src/olmo_core/nn/attention/ring.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/ring.py#L1) | 上下文并行的负载均衡器（zig-zag / llama3 / Ulysses）与 CP 风格配置 |
| [src/olmo_core/nn/attention/te_attn_api.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/te_attn_api.py#L1) | TransformerEngine 可选依赖守卫 |
| [src/olmo_core/nn/attention/kv_cache.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/kv_cache.py#L11) | `KVCacheManager`：推理用 KV 缓存管理（本讲只涉及它与后端的能力协商） |
| [src/olmo_core/nn/transformer/config.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/transformer/config.py#L169) | `TransformerBlockConfig.sequence_mixer` 字段与 `llama_like` 工厂方法——后端配置的入口 |
| [src/test/nn/attention/attention_test.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/nn/attention/attention_test.py#L56) | 后端一致性测试，是本讲代码实践的参考依据 |

## 4. 核心概念与源码讲解

### 4.1 Attention 模块：从插槽抽象到多头注意力实现

#### 4.1.1 概念说明

回忆 u3-l1：`TransformerBlock` 里负责「让 token 互相看」的子层挂在一个名为 `attention` 的属性上，但它真正的类型标注是 `SequenceMixerConfig`——一个可插拔插槽。OLMo-core 在这个插槽上放了三种东西：

- **注意力**（本讲）：`AttentionConfig`（注册名 `"attention"`）；
- 线性注意力 / 循环层（`GatedDeltaNet`、`KimiDeltaAttention`、`NemotronMamba2`，见 `recurrent.py` / `kda.py`）；
- 未来其他序列混合器（卷积等）。

`SequenceMixer` 抽象只约定四件事：如何接入张量并行（`apply_tp`）、如何接入上下文并行（`apply_cp`）、每个 token 的 FLOPs 估算（`num_flops_per_token`）、如何初始化权重（`init_weights`）。它不规定前向签名——每种混合器有自己的 `forward`。

`Attention` 类则是这个插槽上的标准多头自注意力实现，一个实例包含：

- 四个投影：`w_q`、`w_k`、`w_v`、`w_out`（GQA 时 `w_k`/`w_v` 的输出维度按 `n_kv_heads` 缩小）；
- 可选件：门控 `w_g`、QK 归一化 `q_norm`/`k_norm`、旋转位置编码 `rope`、注意力汇 `sinks`、scalable-softmax 缩放 `ssmax_scale`；
- 一个 `backend` 成员：真正执行 SDPA 的核函数对象（4.2 节的主角）。

变体 `FusedAttentionV2` 把 `w_q/w_k/w_v` 三个投影合并成一个打包的 `w_qkv` 投影（省两次矩阵乘的启动开销），`NormalizedAttention` 是 nGPT 风格的归一化注意力。三者都通过同一个 `AttentionConfig` 选择。

#### 4.1.2 核心流程

`Attention.forward(x)` 的执行顺序（`x` 形状为 `(B, T, d_model)`）：

```text
x (B, T, d_model)
 ├─ ① w_q / w_k / w_v 投影      → (B, T, n_heads·D) 与 (B, T, n_kv_heads·D)
 ├─ ② 可选 clip_qkv 数值裁剪
 ├─ ③ 可选 QK norm（整维或按头）
 ├─ ④ reshape 成头              → (B, T, H, D) / (B, T, H_kv, D)
 ├─ ⑤ 可选 RoPE（只作用于 q、k）
 ├─ ⑥ sdpa(q, k, v)             → 委托给 self.backend(...)   ★ 本讲核心
 │      返回 (B, T, H, D)
 ├─ ⑦ 可选门控：att *= sigmoid(w_g(x))
 ├─ ⑧ reshape 回 (B, T, H·D)
 └─ ⑨ w_out 投影                → (B, T, d_model)
```

第 ⑥ 步的数学就是标准 SDPA（带因果掩码，可选滑动窗口）：

\[
\mathrm{att} = \mathrm{softmax}\!\left(\frac{(qW_q)(kW_k)^\top}{\sqrt{D}} + M\right)(vW_v), \qquad M_{ij} = \begin{cases} 0 & j \le i \text{ 且 } i - j < W \\ -\infty & \text{否则} \end{cases}
\]

其中 \( W \) 是滑动窗口大小（无窗口时为 \( +\infty \)）。注意：除了第 ⑥ 步，其余所有步骤都与后端无关——这正是「注意力数学」与「核函数」解耦的关键。

#### 4.1.3 源码精读

**① 插槽抽象。** [src/olmo_core/nn/attention/base.py:L20-L47](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/base.py#L20-L47) 定义了 `SequenceMixer` 的四个抽象方法：`apply_tp`（张量并行改造）、`apply_cp`（上下文并行改造）、`num_flops_per_token`（FLOPs 估算）、`init_weights`（权重初始化）。[src/olmo_core/nn/attention/base.py:L66-L80](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/base.py#L66-L80) 的 `SequenceMixerConfig` 继承了 `ModuleConfig` 与 `Registrable`，其 `build(d_model, layer_idx=, n_layers=, ...)` 约定了「构建时才知道模型宽度与层数」的装配协议——block 构建时把 `d_model` 传进来，序列混合器才知道投影该多大。

**② 注册为 `"attention"`。** [src/olmo_core/nn/attention/__init__.py:L354-L356](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/__init__.py#L354-L356)：

```python
@SequenceMixerConfig.register("attention")
@dataclass
class AttentionConfig(SequenceMixerConfig["SequenceMixer"]):
```

这一行 `@SequenceMixerConfig.register("attention")` 就是 u2-l2 讲过的 Registrable 机制：`TransformerBlockConfig.sequence_mixer` 字段类型标注为基类 `SequenceMixerConfig`，YAML 里写 `type: attention` 就能选中注意力（写 `type: gated_delta_net` 则选中线性注意力变体）。

**③ 配置字段。** [src/olmo_core/nn/attention/__init__.py:L363-L417](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/__init__.py#L363-L417) 是 `AttentionConfig` 的全部字段，重点几个：

- `name`（L363）：`AttentionType.default / fused_v2 / normalized`，决定 build 出哪个类；
- `n_heads` / `n_kv_heads` / `head_dim`（L367-L369）：GQA 参数；
- `rope` / `qk_norm` / `gate`（L371-L374）：可选组件的嵌套 Config；
- `backend`（L378）：`Optional[AttentionBackendName]`，**本讲的开关**，不填则自动选择（见 4.2.2）；
- `sliding_window`（L380）：`SlidingWindowAttentionConfig`，按层模式配置滑动窗口（如 `[4096, 4096, 4096, -1]` 循环，`-1` 表示该层全注意力，见 [src/olmo_core/nn/attention/__init__.py:L252-L306](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/__init__.py#L252-L306)）。

**④ build 分发。** [src/olmo_core/nn/attention/__init__.py:L481-L606](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/__init__.py#L481-L606) 的 `AttentionConfig.build()` 先把 dataclass 字段转成 kwargs、按层解析滑动窗口（L524-L538），再在 [L590-L602](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/__init__.py#L590-L602) 按 `name` 分发到 `Attention` / `FusedAttentionV2` / `NormalizedAttention`。若传入了目标类不支持的组合（如给 `normalized` 传 `window_size`），会抛 `OLMoConfigurationError`，配置错误在构建期就被拦截。

**⑤ Attention 的骨架。** [src/olmo_core/nn/attention/__init__.py:L627-L658](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/__init__.py#L627-L658) 的类文档写明：支持 MQA 与 GQA；文档内掩码（intra-document masking）需传入 `cu_doc_lens`/`max_doc_len` 且要求后端支持。四个投影的创建在 [L705-L716](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/__init__.py#L705-L716)：`w_k`/`w_v` 的输出特征数是 `n_kv_heads * head_dim`，天然实现 GQA 的参数节省。

**⑥ Q/K/V 准备。** [src/olmo_core/nn/attention/__init__.py:L965-L1033](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/__init__.py#L965-L1033) 的 `_prepare_qkv` 完成流程图中的 ①—⑤ 步。两个值得注意的注释：reshape 用 `-1` 而不是 `n_heads`（L1000-L1002），是为了在张量并行下自动推断本地头数；头维 QK norm 必须在 reshape 之后做（L1009-L1018），保证逐头增益广播正确。

**⑦ 委托后端。** [src/olmo_core/nn/attention/__init__.py:L832-L893](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/__init__.py#L832-L893) 的 `sdpa()` 方法是 `Attention` 与后端的交界：先做能力检查（`or_mask`/`and_mask` 只有效于声明了 `SUPPORTS_OR_MASK`/`SUPPORTS_AND_MASK` 的后端，L854-L863），然后在 [L878-L890](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/__init__.py#L878-L890) 一行 `att = self.backend((q, k, v), cu_doc_lens=..., kv_cache_manager=..., sinks=..., ...)` 把所有复杂度交给后端。`Attention.forward`（[L1035-L1159](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/__init__.py#L1035-L1159)）则串起完整流程，并在最后做门控与 `w_out` 投影。

**⑧ 推理挂载 KV cache。** [src/olmo_core/nn/attention/__init__.py:L1312-L1330](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/__init__.py#L1312-L1330) 的 `init_kv_cache_manager()` 只在推理时调用；它先 `self.backend.assert_supports_kv_cache()` 协商能力，再创建 `KVCacheManager`（[src/olmo_core/nn/attention/kv_cache.py:L11](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/kv_cache.py#L11)）。「功能是否可用」永远先问后端，这是贯穿本包的设计纪律。

#### 4.1.4 代码实践

**实践目标**：亲手从 `AttentionConfig` 构建一个 `Attention` 模块，确认配置 → 模块 → 后端三层的对应关系，并观察后端自动选择日志。

**操作步骤**（示例代码，可在仓库根目录存成 `tmp_attn_probe.py` 用 `python tmp_attn_probe.py` 运行；请勿放进 `src/`）：

```python
# 示例代码：不属于 OLMo-core 仓库
import logging
import torch
from olmo_core.nn.attention import AttentionConfig
from olmo_core.nn.rope import RoPEConfig, RoPEType

logging.basicConfig(level=logging.INFO)  # 让 "Using attention backend ..." 日志可见

cfg = AttentionConfig(n_heads=8, n_kv_heads=4, rope=RoPEConfig(name=RoPEType.default))
attn = cfg.build(d_model=512, layer_idx=0, n_layers=4)

print(type(attn).__name__)                     # 期望: Attention
print(type(attn.backend).__name__)             # 期望: TorchAttentionBackend（无 GPU 环境自动回落）
print(attn.n_heads, attn.n_kv_heads, attn.head_dim)

x = torch.randn(2, 16, 512)                    # (batch, seq_len, d_model)
out = attn(x)
print(out.shape)                               # 期望: torch.Size([2, 16, 512])
```

**需要观察的现象**：

1. 日志中出现 `Using attention backend 'torch'`（有 GPU 且装有 flash-attn 时可能是别的值）；
2. `attn.w_k.weight.shape` 是 `(4*64, 512)` 而 `attn.w_q.weight.shape` 是 `(8*64, 512)`——GQA 直接体现在权重形状上；
3. 无 GPU 机器上即使把配置改成 `backend=AttentionBackendName.flash_2`，也会打印警告并回落到 torch（原因见 4.2.2）。

**预期结果**：输出形状 `(2, 16, 512)`，与输入同形。权重形状、后端类型如上。运行输出**待本地验证**（本讲义编写环境未执行）。

#### 4.1.5 小练习与答案

**练习 1**：`TransformerBlock` 构建时为什么必须把 `layer_idx` 和 `n_layers` 传给 `sequence_mixer.build()`？

**答案**：因为 `SlidingWindowAttentionConfig` 的窗口模式是按层下标的循环模式（`pattern[layer_idx % len(pattern)]`），且首尾层默认强制全注意力（[src/olmo_core/nn/attention/__init__.py:L271-L297](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/__init__.py#L271-L297)）；同时 `init_weights` 也需要 `block_idx`/`num_blocks` 来做按深度的标准差缩放（如 `llama` 方案的 `std / sqrt(2*num_blocks)`）。不知道自己处在第几层，窗口大小和初始化标准差都无法确定。

**练习 2**：`AttentionConfig` 上同时有已弃用的 `use_flash` 和新的 `backend`，二者冲突时会怎样？

**答案**：[src/olmo_core/nn/attention/__init__.py:L309-L327](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/__init__.py#L309-L327) 的 `_resolve_attention_backend`：显式 `backend` 优先；`use_flash=True` 且 backend 为 None 时发 `DeprecationWarning` 并等价于 `flash_2`；`use_flash=True` 但 backend 是其他值时直接抛 `OLMoConfigurationError`。这是「旧字段保留只为加载旧配置」的兼容模式（与 u2-l1 的 round-trip 讨论呼应）。

**练习 3**：`FusedAttentionV2` 与 `Attention` 的前向差异在哪里？为什么说它「不需要打包 QKV 的核函数」？

**答案**：`FusedAttentionV2` 只是把三个投影换成一个 `w_qkv`，随后立刻 `split` 回 q/k/v（[src/olmo_core/nn/attention/__init__.py:L1772-L1824](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/__init__.py#L1772-L1824) 覆写的 `_prepare_qkv`），SDPA 仍以 `(q, k, v)` 元组交给后端。它的类文档（[L1540-L1548](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/__init__.py#L1540-L1548)）明确说「保持常规后端契约」。真正把打包 QKV 张量直接喂给核函数的是 `FlashAttention2Backend`/`FlashAttention3Backend` 的 packed 路径（4.2.3 ⑥）。

### 4.2 AttentionBackend：六种后端与能力协商

#### 4.2.1 概念说明

`AttentionBackend` 是一个 `nn.Module`，唯一的职责是：吃进 QKV（元组 `(q, k, v)` 或打包张量），吐出注意力输出 `(B, T, H, D)`。所有后端共享同一个 `forward` 签名（[src/olmo_core/nn/attention/backend.py:L243-L265](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/backend.py#L243-L265)），额外参数（`cu_doc_lens` 文档掩码、`kv_cache_manager` 推理缓存、`sinks` 注意力汇等）由各后端按能力自行支持或拒绝。

`AttentionBackendName` 枚举（[src/olmo_core/nn/attention/backend.py:L43-L83](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/backend.py#L43-L83)）列出六种实现：

| 枚举值 | 类 | 依赖 | 硬件 | 一句话定位 |
|---|---|---|---|---|
| `torch` | `TorchAttentionBackend` | 无（PyTorch 内置 SDPA） | 任意（含 CPU） | 全功能兜底参考实现，唯一支持 `or_mask`/`and_mask` 稠密掩码与 attention sinks |
| `flex` | `FlexAttentionBackend` | torch ≥ 2.5 的 `flex_attention` | 任意（CPU 走 eager） | 把稠密掩码语义编译成块稀疏融合核，多模态训练中最快的自定义掩码后端 |
| `flash_2` | `FlashAttention2Backend` | `flash-attn`（CP 另需 `ring-flash-attn`） | Ampere SM 8.0+ | 主力训练后端；KV cache、packed QKV、ring CP 全支持 |
| `flash_3` | `FlashAttention3Backend` | `flash-attn` hopper 分支 | 仅 Hopper SM 9.0（H100/H800） | Hopper 上的更快 beta 版；不支持 ring CP、不支持 dropout |
| `flash_4` | `FlashAttention4Backend` | `flash-attn` 的 CUTE 实现 | 仅 Blackwell SM 10.x | Blackwell 专用；不支持 packed QKV，KV cache 手动写缓存 |
| `te` | `TEAttentionBackend` | `transformer_engine` | Hopper SM 9.0+ | NVIDIA 官方融合注意力，常与 float8 训练搭配（u10-l1） |

注意一个设计细节：后端**不是** Registrable 子类注册体系，而是一个 `StrEnum` + 每个成员在自己的模块里定义实现类。枚举的 `get_class()`（[L85-L99](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/backend.py#L85-L99)）是「名字 → 类」的硬编码映射。这样后端可以直接作为 YAML 里的字符串字段序列化，而不必为每个后端写一个 Config 子类。

每个后端用两组机制声明自己的能力边界：

1. **类方法断言**（[L183-L235](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/backend.py#L183-L235)）：`assert_supported`（环境可用性）、`assert_supports_swa` / `assert_supports_kv_cache` / `assert_supports_packed_qkv` / `assert_supports_ring_cp` / `assert_supports_ulysses_cp`（功能可用性）。不支持就抛 `RuntimeError`。
2. **类属性标志**（[L146-L155](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/backend.py#L146-L155)）：`SUPPORTS_OR_MASK` / `SUPPORTS_AND_MASK`，目前只有 `torch` 与 `flex` 为 True（多模态的双向图像 token 掩码与子段隔离掩码需要它们）。

汇总能力矩阵（依据各类的 `assert_*` 覆写）：

| 后端 | SWA | KV cache | packed QKV | ring CP | ulysses CP | or/and mask | attention sinks |
|---|---|---|---|---|---|---|---|
| torch | ✓ | ✗ | ✗ | ✗ | ✓ | ✓ | ✓ |
| flex | ✓ | ✗ | ✗ | ✗ | ✗ | ✓ | ✗ |
| flash_2 | ✓ | ✓ | ✓ | ✓ | ✓ | ✗ | ✗ |
| flash_3 | ✓ | ✓ | ✓ | ✗ | ✓ | ✗ | ✗ |
| flash_4 | ✓ | ✓ | ✗ | ✗ | ✓ | ✗ | ✗ |
| te | ✓ | ✗ | ✗ | ✓ | ✓ | ✗ | ✗ |

#### 4.2.2 核心流程

**后端的生命周期**分三步：

```text
① 配置期   AttentionConfig.backend（可为 None）
      │   Attention.__init__ 里的解析顺序：
      │     a. _resolve_attention_backend() 兼容旧 use_flash
      │     b. 若配置了滑动窗口且未显式指定后端 → 自动选 flash_2（前提是装了 flash-attn）
      │     c. 仍为 None → 默认 torch
      │     d. attention_sinks 强制要求 torch（否则构造期报错）
      │     e. 无 GPU 且后端非 torch → 警告并回落 torch
      │     f. backend.assert_supported()（缺包/硬件不符 → RuntimeError）
      ▼
② 构建期   AttentionBackendName.build(head_dim, n_heads, n_kv_heads, scale, dropout_p, window_size)
      │   → 具体后端实例，存为 Attention.backend
      ▼
③ 前向期   Attention.sdpa() → self.backend((q, k, v), ...) → (B, T, H, D)
```

**为什么滑动窗口会自动偏向 flash**：torch 后端做 SWA 需要显式构造 `(S, S)` 布尔掩码矩阵（[backend.py:L478-L536](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/backend.py#L478-L536) 的 `_build_sliding_window_mask`），长序列下又慢又占显存；flash 核原生支持窗口参数。自动选择逻辑在 [src/olmo_core/nn/attention/__init__.py:L792-L797](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/__init__.py#L792-L797)。

**TorchAttentionBackend 前向的形状之旅**（最能说明「同一份数学、不同的张量排布」）：

```text
输入 (q, k, v): (B, T, H, D) 与 (B, T, H_kv, D)
 ① GQA 展开：_repeat_kv(k, n_rep) → (B, T, H, D)   # PyTorch SDPA 不支持 GQA
 ② 转置到头前：q.transpose(1,2) 等 → (B, H, T, D)   # SDPA 期望头维在前
 ③ F.scaled_dot_product_attention(q, k, v, attn_mask=..., is_causal=...)
 ④ 转置回：(B, T, H, D).contiguous()
```

**varlen（变长）分发**：flash 系后端通过 `cu_doc_lens`（累积文档长度）支持「一个 batch 里多个不等长文档、文档间互不可见」的打包训练。分发函数判断四元组（`cu_seqlens_q/k`、`max_seqlen_q/k`）是否齐全，齐全则走 `flash_attn_varlen_func`（先把 batch 维压平），否则走普通 `flash_attn_func`。

#### 4.2.3 源码精读

**① 可选依赖守卫。** [src/olmo_core/nn/attention/flash_attn_api.py:L9-L32](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/flash_attn_api.py#L9-L32) 用四个 `try/except ImportError` 分别尝试导入 flash-attn 2、flash-attn 3（两个可能的名字）、flash-attn 4（CUTE）、ring-flash-attn，失败时置 `None`——这正是 u1-l1 说的「重功能按需挂载」。探测函数 [L37-L64](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/flash_attn_api.py#L37-L64) 不仅查包是否存在，还查 GPU 计算能力：`has_flash_attn_3()` 要求 SM 在 `[9.0, 10.0)`，`has_flash_attn_4()` 要求 SM 在 `[10.0, 12.0]`。装错硬件的包同样视为不可用。TransformerEngine 的守卫在 [src/olmo_core/nn/attention/te_attn_api.py:L5-L20](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/te_attn_api.py#L5-L20)，额外捕获 `OSError`（原生库加载失败也优雅降级）。

**② 枚举 → 类 → 实例。** [src/olmo_core/nn/attention/backend.py:L85-L120](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/backend.py#L85-L120)：`get_class()` 返回实现类；`build()` 是统一工厂，把 `head_dim/n_heads/n_kv_heads/scale/dropout_p/window_size` 传给构造器，并给每个后端的 buffer 缓存划一个命名空间（`attn_backend=<名字>`），避免不同后端共享掩码缓存。基类构造器 [L157-L181](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/backend.py#L157-L181) 第一行就调用 `self.assert_supported()`，配置了滑动窗口还会 `assert_supports_swa()`——错误尽可能在构造期而非首次前向时暴露。

**③ 注意力里的后端解析。** [src/olmo_core/nn/attention/__init__.py:L783-L826](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/__init__.py#L783-L826) 是 4.2.2 流程图的落地：SWA 自动选 flash_2（L792-L797）、默认 torch（L799-L800）、attention_sinks 限定 torch（L804-L807）、无 GPU 回落（L809-L813）、最终 `assert_supported()` 并打印日志（L815-L816）。特别注意窗口的表示转换：用户给的是单个正整数 `window_size`，传给后端时变成二元组 `(window_size - 1, 0)`（L796-L797）——「向左看 W-1 个、向右不看」，即只看过去。

**④ 参考实现 TorchAttentionBackend。** [src/olmo_core/nn/attention/backend.py:L416-L438](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/backend.py#L416-L438)：GQA 用 `_repeat_kv` 显式复制 K/V（PyTorch SDPA 当时不支持 GQA），转置后调用 `F.scaled_dot_product_attention`；有掩码时传 `attn_mask`、无掩码时用 `is_causal=True` 让 PyTorch 自行选择内部快路径。`_repeat_kv` 本体在 [L1491-L1500](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/backend.py#L1491-L1500)（unsqueeze→expand→reshape，等价于 `repeat_interleave` 但更快）。它的能力声明在 [L297-L319](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/backend.py#L297-L319)：SWA 支持（pass）、ring CP/KV cache/packed QKV 全部抛错。多模态的 `or_mask`（图像 token 双向可见）与 `and_mask`（子段隔离）逻辑在 [L372-L392](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/backend.py#L372-L392)：`(causal | or_mask) & and_mask`，全部用显式布尔掩码实现。

**⑤ FlexAttentionBackend。** [src/olmo_core/nn/attention/backend.py:L556-L568](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/backend.py#L556-L568) 的类文档道出其定位：复刻 torch 后端的掩码语义，但用 `mask_mod` + `BlockMask` 表达成块稀疏融合核，不实体化 `(B, H, S, S)` 分数矩阵——是支持多模态自定义掩码的**最快**后端。一个小而关键的工程细节在 [L542-L553](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/backend.py#L542-L553)：CUDA 上缓存 `torch.compile(flex_attention)` 的编译结果，CPU 上直接 eager（编译在 CPU 上又慢又无必要）——这也意味着 flex 后端可以在无 GPU 环境做正确性对比（见 4.2.4）。

**⑥ FlashAttention2Backend。** 能力断言在 [src/olmo_core/nn/attention/backend.py:L795-L823](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/backend.py#L795-L823)：`assert_supported` 检查 `has_flash_attn_2()`，`assert_supports_ring_cp` 额外要求 ring-flash-attn。前向是多路分发：packed QKV + ring CP 走 `dispatch_ring_flash_attn_qkvpacked`（[L851-L899](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/backend.py#L851-L899)）；带 KV cache 走 `dispatch_flash_attn_with_kvcache`（[L909-L922](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/backend.py#L909-L922)，缓存原地更新）；普通路径走 `dispatch_flash_attn`（[L980-L994](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/backend.py#L980-L994)）。FA3（[L997-L1050](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/backend.py#L997-L1050)）结构相同，构造期直接拒绝 `dropout_p > 0`；FA4（[L1196-L1289](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/backend.py#L1196-L1289)）的 KV cache 路径是手动把 k/v 写进缓存再用 `seqused_k` 限制有效长度。TE 后端（[L1347-L1380](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/backend.py#L1347-L1380)）在构造器里直接实例化 `TEDotProductAttention`，并注意它整个 `forward` 被 `@torch.compiler.disable` 包裹（[L1438-L1440](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/backend.py#L1438-L1440)），避免 Dynamo 追踪其 pybind 设置代码。

**⑦ varlen 分发函数。** [src/olmo_core/nn/attention/flash_attn_api.py:L98-L123](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/flash_attn_api.py#L98-L123)（`dispatch_flash_attn`）：若 `cu_seqlens_q/k` 与 `max_seqlen_q/k` 四者齐全则压平 batch 维走 `flash_attn_varlen_func`，否则走普通 `flash_attn_func`。这个「按参数齐全度选 API」的模式在所有 dispatch 函数中重复出现。

**⑧ 从 TransformerConfig 到后端。** [src/olmo_core/nn/transformer/config.py:L1764-L1789](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/transformer/config.py#L1764-L1789)：`llama_like` 工厂把 `AttentionConfig(...)` 装进 `TransformerBlockConfig.sequence_mixer`，其中 `backend=_resolve_attention_backend(attn_backend, use_flash)`（L1782）。`fused_ops=True` 且非 GQA 时默认升级为 `fused_v2 + flash_2`（[L1749-L1757](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/transformer/config.py#L1749-L1757)）。而 block 构建时调用 `sequence_mixer.build(...)` 的地方在 [src/olmo_core/nn/transformer/block.py:L132](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/transformer/block.py#L132)，产物挂到 `self.attention`。

#### 4.2.4 代码实践

**实践目标**：在**纯 CPU 环境**下直接构建两个后端实例，验证「不同后端、同一份数学」——`torch` 与 `flex` 对相同 QKV 的输出应在容差内一致。这是 GPU 上 flash 后端一致性测试（[src/test/nn/attention/attention_test.py:L56-L108](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/nn/attention/attention_test.py#L56-L108) 的 `test_attention_backend`）的 CPU 版。

**操作步骤**（示例代码）：

```python
# 示例代码：不属于 OLMo-core 仓库（CPU 可跑，需 torch>=2.5）
import torch
from olmo_core.utils import seed_all
from olmo_core.nn.attention import AttentionBackendName

seed_all(0)
B, T, H, D = 2, 16, 8, 128
q = torch.randn(B, T, H, D)
k = torch.randn(B, T, H, D)
v = torch.randn(B, T, H, D)

# 注意：AttentionBackendName.build() 是后端的统一工厂（backend.py L101-L120）
b_torch = AttentionBackendName.torch.build(head_dim=D, n_heads=H)
b_flex = AttentionBackendName.flex.build(head_dim=D, n_heads=H)

out_torch = b_torch((q, k, v))   # 后端 forward 吃 (q, k, v) 元组
out_flex = b_flex((q, k, v))

print(out_torch.shape)                                    # 期望 [2, 16, 8, 128]
print((out_torch - out_flex).abs().max().item())          # 期望接近 0（fp32 下 ~1e-7 量级）
torch.testing.assert_close(out_torch, out_flex)
```

**需要观察的现象**：

1. 两个后端输出形状均为 `(2, 16, 8, 128)`——后端约定输出保持 `(B, T, H, D)` 排布（torch 后端在内部转置又转置回来，见 4.2.2）；
2. `flex` 在 CPU 上不会触发 `torch.compile`（对照 [backend.py:L542-L553](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/backend.py#L542-L553) 的注释），首次调用也不会有明显编译停顿；
3. 若尝试 `AttentionBackendName.flash_2.build(...)` 且本机未装 flash-attn，会在构造期立刻抛 `RuntimeError`（`assert_supported`），而不是等到前向。

**预期结果**：`assert_close` 通过，最大绝对差在 fp32 下约为 1e-6 ~ 1e-7 量级（两个核的浮点求和顺序不同，不要求逐位相等）。具体数值**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `AttentionBackendName` 用枚举 + `get_class()` 映射，而不是像优化器那样用 `@register("...")` 注册子类 Config？

**答案**：后端没有需要独立序列化的字段——`head_dim`、`n_heads`、`window_size` 等全部由 `AttentionConfig`/`TransformerConfig` 持有，后端实例只是运行期对象。一个 `StrEnum` 字段（`backend: Optional[AttentionBackendName]`）就能在 YAML 里表达切换（`backend: flash_2`），不需要为每个后端维护一个 dataclass 子类。优化器则相反：AdamW 与 Lion 有各自独立的超参字段，所以值得用 Registrable 子类（对照 u2-l2）。

**练习 2**：在装有 flash-attn 的 GPU 机器上，`AttentionConfig(backend=flash_2, attention_sinks=True)` 会发生什么？为什么要在构造期就拦截？

**答案**：抛 `OLMoConfigurationError`（[src/olmo_core/nn/attention/__init__.py:L804-L807](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/__init__.py#L804-L807)）。attention sinks 需要把每头可学习的 logit 拼接进 softmax（torch 后端 [backend.py:L439-L465](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/backend.py#L439-L465) 的手工 softmax 路径），flash 核没有这个入口。构造期拦截（源码注释明说「before the CPU fallback below」）是为了避免错误被后续的 CPU 回落掩盖、延迟到第一次前向才爆炸。

**练习 3**：`has_flash_attn_3()` 为什么在包已导入成功时还可能返回 False？

**答案**：[src/olmo_core/nn/attention/flash_attn_api.py:L42-L48](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/flash_attn_api.py#L42-L48)：有 CUDA 时还要求计算能力落在 `[9.0, 10.0)`（Hopper）。flash-attn 3 的核是按架构手写的，装在 A100（SM 8.0）上也无法运行，所以「包存在」不等于「可用」。

### 4.3 Ring 与 Ulysses：后端之上的上下文并行入口

#### 4.3.1 概念说明

上下文并行（CP）把**序列维度**切到多卡：每张卡只持有 \( T/\text{CP} \) 个 token。难点在于因果注意力需要看到所有历史 K/V，于是有两种经典解法：

- **Ring attention**：K/V 分块在卡间沿环传递，每卡对收到的每块 K/V 增量计算部分 softmax，通信可与计算重叠；
- **Ulysses**：用 all-to-all 把「每卡持有一段完整序列的所有头」变成「每卡持有全部序列的若干头」，注意力在局部头集合上完整计算后再 all-to-all 换回去。

在 OLMo-core 中，这一切对 `Attention` 是透明的：`apply_cp()` 把 CP 进程组和风格记录在后端上（`cp_enabled=True`），后端前向自动切换到对应路径。`ring.py` 里的「负载均衡器」解决另一个问题：因果注意力下靠前的分块计算量天然更少，如何切分序列让各卡负载均衡。

#### 4.3.2 核心流程

```text
配置期：ContextParallelConfig（u7-l2 详讲）
          └─ ring: RingContextParallelStyle(load_balancer=zig_zag|llama3, head_stride=1)
             或 uly: UlyssesContextParallelStyle
数据期：load_balancer.batch_shard(inputs, seq_dims) 把 batch 按序列维切开
          zig-zag：rank i 拿第 i 块 + 第 (2W-1-i) 块（首尾配对，均衡因果负载）
          llama3 ：rank i 拿连续区间 [i·L, (i+1)·L)，专为变长文档掩码设计
          ulysses：rank i 拿连续区间（后续靠 all-to-all 重组，无需配对）
前向期：Attention.apply_cp() → backend.apply_cp() 记录 cp_pg + 风格
          → backend.forward() 内部按 ring/uly 分发到 ring-flash-attn 或 all-to-all + flash
```

#### 4.3.3 源码精读

**① 负载均衡器类型。** [src/olmo_core/nn/attention/ring.py:L14-L48](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/ring.py#L14-L48)：`RingAttentionLoadBalancerType` 有 `zig_zag`、`llama3`、`ulysses` 三个成员，`build(cp_mesh)` 按 CP 进程组的 rank/world size 实例化对应均衡器。

**② 抽象契约。** [src/olmo_core/nn/attention/ring.py:L51-L96](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/ring.py#L51-L96)：`RingAttentionLoadBalancer` 只约定两个方法——`batch_shard`（普通切分）与 `batch_shard_by_document`（变长文档切分，返回值里附带该实现需要的额外输入，如 llama3 的 `cu_doc_lens_q/k` 与 `local_k_slice`）。

**③ zig-zag 切分。** [src/olmo_core/nn/attention/ring.py:L99-L147](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/ring.py#L99-L147)：把序列切成 `2 × cp_world_size` 块，rank i 取「第 i 块 + 倒数第 i+1 块」拼成本地分片（L140-L144）。首尾配对让每卡的「 causal 负载」大致均衡。序列长度不能整除 `2W` 时要么报错要么按 `pad_values` 填充。

**④ llama3 切分。** [src/olmo_core/nn/attention/ring.py:L249-L268](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/ring.py#L249-L268) 的类文档说明：rank i 拿连续区间，且**只支持** `batch_shard_by_document`（为变长文档掩码设计，靠 `ring_flash_attn.llama3_flash_attn_prepare_cu_seqlens` 计算每卡独立的 Q/K 文档边界）。

**⑤ Ulysses 切分与 CP 风格配置。** [src/olmo_core/nn/attention/ring.py:L391-L398](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/ring.py#L391-L398)：Ulysses 只需简单连续切块，因为重排交给 all-to-all。对用户暴露的两个风格配置是纯 dataclass：[L522-L530](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/ring.py#L522-L530) 的 `UlyssesContextParallelStyle`（无字段）与 [L533-L549](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/ring.py#L533-L549) 的 `RingContextParallelStyle`（`load_balancer` 与 `head_stride`——每轮环通信处理几个 K/V 头，加大可减少通信次数）。

**⑥ 后端侧的 CP 启用。** [src/olmo_core/nn/attention/backend.py:L267-L286](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/backend.py#L267-L286) 的 `AttentionBackend.apply_cp()`：先按风格断言能力，再记录 `cp_pg`/`ring`/`uly` 并置 `cp_enabled=True`。此后 `FlashAttention2Backend.forward` 里 `self.cp_enabled` 分支会把调用路由到 `dispatch_ring_flash_attn`（ring 风格）或「all-to-all 头重排 + 普通 flash + all-to-all 换回」（ulysses 风格），后者见 [src/olmo_core/nn/attention/backend.py:L946-L976](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/backend.py#L946-L976)。

#### 4.3.4 代码实践（源码阅读型）

**实践目标**：不跑多进程，纯靠读代码推演 zig-zag 切分结果，建立对负载均衡器的直觉。

**操作步骤**：

1. 精读 [src/olmo_core/nn/attention/ring.py:L117-L147](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/ring.py#L117-L147) 的 `RingAttentionZigZagLoadBalancer.batch_shard`；
2. 假设 `seq_len=16`、`cp_world_size=2`（即 `cp_rank ∈ {0, 1}`），在纸上画出序列被切成 4 块后每块的位置区间；
3. 用 Python（任何环境）验证你的推演：

```python
# 示例代码：模拟 zig-zag 的块分配逻辑（对应 ring.py L140-L144）
W, T = 2, 16                              # CP 世界大小 2，序列长 16
chunk_len = T // (2 * W)                  # 块长 = 16 / 4 = 4
chunks = [f"[{i*chunk_len},{(i+1)*chunk_len})" for i in range(2 * W)]
for rank in range(W):
    local = [chunks[rank], chunks[2 * W - rank - 1]]
    print(f"rank {rank} 拿到: {local}")
```

**需要观察的现象**：打印出的每卡两个区间是否与你手推的一致；注意 rank 0 拿到的是「最靠前」与「最靠后」两块的配对。

**预期结果**：rank 0 拿块 0（`[0,4)`）与块 3（`[12,16)`），rank 1 拿块 1（`[4,8)`）与块 2（`[8,12)`）。两卡各自的「靠前块 + 靠后块」配对正是 zig-zag 负载均衡的体现。多进程真实运行**待本地验证**（可用 [src/test/nn/attention/ring_test.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/nn/attention/ring_test.py#L1) 的分布式测试工具跑）。

#### 4.3.5 小练习与答案

**练习 1**：`Attention.apply_cp()` 只是转调 `self.backend.apply_cp()`，为什么不在 `Attention` 里自己实现 CP？

**答案**：因为 CP 的实现完全依赖核函数能力：ring 风格要 ring-flash-attn 核、ulysses 风格要能把注意力换成头并行的 all-to-all。`Attention` 层只负责 QKV 投影与排布，序列/头的重排属于核的计算策略，必须下沉到后端（`Attention.apply_cp` 在 [src/olmo_core/nn/attention/__init__.py:L1220-L1240](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/__init__.py#L1220-L1240)，先拒绝 scalable_softmax 再委托后端）。这也是为什么 CP 开启后 RoPE 缓冲必须由外部正确分片后再传入（[src/olmo_core/nn/attention/__init__.py:L1020-L1026](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/__init__.py#L1020-L1026) 会检查）。

**练习 2**：`RingContextParallelStyle.head_stride=2` 意味着什么？什么时候会想调大它？

**答案**：ring attention 每轮环通信默认处理 1 个 K 头和 1 个 V 头（[src/olmo_core/nn/attention/ring.py:L544-L549](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/ring.py#L544-L549)）；`head_stride=2` 表示每轮处理两个 K/V 头，通信次数减半、单次通信量翻倍。KV 头很多（GQA 下 `n_kv_heads` 大）且通信延迟主导时值得调大——这是典型的「次数 vs 单次开销」权衡。

## 5. 综合实践

**任务**：构建同一个 `olmo2_14M` 模型两次——一次用默认（自动选择）后端，一次显式指定 `flash_2` 后端——复制权重后对比前向输出是否一致，并测量两者的速度与显存差异。这是本讲规格里指定的代码实践任务，完整脚本如下。

**操作步骤**（示例脚本，可存为 `tmp_backend_compare.py` 在仓库根目录运行）：

```python
# 示例代码：不属于 OLMo-core 仓库
import time
import torch
from olmo_core.nn.transformer import TransformerConfig
from olmo_core.nn.attention import AttentionBackendName

VOCAB, B, T = 1000, 2, 256
torch.manual_seed(0)

# 模型 A：默认后端（无 flash-attn/无 GPU 时自动回落 torch）
cfg_a = TransformerConfig.olmo2_14M(vocab_size=VOCAB)
model_a = cfg_a.build()

# 模型 B：显式 flash_2（llama_like 的 attn_backend kwarg → AttentionConfig.backend）
cfg_b = TransformerConfig.olmo2_14M(vocab_size=VOCAB, attn_backend=AttentionBackendName.flash_2)
model_b = cfg_b.build()

# 关键：对齐权重，让两者只差「后端」这一个变量
model_b.load_state_dict(model_a.state_dict())

# 确认后端身份（block 的 attention 属性就是 sequence_mixer 插槽的产物）
print("A backend:", type(model_a.blocks[0].attention.backend).__name__)
print("B backend:", type(model_b.blocks[0].attention.backend).__name__)

def get_logits(model, ids):
    out = model(ids)
    return out.logits if not isinstance(out, torch.Tensor) else out

input_ids = torch.randint(0, VOCAB, (B, T))
with torch.no_grad():
    logits_a = get_logits(model_a, input_ids)
    logits_b = get_logits(model_b, input_ids)

diff = (logits_a - logits_b).abs().max().item()
print(f"max |Δlogits| = {diff:.3e}")
try:
    torch.testing.assert_close(logits_a, logits_b, rtol=1e-5, atol=5e-3)  # bf16 容差，同测试文件 L52-L53
    print("后端输出一致 ✔")
except AssertionError as e:
    print("输出不一致：", e)

# 速度对比（GPU 上记得 torch.cuda.synchronize）
def bench(model, n=20):
    model.eval()
    with torch.no_grad():
        model(input_ids)  # 预热
        if torch.cuda.is_available():
            torch.cuda.synchronize(); torch.cuda.reset_peak_memory_stats()
        t0 = time.perf_counter()
        for _ in range(n):
            model(input_ids)
        if torch.cuda.is_available():
            torch.cuda.synchronize()
            peak = torch.cuda.max_memory_allocated() / 2**20
        else:
            peak = None
    dt = (time.perf_counter() - t0) / n * 1000
    return dt, peak

for name, m in [("A(默认)", model_a), ("B(flash_2)", model_b)]:
    ms, peak = bench(m)
    print(f"{name}: {ms:.2f} ms/step" + (f"，峰值显存 {peak:.0f} MiB" if peak else ""))
```

**需要观察的现象与预期结果**：

1. **GPU + flash-attn 机器**：A 的后端是 `FlashAttention2Backend` 或 `TorchAttentionBackend`（取决于环境），B 是 `FlashAttention2Backend`；`max |Δlogits|` 在 bf16 容差内（参考测试用的 `rtol=1e-5, atol=5e-3`，[src/test/nn/attention/attention_test.py:L52-L53](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/nn/attention/attention_test.py#L52-L53)）；flash 后端在 `T=256` 这种短序列下未必更快（核启动开销占比大），把 `T` 调到 2048+ 再观察差距拉大。
2. **纯 CPU 机器**：构建模型 B 时会打印 `Backend is set to flash_2, but GPUs are not available. Defaulting to torch.`（[src/olmo_core/nn/attention/__init__.py:L809-L813](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/__init__.py#L809-L813)），两个模型的后端都是 `TorchAttentionBackend`，输出逐位一致（`diff=0`）。此时练习的重点变成解释「为什么会回落」。
3. **GPU 但未装 flash-attn**：模型 B 构建直接抛 `RuntimeError`（`assert_supported` → `has_flash_attn_2()` 为 False），验证了 4.2 节的依赖守卫。

以上三种分支的运行输出均**待本地验证**。若想跳过模型层、只对比后端核本身，可回到 4.2.4 的 CPU 版实践。

## 6. 本讲小结

- Transformer block 中的注意力挂在 `sequence_mixer` 插槽上（block 属性名为 `attention`），`SequenceMixer` 抽象只约定 TP/CP 接入、FLOPs 估算与初始化；`Attention` 是其标准多头自注意力实现，支持 GQA/MQA，可插 `rope`/`qk_norm`/`gate`/`sinks` 等可选件。
- `Attention.forward` = 投影 + QK norm + RoPE + **SDPA** + 门控 + 输出投影；除 SDPA 外全部与后端无关，SDPA 通过 `sdpa()` 方法一行委托给 `self.backend((q, k, v), ...)`。
- 六种后端（`torch`/`flex`/`flash_2`/`flash_3`/`flash_4`/`te`）共享同一 `forward` 签名，用「构造期 `assert_supported` + 六个能力断言 + 两个掩码标志」声明边界；`AttentionBackendName.build()` 是统一工厂，`AttentionConfig.backend` 是唯一开关。
- 后端选择顺序：显式 `backend` > 旧 `use_flash`（弃用警告）> SWA 自动选 flash_2 > 默认 torch；attention sinks 强制 torch；无 GPU 自动回落 torch。可选依赖全部 try/except 守卫，装错硬件的包也算不可用。
- `flex` 是唯一「快且支持多模态 or/and 掩码」的后端（块稀疏 `mask_mod`）；flash 系与 te 不支持稠密自定义掩码，但提供 KV cache、packed QKV 与上下文并行。
- 上下文并行经 `apply_cp()` 记录在后端上，数据切分由 `ring.py` 的负载均衡器（zig-zag / llama3 / Ulysses）完成，`Attention` 层对这一切透明。

## 7. 下一步学习建议

1. **下一讲 u3-l4（核心组件）**：注意力是「序列混合」，下一讲补齐它的邻居——SwiGLU 前馈、LayerNorm/RMSNorm（含新增的 `output_scale`）、RoPE 的内部实现与 lm_head/embedding，凑齐一个完整 block。
2. **提前看 KV cache 的消费方**：本讲只讲了 `init_kv_cache_manager()` 与后端协商，推理侧如何逐 token 生成将在 u8-l2（生成与 Chat）展开，届时阅读 [src/olmo_core/nn/attention/kv_cache.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/kv_cache.py#L11) 与 generation module。
3. **上下文并行的全貌**：本讲的 `apply_cp`/负载均衡器只是后端侧入口，进程组划分与 `ContextParallelConfig` 在 u7-l2（张量/流水线/上下文/专家并行）统一讲解；想跑分布式验证可先读 [src/test/nn/attention/ring_test.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/nn/attention/ring_test.py#L1)。
4. **动手加深**：把第 5 节综合实践脚本改成 `n_kv_heads=2` 的 GQA 配置（`llama_like` 支持 `n_kv_heads` kwarg），观察 torch 后端 `_repeat_kv` 带来的额外显存；再试试给某几层配 `SlidingWindowAttentionConfig(pattern=[64, -1])`，确认自动后端选择如 4.2.2 所述发生变化。
