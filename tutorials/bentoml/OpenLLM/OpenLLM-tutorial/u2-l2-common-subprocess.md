# 公共基础设施（二）：子进程与命令执行

## 1. 本讲目标

OpenLLM 自身是个「指挥官」：它解析模型名、准备虚拟环境、然后把这些工作交给真正的执行者——`bentoml`、`uv`、`python` 这些子进程。本讲聚焦 `common.py` 里那条「把命令交出去执行」的统一通道，读完本讲你应当能够：

- 看懂 `EnvVars` 这个自定义字典「按 key 排序 + 自动去空」的设计意图，以及它如何与 Pydantic 配合。
- 理解 `run_command` 如何把一条 `['bentoml', 'serve', ...]` 改写成 `['python', '-m', 'bentoml', 'serve', ...]`，并拼接 `env`、`cwd`、`venv` 后同步执行。
- 掌握 `async_run_command` 作为**异步上下文管理器**的生命周期：进入时启动子进程、退出时发信号清理。
- 明白 `stream_command_output` 如何把子进程的标准输出/错误流式打到终端，以及为什么需要 `flush=True`。

本讲承接 u2-l1 的 `output`、`VERBOSE_LEVEL`、`EnvVars`，并为 u2-l6（venv）和 u3-l1（local serve/run）打下执行层基础。

## 2. 前置知识

在进入源码前，先建立三点直觉。

**第一，什么是子进程？** 当 OpenLLM（一个 Python 进程）需要运行 `bentoml serve` 时，它并不是自己「变成」bentoml，而是启动一个新的操作系统进程来跑这条命令，自己则在旁边等待结果。Python 标准库提供两套工具：

- `subprocess.run(...)` —— 同步阻塞：调用了就一直等到子进程结束，返回 `CompletedProcess`。
- `asyncio.create_subprocess_*(...)` —— 异步非阻塞：立刻拿到一个 `Process` 对象，主程序可以一边干别的事（比如轮询健康检查接口），一边随时读子进程的输出。

**第二，为什么要改写命令？** 用户/上层代码习惯写成 `bentoml serve`，但 OpenLLM 为每个模型准备了独立的虚拟环境（u2-l6），那个环境里的 `bentoml` 必须用**那个 venv 里的解释器**来跑，而不是系统全局的 `bentoml`。最稳妥的做法不是依赖 `PATH` 查找，而是直接用 `<venv>/bin/python -m bentoml ...`——「指定解释器 + 以模块方式运行」，这样无论环境变量怎么变，跑的都是对的 bentoml。

**第三，环境变量为什么需要排序去空？** OpenLLM 在多处用「环境变量的集合」做哈希来缓存复用（如 venv 目录名）。如果同一个集合因为插入顺序不同就产生不同哈希，缓存就会失效。所以 `EnvVars` 在构造时就排序并丢弃空值，保证「内容相同 ⇒ 哈希相同」。

## 3. 本讲源码地图

| 文件 | 本讲涉及的内容 |
| --- | --- |
| [src/openllm/common.py](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/common.py) | 全部核心代码都在这里：`EnvVars`、`run_command`、`stream_command_output`、`async_run_command` |
| src/openllm/venv.py | 调用方之一：用 `run_command` 执行 `uv venv` / `uv pip install`（u2-l6 详解） |
| src/openllm/local.py | 调用方之二：用 `run_command` 起 serve、用 `async_run_command` + `stream_command_output` 起 run（u3-l1 详解） |
| src/openllm/cloud.py | 调用方之三：用 `run_command` 执行 `bentoml deploy`（u3-l2 详解） |

本讲的源码精读全部聚焦在 `common.py`，其余三个文件只作为「真实调用现场」佐证设计意图。

## 4. 核心概念与源码讲解

### 4.1 EnvVars 与命令重写机制

#### 4.1.1 概念说明

`EnvVars` 是一个「环境变量专用字典」，继承自标准库的 `UserDict[str, str]`。它解决两个问题：

1. **去空**：环境变量值为空字符串时通常意味着「未设置」，留着会污染哈希与显示，所以构造时直接丢弃。
2. **确定性顺序**：内部始终按 key 排序，从而让 `__hash__` 稳定——这对 OpenLLM 用 `hash(venv_spec)` 做缓存目录名（见 u2-l6）至关重要。

「命令重写」则指 `run_command` / `async_run_command` 在真正执行前，把命令数组里的 `bentoml` / `python` 替换成「指定的 Python 解释器」。两者共享同一套重写规则。

#### 4.1.2 核心流程

`EnvVars` 的构造流程可以用下面这段伪代码描述：

```text
输入: {K1: V1, K2: '', K3: V3, ...}
  ↓ 过滤掉 v 为空的项
  ↓ 按 key 排序
内部存储: {K1: V1, K3: V3}   # 顺序确定
  ↓
__hash__ = hash(tuple(sorted(items)))   # 内容相同 ⇒ 哈希相同
```

命令重写的规则（在 `run_command` 与 `async_run_command` 中各出现一次，逻辑一致）：

```text
若 cmd[0] == 'bentoml'：cmd = [py, '-m', 'bentoml', *cmd[1:]]   # 以模块方式运行
若 cmd[0] == 'python' ：cmd = [py, *cmd[1:]]                    # 换成指定解释器
其它（如 uv）：原样执行
```

其中 `py` 是「正确的 Python 解释器路径」：若传了 `venv` 则用 `<venv>/bin/python`，否则用当前进程的 `sys.executable`。

#### 4.1.3 源码精读

先看 `EnvVars` 的定义与去空排序逻辑：

[src/openllm/common.py:111-127](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/common.py#L111-L127) —— `EnvVars` 类：继承 `UserDict`，注释说明它「按 key 排序且只保留有值的环境变量」。

关键的是构造函数里的这一行，它一次完成「去空 + 排序」：

```python
self.data = {k: v for k, v in sorted(self.data.items()) if v}
```

`__get_pydantic_core_schema__` 让 `EnvVars` 能被 Pydantic 当成普通 `dict[str, str]` 序列化/校验，这样它就能放进 `VenvSpec` 这类 Pydantic 模型里参与哈希（详见 u2-l6）。`__hash__` 则基于排序后的项求哈希，保证确定性：

```python
def __hash__(self) -> int:
  return hash(tuple(sorted(self.data.items())))
```

再看命令重写逻辑。它在 `run_command` 中是这样写的（`async_run_command` 里几乎逐字相同）：

[src/openllm/common.py:353-364](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/common.py#L353-L364) —— 先按 `venv` 决定用哪个 Python 解释器，再把 `bentoml`/`python` 重写为该解释器的直接调用。

```python
if venv:
  py = venv / bin_dir / f'python{sysconfig.get_config_var("EXE")}'
else:
  py = pathlib.Path(sys.executable)

if cmd and cmd[0] == 'bentoml':
  cmd = [py.__fspath__(), '-m', 'bentoml', *cmd[1:]]
if cmd and cmd[0] == 'python':
  cmd = [py.__fspath__(), *cmd[1:]]
```

注意两个细节：

- `bin_dir` 在 Windows 上是 `'Scripts'`、其它平台是 `'bin'`，所以 venv 解释器路径是跨平台拼出来的（见 [common.py:341](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/common.py#L341)）。
- `sysconfig.get_config_var("EXE")` 在多数平台返回 `''`，在 Windows 上返回 `'.exe'`，用来补全可执行文件后缀。
- `cmd[0] == 'bentoml'` 的判断意味着：上层只需写 `['bentoml', 'serve', ...]` 这种「人类友好」的形式，底层自动改写成对 venv 解释器安全的调用。

#### 4.1.4 代码实践

**实践目标**：验证 `EnvVars` 的「去空 + 排序」行为，以及同样的内容是否产生相同哈希。

**操作步骤**：

```python
# 文件名: play_envvars.py
from openllm.common import EnvVars

# 故意打乱顺序、并塞入一个空值
a = EnvVars({'B': '2', 'A': '1', 'EMPTY': '', 'C': '3'})
b = EnvVars({'C': '3', 'A': '1', 'B': '2'})   # 顺序不同，内容相同（都不含空值）

print('a 内部顺序:', list(a.items()))   # 观察是否按 key 排序、空值是否被丢弃
print('hash 相同?:', hash(a) == hash(b))
```

**需要观察的现象**：`a.items()` 应输出 `[('A','1'), ('B','2'), ('C','3')]`——空值 `EMPTY` 消失，且按字母序排列。

**预期结果**：`hash 相同?: True`。这正是 venv 缓存复用所依赖的性质。

> 待本地验证：哈希值本身在不同 Python 进程间会因 `PYTHONHASHSEED` 随机化而不同，但「同一进程内 `hash(a) == hash(b)`」一定成立，这也是 OpenLLM 真正用到的场景。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `EnvVars.__init__` 里的 `if v` 过滤去掉，会对 u2-l6 的 venv 缓存造成什么影响？

**参考答案**：两个「逻辑等价」但带不同空值环境变量的 `VenvSpec` 会算出不同哈希，导致本可复用的虚拟环境被重复创建，浪费磁盘与时间。

**练习 2**：为什么重写命令时 `bentoml` 要用 `-m`（`python -m bentoml`），而 `python` 不需要？

**参考答案**：`bentoml` 是一个包/模块名，`-m bentoml` 让解释器以模块入口方式运行它，等价于调用 `bentoml` 命令行脚本但更可靠（不依赖 `PATH` 里能否找到 `bentoml` 可执行文件）。而 `python` 本身就是解释器，替换成指定的 `py` 路径即可，无需 `-m`。

---

### 4.2 run_command：同步命令执行

#### 4.2.1 概念说明

`run_command` 是 OpenLLM 执行外部命令的**同步**入口，用标准库 `subprocess.run` 实现。它的职责是「把上层传来的命令数组 + 环境变量 + 工作目录 + 虚拟环境打包好，改写后执行，并在失败时统一以 `typer.Exit(1)` 退出」。

典型调用现场见 venv.py——创建虚拟环境、安装依赖都用它：

[src/openllm/venv.py:48-59](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/venv.py#L48-L59) —— 连续三次 `run_command` 分别执行 `uv venv` 与 `uv pip install`（示例代码定位，详细讲解见 u2-l6）。

以及 local.py 的 serve：

[src/openllm/local.py:66](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/local.py#L66) —— `run_command(cmd, env=env, cwd=None, venv=venv)` 同步启动 serve。

#### 4.2.2 核心流程

```text
run_command(cmd, cwd, env, copy_env, venv, silent):
  1. env 兜底为空 EnvVars；cmd 元素全部 str 化
  2. （非 silent）用 output() 打印「等价 shell 命令」预览：cd / export / source activate / 实际命令
  3. 选定解释器 py：venv ? <venv>/bin/python : sys.executable
  4. 若 copy_env：env = EnvVars({**os.environ, **env})   # 用户传入的覆盖系统环境
  5. 改写 cmd[0]：bentoml→py -m bentoml；python→py
  6. subprocess.run(cmd, ...)：
       - silent=True  → stdout/stderr 丢弃，check=True
       - silent=False → 输出直接继承到终端，check=True
  7. 出错：VERBOSE_LEVEL>=20 时打印异常文本，然后 typer.Exit(1)
```

第 4 步的合并顺序 `{**os.environ, **env}` 很关键：**调用方传入的 `env` 优先级高于系统环境变量**，这正是「为模型注入特定配置（如 `BENTOML_HOME`、`HF_TOKEN`）」的实现方式。

#### 4.2.3 源码精读

先看「等价 shell 命令」的预览输出，这是 OpenLLM 让用户看懂「我到底跑了什么」的贴心设计：

[src/openllm/common.py:342-351](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/common.py#L342-L351) —— 非静默时，按 `cd → export → source activate → 实际命令` 的顺序打印一行行橙色提示。

```python
if not silent:
  output('\n')
  if cwd:
    output(f'$ cd {cwd}', style='orange')
  if env:
    for k, v in env.items():
      output(f'$ export {k}={shlex.quote(v)}', style='orange')
  if venv:
    output(f'$ source {venv / "bin" / "activate"}', style='orange')
  output(f'$ {" ".join(cmd)}', style='orange')
```

注意 `shlex.quote(v)`：它对含空格/特殊字符的值做 shell 安全转义，让预览复制到终端也能跑。这段只是「给人看的预览」，真正执行并不经过 shell（用的是 `subprocess.run` 的列表形式，天然避免注入）。

接着看环境合并与改写、执行：

[src/openllm/common.py:358-376](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/common.py#L358-L376) —— 合并系统环境、改写命令、同步执行并统一异常处理。

```python
if copy_env:
  env = EnvVars({**os.environ, **env})

if cmd and cmd[0] == 'bentoml':
  cmd = [py.__fspath__(), '-m', 'bentoml', *cmd[1:]]
if cmd and cmd[0] == 'python':
  cmd = [py.__fspath__(), *cmd[1:]]

try:
  if silent:
    return subprocess.run(cmd, cwd=cwd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
  else:
    return subprocess.run(cmd, cwd=cwd, env=env, check=True)
except Exception as e:
  if VERBOSE_LEVEL.get() >= 20:
    output(str(e), style='red')
  raise typer.Exit(1)
```

三个要点：

- `check=True`：子进程返回非零退出码时抛 `CalledProcessError`，进入 `except`。
- `silent` 分支用 `DEVNULL` 丢弃输出——用于「我不关心过程，只关心成不成」的场景（如创建 venv）。
- 异常处理统一吞掉细节：只有 `VERBOSE_LEVEL >= 20`（即 `--verbose` 等级够高）时才把异常文本打出来，随后直接 `raise typer.Exit(1)` 让整个 CLI 以失败码退出，不再继续往下跑。

#### 4.2.4 代码实践

**实践目标**：用 `run_command` 执行 `bentoml --version`，验证 `bentoml` 被改写成 `python -m bentoml`，并观察预览输出。

**操作步骤**：

```bash
# 1. 确保开发环境已装好 bentoml（按 u1-l2 的 DEVELOPMENT.md 流程）
python -c "
from openllm.common import run_command
# 注意：这里写的是人类友好的 'bentoml'，底层会被改写
run_command(['bentoml', '--version'])
"
```

**需要观察的现象**：终端会先打印一行橙色的 `$ python -m bentoml --version`（即改写后的命令），随后是 bentoml 的版本号。这正好印证了 4.1 的改写规则。

**预期结果**：看到形如 `1.x.x` 的版本输出，且退出码为 0。如果你设置 `VERBOSE_LEVEL` 不够高、且命令失败，则只会看到 CLI 以非零码退出而看不到异常文本——可对比加 `--verbose` 后的行为。

> 待本地验证：具体版本号随安装的 bentoml 版本而定。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `silent=True` 时要把 stdout/stderr 都指向 `DEVNULL`，而不是简单地不传这两个参数？

**参考答案**：不传时子进程会继承父进程（CLI）的终端，输出会和 CLI 自己的输出混在一起。指向 `DEVNULL` 是「明确丢弃」，保证 `silent` 真的安静，常用于内部步骤（建 venv）不想打扰用户的场景。

**练习 2**：如果调用方既传了 `env={'HF_TOKEN': 'xxx'}`，系统环境里也有 `HF_TOKEN=yyy`，最终子进程拿到的是哪个？

**参考答案**：是 `xxx`。因为合并写成 `{**os.environ, **env}`，后面的 `env` 覆盖前面的 `os.environ`，即「调用方显式传入的优先」。

---

### 4.3 async_run_command 与 stream_command_output：异步执行与流式输出

#### 4.3.1 概念说明

有些命令是「长跑」的，比如 `bentoml serve` 启动的模型服务会一直驻留。对这类命令，同步 `run_command` 会一直卡住，无法在等待期间做别的事。`async_run_command` 用 `asyncio` 提供了**异步上下文管理器**：进入 `async with` 时启动子进程并立刻返回 `Process`，让调用方可以并发地轮询健康检查、读输出；离开 `async with` 时自动发信号清理子进程。

`stream_command_output` 则是个小工具：把子进程的某个输出流（stdout/stderr）逐行异步读到终端，实现「实时看日志」。

真实现场在 local.py 的 `_run_model`：起服务 → 轮询 `/readyz` → 30 秒还没好就把 stdout/stderr 流式打出来给用户排查：

[src/openllm/local.py:83-105](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/local.py#L83-L105) —— `async with async_run_command(...) as server_proc` 内部，用 `asyncio.create_task(stream_command_output(...))` 并发搬运服务日志。

#### 4.3.2 核心流程

```text
async with async_run_command(cmd, ..., venv, silent) as proc:
  ┌─ 进入时：env 兜底 → 选 py → copy_env 合并 → 改写 cmd[0] ──┐
  │   proc = await asyncio.create_subprocess_shell(' '.join(cmd),  │
  │                stdout=PIPE, stderr=PIPE, cwd, env)              │
  └── yield proc   # 把进程对象交给 with 体使用 ───────────────────┘
  ... with 体里：轮询 /readyz、create_task(stream_command_output(proc.stdout)) ...
  ┌─ 退出时（finally）：proc.send_signal(SIGINT); await proc.wait() ─┐
  └── 保证子进程一定被收尾，不会成为孤儿进程 ─────────────────────────┘
```

和 `run_command` 的关键差异：

| 维度 | run_command | async_run_command |
| --- | --- | --- |
| 执行模型 | 同步阻塞（`subprocess.run`） | 异步（`asyncio` 子进程） |
| 返回值 | `CompletedProcess`（含返回码、输出） | `Process` 对象，需自行读流 |
| 默认 silent | `False`（默认打印预览） | `True`（默认静默，由调用方决定何时打输出） |
| 退出语义 | 失败 `typer.Exit(1)` | `finally` 里发 `SIGINT` 清理 |
| 执行方式 | 列表（不经 shell） | `' '.join` 后经 shell |

#### 4.3.3 源码精读

先看小工具 `stream_command_output`，它非常短：

[src/openllm/common.py:379-384](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/common.py#L379-L384) —— 异步逐行读取一个流并 `output` 到终端。

```python
async def stream_command_output(stream, style='gray'):
  if stream:
    async for line in stream:
      output(line.decode(), style=style, end='')
```

`async for line in stream` 会阻塞地等下一行（但在 asyncio 意义上是「挂起让出事件循环」），所以它通常被 `asyncio.create_task(...` 包起来和别的任务并发跑（见 local.py L99/L103）。`end=''` 是因为 `line` 本身已带换行，避免多打一个空行。

> 小坑提示：被监控的子进程必须对输出做行缓冲或显式 `flush=True`，否则在「非 TTY（管道）」场景下 Python 会块缓冲，`stream_command_output` 要等缓冲区满才能读到一行，看起来就像「卡住没输出」。

再看 `async_run_command` 主体。它的前半段（env 兜底、预览、选 py、copy_env、改写 cmd）和 `run_command` 几乎逐字相同：

[src/openllm/common.py:396-421](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/common.py#L396-L421) —— 与 `run_command` 共享的预处理逻辑（注意这里 venv 解释器路径直接写死 `bin`，不像同步版做了 Windows `Scripts` 适配）。

真正不同的是「启动 + yield + 清理」这一段：

[src/openllm/common.py:423-439](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/common.py#L423-L439) —— 用 `asyncio.create_subprocess_shell` 启动，`yield` 进程，`finally` 发 `SIGINT` 收尾。

```python
proc = None
try:
  proc = await asyncio.create_subprocess_shell(
    ' '.join(map(str, cmd)),
    stdout=asyncio.subprocess.PIPE,
    stderr=asyncio.subprocess.PIPE,
    cwd=cwd,
    env=env,
  )
  yield proc
except subprocess.CalledProcessError:
  output('Command failed', style='red')
  raise typer.Exit(1)
finally:
  if proc:
    proc.send_signal(signal.SIGINT)
    await proc.wait()
```

三个要点：

- `@asynccontextmanager` + `yield proc`：把一个「开始-使用-结束」的流程包装成 `async with` 语句，调用方拿到 `proc` 后能在 with 体里自由使用。
- `finally` 里**无条件**发 `SIGINT` 并 `await proc.wait()`：无论 with 体是正常结束还是抛异常，子进程都会被发中断信号并等待回收，避免服务进程泄漏成孤儿。这也是为什么它适合「长跑」的 serve——退出对话时服务会随之被收掉。
- 用 `create_subprocess_shell`（经 shell）而非 `create_subprocess_exec`：因为命令已被 `' '.join` 成字符串。这与同步版「列表不经 shell」的风格不同，使用时要注意参数本身的转义（OpenLLM 内部命令多为固定字面量，风险可控）。

#### 4.3.4 代码实践

**实践目标**：用 `async_run_command` 跑一个会持续产出的短命令，配合 `stream_command_output` 实时打印，体会「异步 + 流式」与 `run_command` 同步执行的差别。

**操作步骤**：

```python
# 文件名: play_async.py
import asyncio
from openllm.common import async_run_command, stream_command_output

async def main():
    # 注意 flush=True：非 TTY 下 Python 默认块缓冲，不 flush 会看不到实时输出
    cmd = [
        'python', '-c',
        'import time\n'
        'for i in range(3):\n'
        '    print(f"line {i}", flush=True); time.sleep(0.5)',
    ]
    async with async_run_command(cmd, silent=False) as proc:
        # 把 stdout 逐行搬到终端
        await stream_command_output(proc.stdout)
        print('子进程 pid:', proc.pid)

asyncio.run(main())
```

运行：`python play_async.py`

**需要观察的现象**：

1. 先打印橙色的 `$ python -c ...` 预览（因为 `silent=False`）。
2. 大约每 0.5 秒出现一行 `line 0` / `line 1` / `line 2`，**逐行实时**出现而不是一次性全打出来。
3. 最后打印子进程 pid。
4. 离开 `async with` 后，`finally` 会给子进程发 `SIGINT`（对这个会自然结束的命令是 no-op）并 `wait` 回收。

**预期结果**：能看到三行实时滚动输出，证明流式读取生效。对比：如果把 `flush=True` 去掉，三行往往会**在子进程结束后才一起**出现——这正是块缓冲导致的，有助于理解真实 serve 场景里日志「为何有时不及时」。

> 待本地验证：在不同操作系统上 `SIGINT` 的具体表现略有差异；Windows 对信号支持有限，但本实践在 Linux/macOS 上行为一致。

#### 4.3.5 小练习与答案

**练习 1**：`async_run_command` 的 `finally` 里为什么要 `await proc.wait()`，光 `send_signal` 不行吗？

**参考答案**：`send_signal` 只是「发」信号，子进程不会瞬间消失。`await proc.wait()` 等待子进程真正结束并回收资源（避免僵尸进程），保证 `async with` 退出后系统状态干净。

**练习 2**：`async_run_command` 用 `create_subprocess_shell`（经 shell），而 `run_command` 用列表（不经 shell）。这一差异带来什么潜在风险？

**参考答案**：经 shell 意味着命令字符串里的特殊字符（如 `$`、`;`、反引号）会被 shell 解释，若参数来自不可信输入就有命令注入风险。OpenLLM 的命令多为内部固定字面量（`bentoml serve` 等），风险可控；但若日后要传入用户可控的字符串参数，应改用 `create_subprocess_exec` 并以列表形式传参。

---

## 5. 综合实践

把本讲三块内容串起来，完成一个「迷你命令执行器」：它先用 `EnvVars` 准备一组带空值、顺序混乱的环境变量；分别用同步与异步两条路径执行同一条 `bentoml --version`（验证改写）；并尝试用异步路径执行一个流式命令。

```python
# 文件名: mini_runner.py
import asyncio
from openllm.common import EnvVars, run_command, async_run_command, stream_command_output, VERBOSE_LEVEL

# 1) EnvVars：去空 + 排序
env = EnvVars({'NOISE': '', 'Z_VAR': 'z', 'A_VAR': 'a'})
print('清洗后的 env:', dict(env))   # 期望: {'A_VAR':'a','Z_VAR':'z'}

# 2) 同步路径：bentoml → python -m bentoml
print('--- 同步 ---')
run_command(['bentoml', '--version'])

# 3) 异步路径：同样的改写规则，但拿到 Process
async def async_path():
    print('--- 异步 ---')
    async with async_run_command(['bentoml', '--version'], silent=False) as proc:
        await stream_command_output(proc.stdout)

asyncio.run(async_path())
```

**验收点**：

- 步骤 1 输出的 env 不含 `NOISE`，且按键序排列。
- 步骤 2、3 都能看到橙色的 `$ python -m bentoml --version` 预览，证明两种路径**共用同一套改写规则**。
- 若想进一步观察异常处理，可故意执行一条不存在的命令（如 `['bentoml', '__no_such_subcommand__']`），再加 `--verbose`（等价 `VERBOSE_LEVEL>=20`）对比是否能看到红色异常文本。

> 待本地验证：步骤 2/3 需要环境中已安装 bentoml；无 bentoml 时可换成 `['python', '--version']`，改写规则同样适用（`python` → 当前解释器）。

## 6. 本讲小结

- `EnvVars` 是「按 key 排序 + 自动去空」的环境变量字典，提供确定性 `__hash__`，是 venv 缓存复用（u2-l6）的基础。
- `run_command` 是同步执行入口：合并 `os.environ` 与传入 `env`（后者优先）、把 `bentoml`/`python` 改写为指定解释器、用 `subprocess.run(check=True)` 执行、失败统一 `typer.Exit(1)`。
- 命令改写规则两处共用：`bentoml` → `<py> -m bentoml`，`python` → `<py>`，`py` 取自 venv 或 `sys.executable`。
- `async_run_command` 是异步上下文管理器：进入启动子进程并 `yield proc`，`finally` 无条件发 `SIGINT` 并 `await wait()` 收尾，专为 serve 这类长跑命令设计。
- `stream_command_output` 把子进程输出流逐行 `output` 到终端，依赖子进程 `flush=True` 才能真正实时。
- 三者共同构成 OpenLLM 的「执行层」：venv.py、local.py、cloud.py 都通过这条通道把命令交给 bentoml/uv/python。

## 7. 下一步学习建议

- **接着读 u2-l3（repo.py）**：仓库管理本身较少直接调子进程，但它产出的 `RepoInfo.path` 是后续 serve 的 `cwd` 来源，理解本讲的 `cwd` 参数如何被消费。
- **重点承接 u2-l6（venv.py）**：那里会密集调用 `run_command` 执行 `uv venv` / `uv pip install`，并用到 `EnvVars` 做哈希，是本讲知识的第一现场。
- **进阶到 u3-l1（local.py）**：`_run_model` 把 `async_run_command` + `stream_command_output` + `/readyz` 轮询组合成一个完整的「起服务-等就绪-流日志」闭环，是异步执行层的最佳综合案例。
- 想深入异步子进程细节，可阅读 Python 官方文档中 `asyncio.subprocess` 与 `asynccontextmanager` 两节，对照本讲源码理解 `yield` + `finally` 的资源管理模式。
