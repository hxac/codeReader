# 列出与检查依赖（ListDependencies / CheckDependency）

## 1. 本讲目标

本讲进入「动作执行」阶段。前面 u2 系列已经解决了「依赖列表从哪里来」——由 `Parse.FromReadme` 把每个 FPGA 库的 `README.md` 解析成一张扁平的 `Dependency` 列表。从本讲开始，我们关心「拿到列表之后能做什么」。

学完本讲，你应该能够：

- 知道 `-list` 动作背后的 `ListDependencies` 是如何把一条依赖格式化成一行文本的，并能预测它的输出。
- 知道 `-check` 动作背后的 `CheckDependency` 是如何切换到 `rootdir`、逐条判断依赖目录是否存在、存在则交给 `CheckCompatibility`、不存在则打印 `ERROR` 的。
- 理解 `os.chdir` 配合 `try/finally` 这种「改了全局工作目录就必须在 `finally` 里恢复」的防御式编程模式，并能解释为什么必须用 `os.path.abspath(os.curdir)` 来保存原目录。

本讲只覆盖「列出」和「检查」两个最轻量的动作；「检出」(`Checkout`)、版本兼容性细节 (`CheckCompatibility` 内部逻辑)、URL 替换、命令行接口分别在 u3-l2、u3-l3、u3-l4、u3-l5 展开。

## 2. 前置知识

本讲默认你已经读过 u2-l1（`Dependency` 模型）和 u2-l5（相对路径与依赖列表组装）。为了自洽，这里把必要的基础概念再点一遍。

### 2.1 Dependency 的四个字段

每条依赖是一个 `Dependency` 对象，含四个字段（详见 u2-l1）：

| 字段 | 含义 | 本讲如何用到 |
|---|---|---|
| `libraryName` | 库名 / 克隆后的目录名 | 打印行首标识 |
| `url` | git 远程地址 | `ListDependencies` 打印 |
| `relativePath` | 相对于 `rootdir` 的本地落点 | `CheckDependency` 判断目录是否存在 |
| `minVersion` | 最低版本要求（`VersionNr` 对象） | 打印、并交给 `CheckCompatibility` 比较 |

关键点：`relativePath` 是**相对于 `rootdir`** 的路径（如 `../Test/libs/foo`），不是相对于当前工作目录的路径。这正是 `CheckDependency` 必须先 `chdir(rootdir)` 的根本原因。

### 2.2 进程的「当前工作目录」（CWD）

操作系统里，每个进程都有一个**全局的**当前工作目录（Current Working Directory，CWD）。Python 中：

- `os.getcwd()` 返回 CWD 的绝对路径。
- `os.chdir(path)` **修改整个进程的** CWD。注意它是「进程级全局状态」，不是某个函数的局部状态。
- `os.curdir` 是常量字符串 `"."`，它永远代表「当前 CWD」。
- `os.path.abspath(".")` 等价于把 `"."` 解析成 CWD 的绝对路径，即 `os.getcwd()`。
- `os.path.isdir(p)`：`p` 若是相对路径，就按**当前 CWD** 来解析判断。

这一节的「全局性」是理解本讲 `try/finally` 模式的钥匙——下文会反复用到。

### 2.3 Python 的 try / finally

```python
old = save_state()
try:
    do_something_that_may_raise()   # 可能抛异常
finally:
    restore_state(old)              # 无论是否异常，都会执行
```

`finally` 块**无论 `try` 块是正常结束还是抛出异常都会执行**；若 `try` 抛了异常，`finally` 执行完后异常会继续向上传播。这是「修改了全局状态就必须负责还原」的标准写法。

### 2.4 版本号对象 VersionNr

`dep.minVersion` 在运行时的真实类型是 `VersionNr`（不是签名上写的 `str`，见 u2-l1、u2-l2）。它支持：

- `str(dep.minVersion)` → `"major.minor.bugfix"`（`__str__`）。
- `dep.minVersion < other`、`==`、`>` → 逐段语义化比较（高位优先）。
- `dep.minVersion.major` → 主版本号整数。

`ListDependencies` 打印时会隐式调用 `__str__`；`CheckCompatibility` 比较时会用到 `<` 和 `.major`。

## 3. 本讲源码地图

本讲几乎只围绕一个文件：

| 文件 | 作用 | 本讲关注点 |
|---|---|---|
| [Actions.py](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py) | 全部「动作」函数与命令行入口 | `ListDependencies`、`CheckDependency`、`CheckCompatibility`、`ExecMain` |

辅助理解（不在本讲重点，但会被引用）：

| 文件 | 作用 |
|---|---|
| [Dependency.py](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Dependency.py) | `Dependency` 数据模型 |
| [VersionNr.py](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/VersionNr.py) | 版本号解析与比较 |

`Actions.py` 的总体布局（行号）：

```
17-29   URL 替换相关定义
31-37   CHECKOUT_MODE 枚举
42-61   CheckCompatibility(...)      <- 被 CheckDependency 调用
66-72   ListDependencies(...)        <- 本讲主角之一
74-91   CheckDependency(...)         <- 本讲主角之二
93-133  Checkout(...)                <- u3-l2 详解
135-170 ExecMain(...)                <- 命令行入口
```

## 4. 核心概念与源码讲解

### 4.1 ListDependencies：依赖列表的格式化输出

#### 4.1.1 概念说明

`ListDependencies` 是三个动作里最简单的一个：它只做「读」，不做任何文件系统改动，也不切换目录。它的职责就是把一张 `Dependency` 列表**逐条格式化打印**到标准输出，让用户一眼看清「我到底依赖了哪些库、从哪里拉、最低要求什么版本」。

它对应命令行开关 `-list`。典型使用场景是：在动手克隆之前，先看一下自己声明了哪些依赖。

#### 4.1.2 核心流程

```
对 deps 中的每一条 dep：
    打印 "libraryName - url - minVersion"
```

只有一层循环、一行 `print`，没有分支、没有副作用。注意三点：

1. 输出顺序就是 `deps` 列表的顺序（`Parse.FromReadme` 产生的顺序）。
2. `minVersion` 是 `VersionNr` 对象，`"{}".format(...)` 会隐式调用 `VersionNr.__str__`，所以打印出来是规整的 `major.minor.bugfix`。
3. 函数本身**不打印表头**（如 `*** Dependencies ***`）；表头由调用方 `ExecMain` 在调用前打印。

#### 4.1.3 源码精读

完整函数（仅 3 行有效代码）：

[ListDependencies 定义，Actions.py:66-72](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L66-L72)

```python
def ListDependencies(deps : List[Dependency]):
    for dep in deps:
        print("{} - {} - {}".format(dep.libraryName, dep.url, dep.minVersion))
```

逐字解读这一行 `print`：

- `{0}` = `dep.libraryName` → 库名，例如 `PSI_FpgaCommon`。
- `{1}` = `dep.url` → git 远程地址，例如 `https://git.psi.ch/GFA/FpgaCommon.git`。
- `{2}` = `dep.minVersion` → `VersionNr` 对象，经 `__str__` 变成 `1.2.0` 这样的字符串。

所以一条依赖最终长这样：

```
PSI_FpgaCommon - https://git.psi.ch/GFA/FpgaCommon.git - 1.2.0
```

调用方打印表头的地方（`-list` 分支）：

[ExecMain 中 -list 分发，Actions.py:151-153](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L151-L153)

```python
if args.list:
    print("*** Dependencies ***")
    ListDependencies(dependencies)
```

因此完整 `-list` 输出形如：

```
*** Dependencies ***
PSI_FpgaCommon - https://git.psi.ch/GFA/FpgaCommon.git - 1.2.0
PSI_FpgaRegs   - https://git.psi.ch/GFA/FpgaRegs.git   - 2.0.1
```

#### 4.1.4 代码实践

**实践目标**：亲手构造几条 `Dependency`，调用 `ListDependencies`，验证输出格式与你的预测一致。

**操作步骤**（库式调用，见 u1-l3）：

```python
# practice_list.py
from PsiFpgaLibDependencies import Dependency
from PsiFpgaLibDependencies import Actions

deps = [
    Dependency("lib_alpha", "https://example.com/alpha.git", "../libs/alpha", "1.2.0"),
    Dependency("lib_beta",  "https://example.com/beta.git",  "../libs/beta",  "2.0.1"),
    Dependency("lib_gamma", "https://example.com/gamma.git", "../libs/gamma", "1.10.3"),
]

Actions.ListDependencies(deps)
```

> 说明：本包是「库」，没有可执行入口（u1-l3）。若尚未安装本包，可先 `pip3 install dist/` 下的归档（u1-l2），或把仓库根目录加入 `PYTHONPATH` 后再用 `python3 practice_list.py` 运行。

**需要观察的现象**：

1. 每行三段，以 ` - ` 分隔。
2. `lib_gamma` 的版本打印为 `1.10.3`，**不是** `1.1.3`——这验证了 `VersionNr.__str__` 按整数段还原（去前导零、保留进位），而不是字符串截断。

**预期结果**：

```
lib_alpha - https://example.com/alpha.git - 1.2.0
lib_beta - https://example.com/beta.git - 2.0.1
lib_gamma - https://example.com/gamma.git - 1.10.3
```

> 若运行环境与本讲描述不符（例如包未安装、导入失败），请在本地核实后记录实际输出（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `dep.url` 改成 `None`，`ListDependencies` 还能正常打印吗？

**参考答案**：能打印。`"{}".format(None)` 得到字符串 `"None"`，这一行会变成 `lib_alpha - None - 1.2.0`。`ListDependencies` 不校验字段合法性，它只负责格式化——校验发生在更上游（`Parse.FromReadme` 解析时强制要求 url 存在，见 u2-l3）。

**练习 2**：为什么 `dep.minVersion` 明明声明为 `str`（构造函数签名），打印出来却是一个规整的版本号？

**参考答案**：因为 `Dependency.__init__` 里执行了 `self.minVersion = VersionNr(minVersion)`（u2-l1），运行时 `dep.minVersion` 已是 `VersionNr` 对象；`format` 触发 `VersionNr.__str__`，输出 `major.minor.bugfix`。签名上的 `str` 只是「输入契约」，不是「运行时类型」。

---

### 4.2 CheckDependency：目录切换与逐条存在性判断

#### 4.2.1 概念说明

`CheckDependency` 对应命令行开关 `-check`，它回答的问题是：**我声明的这些依赖，在本地是否都已经存在？** 它不做任何克隆，只「看」。

它和 `ListDependencies` 的本质区别有两点：

1. 它需要访问真实的文件系统，因此必须知道「相对于哪个根目录去判断」——这就是参数 `rootdir`。
2. 它对每条依赖做一个**二选一判定**：目录存在 → 交给 `CheckCompatibility` 进一步校验版本；目录不存在 → 打印一行 `ERROR`。

注意：`ERROR` 只是打印到标准输出，**不会抛异常、不会中断循环**。哪怕有 5 条依赖全都不存在，它会打印 5 行 `ERROR`，然后正常返回。这一点对理解它的「检查报告」语义很重要。

#### 4.2.2 核心流程

```
保存原工作目录 oldDir
try:
    chdir(rootdir)                      # 把 CWD 切到 rootdir
    对每条 dep：
        打印 "-- libraryName --"        # 分隔行
        depPathAbs = abspath(dep.relativePath)   # 相对 rootdir 解析成绝对路径
        if isdir(depPathAbs):           # 目录存在
            CheckCompatibility(rootdir, dep, True)   # 进一步校验版本（详见 u3-l3）
        else:                           # 目录不存在
            打印 "ERROR: Dependency <relativePath> does not exist"
finally:
    chdir(oldDir)                       # 无论上面是否异常，都恢复 CWD
```

这里有一个关键设计：**为什么要先 `chdir(rootdir)`？** 因为 `dep.relativePath` 是相对于 `rootdir` 的路径（u2-l5），不是相对于调用方当前所在目录的路径。只有先站到 `rootdir` 上，`relativePath` 才能被正确解析。

> 细节：切到 `rootdir` 之后，`os.path.abspath(dep.relativePath)` 和直接 `os.path.isdir(dep.relativePath)` 的判定结果其实相同（因为 CWD 已经是 `rootdir`）。代码用 `abspath` 是为了得到一个绝对路径对象 `depPathAbs`，使后续判断更直观、也方便调试。这不是必须的，但无害。

#### 4.2.3 源码精读

完整函数：

[CheckDependency 定义，Actions.py:74-91](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L74-L91)

```python
def CheckDependency(rootdir : str, deps : List[Dependency]):
    oldDir = os.path.abspath(os.curdir)
    try:
        os.chdir(rootdir)
        for dep in deps:
            print("-- {} --".format(dep.libraryName))
            depPathAbs = os.path.abspath(dep.relativePath)
            if os.path.isdir(depPathAbs):
                CheckCompatibility(rootdir, dep, True)
            else:
                print("ERROR: Dependency {} does not exist".format(dep.relativePath))
    finally:
        os.chdir(oldDir)
```

分段精读：

**第 80 行** `oldDir = os.path.abspath(os.curdir)`：在改动 CWD **之前**，先把当前 CWD 的绝对路径存下来。为什么用 `abspath(os.curdir)` 而不是直接存 `os.curdir`？因为 `os.curdir` 就是字符串 `"."`，如果存 `"."`，最后 `os.chdir(".")` 会指向「执行 `finally` 那一刻的 CWD」，而不是最初的位置。**必须保存绝对路径**才能可靠还原。这一点会在 4.3 节专门展开。

**第 82 行** `os.chdir(rootdir)`：把整个进程的 CWD 切到 `rootdir`，使后续所有相对路径都以此为基准。

**第 83-84 行** 循环 + 打印分隔行 `-- libraryName --`，让每条依赖的检查结果在输出里成块、可读。

**第 85-89 行** 存在性二选一判定：

[存在性判定，Actions.py:85-89](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L85-L89)

```python
depPathAbs = os.path.abspath(dep.relativePath)
if os.path.isdir(depPathAbs):
    CheckCompatibility(rootdir, dep, True)
else:
    print("ERROR: Dependency {} does not exist".format(dep.relativePath))
```

注意 `ERROR` 行里打印的是 `dep.relativePath`（相对路径），而不是 `depPathAbs`（绝对路径）——这是为了报告对用户更友好、可移植，让人一眼看出「相对于 rootdir 缺了哪个目录」。

**第 90-91 行** `finally: os.chdir(oldDir)`：兜底恢复，详见 4.3。

**存在的分支调用 CheckCompatibility**：当目录存在时，`CheckDependency` 把版本校验的工作「委托」给 `CheckCompatibility`，并传入 `printOk=True`（让它在版本 OK 时也打印一行 `OK (...)`）。`CheckCompatibility` 内部同样有自己的 `chdir` + `try/finally`：

[CheckCompatibility 定义，Actions.py:42-61](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L42-L61)

```python
def CheckCompatibility(rootdir : str, dep : Dependency, printOk : bool):
    oldDir = os.path.abspath(os.curdir)
    try:
        rootdir = os.path.abspath(rootdir)
        os.chdir(rootdir)
        os.chdir(dep.relativePath)
        versionFoundStr = subprocess.check_output("git describe --tags").decode().split("-")[0]
        versionFound = VersionNr(versionFoundStr)
        if versionFound.major > dep.minVersion.major:
            print("WARNING: Major mismatch, maybe incompatible. Required {}, Found {}".format(dep.minVersion, versionFound))
            return
        if versionFound < dep.minVersion:
            print("ERROR: Version lower than required. Required {}, Found {}".format(dep.minVersion, versionFound))
            return
        if printOk:
            print("OK ({})".format(versionFound))
    finally:
        os.chdir(oldDir)
```

本讲只需理解它对 `CheckDependency` 的两点影响（详细的 `git describe`、major 越界、版本下限判定留到 u3-l3）：

1. 它会用 `git describe --tags` 取已检出依赖的实际版本并和 `minVersion` 比较，打印 `OK (...)` / `WARNING ...` / `ERROR ...`。
2. 它有自己的 `try/finally`，返回前会把 CWD 还原到「进入 `CheckCompatibility` 之前」的状态——也就是 `rootdir`（因为 `CheckDependency` 此刻正站在 `rootdir` 上）。所以两层 `chdir` 不会互相污染。

最终 `-check` 的输出（假设 `lib_alpha` 存在且版本 OK、`lib_beta` 缺失）形如：

```
*** Dependency Check ***
-- lib_alpha --
OK (1.2.0)
-- lib_beta --
ERROR: Dependency ../libs/beta does not exist
```

表头 `*** Dependency Check ***` 同样由 `ExecMain` 打印：

[ExecMain 中 -check 分发，Actions.py:155-157](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L155-L157)

```python
if args.check:
    print("*** Dependency Check ***")
    CheckDependency(repoPath, dependencies)
```

#### 4.2.4 代码实践

**实践目标**：构造一个 `rootdir`，让它「缺一个依赖」，运行 `CheckDependency`，捕获 `ERROR` 行；并验证「缺失不会中断循环」。

**操作步骤**：

```python
# practice_check.py
import os, tempfile
from PsiFpgaLibDependencies import Dependency
from PsiFpgaLibDependencies import Actions

rootdir = tempfile.mkdtemp()              # 一个空的临时目录作为 rootdir
# 注意：这里刻意不创建任何子目录，让所有依赖都"缺失"

deps = [
    Dependency("lib_alpha", "https://example.com/alpha.git", "../libs/alpha", "1.2.0"),
    Dependency("lib_beta",  "https://example.com/beta.git",  "../libs/beta",  "2.0.1"),
]

Actions.CheckDependency(rootdir, deps)
```

**需要观察的现象**：

1. 出现两行 `ERROR: Dependency ../libs/xxx does not exist`。
2. 第一条缺失**没有**让程序崩溃，循环继续处理第二条——说明 `ERROR` 是「报告」而非「中断」。

**预期结果**：

```
-- lib_alpha --
ERROR: Dependency ../libs/alpha does not exist
-- lib_beta --
ERROR: Dependency ../libs/beta does not exist
```

> 关于「目录存在」的分支：它会进入 `CheckCompatibility`，而后者要求该目录是一个带 tag 的 git 仓库（执行 `git describe --tags`）。构造一个真实 git 仓库的端到端实践放在本讲「综合实践」中；此处先聚焦缺失分支（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：`CheckDependency` 打印 `ERROR` 后会停止吗？如果 3 条依赖全部缺失，输出会有几行 `ERROR`？

**参考答案**：不会停止。`ERROR` 只是普通 `print`，不在循环里 `raise` 或 `break`。3 条全缺会打印 3 行 `ERROR`，函数随后正常返回。

**练习 2**：`ERROR` 行里为什么打印 `dep.relativePath`（如 `../libs/beta`），而不是 `depPathAbs`（如 `/tmp/xxx/../libs/beta`）？

**参考答案**：相对路径更短、更可读，且与 `README` 里声明的写法一致，便于用户定位「相对 rootdir 缺了哪个目录」。`depPathAbs` 只在 `isdir` 判断时内部使用。

**练习 3**：如果不先 `os.chdir(rootdir)`，直接 `os.path.isdir(dep.relativePath)` 会发生什么？

**参考答案**：`relativePath` 会被按**调用方当前的 CWD** 解析，而不是按 `rootdir` 解析，于是判断的目录根本不是声明里所指的那个——几乎必然误判（要么全不存在，要么误以为存在）。`chdir(rootdir)` 是让 `relativePath` 的语义「对齐」的必要前提。

---

### 4.3 os.chdir + try/finally：工作目录的恢复保证

#### 4.3.1 概念说明

本节抽出一个贯穿 `Actions.py` 的通用模式。`os.chdir` 改的是**进程级全局状态**（CWD）。一旦某个函数改了 CWD 却没还原，进程里**后续所有**相对路径操作（包括别的库、别的线程逻辑）都会基于错误的位置执行——这是一个隐蔽且致命的副作用 bug。

`Actions.py` 里三个动作函数 `CheckDependency`、`CheckCompatibility`、`Checkout` **无一例外**都套用了同一个骨架：

```python
oldDir = os.path.abspath(os.curdir)   # 1. 进门前：存绝对路径
try:
    os.chdir(...)                      # 2. 改 CWD，做正事（可能抛异常）
    ...
finally:
    os.chdir(oldDir)                   # 3. 出门时：无论如何都还原
```

这保证了一个**不变式（invariant）**：

\[ \text{CWD}_{\text{函数返回后}} \;=\; \text{CWD}_{\text{函数调用前}} \]

无论 `try` 块是正常返回、提前 `return`，还是抛了异常上抛，这个等式都成立。`finally` 是让等式成立的关键：它无视 `try` 内部的控制流（包括异常），强制执行还原。

#### 4.3.2 核心流程

把模式看成一个状态机，以 `CheckDependency` 为例：

```
状态 S0: CWD = D_before                       （调用方所在目录）
    │ oldDir := abspath(D_before)             ← 必须存绝对路径！
    ▼
状态 S1: 进入 try
    │ chdir(rootdir)  → CWD = rootdir         （全局状态被改）
    │   循环里：isdir / CheckCompatibility      （可能抛异常）
    ▼
状态 S2: 离开 try（正常 / return / 抛异常 三者其一）
    │ finally 块强制执行
    ▼
状态 S3: chdir(oldDir) → CWD = D_before       （不变式恢复）
    │ 若 try 抛了异常 → 异常继续向上传播
    ▼
返回调用方
```

两个易错点：

1. **必须存绝对路径**。`oldDir = os.path.abspath(os.curdir)`，不能写成 `oldDir = os.curdir`（即 `"."`）。原因见 4.3.3。
2. **`finally` 在 `return` 之后、函数真正退出之前执行**。`CheckCompatibility` 里有 `return`（major 越界、版本过低两个分支），但 `finally` 依然会先还原 CWD 再让 `return` 生效——所以这些提前返回也是安全的。

#### 4.3.3 源码精读

**为什么必须 `os.path.abspath(os.curdir)`？**

[CheckDependency 保存原目录，Actions.py:80-82](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L80-L82)

```python
oldDir = os.path.abspath(os.curdir)
try:
    os.chdir(rootdir)
```

设想一个反例：假如写成 `oldDir = os.curdir`（即 `oldDir = "."`）。那么：

- 进门前 CWD 是 `/home/me/project`，`oldDir = "."`。
- `chdir(rootdir)` 之后 CWD 变成 `/tmp/rootdir`。
- `finally` 里执行 `os.chdir(".")`——此刻 `.` 指向的是**当前** CWD，即 `/tmp/rootdir`，于是「恢复」成了留在 `rootdir` 里，**没有**回到 `/home/me/project`。

而 `os.path.abspath(os.curdir)` 在进门那一刻就把 `.` 解析成了 `/home/me/project` 这个**绝对**字符串，存的是快照，与之后的 CWD 变化无关，因此 `os.chdir(oldDir)` 能精确回到原点。

> 一句话：**存快照（绝对路径），不要存引用（`"."`）**。

**两层 chdir 不互相污染**：`CheckDependency`（站在 `rootdir`）调用 `CheckCompatibility` 时，后者进门存的是 `rootdir`（即当时的 CWD），自己做两次 `chdir`（到 `rootdir` 再到 `dep.relativePath`），`finally` 还原回 `rootdir`。于是控制权回到 `CheckDependency` 时，CWD 仍是 `rootdir`，外层循环的下一条 `dep` 的相对路径解析不受影响。

[CheckCompatibility 的 finally 兜底，Actions.py:60-61](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L60-L61)

```python
    finally:
        os.chdir(oldDir)
```

[CheckDependency 的 finally 兜底，Actions.py:90-91](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L90-L91)

```python
    finally:
        os.chdir(oldDir)
```

**`git describe --tags` 失败会怎样？** 在「目录存在但不是 git 仓库」时，`CheckCompatibility` 第 49 行的 `subprocess.check_output("git describe --tags")` 会抛 `CalledProcessError`。此时 `CheckDependency` 的 `finally` 仍会执行 `os.chdir(oldDir)`，把 CWD 还原；之后该异常继续向上传播给调用方。也就是说：**即便发生异常，工作目录也不会被遗留在一个错误的位置**——这正是该模式的价值。

#### 4.3.4 代码实践

**实践目标**：亲手证明「即使动作函数内部抛了异常，CWD 也会被 `finally` 还原」。

**操作步骤**：故意构造一个「目录存在但不是 git 仓库」的依赖，让 `CheckCompatibility` 在 `git describe --tags` 处抛异常，然后检查调用前后 CWD 是否一致。

```python
# practice_finally.py
import os, tempfile
from PsiFpgaLibDependencies import Dependency
from PsiFpgaLibDependencies import Actions

rootdir = tempfile.mkdtemp()
existing = os.path.join(rootdir, "existing")
os.makedirs(existing)        # 一个"存在但不是 git 仓库"的目录

deps = [Dependency("existing", "https://example.com/existing.git", "existing", "1.0.0")]

before = os.getcwd()
try:
    Actions.CheckDependency(rootdir, deps)
except Exception as e:
    print("Caught exception:", type(e).__name__)
after = os.getcwd()

print("CWD before:", before)
print("CWD after :", after)
print("Restored  :", before == after)
```

**需要观察的现象**：

1. 会捕获到一个异常（`CalledProcessError`，来自 `git describe --tags` 在非 git 目录上的失败）。
2. 打印的 `-- existing --` 出现在异常之前。
3. `Restored: True`——尽管抛了异常，CWD 仍被还原到调用前的目录。

**预期结果**（异常类型与文本以本地为准，待本地验证）：

```
-- existing --
Caught exception: CalledProcessError
CWD before: /home/me/project
CWD after : /home/me/project
Restored  : True
```

> 若把 4.2 节 `CheckDependency` 的 `finally` 临时注释掉（仅用于实验，**勿提交**），你会看到 `Restored: False`、CWD 停在 `rootdir`——以此反证 `finally` 的作用。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `oldDir = os.path.abspath(os.curdir)` 不能换成 `oldDir = os.curdir`？

**参考答案**：`os.curdir` 是常量 `"."`，它表示「当前 CWD」。存 `"."` 后，中途任何 `chdir` 都会让 `.` 指向新位置；`finally` 里 `os.chdir(".")` 会停在「执行 `finally` 那一刻的 CWD」，而不是进门前的目录。`abspath` 在进门瞬间把 `.` 解析成绝对路径快照，与后续 CWD 变化解耦，从而可靠还原。

**练习 2**：`CheckCompatibility` 在 major 越界分支里写了 `return`，这个 `return` 会跳过 `finally` 吗？

**参考答案**：不会。Python 规定 `try` 块里的 `return` 会先执行对应的 `finally` 块、再真正返回。所以 `return` 前 `os.chdir(oldDir)` 照常执行，CWD 被还原后函数才返回。这正是用 `finally`（而非在每条 `return` 前手写 `chdir`）的好处：单一还原点，不会漏。

**练习 3**：`CheckDependency` 里嵌套调用了 `CheckCompatibility`，两层都改了 CWD。为什么外层循环里下一条 `dep` 的 `relativePath` 解析不会被内层弄乱？

**参考答案**：`CheckCompatibility` 的 `try/finally` 保证它返回时 CWD 恢复到「进入它之前」的状态，也就是外层 `CheckDependency` 设定的 `rootdir`。所以控制权回到外层循环时 CWD 仍是 `rootdir`，下一条 `dep.relativePath` 仍正确按 `rootdir` 解析。两层各自管好自己的「进出门还原」，互不污染。

---

## 5. 综合实践

设计一个贯穿本讲三个模块的端到端小任务：搭一个真实的 `rootdir`，里面放**一个真正的、打了 tag 的 git 仓库**（模拟「依赖已检出」）和**一个故意缺失的目录**，然后依次运行 `ListDependencies` 和 `CheckDependency`，把「格式化输出 / 存在性判定 / finally 还原」三件事一次性看到。

**操作步骤**：

```python
# practice_integration.py
import os, subprocess, tempfile
from PsiFpgaLibDependencies import Dependency
from PsiFpgaLibDependencies import Actions

rootdir = tempfile.mkdtemp()

# 1) 准备一个真实的、打了 tag 的依赖仓库 existing（模拟"已检出"）
dep_repo = os.path.join(rootdir, "existing")
os.makedirs(dep_repo)
def g(args, cwd):
    subprocess.call(["git"] + args, cwd=cwd,
                    env={**os.environ,
                         "GIT_AUTHOR_NAME": "demo", "GIT_AUTHOR_EMAIL": "demo@example.com",
                         "GIT_COMMITTER_NAME": "demo", "GIT_COMMITTER_EMAIL": "demo@example.com"})
g(["init"], dep_repo)
with open(os.path.join(dep_repo, "README.md"), "w") as f:
    f.write("# existing\n")
g(["add", "."], dep_repo)
g(["commit", "-m", "init"], dep_repo)
g(["tag", "1.2.3"], dep_repo)     # 实际版本 1.2.3，>= 声明的 1.0.0

# 2) 依赖列表：existing 存在且版本够；missing 故意缺失
deps = [
    Dependency("existing", "https://example.com/existing.git", "existing", "1.0.0"),
    Dependency("missing",  "https://example.com/missing.git",  "missing",  "1.0.0"),
]

# 3) 列出
print("*** Dependencies ***")
Actions.ListDependencies(deps)

# 4) 检查，并验证 CWD 被还原
print("*** Dependency Check ***")
before = os.getcwd()
Actions.CheckDependency(rootdir, deps)
print("CWD restored:", os.getcwd() == before)
```

**需要观察的现象**：

1. `ListDependencies` 输出两行，`missing` 的版本显示为 `1.0.0`。
2. `CheckDependency` 对 `existing` 打印 `OK (1.2.3)`（`git describe --tags` 得到 `1.2.3`，未低于 `minVersion`，且 major 未越界）；对 `missing` 打印 `ERROR`。
3. 整个调用结束后，CWD 与调用前一致（`CWD restored: True`）。

**预期结果**（待本地验证：需本机已安装 git、且已安装/可导入本包）：

```
*** Dependencies ***
existing - https://example.com/existing.git - 1.0.0
missing - https://example.com/missing.git - 1.0.0
*** Dependency Check ***
-- existing --
OK (1.2.3)
-- missing --
ERROR: Dependency missing does not exist
CWD restored: True
```

**反思题**（结合输出回答）：

- 若把 `existing` 的 `minVersion` 从 `1.0.0` 改成 `2.0.0`，`OK (1.2.3)` 会变成什么？为什么？（提示：`1.2.3 < 2.0.0`，进入 `CheckCompatibility` 的第二个 `if`。）
- 若把 `existing` 仓库的 tag 改成 `9.9.9`（major 远大于声明的 `1.0.0`），会打印哪一行？为什么 `major` 越界是 `WARNING` 而不是 `ERROR`？

> 这两题的答案都在 [CheckCompatibility，Actions.py:52-57](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L52-L57)；完整的版本判定逻辑是 u3-l3 的主题。

## 6. 本讲小结

- `ListDependencies` 是纯读、无副作用的格式化函数，每条依赖打印成 `libraryName - url - minVersion`，`minVersion` 经 `VersionNr.__str__` 还原为 `major.minor.bugfix`；表头 `*** Dependencies ***` 由 `ExecMain` 打印。
- `CheckDependency` 先 `chdir(rootdir)` 让 `dep.relativePath` 的基准对齐，再逐条用 `os.path.isdir` 做二选一判定：存在则委托 `CheckCompatibility`（`printOk=True`）校验版本，不存在则打印 `ERROR`。
- `ERROR` 是「报告」不是「中断」：缺失不会抛异常、不会停止循环，函数处理完所有依赖后正常返回。
- 三个动作函数（`CheckDependency`、`CheckCompatibility`、`Checkout`）共用一套 `os.chdir` + `try/finally` 骨架，保证不变式 \(\text{CWD}_{\text{返回后}} = \text{CWD}_{\text{调用前}}\)，即便抛异常或提前 `return` 也成立。
- 还原的关键细节：进门时必须用 `os.path.abspath(os.curdir)` 存**绝对路径快照**，而不是存常量 `"."`，否则 `finally` 里的 `chdir(".")` 会指向错误位置。
- 两层嵌套调用之所以不互相污染 CWD，是因为每一层都各自负责「进出门还原」，内层返回时把 CWD 恢复到外层设定的 `rootdir`。

## 7. 下一步学习建议

- **u3-l2 检出依赖与检出模式**：继续读 `Actions.Checkout`，看它如何用 `git clone --recurse-submodules` 或 `git submodule add` 把缺失依赖拉下来，以及 `master/latest_release/specified_version` 三种 `CHECKOUT_MODE` 的差异——它同样套用了本讲的 `try/finally` 骨架，可作为本讲模式的第二个实例。
- **u3-l3 语义版本兼容性校验**：深入 `CheckCompatibility` 内部，搞清 `git describe --tags` 取 tag、`major` 越界给 `WARNING`、版本低于下限给 `ERROR`、否则 `OK` 的完整判定，并理解它与 `VersionNr` 比较运算的配合。本讲的综合实践已经为它做了铺垫。
- **动手验证建议**：把本讲的三个 `practice_*.py` 在本地跑一遍，尤其是 4.3 节「注释掉 `finally` 反证」的实验，亲眼看到 CWD 是否被遗留——这是最直观的理解方式。
