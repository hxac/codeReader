# Transformer 总体架构：从 token 到 logits

## 1. 本讲目标

本讲是「单元三：Transformer 模型架构」的第一讲。读完本讲，你应该能够：

1. **跟踪一次 forward 中数据的完整流动路径**：从 `(batch, seq_len)` 的 token ID 矩阵进入模型，到 `(batch, seq_len, vocab_size)` 的 logits（或一个带 loss 的输出）离开模型，中间每一步的张量形状如何变化、代码在哪里发生。
2. **理解 `Transformer` 与 `TransformerBlock` 的职责划分**：顶层类管「骨架」（embedding、block 堆叠、lm_head），block 管「一个计算单元」（注意力子层 + 前馈子层 + 残差流）。
3. **对比 OLMo-core 实现与经典 Transformer 的差异点**：例如 OLMo 2 实际使用的 `reordered_norm` block（norm 放在子层输出而非输入上）、独立成模块的 `ResidualStream`、以及 `self.attention` 字段其实是可替换的「序列混合器」。

本讲只关注**单进程、单卡视角下的前向计算主链路**。并行（`apply_tp` / `apply_fsdp` 等）、注意力后端细节、MoE、初始化分别在单元七、u3-l3、u3-l5、u3-l6 讲解。

## 2. 前置知识

### 2.1 语言模型在做什么

语言模型的本质是一个「下一个 token 预测器」：给定一串 token ID（整数编号，来自分词器），模型为序列中每个位置输出一个覆盖整个词表的概率分布，表示「下一个 token 是谁」。训练时用交叉熵损失把预测分布向真实标签拉近。所以整个模型可以概括为：

\[ \text{logits} = f(\text{input\_ids}) \in \mathbb{R}^{B \times S \times V} \]

其中 \( B \) 是 batch 大小，\( S \) 是序列长度，\( V \) 是词表大小。

### 2.2 残差流（residual stream）

现代 Transformer 的核心思想是**残差连接**：信息像在一条「主干河道」里流淌，每个子层（注意力、前馈）只是向河里「注入」自己的贡献：

\[ h_{\text{out}} = h_{\text{in}} + \text{SubLayer}(h_{\text{in}}) \]

这条不断被累加的主干就叫残差流。它的好处是梯度可以直通底层，让几十层的堆叠成为可能。OLMo-core 把这个「相加」操作做成了一个独立模块 `ResidualStream`，后面会看到为什么。

### 2.3 norm 放在哪里：pre-norm 与它的变体

经典 Transformer（原始论文）把 LayerNorm 放在子层**输出**上（post-norm）；GPT-2/Llama 系列把它放在子层**输入**上（pre-norm），即 `x + Attn(Norm(x))`，训练更稳定。而 OLMo 2 用的是另一种变体：norm 放在子层**输出、残差相加之前**，即 `x + Norm(Attn(x))`。三种摆法在源码里都有对应实现，本讲会用一张表对比。

### 2.4 承接前面几讲

- u1-l4 已经跑通 `examples/llm/train.py`：一次实验 = 五块 Config（model / dataset / data_loader / train_module / trainer）经 `build()` 组装。
- u2-l1 讲过 `Config → ModuleConfig → ModelConfig → TransformerConfig` 继承链与 `build()` 模式：**Config 是图纸，`build()` 是施工队，产出真正的 `nn.Module`**。
- 本讲就是钻进 `TransformerConfig.build()` 产出的那个 `nn.Module` 内部。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/olmo_core/nn/transformer/model.py](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/model.py) | 顶层模型：`Transformer`（Llama 风格）、`NormalizedTransformer`（nGPT）、`MoETransformer`。本讲主角，embedding → blocks → lm_head 的主链路在这里 |
| [src/olmo_core/nn/transformer/block.py](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/block.py) | 各种 `TransformerBlock` 实现：default / reordered_norm / peri_norm / normalized / moe 等，每个 block 含注意力与前馈两个子层 |
| [src/olmo_core/nn/transformer/__init__.py](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/__init__.py) | 子包门面：再导出全部模型类、block 类与 Config，是日常导入入口（`from olmo_core.nn.transformer import TransformerConfig`） |
| [src/olmo_core/nn/residual_stream.py](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/residual_stream.py) | `ResidualStream` 模块：block 里那次「残差相加」的实现 |
| [src/olmo_core/nn/transformer/config.py](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/config.py) | `TransformerConfig` 及其工厂方法（本讲用 `olmo2_14M`）、`resolve_block_configs`（把一份 block 配置展开成每层一份） |
| [src/olmo_core/nn/lm_head.py](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/lm_head.py) | `LMHead` 与 `LMOutputWithLoss`：模型末端的 logits 投影与交叉熵损失 |

## 4. 核心概念与源码讲解

本讲拆成两个最小模块：**4.1 Transformer（顶层骨架）** 和 **4.2 TransformerBlock（残差流上的计算单元）**。

### 4.1 Transformer：模型的顶层骨架

#### 4.1.1 概念说明

`Transformer` 是 OLMo-core 的默认模型实现，类文档自述为「典型的 Llama 风格 transformer」（[model.py:L81-L99](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/model.py#L81-L99)，这段 docstring 同时是全部构造参数的权威说明）。

它解决的问题是：**把「词元 → 向量 → N 层堆叠计算 → 词表分布」这条流水线组织成一个可配置、可并行、可序列化的 `nn.Module`**。它自己只做三件事：

1. 查表得到 embedding（可选缩放、可选 norm）；
2. 依次调用 N 个 block——注意它**不关心** block 内部是注意力还是别的；
3. 把最终隐状态交给 `lm_head` 得到 logits（以及可选的 loss）。

所有「重活」（注意力计算、MoE 路由、损失计算）都委托给子模块，顶层只负责编排。这是理解后续并行机制的关键：正因为顶层只做编排，才可以在不动 `forward` 主链路的情况下，用 `apply_tp` / `apply_fsdp` 等方法把子模块逐个替换成并行版本。

另外两个变体类直接继承它，几乎不改动前向逻辑：

- `NormalizedTransformer`（nGPT，beta 功能）：[model.py:L1303-L1304](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/model.py#L1303-L1304)，要求 block 必须是 `NormalizedTransformerBlock`；
- `MoETransformer`：[model.py:L1395-L1404](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/model.py#L1395-L1404)，只是把 `is_moe` 翻成 `True` 并增加 MoE 相关的指标/专家并行接口。

#### 4.1.2 核心流程

一次 `model(input_ids, labels=labels)` 的主链路（忽略并行与多模态分支）：

```text
input_ids (B, S) 整数
   │
   ├─ ① embeddings 查表                → h: (B, S, d_model)
   │     可选: h = h * embed_scale      （llama_like 系工厂设为 √d_model）
   │     可选: h = embedding_norm(h)    （OLMo2 默认不加）
   │
   ├─ ② for block_idx in 0..n_layers-1:
   │        h = block(h, **kwargs)      → h: (B, S, d_model)   # 形状全程不变
   │
   └─ ③ lm_head(h, labels=labels)      → logits: (B, S, V)
          有 labels 时 → LMOutputWithLoss(logits, loss, ce_loss, ...)
```

伪代码（对应源码 forward 的骨架）：

```python
def forward(self, input_ids, *, labels=None, ...):
    input_ids, labels, all_block_kwargs, per_block_kwargs, lm_head_kwargs = \
        self._prepare_inputs(...)          # 搬运到设备、组装传给 block 的 kwargs
    h = self.embeddings(input_ids)         # (B, S) -> (B, S, d_model)
    if self.embed_scale is not None:
        h = h * self.embed_scale
    for block_key, block in self.blocks.items():
        h = block(h, **all_block_kwargs)   # (B, S, d_model) -> (B, S, d_model)
    if labels is not None:
        lm_head_kwargs["labels"] = labels
    return self.lm_head(h, **lm_head_kwargs)  # logits 或 LMOutputWithLoss
```

两个值得注意的形状事实：

- **残差流的维度自始至终是 `d_model`**：所有 block 的输入输出形状完全一致，这正是「可堆叠」的来源；
- **形状变化只发生在两端**：入口 `(B, S) → (B, S, d_model)`，出口 `(B, S, d_model) → (B, S, V)`。中间是「等宽管道」。

#### 4.1.3 源码精读

**（a）构造函数：三大件 + 一堆「模式开关」**

构造参数见 [model.py:L101-L121](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/model.py#L101-L121)。embedding 的创建在 [model.py:L124-L150](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/model.py#L124-L150)：普通情况就是一个 `nn.Embedding(vocab_size, d_model)`；当 `n_extra_vocab > 0` 时换成 `SplitVocabEmbedding`（把「额外词表行」拆成单独参数，多模态场景使用，本讲不展开）。`embedding_norm` 默认 `None`，即 OLMo 2 风格里 embedding 后**不**接 norm。

block 的组装在 [model.py:L152-L169](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/model.py#L152-L169)，这段代码做了三件事：

```python
block_configs = resolve_block_configs(n_layers=n_layers, block=block,
                                      block_pattern=block_pattern,
                                      block_overrides=block_overrides)
self.blocks = nn.ModuleDict()
for block_idx in range(n_layers):
    self.blocks[str(block_idx)] = self._validate_block(
        block_configs[block_idx].build(d_model=d_model, block_idx=block_idx,
                                       n_layers=n_layers, init_device=init_device,
                                       cache=cache))
```

- `resolve_block_configs`（实现在 [config.py:L2176-L2206](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/config.py#L2176-L2206)）把「一份 block 配置」展开成「每层一份」：单一配置就复制 `n_layers` 份（再按 `block_overrides` 替换特定层）；`block` 是 dict 时按 `block_pattern` 循环取名（这就是「层异构」模型的配置方式）。注意复制的是 **Config 对象**而非 Module——每层才在 `build()` 时各自实例化，block 之间**不共享参数**。
- `self.blocks` 用的是 `nn.ModuleDict`（字符串作键，`"0"`、`"1"`、…）而不是 `nn.ModuleList`。字符串命名让每层在 `state_dict`、FSDP 包裹、激活检查点替换（`register_module`）里都有稳定可寻址的名字。
- 所有 block 共享同一个 `BufferCache`（构造函数 [model.py:L124](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/model.py#L124)），用于跨层复用非参数缓冲区（如 attention mask），避免每层重复生成。

lm_head 的创建与权重共享在 [model.py:L170-L176](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/model.py#L170-L176)：`tie_word_embeddings=True` 时调用 `_tie_weights()`（[model.py:L201-L208](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/model.py#L201-L208)），直接令 `lm_head.w_out.weight = embeddings.weight`——输入查表和输出投影共用一张 \( V \times d_{\text{model}} \) 的表，省下一份大参数。

构造函数末尾还有一批 `_pp_enabled` / `_tp_enabled` / `_fsdp_enabled` / `_compile_enabled` 等标志位（[model.py:L184-L199](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/model.py#L184-L199)），全部初始化为关闭，之后由训练模块按配置调用对应的 `apply_*` 方法打开。同时这里**提前缓存**了 `num_params` / `num_non_embedding_params`——因为流水线并行可能会在之后把 embedding 或 lm_head 从本进程的模型上删掉，那时再数参数就数不全了。

**（b）forward：主链路只有三十行**

完整签名见 [model.py:L687-L735](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/model.py#L687-L735)，docstring 写明：**`labels` 为 `None` 时返回 logits，否则返回损失**。

第一步 `_prepare_inputs`（调用点 [model.py:L758-L776](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/model.py#L758-L776)，实现 [model.py:L462-L685](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/model.py#L462-L685)）在单卡视角下做的事很少：把 `input_ids`/`labels` 搬到模型设备，并把损失相关参数（`ignore_index`、`loss_reduction` 等）打包进 `lm_head_kwargs`。它的漫长篇幅几乎都在处理上下文并行（序列分片）和文档内掩码——本讲可安全跳过，单元七再回来。

embedding 段在 [model.py:L824-L833](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/model.py#L824-L833)：

```python
# Get embeddings but pass-through for non-existent layers to allow easy
# pipeline parallel configuration.
if input_embeddings is not None:
    h = move_to_device(input_embeddings, self.device)   # 多模态：外部已算好 embedding
else:
    h = self.embeddings(input_ids) if self.embeddings is not None else input_ids
    if self.embeddings is not None and self.embed_scale is not None:
        h = h * self.embed_scale
    if self.embedding_norm is not None:
        h = self.embedding_norm(h)
```

注意那行注释：`self.embeddings` 为 `None` 时**直接把 input_ids 当作 h 传下去**。这不是 bug，而是为流水线并行准备的「直通」设计——中间阶段的模型没有 embedding 层，收到的本来就是上一阶段传来的隐状态。`embed_scale` 是可选乘子，`llama_like` 系工厂方法会把它设为 \( \sqrt{d_{\text{model}}} \)（见 [config.py:L2001](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/config.py#L2001)），让 embedding 的数值尺度与初始化标准差解耦。

block 循环在 [model.py:L840-L847](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/model.py#L840-L847)：

```python
# Run each block.
for block_key, block in self.blocks.items():
    block_idx = int(block_key)
    block_kwargs = per_block_kwargs.get(block_idx, {})
    if self.compile_enabled:
        mark_dynamic(h, (0, 1), strict=False)   # 为 torch.compile 标记动态维度
    h = block(h, **all_block_kwargs, **block_kwargs)
```

`all_block_kwargs` 是发给**所有** block 的参数（如多模态注意力掩码、RoPE 缓冲区），`per_block_kwargs` 是只发给**特定层**的参数（如上下文并行的逐层 RoPE 分片）。顶层完全不解析这些参数——**收进来、分出去，仅此而已**。

lm_head 段在 [model.py:L849-L861](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/model.py#L849-L861)：

```python
if self.lm_head is not None:
    ...
    if labels is not None:
        lm_head_kwargs["labels"] = labels
    return self.lm_head(h, **lm_head_kwargs)
else:
    return h    # 流水线并行的中间阶段：把隐状态原样交给下一阶段
```

交叉熵损失不是在 `Transformer.forward` 里手写的，而是由 `LMHead` 一并完成。`LMHead.forward` 接收 `h` 与 `labels`，返回一个 `LMOutputWithLoss` 命名元组（定义见 [lm_head.py:L192-L198](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/lm_head.py#L192-L198)）：字段有 `logits`（可为 `None` 以省显存）、`loss`（要优化的总损失，可含 z-loss）、`ce_loss`（纯交叉熵，仅用于记录日志）。把损失放进 lm_head 而不是训练循环，好处是**张量并行下「分片 logits + 聚合损失」可以作为一个整体优化**（u7-l2 会看到这正是最近一次修复涉及的 `CrossEntropyLoss`）。

**（c）`apply_*` 方法群：并行的挂钩**

`forward` 之外，`Transformer` 的另一大块是与并行/优化相关的入口方法：`apply_fp8`（[model.py:L863-L881](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/model.py#L863-L881)）、`apply_pp`（L883）、`apply_tp`（[model.py:L900-L962](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/model.py#L900-L962)）、`apply_cp`（L964）、`apply_activation_checkpointing`（L1006）、`apply_compile`（L1132）、`apply_fsdp`（L1151）、`apply_ddp`（L1229）。本讲只需记住调用顺序的约定：**先 build 出普通模型，再由训练模块按配置逐个调用这些方法原地改造**（如 `apply_compile` 的 docstring 要求它必须在 FSDP/DDP 之前调用）。细节留给单元七。

#### 4.1.4 代码实践

**实践目标**：亲手跑一次 `Transformer.forward`，验证「形状只在前向两端变化」的论断，并区分带/不带 labels 两种返回值。

**操作步骤**（示例代码，非项目原有代码；需已按 u1-l2 完成开发安装）：

```python
# build_and_forward.py —— 示例代码
import torch
from olmo_core.nn.transformer import TransformerConfig

# 1. 用预置工厂方法造一个 14M 级别的小模型配置（d_model=128, n_layers=4, n_heads=8）
config = TransformerConfig.olmo2_14M(vocab_size=1000)

# 2. build：Config -> nn.Module
model = config.build(init_device="cpu")

# 3. 按项目惯例初始化权重（build 只负责搭结构，不填数值）
model.init_weights(device=torch.device("cpu"))

B, S, V = 2, 16, 1000
input_ids = torch.randint(0, V, (B, S))
labels = torch.randint(0, V, (B, S))

with torch.no_grad():
    logits = model(input_ids)              # 不带 labels
    print("logits:", tuple(logits.shape))  # 预期 (2, 16, 1000)

    out = model(input_ids, labels=labels)  # 带 labels
    print(type(out).__name__)              # 预期 LMOutputWithLoss
    print("out.logits:", tuple(out.logits.shape))
    print("out.loss:", out.loss.item(), "out.ce_loss:", out.ce_loss.item())
```

**需要观察的现象**：`logits` 形状 `(2, 16, 1000)`；`out` 是 `LMOutputWithLoss`；`loss` 与 `ce_loss` 数值接近（没配 z_loss 时两者相等）；未训练模型的 loss 大约在 \( \ln V \approx 6.9 \) 附近（均匀分布的交叉熵）。

**预期结果**：以上全部成立。形状由源码直接保证：`embeddings(input_ids)` 产出 `(B, S, 128)`，四个 block 保持不变，`lm_head` 投影到 `(B, S, 1000)`。loss ≈ ln(1000) 属于「待本地验证」的数值预期（取决于初始化细节，允许有小幅偏差）。

#### 4.1.5 小练习与答案

**练习 1**：`TransformerConfig.build()` 会调用 `Transformer.init_weights()` 吗？

**答案**：不会。`build()` 只负责按 Config 实例化 `Transformer`（见 [config.py:L399-L436](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/config.py#L399-L436)，其中没有任何 `init_weights` 调用）；权重初始化是训练模块在 build 之后显式调用 `model.init_weights(...)` 完成的（[model.py:L322-L460](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/model.py#L322-L460)）。这样设计是因为分布式场景要先在 meta 设备上建结构、落位后再统一初始化（回忆 u1-l4）。

**练习 2**：为什么 `forward` 里 `self.embeddings` 和 `self.lm_head` 都要做 `is not None` 检查？什么情况下它们会缺失？

**答案**：为流水线并行（PP）准备的。PP 会把模型沿层切开，只有第一个阶段持有 embedding、只有最后一个阶段持有 lm_head；中间阶段的 `forward` 收到的 `input_ids` 实际是上一阶段的隐状态，需要原样穿过（embedding 直通分支 [model.py:L829](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/model.py#L829)），计算完再把 `h` 原样返回（[model.py:L860-L861](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/model.py#L860-L861)），由 PP 调度器在阶段间搬运。相关机制在 u7-l2 展开。

**练习 3**：`n_layers=4`、单一 block 配置时，模型里有几个 `TransformerBlock` 实例？它们共享参数吗？

**答案**：4 个实例，互不共享参数。`resolve_block_configs` 只是把**同一份 Config** 复制 4 份（[config.py:L2197-L2203](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/config.py#L2197-L2203)），随后循环里每层各自 `build()` 出独立的 Module。结构相同（超参一样），但权重各自随机初始化——`init_weights` 会带着 `block_idx` 调用（[model.py:L396-L403](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/model.py#L396-L403)），部分初始化方案（如按层缩放）正是利用了这个索引。

### 4.2 TransformerBlock：残差流上的两个子层

#### 4.2.1 概念说明

如果说 `Transformer` 是「楼层」，`TransformerBlock` 就是「标准化的一层楼」：**接收一个 `(B, S, d_model)` 隐状态，还回一个同形状隐状态**。它内部固定装有两部「电梯井」：

1. **注意力子层**（序列混合）：让序列中每个位置看到其他位置的信息；
2. **前馈子层**（逐位置变换）：对每个 token 独立做一次升维-非线性-降维。

每个子层配一个 norm 和一条残差连接。block 家族的全部差异，本质上只是回答三个问题：**norm 摆在子层的输入侧还是输出侧？残差怎么加？前馈是稠密 MLP 还是 MoE？**

一个容易踩的坑：block 里那个字段叫 `self.attention`，但它**不一定是注意力**。构造它的入参叫 `sequence_mixer`（序列混合器），源码注释明确说明命名只为兼容旧 checkpoint，字段里装的可能是 `GatedDeltaNet` 这类线性循环/卷积型序列混合器（[block.py:L129-L134](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/block.py#L129-L134)）。抽象层次上，「让序列内位置交换信息」的模块都插在同一个插槽上——这是 u3-l3（注意力与可插拔后端）的主题。

#### 4.2.2 核心流程

默认 `TransformerBlock`（pre-norm 风格）的单层计算：

```text
x (B, S, d_model)
 ├── 子层 1：注意力
 │    a = attention(attention_norm(x))      # norm 在输入侧（pre-norm）
 │    h = attention_residual_stream(x, a)   # h = x + α·dropout(a)
 ├── 子层 2：前馈
 │    f = feed_forward(feed_forward_norm(h))
 │    y = feed_forward_residual_stream(h, f)  # y = h + α·dropout(f)
 └── y (B, S, d_model)
```

写成公式：

\[ y = h + \alpha \cdot \mathrm{Dropout}(\mathrm{FFN}(\mathrm{Norm}(h))), \quad h = x + \alpha \cdot \mathrm{Dropout}(\mathrm{Attn}(\mathrm{Norm}(x))) \]

其中 \( \alpha \) 是残差分支缩放系数（`ResidualStream` 的 `alpha`，默认 1.0）。

#### 4.2.3 源码精读

**（a）基类：一份「block 合同」**

[block.py:L32-L58](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/block.py#L32-L58) 定义 `TransformerBlockBase`：唯一的抽象 `forward` 约定了输入输出都是 `(B, S, d_model)` 张量；随后的 [block.py:L60-L93](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/block.py#L60-L93) 用一组抽象方法规定了每个 block 必须实现的并行接口：`apply_tp` / `apply_cp` / `apply_fsdp` / `num_flops_per_token`（`apply_pp` 与 `apply_compile` 有默认实现）。这就是「任何 block 变体都能被 `Transformer.apply_tp` 统一改造」的合同基础。

**（b）默认 block 的成员清单**

`TransformerBlock.__init__`（[block.py:L109-L143](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/block.py#L109-L143)）装配出六个子模块：

| 成员 | 来源 | 作用 |
| --- | --- | --- |
| `attention` | `sequence_mixer.build(d_model, layer_idx=block_idx, ...)` | 序列混合（通常是多头注意力 + RoPE） |
| `attention_norm` | `layer_norm.build(d_model)` | 注意力前的 norm（通常 RMSNorm） |
| `attention_residual_stream` | `ResidualStream(alpha, dropout, masked_dropout)` | 注意力残差相加 |
| `feed_forward` | `feed_forward.build(d_model)` | 前馈（通常 SwiGLU MLP） |
| `feed_forward_norm` | `layer_norm.build(d_model)` | 前馈前的 norm |
| `feed_forward_residual_stream` | `ResidualStream(...)` | 前馈残差相加 |

注意所有子模块都由** Config 的 `build()`** 创建（u2-l1 的模式在这里层层复现），norm 配置被用了两次（注意力侧与前馈侧各一份实例）。

**（c）forward：六行核心逻辑**

[block.py:L145-L165](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/block.py#L145-L165)：

```python
def forward(self, x, *, loss_div_factor=None, drop_mask=None, **kwargs):
    del loss_div_factor
    h = self.attention_residual_stream(
        x, self.attention(self.attention_norm(x), **kwargs), drop_mask=drop_mask
    )
    return self.feed_forward_residual_stream(
        h, self.feed_forward(self.feed_forward_norm(h)), drop_mask=drop_mask
    )
```

读法：第一行是「norm → attention → 残差相加」，第二行是「norm → FFN → 残差相加」。`**kwargs` 原样转交给 `attention`——顶层 `forward` 收集的那些掩码/RoPE 参数最终就是在这一步流进注意力后端的。`del loss_div_factor` 表明普通稠密 block 用不到这个参数（它是给 MoE 的负载均衡辅助损失做归一化用的）。

**（d）OLMo 2 实际用的 block：`ReorderedNormTransformerBlock`**

关键差异点来了。`TransformerConfig.olmo2_14M()` 的默认 `block_name` 是 `TransformerBlockType.reordered_norm`（[config.py:L587-L598](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/config.py#L587-L598)），也就是说 **OLMo 2 系列模型默认并不走上面那个 pre-norm block**，而是走 [block.py:L305-L321](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/block.py#L305-L321) 的这个子类：

```python
def forward(self, x, *, loss_div_factor=None, **kwargs):
    del loss_div_factor
    h = self.attention_residual_stream(x, self.attention_norm(self.attention(x, **kwargs)))
    return self.feed_forward_residual_stream(h, self.feed_forward_norm(self.feed_forward(h)))
```

对比一下调用顺序：`self.attention_norm(self.attention(x))` —— **norm 套在子层输出外面**，先算注意力再 norm，然后才加进残差流。docstring（[block.py:L305-L310](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/block.py#L305-L310)）对此的表述是「norm 应用在注意力输出而非输入上」。这种摆法让流入残差流的每个分支贡献都被 norm 约束在稳定尺度上，属于 OLMo 2 的架构选择。子类**只重写了 `forward`、完全复用父类的 `__init__`**——同一组零件，不同的组装顺序，这是继承复用的干净示范。

**（e）残差相加本身：`ResidualStream`**

那次「相加」被做成了独立模块（[residual_stream.py:L7-L29](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/residual_stream.py#L7-L29)）。核心实现（[residual_stream.py:L49-L65](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/residual_stream.py#L49-L65)）：

```python
def forward(self, residual, x, drop_mask=None):
    if self.masked_dropout > 0.0 and self.training:
        if drop_mask is None:
            raise ValueError("'drop_mask' is required when 'masked_dropout' > 0")
        return torch.add(residual, self._masked_dropout(x, drop_mask), alpha=self.alpha)
    return torch.add(residual, self.dropout(x), alpha=self.alpha)
```

它等价于 `torch.add(residual, x, alpha=self.alpha)`，即 \( \text{residual} + \alpha \cdot x \)。独立成模块的理由 docstring 写得很清楚：**为了有一个固定的挂载点**——`GAPMonitorCallback` 之类的回调可以在 `ResidualStream` 上注册 hook 来观测残差流的幅值；同时它顺带支持了 `alpha` 缩放和 Molmo2 的 `masked_dropout`（按 token 掩码选择性地丢残差贡献，[residual_stream.py:L31-L47](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/residual_stream.py#L31-L47) 的注释还解释了为什么必须逐**元素**采样而不是逐 token 采样——逐 token 丢整条残差贡献会导致训练发散）。这是「无参数也要模块化」的代表性设计。

**（f）block 变体家族一览**

| 类 | norm 摆放 | 前馈 | 位置 |
| --- | --- | --- | --- |
| `TransformerBlock` | pre-norm（子层输入侧） | 稠密 | [block.py:L96-L241](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/block.py#L96-L241) |
| `LayerNormScaledTransformerBlock` | pre-norm，输出乘 \( 1/\sqrt{l} \)（\(l\) 为 1 起层的层号） | 稠密 | [block.py:L244-L302](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/block.py#L244-L302) |
| `ReorderedNormTransformerBlock` | **子层输出侧**（OLMo 2 默认） | 稠密 | [block.py:L305-L321](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/block.py#L305-L321) |
| `PeriNormTransformerBlock` | 输入、输出**两侧**都 norm | 稠密 | [block.py:L324-L373](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/block.py#L324-L373) |
| `NormalizedTransformerBlock` | nGPT：全流程 L2 归一化 + 可学习插值系数 | 稠密 | [block.py:L388-L459](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/block.py#L388-L459) |
| `MoETransformerBlock` | pre-norm | **MoE**（稠密 FFN 换成专家组） | [block.py:L524-L688](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/block.py#L524-L688) |

其中 MoE 版的 forward（[block.py:L598-L608](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/block.py#L598-L608)）值得扫一眼，它是本讲两个模块的「合体对照」：

```python
def forward(self, x, *, loss_div_factor=None, **kwargs):
    h = x + self.dropout(self.attention(self.attention_norm(x), **kwargs))
    return h + self.dropout(
        self.feed_forward_moe(self.feed_forward_norm(h), loss_div_factor=loss_div_factor)
    )
```

结构完全同构（norm → 子层 → 加残差），只是前馈换成了 `feed_forward_moe`，残差用最朴素的 `x +`（没走 `ResidualStream`），并且把 `loss_div_factor` 传下去用于辅助损失归一化。这一层留给 u3-l5 精读。

所有变体都通过 [src/olmo_core/nn/transformer/__init__.py:L1-L24](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/__init__.py#L1-L24) 这个门面统一导出，并通过 `TransformerBlockConfig` 的 `name` 字段（`TransformerBlockType` 枚举）在 `build()` 时选择具体类——又一次「Registrable/工厂选择实现」模式（u2-l2）。

#### 4.2.4 代码实践

**实践目标**：用同一个小配置分别构建 pre-norm 与 reordered_norm 两种 block 的模型，通过 forward hook 证明「block 输入输出形状不变」，并观察两者前向输出的差异。

**操作步骤**（示例代码，非项目原有代码）：

```python
# block_compare.py —— 示例代码
import torch
from olmo_core.nn.transformer import TransformerConfig, TransformerBlockType

def build(block_name):
    config = TransformerConfig.olmo2_14M(vocab_size=1000, block_name=block_name)
    model = config.build(init_device="cpu")
    model.init_weights(device=torch.device("cpu"))
    return model

# 1. 看配置层：默认 block 类型确实是 reordered_norm
cfg = TransformerConfig.olmo2_14M(vocab_size=1000)
print("block name:", cfg.block.name)          # 预期 block TransformerBlockType.reordered_norm

# 2. 两种 block 各建一个模型
m_pre  = build(TransformerBlockType.default)         # pre-norm
m_reor = build(TransformerBlockType.reordered_norm)  # OLMo 2 风格

# 3. 在第 0 个 block 的每个子模块上挂 forward hook，打印输出形状
def shape_hook(name):
    def fn(module, args, output):
        if isinstance(output, torch.Tensor):
            print(f"{name:>28s} -> {tuple(output.shape)}")
        else:
            print(f"{name:>28s} -> {type(output).__name__}")
    return fn

blk = m_pre.blocks["0"]
blk.attention.register_forward_hook(shape_hook("block0.attention"))
blk.attention_norm.register_forward_hook(shape_hook("block0.attention_norm"))
blk.feed_forward.register_forward_hook(shape_hook("block0.feed_forward"))
blk.attention_residual_stream.register_forward_hook(shape_hook("block0.residual_stream(ATT)"))

x = torch.randint(0, 1000, (2, 16))
with torch.no_grad():
    _ = m_pre(x)

# 4. 两个模型的 block 类名对照
print(type(m_pre.blocks["0"]).__name__)    # 预期 TransformerBlock
print(type(m_reor.blocks["0"]).__name__)   # 预期 ReorderedNormTransformerBlock
```

**需要观察的现象**：`attention_norm` 与 `attention` 的输出都是 `(2, 16, 128)`；`residual_stream` 的输出同样是 `(2, 16, 128)`——d_model 全程不变；两个模型第 0 层的类名分别是 `TransformerBlock` 与 `ReorderedNormTransformerBlock`。

**预期结果**：如上。由于两者权重是独立随机初始化的，且 norm 摆放不同，`m_pre(x)` 与 `m_reor(x)` 的 logits **不应**逐元素相等（这正是「同一个 Config 骨架、不同 block 装配顺序」的行为证据）。具体数值待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：不翻 `block.py`，仅凭 `TransformerBlock.__init__` 的装配顺序写出 pre-norm block 的 forward 表达式，再对照源码验证。

**答案**：`h = x + α·Dropout(Attn(Norm(x)))`；`y = h + α·Dropout(FFN(Norm(h)))`。与 [block.py:L160-L165](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/block.py#L160-L165) 一致（`α=1`、`Dropout=Identity` 时退化为最简形式）。

**练习 2**：`ReorderedNormTransformerBlock` 与 `TransformerBlock` 的 `__init__` 有何不同？参数初始化会因此不同吗？

**答案**：前者完全没有自己的 `__init__`，直接继承父类（[block.py:L305-L321](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/block.py#L305-L321) 只有 `forward`）。两者的子模块集合与初始化完全相同，区别仅在前向时 norm 的套用位置（输入侧 vs 输出侧）。

**练习 3**：为什么 MoE block 的 forward 里要把 `loss_div_factor` 继续传给 `feed_forward_moe`，而稠密 block 里直接 `del loss_div_factor`？

**答案**：MoE 的路由器会产生一个**负载均衡辅助损失**，它需要用有效 token 数做归一化才能在不同 batch 大小/并行度之间可比；`loss_div_factor` 就是这个归一化因子，由训练侧统一计算后逐层下传（`Transformer._prepare_inputs` 会把它同时放进 `lm_head_kwargs` 与 `all_block_kwargs`，[model.py:L521-L524](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/model.py#L521-L524)）。稠密 block 没有辅助损失，自然用不到。细节在 u3-l5。

## 5. 综合实践

把本讲两个模块串起来的任务：**手绘数据流图 + 用 hook 全程追踪 `olmo2_14M` 的张量形状**。

1. **手绘**：不看代码，先凭记忆画出 `input_ids → embeddings → block×4 → lm_head → logits/loss` 的数据流图，在每条边上标注形状（用符号 `B/S/D/V`）和发生跳变的代码位置（文件 + 行号）。然后对照 [model.py:L824-L861](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/model.py#L824-L861) 补漏。
2. **机验**：在 `embeddings`、全部 4 个 `blocks["0".."3"]`、`lm_head` 上注册 forward hook，跑一个 `(2, 16)` 的 batch，把每站输出形状打印成表，与手绘图逐条核对。参考骨架（示例代码）：

```python
import torch
from olmo_core.nn.transformer import TransformerConfig

model = TransformerConfig.olmo2_14M(vocab_size=1000).build(init_device="cpu")
model.init_weights(device=torch.device("cpu"))

def hook(name):
    def fn(module, args, output):
        shape = tuple(output.shape) if isinstance(output, torch.Tensor) else "非张量输出"
        print(f"{name:<16s} {shape}")
    return fn

model.embeddings.register_forward_hook(hook("embeddings"))
for key, block in model.blocks.items():
    block.register_forward_hook(hook(f"block[{key}]"))
model.lm_head.register_forward_hook(hook("lm_head"))

x = torch.randint(0, 1000, (2, 16))
with torch.no_grad():
    out = model(x, labels=torch.randint(0, 1000, (2, 16)))
print("loss:", out.loss.item())
```

3. **思考题（选做）**：把 `vocab_size` 从 1000 换成 100278（OLMo 2 真实词表大小），哪些边的形状会变、参数量会涨多少？参数量可用 `model.num_params` 与 `model.num_non_embedding_params`（[model.py:L1262-L1272](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/model.py#L1262-L1272)）直接验证——预期能看到 embedding 与 lm_head 占了大头，这也解释了为什么存在 `tie_word_embeddings` 选项。

预期输出形如（具体数值待本地验证）：

| 站点 | 形状 |
| --- | --- |
| embeddings | (2, 16, 128) |
| block[0..3] | (2, 16, 128) |
| lm_head（无 labels 返回） | (2, 16, 1000) |

## 6. 本讲小结

- **顶层 `Transformer` 只做编排**：embedding 查表（可选 `embed_scale` 缩放与 norm）→ 依次过 N 个 block → `lm_head` 同时产出 logits 与交叉熵损失（`LMOutputWithLoss`）。形状只在两端变化，中间是 `d_model` 等宽的残差流。
- **block 的合同是「同形进同形出」**：`TransformerBlockBase` 用抽象方法约定了 forward 形状与一组 `apply_tp/cp/fsdp` 并行接口；顶层因此可以用统一循环驱动任意 block 变体。
- **每个子层 = norm + 序列混合/前馈 + ResidualStream**：残差相加被独立成无参数模块，为回调观测（如 GAPMonitor）和 Molmo2 的 masked dropout 提供挂载点。
- **OLMo 2 默认不是 pre-norm**：`olmo2_*` 工厂方法默认选择 `reordered_norm` block，norm 套在子层**输出**上再加残差；pre-norm、LayerNorm 缩放、Peri-LN、nGPT、MoE 各有对应实现，全部复用同一组装配。
- **为并行预留的直通设计**：`embeddings` / `lm_head` 可为 `None`（流水线并行中间阶段），`self.attention` 实为可插拔的 `sequence_mixer` 插槽；`apply_*` 方法群在 build 之后由训练模块按配置原地改造模型。
- **blocks 用 `nn.ModuleDict` 存储、每层独立 build**：同一份 block Config 复制 N 份再逐层实例化，层间不共享参数，但可用 `block_pattern` / `block_overrides` 配置异构层。

## 7. 下一步学习建议

本讲刻意回避了三块「内部」：**配置字段全貌**、**注意力内部**、**权重初始化**。建议按顺序继续：

1. **u3-l2（TransformerConfig 与预置架构工厂方法）**：读懂 `d_model` / `n_heads` / `mlp_hidden_size` 等字段如何映射到本讲看到的骨架上，并学会用 kwargs 改造预置配置。
2. **u3-l3（注意力机制与可插拔后端）**：钻进 `self.attention` 插槽，理解 `MultiHeadAttention`、RoPE 与 flash attention 等后端的选择机制。
3. **u3-l6（模型初始化）**：搞清 `init_weights` 里 `InitMethod` 如何按 `block_idx` 缩放各层初始化标准差。
4. 如果你想先验证本讲的并行挂钩，可以跳到 **u7-l1（数据并行）** 看 `apply_fsdp` / `apply_ddp` 如何被训练模块调用，再回来读本讲的 `apply_*` 方法群。
