# Logits 与温度采样

> 前置讲义：u3-l2（流式生成与生成函数族）。本讲在第 2 章生成函数骨架上，只讲解「NEW」的那一部分——如何把确定性的贪心解码改造成可控随机的温度采样。

## 1. 本讲目标

学完本讲，你应该能够：

1. 解释温度（temperature）如何通过一步「logits 除以 T」改变 softmax 概率分布的形状（锐化 / 平滑）。
2. 用 `torch.multinomial` 按概率采样，并用 `count_samples` 以「采样频率 ≈ 概率」的方式验证分布。
3. 读懂 `generate_text_temp_stream_cache` 的完整生成循环，特别是 `temperature=0` 时退化为 argmax 贪心解码的分支，以及它与第 2 章生成函数族共享的骨架。
4. 独立完成一个「温度扫描 + 确定性对比」实验，亲眼看到 T 从 0 升到 1.2 时输出从完全确定变为多样化。

## 2. 前置知识

### 2.1 logits 是什么（复习 u2-l1）

模型每前向一次，每个位置都会输出**全词表**的原始打分，称为 logits。对 Qwen3 0.6B 来说，词表大小是 151 936，所以「预测下一个 token」实际是给 151 936 个候选各打一个分。logits 可正可负、没有界限，还不是概率。

### 2.2 softmax：把打分变成概率

softmax 把任意实数向量 \( z = (z_1, \dots, z_V) \) 变成合法的概率分布（所有分量非负、且和为 1）：

\[ p_i = \frac{e^{z_i}}{\sum_{j=1}^{V} e^{z_j}} \]

指数放大的特性意味着：logit 稍高的 token，概率会**高得多**。

### 2.3 确定性解码 vs 随机采样

- **贪心解码（argmax）**：永远选分数最高的 token。输出完全确定，但只有一个「标准答案」，无法产生多样化文本，也容易陷入重复循环。第 2、3 章用的就是它。
- **随机采样（sampling）**：按概率分布「抽签」。分数高的 token 更容易被抽中，但分数低的也有机会。本讲的温度采样就是这一路线的第一步。

### 2.4 频率近似概率

如果按概率 \( p_i \) 独立采样 \( N \) 次，token \( i \) 被抽中的次数约为 \( N \cdot p_i \)。反过来，统计 1000 次采样中各 token 出现的次数，就能**用频率还原分布**——这是本讲 `count_samples` 的原理，也是我们验证「温度确实改变了分布」的实验手段。

### 2.5 为什么推理模型需要随机性

第 4 章的主题是**推理时扩展（inference-time scaling）**：不修改权重，只靠更好的解码策略提升准确率。其中「自一致性投票」（u5-l4）需要对同一道题采样出**多条不同的推理链**再投票——如果解码是确定性的，采 100 次也只会得到同一个答案。所以随机采样是后续自一致性、Best-of-N、GRPO rollout 采样的共同地基。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [reasoning_from_scratch/ch04.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch04.py) | 第 4 章的库代码：可视化、温度缩放、采样统计、温度/top-p 生成函数、自一致性投票 |
| [ch04/01_main-chapter-code/ch04_main.ipynb](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch04/01_main-chapter-code/ch04_main.ipynb) | 第 4 章教学 notebook：用 "The capital of Germany is" 这个例子逐步演示 logits → 温度 → 采样 |
| [tests/test_ch04.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_ch04.py) | 第 4 章的回归测试，含温度缩放的参数校验与生成函数的 EOS 停止测试 |
| reasoning_from_scratch/ch02.py（已学） | `generate_text_basic_stream_cache`：本讲生成函数的原型骨架 |
| reasoning_from_scratch/ch03.py（已学） | `load_model_and_tokenizer` / `render_prompt`：实践环节加载模型用 |

## 4. 核心概念与源码讲解

### 4.1 起点：用 plot_scores_bar「看见」logits

#### 4.1.1 概念说明

温度采样的一切都发生在「下一个 token 的打分」上，所以在动手改代码之前，先要把这个看不见的 151 936 维向量画出来。`plot_scores_bar` 是 notebook 提供的「显微镜」：从词表里截取一段 100 个 token 的窗口，把它们的 logit 画成柱状图。

notebook 的演示 prompt 是 `"The capital of Germany is"`。模型一次前向后取出**最后一个位置**的 logits（shape 为 `(1, 151936)`，回顾 u2-l1：每个位置都输出全词表 logits，而自回归生成只关心末位），直接 `argmax` 就得到 token id 19846，解码为 `' Berlin'`——这就是贪心解码在一步内的全部动作。

#### 4.1.2 核心流程

```text
prompt 文本
  → tokenizer.encode            # 得到 token id 序列，如 [785, 6722, 315, 9856, 374]
  → model(input_ids)[:, -1]     # 一次前向，取末位 logits，shape (1, 151936)
  → torch.argmax                # 贪心：取分数最高的 token id
  → tokenizer.decode            # ' Berlin'
```

#### 4.1.3 源码精读

先看 notebook 中取 argmax 的那一步（教学代码，与库中逻辑一致）：

- [ch04/01_main-chapter-code/ch04_main.ipynb:L518](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch04/01_main-chapter-code/ch04_main.ipynb#L518)：`max_token_id = torch.argmax(next_token_logits)`——对 `(1, V)` 的 logits 沿最后一维取最大值下标，得到全局最优 token id。notebook 输出为 `Token ID: 19846`，解码为 `' Berlin'`。

再看库里的可视化函数：

- [reasoning_from_scratch/ch04.py:L47-L59](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch04.py#L47-L59)：`plot_scores_bar` 的入参是 `next_token_logits`（`(1, V)` 张量），默认只截取词表下标 19 800–19 900 这 100 个 token 的窗口（`x = torch.arange(start, end)`），并 `.float().cpu()` 转成 CPU 上的 float32 以便 matplotlib 绘图——注意这一步只影响画图，不影响计算。
- [reasoning_from_scratch/ch04.py:L66-L76](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch04.py#L66-L76)：`arrow=True` 时在窗口内 argmax 并用箭头标注最高柱——notebook 里标注的正是 "Berlin"，直观展示「贪心选的就是最高的那根柱子」。
- 这个函数**只读不改** logits，后面还会被复用来画概率分布（把 `ylabel` 换成 "Probability value"）。

#### 4.1.4 代码实践

1. **实践目标**：亲手复现 notebook 的 Berlin 例子，并观察 top-5 候选。
2. **操作步骤**（示例代码，基于 notebook 4.4.1 节改写）：

```python
import torch
from reasoning_from_scratch.ch02 import get_device
from reasoning_from_scratch.ch03 import load_model_and_tokenizer

device = torch.device("cpu")  # 第 4 章 notebook 明确用 CPU 演示
model, tokenizer = load_model_and_tokenizer(which_model="base", device=device)

ex_prompt = "The capital of Germany is"
input_token_ids = torch.tensor(tokenizer.encode(ex_prompt), device=device).unsqueeze(0)

with torch.inference_mode():
    next_token_logits = model(input_token_ids)[:, -1]   # shape (1, 151936)

top5 = torch.topk(next_token_logits.squeeze(0), k=5)
for score, tid in zip(top5.values, top5.indices):
    print(f"{tid.item():>6}  {score.item():+.3f}  {tokenizer.decode([tid])!r}")
```

3. **需要观察的现象**：打印出的 5 个候选里应当有 `' Berlin'`（id 19846），且它的分数最高。
4. **预期结果**：top-5 大概率是德国城市相关 token + 少量格式 token；具体分数与机器有关（bfloat16 精度与后端会影响末位），首名应为 `' Berlin'`。
5. 精确的分数数值**待本地验证**（notebook 只展示了 argmax 结果 19846）。

#### 4.1.5 小练习与答案

**练习 1**：为什么取 logits 时要写 `[:, -1]`，而不是直接用 `model(input_token_ids)` 的输出？

**答案**：模型对输入的**每个位置**都输出一份全词表 logits，输出 shape 是 `(batch, seq_len, vocab_size)`。自回归生成只关心「接着最后一个 token 该出什么」，所以沿 seq_len 维取最后一个位置，得到 `(batch, vocab_size)`。

**练习 2**：`plot_scores_bar` 默认只画下标 19 800–19 900 的窗口，为什么不全画？

**答案**：词表有 151 936 个 token，全画出来既慢又看不清。恰好 19 846（Berlin）落在 19 800–19 900 这个窗口内，截取 100 个柱子足以展示「有一根柱子远高于其他」的形态。

### 4.2 温度缩放：scale_logits_by_temperature 与 softmax 概率化

#### 4.2.1 概念说明

「温度」借自统计力学：**温度越高，系统越混乱（分布越平）；温度越低，系统越有序（分布越尖）**。在语言模型里，它的实现简单到只有一行——在 softmax **之前**把 logits 除以 T：

\[ p_i(T) = \frac{e^{z_i/T}}{\sum_{j=1}^{V} e^{z_j/T}} \]

为什么「除以 T」能改变分布形状？关键在指数。除法发生在指数**里面**，等价于把 logit 之间的差距整体缩放为原来的 \( 1/T \) 倍：T < 1 时差距被放大，高分 token 的概率被指数级抬高（**锐化**）；T > 1 时差距被压缩，各 token 概率趋于接近（**平滑**）。

用一个只有两个 token 的迷你例子手算（logits 为 \( z = (2.0,\ 1.0) \)）：

| T | 计算 | 概率分布 | 效果 |
| --- | --- | --- | --- |
| 0.5 | \( e^{4} : e^{2} \approx 54.60 : 7.39 \) | (0.881, 0.119) | 锐化：强者更强 |
| 1.0 | \( e^{2} : e^{1} \approx 7.39 : 2.72 \) | (0.731, 0.269) | 原始分布 |
| 2.0 | \( e^{1} : e^{0.5} \approx 2.72 : 1.65 \) | (0.622, 0.378) | 平滑：差距缩小 |

两个重要极限：

- \( T \to 0 \)：差距被无限放大，分布退化为 one-hot——概率 1 落在 argmax 上，采样等价于贪心。这正是 4.4 节生成函数里 T=0 分支的数学依据。
- \( T \to \infty \)：所有 logit 差距被压平，分布趋近**均匀分布**——采样变成「在全词表里完全随机抽」，生成内容将不可读。

还有一个容易忽略的性质：**温度不改变排序**。除以正数 T 不改变 logits 的大小顺序，所以无论 T 取多少，argmax 选出的 token 始终相同。温度只改变「第一名赢多少」，不改变「谁是第一名」。

#### 4.2.2 核心流程

```text
logits z  (1, V)
  → z / T                # scale_logits_by_temperature，T 必须 > 0
  → softmax(z / T)       # 概率化，和为 1
  → 送入 4.3 节的采样器
```

#### 4.2.3 源码精读

温度缩放函数本体只有 4 行：

- [reasoning_from_scratch/ch04.py:L83-L86](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch04.py#L83-L86)：`scale_logits_by_temperature` 先做防御性检查——`temperature <= 0` 直接抛 `ValueError("Temperature must be positive")`，然后返回 `logits / temperature`。除法是对整个张量逐元素进行的，shape 不变。
- notebook 中同一函数的定义见 [ch04/01_main-chapter-code/ch04_main.ipynb:L610-L613](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch04/01_main-chapter-code/ch04_main.ipynb#L610-L613)。

配套的可视化函数把三条曲线叠在一张图上：

- [reasoning_from_scratch/ch04.py:L99-L112](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch04.py#L99-L112)：`plot_logits_with_temperature` 对 `temps=(0.5, 5.0)` 各调用一次 `scale_logits_by_temperature`（L100-L102 的列表推导），分别以 `(sharper)`、`(flatter)` 标注。T=0.5 的曲线是原图纵向拉伸 2 倍，T=5.0 是压缩到 1/5——形状相似，幅度不同。

notebook 随后演示了 T=5.0 的完整「缩放 + 概率化」两步：

- [ch04/01_main-chapter-code/ch04_main.ipynb:L728-L731](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch04/01_main-chapter-code/ch04_main.ipynb#L728-L731)：`rescaled_logits = scale_logits_by_temperature(next_token_logits, 5.0)` 之后 `torch.softmax(rescaled_logits, dim=-1)` 得到 `next_token_probas`。notebook 打印 `Probability sum: tensor(1., ...)` 验证归一化，且 `' Berlin'` 的概率只剩 `0.0003`——温度 5.0 把「压倒性优势」抹平成了「几乎人人平等」。

#### 4.2.4 代码实践

1. **实践目标**：验证「温度不改变排序，只改变概率」。
2. **操作步骤**（示例代码，无需加载模型）：

```python
import torch
from reasoning_from_scratch.ch04 import scale_logits_by_temperature

z = torch.tensor([[2.0, 1.0, -0.5, 0.3]])
for T in (0.5, 1.0, 2.0):
    p = torch.softmax(scale_logits_by_temperature(z, T), dim=-1)
    print(f"T={T}: probas={p.tolist()}, argmax={p.argmax().item()}")

# 验证防御性检查
try:
    scale_logits_by_temperature(z, 0.0)
except ValueError as e:
    print("ValueError:", e)
```

3. **需要观察的现象**：三行输出的 `argmax` 都是 0（即 logit 最大的那个位置），但 T=0.5 时第一个概率明显更大、T=2.0 时更小；`T=0.0` 触发 `ValueError: Temperature must be positive`。
4. **预期结果**：排序不变、概率形状按 4.2.1 表格的方向变化；异常消息与源码 L85 完全一致。这一行为同时有回归测试锁定：[tests/test_ch04.py:L73-L79](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_ch04.py#L73-L79) 断言 `logits/2.0` 的缩放结果与 `T=0.0` 抛错两个事实。
5. 本实践不依赖模型权重，可在任何装有 torch 的环境直接运行。

#### 4.2.5 小练习与答案

**练习 1**：如果把 T 设成 -1 会发生什么？softmax 还能产出合法概率吗？

**答案**：`scale_logits_by_temperature` 会在入口处抛 `ValueError`，因为 `temperature <= 0`。即使没有这层检查，除以负数会**翻转 logits 的排序**（最大变最小），softmax 虽然仍输出和为 1 的向量，但语义完全错了——模型最不想要的 token 反而概率最高。所以防御性检查是必要的。

**练习 2**：为什么不直接对 softmax 之后的概率除以 T，而要在 softmax 之前除 logits？

**答案**：对概率线性除以 T 会让总和不再等于 1，而且线性缩放**指数级地**改变不了相对优势。在 softmax 前除以 T，利用指数的非线性，让 \( 1/T \) 的缩放被放大为概率上的指数级变化，才有了 T→0 退化为 one-hot、T→∞ 退化为均匀分布这两个漂亮极限。

### 4.3 按概率抽签：torch.multinomial 与 count_samples

#### 4.3.1 概念说明

有了概率分布，还需要一个「抽签器」从中选 token。`torch.multinomial(probas, num_samples=1)` 的语义是：把词表看成一个巨大的轮盘，每个 token 占据与其概率成正比的扇形，转一次轮盘抽出一个下标。单次抽签结果随机，但**长期频率收敛于概率本身**——这正是 2.4 节说的大数定律。

`count_samples` 是作者为教学写的「频率计」：抽 1000 次签，统计每个 token 中签次数并打印。它让我们不画图也能定量回答「温度到底把分布变成了什么样」。

需要提前认识的一个实现细节：`torch.multinomial` 默认**无放回**（`replacement=False`），那样抽 1000 次会直接报错（样本数不能超过总体数）或语义不对。所以这里必须传 `replacement=True`——每次抽签后「放回」，各次抽样相互独立，频率才能逼近同一组概率。

#### 4.3.2 核心流程

```text
probas (1, V)
  → torch.multinomial(probas.cpu(), num_samples=1000, replacement=True)
      # 返回 (1, 1000)：1000 个抽中的 token 下标
  → torch.bincount(samples.squeeze(0))
      # 数出每个下标中签次数
  → 只打印次数 > threshold 的 token（过滤噪声，避免打印 15 万行）
```

#### 4.3.3 源码精读

- [reasoning_from_scratch/ch04.py:L132-L136](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch04.py#L132-L136)：`count_samples` 的抽签步骤——`torch.multinomial(probas.cpu(), num_samples=num_samples, replacement=True)`。注意 `.cpu()`：无论 probas 在哪个设备，统计前先搬到 CPU。
- [reasoning_from_scratch/ch04.py:L139](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch04.py#L139)：`torch.bincount(samples.squeeze(0), minlength=1)` 把 `(1, 1000)` 压成一维后逐下标计数，得到长度为「最大下标+1」的计数向量。
- [reasoning_from_scratch/ch04.py:L142-L147](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch04.py#L142-L147)：打印循环。`threshold=1` 表示只打印中签 ≥ 2 次的 token；传入 `tokenizer` 时打印 token 文本（更可读），否则打印词表下标。

notebook 用它做了两组对照实验，结果非常有说服力：

- [ch04/01_main-chapter-code/ch04_main.ipynb:L886](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch04/01_main-chapter-code/ch04_main.ipynb#L886)：对 **T=5.0** 的分布抽 1000 次，输出只有零星几个 token 各中 2 次（如 `'}': 2x`、`' represent': 2x`）——分布几乎均匀，没有任何 token 能形成气候。
- [ch04/01_main-chapter-code/ch04_main.ipynb:L910-L914](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch04/01_main-chapter-code/ch04_main.ipynb#L910-L914)：改为 **T=0.35** 再抽 1000 次，输出 `' Berlin': 435x`、`' ______': 209x`、`' ____': 169x`、`' __': 158x`、`' _____': 18x`、`' Munich': 3x`、`' Hamburg': 3x`。notebook 还单独打印了 `probas_lowT[0, 19846] = 0.4199`——理论概率 0.4199 对应实测频率 435/1000 = 0.435，频率与概率对上了。

这两组数字合起来就是温度的完整故事：**同一个模型、同一份 logits，仅仅因为除以 0.35 还是 5.0，「Berlin」的中签率就从几乎为零变成 43.5%**。T=0.35 的结果里那些下划线 token（`' ____'`）也值得一提——低温下模型「认真」了起来，第二名不是别的城市，而是「出填空题」的格式续写，这是基座模型训练数据留下的偏好。

#### 4.3.4 代码实践（本讲核心实践之一）

1. **实践目标**：用 10 个 token 的迷你词表，亲手复现「温度 → 分布 → 频率」整条链路。
2. **操作步骤**（示例代码，沿用 notebook 4.5.1 节的 toy logits）：

```python
import torch
from reasoning_from_scratch.ch04 import scale_logits_by_temperature, count_samples

toy_logits = torch.tensor([[-0.7, -3.0, 0.1, -1.2, 2.0, -1.0, -0.5, -2.0, 0.3, 1.5]])

for T in (0.5, 1.0, 2.0):
    probas = torch.softmax(scale_logits_by_temperature(toy_logits, T), dim=-1)
    print(f"\n=== T={T} ===")
    print("probas :", [round(p, 4) for p in probas.squeeze(0).tolist()])
    torch.manual_seed(123)
    count_samples(probas, num_samples=1000, threshold=1)
```

3. **需要观察的现象**：
   - 三次的理论概率列：T=0.5 时最大者（下标 4，logit=2.0）显著超过 0.8；T=2.0 时它跌到 0.4 以下，其余 token 概率集体抬升。
   - 三次的频率计数与各自的理论概率同向：T 越小，下标 4 的中签次数越多。
   - argmax 在三次里都是下标 4——再次验证温度不改变第一名。
4. **预期结果**：以 T=1.0 为例，softmax 后下标 4 的概率约 0.4538（notebook 4.5.1 节同一组 logits 算出的第一名概率正是 0.4538），1000 次采样应中签约 450 次上下浮动；T=0.5 时该值应明显上升（锐化），T=2.0 时明显下降（平滑）。具体频率数值随种子浮动，属正常统计涨落。
5. 三个温度的精确频率值**待本地验证**（取决于随机种子与 torch 版本）。

#### 4.3.5 小练习与答案

**练习 1**：`count_samples` 里如果不传 `replacement=True` 会怎样？

**答案**：`torch.multinomial` 默认无放回，`num_samples=1000` 大于概率向量长度（本例只有 10，真实词表 151 936 时虽不报错但语义错误），要么直接报错，要么抽出的 1000 个下标互不重复——「频率逼近概率」的前提（独立同分布重复抽样）被破坏，统计结果失去意义。

**练习 2**：T=0.35 的实验里，`' Berlin'` 理论概率 0.4199、实测 435 次。如果改成抽 10 000 次，实测频率会怎么变？

**答案**：大数定律下频率会**更接近** 0.4199（约 4200 次上下，相对涨落按 \( 1/\sqrt{N} \) 缩小）。抽样次数越多，频率估计越准。

**练习 3**：`threshold` 参数的作用是什么？为什么默认是 1？

**答案**：只打印中签次数**大于** threshold 的 token。默认 1 即只显示中签 ≥ 2 次的 token，把只中签 0 或 1 次的长尾过滤掉——否则 151 936 个 token 每个都打印一行会淹没真正有用的信息。

### 4.4 生成函数改造：generate_text_temp_stream_cache

#### 4.4.1 概念说明

前面三节都在「单步」层面工作，本节把温度接入自回归生成循环，得到第一个**随机解码**的生成函数。它的骨架完全继承自 u3-l2 讲过的 `generate_text_basic_stream_cache`（流式 + KV 缓存），改动集中在循环内「如何选下一个 token」这一处——源码里作者特意用 `# NEW:` 注释框出了这块改动。

三个设计要点：

1. **T=0 / None 走 argmax 分支**。注意不是「除以一个很小的 T 来近似贪心」，而是显式分支直接 `torch.argmax`。这既避免了除零，又保证 T=0 的行为与第 2 章贪心解码**逐位一致**。数学上（4.2.1 节）T→0 的极限就是 one-hot argmax，代码实现取的正是这个极限的精确版本。
2. **默认参数 `temperature=0.`**。默认行为与旧函数完全相同（确定、可复现）， randomness 需要显式开启——这是保持向后兼容的接口设计。
3. **防御性检查与分支的分工**。`scale_logits_by_temperature` 的 `ValueError` 只针对「直接误用辅助函数」的场景；生成函数内部因为先做了 T=0 判断，正常调用路径永远不会触发这个异常。

#### 4.4.2 核心流程

```text
函数入口
  model.eval()                          # 关闭 dropout 等（推理模式）
  cache = KVCache(n_layers)             # 新建 KV 缓存
  model.reset_kv_cache()                # 清零位置游标（u2-l5）
  out = model(token_ids, cache)[:, -1]  # prefill 整段前向，取末位 logits
循环 step = 0 .. max_new_tokens-1:
  if T 为 None 或 0.0:
      next_token = argmax(out)                    # 贪心分支（NEW 的另一半）
  else:
      logits = out / T                            # Step 3.2 温度缩放
      probas = softmax(logits)                    # Step 3.3 概率化
      next_token = multinomial(probas.cpu(), 1)   # Step 3.4 抽签
      next_token = next_token.to(orig_device)     # 搬回原设备
  if next_token == eos_token_id: break            # EOS 提前停止（EOS 不产出）
  yield next_token                                # 流式交出（u3-l2）
  if step+1 < max_new_tokens:
      out = model(next_token, cache)[:, -1]       # decode 单 token 前向，末位之后跳过最后一次
```

#### 4.4.3 源码精读

- [reasoning_from_scratch/ch04.py:L150-L160](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch04.py#L150-L160)：函数签名与初始化。`@torch.inference_mode()` 关闭梯度追踪；`temperature=0.` 是默认值。`cache = KVCache(...)` + `model.reset_kv_cache()` 成对出现——KV 缓存生命周期收进函数内（u3-l2 的结论），自愈调用方遗留的脏状态。
- [reasoning_from_scratch/ch04.py:L163-L164](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch04.py#L163-L164)：prefill——整段 prompt 一次前向，`[:, -1]` 取末位 logits 进入循环。
- [reasoning_from_scratch/ch04.py:L168-L171](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch04.py#L168-L171)：**贪心分支**。`temperature is None or temperature == 0.0` 时 `torch.argmax(out, dim=-1, keepdim=True)` 直接取最优，`keepdim=True` 保证输出 shape 为 `(b, 1)`，与采样分支的输出 shape 完全一致——两个分支可以无缝互换。
- [reasoning_from_scratch/ch04.py:L174-L182](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch04.py#L174-L182)：**采样分支**，就是 4.2、4.3 两节的三步流水线：L175 温度缩放 → L178 softmax → L181 `torch.multinomial(probas.cpu(), num_samples=1)`。L168 先保存了 `orig_device = token_ids.device`，L182 再 `next_token.to(orig_device)` 把抽到的 token 搬回模型所在设备（CUDA/MPS/CPU 都适用），循环内后续前向不受影响。
- [reasoning_from_scratch/ch04.py:L185-L187](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch04.py#L185-L187)：EOS 提前停止。检查放在 `yield` **之前**，所以 EOS 本身不会出现在输出里（与 u3-l1 的结论一致）；`torch.all(...)` 的写法让它天然兼容批量场景。这一行为有回归测试：[tests/test_ch04.py:L110-L126](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_ch04.py#L110-L126) 用一个永远输出固定 token 的假模型 + `eos_token_id` 验证「一个 token 都不产出、且 eval/reset_kv_cache 都被调用」。
- [reasoning_from_scratch/ch04.py:L189-L191](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch04.py#L189-L191)：流式产出与「跳过最后一次前向」优化——最后一个 token 交出后不再前向，直接结束（u3-l2 已详述）。

调用侧的胶水函数也值得一提——温度参数正是通过它传进来的：

- [reasoning_from_scratch/ch04.py:L13-L34](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch04.py#L13-L34)：`generate_text_stream_concat_flex` 用 `generate_func=None` + `**generate_kwargs` 两个入口，把「用哪个生成函数」和「传给它什么参数」都做成可插拔的：不指定时回落到第 2 章的贪心流式函数，指定 `generate_func=generate_text_temp_stream_cache, temperature=1.1` 时 `**generate_kwargs` 会把 `temperature` 原样转发（L33）。notebook 的调用方式见 [ch04/01_main-chapter-code/ch04_main.ipynb:L1021-L1026](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch04/01_main-chapter-code/ch04_main.ipynb#L1021-L1026)。

notebook 在 4.4.4 节用 `temperature=1.1` 对那道数学题生成，得到的答案是 `\boxed{$x = \frac{90}{7}$}`——而贪心解码给的是 `\boxed{20}`，正确答案是 83。这提示了一个重要事实：**温度带来多样性的同时也带来方差**，单次随机采样未必比贪心好，需要后续的 top-p 截断（u5-l2）和自一致性投票（u5-l4）来把「多样性」转化为「准确率」。

#### 4.4.4 代码实践（本讲核心实践之二）

1. **实践目标**：对比 T=0（确定）与 T=1.2（随机）两种生成行为，验证「T=0 与贪心一致、T>0 受种子控制」。
2. **操作步骤**（示例代码，CPU 即可，第 4 章 notebook 就是用 CPU 演示的）：

```python
import torch
from reasoning_from_scratch.ch02 import get_device
from reasoning_from_scratch.ch03 import load_model_and_tokenizer, render_prompt
from reasoning_from_scratch.ch04 import (
    generate_text_temp_stream_cache,
    generate_text_stream_concat_flex,
)

device = torch.device("cpu")
model, tokenizer = load_model_and_tokenizer(which_model="base", device=device)

raw_prompt = "Half the value of $3x-9$ is $x+37$. What is the value of $x$?"
prompt = render_prompt(raw_prompt)

# --- T=0：应与贪心解码完全一致，且与随机种子无关 ---
for _ in range(2):
    torch.manual_seed(0)   # 故意换种子也不该影响 T=0
    out0 = generate_text_stream_concat_flex(
        model, tokenizer, prompt, device,
        max_new_tokens=128,                    # 控制时长
        generate_func=generate_text_temp_stream_cache,
        temperature=0.0,
    )
    print("T=0  ->", repr(out0.strip()))

# --- T=1.2：同种子可复现，换种子应得到不同输出 ---
torch.manual_seed(123)
out_a = generate_text_stream_concat_flex(
    model, tokenizer, prompt, device, max_new_tokens=128,
    generate_func=generate_text_temp_stream_cache, temperature=1.2,
)
torch.manual_seed(123)     # 相同种子 → 相同输出
out_b = generate_text_stream_concat_flex(
    model, tokenizer, prompt, device, max_new_tokens=128,
    generate_func=generate_text_temp_stream_cache, temperature=1.2,
)
torch.manual_seed(7)       # 不同种子 → 期望不同输出
out_c = generate_text_stream_concat_flex(
    model, tokenizer, prompt, device, max_new_tokens=128,
    generate_func=generate_text_temp_stream_cache, temperature=1.2,
)
print("T=1.2 seed123 ==seed123 :", out_a == out_b)
print("T=1.2 seed123 == seed7  :", out_a == out_c)
print("T=1.2 样例:", repr(out_a.strip()[:120]))
```

3. **需要观察的现象**：
   - 两次 T=0 生成的输出**逐字符相同**（argmax 不消耗随机数，种子无影响）。
   - T=1.2 且种子相同时两次输出相同（`torch.manual_seed` 完全控制 `torch.multinomial`）；种子不同时输出**大概率**不同。
   - T=1.2 的答案内容与 T=0 不同，且多次重跑内容会漂移。
4. **预期结果**：三个布尔/文本对比如上；具体生成文本**待本地验证**（依赖设备、精度与种子）。若想看更强的多样性，把 T 提到 1.5 并对比即可。
5. 若尚未下载权重，`load_model_and_tokenizer` 会自动触发下载（u1-l3 讲过的下载机制），0.6B 权重约 1.2 GB。

#### 4.4.5 小练习与答案

**练习 1**：源码里 T=0 为什么不直接调用 `scale_logits_by_temperature(out, 1e-9)` 来「近似」贪心，而要单独写 argmax 分支？

**答案**：三个理由：① 极小的 T 会把 logits 除成巨大的数，softmax 指数运算可能数值溢出（inf/NaN）；② argmax 是精确的，数值近似永远有舍入风险；③ 显式分支让「T=0 == 贪心」成为接口契约而不是巧合，回归测试也能直接锁定该行为。

**练习 2**：`generate_text_temp_stream_cache` 的默认参数是 `temperature=0.`。如果不传 temperature 直接调用，它和第 2 章的 `generate_text_basic_stream_cache` 行为有什么差别？

**答案**：选 token 的路径完全一致（都走 argmax）；KV 缓存管理、EOS 提前停止、跳过最后一次前向也一致。差别只在代码层面多了一个永远不走的采样分支，以及采样分支里的 `.cpu()`/`.to(orig_device)` 设备往返不会发生。对 T=0 而言两者是行为等价的。

**练习 3**：为什么采样分支要写 `probas.cpu()` 再 `next_token.to(orig_device)`，而贪心分支不需要？

**答案**：`torch.multinomial` 这里被安排在 CPU 上执行（`count_samples` 中同样如此），所以先 `.cpu()`；抽完的 token 下标必须回到模型所在的设备（`orig_device`），否则下一步 `model(next_token, cache=cache)` 会因输入与权重设备不一致而报错。贪心分支的 `argmax` 直接在 `out` 所在设备上完成，天然与模型同设备，无需搬运。

## 5. 综合实践：温度扫描与「重合率」实验

把本讲的四块知识串成一个小实验：**衡量「生成结果的确定性如何随温度变化」**。

1. **实践目标**：对同一 prompt 在多个温度下各生成两次，统计「两次输出是否完全相同」，把 4.2 的数学、4.3 的频率思想、4.4 的生成函数连成一条线。
2. **操作步骤**（示例代码，接 4.4.4 的模型加载）：

```python
temps = [0.0, 0.5, 0.8, 1.2, 1.6]
for T in temps:
    outs = []
    for seed in (123, 456):                    # 两个不同种子各生成一次
        torch.manual_seed(seed)
        outs.append(generate_text_stream_concat_flex(
            model, tokenizer, prompt, device, max_new_tokens=128,
            generate_func=generate_text_temp_stream_cache, temperature=T,
        ))
    same = "相同" if outs[0] == outs[1] else "不同"
    print(f"T={T}: 两次输出{same} | 长度 {len(outs[0])} vs {len(outs[1])} 字符")
```

3. **需要观察的现象**：
   - T=0：两次输出必然相同（argmax 与种子无关）。
   - T 从 0.5 往上：随着 T 升高，两次输出「不同」的概率越来越大；1.6 时几乎必然不同，且文本开始出现语法或事实上的劣化（对应分布被过度平滑）。
   - 把温度固定、把两个种子换成三四个种子重复实验，「互不相同」的比例会进一步上升——这正是 u5-l4 自一致性投票需要的多样性来源。
4. **预期结果**：得到一张「温度 → 确定性」的定性单调表；T=1.6 附近的输出质量下降是**高温平滑**的直接代价，也是下一讲引入 top-p（在保留高温多样性的同时砍掉长尾垃圾 token）的动机。具体每个温度的输出文本**待本地验证**。
5. **延伸思考**（选做）：用 4.3 节的 `count_samples` 对某一步的 logits 在不同 T 下各抽 1000 次，观察「中签 ≥ 2 次的 token 个数」随 T 的变化——这个数量正是分布集中程度的粗略度量（分布越尖，能越过 threshold 的 token 越少）。

## 6. 本讲小结

- **温度缩放只有一行**：softmax 之前 `logits / T`（[ch04.py:L83-L86](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch04.py#L83-L86)），但凭借指数的非线性，T<1 锐化、T>1 平滑，T→0 退化为 one-hot、T→∞ 退化为均匀分布；温度**不改变 token 排序**，只改变概率差距。
- **采样与统计**：`torch.multinomial(..., replacement=True)` 按概率抽签，`count_samples`（[ch04.py:L132-L147](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch04.py#L132-L147)）用 1000 次采样的频率还原分布——notebook 实测：T=5.0 时无 token 中签超过 2 次，T=0.35 时 ' Berlin' 中签 435 次、对应理论概率 0.4199。
- **生成函数的三步采样流水线**：`generate_text_temp_stream_cache`（[ch04.py:L150-L191](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch04.py#L150-L191)）在继承第 2 章流式 + KV 缓存骨架的同时，把「argmax 选 token」替换为「温度缩放 → softmax → multinomial」；`temperature=0`/`None` 走显式 argmax 分支，保证与贪心解码逐位一致且不受种子影响。
- **可复现性由 `torch.manual_seed` 控制**：T>0 时相同种子得到相同输出，不同种子得到（大概率）不同输出；这是后续自一致性、GRPO rollout 能「可控地多样化」的基础。
- **多样性的双刃剑**：notebook 中 T=1.1 单次采样给出了错误答案，说明随机性本身不提升准确率——它提供的是「多条不同推理链」的原料，需要 top-p 截断与多数投票才能转化为收益。
- **接口设计的两个细节**：辅助函数以 `ValueError` 拒绝非法 T（[tests/test_ch04.py:L73-L79](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_ch04.py#L73-L79) 锁定），生成函数则把 T=0 语义化为贪心；`generate_text_stream_concat_flex` 的 `generate_func` + `**generate_kwargs` 让解码策略可插拔。

## 7. 下一步学习建议

- **下一讲 u5-l2（Top-p 过滤）**：本讲留下了「高温平滑带来垃圾 token」的问题，`top_p_filter`（[ch04.py:L194-L222](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch04.py#L194-L222)）通过只保留累积概率前 top_p 的 token 来解决，它作用在本讲产出的概率分布上，二者在 `generate_text_top_p_stream_cache` 中组合。
- **u5-l4（自一致性投票）**：`self_consistency_vote`（[ch04.py:L270-L319](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch04.py#L270-L319)）把本讲的采样函数调用 N 次再投票，是「温度多样性 → 准确率」的第一条完整路线。
- **源码预习**：`generate_text_top_p_stream_cache`（[ch04.py:L225-L267](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch04.py#L225-L267)）与本讲的 `generate_text_temp_stream_cache` 只差一行 top-p 过滤，对照阅读可以最快掌握它。
- **远期连接**：第 6 章的 `sample_response`（rollout 采样）复用的正是本讲的「温度缩放 + softmax + multinomial」三步，届时随机性将从「评测技巧」升级为「强化学习的探索机制」。
