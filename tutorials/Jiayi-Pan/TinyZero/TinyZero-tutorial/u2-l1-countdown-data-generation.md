# Countdown 任务：数据生成与 prompt 模板

## 1. 本讲目标

在上一单元你已经「跑通了一次 countdown 训练」，知道训练入口是 `python3 -m verl.trainer.main_ppo`，也知道数据必须先被打包成 **parquet** 文件。但那个 parquet 是怎么来的？里面到底有哪些字段？模型看到的 prompt 长什么样？

本讲带你看懂 TinyZero 仓库里「真正属于它自己的代码」之一：数据预处理脚本 `examples/data_preprocess/countdown.py`。读完本讲你应当能够：

- 说清楚 countdown 任务是什么，以及一条样本由哪些随机参数决定。
- 看懂 `gen_dataset`、`make_prefix`、`make_map_fn` 三个函数各自的职责与它们之间的调用关系。
- 理解 `<think>` / `<answer>` 标签为什么是 prompt 的核心，以及为什么改 prompt 必须同步改奖励函数。
- 读懂最终 parquet 里 `data_source`、`prompt`、`reward_model.ground_truth`、`extra_info` 等字段分别承担什么角色。

## 2. 前置知识

在进入源码前，先建立两个直觉。

**(1) 强化学习需要「精确、即时、自动」的奖励信号。** 很多任务（写诗、聊天）很难自动打分；而 countdown（凑数游戏）天然可验证——给定目标数字和一组可用数字，模型给出的算式要么算出来等于目标，要么不等于。这种「能机器判对错」的任务最适合做 RL。这也是 TinyZero 选择 countdown 与 multiplication 两类任务的根本原因。

**(2) RL 训练循环对数据格式有固定要求。** veRL 的训练循环（下一单元会精读）从 parquet 里读取每一条样本时，期望看到一组**约定好的字段**：模型要续写的 `prompt`、用于校验答案的 `reward_model.ground_truth`、以及用来「路由到对应奖励函数」的 `data_source`。数据预处理脚本的全部工作，就是把任务本身的原始数据，翻译成这一套固定格式。

> 名词速查
> - **parquet**：一种列式存储的表格文件格式，HuggingFace `datasets` 库原生支持读写，是 veRL 数据加载的标准载体。
> - **prompt（提示）**：喂给语言模型、让它续写的输入文本。
> - **token**：语言模型处理文本的最小单位，一个词或子词通常对应一个 token。
> - **ground truth（标准答案）**：训练时用来核对模型输出是否正确的参考信息。注意 countdown 的 ground truth 不是「唯一正确算式」，而是 `target + 可用数字`——满足条件的算式可能有很多种。

## 3. 本讲源码地图

本讲只围绕一个核心文件，外加一个辅助文件：

| 文件 | 作用 |
| --- | --- |
| [examples/data_preprocess/countdown.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/examples/data_preprocess/countdown.py) | countdown 任务的数据预处理主脚本：生成/读取样本、构造 prompt、写出 parquet。本讲的全部重点。 |
| [verl/utils/hdfs_io.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/hdfs_io.py) | 兼容本地与 HDFS 的文件读写工具，提供 `makedirs` / `copy`，用于把生成好的 parquet 拷到分布式存储。 |
| [verl/utils/reward_score/countdown.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/reward_score/countdown.py) | （下游，本讲只引用）countdown 的规则奖励函数，与 prompt 模板强耦合。 |
| [verl/utils/dataset/rl_dataset.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/dataset/rl_dataset.py) | （下游，本讲只引用）`RLHFDataset`，训练时真正读取 parquet 的加载器，定义了字段约定。 |

调用关系一览（建议先记住这张「数据流」图）：

```
原始任务数据  →  make_prefix()  →  make_map_fn().process_fn()  →  parquet 文件
(target,nums)    构造 prompt        组装成约定字段结构           train/test.parquet
                                                              ↓
                                                    (训练时) RLHFDataset 读取
                                                              ↓
                                                    (训练时) 按 data_source 路由到 compute_score
```

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：样本生成 `gen_dataset`、prompt 模板 `make_prefix`、parquet 组装 `make_map_fn`。

---

### 4.1 gen_dataset：countdown 样本是怎么造出来的

#### 4.1.1 概念说明

**countdown 任务**的规则是：给你一个目标数 `target` 和 N 个可用数字 `numbers`，要求你用基本算术运算（`+ - * /`）和这些数字（每个最多用一次）拼出一个等于 `target` 的算式。

例如 `target = 24`、`numbers = [3, 8, 3, 8]`，一个合法解是 `8 / (3 - 8/3) = 24`。

`gen_dataset` 这个函数回答的问题是：「这样的一道题，里面的 target 和 numbers 该怎么随机生成？」它接受一组参数，返回一个 `[(target, numbers), ...]` 的列表。

> 重要提醒：在本仓库的实际运行流程里，`gen_dataset` **并没有被调用**。主程序直接用 `load_dataset('Jiayi-Pan/Countdown-Tasks-3to4')` 读取了一份已经造好的 HuggingFace 数据集（见 4.1.3）。`gen_dataset` 更像是一份「数据生成器参考实现」——它用代码记录了这份数据集当初是按什么参数造出来的（6 个数字、target 上限 1000、数字范围 1~100、随机种子 42）。理解它，等于理解了数据集的「出厂规格」。

#### 4.1.2 核心流程

`gen_dataset` 的逻辑非常直接，用伪代码描述：

```
固定随机种子 seed_value（保证可复现）
for i in 1..num_samples:
    target  ← 在 [1, max_target] 内随机取一个整数
    numbers ← 在 [min_number, max_number] 内随机取 num_operands 个整数
    记录 (target, numbers)
返回所有样本
```

两个值得注意的细节：

- **随机性来自均匀分布**。target 服从 \(\text{Uniform}\{1,\text{max\_target}\}\)，每个 number 服从 \(\text{Uniform}\{\text{min\_number},\text{max\_number}\}\)。
- **函数签名里有 `operations` 参数，但函数体里并没有用到它**。也就是说 `gen_dataset` 只生成「题目」（target + numbers），并不生成「标准答案 solution」——尽管它的返回类型注释写着 `(target, numbers, solution)`。这是有意为之：RL 训练不需要人类标准答案，只需要能自动判分的规则（参见 4.2）。原始仓库里 `operations` 参数目前更像是一个「占位/计划中」的字段。

#### 4.1.3 源码精读

[examples/data_preprocess/countdown.py:15-51](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/examples/data_preprocess/countdown.py#L15-L51) 定义了 `gen_dataset`，其中关键几行：

```python
seed(seed_value)                                   # 固定随机种子，结果可复现
for _ in tqdm(range(num_samples)):
    target = randint(1, max_target)                # target ∈ [1, 1000]
    numbers = [randint(min_number, max_number)
               for _ in range(num_operands)]       # 6 个 ∈ [1, 100] 的数字
    samples.append((target, numbers))
```

而真正被主流程使用的，是 [examples/data_preprocess/countdown.py:88-92](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/examples/data_preprocess/countdown.py#L88-L92)，它**绕过了 `gen_dataset`**：

```python
raw_dataset = load_dataset('Jiayi-Pan/Countdown-Tasks-3to4', split='train')
train_dataset = raw_dataset.select(range(TRAIN_SIZE))
test_dataset  = raw_dataset.select(range(TRAIN_SIZE, TRAIN_SIZE + TEST_SIZE))
```

这里 `TRAIN_SIZE` 默认 `327680`、`TEST_SIZE` 默认 `1024`（见 [examples/data_preprocess/countdown.py:78-79](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/examples/data_preprocess/countdown.py#L78-L79)），即从已有数据集里切出训练集与测试集。

#### 4.1.4 代码实践

实践目标：亲手验证 `gen_dataset` 的随机生成逻辑，并确认它确实独立于主流程。

操作步骤：

1. 在项目根目录启动已装好依赖的 conda 环境。
2. 新建一个临时脚本（**示例代码，非项目原有文件**），直接 import 并调用 `gen_dataset`：

```python
# 示例代码
from examples.data_preprocess.countdown import gen_dataset
samples = gen_dataset(num_samples=3, seed_value=42)
for i, (target, numbers) in enumerate(samples):
    print(i, "target =", target, "| numbers =", numbers)
```

3. 再读一遍 [examples/data_preprocess/countdown.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/examples/data_preprocess/countdown.py) 的 `if __name__ == '__main__':` 段，用搜索确认 `gen_dataset` 在 `__main__` 中**没有任何调用点**。

需要观察的现象：

- 同一个 `seed_value=42` 每次运行输出完全一致（可复现）。
- 每个 `target` 落在 `[1, 1000]`，每个数字落在 `[1, 100]`，`numbers` 长度恒为 6。

预期结果：3 条 `(target, numbers)` 样本被打印；`gen_dataset` 在主流程中无调用点。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `gen_dataset` 要在函数开头调用 `seed(seed_value)`？
**答案**：固定随机数发生器的种子，让「同样的参数 → 同样的样本序列」，保证数据集可复现、实验可对比。

**练习 2**：`gen_dataset` 的返回类型注释写的是 `List[Tuple]`、元素含 `solution`，但实际只 append 了 `(target, numbers)`。这矛盾说明什么？
**答案**：说明 `solution`（标准答案）是设计上打算支持、但最终没有实现/不需要的字段。countdown 靠规则奖励函数自动判分，不需要预置人类答案，因此只保留 `target` 和 `numbers` 两项即可。

---

### 4.2 make_prefix：把题目翻译成模型能读的 prompt

#### 4.2.1 概念说明

`gen_dataset` 产出的是结构化数据 `(target, numbers)`，但语言模型只认文本。`make_prefix` 负责把它们拼成一段**自然语言指令**，并在结尾留下「让模型续写」的位置。

它做了三件关键的事：

1. **明确任务**：用一句话讲清楚「用这些数字、这些运算、凑出目标」的规则。
2. **约定输出格式**：要求模型把推理过程写在 `<think> ... </think>` 标签里，把最终答案写在 `<answer> ... </answer>` 标签里。这个格式约定是后续**奖励函数能自动提取答案**的前提。
3. **开放续写口**：prompt 以未闭合的 `<think>` 结尾——即「我已经开始想了，请继续」。这样模型生成时自然从「推理过程」开始写。

`make_prefix` 提供两种「模板」（`template_type`）：

| `template_type` | 适用模型 | 形式 |
| --- | --- | --- |
| `base` | 任意 base 基座模型 | 纯文本对话（`User:` / `Assistant:`） |
| `qwen-instruct` | Qwen Instruct 模型 | 使用 `<|im_start|>` / `<|im_end|>` 特殊 token 的对话格式 |

TinyZero 默认用 `base`（因为它的实验对象是 Qwen2.5 **base** 模型，不是 instruct 微调版）。

#### 4.2.2 核心流程

```
make_prefix(dp, template_type):
    target  ← dp['target']
    numbers ← dp['nums']
    若 template_type == 'base':
        返回 "A conversation between User and Assistant... User: 用 {numbers} 凑 {target}...
              要求用 <think></think> 和 <answer></answer>... Assistant: ... <think>"
    若 template_type == 'qwen-instruct':
        返回 用 <|im_start|> 包装的同义指令，同样以 <think> 结尾
```

两种模板**指令内容等价**，差别只在「对话外壳」。真正决定任务语义和奖励可提取性的，是这段固定文字：

```
Show your work in <think> </think> tags. And return the final answer in <answer> </answer> tags,
for example <answer> (1 + 2) / 3 </answer>.
```

#### 4.2.3 源码精读

[examples/data_preprocess/countdown.py:53-66](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/examples/data_preprocess/countdown.py#L53-L66) 是 `make_prefix` 全文。注意第 56 行那句注释——它是本讲最重要的一个提醒：

```python
def make_prefix(dp, template_type):
    target = dp['target']
    numbers = dp['nums']
    # NOTE: also need to change reward_score/countdown.py      # ← 关键注释
    if template_type == 'base':
        prefix = f"""A conversation between User and Assistant. ...
User: Using the numbers {numbers}, create an equation that equals {target}. ...
Show your work in <think> </think> tags. And return the final answer in <answer> </answer> tags, ...
Assistant: Let me solve this step by step.
<think>"""                                     # ← 注意：以未闭合的 <think> 结尾
    elif template_type == 'qwen-instruct':
        prefix = f"""<|im_start|>system\n...<|im_end|>\n<|im_start|>user\n ... <im_end|>\n<|im_start|>assistant\n...<think>"""
    return prefix
```

为什么这句注释这么重要？因为奖励函数 [verl/utils/reward_score/countdown.py:7-25](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/reward_score/countdown.py#L7-L25) 的 `extract_solution` 是**靠 prompt 的格式约定来定位答案**的：

```python
def extract_solution(solution_str):
    # 先用 "Assistant:" 或 "<|im_start|>assistant" 把 prompt 和模型回答切开
    if "Assistant:" in solution_str: ...
    elif "<|im_start|>assistant" in solution_str: ...
    else: return None
    # 再用正则匹配最后一个 <answer>...</answer>
    answer_pattern = r'<answer>(.*?)</answer>'
    ...
```

也就是说：prompt 里用了 `Assistant:` 分隔符、要求 `<answer>` 标签，奖励函数才能正确切分、提取。**一旦你改了 prompt 的这些约定（比如换成中文标签 `<答案>`），却不改 `reward_score/countdown.py` 里的对应字符串，奖励函数就会提取失败，所有样本都拿 0 分，训练直接崩。** 这正是那句 `NOTE` 想警告你的。

#### 4.2.4 代码实践

实践目标：仿照 `base` 模板，新增一个中文模板 `qwen2.5-base-zh`，并解释为什么必须同步改奖励函数。

操作步骤（这是「源码阅读 + 局部修改」型实践，**不会真的运行训练**）：

1. 在 [examples/data_preprocess/countdown.py:53-66](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/examples/data_preprocess/countdown.py#L53-L66) 的 `make_prefix` 里，紧跟 `qwen-instruct` 分支之后，新增一个分支（**示例代码**，可在本地副本上修改练习，不要提交到源码）：

```python
# 示例代码
elif template_type == 'qwen2.5-base-zh':
    prefix = f"""用户与助手之间的对话。用户提问，助手解答。助手先在脑中思考推理过程，再把答案给用户。
用户：请用数字 {numbers} 构造一个等于 {target} 的等式。你可以使用基本算术运算（+、-、*、/），每个数字只能用一次。请把推理过程放在 <think> </think> 标签中，并把最终答案放在 <answer> </answer> 标签中返回，例如 <answer> (1 + 2) / 3 </answer>。
助手：让我一步步来解决。
<think>"""
```

2. 同时把 `template_type` 新增的可选项，在 [examples/data_preprocess/countdown.py:80](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/examples/data_preprocess/countdown.py#L80) 的 argparse 默认值注释里说明（可选）。
3. 打开 [verl/utils/reward_score/countdown.py:7-25](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/reward_score/countdown.py#L7-L25)，检查 `extract_solution` 的分隔逻辑。

需要观察/思考的现象：

- 中文模板里我**保留了** `助手：` 后面紧跟 `<think>`，并且**保留了** `<answer>...</answer>` 标签和 example。
- 那么 `extract_solution` 还能正常工作吗？它会先用 `"Assistant:"` 切分——但中文模板里是「助手：」不是 `Assistant:`！

预期结论（待本地验证）：若中文模板把 `Assistant:` 也换成了中文「助手：」，则 `extract_solution` 第 10-15 行的两个分支都不命中、直接 `return None`，奖励恒为 0。修复方法有二选一：要么在 prompt 里保留英文 `Assistant:` 标记（推荐，最小改动），要么在 `extract_solution` 里新增对「助手：」的切分分支。这就用实例回答了「为什么改 prompt 必须同步改 reward 函数」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 prompt 要以**未闭合的 `<think>`** 结尾，而不是闭合的 `<think></think>`？
**答案**：因为这是「续写」任务——prompt 是给模型的输入，末尾的 `<think>` 表示「推理过程已开始，请接着写」。若闭合，模型会以为思考部分已结束、直接跳到 `<answer>`，失去自由推理的空间。

**练习 2**：`base` 与 `qwen-instruct` 两个模板的**任务描述文字几乎一样**，区别在哪里？为什么需要两套？
**答案**：区别在对话外壳：`base` 用纯文本 `User:/Assistant:`，适配没经过指令微调的基座模型；`qwen-instruct` 用 `<|im_start|>/<|im_end|>` 这类模型专属特殊 token，适配 Qwen Instruct 系列。两套存在是为了让同一份数据能服务于不同基座/指令模型。

---

### 4.3 make_map_fn：把 prompt 装进 parquet 的字段结构

#### 4.3.1 概念说明

有了 prompt 文本，还差最后一步：把它和「判分所需的参考信息」一起，组装成 veRL 训练循环**期望读取的字段结构**，再写成 parquet。这一步由 `make_map_fn` 返回的 `process_fn` 完成。

`make_map_fn` 是一个**高阶函数**（返回函数的函数）：它接收一个 `split`（`'train'` 或 `'test'`），返回一个针对该划分的 `process_fn`。这种写法的好处是：训练集和测试集走完全相同的字段组装逻辑，只是 `extra_info.split` 字段不同，避免重复代码。

#### 4.3.2 核心流程

每条样本经 `process_fn` 处理后，会变成这样一个字典（即 parquet 的一行）：

```
{
  data_source:   "countdown"                     # 任务名，训练时据此路由奖励函数
  prompt:        [ {role:"user", content: <上面拼好的 prompt 文本>} ]
  ability:       "math"
  reward_model:  { style:"rule",                 # 用「规则」而非神经网络 RM 打分
                   ground_truth: { target, numbers } }   # 判分参考
  extra_info:    { split:"train"/"test", index: <行号> }
}
```

关键字段逐个解释：

| 字段 | 作用 | 下游谁用它 |
| --- | --- | --- |
| `data_source` | 任务标识字符串，这里是 `'countdown'` | `main_ppo._select_rm_score_fn` 据此选 `compute_score` |
| `prompt` | 模型续写用的输入（chat 格式，role+content） | `RLHFDataset` tokenize 后喂给 rollout 引擎生成 |
| `reward_model.ground_truth` | 判分参考 `{target, numbers}` | 传给 `compute_score` 与模型输出比对 |
| `reward_model.style` | `"rule"` 表示用规则奖励 | 告诉 trainer 这是规则打分，不调神经网络 RM |
| `extra_info` | 调试用元信息（划分、行号） | 日志、抽样检查 |

整张表读下来你会明白：**`data_source` 和 `ground_truth` 是数据与训练系统之间的两根「接口线」**——前者决定「用哪个奖励函数」，后者决定「拿什么校验答案」。

最后，写出文件（[examples/data_preprocess/countdown.py:126-131](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/examples/data_preprocess/countdown.py#L126-L131)）：

```
train_dataset.to_parquet(local_dir/train.parquet)
test_dataset.to_parquet(local_dir/test.parquet)
若指定了 hdfs_dir:
    makedirs(hdfs_dir)            # 来自 verl.utils.hdfs_io
    copy(src=local_dir, dst=hdfs_dir)
```

这里的 `makedirs` / `copy` 来自 [verl/utils/hdfs_io.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/hdfs_io.py)。它们是对 `os.makedirs` / `shutil.copy` 的薄封装：当路径以 `hdfs://` 开头时走 HDFS 命令行，否则退化为本地文件操作（见 [verl/utils/hdfs_io.py:50-72](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/hdfs_io.py#L50-L72) 与 [verl/utils/hdfs_io.py:84-110](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/hdfs_io.py#L84-L110)）。所以本地用户即使不配 HDFS，`hdfs_dir=None` 时这两行根本不会执行，脚本完全可用。

#### 4.3.3 源码精读

[examples/data_preprocess/countdown.py:94-118](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/examples/data_preprocess/countdown.py#L94-L118) 是 `make_map_fn` 与 `process_fn` 全文：

```python
def make_map_fn(split):
    def process_fn(example, idx):
        question = make_prefix(example, template_type=args.template_type)  # 复用 4.2 的模板
        solution = {"target": example['target'], "numbers": example['nums']} # 判分参考
        data = {
            "data_source": data_source,                  # 'countdown'
            "prompt": [{"role": "user", "content": question}],
            "ability": "math",
            "reward_model": {"style": "rule", "ground_truth": solution},
            "extra_info": {'split': split, 'index': idx},
        }
        return data
    return process_fn
```

注意两点：

- `example['target']` 和 `example['nums']` 这两个键名，来自上游 HuggingFace 数据集 `Jiayi-Pan/Countdown-Tasks-3to4` 的列名。`make_prefix` 里读的也是 `dp['target']`、`dp['nums']`——两边键名必须一致。
- `process_fn` 带 `idx` 参数，是因为下面用 `with_indices=True` 调用（[examples/data_preprocess/countdown.py:120-121](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/examples/data_preprocess/countdown.py#L120-L121)），让每条样本拿到自己的行号写进 `extra_info.index`。

写 parquet 在 [examples/data_preprocess/countdown.py:126-127](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/examples/data_preprocess/countdown.py#L126-L127)：

```python
train_dataset.to_parquet(os.path.join(local_dir, 'train.parquet'))
test_dataset.to_parquet(os.path.join(local_dir, 'test.parquet'))
```

#### 4.3.4 代码实践

实践目标：用最小代价，看清「一条原始样本 → 一个 parquet 行字典」的完整变换，**不需要下载 HuggingFace 数据集、也不需要 GPU**。

操作步骤（**示例代码**，在本地新建临时脚本运行）：

```python
# 示例代码：绕过 HF 数据集，手动构造一条 example，走完 make_prefix + process_fn
from examples.data_preprocess.countdown import make_prefix

# 模拟一条原始样本（键名必须与 HF 数据集一致：target / nums）
example = {"target": 24, "nums": [3, 8, 3, 8]}

# 第一步：生成 prompt
question = make_prefix(example, template_type="base")
print("=== PROMPT ===")
print(question)

# 第二步：手动复刻 process_fn 的字段组装
data = {
    "data_source": "countdown",
    "prompt": [{"role": "user", "content": question}],
    "ability": "math",
    "reward_model": {"style": "rule",
                     "ground_truth": {"target": example["target"],
                                      "numbers": example["nums"]}},
    "extra_info": {"split": "train", "index": 0},
}
print("\n=== PARQUET ROW ===")
import json
print(json.dumps(data, indent=2, ensure_ascii=False))
```

需要观察的现象：

- 打印出的 `prompt` 文本里 `target`/`numbers` 已被填入，且确实以 `<think>` 结尾。
- `data_source` 为字符串 `"countdown"`，`ground_truth` 含 `target` 与 `numbers` 两个键。

预期结果：得到一个结构完整、可直接 `to_parquet` 的字典。

如果你本地已装好 `datasets` 库且能联网，可以再进一步，按 README 的方式真正生成 parquet（**待本地验证**）：

```bash
# 来自 README
python ./examples/data_preprocess/countdown.py --local_dir {path_to_your_dataset}
```

#### 4.3.5 小练习与答案

**练习 1**：为什么 `make_map_fn` 要设计成「接收 split、返回 process_fn」的高阶函数，而不是直接写一个普通函数？
**答案**：因为训练集与测试集的组装逻辑完全相同，唯一差别是写入 `extra_info.split` 的值。用闭包把 `split` 绑进 `process_fn`，既复用了全部字段拼装代码，又能在 `.map()` 时传一个干净的 `process_fn`，避免重复。

**练习 2**：如果我把 `data_source` 从 `"countdown"` 改成 `"my_countdown"`，但其它什么都不动，训练时会怎样？
**答案**：训练时 `main_ppo._select_rm_score_fn` 会按 `data_source` 路由奖励函数；如果它里面没有 `"my_countdown"` 的分支，就拿不到对应的 `compute_score`，奖励计算会失败或回退到默认处理。所以 `data_source` 必须与奖励函数注册表里的名字严格对应。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个**端到端小任务**（纯源码阅读 + 本地脚本，无需 GPU）：

**任务**：在不下载 HuggingFace 数据集的前提下，自己「造 3 道题」，走完从 `gen_dataset` 到 parquet 行字典的完整链路，并验证 prompt 与字段结构正确。

步骤：

1. 调用 `gen_dataset(num_samples=3, seed_value=42)` 拿到 3 条 `(target, numbers)`。
2. 把每条包装成 `{"target": ..., "nums": [...]}` 的 `example`（注意键名用 `nums`，与 HF 数据集一致）。
3. 对每条调用 `make_prefix(example, "base")` 生成 prompt。
4. 仿照 `process_fn`，组装出含 `data_source`、`prompt`、`reward_model.ground_truth`、`extra_info` 的字典列表。
5. 打印第 1 条的完整字典，人工核对：
   - prompt 是否以 `<think>` 结尾？
   - `ground_truth` 是否含正确的 `target` 和 `numbers`？
   - `data_source` 是否为 `"countdown"`？
6. （进阶）把 `template_type` 换成 `"qwen-instruct"` 再跑一次，对比两个 prompt 的外壳差异。

验收标准：能口述「`(target, numbers)` → `make_prefix` → `process_fn` → parquet 行」这条数据流上每一步的输入和输出，并解释 `data_source` 与 `ground_truth` 在下游训练中分别被谁使用。

> 如果你本地环境齐全，可追加一步：把这 3 条字典包成 `datasets.Dataset`，调用 `.to_parquet('mini.parquet')`，再用 `RLHFDataset` 读取，验证字段能被正确 tokenize（**待本地验证**）。

## 6. 本讲小结

- countdown 任务 = 给定 `target` 和一组 `numbers`，用算术运算凑出目标；它天然可自动判分，是 RL 的理想任务。
- `gen_dataset` 用随机参数（6 个数字、target≤1000、数字 1~100、seed=42）描述样本生成规格，但在实际主流程中**未被调用**——数据来自 `load_dataset('Jiayi-Pan/Countdown-Tasks-3to4')`。
- `make_prefix` 把 `(target, numbers)` 翻译成自然语言 prompt，核心是用 `<think>` / `<answer>` 标签约定输出格式，并以未闭合的 `<think>` 收尾引导续写；`base` 与 `qwen-instruct` 两套模板只差对话外壳。
- prompt 的格式约定与奖励函数 `reward_score/countdown.py` **强耦合**：改 prompt 必须同步改奖励函数里的分隔符与标签，否则奖励恒为 0——这就是 `# NOTE: also need to change reward_score/countdown.py` 的含义。
- `make_map_fn` 把 prompt 组装成 veRL 期望的 parquet 字段：`data_source`（路由奖励函数）、`prompt`（模型输入）、`reward_model.ground_truth`（判分参考）、`extra_info`（调试元信息）。
- 最终通过 `to_parquet` 写出 `train.parquet` / `test.parquet`，可选地用 `hdfs_io` 的 `makedirs` / `copy` 拷到 HDFS。

## 7. 下一步学习建议

本讲只覆盖了 countdown 一个任务的数据生成。建议接下来：

- **横向对比**：阅读 [u2-l2](u2-l2-multiply-arithmetic-data.md)，看 `multiply.py` 与 `arth.py` 在样本生成（按位数 DIGIT）和 prompt 模板上与 countdown 的异同。
- **纵向深入下游**：阅读 [u2-l3](u2-l3-rlhf-dataset-loading.md)，看 `RLHFDataset` 如何把本讲产出的 parquet 读取、tokenize 成 `input_ids` / `attention_mask`，完成「文本 → 张量」的最后一步。
- **奖励侧闭环**：阅读 [u2-l4](u2-l4-rule-based-reward.md)，精读 `reward_score/countdown.py` 的 `extract_solution` / `validate_equation` / `evaluate_equation` 三步判分，彻底闭环「prompt 约定 ↔ 奖励提取」这对强耦合关系。
```
