# 四大提示词模板：math_templates.py 逐段精读

## 1. 本讲目标

学完本讲，你应该能够：

1. 逐段复述 `proof_verification` 模板的内容：0 / 0.5 / 1 三档评分标准、每档的适用条件，以及「引用论文结论必须自行证明」这条防钻空子约束。
2. 解释 `proof_generation` 模板为什么把验证器的评分标准原文嵌进去，并要求模型输出 `## Solution` 与 `## Self Evaluation` 两个小节、给自己打分。
3. 说出 `meta_verification` 模板的判定对象是「评价是否合理」而不是「解答是否正确」，并能手推它的 0 / 0.5 / 1 评级决策树。
4. 说明 `proof_refinement` 模板如何用 `{instruction}` 与 `{proofs_to_refine}` 两个占位符，把「完整生成提示词 + 候选证明 + 候选证明的评价」拼装成一个精炼提示词。
5. 独立用 `str.format` 渲染这四个模板，理解模板里 `{{` 双大括号转义的必要性。

## 2. 前置知识

阅读本讲前，你需要理解以下几个基础概念（不懂的话先看这里的解释）：

- **提示词（prompt）与 messages**：本流水线把每个模板渲染成一个字符串，作为 `messages` 列表里 `role=user` 的 `content` 发给模型（回顾 u1-l3、u2-l1）。模板就是「填空题的题面」，占位符是空，`.format(...)` 负责填空。
- **`str.format` 与大括号转义**：Python 的 `"...{name}...".format(name=1)` 会把 `{name}` 替换成 `1`。因此模板文本里**字面上的** `{` 必须写成 `{{`、`}` 写成 `}}`，否则 `.format` 会把它当成占位符去解析，抛出 `KeyError`。这一点在本文件里非常关键，因为模板要求模型输出 LaTeX 的 `\boxed{...}`。
- **`\boxed{...}` 约定**：LaTeX 里 `\boxed{x}` 表示把 x 框起来。模板约定「最终分数必须写在 `\boxed{}` 里」，下游解析器 `extract_boxed_answers` 就靠这个标记从自由文本里捞分数（下一讲 u3-l2 精读）。
- **Markdown 标题作为切分标记**：`## Solution`、`## Self Evaluation` 是 Markdown 二级标题。模板要求模型用「一字不差的标题」分隔两个小节，解析器 `extract_solution` / `extract_self_eval` 才能用正则可靠切分。提示词的格式要求和解析器的正则，是同一份契约的两面。
- **三角色分工（回顾 u1-l1）**：生成器写证明并自我评价；验证器给证明打 \(0 / 0.5 / 1\) 分；元验证器只复核「验证器的评价是否合理」。本讲的四个模板恰好一一对应：`proof_generation` 给生成器、`proof_verification` 给验证器、`meta_verification` 给元验证器、`proof_refinement` 把前三者的产物重新喂回生成器。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [inference/math_templates.py](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/math_templates.py) | 全部提示词模板，一个普通字典 `math_templates` | 四个模板逐段精读（本讲主体） |
| [inference/main.py](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py) | 流水线编排，负责 `.format(...)` 渲染模板 | 只看四个 `.format` 调用点，理解占位符从哪来 |
| [inference/utils.py](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/utils.py) | 输出解析工具 | 只看 `extract_solution` / `extract_self_eval` 依赖哪些标题，说明「格式契约」 |
| [inputs/CMO2024.json](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inputs/CMO2024.json) | CMO 2024 竞赛题输入 | 代码实践用它的第一题来渲染模板 |

`math_templates.py` 全文只有一个字典：键是模板名（`"proof_verification"`、`"meta_verification"`、`"proof_generation"`、`"proof_refinement"`），值是三引号字符串并在导入时就 `.strip()` 去掉首尾空白，见 [inference/math_templates.py:L1-L2](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/math_templates.py#L1-L2) 与结尾的 [inference/math_templates.py:L213-L214](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/math_templates.py#L213-L214)。

`main.py` 通过四个 argparse 参数选择模板键，默认值正好对应字典里的四个键，因此**模板是可配置替换的**（尽管字典里目前只有这四个）：

- [inference/main.py:L29](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L29)：`--proof_gen_template`，默认 `proof_generation`
- [inference/main.py:L30](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L30)：`--proof_refine_template`，默认 `proof_refinement`
- [inference/main.py:L40](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L40)：`--proof_verification_template`，默认 `proof_verification`
- [inference/main.py:L48](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L48)：`--meta_verification_template`，默认 `meta_verification`

## 4. 核心概念与源码讲解

### 4.1 proof_verification：验证器的评分契约

#### 4.1.1 概念说明

`proof_verification` 是发给**验证器**的提示词。生成器产出一批证明之后，流水线需要给每个证明打分，这个分数后面会用来：筛掉没写完的样本、聚合出每个证明的均分、决定哪些评价要送去元验证。所以这份模板本质上是整条流水线的「评分契约」——它定义了：

1. 任务形式：题目可能要求证明，也可能要求算答案；若要求答案，解答必须**同时给出答案和对答案成立性的严格证明**。
2. 三档评分标准（0 / 0.5 / 1）。
3. 一条防钻空子约束：引用论文结论不能免于证明。
4. 输出格式：先详细评价，再在 `\boxed{}` 里给最终分。
5. 两个占位符：`{statement}`（题面）和 `{proof}`（待评的证明）。

#### 4.1.2 核心流程

模板在本讲只做「渲染」这一步，它在流水线中的位置是：

1. `main.py` 的 `prepare_proof_verification` 读取证明生成阶段的输出 `output.jsonl`，过滤掉 `finish_reason != 'stop'` 的样本，并从 `</think>` 之后切出证明正文（[inference/main.py:L70-L78](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L70-L78)）。
2. 用 `extract_solution` / `extract_self_eval` 把证明切成「解答」和「自我评价」两部分（[inference/main.py:L85-L86](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L85-L86)）。
3. 调用本模板的 `.format(statement=..., proof=...)` 渲染成验证请求（详见 4.1.3）。
4. 渲染结果写入 `proof_verification_R{R}/input.jsonl` 的 `messages` 字段，由 `generate.py` 发给验证模型（u2-l1）。
5. 验证模型的回复里带 `\boxed{分数}`，下游用 `extract_boxed_answers` 解析（u3-l2、u4-l2 展开）。

三档标准可以形式化成一个分段函数，设证明为 \( p \)、题为 \( s \)：

\[
\mathrm{score}(p) =
\begin{cases}
1 & p \text{ 完全正确，所有步骤执行得当且展示清楚} \\
0.5 & p \text{ 大体正确，但有细节省略或轻微错误} \\
0 & p \text{ 没有真正解决题目，或含致命错误，或严重省略}
\end{cases}
\]

#### 4.1.3 源码精读

**评分标准与引用约束**（[inference/math_templates.py:L7-L11](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/math_templates.py#L7-L11)）：这三行 bullet 加第四条「Additionally」就是三档标准。注意第四条：引用任何论文里的结论都**不能免除证明义务**，当且仅当解答同时给出该引用结论的有效证明时才允许引用，否则按上面的标准打分且「绝对不给 1 分」。这条是针对竞赛题场景的防作弊条款——竞赛证明常可以「引用某个已知引理一步秒杀」，该条款堵住了这条捷径，保证 1 分意味着自包含的完整证明。

**输出格式约定**（[inference/math_templates.py:L13-L19](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/math_templates.py#L13-L19)）：

```text
Here is my evaluation of the solution:
... // Your evaluation here. You are required to present in detail ...

Based on my evaluation, the final overal score should be:
\boxed{{...}} // where ... should be the final overall score (0, 0.5, or 1, and nothing else)
```

三个值得注意的细节：

- 源码里写的是 `\\boxed{{...}}`。该字符串不是 raw 字符串，字面量解析后模板里是 `\boxed{{...}}`；再经 `.format(...)` 把 `{{` 变成 `{`，最终发给模型的是 `\boxed{...}`。**如果你抄模板时漏写双大括号，`.format` 会直接抛 `KeyError`**。
- 源码里 "overal" 是拼写错误（少了一个 l）。它不影响功能，因为下游只解析 `\boxed{}` 里的内容——这也提醒我们：格式契约的核心是 `\boxed{}`，不是这句话本身。
- 评价部分被要求「逐个分析关键步骤或你曾怀疑的步骤」：对正确的步骤要解释为什么怀疑、为什么最终正确；对错误的步骤要解释错因和影响。这为元验证阶段的「缺陷分析」提供了可核对的素材（见 4.3）。

**两个占位符**（[inference/math_templates.py:L23-L29](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/math_templates.py#L23-L29)）：模板末尾是任务输入区，`## Problem` 下填 `{statement}`，`## Solution` 下填 `{proof}`。

**唯一的调用点**（[inference/main.py:L99-L102](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L99-L102)）：

```python
question = math_templates[args.proof_verification_template].format(
    statement=statement.strip(),
    proof=proof.strip()
)
```

`statement` 来自原始题目的 `question` 字段（[inference/main.py:L72](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L72)），`proof` 是 4.1.2 第 2 步切出的证明正文。注意：**填入的值里含大量 LaTeX 大括号也没关系**——`str.format` 只扫描模板本身的大括号，不会二次处理替换进来的值。

#### 4.1.4 代码实践

实践目标：亲手渲染一次验证提示词，并观察 `{{` → `{` 的转义效果。

操作步骤（示例代码，非项目原有代码）：

1. 在项目根目录新建 `render_verify.py`，内容如下：

   ```python
   import sys
   sys.path.insert(0, "inference")   # math_templates.py 在 inference/ 下
   from math_templates import math_templates

   tpl = math_templates["proof_verification"]
   # 检查渲染前的模板文本：双大括号还在
   print(r"渲染前包含 \boxed{{ ：", "\\boxed{{" in tpl)

   prompt = tpl.format(
       statement="Prove that the sum of two even integers is even.",  # 随便一道小题
       proof="Let a=2m and b=2n. Then a+b=2(m+n), which is even. QED.",
   )
   print("渲染后包含 \\boxed{ ：", "\\boxed{" in prompt)
   print("渲染后包含 {{ ：", "{{" in prompt)
   print(prompt)  # 完整打印
   ```

2. 运行 `python3 render_verify.py`。

需要观察的现象：

- 「渲染前」检查为 `True`，「渲染后」`\\boxed{` 为 `True` 而 `{{` 为 `False`——即 `.format` 把 `{{`、`}}` 各折叠成一个字符。
- 完整打印的提示词末尾，`## Problem` 和 `## Solution` 两段已被填入你给的内容。

预期结果：三行检查分别输出 `True / True / False`，提示词末尾出现 `\boxed{...}`（单大括号）。

再做一个小实验：把渲染**后的**字符串再 `.format()` 一次（`prompt.format()`），观察异常。预期抛 `KeyError`，因为渲染结果里的 `\boxed{...}` 中 `{...}` 会被当成占位符解析；确切的异常消息待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：一个解答「结论正确、论证完整，但引用了某论文的定理 3 而没有给出该定理的证明」，按此模板应打多少分？

答案：按第四条约束，引用论文结论不豁免证明义务，该解答「绝对不能得 1」；若其余部分完全正确，属于「细节省略」，一般落在 0.5 档；若被引用的结论正是解题关键，也可能被评 0。模板把裁量权交给了验证器的分析，但封死了 1 分。

**练习 2**：为什么模板不直接让验证器输出一个数字，而要求先写详细评价再给 `\boxed{}` 分数？

答案：两个原因。（1）详细评价是元验证阶段的输入素材——元验证器要逐条核对「评价指出的缺陷是否真实存在」，没有评价文本就没有可核对的对象（见 4.3）。（2）`\boxed{}` 是机器可解析的稳定锚点，`extract_boxed_answers` 靠它从自由文本里取分，比解析自然语言可靠。

**练习 3**：如果把模板第 19 行的 `\\boxed{{...}}` 改成 `\boxed{...}`（单大括号），会发生什么？

答案：`.format` 会把 `{...}` 当成名为 `...` 的占位符，渲染时抛 `KeyError: '...'`。这就是模板里所有字面大括号都必须双写的原因。

### 4.2 proof_generation：让生成器拿着评分标准写证明

#### 4.2.1 概念说明

`proof_generation` 是发给**生成器**的提示词，也是整条流水线的第一环。它最特别的设计是：**把验证器的评分标准原文嵌入到生成提示词里**（装在一个 ```txt 代码围栏中），并明确告诉模型「你已经有能力给自己评分」。也就是说，生成器在动笔之前就看到了自己将来会被怎样评判。

它要求最终回复必须分成两个小节：

- `## Solution`：最终解答（要求按评分标准尽量打磨）。
- `## Self Evaluation`：对上面解答的自我评价，以固定短语开头，以 `\boxed{自评分}` 结尾。

为什么要求「自打分」？因为 DeepSeekMath-V2 的核心主张就是「用验证器当奖励模型训练生成器」，训练出来的生成器天然带着自我评价能力；推理时让模型显式输出自评分，这个分数（`self_eval_score`）在后续精炼阶段会作为证明排序的次级键使用（[inference/main.py:L227](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L227) 的排序键里有 `self_eval_score`）。

而两个小节的标题之所以强调 "this exact same markdown title"（一字不差的标题），是因为解析器就靠这两个标题切分输出——这是提示词与 `utils.py` 解析器之间的格式契约。

#### 4.2.2 核心流程

1. 第 1 轮开始时，`main.py` 读入原始题目文件，对每道题渲染本模板（唯一占位符 `{question}`），生成 `messages` 写入 `proof_gen_R1/input.jsonl`（[inference/main.py:L416-L421](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L416-L421)）。
2. 生成器（经 `generate.py`）返回 `reasoning_content` + `content` 拼接的输出（u2-l1），`</think>` 之后是正式回复。
3. 流水线检查 `finish_reason == 'stop'` 且含 `</think>'`（[inference/main.py:L74-L75](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L74-L75)）。
4. `extract_self_eval` / `extract_solution` 按 `## Self Evaluation`、`## Solution` 标题切出两段；自评分取自我评价小节里最后一个 `\boxed` 值（[inference/main.py:L85-L88](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L85-L88)）。
5. 切出的 `proof` 进入 4.1 的验证模板。

模板提示词的「内循环」要求可以概括成伪代码：

```text
loop:
    解题（形成当前最佳解答）
    按嵌入的评分标准自查
    if 找到问题 and 还能修复: 修复后继续
    else: 跳出
输出: ## Solution（当前最佳解答） + ## Self Evaluation（如实评价 + \boxed{分}）
```

#### 4.2.3 源码精读

**嵌入评分标准**（[inference/math_templates.py:L145-L156](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/math_templates.py#L145-L156)）：

````text
Your final solution to the problem should be exceptionally comprehensive and easy-to-follow, which will be rated according to the following evaluation instruction:

```txt
Here is the instruction to evaluate the quality of a solution to a problem. ...
Please evaluate the solution and score it according to the following criteria:
- If the solution is completely correct, with all steps executed properly and clearly demonstrated, then the score is 1
- If the solution is generally correct, but with some details omitted or minor errors, then the score is 0.5
- If the solution does not actually address the required problem, contains fatal errors, or has severe omissions, then the score is 0

Additionally, referencing anything from any paper does not save the need to prove the reference. ...
```
````

围栏内的三档 bullet 和「引用论文」条款，与 `proof_verification` 的 [L8-L11](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/math_templates.py#L8-L11) **逐字相同**（仅围栏前多一句引导语）。生成器与验证器共享同一份评分语言，这正是「自验证」能在语义上对齐的前提。

**自我修复与诚实条款**（[inference/math_templates.py:L158-L165](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/math_templates.py#L158-L165)）：要求模型「仔细推理如何解题、按指令评价自己的方法、修复发现的问题，直到无法再取得进展」；并反复强调「如实呈现进度」——只有复查后确实找不到问题才允许自评 1 分；发现问题但修不掉也没关系，如实写出来；最糟糕的回复是「解错了却谎称正确」。最后一句 "You CAN'T cheat! If you cheat, we will know, and you will be penalized!" 带有明显的 RLVR 训练语气，推理阶段保留它是为了延续训练与推理提示词的一致性。

**输出格式契约**（[inference/math_templates.py:L167-L178](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/math_templates.py#L167-L178)）：规定 `## Solution` 与 `## Self Evaluation` 两个「一字不差」的标题、自我评价以 "Here is my evaluation of the solution:" 固定短语开头、以 `\\boxed{{...}}`（渲染后 `\boxed{...}`）收尾，且分数只能取 0、0.5、1。这段格式要求与 `utils.py` 的解析器严丝合缝：

- [inference/utils.py:L36-L42](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/utils.py#L36-L42) 的 `_normalize_prover_output` 先把 `* Solution *` 这类星号写法的标题归一成 `## Solution`（模型偶尔不守格式时的容错）。
- [inference/utils.py:L44-L46](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/utils.py#L44-L46) 的 `extract_solution` 按 `\n## Self Evaluation\n` 先切掉自我评价，再取 `## Solution` 之后的部分。
- [inference/utils.py:L48-L50](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/utils.py#L48-L50) 的 `extract_self_eval` 取 `## Self Evaluation` 之后的部分。

**占位符与调用点**（[inference/math_templates.py:L182-L185](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/math_templates.py#L182-L185)；[inference/main.py:L418](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L418)）：

```python
question = math_templates[args.proof_gen_template].format(question=item['question'].strip())
```

唯一的 `{question}` 填的是 `inputs/*.json` 里的 `question` 字段，例如 [inputs/CMO2024.json:L4](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inputs/CMO2024.json#L4) 的 CMO2024 第 1 题（一道关于无理数 \(\alpha\) 与序列 \(\{x_n\}\) 最终周期性的证明题）。题面里满是 LaTeX 大括号，但如 4.1.3 所述，替换值不参与格式解析，安全。

#### 4.2.4 代码实践

实践目标：用真实竞赛题渲染生成提示词，并验证「三份模板共享同一套评分标准文本」。

操作步骤（示例代码，非项目原有代码）：

1. 新建 `render_gen.py`：

   ```python
   import sys, json
   sys.path.insert(0, "inference")
   from math_templates import math_templates

   item = json.load(open("inputs/CMO2024.json"))[0]   # CMO2024-1
   gen_prompt = math_templates["proof_generation"].format(question=item["question"].strip())
   ver_prompt = math_templates["proof_verification"].format(
       statement=item["question"].strip(), proof="(placeholder proof)")
   meta_prompt = math_templates["meta_verification"].format(
       statement=item["question"].strip(), proof="(placeholder proof)",
       rating="(placeholder rating)")

   bullet = ("- If the solution is completely correct, with all steps executed properly "
             "and clearly demonstrated, then the score is 1")
   print("生成模板含该 bullet: ", bullet in gen_prompt)
   print("验证模板含该 bullet: ", bullet in ver_prompt)
   print("元验证模板含该 bullet:", bullet in meta_prompt)
   print("--- 完整生成提示词 ---")
   print(gen_prompt)
   ```

2. 运行 `python3 render_gen.py`。

需要观察的现象：三行检查是否都为 `True`；完整提示词末尾 `## Problem` 下是否是 CMO2024-1 的题面（含 `\alpha`、`\begin{cases}` 等 LaTeX）；提示词中部是否出现 ```txt 围栏包裹的评分标准。

预期结果：三个 `True`；题面完整、LaTeX 原样保留。这证明生成器、验证器、元验证器三份提示词引用的是同一套评分语言。

#### 4.2.5 小练习与答案

**练习 1**：模板为什么要规定自我评价以固定短语 "Here is my evaluation of the solution:" 开头？

答案：主要是约束输出风格、降低模型「跳过评价只给分」的概率，同时与 `proof_verification` 的输出格式保持同构（两边评价小节句式一致，便于后续把自评与验证器评价放进同一套解析与对比流程）。真正参与机器解析的是 `## Self Evaluation` 标题和 `\boxed{}`，固定短语是辅助锚点。

**练习 2**：如果模型输出的标题写成 `**Solution**`（加粗）而不是 `## Solution`，流水线还能解析吗？

答案：不能直接解析，但 `_normalize_prover_output` 只归一「星号包裹」的写法（`* Solution *` / `**Solution**` 属于 `\*+` 匹配范围），归一成 `## Solution` 后即可解析；若模型用了第三种写法（例如 `### Solution`），则会解析失败，在 [inference/main.py:L91-L92](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L91-L92) 被 `except: continue` 直接丢弃。

**练习 3**：`self_eval_score` 在后续流程里有什么用？

答案：在 `_prepare_proof_agg_tasks` 里对证明池按 `(meanscore, self_eval_score)` 双键降序排序（[inference/main.py:L227](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L227)）：验证均分相同时，自评分高的证明优先被选入精炼候选。这是 u5-l2 的内容，这里只需知道「自评分不是摆设」。

### 4.3 meta_verification：评价「评价者」

#### 4.3.1 概念说明

`meta_verification` 是发给**元验证器**的提示词。它拿到三样东西：题面（`{statement}`）、证明（`{proof}`）、以及验证器对这个证明的完整评价（`{rating}`），然后回答一个问题：**这份评价本身合理吗？**

最关键的一点：模板第一条规则就声明「你的任务是分析 solution evaluation，不需要解题，也不需要严格判断解答是否正确」。元验证器评判的对象是**评价的质量**，不是**解答的质量**。打个比方：验证器是阅卷老师，元验证器是抽查阅卷质量的督导——督导不需要会做这道题，只需要判断老师的批语有没有道理。

为什么需要这一环？因为验证器自身会犯错（生成-验证差距，回顾 u1-l1）。当验证器给出低分（\( \le 0.75 \)，即 0 或 0.5 档）时，这个「差评」可能是冤枉的；元验证就是给这些差评一次申诉复核，复核后的质量分（0 / 0.5 / 1）在后续聚合时用来加权评价的可信度（u5-l3 的 `rating2quality`）。

#### 4.3.2 核心流程

模板给元验证器规定的分析流程：

1. 先阅读题面、证明、评价三段输入。
2. 从四个维度分析评价（见 4.3.3 的「四个分析维度」）。
3. 走评级决策树（下面的伪代码）。
4. 按格式输出：分析正文 + `\boxed{0 / 0.5 / 1}` 的评价质量分。

评级决策树（对应模板规则 5）：

```text
if 评价指出的缺陷中至少有一个不合理:
    # 只做缺陷分析
    if 指出的缺陷全部不合理:   评级 = 0
    else (部分合理部分不合理):  评级 = 0.5
else:  # 未指出缺陷，或缺陷全部合理
    if 存在表达错误 or 打分不符合评分规则:  评级 = 0.5
    else:                                 评级 = 1
```

用数学语言写：设评价指出的缺陷集合为 \( D \)，其中合理的子集为 \( D_{valid} \)，则

\[
\mathrm{rating} =
\begin{cases}
0 & D \neq \varnothing \;\wedge\; D_{valid} = \varnothing \\
0.5 & D \neq \varnothing \;\wedge\; \varnothing \neq D_{valid} \neq D \\
0.5 \text{ 或 } 1 & D = \varnothing \;\vee\; D_{valid} = D \text{（再查表达与打分）}
\end{cases}
\]

#### 4.3.3 源码精读

**任务定义与「规则转述」**（[inference/math_templates.py:L47-L58](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/math_templates.py#L47-L58)）：模板先声明任务，然后把验证器当初打分所依据的规则原文装进 ``` 围栏转述一遍，并特意注明 "(these are not your rules)"——这份围栏是背景材料，告诉元验证器「评价者当时被要求遵守什么」，而不是元验证器自己要执行的规则。注意围栏里没有 `\\boxed` 这类大括号内容，规则文本用 `\( \)` 书写行内公式（如 [L98](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/math_templates.py#L98) 的 \(0\)），因此不与 `.format` 冲突。

**四个分析维度**（[inference/math_templates.py:L64-L72](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/math_templates.py#L64-L72)）：

| 维度 | 检查什么 |
| --- | --- |
| Step Restatement（步骤复述） | 评价里复述的解答行为，解答原文里是否真的发生了 |
| Defect Analysis（缺陷分析） | 评价指出的错误/缺陷是否确实成立 |
| Expression Analysis（表达分析） | 评价的表述是否准确 |
| Score Analysis（打分分析） | 评价给的最终分与其找到的缺陷是否匹配 |

**只查「差评」的范围限定**（[inference/math_templates.py:L74-L77](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/math_templates.py#L74-L77)）：这是全模板最重要的一条——评价中对解答的**正面肯定不在评审范围内**；如果评价认为解答完全正确、没发现任何缺陷，那么无论解答实际上错得多明显，都应认定其「缺陷分析」是合理的。这把元验证的火力集中在对流水线危害最大的错误类型上：**冤枉好证明的假差评**（会把好证明从精炼候选里挤掉），而「漏抓错误」的假好评留给下一轮验证去自然淘汰。

**缺陷分析的双重检查**（[inference/math_templates.py:L79-L84](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/math_templates.py#L79-L84)）：对每条被指出的缺陷要同时回答两问——缺陷是否真实存在；评价对该缺陷的分析是否准确。

**表达错误的实例清单**（[inference/math_templates.py:L86-L92](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/math_templates.py#L86-L92)）：包括「把错误步骤之后的结论说成是错的」（正确说法只能是「未获严格证明」）、评价自己的笔误与算错、对解答内容的失实复述；并且明确「把错误的步骤说成正确」不算表达错误（那属于缺陷分析的漏报，而漏报不在范围内，呼应 L74-L77）。

**评级规则与输出格式**（[inference/math_templates.py:L94-L111](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/math_templates.py#L94-L111)）：即 4.3.2 的决策树原文，输出同样以 `\\boxed{{...}}` 收尾（渲染后 `\boxed{...}`），分数限定 0、0.5、1。

**三个占位符与调用点**（[inference/math_templates.py:L115-L124](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/math_templates.py#L115-L124)）：`## Problem` / `## Solution` / `## Solution Evaluation` 三段分别填 `{statement}`、`{proof}`、`{rating}`。渲染发生在 `prepare_meta_verification`（[inference/main.py:L135-L139](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L135-L139)）：

```python
inp = math_templates[args.meta_verification_template].format(
    statement=problem.strip(),
    proof=item['proof'].strip(),
    rating=rating.strip()
)
```

注意 `{rating}` 填的是**验证器输出的完整评价文本**（批语加分数），不只是数字。上游还有一个 `score > 0.75: continue` 的过滤（[inference/main.py:L133-L134](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L133-L134)）——满分好评不送元验证，只复核差评，这一步属于 u4-l3 的内容，这里知道即可。

#### 4.3.4 代码实践

实践目标：渲染元验证提示词，并用手写的假评价走一遍评级决策树。

操作步骤（示例代码，非项目原有代码）：

1. 新建 `render_meta.py`：

   ```python
   import sys, json
   sys.path.insert(0, "inference")
   from math_templates import math_templates

   item = json.load(open("inputs/CMO2024.json"))[0]
   fake_proof = "## Solution\nLet a = 2m, b = 2n, then a + b = 2(m+n). QED."

   rating_A = (  # 指出一个并不存在的缺陷
       "Here is my evaluation of the solution:\n"
       "The solution claims a+b=2(m+n), but this algebraic expansion is wrong.\n\n"
       "Based on my evaluation, the final overal score should be:\n\\boxed{0}")

   rating_B = (  # 没指出任何缺陷，但打了个规则之外的分数
       "Here is my evaluation of the solution:\n"
       "All steps are correct and clearly demonstrated.\n\n"
       "Based on my evaluation, the final overal score should be:\n\\boxed{0.9}")

   tpl = math_templates["meta_verification"]
   for name, rating in [("A", rating_A), ("B", rating_B)]:
       prompt = tpl.format(statement=item["question"].strip(),
                           proof=fake_proof, rating=rating)
       print(f"===== rating {name} 已渲染，长度 {len(prompt)} =====")
       print(prompt[-400:])   # 只看结尾的任务输入区
   ```

2. 运行 `python3 render_meta.py`，确认两份提示词末尾都正确填入题面、证明、评价三段。
3. 手动套用 4.3.2 的决策树给 A、B 各推一个「完美元验证器应有的评级」。

需要观察的现象与预期结果：

- rating A：唯一被指出的缺陷（展开错误）并不存在 → 「全部缺陷不合理」→ 理想评级 \(0\)。
- rating B：没有指出任何缺陷 → 进入表达/打分检查；`0.9` 不在 \(\{0, 0.5, 1\}\) 内，属于打分分析不合格 → 理想评级 \(0.5\)。
- 真实模型是否会按此评级输出：待本地验证（需要接入 API，属于 u6 的实践范围）。

#### 4.3.5 小练习与答案

**练习 1**：验证器的评价说「解答第 3 步的引理引用没有给出证明，扣 0.5」，而解答第 3 步其实完整证明了该引理。元验证器应评多少？

答案：这条缺陷「并不存在」（复述与事实不符，缺陷分析不成立）。若这是评价里唯一的缺陷，则「全部缺陷不合理」→ 评级 \(0\)。

**练习 2**：验证器把一个实际错误的步骤夸成正确，并给了满分 1。元验证器应该扣它的分吗？

答案：不应该因为「漏抓错误」而扣分。按 [L74-L77](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/math_templates.py#L74-L77) 的范围限定，正面肯定不在评审范围；若评价无表达错误，评级应为 \(1\)。这看似矛盾，实则是设计取舍：元验证只服务于「差评复核」这一个下游用途（低分才送元验证），假好评不会进入这条通路。

**练习 3**：为什么 `{rating}` 传的是验证器的完整输出文本，而不是只传一个分数？

答案：因为元验证的四个维度（复述、缺陷、表达、打分）全部建立在评价的**文字内容**之上；只传分数的话，缺陷分析、表达分析都无从谈起，元验证就退化成了让模型重新打一遍分。

### 4.4 proof_refinement：组合式提示词

#### 4.4.1 概念说明

`proof_refinement` 是第二轮及以后发给生成器的**精炼**提示词。它是四个模板里最短的一个，因为它不自己写规则，而是**组合**：

- `{instruction}`：填一整份**渲染完毕的 `proof_generation` 提示词**（含题面和评分标准）。
- `{proofs_to_refine}`：填若干「候选证明 + 各自收到的评价」组成的摘要文本。

也就是「精炼 = 原始任务说明书 + 历史尝试及其反馈」。模板用一句指令告诉模型怎么用这些材料：修复评价中指出的问题、复用候选证明里有希望的思路，或者两者兼顾。这在提示词层面实现了「从失败尝试中学习」——正是自我验证闭环里把验证结果回流给生成器的那条边。

#### 4.4.2 核心流程

1. 一轮结束后，`prepare_proof_refinement` 从元验证输出建立「评价 → 质量分」映射，从验证输出按题目聚合每个证明的均分与评价（u5-l3）。
2. `_prepare_proof_agg_tasks` 为每道题从证明池挑出排名前 `n_best_proofs_to_sample` 的证明，枚举证明组合，并把每个组合的证明与抽样评价拼成摘要（[inference/main.py:L242-L264](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L242-L264)）。
3. 摘要格式由代码硬编码：

   ```text
   --- Solution 0 ---
   {证明正文}

   === Evaluation 0 of Solution 0 ===
   {评价正文}

   === Evaluation 1 of Solution 0 ===
   {评价正文}


   --- Solution 1 ---
   {证明正文}
   ...
   ```

   即每个证明一个 `--- Solution N ---` 块，块内先放证明、后放若干条 `=== Evaluation M of Solution N ===` 评价，评价与证明之间用空行分隔（[inference/main.py:L259-L263](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L259-L263)）。

4. 用本模板 `.format(instruction=..., proofs_to_refine=...)` 渲染，写入下一轮 `proof_gen_R{R+1}/input.jsonl`。

#### 4.4.3 源码精读

**模板全文**（[inference/math_templates.py:L203-L213](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/math_templates.py#L203-L213)）：

```text
{instruction}

## Candidate Solution(s) to Refine
Here are some solution sample(s) along with their correctness evaluation(s). You should provide a better solution by solving issues mentioned in the evaluation(s), or by re-using promising ideas mentioned in the solution sample(s), or by doing both.

{proofs_to_refine}

## Final Instruction
Your final response should follow the format above, including a `## Solution` section followed by a `## Self Evaluation` section
```

三个角色段落：开头的 `{instruction}` 是完整任务书；中间在 `## Candidate Solution(s) to Refine` 标题下给出候选材料与使用方式；结尾 `## Final Instruction` 再次强调输出格式必须是 `## Solution` + `## Self Evaluation`——因为候选材料很长，模型读到末尾容易忘记任务书里定义的格式，这句提醒把格式要求「就近」重申一遍。

**嵌套渲染的调用点**（[inference/main.py:L265-L273](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L265-L273)）：

```python
msg = [
    {
        'role': 'user',
        'content': math_templates[args.proof_refine_template].format(
            instruction=math_templates[args.proof_gen_template].format(question=problem.strip()).strip(),
            proofs_to_refine=summary.strip()
        )
    }
]
```

注意 `instruction` 的值是**内层 `.format` 的渲染结果**。这在 Python 里是安全的：`str.format` 只扫描模板字符串本身的大括号，替换进来的值（哪怕满是 LaTeX 大括号）不会被再次解析。反过来，如果把渲染结果再单独 `.format()` 一次（像 4.1.4 的那个小实验），就会因为文本里的 `\boxed{...}`、`\frac{...}` 抛 `KeyError`——**对渲染结果绝不能再 format**。

**模板顺序的一个细节**：`math_templates` 字典里键的书写顺序是 verification、meta_verification、generation、refinement（[L2](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/math_templates.py#L2)、[L46](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/math_templates.py#L46)、[L142](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/math_templates.py#L142)、[L203](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/math_templates.py#L203)），大致对应流水线一轮内的执行顺序：生成 → 验证 → 元验证 →（用结果）精炼。字典顺序本身不影响逻辑（按键取值），但按这个顺序阅读源码正好顺着数据流走。

#### 4.4.4 代码实践

实践目标：手工构造一份只含「一个证明 + 一条评价」的 `proofs_to_refine`，渲染精炼提示词，并验证它逐字内嵌了完整的生成提示词。

操作步骤（示例代码，非项目原有代码）：

1. 新建 `render_refine.py`：

   ```python
   import sys, json
   sys.path.insert(0, "inference")
   from math_templates import math_templates

   item = json.load(open("inputs/CMO2024.json"))[0]
   gen_prompt = math_templates["proof_generation"].format(
       question=item["question"].strip())

   fake_proof = "Let a = 2m, b = 2n, then a + b = 2(m+n). QED."
   fake_rating = ("Here is my evaluation of the solution:\n"
                  "The proof only handles integers, but the problem is "
                  "about a general irrational alpha; it does not address "
                  "the required problem.\n\n"
                  "Based on my evaluation, the final overal score should be:\n\\boxed{0}")

   summary = (f"--- Solution 0 ---\n{fake_proof}\n\n"
              f"=== Evaluation 0 of Solution 0 ===\n{fake_rating}")

   refine_prompt = math_templates["proof_refinement"].format(
       instruction=gen_prompt.strip(), proofs_to_refine=summary.strip())

   print("精炼提示词以内嵌生成提示词开头:",
         refine_prompt.startswith(gen_prompt.strip()))
   print("含候选证明:", fake_proof in refine_prompt)
   print("含评价:", "\\boxed{0}" in refine_prompt)
   print("总长度:", len(refine_prompt), "字符;  生成提示词长度:", len(gen_prompt))
   ```

2. 运行 `python3 render_refine.py`。

需要观察的现象与预期结果：三个检查均为 `True`；精炼提示词比生成提示词长出一截（多出的就是候选证明、评价和两段标题/指令）。打开打印的全文，确认结构是「完整生成提示词 → `## Candidate Solution(s) to Refine` → 摘要 → `## Final Instruction`」。

#### 4.4.5 小练习与答案

**练习 1**：`proof_refinement` 模板自身没有定义输出格式，那模型怎么知道要输出 `## Solution` 和 `## Self Evaluation`？

答案：格式定义在内嵌的 `{instruction}`（即完整的 `proof_generation` 提示词）里；此外模板结尾的 `## Final Instruction` 段落又显式重申了一遍「follow the format above, including a `## Solution` section followed by a `## Self Evaluation` section」，双保险。

**练习 2**：为什么不把 `instruction` 设计成一个占位符传「题面」，而是在调用点就把整份生成提示词渲染好再塞进来？

答案：这样精炼提示词与首轮生成提示词保持**逐字一致**（同一份任务书），模型在第一轮和后续轮次看到完全相同的指令语境，只有候选材料部分在增长；同时也让 `proof_refinement` 模板保持极简，不需要复制维护一份与 `proof_generation` 同步的规则文本。代价是提示词变长（任务书整段重复），这是用 token 换一致性的取舍。

**练习 3**：`--- Solution N ---` 和 `=== Evaluation M of Solution N ===` 这两种分隔标记是模板定义的吗？

答案：不是。模板只提供 `{proofs_to_refine}` 这个占位符；这两种标记是 `main.py` 在拼摘要时硬编码的（[inference/main.py:L259-L263](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L259-L263)）。它们只给模型看，不参与下游机器解析（解析只认 `## Solution` / `## Self Evaluation` / `\boxed{}`）。

## 5. 综合实践

把四个模板串起来写一个完整的 `render_templates.py`（示例代码，非项目原有代码），目标是产出三份相互衔接的真实提示词，并自查它们的衔接关系：

1. **实践目标**：用 `inputs/CMO2024.json` 第一题（`problem_idx` 为 `CMO2024-1`）渲染生成、验证、精炼三份提示词（元验证提示词已在 4.3.4 渲染过，可一并纳入），检查「同一道题如何流经三份提示词」。

2. **操作步骤**：

   ```python
   # render_templates.py —— 放在项目根目录运行: python3 render_templates.py
   import sys, json
   sys.path.insert(0, "inference")
   from math_templates import math_templates

   item = json.load(open("inputs/CMO2024.json"))[0]
   question = item["question"].strip()
   print(f"题目: {item['problem_idx']}\n")

   # 1) 生成提示词（第一轮 proof_gen_R1/input.jsonl 的内容）
   gen_prompt = math_templates["proof_generation"].format(question=question)

   # 2) 验证提示词（假设生成器交回了这份假证明）
   fake_proof = ("## Solution\nBy the boxed criterion we directly have the claim. "
                 "QED.\n\n## Self Evaluation\nHere is my evaluation of the solution:\n"
                 "I did not verify the criterion.\n\n"
                 "Based on my evaluation, the final overal score should be:\n\\boxed{0.5}")
   ver_prompt = math_templates["proof_verification"].format(
       statement=question, proof=fake_proof.split("## Self Evaluation")[0]
       .replace("## Solution", "").strip())

   # 3) 精炼提示词（instruction = 完整生成提示词; 摘要 = 证明 + 一条差评）
   summary = (f"--- Solution 0 ---\n{ver_prompt.split('## Solution')[-1].strip()}\n\n"
              f"=== Evaluation 0 of Solution 0 ===\n"
              "This is a placeholder verifier evaluation with \\boxed{0}.")
   refine_prompt = math_templates["proof_refinement"].format(
       instruction=gen_prompt.strip(), proofs_to_refine=summary)

   for name, p in [("生成", gen_prompt), ("验证", ver_prompt), ("精炼", refine_prompt)]:
       print(f"===== {name}提示词 ({len(p)} 字符) =====\n{p}\n")

   # 4) 衔接关系自查
   checks = {
     "① 验证的题面 == 生成的题面 (同一段 question)":
        question in ver_prompt and question in gen_prompt,
     "② 精炼以内嵌方式复用完整生成提示词":
        refine_prompt.startswith(gen_prompt.strip()),
     "③ 三份提示词共享同一条 1 分标准 bullet":
        ("then the score is 1" in gen_prompt
         and "then the score is 1" in ver_prompt
         and "then the score is 1" in refine_prompt),
     "④ 生成与验证提示词都含 \\boxed{ 约定（精炼内嵌生成，自然继承）":
        ("\\boxed{" in gen_prompt and "\\boxed{" in ver_prompt
         and "\\boxed{" in refine_prompt),
   }
   for k, v in checks.items():
       print(("PASS " if v else "FAIL ") + k)
   ```

3. **需要观察的现象**：三份提示词的长度递增关系（生成 < 验证 < 精炼，因为精炼完整包含生成提示词再加候选材料）；④ 里精炼提示词的 `\boxed{` 来自内嵌的生成提示词。

4. **预期结果**：四个检查全部 `PASS`。如果 ② 失败，最常见原因是给 `instruction` 传值前忘了 `.strip()`（调用点 [inference/main.py:L269](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L269) 是带 `.strip()` 的）。

5. 以上全部为纯本地字符串操作，不需要 API Key；「真实模型在这三份提示词下的行为」待本地验证（接入方式见 u6-l2）。

## 6. 本讲小结

- `math_templates.py` 是一个只有四个键的字典，但它是整条「生成—验证—元验证—精炼」闭环的语义中心：每个角色一份提示词，`main.py` 用四个 argparse 参数按键选用。
- `proof_verification` 确立评分契约：三档标准 \(0 / 0.5 / 1\) + 「引用论文结论必须自行证明，否则绝不给 1」+ `\boxed{}` 收分格式；`{{` 双大括号是为了让 `.format` 与 LaTeX 共存。
- `proof_generation` 把验证器的评分标准**逐字**嵌给生成器，要求输出 `## Solution` + `## Self Evaluation` 两个固定标题小节并自打分；这些标题同时是 `utils.py` 解析器的切分标记——提示词与解析器是一份契约的两面。
- `meta_verification` 评判的是「评价是否合理」而非「解答是否正确」：四个分析维度中**缺陷分析最重要**，正面肯定明确排除在范围之外，评级决策树按「不合理缺陷的比例 → 表达/打分检查」两级展开。
- `proof_refinement` 是组合式提示词：`{instruction}` 内嵌整份渲染好的生成提示词（嵌套 `.format` 安全，因为值不参与二次解析），`{proofs_to_refine}` 填 `--- Solution N ---` / `=== Evaluation M ===` 格式的证明与评价摘要。

## 7. 下一步学习建议

本讲只讲了「提示词怎么写」，下一讲 **u3-l2《输出解析工具：utils.py 的 boxed 提取与章节切分》** 讲「模型回复怎么拆」：`extract_boxed_answers` 的括号计数算法如何对付嵌套的 `\boxed{\frac{1}{2}}`，`_normalize_prover_output` 如何容错星号标题。之后带着本讲的「格式契约」视角进入 **u4-l2 / u4-l3**，看 `prepare_proof_verification` 与 `prepare_meta_verification` 如何把模板渲染与解析串成完整阶段；对精炼摘要的采样细节感兴趣的读者可以预习 [inference/main.py:L222-L284](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L222-L284)，那是 u5-l2 的主战场。
