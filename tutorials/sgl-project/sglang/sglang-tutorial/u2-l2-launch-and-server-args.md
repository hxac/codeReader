# 启动流程与 ServerArgs 配置

## 1. 本讲目标

本讲承接 u2-l1（多进程架构与 ZMQ IPC），把视线从「进程之间怎么通信」上移到「这些进程是怎么被一行命令拉起来的」。

读完本讲，你应当能够：

- 从 `sglang serve` 这一行命令出发，完整追踪出 `serve()` → `prepare_server_args()` → `run_server()` → `http_server.launch_server()` 的调用链，并说出每一站做了什么。
- 理解 `ServerArgs` 这个巨大的配置数据类是如何用 `A[T, ...]`（即 `typing.Annotated`）把「数据类字段」自动变成「CLI 参数」的。
- 说清 `model_path`、`tp_size`、`dp_size`、`chunked_prefill_size`、`mem_fraction_static` 这五个关键字段的默认值、用途，以及为什么有些「默认值」其实是在 `__post_init__` 里被算出来的。
- 知道如何用命令行参数或 `--config` 配置文件覆盖这些字段。

## 2. 前置知识

在进入源码前，先建立两个直觉。

### 2.1 数据类（dataclass）与 `typing.Annotated`

Python 的 `@dataclasses.dataclass` 让你用「字段声明」的方式定义一个容器类，编译器会自动为你生成 `__init__` 等方法。SGLang 的 `ServerArgs` 就是一个数据类，但它有上百个字段，如果每个字段都要手写一段 `parser.add_argument(...)`，既啰嗦又容易漏改。

`typing.Annotated[T, meta]` 是 Python 的类型注解语法，它表示「类型是 `T`，同时附带一段元信息 `meta`」。SGLang 把 CLI 参数的元信息（help 文本、可选值、别名）塞进 `meta`，再写一个工具函数自动扫描字段、生成对应的 `argparse` 参数。这样「声明一个字段」就等于「同时声明了 CLI 参数和默认值」，单一数据源。

### 2.2 模式分发（dispatcher）

SGLang 不止能跑「标准 HTTP 服务」，还能跑 Ray 后端、gRPC 服务、编码器分离服务（PD 分离）等。`run_server()` 函数用一连串 `if/elif` 根据 `server_args` 里的标志位选择真正要执行的入口，这种写法叫做「分发器（dispatcher）」。本讲要读的几个函数，大多都是薄薄的分发器，把细节推到更深的函数里。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `python/sglang/cli/serve.py` | `sglang serve` 子命令的入口。解析 `--model-type`、判断是语言模型还是扩散模型，语言模型分支会构造 `ServerArgs` 并调用 `run_server`。 |
| `python/sglang/launch_server.py` | 提供 `run_server(server_args)`，按运行模式（默认 HTTP / Ray / gRPC / 编码分离）分发到具体的 `launch_server`。 |
| `python/sglang/srt/server_args.py` | 定义 `ServerArgs` 数据类（上百个字段）、`prepare_server_args()`（CLI → `ServerArgs`）、`__post_init__`（默认值派生与校验）、`url()` 等。是本讲最重的文件。 |
| `python/sglang/srt/entrypoints/http_server.py` | 默认 HTTP 模式入口，`launch_server()` 拉起 TokenizerManager / Scheduler / Detokenizer 三个进程，再启动 FastAPI + uvicorn。 |
| `python/sglang/srt/arg_groups/arg_utils.py` | 提供 `A`（=`Annotated`）、`Arg`、`add_cli_args_from_dataclass`，是 `ServerArgs` 字段自动转 CLI 参数的引擎。 |

## 4. 核心概念与源码讲解

### 4.1 启动链路总览：从 `sglang serve` 到运行中的服务

#### 4.1.1 概念说明

当你在终端敲下 `sglang serve --model-path <模型> --tp 2`，背后发生的事情可以拆成三段：

1. **CLI 分发**：`sglang` 命令（来自 `pyproject.toml` 的 `[project.scripts]`）进入 `cli/main.py`，识别到 `serve` 子命令，调用 `cli/serve.py` 的 `serve(args, extra_argv)`。`serve()` 先判断这是语言模型还是扩散模型——语言模型才走本讲的链路。
2. **配置构造**：`serve()` 调用 `prepare_server_args(argv)`，把命令行字符串解析成一个 `ServerArgs` 对象。这一步会把缺省的字段补上默认值（很多默认值要根据 GPU 显存动态算出来）。
3. **进程拉起**：`serve()` 调用 `run_server(server_args)`，后者在默认模式下转发到 `http_server.launch_server()`，由它拉起 TokenizerManager、Scheduler、Detokenizer 三个进程（回顾 u2-l1 的多进程拓扑），再启动 FastAPI HTTP 服务。

`ServerArgs` 对象是贯穿这三段的「唯一配置事实来源」：它从命令行诞生，被一路传递到最底层的每个进程。

#### 4.1.2 核心流程

用伪代码描述语言模型的启动链路：

```
sglang serve --model-path M --tp 2
   │
   ▼  cli/main.py 识别子命令
serve(args, extra_argv)                     # cli/serve.py
   │  _extract_model_type_override()        # 剥离 --model-type
   │  _normalize_positional_model_path()    # 允许 `sglang serve <model>` 位置参数
   │  get_model_path()                      # 提取/推断模型路径
   │  get_is_diffusion_model()              # auto 模式自动探测
   │  ── 语言模型分支 ──
   ├─▶ prepare_server_args(dispatch_argv)   # server_args.py
   │       parser = ArgumentParser(prog="sglang serve")
   │       ServerArgs.add_cli_args(parser)  # 自动生成 --tp 等
   │       raw_args = parser.parse_args(argv)
   │       return ServerArgs.from_cli_args(raw_args)   # 触发 __post_init__
   │
   └─▶ run_server(server_args)              # launch_server.py
           └─ 默认 HTTP 模式 ─▶ http_server.launch_server(server_args)
                                   ├─ Engine._launch_subprocesses(...)
                                   │     → TokenizerManager + Scheduler + Detokenizer
                                   └─ _setup_and_run_http_server(...)
                                         → FastAPI + uvicorn，监听 :30000
```

注意 `serve()` 全程包在 `try / finally` 里，`finally` 调用 `kill_process_tree(...)`：哪怕中途异常，也会把派生出去的子进程（Scheduler、Detokenizer 等）一并回收，避免僵尸进程。

#### 4.1.3 源码精读

先看 `serve()` 的语言模型分支，这是把「命令行」和「配置对象」接起来的关键几行：

[python/sglang/cli/serve.py:134-143](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/cli/serve.py#L134-L143) —— 语言模型分支：调用 `prepare_server_args` 构造配置，再交给 `run_server` 执行；`finally` 里的 `kill_process_tree` 负责清理子进程。

再看 `serve()` 开头对参数的预处理。`sglang serve` 允许两种写法：`sglang serve --model-path <M>` 和 `sglang serve <M>`（位置参数）。后者由 `_normalize_positional_model_path` 归一化成前者：

[python/sglang/cli/serve.py:49-53](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/cli/serve.py#L49-L53) —— 把位置参数模型路径改写成 `--model-path <M>`，让后续的 argparse 逻辑统一处理。

`_extract_model_type_override` 则负责从 argv 里抠出 `--model-type {auto,llm,diffusion}`，它不交给 argparse 而是手动解析，因为「服务类型」要在 argparse 之前就决定（不同类型用不同的参数集）：

[python/sglang/cli/serve.py:16-46](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/cli/serve.py#L16-L46) —— 手动剥离 `--model-type`，校验取值，并返回过滤后的 argv。

#### 4.1.4 代码实践

**实践目标**：不启动服务，只用源码追踪验证启动链路。

**操作步骤**：

1. 打开 `python/sglang/cli/serve.py`，在 `serve()` 函数里找到 `is_diffusion_model` 的判断（约 L107-L118），确认：当 `model_type == "auto"` 时走 `get_is_diffusion_model()` 自动探测，否则按显式指定的类型分流。
2. 在 L134-L141 标出语言模型分支的两步：`prepare_server_args(dispatch_argv)` 与 `run_server(server_args)`。
3. 打开 `python/sglang/launch_server.py`，在 `run_server` 的 `else` 分支（L48-L52）找到默认调用的 `from sglang.srt.entrypoints.http_server import launch_server`。

**需要观察的现象**：`serve()` 的语言模型分支里，`prepare_server_args` 和 `run_server` 是**先后两步**——前者产出配置对象，后者消费它。这印证了「`ServerArgs` 是贯穿全链路的唯一配置来源」。

**预期结果**：你能在三个文件里画出一条 `serve() → prepare_server_args() → run_server() → http_server.launch_server()` 的直线调用链，中间没有 fork、没有网络，全是普通函数调用。

#### 4.1.5 小练习与答案

**练习 1**：如果用户运行 `sglang serve my-model --tp 2`（位置参数），`_normalize_positional_model_path` 会把它改写成什么？

**答案**：改写成 `["--model-path", "my-model", "--tp", "2"]`，即把第一个非 `-` 开头的参数显式变成 `--model-path` 的值，其余参数原样保留。

**练习 2**：为什么 `serve()` 要把整个模型分发逻辑包在 `try / finally` 里，并在 `finally` 调 `kill_process_tree`？

**答案**：因为 `run_server` 会派生 Scheduler、Detokenizer 等子进程；如果服务异常退出而没有清理，这些子进程会变成孤儿/僵尸进程。`finally` 保证无论正常退出还是异常，子进程都被一并回收。

---

### 4.2 `launch_server.run_server` 的四种模式分发

#### 4.2.1 概念说明

`run_server(server_args)` 是一个非常薄的函数，它只做一件事：**根据 `server_args` 上的标志位，选择真正要执行的启动入口**。SGLang 有多种部署形态，每一种对应一个不同的 `launch_server` 实现：

| 模式标志 | 形态 | 实际入口 |
| --- | --- | --- |
| `encoder_only`（无 grpc） | 多模态编码器分离服务 | `disaggregation.encode_server.launch_server` |
| `encoder_only`（含 grpc） | 编码器 gRPC 分离服务 | `disaggregation.encode_grpc_server.serve_grpc_encoder` |
| `smg_grpc_mode` | 旧版 SMG gRPC 服务 | `entrypoints.grpc_server.serve_grpc` |
| `use_ray` | Ray 后端的 HTTP 服务 | `srt.ray.http_server.launch_server` |
| （以上都不命中） | **默认 HTTP 服务** | `entrypoints.http_server.launch_server` |

本讲（以及后续大多数讲义）聚焦的「普通 HTTP 服务」就是最后一行。其他形态分别对应 U9（PD 分离、编码服务）和分布式部署。

#### 4.2.2 核心流程

```
run_server(server_args):
    if server_args.encoder_only:        # 编码器分离
        if grpc:  serve_grpc_encoder()  #   gRPC 形态
        else:     encode_server.launch_server()
    elif server_args.smg_grpc_mode:     # 旧版 gRPC
        serve_grpc()
    elif server_args.use_ray:           # Ray 后端
        ray.http_server.launch_server()
    else:                               # 默认 HTTP（最常用）
        http_server.launch_server()
```

判定的优先级从前到后：先看是否「只做编码器」，再看是否「旧版 gRPC」，再看是否「Ray」，最后才落到默认 HTTP。这个顺序意味着 `encoder_only` 是最高优先级的特殊形态。

#### 4.2.3 源码精读

整个分发器只有不到 40 行，读起来像一张查表：

[python/sglang/launch_server.py:15-52](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/launch_server.py#L15-L52) —— `run_server` 完整实现：四个 `if/elif` 分支对应四种部署形态，每个分支里 `import` 具体入口并调用。注意 import 都写在分支**内部**，是「懒加载」，避免拉起一个简单 HTTP 服务时还连带加载 Ray/gRPC 等可选依赖。

特别看默认分支：

[python/sglang/launch_server.py:48-52](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/launch_server.py#L48-L52) —— 默认 HTTP 模式：`from sglang.srt.entrypoints.http_server import launch_server` 后立即 `launch_server(server_args)`。

`launch_server.py` 也保留了一个 `__main__` 入口，支持 `python -m sglang.launch_server` 这种老写法，但会打印一条警告推荐改用 `sglang serve`：

[python/sglang/launch_server.py:55-73](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/launch_server.py#L55-L73) —— `python -m sglang.launch_server` 入口：发出弃用警告，加载插件，用 `prepare_server_args(sys.argv[1:])` 构造配置后调 `run_server`，`finally` 同样 `kill_process_tree`。

#### 4.2.4 代码实践

**实践目标**：理解分发器的判定顺序，能够预测给定配置会进入哪个分支。

**操作步骤**：

1. 假设你用 `sglang serve --model-path M`（不附加任何模式开关）启动，预测 `server_args.encoder_only`、`smg_grpc_mode`、`use_ray` 各是什么值（回顾 4.3 会看到它们默认都是 `False`）。
2. 在 `run_server` 里顺着 `if/elif` 走一遍，确认这种「裸启动」会落到最后的 `else` → `http_server.launch_server`。
3. 思考：如果同时设了 `--use-ray` 又想跑 HTTP，会进入哪个分支？（提示：`use_ray` 的判定在 `smg_grpc_mode` 之后。）

**需要观察的现象**：分发器是「第一个命中的分支生效」，靠的是 `if/elif` 的短路语义，而不是优先级数字。

**预期结果**：裸启动进入默认 HTTP 分支；只要 `encoder_only=True`，无论其他标志如何都会走编码器分离分支（因为它在最前面）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `run_server` 把每个 `import` 都写在对应分支里，而不是统一放在文件顶部？

**答案**：为了懒加载/可选依赖。默认 HTTP 模式的用户不需要安装 Ray；如果把 `from sglang.srt.ray.http_server import launch_server` 放在文件顶部，任何启动都会强制要求 Ray 已安装。分支内 import 让可选依赖只在真正用到时才被加载。

**练习 2**：`python -m sglang.launch_server` 和 `sglang serve` 哪个是推荐入口？为什么源码里还保留前者？

**答案**：`sglang serve` 是推荐入口（`__main__` 里会打印弃用警告）。保留前者是为了向后兼容旧脚本和旧文档，避免破坏现有用户的自动化调用。

---

### 4.3 `ServerArgs` 数据类：注解驱动的上百字段配置

#### 4.3.1 概念说明

`ServerArgs` 是 SGLang 最核心的配置对象，包含上百个字段，覆盖模型路径、并行度、内存、调度、分布式、量化、投机解码、PD 分离等方方面面。它的精妙之处在于：**你只需要声明字段，CLI 参数就自动生成了**。

实现这一点的机制是 `A[T, ...]`，其中 `A` 就是 `typing.Annotated` 的别名。字段的注解里塞入一个 `Arg(...)`（或一段纯字符串作为 help 文本），`add_cli_args_from_dataclass` 会扫描这些元信息，自动给 argparse 注册参数：

- 字段名 `tp_size` → 自动生成 CLI 标志 `--tp-size`。
- `Arg(aliases=["--tensor-parallel-size"])` → 额外注册一个长别名。
- `Arg(choices=[...])` → 限制可选值。
- `Arg(no_cli=True)` → 该字段不暴露到 CLI（只能通过代码或配置文件设置）。

字段声明的「默认值」就是 CLI 参数的默认值。所以**一处声明，三处生效**：Python 字段、CLI 参数、默认值。

#### 4.3.2 核心流程

```
# 声明（在 ServerArgs 类里）：
tp_size: A[int, Arg(help="The tensor parallelism size.",
                    aliases=["--tensor-parallel-size"])] = 1

# prepare_server_args(argv) 时：
parser = ArgumentParser(prog="sglang serve")
ServerArgs.add_cli_args(parser)              # 自动扫描字段 → 注册 --tp-size / --tensor-parallel-size
raw_args = parser.parse_args(argv)           # argv 里的 --tp 2 → raw_args.tp_size = 2
server_args = ServerArgs.from_cli_args(raw_args)  # 构造对象，触发 __post_init__
```

`add_cli_args` 的主体就是一行自动扫描，外加少量「无法用注解表达」的特殊参数（动态 choices、`--config`、已弃用标志）需要手动注册。

#### 4.3.3 源码精读

先看 `A` 与 `Arg` 的定义，这是理解一切的基础：

[python/sglang/srt/arg_groups/arg_utils.py:58-62](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/arg_groups/arg_utils.py#L58-L62) —— `A = Annotated`（别名）；`Arg` 是一个 `frozen=True` 的数据类，字段包括 `help`、`choices`、`aliases`、`cli_name`、`no_cli` 等 CLI 元信息。

再看 `ServerArgs` 类的文档注释，它直接告诉维护者「如何加一个新参数」——这正是注解驱动体系的「使用说明书」：

[python/sglang/srt/server_args.py:412-451](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/server_args.py#L412-L451) —— `ServerArgs` 类定义与文档：说明了 `A[T, ...]` 注解规范、字段应放入对应注释分节、以及只有「弃用标志 / 动态 choices / `--config`」这三类才需要在 `add_cli_args` 里手动注册。

看一个最简单的字段和最关键的字段：

[python/sglang/srt/server_args.py:1062-1063](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/server_args.py#L1062-L1063) —— 最简形式：`host` 和 `port`，注解里直接是一段纯字符串（等于 `Arg(help=...)`），默认值 `"127.0.0.1"` / `30000` 就是 CLI 默认值。

[python/sglang/srt/server_args.py:456-462](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/server_args.py#L456-L462) —— `model_path`：**没有默认值**（必填），带别名 `--model`，所以 `sglang serve --model M` 和 `--model-path M` 等价。

再看「自动转 CLI」的引擎本身：

[python/sglang/srt/server_args.py:7141-7144](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/server_args.py#L7141-L7144) —— `add_cli_args` 主体就是 `add_cli_args_from_dataclass(parser, ServerArgs)`；后面才手动补充动态 choices（`--reasoning-parser` 等）、`--config` 和弃用标志。

最后是「Namespace → ServerArgs」的反向转换：

[python/sglang/srt/server_args.py:7383-7390](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/server_args.py#L7383-L7390) —— `from_cli_args`：遍历数据类字段，只取 Namespace 上确实存在的属性（跳过 `no_cli` 等无 CLI 表面的字段，让其走数据类默认值），再 `cls(**{...})` 构造对象。

#### 4.3.4 代码实践

**实践目标**：亲手验证「字段声明 = CLI 参数」的自动生成机制。

**操作步骤**：

1. 在终端运行（**待本地验证**）：

   ```bash
   python -c "
   import argparse
   from sglang.srt.server_args import ServerArgs
   p = argparse.ArgumentParser(prog='sglang serve')
   ServerArgs.add_cli_args(p)
   p.parse_args(['--help'])
   "
   ```

2. 观察输出的帮助文本里是否同时出现了 `--tp-size`、`--tensor-parallel-size`（两者都来自 `tp_size` 一个字段）、`--dp-size`、`--model-path`、`--model`。

**需要观察的现象**：你并没有在代码里手写 `parser.add_argument("--tp-size", ...)`，但它确实出现在帮助里——这就是 `add_cli_args_from_dataclass` 扫描注解自动生成的。

**预期结果**：帮助文本里 `--tp-size` 和 `--tensor-parallel-size` 指向同一个参数；`--model-path` 与 `--model` 互为别名。

#### 4.3.5 小练习与答案

**练习 1**：字段 `host: A[str, "The host of the HTTP server."] = "127.0.0.1"` 里，注解中的纯字符串 `"The host of the HTTP server."` 起什么作用？

**答案**：它是 CLI 参数的 help 文本。`A` 把它当作 `Arg` 的简写，等价于 `Arg(help="The host of the HTTP server.")`，扫描时会据此生成 `--host` 参数的帮助说明。

**练习 2**：如果一个字段不希望暴露到命令行（只能通过代码设置），该怎么做？

**答案**：在 `Arg(...)` 里设置 `no_cli=True`。例如 `disable_cuda_graph: A[bool, Arg(no_cli=True)] = False`，扫描时会跳过它，不为它生成 CLI 参数，但仍保留为数据类字段。

---

### 4.4 关键字段精读、默认值派生与 `prepare_server_args`

#### 4.4.1 概念说明

本模块回答实践任务的核心问题：`model_path`、`tp_size`、`dp_size`、`chunked_prefill_size`、`mem_fraction_static` 这五个字段，默认值是什么、做什么用、怎么改。

这五个字段里有一个重要陷阱：**有两个的「默认值」并不是字段声明里写的那个**。`chunked_prefill_size` 和 `mem_fraction_static` 声明里都是 `None`，真正的值是在 `__post_init__` 里根据 GPU 显存动态算出来的。`__post_init__` 是数据类在构造完成后自动调用的钩子，SGLang 把它写成一个**有序的分发器（dispatcher）**——一长串 `self._handle_xxx()` 调用，每个 handler 负责一类配置的「补默认值 + 校验」。

#### 4.4.2 核心流程

`prepare_server_args(argv)` 的流程：

```
prepare_server_args(argv):
    parser = ArgumentParser(prog="sglang serve")
    ServerArgs.add_cli_args(parser)          # 注册所有 --xxx
    if "--config" in argv:                   # 支持 YAML 配置文件
        argv = ConfigArgumentMerger(...).merge_config_with_args(argv)
    raw_args = parser.parse_args(argv)       # 命令行 → Namespace
    logging.basicConfig(level=raw_args.log_level, ...)  # 提前配置日志
    return ServerArgs.from_cli_args(raw_args)# Namespace → ServerArgs
        └─ __post_init__ 自动触发
              ├─ 若 model_path ∈ {none, dummy}：直接 return（用于测试/profiling）
              ├─ _handle_model_source_paths() / _handle_multimodal() ...
              ├─ _handle_missing_default_values()
              ├─ _handle_gpu_memory_settings(gpu_mem)   # ← 算 chunked_prefill_size / mem_fraction_static
              ├─ _handle_attention_backend_compatibility()
              └─ ... 一系列 _handle_* 与校验
```

`_handle_gpu_memory_settings` 里的两个启发式（heuristic）：

- **`chunked_prefill_size` 默认值按 GPU 显存分档**：T4/4080 这类小卡给 2048，A100 40GB 给 4096，H100/H200 给 8192，B200/MI300 给 16384。设成 `-1` 表示关闭 chunked prefill。
- **`mem_fraction_static` 由预留显存反推**：

  \[
  \text{mem\_fraction\_static} = \frac{\text{gpu\_mem} - \text{reserved\_mem}}{\text{gpu\_mem}}
  \]

  其中 `reserved_mem` 用来为激活值（与 `chunked_prefill_size` 成正比）和 CUDA graph 缓冲（与 decode `max_bs` 成正比）留空间。简化的估计式是 `reserved_mem ≈ chunked_prefill_size * 1.5 + max_bs * 2`（单位 MB）。

#### 4.4.3 源码精读

**字段 1：`model_path`** —— 必填，模型权重路径（本地目录或 HuggingFace repo id）。

[python/sglang/srt/server_args.py:456-462](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/server_args.py#L456-L462) —— 无默认值（必填），别名 `--model`。

**字段 2：`tp_size`** —— 张量并行度，默认 1。

[python/sglang/srt/server_args.py:888-894](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/server_args.py#L888-L894) —— `tp_size: ... = 1`，别名 `--tensor-parallel-size`，CLI 主名 `--tp`（由 `tp_size` 自动派生为 `--tp-size`）。

**字段 3：`dp_size`** —— 数据并行度，默认 1。

[python/sglang/srt/server_args.py:914-920](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/server_args.py#L914-L920) —— `dp_size: ... = 1`，别名 `--data-parallel-size`。

**字段 4：`chunked_prefill_size`** —— 声明默认 `None`，实际默认由 GPU 显存决定。

[python/sglang/srt/server_args.py:714-717](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/server_args.py#L714-L717) —— 字段声明：`Optional[int]`，默认 `None`；`-1` 表示关闭 chunked prefill。

[python/sglang/srt/server_args.py:3906-3966](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/server_args.py#L3906-L3966) —— `_handle_gpu_memory_settings` 中按 GPU 显存分档补默认值（仅当仍为 `None` 时）：`<20GB→2048`、`<35GB→2048`、`<60GB→4096`、`<90GB(H100)→8192`、`<160GB(H200)→8192`、`≥160GB(B200)→16384`；`gpu_mem` 为 `None` 时兜底 `4096`。

**字段 5：`mem_fraction_static`** —— 声明默认 `None`，实际默认由公式反推。

[python/sglang/srt/server_args.py:690-693](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/server_args.py#L690-L693) —— 字段声明：`Optional[float]`，默认 `None`；含义是「（模型权重 + KV 缓存池）/ GPU 显存」。

[python/sglang/srt/server_args.py:4064-4068](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/server_args.py#L4064-L4068) —— `mem_fraction_static` 的实际计算：`(gpu_mem - reserved_mem) / gpu_mem`（`gpu_mem` 为 `None` 时兜底 `0.88`）。`reserved_mem` 在上方（L4050-L4062）累加激活值、CUDA graph 缓冲、并行度开销等。

**`__post_init__` 分发器骨架**：

[python/sglang/srt/server_args.py:2883-2916](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/server_args.py#L2883-L2916) —— `__post_init__` 开头：对 `model_path ∈ {none, dummy}` 直接返回（用于单元测试/profiling）；否则按「模型源路径 → 多模态 → SSL → 弃用参数 → 缺省值 → PD 分离 → CUDA graph → 后端 → GPU 显存 → 模型特定调整 → 注意力后端 → 内存/缓存」的领域顺序调用一串 `self._handle_*`。这套写法保证默认值派生与校验有确定的依赖顺序。

**`prepare_server_args` 全貌**：

[python/sglang/srt/server_args.py:8234-8270](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/server_args.py#L8234-L8270) —— `prepare_server_args`：构造 parser、`add_cli_args`、可选的 `--config` YAML 合并、`parse_args`、提前配置日志、最后 `from_cli_args` 构造 `ServerArgs`（触发 `__post_init__`）。

#### 4.4.4 代码实践（本讲主实践）

**实践目标**：把这五个字段填进一张表，并用 CLI 覆盖其中几个启动服务（或至少生成 `ServerArgs` 对象观察派生结果）。

**操作步骤**：

1. 先在源码里定位并填写下表（字段名、声明默认值、CLI 主名/别名、实际生效默认值、用途）：

   | 字段 | 声明默认 | CLI 主名 / 别名 | 实际生效默认 | 用途 |
   | --- | --- | --- | --- | --- |
   | `model_path` | （无，必填） | `--model-path` / `--model` | 必填 | 模型权重路径 |
   | `tp_size` | `1` | `--tp-size` / `--tensor-parallel-size` | `1` | 张量并行度 |
   | `dp_size` | `1` | `--dp-size` / `--data-parallel-size` | `1` | 数据并行度 |
   | `chunked_prefill_size` | `None` | `--chunked-prefill-size` | 按 GPU 显存分档（见上） | 单个 prefill 分块最大 token 数 |
   | `mem_fraction_static` | `None` | `--mem-fraction-static` | 由公式反推（见上） | 静态显存占用比例 |

2. **不依赖 GPU 也能验证派生逻辑**（推荐先做这步）：用 `dummy` 模型跳过重型初始化，只观察 `ServerArgs` 构造（**待本地验证**）：

   ```python
   from sglang.srt.server_args import prepare_server_args
   # dummy 模型会让 __post_init__ 提前 return，便于观察 CLI 解析本身
   sa = prepare_server_args(["--model-path", "dummy", "--tp", "2", "--port", "30001"])
   print(sa.model_path, sa.tp_size, sa.dp_size, sa.port)
   ```

3. **真实启动并覆盖字段**（需要 GPU 与已下载的小模型，**待本地验证**）：

   ```bash
   # 覆盖 tp/dp/chunked-prefill/mem-fraction 并启动
   sglang serve --model-path <小模型> \
       --tp 2 --dp 1 \
       --chunked-prefill-size 2048 \
       --mem-fraction-static 0.85 \
       --port 30000
   ```

**需要观察的现象**：

- 步骤 2：`sa.tp_size` 应为 `2`，`sa.port` 应为 `30001`，验证 CLI 覆盖生效；由于 `model_path=dummy`，`__post_init__` 提前返回，`chunked_prefill_size`/`mem_fraction_static` 不会被派生（保持 `None`）——这恰好印证了「派生逻辑依赖真实模型与 GPU 信息」。
- 步骤 3：启动日志里应能看到最终生效的 `chunked_prefill_size` 与 `mem_fraction_static`，对比你显式传入的 `2048` / `0.85` 是否被采纳。

**预期结果**：

- 不传 `--chunked-prefill-size` 时，日志显示按 GPU 分档的默认值；传入 `2048` 时显示 `2048`。
- `--mem-fraction-static 0.85` 覆盖后，KV 缓存池按该比例分配；设得过大可能 OOM，设得过小则 KV 缓存变小、吞吐下降。

> 说明：步骤 2/3 的具体运行输出依赖你的本地 GPU 型号与已安装依赖，故标注「待本地验证」。源码定位部分（步骤 1）可以离线完成。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `chunked_prefill_size` 和 `mem_fraction_static` 的字段声明默认值是 `None` 而不是具体数字？

**答案**：因为它们的合理默认值依赖运行环境（GPU 显存、模型结构），无法在声明时写死。`None` 作为「占位符」，留给 `__post_init__` 的 `_handle_gpu_memory_settings` 根据实际 GPU 显存动态计算。如果用户在 CLI 显式指定了值，handler 里的 `if self.chunked_prefill_size is None:` 判断会跳过覆盖，尊重用户输入。

**练习 2**：`--mem-fraction-static 0.99` 会有什么风险？

**答案**：`mem_fraction_static` 表示预留给「模型权重 + KV 缓存池」的显存比例。设成 `0.99` 意味着只给激活值、CUDA graph 缓冲、临时张量留 1% 显存，极易在 prefill（激活值与 `chunked_prefill_size` 成正比）或 CUDA graph 捕获阶段触发 OOM。生产环境通常让它走默认派生，只在显存紧张时手动调小（如 `0.85`）而不是调大。

**练习 3**：`prepare_server_args` 里 `logging.basicConfig` 为什么要放在 `parse_args` 之后、`from_cli_args` 之前？

**答案**：因为日志级别取自 `raw_args.log_level`（先 parse 才拿得到），而 `__post_init__`（在 `from_cli_args` 内触发）会调用大量 `logger.info` / `logger.warning`——日志必须先配置好，这些输出才会按正确格式和级别打印。

---

### 4.5 `http_server.launch_server`：拉起三进程引擎与 HTTP 外壳

#### 4.5.1 概念说明

进入默认分支后，真正干活的入口是 `http_server.launch_server(server_args)`。回顾 u2-l1：一个运行中的 SGLang 服务由三个逻辑进程组成——TokenizerManager（分词）、Scheduler（调度 + 前向）、DetokenizerManager（解码）。`launch_server` 做两件事：

1. **拉起引擎子进程**：委托 `Engine._launch_subprocesses(...)` 创建这三个进程（TokenizerManager 与 HTTP 同在主进程，Scheduler/Detokenizer 是子进程），并用 ZMQ 把它们连成环。
2. **启动 HTTP 外壳**：调用 `_setup_and_run_http_server(...)` 搭建 FastAPI 应用、注册 OpenAI 兼容路由（`/v1/chat/completions` 等），最后用 uvicorn 监听 `server_args.host:server_args.port`（默认 `127.0.0.1:30000`）。

注意这个函数大量使用「可注入的回调」参数（`init_tokenizer_manager_func` 等），默认值是真实的实现，但测试时可以替换成 mock——这是一种便于测试的依赖注入设计。

#### 4.5.2 核心流程

```
http_server.launch_server(server_args, ...):
    (tokenizer_manager, template_manager, port_args,
     scheduler_init_result, subprocess_watchdog) =
        Engine._launch_subprocesses(server_args, ...)   # 拉起三进程 + ZMQ 连接
    _setup_and_run_http_server(server_args, tokenizer_manager,
        template_manager, port_args,
        scheduler_init_result.scheduler_infos,
        subprocess_watchdog, ...)                        # FastAPI + uvicorn
```

文档里明确点出了两条重要事实：① HTTP server、Engine、TokenizerManager 都在主进程；② 进程间通信走 ZMQ（每个进程用不同端口/IPC 名）。

#### 4.5.3 源码精读

[python/sglang/srt/entrypoints/http_server.py:2638-2684](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/entrypoints/http_server.py#L2638-L2684) —— `launch_server` 全文：先 `Engine._launch_subprocesses` 拉起引擎三件套，再 `_setup_and_run_http_server` 套上 HTTP 外壳。函数签名里 `init_tokenizer_manager_func` 等回调参数默认指向真实实现，便于测试注入。

重点读它的文档字符串，这是理解整个服务拓扑的最佳入口：

[python/sglang/srt/entrypoints/http_server.py:2646-2660](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/entrypoints/http_server.py#L2646-L2660) —— 文档说明：SRT 服务 = HTTP server + 引擎（TokenizerManager / Scheduler / DetokenizerManager）；HTTP、Engine、TokenizerManager 同处主进程，进程间用 ZMQ IPC 通信。这与 u2-l1 的多进程拓扑完全对应。

子进程拉起的实际调用：

[python/sglang/srt/entrypoints/http_server.py:2662-2673](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/entrypoints/http_server.py#L2662-L2673) —— `Engine._launch_subprocesses(...)` 返回五个对象：`tokenizer_manager`、`template_manager`、`port_args`（ZMQ 端口/IPC 名，对应 u2-l1 的三条 ZMQ 边）、`scheduler_init_result`（含各 scheduler 信息）、`subprocess_watchdog`（子进程看门狗）。

套上 HTTP 外壳：

[python/sglang/srt/entrypoints/http_server.py:2675-2684](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/entrypoints/http_server.py#L2675-L2684) —— `_setup_and_run_http_server(...)`：把 tokenizer_manager、scheduler_infos 等注入 FastAPI 应用并启动 uvicorn。监听地址来自 `server_args.url()`。

`server_args.url()` 负责把 host/port 拼成最终 URL，并对 `0.0.0.0` 等通配地址做回环处理（内部请求用 `127.0.0.1`）：

[python/sglang/srt/server_args.py:7397-7407](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/server_args.py#L7397-L7407) —— `url(port=None)`：按是否配置 `ssl_certfile` 选 `https`/`http`；当 host 是 `0.0.0.0`/`::` 时，内部请求改用回环地址，避免连接通配地址失败。

#### 4.5.4 代码实践

**实践目标**：把「`launch_server` 内部两步」与 u2-l1 的多进程拓扑对上号。

**操作步骤**：

1. 打开 `http_server.py` 的 `launch_server`（L2638-L2684），把 L2662-L2673 的返回值拆成五项，分别对应：哪个是 u2-l1 里的 TokenizerManager？哪个承载 ZMQ 的端口/IPC 名（即 `port_args`）？
2. 在 u2-l1 讲过的三条 ZMQ 边（`scheduler_input_ipc_name`、`detokenizer_ipc_name`、`tokenizer_ipc_name`）基础上，确认它们就来自 `port_args`（参考 `PortArgs` 数据类的字段定义，`server_args.py` L8278-L8299）。
3. 用 `--host 0.0.0.0 --port 30000` 启动服务（**待本地验证**），思考：外部客户端访问 `http://<本机IP>:30000`，而服务内部健康检查为何要用 `url()` 返回的 `http://127.0.0.1:30000`？

**需要观察的现象**：`launch_server` 只有两个顶层动作——拉子进程 + 起 HTTP。多进程拓扑的所有细节都被封装在 `Engine._launch_subprocesses` 里。

**预期结果**：你能把 `launch_server` 的返回值和 u2-l1 的进程/ZMQ 拓扑一一对应，确认本讲的「启动链路」最终落到的就是 u2-l1 描述的那个多进程系统。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `launch_server` 的参数里有 `init_tokenizer_manager_func`、`run_scheduler_process_func` 这样的回调，而不是直接调用真实函数？

**答案**：为了可测试性（依赖注入）。默认值就是真实实现，生产路径不受影响；但在单元测试里可以传入 mock 版本，从而在不真正启动子进程的前提下测试 `launch_server` 的编排逻辑。

**练习 2**：HTTP server、TokenizerManager、Scheduler、DetokenizerManager 这四个角色里，哪些在主进程、哪些在子进程？

**答案**：HTTP server、Engine、TokenizerManager 都在主进程；Scheduler 和 DetokenizerManager 是子进程（由 `Engine._launch_subprocesses` 派生）。它们之间通过 ZMQ IPC 通信，这正是 u2-l1 描述的环形拓扑。

---

## 5. 综合实践

把本讲学到的链路、配置和拓扑串起来，完成下面这个端到端的小任务：

**任务**：模拟一次「带自定义配置的启动」，并产出一份配置说明文档。

1. **画调用链**：在一张图上标出 `sglang serve` → `serve()` → `prepare_server_args()` → `run_server()` → `http_server.launch_server()` → `Engine._launch_subprocesses()` / `_setup_and_run_http_server()`，并在每个节点旁用一句话写它做了什么。
2. **挑配置**：为一个「2 卡 H100、要跑长上下文对话」的场景，从 `ServerArgs` 里挑选并解释你会显式设置哪些 CLI 参数（至少包括 `--tp`、`--mem-fraction-static`、`--chunked-prefill-size`、`--context-length` 中的一个），说明为什么。
3. **验证派生**：用 4.4.4 步骤 2 的 `dummy` 方式，传入你挑的参数构造 `ServerArgs` 对象，打印出来核对 CLI 覆盖是否生效（注意 `dummy` 会跳过 GPU 相关派生，所以重点核对 `tp_size`/`port` 等非派生字段）。
4. **对接拓扑**：在图上再画出 u2-l1 的 TokenizerManager → Scheduler → DetokenizerManager 环形拓扑，标出 `port_args` 提供的三条 ZMQ 边落在哪。

完成后，你应该能用一张图同时讲清「一条命令如何变成一个多进程服务」和「这个服务里每个进程的角色」。

> 说明：步骤 3 的运行输出依赖本地环境，相关结论请标注「待本地验证」；步骤 1、2、4 可纯靠源码阅读完成。

## 6. 本讲小结

- `sglang serve` 的语言模型链路是一条直线：`cli/serve.py:serve()` → `prepare_server_args()` → `run_server()` → `http_server.launch_server()`，`ServerArgs` 是贯穿全程的唯一配置对象。
- `run_server()` 是一个薄分发器，按 `encoder_only` / `smg_grpc_mode` / `use_ray` / 默认 HTTP 四种模式选择入口；普通服务走最后的 `else` 分支。
- `ServerArgs` 用 `A[T, Arg(...)]`（`A = typing.Annotated`）把数据类字段自动变成 CLI 参数，一处声明三处生效（字段、CLI、默认值）；`add_cli_args_from_dataclass` 是扫描引擎。
- `prepare_server_args()` 完成「CLI → Namespace → `ServerArgs`」的转换，并支持 `--config` YAML 合并；构造对象时自动触发 `__post_init__`。
- `__post_init__` 是一个有序分发器（一串 `self._handle_*`），负责补默认值和校验；`chunked_prefill_size`、`mem_fraction_static` 的真正默认值就是在这里按 GPU 显存动态算出来的。
- `http_server.launch_server()` 做两件事：`Engine._launch_subprocesses()` 拉起 TokenizerManager/Scheduler/Detokenizer 三进程并用 ZMQ 连成环，`_setup_and_run_http_server()` 套上 FastAPI + uvicorn 监听 `host:port`（默认 `127.0.0.1:30000`）。

## 7. 下一步学习建议

本讲讲清了「服务怎么被拉起来、配置怎么流转」，但还没有进入**单条请求在进程间的实际流转细节**。建议下一步：

- **紧接 u2-l3（请求端到端流转）**：跟踪一条 HTTP 请求从进入 TokenizerManager、被 Scheduler 调度执行、到 Detokenizer 流式返回的全过程，把本讲的「静态拓扑」变成「动态数据流」。
- **后续可读源码**：`python/sglang/srt/managers/tokenizer_manager.py`（请求接收与转发）、`python/sglang/srt/managers/detokenizer_manager.py`（增量解码与回写），它们正是 `launch_server` 拉起的两个核心进程的实现。
- **进阶方向**：当你需要自定义启动形态（Ray、PD 分离、编码服务）时，回头细读 `run_server` 的其他分支与 `ServerArgs` 里 `use_ray`、`encoder_only`、`disaggregation_*` 等字段，那将分别通往 U8（分布式）与 U9（PD 分离）。
