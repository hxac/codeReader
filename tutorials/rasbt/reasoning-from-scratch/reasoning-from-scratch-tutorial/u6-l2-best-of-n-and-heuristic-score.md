# Best-of-N 与启发式打分

## 1. 本讲目标

上一讲（u6-l1）我们给模型装上了「打分器」：`avg_logprob_answer` 能给一段文本打出平均 log 概率。但那个分数只度量「模型自己觉得这段话有多顺」，跟答案对不对没有直接关系。本讲介绍另一种打分器——**启发式打分 `heuristic_score`**，它不请模型当裁判，而是用几条人类写死的规则打分：「有 `\boxed{}` 加 2 分、能提取出数字加 1 分、越短越好」。

有了打分器，就能解锁一种新的推理时扩展方法——**Best-of-N**：对同一道题采样 N 个答案，用分数（而不是上一讲 u5-l4 的多数投票）选出最终答案。

学完本讲，你应当能够：

1. 分解 `heuristic_score` 的三档加分（boxed > 可提取数字 > 全文）与长度衰减奖励，并能手工计算任意候选的得分。
2. 说明 `best_of_n_math500.py` 的主循环如何采样 N 个答案、逐个打分、以严格最大分数选优，以及它为什么**不能**提前停止采样。
3. 对比 Best-of-N（打分择优）与自一致性（投票择优、打分仅做平票裁决）两种策略，并结合仓库 README 中的 500 题实测数据解释各自的适用条件。

## 2. 前置知识

### 2.1 奖励塑形：没有裁判时，先定一个「代理分数」

在 MATH-500 评测里，我们其实有一个完美的裁判：`grade_answer`（u4-l3 讲过的 SymPy 数学等价判定）。但注意一个关键区别：

- **判分（grading）**：拿模型答案与标准答案比对，只用于评测阶段打准确率。
- **选择（selection）**：在 N 个候选中挑一个当最终答案。

选择阶段**不能**用 `grade_answer`——标准答案在真正使用模型时是不可见的，用它来挑答案等于作弊。所以在没有外部裁判的场合，只能设计一个**不看标准答案**的代理分数，凭经验假设「格式规整（有 `\boxed{}`）、能给出明确数值、表述简洁」的答案更可能正确。这种用人工规则给「好行为」加分的做法，在强化学习里叫**奖励塑形（reward shaping）**。第 7 章的 RLVR 会用真正的验证器替代这些启发式，到时候可以回头对比。

### 2.2 指数衰减：越短越好，但不是线性扣分

`heuristic_score` 的简洁奖励是：

\[ \text{brevity}(a) = 1.5 \cdot e^{-|a| \,/\, 500} \]

其中 \( |a| \) 是答案文本的**字符数**（Python 的 `len(answer)`，不是 token 数）。这个函数的性质：

- 空文本（\( |a|=0 \)）拿到满分 1.5；
- 每多写 500 个字符，奖励再乘一个 \( e^{-1} \approx 0.368 \)；
- 它单调递减且**永远大于 0**——长答案不会被扣成负分，只是奖励趋近于 0。

选指数衰减而不是「每字符扣 0.001」的线性衰减，是为了让惩罚集中在「从很短到中等长度」这一段——一个 10 字符与一个 100 字符的答案奖励差 0.24，而 1000 与 2000 字符的答案奖励只差 0.18。前几百字符的「啰嗦程度」最影响体验。

### 2.3 Best-of-N：采样 N 个，用分数挑一个

上一讲 u5-l4 的自一致性（self-consistency）与 Best-of-N 同属「采样 N 个答案再选 1 个」的推理时扩展家族，流水线的前半段完全相同：

| 阶段 | 自一致性（u5-l4） | Best-of-N（本讲） |
|------|------------------|-------------------|
| ① 采样 | 温度 + top-p 采 N 次 | 温度 + top-p 采 N 次 |
| ② 提取 | `extract_final_candidate` 取短答案 | 同左（但只作记录） |
| ③ 选择 | `Counter` 多数投票，平票返回 None | 打分器对**完整生成文本**打分，取最高分 |
| ④ 加速 | 票数过半可提前停止 | **不能**提前停止，必须看完 N 个 |

直觉区别：投票相信「群众的共识」（多数答案相同的那个），打分相信「个体质量」（格式最好、最干脆的那个）。当模型单链正确率超过 50% 时，投票通常更强——错误答案很难恰好多数一致；而打分器只看格式与长度，容易被「短而无脑」的答案骗过。本讲最后会用仓库的真实实验数据验证这个直觉。

### 2.4 需要回收的前置术语

- `extract_final_candidate`（u4-l2）：从生成文本中提取短答案，`fallback` 参数控制回退策略——本讲的分档判定正是靠它的三种 fallback 行为实现的。
- `avg_logprob_answer`（u6-l1）：另一种可插拔打分器，本讲的 Best-of-N 脚本同样支持，作为对照组。
- `generate_text_stream_concat_flex` + `generate_text_top_p_stream_cache`（u5-l1/u5-l2）：可注入生成引擎的采样函数组合，Best-of-N 的采样阶段直接复用。

## 3. 本讲源码地图

| 文件 | 角色 |
|------|------|
| [reasoning_from_scratch/ch05.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch05.py) | 第 5 章包代码。本讲焦点是 `heuristic_score`（L14-43）；`avg_logprob_answer`（L99-121）作为对照打分器 |
| [reasoning_from_scratch/ch03.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py) | 第 3 章包代码。`extract_final_candidate`（L186-205）的三种 fallback 行为是分档判定的依赖；`grade_answer` 用于评测阶段判分 |
| [ch05/02_math500-more-inference-scaling-scripts/best_of_n_math500.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch05/02_math500-more-inference-scaling-scripts/best_of_n_math500.py) | Best-of-N 独立评测脚本：改造 `self_consistency_vote` 为打分选优，套进 MATH-500 主循环 |
| [ch05/02_math500-more-inference-scaling-scripts/self_consistency_scorer_math500.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch05/02_math500-more-inference-scaling-scripts/self_consistency_scorer_math500.py) | 对照脚本：保留多数投票，打分器只用于平票裁决 |
| [ch05/02_math500-more-inference-scaling-scripts/README.md](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch05/02_math500-more-inference-scaling-scripts/README.md) | 两套方法的完整 500 题实测数据表与复现命令 |
| [ch05/02_math500-more-inference-scaling-scripts/self_refinement_math500.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch05/02_math500-more-inference-scaling-scripts/self_refinement_math500.py) | 下一讲（u6-l3）主角，此处只看它如何把 `heuristic_score` 当 `score_fn` 直接赋值 |

## 4. 核心概念与源码讲解

### 4.1 heuristic_score：三档加分 + 简洁奖励

#### 4.1.1 概念说明

`heuristic_score` 是一个纯 Python 函数：输入答案文本，输出一个 0 到 3.5 之间的浮点数，**完全不需要模型参与**（不调 forward、不需要 GPU）。它的打分公式：

\[ \text{score}(a) \;=\; \underbrace{\begin{cases} 2.0 & \text{含 } \texttt{\textbackslash boxed} \\ 1.0 & \text{无 boxed，但能提取出数字} \\ 0.0 & \text{其他（返回全文兜底）} \end{cases}}_{\text{三档加分，互斥}} \;+\; \underbrace{1.5\, e^{-|a|/500}}_{\text{简洁奖励}} \]

三档的依据来自评测链路的经验（u4-l2 讲过提取器）：

1. **有 `\boxed{}`**（+2.0）：模型遵循了输出契约，`\boxed{}` 里的内容就是机器可解析的最终答案——这是最可靠的一档。
2. **没有 boxed 但文本里有数字**（+1.0）：模型没按格式作答，但至少给出了一个数值，`extract_final_candidate` 的数字回退还能救回来。
3. **既无 boxed 也无数字**（+0.0）：只能把整段文字当答案，判分几乎必然失败，不给奖励。

注意「互斥」：三档是 if / elif / else 的瀑布，一个答案只落进一档。简洁奖励则对**所有**答案（包括空字符串）都加上。

#### 4.1.2 核心流程

```text
输入: answer（完整生成文本）, brevity_bonus=500, boxed_bonus=2.0, extract_bonus=1.0, fulltext_bonus=0.0

1. cand = extract_final_candidate(answer, fallback=None)
      # fallback=None → 只有存在 \boxed{} 时才返回非空
2. 若 cand 非空: score += boxed_bonus; 跳到第 5 步
3. cand = extract_final_candidate(answer, fallback="number_only")
      # 无 boxed 时找最后一个数字；找不到返回空串（不回退全文）
4. 若 cand 非空: score += extract_bonus
   否则: cand = extract_final_candidate(answer, fallback="number_then_full")
         # 兜底返回全文（只要 answer 非空就非空），加 fulltext_bonus=0.0
5. score += 1.5 * exp(-len(answer) / brevity_bonus)   # 字符数
6. 返回 score
```

默认参数下几个典型取值（brevity 项，\( 1.5e^{-|a|/500} \)）：

| 字符数 \( |a| \) | 0 | 50 | 100 | 500 | 1000 | 2000 |
|---|---|---|---|---|---|---|
| brevity 奖励 | 1.50 | 1.36 | 1.23 | 0.55 | 0.20 | 0.03 |

于是三档的**实际总分区间**（默认参数）：

| 档位 | 加分 | 总分区间 |
|------|------|----------|
| 有 boxed | 2.0 | \( (2.0,\ 3.5] \) |
| 只有数字 | 1.0 | \( (1.0,\ 2.5] \) |
| 全文兜底 / 空文本 | 0.0 | \( [0,\ 1.5] \) |

**一个容易被忽略的细节：档与档之间的 1.0 分差是可以被简洁奖励跨越的。** 第二档反超第一档的条件是：

\[ 1.0 + 1.5\,e^{-L_2/500} > 2.0 + 1.5\,e^{-L_1/500} \;\Longleftrightarrow\; 1.5\left(e^{-L_2/500} - e^{-L_1/500}\right) > 1.0 \]

左边两项之差最大只能到 1.5（\( L_2 \to 0 \)、\( L_1 \to \infty \)），所以跨越需要长度相当悬殊。取极端例子 \( L_2 = 2 \)（答案就是 `"42"` 两个字符），解出 \( L_1 \approx 556 \)：**只要 boxed 答案超过约 556 个字符，一个只有两个字符的裸数字得分就会反超它**。而 CoT 提示下 `max_new_tokens=2048` 的生成动辄上千字符，这种反超在实践中并不罕见。这正是本讲实践环节要动手验证的现象。

#### 4.1.3 源码精读

**打分主体**，[reasoning_from_scratch/ch05.py:L14-L43](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch05.py#L14-L43)：函数签名含 `prompt=None` 占位参数与四个可调权重；L25 以 `fallback=None` 探测 boxed；L31-39 是数字档与全文档的瀑布；L42 是指数衰减的简洁奖励。

```python
def heuristic_score(
    answer,
    prompt=None,  # Placeholder that is ignored
    brevity_bonus=500.0,
    boxed_bonus=2.0,
    extract_bonus=1.0,
    fulltext_bonus=0.0,
):
    score = 0.0

    # Reward answers that have a final boxed value
    cand = extract_final_candidate(answer, fallback=None)
    if cand:
        score += boxed_bonus

    # Give weaker rewards if answer doesn't have a boxed value
    else:
        cand = extract_final_candidate(answer, fallback="number_only")
        if cand:
            score += extract_bonus
        else:
            cand = extract_final_candidate(
                answer, fallback="number_then_full"
            )
            if cand:
                score += fulltext_bonus

    # Add a brevity reward that decays with text length
    score += 1.5 * math.exp(-len(answer) / brevity_bonus)
    return score
```

逐点说明：

- **`prompt=None, # Placeholder that is ignored`**——这个参数在函数体内一次都没被用到。它存在的意义是**接口对齐**：第 5 章的所有打分器都要能塞进 `self_refinement_loop` 的 `score_fn(answer=..., prompt=...)` 调用位（见 [reasoning_from_scratch/ch05.py:L182-L185](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch05.py#L182-L185)）。对比另一件打分器 `avg_logprob_answer`——它需要 `model/tokenizer/prompt/device` 四个额外参数，必须用 `functools.partial` 预绑定后才能当 `score_fn` 用（见 [ch05/02_math500-more-inference-scaling-scripts/self_refinement_math500.py:L97-L107](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch05/02_math500-more-inference-scaling-scripts/self_refinement_math500.py#L97-L107)：`score_fn = heuristic_score` 直接赋值，`avg_logprob_answer` 则要 `partial(...)` 包装）。这就是 u6-l1 说过的「可插拔 score_fn」约定的落地。
- **三档判定完全委托给提取器**，`heuristic_score` 自己不写任何正则。它靠 `extract_final_candidate` 三种 fallback 的行为差异来「探测」文本形态。

**分档判定的依赖**——[reasoning_from_scratch/ch03.py:L186-L205](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L186-L205)，`extract_final_candidate` 的 fallback 分支（u4-l2 已精读过全文，这里只看 fallback 语义）：

```python
def extract_final_candidate(text, fallback="number_then_full"):
    result = ""
    if text:
        boxed = get_last_boxed(text.strip())
        if boxed:
            result = boxed.strip().strip("$ ")
        elif fallback in ("number_then_full", "number_only"):
            m = RE_NUMBER.findall(text)
            if m:
                result = m[-1]            # 取最后一个数字
            elif fallback == "number_then_full":
                result = text             # 兜底返回全文
    return result
```

对照三种调用方式：

| 调用（在 heuristic_score 中） | 无 boxed 时找数字 | 无数字时兜底 | 用途 |
|---|---|---|---|
| `fallback=None` | 不会走到（None 不在元组里） | 无 → 返回空串 | **只**认 boxed：返回非空 ⟺ 有 boxed |
| `fallback="number_only"` | 取最后一个数字 | 无 → 返回空串 | 探测「有数字但没 boxed」 |
| `fallback="number_then_full"` | 取最后一个数字 | 返回全文 | 探测「连数字都没有」 |

空字符串在 Python 中为假，于是 `if cand:` 就成了天然的「这一档是否命中」开关——分档逻辑与提取逻辑复用同一份代码，`heuristic_score` 里没有一个正则表达式。

**注意打分对象是完整生成文本**。`heuristic_score(answer, ...)` 的 `answer` 是 `generate_text_stream_concat_flex` 返回的完整回答（含推理过程），不是提取后的短答案——简洁奖励惩罚的长度是**整段回答的长度**，「鼓励模型别啰嗦」而非「鼓励答案数值短」。

#### 4.1.4 代码实践

**实践目标**：亲手复算三档加分与简洁奖励，再通过调参观察「档序被简洁奖励跨越 / 被大档差锁死」两种状态。

**操作步骤**（以下为示例代码，保存为任意 `.py` 文件在仓库根目录运行；本实践不需要模型与 GPU，`heuristic_score` 是纯函数）：

```python
# 示例代码
import math
from reasoning_from_scratch.ch05 import heuristic_score

candidates = {
    # ① 短 boxed：约 25 字符
    "A_short_boxed": "The answer is $\\boxed{42}$.",
    # ② 极短裸数字：2 字符
    "B_bare_number": "42",
    # ③ 中等长度、无 boxed、含数字：约 58 字符
    "C_number_sentence": "After working through the steps, the final result is 3.14.",
    # ④ 长 boxed：约 780 字符（43 字符的句子 × 18 + 结尾 boxed）
    "D_long_boxed": ("Step: compute the partial sums carefully. " * 18) + " $\\boxed{0.5}$",
    # ⑤ 长且无数字：约 784 字符
    "E_long_no_number": ("To solve this problem, we proceed step by step. " * 16),
}

def rank(label, **kw):
    print(f"\n=== {label} ===")
    scored = [(name, len(text), heuristic_score(text, **kw))
              for name, text in candidates.items()]
    for name, ln, sc in sorted(scored, key=lambda x: -x[2]):
        print(f"{sc:7.4f}  {name:18s} len={ln}")

rank("默认参数 boxed_bonus=2.0, brevity_bonus=500")
rank("调参后   boxed_bonus=10.0, brevity_bonus=100",
     boxed_bonus=10.0, brevity_bonus=100.0)

# 手工复算候选 B 与 D（默认参数），验证与函数输出一致
print("\nB_bare_number 手算:", 1.0 + 1.5 * math.exp(-2 / 500))
print("D_long_boxed   手算:", 2.0 + 1.5 * math.exp(-len(candidates['D_long_boxed']) / 500))
```

**需要观察的现象**：

1. 默认参数下五个候选的得分与名次。手工核对两条：`B_bare_number` 应为 \( 1.0 + 1.5e^{-2/500} \approx 2.494 \)；`D_long_boxed` 应为 \( 2.0 + 1.5e^{-|a|/500} \)，\(|a| \approx 780\) 时约 2.31。
2. **名次异常**：默认参数下，2 字符的裸数字 `B`（约 2.49 分）排在约 780 字符、格式规整的 boxed 完整解答 `D`（约 2.31 分）**之前**——这就是 4.1.2 节推导的「跨越约 556 字符阈值」现象。
3. 调参后（`boxed_bonus=10.0, brevity_bonus=100`）重新排序：`D` 应反超 `B` 升到第二位（\( 10 + 1.5e^{-7.8} \approx 10.0 \)，而 `B` 仍约 \( 1.0 + 1.5e^{-0.02} \approx 2.47 \)）；`A` 以 \( 10 + 1.5e^{-0.05} \approx 11.4 \) 居首；`E` 跌至约 0 分。

**预期结果**：默认参数排名大致为 `A > B > C > D > E`；调参后变为 `A > D > B > C > E`。名次变化的原因有二：其一，`boxed_bonus` 从 2.0 提到 10.0 后，档差 9.0 远超简洁奖励的理论最大差值 1.5，档序从「可跨越」变成「硬约束」；其二，`brevity_bonus` 从 500 降到 100 后衰减快了五倍，长答案的简洁奖励几乎归零、短答案相对优势被放大，于是长而规整的 `D` 不再因为长度吃亏。具体数值以本地运行为准（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：`heuristic_score("")`（空字符串）得多少分？这个分数意味着空答案在 Best-of-N 中可能排到什么位置？

**答案**：\( 0 + 1.5 \cdot e^{0} = 1.5 \)。它落在第三档（全文兜底，0.0 加分）的分数区间内，且是简洁奖励的满分。也就是说，一个空答案会打平「中等长度的纯文字废话」（第三档），但永远低于任何含数字（>1.0 且数字极短时可逼近 2.5）或含 boxed 的答案——幸好如此，否则 Best-of-N 会专门挑空答案。

**练习 2**：为什么 `heuristic_score` 的 `prompt` 参数明明没用还保留在签名里？删掉它会破坏什么？

**答案**：为了满足第 5 章打分器的统一接口 `score_fn(answer=..., prompt=...)`。调用方 `self_refinement_loop` 以关键字形式传入两个参数（ch05.py L183 与 L229-231）；若删掉 `prompt`，`heuristic_score` 就无法像 [self_refinement_math500.py:L97-L98](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch05/02_math500-more-inference-scaling-scripts/self_refinement_math500.py#L97-L98) 那样 `score_fn = heuristic_score` 一行直接赋值，也得包一层 `partial`/`lambda`。保留占位参数是让打分器「即插即用」的最低成本做法。

**练习 3**：默认参数下，要让第二档（无 boxed、有数字）反超第一档（有 boxed），两个答案的长度需要满足什么关系？举一组具体数值。

**答案**：需 \( 1.5(e^{-L_2/500} - e^{-L_1/500}) > 1.0 \)。取 \( L_2 = 2 \)（如 `"42"`），解 \( e^{-L_1/500} < (1.5 \times 0.996 - 1.0)/1.5 \approx 0.3296 \)，得 \( L_1 \gtrsim 556 \) 字符。即：数字答案只有 2 字符、boxed 答案超过约 556 字符时反超发生。这解释了为什么 CoT 长回答场景下裸数字偶尔会赢得 Best-of-N——它提醒我们启发式分数是格式与长度的代理，不是正确性的度量。

### 4.2 best_of_n_math500.py：Best-of-N 主循环

#### 4.2.1 概念说明

`best_of_n_math500.py` 把 Best-of-N 做成可复现的 MATH-500 评测脚本。一个有趣的工程细节是：它**没有新写一个函数**，而是在第 4 章的 `self_consistency_vote` 骨架上打补丁——所有改动都用 `# NEW (Best-of-N)` 注释标出。函数名沿用 `self_consistency_vote`，但新增的 `scoring` 参数让它从「投票机」变成「选优机」。

这种「同名函数、参数切换行为」的写法在教学代码里便于 diff 对比（和 `self_consistency_scorer_math500.py` 的 `# NEW2` 注释风格一致），但代价是函数名与实际行为脱节——读代码时要以参数分支为准，不能望文生义。

#### 4.2.2 核心流程

主循环伪代码（`self_consistency_vote`，Best-of-N 模式）：

```text
best_score = -∞, best_idx = None
for i in 0 .. num_samples-1:
    ① torch.manual_seed(seed + i + 1)          # 每个样本独立种子，可复现
    ② answer = 采样生成(prompt, temperature, top_p, max_new_tokens)
    ③ short = extract_final_candidate(answer)  # 记录短答案（不参与选择）
    ④ counts[short] += 1; groups[short].append(i)   # 投票账本照记（仅记录）
    ⑤ score = 打分器(answer)：                    # 对完整文本打分
         scoring=="heuristic" → heuristic_score(answer, prompt=prompt)
         scoring=="logprob"   → avg_logprob_answer(model, tokenizer, prompt, answer, device)
         其他                 → ValueError
    ⑥ 若 score > best_score（严格大于）: best_score, best_idx = score, i
    （没有 early stop —— 必须采满 N 个）
final_answer = short_answers[best_idx]
```

评测主循环 `evaluate_math500_stream` 对每道题执行：渲染 prompt（+ CoT 后缀）→ `self_consistency_vote(scoring=...)` → 取 `final_answer` → 找回对应的完整生成文本写日志 → `grade_answer` 对标准答案判分 → 逐行写 jsonl → 打印 ETA。

#### 4.2.3 源码精读

**打分分支与最优跟踪**——[ch05/02_math500-more-inference-scaling-scripts/best_of_n_math500.py:L76-L96](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch05/02_math500-more-inference-scaling-scripts/best_of_n_math500.py#L76-L96)：

```python
        if scoring == "heuristic":  # NEW (Best-of-N)
            score = heuristic_score(answer, prompt=prompt)  # NEW (Best-of-N)
        elif scoring == "logprob":  # NEW (Best-of-N)
            score = avg_logprob_answer(  # NEW (Best-of-N)
                model=model, tokenizer=tokenizer, prompt=prompt, answer=answer, device=device  # NEW (Best-of-N)
            )  # NEW (Best-of-N)
        else:  # NEW (Best-of-N)
            raise ValueError(f"Unknown scoring method: {scoring}")  # NEW (Best-of-N)
        if score > best_score:  # NEW (Best-of-N)
            best_score, best_idx = score, i  # NEW (Best-of-N)
```

这段是 Best-of-N 的心脏：每个样本生成后立即打分（L77 对**完整文本** `answer` 打分，而非短答案 `short`），并以**严格大于**更新最优。平分时保留先出现者——`>` 而非 `>=` 意味着并列时偏向更早的样本，这是一个确定性的平分规则。

**为什么不能提前停止**——[best_of_n_math500.py:L90-L92](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch05/02_math500-more-inference-scaling-scripts/best_of_n_math500.py#L90-L92)：

```python
        #########################################################
        # Track best candidate; no early stopping to allow best-of-n scoring
        #########################################################
```

u5-l4 的投票版可以在某答案票数过半时提前终止（后面的样本不可能翻盘）；打分版则不行——**分数没有「过半」这种多数派性质**，第 N 个样本随时可能拿到全场最高分，所以必须采满 N 个才能定论。这也意味着 Best-of-N 与自一致性在相同 N 下的生成开销几乎一样（README 实测 327.7 vs 328.2 分钟，差别在打分本身：`avg_logprob_answer` 需要额外一次 forward，`heuristic_score` 纯 CPU 秒算）。

**最终选择与返回结构**——[best_of_n_math500.py:L94-L105](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch05/02_math500-more-inference-scaling-scripts/best_of_n_math500.py#L94-L105)：

```python
    if best_idx is not None:  # NEW (Best-of-N)
        final_answer = short_answers[best_idx]  # NEW (Best-of-N)
        majority_winners = [final_answer]  # NEW (Best-of-N)
```

注意选出的 `final_answer` 是最高分样本的**短答案**（提取后的形式），直接送去判分；`majority_winners` 被填成单元素列表，纯粹是为了兼容下游读取返回字典的代码——这里的「多数赢家」名不副实，实为「分数赢家」。

**评测主循环**——[best_of_n_math500.py:L133-L167](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch05/02_math500-more-inference-scaling-scripts/best_of_n_math500.py#L133-L167)：每题渲染 prompt 并追加 `prompt_suffix`（默认是 u5-l3 的 CoT 后缀 `"\n\nExplain step by step."`），调用打分版投票，取 `final_answer` 判分，并在 L160-L166 从 `short_answers` 里找回第一个匹配的完整生成文本写进日志（jsonl 的 `generated_text` 字段）——便于事后人工检查「被选中的那个答案长什么样」。

**命令行接口**——[best_of_n_math500.py:L275-L281](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch05/02_math500-more-inference-scaling-scripts/best_of_n_math500.py#L275-L281)：`--scoring` 只允许 `heuristic` / `logprob` 二选一（没有 `none`——不打分就不是 Best-of-N 了）；配合 `--num_samples`（默认 3）、`--temperature`、`--top_p`、`--prompt_suffix`、`--seed`（默认 42）等参数。README 的复现命令用 `--temperature 0.9 --top_p 0.9 --num_samples 3`（见 [ch05/02_math500-more-inference-scaling-scripts/README.md:L334-L344](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch05/02_math500-more-inference-scaling-scripts/README.md#L334-L344)）。

#### 4.2.4 代码实践

**实践目标**：读懂主循环并写出选择逻辑伪代码；有 GPU 的读者再跑一个 5 题小规模冒烟测试。

**操作步骤**：

1. 打开 [best_of_n_math500.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch05/02_math500-more-inference-scaling-scripts/best_of_n_math500.py)，从 `def self_consistency_vote`（L32）读到 L105，把 4.2.2 节的伪代码与真实代码逐行对上号，标出：种子设置（L53-54）、采样（L56-66）、短答案提取（L68）、投票账本（L69-75）、打分（L76-85）、最优更新（L84-85）。
2. 写出你自己的选择逻辑伪代码（不超过 10 行），必须体现三件事：打分对象是完整文本、严格大于更新、无提前停止。
3. （可选，需已下载 base 模型权重，约 1.2 GB；CPU 可跑但慢）在仓库根目录运行小子集冒烟测试：

   ```bash
   python ch05/02_math500-more-inference-scaling-scripts/best_of_n_math500.py \
       --which_model base --device cpu \
       --dataset_size 5 --num_samples 3 \
       --max_new_tokens 512 --temperature 0.9 --top_p 0.9 \
       --scoring heuristic
   ```

4. 运行结束后打开脚本同目录生成的 `math500_base-cpu-evaluate-script.jsonl`，查看每行的 `extracted`（被选中的短答案）与 `generated_text`（被选中的完整文本）。

**需要观察的现象**：伪代码环节应发现 `counts` 与 `groups` 在 Best-of-N 模式下只记账不决策；冒烟测试环节观察 jsonl 中被选中样本的 `generated_text` 是否普遍「带 `\boxed{}` 且相对较短」。

**预期结果**：jsonl 每行有 `index/problem/gtruth_answer/generated_text/extracted/correct` 六个字段；终端最后打印 `Accuracy: ...% (x/5)`。冒烟测试的具体准确率与耗时取决于硬件（待本地验证；作者在 DGX Spark 上跑满 500 题、n=3、max_new_tokens=2048 约 328 分钟，CPU 上 5 题 × 3 样本 × 512 token 可能要数十分钟量级）。

#### 4.2.5 小练习与答案

**练习 1**：如果把 L85 的 `if score > best_score` 改成 `>=`，Best-of-N 的结果会怎么变？哪种更合理？

**答案**：`>=` 时并列分数总是偏向**后出现**的样本，`>` 时偏向先出现的。两者选出答案的「分数」相同，但答案本身可能不同（平分时选的样本不同）。`>` 更常见也更稳定——它保证相同种子下结果确定，且不会因为后面样本「碰巧追平」就换答案；不过对启发式打分这种粗粒度分数（大量候选挤在同一档）而言，平分很常见，平分规则的选择实际影响不小，值得留意。

**练习 2**：`evaluate_math500_stream` 里为什么要在 L160-L166 花一个循环从 `short_answers` 找回 `full_answers[idx]`？

**答案**：因为 `final_answer` 是**提取后的短答案**（比如 `"42"`），只拿它写日志无法人工复核模型当时的推理过程；而 Best-of-N 选中的是**第 `best_idx` 个样本**，日志需要的是那个样本的完整生成文本。循环找到第一个短答案等于 `extracted` 的样本，取其完整文本写入 `generated_text` 字段。（严格说若多个样本短答案相同，找回的不一定是 `best_idx` 那一个，但对人工复核而言内容等价。）

**练习 3**：`--scoring` 为什么没有 `none` 选项（对照 `self_consistency_scorer_math500.py` 有）？

**答案**：在 `self_consistency_scorer_math500.py` 里打分只是平票时的**辅助裁决**，`none` 表示平票直接取第一个并列者，投票照常工作；而在 `best_of_n_math500.py` 里打分是**唯一的**选择机制，没有打分这个脚本就退化成了「取第一个样本」，Best-of-N 不复存在，所以 `choices` 只有 `heuristic` 和 `logprob`。

### 4.3 与自一致性投票的对比：打分做主 vs 打分做辅

#### 4.3.1 概念说明

`self_consistency_scorer_math500.py` 是介于 u5-l4（纯投票）与本讲 Best-of-N（纯打分）之间的**混合方案**：投票为主，打分仅在**平票**时出面裁决。三个脚本的谱系：

| 脚本 | 选择机制 | 打分器角色 | 提前停止 |
|------|----------|-----------|----------|
| ch04 自一致性（u5-l4） | 多数投票 | 无 | 可（票数过半） |
| `self_consistency_scorer_math500.py` | 多数投票 | **平票裁决**（可选 `none`/`heuristic`/`logprob`） | 可 |
| `best_of_n_math500.py`（本讲） | 打分选优 | **唯一裁决** | 不可 |

平票是投票法的软肋：n=3 时三个样本三个不同答案（1:1:1）或两组各占一半并不罕见，u5-l4 的处理是返回 `None`（弃权），混合方案则让打分器在并列者中挑一个——既保留「共识优先」的优点，又消除了弃权样本。

#### 4.3.2 核心流程

平票裁决的伪代码：

```text
先跑完整投票（含可选 early stop）
若 final_answer is None（存在平票）:
    若 scoring == "none": 取 majority_winners[0]（先出现者）
    否则:
        for cand in majority_winners:          # 每个并列的短答案
            scores = [ 打分器(full_answers[idx]) for idx in groups[cand] ]
                 # 对该答案的每一个原始样本都打分
            cand_score = max(scores)           # 以「组内最好样本」代表该答案
        extracted = argmax_cand cand_score
否则: extracted = final_answer（投票直接出结果）
```

关键设计：**组内取最大值**。同一个短答案（如 `"42"`）可能来自多个格式迥异的样本——有的带完整推导和 boxed，有的只有裸数字。混合方案用组内最高分代表该答案的「最佳形态」，而非平均分，这与「一个答案只要被一次高质量地表达过」的直觉一致。

#### 4.3.3 源码精读

**投票本体与平票判定**——[ch05/02_math500-more-inference-scaling-scripts/self_consistency_scorer_math500.py:L76-L90](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch05/02_math500-more-inference-scaling-scripts/self_consistency_scorer_math500.py#L76-L90)：

```python
        # NEW
        # Early stop if one answer already meets >= 50% majority
        if early_stop and counts[short] > num_samples / 2:
            majority_winners = [short]
            final_answer = short
            break

    if final_answer is None:
        mc = counts.most_common()
        if mc:
            top_freq = mc[0][1]
            majority_winners = [s for s, f in mc if f == top_freq]
            final_answer = mc[0][0] if len(majority_winners) == 1 else None
```

这段与 u5-l4 的投票版一致：票数严格过半可提前终止；结束后若最高频答案不唯一，`final_answer` 置 `None`、`majority_winners` 收集所有并列者——交给打分裁决。

**平票裁决分支**——[self_consistency_scorer_math500.py:L151-L178](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch05/02_math500-more-inference-scaling-scripts/self_consistency_scorer_math500.py#L151-L178)：

```python
            if results["final_answer"] is None:  # NEW2
                if scoring == "none":  # NEW2
                    extracted = results["majority_winners"][0]  # NEW2
                else:  # NEW2
                    best = None  # NEW2
                    best_score = float("-inf")  # NEW2
                    for cand in results["majority_winners"]:  # NEW2
                        scores = []  # NEW2
                        for idx in results["groups"][cand]:  # NEW2
                            candidate_full = results["full_answers"][idx]  # NEW2
                            if scoring == "heuristic":  # NEW2
                                score = heuristic_score(candidate_full, prompt=prompt)  # NEW2
                            elif scoring == "logprob":  # NEW2
                                score = avg_logprob_answer(  # NEW2
                                    model=model, tokenizer=tokenizer,  # NEW2
                                    prompt=prompt, answer=candidate_full,  # NEW2
                                    device=device,  # NEW2
                                )  # NEW2
                            scores.append(float(score))  # NEW2
                        cand_score = max(scores)  # NEW2
                        if cand_score > best_score:  # NEW2
                            best_score = cand_score  # NEW2
                            best = cand  # NEW2
                    extracted = best  # NEW2
```

对照 4.2.3 的 Best-of-N 打分分支，代码形状几乎相同（同样的 if/elif 打分、同样的严格大于更新），但**作用域完全不同**：这里只对 `majority_winners`（并列者）× `groups[cand]`（各自样本）双重循环打分，Best-of-N 则对所有 N 个样本打分。打分从「主裁判」降级为「加时赛裁判」。

**实测数据对比**——[ch05/02_math500-more-inference-scaling-scripts/README.md:L253-L258](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch05/02_math500-more-inference-scaling-scripts/README.md#L253-L258) 与 [README.md:L321-L325](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch05/02_math500-more-inference-scaling-scripts/README.md#L321-L325)。base 模型 + CoT 后缀、n=3、全部 500 题（DGX Spark）：

| 方法 | 准确率 | 用时 |
|------|--------|------|
| 基线（CoT 单样本贪心） | 33.4% | 129.2 min |
| 自一致性 n=3 纯投票 | 43.2% | 328.2 min |
| 自一致性 n=3 + heuristic 平票裁决 | 43.4% | 326.5 min |
| 自一致性 n=3 + logprob 平票裁决 | 44.8% | 327.7 min |
| Best-of-N n=3 + heuristic | 40.6% | 327.7 min |
| Best-of-N n=3 + logprob | 43.2% | 330.2 min |

三条结论：

1. **采样 + 择优整体有效**：所有 n=3 方法都比单样本基线高 7–11 个百分点，且耗时约为基线的 2.5 倍（接近 3 倍采样量的理论值）。
2. **n=3 时投票强于打分**：纯投票 43.2% > Best-of-N heuristic 40.6%。投票利用的是「多个独立样本恰好答对同一答案」的统计信号，而启发式分数只看格式与长度——一个短而无脑的错误答案（练习算过，2 字符的裸数字可得约 2.49 分）完全可能压过冗长的正确解答。
3. **打分器质量决定 Best-of-N 上限**：换成 `avg_logprob_answer` 后 Best-of-N 从 40.6% 升到 43.2%，追平纯投票；混合方案中 logprob 裁决（44.8%）也是全场最佳。模型似然比人工规则更能识别「质量」，但它要为每个候选多跑一次 forward（对照 heuristic 的纯 Python 秒算），这也是 330.2 vs 327.7 分钟的差异来源。理论上当 N 增大、或单链正确率不足 50%（投票的理论前提，见 u5-l4）时，打分选优相对投票还有翻盘空间——这留作读者的扩展实验。

#### 4.3.4 代码实践

**实践目标**：用不加载模型的方式，亲手走一遍「平票 → 打分裁决」与「纯打分选优」两条路径，体会计分口径差异。

**操作步骤**（以下为示例代码；不需要 GPU，只复用打分函数与数据结构）：

```python
# 示例代码
from collections import Counter
from reasoning_from_scratch.ch05 import heuristic_score

# 模拟 4 个采样：2:2 平票（"42" 组与 "0.5" 组各两票）
full_answers = [
    "The answer is $\\boxed{42}$.",                 # → "42"
    "42",                                            # → "42"
    ("Step: compute the partial sums carefully. " * 18) + " $\\boxed{0.5}$",  # → "0.5"
    "After working through the steps, the result is 0.5.",  # → "0.5"
]
short_answers = ["42", "42", "0.5", "0.5"]

# 1) 纯投票：平票 → final_answer = None
counts = Counter(short_answers)
mc = counts.most_common()
top_freq = mc[0][1]
majority_winners = [s for s, f in mc if f == top_freq]
final_answer = mc[0][0] if len(majority_winners) == 1 else None
print("投票结果:", final_answer, "并列者:", majority_winners)

# 2) 混合方案：平票裁决，组内取最大分
groups = {}
for i, s in enumerate(short_answers):
    groups.setdefault(s, []).append(i)
best, best_score = None, float("-inf")
for cand in majority_winners:
    cand_score = max(heuristic_score(full_answers[i]) for i in groups[cand])
    print(f"候选 {cand!r} 组内最高分: {cand_score:.4f}")
    if cand_score > best_score:
        best_score, best = cand_score, cand
print("平票裁决胜者:", best)

# 3) Best-of-N：四个样本全打分，取最高
for i, (s, f) in enumerate(zip(short_answers, full_answers)):
    print(f"样本{i} score={heuristic_score(f):.4f} len={len(f)}")
```

**需要观察的现象**：步骤 1 输出 `final_answer=None`（2:2 平票）；步骤 2 中 `"42"` 组的组内最高分来自第 0 个样本（短 boxed，约 3.4 分），`"0.5"` 组的最高分来自第 2 个样本（长 boxed，约 2.3 分）——两组都取到了各自的「最佳形态」；步骤 3 的全场最高分样本与步骤 2 的裁决胜者是否一致。

**预期结果**：平票裁决与 Best-of-N 都应选出 `"42"`（其最佳样本是全场最高分）。注意体会口径差别：混合方案中 `"0.5"` 若有第二个高质量短样本仍有机会胜出——因为它比的是**组内最大值**，而投票已经给了两个组相同的起点；Best-of-N 则完全无视 2:2 的票型。具体数值以本地运行为准（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：混合方案用 `max(scores)` 而不是 `sum(scores)/len(scores)` 代表一个答案的分数，各有什么道理？什么时候平均值更合理？

**答案**：`max` 的语义是「这个答案曾被最好的方式表达过」，符合「组内有一个规整样本即可」的直觉，且不受组内垃圾样本拖累；`mean` 的语义是「这个答案的平均表达质量」，在组内样本数不均（如 3:2 平票时两组样本数不同）时更能反映整体水平，且更抗单样本侥幸。当采样数 N 较大、组内样本数较多时，`mean`（或 top-2 平均）通常更稳健；N 小、平票组常只有 1-2 个样本时，两者差别不大，`max` 实现最简单。

**练习 2**：结合 README 数据，说明为什么 Best-of-N + heuristic（40.6%）反而低于纯投票（43.2%），而 Best-of-N + logprob（43.2%）能追平投票？

**答案**：heuristic 分数只奖励「有 boxed、有数字、短」，与正确性只有弱相关——它经常把票投给短小精悍但错误的裸数字（4.1.5 练习 3 的跨越现象）。而投票的正确性信号来自「多个独立采样收敛到同一答案」，错误答案彼此不同的概率远大于彼此相同。logprob 是模型自身的似然度量，对「这段推理模型自己是否信服」有一定区分度，所以能把 Best-of-N 拉回投票的水平。结论：**Best-of-N 的上限由打分器质量决定**，启发式打分只适合做廉价的兜底裁决。

**练习 3**：三个脚本的采样阶段（种子、温度、top-p、提取）几乎逐字相同，这是重复代码的坏味道吗？

**答案**：从工程看确实是复制粘贴式演化（作者刻意用 `# NEW`/`# NEW2` 注释标注差异，便于书中对照教学），生产代码应把「采样 N 个 → (short, full, counts, groups)」抽成公共函数，把「选择策略」做成可插拔参数。但从教学看，三个脚本并排阅读能精确看到「从投票到打分」每一步改动，这是本书「章节代码即 diff」风格的取舍。第 7 章的 GRPO 脚本会看到同样的演进方式。

## 5. 综合实践

**任务**：不加载模型，实现一个「桌面级 Best-of-N + 调参对照实验」，把本讲三个模块串起来。

1. **构造题库与候选**：为 3 道简单数学题（如「12 × 8」「\(\sqrt{144}\)」「1/2 + 1/3」）各手写 4 个候选回答，刻意覆盖四种形态：短 boxed、长 boxed、短裸数字、长废话（无数字）。这一步代替真实的温度采样——重点在打分与选择，不在生成。
2. **实现选择器**（示例代码）：对每道题分别跑三种策略并对比：
   - 多数投票（`Counter`，平票记 `None`）；
   - 混合方案（平票时对并列者按组内 `max(heuristic_score)` 裁决）；
   - Best-of-N（全部候选过 `heuristic_score`，`score > best` 严格更新选最高）。
3. **调参重跑 Best-of-N**：`boxed_bonus=2.0→10.0`、`brevity_bonus=500→100` 再跑一遍，记录名次变化。
4. **输出一张对照表**：行是「题号 × 策略（× 参数）」，列是「选中候选、得分、是否含 boxed、字符数」，最后用一句话总结：哪种策略最容易被「短而错误」的候选骗过？调参后是否缓解？

**验收标准**：表格中至少出现一次三种策略选中不同候选的行；能指出该行中投票与打分分歧的原因（票型 vs 格式）；调参行能复现 4.1.4 的名次翻转逻辑。全程不需要 GPU（待本地验证）。

## 6. 本讲小结

- `heuristic_score` 是纯 Python 的启发式打分器：三档互斥加分（有 `\boxed{}` +2.0 > 可提取数字 +1.0 > 全文兜底 +0.0），再加 \( 1.5e^{-|a|/500} \) 的字符长度指数衰减奖励；三档判定完全复用 `extract_final_candidate` 三种 fallback 的行为差异，自身不含正则。
- 档与档 1.0 分的分差可被简洁奖励跨越：约 556 字符是「2 字符裸数字反超 boxed 长答案」的阈值，调大 `boxed_bonus` 可把档序变成硬约束，调小 `brevity_bonus` 会放大长短差距。
- `best_of_n_math500.py` 在 `self_consistency_vote` 骨架上以 `scoring` 参数切换为打分选优：对每个样本的**完整生成文本**打分、严格大于更新最优、必须采满 N 个（无 early stop）；`prompt` 占位参数让 `heuristic_score` 能直接充当统一的 `score_fn`。
- `self_consistency_scorer_math500.py` 是混合方案：投票为主，打分仅在平票时对并列答案按「组内最高分」裁决。
- 实测数据（500 题、n=3、base 模型）：Best-of-N + heuristic 40.6% < 纯投票 43.2% = Best-of-N + logprob；混合 + logprob 裁决 44.8% 最佳——启发式分数只度量格式与长度，Best-of-N 的上限由打分器质量决定。
- 三个脚本是同一份采样代码的复制演化（`# NEW` 注释标注差异），「采样 N 个」是公共前半段，「如何选 1 个」是可插拔后半段。

## 7. 下一步学习建议

本讲的 `heuristic_score` 与 `avg_logprob_answer` 都已就位为可插拔 `score_fn`，下一讲 **u6-l3 自我精炼循环** 将把它们用于完全不同的场景：不再「一次采样 N 个挑最好」，而是「对同一答案反复批评-修改-打分」，只有修订稿分数不低于旧稿才接受。建议先阅读 [reasoning_from_scratch/ch05.py:L124-L145](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch05.py#L124-L145) 的 `make_critique_prompt` / `make_refine_prompt`，注意它们如何强制修订稿以 `\boxed{}` 结尾——那正是本讲三档加分里最高的一档。之后可顺带读 [ch05/02_math500-more-inference-scaling-scripts/README.md:L38-L52](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch05/02_math500-more-inference-scaling-scripts/README.md#L38-L52) 的自我精炼实验表，预习「打分接受准则」在 1-2 轮迭代下的真实收益。
