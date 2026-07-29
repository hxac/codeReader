# 规则奖励函数：从答案到分数

## 1. 本讲目标

本讲是「数据与任务定义」单元的收口。前面三讲我们看到了 countdown / multiply 任务如何生成数据、prompt 如何被 tokenize 成张量。现在要回答强化学习里最关键的一个问题：

> 模型给出了一段回答，我们怎么把它变成一个数字（奖励）去训练它？

读完本讲，你应当能够：

1. 看懂 `countdown.compute_score` 的「提取 → 校验 → 求值」三步流程，并能预测任意一条输入会得到 `0`、`0.1` 还是 `1.0`。
2. 理解 `format_score`（格式分）为什么存在、它作为「奖励塑形（reward shaping）」的作用与潜在风险。
3. 看清 `evaluate_equation` 用 `eval` 求值时所做的两层安全防护（正则白名单 + 受限命名空间）。
4. 解释 `do_print = random.randint(1, 64) == 1` 这行抽样日志的作用。

本讲只讲**规则奖励（rule-based reward）**：不依赖神经网络判分，而是用确定性的 Python 代码判定对错。这正是 TinyZero 选择 countdown / multiply 这类任务的根本原因——它们的答案可以用代码精确、即时、自动地判错对，这正是 RL 训练能跑起来的前提。

## 2. 前置知识

在进入源码前，先用一段话建立直觉。

- **奖励（reward）**：RL 里引导模型学习的「分数」。在 TinyZero 中，回答完全正确得满分，格式正确但答案错误得一个小分，连格式都不对得 0 分。
- **稀疏奖励问题（sparse reward）**：如果模型一开始很差、几乎永远拿不到满分，那么奖励恒为 0，梯度无从下手，训练就停滞了。`format_score` 就是为缓解这个问题而设计的小额「保底分」——只要格式对，哪怕算错了也给一点点信号，让模型先学会「按格式回答」。
- **奖励塑形（reward shaping）**：在真正的目标奖励之外，人为加一些辅助性的、稠密的小奖励，帮模型更快进入正轨。`format_score` 就是一种塑形。
- **`eval` 的危险性**：Python 的 `eval` 会执行任意字符串里的代码。如果直接 `eval(模型生成的字符串)`，模型（或对抗样本）可以写出 `__import__('os').system(...)` 之类危险代码。因此 TinyZero 在调用 `eval` 前做了正则白名单过滤，并在调用时禁用了所有内建函数。
- **奖励路由（routing）**：不同任务用不同判分函数。系统根据每条样本的 `data_source` 字段（如 `'countdown'`、`'yolo/multiply-3_digit'`）查表决定调用哪个 `compute_score`。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `verl/utils/reward_score/countdown.py` | countdown 任务的规则奖励：提取等式、校验数字、安全求值、分级打分。本讲主线。 |
| `verl/utils/reward_score/multiply.py` | multiply / arithmetic 任务的规则奖励：只比较 `<answer>` 里的整数与 ground_truth。作为对照。 |
| `verl/utils/reward_score/gsm8k.py` | GSM8K 任务的规则奖励：另一种「严格 / 灵活」提取模式，且 `format_score` 默认为 `0`。作为对照。 |
| `verl/trainer/main_ppo.py` | 把上述 `compute_score` 接入训练：`_select_rm_score_fn` 做路由，`RewardManager.__call__` 做实际调用并把标量分数放到 token 张量上。 |

## 4. 核心概念与源码讲解

### 4.1 规则奖励函数的整体定位与路由

#### 4.1.1 概念说明

`compute_score` 不是一个被孤立的函数，它要被训练主循环调用。调用链是：

```
训练一步 → RewardManager(data) → 按 data_source 选 compute_score_fn → score = compute_score(solution_str, ground_truth)
```

也就是说，`RewardManager` 拿到一个 batch 的生成结果，逐条解码成字符串，再按该样本的 `data_source` 字段去 `_select_rm_score_fn` 里查表，找到对应的判分函数。这就是 TinyZero「加一个新任务 = 加一条路由 + 写一个 compute_score」的原因（详见 u7-l3）。

#### 4.1.2 核心流程

`_select_rm_score_fn` 是一张「字符串 → 函数」的路由表：

1. 输入 `data_source`（如 `'countdown'`）。
2. 依次匹配：`'openai/gsm8k'` → `gsm8k.compute_score`；`'lighteval/MATH'` → `math.compute_score`；字符串里含 `'multiply'` 或 `'arithmetic'` → `multiply.compute_score`；含 `'countdown'` → `countdown.compute_score`。
3. 都不匹配则抛 `NotImplementedError`。

注意 multiply / countdown 用的是**子串包含**判断（`"countdown" in data_source`），所以 u2-l1 里数据预处理写出的 `data_source='countdown'` 和 u2-l2 里的 `'yolo/multiply-3_digit'` / `'yolo/arithmetic-3_digit'` 都能命中。

#### 4.1.3 源码精读

路由表 [verl/trainer/main_ppo.py:24-34](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/main_ppo.py#L24-L34)：用一串 `if/elif` 把 `data_source` 映射到具体的 `compute_score` 函数对象，注意 multiply/arithmetic 共用同一个 `multiply.compute_score`（这也印证了 u2-l2 的结论：乘法/加减乘任务判分逻辑相同，只比结果整数）。

`RewardManager.__call__` [verl/trainer/main_ppo.py:45-90](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/main_ppo.py#L45-L90) 是真正调用判分函数的地方，关键几步：

- 先从 `non_tensor_batch['data_source']` 取任务名，再 `_select_rm_score_fn` 拿到判分函数；
- 用 `tokenizer.decode(prompt_ids + response_ids)` 还原成完整字符串 `sequences_str`；
- 从 `non_tensor_batch['reward_model']['ground_truth']` 取出判分参考答案；
- 调用 `score = compute_score_fn(solution_str=sequences_str, ground_truth=ground_truth)`（第 80 行）；
- 把这个标量 `score` 放到 `reward_tensor[i, valid_response_length - 1]`，也就是放到**回答最后一个有效 token** 的位置上（第 81 行）。

「把分数放到回答末尾 token」是 PPO/GRPO 的标准做法：advantage 计算需要 token 级奖励，而规则奖励只能给整个回答一个总分，于是把它挂到回答的最后一个 token 上，再由后续 `compute_advantage` 传播到整个回答（详见 u5-l1）。

#### 4.1.4 代码实践

实践目标：确认 `data_source` 字符串与判分函数的对应关系。

操作步骤（源码阅读型实践）：

1. 打开 `_select_rm_score_fn`，对照 u2-l1、u2-l2 中各数据预处理脚本写出的 `data_source` 取值。
2. 自查：如果新任务的数据源命名为 `'yolo/arithmetic-5_digit'`，会命中哪一行？是否会正确落到 `multiply.compute_score`？

预期结果：`'yolo/arithmetic-5_digit'` 含子串 `'arithmetic'`，命中第 29 行，返回 `multiply.compute_score`。这正是 u2-l2 所说的「扩展运算符无需改奖励代码」的原因——连路由都不用改。

#### 4.1.5 小练习与答案

**练习**：若某条样本的 `data_source='my_new_task'`，训练时会发生什么？

**答案**：四个分支都不命中，`_select_rm_score_fn` 执行到末尾抛出 `NotImplementedError`，训练会中断。要支持新任务，必须在路由表里加一个分支（例如 `elif "my_new_task" in data_source: return my_task.compute_score`），这正是 u7-l3「自定义新任务」要做的事。

---

### 4.2 extract_solution：从生成文本里抠出答案

#### 4.2.1 概念说明

模型生成的是一长串 token（解码后是一段自然语言，含 `<think>...</think><answer>...</answer>`）。判分的第一步是**从这段文本里把模型给出的答案（等式）抠出来**。`extract_solution` 干的就是这件事。

它有两个隐藏前提，初学者很容易踩坑：

1. 输入字符串里必须含有 `"Assistant:"` 或 `"<|im_start|>assistant"` 标记，否则直接返回 `None`。这是因为 `RewardManager` 解码的是 prompt+response 整体，prompt 模板（见 u2-l1）里本来就带 `"Assistant:"`，用来标识「从这里开始是模型的回答」。
2. 抠答案时只看**最后一行**（按 `\n` 切分取 `[-1]`）。所以 `<answer>` 标签必须出现在模型回答的最后一行，否则可能取不到。

#### 4.2.2 核心流程

```
输入 solution_str
  ↓
定位 "Assistant:"（或 <|im_start|>assistant），取其后的部分 → 视为「模型回答」
  ↓
取最后一行
  ↓
用正则 <answer>(.*?)</answer> 找所有匹配，取最后一个匹配的内容并 strip
  ↓
返回等式字符串；若任一步失败 → 返回 None
```

#### 4.2.3 源码精读

[verl/utils/reward_score/countdown.py:7-25](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/reward_score/countdown.py#L7-L25)

关键点逐条说明：

- 第 10-15 行：先用 `"Assistant:"` 切分定位模型回答；countdown 还兼容了 Qwen 的 `<|im_start|>assistant` 标记（对应 u2-l1 里 `qwen-instruct` 模板）。两者都没有就 `return None`。
- 第 16 行：`split('\n')[-1]` 取最后一行。
- 第 18-24 行：正则 `<answer>(.*?)</answer>`，`.*?` 非贪婪；用 `re.finditer` 列出所有匹配后取 `matches[-1]`，即**最后一个** `<answer>` 标签——这是为了防止模型在思维链里写了多个 `<answer>`，只认最后那个。

> 对照：multiply 版的 `extract_solution` [verl/utils/reward_score/multiply.py:5-24](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/reward_score/multiply.py#L5-L24) 逻辑几乎相同，但多了一步 `int(final_answer)`：若 `<answer>` 里不是整数，直接返回 `None`。因为 multiply 只比较整数结果。

#### 4.2.4 代码实践

实践目标：亲手验证「最后一行」与「最后一个 `<answer>`」两个细节。

操作步骤：

1. 在 Python 里构造一段含两个 `<answer>` 的字符串（示例代码）：
   ```python
   # 示例代码
   s = "User: ...\nAssistant: <think>先猜 5*3</think><answer>5*3</answer>\n再确认一下<answer>4*3+2*2</answer>"
   from verl.utils.reward_score.countdown import extract_solution
   print(extract_solution(s))   # 观察取到的是哪个 <answer>
   ```
2. 把第二个 `<answer>` 换到第一行、末尾另起一行空白，再观察。

需要观察的现象：取到的是最后一个 `<answer>` 的内容；但如果 `<answer>` 不在最后一行（比如后面跟了一个换行 + 文字），可能取不到。

预期结果：上面示例应返回 `"4*3+2*2"`（最后一个 `<answer>`，且它在最后一行）。若无法本地运行，标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `extract_solution` 要取最后一个 `<answer>` 而不是第一个？

**答案**：模型在 `<think>` 思维链里可能反复尝试、写出多个 `<answer>`，只有放在回答末尾的才是它的最终结论。取最后一个能贴合「最终答案」的语义。

**练习 2**：如果 `solution_str` 里完全没有 `"Assistant:"` 字样，`extract_solution` 返回什么？后续 `compute_score` 会给几分？

**答案**：返回 `None`；`compute_score` 进入 `if equation is None` 分支，返回 `0`。这就是为什么 prompt 模板里必须保留 `"Assistant:"` 标记——它既是给模型看的角色提示，也是奖励函数定位回答的锚点（与 u2-l1 所讲「改 prompt 必须同步改奖励函数」相呼应）。

---

### 4.3 validate_equation 与 evaluate_equation：校验数字与安全求值

#### 4.3.1 概念说明

抠出等式字符串（如 `"4*3*2*1"`）后，还不能直接判分。countdown 任务的要求是「**用给定的若干数字，每个恰好用一次，凑出目标数**」。所以判分有两步：

1. **`validate_equation`**：检查等式里出现的数字，是不是恰好等于题目给定的 `numbers`（每个用一次，不多不少）。
2. **`evaluate_equation`**：把等式字符串安全地求值成数值，再与 `target` 比较。

这两步必须分开，因为光求值对了还不够——模型可能「偷数字」（比如题目给 `[1,2,3,4]` 凑 24，模型写 `25-1`，结果对但用了非法的 25）。`validate_equation` 就是堵这个漏洞。

`evaluate_equation` 的难点在于：它要对**模型生成的字符串**调用 `eval`，必须保证安全。

#### 4.3.2 核心流程

`validate_equation`：

```
等式字符串 → 用正则 \d+ 抓出所有数字 → 转成 int → 排序
可用数字 numbers → 排序
两者完全相等？是 → True；否 → False
```

`evaluate_equation`（两层防护）：

```
等式字符串
  ↓
第一层：正则 ^[\d+\-*/().\s]+$ 校验「整串只含数字、+-*/(). 与空白」
        不通过 → 抛错 → 返回 None
  ↓
第二层：eval(eq, {"__builtins__": None}, {}) 在「无内建、空局部命名空间」下求值
        出错 → 返回 None
  ↓
返回数值结果（int 或 float）
```

#### 4.3.3 源码精读

`validate_equation` [verl/utils/reward_score/countdown.py:28-41](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/reward_score/countdown.py#L28-L41)：`re.findall(r'\d+', equation_str)` 抓出所有连续数字段并转 `int`，排序后与排序后的 `available_numbers` 比较。注意它是**按整体集合（含重复元素）比较**：等式里每个数字的出现次数必须与题目给定完全一致。题目给 `[2,3,5]`，等式 `5*3` 抓到 `[3,5]`，缺了 2，不相等 → 校验失败。

`evaluate_equation` [verl/utils/reward_score/countdown.py:44-56](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/reward_score/countdown.py#L44-L56)：

- 第 48-49 行，白名单正则 `^[\d+\-*/().\s]+$` 用 `^...$` 锚定**整个字符串**，只允许数字、四则运算符、小括号、小数点和空白。任何字母、下划线、引号都会被拒——这就排除了 `__import__`、`os`、`eval` 嵌套等危险写法。
- 第 53 行，`eval(equation_str, {"__builtins__": None}, {})`：第二个参数把全局命名空间的 `__builtins__` 设为 `None`（禁用 `open`/`__import__` 等所有内建），第三个参数给空局部命名空间。这是「纵深防御」——即便白名单正则有疏漏，命名空间里也没有任何可调用的危险对象。
- 第 55 行：任何异常（含被第 49 行主动抛出的 `ValueError`）都被捕获，返回 `None`。

> 安全提示：这种「正则白名单 + 受限 eval」是社区常见做法，但 `eval` 本质上仍有风险。TinyZero 这里能接受，是因为输入先被白名单锁死成纯算术表达式。生产环境若处理更复杂的输入，建议改用 `ast.literal_eval` 或专用的表达式解析库。

#### 4.3.4 代码实践

实践目标：体会 `validate_equation` 防「偷数字」、`evaluate_equation` 防「危险字符」。

操作步骤（示例代码，待本地验证）：

```python
# 示例代码
from verl.utils.reward_score.countdown import validate_equation, evaluate_equation
print(validate_equation("4*3*2*1", [1, 2, 3, 4]))   # 预期 True：数字用全且各一次
print(validate_equation("25-1",     [1, 2, 3, 4]))   # 预期 False：用了非法的 25
print(validate_equation("4*3",      [1, 2, 3, 4]))   # 预期 False：少用了 1 和 2
print(evaluate_equation("4*3*2*1"))                  # 预期 24
print(evaluate_equation("__import__('os')"))         # 预期 None：白名单拒绝字母
```

需要观察的现象：`25-1` 虽然结果也是 24，但 `validate_equation` 判 False——这正是 countdown 区别于 multiply 的核心：**不仅结果要对，过程（用了哪些数字）也要合规**。

预期结果：依次为 `True`、`False`、`False`、`24`、`None`。

#### 4.3.5 小练习与答案

**练习 1**：等式 `"4*3*2*1"`、可用数字 `[1,2,3,4]`，`validate_equation` 为何返回 `True`？如果把可用数字改成 `[1,2,3]` 呢？

**答案**：`re.findall` 从 `"4*3*2*1"` 抓到 `[4,3,2,1]`，排序后 `[1,2,3,4]`，与可用数字排序后相等 → `True`。若可用数字是 `[1,2,3]`，等式抓到四个数字、可用只有三个，不相等 → `False`。

**练习 2**：`evaluate_equation("__import__('os').system('rm -rf /')")` 会返回什么？为什么不会真的执行删除？

**答案**：返回 `None`。因为白名单正则 `^[\d+\-*/().\s]+$` 检测到字符串里有字母、下划线、引号，与模式不符，第 49 行直接 `raise ValueError`，被第 55 行 `except` 捕获返回 `None`。字符串根本到不了真正的 `eval` 执行阶段。

---

### 4.4 compute_score：分级打分与 format_score

#### 4.4.1 概念说明

把前三步串起来就是 `compute_score`：它接收模型回答字符串和 ground_truth，返回一个 `0` / `0.1` / `1.0` 的标量。它的打分是**分级（graded）**的：

- 连 `<answer>` 都抠不到 → `0`：完全没学会格式。
- 有 `<answer>` 且格式对，但数字用错 / 算错 / 求值失败 → `format_score`（默认 `0.1`）：格式对，奖励一点点。
- 数字用对、结果也对 → `score`（默认 `1.0`）：完全正确。

`format_score` 就是本讲最重要的设计。它本质是**奖励塑形**：在模型还远没学会解题的早期，给它一点「至少把格式写对」的稠密信号，避免奖励长期为 0、训练停滞。但它的风险也在这里——如果 `format_score` 设得太高，模型可能「只刷格式不解题」（奖励黑客 / reward hacking），这在 u7-l6 会详细讨论。

#### 4.4.2 核心流程

判定逻辑（用伪代码表示）：

```
equation = extract_solution(solution_str)        # 抠答案
if equation is None:            return 0          # 没格式
if not validate_equation(...):  return format_score  # 格式对但数字不合规
result = evaluate_equation(equation)
if result is None:             return format_score  # 求值失败
if |result - target| < 1e-5:    return score          # 全对
else:                          return format_score  # 算错
（任何异常）                    return format_score
```

数学上，正确判定的阈值是

\[
\text{correct} \iff |\text{result} - \text{target}| < 10^{-5}
\]

用 `1e-5` 容差是为了吸收浮点误差（除法 `/` 会产生 float，例如 `8/2` 在浮点下可能不是精确的 `4.0`）。

#### 4.4.3 源码精读

`compute_score` [verl/utils/reward_score/countdown.py:59-111](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/reward_score/countdown.py#L59-L111)，要点：

- 第 69-70 行：从 `ground_truth` 字典取出 `target` 和 `numbers`（对应 u2-l1 parquet 里 `reward_model.ground_truth = {'target':..., 'numbers':[...]}`）。
- 第 72 行：调用 `extract_solution` 抠等式。
- 第 73 行 `do_print = random.randint(1, 64) == 1`：抽样日志开关，约 1/64 的概率为真（见 4.4.4）。
- 第 81-84 行：`equation is None` → 返回 `0`。
- 第 87-90 行：`validate_equation` 不通过 → 返回 `format_score`。
- 第 94-107 行：求值后比较，正确返回 `score`，其余（求值失败 / 结果错误）返回 `format_score`。
- 第 100 行 `abs(result - target) < 1e-5`：浮点容差判定。
- 第 108-111 行：`try` 块外的兜底 `except`，也返回 `format_score`。

需要留意两个细节：

1. **`method` 参数是摆设**。`countdown.compute_score` 的签名有 `method='strict'`（第 59 行），但函数体里从未使用 `method`。它是为了和 `gsm8k.compute_score` / `math.compute_score` 保持「同签名」而保留的，countdown 实际不区分 strict/flexible。读源码时要警惕这种「签名有、实现没用」的情况，不要被误导。
2. **三个对照函数的 `format_score` 默认值不同**：countdown 与 multiply 默认 `0.1`（给格式分），而 gsm8k 默认 `0.0`（[verl/utils/reward_score/gsm8k.py:44-62](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/reward_score/gsm8k.py#L44-L62)）。这说明 format_score 是可调的超参，不同任务策略不同。

#### 4.4.4 代码实践

实践目标：理解 `do_print` 抽样日志的作用。

操作步骤：

1. 阅读 [verl/utils/reward_score/countdown.py:73](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/reward_score/countdown.py#L73) 这一行 `do_print = random.randint(1, 64) == 1`。
2. 解释：`random.randint(1, 64)` 在闭区间 `[1, 64]` 上均匀取整数，等于 1 的概率是多少？为什么用这种方式而不是「每条都打印」？

参考答案：概率为 \(1/64 \approx 1.56\%\)。RL 训练每一步有成千上万条样本流过 `compute_score`，全打印会淹没 stdout 并拖慢训练；按约 1/64 抽样，既能让你用肉眼持续观察到「模型当前在生成什么样的等式、格式对不对、卡在哪一步」，又不会刷屏。它打印的内容（target、numbers、抠出的等式、命中的分支、原始 solution_str）正是调试奖励函数最需要的信息。

注意区分两套独立的抽样日志：
- 这里的 `do_print` 在 `compute_score` 内部，按 1/64 随机抽样，打印**判分细节**；
- `RewardManager` 的 `num_examine`（[verl/trainer/main_ppo.py:86-88](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/main_ppo.py#L86-L88)）按每个 `data_source` 最多打印若干条**完整解码序列**。
两者互不影响。

#### 4.4.5 小练习与答案

**练习 1**：题目 `target=24, numbers=[1,2,3,4]`，模型回答抠出等式 `"1+2+3+4"`。会得几分？为什么？

**答案**：得 `0.1`（format_score）。校验：`re.findall` 抓到 `[1,2,3,4]`，与可用数字相等，`validate_equation` 通过；但 `evaluate_equation("1+2+3+4")=10`，`|10-24|=14` 不小于 `1e-5`，进入「结果错误」分支，返回 `format_score`。即「格式对、过程合规、但算错了」。

**练习 2**：如果把 countdown 的 `format_score` 从 `0.1` 调到 `0.9`，训练可能出什么问题？

**答案**：模型会发现「只要把 `<answer>` 格式写对、哪怕瞎写一个合规等式，就能拿 0.9 分」，于是学会「刷格式 + 凑合规数字」而不真正解题——这就是奖励黑客。所以 format_score 必须远小于满分，仅作为起步信号。更深入的分析见 u7-l6。

---

## 5. 综合实践

**任务**：为 `countdown.compute_score` 写一个最小测试脚本，构造三条输入，分别命中 `1.0`、`0.1`、`0` 三个分支，并预测每条得分。

实践目标：把本讲的「提取 → 校验 → 求值 → 分级」串起来，验证你对打分逻辑的理解。

操作步骤（示例代码，请本地运行验证）：

```python
# 示例代码 —— 待本地验证
from verl.utils.reward_score import countdown

ground_truth = {'target': 24, 'numbers': [1, 2, 3, 4]}

# 前缀必须含 "Assistant:"，否则 extract_solution 直接返回 None
prefix = "User: ...\nAssistant: "

# 分支 A：完全正确 → 期望 1.0
# 4*3*2*1 用了 1,2,3,4 各一次，且结果=24
sA = prefix + "<think>把四个数相乘</think><answer>4*3*2*1</answer>"

# 分支 B：格式对、数字合规、但结果错 → 期望 0.1
# 1+2+3+4=10 != 24
sB = prefix + "<think>直接相加试试</think><answer>1+2+3+4</answer>"

# 分支 C：连 <answer> 标签都没有 → 期望 0
sC = prefix + "<think>嗯……我不太确定</think> 答案大概是 24 吧"

print("A:", countdown.compute_score(solution_str=sA, ground_truth=ground_truth))  # 期望 1.0
print("B:", countdown.compute_score(solution_str=sB, ground_truth=ground_truth))  # 期望 0.1
print("C:", countdown.compute_score(solution_str=sC, ground_truth=ground_truth))  # 期望 0
```

需要观察的现象与预期结果：

| 输入 | 命中分支 | extract_solution | validate_equation | evaluate_equation | 预期得分 |
| --- | --- | --- | --- | --- | --- |
| A `4*3*2*1` | 完全正确 | `"4*3*2*1"` | True（数字用全） | `24`，`|24-24|<1e-5` | **1.0** |
| B `1+2+3+4` | 结果错误 | `"1+2+3+4"` | True（数字用全） | `10`，`|10-24|≥1e-5` | **0.1** |
| C 无 `<answer>` | 无格式 | `None` | 不执行 | 不执行 | **0** |

进阶（可选）：再补两条——D：等式用了非法数字（如 `"25-1"`），预期 `0.1`（校验失败）；E：等式含非法字符（如 `"4**3**2**1"`，注意 `**` 是幂运算，不在白名单内），预期 `0.1`（求值失败）。若本地 `random` 抽到打印，你还会在 stdout 看到 `do_print` 的诊断块——这正好顺便验证 4.4.4。

说明：本仓库 `tests/` 下没有针对 `compute_score` 的现成测试（可用 `grep -r "compute_score" tests/` 验证），所以这个脚本是你新建的，不属于项目原有代码。

## 6. 本讲小结

- **三步流程**：countdown 的规则奖励是「`extract_solution` 提取 `<answer>` → `validate_equation` 校验数字合规 → `evaluate_equation` 安全求值并与 `target` 比较」。
- **分级打分**：无格式 `0`；格式对但过程/结果错 `format_score`（默认 `0.1`）；完全正确 `score`（默认 `1.0`）；用 `abs(result-target)<1e-5` 吸收浮点误差。
- **format_score 是奖励塑形**：给早期模型一个稠密的「至少写对格式」信号，缓解稀疏奖励；但设太高会诱发「只刷格式」的奖励黑客。
- **安全求值**：`evaluate_equation` 用「正则白名单（`^[\d+\-*/().\s]+$`）+ 受限 eval（`__builtins__=None`、空 locals）」两层防护，避免模型生成的危险字符串被执行。
- **路由接入**：`_select_rm_score_fn` 按 `data_source` 子串匹配选函数，`RewardManager.__call__` 调用后把标量分数挂到回答最后一个有效 token 上。
- **对照差异**：multiply 只比 `<answer>` 里的整数与 ground_truth，无校验/求值；gsm8k 的 `format_score` 默认 `0`；countdown 的 `method` 参数实际未使用。

## 7. 下一步学习建议

本讲结束了「数据与任务定义」单元。到这里你已经掌握了从数据生成、tokenize 到规则奖励的完整数据侧链路。

接下来进入第三单元「数据协议与单控制器」：

- 先读 **u3-l1 DataProto 数据传输协议**，理解 `RewardManager` 返回的 `reward_tensor` 如何作为 `DataProto.batch` 的一部分在各 Worker 间流转。
- 之后 **u5-l1 KL 惩罚与优势函数计算** 会用到本讲的标量 `score`：`token_level_rewards = score - beta * KL`，把规则奖励与 KL 惩罚组合成最终的 token 级奖励。
- 如果你想立刻动手加一个自己的任务，可以跳读 **u7-l3 自定义新任务：端到端扩展**，它会用到本讲的「写 compute_score + 加路由分支」三件套。
