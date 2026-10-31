# 损失函数：语言损失 + 分割损失

## 1. 本讲目标

上一讲（u5-l1）我们读完了查询解码器的六层前向，它输出的是「一堆还没被打分的预测」：`masks`、`aux_outputs`、`original_queries` 和 `language_losses`。本讲回答的问题是：**这些预测如何变成一个可以反向传播的标量损失？**

学完本讲你应该能够：

1. 说出 `Part3dGlammCriterion_v2` 如何把分割损失与语言损失合并成一个字典，以及 mmengine 如何消费它。
2. 解释 `CoTCriterion` 的输入 `language_losses` 从哪里来、为什么是「逐场景先平均、场景间再平均」。
3. 逐行读懂 `TextPrompt_Criterion_Glamm` 的 BCE + Dice 组合、有效行过滤（零查询 ∧ GT 全 NaN）、`fix_dice_loss_weight` / `fix_mean_loss` 两个缩放开关，以及 5 层辅助深监督的 \(1/i\) 加权。
4. 追踪 `sp_gt_masks` 从数据管线的 `sp_gt_seg` 列表到 criterion 形参 `insts` 的完整形状变化，理解「行对齐契约」。

## 2. 前置知识

本讲会用到以下几个基础概念，用通俗语言先过一遍：

- **多任务学习与损失加权**：一个模型同时学两件事（生成文本 + 分割点云）时，总损失通常是两项的加权和：\(\mathcal{L}_{total} = \mathcal{L}_{seg} + \mathcal{L}_{lang}\)。权重决定「梯度天平」偏向哪边。
- **交叉熵与 teacher forcing**：语言模型训练时，把标准答案（GT 回复）直接拼在 prompt 后面喂给模型，让模型在每个位置预测下一个 token，用交叉熵比较预测分布与真实 token。被标为忽略（`-100`）的位置不计损失。
- **BCEWithLogits**：二值交叉熵 + sigmoid 的数值稳定合体。分割掩码每个位置是 0/1，预测是 logits，公式为
  \[ \mathcal{L}_{bce} = -\frac{1}{M}\sum_{j}\big[g_j \log \sigma(z_j) + (1-g_j)\log(1-\sigma(z_j))\big] \]
  其中 \(z\) 是 logits，\(g \in \{0,1\}\) 是 GT，\(\sigma\) 是 sigmoid。
- **Dice 系数与 Dice loss**：衡量两个集合重叠度的指标 \(\frac{2|A \cap B|}{|A| + |B|}\)，对前景占比小的不平衡掩码比 BCE 更敏感。软化版把集合运算换成概率加权和：
  \[ \mathcal{L}_{dice} = 1 - \text{mean}\left(\frac{2\sum_j p_j g_j + \varepsilon}{\sum_j p_j + \sum_j g_j + \varepsilon}\right) \]
- **辅助深监督（deep supervision）**：解码器每一层都输出一份掩码预测，各自的损失按递减权重累加进总损失，让中间层也能收到直接监督信号（Mask2Former 的标准做法）。
- **NaN 哨兵与零向量哨兵**：承接 u2-l4 与 u4-l4 的约定——数据侧取不到有效 mask 的 GT 行整体填 NaN；解码器侧在回答中找不到 `<p>…</p>` 指称的查询是零向量。损失侧必须把这两种「无效行」过滤掉，否则 NaN 会污染整个 backward。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `uniseg3d/unified_criterion.py` | 损失组合器。`Part3dGlammCriterion_v2` 在这里注册与实现，把两个子损失器拼成一个字典 |
| `uniseg3d/cot_criterion.py` | 语言损失 `CoTCriterion`：对 `language_losses` 列表求平均 |
| `uniseg3d/text_prompt_instance_criterion.py` | 分割损失 `TextPrompt_Criterion_Glamm`：BCE + Dice + 有效行过滤 + 辅助深监督 |
| `uniseg3d/query_decoder.py` | 损失的「原料」产地：`preprocess_chatml` 构造 labels，`infer_reason_train` 产生 `language_losses` 与查询，`forward_iter_pred` 打包返回 |
| `uniseg3d/uniseg3d.py` | 模型 `loss()`：拼接 GT、调用 criterion 的唯一入口 |
| `configs/ground3dlmm_train.py` | criterion 的配置与全部权重旋钮 |

## 4. 核心概念与源码讲解

### 4.1 Part3dGlammCriterion_v2 组合器

#### 4.1.1 概念说明

Ground3D-LMM 一次训练前向同时产出两种预测：LLM 生成的 token 分布、解码器生成的逐超点掩码。相应地需要两种损失——语言建模损失（教模型「说对话」）和掩码损失（教模型「分对点」）。

`Part3dGlammCriterion_v2` 就是一个**纯组装者**：它自己不做任何数学运算，只负责

1. 把预测和 GT 分发给两个子损失器；
2. 把两个子损失器的返回字典合并成一个字典。

它在 MODELS 注册表里的名字即类名，配置中通过 `type='Part3dGlammCriterion_v2'` 引用。注意它虽然是 `nn.Module` 子类，但**不含任何可训练参数**——两个子损失器都是普通 Python 对象（不注册为子模块），criterion 是纯函数式的。

#### 4.1.2 核心流程

```text
模型 loss()（uniseg3d.py:1184）
    │
    ├─ pred = x（decoder 返回的 dict：masks / aux_outputs / original_queries）
    ├─ insts = sp_gt_masks（逐场景 GT 张量列表）
    ├─ language_loss = language_losses（逐场景语言损失列表）
    │
    ▼
Part3dGlammCriterion_v2.__call__(pred, insts, language_loss, original_queries)
    ├─ lang_seg_criterion(pred, insts, original_queries)  ──► {'text_seg_loss': ...}
    ├─ lang_criterion(language_loss)                      ──► {'cot_loss': ...}
    └─ dict.update 合并                                    ──► {'text_seg_loss', 'cot_loss'}
    │
    ▼
mmengine BaseModel.parse_losses：键名含 'loss' 的项逐项写入日志，求和得总损失
```

加权方式是**分布式**的：组合器本身没有总权重旋钮，语言/分割的相对强度由各自子损失器的 `loss_weight` / `total_weight` 控制。

#### 4.1.3 源码精读

组合器的完整实现只有 15 行：

- [uniseg3d/unified_criterion.py:L126-L141](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/unified_criterion.py#L126-L141)：`Part3dGlammCriterion_v2` 注册与实现。`__init__` 用 `MODELS.build` 构建两个子损失器；`__call__` 先算分割损失得到 `{'text_seg_loss'}`，若 `lang_criterion` 与 `language_loss` 都非 None 再 `loss.update` 合并语言损失，返回合并后的字典。

调用点在模型主入口：

- [uniseg3d/uniseg3d.py:L1172-L1187](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L1172-L1187)：训练链路中先取 `language_losses = x['language_losses']`，再以四个参数调用 `self.criterion(x, sp_gt_masks, language_losses, original_queries=x['original_queries'])`，返回值直接作为 `loss()` 的返回。

配置里的权重旋钮：

- [configs/ground3dlmm_train.py:L79-L93](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py#L79-L93)：criterion 配置。分割侧 `loss_weight=[1.0, 1.0]`（BCE 与 Dice 等权）、`total_weight=1.0`；语言侧 `CoTCriterion` 的 `loss_weight=1.0`。注意 87-89 行注释掉的 `use_focal_loss` 等字段是给 `LanguageSegCriterion_v2` 准备的开关，`TextPrompt_Criterion_Glamm` 并不接受这些参数——如果取消注释，`MODELS.build` 会直接抛 `TypeError`。

同文件里还有两个「亲戚」帮助理解演化：

- [uniseg3d/unified_criterion.py:L107-L122](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/unified_criterion.py#L107-L122)：v1 版 `Part3dGlammCriterion`——不给分割损失传 `original_queries`，因此无法过滤零查询行，这是 v2 的关键改进。
- [uniseg3d/unified_criterion.py:L89-L101](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/unified_criterion.py#L89-L101)：`ScanNetUnifiedCriterion_OnlyLLM`——只算语言损失的消融用组合器。

#### 4.1.4 代码实践

**实践目标**：不改任何代码，确认训练日志里会出现哪些损失字段。

1. 打开 [configs/ground3dlmm_train.py:L79-L93](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py#L79-L93)，记下两个子损失器各自的返回键名（下两节会验证：`text_seg_loss` 与 `cot_loss`）。
2. 追一遍调用链：`uniseg3d.py` 的 `loss()` 返回 criterion 字典 → mmengine `BaseModel.train_step` → `parse_losses`。`parse_losses` 的规则是：键名含 `loss` 的项各自写入 `log_vars`（训练日志逐项显示），求和得到 `loss`（总损失）。
3. 若你本地已跑过训练（u5-l3 会讲启动方式），翻一条日志，应能看到 `loss`、`text_seg_loss`、`cot_loss` 三个字段同屏出现。

**需要观察的现象**：日志中两项子损失与总损失满足 `loss ≈ text_seg_loss + cot_loss`（浮点求和误差内）。

**预期结果**：能说出「想调语言/分割的相对权重，改的是子损失器里的 `loss_weight`，组合器无总旋钮」。

**待本地验证**：日志字段取决于实际训练运行，本环境未运行训练。

#### 4.1.5 小练习与答案

**练习 1**：若想把语言损失的重要性降为分割损失的 1/4，最小改动是什么？

答案：把 [configs/ground3dlmm_train.py:L91-L93](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py#L91-L93) 中 `CoTCriterion` 的 `loss_weight` 从 `1.0` 改为 `0.25`。组合器没有顶层权重，只能调子损失器。

**练习 2**：为什么 mmengine 能在日志里同时记录两个子损失？

答案：`parse_losses` 按键名过滤，凡含 `loss` 的项都会逐项记入 `log_vars` 并参与求和。`text_seg_loss` 与 `cot_loss` 都满足条件。

**练习 3**：v1 与 v2 组合器的本质区别是什么？

答案：v2 多传了 `original_queries` 给分割损失器，使后者能识别「零向量查询」（无 `<p>` 指称的行）并在计算损失前剔除；v1 只能靠 GT 侧的 NaN 过滤。

### 4.2 CoTCriterion 语言损失

#### 4.2.1 概念说明

语言损失就是我们熟悉的「下一个 token 预测」交叉熵，只是套了个 Chain-of-Thought（CoT）的名字。它要教 Qwen3-VL 在看到点云 token 和问题后，生成正确的回答——回答里包括 `<p>…</p><SEG>` 这样的接地标记，所以语言损失也间接教会了模型「何时输出接地标记」。

关键点在于：**损失只落在 assistant 回复上**，问题和角色头都不算。这是由 u4-l3 讲过的 `preprocess_chatml` 构造的 labels 决定的。

#### 4.2.2 核心流程

```text
每个场景 b（共 B 个）：
  messages（多轮对话） ──apply_chat_template──► text
  processor(text, images) ──► input_ids
  labels = preprocess_chatml(input_ids, text)   # 提问与角色头 → -100
  output = reason_model(input_ids, labels=labels)
  language_losses.append(output.loss)           # HF 内部：非忽略 token 上的平均交叉熵

CoTCriterion(language_losses):
  cot_loss = loss_weight × mean(stack(language_losses))
```

数学上：

\[ \mathcal{L}_{cot} = \lambda \cdot \frac{1}{B}\sum_{b=1}^{B} \ell_b, \qquad \ell_b = \frac{1}{|T_b|}\sum_{t \in T_b} \text{CE}(z_{b,t},\, y_{b,t}) \]

其中 \(T_b\) 是场景 b 中所有非忽略的 assistant token 位置，\(\lambda = 1.0\)。注意这是**先场景内平均、再场景间平均**（macro-average）：无论一个场景的回复多长，它对损失的贡献都相同。

#### 4.2.3 源码精读

- [uniseg3d/cot_criterion.py:L7-L41](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/cot_criterion.py#L7-L41)：`CoTCriterion` 全部实现。核心只有 [L40](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/cot_criterion.py#L40) 一行：`loss = self.loss_weight * torch.mean(torch.stack(language_losses))`，返回 `dict(cot_loss=loss)`。33-39 行是被注释掉的旧版逐 mask 交叉熵，可见该类是从语义分割损失「改名复用」而来。

原料产地（语言损失在哪里产生）：

- [uniseg3d/query_decoder.py:L1420-L1437](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1420-L1437)：训练分支里，`apply_chat_template` 得到完整对话文本，`processor` 编码成 `input_ids`，`preprocess_chatml` 生成 labels 后直接把 `labels` 喂给 `reason_model` 前向；[L1431](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1431) 取 `output.loss`（HuggingFace 对非忽略 token 的平均交叉熵），[L1437](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1437) append 进列表。**每个场景一次 LLM 前向、append 一个 0 维张量**，因此列表长度 = batch 内场景数。

labels 的构造规则：

- [uniseg3d/query_decoder.py:L33-L84](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L33-L84)：`preprocess_chatml` 按 ChatML 分隔符切轮次，把每轮里「assistant 头之前」的全部内容置 `IGNORE_INDEX = -100`（[L30](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L30) 定义），最后一段未完成的生成提示也置忽略（[L81-L82](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L81-L82)）。若全部被忽略则抛 `ValueError`（[L78-L79](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L78-L79)）。

#### 4.2.4 代码实践

**实践目标**：不跑模型，手算一遍 CoTCriterion，确认你理解「列表 → 标量」的聚合方式。

1. 读 [uniseg3d/cot_criterion.py:L40](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/cot_criterion.py#L40)。
2. 假设 batch 有 3 个场景，`language_losses = [2.0, 1.0, 0.5]`，`loss_weight = 1.0`，计算 `cot_loss`。
3. 再算 `loss_weight = 0.25` 时的值，体会权重的作用点。

**需要观察的现象 / 预期结果**：`cot_loss = 1.0 × mean(2.0, 1.0, 0.5) = 1.1667`；权重 0.25 时为 `0.2917`。

**待本地验证**：可选——在有环境的机器上用 `python -c "import torch; print(torch.mean(torch.stack([torch.tensor(x) for x in [2.0,1.0,0.5]])))"` 核对。

#### 4.2.5 小练习与答案

**练习 1**：`language_losses` 列表的长度等于什么？

答案：batch 内场景数。`infer_reason_train` 的主循环逐场景执行一次 LLM 前向并 append 一次（[query_decoder.py:L1343](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1343)、[L1437](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1437)），与每个场景有几条 QA 无关（一个场景的多轮 QA 拼成一次前向）。

**练习 2**：为什么先在场景内对 token 平均、再在场景间平均，而不是把所有场景的 token 拼起来全局平均？

答案：这是 macro-average，让每个场景（每个点云样本）对损失贡献相同；全局平均会让回复特别长的场景主导梯度。实现上正好对应「HF 前向内部平均（`output.loss`）+ `CoTCriterion` 对列表求 mean」两层结构。

**练习 3**：如果某个 batch 的 `qa_data` 是空列表，语言损失会发生什么？

答案：`infer_reason_train` 在 [query_decoder.py:L1336-L1340](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1336-L1340) 提前返回空的 `language_losses`；`Part3dGlammCriterion_v2` 只挡 `None` 不挡空列表，`torch.stack([])` 会抛错。实际训练中 `QA_Generation_v2` 保证每帧至少采样到 QA，不会触发——但这是一个值得知道的潜在边界。

### 4.3 TextPrompt_Criterion_Glamm 分割损失

#### 4.3.1 概念说明

分割损失监督的是解码器输出的**逐超点掩码 logits**：每条 `<SEG>` 查询对应一行 logits，GT 是同一形状的 0/1 掩码。它由两部分组成：

- **BCE**：逐元素的二值交叉熵，监督每个超点「属于/不属于该指称目标」；
- **Dice**：监督整行掩码与 GT 的重叠度，弥补正样本稀少时 BCE 的不平衡问题。

这个类继承自 GLaMM / OneFormer3D 一脉的文本提示实例损失（文件里还保留着 `from turtle import pd` 这样的复制痕迹），但为了适配「QA 驱动的逐行查询」做了大改：**不再需要匈牙利匹配**——因为每条查询与每行 GT 的对应关系由 `<p>` 数量契约天然确定（u4-l4），逐行对齐即可。

它要解决的第三个问题是**无效行过滤**：零向量查询（回答里没有 `<p>`）和全 NaN 的 GT 行必须先剔除，否则 BCE 直接产出 NaN 毁掉整次 backward。

#### 4.3.2 核心流程

```text
对每个场景 b（zip(pred_masks, insts, origin_queries)）：
  none_zero_mask_index = (origin_query.sum(dim=1) != 0)      # 查询非零向量
  none_nan_gt_index    = ~torch.isnan(inst).all(dim=1)       # GT 行非全 NaN
  valida_ids = 两者按位与                                    # 有效行
  若有效行数为 0 → 跳过该场景
  pred_valid = mask[valida_ids]   # (Q_valid, S) logits
  tgt_valid  = inst[valida_ids]   # (Q_valid, S) 0/1
  bce  = BCEWithLogits(pred_valid, tgt_valid)  # mean
  dice = dice_loss(pred_valid, tgt_valid)      # sigmoid 后按行 Dice，再行平均

聚合：
  mask_bce_loss = mean(各有效场景的 bce)
  mask_dice_loss = mean(各有效场景的 dice)
  fix_dice_loss_weight=True → dice × 4        # dice_scale
  fix_mean_loss=True      → 两项 × (场景总数 / 有效场景数)   # 补偿被跳过的场景
  主损失 = loss_weight[0]×bce + loss_weight[1]×dice

辅助深监督：
  对 5 份 aux_outputs，第 i 份权重 1/(i+1)，逐份重复上述流程后累加

返回 {'text_seg_loss': total_weight × 总和}
```

公式化：

\[ \mathcal{L}_{seg} = \underbrace{\big(w_{b}\,\overline{BCE} + w_{d}\,s\,\overline{Dice}\big)}_{\text{末层主预测}} + \sum_{i=1}^{5}\frac{1}{i}\,\mathcal{L}_{aux}^{(i)}, \qquad w_b = w_d = 1,\ s = 4 \]

#### 4.3.3 源码精读

- [uniseg3d/text_prompt_instance_criterion.py:L751-L767](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/text_prompt_instance_criterion.py#L751-L767)：`TextPrompt_Criterion_Glamm.__init__`。`loss_weight` 是 `[bce_weight, dice_weight]` 二元列表；`dice_scale` 默认 4.0；`use_cls_supervise=False` 时 `cls_weight=0`（本配置无分类分支）。

- [uniseg3d/text_prompt_instance_criterion.py:L876-L916](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/text_prompt_instance_criterion.py#L876-L916)：`__call__` 主循环。对每个场景：[L895](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/text_prompt_instance_criterion.py#L895) 以 `origin_query.sum(dim=1) != 0` 找出非零查询行，[L897](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/text_prompt_instance_criterion.py#L897) 以 `~torch.isnan(inst).all(dim=1)` 找出非全 NaN 的 GT 行，[L901](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/text_prompt_instance_criterion.py#L901) 取交集得 `valida_ids`；[L912-L913](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/text_prompt_instance_criterion.py#L912-L913) 对筛选后的行计算 BCE 与 Dice。

  **为什么过滤必须在投影前的 `original_queries` 上做**：`query_proj` 是带偏置的 Linear，零向量投影后不再是零（u4-l4 讲过的坑），所以「零向量识别」只能用投影前的原始隐状态。

- [uniseg3d/text_prompt_instance_criterion.py:L925-L945](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/text_prompt_instance_criterion.py#L925-L945)：聚合与两个缩放开关。有效场景求 `mean`；`fix_dice_loss_weight=True` 时 Dice 乘 `dice_scale`（4.0）——因为 Dice 天然比 BCE 小一个量级，乘 4 平衡两者量级；`fix_mean_loss=True` 时按 `len(pred_masks) / len(mask_bce_losses)`（期望场景数 / 实际有效场景数）放大，补偿被无效行整场景跳过造成的损失缩水。若全部场景无效，回退为 0 张量而非 NaN（[L939-L942](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/text_prompt_instance_criterion.py#L939-L942)），保证训练不崩。

- [uniseg3d/text_prompt_instance_criterion.py:L949-L954](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/text_prompt_instance_criterion.py#L949-L954)：辅助深监督——对 `pred['aux_outputs']` 的第 i 份调用 `get_layer_loss` 并以 `1.0 / (i + 1)` 加权累加，最后返回 `{'text_seg_loss': self.total_weight * loss}`。

- [uniseg3d/text_prompt_instance_criterion.py:L776-L855](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/text_prompt_instance_criterion.py#L776-L855)：`get_layer_loss`，每层 aux 的损失计算，逻辑与主循环一致（同样做零查询/NaN 过滤与两个缩放）。注意它只读 `aux_outputs['masks']`（[L782](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/text_prompt_instance_criterion.py#L782)），aux 字典里的 `scores` 对本损失器完全无用。

关于 `dice_loss` 有一个容易踩的细节——**这个文件里有两个同名定义**：

- [uniseg3d/text_prompt_instance_criterion.py:L734-L748](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/text_prompt_instance_criterion.py#L734-L748)：模块级第二次定义的 `dice_loss`，用 `eps=1e-6` 平滑。Python 按顺序执行模块，**后一个定义会遮蔽前一个**，所以运行时所有调用点（包括 `TextPrompt_Criterion_Glamm`）用的都是这个版本。
- [uniseg3d/text_prompt_instance_criterion.py:L29-L46](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/text_prompt_instance_criterion.py#L29-L46)：被遮蔽的第一个版本，平滑项是 `+1`。读源码时若只搜到这一处会得出错误结论。

另有一个未启用的近亲值得对比：

- [uniseg3d/text_prompt_instance_criterion.py:L962-L1172](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/text_prompt_instance_criterion.py#L962-L1172)：`TextPrompt_Criterion_Glamm_v2`。区别是它不再需要 `origin_queries`（直接检查预测掩码自身是否全 NaN，[L1108](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/text_prompt_instance_criterion.py#L1108)），并额外剔除**全零 GT 行**（[L1112](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/text_prompt_instance_criterion.py#L1112)，`tgt_mask.sum(dim=1) != 0`，即超点多数投票后一个小实例一行全 0 的情况，见 u2-l4）。当前训练配置用的是 v1，全零 GT 行会被保留并监督为「预测空掩码」。

#### 4.3.4 代码实践

**实践目标**：手算一遍完整加权公式，确认你理解 `×4` 与 `1/(i+1)` 两个系数的作用点。

假设某次前向：末层主预测的 `bce = 0.2`、`dice = 0.3`；5 层 aux 各自算出的（bce + dice×4）分别为 `0.9, 0.7, 0.5, 0.3, 0.2`；`fix_mean_loss` 未触发缩放（全部场景有效）；`total_weight = 1.0`。

1. 写出 `text_seg_loss` 的计算式。
2. 代入数值求结果。

**需要观察的现象 / 预期结果**：

主预测项 \(= 0.2 + 4 \times 0.3 = 1.4\)；
aux 项 \(= 1 \times 0.9 + \frac{1}{2} \times 0.7 + \frac{1}{3} \times 0.5 + \frac{1}{4} \times 0.3 + \frac{1}{5} \times 0.2 \approx 1.532\)；
`text_seg_loss` \(\approx 2.932\)。

若没有 `×4`，主预测项只有 0.5——可见 Dice 在未放大时贡献被严重稀释。

#### 4.3.5 小练习与答案

**练习 1**：有效行过滤为什么要「零查询」和「GT 非 NaN」两个条件同时满足？

答案：行是逐索引对齐的——第 k 行查询对应第 k 行 GT。零查询行（回答无 `<p>`，解码器填零向量）没有对应的监督语义；全 NaN GT 行（数据管线取不到有效 mask）没有监督目标。任何一侧无效，这一行的 BCE/Dice 都无意义甚至产生 NaN，所以按位与剔除。

**练习 2**：`fix_mean_loss=True` 什么时候会真正起作用？

答案：当某些场景因 `valida_ids.sum() == 0` 被整体跳过时（例如该场景全部查询是零向量）。此时 `mask_bce_losses` 短于 `pred_masks`，损失被「少平均了一次」，[L932-L938](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/text_prompt_instance_criterion.py#L932-L938) 按 `期望场景数/实际场景数` 放大补偿，使一个 batch 的分割损失量级不随无效场景数波动。

**练习 3**：如果某场景的 GT 恰好整行全 0（小实例被超点多数投票吞掉），v1 会怎么处理？

答案：v1（当前配置）不检查全零 GT，该行保留，模型被监督为对该查询预测全 0 掩码；v2 会把这类行也剔除。这是两个版本的语义差异，不是谁对谁错——v1 相当于教模型「这个小东西在超点粒度上不存在」。

### 4.4 sp_gt_masks 形状追踪：从数据管线到 criterion

#### 4.4.1 概念说明

criterion 的第 2 个形参 `insts` 在调用点叫 `sp_gt_masks`。它既不是 `InstanceData_`（老损失器 `TextPromptInstanceCriterion` 的接口），也不是逐点掩码，而是**逐场景的裸张量列表**，每张量形状 \((Q_b,\ S_b)\)：\(Q_b\) 是该场景所有 QA 的 `<p>` 标签总数（即查询行数），\(S_b\) 是该场景的超点数。

追踪它的形状变化，等于把整条「GT 通道」从头到尾走一遍——这正是上一讲预测通道的对偶面。

#### 4.4.2 核心流程

```text
数据管线（u2-l4）
  每条 QA → sp_gt_seg[i]：(num_p_tags, S_b)，无效行填 NaN
  打包（u2-l5）→ gt_pts_seg.sp_gt_seg：长度为 QA 数的张量列表
        │
        ▼  模型 loss()（u3-l1 / uniseg3d.py）
  sp_gt_masks[b] = torch.cat(sp_gt_seg, dim=0)        # (Q_b, S_b)，QA 顺序拼接
        │
        ▼  criterion
  insts[b]：第 b 个场景的 GT；行 k 对应第 k 条查询（query_list[b][k]）
  有效行 = 行内容非全 NaN（列维度 S_b 与该场景 mask_feats 列数一致）
```

关键契约：**行对齐**。数据侧按 `selected_qa_data` 的顺序逐 QA 生成 GT 行，解码器侧 `get_query_tokens_id` 按同一顺序逐 QA 提取查询行，两侧拼接后第 k 行严格对应——criterion 才敢用同一个 `valida_ids` 同时索引预测和 GT。

#### 4.4.3 源码精读

- [uniseg3d/uniseg3d.py:L1111-L1125](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L1111-L1125)：`loss()` 的 GT 组装段。循环内先把 `sp_pts_mask` 加上 `superpoint_bias` 做全局化（供 scatter_mean 池化用），然后 [L1125](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L1125) `torch.cat(batch_data_samples[i].gt_pts_seg.sp_gt_seg, dim=0)` 把逐 QA 的 GT 沿行维拼成单张量 append 进 `sp_gt_masks`。注意 **GT 不做任何偏移平移**：它的列是场景局部的超点编号，与 `extract_feat` 池化后按 `batch_offsets` 切回逐场景的特征列数一致（承接 u3-l1/u3-l3）。

- [uniseg3d/query_decoder.py:L1871-L1880](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1871-L1880)：预测通道的对偶组装。`infer_reason_train` 返回逐场景 `queries`（每行是一个 `<p>…</p>` 区间的均值池化隐状态，\(Q_b\) 行 × hidden_size 列），[L1879](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1879) 在投影前克隆出 `original_queries`（供损失侧识别零向量行），之后才经 `_get_queries` 做 `query_proj` 降维。

- [uniseg3d/query_decoder.py:L1899-L1910](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1899-L1910)：训练分支的返回字典。`aux_outputs` 由 `pred_masks[:-1]`（初始 head + 前 5 层，共 5 份）与 `pred_scores[:-1]` zip 而成；主 `masks` 取 `pred_masks[-1]`（末层）。掩码行数 = 查询行数 = GT 行数，三方一致。

- [uniseg3d/text_prompt_instance_criterion.py:L879-L890](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/text_prompt_instance_criterion.py#L879-L890)：criterion 侧对 `insts` 的消费方式证明它是裸张量——直接 `inst.to(device)`、`torch.isnan(inst)`，没有任何 `.sp_masks` 属性访问。

#### 4.4.4 代码实践

**实践目标**：为一个假想 batch 手工推出所有张量形状，检验你对契约的掌握。

设定：batch 2 个场景。场景 A 有 2 条 QA，答案 `<p>` 标签数分别为 2 和 1，场景 A 有 1200 个超点；场景 B 有 1 条 QA，`<p>` 数为 2，场景 B 有 800 个超点（本例为示例数据，非真实场景）。

1. 写出 `sp_gt_masks` 列表两个张量的形状。
2. 写出 `original_queries` 列表两个张量的形状（Qwen3-VL-4B 的 hidden_size = 2560）。
3. 写出 `pred['masks']` 列表两个张量的形状。
4. 若场景 B 的那条 QA 回答中没有任何 `<p>`，`original_queries[1]` 与 `sp_gt_masks[1]` 各是什么形状？该场景在损失里的命运如何？

**需要观察的现象 / 预期结果**：

1. `sp_gt_masks[0]`：\((3, 1200)\)（2+1 行），`sp_gt_masks[1]`：\((2, 800)\)。
2. `original_queries[0]`：\((3, 2560)\)，`original_queries[1]`：\((2, 2560)\)。
3. `masks[0]`：\((3, 1200)\)，`masks[1]`：\((2, 800)\)——行数与 GT 完全一致。
4. 数据侧该 QA 不会产生 GT 行（`sp_gt_masks[1]` 行数为 0），解码器侧也不产生查询行（`original_queries[1]` 行数为 0），行对齐仍保持；该场景因 `len(inst) == 0` / `valida_ids.sum() == 0` 被跳过，`fix_mean_loss` 随后把损失放大 \(2/1\) 倍补偿。

**待本地验证**：形状可对照 u2-l4 与 u4-l4 的源码自行复核；真实维度取决于数据。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `sp_pts_mask` 要做 `superpoint_bias` 全局偏移，而 `sp_gt_masks` 不用？

答案：`sp_pts_mask` 是逐点的超点编号，用于整 batch scatter_mean 池化——不同场景的超点编号必须全局唯一才不会混在一起（u3-l3）。`sp_gt_masks` 的列只在该场景内与切回来的逐场景特征对齐，编号是场景局部的，无需平移。

**练习 2**：criterion 收到的 `insts` 与老接口 `TextPromptInstanceCriterion` 期望的 `insts` 有什么类型差异？

答案：老接口期望 `InstanceData_` 对象列表，通过 `.sp_masks` / `.labels_3d` 取 GT 并做匹配；`TextPrompt_Criterion_Glamm` 收的是裸张量列表，直接把张量当 GT 用、逐行对齐不做匹配。

**练习 3**：GT 通道（`data_samples`）与前向通道（`inputs`）在哪里汇合？

答案：在 `loss()` 里——`uniseg3d.py` 的 [L1184](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L1184) 把 decoder 的输出 `x` 与从 `batch_data_samples` 拼出的 `sp_gt_masks` 一起送进 criterion，两条物理隔离的通道（u2-l5）至此汇合。

## 5. 综合实践

**任务**：用最小合成数据独立驱动整个 criterion，验证本讲全部结论——键名、加权、无效行过滤与梯度回传。

下面是示例代码（非项目原有代码，需要先完成 u1-l2 的环境安装，仅用 CPU 即可，不需要 checkpoint 与数据集）：

```python
# 示例代码：save 为 ground3d-lmm-tutorial/tmp_check_criterion.py 后运行
# python ground3d-lmm-tutorial/tmp_check_criterion.py
import torch
from mmdet3d.registry import MODELS
import uniseg3d  # noqa: F401  触发注册（u1-l3 讲过的 import 副作用）

crit = MODELS.build(dict(
    type='Part3dGlammCriterion_v2',
    lang_seg_criterion=dict(
        type='TextPrompt_Criterion_Glamm',
        fix_dice_loss_weight=True,
        fix_mean_loss=True,
        loss_weight=[1.0, 1.0],
        total_weight=1.0),
    lang_criterion=dict(type='CoTCriterion', loss_weight=1.0)))

torch.manual_seed(0)
Q, S, D = 4, 50, 2560          # 每场景 4 行查询、50 个超点、hidden_size 2560

pred = {
    'masks': [torch.randn(Q, S) for _ in range(2)],
    'aux_outputs': [
        dict(masks=[torch.randn(Q, S) for _ in range(2)])
        for _ in range(5)],                     # 5 层辅助深监督
}
insts, origin_queries = [], []
# 场景 0：第 2 行 GT 全 NaN（数据侧无效），第 3 行查询零向量（无 <p>）
gt0 = torch.randint(0, 2, (Q, S)).float(); gt0[2] = float('nan')
oq0 = torch.randn(Q, D); oq0[3] = 0
# 场景 1：全部查询零向量 → 整场景被跳过，触发 fix_mean_loss 的 2 倍补偿
insts.append(gt0); origin_queries.append(oq0)
insts.append(torch.randint(0, 2, (Q, S)).float())
origin_queries.append(torch.zeros(Q, D))

lang = [torch.tensor(1.4, requires_grad=True), torch.tensor(0.9, requires_grad=True)]
loss = crit(pred, insts, lang, original_queries=origin_queries)
print('损失字典：', loss)
print('cot 手算值应为 mean(1.4, 0.9) =', (1.4 + 0.9) / 2)
print('场景 0 有效行数应为 2（NaN GT 行与零查询行各剔除一行）')

total = sum(v for v in loss.values())
total.backward()
print('梯度回传成功，lang[0].grad =', lang[0].grad)
```

操作步骤与观察清单：

1. 运行脚本，确认输出字典只有 `text_seg_loss` 与 `cot_loss` 两个键（对应 4.1 的合并逻辑）。
2. 核对 `cot_loss` 是否等于 1.15（对应 4.2 的聚合公式）。
3. 在 `TextPrompt_Criterion_Glamm.__call__` 的循环里临时加一行 `print(valida_ids)`（看完记得还原，不要提交对源码的修改），确认场景 0 只有前两行有效、场景 1 整体跳过（对应 4.3 的过滤逻辑）。
4. 确认 `total.backward()` 无报错、`lang[0].grad` 非空——语言损失的梯度能穿过组合器流回（实际训练中它流向 LoRA 与解冻的嵌入层）。

预期结果：四个观察点全部符合；若第 3 点打印出 NaN 或报错，说明你构造的无效行没被正确过滤，回头对照 4.3.3 的过滤条件。

待本地验证：本环境未安装运行依赖，以上为设计好的验证步骤与预期，未实际执行。

## 6. 本讲小结

- `Part3dGlammCriterion_v2` 是无参数的纯组装者：把 decoder 输出、`sp_gt_masks`、`language_losses`、`original_queries` 分发给两个子损失器，合并成 `{'text_seg_loss', 'cot_loss'}`；语言与分割的相对权重只能在各子损失器的 `loss_weight` / `total_weight` 上调。
- 语言损失 `CoTCriterion` = 「逐场景 `output.loss`（仅 assistant 回复 token 上的平均交叉熵，由 `preprocess_chatml` 的 `-100` 掩码保证）」再对场景求 macro-average，列表长度 = batch 场景数。
- 分割损失 `TextPrompt_Criterion_Glamm` = 有效行（查询非零向量 ∧ GT 非全 NaN）上的 BCE + Dice，Dice 乘 `dice_scale=4`，`fix_mean_loss` 按期望/有效场景数补偿，5 层辅助深监督按 \(1/i\) 递减加权，无需匈牙利匹配——行对齐由 `<p>` 数量契约天然保证。
- 过滤零查询必须在投影前的 `original_queries` 上做，因为 `query_proj` 带偏置会让零向量投影后非零；文件里有两个同名 `dice_loss`，运行时生效的是后定义的 `eps=1e-6` 版本。
- `sp_gt_masks` 的形状链：逐 QA `sp_gt_seg (num_p_tags, S_b)` → `torch.cat` 成逐场景 \((Q_b, S_b)\)，与查询行、掩码行三方逐索引对齐；GT 不做超点编号全局偏移，因为它工作在场景局部坐标系。

## 7. 下一步学习建议

损失读懂后，训练闭环只剩「怎么把训练跑起来」。下一讲 **u5-l3 训练配置与启动** 将逐项讲解 `configs/ground3dlmm_train.py` 的其余字段——双数据集 `ConcatDataset`、paramwise 差异学习率、`load_from` 主干预训练——以及 `train.sh` / `tools/train.py` 如何把本讲的损失字典变成每步更新的梯度。建议顺带阅读 [unified_criterion.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/unified_criterion.py) 中未启用的 `ScanNetUnifiedCriterion`，了解这套损失体系是从怎样的多任务前辈裁剪而来的。
