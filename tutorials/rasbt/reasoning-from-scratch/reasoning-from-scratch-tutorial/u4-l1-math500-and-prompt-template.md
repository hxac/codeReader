# MATH-500 数据集与提示模板

> 所属单元：单元四　评测与数学验证器（第 3 章）
> 讲义 ID：u4-l1　|　前置讲义：u3-l1（设备选择与贪心解码）

## 1. 本讲目标

第 2 章我们学会了让模型「说话」（生成文本），第 3 章要回答一个更难的问题：模型说得**对不对**？这需要三样东西：一个待评测的模型、一批有标准答案的题目、一套把题目「翻译」给模型的提示模板。本讲聚焦这三样东西的源码入口，学完后你应该能够：

1. 说出 MATH-500 数据集每条记录的字段结构（`problem` / `solution` / `answer` / `subject` / `level` / `unique_id`），并解释 `load_math500_test` 的「本地优先 + GitHub 回退」加载逻辑。
2. 解释 `render_prompt` 的 `\boxed{ANSWER}` 输出契约为什么是整条评测管线的地基。
3. 使用 `load_model_and_tokenizer` 统一加载 base 与 reasoning 两种模型变体，说清二者在分词器配置上的关键差异。
4. 用实验数据（作者在 notebook 中报告的结果）说明一个反直觉的事实：**只改几个词的提示模板，能让评测分数波动 20%→70%**，从而理解「评测协议本身就是变量」。

本讲不涉及答案提取与判分（那是 u4-l2、u4-l3 的内容），但会让读者看清：判分管线的每一步都依赖本讲建立的「数据 + 模板 + 模型」三件套。

## 2. 前置知识

- **评测集（benchmark）**：一组带标准答案（ground truth）的公开题目。数学题特别适合评测推理能力：答案客观唯一（\(\frac{14}{3}\) 与 `4.67` 是否等价可以机器判定），不需要人工打分。
- **MATH-500**：从 MATH 竞赛题数据集的测试集中精选的 500 道题，由 Hugging Face 以 [HuggingFaceH4/MATH-500](https://huggingface.co/datasets/HuggingFaceH4/MATH-500) 托管。本仓库已把它转存为一份静态 JSON，随仓库分发。
- **LaTeX 数学记法**：题目与答案大量使用 LaTeX，如 `\frac{14}{3}`（分数）、`\sqrt{2}`（根号）、`\boxed{...}`（答案框）。`\boxed` 是数学排版中「把最终答案框起来」的惯例，本项目的判分管线正是围绕它构建的。
- **提示模板（prompt template）**：把「角色设定 + 作答指令 + 题目 + 作答起点」拼成固定格式文本的函数。对 base 模型（续写型）与 reasoning 模型（指令微调型），同一模板的效果可能截然不同——这是本讲的核心看点。
- **zero-shot 提示**：不在提示里给示例，直接让模型作答。本讲评测全部是 zero-shot。
- **回顾**（来自前置讲义）：`download_qwen3_small` 按 `kind` 下载 base/reasoning 两套权重与分词器（u1-l3）；`Qwen3Tokenizer` 的 `apply_chat_template` / `add_generation_prompt` / `add_thinking` 四个开关（u2-l6）；`generate_text_basic_stream_cache` 流式缓存生成（u3-l1、u3-l2）；`torch.compile` 与 `allow_unspec_int_on_nn_module`（u3-l3）。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `reasoning_from_scratch/ch03.py` | 第 3 章可复用实现：模型加载、生成封装、答案提取、归一化、判分、评测循环 | `load_model_and_tokenizer`（L52-L90）、`load_math500_test`（L423-L442）、`render_prompt`（L413-L420），以及消费它们的 `mini_eval_demo`、`evaluate_math500_stream` |
| `ch03/01_main-chapter-code/ch03_main.ipynb` | 第 3 章教学 notebook，逐节演算并记录了真实运行输出 | 3.2 节（模型加载）、3.8 节（数据集来源与字段）、3.9 节（模板对比实验及各设备准确率表） |
| `ch03/01_main-chapter-code/math500_test.json` | MATH-500 数据集的本地副本，500 条记录 | 字段结构（L2-L9 为第一条记录） |

> 注：notebook 在 GitHub 上以交互形式渲染，行号锚点不稳定，因此本文对 notebook 只给文件级永久链接，用「节号 + 单元内容」定位；对 `.py` 与 `.json` 文件给精确到行的永久链接。所有链接均指向当前 HEAD `b864602`。

## 4. 核心概念与源码讲解

本讲的评测主链路可以概括为四步（出自 `mini_eval_demo` 的注释）：

```text
1. render_prompt(row["problem"])   # 套提示模板          ← 本讲 4.3
2. generate_text_stream_concat     # 模型生成回答        ← u3 回顾 + 本讲 4.1 的模型
3. extract_final_candidate         # 从生成文本提取答案  ← u4-l2
4. grade_answer                    # 数学等价判分        ← u4-l3
```

准确率定义为：

\[ \mathrm{Acc} = \frac{1}{N}\sum_{i=1}^{N} \mathbb{1}\,[\,\hat{y}_i \equiv y_i\,] \]

其中 \(\hat{y}_i\) 是模型答案、\(y_i\) 是数据集 `answer` 字段的标准答案，「\(\equiv\)」不是字符串相等，而是 u4-l3 将讲的**符号等价**。本讲负责把 \(\hat{y}\) 的生产原料（模型、模板、题目）准备妥当。

### 4.1 评测前的统一模型入口：load_model_and_tokenizer

#### 4.1.1 概念说明

第 3 章要评测的对象有两种：

- **base 模型**（`qwen3-0.6B-base.pth`）：仅预训练的「续写机器」，官方建议**不套**聊天模板直接喂裸文本；
- **reasoning 模型**（`qwen3-0.6B-reasoning.pth`）：经过指令微调与推理训练的版本，**必须**套聊天模板并开启 thinking 标签才能发挥实力（回顾 u2-l6 的四种包装形态）。

如果每处评测代码都手写一遍「下载权重 → 选分词器 → 配模板 → 建模型 → 加载权重 → 搬设备 → 可选编译」，既啰嗦又容易在两套变体间抄错配置。`load_model_and_tokenizer` 把这些差异**封装成一个 `which_model` 参数**：调用方只说「我要 base 还是 reasoning」，其余全部自动对齐。这也是第 4~5 章所有实验脚本复用的公共入口。

#### 4.1.2 核心流程

```text
load_model_and_tokenizer(which_model, device, use_compile)
├── which_model == "base"
│     ├── download_qwen3_small(kind="base")        # 权重+分词器（已存在则跳过）
│     ├── tokenizer = Qwen3Tokenizer(tokenizer-base.json)   # 裸文本模式
│     └── model_path = qwen3-0.6B-base.pth
├── which_model == "reasoning"
│     ├── download_qwen3_small(kind="reasoning")
│     ├── tokenizer = Qwen3Tokenizer(tokenizer-reasoning.json,
│     │         apply_chat_template=True,           # 套 <|im_start|> 消息块
│     │         add_generation_prompt=True,         # 预置 assistant 生成头
│     │         add_thinking=True)                  # 让模型自写 <think> 段
│     └── model_path = qwen3-0.6B-reasoning.pth
├── else → raise ValueError                         # 非法取值就地报错
├── model = Qwen3Model(QWEN_CONFIG_06_B)            # 两种变体共享同一架构
├── model.load_state_dict(torch.load(model_path))   # 唯一的区别只是权重文件
├── model.to(device)
├── use_compile → torch.compile(model)              # 先搬设备再编译（u3-l3 结论）
└── return model, tokenizer
```

关键认知：**两个变体的网络结构完全相同**（同一个 `QWEN_CONFIG_06_B`），差异全部体现在权重文件与分词器配置两处。这正是 u2-l1「配置驱动」设计的红利。

#### 4.1.3 源码精读

入口与 base 分支（下载 + 裸文本分词器）：

[reasoning_from_scratch/ch03.py:L52-L61](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L52-L61) — 按 `which_model` 分发；base 分支调用 `download_qwen3_small(kind="base")`（缓存命中时打印 already up-to-date 后直接返回，见 u1-l3），分词器用 `tokenizer-base.json`，**不传任何模板开关**。

reasoning 分支（三个模板开关全开）：

[reasoning_from_scratch/ch03.py:L63-L76](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L63-L76) — reasoning 分支除换权重与分词器文件外，还以 `apply_chat_template=True, add_generation_prompt=True, add_thinking=True` 构造分词器。对照 u2-l6：这对应「生成提示 + 模型自写思考段」的形态，是推理模型的标准作答姿势。

兜底报错与共享装配段：

[reasoning_from_scratch/ch03.py:L78-L90](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L78-L90) — 非法 `which_model` 抛 `ValueError`（把参数错误拦在下载之前）；随后两种变体走同一段代码：`Qwen3Model(QWEN_CONFIG_06_B)` 建骨架 → `load_state_dict(torch.load(model_path))` 灌权重 → `to(device)` 搬设备 → 可选 `torch.compile`。注意编译发生在搬设备**之后**，与 u3-l3 给出的推荐次序一致；编译前设置的 `torch._dynamo.config.allow_unspec_int_on_nn_module = True` 用于抑制 KV 游标整数属性引发的反复重编译（细节见 u3-l3）。

配套的轻量版（只要分词器、不下载约 1.2 GB 的权重）：

[reasoning_from_scratch/ch03.py:L93-L118](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L93-L118) — `load_tokenizer_only` 与上面两个分支逻辑相同，但传 `tokenizer_only=True`，适合只研究文本处理、不跑模型的场合（下面的代码实践会用到它）。

notebook 中与此完全对应的单元（3.2 节）及一段重要提醒见 [ch03/01_main-chapter-code/ch03_main.ipynb](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch03/01_main-chapter-code/ch03_main.ipynb)：作者建议先用 `which_model="base"` 学完全章，再切 `"reasoning"` 重跑一遍对比。

#### 4.1.4 代码实践

**实践目标**：不下载大权重，仅用分词器验证 base 与 reasoning 两种配置对同一句话的编码差异。

1. 操作步骤（示例代码，运行前确认已完成 u1-l2 的安装）：

   ```python
   # practice_tokenizer_configs.py（示例代码）
   from reasoning_from_scratch.ch03 import load_tokenizer_only

   for kind in ("base", "reasoning"):
       tok = load_tokenizer_only(kind)          # 只下载/复用分词器 JSON
       ids = tok.encode("What is 2+2?")
       print(f"[{kind}] eos_token_id = {tok.eos_token_id}")
       print(f"[{kind}] 首 5 个 token id: {ids[:5]}")
       print(f"[{kind}] decode 回文: {tok.decode(ids)!r}")
       print("-" * 40)
   ```

2. 需要观察的现象：两套配置的 `eos_token_id` 是否不同（回顾 u2-l6：base 用 `<|endoftext|>`，reasoning 用 `<|im_end|>`）；reasoning 版 `encode` 的输出应包含聊天模板的特殊 token（如 `<|im_start|>`），decode 回来会看到完整的消息块包装，而 base 版是裸文本。
3. 预期结果：base 输出即原文；reasoning 输出被 `<|im_start|>user ... <|im_end|>` 等结构包裹，且因 `add_generation_prompt=True` 末尾出现 assistant 生成头、因 `add_thinking=True` 不预填 `<think>`（这两种 thinking 形态的区别见 u2-l6）。具体 token 序列**待本地验证**。
4. 注意：首次运行会各下载一个几 MB 的分词器 JSON 到 `qwen3/` 目录；若网络受限，可参考 troubleshooting.md（u1-l2）。

#### 4.1.5 小练习与答案

1. **问**：为什么 `load_model_and_tokenizer` 里两种模型共用 `QWEN3Model(QWEN_CONFIG_06_B)`，却不需要任何 if/else？
   **答**：base 与 reasoning 变体的**架构和尺寸完全一致**，区别只在权重文件内容与分词器配置。结构由配置字典驱动（u2-l1），所以建骨架的代码可以共享，分支只需选对 `model_path` 和分词器参数。
2. **问**：把 `use_compile=True` 但你的机器是 MPS 且 `torch.compile` 报 `InductorError`，该怎么办？
   **答**：直接退回 `use_compile=False`。u3-l3 讲过编译是可选优化而非功能依赖，MPS 上 Inductor 后端可能不成熟；评测结论不受影响，只是吞吐降低。
3. **问**：如果误传 `which_model="chat"`，函数在哪一行、以什么方式失败？
   **答**：在 [ch03.py:L78-L79](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L78-L79) 抛出 `ValueError(f"Invalid choice: which_model={which_model}")`，且发生在任何下载动作之前——参数校验前置，避免浪费带宽。

### 4.2 MATH-500 数据集：load_math500_test 与字段结构

#### 4.2.1 概念说明

评测要有「考卷」。MATH-500 的 500 道题覆盖代数、数论、预积分等多个学科、难度分 1~5 级，每道题带人工解答与最终答案。它对本项目有三重意义：

1. **答案可验证**：`answer` 字段是简短的标准答案（如 `\frac{14}{3}`、`9`、`\text{Evelyn}`），使 u4-l3 的符号等价判分成为可能——这正是第 6 章 RLVR「可验证奖励」的雏形。
2. **静态可复现**：数据以 JSON 固定随仓库分发，评测结果不随外部数据集版本漂移。
3. **题目形态真实**：题干含 LaTeX、甚至含 `[asy]` 几何作图代码，逼着评测管线处理「脏」输入。

加载函数 `load_math500_test` 要解决的问题是：**第一次运行没数据、之后每次运行不该重新下载**。它的策略是「本地文件存在即用，否则从 GitHub 拉取并落盘副本」。

#### 4.2.2 核心流程

```text
load_math500_test(local_path="math500_test.json", save_copy=True)
├── local_path 存在？
│     ├── 是 → json.load(本地文件)               # 命中缓存，零网络请求
│     └── 否 → requests.get(GitHub raw URL, timeout=30)
│               ├── raise_for_status()            # 4xx/5xx 立即抛错
│               ├── r.json()
│               └── save_copy=True → 写回 local_path（indent=2）
└── return data                                   # 500 个 dict 组成的 list
```

两条值得注意的设计边界：

- 缓存判据**只有文件存在性**（`Path.exists()`），不校验大小或内容完整性。对比 u1-l3 的 `download_file`（以本地大小与 `Content-Length` 严格相等作为命中条件），这里的校验更弱——若本地 JSON 被截断，`json.load` 会直接抛解析错误而不是重新下载。对于一份随仓库分发、几乎不会被写坏的小文件，这是够用且更简单的取舍。
- 远程 URL 指向仓库 **main 分支**的这份 JSON，本质上是「首次运行的数据源」，而非热更新通道；一旦落盘，之后永远读本地。

#### 4.2.3 源码精读

加载函数本体：

[reasoning_from_scratch/ch03.py:L423-L442](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L423-L442) — `load_math500_test`：先把 `local_path` 转 `Path`；存在则读本地；否则从 GitHub raw 地址（指向本仓库 main 分支的 `math500_test.json`）请求、`raise_for_status()` 保证 HTTP 错误显式失败、`save_copy=True` 时以 `indent=2` 写回本地。返回解析后的 JSON 对象（list[dict]）。

第一条数据记录（字段全景）：

[ch03/01_main-chapter-code/math500_test.json:L2-L9](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch03/01_main-chapter-code/math500_test.json#L2-L9) — 第一条记录包含全部六个字段：`problem`（题干，含 LaTeX）、`solution`（人工解答，内含 `\boxed{...}`，**评测管线不使用它**）、`answer`（标准答案 `\left( 3, \frac{\pi}{2} \right)`）、`subject`（`Precalculus`）、`level`（`2`，整数 1~5）、`unique_id`（`test/precalculus/807.json`）。

各字段的消费者一览：

| 字段 | 类型 | 谁在用 |
| --- | --- | --- |
| `problem` | str | `render_prompt` 套模板后作为题目正文（见 4.3） |
| `answer` | str | `grade_answer` 的标准答案一侧（u4-l3） |
| `solution` | str | 无人消费（可理解为数据集原生字段，留作参考） |
| `subject` / `level` / `unique_id` | str / int / str | 分组分析用，主评测循环不读 |

notebook 3.8 节还附了这段 JSON 的**原始生成方式**（用 `datasets` 库从 HuggingFaceH4/MATH-500 拉取后转存，仅供追溯、无需执行），并打印了 `len(math_data) == 500` 与第一条记录的完整字典，见 [ch03/01_main-chapter-code/ch03_main.ipynb](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch03/01_main-chapter-code/ch03_main.ipynb)。

#### 4.2.4 代码实践

**实践目标**：亲手加载数据集，摸清答案字段的「脏」程度——这直接决定后续判分管线要做多少归一化工作。

1. 操作步骤（示例代码）：

   ```python
   # practice_math500_stats.py（示例代码）
   import re
   from collections import Counter
   from reasoning_from_scratch.ch03 import load_math500_test

   # 优先指向仓库自带副本，避免在当前目录再落一份
   data = load_math500_test("ch03/01_main-chapter-code/math500_test.json")
   print("总题数:", len(data))

   def classify(ans: str) -> str:
       a = ans.strip()
       if re.fullmatch(r"-?\d+(\.\d+)?", a):          # 优先判「纯数字」
           return "纯数字"
       if "\\frac" in a or re.search(r"\d+\s*/\s*\d+", a):
           return "含分数"
       if "\\sqrt" in a:
           return "含根号"
       return "其他"

   counts = Counter(classify(ex["answer"]) for ex in data[:100])
   total = sum(counts.values())
   for k in ("纯数字", "含分数", "含根号", "其他"):
       print(f"{k}: {counts[k]:3d}  ({counts[k]/total:.0%})")

   print("\n学科分布(全部 500 题):", dict(Counter(ex["subject"] for ex in data)))
   ```

2. 需要观察的现象：四个类别的比例；`其他` 类里都长什么样（建议再打印几条 `classify` 返回「其他」的答案原文，如坐标组 `\left( 3, \frac{\pi}{2} \right)`、文本 `\text{Evelyn}`、表达式 `p - q`——注意按上面的优先级规则它们会被计入「含分数」，这正是分类规则影响结论的活例子）；学科分布是否均衡。
3. 预期结果：比例数值**待本地验证**；可以确定的定性结论是：纯数字只占一部分，大量答案是 LaTeX 分数、根号、区间/坐标、甚至英文文本——这就是 u4-l2、u4-l3 要做归一化与符号判分的原因。
4. 缓存行为观察：把 `local_path` 换成一个不存在的路径（如 `"my_math500.json"`）再运行一次，第一次会发起网络请求并在当前目录生成 `my_math500.json`，第二次运行不再有下载日志、瞬间返回。若断网且本地无副本，`requests.get` 抛出的异常即失败路径（**待本地验证**）。

#### 4.2.5 小练习与答案

1. **问**：`load_math500_test` 的缓存命中条件与 u1-l3 的 `download_file` 有何不同？各自的适用场景是什么？
   **答**：前者只查 `Path.exists()`；后者还比对本地大小与 HTTP `Content-Length`，不匹配则覆盖重下。`download_file` 面向几百 MB~GB 的权重（残缺代价高，必须严格）；`load_math500_test` 面向随仓库分发的小 JSON（被写坏的概率低，简单优先）。
2. **问**：`solution` 字段里也有 `\boxed{...}`，评测管线为什么不用它？
   **答**：评测要判的是**模型**的答案，`answer` 才是标准答案；`solution` 是数据集自带的人工推导过程，对本项目的判分与奖励计算都不需要（第 8 章蒸馏生成教师数据时才会用到带推理过程的文本，且来源不同）。
3. **问**：为什么 `save_copy` 默认为 `True`？设为 `False` 会发生什么？
   **答**：默认落盘使得第二次运行起完全离线、结果可复现；设 `False` 后每次运行都要发起网络请求，离线环境直接失败，也不利于固定评测数据版本。

### 4.3 render_prompt：一行模板如何撬动 20%→70% 的分数

#### 4.3.1 概念说明

`render_prompt` 只有 8 行，却是整条评测管线「最便宜也最贵」的一环：便宜在零参数、零计算；贵在它**直接重塑模型的输入分布**，进而重塑所有下游数字。它同时承担三个职责：

1. **输出格式契约**：指令模型把最终结果写到新的一行、并套上 `\boxed{ANSWER}`。这不是排版洁癖——u4-l2 的 `get_last_boxed` 靠 `\boxed` 这个锚点从几百 token 的生成文本里**机器可解析**地抠出答案。没有这行指令，提取器只能退化为「找最后一个数字」的启发式。
2. **作答上下文**：`You are a helpful math assistant.` 的角色设定 + `Answer:` 的续写起点，把 base 模型引导进「数学解答」的语料分布。
3. **协议固定**：`evaluate_math500_stream` 对全部 500 题用同一模板，保证分数之间的可比性。

由此推出本讲最重要的观念：**提示模板是评测协议的一部分，不是无关紧要的包装纸**。改两个字（`Question` → `Problem`）就足以让同一模型、同一数据、同一判分器下的分数发生巨幅变化。

#### 4.3.2 核心流程

```text
render_prompt(prompt)
└── 返回五段拼接的固定格式字符串：
      "You are a helpful math assistant.\n"            # ① 角色设定
      "Answer the question and write the final result on a new line as:\n"
      "\\boxed{ANSWER}\n\n"                            # ② 输出格式契约（给提取器看的锚点）
      f"Question:\n{prompt}\n\n"                       # ③ 题目正文（唯一变量）
      "Answer:"                                        # ④ 作答起点（base 模型从这里续写）
```

模板经分词后进入生成函数，生成文本再交给提取器。三个环节环环相扣：

```text
render_prompt(problem) ──► tokenizer.encode ──► generate_text_basic_stream_cache
        ▲                                              │
        │                                              ▼
evaluate_math500_stream L524 调用          生成文本（理想情况下含 \boxed{...}）
                                                   │
                                                   ▼
                                     get_last_boxed / extract_final_candidate（u4-l2）
```

#### 4.3.3 源码精读

模板函数本体：

[reasoning_from_scratch/ch03.py:L413-L420](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L413-L420) — `render_prompt`：五段字符串常量 + f-string 拼接，`prompt` 是唯一变量。注意 `\\boxed{ANSWER}` 在 Python 源码中是转义写法，实际文本为 `\boxed{ANSWER}`。

评测主循环中的消费点：

[reasoning_from_scratch/ch03.py:L523-L529](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L523-L529) — `evaluate_math500_stream` 对每条记录执行四步：`render_prompt(row["problem"])` 套模板 → `generate_text_stream_concat` 生成（该封装函数见 [ch03.py:L121-L145](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L121-L145)，内部即 u3-l2 的流式缓存生成）→ 提取 → 判分。

四步管线的最小演示：

[reasoning_from_scratch/ch03.py:L445-L462](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L445-L462) — `mini_eval_demo` 用一道 `1/2 + 1/6` 的例题把「渲染 → 生成 → 提取 → 判分」走通，是理解模板位置的最好入口（notebook 中该演示在 base 模型上预测 `1/3`、判错，真实输出记录于 3.9 节）。

**模板敏感性实验**（notebook 3.9 节，作者记录的真实数据，见 [ch03/01_main-chapter-code/ch03_main.ipynb](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch03/01_main-chapter-code/ch03_main.ipynb)）。作者以注释形式给出两个替代模板及其在 MPS 设备、10 题子集上的实测影响：

| 模板 | 关键差异 | base 准确率 | reasoning 准确率 |
| --- | --- | --- | --- |
| 默认（`Question:`） | `Answer the question ...` | 20% | 90% |
| 替代 1（`Problem:`） | `Solve the problem ...`、`Question:` → `Problem:` | **40%**（↑20pt） | **60%**（↓30pt） |
| 替代 2（无模板） | 直接返回原题干 | **70%**（↑50pt） | **50%**（↓40pt） |

解读：base 是续写模型，`Problem:` + 简短题干的形态更接近它在预训练语料里见过的数学题分布（网上大量题目就长这样），所以越「素」越好；reasoning 是指令微调模型，脱掉结构化指令反而破坏了它训练时的输入格式，于是越「素」越差。**同一改动对两种模型的方向完全相反**——这就是「模板是协议变量」的最有力证据。同节还记录了完整 500 题的结果（base CUDA 15.6%、reasoning CUDA 48.2%）以及另一个惊人事实：给 base 模型的分词器加上 `apply_chat_template=True`，10 题准确率可冲到 80%（作者同时提醒 MATH-500 可能存在于训练数据中，数字需审慎解读）。

#### 4.3.4 代码实践

**实践目标**：亲眼看到模板的「拼接结构」，并对照 notebook 的替代模板写出结构化差异。

1. 操作步骤（示例代码）：

   ```python
   # practice_render.py（示例代码）
   from reasoning_from_scratch.ch03 import render_prompt, load_math500_test

   data = load_math500_test("ch03/01_main-chapter-code/math500_test.json")
   for i in range(3):
       print(f"{'='*30} 第 {i+1} 题 {'='*30}")
       print(render_prompt(data[i]["problem"]))
       print()

   # 替代模板（照抄 notebook 3.9 节，示例代码）
   def render_prompt_problem(prompt):
       return (
           "You are a helpful math assistant.\n"
           "Solve the problem and write the final result on a new line as:\n"
           "\\boxed{ANSWER}\n\n"
           f"Problem:\n{prompt}\n\nAnswer:"
       )

   print("两版模板 token 数对比：")
   # 若已下载分词器：tok = load_tokenizer_only("base")
   # print(len(tok.encode(render_prompt(data[0]["problem"]))),
   #       len(tok.encode(render_prompt_problem(data[0]["problem"]))))
   ```

2. 需要观察的现象：输出是「角色句 + 格式指令 + `\boxed{ANSWER}` 示例 + 题目 + `Answer:`」的固定骨架，三道题之间只有题目正文在变；默认版与 `Problem` 版逐词 diff，应只有两处不同（指令动词短语、题目标签）。
3. 预期结果：打印出的第一题 prompt 以 `You are a helpful math assistant.` 开头、以 `Answer:` 结尾；结构差异的观察记录**待本地验证**（如加载数词器，可顺带比较两版模板的 token 长度差）。
4. 写作任务：参照上表（notebook 实测数据），用 3~5 句话总结你观察到的两种模板差异，并回答：为什么把 `Question` 换成 `Problem` 对 base 是提升、对 reasoning 是伤害？

#### 4.3.5 小练习与答案

1. **问**：指令里「write the final result **on a new line**」对下游管线有什么实际意义？
   **答**：答案框通常独立成行，降低 `\boxed` 与题干文字黏连、被换行切断的概率，让 `get_last_boxed` 的文本扫描更稳；本质上是模板在为提取器的鲁棒性服务。
2. **问**：假设你把 `render_prompt` 改成直接 `return prompt`（无模板），评测分数反而从 20% 涨到 70%（base 模型）。能据此说无模板「更好」吗？
   **答**：只能说你找到了**对该模型更合适的协议**。评测分数永远以「模型 + 模板 + 判分器 + 数据」四元组为条件；换模板后推理模型的分数是下降的。正确的做法是报告分数时**同时固定并说明模板**，做模型间比较时用同一模板。
3. **问**：`render_prompt` 里 `\boxed{ANSWER}` 的 `ANSWER` 会被模型当作什么？
   **答**：它是格式示例占位符，向模型展示「把最终答案放进花括号」的写法；模型生成时会用自己的答案替换它。提取器只认 `\boxed{...}` 记号本身，不认 `ANSWER` 这个词。

## 5. 综合实践

把三个模块串成一个「评测原料检查台」脚本，全部离线可跑（不加载权重）：

1. **实践目标**：产出一份评测前的「数据 + 模板」体检报告，为 u4-l4 的端到端评测做好准备。
2. 操作步骤（示例代码）：

   ```python
   # u4l1_capstone.py（示例代码）
   import re
   from collections import Counter
   from reasoning_from_scratch.ch03 import (
       load_math500_test, render_prompt
   )

   # ── 步骤 1：数据体检 ──────────────────────────────
   data = load_math500_test("ch03/01_main-chapter-code/math500_test.json")
   assert len(data) == 500, "数据集应为 500 题"

   def classify(ans):
       a = ans.strip()
       if re.fullmatch(r"-?\d+(\.\d+)?", a):
           return "纯数字"
       if "\\frac" in a or re.search(r"\d+\s*/\s*\d+", a):
           return "含分数"
       if "\\sqrt" in a:
           return "含根号"
       return "其他"

   stats = Counter(classify(ex["answer"]) for ex in data[:100])
   print("[前 100 题答案形态]", dict(stats))
   print("[学科分布]", dict(Counter(ex["subject"] for ex in data)))
   print("[难度分布]", dict(sorted(Counter(ex["level"] for ex in data).items())))

   # ── 步骤 2：模板渲染 ──────────────────────────────
   for i in range(3):
       print(f"\n===== 第 {i+1} 题 prompt =====")
       print(render_prompt(data[i]["problem"]))

   # ── 步骤 3（可选，需已下载分词器）：token 开销对比 ──
   # from reasoning_from_scratch.ch03 import load_tokenizer_only
   # tok = load_tokenizer_only("base")
   # lens = [len(tok.encode(render_prompt(ex["problem"]))) for ex in data[:100]]
   # print(f"prompt token 数：min={min(lens)}, max={max(lens)}, "
   #       f"mean={sum(lens)/len(lens):.0f}")
   ```

3. 需要观察的现象：答案形态四分类的比例；学科与难度是否覆盖广泛；三段完整 prompt 的固定骨架；可选步骤中 prompt 的 token 长度分布（这决定后续 `max_new_tokens` 与上下文的预算）。
4. 预期结果：报告数字**待本地验证**；定性上你应得出三条结论——(a) 答案远不止纯数字，判分必须做归一化与符号等价；(b) 模板骨架固定、只有题干变化，这正是「协议」的含义；(c) 对照 notebook 的模板对比表，模板细节对最终分数的影响可达数十个百分点。
5. 进阶（需 GPU 或耐心，可选）：参考 u3-l2 的 `generate_text_basic_stream_cache`，把第 1 题的 prompt 喂给 base 模型生成 128 个 token，检查生成文本里是否出现 `\boxed{...}`——这是对「格式契约是否被遵守」的直接检验（**待本地验证**）。

## 6. 本讲小结

- `load_model_and_tokenizer`（[ch03.py:L52-L90](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L52-L90)）把 base/reasoning 两套「权重 + 分词器配置」封装成一个 `which_model` 参数；两变体共享同一架构，差异只在权重文件与是否启用聊天模板三开关。
- `load_math500_test`（[ch03.py:L423-L442](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L423-L442)采用「本地优先 + GitHub 回退 + 落盘副本」策略，返回 500 条含 `problem/solution/answer/subject/level/unique_id` 六字段的记录；评测只消费 `problem` 与 `answer`。
- `render_prompt`（[ch03.py:L413-L420](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L413-L420)）以 `\boxed{ANSWER}` 建立输出格式契约，为下游提取器提供机器可解析的锚点。
- 模板是评测协议的变量：notebook 实测中，`Question`→`Problem` 使 base 从 20%→40%、reasoning 从 90%→60%；完全去掉模板使 base 20%→70%、reasoning 90%→50%——同一改动对两种模型方向相反。
- 评测四步链路「渲染 → 生成 → 提取 → 判分」中，本讲交付前两步的全部原料；「提取」与「判分」是接下来两讲的主角。

## 7. 下一步学习建议

下一讲 **u4-l2《答案提取与文本归一化》** 将顺着本讲的格式契约往下走：`get_last_boxed` 如何扫描嵌套大括号抠出 `\boxed` 内容、`extract_final_candidate` 的三级回退策略、`normalize_text` 的十几条正则清洗规则——本讲综合实践中统计出的「含分数/含根号/其他」答案，正是那些规则要对付的对象。之后再进入 u4-l3 的 SymPy 符号等价判分与 u4-l4 的端到端评测循环。建议在进入下一讲前，务必完成本讲综合实践：它产出的数据体检报告会让你在阅读归一化规则时，对「每条规则为哪类答案服务」心中有数。
