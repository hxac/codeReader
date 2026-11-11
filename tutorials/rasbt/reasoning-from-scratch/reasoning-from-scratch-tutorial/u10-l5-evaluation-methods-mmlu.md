# 更多评测方法：MMLU、排行榜与 LLM-as-judge（附录 F）

## 1. 本讲目标

第 3~5 章的评测始终围绕一条链路：自由生成 → 提取 `\boxed{}` → SymPy 判分（u4-l4）。它属于「**生成式评测 + 可验证器**」范式，但业界还有另外三类完全不同的评测范式。本讲概览附录 F（`chF/`），把它们一次讲清：

1. **选择题准确率评测（MMLU）**：模型面对 A/B/C/D 四选一，掌握三种由浅入深的实现——字母匹配、logprob 比较、教师强制，外加一个随机猜测基线；
2. **偏好排行榜**：从「模型 A 打败了模型 B」的成对胜负中还原整体排名——手写 Elo 在线更新，再用 PyTorch 拟合 Bradley-Terry 统计模型；
3. **LLM-as-judge**：用一个更大的模型（Ollama 本地运行的 gpt-oss:20b）按 1~5 分量表给候选模型的回答打分。

学完本讲你应该能够：

- 说出三种 MMLU 方法各自的实现要点、失败模式与适用场景，并解释为什么同一个 0.6B 模型在三种方法下得分差异巨大（21.48% → 34.44%）；
- 手推一场比赛的 Elo 更新，说出 Elo 与 Bradley-Terry 的数学联系（`R = s · 400 / ln 10`）与本质区别（在线顺序更新 vs 全局最大似然拟合）；
- 读懂 Ollama 的流式 REST API 调用与 rubric 评分提示词，理解裁判输出的解析陷阱；
- 会用 `datasets` 库加载 MMLU，用 `appendix_f.elo_ratings` 处理成对胜负数据。

## 2. 前置知识

### 2.1 从 MATH-500 到三类新范式

回顾 u4-l4：MATH-500 评测是「开放生成 + 客观验证器」，模型要自己写出解题过程，验证器只检查最终答案。它的好处是**难以作弊**（答案对就是对），坏处是**只适用于有客观对错的任务**。本讲的三类范式覆盖另外的场景：

| 范式 | 问题形态 | 判分者 | 本讲载体 |
|---|---|---|---|
| 选择题准确率 | 四选一 | 标准答案字母 | `chF/02_mmlu/` 四个脚本 |
| 偏好排行榜 | 两两对比「哪个更好」 | 人类投票 | `chF/03_leaderboards/` |
| LLM 裁判 | 任意开放式回答 | 另一个 LLM | `chF/04_llm-judge/` |

### 2.2 MMLU 与 `datasets` 库

MMLU（Massive Multitask Language Understanding）是一个多选题基准：每题有 `question`（题干）、`choices`（恰好四个选项文本）与 `answer`（**整数索引 0~3**，指向正确选项）三个关键字段。它按学科分成几十个子集（subset/config），本讲用的是题量最小之一的 `high_school_mathematics`（测试集 270 题）。

数据从 Hugging Face Hub 用 `datasets` 库加载。**这不是本项目的依赖**——`pyproject.toml` 里没有它，`chF/02_mmlu/requirements-extra.txt` 单独声明了 `datasets >= 4.1.1`，需要 `pip install datasets`（或 `uv add datasets`）后才能跑本讲脚本。

### 2.3 logprob 与 teacher forcing 回顾

u6-l1 已建立核心直觉：因果语言模型喂入一段文本做一次前向，**每个位置的 logits 都是该位置对下一个 token 的合法概率分布**；`log_softmax` 后用错位索引（或 `gather`）即可抽出「真实下一个 token」的分数。本讲方法二只用「最后一个位置的分布」，方法三则把整段候选答案 teacher-force 进去后逐位置取分数再平均——与 u9-l3 蒸馏损失共用同一套「`prompt_len - 1` 起切片」的下标约定。

### 2.4 Elo 与 Bradley-Terry 的直觉

- **Elo**（国际象棋排名）：每个选手一个分数 R；赛前按分差算期望胜率；赛后赢家加分、输家扣分。它是**在线算法**——一场一场顺序更新，结果依赖比赛顺序。
- **Bradley-Terry**：一个统计模型，假设每个选手有潜在实力分 \( s_i \)，胜负概率由实力差决定；用**全部比赛数据一次性最大似然拟合**。LM Arena（原 Chatbot Arena）正是从 Elo 切换到了 Bradley-Terry，再把分数缩放到 Elo 尺度方便对照。

### 2.5 LLM-as-judge 与 Ollama

让一个强模型扮演阅卷老师，按写好的**评分量表（rubric）**给候选回答打 1~5 分。裁判模型通过 Ollama 本地运行——Ollama 是 llama.cpp 的封装，把模型暴露成 `http://localhost:11434` 上的 REST 服务，Python 端只需 `requests` 即可调用（第 8 章蒸馏数据生成也用过它，见 u9-l1）。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [reasoning_from_scratch/appendix_f.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/appendix_f.py) | 附录 F 的库沉淀，仅两个函数：`predict_choice`（L9-L27）与 `elo_ratings`（L30-L55） |
| [chF/01_main-chapter-code/chF_main.ipynb](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chF/01_main-chapter-code/chF_main.ipynb) | 主 notebook：手工单题演示字母匹配、Elo 六场小例、Ollama 裁判最小流程 |
| [chF/02_mmlu/random_guessing_baseline.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chF/02_mmlu/random_guessing_baseline.py) | 随机猜测基线：gold 分布统计 + 一万次随机试验的准确率分布 |
| [chF/02_mmlu/1_letter_matching.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chF/02_mmlu/1_letter_matching.py) | 方法一：生成后提取首个 A/B/C/D 字母 |
| [chF/02_mmlu/2_logprob.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chF/02_mmlu/2_logprob.py) | 方法二：比较四个字母 next-token logprob，实践最常用 |
| [chF/02_mmlu/3_teacher_forcing.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chF/02_mmlu/3_teacher_forcing.py) | 方法三：teacher-force 完整答案文本取平均 logprob，最适合推理模型 |
| [chF/03_leaderboards/1_elo_leaderboard.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chF/03_leaderboards/1_elo_leaderboard.py) | Elo 排行榜命令行脚本（内嵌一份 `elo_ratings`） |
| [chF/03_leaderboards/2_bradley_terry_leaderboard.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chF/03_leaderboards/2_bradley_terry_leaderboard.py) | Bradley-Terry 排行榜：Adam 优化器拟合 + Elo 尺度缩放 |
| [chF/03_leaderboards/votes.json](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chF/03_leaderboards/votes.json) | 60 场模拟胜负对（4 个模型），两个脚本的公用输入 |
| [chF/04_llm-judge/ollama-judge.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chF/04_llm-judge/ollama-judge.py) | LLM 裁判：Qwen3 0.6B 作答 + gpt-oss:20b 按量表打分 |
| [tests/test_appendix_f.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_appendix_f.py) | Elo 回归测试：单场更新数值 + 总分守恒 |
| [chF/02_mmlu/requirements-extra.txt](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chF/02_mmlu/requirements-extra.txt) | 额外依赖声明：`datasets >= 4.1.1` |

## 4. 核心概念与源码讲解

本讲拆成五个最小模块：

1. **4.1 MMLU 数据与随机猜测基线**——先把「猜也能得 25%」的统计底座打牢；
2. **4.2 方法一与方法二：字母匹配 vs logprob 评分**——两种最常用实现，重点讲 token 对齐技巧；
3. **4.3 方法三：教师强制完整答案评分**——更适合推理模型的稳健方法；
4. **4.4 偏好排行榜：Elo 与 Bradley-Terry**——从成对胜负到整体排名的两条路线；
5. **4.5 LLM-as-judge：Ollama 裁判流水线**——rubric 提示、流式 API 与分数解析。

### 4.1 MMLU 数据与随机猜测基线

#### 4.1.1 概念说明

任何准确率数字都要有参照系才 meaningful。四选一题面上，**均匀随机猜的期望准确率是 25%**——如果一个模型得了 30%，先别急着夸，要问：随机猜的波动范围有多大？这道题的 gold 分布是否均匀（不均匀时「无脑猜 C」都能超过 25%）？`random_guessing_baseline.py` 就是在跑模型**之前**先把这些底线量出来：

- 统计子集内 gold 字母的真实分布（选择偏差检测）；
- 模拟一万次「均匀随机猜完全部 270 题」，输出准确率的均值、标准差与分位数（噪声幅度检测）。

#### 4.1.2 核心流程

```
random_guessing_baseline(subset)
  1. load_dataset("cais/mmlu", subset, split="test")
  2. labels = [gold_letter(ex["answer"]) for ex in ds]   # int 索引 → "ABCD"[ans]
  3. Counter 统计 gold 分布并打印
  4. 重复 trials 次：
       每题 rng.choice("ABCD") 均匀猜一个 → 算本次准确率
  5. 输出均值、标准差、分位数表、频率表
```

理论上，n 题、每题独立以 \( p=\tfrac14 \) 猜对，正确数 \( K \sim \mathrm{Binomial}(n, p) \)，准确率 \( A = K/n \) 的标准差为：

\[
\sigma_A = \sqrt{\frac{p(1-p)}{n}} = \sqrt{\frac{0.25 \times 0.75}{270}} \approx 2.64\%
\]

一万次模拟的经验标准差应与这个解析值几乎一致——这是脚本隐含的一条自检。

#### 4.1.3 源码精读

`gold_letter` 把数据集的 `answer` 字段统一成字母：int 索引直接查表，字符串则清洗为大写并兜底截首字符（[random_guessing_baseline.py:L14-L18](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chF/02_mmlu/random_guessing_baseline.py#L14-L18)）。这个「int → 字母」的转换在三个 MMLU 脚本里以同一行内联出现。

gold 分布统计：对每个字母打印次数与占比（[random_guessing_baseline.py:L46-L57](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chF/02_mmlu/random_guessing_baseline.py#L46-L57)）。作者在 `high_school_mathematics` 上实测记录（见 [02_mmlu/README.md](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chF/02_mmlu/README.md)）：

```text
A: 57 (21.11%)   B: 71 (26.30%)   C: 71 (26.30%)   D: 71 (26.30%)
```

注意 gold **并不均匀**——A 只有 57 题。这意味着「永远猜 B/C/D」的期望准确率是 26.30%，高于均匀猜的 25%。

随机试验主体：`random.Random(seed)` 固定种子保证可复现，每次试验独立猜满 n 题再算准确率（[random_guessing_baseline.py:L63-L76](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chF/02_mmlu/random_guessing_baseline.py#L63-L76)）。作者实测：均值 24.98%、标准差 2.65%，与解析值 \( 25\% \pm 2.64\% \) 吻合。分位数与频率表（[L79-L92](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chF/02_mmlu/random_guessing_baseline.py#L79-L92)）进一步给出完整分布形状。

#### 4.1.4 代码实践

1. **实践目标**：跑通随机基线，把 25% 的「噪声带」量化出来，作为后面三种方法的对照底座。
2. **操作步骤**：
   ```bash
   pip install datasets          # 或 uv add datasets
   cd chF/02_mmlu
   python random_guessing_baseline.py --subset high_school_mathematics
   ```
3. **需要观察的现象**：gold 四个字母的分布是否均匀；一万次试验的均值与标准差。
4. **预期结果**：与 README 记录一致——均值约 24.98%、标准差约 2.65%，gold 分布 A:57 / B:71 / C:71 / D:71。**待本地验证**（需联网下载数据集，约几 MB）。
5. 若不想装 `datasets`，可以纯 Python 复刻：`labels = [57, 71, 71, 71]` 按 README 记录硬编码后跑同样的模拟（示例代码）：

```python
import random, statistics
labels = ["A"]*57 + ["B"]*71 + ["C"]*71 + ["D"]*71
rng = random.Random(42)
accs = []
for _ in range(10_000):
    guesses = [rng.choice("ABCD") for _ in labels]
    accs.append(sum(g == y for g, y in zip(guesses, labels)) / len(labels))
print(statistics.mean(accs), statistics.stdev(accs))
```

#### 4.1.5 小练习与答案

**练习 1**：n=270、p=0.25 时，±1σ 的准确率区间约是多少？一个模型得了 28% 能说明它比随机强吗？

**答案**：\( 25\% \pm 2.64\% \)，即约 [22.4%, 27.6%]。28% 刚好在 1σ 之外（约 1.9σ），单次运行算「弱证据」；考虑到 gold 不均匀时最优常数猜测也有 26.3%，28% 只能说略高于噪声带，需更大题量或更极端的分数才能下结论。

**练习 2**：为什么脚本的模拟标准差（2.65%）和公式 \( \sqrt{p(1-p)/n} \)（2.64%）如此接近但不完全相等？

**答案**：公式给出的是总体标准差的理论值；模拟是 10 000 次抽样的经验估计，存在抽样误差；且随机猜测的对错在固定 labels 下并非严格同分布（gold 不均匀时每题猜对概率略有差异），两者共同造成微小偏差。

**练习 3**：如果把 `--trials` 从 10 000 改成 100，哪个输出统计量会变得不可靠？

**答案**：标准差与分位数（尤其是 1%/99% 尾部分位数）会剧烈抖动——经验分布的尾部需要足够多的抽样才能填满；均值仍大致稳定（其自身误差按 \(1/\sqrt{\text{trials}}\) 收敛）。

### 4.2 方法一与方法二：字母匹配 vs logprob 评分

#### 4.2.1 概念说明

两种方法共用同一个 zero-shot 提示模板：题干 + 四个选项 + `Answer: ` 结尾（尾随空格是刻意的，鼓励下一个 token 直接是单个字母）。区别在「怎么得到模型的 选择」：

- **方法一（字母匹配）**：让模型**真的生成**至多 8 个 token，在生成文本里找第一个出现的 A/B/C/D 字母。最直观，但模型可能不吐字母（返回 `None` 判错），或吐出被噪声字符包裹的字母；
- **方法二（logprob 评分）**：不生成，只做**一次前向**，取最后一个位置的 log_softmax 分布，比较「A / B / C / D 四个字母各自作为下一个 token」的 logprob，取最大者为预测。**永远能给出一个字母**，且一次前向即可，是实践中最常用的方法（GPT-4 技术报告等均采用此路线）。

为什么两种方法对同一个 base 模型会给出 21.48% 与 34.44% 的天壤之别？关键在于：**贪心解码的下一个 token 本来就是 logprob 最大的 token**——若模型乖乖在 `Answer: ` 后先吐字母，两法数学等价；若模型先吐了别的词（比如 "The"），方法一就只能寄希望于后 7 个 token 里冒出字母，而方法二依然冷静地比较四个字母的分数。README 实测中 reasoning 模型两法得分完全相同（都是 57/270 = 21.11%），base 模型两法差异巨大（58 vs 93），正说明 base 模型经常「答非所问」。

#### 4.2.2 核心流程

```
方法一 predict_choice（appendix_f.py / 1_letter_matching.py）
  1. generate_text_basic_stream_cache(model, token_ids, max_new_tokens=8)
  2. 对每个新生成 token：decode 成文本 → 逐字符扫，upper 后命中 "ABCD" 即返回
  3. 8 个 token 都没有字母 → 返回 None（判错）

方法二 predict_choice_logprobs（2_logprob.py）
  1. logits = model(prompt_fmt)          # 一次前向，[1, T, V]
  2. next_logp = log_softmax(logits[0, -1])   # 只看最后位置的下一 token 分布
  3. 对 A/B/C/D 各自算 first_new_token_id（token 对齐！）
  4. scores[letter] = next_logp[tok_id]
  5. pred = argmax(scores)
```

方法二的核心难点是第 3 步：**不能想当然地用 `tokenizer.encode("A")` 的 id**。提示以 `"Answer: "` 结尾，BPE 分词器很可能把「尾随空格 + 字母」合并成一个 token（如 `" A"`）。所以要先 `encode(prompt + letter)`，再找它与 `encode(prompt)` 的**公共前缀**之后的第一枚 token——那才是「模型在当前位置会实际输出的字母 token」。

#### 4.2.3 源码精读

**提示模板**在三个脚本中逐字相同（[1_letter_matching.py:L11-L19](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chF/02_mmlu/1_letter_matching.py#L11-L19)）：题干、四个选项各占一行，最后 `Answer: ` 收尾。

**库版 `predict_choice`**（[appendix_f.py:L9-L27](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/appendix_f.py#L9-L27)）复用 u3-l2 讲过的流式缓存生成器 [generate_text_basic_stream_cache](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch02.py#L117-L141)：贪心 argmax、KV 缓存、EOS 提前停止。每收到一枚 token 就 decode 并扫描：

```python
answer = tokenizer.decode(t.squeeze(0).tolist())
for letter in answer:
    letter = letter.upper()
    if letter in "ABCD":
        pred = letter
        break
```

注意一个**真实存在的陷阱**：这个扫描不检查单词边界。如果模型先输出 `" answer"` 这样的 token，`" answer".upper()` 的第二个字符就是 `A`——预测被错误地定为 A。脚本版 `1_letter_matching.py` 内嵌了同一份实现（[L23-L41](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chF/02_mmlu/1_letter_matching.py#L23-L41)），与库版逻辑一致；附录脚本保持自包含、可直接 `python` 运行，这是 `chXX` 目录的一贯风格。

**评测主循环**（[1_letter_matching.py:L44-L86](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chF/02_mmlu/1_letter_matching.py#L44-L86)）：`subsets` 参数支持 `"all"`（拉全部配置名）、逗号分隔串或列表（[L53-L58](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chF/02_mmlu/1_letter_matching.py#L53-L58)）；逐题渲染、编码、预测，`gold = "ABCD"[ans]`（[L71-L76](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chF/02_mmlu/1_letter_matching.py#L71-L76)），`pred == gold` 计数。**注意 `pred` 为 `None` 时 `int(None == gold)` 就是 0**——「不吐字母」直接计为答错，这是方法一分数偏低的重要原因之一。

**方法二的对齐工具**（[2_logprob.py:L26-L39](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chF/02_mmlu/2_logprob.py#L26-L39)）：

```python
def first_new_token_id(tokenizer, prompt, prompt_ids, continuation):
    ids_full = tokenizer.encode(prompt + continuation)
    j = common_prefix_len(ids_full, prompt_ids)
    if j >= len(ids_full):
        raise ValueError("Continuation produced no new tokens.")
    return ids_full[j]
```

`common_prefix_len` 逐位比较两个 id 序列的公共前缀长度；拼接字母后多出来的第一枚 token 即目标。评分本体（[2_logprob.py:L42-L53](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chF/02_mmlu/2_logprob.py#L42-L53)）：`torch.no_grad()` 下一次前向，`log_softmax(logits[0, -1])` 取末位分布，四个字母各查 `next_logp[tok_id]`，`max(scores, key=scores.get)` 择优。

作者在 MPS 上的实测记录（README）：方法一 base 21.48% / reasoning 21.11%；方法二 base 34.44% / reasoning 21.11%。**reasoning 模型两法同分绝非巧合**——它被 `load_model_and_tokenizer`（[ch03.py:L52-L90](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L52-L90)）套上聊天模板后，行为规整地先吐字母，两法因此一致；而它整体只有 21%，是因为推理模型的「本能」是先长篇思考再作答，被逼着立即吐字母时表现反而差——这正是方法三要解决的问题。

#### 4.2.4 代码实践

1. **实践目标**：亲眼验证「为什么要做 token 对齐」——看清 `encode(prompt)` 与 `encode(prompt + "A")` 在 BPE 边界上的差异。**只需分词器，不需跑模型，CPU 秒级完成**。
2. **操作步骤**（示例代码）：

```python
from pathlib import Path
from reasoning_from_scratch.qwen3 import download_qwen3_small, Qwen3Tokenizer

download_qwen3_small(kind="base", tokenizer_only=True, out_dir="qwen3")  # 仅约 10 MB
tok = Qwen3Tokenizer(tokenizer_file_path=Path("qwen3") / "tokenizer-base.json")

prompt = ("How many ways are there to put 4 distinguishable balls "
          "into 2 indistinguishable boxes?\nA. 7\nB. 11\nC. 16\nD. 8\nAnswer: ")
pid = tok.encode(prompt)

for letter in "ABCD":
    fid = tok.encode(prompt + letter)
    j = 0
    while j < min(len(pid), len(fid)) and pid[j] == fid[j]:
        j += 1
    print(letter, "-> 首新 token:", repr(tok.decode([fid[j]])), "| id:", fid[j],
          "| 公共前缀:", j, "/", len(pid))
```

3. **需要观察的现象**：公共前缀长度是否等于 `len(pid) - 1`；首新 token 的文本形态（是裸 `"A"` 还是带前导空格的 `" A"`）；四个字母的 token id 是否互不相同。
4. **预期结果**：提示结尾的空格与后续字母被 BPE 合并成单独一枚 token，因此公共前缀比 `len(pid)` 少 1——这正是 `first_new_token_id` 存在的意义。**待本地验证**（具体 token 文本以运行结果为准）。
5. 思考题（不用跑）：如果直接用 `tok.encode("A")` 的 id 去查 `next_logp`，会发生什么？

#### 4.2.5 小练习与答案

**练习 1**：预测库版 `predict_choice` 在某步 decode 出 `" answer"` 时的返回值，并解释。

**答案**：返回 `"A"`。逐字符扫描先把 `a` 大写成 `A` 并命中 `"ABCD"`——扫描不看单词边界，任何含 a/b/c/d 的词都会被截获。这说明方法一不仅怕「不吐字母」，也怕「多吐闲话」。

**练习 2**：为什么 README 里 reasoning 模型在方法一和方法二下得分完全相同（57/270），base 模型却差很多（58 vs 93）？

**答案**：贪心解码选的 token 正是 logprob 最大的 token。当模型下一个 token 就吐字母时，两法等价。reasoning 变体经聊天模板规训后总是立即吐字母，所以一致；base 模型经常先吐别的词（甚至 8 个 token 内不出字母），方法一丢失这些样本，方法二不受影响。

**练习 3**：README（[02_mmlu/README.md](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chF/02_mmlu/README.md) L25 附近）说可以用 `--subsets list` 列出可用子集。对照 [1_letter_matching.py:L53-L58](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chF/02_mmlu/1_letter_matching.py#L53-L58) 的参数处理，这个说法成立吗？

**答案**：代码只特判了 `"all"`；`"list"` 会落进逗号分支被当成子集名 `["list"]` 传给 `load_dataset("cais/mmlu", "list")`，大概率因配置不存在而报错。这是文档与代码的一处不一致——列出子集应使用 `get_dataset_config_names("cais/mmlu")`（notebook 中有演示）。**待本地验证**。

### 4.3 方法三：教师强制完整答案评分

#### 4.3.1 概念说明

方法二只比较「单个字母 token」的分数，对 base 模型有效，但对推理模型有失公允：推理模型的本能是先思考再作答，在 `Answer: ` 处被掐断思考、强选字母，测不出真实水平。方法三换个问法：**如果把「字母 + 完整选项文本」（如 `A. 7`）整段塞给模型，模型认为这段续写有多自然？** 对四个选项各算一次「续写的平均 logprob」，取最高者为预测。

这就是 **teacher forcing**：不劳模型自回归生成，而是把候选答案直接喂进去，在因果掩码保护下每个位置都给出「预测下一个 token」的分布，再核对这些位置的预测与真实答案 token 的吻合程度。它与 u6-l1 的 `avg_logprob_answer`、u9-l3 的蒸馏损失完全同构——同一条「一次前向 + 切片取分」技术，在评测、打分、训练三处复用。

代价：每题要跑 **4 次前向**（四个选项各一次），比方法二慢约 4 倍；收益：对推理模型更稳健（README：reasoning 从 21.11% 升到 28.89%）。

#### 4.3.2 核心流程

```
avg_logprob_teacher_forced(prompt, "A. 7")
  1. answer_text = f"{letter}. {choice_text}"          # 如 "A. 7"
  2. ids_full = encode(prompt + answer_text)
     answer_ids = ids_full[公共前缀之后:]               # token 对齐同方法二
  3. combined = cat([prompt_ids, answer_ids[:-1]])      # 去掉最后一个答案 token
  4. logp = log_softmax(model(combined))                # 一次前向，no_grad
  5. steps = logp[prompt_len-1 : prompt_len-1+answer_len]   # 恰好 answer_len 行
  6. avg_logp = steps.gather(答案 token id).mean()      # 答案区平均 logprob
四个选项各算一次 → pred = argmax
```

#### 4.3.3 源码精读

续写 token 的抽取与方法二同源：`encode(prompt + answer_text)` 后用公共前缀截断（[3_teacher_forcing.py:L36-L41](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chF/02_mmlu/3_teacher_forcing.py#L36-L41)）。

构造前向输入（[L43-L49](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chF/02_mmlu/3_teacher_forcing.py#L43-L49)）：喂入 `prompt + 答案区除最后一枚 token`。因为模型输出的是「每个位置对下一 token 的预测」，要给 `answer_len` 枚答案 token 各配一个预测位置，输入只需包含到倒数第二枚答案 token——这就是 u3-l1 贪心循环里「整段前向、取末位 logits」的多位置版本。

切片与取分（[L51-L65](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chF/02_mmlu/3_teacher_forcing.py#L51-L65)）：

```python
prompt_len = prompt_fmt.shape[1]
answer_len = len(answer_ids)
steps = logp[prompt_len-1 : prompt_len-1+answer_len, :]   # [answer_len, V]
targets = torch.tensor(answer_ids, ...).unsqueeze(1)
avg_logp = steps.gather(dim=1, index=targets).mean().item()
```

起点为什么是 `prompt_len - 1`？`combined` 的第 `prompt_len - 1` 个位置是**最后一枚 prompt token**（`Answer: ` 的空格），它负责预测第一枚答案 token——这正是 u6-l1「桥接位置」与 u9-l3 `answer_start = max(prompt_len - 1, 0)` 的同一下标约定。`gather` 后取 `.mean()`：用平均而非求和，避免长选项文本天然吃亏（长度偏差，u6-l1 已讨论）。

预测器与主循环（[L68-L77](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chF/02_mmlu/3_teacher_forcing.py#L68-L77)、[L80-L124](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chF/02_mmlu/3_teacher_forcing.py#L80-L124)）与前面脚本同构。作者实测：base 31.85%（略低于方法二的 34.44%），reasoning 28.89%（显著高于方法二的 21.11%）——「方法三更适合推理模型」的论断正来自这组对照。

#### 4.3.4 代码实践

1. **实践目标**：纸面推演切片下标，把方法三的切片约定与 u6-l1 / u9-l3 打通。
2. **操作步骤**：设 `prompt_len = 10`（即 `prompt_fmt.shape[1]`）、某选项 `answer_ids` 长度 `L = 4`，回答三问：
   - `combined` 的长度是多少？
   - `steps` 取 `logp` 的哪些行？
   - 为什么起点是 `prompt_len - 1` 而不是 `prompt_len`？
3. **需要观察的现象**：自己画一条 14 格的序列，标出每格「预测谁」。
4. **预期结果**：`combined` 长度 = 10 + 4 - 1 = 13（`answer_ids[:-1]` 只有 3 枚）；`steps` 取行 9~12 共 4 行；第 9 行（最后一枚 prompt token，即 `Answer: ` 的空格）预测第一枚答案 token，起点 `prompt_len - 1` 保证第一枚答案 token 也被监督到。
5. 无需运行代码即可完成；如想验证，可在 [3_teacher_forcing.py:L56-L60](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chF/02_mmlu/3_teacher_forcing.py#L56-L60) 处临时加 `print` 观察 `steps.shape`（**待本地验证**）。

#### 4.3.5 小练习与答案

**练习 1**：方法三为什么要用 `.mean()` 而不是 `.sum()`？

**答案**：四个选项文本长度 often 不同（如 "7" 与 "the set of all prime numbers"）。求和版本天然惩罚长选项——长度每多一枚 token 就多扣一份负 logprob。平均消除长度偏差，让四个选项在同一尺度上比「每 token 流畅度」。

**练习 2**：方法三每题的前向次数是方法二的几倍？这与 README 记录的耗时（67.9s vs 22.5s）吻合吗？

**答案**：4 倍——四个选项各一次前向，而方法二全程只做一次前向。67.9 / 22.5 ≈ 3.0，略低于 4，因为方法三的 KV 缓存路径与固定开销摊薄了部分成本，量级上完全吻合。

**练习 3**：方法三与方法二对 base 模型谁更高（34.44% vs 31.85%）？为什么方法三未必全面占优？

**答案**：方法二更高。方法三把「选项文本本身的先验流畅度」混入了评分——常见词组组成的错误选项可能比生僻但正确的选项得分更高；方法二只比字母 token，更纯粹地反映模型对四个选项的即时偏好。两者各有偏差，所以附录同时给出三种方法供对照。

### 4.4 偏好排行榜：Elo 与 Bradley-Terry

#### 4.4.1 模块概念

MMLU 评测给模型打「绝对分」，排行榜回答另一种问题：**用户在两个模型的回答之间二选一，攒下成千上万条偏好投票后，如何排出全局名次？** 输入数据形态见 [votes.json](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chF/03_leaderboards/votes.json)（L1-L62）：60 个二元组，每个 `[赢家, 输家]`，涉及 GPT-5、Claude-3、Llama-4、Llama-3 四个模型。

两条技术路线：

- **Elo（在线更新）**：按比赛时间顺序逐场更新分数。期望胜率由分差决定：

\[
E_{\text{winner}} = \frac{1}{1 + 10^{(R_{\text{loser}} - R_{\text{winner}})/400}}
\]

赛后按实际胜负与期望的差更新：\( R' = R + K \cdot (S - E) \)，其中 \( S \in \{0, 1\}\) 是实际得分、K（默认 32）控制敏感度。分差 400 对应约 91% 期望胜率，分差 0 则各半。

- **Bradley-Terry（全局拟合）**：假设模型 \( i \) 有潜在实力分 \( s_i \)，胜负概率：

\[
P(i \text{ 胜 } j) = \sigma(s_i - s_j) = \frac{1}{1 + e^{-(s_i - s_j)}}
\]

对全部比赛最小化负对数似然 \( \mathcal{L} = -\frac{1}{N}\sum_k \log \sigma(s_{w_k} - s_{l_k}) \)。**损失只依赖胜负对的集合，与顺序无关**——这是与 Elo 的本质区别。

两者的数学联系：令 \( R = s \cdot 400 / \ln 10 \)，则 \( 10^{\Delta R / 400} = e^{\Delta s} \)，Elo 的期望胜率公式正是 BT 概率的「Elo 尺度」改写。脚本最后也正是用 `scale = 400.0 / math.log(10.0)` 把 BT 实力分换算到 Elo 尺度（[2_bradley_terry_leaderboard.py:L41](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chF/03_leaderboards/2_bradley_terry_leaderboard.py#L41)）。

#### 4.4.2 核心流程

```
elo_ratings(vote_pairs, k=32, init=1000)
  1. 所有出现过的模型初始化为 1000 分
  2. 按列表顺序逐场：
       E = 1 / (1 + 10^((R_loser - R_winner)/400))
       R_winner += K * (1 - E)
       R_loser  += K * (0 - (1 - E))      # 一升一降，总和不变
  3. 返回 {model: rating}

bradley_terry_torch(vote_pairs)
  1. 模型名排序编号；theta 是 n-1 维可学习参数（第 n 个固定 0，锚点解决平移不可辨识）
  2. Adam(lr=0.01, weight_decay=1e-4) 迭代 500 轮：
       delta = s[winners] - s[losers]
       loss  = -logsigmoid(delta).mean()  ; 反传；步进
  3. 缩放：R = s * 400/ln10，中心化后 +1000
```

#### 4.4.3 源码精读

**库版 `elo_ratings`**（[appendix_f.py:L30-L55](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/appendix_f.py#L30-L55)）：字典推导式从胜负对里收集全部模型名并初始化（[L32-L36](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/appendix_f.py#L32-L36)）；主循环计算期望并一升一降地更新（[L39-L53](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/appendix_f.py#L39-L53)）。赢家的增量与输家的减量严格相等（都是 `K·(1−E)`），因此**总分守恒**——回归测试 [test_appendix_f.py:L15-L18](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_appendix_f.py#L15-L18) 断言三模型总分恒为 3000；单场测试（[L9-L12](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_appendix_f.py#L9-L12)）断言势均力敌时赢家 +16 / 输家 −16（E = 0.5，K = 32）。

命令行版 [1_elo_leaderboard.py:L8-L22](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chF/03_leaderboards/1_elo_leaderboard.py#L8-L22) 内嵌了同一算法（用 `+=` 原地更新，数学等价），`--path` 读入 votes.json，按分数降序打印榜单（[L37-L47](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chF/03_leaderboards/1_elo_leaderboard.py#L37-L47)）。

**Bradley-Terry**（[2_bradley_terry_leaderboard.py:L12-L46](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chF/03_leaderboards/2_bradley_terry_leaderboard.py#L12-L46)）：收集模型名、把胜负对转成索引张量（[L15-L21](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chF/03_leaderboards/2_bradley_terry_leaderboard.py#L15-L21)）；关键设计是 **n−1 维参数 + 固定零锚点**（[L24-L28](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chF/03_leaderboards/2_bradley_terry_leaderboard.py#L24-L28)）——损失只看分数**差**，全体平移不改变损失（不可辨识），固定一个模型为 0 分即可约束解唯一。训练循环（[L30-L36](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chF/03_leaderboards/2_bradley_terry_leaderboard.py#L30-L36)）就是最小化负对数似然 `logsigmoid`——这里用 PyTorch 的 Adam 而非 scipy 拟合，纯粹为了与全书技术栈统一（README 明言「for better code familiarity」）。最后缩放（[L38-L45](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chF/03_leaderboards/2_bradley_terry_leaderboard.py#L38-L45)）：乘 `400/ln10`、减均值、加 1000，得到与 Elo 可直接对照的分数。

作者实测（README，votes.json 全量 60 场）：

| 排名 | Elo | Bradley-Terry |
|---|---|---|
| 1 | GPT-5　1095.9 | GPT-5　1140.6 |
| 2 | Claude-3　1058.7 | Claude-3　1058.7 |
| 3 | Llama-4　958.2 | Llama-4　950.3 |
| 4 | Llama-3　887.2 | Llama-3　850.4 |

两法排名一致、分数幅度不同（BT 拉得更开）。注意 votes.json 只有 4 个模型，所以榜单最多 4 行——没有「前 5 名」可言。

#### 4.4.4 代码实践

1. **实践目标**：手推一场 Elo 更新；用 `appendix_f.elo_ratings` 观察「部分数据 vs 全量」「原始顺序 vs 打乱顺序」下的榜单变化，并与 BT 的顺序无关性对照。**纯 CPU、秒级，无需模型与数据集下载**。
2. **操作步骤**：
   - 手算：A、B 各 1000 分，A 胜 B，K=32。先算 E，再算更新后双方分数，与 [test_appendix_f.py:L9-L12](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_appendix_f.py#L9-L12) 对照；
   - 运行两个脚本（示例命令）：
     ```bash
     cd chF/03_leaderboards
     python 1_elo_leaderboard.py --path votes.json
     python 2_bradley_terry_leaderboard.py --path votes.json
     ```
   - 再跑一个对比实验（示例代码，仓库根目录下执行）：
     ```python
     import json, random
     from reasoning_from_scratch.appendix_f import elo_ratings

     votes = json.load(open("chF/03_leaderboards/votes.json"))
     rank = lambda r: sorted(r.items(), key=lambda kv: -kv[1])

     print("前 20 场:", rank(elo_ratings(votes[:20])))
     print("全 60 场:", rank(elo_ratings(votes)))

     shuffled = votes[:]
     random.Random(0).shuffle(shuffled)
     print("打乱后 :", rank(elo_ratings(shuffled)))
     ```
3. **需要观察的现象**：三份 Elo 榜单的分数是否互不相同；总分是否始终守恒（4 模型 × 1000 = 4000）；对同一份打乱后的 votes.json 重跑 BT 脚本，结果是否纹丝不动。
4. **预期结果**：Elo 分数随数据量与顺序变化（在线算法），但排名大概率稳定为 GPT-5 > Claude-3 > Llama-4 > Llama-3；全量 Elo 应复现 README 记录（1095.9 / 1058.7 / 958.2 / 887.2）；BT 对顺序完全不敏感（损失只依赖胜负对集合），打乱前后输出逐位一致。**待本地验证**。
5. 若打乱后 Elo 排名发生了翻转，思考：哪种数据分布下在线更新会「记仇」（早期比赛影响被后期稀释/放大）？

#### 4.4.5 小练习与答案

**练习 1**：手算 A(1000) 胜 B(1000)、K=32 的更新结果。

**答案**：\( E = 1/(1+10^0) = 0.5 \)；A：1000 + 32×(1−0.5) = **1016**；B：1000 + 32×(0−0.5) = **984**。与回归测试断言一致；总分 2000 守恒。

**练习 2**：Elo 与 Bradley-Terry 至少有两点本质区别，请说出。

**答案**：① 更新方式：Elo 是在线顺序更新（结果依赖比赛顺序），BT 是对全部数据一次性全局拟合（顺序无关）；② 不确定性处理：Elo 每场等权更新、对「谁先比」敏感且没有概率语义，BT 显式建模胜负概率并给出最大似然估计，天然支持置信区间（LM Arena 论文正是据此换用 BT）。共同点：两者都以分差决定胜负概率，且可经 \( R = s\cdot 400/\ln 10 \) 互相换算尺度。

**练习 3**：`bradley_terry_torch` 为什么把参数定为 n−1 维、最后一维固定 0，而不是 n 维？

**答案**：损失只包含分数差 \( s_i - s_j \)，对整体平移不变——n 维参数有无穷多组等价解（不可辨识）。固定一个锚点（最后一模型恒 0）后解唯一，优化器不会在平移方向上漂移。Elo 无此问题，因为它以 1000 为初始绝对分数且增减守恒。

### 4.5 LLM-as-judge：Ollama 裁判流水线

#### 4.5.1 概念说明

MMLU 有标准答案、排行榜要人工投票，LLM 裁判则是第三条路：**让一个更强的模型按评分量表（rubric）给候选回答打分**。它适用于没有客观对错的开放式任务（写作、摘要、对话质量），代价是引入了裁判自身的偏差（偏爱长回答、偏爱自己风格的「同源偏置」等——附录 F 正文有专门讨论）。

`ollama-judge.py` 组装了一条双模型流水线：**候选模型**是本书的 Qwen3 0.6B（base 或 reasoning），在 MATH-500 上生成回答；**裁判模型**是通过 Ollama 本地运行的 gpt-oss:20b（约 13 GB 内存；可换 `--judge_model qwen3:4b` 等小模型），按 1~5 分量表评分。

#### 4.5.2 核心流程

```
ollama-judge.py 主流程
  1. 加载 MATH-500（本地优先，GitHub 回退——与 ch03 同款逻辑）
  2. prepare_math500_pairs: problem → instruction, answer/solution → reference_answer
  3. 加载候选模型（load_model_and_tokenizer, use_compile=False）
  4. check_if_running("ollama"): psutil 扫描进程名，未运行直接 RuntimeError
  5. 逐题：
       a. generate_with_qwen3: 流式贪心生成候选回答（只解码生成部分）
       b. rubric_prompt: 拼接量表 + instruction + reference + candidate
       c. query_model: POST 到 Ollama /api/chat，流式逐行拼接回复
       d. parse_score: 正则抓第一个 1~5 数字，失败默认 3
       e. 记录 score_counts 与逐题明细
  6. 汇总：平均分 + 各分档计数
```

#### 4.5.3 源码精读

**裁判输入准备**：`get_data` 与 ch03 相同的本地缓存优先策略（[ollama-judge.py:L27-L42](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chF/04_llm-judge/ollama-judge.py#L27-L42)）；`prepare_math500_pairs` 把每题整理成 `instruction`（题干）与 `reference_answer`（优先 `answer` 字段、缺失时回退 `solution`，[L45-L62](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chF/04_llm-judge/ollama-judge.py#L45-L62)）。

**Ollama 健康检查**：psutil 遍历进程名找 "ollama"（[L17-L23](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chF/04_llm-judge/ollama-judge.py#L17-L23)），未运行立即报错（[L227-L234](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chF/04_llm-judge/ollama-judge.py#L227-L234)）——先查环境再干活，避免跑到一半才失败。

**REST 调用**（[L65-L95](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chF/04_llm-judge/ollama-judge.py#L65-L95)）：payload 指定模型、单条 user 消息与采样选项 `seed=123, temperature=0, num_ctx=2048`（贪心、确定性，评测可复现）；`stream=True` 下 Ollama 返回**逐行 JSON（NDJSON）**，代码按行 `json.loads` 并拼接 `message.content`（[L85-L94](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chF/04_llm-judge/ollama-judge.py#L85-L94)）——与 u9-l1 Ollama 教师脚本同款模式。

**评分量表提示**（[L98-L125](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chF/04_llm-judge/ollama-judge.py#L98-L125)）：固定 1~5 分的文字量表（1 = 完全跑题，5 = 完全符合且简洁），随后依次填入 Instruction / Reference Answer / Answer，以 `Evaluation: ` 收尾引导裁判直接输出分数与理由。notebook 中手工例子（企鹅推理题）拿回 `**Score: 5**` 的完整评语。

**分数解析**（[L128-L130](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chF/04_llm-judge/ollama-judge.py#L128-L130)）：

```python
def parse_score(judge_text, default=3):
    m = re.search(r"([1-5])(?:\D|$)", judge_text)
    return int(m.group(1)) if m else int(default)
```

正则抓取**第一个**后面跟非数字或行尾的 1~5 数字；解析失败默认给中间分 3。注意它的脆弱点：裁判若先说「The model solved 12 of 12 parts」，`12` 中的 `1` 因后面跟数字被跳过，但 `2` 会被抓走——裁判文本里任何游离的 1~5 数字都可能污染分数。

**主循环与汇总**（[L236-L268](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chF/04_llm-judge/ollama-judge.py#L236-L268)、[L271-L277](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chF/04_llm-judge/ollama-judge.py#L271-L277)）：候选生成限 `max_new_tokens`（默认 2048），裁判调用限 256 token；每题记录 8 个字段（含 `judge_text` 全文与双模型名），最终打印平均分与 `1:.. 2:.. ... 5:..` 分档计数。作者实测（README）：base 平均 3.800（10 题），reasoning 平均 4.300——裁判视角与 MATH-500 客观准确率方向一致。

#### 4.5.4 代码实践

1. **实践目标**：不启动 Ollama 也先验证裁判流水线的两个纯函数环节——量表提示的组装与分数解析的边界行为。
2. **操作步骤**（示例代码，可直接在 Python 里跑）：

```python
import re

def parse_score(judge_text, default=3):        # 从 ollama-judge.py L128-L130 原样复制
    m = re.search(r"([1-5])(?:\D|$)", judge_text)
    return int(m.group(1)) if m else int(default)

cases = [
    "**Score: 5**\nThe candidate answer directly addresses the question.",  # 期望 5
    "I cannot evaluate this response.",                                     # 期望 3（默认）
    "The model solved 12 of 12 parts correctly.",                           # 陷阱用例
]
for c in cases:
    print(repr(c[:40]), "->", parse_score(c))
```

3. **需要观察的现象**：前两例是否给出 5 与 3；第三例抓到的是哪个数字。
4. **预期结果**：例 1 返回 5（第一个 1~5 数字且后随非数字）；例 2 无匹配返回默认 3；例 3 中 `1` 后面跟着 `2`（数字）不满足 `(?:\D|$)`，正则继续扫描到 `2`（后跟空格）——返回 2 而不是任何合理的评分。**待本地验证**（纯字符串运算，无需任何模型）。
5. 进阶（需本地安装 Ollama，可选）：`python ollama-judge.py --dataset_size 5 --max_new_tokens 512 --judge_model qwen3:4b`，用小裁判模型跑 5 题观察输出格式。**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：`query_model` 的 options 里 `temperature=0, seed=123` 起什么作用？为什么评测场景要这么设？

**答案**：贪心解码 + 固定种子让裁判输出尽量确定可复现——同样的候选回答重复评审应得到同样分数，否则分数抖动会淹没模型间的真实差异。这与本书评测脚本一贯的贪心设置（u3-l1）一致。

**练习 2**：裁判默认给 `default=3` 而不是 0 或 1，有什么权衡？

**答案**：3 是量表中点，语义是「不确定」。解析失败可能是裁判格式跑偏而非候选差，给 0/1 会系统性拉低均分（把裁判的失误算到候选头上）；给 3 则失败样本对均分近似中性。代价是频繁失败时会掩盖信息——所以逐题明细里保留 `judge_text` 供人工复查。

**练习 3**：这条流水线里，`reference_answer` 用的是 MATH-500 的 `answer`（最终答案）而非完整 `solution`。这会让裁判在什么情况下误判？

**答案**：裁判只对照最终答案文本评「候选回答是否包含等价结论」，不逐步验算过程。候选可能推理全错但碰巧抄对结论（被高估），也可能方法正确、中间步骤与官方解法不同而结论对（没问题），或结论对但表述与参考差异过大（可能被低估）。这与第 3 章 SymPy 验证器「只看 boxed 答案」是同一取舍——附录把它交给了更强的裁判模型去理解文本等价性。

## 5. 综合实践

设计一个「**迷你评测工作台**」任务，把本讲三类范式串成一次完整操练：

**任务 A：MMLU 方法对比报告**

1. 安装 `datasets`，跑通随机基线（4.1.4 实践）；
2. 跑方法一：`cd chF/02_mmlu && python 1_letter_matching.py --which_model base --device cpu`（该脚本没有限制题数的参数，`high_school_mathematics` 测试集共 270 题、每题至多生成 8 个 token，CPU 可承受；若想先小规模试水，可仿照 `appendix_f.predict_choice` 自己写循环、用 `ds.select(range(20))` 切前 20 题——**不要改动仓库脚本**）；
3. 把四个数字填进对照表：随机基线均值 ≈ 25%（±2.65%）、方法一（本地实测）、方法二 34.44%、方法三 31.85%（后两者引自 README 的 MPS 实测记录，也可本地复跑 `2_logprob.py` / `3_teacher_forcing.py`）；
4. 用三句话回答：方法一为什么可能低于随机基线？（提示：`pred=None` 判错 + 4.2.5 练习 1 的字母截获陷阱 + gold 分布不均）；方法二对 base 为什么跃升？方法三对 reasoning 为什么重要？

**任务 B：双榜对照与顺序扰动**

1. 手算单场 Elo（4.4.4 步骤 1）；
2. 跑 Elo 与 BT 两个脚本，用 4.4.4 的示例代码做「前 20 场 vs 全 60 场 vs 打乱」三组对比；
3. 产出一张表：每组的四模型分数与排名，标注总分是否守恒、BT 是否完全一致；
4. 用一句话总结：什么时候必须用 BT 而不能信 Elo？（提示：比赛顺序不随机、新模型晚加入持续「刷分」的场景。）

**任务 C（可选）：裁判离线体检**

完成 4.5.4 的 `parse_score` 边界实验，再为它补三个自选用例（例如裁判先输出「**Rating: 4/5**」、纯数字回答、空字符串），记录每个用例的返回值并判断是否符合直觉。

**最终产出**：一张「方法 × 准确率/分数 × 耗时 × 适用场景 × 主要偏差」的决策表，例如：

| 方法 | base 得分 | reasoning 得分 | 相对耗时 | 适用场景 | 主要偏差源 |
|---|---|---|---|---|---|
| 随机基线 | ≈25%（±2.65%） | 同左 | 可忽略 | 一切选择题的底线 | gold 分布不均 |
| 字母匹配 | 21.48%* | 21.11%* | 中 | 直觉演示 | 不吐字母 / 字母截获 |
| logprob | 34.44%* | 21.11%* | 低 | 实践首选 | 逼推理模型立即作答 |
| 教师强制 | 31.85%* | 28.89%* | 高 | 推理模型 | 选项文本流畅度先验 |
| LLM 裁判 | 3.800/5* | 4.300/5* | 高 | 开放式任务 | 裁判偏好与解析正则 |

\* 带 `*` 的数字引自作者在 MPS 上的实测记录（各 README），本地复跑数值会有微小浮动；你自己实测的数字以本地输出为准（**待本地验证**）。

## 6. 本讲小结

- **选择题评测有三层实现**：字母匹配（生成后提取，直观但脆弱——`None` 判错与「任意词中的 a/b/c/d 被截获」两个陷阱）、logprob 评分（一次前向比较四个字母 token，永有输出、实践首选）、教师强制（整段候选答案取平均 logprob，慢 4 倍但对推理模型公允）。
- **token 对齐是 logprob 方法的技术核心**：`encode(prompt)` 与 `encode(prompt + "A")` 的公共前缀之后那枚 token 才是模型实际会输出的字母 token——`first_new_token_id` / `common_prefix_len` 解决的正是 BPE 边界合并问题。
- **随机基线先于模型评测**：25% 只是期望，\( \sigma_A = \sqrt{p(1-p)/n} \approx 2.64\% \) 的噪声带与不均匀的 gold 分布（A 仅 21.11%）共同构成一切分数的解释背景。
- **Elo 与 BT 同源不同法**：都以分差决定胜负概率（\( R = s\cdot400/\ln 10 \) 可互换尺度）；Elo 在线顺序更新（总分守恒、结果依赖赛序），BT 全局最大似然（顺序无关、需固定零锚点解决平移不可辨识）——LM Arena 从前者迁移到后者。
- **LLM 裁判 = rubric 提示 + 流式 REST + 正则解析**：Ollama 的 NDJSON 流式接口、`temperature=0/seed=123` 的可复现设置、以及 `parse_score` 抓「第一个 1~5 数字」的解析陷阱，三者构成可复用的裁判流水线骨架。

## 7. 下一步学习建议

- **u10-l6（构建聊天界面）**：把评测中反复出现的 `generate_text_basic_stream_cache` 换成交互式 Chainlit 界面，亲手「人肉评测」base / GRPO / 蒸馏三个模型的对话质量；
- **u10-l7（测试体系与 CI）**：本讲引用的 [tests/test_appendix_f.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_appendix_f.py) 只是冰山一角，下一讲系统介绍 `tests/` 的组织方式与多平台 CI 矩阵；
- **延伸阅读**：MMLU 原论文（Hendrycks et al., 2021）看 5-shot 协议与学科覆盖；Chatbot Arena 论文（[arXiv:2403.04132](https://arxiv.org/abs/2403.04132)）看 BT 拟合的置信区间与风格控制；
- **动手方向**：给 4.2 的 token 对齐实验换成分词器 reasoning 变体，看 `Answer: ` 的 BPE 边界是否相同；或把 `elo_ratings` 的 K 因子调到 8 / 64 重跑任务 B，观察榜单对 K 的敏感度。
