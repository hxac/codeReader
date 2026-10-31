# 查询解码器：mask attention 与迭代预测

## 1. 本讲目标

上一讲（u4-l2）我们看完了稀疏卷积骨干 `SpConvUNet`：它把点云体素化、逐级提取特征，最终在 `extract_feat` 中用 `scatter_mean` 把体素特征聚合到超点上，得到「每个超点一个 32 维向量」的特征列表 `x`。本讲进入模型的「大脑」——查询解码器 `UnifiedQueryDecoder`，学完后你应当能够：

1. 说清在 `num_instance_queries=0`、`num_semantic_queries=0` 的配置下，解码器里的 query 到底从哪里来（「超点即查询」在代码里的落点），为什么没有任何可学习的 query 参数。
2. 读懂解码器三件套 `CrossAttentionLayer` / `SelfAttentionLayer` / `FFN` 的结构，理解它们为什么按样本逐个循环调用而不是拼成一个 batch 张量。
3. 掌握 `_forward_head` 中用一次 `einsum('nd,md->nm')` 同时算出「所有查询 × 所有点」掩码矩阵的点积掩码范式，以及由它派生的 `attn_mask`（mask attention）的构造细节与防 NaN 保护。
4. 理解 `iter_pred=True` 时「先预测、再迭代」的执行顺序：为什么循环前就有一次预测、为什么每层之后都再出一次预测、`aux_outputs` 深监督到底装了哪几份输出。

本讲只引用一个核心文件 `uniseg3d/query_decoder.py`（约 586 行），并在必要处对照主模型 `uniseg3d/uniseg3d.py` 的调用点与配置文件。

## 2. 前置知识

- **超点即查询**：点云先被过分割成数千个超点（u1-l4、u3-l3），骨干输出的特征也按超点聚合。UniSeg3D 干脆不设可学习的 query 参数，直接把「被选中参与解码的超点特征」当作 query。这意味着 query 的个数随场景变化（一个场景有多少超点就有多少候选 query），且每个 query 天然「知道自己对应哪个超点」——这正是后文匹配器无需全局匈牙利匹配（u5-l2）的根源。
- **query 的采样**：上一讲 u4-l1 已讲过，训练时 `_select_queries` 按比例 \( n \in [0.5, 1.0) \times N \)（上限 3500）随机保留一部分超点作 query，测试时全量保留。
- **多头注意力与 attn_mask**：PyTorch 的 `nn.MultiheadAttention` 接受 `attn_mask` 参数，形状 `(L, S)`（查询数 × 键数），**布尔值为 `True` 的位置表示「禁止注意」**（实现上等价于把该位置的打分设为 \(-\infty\)）：
  \[ \mathrm{Attn}(Q,K,V)=\mathrm{softmax}\!\left(\frac{QK^\top}{\sqrt{d_k}}+M\right)V,\qquad M_{ij}=\begin{cases}0 & \text{允许}\\ -\infty & \text{禁止}\end{cases} \]
- **LayerNorm 与残差**：三件套都采用「残差连接 + LayerNorm」的 Transformer 惯例；本讲会指出 cross-attention 里 `fix_attention` 开关如何改变 norm 的位置（post-LN）。
- **深监督（deep supervision）**：不只在网络最后一层计算损失，而是在中间层也接预测头、一起算损失。DETR 系列的 `aux_outputs` 就是这一惯例：让每一层的中间表示都「直接可解码」，避免信息只在最后一层才被逼出来，训练更稳。
- **einsum 约定**：`torch.einsum('nd,md->nm', A, B)` 表示 \( C_{nm}=\sum_d A_{nd}B_{md} \)，即 A 的第 n 行与 B 的第 m 行的内积，得到逐对相似度矩阵。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [uniseg3d/query_decoder.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/query_decoder.py) | 解码器全部实现 | 三件套、`QueryDecoder` 基类、`UnifiedQueryDecoder` 子类 |
| [uniseg3d/mask_pool.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/mask_pool.py) | 掩码池化函数 | 与 `query_decoder.py` 第 11–27 行是同一实现的**重复副本**，本仓库中没有文件 import 它；解码器实际用的是自己文件内的那份。细节留给 u4-l4 |
| [uniseg3d/uniseg3d.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py) | 主模型 | `_select_queries` 选 query、`interaction_masks` 构造、`self.decoder(...)` 调用点 |
| [configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py) | 主配置 | `decoder=dict(...)` 一段给出全部构造参数 |

配置中解码器的真实参数（[configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py:L47-L66](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/configs/uniseg3d_1xb4_scannet_scanrefer_scannet200_unified.py#L47-L66)）：`num_layers=6`、`num_instance_queries=0`、`num_semantic_queries=0`、`num_instance_classes=18`、`num_semantic_classes=20`、`in_channels=32`、`d_model=256`、`num_heads=8`、`hidden_dim=1024`、`dropout=0.0`、`activation_fn='gelu'`、`iter_pred=True`、`attn_mask=True`、`fix_attention=True`、`objectness_flag=False`、`sphere_cls=True`。`in_channels=32` 正是骨干 `num_channels=32` 的输出维度（u4-l2），两处必须一致。

## 4. 核心概念与源码讲解

### 4.1 三件套：CrossAttentionLayer / SelfAttentionLayer / FFN

#### 4.1.1 概念说明

解码器的每一层由三个子模块串联而成：

- **CrossAttentionLayer（交叉注意力）**：query 从超点特征（`sources`，即骨干输出经 `input_proj` 投影后的 `inst_feats`）中「取信息」。query 是提问方（Q），超点是字典（K=V）。
- **SelfAttentionLayer（自注意力）**：query 之间互相通信。原始查询、点提示查询、文本提示查询共存于同一序列（u4-l6），哪些查询之间允许互相看由 `interaction_masks` 控制。
- **FFN（前馈网络）**：对每条 query 独立地做「升维→非线性→降维」的逐位置变换，补充注意力不擅长的逐通道加工。

一个重要的工程事实：**三件套都是「按样本循环、逐个调用」的**。因为每个场景的超点数 \(N_i\) 与 query 数 \(Q_i\) 各不相同（变长序列），这里没有做 padding 拼接，而是用 Python 列表装各自的张量，for 循环逐样本过注意力。这是整个解码器接口的统一风格：输入输出都是 `List[Tensor]`，长度为 batch_size。

#### 4.1.2 核心流程

单层（第 \(\ell\) 层）对第 \(i\) 个样本的执行顺序：

```text
cross_attn:  q ← q + Dropout(Attn(q, k=inst_feats_i, v=inst_feats_i, attn_mask=掩码注意力))
             q ← LayerNorm(q)                       # 仅 fix_attention=True 时（本配置如此）
self_attn:   q ← LayerNorm(Dropout(Attn(q, q, q, attn_mask=interaction_masks_i)) + q)
ffn:         q ← LayerNorm(FFN_linear(q) + q)
```

即三个子模块全是「残差 + LayerNorm」结构；唯一差异是 cross-attention 的 dropout/norm 受 `fix` 开关控制，self-attention 与 FFN 恒启用。

#### 4.1.3 源码精读

**CrossAttentionLayer 的构造**（[uniseg3d/query_decoder.py:L39-L53](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/query_decoder.py#L39-L53)）：创建 `batch_first=True` 的多头注意力、LayerNorm 与 Dropout，并**显式调用** `init_weights()` 对所有维度大于 1 的参数做 Xavier 均匀初始化（源码里留有一行 `# todo: why BaseModule doesn't call it without us?` 的注释——作者也不确定 mmengine 的 `BaseModule` 为何没有自动调用，于是手动补上）。

**CrossAttentionLayer 的前向**（[uniseg3d/query_decoder.py:L55-L81](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/query_decoder.py#L55-L81)）：逐样本取 `k = v = sources[i]`，query 为 `queries[i]`，可选传入形状 `(n_queries, n_points)` 的 `attn_mask`；`fix=True` 时对注意力输出先 dropout、残差后再 LayerNorm（post-LN），`fix=False` 时只做残差。本配置 `fix_attention=True`，所以走「dropout → 残差 → norm」这条路径。

**SelfAttentionLayer 的前向**（[uniseg3d/query_decoder.py:L100-L120](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/query_decoder.py#L100-L120)）：同样逐样本，q=k=v 都是 query 自身；若提供了 `interaction_masks` 则传入形状 `(Q_i, Q_i)` 的布尔掩码。注意判断写法是 `if not interaction_masks:`——它检验的是**整个列表**的真值（None 或空列表都视为「无掩码」），是整批统一的开关，而非逐样本判断。结构固定为 `LayerNorm(Dropout(attn) + y)`。

**FFN 的构造与前向**（[uniseg3d/query_decoder.py:L133-L160](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/query_decoder.py#L133-L160)）：`Linear(d_model→hidden_dim) → ReLU/GELU → Dropout → Linear(hidden_dim→d_model) → Dropout`，再残差加回输入并 LayerNorm。`activation_fn='gelu'` 时启用 GELU（配置即如此）。

#### 4.1.4 代码实践

单独实例化三件套，验证输入输出形状不变（查询的条数与维度都不被三件套改变）。以下为示例代码（非项目原有代码，需 u1-l2 搭好的环境）：

```python
import torch
from uniseg3d.query_decoder import CrossAttentionLayer, SelfAttentionLayer, FFN

torch.manual_seed(0)
d = 64
cross = CrossAttentionLayer(d_model=d, num_heads=8, dropout=0.0, fix=True)
self_ = SelfAttentionLayer(d_model=d, num_heads=8, dropout=0.0)
ffn   = FFN(d_model=d, hidden_dim=128, dropout=0.0, activation_fn='gelu')

src   = [torch.randn(10, d)]   # 1 个样本，10 个超点特征（K/V）
q     = [torch.randn(6, d)]    # 6 条查询（Q）
attn  = [torch.zeros(6, 10, dtype=torch.bool)]  # 全 False = 不屏蔽任何位置

q = cross(src, q, attn)     # (6, d) -> (6, d)
q = self_(q)                # (6, d) -> (6, d)
q = ffn(q)                  # (6, d) -> (6, d)
print([t.shape for t in q]) # 期望 [torch.Size([6, 64])]
```

需要观察：三步之后形状始终是 `(6, 64)`；把 `attn` 换成 `torch.ones(6, 10, dtype=torch.bool)`（屏蔽全部键）会因 softmax 全 \(-\infty\) 产生 NaN——这正是后文 `_forward_head` 里防 NaN 保护的动机。预期输出形状如上，具体数值待本地验证。

#### 4.1.5 小练习与答案

1. **练习**：为什么三件套不把 batch 拼成一个 `(B, Q, d)` 的张量一起算？
   **答案**：每个场景的超点数与 query 数都不同（变长），且本项目走的是「列表式批处理」路线（u3-l2 的伪批组装传统）；拼批必须 padding 并构造 key_padding_mask，这里选择直接逐样本循环，代码更简单也避免 padding 带来的噪声。
2. **练习**：`fix_attention=False` 时 cross-attention 少了哪两步？
   **答案**：少了注意力输出上的 Dropout，以及残差之后的 LayerNorm（见 L75-L79 的两个 `if self.fix` 分支）；本配置 `fix_attention=True`，两步都启用。
3. **练习**：`SelfAttentionLayer` 里 `if not interaction_masks:` 如果传入的是 `[None]`（长度 1 的列表、元素为 None）会发生什么？
   **答案**：`[None]` 是非空列表，真值为 True，于是走 else 分支，把 `interaction_masks[0]=None` 传给 `nn.MultiheadAttention` 的 `attn_mask`——效果等同无掩码，不会报错；这也说明该判断只看列表整体是否为空。

### 4.2 `_get_queries` 与 `query_proj` 投影：超点即查询的落点

#### 4.2.1 概念说明

「query 从哪来」是理解本解码器的第一问题。传统 DETR 式解码器用 `nn.Embedding` 存一组固定数量的可学习 query（如 100 条），与图像特征做交叉注意力。UniSeg3D 的配置把 `num_instance_queries` 与 `num_semantic_queries` 都设为 0，于是：

- **不存在任何可学习 query**；query 就是主模型传入的「被选中的超点特征」（u4-l1 中 `extract_feat` 的产物、`_select_queries` 的输出）。
- 解码器要做的事只剩「把 32 维超点特征投影到 256 维模型宽度」，这就是 `query_proj` 这个两层 MLP 的全部职责。

代码保留了「可学习 query + 超点 query 混用」的通用能力（`_get_queries` 会把两者 `torch.cat` 到一起），只是本配置没用。

#### 4.2.2 核心流程

```text
主模型: x = extract_feat(...)                # List[Tensor]，每个 (N_i, 32)
        queries = _select_queries(x, ...)     # 训练: 随机抽 50%~100%（≤3500）；测试: 全量
解码器: _get_queries(queries):
            若存在 nn.Embedding(可学习 query): 拼在最前
            result = query_proj(queries[i])   # (Q_i, 32) -> (Q_i, 256)
```

两个投影各司其职：`input_proj` 处理**全部**超点特征 `x`（作为注意力的 K/V），`query_proj` 只处理**被选中**的 `queries`（作为 Q）。二者输入同源（都是超点特征），但参数独立、用途不同。

#### 4.2.3 源码精读

**构造函数中的条件建层**（[uniseg3d/query_decoder.py:L189-L219](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/query_decoder.py#L189-L219)）：这段代码做了四件与本模块相关的事——

1. `input_proj = Linear(in_channels→d_model) + LayerNorm + ReLU`：把超点特征投影成注意力用的 `inst_feats`；
2. `if num_instance_queries + num_semantic_queries > 0` 才创建 `nn.Embedding`（L192-L193）——本配置两者为 0，**该 Embedding 根本不会被创建**；
3. `if num_instance_queries == 0` 才创建 `query_proj`（两层 MLP，L194-L197）——这是「超点即查询」的专用通道；
4. `x_mask`（两层 MLP，L217-L219）把超点特征投影成掩码预测用的 `mask_feats`。

此外 L209-L216 还构造了 `out_norm`（预测头前的 LayerNorm）、`out_cls`（仅 `sphere_cls=False` 时存在的线性分类头）与 `out_score`（仅 `objectness_flag=True` 时存在；本配置为 False，故没有这一支）。配置中的 `sphere_cls=True` 意味着 `out_cls` 也不创建，分类改走 u4-l4 的「球面分类」路径。

**`_get_queries`**（[uniseg3d/query_decoder.py:L223-L246](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/query_decoder.py#L223-L246)）：逐样本把「可学习 query（若有，经 `hasattr(self, 'query')` 探测）」与「`query_proj(queries[i])`」拼进一个列表后 `torch.cat`。本配置下列表里只有一个元素，`torch.cat` 单元素列表直接返回该张量——所以输出就是投影后的超点特征本身。

**主模型侧的 query 供给**（[uniseg3d/uniseg3d.py:L226-L250](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L226-L250)）：`_select_queries` 在训练时按 \( n = ((1-\text{query\_thr})\cdot u + \text{query\_thr})\times N \)（`query_thr=0.5`，即随机取 50%~100%，且封顶 3500）做无放回随机采样，测试分支则取全量。选中的超点特征随后作为 `queries` 传入解码器（调用点见 [uniseg3d/uniseg3d.py:L486](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L486)）。

#### 4.2.4 代码实践

实例化基类 `QueryDecoder`（`sphere_cls=False`，避免依赖类别嵌入文件与 GPU），直接检查两个关键属性是否存在。示例代码：

```python
import torch
from uniseg3d.query_decoder import QueryDecoder

dec = QueryDecoder(
    num_layers=2, num_instance_queries=0, num_semantic_queries=0,
    num_classes=18, in_channels=32, d_model=64, num_heads=8,
    hidden_dim=128, dropout=0.0, activation_fn='gelu',
    iter_pred=True, attn_mask=True, fix_attention=True,
    objectness_flag=False, sphere_cls=False)

print(hasattr(dec, 'query'),      hasattr(dec, 'query_proj'),
      hasattr(dec, 'out_cls'),    hasattr(dec, 'out_score'))
# 期望: False True True False   （无 Embedding、有投影、有线性分类头、无 objectness 分支）

x = [torch.randn(10, 32)]                 # 10 个超点、32 维骨干特征
qs = dec._get_queries([x[0][:6]], len(x)) # 只选前 6 个超点当 query
print(qs[0].shape)                        # 期望: torch.Size([6, 64])
print(sum(p.numel() for p in dec.query_proj.parameters()))
# 期望: (32*64+64) + (64*64+64) = 2112 + 4160 = 6272
```

需要观察：`hasattr(dec, 'query')` 为 False 证实「无可学习 query」；`_get_queries` 输出维度从 32 变为 64（`d_model`）。参数量按两层 Linear 的 `in*out+out` 口算为 6336，可与打印结果核对。具体数值待本地验证。

#### 4.2.5 小练习与答案

1. **练习**：如果把配置改成 `num_instance_queries=100`，`_get_queries` 返回的序列结构会怎么变？
   **答案**：会创建 `nn.Embedding(100, d_model)`；每个样本的返回变成 `torch.cat([self.query.weight, query_proj(queries[i])])`，可学习 query 拼在最前面，总数为 `100 + Q_i`。注意此时 `num_instance_queries != 0`，`query_proj` 反而不再创建——两个通道是互斥的。
2. **练习**：`input_proj` 和 `query_proj` 都吃 32 维超点特征，为什么不能用同一个层？
   **答案**：两者角色不同：`input_proj` 的输出是注意力的 K/V（所有超点都要投影），`query_proj` 的输出是 Q（只有被选中的超点）。分开参数让「作为记忆的特征」与「作为提问的特征」各自适配任务；结构也不同（前者带 LayerNorm+ReLU 一层，后者是两层 MLP）。
3. **练习**：为什么训练时要丢弃一部分超点 query（`query_thr=0.5`）？
   **答案**：一是控制显存与计算量（大场景超点数可达数千，封顶 3500）；二是每次迭代只监督一部分超点，起到类似 dropout 的正则作用，也迫使匹配与损失在「部分查询在场」的情况下依然成立（匹配是稀疏的、按超点归属约束的，见 u5-l2）。

### 4.3 `_forward_head` 与 attn_mask：点积掩码与掩码注意力

#### 4.3.1 概念说明

预测头回答两个问题：**每条 query 是什么类别**、**覆盖哪些点**。后者是本模块的主角：

- **点积掩码范式**（MaskFormer 一脉）：不为每条 query 配一个反卷积分支去画掩码，而是把「掩码」定义为归一化后的 query 向量与每个点的掩码特征向量的**内积**。\( N \) 条 query、\( M \) 个点的全部掩码一次 `einsum` 得到，输出 `(N, M)` 的 logits 矩阵。
- **掩码注意力（mask attention）**：既然本轮已经预测出掩码，下一轮的 cross-attention 就只允许 query 在「自己预测掩码覆盖的点位」上取信息，掩码外一律屏蔽。直觉是：query 先粗略圈出一片区域，随后只在这片区域里精修，避免被无关背景带偏。这是 Mask2Former/OneFormer3D 系列的标准技巧，也是本配置 `attn_mask=True` 启用的路径。

#### 4.3.2 核心流程

对每个样本 \(i\)：

```text
norm_query = out_norm(queries[i])                     # (Q, d)
pred_mask  = einsum('nd,md->nm', norm_query, mask_feats[i])   # (Q, M) 掩码 logits
attn_mask  = (pred_mask.sigmoid() < 0.5)              # True = sigmoid 低于 0.5 = 屏蔽
attn_mask[整行全 True 的行] = False                    # 防全 -inf → NaN 的兜底
attn_mask  = attn_mask.detach()                       # 掩码门控不回传梯度
```

数学上，掩码 logit 与注意力屏蔽分别为
\[ \text{mask}_{nm}=\langle \hat q_n, f_m\rangle,\qquad M_{nm}=\mathbb{1}\!\left[\sigma(\text{mask}_{nm})<0.5\right], \]
其中 \(\hat q_n\) 是 LayerNorm 后的 query，\(f_m\) 是 `x_mask` 投影出的第 \(m\) 个超点的掩码特征。

#### 4.3.3 源码精读

**基类 `_forward_head`**（[uniseg3d/query_decoder.py:L248-L285](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/query_decoder.py#L248-L285)）分四步：

1. L271-L275：`out_norm` 后接 `out_cls` 得到分类 logits（形状 `(Q, num_classes+1)`）；`objectness_flag=False` 时 `pred_score` 为 None；
2. L276：`pred_mask = torch.einsum('nd,md->nm', norm_query, mask_feats[i])`——一次内积得到 `(Q, M)` 掩码矩阵，**这就是点积掩码范式的全部实现，只有一行**；
3. L277-L282：`attn_mask = (pred_mask.sigmoid() < 0.5).bool()` 得到布尔掩码（True=屏蔽）；随后 `attn_mask[torch.where(attn_mask.sum(-1) == attn_mask.shape[-1])] = False` 把「整行全被屏蔽」的行整体放开——若某条 query 对所有点的预测都低于 0.5，softmax 会在全 \(-\infty\) 上归一化产生 NaN，这一行是兜底保护；最后 `detach()` 切断梯度，掩码门控只当「开关」用，不参与反向传播；
4. L284：`attn_mask=False` 时整体返回 None。

**子类 `UnifiedQueryDecoder._forward_head` 的重写**（[uniseg3d/query_decoder.py:L442-L502](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/query_decoder.py#L442-L502)）：`sphere_cls=True` 时分类不再用线性头 `out_cls`，而是在同一段循环里走「`mask_pool` 掩码池化 → `mask_pooling_proj`/`cls_proj` → `forward_logit` 与类别原型算余弦相似度」的球面分类（L479-L491）；语义分支由 `last_flag` 控制只在最后一层计算（L486-L491、L501）；掩码预测 `einsum` 与 `attn_mask` 的构造逻辑与基类完全一致（L477、L493-L498）。注意 L480 调用的 `mask_pool` 是 [uniseg3d/query_decoder.py:L11-L27](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/query_decoder.py#L11-L27) **文件内自定义的那份**（内部同样是 `(mask > 0)` 二值化后按行归一化求均值，与 `sigmoid < 0.5` 的阈值语义一致：logit > 0 等价于 sigmoid > 0.5）。球面分类的细节是下一讲 u4-l4 的主题，这里只需知道「掩码与 attn_mask 的计算方式没变」。

**掩码的两种身份**：`pred_mask` 既是最终输出（交给损失或后处理），又派生出下一层的 `attn_mask` 约束 cross-attention——一份预测，两处复用。

#### 4.3.4 代码实践

用 4.2 节实例化的 `dec`，手动调用 `_forward_head`（基类版本返回 4 个值），核对形状并统计屏蔽比例。示例代码：

```python
dec.eval()
x         = [torch.randn(10, 32)]
mask_feats = [dec.x_mask(y) for y in x]        # (10, 64)
queries   = dec._get_queries([x[0][:6]], 1)     # (6, 64)

cls_preds, pred_scores, pred_masks, attn_masks = dec._forward_head(queries, mask_feats)
print(cls_preds[0].shape, pred_masks[0].shape)   # 期望 (6, 19) 与 (6, 10)：19 = 18+1
print(pred_scores)                               # 期望 None（objectness_flag=False）
ratio = attn_masks[0].float().mean().item()
print(f'{ratio:.3f}')   # 随机初始化下 logits 近零均值，屏蔽比例约在 0.5 上下波动
```

需要观察：`cls_preds` 是 `(6, 19)`（18 类 + 背景）；`pred_masks` 是 `(6, 10)`（6 条 query × 10 个超点）；`attn_masks[0]` 是同形状的布尔矩阵。再检查 `attn_masks[0].all(dim=-1).any()` 是否为 True——若为 True 说明「整行全屏蔽」的行存在、已被兜底放开（可与 4.1.4 的全 True 实验对照理解）。屏蔽比例的具体数值依赖随机初始化，待本地验证。

#### 4.3.5 小练习与答案

1. **练习**：`attn_mask` 为什么要 `detach()`？
   **答案**：`attn_mask` 是由 `pred_mask.sigmoid()` 阈值化得到的硬门控。若不 detach，梯度会经由「阈值化前的 logits」流向掩码预测，等于让「注意力该看哪里」这个离散决策参与训练目标，梯度不稳定且意义含混；detach 后它只作为下一层注意力的结构约束存在（Mask2Former 的做法）。
2. **练习**：`attn_mask[torch.where(attn_mask.sum(-1) == attn_mask.shape[-1])] = False` 这行去掉会有什么后果？
   **答案**：某条 query 若对所有点的掩码 sigmoid 都小于 0.5，其注意力行全为屏蔽，softmax 在全 \(-\infty\) 上得到 NaN，随后前向与反向全部污染为 NaN。该行把这种行整体放开（等价于退化为普通注意力），是必要的数值保护。
3. **练习**：`pred_mask` 的数值范围与 `attn_mask` 的判据分别是什么？二者阈值语义一致吗？
   **答案**：`pred_mask` 是未归一化的内积 logits，可正可负；`attn_mask` 以 `sigmoid(logit) < 0.5` 即 `logit < 0` 为屏蔽判据。而 `mask_pool`（L19）用 `mask > 0` 二值化掩码——两者等价（logit>0 ⟺ sigmoid>0.5），说明「掩码内点」的口径在注意力屏蔽与掩码池化之间是一致的。

### 4.4 `forward_iter_pred`：迭代预测与 aux_outputs 深监督

#### 4.4.1 概念说明

配置 `iter_pred=True` 启用「迭代预测」：解码器在进入第一层注意力**之前**就先做一次预测，之后每过完一层（cross→self→ffn）再做一次预测，共 `num_layers + 1 = 7` 份。最后一份作为主输出，前 6 份打包为 `aux_outputs` 交给损失函数一起监督——这就是深监督。

为什么每层都出预测？直觉上有三点：一是**中间表示直接可解码**，迫使每一层都把 query 加工到「能画出掩码、分出类别」的程度，而不是把信息憋到最后一层；二是**掩码注意力需要逐层更新的掩码**，每次 `_forward_head` 产出的 `attn_mask` 正是下一层 cross-attention 的输入，预测头不只在「输出」，也在「为下一层造约束」；三是**训练信号更密**，6 层解码器的深层也能拿到贴近监督的梯度，缓解深层梯度衰减。

#### 4.4.2 核心流程

`UnifiedQueryDecoder.forward_iter_pred`（本配置实际走的路径）：

```text
inst_feats = input_proj(x)          # 全部超点 -> 注意力 K/V
mask_feats = x_mask(x)              # 全部超点 -> 掩码特征
queries   = _get_queries(queries)   # 被选超点 -> Q

head(queries)                       # 预测 1（还没进任何注意力层）→ 存入列表
for ℓ in 0..5:                      # num_layers = 6
    queries = cross_attn[ℓ](inst_feats, queries, attn_mask)   # 掩码注意力
    queries = self_attn[ℓ](queries, interaction_masks)        # 查询间交互(隔离提示组)
    queries = ffn[ℓ](queries)
    last_flag = (ℓ == 5)
    head(queries, last_flag)        # 预测 ℓ+2；语义分支仅 last_flag 时计算
    contras_embeds.append(out_norm(queries).clone())          # 供跨任务对比蒸馏

aux_outputs = 前 6 份预测
return 最后 1 份 + aux_outputs
```

注意两个「每层都做」：每层都重新生成 `attn_mask`（掩码随迭代逐层收紧），每层都保存一份 `contras_embeds`（LayerNorm 后的 query 嵌入，供 u5-l6 的跨任务对比蒸馏使用）。

#### 4.4.3 源码精读

**入口 `forward`**（[uniseg3d/query_decoder.py:L354-L369](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/query_decoder.py#L354-L369)）：按 `iter_pred` 分派到 `forward_iter_pred` 或 `forward_simple`。一个值得注意的源码细节：L367 传了 `interaction_masks=interaction_masks` 关键字参数，但**基类** `forward_iter_pred` 的签名（[L313](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/query_decoder.py#L313)）只有 `(self, x, queries)`，并不接受该参数——真正生效的是子类 `UnifiedQueryDecoder.forward_iter_pred`（[L531](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/query_decoder.py#L531)）的重写版本，其签名带 `interaction_masks=None`。也就是说，本仓库里基类 `QueryDecoder` 若被单独以 `iter_pred=True` + `forward()` 调用会因关键字参数不匹配而报错；主配置 `type='UnifiedQueryDecoder'`，走子类重写，不受影响。基类版本还有一处差异：其循环内的 self-attention 不传 `interaction_masks`（L336）。

**`UnifiedQueryDecoder.forward_iter_pred` 主体**（[uniseg3d/query_decoder.py:L531-L585](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/query_decoder.py#L531-L585)）：

- L545-L547：两路投影 + `_get_queries`，与流程图对应；
- L548-L554：**循环前的第一次预测**。此时 query 仅仅是 `query_proj` 的产物、未经过任何注意力，这份预测反映「纯超点特征直出的解码」；同段还保存第一份 `contras_embeds`（L554，注意 `out_norm` 之后 `clone`，避免后续原地修改污染列表）；
- L556-L567：六层迭代。cross-attention 传入上一份 `attn_mask`（L557），self-attention 传入 `interaction_masks`（L558）；`last_flag = i == len(self.cross_attn_layers) - 1`（L560）使语义分支只在最后一层计算（呼应 4.3 中 `_forward_head` 的 L486-L491、L501）；
- L569-L578：`aux_outputs` 由 `cls_preds[:-1]` 等五个列表的前 6 份 zip 而成，每个元素是含 `cls_preds/sem_preds/masks/scores/contras_embeds` 五个键的 dict；
- L579-L585：主输出取各列表的最后一份（第 7 份），与 `contras_embeds[-1]`、`aux_outputs` 一并返回。

**`interaction_masks` 从哪来**：主模型在调用解码器前构造它（[uniseg3d/uniseg3d.py:L470-L486](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L470-L486)）：对每个样本建 `(Q, Q)` 的全 False 布尔矩阵，把「原始查询 × 提示查询」两个交叉块置 True（互相看不见），`set_all_mask=True` 时再把提示查询内部块设为「非对角线」（提示彼此隔离）——这正是三件套里 self-attention 拿到的掩码，把「点提示组」「文本提示组」「原始超点组」的通信隔离开。完整拆解在 u4-l6。

**深监督的去向**：`aux_outputs` 随预测 dict 一路交给 `ScanNetUnifiedCriterion`，逐份计算损失后加权合并（u5-l1）；推理时（`predict` 路径，[uniseg3d/uniseg3d.py:L817](https://github.com/dk-liang/UniSeg3D/blob/c2729548d6f64122a92fce39b45d94b948782e67/uniseg3d/uniseg3d.py#L817) 调用同一解码器）只取最后一份主输出，`aux_outputs` 被丢弃。

#### 4.4.4 代码实践

用同一个 `dec`（基类，`num_layers=2`）直接调用 `forward_iter_pred`（注意 4.4.3 说的签名问题，绕过 `forward`），验证「预测份数 = 层数 + 1」与 `aux_outputs` 的键。示例代码：

```python
x = [torch.randn(10, 32)]
out = dec.forward_iter_pred(x, [x[0][:6]])     # 基类版本，直接调用避免 forward 的签名问题
print(type(out))                                # 期望: dict
print(out['cls_preds'][0].shape, out['masks'][0].shape)   # 期望: (6, 19) (6, 10)
print(len(out['aux_outputs']))                  # 期望: 2 = num_layers（最后 1 份为主输出）
print(sorted(out['aux_outputs'][0].keys()))     # 基类: ['cls_preds', 'masks', 'scores']
```

需要观察：主输出与 `aux_outputs` 元素形状一致；`len(aux_outputs)` 恰为 `num_layers`（本例 2，真实配置为 6）。若想看子类版本，需在配有 GPU 与 `data/scannet/scannet_cls_embedding.pth` 的机器上实例化 `UnifiedQueryDecoder`（其 `__init__` 中 `back_token` 固定创建在 `cuda` 上，见 L397-L399），此时 `aux_outputs[0]` 的键会多出 `sem_preds` 与 `contras_embeds`（L569-L578）。具体数值待本地验证。

#### 4.4.5 小练习与答案

1. **练习**：`num_layers=6` 时一次前向共产生几份预测？`aux_outputs` 里几份？
   **答案**：7 份（循环前 1 份 + 每层后各 1 份）；`aux_outputs` 装前 6 份，第 7 份作为主输出直接放在返回 dict 顶层。
2. **练习**：语义分支（`sem_preds`）为什么只在 `last_flag` 时计算？
   **答案**：语义分割是「每个超点/点一个类别」的全局任务，不像实例掩码那样需要逐层细化；中间层的语义预测对迭代掩码精修没有用处（`attn_mask` 只由 `pred_mask` 派生），却在每层都引入一份球面分类的计算与参数激活。只在最后一层算一次可以省去中间层的这份开销，语义监督也只挂在最终表示上（`aux_outputs` 中的 `sem_preds` 均为 None）。
3. **练习**：`contras_embeds` 每层都保存一份，它服务于哪个后续模块？
   **答案**：`contras_embeds` 是 `out_norm` 后的 query 嵌入快照，主输出与每个 aux 层各一份，交给 `ScanNetUnifiedCriterion` 中的跨任务对比/蒸馏损失（`ContrastiveCriterion` 与 `IntrinsicPointAndQuery` 等，见 u5-l6），用于把点提示分支的知识迁移给常规查询。

## 5. 综合实践

把 4.1–4.4 的片段串成一个脚本，完整走一遍「超点特征进、七份预测出」的解码器前向（以基类 `QueryDecoder` 代替需要 GPU 与类别嵌入文件的 `UnifiedQueryDecoder`，掩码与迭代逻辑两者一致）。

**实践目标**：用 `batch=1`、10 个超点、`d_model=64` 的小张量，手动复现解码器一层的执行顺序，验证每一步的形状，并统计 `attn_mask` 的屏蔽比例。

**操作步骤**：

```python
# play_decoder.py —— 示例代码（非项目原有代码），在仓库根目录运行: python play_decoder.py
import torch
from uniseg3d.query_decoder import QueryDecoder

torch.manual_seed(0)
dec = QueryDecoder(
    num_layers=2, num_instance_queries=0, num_semantic_queries=0,
    num_classes=18, in_channels=32, d_model=64, num_heads=8,
    hidden_dim=128, dropout=0.0, activation_fn='gelu',
    iter_pred=True, attn_mask=True, fix_attention=True,
    objectness_flag=False, sphere_cls=False)
dec.eval()

x = [torch.randn(10, 32)]          # 1 个场景、10 个超点、骨干输出 32 维
queries = [x[0][:6].clone()]       # 模拟 _select_queries 选中 6 条超点 query

# ① 两路投影
inst_feats = [dec.input_proj(y) for y in x]      # 注意力 K/V: (10, 64)
mask_feats = [dec.x_mask(y) for y in x]          # 掩码特征:    (10, 64)
qs = dec._get_queries(queries, len(x))           # 查询:        (6, 64)
print('①', inst_feats[0].shape, mask_feats[0].shape, qs[0].shape)

# ② 循环前的一次预测（迭代预测的起点）
cls, _, masks, attn = dec._forward_head(qs, mask_feats)
print('②', cls[0].shape, masks[0].shape, attn[0].shape, f'屏蔽比例={attn[0].float().mean():.3f}')

# ③ 手动走两层：cross(带 attn_mask) -> self(带 interaction_masks) -> ffn -> head
interaction_masks = [torch.zeros(6, 6, dtype=torch.bool)]   # 全 False = 查询间不隔离
for i in range(len(dec.cross_attn_layers)):
    qs = dec.cross_attn_layers[i](inst_feats, qs, attn)
    qs = dec.self_attn_layers[i](qs, interaction_masks)
    qs = dec.ffn_layers[i](qs)
    cls, _, masks, attn = dec._forward_head(qs, mask_feats)
    print(f'③ 第{i}层后', qs[0].shape, masks[0].shape,
          f'屏蔽比例={attn[0].float().mean():.3f}')

# ④ 整体前向，核对 aux_outputs
out = dec.forward_iter_pred(x, queries)
print('④ 主输出', out['cls_preds'][0].shape, out['masks'][0].shape,
      'aux 份数', len(out['aux_outputs']))
```

**需要观察的现象**：

- ①三路投影后分别为 `(10, 64)`、`(10, 64)`、`(6, 64)`——`input_proj`/`x_mask` 作用于全部超点，`query_proj` 只作用于被选中的 query；
- ②预测头输出 `(6, 19)` 的分类 logits 与 `(6, 10)` 的掩码矩阵；`attn_mask` 与掩码同形状，屏蔽比例在随机初始化下应落在 0.5 附近；
- ③两层过后 query 形状始终 `(6, 64)`（三件套不改变序列长度与维度）；逐层打印的屏蔽比例可能逐渐偏离 0.5（掩码随迭代收紧的现象）；
- ④`aux_outputs` 长度等于 `num_layers`（2），主输出形状与 ②③ 一致。

**预期结果**：所有形状断言如上表成立；`②` 与 `④` 的主输出数值不同（④ 又多过了两层）。本脚本未在本文撰写环境中执行（无 PyTorch 环境），全部输出为依据源码推导的预期值，**待本地验证**。验证通过后，可做两个延伸实验：（a）把 `attn_mask` 构造里的 `0.5` 改成 `0.9`（对应 L278 的阈值），观察屏蔽比例与各层掩码的变化——注意这是示例改动，请勿修改仓库源文件，可在脚本里自行复现该行逻辑；（b）把 `interaction_masks` 的左上 3×3 块置 True，确认只影响 self-attention 的输出而 cross-attention 不受影响。

## 6. 本讲小结

- 解码器三件套 `CrossAttentionLayer`/`SelfAttentionLayer`/`FFN` 全部「逐样本循环 + 残差 + LayerNorm」，因为超点数与查询数逐场景不同（变长序列，列表式批处理）；cross-attention 的 dropout 与 norm 由 `fix_attention=True` 启用。
- 「超点即查询」的代码落点：`num_instance_queries=0` 时既不建 `nn.Embedding` 也不建可学习参数，query 就是主模型 `_select_queries` 选出的超点特征，经两层 MLP `query_proj` 从 32 维投到 256 维。
- 掩码预测是**一行 einsum**：`torch.einsum('nd,md->nm', norm_query, mask_feats[i])` 得到 `(Q, M)` 的点积掩码；`sigmoid < 0.5` 派生出屏蔽式的 `attn_mask`（mask attention），带「整行全屏蔽则放开」的 NaN 兜底并 `detach()`。
- `iter_pred=True` 时共产生 `num_layers + 1 = 7` 份预测：循环前 1 份 + 每层后各 1 份；前 6 份打包成 `aux_outputs` 深监督，最后 1 份为主输出；每层还各存一份 `contras_embeds` 供跨任务蒸馏（u5-l6）。
- 实际被实例化的是子类 `UnifiedQueryDecoder`：其 `_forward_head` 额外走 mask_pool + 球面分类（u4-l4 专题），语义分支由 `last_flag` 控制只在最后一层计算；`forward_iter_pred` 的重写版本才接受 `interaction_masks`（原始/点提示/文本提示三组查询的自注意力隔离，u4-l6 专题）。

## 7. 下一步学习建议

下一讲 **u4-l4「UnifiedQueryDecoder：CLIP 词表分类与 mask pooling」**正好接住本讲留下的两个钩子：`sphere_cls=True` 时分类头被替换成「加载 `scannet_cls_embedding.pth` 类别原型 → `mask_pool` 从掩码内点特征池化 → `cls_proj`/`cls_proj_sem` 投影 → `forward_logit` 与原型算余弦相似度」的完整链路，以及实例分类为何跳过前两个类别原型（`cls_embed[:, 2:, :]`）。建议带着两个问题去读：为什么掩码池化要用 `(mask > 0)` 二值化而不是软加权？`logit_scale=e^{4.6052}` 这个温度从哪来（提示：CLIP）。若想先补调用链上游，可回看 u4-l1 的 `loss()` 十步前向中 `self.decoder(...)` 的位置；若想看深监督如何被消费，可直接跳到 u5-l1 的 `ScanNetUnifiedCriterion`。
