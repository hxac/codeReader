# 思维链提示实验

## 1. 本讲目标

学完本讲，你应该能够：

1. **解释**思维链（Chain-of-Thought, CoT）提示为什么能在**完全不碰权重、完全不用随机采样**的前提下，把 0.6B 基座模型在 MATH-500 上的准确率从 15.2% 拉到 40.6%。
2. **读懂**独立评测脚本 `cot_prompting_math500.py`：它的命令行参数、`prompt_suffix` 的注入位置、评测主循环的四步链路，以及 jsonl 输出的字段结构。
3. **算清**这种方法的代价——为什么准确率涨 2.7 倍的同时，评测耗时涨了 8 倍多（10.1 min → 84.5 min），代价的根源是「回答变长」。
4. **设计**属于自己的第三个提示模板变体，并在同一子集上做出「无提示 vs CoT vs 自设计」的三方对照实验。

## 2. 前置知识

本讲站在两块已建好的地基上，不再重复推导：

- **u4-l1（提示模板）**：`render_prompt` 把角色设定、`\boxed{ANSWER}` 输出契约、题干与作答起点 `Answer:` 拼成 zero-shot 提示；提示模板是评测协议的一部分，只改几个词就能让准确率大幅波动。
- **u4-l4（端到端评测）**：MATH-500 评测是「**渲染 → 生成 → 提取 → 判分**」四步链路，逐题写 jsonl 日志；生成环节占 99% 以上耗时。

同时回忆 u5-l1/u5-l2 的结论——温度与 top-p 是在**概率分布上动手脚**的采样手段。本讲恰好相反：**解码全程是贪心（确定性）的**，唯一的变量是拼进 prompt 的几个单词。这是三种推理时扩展手段中最便宜、也最「软」的一种。

本讲需要的新概念：

| 概念 | 一句话解释 |
|---|---|
| **思维链（CoT）** | Wei 等人 2022 年提出：让语言模型在给出最终答案前先生成中间推理步骤。直觉是——Transformer 每生成一个 token 只做「一层固定深度」的计算，复杂题一步算不出来；把推理展开成几十上百个 token，等于给了模型几十上百次串行计算的机会。 |
| **Zero-shot CoT 提示** | 不给示例（few-shot），只在提示末尾加一句「一步一步解释」之类的指令，诱导模型模仿预训练语料中大量存在的分步解题文体。本讲的后缀 `\n\nExplain step by step.` 就是经典写法（Kojima 等人 2022 年的 "Let's think step by step" 同源）。 |
| **推理时扩展（inference-time scaling）** | 不改权重，只在**推理阶段**花更多计算（更长的生成、更多次采样）换准确率。CoT 是其中最基本的一种：计算花在「单次回答更长」上。 |

为什么一句后缀对基座模型特别有效？Qwen3 0.6B base 是在数万亿 token 的原始文本上**继续预训练**出来的，语料里本来就有海量的教科书式分步解题文本。没有后缀时，提示以 `Answer:` 结尾，模型条件在「直接报答案」的文体上，往往一步跳到结论（哪怕算错）；加上后缀后，条件分布被拨到「先列方程、再移项、最后 `\boxed{}`」的文体上，中间步骤本身就是草稿纸。

## 3. 本讲源码地图

| 文件 | 本讲关注的内容 |
|---|---|
| [ch04/02_math500-inference-scaling-scripts/cot_prompting_math500.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch04/02_math500-inference-scaling-scripts/cot_prompting_math500.py) | 主角：命令行入口（L95-L138、L141-L185）与带 `prompt_suffix` 的评测循环 `evaluate_math500_stream`（L24-L92） |
| [ch04/02_math500-inference-scaling-scripts/README.md](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch04/02_math500-inference-scaling-scripts/README.md) | 全量 500 题的基准结果表（L39-L46 与 L83-L97）与运行命令（L49-L62、L148-L156） |
| [reasoning_from_scratch/ch03.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py) | 被脚本导入的全部零件：`render_prompt`（L413-L420）、`generate_text_stream_concat`（L121-L145）、`extract_final_candidate`（L186-L206）、`grade_answer`（L360-L382）、`load_math500_test`（L423-L442）、`load_model_and_tokenizer`（L52-L90）；以及被「复制改造」的原版 `evaluate_math500_stream`（L503-L574） |
| [reasoning_from_scratch/ch02.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch02.py) | 贪心生成内核 `generate_text_basic_stream_cache`（L117-L142），确认本讲解码无随机性 |
| [ch04/01_main-chapter-code/ch04_main.ipynb](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch04/01_main-chapter-code/ch04_main.ipynb) | 书中 4.3 节 "Generating better responses with chain-of-thought prompting"：同一道题在有无 CoT 后缀下的单题对照演示 |

## 4. 核心概念与源码讲解

### 4.1 CoT 提示模板：一行后缀的注入点

#### 4.1.1 概念说明

本讲的「CoT 提示模板」不是新模板，而是在第 3 章既有模板**末尾追加一个可配置后缀**。设计动机很实际：`render_prompt` 是全仓库共享的（第 3 章基线、本讲 CoT、第 4 章自一致性脚本都用它），如果为了 CoT 重写一个模板，就要复制维护两份几乎相同的字符串；而「基础模板 + 可选后缀」把变量收敛成一个参数——`prompt_suffix`。

这就是「对照实验」的正确姿势：**控制变量**。基线与 CoT 两次评测的唯一差别是那一个后缀字符串，模型、权重、解码方式（贪心）、数据集完全一致，准确率差异才能干净地归因到提示上。

#### 4.1.2 核心流程

```
render_prompt(problem)                     # 第 3 章模板，以 "Answer:" 结尾
        │
        ▼
prompt += prompt_suffix                    # ← 本讲唯一的注入点
        │        prompt_suffix = ""                      → 基线
        │        prompt_suffix = "\n\nExplain step by step."  → CoT
        ▼
generate_text_stream_concat(...)           # 贪心生成（KV 缓存版）
```

最终 prompt 的形态（以书中演示题为例）：

```text
You are a helpful math assistant.
Answer the question and write the final result on a new line as:
\boxed{ANSWER}

Question:
Half the value of $3x-9$ is $x+37$. What is the value of $x$?

Answer:

Explain step by step.
```

注意一个容易被忽略的细节：后缀拼接在**整个模板的最后**，也就是落在 `Answer:` 这个作答起点**之后**。最终的提示读起来是「……请作答：（换行）请一步步解释」——指令出现在作答区的开头，模型会把它当作作答要求的一部分。这不是笔误，而是零样本 CoT 的常见写法：指令紧贴生成起点，诱导效果最强。

书中 notebook 4.3 节用这道题做了单题对照（见 [ch04_main.ipynb](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch04/01_main-chapter-code/ch04_main.ipynb) 4.3 节）：

| 提示 | 模型输出（节选） | 提取结果 | 正误 |
|---|---|---|---|
| 基线（无后缀） | ` \boxed{20}`（几乎立刻报答案） | 20 | ✗（真值 83） |
| + `\n\nExplain step by step.` | 设方程 → 两边乘 2 → 移项 → `x = 83` → `\boxed{83}` | 83 | ✓ |

基线输出只有几个 token；CoT 输出是几百 token 的完整推导。这一行对照同时展示了本讲的**收益**（答对）与**代价**（变长）——代价在 4.3 节量化。

#### 4.1.3 源码精读

**基础模板**（[reasoning_from_scratch/ch03.py:L413-L420](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L413-L420)）：`render_prompt` 用 f-string 把题干嵌进固定模板。注意 `\boxed{ANSWER}` 这行输出契约没有变——无论加不加 CoT 后缀，答案提取器 `get_last_boxed` 依然靠它锚定最终答案。**CoT 只改变「答案之前生成什么」，不改变「答案长什么样」**，所以下游提取与判分环节零改动。

**注入点**（[ch04/02_math500-inference-scaling-scripts/cot_prompting_math500.py:L45-L46](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch04/02_math500-inference-scaling-scripts/cot_prompting_math500.py#L45-L46)）：评测循环里 `render_prompt(row["problem"])` 之后紧跟 `prompt += prompt_suffix`（源码用 `# NEW` 注释标记这是相对第 3 章版本的全部改动之一）。两行代码，就是「CoT 提示」在这个仓库里的全部实体。

**后缀从哪来**（[ch04/02_math500-inference-scaling-scripts/cot_prompting_math500.py:L127-L132](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch04/02_math500-inference-scaling-scripts/cot_prompting_math500.py#L127-L132)）：`--prompt_suffix` 命令行参数，默认空串（即基线），help 文本直言用途 "Can be used to adds a chain-of-thought prompt"。README 第 8-12 行实验（[README.md:L148-L156](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch04/02_math500-inference-scaling-scripts/README.md#L148-L156)）示范了传入 `"\n\nExplain step by step."` 的写法。

**两处文本的细微出入**：notebook 4.3 节的拼接是 `prompt + " \n\nExplain step by step."`（`\n\n` 前多一个空格，因为模板以 `Answer:` 结尾、无尾随空格），而 README 与脚本的推荐用法是 `"\n\nExplain step by step."`（无前导空格）。差异只有一个空白字符，对分词与生成的影响可忽略，但做复现实验时值得知道两者并非逐字节相同。

#### 4.1.4 代码实践

**实践目标**：不跑模型，纯文本层面验证「后缀注入位置」与「模板不变性」。

1. **操作步骤**（示例代码）：

```python
# 示例代码：观察 prompt_suffix 的注入效果
from reasoning_from_scratch.ch03 import render_prompt, load_tokenizer_only

problem = "Compute 1/2 + 1/6."
base = render_prompt(problem)
cot = base + "\n\nExplain step by step."

print(repr(base[-20:]))   # 模板结尾长什么样
print(repr(cot[-45:]))    # 后缀接在哪里

tok = load_tokenizer_only("base")   # 只下载分词器，不碰权重
print("基线 prompt token 数:", len(tok.encode(base)))
print("CoT   prompt token 数:", len(tok.encode(cot)))
```

2. **需要观察的现象**：基线模板以 `"Answer:"` 结尾；后缀紧随其后；两个 prompt 的 token 数只差几个（后缀本身约 5-6 个 token）。
3. **预期结果**：`repr` 输出确认拼接位置在 `Answer:` 之后；CoT prompt 比基线长约 5-6 个 token。具体数值**待本地验证**（依赖分词器版本）。
4. 若把后缀换成 `" \n\nThink step by step."` 或 `"\n\nLet's think about this step by step."`，重新 encode 比较长度——这就是 4.3 节综合实践里「第三个模板变体」的预演。

#### 4.1.5 小练习与答案

**练习 1**：为什么不直接写一个 `render_prompt_cot(problem)` 新函数，而要搞「模板 + 后缀」？

**答案**：对照实验需要控制变量。`render_prompt` 被第 3 章基线、本讲脚本、自一致性脚本共用，如果为 CoT 复制一份模板字符串，两份模板日后一旦各自改动就会引入「模板漂移」——届时准确率差异无法归因是 CoT 还是模板本身变了。后缀方案把差异压缩到**一个字符串参数**上，基线就是 `prompt_suffix=""` 的特例，模板保持单一事实来源。

**练习 2**：CoT 后缀为什么放在 `Answer:` 之后而不是塞进开头的角色设定里？

**答案**：两个理由。其一，指令紧贴**生成起点**时，它直接构成模型第一个生成 token 的最近上下文，诱导效果最强；放在开头则可能被长题干「稀释」。其二，开头是角色设定区（"You are a helpful math assistant."），改动它等于改动第 3 章基线协议，破坏与基线的可比性——后缀方案保证不与基线共享的部分仅是追加的几个 token。

**练习 3**：CoT 让模型在 `\boxed{}` 之前生成几百个 token 的推导。从「每 token 计算量」角度解释这为什么可能提升准确率。

**答案**：Transformer 单次前向的计算深度是固定的（28 层），模型「想多久」不是由层数决定，而是由**生成多少个 token** 决定——每个新 token 都以全部前文为条件，等于多了一轮完整的网络计算。一道需要多步代数操作的题，指望一次前向算完相当于让模型「心算」；CoT 把中间结果逐个写进上下文，后续步骤可以「看着」前面的结果算，相当于把心算变成打草稿。

### 4.2 评测循环：复制-改造的 evaluate_math500_stream

#### 4.2.1 概念说明

`cot_prompting_math500.py` 里的 `evaluate_math500_stream` 不是从 `ch03` 导入的，而是把 [reasoning_from_scratch/ch03.py:L503-L574](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L503-L574) 的同名函数**复制过来改了两处**。这是一种常见的工程取舍：库函数是给 notebook 用的稳定接口，而实验脚本需要快速加实验开关（`prompt_suffix`）；与其给库函数加一个只被一个脚本消费的参数，不如让脚本自带一份可随意魔改的评测循环。代价是两份代码需要人肉同步——比如第 3 章版本里有 `total_len` 平均响应长度统计（ch03.py:L519、L530），本脚本就没有带过来。

#### 4.2.2 核心流程

```
main (L141-L185)
  ├─ 解析参数：device / which_model / dataset_size / max_new_tokens
  │            / compile / prompt_suffix / verbose
  ├─ load_math500_test()                  # 500 题，本地缓存优先
  ├─ load_model_and_tokenizer(...)        # base 或 reasoning 权重
  ├─ model.eval() + TF32 精度设置
  └─ evaluate_math500_stream (L24-L92)
       for 每道题 (L44):
         1. render_prompt(problem) + prompt_suffix     L45-L46
         2. generate_text_stream_concat（贪心+KV缓存） L47-L51
         3. extract_final_candidate(gen_text)          L53-L55
         4. grade_answer(extracted, row["answer"])     L56-L59
         5. 写一行 jsonl 记录                          L61-L69
         6. 打印 ETA 进度                              L71-L78
       汇总：Accuracy、Total time、日志路径             L87-L92
```

#### 4.2.3 源码精读

**循环主体四步链路**（[ch04/02_math500-inference-scaling-scripts/cot_prompting_math500.py:L44-L59](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch04/02_math500-inference-scaling-scripts/cot_prompting_math500.py#L44-L59)）：与 u4-l4 精读过的第 3 章版本逐行对应——渲染（含后缀）、生成、提取、判分；`num_correct += int(is_correct)` 累计对题数。生成调用的 `generate_text_stream_concat`（[reasoning_from_scratch/ch03.py:L121-L145](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L121-L145)）内部消费的是 `generate_text_basic_stream_cache`（[reasoning_from_scratch/ch02.py:L117-L142](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch02.py#L117-L142)）——贪心 argmax 解码，**没有任何温度或 top-p**。这一点决定了本讲实验的解读方式：所有差异只能来自 prompt。

**jsonl 记录**（[ch04/02_math500-inference-scaling-scripts/cot_prompting_math500.py:L61-L69](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch04/02_math500-inference-scaling-scripts/cot_prompting_math500.py#L61-L69)）：每题写一行七字段 JSON——`index / problem / gtruth_answer / generated_text / extracted / correct` 加文件句柄。`generated_text` 保留完整推导过程，是事后分析「模型怎么想的」的原始材料；`ensure_ascii=False` 让 LaTeX 原文可读。

**命令行参数**（[ch04/02_math500-inference-scaling-scripts/cot_prompting_math500.py:L95-L138](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch04/02_math500-inference-scaling-scripts/cot_prompting_math500.py#L95-L138)）：

| 参数 | 默认 | 用途 |
|---|---|---|
| `--device` | `auto` | `auto` 走 `get_device()` 自动探测，否则接受任意 torch 设备串 |
| `--which_model` | `base` | `base` / `reasoning` / `instruct` 三选一（见下方映射） |
| `--dataset_size` | `10` | 取 MATH-500 **前 N 题**做子集评测——快速实验的旋钮 |
| `--max_new_tokens` | `2048` | 生成上限。默认给到 2048 正是为 CoT 长推理留空间（第 3 章版本默认 512） |
| `--compile` | 关 | 开启 `torch.compile`（见 u3-l3） |
| `--prompt_suffix` | `""` | **本讲主角**：空串即基线，传 CoT 后缀即实验组 |
| `--verbose` | 关 | 逐题打印提取结果与当前对题数 |

**instruct 变体映射**（[ch04/02_math500-inference-scaling-scripts/cot_prompting_math500.py:L160-L171](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch04/02_math500-inference-scaling-scripts/cot_prompting_math500.py#L160-L171)）：命令行的 `instruct` 并没有对应的第三套权重——它被映射为 `reasoning` 权重，但在加载后追加一行 `tokenizer.add_thinking = False`。回忆 u2-l6：`add_thinking` 决定聊天模板是否预填一个空的 `<think>` 块；关掉它，reasoning 模型就被当成「不带思考标签的指令模型」用。这是用现有两套权重模拟第三种模型形态的小技巧。

**评测调用**（[ch04/02_math500-inference-scaling-scripts/cot_prompting_math500.py:L176-L185](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch04/02_math500-inference-scaling-scripts/cot_prompting_math500.py#L176-L185)）：`math_data[:dataset_size]` 切片控制规模，`out_path` 用 f-string 拼成 `math500_{模型}-{设备}-evaluate-script.jsonl`。**注意：文件名里没有 prompt_suffix 的信息**——先后跑基线和 CoT 两次，第二次会覆盖第一次的日志（综合实践中会处理这个问题）。

#### 4.2.4 代码实践

**实践目标**：通读脚本参数表，学会用 `--help` 与子集参数控制实验成本。

1. **操作步骤**：

```bash
# 1) 查看全部参数及默认值（ArgumentDefaultsHelpFormatter 会把默认值印出来）
python ch04/02_math500-inference-scaling-scripts/cot_prompting_math500.py --help

# 2) 最小冒烟：1 题、128 token，验证权重下载与整条链路能跑通
python ch04/02_math500-inference-scaling-scripts/cot_prompting_math500.py \
    --which_model base --dataset_size 1 --max_new_tokens 128
```

2. **需要观察的现象**：`--help` 输出中各参数的默认值；冒烟运行时控制台的 `Model:`/`Device:` 打印、单行刷新的 ETA 进度，以及结束时的三行汇总（Accuracy / Total time / Logs written to）。
3. **预期结果**：冒烟运行在 CPU 上几分钟内完成（128 token 上限、单题），生成一个 `math500_base-cpu-evaluate-script.jsonl`（设备名随实际硬件变化）。准确率单题无统计意义，只看链路通不通。**待本地验证**。
4. 打开 jsonl 文件对照 4.2.3 的七字段结构，确认 `generated_text` 里是否有 `\boxed{}`、`extracted` 是否取到了 boxed 内容。

#### 4.2.5 小练习与答案

**练习 1**：脚本的 `evaluate_math500_stream` 相对 `ch03.py` 原版改了什么、删了什么？

**答案**：改了两处——函数签名新增 `prompt_suffix=""` 参数（L32），循环内 `render_prompt` 之后追加 `prompt += prompt_suffix`（L46），两处都带 `# NEW` 注释。删了一处——第 3 章版本的 `total_len` 累计与「Average response length」输出（ch03.py:L519、L530、L571-L572）没被带过来。其余（四步链路、jsonl 字段、ETA 进度、汇总打印）逐行一致。

**练习 2**：`--dataset_size 10` 是随机抽 10 题吗？这样做对照实验有什么隐患？

**答案**：不是随机——`math_data[:dataset_size]` 取的是数据集**前 10 题**（顺序切片）。隐患有二：其一，MATH-500 的题目按类型/难度分布未必均匀，前 10 题可能是同类题，子集准确率对方差极大；其二，10 题的准确率粒度是 10%，单题翻转就是 10 个百分点。结论方向可信、具体数字不可信——正式结论应以全量 500 题为准（README 表格即是）。

**练习 3**：为什么这个脚本的 `--max_new_tokens` 默认值（2048）比第 3 章评测函数的默认值（512）大？

**答案**：因为 CoT 模式下模型要先生成完整推导再给 `\boxed{}`，几百到上千 token 是常态；若上限太紧，生成会在推出答案**之前**被截断，`extract_final_candidate` 只能回退到「全文最后一个数字」，把中间量当最终答案，判分几乎必错——截断把「会做但没说完」误伤成「做错」。预填给足空间是 CoT 实验的隐性成本之一（更长的生成 = 更久的时间）。

### 4.3 结果统计：收益、代价与基准解读

#### 4.3.1 概念说明

推理时扩展没有免费的午餐：**准确率的每一分提升都用推理计算量买来**。CoT 的账本尤其清晰——它买准确率的方式是「把一次回答变长」，因此代价直接体现在生成 token 数与评测时长上。读基准表格时要同时看两列：Accuracy 和 Time。

#### 4.3.2 核心流程

来自 README（全量 500 题、DGX Spark GPU 上的实测，[README.md:L39-L46](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch04/02_math500-inference-scaling-scripts/README.md#L39-L46)）：

| 行 | 方法 | 模型 | 准确率 | 耗时 | 相对基线 |
|---|---|---|---|---|---|
| 1 | 基线（第 3 章，贪心） | Base | 15.2% | 10.1 min | — |
| 2 | 基线（第 3 章，贪心） | Reasoning | 48.2% | 182.1 min | 权重不同，不可比 |
| 3 | **CoT 提示** | Base | **40.6%** | **84.5 min** | 准确率 ×2.67，耗时 ×8.37 |

收益与代价的换算：

\[
\text{准确率增幅} = \frac{40.6 - 15.2}{15.2} \approx 167\%,\qquad
\text{耗时增幅} = \frac{84.5 - 10.1}{10.1} \approx 737\%
\]

耗时多 8 倍多的直接原因：基线模式下模型常常几个 token 就报出 `\boxed{20}` 然后停在 EOS；CoT 模式下每题要生成几百 token 推导。生成耗时近似正比于生成 token 数，所以「时间倍率 ≈ 平均回答长度倍率」。

再看组合实验（同表 L83-L97，[README.md:L83-L97](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch04/02_math500-inference-scaling-scripts/README.md#L83-L97)）里一个反直觉的数据点：

| 行 | 方法 | 准确率 |
|---|---|---|
| 3 | CoT（贪心） | 40.6% |
| 8 | Top-p 采样 + CoT | 33.4% |
| 11 | 自一致性(n=10) + Top-p + CoT | 52.0% |

把 u5-l1/u5-l2 的采样（temperature 0.9 + top_p 0.9）叠上去，0.6B base 上不升反降（40.6% → 33.4%）——小模型的单次采样推理链方差太大，一条链走偏就全错；直到用 10 次采样投票（第 11 行，52.0%）才把多样性转化回准确率。这条「CoT → CoT+采样 → CoT+采样+投票」的递进链正是 u5-l4 的内容。

#### 4.3.3 源码精读

**运行命令**（[README.md:L49-L62](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch04/02_math500-inference-scaling-scripts/README.md#L49-L62)）：README 给出的是全量 500 题命令 `python cot_prompting_math500.py --which_model "base" --dataset_size 500`——注意它**没有传 `--prompt_suffix`**，复现表格第 3 行的 CoT 结果需要自行追加 `--prompt_suffix "\n\nExplain step by step."`（同文件 L148-L156 的第 8 行命令里有示范）。README 同时注明（L45、L98）：表内数字是 DGX Spark（CUDA）全量 500 题实测，换硬件绝对时间不可比，准确率在同种子贪心解码下可复现。

**汇总输出**（[ch04/02_math500-inference-scaling-scripts/cot_prompting_math500.py:L87-L92](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch04/02_math500-inference-scaling-scripts/cot_prompting_math500.py#L87-L92)）：循环结束后打印准确率（对题数/总题数）、总耗时（分钟）与日志路径，并把三元组 `(num_correct, num_examples, acc)` 返回给调用方。配合 `--verbose`（L79-L85）可在评测中逐题查看 `Extracted / Expected / Correct so far`。

**结果在哪分析**：脚本没有平均响应长度统计（见 4.2 练习 1），但 jsonl 的 `generated_text` 字段保留了全文——用分词器 encode 即可补算平均长度，正好量化「CoT 让回答变长多少」（综合实践会做）。

#### 4.3.4 代码实践

**实践目标**：从 jsonl 日志补算「平均生成长度」，亲手量化 CoT 的代价。示例代码：

1. **操作步骤**：

```python
# 示例代码：对比两次运行的 jsonl（假设已分别重命名保存）
import json
from reasoning_from_scratch.ch03 import load_tokenizer_only

tok = load_tokenizer_only("base")

def summarize(path):
    rows = [json.loads(line) for line in open(path, encoding="utf-8")]
    acc = sum(r["correct"] for r in rows) / len(rows)
    avg_len = sum(len(tok.encode(r["generated_text"])) for r in rows) / len(rows)
    return acc, avg_len

for name in ["baseline.jsonl", "cot.jsonl"]:
    acc, avg_len = summarize(name)   # 路径按实际重命名调整
    print(f"{name}: 准确率 {acc:.0%}, 平均生成 {avg_len:.0f} tokens")
```

2. **需要观察的现象**：CoT 运行的平均生成长度应是基线的数倍到数十倍；长度倍率应与两次运行的 Total time 倍率大致同向。
3. **预期结果**：方向上「CoT 更长、更准、更慢」；具体倍率**待本地验证**（取决于题目与 max_new_tokens 上限——上限太小会压缩 CoT 的长度差异并误伤其准确率）。
4. 进一步：挑一道两次运行判定不同的题，并排读两段 `generated_text`，观察基线是否「秒答」、CoT 是否分步——把 4.1.2 的单题对照在自己跑的数据上复现。

#### 4.3.5 小练习与答案

**练习 1**：CoT 准确率 ×2.67 而耗时 ×8.37。如果只看「每分钟换来的准确率」，CoT 划算吗？

**答案**：单位时间收益确实下降了：基线约 1.5 个百分点/分钟（15.2/10.1），CoT 约 0.48 个百分点/分钟（40.6/84.5）。但这个算法对基线不公平——基线在 15.2% 处已接近该模型「直接报答案」能力的上限，再多花时间也涨不动；CoT 买到的是基线买不到的区间。推理时扩展方法应按「能否突破基线上限」比较，而非单纯按性价比。

**练习 2**：同为「不改权重的扩展」，CoT（本讲）与自一致性投票（u5-l4 预告）买准确率的「付款方式」有何不同？

**答案**：CoT 把计算花在**单条回答的长度**上——一次生成、更深推理；自一致性把计算花在**回答的条数**上——同题采样 n 次、投票定案。前者对抗「想得不够深」，后者对抗「单次采样方差」。表格第 9-11 行显示两者叠加（长链 × 多次投票）在 0.6B 上把准确率推到 48%-52%，代价是耗时再乘 n。

**练习 3**：为什么第 2 行（Reasoning 模型基线，48.2%）不能和第 3 行（Base + CoT，40.6%）直接归因为「CoT 不如好权重」这么简单？

**答案**：可以比较但要认清变量：两行确实都用贪心解码、同一评测链路，差异是「权重 + 分词器配置」。但 Reasoning 模型的 48.2% 本身就是「思考被训练进权重」的成果——它生成 `<think>` 长推理（182.1 min 的耗时已经暴露了这一点），等价于「内置 CoT」。所以这行对比的真正读法是：**提示工程能追平一部分权重训练的差距（40.6% vs 48.2%），但后者的思考能力更稳、且不依赖提示技巧**——这正是第 6-8 章（GRPO、蒸馏）要把推理能力写进权重的动机。

## 5. 综合实践

**任务：在 10 题子集上完成「基线 vs CoT vs 自设计模板」三方对照实验。**

把本讲三个模块（模板注入、评测循环、结果统计）串成一次完整实验：

1. **准备**：确认依赖已装（u1-l2）、首次冒烟跑通（4.2.4）。**关键操作**——脚本输出的 jsonl 文件名固定（`math500_base-<设备名>-evaluate-script.jsonl`，不含后缀信息），每次运行结束立即重命名存档，例如 `run1-baseline.jsonl`，否则下一轮会覆盖。

2. **三次运行**（CPU 可把 `--max_new_tokens` 降到 256 控制时长；GPU 可保持 512）：

```bash
cd ch04/02_math500-inference-scaling-scripts

# 运行一：基线（无后缀）
python cot_prompting_math500.py --which_model base \
    --dataset_size 10 --max_new_tokens 512
mv math500_base-*-evaluate-script.jsonl run1-baseline.jsonl

# 运行二：标准 CoT 后缀
python cot_prompting_math500.py --which_model base \
    --dataset_size 10 --max_new_tokens 512 \
    --prompt_suffix "\n\nExplain step by step."
mv math500_base-*-evaluate-script.jsonl run2-cot.jsonl

# 运行三：你自己的模板变体，例如——
python cot_prompting_math500.py --which_model base \
    --dataset_size 10 --max_new_tokens 512 \
    --prompt_suffix "\n\nThink carefully and check your work before answering."
mv math500_base-*-evaluate-script.jsonl run3-custom.jsonl
```

3. **统计**：用 4.3.4 的 `summarize` 对三个文件各算准确率与平均生成长度，连同脚本打印的 Total time 填入对照表：

| 运行 | 后缀 | 准确率 | 平均生成 token | 耗时 |
|---|---|---|---|---|
| 1 | （无） | ? | ? | ? |
| 2 | `\n\nExplain step by step.` | ? | ? | ? |
| 3 | （自设计） | ? | ? | ? |

4. **分析与预期**：预期运行二比运行一长且更准（方向上复现 README 第 1、3 行；10 题子集数字波动大，允许不一致，此时翻 jsonl 找具体哪题翻转了）。运行三的结果**待本地验证**——若你的后缀诱导出更长的「验算」行为，长度可能再涨；若模板与 `\boxed{}` 契约冲突（例如让模型「只回答是/否」），准确率可能反而崩掉，那也是有价值的观察。
5. **反思题**（写进实验记录）：三次运行的 10 题是同一批吗（对照 `--dataset_size` 的切片语义）？你的结论能推广到 500 题吗？若不能，缺什么？

## 6. 本讲小结

- CoT 提示是**最便宜的推理时扩展**：不训练、不采样（本脚本解码全程贪心），全部改动浓缩为一个 `--prompt_suffix` 参数，经 `prompt += prompt_suffix` 拼在 `render_prompt` 模板（以 `Answer:` 结尾）之后。
- 机制：后缀把模型的条件分布从「直接报答案」拨到预训练语料中大量存在的「分步解题」文体上——把每题一层前向的「心算」变成几百 token 的「打草稿」。
- 实测收益与代价（500 题、GPU）：15.2%→40.6%（×2.67），10.1→84.5 min（×8.37）；代价根源是回答变长，`--max_new_tokens` 默认也因此从第 3 章的 512 提到 2048，上限不足会把「没说完」误伤成「答错」。
- 脚本结构是「复制-改造」：`evaluate_math500_stream` 从 ch03.py 复制而来，仅加 `prompt_suffix` 参数与拼接两行（`# NEW` 标注）、删去平均长度统计；`instruct` 变体是 reasoning 权重 + `tokenizer.add_thinking = False` 的模拟。
- 两个实验卫生要点：`--dataset_size` 是**前缀切片**而非随机抽样，小子集数字只看方向；输出 jsonl 文件名不含后缀信息，连续运行需手动重命名防覆盖。
- 组合基准揭示递进路线：CoT+采样在 0.6B 上反而掉分（40.6%→33.4%），需自一致性投票（n=10 达 52.0%）才能把采样方差转化回准确率——这是下一讲的入场券。

## 7. 下一步学习建议

- **u5-l4（自一致性投票）**：精读 `self_consistency_vote`——温度 0.8 + top_p 0.9 采多条推理链、`extract_final_candidate` 提短答案、`Counter` 计票与平票处理；对照 README 表格第 9-11 行，理解「为什么投票能把 CoT+采样的 33.4% 救到 52.0%」。
- **回看 ch04_main.ipynb 4.6 节**：notebook 里 CoT 后缀与自一致性的组合演示（`self_consistency_vote` 传 `prompt + "\n\nExplain step by step."`）正是两讲的衔接点。
- **向后眺望**：本讲靠提示「借」来思考能力，第 5 章（u6）用打分器自动择优，第 6-7 章（单元七、八）用 GRPO 把思考直接训练进权重（`reward_format` 连 `<think>` 标签的顺序都要奖励），第 8 章（单元九）用蒸馏从教师模型 copying 思维链——四条路线共享同一个 MATH-500 评测底座（u4-l4）。
- **源码延伸**：通读 [ch04/02_math500-inference-scaling-scripts/run_all_experiments_math500.sh](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch04/02_math500-inference-scaling-scripts/run_all_experiments_math500.sh)，看作者如何用一段 bash 编排 README 表格第 4-12 行的全部实验——这正是本讲综合实践的工业化版本。
