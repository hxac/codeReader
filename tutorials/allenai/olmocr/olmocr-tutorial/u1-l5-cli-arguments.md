# CLI 参数全解析：从 --help 回到 argparse 源码

## 1. 本讲目标

读完本讲，你应该能够：

1. 运行 `olmocr --help`，并把输出里的每一个参数与 [olmocr/pipeline.py](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py) 中 `main()` 的 argparse 定义一一对应。
2. 说出命令行参数的四个组——顶层输入、详细配置、Server（远程推理服务）、VLLM（本地推理引擎）、Beaker（集群提交）——各自控制流水线的哪一段。
3. 判断哪些参数影响**吞吐**（每秒能处理多少页）、哪些影响**成本**（外部 API 按量计费时花多少钱）、哪些影响**质量与容错**。
4. 掌握两个 argparse 进阶技巧：`parse_known_args()` 如何把「不认识的参数」原样转发给 vLLM，以及 `argparse.SUPPRESS` 如何实现「根据其他参数动态决定默认值」。

## 2. 前置知识

本讲假设你已经完成第 1 单元前几讲（尤其是 u1-l3 第一次运行）。需要用到的概念：

- **CLI（命令行接口）**：在终端里输入 `olmocr ./localworkspace --markdown --pdfs a.pdf` 这样的命令来驱动程序。
- **argparse**：Python 标准库里解析命令行参数的模块。你写 `parser.add_argument("--workers", type=int, default=20)`，argparse 就负责把命令行里的 `--workers 5` 变成 Python 对象 `args.workers = 5`。
- **位置参数与可选参数**：`workspace` 不带 `--` 前缀、必须按顺序提供，叫位置参数；`--pdfs`、`--markdown` 这类带 `--` 的叫可选参数。
- **`dest` 属性名**：`--max-model-len` 里的连字符不是合法的 Python 标识符，argparse 会自动把它转成下划线，所以代码里访问的是 `args.max_model_len`。
- **`store_true`**：布尔开关。命令行里写了 `--markdown` 则 `args.markdown` 为 `True`，不写则为 `False`（默认值）。
- **workspace**：u1-l3 讲过的「工作区」，是所有中间状态和结果的存放目录；本讲的第一个位置参数就是它。
- 入口链路回顾：[pyproject.toml:L49-L50](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/pyproject.toml#L49-L50) 把 `olmocr` 命令注册到 `olmocr.pipeline:cli_main`，[olmocr/pipeline.py:L1526-L1528](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1526-L1528) 中的 `cli_main()` 再用 `asyncio.run(main())` 把异步的 `main()` 跑起来。参数解析就发生在 `main()` 的开头。

## 3. 本讲源码地图

| 文件 | 本讲关注的区域 | 作用 |
| --- | --- | --- |
| `olmocr/pipeline.py` | L1199-L1272 | `main()` 开头的 argparse 定义：位置参数、顶层选项、三个参数组、`parse_known_args()` |
| `olmocr/pipeline.py` | L1275-L1289 | 解析后的立即消费：日志文件、并发信号量 |
| `olmocr/pipeline.py` | L1312-L1315 | `pages_per_group` 的条件默认值（本讲核心技巧） |
| `olmocr/pipeline.py` | L807-L834 | `vllm_server_task` 拼装 `vllm serve` 命令，`unknown_args` 在这里被转发 |
| `olmocr/pipeline.py` | L1414-L1420、L1429-L1437 | `--stats` / `--beaker` 短路分支；内部服务器时改写 `args.server` |
| `olmocr/pipeline.py` | L1455-L1459 | `--workers` 决定拉起多少个 worker 协程 |
| `README.md` | L274-L349、L430-L491 | 官方使用示例、各云厂商推荐命令、一份 `--help` 快照 |
| `pyproject.toml` | L49-L50 | `olmocr` 命令的注册处 |

## 4. 核心概念与源码讲解

### 4.1 模块一：argparse 参数组结构

#### 4.1.1 概念说明

一个成熟的批处理工具往往有几十个命令行参数。如果全部平铺，`--help` 会变成一堵墙。argparse 提供了 `add_argument_group()`：把参数按「职责」分到不同小组，`--help` 输出就会按组显示小标题，读者可以按需只看自己关心的组。

olmOCR 的参数按「流水线的执行地点」分成四大块：

1. **顶层输入**（不属于任何组）：workspace 放哪、处理哪些 PDF、用什么模型——任何运行方式都需要回答的问题。
2. **Server 组**：推理引擎不在本机时，远程服务的地址和密钥。
3. **VLLM 组**：在本机（或容器内）自己启动 vLLM 推理引擎时的引擎级配置。
4. **Beaker 组**：把任务提交到 AI2 的 Beaker 集群，而不是本地运行。

这四组恰好对应 olmOCR 的四种运行形态：本地 GPU 跑、远程 API 跑、本地多卡跑、集群跑。

#### 4.1.2 核心流程

`main()` 的参数定义流程可以概括为：

```text
创建 ArgumentParser
  ├─ 位置参数 workspace（必填）
  ├─ --pdfs（可多个值）与 --model
  ├─ 一批"详细配置"选项（直接挂在 parser 上，出现在 --help 的 options 节）
  ├─ server_group：--server、--api_key
  ├─ vllm_group：--gpu-memory-utilization、--max_model_len、-tp、-dp、--port
  └─ beaker_group：--beaker 系列
parse_known_args() → (args, unknown_args)
  ├─ args         ：已识别的参数，装进命名空间
  └─ unknown_args ：未识别的参数，留给 vLLM
```

分组只影响 `--help` 的展示和源码组织，不影响参数的解析优先级；真正让「未识别参数」变得有用的是 `parse_known_args()`（见模块三）。

#### 4.1.3 源码精读

入口：`main()` 的第一行创建解析器，description 一句话点明工具定位——「跑百万级 PDF 的批处理流水线管理器」：

[olmocr/pipeline.py:L1200-L1215](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1200-L1215) 定义唯一的必填位置参数 `workspace`（本地目录或 `s3://` 前缀）、`--pdfs`（`nargs="*"` 允许一次给多个路径，shell 的 `*.pdf` 通配符也会展开成多个）和 `--model`（默认 `allenai/olmOCR-2-7B-1025-FP8`）。

```python
parser.add_argument(
    "workspace",
    help="The filesystem path where work will be stored, can be a local folder, or an s3 path ...",
)
parser.add_argument("--pdfs", nargs="*", help="Path to add pdfs ... can be a glob path s3://bucket/prefix/*.pdf ...", default=None)
```

接着是一批「通常不需要改」的详细配置，直接挂在 parser 上（源码注释原话是 *More detailed config options, usually you shouldn't have to change these*）：[olmocr/pipeline.py:L1217-L1239](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1217-L1239)，包括 `--workers`、`--max_concurrent_requests`、`--pages_per_group`（本讲主角，L1220 用了 `argparse.SUPPRESS`）、`--stats`、`--markdown`、`--disk_logging` 等十几个选项。

然后显式建组。Server 组服务「远程推理」场景：

[olmocr/pipeline.py:L1241-L1247](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1241-L1247) —— `--server` 指向外部 vLLM 或兼容 OpenAI 接口的服务（如 DeepInfra），`--api_key` 是鉴权密钥。注意 `--server` 没有默认值，Python 里读不到的属性默认为不存在，这个细节在模块三会放大讲。

VLLM 组的组描述本身就说明了转发机制：

[olmocr/pipeline.py:L1249-L1258](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1249-L1258) —— 组描述写着「*These arguments are passed to vLLM. Any unrecognized arguments are also automatically forwarded to vLLM.*」（这些参数会传给 vLLM；任何未被识别的参数也会自动转发给 vLLM）。注意两个选项用了连字符命名（`--tensor-parallel-size`）并配了短别名（`-tp`），argparse 的 `dest` 会取第一个长选项名并把连字符换成下划线，所以代码里是 `args.tensor_parallel_size`。

```python
vllm_group.add_argument("--max_model_len", type=int, default=16384, help="Upper bound (tokens) vLLM will allocate KV-cache for, lower if VLLM won't start")
vllm_group.add_argument("--tensor-parallel-size", "-tp", type=int, default=1, help="Tensor parallel size for vLLM")
```

Beaker 组控制集群提交：

[olmocr/pipeline.py:L1260-L1270](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1260-L1270) —— `--beaker` 是个开关，`--beaker_gpus` 控制 GPU 副本数，`--beaker_cluster` 的默认值是一串 AI2 内部集群名组成的列表。

最后一行收口：

[olmocr/pipeline.py:L1272](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1272) —— 用的是 `parse_known_args()` 而不是常见的 `parse_args()`。区别：`parse_args()` 遇到不认识的参数会直接报错退出；`parse_known_args()` 把它们收集进 `unknown_args` 列表继续运行。这一行是「olmocr 透传 vLLM 参数」的开关。

**一个真实的历史痕迹（很有教学价值）**：README 里贴的 `--help` 快照（[README.md:L430-L491](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/README.md#L430-L491)）与当前源码并不一致——快照里 `--model` 的帮助文本还写着旧默认值 `olmOCR-7B-0725-FP8`，且缺少 `--max_concurrent_requests`、`--max_server_ready_timeout`、`--disk_logging`、`--api_key`，`--server` 也还挂在 VLLM 组下。这说明 README 的帮助快照是旧版本留下的，**没有随代码更新**。结论：查参数的权威来源永远是 argparse 源码，README 只作参考。

#### 4.1.4 代码实践

**实践目标**：亲手生成当前版本的 `--help` 输出，验证它与源码分组一一对应，并发现它与 README 快照的差异。

**操作步骤**：

1. 激活 u1-l2 建好的 conda 环境（轻量安装即可，`--help` 不需要 GPU）。
2. 运行 `olmocr --help`，把完整输出保存到文件（例如 `olmocr --help > help.txt`）。
3. 打开 [olmocr/pipeline.py:L1200-L1272](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1200-L1272)，对照输出制作一张参数表：参数名、默认值、作用、所属组。
4. 单独抄下 help 输出里 `options:`、`Server arguments...`、`VLLM arguments:`、`beaker/cluster execution:` 这些小节标题，核对它们分别对应源码里哪些 `add_argument` / `add_argument_group` 调用。
5. 再对照 [README.md:L433-L490](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/README.md#L433-L490) 的旧快照，列出至少两处不一致。

**需要观察的现象**：help 输出顶部第一行是 `usage: pipeline.py [-h] [--pdfs [PDFS ...]] ... workspace`——注意 `workspace` 出现在最后且不带方括号（必填），其他参数都带方括号（可选）。

**预期结果**：当前版本的 help 中能看到 `--max_concurrent_requests`、`--disk_logging`、`--api_key`，以及独立的 Server 参数组；这些在 README 快照里都找不到或位置不同。`--model` 的帮助文本显示默认值为 `allenai/olmOCR-2-7B-1025-FP8`。（本讲义写作环境未安装 olmocr，以上为待本地验证项；分组结构可直接从源码确认。）

#### 4.1.5 小练习与答案

**练习 1**：为什么 `workspace` 设计成位置参数而不是 `--workspace` 选项？

**参考答案**：它是每次运行唯一必填的信息，放在位置参数上使命令更短（`olmocr ./localworkspace` 而不是 `olmocr --workspace ./localworkspace`），且 argparse 会自动校验缺失并在缺省时报错 `the following arguments are required: workspace`。

**练习 2**：`--tensor-parallel-size` 在代码里如何访问？为什么不是 `args.tensor-parallel-size`？

**参考答案**：访问 `args.tensor_parallel_size`。argparse 的 `dest` 由第一个长选项名推导，连字符不是合法的 Python 标识符字符，会被自动替换成下划线。

**练习 3**：VLLM 组的组描述文字除了说明本组参数外，还预告了什么机制？

**参考答案**：预告了「未识别参数自动转发给 vLLM」——即 `parse_known_args()` 收集的 `unknown_args` 会被追加到 `vllm serve` 命令行末尾（见模块三 4.3.3）。

### 4.2 模块二：关键默认值解读

#### 4.2.1 概念说明

默认值是作者对「典型场景」的预判：不传参数时，程序按最常见的方式运行。读懂默认值，等于读懂作者认为什么样的用法是主流。olmOCR 的默认值整体面向「本地/集群 GPU 大批量处理」场景：并发拉满、页组很大、重试次数充足。而一旦你走「外部按量计费 API」路线，就必须主动调小若干参数——README 甚至专门给了建议值。

#### 4.2.2 核心流程

解析完成后，各默认值流向流水线的不同位置：

```text
args.workers=20 ──────────→ main() 里 for 循环创建 20 个 worker 协程
args.max_concurrent_requests=1600 → asyncio.BoundedSemaphore 限制同时在飞的请求数
args.max_model_len=16384 ─→ 拼进 "vllm serve" 命令行
args.port=30024 ─────────→ vllm serve 端口 + args.server 的 localhost 地址
args.stats / args.beaker ─→ 触发短路分支，提前 return
```

两个「短路开关」值得注意先后顺序：`--stats` 在 [olmocr/pipeline.py:L1414-L1416](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1414-L1416) 打印统计后直接 `return`，发生在 GPU 检查之前（这正是 u1-l3 说过的「轻量环境也能 `--stats`」的原因）；`--beaker` 在 [olmocr/pipeline.py:L1418-L1420](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1418-L1420) 提交集群任务后同样 `return`，本地不再执行推理。

#### 4.2.3 源码精读

并发双闸门。第一道是 worker 数：

[olmocr/pipeline.py:L1455-L1459](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1455-L1459) —— `for i in range(args.workers)` 创建协程。每个 worker 从队列领一个「PDF 组」处理，默认 20 个组并行推进。

第二道是请求并发上限：

[olmocr/pipeline.py:L1289](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1289) —— `asyncio.BoundedSemaphore(args.max_concurrent_requests)`，默认 1600。一个组内的多页请求会先拿到信号量才发出（u2-l7 会精读）。两道闸门的区别：`--workers` 限制「同时处理多少个组」，`--max_concurrent_requests` 限制「同时在途的 HTTP 请求数」。

VLLM 组默认值的下游消费。`--max_model_len 16384` 限制 vLLM 为单条请求分配的 KV-cache 上限，`--port 30024` 决定本地服务端口；当 `--server` 未提供时，[olmocr/pipeline.py:L1429-L1437](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1429-L1437) 会把 `args.server` 改写成 `http://localhost:{args.port}/v1`，并把 `args.model` 改成内部别名 `olmocr`（这样本地模型路径再奇怪也不影响请求）。

启动提示日志也值得一看：

[olmocr/pipeline.py:L1282-L1284](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1282-L1284) —— 提示遇到显存不足或 "KV cache is larger than available memory" 时，用更低的 `--gpu_memory_utilization` 和 `--max_model_len` 重试。这条日志同时是**参数影响成本/可行性**的直接证据：这两个参数直接决定能不能在有限显存的 GPU 上跑起来。

**外部 API 场景的反例（来自 README）**：[README.md:L339-L341](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/README.md#L339-L341) 给 DeepInfra、Parasail 等按量计费服务的推荐命令都显式带上了 `--workers 1 --max_concurrent_requests 20`；[README.md:L348-L349](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/README.md#L348-L349) 进一步解释：`--workers` 设为 1 可以「先做完一组再做下一组」，`--pages_per_group` 调小是因为外部服务商的并发上限低。这正说明默认值 20/1600/500 都是为「自己的 GPU」准备的。

关键参数分类速查表（依据源码默认值与 README 建议整理）：

| 参数 | 默认值 | 影响维度 | 一句话说明 |
| --- | --- | --- | --- |
| `--workers` | 20 | 吞吐 | 并行处理的 PDF 组数 |
| `--max_concurrent_requests` | 1600 | 吞吐 / 外部成本 | 在途推理请求上限；外部 API 建议 20 |
| `--pages_per_group` | 动态（50 或 500） | 吞吐 / 外部成本 | 每个工作项的目标页数；详见模块三 |
| `--max_page_retries` | 8 | 质量 / 成本 | 单页最多重试次数 |
| `--max_page_error_rate` | 0.004 | 质量 | 单文档允许的失败页比例（约 1/250），超限整篇丢弃 |
| `--target_longest_image_dim` | 1288 | 质量 / 显存 | 渲染页面最长边像素 |
| `--max_model_len` | 16384 | 显存可行性 | KV-cache token 上限，调低可救活启动失败 |
| `--gpu-memory-utilization` | None（不追加） | 显存可行性 | vLLM 显存占用比例 |
| `--markdown` | False | 输出 | 额外输出 Markdown 文件 |
| `--stats` | False | 运行模式 | 只报统计不干活，提前 return |
| `--beaker_gpus` | 1 | 集群成本 | GPU 副本数 |

#### 4.2.4 代码实践

**实践目标**：体会「同一份默认值，两种场景」——为本地 GPU 和外部 API 各写一条命令，并解释每一个被改动的参数。

**操作步骤**：

1. 阅读 [README.md:L274](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/README.md#L274)（本地默认命令）和 [README.md:L340](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/README.md#L340)（DeepInfra 命令）。
2. 把两条命令并排抄下来，标出后者多出的参数：`--server`、`--api_key`、`--workers 1`、`--max_concurrent_requests 20`，以及换成的模型名 `allenai/olmOCR-2-7B-1025`（非 FP8 版本）。
3. 在表格（4.2.3）里逐个标注这些改动属于哪个影响维度。
4. 选做：若有 DeepInfra 等账号，按 README 命令对 `tests/gnarly_pdfs/` 里 1-2 个 PDF 实际跑一次，观察日志中并发请求的行为。

**需要观察的现象**（选做部分）：外部 API 模式下日志显示的「正在处理的组」几乎总是 1，请求发射节奏明显平缓；本地模式下则是一开局就把并发推到信号量上限附近。

**预期结果**：能口头回答「为什么外部 API 场景要同时调小 `--workers` 和 `--max_concurrent_requests`」（前者控制组的并行度、后者控制请求在途数，两个都超了会触发服务商限流甚至报错；按量计费场景下大批并发失败重试还会放大费用）。（选做的实际运行为待本地验证，需要 API key。）

#### 4.2.5 小练习与答案

**练习 1**：`--stats` 和 `--markdown` 都是 `store_true` 开关，它们对程序控制流的影响有何本质区别？

**参考答案**：`--stats` 是控制流开关——在 [L1414-L1416](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1414-L1416) 触发提前 `return`，整个推理流水线不会运行；`--markdown` 只是输出开关，不影响主流程，只决定 worker 落盘时是否多写一份 Markdown 文件。

**练习 2**：为什么 `--gpu-memory-utilization` 在 argparse 里不设默认值（结果为 `None`），而不是给个 0.9 之类的数？

**参考答案**：作者有意把「不指定」和「指定了某个值」区分开。`None` 时 [L827-L828](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L827-L828) 不会把这个参数追加进 `vllm serve` 命令，vLLM 会使用它自己的默认值；若 olmocr 强行设默认值，反而会覆盖掉 vLLM 未来版本的新默认。这是「把决策权交还给下游工具」的常见手法。

**练习 3**：`--max_page_error_rate 0.004` 意味着一份 100 页的文档允许几页失败？

**参考答案**：\( 100 \times 0.004 = 0.4 \) 页，向下取整即 0 页——100 页的文档一页都失败不得；help 文本里也写了这是「1/250 by default」，即 250 页才容许 1 页。短文档实质上要求零失败（靠最多 8 次重试兜底）。

### 4.3 模块三：argparse.SUPPRESS 与条件默认值技巧

#### 4.3.1 概念说明

argparse 的普通 `default` 有个先天局限：**解析结束后属性一定存在**。比如 `default=None` 之后，你无法用 `args.pages_per_group is None` 之外的语义区分「用户没写」和「用户写了但值恰好等值」；更麻烦的是当合法值本身可能是 `0`、`False` 或 `None` 时，`if not args.xxx` 这类判断会把「用户显式传了 0」误判成「没传」。

`argparse.SUPPRESS` 解决了这个问题：**未在命令行提供时，命名空间里干脆不创建这个属性**。于是 `hasattr(args, "pages_per_group")` 成为百分之百可靠的「用户是否显式设置」探测器。olmOCR 用它实现了一个精巧的条件默认值：`pages_per_group` 的最终默认值取决于用户有没有给 `--api_key`。

此外，olmOCR 还展示了另外两个「解析之后再定值」的技巧，合起来构成一个家族：

- `--disk_logging`：`nargs="?"` + `const` + `default=None` 三件套——「不写 flag」是 None，「只写 flag」是 `const` 值，「flag 带值」是那个值。
- `--server`：解析时无默认，随后在 `main()` 里根据它是否存在改写 `args.server` / `args.model`。

#### 4.3.2 核心流程

`pages_per_group` 的完整生命线：

```text
argparse 定义：default=argparse.SUPPRESS
        ↓
parse_known_args()
        ├─ 用户写了 --pages_per_group N → args.pages_per_group = N（正常属性）
        └─ 用户没写                → args 上【没有】这个属性
        ↓
main() 检查：if not hasattr(args, "pages_per_group"):
        ├─ args.api_key is not None（走外部付费 API）→ 默认 50 页/组
        └─ 否则（本地/集群自有算力）      → 默认 500 页/组
        ↓
收集 --pdfs 时：采样最多 100 个 PDF 估算平均页数
        ↓
items_per_group = max(1, int(args.pages_per_group / 平均页数))
        ↓
populate_queue(paths, items_per_group) 切分工作项
```

为什么外部 API 场景要调小？源码注释（L1312-L1313）给了两个理由：一是外部服务商并发限制低，大页组会「压垮服务器」；二是外部 API 按量计费，如果一个页组中途失败重跑，组越大浪费的钱越多。

#### 4.3.3 源码精读

SUPPRESS 的定义处：

[olmocr/pipeline.py:L1220](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1220) —— `--pages_per_group` 的 default 是 `argparse.SUPPRESS`，是全部参数中唯一一个。

```python
parser.add_argument("--pages_per_group", type=int, default=argparse.SUPPRESS,
                    help="Aiming for this many pdf pages per work item group")
```

条件默认值的赋值处（注意它发生在解析之后、任何使用之前）：

[olmocr/pipeline.py:L1312-L1315](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1312-L1315) —— 注释原话翻译：如果你提供了 API key（意味着你在用远程付费服务商），就调小组大小默认值，免得压垮服务器、也免得组跑不完时浪费钱。

```python
# If you specify an API key, meaning you are on a remote provider, then lower the group size default...
if not hasattr(args, "pages_per_group"):
    args.pages_per_group = 50 if args.api_key is not None else 500
```

这个默认值最终被消费的地方：

[olmocr/pipeline.py:L1398-L1405](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1398-L1405) —— 先随机采样（最多 100 个）PDF 用 pypdf 数页数求平均，再按 `items_per_group = max(1, int(args.pages_per_group / avg_pages_per_pdf))` 计算每组打包几个 PDF。例如平均 10 页/份、目标 500 页/组，则每组约 50 份 PDF。

`parse_known_args` 的另一半——unknown_args 的转发终点在 `vllm_server_task`：

[olmocr/pipeline.py:L807-L834](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L807-L834) —— 先拼出基础命令（端口、tensor/data parallel、关掉 video 编码器省内存等），再按需追加 `--gpu-memory-utilization` 和 `--max-model-len`（都做了 `is not None` 判断），最后：

```python
if unknown_args:
    cmd.extend(unknown_args)     # L833-L834：未识别参数原样追加
```

也就是说，你可以写 `olmocr ./ws --pdfs a.pdf --enforce-eager`——`--enforce-eager` 不是 olmocr 的参数，argparse 不会报错，它会出现在 `vllm serve ... --enforce-eager` 的末尾。这让 olmocr 无需为 vLLM 的每个新参数都改一遍自己的 argparse 定义，两个工具的参数空间直接打通。

同族技巧之 `--disk_logging`：

[olmocr/pipeline.py:L1232-L1239](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1232-L1239) —— `nargs="?"` 表示「该选项可以带值也可以不带」；不带值时用 `const` 指定的 `olmocr-pipeline-debug.log`；完全没写这个选项时是 `default` 的 `None`。于是一句 `--disk_logging` 就能开日志，`--disk_logging my.log` 则自定义文件名；消费点在 [L1275-L1280](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1275-L1280)，为两个 logger 挂上 DEBUG 级别的文件 handler。

同族技巧之 `--server` 的三态：

[olmocr/pipeline.py:L1286](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1286) —— `use_internal_server = not args.server`：提供了 `--server` 就用外部的，没提供就自己启动 vLLM。随后 [L1432-L1433](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1432-L1433) 直接**改写** `args` 命名空间：`args.server = f"http://localhost:{args.port}/v1"`、`args.model = "olmocr"`。下游代码（如构造请求 URL 的 [L161](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L161)）从此不必关心服务器是本地的还是远程的，统一读 `args.server`。`--api_key` 同样只在有 `--server` 时才生效（[L167](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L167)）。

> 顺带一个可自行验证的细节：[L1282-L1284](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1282-L1284) 的提示日志让用户用下划线形式 `--gpu_memory_utilization 0.80`，而注册的选项名是连字符形式 `--gpu-memory-utilization`。不同 Python 版本的 argparse 对「下划线/连字符混写」的接受度不同，建议以 `olmocr --help` 实测为准（待本地验证）；写文档和脚本时用注册形式最稳妥。argparse 默认还支持无歧义的前缀缩写（如 `--max-model` 匹配 `--max-model-len`，前提是不与其他选项冲突）。

#### 4.3.4 代码实践

**实践目标**：用 10 行纯 Python 复现 `argparse.SUPPRESS` + 条件默认值机制，亲眼确认「没写就没有这个属性」。

**操作步骤**：

1. 把下面的脚本存为 `suppress_demo.py`（**示例代码**，不依赖 olmocr，任何 Python 3 均可运行）：

```python
# 示例代码：复现 pipeline.py L1220 与 L1312-L1315 的机制
import argparse

parser = argparse.ArgumentParser()
parser.add_argument("--pages_per_group", type=int, default=argparse.SUPPRESS)
parser.add_argument("--api_key", type=str, default=None)

args = parser.parse_args()  # 不带任何参数运行
print("hasattr:", hasattr(args, "pages_per_group"))  # 预期 False

if not hasattr(args, "pages_per_group"):
    args.pages_per_group = 50 if args.api_key is not None else 500
print("条件默认值:", args.pages_per_group)  # 预期 500

# 对照组：普通 default=None 的属性永远存在
p2 = argparse.ArgumentParser()
p2.add_argument("--pages_per_group", type=int, default=None)
print("default=None 时:", vars(p2.parse_args([])))  # 值存在且为 None
```

2. 运行 `python suppress_demo.py`，记下三行输出。
3. 再分别运行 `python suppress_demo.py --pages_per_group 100` 和 `python suppress_demo.py --api_key sk-test`，观察输出如何变化。

**需要观察的现象**：不带参数时 `hasattr` 为 `False`；加了 `--api_key` 后条件默认值变成 50；加了 `--pages_per_group 100` 后 `hasattr` 为 `True` 且值保持 100 不被覆盖。

**预期结果**：三种运行的输出分别对应「本地默认 500」「外部 API 默认 50」「用户显式指定优先」。这正是 [L1312-L1315](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1312-L1315) 的行为模型。（本讲义写作环境未执行脚本，读者本地运行即得结果。）

#### 4.3.5 小练习与答案

**练习 1**：如果 `--pages_per_group` 改用 `default=None`，再用 `if args.pages_per_group is None` 判断，功能上可行吗？SUPPRESS 的额外价值是什么？

**参考答案**：在本例中功能上可行（None 不是该参数的合法业务值）。SUPPRESS 的额外价值在于通用性：当某参数的合法值可能包含 `0`、`False`、`None` 时，`is None` 或 `if not x` 的哨兵判断会失真，而 `hasattr` 永远准确；它也把「属性不存在」这一信号显式暴露在类型层面，阅读代码的人一眼就知道这里有条件默认值逻辑。

**练习 2**：`olmocr ./ws --pdfs a.pdf --swap-space 4` 能正常运行吗？`--swap-space 4` 去了哪里？

**参考答案**：能。`--swap-space` 不是 olmocr 注册的参数，`parse_known_args()` 把它收进 `unknown_args`；只要这次运行会启动内部 vLLM（没给 `--server`），[L833-L834](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L833-L834) 会把它追加到 `vllm serve` 命令末尾，最终由 vLLM 解释。若用了 `--server` 外部服务，`unknown_args` 则不会被使用（本地不启动 vLLM）。

**练习 3**：`--disk_logging` 不带值、带值、完全不写三种情形下，`args.disk_logging` 分别是什么？

**参考答案**：分别为 `const` 值 `olmocr-pipeline-debug.log`、用户给的值（如 `my.log`）、`default` 值 `None`。三态来自 `nargs="?"` + `const` + `default` 的组合，定义在 [L1232-L1239](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1232-L1239)。

## 5. 综合实践

**任务：为三种部署形态各写出一条「带注释的」完整 olmocr 命令，并预填参数表。**

1. **形态 A——本地 GPU**：单卡、16GB 显存紧张。写出命令并解决「KV cache 超显存」问题（提示：`--gpu-memory-utilization`、`--max_model_len`，参考 [L1282-L1284](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1282-L1284) 的提示与 [README.md:L274](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/README.md#L274) 的示例）。
2. **形态 B——外部 API（DeepInfra）**：仿照 [README.md:L340](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/README.md#L340)，写命令并回答：此时 `pages_per_group` 的默认值是多少？为什么（引用 [L1312-L1315](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1312-L1315) 的注释）？
3. **形态 C——Beaker 集群 4 副本**：参考 [README.md:L384](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/README.md#L384)，写命令并说明 `--beaker` 开关按下后本地进程实际做了什么（提示：[L1418-L1420](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1418-L1420)）。
4. 用 `olmocr --help` 核对三条命令的每个参数拼写（尤其是连字符形式），修正后把 4.1.4 的参数表补全成「三形态对照表」：每个参数一行，三列分别标注 A/B/C 是否需要改、改成什么。
5. 最后跑一遍 4.3.4 的 `suppress_demo.py`，确认你能在 30 秒内向别人讲清「为什么 olmocr 的 `--help` 里查不到 `pages_per_group` 的默认值」。

形态 B/C 的实际提交（API key、Beaker 账号）为待本地验证项；命令拼写与参数语义的核对在本机 `--help` 即可完成。

## 6. 本讲小结

- olmOCR 的几十个 CLI 参数定义全部集中在 [pipeline.py:L1200-L1272](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1200-L1272)，按「顶层输入 / 详细配置 / Server / VLLM / Beaker」分组，恰好对应四种运行形态；`--help` 的分组小节与 `add_argument_group` 一一对应。
- 默认值面向「自有 GPU 大批量」场景：`--workers 20`、`--max_concurrent_requests 1600`、`--pages_per_group` 动态 500；走外部计费 API 时 README 建议显式调小为 `1` / `20`，配合条件默认值 50。
- `parse_known_args()` 收集未识别参数并在启动内部 vLLM 时原样追加到 `vllm serve` 末尾，实现两个工具参数空间的打通；但该转发只在本地启动 vLLM 时生效。
- `argparse.SUPPRESS` 让「用户没写」表现为属性不存在，`hasattr` 探测后按 `--api_key` 有无赋予 50 或 500 的默认值——「解析后条件默认值」是本讲最值得抄走的技巧；同族还有 `--disk_logging` 的 `nargs="?"` 三态和 `--server` 缺省时对 `args.server`/`args.model` 的运行期改写。
- README 的 `--help` 快照已落后于源码（旧默认模型名、缺 `--api_key`/`--disk_logging` 等参数）；查参数以 argparse 源码为准。
- `--stats`（GPU 检查前）与 `--beaker`（提交后即返回）是两个短路开关，`--workers` 与 `--max_concurrent_requests` 是两道不同粒度的并发闸门。

## 7. 下一步学习建议

本讲结束意味着第 1 单元（认识项目并跑起来）完成。你已经掌握的所有参数，都会在第 2 单元「核心流水线精读」中找到真正的消费现场：

- 下一讲 **u2-l1（pipeline 主流程）**：从 `main()` 全景走一遍——创建 WorkQueue、收集 PDF、启动 vLLM、拉起 `args.workers` 个协程，本讲的参数表就是那张地图的图例。
- 随后的 **u2-l2（输入收集）** 会精读 `--pdfs` 的五种输入形态与 `items_per_group` 的采样估算；**u2-l6（单页处理）** 会用上 `--max_page_retries`；**u2-l8（输出侧）** 对应 `--markdown` 与 `--max_page_error_rate`。
- 如果你被 `--pages_per_group` 的分组机制勾起兴趣，可以先跳读 [olmocr/work_queue.py](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/work_queue.py) 中 `populate_queue` 的签名，第 3 单元 u3-l1 会正式精读。
