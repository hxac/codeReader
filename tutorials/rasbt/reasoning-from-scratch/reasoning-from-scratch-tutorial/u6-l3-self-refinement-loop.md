# 自我精炼循环

## 1. 本讲目标

前面两讲（u6-l1、u6-l2）我们给模型装上了两个「打分器」：`avg_logprob_answer`（模型对自己输出的平均置信度）和 `heuristic_score`（有 `\boxed{}` 加分、越短越好的规则分）。打分器在 Best-of-N 里的用法是**并行采样、逐个打分、择优**——N 个答案彼此独立，互不知道对方的存在。

本讲换一种完全不同的推理时扩展思路——**自我精炼（self-refinement）**：不让模型「广撒网」，而是让它**串行地自我改进**——先写一个草稿，再扮演审稿人批评这个草稿，然后根据批评意见重写，最后用打分器决定「修改稿是否真的更好」。整个过程像人类写文章：打草稿 → 自我审阅 → 修改 → 定稿。

学完本讲，你应当能够：

1. 写出 critique→refine 两段式 prompt 的构造：`make_critique_prompt` 如何让模型切换到「审稿人」角色，`make_refine_prompt` 如何把批评意见和新答案拼在一起，并重新注入 `\boxed{}` 输出契约。
2. 解释「不低于旧分才接受」的接受准则：`revised_score >= current_score` 这一行如何保证分数单调不减、如何防止越改越差，以及把 `>=` 改成 `>` 会发生什么。
3. 分析自我精炼在 0.6B 模型上的真实效果与局限：结合仓库 README 的 500 题实测数据，说明它为什么能提升基座模型、为什么两轮迭代反而不如一轮、为什么「流畅度打分」会拖推理模型的后腿。

## 2. 前置知识

### 2.1 从「并行采样」到「串行改进」

先把本讲方法放进已学过的推理时扩展家族里对比：

| 方法 | 讲次 | 策略 | N 次生成之间的关系 | 选择机制 |
|------|------|------|--------------------|----------|
| CoT 提示 | u5-l3 | 引导分步思考 | 1 次生成 | 无需选择 |
| 自一致性投票 | u5-l4 | 并行采样 | 相互独立（同题不同采样） | 多数投票 |
| Best-of-N | u6-l2 | 并行采样 | 相互独立 | 打分择优 |
| **自我精炼（本讲）** | u6-l3 | **串行改进** | **后一次依赖前一次的批评** | 打分决定「接受/拒绝」 |

关键区别在「依赖关系」：Best-of-N 的 N 个候选是独立同分布的采样，谁的运气好就选谁；自我精炼的修改稿是**条件生成**——以「原题 + 旧答案 + 批评」为条件产生，理想情况下每一稿都站在前一稿的肩膀上。代价是失去了并行性带来的多样性：如果批评方向错了，修改稿会跟着错。

### 2.2 自我精炼的三个阶段：草稿 → 批评 → 修改

自我精炼的核心假设是：**同一个模型可以通过角色扮演，担任自己的老师**。

1. **草稿（draft）**：模型以「答题者」身份作答——用我们熟悉的 `render_prompt` 模板（u4-l1）。
2. **批评（critique）**：模型以「审稿人」身份检查草稿——找逻辑错误、缺失步骤、算术错误，并给出修复计划。这一步用的是一个全新的 prompt，不再用答题模板。
3. **修改（refine）**：模型以「修订者」身份重写——把原题、旧答案、批评意见三样东西都放进 prompt，要求产出一个以 `\boxed{}` 收尾的新答案。

这三个阶段循环 `iterations` 次。注意批评和修改都是**完整的文本生成**——每次都要跑一遍 prefill + decode（u2-l5），所以自我精炼的计算开销是线性叠加的，这一点后面用公式量化。

### 2.3 接受准则：修改不一定更好，凭什么接受？

LLM 的修改稿完全可能比原稿更差（改对变改错、甚至把对的改错）。如果无条件接受每一轮修改，最终答案就是「最后一稿」，前面写得再好也会被冲掉。所以需要一个**接受准则**：

- 给当前答案和修改稿分别打分（用 u6-l1/u6-l2 的打分器）；
- **只有当修改稿的分数不低于当前答案时才接受**，否则保留旧答案。

这在优化理论里就是最朴素的**贪心爬山（greedy hill climbing）**：每步只往「不更差」的方向走。它带来一个重要性质——**当前分数关于迭代次数单调不减**（后面用数学表述）。而没有打分器时（`score_fn=None`，所有分数恒为 0.0），「不低于」永远成立，循环退化成「无条件接受最新一稿」——这也是仓库实验里 `scoring="none"` 这一组的真实含义。

### 2.4 需要回收的前置术语

- `heuristic_score`（u6-l2）：规则打分器，本讲直接作为 `score_fn` 使用。
- `avg_logprob_answer`（u6-l1）：模型置信度打分器，配合 `functools.partial` 绑定参数后同样可插拔。
- `extract_final_candidate`（u4-l2）：从生成文本提取短答案，`fallback="number_then_full"` 的三级回退。
- `generate_text_stream_concat_flex` + `generate_text_top_p_stream_cache`（u5-l1/u5-l2/u5-l4）：可注入生成引擎的采样函数组合，自我精炼的每一次生成都经过它。
- `render_prompt`（u4-l1）：`\boxed{ANSWER}` 输出契约的来源。

## 3. 本讲源码地图

| 文件 | 角色 |
|------|------|
| [reasoning_from_scratch/ch05.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch05.py) | 第 5 章包代码，本讲全部核心实现：`make_critique_prompt`（L124-134）、`make_refine_prompt`（L137-145）、`self_refinement_loop`（L148-268） |
| [ch05/02_math500-more-inference-scaling-scripts/self_refinement_math500.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch05/02_math500-more-inference-scaling-scripts/self_refinement_math500.py) | 独立评测脚本：把 `self_refinement_loop` 套进 MATH-500 主循环，支持四种打分模式 |
| [ch05/02_math500-more-inference-scaling-scripts/README.md](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch05/02_math500-more-inference-scaling-scripts/README.md) | 自我精炼 500 题完整实测数据表（L38-61）与复现命令，本讲效果分析的依据 |
| [tests/test_ch05.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_ch05.py) | 回归测试：prompt 构造断言（L84-101）与用 monkeypatch 假生成器验证接受/拒绝逻辑（L104-148） |
| [reasoning_from_scratch/ch04.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch04.py) | 生成适配器 `generate_text_stream_concat_flex`（L13-44），循环内所有生成的必经之路 |
| [ch05/01_main-chapter-code/ch05_main.ipynb](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch05/01_main-chapter-code/ch05_main.ipynb) | 教学 notebook 的 5.7 节（单次批评/修改演示）与 5.8 节（完整循环演示），本讲引用其真实运行输出 |

## 4. 核心概念与源码讲解

### 4.1 self_refinement_loop：总体骨架与初始草稿

#### 4.1.1 概念说明

`self_refinement_loop` 是第 5 章的压轴函数：把「草稿 → 批评 → 修改 → 择优」装配成一个可配置的循环。它自己**不实现任何生成或打分逻辑**，而是把三件事委托出去：

- **怎么答题** → `prompt_renderer` 参数（默认 `render_prompt`）+ `prompt_suffix`（可追加 CoT 后缀，u5-l3）；
- **怎么生成** → 固定使用 `generate_text_stream_concat_flex`（适配器）注入 `generate_text_top_p_stream_cache`（温度 + top-p 采样引擎）；
- **怎么打分** → `score_fn` 参数（可插拔，`None` 表示不打分）。

这种「骨架自己写、零件全部注入」的设计让同一个循环能支撑仓库实验矩阵里的所有组合（2 种模型 × 3~4 种打分器 × 1~2 轮迭代）。

#### 4.1.2 核心流程

```text
输入: model, tokenizer, raw_prompt(裸题干), device
      iterations=2, max_response_tokens=2048, max_critique_tokens=256
      score_fn=None, prompt_renderer=render_prompt, prompt_suffix=""
      temperature=0.7, top_p=0.9

阶段 0（初始草稿，只执行一次）:
  prompt = prompt_renderer(raw_prompt) + prompt_suffix
  current_full      = 采样生成(max_response_tokens)      # 草稿全文
  current_extracted = extract_final_candidate(current_full)
  current_score     = score_fn(current_full) 或 0.0

阶段 1~iterations（每轮迭代）:
  快照: draft_before = current_*, score_before = current_score
  critique = 采样生成( make_critique_prompt(题干, draft_before),
                       最多 max_critique_tokens )
  revised  = 采样生成( make_refine_prompt(题干, draft_before, critique),
                       最多 max_response_tokens )
  revised_score = score_fn(revised)
  记录 step{iteration, draft, critique, revised, score_before, score_after}
  若 revised_score >= current_score: 用 revised 覆盖 current_*

返回: {final_full, final_extracted, steps}
```

两个可以立刻推出的量化结论：

- **生成次数**：\( 1 + 2 \times \text{iterations} \)。1 份草稿 + 每轮各 1 份批评、1 份修改。
- **生成 token 预算上界**：\( (1+\text{iterations}) \cdot M_{\text{resp}} + \text{iterations} \cdot M_{\text{crit}} \)。默认配置（2 轮、2048/256）下最多 \( 3\times2048 + 2\times256 = 6656 \) 个生成 token——这就是 README 里自我精炼比基线慢 8 倍的来源。

#### 4.1.3 源码精读

先看函数签名与默认参数——几乎所有自由度都暴露成了关键字参数：

[reasoning_from_scratch/ch05.py:L148-L162](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch05.py#L148-L162) 定义 `self_refinement_loop` 的 11 个参数：`iterations=2`、`max_response_tokens=2048`（答案生成分配的预算，远大于批评的 256）、`score_fn=None`、`prompt_renderer=render_prompt`、`prompt_suffix=""`、`temperature=0.7`、`top_p=0.9`。注意采样温度用的是 0.7，比第 4 章自一致性脚本的 0.9 略低——改进循环里希望每一步生成更「稳」一些。

初始草稿阶段：

[reasoning_from_scratch/ch05.py:L163-L177](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch05.py#L163-L177) 先 `prompt = prompt_renderer(raw_prompt) + prompt_suffix` 渲染出带 `\boxed{}` 契约的答题模板，然后经 `generate_text_stream_concat_flex` 注入 top-p 采样引擎生成草稿全文。适配器的签名见 [reasoning_from_scratch/ch04.py:L13-L44](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch04.py#L13-L44)：它负责把文本 prompt 编码成 token、逐个收集生成器产出的 token、最后 decode 成完整字符串——`generate_func` 与 `**generate_kwargs`（`temperature`、`top_p`）原样透传。

[reasoning_from_scratch/ch05.py:L179-L185](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch05.py#L179-L185) 对草稿做两件事：用 `extract_final_candidate(..., fallback="number_then_full")` 提取短答案；若提供了 `score_fn` 则给草稿全文打分（`score_fn(answer=current_full, prompt=prompt)`），否则记 0.0。这一步建立了循环的初始状态三元组 `(current_full, current_extracted, current_score)`。

#### 4.1.4 代码实践

**实践目标**：在真实模型上跑一次最小规模的自我精炼，观察 `steps` 的结构与草稿演化；无 GPU 也能完成。

**操作步骤**（示例代码，保存为仓库外的脚本运行，如 `refine_demo.py`）：

```python
# 示例代码：最小自我精炼演示（需先完成 u1-l2 的环境安装）
import torch
from reasoning_from_scratch.ch02 import get_device
from reasoning_from_scratch.ch03 import load_model_and_tokenizer
from reasoning_from_scratch.ch05 import self_refinement_loop, heuristic_score

device = get_device()
model, tokenizer = load_model_and_tokenizer("base", device, use_compile=False)
torch.manual_seed(123)

result = self_refinement_loop(
    model=model,
    tokenizer=tokenizer,
    raw_prompt="Half the value of $3x-9$ is $x+37$. What is the value of $x$?",
    device=device,
    iterations=2,
    max_response_tokens=256,   # 压小预算，CPU 也能跑完
    max_critique_tokens=128,
    score_fn=heuristic_score,  # 规则打分器，不额外耗算力
    verbose=True,              # 逐步打印分数变化
)
print("最终答案:", result["final_extracted"])
for s in result["steps"]:
    print(f"第{s['iteration']}轮: {s['score_before']:.3f} -> {s['score_after']:.3f}")
    print("  批评节选:", s["critique"][:80].replace("\n", " "))
```

**需要观察的现象**：

1. `verbose=True` 时终端每轮打印 `[Refinement i/2]`、当前/修改后的提取答案、`Score before/after`；
2. `result["steps"]` 的长度恰为 `iterations`，每个 step 含 8 个字段（`iteration`、`draft_full`、`draft_extracted`、`critique`、`revised_full`、`revised_extracted`、`score_before`、`score_after`）；
3. 若某轮被拒绝，下一轮的 `draft_full` 仍是**旧答案**（快照机制，见 4.3）。

**预期结果**：`final_extracted` 是一个短答案字符串（如 `83` 或其他值）；两轮的分数序列满足 `score_before` 单调不减。具体分数与答案取决于采样随机性——具体数值**待本地验证**（notebook 5.8 节同题、logprob 打分、`torch.manual_seed(1)`、`max_response_tokens=2048` 的一次真实运行为：第 1 轮 `10 → 83`（-0.855 → -0.226，接受），第 2 轮 `83 → 83`（-0.226 → -1.320，拒绝），最终答案 `83`；本实践的更小 token 预算与 heuristic 打分会产生不同的数值）。

#### 4.1.5 小练习与答案

**练习 1**：`iterations=3`、`max_response_tokens=2048`、`max_critique_tokens=256` 时，一次 `self_refinement_loop` 调用最多触发几次文本生成？生成 token 预算上界是多少？

**答案**：生成 \( 1 + 2\times3 = 7 \) 次（1 草稿 + 3 批评 + 3 修改）；预算上界 \( 4\times2048 + 3\times256 = 8960 \) 个 token。

**练习 2**：为什么 `max_critique_tokens` 默认（256）远小于 `max_response_tokens`（2048）？

**答案**：批评只需要「指出问题 + 列修复计划」，`make_critique_prompt` 明确要求 under ~120 words，短批评足够且省时；而答案需要完整推理过程，截断会丢掉末尾的 `\boxed{}`，直接导致提取失败、启发式分数掉档（u6-l2 的三档加分）。

**练习 3**：循环内三次生成（草稿/批评/修改）共用同一组 `temperature=0.7, top_p=0.9`。如果想批评更保守、修改更有创意，需要改哪里？

**答案**：当前实现的循环体内两处 `generate_text_stream_concat_flex(...)` 调用（[ch05.py:L197-L207](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch05.py#L197-L207) 与 [ch05.py:L213-L223](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch05.py#L213-L223)）都硬编码读取同一对参数，需要复制函数后给批评/修改各加一个温度参数——这正是本讲综合实践要做的函数复制改造。

### 4.2 两段式 prompt：make_critique_prompt 与 make_refine_prompt

#### 4.2.1 概念说明

自我精炼的成败系于两个 prompt 的设计。它们都在做同一件事——**用自然语言给模型分配角色**——但分工不同：

- `make_critique_prompt`：把模型从「答题者」切换成「细心的审稿人」。它**不需要** `\boxed{}` 输出契约（批评不是答案），但要约束篇幅、要求给出可操作的修复计划——因为批评的下游消费者是修改 prompt，空洞的「答得不错」无法指导重写。
- `make_refine_prompt`：把「原题 + 旧答案 + 批评」三段材料拼起来，让模型以「修订者」身份重写，并**重新注入 `\boxed{ANSWER}` 契约**——修改稿必须再次机器可解析，否则下游 `extract_final_candidate` 提取不到答案。

两者都采用**补全式引导**（completion-style priming）：prompt 以 `"Critique:"`、`"Revised answer:"` 这样的冒号结尾，把模型「推」进对应角色的第一句话里——这与 `render_prompt` 以 `"Answer:"` 结尾（u4-l1）是同一手法。

还有一个容易被忽略的细节：两个函数的第一个参数都是 **`raw_prompt`（裸题干）**，而不是渲染后的答题模板。批评和修改只需要知道题目本身，套上答题模板反而浪费 token（模板里的格式指令对审稿无意义）。

#### 4.2.2 核心流程

两个 prompt 的拼装结构（`\n\n` 分段）：

```text
make_critique_prompt(题干, 草稿)
┌─────────────────────────────────────────────┐
│ 角色设定: "You are a meticulous reviewer..." │
│ 任务指令: 找逻辑错误/缺失步骤/算术错误;       │
│           答案若对则简短肯定; 给出修复计划     │
│                                             │
│ Question:      {raw_prompt}    ← 裸题干      │
│ Draft answer:  {draft}         ← 待审的草稿  │
│                                             │
│ 篇幅约束: "under ~120 words"                 │
│ 引导后缀: "Critique:"           ← 补全式引导 │
└─────────────────────────────────────────────┘

make_refine_prompt(题干, 草稿, 批评)
┌─────────────────────────────────────────────┐
│ 任务指令: 按批评修改; 保持简洁;               │
│           以 \boxed{ANSWER} 收尾 ← 重新注入契约│
│                                             │
│ Question:        {raw_prompt}  ← 裸题干      │
│ Previous answer: {draft}       ← 被批评的稿  │
│ Critique:        {critique}    ← 批评全文    │
│                                             │
│ 引导后缀: "Revised answer:"     ← 补全式引导 │
└─────────────────────────────────────────────┘
```

注意信息流：**批评的输出整体嵌入修改的输入**。所以批评里若包含正确的修复步骤（如 notebook 里那条「先两边乘 2 消去分数」的 5 步计划），修改稿大概率照做——自我精炼的纠错能力实际上来自「模型在审稿人角色下重新读题」这次机会。

#### 4.2.3 源码精读

[reasoning_from_scratch/ch05.py:L124-L134](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch05.py#L124-L134) 是 `make_critique_prompt` 全文：角色设定句要求「识别逻辑错误、缺失步骤或算术错误；若答案看起来正确，就简短地这么说」——后半句很重要，它允许模型在答案本来就对时**不强行挑刺**，避免把正确答案改错。随后依次拼接 `Question:`（裸题干）与 `Draft answer:`（草稿全文），再用「120 词以内」约束篇幅、以 `"Critique:"` 收尾引导补全。这是一个纯函数：只做字符串拼接，零模型调用。

[reasoning_from_scratch/ch05.py:L137-L145](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch05.py#L137-L145) 是 `make_refine_prompt` 全文：指令句「按批评修改、保持简洁、以 `\boxed{ANSWER}` 收尾」重新 Establish 输出契约；随后三段材料依次是题干、`Previous answer:`（旧稿）、`Critique:`（批评全文），以 `"Revised answer:"` 收尾。

两个函数在循环中的调用点：

[reasoning_from_scratch/ch05.py:L194-L207](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch05.py#L194-L207) 用 `make_critique_prompt(raw_prompt, draft_before_full)` 构造批评 prompt 并采样生成批评（预算 `max_critique_tokens`）；[reasoning_from_scratch/ch05.py:L210-L223](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch05.py#L210-L223) 用 `make_refine_prompt(raw_prompt, draft_before_full, critique_full)` 构造修改 prompt 并生成修改稿（预算 `max_response_tokens`）。注意传的是 `raw_prompt`（裸题干）而非渲染模板，与 4.2.1 的分析一致。

#### 4.2.4 代码实践

**实践目标**：不开模型、不下载权重，验证两个 prompt 构造函数的拼装行为，并对照官方测试断言。

**操作步骤**（纯函数实验，可直接在 Python 交互环境运行）：

```python
# 示例代码：prompt 构造实验（零模型依赖）
from reasoning_from_scratch.ch05 import (
    make_critique_prompt, make_refine_prompt
)

raw_prompt = "What is 1+1?"
draft = "It is \\boxed{2}."
critique_text = "Looks fine."

cp = make_critique_prompt(raw_prompt, draft)
rp = make_refine_prompt(raw_prompt, draft, critique_text)

print(cp)
print("=" * 40)
print(rp)
```

**需要观察的现象**：

1. `cp` 中三段关键内容依次出现：角色句（含 `meticulous reviewer`）、`Question:\nWhat is 1+1?`、`Draft answer:\nIt is \boxed{2}.`，且以 `Critique:` 结尾；
2. `rp` 中出现 `Previous answer:` 与 `Critique:` 两段，且指令句里含 `\boxed{ANSWER}` 字样，以 `Revised answer:` 结尾；
3. 批评文本 `Looks fine.` 被原样嵌入 `rp`——印证「批评输出 → 修改输入」的信息流。

**预期结果**：与 [tests/test_ch05.py:L84-L101](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_ch05.py#L84-L101) 的 `test_prompt_builders_embed_question_and_context` 断言完全一致（该测试检查 `"meticulous reviewer"`、`Question:\n...`、`Draft answer:\n...`、结尾 `Critique:`，以及 refine prompt 中的 `Revised answer:`、`Previous answer:`、`Critique:` 与结尾）。跑 `pytest tests/test_ch05.py::test_prompt_builders_embed_question_and_context -q` 应通过——**待本地验证**。

作为对照，notebook 5.7 节用真模型演示过一次完整的批评/修改（见 [ch05_main.ipynb](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch05/01_main-chapter-code/ch05_main.ipynb) 5.7 节）：题干还是那道「Half the value of $3x-9$…」，初始草稿答 `\boxed{18}`（错）；批评 prompt 产出的批评指出了方程 setup 的理解偏差并给出 5 步修复计划；修改稿答 `\boxed{83}`（对，且与 u5-l3 里 CoT 版的正确答案一致）。这说明 0.6B 基座模型在「审稿人」角色下有概率发现「答题者」角色下犯的错——这正是自我精炼有效的微观机制。

#### 4.2.5 小练习与答案

**练习 1**：批评 prompt 里「If the answer seems correct, say so briefly」这句为什么不能删？

**答案**：它给了模型「通过审查」的出口。若强制必须挑刺，模型会为赋新词强说愁——在答案本来就正确时编造问题，诱导修改稿偏离正确答案；接受准则只能挡住「分数变差」的修改，挡不住「格式更规整但答案改错、分数反而更高」的修改（heuristic 分数不懂对错）。

**练习 2**：`make_refine_prompt` 为什么必须重提 `\boxed{ANSWER}`，而 `make_critique_prompt` 不需要？

**答案**：修改稿要重新进入评测链路，`extract_final_candidate` 靠 `\boxed{}` 提取答案（u4-l2），丢了契约修改稿就「判分必败」；批评只是中间产物，只被 `make_refine_prompt` 整体消费，不需要机器可解析的短答案。

**练习 3**：如果把 `make_refine_prompt` 的 `Previous answer:` 段删掉，只留题干和批评，会发生什么？

**答案**：修改稿失去「正在修改谁」的上下文，模型实际上从零重答一遍题，批评里的「修复计划」失去锚点（计划是针对旧稿的步骤逐条修正的）。自我精炼退化为「换个角色重采样」——串行改进退化成并行采样的另一种形式，迭代也就失去了意义。

### 4.3 接受准则：打分择优与状态更新

#### 4.3.1 概念说明

接受准则是自我精炼的「安全带」，源码里只有一个 if：

```python
if revised_score >= current_score:   # 不低于旧分才接受
    current_full = revised_full
    current_extracted = revised_extracted
    current_score = revised_score
```

形式化一点，设第 \( t \) 轮的当前答案为 \( a_t \)、分数 \( s_t = S(a_t) \)，修改稿为 \( r_t \)：

\[
(a_{t+1},\, s_{t+1}) \;=\;
\begin{cases}
\bigl(r_t,\; S(r_t)\bigr), & S(r_t) \,\geq\, s_t \\[4pt]
(a_t,\; s_t), & \text{否则}
\end{cases}
\]

由此立即得到两条性质：

- **单调性**：\( s_0 \le s_1 \le \cdots \le s_{\text{final}} \)。循环里 `current_score` 只会升不会降。
- **最终答案不一定是最后一稿**：哪一稿的分数最高（在「不低于」意义下最早达到峰值），最终就停留在哪一稿。

`>=` 与 `>` 的差别只在**平分**时出现：`>=` 接受新稿（平局偏爱变化），`>` 保留旧稿（平局偏爱现状）。两种极端情形能把这个差别放大到极致：

1. `score_fn=None`：所有分数恒为 0.0。`>=` 时 `0 >= 0` 恒真 → **每轮都接受**，最终答案是最后一稿（这就是实验里 `scoring="none"` 的语义）；`>` 时 `0 > 0` 恒假 → **永远不接受**，最终答案退回初始草稿，整个循环白跑。
2. `heuristic_score`：分数是「分档整数 + 连续 brevity 项」的浮点和，两稿严格相等罕见，`>=` 与 `>` 的行为几乎一致；差异集中在「两稿都无 boxed 无数字、长度恰好相同」这类边缘情形。

#### 4.3.2 核心流程

每轮迭代围绕状态三元组 \((\text{current\_full},\ \text{current\_extracted},\ \text{current\_score})\) 展开：

```text
┌─ 快照: draft_before_*, score_before ← 当前状态（本轮的"旧稿"）
│
├─ 批评: critique = 生成(make_critique_prompt(题干, 旧稿))
├─ 修改: revised  = 生成(make_refine_prompt(题干, 旧稿, 批评))
├─ 评估: revised_extracted = extract(revised)
│        revised_score = score_fn(revised)   # 用原题 prompt 打分
│
├─ 记账: steps.append({iteration, 旧稿, 批评, 新稿, before, after})
│        # 注意: 记账在裁决之前 → 被拒绝的修改也留有案底
│
└─ 裁决: if revised_score >= current_score:
             三元组整体更新为修改稿
         else:
             三元组不动（下一轮仍批评旧稿）
```

两个设计细节值得强调：**先记账后裁决**——`steps` 里同时保留被接受和被拒绝的轮次，供离线分析「哪一轮改坏了」；**打分用原始 prompt**——修改稿虽然是由 refine prompt 生成的，打分时却传 `prompt=prompt`（初始渲染的答题模板），保证草稿与修改稿在同一个评分语境下比较（对 `avg_logprob_answer` 这类依赖 prompt 条件的打分器尤其重要）。

#### 4.3.3 源码精读

修改稿的提取与打分：

[reasoning_from_scratch/ch05.py:L225-L233](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch05.py#L225-L233) 对修改稿执行与草稿完全对称的两步：`extract_final_candidate(revised_full, fallback="number_then_full")` 提取短答案；`score_fn(answer=revised_full, prompt=prompt)` 打分——源码注释 `# Still use original prompt here` 明确点出「仍用原始 prompt」这个刻意选择。

记账与裁决：

[reasoning_from_scratch/ch05.py:L236-L256](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch05.py#L236-L256) 把本轮全部材料装进 8 字段 step 字典并 append 进 `steps`；`verbose=True` 时打印 `[Refinement it+1/iterations]`、前后提取答案与分数。注意这段**在裁决之前执行**。

[reasoning_from_scratch/ch05.py:L258-L262](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch05.py#L258-L262) 就是接受准则本体：注释写明「Accept revised response if it's not worse」（不更差就接受），`revised_score >= current_score` 成立时三元组整体更新。这是全函数唯一写 `current_*` 的地方——被拒绝的轮次对状态零副作用。

[reasoning_from_scratch/ch05.py:L264-L268](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch05.py#L264-L268) 返回 `{final_full, final_extracted, steps}`——终稿全文、终稿短答案、逐轮审计日志三件套。

官方测试如何「无模型」地验证这套裁决逻辑？看 [tests/test_ch05.py:L104-L148](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_ch05.py#L104-L148) 的 `test_self_refinement_loop_accepts_improving_revisions`：它用 `monkeypatch.setattr` 把 `ch05` 模块里的 `generate_text_stream_concat_flex` 换成一个假函数，按调用顺序依次返回 5 条固定文本（草稿 → 批评1 → 修改1 → 批评2 → 修改2），`score_fn` 取 `len(answer)`（答案越长分越高）。于是「修改1（22 字符）> 草稿（13 字符）→ 接受；修改2（3 字符）< 当前（22 字符）→ 拒绝」这条完整故事线被断言锁死：`final_extracted == "draft with more detail"`、`steps[1]["draft_full"]` 是被接受的修改1（证明状态确实更新）、`prompts_seen` 长度为 5（证明 2 轮迭代恰好 5 次生成）。这个测试同时是 4.1.2 生成次数公式的机器验证。

再配一个真实运行例子（notebook 5.8 节，logprob 打分、`torch.manual_seed(1)`）：第 1 轮草稿提取出 `10`、修改稿 `83`，分数 -0.855 → -0.226，接受；第 2 轮旧稿 `83`、修改稿也是 `83`，但分数 -0.226 → -1.320，**同答案不同表述、流畅度更低，照样被拒**。这既演示了拒绝分支，也再次敲打 u6-l1 的结论：logprob 度量的是「模型觉得顺不顺」，不是「对不对」。

#### 4.3.4 代码实践

**实践目标**：不碰模型，亲手复现「接受/拒绝」两条分支；再把 `>=` 改成 `>` 观察行为差异。

**操作步骤**：

1. 直接运行官方测试（不需要 GPU，秒级完成）：

```bash
pytest tests/test_ch05.py::test_self_refinement_loop_accepts_improving_revisions -v
```

2. 写一个自己的迷你版循环（示例代码，`my_refine.py`，放在仓库外），把裁决条件做成参数：

```python
# 示例代码：接受准则 A/B 实验（零模型依赖）
def refine_oracle(responses, score_fn, accept="ge"):
    """responses 按序消费: 草稿, 批评1, 修改1, 批评2, 修改2, ...
    accept="ge" 对应 >=, "gt" 对应 >"""
    it = iter(responses)
    current = next(it)
    cur_score = score_fn(current)
    trace = [(current, cur_score)]
    while True:
        try:
            _critique = next(it)   # 批评：本实验中不消费内容
            revised = next(it)     # 修改
        except StopIteration:
            break
        rev_score = score_fn(revised)
        ok = rev_score > cur_score if accept == "gt" else rev_score >= cur_score
        trace.append((revised, rev_score, "接受" if ok else "拒绝"))
        if ok:
            current, cur_score = revised, rev_score
    return current, trace

# 场景 A: 平分（两稿同分）→ 两种条件的分歧点
answers = ["draft", "c1", "same", "c2", "same"]
for acc in ("ge", "gt"):
    final, _ = refine_oracle(answers, lambda a: 1.0, accept=acc)
    print(f"平分场景 accept={acc}: 最终答案 = {final!r}")
```

3. 再用 `score_fn=None` 的真实退化情形推演：把 `refine_oracle` 的 `score_fn` 换成 `lambda a: 0.0`，重跑两种条件。

**需要观察的现象**：

- 官方测试通过（**待本地验证**）；
- 平分场景：`accept="ge"` 最终答案是最后一稿 `"same"`，`accept="gt"` 停在第一稿 `"draft"`；
- 全零分场景：`ge` 永远接受（最终 = 最后一稿），`gt` 永远拒绝（最终 = 初始草稿）。

**预期结果**：与 4.3.1 的两条性质完全吻合；这也解释了为什么实验脚本里 `scoring="none"` 与 `scoring="heuristic"` 是两组不同语义的实验——前者「无条件换新」，后者「择优保留」。

#### 4.3.5 小练习与答案

**练习 1**：`score_fn=None` 且接受条件保持 `>=` 时，`self_refinement_loop` 的最终答案等价于什么？

**答案**：等价于「最后一轮的修改稿」（`iterations=0` 时是初始草稿）。因为所有分数都是 0.0，`0 >= 0` 恒真，每轮都接受。

**练习 2**：假设某轮修改稿与当前稿的 heuristic 分数**完全相等**（例如两稿都无 boxed 无数字且字符数相同），`>=` 会怎么做？这有什么隐患？

**答案**：`>=` 接受修改稿。隐患是「平局换新」在打分器噪声大的情况下可能造成无意义的漂移——分数没变但答案变了；不过因为分数单调不减，至少不会「越换越差」。`>` 则更保守，平局时保留旧稿，减少抖动。

**练习 3**：为什么 `steps` 的记账要放在裁决**之前**？

**答案**：让审计日志完整记录每一轮的「旧稿 → 批评 → 新稿 → 分数变化」，包括被拒绝的轮次。下游可据此离线统计「多少轮改进有效、多少轮改坏被拦下」（脚本 jsonl 的 `refinement` 字段就整体保存了它）；若只记被接受的轮次，就无法评估「接受准则拦住了多少次劣化」。

### 4.4 MATH-500 实验脚本与效果分析

#### 4.4.1 概念说明

[ch05/02_math500-more-inference-scaling-scripts/self_refinement_math500.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch05/02_math500-more-inference-scaling-scripts/self_refinement_math500.py) 是把 `self_refinement_loop` 推上 MATH-500 全量评测的独立脚本，结构沿用 u4-l4 建立的评测骨架（渲染→生成→提取→判分→jsonl 落盘→ETA 进度），最大的新增点是 **`--scoring` 四选一**：`none`（不打分，无条件换新）、`heuristic`（规则分）、`logprob`（`avg_logprob_answer` 直接给全文打分）、`logprob_extract`（先提取再打的变体）。它还额外定义了一个包里没有的打分器 `avg_logprob_answer_extract`，专治「logprob 给冗长全文打分时长度吃亏」的问题。

#### 4.4.2 核心流程

```text
main (L242-278)
 ├─ get_data(): 本地 math500_test.json 优先，缺失则从 GitHub 拉取
 ├─ load_model_and_tokenizer(which_model, device, compile)
 └─ evaluate_math500_stream(...)  逐题主循环
      每一题:
      ├─ 按 --scoring 构造 score_fn  ←  四选一（含 partial 绑定）
      ├─ self_refinement_loop(...)    ←  本讲主角，返回 final_extracted + steps
      ├─ grade_answer(extracted, gtruth)   ←  评测阶段才碰标准答案
      └─ 写 jsonl 一行（含完整 refinement 审计日志）
      结束: 打印 Accuracy / Total time / 日志路径
```

与 u4-l4 评测链路的关系完全同构：判分（`grade_answer`）与选择（`score_fn`）严格分离——标准答案只在评测阶段出现，绝不参与自我精炼过程中的任何决策。

#### 4.4.3 源码精读

先看脚本自带的第四种打分器：

[ch05/02_math500-more-inference-scaling-scripts/self_refinement_math500.py:L29-L61](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch05/02_math500-more-inference-scaling-scripts/self_refinement_math500.py#L29-L61) 定义 `avg_logprob_answer_extract`：先用 `extract_final_candidate` 提取短答案（boxed 优先、数字兜底），**不可提取时直接返回 -1e9**（极低分，让这种答案在裁决中天然处于劣势）；可提取时把候选包装成 `"\n\\boxed{" + cand + "}"` 再走与 `avg_logprob_answer` 相同的一次前向 + 答案区间平均 logprob。与 `logprob` 模式的区别：打分对象从「整个生成全文」换成「提取出的短答案」——避免冗长推理过程把平均分摊薄。

打分器分派：

[ch05/02_math500-more-inference-scaling-scripts/self_refinement_math500.py:L97-L120](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch05/02_math500-more-inference-scaling-scripts/self_refinement_math500.py#L97-L120) 在每题循环内按 `scoring` 字符串分派四种 `score_fn`：`heuristic` 直接赋值函数；`logprob` / `logprob_extract` 用 `functools.partial` 把 `model/tokenizer/prompt/device` 预先绑定进 `avg_logprob_answer`（及脚本版变体），只留 `answer` 作为关键字参数——这正是 u6-l1 结尾预告的 partial 技巧在实战中的用法，使两种打分器都满足 `score_fn(answer=..., prompt=...)` 的统一接口；`none` 给 `None`；非法值就地 `ValueError`。

主循环核心：

[ch05/02_math500-more-inference-scaling-scripts/self_refinement_math500.py:L122-L159](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch05/02_math500-more-inference-scaling-scripts/self_refinement_math500.py#L122-L159) 调用 `self_refinement_loop`（透传 `iterations`、`max_response_tokens`、`critique_max_new_tokens`、`score_fn`、`temperature`、`top_p` 等），取 `result["final_extracted"]` 交 `grade_answer` 判分，然后把**整个 result（含 steps 审计日志）**塞进记录的 `refinement` 字段写 jsonl——所以每行日志都能复盘每一轮的批评与裁决。

命令行接口：

[ch05/02_math500-more-inference-scaling-scripts/self_refinement_math500.py:L207-L239](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch05/02_math500-more-inference-scaling-scripts/self_refinement_math500.py#L207-L239) 的 `parse_args` 暴露全部实验变量：`--which_model`（base/reasoning/instruct）、`--dataset_size`（默认 10，前缀切片）、`--max_new_tokens`（默认 2048）、`--critique_max_new_tokens`（默认 192）、`--iterations`（默认 2）、`--temperature 0.7`、`--top_p 0.9`、`--seed 42`、`--scoring`（四选一）、`--prompt_suffix`（可叠加 CoT 后缀，复用 u5-l3 的结论）。

效果数据（DGX Spark、500 题全量、[README.md:L38-L61](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch05/02_math500-more-inference-scaling-scripts/README.md#L38-L61)）：

| # | 方法 | 打分器 | 迭代 | 模型 | 准确率 | 耗时 |
|---|------|--------|------|------|--------|------|
| 1 | 基线（ch03） | – | – | Base | 15.2% | 10.1 min |
| 2 | 自我精炼 | None | 1 | Base | **25.0%** | 84.8 min |
| 3 | 自我精炼 | None | 2 | Base | 22.0% | 165.4 min |
| 4 | 自我精炼 | Heuristic | 1 | Base | 21.6% | 84.7 min |
| 6 | 自我精炼 | Logprob | 1 | Base | 21.4% | 85.3 min |
| 10 | 基线（ch03） | – | – | Reasoning | 48.2% | 182.1 min |
| 13 | 自我精炼 | Heuristic | 1 | Reasoning | **57.8%** | 498.6 min |
| 15 | 自我精炼 | Logprob | 1 | Reasoning | 48.4% | 499.7 min |

四条结论（对照已学方法）：

1. **有效但昂贵**：base 从 15.2% 升到 25.0%（+9.8pp，相对提升约 65%），reasoning 从 48.2% 升到 57.8%——自我精炼对两种模型都有效。但耗时约 8.4 倍（每题 3~5 次完整生成）。
2. **远不如采样类方法**：base + 自我精炼的 25.0%，低于 CoT 提示的 40.6%（u5-l3）和自一致性 n=10 的 52.0%（u5-l4）。0.6B 模型的批评质量不稳——它经常审不出错，甚至把对的改错。
3. **迭代两次常不如一次**：#2→#3 从 25.0% 掉到 22.0%。批评-修改链条越长，错误累积与「无意义改写」越多；接受准则只能拦住「分数变差」的轮次，拦不住「分数没变差但答案错了」的漂移。
4. **打分器是把双刃剑**：base 上「不打分」（25.0%）反而最好；reasoning 上 heuristic（57.8%）最好、logprob（48.4%）几乎白跑——再次印证 u6-l1 的「流畅 ≠ 正确」：logprob 偏爱模型爱说的话，推理模型生成的深度思考天然 logprob 不高，用它裁决会把好答案误判为差。

#### 4.4.4 代码实践

**实践目标**：在 5 题子集上冒烟运行脚本，拿到一份可复盘的 jsonl。

**操作步骤**：

```bash
cd ch05/02_math500-more-inference-scaling-scripts

# 5 题子集 + 小预算 + heuristic 打分（CPU 可承受；GPU 更快）
uv run self_refinement_math500.py \
    --which_model "base" \
    --dataset_size 5 \
    --iterations 1 \
    --max_new_tokens 512 \
    --critique_max_new_tokens 128 \
    --scoring "heuristic" \
    --seed 42
```

（非 uv 用户把 `uv run` 换成 `python`，见 [README.md:L17](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch05/02_math500-more-inference-scaling-scripts/README.md#L17)。）

**需要观察的现象**：终端逐题打印 ETA 进度行，结尾输出 `Accuracy: xx.x% (n/5)`、`Total time` 与日志文件名 `math500_base-<device>-self-refine-heuristic.jsonl`；打开 jsonl，每行含 `index/problem/gtruth_answer/generated_text/extracted/correct/refinement`，其中 `refinement.steps[0]` 里能读到该题的批评全文与 `score_before/score_after`。

**预期结果**：5 题准确率在 0%~40% 之间波动均属正常（样本太小）；`correct` 字段与 `extracted`/`gtruth_answer` 的关系可用 u4-l3 的 `grade_answer` 手工复核。总耗时与准确率具体数值**待本地验证**（取决于设备；500 题全量在 DGX Spark 上为 84.7 分钟，5 题按比例估算约 1 分钟量级，CPU 会显著更慢）。

#### 4.4.5 小练习与答案

**练习 1**：`avg_logprob_answer_extract` 对「提取不出任何候选」的答案返回 -1e9。结合接受准则，这个极低分会产生什么行为？

**答案**：`-1e9` 几乎必然小于当前分数，该修改稿会被拒绝——相当于给「不可解析的答案」加了一道一票否决。但注意边界：若**初始草稿**就不可提取，`current_score` 也是 -1e9，此时 `-1e9 >= -1e9` 成立，修改稿仍会被接受（只要它不至于更低）——平局兜底恰好给了「从不可解析自救」的机会。

**练习 2**：为什么 `--scoring logprob` 需要用 `partial` 预绑定参数，而 `--scoring heuristic` 直接赋值即可？

**答案**：循环统一按 `score_fn(answer=..., prompt=...)` 的双关键字接口调用。`heuristic_score` 的签名天然是 `(answer, prompt=None, ...)`，直接满足；`avg_logprob_answer` 的必填参数是 `(model, tokenizer, prompt, answer, device)`，必须用 `partial` 把 `model/tokenizer/prompt/device` 先钉死，只留 `answer` 接收循环传入——这是把任意函数「适配成统一打分器接口」的标准手法。

**练习 3**：基于本表数据，如果只有一次 500 题评测的预算、目标是 base 模型的最高准确率，你会在「自我精炼」和「CoT + 自一致性」之间怎么选？依据是什么？

**答案**：选 CoT + 自一致性（52.0%）。依据三方面：准确率上限（52.0% vs 25.0%）；单位时间收益（自一致性 328 分钟拿 43.2%（n=3），自我精炼 85 分钟拿 25.0%，折算每小时提升相近但上限差距大）；鲁棒性（自我精炼第二轮回退、打分器选择敏感，调参风险高）。自我精炼的价值更多在于「与投票/打分正交」——可作为组合成分，而非单打独斗的最优解。

## 5. 综合实践

**任务**：完整走一遍「单题精炼 + 接受准则 A/B + 5 题汇总」三段实验，把本讲三个最小模块串起来。

**步骤 1——单题观察**：用 4.1.4 的脚本，在一道数学题（如 `Half the value of $3x-9$ is $x+37$. What is the value of $x$?`）上运行：

```python
torch.manual_seed(42)
result = self_refinement_loop(
    model, tokenizer, raw_prompt, device,
    iterations=2, max_response_tokens=256, max_critique_tokens=128,
    score_fn=heuristic_score, verbose=False,
)
for s in result["steps"]:
    print(f"第{s['iteration']}轮 score: {s['score_before']:.3f} -> {s['score_after']:.3f}")
    print(f"  草稿提取: {s['draft_extracted']!r} -> 修改提取: {s['revised_extracted']!r}")
    print(f"  批评前 120 字符: {s['critique'][:120]!r}")
print("最终:", result["final_extracted"])
```

记录两轮的 `score_before/score_after`、批评要点、每轮是否被接受（比较下一轮 `draft_full` 是否换成了本轮 `revised_full`）。

**步骤 2——改接受条件**：把 `self_refinement_loop` 复制为自己的 `self_refinement_loop_strict`（示例代码，放仓库外），只改一处——[ch05.py:L259](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch05.py#L259) 的 `>=` 改为 `>`。用**同一个随机种子**对同一道题分别跑原版与严格版，填表对比：

| 条件 | 第 1 轮 before→after（接受?） | 第 2 轮 before→after（接受?） | final_extracted |
|------|------------------------------|------------------------------|-----------------|
| `>=`（原版） |  |  |  |
| `>`（严格版） |  |  |  |

两版结果可能相同（无平局时行为一致），也可能不同（出现平分轮次时 `>` 更保守）——把实际观察写进表格，若相同请解释为什么（提示：heuristic 分数的 brevity 项是连续浮点）。

**步骤 3——5 题子集汇总**：仿照 `evaluate_math500_stream`（[self_refinement_math500.py:L64-L182](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch05/02_math500-more-inference-scaling-scripts/self_refinement_math500.py#L64-L182)）写一个 15 行左右的汇总循环：`get_data()` 取前 5 题 → 对每题分别调用原版与严格版循环 → `grade_answer` 判分 → 输出两版各自的正确题数。也可以直接跑两遍 4.4.4 的脚本命令（`--scoring heuristic` 与 `--scoring none`）作对照。

**预期结果**：步骤 3 的两版准确率差异通常为 0 或 ±1 题；若观察到差异，回到 jsonl/`steps` 里找那道「平局轮次」的题，确认差异确实来自裁决方向而非采样噪声（固定 seed 可以排除采样差异）。全部数值**待本地验证**。

## 6. 本讲小结

- **自我精炼 = 串行改进**：草稿 → 批评 → 修改 → 打分裁决，循环 `iterations` 轮；生成次数 \(1+2\times\text{iterations}\)，开销线性叠加（500 题实测约 8 倍于基线）。
- **两段式 prompt 靠角色扮演与补全式引导**：`make_critique_prompt` 让模型当审稿人（裸题干 + 草稿 + 120 词约束 + `"Critique:"` 收尾）；`make_refine_prompt` 把原题、旧稿、批评拼在一起并重新注入 `\boxed{}` 契约，以 `"Revised answer:"` 收尾。
- **接受准则保证单调不减**：`revised_score >= current_score` 才整体更新状态三元组；`steps` 先记账后裁决，被拒轮次也留档；`score_fn=None` 时退化为「无条件换新」（`>=`）或「永远不换」（`>`）。
- **打分器可插拔**：`heuristic_score` 直接赋值，`avg_logprob_answer` 用 `partial` 绑定参数，统一满足 `score_fn(answer=..., prompt=...)` 接口；脚本还提供 `avg_logprob_answer_extract`（先提取再打分，不可提取记 -1e9）。
- **0.6B 上的真实战绩**：base 15.2%→25.0%、reasoning 48.2%→57.8%，但两轮常不如一轮，且全面落后于 CoT + 自一致性（52.0%）——批评质量是瓶颈，接受准则只能拦「分数变差」，拦不住「分数不差但答案错」。
- **与判分严格分离**：`grade_answer` 只在评测阶段碰标准答案，精炼过程中的所有决策都不作弊。

## 7. 下一步学习建议

自我精炼把「改进」留在推理时：每道题都要现场跑 3~5 次生成，权重一个字节都没变，换个题目一切从零再来。第 6 章开始换赛道——**把改进固化进权重**：下一讲 [u7-l1 RLVR 奖励函数与训练数据](u7-l1-rlvr-reward-and-data.md) 将介绍可验证强化学习（RLVR）的奖励设计 `reward_rlvr`（强制 `\boxed{}` + `grade_answer` 判分——你会发现它正是本讲 `heuristic_score` 的「真裁判」版本）与训练数据加载 `load_math_train`。建议先复习 u4-l3 的 `grade_answer`（u7 的奖励直接复用它），再预习 `reasoning_from_scratch/ch06.py` 的前 100 行，留意 `reward_rlvr` 与 `heuristic_score` 在「分档 vs 判分」上的本质区别。
