# 元验证准备：prepare_meta_verification 与低分评价复核

## 1. 本讲目标

上一讲（u4-l2）我们读完了 `prepare_proof_verification`：它把生成器的输出加工成验证器请求。本讲顺着数据流往下一站走，精读 [inference/main.py](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py) 中的 `prepare_meta_verification` 函数，以及它在 [inference/math_templates.py](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/math_templates.py) 中对应的 `meta_verification` 模板。

学完本讲，你应该能够：

1. 逐关卡解释 `prepare_meta_verification` 的过滤逻辑，并说明 `score > 0.75` 这个阈值为什么在 0/0.5/1 三档评分契约下等价于「只把 0 分与 0.5 分的低分评价送入元验证」。
2. 说明元验证输入如何由 `statement`（题面）、`proof`（证明）、`rating`（评价）三段拼装而成，以及 `rating` 字段为什么必须原样保留。
3. 对比 `prepare_meta_verification` 与 `prepare_proof_verification` 在字段处理上的差异，理解「静默丢弃」与「断言崩溃」两种容错风格的取舍。

## 2. 前置知识

本讲默认你已读过 u4-l2。以下几个概念会反复出现，先用通俗语言铺垫一遍。

**元验证（meta-verification）：给阅卷老师的批语打分。** 把流水线想象成一场考试：生成器是学生，写证明（答卷）；验证器是阅卷老师，给证明打 0/0.5/1 分并写出批语（评价）。老师也会犯错——把好证明误判为差证明。元验证器就是「复核老师批语的督考员」：它不重新解题，只检查批语本身是否站得住脚（指出的缺陷是否真的存在、给的分数与指出的缺陷是否匹配）。这就是 u1-l1 讲过的三角色分工中「元验证器」的落地代码。

**为什么只复核低分评价？** 验证器打低分（0 或 0.5）意味着这条批语里「指控了证明有缺陷」。如果指控是误判，一个好证明就会被冤枉。而打 1 分的批语基本不含缺陷指控——按模板规则，对「未发现任何缺陷」的批语，元验证能做的主要只剩表达与分数一致性检查，复核收益低。于是代码用 `score > 0.75` 一刀切：高分批语直接放行，不进元验证。这也是成本控制——元验证是额外的 API 调用，算力要花在刀刃上。

**`drop_thought`（切掉思维链）。** u2-l1 讲过，`APIModel` 会把 `reasoning_content` 与 `content` 拼成 `<think>...</think>正文` 的单字符串。下游加工时通常只想要正文，于是按 `</think>` 切一刀、取最后一段。`drop_thought=True` 就是「执行这一刀」的开关。

**join 主键。** 两个数据文件要按某个字段精确匹配合并，这个字段就是 join 主键。本讲会看到一个非常朴素的 join：元验证的输出靠 `rating`（评价全文文本）与验证输出里的评价文本做字符级精确匹配。这决定了 `rating` 字段必须被原样保留。

**评分契约。** u3-l1 讲过，`proof_verification` 模板硬性规定验证器最终分数只能写进 `\boxed{}`，且取值只能是 0、0.5 或 1。本讲的阈值分析完全建立在这个契约之上。

## 3. 本讲源码地图

| 文件 | 本讲关注范围 | 作用 |
|---|---|---|
| [inference/main.py:118-154](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L118-L154) | `prepare_meta_verification` 全函数 | 本讲主角：把验证输出加工成元验证输入 |
| [inference/main.py:66-116](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L66-L116) | `prepare_proof_verification` | 上一讲的主角，本讲只作对照，不重复展开 |
| [inference/math_templates.py:46-125](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/math_templates.py#L46-L125) | `meta_verification` 模板 | 元验证器的完整提示词，含评级决策树 |
| [inference/main.py:44-49](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L44-L49) | 元验证相关 argparse 参数 | `--skip_meta_verification` 开关与采样数 |
| [inference/main.py:502-523](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L502-L523) | 主循环中的调用点 | 断点续跑检查与 `os.system` 生成命令 |
| [inference/main.py:297-317](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L297-L317) | `prepare_proof_refinement` 的开头 | 元验证输出的下游消费点（`rating2quality`） |
| [inference/run.sh](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/run.sh) | 竞赛配置 | 注意它开启了 `--skip_meta_verification` |

## 4. 核心概念与源码讲解

### 4.1 prepare_meta_verification：四道关卡与三段式拼装

#### 4.1.1 概念说明

`prepare_meta_verification` 是主循环第三阶段（元验证）的数据准备函数。它的输入是上一阶段验证器的输出文件 `proof_verification_R{R}/output.jsonl`，输出是 `meta_verification_R{R}/input.jsonl`。

它要解决的问题是：验证输出里既有高分评价也有低分评价，既有完整样本也有截断或格式损坏的样本，而元验证（一次额外的 API 调用）只应该花在「值得怀疑的低分评价」上。函数因此实现了一个漏斗：每条记录依次过四道关卡，全部通过才能成为一条元验证请求。

与 u4-l2 的三步流水（闸门 → 解析自评 → 重装记录）对照，本函数结构类似但取舍不同：它没有自评解析，多了分数阈值过滤；容错风格从「断言崩溃 / 记 0 分保留」统一变成「静默丢弃」。

#### 4.1.2 核心流程

```text
读入 proof_verification_R{R}/output.jsonl
对每条 item：
    关卡 1（完整性）: finish_reason == 'stop' 且 output 含 '</think>'
        不满足 → 整条跳过（不进入 if 块）
    关卡 2（切分思维链）: drop_thought=True 时 rating 取 </think> 之后的部分
    关卡 3（分数解析）: extract_boxed_answers 取最后一个非空值转 float
        解析失败（含 boxed 缺失的 IndexError）→ continue 丢弃
    关卡 4（低分过滤）: score > 0.75 → continue 跳过
    拼装: meta_verification 模板渲染 statement / proof / rating 三槽位
    重装: 写入 messages 与 rating 字段，弹出 finished/finish_reason/input/output
写出 tar_path（JSONL 逐行）并返回条数
```

四道关卡串成一个漏斗，可以用集合语言描述第 4 关：

\[ \text{进入元验证的评价集} = \{\, r :\ \text{score}(r) \le 0.75 \,\} \]

在评分契约 \( \text{score}(r) \in \{0,\ 0.5,\ 1\} \) 下，这个条件等价于 \( \text{score}(r) \in \{0,\ 0.5\} \)——恰好筛掉 1 分。代码不写 `score == 1` 而写 `score > 0.75`，是一种防御性区间判断：万一模型违反契约输出 0.8 之类的中间值，`> 0.75` 会把它当高分放行，而 `== 1` 不会。同时也规避了浮点相等比较的脆弱写法。

#### 4.1.3 源码精读

先看函数签名与整体骨架（本讲主角，仅 37 行）：

[main.py:118-122](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L118-L122) —— 定义 `prepare_meta_verification(path, tar_path, drop_thought=True)`，读入验证输出、准备空列表累加结果。`drop_thought` 默认开启，主循环调用时不传该参数。

**关卡 1：完整性闸门。**

[main.py:123-125](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L123-L125) —— 只有 `finish_reason == 'stop'` 且输出里含 `</think>` 的记录才进入加工；否则整条静默跳过。注意两个细节：其一，这里没有像 u4-l2 那样先 `.lower()`——它依赖 u2-l1 讲过的契约：`APIModel` 输出时已把 `finish_reason` 小写化。其二，"stop 却缺 `</think>`"在这里不触发 `assert`，与 u4-l2 的 `assert '</think>' in prover_output` 形成鲜明对比。取舍逻辑：生成器输出缺 `</think>` 意味着上游引擎违约，属程序性错误，值得崩溃暴露；验证输出丢一条只是损失一个评价样本，元验证本身又是可选环节（见 4.3），静默跳过更稳。

**关卡 2：切掉思维链。**

[main.py:125-127](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L125-L127) —— `rating` 先取完整输出，若含 `</think>` 则取其后的正文部分。元验证器要复核的是「批语正文」，不需要看验证器的思维链。

**关卡 3：从批语里提取分数。**

[main.py:128-132](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L128-L132) —— 用 u3-l2 精读过的 `extract_boxed_answers` 提取所有非空 boxed 值，取**最后一个**转 float。`try/except` 同时兜住两种失败：boxed 缺失时 `scores[-1]` 抛 `IndexError`，内容不可转 float 时抛 `ValueError`，一律 `continue` 丢弃。对照 u4-l2 的「两层异常分工」（小节缺失丢样本、boxed 坏记 0 分保留）：这里统一丢弃，因为本阶段产出的 `score` 只用于过滤，不是要保留的业务字段。

**关卡 4：低分过滤，本函数的灵魂。**

[main.py:133-134](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L133-L134) —— `score > 0.75` 即跳过。如 4.1.2 所析，契约内等价于「丢掉 1 分评价」，只让 0 与 0.5 档的批语进入复核。

**三段式拼装与字段重装。**

[main.py:135-145](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L135-L145) —— 用 `meta_verification` 模板渲染三个槽位：`statement` 填题面（`item['question']`）、`proof` 填证明、`rating` 填切分后的批语正文。随后 `item.update` 写入新的 `messages` 与 `rating` 字段。这里的 `item['proof']` 正是 u4-l2 中 `extract_solution` 切分后的纯 Solution 文本——元验证器看到的证明与验证器当时看到的是同一份字符串，保证「批语 ↔ 证明」的对应关系不失真。新写入的 `rating` 字段则原样保留批语全文，它是下游 join 的主键（见 4.3.1）。

**清理与落盘。**

[main.py:146-154](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L146-L154) —— 弹出 `finished / finish_reason / input / output` 四个残留键（注意 `proof_finish_reason` 不在清理列表，会随行透传），逐行写出 JSONL 并返回条数。清理列表与 u4-l2 完全相同，是两个 prepare 函数共享的「出舱清理」惯例。

#### 4.1.4 代码实践

**实践目标**：在 u4-l2 的 `build_verification_input.py` 基础上扩展，手造三条评分分别为 1、0.5、0 的验证输出，复刻 `prepare_meta_verification` 的过滤逻辑，验证 1 分记录被跳过、其余记录的 `rating` 被完整保留并渲染进模板。纯本地数据处理，不调用任何 API。

**操作步骤**：

1. 在教程工作目录（`DeepSeek-Math-V2-tutorial/`）新建 `build_meta_verification_input.py`。
2. 手造输入数据：同一道题、同一份证明，三条验证输出只差 `\boxed{}` 里的分数。
3. 复刻四道关卡。注意源码函数通过全局 `args.meta_verification_template` 取模板，与 argparse 耦合；复刻时直接用字符串键 `"meta_verification"` 解耦。
4. 运行并检查断言。

下面是示例代码（标注：**示例代码**，非仓库原有文件）：

```python
# build_meta_verification_input.py（示例代码）
import json, os, sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "inference"))
from math_templates import math_templates   # 复用仓库真实模板
from utils import extract_boxed_answers     # 复用仓库真实解析器

def make_item(score_box):
    # 评价正文：含思维链 + 固定格式批语，仅末尾分数不同
    output = ("<think>让我逐步核对这份证明……</think>\n"
              "Here is my evaluation of the solution:\n(省略分析)\n"
              "Based on my evaluation, the final overal score should be:\n"
              + score_box)
    return {
        "question": "Prove that the sum of two even integers is even.",
        "problem_idx": "TEST-1", "source_name": "mytest",
        "proof": "Let a = 2m and b = 2n ...",
        "self_eval": "...", "self_eval_score": 0.5,
        "prover_output": "...", "proof_finish_reason": "stop",
        "finish_reason": "stop", "output": output,
    }

items = [make_item(s) for s in ["\\boxed{1}", "\\boxed{0.5}", "\\boxed{0}"]]

def prepare_meta_verification_local(items, drop_thought=True):
    data = []
    for item in items:
        problem = item["question"].strip()
        if item["finish_reason"] == "stop" and "</think>" in item["output"]:
            rating = item["output"].strip()
            if drop_thought and "</think>" in rating:
                rating = rating.split("</think>")[-1].strip()
            scores = [s.strip() for s in extract_boxed_answers(rating) if s.strip()]
            try:
                score = float(scores[-1])
            except:
                continue
            if score > 0.75:
                continue
            inp = math_templates["meta_verification"].format(
                statement=problem.strip(),
                proof=item["proof"].strip(),
                rating=rating.strip())
            item.update({"messages": [{"role": "user", "content": inp}],
                         "rating": rating})
            for key in ["finished", "finish_reason", "input", "output"]:
                item.pop(key, None)
            data.append(item)
    return data

result = prepare_meta_verification_local(items)
assert len(result) == 2, "1 分记录应被关卡 4 跳过"
for r in result:
    assert float(extract_boxed_answers(r["rating"])[-1]) <= 0.75
    assert "<think>" not in r["rating"], "rating 应已切掉思维链"
msg = result[0]["messages"][0]["content"]
assert "## Problem" in msg and "## Solution" in msg \
       and "## Solution Evaluation" in msg
print(json.dumps(result[0], indent=2, ensure_ascii=False)[:800])
```

运行方式（在教程目录下）：`python build_meta_verification_input.py`。

**需要观察的现象**：断言是否全部通过；打印出的记录里 `rating` 是否为不含 `<think>` 的批语正文；`messages[0]['content']` 是否呈现 `## Problem / ## Solution / ## Solution Evaluation` 三段结构；透传字段（`problem_idx`、`proof`、`self_eval_score`、`proof_finish_reason`）是否随行保留。

**预期结果**：3 条输入产出 2 条输出，`\boxed{1}` 的记录消失；两条留存记录的 `rating` 以 "Here is my evaluation" 开头、以各自 `\boxed{...}` 结尾。本实践为纯本地脚本，具体运行输出待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：把过滤条件从 `score > 0.75` 改成 `score == 1`，在评分契约内行为是否相同？什么情况下会不同？

> **答案**：契约内（模型只输出 0/0.5/1）行为完全相同。不同点出现在模型违约输出中间值时：如输出 0.8，`> 0.75` 会把它当高分跳过，而 `== 1` 判定不成立（0.8 ≠ 1），该记录会进入元验证。`> 0.75` 把「任何落入高分区间的违规值」都视为无需复核，更稳健。

**练习 2**：为什么 `stop` 但缺 `</think>` 的记录，在 `prepare_proof_verification` 里触发 `assert` 崩溃，在 `prepare_meta_verification` 里却静默跳过？

> **答案**：u4-l2 阶段，生成器输出缺 `</think>` 意味着违反了 `APIModel` 的输出拼接契约，属程序性错误，用 `assert` 当场暴露便于排查；本阶段丢掉一条验证输出只是少一个评价样本，且元验证整体是可选增强环节（run.sh 甚至直接跳过），静默跳过保证流水线鲁棒。两种风格的不一致本身也可视为可改进点。

**练习 3**：新写入的 `rating` 字段如果被改成只保留前 100 个字符，下游会发生什么？

> **答案**：`rating` 是下游 `rating2quality` 映射的 join 主键——`prepare_proof_refinement` 两边各自切分出评价全文后做字符级精确匹配（见 4.3.1）。截断后键对不上，元验证质量信息会全部丢失（`rating2quality.get(rating, [])` 返回空列表），且不会有任何报错。

### 4.2 meta_verification 模板：评「评价」的分析框架与决策树

#### 4.2.1 概念说明

`math_templates["meta_verification"]` 是元验证器的完整提示词，在 `str.format` 时消费 4.1 节看到的三个槽位。它与 `proof_verification` 模板构成「两层审稿」结构：后者给解答打分，前者给「打分意见」打分。模板的全部设计围绕一个反直觉的约束：**元验证器不解题、也不判断解答对错**，它只判断批语自身是否合理。

#### 4.2.2 核心流程

模板可拆成四块：任务定位 → 嵌入的验证器规则 → 四维分析框架 → 评级决策树与输出格式。决策树是核心，用文字画出来：

```text
批语是否指出了缺陷（错误/遗漏）？
├── 是 → 这些缺陷是否全部合理？
│   ├── 全部不合理       → 元验证质量 0
│   ├── 部分合理部分不合理 → 元验证质量 0.5
│   └── 全部合理         → 转入表达分析 / 分数分析
└── 否（未指出任何缺陷）  → 直接转入表达分析 / 分数分析
    ├── 发现表达错误，或分数与所指缺陷不匹配 → 质量 0.5
    └── 均无问题 → 质量 1
```

#### 4.2.3 源码精读

**任务定位与角色边界。**

[math_templates.py:46-49](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/math_templates.py#L46-L49) —— 开篇即声明任务是评估「solution evaluation」是否合理，并交代这份评价是按什么规则生成的。

**嵌入验证器的评分规则。**

[math_templates.py:49-58](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/math_templates.py#L49-L58) —— 把 `proof_verification` 的 0/0.5/1 三档标准（外加「引用论文结论必须自行证明」条款）原文嵌入，并明确标注 "these are not your rules"。元验证器必须知道这把尺子，才能做后文的分数分析：批语给的分数与它自己指出的缺陷是否匹配。

**四维分析框架。**

[math_templates.py:62-72](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/math_templates.py#L62-L72) —— 规则 2 列出四个分析维度：Step Restatement（批语复述的解答行为，解答里是否真有）、Defect Analysis（指出的缺陷是否真实存在且分析准确）、Expression Analysis（表述是否准确）、Score Analysis（分数与缺陷是否匹配）。

**缺陷分析优先与「正面不在范围」原则。**

[math_templates.py:74-84](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/math_templates.py#L74-L84) —— 规则 3 声明缺陷分析最重要，并规定：批语中对解答的**正面肯定**无论对错都不在评审范围内。极端情形（第 77 行）：批语若认为解答完全正确、未发现任何缺陷，那么即使解答明显有错，其「错误分析」仍视为合理——因为它压根没做错误分析。这一原则把元验证的火力集中在对负面指控的核查上，与前述「只复核低分评价」的代码设计互为表里：低分批语必然含缺陷指控，正是这套规则要审的东西。

**表达错误的界定。**

[math_templates.py:86-92](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/math_templates.py#L86-L92) —— 列举表达错误：把某步骤判错后却断言其后续结论「错误」而非「未被严格证明」、批语自身的笔误与计算错误、对解答内容的失实复述。并特别澄清：把错步骤说成对步骤**不算**表达错误（那属于缺陷分析的漏报）。

**评级决策树与输出格式。**

[math_templates.py:94-103](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/math_templates.py#L94-L103) —— 即 4.2.2 画出的决策树原文：存在不合理缺陷时只做缺陷分析（全不合理给 \(0\)，部分合理给 \(0.5\)）；无缺陷或缺陷全合理时做表达与分数分析（有问题给 \(0.5\)，无问题给 \(1\)）。[math_templates.py:105-111](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/math_templates.py#L105-L111) 规定输出以 `\boxed{...}` 收尾——与 u3-l1、u3-l2 讲过的「格式契约 ↔ 解析器」配对：下游 `prepare_proof_refinement` 正是用 `extract_boxed_answers` 取最后一个值作质量分。

**三段式任务输入。**

[math_templates.py:113-124](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/math_templates.py#L113-L124) —— `## Problem {statement}`、`## Solution {proof}`、`## Solution Evaluation {rating}` 三段，正是 4.1.3 中三个 `format` 槽位的落点。与 `proof_verification` 模板的两段输入相比，多出的第三段 `rating` 就是本阶段的特有输入。

#### 4.2.4 代码实践

**实践目标**：用真实模板渲染一份完整元验证提示词，肉眼确认三段式结构与决策树的位置。

**操作步骤**：在 4.1.4 脚本末尾追加（示例代码）：

```python
print(math_templates["meta_verification"].format(
    statement="Prove that the sum of two even integers is even.",
    proof="Let a = 2m and b = 2n, then a + b = 2(m+n) ...",
    rating=result[0]["rating"]))
```

**需要观察的现象**：提示词中「嵌入的验证器规则」代码块、四个分析维度标题、决策树的 0/0.5/1 分支、末尾三段输入是否齐全；`\boxed{{...}}` 的双大括号在渲染后是否变成 `\boxed{...}`（u3-l1 讲过的 `str.format` 转义）。

**预期结果**：得到一份可直接发给元验证模型的完整提示词，总长约百余行。具体渲染输出待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：批语指出了解答的两个缺陷，其中一个是误判（解答该步骤其实正确）。按决策树，元验证质量分是多少？

> **答案**：\(0.5\)。「部分缺陷合理、部分不合理」分支明确给 0.5。

**练习 2**：批语未指出任何缺陷、通篇夸奖，但最终给了 \(\boxed{0}\)。元验证质量分是多少？

> **答案**：走「未指出缺陷 → 表达分析 / 分数分析」分支：分数与所指缺陷（无）不匹配，属分数分析不过关，给 \(0.5\)。注意按「正面不在范围」原则，夸错了地方本身不扣分，扣分的是分数与缺陷的失衡。

**练习 3**：为什么模板要强调「把错步骤说成对步骤不算表达错误」？

> **答案**：因为表达分析只管批语的表述质量（笔误、失实复述、逻辑衔接），而「漏报缺陷」属于缺陷分析维度的缺失。若算作表达错误，一个通篇夸奖却没发现错误的批语会被打 0.5，与第 77 行「未发现缺陷即视为错误分析合理」的规则冲突。

### 4.3 主循环接线：skip 开关、生成命令与下游消费

#### 4.3.1 概念说明

`prepare_meta_verification` 只产出 `input.jsonl`，真正发起元验证请求的是主循环里的第三处 `os.system`。本模块把调用点、参数与下游消费串起来，并指出一个重要事实：**当前 HEAD 的 run.sh 竞赛配置开启了 `--skip_meta_verification`，元验证在官方配置中并未启用**——它是代码支持、配置默认关闭的可选环节。

#### 4.3.2 核心流程

```text
if not skip_meta_verification:
    if input.jsonl 不存在（断点续跑检查）:
        prepare_meta_verification(验证输出 → 元验证输入)
    os.system 调 generate.py 生成元验证输出
下一轮 prepare_proof_refinement:
    读元验证输出 → rating2quality（以 rating 全文为键）
    读验证输出时逐条挂上 quality 信息
```

#### 4.3.3 源码精读

**参数与开关。**

[main.py:44-49](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L44-L49) —— `--skip_meta_verification` 是 `store_true` 开关；`--n_meta_verification_per_rating` 默认 1，即每条低分评价只复核一次（对照验证阶段的 `--n_verification_per_proof` 默认 4、run.sh 取 64，元验证的算力配额明显更克制）。[run.sh:18](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/run.sh#L18) 竞赛配置确实带着 `--skip_meta_verification`。

**调用点与断点续跑。**

[main.py:502-509](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L502-L509) —— 整个元验证块被 `if not args.skip_meta_verification` 包住；跳过时连 `meta_verification_R{R}/` 目录都不产生。内部的 `os.path.exists` 检查沿用 u4-l1 讲过的「文件存在即跳过准备阶段」续跑模式。

**生成命令。**

[main.py:511-523](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L511-L523) —— 第三处 `os.system`：硬编码 `generate.py`（不是 `args.infer_script`），使用元验证专属的进程数、温度与最大长度，采样数取 `n_meta_verification_per_rating`。结合 u2-l2 的计数公式：元验证输出条数 = 请求数 = 输入行数 × 1。

**下游消费：以评价全文为键的 join。**

[main.py:297-317](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L297-L317) —— `prepare_proof_refinement` 开头读元验证输出：对 `finish_reason` 与 `</think>` 做与 4.1 完全相同的检查、用同样的 `split("</think>")[-1].strip()` 切分、用同样的 boxed 解析取质量分，然后以 `item['rating']`（正是 `prepare_meta_verification` 写入并透传的那个字段）为键累积进 `rating2quality`。随后在 [main.py:347-351](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L347-L351) 读验证输出时，对每条评价做**同一套切分**得到 rating 文本，再 `rating2quality.get(rating, [])` 精确匹配挂载。两边切分逻辑必须字符级一致，join 才能对上——这就是 `rating` 字段被原样保留、且两处代码重复同一段切分表达式的原因。

值得指出的一个观察：在当前 HEAD 中，挂载好的 `quality` 信息进入了 `problem2proof2ratings` 的数据结构，但 `_prepare_proof_agg_tasks` 拼装精炼摘要时（u5 单元详读）只读取了 `rating['rating']` 文本与 `rating['score']` 分数，未再消费 `quality`。换言之，`rating2quality` 是「已建好但暂未被下游使用」的通路；`run.sh` 跳过元验证时它退化为空字典，全程无感兼容。

#### 4.3.4 代码实践

**实践目标**：通过源码推演（无需运行）对比「开启 / 跳过元验证」两种配置下一轮目录产出与请求量的差异。

**操作步骤**：

1. 读 [main.py:398-446](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L398-L446)，确认 R1 目录产出只在 R≥2 被消费。
2. 假设 1 道题、`n_parallel_proof_gen = 8`、`n_verification_per_proof = 4`、全部证明完整。推算：验证请求 8 × 4 = 32 条；若其中低分评价占一半，元验证输入约 16 条、请求也是 16 条。
3. 对照 `run.sh` 的 `--skip_meta_verification`：这 16 次请求全部省下，`meta_verification_R1/` 不存在，下一轮 `prepare_proof_refinement` 在 [main.py:298](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L298) 的 `os.path.exists` 检查处自然落空。

**需要观察的现象 / 预期结果**：写出两种配置下 `output_dirname` 的目录树差异清单（跳过时少了 `meta_verification_R{R}/` 两个文件）。数值推演待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`--skip_meta_verification` 开启时，流水线哪三个位置的行为会改变？

> **答案**：（1）主循环不再调用 `prepare_meta_verification`，不产出 `meta_verification_R{R}/` 目录；（2）不再执行第三处 `os.system`，省下全部元验证 API 请求；（3）下一轮 `prepare_proof_refinement` 中 `rating2quality` 保持空字典，验证输出里的评价照常聚合，只是不带质量信息。

**练习 2**：如果想让每条低分评价被复核 3 次取多数，改哪个参数？会有什么副作用？

> **答案**：把 `--n_meta_verification_per_rating` 设为 3。副作用是元验证请求量 ×3（u2-l2 的计数公式），且注意 `prepare_proof_refinement` 目前对同一 rating 的多条质量分只是全部 append 进列表，没有实现「取多数」的聚合逻辑——那部分需要自行二次开发。

## 5. 综合实践

把 u4-l2 与本讲串成一条「关卡漏斗」实验（纯本地、不调 API）：

1. 手造一个 mini 验证输出集：2 道题 × 每题 2 个证明 × 每证明 3 条评价，共 12 条，评分刻意混布——6 条 `\boxed{1}`、4 条 `\boxed{0.5}`、2 条 `\boxed{0}`，另外塞入 3 条边界样本：1 条 `finish_reason = "length"`、1 条 boxed 缺失、1 条违规输出 `\boxed{0.8}`。
2. 依次跑你在 u4-l2 与本讲写的两个脚本（或合并成一个），在每道关卡后打印剩余条数，形成漏斗报告。
3. 验证最终元验证输入条数符合公式：

\[ \#\text{元验证输入} \;=\; \#\{\, r \in \text{评价} :\ \text{score}(r) \le 0.75 \,\} \;=\; 4 + 2 + 0 \;=\; 6 \]

（0.5 档 4 条 + 0 档 2 条；截断、boxed 缺失样本在前三关已被丢弃，0.8 被第 4 关当高分拦下。）

4. 再随机抽 1 条输出记录，肉眼核对 `messages` 中三段输入与 `rating` 字段的对应关系，为 u5 单元的证明池学习准备好数据直觉。

## 6. 本讲小结

- `prepare_meta_verification`（[main.py:118-154](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L118-L154)）是一个四关卡漏斗：完整性闸门 → 切分思维链 → boxed 分数解析 → `score > 0.75` 低分过滤，全过才渲染成元验证请求。
- 在 0/0.5/1 三档评分契约下，`score > 0.75` 等价于「丢掉 1 分评价」，只让 0 与 0.5 档批语进入复核；写成区间判断是对模型违约输出中间值的防御。
- 元验证输入是 `statement`（题面）+ `proof`（u4-l2 切分出的纯 Solution）+ `rating`（切掉思维链的批语正文）三段拼装；`rating` 字段被原样保留，充当下游 `rating2quality` 的字符级 join 主键。
- 与 u4-l2 对比：本函数无自评解析、多分数过滤、容错统一为静默丢弃、`finish_reason` 直接弹出且不 `.lower()`（依赖 `APIModel` 已小写化的上游契约）。
- `meta_verification` 模板的核心是「评评价不评解答」：缺陷分析优先、正面肯定不在范围、评级决策树按「缺陷合理性 → 表达/分数一致性」两级给出 0/0.5/1。
- 官方 `run.sh` 开启 `--skip_meta_verification`，元验证在竞赛配置中未启用；且当前 HEAD 中挂载的 `quality` 信息未被精炼摘要消费，属「已建成、待启用」的通路。

## 7. 下一步学习建议

本讲完成后，主循环三个阶段的「输入准备」已全部读完，数据流到达最后一站：下一轮精炼输入的构建。建议进入 u5 单元：

1. 先读 [main.py:286-395](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L286-L395) 的 `prepare_proof_refinement`，重点看你已熟悉的 `rating2quality` 如何与验证输出的两级聚合（问题 → 证明 → 评价）汇合。
2. 再读 u5-l1（证明池持久化）与 u5-l2（多证明聚合与组合采样），那里会解释 `meanscore`、`proof_id`、`dep_proof_ids` 这些本讲已随行透传的字段的最终用途。
3. 带着一个问题去读：`use_old_proofs_for_refinement=True` 时，历史证明池如何改变下一轮的精炼候选集？
