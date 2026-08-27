# Python 前端主流程:从 CLI 到 make_engine

## 1. 本讲目标

学完本讲,你应该能够:

1. 完整说出 `python -m dynamo.frontend` 从敲下回车到 HTTP 服务可用的每一步初始化链,以及每一步落在哪个文件、哪一行附近。
2. 解释 `FrontendConfig` 的构造机制:CLI 标志、`DYN_*` 环境变量、多个 ArgGroup、类注解默认值是如何汇合成一个配置对象的,`validate()` 在其中做了什么(包括副作用)。
3. 理解 `make_engine` 这条 PyO3 边界:`EntrypointArgs(EngineType.Dynamic, **kwargs)` 如何把 Python 侧的配置与 `chat_engine_factory` 回调一起交给 Rust,由 Rust 在发现模型时回调 Python processor。

本讲是 u4-l1(引擎装配)的 Python 驾驶员视角:u4-l1 讲的是 Rust 装配车间的内部结构,本讲讲的是「谁把零件单递进车间」。

## 2. 前置知识

本讲默认你已读过前置讲义,这里只做最小复习:

- **`python -m 包名` 的语义**:Python 会执行该包目录下的 `__main__.py`。所以 `python -m dynamo.frontend` 的入口是 `components/src/dynamo/frontend/__main__.py`。
- **argparse 两遍解析**:主解析器先 `parse_known_args()` 收下 frontend 自己认识的标志,把不认识的留在 `unknown` 里;之后按 `--dyn-chat-processor` 的取值,把 `unknown` 再交给 vLLM 或 SGLang 的原生解析器(见 4.2)。这就是为什么 frontend 命令行能「混入」vLLM 的 `--tool-call-parser` 等私有标志。
- **uvloop**:`asyncio` 事件循环的一个 C 实现,性能更好。frontend 用 `uvloop.run()` 启动主协程,之后把这个 loop 交给 `DistributedRuntime`(u2-l1 讲过:PyO3 类内部持有 Rust 原生结构体,异步回调需要知道往哪个 loop 里投递)。
- **EntrypointArgs / EngineConfig / make_engine(u4-l1 已建立)**:`EngineType.Dynamic` 表示「引擎在远端 worker,本地惰性装配」;`make_engine` 是 PyO3 暴露的异步函数,返回 Rust 侧的 `EngineConfig`;有 `chat_engine_factory` 时 chat 管线走 `build_preprocessed_pipeline`,Python 的预处理/后处理逻辑由此注入。
- **FrontendConfig 不是 dataclass**:它是一个普通类,字段只有类型注解(部分带默认值),真正的实例化和填充实现在 `ConfigBase.from_cli_args()` 里(见 4.2.3)。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `components/src/dynamo/frontend/__main__.py` | `python -m dynamo.frontend` 的物理入口,仅 3 行有效代码 |
| `components/src/dynamo/frontend/main.py` | 主流程:参数解析、运行时构建、信号处理、kwargs 组装、`make_engine`、`run_input` |
| `components/src/dynamo/frontend/frontend_args.py` | `FrontendConfig`(配置对象)与 `FrontendArgGroup`(参数注册)的定义,含 `validate()` |
| `components/src/dynamo/common/configuration/config_base.py` | `ConfigBase.from_cli_args`:argparse Namespace → 配置对象的通用算法 |
| `components/src/dynamo/common/configuration/utils.py` | `add_argument` / `env_or_default`:「CLI 标志 + 环境变量默认值」的统一封装 |
| `components/src/dynamo/common/configuration/groups/router_args.py` | `RouterArgGroup` 与 `build_router_config`:路由参数组及其到 Rust `RouterConfig` 的映射 |
| `components/src/dynamo/frontend/vllm_processor.py` | `EngineFactory.chat_engine_factory`:被 Rust 回调的 Python processor 工厂(本讲只看签名) |
| `lib/bindings/python/src/dynamo/_core.pyi` | PyO3 扩展 `_core` 的类型存根:`make_engine` / `run_input` / `EntrypointArgs` / `EngineType` 的 Python 侧签名 |
| `lib/bindings/python/src/dynamo/llm/__init__.py` | `dynamo.llm` 再导出层:把 `_core` 的符号分发给 Python |

## 4. 核心概念与源码讲解

本讲拆三个最小模块:**main.py 主流程**、**FrontendConfig 参数体系**、**make_engine 注入链**。

### 4.1 启动链总览:`__main__.py` → `main()` → `async_main()`

#### 4.1.1 概念说明

frontend 是一个「进程」。研究任何进程的第一问都是:main 在哪、启动前做了什么进程级准备。Dynamo frontend 的启动分三层:

1. **`__main__.py`**:只是把 `python -m dynamo.frontend` 这个命令翻译成一次函数调用。
2. **同步层 `main()`**:做两件必须在 asyncio 之前完成的事——抬高文件描述符上限、启动事件循环。
3. **异步层 `async_main()`**:全部业务初始化,从解析参数到 `run_input`。

这个分层解释了一个常见困惑:「为什么日志和配置代码写在模块顶层,`DistributedRuntime` 却在一个 `async def` 里?」因为前者是 import 时的同步初始化,后者依赖已运行的 loop。

#### 4.1.2 核心流程

```text
python -m dynamo.frontend [flags]
└─ __main__.py: main()
    └─ main(): _raise_fd_limit() + uvloop.run(async_main())
        └─ async_main():
             1. os.environ.pop("DYN_SYSTEM_PORT")        # 防止抢占 worker 的指标端口
             2. parse_args() → (FrontendConfig, vllm_flags, sglang_flags)
             3. dump_config(...)                          # 可选:把最终配置转储成文件
             4. _export_transport_tls_env(config)         # 把 TLS 标志翻成 Rust 读的环境变量
             5. runtime = DistributedRuntime(loop, discovery_backend, request_plane, event_plane)
             6. 注册 SIGTERM/SIGINT → graceful_shutdown(runtime)
             7. build_router_config(config) → RouterConfig(永不返回 None)
             8. 组装 kwargs 字典(约 20 项显式键)
             9. 按 chat_processor 注入 kwargs["chat_engine_factory"]
            10. e = EntrypointArgs(EngineType.Dynamic, **kwargs)
            11. engine = await make_engine(runtime, e)
            12. load_frontend_route_extensions(...)        # 可选扩展路由
            13. await run_input(runtime, "http"|"grpc"|"text", engine, ...)
```

其中第 5 步之后控制权就基本进入 Rust(u4-l1 的装配车间),Python 侧剩下的角色是「配置提供者 + 回调实现者」。

#### 4.1.3 源码精读

**物理入口**——import `main` 并调用,没有任何逻辑:

[__main__.py:L4-L7](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/__main__.py#L4-L7)
把 `python -m dynamo.frontend` 转发到 `dynamo.frontend.main` 模块的 `main()` 函数。

**同步入口**:抬高 fd 上限,再用 uvloop 跑主协程:

[main.py:L502-L505](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/main.py#L502-L505)
`main()` 先调 `_raise_fd_limit()` 再 `uvloop.run(async_main())`。

**为什么要抬 fd 上限**——这是一段很好的「注释驱动源码阅读」样本:

[main.py:L55-L95](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/main.py#L55-L95)
注释解释了动机:高并发下 TCP accept 循环可能耗尽默认 1024 的软 `RLIMIT_NOFILE`,导致 `accept()` 返回 `EMFILE`。该函数尽力把软上限抬到 `DYN_FRONTEND_FD_LIMIT_TARGET`(默认 8192,环境变量可覆盖,取非正值即禁用),且以硬上限为界、失败静默(best-effort 加固)。

**信号处理与优雅关停**:

[main.py:L401-L405](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/main.py#L401-L405)
给 `SIGTERM`/`SIGINT` 各挂一个 handler,收到信号就 `create_task(graceful_shutdown(runtime))`。注意 handler 里不是直接调用而是创建任务——signal handler 的执行上下文不适合做耗时操作,抛回事件循环才是 asyncio 的正确姿势。`graceful_shutdown` 只做一件事:`runtime.shutdown()`([main.py:L493-L499](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/main.py#L493-L499)),真正的分阶段停机由 Rust 侧完成(u3-l1 讲过三阶段关停)。

**清掉 `DYN_SYSTEM_PORT`**:

[main.py:L361-L368](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/main.py#L361-L368)
聚合启动脚本先起 frontend 再起 worker,若 frontend 继承了为 worker 设置的 `DYN_SYSTEM_PORT`,会先绑定该端口造成冲突,因此主动 pop;若之后环境里仍检测到该变量,再打一条醒目告警([main.py:L379-L387](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/main.py#L379-L387))。这个细节是「环境变量是进程间隐式契约」的活教材。

#### 4.1.4 代码实践

1. **实践目标**:不启动服务,只验证启动链前三层的行为。
2. **操作步骤**(在 u1-l4 构建好的 venv 中):
   - `python -m dynamo.frontend --version` —— 会打印 `Dynamo Frontend <版本>`,版本号来自 `frontend_args.py` 里注册的 `--version` 动作。
   - `python -m dynamo.frontend --help` —— 观察帮助输出被分成了哪些参数组(至少有 `Dynamo Frontend Options`、router、kv-router、aic 几组)。
3. **需要观察的现象**:`--help` 中每个参数的 help 文本末尾都标注了对应环境变量和默认值。
4. **预期结果**:你能从 `--help` 输出里找到本讲后续引用的 `--http-port`、`--discovery-backend`、`--dyn-chat-processor` 等标志,并看到它们的环境变量名。
5. 运行输出与具体分组文案随版本变化,以本地 `--help` 为准(待本地验证)。

#### 4.1.5 小练习与答案

**练习 1**:为什么 `_raise_fd_limit()` 放在 `main()` 里、`uvloop.run()` 之前,而不是放进 `async_main()` 开头?

**答案**:技术上放 `async_main()` 开头也来得及(loop 还没跑起来),但语义上它是「进程级资源准备」,与 asyncio 无关,和日志初始化同属同步层;且 `main()` 是唯一入口,放这里保证无论协程怎么改,fd 上限总是先于任何网络活动被抬高。

**练习 2**:信号 handler 里为什么是 `asyncio.create_task(...)` 而不是直接 `runtime.shutdown()`?

**答案**:`loop.add_signal_handler` 的回调运行在事件循环上下文里,但回调本身应尽量短、不可阻塞;`shutdown` 是一次跨 PyO3 的关停流程,应作为任务调度回事件循环执行,避免在 handler 里同步等待。

### 4.2 FrontendConfig:CLI 与环境变量的汇合点

#### 4.2.1 概念说明

frontend 有上百个配置项,但定义方式高度统一,靠的是四个约定:

1. **ArgGroup 分组**:`ArgGroup` 是一个抽象基类,每个子类在自己的 `add_arguments(parser)` 里注册自己领域的参数(且要求除修改 parser 外无副作用)。`FrontendArgGroup` 注册 frontend 专属参数,并**嵌套调用** `RouterArgGroup`、`KvRouterArgGroup`、`AicPerfArgGroup` —— 这就是 router 独立进程与 frontend 能共享同一套路由参数的原因(u6-l1 会再遇到它们)。
2. **`add_argument` 统一封装**:仓库自带的工具函数把「CLI 标志 + 环境变量默认值 + help 文案拼接」固化成一个调用,因此每个参数天然支持 `DYN_*` 环境变量覆盖。
3. **多继承配置基类**:`FrontendConfig` 同时继承 `RouterConfigBase`、`KvRouterConfigBase`、`AicPerfConfigBase`——它「是」一个路由配置、也「是」一个 KV 路由配置。
4. **`from_cli_args` 两步填充**:argparse Namespace 先铺满实例,再由类注解兜底补默认值。

理解了这四条,你就能在几十秒内给 frontend 加一个新参数——这正是本讲综合实践的任务。

#### 4.2.2 核心流程

```text
FrontendArgGroup().add_arguments(parser)      # ① 注册所有标志(含嵌套组)
args, unknown = parser.parse_known_args()     # ② 主解析,unknown 留给 vLLM/SGLang
config = FrontendConfig.from_cli_args(args)   # ③ Namespace → FrontendConfig
    ├─ 第一步: vars(args) 逐项 setattr 到实例
    └─ 第二步: 沿 MRO 扫注解,把「类里有默认值且实例未设置」的字段补上
config.validate()                              # ④ 交叉校验 + 少量副作用
按 chat_processor 二次解析 unknown             # ⑤ vllm_flags / sglang_flags
```

**环境变量的优先级**藏在 ② 之前的默认值计算里:`add_argument` 在注册时就把 `default` 换成了 `env_or_default(env_var, default)` 的结果,即「环境变量 > 代码默认值」;而显式给出的 CLI 标志又永远覆盖默认值。所以最终优先级是:**CLI 标志 > 环境变量 > 代码默认值**。

#### 4.2.3 源码精读

**配置对象本体**——注意它只是注解,不是 dataclass:

[frontend_args.py:L55-L105](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/frontend_args.py#L55-L105)
`FrontendConfig` 继承三个配置基类,声明 frontend 全部自有字段:`http_host`/`http_port`、TLS 一族、`namespace`、`migration_limit`、`discovery_backend`/`request_plane`/`event_plane`、`chat_processor`、`frontend_route_extensions` 等。字段只写类型注解(部分带默认值),实例化算法在 `ConfigBase`。

**两步填充算法**——理解它才能安全加字段:

[config_base.py:L10-L37](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/common/configuration/config_base.py#L10-L37)
`from_cli_args` 先用 `vars(args)` 把 argparse 给的每个值(包括没在命令行出现、由 parser 默认值填充的)写到实例上;再沿 `__mro__` 反向扫描各基类注解,**只在实例上尚未设置**时才把类级默认值物化下来。注释里的 "IMPORTANT" 强调判断的是实例字典而非类字典——这决定了「parser 默认值」与「类注解默认值」冲突时前者胜出。

**参数注册的统一封装**:

[utils.py:L102-L155](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/common/configuration/utils.py#L102-L155)
`add_argument` 接收 `flag_name`/`env_var`/`default`/`help`,内部先用 `env_or_default` 把默认值替换为「环境变量优先」的结果([utils.py:L44-L85](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/common/configuration/utils.py#L44-L85),按默认值类型自动转换 bool/int/float/list),再把环境变量名和默认值拼进 help 文案,最后调用原生 `parser.add_argument`。

**ArgGroup 抽象**:

[arg_group.py:L9-L27](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/common/configuration/arg_group.py#L9-L27)
`ArgGroup` 只有一个抽象方法 `add_arguments(parser)`,文档要求它「除 parser 变异外无副作用、不依赖运行时状态或其他组」——这是参数组可以任意嵌套组合的前提。

**FrontendArgGroup 的注册顺序**——先自有参数,再嵌套共享组:

[frontend_args.py:L219-L237](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/frontend_args.py#L219-L237)
`FrontendArgGroup.add_arguments` 开头注册 `--version` 和 `-i/--interactive`(布尔可否定动作,`DYN_INTERACTIVE` 环境变量默认)。

[frontend_args.py:L380-L387](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/frontend_args.py#L380-L387)
随后嵌套调用三个共享组:`RouterArgGroup(default_router_mode="round-robin", include_frontend_only=True)`、`KvRouterArgGroup()`、`AicPerfArgGroup()`。frontend 的默认路由模式是 round-robin,且它拿到了 router 独立进程没有的「frontend 专属」路由参数。

**三个平面开关**:

[frontend_args.py:L496-L528](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/frontend_args.py#L496-L528)
`--discovery-backend`(kubernetes/etcd/file/mem,默认 etcd)、`--request-plane`(nats/tcp,默认 tcp)、`--event-plane`(nats/zmq,默认未设时由 Rust 侧落到 zmq)——u3-l1 讲过的三正交开关在 CLI 上的样子。

**validate() 的校验与副作用**:

[frontend_args.py:L107-L210](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/frontend_args.py#L107-L210)
`validate()` 不是纯函数:开头三行([frontend_args.py:L108-L110](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/frontend_args.py#L108-L110))在 `load_aware` 为真时直接把 `router_mode` 改写成 `"kv"` 并应用预设——「负载感知」本质上是 kv 模式的一个别名;其余部分是交叉校验:TLS 证书与密钥必须成对(XOR 判断)、迁移上限范围、tokenizer 后端枚举、`--router-prefill-load-model=aic` 对 kv 模式/dynamo processor/必填 aic 参数的一组约束、`--serve-indexer` 与 `--use-remote-indexer` 互斥、conditional-disagg 系列阈值范围等。

**主解析与二次解析**:

[main.py:L250-L317](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/main.py#L250-L317)
`parse_args()` 先 `FrontendArgGroup().add_arguments(parser)` 再 `parse_known_args()`;接着 `FrontendConfig.from_cli_args(args)` + `config.validate()`;然后按 `config.chat_processor` 分三路:vllm 路用 vLLM 的 `FlexibleArgumentParser` 解析 `unknown`(无 GPU 主机上还会把平台强制成 `CpuPlatform` 以避免设备探测崩溃,因为 frontend 只借 vLLM 的解析器、从不建引擎);sglang 路只收 `--tool-call-parser`/`--reasoning-parser`/`--chat-template` 三个标志;默认 dynamo 路若有剩余 unknown 参数则直接报错退出。

**结果可视化**——`--dump-config-to`:

[main.py:L368-L369](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/main.py#L368-L369)
`parse_args()` 返回后立刻 `dump_config(config.dump_config_to, config)`;序列化器经 [frontend_args.py:L213-L216](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/frontend_args.py#L213-L216) 注册,直接输出 `config.__dict__`——这也是实践环节验证新字段的抓手。

代表性参数与其去向一览(均已核对源码):

| CLI 标志 | 环境变量 | 默认值 | 最终去向 |
|---|---|---|---|
| `--http-port` | `DYN_HTTP_PORT` | 8000 | `kwargs["http_port"]` → `EntrypointArgs` |
| `--discovery-backend` | `DYN_DISCOVERY_BACKEND` | etcd | `DistributedRuntime` 构造参数(第 3 个) |
| `--request-plane` | `DYN_REQUEST_PLANE` | tcp | `DistributedRuntime` 构造参数 |
| `--event-plane` | `DYN_EVENT_PLANE` | 未设(Rust 默认 zmq) | `DistributedRuntime` 构造参数 |
| `--router-mode` | `DYN_ROUTER_MODE` | round-robin(frontend 默认) | `build_router_config` → `kwargs["router_config"]` |
| `--kv-cache-block-size` | `DYN_KV_CACHE_BLOCK_SIZE` | None | `kwargs["kv_cache_block_size"]` |
| `--dyn-chat-processor` | `DYN_CHAT_PROCESSOR` | dynamo | 决定是否注入 `chat_engine_factory` |
| `--model-name` | `DYN_MODEL_NAME` | None | `kwargs["model_name"]`(仅设置时) |

#### 4.2.4 代码实践

1. **实践目标**:用 `--dump-config-to` 亲眼看到「CLI/环境变量 → FrontendConfig 实例」的合并结果。
2. **操作步骤**:
   ```bash
   python -m dynamo.frontend --http-port 9000 --discovery-backend file \
       --router-mode kv --dump-config-to /tmp/frontend_config.json
   # 进程会继续启动;另开终端观察 /tmp/frontend_config.json,然后 Ctrl-C 退出
   DYN_HTTP_PORT=9111 python -m dynamo.frontend --dump-config-to /tmp/frontend_config2.json
   ```
3. **需要观察的现象**:JSON 里 `http_port` 的值——第一种应为 9000(CLI),第二种应为 9111(环境变量);同时 `discovery_backend`、`router_mode` 与命令行一致。
4. **预期结果**:两份 JSON 除命令行差异外结构相同,字段集合与 `FrontendConfig` 注解一致(含从三个基类继承来的路由字段)。
5. 文件的确切字段集合随版本演进,以本地输出为准(待本地验证)。

#### 4.2.5 小练习与答案

**练习 1**:如果不小心在 `FrontendConfig` 里写了 `foo: str` 但忘记在 `FrontendArgGroup.add_arguments` 注册 `--foo`,`from_cli_args` 之后访问 `config.foo` 会发生什么?

**答案**:会抛 `AttributeError`。因为类注解没有默认值时,`from_cli_args` 第二步只在「类 `__dict__` 里存在同名默认值」时才物化字段;argparse 又没提供 `foo`,实例字典里就没有它。所以加参数的两处(注册标志 + 声明字段)缺一不可。

**练习 2**:优先级「CLI > 环境变量 > 代码默认值」是在哪一行代码实现的?

**答案**:在 [utils.py:L136](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/common/configuration/utils.py#L136) ——`default_with_env = env_or_default(env_var, default, ...)`:注册参数那一刻,环境变量就已战胜代码默认值成为 parser 默认值;而 argparse 保证显式 CLI 值永远覆盖 parser 默认值。

**练习 3**:为什么 `--tls-cert-path` 与 `--tls-key-path` 的校验用异或(XOR)而不是 `and`?

**答案**:要拒绝的是「只给其一」的半配置状态:`bool(a) ^ bool(b)` 为真当且仅当恰好一个为真;`and` 只能抓「两个都错」,抓不住「只给证书不给密钥」。

### 4.3 make_engine:把 Python processor 注入 Rust 引擎

#### 4.3.1 概念说明

frontend 的本质工作,是把上一步得到的 `FrontendConfig` 翻译成 Rust 能懂的 `EntrypointArgs`,再调用 `make_engine` 完成装配。这条 PyO3 边界上有三个关键角色:

- **`EntrypointArgs`**:定义在 Rust 侧的 PyO3 类(u2-l2 讲过符号三步定位法:`dynamo.llm/__init__.py` 的 import 行 → `lib.rs` 注册行 → Rust 定义文件;本讲只需看 `.pyi` 签名)。它是一个**显式白名单**构造器——kwargs 字典里出现的每个键都必须是它 `__init__` 签名里的具名参数,多一个就 `TypeError`。
- **`EngineType.Dynamic`**:u4-l1 讲过三种引擎形态之一,表示「引擎逻辑在远端 worker,本地按需装配路由与网络组件」;`chat_engine_factory` 回调正是 Dynamic 形态的配套设施。
- **`chat_engine_factory`**:一个 Python 可调用对象,被塞进 `EntrypointArgs`,由 **Rust 在发现某个模型/worker 时回调**,用来现场构造 Python 侧的预处理/后处理引擎。这就是「引擎逻辑留 Python、路由与发现归 Rust」分工的物理实现。

另外要理解一个设计纪律(frontend 目录的 AGENTS.md 明文规定):**`FrontendConfig` 解析之后就是 frontend 配置的唯一事实来源,不允许把解析出的值写回 `os.environ` 让 Rust 再读一遍**——那会形成「Python → env → Rust」的循环契约,CLI 覆盖可能与 Rust 行为分叉。跨语言传值应走显式 PyO3 参数(即 `EntrypointArgs` 的具名参数)。代码里现存两处刻意保留并注释了理由的例外:TLS 环境变量导出(见 4.3.3)和 `DYN_ROUTER_MIN_INITIAL_WORKERS` 回写(为与后端 worker 共享同一语义)。给 frontend 加新配置时,应走 kwargs 白名单而不是环境变量。

#### 4.3.2 核心流程

```text
config (FrontendConfig)
  │  ① 显式组装 kwargs(http/router/migration/tokenizer/...约 20 键)
  │  ② 可选键按条件追加(model_name/model_path/tls_*/namespace*/aic_perf_config)
  │  ③ chat_processor ∈ {vllm, sglang} 时:
  │       setup_engine_factory(config, flags).chat_engine_factory
  │       → kwargs["chat_engine_factory"]   # 一个 Python 可调用对象
  ▼
EntrypointArgs(EngineType.Dynamic, **kwargs)     # PyO3 类,白名单校验发生在此
  ▼
engine = await make_engine(runtime, e)           # 进入 Rust 装配车间(u4-l1)
  │   ├─ 解析 EngineConfig → Dynamic 变体
  │   ├─ 等 watcher 发现 worker → 组网络组件/路由
  │   └─ 发现模型时回调 chat_engine_factory(instance_id, mdc, routed_engine)
  │        → 返回 PythonAsyncEngine → 挂进 build_preprocessed_pipeline
  ▼
await run_input(runtime, "http"|"grpc"|"text", engine, extensions)
```

回调注入的意义:`make_engine` 返回时 worker 可能尚未上线,所以 factory 不是「构造引擎」而是「登记如何构造引擎」;真正的调用发生在 Rust 侧模型发现事件之后。

#### 4.3.3 源码精读

**运行时构建与 TLS 前置导出**:

[main.py:L389-L399](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/main.py#L389-L399)
拿到当前 loop,先调 `_export_transport_tls_env(config)` 再构造 `DistributedRuntime(loop, config.discovery_backend, config.request_plane, event_plane=config.event_plane)`。

[main.py:L320-L352](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/main.py#L320-L352)
docstring 解释了顺序的刚性:`DistributedRuntime` 构造时会**立即**连接 NATS,晚设的环境变量会被静默忽略;TCP 请求面虽然惰性拨号,也一并前置保持一致。这是上述「不写回环境变量」纪律的注释在案例外——它服务于一个泛型构造函数的 env 回退路径,而非新增配置的推荐做法。

**路由配置的桥接**:

[main.py:L407-L411](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/main.py#L407-L411)
把 `min_initial_workers` 写进 `DYN_ROUTER_MIN_INITIAL_WORKERS` 与后端共享(注释说明这是「让 worker 用同一套标志和语义构建它宣告的配置」),然后 `build_router_config(config)` 生成 Rust 的 `RouterConfig`。

[router_args.py:L383-L405](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/common/configuration/groups/router_args.py#L383-L405)
`build_router_config` 的契约:配置没有 `router_mode` 时返回 `None`(worker 就不宣告路由配置、继承 frontend 的);frontend 自己传进来时永远有 mode,所以永不返回 `None`。内部把字符串 mode 经 `ROUTER_MODE_MAP` 映射成 Rust 枚举(u6-l1 承接)。

**kwargs 组装——白名单的内容**:

[main.py:L413-L448](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/main.py#L413-L448)
无条件键:`http_host`、`http_port`、`kv_cache_block_size`、`router_config`、`migration_limit`、`metrics_prefix`、anthropic 与流式 dispatch 开关、`reasoning_field_name`、`tokenizer_backend`、`tokenizer_fallback`;条件键:`migration_max_seq_len`、`model_name`、`model_path`、`tls_cert_path`/`tls_key_path`、`namespace`、`namespace_prefix`、gRPC 模式下的 `http_metrics_port`。每个键都对应 `EntrypointArgs.__init__` 的一个具名参数。

**processor 工厂注入**:

[main.py:L451-L466](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/main.py#L451-L466)
`chat_processor == "vllm"` 时取 `setup_engine_factory(config, vllm_flags).chat_engine_factory`(`setup_engine_factory` 定义在 [main.py:L98-L108](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/main.py#L98-L108),惰性 import `vllm_processor.EngineFactory`,避免无 vLLM 环境崩溃);`"sglang"` 路对称([main.py:L111-L131](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/main.py#L111-L131),把 sglang_flags 里的三个解析器名传给工厂);默认 `"dynamo"` 路不注入任何 factory,chat 管线走全 Rust 预处理。aic 分支把性能模型配置也塞进 kwargs。

**跨边界的一跃**:

[main.py:L468-L469](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/main.py#L468-L469)
`e = EntrypointArgs(EngineType.Dynamic, **kwargs)` 然后 `engine = await make_engine(runtime, e)`——Python 侧主流程的全部产出就是这两行交出去的东西。

**PyO3 侧签名**(类型存根,即 Python 视角的契约):

[_core.pyi:L3096-L3101](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/bindings/python/src/dynamo/_core.pyi#L3096-L3101)
`EngineType` 三个类属性:Echo / Dynamic / Mocker——对应 u4-l1 的 InProcessText / Dynamic / InProcessTokens 三种形态。

[_core.pyi:L3103-L3178](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/bindings/python/src/dynamo/_core.pyi#L3103-L3178)
`EntrypointArgs.__init__` 的完整参数表——对照 [main.py:L418-L448](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/main.py#L418-L448) 逐键核对,其中 `chat_engine_factory: Optional[Callable]`(docstring:"Optional Python chat completions engine factory callback")就是回调的注入点。

[_core.pyi:L2343-L2349](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/bindings/python/src/dynamo/_core.pyi#L2343-L2349)
`make_engine(distributed_runtime, args) -> EngineConfig`,异步;`EngineConfig` 是不透明句柄,后续只被 `run_input` 消费。

**被 Rust 回调的 Python 工厂**:

[vllm_processor.py:L1021-L1031](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/vllm_processor.py#L1021-L1031)
`EngineFactory.chat_engine_factory` 的签名:`(instance_id: ModelCardInstanceId, mdc: ModelDeploymentCard, routed_engine: RoutedEngine) -> PythonAsyncEngine`,docstring 直说 "Called by Rust when a model is discovered"——Rust 送上模型卡,Python 返回引擎,方向与直觉相反(Rust 调 Python)。其构造见 [vllm_processor.py:L1001-L1011](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/vllm_processor.py#L1001-L1011)。细节留给 u5-l3。

**再导出层**——这些符号从哪来:

[llm/__init__.py:L11-L51](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/bindings/python/src/dynamo/llm/__init__.py#L11-L51)
`EngineType`、`EntrypointArgs`、`FrontendRoute`、`make_engine`、`run_input` 全部 `from dynamo._core import ...`——u2-l2 的符号三步定位法在此实例化。

**输入源选择与收尾**:

[main.py:L482-L490](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/main.py#L482-L490)
按配置三选一:`--interactive` 走 `run_input(runtime, "text", engine)`(终端聊天),`--kserve-grpc-server` 走 grpc,默认走 http 并附带扩展路由。`run_input` 的语义见 [_core.pyi:L2413-L2424](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/bindings/python/src/dynamo/_core.pyi#L2413-L2424):"Start an engine, connect it to an input, and run until stopped"——此后 Python 主协程只是等待,HTTP 服务(u4-l2 的 HttpService)在 Rust 侧运行。

**扩展路由(选读)**:

[main.py:L472-L480](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/main.py#L472-L480)
`--frontend-route-extension` 只允许 HTTP 模式(fail fast 在 import 第三方代码之前);解析逻辑 [main.py:L170-L212](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/main.py#L170-L212) 支持入口点名或 `module:function` 直连路径,类型规整在 [main.py:L142-L167](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/main.py#L142-L167)。`FrontendRoute` 只支持静态 GET 路径([_core.pyi:L2383-L2398](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/bindings/python/src/dynamo/_core.pyi#L2383-L2398))。

#### 4.3.4 代码实践

1. **实践目标**:追踪一个 kwarg 的完整旅程,验证「FrontendConfig → kwargs → EntrypointArgs」链路。
2. **操作步骤**(源码阅读型,无需运行):
   - 任选 `enable_anthropic_api`:在 [frontend_args.py:L529-L538](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/frontend_args.py#L529-L538) 找到它的注册(布尔可否定标志);在 [main.py:L418-L432](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/main.py#L418-L432) 找到它进 kwargs;在 [_core.pyi:L3134-L3141](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/bindings/python/src/dynamo/_core.pyi#L3134-L3141) 找到它作为关键字参数的声明。
   - 再选一个你没在 kwargs 里见过的配置(如 `preprocess_workers`),确认它**没有**进 `EntrypointArgs`——查 [vllm_processor.py:L1001-L1011](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/vllm_processor.py#L1001-L1011) 它去了哪里(提示:它被 EngineFactory 持有,服务于 Python 侧多进程预处理,不跨 PyO3 边界)。
3. **需要观察的现象**:同一份 `FrontendConfig` 里的字段分裂成两股——一股过 `EntrypointArgs` 去 Rust,一股留在 Python processor 手里。
4. **预期结果**:能画出两条箭头图:`enable_anthropic_api → kwargs → EntrypointArgs → Rust HttpService`;`preprocess_workers → EngineFactory.config → Python 预处理进程池`。
5. `preprocess_workers` 的具体消费代码可能在版本间移动,以 Grep `preprocess_workers` 的实际命中为准(待本地验证)。

#### 4.3.5 小练习与答案

**练习 1**:如果给 `EntrypointArgs` 传一个它签名里没有的 kwarg(比如把 `preprocess_workers` 误加进 kwargs),会发生什么?为什么这反而是件好事?

**答案**:Python 在调用 PyO3 构造器时就抛 `TypeError`(unexpected keyword argument)。好处是白名单把「跨语言契约的变更」变成显式失败:想新增一个跨边界配置,必须同时改 Rust 侧 `EntrypointArgs` 定义与 Python 侧 kwargs,不会静默丢失。

**练习 2**:为什么 `chat_engine_factory` 不在 `make_engine` 时立即调用,而要等 Rust 回调?

**答案**:Dynamic 引擎的本地组件要等 watcher 发现 worker 之后才能装配(u4-l1:网络组件惰性生成);`make_engine` 返回时可能还没有任何 worker。factory 是「登记构造方法」,Rust 在模型发现事件发生后带着 `ModelDeploymentCard` 回调它,Python 才知道该为哪个模型、哪个引擎槽位构造 processor。

**练习 3**:frontend 目录的 AGENTS.md 禁止「把解析后的 FrontendConfig 值写回 os.environ 给 Rust 再读」,但 `_export_transport_tls_env` 恰恰在写环境变量。这两者矛盾吗?

**答案**:不矛盾,是「历史回退路径的显式豁免」。该函数的 docstring 写明了顺序约束(`DistributedRuntime` 构造时急切连 NATS,事后设环境变量无效),它服务于 Rust 泛型构造函数的 env 回退;规范要求的是**新增** frontend 配置必须走显式 PyO3 参数,避免形成 Python→env→Rust 的循环契约。判断标准:这是 env 回退的兼容层,还是新的传值通道。

## 5. 综合实践

**任务:给 frontend 增加 `--banner-text` 参数,走通 `frontend_args.py → FrontendConfig → main.py` 完整参数传递,并在服务启动时打印。**

这个任务浓缩了本讲全部三个模块:参数注册(M2)、配置物化(M2)、主流程消费(M1/M3)。全程不改任何 Rust 代码——因为 banner 是 frontend 私有配置,不需要过 `EntrypointArgs` 白名单(这正是 4.3 配置边界纪律的应用)。

**第一步:在 `frontend_args.py` 声明字段**

在 `FrontendConfig` 字段区(建议放在 [frontend_args.py:L82](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/frontend_args.py#L82) 的 `metrics_prefix: Optional[str] = None` 附近)加:

```python
banner_text: Optional[str] = None   # 示例代码:本实践新增
```

**第二步:在 `FrontendArgGroup.add_arguments` 注册标志**

在 `--metrics-prefix` 的注册(约 [frontend_args.py:L444-L453](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/frontend_args.py#L444-L453))之后加,完全照抄邻居的形态:

```python
add_argument(                                          # 示例代码:本实践新增
    g,
    flag_name="--banner-text",
    env_var="DYN_BANNER_TEXT",
    default=None,
    help="Text printed (INFO log) when the frontend finishes startup.",
)
```

由于 `add_argument` 的默认 `arg_type=str` 且 `dest` 由 flag 推导,这一行同时给了你 CLI 标志、`DYN_BANNER_TEXT` 环境变量覆盖和 `config.banner_text` 属性。

**第三步:在 `main.py` 的 `async_main` 里消费**

在 [main.py:L482](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/main.py#L482) 的 `try:` 块之前(`run_input` 之前)加:

```python
if config.banner_text:                                 # 示例代码:本实践新增
    logger.info(f"=== {config.banner_text} ===")
```

**第四步:验证三层各一次**

```bash
# 层 1:参数注册生效,环境变量出现在 help 里
python -m dynamo.frontend --help | grep -A2 banner-text

# 层 2:配置物化生效,值与环境变量覆盖都正确
python -m dynamo.frontend --banner-text "hello dynamo" \
    --discovery-backend file --dump-config-to /tmp/bcfg.json
# (Ctrl-C 退出后)检查 /tmp/bcfg.json 中 "banner_text": "hello dynamo"
DYN_BANNER_TEXT="from env" python -m dynamo.frontend --dump-config-to /tmp/bcfg2.json
# 检查 "banner_text": "from env"

# 层 3:主流程消费生效,日志真的打出来
python -m dynamo.frontend --banner-text "Serving NOW" \
    --http-port 8000 --discovery-backend file
# 观察日志中出现 "=== Serving NOW ==="(在 run_input 之前的最后一批 Python 日志)
```

**预期结果**:三步验证分别证明注册、物化、消费三环;无 worker 时 frontend 仍能启动 HTTP 服务(u1-l2 已验证),banner 会打印,只是 `/v1/chat/completions` 要等 worker 上线才能成功。

**需要观察的现象**:第三步里 banner 日志出现在 `make_engine` 相关日志之后、HTTP 服务开始 serve 的日志附近——这就是 Python 侧能拿到的最接近「服务启动完成」的时刻;精确的 ready 信号在 Rust `run_input` 内部,Python 侧只能取最近点。

**思考题(不必实现)**:如果要求 banner 必须出现在 `/health` 变 ready 的同一时刻,你会把它挪到哪里?(提示:FrontendExtensionContext 有 `is_ready()`,见 [_core.pyi:L2351-L2381](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/bindings/python/src/dynamo/_core.pyi#L2351-L2381);但那需要把状态查询接到一个轮询点——工程上往往不值得,这正是「Python 驾驶员/Rust 车间」边界的自然代价。)

注意:以上改动仅为学习目的的示例,不要提交到仓库;实践完成后 `git checkout -- components/src/dynamo/frontend/` 还原。

## 6. 本讲小结

- frontend 启动是清晰的三层:`__main__.py`(转发)→ `main()`(`_raise_fd_limit` + uvloop)→ `async_main()`(解析、建运行时、装配、`run_input`),信号经 `create_task` 调度优雅关停。
- `FrontendConfig` 由四条约定构成:ArgGroup 分组(嵌套复用 Router/KvRouter/Aic 三个共享组)、`add_argument` 统一封装(环境变量优先于代码默认值)、多继承配置基类、`from_cli_args` 两步填充(argparse 先铺、类注解默认后补);优先级为 CLI > 环境变量 > 代码默认。
- `validate()` 有副作用:`load_aware` 直接把 `router_mode` 改写为 `kv`,其余是大量交叉校验(TLS 成对、互斥参数、阈值范围)。
- `EntrypointArgs(EngineType.Dynamic, **kwargs)` 是显式白名单的 PyO3 边界:`kwargs` 每个键必须对应签名参数;`chat_engine_factory` 是 Rust 在模型发现时**反向回调** Python 的注入点,签名 `(instance_id, mdc, routed_engine) -> PythonAsyncEngine`。
- 配置边界纪律:`FrontendConfig` 是唯一事实来源,不写回 `os.environ`;现存 TLS 导出与 `DYN_ROUTER_MIN_INITIAL_WORKERS` 两处是注释在案的显式例外。
- `run_input(runtime, "http", engine, extensions)` 之后 Python 主协程进入等待,HTTP 服务在 Rust 侧运行(u4-l2)。

## 7. 下一步学习建议

- **u5-l2(请求整形:prepost.py 与流式后处理)**:本讲只看了 `chat_engine_factory` 的签名,下一讲进入工厂内部——`prepost.py` 的 `preprocess_chat_request` 与 `StreamingPostProcessor` 如何逐 chunk 后处理 token 流。
- **u5-l3(后端差异化 Processor)**:`vllm_processor.py` 与 `sglang_processor.py` 的完整对比,理解 `--dyn-chat-processor` 三条分支的深浅差异。
- **回看 u4-l1**:用本讲的 kwargs 清单对照 `lib/llm/src/entrypoint.rs` 的 Rust 参数结构,验证白名单两端一一对应——这是体会 PyO3 边界设计的最好练习。
- 建议顺手 Grep 一次 `os.environ` 在 `components/src/dynamo/frontend/` 中的命中,逐条判断「合规传值 / 显式例外 / 应改造」——把配置边界纪律从规则变成手感。
