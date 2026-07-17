# 本地 serve 与 run 的完整链路 local.py

## 1. 本讲目标

本讲是专家层的第一讲，承接 u2-l2（子进程执行层）、u2-l4（模型发现与 Bento 解析）、u2-l6（虚拟环境管理）三讲，把「装好依赖的 venv」最终变成「一个跑起来的 OpenAI 兼容服务」。

读完本讲，你应当能够：

- 说清 `openllm serve` 与 `openllm run` 在源码层面的分工：`serve` 起一个常驻服务，`run` 在服务之上再加一层「等就绪 + 终端多轮对话」。
- 画出一条 `serve` 命令从 `__main__.py` 到 `run_command` 的完整调用时序，理解 `_get_serve_cmd` 如何拼装 `bentoml serve`、`BENTOML_HOME` 指向哪里、环境变量如何分两路注入子进程。
- 读懂 `_run_model` 中的异步就绪轮询：`/readyz` 探活、30 秒阈值后才流式打印日志、`for...else` 超时判定、`async_run_command` 的信号清理。
- 看懂 `run` 如何用 `openai.AsyncOpenAI` 客户端在终端做流式多轮对话。

## 2. 前置知识

在进入本讲前，请确保你已经理解下面几个前置概念（它们都在前序讲义中讲过，这里只做一句话回顾）：

- **BentoInfo 与 `bentoml_tag`**（u2-l4）：每个可运行模型版本是一个 Bento 目录。`BentoInfo` 持有 `repo/path/alias`，其中 `bentoml_tag` 恒为「真实版本号」（`{name}:{version}`），是真正要传给 `bentoml serve` 的名字；而 `tag` 是别名感知的、面向用户的名字。本讲拼命令时用的是 `bentoml_tag`。
- **EnvVars**（u2-l2）：一个继承 `UserDict` 的环境变量字典，构造时按 key 排序、去空值，并提供确定性 `__hash__`。
- **run_command / async_run_command**（u2-l2）：同步 / 异步两套子进程执行通道，共用「把 `bentoml` 改写为 `python -m bentoml`、把 `python` 改写为 venv 解释器」的命令重写规则；`async_run_command` 是异步上下文管理器，退出时无条件发 `SIGINT` 收尾。
- **ensure_venv**（u2-l6）：给定 `BentoInfo` 与运行时环境变量，解析出 `VenvSpec`、用 uv 建好虚拟环境并装好依赖，返回 venv 目录路径。
- **Typer 命令分层**（u1-l3）：`__main__.py` 是「指挥层」，只做参数收集与编排；真正的执行在 `local.py`（本地）和 `cloud.py`（云端）。

如果你对「为什么 bentoml serve 需要 BENTOML_HOME」「为什么 run 要轮询 /readyz」这些动机问题感兴趣，本讲会逐一回答。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 角色 | 本讲关注点 |
| --- | --- | --- |
| [src/openllm/local.py](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/local.py) | 本地运行执行层 | `serve` / `run` / `_run_model` / `_get_serve_cmd` / `prep_env_vars` |
| [src/openllm/common.py](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/common.py) | 公共基础设施 | `run_command`、`async_run_command`、`stream_command_output`、`BentoInfo.bentoml_tag` |
| [src/openllm/venv.py](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/venv.py) | 虚拟环境管理 | `ensure_venv` 把 bento 变成装好依赖的 venv 目录 |
| [src/openllm/\_\_main\_\_.py](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/__main__.py) | CLI 指挥层 | `serve` / `run` 顶层命令如何调用 `local_serve` / `local_run` |

一句话定位：`__main__.py` 接命令 → `local.py` 干活 → `local.py` 依赖 `venv.py` 准备环境、依赖 `common.py` 真正拉起子进程。

> 说明：本仓库**没有**针对 `local.py` 的单元测试（`test*.py` 全仓为零），所以本讲的实践以「源码跟踪 + 真实命令观察 + 少量示例代码」为主，凡涉及真实模型加载的步骤都标注「待本地验证」。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **serve 命令拼装与环境注入**——`_get_serve_cmd` + `prep_env_vars` + `serve`。
2. **`_run_model` 异步就绪轮询**——`async_run_command` 上下文 + `/readyz` 探活 + 超时流式日志 + 信号清理。
3. **OpenAI 客户端终端对话**——`openai.AsyncOpenAI` 流式多轮对话循环。

### 4.1 serve 命令拼装与环境注入

#### 4.1.1 概念说明

`openllm serve llama3.2:1b` 这条命令背后真正发生的事是：把 `llama3.2:1b` 解析成一个 `BentoInfo`（u2-l4 的 `ensure_bento` 负责），然后拼出一条形如

```bash
python -m bentoml serve llama3.2:1b --port 3000
```

的命令，在一个装好依赖的 venv 里执行。`local.py` 中的 `serve()` 就是干这件事的「装配车间」。

这里有两个不那么直观的设计点，是本模块的重点：

- **命令里用的是 `bentoml_tag` 而非 `tag`**：因为 `bentoml serve` 需要的是磁盘上真实存在的版本目录名，别名只有 OpenLLM 自己认识。
- **环境变量走两条路注入**：一路是 `prep_env_vars` 把 `bento.yaml` 里声明的 `envs` 写进当前进程的 `os.environ`；另一路是 `_get_serve_cmd` 返回一个只含 `BENTOML_HOME` 的 `EnvVars`，再加上命令行 `--env`。两路最终在 `run_command` 的 `copy_env=True` 里合流。理解这两路是看懂 `serve` 的关键。

#### 4.1.2 核心流程

`serve` 的执行流程可以用下面的伪代码描述：

```
serve(bento, port, cli_envs, cli_args):
    1. prep_env_vars(bento)          # 把 bento.yaml 的 envs 写进 os.environ（全局副作用）
    2. cmd, env = _get_serve_cmd(bento, port, cli_args)
            # cmd = ['bentoml','serve', bento.bentoml_tag, 可选 --port, 可选 --arg ...]
            # env = EnvVars({'BENTOML_HOME': '<repo.path>/bentoml'})
    3. 把 cli_envs 合并进 env（带 = 直接拆；不带 = 则取 os.environ 同名值）
    4. venv = ensure_venv(bento, runtime_envs=env)   # u2-l6：建/复用 venv
    5. 提示 Chat UI 地址
    6. run_command(cmd, env=env, venv=venv)          # u2-l2：真正拉起子进程（前台阻塞）
```

第 6 步的 `run_command` 是**同步阻塞**的——它会一直占着终端，直到你 `Ctrl+C` 杀掉服务。这正是 `serve` 与 `run` 最本质的区别：`serve` 把控制权交给 bentoml 服务进程本身。

#### 4.1.3 源码精读

先看命令拼装。`_get_serve_cmd` 只做三件事：以 `bentoml serve <bentoml_tag>` 起头、按需追加 `--port`、把每个 `--arg` 透传给 bentoml serve；并固定返回一个指向「仓库内 bentoml 目录」的 `BENTOML_HOME`：

[查看 `_get_serve_cmd`：src/openllm/local.py:31-43](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/local.py#L31-L43)

> 中文说明：`cmd[0]` 写成字符串 `'bentoml'`，但在 u2-l2 讲过的 `run_command` 里会被重写成 `<venv>/bin/python -m bentoml`，所以这里看起来像在调一个 `bentoml` 可执行文件，实际是以模块方式运行。`BENTOML_HOME` 指向 `bento.repo.path/bentoml`，正是 BentoML 在仓库里存放 `bentos/<name>/<version>` 的根目录，这样 `bentoml serve <tag>` 才能找到对应的 Bento。

再看环境变量注入的第一条路 `prep_env_vars`。它读取 `bento.envs`（来自 `bento.yaml` 的 `envs` 字段），把每个「有 value」的变量直接写进 `os.environ`——注意这是对**当前 OpenLLM 进程**的全局副作用：

[查看 `prep_env_vars`：src/openllm/local.py:21-28](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/local.py#L21-L28)

> 中文说明：`bento.envs` 是 `bento.yaml` 里声明的环境变量列表，每项形如 `{'name': 'HF_TOKEN', 'value': 'xxx'}`。这里只把 `value` 非空的写进去，空值的跳过——这正好与 `EnvVars` 构造时「去空值」的理念一致。之所以写 `os.environ` 而不是写进返回的 `env`，是因为下游 `ensure_venv` 和 `run_command` 都默认 `copy_env=True`，会把 `os.environ` 作为基底拷贝，从而让这些变量自然流入子进程。

最后看装配车间 `serve` 本身，把上面两段串起来，并处理命令行 `--env`：

[查看 `serve`：src/openllm/local.py:46-66](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/local.py#L46-L66)

> 中文说明：第 56–62 行处理 `--env NAME` 与 `--env NAME=value` 两种形式——前者从当前 `os.environ` 取值（于是 `--env HF_TOKEN` 可以把 shell 里已存在的 `HF_TOKEN` 透传下去），后者直接用给定值。注意 `cli_envs` 写进的是 `env`（`EnvVars`），而 `prep_env_vars` 写进的是 `os.environ`；二者在最后 `run_command(env=env, ..., venv=venv)` 里，由 `copy_env=True` 合并为 `{**os.environ, **env}`（`env` 优先级更高）。

合流点在 `run_command` 里，可对照 [src/openllm/common.py:358-364](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/common.py#L358-L364)：先把 `os.environ` 与传入 `env` 合并，再把 `bentoml` 重写为 `python -m bentoml`。

> 优先级结论：当同一个变量同时出现在 `bento.yaml`(→ `os.environ`)、`--env`、shell 环境里时，最终生效的是「`--env` 显式值 > shell 已有值（被 `--env NAME` 透传时）≈ `bento.yaml` 值（写在 `os.environ`）」，因为 `env`（含 `--env`）会覆盖 `os.environ` 的同名键。

#### 4.1.4 代码实践

**实践目标**：在不真正加载模型的前提下，亲眼看到 `_get_serve_cmd` 拼出的命令与 `BENTOML_HOME` 指向。

**操作步骤**（示例代码，非项目原有代码）：

1. 在仓库根目录启动一个 `python`（建议 `pip install -e .` 后的开发环境，见 u1-l2）。
2. 运行下面这段最小示例，用一个「假 Bento」直接调用 `local._get_serve_cmd`，绕过 `ensure_bento` 的磁盘扫描：

```python
# 示例代码：仅用于观察命令拼装，不是 OpenLLM 的真实用法
import types
from openllm import local

# 伪造一个只具备 _get_serve_cmd 所需属性的对象
fake_repo = types.SimpleNamespace(path='/home/me/.openllm/repos/.../...')
fake_bento = types.SimpleNamespace(
    bentoml_tag='llama3.2:1b',
    repo=fake_repo,
)

cmd, env = local._get_serve_cmd(fake_bento, port=8000, cli_args=['k=v'])
print('cmd =', cmd)
print('env =', dict(env))
```

3. **需要观察的现象**：`cmd` 应为 `['bentoml', 'serve', 'llama3.2:1b', '--port', '8000', '--arg', 'k=v']`；`env` 应只含一个键 `BENTOML_HOME`，值以 `/bentoml` 结尾。
4. **预期结果**：注意 `cmd[0]` 仍是字符串 `bentoml`——重写发生在 `run_command` 内部，本步骤看不到。把 `port` 改回默认 `3000`，`--port` 片段应消失（见 `_get_serve_cmd` 的 `if port != 3000` 判断）。
5. 想看真正的重写效果，可在示例里加一句 `from openllm.common import run_command; run_command(cmd, env=env)`（**不要带 venv**，否则会真去起服务），观察终端打印的橙色 `$ ...` 行里 `bentoml` 是否变成了 `python -m bentoml`。该步会真的尝试启动服务，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `_get_serve_cmd` 用 `bento.bentoml_tag` 而不是 `bento.tag`？

**参考答案**：`tag` 是别名感知的面向用户名字（如 `llama3.2:latest`），别名只有 OpenLLM 自己认识；而 `bentoml serve` 要在磁盘上的 `bentos/<name>/<version>` 目录里找到 Bento，必须用恒为真实版本号的 `bentoml_tag`（`{name}:{version}`）。

**练习 2**：假设 `bento.yaml` 声明了 `OPENAI_API_BASE=https://a`，用户又传了 `--env OPENAI_API_BASE=https://b`，子进程最终看到哪个值？为什么？

**参考答案**：看到 `https://b`。`bento.yaml` 的值经 `prep_env_vars` 写进 `os.environ`；`--env` 的值写进返回给 `run_command` 的 `env`。`run_command` 用 `{**os.environ, **env}` 合并，`env` 后置故优先级更高，覆盖 `os.environ` 的同名键。

**练习 3**：`prep_env_vars` 为什么要 `if not env_var.get('value'): continue`？

**参考答案**：声明里 `value` 为空的变量（例如只声明名字、由用户在运行时填）不应被写成空字符串覆盖掉 shell 里可能已有的同名值；跳过空值与 `EnvVars`「去空值」的设计保持一致。

---

### 4.2 `_run_model` 异步就绪轮询

#### 4.2.1 概念说明

`openllm run` 与 `serve` 的区别在于：`run` 不把控制权交给服务进程，而是自己在后台拉起服务，**等它就绪后**用一个 OpenAI 客户端在终端里和你对话。这就引出一个核心问题——「怎么知道服务就绪了？」

答案是轮询 `/readyz`。BentoML 服务在模型加载完成、可以接客时会把这个端点返回 200。`_run_model` 就是一个「拉起服务 → 反复试探 /readyz → 就绪后开始聊天」的异步编排函数。

本模块要讲清四件事：

1. **异步上下文管理器** `async_run_command` 如何托管服务子进程的生命周期（进入时启动、退出时发 `SIGINT`）。
2. **`/readyz` 轮询循环**：循环次数、成功条件、连接被拒时的退避。
3. **30 秒阈值**：为什么前 30 秒静默、之后才把服务日志流式打出来。
4. **超时判定与信号清理**：`for...else` 的失败分支、`SIGINT` 收尾。

#### 4.2.2 核心流程

`_run_model` 的骨架（省略聊天部分，那是下一个模块）：

```
async _run_model(bento, port, timeout=600, cli_env, cli_args):
    cmd, env = _get_serve_cmd(...)        # 复用 4.1 的拼装
    env.update(cli_env or {})
    venv = ensure_venv(bento, runtime_envs=env)

    async with async_run_command(cmd, env=env, venv=venv, silent=False) as server_proc:
        start_time = now()
        for _ in range(timeout):          # 至多 timeout 次试探
            try:
                if GET /readyz == 200: break
            except RequestError:          # 服务还没起来（连接被拒）
                if now() - start_time > 30:
                    启动 stdout/stderr 流式打印任务   # 给用户排查
                await sleep(1)
        else:                              # 跑满 timeout 次都没 200
            输出失败；terminate；return
        取消流式打印任务
        ...进入聊天循环（4.3）...
    # 离开 with：async_run_command 的 finally 发 SIGINT + wait，清理服务进程
```

关于「轮询节流」有一个容易看漏的细节：`await asyncio.sleep(1)` **只写在 `except RequestError` 分支里**。也就是说：

- 服务还没监听端口（连接被拒，抛 `RequestError`）→ 睡 1 秒再试，这是最常见的启动期路径，节流约等于「每秒探一次」。
- 服务已监听但 `/readyz` 返回非 200（模型还在加载）→ 没有异常、也没 `break`，会立刻进入下一轮，相当于忙等。好在模型从「能响应」到「就绪」通常很快。

因此 `timeout=600` 这个参数名义上是「秒级超时」，严格说是「最多试探 600 次」，在连接被拒的常见情况下近似 600 秒。

#### 4.2.3 源码精读

先看轮询本体。这段是本讲最值得逐行读的代码：

[查看 `/readyz` 轮询与超时流式日志：src/openllm/local.py:88-110](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/local.py#L88-L110)

> 中文说明：
>
> - 第 93 行 `httpx.get(..., timeout=3)` 给每次探活本身最多 3 秒；连接被拒时 httpx 抛 `httpx.RequestError`，进入 `except`。
> - 第 97 行 `if time.time() - start_time > 30` 是「30 秒阈值」：只有当启动耗时超过 30 秒（说明可能卡住了），才用 `asyncio.create_task` 把 `server_proc.stdout` / `stderr` 流式打到终端，帮助用户看到 vLLM/bentoml 的真实报错。前 30 秒保持安静，是为了不让正常启动期的噪声刷屏。
> - 两个 `if not stdout_streamer` / `if not stderr_streamer` 保证流式任务**只创建一次**（懒启动），后续轮询不会重复 create_task。
> - 第 107–110 行的 `else` 属于 `for`：只有当循环**跑满 `timeout` 次都没 `break`**（即始终没拿到 200）时才执行，输出失败、`terminate()` 后 `return`。

再看信号清理。`_run_model` 把服务进程交给 `async_run_command` 这个异步上下文管理器托管，它的 `finally` 是「安全收尾」的保障：

[查看 `async_run_command` 的 finally 清理：src/openllm/common.py:436-439](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/common.py#L436-L439)

> 中文说明：无论聊天循环是正常结束（用户 `Ctrl+C`）还是中途异常，离开 `async with` 块时，`finally` 都会向服务子进程发 `signal.SIGINT` 并 `await proc.wait()` 收尸。这保证 `run` 退出后不会留下孤儿 bentoml 服务进程占用端口/显存。注意是发 `SIGINT`（优雅中断）而非 `SIGKILL`，给 vLLM 一个清理显存、写缓存的机会。

最后看「取消日志流」：就绪后立刻把排查用的流式任务取消，避免聊天输出和服务日志混在一起：

[查看就绪后取消流式任务：src/openllm/local.py:112-117](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/local.py#L112-L117)

> 中文说明：`stdout_streamer.cancel()` 取消先前 `create_task` 出来的流式打印协程。`if` 守卫是因为这两个任务可能压根没被创建（30 秒内就绪的常见情况）。

#### 4.2.4 代码实践

**实践目标**：观察 `/readyz` 在真实服务上的行为，并验证「30 秒阈值」的存在。

**操作步骤**：

1. 先用 `serve` 单独起一个小模型服务（需要能跑该模型的环境；CPU/GPU 视模型而定，**待本地验证**）：

   ```bash
   openllm serve llama3.2:1b --port 3000
   ```

2. 另开一个终端，在服务启动**过程中**反复探活，观察状态码从「连接被拒」到「200」的变化：

   ```bash
   # 连接被拒时 curl 会报 "Connection refused"；就绪后返回 200
   for i in $(seq 1 60); do curl -s -o /dev/null -w "%{http_code}\n" http://localhost:3000/readyz; sleep 1; done
   ```

3. 服务就绪后，确认 `/readyz` 返回 200（这一步是 `run` 判定「可以开始聊天」的依据）：

   ```bash
   curl -i http://localhost:3000/readyz
   ```

**需要观察的现象**：

- 启动早期 `curl` 要么连接被拒（对应代码里的 `httpx.RequestError`），要么返回非 200；模型加载完成后稳定返回 200。
- 同时访问 `http://localhost:3000/chat` 能看到 Chat UI（`serve` 第 65 行提示的地址）。

**预期结果**：`/readyz` 返回 200 即代表 `_run_model` 会 `break` 出轮询、进入聊天循环。

**进阶（验证 30 秒阈值）**：阅读 [src/openllm/local.py:91-106](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/local.py#L91-L106) 后回答：如果你故意启动一个加载非常慢（或必定失败）的模型，运行 `openllm run <model> --verbose`，前 30 秒终端会看到什么？30 秒之后又会看到什么？

> 参考现象：前 30 秒只有绿色的 `Model loading...`；30 秒后开始出现灰色的服务 stdout 与红色（`#BD2D0F`）的服务 stderr——这是 `create_task(stream_command_output(...))` 被触发后的效果。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`_run_model` 里 `for _ in range(timeout)` 的 `else` 分支什么时候执行？

**参考答案**：当循环**自然耗尽** `timeout` 次迭代都没有遇到 `break`（即始终没拿到 `/readyz` 的 200）时执行。一旦中途 `break`（就绪成功），`else` 不会执行。这是 Python `for...else` 的语义。

**练习 2**：为什么流式日志任务只在「启动超过 30 秒」后才创建，而不是从一开始就流式打印？

**参考答案**：正常启动时 vLLM/bentoml 会打印大量进度日志，从一开始就流式打印会刷屏、淹没 OpenLLM 自己的提示。30 秒内就绪是常态，保持静默更干净；超过 30 秒通常意味着出了问题，此时再放出日志帮助排查。

**练习 3**：服务进程是用 `SIGINT` 而非 `SIGKILL` 收尾的，这样设计有什么好处？

**参考答案**：`SIGINT` 允许子进程（bentoml/vLLM）执行清理逻辑——释放 GPU 显存、刷写缓存、关闭已下载权重的句柄等；`SIGKILL` 是强杀，没有清理机会，容易留下显存占用或半截文件。

---

### 4.3 OpenAI 客户端终端对话

#### 4.3.1 概念说明

`run` 的最后一段是一个 REPL（读取-求值-打印循环）：用内置的 `input()` 读一行你的提问，用 `openai.AsyncOpenAI` 客户端向本地服务发请求，把模型回复**流式**打到终端，并把这一轮对话存进 `messages` 列表，从而支持**多轮上下文**。

这里有两个知识点：

- **OpenAI 兼容协议**：OpenLLM 之所以能直接用官方 `openai` SDK 当客户端，是因为 bentoml 服务暴露的是 OpenAI 兼容的 `/v1/chat/completions`、`/v1/models` 接口（这也是整个项目「OpenAI API compatible」承诺的落点）。
- **流式（streaming）输出**：`stream=True` 让服务逐 token 返回（SSE），客户端用 `async for chunk in stream` 增量拼接，实现「打字机」效果。

#### 4.3.2 核心流程

聊天循环（接在 4.2 的「Model is ready」之后）：

```
client = AsyncOpenAI(base_url='http://localhost:<port>/v1', api_key='local')
messages = []
loop:
    message = input('user: ')              # 阻塞读终端
    if message == '': 提示非空；continue
    messages.append({'role':'user', content=message})
    model_id = (await client.models.list()).data[0].id     # 从 /v1/models 取第一个模型名
    stream  = await client.chat.completions.create(model=model_id, messages=messages, stream=True)
    assistant_message = ''
    async for chunk in stream:             # 逐 token 增量
        text = chunk.choices[0].delta.content or ''
        assistant_message += text
        实时打印 text
    messages.append({'role':'assistant', content=assistant_message})  # 记进历史，支撑多轮
except KeyboardInterrupt: break            # Ctrl+C 退出对话
```

> 多轮上下文的关键：每轮把「用户消息」和「助手回复」都追加进 `messages`，下一次请求把整段 `messages` 发回去，服务端据此维护对话历史——这是 OpenAI Chat Completions 协议的标准用法。

#### 4.3.3 源码精读

先看客户端构造与模型名获取。注意 `api_key='local'` 只是个占位符（本地服务通常不校验），`model` 名是动态拉取的：

[查看客户端构造与模型名获取：src/openllm/local.py:120-132](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/local.py#L120-L132)

> 中文说明：
>
> - 第 120 行 `base_url` 指向本地服务的 `/v1`，`api_key='local'` 是占位（OpenAI SDK 强制要求非空 api_key，本地服务并不真的鉴权）。
> - 第 123 行 `input('user: ')` 是**阻塞**的终端读取——这也是为什么 `run` 必须用 `asyncio.run` 在最外层驱动：`input` 阻塞期间事件循环虽被挂起，但因为流式打印任务此刻已被取消（4.2 末尾），所以不会冲突。
> - 第 130–131 行先 `await client.models.list()` 再取 `.data[0].id` 作为 `model`：因为本地服务加载的模型名由 bento 决定，客户端并不知道，干脆问 `/v1/models` 拿第一个。

再看流式增量与多轮记忆：

[查看流式增量与多轮记忆：src/openllm/local.py:133-144](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/local.py#L133-L144)

> 中文说明：
>
> - 第 133 行 `async for chunk in stream` 逐个 SSE 事件迭代；第 134 行 `chunk.choices[0].delta.content` 是该 token 的增量文本，可能为 `None`（首个 chunk 通常只有 role），用 `or ''` 兜底。
> - 第 135 行 `assistant_message += text` 在客户端把碎片拼成完整回复，第 137–139 行再把完整回复包成 `ChatCompletionAssistantMessageParam` 追加进 `messages`——下一轮请求会带上它，从而实现多轮。
> - 第 141 行 `except KeyboardInterrupt: break` 让用户用 `Ctrl+C` 优雅退出对话循环；退出后由 4.2 讲的 `async_run_command` 的 `finally` 负责关闭服务。

最后看 `run` 这个对外入口：它只是把同步参数收齐，把 `cli_envs` 转成 dict，然后用 `asyncio.run` 驱动 `_run_model`：

[查看 `run` 入口：src/openllm/local.py:147-166](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/local.py#L147-L166)

> 中文说明：`run` 与 `serve` 一样先 `prep_env_vars(bento)`（把 `bento.yaml` 的 envs 写进 `os.environ`），再把命令行 `--env` 收进一个普通 dict `env` 传给 `_run_model`（`_run_model` 内部会 `env.update(cli_env)`，且 `async_run_command` 同样 `copy_env=True`，于是 `os.environ` 里的 bento envs 仍能流入子进程）。注意 `run` 里传给 `_run_model` 的是普通 `dict` 而非 `EnvVars`，但合并发生在 `_get_serve_cmd` 返回的 `EnvVars` 之后。

#### 4.3.4 代码实践

**实践目标**：用官方 `openai` SDK 复刻 `run` 的流式多轮对话，理解「OpenAI 兼容」意味着可以完全用标准客户端访问 OpenLLM 起的服务。

**操作步骤**：

1. 用 `serve` 起一个本地服务（**待本地验证**，需要可运行该模型的环境）：

   ```bash
   openllm serve llama3.2:1b --port 3000
   ```

2. 在另一个终端 / 脚本里运行下面这段「精简版 run」（示例代码，等价于 `_run_model` 聊天部分的最小实现）：

   ```python
   # 示例代码：精简版 run 的聊天循环
   import asyncio
   import openai

   async def chat(port=3000):
       client = openai.AsyncOpenAI(base_url=f'http://localhost:{port}/v1', api_key='local')
       model_id = (await client.models.list()).data[0].id
       messages = []
       while True:
           user = input('user: ')
           if not user:
               continue
           messages.append({'role': 'user', 'content': user})
           stream = await client.chat.completions.create(
               model=model_id, messages=messages, stream=True
           )
           full = ''
           print('assistant: ', end='', flush=True)
           async for chunk in stream:
               t = chunk.choices[0].delta.content or ''
               full += t
               print(t, end='', flush=True)   # 打字机效果
           print()
           messages.append({'role': 'assistant', 'content': full})

   asyncio.run(chat())
   ```

**需要观察的现象**：

- 模型回复是逐字「流」出来的（不是一次性返回），对应代码里的 `async for chunk in stream`。
- 连续提问时，模型能记住上文（例如先问「我叫小明」，再问「我叫什么」），因为 `messages` 列表不断累积。

**预期结果**：这段示例代码与 `openllm run` 在终端里的体验一致——证明 `run` 本质就是「`serve` 起服务 + 一个 OpenAI 客户端 REPL」。

#### 4.3.5 小练习与答案

**练习 1**：`run` 里 `api_key='local'` 是真的在做鉴权吗？

**参考答案**：不是。本地服务一般不校验 api_key，但官方 `openai` SDK 要求构造客户端时 `api_key` 非空，所以随便填一个占位字符串 `'local'` 即可。

**练习 2**：为什么 `model` 名要用 `(await client.models.list()).data[0].id` 动态获取，而不是写死？

**参考答案**：本地服务加载的模型名由具体 bento 决定（与 `bentoml_tag` 相关但未必相等），客户端事先不知道；问 `/v1/models` 拿第一个是最稳妥的做法。

**练习 3**：如果把 `stream=True` 去掉，这段代码的行为会怎样变化？

**参考答案**：`create` 会直接返回一个完整的 `ChatCompletion`（而非异步迭代器），`async for chunk in stream` 这一行会报错（无法迭代）；要改为读取 `response.choices[0].message.content` 一次性输出，也就失去了「打字机」效果。

## 5. 综合实践

**任务**：跟踪一次 `openllm serve <小模型>` 的完整执行，画出从 `__main__.serve` 到 `run_command` 的调用时序，并解释 `/readyz` 在 `run` 模式下的作用。

**步骤 1：对照源码画出 `serve` 的调用时序**

阅读以下两段源码并把它们串成一张时序图：

- 指挥层入口 [src/openllm/\_\_main\_\_.py:246-268](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/__main__.py#L246-L268)
- 执行层 [src/openllm/local.py:46-66](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/local.py#L46-L66) 与 [src/openllm/venv.py:88-92](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/venv.py#L88-L92)

参考时序（请用你自己的语言补充每一步的输入/输出）：

```
__main__.serve(model, port, env, arg)
  ├── cmd_update()                         # 更新模型仓库（u2-l3）
  ├── (若 --verbose) VERBOSE_LEVEL.set(20) # 全局详细度（u2-l1）
  ├── get_local_machine_spec()             # 探测本机硬件（u2-l5）
  ├── ensure_bento(model, target, repo)    # 名字 → BentoInfo（u2-l4）
  └── local_serve(bento, port, cli_envs, cli_args)        # local.serve
        ├── prep_env_vars(bento)           # bento.yaml envs → os.environ
        ├── _get_serve_cmd(bento, port, cli_args)
        │     → cmd=['bentoml','serve',bento.bentoml_tag,...], env=EnvVars({BENTOML_HOME})
        ├── 合并 cli_envs 进 env
        ├── ensure_venv(bento, runtime_envs=env)          # venv.py：建/复用 venv
        │     ├── _resolve_bento_venv_spec → VenvSpec（哈希）
        │     └── _ensure_venv → uv venv + uv pip install（命中 DONE 则复用）
        └── run_command(cmd, env=env, venv=venv)          # common.py
              ├── {**os.environ, **env} 合流环境
              ├── 'bentoml' → '<venv>/bin/python -m bentoml' 重写
              └── subprocess.run(check=True)  # 前台阻塞，直到 Ctrl+C
```

**步骤 2：解释 `/readyz` 在 `run` 模式下的作用**

阅读 [src/openllm/local.py:69-144](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/local.py#L69-L144)，用一段话回答：

- `run` 为什么不能像 `serve` 那样「拉起子进程就完事」？（提示：`run` 要在服务就绪后用客户端对话。）
- `/readyz` 在这里扮演什么角色？（提示：模型加载是异步的、耗时的；需要一个「能接客了吗」的信号。）
- 如果 `/readyz` 在 `timeout` 内始终不返回 200，会发生什么？（提示：`for...else` 失败分支。）

**参考答案要点**：`serve` 是把控制权交给服务进程本身（前台阻塞），用户自己去用浏览器/客户端；`run` 则要先用 `async_run_command` 在后台拉起服务，但模型权重加载需要时间，直接发请求会失败，所以必须轮询 `/readyz`——它返回 200 表示服务已加载完模型、可接客，此时 `break` 出轮询、进入聊天循环。若 `timeout`（默认 600 次）内始终非 200，`for...else` 的失败分支会 `terminate()` 服务并退出。注意 `serve` 模式下并不调用 `/readyz`，它是 `run` 专属的就绪闸门。

**步骤 3（可选，待本地验证）**：在有 GPU 或可运行小模型的环境里，分别运行

```bash
openllm serve llama3.2:1b --port 3000 --verbose
openllm run   llama3.2:1b --verbose
```

对比两者终端输出：`serve` 应在打印 Chat UI 地址后进入服务前台日志；`run` 应先打印 `Model loading...`、（若超过 30 秒）流出服务日志、就绪后打印 `Model is ready` 并出现 `user:` 提示符。

## 6. 本讲小结

- `local.py` 是本地执行层：`serve` 起常驻服务（前台阻塞），`run` 在服务之上加「等就绪 + 终端多轮对话」。指挥层 `__main__.py` 只做参数收集与 `ensure_bento`，重活都在这里。
- `_get_serve_cmd` 用 `bento.bentoml_tag`（真实版本，非别名 `tag`）拼出 `bentoml serve`，并固定返回指向 `bento.repo.path/bentoml` 的 `BENTOML_HOME`——这是 bentoml 找到 Bento 的关键。
- 环境变量走两路：`prep_env_vars` 把 `bento.yaml` 的 `envs` 写进 `os.environ`，`_get_serve_cmd`/`--env` 写进 `EnvVars`；二者在 `run_command` 的 `copy_env=True`（`{**os.environ, **env}`）里合流，`--env` 优先级最高。
- `_run_model` 用 `async with async_run_command(...)` 托管服务子进程：进入时启动、退出时发 `SIGINT` 收尾，保证不留孤儿进程。
- `/readyz` 轮询是 `run` 的就绪闸门：最多试探 `timeout` 次，连接被拒时睡 1 秒退避；超过 30 秒未就绪才流式打印服务日志辅助排查；`for...else` 处理始终未就绪的失败分支。
- 聊天循环用 `openai.AsyncOpenAI`（`api_key='local'` 占位、`base_url` 指本地 `/v1`），`stream=True` 逐 token 流式输出，`messages` 累积实现多轮上下文——这正是「OpenAI 兼容」承诺的最终落点。

## 7. 下一步学习建议

- 本讲只讲了「本地」链路。把视线转向云端，下一讲 [u3-l2 云端部署 cloud.py](u3-l2-cloud-deploy.md) 讲 `_get_deploy_cmd` 如何拼 `bentoml deploy`、如何处理 `bento.yaml`/`--env`/`os.environ` 三层环境变量优先级，以及 `ensure_cloud_context` 的登录引导——它的环境变量合流逻辑与本讲 4.1 可以对照着读。
- 想理解 `__main__.py` 里 `serve`/`run` 命令如何被自动裹上「埋点 + 计时」，参看 [u3-l3 CLI 命令装饰器与使用分析 analytic.py](u3-l3-cli-analytics.md)。
- 想亲手让 OpenLLM 发现并运行自己的模型，综合 `repo.py` + `model.py` + `local.py`，参看 [u3-l5 二次开发：自定义模型仓库与 Bento 实践](u3-l5-custom-repo-and-bento.md)。
- 建议延伸阅读 BentoML 的 `bentoml serve` 与 `/readyz`（健康检查）语义，以补全本讲「服务端」那一侧的视角。
