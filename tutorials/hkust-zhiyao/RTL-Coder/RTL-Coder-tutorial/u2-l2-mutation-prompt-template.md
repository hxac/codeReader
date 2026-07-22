# 变异提示词模板 p_example.txt

## 1. 本讲目标

学完本讲，读者应该能够：

- 说清 `data_generation/p_example.txt` 这份提示词模板的三大组成部分（角色设定、变异方法、单样本示例）各自的作用。
- 区分两套符号：`#...#` 是段落标记，`{...}` 是字段占位符，并理解 `{Instruction}` / `{Input}` 为何「身兼二职」。
- 解释 `#method#` 如何控制数据变异的方向（「功能不同、方法/器件相似」）。
- 读懂 `instruction_gen.py` 中 `encode_prompt_uniq` 是怎样把模板和一条种子题目拼接成最终发给 GPT-3.5 的完整 prompt 的。

## 2. 前置知识

在进入模板之前，先回顾三个基础概念：

- **One-shot prompting（单样本提示）**：在提示词里给模型看一个完整的「输入 → 输出」示例，再让它对新输入做同样的变换。本讲的模板就是一个典型的单样本模板——先演示一遍「3 级流水线 → 4 级流水线」，再让模型照葫芦画瓢。
- **占位符 / 模板**：用一段固定文本来框定输出格式，留出若干「填空位」交给程序或模型去填。模板的质量直接决定生成数据的质量。
- **种子指令（seed）**：作为变异起点的原始题目。在 RTL-Coder 里它来自 [data_generation/data_sample.json](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/data_generation/data_sample.json)，每条形如 `{"Instruction": ..., "Input": ...}`。

承接 [u2-l1](u2-l1-data-generation-overview.md)：RTL-Coder 用 GPT-3.5 自动造数据，`p_example.txt` 正是第二阶段「指令变异」所用的提示词模板，由 `instruction_gen.py` 读取并驱动。GPT 在整个流程里只充当「数据标注员」，不参与最终模型。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [data_generation/p_example.txt](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/data_generation/p_example.txt) | 变异提示词模板：定义角色、变异方法（`#method#`）、一个 given→rewritten 的单样本示例，末尾留一个待填充的 `#given prompt#` 钩子。 |
| [data_generation/instruction_gen.py](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/data_generation/instruction_gen.py) | 读取模板并与种子拼接的 `encode_prompt_uniq` 函数，以及用 `{Instruction}`/`{Input}` 切分模型回复的 `post_process_gpt3_response_uniq`。 |

## 4. 核心概念与源码讲解

本讲按三个最小模块拆解：先看 `#method#` 决定「怎么变异」，再看模板的两套占位符与那个单样本示例「长什么样」，最后看 `encode_prompt_uniq` 怎么把模板和种子拼起来。

### 4.1 `#method#`：变异方法描述

#### 4.1.1 概念说明

数据集要扩大、要多样，但不能「乱」。如果让 GPT 随意生成 Verilog 题目，它要么重复造轮子，要么给出脱离真实设计模式的怪题。`#method#` 就是用来给变异「定方向」的一段指令：它告诉模型在改写题目时应该遵循什么策略。

RTL-Coder 选择的策略是 **「功能不同，但方法/器件相似」**（different circuit functionality but requiring similar methods or components）。直觉上，这样能批量产出「结构同源、功能各异」的题目——比如同一个流水线骨架，换个 ALU 运算集合、加几级流水段，就变成一道新题。这类样本对模型学习「可迁移的设计模式」很有价值。

#### 4.1.2 核心流程

`#method#` 段落在模板里由两部分构成：

1. **核心策略句**（第 3 行）：一句话写明变异方向。
2. **三条输出格式约束**（第 4–6 行）：规定改写后的 `{Instruction}` / `{Input}` 必须满足哪些硬性要求。

伪代码描述 `#method#` 的作用：

```
读取 #method# 段落
  ├─ 策略：新题目功能要不同，但要用到原题相似的方法/器件
  └─ 格式约束：
       · {Instruction} 开头必须加 "Please act as a professional verilog designer."
       · {Instruction} 内用 ≤100 词描述实现思路，且要 self-contained
       · {Input} 里不要写任何注释/说明
```

这三条约束配合 `post_process_gpt3_response_uniq`（见 4.2）里的字数过滤（`> 400` 词直接丢弃），共同保证落盘数据干净可用。

#### 4.1.3 源码精读

`#method#` 段落在模板的第 2–6 行：[data_generation/p_example.txt:L2-L6](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/data_generation/p_example.txt#L2-L6)。其中最关键的是第 3 行的策略句：

```text
The rewritten task should achieve different circuit functionality
but requiring similar methods or components in the #given prompt#
```

紧接着第 4–6 行是三条格式约束（加专业设计师前缀、限 100 词、`{Input}` 不写注释）。注意第 4 行还要求改写后的 prompt 要 **self-contained and detailed**（自包含、细节充分）——这正是 `data_sample.json` 里每条 `Instruction` 都长篇大论的原因：它们都是按这套模板产出的。

模板末尾的那个「示例」（第 7–44 行）正好是 `#method#` 策略的一次落地示范，4.2 会展开。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**，目标是用自然语言把 `#method#` 的策略复述出来，并预测改写策略会带来什么数据分布。

1. 实践目标：能口头解释「为什么是『功能不同、方法相似』而不是『功能相同、方法不同』」。
2. 操作步骤：
   - 打开 `data_generation/p_example.txt`，定位第 2–6 行。
   - 用一句话写下当前 `#method#` 的变异策略。
   - 再设想两种相反的策略，例如：
     - A：相同功能，但换一种实现方法/器件；
     - B：相同功能，但改变位宽（更宽或更窄的数据通路）。
3. 需要观察的现象：策略 A、B 产出的题目与原题的「相似度」会更高于当前策略，因为功能没变。
4. 预期结果：当前策略（功能不同）倾向于扩展题目覆盖的电路类型；策略 A/B 倾向于在同一电路上做变体，数据多样性更低、但「同源变体」更多。
5. 无法确定运行结果的部分：标注「待本地验证」——具体数据分布需真的跑一轮 GPT 生成才能统计。

#### 4.1.5 小练习与答案

**练习 1**：如果把第 3 行改成「相同的电路功能、相同的实现方法」，ROUGE-L 去重（见 [u2-l5](u2-l5-rouge-dedup.md)）会更难还是更容易？为什么？

> **答案**：更容易触发去重。功能和方法都不变，生成的指令文本与原题高度相似，ROUGE-L 分数容易超过 0.7 阈值而被丢弃，最终保留率会很低。

**练习 2**：第 4 行要求在 `{Instruction}` 开头加 "Please act as a professional verilog designer."。这条约束的下游消费者是谁？

> **答案**：消费者是最终训练阶段。这套前缀让造出的指令格式与模型推理时（见 [u1-l3](u1-l1-quick-start-inference.md)）使用的 prompt 范式一致，保证训练/推理分布对齐。

### 4.2 占位符体系：两套符号与一个单样本示例

#### 4.2.1 概念说明

模板里出现了长相相似但职责完全不同的两套符号，初学者最容易混淆：

| 符号样式 | 名称 | 例子 | 职责 |
|---|---|---|---|
| `#...#` | 段落标记 | `#method#`、`#given prompt#`、`#rewritten prompt#` | 给人/模型看的小标题，划分文本段落 |
| `{...}` | 字段占位符 | `{Instruction}`、`{Input}` | 数据字段名，既被模型「抄写」，又被程序「切分」 |

`{Instruction}` / `{Input}` 之所以关键，是因为它们 **身兼二职**：

1. 作为**字段标签**：模板示例里用它们标注「这里是题目描述」「这里是模块签名」，模型会照样在自己的输出里抄一遍这两个标记。
2. 作为**切分定位点**：`post_process_gpt3_response_uniq` 用 `split('{Instruction}')` / `split('{Input}')` 把模型的回复切成「指令」和「输入」两段。没有这两个标记，解析就无法定位。

这就是为什么模板要反复出现 `{Instruction}` / `{Input}`——它们不只是排版，更是程序读取数据的「锚点」。

#### 4.2.2 核心流程

模板第 7–45 行构成一个完整的 one-shot 示例 + 待填充钩子，结构如下：

```
第 7 行  : Here is one example for you:          ← 宣告「下面是一个示例」
第 8 行  : #given prompt#                         ← 示例的「输入题」开始
第 9-10行: {Instruction} + 3 级流水线描述
第11-26行: {Input}     + module pipeline 签名
第27 行  : #rewritten prompt#                     ← 示例的「改写题」开始
第28-29行: {Instruction} + 4 级流水线描述（ALU 换成 MUL/DIV/shift）
第30-44行: {Input}     + module pipeline 签名
第45 行  : #given prompt#                         ← 第二次出现！真正的「待填充」钩子
```

这里有**两个关键的易错点**：

- **示例里的 given 是 3 级流水线（ADD/SUB/AND/NAND），rewritten 是 4 级流水线（MUL/DIV/shift/comparison）**。功能变了（不同 ALU 运算、多一级流水段），但「方法/器件相似」（都是流水线 + 寄存器堆 + 指令译码）——这正是 `#method#` 策略的标准示范。
- **`#given prompt#` 在模板里出现了两次**。第一次（第 8 行）属于示例；第二次（第 45 行）是一个**故意留在末尾的「钩子」**，等待 `encode_prompt_uniq` 把真正的种子题目拼到它后面（见 4.3）。

#### 4.2.3 源码精读

示例段落见 [data_generation/p_example.txt:L7-L44](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/data_generation/p_example.txt#L7-L44)。「第二个 `#given prompt#` 钩子」见 [data_generation/p_example.txt:L45-L47](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/data_generation/p_example.txt#L45-L47)。

而 `{Instruction}` / `{Input}` 作为切分锚点的下游用法在解析函数里：[data_generation/instruction_gen.py:L56-L58](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/data_generation/instruction_gen.py#L56-L58)，逐行说明：

```python
tmp = raw_instructions.split('{Instruction}', 1)[1]   # 取 {Instruction} 之后的内容
ins = tmp.split('{Input}', 1)[0].strip()               # {Instruction} 与 {Input} 之间 = 指令
inp = tmp.split('{Input}', 1)[1].strip()               # {Input} 之后 = 输入/模块签名
```

可以看到，解析逻辑完全依赖模型在输出里「照抄」了 `{Instruction}` 和 `{Input}` 这两个标记。这就是模板要反复写它们的原因。

#### 4.2.4 代码实践

这是一个**手工跟踪解析逻辑**的实践，帮助理解 `{Instruction}` / `{Input}` 为何身兼二职。

1. 实践目标：手工模拟 `post_process_gpt3_response_uniq` 对一段假回复的切分。
2. 操作步骤：假设 GPT 对某条种子返回了如下 `text`（示例代码，非项目真实输出）：

   ```text
   {Instruction} Please act as a professional verilog designer. Create a 16-bit ALU.
   {Input} module alu(input [15:0] a, input [15:0] b, output [16:0] y);
   ```

   按上面 L56–L58 的三行代码，手动算出 `ins` 和 `inp`。
3. 需要观察的现象：`ins` 是否正好是两行之间的指令文本？`inp` 是否正好是模块签名？
4. 预期结果：
   - `ins = "Please act as a professional verilog designer. Create a 16-bit ALU."`
   - `inp = "module alu(input [15:0] a, input [15:0] b, output [16:0] y);"`
5. 结论：若模型漏抄了 `{Instruction}` 或 `{Input}`，`split(...)[1]` 会抛 `IndexError`，被第 65–66 行的 `try/except` 兜底成空列表丢弃。

#### 4.2.5 小练习与答案

**练习 1**：`#given prompt#` 在模板里出现两次，分别承担什么职责？

> **答案**：第一次（第 8 行）是 one-shot **示例**的输入题标题；第二次（第 45 行）是**待填充的钩子**，等待 `encode_prompt_uniq` 把真正的种子题目拼到它后面。

**练习 2**：如果把模板里所有 `{Input}` 都改成 `[Input]`，哪一处代码会先报错？

> **答案**：`post_process_gpt3_response_uniq`（L57–L58）会先受影响——它用 `split('{Input}')` 切分，改成 `[Input]` 后切不出来，触发 `IndexError` 被丢弃，导致所有样本都无法落盘。

**练习 3**：示例里 given 是 3 级流水线、rewritten 是 4 级流水线，这对应 `#method#` 的哪条策略？

> **答案**：对应「different circuit functionality but requiring similar methods or components」——功能（ALU 运算集合、流水级数）变了，但方法/器件（流水线、寄存器堆、译码）相似。

### 4.3 `encode_prompt_uniq`：模板与种子的拼接

#### 4.3.1 概念说明

模板 `p_example.txt` 是「死」的静态文本，真正生成数据时需要把「活」的种子题目填进去。这个「模板 + 种子」的拼装工作由 `encode_prompt_uniq` 完成。它的职责很单一：读取模板，在末尾的 `#given prompt#` 钩子后追加一条种子的 `{Instruction}` / `{Input}`，再补一个 `#Rewritten prompt#` 收尾——把球踢给 GPT，让它产出改写后的题目。

#### 4.3.2 核心流程

函数逻辑可以概括为四步：

```
1. prompt = 读模板全文 + "\n"          # 模板末尾自带 #given prompt# 钩子
2. for 每条种子:
     · 清洗 instruction（折叠空白、去尾冒号）
     · 追加 "\n{Instruction}\n" + instruction
     · 追加 "\n{Input}\n"     + input
3. 追加 "\n#Rewritten prompt#\n"        # 告诉模型「现在轮到你改写了」
4. return prompt
```

最终发给 GPT 的 prompt 结构是：

```
[角色设定 + #method# + 单样本示例(3级→4级流水线) + 第45行 #given prompt#]
{Instruction}
<种子的真实指令>
{Input}
<种子的真实模块签名>
#Rewritten prompt#
```

模型看到「示例 + 一条新题目 + `#Rewritten prompt#`」，就会模仿示例格式输出这条新题目的改写版。

#### 4.3.3 源码精读

先看模板路径是怎么定的：[data_generation/instruction_gen.py:L15-L16](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/data_generation/instruction_gen.py#L15-L16) 说明默认用 `p_example.txt` 作为变异方法。

拼接函数本体：[data_generation/instruction_gen.py:L36-L47](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/data_generation/instruction_gen.py#L36-L47)，逐行解读：

```python
def encode_prompt_uniq(prompt_instructions):
    prompt = open(evo_type).read() + "\n"          # 读模板（末尾已含 #given prompt# 钩子）
    for idx, task_dict in enumerate(prompt_instructions):
        (instruction, input) = task_dict["Instruction"], task_dict["Input"]
        instruction = re.sub(r"\s+", " ", instruction).strip().rstrip(":")  # 清洗
        # prompt += '#given prompt#\n'             # ← 关键：这一行被注释掉了
        prompt += '\n{Instruction}\n' + instruction
        prompt += '\n{Input}\n'     + input
    prompt += '\n#Rewritten prompt#\n'
    return prompt
```

**这里有一个必须看懂的细节**：第 41 行 `# prompt += '#given prompt#\n'` 是**被注释掉的**。原因正是 4.2 提到的——模板第 45 行末尾已经自带了一个 `#given prompt#` 钩子，所以这里不需要再程序化补一个，否则会重复。这条注释是「模板钩子设计」的直接证据：开发者本想在这里加，发现模板已经有了，就注释掉了。

**一个诚实的观察**：当前 `generate_instruction_following_data` 主循环里实际调用 GPT 时，并没有调用 `encode_prompt_uniq`，而是直接读了光秃秃的模板：[data_generation/instruction_gen.py:L120-L123](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/data_generation/instruction_gen.py#L120-L123)。也就是说，shipped 版主循环靠模板内置的那个 3 级→4 级流水线示例就让 GPT 反复产出变体，而 `encode_prompt_uniq` 是「为注入自定义种子而准备」的函数（也是本讲实践任务的主角）。读源码时要意识到这种「定义了但当前主路径未调用」的情况，避免误以为它在生产链路里被使用。

#### 4.3.4 代码实践

这是本讲的核心**可运行实践**，对应任务规格：修改 `#method#` 为「相同功能但不同位宽」，手写一个 `#given prompt#` 种子，跑通 `encode_prompt_uniq` 查看输出。

1. 实践目标：亲眼看到「模板 + 种子」拼出的完整 prompt 长什么样，并验证修改 `#method#` 后拼接结果随之变化。
2. 操作步骤：

   **步骤 a**：进入 `data_generation/` 目录，把模板复制一份做实验（**不要直接改原模板**，以免破坏真实数据生成）：

   ```bash
   cp p_example.txt p_example_width.txt
   ```

   **步骤 b**：编辑 `p_example_width.txt` 第 3 行，把策略改成「相同功能、不同位宽」，例如：

   ```text
   The rewritten task should achieve the SAME circuit functionality as the #given prompt#, but with a DIFFERENT bit-width (e.g. a wider or narrower data path).
   ```

   **步骤 c**：在同目录新建 `run_encode_prompt_uniq.py`（示例代码，非项目原有文件）。注意——**不要直接 `import instruction_gen`**，因为该文件末尾第 165–170 行有一段顶层 `generate_instruction_following_data(...)` 调用，import 时会触发全量生成（需要 OpenAI key）。所以我们把函数体复制出来运行：

   ```python
   # 示例代码：run_encode_prompt_uniq.py
   import re

   # 复制自 instruction_gen.py 的 encode_prompt_uniq，evo_type 改成参数以便指定实验模板
   def encode_prompt_uniq(prompt_instructions, evo_type="p_example_width.txt"):
       prompt = open(evo_type).read() + "\n"
       for idx, task_dict in enumerate(prompt_instructions):
           (instruction, input) = task_dict["Instruction"], task_dict["Input"]
           instruction = re.sub(r"\s+", " ", instruction).strip().rstrip(":")
           prompt += '\n{Instruction}\n' + instruction
           prompt += '\n{Input}\n' + input
       prompt += '\n#Rewritten prompt#\n'
       return prompt

   # 手写一个种子 #given prompt#（字段名必须叫 Instruction / Input）
   seed = [{
       "Instruction": "Please act as a professional verilog designer. "
                      "Create a module that implements a 4-bit ripple-carry adder.",
       "Input": "module adder (\n  input  [3:0] a,\n  input  [3:0] b,\n  output [4:0] sum\n);"
   }]

   print(encode_prompt_uniq(seed))
   ```

   **步骤 d**：运行 `python run_encode_prompt_uniq.py`。

3. 需要观察的现象：
   - 输出的开头是完整的模板（角色 + `#method#`（已是位宽版）+ 3 级→4 级流水线示例 + 第 45 行 `#given prompt#`）。
   - 紧接着是 `\n{Instruction}\n` + 你的种子指令、`\n{Input}\n` + 你的模块签名。
   - 最后一行是 `#Rewritten prompt#`，后面没有内容——这正是留给 GPT 接着写的位置。
4. 预期结果：你能清楚地看到「模板钩子 + 种子 + `#Rewritten prompt#`」三段式结构，且修改后的「位宽」`#method#` 出现在输出开头。
5. 进阶观察：把 `evo_type` 改回 `p_example.txt` 再跑一次，对比开头 `#method#` 行的差异，体会「换模板 = 换变异策略」。

#### 4.3.5 小练习与答案

**练习 1**：为什么第 41 行 `# prompt += '#given prompt#\n'` 是注释掉的？如果取消注释会发生什么？

> **答案**：因为模板第 45 行末尾已经自带 `#given prompt#` 钩子。若取消注释，prompt 里会出现两个连续的 `#given prompt#`，虽然不影响运行，但会让模型困惑、显得多余。

**练习 2**：`instruction = re.sub(r"\s+", " ", instruction).strip().rstrip(":")` 这行清洗起什么作用？

> **答案**：把指令里所有连续空白（含换行、缩进）折叠成单空格，再去掉首尾空白和结尾的冒号。保证拼进模板的种子指令是规整的单行文本，便于后续 ROUGE-L 比对（见 [u2-l5](u2-l5-rouge-dedup.md)）。

**练习 3**：为什么实践脚本里建议把函数「复制出来」而不是 `from instruction_gen import encode_prompt_uniq`？

> **答案**：`instruction_gen.py` 末尾有顶层 `generate_instruction_following_data(...)` 调用，import 时会立刻执行，触发对 OpenAI 的真实请求。复制函数体可避免这一副作用，便于离线调试拼接逻辑。

## 5. 综合实践

把本讲三个模块串起来：写一个「最小变异器」调试脚本，让你能任意切换 `#method#` 策略、任意喂种子，并打印出最终 prompt，同时手工验证解析能否正常切分。

任务步骤：

1. 在 `data_generation/` 下准备两份模板副本：`p_example_width.txt`（策略改为「相同功能、不同位宽」）和 `p_example_func.txt`（沿用原策略「功能不同、方法相似」）。
2. 写一个脚本，分别用这两份模板对同一条种子（例如一个 8 位计数器）调用你在 4.3 复制的 `encode_prompt_uniq`，打印两份完整 prompt。
3. 对照两份输出的 `#method#` 段，说明同一颗种子在不同策略下，模型被引导去往「改位宽」还是「换功能」两个不同方向。
4. 进阶：自己手写一段「假设的模型改写输出」，里面包含 `{Instruction}` 和 `{Input}` 标记，套用 `post_process_gpt3_response_uniq`（L49–L66）的切分逻辑，验证能否正确抽出 `{"Instruction": ..., "Input": ...}`，并测试一个「漏抄 `{Input}`」的样例是否被 `try/except` 兜底丢弃。

预期结果：你能独立解释「模板钩子 → 种子注入 → 模型照格式输出 → 解析切分回字段」的完整闭环，并能通过换模板/换种子预测生成数据的走向。真实生成结果需接 OpenAI key，标注「待本地验证」。

## 6. 本讲小结

- `p_example.txt` 由三部分组成：角色设定（第 1 行）、变异方法 `#method#`（第 2–6 行）、一个 given→rewritten 的单样本示例（第 7–44 行）。
- `#method#` 的核心策略是「功能不同、方法/器件相似」，目的是批量产出结构同源、功能各异的题目；第 4–6 行是输出格式约束。
- 两套符号职责不同：`#...#` 是段落标记，`{...}` 是字段占位符；`{Instruction}` / `{Input}` 身兼「模型抄写的标签」和「程序切分的锚点」二职。
- 模板末尾第 45 行的第二个 `#given prompt#` 是「待填充钩子」，`encode_prompt_uniq` 把种子拼到它后面，再补 `#Rewritten prompt#`。
- 第 41 行被注释的 `#given prompt#` 正是「钩子已在模板里」的证据；shipped 主循环当前直接用光模板调用 GPT，`encode_prompt_uniq` 是为注入自定义种子而准备。

## 7. 下一步学习建议

- 下一讲 [u2-l3 GPT-3.5 调用工具 utils.py](u2-l3-gpt-utils.md)：`encode_prompt_uniq` 拼出的 prompt 最终交给 `utils.askGPT35` 发送，去读它的消息构造、重试与上下文超限降级。
- 之后 [u2-l4 指令生成主循环](u2-l4-instruction-gen-loop.md)：把本讲的「拼接」放回 `generate_instruction_following_data` 主循环，看它如何批量调用、解析、落盘。
- 想理解落盘前的去重过滤，衔接 [u2-l5 基于 ROUGE-L 的指令去重](u2-l5-rouge-dedup.md)。
