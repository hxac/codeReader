# 精度评测的通用骨架：以 MMLU 为例

## 1. 本讲目标

学完本讲，你应当能够：

- 看懂 `benchmark/mmlu/bench_sglang.py` 这一份「样板」精度评测脚本的完整结构，并能把它当作模板套用到任意选择题/简答题数据集。
- 理解一个精度评测的五个固定环节：**准备数据 → 定义生成程序 → 选择后端 → 并发执行 → 统计并落盘**。
- 掌握四个被几乎所有 `bench_sglang.py` 复用的公共工具：`add_common_sglang_args_and_parse`、`select_sglang_backend`、`sgl.function`/`run_batch`、`dump_bench_raw_result`。
- 知道评测的「产物」是什么（`result.jsonl` 汇总行 + 可选的逐题 raw 结果），以及它们如何用于跨模型/跨后端对比。

承接上一讲：本讲默认你已经建立了「benchmark/ 下的脚本是**客户端**，它们去驱动一个由 `launch_server` 启动的**服务端**」这一心智模型（见 u1-l1）。本讲正是这种客户端的「标准模板」。

## 2. 前置知识

- **MMLU**：一个大规模多项选择题（multiple-choice）基准，覆盖 57 个学科，每题有 A/B/C/D 四个选项和一个正确答案。它是衡量模型「知识广度」的典型题库。
- **few-shot（少样本）提示**：在真正要回答的问题前，先放几道「带答案」的示例题，让模型学会答题格式。例如先给 5 道「…Answer: A」的例子，再给一道「…Answer:」让模型续写。
- **准确率（accuracy）**：预测答案与标准答案一致的比例。
- **SGL 程序（SGL program）**：SGLang 前端用 `@sgl.function` 装饰器描述的一段生成逻辑，可把多轮/多步生成组织成一个可重用的函数，再用 `run_batch` 一次性并发跑很多组输入。
- **后端（backend）**：SGL 程序最终把请求发到哪里：`srt` 表示发到运行中的 SGLang 服务（`RuntimeEndpoint`），`gpt-*` 表示发到 OpenAI 接口。
- **JSONL**：每行一个 JSON 对象的文本文件，是评测结果落盘的常用格式，便于后续逐行读取、对比、画图。

一个直觉性的结论先放这里：**一份精度评测脚本，本质上就是「把数据集里的每一道题，变成一个 prompt，丢给一个后端并发去跑，再算答对率」**。理解了这个骨架，后面 gsm8k、hellaswag、ceval 等几十个 `bench_sglang.py` 都只是换了「数据怎么读、prompt 怎么拼、答案怎么抽」这三个地方。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [benchmark/mmlu/bench_sglang.py](https://github.com/sgl-project/sglang/blob/4eaa5ca6510622cb0006bcfee5947b17859ac8c7/benchmark/mmlu/bench_sglang.py) | 本讲的主角：一个完整的 MMLU 精度评测脚本，是整个 benchmark/ 家族的样板。 |
| [benchmark/mmlu/README.md](https://github.com/sgl-project/sglang/blob/4eaa5ca6510622cb0006bcfee5947b17859ac8c7/benchmark/mmlu/README.md) | 「可复现的操作手册」：如何起服务、如何跑 sglang / vllm / lightllm / guidance / lmql。 |
| [python/sglang/test/test_utils.py](https://github.com/sgl-project/sglang/blob/4eaa5ca6510622cb0006bcfee5947b17859ac8c7/python/sglang/test/test_utils.py) | 公共工具库：`add_common_sglang_args_and_parse`、`select_sglang_backend`、`dump_bench_raw_result` 都定义在这里，被各 `bench_sglang.py` 复用。 |
| [python/sglang/lang/ir.py](https://github.com/sgl-project/sglang/blob/4eaa5ca6510622cb0006bcfee5947b17859ac8c7/python/sglang/lang/ir.py) | SGL 程序的 `run_batch` 方法定义处，用于理解并发执行与返回值结构。 |
| [python/sglang/lang/backend/runtime_endpoint.py](https://github.com/sgl-project/sglang/blob/4eaa5ca6510622cb0006bcfee5947b17859ac8c7/python/sglang/lang/backend/runtime_endpoint.py) | `RuntimeEndpoint` 后端：负责把请求发到运行中服务的 `/generate` 端点。 |

## 4. 核心概念与源码讲解

本讲拆成 5 个最小模块。其中 **4.2 / 4.3 / 4.4 / 4.5** 对应任务要求的四个公共工具，**4.1** 是它们共同依赖的数据准备环节（也是本讲代码实践的着手点）。

一个精度评测脚本的主流程可以用下面这段伪代码概括（对应 `main(args)` 的执行顺序）：

```
读取命令行参数与数据集
  → 为每道题拼出 few-shot prompt（arguments 列表 + labels 列表）
  → 用 @sgl.function 定义生成程序 few_shot_mmlu
  → select_sglang_backend(args) 选后端
  → few_shot_mmlu.run_batch(arguments, backend=..., num_threads=...) 并发跑
  → 从每条结果抽出预测答案，与 labels 比对得准确率
  → dump_bench_raw_result(...) 落逐题结果；再写一行汇总到 result.jsonl
```

### 4.1 数据准备：MMLU 数据集与 few-shot prompt 构造

#### 4.1.1 概念说明

这一模块解决的是「**题库从哪来、prompt 长什么样**」。MMLU 把每个学科拆成三个 CSV：`dev`（少量示例，用来做 few-shot）、`test`（正式评测题）、`val`。CSV 没有表头，列依次是：`题目, 选项A, 选项B, 选项C, 选项D, 正确答案`。

few-shot prompt 的拼法是：开头一句说明「以下是关于某学科的带答案选择题」，然后塞 `k` 道**带答案**的示例题，最后接一道**不带答案**的待答题，模型只需续写一个字母。

#### 4.1.2 核心流程

1. 若本地没有 `data/test/`，调用 `download_data` 用 `wget` 下载并解压 tar 包。
2. 遍历每个学科（最多 `--nsub` 个）：读 `dev` 和 `test` 两个 CSV。
3. 用 `gen_prompt` 把 `dev` 的前 `k` 道题拼成 few-shot 前缀；若 token 数超过 1536 就减少 `k`。
4. 对 `test` 里每一道题，用 `format_example(..., include_answer=False)` 拼出待答题尾巴，与 few-shot 前缀组合成一条 `arguments`，同时记下它的正确答案到 `labels`。

#### 4.1.3 源码精读

下载与解压（用 `subprocess` 调 `wget`，再用 `tarfile` 解压，并把多余的 `data/` 嵌套层提上来）：

[benchmark/mmlu/bench_sglang.py:L55-L74](https://github.com/sgl-project/sglang/blob/4eaa5ca6510622cb0006bcfee5947b17859ac8c7/benchmark/mmlu/bench_sglang.py#L55-L74) —— 定义 `download_data`：检测到 `data/test` 已存在就直接返回，否则下载 `https://people.eecs.berkeley.edu/~hendrycks/data.tar` 并解压。

单道题的格式化（`format_example`），第 `idx` 行的题目与四个选项拼成「题干 + A. … + B. … + Answer:」：

[benchmark/mmlu/bench_sglang.py:L33-L41](https://github.com/sgl-project/sglang/blob/4eaa5ca6510622cb0006bcfee5947b17859ac8c7/benchmark/mmlu/bench_sglang.py#L33-L41) —— `format_example`：`include_answer=True` 时在 `Answer:` 后补上正确字母（用于 few-shot 示例），`False` 时不补（用于待答题）。

few-shot 前缀的拼装（`gen_prompt`）：

[benchmark/mmlu/bench_sglang.py:L44-L52](https://github.com/sgl-project/sglang/blob/4eaa5ca6510622cb0006bcfee5947b17859ac8c7/benchmark/mmlu/bench_sglang.py#L44-L52) —— `gen_prompt`：开头放学科说明，再循环 `k` 次拼接带答案示例。

在 `main` 里把每道题组装成一条参数，并控制 few-shot 长度不超过 1536 token（用 tiktoken 计数，超了就 `k -= 1`）：

[benchmark/mmlu/bench_sglang.py:L87-L117](https://github.com/sgl-project/sglang/blob/4eaa5ca6510622cb0006bcfee5947b17859ac8c7/benchmark/mmlu/bench_sglang.py#L87-L117) —— 逐学科读取 dev/test，构造 `arguments`（含 `examples` 与 `question` 两个键）和 `labels`。

> 小提示：脚本里用 `len(tokenizer.encode(few_shot_examples)) > 1536` 这个硬上限来裁剪 few-shot 数量。这个 1536 是为小模型预留的「安全长度」，换成大模型/长上下文模型时通常可以放宽。

#### 4.1.4 代码实践

**目标**：读懂「题库 → prompt」这一步，为后面的综合实践（换成自己的 CSV）做准备。

1. 打开 `data/`（若已下载），挑一个学科，如 `data/test/abstract_algebra_test.csv`，人工核对前两行：确认列顺序是 `题目,A,B,C,D,答案`。
2. 用一个 Python 交互环境，导入本脚本的 `format_example`、`gen_prompt`，给一个 2 行的假 DataFrame，打印出 `gen_prompt` 的输出。
3. **需要观察的现象**：输出文本里示例题带 `Answer: X`，而「待答题」没有。
4. **预期结果**：你能清楚看到「带答案示例 + 不带答案待答题」的拼接结构。
5. 若本地没有数据，下载步骤的实际产物**待本地验证**（取决于网络与外部链接是否可用）。

#### 4.1.5 小练习与答案

- **练习 1**：为什么 few-shot 示例题要用 `include_answer=True`，而待答题用 `False`？
  - **答案**：示例带答案是为了「教模型答题格式」（监督信号），待答题不带答案是为了让模型**生成**答案而非复读。带答案会让 `Answer:` 后面已有内容，模型无生成空间。
- **练习 2**：把 few-shot 上限从 1536 改成 4096，对一个小上下文模型可能造成什么后果？
  - **答案**：可能导致 prompt 加上待答题后超过模型最大上下文长度，触发截断或服务端报错；few-shot 数量 `k` 本来是用来动态收缩以避开这个问题的。

### 4.2 公共参数解析：add_common_sglang_args_and_parse

#### 4.2.1 概念说明

每个 `bench_sglang.py` 都需要一批「**和具体数据集无关、只和服务连接/运行方式有关**」的参数：连接哪个 host:port、用什么 backend、开多少并发、结果写哪个文件……。如果在每个脚本里重复写一遍 `argparse`，既啰嗦又容易不一致。SGLang 把这些公共参数抽到 `add_common_sglang_args_and_parse` 里，所有评测脚本共用。

#### 4.2.2 核心流程

1. 脚本先建一个自己的 `argparser`，加数据集专属参数（如 MMLU 的 `--ntrain`、`--nsub`）。
2. 把这个 parser 传给 `add_common_sglang_args_and_parse(parser)`。
3. 该函数在 parser 上**追加**公共参数，然后调用 `parser.parse_args()` 一次性解析，返回完整的 `args`。
4. 后续整个脚本（`main`、`select_sglang_backend`、落盘）都读这个 `args`。

#### 4.2.3 源码精读

公共参数的定义与解析（默认值是理解脚本行为的关键）：

[python/sglang/test/test_utils.py:L442-L458](https://github.com/sgl-project/sglang/blob/4eaa5ca6510622cb0006bcfee5947b17859ac8c7/python/sglang/test/test_utils.py#L442-L458) —— `add_common_sglang_args_and_parse`：注入 `--parallel`(默认 64)、`--host`(127.0.0.1)、`--port`(30000)、`--backend`(srt)、`--device`、`--result-file`(result.jsonl)、`--raw-result-file`，再 `parse_args()`。

脚本入口如何使用它：

[benchmark/mmlu/bench_sglang.py:L200-L210](https://github.com/sgl-project/sglang/blob/4eaa5ca6510622cb0006bcfee5947b17859ac8c7/benchmark/mmlu/bench_sglang.py#L200-L210) —— `__main__`：先加 `--ntrain`/`--data_dir`/`--save_dir`/`--nsub`，再 `add_common_sglang_args_and_parse(parser)`，最后 `download_data` 与 `main`。

公共参数默认值速查表：

| 参数 | 默认值 | 含义 |
| --- | --- | --- |
| `--parallel` | 64 | 并发线程数（喂给 `run_batch` 的 `num_threads`） |
| `--host` / `--port` | 127.0.0.1 / 30000 | 服务端地址 |
| `--backend` | srt | 后端类型，决定走 `RuntimeEndpoint` 还是 `OpenAI` |
| `--result-file` | result.jsonl | 汇总结果落盘文件 |
| `--raw-result-file` | （无） | 逐题 raw 结果文件，**不给则不落** |

> 批判性阅读提示：脚本里定义了 `--save_dir`（[L206](https://github.com/sgl-project/sglang/blob/4eaa5ca6510622cb0006bcfee5947b17859ac8c7/benchmark/mmlu/bench_sglang.py#L206)），但 `main` 中并未使用它——结果实际写到 `--result-file`（默认 `result.jsonl`）。这是一个遗留参数，读懂这种「声明了却没用」的代码有助于你日后改脚本时不被误导。

#### 4.2.4 代码实践

**目标**：亲眼看到公共参数被注入并能被覆盖。

1. 在 `benchmark/mmlu/` 下执行 `python3 bench_sglang.py --help`。
2. **需要观察的现象**：帮助文本里同时出现 MMLU 专属参数（`--ntrain`、`--nsub`）和公共参数（`--parallel`、`--port`、`--backend`、`--result-file`）。
3. **预期结果**：`--parallel` 默认 64、`--port` 默认 30000、`--backend` 默认 `srt`，与上表一致。
4. 若环境未装好依赖，`--help` 是否可正常输出**待本地验证**。

#### 4.2.5 小练习与答案

- **练习 1**：为什么要把公共参数放进一个公共函数，而不是每个脚本各写一份？
  - **答案**：避免重复与不一致——所有评测脚本的默认端口、backend 语义、结果文件名保持统一，也方便日后统一调整（比如改默认并发数只需改一处）。
- **练习 2**：若我想把结果写到 `mmlu_run.jsonl` 而非默认文件，该传什么参数？
  - **答案**：`--result-file mmlu_run.jsonl`（注意参数名带连字符，对应 `args.result_file`）。

### 4.3 后端选择：select_sglang_backend

#### 4.3.1 概念说明

`--backend` 决定了「SGL 程序的请求最终发到哪里」。这是 SGLang 让同一份评测脚本能在**不同推理后端**之间切换的关键。`select_sglang_backend` 把字符串形式的 `--backend` 翻译成一个真正的后端对象。

#### 4.3.2 核心流程

```
args.backend
  ├─ 以 "srt" 开头  → RuntimeEndpoint(http://host:port)   # 连运行中的 SGLang 服务
  ├─ 以 "gpt-" 开头 → OpenAI(args.backend)               # 连 OpenAI 兼容接口
  └─ 其它           → 报错 ValueError
```

`srt` 后端（`RuntimeEndpoint`）在构造时会向服务端打一次 `/get_model_info` 拿到模型信息，后续生成请求打到 `/generate`。

#### 4.3.3 源码精读

后端选择逻辑：

[python/sglang/test/test_utils.py:L461-L473](https://github.com/sgl-project/sglang/blob/4eaa5ca6510622cb0006bcfee5947b17859ac8c7/python/sglang/test/test_utils.py#L461-L473) —— `select_sglang_backend`：`srt*` 走 `RuntimeEndpoint(normalize_base_url(args.host, args.port))`；`gpt-*` 走 `OpenAI(args.backend)`；其余抛 `ValueError`。

在脚本中的调用点：

[benchmark/mmlu/bench_sglang.py:L143](https://github.com/sgl-project/sglang/blob/4eaa5ca6510622cb0006bcfee5947b17859ac8c7/benchmark/mmlu/bench_sglang.py#L143) —— `backend = select_sglang_backend(args)`：把后端对象准备好，稍后传给 `run_batch`。

`RuntimeEndpoint` 构造时与服务端的交互：

[python/sglang/lang/backend/runtime_endpoint.py:L26-L57](https://github.com/sgl-project/sglang/blob/4eaa5ca6510622cb0006bcfee5947b17859ac8c7/python/sglang/lang/backend/runtime_endpoint.py#L26-L57) —— `RuntimeEndpoint.__init__`：保存 `base_url`，向 `/get_model_info` 发请求并断言成功，据此推导聊天模板。

> 这就印证了 u1-l1 的结论：评测脚本是客户端，`--host/--port` 指向的那个服务必须**事先由 `launch_server` 起好**，否则 `select_sglang_backend` 在构造 `RuntimeEndpoint` 时就会因 `/get_model_info` 连不上而失败。

#### 4.3.4 代码实践

**目标**：体会「同一脚本、多后端」的切换能力。

1. 阅读 [benchmark/mmlu/README.md:L8-L20](https://github.com/sgl-project/sglang/blob/4eaa5ca6510622cb0006bcfee5947b17859ac8c7/benchmark/mmlu/README.md#L8-L20) 给出的两条命令：一条默认 `srt` 连本地服务，一条 `--backend gpt-3.5-turbo` 连 OpenAI。
2. **需要观察的现象**：两条命令除了 `--backend` 和 `--parallel`，其余完全相同——同一份脚本、同一份数据，仅后端不同。
3. **预期结果**：`srt` 需要先 `python -m sglang.launch_server … --port 30000` 起服务；`gpt-3.5-turbo` 则需要一个有效的 OpenAI API Key（脚本会读环境变量），无需本地服务。
4. 实际能否跑通 OpenAI 分支**待本地验证**（取决于是否配置了 Key 与网络）。

#### 4.3.5 小练习与答案

- **练习 1**：如果我传 `--backend vllm`，`select_sglang_backend` 会怎样？
  - **答案**：会走到 `else` 分支抛 `ValueError("Invalid backend: vllm")`。注意：vllm 对比是走 `bench_other.py` + `_get_call_generate`（见 [test_utils.py:L478-L481](https://github.com/sgl-project/sglang/blob/4eaa5ca6510622cb0006bcfee5947b17859ac8c7/python/sglang/test/test_utils.py#L478-L481)），而不是这个 SGL 前端路径。这是 `bench_sglang.py`（SGL 前端）与 `bench_other.py`（直接 HTTP）两套体系的区别。
- **练习 2**：为什么 `RuntimeEndpoint` 构造时要打 `/get_model_info`？
  - **答案**：为了拿到 `model_path`，进而推导正确的聊天模板（chat template），保证 prompt 拼接与服务端一致。

### 4.4 SGL 程序与并发执行：sgl.function 与 run_batch

#### 4.4.1 概念说明

这是整个骨架的「发动机」。`@sgl.function` 把「怎么生成一道题的答案」定义成一个可重用的程序 `few_shot_mmlu`；`run_batch` 则拿一大堆输入（`arguments`）一次性**并发**跑完，返回每条输入对应的状态（state），从状态里取出变量 `answer` 的值就是模型生成结果。

#### 4.4.2 核心流程

1. 用 `@sgl.function` 定义 `few_shot_mmlu(s, examples, question)`：把 few-shot 前缀 `examples` 和待答题 `question` 拼起来，再 `sgl.gen("answer")` 生成一个名为 `answer` 的变量。
2. 注意脚本里对 `gpt-*` 和其它后端用了**两套**程序写法：前者用 `sgl.user(...)/sgl.assistant(...)` 组织成对话格式；后者直接字符串拼接（base 模型风格）。
3. 调 `few_shot_mmlu.run_batch(arguments, temperature=0, max_new_tokens=1, backend=..., num_threads=..., progress_bar=True)`，`max_new_tokens=1` 因为只要一个字母。
4. 返回的 `states` 是一个列表，`s["answer"]` 取出生成文本，取首字母即为预测答案。

#### 4.4.3 源码精读

SGL 程序定义（注意 gpt 与非 gpt 两个分支）：

[benchmark/mmlu/bench_sglang.py:L123-L140](https://github.com/sgl-project/sglang/blob/4eaa5ca6510622cb0006bcfee5947b17859ac8c7/benchmark/mmlu/bench_sglang.py#L123-L140) —— `@sgl.function few_shot_mmlu`：`gpt-*` 走 `sgl.user`/`sgl.assistant` 对话式；其余走 `examples + question + sgl.gen("answer")` 直拼式。

并发执行与结果抽取：

[benchmark/mmlu/bench_sglang.py:L146-L157](https://github.com/sgl-project/sglang/blob/4eaa5ca6510622cb0006bcfee5947b17859ac8c7/benchmark/mmlu/bench_sglang.py#L146-L157) —— `run_batch(...)` 计时执行；`preds` 取每条 `s["answer"].strip()[0]`（空串则记空）。

`run_batch` 的关键签名（理解可调旋钮）：

[python/sglang/lang/ir.py:L223-L247](https://github.com/sgl-project/sglang/blob/4eaa5ca6510622cb0006bcfee5947b17859ac8c7/python/sglang/lang/ir.py#L223-L247) —— `run_batch`：接收 `batch_kwargs`（即 `arguments`）、采样参数（`temperature`/`max_new_tokens`/`stop` 等）、`backend`、`num_threads`（默认 `"auto"`）、`progress_bar`；返回每条输入对应的状态列表。

几个要点：

- `temperature=0` 让结果**确定性**（贪心解码），保证可复现。
- `max_new_tokens=1` 只生成 1 个 token——因为答案就是单个字母 A/B/C/D，多生成反而可能引入噪声。
- `num_threads=args.parallel` 控制客户端并发度，直接决定评测墙钟时间；它和服务端的 batch 是两回事（客户端并发把请求「灌」进去，服务端再自行组批）。

#### 4.4.4 代码实践

**目标**：理解「并发度 vs 墙钟时间」的关系。

1. 起一个本地服务后，分别用 `--parallel 1` 和 `--parallel 64` 跑 `--nsub 1`（只跑 1 个学科，题量小）。
2. **需要观察的现象**：脚本结尾会打印 `Total latency: …`（[L180](https://github.com/sgl-project/sglang/blob/4eaa5ca6510622cb0006bcfee5947b17859ac8c7/benchmark/mmlu/bench_sglang.py#L180)）。对比两次的总延迟。
3. **预期结果**：`--parallel 64` 的总延迟明显小于 `--parallel 1`，因为多题被并发发送、服务端能合并成更大的 batch。但并发不是越大越好——过大可能让服务端排队、单题延迟上升。
4. 具体延迟数值**待本地验证**（取决于硬件与模型）。

#### 4.4.5 小练习与答案

- **练习 1**：为什么 MMLU 用 `max_new_tokens=1`，而 gsm8k 这类数学题不能用？
  - **答案**：MMLU 答案是单字母，1 个 token 足够且最干净；数学题需要模型输出推理过程与最终数字，必须给足生成长度（gsm8k 会设大得多的 `max_new_tokens`）。
- **练习 2**：`preds` 里 `s["answer"].strip()[0]` 为什么要先 `.strip()` 再取 `[0]`？
  - **答案**：模型生成可能带有前导空格（如 `" A"`），先去空白再取首字符，才能稳定拿到字母 A；若生成空串则用 `if len(...) > 0 else ""` 兜底，避免索引越界。

### 4.5 结果统计与落盘：cors、weighted_acc 与 dump_bench_raw_result

#### 4.5.1 概念说明

跑完之后要做两件事：**算准确率**和**把结果存下来**。脚本会算逐学科准确率与总体（micro）准确率，并把一行汇总写进 `result.jsonl`（便于跨运行对比）；若给了 `--raw-result-file`，还会用 `dump_bench_raw_result` 把每道题的 prompt/output/对错逐条落盘，便于事后逐题分析。

#### 4.5.2 核心流程

1. `cors = [pred == label for pred, label in zip(preds, labels)]`：逐题对错布尔列表。
2. 按学科切片打印各学科准确率，再用 `np.mean(cors)` 算总体准确率（每个题目等权）。
3. 若 `--raw-result-file` 非空，调 `dump_bench_raw_result` 写逐题 JSONL。
4. 把汇总（task/backend/latency/accuracy/请求数等）作为一行 JSON 追加到 `--result-file`。

总体准确率定义为每个题目等权的 micro 平均：

\[
\text{Acc} = \frac{1}{N}\sum_{i=1}^{N} \mathbb{1}[\text{pred}_i = \text{label}_i]
\]

其中 \(N\) 是全部学科的题目总数，\(\mathbb{1}[\cdot]\) 是指示函数（条件成立为 1 否则为 0）。注意它是**题目级**平均，不是「学科级」平均——学科题数不同时二者会有差异。

#### 4.5.3 源码精读

逐学科 + 总体准确率：

[benchmark/mmlu/bench_sglang.py:L161-L181](https://github.com/sgl-project/sglang/blob/4eaa5ca6510622cb0006bcfee5947b17859ac8c7/benchmark/mmlu/bench_sglang.py#L161-L181) —— `cors` 对错列表、按 `num_questions` 切片打印各学科准确率、`weighted_acc = np.mean(cors)`、打印总延迟与平均准确率。

逐题 raw 结果落盘的调用：

[benchmark/mmlu/bench_sglang.py:L172-L177](https://github.com/sgl-project/sglang/blob/4eaa5ca6510622cb0006bcfee5947b17859ac8c7/benchmark/mmlu/bench_sglang.py#L172-L177) —— 调用 `dump_bench_raw_result(path=args.raw_result_file, states=states, preds=preds, labels=labels)`。

`dump_bench_raw_result` 的实现：

[python/sglang/test/test_utils.py:L2342-L2366](https://github.com/sgl-project/sglang/blob/4eaa5ca6510622cb0006bcfee5947b17859ac8c7/python/sglang/test/test_utils.py#L2342-L2366) —— `dump_bench_raw_result`：`path` 为空则直接返回；否则逐条构造 `{prompt_id, prompt, output, correct}`，其中 `prompt = state.text()` 去掉尾部 `output`，`correct = preds[i]==labels[i]`，最后按行写 JSON。

汇总行写入：

[benchmark/mmlu/bench_sglang.py:L184-L197](https://github.com/sgl-project/sglang/blob/4eaa5ca6510622cb0006bcfee5947b17859ac8c7/benchmark/mmlu/bench_sglang.py#L184-L197) —— 以追加模式打开 `args.result_file`，写一行含 `task/backend/num_gpus/latency/accuracy/num_requests` 及 `other`（nsub、parallel）的 JSON。

> 「逐题 raw 文件」和「汇总文件」的分工：前者用于**事后排查**（看某道题模型答了什么、为什么错），后者用于**跨运行对比**（多次跑、多模型跑，每行一个结果，可拼成表格）。`dump_bench_raw_result` 在不给 `--raw-result-file` 时是空操作，不产生任何开销。

#### 4.5.4 代码实践

**目标**：亲手产生并解读两份产物。

1. 起服务后跑一次，带上 `--raw-result-file raw.jsonl --result-file summary.jsonl`。
2. **需要观察的现象**：终端先打印各学科 `acc: …`，再打印 `BenchRawResultDumper save results to raw.jsonl`（见 [test_utils.py:L2365](https://github.com/sgl-project/sglang/blob/4eaa5ca6510622cb0006bcfee5947b17859ac8c7/python/sglang/test/test_utils.py#L2365)），最后 `summary.jsonl` 多出一行汇总。
3. **预期结果**：`raw.jsonl` 每行一个 `{prompt_id, prompt, output, correct}`；`summary.jsonl` 一行含 `accuracy` 等字段。
4. 实际能否跑通**待本地验证**（需起服务并下载数据）。

#### 4.5.5 小练习与答案

- **练习 1**：`weighted_acc = np.mean(cors)` 为什么叫「weighted」？
  - **答案**：因为它把所有学科的题目混在一起求平均，**题数多的学科贡献更大**（按题目数加权）。若想看「学科级平均」应先对每个学科求 acc 再对学科求均值，两者数值通常不同。
- **练习 2**：如果不传 `--raw-result-file`，`dump_bench_raw_result` 会报错吗？
  - **答案**：不会。函数开头 `if not path: return`（[L2348-L2349](https://github.com/sgl-project/sglang/blob/4eaa5ca6510622cb0006bcfee5947b17859ac8c7/python/sglang/test/test_utils.py#L2348-L2349)），直接返回，是安全的空操作。

## 5. 综合实践：用 3 道选择题的本地 CSV 跑通最小流程

把本讲的五个模块串起来：仿照 `bench_sglang.py`，**把数据源从 MMLU 换成一个只含 3 道选择题的本地 CSV**，跑通「构造 prompt → 生成 → 统计准确率」的最小流程。

**目标**：验证你已经掌握整条骨架，并能替换最关键的「数据准备」环节。

**操作步骤**：

1. 在某目录建一个 `mytask/data/test/mytest_test.csv`（无表头），写 3 行，列顺序为 `题目,A,B,C,D,答案`，例如：

   ```csv
   1+1=?,2,3,4,5,A
   太阳从哪边升起?,东,西,南,北,A
   水的化学式是?,H2O,CO2,O2,NaCl,A
   ```

2. 复制 `benchmark/mmlu/bench_sglang.py` 为 `mytask/bench_sglang.py`，做最小改动：
   - 去掉对 `dev`/few-shot 的依赖（或留一个空的 few-shot），让 `arguments` 直接来自你这张 CSV；
   - 保留 `add_common_sglang_args_and_parse`、`select_sglang_backend`、`@sgl.function` + `run_batch`、`dump_bench_raw_result` 这四个公共件**不动**——它们就是本讲的精华。
3. 起一个本地服务：

   ```bash
   python -m sglang.launch_server --model-path <你的模型> --port 30000
   ```

4. 运行你的脚本（参考 README 的客户端命令风格）：

   ```bash
   python3 mytask/bench_sglang.py --port 30000 --result-file my_summary.jsonl --raw-result-file my_raw.jsonl
   ```

**需要观察的现象**：

- 终端打印每题或总体的准确率（理想情况 3 题全对，accuracy ≈ 1.0）。
- `my_summary.jsonl` 出现一行汇总；`my_raw.jsonl` 出现 3 行逐题记录。

**预期结果**：

- 因为题目极简单，正常模型的 `accuracy` 应接近 1.0；`my_raw.jsonl` 的 `correct` 字段多为 `true`。
- 若准确率偏低，先检查 prompt 拼接（是否漏了 `Answer:` 提示）与答案抽取（首字母是否取对）。

**说明**：本实践涉及起服务与模型加载，具体能否一次跑通、准确率多少**待本地验证**。重点是走通「数据 → 程序 → 后端 → 并发 → 落盘」这条链路，而不是追求绝对数值。

## 6. 本讲小结

- 一份精度评测脚本的骨架是固定的五步：**准备数据 → 定义 SGL 程序 → 选择后端 → 并发执行 → 统计落盘**，MMLU 是这个骨架最清晰的样板。
- `add_common_sglang_args_and_parse` 统一注入连接/并发/结果文件等公共参数，让所有 `bench_sglang.py` 行为一致。
- `select_sglang_backend` 用 `--backend` 字符串决定走 `RuntimeEndpoint`（连本地 SGLang 服务）还是 `OpenAI`，是「同脚本多后端」的关键。
- `@sgl.function` + `run_batch` 是发动机：前者描述「单题怎么生成」，后者并发跑完一大批并返回每条状态，`s["answer"]` 取结果。
- 结果有两类产物：逐题 raw 文件（`dump_bench_raw_result`，可选）用于排查，汇总 `result.jsonl`（追加一行）用于跨运行对比。
- few-shot prompt 的拼接、答案首字母抽取、micro 准确率是选择题评测的三个通用技巧，会迁移到后续 hellaswag/ceval 等讲义。

## 7. 下一步学习建议

- **横向对照**：进入 u2-l1（选择题评测范式），看 hellaswag/boolq/ceval 如何复用这套骨架、只在「prompt 拼法/答案抽取/加权方式」上变化。
- **换数据形态**：若对开放式/数学题感兴趣，可先看 u2-l2（gsm8k、reasoning_benchmark），体会「答案抽取从首字母变成数字匹配、并引入多次采样与标准误差」。
- **读公共件实现**：想更深入了解 `run_batch` 的并发与后端机制，可阅读 [python/sglang/lang/ir.py](https://github.com/sgl-project/sglang/blob/4eaa5ca6510622cb0006bcfee5947b17859ac8c7/python/sglang/lang/ir.py) 与 `runtime_endpoint.py` 的 `/generate` 调用路径。
- **动手准备**：把本讲的「综合实践」做完——能独立替换数据源跑通最小流程，是进入后续所有评测讲义的前提。
