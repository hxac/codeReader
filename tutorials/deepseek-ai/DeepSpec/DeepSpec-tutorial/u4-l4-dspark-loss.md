# DSpark 训练损失:CE、L1 蒸馏与置信度监督的组合

## 1. 本讲目标

学完本讲,你应该能够:

1. 写出 DSpark 总损失的完整表达式:三个分项(带位置衰减的交叉熵、与目标分布的 L1 距离、置信度头 BCE)如何被 `ce_loss_alpha`、`l1_loss_alpha`、`confidence_head_alpha` 加权组合。
2. 解释位置衰减权重 \( e^{-t/\gamma} \) 的来源与作用:为什么块内越靠前的槽位在损失里越重要。
3. 推导「L1 距离 = 2 × (1 − 单 token 接受率)」这一关键等式,理解为什么最小化 L1 就是最大化投机解码的接受概率。
4. 说明置信度头的监督目标是什么、为什么要对目标做 `detach`。
5. 解释损失分母为什么要跨 rank `all_reduce`、返回值为什么还要乘 `world_size`。

本讲是 u4-l2(草稿模型结构与 `DSparkForwardOutput` 形状合同)的直接续篇:模型 forward 产出的那份「合同」,消费方正是本讲的 `compute_dspark_loss`。

## 2. 前置知识

### 2.1 硬目标与软目标(蒸馏)

- **硬目标(hard target)**:训练标签是「正确答案的 token id」,损失是交叉熵(CE)。它只告诉模型「正确的是什么」,不告诉它「错得有多离谱」。
- **软目标(soft target)**:标签是一个完整的概率分布(这里是目标模型在同一位置的分布),损失衡量两个分布的距离。这通常叫**知识蒸馏**。软目标携带的信息量远大于 one-hot,对「要模仿目标模型」的草稿模型尤其合适。

DSpark 两个都要:CE 用真值 token 整,蒸馏用目标分布对齐。

### 2.2 比值型损失:分子与分母

u3-l6 讲过 `add_metric` 的 num/den 模式。本讲的损失本身就是同样的结构:

\[ \mathcal{L} = \frac{\sum_{\text{有效位置}} w \cdot \ell(\text{该位置})}{\sum_{\text{有效位置}} w} \]

即「加权的逐位置损失之和」除以「权重之和」。先把分子分母分开存、最后再除,而不是每个位置先除再平均——这与 u2-l1 里提到的「各卡先除再平均在数据倾斜下有偏差」是同一个道理:有效监督位置数在各样本、各 rank 之间都不相等(锚点是随机采样的、块会被截断),只有「先加总、后归一」才能保证每个有效 token 平权。

### 2.3 前缀接受:乘法链

投机解码每轮提交的 token 数,等于「连续被接受的前缀长度 + 1」(u1-l1 讲过兜底 token)。因此第 t 个槽位能发挥作用的前提是它**前面所有**槽位都被接受——概率上是一个连乘(cumprod)。这个「前缀」视角是理解本讲位置衰减和置信度指标的钥匙。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [deepspec/modeling/dspark/loss.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/loss.py) | 本讲主角:全部损失计算与归一化逻辑 |
| [deepspec/modeling/dspark/common.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/common.py) | `DSparkForwardOutput` 形状合同、`build_eval_mask`、置信度头 `AcceptRatePredictor` |
| [deepspec/trainer/dspark_trainer.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/dspark_trainer.py) | 消费侧:`run_batch` 把模型输出与 config 超参喂给损失函数 |
| [deepspec/modeling/dspark/qwen3/modeling.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py) | 生产侧:forward 如何造出 `aligned_target_logits` 与 `confidence_pred` |
| [config/dspark/dspark_qwen3_4b.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/dspark/dspark_qwen3_4b.py) | 默认超参:各 alpha 与 `loss_decay_gamma` 的官方取值 |
| [config/dflash/dflash_qwen3_4b.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/dflash/dflash_qwen3_4b.py) | 对照组:DFlash 如何只靠改配置退化成纯 CE 损失 |
| [deepspec/modeling/dspark/qwen3/config.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/config.py) | `enable_confidence_head` 由 `confidence_head_alpha > 0` 派生 |

## 4. 核心概念与源码讲解

### 4.1 损失总览:三个分项的 alpha 加权组合

#### 4.1.1 概念说明

DSpark 的总损失是三项的加权和:

\[ \mathcal{L} = \alpha_{ce} \cdot \mathcal{L}_{ce} + \alpha_{l1} \cdot \mathcal{L}_{l1} + \alpha_{conf} \cdot \mathcal{L}_{conf} \]

三个分项各管一件事:

| 分项 | 监督信号 | 回答的问题 |
| --- | --- | --- |
| \( \mathcal{L}_{ce} \)(交叉熵) | 真值 token id(u2-l2 的 loss_mask 标出的 assistant 回复) | 「下一句话到底该是什么?」 |
| \( \mathcal{L}_{l1} \)(L1 蒸馏) | 目标模型的完整概率分布 | 「目标模型此刻会怎么想?」 |
| \( \mathcal{L}_{conf} \)(置信度 BCE) | 每个位置「会被验证接受」的概率 | 「我自己知道自己什么时候不靠谱吗?」 |

官方默认权重(`dspark_qwen3_4b.py`)非常耐人寻味:**蒸馏为纲,CE 为辅**——`l1_loss_alpha=0.9`、`ce_loss_alpha=0.1`、`confidence_head_alpha=1.0`。因为草稿模型的 KPI 不是「自己会说话」,而是「分布像目标」(u1-l1:加速比由期望接受长度决定)。

#### 4.1.2 核心流程

`compute_dspark_loss` 的执行分四步:

```text
1. _collect_local_terms      # 本 rank:算三个分子 + 三个分母,顺手打训练指标
2. _all_reduce_loss_denominators  # 跨 rank SUM 三个分母,得到全局分母
3. 本地版损失(分子/本地下母)→ add_metric  # 只用于日志
4. _build_loss               # 分子/全局分母 → alpha 加权和 → × world_size → 返回给 backward
```

调用链上游是 trainer 的 `run_batch`(u3-l1 讲过模板方法模式,损失计算就是这个钩子之一),`loss` 返回给 `BaseTrainer.train()` 去 `backward()`(u3-l2)。

#### 4.1.3 源码精读

先看消费侧。`Qwen3DSparkTrainer.run_batch` 把模型 forward 和损失函数串起来,超参全部来自 config:

> [deepspec/trainer/dspark_trainer.py:25-39](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/dspark_trainer.py#L25-L39)
> `run_batch` 先调 `self.model(...)` 拿到 `DSparkForwardOutput`,再把 `loss_decay_gamma` 与三个 alpha 从 `self.args.model` 透传给 `compute_dspark_loss`,返回标量 `loss`。注意 `target_last_hidden_states` 被显式传给 forward——没有它就没有 `aligned_target_logits`,L1 与置信度两个分项都会缺原料。

官方默认权重在配置文件里:

> [config/dspark/dspark_qwen3_4b.py:22-29](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/dspark/dspark_qwen3_4b.py#L22-L29)
> `confidence_head_alpha=1.0`、`loss_decay_gamma=4.0`、`ce_loss_alpha=0.1`、`l1_loss_alpha=0.9`。同时 `markov_rank=256`、`confidence_head_with_markov=True`——这两个字段会在 qwen3 的 `build_draft_config` 里决定模型是否真的长出对应头。

而 `enable_confidence_head` 不是独立配置,是从 alpha 派生的开关:

> [deepspec/modeling/dspark/qwen3/config.py:22-24](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/config.py#L22-L24)
> `enable_confidence_head = confidence_head_alpha > 0.0`——把「损失权重」和「模型结构」绑在一起:`confidence_head_alpha` 置 0,置信度头根本不会被创建,损失侧也会因为 `confidence_pred is None` 而跳过该项。

对照组 DFlash 证明了这套设计的「配置化」程度:

> [config/dflash/dflash_qwen3_4b.py:18-27](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/dflash/dflash_qwen3_4b.py#L18-L27)
> `markov_rank=0`(无 markov 头)、`confidence_head_alpha=0.0`(无置信度头)、`ce_loss_alpha=1.0`、`l1_loss_alpha=0.0`(纯 CE)。同一份 `Qwen3DSparkTrainer` 与同一个损失函数,只改四个数字,DSpark 就退化成 DFlash——上一讲 u4-l3 讲过 markov 头的开关,本讲补齐损失的开关。

主入口的骨架:

> [deepspec/modeling/dspark/loss.py:255-267](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/loss.py#L255-L267)
> `compute_dspark_loss` 是关键字参数-only 的纯函数(不依赖 trainer 状态):先 `_collect_local_terms` 算本地分子分母,再取 `dist.get_world_size()` 并 all_reduce 分母。

> [deepspec/modeling/dspark/loss.py:320-329](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/loss.py#L320-L329)
> 真正返回给 `backward()` 的 `backward_loss` 由 `_build_loss` 构造——注意返回的只有这一个标量,日志用的本地版损失已经在上面通过 `add_metric` 单独发出去了。

最终组合在 `_build_loss`:

> [deepspec/modeling/dspark/loss.py:237-252](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/loss.py#L237-L252)
> 每个分项 = 本地分子 / (全局分母 + 1e-6);`1e-6` 兜底全空 batch(比如 dummy 锚点全被 mask 掉时分母为 0);L1 与置信度项在原料缺失时置 0 而不是报错。最后 `* world_size`——这个乘法是 4.5 节的主角。

#### 4.1.4 代码实践

**实践目标**:确认「改 alpha 就能开关分项」,并观察 DSpark 与 DFlash 的损失配置差异。

1. 打开 `config/dspark/dspark_qwen3_4b.py` 与 `config/dflash/dflash_qwen3_4b.py`,逐字段 diff `model` 字典。
2. 回答:如果想在 DSpark 基础上做消融实验「去掉位置衰减」,应该覆盖哪个键?(答案:`model.loss_decay_gamma`,置 0 或 None 即可,见 4.2.3 的判断逻辑。)
3. 用 u1-l4 学过的 `--opts` 语法写出等价命令行:`--opts "model.ce_loss_alpha=1.0" "model.l1_loss_alpha=0.0"`。

**需要观察的现象 / 预期结果**:两份配置只差 4 个键(`markov_rank`、`markov_head_type`、`confidence_head_alpha`、`confidence_head_with_markov` 是否存在,以及两个 loss alpha);`train` 与 `data` 字典完全相同。此实践为纯阅读型,不涉及运行。

#### 4.1.5 小练习与答案

**练习 1**:为什么 `confidence_head_alpha` 既能控制损失权重,又能控制模型是否创建置信度头?

**答案**:qwen3 的 `build_draft_config` 把 `enable_confidence_head` 派生为 `confidence_head_alpha > 0.0`([deepspec/modeling/dspark/qwen3/config.py:22-24](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/config.py#L22-L24)),模型据此决定是否实例化 `AcceptRatePredictor`;损失侧则用 `outputs.confidence_pred is not None` 判断。一个配置键同时驱动结构与损失,保证两者不会出现「有头无监督」或「无头有监督」的失配。

**练习 2**:`_build_loss` 里为什么给分母加 `1e-6` 而不是直接除?

**答案**:DSpark 允许 dummy 锚点存在(u4-l1:凑不满有效锚点时以 dummy 块兜底),极端 batch 里所有位置都可能被 `eval_mask` 屏蔽,权重和为 0;直接除会产生 NaN 污染梯度。加 `1e-6` 后分项退化为 0/1e-6 ≈ 0,安全。

### 4.2 CE 损失与位置衰减:前面的槽位更重要

#### 4.2.1 概念说明

DSpark 一次前向产出 \( B \times A \times T \) 个监督位置(batch × 锚点数 × 块长,u4-l2 讲过 512×7 的规模)。但这些位置**不平等**:

- 推理时接受是**前缀式**的:第 t 个草稿 token 想被接受,先得让前 t−1 个都被接受(概率连乘)。优化好槽位 0 的收益,会被后面所有槽位反复复用;优化好槽位 6 的收益,只有前 5 个全对时才兑现。
- 块内越靠后,token 级自回归信息越少:DSpark 块内并行生成,token 间依赖只靠 markov 头的一阶近似(u4-l3),后面槽位的可优化空间天然更小、噪声更大。

所以损失权重随槽位 t 指数衰减:

\[ w_t = \mathbb{1}[\text{eval}] \cdot e^{-t/\gamma} \]

其中 \( \gamma \) 是 `loss_decay_gamma`(默认 4.0),t 从 0(紧跟锚点的第一个预测位)开始。权重不是硬截断,而是「越远越轻」的软聚焦。

带衰减的 CE 为:

\[ \mathcal{L}_{ce} = \frac{\sum_{b,a,t} w_{b,a,t} \cdot \mathrm{CE}(z_{b,a,t},\, y_{b,a,t})}{\sum_{b,a,t} w_{b,a,t}} \]

#### 4.2.2 核心流程

```text
eval_mask [B,A,T] (bool)
    │ 转 float32
    ▼
× decay_weights [1,1,T]  其中 decay_weights[t] = exp(-t/γ)   (γ>0 时)
    │
    ▼
loss_weight_mask [B,A,T]
    ├── CE 路径: flatten 后 F.cross_entropy(reduction="none") 逐位置损失 × 权重
    ├── L1 路径(4.3 节): 复用同一个 loss_weight_mask
    └── 置信度路径(4.4 节): 也复用同一个 loss_weight_mask
```

注意三个分项**共享**同一份权重掩码——衰减对整个损失体系生效,而不仅是 CE。

#### 4.2.3 源码精读

> [deepspec/modeling/dspark/loss.py:25-37](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/loss.py#L25-L37)
> `_build_loss_weight_mask`:`eval_mask` 转 float 后,若 `loss_decay_gamma` 非 None 且 > 0,构造 `positions = arange(block_size)` 并乘上 `exp(-positions/γ)`。γ=4、T=7 时的权重见下表。γ 传 None 或 0 则完全不衰减(等权重)。

γ=4.0、block_size=7 时的衰减权重(可直接手算验证):

| t | 0 | 1 | 2 | 3 | 4 | 5 | 6 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| \( e^{-t/4} \) | 1.000 | 0.779 | 0.607 | 0.472 | 0.368 | 0.287 | 0.223 |

槽位 0 的权重约是槽位 6 的 4.5 倍。

> [deepspec/modeling/dspark/loss.py:109-114](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/loss.py#L109-L114)
> CE 的计算:把 `[B,A,T,V]` 的 logits 与 `[B,A,T]` 的目标 id 全部 flatten 成二维/一维,`F.cross_entropy(reduction="none")` 得到逐位置的标量损失,再乘 flatten 后的权重求和。`reduction="none"` 是关键——要先加权,就不能让 PyTorch 提前帮我们求平均。

`eval_mask` 本身从哪来?u4-l1/u4-l2 讲过它在模型 forward 里由 `build_eval_mask` 生成:

> [deepspec/modeling/dspark/common.py:172-188](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/common.py#L172-L188)
> `build_eval_mask`:目标位需在序列内且落在 loss_mask 监督区,且属于被保留的块;最后 `.cumprod(dim=-1)` 保证监督只发生在「连续有效前缀」——第一个无效位之后整块作废。这与 2.3 节的前缀接受语义一一对应:训练时的监督掩码和推理时的接受方式用的是同一套「前缀」世界观。

还有一处容易忽略的细节:进入损失的 `draft_logits` 已经包含 markov 头的修正(u4-l3):

> [deepspec/modeling/dspark/qwen3/modeling.py:482-493](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py#L482-L493)
> 冻结 `lm_head` 先把草稿隐状态投成 logits,再经 `markov_head.apply_block_logits` 加上基于前一 token 的修正。也就是说,CE 与 L1 监督的都是「最终用于推理采样的那份 logits」,训练目标与推理行为严格对齐。

#### 4.2.4 代码实践

**实践目标**:验证位置衰减权重公式,并确认被 mask 位置对 CE 无贡献。

1. 在 Python 里手工复现权重:`torch.exp(-torch.arange(7) / 4.0)`,与上表对照。
2. 构造一个玩具样本:`eval_mask` 只在 t=2..5 为 1,γ=4,手算 `loss_weight_mask`;再算「权重和」——这就是 CE 的分母。
3. 把 t=6 位置的 `target_ids` 改成任意其他 token id,重算损失(用第 5 节综合实践的脚本骨架),观察 CE 是否变化。**预期结果**:不变——该位置被 `eval_mask` 屏蔽,既不进分子也不进分母。此数值实验待本地验证。

#### 4.2.5 小练习与答案

**练习 1**:把 `loss_decay_gamma` 从 4.0 调大到 40.0,损失行为趋向什么?调到极小(如 0.1)呢?

**答案**:γ→∞ 时 \( e^{-t/\gamma} \to 1 \),退化为等权重(与传 None 等价);γ→0⁺ 时权重集中在 t=0,基本只训练「锚点的下一个 token」,后面槽位几乎没有梯度,块式提议的多 token 收益会丧失。γ 是「关注的槽位深度」旋钮:γ 约等于有效监督的 e 折叠深度。

**练习 2**:为什么衰减权重挂的是块内槽位 t,而不是序列绝对位置?

**答案**:因为接受概率的连乘发生在「一块之内」——每轮验证从块首开始,块间互相独立(每轮重新提议)。决定某个草稿 token 能否被用的是它在块内的深度,与它在原序列里的绝对位置无关。

**练习 3**:`eval_mask` 已经是「前缀连续」的了(cumprod),那 `loss_weight_mask` 里的衰减是不是冗余?

**答案**:不冗余。cumprod 解决的是「0/1 的有效性」(哪里完全不该监督),衰减解决的是「有效位置内部的相对重要性」(哪里更值得学好)。前者是硬边界,后者是软偏好,两者正交。

### 4.3 L1 概率距离蒸馏:最小化 1 − accept rate

#### 4.3.1 概念说明

L1 分项的原料是两份 logits:

- `draft_logits`:草稿模型(含 markov 修正)在各监督位置的分布 \( q \);
- `aligned_target_logits`:目标模型在**同样位置**的分布 \( p \)(用 u2-l4 缓存里的 `target_last_hidden_states` 过冻结 lm_head 得到)。

损失是逐位置的 L1 距离,同样按 `loss_weight_mask` 加权归一:

\[ \mathcal{L}_{l1} = \frac{\sum w_{b,a,t} \cdot \| p_{b,a,t} - q_{b,a,t} \|_1}{\sum w_{b,a,t}} \]

它为什么叫「1 − accept rate」?先看接受率的定义。代码里:

\[ a_{b,a,t} = \mathrm{clamp}\bigl(1 - \tfrac{1}{2}\| p - q \|_1,\ 0,\ 1\bigr) \]

这正好是统计学的**总变差距离**的补:\( \mathrm{TV}(p,q) = \frac{1}{2}\sum_x |p(x)-q(x)| \),故 \( a = 1 - \mathrm{TV}(p,q) \)。

#### 4.3.2 核心流程

一个关键的数学事实(单 token 拒绝采样,u6-l3 会再次遇到它):

\[ \mathbb{E}_{x \sim q}\Bigl[\min\bigl(1, \tfrac{p(x)}{q(x)}\bigr)\Bigr] = \sum_x \min\bigl(q(x), p(x)\bigr) = 1 - \mathrm{TV}(p, q) = a \]

推导分三步:\( q(x)\min(1, p(x)/q(x)) = \min(q(x), p(x)) \);\( \sum_x \min(q,p) = 1 - \sum_x (q-p)^+ \)(因为两个分布各自和为 1,min 之和 = 1 − 只在一方超出另一方的部分);而 \( \sum_x (q-p)^+ = \mathrm{TV} \)。

含义:**「从草稿分布采一个 token,按标准投机解码验证被接受」的概率,恰好等于 1 − TV 距离。** 于是

\[ \mathcal{L}_{l1} = 2\,(1 - a) \]

最小化 L1 距离 = 最大化每个位置的期望接受概率。训练损失直接对齐推理 KPI,这就是 `l1_loss_alpha=0.9` 占主导的原因——它优化的是「分布形状」,而 CE(0.1)只优化「正确答案上的概率质量」,一个只顾 argmax 对、分布其余部分乱七八糟的草稿模型,CE 可以很低但接受率很差。

#### 4.3.3 源码精读

> [deepspec/modeling/dspark/loss.py:60-70](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/loss.py#L60-L70)
> `_compute_accept_rate_3d`:两份 logits 分别 `softmax`(**显式 `.float()`**——bf16 下 softmax 的指数运算精度不够,蒸馏类计算一律升 fp32),逐元素差的绝对值沿词表维求和,乘 0.5 取补,再 `clamp_(0,1)` 防数值越界。没有 `aligned_target_logits` 时返回 None,下游全部优雅降级。

> [deepspec/modeling/dspark/loss.py:73-87](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/loss.py#L73-L87)
> `_compute_local_l1_term`:同样的 L1 距离,乘 4.2 节的 `loss_weight_mask` 得分子,权重和为分母。注意它与 `_compute_accept_rate_3d` 的关系就是差一个 0.5 的系数和一次 clamp——同一份距离的两种用途:进损失(带梯度)和当接受率(4.4 节当置信度的目标)。

目标分布的生产侧在模型 forward 里(u4-l2 讲过「位置 i−1 预测 token i」的错位对齐,这里看它落到损失原料上):

> [deepspec/modeling/dspark/qwen3/modeling.py:447-465](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py#L447-L465)
> 当 `target_last_hidden_states` 存在时,在 `target_pred_indices = safe_label_indices - 1` 处 gather 目标模型的最终隐状态,过 `compute_logits`(即冻结的 `lm_head`)得到 `aligned_target_logits`。草稿与目标共用同一个 lm_head,所以两份 logits 的尺度天然可比——蒸馏比较的是「同一个读出层、不同输入隐状态」的输出,这是 u3-l1 里冻结 embed/lm_head 策略在损失侧的红利。

> [deepspec/modeling/dspark/loss.py:121-123](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/loss.py#L121-L123)
> 防御性断言:`l1_loss_alpha > 0` 却没有 `aligned_target_logits` 时立刻失败,而不是静默给出 0 损失——配置错误要在第一步就炸出来。

#### 4.3.4 代码实践

**实践目标**:验证恒等式 \( \mathcal{L}_{l1} = 2(1-a) \) 与「分布完全一致时 L1 为 0」。

1. 取两组随机分布 p、q(可用 `torch.softmax(torch.randn(2, 5), -1)`),手算 `0.5 * (p - q).abs().sum(-1)`,与 `_compute_accept_rate_3d` 的输出相加,验证和恒为 1。
2. 令 `aligned_target_logits = draft_logits.clone().requires_grad_(False)`,用第 5 节脚本以 `l1_loss_alpha=1.0` 重算——**预期结果**:L1 分量为 0(两分布相同,TV=0,接受率=1)。
3. 把 `draft_logits` 乘 5(更尖锐),观察 L1 变化方向。待本地验证。

#### 4.3.5 小练习与答案

**练习 1**:为什么用 L1 而不是 KL 散度做蒸馏?

**答案**:(源码未附理由,以下为分析。)KL \( \sum q \log(q/p) \) 对「q 有质量而 p 几乎没有」的位置惩罚无界,训练容易被个别极端 token 主导;L1 每个 token 的贡献以 |p−q| 为界,梯度更平稳。更本质的是 L1 与接受率有精确等式关系(\( a = 1 - \mathrm{TV} \)),KL 没有这种与拒绝采样的直接对应——损失值本身就能解读为「接受率损失了多少」。

**练习 2**:`accept_rate_3d` 会被 clamp 到 [0,1],什么时候真的会越界需要 clamp?

**答案**:数学上 \( 0 \le \mathrm{TV} \le 1 \) 恒成立,纯数学不会越界;但 float 运算的舍入可能让它落出区间一丁点(如 1+1e-7 或 −1e-8),clamp 是数值卫生而非模型假设。注意 clamp 用的是原地 `clamp_`,发生在无梯度的目标构造路径之外,对损失的梯度传播无影响(L1 分项用的是未 clamp 的原始距离)。

**练习 3**:DFlash 把 `l1_loss_alpha` 置 0,但训练数据里 `target_last_hidden_states` 仍然存在。`_compute_accept_rate_3d` 还会被调用吗?有什么用?

**答案**:会。`_collect_local_terms` 无条件调用它([deepspec/modeling/dspark/loss.py:116-119](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/loss.py#L116-L119)),只要 `aligned_target_logits` 非 None。它不进损失,但喂给了 `accept_rate@t`、`tau_probabilistic` 等训练指标(4.4 节)——即使纯 CE 训练,也能在 TensorBoard 里监控接受率走势。

### 4.4 accept rate 与置信度损失:让草稿知道自己几斤几两

#### 4.4.1 概念说明

置信度头(`AcceptRatePredictor`,一个 `Linear(input_dim, 1)`)预测每个监督位置的**接受概率** \( a_{b,a,t} \)。它的价值在推理期:DSpark 评估器可以用置信度阈值提前截断低置信提议、少浪费验证算力(u6-l5 专讲校准与阈值)。但头本身要在训练期学会「诚实」——本讲的 BCE 损失就是它的老师。

目标 \( a \) 有一个微妙之处:它是**当前草稿分布**的函数(\( a = 1 - \mathrm{TV}(p, q_\theta) \)),不是固定标签。若不切断梯度,BCE 的梯度会流进 `draft_logits`,相当于让「预测者」去搬动「被预测的对象」,目标随预测漂移。所以代码对目标做了 `detach`。

#### 4.4.2 核心流程

```text
confidence_pred [B,A,T] (logit, fp32)          accept_rate_3d [B,A,T] (detach, 无梯度)
        │                                              │
        └────────── F.binary_cross_entropy_with_logits ──┘
                          (reduction="none")
                                   │
                          × loss_weight_mask   ← 与 CE/L1 同一份带衰减权重
                                   │
                      num = 加权和,den = 权重和  →  进 alpha 组合
```

同一函数里还顺带产出**训练期的接受率仪表盘**(全部 `no_grad`):

- `accept_rate@t`:每个槽位 t 的平均接受率(分子分母按位置拆开,经 u3-l6 的 ratio 聚合跨 rank 加权);
- `tau_probabilistic`:期望接受长度 \( \tau \)——训练时就能看到「若现在拿去投机解码,平均每轮能提交几个 token」的解析估计,不必跑评估。

其中期望接受长度(u1-l1 的「最差也提交 1 个兜底 token」在这里落地为 +1):

\[ \tau = 1 + \sum_{t=0}^{T-1} \prod_{s=0}^{t} a_s \]

#### 4.4.3 源码精读

> [deepspec/modeling/dspark/loss.py:146-163](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/loss.py#L146-L163)
> 置信度损失段:`confidence_targets = accept_rate_3d.detach()` 切断目标侧梯度;`F.binary_cross_entropy_with_logits` 用 logit 而非 sigmoid 概率入参(数值稳定的标准做法);逐位置损失乘 `loss_weight_mask`——注意置信度头**同样吃位置衰减**,前面槽位的校准被优先保证,与它们在推理期的前缀地位一致。

> [deepspec/modeling/dspark/loss.py:164-181](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/loss.py#L164-L181)
> 三个只做记录的诊断量(不进损失):`confidence_abs_error`(预测概率与真接受率的平均绝对误差)、`confidence_bias`(带符号偏差,看系统性高估/低估)、`confidence_cumprod_bias`(预测置信度的**连乘**与真接受率连乘的差——正是期望接受长度意义上的校准误差,是 u6-l5 可靠性图的训练期前身)。

> [deepspec/modeling/dspark/loss.py:40-57](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/loss.py#L40-L57)
> `_compute_local_probabilistic_stats`:用 `eval_mask`(注意:是原始的 0/1 掩码,**不带**衰减)筛出有效位置,`cumprod(dim=-1).sum(dim=-1)` 得每块的接受长度期望,`+1.0` 补上兜底 token;再按「有效块」加权求和。`tau_probabilistic` 的分母是有效块数,所以它就是每块平均期望接受长度。

> [deepspec/modeling/dspark/loss.py:192-204](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/loss.py#L192-L204)
> 指标发射:循环每个槽位发 `accept_rate@t`,再发 `tau_probabilistic`。分母分别是该槽位的有效位置数与有效块数——又是 u3-l6 的 num/den ratio 模式,flush 时自动跨 rank 全局加权。

置信度头本体的定义与输入特征:

> [deepspec/modeling/dspark/common.py:43-49](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/common.py#L43-L49)
> `AcceptRatePredictor` 只有一个 `nn.Linear(input_dim, 1)`——预测「会不会被接受」本质是丰富的隐状态上一个近似线性的读出。

> [deepspec/modeling/dspark/qwen3/modeling.py:504-516](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py#L504-L516)
> `confidence_pred` 的构造:默认吃草稿隐状态;若 `confidence_head_with_markov=True`(官方默认),则把 markov 嵌入(u4-l3:同一份前驱 token 嵌入)拼接进来——因为接受与否同样依赖前一 token 的取值。输出 `.float()` 保证 BCE 在 fp32 里算。

#### 4.4.4 代码实践

**实践目标**:理解 \( \tau \) 公式与 cumprod 的对应关系。

1. 手工构造接受率向量 a = [0.9, 0.8, 0.7, 0.6],计算 cumprod = [0.9, 0.72, 0.504, 0.3024],求和 2.4264,加 1 得 τ = 3.4264。
2. 用 `torch.tensor([0.9, 0.8, 0.7, 0.6]).cumprod(-1).sum() + 1` 复算验证。
3. 回答:若只把 a[0] 从 0.9 提到 0.95,τ 变多少?若把 a[3] 从 0.6 提到 0.65 呢?算完后用一句话解释 4.2 节位置衰减的合理性。**预期结果**:前者提升约 0.06(每个后续槽位的连乘都变大),后者只提升 0.05×0.504 ≈ 0.025——同样幅度的单点改进,越靠前杠杆越大。

#### 4.4.5 小练习与答案

**练习 1**:为什么置信度目标要用 `detach`,而 L1 蒸馏里的 `target_probs` 不需要?

**答案**:蒸馏的语义就是「让 draft 分布向 target 分布靠」,梯度本该流向 draft——target 是老师,来自冻结 lm_head 与不训练的目标缓存,天然无梯度。置信度头则不同:它的目标是 \( 1 - \mathrm{TV}(p, q_\theta) \),是**学生自己分布**的函数;不 detach 的话,BCE 会同时驱动草稿分布去「变得容易被自己预测」,引入自我指涉的目标漂移,破坏蒸馏主目标。

**练习 2**:`tau_probabilistic` 与评估侧的 `accept_len`(u6 将讲)有什么区别?

**答案**:`tau_probabilistic` 是训练 batch 上的解析期望(由 TV 距离算出,无需真的采样验证),开销为零但只在教师强制语境下成立;`accept_len` 是真实投机解码采样的统计量。前者是训练期便宜的代理指标,后者是部署口径的真值。两者走势应当一致,若背离说明过拟合或分布偏移。

**练习 3**:`confidence_cumprod_bias` 为什么比 `confidence_bias` 更贴近推理收益?

**答案**:推理期决定加速比的是**前缀接受长度**——连乘之后的结果。单点概率偏差 0.02,经过 7 连乘可能放大或湮灭;`cumprod_bias` 直接度量「预测的期望接受长度与真实期望接受长度之差」,与阈值早停决策(u6-l5)所依赖的量同构。

### 4.5 跨 rank all_reduce 分母与乘 world_size

#### 4.5.1 概念说明

DSpark 的有效监督位置数在不同 rank、不同 micro-batch 之间**天然不均**:锚点是随机采样的(forward 里现场采样,u4-l2),序列长短不一,块会被截断。若每个 rank 用自己的本地分母归一化再各退各的梯度,等价于隐式地给「监督位置少的 rank」更大的权重——改变了「全局 token 平权」的语义,梯度尺度还会随数据抖动。

代码的解法:**分子留本地,分母取全局**。

#### 4.5.2 核心流程与数学

设共 W 个 rank,三分项 i ∈ {ce, l1, conf},rank r 的分子为 \( n_i^r \)、分母为 \( d_i^r \)。目标是让 backward 后的梯度等于「全局批损失」的梯度:

\[ \mathcal{L}^\star = \sum_i \alpha_i \cdot \frac{\sum_r n_i^r}{\sum_r d_i^r} \]

代码实际做的事(见下引源码):

1. `all_reduce`(SUM)三个分母,每个 rank 都拿到 \( D_i = \sum_r d_i^r \);
2. 本地构造 \( \mathcal{L}_r = W \cdot \sum_i \alpha_i \frac{n_i^r}{D_i} \);
3. `loss.backward()` 后,FSDP 对梯度做归约——与 DDP 语义一致,结果是各 rank 梯度的**平均**(求和后除以 W)。

于是最终梯度:

\[ \frac{1}{W} \sum_r \frac{\partial \mathcal{L}_r}{\partial \theta} = \frac{\partial}{\partial \theta} \sum_i \alpha_i \frac{\sum_r n_i^r}{D_i} = \frac{\partial \mathcal{L}^\star}{\partial \theta} \]

\( W \) 被 FSDP 的平均消掉,不多不少正好落在全局比值上。**乘 `world_size` 不是放大学习率,而是预先抵消即将发生的梯度平均。**

附带收益:`D_i` 对所有 rank 相同且在 backward 前就已固定,把它视作常数,梯度表达式干净;而日志走的是另一条本地版路径(`local_ce_loss` 等用本地分母),经 `add_metric` 的 ratio 聚合(u3-l6:`flush` 时 `sum(num)/sum(den)` 跨 rank 加权)在日志窗口上重新组合成全局口径——**训练用的损失和日志用的损失殊途同归,但实现路径不同**。

#### 4.5.3 源码精读

> [deepspec/modeling/dspark/loss.py:11-22](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/loss.py#L11-L22)
> `_all_reduce_loss_denominators`:对三个分母 `detach().clone()` 后做 SUM all_reduce(`detach`+`clone` 确保分母不进计算图、也不被原地归约污染);`world_size == 1` 时跳过通信,单卡零开销。

> [deepspec/modeling/dspark/loss.py:268-272](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/loss.py#L268-L272)
> 主入口里 `world_size = dist.get_world_size()` 后立刻 all_reduce 分母——这意味着 `compute_dspark_loss` **要求进程组已初始化**(即使单卡也要 `init_process_group`,见综合实践)。

> [deepspec/modeling/dspark/loss.py:277-292](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/loss.py#L277-L292)
> 本地版损失:分子除以**本地**分母,只用于 `add_metric("loss", ..., reduction="mean")` 打日志,从不返回。它与 backward 版的差就是 4.5.2 里的归一化口径差。

> [deepspec/modeling/dspark/loss.py:237-252](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/loss.py#L237-L252)
> `_build_loss` 的返回行 `(α_ce·ce + α_l1·l1 + α_conf·conf) * world_size`——本讲的最后一块拼图。L1 与置信度项还有 `den > 0` 的开关判断,保证原料缺失时安全置 0。

与 u3-l2 的连接:这个 `* world_size` 与主循环里 `loss / G`(梯度累积时先除以累积步数)是同一类「账目平衡」操作——一切为了「无论 W、G 取多少,等效梯度都严格等于全局大批单步的梯度」。

#### 4.5.4 代码实践

**实践目标**:用数学验证代替昂贵的多卡实验。

1. 推导检验:假设 2 个 rank,rank0 的 ce 分子=10、分母=5,rank1 分子=30、分母=15。写出 `_build_loss` 在两个 rank 上各自返回的 ce 部分(不含 alpha),以及「理想全局损失」。**预期结果**:两 rank 都返回 \( 2 \times 40/20 = 4 \);理想全局值 \( 40/20 = 4 \times \frac{1}{2} \)——FSDP 平均后正好回到 4。
2. (可选,需两卡)跑同一份数据两次:单卡一次、双卡各持一半一次,比较若干步后的梯度范数。**预期结果**:二者应基本一致(数值误差内),这正是该设计的验收标准。待本地验证。

#### 4.5.5 小练习与答案

**练习 1**:如果去掉 `* world_size`(其余不变),实际有效学习率会怎么变?

**答案**:FSDP 平均后梯度变为理想值的 \( \frac{1}{W} \),等效于学习率缩水 W 倍——8 卡训练 lr=6e-4 实际只相当于 7.5e-5,且换卡数就得重调 lr,实验不可迁移。

**练习 2**:分母 all_reduce 用 SUM 而不是平均值,配合分子留本地,为什么恰好等价于「全局 token 平权」?

**答案**:梯度里每个有效 token 的贡献系数是 \( \alpha_i / D_i = \alpha_i / \sum_r d_i^r \)——对所有 rank 的所有有效 token 一视同仁的常数。若改用本地分母,系数变为 \( \alpha_i / d_i^r \),监督位置少的 rank 系数大,token 不再平权。

**练习 3**:为什么不干脆在每个 rank 上算「完整全局损失」(把分子也 all_reduce)?

**答案**:分子 \( n_i^r \) 挂着计算图,all_reduce 一个带梯度的张量再 backward 技术上可行但通信两倍(前向归约一次、反向还要归约梯度);留本地分子、只归约 detachment 过的分母,反向时 FSDP 的既有梯度归约顺路完成汇总,零额外成本。这是「一次通信,两头复用」的典型工程权衡。

## 5. 综合实践

**任务**:构造一个形状为 (1, 2, 4, V) 的玩具 `DSparkForwardOutput`,分别单独开启 `ce_loss_alpha` 与 `l1_loss_alpha`,验证各分量的量级、mask 生效与位置衰减,把本讲四个知识点串成一次实验。

前置说明:仓库没有单元测试覆盖 `compute_dspark_loss`(全仓库 grep 仅 `dspark_trainer.py` 一处调用),所以这是一份自写的「源码阅读型 + 运行型」实践。以下为**示例代码**(非仓库原有),可在仓库根目录保存为临时脚本运行,不要提交:

```python
# toy_dspark_loss.py(示例代码)
import torch
import torch.distributed as dist

from deepspec.modeling.dspark.common import DSparkForwardOutput
from deepspec.modeling.dspark.loss import compute_dspark_loss

# 1) compute_dspark_loss 内部调用 dist.get_world_size() 与 all_reduce,
#    单进程也必须先初始化进程组(gloo 后端,CPU 即可,无需 GPU)。
dist.init_process_group(backend="gloo", rank=0, world_size=1)

torch.manual_seed(42)
B, A, T, V = 1, 2, 4, 11   # batch=1, 锚点=2, 块长=4, 词表=11(规格中的玩具形状)

draft_logits = torch.randn(B, A, T, V, requires_grad=True)
target_ids = torch.randint(0, V, (B, A, T))
aligned_target_logits = torch.randn(B, A, T, V)

eval_mask = torch.zeros(B, A, T, dtype=torch.bool)
eval_mask[0, 0, :] = True        # 块 0:四个槽位全监督
eval_mask[0, 1, 0] = True        # 块 1:只有槽位 0 监督(前缀语义)
block_keep_mask = eval_mask.any(dim=-1)

outputs = DSparkForwardOutput(
    draft_logits=draft_logits,
    target_ids=target_ids,
    eval_mask=eval_mask,
    block_keep_mask=block_keep_mask,
    confidence_pred=None,          # 玩具局:不开置信度头
    aligned_target_logits=aligned_target_logits,
)

def run(gamma, ce, l1):
    loss = compute_dspark_loss(
        outputs=outputs,
        loss_decay_gamma=gamma,
        ce_loss_alpha=ce,
        l1_loss_alpha=l1,
        confidence_head_alpha=0.0,
    )
    return loss

# 2) 单独开 CE
loss_ce = run(gamma=None, ce=1.0, l1=0.0)
print("CE-only loss:", float(loss_ce))

# 3) 单独开 L1
loss_l1 = run(gamma=None, ce=0.0, l1=1.0)
print("L1-only loss:", float(loss_l1))

# 4) 对照:手工复算 L1(验证 4.3 的公式)
p = torch.softmax(aligned_target_logits.float(), dim=-1)
q = torch.softmax(draft_logits.detach().float(), dim=-1)
manual_l1 = (p - q).abs().sum(-1)[eval_mask].mean()
print("manual L1:", float(manual_l1))    # 应与 loss_l1 一致(gamma=None 时权重全 1)

# 5) 位置衰减生效:同一份数据,gamma=4.0 时损失应改变
loss_ce_decay = run(gamma=4.0, ce=1.0, l1=0.0)
print("CE-only loss (gamma=4):", float(loss_ce_decay))

# 6) mask 生效:被屏蔽位置的真值改动不应影响损失
outputs.target_ids = target_ids.clone()
outputs.target_ids[0, 1, 1:] = (outputs.target_ids[0, 1, 1:] + 3) % V
print("CE loss after mutating masked targets:", float(run(gamma=None, ce=1.0, l1=0.0)))

# 7) 梯度不泄漏:被屏蔽位置不应收到梯度
loss = run(gamma=None, ce=1.0, l1=0.0)
loss.backward()
grad = draft_logits.grad.reshape(B, A, T, V)
print("grad nonzero positions:", grad.abs().sum(-1) > 0)
```

**操作步骤**:

1. `python -m pip install -r requirements.txt` 后运行 `python toy_dspark_loss.py`(CPU 即可)。
2. 逐项核对打印结果。

**需要观察的现象与预期结果**:

- 第 3、4 步:`L1-only loss` 与 `manual L1` 相等——验证「分子分母归一 + 无衰减等权」的实现与公式一致。
- 第 5 步:`gamma=4.0` 的 CE 与 `gamma=None` 不同——位置衰减确实改变了加权。
- 第 6 步:改动被屏蔽位置的 `target_ids` 后 CE 不变——mask 同时挡住分子与分母。
- 第 7 步:`grad nonzero positions` 恰好与 `eval_mask` 图案一致(块 1 的槽位 1..3 无梯度)。
- 第 2 步 CE 的具体数值取决于随机种子,量级约在 \( \ln V \approx 2.4 \) 附近(随机 logits 对 11 类的期望交叉熵)。

以上运行结论为基于源码逻辑的推演,**待本地验证**。验证通过后可删除临时脚本。

## 6. 本讲小结

- DSpark 总损失 = `ce_loss_alpha`×(带位置衰减的 CE)+ `l1_loss_alpha`×(L1 蒸馏)+ `confidence_head_alpha`×(置信度 BCE);官方默认 0.1 / 0.9 / 1.0,蒸馏为纲;DFlash 仅靠把权重改成 1.0 / 0.0 / 0.0 就退化成纯 CE。
- 位置衰减 \( e^{-t/\gamma} \)(γ=4)让块首槽位权重约为块尾的 4.5 倍——前缀接受是连乘,越靠前的修正杠杆越大;三个分项共享同一份 `loss_weight_mask`。
- L1 距离与单 token 接受率是一枚硬币的两面:\( a = 1 - \mathrm{TV}(p,q) = 1 - \frac{1}{2}\|p-q\|_1 \),最小化 L1 就是最大化拒绝采样下的期望接受概率;`tau_probabilistic` 则用 cumprod+1 给出期望接受长度的免费解析估计。
- 置信度头的监督目标是当前草稿分布对应的接受率(`detach` 切断自我指涉),用 BCEWithLogits 在 fp32 中计算,并配套 abs_error / bias / cumprod_bias 三个校准诊断量。
- 分子留本地、分母跨 rank SUM、返回值乘 `world_size` 抵消 FSDP 的梯度平均——三者合力保证任意卡数下梯度严格等于「全局 token 平权」的单步大批梯度。

## 7. 下一步学习建议

本讲补齐了 DSpark 训练侧的最后一块:你已经能从 batch 字段一路追到 backward 的标量。接下来:

1. **u4-l5(Gemma4 变体)**:看第二个模型族如何复用本讲的 `loss.py`——损失完全模型无关,只换 modeling/config。
2. **u5-l2(Eagle3 损失与训练器)**:对比另一种损失哲学(逐步 CE + 步长衰减,无蒸馏项),思考两者取舍。
3. 预习 **u6-l3(verify_draft_tokens)**:本讲的 \( \min(1, p/q) \) 期望接受率将在评估侧以真正的拒绝采样实现,训练期的 `accept_rate@t` 指标届时有了真值对照。
4. 动手方向:把综合实践脚本扩展成你自己的回归测试——改动 `loss.py` 任何一行后重跑,四个断言(公式一致、衰减生效、mask 生效、梯度不泄漏)能立刻告诉你有没有改坏语义。
