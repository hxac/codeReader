# 长度、余弦调度与重复惩罚奖励

## 1. 本讲目标

本讲承接 [u3-l2 奖励函数注册表与数学正确性奖励](u3-l2-reward-registry-accuracy.md)。在那一讲里，我们已经看到 `accuracy_reward` 给出的是「稀疏的二值信号」——对就是对（1.0）、错就是错（0.0）。这种信号有一个老问题：**它只看最终答案，不管模型啰不啰嗦、有没有原地打转**。

一个「想了 2000 个 token 才得到正确答案」的回答，和一个「100 个 token 就得到正确答案」的回答，在 `accuracy_reward` 眼里是一模一样的 1.0。但前者浪费算力、推理慢，还常常伴随「复读机」式的重复。DeepSeek-R1 / Kimi 1.5 / DAPO 等工作都专门为这个问题设计了**长度塑形（length shaping）奖励**。

学完本讲，你应当能够：

- 说清 `len_reward` 如何用 `lambda_val` 在一个 batch 内对「正确且简短」给正奖励、对「冗长且错误」给负奖励。
- 推导 `get_cosine_scaled_reward` 里 `progress → cosine → reward` 的三段映射，并解释为什么「短的正确得多、短的错误反而罚得重」。
- 解释 `get_repetition_penalty_reward` 用 n-gram 唯一率给出 \([0, -1]\) 区间惩罚的原理。
- 解释 `get_soft_overlong_punishment` 的「软超长」分段函数与它操作的是 token 数（而非字符数）这一关键细节。

## 2. 前置知识

### 2.1 为什么要奖励「长度」

大模型在做长链推理（Long Chain-of-Thought）时会出现两种病态：

1. **过度思考（overthinking）**：明明几步能算完，却啰嗦几千 token，既慢又费钱。
2. **复读（repetition / degeneration）**：陷入「我需要仔细分析……我需要仔细分析……」的死循环，生成一堆无意义重复。

长度塑形奖励就是用一组**密集的、连续的奖励信号**，把模型从这两个坑里往外拉。它们和 `accuracy_reward` 搭配使用：正确性奖励负责「答对」，长度奖励负责「答得干净」。

### 2.2 你需要记住的四个签名约定

open-r1 的奖励函数都遵循 trl 的「列注入」机制（见 u3-l2），统一形如 `func(completions, solution=None, **kwargs)`：

- `completions`：`list[list[dict]]`，每个内层 `dict` 至少有 `content` 字段。绝大多数函数取 `completion[0]["content"]` 当文本。
- `solution`：金答案列表，由数据集列注入。
- 返回值：与 `completions` 等长的 `list[float]`。

本讲的四个函数里，`len_reward`、`get_cosine_scaled_reward`、`get_repetition_penalty_reward` 都吃「文本」、按**字符数**算长度；唯独 `get_soft_overlong_punishment` 吃 `completion_ids`（token id 列表）、按**token 数**算长度——这个差异是本讲最大的坑，4.4 节会专门讲。

### 2.3 工厂函数回顾

四个函数里有三个是「工厂函数」：`get_cosine_scaled_reward`、`get_repetition_penalty_reward`、`get_soft_overlong_punishment`。它们**外层接收超参、返回一个闭包**，闭包才是真正被 `GRPOTrainer` 调用的奖励函数。只有 `len_reward` 是直接函数——它不读任何超参，行为完全固定。这一差异在 [源码精读](#46-注册表如何把它们焊在一起) 一节会再次对照。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `src/open_r1/rewards.py` | 四个奖励函数的实现，以及把它们组装进 `get_reward_funcs` 注册表 |
| `src/open_r1/configs.py` | `GRPOScriptArguments`，定义这些奖励的**超参默认值**（`cosine_*`、`repetition_*`、`max_completion_len`、`soft_punish_cache`） |
| `tests/test_rewards.py` | 对四个函数的行为有详尽单元测试，是理解「正确行为」的最好参照 |

## 4. 核心概念与源码讲解

### 4.1 len_reward：组内相对的长度奖惩

#### 4.1.1 概念说明

`len_reward` 来自 [Kimi 1.5 技术报告](https://huggingface.co/papers/2501.12599)，目标是「**在不牺牲正确性的前提下，鼓励更短的回答**」。

它的核心思想是：**长度本身没有绝对意义，只有「相对同批次的其它回答」才有意义**。所以它先计算一个 batch 内所有回答的最短长度 `min_len` 和最长长度 `max_len`，再把每个回答的长度归一化到这个区间，得到一个相对分 `lambda_val`。

关键设计：

- **正确回答**：`lambda_val` 直接当奖励。越短分越高（最短可得 +0.5），越长分越低（最长可得 −0.5）。
- **错误回答**：`reward = min(0, lambda_val)`。也就是说**短而错的回答不奖励（截断到 0）**，只有**长而错的回答才被罚**。逻辑是：「答错了就不该奖励，答错还啰嗦那就更该罚」。

#### 4.1.2 核心流程

```
1. 取出每个 completion 的文本 content
2. 逐条判正确性 correctness（用 math_verify 的 parse + verify）
   - 金答案解析失败 → 当作"正确"(True)，避免误罚
3. 算每条长度 lengths，取 min_len / max_len
4. 若 max_len == min_len（全等长）→ 全返回 0.0（无法比较）
5. 对每条：
   lambda_val = 0.5 - (length - min_len) / (max_len - min_len)   # ∈ [-0.5, 0.5]
   正确 → reward = lambda_val
   错误 → reward = min(0, lambda_val)
```

`lambda_val` 的取值范围：

\[
\lambda_{val} = 0.5 - \frac{length - min\_len}{max\_len - min\_len}, \qquad \lambda_{val} \in [-0.5,\ 0.5]
\]

最短回答 \(\lambda_{val}=0.5\)，最长回答 \(\lambda_{val}=-0.5\)。

| 情形 | 最短 | 最长 |
|---|---|---|
| 正确 | \(+0.5\) | \(-0.5\) |
| 错误 | \(0\)（被 `min(0,·)` 截断） | \(-0.5\) |

#### 4.1.3 源码精读

函数定义与长度归一化：

[len_reward：先判正确性，再用 min/max 归一化长度并分支给分（src/open_r1/rewards.py:L132-L202）](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/rewards.py#L132-L202)

「金答案解析失败就当作正确」是一个值得注意的兜底，和 `accuracy_reward` 返回 `None`（让 Trainer 跳过）的策略不同：

[金答案解析失败 → correctness=True，避免误罚（src/open_r1/rewards.py:L156-L159）](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/rewards.py#L156-L159)

> 为什么这里不一刀切返回 `None`？因为 `len_reward` 是**组内相对**的——它必须先拿到全组的 `min_len`/`max_len` 才能算分，逐条返回 `None` 会破坏 min/max 的一致性。所以它选择「当作正确」，把判分压力转移给后续的长度项。

最关键的打分逻辑只有两行：

[lambda_val 与正确性分支：正确直接用，错误截断到 0（src/open_r1/rewards.py:L193-L198）](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/rewards.py#L193-L198)

还有一个边界保护：当组内所有回答长度相等时（`max_len == min_len`），分母为 0，直接返回全 0：

[所有回答等长时返回全 0（src/open_r1/rewards.py:L188-L189）](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/rewards.py#L188-L189)

> 这条也意味着：**单条调用 `len_reward` 永远返回 0**——因为它必须在「一组」回答（GRPO 里就是一个 prompt 采样的 G 条 `num_generations`）里才有意义。

#### 4.1.4 代码实践

实践目标：亲手验证「正确+短 > 正确+长 > 错误+短(=0) > 错误+长」的排序。

操作步骤（示例代码）：

```python
# 示例代码：在仓库根目录用 PYTHONPATH=src 运行
from open_r1.rewards import len_reward

# 同一个金答案，4 条不同长度/正确性的回答
completions = [
    [{"content": r"\boxed{\frac{63}{400}}"}],                       # 正确，最短
    [{"content": r"\boxed{\frac{63}{400}}  " + "x" * 10}],          # 正确，较长
    [{"content": r"\boxed{\frac{64}{400}}"}],                       # 错误，最短
    [{"content": r"\boxed{\frac{64}{400}}  " + "x" * 10}],          # 错误，较长
]
solutions = [r"\frac{63}{400}"] * 4
print(len_reward(completions, solutions))
```

预期结果（与 `tests/test_rewards.py` 的 `test_mixed_correctness` 一致）：

- 第 0 条（正确最短）≈ `0.5`（正奖励，最大）。
- 第 1 条（正确较长）≈ `-0.5`。
- 第 2 条（错误最短）= `0.0`（被 `min(0, ·)` 截断，不奖励）。
- 第 3 条（错误较长）≈ `-0.5`（罚得最重）。

需要观察的现象：`rewards[0] > rewards[1]`、`rewards[2] > rewards[3]`，且两个错误项都 ≤ 0。若把四条都改成同一长度，会得到 `[0,0,0,0]`。精确数值「待本地验证」（取决于 `math_verify` 对 `\boxed{}` 的解析，仓库测试已固化该行为）。

#### 4.1.5 小练习与答案

**练习 1**：如果只给 `len_reward` 传**一条** completion，它返回什么？为什么？

> 答案：返回 `[0.0]`。因为 `min_len == max_len`，触发 `if max_len == min_len: return [0.0] * ...` 分支。长度奖励天然是「组内相对」的，单条无从比较。

**练习 2**：一条回答既长又错，和一条回答既长又对，谁的 `lambda_val` 更高？

> 答案：`lambda_val` 只看长度，不看正确性，所以**两者相同**（都是 −0.5）。但最终奖励不同：长而错被 `min(0, −0.5) = −0.5` 罚，长而对也是 −0.5——可见 `len_reward` 对「太长」是一视同仁地不喜欢的，正确性只决定「短」的时候要不要给正分。

---

### 4.2 get_cosine_scaled_reward：用余弦曲线调度奖励

#### 4.2.1 概念说明

`get_cosine_scaled_reward` 是一个**工厂函数**，用一条**余弦曲线**把「长度」光滑地映射到「奖励」。相比 `len_reward` 的线性归一化，它有三个升级：

1. **绝对长度感知**：它不依赖 batch 内的 `min_len`/`max_len`，而是用绝对长度除以超参 `max_len`，所以单条也能打分。
2. **四个端点可调**：用 `min/max_value_correct`、`min/max_value_wrong` 四个旋钮分别控制「正确/错误」回答的奖励上下界。
3. **正确与错误的曲线方向相反**：
   - 正确回答：**越短奖励越高**（短而准 = 高效）。
   - 错误回答：**越短罚得越重**（短而错 = 武断，不如长一点「至少思考过」）。

#### 4.2.2 核心流程

对每条回答：

\[
progress = \frac{gen\_len}{max\_len}, \qquad cosine = \cos(progress \cdot \pi)
\]

当长度从 0 涨到 `max_len`，`progress` 从 0 到 1，`cosine` 从 \(\cos 0 = 1\) 平滑降到 \(\cos\pi = -1\)。然后线性映射到奖励区间：

\[
reward = min\_value + \frac{1}{2}(max\_value - min\_value)(1 + cosine)
\]

注意 `cosine` 是 `[-1, 1]` 上的值，`(1+cosine)` 把它搬到 `[0, 2]`，乘以 `0.5(max_value-min_value)` 后平移到 `[min_value, max_value]`：

| `cosine` 值 | 含义 | reward |
|---|---|---|
| \(+1\)（最短） | \(progress=0\) | \(max\_value\) |
| \(0\)（半长） | \(progress=0.5\) | \(\frac{min+max}{2}\) |
| \(-1\)（满长） | \(progress=1\) | \(min\_value\) |

正确回答直接用 `min_value_correct / max_value_correct`；错误回答**故意把 min/max 对调**，于是曲线方向反转：

[错误回答时交换 min/max，使曲线方向反转（src/open_r1/rewards.py:L269-L275）](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/rewards.py#L269-L275)

用函数签名默认值（`min_value_correct=0.5, max_value_correct=1.0, min_value_wrong=-1.0, max_value_wrong=-0.5`）画一张表：

| 情形 | 最短（cosine=1） | 满长（cosine=−1） |
|---|---|---|
| 正确 | \(1.0\)（`max_value_correct`） | \(0.5\)（`min_value_correct`） |
| 错误 | \(-1.0\)（`min_value_wrong`） | \(-0.5\)（`max_value_wrong`） |

> 直觉：正确的越短越好（高效），错误的越短越糟（武断）。这条曲线是「光滑」的，相比 `len_reward` 的折线，梯度信号更平滑，有利于训练稳定。

#### 4.2.3 源码精读

工厂签名，五个超参都有默认值：

[工厂签名：四个端点 + max_len（src/open_r1/rewards.py:L205-L211）](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/rewards.py#L205-L211)

映射三连——注意 `progress` **没有做 clamp**：

[progress 与 cosine 计算（src/open_r1/rewards.py:L266-L267）](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/rewards.py#L266-L267)

最终奖励公式：

[reward = min_value + 0.5(max-min)(1+cosine)（src/open_r1/rewards.py:L269-L277）](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/rewards.py#L269-L277)

> ⚠️ `progress` 不截断的副作用：当 \(gen\_len > max\_len\) 时 \(progress > 1\)，\(progress\cdot\pi\) 越过 \(\pi\)，余弦值会从 −1 重新往上爬。这意味着「超长到离谱」的回答反而可能拿到比「刚好满长」更高的分。实践中 `max_len` 通常设得比真实生成长度大（默认 1000），所以多数样本落在 \([0,1]\) 区间；但调参时要留意这个边界。

和 `len_reward` 的另一个差别：金答案解析失败时，`cosine` 给 `1.0`（跳过且给满），而不是当作正确：

[金答案解析失败 → reward=1.0（src/open_r1/rewards.py:L238-L241）](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/rewards.py#L238-L241)

#### 4.2.4 代码实践

实践目标：用 `tests/test_rewards.py::test_cosine_scaled_reward` 的方法，把「长度→奖励」曲线画出来，直观对比正确/错误两条曲线。

操作步骤（示例代码）：

```python
# 示例代码：需安装 matplotlib（pip install matplotlib）
import math
from open_r1.rewards import get_cosine_scaled_reward

# 用与单元测试一致的参数，max_len=100 便于观察
reward_fn = get_cosine_scaled_reward(
    min_value_wrong=-1.0, max_value_wrong=-0.5,
    min_value_correct=0.5, max_value_correct=1.0, max_len=100,
)

gold = r"\frac{63}{400}"
correct_ans = r"\boxed{\frac{63}{400}}"   # 长度 22
wrong_ans   = r"\boxed{\frac{64}{400}}"   # 长度 22

lengths = list(range(22, 101))             # 22..100
correct_rewards, wrong_rewards = [], []
for L in lengths:
    pad = " " * (L - len(correct_ans))
    correct_rewards.append(reward_fn([[{"content": correct_ans + pad}]], [gold])[0])
    wrong_rewards.append(reward_fn([[{"content": wrong_ans + pad}]], [gold])[0])

# 绘制 length-reward 曲线
import matplotlib.pyplot as plt
plt.plot(lengths, correct_rewards, label="correct")
plt.plot(lengths, wrong_rewards, label="wrong")
plt.xlabel("completion length (chars)"); plt.ylabel("reward")
plt.legend(); plt.title("cosine length-reward curve")
plt.savefig("cosine_curve.png")
```

需要观察的现象与预期结果：

- **正确曲线**从约 `0.94`（长度 22 处，因为 `progress=0.22`）单调下降到 `0.5`（长度 100 处）——越短奖励越高。
- **错误曲线**从约 `−0.94` 单调上升到 `−0.5`——越短罚得越重。
- 两条曲线关于 y 轴大致「镜像」（因为错误分支交换了 min/max）。

> 上述关键数值 `0.943 / -0.942` 已在 `tests/test_rewards.py` 的 `test_cosine_scaled_reward` 中被断言固化，可作为参照（测试里 content 实际是 22 字符，`content_len=20` 因小于真实长度而被忽略，最终 `gen_len=22`）。绘图结果「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：用签名默认值，一条**正确**回答长度恰为 `max_len` 时，奖励是多少？

> 答案：\(0.5\)。因为 `progress=1`、`cosine=cos(π)=-1`，`reward = min_value_correct + 0.5·(max-min)·(1-1) = min_value_correct = 0.5`。

**练习 2**：为什么错误分支要交换 `min_value` 和 `max_value`？

> 答案：公式 `min + 0.5(max-min)(1+cosine)` 把 `cosine=1`（最短）映射到 `max`、`cosine=-1`（最长）映射到 `min`。错误回答希望「最短罚最重」，即最短要落在更负的 `min_value_wrong=-1.0`，于是把 `max_value` 设成 `min_value_wrong`、`min_value` 设成 `max_value_wrong=-0.5`，相当于把曲线翻转，使「短」对应更负的端点。

---

### 4.3 get_repetition_penalty_reward：n-gram 重复惩罚

#### 4.3.1 概念说明

这个函数专门对付「复读机」。它来自 [Demystify-Long-CoT 论文附录 C.2](https://huggingface.co/papers/2502.03373)，思路很直接：

> **把回答切成 n-gram（连续 n 个词），统计「唯一 n-gram 占比」。重复越多，唯一占比越低，惩罚越重。**

它只看重复程度，**不看正确性**，所以是个纯「格式卫生」奖励，输入也不需要 `solution`。

#### 4.3.2 核心流程

1. 工厂校验 `max_penalty ≤ 0`（惩罚必须是负数或 0，否则报 `ValueError`）。
2. 按语言选分词器：英文 `text.lower().split()`（小写化 + 空格切分），中文用 `jieba` 分词。
3. 对每条回答：
   - 空串 → 0.0。
   - 词数 < `ngram_size` → 0.0（凑不出任何 n-gram）。
   - 否则统计唯一 n-gram 数 `unique` 与总 n-gram 数 `total`：

\[
scaling = 1 - \frac{unique}{total} \in [0, 1), \qquad reward = scaling \times max\_penalty \in [max\_penalty,\ 0]
\]

`scaling=0` 表示完全没有重复（所有 n-gram 都不同），不罚；`scaling` 越接近 1 表示重复越严重，罚得越接近 `max_penalty`。

#### 4.3.3 源码精读

工厂校验 + 语言分流：

[max_penalty 必须非正；英文/中文分词分流（src/open_r1/rewards.py:L285-L296）](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/rewards.py#L285-L296)

英文 n-gram 生成（小写 + 滑窗 zip）：

[en：text.lower().split() 后用 zip 滑窗取 n-gram（src/open_r1/rewards.py:L300-L302）](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/rewards.py#L300-L302)

打分核心——`scaling` 与 `reward`：

[scaling = 1 - unique/total，reward = scaling * max_penalty（src/open_r1/rewards.py:L333-L350）](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/rewards.py#L333-L350)

> 手算验证（与 `tests/test_rewards.py::test_full_repetition` 一致）：`"this this this this this"` 切 2-gram 得 4 个全是 `(this,this)`，`unique=1, total=4`，`scaling=1-1/4=0.75`，`reward=0.75×(-1)=-0.75`。
>
> 再看 `test_partial_repetition`：`"this is a this is a test"` 切 2-gram 共 6 个、唯一 4 个，`scaling=1-4/6=1/3`，`reward≈-0.333`。

#### 4.3.4 代码实践

实践目标：构造从「无重复」到「全重复」的若干回答，观察惩罚如何随重复率线性加深。

操作步骤（示例代码）：

```python
# 示例代码
from open_r1.rewards import get_repetition_penalty_reward

fn = get_repetition_penalty_reward(ngram_size=2, max_penalty=-1.0)
samples = [
    "the quick brown fox jumps over the lazy dog",   # 几乎无重复
    "the the the quick brown fox jumps over",        # 局部重复
    "the the the the the the the the the",           # 全重复
]
completions = [[{"content": s}] for s in samples]
print(fn(completions))
# 预期：约 [ 0.0（或接近0） ,  某负值 ,  ≈ -0.875 ]
```

需要观察的现象：重复越严重，奖励越接近 `max_penalty`（这里是 −1.0）；完全不重复时为 0.0。

预期结果：第 1 条 ≈ 0.0（词几乎全不同），第 2 条为某负值，第 3 条接近 −0.875（9 个词、8 个 2-gram 全为 `(the,the)`，`scaling=1-1/8=0.875`）。精确值「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`get_repetition_penalty_reward(ngram_size=2, max_penalty=1.0)` 会发生什么？

> 答案：抛 `ValueError`。工厂开头就校验 `if max_penalty > 0: raise ValueError(...)`，因为「惩罚」按定义必须 ≤ 0，写成正值是配置错误（见 `test_positive_max_penalty_raises_value_error`）。

**练习 2**：为什么中文要用 `jieba` 而不是直接 `split()`？

> 答案：中文没有空格分词，`split()` 会把整句当成一个「词」，n-gram 退化失效。`jieba.cut` 做中文分词后才能正确统计词级 n-gram 重复。工厂里对 `language="zh"` 会先检查 jieba 是否安装，未装则报错。

---

### 4.4 get_soft_overlong_punishment：软超长惩罚（按 token 数）

#### 4.4.1 概念说明

前三个函数都按「字符数」算长度。`get_soft_overlong_punishment` 不一样——它来自 [DAPO 论文 Eq.(13)](https://huggingface.co/papers/2503.14476)，按 **token 数**惩罚超长回答，而且只罚不长、不奖短（对短回答给 0）。

「软（soft）」是相对「硬截断」而言的。普通做法是「超过 max 就一刀切 −1」，这会造成奖励悬崖，梯度不稳。DAPO 的做法是设一个**缓冲带** `soft_punish_cache`：在 `[max_len - cache, max_len]` 这段区间里**线性渐降**到 −1，过了 `max_len` 才恒为 −1。

#### 4.4.2 核心流程

设 `max_len = max_completion_len`、`cache = soft_punish_cache`，对每条回答的 token 数 `L = len(ids)`：

\[
reward(L) =
\begin{cases}
0, & L \le max\_len - cache \\
\dfrac{(max\_len - cache) - L}{cache}, & max\_len - cache < L \le max\_len \\
-1, & L > max\_len
\end{cases}
\]

用默认 `max_len=100, cache=20` 画图（`tests/test_rewards.py` 用的就是这组值）：

| token 数 L | 区间 | reward |
|---|---|---|
| 50 | \(L \le 80\) | `0.0` |
| 80 | 刚进缓冲带 | `0.0` |
| 90 | 缓冲带中段 | `(80−90)/20 = −0.5` |
| 100 | 缓冲带末端 | `(80−100)/20 = −1.0` |
| 110 | 超过 max | `-1.0` |

#### 4.4.3 源码精读

工厂签名，两个超参：

[工厂：max_completion_len + soft_punish_cache（src/open_r1/rewards.py:L620-L628）](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/rewards.py#L620-L628)

> 注意函数注释把 `soft_punish_cache` 描述为「Minimum length of the completion」，其实它是**缓冲带宽度**，不是最小长度——读源码时以代码的三段式逻辑为准。

核心三段式打分：

[按 token 数 L 分段：≤阈值给0，缓冲带线性渐降，超 max 给-1（src/open_r1/rewards.py:L630-L641）](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/rewards.py#L630-L641)

> ⚠️ **最关键的细节**：这个函数的入参是 `completion_ids: list[list[int]]`（token id 列表），长度按 `len(ids)` 即 **token 数**计算。这与前三个函数按 `len(content)` 字符数计算完全不同。`GRPOTrainer` 会把每个回答的 token id 列表以 `completion_ids` 列注入，所以它能拿到 token 级长度。
>
> 另外，`configs.py` 里 `max_completion_len` 的 help 文本写的是「Maximum number of **characters**」，与实际按 token 计的行为不一致——这是文档与代码的一处偏差，以代码（token 数）为准。

#### 4.4.4 代码实践

实践目标：直接用 token id 列表（长度即元素个数）验证三段式曲线。

操作步骤（示例代码，对应 `test_soft_overlong_punishment_*`）：

```python
# 示例代码
from open_r1.rewards import get_soft_overlong_punishment

fn = get_soft_overlong_punishment(max_completion_len=100, soft_punish_cache=20)
for L in [50, 80, 90, 100, 110]:
    print(L, fn(completion_ids=[[1] * L]))
```

预期结果：`50→0.0`、`80→0.0`、`90→-0.5`、`100→-1.0`、`110→-1.0`。这与单元测试 `test_soft_overlong_punishment_intermediate_completion`（90 → −0.5）等完全一致。

需要观察的现象：在 `[80, 100]` 区间奖励从 0 线性跌到 −1，之后封顶 −1。

#### 4.4.5 小练习与答案

**练习 1**：`max_completion_len=16384, soft_punish_cache=4096`（`configs.py` 默认值），一条 12000 token 的回答会被罚多少？

> 答案：`0.0`。因为阈值 `16384 - 4096 = 12288`，而 `12000 ≤ 12288`，落在第一段。只有 token 数超过 12288 才开始线性惩罚，超过 16384 才到 −1。

**练习 2**：把 `soft_punish_cache` 设为 0 会怎样？

> 答案：缓冲带消失，第二段条件 `0 < L ≤ max_len` 永远无法满足（分母也为 0），函数退化为「不超过 `max_len` 给 0、超过给 −1」的硬截断——这正是「软」要避免的悬崖。所以 `cache` 必须为正才有意义。

---

### 4.5 配置：超参从哪里来

四个函数里只有 `len_reward` 不读超参。另外三个都通过 `GRPOScriptArguments` 注入超参，默认值定义在 `configs.py`：

[cosine 相关超参（src/open_r1/configs.py:L240-L259）](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/configs.py#L240-L259)

[repetition 相关超参（src/open_r1/configs.py:L260-L267）](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/configs.py#L260-L267)

[soft_overlong 相关超参（src/open_r1/configs.py:L324-L331）](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/configs.py#L324-L331)

> ⚠️ 一个容易踩的坑：**`configs.py` 的默认值和函数签名的默认值并不一致**。最显著的是 `cosine_min_value_wrong`：函数签名默认 `−1.0`，但配置默认 `0.0`。由于 `get_reward_funcs` 用的是 `script_args.cosine_min_value_wrong`（见下节），实际训练里走的是配置值 `0.0`。这意味着默认配置下，「短而错」的回答只会被罚到 `0.0`（不罚）而不是 `−1.0`。改 YAML 才能调出函数签名那种「短错重罚」行为。

完整的默认值对照：

| 超参 | 函数签名默认 | configs.py 默认 |
|---|---|---|
| `cosine_min_value_wrong` | −1.0 | **0.0** |
| `cosine_max_value_wrong` | −0.5 | −0.5 |
| `cosine_min_value_correct` | 0.5 | 0.5 |
| `cosine_max_value_correct` | 1.0 | 1.0 |
| `cosine_max_len` | 1000 | 1000 |
| `repetition_n_grams` | （必填） | 3 |
| `repetition_max_penalty` | （必填） | −1.0 |
| `max_completion_len` | （必填） | 16384 |
| `soft_punish_cache` | （必填） | 4096 |

### 4.6 注册表如何把它们焊在一起

`get_reward_funcs` 把字符串名翻译成可调用函数（详见 u3-l2）。本讲四个函数的注册方式恰好体现了三种形态：

[注册表：cosine / repetition_penalty 是工厂调用，length 是直接引用（src/open_r1/rewards.py:L651-L662）](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/rewards.py#L651-L662)

[注册表：soft_overlong_punishment 是工厂调用（src/open_r1/rewards.py:L699-L702）](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/rewards.py#L699-L702)

- `"length"` → `len_reward`（直接函数引用，零超参）。
- `"cosine"` / `"repetition_penalty"` / `"soft_overlong_punishment"` → 工厂调用，把 `script_args` 的超参焊进闭包。

于是在 YAML 里写 `reward_funcs: ["accuracy", "cosine", "repetition_penalty", "length", "soft_overlong_punishment"]`，多函数按 `reward_weights` 加权求和（见 u3-l1），就能让正确性、长度、重复、超长四类信号同时作用于 GRPO 的优势估计。

## 5. 综合实践

**任务**：模拟一次 GRPO 打分，把本讲四个长度/重复奖励**同时**施加到同一组回答上，观察它们如何分工。

步骤：

1. 构造 3 条针对同一金答案 `\frac{63}{400}` 的回答：一条「短而正确」、一条「长而正确（带一段啰嗦）」、一条「长而错误（且含重复）」。
2. 用 `GRPOScriptArguments`（`dataset_name="dummy"`）拿到默认超参，调 `get_reward_funcs` 但**只**取这四个函数（参考 `tests/test_rewards.py::test_get_reward_funcs` 的构造方式）。
3. 分别调用 `len_reward`、`get_cosine_scaled_reward(...)`、`get_repetition_penalty_reward(...)`，并手工构造 `completion_ids` 调 `get_soft_overlong_punishment(...)`。
4. 把四列奖励画成一张分组柱状图，回答：哪条回答综合得分最高？`len_reward` 与 `cosine` 的结论是否一致？`repetition_penalty` 是否只惩罚了第 3 条？

```python
# 示例代码（骨架）
from open_r1.configs import GRPOScriptArguments
from open_r1.rewards import (
    get_reward_funcs, len_reward, get_cosine_scaled_reward,
    get_repetition_penalty_reward, get_soft_overlong_punishment,
)

args = GRPOScriptArguments(dataset_name="dummy", reward_funcs=["length"])  # 只为拿超参默认值
gold = r"\frac{63}{400}"
completions = [
    [{"content": r"\boxed{\frac{63}{400}}"}],                                          # 短·正确
    [{"content": r"\boxed{\frac{63}{400}}\n" + "Let me double check. " * 20}],        # 长·正确
    [{"content": r"\boxed{\frac{64}{400}}\n" + "I need to think. " * 20}],            # 长·错误·重复
]
solutions = [gold] * 3

cos = get_cosine_scaled_reward(
    min_value_wrong=args.cosine_min_value_wrong, max_value_wrong=args.cosine_max_value_wrong,
    min_value_correct=args.cosine_min_value_correct, max_value_correct=args.cosine_max_value_correct,
    max_len=args.cosine_max_len,
)
rep = get_repetition_penalty_reward(ngram_size=args.repetition_n_grams, max_penalty=args.repetition_max_penalty)
soft = get_soft_overlong_punishment(max_completion_len=args.max_completion_len, soft_punish_cache=args.soft_punish_cache)

print("len    :", len_reward(completions, solutions))
print("cosine :", cos(completions, solutions))
print("repeat :", rep(completions))
# soft 按 token 数：用字符长度近似 token id 长度（仅演示，真实应传 completion_ids）
print("soft   :", soft(completion_ids=[[1]*len(c[0]["content"]) for c in completions]))
```

讨论要点（预期，精确值「待本地验证」）：

- `len_reward`：因第 1 条最短且正确，应得最高（≈0.5 量级）；第 2、3 条等长或更长，分更低。
- `cosine`：注意默认 `cosine_min_value_wrong=0.0`，所以「长而错」的第 3 条不会被罚到 −1，而是接近 0——体会配置默认值与函数签名默认值的差异。
- `repetition_penalty`：只有第 3 条（「I need to think.」重复）拿到明显负值，前两条接近 0。
- `soft_overlong_punishment`：三条都远短于阈值（默认 16384−4096=12288），应全为 0.0。

## 6. 本讲小结

- `len_reward` 是**组内相对**的线性长度奖励：用 `lambda_val = 0.5 − (L−min)/(max−min)` 给「正确且短」正分、给「长而错」负分，单条调用恒为 0。
- `get_cosine_scaled_reward` 是**绝对长度**的余弦调度工厂：`progress→cosine→reward` 三段映射，正确分支越短越高、错误分支交换端点后越短越罚；`progress` 不截断是潜在边界坑。
- `get_repetition_penalty_reward` 用 n-gram 唯一率 `scaling = 1 − unique/total` 给出 \([max\_penalty, 0]\) 的纯重复惩罚，不看正确性、不奖短只罚重复。
- `get_soft_overlong_punishment` 是按 **token 数** 的分段软惩罚，缓冲带 `soft_punish_cache` 内线性渐降到 −1，是唯一吃 `completion_ids` 的函数。
- 四个函数三种注册形态：`len_reward` 直接引用，其余三个是工厂闭包；`configs.py` 的超参默认值与函数签名默认值存在差异（尤其 `cosine_min_value_wrong`）。
- 它们与 `accuracy_reward` 搭配，把「答对」的稀疏信号补成「答对 + 答得干净」的密集信号，是 open-r1 GRPO 长度塑形的核心工具箱。

## 7. 下一步学习建议

- 本讲只覆盖了 `rewards.py` 中「长度/重复」这一族函数。建议接着读 [u3-l3 格式与推理过程奖励](u3-l3-format-reasoning-rewards.md)，把 `format` / `tag_count` / `reasoning_steps` / `code_format` 这一组「结构塑形」奖励也补齐，形成对奖励函数全家桶的完整认识。
- 若想深入**代码类**奖励，进入 [u5 代码奖励与沙箱执行](u5-l1-code-reward-template.md)，看 `code_reward` 如何把回答送进沙箱执行测试用例——那是本讲之外、最复杂的一类奖励。
- 想亲手调参的读者，可以阅读 `recipes/Qwen2.5-1.5B-Instruct/grpo/config_demo.yaml`，尝试把 `reward_funcs` 改成本讲讲到的四个函数，并调整 `cosine_max_len`、`repetition_n_grams` 观察训练曲线（需 GPU，「待本地验证」）。
