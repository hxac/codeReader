# CLI 总入口与子命令分发

## 1. 本讲目标

在入门层的第一单元里，你已经知道 MLC LLM 的完整工作流是 `convert_weight → gen_config → compile → serve/chat`。本讲要回答一个更底层的问题：**当你在终端敲下 `mlc_llm compile ...` 时，这一行命令究竟是怎么进入代码、又怎么被送到正确的处理函数里的？**

学完本讲，你应当能够：

- 说清 `mlc_llm` 这个 shell 命令是怎么被「注册」出来的（`console_scripts` 机制）。
- 读懂 [`__main__.py`](#) 里那段「解析一个子命令名 → 懒加载对应模块 → 转交控制权」的分发逻辑。
- 区分 `cli/`（命令行入口层）与 `interface/`（Python 接口层）的职责边界，理解为什么要把这两层分开。
- 跟踪一条完整的调用链：`__main__.py` → `cli/compile.py` → `interface/compile.py`。

本讲是 **u2 CLI 单元**的入口，承接 [u1-l4 端到端工作流](u1-l4-workflow-and-artifacts.md) 中提到的四步工作流，为后续 [u2-l2 编译三件套](u2-l2-compile-trio-commands.md) 和 [u2-l3 运行入口](u2-l3-run-commands.md) 打基础。

## 2. 前置知识

本讲只涉及 Python 层的入口与分发，不需要懂编译器或推理引擎内部。但有几个概念需要先建立直觉。

### 2.1 什么是「入口点（entry point）」

你在终端敲的 `mlc_llm`，本身并不是一个可执行程序，而是一个由 Python 安装器（pip）生成的小小的 **包装脚本（wrapper script）**。它的内容大致是：找到 Python 解释器，然后调用某个 Python 函数。这个「某个函数」由包的作者在 `setup.py` / `pyproject.toml` 里通过 `console_scripts` 声明。

> 小知识：在 Unix 上，pip 会在 `bin/` 下生成一个无后缀的可执行脚本；在 Windows 上则生成 `.exe`。无论哪种平台，最终都是「跑 Python、调函数」。

### 2.2 `__main__.py` 与 `python -m`

Python 包里如果有一个名为 `__main__.py` 的文件，那么 `python -m <包名>` 就会执行它。所以 MLC LLM 有两种等价的启动方式：

```bash
mlc_llm compile ...        # 走 console_scripts 包装脚本
python -m mlc_llm compile ...  # 走 __main__.py
```

两者最终都进入同一个 `main()` 函数。

### 2.3 命令行参数与 `sys.argv`

当你在终端输入 `mlc_llm compile model --opt O2`，Python 看到的 `sys.argv` 大致是：

```python
sys.argv = ["mlc_llm", "compile", "model", "--opt", "O2"]
#            [0]        [1]       [2]      [3]      [4]
```

`sys.argv[0]` 是程序名，`sys.argv[1]` 是子命令名 `compile`，从 `sys.argv[2:]` 开始才是子命令自己的参数。记住这个切片方式，它是本讲分发机制的关键。

### 2.4 前置讲义承接

[u1-l2](u1-l2-repository-structure.md) 已经指出：`python/` 同时承载编译器、CLI 与引擎封装；[u1-l4](u1-l4-workflow-and-artifacts.md) 给出了四步工作流。本讲就站在工作流的「入口」处，看命令是如何被路由到工作流各步对应的代码里的。

## 3. 本讲源码地图

本讲涉及的关键文件都在 `python/` 下：

| 文件 | 行数（约） | 作用 |
| --- | --- | --- |
| [`python/mlc_llm/__main__.py`](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/__main__.py) | 68 | **CLI 总入口**：解析子命令名，懒加载并转交给 `cli/` 下对应模块。 |
| [`python/setup.py`](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/setup.py) | 142 | 打包脚本，其中 `console_scripts` 把 `mlc_llm` 命令注册到 `__main__:main`。 |
| [`python/mlc_llm/__init__.py`](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/__init__.py) | 21 | 包初始化；额外注册了一个 disco 多进程 worker 入口。 |
| [`python/mlc_llm/cli/compile.py`](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/cli/compile.py) | 152 | `compile` 子命令的 **CLI 层**：解析参数、校验、调用接口层。 |
| [`python/mlc_llm/interface/compile.py`](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/compile.py) | — | `compile` 的 **接口层**：真正的编译实现（`CompileArgs` + `compile()`）。 |
| [`python/mlc_llm/support/argparse.py`](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/support/argparse.py) | 17 | 一个定制版 `ArgumentParser`，出错时打印更友好的用法提示。 |

> 说明：`cli/` 与 `interface/` 目录下还有不少文件，本讲只以 `compile` 为例串通调用链，其余子命令结构类似。

## 4. 核心概念与源码讲解

本讲的三个最小模块：

1. **入口点注册**：`mlc_llm` 这个命令是怎么来的。
2. **子命令分发机制**：`__main__.py` 如何把控制权交给正确的子命令。
3. **`cli/` 与 `interface/` 分层**：为什么命令行逻辑和真正实现要分两层。

---

### 4.1 入口点注册：`mlc_llm` 命令是怎么来的

#### 4.1.1 概念说明

「入口点注册」要解决的问题是：用户在终端敲 `mlc_llm`，系统怎么知道该去调哪个 Python 函数？

答案在打包阶段就写好了。作者在 [`setup.py`](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/setup.py) 里通过 `entry_points` 字段声明一条 **console_scripts** 规则：

> 把名为 `mlc_llm` 的命令，绑定到 `mlc_llm.__main__` 模块里的 `main` 函数。

`pip install` 时，pip 读取这条规则，在 `bin/`（或 Windows 的 `Scripts\`）下生成一个包装脚本。之后你敲 `mlc_llm`，本质就是「启动 Python 解释器 → `from mlc_llm.__main__ import main; main()`」。

#### 4.1.2 核心流程

```text
┌─────────────────┐     pip install      ┌──────────────────────────┐
│  setup.py 中的  │ ───────────────────▶ │ bin/mlc_llm (包装脚本)   │
│  console_scripts│   生成包装脚本        │  → 调 __main__.main()    │
└─────────────────┘                       └──────────────────────────┘
                                                     │
                                                     ▼
                                          ┌──────────────────────────┐
                                          │ python -m mlc_llm ...    │
                                          │   也进入同一个 main()     │
                                          └──────────────────────────┘
```

注册规则是「命令名 = 模块:函数」三段式：`mlc_llm = mlc_llm.__main__:main`。

#### 4.1.3 源码精读

在 [`setup.py` 的 `main()` 里](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/setup.py#L99-L124)，调用了 `setuptools.setup(...)`，其中关键字段是：

[python/setup.py:L117-L119](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/setup.py#L117-L119) —— 这一行就是 `mlc_llm` 命令的「出生证明」：

```python
entry_points={
    "console_scripts": ["mlc_llm = mlc_llm.__main__:main"],
},
```

读法：在 `console_scripts` 组下注册一个条目，命令名叫 `mlc_llm`，它指向 `mlc_llm.__main__` 模块的 `main` 函数。

值得注意的还有两点：

- [setup.py:L75-L84](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/setup.py#L75-L84) 定义了 `BinaryDistribution`，并把 `distclass=BinaryDistribution` 传给 `setup()`。这告诉 setuptools 这是一个**含二进制扩展**（C++ 编译产物 `libmlc_llm.so`）的包，打包时要把 `.so` 一起带上。这正是 [u1-l3](u1-l3-install-and-quickstart.md) 提到「验证安装要找到 `libmlc_llm.so`」的源头。
- `__main__.py` 文件底部 [__main__.py:L66-L67](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/__main__.py#L66-L67) 有：

```python
if __name__ == "__main__":
    main()
```

正是这一句让 `python -m mlc_llm` 也能进入同一个 `main()`。

> 旁支：还有一个「不走 `__main__` 的入口」。[`__init__.py:L13-L20`](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/__init__.py#L13-L20) 里用 `@register_global_func` 注册了一个多 GPU（disco）的 worker 入口，指向 `mlc_llm.cli.worker`。也就是说 `cli/worker.py` 不是由 `__main__.py` 分发的终端命令，而是被分布式运行时当成**子进程入口**拉起来的。这解释了为什么 `cli/` 目录下的文件比 `__main__.py` 里的 8 个选项要多。

#### 4.1.4 代码实践

**实践目标**：验证 `mlc_llm` 命令与 `python -m mlc_llm` 走的是同一个 `main()`。

**操作步骤**：

1. 找到包装脚本的位置：
   ```bash
   which mlc_llm
   ```
2. 查看包装脚本内容（它是纯文本）：
   ```bash
   cat "$(which mlc_llm)"
   ```
3. 用模块方式启动，确认同样能进入 CLI：
   ```bash
   python -m mlc_llm --help
   ```

**需要观察的现象**：

- `which mlc_llm` 应返回一个 `bin/mlc_llm` 路径。
- `cat` 出来的脚本里应能看到类似 `from mlc_llm.__main__ import main` 与 `main()` 的字样。
- `python -m mlc_llm --help` 与 `mlc_llm --help` 的输出应当**完全一致**。

**预期结果**：两种方式都打印出包含 8 个子命令（compile / convert_weight / gen_config / chat / serve / package / calibrate / router）的用法说明。若你的环境未安装该包，可改为直接阅读 `__main__.py` 推断（待本地验证实际输出）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `setup.py` 里的 `console_scripts` 行删掉，重新 `pip install`，会发生什么？

**参考答案**：`bin/` 下不再生成 `mlc_llm` 包装脚本，终端敲 `mlc_llm` 会报「command not found」；但 `python -m mlc_llm` 仍可用，因为那依赖 `__main__.py` 而非 `console_scripts`。

**练习 2**：`console_scripts` 条目 `mlc_llm = mlc_llm.__main__:main` 中，冒号前后分别代表什么？

**参考答案**：冒号前是**模块路径**（`mlc_llm.__main__`），冒号后是该模块内的**可调用对象名**（`main` 函数）。

---

### 4.2 子命令分发机制

#### 4.2.1 概念说明

`mlc_llm` 不是一个只做一件事的命令，而是一个**命令家族**的「总入口」：`compile`、`convert_weight`、`gen_config`、`chat`、`serve`、`package`、`calibrate`、`router` 共 8 个子命令。每个子命令都有自己的一套参数、自己的帮助文档、自己的处理逻辑。

分发机制要解决的问题是：**总入口只看一眼第一个参数（子命令名），然后立刻把剩下的参数整体交给对应的子命令处理。** 这就像公司前台：只负责把访客引到正确的部门，不参与部门内部业务。

#### 4.2.2 核心流程

分发分为三步：

1. **只解析子命令名**：用 `argparse` 解析 `sys.argv[1:2]`（仅一个 token），限定它必须是 8 个选项之一。
2. **懒加载对应模块**：根据子命令名，在 `if/elif` 分支里 `from mlc_llm.cli import <名字> as cli`。
3. **转交控制权**：调用 `cli.main(sys.argv[2:])`，把剩余参数原封不动地交给子命令。

```text
sys.argv = ["mlc_llm", "compile", "model", "--opt", "O2"]
                 │         │
                 │         └─ argv[1:2] ─▶ 解析出 subcommand="compile"
                 │
                 └─ argv[2:] ─────────────▶ 交给 cli/compile.py 的 main(argv)
                                              ["model", "--opt", "O2"]
```

#### 4.2.3 源码精读

整个分发逻辑就在 [`__main__.py` 的 `main()` 函数](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/__main__.py#L11-L63) 里，非常短。

**第一步：建一个解析器，只接受子命令名。**

[__main__.py:L13-L29](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/__main__.py#L13-L29) —— 这里把 8 个子命令写死成 `choices`：

```python
parser = ArgumentParser("MLC LLM Command Line Interface.")
parser.add_argument(
    "subcommand",
    type=str,
    choices=[
        "compile", "convert_weight", "gen_config",
        "chat", "serve", "package", "calibrate", "router",
    ],
    help="Subcommand to to run. (choices: %(choices)s)",
)
parsed = parser.parse_args(sys.argv[1:2])
```

两个要点：

- `sys.argv[1:2]` 是一个**长度为 1 的切片**，只把子命令名喂给解析器，剩余参数一个都不动。
- `choices=[...]` 让 argparse 自动做校验：如果用户敲了一个不在列表里的名字，argparse 会直接报错并列出合法选项。
- 注意这里用的是 `mlc_llm.support.argparse.ArgumentParser`，而不是标准库的 `argparse.ArgumentParser`。这个定制版（见 [support/argparse.py:L7-L16](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/support/argparse.py#L7-L16)）重写了 `error()`，出错时打印更友好的「Usage / Error」分栏提示。

**第二步 & 第三步：懒加载 + 转交。**

[__main__.py:L30-L63](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/__main__.py#L30-L63) —— 一长串 `if/elif`，每个分支长得几乎一样。以 `compile` 为例：

```python
if parsed.subcommand == "compile":
    from mlc_llm.cli import compile as cli

    cli.main(sys.argv[2:])
elif parsed.subcommand == "convert_weight":
    from mlc_llm.cli import convert_weight as cli

    cli.main(sys.argv[2:])
# ... 其余同理 ...
```

这里有一个**关键设计**：`from mlc_llm.cli import compile as cli` 这句 import 写在 `if` 分支**内部**，而不是文件顶部。这叫**懒加载（lazy import）**。它带来的好处非常实在：

- `compile` 子命令会拖入整个 TVM、模型注册表 `MODELS`、量化注册表 `QUANTIZATION` 等一大堆重型依赖（见 [cli/compile.py:L16-L24](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/cli/compile.py#L16-L24) 的 import）。
- 如果放在文件顶部，那么用户敲 `mlc_llm chat --help` 时也会被迫加载编译相关的全部依赖，白白浪费几秒。
- 放在分支内部后，**只有真正用到某个子命令时才加载它**，`mlc_llm --help` 因此又快又轻。

最后 [`else` 分支](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/__main__.py#L62-L63) 抛 `ValueError`。实际上由于 `choices` 已经做了校验，这行在正常路径下不可达，属于防御性编程。

> 为什么不用 argparse 自带的 `add_subparsers`？因为 subparsers 会在构造阶段就把所有子命令的解析器都建出来，从而触发各子命令模块的 import，**破坏了懒加载**。这里手写两段式解析，正是为了保住懒加载的性能优势。

#### 4.2.4 代码实践

**实践目标**：观察分发机制的实际行为，理解「两段式解析」。

**操作步骤**：

1. 列出全部子命令并查看顶层帮助：
   ```bash
   mlc_llm --help
   ```
2. 故意敲一个不存在的子命令，观察 `choices` 校验：
   ```bash
   mlc_llm nonexistent_cmd
   ```
3. 查看某个具体子命令的帮助（注意这是**第二段解析器**打印的，不是 `__main__.py`）：
   ```bash
   mlc_llm compile --help
   ```

**需要观察的现象**：

- 第 1 步应看到 `subcommand` 参数下列出全部 8 个选项（由 `%(choices)s` 渲染）。
- 第 2 步应被 argparse 拦下，提示「invalid choice」并列出合法选项，进程以非零码退出。
- 第 3 步打印的是 `compile` 自己的参数（`model`、`--quantization`、`--opt`、`--output` 等），说明控制权已经交到 `cli/compile.py` 手里。

**预期结果**：三步输出分别对应「总入口解析器」「校验失败」「子命令解析器」。若环境未安装，可对照 [__main__.py:L13-L29](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/__main__.py#L13-L29) 与 [cli/compile.py:L56-L124](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/cli/compile.py#L56-L124) 推断（待本地验证实际输出）。

#### 4.2.5 小练习与答案

**练习 1**：`parser.parse_args(sys.argv[1:2])` 为什么传的是 `[1:2]` 而不是 `[1:]`？

**参考答案**：因为总入口只关心「子命令名」这一个 token。`[1:2]` 切出长度为 1 的列表（只含子命令名），把后续参数留给子命令自己的解析器处理；若传 `[1:]`，argparse 会因为不认识 `--opt` 之类的参数而报错。

**练习 2**：把 `from mlc_llm.cli import compile as cli` 移到文件顶部，会对 `mlc_llm chat --help` 产生什么影响？

**参考答案**：即使只想用 `chat`，也会在启动时被迫加载 `compile` 模块及其重型依赖（TVM、模型/量化注册表），导致启动变慢、内存占用变高。这正是当前代码坚持懒加载的原因。

**练习 3**：用户敲 `mlc_llm foo`（`foo` 不在 choices 里），代码会走到 `__main__.py` 末尾的 `raise ValueError(...)` 吗？

**参考答案**：不会。`choices=[...]` 会让 `parse_args` 在解析阶段就直接报错退出（退出码 2），根本到不了后面的 `if/elif`，更到不了 `else`。`raise ValueError` 是防御性兜底。

---

### 4.3 `cli/` 与 `interface/` 分层

#### 4.3.1 概念说明

注意看 `python/mlc_llm/` 下有两个名字几乎一一对应的目录：

| 子命令 | CLI 层文件 | 接口层文件 |
| --- | --- | --- |
| compile | `cli/compile.py` | `interface/compile.py` |
| convert_weight | `cli/convert_weight.py` | `interface/convert_weight.py` |
| gen_config | `cli/gen_config.py` | `interface/gen_config.py` |
| chat | `cli/chat.py` | `interface/chat.py` |
| serve | `cli/serve.py` | `interface/serve.py` |
| package | `cli/package.py` | `interface/package.py` |
| calibrate | `cli/calibrate.py` | `interface/calibrate.py` |
| router | `cli/router.py` | `interface/router.py` |

这不是巧合，而是刻意的**两层架构**：

- **`cli/` 层 —— 命令行入口（Command line entrypoint）**：只负责「和命令行打交道」。它解析 argv、校验路径、把字符串参数转换成 Python 对象（如 `Path`、`Quantization` 枚举、`Target`），然后调用接口层。它**不包含**真正的业务逻辑。
- **`interface/` 层 —— Python 接口（Python entrypoint）**：真正的实现。它接收的不再是 argv 字符串，而是结构化的 Python 对象；它完成实际的编译/转换/服务等工作。

为什么要分两层？为了让**同一套实现既能被命令行调用，也能被 Python 代码直接调用**。如果你想在脚本或测试里编译模型，不必去模拟命令行，直接 `from mlc_llm.interface.compile import compile` 调函数即可。`cli/` 只是 `interface/` 的一层「argv 翻译」外壳。

> 文件开头的 docstring 直接点明了这种分工：`cli/compile.py` 第一行是 *"Command line entrypoint of compilation."*，而 `interface/compile.py` 第一行是 *"Python entrypoint of compilation."*（见 [interface/compile.py:L1](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/compile.py#L1)）。

#### 4.3.2 核心流程

以 `compile` 为例，完整的调用链是：

```text
mlc_llm compile model --opt O2 -o out.so
        │
        ▼
__main__.py: main()
   解析 argv[1:2] → subcommand="compile"
   懒加载 cli.compile，调用 cli.main(argv[2:])
        │
        ▼
cli/compile.py: main(argv)
   用 ArgumentParser 解析 argv → 得到 parsed（含 model, opt, output, ...）
   做 detect_* 自动探测（target、model_type、quantization、system_lib_prefix）
   读取 mlc-chat-config.json
   调用 interface.compile(...)
        │
        ▼
interface/compile.py: compile(config, quantization, model_type, target, opt, ...)
   真正的编译实现：构建 IRModule → 跑 pass 流水线 → build_func 导出 model lib
```

注意每一层「翻译」的内容：

- `__main__` 翻译：`argv` → 子命令名。
- `cli` 翻译：`argv` → 结构化参数对象（含自动探测后的 `target`、`model_type`、`quantization`）。
- `interface` 执行：结构化参数 → 编译产物。

#### 4.3.3 源码精读

**CLI 层：`cli/compile.py:main(argv)`**

[cli/compile.py:L27-L152](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/cli/compile.py#L27-L152) 是 `compile` 子命令的 CLI 层。它做三件事：

1. **建解析器、定义参数**（节选）：

[cli/compile.py:L56-L124](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/cli/compile.py#L56-L124) —— 把 `model`、`--quantization`、`--model-type`、`--device`、`--opt`、`--output` 等参数逐一声明出来。注意几个细节：
   - `--quantization` 的 `choices=list(QUANTIZATION.keys())`，直接把量化注册表 [u5-l1 会讲](u5-l1-quantization-registry.md) 的所有合法量化名当成可选项。
   - `--model-type` 默认 `"auto"`，`--device` 默认 `"auto"`，`--system-lib-prefix` 默认 `"auto"`。这些 `auto` 会在下一步被「自动探测」替换成真实值。

2. **解析 + 自动探测**：

[cli/compile.py:L124-L139](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/cli/compile.py#L124-L139) —— 这是 CLI 层的「增值工作」：把用户给的 `auto` 翻译成具体值：

```python
parsed = parser.parse_args(argv)
target, build_func = detect_target_and_host(parsed.device, parsed.host, ...)
parsed.model_type = detect_model_type(parsed.model_type, parsed.model)
parsed.quantization = detect_quantization(parsed.quantization, parsed.model)
parsed.system_lib_prefix = detect_system_lib_prefix(...)
with open(parsed.model, encoding="utf-8") as config_file:
    config = json.load(config_file)
```

这些 `detect_*` 函数来自 [`support/auto_config.py`](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/support/auto_config.py) 与 [`support/auto_target.py`](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/support/auto_target.py)，它们会读取模型目录推断出 model_type、量化方式、target 等（[u3-l3](u3-l3-model-config-preset.md) 会展开）。CLI 层在这里把「字符串/`auto`」转成了「Python 对象」。

3. **调用接口层**：

[cli/compile.py:L141-L152](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/cli/compile.py#L141-L152) —— 把探测好的结构化对象整体喂给接口层的 `compile` 函数：

```python
compile(
    config=config,
    quantization=parsed.quantization,
    model_type=parsed.model_type,
    target=target,
    opt=parsed.opt,
    build_func=build_func,
    system_lib_prefix=parsed.system_lib_prefix,
    output=parsed.output,
    overrides=parsed.overrides,
    debug_dump=parsed.debug_dump,
)
```

注意 `import` 在文件顶部 [cli/compile.py:L10-L14](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/cli/compile.py#L10-L14)：

```python
from mlc_llm.interface.compile import (
    ModelConfigOverride,
    OptimizationFlags,
    compile,
)
```

CLI 层从接口层「拿」到的，正是接口层对外暴露的 `compile` 函数和两个参数类型（`ModelConfigOverride`、`OptimizationFlags`，定义在 [`interface/compiler_flags.py`](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/compiler_flags.py)）。

**接口层：`interface/compile.py`**

进入接口层后，一切参数都已经是 Python 对象，不再有 argv 字符串。接口层用一个 dataclass 把所有参数收敛到一起：

[interface/compile.py:L27-L43](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/compile.py#L27-L43) —— `CompileArgs` 定义了编译所需的全部结构化参数：

```python
@dataclasses.dataclass
class CompileArgs:
    config: Path
    quantization: Quantization
    model: Model
    target: Target
    opt: OptimizationFlags
    build_func: Callable[[IRModule, "CompileArgs", Pass], None]
    system_lib_prefix: str
    output: Path
    overrides: ModelConfigOverride
    debug_dump: Optional[Path]
```

看，这里的类型是 `Quantization`、`Model`、`Target`、`OptimizationFlags`——全是结构化对象，正是 CLI 层 `detect_*` 翻译后的产物。`compile()` 函数（同文件下方）会消费这些参数，真正去构建 IRModule、跑 pass 流水线、导出模型库——那是 [u7-l1](u7-l1-compile-interface.md) 的主题，本讲不展开。

**分层带来的直接好处**：你完全可以绕过命令行，在 Python 里直接调用接口层：

```python
# 示例代码：直接调用接口层，跳过 CLI（仅为说明分层，非项目内置脚本）
from mlc_llm.interface.compile import compile, OptimizationFlags
# compile(config=..., quantization=..., model_type=..., target=..., ...)
```

这样测试、Notebook、上层框架都能复用同一套实现，而不必 `subprocess` 去跑命令行。

#### 4.3.4 代码实践

**实践目标**：动手跟踪 `compile` 子命令从总入口到接口层的完整调用链，画出调用图。

**操作步骤**：

1. 打开 [`__main__.py`](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/__main__.py#L30-L33)，找到 `compile` 分支，确认它调用 `cli.main(sys.argv[2:])`。
2. 打开 [`cli/compile.py`](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/cli/compile.py#L27-L152)，定位三处关键代码：
   - 参数定义（`parser.add_argument(...)`）。
   - `detect_*` 自动探测块。
   - 末尾对 `compile(...)` 的调用。
3. 打开 [`interface/compile.py`](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/compile.py#L27-L43)，看 `CompileArgs` 的字段类型，体会「到了这一层已经全是 Python 对象」。
4. 在 `cli/compile.py` 的 `compile(...)` 调用前（约第 141 行）临时加一行日志（**仅本地调试，勿提交**）：
   ```python
   print(f"[trace] cli→interface: model_type={parsed.model_type.name}, "
         f"quantization={parsed.quantization.name}, target={target.export()}")
   ```
   然后跑一次 `mlc_llm compile ...`（需要真实模型，待本地验证）。

**需要观察的现象**：

- 三层文件的函数签名逐层「具象化」：`main()` 无参（读 sys.argv）→ `main(argv: list[str])`（字符串列表）→ `compile(config=dict, quantization=Quantization, ...)`（结构化对象）。
- 加日志后，能看到 CLI 层探测出的 `model_type`、`quantization`、`target` 被原样传进接口层。

**预期结果**：你应当能得到一张与本节「核心流程」里一致的调用链图。如果手头没有可编译的小模型，可仅完成步骤 1–3 的「源码阅读型跟踪」，并在图中标注每层的输入/输出类型。

#### 4.3.5 小练习与答案

**练习 1**：`cli/compile.py` 从 `interface/compile.py` 导入了哪三样东西？为什么要这样组织？

**参考答案**：导入了 `compile`（实现函数）、`OptimizationFlags` 和 `ModelConfigOverride`（两个参数类型）。这样组织是因为 CLI 层需要用这两个类型来解析 `--opt` 与 `--overrides` 字符串，再把结果连同 `compile` 一起用——接口层既是「实现来源」也是「类型来源」，保证两层契约一致。

**练习 2**：假设你想在 Jupyter Notebook 里编译模型，应该调用 `cli/compile.py` 还是 `interface/compile.py`？为什么？

**参考答案**：应该调用 `interface/compile.py` 的 `compile()`。因为 `cli/compile.py` 的 `main(argv)` 期望的是命令行字符串列表，在 Notebook 里模拟 argv 很别扭；而接口层接收结构化 Python 对象，更适合编程式调用。这正是两层分离的意义。

**练习 3**：`cli/compile.py` 里 `parsed.model_type`、`parsed.quantization` 一开始可能是字符串 `"auto"`，它们后来是怎么变成 `Model` / `Quantization` 对象的？

**参考答案**：通过 `detect_model_type(...)` 和 `detect_quantization(...)` 这两个自动探测函数（[cli/compile.py:L130-L131](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/cli/compile.py#L130-L131)）。它们读取模型目录下的配置（如 `mlc-chat-config.json`、`config.json`），把 `"auto"` 或字符串名解析成注册表里的 `Model` / `Quantization` 对象。这正是 CLI 层「翻译」职责的体现。

---

## 5. 综合实践

**任务**：完成一次「端到端调用链追踪」，把本讲三个模块串起来。

**背景**：你所在团队新来了一位同事，他不明白 `mlc_llm compile ...` 这条命令背后到底经历了哪些代码。请你产出一份「调用链说明文档」。

**操作步骤**：

1. **注册层**：在 [`setup.py`](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/setup.py#L117-L119) 中找到 `mlc_llm` 命令的注册行，说明 `mlc_llm = mlc_llm.__main__:main` 的含义。
2. **分发层**：在 [`__main__.py`](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/__main__.py#L11-L63) 中，指出 `sys.argv[1:2]` 的作用、`choices` 列表的 8 个子命令，以及懒加载 `from mlc_llm.cli import compile as cli` 的位置与意义。
3. **CLI 层**：在 [`cli/compile.py`](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/cli/compile.py#L27-L152) 中，圈出「参数定义」「自动探测」「调用接口层」三段，并解释为什么把 `detect_*` 放在 CLI 层而不是接口层。
4. **接口层**：在 [`interface/compile.py`](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/compile.py#L27-L43) 中，读 `CompileArgs` 字段类型，说明这些类型与 CLI 层 `detect_*` 的产物如何对应。
5. **画图**：用你顺手的工具（纸笔、Mermaid、draw.io）画出下面这张调用链图，并在每个箭头上标注「传递的数据类型」：

```text
shell: mlc_llm compile model --opt O2 -o out.so
   │  (argv 字符串列表)
   ▼
__main__.main()  ──[argv[1:2]="compile"]──▶  cli.compile.main(argv[2:])
   │  (argv[2:]: ["model","--opt","O2",...])
   ▼
cli/compile.py: main(argv)
   │  parse_args + detect_*  →  结构化对象
   ▼
interface/compile.py: compile(config, quantization, model_type, target, opt, ...)
   │  (CompileArgs: Path / Quantization / Model / Target / ...)
   ▼
构建 IRModule → pass 流水线 → build_func → model lib（产物）
```

**验收标准**：

- 能用一句话说清 `console_scripts`、两段式解析、懒加载、`cli/interface` 分层这四件事。
- 调用链图里每个箭头都标注了正确的数据类型（字符串 → 结构化对象）。
- 能指出 `cli/` 目录下哪些文件**不是**由 `__main__.py` 分发的（如 `worker.py`，它由 disco 以子进程方式拉起，见 [`__init__.py:L13-L20`](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/__init__.py#L13-L20)）。

> 若没有可运行环境，本实践可完全作为「源码阅读型实践」完成：所有结论都能从 cited 的源码行号直接读出。

## 6. 本讲小结

- `mlc_llm` 这个 shell 命令由 [`setup.py` 的 `console_scripts`](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/setup.py#L117-L119) 注册，绑定到 `mlc_llm.__main__:main`；`python -m mlc_llm` 也进入同一个 `main()`。
- [`__main__.py`](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/__main__.py#L11-L63) 用「两段式解析」：先用 `sys.argv[1:2]` 解析子命令名（8 选 1），再把 `sys.argv[2:]` 整体交给对应子命令。
- 子命令到模块的映射靠 `if/elif` + **懒加载**（`from mlc_llm.cli import <名> as cli`），保证 `--help` 和无关子命令不被重型依赖（TVM/模型注册表）拖慢。
- `cli/` 是「命令行入口层」，只做 argv 解析、路径校验与 `detect_*` 自动探测；`interface/` 是「Python 接口层」，含真正的实现。两者职责清晰、一一对应。
- 同一套 `interface` 实现既能被 CLI 调用，也能被 Python 代码/测试直接 import，无需模拟命令行。
- `cli/` 下有些文件（如 `worker.py`）并非 `__main__.py` 分发的终端命令，而是被分布式运行时（disco）当子进程入口拉起。

## 7. 下一步学习建议

- **下一步讲义**：进入 [u2-l2 模型编译三件套](u2-l2-compile-trio-commands.md)，逐个精读 `convert_weight`、`gen_config`、`compile` 三个子命令的 CLI 层参数与用法；随后 [u2-l3 运行入口](u2-l3-run-commands.md) 覆盖 `chat` / `serve` / `package`。
- **横向延伸**：本讲只跟踪到 `interface/compile.py` 的门口。想看接口层内部如何构建 IRModule 并跑 pass 流水线，可跳到 [u7-l1 compile 接口与编译主流程](u7-l1-compile-interface.md)。
- **建议阅读的源码**：用本讲的「跟踪法」自行跟踪另外两条链——`mlc_llm chat`（`__main__` → `cli/chat.py` → `interface/chat.py`）与 `mlc_llm serve`（`__main__` → `cli/serve.py` → `interface/serve.py`），验证两层架构是否在每个子命令上都成立。
