# 进程入口与启动方式

## 1. 本讲目标

本讲解决一个问题：**LMCache 到底有哪几个进程？每个进程从哪里启动、各吃什么参数？**

学完后你应该能够：

1. 把 `pyproject.toml` 里注册的三个 console script（`lmcache` / `lmcache_server` / `lmcache_controller`）分别追踪到具体的 `main()` 函数。
2. 说清楚 `lmcache` 这个 CLI 是如何「自动发现」子命令的（不需要手工维护一张命令表）。
3. 区分两种后台进程：原始 socket server（`v1/server`）与 MP coordinator（`v1/mp_coordinator`），理解它们在协议、配置来源、角色上的差别。
4. 看懂 `python -m lmcache.v1.mp_coordinator` 这条命令背后发生了什么。

## 2. 前置知识

在进入正文前，先建立三个直觉。

### 2.1 什么是「入口（entry point）」

一个 Python 包可以暴露多种「被启动」的方式：

- **console script（命令行脚本）**：安装时由 `pip` 在 `bin/` 下生成一个可执行文件，敲 `lmcache` 就等于敲 `python -m 某个模块`。它由 `pyproject.toml` 的 `[project.scripts]` 声明，每条形如 `名字="模块路径:函数名"`。
- **module 入口**：直接用 `python -m 包.模块` 启动，Python 会执行该模块里的 `if __name__ == "__main__":` 分支。
- **daemon（守护进程）**：一个长期常驻、对外提供服务的进程。本讲的 server 和 coordinator 都属于 daemon。

> 术语约定：本讲里「CLI」特指带子命令的诊断工具 `lmcache`；「server」在不同语境下指代不同的东西，遇到时我会明确说明是 `v1/server`（原始 socket 服务）还是别的。

### 2.2 三个进程，三种性格

LMCache 不是一个单一进程，而是一组可以独立启动的程序。本讲聚焦其中三个最具代表性的：

| 进程 | 性格 | 一句话定位 |
| --- | --- | --- |
| `lmcache`（CLI） | 短命、一次性 | 命令行工具，跑完即退（如 `lmcache ping`） |
| `lmcache_server`（socket server） | 长期常驻 | 一台「KV 仓库」，用自定义二进制协议在裸 TCP 上读写 |
| mp coordinator | 长期常驻 | 多实例部署里的「指挥官」，用标准 HTTP（FastAPI）协调 |

### 2.3 配置从哪里来

后台进程最关键的问题是「配置从哪里读」。你会看到两种风格：

- **命令行位置参数**：socket server 直接读 `sys.argv` 的第 1、2、3 个参数（host、port、device）。
- **环境变量**：coordinator 完全从 `LMCACHE_MP_COORDINATOR_*` 环境变量读配置。

这两种风格后面会反复对比。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [pyproject.toml](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/pyproject.toml) | 声明三个 console script，是追踪入口的「总目录」 |
| [lmcache/cli/main.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/cli/main.py) | `lmcache` CLI 的根入口，负责建 parser、分发子命令 |
| [lmcache/cli/commands/\_\_init\_\_.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/cli/commands/__init__.py) | 用「子类自动发现」收集所有子命令，得到 `ALL_COMMANDS` |
| [lmcache/cli/commands/base.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/cli/commands/base.py) | `BaseCommand` 抽象基类，定义每个子命令必须实现的契约 |
| [lmcache/v1/server/\_\_main\_\_.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/server/__main__.py) | socket server 的 `LMCacheServer` 与启动函数 |
| [lmcache/v1/mp_coordinator/\_\_main\_\_.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/__main__.py) | coordinator 的启动函数，从环境变量构建配置后用 uvicorn 起服务 |
| [lmcache/v1/mp_coordinator/config.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/config.py) | `MPCoordinatorConfig`，定义 coordinator 的所有可调字段与 `from_env()` |
| [lmcache/banner.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/banner.py) | CLI 启动时打印一次的 banner（与入口相关） |

---

## 4. 核心概念与源码讲解

### 4.1 入口总览：从 pyproject.toml 追踪到 main()

#### 4.1.1 概念说明

`pyproject.toml` 的 `[project.scripts]` 段是「命令名 → Python 函数」的总目录。`pip install` 时，setuptools 会据此在安装环境的 `bin/` 下生成同名可执行文件。所以「追踪入口」的第一步，永远是先读这一段。

#### 4.1.2 核心流程

1. 打开 `pyproject.toml`，定位 `[project.scripts]`。
2. 对每一行 `名字="模块:函数"`，把 `模块` 当作 import 路径，`函数` 当作入口函数。
3. 用 `名字 --help` 验证：它应当与「`python -c "from 模块 import 函数; 函数()"`」等价（参数透传）。

#### 4.1.3 源码精读

LMCache 在 [pyproject.toml:45-48](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/pyproject.toml#L45-L48) 注册了**三个** console script：

```toml
[project.scripts]
lmcache="lmcache.cli.main:main"
lmcache_server="lmcache.v1.server.__main__:main"
lmcache_controller="lmcache.v1.api_server.__main__:main"
```

逐行翻译：

- `lmcache` → `lmcache.cli.main:main`：本讲的 CLI（4.2 节）。
- `lmcache_server` → `lmcache.v1.server.__main__:main`：原始 socket server（4.3 节）。
- `lmcache_controller` → `lmcache.v1.api_server.__main__:main`：基于 FastAPI 的 **cache-controller** HTTP 服务（注意它指向 `v1/api_server`，是另一个进程，本讲不深入）。

> ⚠️ **容易踩的坑**：第三个脚本叫 `lmcache_controller`，但它指向 `v1/api_server`，**不是**本讲的 mp coordinator。coordinator 没有被注册成 console script，它的启动方式见 4.4 节。同样要小心：`lmcache server`（带空格的 CLI 子命令）和 `lmcache_server`（带下划线的 console script）是**两个完全不同的进程**——前者启动新的多进程 http_server，后者启动本讲的原始 socket server。

把映射关系画成一张图：

```
[project.scripts]                     运行命令                入口模块
lmcache            ───────────────►   lmcache ...          lmcache/cli/main.py:main
lmcache_server     ───────────────►   lmcache_server ...   lmcache/v1/server/__main__.py:main
lmcache_controller ───────────────►   lmcache_controller   lmcache/v1/api_server/__main__.py:main
（无 console script）─────────────►   python -m lmcache.v1.mp_coordinator
                                                          lmcache/v1/mp_coordinator/__main__.py:main
```

#### 4.1.4 代码实践

1. **目标**：亲手验证「命令名 = Python 函数」的等价关系。
2. **步骤**：
   - 在安装好 LMCache 的环境里运行 `which lmcache`，确认它指向 `bin/lmcache`。
   - 运行 `lmcache --help`。
   - 对照 [pyproject.toml:46](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/pyproject.toml#L46)，确认它等价于执行 `lmcache.cli.main` 里的 `main()`。
3. **观察现象**：`--help` 会列出一组子命令（如 `ping`、`describe`、`coordinator`、`server` 等）。
4. **预期结果**：帮助文本里的 `prog` 显示为 `lmcache`，与 [lmcache/cli/main.py:24](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/cli/main.py#L24) 的 `prog="lmcache"` 一致。
5. **待本地验证**：具体子命令列表取决于你安装的版本与可用可选依赖。

#### 4.1.5 小练习与答案

**练习 1**：如果你新增一行 `mytool="lmcache.cli.main:main"`，敲 `mytool` 会发生什么？
**答案**：pip 会在 `bin/` 下生成 `mytool`，效果与 `lmcache` 完全一致——命令名只是别名，真正的行为由「模块:函数」决定。

**练习 2**：`lmcache_controller` 指向哪个模块？它和 mp coordinator 是同一个进程吗？
**答案**：指向 `lmcache.v1.api_server.__main__:main`（FastAPI cache-controller）。它**不是** mp coordinator，coordinator 需要用 `python -m lmcache.v1.mp_coordinator` 或 `lmcache coordinator` 启动。

---

### 4.2 CLI 入口 cli/main.py 与子命令自动发现

#### 4.2.1 概念说明

`lmcache` 是一个「带子命令」的 CLI，类似 `git`（`git commit`、`git pull`）。它的设计目标是**新增子命令时不用改 `main.py`**：把一个继承 `BaseCommand` 的类放进 `lmcache/cli/commands/`，下次 `lmcache --help` 就自动多出一条命令。这就是「子命令自动发现」。

#### 4.2.2 核心流程

`lmcache <subcommand> [args]` 的执行过程：

```
print_banner_once(stderr)              # 打印一次 banner
build ArgumentParser(prog="lmcache")
ALL_COMMANDS = _discover_commands()    # 扫描 cli/commands/ 收集所有 BaseCommand 子类
for cmd in ALL_COMMANDS:
    cmd.register(subparsers)           # 每个命令挂一个子 parser，绑定 func=execute
args = parser.parse_args()
if no func attr:  print_help(); exit(1)
try: args.func(args)                   # 分发到具体命令的 execute()
except KeyboardInterrupt: exit(130)
except Exception: log; exit(1)
```

其中「自动发现」由两步接力完成：

1. [lmcache/cli/commands/\_\_init\_\_.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/cli/commands/__init__.py) 调用 `discover_subclasses()` 扫描本包的所有子模块，收集 `BaseCommand` 的具体子类，实例化后得到 `ALL_COMMANDS`。
2. [lmcache/cli/main.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/cli/main.py) 遍历 `ALL_COMMANDS`，调用每个命令的 `register()` 挂到 argparse 上。

#### 4.2.3 源码精读

CLI 的根入口在 [lmcache/cli/main.py:20-44](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/cli/main.py#L20-L44)：

```python
def main() -> None:
    print_banner_once(sys.stderr)                       # banner，只打一次
    parser = argparse.ArgumentParser(prog="lmcache", ...)
    subparsers = parser.add_subparsers(dest="command")
    for cmd in ALL_COMMANDS:                            # 自动发现的命令列表
        cmd.register(subparsers)
    args = parser.parse_args()
    if not hasattr(args, "func"):                       # 没给子命令 → 打印帮助
        parser.print_help()
        sys.exit(1)
    try:
        args.func(args)                                 # 分发
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception:
        logger.exception("Command failed")
        sys.exit(1)
```

关键点：`args.func` 是谁设的？答案在 `BaseCommand.register()` 里——[lmcache/cli/commands/base.py:78-91](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/cli/commands/base.py#L78-L91)：

```python
def register(self, subparsers):
    parser = subparsers.add_parser(self.name(), help=self.help())
    self.add_arguments(parser)            # 让子类自己加参数
    _add_output_args(parser)              # 统一加 --format/--output/--quiet
    parser.set_defaults(func=self.execute)   # ← 关键：把 execute 绑成 func
```

也就是说，argparse 解析完后，`args.func` 自动指向该子命令的 `execute()`，`main.py` 只需 `args.func(args)` 即可分发。`BaseCommand` 要求每个子类实现四个抽象方法——[lmcache/cli/commands/base.py:49-76](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/cli/commands/base.py#L49-L76)：`name()`、`help()`、`add_arguments()`、`execute()`。

「自动发现」的核心在 [lmcache/cli/commands/\_\_init\_\_.py:15-38](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/cli/commands/__init__.py#L15-L38)：

```python
def _discover_commands() -> list[BaseCommand]:
    return [
        cls() for cls in discover_subclasses(
            __name__, BaseCommand,
            module_filter=lambda name: name != "base",   # 跳过定义基类的 base 模块
            on_import_error=_raise,                       # 子命令导入失败直接报错
        )
    ]
ALL_COMMANDS: list[BaseCommand] = _discover_commands()
```

`discover_subclasses()` 是一个通用工具（[lmcache/v1/utils/subclass_discovery.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/utils/subclass_discovery.py)）：它用 `pkgutil.iter_modules` 遍历包的子模块，import 后用 `inspect.getmembers` 找出所有 `BaseCommand` 的具体子类。注意 `on_import_error=_raise`——如果某个命令模块本身有 import 错误，会直接抛出而不是「悄悄消失」，这符合「坏命令要大声失败」的设计原则。

> 额外细节：`main()` 第一行调用的 `print_banner_once(sys.stderr)` 来自 [lmcache/banner.py:104-123](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/banner.py#L104-L123)，它「每进程只打一次」，可用 `LMCACHE_DISABLE_BANNER=1` 关闭，且只在 TTY 上着色。这也是入口行为的一部分。

#### 4.2.4 代码实践

1. **目标**：观察「自动发现」的威力——不改 `main.py` 就能让子命令出现。
2. **步骤**：
   - 运行 `lmcache --help`，记下当前子命令列表。
   - 阅读 [lmcache/cli/commands/coordinator.py:19-36](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/cli/commands/coordinator.py#L19-L36)，确认 `CoordinatorCommand` 就是 `lmcache coordinator` 子命令的来源（`name()` 返回 `"coordinator"`）。
   - （可选）仿照 `ping.py` 在 `lmcache/cli/commands/` 下新建一个继承 `BaseCommand` 的类，再跑 `lmcache --help`，看是否自动多出一条命令。
3. **观察现象**：新命令无需在 `main.py` 或 `__init__.py` 里登记就出现了。
4. **预期结果**：`--help` 输出里能看到新命令的 `name()` 与 `help()` 文本。
5. **待本地验证**：若环境是 slim 安装（缺 `uvicorn` 等依赖），`coordinator` 子命令的 `execute()` 会打印安装提示并 `sys.exit(1)`（见 [lmcache/cli/commands/coordinator.py:159-172](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/cli/commands/coordinator.py#L159-L172)）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `main.py` 里分发子命令只需写 `args.func(args)`，而不需要一长串 `if cmd == "ping": ... elif ...`？
**答案**：因为 `BaseCommand.register()` 用 `parser.set_defaults(func=self.execute)` 把每个子命令的 `execute` 绑到了 `args.func`，argparse 在解析时已自动选中正确的函数，分发逻辑被前移到注册阶段。

**练习 2**：如果一个新命令模块里有语法错误，`lmcache --help` 会怎样？
**答案**：`_discover_commands` 设置了 `on_import_error=_raise`，导入失败会直接抛异常，CLI 启动报错——而不是把这个坏命令静默从列表里漏掉。

---

### 4.3 独立 socket server：v1/server/__main__.py

#### 4.3.1 概念说明

`lmcache_server` 启动的是一个**最朴素的 KV 仓库**：它监听一个 TCP 端口，客户端连上来后用一套自定义的「二进制协议」发命令（`PUT`/`GET`/`EXIST`/`HEALTH`）。它不依赖 HTTP、不依赖 web 框架，纯粹用标准库的 `socket` 手写。理解它能让你看清「daemon 的最小骨架」长什么样。

#### 4.3.2 核心流程

启动 `lmcache_server 0.0.0.0 9999 cpu`：

```
main() 读 sys.argv: host=argv[1], port=argv[2], device=argv[3] or "cpu"
LMCacheServer(host, port, device):
    data_store = CreateStorageBackend(device)   # 按设备建存储后端
    server_socket = socket(AF_INET, SOCK_STREAM)
    bind((host, port)); listen()
server.run():                                    # accept 循环
    while True:
        client_socket, addr = server_socket.accept()
        threading.Thread(target=handle_client, ...).start()   # 每个连接一个线程
handle_client():                                 # 处理单个连接
    while True:
        读 ClientMetaMessage 头部
        match command:
            PUT:  读 length 字节 → data_store.put(meta, data)
            GET:  data_store.get(key) → 回 ServerMetaMessage + data（或 FAIL）
            EXIST/HEALTH: 回一个状态码
```

#### 4.3.3 源码精读

启动函数在 [lmcache/v1/server/\_\_main\_\_.py:150-166](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/server/__main__.py#L150-L166)，注意它**直接读位置参数**，不用 argparse：

```python
def main():
    import sys
    if len(sys.argv) not in [3, 4]:
        logger.error(f"Usage: {sys.argv[0]} <host> <port> <storage>(default:cpu)")
        exit(1)
    host = sys.argv[1]
    port = int(sys.argv[2])
    device = sys.argv[3] if len(sys.argv) == 4 else "cpu"
    server = LMCacheServer(host, port, device)
    server.run()
```

`LMCacheServer` 的构造在 [lmcache/v1/server/\_\_main\_\_.py:24-32](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/server/__main__.py#L24-L32)：用 `CreateStorageBackend(device)` 建后端，再建一个 TCP socket 并 `bind`+`listen`。`device` 字符串决定后端类型（如 `"cpu"`），`CreateStorageBackend` 定义在 [lmcache/v1/server/storage_backend/\_\_init\_\_.py:10](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/server/storage_backend/__init__.py#L10)。

accept 循环在 [lmcache/v1/server/\_\_main\_\_.py:137-147](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/server/__main__.py#L137-L147)：每来一个连接就 `threading.Thread` 起一个线程跑 `handle_client`——这是一个典型的「connection-per-thread」并发模型。

最值得读的是协议处理 [lmcache/v1/server/\_\_main\_\_.py:43-135](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/server/__main__.py#L43-L135)。它先 `receive_all` 读一个定长的 `ClientMetaMessage` 头部，再按 `meta.command` 用 `match/case` 分发：

```python
match meta.command:
    case ClientCommand.PUT:
        s = self.receive_all(client_socket, meta.length)   # 按 meta.length 读数据体
        self.data_store.put(meta, s)
    case ClientCommand.GET:
        obj = self.data_store.get(meta.key)
        if obj is not None:
            client_socket.sendall(ServerMetaMessage(SUCCESS, obj.length, ...).serialize())
            client_socket.sendall(obj.data)
        else:
            client_socket.sendall(ServerMetaMessage(FAIL, ...).serialize())
    case ClientCommand.EXIST: ...
    case ClientCommand.HEALTH: ...
```

这里出现的 `ClientCommand` / `ClientMetaMessage` / `ServerMetaMessage` / `ServerReturnCode` 都来自 [lmcache/v1/protocol.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/protocol.py)，定义了这套「meta 头 + 数据体」的二进制线协议（`ClientMetaMessage.packlength()` / `deserialize()` 等）。这套协议会在 u3-l4「HTTP API 与通信协议」里专门讲。

> 注意 `receive_all`（[lmcache/v1/server/\_\_main\_\_.py:34-41](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/server/__main__.py#L34-L41)）：TCP 不保证一次 `recv` 读全，所以要用循环读到凑满 `n` 字节——这是裸 socket 编程的标配细节。

#### 4.3.4 代码实践

1. **目标**：起一个 socket server，用 `HEALTH` 命令验证它活着。
2. **步骤**：
   - 阅读 [lmcache/v1/server/\_\_main\_\_.py:150-156](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/server/__main__.py#L150-L156)，确认用法是 `lmcache_server <host> <port> [device]`。
   - 启动：`lmcache_server 127.0.0.1 9999 cpu`（或 `python -m lmcache.v1.server 127.0.0.1 9999 cpu`）。
   - 观察日志里出现 `Server started at 127.0.0.1:9999`（对应 [lmcache/v1/server/\_\_main\_\_.py:138](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/server/__main__.py#L138)）。
   - 用任意 TCP 客户端（如 `nc`）连上去手工构造一个 `HEALTH` 的 meta 头发送，观察是否回 `SUCCESS`。
3. **观察现象**：每来一个连接，日志打印 `Connected by <addr>`；断开时打印 `Client disconnected`（[lmcache/v1/server/\_\_main\_\_.py:134](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/server/__main__.py#L134)）。
4. **预期结果**：服务常驻，`Ctrl-C` 退出时 `finally` 关闭 socket（[lmcache/v1/server/\_\_main\_\_.py:146-147](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/server/__main__.py#L146-L147)）。
5. **待本地验证**：手工拼二进制 meta 头较繁琐，若不便，至少把 `handle_client` 的 `match/case` 四个分支与 `receive_all` 走读一遍，确认你理解「头部 → 按长度读数据体」的拆包过程。

#### 4.3.5 小练习与答案

**练习 1**：为什么 server 用「每连接一个线程」而不是单线程顺序处理？
**答案**：因为单个客户端的 `handle_client` 是个 `while True` 长连接循环，若顺序处理，第二个客户端会被第一个阻塞；起线程能让多个连接并发被服务。

**练习 2**：如果把 `port` 传成非数字（如 `abc`），会发生什么？
**答案**：`int(sys.argv[2])` 会抛 `ValueError`，进程崩溃退出——因为这里没有做参数校验（也没有 argparse 的类型检查），这是「位置参数 + 裸 socket」风格的典型弱点。

---

### 4.4 MP coordinator：v1/mp_coordinator/__main__.py

#### 4.4.1 概念说明

coordinator（协调器）是多实例部署里的「指挥官」。当集群里跑了多个 LMCache 实例时，coordinator 负责记住「谁在线、谁持有哪些 KV」，并在实例失联时把它踢出。与 socket server 不同，coordinator 是一个**现代的 HTTP 服务**：用 FastAPI 构建应用、用 uvicorn 跑事件循环、配置全部来自环境变量。

它**不是** console script，而是用 `python -m lmcache.v1.mp_coordinator` 启动（也可以用等价的 `lmcache coordinator` 子命令，二者最终都调用同一个 `create_app`）。

#### 4.4.2 核心流程

启动 `python -m lmcache.v1.mp_coordinator`：

```
main():
    config = MPCoordinatorConfig.from_env()      # 读 LMCACHE_MP_COORDINATOR_* 环境变量
    app = create_app(config)                      # 用 FastAPI 工厂建 app（含 registry/eviction 等协作者）
    uvicorn.run(app, host=config.host, port=config.port, ...)
```

配置解析的优先级（高 → 低）：

```
CLI flag（lmcache coordinator --port 9300）
   ↓ 未设置则
环境变量 LMCACHE_MP_COORDINATOR_PORT
   ↓ 未设置则
MPCoordinatorConfig 字段默认值（port=9300）
```

#### 4.4.3 源码精读

启动函数极其简短——[lmcache/v1/mp_coordinator/\_\_main\_\_.py:20-30](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/__main__.py#L20-L30)：

```python
def main() -> None:
    config = MPCoordinatorConfig.from_env()      # 全部从环境变量来
    app = create_app(config)                      # FastAPI 应用工厂
    uvicorn.run(
        app,
        host=config.host, port=config.port,
        log_level="info",
        timeout_keep_alive=config.timeout_keep_alive,
    )
```

文件顶部 docstring 明确说明了运行方式与配置来源——[lmcache/v1/mp_coordinator/\_\_main\_\_.py:1-7](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/__main__.py#L1-L7)：`Run with python -m lmcache.v1.mp_coordinator. Configuration is read from LMCACHE_MP_COORDINATOR_* environment variables.`

配置对象的字段与默认值在 [lmcache/v1/mp_coordinator/config.py:63-77](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/config.py#L63-L77)，例如 `host="0.0.0.0"`、`port=9300`、`instance_timeout=30.0`、`health_check_interval=10.0` 等。它是一个 `frozen` dataclass，`__post_init__` 做校验（[lmcache/v1/mp_coordinator/config.py:79-110](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/config.py#L79-L110)，如 `eviction_ratio` 必须在 0~1 之间）。

`from_env()` 用统一前缀 `LMCACHE_MP_COORDINATOR_` 拼环境变量名——[lmcache/v1/mp_coordinator/config.py:112-171](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/config.py#L112-L171)：

```python
_ENV_PREFIX = "LMCACHE_MP_COORDINATOR_"
...
@classmethod
def from_env(cls):
    def _str(name, default):  return os.getenv(f"{_ENV_PREFIX}{name}", default)
    def _num(name, default, cast): ...      # 非法值时打 warning 并回退默认
    return cls(host=_str("HOST", cls.host), port=int(_num("PORT", cls.port, int)), ...)
```

也就是说：设 `LMCACHE_MP_COORDINATOR_PORT=9400` 就能把端口改成 9400，未设置则用默认 9300；非法值会打日志告警并回退（不会崩溃）。

> 等价的 CLI 路径：`lmcache coordinator` 子命令（[lmcache/cli/commands/coordinator.py:143-203](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/cli/commands/coordinator.py#L143-L203)）先 `MPCoordinatorConfig.from_env()`，再用 `dataclasses.replace` 把「用户显式传入的 CLI flag」覆盖到对应字段，最后同样 `create_app(config)` + `uvicorn.run(...)`。两条路殊途同归。

`create_app(config)` 是 FastAPI 应用工厂（[lmcache/v1/mp_coordinator/app.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/app.py)）：它把 `registry`（实例注册表）、`quota_manager`、`eviction_manager` 等协作者挂到 `app.state` 上，并在 lifespan 里启动「健康检查」「L2 淘汰」等后台任务。这些属于 u3 单元的 coordinator 深入内容，本讲只需记住：**coordinator = 配置（环境变量）+ FastAPI app + uvicorn**。

#### 4.4.4 代码实践

1. **目标**：用环境变量改 coordinator 的端口，验证 `from_env()` 生效。
2. **步骤**：
   - 读 [lmcache/v1/mp_coordinator/\_\_main\_\_.py:20-30](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_coordinator/__main__.py#L20-L30)，确认它不会读任何命令行参数，配置只来自 `from_env()`。
   - 运行 `LMCACHE_MP_COORDINATOR_PORT=9400 python -m lmcache.v1.mp_coordinator`。
   - 也可对比 `lmcache coordinator --port 9400`（CLI flag 覆盖）。
3. **观察现象**：uvicorn 启动日志显示监听 `0.0.0.0:9400`（而非默认 9300）。
4. **预期结果**：环境变量被 `from_env()` 读到，`config.port` 变为 9400，并传给 `uvicorn.run`。
5. **待本地验证**：若环境是 slim 安装缺少 `uvicorn`，进程会在 import 阶段失败——这正是 `lmcache coordinator` 子命令里那段 `try/except ImportError` 提示「请完整安装」的原因。

#### 4.4.5 小练习与答案

**练习 1**：为什么 coordinator 不像 socket server 那样直接读 `sys.argv`，而是用环境变量？
**答案**：因为 coordinator 是「配置项多、且常在容器/编排环境部署」的服务，环境变量更易于被 k8s/systemd 等注入，也便于和 `lmcache coordinator` 的 CLI flag 形成「env 设默认、flag 覆盖」的清晰优先级。

**练习 2**：`python -m lmcache.v1.mp_coordinator` 与 `lmcache coordinator` 最终调用的应用工厂是同一个吗？
**答案**：是的，两者都调用 `lmcache.v1.mp_coordinator.app.create_app(config)`，区别只在前者只用环境变量、后者额外允许 CLI flag 覆盖。

---

## 5. 综合实践

把本讲三个进程串成一张「输入 → 启动」对照表，这是本讲的核心产出。请在本地（或源码阅读）完成后填写：

| 进程 | 启动命令 | 入口文件:函数 | 配置来源 | 协议/并发模型 | 生命周期 |
| --- | --- | --- | --- | --- | --- |
| CLI | `lmcache <subcmd>` | `lmcache/cli/main.py:main` | argparse（子命令自带参数） | 无（一次性） | 短命 |
| socket server | `lmcache_server <host> <port> [device]` | `lmcache/v1/server/__main__.py:main` | `sys.argv` 位置参数 | 裸 TCP + 自定义二进制协议，connection-per-thread | 常驻 |
| coordinator | `python -m lmcache.v1.mp_coordinator` 或 `lmcache coordinator` | `lmcache/v1/mp_coordinator/__main__.py:main` | `LMCACHE_MP_COORDINATOR_*` 环境变量（+ CLI flag 覆盖） | HTTP（FastAPI + uvicorn 事件循环） | 常驻 |

进阶任务（可选）：

1. 用 `lmcache --help` 截图，对照 [lmcache/cli/commands/\_\_init\_\_.py:15-38](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/cli/commands/__init__.py#L15-L38) 解释每条子命令是从哪个模块「自动发现」来的。
2. 在同一台机器上分别启动 socket server 与 coordinator，用 `ss -ltnp`（或 `netstat`）确认它们分别监听了 9999 与 9300 端口，体会「两个独立 daemon」的差别。
3. 思考题：如果让你新增一个 daemon，你会选 socket server 的「位置参数 + 裸 socket」风格，还是 coordinator 的「环境变量 + FastAPI」风格？为什么？（提示：可观测性、客户端易用性、配置复杂度。）

## 6. 本讲小结

- LMCache 的「入口总目录」是 `pyproject.toml` 的 `[project.scripts]`，注册了 `lmcache` / `lmcache_server` / `lmcache_controller` 三个命令；**mp coordinator 不是 console script**，要用 `python -m lmcache.v1.mp_coordinator` 或 `lmcache coordinator` 启动。
- `lmcache` CLI 的根入口 `cli/main.py` 极薄，靠 `ALL_COMMANDS`（自动发现 `BaseCommand` 子类）+ `args.func(args)` 分发，新增子命令无需改 `main.py`。
- 自动发现由 `discover_subclasses()` 用 `pkgutil` + `inspect` 扫包实现，且「坏命令大声失败」（`on_import_error=_raise`）。
- `v1/server` 是「位置参数 + 裸 TCP + connection-per-thread + 自定义二进制协议」的最朴素 daemon；`v1/mp_coordinator` 是「环境变量 + FastAPI + uvicorn」的现代 HTTP daemon——两者代表了两种典型的服务进程风格。
- coordinator 的配置来自 `MPCoordinatorConfig.from_env()`，统一前缀 `LMCACHE_MP_COORDINATOR_*`，非法值会告警并回退默认，不会崩溃。

## 7. 下一步学习建议

- 想深入 coordinator 的内部协作（registry、eviction、blend directory）→ 进入 **u3-l3 MP Coordinator：跨实例协调**。
- 想搞清 socket server 那套 `ClientMetaMessage` / `ServerMetaMessage` 二进制线协议 → 进入 **u3-l4 HTTP API 与通信协议**。
- 想理解 `lmcache coordinator` / `lmcache server` 这类子命令的扩展写法 → 进入 **u4-l1 CLI 命令框架与扩展**。
- 如果你想先把「配置系统」彻底弄懂（为什么 coordinator 用环境变量、Engine 用 YAML）→ 回看 **u1-l5 配置系统：LMCacheEngineConfig**。
