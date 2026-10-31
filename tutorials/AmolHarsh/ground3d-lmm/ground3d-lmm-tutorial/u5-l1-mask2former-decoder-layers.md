# Mask2Former 式查询解码器：逐层前向

## 1. 本讲目标

上一讲（u4-l4）我们搞清楚了「分割查询从哪里来」：语言塔最后一层隐状态中 `<p>…</p>` 指称文本的均值池化向量。本讲回答下一个问题：**这些查询进入解码器后，如何一步步变成逐超点的分割掩码？**

读完本讲你应该能够：

1. 说出解码器每层「Cross-Attn → Self-Attn → FFN」三件套的精确计算与残差/归一化顺序。
2. 解释两类布尔掩码的屏蔽语义：cross-attn 的 `attn_mask`（Mask2Former 式 masked attention，只注意自己认为是前景的超点）与 self-attn 的 `interaction_masks`（`~eye(L)` 查询隔离）。
3. 讲清 `_forward_head` 如何用一个 einsum 完成动态 mask 打分，以及全屏蔽行的「救援」逻辑。
4. 对比 `forward_iter_pred` 的两条路径：训练时整场景全部查询一次过 6 层；推理时逐 QA 独立循环 6 层。

## 2. 前置知识

### 2.1 查询解码器（query decoder）范式

DETR / Mask2Former 一族模型的核心是「查询」（query）：一组可学习的向量，每条查询负责「寻找」一个目标。解码器逐层 refine 这些查询：查询先与图像（这里是点云）特征做交叉注意力「看场景」，查询之间做自注意力「互相协调」，再过 FFN「独立加工」。最后每条查询与 mask 特征做点积，直接得到该目标的分割掩码——**掩码是算出来的，不是分割头卷出来的**。

### 2.2 Mask2Former 的 masked attention

Mask2Former 的关键改进：交叉注意力不让每条查询看全部位置，而是只看**该查询当前预测掩码内的位置**。原论文公式为：

\[
\text{Attn}(Q, K, V) = \text{softmax}\left(\frac{QK^\top}{\sqrt{d}} + \log \hat{M}\right)
\]

其中 \(\hat{M}\) 是上一轮预测掩码的二值化（0/1）。加 \(\log 0 = -\infty\) 即把掩码外位置的注意力分数压成负无穷，softmax 后权重为 0。本项目用 PyTorch 布尔 `attn_mask` 实现同一语义。

### 2.3 PyTorch 布尔 attn_mask 的约定

`nn.MultiheadAttention` 的布尔 `attn_mask` 中 **`True` 表示「禁止注意该位置」**（内部会被填充为 \(-\infty\)）。本仓库代码只有在这一约定下才自洽，有两处内证：

- `_forward_head` 里若某查询把所有超点都判为背景（attn_mask 整行全 `True`），代码会把该行重置为全 `False`「放行一切」——只有 True=禁止 时这才叫救援（全禁止 = softmax 空集 = NaN）。
- `~eye(L)` 自注意力掩码在 L=1 时是 `[[False]]`（无任何限制）；若 True 反倒表示允许，单个查询会把自己也屏蔽掉，产生 NaN，模型根本无法工作。

### 2.4 逐场景列表式批组织

本项目的解码器**不做 padding 拼批**，所有中间结果都是「按场景的 `List[Tensor]`」：第 i 个元素形状 `(n_i, d)`，n_i 是该场景的超点数（或查询数），各场景可以不同。每个注意力层内部用 `for` 循环逐场景计算。这承接 u3-l1：`extract_feat` 按偏移把全局池化结果切回逐场景，输入解码器的 `x` 就是这个列表，每个元素是 `(超点数, 32)` 的超点特征。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [uniseg3d/query_decoder.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py) | 本讲主战场：三件套注意力层（L311-L450）、基类 `QueryDecoder`（L453-L659）、`Grounded_Decoder_Joint` 的 `_forward_head` 与 `forward_iter_pred`（L1733-L1959） |
| [configs/ground3dlmm_train.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py) | 解码器超参数（6 层、256 维、8 头等） |
| [uniseg3d/uniseg3d.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py) | 调用方：`loss()`/`predict()` 把超点特征递给 `self.decoder(...)`（L1172、L1305） |

类继承链（本讲涉及的三个类都在 query_decoder.py 中）：

```
QueryDecoder                       ← 三件套堆叠 + _get_queries + _forward_head 骨架
  └── UnifiedQueryDecoder          ← 加分类头（sphere_cls 等），本讲用于对照
        └── Grounded_Decoder_Joint ← 训练模型：裁掉分类头，查询来自 LLM 的 <SEG> 隐状态
```

## 4. 核心概念与源码讲解

### 4.1 三件套：CrossAttentionLayer / SelfAttentionLayer / FFN

#### 4.1.1 概念说明

解码器的「一层」由三个子模块串联组成：

- **CrossAttentionLayer**：查询（Q）去「看」超点特征（K=V），是查询从场景收集证据的唯一通道。
- **SelfAttentionLayer**：查询之间互相注意，用于查询间协调；本项目刻意用掩码把它改造成**查询隔离**（见 4.1.3）。
- **FFN**：逐位置前馈网络（256→1024→256），对每条查询独立做非线性加工。

训练配置（[configs/ground3dlmm_train.py:L47-L64](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py#L47-L64)）规定：`num_layers=6`、`d_model=256`、`num_heads=8`、`hidden_dim=1024`、`dropout=0.0`、`activation_fn='gelu'`、`fix_attention=True`——即 6 层、每层 256 维 8 头交叉/自注意力 + 1024 维隐层 FFN，无 dropout。

#### 4.1.2 核心流程

一层解码器（对每个场景独立执行）：

```
输入: sources[i] (n_sp_i, 256) 超点特征, queries[i] (n_q_i, 256) 查询

1. CrossAttn:  q' = LN( dropout(Attn(q, k=v=sources[i], attn_mask)) + q )   # fix=True 时 post-norm
2. SelfAttn :  q'' = LN( dropout(Attn(q', q', q', interaction_mask)) + q' )  # NaN 行先置零再算
3. FFN      :  q''' = LN( FFN(q'') + q'' )                                  # 256→1024→256

重复 6 层；每层结束后立即调用一次 _forward_head 出一份 mask 预测（见 4.3）
```

注意三件套**全部是后归一化（post-norm）**：先残差相加、再 LayerNorm。

#### 4.1.3 源码精读

**CrossAttentionLayer** 的构造与初始化：

- [uniseg3d/query_decoder.py:L320-L334](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L320-L334)：用 `nn.MultiheadAttention(d_model, num_heads, dropout, batch_first=True)` 做核心注意力；构造函数末尾**手动调用** `self.init_weights()`（对二维以上参数做 xavier 初始化）——注释里那句 `# todo: why BaseModule doesn't call it without us?` 是作者的疑问：mmengine 的 `BaseModule` 需要外部触发 `init_weights`，这里等不及就自己调了。

- [uniseg3d/query_decoder.py:L336-L362](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L336-L362)：逐场景循环，`k = v = sources[i]`（超点特征同时充当键与值），查询做 Q；`attn_mask` 按场景传入。`fix=True`（本配置 `fix_attention=True`）时执行 `dropout(attn输出) + 残差 → LayerNorm`；`fix=False` 时**只有残差、连 LayerNorm 都没有**——这是一处容易忽略的分支差异。

**SelfAttentionLayer**：

- [uniseg3d/query_decoder.py:L381-L410](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L381-L410)：先做 NaN 防护——`torch.isnan(y).any(dim=-1)` 找出含 NaN 的查询行，复制一份把 NaN 置 0 再送入注意力（残差仍用**原始** y，所以 NaN 行自身保留 NaN，但不会通过注意力污染其他行）。这些 NaN 行的来源见 4.3.3：场景没有 QA 时解码器直接用全 NaN 查询占位。随后 `z = dropout(z) + y`、`z = norm(z)`。

  `interaction_masks` 的语义：训练分支传入的是 `~torch.eye(L)`（[L1874-L1878](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1874-L1878)），对角线为 `False`、非对角为 `True`。按「True=禁止」约定翻译：**每条查询只允许注意自己，禁止注意其他任何查询**。也就是说 self-attention 在这里被改造成了「查询隔离器」：不同 QA、不同指称目标的查询之间不许交换信息，防止「门」的查询被「桌子」的查询带偏。L=1 时掩码是 `[[False]]`，等于不加限制。注意 `if not interaction_masks:` 判断的是列表本身——`None` 或空列表走无掩码分支，非空列表才逐场景取 `interaction_masks[i]`。

**FFN**：

- [uniseg3d/query_decoder.py:L423-L450](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L423-L450)：`Linear(256,1024) → GELU → Dropout → Linear(1024,256) → Dropout`，再残差加输入、LayerNorm。逐场景循环、无交互。

三件套的堆叠发生在基类构造函数里：

- [uniseg3d/query_decoder.py:L488-L498](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L488-L498)：三个 `nn.ModuleList`（`cross_attn_layers` / `self_attn_layers` / `ffn_layers`）各 append `num_layers` 个实例，层与层之间共享同一组模块列表、按索引对齐。

#### 4.1.4 代码实践：小维度跑通三件套并验证掩码语义

**实践目标**：亲手确认「True=禁止注意」的语义，以及三件套在变长场景列表上的形状行为。

**操作步骤**：

1. 进入 u1-l2 搭好的 conda 环境，在任意目录新建 `layers_probe.py`：

```python
# 示例代码：直接实例化 uniseg3d 三件套（无需加载 LLM）
import torch
from uniseg3d.query_decoder import CrossAttentionLayer, SelfAttentionLayer, FFN

torch.manual_seed(0)
d, heads = 16, 2
cross = CrossAttentionLayer(d, heads, dropout=0.0, fix=True)
selfattn = SelfAttentionLayer(d, heads, dropout=0.0)
ffn = FFN(d, hidden_dim=64, dropout=0.0, activation_fn='gelu')

# 两个变长「场景」：超点数 12 与 7；查询数 3 与 2
sources = [torch.randn(12, d), torch.randn(7, d)]
queries = [torch.randn(3, d), torch.randn(2, d)]

q = cross(sources, queries)          # 先不加 attn_mask
q = selfattn(q)                       # 先不加 interaction_masks
q = ffn(q)
print([t.shape for t in q])           # 期望 [(3,16), (2,16)]

# 验证 True=禁止：1 条查询对 2 个键，屏蔽第 0 个键
attn = torch.nn.MultiheadAttention(d, heads, batch_first=True)
qq, kk = torch.randn(1, 1, d), torch.randn(1, 2, d)
out = attn(qq, kk, kk, attn_mask=torch.tensor([[True, False]]))[0]
out_k1 = attn(qq, kk[:, 1:], kk[:, 1:])[0]   # 只留键 1
print('True=禁止(输出≈只留键1):', torch.allclose(out, out_k1, atol=1e-5))

# 验证查询隔离：~eye(2) 下每条查询只能注意自己
q2 = torch.randn(1, 2, d)
m = ~torch.eye(2, dtype=torch.bool)   # [[F,T],[T,F]]：禁止注意对方
o = attn(q2, q2, q2, attn_mask=m)[0]
o_self0 = attn(q2[:, :1], q2[:, :1], q2[:, :1])[0]
print('查询0只注意自己:', torch.allclose(o[:, :1], o_self0, atol=1e-5))
```

2. 运行 `python layers_probe.py`。

**需要观察的现象**：三件套输出形状与输入查询完全一致（变长场景互不影响）；两条 `allclose` 的打印结果。

**预期结果**：形状打印 `[(3,16), (2,16)]`；两个布尔都应为 `True`（即 True 的确表示禁止注意、`~eye` 的确实现查询隔离）。**待本地验证**（语义由 2.3 节的代码内证推出，请在本地实跑确认）。

3. 若环境中 mmdet3d/transformers 等依赖未装好导致 `import uniseg3d` 失败：把三个类的代码原样拷出、令其继承 `nn.Module`，即可在纯 PyTorch 下运行同样的实验（三件套本身只依赖 torch 与 `BaseModule` 的空壳行为）。

#### 4.1.5 小练习与答案

**练习 1**：`fix_attention=True` 与 `False` 的 CrossAttentionLayer 输出有何差别？

答案：`fix=False` 时（[L356-L360](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L356-L360)）既不做注意力输出的 dropout 也不做 LayerNorm，只做 `output + queries[i]`；`fix=True` 时是完整的 `LN(dropout(attn) + q)` 后归一化残差结构。本项目配置取 True。

**练习 2**：为什么 SelfAttentionLayer 要在注意力前把 NaN 行置零，而不是直接删掉这些行？

答案：删行会破坏「查询行 ↔ GT 行」的索引对应（u2-l4 建立的形状契约：第 k 条查询对应 `sp_gt_seg` 第 k 行）。置零让 NaN 行不通过注意力把 NaN 传给其他行，同时保住行数与顺序；NaN 行残差后仍是 NaN，损失侧会按「零向量 ∧ GT 非 NaN」的过滤规则跳过它们（承接 u4-l4）。

### 4.2 _forward_head：动态 mask 打分与 attn_mask 生成

#### 4.2.1 概念说明

`_forward_head` 是「查询 → 掩码」的出口，做两件事：

1. **打分**：每条归一化后的查询与每个超点的 mask 特征做点积，得到掩码 logit 矩阵。没有卷积头、没有上采样——因为这里的「像素」本来就是超点。
2. **造掩码**：把当前预测 sigmoid 后 < 0.5 的位置标成 `True`（禁止），作为下一层 cross-attn 的 masked attention 掩码。

与之配套，`QueryDecoder.__init__` 从 32 维超点特征造出**两路投影**：

- `input_proj`（[L479-L480](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L479-L480)）：`Linear(32,256)+LN+ReLU` → `inst_feats`，作 cross-attn 的 K/V（「看场景」通道）。
- `x_mask`（[L507-L509](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L507-L509)）：`Linear(32,256)+ReLU+Linear(256,256)` → `mask_feats`，作 mask 打分基底；**同时**它就是送入语言分支的 256 维超点特征（`forward_iter_pred` 里 `infer_reason_train(mask_feats, ...)`，再经 `point2token` 升维成 `<|point|>` token，见 [L1370](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1370)）——一份特征两用：既是 LMM 的 3D token 源，又是分割的打分基底。

#### 4.2.2 核心流程

对每个场景 i：

\[
M_{ij} = \langle \text{LN}(q_i),\ f_j \rangle, \quad M \in \mathbb{R}^{n_q \times n_{sp}}
\]

- 掩码概率 \(p = \sigma(M)\)（损失侧才做 sigmoid，这里输出裸 logit）。
- attn_mask：\(\text{mask}_{ij} = \mathbb{1}[p_{ij} < 0.5]\)，即 logit < 0 → `True`（禁止）。
- 救援：若某行所有位置都为 `True`（该查询认为全场都是背景），整行置 `False`，避免 softmax 全 \(-\infty\) 产生 NaN。
- `detach()`：掩码不回传梯度，仅作为路由先验。

#### 4.2.3 源码精读

`Grounded_Decoder_Joint` 覆写版 `_forward_head`（训练与推理都用它）：

- [uniseg3d/query_decoder.py:L1733-L1772](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1733-L1772)：逐场景 `norm_query = self.out_norm(queries[i])`；`pred_mask = torch.einsum('nd,md->nm', norm_query, mask_feats[i])` 就是上面的点积打分；随后三行生成 attn_mask：`(pred_mask.sigmoid() < 0.5).bool()` → 全 True 行救援 → `detach()`。注意它**只**填 `pred_masks` 与 `attn_masks`，`pred_scores` 自始至终是空列表——接地模型没有类别头，返回值里的 `scores` 是占位。

与祖先对照，能看清「裁掉了什么」：

- 基类版 [uniseg3d/query_decoder.py:L538-L575](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L538-L575)：多了 `out_cls` 分类打分与 `out_score` 置信度打分，mask 打分三行（einsum + sigmoid 掩码 + 救援）完全同构。
- `UnifiedQueryDecoder` 版 [uniseg3d/query_decoder.py:L732-L792](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L732-L792)：`sphere_cls=True` 分支里还有 mask pooling + 余弦分类（`mask_pool` + `forward_logit`），用「掩码内特征池化」去比对手工类别嵌入。Grounded 覆写把这些全删了——分割查询直接由语言给出，无需再预测「这是哪一类」。配置里 `sphere_cls=True`（[configs/ground3dlmm_train.py:L65](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py#L65)）只是让基类不建 `out_cls`（[L500-L503](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L500-L503)），`objectness_flag=False` 让 `out_score` 也不建（[L504-L506](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L504-L506)）。

两处阅读考古（不影响运行，但要知道它们是死代码）：

- `_forward_head_eval`（[L1775-L1824](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1775-L1824)）：按 QA 嵌套打分的版本，全文件无任何调用点。推理分支实际复用训练版 `_forward_head`（[L1930](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1930)、[L1941](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1941)）。
- `forward_simple`（[L1826-L1851](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1826-L1851)）：调用 `_forward_head` 时少传了 `qa_data`，真调用会直接 TypeError；但配置 `iter_pred=True`（[configs/ground3dlmm_train.py:L61](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py#L61)），`forward` 只走 `forward_iter_pred`，此路径永不触达。

#### 4.2.4 代码实践：复刻 attn_mask 的救援逻辑

**实践目标**：亲眼看到「全背景查询」被救援行放行。

**操作步骤**：接 4.1.4 的脚本，追加：

```python
# 示例代码：复刻 _forward_head L1764-L1771 的掩码生成
import torch
n_q, n_sp = 3, 10
pred_mask = torch.randn(n_q, n_sp) * 2
pred_mask[1] = -10.0            # 查询 1 认为全场都是背景
attn_mask = (pred_mask.sigmoid() < 0.5).bool()
blocked_rows = torch.where(attn_mask.sum(-1) == attn_mask.shape[-1])
attn_mask[blocked_rows] = False
print('第 1 行救援后允许的超点数:', attn_mask[1].logical_not().sum().item())
```

**需要观察的现象**：查询 1 原本 10 个位置全为 `True`（全禁止），救援后变全 `False`。

**预期结果**：打印 `10`。若删掉救援那行再跑，`attn_mask[1]` 全 True，送进 `nn.MultiheadAttention` 会在 softmax 处得到 NaN（可自行加一行验证 `torch.softmax(torch.tensor([float('-inf')]), 0)` 的结果是 nan）。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 cross-attn 的 K/V 用 `input_proj(x)` 而 mask 打分用 `x_mask(x)`，要分成两路投影？

答案：两路职责不同——`inst_feats` 服务于注意力聚合（要的是「便于被查询检索」的表示），`mask_feats` 服务于点积打分且同时是 LMM 的 `<|point|>` token 源（u4-l1/u4-l2）。解耦后分割打分基底与语言 token 共享同一份 256 维特征，而注意力的键值空间可以独立学习，互不牵制。

**练习 2**：attn_mask 为什么必须 `detach()`？

答案：它是「下一层注意力的路由开关」，由上一轮预测**离散化**（sigmoid + 阈值）而来，本身不可微。detach 阻止梯度经由这条布尔路径回传，训练信号只走掩码 logit 的主路径，避免奇怪的梯度竞争。

### 4.3 forward_iter_pred 训练路径：整场景查询一次过 6 层

#### 4.3.1 概念说明

`iter_pred`（iterative prediction）指**每层解码器之后都出一份 mask 预测**，而不是只在最后出一次。训练时 6 层产出 6+1 份预测，中间层预测作为辅助损失（deep supervision），强迫每一层都「已经会分割」。训练路径的关键组织方式：**一个场景的全部 QA 查询（最多 num_qa×2 条）拼成一个查询集，一次性串行通过 6 层**。

#### 4.3.2 核心流程

```
输入: x = 按场景的超点特征列表 (n_sp_i, 32), qa_data = 按场景的 QA

1. inst_feats = input_proj(x); mask_feats = x_mask(x)         # 两路投影
2. queries, lang_losses = infer_reason_train(mask_feats, qa_data)
   # 语言分支（u4-l3）：LLM 前向 → 语言损失 + 每场景 (n_q_i, 2560) 查询张量
3. interaction_masks = 每场景 ~eye(n_q_i)                       # 查询隔离
4. original_queries = queries 克隆（投影前，供损失侧识别零查询，承接 u4-l4）
5. queries = _get_queries(queries)                              # query_proj: 2560 → 256
6. 初始 head：_forward_head → 第 0 份 mask
7. for i in 0..5:
     queries = cross_attn[i](inst_feats, queries, attn_mask)    # masked attention
     queries = self_attn[i](queries, interaction_masks)          # 查询隔离
     queries = ffn[i](queries)
     attn_mask 更新自本轮 _forward_head
8. 返回 dict(original_queries, masks=第 6 份, scores, aux_outputs=5 份中间, language_losses)
```

#### 4.3.3 源码精读

训练分支主体：

- [uniseg3d/query_decoder.py:L1871-L1880](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1871-L1880)：调 `infer_reason_train` 拿到语言损失与每场景查询（**2560 维**、未经投影——`get_query_tokens_id` 直接返回隐状态均值，见 [L1262](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1262)）。随后逐场景构造 `~eye(L)` 掩码（L=该场景查询总数），克隆 `original_queries`，再交给 `_get_queries` 投影到 256 维。若场景没有 QA，`infer_reason_train` 返回**全 NaN 查询**占位（[L1336-L1340](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1336-L1340)）——这正是 SelfAttentionLayer NaN 防护要对付的输入。

- `_get_queries`（[L513-L536](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L513-L536)）：本配置 `num_instance_queries=0` 且 `num_semantic_queries=0`，所以**没有可学习查询嵌入**（`self.query` 不会创建，条件在 [L481-L483](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L481-L483)），输出就是 `query_proj(queries[i])`。注意一个继承细节：基类在 `num_instance_queries == 0` 时会先建一个 `Linear(32,256)+ReLU+Linear(256,256)` 的 `query_proj`（[L484-L487](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L484-L487)），随后 `Grounded_Decoder_Joint.__init__` 用 `RMSNorm(2560) → Linear(2560,256) → GELU → Linear(256,256)` **整体覆盖**它（[L944-L947](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L944-L947)）——所以 `_get_queries` 里真正生效的是 2560→256 的语言桥。

- [uniseg3d/query_decoder.py:L1882-L1896](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1882-L1896)：先在**进第一层之前**出一份初始 head 预测（检验「纯语言查询、未看场景」时的分割能力），再进入 6 层循环：cross（带上一轮 attn_mask）→ self（带 interaction_masks）→ FFN → head。每层 head 刷新的 attn_mask 供**下一层** cross-attn 使用，形成「预测→聚焦→再预测」的迭代细化。

- [uniseg3d/query_decoder.py:L1899-L1910](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1899-L1910)：`aux_outputs` 由 `zip(pred_scores[:-1], pred_masks[:-1])` 组装。数一数：`pred_masks` 有 7 份（初始 + 6 层，L1884 与 L1895 各 append），`pred_scores` 只有 6 份（仅循环内 append，且因 4.2.3 所述全是空列表），zip 截断后 aux 为 **5 份**（用掉第 0~4 份 mask），最终返回 `pred_masks[-1]`（第 7 份）。第 6 份（下标 5，即第 5 层输出）计算了但未被监督——这是 zip 截断留下的缝隙，非设计亮点，读懂即可。另外 `contras_embeds`（L1885、L1896）逐层收集了归一化查询却未放进返回值，是从 UniSeg3D 对比学习用法退化而来的遗留计算。

#### 4.3.4 代码实践：数一数 head 输出与 aux 监督（源码阅读型）

**实践目标**：亲手验证「7 份预测、5 份辅助监督」的结构，建立对 deep supervision 的直觉。

**操作步骤**：

1. 打开 [uniseg3d/query_decoder.py:L1866-L1910](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1866-L1910)。
2. 准备一张纸，分别记录 `pred_masks.append` 与 `pred_scores.append` 的每一次出现及其位置（循环外/循环内）。
3. 写出 `pred_masks`、`pred_scores` 的最终长度，推算 `zip(pred_scores[:-1], pred_masks[:-1])` 的元素个数。
4. 回答：第 5 层（最后一个循环迭代 i=5）产生的 mask 进了返回值吗？

**需要观察的现象**：append 的不对称（初始 head 只 append mask 不 append score）。

**预期结果**：`pred_masks` 长度 7、`pred_scores` 长度 6、aux 5 份；i=5 那层 mask 是 `pred_masks[6]`，就是返回的 `masks`（最后一份）；而 `pred_masks[5]`（i=4 那层）不进 aux（zip 只取到下标 4）也不作最终输出，未被监督。此为纯阅读任务，结论可直接从代码推得。

#### 4.3.5 小练习与答案

**练习 1**：训练时一个 batch 有 B 个场景、每场景最多 Q 条查询，self-attention 的 `interaction_masks` 形状是什么？它隔离了谁？

答案：是长度 B 的列表，第 b 个元素 `(Q_b, Q_b)`，其中 \(Q_b = \sum_{qa} \min(2, \#\langle p\rangle)\) 是该场景查询总数。对角线 `False`、其余 `True`（禁止）——同场景**所有**查询（无论来自哪条 QA）彼此隔离，每条查询的 self-attention 只作用于自身。

**练习 2**：初始 head（进第一层解码器之前）有什么存在意义？

答案：它给出「语言查询未经任何视觉聚合」的分割基线。若模型学得好，6 层之后最终 mask 应显著优于初始 mask；辅助损失也让梯度不必穿过全部 6 层才能回传到 `query_proj`，训练更稳。

### 4.4 forward_iter_pred 推理路径：逐 QA 循环

#### 4.4.1 概念说明

推理时（`self.training == False`）流程一分为二：`infer_reason_eval` 先完成所有 LLM 文本生成与查询提取（u4-l5），返回**按场景、再按 QA 组织**的查询列表 `query_list`；然后分割解码对**每条 QA 单独跑完整的 6 层解码器**。这与训练「整场景一次过」形成鲜明对比：训练按 batch 组织查询是为了效率，推理按 QA 组织是为了每条问答的掩码完全独立、可逐条写出 JSON 与算 IoU。

#### 4.4.2 核心流程

```
输入: query_list[b][k] = 场景 b 第 k 条 QA 的查询 (1 或 2, 2560)

for batch_queries in query_list:            # 逐场景
    for query in batch_queries:             # 逐 QA
        interaction_masks = [~eye(n_q_of_this_qa)]     # 通常 1×1 的 [[False]]
        queries = _get_queries([query])                # 投影到 256 维
        初始 head → pred_mask；零查询行置 1e-4
        for i in 0..5:
            cross(i) → self(i) → ffn(i) → head
            零查询行置 1e-4
        收集该 QA 最后一层 mask
返回 dict(original_queries, masks=每 QA 最后层 mask, language_losses)
```

注意计算量：每条 QA 要完整跑 6 层解码器，解码器前向次数 = QA 总数；而训练每场景只需 1 次。

#### 4.4.3 源码精读

推理分支主体：

- [uniseg3d/query_decoder.py:L1912-L1915](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1912-L1915)：调 `infer_reason_eval` 拿 `query_list`（结构为「场景 → QA → `(n_q, 2560)` 张量」，`get_query_tokens_eval` 每条 QA 返回 1~2 行，无 `<p>` 时是一行零向量，见 [L1314-L1321](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1314-L1321)）。`original_queries` 把双层列表摊平。

- [uniseg3d/query_decoder.py:L1918-L1934](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1918-L1934)：双层循环内 `queries = [query]` 把单条 QA 包成「batch=1」，`_get_queries(queries, 1)` 投影，初始 head 出第一份 mask。此处有一个**阅读陷阱**：内层 `for m, query in enumerate(queries)` 会把变量名 `query` 重新绑定回**投影前**的 2560 维原始查询张量（列表里唯一元素）。因此 L1932 的 `query.abs().sum(-1) == 0` 判断的是原始查询的零行——因为 `query_proj` 带偏置，零向量投影后不再为零，必须在投影前识别（承接 u4-l4 的结论）。

- [uniseg3d/query_decoder.py:L1932](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1932) 与 [L1942](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1942)：零查询行的 mask logit 被整体替换为常数 `1e-4`（\(\sigma(10^{-4}) \approx 0.50003\)）。这是「无接地」的显式标记：模型没找到 `<p>…</p>` 时，输出一个几乎全 0.5 的中性掩码，而不是让带偏置投影产生的伪 logit 误导评估。这一替换在初始 head 与每层 head 之后都执行一次。

- [uniseg3d/query_decoder.py:L1936-L1944](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1936-L1944)：6 层循环与训练版同构，但 `interaction_masks` 的 L 是**该 QA 自己的查询数**（1 或 2）——单查询时为 `[[False]]`，两查询时互相隔离。跨 QA 之间天然无交互（根本不在同一次前向里）。

- [uniseg3d/query_decoder.py:L1954-L1959](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1954-L1959)：`final_pred_masks` 对每条 QA 取 `[-1]`（最后一层，即第 7 份预测）。返回 dict 不含 `aux_outputs`/`scores`——推理不需要中间监督。`language_losses` 此时是空列表（推理不计算语言损失），原样透传。

训练/推理两条循环的对比：

| 维度 | 训练（L1871-L1910） | 推理（L1912-L1959） |
| --- | --- | --- |
| 查询组织 | 整场景全部查询一次前向 | 逐场景 → 逐 QA，每次 batch=1 |
| 6 层解码器执行次数 | 每场景 1 次 | 每条 QA 1 次（× 场景数 × QA 数） |
| self-attn 的 L | 场景内查询总数（可达 num_qa×2） | 该 QA 查询数（1 或 2） |
| 查询间交互 | 全部被 `~eye` 隔离 | 同 QA 内 ≤2 条隔离；跨 QA 物理隔离 |
| 零查询兜底 | 无（损失侧按 NaN/零过滤） | mask 行替换为常数 1e-4 |
| 返回 | masks + scores + 5 份 aux + 语言损失 | 仅每 QA 最终 mask + 空语言损失 |

推理分支的输出 `masks` 随后被 `Grounded_LMM_main.qa_predict_by_feat` 用 `mask[:, superpoints]` 上采样到点级并组装成 `qa_pred_masks`/`qa_gt_masks`（u3-l1 已讲），供 u6-l1 的 `Part3DGlamm_SegMetric` 计算 mIoU。

（补充：`Grounded_Decoder_Eval` 的 `forward_iter_pred`（[L3022-L3128](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L3022-L3128) 是逐行同构的另一份拷贝，评估加载的正是这个类；两者逻辑一致，读其一即可。）

#### 4.4.4 代码实践：跟踪一条 QA 的解码数据流（源码阅读型）

**实践目标**：把 u4-l5 的「查询提取」与本讲的「逐 QA 解码」串成一条完整调用链。

**操作步骤**：

1. 从 [uniseg3d/query_decoder.py:L1686](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1686)（`infer_reason_eval`）读起，找到它 append `query_token_list` 的位置，确认元素形状是 `(1 或 2, 2560)`。
2. 跳到 [L1913](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1913) 接收 `query_list`，沿 `queries = [query]` → `_get_queries` → 初始 head → 6 层循环 → `final_pred_masks` 走一遍，在纸上记下每一步张量形状（提示：`(1或2, 2560) → (1或2, 256) → (1或2, n_sp)`）。
3. 回答：一条答案里有两个指称（如 "the <p>door</p> is left of the <p>window</p>"）时，两行查询各自得到几份 mask？分别取哪一份作为最终输出？

**需要观察的现象**：形状从语言维（2560）到解码维（256）再到超点数的两次跳变都发生在哪几行。

**预期结果**：2560→256 发生在 `_get_queries` 内的 `query_proj`（L944-L947 定义的桥）；256→n_sp 发生在 `_forward_head` 的 einsum（L1762）。两行查询各自跑 7 次 head（初始+6 层）、`que_masks` 存 7 份、最终取 `[-1]`。

#### 4.4.5 小练习与答案

**练习 1**：推理为什么不像训练那样把整场景查询拼一起过解码器？

答案：三个原因：(a) 每条 QA 的掩码要独立写盘、独立算 IoU，逐 QA 组织天然对齐；(b) 各 QA 查询互不影响，避免长查询序列里互相干扰（训练已经用 `~eye` 隔离，这里干脆物理分开）；(c) 推理没有 batch 效率压力（eval 配置通常 batch=1），逐 QA 循环以清晰性优先。

**练习 2**：若把 L1932 的 `1e-4` 改成 `0.0`，掩码概率会怎么变？为什么作者选一个「略大于 0」的值？

答案：logit=0 时 \(\sigma(0)=0.5\)，与 1e-4 的 \(\approx 0.50003\) 几乎相同；选 1e-4 更像是从「中性但非严格零」的角度取的哨兵值——数值上区别可忽略，重要的是把「无接地」行与正常预测行区分开（正常行的 logit 不会恰好全场恒等）。此问无唯一标准答案，重点是意识到该行是哨兵而非概率校准。

## 5. 综合实践

**任务：不加载 LLM，用基类 `QueryDecoder` 复现训练版 `forward_iter_pred` 的骨架，验证本讲全部三个模块的行为。**

`QueryDecoder` 基类不含任何语言模型依赖（构造函数只建投影与三件套），但与 Grounded 版共享同一套 einsum 打分、attn_mask 生成与救援、6 层迭代预测逻辑——是理解本讲最便宜的实验台。

**步骤**：

1. 进入 u1-l2 环境，新建 `decoder_probe.py`：

```python
# 示例代码：基类 QueryDecoder 的最小前向（不涉及 LLM）
import torch
from uniseg3d.query_decoder import QueryDecoder

torch.manual_seed(0)
dec = QueryDecoder(
    num_layers=6, num_instance_queries=0, num_semantic_queries=0,
    num_classes=18, in_channels=32, d_model=256, num_heads=8,
    hidden_dim=1024, dropout=0.0, activation_fn='gelu',
    iter_pred=True, attn_mask=True, fix_attention=True,
    objectness_flag=False, sphere_cls=False)
dec.eval()

x = [torch.randn(120, 32), torch.randn(90, 32)]   # 两个场景的超点特征
queries = [torch.randn(3, 32), torch.randn(2, 32)]  # 模拟 _get_queries 之前的查询
out = dec.forward_iter_pred(x, queries)

print('masks:', [m.shape for m in out['masks']])      # 期望 [(3,120), (2,90)]
print('aux 份数:', len(out['aux_outputs']))           # 期望 6（基类版 score 也逐层 append）
print('每份 aux 的 mask 形状:', [a['masks'][0].shape for a in out['aux_outputs']])
# 救援不变式（attn_mask 未直接返回，见步骤 3 的临时打印验证）
```

2. 运行并核对：`masks` 形状是否为 `(查询数, 超点数)`；`aux_outputs` 份数与 4.3.3 中 Grounded 版的 5 份为何差 1（提示：基类版初始 head 同时 append 了 score，见 [L619-L623](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L619-L623)）。
3. 在 `_forward_head` 的 attn_mask 段（[L567-L572](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L567-L572)）临时加一行 `print(attn_mask.sum(-1).max().item(), attn_mask.shape[-1])`，确认打印的最大禁止数恒小于总超点数（救援生效），观察后删掉打印。**注意：改完务必还原，本手册不允许留下源码修改。**

**预期结果**：形状与份数符合上述推断；救援检查的打印值恒 `< n_sp`。**待本地验证**（本讲义写作时未实际运行）。

## 6. 本讲小结

- 解码器一层 = Cross-Attn（查询看超点，K/V 来自 `input_proj`）→ Self-Attn（`~eye` 掩码实现查询隔离）→ FFN（256→1024→256），三件套全部 post-norm、逐场景循环、不拼批，共 6 层。
- `_forward_head` 用一个 einsum `⟨LN(q), mask_feat⟩` 完成动态 mask 打分；`sigmoid<0.5 → True(禁止)` 生成下一层的 masked attention 掩码，全禁止行被重置放行，掩码 detach 不回传梯度。
- `interaction_masks = ~eye(L)`：True=禁止 → 每条查询只注意自己、跨查询零交互；L=1 时为 `[[False]]` 等价于无限制。
- 训练路径：整场景全部（2560 维）查询经 `query_proj` 桥降到 256 维后一次过 6 层，初始 head + 每层 head 共 7 份预测，zip 截断出 5 份辅助监督，最终取第 7 份；`scores` 恒为空（无类别头）。
- 推理路径：语言分支（u4-l5）完成后，逐场景逐 QA 独立跑完整 6 层解码，零查询（无 `<p>`）行的 mask 被替换为常数 1e-4 作「无接地」哨兵，每条 QA 取最后一层 mask 交给 `qa_predict_by_feat`。
- 阅读考古：`_forward_head_eval` 与 `forward_simple` 是无调用/会报错的死路径；`contras_embeds` 是计算后即弃的遗留；第 5 层 mask 因 zip 截断未被监督。

## 7. 下一步学习建议

本讲拿到了解码器的全部输出（`masks`、`aux_outputs`、`original_queries`、`language_losses`），下一讲 **u5-l2 损失函数：语言损失 + 分割损失** 正是消费这些输出的地方：`Part3dGlammCriterion_v2` 如何对 7 份 mask 预测做 BCE/Dice 监督、如何用 `original_queries` 的零行过滤无效查询、语言损失与分割损失如何加权合并。建议带着两个问题去读：辅助损失真的用了 5 份还是全部？`sp_gt_seg` 的 NaN 行在损失侧如何与零查询对齐？之后可顺读 u5-l3 把训练全流程串起来。
