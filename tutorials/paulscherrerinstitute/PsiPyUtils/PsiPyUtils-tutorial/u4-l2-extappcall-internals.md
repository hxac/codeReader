# ExtAppCall 内部机制：文件通信、超时与跨平台

## 1. 本讲目标

上一讲（u4-l1）我们学会了 `ExtAppCall` 的**对外用法**：构造 → 执行 → 取结果，把它当成一个能「在指定目录跑命令、拿到 stdout/stderr/退出码」的黑盒。本讲要打开这个黑盒，回答三个「为什么」：

1. **为什么用临时文件接收子进程输出，而不用看起来更自然的 `subprocess.PIPE`？**
2. **为什么 Linux 上 `shell=True`、Windows 上却 `shell=False`？**
3. **为什么删除临时文件时要「最多重试 5 次」？**

学完后你应当能够：

- 说清「管道会阻塞、文件不会」的根因，并指出它正是 Changelog 1.1.1 修复的那个 bug。
- 看懂 `sys.platform` 分支如何切换 `shell` 参数，并能解释两个平台命令解析方式的差异。
- 读懂 `wait()` 里「关闭文件 → 读取结果 → 重试删除」的工程处理，识别其中注释与代码不一致的地方（批判性读源码）。
- 画出 `run_async` → `wait` 的完整时序，并把 `TempWorkDir`（u2-l1）的复用关系标进图里。

## 2. 前置知识

本讲假设你已经掌握：

- **上下文管理器协议**（`__enter__`/`__exit__`、`finally` 语义）：来自 u2-l1。`ExtAppCall` 的 `run_async` 直接复用了 `TempWorkDir`。
- **`TempWorkDir` 的「记录-切换-还原」三步**：来自 u2-l1。`__enter__` 用 `os.path.abspath(os.curdir)` 锁定旧目录、`os.chdir` 切入；`__exit__` 再切回。
- **`subprocess.Popen` 的基本概念**：Python 标准库启动子进程的接口，`wait()` 阻塞等待子进程结束，`returncode` 存放退出码。
- **`ExtAppCall` 的对外 API**：来自 u4-l1（`run_async`/`wait`/`run_sync`、`get_stdout`/`get_stderr`/`get_exit_code`、`timeout_sec` 的语义）。

两个本讲要用到、但可能还不熟的概念，先在这里讲清：

- **管道（pipe）与内核缓冲区**：`subprocess.PIPE` 在父子进程之间架一根内核维护的「水管」。水管有固定容量（Linux 上通常约 64 KiB）。子进程往里写、父进程从里读。**如果父进程不读，水管被写满后子进程的 `write` 就会阻塞**——这是后面一切麻烦的根源。
- **文件重定向**：把子进程的 `stdout`/`stderr` 直接接到一个磁盘文件（`open(...)` 返回的文件对象传给 `Popen` 的 `stdout=`/`stderr=`）。磁盘不是固定容量的水管，写多少都能装下，父进程不需要「及时排水」。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [ExtAppCall.py](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/ExtAppCall.py) | 执行外部命令并捕获输出（全量约 168 行） | `run_async` 的文件创建与 `Popen`、`wait` 的关闭-读取-删除流程 |
| [TempWorkDir.py](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/TempWorkDir.py) | 临时切换工作目录的上下文管理器（约 28 行） | `ExtAppCall` 如何复用它，以及它对文件路径的影响 |
| [Changelog.md](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/Changelog.md) | 版本演进记录 | 1.1.0（`shell=True` 修复）、1.1.1（改用文件通信修复阻塞）两条历史 |

> 提醒：`ExtAppCall` **没有对应的测试文件**（`Tests/` 下无 `TestExtAppCall.py`），所以本讲的「代码实践」以**源码阅读型实践**为主——画时序图、做推演、写说明，而不是跑现成测试。测试缺口的盘点留到 u5-l3。

## 4. 核心概念与源码讲解

### 4.1 用文件而非管道：subprocess.Popen 重定向与 TempWorkDir 复用

#### 4.1.1 概念说明

最「教科书」的捕获子进程输出的写法是：

```python
proc = Popen(cmd, stdout=PIPE, stderr=PIPE)
out, err = proc.communicate()   # 必须边等边读
```

`communicate()` 之所以要「边等边读」，正是因为管道有固定容量。如果改成 `proc.wait()` 先等结束、再去读 `proc.stdout`，当子进程输出超过约 64 KiB 时，管道被写满、子进程阻塞在写、父进程阻塞在等——**双方互等，死锁**。

`ExtAppCall` 的作者在 Changelog 1.1.1 里明确记录踩过这个坑：

> ExtAppCall blocked under some circumstances (subprocess communication did not work propperly). Fixed this by using files to communicate (this seems the only OS independent mechanism that works reliably)

见 [Changelog.md:L31-L33](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/Changelog.md#L31-L33)。

解决办法不是「更小心地读管道」，而是**绕开管道**：把 `stdout`/`stderr` 重定向到磁盘文件。磁盘没有 64 KiB 上限，子进程可以一口气写完再退出，父进程只需在 `wait()` 之后打开文件读回即可，**读写时间上完全解耦**。作者把这称为「跨 OS 唯一可靠的方式」。

#### 4.1.2 核心流程

`run_async` 在内部做四件事，而且**全部包在一个 `TempWorkDir` 上下文里**：

1. 切换到 `work_dir`（复用 u2-l1 的 `TempWorkDir`）。
2. 在**当前目录**（即已切换后的 `work_dir`）下，用绝对路径拼出两个临时文件名（`uuid` 保证唯一）。
3. 以 `"w+"` 打开这两个文件，作为 `stdout`/`stderr` 的去向。
4. 在**仍处于 `TempWorkDir` 块内**时调用 `Popen` 启动子进程，随后退出 `with` 块、还原工作目录。

这里有一个极关键的设计点，初读很容易漏掉：

> **临时文件用 `os.path.abspath(".")` 取绝对路径，而这个路径是在 `TempWorkDir` 块内算出来的，所以它指向 `work_dir`。** 因为存进 `self._outPath`/`self._errPath` 的是**绝对路径**，`run_async` 退出 `with` 块、工作目录还原之后，`wait()` 仍能凭这两个绝对路径找到文件。

如果当初用相对路径，`run_async` 一退出 `TempWorkDir`，`wait()` 就再也找不到文件了。这是「绝对路径 vs 相对路径」在并发/异步场景下的典型教训。

还有一个容易被忽视的进程语义：

> **工作目录是「每进程独立」的，且在 `Popen` 启动那一刻被子进程捕获。** 父进程事后 `os.chdir` 切回原目录，并不会影响已经启动的子进程——它的 `cwd` 仍是 `work_dir`。所以 `TempWorkDir` 可以在 `Popen` 之后**立刻**退出，既还原了父进程的目录，又不影响子进程在 `work_dir` 里运行。

#### 4.1.3 源码精读

`run_async` 的完整实现（关键部分）：

[ExtAppCall.py:L48-L71](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/ExtAppCall.py#L48-L71) —— 进入 `TempWorkDir`、创建两个临时文件、按平台选 `shell` 参数、`Popen` 启动子进程后退出 `with` 块。

逐段看：

```python
with TempWorkDir(self.work_dir):           # ① 切到 work_dir
    self._stderr = ""
    self._stdout = ""

    # 文件是所有 OS 上唯一可靠的通信方式
    self._outPath = os.path.abspath(".") + "/ExtAppCall-OUT-" + str(uuid.uuid4())
    self._errPath = os.path.abspath(".") + "/ExtAppCall-ERR-" + str(uuid.uuid4())
    self._errFile = open(self._errPath, "w+")   # ② 重定向目标
    self._outFile = open(self._outPath, "w+")

    shellParam = True                            # ③ 见 4.2
    if sys.platform.startswith("win"):
        shellParam = False
    self._proc = Popen(self.command, stdout=self._outFile,
                       stderr=self._errFile, stdin=PIPE, shell=shellParam)  # ④
```

几个要点：

- **第 55 行**：`with TempWorkDir(self.work_dir)` —— 直接复用 u2-l1 的上下文管理器，没有任何额外抽象。
- **第 60-61 行**：`os.path.abspath(".")` 在 `TempWorkDir` 块内被调用，返回的是 `work_dir` 的绝对路径。`uuid.uuid4()` 保证多次调用、多实例并发时文件名不撞。路径分隔符写死成 `/`，但 Python 的 `open` 在 Windows 上也接受 `/`，所以跨平台安全。
- **第 62-63 行**：文件以 `"w+"`（读写）打开。它们被当作「文件对象」直接传给 `Popen` 的 `stdout=`/`stderr=`，这就是**文件重定向**——`subprocess` 会把子进程的标准输出/错误接到这两个文件上，而非管道。
- **第 69 行**：`stdin=PIPE` 给子进程的标准输入架了一根管道。但代码**既不往里写、也不显式关闭**它。对于 `echo`/`ls` 这类不读 stdin 的命令无影响；但若子进程会「读到 stdin 的 EOF」，由于父进程始终持有管道写端、不关闭，EOF 永不到来，子进程会卡住。这是当前实现里一个潜伏的、和 1.1.1 同源家族的隐患，读源码时应注意到（批判性观察）。
- **第 71 行**：`return self` 发生在 `with` 块**之外**——此时工作目录已还原。能这么做，全靠 60-61 行存的是绝对路径。

对照 `TempWorkDir` 的实现，体会「记录-切换-还原」如何被原样复用：

[TempWorkDir.py:L20-L25](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/TempWorkDir.py#L20-L25) —— `__enter__` 锁定旧目录绝对路径并 `chdir`；`__exit__` 切回。

```python
def __enter__(self):
    self._prevDir = os.path.abspath(os.curdir)   # 记录
    os.chdir(self._dir)                          # 切换

def __exit__(self, exc_type, exc_val, exc_tb):
    os.chdir(self._prevDir)                      # 还原
```

`ExtAppCall` 没有重新实现这套逻辑，而是 `from .TempWorkDir import TempWorkDir` 后直接 `with TempWorkDir(self.work_dir):`（见 [ExtAppCall.py:L11](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/ExtAppCall.py#L11) 与 [ExtAppCall.py:L55](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/ExtAppCall.py#L55)）。这就是 u2-l1 所说的「同一对 `__enter__`/`__exit__` 钩子被全库复用」的一个活样本。

#### 4.1.4 代码实践

**实践目标**：亲手验证「文件重定向让读写解耦」，并体会 `TempWorkDir` 复用与绝对路径的配合。

**操作步骤**（示例代码，非项目原有代码）：

```python
# demo_file_redirect.py —— 示例代码
import os, tempfile, uuid
from PsiPyUtils import ExtAppCall, TempWorkDir

# 1) 准备一个干净的工作目录
work = tempfile.mkdtemp()
print("父进程原始 cwd:", os.getcwd())

call = ExtAppCall(work_dir=work, command="echo hello-stdout")
call.run_async()                       # 内部进入 TempWorkDir(work) 又退出
print("run_async 后 cwd:", os.getcwd())  # 应已还原，不再是 work

call.wait()
print("stdout:", repr(call.get_stdout()))
print("exit_code:", call.get_exit_code())

# 2) 观察 work 目录里是否还残留临时文件（被 wait 删除则不应有 ExtAppCall-OUT/ERR-*）
print("残留临时文件:", [f for f in os.listdir(work) if f.startswith("ExtAppCall-")])
```

**需要观察的现象**：

1. `run_async` 返回后，父进程 `cwd` 已还原（说明 `TempWorkDir` 已退出）。
2. `get_stdout()` 仍能正确拿到 `"hello-stdout\n"`（说明绝对路径在目录还原后依然有效）。
3. `wait()` 执行完毕后，`work` 目录里不再有 `ExtAppCall-OUT-*`/`ExtAppCall-ERR-*`（说明删除流程跑过）。

**预期结果**：stdout 为 `"hello-stdout\n"`，exit_code 为 `0`，无残留临时文件。

> **注意**：在 Windows 上偶发残留是可能的（见 4.3 的句柄滞留），属预期范围内的已知行为。

#### 4.1.5 小练习与答案

**练习 1**：如果把第 60 行改成相对路径 `self._outPath = "ExtAppCall-OUT-" + str(uuid.uuid4())`，会发生什么？

**参考答案**：`run_async` 在 `TempWorkDir` 块内创建文件、启动子进程都没问题；但退出 `with` 块后父进程 `cwd` 已还原。随后 `wait()` 里 `open(self._outPath, "r")` 会以**当前（还原后的）目录**解析这个相对路径，找不到文件而抛 `FileNotFoundError`。这正是作者用 `os.path.abspath(".")` 的原因。

**练习 2**：`Popen` 调用放在 `TempWorkDir` 块内，但 `wait()` 在块外调用，子进程的工作目录到底是哪个？

**参考答案**：仍是 `work_dir`。工作目录是进程级状态，子进程在 `Popen`（即 `fork`/`spawn`）那一刻继承了父进程当时的 `cwd=work_dir`，此后各自独立。父进程退出 `TempWorkDir` 切回原目录，不影响子进程。

---

### 4.2 shell 参数的跨平台分支

#### 4.2.1 概念说明

`Popen` 的第一个参数 `command` 在 `ExtAppCall` 里是一个**字符串**（如 `"echo hello"`、`"vivado -mode batch -source run.tcl"`），而不是字符串列表（如 `["echo", "hello"]`）。

字符串命令需要有人去「把它切成 程序名 + 参数」。这件事，两个操作系统的做法不一样，于是 `shell` 参数就得按平台分开：

- **Linux**：`Popen` 底层走 `fork`+`exec`，**不会**把一个含空格的字符串自动拆成参数。如果不加 `shell=True`，`exec` 会把整串 `"echo hello"` 当作「可执行文件名」去查找，自然找不到。`shell=True` 则交给 `/bin/sh -c "echo hello"`，由 shell 拆分。
- **Windows**：`Popen` 走 `CreateProcess`，**本身就接受命令行字符串**并做解析；如果再叠 `shell=True`，会多套一层 `cmd.exe`，反而可能引入额外的引号处理问题。

所以这个分支不是「偏好」，而是两个 OS 命令解析模型差异的必然结果。

#### 4.2.2 核心流程

`run_async` 里的分支很简短：

```
默认 shellParam = True          # 给 Linux/macOS 用
若 sys.platform 以 "win" 开头：  # Windows
    shellParam = False
用 shellParam 启动 Popen
```

用 `sys.platform.startswith("win")` 而非 `== "win32"`，是为了前向兼容（万一未来出现新的 Windows platform 标识），这和 u3-l2 里 `EnvVariables.AddToPathVariable` 的写法是同一个套路。

> 提醒：`sys.platform` 在 Linux 上是 `"linux"`，macOS 上是 `"darwin"`。这段代码默认让 **macOS 也走 `shell=True`** 分支（因为不以 `"win"` 开头），但 Changelog 并未声明支持 macOS，所以这属于「事实上能跑、官方未承诺」的灰色地带——读源码时要意识到这一点。

#### 4.2.3 源码精读

[ExtAppCall.py:L65-L69](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/ExtAppCall.py#L65-L69) —— 按平台选 `shell` 参数并启动子进程。

```python
#Shell must be used for linux but not for windaes
shellParam = True
if sys.platform.startswith("win"):
    shellParam = False
self._proc = Popen(self.command, stdout=self._outFile, stderr=self._errFile, stdin=PIPE, shell=shellParam)
```

注释里 `windaes` 是 `windows` 的笔误——一个无关紧要但真实的痕迹，提示这段代码是手写的、经过了真实调试而非自动生成。

这条分支的历史背景记录在 Changelog 1.1.0：

[Changelog.md:L35-L40](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/Changelog.md#L35-L40) —— 「在 Unix 上跑不起来，因为某处漏了 `shell=True`」。

> Made ExtAppCall working on Unix Systems (failed because shell=True was missing at one place)

也就是说，这个跨平台分支是 1.1.0 那次「Linux 跑不通」bug 的修复结果。把 Changelog、注释、源码三者对照，能完整还原出作者的决策脉络：先有 1.1.0 的 `shell=True` 修复，后有 1.1.1 的文件通信修复。

#### 4.2.4 代码实践

**实践目标**：直观体会「字符串命令在无 shell 时为何失败」。

**操作步骤**（示例代码，非项目原有代码）：

```python
# demo_shell_param.py —— 示例代码
from subprocess import Popen, PIPE

# A) 不走 shell：把整串当「文件名」找，Linux 上通常报 FileNotFoundError
try:
    Popen("echo hello", shell=False, stdout=PIPE).wait()
except Exception as e:
    print("shell=False 失败:", type(e).__name__, e)

# B) 走 shell：交给 /bin/sh -c，正常
print("shell=True 退出码:", Popen("echo hello", shell=True, stdout=PIPE).wait())
```

**需要观察的现象**：A 抛异常（找不到名为 `"echo hello"` 的可执行文件）；B 正常退出、退出码 0。

**预期结果**：A 报 `FileNotFoundError`（或 `PermissionError`，视环境）；B 返回 `0`。

> 待本地验证：不同 shell 与系统版本上 A 的具体异常类型可能略有差异。

#### 4.2.5 小练习与答案

**练习 1**：为什么作者用 `startswith("win")` 而不是 `sys.platform == "win32"`？

**参考答案**：为了前向兼容。`sys.platform` 的值理论上可能随 Python 版本演变，用前缀匹配能兼容未来可能的 Windows 平台标识；同时也与库里 `EnvVariables` 模块的判断风格保持一致。

**练习 2**：在 Linux 上，`shell=True` 时命令实际是由谁执行的？这带来了什么安全风险？

**参考答案**：由 `/bin/sh -c <command>` 执行。风险在于：如果 `command` 字符串里拼接了不可信的外部输入，会构成**命令注入**（shell 元字符如 `;`、`$()`、反引号会被解释）。所以用 `ExtAppCall` 时，`command` 应只接受可信来源的字符串，不要直接拼用户输入。

---

### 4.3 临时文件删除重试循环

#### 4.3.1 概念说明

`wait()` 跑完子进程后，要清理 `run_async` 建的两个临时文件。听起来只是一句 `os.remove`，实际却有一段「最多重试 5 次」的循环。原因写在源码注释里，是 **Windows 上 `Popen` 的一个已知行为**：

> 当 `Popen` 重定向 stdout/stderr 到文件时，Windows 会把这两个文件的句柄（handle）**也传给子进程**，而且无法关闭。即便子进程已退出，Windows 内核仍可能在数分钟内「黏着」这两个临时文件的句柄，导致父进程立即 `os.remove` 时失败（文件被占用）。

Linux 上没有这个问题（`unlink` 即便文件被打开也能成功，只是变成「删除但仍有句柄」）。所以这段重试循环本质上是**为 Windows 的句柄滞留兜底**，按「最严平台」约束写代码——和 u2-l2 里 `TempFile` 「先 close 再 remove」是同一思路。

#### 4.3.2 核心流程

`wait()` 的后半段流程（删除部分）：

```
对 [outPath, errPath] 两个路径依次处理：
    count = 0
    只要 count < 5：
        试 os.remove(path)
            成功 → break，处理下一个路径
            失败（任意异常）→ sleep，count += 1，继续重试
    满 5 次仍失败 → 静默放弃，文件残留
```

注意三个细节（初读容易看走眼，下面源码精读会逐个指出）：

1. **重试上限是 5 次**，失败后**静默放弃**（不抛异常），临时文件就留在磁盘上。
2. `except` 是**裸捕获**（bare `except:`），连 `KeyboardInterrupt`、`SystemExit` 都会被吞掉——这是个代码异味。
3. **注释和代码不一致**：注释写「wait for 5 seconds」，但代码 `time.sleep(15)` 实际睡 15 秒。最坏情况下，单个文件的删除会阻塞很久。

#### 4.3.3 源码精读

先看 `wait()` 读取结果、再删除的整段：

[ExtAppCall.py:L94-L118](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/ExtAppCall.py#L94-L118) —— 关闭文件对象、重新以只读方式打开读取、重试删除临时文件。

```python
#close the file since the subprocess has exited now
self._errFile.close()
self._outFile.close()

#Read stdout and stderr
with open(self._outPath, "r") as fo, open(self._errPath, "r") as fe:
    self._stdout = fo.read()
    self._stderr = fe.read()
```

为什么「先 close，再用路径 reopen 读」？因为 `self._outFile`/`self._errFile` 是 `run_async` 里以 `"w+"` 打开、并交给 `Popen` 当重定向目标的句柄。子进程退出后：

- 父进程先 `close()` 自己持有的写句柄（95-96 行），确保缓冲落盘、Windows 上释放占用。
- 再用**只读 `"r"`** 重新打开（99 行），从一个干净的读位置把内容全读回来。

这一「关写句柄 → 以只读重开」的组合，是为了在两个 OS 上都拿到完整、确定的输出。

再看删除重试循环：

[ExtAppCall.py:L103-L118](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/ExtAppCall.py#L103-L118) —— 注释解释了 Windows 句柄滞留的根因，并给出「重试 5 次」的兜底。

```python
# remove temporary files. If it fails, wait for 5 seconds and retry ...
# 28.07.2018 - known problem of the Popen library on Windows
for path in [self._outPath, self._errPath]:
    count = 0
    while count < 5:
      try:
          os.remove(path)
          break
      except:
          time.sleep(15)   # 注释说 5 秒，代码睡 15 秒
          count += 1
```

逐点批注（批判性读源码）：

- **第 110 行**：遍历两个路径，每个独立计数。
- **第 112 行**：`while count < 5`，最多尝试 5 次。
- **第 114 行**：`os.remove(path)` 成功就 `break`，进入下一个路径。Linux 上几乎第一次就成功。
- **第 116 行**：**裸 `except:`** —— 捕获一切。这会吞掉 `KeyboardInterrupt`（用户按 Ctrl+C 想中断，会被当成「删除失败」然后继续睡 15 秒重试），是公认的代码异味，应写成 `except OSError:`。
- **第 117 行**：`time.sleep(15)` 与注释里的「5 seconds」**对不上**。这是注释陈旧、代码后改但注释没同步的典型痕迹——读源码时以**代码为准**，但要把这种不一致记下来（它会影响 `wait()` 的最坏阻塞时长）。
- **静默放弃**：5 次都失败后循环退出，既不抛异常也不打日志，临时文件就永久留在 `work_dir` 里。

最坏阻塞时长推演（单个路径、Windows 句柄滞留场景）：

\[ T_{\text{worst}} \approx 5 \times 15\,\text{s} = 75\,\text{s} \]

两个路径串行，理论上 `wait()` 可能阻塞约 150 秒才返回。这是用 `ExtAppCall` 时需要心里有数的「尾巴延迟」。

把这段和 u2-l2 `TempFile` 的「先 close 再 remove」对照着读，会发现作者在两处都遵循同一条跨平台铁律：**Windows 上不能删除仍被打开的文件，所以删除前必须先释放句柄；若释放后内核仍滞留，就用重试兜底。**

#### 4.3.4 代码实践

**实践目标**：用真实命令验证删除流程，并量化「正常情况」下 `wait()` 的耗时（无句柄滞留时几乎不阻塞）。

**操作步骤**（示例代码，非项目原有代码）：

```python
# demo_retry_loop.py —— 示例代码
import time, os, tempfile
from PsiPyUtils import ExtAppCall

work = tempfile.mkdtemp()
call = ExtAppCall(work_dir=work, command="echo retry-demo")

t0 = time.time()
call.run_async()
call.wait()
dt = time.time() - t0

print("wait 用时(秒):", round(dt, 3))
print("残留:", [f for f in os.listdir(work) if f.startswith("ExtAppCall-")])
```

**需要观察的现象**：

1. Linux 上 `wait` 用时应在毫秒级（删除第一次就成功，没有 15 秒 sleep）。
2. 目录中无 `ExtAppCall-OUT-*`/`ExtAppCall-ERR-*` 残留。

**预期结果**：Linux 上 `dt` 远小于 1 秒；无残留。

> 待本地验证：在 Windows 上若偶发句柄滞留，可能观察到残留文件；这也是作者写重试循环的原因。

**源码阅读型实践（必做）**：阅读 [ExtAppCall.py:L110-L118](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/ExtAppCall.py#L110-L118)，回答：如果把 `except:` 改成 `except OSError:`，行为在什么场景下会**不同于现状**？

> 参考答案：当用户在重试期间按 Ctrl+C 触发 `KeyboardInterrupt` 时，现状会把它吞掉、继续 sleep 重试；改成 `except OSError:` 后 `KeyboardInterrupt` 会正常向上抛出，用户能及时中断。即：`except OSError:` 更安全、更符合预期。

#### 4.3.5 小练习与答案

**练习 1**：为什么这段重试循环在 Linux 上「几乎不触发」？

**参考答案**：Linux 的 `unlink` 即便文件仍被某进程打开也能成功（内核保留「已删除但有句柄」的 inode，直到所有句柄关闭）。所以 Linux 上 `os.remove` 第一次就会成功，`break` 跳出，不会 sleep。重试是专门为 Windows 句柄滞留兜底的。

**练习 2**：5 次重试都失败后会怎样？

**参考答案**：循环静默退出，既不抛异常也不记日志，两个临时文件就留在 `work_dir` 里。调用方从 `get_stdout`/`get_exit_code` 拿不到任何「清理失败」的信号——这是一个静默的资源泄漏点。

---

### 4.4 把流程串起来：run_async → wait 时序

#### 4.4.1 概念说明

前面三节分别讲了文件通信、`shell` 分支、删除重试。这一节把它们**按时间顺序**串成一条完整的执行链，作为本讲的总结，也作为综合实践（第 5 节）的直接依据。

关键参与者有四个：**父进程（`ExtAppCall`）**、**`TempWorkDir`**、**两个临时文件**、**子进程**。

#### 4.4.2 核心流程（时序）

```
父进程                         TempWorkDir          临时文件 out/err        子进程
  |                                 |                    |                 |
  | run_async()                     |                    |                 |
  |── with TempWorkDir(work) ──────>|                    |                 |
  |   (cwd: 原目录 → work)          |                    |                 |
  |   abspath(".") 拼出 out/err 绝对路径            <创建> |                 |
  |   open(out,"w+") / open(err,"w+") ──────────────────>|                 |
  |   选 shellParam (Linux=True / Win=False)            |                 |
  |   Popen(cmd, stdout=out, stderr=err, shell=…) ────────────────────────>| (spawn)
  |                                 |                    |            (子进程 cwd=work)
  |── with 块结束 ─────────────────>|                    |                 |
  |   (cwd: work → 原目录)          |                    |                 |
  | return self                     |                    |          子进程写 stdout/stderr ──> 文件
  |                                 |                    |                 |
  | wait()                          |                    |                 |
  |── proc.wait() 阻塞 <───────────────────────────────────────────── 子进程退出
  |   （超时则 terminate()，置 timed_out）              |                 |
  |   errFile.close() / outFile.close() ──────────────>|                 |
  |   open(out,"r").read() / open(err,"r").read() ─────>|                 |
  |   for out,err: os.remove，最多重试 5 次（Win 句柄滞留兜底）            |
  |   若 timed_out: raise TimeoutExpired                                |
  | return self                                                         |
```

要点回顾：

- **`Popen` 必须在 `TempWorkDir` 块内调用**，子进程才能以 `work` 为 `cwd` 启动；但启动后 `TempWorkDir` 立即退出不影响子进程。
- **临时文件路径是绝对路径**，所以 `wait()`（在 `TempWorkDir` 块外执行）仍能定位它们。
- **`wait()` 的顺序固定**：等子进程 → 关文件 → 读结果 → 删文件 → （若超时）抛异常。
- **`run_sync`** 只是 `run_async()` + `wait()` + `time.sleep(0.1)` 的拼接（见 [ExtAppCall.py:L125-L140](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/ExtAppCall.py#L125-L140)），那个 0.1 秒是作者的经验性缓冲。

#### 4.4.3 代码实践（画时序图）

见第 5 节「综合实践」，那里给出完整的画图与说明任务。

#### 4.4.4 小练习与答案

**练习**：`run_async` 结束时父进程的 `cwd` 是什么？`wait()` 读文件时 `cwd` 又是什么？为什么这不影响读取？

**参考答案**：`run_async` 结束时 `cwd` 已被 `TempWorkDir` 还原为原目录；`wait()` 读文件时 `cwd` 也是原目录。不影响读取，是因为 `self._outPath`/`self._errPath` 是在 `TempWorkDir` 块内用 `os.path.abspath(".")` 算出的**绝对路径**，`open()` 对绝对路径的解释与当前 `cwd` 无关。

## 5. 综合实践

本任务把全讲内容串起来，是一个**源码阅读型实践**（`ExtAppCall` 无现成测试）。

### 任务一：画一张完整时序图

阅读 [ExtAppCall.py:L48-L71](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/ExtAppCall.py#L48-L71)（`run_async`）与 [ExtAppCall.py:L73-L123](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/ExtAppCall.py#L73-L123)（`wait`），画一张时序图，必须包含以下五个阶段，并标注每一步发生在 `TempWorkDir` 块的「内」还是「外」：

1. **创建 out/err 文件**：`os.path.abspath(".")` 拼绝对路径 + `uuid`，`open(...,"w+")`。
2. **`Popen` 启动**：选 `shellParam`、传入文件重定向、`stdin=PIPE`。
3. **`wait` 等待**：`proc.wait(timeout_sec)`，超时则 `terminate()` 并记 `timed_out`。
4. **关闭并读取文件**：`close()` 写句柄 → 以 `"r"` 重开 → `read()`。
5. **重试删除**：`for path in [out, err]` 往下，`while count < 5` 的 `os.remove` + `time.sleep(15)`。

> 参考：可直接使用 4.4.2 的 ASCII 时序图作为底稿，再补上「块内/块外」的竖直分隔线（`TempWorkDir` 块从 `run_async` 进入到 `Popen` 之后那一段）。

### 任务二：写一段「改回 PIPE 会怎样」的说明

假设有人为了「少生成两个临时文件」，把 `run_async` 改成：

```python
self._proc = Popen(self.command, stdout=PIPE, stderr=PIPE, stdin=PIPE, shell=shellParam)
```

并把 `wait()` 改成 `self._proc.wait()` 后再 `self._stdout = self._proc.stdout.read()`。请写一段 5–8 行的说明，回答：

1. 在**什么场景**下会再次出现 Changelog 1.1.1 描述的「ExtAppCall blocked under some circumstances」？
2. 为什么「文件通信」能从根本上避开这个问题？

**参考要点**（写完后对照）：

1. 当子进程的 stdout（或 stderr）输出量**超过管道内核缓冲区**（Linux 约 64 KiB）时：管道写满 → 子进程阻塞在写 → 父进程阻塞在 `proc.wait()` 等子进程退出 → **双方互等，死锁**。这与 1.1.1 的「blocked under some circumstances」是同一个 bug。
2. 文件重定向把输出写往磁盘，磁盘没有 64 KiB 上限，子进程可以一直写到结束、自然退出；父进程 `wait()` 不需要「及时排水」，**读写时间上完全解耦**，故不会死锁。这正是作者称其为「跨 OS 唯一可靠通信方式」的原因。

### 任务三（可选，进阶）

运行 4.1.4 的示例脚本，用真实的 `echo` 命令验证「文件重定向 + 绝对路径 + 删除」整条链路；并在 Linux 上记录 `wait()` 的耗时，确认它远小于「最坏 75 秒/文件」的上界（因为 Linux 上删除一次成功、不 sleep）。

## 6. 本讲小结

- **文件通信而非管道**：`ExtAppCall` 把 stdout/stderr 重定向到临时文件（`Popen(stdout=file, stderr=file)`），避开了管道 64 KiB 缓冲写满导致的父子进程死锁——这正是 Changelog 1.1.1 修复的 bug。
- **TempWorkDir 复用 + 绝对路径**：`run_async` 在 `with TempWorkDir(work_dir)` 内创建文件、启动 `Popen`；临时文件用 `os.path.abspath(".")` 取得 `work_dir` 下的**绝对路径**，所以 `TempWorkDir` 退出后 `wait()` 仍能定位它们。
- **跨平台 `shell` 分支**：Linux 用 `shell=True`（让 `/bin/sh` 拆分字符串命令），Windows 用 `shell=False`（`CreateProcess` 已能解析），源自 Changelog 1.1.0 的 Unix 修复。
- **删除重试循环**：为 Windows 上 `Popen` 句柄滞留兜底，每个文件最多重试 5 次、每次失败 `sleep(15)`；最坏情况下单个文件阻塞约 75 秒（\(5 \times 15\,\text{s}\)），5 次都失败则静默放弃、文件残留。
- **批判性读源码的几处痕迹**：注释「5 seconds」与代码 `time.sleep(15)` 不一致；裸 `except:` 会吞掉 `KeyboardInterrupt`；`stdin=PIPE` 既不写也不闭，对读 stdin 的子进程是潜在隐患；`timeout_sec` docstring 写「毫秒」、实际是秒。
- **复用关系**：`ExtAppCall` 没有重造「临时切目录」与「先 close 再 remove」的轮子，而是直接复用 u2-l1 的 `TempWorkDir`，并与 u2-l2 的 `TempFile` 共享同一条 Windows 句柄处理铁律。

## 7. 下一步学习建议

- **横向对照 `TempFile`（u2-l2）**：把本讲的「先 close 写句柄 → 重开只读 → remove」与 `TempFile.__exit__` 的「先 close 再 remove」放在一起读，体会 PsiPyUtils 在多处遵循同一条「Windows 不允许删除占用中文件」的跨平台铁律。
- **回到测试缺口（u5-l3）**：本讲多处指出 `ExtAppCall` 没有测试（`Tests/` 下无 `TestExtAppCall.py`）。u5-l3 会系统盘点全库测试覆盖，并把本讲发现的几处「注释/签名与实现不一致」纳入「批判性读源码」的训练。
- **深入 `subprocess`**：若想彻底理解管道死锁，建议读 Python 官方文档里 `subprocess.Popen.communicate` 一节，它解释了「为何 `communicate` 必须边等边读」，正好对照本讲「为何 `ExtAppCall` 干脆不用管道」。
- **下一个学习单元 u5（专家层）**：从本讲的「读懂一个模块的内部机制与历史 bug」过渡到 XmlToolbox、打包发布与测试组织等更宏观的工程主题。
