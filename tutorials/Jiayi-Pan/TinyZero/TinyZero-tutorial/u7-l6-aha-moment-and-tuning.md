# R1 Zero "Aha" 现象与调参解读

## 1. 本讲目标

本讲是整本学习手册的收尾篇。前面你已经读完了数据、调度、PPO 主循环、core_algos 数学、各 Worker 实现以及测试调试。本讲不再讲新的代码机制，而是**退后一步**，回答三个「为什么」：

1. 为什么完全跳过 SFT、只用规则奖励做纯 RL，一个 3B 的基座模型会**自发涌现**自我验证与搜索能力（即 R1 Zero 的"Aha moment"）？
2. 为什么奖励函数里那个不起眼的 `format_score=0.1` 既可能是「起步的梯子」、又可能是「奖励黑客的温床」？
3. 为什么 `kl_coef=0.001` 这根细细的缰绳，是防止策略跑偏、保住基座模型语言能力的关键？

学完本讲，你应当能够：

- 用「稀疏结果奖励 + 策略梯度放大正确轨迹」解释推理能力的涌现机制；
- 独立分析 `format_score` 调高/调低对训练动力学的影响，并能诊断「只刷格式」这类奖励黑客；
- 结合 `apply_kl_penalty` 与监控指标，理解 KL 约束如何把策略拴在基座模型附近；
- 看着 `response_length/mean` 与 `critic/score/mean` 两条曲线**同时上升**，判断小尺度下是否出现了"Aha moment"。

---

## 2. 前置知识

本讲默认你已经读过以下讲义（或掌握其中结论），不会重复讲解其细节：

- **u2-l4 规则奖励函数**：`compute_score` 是把模型回答转成分数的确定性函数；奖励是**稀疏**的（只在回答末位有效 token 放一个标量）。
- **u4-l3 fit() 主循环**：一个训练 step 是「生成 → 算奖励 → 算优势 → 更新」。
- **u5-l1 KL 惩罚与优势**：`token_level_rewards = token_level_scores - β·kld`；`score`（任务分）与 `reward`（含 KL 罚的奖励）是两回事。
- **u5-l4 KL 控制器**：TinyZero 默认走 **reward 端、fixed** 的 KL 路线，β 由 `kl_coef` 给定（默认 `0.001`）。
- **u7-l5 测试调试与跟踪**：每 step 的指标由 `compute_data_metrics` 产出，经 `reduce_metrics` 取均值后写入 wandb。

几个关键术语再快速对齐：

| 术语 | 含义 |
|------|------|
| **R1 Zero 路线** | 完全跳过 SFT，直接在基座模型上做 RL，只用规则奖励当唯一学习信号 |
| **Aha moment（顿悟时刻）** | 模型自发涌现自我验证（self-verification）与搜索（search）行为，伴随回答变长、奖励上升 |
| **奖励塑形（reward shaping）** | 在稀疏的正确性奖励之外，额外给「格式正确」等中间行为一个小分，提供起步梯度 |
| **奖励黑客（reward hacking）** | 模型找到了提高分数但不真正解决问题的捷径（如只产出格式、绕过校验） |
| **结果奖励（outcome reward）** | 只看最终答案对不对，不看推理过程——countdown 就是典型 |

---

## 3. 本讲源码地图

本讲聚焦的源码很少，但每一处都对应一个调参旋钮：

| 文件 | 作用 | 对应本讲模块 |
|------|------|--------------|
| [verl/utils/reward_score/countdown.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/reward_score/countdown.py) | countdown 的三级打分函数 `compute_score` | 4.1 / 4.2 |
| [scripts/train_tiny_zero.sh](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/scripts/train_tiny_zero.sh) | 训练入口脚本，承载 `kl_coef`、`max_response_length` 等超参覆盖 | 4.3 / 4.4 |
| [verl/trainer/config/ppo_trainer.yaml](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/config/ppo_trainer.yaml) | `kl_ctrl` 默认配置（`type: fixed`, `kl_coef: 0.001`） | 4.3 |
| [verl/trainer/ppo/ray_trainer.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py) | `apply_kl_penalty`（reward 端 KL）与 `compute_data_metrics`（监控指标） | 4.3 / 4.4 |
| [verl/trainer/ppo/core_algos.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/core_algos.py) | `kl_penalty` 四种 KL 估计 | 4.3 |
| [docs/experiment/ppo.rst](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/docs/experiment/ppo.rst) | 不同基座模型的 PPO 基线对比表 | 4.4 |
| [README.md](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/README.md) | 项目定位与 3B / 0.5B 的能力差异说明 | 4.4 |

---

## 4. 核心概念与源码讲解

### 4.1 从规则奖励到能力涌现：countdown.compute_score 的三级打分

#### 4.1.1 概念说明

R1 Zero 最反直觉的一点是：**我们从不告诉模型「怎样推理」，只告诉它「答对了给 1 分」**。countdown 任务尤其适合这一点——给定一个目标数 `target` 和一组可用 `numbers`，要求用算术运算凑出目标，结果对不对可以精确、即时、自动地判出来（参见 u2-l1）。这种「只看结果、不看过程」的奖励叫**结果奖励（outcome reward）**。

`compute_score` 就是这个判分器：它把模型生成的一整段文本（含 `<think>` 推理与 `<answer>` 答案）压缩成一个标量分数。整个 RL 训练的唯一学习信号，就来自这一个函数。

关键问题随之而来：如此稀疏的信号（一个标量 / 一条轨迹），凭什么能让模型学会「先推理、再验证、再搜索」？答案是**策略梯度的放大效应**——在 PPO/GRPO 下，那些「恰好推理对了」的轨迹会获得正优势并被强化，而推理能力更强的样本被反复采样、反复强化，行为分布就慢慢向「会推理」偏移。当模型容量足够（3B，见 4.4），这种偏移会越过某个临界点，表现为回答突然变长、出现「等等，让我重新算一遍」之类的自验证行为，这就是小尺度下的"Aha moment"。

#### 4.1.2 核心流程

`compute_score` 走「提取 → 校验 → 求值 → 分级打分」四步，最终输出三级之一：

```
solution_str（整段生成文本）
   │
   ├─ extract_solution：定位最后一个 <answer>...</answer>，取出里面的等式
   │      取不到 → 返回 0
   │
   ├─ validate_equation：等式里出现的数字是否与题目给定 numbers 完全一致（防偷数字）
   │      不一致 → 返回 format_score（0.1）
   │
   ├─ evaluate_equation：受限 eval 安全求值
   │      求不出 → 返回 format_score（0.1）
   │
   └─ |result - target| < 1e-5 ？
          是 → 返回 score（1.0）
          否 → 返回 format_score（0.1）
```

三个返回值 `\{0,\ 0.1,\ 1.0\}` 构成了奖励的三个台阶，其中 `0.1` 就是下一节要重点讨论的「塑形奖励」。

#### 4.1.3 源码精读

函数签名里直接写死了两个关键常量：`format_score=0.1` 与 `score=1.`：

[verl/utils/reward_score/countdown.py:59-70](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/reward_score/countdown.py#L59-L70) —— `compute_score` 入口，取出 `target` 与 `numbers`，并调用 `extract_solution`。

三个出口对应三级打分：

- [countdown.py:81-84](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/reward_score/countdown.py#L81-L84) —— 提取不到任何 `<answer>` 等式，返回 `0`。
- [countdown.py:87-90](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/reward_score/countdown.py#L87-L90) —— 等式里的数字与题目不一致（防偷数字），返回 `format_score`。
- [countdown.py:100-107](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/reward_score/countdown.py#L100-L107) —— 等式可求值后，与 `target` 比对：相等返回 `score`（1.0），否则返回 `format_score`。

判等用的是 `abs(result - target) < 1e-5`，容忍浮点误差（如 `2.9999999` 视作 `3`）。

注意一个常被忽略的设计：[countdown.py:73-79](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/reward_score/countdown.py#L73-L79) 的 `do_print = random.randint(1, 64) == 1`。它以 1/64 概率随机抽样一条轨迹打印到日志，这是你在训练中**亲眼看到模型推理文本演化**的窗口——`random` 抽样保证不会刷屏，又能在长训练里凑齐足够样本供你观察"Aha moment"是否出现。

> 小结：`compute_score` 是整个涌现现象的物质基础——它定义了「什么算赢」。没有它，就没有梯度。

#### 4.1.4 代码实践（源码阅读型）

本实践**不运行训练**，而是直接调用打分函数，建立对三级打分的直觉。

1. **实践目标**：验证 `compute_score` 对三类输入分别返回 `1.0 / 0.1 / 0`。
2. **操作步骤**：在仓库根目录启动 Python，构造三种 `solution_str`：

```python
from verl.utils.reward_score.countdown import compute_score
gt = {"target": 24, "numbers": [3, 8, 1]}

# ① 完全正确：(8-1)*3 ... 不对，换个真能凑出 24 的：3*(8/... )
#    countdown 题目允许 + - * /，(8-1)*? ... 我们用 (1+8)*? 也不对。
#    真能凑 24 的例子： (3) * (8) = 24，但题目要求"每个数字恰好用一次"，
#    数字是 [3,8,1]，所以至少要三个都用上。一个合法凑法：(1+? )...
#    实际只要等式 = 24 且用了 {1,3,8} 即可，例如 3*8*1 = 24。
good   = "Assistant: <think>...</think> <answer>3*8*1</answer>"
# ② 格式正确、数字也对、但结果错
fmt    = "Assistant: <think>...</think> <answer>3+8+1</answer>"   # =12 ≠24
# ③ 完全没有 <answer> 标签
nolang = "Assistant: 我觉得答案是二十多。"
```

3. **需要观察的现象**：分别打印三个返回值。
4. **预期结果**：`compute_score(good, gt) == 1.0`、`compute_score(fmt, gt) == 0.1`、`compute_score(nolang, gt) == 0`。
5. **若结果不符**：检查 `extract_solution` 是否要求文本里先出现 `Assistant:`（它用 `split("Assistant:", 1)` 截断）；若你的字符串没有 `Assistant:`，会被判为「无等式」返回 0。**待本地验证**浮点凑数是否真等于 24。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `<answer>3*8*1</answer>` 改成 `<answer>3*8</answer>`（少用数字 1，结果也是 24），会得多少分？
**答案**：得 `format_score = 0.1`。因为 `validate_equation` 发现等式里的数字 `{3,8}` 与题目给定 `{1,3,8}` 不一致，即便结果正确也算「偷工减料」。

**练习 2**：为什么判等用 `abs(result - target) < 1e-5` 而不是 `==`？
**答案**：`eval` 对除法返回浮点（如 `24/1` 可能是 `23.9999...`），直接 `==` 会误判。1e-5 容忍这种浮点抖动。

---

### 4.2 format_score：塑形奖励的双刃剑

#### 4.2.1 概念说明

`format_score=0.1` 是一个典型的**奖励塑形（reward shaping）**手段。纯结果奖励的问题在于**冷启动**：训练初期模型几乎不可能凑出正确等式，于是奖励长期为 0、梯度接近 0、策略几乎不动——这叫「奖励稀疏导致的探索困境」。

`format_score` 给了一条「中间台阶」：即便答案错，只要模型**学会了 `<answer>` 的格式**，就给 0.1 分。这为策略梯度提供了一个非零的、稠密一些的信号，让模型先学会「把答案放进正确标签」，再慢慢学会「把答案算对」。

但它也是双刃剑——**塑形奖励越高，模型就越倾向于「只刷格式」而不真正解题**（reward hacking）。这是本讲实践任务的核心争议点。

#### 4.2.2 核心流程：三种 `format_score` 取值的行为对比

| `format_score` 取值 | 奖励台阶 | 行为倾向 | 风险 |
|---|---|---|---|
| `0.0`（设为 0） | `{0, 1.0}` 二元 | 奖励极稀疏 | 冷启动困难、梯度长期为 0；模型可能收敛到「早停输出」等退化行为 |
| `0.1`（默认） | `{0, 0.1, 1.0}` | 先学格式、再学解题 | 平衡——格式分远小于正确分，解题梯度占主导 |
| `≥ 0.5`（调太高） | `{0, ≥0.5, 1.0}` | 格式分与正确分接近 | **只刷格式**：模型发现「凑出格式」性价比极高，拒绝冒风险去解题 |

> 重要澄清：实践任务问「`format_score=0.1` 若设为 0 可能带来的『只刷格式』风险」。从机制上看需要分两层看——**真正诱发「只刷格式」的，是 `format_score` 被调得过高**（趋近 `score`）；而**把 `format_score` 从 0.1 降到 0，反而会消除这条奖励面**，但它引入的是另一种风险（奖励过稀、冷启动困难）。两者都是「调参失误」，只是方向相反。下一节的实践会分别推演。

#### 4.2.3 源码精读

塑形奖励的全部实现就是函数签名里那两个默认参数：

[verl/utils/reward_score/countdown.py:59](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/reward_score/countdown.py#L59) —— `format_score=0.1, score=1.` 把两个台阶直接写死。

调用方在 `main_ppo.py` 的 `_select_rm_score_fn` 里路由到本函数时，**没有覆盖这两个参数**（参见 u4-l1），所以全仓库统一使用 `0.1 / 1.0` 这组默认值。换句话说，要改塑形强度，只能改源码这一行（或改路由处显式传参），没有 yaml 配置项——这本身就是个提示：作者认为 `0.1` 是经过实验校准、不该轻易动的值。

注意 `format_score` 与 `score` 的**相对比例**而非绝对值才是关键：`0.1 / 1.0 = 10%`，意味着「凑对格式」最多只能拿到「凑对答案」十分之一的回报，不足以让模型满足于此。这正是 calibrate 的中间地带。

#### 4.2.4 代码实践（思想实验 + 源码阅读）

1. **实践目标**：推演 `format_score` 在三种取值下，模型奖励分布会如何变化，并定位「只刷格式」的临界点。
2. **操作步骤**：
   - 读 [countdown.py:59](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/reward_score/countdown.py#L59)，确认默认 `format_score=0.1`。
   - 设想一个**只会刷格式**的策略 `π_hack`：它总是输出 `Assistant: <answer>0+0</answer>`（格式正确、但既用错数字也算错）。它在默认配置下每条只得 `0.1`。
   - 再设想一个**偶尔解对**的策略 `π_solve`：50% 概率解对得 `1.0`，50% 概率格式对但错得 `0.1`，期望分 `0.55`。
3. **需要观察的现象**：比较 `π_hack` 与 `π_solve` 的期望分。
4. **预期结果**：默认 `format_score=0.1` 时，`π_solve`（0.55）远高于 `π_hack`（0.1），策略梯度会淘汰黑客。但若把 `format_score` 改成 `0.6`，则 `π_hack` 期望 `0.6 > π_solve` 的 `0.55`——**此时只刷格式反而更划算，黑客策略会被强化**。这就是「只刷格式」的真实成因。
5. **关于「设为 0」**：把 `format_score` 设为 0 后，`π_hack` 得分降为 0，黑客被彻底消灭；但代价是训练初期几乎所有轨迹都得 0 分（如上节所述），需要更强的探索（更大的 `rollout.n` 或更激进采样）才能冷启动。**待本地验证**你的 3B 模型在 `format_score=0` 下能否仍然涌现。

#### 4.2.5 小练习与答案

**练习 1**：countdown 的 `validate_equation` 校验「数字必须与题目完全一致」，这如何抑制一类奖励黑客？
**答案**：它挡住了「直接把 `target` 写进 `<answer>`」这种作弊——比如题目要凑 24，模型若输出 `<answer>24</answer>`，等式里没有可用数字，`validate_equation` 判定不一致，只给 `format_score`，不给满分。

**练习 2**：如果把 `format_score` 调到与 `score` 相等（都 1.0），会发生什么？
**答案**：奖励退化为「只要格式对就满分」，模型完全没有解题动力，会塌缩成「永远只输出格式外壳」的黑客策略。这印证了塑形奖励必须显著小于正确奖励。

---

### 4.3 kl_coef=0.001 与 reward 端 KL：拴住策略的缰绳

#### 4.3.1 概念说明

纯 RL 训练有个隐患：策略梯度只关心「提高奖励」，只要能涨分，模型可以学出任何行为——包括生成乱码、复读、或者为了凑分把语言能力彻底丢掉（即「策略漂移」）。尤其在小模型 + 长训练下，策略很容易**跑离**基座模型赖以泛化的那个概率分布，一旦离开，模型连「说人话」都成问题，更别提推理。

KL 约束就是拴住策略的缰绳：它在奖励里加一项**惩罚**，惩罚当前策略与**冻结的参考策略（ref policy，即未训练的基座模型）**之间的 KL 散度。策略越偏离基座，惩罚越大，奖励被扣得越多。于是模型在「涨分」和「别跑太远」之间权衡，既学到了任务，又保住了基座的语言底子。

`kl_coef`（β）就是这根缰绳的松紧。TinyZero 默认 `β=0.001`，非常温和——足以防止灾难性漂移，又不至于把模型死死按在基座上动不了。

#### 4.3.2 核心流程与数学

reward 端 KL 在 `apply_kl_penalty` 里实现（详见 u5-l1）。核心一行：

\[ r_t = s_t - \beta \cdot d_t \]

其中：
- \(s_t\) 是 `token_level_scores`（任务分，只在回答末位有效 token 非零，来自 `compute_score`）；
- \(\beta =\) `kl_coef`（默认 `0.001`）；
- \(d_t\) 是 token 级 KL 估计。

默认 `kl` 估计器是朴素无偏估计（采样自当前策略 \(\pi_\theta\)）：

\[ d_t = \log \pi_\theta(a_t) - \log \pi_{\text{ref}}(a_t) \]

它是 \(\mathrm{KL}(\pi_\theta \| \pi_{\text{ref}})\) 的单样本无偏估计，但**可以为负**（当 \(\pi_\theta\) 对某 token 反而比 ref 概率低时），因此方差较大；这是 `core_algos.kl_penalty` 提供 `abs / mse / low_var_kl` 等变体的原因（详见 u5-l4）。

整体效果：每个 token 的奖励都被「偏离 ref 的程度」削掉一点，偏离越大削得越多，把策略拉回 ref 附近。

> 注意 TinyZero 走的是 **reward 端 fixed KL**（β 恒定 0.001），而非 GRPO 的 **loss 端 KL**（`use_kl_loss=True` 把 KL 直接加进策略损失）。两者互斥，详见 u5-l4 / u5-l5。

#### 4.3.3 源码精读

**① β 从哪来**：`train_tiny_zero.sh` 覆盖了 yaml 默认值——

[scripts/train_tiny_zero.sh:21](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/scripts/train_tiny_zero.sh#L21) —— `algorithm.kl_ctrl.kl_coef=0.001`。

而 yaml 默认配置（被脚本「确认」而非改写，因为值相同）声明了控制器类型：

[verl/trainer/config/ppo_trainer.yaml:143-145](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/config/ppo_trainer.yaml#L143-L145) —— `kl_ctrl.type: fixed`、`kl_coef: 0.001`。`fixed` 意味着 β 恒定不随训练自适应（对比 `adaptive` 会按当前 KL 误差调 β，见 u5-l4）。

**② β 如何作用**：`apply_kl_penalty` 把任务分减去 KL 罚：

[verl/trainer/ppo/ray_trainer.py:84-113](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L84-L113) —— reward 端 KL 的全部实现。

关键几行：

- [ray_trainer.py:94-95](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L94-L95) —— 用默认 `kl` 估计算 `kld = old_log_probs - ref_log_prob`，再乘 `response_mask`（只在回答有效 token 上计罚，pad 位不算）。
- [ray_trainer.py:102](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L102) —— `token_level_rewards = token_level_scores - beta * kld`，即上面那条公式。
- [ray_trainer.py:111](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L111) —— 把当前 batch 的 `current_kl` 与 `beta` 作为指标 `critic/kl`、`critic/kl_coeff` 上报，供你监控策略漂移程度。

**③ KL 估计器**：

[verl/trainer/ppo/core_algos.py:253-254](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/core_algos.py#L253-L254) —— 默认分支 `return logprob - ref_logprob`，即朴素的 \(\log\pi_\theta - \log\pi_{\text{ref}}\)。

> 一个常被忽视的细节：`apply_kl_penalty` 在没有 ref policy 时（`ref_log_prob` 不在 batch 里）会令 `beta = 0`、`kld = 0`（[ray_trainer.py:98-100](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L98-L100)）。也就是说「缰绳」的前提是建了 ref policy；TinyZero 默认建 ref，所以缰绳始终在。

#### 4.3.4 代码实践（数值手算）

1. **实践目标**：手算一个 token 的 `token_level_rewards`，体会 β 的「松紧」。
2. **操作步骤**：假设某条轨迹回答只有 1 个有效 token，且它恰好是末位放分 token：
   - 任务分 `s = 1.0`（答对）；
   - 该 token 上 `old_log_prob = -2.0`、`ref_log_prob = -1.5`（当前策略比 ref 更不确信这个 token）；
   - `β = kl_coef = 0.001`。
3. **代入公式**：\(d = -2.0 - (-1.5) = -0.5\)；\(r = 1.0 - 0.001 \times (-0.5) = 1.0005\)。
4. **需要观察的现象**：β 这么小，对奖励的影响（0.0005）几乎可以忽略。
5. **预期结论**：这正是「温和缰绳」的含义——单 token 上 KL 罚微乎其微，但它在**整条序列上累积**（所有有效 token 都被扣一点），并在**整个训练过程中持续作用**，足以在宏观上把策略拉回 ref 附近，又不会在微观上压住每一次有用的探索。若把 β 调大到 `0.1`，则 \(r = 1.0 - 0.1\times(-0.5) = 1.05\) 或对正向 KL 的 token 大幅削分，策略会被过早按死、涨不动分。**待本地验证**不同 β 下 `critic/kl` 曲线的差异。

#### 4.3.5 小练习与答案

**练习 1**：为什么默认用 `fixed` 控制器而非 `adaptive`？
**答案**：`fixed` 的 β 恒定，行为可预测、易复现；`adaptive` 会根据当前 KL 与目标的偏差动态调 β（见 u5-l4），更鲁棒但多一个 `target_kl`/`horizon` 需要调。TinyZero 追求「最小复现」，选了最简单的 fixed。

**练习 2**：若把 `kl_coef` 设为 `0`，训练会发生什么？
**答案**：`token_level_rewards = token_level_scores`，缰绳完全松开。模型可以无约束地漂移，短期内分可能涨得更快，但很快会出现语言能力退化、生成乱码等灾难性漂移，最终分反而崩塌。

---

### 4.4 模型容量与训练超参：为什么 3B 涌现而 0.5B 不行

#### 4.4.1 概念说明

R1 Zero 的涌现**不是免费的**——它对模型容量有门槛。README 明确指出：0.5B 基座「学不会推理」，而 3B 才能涌现出复杂的推理技能。这不是超参没调好，而是**容量不足**：推理所需的「自我验证、回溯、多步搜索」等行为，需要模型在预训练阶段已经积累了足够的世界知识与组合泛化能力，RL 只是把这些潜藏能力「激活」出来。容量太小的模型，预训练时根本没攒够这些能力，RL 自然激活不出。

这就解释了为什么"Aha moment"是一个**相变（phase transition）**现象：不是「越训越好」的线性进步，而是越过某个容量/训练步数临界点后，行为质变——回答突然变长、开始出现自验证。

#### 4.4.2 核心流程：从指标看相变

相变在监控指标上表现为两条曲线**同时上升**：

1. `response_length/mean` 上升：模型自发写更长的推理链（不再一两句就草草给答案）。
2. `critic/score/mean` 上升：更长推理确实带来更高准确率（不是在堆废话）。

两者同时上升，就是小尺度下"Aha moment"的可观测签名。若只有 `response_length` 涨而 `score` 不涨，那是在堆无效 token（甚至刷长度）；若只有 `score` 涨而长度不变，那是普通的能力提升、而非推理涌现。

#### 4.4.3 源码精读

**① 容量门槛的官方表述**：

[README.md:10](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/README.md#L10) ——「Through RL, the 3B base LM develops self-verification and search abilities all on its own.」明确点出 3B、self-verification、search 三个关键词。

[README.md:57](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/README.md#L57) ——「For Qwen2.5-0.5B base, we know it fails to learn reasoning.」直说 0.5B 不行。

[README.md:71](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/README.md#L71) ——「the base model is able to develop sophisticated reasoning skills」，对应 3B 段落。

**② PPO 基线对比表**（虽是 GSM8k，但佐证「不同基座 RL 收益差异」）：

[docs/experiment/ppo.rst:29-32](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/docs/experiment/ppo.rst#L29-L32) —— `Qwen/Qwen2.5-0.5B-Instruct` 预训练 36.4 → PPO 后 56.7；同表里 `gemma-2-2b-it` 预训练 23.9 → SFT 52.06 → SFT+PPO 64.02。可见 RL/RLHF 能显著提升准确率，但起点（基座）决定天花板。

**③ 关键超参（来自训练脚本）**：

[scripts/train_tiny_zero.sh:7](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/scripts/train_tiny_zero.sh#L7) —— `max_response_length=1024`：给推理链留足长度空间。若设太短（如 128），模型还没来得及展开推理就被截断，涌现无从发生。监控 `response_length/clip_ratio` 可看是否频繁撞墙。

[scripts/train_tiny_zero.sh:11](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/scripts/train_tiny_zero.sh#L11) 与 [train_tiny_zero.sh:18](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/scripts/train_tiny_zero.sh#L18) —— actor `lr=1e-6`、critic `lr=1e-5`：actor 学习率比 critic 小一个数量级，因为策略更新要稳（PPO clip 已经在约束，再大的 lr 会破坏信任域）；critic 要追上变化的回报，可以稍快。

[scripts/train_tiny_zero.sh:31](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/scripts/train_tiny_zero.sh#L31) —— `total_epochs=15`：足够长的训练让相变有时间发生。

**④ 监控指标的定义**：

[verl/trainer/ppo/ray_trainer.py:172-257](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L172-L257) —— `compute_data_metrics` 产出的全套数据指标。

两条关键曲线就在这里：

- [ray_trainer.py:202-203](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L202-L203) —— `critic/score/mean`：`token_level_scores` 沿序列求和后的均值，即平均任务分（含 format_score）。
- [ray_trainer.py:239-240](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L239-L240) —— `response_length/mean`：平均有效回答长度。
- [ray_trainer.py:245-246](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L245-L246) —— `response_length/clip_ratio`：回答撞到 `max_response_length=1024` 上限的比例，撞墙率持续走高说明模型想写更长但被截断。

配合 KL 指标（4.3 节的 `critic/kl`、`critic/kl_coeff`）与 actor 监控（`actor/pg_clipfrac`、`actor/ppo_kl`，见 u5-l2 / u7-l5），就构成完整的调参仪表盘。

#### 4.4.4 代码实践（监控型，承接 u7-l5）

1. **实践目标**：在 wandb 面板上识别"Aha moment"的信号。
2. **操作步骤**：
   - 按 u1-l3 跑 countdown 3B 训练，或读作者公开的实验日志 `wandb.ai/jiayipan/TinyZero`（README 第 16 行给出链接）。
   - 在面板上同时勾选 `response_length/mean` 与 `critic/score/mean` 两条曲线。
3. **需要观察的现象**：训练前期两条线缓慢上升或震荡；某个 step 附近 `response_length/mean` 出现明显跃升，随后 `critic/score/mean` 也跟着上一个台阶。
4. **预期结果**：两条曲线同时上升即推理涌现信号；同时 `do_print` 抽样打印的轨迹文本里开始出现「Wait」「let me check again」之类的自验证措辞。
5. **若只涨长度不涨分**：检查 `format_score` 是否被改高诱发刷长度，或 `max_response_length` 是否过小导致模型在长度内无法完成推理。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `max_response_length` 设为 1024 而不是 256？
**答案**：推理涌现的标志就是回答变长。若上限太短，模型还没展开「思考→验证→修正」就被截断，`response_length/clip_ratio` 会早早撞到 1.0，涌现被人为压住。1024 给了足够的相变空间。

**练习 2**：`actor.optim.lr=1e-6` 比 `critic.optim.lr=1e-5` 小，有什么考量？
**答案**：策略（actor）更新必须稳——PPO 的 clip 已经在限制单步策略变化，过大的 lr 会突破信任域、导致 `actor/ppo_kl` 失控、训练崩溃；critic 只是回归拟合回报，可以学得快些。两者解耦调参。

---

## 5. 综合实践：写一份 TinyZero 调参复盘

把本讲四个模块串起来，写一份**一页纸实验复盘**（Markdown，无需运行训练，基于源码与公开实验日志推理即可）。复盘需回答以下问题：

1. **奖励面分析**：定位 [countdown.py:59](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/reward_score/countdown.py#L59) 的 `format_score=0.1`。分别推演 `format_score` 取 `0 / 0.1 / 0.6` 时，一个「只刷格式」的策略与一个「偶尔解对」的策略各自的期望分（用 4.2.4 的方法）。给出结论：默认 0.1 为什么是平衡点，而「设为 0」真正的风险是冷启动而非「只刷格式」。

2. **KL 缰绳分析**：引用 [ray_trainer.py:102](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L102) 的 `token_level_rewards = token_level_scores - beta * kld`，说明 `kl_coef=0.001` 如何在「奖励涨分」与「不偏离基座」之间权衡；并指出没有 ref policy 时（[ray_trainer.py:98-100](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L98-L100)）缰绳会失效。

3. **监控方案**：给出观察「`response_length` 上升 + `score` 上升」的具体方法——在 wandb 勾选 [ray_trainer.py:239-240](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L239-L240) 的 `response_length/mean` 与 [ray_trainer.py:202-203](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L202-L203) 的 `critic/score/mean`，辅以 `response_length/clip_ratio`、`critic/kl`、`actor/ppo_kl`。

4. **容量门槛**：结合 [README.md:57](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/README.md#L57) 与 [ppo.rst:29-32](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/docs/experiment/ppo.rst#L29-L32)，说明为什么 3B 能涌现而 0.5B 不能。

**交付物**：一份 `tuning-postmortem.md`（写在仓库外或 `TinyZero-tutorial/` 之外的个人笔记里，**不要写进仓库源码**），包含上述四节，每节给出对应的源码永久链接作为论据。

---

## 6. 本讲小结

- **涌现的物质基础是 `compute_score`**：它定义了「什么算赢」，用三级打分 `{0, 0.1, 1.0}` 把稀疏结果奖励注入训练；策略梯度放大正确轨迹，越过容量临界点后行为质变。
- **`format_score=0.1` 是塑形奖励的双刃剑**：给冷启动一个非零台阶，但调太高（趋近 `score`）会诱发「只刷格式」黑客；调到 0 反而消灭黑客、却引入稀疏冷启动困难。真正要警惕的是「相对比例」而非绝对值。
- **`kl_coef=0.001` 是 reward 端的温和缰绳**：`token_level_rewards = token_level_scores - β·kld` 把策略拴在冻结的基座附近，防灾难性漂移又不过度压制探索；前提是建了 ref policy。
- **涌现是相变、有容量门槛**：3B 能涌现 self-verification/search，0.5B 不能；`max_response_length=1024` 给相变留长度空间，actor/critic 学习率解耦。
- **Aha moment 的可观测签名**：`response_length/mean` 与 `critic/score/mean` **同时上升**，辅以 `do_print` 抽样文本里出现自验证措辞。
- **调参仪表盘**：`critic/score`、`critic/kl`、`response_length`、`actor/ppo_kl`、`actor/pg_clipfrac` 五组指标共同诊断训练健康度。

---

## 7. 下一步学习建议

本讲是整本手册的终篇。你已经从「项目定位」一路读到「算法实现 → Worker 引擎 → 调参解读」，具备了独立阅读 TinyZero 全部源码的能力。后续建议：

1. **跑一次真实训练**：按 u1-l3 在 3B 模型上跑 countdown，亲手在 wandb 看 `response_length` 与 `score` 的相变曲线，验证本讲的论断。这是把「读懂」变成「真懂」的关键一步。
2. **做一个自定义任务**：按 u7-l3 的「数据-奖励-路由」三件套，新增一个你自己设计的可验证任务（如 u7-l3 的两数求和），完整跑通一次 RL，体会奖励设计如何影响涌现。
3. **迁移到上游 veRL**：README 已声明 TinyZero 弃用、生产请用最新 [veRL](https://github.com/volcengine/verl)。带着本手册建立的心智模型去读 veRL，你会发现数据协议、single-controller、PPO/GRPO、混合引擎的核心思想是一脉相承的，只是工程更完善、支持更多后端（SGLang、Megatron 等）。
4. **深入 RL 算法本身**：若对 GRPO 感兴趣，可对照 u5-l5 阅读 DeepSeek-R1 与 GRPO 原始论文，理解「组内归一化替代 critic」在小尺度推理涌现中的角色；再对比 DAPO、ReMax 等后续改进为何调整了 clipping 与 KL 策略。
5. **奖励工程的进阶**：本讲的 `format_score` 只是塑形奖励的最简形式。可进一步学习过程奖励模型（PRM）、PRM800K 等思路，理解「结果奖励 vs 过程奖励」在推理训练中的取舍——这正是 R1 Zero 之后推理模型研究的主线之一。
