# 数据集生成流程总览

## 1. 本讲目标

学完本讲，你应该能够：

- 说出 RTL-Coder 自动化数据集生成的三个阶段（**关键词准备 → 指令生成 → 参考代码生成**）各自解决什么问题；
- 在仓库里准确定位每个阶段对应的脚本与数据文件；
- 用一句话讲清「GPT-3.5 只用于造数据、不参与最终模型」这条角色边界；
- 理解 `instruction_gen.py`（流程编排）与 `utils.py`（GPT 调用 + JSON 读写）的分工。

本讲的定位是**总览**：只画大图、把每个阶段钉到具体代码位置，不深入每个函数的实现细节——那些留给后续 u2-l2（变异模板）、u2-l3（GPT 工具）、u2-l4（生成主循环）、u2-l5（ROUGE 去重）逐文件精读。

## 2. 前置知识

- **数据格式（承接 u1-l4）**：项目用逐行 JSON（JSONL）存数据，`Response` 字段统一是列表。本讲会反复用到这个认知。
- **指令微调（Instruction Tuning）**：把「自然语言指令 + 期望输出」配对成训练样本，教模型按指令做事。RTL-Coder 里指令是 Verilog 设计需求描述，期望输出是参考 Verilog 代码。
- **提示词工程（Prompt Engineering）**：用一段精心设计的提示词约束大模型产出你想要的格式。本项目每个生成阶段都靠一段「提示词模板」控制 GPT。
- **变异（Mutation / Evol-Instruct）**：拿一条已有指令，让模型改写成「功能不同但用到相似方法/器件」的新指令，从而把少量种子扩展成海量多样数据。这是阶段 2 的核心思想。
- **JSONL（JSON Lines）**：每行一个独立 JSON 对象的文本格式，可流式逐行读写，非常适合中途断点续跑。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
|---|---|---|
| `README.md` | 项目说明 | 三阶段流程的文字定义与 `data_gen_flow` 流程图 |
| `data_generation/instruction_gen.py` | 指令生成主编排脚本 | 主循环、提示词拼装、响应解析、去重落盘 |
| `data_generation/utils.py` | GPT 调用与 JSON 读写工具 | `askGPT35`、`load_json`、`save_json` |
| `data_generation/p_example.txt` | 变异提示词模板 | `#method#`、`{Instruction}`、`{Input}` 占位符 |
| `data_generation/data_sample.json` | 种子指令数据 | 流程输入与去重基线 |

一句话记住目录边界：`data_generation/` 整个文件夹只做一件事——**造数据**；造出来的成品数据集放在另一个目录 `dataset/Resyn27k.json`（约 2.7 万条），供 `train/` 下的训练脚本消费。

## 4. 核心概念与源码讲解

### 4.1 三阶段数据生成流程

#### 4.1.1 概念说明

RTL-Coder 要训练一个 Verilog 代码生成模型，但公开的 RTL（寄存器传输级）训练数据极度稀缺。作者的解法不是去爬数据，而是用商业大模型 GPT-3.5 **自动「造」**出约 2.7 万条「设计需求指令 + 参考 Verilog 代码」配对样本，汇总成 Resyn27k 数据集。

这个造数据的过程被组织成一条三阶段流水线，README 用 **Figure 1（data_gen_flow）** 描绘：

1. **RTL 领域关键词准备**：先准备一批 RTL/集成电路领域的主题关键词（如流水线、乘法器、状态机、存储控制器……），保证生成出的题目覆盖足够广的电路类型，而不是翻来覆去几种。
2. **指令生成**：以关键词/种子为引子，让 GPT-3.5 通过「变异」批量产出多样化、自洽的 Verilog 设计需求指令（自然语言描述 + 模块签名）。
3. **参考代码生成**：对每条指令，再让 GPT-3.5 以「专业 Verilog 程序员」身份写出对应的参考 Verilog 代码，填进 `Response` 字段。

三阶段的本质是：**先保证题目的多样性，再为每道题配一份参考答案**。这也解释了为什么最终数据格式是 `{Instruction, Input, Response}`——三个字段分别对应「需求描述、模块签名骨架、参考代码」。

#### 4.1.2 核心流程

用文字流程图把三阶段、它们用到的脚本/模板，以及 GPT 的位置画出来：

```
 阶段1：RTL 领域关键词准备        阶段2：指令生成               阶段3：参考代码生成
 (领域种子/关键词)               (变异扩量)                   (为每条指令配代码)
         │                            │                            │
         ▼                            ▼                            ▼
   领域关键词/种子指令         数万条多样化指令             每条指令的参考 Verilog
   data_sample.json           {Instruction, Input}         {Instruction,Input,Response}
         │                            │                            │
         └──────────── GPT-3.5  (utils.askGPT35) ─────────────────────────┘
                       ↑ 仅在数据生成阶段被调用，不参与最终模型 ↑

   编排脚本：data_generation/instruction_gen.py  （阶段 2 的主驱动）
   变异模板：data_generation/p_example.txt       （阶段 2 的提示词模板）
   共享工具：data_generation/utils.py            （三阶段共用的 askGPT35 + JSON 读写）
   成品输出：dataset/Resyn27k.json               （约 2.7 万条，喂给 train/）
```

一个关键认知：**三个阶段共用同一个 GPT 调用工具 `askGPT35`**，区别只在于喂给它的「系统提示词」和「用户提示词模板」不同。阶段 2（造题目）用通用空系统提示；阶段 3（造代码）用「专业 Verilog 程序员」系统提示——这一点在 4.3 节会看到源码证据。

#### 4.1.3 源码精读

README 的 RTLCoder-flow 章节明确写出了三阶段划分与「GPT 仅用于造数据」的边界（这段是本讲最重要的原文依据）：

> README 对三阶段流程的定义与 GPT 角色边界：[README.md:L62-L64](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/README.md#L62-L64)

其中关键一句是："Please notice that GPT is only used for dataset generation in this work"——GPT 只用来造数据集，最终的 RTLCoder 模型是基于开源底座（DeepSeek-coder / Mistral）微调出来的，与 OpenAI 的模型不存在商业竞争。

README 的 Dataset 章节给出了阶段 2 的运行入口，一行命令即可扩量：

> 扩展数据集的入口命令：[README.md:L84-L86](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/README.md#L84-L86)

需要诚实指出的一点是：**仓库里只完整 shipped 了阶段 2 的驱动脚本** `instruction_gen.py`；阶段 1（关键词准备）和阶段 3（参考代码生成）并没有单独的批量驱动脚本随仓库发布，它们复用的就是 `utils.py` 里的 `askGPT35`（阶段 3 走 `is_response=True` 分支，详见 4.3.3）。所以本讲的「定位代码」结论是：

| 阶段 | 是否 shipped 驱动 | 对应代码位置 |
|---|---|---|
| 阶段 1 关键词准备 | 否（用种子数据代替） | `data_generation/data_sample.json` 作种子；`encode_prompt_topic` 留有 `#task#` 接口（待确认是否有外部关键词表） |
| 阶段 2 指令生成 | **是** | `data_generation/instruction_gen.py` + `p_example.txt` |
| 阶段 3 参考代码生成 | 否（仅提供工具） | `utils.py` 的 `askGPT35(is_response=True)` 提供「Verilog 程序员」系统提示路径（待确认批量脚本） |

> 阅读诚实性说明：上表中标注「待确认」的部分表示仓库当前 HEAD 下没有对应文件，不代表它不存在于作者本地。学习时以「阶段 2 完整可读、阶段 1/3 只能看工具层」为准。

#### 4.1.4 代码实践

**实践目标**：把抽象的三阶段流程钉到具体文件，建立「流程 ↔ 代码」的肌肉记忆。

**操作步骤**：

1. 打开 README 的 RTLCoder-flow 章节，找到 Figure 1（`_pic/data_gen_flow.jpg`）。
2. 在三个阶段框旁边分别手写标注对应的文件：
   - 阶段 1 → `data_generation/data_sample.json`（种子）
   - 阶段 2 → `data_generation/instruction_gen.py` + `data_generation/p_example.txt`
   - 阶段 3 → `data_generation/utils.py` 中的 `askGPT35(is_response=True)`
3. 在流程图最底部的 GPT 框上，用红笔写一句话角色边界声明（见「预期结果」）。

**需要观察的现象**：你会注意到流程图里 GPT 同时出现在三个阶段的下方——这正是「三阶段共用一个 GPT 工具」的可视化体现。

**预期结果**：角色边界声明可以写成——「GPT-3.5 仅作为数据标注员出现在 `data_generation/` 目录内，产出的 Resyn27k 数据集被 `train/` 消费来训练开源底座模型，最终 RTLCoder 推理时不调用任何 OpenAI 接口。」

#### 4.1.5 小练习与答案

**练习 1**：如果三个阶段都不用 GPT，仅靠人工，最瓶颈的是哪个阶段？为什么？
**参考答案**：阶段 2（指令生成）。因为需要数量庞大（约 2.7 万条）且多样的题目，人工编写成本极高；而阶段 1 只需准备一份关键词/种子列表，阶段 3 虽然也要写代码但可以借助阶段 2 已有的题目。

**练习 2**：为什么 README 要特意声明「GPT 只用于造数据，遵守 OpenAI 服务条款，与 OpenAI 模型无商业竞争」？
**参考答案**：因为最终发布的 RTLCoder 模型是用来做 Verilog 生成的，和 GPT 在同一任务赛道上可能构成竞争。这条声明划清了边界：GPT 只是「数据标注工具」，最终模型基于开源底座，不直接转售或蒸馏 GPT 的推理能力。

---

### 4.2 p_example 模板驱动的指令变异

#### 4.2.1 概念说明

阶段 2 要把少量种子指令「变异」成海量新指令。怎么让 GPT 每次都按统一的格式和思路去改写？答案是：**写一个变异提示词模板**，里面包含三样东西——

- **角色设定**：告诉 GPT「你是一个擅长出高质量 Verilog 题目的专家」；
- **变异方法 `#method#`**：一句话规定「怎么改」——本项目规定改成「功能不同但用到相似方法/器件」的题目；
- **一个完整的示例**：给 GPT 看一道「原题 → 改写题」的范本，让它照着模仿。

这个模板就是 `p_example.txt`。它本质是一份**带占位符的填空说明书**：占位符 `{Instruction}`、`{Input}` 标记题目正文和模块签名的位置，`#given prompt#` / `#rewritten prompt#` 标记原题和改写题的边界。脚本和 GPT 通过这些占位符来「写入种子」和「读出结果」。

#### 4.2.2 核心流程

变异一次的伪代码：

```
读取 p_example.txt 模板内容
（可选）把一条种子指令填进 {Instruction}/{Input} 占位符
把整段文本作为 user 消息发给 GPT-3.5
GPT 产出一段含 {Instruction}/{Input} 的改写文本
用 split('{Instruction}') / split('{Input}') 把改写文本切出新的 Instruction 和 Input
```

这套「占位符写入 + 占位符切出」的设计，让格式约束完全由模板控制，而不依赖 GPT 自觉——这是提示词工程的典型手法。

#### 4.2.3 源码精读

`instruction_gen.py` 顶部用一个列表来管理可切换的变异方法，当前只用 `p_example.txt` 这一种：

```python
evolv_dic = ['p_example.txt']  # list of mutation method
evo_type = evolv_dic[0] #choose a  mutation method
```

> 变异方法的选择：[instruction_gen.py:L15-L16](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/data_generation/instruction_gen.py#L15-L16)

这个列表暗示：**你可以新增自己的变异模板**（比如另写一个 `p_example_bitwidth.txt` 专做位宽变异），只要把它加进 `evolv_dic` 并切换 `evo_type` 即可。这正是 README 说的「You can design your own prompting method by modifying the file p_example.txt and instruction_gen.py」。

模板开头三行包含了角色设定和变异方法 `#method#`：

> 角色设定与 `#method#` 变异方法：[p_example.txt:L1-L3](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/data_generation/p_example.txt#L1-L3)

第 3 行的 `#method#` 内容规定了变异方向："The rewritten task should achieve **different circuit functionality** but requiring **similar methods or components**"——功能要变，但用到的设计手法/器件要相近，这样既能扩多样性，又能控制在 GPT 写得出代码的难度范围内。

模板中段是一个完整的「原题 → 改写题」示例（3 级流水线 → 4 级流水线），给 GPT 做模仿样本：

> 示例的原题标记 `#given prompt#`：[p_example.txt:L7-L8](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/data_generation/p_example.txt#L7-L8)
> 示例的改写题标记 `#rewritten prompt#` 与 `{Instruction}` 占位符：[p_example.txt:L27-L28](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/data_generation/p_example.txt#L27-L28)

GPT 回包后，`post_process_gpt3_response_uniq` 用 `split` 切出 `{Instruction}` 和 `{Input}` 之间的内容：

```python
tmp = raw_instructions.split('{Instruction}', 1)[1]
ins = tmp.split('{Input}', 1)[0].strip()
inp = tmp.split('{Input}', 1)[1].strip()
```

> 响应解析（按占位符切分）：[instruction_gen.py:L56-L58](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/data_generation/instruction_gen.py#L56-L58)

可以看到，**模板里定义的占位符，就是解析时切分的定位点**——写入和读出用的是同一套标记，这是整个变异机制能稳定运转的关键。占位符的逐个含义和拼接细节，会在 u2-l2 详细展开。

#### 4.2.4 代码实践

**实践目标**：在不调用 GPT 的前提下，亲手看清模板的结构和占位符分布。

**操作步骤**：

1. 用编辑器打开 `data_generation/p_example.txt`。
2. 数出以下标记各自出现在第几行：`#method#`、`{Instruction}`、`{Input}`、`#given prompt#`、`#rewritten prompt#`。
3. 阅读第 8–26 行的「原题」和第 27–44 行的「改写题」，对比两者：功能从 3 级流水线变成了什么？端口表有没有变？

**需要观察的现象**：改写题把 3 级流水线改成了 4 级流水线（多了 fetch 级），并把 ADD/SUB/AND/NAND 换成了 MUL/DIV/shift/comparison，还新增了 2 路转发；但 `module pipeline (...)` 的端口签名几乎没变。

**预期结果**：你会直观看到「功能变了、方法/器件相近、端口签名稳定」这条 `#method#` 规则的具体体现。这正是阶段 2 想要的变异效果。

#### 4.2.5 小练习与答案

**练习 1**：`{Instruction}` 和 `{Input}` 这两个占位符，在「写入种子」和「读出结果」时分别扮演什么角色？
**参考答案**：写入时，脚本把种子的需求描述填到 `{Instruction}` 后、模块签名填到 `{Input}` 后（见 `encode_prompt_uniq`）；读出时，脚本用 `split('{Instruction}')` 和 `split('{Input}')` 从 GPT 输出里切出新题目的描述和签名。同一个标记既是填空位也是切割位。

**练习 2**：`#method#` 为什么要求「相似方法/器件」而不是「完全不同的电路」？
**参考答案**：为了让阶段 3 GPT 写得出参考代码——如果改成完全陌生的电路，GPT 可能写不出可仿真代码，`Response` 质量会崩；限定在相似方法/器件内，既能扩多样性又能保证答案可生成。

---

### 4.3 instruction_gen.py 与 utils.py 的分工

#### 4.3.1 概念说明

阶段 2 的代码被刻意拆成两个文件，职责严格分离：

- **`instruction_gen.py` 是「编排者」**：负责整个流程的调度——加载种子、循环调 GPT、解析响应、去重、落盘。它**不直接**碰 OpenAI 的 API 细节。
- **`utils.py` 是「工具箱」**：提供两类底层能力——一是封装好的 GPT 调用 `askGPT35`（含重试、超限降级），二是逐行 JSON 读写 `load_json` / `save_json`。

这种「编排层 / 工具层」分离的好处是：以后想把 GPT-3.5 换成别的模型，只改 `utils.py`；想改生成策略（比如换变异模板、调去重阈值），只改 `instruction_gen.py`。两者互不干扰。

#### 4.3.2 核心流程

`instruction_gen.py` 的主循环 `generate_instruction_following_data` 把两个文件串成一条链：

```
load_json(data_sample.json)          ← utils 读种子
        │
        ▼
while 未达目标数量:
    读 p_example.txt 模板拼成 prompt
    askGPT35(prompt)                 ← utils 调 GPT
    post_process_gpt3_response_uniq  ← 解析出 {Instruction,Input}
    ROUGE-L 去重（阈值 0.7）
    save_json(target, output_file)   ← utils 落盘（每轮都存，支持断点续跑）
```

注意一个阅读要点：shipped 版的主循环**直接把 `p_example.txt` 原文**发给 GPT（依赖模板自带的那道 3→4 级流水线示例来教 GPT 变异），而没有调用 `encode_prompt_uniq` 把具体种子填进占位符。`encode_prompt_uniq` 是为「逐条种子注入式变异」准备的接口，留作更精细控制的扩展点。

#### 4.3.3 源码精读

先看工具层。`utils.py` 的逐行 JSON 读写是三阶段共用的存取方式：

> `load_json` 逐行读取：[utils.py:L18-L24](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/data_generation/utils.py#L18-L24)
> `save_json` 逐行写入（每轮落盘，便于断点续跑）：[utils.py:L26-L31](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/data_generation/utils.py#L26-L31)

`askGPT35` 是三阶段共用的 GPT 调用入口。它的 `is_response` 参数正是区分「造题目」和「造代码」的开关：

```python
def askGPT35(question ,model='gpt-35-turbo', is_response=False, temperature=0.7):
    ...
    if is_response is True:
        p_message = [
            {'role': 'system', 'content': 'I want you act as a Professional Verilog coder.'},
            {'role': 'user', 'content': question}]
    else:
        p_message = [
            {'role': 'system', 'content': ''},
            {'role': 'user', 'content': question}]
```

> `askGPT35` 的 `is_response` 分支——这正是阶段 3 的入口：[utils.py:L34-L45](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/data_generation/utils.py#L34-L45)

读这段代码就能验证 4.1 节的论断：

- `is_response=False`（默认，阶段 2 造题目用）→ 系统提示为空；
- `is_response=True`（阶段 3 造代码用）→ 系统提示是「Professional Verilog coder」。

底层调用走的是老版 openai 库（`openai.ChatCompletion.create`），对应 `requirements.txt` 里 pin 死的 `openai==0.28`：

> 真正的 OpenAI 调用与超限降级（`max_tokens/1.3`）：[utils.py:L55-L68](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/data_generation/utils.py#L55-L68)

再看编排层。`instruction_gen.py` 用 `utils.load_json` 读种子，并支持从中途产物续跑：

> 加载种子与断点续跑：[instruction_gen.py:L98-L104](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/data_generation/instruction_gen.py#L98-L104)

主循环把模板原文喂给 `utils.askGPT35`（注意：默认 `is_response=False`，即造题目而非造代码）：

> 主循环：拼 prompt → 调 GPT → 解析：[instruction_gen.py:L117-L133](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/data_generation/instruction_gen.py#L117-L133)

每轮结束后用 `utils.save_json` 落盘，并按 ROUGE-L 相似度 > 0.7 过滤近似重复（去重细节留给 u2-l5）：

> ROUGE-L 去重阈值与落盘：[instruction_gen.py:L145-L159](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/data_generation/instruction_gen.py#L145-L159)

最后，文件末尾就是真正执行的入口调用，默认目标是生成 50 条（注意：不是 2.7 万——2.7 万是完整成品 Resyn27k，这里 shipped 的只是个小规模 demo 默认值）：

> 脚本入口调用（seed=data_sample.json，目标 50 条）：[instruction_gen.py:L165-L170](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/data_generation/instruction_gen.py#L165-L170)

#### 4.3.4 代码实践

**实践目标**：不调用 OpenAI，本地验证「编排层调工具层」的调用关系，并理解数据如何在两个文件之间流动。

**操作步骤**：

1. 在 `instruction_gen.py` 中用搜索定位三处对 `utils.` 的调用：`utils.load_json`、`utils.askGPT35`、`utils.save_json`，记下它们的行号。
2. 用 Python 本地读取种子，确认数据能被 `load_json` 正确解析（无需 GPT）：

   ```python
   # 示例代码：仅演示读取种子格式，不调用 GPT
   import json
   with open('data_generation/data_sample.json') as f:
       seeds = [json.loads(line) for line in f]
   print('种子条数:', len(seeds))
   print('字段:', list(seeds[0].keys()))
   print('Response 是否列表:', isinstance(seeds[0]['Response'], list))
   ```

3. 想象把 `utils.askGPT35` 替换成一个返回固定文本的 mock 函数（u2-l3 会真正做这件事），观察：主循环里哪几行依赖 GPT 的真实返回？哪几行与 GPT 无关（加载、去重、落盘）？

**需要观察的现象**：步骤 2 会打印种子条数 10、字段 `['Instruction', 'Input', 'Response']`、`Response 是否列表: True`，与 u1-l4 学到的格式认知一致。

**预期结果**：你会清楚地看到——**只有「调 GPT + 解析响应」这两步依赖 OpenAI，其余的加载/去重/落盘完全是本地逻辑**。这正好印证了「GPT 只在数据生成环节、且只占流程的一小部分」。

#### 4.3.5 小练习与答案

**练习 1**：如果把 GPT-3.5 换成另一个聊天模型（比如开源大模型），需要改 `instruction_gen.py` 吗？
**参考答案**：原则上不用改主流程。因为对 OpenAI 的依赖被封装在 `utils.askGPT35` 里（返回 `[{text, finish_reason}]` 的统一结构）。只要新模型也封装成同样的返回结构，`instruction_gen.py` 的编排逻辑可以原样复用。这正是「编排/工具分层」的价值。

**练习 2**：为什么 `save_json` 要放在 `while` 循环**内部**、每轮都写一次，而不是循环结束后写一次？
**参考答案**：为了断点续跑。调 GPT 成本高、耗时长，且可能因网络/超限失败。每轮落盘后，即使中途崩溃，下次重启时 `os.path.exists` 检查到产物文件、`load_json` 读回已生成部分，就能从断点继续，而不是从头再来。

---

## 5. 综合实践

把本讲三块内容串起来，完成一份「数据生成流程导览卡」。

**任务**：

1. **数据层（可本地运行）**：写一段 Python，读取 `data_generation/data_sample.json`，打印：种子条数、第一条的 `Instruction` 前 80 个字符、`Input` 前 80 个字符、`Response` 列表长度。确认它与 u1-l4 的格式认知吻合。

2. **模板层（源码阅读）**：打开 `p_example.txt`，用一句话总结 `#method#` 规定的变异方向；再找出 `{Instruction}` 和 `{Input}` 在模板里出现的所有行号。

3. **流程层（绘图）**：画一张本讲 4.1.2 的三阶段流程图，并在每个阶段框上标注「输入 / 用到的脚本或模板 / 输出 / 是否调 GPT」。在阶段 2 框内额外标出 `instruction_gen.py` 调用了 `utils.py` 的哪三个函数。

4. **边界声明（写作）**：用一句话写清 GPT 在整条流程中的角色边界（提示：出现在哪个目录、被几个阶段共用、最终模型推理时还在不在）。

**预期结果（步骤 1，可本地验证）**：种子条数为 10；`Instruction` 形如 `"Please act as a professional Verilog designer. Your task is to create a Verilog module that implements a channel equalization block..."`；`Response` 列表长度为 1。

**预期结果（步骤 4，参考答案）**：「GPT-3.5 仅作为数据标注工具出现在 `data_generation/` 目录内，被三个生成阶段共用（通过 `utils.askGPT35` 的 `is_response` 开关切换造题/造代码），产出的 Resyn27k 喂给 `train/` 训练开源底座；最终 RTLCoder 模型在 `benchmark_inference/` 推理时不再调用任何 OpenAI 接口。」

## 6. 本讲小结

- RTL-Coder 用 GPT-3.5 自动造数据，流程分三阶段：**领域关键词准备 → 指令生成（变异）→ 参考代码生成**，对应 README 的 Figure 1（data_gen_flow）。
- 仓库只完整 shipped 了**阶段 2** 的驱动 `instruction_gen.py`；阶段 1 用种子数据 `data_sample.json` 代替，阶段 3 复用 `utils.askGPT35(is_response=True)` 的「Verilog 程序员」系统提示路径（批量驱动待确认）。
- `p_example.txt` 是变异提示词模板，靠 `#method#`（变异方向）+ 一个原题→改写题示例 + `{Instruction}`/`{Input}` 占位符来控制 GPT 的产出格式。
- 代码分两层：`instruction_gen.py` 负责**编排**（加载、循环、解析、去重、落盘），`utils.py` 负责**工具**（`askGPT35` 调 GPT、`load_json`/`save_json` 读写 JSONL）。
- 三阶段共用同一个 `askGPT35`，靠 `is_response` 开关在「造题目」和「造代码」之间切换——这是理解阶段 3 入口的钥匙。
- GPT 的角色边界非常清晰：**仅用于数据生成，不参与最终模型**；最终 RTLCoder 基于开源底座微调，推理时不碰 OpenAI 接口。

## 7. 下一步学习建议

本讲只画了总览大图，接下来按真实调用链逐文件下钻：

- **u2-l2 变异提示词模板 p_example.txt**：逐个占位符精读 `#method#`、`{Instruction}`、`{Input}`、`#given prompt#`、`#Rewritten prompt#`，以及 `encode_prompt_uniq` 如何把种子填进模板。
- **u2-l3 GPT 调用工具 utils.py**：精读 `askGPT35` 的重试机制、`maximum context` 超限时 `max_tokens/1.3` 的降级策略，以及逐行 JSON 读写。
- **u2-l4 指令生成主循环**：逐段走完 `generate_instruction_following_data` 的批量请求、响应解析、断点续跑与 `tqdm` 进度。
- **u2-l5 ROUGE-L 去重**：理解 `rouge_scorer.RougeScorer(['rougeL'])`、`_score_lcs` 与 0.7 阈值如何过滤近似重复。

建议阅读顺序与上述编号一致：先把模板和工具吃透，再读主循环，最后看去重，就能把整条阶段 2 链路彻底打通。
