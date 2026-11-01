# u4-l2 解码器基础件：Self/Cross-Attention、FFN 与位置编码

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 Transformer 解码器的序列格式为什么是 `(L, B, C)`，以及 `query_pos` / `pos` 两种位置编码是如何「加在特征上」参与注意力的。
2. 独立读懂并使用 `SelfAttentionLayer`、`CrossAttentionLayer`、`FFNLayer` 三个积木类的 `forward`，理解它们与 `nn.MultiheadAttention` 的关系。
3. 区分 post-norm（先子层、后归一化）与 pre-norm（先归一化、后子层）两条执行路径，并画出两种模式的数据流图。
4. 理解 `PositionEmbeddingSine` 如何把二维网格坐标编码成与特征同形状的正弦位置编码，包括归一化坐标、温度分母与 sin/cos 交错排列。

本讲只讲「积木」，不讲把这些积木串成 masked attention 主循环的完整解码器——那是下一讲（u4-l3）的内容。

## 2. 前置知识

### 2.1 注意力回顾

注意力机制的本质是「按相关性加权取值」。给定查询 \(Q\)、键 \(K\)、值 \(V\)，缩放点积注意力为：

\[
\mathrm{Attention}(Q, K, V) = \mathrm{softmax}\!\left(\frac{QK^\top}{\sqrt{d_k}}\right)V
\]

- \(Q\) 的每一行问「我要找什么」；
- \(K\) 的每一行回答「我这里有什么」；
- 匹配分数决定从 \(V\) 中按多大比例取用每一行。

多头注意力（Multi-Head Attention）把 \(d\) 维特征切成 \(h\) 份，各自独立做注意力再拼接，让不同头关注不同的匹配模式。本项目中 `hidden_dim=256`、`nheads=8`，每个头 32 维。

### 2.2 PyTorch 的 nn.MultiheadAttention

本项目不手写注意力公式，而是直接使用 `torch.nn.MultiheadAttention`。它有一个容易踩坑的约定：**默认输入格式是「序列长度优先」，即 `(L, B, E)`**（L=序列长度，B=batch，E=特征维度）。这与卷积常用的 `(B, C, H, W)` 完全不同，是本讲反复出现的主题。

它的调用签名是 `attn(query, key, value, attn_mask=..., key_padding_mask=...)`，返回 `(输出, 注意力权重)` 两个值，所以源码里到处是 `[0]`。

`attn_mask` 的语义：**True 的位置被禁止注意**（bool 掩码）。这是 Mask2Former「masked attention」的实现载体。

### 2.3 残差连接与 LayerNorm

每个子层（注意力或 FFN）外面都套着「残差 + 归一化」的结构：

- **残差连接**：`x = x + Sublayer(x)`，让梯度可以直通，深层网络才训得动。
- **LayerNorm**：对每个 token 的特征向量做标准化，稳定各层分布。

「归一化放在子层之前还是之后」就是 pre-norm 与 post-norm 之分，本讲 4.4 详解。

### 2.4 位置编码为什么必要

注意力本身对输入顺序完全无感——把序列打乱，输出只是相应换位。要让模型知道「每个 token 在图像网格中的位置」，必须显式注入位置信息。常见做法是把一个位置向量加到该 token 的特征上。本讲涉及两套位置信息：

- **图像特征侧的 `pos`**：由 `PositionEmbeddingSine` 从特征图网格计算（4.5）。
- **查询侧的 `query_pos`**：可学习的 `nn.Embedding`，每个 query 一个专属位置向量（本讲只讲它如何被使用，其来源在 u4-l3）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py) | 本讲主角。文件开头依次定义 `SelfAttentionLayer`、`CrossAttentionLayer`、`FFNLayer` 三个积木，之后是 `MLP` 与 `MultiScaleMaskedTransformerDecoder`（下一讲主角） |
| [mask2former/modeling/transformer_decoder/position_encoding.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/position_encoding.py) | `PositionEmbeddingSine`：把二维网格坐标变成正弦位置编码 |
| [mask2former/config.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/config.py) | `MODEL.MASK_FORMER.*` 下与本讲相关的默认值：`HIDDEN_DIM=256`、`NHEADS=8`、`DIM_FEEDFORWARD=2048`、`PRE_NORM=False` |

三个积木的使用范围（帮你定位本讲在整个拼图中的位置）：

- 它们**只**被同文件中的 `MultiScaleMaskedTransformerDecoder`（v2 解码器）消费；v1 的 `StandardTransformerDecoder` 用的是 DETR 式的整体 `Transformer`，不用这三个类。
- `PositionEmbeddingSine` 则是三处共享的公共件：v1 解码器、v2 解码器、两个像素解码器（[fpn.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/fpn.py) 的 Transformer encoder 版、[msdeformattn.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/pixel_decoder/msdeformattn.py) 的可变形注意力 encoder）都在用——u3-l4/u3-l5 已见过它。
- 视频包 `mask2former_video` 里有一份近乎相同的拷贝（`video_mask2former_transformer_decoder.py` 中同样定义了这三个类），差异留到 u9-l2 分析。

## 4. 核心概念与源码讲解

### 4.1 (L, B, C) 序列格式与位置注入：with_pos_embed

#### 4.1.1 概念说明

Transformer 是为一维序列设计的，而 Mask2Former 的输入是二维特征图。把 `(B, C, H, W)` 变成序列的方式是 **flatten**：每个像素一个 token，得到 `(B, C, H*W)`；再为了适配 `nn.MultiheadAttention` 的默认格式，把序列维换到最前面，得到 `(H*W, B, C)`——这就是 `(L, B, C)` 约定。

在这个约定下，位置的注入方式非常朴素：**逐元素相加**。`with_pos_embed(tensor, pos)` 就是 `tensor + pos`，两者形状完全相同。关键的语义分工是：

- 加位置的版本只用于构造注意力的 **Q 和 K**（决定「匹配在哪发生」）；
- **V 用原始特征**（决定「被取用的内容是什么」）。

这样位置影响「谁注意谁」，但不污染「传递的内容」。

#### 4.1.2 核心流程

```text
图像特征 (B, C, H, W)
    │ flatten(2)              → (B, C, H*W)   每像素一个 token
    │ permute(2, 0, 1)        → (H*W, B, C)   即 (L, B, C)
    ▼
src（图像 token 序列）  ⊕ pos（同形状的正弦位置编码，仅用于 K/Q）
query（Q 个可学习向量）⊕ query_pos（同形状的可学习位置编码，仅用于 Q/K）
```

query 侧则简单得多：`query_feat.weight` 形状 `(Q, C)`，`unsqueeze(1).repeat(1, bs, 1)` 直接广播成 `(Q, B, C)`——天然就是序列格式。

#### 4.1.3 源码精读

**位置注入的三行实现**（三个积木类里各有一份完全相同的定义）：

[mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py:L37-L38](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L37-L38)
`pos` 为 `None` 时原样返回（不注入位置），否则逐元素相加。

**图像特征如何变成 (L, B, C) 序列**——看 v2 解码器 `forward` 开头的预处理（本讲只需关注形状变换，语义留到 u4-l3）：

[mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py:L373-L380](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L373-L380)
对每个尺度：记录 `size_list`；用 `self.pe_layer(x[i], None)` 生成位置编码并 `flatten(2)`；特征过 `input_proj` 拉齐通道、加 `level_embed` 后同样 `flatten(2)`；最后两者都 `permute(2, 0, 1)` 变成 `(H*W, B, C)`。

**query 侧的广播**：

[mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py:L384-L386](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L384-L386)
`query_feat`（承载内容的可学习 query 特征）与 `query_embed`（可学习 query 位置编码）两个 `(Q, C)` 嵌入表，各自沿 batch 维广播成 `(Q, B, C)`。注释里的 `QxNxC` 即 (Q, batch, C)。

#### 4.1.4 代码实践

**实践目标**：亲手体会 `(B, C, H, W)` → `(L, B, C)` 的变换与「加位置只进 Q/K」的语义。

1. 在装好 u1-l2 环境的机器上，进入项目根目录，新建脚本 `practice_pe_add.py`（放在仓库外或 `/tmp` 均可，不要放进源码目录）：

   ```python
   # 示例代码
   import torch
   x = torch.randn(2, 256, 24, 24)          # (B, C, H, W) 假想的单尺度特征
   seq = x.flatten(2).permute(2, 0, 1)      # → (H*W, B, C)
   print(seq.shape)                          # torch.Size([576, 2, 256])

   pos = torch.randn(576, 2, 256)            # 同形状位置编码
   q = k = seq + pos                          # 位置只进 Q/K
   v = seq                                    # V 保持原始特征
   print((q == k).all().item(), v.shape)
   ```

2. 运行 `python practice_pe_add.py`。

**需要观察的现象**：第一行打印 `(576, 2, 256)`——24×24=576 个 token；`q == k` 为 `True`（自注意力中 Q、K 同源），`v` 仍是 `(576, 2, 256)` 且未加 `pos`。

**预期结果**：形状输出如上所示（确定性）。这是纯张量操作，不依赖项目环境。

#### 4.1.5 小练习与答案

**练习 1**：为什么 V 不加位置编码，而 Q/K 要加？

**参考答案**：注意力权重由 \(QK^\top\) 决定，位置加在 Q/K 上就足以让「匹配打分」感知空间关系；V 是被加权取用的内容本身，若加位置会让位置信号混入逐层传递的特征中，破坏残差结构中「内容」的纯净性。这也是 DETR 以来的通行做法。

**练习 2**：`(L, B, C)` 与 `(B, L, C)` 各自适合什么场景？

**参考答案**：`nn.MultiheadAttention` 默认要求 `(L, B, E)`（可传 `batch_first=True` 切换）；`(B, L, C)` 则是 LayerNorm、Linear 等「最后一维是特征」模块的常规格式，也便于与卷积/einsum 世界衔接。Mask2Former 选择序列优先，于是预测头里会出现 `transpose(0, 1)` 把 query 序列转回 batch 优先（见 `forward_prediction_heads`）。

---

### 4.2 SelfAttentionLayer：query 之间的自注意力

#### 4.2.1 概念说明

`SelfAttentionLayer` 让 **Q 个 query 互相通信**：每个 query 既当 Q 也当 K，同时充当 V。它解决的问题是「同一层的各个掩码预测之间交换信息、避免互相抢地盘」——直觉上，多个 query 若各自独立地从图像取证，很容易都盯上同一个显眼目标；自注意力让它们彼此「看到对方」，从而隐式地分工。

它是一个薄封装：真正的计算全部交给 `nn.MultiheadAttention`，类本身只负责位置注入、残差、归一化和 pre/post 两条路径的分派。

#### 4.2.2 核心流程

post-norm 路径（`normalize_before=False`，默认）：

```text
q = k = tgt + query_pos          # 位置注入，Q/K 同源
v = tgt                          # 原始特征
tgt2 = MHA(q, k, v)
tgt  = LayerNorm(tgt + Dropout(tgt2))     # 先残差，后归一化
```

pre-norm 路径（`normalize_before=True`）：

```text
tgt2 = LayerNorm(tgt)            # 先归一化
q = k = tgt2 + query_pos
v = tgt2
tgt2 = MHA(q, k, v)
tgt  = tgt + Dropout(tgt2)       # 残差在外，不再归一化
```

#### 4.2.3 源码精读

**构造函数**：

[mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py:L19-L30](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L19-L30)
组装一个 `nn.MultiheadAttention(d_model, nhead)`、一个 `LayerNorm`、一个 `Dropout`，记录 `normalize_before` 开关。`activation` 参数被存了但**在本类的 forward 中并未使用**（只有 FFNLayer 用激活函数）——这是一个容易误读的点。

**参数初始化**：

[mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py:L32-L35](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L32-L35)
对所有维度大于 1 的参数做 Xavier 均匀初始化（会覆盖 `nn.MultiheadAttention` 自身的默认初始化）。

**post-norm 前向**：

[mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py:L40-L50](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L40-L50)
第 44 行 `q = k = self.with_pos_embed(tgt, query_pos)` 一行完成「Q/K 同源 + 位置注入」；第 45-46 行调用 MHA，`value=tgt` 不带位置，`[0]` 取输出张量（丢弃注意力权重）；第 47-48 行残差 + 归一化。

**pre-norm 前向**：

[mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py:L52-L62](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L52-L62)
先 `norm(tgt)` 再做注意力，输出直接残差相加，**没有**末端 LayerNorm。

**分派入口**：

[mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py:L64-L72](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L64-L72)
`forward` 本身不算任何东西，只按 `self.normalize_before` 二选一。这是「一个类支持两种架构变体」的典型写法。

#### 4.2.4 代码实践

**实践目标**：验证 `SelfAttentionLayer` 的输入输出形状不变，且 `query_pos` 确实影响输出。

1. 编写脚本（示例代码）：

   ```python
   # 示例代码；需在项目根目录、u1-l2 环境下运行
   import torch
   from mask2former.modeling.transformer_decoder.mask2former_transformer_decoder import (
       SelfAttentionLayer,
   )

   torch.manual_seed(0)
   layer = SelfAttentionLayer(d_model=256, nhead=8, dropout=0.0, normalize_before=False)
   tgt = torch.randn(10, 2, 256)                # (L=10 个 query, B=2, C=256)
   out = layer(tgt)
   print("无位置编码:", out.shape)

   query_pos = torch.randn(10, 2, 256)
   out_pos = layer(tgt, query_pos=query_pos)
   print("加 query_pos:", out_pos.shape)
   print("两次输出是否相同:", torch.allclose(out, out_pos))
   ```

2. 运行 `python practice_self_attn.py`（注意：`import mask2former...` 会触发整个包的注册与 CUDA 算子导入，需先完成 u1-l2 的编译）。

**需要观察的现象**：两次输出形状都是 `(10, 2, 256)`；最后一行打印 `False`——同层同权重下，加不加 `query_pos` 结果不同，证明位置编码确实参与了计算。

**预期结果**：形状与布尔值结论如上（确定性）；具体数值待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：自注意力里 `q = k` 是同一个张量，会不会导致注意力退化？

**参考答案**：不会退化成恒等。每个 query 对所有 query（含自己）算相似度后 softmax 加权求和，输出是「全序列的加权聚合」。自己与自己内积通常最大，但其他 query 仍按相似度获得权重，信息交换照常发生。

**练习 2**：为什么解码器主循环里调用 `SelfAttentionLayer` 时传的是 `tgt_mask=None`（见 [L407-L411](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L407-L411)）？

**参考答案**：因为 masked attention 的掩码只约束 query 对**图像 token** 的注意（下一讲详述），而 query 之间（固定 Q=100 个、无 padding）不需要任何掩码；`tgt_key_padding_mask=None` 同理——序列里没有无效 token。

---

### 4.3 CrossAttentionLayer：query 对图像特征的注意力

#### 4.3.1 概念说明

`CrossAttentionLayer` 让 **query 从图像特征中取证据**：query 是注意力的发起方（Q），图像 token 序列（代码中叫 `memory`，DETR 的命名传统）是被查询的 K/V。Q 个 query 各自决定「看图像的哪些位置、取多少」，输出仍是 `(Q, B, C)`——形状由 query 决定，与 memory 长度无关。

它与自注意力的实现差异只有两点：

1. Q 来自 `tgt + query_pos`，K 来自 `memory + pos`（两侧用**各自**的位置编码）；
2. 多了 `memory_mask` 参数——这正是 Mask2Former 的核心创新 **masked attention** 的入口：每个 query 只被允许注意自己预测掩码覆盖的区域。掩码如何生成是下一讲（u4-l4）的内容，本讲只要掌握「接口长什么样、语义是什么」。

#### 4.3.2 核心流程

post-norm 路径：

```text
Q = tgt + query_pos                 # (Q_len, B, C)
K = memory + pos                    # (S_len, B, C)，S_len = 当前尺度 H*W
V = memory
tgt2 = MHA(Q, K, V, attn_mask=memory_mask)
tgt  = LayerNorm(tgt + Dropout(tgt2))
```

masked attention 的掩码语义：`memory_mask` 中为 `True` 的 (query, 图像token) 对被禁止注意。形状约定为 `(B*num_heads, Q_len, S_len)`（3D 形式，每个 batch 每个头一份）。

#### 4.3.3 源码精读

**post-norm 前向**：

[mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py:L98-L110](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L98-L110)
第 103-106 行一次调用完成：`query=with_pos_embed(tgt, query_pos)`（query 侧位置），`key=with_pos_embed(memory, pos)`（图像侧位置），`value=memory`（不带位置），掩码透传给 `attn_mask`。对比自注意力：那里 Q/K 同源，这里两侧来源不同。

**pre-norm 前向**：

[mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py:L112-L124](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L112-L124)
与 4.2 的 pre 路径同构：先 norm tgt，再交叉注意力，残差输出。

**解码器主循环中的调用点**（看接口如何被用，顺序细节留待 u4-l3）：

[mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py:L396-L405](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L396-L405)
每层先做交叉注意力：`output`（query 序列）作 `tgt`，`src[level_index]`（当前轮转尺度的图像序列）作 `memory`，上一预测头生成的 `attn_mask` 作 `memory_mask`，`pos=pos[level_index]`、`query_pos=query_embed` 两侧位置齐备。注释明确 `memory_key_padding_mask=None`——不对 padding 区域做屏蔽。

#### 4.3.4 代码实践

**实践目标**：验证交叉注意力输出形状跟 query 走，并用一个手工构造的掩码体会「True = 禁止注意」。

1. 编写脚本（示例代码）：

   ```python
   # 示例代码；需在项目根目录、u1-l2 环境下运行
   import torch
   from mask2former.modeling.transformer_decoder.mask2former_transformer_decoder import (
       CrossAttentionLayer,
   )

   torch.manual_seed(0)
   layer = CrossAttentionLayer(d_model=256, nhead=8, dropout=0.0, normalize_before=False)
   tgt = torch.randn(10, 2, 256)        # 10 个 query
   memory = torch.randn(576, 2, 256)    # 24×24 的图像 token
   pos = torch.randn(576, 2, 256)
   query_pos = torch.randn(10, 2, 256)

   out = layer(tgt, memory, pos=pos, query_pos=query_pos)
   print("无掩码输出:", out.shape)

   # 构造 (B*num_heads, Q, S) 的 bool 掩码：每个 query 只允许注意前 100 个图像 token
   S, B, h, Q = 576, 2, 8, 10
   mask = torch.ones(B * h, Q, S, dtype=torch.bool)
   mask[:, :, :100] = False             # False = 允许注意
   out_masked = layer(tgt, memory, memory_mask=mask, pos=pos, query_pos=query_pos)
   print("带掩码输出:", out_masked.shape)
   ```

2. 运行脚本。

**需要观察的现象**：两个输出都是 `(10, 2, 256)`——长度由 query 数决定；带掩码版本与无掩码版本数值不同。

**预期结果**：形状如上（确定性）；数值差异待本地验证。若把掩码全设为 `True`（query 一个 token 都不能看），softmax 将在空集上归一化，输出不可用——这正是下一讲主循环里 [L398](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L398) 那行「全 True 行兜底置 False」存在的原因。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `pos` 加在 K 上而不加在 V 上？`query_pos` 加在 Q 上，是否也应该有个「query 侧的 K」？

**参考答案**：交叉注意力里图像侧只提供 K 和 V，位置加在 K 上即可让打分感知空间位置，V 保持内容纯净。query 侧只提供 Q（query 之间不互为 K/V），所以 `query_pos` 只需要加在 Q 上；query 之间的交互由 4.2 的自注意力层负责，那里 `query_pos` 同时进了 Q 和 K。

**练习 2**：`memory_mask` 为什么是 `(B*num_heads, Q, S)` 而不是 `(Q, S)`？

**参考答案**：`nn.MultiheadAttention` 的 `attn_mask` 支持 2D `(L, S)` 与 3D `(N*num_heads, L, S)` 两种。2D 意味着所有 batch、所有头共享同一掩码；而 Mask2Former 的掩码是**逐 query 逐样本**从各自的预测掩码生成的，必须用 3D 才能表达「第 b 个样本的第 q 个 query 只看它自己的区域」。

---

### 4.4 FFNLayer 与 pre-norm / post-norm 两条执行路径

#### 4.4.1 概念说明

`FFNLayer` 是逐 token 的两层感知机：先把 256 维升到 `dim_feedforward`（默认 2048）维、过激活，再降回 256 维，外面套残差与归一化。注意力擅长「跨 token 路由信息」，FFN 擅长「对每个 token 独立做非线性变换」，两者交替堆叠构成标准 Transformer 层。

**pre-norm vs post-norm** 是本讲最重要的架构对比，三个积木类用同一个 `normalize_before` 开关统一控制：

| | post-norm（`normalize_before=False`） | pre-norm（`normalize_before=True`） |
| --- | --- | --- |
| 归一化位置 | 子层**之后**：`x = LN(x + Sub(x))` | 子层**之前**：`x = x + Sub(LN(x))` |
| 出处 | 原版 Transformer、DETR、MaskFormer/Mask2Former 默认 | GPT 系、ViT 等深层网络 |
| 特点 | 每层输出都被归一化，分布稳定但深层梯度路径经过 LN | 残差主干上没有任何变换，梯度可无阻碍直通底层，更易训深 |
| 本项目配置 | `cfg.MODEL.MASK_FORMER.PRE_NORM = False`（默认即 post-norm） | 置 `True` 即整体切换 |

注意区分两个名字相近的类：`FFNLayer`（本讲，解码器层内的逐 token 前馈）与 `MLP`（同文件 L192-L204，预测头中把 decoder 输出映射为 mask 嵌入的三层感知机，见 u4-l4）。

#### 4.4.2 核心流程

两种模式的数据流图（以 FFNLayer 为例，注意力层同构）：

```text
post-norm（normalize_before=False）      pre-norm（normalize_before=True）

 tgt ──┬──────────────┐                   tgt ──┬────────────────┐
       │              │                         │                │
       ▼              │                         ▼                │
   Linear1 (256→2048) │                     LayerNorm            │
       ▼              │                         ▼                │
     ReLU             │                    Linear1               │
       ▼              │                         ▼                │
    Dropout           │                       ReLU               │
       ▼              │                         ▼                │
   Linear2 (2048→256) │                     Dropout               │
       ▼              │                         ▼                │
    Dropout           │                    Linear2                │
       ▼              │                         ▼                │
      (＋)＜──────────┘                      Dropout               │
       ▼                                      ▼                   │
   LayerNorm                                (＋)＜────────────────┘
       ▼                                      │
     out ────────────── 带末端 LN ──          out ── 无末端 LN（主干干净）
```

解码器主循环中三种子层的固定顺序是 **cross-attention → self-attention → FFN**（[L396-L416](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L396-L416)），每一类各有 `dec_layers` 份、逐层一一配对。

#### 4.4.3 源码精读

**FFNLayer 构造**：

[mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py:L140-L153](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L140-L153)
`linear1` 升维到 `dim_feedforward`，`linear2` 降回 `d_model`；同样持有 `norm` 与 `normalize_before`。注意它也有个从未被用到的 `with_pos_embed`（L160-L161）——FFN 不涉及注意力，天然不需要位置注入，这两个方法只是从兄弟类复制时留下的痕迹，阅读时可以忽略。

**两条路径**：

[mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py:L163-L178](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L163-L178)
`forward_post`：`linear2(dropout(activation(linear1(tgt))))` 后残差再 LayerNorm；`forward_pre`：先 LayerNorm 再过两层 Linear，残差输出。`forward` 同样只做 `normalize_before` 二选一。

**激活函数选择**：

[mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py:L181-L189](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L181-L189)
`_get_activation_fn` 支持 relu/gelu/glu 三选一；解码器构建时全部使用默认的 `relu`。

**三类层如何被统一构建、开关如何传入**：

[mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py:L280-L310](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L280-L310)
三个 `nn.ModuleList`（self-attn / cross-attn / FFN）各建 `num_layers` 份，每份的 `normalize_before` 都取自同一个 `pre_norm` 变量——所以 pre/post-norm 是**整网一刀切**的开关，不存在按层混配。

**默认配置值**：

[mask2former/config.py:L40-L48](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/config.py#L40-L48)
`NHEADS=8`、`DIM_FEEDFORWARD=2048`、`PRE_NORM=False`、`HIDDEN_DIM=256`。即默认配置训练出的 Mask2Former 是 **post-norm** 网络；`pre_norm` 经 [L356](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L356) 从配置读入。

#### 4.4.4 代码实践

**实践目标**：用同一组权重状态分别以 pre/post-norm 前向，确认「路径不同、结果不同、形状相同」。

1. 编写脚本（示例代码）：

   ```python
   # 示例代码；需在项目根目录、u1-l2 环境下运行
   import torch
   from copy import deepcopy
   from mask2former.modeling.transformer_decoder.mask2former_transformer_decoder import FFNLayer

   torch.manual_seed(0)
   post = FFNLayer(d_model=256, dim_feedforward=2048, dropout=0.0, normalize_before=False)
   pre = FFNLayer(d_model=256, dim_feedforward=2048, dropout=0.0, normalize_before=True)
   pre.load_state_dict(deepcopy(post.state_dict()))   # 两层共享同一套权重

   tgt = torch.randn(10, 2, 256)
   print("post-norm:", post(tgt).shape)
   print("pre-norm :", pre(tgt).shape)
   print("输出差异 (max abs):", (post(tgt) - pre(tgt)).abs().max().item())
   print("post-norm 输出均值/方差:",
         post(tgt).mean().item(), post(tgt).var(unbiased=False).item())
   ```

2. 运行脚本，对照 4.4.2 的两张数据流图逐行核对代码。

**需要观察的现象**：两个输出形状同为 `(10, 2, 256)`；差异的 max abs 是一个明显的正数（不是 0）；post-norm 输出的方差接近 1（因为末端就是 LayerNorm），pre-norm 输出方差通常偏离 1。

**预期结果**：形状确定；具体数值与方差待本地验证（依赖随机种子与初始化，但「post 方差≈1、pre 方差≠1」的定性结论可提前预期）。

#### 4.4.5 小练习与答案

**练习 1**：pre-norm 网络往往需要在**最末端**额外加一次 LayerNorm（本项目的 `decoder_norm`，[L312](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L312)）。为什么 post-norm 不需要？

**参考答案**：post-norm 每层输出都刚经过 LayerNorm，末层输出天然归一化；pre-norm 的残差主干从头到尾没有任何归一化操作，各层输出分布随深度漂移，所以要在进入预测头之前补一次 `decoder_norm` 把分布拉回。注意本项目无论哪种模式都构建了这个末端 norm（`forward_prediction_heads` 第一行调用它）。

**练习 2**：把 `PRE_NORM` 设为 `True` 需要改几处代码？为什么？

**参考答案**：一处都不用改，只改配置：yaml 里 `MODEL.MASK_FORMER.PRE_NORM: True` 或命令行 `MODEL.MASK_FORMER.PRE_NORM True`。因为 `from_config` 会把它读进 `pre_norm`，三个 ModuleList 构建时统一传入，`forward` 自动分派到 pre 路径。这正是「开关集中、分派内聚」设计的好处。注意：切换后旧权重不兼容，需重新训练。

---

### 4.5 PositionEmbeddingSine：二维正弦位置编码

#### 4.5.1 概念说明

`PositionEmbeddingSine` 回答的问题是：**图像 token 的 `pos` 从哪来**。它把每个像素的网格坐标 \((x, y)\) 编码成一组固定（无可学习参数）的正弦/余弦值，拼接后与特征图同形状地逐元素加到特征上。

它的三个关键设计：

1. **二维拆分**：`hidden_dim` 通道对半分，前一半编码 y 坐标、后一半编码 x 坐标（`N_steps = hidden_dim // 2`）。
2. **多频率**：不同通道用不同波长的正弦波——低频通道刻画粗位置（在图像哪个角落），高频通道刻画细位置（局部几个像素的差别）。波长由温度参数 \(T=10000\) 按几何级数铺开，与原版 Transformer 一致。
3. **归一化坐标**：`normalize=True` 时坐标先除以该方向的像素总数，映射到 \((0, 1]\)，再乘 `scale=2π`。好处是**尺度不变**——同一物体在 1/8 与 1/32 特征图上，若有效区域相同，归一化后的坐标编码一致；这也是它比绝对像素坐标更适合多尺度特征的原因。本项目的两处使用（像素解码器、Transformer 解码器）都开 `normalize=True`。

#### 4.5.2 核心流程

对特征图 `(B, C, H, W)`（通道数任意，只用形状与设备）：

```text
1. mask（可选，True=padding）→ not_mask；mask=None 时视为全部有效
2. y_embed = not_mask 沿 H 前缀累加（cumsum）   → (B, H, W)，值 1..H
   x_embed = not_mask 沿 W 前缀累加             → (B, H, W)，值 1..W
3. normalize=True：各自除以最后一个有效坐标（≈H 或 W）→ (0,1]，再乘 2π
4. 分母 dim_t[i] = T^(2·(i//2)/num_pos_feats)   → 共 num_pos_feats 个频率
5. pos_x[..., 偶] = sin(x/dim_t)，pos_x[..., 奇] = cos(x/dim_t)，y 同理
6. pos = concat(pos_y, pos_x) → permute → (B, 2*num_pos_feats, H, W)
```

第 i 个通道使用的角频率由分母决定：

\[
\text{dim\_t}_i = T^{\frac{2\lfloor i/2 \rfloor}{N}}, \quad T = 10000,\ N = \text{num\_pos\_feats}
\]

偶数通道取 \(\sin\)、相邻奇数通道取 \(\cos\)，交错排列；波长范围从 \(2\pi\)（i=0,1）到 \(2\pi T\)（i 最大）。由于 cumsum 从 1 开始计数，归一化后第一个像素的坐标是 \(2\pi/H\) 而非 0。

mask 的用途：对 padding 区域，`not_mask=0` 使 cumsum 停在最后一个有效坐标上（padding 位置复用边缘坐标），保证不同 batch 内图像尺寸不一致时坐标仍然「对齐右下角」。本项目 v2 解码器调用时传 `None`（无 padding 语义），但像素解码器一侧使用 mask（见 u3-l5）。

#### 4.5.3 源码精读

**构造与参数校验**：

[mask2former/modeling/transformer_decoder/position_encoding.py:L18-L27](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/position_encoding.py#L18-L27)
`num_pos_feats` 是**每个方向**的编码通道数（总输出 = 2 倍）；`scale` 默认 `2π`；传 `scale` 必须同时开 `normalize`，否则报错。

**坐标生成与归一化**：

[mask2former/modeling/transformer_decoder/position_encoding.py:L29-L38](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/position_encoding.py#L29-L38)
第 30-32 行：`mask=None` 时构造全零 bool 掩码（即全有效）。第 33-34 行两个 `cumsum` 得到递增坐标。第 35-38 行归一化：分母取 `y_embed[:, -1:, :]`（沿 H 的最后一个值，即每列最高有效坐标）与 `x_embed[:, :, -1:]`（每行最右有效坐标），加 `eps=1e-6` 防零。

**多频率 sin/cos 与拼接**：

[mask2former/modeling/transformer_decoder/position_encoding.py:L40-L51](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/position_encoding.py#L40-L51)
第 40-41 行构造分母 `dim_t`（几何级数频率）；第 43-44 行坐标除以分母广播到 `num_pos_feats` 维；第 45-50 行对偶数下标取 sin、奇数下标取 cos 并交错堆叠；第 51 行 `cat((pos_y, pos_x), dim=3)` 前一半通道给 y、后一半给 x，`permute` 成 `(B, 2*num_pos_feats, H, W)`。

**解码器中的构建与调用**：

[mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py:L273-L275](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L273-L275)
`N_steps = hidden_dim // 2`（=128），`PositionEmbeddingSine(N_steps, normalize=True)`——输出通道恰好等于 `hidden_dim`，可以直接与特征相加。

[mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py:L373-L375](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/mask2former_transformer_decoder.py#L373-L375)
`forward` 中对三个尺度分别调用 `self.pe_layer(x[i], None)`（mask 恒为 `None`），随后 flatten 成序列——这就是 4.1 中 `pos` 的来源。

#### 4.5.4 代码实践

**实践目标**：验证输出形状、通道两半的 y/x 分工，以及「值域有界」的特性。

1. 编写脚本（示例代码）：

   ```python
   # 示例代码；需在项目根目录、u1-l2 环境下运行
   import torch
   from mask2former.modeling.transformer_decoder.position_encoding import PositionEmbeddingSine

   pe = PositionEmbeddingSine(num_pos_feats=128, normalize=True)
   feat = torch.zeros(2, 256, 24, 24)          # 通道数无所谓，只借形状
   pos = pe(feat)                               # (B, 256, H, W)
   print("pos shape:", pos.shape)
   print("值域:", pos.min().item(), pos.max().item())

   # 同一物体在两个尺度下：右下角像素的归一化坐标应一致
   pos_small = pe(torch.zeros(1, 256, 12, 12))
   print("大图右下角 y 通道段 vs 小图右下角 y 通道段:",
         pos[0, :128, -1, -1][:4].tolist(),
         pos_small[0, :128, -1, -1][:4].tolist())
   ```

2. 运行脚本。

**需要观察的现象**：`pos.shape` 为 `(2, 256, 24, 24)`；所有值落在 \([-1, 1]\)（sin/cos 的值域）；大小两图右下角像素的前几个 y 通道值**相等**——归一化坐标在起作用（都是「最靠下」的位置）。

**预期结果**：形状与值域结论确定；两组通道值相等的结论由公式可证（归一化后右下角坐标同为 2π），具体打印值待本地验证。

#### 4.5.5 小练习与答案

**练习 1**：`num_pos_feats=128`，输出为什么是 256 通道？

**参考答案**：y 与 x 各占 `num_pos_feats` 个通道（内部再按 sin/cos 交错分成 64+64），拼接后 `2×128=256`，正好等于 `hidden_dim`，使位置编码能逐元素加到 256 维特征上。

**练习 2**：为什么用 sin/cos 而不是直接把归一化坐标 (x, y) 拼成 2 个通道？

**参考答案**：单个标量坐标的表达能力太弱，且线性坐标难以表达「相对位置」。多频率 sin/cos 让任意两个位置的编码内积只依赖相对位移（不同频率三角函数的积化和差性质），高维编码也更容易被线性投影（Q/K 变换）利用。这与原版 Transformer 用三角函数编码的动机一致。

**练习 3**：v2 解码器调用 `pe_layer(x, None)` 恒传 `mask=None`，那 mask 参数是死代码吗？

**参考答案**：不是。它在别处有真实调用：v1 解码器用 `pos = self.pe_layer(x, mask)`（[maskformer_transformer_decoder.py:L133](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/modeling/transformer_decoder/maskformer_transformer_decoder.py#L133)），像素解码器的 encoder 同样传入真实 padding mask。v2 解码器之所以传 `None`，是因为其输入是 `ImageList.from_tensors` 产物经 `size_divisibility` 对齐后的特征（u3-l1），padding 语义在进入解码器前已被丢弃。

---

## 5. 综合实践

**任务：三种积木一次跑通 + pre/post-norm 数据流图对照**。这是本讲规格指定的综合实践，把 4 个模块的验证合并成一个可复跑的脚本。

### 步骤

1. 前置：完成 u1-l2 环境安装与 CUDA 算子编译（`import mask2former` 需要编译好的 `MultiScaleDeformableAttention` 扩展），进入仓库根目录。

2. 新建脚本 `practice_u4l2.py`（示例代码，建议放在 `/tmp`，避免污染源码目录）：

   ```python
   # 示例代码
   import torch
   from copy import deepcopy
   from mask2former.modeling.transformer_decoder.mask2former_transformer_decoder import (
       SelfAttentionLayer, CrossAttentionLayer, FFNLayer,
   )

   torch.manual_seed(42)
   L, B, C, H = 10, 2, 256, 24
   tgt = torch.randn(L, B, C)            # 10 个 query
   memory = torch.randn(H * H, B, C)     # 24×24=576 个图像 token
   pos = torch.randn(H * H, B, C)
   query_pos = torch.randn(L, B, C)

   def build(cls, **kw):
       return cls(d_model=C, nhead=8, dropout=0.0, **kw)

   # ① 三种层、post-norm 默认版各跑一次
   self_layer  = build(SelfAttentionLayer,  normalize_before=False)
   cross_layer = build(CrossAttentionLayer, normalize_before=False)
   ffn_layer   = build(FFNLayer, dim_feedforward=2048, normalize_before=False)

   print("self-attn :", self_layer(tgt, query_pos=query_pos).shape)
   print("cross-attn:", cross_layer(tgt, memory, pos=pos, query_pos=query_pos).shape)
   print("ffn       :", ffn_layer(tgt).shape)

   # ② 同权重下 pre-norm 对照
   def flip(cls, src, **kw):
       pre = build(cls, normalize_before=True, **kw)
       pre.load_state_dict(deepcopy(src.state_dict()))
       return pre

   x = tgt.clone()
   for name, src, pre, kwargs in [
       ("self-attn",  self_layer,  flip(SelfAttentionLayer, self_layer),  dict(query_pos=query_pos)),
       ("cross-attn", cross_layer, flip(CrossAttentionLayer, cross_layer), dict(memory=memory, pos=pos, query_pos=query_pos)),
       ("ffn",        ffn_layer,   flip(FFNLayer, ffn_layer, dim_feedforward=2048), dict()),
   ]:
       d = (src(x, **kwargs) - pre(x, **kwargs)).abs().max().item()
       print(f"{name}: pre/post 输出 max abs diff = {d:.4f}")

   # ③ 按主循环真实顺序串一层：cross → self → FFN
   y = cross_layer(tgt, memory, pos=pos, query_pos=query_pos)
   y = self_layer(y, query_pos=query_pos)
   y = ffn_layer(y)
   print("一层完整解码 (cross→self→ffn):", y.shape)
   ```

3. 运行 `python practice_u4l2.py`。

4. 拿纸笔画两张数据流图（或用 Mermaid/文本图）：把 4.4.2 的两张图各自扩展成「cross-attention 块 + self-attention 块 + FFN 块」的完整一层，标注每个块内部 LN 的位置，与打印结果对照。

### 需要观察的现象

- ① 中三个形状依次为 `(10, 2, 256)`、`(10, 2, 256)`、`(10, 2, 256)`——无论中间跟多少个图像 token 交互，query 序列形状不变。
- ② 中三个 max abs diff 均为正数——同样的权重，pre/post 路径产生不同输出。
- ③ 中串联后仍是 `(10, 2, 256)`——解码器可以任意堆叠层数而形状稳定，这正是 `dec_layers` 层循环能写成一个 for 循环的原因。

### 预期结果

形状结论与「diff > 0」由源码结构决定（dropout 均为 0 时无随机性，结果只依赖权重）；具体数值待本地验证。若 ② 中出现 diff = 0，优先检查是否忘记 `load_state_dict`（两套独立随机初始化的权重也会产生 diff，但那不是「同权重对照」）。

## 6. 本讲小结

- Transformer 解码器世界使用 `(L, B, C)` 序列格式：特征图 `flatten(2)` 后 `permute(2, 0, 1)` 变成 token 序列；query 侧由 `(Q, C)` 嵌入直接广播。
- 位置注入的统一手法是 `with_pos_embed` 的逐元素相加，且**只进 Q/K、不进 V**；query 侧用可学习 `query_pos`，图像侧用 `PositionEmbeddingSine` 算出的 `pos`。
- `SelfAttentionLayer`（query 间通信，q=k 同源）与 `CrossAttentionLayer`（query 从图像 memory 取证据，Q/K 两侧各带各的位置、`memory_mask` 承载 masked attention）都是 `nn.MultiheadAttention` 的薄封装：残差 + LayerNorm + 路径分派。
- `FFNLayer` 是逐 token 两层感知机（256→2048→256，ReLU）；注意与预测头里的 `MLP` 区分。
- `normalize_before` 一个开关控制 pre-norm / post-norm 两条路径，三层 ModuleList 共用该开关；本项目默认 `PRE_NORM=False`（post-norm），pre-norm 主干干净但需要末端 `decoder_norm` 补一次归一化。
- `PositionEmbeddingSine` 用 cumsum 坐标 + 归一化到 \((0, 2\pi]\) + 多频率 sin/cos 交错，输出 `(B, hidden_dim, H, W)`，y/x 各占一半通道；归一化带来多尺度下的尺度不变性。

## 7. 下一步学习建议

本讲的三个积木还只是「零件」。下一讲 **u4-l3 Masked Attention 主循环**将把它们装配成完整的 `MultiScaleMaskedTransformerDecoder.forward`：`query_feat`/`query_embed` 双嵌入的来源、三尺度 `level_index = i % 3` 轮转交叉注意力、`attn_mask` 全 True 行的兜底处理，以及为什么顺序是 cross→self→FFN。之后 u4-l4 讲预测头（`forward_prediction_heads`）如何产出分类 logits、mask 与下一层的 attention mask。

阅读源码时建议带着两个问题进入下一讲：

1. 本讲 4.3 的 `memory_mask` 接口，其输入张量是谁在哪个函数里生成的？（提示：`forward_prediction_heads`）
2. 若把主循环里的 cross 与 self 顺序对调，从本讲的模块语义出发推理，信息流会发生什么变化？
