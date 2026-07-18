# ExtAppCall 基本用法：执行外部程序并捕获输出

## 1. 本讲目标

学完本讲，你应当能够：

- 用 `ExtAppCall` 在**指定工作目录**里启动一个外部命令（如 `echo`、`ls`、某个编译器或脚本）。
- 说清 **同步**（`run_sync`）与 **异步**（`run_async` + `wait`）两种执行模型的区别，并知道什么时候该用哪一种。
- 用 `timeout_sec` 给命令设一个时限，理解超时后会发生什么（终止子进程 + 抛 `TimeoutExpired`）。
- 用 `get_stdout` / `get_stderr` / `get_exit_code` 三个取值方法读取命令的输出与退出码，并据此判断命令是成功还是失败。

本讲**只讲对外 API**（怎么构造、怎么跑、怎么取结果）。至于「为什么用临时文件而不是管道来接输出」「为什么 Linux 要 `shell=True` 而 Windows 不要」「删除临时文件为什么要重试 5 次」这些内部机制，留到下一讲 **u4-l2** 再拆。

## 2. 前置知识

本讲依赖你在 **u2-l1** 已经建立的两点认知：

- **`with` 协议**：一个对象只要实现了 `__enter__` / `__exit__` 两个钩子，就能用在 `with` 语句里。`__enter__` 在进入 `with` 块时执行一次，`__exit__` 在退出时执行一次（即使块内抛异常也会执行，走 `finally` 语义）。
- **`TempWorkDir`**：PsiPyUtils 自己写的上下文管理器，`__enter__` 时记录当前目录、`os.chdir` 切换到目标目录，`__exit__` 时切回去。它实现「临时换目录、用完自动还原」。

`ExtAppCall` 内部正好复用了 `TempWorkDir`——这是 PsiPyUtils 模块互相组合的一个典型例子，所以在读本讲之前请确认你已经理解 `TempWorkDir` 的三步流程（记录—切换—还原）。

另外需要一点对 Python 标准库 `subprocess` 的最浅印象：`subprocess.Popen(命令)` 会启动一个子进程来执行那条命令。本讲不需要你写过 `subprocess`，但知道「`Popen` = 起一个子进程」就够了。

## 3. 本讲源码地图

本讲只看一个文件，但它信息量不小：

| 文件 | 作用 | 本讲关注点 |
|------|------|-----------|
| [ExtAppCall.py](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/ExtAppCall.py) | 封装「执行一条外部命令并拿到它的输出/退出码」这件事 | 构造函数、`run_async`/`wait`/`run_sync`、`get_stdout`/`get_stderr`/`get_exit_code` |

补充说明：

- `ExtAppCall.py` 里 `import` 了 [`.TempWorkDir`](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/ExtAppCall.py#L11)（即 u2-l1 讲的那个类），这是它与本库其他模块的连接点。
- `Tests/` 目录下**没有** `TestExtAppCall.py`（可用 `ls Tests/` 自行确认）。也就是说这个类目前没有官方单元测试，本讲的实践任务会带你手动驱动它——这也正好训练「没有测试时如何自己验证行为」。

> 永久链接说明：本文所有源码链接均指向当前 HEAD `6adbdb1`。

## 4. 核心概念与源码讲解

### 4.1 ExtAppCall 构造与状态字段

#### 4.1.1 概念说明

很多自动化场景都需要在脚本里调用一个**外部程序**：调编译器把 HDL 代码编出来、调 Vivado 跑综合、调 `git` 拉代码、甚至调 `echo` 做最小验证。直接用标准库 `subprocess` 写起来啰嗦：要管工作目录、要收 stdout、要收 stderr、要拿退出码、要处理超时。`ExtAppCall` 把这些统包成一个对象：你先**构造**它（告诉它「在哪个目录、跑哪条命令」），再让它**跑**，最后从它身上**取结果**。

构造时确定的两个核心属性是**只读**的：

- `work_dir`：命令在**哪个目录**下执行。
- `command`：要执行的**命令字符串**。

此外对象内部还持有一组**私有状态字段**（以下划线开头），用来存放「还没跑」「跑了一半」「跑完了」各阶段的中间数据：输出缓冲、临时文件句柄、子进程句柄等。使用者不需要直接碰它们，了解它们的存在有助于你看懂后面的方法。

#### 4.1.2 核心流程

构造阶段只做「记住参数 + 初始化空状态」，**不**启动子进程：

```
new ExtAppCall(work_dir, command)
   ├─ self.work_dir   = work_dir      # 只读，后续 run 时切到这个目录
   ├─ self.command    = command        # 只读，要执行的命令
   ├─ self._stdout = ""                # 还没跑，输出为空
   ├─ self._stderr = ""                # 还没跑，输出为空
   └─ self._proc   = None              # 还没有子进程
```

构造完成后，对象处于「已就绪、未执行」状态，可以安全地多次读取 `work_dir` / `command`，也可以反复调用 `run_*`（每次会重置输出）。

#### 4.1.3 源码精读

构造函数定义在 [ExtAppCall.py:16-46](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/ExtAppCall.py#L16-L46)：

```python
def __init__(self, work_dir : str, command : str, ignore_stdout : bool = False, ignore_stderr : bool = False):
    ...
    self.work_dir = work_dir
    self.command = command
    self._stderr = ""
    self._stdout = ""
    self._errFile = None
    self._outFile = None
    self._outPath = None
    self._errPath = None
    self._proc = None
```

- `self.work_dir`、`self.command`：构造时写入、之后不再改，是面向用户的只读属性。
- `self._stdout`、`self._stderr`：用空串初始化，等命令跑完再由 `wait` 填入真正内容。
- `self._proc`：占位为 `None`，`run_async` 时会被替换成真正的 `Popen` 子进程对象。

> **读源码小贴士（文档 vs 实现）**：构造函数的 docstring 里有一段 [warning（L20-L21）](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/ExtAppCall.py#L20-L21) 说「如果程序在某个 I/O 通道上没有输出，就必须设 `ignore_stdout`/`ignore_stderr`，否则执行可能完全卡死」。但同一函数的参数说明（[L27-L28](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/ExtAppCall.py#L27-L28)）又写这两个参数「已无任何作用、仅为向后兼容」，而且函数体里（[L34-L46](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/ExtAppCall.py#L34-L46)）确实**从未读取**它们。这段 warning 是早期版本用「管道」接输出时遗留的旧说明（详见 u4-l2 与 Changelog 1.1.1），现在已不成立。读源码时**以实现为准、不被过时 docstring 误导**，这是 PsiPyUtils 里反复出现的一类训练点。

#### 4.1.4 代码实践

**目标**：验证构造阶段不会启动子进程，且 `work_dir`/`command` 被正确记录。

**步骤**（在仓库根目录，Linux 下）：

```python
# 示例代码：需先能 import 到 ExtAppCall（见下方说明）
from PsiPyUtils import ExtAppCall

c = ExtAppCall(work_dir=".", command="echo hello")
print("work_dir :", c.work_dir)
print("command  :", c.command)
print("stdout   :", repr(c.get_stdout()))   # 此时还没跑，应当是 ''
```

> 说明：若你已 `pip install` 了 PsiPyUtils，可用 `from PsiPyUtils import ExtAppCall`；若只是把源码克隆下来，参考 u1-l2 / u1-l3 的 `sys.path` 技巧把仓库根目录加入路径即可。

**需要观察的现象**：构造之后立刻打印 `work_dir`、`command` 能拿到你传入的值；而调用 `get_stdout()` 得到的是空串（命令还没跑）。

**预期结果**：屏幕上应打印出 `work_dir : .`、`command : echo hello`、`stdout : ''`，且**没有**任何 `hello` 被打印出来——证明构造本身不会执行命令。

#### 4.1.5 小练习与答案

**练习 1**：如果不传 `command`（即 `ExtAppCall(".")`），会发生什么？
**答案**：`__init__` 的第二个位置参数 `command` 没有默认值，Python 会在调用时报 `TypeError: __init__() missing 1 required positional argument: 'command'`。命令是必填项。

**练习 2**：构造之后、运行之前，`get_exit_code()` 的返回值是什么？为什么？
**答案**：会抛 `AttributeError: 'NoneType' object has no attribute 'returncode'`。因为此时 `self._proc` 还是 `None`（[L46](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/ExtAppCall.py#L46)），而 `get_exit_code` 直接读 `self._proc.returncode`（见 4.3）。所以退出码只能在**运行完成之后**读取。

---

### 4.2 run_async / run_sync / wait：三种执行模型

#### 4.2.1 概念说明

「执行一条命令」其实有两种需求：

- **同步**：我就要这条命令的结果，跑完才往下走。最常见。
- **异步**：我想先启动命令，期间去做点别的事（比如再起几条命令并行跑），回头再来等它结束。

`ExtAppCall` 用三个方法覆盖这两种需求，但它们**不是并列的三套实现**，而是有清晰的组合关系：

```
run_sync(timeout_sec)        =  run_async()  +  wait(timeout_sec)  +  sleep(0.1)
```

- `run_async()`：**启动**子进程后**立刻返回**，不等待它结束。
- `wait(timeout_sec)`：**阻塞**直到子进程结束（或超时），并把输出读回到对象里。
- `run_sync(timeout_sec)`：把上面两步连起来，再加一个 0.1 秒的小睡眠——一行调用就「启动 + 等到完」。

所以本质上只有**一个执行引擎**（`run_async` + `wait`），`run_sync` 只是它们的便捷封装。理解了这一点，三种「模式」就不再混淆。

#### 4.2.2 核心流程

**`run_async` 的流程**（[L48-L71](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/ExtAppCall.py#L48-L71)）：

```
run_async():
  with TempWorkDir(self.work_dir):     # 切到 work_dir（u2-l1）
      清空 self._stdout / self._stderr
      建两个临时文件：ExtAppCall-OUT-<uuid>、ExtAppCall-ERR-<uuid>
      打开它们（"w+"）
      self._proc = Popen(command, stdout=OUT文件, stderr=ERR文件, ...)
  # TempWorkDir 退出，把工作目录切回原处
  return self
```

关键点：

1. **命令在 `work_dir` 里执行**。这是靠 u2-l1 的 `TempWorkDir` 实现的——`with` 进入时 `os.chdir(work_dir)`，`Popen` 在此刻被调用，所以子进程继承了 `work_dir` 作为当前目录；`with` 退出时自动切回。**`Popen` 必须在 `TempWorkDir` 的 `with` 块内调用**，否则切目录就白做了。
2. **输出写到临时文件**，而不是用管道（PIPE）。为什么这么做是 u4-l2 的主题，本讲只需知道「stdout/stderr 最终会落进两个文件」。
3. **`uuid.uuid4()`** 用来给临时文件起一个几乎不会重名的名字，避免多次调用互相覆盖。
4. **立刻返回**——子进程此时可能还没跑完。

**`wait` 的流程**（API 视角，[L73-L123](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/ExtAppCall.py#L73-L123)）：

```
wait(timeout_sec):
  尝试 self._proc.wait(timeout_sec)        # 阻塞等子进程
    ├─ 若超时 → self._proc.terminate() 终止子进程，记 timed_out=True
  关闭两个临时文件
  把两个文件里的内容读进 self._stdout / self._stderr
  （清理临时文件 —— 细节见 u4-l2）
  若 timed_out → raise TimeoutExpired("Error: Timeout expired", timeout_sec)
  return self
```

从**调用者**视角看 `wait` 的契约只有三条：

- 它会**阻塞**到子进程结束。
- 给了正的 `timeout_sec`，就最多等这么久；超时会**先终止子进程，再抛 `TimeoutExpired`**。
- 无论正常结束还是超时，它都会把已经产生的 stdout/stderr 读回到对象里（超时时也能拿到子进程在被杀之前输出过的内容）。

**超时单位的提醒（待本地验证）**：参数名叫 `timeout_sec`（秒），实现里直接传给 [`self._proc.wait(timeout_sec)`](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/ExtAppCall.py#L87)（[L86-L87](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/ExtAppCall.py#L86-L87)），而标准库 `Popen.wait(timeout)` 的单位是**秒**。但 docstring（[L78](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/ExtAppCall.py#L78)、[L131](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/ExtAppCall.py#L131)）却写「Timeout in **milliseconds**」。二者矛盾——以实现为准应是**秒**。你可以在 4.2.4 的实践里亲自验证（一个 `sleep 5` 配 `timeout_sec=2`：若是秒，约 2 秒后超时；若是毫秒，2 毫秒会瞬间超时）。

#### 4.2.3 源码精读

**`run_async`** ——[ExtAppCall.py:48-71](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/ExtAppCall.py#L48-L71)。注意它如何用 `TempWorkDir` 把命令的工作目录临时切换过来：

```python
with TempWorkDir(self.work_dir):
    self._stderr = ""
    self._stdout = ""
    self._outPath = os.path.abspath(".") + "/ExtAppCall-OUT-" + str(uuid.uuid4())
    self._errPath = os.path.abspath(".") + "/ExtAppCall-ERR-" + str(uuid.uuid4())
    self._errFile = open(self._errPath, "w+")
    self._outFile = open(self._outPath, "w+")
    ...
    self._proc = Popen(self.command, stdout=self._outFile, stderr=self._errFile,
                       stdin=PIPE, shell=shellParam)
return self
```

`os.path.abspath(".")` 在 `TempWorkDir` 内部取到的是 `work_dir` 的绝对路径，所以两个临时文件是建在 `work_dir` 里的。`Popen` 把子进程的 `stdout`/`stderr` 直接重定向到这两个文件对象。

> 这里有个跨平台分支 `shellParam`（[L66-L68](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/ExtAppCall.py#L66-L68)）：Linux 取 `True`、Windows 取 `False`。「为什么这么分」属于内部机制，留到 u4-l2。对使用者来说，本讲实践都在 Linux 下进行，`shell=True` 意味着你写的 `command` 是交给 shell 解释的，所以 `echo hello`、`ls`、`sleep 5` 这种带空格的命令串能直接生效。

**`wait`** ——[ExtAppCall.py:73-123](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/ExtAppCall.py#L73-L123)。聚焦「等待 + 超时」这段：

```python
timed_out = False
try:
    if timeout_sec > 0:
        self._proc.wait(timeout_sec)
    else:
        self._proc.wait()
except TimeoutExpired:
    self._proc.terminate()
    timed_out = True
```

- `timeout_sec <= 0`（默认 `0`）表示**无限等待**。
- 超时后 `terminate()` 优雅地结束子进程（发 `SIGTERM`，不是直接 kill），并打上 `timed_out` 标记。
- 注意：**超时被 `except` 接住后并没有立刻抛**，而是先把文件读完、清理完，再在 [L121-L122](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/ExtAppCall.py#L121-L122) 统一抛出：

```python
if timed_out:
    raise TimeoutExpired("Error: Timeout expired", timeout_sec)
```

这是一个值得学习的写法——**清理逻辑不被异常路径跳过**，无论超时与否，临时文件都会被关闭和读取。

**`run_sync`** ——[ExtAppCall.py:125-140](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/ExtAppCall.py#L125-L140)，确实就是 `run_async` + `wait` 的拼接：

```python
def run_sync(self, timeout_sec : int = 0):
    self.run_async()
    self.wait(timeout_sec)
    time.sleep(0.1)
    return self
```

末尾的 `time.sleep(0.1)` 给操作系统留了一点缓冲时间去回收子进程资源（一个工程上的「保险」，不是必需逻辑）。

#### 4.2.4 代码实践

**目标**：用 `run_async` + `wait` 跑一个 `sleep` 命令，并故意设一个比睡眠时间短的超时，观察 `TimeoutExpired` 被抛出；借此顺便验证 `timeout_sec` 的真实单位。

**步骤**（Linux）：

```python
# 示例代码
import time
from PsiPyUtils import ExtAppCall
from subprocess import TimeoutExpired

c = ExtAppCall(work_dir=".", command="sleep 5")
c.run_async()                      # 立刻返回，子进程在后台睡 5 秒

t0 = time.time()
try:
    c.wait(timeout_sec=2)          # 只等 2 秒
    print("没有超时（不该走到这里）")
except TimeoutExpired as e:
    dt = time.time() - t0
    print(f"触发了 TimeoutExpired，实际耗时约 {dt:.1f} 秒")
```

**需要观察的现象**：约 2 秒后（而不是 5 秒后、也不是瞬间）抛出 `TimeoutExpired`。

**预期结果**：`实际耗时约 2.0 秒`。如果耗时接近 0 秒，说明 `timeout_sec` 在你这里被当成了毫秒（即 docstring 的说法成立）；如果接近 2 秒，说明是秒（即实现/参数名的语义成立）。**待本地验证**：用这一个问题同时把超时行为和单位歧义一起落实。

#### 4.2.5 小练习与答案

**练习 1**：如果我希望同时启动三条互不依赖的命令、让它们并行跑，应该用 `run_sync` 还是 `run_async`？
**答案**：用 `run_async`。先对三个对象依次调用 `run_async()`（它们会几乎同时启动），然后再依次调用 `wait()` 收尾。若用 `run_sync`，第一条会阻塞到跑完才轮到第二条，就退化成串行了。

**练习 2**：`wait()` 不传任何参数时，行为是什么？
**答案**：`timeout_sec` 取默认值 `0`，命中 [L88-L89](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/ExtAppCall.py#L88-L89) 的 `else` 分支，调用 `self._proc.wait()`（无 timeout），即**无限等待**直到子进程自然结束。如果命令本身永不退出（如一个死循环服务），`wait()` 会永远卡住。

**练习 3**：超时抛出的异常 `TimeoutExpired` 是从哪里来的？
**答案**：`ExtAppCall` 在文件头 [`from subprocess import ... TimeoutExpired`](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/ExtAppCall.py#L9)（[L9](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/ExtAppCall.py#L9)）直接复用了标准库 `subprocess.TimeoutExpired`，没有自定义。所以你 `except` 时既可以从 `subprocess` 导入，也可以写 `except Exception` 兜底。

---

### 4.3 结果获取方法：get_stdout / get_stderr / get_exit_code

#### 4.3.1 概念说明

命令跑完（或被超时终止）后，`ExtAppCall` 对象身上存着三样你关心的东西：

- **stdout**（标准输出）：命令正常打印出来的内容，比如 `echo hello` 的 `hello`。
- **stderr**（标准错误）：命令打印的报错/警告信息。
- **exit code**（退出码 / 返回码）：一个整数，约定俗成 `0` 表示成功、**非 0** 表示失败。这是判断「命令到底有没有成功」最可靠的依据。

`ExtAppCall` 用三个同名 getter 暴露它们。注意它们都是**读**操作，调用多少次结果都一样（直到你再次 `run_*` 重置）。

#### 4.3.2 核心流程

```
命令跑完后：
  get_stdout()   →  返回 self._stdout   （wait 时从 OUT 文件读入的字符串）
  get_stderr()   →  返回 self._stderr   （wait 时从 ERR 文件读入的字符串）
  get_exit_code() →  返回 self._proc.returncode  （子进程退出码，int）
```

判定命令成功与否的典型写法：

```python
if c.get_exit_code() == 0:
    # 成功
else:
    # 失败，去看 get_stderr()
```

两个要点：

1. **必须在 `wait`/`run_sync` 之后再读**。`get_exit_code` 直接读 `self._proc.returncode`，子进程未结束时它是 `None`；只有 `wait` 真正等到了退出，`returncode` 才会被填成整数。
2. **失败不一定抛异常**。命令返回非 0 退出码时，`ExtAppCall` 自己**不会**抛异常——它只是把退出码如实交给你。要不要把非 0 当错误处理，由调用方决定。唯一会抛异常的是**超时**（`TimeoutExpired`）。

#### 4.3.3 源码精读

三个 getter 都极短，定义在 [ExtAppCall.py:142-167](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/ExtAppCall.py#L142-L167)：

```python
def get_stdout(self):
    return self._stdout

def get_stderr(self):
    return self._stderr

def get_exit_code(self):
    return self._proc.returncode
```

而 `self._stdout` / `self._stderr` 是在 `wait` 里被填进去的——[ExtAppCall.py:99-101](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/ExtAppCall.py#L99-L101)：

```python
with open(self._outPath, "r") as fo, open(self._errPath, "r") as fe:
    self._stdout = fo.read()
    self._stderr = fe.read()
```

也就是说，getter 返回的内容源头是那两个临时文件，`wait` 负责把它们一次性读进内存。

> **读源码小贴士**：`get_stderr` 的 docstring（[L155-L156](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/ExtAppCall.py#L155-L156)）里 `:return:` 写的是「stdout content」，而函数显然返回的是 stderr——又是一个文档笔误。实现是对的，docstring 抄错了。这再次提醒：读 PsiPyUtils 时，**对照函数体来理解 docstring**，遇到矛盾以实现为准（与 4.1.3、4.2.2 一致，也呼应 u5-l3 的批判性读源码主题）。

#### 4.3.4 代码实践

**目标**：跑一个会失败（非零退出码）的命令，验证 `get_exit_code` 能反映失败、`get_stderr` 能拿到报错信息。

**步骤**（Linux）：

```python
# 示例代码
from PsiPyUtils import ExtAppCall

# ls 一个不存在的目录：退出码非 0，stderr 有报错信息
c = ExtAppCall(work_dir=".", command="ls /这个目录绝对不存在_zzz")
c.run_sync()

print("exit_code:", c.get_exit_code())
print("stdout   :", repr(c.get_stdout()))
print("stderr   :", repr(c.get_stderr()))
```

**需要观察的现象**：`exit_code` 是一个非 0 整数（`ls` 在找不到路径时通常返回 `2`）；`stderr` 里能看到类似 `No such file or directory` 的提示；`stdout` 一般为空。

**预期结果**：类似
```
exit_code: 2
stdout   : ''
stderr   : "ls: cannot access '/这个目录绝对不存在_zzz': No such file or directory\n"
```
（具体文本随系统 `ls` 版本略有不同。）由此验证：**非零退出码不会让 `run_sync` 抛异常**，需要你自己检查 `get_exit_code()`。

#### 4.3.5 小练习与答案

**练习 1**：成功命令（如 `echo hello`）的 `get_exit_code()` 应该返回什么？
**答案**：`0`。POSIX 约定退出码 0 表示成功。

**练习 2**：为什么推荐用 `get_exit_code()` 而不是「stdout 是不是空」来判断成功？
**答案**：因为成功命令也可能没有输出（stdout 为空），失败命令也可能往 stdout 打印了内容。退出码是唯一为「成功/失败」设计的信号，最可靠。

**练习 3**：`run_sync` 之后立刻调用 `get_stdout()` 没问题；但如果在 `run_async()` 之后、`wait()` 之前调用 `get_stdout()`，会拿到什么？
**答案**：会拿到空串 `""`（`run_async` 把它重置为 `""`，[L56-L57](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/ExtAppCall.py#L56-L57)），因为真正的输出要等 `wait` 读取文件后才填入。这也再次说明「读结果必须在 `wait` 之后」。

## 5. 综合实践

把本讲三个模块串起来，写一个小脚本 **`run_ext.py`**，完整体验「构造 → 执行 → 取结果 → 判成败 → 处理超时」的全流程：

```python
# 示例代码
from subprocess import TimeoutExpired
from PsiPyUtils import ExtAppCall

def run_one(work_dir, command, timeout=0):
    print(f"\n=== {command}  (cwd={work_dir}, timeout={timeout}) ===")
    c = ExtAppCall(work_dir=work_dir, command=command)
    try:
        c.run_sync(timeout_sec=timeout)
    except TimeoutExpired:
        print("  >> 超时！")
    print("  exit_code:", c.get_exit_code())
    print("  stdout   :", repr(c.get_stdout()))
    print("  stderr   :", repr(c.get_stderr()))

if __name__ == "__main__":
    # 1. 成功命令
    run_one(".", "echo hello")
    # 2. 失败命令（非零退出码）
    run_one(".", "ls /不存在的路径_zzz")
    # 3. 超时命令：故意给 1 秒，命令要睡 5 秒
    run_one(".", "sleep 5", timeout=1)
```

**操作步骤**：

1. 把上面的脚本存为 `run_ext.py`，放在能 `import PsiPyUtils` 的环境里（参考 u1-l2/u1-l3 的路径处理）。
2. 在 Linux 终端运行 `python3 run_ext.py`。
3. 把三段输出的 `exit_code` / `stdout` / `stderr` 分别记下来。

**需要观察并解释的现象**：

- 第 1 段：`exit_code` 应为 `0`，`stdout` 含 `hello`。
- 第 2 段：`exit_code` 非 0（通常 `2`），`stderr` 含错误信息，`run_sync` **没有抛异常**。
- 第 3 段：约 1 秒后打印 `>> 超时！`，且能拿到 `exit_code`（被 `terminate` 后的值，通常是负数或非零），说明即便超时，结果也已被读回。

**预期结果**：三段输出都能正确反映「成功 / 失败 / 超时」三种结局，证明你已经能用 `ExtAppCall` 的对外 API 完整驾驭一条外部命令的生命周期。

> 如果你不在 Linux 上（例如 Windows），第 3 步的 `sleep 5` 要换成 `timeout /t 5` 之类的等价命令，且由于 `shell=False`（见 [L67-L68](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/ExtAppCall.py#L67-L68)），命令字符串的解析方式不同——这正是 u4-l2 要展开的跨平台话题。本实践推荐在 Linux 下完成。

## 6. 本讲小结

- `ExtAppCall` 把「在指定目录执行外部命令并拿到输出/退出码」封装成一个对象：先**构造**（`work_dir` + `command`），再**执行**，再**取结果**。
- 构造函数只记录参数、初始化空状态字段，**不启动子进程**；`ignore_stdout`/`ignore_stderr` 仅为向后兼容、已无实际作用。
- 执行模型只有一个引擎：`run_async`（启动即返回）+ `wait`（阻塞到结束、读回输出）；`run_sync` 只是二者拼接再加 0.1 秒缓冲。
- `wait(timeout_sec)` 超时会**先 `terminate()` 子进程、再抛 `TimeoutExpired`**，且清理逻辑不被异常跳过；`timeout_sec <= 0` 表示无限等待。注意参数名义是「秒」、docstring 却写「毫秒」，以实现为准（待本地验证）。
- `get_stdout` / `get_stderr` / `get_exit_code` 都要在 `wait`/`run_sync` 之后读；**非零退出码不会自动抛异常**，需自己检查。
- 命令的工作目录由 u2-l1 的 `TempWorkDir` 临时切换实现，这是本库模块复用的典型样本。

## 7. 下一步学习建议

本讲只用了 `ExtAppCall` 的**对外契约**，刻意回避了内部实现细节。强烈建议接着读 **u4-l2《ExtAppCall 内部机制：文件通信、超时与跨平台》**，那里会回答你读本讲时积攒的几个「为什么」：

- 为什么用**临时文件**而不是 `PIPE` 接收 stdout/stderr（Changelog 1.1.1 的阻塞修复）？
- 为什么 Linux 要 `shell=True`、Windows 要 `shell=False`？
- `wait` 里那段「最多重试 5 次、每次睡 15 秒」的删除循环在防什么（Windows 句柄滞留）？

读完 u4-l2 后，如果你想再上一层，可以去看 **u5-l3《测试组织与批判性读源码》**——本讲里那些「docstring 与实现不一致」的小贴士在那里会被系统化成一种读源码的习惯，并对照盘点哪些模块（包括 `ExtAppCall`）还没有测试。
