# 聚合与组合策略：多证明精炼的采样算法

## 1. 本讲目标

上一讲（u5-l1）我们弄清了证明池如何把一题的所有历史证明持久化成「一题一文件的跨轮账本」。本讲顺着数据流往下走，钻进整条流水线里**算法密度最高**的一段：`_prepare_proof_agg_tasks` 的后半段（[inference/main.py:L222-L284](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L222-L284)）——它要回答的问题是：

> 对一道还没被完全做对的题，如何从一堆「半成品证明」里挑出最有价值的几条，把它们连同验证器的批评意见打包成新的提示词，让生成器在下一轮写出更好的证明？

学完本讲，你应该能够：

1. 解释「满分早停」与 `(meanscore, self_eval_score)` 双键降序排序加截断的候选筛选策略；
2. 复述 `itertools.combinations` 枚举候选组合、再用「排序后下标元组」作去重键逐个去重、凑满 `num_trials` 个即停的完整流程，并指出代码中递减循环的不可达分支；
3. 说明摘要拼装时评价采样的双层上限：单一分数桶最多 8 条、多分数桶每桶最多 `max_rating_per_score` 条、每个证明总量封顶 8 条；
4. 推导参数与请求量的数量关系，例如竞赛配置下 `n_agg_trials × n_sample = n_parallel_proof_gen` 的守恒式；
5. 独立解释 `_split_jobs` 的切分数学（分成约 50 块，而不是每块 50 题）。

## 2. 前置知识

### 2.1 承接前几讲：进入本讲时数据长什么样

经过 u4-l2（验证准备）与 u5-l1（证明池），`_prepare_proof_agg_tasks` 拿到的每题数据是一个四元组任务：

```python
(item, proof2ratings, proof2self_eval, proof2dep_proof_ids)
```

- `proof2ratings`：`{证明正文 -> [{'rating': 批语, 'score': 0/0.5/1}, ...]}`，同一证明被验证了多次（`n_verification_per_proof` 次）；
- `proof2self_eval`：`{证明正文 -> {'self_eval': 自评文字, 'self_eval_score': 自评分}}`；
- 在进入本讲的代码段之前，函数前半段（L165-L221，u5-l1 的内容）已经算好每个证明的均分 `meanscore`、按分数分桶的 `score2ratings`，把新证明写入证明池，并按 `use_old_proofs_for_refinement` 决定是否把历史证明并入候选列表。

此时每条候选记录是一个元组（新证明 6 元组、旧池证明 5 元组，取前 5 个字段即本文关心的部分）：

```
(proof, meanscore, score2ratings, self_eval_dict, proof_id)
```

### 2.2 关键术语

| 术语 | 含义 |
|---|---|
| 组合（combination） | 从候选证明里选出 `n_proofs_to_refine` 条构成的一个「精炼输入组」 |
| 试验（trial） | 一次精炼请求 = 一个去重后的组合；`num_trials`（即 `--n_agg_trials`）是每题组合数上限 |
| 去重键 | `tuple(sorted(组合内证明的下标))`，证明集合相同即视为同一组合 |
| 分数桶（score bucket） | `score2ratings` 字典的一个键值对：同一证明所有同一档（0 / 0.5 / 1）的批语归为一桶 |
| 摘要（summary） | 拼装进 `proof_refinement` 模板的文本块：若干个证明 + 各自被采样出的批语 |
| 早停 | 某题出现过均分近乎满分的证明后，不再为它生成精炼请求 |

### 2.3 为什么需要「多证明组合」精炼

单条证明 + 单条批语的精炼只能让模型「改错」；把多条证明放进同一个提示词，模型还可以「跨证明复用思路」——A 证明的前半段思路对、B 证明的后半段对，组合精炼有机会拼出完整解。`proof_refinement` 模板明确写了这三种用法（[inference/math_templates.py:L206-L208](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/math_templates.py#L206-L208)）：

> You should provide a better solution by solving issues mentioned in the evaluation(s), or by re-using promising ideas mentioned in the solution sample(s), or by doing both.

而组合的方式如果只有一种（比如永远取 top-k），采样就缺乏多样性。于是这段代码实现了一个**带去重的随机组合采样器**：排序选优 → 枚举组合 → 随机化顺序 → 去重限量。它本质上是在「测试时算力」预算内，最大化喂给生成器的「证明×批语」信息多样性。

## 3. 本讲源码地图

| 文件 | 本讲关注的段落 | 作用 |
|---|---|---|
| [inference/main.py:L222-L227](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L222-L227) | 早停 + 双键排序截断 | 决定哪些证明有资格进入组合 |
| [inference/main.py:L228-L241](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L228-L241) | 组合列表构造 + 枚举去重 | 决定生成多少个、哪些组合 |
| [inference/main.py:L242-L273](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L242-L273) | 分数桶采样 + 摘要拼装 + 模板渲染 | 决定每个精炼请求的内容 |
| [inference/main.py:L274-L284](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L274-L284) | 样本落盘与 trials 统计 | 输出结构与统计量 |
| [inference/main.py:L156-L163](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L156-L163) | `_split_jobs` | 任务切分，供多进程池消费 |
| [inference/main.py:L382-L387](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L382-L387) | `multiprocessing.Pool` 调用点 | 并行执行切分后的任务块 |
| [inference/main.py:L439-L442](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L439-L442) | 主循环传参处 | 实际生效的参数值（覆盖函数签名默认值） |
| [inference/math_templates.py:L203-L213](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/math_templates.py#L203-L213) | `proof_refinement` 模板 | 摘要的最终容器 |

一个容易踩的坑先放在地图里提醒：函数签名的默认值（`num_trials=16, n_best_proofs_to_sample=6, n_proofs_to_refine=4`，见 [inference/main.py:L165](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L165)）在真实流程中**从不生效**——主循环调用时总是显式传入 `args` 值（[inference/main.py:L439-L442](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L439-L442)）。读代码时以 argparse 默认值与 run.sh 为准。

## 4. 核心概念与源码讲解

### 4.1 满分早停与双键排序：选出候选证明

#### 4.1.1 概念说明

组合采样的第一步不是「选」，而是「不选」：一道题如果已经被验证器完全认可（均分近乎满分），继续精炼纯属浪费算力，直接跳过。这呼应了 u1-l1 讲过的「生成-验证差距」思想——验证器说满分，就当已解决。

对未解决的题，需要从可能几十条证明里圈出候选。圈选标准是两级：

1. **主键 `meanscore`**：验证器多次评分的均值，代表外部（验证器）对证明的信任度；
2. **次键 `self_eval_score`**：生成器自己的自评分（u4-l2 讲过它取自 Self Evaluation 小节最后一个 `\boxed{}` 值），代表内部自信度。

均分并列时用自评分打破平局；两者都并列时，靠预先洗牌随机打破。

#### 4.1.2 核心流程

```text
候选列表（新证明 + 可选旧池证明）
    │
    ├─ 任一记录 meanscore > 0.99999 ？ ── 是 ──> continue，该题本轮不生成精炼请求
    │                                        （但 trials 仍会记录 0）
    ├─ np.random.shuffle 十次          # 打乱稳定排序中的并列顺序
    │
    ├─ sorted(key=(meanscore, self_eval_score), reverse=True)
    │
    └─ 截取前 n_best_proofs_to_sample 条 ──> 候选证明池（下标 0..m-1）
```

注意排序键的比较是元组比较：先比 `x[1]`（meanscore），相等再比 `x[3]['self_eval_score']`。因为 Python 的 `sorted` 是**稳定排序**，键完全相同的记录会保持洗牌后的随机相对顺序——这就是「先洗牌十次」的全部意义：让并列者随机地落在截断窗口的边缘。

#### 4.1.3 源码精读

早停判断（[inference/main.py:L222-L223](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L222-L223)）：`record[1]` 就是元组里的 `meanscore`。此时列表已包含并入的旧池证明（`use_old_proofs_for_refinement=True` 时在 L219-L220 完成），所以**历史上任何一轮**出现过满分证明都会让该题永久早停。

```python
if any(record[1] > 0.99999 for record in proof_meanscore_ratings_tuples):
    continue
```

阈值写 `0.99999` 而非 `1.0` 是浮点防御：`meanscore` 由 `np.mean` 计算（[inference/main.py:L199](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L199)），而评分只取 0/0.5/1 三档，所以该阈值在数学上等价于「所有验证评分都是 1」——例如 64 次验证里 63 次 1 分、1 次 0.5 分，均值约 0.992，不会触发早停。

洗牌与双键排序（[inference/main.py:L225-L227](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L225-L227)）：排序后立即截取前 `n_best_proofs_to_sample` 条作为组合采样的下标空间。

```python
for _ in range(10):
    np.random.shuffle(proof_meanscore_ratings_tuples)
proof_meanscore_ratings_tuples = sorted(
    proof_meanscore_ratings_tuples,
    key=lambda x: (x[1], x[3]['self_eval_score']), reverse=True
)[:n_best_proofs_to_sample]
```

排序键里 `x[3]` 是 `self_eval` 字典（形如 `{'self_eval': ..., 'self_eval_score': ...}`）。新证明由 [inference/main.py:L342-L345](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L342-L345) 构造、旧池记录由 JSON 反序列化而来，两者此处结构一致，可以统一取键。

#### 4.1.4 代码实践

**实践目标**：亲眼看到「稳定排序 + 预洗牌」如何随机打破平局。

**操作步骤**（示例代码，可在任意目录以 `python` 交互式运行）：

```python
import numpy as np

# 6 条记录：两档 meanscore，各档内 self_eval_score 相同 → 排序键完全并列
recs = [(f"proof-{i}", 0.5, None, {'self_eval_score': 0}) for i in range(3)] + \
       [(f"proof-{i}", 0.25, None, {'self_eval_score': 0}) for i in range(3, 6)]

np.random.seed(0)
for _ in range(10):
    np.random.shuffle(recs)
ranked = sorted(recs, key=lambda x: (x[1], x[3]['self_eval_score']),
                reverse=True)[:4]
print([r[0] for r in ranked])
```

**需要观察的现象**：`np.random.seed(0)` 固定后输出可复现；改 `seed(1)`、`seed(2)` 再跑，0.5 分档的三条证明进入前 4 名的**顺序与截断结果**会变化（第 4 名一定来自 0.25 分档，前三名的内部顺序随机）。

**预期结果**：前 3 名总是三条 0.5 分证明（顺序随种子变化），第 4 名是某条 0.25 分证明。如果不先洗牌，同键记录将永远保持构造顺序，截断窗口边缘就失去了随机性。

#### 4.1.5 小练习与答案

**练习 1**：如果把 L225-L226 的十次洗牌删掉，算法会出什么问题？

**答案**：排序是稳定的，`(meanscore, self_eval_score)` 完全并列的证明会永远按「入池先后顺序」（新证明按 `proof2ratings` 字典插入序、旧池按文件行序）排列。于是截断窗口的边缘（第 `n_best_proofs_to_sample` 名）总是同一批证明，组合采样的多样性下降，且并列者的命运由偶然的数据装载顺序决定而非随机。

**练习 2**：为什么早停检查放在并入旧池证明**之后**？

**答案**：`use_old_proofs_for_refinement=True`（主循环 R≥2 固定传 True，[inference/main.py:L438](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L438)）时旧池证明在 L219-L220 追加进列表；若早停放在并入之前，上一轮已获满分的证明就检测不到，该题会被无意义地继续精炼。放在之后，「历史任一轮满分」即停。

### 4.2 组合枚举与递减去重：生成至多 num_trials 个精炼请求

#### 4.2.1 概念说明

有了 `m` 条候选证明（`m = min(n_best_proofs_to_sample, 候选数)`），下一步是产出若干个大小为 `k = n_proofs_to_refine` 的下标组合。设计上有三个要素：

1. **保底最优组合**：排第一的组合永远是「名次前 k 的证明、按名次顺序」，不随机——确保最优信息一定被喂给生成器一次；
2. **字典序枚举 + 随机洗牌**：其余组合来自 `itertools.combinations` 的字典序枚举，但每个组合的**内部顺序**先被 `np.random.shuffle` 打乱，使摘要中「Solution 0 / Solution 1 / …」的排布随机化；
3. **去重键 + 计数器二合一**：`dedup` 集合存 `tuple(sorted(前缀下标))`，既防止重复组合，又靠 `len(dedup) == num_trials` 实现限量。

#### 4.2.2 核心流程

```text
combinations = [ [0, 1, ..., k-1] ]                       # 保底最优组合
             + list(itertools.combinations(range(m), k))  # 字典序枚举，C(m,k) 个

若候选证明列表为空 → combinations = []   # 防止生成"零证明"样本

for i, indices in enumerate(combinations):
    if len(dedup) == num_trials: break      # 凑满即停
    if i > 0: np.random.shuffle(indices)    # 随机化组合内顺序
    for num_proofs_to_include in [k, k-1, ..., 1]:      # 递减前缀尝试
        if tuple(sorted(indices[:num_proofs_to_include])) in dedup:
            break                            # 该候选已用过 → 放弃
        <生成样本并登记 dedup>
        break                                # 生成完一个就进入下一候选
trials.append(len(dedup))
```

组合数的数学上限：

\[ \binom{m}{k} = \frac{m!}{k!\,(m-k)!} \]

竞赛配置下 `m = 32, k = 1`：上限 \(\binom{32}{1} = 32\)，`num_trials = 32`，恰好用满。函数签名默认配置下 `m = 6, k = 4`：上限 \(\binom{6}{4} = 15 < 16\)，凑不满 `num_trials`，实际生成 15 个（见 4.2.4 实践的推演）。

还有一个与整体算力守恒有关的事实：主循环在 R≥2 时取 `n_sample = n_parallel_proof_gen // n_agg_trials`（[inference/main.py:L448](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L448)），作为 generate.py 的 `--n` 参数（每条输入采样几次，见 u2-l2）。于是每题每轮的生成请求数为：

\[ n_{agg\_trials} \times n_{sample} \approx n_{parallel\_proof\_gen} \]

竞赛配置 \(32 \times 4 = 128\)。`n_agg_trials` 控制「多少个不同组合」，`n_sample` 控制「每个组合重复采样几次」，乘积守恒——这是 u4-l1 提过的「第二轮起生成条数为整除商」的另一面。

#### 4.2.3 源码精读

组合列表构造（[inference/main.py:L228-L231](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L228-L231)）：

```python
combinations = [list(range(min(n_proofs_to_refine, len(proof_meanscore_ratings_tuples))))] + \
    list(itertools.combinations(
        list(range(min(n_best_proofs_to_sample, len(proof_meanscore_ratings_tuples)))),
        min(n_proofs_to_refine, len(proof_meanscore_ratings_tuples))))
if not proof_meanscore_ratings_tuples:
    combinations = []
```

这段代码做三件事：第一项是保底组合 `[0..k-1]`（`k` 已对候选数取 `min` 防越界）；第二项用 `itertools.combinations` 按字典序枚举全部大小为 `k` 的下标组合——注意保底组合与枚举出的第一个 `(0, 1, ..., k-1)` **必然重复**，靠后面的 `dedup` 吸收；`min(n_best_proofs_to_sample, len(...))` 是双保险（L227 已截断到 `n_best`，正常情况下 `len ≤ n_best`）。空列表保护是必要的：若候选为 0，第一项会得到 `[[]]`、`combinations(range(0), 0)` 会得到 `[()]`，不置空就会生成「零证明」的精炼请求。

枚举与去重主循环（[inference/main.py:L232-L241](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L232-L241)）：

```python
dedup = set()
for i, indices in enumerate(combinations):
    if len(dedup) == num_trials:
        break
    indices = list(indices)
    if i > 0:
        np.random.shuffle(indices)
    for num_proofs_to_include in range(n_proofs_to_refine, 0, -1):
        if tuple(sorted(indices[:num_proofs_to_include])) in dedup:
            break
        ...  # 生成样本（见 4.3），随后 dedup.add(...) 并 break
```

三个细节值得逐个咀嚼：

- **`i > 0` 才洗牌**：`i = 0` 是保底最优组合，保持名次顺序（最优证明排 Solution 0）；其余组合洗牌内部顺序。
- **去重键是「集合」不是「序列」**：`tuple(sorted(...))` 把顺序信息抹掉，`(2, 0, 3, 1)` 与 `(0, 1, 2, 3)` 视为同一组合。因此枚举出的第二个组合 `(0,1,...,k-1)`（即 `i = 1`）无论怎么洗牌都会命中 `i = 0` 登记的键而被放弃——这是刻意为之的重复，`dedup` 一个集合就消化了。
- **递减循环的不可达分支**：内层 `for num_proofs_to_include in range(n_proofs_to_refine, 0, -1)` 字面上是「先试完整组合，用过就降级试更短前缀」，但两条出路——重复时 L240-241 的 `break`、生成后 L281 的 `break`——都在**第一轮迭代**就离开循环，所以 `num_proofs_to_include` 实际永远等于起始值 `n_proofs_to_refine`（候选不足 k 条时，切片越界安全地取到整个 `indices`，效果相同）。`k-1, k-2, ..., 1` 这些分支在当前代码里不可达；递减写法保留了「降级采样」的外形，若把 L240-241 的 `break` 改成 `continue`，降级才会真正生效。阅读时不要被字面误导——实际语义是「完整组合已用过 ⇒ 整个候选放弃」。

每题收尾统计（[inference/main.py:L283-L284](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L283-L284)）：`trials.append(len(dedup))`，早停或无候选的题记 0；这些数字最终在 [inference/main.py:L389](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L389) 汇总成 `Avg trials per statement` 打印，是观察「每题实际组合数」的现成指标。

#### 4.2.4 代码实践

**实践目标**：用纯 Python 验证组合枚举与去重的数量规律，确认「`i = 1` 必被跳过」。

**操作步骤**（示例代码）：

```python
import itertools

def enumerate_combinations(m, k, num_trials):
    combinations = [list(range(min(k, m)))] + \
        list(itertools.combinations(list(range(m)), min(k, m)))
    dedup, kept = set(), []
    for i, indices in enumerate(combinations):
        if len(dedup) == num_trials:
            break
        indices = list(indices)
        for num in range(k, 0, -1):
            key = tuple(sorted(indices[:num]))
            if key in dedup:
                break
            dedup.add(key); kept.append(key)
            break
    return kept

print(len(enumerate_combinations(6, 4, 16)))   # 场景 A：签名默认值风格
print(len(enumerate_combinations(32, 1, 32)))  # 场景 B：run.sh 竞赛配置
print(enumerate_combinations(6, 4, 16)[:3])    # 看前三个组合
```

**需要观察的现象**：场景 A 输出 `15`（= \(\binom{6}{4}\)，保底组合吸收掉重复的 `(0,1,2,3)` 后 `i=1` 也被跳过，`num_trials=16` 没凑满）；场景 B 输出 `32`（= \(\binom{32}{1}\)，恰好用满）。

**预期结果**：`15` 与 `32`；前三个组合为 `(0,1,2,3), (0,1,2,4), (0,1,2,5)`（字典序）。以上为静态推演，待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：候选证明共 10 条，`n_best_proofs_to_sample=32`、`n_proofs_to_refine=1`、`n_agg_trials=16`。该题最多生成几个组合？为什么凑不满 16？

**答案**：10 个。`m = min(32, 10) = 10`，大小为 1 的组合上限 \(\binom{10}{1} = 10 < 16\)；枚举列表耗尽前 `dedup` 只能积累到 10 个键（保底 `(0,)` 与枚举出的 `(0,)` 重复、后者被跳过）。`num_trials` 是上限而非目标值。

**练习 2**：把 L240-241 的 `break` 改成 `continue`，行为会怎样变化？

**答案**：降级分支被激活。当某个候选的完整 k 前缀已在 `dedup` 中，程序不再放弃整个候选，而是继续尝试 `k-1` 长度的前缀、再不行试 `k-2`……于是同一候选可能产出「更短的小组合」样本，`dedup` 的键集合将包含多种长度。这会让每题的组合数更贴近 `num_trials`，但也会引入「只含 1 条证明」的低信息组合（在 `n_proofs_to_refine > 1` 的配置下）。这是行为改动而非纯优化，需重新评估精炼质量。

**练习 3**：为什么保底组合 `(0, 1, ..., k-1)` 不参与 `np.random.shuffle`？

**答案**：`i = 0` 不洗牌（L237 的 `if i > 0`），保证最优的 k 条证明至少有一次以「名次从高到低」的确定顺序进入摘要——Solution 0 永远是验证器最认可的证明。这给每题提供了一个信息结构固定的锚点请求，其余请求再提供随机化的视角。

### 4.3 分数桶评价采样与摘要拼装

#### 4.3.1 概念说明

组合确定了「哪几条证明」进入精炼请求；摘要拼装解决「每条证明带哪些批语」。一条证明可能被验证过几十次（竞赛配置 `n_verification_per_proof=64`），把所有批语塞进提示词既贵又淹没重点。采样策略按**分数桶**分层：

- 每条证明的批语先按评分（0 / 0.5 / 1）分桶（u5-l1 已建立的 `score2ratings`）；
- 只有一个桶时（所有验证者同档），最多取 8 条；有多个桶时，每桶最多 `max_rating_per_score` 条（主循环硬编码 4，[inference/main.py:L442](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L442)）；
- 桶间按分数**升序**遍历，桶内随机洗牌后截取——低分（挑毛病的）批语优先入摘要，同一桶内随机选代表。

#### 4.3.2 核心流程

```text
对组合中的每个证明 idx：
    score2ratings 键统一转 float（旧池 JSON 读回的是字符串 "0.5"，新证明是 0.5）
    scores = 升序排列的分数档列表          # 如 [0.0, 0.5, 1.0]
    max_rating = 8           若只有一档
               = max_rating_per_score(=4)  否则
    ratings = []
    for score in scores:                  # 低分档在前
        洗牌该桶
        取前 max_rating 条，逐条编号追加   # "=== Evaluation j of Solution i ==="
        若 len(ratings) == 8: 提前结束     # 每证明总量封顶 8
    summary.append("--- Solution i ---\n{proof}\n\n{ratings 用空行连接}")
组合内多个证明之间用三个空行连接 ──> summary 字符串
渲染 proof_refinement 模板（instruction=整套生成提示词, proofs_to_refine=summary）
```

多桶时每证明的评价条数上限是 \(\min(8,\; 4 \times \text{桶数})\)：两桶 8 条、三桶 12 条被 8 封顶。

#### 4.3.3 源码精读

采样上限的判定（[inference/main.py:L247-L252](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L247-L252)）：

```python
score2ratings = {float(key): val for key, val in score2ratings.items()}
scores = sorted(list(score2ratings.keys()))
if len(scores) == 1:
    max_rating = 8
else:
    max_rating = max_rating_per_score
```

`float(key)` 的统一转换是兼容新旧两种来源：本轮新证明的桶键本就是 float（评分经 `float(...)` 解析，[inference/main.py:L332](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L332)），而旧池记录从 JSON 反序列化后键变成字符串（JSON 的键只能是字符串）——u5-l1 提过的「落盘后桶键由 float 变字符串」在此处被消化。L255 的 `assert isinstance(score, float)` 是对转换的即时自检。

桶间遍历与总量封顶（[inference/main.py:L253-L262](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L253-L262)）：`sorted` 使 0 分桶先于 1 分桶被消费，批评意见优先进入摘要；`np.random.shuffle` 让同档批语随机选出代表；计数到 8 立即 `break`（注意只跳出桶间 `for`，不影响其他证明）。

```python
for score in scores:
    assert isinstance(score, float), score
    np.random.shuffle(score2ratings[score])
    for rating in score2ratings[score][:max_rating]:
        rating = rating['rating']
        ratings.append(f"=== Evaluation {len(ratings)} of Solution {len(summary)} ===\n{rating}")
        if len(ratings) == 8:
            break
ratings = "\n\n".join(ratings)
```

一个容易看走眼的小细节：f-string 在 `append` **之前**求值 `len(ratings)` 与 `len(summary)`，所以编号从 0 开始——第一条批语是 `=== Evaluation 0 of Solution 0 ===`，第二个证明是 `--- Solution 1 ---`。这与人类习惯的从 1 编号不同，但模板对编号没有语义要求，只是占位标记。

证明块与摘要的拼装（[inference/main.py:L263-L264](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L263-L264)）：每个证明块是「标题 + 证明正文 + 批语列表」，证明之间用三个空行分隔，比批语之间的两个空行更宽，视觉上区分「证明边界」与「批语边界」。

```python
summary.append(f"--- Solution {len(summary)} ---\n{proof}\n\n{ratings}")
summary = "\n\n\n".join(summary)
```

最终渲染（[inference/main.py:L265-L273](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L265-L273)）：`instruction` 不是简短指令，而是**整套渲染后的 `proof_generation` 提示词**（含题面与输出格式契约）；`proofs_to_refine` 是刚拼好的摘要。两者嵌进 `proof_refinement` 模板（[inference/math_templates.py:L203-L213](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/math_templates.py#L203-L213)）——这正是 u3-l1 讲过的「组合式模板」：`{instruction}` 内嵌整份生成提示，末尾追加 `## Candidate Solution(s) to Refine` 与最终格式要求。

```python
msg = [{
    'role': 'user',
    'content': math_templates[args.proof_refine_template].format(
        instruction=math_templates[args.proof_gen_template].format(question=problem.strip()).strip(),
        proofs_to_refine=summary.strip()
    )
}]
```

样本落盘（[inference/main.py:L274-L281](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L274-L281)）：登记去重键后，`deepcopy(item)` 复制原始题目记录（question、problem_idx、source_name 等），只覆盖 `messages` 与 `dep_proof_ids`——后者是所选证明的 `proof_id` 按摘要顺序排列，下一轮这些证明的后代入池时将以此追溯谱系（u5-l1 的 DAG）。注意 `dep_proof_ids` 在这里**重新计算**（L243-L246 收集的是组合内各证明的 id），候选元组第 6 位的旧谱系字段并不直接透传；L245 的 `[:5]` 截取正是为了兼容 5 元组的旧池记录。

#### 4.3.4 代码实践

**实践目标**：验证双层采样上限与「低分桶优先」的顺序。

**操作步骤**（示例代码）：

```python
import numpy as np

np.random.seed(0)
# 一条证明的三桶批语：0 分 5 条、0.5 分 3 条、1 分 6 条
score2ratings = {0.0: [{'rating': f'bad-{i}', 'score': 0.0} for i in range(5)],
                 0.5: [{'rating': f'half-{i}', 'score': 0.5} for i in range(3)],
                 1.0: [{'rating': f'good-{i}', 'score': 1.0} for i in range(6)]}
max_rating_per_score = 4

scores = sorted(score2ratings.keys())          # [0.0, 0.5, 1.0]
max_rating = 8 if len(scores) == 1 else max_rating_per_score
ratings = []
for score in scores:
    np.random.shuffle(score2ratings[score])
    for r in score2ratings[score][:max_rating]:
        ratings.append(r['rating'])
        if len(ratings) == 8:
            break
print(len(ratings), ratings)
```

**需要观察的现象**：输出 8 条批语；顺序是 0 分桶 4 条 → 0.5 分桶 3 条 → 1 分桶 1 条（4+3+1=8 触发封顶，1 分桶只挤进 1 条）；各桶内部的具体条目随种子变化。

**预期结果**：`8`，且 `ratings` 前四条均为 `bad-*`、中间三条 `half-*`、最后一条 `good-*`。若把 1.0 桶删掉（只剩两桶），结果仍是 8 条（4+4）；若删到只剩一桶，`max_rating` 变 8，5 条全进。待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：一条证明的验证评分分布为 32 个 1 分（单桶）。摘要会带几条批语？如果分布是 16 个 0 分 + 16 个 1 分（两桶）呢？

**答案**：单桶 32 条 1 分 ⇒ `max_rating=8`，桶内洗牌后取 8 条。两桶 ⇒ 每桶 4，0 分桶 4 条先入、1 分桶 4 条后入，共 8 条。两种分布条数相同，但后者保留了对证明的正面与负面评价的平衡样本，前者全是好评（对精炼的价值反而低——这条证明均值不低，通常不会是重点精炼对象）。

**练习 2**：把 `max_rating_per_score` 从 4 调到 2，摘要会发生什么变化？什么时候这个调优有意义？

**答案**：多桶证明的批语上限从 \(\min(8, 4 \times 桶数)\) 降到 \(\min(8, 2 \times 桶数)\)，三桶时从 8 条降到 6 条，摘要变短、请求变便宜，但每档批评意见的覆盖变薄。当证明普遍已有大量验证（`n_verification_per_proof` 大）且 API 按 token 计费成为瓶颈时，下调它可以在 `num_trials` 不变的前提下压缩成本；代价是精炼模型看到的「同档不同角度」的批评变少。

**练习 3**：为什么桶间要按分数**升序**而不是降序遍历？

**答案**：升序让 0 分（指出致命缺陷）的批语最先进入、最可能被完整保留；若按降序，高分表扬先占名额，批评意见容易在触到 8 条上限后被挤掉。精炼的目标是「修复问题」，缺陷描述比正面肯定信息价值更高（meta_verification 模板同理规定「正面肯定不在评审范围」，见 u4-l3）。

### 4.4 _split_jobs：把任务切成约 50 块并行处理

#### 4.4.1 概念说明

以上三节是**单题**的聚合算法。竞赛有几十上百道题，串行跑会很慢，`prepare_proof_refinement` 用 `multiprocessing.Pool` 并行处理。并行的前提是把题目列表切块——`_split_jobs` 就是那个不起眼但值得精读的切分函数。它只有 8 行，却有一个容易误读的参数语义：`nsplit` 是「期望的**块数**」，不是「每块的**大小**」。

#### 4.4.2 核心流程

```text
_split_jobs(jobs, nsplit):
    若 len(jobs) < nsplit: 返回 [jobs]            # 太少就不切
    sz = ceil(len(jobs) / nsplit)                  # 每块大小
    按 sz 步长切片，返回 ceil(len / sz) 块          # 块数 ≈ nsplit
```

块数与块大小的换算：

\[ \text{sz} = \left\lceil \frac{n}{\text{nsplit}} \right\rceil, \qquad \text{块数} = \left\lceil \frac{n}{\text{sz}} \right\rceil \le \text{nsplit} \]

调用点（[inference/main.py:L385](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L385)）传的是 `_split_jobs(tasks, 50)`：

| 题目数 n | sz = ceil(n/50) | 实际块数 = ceil(n/sz) | 每块题数 |
|---|---|---|---|
| 30 | 不切（<50） | 1 | 30 |
| 50 | 1 | 50 | 1 |
| 60 | 2 | 30 | 2 |
| 100 | 2 | 50 | 2 |
| 1000 | 20 | 50 | 20 |

可见「按 50 一组切」是常见误读——1000 题时每块是 20 题而非 50 题；准确说法是「切成约 50 块」。

#### 4.4.3 源码精读

切分函数本体（[inference/main.py:L156-L163](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L156-L163)）：

```python
def _split_jobs(jobs, nsplit):
    if len(jobs) < nsplit:
        return [jobs]
    res = []
    sz = math.ceil(len(jobs) / nsplit)
    for i in range(0, len(jobs), sz):
        res.append(jobs[i: i + sz])
    return res
```

并行消费（[inference/main.py:L382-L387](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L382-L387)）：`partial` 把 7 个聚合参数固化后，`pool.imap` 让 `cpu_count()` 个 worker 进程逐块领取任务。每个 worker 进程内的 `np.random.shuffle` 相互独立地作用于各自块的数据。

```python
pool = multiprocessing.Pool(cpu_count())
for (_data, _trials) in tqdm(pool.imap(partial(_prepare_proof_agg_tasks, **_args), _split_jobs(tasks, 50))):
    data.extend(_data)
    trials.extend(_trials)
```

块粒度的选择是平衡的结果：块太少（比如 1 题 1 块）则进程间通信与任务调度开销占比高；块太多（比如全切 1 块）则并行度归零。约 50 块 × `cpu_count` 个 worker，在常见多核机器上既能喂饱 CPU，又不会让单块过大造成负载不均（各题证明数差异很大，块内题目多一些可以让块间工作量更均匀）。

另外注意：worker 内部对证明池文件的写入是「一题一文件」的追加（u5-l1 讲过并行无锁的依据），`_split_jobs` 按题目边界切块恰好保证了同一题不会出现在两个块中——否则两个进程同时追加同一题的 jsonl 就会破坏账本。切分函数与证明池的并发安全是配套设计。

#### 4.4.4 代码实践

**实践目标**：验证块数公式，并确认「同题不跨块」。

**操作步骤**（示例代码，也可以直接在项目根目录 `python -c "..."` 引用真实函数——`main.py` 顶层有 argparse 与两处属性访问，直接 import 会失败，需先按 u1-l3 补参数；这里用独立副本最省事）：

```python
import math

def _split_jobs(jobs, nsplit):
    if len(jobs) < nsplit:
        return [jobs]
    res = []
    sz = math.ceil(len(jobs) / nsplit)
    for i in range(0, len(jobs), sz):
        res.append(jobs[i: i + sz])
    return res

for n in [30, 50, 60, 100, 1000]:
    chunks = _split_jobs(list(range(n)), 50)
    sizes = [len(c) for c in chunks]
    flat = [x for c in chunks for x in c]
    print(f"n={n:5d} 块数={len(chunks):3d} 首块大小={sizes[0]:3d} "
          f"无重叠无损={flat == list(range(n))}")
```

**需要观察的现象**：输出与 4.4.2 表格逐行一致；`无重叠无损=True` 说明切块是原列表的精确划分。

**预期结果**：`n=30 → 1 块`、`n=50 → 50 块×1`、`n=60 → 30 块×2`、`n=100 → 50 块×2`、`n=1000 → 50 块×20`。

#### 4.4.5 小练习与答案

**练习 1**：`_split_jobs(jobs, 50)` 作用于 60 个任务，返回几块、每块几个？为什么不是 50 块也不是 2 块？

**答案**：`sz = ceil(60/50) = 2`，`range(0, 60, 2)` 产出 30 块、每块 2 个。不是 50 块（任务不够切成 50 个非空块且每块 ≥2）；不是「每块 50」的 2 块（那需要 sz=50，与 `ceil(60/50)=2` 矛盾）。函数目标是块数接近 `nsplit`，块大小由任务总数反推。

**练习 2**：如果把 `_split_jobs(tasks, 50)` 改成 `_split_jobs(tasks, 1)`，对正确性与性能各有什么影响？

**答案**：`sz = n`，返回 1 块，`pool.imap` 只有一个任务，并行完全失效（其余 worker 空闲），`Avg trials per statement` 等行为不变——正确性不受影响，因为算法本身不依赖并行；性能退化为单进程逐题聚合。

**练习 3**：为什么「同一题不能出现在两个块里」对正确性至关重要？

**答案**：`_prepare_proof_agg_tasks` 对每题执行「读证明池 → 追加新证明」的非原子序列（u5-l1）。若同一题被两个进程同时处理，两者的读池都看不到对方未落盘的新证明，`nxt_proof_id` 会各自从同一最大值递增，导致证明池出现重复 `proof_id`、谱系断言（L217）可能失败。`_split_jobs` 按题目边界整体切块，从调度上排除了这种竞态。

## 5. 综合实践

把本讲三个算法模块（排序筛选、组合去重、桶采样）串成一个可独立运行的 `toy_aggregation.py`，用假数据完整复刻 `_prepare_proof_agg_tasks` 后半段的行为，再通过两组参数对比观察配置如何塑造输出规模。

**实践目标**：

1. 在不依赖项目源码的前提下复刻算法，检验你对该流程的理解；
2. 量化 `n_best_proofs_to_sample` 与 `n_proofs_to_refine` 对组合数、摘要长度的影响。

**操作步骤**：

第一步，在任意目录创建 `toy_aggregation.py`（示例代码，逻辑忠实对应 [inference/main.py:L222-L284](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L222-L284)，摘要以「批语条数」代替全文以缩短输出）：

```python
import itertools
import numpy as np

def build_fake_proofs(n=10):
    """构造 n 个 5 元组候选：(proof, meanscore, score2ratings, self_eval, proof_id)"""
    tuples = []
    for i in range(n):
        score2ratings = {
            0.0:  [{'rating': f'P{i}-bad-{j}',  'score': 0.0} for j in range(2)],
            0.5:  [{'rating': f'P{i}-half-{j}', 'score': 0.5} for j in range(1)],
            1.0:  [{'rating': f'P{i}-good-{j}', 'score': 1.0} for j in range(1)],
        }
        tuples.append((
            f'fake proof #{i}',
            round(0.95 - 0.07 * i, 2),                     # meanscore 互不相同且 < 0.99999
            score2ratings,
            {'self_eval': 'null', 'self_eval_score': (i * 3) % 5 / 4},  # 自评分 0~1
            i + 1,                                          # proof_id 从 1 递增
        ))
    return tuples

def aggregate(tuples, num_trials, n_best_proofs_to_sample,
              n_proofs_to_refine, max_rating_per_score=4, seed=0):
    np.random.seed(seed)
    if any(rec[1] > 0.99999 for rec in tuples):            # L222-L223 早停
        return []
    for _ in range(10):                                     # L225-L226 洗牌
        np.random.shuffle(tuples)
    tuples = sorted(tuples,                                 # L227 双键排序截断
                    key=lambda x: (x[1], x[3]['self_eval_score']),
                    reverse=True)[:n_best_proofs_to_sample]
    combinations = [list(range(min(n_proofs_to_refine, len(tuples))))] + \
        list(itertools.combinations(                        # L228-L229 组合枚举
            list(range(min(n_best_proofs_to_sample, len(tuples)))),
            min(n_proofs_to_refine, len(tuples))))
    if not tuples:                                          # L230-L231 空保护
        combinations = []
    dedup, results = set(), []
    for i, indices in enumerate(combinations):              # L233 枚举
        if len(dedup) == num_trials:
            break
        indices = list(indices)
        if i > 0:
            np.random.shuffle(indices)                      # L237-L238 随机化顺序
        for num in range(n_proofs_to_refine, 0, -1):        # L239 递减前缀
            if tuple(sorted(indices[:num])) in dedup:
                break
            chosen, n_ratings = [], 0
            for idx in indices[:num]:                       # L244 摘要拼装
                proof, _, score2ratings, _, proof_id = tuples[idx][:5]
                score2ratings = {float(k): v for k, v in score2ratings.items()}
                scores = sorted(score2ratings)
                max_rating = 8 if len(scores) == 1 else max_rating_per_score
                cnt = 0
                for score in scores:
                    np.random.shuffle(score2ratings[score])
                    for _r in score2ratings[score][:max_rating]:
                        if cnt == 8:      # 每证明独立封顶 8（对应源码 L260-L261）
                            break
                        cnt += 1
                n_ratings += cnt
                chosen.append(proof_id)
            dedup.add(tuple(sorted(indices[:num])))         # L274 登记
            results.append((tuple(chosen), n_ratings))
            break
    return results

if __name__ == '__main__':
    for label, kw in [
        ('基准(签名默认风格): n_best=6,  k=4, trials=16',
         dict(num_trials=16, n_best_proofs_to_sample=6,  n_proofs_to_refine=4)),
        ('修改(竞赛风格):     n_best=32, k=1, trials=16',
         dict(num_trials=16, n_best_proofs_to_sample=32, n_proofs_to_refine=1)),
    ]:
        res = aggregate(build_fake_proofs(10), seed=0, **kw)
        print(f'\n=== {label} ===')
        print(f'组合数 = {len(res)}')
        for combo, n_ratings in res:
            print(f'  proof_id 组合 = {combo}, 摘要批语条数 = {n_ratings}')
        print(f'平均批语条数 = {sum(r[1] for r in res) / len(res):.1f}')
```

第二步，运行 `python toy_aggregation.py`。

第三步，记录两组配置的输出，填入下面的对比表。

**需要观察的现象**：

| 观察项 | 基准 n_best=6, k=4 | 修改 n_best=32, k=1 |
|---|---|---|
| 参与排序的证明数 | 6（10 条中截断） | 10（32 截不动 10 条） |
| 组合数 | 预期 15（= \(\binom{6}{4}\)，`i=1` 与保底重复被跳过） | 预期 10（= \(\binom{10}{1}\)） |
| 每组合证明数 | 4 | 1 |
| 每组合批语条数 | 每证明三桶 2+1+1=4 条（未触 8 上限），4 证明组合共 16 条 | 单证明 4 条 |
| `dep_proof_ids`（输出元组） | 每组合 4 个 id | 每组合 1 个 id |

**预期结果**（静态推演，待本地验证；固定 `seed=0` 后可复现）：

- 基准配置组合数 15：保底 `(0,1,2,3)` 先入，枚举出的 `(0,1,2,3)`（`i=1`）命中去重被放弃，其余 14 个字典序组合全部入选，凑不满 16；
- 修改配置组合数 10：全部为单证明组合，id 集合恰为排序后前 10 名各一次；
- 批语条数远小于「全部批语」——10 条假证明共有 4×10=40 条批语，单个 4 证明组合的摘要只带 16 条（每证明 4 条），体现了桶采样上限；
- 摘要总文本量：基准配置 15 组合 × 16 条 = 240 条批语，修改配置 10 组合 × 4 条 = 40 条，约 6 倍——这就是 `n_proofs_to_refine` 对 token 成本的主要放大效应。

**记录结论**（供你对照自己的运行输出后填写）：

1. `n_best_proofs_to_sample` 只有在候选证明多于它时才起「截断」作用；候选不足时形同虚设（修改配置 10 < 32 即如此）；
2. `n_proofs_to_refine` 同时放大两个维度：单组合摘要长度（≈ k × 单证明批语数）与组合多样性上限 \(\binom{m}{k}\) 的凹增长；
3. `num_trials` 是「每题组合数」的天花板，实际值 = \(\min(num\_trials, \binom{m}{k})\)；
4. 每题下一轮的生成请求数 ≈ 实际组合数 × `n_parallel_proof_gen // n_agg_trials`（竞赛配置下 32 × 4 = 128）。

## 6. 本讲小结

- **满分早停**：候选列表（含并入的旧池证明）中任一证明 `meanscore > 0.99999`（等价于全部验证评分 1 分）即跳过该题，历史任一轮的满分证明都会让该题永久停止精炼。
- **双键排序筛选**：先洗牌十次打破稳定排序的并列顺序，再按 `(meanscore, self_eval_score)` 降序取前 `n_best_proofs_to_sample` 条——外部分（验证器均分）为主、内部分（生成器自评）为辅。
- **组合枚举去重**：保底最优组合 + `itertools.combinations` 字典序枚举，`tuple(sorted(前缀下标))` 兼任去重键与计数器，凑满 `num_trials` 即停；递减循环的字面降级分支在当前 `break` 语义下不可达。
- **桶采样摘要**：批语按 0/0.5/1 分桶，单桶上限 8、多桶每桶 `max_rating_per_score`（=4）且每证明总量封顶 8；低分桶优先，编号从 0 起；`instruction` 是整套渲染后的生成提示词。
- **算力守恒**：R≥2 时每题生成请求数 ≈ `n_agg_trials × (n_parallel_proof_gen // n_agg_trials)`，竞赛配置 \(32 \times 4 = 128\)。
- **`_split_jobs`**：按 `ceil(n/50)` 的块大小切成约 50 块交给进程池，「同题不跨块」是证明池并行追加写入的安全前提。

## 7. 下一步学习建议

本讲搞定了「单题如何从证明池生成精炼请求」，但这些请求还只是 `proof_gen_R{R}/input.jsonl` 里的静态数据。下一讲 **u5-l3《精炼输入构建：prepare_proof_refinement 的数据汇合》**将补全外层容器：`rating2quality` 如何从元验证输出建立映射、扁平的验证输出如何按「问题→证明→评价」两级聚合成本讲的输入四元组、以及 `multiprocessing.Pool` 如何把本讲的 `_prepare_proof_agg_tasks` 组织成流水线。建议提前回顾 u4-l3 的 `rating` 字段语义（它是 `rating2quality` 的字符级 join 主键），再带着一个问题读 u5-l3：本讲 4.3 里采集的批语如果曾被元验证判为低质量，有没有被过滤掉？（提示：注意 `quality` 字段存进了 `score2ratings` 但摘要循环只读取 `rating['rating']`——这个悬念将在 u5-l3 与 u6-l1 中进一步讨论。）
