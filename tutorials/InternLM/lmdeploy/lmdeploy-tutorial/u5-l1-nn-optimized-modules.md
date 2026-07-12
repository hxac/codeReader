# 优化 nn 模块：attention / norm / rope

## 1. 本讲目标

本讲聚焦 `lmdeploy/pytorch/nn/` 下三个最常被复用的「积木」模块：

- `Attention`（分页注意力，抽象 MHA / GQA / MLA）
- `RMSNorm` / `LayerNorm`（带残差融合的归一化）
- `RotaryEmbedding` / `ApplyRotaryEmb`（旋转位置编码及其多种变体）

学完本讲你应该能够：

1. 说出 `nn/*.py` 里这些模块统一采用的「薄包装 + 委托给 backend 实现」的设计模式，并能指出接口与实现分别住在哪个目录。
2. 看懂 `Attention` 如何用 `num_heads` / `num_kv_heads` / `v_head_size` 三个参数统一表达 MHA、GQA、MLA，以及它在 `forward` 里如何对接 Paged Attention 的 `k_cache` / `v_cache`。
3. 读懂 `RMSNorm` 与 PyTorch 原生 `LayerNorm` 在数学公式与实现上的差异，理解残差融合与 W8A8 分支。
4. 理解 RoPE 的「先建表、再施加」两步走结构，看懂 `build_rotary_params` 如何根据 `config.json` 分发到 default / linear / yarn / llama3 等多种位置编码。
5. 在源码里准确定位这些模块被各模型（如 Llama）复用的位置。

## 2. 前置知识

阅读本讲前，建议你已经建立以下认知（见 u3-l3、u3-l4）：

- **模型 Patch 重写机制**：LMDeploy 不直接跑 HuggingFace 原模型，而是把模型的某些子类整体替换成 `lmdeploy/pytorch/models/` 下的优化实现。但重写类本身**几乎不写数学公式**——它只是「拼装工」，真正的算子藏在 `nn/` 和 `backends/`。
- **积木与拓扑**：每个 `models/*.py` 的重写类，其 `attention` / `norm` / `rope` / `激活` / `线性层` 都来自 `lmdeploy/pytorch/nn/`。本讲就是把这些积木逐一拆开。
- **张量并行（TP）**：多卡推理时，注意力头数、归一化权重等要按 `rank` 切分。本讲会看到 `get_distribute_size` / `chunk_aligned` 这些切分工具如何嵌进积木的构造函数。

另外需要一点基础数学：向量内积、均方根（RMS）、复数旋转矩阵。本讲会用公式给出，不展开推导。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [lmdeploy/pytorch/nn/attention.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/attention.py) | `Attention`（分页注意力包装）与 `FlashAttention`（非分页） |
| [lmdeploy/pytorch/nn/norm.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/norm.py) | `RMSNorm` 与 `LayerNorm`，都支持残差融合 |
| [lmdeploy/pytorch/nn/rotary_embedding.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/rotary_embedding.py) | RoPE 参数解析、建表、施加，含 MRoPE / FoPE 变体 |
| [lmdeploy/pytorch/nn/utils.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/utils.py) | TP 切分工具 `get_distribute_size` / `chunk_aligned` |
| [lmdeploy/pytorch/nn/activation.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/activation.py) | `SiluAndMul` 等，与本讲积木同构，作对照 |
| [lmdeploy/pytorch/backends/base.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/backends/base.py) | `OpType` 枚举与 `OpsBackend` 抽象，定义「算子类型」这张路由表 |
| [lmdeploy/pytorch/backends/selector.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/backends/selector.py) | `get_backend()`：按当前设备选 cuda / ascend / maca / camb 后端 |
| [lmdeploy/pytorch/backends/attention.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/backends/attention.py) | `AttentionMetadata`（注意力元信息）与 `AttentionImpl` 抽象基类 |
| [lmdeploy/pytorch/models/llama.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/llama.py) | 消费者样本：Llama 重写类如何拼装这些积木 |

## 4. 核心概念与源码讲解

### 4.1 统一的设计模式：薄包装 + 委托给 backend

#### 4.1.1 概念说明

`nn/` 下的所有积木模块有一个共同的结构。以一句话概括：

> 每个 `nn.Module` 只负责「**对外接口 + 权重持有 + 张量并行切分**」，真正的数学计算委托给一个由 `backends/` 按当前设备动态选出的 `impl` 对象。

这是一种典型的**桥接模式（Bridge）**：把「算子的接口形状」和「算子的具体实现」分离到两棵继承树。

- 接口树：`lmdeploy/pytorch/nn/*.py`，每个类都是一个 `nn.Module`，定义 `forward` 的输入输出签名。
- 实现树：`lmdeploy/pytorch/backends/<device>/`，每个设备（cuda、ascend、maca、camb）各有一套同名算子的 kernel 实现。

这样设计的好处：新增一个设备后端时，`nn/` 一行都不用改，只需要在 `backends/` 下补一套 `impl`。模型重写类（`models/*.py`）也只认 `nn/` 的接口，因此同一份重写代码能在多设备上跑。

#### 4.1.2 核心流程

每个积木模块的构造与调用都走这五步：

```text
1. 构造时：backend = get_backend()                      # 按当前设备拿到后端类(如 CudaOpsBackend)
2. 构造时：builder  = backend.get_layer_impl_builder(OpType.XXX)  # 按算子类型拿到构建器
3. 构造时：self.impl = builder.build(...)                # 用本层的具体参数实例化实现对象
4. 前向时：forward(...) → self.impl.forward(...)         # 把张量原样转交
5. (可选) 构造时注册可学习参数/缓冲区，并挂 weight_loader 支持权重加载与 TP 切分
```

其中第 1 步是「设备路由」的入口，定义在 [selector.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/backends/selector.py)：它读取当前线程的 `DeviceContext.device_type`，返回对应的 backend 类。

第 2 步的「算子类型」是一张枚举表 `OpType`，它是接口与实现之间唯一的「共同词汇」。

#### 4.1.3 源码精读

**算子类型表 `OpType`**（接口侧与实现侧的共同词汇）：

[lmdeploy/pytorch/backends/base.py:12-41](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/backends/base.py#L12-L41)——枚举了本讲涉及的三类算子：`PagedAttention`、`FlashAttention`、`RotaryEmbedding`、`ApplyRotaryEmb`、`RMSNorm`、`RMSNormW8A8`、`LayerNorm`。每个 `nn/` 积木都用其中一项去问 backend 要实现。

**设备路由 `get_backend`**：

[lmdeploy/pytorch/backends/selector.py:28-43](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/backends/selector.py#L28-L43)——`get_backend()` 默认取当前设备上下文，`_get_backend()` 内部用 `if device_type == 'cuda' ...` 把字符串映射到 backend 类。这就是「同一份 nn 代码、多设备运行」的总开关。

**委托模式的极简样本 `SiluAndMul`**：

[lmdeploy/pytorch/nn/activation.py:7-18](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/activation.py#L7-L18)——这是理解本讲三个模块的最佳样板：构造时 `get_backend()` → `get_layer_impl_builder(OpType.SiluAndMul)` → `builder.build(inplace)` 存进 `self.impl`；`forward` 只有一行 `self.impl.forward(x)`。`Attention`、`RMSNorm`、`ApplyRotaryEmb` 都是这套结构的「加料版」（多了权重、TP 切分、KV cache 等）。

#### 4.1.4 代码实践

**实践目标**：亲眼确认「薄包装 + 委托」这套结构在 `nn/` 下是普遍规律，而不是 `attention.py` 独有。

**操作步骤**：

1. 用编辑器打开 `lmdeploy/pytorch/nn/` 下的 `attention.py`、`norm.py`、`rotary_embedding.py`、`activation.py`。
2. 在每个文件里搜索 `get_layer_impl_builder`，记录它出现在哪个类的 `__init__`、传入的是哪个 `OpType`。
3. 再搜索 `self.impl.forward`，确认每个类的 `forward` 都最终落到 `self.impl.forward`。

**需要观察的现象**：四个文件的 `__init__` 都出现了 `get_backend()` 与 `get_layer_impl_builder(OpType.XXX)`，`forward` 里都能找到 `self.impl.forward(...)` 的调用。

**预期结果**：你能填出下面这张表（答案见 4.1.5）：

| nn 模块 | 请求的 OpType |
| --- | --- |
| `Attention` | `PagedAttention` |
| `FlashAttention` | `FlashAttention` |
| `RMSNorm`（非量化） | `RMSNorm` |
| `RMSNorm`（smooth_quant） | `RMSNormW8A8` |
| `LayerNorm` | `LayerNorm` |
| `ApplyRotaryEmb` | `ApplyRotaryEmb` |
| `SiluAndMul` | `SiluAndMul` |

#### 4.1.5 小练习与答案

**练习 1**：为什么 `nn/` 模块要用 `OpType` 这个枚举去问 backend，而不是直接 `import` 某个 cuda kernel？

**参考答案**：为了让接口（`nn/`）与实现（`backends/<device>/`）解耦。`nn/` 不应知道当前是 cuda 还是 ascend，它只声明「我要一个 `RMSNorm` 实现」；由 `get_backend()` 在运行时把这一声明翻译成具体设备的 kernel。换设备时 `nn/` 与 `models/` 都不用改。

**练习 2**：如果把一个新算子接入 LMDeploy，需要同时改 `OpType` 和某 backend 的 `get_layer_impl_builder`，对吗？

**参考答案**：对。`OpType` 是接口侧与实现侧的共同词汇，新增算子要先在 [base.py 的 OpType](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/backends/base.py#L12-L41) 加一项，再在每个要支持的设备 backend 里注册对应的 `impl_builder`，最后在 `nn/` 写薄包装。

---

### 4.2 Attention：分页注意力的统一抽象

#### 4.2.1 概念说明

`Attention` 是 `nn/` 里最复杂的积木。它把「注意力计算 + Paged Attention 的 KV cache 读写」打包成一个对外统一的模块。它要同时表达三类注意力变体：

- **MHA**（Multi-Head Attention）：Query 头数 = KV 头数。
- **GQA**（Grouped-Query Attention）：Query 头数 > KV 头数，多个 Query 头共享一组 KV。Llama 系列典型。
- **MLA**（Multi-head Latent Attention）：V 的维度可以与 Q/K 不同，用 `v_head_size` 单独指定。DeepSeek 风格。

这三类只用「头数 + 维度」三个参数就区分开了，不需要三套代码——区别只在于传给 `impl` 的数值不同，由 `impl` 内部决定如何复制/共享 KV 头。

#### 4.2.2 核心流程

`Attention` 的注意力得分缩放系数（缺失时由实现侧补默认值）：

\[
\text{scale} = \frac{1}{\sqrt{\text{head\_size}}}
\]

多头数在多卡下的切分（每个 rank 只负责一部分头）：

\[
\text{num\_heads}_{\text{rank}} = \left\lfloor \frac{\text{num\_heads}}{\text{world\_size}} \right\rfloor +
\begin{cases} 1 & \text{rank} < \text{num\_heads} \bmod \text{world\_size} \\ 0 & \text{否则} \end{cases}
\]

构造期与前向期的关键动作：

```text
构造期:
  1. 处理默认值: num_kv_heads 缺省取 num_heads(MHA), v_head_size 缺省取 head_size
  2. _update_num_heads: 按 TP rank 切分 num_heads / num_kv_heads
  3. get_backend().get_layer_impl_builder(OpType.PagedAttention).build(...) → self.impl
  4. 注册 k_scale / v_scale 两个缓冲区(默认 1.0), 用于 FP8 KV cache

前向期 forward(q, k, v, k_cache, v_cache, attn_metadata, ...):
  1. _lazy_init: 若用 alibi 位置编码, 首次调用时按本 rank 的头范围生成 alibi_slopes
  2. 若 quant_policy 为 FP8: 把 k_scale/v_scale 塞进 k_scales_zeros/v_scales_zeros 两个形参
  3. self.impl.forward(q, k, v, k_cache, v_cache, attn_metadata, ...) → attn_output
```

注意第 2 步：`k_scales_zeros` / `v_scales_zeros` 这两个形参名暗示它本是为「带 scale/zero 的量化 KV」设计的，但 FP8 KV cache 目前用的是固定标量 1.0，于是这里**复用**了这两个形参来传标量 scale，注释里也写明了这一点。这是阅读源码时常见的「形参复用」陷阱。

#### 4.2.3 源码精读

**TP 头数切分工具**：

[lmdeploy/pytorch/nn/attention.py:13-18](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/attention.py#L13-L18)——`_update_num_heads` 调用 `get_tp_world_rank('attn')` 拿到注意力通信组的 world_size 与 rank，再对 `num_heads` 和 `num_kv_heads` 分别调用 `get_distribute_size` 做整除取余式切分。这就是「同一份模型、多卡各算一部分头」的入口。

**`get_distribute_size` 的切分细节**：

[lmdeploy/pytorch/nn/utils.py:10-20](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/utils.py#L10-L20)——先按 `align` 对齐，再尽量均分；余数从头几个 rank 各加一份。注意它带 `align` 参数，是为了让切分边界落在某些硬件友好的倍数上（如 8 字节对齐）。

**`Attention.__init__`**：

[lmdeploy/pytorch/nn/attention.py:24-76](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/attention.py#L24-L76)——这是本讲最核心的一段。要点：

- 第 41-44 行：`num_kv_heads` 与 `v_head_size` 的缺省处理，正是 MHA / GQA / MLA 三态的开关。
- 第 46 行：`_update_num_heads` 完成 TP 切分。
- 第 49-66 行：`get_backend()` → `impl_builder` → `self.impl`，标准的委托三连。注意它把 `sliding_window`、`logit_softcapping`、`use_flash_mla`、`learnable_sink`、`block_sparse_size` 等近年新模型（Gemma、DeepSeek、NSA 等）需要的开关都透传给 `impl`——`nn/` 这层只搬运参数，不解释含义。
- 第 75-76 行：注册 `k_scale` / `v_scale` 缓冲区为全 1 标量，注释说明这是为 PyTorch FP8 KV cache 预留的固定 scale。

**`_lazy_init`（alibi 懒初始化）**：

[lmdeploy/pytorch/nn/attention.py:78-91](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/attention.py#L78-L91)——只有启用 alibi（如某些 BLOOM/RoFormer 衍生模型）时才需要生成 `alibi_slopes`。因为 `alibi_slopes` 依赖运行时 device 与本 rank 的头范围，无法在构造期确定，故设计成首次 `forward` 时懒初始化，并用 `self.alibi_ready` 标志位保证只算一次。

**`Attention.forward`**：

[lmdeploy/pytorch/nn/attention.py:93-136](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/attention.py#L93-L136)——要点：

- 入参里的 `k_cache` / `v_cache` 是 Paged Attention 的**物理块缓存**（见 u4-l5），`attn_metadata` 携带 `block_offsets`（逻辑块→物理块映射）等调度信息。
- 第 110-118 行：FP8 分支，复用 `k_scales_zeros` / `v_scales_zeros` 形参传标量 scale，并按需把缓冲区搬到 query 所在 device。
- 第 120-124 行：`nsa_indices`（Native Sparse Attention）与 `s_aux`（learnable sink）是可选进阶特性，按需塞进 `kwargs`。
- 第 125-136 行：把所有张量原样转交 `self.impl.forward`，本层不做任何数学运算。

**`AttentionMetadata`（前向时携带的调度信息）**：

[lmdeploy/pytorch/backends/attention.py:12-23](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/backends/attention.py#L12-L23)——这是个 `@dataclass`，字段如 `is_decoding`（是否纯 decode 阶段）、`block_offsets`（Paged Attention 的块表）、`q_start_loc` / `q_seqlens` / `kv_seqlens`（变长批次的累计偏移与长度）、`quant_policy`（KV cache 量化策略）。它是调度器（u4-l4）与注意力 kernel 之间的「数据信封」。

**`scale` 默认值的真正落点**：

[lmdeploy/pytorch/backends/attention.py:46-47](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/backends/attention.py#L46-L47)——`if scale is None: scale = 1.0 / (head_size**0.5)`。可见 `Attention.__init__` 接受 `scale=None` 的默认值，真正的 \(1/\sqrt{d}\) 在实现基类 `AttentionImpl` 里补齐。

#### 4.2.4 代码实践

**实践目标**：在源码里精确标注 `Attention` 的关键方法，并看清它在 Llama 里是如何被调用的。

**操作步骤**：

1. 打开 [attention.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/attention.py)，给 `Attention` 类标注：
   - 构造期方法：`__init__`、`_lazy_init`
   - 前向方法：`forward`
   - 静态方法：`update_meta_flashmla`
   - 模块级辅助：`_update_num_heads`
2. 打开 [llama.py 的 LlamaAttention](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/llama.py#L24-L109)，定位 `self.attn_fwd = Attention(...)` 的实例化（约 53-58 行），以及它在 `forward` 里被调用的位置（约 94-104 行）。
3. 注意 Llama 传入的 `num_kv_heads=num_key_value_heads`：当 `num_key_value_heads < num_attention_heads` 时即为 GQA，相等时即退化为 MHA——**两种情况用的是同一份代码**。

**需要观察的现象**：

- `Attention` 类内部完全没有 `softmax`、没有 `Q @ K^T`，它只是把张量递给 `self.impl`。
- Llama 的 `forward` 把 `past_key_value[0]` / `past_key_value[1]` 作为 `k_cache` / `v_cache` 传进来，这正是 Paged Attention 的块缓存。

**预期结果**：你能复述「Llama 的注意力前向 = `qkv_proj` 投影 → 拆 QKV → `ApplyRotaryEmb` → `Attention` 读 k_cache/v_cache → `o_proj`」这条链路，并能指出其中 `Attention` 这一步本身不含数学公式，只做委托。

#### 4.2.5 小练习与答案

**练习 1**：一个模型 `num_attention_heads=32`、`num_key_value_heads=8`，在 `tp=2` 下，每个 rank 的 `num_heads` 与 `num_kv_heads` 分别是多少？

**参考答案**：`num_heads`：32/2=16，整除，每 rank 16。`num_kv_heads`：8/2=4，整除，每 rank 4。注意 KV 头数必须能被 world_size 整除，否则 `get_distribute_size` 会把余数分给前几个 rank，可能导致负载不均——这也是为什么 GQA 模型的 KV 头数常取 2 的幂。

**练习 2**：为什么 `k_scale` / `v_scale` 用 `register_buffer` 而不是普通属性？

**参考答案**：`register_buffer` 让它随 `.to(device)` / `.cuda()` 一起搬运、随 `state_dict` 一起序列化，但不会被当作可学习参数优化。`forward` 里第 113-116 行正是依赖它能被 `.to(device=query.device)` 搬运，避免 device 不一致报错。

---

### 4.3 RMSNorm / LayerNorm：归一化与残差融合

#### 4.3.1 概念说明

归一化层（Normalization）的作用是在每层把隐藏状态拉回稳定的数值范围，避免训练/推理时数值爆炸或消失。LMDeploy 的 `nn/norm.py` 提供两种：

- **`RMSNorm`**：Root Mean Square Normalization，Llama / Qwen 等主流模型使用。
- **`LayerNorm`**：经典 Layer Normalization，部分老模型或非 Llama 系使用。

二者的数学区别在于「是否减均值、是否有偏置」。LMDeploy 的实现还多做了一件事——**残差融合**：把「残差相加 + 归一化」合并成一次 kernel 调用，省一次访存。

此外，`RMSNorm` 还内置了 **W8A8（smooth_quant）分支**：当模型做了 W8A8 量化时，归一化输出要转成 8bit，于是它请求的是另一个 `OpType.RMSNormW8A8` 实现。

#### 4.3.2 核心流程

**标准 LayerNorm**（PyTorch 原生语义），对每个位置在隐藏维度 \(H\) 上：

\[
\mu = \frac{1}{H}\sum_{i=1}^{H} x_i, \quad
\sigma^2 = \frac{1}{H}\sum_{i=1}^{H}(x_i - \mu)^2
\]
\[
y_i = \frac{x_i - \mu}{\sqrt{\sigma^2 + \epsilon}} \cdot \gamma_i + \beta_i
\]

**RMSNorm** 省去了减均值（\(\mu\)）与偏置（\(\beta\)），只用均方根：

\[
\text{RMS} = \sqrt{\frac{1}{H}\sum_{i=1}^{H} x_i^2 + \epsilon}
\]
\[
y_i = \frac{x_i}{\text{RMS}} \cdot \gamma_i
\]

少一次均值计算、少一组偏置参数，这就是 RMSNorm 比 LayerNorm 更轻量的原因。

**残差融合**（`forward(x, residual)`）：当上层传入了 `residual`，kernel 先做 \(x \leftarrow x + \text{residual}\)，再对新的 \(x\) 做归一化，并返回 \((y,\ x)\)——新的 \(x\) 即下一层的残差。这把「Add + Norm」两步合成一步。

```text
构造期 RMSNorm.__init__:
  1. get_backend()
  2. 若 quant_config 非空: 取该 prefix 的量化方法, 判断是否 smooth_quant(W8A8)
  3. W8A8 → OpType.RMSNormW8A8; 否则 → OpType.RMSNorm
  4. 若 tp=True: 按 chunk_aligned 切分 hidden_size
  5. register_parameter('weight', create_weight(...))   # 全 1 初始化, requires_grad=False
  6. builder.build(hidden_size, eps[, quant_dtype]) → self.impl
  7. 若 tp=True: 给 weight 挂 weight_loader

前向期 forward(x, residual=None):
  return self.impl.forward(x, self.weight, residual)
```

注意 `create_weight` 里 `requires_grad=False`：推理专用，不参与训练。

#### 4.3.3 源码精读

**`RMSNorm.__init__`**：

[lmdeploy/pytorch/nn/norm.py:16-54](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/norm.py#L16-L54)——要点：

- 第 30-35 行：从 `get_build_model_context().quant_config` 取量化方法，判定是否 `smooth_quant`。这里体现了「量化配置是全局上下文」的设计（见 u3-l5 的 `get_build_model_context`）。
- 第 37-40 行：W8A8 选 `RMSNormW8A8`，否则选 `RMSNorm`。注意 `RMSNorm` 不需要知道量化细节，它只决定「要哪种实现」。
- 第 42-44 行：`tp=True` 时，用 `get_distribute_size` 把 `hidden_size` 按 rank 切分并对齐。这里 `align` 参数允许把切分边界对齐到指定倍数。
- 第 46 行：注册权重（全 1 初始化，见下方 `create_weight`）。
- 第 48-50 行：W8A8 多传一个 `quant_dtype`，普通情况只传 `hidden_size, eps`。
- 第 52-53 行：给 `weight` 挂 `weight_loader`，供权重加载器按 TP 切分加载。

**权重创建**：

[lmdeploy/pytorch/nn/norm.py:62-70](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/norm.py#L62-L70)——默认 `dtype=torch.float16`、`device='cuda'`、全 1 初始化、`requires_grad=False`。注意默认设备直接写 `'cuda'`，这是 PyTorch 后端的固有假设（ascend 等设备会在外层改写）。

**TP 权重加载器**：

[lmdeploy/pytorch/nn/norm.py:56-60](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/norm.py#L56-L60)——`weight_loader` 用 `chunk_aligned` 把整张加载权重按 `align` 对齐地切成 `world_size` 份，取本 `rank` 那一份 `copy_` 进参数。这与 u3-l5 讲的「权重加载契约」对接：线性层等其它模块也是用挂 `weight_loader` 属性的方式声明自己的切分规则。

**`chunk_aligned` 的对齐切分**：

[lmdeploy/pytorch/nn/utils.py:23-36](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/utils.py#L23-L36)——与 `get_distribute_size` 配套：先把总长按 `align` 折算，再尽量均分到 `chunks` 份，最后每份乘回 `align`。`align==1` 时退化为普通 `chunk`。

**`RMSNorm.forward`**：

[lmdeploy/pytorch/nn/norm.py:72-74](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/norm.py#L72-L74)——只有一行：把 `x`、`weight`、`residual` 交给 `self.impl`。残差融合的真实逻辑（先 add 后 norm）在 cuda kernel 里，这层不写。

**`LayerNorm` 对照**：

[lmdeploy/pytorch/nn/norm.py:77-114](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/norm.py#L77-L114)——结构几乎与 `RMSNorm` 同构，差异在：①多一个 `bias`（\(\beta\)），`create_weight` 返回 `(weight, bias)`；②`forward` 多传一个 `self.bias`；③没有量化分支与 TP 切分（`LayerNorm` 在主流大模型里较少用，故未做 W8A8/TP）。二者都支持 `residual` 残差融合。

**消费者样本（Llama 如何用 RMSNorm）**：

[lmdeploy/pytorch/models/llama.py:169-180](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/llama.py#L169-L180)——Llama 的 `LlamaDecoderLayer` 实例化了两个 `RMSNorm`：`input_layernorm` 与 `post_attention_layernorm`，并把 `config.rms_norm_eps` 与 `quant_config` 透传。

[lmdeploy/pytorch/models/llama.py:191-207](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/llama.py#L191-L207)——调用处：第一层（`residual is None`）只做归一化；后续层走「残差融合」分支 `hidden_states, residual = self.input_layernorm(hidden_states, residual)`。这就是「Add + Norm 融合」在模型侧的体现。

#### 4.3.4 代码实践

**实践目标**：通过「读源码 + 跑一段 CPU 可运行的对照脚本」，搞清 `RMSNorm` 与 PyTorch 原生 `LayerNorm` 的实现差异。

**操作步骤**：

1. **读源码部分**：对照 [norm.py 的 RMSNorm](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/norm.py#L13-L74) 与 [norm.py 的 LayerNorm](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/norm.py#L77-L114)，列出三类差异：
   - 数学差异：RMSNorm 不减均值、无 bias（对应公式差异）。
   - 参数差异：LayerNorm 多 `bias`、多 `create_weight` 返回二元组。
   - 能力差异：RMSNorm 有 W8A8 分支与 TP 切分，LayerNorm 没有。
2. **跑对照脚本**（CPU 可运行，验证数学差异；**示例代码**，非项目原有代码）：

   ```python
   import torch
   import torch.nn.functional as F

   torch.manual_seed(0)
   H = 8
   x = torch.randn(2, 3, H)           # (batch, seq, hidden)
   gamma = torch.ones(H)
   eps = 1e-6

   # 1) RMSNorm 手写实现（对应 norm.py 的数学语义）
   rms = torch.sqrt(x.pow(2).mean(dim=-1, keepdim=True) + eps)
   rmsnorm_out = x / rms * gamma

   # 2) PyTorch 原生 LayerNorm（减均值 + 有 bias）
   layernorm_out = F.layer_norm(x, (H,), weight=gamma, bias=torch.zeros(H), eps=eps)

   print('RMSNorm out[0,0]:', rmsnorm_out[0, 0])
   print('LayerNorm out[0,0]:', layernorm_out[0, 0])
   print('差值范数:', (rmsnorm_out - layernorm_out).norm())
   ```

**需要观察的现象**：

- RMSNorm 与 LayerNorm 的输出**不相等**（差值范数明显大于 0），因为前者不减均值。
- 把 RMSNorm 的实现里加一步 `x = x - x.mean(...)`，其结果会逼近 LayerNorm（在 bias=0 时基本一致）。

**预期结果**：你能在源码里指出「RMSNorm 之所以更轻量，是因为省去了 `mean` 与 `bias`」，并理解 `forward(x, residual)` 的残差融合是把 Add 合进 Norm 的一次 kernel。

**说明**：lmdeploy 的 `RMSNorm` 实际计算委托给 cuda/ascend 的 kernel（需初始化 backend 与 GPU），上面的 CPU 脚本只验证**数学语义**，不是调用 lmdeploy 的 `RMSNorm`。若要在 GPU 上对照 lmdeploy 的 `RMSNorm` 输出，需先 `init_backend('cuda')` 并把张量搬到 cuda——该部分**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`RMSNorm` 在什么条件下会请求 `OpType.RMSNormW8A8` 而非 `OpType.RMSNorm`？

**参考答案**：当模型带 `quant_config`，且该 `prefix` 对应的量化方法为 `smooth_quant`（即 W8A8 量化）时，`w8a8_flag` 为真，于是请求 `RMSNormW8A8` 实现，并把 `quant_dtype` 传进去，使归一化输出直接落到 8bit。见 [norm.py:35-50](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/norm.py#L35-L50)。

**练习 2**：为什么 `RMSNorm` 的 `weight` 要挂 `weight_loader`（仅 `tp=True` 时）？

**参考答案**：张量并行下，每个 rank 只持有归一化权重的一部分（hidden_size 被切分）。挂 `weight_loader` 是为了在权重加载阶段（u3-l5）让加载器知道「这个参数要按 `chunk_aligned` 切成 world_size 份、取本 rank 那一份」，从而把磁盘上的完整权重正确分发到各卡。

---

### 4.4 RotaryEmbedding / ApplyRotaryEmb：旋转位置编码

#### 4.4.1 概念说明

旋转位置编码（Rotary Position Embedding, RoPE）是把「位置信息」注入 Q、K 的方式。它的特点是不额外增加向量长度，而是**旋转** Q、K 向量的成对维度。LMDeploy 把 RoPE 拆成两步：

1. **建表（build）**：根据位置 `position_ids` 与频率 `inv_freq` 预先算出每个位置的 `cos` / `sin` 表。这一步在整个模型里只做一次（通常在 `LlamaModel` 顶层），结果下传给每一层。
2. **施加（apply）**：每一层用自己的 Q、K 与收到的 `cos` / `sin` 做旋转。这一步每层都做，由轻量的 `ApplyRotaryEmb` 承担。

之所以拆开，是为了**复用**：`cos` / `sin` 表对所有层都一样，只算一次；而每层的 Q、K 不同，所以旋转要每层各做。

`rotary_embedding.py` 还要处理一个工程问题：不同模型用的 RoPE 变体不同——`default`、`linear`、`dynamic`（NTK）、`yarn`、`longrope`、`llama3`，以及多模态的 `mrope` / `fope`。这些变体都靠 `build_rotary_params` 根据 `config.json` 自动分发。

#### 4.4.2 核心流程

**RoPE 的数学核心**。设头维度为 \(d\)，基为 \(\text{base}\)（默认 10000），则频率向量：

\[
\theta_i = \text{base}^{-2i/d}, \quad i = 0, 1, \dots, d/2-1
\]

对位置 \(m\)，角度 \(\text{freq}_{m,i} = m \cdot \theta_i\)。施加时采用 rotate-half（旋转后半个维度）：

\[
q'_{i} = q_{i}\cos(\text{freq}_{m,i}) - q_{i+d/2}\sin(\text{freq}_{m,i})
\]
\[
q'_{i+d/2} = q_{i+d/2}\cos(\text{freq}_{m,i}) + q_{i}\sin(\text{freq}_{m,i})
\]

对 K 同理。最终注意力内积只依赖 Q、K 的**相对位置** \(m - n\)，这就是 RoPE 注入相对位置信息的原理。

**两步走的工程流程**：

```text
# 模型顶层(LlamaModel.__init__):
self.rotary_emb = build_rotary_embedding_from_config(config)   # 建一个"建表器"

# 模型顶层 forward:
cos, sin = self.rotary_emb(hidden_states, position_ids)        # 算出整批的 cos/sin 表
cos, sin = cos[0], sin[0]
rotary_pos_emb = (cos, sin)

# 每一层(LlamaAttention):
self.apply_rotary_pos_emb = ApplyRotaryEmb()                   # 轻量"施加器"
query, key = self.apply_rotary_pos_emb(query, key, cos, sin)   # 用收到的表旋转本层 Q/K
```

**RoPE 变体分发**（`build_rotary_params`）：

```text
读 config.rope_scaling(或 transformers v5 的 config.rope_parameters)
  ↓ 取 rope_type 字符串
default   → 默认, scaling_factor=1.0
linear    → 线性缩放(factor 直接乘到位置)
dynamic   → Dynamic-NTK(随上下文长度动态调整 base)
yarn      → YaRN(含 mscale 注意力缩放)
longrope  → LongRoPE(长/短两套 factor, su 别名也走这里)
llama3    → Llama3 RoPE(低/高频分段)
  ↓ 再叠加可选的 fope(分数位置) / mrope(多模态三轴) / partial_rotary_factor
返回 params 字典 → 交给 build_rotary_embedding
```

#### 4.4.3 源码精读

**从 config 构建完整 RoPE（模型顶层入口）**：

[lmdeploy/pytorch/nn/rotary_embedding.py:226-238](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/rotary_embedding.py#L226-L238)——`build_rotary_embedding_from_config` 是模型构造时的入口。它从 `config` 读 `head_dim`（缺省取 `hidden_size // num_attention_heads`）、`max_position_embeddings`、`rope_theta`，再调 `build_rotary_params(config)` 补上缩放参数，最后交给 `build_rotary_embedding`。

**RoPE 变体分发 `build_rotary_params`**：

[lmdeploy/pytorch/nn/rotary_embedding.py:144-174](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/rotary_embedding.py#L144-L174)——要点：

- 第 148-155 行：读 `rope_scaling`，取 `rope_type`（旧称 `type`，做了向后兼容）；`mrope` / `fope` 在这里先被映射回 `default`，由后续叠加处理。
- 第 156-163 行：用 `build_funcs` 字典把字符串分发到各自的参数解析函数，这是典型的「策略表」写法，新增一种 RoPE 只需加一个 `_get_xxx_parameters` 函数与一行注册。
- 第 164-165 行：再叠加 `fope` / `mrope`（它们与主 rope 类型正交，可同时存在）。
- 第 167-172 行：处理 `partial_rotary_factor`（只旋转部分维度）。

**YaRN 参数解析（含 mscale）**：

[lmdeploy/pytorch/nn/rotary_embedding.py:48-81](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/rotary_embedding.py#L48-L81)——YaRN 的特色是 `attention_factor`（注意力缩放），其 `get_mscale` 为：

\[
\text{mscale}(\text{scale}) = 0.1 \cdot \text{mscale} \cdot \ln(\text{scale}) + 1.0 \quad (\text{scale} > 1)
\]

该值会乘到 cos/sin 上，缓解长上下文下注意力分布过于平缓的问题。

**`build_rotary_embedding`（产出建表器 impl）**：

[lmdeploy/pytorch/nn/rotary_embedding.py:177-213](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/rotary_embedding.py#L177-L213)——要点：

- 第 190-204 行：标准三连 `get_backend()` → `get_layer_impl_builder(OpType.RotaryEmbedding)` → `builder.build(...)`，把 rope 类型与参数都交给设备 impl。
- 第 195-196 行：`partial_rotary_factor` 缩放 `dim`，只旋转部分维度。
- 第 206-211 行：若带 `fope_params` 或 `mrope_params`，把刚建好的 impl 再包一层（`FopeRotaryEmbedding` / `MRotaryEmbedding`），这是「装饰器式」扩展——基础 impl 负责 `inv_freq`，外层包装负责特殊的轴选择逻辑。

**`ApplyRotaryEmb`（每层的轻量施加器）**：

[lmdeploy/pytorch/nn/rotary_embedding.py:241-273](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/rotary_embedding.py#L241-L273)——要点：

- 第 244-248 行：极简的委托三连（连 `build()` 都不带参数），因为它不持有可学习权重，只做旋转。
- 第 250-273 行：`forward` 对 `cos`/`sin` 的维度做兼容处理——普通 RoPE 的 cos/sin 是 2 维，而 FoPE 是 3 维（多一个 head 维），这里用 `need_reshape` 标志在两种布局间转换，旋转后再 reshape 回去。这是为 FoPE 多模态留的兼容口。

**消费者样本（Llama 如何用 RoPE）**：

[lmdeploy/pytorch/models/llama.py:236-258](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/llama.py#L236-L258)——`LlamaModel` 在顶层建一次 `self.rotary_emb = build_rotary_embedding_from_config(config)`，并在 `forward` 里算出 `cos, sin` 下传。

[lmdeploy/pytorch/models/llama.py:50-91](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/llama.py#L50-L91)——`LlamaAttention` 建一个 `self.apply_rotary_pos_emb = ApplyRotaryEmb()`，并在 QKV 拆分后用它旋转 Q、K。注意 RoPE **只作用于 Q 和 K，不作用于 V**。

#### 4.4.4 代码实践

**实践目标**：验证「建表一次、施加多次」的两步走结构，并跑通一段最小 RoPE 数学实现，理解 rotate-half。

**操作步骤**：

1. **读源码部分**：在 [llama.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/llama.py) 里确认：
   - `build_rotary_embedding_from_config` 只在 `LlamaModel.__init__` 调一次（顶层）。
   - `ApplyRotaryEmb` 在每个 `LlamaAttention.__init__` 各建一个（每层）。
   - `cos, sin` 在 `LlamaModel.forward` 算一次，通过 `rotary_pos_emb = (cos, sin)` 下传到每层。
2. **跑最小 RoPE 实现**（CPU 可运行；**示例代码**，非项目原有代码）：

   ```python
   import torch

   d = 8          # head dim
   base = 10000
   # 1) 建频率表 inv_freq: theta_i = base^(-2i/d)
   inv_freq = 1.0 / (base ** (torch.arange(0, d, 2).float() / d))   # shape (d/2,)
   # 2) 给定位置 m, 算 cos/sin 表
   m = torch.tensor([0, 1, 2])                  # 三个位置
   freqs = torch.outer(m, inv_freq)             # (3, d/2)
   emb = torch.cat((freqs, freqs), dim=-1)      # (3, d)  —— 复制成两半
   cos, sin = emb.cos(), emb.sin()
   # 3) 施加 rotate-half
   q = torch.randn(3, d)
   def rotate_half(x):
       x1, x2 = x.chunk(2, dim=-1)
       return torch.cat((-x2, x1), dim=-1)
   q_rot = q * cos + rotate_half(q) * sin
   print('q after RoPE, position 0 vs 1 夹角随相对位置变化:', q_rot[0] @ q_rot[1])
   ```

**需要观察的现象**：

- 改变位置 `m` 时，`q_rot` 的内积会随**相对位置**变化；绝对位置相同（两个都在 m=0）时旋转后不变。
- 把 `base` 调大（如 1e6），长距离衰减变缓——这正是 `dynamic` / `yarn` 等变体在长上下文场景里调整 `base` 的动机。

**预期结果**：你能复述「RoPE = 建频率表 → 按位置算 cos/sin → rotate-half 施加到 Q/K」，并能在 [rotary_embedding.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/rotary_embedding.py) 里指出「建表」与「施加」分别对应 `OpType.RotaryEmbedding` 和 `OpType.ApplyRotaryEmb` 两个不同算子。

**说明**：lmdeploy 的实际 `inv_freq` / rotate 由设备 kernel 计算，上面 CPU 脚本只复现数学语义。多模态的 MRoPE / FoPE 涉及三轴位置选择，逻辑见 [MRotaryEmbedding](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/rotary_embedding.py#L276-L374)，超出本讲范围，将在 u9-l1（VLM）展开。

#### 4.4.5 小练习与答案

**练习 1**：为什么 RoPE 把「建表」和「施加」拆成 `RotaryEmbedding` 与 `ApplyRotaryEmb` 两个算子，而不是合在一个？

**参考答案**：因为 `cos` / `sin` 表只依赖位置，与具体层无关，整批数据算一次即可被所有层共享；而旋转要作用到每层各自的 Q、K 上，必须每层各做一次。拆开后避免了对每层重复建表，节省计算。对应到代码：建表器在 `LlamaModel` 顶层建一个，施加器在每层各建一个。

**练习 2**：`build_rotary_params` 用「策略表 `build_funcs`」分发 rope 类型，相比 `if/elif` 链有什么好处？

**参考答案**：扩展性更好。新增一种 RoPE 只需写一个 `_get_xxx_parameters` 函数并在字典里加一行 `'xxx': _get_xxx_parameters`，无需改动分发主流程，符合开闭原则。这与 u1-l5 讲的 CLI「类体即注册」是同一种「用数据结构代替分支」的设计思想。

---

## 5. 综合实践

**任务**：画一张「Llama 一个 Decoder Layer 的积木装配图」，并把每个积木对应到本讲的源码位置。

要求：

1. 仿照下图，用文字或画图工具画出 `LlamaDecoderLayer` 的数据流（输入 `hidden_states` + `residual` → 输出 `hidden_states` + `residual`）：

   ```text
   (residual=None 首层) hidden_states ─┐
                                       ├─ input_layernorm(RMSNorm) ─→ self_attn ─┐
   (后续层) hidden_states,residual ────┘                                         │
                                                                                ├─ post_attention_layernorm(RMSNorm, 残差融合) ─→ mlp ─→ 输出
                                            ↑ Attention 内部: qkv_proj → split → ApplyRotaryEmb → Attention(读 k/v_cache) → o_proj
   ```

2. 在图上标注每个积木的「类名 + 所在文件 + 请求的 OpType」，例如：
   - `input_layernorm` → `RMSNorm`（`nn/norm.py`，`OpType.RMSNorm`）
   - `apply_rotary_pos_emb` → `ApplyRotaryEmb`（`nn/rotary_embedding.py`，`OpType.ApplyRotaryEmb`）
   - `attn_fwd` → `Attention`（`nn/attention.py`，`OpType.PagedAttention`）
   - 建表器 `rotary_emb`（在顶层 `LlamaModel`）→ `OpType.RotaryEmbedding`
3. 回答两个问题：
   - 这一层的 `forward` 里，**哪些步骤发生了残差融合**？（提示：看 [llama.py:191-207](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/llama.py#L191-L207)）
   - 如果要把这个模型跑在 ascend 上，`nn/` 下的积木代码需要改吗？为什么？（提示：回顾 4.1 的桥接模式与 `get_backend()`）

**验收标准**：你能不查源码地复述「一个 decoder layer 用了 2 个 RMSNorm、1 个 Attention、1 个 ApplyRotaryEmb，以及若干线性层」，并能解释为何这些 `nn` 积木本身不含数学公式。

## 6. 本讲小结

- **统一模式**：`nn/` 下的 `Attention`、`RMSNorm`、`LayerNorm`、`ApplyRotaryEmb`、`SiluAndMul` 都采用「薄包装 + 委托」——构造时 `get_backend()` → `get_layer_impl_builder(OpType.X)` → `self.impl`，前向时 `self.impl.forward(...)`，把接口与设备实现解耦。
- **Attention**：用 `num_heads` / `num_kv_heads` / `v_head_size` 三个参数统一表达 MHA / GQA / MLA；构造期用 `_update_num_heads` 按 TP 切分头数；前向对接 Paged Attention 的 `k_cache` / `v_cache` 与 `AttentionMetadata`；为 FP8 KV cache 预留了 `k_scale` / `v_scale` 缓冲区。
- **RMSNorm**：相对 LayerNorm 省去了减均值与 bias（公式更轻），支持残差融合（Add+Norm 合一）与 W8A8（smooth_quant）分支，并通过 `weight_loader` + `chunk_aligned` 支持 TP 权重切分。
- **RoPE 两步走**：「建表」(`RotaryEmbedding`，整模型一次) 与「施加」(`ApplyRotaryEmb`，每层一次) 分离；`build_rotary_params` 用策略表分发 default / linear / dynamic / yarn / longrope / llama3 等变体，并可叠加 FoPE / MRoPE 多模态扩展。
- **共同词汇**：`OpType` 枚举是 `nn/` 接口与 `backends/` 实现之间唯一的耦合点；新增算子要同时改 `OpType` 与对应 backend 的 `impl_builder`。
- **承接关系**：这些积木是 u3-l4「模型重写 = 拼装」里被拼装的零件；它们请求的 `impl` 真身住在 `backends/`（u5-l4 详讲），其中线性层等更复杂的积木在 u5-l2 详讲。

## 7. 下一步学习建议

- **u5-l2 线性层与权重量化变体**：本讲只讲了 attention / norm / rope，最庞大的一类积木——线性层（`nn/linear/` 下的 default / awq / w8a8 / blocked_fp8 / lora）还没展开。它们同样遵循「薄包装 + 委托」，但多了一套按 `quant_config` 自动选实现的机制，是下一讲的核心。
- **u5-l4 算子后端分发 backends**：本讲反复出现的 `get_backend()` / `get_layer_impl_builder` 到底如何按「设备 × 算子类型」路由到具体 kernel，将在 u5-l4 用 `selector.py` 与各设备目录讲透。读完那讲你会真正看清「`self.impl` 是谁」。
- **u5-l5 Triton / CUDA Kernel**：如果你想知道 `RMSNormW8A8`、`ApplyRotaryEmb` 的 cuda 实现到底长什么样、Triton kernel 如何写，去 `kernels/` 目录。
- **回头验证**：学完 u5-l4 后，建议回到本讲的 4.1.4 实践表，把每行 `OpType` 对应到 `backends/cuda/` 下的具体 `impl_builder`，确认你理解了「接口→实现」的完整链路。
