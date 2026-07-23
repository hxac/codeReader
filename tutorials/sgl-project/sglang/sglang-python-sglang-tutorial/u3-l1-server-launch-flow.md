# 服务启动全流程：CLI 到 HTTP server

## 1. 本讲目标

学完本讲后，你应该能够：

- 把一条 `sglang serve ...` 命令在源码里「从敲下回车到 uvicorn 开始监听端口」的完整链路走一遍，并说出每一步发生在哪个文件的哪个函数。
- 理解 `prepare_server_args` 如何把命令行字符串解析、合并配置文件、最终构造并校验出一个 `ServerArgs` 对象。
- 理解 `load_plugins` 这个统一插件框架如何通过 setuptools `entry_points` 发现并执行插件、注册钩子，以及它「幂等、早调用」的设计。
- 理解 `launch_server` 如何在主进程里拉起 Scheduler / Detokenizer 子进程、初始化 TokenizerManager，把「引擎」组装起来。
- 看清 `_setup_and_run_http_server` 如何设置全局状态、装中间件、根据「单/多 tokenizer 进程」选择 uvicorn 或 Granian，最终通过 FastAPI 的 `lifespan` 触发 warmup，让服务真正就绪。

本讲是 u1-l2（启动第一个推理服务）的源码版深化：上一讲你学会了「用命令把服务跑起来」，本讲带你钻进源码看清「这条命令在内部到底做了哪些事、按什么顺序做」。

## 2. 前置知识

在追源码前，先建立几个直觉。

**「启动服务」其实是一连串「组装」动作**。很多人以为 `sglang serve` 就是「加载模型 → 开端口」两步。实际上它是一条很长的编排链：解析参数 → 加载插件 → 拉起若干子进程 → 建立进程间通信管道 → 创建 FastAPI 应用 → 注册一堆路由 → 装中间件 → 启动 HTTP 服务器 → 预热（warmup）→ 标记就绪。本讲的目标就是把这条链拆开。

**为什么需要子进程**。SGLang 运行时（SRT）由三类角色组成：`TokenizerManager`（主进程内）、`Scheduler`（独立子进程，负责组 batch 与 GPU 前向）、`DetokenizerManager`（独立子进程，负责把 token id 翻译回文本）。把它们放在不同进程，是为了让「调度 + GPU 计算」这条重路径不被主进程的 HTTP I/O 阻塞，三者之间用 ZMQ 通信。进程拓扑的细节留到 u3-l2，本讲只关注「谁在什么时候把它们拉起来」。

**FastAPI 的 `lifespan` 机制**。FastAPI（底层 Starlette）允许你给应用传一个 `lifespan` 异步生成器：在 `yield` 之前的代码是「启动时执行一次」，`yield` 之后是「关闭时执行一次」。SGLang 把 warmup 放在 `lifespan` 里，意味着「HTTP 端口已经监听、但模型还没预热完」这段时间，服务其实是「半就绪」状态。理解这点能解释很多「为什么刚启动时第一个请求很慢」的现象。

下面是本讲要追的完整调用链（从左到右是时间顺序）：

```
sglang serve <model> [opts]
   └─ cli/main.py: main()            解析子命令 "serve"，转发到 serve()
       └─ cli/serve.py: serve()
            ├─ load_plugins()        加载插件、注册钩子
            ├─ prepare_server_args() 解析参数 → ServerArgs（含 __post_init__ 校验）
            └─ run_server(server_args)
                 └─ launch_server.py: run_server()   按标志分发到 4 种服务形态
                      └─ (默认) http_server.launch_server()
                           ├─ Engine._launch_subprocesses()  拉起 Scheduler/Detokenizer 子进程 + 初始化 TokenizerManager
                           └─ _setup_and_run_http_server()
                                ├─ set_global_state()         把 manager 存进全局状态
                                ├─ 装中间件 (CORS / API key / metrics)
                                └─ uvicorn.run(app, ...)      启动 HTTP server
                                     └─ lifespan()            启动时：建 serving handlers + 起 warmup 线程
                                          └─ _wait_and_warmup() → _execute_server_warmup()  发一条真实请求预热
                                               → "The server is fired up and ready to roll!"
```

记住这张图，后面的四个最小模块就是在逐一展开它。

## 3. 本讲源码地图

本讲涉及的关键文件，按「启动时调用顺序」排列：

| 文件 | 作用 |
| --- | --- |
| [cli/main.py](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/cli/main.py) | CLI 总入口，用 `argparse` 子命令把 `serve` / `generate` / `version` 分发到各自处理函数。 |
| [cli/serve.py](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/cli/serve.py) | `serve` 子命令的处理函数：加载插件、区分「语言模型 / 扩散模型」、对语言模型调用 `prepare_server_args` + `run_server`。 |
| [launch_server.py](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/launch_server.py) | `run_server(server_args)`：根据 `server_args` 上的标志（encoder_only / gRPC / Ray / 默认），把服务分发到四种形态之一。 |
| [srt/server_args.py](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/server_args.py) | 定义 `ServerArgs` 巨型数据类，以及 `prepare_server_args(argv)` 解析入口和 `from_cli_args` 构造方法。 |
| [srt/plugins/__init__.py](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/plugins/__init__.py) | 统一插件框架：`load_plugins()` 通过 setuptools `entry_points` 发现并执行插件，再统一应用已注册的钩子。 |
| [srt/entrypoints/engine.py](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/entrypoints/engine.py) | `Engine._launch_subprocesses`：在主进程里拉起 Scheduler / Detokenizer 子进程并初始化 TokenizerManager。 |
| [srt/entrypoints/http_server.py](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/entrypoints/http_server.py) | 本讲重头戏：FastAPI `app` 的定义与路由表、`lifespan`、`launch_server`、`_setup_and_run_http_server`、warmup 全在这里。 |

> 提示：表格里的链接已固定到本讲使用的 HEAD `d0b9689805`，可直接点击阅读。下文正文中的「永久链接」均采用 `#L起始-L结束` 行号格式。

---

## 4. 核心概念与源码讲解

### 4.1 prepare_server_args：把命令行参数变成 ServerArgs

#### 4.1.1 概念说明

`sglang serve` 后面可以跟几十上百个参数（`--tp`、`--mem-fraction-static`、`--context-length`……）。SGLang 的做法是：用一个超大型的数据类 `ServerArgs` 作为「运行时的单一配置源」——整个运行时只认这一个对象。`prepare_server_args` 的职责就是把命令行上的字符串（以及可选的 `--config` 配置文件）转换并校验成这个对象。

它解决了三个问题：

1. **解析**：把 `["--tp", "2", "--port", "30000"]` 这样的字符串列表变成带类型的 Python 对象。这是标准 `argparse` 的工作。
2. **配置合并**：当用户传了 `--config server_args.yaml` 时，要把配置文件的值与命令行参数合并（命令行优先级更高），这由独立的 `ConfigArgumentMerger` 负责。
3. **校验与派生**：很多参数之间有约束（比如 `tp_size` 必须能整除 GPU 数、某些组合互斥）。这些校验与派生计算放在 `ServerArgs.__post_init__` 里，对象一构造完就立即跑。

#### 4.1.2 核心流程

`prepare_server_args` 的执行步骤：

1. 建 `argparse.ArgumentParser`，调用 `ServerArgs.add_cli_args(parser)` 把所有字段注册成命令行参数。
2. 如果 `argv` 里有 `--config`，用 `ConfigArgumentMerger` 把配置文件合并进 `argv`。
3. `parser.parse_args(argv)` 得到一个 `Namespace`。
4. 在 `__post_init__` 之前先 `logging.basicConfig` 配好基础日志（保证校验阶段的日志能正确输出）。
5. 调 `ServerArgs.from_cli_args(raw_args)` 构造对象，触发 `__post_init__` 完成校验与派生。

伪代码：

```text
def prepare_server_args(argv):
    parser = 新建 ArgumentParser(prog="sglang serve")
    ServerArgs.add_cli_args(parser)            # 注册所有 --xxx 参数
    if "--config" in argv:
        argv = ConfigArgumentMerger(parser).merge_config_with_args(argv)
    raw_args = parser.parse_args(argv)
    logging.basicConfig(level=raw_args.log_level, ...)
    return ServerArgs.from_cli_args(raw_args)  # 构造时触发 __post_init__ 校验
```

#### 4.1.3 源码精读

`serve()` 在判断出是「标准语言模型」后，会走到这条分支，调用 `prepare_server_args` 并把结果交给 `run_server`：

[cli/serve.py:134-141](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/cli/serve.py#L134-L141) — 语言模型分支：解析参数后调用 `run_server`。

`prepare_server_args` 本体逻辑很薄，但它串联起了「argparse + 配置合并 + 日志 + 数据类构造」四件事：

[srt/server_args.py:8682-8718](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/server_args.py#L8682-L8718) — 参数解析入口：建 parser、可选合并 `--config`、配日志、最后 `from_cli_args`。

注意第 8697-8703 行的 `--config` 合并：只有命令行里真的出现 `--config` 时，才会导入 `ConfigArgumentMerger`（注释说是为了规避循环导入）。合并后 `argv` 被替换，因此「命令行显式传的参数」能覆盖「配置文件里的默认值」。

`from_cli_args` 的实现非常简洁，它体现了「数据类字段即 CLI 参数」的设计——只把 Namespace 上确实存在的字段取出来传给构造器：

[srt/server_args.py:7830-7837](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/server_args.py#L7830-L7837) — `from_cli_args`：把 argparse Namespace 上存在的字段透传给 `ServerArgs(...)`，跳过那些没有 CLI 表面的字段（如 `stat_loggers`）。

构造完成后，`__post_init__`（位于 [srt/server_args.py:3290](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/server_args.py#L3290)）会做大量校验与派生计算（例如根据 `tp_size`/`dp_size` 推算 worker 数、把已废弃的 `--grpc-mode` 折叠进 `smg_grpc_mode`、校验互斥参数等）。本讲不展开它的内部，记住「`from_cli_args` 返回时，参数已经合法且派生字段已就绪」即可——这也是后续 `run_server` 能放心按标志分发的底气。

> 术语：`__post_init__` 是 Python `@dataclass` 的钩子方法，对象构造完会自动调用一次，常用于校验和派生计算。`ServerArgs` 的配置体系细节（含 `arg_groups` 分组）是 u3-l3 的主题，本讲只用到它的「解析入口」。

#### 4.1.4 代码实践

**实践目标**：亲手走一遍「命令行字符串 → ServerArgs 对象」的解析过程，不启动服务，纯用 Python 调用。

**操作步骤**：

1. 在装好 sglang 的环境里进入 Python：
   ```bash
   python -c "
   from sglang.srt.server_args import prepare_server_args
   sa = prepare_server_args(['--model-path', 'Qwen/Qwen2.5-0.5B', '--tp', '2', '--port', '30000'])
   print('tp_size =', sa.tp_size)
   print('port    =', sa.port)
   print('host    =', sa.host)
   print('url     =', sa.url())
   "
   ```
2. 再故意传一个非法组合（例如 `--tp 3` 而你只有 1 张卡，或两个互斥参数），观察 `__post_init__` 抛出的错误信息。

**需要观察的现象**：

- 第 1 步应打印出 `tp_size = 2`、`port = 30000` 等值，证明字符串已被解析成对象。
- 第 2 步应在 `from_cli_args` 阶段（即 `__post_init__` 内）就抛异常，**服务根本不会启动**——说明校验发生在很早的阶段。

**预期结果**：你能看到一个构造好的 `ServerArgs`，并理解「参数错误」会在这一步被拦截，而不是等到后面拉子进程时才崩。具体的报错文案与是否触发取决于本机硬件，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `prepare_server_args` 里要先 `logging.basicConfig` 再 `from_cli_args`，而不是反过来？
<details><summary>参考答案</summary>
因为 `ServerArgs.__post_init__` 里会有 `logger.info` / `logger.warning` 调用（例如提示某个参数被自动调整、某个旧别名被迁移）。如果先构造对象，这些日志就会在日志格式配置好之前输出，格式不对、甚至可能丢失。先 `basicConfig` 能保证校验阶段的日志按统一格式打印。</details>

**练习 2**：`from_cli_args` 里为什么用 `if hasattr(args, attr.name)` 过滤字段，而不是直接 `cls(**vars(args))`？
<details><summary>参考答案</summary>
因为有些数据类字段（如 `stat_loggers`）是「故意没有 CLI 表面」的——它们不在 argparse 注册，因此不会出现在 `Namespace` 上。直接透传全部 Namespace 属性会传入 `ServerArgs` 不认识的参数而报错；过滤后这些字段会使用数据类自带默认值。</details>

---

### 4.2 load_plugins：统一插件框架

#### 4.2.1 概念说明

`load_plugins()` 在启动链里出现得很早（`serve()` 一进来、解析参数之前就调用），但它做的事很「元」：**让外部 pip 包能在不修改 sglang 源码的前提下，向运行时注入钩子、替换类、注册自定义硬件平台**。

它基于 Python 打包标准的 `entry_points` 机制：任何 pip 包都可以在自己的元数据里声明「我属于 `sglang.srt.plugins` 这个入口组」，sglang 启动时用 `importlib.metadata.entry_points(group=...)` 把它们全部发现出来并执行。

为什么要在「解析参数之前」就加载？因为插件可能注册新的命令行参数（通过 `arg_groups` 钩子）、替换默认的硬件平台、或给某些函数挂上钩子——这些都必须在 `ServerArgs` 构造、模型加载之前完成。

#### 4.2.2 核心流程

`load_plugins` 的执行步骤：

1. **幂等检查**：用一个模块级标志 `_plugins_loaded` 保证同一进程内多次调用只真正执行一次（启动链里 `serve()`、`launch_server`、`Engine._launch_subprocesses` 都可能调用它）。
2. **发现**：`load_plugins_by_group("sglang.srt.plugins")` 枚举该入口组下所有插件；支持用环境变量 `SGLANG_PLUGINS`（逗号分隔）做白名单过滤；当设置了 `SGLANG_PLATFORM` 时，自动跳过未被选中的平台包，避免拉入它们的硬件依赖。
3. **执行**：逐个 `ep.load()` 拿到可调用对象并调用它——插件的「副作用」（注册钩子、替换类）就是它要的效果，返回值被忽略。
4. **应用钩子**：所有插件执行完后，统一调用 `HookRegistry.apply_hooks()`，把这一轮注册的钩子真正打到目标函数上。

伪代码：

```text
def load_plugins():
    if _plugins_loaded: return        # 幂等
    _plugins_loaded = True
    plugins = load_plugins_by_group("sglang.srt.plugins",
                                    excluded_dists=_get_excluded_dists())
    for name, (func, dist) in plugins.items():
        在 _current_plugin_source 上下文里执行 func()   # 插件靠副作用注册钩子
    HookRegistry.apply_hooks()        # 统一把钩子装到目标函数
```

#### 4.2.3 源码精读

`serve()` 里插件加载发生在解析参数之前，且与具体走「语言模型 / 扩散模型」哪条分支无关：

[cli/serve.py:97-99](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/cli/serve.py#L97-L99) — `serve()` 入口处先 `load_plugins()`，再开始分流。

`load_plugins` 本体强调「幂等 + 早调用」，注释明确指出它应在每个进程（main / engine core / workers）里尽早调用：

[srt/plugins/__init__.py:103-141](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/plugins/__init__.py#L103-L141) — 加载并执行所有通用插件，最后统一 `apply_hooks()`。

发现逻辑 `load_plugins_by_group` 是理解插件「从哪来」的关键——它就是标准的 `entry_points` 枚举，外加白名单与排除集：

[srt/plugins/__init__.py:35-86](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/plugins/__init__.py#L35-L86) — 枚举入口组、按 `SGLANG_PLUGINS` 白名单和 `SGLANG_PLATFORM` 排除集过滤、逐个 `ep.load()`。

注意第 51-55 行的 `SGLANG_PLUGINS` 白名单：当装了多个插件但只想启用其中几个时，用这个环境变量逗号分隔即可，没在名单里的会被 `logger.info` 提示跳过。第 89-100 行的 `_get_excluded_dists` 则解决了一个常见痛点——当用户用 `SGLANG_PLATFORM=npu` 选定华为 NPU 平台时，已安装的其它平台包（如 MLX）不应再注册钩子，否则会拉入互相冲突的硬件依赖。

> 插件的具体能力（`HookRegistry` 如何替换函数、注册参数）属于 u9-l5 的扩展开发主题。本讲只要记住：`load_plugins()` 是「让外部代码介入运行时」的总开关，且它在启动链里出现得足够早。

#### 4.2.4 代码实践

**实践目标**：在没有安装任何第三方插件的环境下，观察 `load_plugins` 的「空跑」行为，确认它是幂等且安全的。

**操作步骤**：

1. 运行下面这段，观察日志：
   ```bash
   python -c "
   import logging; logging.basicConfig(level=logging.DEBUG)
   from sglang.srt.plugins import load_plugins
   load_plugins()   # 第一次：真正执行（即便没插件）
   load_plugins()   # 第二次：因 _plugins_loaded 为 True 而直接返回
   print('done')
   "
   ```

**需要观察的现象**：

- 第一次调用时，`load_plugins_by_group` 内部会打印 `No plugins found for group sglang.srt.plugins.`（DEBUG 级别，因为没装插件时 `discovered` 为空）。
- 第二次调用**不会再有任何插件相关日志**，证明幂等守卫生效。

**预期结果**：在没有第三方插件时，`load_plugins` 是无副作用的空操作，但它的发现机制已经跑通。如果你装了带 `sglang.srt.plugins` 入口点的包，会看到 `Available plugins for group ...` 和 `Loaded plugin ...` 日志。具体是否看到这些取决于本地安装了什么，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`_plugins_loaded` 这个模块级布尔变量为什么是必要的？删掉它会怎样？
<details><summary>参考答案</summary>
因为启动链里多处会调用 `load_plugins()`（`serve()`、`launch_server.py` 的 `__main__`、`Engine._launch_subprocesses`）。如果没有幂等守卫，同一个进程里插件会被执行多次、钩子会被重复注册。虽然 `apply_hooks` 自身对「已 patch 的目标」会跳过（注释说 idempotent），但插件函数本身的副作用（如往某个全局列表 append）可能被重复触发。守卫从源头保证只执行一次。</details>

**练习 2**：`SGLANG_PLUGINS` 和 `SGLANG_PLATFORM` 这两个环境变量分别控制什么？
<details><summary>参考答案</summary>
`SGLANG_PLUGINS` 是「通用插件白名单」：逗号分隔的插件名，只有名单内的通用插件会被加载。`SGLANG_PLATFORM` 是「选定哪个硬件平台」：一旦设定，其它平台包提供的通用插件会被自动排除（`_get_excluded_dists`），避免拉入未被选中平台的硬件依赖。前者是「启用哪些」，后者是「排除哪些」。</details>

---

### 4.3 launch_server：拉起子进程组装引擎

#### 4.3.1 概念说明

拿到合法的 `ServerArgs` 后，下一步是「真正把引擎跑起来」。这里有两个层次：

- **`run_server(server_args)`**（在 `launch_server.py`）是一个**分发器**：它不看模型、不做组装，只根据 `server_args` 上的几个标志，把控制权交给四种服务形态之一。
- **`http_server.launch_server(server_args)`**（默认形态）才是**真正的组装函数**：它在主进程里拉起 Scheduler / Detokenizer 子进程、初始化 TokenizerManager，再把这些对象交给 HTTP 层。

为什么要分一个「分发器」出来？因为 SGLang 支持多种部署形态（普通 HTTP、Ray 后端、SMG gRPC、encoder 分离），它们的「组装方式」差别很大，但入口参数是同一份 `ServerArgs`。`run_server` 用一组 `if/elif` 把这层差异收敛掉，让上游（`serve()`）只管调一个函数。

#### 4.3.2 核心流程

**`run_server` 的分发逻辑**（按优先级）：

1. `server_args.encoder_only` → encoder 分离形态（PD 分离的 encode 侧，u8-l1）。
2. `server_args.smg_grpc_mode` → SMG gRPC 服务。
3. `server_args.use_ray` → Ray 后端的 HTTP 服务。
4. **否则（默认）** → `http_server.launch_server`，即本讲重点。

**`http_server.launch_server` 的组装步骤**：

1. 调 `Engine._launch_subprocesses(server_args, ...)`，返回 `(tokenizer_manager, template_manager, port_args, scheduler_init_result, subprocess_watchdog)`。
2. 把这些对象传给 `_setup_and_run_http_server(...)`（见 4.4）。

而 `_launch_subprocesses` 内部又按顺序做了：配置日志与环境 → 分配 IPC 端口（`PortArgs.init_new`）→ 拉起 Scheduler 子进程 → 拉起 Detokenizer 子进程 → 在主进程初始化 TokenizerManager。

伪代码（默认 HTTP 形态）：

```text
def launch_server(server_args):
    (tokenizer_manager, template_manager, port_args,
     scheduler_init_result, watchdog) = Engine._launch_subprocesses(server_args, ...)
    _setup_and_run_http_server(server_args, tokenizer_manager, template_manager,
                               port_args, scheduler_init_result.scheduler_infos, watchdog)
```

#### 4.3.3 源码精读

`run_server` 的四个分支结构清晰，最后一个 `else` 就是默认的 HTTP 形态——绝大多数用户走的就是这条路：

[launch_server.py:15-52](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/launch_server.py#L15-L52) — `run_server`：按 `encoder_only` / `smg_grpc_mode` / `use_ray` / 默认分发到四种形态。注意第 48-52 行的默认分支导入并调用 `http_server.launch_server`。

> 旁注：`launch_server.py` 也可以用 `python -m sglang.launch_server` 直接跑（见文件末尾 [launch_server.py:55-73](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/launch_server.py#L55-L73)），但它会发一条 `UserWarning` 提示「推荐用 `sglang serve`」。这条旧入口同样会 `load_plugins()` + `prepare_server_args()` + `run_server()`，是 `serve()` 的「等价但被弃用」版本。

`http_server.launch_server` 的 docstring 把「三大组件 + 进程归属 + ZMQ 通信」讲得很清楚，是理解进程拓扑的最佳入口：

[srt/entrypoints/http_server.py:2648-2694](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/entrypoints/http_server.py#L2648-L2694) — `launch_server`：先 `_launch_subprocesses`，再 `_setup_and_run_http_server`。

注意 docstring（第 2657-2670 行）点明的三件事：

1. HTTP server、Engine、TokenizerManager 都在**主进程**；
2. Scheduler、DetokenizerManager 各为**子进程**；
3. 进程间通过 ZMQ（每个进程用不同端口）通信。

函数体只有两步：第 2671-2683 行调 `_launch_subprocesses`，第 2685-2694 行调 `_setup_and_run_http_server`。组装的重活都在 `_launch_subprocesses` 里。

`Engine._launch_subprocesses` 是一个 `@classmethod`，按「Scheduler → Detokenizer → TokenizerManager」的顺序组装，下面是关键片段：

[srt/entrypoints/engine.py:790-878](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/entrypoints/engine.py#L790-L878) — 组装引擎的核心：校验参数、分配 IPC 端口、依次拉起 Scheduler / Detokenizer 子进程、最后初始化 TokenizerManager。

逐段看：

- 第 790 行 `load_plugins()` 是**防御性**的——上游 `serve()` 已经调过一次，这里再调一次保险（`_launch_subprocesses` 也可能被 `sglang.Engine` 嵌入式 API 直接调用，那条路径不一定经过 `serve()`）。这正是 4.2 强调「`load_plugins` 必须幂等」的原因。
- 第 792-793 行：`check_server_args()` 做最后的一致性校验，`_set_gc()` 配置垃圾回收策略。
- 第 796-797 行：`PortArgs.init_new(server_args)` 分配进程间通信要用到的端口/IPC 名（`scheduler_input_ipc_name`、`detokenizer_ipc_name`、`nccl_port` 等），这一步决定了「谁往哪个管道发消息」。
- 第 824-826 行：`_launch_scheduler_processes` 拉起 Scheduler 子进程（TP/DP/PP 下会有多个）。
- 第 866-870 行：`_launch_detokenizer_subprocesses` 拉起 Detokenizer 子进程。
- 第 875-878 行：在**主进程**里 `init_tokenizer_manager_func(server_args, port_args)` 初始化 TokenizerManager——这就是后面 HTTP 路由要用的那个对象。

> 术语：`PortArgs`（定义在 [srt/server_args.py:8725](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/server_args.py#L8725)）是一组「进程间通信地址」的集合，包含 tokenizer/scheduler/detokenizer 三方互发消息用的 IPC 名与 NCCL 初始化端口。它和进程拓扑（u3-l2）紧密相关。

#### 4.3.4 代码实践

**实践目标**：通过启动日志，确认 `launch_server` 确实按「Scheduler 子进程 → Detokenizer 子进程 → TokenizerManager」的顺序完成了组装，并看到分配出的 IPC 端口。

**操作步骤**：

1. 用一个小模型启动服务，并保留完整日志：
   ```bash
   sglang serve --model-path Qwen/Qwen2.5-0.5B --port 30000 --log-level info 2>&1 | tee /tmp/sglang_launch.log
   ```
2. 在另一个终端，待日志稳定后用 `ps` 查看进程树：
   ```bash
   ps -ef | grep -E "scheduler|detokenizer|sglang" | grep -v grep
   ```

**需要观察的现象**：

- 日志里会出现一行 `server_args=ServerArgs(...)`（对应 [engine.py:798](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/entrypoints/engine.py#L798) 的 `logger.info(f"{server_args=}")`），打印出最终生效的全部参数。
- `ps` 输出里能看到至少 3 个相关进程：1 个主进程（含 HTTP/TokenizerManager）、1 个 Scheduler 进程、1 个 Detokenizer 进程（多卡/多 DP 时更多）。
- 子进程的命令行里会带类似 `--scheduler` 等内部标志，表明它们是被 `_launch_subprocesses` 以特定角色拉起的。

**预期结果**：你会直观看到「一个 sglang serve 命令 = 1 主进程 + 若干子进程」。具体的进程数量取决于 `--tp`/`--dp`，**待本地验证**。把观察到的进程数与你的并行度配置对上，就完成了对 `launch_server` 组装逻辑的验证。

#### 4.3.5 小练习与答案

**练习 1**：`run_server` 为什么把「默认 HTTP 形态」放在最后用 `else` 兜底，而不是显式 `if not <其它标志>`？
<details><summary>参考答案</summary>
因为这四种形态是「互斥优先级」关系，且默认形态最常见。把最常见的情况放在 `else` 里，既省去一次条件判断，也让代码意图清晰：前面几个 `if/elif` 是「特殊形态的特例」，剩下的就是普通 HTTP 服务。任何不属于特例的配置都落到默认路径，符合「默认安全、显式 opt-in 特殊模式」的设计。</details>

**练习 2**：为什么 `_launch_subprocesses` 里要再调一次 `load_plugins()`，尽管 `serve()` 已经调过？
<reference>参考答案</reference>
<details><summary>参考答案</summary>
因为 `_launch_subprocesses` 是一个可被多条入口复用的组装函数：除了 `serve()` → `run_server` 这条 CLI 链，`sglang.Engine(...)` 嵌入式 API（u1-l4）也会直接走到这里，而那条路径不一定经过 `serve()`。为了在所有入口下都保证「插件已加载」，这里做一次防御性调用。由于 `load_plugins` 是幂等的（4.2），重复调用无副作用。</details>

---

### 4.4 _setup_and_run_http_server：挂载路由、配置中间件、启动 uvicorn

#### 4.4.1 概念说明

到这里，引擎已经在主进程里组装好了（`tokenizer_manager` 等对象已就位）。`_setup_and_run_http_server` 的职责是：**把这些对象「挂」到 HTTP 层，然后启动 HTTP 服务器**。

这里有一个容易困惑的点需要先澄清：**FastAPI 的路由表是「在模块导入时」就注册好的，不是在这个函数里动态注册的**。`http_server.py` 在模块顶层就执行了 `app = FastAPI(...)` 并用大量 `@app.get / @app.api_route` 装饰器把 `/generate`、`/v1/chat/completions`、`/health` 等几十个路由绑好。`_setup_and_run_http_server` 不需要再去注册路由，它要做的是：

1. 把组装好的 `tokenizer_manager` 等对象存进**全局状态** `_global_state`，让那些路由 handler 能取到。
2. 根据配置装上**中间件**（CORS、API key 鉴权、Prometheus 指标）。
3. 根据是「单 tokenizer 进程」还是「多 Tokenizer 进程」选择不同的启动方式（单进程直接 `uvicorn.run(app)`；多进程要写共享内存并让 uvicorn fork 出 worker）。
4. 在 HTTP/2、SSL 证书自动刷新等特殊配置下，切换到 Granian 或带 SSL refresher 的 uvicorn。

#### 4.4.2 核心流程

`_setup_and_run_http_server` 的步骤：

1. `set_global_state(...)`：把 `tokenizer_manager`、`template_manager`、`scheduler_info` 存进全局 `_global_state`。
2. （可选）`add_prometheus_track_response_middleware(app)`：开启指标采集。
3. 区分单/多 tokenizer 模式：
   - 单 tokenizer：把 `server_args`、warmup 参数直接挂在 `app` 对象上，供 `lifespan` 读取；必要时加 API key 中间件。
   - 多 tokenizer：把参数写进共享内存，供其它 worker 进程读取。
4. `set_uvicorn_logging_configs(server_args)` 配日志。
5. 根据配置启动 HTTP server：默认 `uvicorn.run(app, ...)`，HTTP/2 用 Granian，SSL 刷新用带 refresher 的 uvicorn。
6. `uvicorn.run` 内部会触发 FastAPI 的 `lifespan`，`lifespan` 里建好各 OpenAI/Ollama/Anthropic serving handler，并**起一个 warmup 线程**发一条真实请求预热，预热成功后才标记 `ServerStatus.Up`。

启动到「真正就绪」的时间关系（warmup 等待上限）：

\[ T_{\text{warmup\_wait}} = \min(\text{就绪},\; 120 \times 1\text{s}) \]

即 `_execute_server_warmup` 最多轮询 120 次、每次间隔 1 秒去探测 `/model_info`（见 [http_server.py:2127-2138](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/entrypoints/http_server.py#L2127-L2138)）。

#### 4.4.3 源码精读

先看路由表是「模块导入时」建好的——`app` 在顶层就创建并装了 CORS 中间件、include 了路由：

[srt/entrypoints/http_server.py:429-455](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/entrypoints/http_server.py#L429-L455) — `app = FastAPI(lifespan=lifespan, ...)`，加 CORS，按需加请求解压中间件，include `v1_loads` 与 `elastic_ep` 路由。

注意第 430 行 `lifespan=lifespan`：这里把「启动/关闭钩子」绑定到 app 上，后面 `uvicorn.run(app)` 一启动就会执行它。文件里随后几十个 `@app.get("/health")`、`@app.api_route("/generate", ...)` 等装饰器（如 [http_server.py:616-617](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/entrypoints/http_server.py#L616-L617)、[http_server.py:828](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/entrypoints/http_server.py#L828)）就是在「导入模块」时把这些 URL 绑定到 handler 函数。

`_setup_and_run_http_server` 开头先把组装好的对象存进全局状态——这是路由 handler 能拿到运行时的唯一通道：

[srt/entrypoints/http_server.py:2413-2424](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/entrypoints/http_server.py#L2413-L2424) — `set_global_state(_GlobalState(tokenizer_manager=..., template_manager=..., scheduler_info=...))`，并把 watchdog 挂到 tokenizer_manager 上。

接着是单 tokenizer 模式的处理——把后续 `lifespan` 要用的参数挂在 `app` 上，并按需加 API key 鉴权中间件：

[srt/entrypoints/http_server.py:2431-2459](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/entrypoints/http_server.py#L2431-L2459) — 单 tokenizer 模式：设 `app.is_single_tokenizer_mode`、挂 `server_args` 和 warmup 参数；当配置了 `api_key`/`admin_api_key` 时加鉴权中间件。

最后是最关键的「启动 HTTP server」分支。默认（单进程、无 HTTP/2、无 SSL 刷新）走最简单的 `uvicorn.run(app, ...)`：

[srt/entrypoints/http_server.py:2532-2546](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/entrypoints/http_server.py#L2532-L2546) — 默认启动路径：`uvicorn.run(app, host, port, loop="uvloop", ssl_*, ...)`。

注意几个细节：`loop="uvloop"` 用更快的 uvloop 替代默认 asyncio 事件循环；`root_path=server_args.fastapi_root_path` 支持反代部署；`timeout_keep_alive` 来自环境变量 `SGLANG_TIMEOUT_KEEP_ALIVE`。如果是多 Tokenizer 进程（[http_server.py:2580-2594](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/entrypoints/http_server.py#L2580-L2594)），则传字符串形式的 app 路径 `"sglang.srt.entrypoints.http_server:app"` 并设 `workers=tokenizer_worker_num`，让 uvicorn 自己 fork 出多个 worker 进程。

**`lifespan`：服务真正就绪的最后一步**。`uvicorn.run` 启动后会调用绑定在 app 上的 `lifespan`。它在 `yield` 之前做一堆初始化：建好各 OpenAI/Ollama/Anthropic serving handler、按需启动原生 gRPC、然后**起一个 warmup 线程**：

[srt/entrypoints/http_server.py:406-414](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/entrypoints/http_server.py#L406-L414) — `lifespan` 里用独立线程跑 `_wait_and_warmup`，随后 `yield` 把控制权交还给 uvicorn 开始接收请求。

为什么要用**独立线程**做 warmup？因为 warmup 要往「自己这个 HTTP 服务」发请求（见下面 `_execute_server_warmup`），而 `lifespan` 跑在 uvicorn 的主事件循环里——如果在主循环里同步等待，就会死锁（服务还没开始接请求，却在自己等自己的响应）。放进后台线程，主循环 `yield` 后立即开始监听，warmup 线程在后台探测，两者并行不冲突。

warmup 线程的实际工作分两段：先轮询 `/model_info` 等服务起来，再发一条真实推理请求预热 CUDA graph / KV cache：

[srt/entrypoints/http_server.py:2269-2303](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/entrypoints/http_server.py#L2269-L2303) — `_wait_and_warmup`：可选先等权重就绪，再调 `execute_warmup_func` 发预热请求，成功后打印 "The server is fired up and ready to roll!" 并触发 `launch_callback`。

预热请求的构造在 `_execute_server_warmup` 里——它会根据是「生成模型 / embedding 模型 / VLM」选择不同的预热端点（`/generate`、`/encode` 或 `/v1/chat/completions`），并发出一条带 `max_new_tokens=8` 的小请求：

[srt/entrypoints/http_server.py:2117-2143](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/entrypoints/http_server.py#L2117-L2143) — `_execute_server_warmup` 开头：轮询 `/model_info`（最多 120 次、每次 1 秒）等服务就绪，失败则 `kill_process_tree` 终止。

预热成功后，`_global_state.tokenizer_manager.server_status` 被设为 `ServerStatus.Up`（[http_server.py:2231](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/entrypoints/http_server.py#L2231)），外部才能认为服务真正可用。这也解释了**为什么刚启动时第一条用户请求往往偏慢**：warmup 完成前，服务处于「端口已开、但未就绪」状态；warmup 本身又要等模型加载 + 一次完整前向。

> 术语：`lifespan` 是 Starlette/FastAPI 的「应用生命周期钩子」，`yield` 前=启动初始化、`yield` 后=关闭清理。`uvloop` 是 asyncio 事件循环的高性能 C 实现。`Granian` 是一个 Rust 写的 ASGI 服务器，SGLang 在需要 HTTP/2 时用它替代 uvicorn。

#### 4.4.4 代码实践

**实践目标**：按本讲实践任务的描述，在启动链的三个关键节点（拉起子进程、设置全局状态/挂路由、启动 uvicorn）前后加日志，启动一次服务，按时间顺序记录每个阶段，画出启动时序图。

**操作步骤**：

1. 在三处加临时 `logger.info`（这是「源码阅读型实践」，记得改完测完要还原，不要提交）：
   - 拉起子进程前后：在 [engine.py:824](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/entrypoints/engine.py#L824) 的 `_launch_scheduler_processes` 调用前后各加一行 `logger.info("[TRACE] before/after launch scheduler subprocesses")`。
   - 设置全局状态前后：在 [http_server.py:2414](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/entrypoints/http_server.py#L2414) 的 `set_global_state(...)` 前后各加一行 `logger.info("[TRACE] before/after set_global_state")`。
   - 启动 uvicorn 前后：在 [http_server.py:2534](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/entrypoints/http_server.py#L2534) 的 `uvicorn.run(...)` 前加一行 `logger.info("[TRACE] before uvicorn.run")`（`uvicorn.run` 会阻塞，退出后才到「后」）。
2. 启动服务并抓取带时间戳的日志：
   ```bash
   sglang serve --model-path Qwen/Qwen2.5-0.5B --port 30000 --log-level info 2>&1 | tee /tmp/sglang_trace.log
   ```
3. 待看到 `The server is fired up and ready to roll!` 后停止服务。

**需要观察的现象**：日志中 `[TRACE]` 标记会按以下时间顺序出现：

```
[TRACE] before launch scheduler subprocesses     ← 4.3 子进程阶段
[TRACE] after  launch scheduler subprocesses
[TRACE] before set_global_state                  ← 4.4 挂全局状态阶段
[TRACE] after  set_global_state
[TRACE] before uvicorn.run                       ← HTTP server 启动
... (lifespan 执行，warmup 线程探测 /model_info、发预热请求) ...
The server is fired up and ready to roll!         ← 真正就绪
```

**预期结果**：你能据此画出一张启动时序图，三个阶段的时间间隔会揭示「子进程拉起 + 模型加载」通常是耗时大头，而 `set_global_state` → `uvicorn.run` 几乎瞬时。各阶段绝对耗时取决于模型大小与硬件，**待本地验证**。

> 还原提醒：本实践修改了三个源码文件，仅用于学习。确认现象后请用 `git checkout -- cli/... srt/entrypoints/engine.py srt/entrypoints/http_server.py`（或对应路径）还原，不要把调试日志带入正式环境。

#### 4.4.5 小练习与答案

**练习 1**：为什么 warmup 要放在**独立线程**里，而不是直接在 `lifespan` 的主事件循环里同步等待？
<details><summary>参考答案</summary>
因为 warmup 的探测请求（`/model_info`、`/generate`）是发往「本服务自己」的。`lifespan` 跑在 uvicorn 的主事件循环里，如果在主循环里同步等待响应，而服务此时还没开始接收请求（要等 `lifespan` 执行到 `yield`），就会形成「自己等自己」的死锁。放进后台线程，主循环可以顺利 `yield` 开始监听，warmup 在后台并发探测，互不阻塞。</details>

**练习 2**：`app = FastAPI(...)` 和那些 `@app.get(...)` 路由是在「启动服务时」动态注册的，还是在「模块导入时」就绑好的？这对 `_setup_and_run_http_server` 的职责有什么影响？
<details><summary>参考答案</summary>
是在「模块导入 `http_server` 时」就绑好的（模块顶层代码在 import 时执行）。因此 `_setup_and_run_http_server` **不需要**再去注册路由——它的职责是「把已组装好的运行时对象存进全局状态、装中间件、启动 uvicorn」。路由 handler 通过 `_global_state` 间接拿到 `tokenizer_manager`。理解这点能避免「以为路由是在这个函数里加的」的误解。</details>

**练习 3**：单 Tokenizer 进程时传给 uvicorn 的是 `app` 对象本身，而多 Tokenizer 进程时传的是字符串 `"sglang.srt.entrypoints.http_server:app"`，为什么？
<reference>参考答案</reference>
<details><summary>参考答案</summary>
单进程时 uvicorn 直接在当前进程跑这个现成的 app 对象即可。多进程时 uvicorn 需要 `workers=N` fork 出多个 worker，每个 worker 都得重新 import 模块、各自拿到一份 app 实例——传对象无法跨进程复制，必须传「模块:属性」字符串让每个 worker 自己 import 并定位 app。</details>

---

## 5. 综合实践

把本讲四个最小模块串起来，完成一次「启动链路逆向标注」任务：

1. **准备**：选一个小模型（如 `Qwen/Qwen2.5-0.5B`），确认 `sglang serve --help` 能正常输出（这会触发 [cli/serve.py:71-76](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/cli/serve.py#L71-L76) 的 help 分支，间接验证 `prepare_server_args(["--help"])` 可用）。
2. **标注调用链**：对照第 2 节的那张调用链图，在源码里为每个箭头找到对应的「文件:函数:行号」，填进下表：

   | 阶段 | 函数 | 文件:行号 | 你观察到的证据（日志/进程/返回值） |
   | --- | --- | --- | --- |
   | CLI 分发 | `main` → `serve` | | |
   | 加载插件 | `load_plugins` | | |
   | 解析参数 | `prepare_server_args` | | |
   | 分发服务形态 | `run_server` | | |
   | 组装引擎 | `_launch_subprocesses` | | |
   | 挂全局状态 | `set_global_state` | | |
   | 启动 HTTP | `uvicorn.run` | | |
   | 预热就绪 | `_execute_server_warmup` | | |

3. **验证就绪判定**：在 warmup 完成前（服务刚启动几秒内）连续 `curl http://localhost:30000/health`，观察它何时返回成功；再对照日志里 `The server is fired up and ready to roll!` 出现的时间点，体会「端口已开 ≠ 服务已就绪」。
4. **画时序图**：综合 4.4.4 实践的 `[TRACE]` 时间戳，画一张从 `sglang serve` 回车到 `ready to roll` 的时序图，横轴为时间，标注「子进程拉起 / 模型加载 / 全局状态 / uvicorn 启动 / warmup」五个区段。

完成这个任务后，你应该能在不看源码的情况下，口述 `sglang serve` 从命令行到 HTTP 就绪的每一步。

## 6. 本讲小结

- `sglang serve` 的启动链是一条清晰的编排链：`cli/main.py` 分发 → `cli/serve.py: serve()` → `load_plugins()` → `prepare_server_args()` → `run_server()` → `http_server.launch_server()` → `_setup_and_run_http_server()` → `uvicorn.run()` → `lifespan` warmup。
- `prepare_server_args` 用 argparse + 可选 `--config` 合并，把命令行字符串解析成 `ServerArgs`，并在 `__post_init__` 里完成校验与派生——参数错误在这一步就被拦截。
- `load_plugins` 是基于 setuptools `entry_points` 的统一插件框架，幂等、早调用，支持 `SGLANG_PLUGINS` 白名单与 `SGLANG_PLATFORM` 自动排除，是「外部代码介入运行时」的总开关。
- `run_server` 是一个分发器，按 `encoder_only` / `smg_grpc_mode` / `use_ray` / 默认把服务分到四种形态；默认形态走 `http_server.launch_server`。
- `http_server.launch_server` 通过 `Engine._launch_subprocesses` 按「Scheduler 子进程 → Detokenizer 子进程 → 主进程 TokenizerManager」顺序组装引擎。
- `_setup_and_run_http_server` 把组装好的对象存进全局状态、装中间件，然后 `uvicorn.run(app)`；FastAPI 路由是在模块导入时就绑好的，这个函数只负责「接线 + 启动」。
- 真正就绪要等 `lifespan` 里后台线程跑完 warmup（轮询 `/model_info` + 发一条真实推理请求），打印 `The server is fired up and ready to roll!`——这解释了「端口已开但首请求偏慢」。

## 7. 下一步学习建议

- **进程拓扑细节**：本讲只讲了「谁拉起谁」，下一讲 **u3-l2（进程拓扑：三大管理器与 IPC 概览）** 会展开 TokenizerManager / Scheduler / DetokenizerManager 三者的进程归属、职责与 ZMQ 消息流，建议紧接着读。
- **ServerArgs 全貌**：本讲把 `ServerArgs` 当黑盒用，**u3-l3（ServerArgs 配置体系）** 会讲它的字段组织、`arg_groups` 分组与 `--config` 合并的完整规则。
- **请求生命周期**：搞懂启动后，自然进入第 4 单元 **u4-l1（io_struct 与进程间通信）**，看一条请求如何在刚拉起的这几个进程间流转。
- **源码延伸阅读**：想看 warmup 的完整预热请求构造，读 [http_server.py:2117-2266](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/entrypoints/http_server.py#L2117-L2266)；想看子进程拉起的全部细节，读 [engine.py](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/entrypoints/engine.py) 中 `_launch_subprocesses` 及其调用的 `_launch_scheduler_processes` / `_launch_detokenizer_subprocesses`。
