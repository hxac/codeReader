# 架构取舍与总结：设计决策回顾

## 1. 本讲目标

本讲是整套手册的收官篇。前面十二讲我们「顺着代码走」——数据怎么流、模块怎么转；本讲换一个视角，「站在设计者的高度回看」：每一个关键机制背后，都藏着一个被否决的备选方案。

读完本讲，你应该能够：

1. **论证超点池化相对逐点 token 的效率优势**：用复杂度公式说明为什么 15 万个点不能直接喂给 LLM，以及为什么语言侧和分割侧可以对同一份特征采用两种不同的「token 预算」。
2. **分析 `<SEG>` 隐状态复用为分割查询的机制与代价**：对比「专用分割头」路线，说清这套设计为什么能免掉分类头和匈牙利匹配，以及「一次生成 + 二次前向」这个看似浪费的选择背后的考量。
3. **评价 Joint（3D+2D）与 3D-only 两种变体的精度/成本权衡**：包括 LoRA 而非全参微调的理由、两变体 LoRA rank 不同的含义，以及 rank 失配静默失败这一运维风险。
4. **梳理与 UniSeg3D / Mask2Former / SPFormer 的继承关系**：学会从「死代码化石」和「复制而非继承的类」中读出一个项目的演化史。

本讲引用的每一处源码都给出永久链接，行号以当前 HEAD（`4b2c733`）为准。

## 2. 前置知识

本讲不再介绍新机制，而是把已学机制放在「天平」上称重。你需要具备以下概念（前几讲已建立，这里只做一句话回顾）：

- **超点（superpoint）**：由聚类预计算的点分组，一个场景几十万个点通常只对应几千个超点；超点内所有点共享一个特征（u3-l3）。
- **`<|point|>` token**：注册进 Qwen3-VL 词表的特殊占位符，前向预钩子把超点嵌入写进这些位置（u4-l1）。
- **`<p>…</p><SEG>` 接地标记**：回答中 `<p>指称文本</p>` 界定指称区间，该区间隐状态的均值池化被复用为分割查询（u4-l4）。
- **LoRA**：冻结底座权重、只在注意力投影旁挂低秩分解 \( \Delta W = BA \) 的微调方式（u4-l2）。
- **静默失败**：`strict=False` 加载权重时形状不匹配的参数被直接丢弃、不留报错的现象（u1-l4、u6-l2）。
- **复杂度记号**：\( O(S^2) \) 表示计算量随序列长度 \( S \) 的平方增长——这是本讲反复使用的唯一数学工具。

另外，本讲会刻意区分两类陈述：**源码可见事实**（有永久链接支撑）与**合理推断**（基于源码行为的分析，会明确标注）。读架构文章时养成这个习惯，可以避免把「作者的设计意图」和「代码恰好这么写」混为一谈。

## 3. 本讲源码地图

| 文件 | 在本讲中的角色 |
|---|---|
| [README.md](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/README.md) | 官方对架构的一句话描述、Model Zoo 中的变体/参数量、致谢段落（血统来源） |
| [uniseg3d/uniseg3d.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py) | `Grounded_LMM_main` 与被其「拷贝改」的 `UniSeg3D` 并存于此，是研究继承关系的最佳标本 |
| [uniseg3d/query_decoder.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py) | 解码器族：基类 `QueryDecoder`（带专用分割头）、`Grounded_Decoder_Joint`（训练）、`Grounded_Decoder_Eval`（评估） |
| [uniseg3d/point_qwen3_vl.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/point_qwen3_vl.py) | 钩子式点 token 注入——「最小侵入接入冻结底座」这一取舍的实现载体 |
| [uniseg3d/spconv_unet.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/spconv_unet.py) | 文件首行 `Adapted from sunjiahao1999/SPFormer` 是血统链的起点 |
| [configs/ground3dlmm_train.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py) | 训练侧关键数字：`voxel_size`、`lora_r`、`max_point_tokens` |
| [configs/ground3dlmm_eval_ground3d_scannet_3d.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_ground3d_scannet_3d.py) | Joint 与 3D-only 仅差三字段的直接证据，其 docstring 亲口描述了静默失败风险 |

## 4. 核心概念与源码讲解

本讲四个最小模块，每个对应一组「取舍」：

1. 超点 token 化 vs 逐点 token（效率）
2. `<SEG>` 查询机制 vs 专用分割头（语言与分割如何对齐）
3. LoRA 容量与 Joint / 3D-only 权衡（训练与部署成本）
4. 与 UniSeg3D / Mask2Former 的继承关系（代码考古）

### 4.1 超点 vs 逐点 token：一笔效率账

#### 4.1.1 概念说明

一个 ScanNet 场景的点云通常有 \( N \approx 10^5 \) 量级的点；而 LLM 的自注意力对序列长度 \( S \) 的计算量是 \( O(S^2 d) \)、显存是 \( O(S^2) \)。如果把每个点当作一个 token 直接喂给 Qwen3-VL，序列长度会轻松超过 10 万——注意力矩阵本身就要 \( 10^5 \times 10^5 \) 个元素，无论算力还是显存都不可行。

超点池化把 \( N \) 个点压缩成 \( M \) 个超点（\( M \) 通常是数千）。压缩比 \( N/M \) 一般在 30~100 倍，而注意力开销按平方收缩：

\[ \frac{\text{FLOPs}_{\text{point}}}{\text{FLOPs}_{\text{sp}}} = \frac{(N + L)^2}{(M' + L)^2} \approx \left(\frac{N}{M'}\right)^2 \]

其中 \( L \) 是文本 token 数，\( M' \) 是进入 LLM 的超点 token 数（受 `max_point_tokens` 封顶）。取 \( N = 150{,}000 \)、\( M' = 2{,}000 \)，比值约 \( 75^2 = 5625 \) 倍——这就是「为什么必须是超点」的量化答案。

但本模块真正想讲的不是「压缩」本身，而是一个更精细的细节：**项目对同一份超点特征，在两个消费端采用了两种不同的 token 预算**。

- **语言侧**（`<|point|>` token）：允许有损压缩。语言回答是场景级、粗粒度的推理（「这个房间大约 4 米宽」），2000 个超点 token 足以承载；超限时用零参数的 FPS 聚合再压一层。
- **分割侧**（解码器 K/V）：不允许压缩。掩码预测要落到每一个超点上，丢一个超点就丢一块几何，因此解码器始终消费**全部**超点特征。

#### 4.1.2 核心流程

回顾两路消费的完整链路（前向时的分支点）：

```text
点云 (N 点)
  │  collate(): 体素量化 → SparseConvTensor        [uniseg3d.py]
  ▼
SpConvUNet 稀疏主干 → 逐体素 32 维特征
  │  extract_feat(): scatter_mean 按超点分组求均值
  ▼
逐超点特征 (M × 32)                          ← 同一份特征
  ├─► (A) 语言侧: mask_feats ──► point2token ──► <|point|> token
  │        训练时若 M > max_point_tokens(2000):
  │        FPS 聚合（按质心位置优先，按特征距离兜底）──► 有损压缩
  └─► (B) 分割侧: 同一 mask_feats 作为解码器 K/V
           完整 M 个超点，永不聚合                ──► 无损
```

关键结论：**压缩只发生在 (A) 支路，(B) 支路原封不动**。这不是实现上的巧合，而是「语言容忍丢失、分割不容忍丢失」这一判断的直接代码化。

#### 4.1.3 源码精读

**① 压缩的源头：一行 `scatter_mean`**

[uniseg3d/uniseg3d.py:1005-1026](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L1005-L1026) — `Grounded_LMM_main.extract_feat`：稀疏主干输出经 `inverse_mapping` 广播回点级后，第 1022 行用 `scatter_mean(x.features[inverse_mapping], superpoints, dim=0)` 把逐点特征按超点分组求均值，得到 `(M, 32)` 的逐超点特征，再按 `batch_offsets` 切回逐场景。整个「\( N \to M \)」的压缩就浓缩在这一行。

**② 双预算的分流点：`forward_iter_pred` 里谁被送进了语言分支**

[uniseg3d/query_decoder.py:1866-1873](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1866-L1873) — `mask_feats` 由 `self.x_mask(y)` 生成后，第 1872 行 `self.infer_reason_train(mask_feats, qa_data, ...)` 把它送进语言分支；而外层的 `inst_feats` / `mask_feats` 原样保留，供后续 `_forward_head` 逐超点打分。注意：**送进语言分支的 `mask_feats` 是「值拷贝的引用」，函数内部对它做 FPS 聚合不会影响外面的 `mask_feats`**——这正是双预算得以成立的技术前提。

[uniseg3d/query_decoder.py:1360-1370](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1360-L1370) — `infer_reason_train` 内部：仅当 `self.training and n_points > self.max_point_tokens` 时，才对**语言侧的**超点特征做 FPS 聚合（优先 `pos_aggregator` 按质心位置，兜底 `point_aggregator` 按特征距离），随后 `point2token` 升维成 `<|point|>` 嵌入。推理（`infer_reason_eval`）则不做任何聚合。

**③ 预算上限是一个训练配置项**

[configs/ground3dlmm_train.py:76](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py#L76) — `max_point_tokens=2000`：语言侧 token 预算的封顶值。README 的调参表也把它列为可改字段（[README.md:237](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/README.md#L237)）。

**④ 两个聚合器都是「零参数」的**

[uniseg3d/query_decoder.py:162-289](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L162-L289) — `FPSAggregator` 与 `FPSPositionAggregator`：两者都只做最远点采样、不含任何可学习参数。同一文件里还存在带参数的 `ConvSetAggregator`（L87）和 `PositionSoftClusterAggregator`（L120），但 `__init__` 实际实例化的是零参数那两个（[uniseg3d/query_decoder.py:953-955](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L953-L955)）。带参数版本是留下的备选方案——又一个「取舍现场」：作者试过可学习的聚合，最终选择了免费的那个（**合理推断**：零参数聚合不引入额外训练负担，也不改变 FPS 的几何覆盖保证；源码未记录选择原因）。

#### 4.1.4 代码实践

**实践目标**：亲手算出「逐点 vs 超点」的注意力开销差，并对照源码验证「压缩只作用于语言侧」。

**操作步骤**：

1. 把下面的「示例代码」保存为 `budget_calc.py`（任意目录均可，无需 GPU 和项目依赖）并运行：

```python
# 示例代码：token 预算计算器（与仓库无关，可独立运行）
def attention_cost(seq_len, d=2560):
    # 自注意力 FLOPs 量级 ~ 4*L*d^2(投影) + 2*L^2*d(注意力)，此处只比 L^2 项
    return seq_len ** 2

N = 150_000          # 典型 ScanNet 场景点数
M = 4_000            # 典型超点数
M_prime = 2_000      # max_point_tokens 封顶后的语言侧 token 数
L = 300              # 文本 prompt + 图像 token 的粗略量级

seq_point = N + L            # 逐点路线：把每个点当 token
seq_sp_lang = M_prime + L    # 超点路线（语言侧，封顶后）
seq_sp_seg = M               # 超点路线（分割侧 K/V，不封顶）

for name, s in [("逐点", seq_point), ("超点-语言侧", seq_sp_lang), ("超点-分割侧", seq_sp_seg)]:
    print(f"{name:10s} 序列长度 = {s:>8,d}   L^2 项 = {attention_cost(s):>16,d}")

ratio = attention_cost(seq_point) / attention_cost(seq_sp_lang)
print(f"\n逐点 / 超点(语言侧) 的注意力开销比 ≈ {ratio:,.0f} 倍")
print(f"压缩比 N/M = {N/M:.1f}，开销比 ≈ (N/M')^2 = {(N/M_prime)**2:,.0f}")
```

2. 打开源码对照分流点：阅读 [uniseg3d/query_decoder.py:1866-1873](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1866-L1873)，确认送入 `infer_reason_train` 的 `mask_feats` 与供 `_forward_head` 使用的 `mask_feats` 是两处独立消费；再阅读 [uniseg3d/query_decoder.py:1360-1368](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1360-L1368)，确认聚合条件是 `self.training and n_points > self.max_point_tokens`。

**需要观察的现象**：

- 脚本输出的开销比应在数千倍量级（约 5,000+ 倍）。
- 源码中 FPS 聚合只出现在 `infer_reason_train`（语言分支）内部；`infer_reason_eval`（L1686 起）和 `_forward_head`（L1733 起）均无聚合调用。

**预期结果**：你会得出「逐点路线的注意力开销是超点路线的数千倍」这一结论；同时在源码里确认分割侧从未被压缩——双预算设计成立。

**待本地验证**：脚本本身纯 Python 可直接运行；若想验证真实场景的 \( N \) 与 \( M \)，可加载任一场景的 `points/*.bin` 与 `super_points/*.bin` 统计（方法见 u2-l3、u3-l3），本讲不展开。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `max_point_tokens` 从 2000 调到 4000，语言侧注意力开销增加多少？分割侧呢？

**答案**：语言侧 \( (M' + L)^2 \) 项约变为原来的 \( (4000+300)^2 / (2000+300)^2 \approx 3.5 \) 倍；分割侧完全不变——因为解码器 K/V 消费的是全部超点特征，与 `max_point_tokens` 无关（该值只在 `infer_reason_train` 的聚合分支被读取）。

**练习 2**：为什么 FPS 聚合优先用「质心位置距离」（`FPSPositionAggregator`）而不是「特征距离」（`FPSAggregator`）？

**答案**：FPS 的价值在于几何覆盖——按位置最远采样能保证被保留的超点均匀铺满整个房间，避免采样扎堆；而特征距离下的「最远」可能反复挑出离群特征，几何覆盖无保证。此外位置聚合依赖的质心由 [uniseg3d/uniseg3d.py:1164-1167](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L1164-L1167) 用 `scatter_mean` 对原始米制坐标计算得出。特征距离版本仅作为 `point_positions` 缺失或长度不匹配时的兜底（L1361-1365 的条件分支）。

**练习 3**：`ConvSetAggregator` 与 `PositionSoftClusterAggregator` 定义了却没被用，这说明了什么？

**答案**：它们是被尝试后放弃（或预留）的方案。项目用「保留备选实现」的方式记录了设计空间：可学习聚合（Conv1d + 自适应池化 / 可学习聚类中心）都存在，但最终选了零参数 FPS。阅读开源项目时，这类「未接线模块」是还原设计决策的重要线索。

---

### 4.2 `<SEG>` 查询机制 vs 专用分割头：语言和分割如何对齐

#### 4.2.1 概念说明

分割模型的传统做法是**专用分割头**：每个查询经过 `out_cls` 投影到 \( \mathbb{R}^{K+1} \)（\( K \) 个闭合词表类别 + 背景），训练时用匈牙利匹配把预测和 GT 一一配对，推理时按分类分数挑 Top-K。本项目的基类 `QueryDecoder`（继承自 UniSeg3D）就是这个形态。

Ground3D-LMM 换了一条路：**分割查询直接取自语言塔的隐状态**。模型生成形如「the handle is `<p>about 15 cm</p><SEG>`」的回答，`<p>…</p>` 之间指称文本的最后一层隐状态做均值池化，经 `query_proj` 降到 256 维，就是掩码解码器的查询。README 对此的官方描述在 [README.md:38-40](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/README.md#L38-L40)。

两条路线的对比：

| 维度 | 专用分割头（基类路线） | `<SEG>` 隐状态复用（本项目路线） |
|---|---|---|
| 「类别」从哪来 | 闭合词表 \( K \) 类，需 `out_cls` | 开放词表——回答文本本身就是类别 |
| 预测-GT 对齐 | 匈牙利匹配（最优指派） | `<p>` 标签个数决定行数，天然逐行对齐 |
| 语言与分割的对齐 | 需要 CLIP 式文本编码器搭桥 | 隐状态在生成回答的同一前向内产生，天然对齐 |
| 风险 | 词表封闭，新类别即失效 | 分割质量绑定语言塔的隐状态质量 |

第三行是这套设计的核心红利：回答「哪一个物体」和分割「同一物体」由**同一组隐状态**承载，不存在「文本理解对了、分割指错了」的双塔错位。

#### 4.2.2 核心流程

训练与推理中查询的产生路径不同：

```text
训练（teacher forcing，一次前向）:
  messages(多轮 QA) → processor 编码 → input_ids
  → Qwen3-VL 前向（labels 只落在 assistant 回复上）
  → 最后一层隐状态
  → get_query_tokens_id(): 在 input_ids 中按 assistant 轮次切区间，
     取 <p>…</p> 之间隐状态的均值     → 每条 QA 最多 2 个查询
  → query_proj: 2560 → 256           → 6 层解码器精炼 → 掩码

推理（两阶段，每条 QA 两次 LLM 前向）:
  阶段 1  generate(): 贪心解码生成回答文本（含 <p>…</p><SEG>）
  阶段 2  重组完整序列(prompt+生成段) → use_cache=False 再前向一次
          → 取最后一层隐状态，切片对准生成段
          → get_query_tokens_eval(): 全局搜 <p>…</p> → 查询
```

#### 4.2.3 源码精读

**① 基类的专用分割头长什么样（对照组）**

[uniseg3d/query_decoder.py:499-509](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L499-L509) — 基类 `QueryDecoder.__init__` 构建 `out_norm`、`out_cls`（`d_model → num_classes+1` 的分类头）、`out_score`（objectness 打分头）与 `x_mask`。这些就是「专用分割头」路线的全部零件。

[uniseg3d/query_decoder.py:538-575](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L538-L575) — 基类 `_forward_head` 同时产出 `cls_preds / pred_scores / pred_masks / attn_masks` 四元组；分类预测是第一公民。

**② Grounded 版把分类头整个绕开了**

[uniseg3d/query_decoder.py:1733-1772](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1733-L1772) — `Grounded_Decoder_Joint._forward_head` 是同名重写：返回值只剩 `pred_scores, pred_masks, attn_masks`，函数体内**没有一处调用 `out_cls`**，`pred_mask` 直接由 `torch.einsum('nd,md->nm', norm_query, mask_feats[i])` 的查询-特征点积得到。「类别」由回答文本承担，分类头失业。

**③ 查询的诞生地：均值池化 `<p>…</p>`

[uniseg3d/query_decoder.py:1268-1322](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1268-L1322) — `get_query_tokens_eval`：推理版提取。在 `input_ids` 里找全部 `<p>` / `</p>` 的位置，取两层隐状态 `hidden_flat[p_start+1 : p_end]` 做 `mean(dim=0)` 得到一条查询，每条 QA 至多保留 2 条（`qa_queries[:2]`）；找不到 `<p>` 时返回零向量（后续被 1e-4 哨兵掩码兜底）。

[uniseg3d/query_decoder.py:944-947](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L944-L947) — `query_proj`：`RMSNorm → Linear(2560→256) → GELU → Linear(256→256)`。这座桥把语言塔 2560 维隐状态拉回解码器的 256 维世界——因为隐状态本是为「预测下一个词」优化的表示，直接当分割查询未必合用，需要一个可学习的投影垫层。

**④ 「一次生成 + 二次前向」：代价与动机**

代价是明确的：**每条 QA（多轮则每轮）要跑两次完整 LLM 前向**。先看两段源码：

[uniseg3d/query_decoder.py:1493-1510](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1493-L1510) — `_generate_response`：贪心解码（`do_sample=False`、`repetition_penalty=1.2`）生成回答。注意它已经传了 `output_hidden_states=True`——生成路径的隐状态**拿得到**。

[uniseg3d/query_decoder.py:1591-1602](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1591-L1602) — 一段**被注释掉的代码**：逐解码步从 `outputs.hidden_states[-1]`（结构为 `(Step, Layer)` 的嵌套元组）取每步最后一层、再 `torch.cat` 拼接成 `(1, gen_len, hidden)`。这是「只前向一次、直接用生成隐状态」的方案，被放弃了。

[uniseg3d/query_decoder.py:1512-1531](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1512-L1531) — `_get_hidden_states_and_query_token`：实际采用的方案。把 prompt 与生成段重组成完整序列，`use_cache=False` 再做一次 teacher-forcing 全前向（`torch.no_grad()` 下），取最后一层隐状态并切片 `[:, -output_ids.shape[1]:]` 对准生成段。

动机分析（**源码可见事实** + **合理推断**分开说）：

- **事实**：替代方案（逐步拼接）写过、被注释保留在 L1591-1602；现行方案的注释写着 "mirror single-turn: rebuild full-sequence inputs"（[L1671](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1671)）。
- **推断**：逐步拼接要处理 `(Step, Layer)` 嵌套结构、还要注意丢掉首步（注释代码里的 `[1:]`）、并与 `output_ids` 精确对齐——细节多、易错；而二次前向是一次普通的全序列前向，得到的隐状态与训练时（`infer_reason_train` 同样是全序列 teacher forcing，[L1430-1434](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1430-L1434)）的分布**严格同型**。训练-推理一致性很可能是真正动机：查询提取路径训练和推理走同一形状的前向，避免「训练时隐状态来自全前向、推理时来自带 KV cache 的增量解码」造成的分布偏移。此外二次前向在 `no_grad` 下进行，不做梯度回传，纯粹是为了一组「干净的」隐状态。
- **代价**：推理吞吐近乎减半（LLM 侧），外加峰值显存里多存一份完整序列激活。这是一个**用算力换正确性/一致性**的典型取舍。

**⑤ 查询零向量的兜底：1e-4 哨兵**

[uniseg3d/query_decoder.py:1932](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1932) — 推理路径中，零查询行（回答里没有 `<p>`）的预测掩码被整体置为常数 `1e-4`（sigmoid 后约 0.5 的临界值之下的平的 logits）。因为 `query_proj` 带偏置，零向量投影后并非零，必须在投影前的原始查询上判定无效——这正是 u4-l4 讲过的「零向量 ∧ 投影前判定」契约，这里不再展开。

#### 4.2.4 代码实践

**实践目标**：亲眼确认「专用分割头在 Grounded 路线中被绕开」，并量化「二次前向」的开销结构。

**操作步骤**：

1. 用只读命令并排打印两个 `_forward_head`（示例命令，在你本机的仓库根目录执行）：

```bash
# 基类版（含分类头）
sed -n '555,575p' uniseg3d/query_decoder.py
# Grounded 版（无分类头）
sed -n '1756,1772p' uniseg3d/query_decoder.py
```

2. 数一数两段代码里 `out_cls` / `out_score` 出现的次数。
3. 再看被放弃的方案与现行方案：

```bash
sed -n '1591,1602p' uniseg3d/query_decoder.py   # 注释掉的逐步拼接
sed -n '1512,1531p' uniseg3d/query_decoder.py   # 现行的二次前向
```

4. （可选，需 GPU 与 checkpoint）在 u1-l4 的快速上手命令里给 `--cfg-options` 追加一个子任务，分别统计「有 `<p>` 接地的子任务」（如 `functional_part_grounding`）与「纯文本子任务」（`existence_verification`）的每场景耗时差。**待本地验证**。

**需要观察的现象**：步骤 1-2 中，基类段里 `cls_preds.append(self.out_cls(norm_query))` 一类语句在 Grounded 段完全消失；步骤 3 中注释代码与现行代码形成鲜明对照。

**预期结果**：Grounded 版 `_forward_head` 只保留掩码点积（`einsum('nd,md->nm', ...)`），`out_cls` 虽然仍被 `__init__` 构建（继承自基类，L499-503）却永不登场——这就是「分类头失业」的化石证据。

#### 4.2.5 小练习与答案

**练习 1**：`<SEG>` 路线为什么可以不做匈牙利匹配？

**答案**：因为预测行与 GT 行的对齐由「`<p>` 标签个数契约」天然确定：答案里有几个 `<p>…</p>`，就有几条查询，GT 侧（数据管线 `get_seg_from_single_qa`，见 u2-l4）也按同样规则生成同样行数的超点级 GT，两侧行号一一对应。专用分割头路线中「预测 100 个查询、GT 只有 3 个实例」的二部图匹配问题在这里根本不存在。

**练习 2**：既然 `generate` 已经传了 `output_hidden_states=True`，为什么还要二次前向？

**答案**：见 4.2.3 ④。短答：逐步生成的隐状态组织成 `(Step, Layer)` 嵌套结构，拼接对齐易错；更重要的是训练时查询来自全序列 teacher-forcing 前向，二次前向让推理与训练的隐状态分布严格同型（一致性优先），代价是每条 QA 两次 LLM 前向。

**练习 3**：`existence_verification` 这类无 `<p>` 的任务在这套机制下怎么运转？

**答案**：语言分支照常生成「yes/no」回答，`get_query_tokens_eval` 找不到 `<p>` 返回零向量查询，其预测掩码被置为 1e-4 哨兵（L1932）；评估侧该任务的 GT 是全 NaN 哨兵行、被 `get_iou` 的「行和>0」判据自动剔除（见 u6-l1），不参与 mIoU。纯文本任务以近乎零成本搭上同一架构。

---

### 4.3 LoRA 容量与 Joint / 3D-only 权衡：训练与部署的成本函数

#### 4.3.1 概念说明

本模块把两个看似独立的决策放在一起讲，因为它们在源码里被同一组字段（`lora_r` / `lora_alpha` / `image_dir`）串起：

**决策 A：LoRA 而非全参微调。** 底座 Qwen3-VL-4B 共 4.47B 参数（[README.md:209](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/README.md#L209)）。全参微调在 bf16 + AdamW 下，仅「参数 + 梯度 + 两个动量」就要约 \( 16 \times 4.47\text{B} \approx 72 \) GB 显存，还未计激活；LoRA 把可训练量压到约 400M（同上），且底座冻结后梯度不流过主干的大部分算子。

LoRA 的参数量公式（每个被注入的线性层）：

\[ \text{params}_{\text{LoRA}} = r \times (d_{in} + d_{out}) \]

\( r \) 是秩，\( d_{in}, d_{out} \) 是该层的输入/输出维度。秩越大，旁路表达能力越强、可训练参数越多。

**决策 B：发布 Joint（3D+RGB 帧）与 3D-only 两个变体。** 两变体共享全部解码代码，配置只差三个字段；Joint 是论文主结果，3D-only 是「无需准备不可再分发的 ScanNet 图像、部署门槛更低」的实用版本（[README.md:195-200](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/README.md#L195-L200)）。

两决策的交汇点：**3D-only 变体用的是更大的 LoRA（r=32/α=64），Joint 用 r=16/α=32**。

#### 4.3.2 核心流程

先看可训练参数是怎么被「圈定」出来的（`_init_reason_model` 的三步）：

```text
第 1 步  加载并整体冻结 Qwen3-VL 底座
         reason_model.requires_grad_(False)          → 4.47B 全冻
第 2 步  词表扩容：新增 <p> </p> <SEG> <|point|> 等 6 个特殊 token，
         嵌入矩阵按旧行均值初始化
第 3 步  peft 注入 LoRA（只挂语言塔注意力 q/k/v/o 投影）
         随后二次修剪：
           · 解冻 embed_tokens / lm_head   ← 词表扩容的必然要求
           · 显式冻结 visual（视觉塔）
         最终打印 Total / Learnable 参数统计
```

为什么第 3 步必须解冻嵌入层？因为第 2 步给嵌入矩阵**新增了 6 行**，这些行如果保持冻结，`<|point|>` / `<p>` / `<SEG>` 的嵌入将永远停留在「全体旧嵌入的均值」这个初始化上，模型无法学会区分它们——点 token 注入和查询提取就都成了空中楼阁。源码采取的简化做法是把整个 `embed_tokens` / `lm_head` 解冻（子串匹配，见下），而不是只解冻新增行。

再看变体差异的传导链：

```text
Joint (r=16)  ──消费──► 一帧 448×448 RGB（数百视觉 token）+ 全部超点 token
3D-only (r=32) ──消费─► 仅超点 token（image_dir=None，消息里 image 条目被过滤）
```

#### 4.3.3 源码精读

**① 冻结 → 扩词表 → 注入 → 修剪 的完整链路**

[uniseg3d/query_decoder.py:978](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L978) — 第 1 步：`self.reason_model.requires_grad_(False)` 整体冻结。

[uniseg3d/query_decoder.py:984-991](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L984-L991) — 第 2 步：`add_special_tokens` 注册 6 个特殊 token、`resize_token_embeddings` 扩容，新增行用旧嵌入均值初始化（`i_emb[-new_tokens:] = i_emb[:-new_tokens].mean(0, keepdim=True)`）。

[uniseg3d/query_decoder.py:998-1008](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L998-L1008) — 第 3 步：`get_peft_model` 按 `LoraConfig(r=lora_r, lora_alpha=lora_alpha, ...)` 注入。

[uniseg3d/query_decoder.py:1010-1019](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1010-L1019) — 二次修剪与统计：名字含 `embed_tokens` / `lm_head` 的参数解冻、含 `visual` 的显式冻结；L1018-1021 打印 `Total params / Learnable params (ratio%)`——**这是验证参数量口径的官方入口**，任何关于「LoRA 占多少」的讨论都应以此为锚。

**② LoRA 挂在哪：范围由一个字符串决定**

[uniseg3d/query_decoder.py:1025-1047](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1025-L1047) — `get_target_modules`：把 `lora_type`（如 `qkvo_all`）拆成「层型 + 范围」两段，只有名字以 `q_proj/k_proj/v_proj/o_proj/attn.qkv/attn.proj` 结尾的模块进入注入名单。配合二次修剪中的「visual 全冻」，最终效果是 **LoRA 旁路只活在语言塔注意力上**；~400M 可训练参数的大头并不是 LoRA 旁路，而是被解冻的词嵌入层（Qwen3-4B 的词表约 15 万、隐藏维 2560，单份嵌入即约 3.9 亿参数；量级估算，**精确数值待本地验证**——跑一次模型构建看 L1018 的日志即可）。

**③ 三个字段定义了两个产品**

[configs/ground3dlmm_eval_ground3d_scannet_3d.py:19-27](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_ground3d_scannet_3d.py#L19-27) — `_3d` 配置整体继承 joint 配置，只覆盖 `lora_r=32, lora_alpha=64, image_dir=None` 三项。整个「变体系统」的表面积就这么大。

[configs/ground3dlmm_eval_ground3d_scannet_3d.py:6-12](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_ground3d_scannet_3d.py#L6-L12) — 这段 docstring 是全仓库对「静默失败」最直白的自白：把 r=32 的权重装进 r=16 的模型，失配的 adapter 会被 `strict=False` 静默忽略，**模型无声退回基座 Qwen**。作者为此让每个 checkpoint 配一个专属配置，并在 `eval_ground3d.sh` 里做了 rank 自动探测（u6-l2）。

**④ 3D-only 不是独立代码分支，而是「把图像条目置空」**

[uniseg3d/query_decoder.py:1478-1485](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1478-L1485) — `_prepare_messages_data`：`image=None` 的 image 条目在编码前被统一过滤，序列里一个视觉 token 都没有。两变体共享全部解码代码——实现一个变体的成本是三行配置，而不是一个分支。

**⑤ 图像的成本被钳在固定预算内**

[uniseg3d/query_decoder.py:1550-1554](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1550-L1554) — Joint 变体每条 QA 的消息里挂一帧图像，`max_pixels=448*448 / min_pixels=224*224` 把视觉 token 数钳在窄区间。Joint 相对 3D-only 的额外推理成本 = 一帧图像的视觉 token 编码 + 注意力序列加长，被这两个常数封顶。

#### 4.3.4 代码实践

**实践目标**：把「变体 × LoRA 容量 × 成本」整理成一张可核对的表，并找到官方参数量口径的打印位置。

**操作步骤**：

1. 阅读三处源码/文档，填写下表（答案见本实践末尾）：

| 字段 | Joint（主结果） | 3D-only | 出处（文件:行） |
|---|---|---|---|
| `lora_r` / `lora_alpha` | ? | ? | |
| `image_dir` | ? | ? | |
| 每条 QA 额外输入 | ? | ? | |
| 需要准备 ScanNet 原始图像？ | ? | ? | |
| checkpoint | ? | ? | |

   出处提示：[README.md:204-213](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/README.md#L204-L213)、[_3d 配置 L19-27](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_ground3d_scannet_3d.py#L19-L27)、[README.md:195-200](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/README.md#L195-L200)。

2. 用 LoRA 公式估算两变体旁路参数量的**相对关系**：同一组目标层下，r=32 的旁路参数是 r=16 的多少倍？（不需要绝对数——绝对数以日志为准。）
3. 定位官方统计入口：阅读 [uniseg3d/query_decoder.py:1018-1021](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1018-L1021)，记住这条日志的格式；若你有 GPU 环境，跑一次模型构建（如 u1-l4 的推理命令）即可在日志开头看到 `Total params ... Learnable params ...`。**待本地验证**。

**需要观察的现象**：表格填完后，你会发现两变体的差异面积极小（3 个配置字段 + 一帧图像），而行为差异（是否需要图像数据、LoRA 容量）全部由这几个字段解释。

**预期结果（参考答案）**：

| 字段 | Joint | 3D-only | 出处 |
|---|---|---|---|
| `lora_r` / `lora_alpha` | 16 / 32 | 32 / 64 | README L206-207；`_3d` 配置 L23-24 |
| `image_dir` | 指向帧图像目录 | `None` | `_3d` 配置 L25 |
| 每条 QA 额外输入 | 一帧 448×448 RGB | 无 | query_decoder L1550-1554 |
| 需要 ScanNet 原始图像？ | 需要（不可再分发，需自行抽取） | 不需要 | README L195-200 |
| checkpoint | Ground3D-LMM-4B-Joint | Ground3D-LMM-4B-3D | README L206-207 |

步骤 2 答案：r=32 与 r=16 的旁路参数量之比 = \( 32/16 = 2 \)（同一组目标层、同维度，LoRA 参数对 \( r \) 是线性的）。

#### 4.3.5 小练习与答案

**练习 1**：为什么解冻 `embed_tokens` / `lm_head` 是 LoRA 方案的必要补充，而不是可选项？

**答案**：词表扩容新增了 6 行嵌入（`<p>`、`</p>`、`<SEG>`、`<|point|>` 等，L984-991）。LoRA 只挂在注意力投影上、碰不到嵌入矩阵；若嵌入保持冻结，新 token 的表示永远停留在均值初始化，`point_mask = input_ids == point_token_id` 定位到的位置拿到的将是同一个「平均词」，点云注入与 `<SEG>` 查询提取都会失效。解冻整层是实现上的简化（子串匹配 L1011-1012），代价是可训练参数的大头变成了嵌入层而非 LoRA 旁路。

**练习 2**：3D-only 变体用了更大的 LoRA rank，一种解读是「用 LoRA 容量补偿缺失的视觉通道」。这个说法在源码里能找到直接证据吗？

**答案**：找不到直接证据，它属于**合理推断**。源码事实只有：两 checkpoint 的 rank 不同（README L206-207、`_3d` 配置 L21-27），以及 3D-only 没有图像输入。「rank 差异是刻意为补偿视觉信息而设计」是解释性叙事，仓库没有任何注释或文档这样声明。架构分析时要能把这两类陈述分开——这正是本讲反复强调的方法论。

**练习 3**：如果把 Joint 的 checkpoint 错装进 joint 配置但手动把 `lora_r` 改成 32，会发生什么？

**答案**：LoRA 旁路权重形状失配（A/B 矩阵的秩维度对不上），`strict=False` 加载时被静默丢弃，模型退回未微调的基座 Qwen3-VL 继续出结果——不报错、不崩溃、指标崩坏。`_3d` 配置 docstring（L6-12）和 u6-l2 讲的 `probe_lora_rank` 自动探测都是针对这个隐患的防御。这类「静默失败」是本架构最值得警惕的运维风险。

---

### 4.4 与 UniSeg3D / Mask2Former 的继承关系：代码考古

#### 4.4.1 概念说明

Ground3D-LMM 不是从零写的，它的血统链是：

```text
SPFormer（稀疏 U-Net 实现）
   └─► UniSeg3D（稀疏编码器 + Mask2Former 式解码器 + 多任务分割）
          └─► Ground3D-LMM（把「查询」换成语言 <SEG>，把闭合词表换成开放 QA）
Mask2Former ──(设计思想)──► 解码器的 masked attention 与迭代预测
Qwen3-VL    ──(底座)──────► 语言塔（钩子式注入点 token）
mmdet3d/mmengine ──(框架)─► 注册表、Config、数据管线、Runner
```

官方致谢见 [README.md:402-409](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/README.md#L402-L409)；[uniseg3d/spconv_unet.py:1](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/spconv_unet.py#L1) 的 `# Adapted from sunjiahao1999/SPFormer.` 是链条最上游的书面证据。

研究「继承」有两条互补的路径：读文档致谢（作者想让你知道的），和读代码化石（代码自己暴露的）。后者包括三类标本：

1. **复制而非继承的类**：`Grounded_LMM_main` 与 `UniSeg3D` 平行注册、互不继承。
2. **被注释掉的调用**：`loss()` 里注释掉的 `_select_queries` 调用。
3. **构建了却不再使用的组件**：基类的 `out_cls` 分类头、配置里的 `query_thr`。

#### 4.4.2 核心流程

把「从 UniSeg3D 到 Ground3D-LMM 改了什么」提炼成一张对照表（这是理解本项目创新点最快的透镜）：

| 环节 | UniSeg3D（基类形态） | Ground3D-LMM |
|---|---|---|
| 3D 主干 | SpConvUNet + scatter_mean 超点池化 | **原样保留**（逐字复制） |
| 查询来源 | 超点特征本身（`_select_queries`）+ 可学习 query | 语言塔 `<p>…</p>` 隐状态 |
| 分类 | `out_cls` 闭合词表 + 匈牙利匹配 | 无分类头，回答文本即类别 |
| 语言能力 | CLIP 式文本编码器 `lang` | 完整 Qwen3-VL-4B + LoRA |
| 损失 | mask BCE/Dice + 分类 CE | mask BCE/Dice + 语言交叉熵 |
| 数据 | 实例/语义/全景分割标注 | 247 万 QA 对 |

**继承的是骨架，替换的是灵魂**：查询从哪来、类别从哪定义——这两处替换正是「分割模型 → 接地语言模型」的分水岭。

#### 4.4.3 源码精读

**① 标本一：平行注册的两个入口类**

[uniseg3d/uniseg3d.py:260-261](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L260-L261) 与 [uniseg3d/uniseg3d.py:905-906](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L905-L906) — `UniSeg3D` 与 `Grounded_LMM_main` 各自 `@MODELS.register_module()` 注册，是两个平行的顶层类，**没有继承关系**。代价与收益：`Grounded_LMM_main` 需要重写 `loss`/`predict` 的主体（查询机制彻底变了），继承父类反而会被旧接口束缚；于是选择整类拷贝——`extract_feat`（L1005-1026 vs 基类 L362-383）与 `collate`（L1028-1060）逐字相同。**代价**是上游 UniSeg3D 的任何改进不会自动同步；**收益**是改造零顾忌。这与 u7-l2 总结的「拷贝改优先」哲学一脉相承。

**② 标本二：被注释掉的调用与永不生效的开关**

[uniseg3d/uniseg3d.py:1137-1162](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L1137-L1162) — `Grounded_LMM_main.loss` 中，`_select_queries` 的调用、`text_token` 分支、`interaction_masks` 构建等大段逻辑整块被注释。查询来源已改为 `self.decoder(x, scene_names=..., qa_data=selected_qa_data, ...)`（L1172）。

[uniseg3d/uniseg3d.py:1066-1094](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L1066-L1094) — 但 `_select_queries` 方法本身还在（化石）。配套的 `query_thr=0.5` 仍写在训练配置里（[configs/ground3dlmm_train.py:36](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py#L36)），却不再有任何代码路径读取它参与计算——它只作为 `__init__` 的一个属性被存储。同理还有 `pred_iou`、`set_query_mask` 等开关（L937-946）。这也是训练必须 `find_unused_parameters=True` 的原因之一（u3-l1、u5-l3）。

**③ 标本三：失业的零件与它的主人**

[uniseg3d/query_decoder.py:500-506](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L500-L506) — `out_cls` / `out_score` 在基类构建、被 `Grounded_Decoder_Joint` 继承，但 Grounded 版 `_forward_head`（L1733-1772）从不调用。它们是被「绕开」而非「删除」——删除需要动基类，绕开只需重写一个方法。

**④ Mask2Former 的遗产：三件套与 masked attention**

[uniseg3d/query_decoder.py:311-450](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L311-L450) — `CrossAttentionLayer / SelfAttentionLayer / FFN` 三件套逐层堆叠（配置 6 层），即 Mask2Former 解码器层的 3D 翻版；masked attention 体现在 [uniseg3d/query_decoder.py:1764-1769](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1764-L1769)：`attn_mask = (pred_mask.sigmoid() < 0.5).bool()`——查询只注意上一轮预测掩码内的超点，全禁止行被救援放行并 `detach()`。迭代精炼（每层结束都出一份掩码预测、前 5 份作辅助深监督）同样是 Mask2Former 的 `iter_pred` 传统（L1853-1959 的循环结构）。

**⑤ 解码器族的四次分叉**

[uniseg3d/query_decoder.py:881](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L881)、[L1982](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1982)、[L3152](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L3152)、[L3273](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L3273) — 四个解码器类依次是 `Grounded_Decoder_Joint`（训练）、`Grounded_Decoder_Eval`（Ground3D 评估）、`Grounded_Decoder_Eval_MultiImg` / `Grounded_Decoder_Eval_Text`（ScanRefer/Reason3D 迁移，见 u7-l1）。注意 `Grounded_Decoder_Eval` 不是 `Grounded_Decoder_Joint` 的子类，而是它的「评估世界孪生」：`_init_reason_model` 在 L959-1023 与 L2064-2128 **近乎逐字重复**。又一次「拷贝改」：训练与推理的分支逻辑差异太大（一次前向 vs 生成+二次前向），共享基类反而要塞满 `if self.training`。

#### 4.4.4 代码实践

**实践目标**：用只读命令做一次「化石考古」，亲手验证本模块列举的三类标本，并画出项目的血统图。

**操作步骤**（在仓库根目录执行，全部只读）：

```bash
# 1. 找到 _select_queries 的定义与(被注释的)调用 —— 注意第二列行号
grep -n "_select_queries" uniseg3d/uniseg3d.py

# 2. 看 query_thr 是否真的"只被存储、从未被用"（除 __init__ 赋值与定义外应无他用）
grep -n "query_thr" uniseg3d/uniseg3d.py configs/ground3dlmm_train.py

# 3. 确认 out_cls 只在基类 _forward_head 里被调用
grep -n "out_cls" uniseg3d/query_decoder.py

# 4. 看主干文件的出身注释
head -1 uniseg3d/spconv_unet.py

# 5. (可选) 用 git 历史交叉验证主干与解码器的演化节奏
git log --oneline --follow -- uniseg3d/spconv_unet.py | head -20
git log --oneline -- uniseg3d/query_decoder.py | head -20
```

**需要观察的现象**：

- 步骤 1：`_select_queries` 的**定义**出现在 `Grounded_LMM_main` 内（L1066 附近），而 `loss` 中对它的调用整行带 `#` 前缀（L1137 附近）。
- 步骤 2：`query_thr` 只出现在 `__init__` 签名、赋值语句和配置里，没有任何读取它做计算的表达式。
- 步骤 3：`out_cls` 的调用全部落在基类的 `_forward_head`（L562、L760 一带），Grounded 子类的 `_forward_head`（L1733 起）中没有。

**预期结果**：三类「化石」全部得到命令行证据；步骤 5 的 git 历史可佐证 `spconv_unet.py` 几乎不再改动（成熟继承件），而 `query_decoder.py` 持续演化（本项目创新集中地）。**待本地验证**（git 历史输出依赖仓库状态）。

#### 4.4.5 小练习与答案

**练习 1**：`Grounded_LMM_main` 为什么不继承 `UniSeg3D`？

**答案**：因为改造发生在 `loss`/`predict` 的主体（查询来源、GT 组织、输出结构全变了），能原样保留的只剩 `extract_feat`/`collate` 两个方法。若继承，父类大量不适用的方法（`add_text_prompt_queries`、`split_original_and_*` 等）都会挂在新类上，徒增误用风险；整类拷贝则可以大刀阔斧地删注释、换逻辑。代价是代码重复（`collate`/`extract_feat` 两份）与上游脱钩——这是「改造深度」与「代码复用」之间的经典权衡点：改动越深，越倾向拷贝。

**练习 2**：`Grounded_Decoder_Eval` 与 `Grounded_Decoder_Joint` 有大段重复代码，重构成一个类 + `if self.training` 分支是否更好？

**答案**：不一定。两类的差异不是一两个 if：训练走一次 teacher-forcing 前向 + `preprocess_chatml` 标签；评估走 `generate` + 二次前向 + 逐 QA 循环 + JSON 写盘。合并会让每个方法都充满分支、测试组合爆炸，且评估侧的改动（如 u7-l1 新增 `Eval_MultiImg`/`Eval_Text`）会威胁训练侧的稳定性。「按模式分叉、允许重复」对研究代码是更低风险的选择——这与练习 1 是同一条原则的两次应用。

**练习 3**：从血统链的角度，Ground3D-LMM 相对 UniSeg3D 的「最小创新集」是什么？

**答案**：三件事——(1) 查询来源换成语言塔 `<p>…</p>` 隐状态（`query_proj` 桥 + `get_query_tokens_*`）；(2) 用 Qwen3-VL + 钩子式点 token 注入取代 CLIP 式文本编码器，使模型能生成自由文本回答；(3) 损失增加语言交叉熵（`CoTCriterion`）。除此之外的稀疏主干、超点池化、6 层解码器、masked attention、BCE+Dice 分割损失全部继承自 UniSeg3D/Mask2Former 传统。骨架不动、替换灵魂，正是快速验证新想法的工程智慧。

## 5. 综合实践

**任务：为 Ground3D-LMM 撰写一张「架构决策记录（ADR）表」**——这是把整套手册所学压缩成一页纸的收官练习。

对下列四项决策，各写一行记录，每行必须包含四列：**决策**、**被否决的备选方案**、**源码证据（永久链接）**、**代价/风险**。

1. 超点池化 + 语言侧 FPS 封顶（vs 逐点 token 直入 LLM）
2. `<SEG>` 隐状态复用为分割查询（vs 专用分类头 + 匈牙利匹配）
3. 推理时一次生成 + 二次前向取隐状态（vs 逐步拼接生成隐状态）
4. LoRA + 解冻嵌入层，Joint/3D-only 双变体三字段分叉（vs 全参微调、单变体）

写作要求：

- 每条源码证据必须是你**亲自打开核对过**的永久链接（本讲 4.1-4.4 各节的链接可作为起点，但至少补一条本讲未引用过的位置，例如 `infer_reason_eval` 的循环结构或 `_forward_head_eval`）。
- 「代价/风险」一列至少写出一条源码可见的代价（如二次前向的双倍 LLM 开销、rank 失配静默失败）。
- 完成后自测：遮住「决策」列，只看「备选方案」列，能否复述出每项取舍的核心理由？能，说明本讲目标达成。

这张表没有唯一答案——它是一个持续文档：当你日后给项目加新子任务（u7-l2）、换底座或调 LoRA 预算时，回填新行即可。

## 6. 本讲小结

- **超点 token 化的本质是一笔平方级的账**：逐点路线的注意力开销约为超点路线的数千倍；且项目对同一份超点特征实行**双预算**——语言侧受 `max_point_tokens=2000` 封顶、超限 FPS 聚合（有损），分割侧始终消费全部超点（无损）。
- **`<SEG>` 查询机制用「回答即类别」换掉了分类头与匈牙利匹配**：`<p>…</p>` 隐状态均值池化经 `query_proj` 降维即成分割查询，预测与 GT 靠「`<p>` 个数契约」逐行对齐；语言与分割在同一个前向内天然对齐是最大红利，风险是分割质量绑定语言塔表示质量。
- **推理的「一次生成 + 二次前向」是用算力换一致性**：被注释的逐步拼接代码（L1591-1602）记录了备选方案；现行方案让训练与推理的查询提取都来自全序列 teacher-forcing 前向，代价是每条 QA 两次 LLM 前向。
- **LoRA + 解冻嵌入层是必要组合**：词表扩容的新 token 行必须可训练，否则 `<|point|>`/`<SEG>` 永远停在均值初始化；~400M 可训练参数的大头是解冻的嵌入层而非 LoRA 旁路。Joint（r=16）与 3D-only（r=32）仅差三个配置字段，rank 失配会被 `strict=False` 静默吞掉——最需警惕的运维风险。
- **继承的是骨架，替换的是灵魂**：稀疏主干、超点池化、6 层 Mask2Former 式解码器全部承自 SPFormer/UniSeg3D/Mask2Former；真正替换的只有查询来源与类别定义。平行注册的孪生类、被注释的调用、失业的 `out_cls` 是这场替换留下的三类化石，也是读任何演化中项目的通用方法。

## 7. 下一步学习建议

至此整套手册（u1~u7）已完整覆盖 Ground3D-LMM 的数据、模型、训练、评估与二次开发。接下来建议：

1. **对照原始论文阅读**：本手册全部结论来自源码；拿论文的消融表（Joint vs 3D-only、各子任务 mIoU）与你在 u6 系列复现的数字互证，体会「论文叙述」与「代码事实」的对应与出入。
2. **动手做一个属于你的 ADR 回填**：按第 5 节的表格，挑一个真实改动（例如把 `max_point_tokens` 调到 3000 重训一个子任务，或按 u7-l2 的路线接入一个新基准），把实验结果回填进你的决策记录——这是从「读者」到「二次开发者」的临界一步。
3. **顺血统向上游读**：通读 [UniSeg3D](https://github.com/dk-liang/UniSeg3D) 与 [Mask2Former](https://github.com/facebookresearch/Mask2Former) 的解码器实现，对比哪些代码被 Ground3D-LMM 逐字保留、哪些被重写——你会对本讲 4.4 的对照表有更深的体感。
4. **关注底座演化**：`base_model` 分支（qwen2_5_vl 前向重写式 vs qwen3_vl 钩子式，见 u7-l2）预示着接入新底座的模式；若 Qwen 系列发布新版本，可按 u7-l2 的「钩子式优先」路线尝试迁移，并验证点 token 注入在 `prepare_inputs_for_generation`（[uniseg3d/point_qwen3_vl.py:131-143](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/point_qwen3_vl.py#L131-L143)）处的「仅首步注入」契约是否仍成立。
