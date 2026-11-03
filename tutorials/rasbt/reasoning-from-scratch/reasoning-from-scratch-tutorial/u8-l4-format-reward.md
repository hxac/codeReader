# GRPO 改进三：格式奖励与 think 标签

## 1. 本讲目标

本讲是「GRPO 改进」系列的第三讲。前两讲我们给 GRPO 加上了 clip ratio 裁剪（u8-l2）和 KL 散度正则（u8-l3），它们改造的都是**损失端**；本讲回到**奖励端**，把单一的正确性奖励扩展为「正确性 + 格式」的复合奖励。学完本讲，你应该能够：

1. 解释**多目标奖励线性组合**的加权方式：`reward = rlvr_reward + format_reward_weight × format_reward`，以及权重如何改变组内优势（advantage）的符号与排序。
2. 读懂 `reward_format` 的实现：它不碰文本字符串，而是直接在 **token id 层面**检测生成部分中 `<think>`（151667）是否先于 `</think>`（151668）出现。
3. 理解 `render_prompt_with_think_tokens` 这个"提示工程版"方案为什么被作者注释掉，以及最终脚本为什么改训 **reasoning 变体**模型。
4. 用真实训练日志和手推计算，解释**格式奖励如何引导模型学会结构化思考**，以及权重过大时「奖励黑客」（reward hacking）的具体发生机制。

## 2. 前置知识

本讲假设你已读完 u7-l1（RLVR 奖励）、u7-l3（GRPO 损失）与 u8-l2（clip ratio）。用到的前置概念如下：

- **策略与 rollout**：被训练的 LLM 是策略（policy）；策略对一条 prompt 生成的一条完整回答是一个 rollout。
- **RLVR 奖励**（`reward_rlvr`）：先强制提取 `\boxed{}` 里的答案（无格式直接 0 分），再用 SymPy 数学等价判分，返回 1.0 或 0.0。
- **组内标准化优势**（GRPO 核心）：

  \[ A_i = \frac{r_i - \mu}{\sigma + 10^{-4}} \]

  其中 \(\mu\)、\(\sigma\) 是同一 prompt 的 num_rollouts 个奖励的均值与样本标准差。**优势的符号完全由「该 rollout 的奖励是否高于组均值」决定**——这个性质是本讲分析奖励黑客的钥匙。
- **奖励塑形与奖励黑客**：在真实目标（答对数学题）之外追加易于计算的辅助奖励（格式合规）称为奖励塑形（reward shaping）；如果辅助奖励权重过大，策略会发现「只满足格式、不管对错」就能拿到高于组均值的奖励，于是把概率质量投向格式漂亮但答案错误的输出——这就是奖励黑客（reward hacking）。
- **`<think>` 标签**：DeepSeek-R1、Qwen3 等推理模型用 `<think>…</think>` 包裹中间推理过程，与最终答案分离。在第 8 章你会看到蒸馏数据也用同样的标签结构。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [reasoning_from_scratch/ch07.py:L143-L155](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch07.py#L143-L155) | 库版 `reward_format`：token id 层面的 think 标签顺序检测（带可覆盖的默认 id 参数） |
| [ch07/03_rlvr_grpo_scripts_advanced/7_6_plus_format_reward.py:L35-L36](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_6_plus_format_reward.py#L35-L36) | 独立训练脚本：think token id 常量、`reward_format`、`compute_grpo_loss_plus_format_reward`、训练循环与命令行参数 |
| [reasoning_from_scratch/ch03.py:L413-L420](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L413-L420) | `render_prompt`：本讲沿用的原始提示模板（`\boxed{ANSWER}` 契约） |
| [reasoning_from_scratch/ch03.py:L93-L118](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L93-L118) | `load_tokenizer_only`：reasoning 分词器的三个开关（chat 模板 + 生成提示 + 自写思考） |
| [reasoning_from_scratch/qwen3.py:L347-L419](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L347-L419) | `Qwen3Tokenizer`：特殊 token 表、分割正则与 `_wrap_chat` 的 `add_thinking` 行为 |
| [ch07/02_logs/7_6_plus_format_reward_metrics.csv](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/02_logs/7_6_plus_format_reward_metrics.csv) | 作者发布的 500 步真实训练日志（含 `format_reward_avg` 列） |
| [ch07/01_main-chapter-code/ch07_main.ipynb](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/01_main-chapter-code/ch07_main.ipynb) | 第 7 章主 notebook 的 7.6 节：本讲义的叙事主线与分词器实验 |

## 4. 核心概念与源码讲解

### 4.1 格式奖励的动机与 think 标签的 token 化

#### 4.1.1 概念说明

到目前为止，我们的奖励信号只有一个维度：答案对不对（`reward_rlvr`）。这个奖励有两个问题：

1. **稀疏且同质**：早期很多 rollout 组里全对或全错，奖励全为 1 或全为 0，组内标准差为 0，优势全零，没有梯度信号（u8-l1 讲过，工程上用 `skip_zero_adv` 跳过这种步）。u8-l3 的 KL 崩溃分析中，「奖励坍缩后 KL 独占梯度」正是这一问题的连锁反应，而 notebook 7.5.2 节列出的第 4 条缓解方案就是：*加一个小格式奖励，防止优势坍缩为零*——本讲正是它的实现。
2. **不约束输出结构**：裸的正确性奖励不关心模型是否把「思考过程」和「最终答案」分开。推理模型（DeepSeek-R1、Qwen3）普遍用 `<think>…</think>` 包裹中间推理，让结构化的思考与可提取的答案各归其位。

格式奖励（format reward）就是把「输出结构合规」变成第二个可验证的 0/1 奖励，与正确性奖励**线性组合**。这与 DeepSeek-R1 的做法同源：除了准确性奖励，再加一个格式奖励要求模型把推理放进 `<think>` 与 `</think>` 之间。

但要奖励 think 标签，前提是这些标签在**分词器里是单个、无歧义的 token**——否则文本级检测会既慢又脆（比如 `"<think>"` 作为子串可能出现在别的词里，或被 BPE 拆碎）。这就是本模块要解决的「token 化」问题。

#### 4.1.2 核心流程

notebook 7.6.1 节的实验流程：

1. 加载 **base** 分词器，`encode("<think>")` 得到 `[13708, 766, 29]`——被拆成 `<th`、`ink`、`>` 三个子词，说明 base 词表没有收录该标签。
2. base 分词器有空闲占位槽位，可以用 `tokenizer_base._tok.add_special_tokens(["<tool_response>", "</tool_response>", "<think>", "</think>"])` 把它们注册进去（返回 4，表示新增 4 个 token），之后 `encode("<think>")` 就是单 token `[151667]`。
3. 更省事的路：**reasoning 分词器原生支持**这两个标签，`tokenizer-reasoning.json` 的词表已收录 `<think>`→151667、`</think>`→151668，无需任何手工注册。
4. 模型侧无障碍：Qwen3 0.6B 的词表大小为 151936（notebook 实测 `model.tok_emb.weight.shape[0]`），token id 0–151935 都有对应的 embedding 行，151667/151668 落在合法区间内。

随后 notebook 定义了 `render_prompt_with_think_tokens`：在提示模板里显式**指示**模型「先在 `<think>` 与 `</think>` 标签内写推理，再在下一行写 `\boxed{ANSWER}`」。但注意独立训练脚本里它被整体注释掉了，作者备注是"The alternative prompt below peforms worse in practice"（实测更差）。最终脚本的实际做法是：

- 保留原版 [render_prompt](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L413-L420)（不在提示里提 think 标签）；
- 改训 **reasoning 变体**模型——它预训练时已大量见过 think 标签，天生会在回答里写 `<think>…</think>`，格式奖励只需「筛选并强化」这个既有行为。

#### 4.1.3 源码精读

**① 注释掉的备选提示模板**（[7_6_plus_format_reward.py:L39-L50](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_6_plus_format_reward.py#L39-L50)）：这段被注释的 `render_prompt_with_think_tokens` 用一条指令把格式要求写进 prompt。它被弃用的原因见上方源码注释——作者实测效果更差。教学上它值得读：**改变奖励不如同时考虑「提示约束 + 奖励约束」两条路线，且二者需要实验对比**，不能想当然。

**② think token id 常量**（[7_6_plus_format_reward.py:L35-L36](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_6_plus_format_reward.py#L35-L36) 与 [ch07.py:L13-L14](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch07.py#L13-L14)）：

```python
THINK_TOKEN_ID = 151667       # <think>
END_THINK_TOKEN_ID = 151668   # </think>
```

硬编码这两个 id 是安全的：分词器文件是确定性资源，reasoning 词表中这两个位置的语义固定不变。

**③ 分词器对 think 标签的处理路径**（[qwen3.py:L347-L357](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L347-L357)）：`_SPECIALS` 表只收录 `<|…|>` 形状的 token，`_SPLIT_RE` 正则 `(<\|[^>]+?\|>)` 也只切这种形状——`<think>` **不匹配**这个形状，于是它走 `encode` 的 BPE 分支（[qwen3.py:L400-L406](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L400-L406)）`self._tok.encode(part).ids`。base 词表里没有这个词，BPE 拆成 `[13708, 766, 29]`；reasoning 词表收录了它，BPE 直接返回单 id `[151667]`。**两个分词器的差异在 JSON 词表文件本身，而不在 `Qwen3Tokenizer` 包装类。**

**④ 为什么 prompt 里不会混入 think 标签**（[ch03.py:L102-L113](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L102-L113)）：`load_tokenizer_only(which_model="reasoning")` 用三个开关构造分词器：`apply_chat_template=True, add_generation_prompt=True, add_thinking=True`。看 [`_wrap_chat`](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L411-L419) 的分支：

```python
if self.add_thinking:
    s += "\n"                      # 不预填 <think>，让模型自己写
else:
    s += "\n<think>\n\n</think>\n\n"  # 预填空思考块，跳过思考
```

`add_thinking=True` 时生成提示止于 `<|im_start|>assistant\n`，**prompt 段不含任何 think token**——这是 `reward_format` 只检查生成段的前提（见 4.2.3 的切片逻辑）。

**⑤ 训练脚本加载 reasoning 变体**（[7_6_plus_format_reward.py:L653-L656](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_6_plus_format_reward.py#L653-L656)）：

```python
model, tokenizer = load_model_and_tokenizer(
    which_model="reasoning", device=device, use_compile=False
)
```

对比第 6 章 GRPO 脚本加载的是 base 变体，这里是本脚本最关键的静默改动。notebook 7.6.2 节解释：若用 GRPO 直接训练 base 模型输出 think 标签，效果会很差——base 模型从未见过这些 token，生成它们的概率近乎为零，策略梯度没有可放大的信号；正确姿势是先（预训练或指令微调）让模型见过 think 数据，或直接选用已具备该行为的 reasoning 变体。

#### 4.1.4 代码实践

**实践目标**：亲手验证「base 分词器拆碎 think 标签、reasoning 分词器单 token、chat 包装不预填 think」三件事。

**操作步骤**（示例代码，需已按 u1-l3 下载分词器文件）：

```python
from pathlib import Path
from reasoning_from_scratch.qwen3 import Qwen3Tokenizer, download_qwen3_small

download_qwen3_small(kind="reasoning", tokenizer_only=True, out_dir="qwen3")

# 1) reasoning 分词器：单 token
tok_r = Qwen3Tokenizer(
    Path("qwen3") / "tokenizer-reasoning.json",
    apply_chat_template=True, add_generation_prompt=True, add_thinking=True,
)
print(tok_r.encode("<think>"))    # 预期 [151667]
print(tok_r.encode("</think>"))   # 预期 [151668]

# 2) base 分词器：被 BPE 拆碎
tok_b = Qwen3Tokenizer(Path("qwen3") / "tokenizer-base.json")
print(tok_b.encode("<think>"))    # 预期 [13708, 766, 29]（notebook 实测值）

# 3) chat 包装：prompt 段不含 think token
ids = tok_r.encode("What is 2+2?")
print(151667 in ids, 151668 in ids)   # 预期 False False
print(tok_r.decode(ids))              # 观察 <|im_start|>assistant 之后只有换行
```

**需要观察的现象**：第 1 步与第 2 步的对比说明「同一个字符串，词表不同则 token 化完全不同」；第 3 步确认 think 标签若出现，必然在生成段。

**预期结果**：与注释一致（这些值均与 notebook 7.6.1 节的已执行输出相同）。注意 `add_special_tokens` 只改内存中的分词器对象、不落盘，所以重新运行脚本不会影响第 2 步的结果。完整运行结果待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 7_6 脚本不用 `render_prompt_with_think_tokens`，也不训 base 模型，而是训 reasoning 变体？

**答案**：提示工程路线（把格式要求写进指令）实测更差（脚本 L39 注释）；而 base 模型从未在含 think 标签的数据上训练过，生成 151667/151668 的概率近乎为零，策略梯度 \(\nabla A_i \log \pi\) 无从放大零概率行为。reasoning 变体已具备写 think 标签的行为，格式奖励只需筛选并强化它，GRPO 才能「锦上添花」而非「无中生有」。

**练习 2**：`reward_format` 在 base 模型的输出上（配合 base 分词器）会表现如何？

**答案**：几乎恒为 0。base 分词器把 `<think>` 拆成 `[13708, 766, 29]`，生成段里根本不存在 151667 这个 id，`gen.index(151667)` 抛 `ValueError`，按 0 分处理。这也再次说明：**格式奖励必须与分词器配套**。

### 4.2 reward_format：token id 顺序检测

#### 4.2.1 概念说明

`reward_format` 回答一个二元问题：*生成部分是否含有一对按正确顺序出现的 think 标签？* 返回 1.0 或 0.0。它的设计有三个值得注意的取舍：

1. **在 token id 层面而非文本层面检测**：单 id 匹配精确且 O(n)（`list.index` 一次线性扫描），避免了解码再正则的往返，也不受子串误匹配干扰。
2. **只看「第一次出现」**：`list.index` 返回首次出现位置，判据是「第一个 `<think>` 先于第一个 `</think>`」。模型若输出多个思考块，只要第一对顺序正确即可得分——检测的是「会开门也会关门」，不是「只开一次门」。
3. **任何缺失都判 0**：任一标签不存在即 `ValueError` → 0.0，不抛异常、不部分给分。

#### 4.2.2 核心流程

```text
输入: token_ids（prompt + 生成的完整 id 序列）, prompt_len（prompt 的 token 数）
 1  gen = token_ids[prompt_len:]        # 只取生成段，剥掉 prompt
 2  i1 = gen 中第一次出现 151667 的下标   # <think>
 3  i2 = gen 中第一次出现 151668 的下标   # </think>
 4  任一不存在 → ValueError → 返回 0.0
 5  返回 float(i1 < i2)                 # True→1.0，False→0.0
```

四种典型情形：

| 生成段（示意） | 结果 | 原因 |
| --- | --- | --- |
| `<think> 推理… </think> 答案` | 1.0 | 首个 `<think>` 下标 < 首个 `</think>` 下标 |
| `推理… </think> 答案`（缺开始） | 0.0 | `index(151667)` 抛 ValueError |
| `<think> 推理…`（缺结束） | 0.0 | `index(151668)` 抛 ValueError |
| `</think> … <think>`（顺序颠倒） | 0.0 | 0 < 2 为 False |

#### 4.2.3 源码精读

库版实现（[reasoning_from_scratch/ch07.py:L143-L155](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch07.py#L143-L155)）：

```python
def reward_format(
    token_ids,
    prompt_len,
    start_think_id=151667,
    end_think_id=151668,
):
    try:
        gen = token_ids[prompt_len:].tolist()      # 剥掉 prompt，只看生成段
        return float(
            gen.index(start_think_id) < gen.index(end_think_id)
        )
    except ValueError:                              # 任一标签缺失
        return 0.0
```

这段代码做了三件事：切片 `token_ids[prompt_len:]` 把 prompt 段的 token 全部排除（4.1.3 的 ④ 保证了 prompt 里本来就没有 think 标签，这层切片是双保险——即使模板变化也只考察模型自己的输出）；`.tolist()` 把张量转成 Python 列表以便用 `list.index`；用 `try/except ValueError` 把「标签缺失」统一折算为 0 分。默认参数 `start_think_id=151667, end_think_id=151668` 让函数可独立复用于其它配套了 think 词表的模型。

脚本版（[7_6_plus_format_reward.py:L142-L147](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_6_plus_format_reward.py#L142-L147)）逻辑完全相同，只是改为引用模块级常量 `THINK_TOKEN_ID` / `END_THINK_TOKEN_ID`。

调用点在 rollout 循环里（[7_6_plus_format_reward.py:L179-L196](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_6_plus_format_reward.py#L179-L196)）：每个 rollout 由 `sample_response` 生成，返回的 `token_ids` 是「prompt + 生成」的完整拼接、`prompt_len` 是 prompt 长度——正好是 `reward_format` 的两个入参；同一批数据同时喂给 `reward_rlvr(text, example["answer"])`（[L132-L139](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_6_plus_format_reward.py#L132-L139)，与第 6 章相同的 boxed 提取 + SymPy 判分链路）。

#### 4.2.4 代码实践

**实践目标**：用 `ch07.py` 的 `reward_format` 对四段构造的 token 序列打分，验证 1/0 行为与两个边界细节。本实践**纯 CPU、无需下载模型**。

**操作步骤**（示例代码）：

```python
import torch
from reasoning_from_scratch.ch07 import reward_format

PROMPT = torch.tensor([101, 202, 303])   # 任意伪 prompt id，占 3 个位置
cases = {
    "正常 <think>…</think>": torch.cat([PROMPT, torch.tensor([151667, 10, 11, 12, 151668, 13])]),
    "缺失开始标签":          torch.cat([PROMPT, torch.tensor([10, 11, 151668, 13])]),
    "缺失结束标签":          torch.cat([PROMPT, torch.tensor([151667, 10, 11])]),
    "顺序颠倒":              torch.cat([PROMPT, torch.tensor([151668, 10, 151667])]),
}
for name, ids in cases.items():
    print(f"{name}: reward_format = {reward_format(ids, prompt_len=3)}")

# 边界 1：think 标签只出现在 prompt 段 → 不计入
only_in_prompt = torch.tensor([151667, 202, 151668, 10, 11])
print("prompt 段含标签:", reward_format(only_in_prompt, prompt_len=3))   # 预期 0.0

# 边界 2：多个思考块 → 只看第一次出现
multi = torch.cat([PROMPT, torch.tensor([151667, 5, 151668, 6, 151667, 7])])
print("多思考块:", reward_format(multi, prompt_len=3))                    # 预期 1.0
```

**需要观察的现象**：前四行输出依次为 `1.0, 0.0, 0.0, 0.0`；边界 1 说明切片确实把 prompt 段排除在外；边界 2 说明判据是「首个 `<think>` 先于首个 `</think>`」，与后续标签无关。

**预期结果**：如上所述（由源码逻辑直接推得，可即时验证）。待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：生成段为 `[151667, 151668]`（`<think>` 后立刻 `</think>`，中间没有任何推理）会得几分？这暴露了什么缺陷？

**答案**：得 1.0——判据只检查顺序，不检查思考内容的长度或质量。这是**奖励与真实目标错位**的典型缺口：模型可以学会「打个空标签」白拿格式分。也正是 4.3 节要引入 `conditional_reward` 的动机之一。

**练习 2**：为什么用 `gen.index(...)`（首次出现）而不是检查「最后一个 `<think>` 是否在最后一个 `</think>` 之前」？

**答案**：`index` 语义对应「模型是否先打开思考块再关闭它」这一最小结构契约，实现最简单、无歧义；若改用「最后一次出现」的判据，多思考块输出的评分会更严格，但也更容易在模型偶发的重复标签下误判为 0。两者都是合理设计，作者选择了宽松版——配合正确性奖励使用时，宽松的格式奖励主要起「维持组内奖励方差」的作用（见 4.3）。

### 4.3 组合奖励：线性加权、条件奖励与奖励黑客

#### 4.3.1 概念说明

两个 0/1 奖励如何合成一个标量？脚本采用最简单的**线性加权**：

\[ r_i = r_i^{\text{correct}} + w \cdot r_i^{\text{format}}, \qquad w = \text{format\_reward\_weight} \]

默认 \( w = 1.0 \)，即满分 2.0（对且格式合规）。另有可选的**条件奖励**（`conditional_reward`）：`format_reward *= rlvr_reward`，格式分只有答案正确时才生效——彻底封死「格式漂亮但答错」的得分路径。

理解这个设计的杠杆在**组内优势的符号**。GRPO 不直接消费奖励绝对值，而是消费 \( A_i = (r_i - \mu)/(\sigma + 10^{-4}) \)。策略梯度会**提高** \( A_i > 0 \) 的 rollout 的概率、**压低** \( A_i < 0 \) 的 rollout 的概率。因此 \( w \) 的真正作用是**重新排列组内 rollout 的相对名次**：

- \( w \) 合适：格式分作为「平局破坏者」——两个都答错的 rollout，格式好的略胜；两个都答对的，格式好的略胜。学习信号更细腻。
- \( w \) 过大：一个「答错但格式好」的 rollout 的总分可能**超过组均值**，获得正优势，被策略梯度强化——模型迅速学会用格式分套利，正确性被牺牲。这就是奖励黑客。
- \( w \to 0\)：退化为纯正确性奖励，回到奖励稀疏的老问题。

#### 4.3.2 核心流程

`compute_grpo_loss_plus_format_reward` 的 rollout 循环中，每个 rollout 依次：

1. `sample_response` 采样 → `(token_ids, prompt_len, text)` 三件套；
2. `reward_rlvr(text, answer)` → 正确性 0/1；
3. `reward_format(token_ids, prompt_len)` → 格式 0/1；
4. （可选）`conditional_reward` 时格式分乘以正确性分；
5. 线性组合 `reward = rlvr_reward + format_reward_weight * format_reward`；
6. 全部 rollout 完成后，对**组合奖励**做组内标准化得优势（[L221-L223](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_6_plus_format_reward.py#L221-L223)）：

   \[ A_i = \frac{r_i - \bar r}{\mathrm{std}(r) + 10^{-4}} \]

7. 后续与 u8-l2/u8-l3 完全一致：重算新 logprob → ratio → clip → pg_loss，再加 KL 项（本实验默认 `kl_coeff=0.0`，KL 关闭）。

**一个手推例子**（后面实践会验证）：设 num_rollouts=4，四个 rollout 的（正确性， 格式）为 \((1,1), (1,1), (0,1), (0,0)\)。

- \( w=1.0 \)：组合奖励 \([2,2,1,0]\)，均值 1.25，样本标准差 ≈ 0.957，优势 ≈ \([0.78, 0.78, -0.26, -1.31]\)。「错但格式好」仍是负优势，被抑制 ✔
- \( w=5.0 \)：组合奖励 \([6,6,5,0]\)，均值 4.25，标准差 ≈ 2.872，优势 ≈ \([0.61, 0.61, +0.26, -1.48]\)。「错但格式好」**翻正**，被强化 ✘ ——奖励黑客发生。

对这个分组可以解出精确阈值：\( r(0,1) = w \)，\( \bar r = \frac{2(1+w) + w}{4} = \frac{2+3w}{4} \)，令 \( w > \bar r \) 解得 \( w > 2 \)。**即权重大于 2 时，该分组里「错但格式好」的 rollout 开始获得正优势。**（阈值随分组构成变化，但方法通用：看辅助奖励能否把总分抬过组均值。）

#### 4.3.3 源码精读

**① 奖励组合的五行核心**（[7_6_plus_format_reward.py:L195-L199](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_6_plus_format_reward.py#L195-L199)）：

```python
rlvr_reward = reward_rlvr(text, example["answer"])
format_reward = reward_format(token_ids, prompt_len)
if conditional_reward:
    format_reward *= rlvr_reward
reward = rlvr_reward + format_reward_weight * format_reward
```

这段是本讲的全部「新算法」：正确性奖励保持原样（权重恒为 1），格式奖励乘权重后叠加；`conditional_reward` 一行把格式分变成正确性分的「乘法门」。注意 [`roll_format_rewards.append(format_reward)`](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_6_plus_format_reward.py#L205) 记录的是**乘权重前**的格式分（若 `conditional_reward=True` 则是门控后的值），因此 CSV 里的 `format_reward_avg` 始终是 0–1 之间的「格式合规率」。

**② 优势在组合奖励上计算**（[L221-L223](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_6_plus_format_reward.py#L221-L223)）：

```python
rewards = torch.tensor(roll_rewards, device=device)
advantages = (rewards - rewards.mean()) / (rewards.std() + 1e-4)
```

没有为两个奖励分别算优势再合并，而是**先合成一个标量再标准化**——这让权重 \( w \) 直接控制两个目标在梯度中的相对话语权。

**③ 损失端与上一讲完全一致**（[L246-L267](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_6_plus_format_reward.py#L246-L267)）：重算 `new_logps`、`ratio = exp(new − old)`、PPO 式 `obj = min(ratio·A, clamp(ratio)·A)`、`pg_loss = -obj.mean()`，再加 KL 项。也就是说，7_6 = 7_4（clip）+ 7_5（KL，默认关闭）+ 格式奖励，是前两讲改进的**叠加**而非替换。

**④ 训练循环与命令行的接线**：

- 函数签名默认值（[L150-L165](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_6_plus_format_reward.py#L150-L165)）：`format_reward_weight=1.0`、`conditional_reward=False`；训练循环把二者透传（[L363-L379](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_6_plus_format_reward.py#L363-L379)）。
- 命令行参数（[L593-L605](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_6_plus_format_reward.py#L593-L605)）：`--format_reward_weight`（别名 `--format_coeff`）默认 1.0，帮助文案"Format reward coefficient for THINK->END_THINK token order"；`--conditional_reward` 是开关型参数，"Only apply format reward if the answer is correct"。
- 新监控指标：每步计算 `format_reward_avg`（[L431](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_6_plus_format_reward.py#L431)），写入文本与 CSV 日志（[L297-L342](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_6_plus_format_reward.py#L297-L342)，CSV 表头含 `format_reward_avg` 列）。

**⑤ 真实日志的证据**。作者发布的 500 步日志（[7_6_plus_format_reward_metrics.csv](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/02_logs/7_6_plus_format_reward_metrics.csv)，默认设置：`kl_coeff=0.0`、`inner_epochs=1`、`w=1.0`、训练 reasoning 变体）：

| 步数 | reward_avg（含格式分） | format_reward_avg | 正确性分量 ≈ 两者之差 | eval_acc（%） |
| ---: | ---: | ---: | ---: | ---: |
| 1 | 0.000 | 0.000 | 0.00 | 50.8 |
| 50 | 1.250 | 0.625 | 0.63 | **56.2**（峰值） |
| 100 | 2.000 | 1.000 | 1.00 | 54.8 |
| 200 | 1.125 | 1.000 | 0.13 | 32.2 |
| 250 | 1.000 | 1.000 | 0.00 | 35.0 |
| 300 | 1.125 | 1.000 | 0.13 | **9.4**（谷底） |
| 500 | 1.750 | 1.000 | 0.75 | 35.0 |

（各行分别对应 CSV 的第 2、51、101、201、251、301、501 行，[第 51 行示例](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/02_logs/7_6_plus_format_reward_metrics.csv#L51)。）读法：`format_reward_avg` 在 100 步后几乎钉死在 1.0（个别步骤更早就到 1）——格式被完美学会；但 eval 准确率从 56.2% 一路跌到 9.4%，正确性分量（reward_avg − format_reward_avg）同步塌缩。notebook 7.6.2 节的解读是：**模型从 think 标签上拿到的奖励太多，对正确性的投入不足**；且后期平均响应长度变短，与「学会偷工减料的格式套利」一致。作者给出的两个改进方向正是降权（`w` 1.0 → 0.1）与开条件奖励（`--conditional_reward`）。顺带一提，日志中 `loss` 恒为 0 是默认设置的计算伪影（单内层 epoch 且无 KL 时新旧策略的逐 token 概率比恒为 1），notebook 7.6.2 节的备注提醒此时应关注 `reward_avg`、`format_reward_avg`、`adv_std`、`entropy_avg` 而不是 loss。

#### 4.3.4 代码实践

**实践目标**：把讲义规格中的两件事做完——(a) 复核 4.2 的 1/0 打分（已在前一模块完成，此处引用其结果）；(b) 把 `format_reward_weight` 从代码默认值 1.0 改为 0.5，手推一个 rewards 组合示例，并量化「权重过大 → 奖励黑客」的边界。

**操作步骤**（示例代码，纯 CPU）：

```python
import torch

def combined(rlvr, fmt, w, conditional=False):
    if conditional:
        fmt = fmt * rlvr
    return rlvr + w * fmt

def group_advantages(pairs, w, conditional=False):
    """pairs: [(正确性, 格式), ...] 一个 prompt 的 rollout 组"""
    rewards = torch.tensor(
        [combined(r, f, w, conditional) for r, f in pairs], dtype=torch.float
    )
    return (rewards - rewards.mean()) / (rewards.std() + 1e-4)

pairs = [(1, 1), (1, 1), (0, 1), (0, 0)]  # 第三个元素 (0,1) 即「答案错误但格式合规」

for w in [0.0, 0.1, 0.5, 1.0, 2.0, 3.0, 5.0]:
    adv = group_advantages(pairs, w)
    wrong_fmt_adv = adv[2].item()        # 「错但格式好」的 rollout
    print(f"w={w:>4}: advantages={[round(a,3) for a in adv.tolist()]}, "
          f"错但格式好的优势={wrong_fmt_adv:+.3f} "
          f"({'被强化 ✘' if wrong_fmt_adv > 0 else '被抑制 ✔'})")

# 条件奖励：彻底封死格式套利
print("conditional:", group_advantages(pairs, w=5.0, conditional=True).tolist())
```

（说明：示例代码。`pairs` 中第三个元素 `(0,1)` 就是「答案错误但格式合规」的 rollout；`group_advantages` 完整复刻脚本 [L221-L223](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_6_plus_format_reward.py#L221-L223) 的标准化公式。）

**需要观察的现象**：

- `w=0.5` 时组合奖励为 `[1.5, 1.5, 0.5, 0]`，均值 0.875、样本标准差 0.75，「错但格式好」的优势 ≈ −0.500（仍被抑制）；
- `w` 从 0 → 5 扫描，「错但格式好」的优势单调上升，在 **w=2 与 w=3 之间由负转正**（理论阈值 w>2，见 4.3.2 的推导）；
- `w=5.0` 时它的优势 ≈ +0.261——策略梯度会提高这条「错误答案 + 漂亮格式」的概率；
- `conditional=True` 时无论 w 多大，格式分被正确性分门控为 0，`[6,6,0,0]` 型奖励不再出现。

**预期结果**：上述数值均可由公式手算复核（例如 w=1.0 时优势 ≈ `[0.783, 0.783, -0.261, -1.305]`）。待本地验证。

**风险讨论（权重过大的奖励黑客机制，写清因果链）**：格式奖励是一个**容易满足的最优子目标**（打一对标签即可），正确性奖励是**困难的真实目标**。\( w \) 过大时，组内出现「错但格式好 > 组均值」的排序倒挂，GRPO 的组相对优势机制会忠实地强化这条捷径；模型在几十步内就能把 format_reward_avg 推到 1.0（真实日志 100 步达成），随后梯度几乎全部流向「维持格式 + 随便作答」，正确性分量塌缩、eval 掉到个位数。防御手段正是源码提供的两个旋钮：降权（notebook 建议 0.1）与 `conditional_reward` 门控。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `reward = rlvr + w * format` 里正确性奖励的权重固定为 1，而不是也乘一个 `correct_weight`？

**答案**： GRPO 只消费组内**相对**排序，所有奖励同乘常数 \( c \) 会被标准化完全抵消（\( (cr - c\mu)/(c\sigma) = (r-\mu)/\sigma \)）。所以唯一有意义的自由度是**两个奖励之间的比例** \( w \)，固定一个为 1、调另一个，是等价且更简洁的参数化。

**练习 2**：`conditional_reward=True` 时，四个 rollout 的（正确性， 格式）为 \((1,1),(0,1),(0,1),(0,0)\)，组合奖励是什么？此时格式奖励还剩什么作用？

**答案**：门控后格式分为 \((1,0,0,0)\)，组合奖励 \([2,0,0,0]\)。「格式好但答错」不再有回报，杜绝了格式套利；格式奖励的作用收窄为「在都答对的 rollout 之间做平局裁决」（如 \((1,1)\) 与 \((1,0)\) → 2 对 1），以及对「答对且格式好」的额外巩固。

**练习 3**：结合 u8-l3：为什么说格式奖励还能缓解 KL 崩溃？

**答案**：u8-l3 的崩溃机理之一是奖励坍缩（全组 0 分）→ 优势全零 → 策略梯度无信号 → KL 项独占梯度把模型推向均匀分布。格式奖励让「全组答错」的步骤仍可能保有奖励方差（有的 rollout 至少格式对），优势不全为零，策略梯度和 KL 保持竞争，训练信号不中断。

## 5. 综合实践

**任务：搭建一个「格式奖励权重实验台」，用计算与真实日志双线验证奖励黑客的边界。**

1. **复刻奖励与优势计算**（纯 CPU，10 行以内）：实现 4.3.4 的 `combined` 与 `group_advantages`。
2. **扫描多种分组构成**：至少再构造三种分组，例如
   - 全对但格式各异：\((1,1),(1,1),(1,0),(1,0)\)；
   - 混合：\((1,1),(0,1),(0,1),(0,0)\)；
   - 全错但格式各异：\((0,1),(0,1),(0,0),(0,0)\)。
   对每种分组扫描 \( w \in \{0, 0.1, 0.5, 1, 2, 3, 5\} \)，记录「错误但格式好」的 rollout 优势由负转正的临界 \( w \)，汇总成一张「分组构成 × 临界权重」表，回答：哪种分组最容易被打劫？（提示：组内正确性奖励越同质，格式分的相对话语权越大——这正是「格式奖励防优势坍缩」的另一面。）
3. **对照真实日志**：读取 [7_6_plus_format_reward_metrics.csv](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/02_logs/7_6_plus_format_reward_metrics.csv)，用 pandas 计算 `correctness ≈ reward_avg − format_reward_avg` 的时间序列，与 eval_acc 并排画图（复用 u8-l1 的 `plot_grpo_metrics` 或手写 matplotlib），验证「format 钉在 1.0、correctness 与 eval 同步下滑」的结论。
4. **（可选，需 GPU 且耗时）**：`uv run ch07/03_rlvr_grpo_scripts_advanced/7_6_plus_format_reward.py --steps 20 --max_new_tokens 256 --format_reward_weight 0.1 --conditional_reward`，对比默认参数下前 20 步的 `format_reward_avg` 爬升速度。若无可运行环境，改为写下你对 notebook 两个补救措施（降权 vs 条件奖励）的预测，并在笔记本 7.6.2 节的图示中找证据。

**验收标准**：能给出至少三个分组的临界权重手算值，并用一句话解释「为什么 w=1.0 在作者的分组构成下足以触发格式套利，而 notebook 却推荐 0.1」。（参考：日志中大量步骤的 rollout 组正确率并不高，此时格式分对组均值的贡献更容易越过排序阈值。）

## 6. 本讲小结

- **格式奖励**把「输出结构合规」变成第二个可验证 0/1 奖励：`reward_format` 在 token id 层面检测生成段中 `<think>`（151667）是否先于 `</think>`（151668），缺失任一标签即 0 分，判据只看首次出现。
- **组合方式是线性加权** `reward = rlvr_reward + w × format_reward`（默认 \( w=1.0 \)，满分 2.0），优势在组合奖励上做组内标准化；\( w \) 的实质作用是重排组内名次，而非改变奖励绝对量（同乘常数会被标准化抵消）。
- **提示工程路线被实验否决**：`render_prompt_with_think_tokens` 把格式要求写进指令，实测更差，脚本中仅存为注释；最终方案是训 **reasoning 变体**（它已会写 think 标签）+ 原版 `render_prompt` + 格式奖励筛选强化。
- **奖励黑客有精确的边界**：对 \((1,1),(1,1),(0,1),(0,0)\) 分组，\( w>2 \) 就会让「错但格式好」的 rollout 获得正优势而被强化；真实日志显示 \( w=1.0 \) 已足够让 format_reward_avg 钉在 1.0、eval 从 56.2% 跌至 9.4%。
- **两个防御旋钮**：降权（1.0 → 0.1）与 `conditional_reward` 门控（格式分乘以正确性分）；此外格式奖励兼任「防优势坍缩」的稳定器，是 u8-l3 KL 崩溃的推荐缓解手段之一。
- 7_6 脚本 = 7_4 的 clip + 7_5 的 KL（默认关闭）+ 本讲的格式奖励，展示了 GRPO 改进逐层叠加的组织方式；新增 `format_reward_avg` 列让「格式学会了没有、有没有在套利」可以直接从日志读出。

## 7. 下一步学习建议

下一讲 **u8-l5（训练变体与 GRPO checkpoint 使用）** 将收束整个第 7 章：概览 `7_7_improvements` 下 DeepSeek-V3.2 风格、Olmo3 风格、GDPO 三个进阶变体（本讲的「多目标奖励线性组合」在 GDPO 中会演化为逐奖励分组归一化），并用 `download_qwen3_grpo_checkpoints` 实际加载作者训练好的 GRPO 检查点。延伸阅读：第 8 章蒸馏数据里的 `<think>…</think>` 字段结构（`ch08.py` 的 `format_distilled_answer`），与本讲的 think 标签机制一脉相承；若想深挖奖励设计，可对照 `reasoning_from_scratch/bonus/parser.py` 思考「更严的格式契约」应如何定义。
