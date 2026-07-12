# 命令行工具体系 (CLI)

## 1. 本讲目标

本讲聚焦于「在终端敲下 `lmdeploy ...` 之后，到底发生了什么」。学完后你应当能够：

- 说清楚 `lmdeploy` 这条命令是如何被操作系统找到、又如何一步步分发到具体处理函数的。
- 看懂 `lmdeploy/cli/` 目录下 `entrypoint.py`、`cli.py`、`serve.py`、`lite.py` 之间的分工与调用关系。
- 理解 LMDeploy CLI 的核心设计模式：**「类体即注册 + `set_defaults(run=...)` 派发 + 延迟导入」**。
- 学会用 `--help` 自助查阅任意子命令（如 `lmdeploy lite auto_awq`）的全部参数。
- 能在源码中精确定位某个子命令的「注册行」和「处理函数」。

本讲不要求你真的有 GPU 或跑起模型，所有结论都可以从纯 Python 源码中验证。

## 2. 前置知识

阅读本讲前，你需要了解：

- **Python 包入口**：`python -m 包名` 会执行包内的 `__main__.py`；而 `pip` 安装时通过 `entry_points` 里的 `console_scripts` 注册的命令（如 `lmdeploy`）会指向某个 `模块:函数`。
- **`argparse` 基础**：`ArgumentParser` 解析命令行；`add_subparsers()` 创建一组子命令；`set_defaults(func=...)` 把一个可调用对象绑到解析结果上，便于后续派发。本讲的 CLI 几乎完全建立在 `argparse` 之上。
- **类属性在「类体执行时」创建**：Python 中，写在 `class X:` 体内、但不在方法里的赋值语句（如 `parser = ArgumentParser(...)`），是在**导入该模块、类体被执行的那一刻**就运行的。这是理解 LMDeploy CLI「为什么一 import 就把命令挂上去」的关键。
- **承接 u1-l4**：你已经知道 `pipeline()` 是面向用户的 Python 推理入口；本讲的 `chat`/`serve` 子命令，最终也是去调用 `pipeline` 或异步引擎来干活——CLI 只是它们的一层「命令行外壳」。

## 3. 本讲源码地图

本讲涉及的关键文件及其职责：

| 文件 | 职责 |
| --- | --- |
| `setup.py` | 在 `entry_points` 中注册 `lmdeploy = lmdeploy.cli:run`，把命令名钉到函数上。 |
| `lmdeploy/__main__.py` | 支持 `python -m lmdeploy`，转调 `cli.run`。 |
| `lmdeploy/cli/__init__.py` | 包入口，导出 `run`。 |
| `lmdeploy/cli/entrypoint.py` | CLI 总分发器 `run()`：装配所有子命令、解析参数、派发到处理函数。 |
| `lmdeploy/cli/cli.py` | `CLI` 基类：持有顶层 `parser`/`subparsers`，注册 `check_env`、`chat` 两个一级命令。 |
| `lmdeploy/cli/serve.py` | `SubCliServe`：注册 `serve` 一级命令及其 `api_server`、`proxy` 子命令。 |
| `lmdeploy/cli/lite.py` | `SubCliLite`：注册 `lite` 一级命令及其 `auto_awq`、`auto_gptq`、`calibrate`、`smooth_quant` 子命令。 |
| `lmdeploy/cli/utils.py` | 共享积木：`ArgumentHelper`（统一参数）、`convert_args`、`FlexibleArgumentParser`、`DefaultsAndTypesHelpFormatter`。 |
| `lmdeploy/cli/chat.py` | `chat` 命令真正的交互式实现（基于 `fire` + `pipeline`）。 |

> 注意：当前 HEAD 下，旧文档里常出现的 `lmdeploy convert` 命令**已不存在**于 CLI 注册表中（源码里仅在帮助文本中残留「converted by `lmdeploy convert` command」字样）。本讲只讲真实存在的命令。

## 4. 核心概念与源码讲解

本讲按四个最小模块展开：先讲「命令从哪来、到哪去」（4.1 入口与分发），再讲「CLI 基类与共享积木」（4.2），最后分别讲两类业务子命令（4.3 `SubCliServe`、4.4 `SubCliLite`）。

### 4.1 命令行入口与分发逻辑

#### 4.1.1 概念说明

当你在终端输入 `lmdeploy lite auto_awq ...` 回车后，操作系统并不认识 `lmdeploy`，它依靠 `pip` 安装时写入的 **console script**（一个生成的可执行入口）找到要调用的 Python 函数。LMDeploy 把这个入口指向了 `lmdeploy.cli:run`。

因此整个 CLI 的「主函数」就是 `entrypoint.py` 里的 `run()`。它要做三件事：

1. **装配**：把所有一级命令、二级命令挂到解析树上。
2. **解析**：用 `argparse` 把命令行字符串变成一个 `Namespace` 对象。
3. **派发**：根据解析结果，要么调用对应的处理函数，要么打印帮助。

理解这条链路，就理解了 LMDeploy 所有命令行的「骨架」。

#### 4.1.2 核心流程

下面是 `lmdeploy <args>` 从敲键到执行的完整流程（伪代码）：

```
终端: lmdeploy lite auto_awq /path/model --w-bits 4
 │
 ├─ console script (setup.py 注册)  →  lmdeploy.cli:run
 ├─ lmdeploy/cli/__init__.py        →  from .entrypoint import run
 └─ entrypoint.run():
     1) 【装配阶段】
        CLI.add_parsers()           → 挂 check_env, chat
        SubCliServe.add_parsers()   → 挂 api_server, proxy
        SubCliLite.add_parsers()    → 挂 auto_awq, auto_gptq, calibrate, smooth_quant
        （注意：'serve' 与 'lite' 这两个一级命令在「导入时」就已挂上，见 4.2）
     2) parser.parse_args()  → Namespace{command='lite', run=SubCliLite.auto_awq,
                                         model='/path/model', w_bits=4, ...}
     3) 回填 model_name：若 model_name 为空则用 model_path 代替
     4) 派发：
        if 'run' in dir(args):
            若 model_path 是远程仓库 id 且本地不存在 → get_model() 下载
            args.run(args)          # 实际执行 SubCliLite.auto_awq(args)
        else:
            按 args.command 打印对应层级的 --help
```

关键点：**`set_defaults(run=<处理函数>)` 是派发的契约**。每个子命令在注册时都会把自己的处理函数塞进 `run` 默认值；`entrypoint.run()` 只需统一判断「有没有 `run`」就能决定是执行还是打印帮助。

#### 4.1.3 源码精读

**① 命令名 → 函数的钉子（setup.py）**

[setup.py:191-192](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/setup.py#L191-L192) 在 `entry_points` 里把命令名 `lmdeploy` 绑定到 `lmdeploy.cli:run`：

```python
entry_points={'console_scripts': ['lmdeploy = lmdeploy.cli:run']},
```

这一行让 `pip install` 后系统里出现 `lmdeploy` 命令，调用它等价于调用 `lmdeploy.cli.run()`。

**② 支持 `python -m lmdeploy`（`__main__.py`）**

[__main__.py:1-5](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/__main__.py#L1-L5) 全文很短，转调 `run`，使两种调用方式等价：

```python
from .cli import run
if __name__ == '__main__':
    run()
```

**③ 包入口再转发一层（`cli/__init__.py`）**

[__init__.py:1-4](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/cli/__init__.py#L1-L4) 只导出 `run`，把真正的实现藏在 `entrypoint` 模块里：

```python
from .entrypoint import run
__all__ = ['run']
```

**④ 总分发器 `run()`（entrypoint.py）**

[entrypoint.py:10-17](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/cli/entrypoint.py#L10-L17) 完成装配与解析：

```python
def run():
    args = sys.argv[1:]
    CLI.add_parsers()
    SubCliServe.add_parsers()
    SubCliLite.add_parsers()
    parser = CLI.parser
    args = parser.parse_args()
```

注意 `args = sys.argv[1:]` 这一行其实只是「读取一下」，真正解析仍交给 `parser.parse_args()`（默认就会读 `sys.argv[1:]`）。

[entrypoint.py:25-39](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/cli/entrypoint.py#L25-L39) 是派发的「执行分支」，核心是模型自动下载与 `args.run(args)`：

```python
if 'run' in dir(args):
    from lmdeploy.utils import get_model
    model_path = getattr(args, 'model_path', None)
    ...
    if model_path is not None and not os.path.exists(args.model_path):
        args.model_path = get_model(args.model_path, ...)
    ...
    args.run(args)
```

也就是说：如果用户给的 `model_path` 在本地不存在，CLI 会把它当成 HuggingFace 仓库 id 自动下载，**再**交给处理函数。这让 `lmdeploy serve api_server internlm/internlm2_5-7b-chat` 这样的写法直接可用。

[entrypoint.py:40-50](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/cli/entrypoint.py#L40-L50) 是「帮助分支」——当用户只敲到一级命令（如 `lmdeploy serve`）或干脆没带子命令时，按 `command` 打印对应层级的帮助：

```python
else:
    try:
        args.print_help()
    except AttributeError:
        command = args.command
        if command == 'serve':
            SubCliServe.parser.print_help()
        elif command == 'lite':
            SubCliLite.parser.print_help()
        else:
            parser.print_help()
```

这条分支解释了为什么 `lmdeploy serve`（不带子命令）会打印 `serve` 的帮助、而裸 `lmdeploy` 会打印顶层帮助。

#### 4.1.4 代码实践

**实践目标**：亲眼确认「命令名 → 函数」的绑定，并追踪一次派发。

**操作步骤**：

1. 在仓库根目录打开 `setup.py`，找到 191–192 行的 `entry_points`，确认 `lmdeploy = lmdeploy.cli:run`。
2. 打开 `lmdeploy/__main__.py`，确认它只是转调 `run`。
3. 在 `entrypoint.py` 的 `run()` 内，用注释标出三段：装配（`add_parsers`）、解析（`parse_args`）、派发（`if 'run' in dir(args)`）。
4. （可选，待本地验证）若本机已安装 lmdeploy，执行 `python -m lmdeploy --help`，确认其输出与 `lmdeploy --help` 一致——这验证了 `__main__.py` 的转发。

**需要观察的现象**：`run()` 中没有任何 `if command == 'lite': ... elif command == 'serve': ...` 这种硬编码分支去调用业务函数；派发完全依赖 `args.run`。

**预期结果**：你会看到 LMDeploy 用「`set_defaults(run=...)` + 统一 `args.run(args)`」实现了开闭原则——新增子命令无需修改 `entrypoint.run()`。

#### 4.1.5 小练习与答案

**练习 1**：如果用户输入 `lmdeploy`（不带任何子命令），程序会进入 `run()` 的哪个分支？为什么？
**答案**：进入 `else`（帮助分支）。因为没指定子命令，`parser.parse_args()` 返回的 `Namespace` 不含 `run` 属性（没有任何子命令用 `set_defaults(run=...)` 设值），`'run' in dir(args)` 为假。

**练习 2**：`entrypoint.run()` 里同时存在 `args = sys.argv[1:]` 和 `args = parser.parse_args()`，第一个赋值会被覆盖，是否是 bug？
**答案**：不是 bug，但确实是冗余。`parser.parse_args()` 不传参时默认读取 `sys.argv[1:]`，所以第一行读取的 `args` 立即被第二行覆盖。它不影响行为，可视为遗留代码。

---

### 4.2 CLI 基类与子命令注册积木

#### 4.2.1 概念说明

`CLI` 类是整个命令树的「根」。它有两个与众不同之处：

1. **顶层 `parser` 与 `subparsers` 是「类属性」**——在类体执行（即模块导入）时就被创建。这意味着 `import` 一下，根解析器就存在了。
2. **一级命令分两批挂载**：`serve`/`lite` 在各自子模块（`serve.py`/`lite.py`）的**类体**里就挂到了 `CLI.subparsers`；而 `check_env`/`chat` 则要等到 `CLI.add_parsers()` 被显式调用时才挂。

这一节还要介绍贯穿所有子命令的「共享积木」：`ArgumentHelper`（统一参数定义）、`convert_args`（参数对象转字典）、`FlexibleArgumentParser`（更宽松的解析器）、`DefaultsAndTypesHelpFormatter`（让 `--help` 自动显示默认值与类型）。理解这些积木，你才能看懂后面 `serve`/`lite` 动辄几十个参数是如何被「拼」出来的。

#### 4.2.2 核心流程

```
导入 cli.py:
  class CLI:
      parser = FlexibleArgumentParser('lmdeploy')      # 根解析器(立即创建)
      parser.add_argument('-v','--version', ...)        # 顶层 -v 选项
      subparsers = parser.add_subparsers(dest='command')# 一级命令容器(立即创建)
  # 此时 CLI.subparsers 还是空的(serve/lite 由各自模块导入时挂入)

CLI.add_parsers()（在 run() 中调用）:
  ├─ add_parser_checkenv() → 挂 'check_env', set_defaults(run=CLI.check_env)
  └─ add_parser_chat()     → 挂 'chat',     set_defaults(run=CLI.chat)
```

每个 `add_parser_xxx` 都遵循同一套模板：

```
parser = CLI.subparsers.add_parser(名字, formatter_class=..., description=xxx.__doc__, help=xxx.__doc__)
parser.set_defaults(run=CLI.xxx)              # 派发契约
parser.add_argument(...)                       # 该命令专属位置/可选参数
ArgumentHelper.<公共参数>(parser)             # 复用统一参数
```

#### 4.2.3 源码精读

**① 根解析器与一级命令容器（类属性）**

[cli.py:15-20](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/cli/cli.py#L15-L20) 在 `CLI` 类体内直接创建 `parser` 与 `subparsers`：

```python
class CLI:
    _desc = 'The CLI provides a unified API for converting, ' \
            'compressing and deploying large language models.'
    parser = FlexibleArgumentParser(prog='lmdeploy', description=_desc, add_help=True)
    parser.add_argument('-v', '--version', action='version', version=__version__)
    subparsers = parser.add_subparsers(title='Commands', description='lmdeploy has following commands:', dest='command')
```

注意 `dest='command'`：解析后 `args.command` 会记录用户选了哪个一级命令（如 `'lite'`、`'serve'`、`None`），这正是 4.1 帮助分支判断的依据。

**② 装配方法 `add_parsers`**

[cli.py:176-180](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/cli/cli.py#L176-L180) 注册 `CLI` 自己名下的两个命令：

```python
@staticmethod
def add_parsers():
    CLI.add_parser_checkenv()
    CLI.add_parser_chat()
```

**③ `chat` 命令：注册 + 处理分离**

注册在 [cli.py:22-29](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/cli/cli.py#L22-L29)：

```python
@staticmethod
def add_parser_chat():
    parser = CLI.subparsers.add_parser('chat',
                                       formatter_class=DefaultsAndTypesHelpFormatter,
                                       description=CLI.chat.__doc__,
                                       help=CLI.chat.__doc__)
    parser.set_defaults(run=CLI.chat)
    parser.add_argument('model_path', type=str, help='The path of a model. ...')
```

处理函数在 [cli.py:164-174](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/cli/cli.py#L164-L174)，真正的交互逻辑被**延迟导入**到 `lmdeploy/cli/chat.py`：

```python
@staticmethod
def chat(args):
    from .chat import main
    kwargs = convert_args(args)
    ...
    main(**kwargs)
```

> 小细节：`CLI.chat` 没有 docstring，故 `CLI.chat.__doc__` 为 `None`，因此 `lmdeploy --help` 的命令列表里 `chat` 这一项**不会显示帮助文字**（仅显示命令名）。这是源码可直接验证的事实。

**④ 共享积木之一：`ArgumentHelper`**

[utils.py:104-117](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/cli/utils.py#L104-L117) 定义了一堆静态方法，每个方法向 `parser`「贴一个统一规格的参数」：

```python
class ArgumentHelper:
    """Helper class to add unified argument."""

    @staticmethod
    def model_name(parser):
        return parser.add_argument('--model-name', type=str, default=None,
                                   help='The name of the served model. ...')
```

它的价值在于**复用与一致性**：例如 `--tp`、`--dtype`、`--cache-max-entry-count` 等参数在 `chat`、`serve api_server` 中都要出现，靠 `ArgumentHelper.tp(parser)` 一行就能以完全相同的默认值和帮助文本挂上去。`utils.py` 里有数十个这样的方法（`tp/dp/ep/dtype/session_len/cache_max_entry_count/calib_dataset/...`）。

**⑤ 共享积木之二：`convert_args`**

[utils.py:31-35](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/cli/utils.py#L31-L35) 把 `argparse.Namespace` 转成普通字典，并剔除 `run`、`command` 两个派发用字段：

```python
def convert_args(args):
    special_names = ['run', 'command']
    kwargs = {k[0]: k[1] for k in args._get_kwargs() if k[0] not in special_names}
    return kwargs
```

这样处理函数就能用 `auto_awq(**kwargs)` 的形式把参数原样转发给底层 API——这就是「CLI 参数」与「Python API 参数」名字能一一对应的根本原因（注意：CLI 的 `--w-bits` 经 argparse 自动转成属性名 `w_bits`）。

**⑥ 共享积木之三：`DefaultsAndTypesHelpFormatter`**

[utils.py:11-28](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/cli/utils.py#L11-L28) 自定义帮助格式化器，会在每条参数帮助后自动追加 `Default: ...` 与 `Type: ...`。这就是为什么你看到的 `lmdeploy lite auto_awq --help` 每个参数都自带默认值和类型。

**⑦ 共享积木之四：`FlexibleArgumentParser`**

[utils.py:829-915](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/cli/utils.py#L829-L915) 继承 `argparse.ArgumentParser`，重写 `parse_args`，提供两项「宽容」能力：

- **下划线/连字符互通**：`--w_bits` 与 `--w-bits` 都能识别（把 `--xxx_yyy` 自动改成 `--xxx-yyy`）。
- **点号嵌套字典**：支持 `--a.b.c 1` 这种写法，合并成 `{"a": {"b": {"c": 1}}}` 后以 JSON 形式注入，常用于 `--hf-overrides` 之类的嵌套配置。

#### 4.2.4 代码实践

**实践目标**：体会「类属性在导入时创建」这一机制，并验证 `ArgumentHelper` 的复用。

**操作步骤**：

1. 读 [cli.py:15-20](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/cli/cli.py#L15-L20)，确认 `parser`、`subparsers` 写在类体内（不在任何方法里）。
2. 在 `utils.py` 中找到 `ArgumentHelper.tp`（约 169 行附近）与 `ArgumentHelper.dtype`（约 119 行附近），记录它们的默认值（`tp` 默认 1，`dtype` 默认 `'auto'`）。
3. 用搜索（`Grep`）在整个 `lmdeploy/cli/` 内统计 `ArgumentHelper.tp(` 出现的次数——你会看到它在 `chat`、`serve api_server` 中都被调用，印证「一份定义、多处复用」。

**需要观察的现象**：`tp` 的默认值与帮助文本只在 `utils.py` 定义一次；改这一处，所有用到它的子命令行为同步变化。

**预期结果**：你能向他人解释「为什么 LMDeploy 这么多参数却很少出现默认值不一致」——因为它们共享 `ArgumentHelper`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `serve` 和 `lite` 两个一级命令不需要在 `CLI.add_parsers()` 里显式挂载？
**答案**：因为它们在各自模块（`serve.py`、`lite.py`）的**类体**里就执行了 `parser = CLI.subparsers.add_parser('serve'/'lite', ...)`。只要 `entrypoint.py` 顶部 `from .serve import SubCliServe` / `from .lite import SubCliLite` 触发了导入，这两个命令就被挂上了。`add_parsers()` 只负责挂二级命令。

**练习 2**：用户在命令行写 `--cache_max_entry_count 0.5`（下划线）能被正确解析吗？
**答案**：能。`FlexibleArgumentParser` 会把首个 `--` 开头参数中的下划线替换为连字符，故 `--cache_max_entry_count` 等价于 `--cache-max-entry-count`。

---

### 4.3 SubCliServe：服务部署子命令

#### 4.3.1 概念说明

`SubCliServe` 负责把模型「服务化」——对外提供 OpenAI 兼容的 HTTP 接口。它注册了一级命令 `serve`，下辖两个子命令：

- `serve api_server`：启动一个 FastAPI 服务，提供 chat/completion 等接口（最常用）。
- `serve proxy`：启动一个代理/路由服务，用于多副本、多机、甚至 Prefill-Decode 分离（disaggregation）部署。

`api_server` 是整个 CLI 里参数最多的命令——因为它要把 PyTorch 引擎、TurboMind 引擎、视觉模型、投机解码等所有可调项都暴露出来。看懂它，等于把 u2（引擎配置）的绝大多数字段在命令行上又见了一遍。

#### 4.3.2 核心流程

```
SubCliServe（导入时）:
  parser = CLI.subparsers.add_parser('serve', help='Serve LLMs with openai API')
  subparsers = parser.add_subparsers()         # 二级命令容器

SubCliServe.add_parsers()（在 run() 中调用）:
  ├─ add_parser_api_server()  → 'api_server', set_defaults(run=api_server)
  └─ add_parser_proxy()       → 'proxy',      set_defaults(run=proxy)

运行 serve api_server 时:
  args.run(args) = SubCliServe.api_server(args):
    1) 依 args.backend 决定后端（pytorch / 自动 autoget_backend）
    2) 构造 PytorchEngineConfig 或 TurbomindEngineConfig
    3) 构造 chat_template_config / vision_config / speculative_config
    4) dp==1 或 turbomind → run_api_server(...)（单进程）
       否则               → launch_server(...)（多卡/多进程编排）
```

#### 4.3.3 源码精读

**① 一级命令 `serve` 的挂载（类体）**

[serve.py:16-25](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/cli/serve.py#L16-L25)：

```python
class SubCliServe:
    """Serve LLMs and interact on terminal."""
    _help = 'Serve LLMs with openai API'
    parser = CLI.subparsers.add_parser('serve', help=_help, description=_desc)
    subparsers = parser.add_subparsers(title='Commands', ...)
```

**② `api_server` 参数与「共享 action」技巧**

[serve.py:27-34](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/cli/serve.py#L27-L34) 是注册头，与 4.2 的模板一致。最值得学习的是它如何让同一个参数（如 `--tp`、`--dtype`）**同时出现在「PyTorch engine arguments」和「TurboMind engine arguments」两个帮助分组里**。

先在 PyTorch 分组里创建并接收返回的 action 对象（[serve.py:119-125](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/cli/serve.py#L119-L125)）：

```python
dtype_act = ArgumentHelper.dtype(pt_group)
tp_act = ArgumentHelper.tp(pt_group)
session_len_act = ArgumentHelper.session_len(pt_group)
...
```

再把同一个 action 对象追加到 TurboMind 分组（[serve.py:144-161](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/cli/serve.py#L144-L161)）：

```python
tb_group = parser.add_argument_group('TurboMind engine arguments')
tb_group._group_actions.append(dtype_act)
tb_group._group_actions.append(tp_act)
...
```

这是一个**仅显示用途**的技巧：物理上参数只定义一次（避免重复注册报错），但通过共享同一个 `_group_actions` 条目，让 `--help` 把它同时列在两个引擎分组下，方便用户按引擎查阅。其余 PyTorch 专属参数（`--eager-mode`、`--kernel-block-size` 等）与 TurboMind 专属参数（`--cp`、`--rope-scaling-factor` 等）则分别只在各自分组注册。

**③ `api_server` 处理函数：从参数到引擎配置**

[serve.py:218-228](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/cli/serve.py#L218-L228) 是入口，决定后端：

```python
@staticmethod
def api_server(args):
    from lmdeploy.archs import autoget_backend
    max_batch_size = args.max_batch_size if args.max_batch_size else get_max_batch_size(args.device)
    backend = args.backend
    if backend != 'pytorch':
        backend = autoget_backend(args.model_path, trust_remote_code=args.trust_remote_code)
```

注意：`--backend` 默认是 `turbomind`（见 `ArgumentHelper.backend`），但它并不直接强制 TurboMind——只要不是显式 `pytorch`，就会走 `autoget_backend` 自动判定（承接 u1-l4 讲过的自动后端选择）。随后按后端构造对应的 `EngineConfig`（[serve.py:229-291](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/cli/serve.py#L229-L291)），把几十个 CLI 参数一一填入数据类。

最后依据 `dp` 选择单进程还是多进程启动（[serve.py:297-360](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/cli/serve.py#L297-L360)）：`dp==1` 或 TurboMind 走 `run_api_server`，否则走 `launch_server` 做多卡编排。

**④ 装配方法**

[serve.py:369-372](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/cli/serve.py#L369-L372)：

```python
@staticmethod
def add_parsers():
    SubCliServe.add_parser_api_server()
    SubCliServe.add_parser_proxy()
```

#### 4.3.4 代码实践

**实践目标**：看懂 `serve api_server` 的参数分组结构，并定位「自动后端选择」。

**操作步骤**：

1. 读 [serve.py:101-168](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/cli/serve.py#L101-L168)，把参数分成三类记笔记：①两引擎共享（`dtype/tp/session_len/...`）；②PyTorch 专属（`eager_mode/kernel_block_size/...`）；③TurboMind 专属（`cp/rope_scaling_factor/...`）。
2. 在 [serve.py:225-227](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/cli/serve.py#L225-L227) 处确认：只有 `--backend pytorch` 才会强制 PyTorch，其余都进 `autoget_backend`。
3. （待本地验证）在有 GPU 与模型的机器上执行 `lmdeploy serve api_server --help`，对照你画的分组表，核对帮助里的分组标题是否一致。

**需要观察的现象**：`--help` 中 `dtype/tp` 等参数会**重复出现**在「PyTorch engine arguments」和「TurboMind engine arguments」两个分组里，但 argparse 并没有报「参数冲突」错误。

**预期结果**：你能解释这是因为「同一个 action 对象被两个分组共享」，而非参数被定义了两次。

#### 4.3.5 小练习与答案

**练习 1**：`lmdeploy serve api_server <model>` 默认会用哪个后端？能保证一定是 TurboMind 吗？
**答案**：`--backend` 默认值是 `turbomind`，但处理函数里只要 `backend != 'pytorch'` 就会调用 `autoget_backend` 重新判定。所以**不能**保证一定是 TurboMind——若该模型 TurboMind 不支持，会自动落到 PyTorch。想强制 PyTorch 需显式加 `--backend pytorch`。

**练习 2**：为什么 `dtype/tp` 等参数能在两个分组里显示，却不会触发 argparse 的「重复参数」错误？
**答案**：因为它们只通过 `ArgumentHelper.dtype(pt_group)` 调用了一次 `add_argument`（物理上只有一个 action）；TurboMind 分组是通过 `tb_group._group_actions.append(dtype_act)` 复用了同一个 action 对象，仅影响 `--help` 的归类显示，并未真正注册第二次。

---

### 4.4 SubCliLite：量化压缩子命令（含 auto_awq 注册精读）

#### 4.4.1 概念说明

`SubCliLite` 是「模型压缩」入口，对应 `lmdeploy/lite` 子包（会在 U7 单元深入）。它注册了一级命令 `lite`，下辖四个子命令，分别对应四种压缩/校准流程：

| 子命令 | 作用 | 底层 API |
| --- | --- | --- |
| `auto_awq` | 用 AWQ 算法做权重量化（4bit） | `lmdeploy.lite.apis.auto_awq.auto_awq` |
| `auto_gptq` | 用 GPTQ 算法做权重量化 | `lmdeploy.lite.apis.gptq.auto_gptq` |
| `calibrate` | 只做校准、收集激活统计（不写出量化权重） | `lmdeploy.lite.apis.calibrate.calibrate` |
| `smooth_quant` | 用 SmoothQuant 做 w8a8（权激活同时量化） | `lmdeploy.lite.apis.smooth_quant.smooth_quant` |

本模块以 `auto_awq` 为代表，精读一个子命令从「注册」到「派发」的完整代码路径——这也是本讲 `practice_task` 要求定位的目标。

#### 4.4.2 核心流程

```
SubCliLite（导入时）:
  parser = CLI.subparsers.add_parser('lite', help='Compressing and accelerating LLMs ...')
  subparsers = parser.add_subparsers()

SubCliLite.add_parsers()（在 run() 中调用）:
  ├─ add_parser_auto_awq()      → 'auto_awq',     set_defaults(run=auto_awq)
  ├─ add_parser_auto_gptq()     → 'auto_gptq',    set_defaults(run=auto_gptq)
  ├─ add_parser_calibrate()     → 'calibrate',    set_defaults(run=calibrate)
  └─ add_parser_smooth_quant()  → 'smooth_quant', set_defaults(run=smooth_quant)

运行 lmdeploy lite auto_awq <model> 时:
  args.run(args) = SubCliLite.auto_awq(args):
    from lmdeploy.lite.apis.auto_awq import auto_awq   # 延迟导入(避免启动时加载 torch 等)
    kwargs = convert_args(args)                         # Namespace → dict
    auto_awq(**kwargs)                                  # 转发到底层 Python API
```

四个处理函数都遵循「**延迟导入 + convert_args + 转发**」的同一套模板。

#### 4.4.3 源码精读

**① 一级命令 `lite` 的挂载（类体）**

[lite.py:6-15](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/cli/lite.py#L6-L15)：

```python
class SubCliLite:
    """CLI for compressing LLMs."""
    _help = 'Compressing and accelerating LLMs with lmdeploy.lite module'
    parser = CLI.subparsers.add_parser('lite', help=_help, description=_desc)
    subparsers = parser.add_subparsers(title='Commands', ...)
```

**② `auto_awq` 的注册（本讲 practice_task 的目标行）**

[lite.py:17-42](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/cli/lite.py#L17-L42) 是 `auto_awq` 子命令的注册函数。其中**真正「挂上」`auto_awq` 这名字的一行在第 20 行**，**绑定处理函数的一行在第 24 行**：

```python
@staticmethod
def add_parser_auto_awq():
    """Add parser for auto_awq command."""
    parser = SubCliLite.subparsers.add_parser('auto_awq',                          # 第20行：注册子命令
                                              formatter_class=DefaultsAndTypesHelpFormatter,
                                              description=SubCliLite.auto_awq.__doc__,
                                              help=SubCliLite.auto_awq.__doc__)
    parser.set_defaults(run=SubCliLite.auto_awq)                                    # 第24行：派发契约
    parser.add_argument('model', type=str, help='The path of model in hf format')
    ArgumentHelper.revision(parser)
    ArgumentHelper.download_dir(parser)
    ArgumentHelper.work_dir(parser)
    ArgumentHelper.calib_dataset(parser)
    ArgumentHelper.calib_samples(parser)
    ArgumentHelper.calib_seqlen(parser)
    ArgumentHelper.calib_batchsize(parser)
    ArgumentHelper.calib_search_scale(parser)
    ArgumentHelper.dtype(parser)
    ArgumentHelper.trust_remote_code(parser)
    parser.add_argument('--device', type=str, default='cuda', help='Device for weight quantization (cuda or npu)')
    parser.add_argument('--w-bits', type=int, default=4, help='Bit number for weight quantization')
    parser.add_argument('--w-sym', action='store_true', help='Whether to do symmetric quantization')
    parser.add_argument('--w-group-size', type=int, default=128, help='Group size for weight quantization statistics')
```

参数清单（供你在 `--help` 中对照）：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `model`（位置参数） | — | HF 格式模型路径 |
| `--calib-dataset` | `wikitext2` | 校准数据集（可选 c4/pileval/gsm8k 等） |
| `--calib-samples` | `128` | 校准样本数；`0` 表示 data-free |
| `--calib-seqlen` | `2048` | 校准序列长度 |
| `--batch-size` | `1` | 校准批大小（显存小则调小） |
| `--search-scale` | `False` | 是否搜索平滑比例 |
| `--dtype` | `auto` | 权重精度（auto/float16/bfloat16） |
| `--device` | `cuda` | 量化设备（cuda/npu） |
| `--w-bits` | `4` | 权重量化比特数 |
| `--w-sym` | `False` | 是否对称量化 |
| `--w-group-size` | `128` | 权重量化分组大小 |

**③ `auto_awq` 处理函数（延迟导入 + 转发）**

[lite.py:111-116](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/cli/lite.py#L111-L116)：

```python
@staticmethod
def auto_awq(args):
    """Perform weight quantization using AWQ algorithm."""
    from lmdeploy.lite.apis.auto_awq import auto_awq
    kwargs = convert_args(args)
    auto_awq(**kwargs)
```

`from ... import auto_awq` 写在函数体内而非文件顶部，是刻意为之：量化依赖 `torch`、`transformers` 等重型库，延迟到真正执行量化时才导入，能让 `lmdeploy --help`、`lmdeploy lite --help` 这类轻量查询**几乎瞬间返回**，而不必先加载整套训练栈。`convert_args` 把 `Namespace` 转成字典后，参数名与底层 `auto_awq(...)` 的形参一一对应，直接 `**kwargs` 转发。

**④ 装配方法**

[lite.py:138-144](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/cli/lite.py#L138-L144) 把四个子命令一口气挂上：

```python
@staticmethod
def add_parsers():
    SubCliLite.add_parser_auto_awq()
    SubCliLite.add_parser_auto_gptq()
    SubCliLite.add_parser_calibrate()
    SubCliLite.add_parser_smooth_quant()
```

#### 4.4.4 代码实践（本讲 practice_task）

**实践目标**：自助查阅 `auto_awq` 的全部参数，并精确定位其注册代码行。

**操作步骤**：

1. **定位注册行**：打开 `lmdeploy/cli/lite.py`，找到 `add_parser_auto_awq` 方法（17 行起）。确认：
   - 第 **20** 行：`parser = SubCliLite.subparsers.add_parser('auto_awq', ...)`——这是 `auto_awq` 子命令被注册的一行。
   - 第 **24** 行：`parser.set_defaults(run=SubCliLite.auto_awq)`——这是把它和处理函数绑死的一行。
   - 第 **141** 行：在 `add_parsers()` 里被调用，真正「激活」注册。
2. **查阅参数（待本地验证）**：在已安装 lmdeploy 的环境中执行：

   ```bash
   lmdeploy lite auto_awq --help
   ```

   对照上面「参数清单」表格，逐条核对帮助文本。由于使用了 `DefaultsAndTypesHelpFormatter`，每条参数后还应自动附带 `Default: ...` 与 `Type: ...`。
3. **验证下划线/连字符互通（待本地验证）**：分别用 `--w-bits 4` 与 `--w_bits 4`（仅查阅帮助层面无差异；若真跑，两者等效，得益于 `FlexibleArgumentParser`）。

**需要观察的现象**：`--help` 输出的参数与你从源码 `add_parser_auto_awq` 中读到的完全一致——`ArgumentHelper.*` 贴上的公共参数与 `parser.add_argument` 贴上的专属参数都会出现。

**预期结果**：你能在不看 `--help` 的情况下，仅凭 [lite.py:17-42](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/cli/lite.py#L17-L42) 准确复述 `auto_awq` 的全部参数与默认值；并能指出注册行是第 20 行、绑定行是第 24 行。

> 说明：本实践的运行部分（`--help`）需在本地具备 lmdeploy 运行环境时执行；若仅做源码阅读，步骤 1 的「定位注册行」在当前仓库即可完成并验证。

#### 4.4.5 小练习与答案

**练习 1**：`SubCliLite.auto_awq` 处理函数里，`from lmdeploy.lite.apis.auto_awq import auto_awq` 为什么不写在文件顶部？
**答案**：为了延迟导入（lazy import）。量化 API 依赖 `torch` 等重型库，放函数体内可以确保只有在真正执行量化时才加载它们，从而让 `--help` 等轻量操作快速返回，降低 CLI 启动开销。

**练习 2**：CLI 参数 `--w-group-size` 是如何变成能传给底层 `auto_awq(w_group_size=...)` 的关键字的？
**答案**：argparse 会把 `--w-group-size` 自动映射为属性名 `w_group_size`（连字符转下划线）；`convert_args` 再把 `Namespace` 转成字典 `{'w_group_size': ..., ...}`，于是 `auto_awq(**kwargs)` 能正确匹配底层函数的形参 `w_group_size`。

**练习 3**：如果要新增一个 `lmdeploy lite my_quant` 子命令，最少要改哪几处？
**答案**：①在 `SubCliLite` 里加 `add_parser_my_quant()`（按模板：`add_parser` + `set_defaults(run=...)` + 参数）；②加 `my_quant(args)` 处理函数（延迟导入 + `convert_args` + 转发）；③在 `add_parsers()` 里追加一行 `SubCliLite.add_parser_my_quant()`。**无需改动 `entrypoint.run()`**——这正是这套派发模式的好处。

## 5. 综合实践

把本讲四个模块串起来，做一次「全链路追踪」：

**任务**：选择命令 `lmdeploy lite auto_awq /models/qwen --w-bits 4 --calib-samples 64`，画出从「回车」到「进入 AWQ 量化算法」的完整调用链，并标注每一步对应的源码位置。

**要求产出的调用链（参考答案）**：

```
[1] 终端 lmdeploy ...
      ↓ console script
[2] setup.py: lmdeploy = lmdeploy.cli:run
      ↓
[3] lmdeploy/cli/__init__.py: from .entrypoint import run
      ↓
[4] entrypoint.run()  (entrypoint.py:10)
      ├─ 导入阶段: serve.py/lite.py 类体把 'serve'/'lite' 挂入 CLI.subparsers
      ├─ CLI.add_parsers() → 挂 check_env/chat            (cli.py:176)
      ├─ SubCliServe.add_parsers() → 挂 api_server/proxy   (serve.py:369)
      ├─ SubCliLite.add_parsers()  → 挂 auto_awq/...       (lite.py:138)
      ├─ parser.parse_args() → Namespace{command='lite', run=SubCliLite.auto_awq, ...}
      └─ 'run' in dir(args) → True → args.run(args)        (entrypoint.py:39)
      ↓
[5] SubCliLite.auto_awq(args)  (lite.py:111)
      ├─ from lmdeploy.lite.apis.auto_awq import auto_awq  (延迟导入)
      ├─ convert_args(args) → dict                         (utils.py:31)
      └─ auto_awq(**kwargs) → 进入真正的 AWQ 量化流程(U7 讲)
```

**附加任务**：

1. 在上图基础上，把 `serve api_server` 的调用链也画一遍，标出「`autoget_backend` → 选 `PytorchEngineConfig`/`TurbomindEngineConfig` → `run_api_server`/`launch_server`」这一段（参考 4.3）。
2. 用一句话解释：为什么这套设计能在不改 `entrypoint.run()` 的前提下无限增加新子命令？

## 6. 本讲小结

- `lmdeploy` 命令由 `setup.py` 的 `console_scripts` 绑到 `lmdeploy.cli:run`，`__main__.py` 让 `python -m lmdeploy` 等效。
- 总分发器 `entrypoint.run()` 三步走：**装配 → 解析 → 派发**；派发完全依赖 `set_defaults(run=...)` 契约，统一用 `args.run(args)` 执行。
- 一级命令分两批挂载：`serve`/`lite` 在各自模块**类体（导入时）**挂入；`check_env`/`chat` 由 `CLI.add_parsers()` 显式挂入；二级命令统一由各 `SubCli.add_parsers()` 挂入。
- `ArgumentHelper` 是共享参数词汇表，`convert_args` 让 CLI 参数与底层 Python API 参数名一一对应，`FlexibleArgumentParser` 允许下划线/连字符互通与点号嵌套，`DefaultsAndTypesHelpFormatter` 让 `--help` 自带默认值与类型。
- `serve api_server` 用「共享 action」技巧让 `tp/dtype` 等参数在 PyTorch 与 TurboMind 两个帮助分组同时显示；`lite` 下四个子命令都遵循「延迟导入 + convert_args + 转发」模板。
- `auto_awq` 的注册行在 [lite.py:20](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/cli/lite.py#L20)，绑定处理函数在 [lite.py:24](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/cli/lite.py#L24)，在 [lite.py:141](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/cli/lite.py#L141) 被激活。

## 7. 下一步学习建议

- **向「数据类型」深入**：本讲反复提到 `PytorchEngineConfig`、`TurbomindEngineConfig`、`GenerationConfig`。下一单元 U2 会逐一拆解这些贯穿全项目的核心数据类型（建议先读 u2-l1 `messages.py`）。
- **向「服务」深入**：想真正跑通 `lmdeploy serve api_server`，进入 U8（serve 服务部署），从 `serve/openai/api_server.py` 的 FastAPI 入口读起。
- **向「量化」深入**：`SubCliLite` 只是命令行外壳，真正的 AWQ/GPTQ/SmoothQuant 算法在 `lmdeploy/lite/apis/` 与 `lmdeploy/lite/quantization/`，U7 会完整讲解。
- **动手验证**：在本机装好 lmdeploy 后，把本讲所有标注「待本地验证」的 `--help` 命令跑一遍，对照源码表格核对，印象会深刻得多。
