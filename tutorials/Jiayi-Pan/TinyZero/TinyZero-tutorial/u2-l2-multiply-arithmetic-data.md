# Multiply 与 Arithmetic 任务数据生成

## 1. 本讲目标

上一讲（u2-l1）我们拆解了 countdown（数字凑数）任务的数据预处理脚本，理解了「prompt 模板 + parquet 字段 + data_source 路由」这套数据出厂流程。本讲把目光转向另外两个数值类任务——**multiply（乘法）** 与 **arithmetic（四则运算）**，对照阅读它们的预处理脚本。

读完本讲，你应当能够：

- 说清楚 `get_random_num` 如何用 `DIGIT` 和 `LESS_OR_EQUAL` 控制生成数字的位数范围；
- 对比 `multiply.py`（只做乘法）与 `arth.py`（设计上支持 `+ - *`）在样本生成、字段结构、prompt 模板上的差异；
- 解释 `arth.py` 为什么在减法分支里交换 `num1`、`num2` 以保证结果非负；
- 独立完成「让 arithmetic 真正生成加减乘三种运算」的最小改动，并判断这一改动是否需要同步修改奖励函数。

## 2. 前置知识

在进入源码前，先建立两个直觉。

**第一，为什么要按「位数」生成数字？** 乘法/算术任务的难度高度依赖数字的位数：`12 × 7` 和 `987 × 64` 的计算量不在一个量级。直接在全范围 `[0, 999]` 均匀采样会让题目偏简单（多数数字位数较少）。因此脚本用一个「分层抽样」的小技巧：每次先随机决定「这次抽几位数的数字」（N 位、N-1 位、N-2 位），再在该位数区间内均匀采样。一个 DIGIT 位的数 \(n\) 满足

\[
10^{\text{DIGIT}-1} \le n \le 10^{\text{DIGIT}}-1
\]

脚本用 `max_num = 10**DIGIT` 给出区间上界 `max_num-1`，用 `max_num//10`（即 \(10^{\text{DIGIT}-1}\)）给出「恰好 DIGIT 位」的下界。

**第二，为什么数据生成脚本里要操心运算符？** 因为 RL 训练需要「可机器判错对」的奖励信号。对算术题而言，标准答案就是一个整数，模型在 `<answer>` 标签里输出一个整数，比一下是否相等即可。运算符越多，题目种类越丰富，但只要奖励函数只看「最终整数是否正确」，就**不需要为每种运算符写不同的奖励逻辑**——这是本讲后半段会验证的关键点。

本讲涉及的术语大多在 u2-l1 已建立：`data_source`（奖励函数路由键）、`reward_model.ground_truth`（判分参考答案）、`<think>`/`<answer>` 标签（约定输出格式）、parquet（veRL 数据加载器期望的列式存储格式）。如有遗忘请回看 u2-l1。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [examples/data_preprocess/multiply.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/examples/data_preprocess/multiply.py) | 乘法任务数据生成脚本。生成 `(num1, num2, result)` 三元组，prompt 里运算符写死为 `*`，`data_source='yolo/multiply-3_digit'`。 |
| [examples/data_preprocess/arth.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/examples/data_preprocess/arth.py) | 算术任务数据生成脚本。设计上支持 `+ - *`，每条样本多带一个 `operation` 字段，`data_source='yolo/arithmetic-3_digit'`。 |
| [verl/trainer/main_ppo.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/main_ppo.py) | 训练入口。其 `_select_rm_score_fn` 根据 `data_source` 字符串选择奖励函数——本讲用它确认 multiply/arithmetic 都路由到同一个奖励函数。 |
| [verl/utils/reward_score/multiply.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/reward_score/multiply.py) | multiply/arithmetic 共用的规则奖励函数。只比较 `<answer>` 里的整数与 ground_truth，**与运算符无关**。 |

两个预处理脚本结构几乎是镜像的，差异集中在「是否带运算符」这一点上，所以本讲会反复左右对照。

## 4. 核心概念与源码讲解

### 4.1 multiply.py 的 gen_dataset：按位数生成数字与乘法样本

#### 4.1.1 概念说明

`gen_dataset` 是数据生成的核心：给定样本数 `N`、目标位数 `DIGIT`、是否允许更短位数的开关 `LESS_OR_EQUAL`，返回一个 `(num1, num2, result)` 三元组列表。multiply 任务里 `result` 永远是 `num1 * num2`，没有别的运算。

它内部定义了一个闭包 `get_random_num()`，把「分层抽样」封装起来：先掷一个 `r` 决定抽哪个位数段，再在该段里均匀取数。理解了这个闭包，两个脚本的数据生成就都懂了。

#### 4.1.2 核心流程

`gen_dataset` 的执行过程（伪代码）：

```text
seed(1)                            # 固定随机种子，保证可复现
equations = []
for _ in range(N):
    num1 = get_random_num()        # 分层抽样一个数
    num2 = get_random_num()        # 独立再抽一个
    result = num1 * num2           # multiply：只算乘法
    equations.append((num1, num2, result))
return equations
```

`get_random_num` 内部的分层逻辑：

```text
r = randint(0, 3)                  # 见 4.1.3 关于源码实际写法的说明
if r == 0:   max_num = 10**(DIGIT-2)   # 比 DIGIT 少 2 位
elif r == 1: max_num = 10**(DIGIT-1)   # 比 DIGIT 少 1 位
else:        max_num = 10**(DIGIT)     # DIGIT 位
return randint(下界, max_num - 1)
```

下界由 `LESS_OR_EQUAL` 决定：`True` 时下界为 0（允许更短的数，含 0），`False` 时下界为 `max_num//10`（强制恰好该位数）。

#### 4.1.3 源码精读

`gen_dataset` 的整体定义与三元组产出：

[multiply.py:29-59 — gen_dataset 定义，返回 (num1, num2, result) 三元组](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/examples/data_preprocess/multiply.py#L29-L59)

分层抽样的闭包：

[multiply.py:39-52 — get_random_num：按 r 选择位数段并采样](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/examples/data_preprocess/multiply.py#L39-L52)

关键一行（需要特别注意的真实源码）：

[multiply.py:40 — `r = randint(,3)`](https://github.com/Jiayi-Pan-TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/examples/data_preprocess/multiply.py#L40)

> ⚠️ **真实源码里这一行就写作 `r = randint(,3)`，缺了第一个参数。** 在 Python 里 `randint(,3)` 是语法错误（`SyntaxError: invalid syntax`），所以 `multiply.py` **按当前仓库状态是无法被导入/运行的**。这很可能是想写 `randint(0, 3)`（与 arth.py 的 `randint(1,3)` 对照可见）。本讲后续对 multiply 行为的分析都按「修正为 `randint(0, 3)`」的意图来讲解；是否真如此、以及修正后能否跑通，**待本地验证**。

在「修正为 `randint(0, 3)`」的理解下，`r` 取 `{0,1,2,3}`，三个分支都会命中；multiply 因此会比 arth 多产生一些「少 2 位」的小数字（见 4.3.3 对比）。

固定乘法计算：

[multiply.py:54-58 — num1、num2 独立采样，result = num1 * num2](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/examples/data_preprocess/multiply.py#L54-L58)

`__main__` 里的参数（`DIGIT=3`、`data_source` 命名）：

[multiply.py:82-88 — DIGIT=3，data_source='yolo/multiply-3_digit'，TRAIN/TEST 划分](https://github.com/Jiayi-Pan-TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/examples/data_preprocess/multiply.py#L82-L88)

`LESS_OR_EQUAL=True` 的作用体现在这一行的下界选择上：

[multiply.py:44 — `randint(0 if LESS_OR_EQUAL else max_num//10, max_num-1)`](https://github.com/Jiayi-Pan-TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/examples/data_preprocess/multiply.py#L44)

#### 4.1.4 代码实践

这是一个**源码阅读型实践**（不需要 GPU）。

1. **实践目标**：建立「DIGIT + LESS_OR_EQUAL → 数字范围」的直觉。
2. **操作步骤**：取 `DIGIT=3`，分别对 `r==0/1/else` 三个分支，写出 `max_num`、`max_num//10`、`max_num-1` 的值；再分别对 `LESS_OR_EQUAL=True` 和 `False` 写出最终采样区间。
3. **需要观察的现象**：`LESS_OR_EQUAL=True` 时下界统一是 0，意味着每个分支都可能采到比该位数更短的数；`False` 时下界是 `max_num//10`，强制恰好该位数。
4. **预期结果**（DIGIT=3）：

| r 分支 | max_num | True 区间 | False 区间 |
| --- | --- | --- | --- |
| r==0（少 2 位） | 10 | [0, 9] | [1, 9] |
| r==1（少 1 位） | 100 | [0, 99] | [10, 99] |
| else（3 位） | 1000 | [0, 999] | [100, 999] |

5. 运行结果：**待本地验证**（且需先把 `randint(,3)` 改为合法写法）。

#### 4.1.5 小练习与答案

**练习 1**：把 `DIGIT` 从 3 改成 4，`r==1` 分支在 `LESS_OR_EQUAL=True` 下的采样区间是什么？
**答案**：`max_num = 10**(4-1) = 1000`，区间为 `[0, 999]`（即至多 3 位数）。

**练习 2**：为什么脚本用 `seed(1)`？
**答案**：固定随机种子让两次运行生成相同的数据集，保证实验可复现、train/test 划分稳定。

**练习 3**：源码里 `r==0` 分支的注释写的是「2 digits less than original」，但 `DIGIT=3` 时 `max_num=10` 只能产生 1 位数（甚至 0）。注释和代码一致吗？
**答案**：不完全一致。注释说的是「相对 DIGIT 少 2 位」这件事本身没错（3→1），但「位数」的概念在边界上含糊（0 算几位）。读源码时要以代码为准，注释只能当线索。脚本顶部「50% chance of being N-digit or N/2-digit」的注释也与实际的 3/4 路分支不符——这是真实仓库里注释陈旧的常见现象。

---

### 4.2 multiply.py 的 make_prefix：乘法 prompt 模板与 `<think>` 引导续写

#### 4.2.1 概念说明

`make_prefix` 把一条样本 `(num1, num2)` 翻译成给模型看的自然语言 prompt。它要完成两件事：一是用 `<think>`/`<answer>` 标签告诉模型输出格式，二是以一个**未闭合的 `<think>`** 结尾，引导模型从这里开始续写推理过程。multiply 版本把运算符直接写死成 `*`。

#### 4.2.2 核心流程

prompt 的结构是一段「角色设定 + 例示 + 题目 + 助手开场白 + 未闭合标签」：

```text
[角色设定] A conversation between User and Assistant. ...
[格式约定] ... enclosed within <think> </think> and <answer> </answer> tags ...
[例示]     <think> reasoning process here </think> <answer> RESULT_NUMBER </answer>.
[题目]     User: Give me the answer of the following equation: {num1} * {num2} =
[开场白]   Assistant: Ok let me think about it.
[续写钩子] <think>
```

模型拿到的字符串在 `<think>` 处截断，于是它会从「接着写推理」开始生成，先输出推理链，再（理想情况下）闭合 `</think>` 并给出 `<answer>`。

#### 4.2.3 源码精读

[multiply.py:69-73 — make_prefix：运算符写死为 *，以未闭合 <think> 结尾](https://github.com/Jiayi-Pan-TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/examples/data_preprocess/multiply.py#L69-L73)

两个要点：

- 模板里是 `{num1} * {num2} =`，`*` 是字面量，不来自数据。这意味着即使你给 multiply 喂别的运算符，prompt 也只会显示乘号。
- 末尾 `\n<think>` 没有对应的 `</think>`——这是刻意的「续写钩子」，和 u2-l1 countdown 的做法完全一致。

#### 4.2.4 代码实践

1. **实践目标**：定位「运算符写死」与「续写钩子」两处，为 4.3 对比 arth 做准备。
2. **操作步骤**：在 [multiply.py:69-73](https://github.com/Jiayi-Pan-TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/examples/data_preprocess/multiply.py#L69-L73) 中找到运算符 `*` 出现的位置；再找到末尾的 `<think>`。
3. **需要观察的现象**：运算符是字符串字面量，无法从样本字段控制；末尾 `<think>` 后没有任何字符。
4. **预期结果**：你能指出「如果把 multiply 改成支持加减乘，至少要动模板里这一处字面量」——这正是 arth.py 用 `{op}` 变量替代它的原因（见 4.3.3）。
5. 运行结果：**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 prompt 要以未闭合的 `<think>` 结尾，而不是闭合的 `</think>`？
**答案**：因为这是给模型的「填空」起始点。模型续写时从 `<think>` 之后开始，自然先产出推理内容；若以 `</think>` 结尾，模型会跳过推理直接写 `<answer>`，失去链式推理的训练信号。

**练习 2**：prompt 里「例示」那一行 `<think> reasoning process here </think> <answer> RESULT_NUMBER </answer>` 起什么作用？
**答案**：它是 few-shot 式的格式示范，告诉模型「推理放 `<think>` 里、最终数字放 `<answer>` 里」。奖励函数 `reward_score/multiply.py` 正是靠解析 `<answer>...</answer>` 来判分的，所以这个格式约定与奖励函数强耦合（同 countdown）。

---

### 4.3 arth.py 的 gen_dataset：多运算支持与减法非负处理

#### 4.3.1 概念说明

`arth.py`（arithmetic）的野心比 multiply 大：它的 `gen_dataset` 设计上能生成 `+ - *` 三种运算，每条样本多带一个 `operation` 字段，因此返回的是**四元组** `(num1, num2, result, op)`。但仓库里它**默认只配置成乘法**——这是本讲最值得注意的「设计 vs 出厂配置」落差。同时，为了让减法结果不出现负数，它在减法分支里交换两个数。

#### 4.3.2 核心流程

```text
seed(1)
operations = ['*']                 # 出厂配置：只有乘法（注释里有 ['*','+','-','*','*']）
for _ in range(N):
    num1 = get_random_num()
    num2 = get_random_num()
    op = choice(operations)        # 从运算符池里随机挑一个
    if op == '*':  result = num1 * num2
    elif op == '+': result = num1 + num2
    else:  # op == '-'
        if num1 < num2:            # 保证 num1 >= num2
            num1, num2 = num2, num1
        result = num1 - num2       # 结果非负
    equations.append((num1, num2, result, op))
return equations
```

两个关键差异：① `op` 来自 `choice(operations)`，multiply 里没有这一步；② 减法有交换逻辑。

#### 4.3.3 源码精读

运算符池（注意第 41 行被注释掉的「丰富版」）：

[arth.py:41-42 — operations 默认硬编码为 ['*']，上一行注释展示了含 + - * 的设计意图](https://github.com/Jiayi-Pan-TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/examples/data_preprocess/arth.py#L41-L42)

随机选运算符并按运算符算结果（含减法交换）：

[arth.py:62-75 — choice(op) 选运算符；减法分支交换 num1/num2 保证非负；append 四元组](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/examples/data_preprocess/arth.py#L62-L75)

减法非负处理（本讲实践任务的核心）：

[arth.py:69-74 — 减法：若 num1<num2 则交换，再相减，确保 result>=0](https://github.com/Jiayi-Pan-TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/examples/data_preprocess/arth.py#L69-L74)

> 为什么坚持非负？因为 prompt 模板写的是 `<answer> RESULT_NUMBER </answer>`，奖励函数用 `int(final_answer)` 解析。虽然 `int()` 能解析负号，但负数运算对 3B 基座模型更难、也更容易和「减号/负号」的 token 混淆。把结果限制在非负整数，能让任务更干净、奖励信号更稳。

`get_random_num` 的一个细节——`r` 的取值范围与 multiply 不同：

[arth.py:46 — `r = randint(1,3)`，r 只能是 1/2/3](https://github.com/Jiayi-Pan-TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/examples/data_preprocess/arth.py#L46)

> 这意味着 [arth.py:47-50 的 `r == 0` 分支（「少 2 位」）实际是死代码](https://github.com/Jiayi-Pan-TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/examples/data_preprocess/arth.py#L47-L50)，永远不会进入。所以 arth 生成的数字最少也是「少 1 位」段（DIGIT=3 时即 0~99），比（修正后的）multiply 少了一些个位数样本。这是两个脚本一个隐蔽的行为差异。

`make_prefix` 的差异——运算符来自数据字段 `{op}`，不再是字面量：

[arth.py:79-84 — make_prefix 读取 dp['operation']，模板用 {num1} {op} {num2}](https://github.com/Jiayi-Pan-TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/examples/data_preprocess/arth.py#L79-L84)

对比 multiply（4.2.3）可以看出三处不同：① 运算符从字面量 `*` 变成变量 `{op}`；② 题目结尾从 `=` 变成 `.`；③ 助手开场白从 "Ok let me think about it." 变成 "Let me solve this step by step."。但**末尾同样以未闭合的 `<think>` 结尾**，续写机制完全一致。

`data_source` 命名与 multiply 的对照：

[arth.py:93 — data_source='yolo/arithmetic-3_digit'（multiply 是 'yolo/multiply-3_digit'）](https://github.com/Jiayi-Pan-TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/examples/data_preprocess/arth.py#L93)

`to_dataset` 多了一个 `operation` 列（multiply 只有 3 列）：

[arth.py:134-146 — to_dataset 构建 num1/num2/result/operation 四列](https://github.com/Jiayi-Pan-TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/examples/data_preprocess/arth.py#L134-L146)

#### 4.3.4 代码实践

这是本讲主实践的前半部分（完整版见第 5 节）。

1. **实践目标**：验证「让 arth 生成加减乘」只需改一行，且 `gen_dataset` 已为三种运算准备好了计算逻辑。
2. **操作步骤**：把 [arth.py:42](https://github.com/Jiayi-Pan-TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/examples/data_preprocess/arth.py#L42) 的 `operations = ['*']` 改成 `operations = ['*', '+', '-']`（或恢复第 41 行注释里的 `['*', '+', '-', '*', '*']`）。
3. **需要观察的现象**：改完后 `choice(operations)` 会随机抽到 `+` 或 `-`；当抽到 `-` 且 `num1 < num2` 时，[第 72-73 行的交换](https://github.com/Jiayi-Pan-TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/examples/data_preprocess/arth.py#L72-L73) 会触发，保证 `result` 非负。
4. **预期结果**：生成出的四元组里 `op` 会混合出现 `* + -`，且所有 `result` 都 ≥ 0；`make_prefix` 因为读 `dp['operation']`，prompt 里会正确显示对应运算符。
5. 运行结果：**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么把 `operations` 改成含 `+ - *` 之后，不需要改 `make_prefix`？
**答案**：因为 arth 的 `make_prefix` 用 `{op}` 变量从 `dp['operation']` 取运算符，而 `to_dataset` 已经把 `operation` 列写进了数据。运算符变了，prompt 自动跟着变。

**练习 2**：如果把减法分支的交换逻辑（第 72-73 行）删掉，会出现什么问题？
**答案**：当 `num1 < num2` 时 `result` 为负数。负数答案会让任务变难，且与「`<answer> RESULT_NUMBER </answer>`」的格式约定产生摩擦（负号 token）。奖励函数 `int()` 仍能解析，但训练信号会变嘈杂。

**练习 3**：`arth.py` 的 `get_random_num` 用 `randint(1,3)`，那 `r==0` 分支还有用吗？
**答案**：没用，是死代码（见 4.3.3）。`r` 永远不会等于 0，所以「少 2 位」的小数字段在 arth 里从不采样。

---

### 4.4 统一管线：从样本列表到 parquet 与 data_source 奖励路由

#### 4.4.1 概念说明

两个脚本的 `__main__` 几乎是同一套模板：`gen_dataset` 产出元组列表 → 去重 → 划分 train/test → `to_dataset` 转成 HuggingFace `Dataset` → `.map(make_map_fn)` 给每条样本贴上 prompt/`data_source`/`reward_model` 等字段 → `to_parquet` 写盘。理解这条管线，就能把任意自造任务塞进 veRL 的训练入口。最后还要确认：写进 parquet 的 `data_source` 字符串，在训练时会被路由到哪个奖励函数。

#### 4.4.2 核心流程

```text
dataset = gen_dataset(...)                 # 元组列表
dataset = list(set(dataset))               # 去重
assert len(dataset) > TRAIN_SIZE + TEST_SIZE
train, test = dataset[:TRAIN], dataset[-TEST:]
train = to_dataset(train); test = to_dataset(test)        # → Dataset.from_dict
train = train.map(make_map_fn('train'), with_indices=True)
test  = test.map(make_map_fn('test'),  with_indices=True)
train.to_parquet(.../train.parquet); test.to_parquet(.../test.parquet)
# 可选：copy 到 HDFS
```

`make_map_fn` 返回的 `process_fn` 负责把每条样本组装成 veRL 期望的字段：

```python
data = {
    "data_source": data_source,            # 路由奖励函数的键
    "prompt": [{"role": "user", "content": question}],
    "ability": "math",
    "reward_model": {"style": "rule", "ground_truth": solution},
    "extra_info": {"split": split, "index": idx},
}
```

#### 4.4.3 源码精读

multiply 的 `make_map_fn`/`process_fn`（arth 版本字段结构完全相同，只是 `to_dataset` 多一列）：

[multiply.py:98-121 — make_map_fn/process_fn：组装 data_source/prompt/reward_model/extra_info](https://github.com/Jiayi-Pan-TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/examples/data_preprocess/multiply.py#L98-L121)

multiply 的 `to_dataset`（3 列）与 arth 的 `to_dataset`（4 列，多 `operation`）：

[multiply.py:123-133 — to_dataset：num1/num2/result 三列](https://github.com/Jiayi-Pan-TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/examples/data_preprocess/multiply.py#L123-L133)

写盘与可选 HDFS 拷贝（两脚本一致）：

[multiply.py:141-150 — to_parquet 写盘，可选 makedirs+copy 到 HDFS](https://github.com/Jiayi-Pan-TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/examples/data_preprocess/multiply.py#L141-L150)

最后是关键的奖励路由——`main_ppo.py` 里这样选奖励函数：

[main_ppo.py:24-32 — _select_rm_score_fn：按 data_source 字符串路由](https://github.com/Jiayi-Pan-TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/main_ppo.py#L24-L32)

第 29-30 行是本讲的核心结论：

[main_ppo.py:29-30 — "multiply" 或 "arithmetic" 都返回 multiply.compute_score](https://github.com/Jiayi-Pan-TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/main_ppo.py#L29-L30)

也就是说，`yolo/multiply-3_digit` 和 `yolo/arithmetic-3_digit` **共用同一个奖励函数**。而这个函数只做一件事——比较 `<answer>` 里的整数与 ground_truth：

[reward_score/multiply.py:27-58 — compute_score：只判 int(answer)==int(ground_truth)，与运算符无关](https://github.com/Jiayi-Pan-TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/reward_score/multiply.py#L27-L58)

> 结论：奖励函数对运算符完全无感。所以你在 arth.py 里把 `operations` 扩成 `+ - *`，**不需要改任何奖励代码**——只要模型在 `<answer>` 里输出正确的整数，就能拿到分。

#### 4.4.4 代码实践

1. **实践目标**：亲手确认「数据生成 → 路由」这条链路对运算符扩展是透明的。
2. **操作步骤**：
   - 在 [main_ppo.py:29](https://github.com/Jiayi-Pan-TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/main_ppo.py#L29) 确认 `"arithmetic" in data_source` 命中后返回 `multiply.compute_score`。
   - 在 [reward_score/multiply.py:51](https://github.com/Jiayi-Pan-TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/reward_score/multiply.py#L51) 确认判分只看 `int(answer) == int(ground_truth)`。
3. **需要观察的现象**：整条判分逻辑里没有任何 `op`、`operation`、`* + -` 的字样。
4. **预期结果**：你能向别人解释「arithmetic 任务即便混合加减乘，奖励函数一行都不用改」。
5. 运行结果：**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`to_dataset` 在 multiply 和 arth 里的列数不同（3 列 vs 4 列），这会影响 `make_map_fn` 吗？
**答案**：不会。`make_map_fn` 的 `process_fn` 只从 `example` 里取它需要的字段（`make_prefix` 用到的 num1/num2/operation、以及 result 作为 ground_truth）。多出的列只是被 `.map` 原样保留，不干扰 prompt 组装。

**练习 2**：为什么两个脚本都用 `list(set(dataset))` 去重？
**答案**：随机生成的 `(num1, num2, ...)` 元组可能重复。去重避免同一道题在训练集里出现多次，影响数据多样性；同时也让 train/test 划分更干净。

**练习 3**：`data_source` 里的 `yolo/` 前缀和 `-3_digit` 后缀分别传达了什么？
**答案**：`yolo/` 是命名空间前缀（veRL 示例项目用的标签）；`-3_digit` 对应 `DIGIT=3`，标明题目里数字的最大位数，方便人眼区分难度。但路由只看字符串里是否包含 `"multiply"` 或 `"arithmetic"` 子串（见 main_ppo.py:29），前缀后缀本身不参与匹配。

## 5. 综合实践

把本讲内容串起来，完成下面这个端到端小任务（数据侧，无需 GPU）。

**任务**：让 arithmetic 任务真正生成「加、减、乘」混合题目，并验证奖励链路无需改动。

1. **实践目标**：把 4.3 的单点改动扩展成一次完整的数据再生成 + 路由核验。
2. **操作步骤**：
   - 第 1 步——改运算符池：把 [arth.py:42](https://github.com/Jiayi-Pan-TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/examples/data_preprocess/arth.py#L42) 的 `operations = ['*']` 改为 `operations = ['*', '+', '-']`。
   - 第 2 步——回顾减法处理：确认 [arth.py:69-74](https://github.com/Jiayi-Pan-TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/examples/data_preprocess/arth.py#L69-L74) 在 `num1 < num2` 时交换两者，保证减法 `result` 非负。在你的改动里，这段逻辑**不需要动**——它已经为 `-` 准备好了。
   - 第 3 步——回顾 prompt：确认 [arth.py:79-84](https://github.com/Jiayi-Pan-TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/examples/data_preprocess/arth.py#L79-L84) 的 `make_prefix` 读 `dp['operation']`，运算符会自动出现在 prompt 里，也**不需要动**。
   - 第 4 步——回顾奖励路由：确认 [main_ppo.py:29-30](https://github.com/Jiayi-Pan-TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/main_ppo.py#L29-L30) 把 arithmetic 路由到 `multiply.compute_score`，而 [reward_score/multiply.py:51](https://github.com/Jiayi-Pan-TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/reward_score/multiply.py#L51) 只比较整数答案，**奖励代码也不需要动**。
   - 第 5 步（可选运行）：执行 `python examples/data_preprocess/arth.py --local_dir ~/data/arithmetic-3_digit`，再用 pandas/pyarrow 读回 `train.parquet`，抽查几行看 `operation` 是否混合、`result` 是否都非负。
3. **需要观察的现象**：改动的只有 `operations` 这一行；prompt、减法交换、奖励函数都因为「已经预留好变量/逻辑」而无需触碰。这正体现了 arth.py「设计支持多运算、出厂只开乘法」的结构。
4. **预期结果**：生成数据中 `op` 列出现 `* + -` 三种值，所有 `result ≥ 0`，prompt 里运算符与 `op` 列一致。
5. 运行结果：**待本地验证**（脚本运行依赖 `datasets`、`tqdm` 等库；且本仓库 `multiply.py` 存在 `randint(,3)` 的语法问题，但 `arth.py` 本身语法合法，可独立运行）。

**附加解释题**（对应学习目标里的「`<think>` 标签开头的用意」）：`make_prefix` 末尾的未闭合 `<think>` 是「续写钩子」——它让模型从「接着写推理」开始生成，先产出思维链再给 `<answer>`，这是 R1 Zero 路线强制链式推理的关键设计，也是奖励函数能稳定解析 `<answer>` 的前提（详见 4.2.5）。

## 6. 本讲小结

- `multiply.py` 与 `arth.py` 共享同一套「按位数分层抽样」的 `get_random_num`，靠 `DIGIT` 和 `LESS_OR_EQUAL` 控制数字范围。
- `multiply.py` 只生成乘法，三元组 `(num1, num2, result)`，prompt 里 `*` 写死；`arth.py` 设计上支持 `+ - *`，四元组多一个 `operation`，prompt 用 `{op}` 变量。
- `arth.py` 出厂配置 `operations = ['*']`，所以默认行为和 multiply 一样；真正开启加减乘只需改这一行。
- 减法分支通过 `if num1 < num2: num1, num2 = num2, num1` 保证结果非负，避免负数答案带来的复杂度。
- 两个任务的 `data_source`（`yolo/multiply-3_digit`、`yolo/arithmetic-3_digit`）都路由到同一个 `multiply.compute_score`，该奖励只比整数答案、与运算符无关——所以扩展运算符不必改奖励。
- 阅读真实源码时要警惕注释陈旧与潜在 bug：multiply.py:40 的 `randint(,3)` 是语法错误，arth.py:46 的 `randint(1,3)` 使 `r==0` 分支成为死代码。

## 7. 下一步学习建议

本讲只讲了「数据怎么来」，还没有讲「数据怎么被加载进训练」。建议：

- 接着学 **u2-l3（RLHFDataset 数据加载与 tokenization）**，看 parquet 里的 `prompt`/`reward_model.ground_truth` 如何被 tokenize 成 `input_ids`/`attention_mask`/`position_ids`，补齐数据侧最后一公里。
- 若想了解奖励函数内部的「提取-校验-打分」细节（`format_score=0.1` 的分级打分），可直接学 **u2-l4（规则奖励函数）**，其中会精读 `reward_score/countdown.py`，与本讲引用的 `reward_score/multiply.py` 是同一套打分范式。
- 读源码时可以带着本讲留下的两个「真实仓库瑕疵」去验证：亲手运行 `python -c "import examples.data_preprocess.multiply"` 看是否真的报语法错误，并确认 arth 的 `r==0` 分支是否真的不触发——这是把「读源码」变成「信源码」的好练习。
