# 检出依赖与检出模式（Checkout / CHECKOUT_MODE）

## 1. 本讲目标

学完本讲，读者应该能够：

- 说清 `Checkout` 函数对每条依赖做了什么：**已存在则跳过并查版本**，**不存在则克隆到正确的父目录**。
- 区分 `asSubmodule=False/True` 两条分支：**普通 clone（`git clone --recurse-submodules`）** 与 **子模块添加（`git submodule add`）**。
- 解释 `CHECKOUT_MODE` 三种枚举值（`Master` / `LatestRelease` / `SpecifiedRelease`）的语义差异，以及命令行字符串 `master / latest_release / specified_version` 如何映射到它们。
- 理解 `LatestRelease` 模式的核心算法：**用 `git tag` 取出所有标签 → 用 `VersionNr` 解析 → 用 `max()` 取最大语义版本 → `git checkout`**。
- 能够在本地用 `file://` 仓库构造一条依赖，分别以 `master` 和 `latest_release` 模式调用 `Checkout`，并对比检出结果。

## 2. 前置知识

本讲假定你已经掌握以下内容（来自前置讲义）：

- **`Dependency` 四字段模型**（[u2-l1](u2-l1-dependency-model.md)）：`libraryName`（克隆后的目录名）、`url`（git 远程地址）、`relativePath`（相对于 rootdir 的落点）、`minVersion`（最低版本，运行时是 `VersionNr` 对象）。
- **`Dependency.GetParentDir()`**：返回 `relativePath` 砍掉最后一段后的父目录（保留所有 `..` 前缀），本讲的克隆落点就由它决定。
- **`VersionNr` 的语义比较**（[u2-l2](u2-l2-versionnr.md)）：版本号按 `(major, minor, bugfix)` 逐段整数比较，所以 `1.10.0 > 1.2.0`；`VersionNr` 只显式实现了 `__eq__` 与 `__gt__`，但 Python 的反射机制让 `max()` 在只有 `__gt__` 时也能正常工作。
- **`CheckDependency` 与 `os.chdir + try/finally` 骨架**（[u3-l1](u3-l1-list-check.md)）：进门用 `os.path.abspath(os.curdir)` 存绝对路径快照，`finally` 还原当前工作目录（CWD）。本讲的 `Checkout` 沿用同一套骨架。

此外需要一点 git 基础：

- `git clone --recurse-submodules <url> <dir>`：把远程仓库克隆到 `<dir>`，并递归拉取它的子模块。
- `git submodule add <url> <dir>`：把 `<url>` 作为子模块登记到**当前仓库**的 `.gitmodules`，并检出到 `<dir>`。
- `git tag`：列出本地仓库的所有标签（每行一个）。
- `git checkout <tag>`：把工作区切换到指定标签指向的提交（处于 detached HEAD 状态）。

> 名词解释：**检出（checkout）** 在本项目中特指「把依赖仓库弄到本地磁盘上」这一动作，既可能是普通克隆，也可能是登记为子模块；不要和 `git checkout` 命令混淆——后者只是实现细节之一。

## 3. 本讲源码地图

本讲几乎全部围绕 `Actions.py` 展开，并用到 `Dependency.py` 的一个方法：

| 文件 | 作用 | 本讲用到的地方 |
| --- | --- | --- |
| `Actions.py` | 全部动作（列出/检查/检出）与命令行入口 | `CHECKOUT_MODE` 枚举、`Checkout` 函数、`ExecMain` 的 mode 映射、`URL_REPLACEMENTS`、`CheckCompatibility` |
| `Dependency.py` | 依赖数据模型 | `GetParentDir()`——决定克隆落点的父目录 |
| `VersionNr.py` | 语义版本号 | `LatestRelease` 模式用 `VersionNr(tag)` 解析每个 tag，再用 `max()` 取最大 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

1. **`CHECKOUT_MODE` 枚举与命令行字符串映射**——三种「检出意图」从哪里来。
2. **`Checkout` 函数总骨架：跳过 vs 检出**——逐条遍历、判存在、建父目录、还原 CWD。
3. **clone 与 submodule 两条分支**——`asSubmodule` 如何切换克隆方式，URL 如何被链式替换。
4. **`LatestRelease`：用 `git tag` + `VersionNr` 取最大版本**——本讲的算法重点。

### 4.1 CHECKOUT_MODE 枚举与命令行字符串映射

#### 4.1.1 概念说明

「检出一条依赖」听起来是一个动作，但其实有**三种不同的意图**：

- 我就想拿到依赖的**最新开发版**（master 分支 HEAD）。
- 我想拿到依赖的**最新正式发布版**（语义版本号最大的那个 tag）。
- 我想拿到依赖的**指定版本**（README 里 `minVersion` 声明的那个精确 tag）。

这三种意图被建模成一个枚举 `CHECKOUT_MODE`，作为 `Checkout` 函数的入参之一。命令行用户不会直接输入枚举对象，而是输入字符串（`master` / `latest_release` / `specified_version`），再由 `ExecMain` 翻译成枚举。

> 注意一个易混淆点：枚举成员叫 `SpecifiedRelease`，而命令行字符串是 `specified_version`，两者字面不一样，靠 `ExecMain` 里的一张映射表对应。

#### 4.1.2 核心流程

```text
命令行: -mode latest_release
   │
   ▼  ExecMain 用 argparse 解析（choices 限定取值）
字符串 "latest_release"
   │
   ▼  if/elif 映射表
枚举 CHECKOUT_MODE.LatestRelease
   │
   ▼  传入 Checkout(..., mode=..., ...)
Checkout 内部 if mode == ...: 执行对应分支
```

#### 4.1.3 源码精读

枚举定义在文件靠前的「Definitions」区：

[Actions.py:31-37](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L31-L37) — 用 `Enum` 定义三种检出模式，整数值只是占位、本身无业务含义：

```python
class CHECKOUT_MODE(Enum):
    Master = 0
    LatestRelease = 1
    SpecifiedRelease = 2
```

命令行侧，`ExecMain` 用 `argparse` 声明 `-mode` 参数，**用 `choices` 限定合法取值**，并**默认 `latest_release`**：

[Actions.py:146](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L146) — 注意 `default="latest_release"`，即命令行不写 `-mode` 时走的是最新发布版，而不是 master。

随后 `ExecMain` 在分发 `-checkout` 时把字符串翻译成枚举：

[Actions.py:160-170](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L160-L170) — 字符串到枚举的映射表，非法值会抛异常：

```python
if args.checkout:
    print("*** Checkout ***")
    if args.mode == "latest_release":
        mode = CHECKOUT_MODE.LatestRelease
    elif args.mode == "master":
        mode = CHECKOUT_MODE.Master
    elif args.mode == "specified_version":
        mode = CHECKOUT_MODE.SpecifiedRelease
    else:
        raise Exception("Illegel -mode: {}".format(args.mode))
    Checkout(repoPath, dependencies, mode, args.as_submodule)
```

> 一个值得记住的细节：`Checkout` **函数签名**的默认值是 `CHECKOUT_MODE.Master`（见 4.2.3），而 `ExecMain` 的命令行**默认值**是 `latest_release`。也就是说，「直接当库调用 `Checkout()` 不传 mode」与「走命令行不写 `-mode`」会得到**不同的默认行为**。这是初学者最容易踩的坑。

#### 4.1.4 代码实践

**实践目标**：验证三种命令行字符串都能被正确映射，并确认「不写 `-mode`」时的默认行为。

**操作步骤**：

1. 在本仓库根目录写一个最小驱动脚本 `drive_cli.py`（**示例代码**，可放在仓库外或临时目录，避免污染源码树）：

   ```python
   # 示例代码：模拟客户端调用 ExecMain
   from PsiFpgaLibDependencies import Actions
   from PsiFpgaLibDependencies.Dependency import Dependency

   # 一条指向本地 file:// 仓库的依赖（url 请替换成你自己的路径）
   dep = Dependency("demo_lib", "file:///tmp/demo_repo.git", "DemoLib/demo_lib", "1.0.0")
   Actions.ExecMain("/tmp/work", [dep])
   ```

2. 分别运行下面三条命令（依赖目录先确保不存在，以免被「跳过」分支跳过）：

   ```bash
   python3 drive_cli.py -checkout -mode master
   python3 drive_cli.py -checkout -mode latest_release
   python3 drive_cli.py -checkout            # 不写 -mode，观察默认
   ```

3. 再故意输入一个非法字符串，观察 `argparse` 与 `raise Exception` 两种拦截：

   ```bash
   python3 drive_cli.py -checkout -mode foobar
   ```

**需要观察的现象**：

- 前两条命令能进入 `Checkout`；第三条应与 `latest_release` 行为一致（默认值生效）。
- 非法字符串 `foobar`：因为 `choices=["master","latest_release","specified_version"]` 的限制，`argparse` 通常会**在解析阶段就报错并退出**，根本到不了 `raise Exception("Illegel -mode: ...")` 那一行。

**预期结果**：`-mode` 取值受 `choices` 约束；省略 `-mode` 等价于 `latest_release`。**待本地验证**：`argparse` 是否在你的 Python 版本下对 `foobar` 直接退出（取决于 `choices` 的优先级，建议实际跑一次确认）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `CHECKOUT_MODE.SpecifiedRelease` 改名为 `SpecifiedVersion`，`Checkout` 函数内部需要同步修改哪些地方？

**答案**：需要修改 [Actions.py:123](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L123) 处的 `if mode == CHECKOUT_MODE.SpecifiedRelease:` 这一处比较即可；命令行字符串 `specified_version` 与枚举名无关（它只经过 `ExecMain` 的映射表），不需要改。

**练习 2**：为什么 `argparse` 设了 `choices` 之后，[Actions.py:169](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L169) 的 `else: raise Exception(...)` 看起来「永远到不了」？留着它有意义吗？

**答案**：`choices` 会在命令行解析阶段拒绝非法值，所以经命令行进入时确实到不了 `else`。但 `ExecMain` 的映射逻辑作为一段独立代码，作者出于防御性编程保留了兜底分支；这也能在将来有人改动 `choices` 或绕过 `argparse` 调用时提供安全网。

---

### 4.2 Checkout 函数总骨架：跳过 vs 检出

#### 4.2.1 概念说明

`Checkout` 对**一整条依赖列表**负责，但它的策略很简单：**幂等地把缺失的依赖补齐**。

- 如果某条依赖的 `relativePath` 已经存在（说明之前克隆过），就**跳过克隆**，只调用 `CheckCompatibility` 报告一下当前版本是否满足要求。
- 如果不存在，就**进入克隆分支**，把它弄到磁盘上。

这种「先看在不在，不在才动手」的设计让 `-checkout` 可以被**重复执行**而不会报错或重复克隆。

#### 4.2.2 核心流程

```text
oldDir = 当前 CWD 的绝对路径快照          ┐
rootdir = rootdir 的绝对路径              │ 进门准备
                                          ┘
for dep in deps:
    chdir(rootdir)
    parent = abspath(dep.GetParentDir())   # 克隆落点的父目录
    if exists(dep.relativePath):           # 已存在？
        打印 "skipped, already exists"
        CheckCompatibility(...)            # 只查版本
    else:
        打印 "checkout <path>"
        若 parent 不存在则 makedirs(parent) # 保证父目录在
        chdir(parent)                      # 切到父目录准备克隆
        ...（进入 4.3 的 clone/submodule 分支）...
        ...（进入 4.4 的 mode 分支，按需切版本）...
finally:
    chdir(oldDir)                          # 无论成功失败都还原 CWD
```

#### 4.2.3 源码精读

函数签名与默认值（注意 `mode` 默认是 `Master`）：

[Actions.py:93-100](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L93-L100) — 四个入参：`rootdir`、`deps`、`mode`（默认 `Master`）、`asSubmodule`（默认 `False`）：

```python
def Checkout(rootdir : str, deps : List[Dependency], mode : CHECKOUT_MODE = CHECKOUT_MODE.Master, asSubmodule : bool = False):
```

进门准备与骨架（沿用 u3-l1 讲过的 `chdir + try/finally` 模式）：

[Actions.py:101-115](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L101-L115) — 逐条遍历，判存在，必要时建父目录：

```python
oldDir = os.path.abspath(os.curdir)
rootdir = os.path.abspath(rootdir)
try:
    for dep in deps:
        print("-- {} --".format(dep.libraryName))
        os.chdir(rootdir)
        parent = os.path.abspath(dep.GetParentDir())
        if os.path.exists(dep.relativePath):
            print("> skipped, already exists, checking version")
            CheckCompatibility(rootdir, dep, True)
        else:
            print("> checkout {}".format(dep.relativePath))
            if not os.path.exists(parent):
                os.makedirs(parent, exist_ok=True)
            os.chdir(parent)
            # ...克隆与切版本分支接在后面...
```

几个关键点逐条解释：

- **`os.chdir(rootdir)`（每轮循环开头）**：每处理一条依赖都先回到 `rootdir`，避免上一条依赖把 CWD 改到深层目录后，下一条的 `relativePath` 解析错位。
- **`parent = os.path.abspath(dep.GetParentDir())`**：`GetParentDir()` 返回相对父目录（见 [Dependency.py:28-33](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Dependency.py#L28-L33)），在 `chdir(rootdir)` 之后取 `abspath`，就把它锚定到了 `rootdir` 之下。后面 `git clone <url> <libraryName>` 会在 `parent` 里创建 `libraryName` 子目录。
- **`os.path.exists(dep.relativePath)`**：判存在的依据是相对 `rootdir` 的整条 `relativePath`（即最终的克隆目录），而不是父目录。
- **`os.makedirs(parent, exist_ok=True)`**：依赖可能落在多层嵌套目录里（例如 `../SomeLib/Demo/demo_lib`），父目录可能尚不存在，需要先递归创建；`exist_ok=True` 保证已存在时不报错。
- **`finally: os.chdir(oldDir)`**（[Actions.py:132-133](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L132-L133)）：与 u3-l1 完全相同的还原策略，保证函数返回后 CWD 与调用前一致，即使中途 `os.system` 的 git 命令失败也照样还原。

#### 4.2.4 代码实践

**实践目标**：体验「幂等重复检出」——第二次执行应命中跳过分支。

**操作步骤**：

1. 准备一个本地裸仓库（**示例命令**）：

   ```bash
   mkdir -p /tmp/demo_repo.git && cd /tmp/demo_repo.git && git init --bare
   # 另开一个工作仓库推送点内容进去（略），使其非空
   ```

2. 用 4.1.4 的驱动脚本执行两次 `-checkout -mode master`，每次执行后检查 `/tmp/work` 下是否生成 `DemoLib/demo_lib`。

**需要观察的现象**：

- 第一次：打印 `> checkout DemoLib/demo_lib`，随后执行 `git clone`，目录被创建。
- 第二次：打印 `> skipped, already exists, checking version`，**不再执行 clone**，转而调用 `CheckCompatibility` 报告版本。

**预期结果**：`-checkout` 可重复执行，已存在的依赖被跳过。**待本地验证**：`CheckCompatibility` 在 master 分支（通常没有 `major.minor.bugfix` 形式的 tag）下的具体输出。

#### 4.2.5 小练习与答案

**练习 1**：为什么每轮循环开头都要 `os.chdir(rootdir)` 一次？如果删掉这一行会怎样？

**答案**：因为克隆分支里会 `os.chdir(parent)`，把 CWD 改到深层父目录；若不在下一轮开头回到 `rootdir`，下一条依赖的 `relativePath` 就会相对错误的 CWD 解析，导致 `os.path.exists` 误判、或克隆到错误的位置。

**练习 2**：`os.makedirs(parent, exist_ok=True)` 中的 `exist_ok=True` 有什么用？

**答案**：当 `parent` 已经存在时不抛 `FileExistsError`。因为代码先用 `if not os.path.exists(parent)` 守卫过，正常情况下不会重复创建，但 `exist_ok=True` 是一层额外保险，避免在并发或路径被外部提前创建的极端情况下崩溃。

---

### 4.3 clone 与 submodule 两条分支

#### 4.3.1 概念说明

确认要克隆之后，下一个分叉是「**怎么克隆**」，由 `asSubmodule` 参数决定：

- `asSubmodule=False`（默认）：执行**普通克隆** `git clone --recurse-submodules`。依赖仓库被当作一个**独立的 git 仓库**放进 `parent/libraryName`，与当前项目之间没有 git 层面的绑定关系。
- `asSubmodule=True`：执行 `git submodule add`。依赖被**登记为当前仓库的子模块**（写入 `.gitmodules`），git 会跟踪它指向的某个提交。

两者的本质区别在于「git 是否记录这层依赖关系」。普通克隆只是把代码拉下来；子模块则把「依赖哪个仓库的哪个提交」写进了父仓库的元数据，团队协作时 `git clone --recurse-submodules` 就能一并拉取。

> 名词解释：**子模块（submodule）** 是 git 的机制，把另一个仓库嵌入当前仓库并登记其 URL 与提交号；`.gitmodules` 文件记录这层映射。命令行用 `-as_submodule` 开关启用。

#### 4.3.2 核心流程

```text
url = dep.url
for repl in URL_REPLACEMENTS:        # 链式 URL 替换（详见 u3-l4）
    url = repl(url)

if not asSubmodule:
    git clone --recurse-submodules <url> <libraryName>   # 普通克隆
else:
    git submodule add <被PSI_GFA_HTTPS_TO_SSH处理过的url> <libraryName>  # 子模块
```

#### 4.3.3 源码精读

[Actions.py:116-122](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L116-L122) — 两条克隆分支：

```python
url = dep.url
for repl in URL_REPLACEMENTS:
    url = repl(url)
if not asSubmodule:
    os.system("git clone --recurse-submodules {} {}".format(url, dep.libraryName))
else:
    os.system("git submodule add {} {}".format(PSI_GFA_HTTPS_TO_SSH(url), dep.libraryName))
```

需要注意两处**不对称**：

1. **URL 替换的次数不同**。普通克隆分支会遍历**整个 `URL_REPLACEMENTS` 列表**（当前只有一个替换函数 `PSI_GFA_HTTPS_TO_SSH`，见 [Actions.py:29](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L29)），逐个替换；而子模块分支**没有走这个循环**，而是直接又调用了一次 `PSI_GFA_HTTPS_TO_SSH(url)`。也就是说，如果将来向 `URL_REPLACEMENTS` 里新增第二个替换函数，**普通克隆分支会自动应用它，但子模块分支不会**。这是一个扩展时要注意的隐患（详见 [u3-l4](u3-l4-url-replacement.md)）。

2. **`os.system` 的返回值被忽略**。`os.system` 返回子进程的退出码，但这里没有检查。如果 `git clone` 失败（例如 URL 不通、网络中断），代码不会抛异常，而是**继续往下走**，可能进入 `mode` 分支去 `os.chdir(dep.libraryName)`，结果目录不存在，行为会变得混乱。这是阅读源码时要意识到的一个健壮性短板。

`PSI_GFA_HTTPS_TO_SSH` 的实现（URL 替换函数详见 u3-l4）：

[Actions.py:17-26](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L17-L26) — 把 PSI 内网的 HTTPS 地址换成 SSH 地址并补 `.git` 后缀：

```python
def PSI_GFA_HTTPS_TO_SSH(path : str) -> str:
    if path.startswith("https://git.psi.ch/GFA"):
        path = path.replace("https://git.psi.ch/GFA", "git@git.psi.ch:GFA")
        path += ".git"
    return path
```

#### 4.3.4 代码实践

**实践目标**：对比 `asSubmodule=False` 与 `asSubmodule=True` 两种方式在磁盘与 git 元数据上的差异。

**操作步骤**：

1. 准备**两个干净的 rootdir**（例如 `/tmp/work_clone` 与 `/tmp/work_sub`），并且 `/tmp/work_sub` 本身必须已经是一个 git 仓库（`git init`），因为 `git submodule add` 要求当前目录在某个 git 仓库内。
2. 用驱动脚本分别执行：

   ```bash
   python3 drive_cli.py -checkout -mode master                     # 普通克隆
   python3 drive_cli.py -checkout -mode master -as_submodule       # 子模块
   ```

   （两次脚本里的 `rootdir` 分别指向上面两个目录。）

3. 检查两处产物：

   ```bash
   ls -la /tmp/work_clone/DemoLib/demo_lib/.git          # 独立仓库的 .git 目录
   ls -la /tmp/work_sub/.gitmodules                      # 子模块登记文件
   cat /tmp/work_sub/.gitmodules
   ```

**需要观察的现象**：

- 普通克隆：`demo_lib` 内有自己的 `.git`，但 `/tmp/work_clone` 没有记录对它的引用。
- 子模块：`/tmp/work_sub` 多出 `.gitmodules` 文件，里面写明了 `demo_lib` 的 `path` 与 `url`；`git submodule status` 能看到它。

**预期结果**：普通克隆只把代码拉下来；子模块额外登记依赖关系。**待本地验证**：若你的依赖 URL 不以 `https://git.psi.ch/GFA` 开头，`PSI_GFA_HTTPS_TO_SSH` 不做任何替换，`git submodule add` 收到的就是原始 URL。

#### 4.3.5 小练习与答案

**练习 1**：为什么子模块分支不需要 `git clone --recurse-submodules` 里的 `--recurse-submodules`？

**答案**：`git submodule add` 本身的语义就是「登记并检出一个子模块」，它的递归需求由后续 `git submodule update --init --recursive` 等命令承担；`add` 阶段会把目标仓库拉下来并登记，不依赖 `--recurse-submodules` 这个 clone 专用的开关。

**练习 2**：假如有人向 `URL_REPLACEMENTS` 追加了第二个替换函数 `FOO_TO_BAR`，普通克隆分支与子模块分支谁会自动用上它？

**答案**：只有**普通克隆分支**会自动用上，因为它遍历了 `URL_REPLACEMENTS` 列表；子模块分支硬编码调用了 `PSI_GFA_HTTPS_TO_SSH(url)`，不会应用新函数。这是当前实现的一个不对称之处。

---

### 4.4 LatestRelease：用 git tag + VersionNr 取最大版本

#### 4.4.1 概念说明

`LatestRelease` 是三种模式里**算法含量最高**的一个。它的目标是：**在依赖仓库的所有 tag 中，选出语义版本号最大的那一个，并 checkout 过去。**

为什么不能直接用字符串排序？因为字符串字典序会得出 `"1.10.0" < "1.2.0"`（按字符逐位比，`'1'` vs `'2'`），这是错的。必须把每个 tag 解析成 `(major, minor, bugfix)` 三个整数，再按整数大小比较——这正是 [u2-l2](u2-l2-versionnr.md) 讲过的 `VersionNr` 的工作。

> 前置认知衔接：`VersionNr` 只显式实现了 `__eq__` 与 `__gt__`。Python 内置 `max()` 在比较两个对象时会调用 `>`，只要 `__gt__` 存在就足够推导出最大值——所以这里 `max(tagList)` 能正常工作，**不需要** `VersionNr` 再实现 `__lt__`。

#### 4.4.2 核心流程

```text
chdir(dep.libraryName)                 # 进入刚克隆出来的依赖目录
tags = subprocess.check_output("git tag").decode()   # 拿到所有 tag（每行一个）
tagList = [VersionNr(t) for t in tags.split("\n") if t != ""]   # 解析成版本号列表
latest = max(tagList)                  # 取语义最大版本
git checkout <latest>                  # 切到该 tag
```

用公式表达「取最大语义版本」：

\[
\text{latest} \;=\; \arg\max_{t \in \text{tags}} \bigl(t.\text{major},\; t.\text{minor},\; t.\text{bugfix}\bigr)
\]

其中 \(\arg\max\) 按 `(major, minor, bugfix)` 的**逐段整数字典序**取最大，即 `VersionNr.__gt__` 定义的那个序。

#### 4.4.3 源码精读

切版本的三个 `mode` 分支紧跟在克隆之后：

[Actions.py:123-131](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L123-L131) — `SpecifiedRelease` 切到 `minVersion`，`LatestRelease` 取最大 tag，`Master` 什么都不做（保持 clone 出来的默认状态）：

```python
if mode == CHECKOUT_MODE.SpecifiedRelease:
    os.chdir(dep.libraryName)
    os.system("git checkout {}".format(dep.minVersion))
elif mode == CHECKOUT_MODE.LatestRelease:
    os.chdir(dep.libraryName)
    tags = subprocess.check_output("git tag").decode()
    tagList = [VersionNr(tag.strip) for tag in tags.split("\n") if tag.strip() != ""]
    latest = max(tagList)
    os.system("git checkout {}".format(latest))
```

逐行解读：

- **`Master` 分支没有任何代码**：`if/elif` 里没有对应分支，意味着 `mode == Master` 时**既不切版本、也不报错**，依赖就停留在 `git clone` 后的默认状态（通常是默认分支的最新提交）。
- **`SpecifiedRelease`**：`dep.minVersion` 运行时是 `VersionNr` 对象，但 `"...".format(dep.minVersion)` 会调用它的 `__str__`（见 [VersionNr.py:39-40](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/VersionNr.py#L39-L40)），还原成 `"major.minor.bugfix"` 字符串，于是 `git checkout 1.2.0` 能正确匹配到 tag。
- **`LatestRelease`**：`subprocess.check_output("git tag")` 取到字节串，`.decode()` 转成字符串，`.split("\n")` 按行切开。每个非空 tag 用 `VersionNr(...)` 解析成可比较的版本号，最后 `max(tagList)` 取最大者。

> ⚠️ **读源码要较真（重要）**：仔细看 [Actions.py:129](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L129) 这一行：
>
> ```python
> tagList = [VersionNr(tag.strip) for tag in tags.split("\n") if tag.strip() != ""]
> ```
>
> 注意 `VersionNr(tag.strip)` 里的 `tag.strip` **没有括号**——它传入的是字符串的 `strip` **方法对象本身**，而不是调用 `tag.strip()` 得到的字符串。而同一行的过滤条件 `if tag.strip() != ""` 是**带括号**的（正确调用了 `strip()`）。这两处写法不一致。
>
> 进入 `VersionNr.__init__`（[VersionNr.py:8-14](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/VersionNr.py#L8-L14)）后，`version` 接到的是这个方法对象，执行 `version.strip()` 时会找不到 `strip` 属性。因此 **`LatestRelease` 这条路径在实际运行时的行为需要本地验证**——它很可能无法按字面意图选出最大 tag。本讲**不改源码**，把这一点作为「带着批判眼光读真实代码」的练习留给读者：在 4.4.4 的实践中亲自观察它到底报什么错，再思考若要让它按意图工作应该怎么改（例如把 `tag.strip` 改成 `tag.strip()`）。

此外还要注意 `LatestRelease` 的两个**边界情况**（无论上面的笔误是否修正都成立）：

- **仓库没有任何 tag**：`tagList` 为空，`max([])` 会抛 `ValueError: max() arg is an empty sequence`。
- **tag 不是 `major.minor.bugfix` 格式**（例如 `v1.0` 或 `release-2`）：`VersionNr(tag)` 在 [VersionNr.py:10-11](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/VersionNr.py#L10-L11) 处会抛 `Exception("Got illegal version number: ...")`。也就是说，`LatestRelease` **隐式要求**依赖仓库的所有 tag 都符合 PSI 的三段式语义版本约定（见 README 的 Tagging Policy）。

#### 4.4.4 代码实践

**实践目标**：亲手验证 `LatestRelease` 的实际行为，并思考它与「字面意图」是否一致。

**操作步骤**：

1. 准备一个本地仓库并打上**多个三段式 tag**（**示例命令**）：

   ```bash
   mkdir -p /tmp/demo_repo && cd /tmp/demo_repo
   git init && echo "hello" > README.md && git add . && git commit -m "init"
   git tag 1.0.0
   git tag 1.2.0
   git tag 1.10.0      # 用来验证「字符串排序会错，语义排序才对」
   git tag             # 确认四个 tag 都在
   ```

2. 把它变成可克隆的源（裸仓库或直接用工作目录）：

   ```bash
   cd /tmp && git clone --bare demo_repo demo_repo.git
   ```

3. 用 4.1.4 的驱动脚本（`url` 指向 `file:///tmp/demo_repo.git`），分别执行两次，每次前先清空 `/tmp/work`：

   ```bash
   rm -rf /tmp/work && mkdir -p /tmp/work
   python3 drive_cli.py -checkout -mode master
   # 记录 demo_lib 当前指向的提交/tag

   rm -rf /tmp/work && mkdir -p /tmp/work
   python3 drive_cli.py -checkout -mode latest_release
   # 观察 latest_release 分支的实际输出
   ```

4. 进入克隆出来的目录手动核对：

   ```bash
   cd /tmp/work/DemoLib/demo_lib && git describe --tags && git tag
   ```

**需要观察的现象**：

- **`master` 模式**：依赖停留在默认分支最新提交，没有切到任何 tag。
- **`latest_release` 模式**：根据 4.4.3 的分析，这里**很可能不会**得到「选中 `1.10.0`」的理想结果。请如实记录你看到的现象：
  - 是否在 `tagList = [VersionNr(tag.strip) ...]` 这一行抛出异常？异常类型与信息是什么？
  - 如果异常被你（在练习中）修正为 `tag.strip()` 后再跑，`max(tagList)` 是否正确选中 `1.10.0` 而非 `1.2.0`？

**预期结果**：

- 算法**意图**：`1.10.0 > 1.2.0 > 1.0.0`，`LatestRelease` 应选中 `1.10.0`，`master` 不切 tag。这验证了「必须用语义比较、不能用字符串比较」。
- 算法**实际**：**待本地验证**。请把真实输出记下来——本讲不预设结论，鼓励你以源码字面为准去观察。

> 这个练习的价值不在于「让 latest_release 跑通」，而在于训练两种能力：一是**按意图读懂算法**（tag → VersionNr → max → checkout），二是**按字面核实实现**（发现 `tag.strip` 这类细节差异）。真实项目里这两者经常不一致，能看出区别就是源码阅读的进阶。

#### 4.4.5 小练习与答案

**练习 1**：仓库有 tag `1.0.0`、`1.2.0`、`1.10.0`。如果**不**用 `VersionNr`，直接对字符串列表 `sorted(["1.0.0","1.2.0","1.10.0"])[-1]`，会得到什么？为什么是错的？

**答案**：会得到 `"1.2.0"`。因为字符串按字符逐位比较，`"1.10.0"` 与 `"1.2.0"` 在第二位比的是 `'1'` 与 `'2'`，`'1' < '2'`，于是 `"1.2.0"` 被判为更大。只有把它们解析成 `(1,10,0)` 与 `(1,2,0)` 按整数比较，才能得出 `1.10.0` 更大。这就是 `VersionNr` 存在的意义。

**练习 2**：`LatestRelease` 模式对依赖仓库的 tag 命名有什么**隐式假设**？违反时会怎样？

**答案**：它假设**所有 tag**都是 `major.minor.bugfix` 三段纯数字格式。违反时，`VersionNr(tag)` 会在段数不足（`< 3`）时抛 `Exception("Got illegal version number: ...")`，或某段非数字时抛 `ValueError`。因此混用了非语义 tag（如 `v1.0`、`nightly`）的仓库无法使用 `LatestRelease` 模式。

**练习 3**：`max(tagList)` 依赖 `VersionNr` 的哪个方法？为什么不需要 `__lt__`？

**答案**：依赖 `__gt__`（[VersionNr.py:25-37](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/VersionNr.py#L25-L37)）。`max()` 的实现是「维护一个当前最大值，遍历时用 `>` 比较更新」，只需要 `>` 运算，所以只定义 `__gt__` 就够了，不必实现 `__lt__`。

---

## 5. 综合实践

把本讲的四个模块串起来，完成一次「对比两种模式」的完整任务。

**任务背景**：你想给一个 FPGA 项目拉取依赖 `demo_lib`。你需要决定是用 `master`（追最新开发版）还是 `latest_release`（追最新正式发布版），并核实 `latest_release` 选中的 tag 是否符合预期。

**步骤**：

1. **准备依赖仓库**（4.4.4 已给）：一个带 `1.0.0 / 1.2.0 / 1.10.0` 三个 tag 的本地仓库，并暴露成 `file:///tmp/demo_repo.git`。
2. **写驱动脚本** `drive_cli.py`（4.1.4），构造依赖：

   ```python
   dep = Dependency("demo_lib", "file:///tmp/demo_repo.git", "DemoLib/demo_lib", "1.0.0")
   Actions.ExecMain("/tmp/work", [dep])
   ```

3. **第一轮：master 模式**

   ```bash
   rm -rf /tmp/work && mkdir -p /tmp/work
   python3 drive_cli.py -checkout -mode master
   ```

   观察并记录：`/tmp/work/DemoLib/demo_lib` 是否生成？它当前处于哪个提交（`git log --oneline -1`、`git describe --tags`）？是否被切到了某个 tag？

4. **第二轮：latest_release 模式**

   ```bash
   rm -rf /tmp/work && mkdir -p /tmp/work
   python3 drive_cli.py -checkout -mode latest_release
   ```

   观察并记录：输出里有没有 `git tag` 的结果？`max(tagList)` 最终选中了哪个 tag？是否在 [Actions.py:129](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L129) 处抛异常？把真实现象写下来。

5. **对比与结论**：用一句话总结 `master` 与 `latest_release` 在「最终检出内容」上的差异；并说明 `latest_release` 的实际行为是否与它的算法意图（选中 `1.10.0`）一致，如不一致，定位到具体哪一行代码。

**预期结果**：

- `master`：检出默认分支最新提交，不切 tag。
- `latest_release`：**意图**是检出 `1.10.0`；**实际**待本地验证（关注 4.4.3 指出的 `tag.strip` 笔误）。无论结果如何，你应当能指出 `master` 与 `latest_release` 的差异落在 [Actions.py:123-131](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L123-L131) 这段「切版本」逻辑上。

**反思题**：如果 `latest_release` 这条路径确实因笔误而无法工作，那么「命令行默认 `-mode` 是 `latest_release`」这一设定（[Actions.py:146](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L146)）会给普通用户带来什么影响？这是一个值得在团队内讨论的问题。

## 6. 本讲小结

- `CHECKOUT_MODE` 有三种成员 `Master / LatestRelease / SpecifiedRelease`；命令行用字符串 `master / latest_release / specified_version`，由 `ExecMain` 的 `if/elif` 表翻译成枚举。
- **默认值不一致**：`Checkout` 函数签名默认 `Master`，而 `ExecMain` 命令行默认 `latest_release`——两条入口的默认行为不同。
- `Checkout` 的骨架是「幂等补齐」：依赖已存在就跳过并查版本（`CheckCompatibility`），不存在就建父目录并克隆，全程用 `os.chdir + try/finally` 还原 CWD。
- `asSubmodule` 切换克隆方式：`False` 走 `git clone --recurse-submodules`，`True` 走 `git submodule add`；两条分支对 `URL_REPLACEMENTS` 的应用方式不对称。
- `LatestRelease` 的算法意图是 `git tag` → `VersionNr` 列表 → `max()` → `git checkout`，依赖 `VersionNr.__gt__` 提供语义比较（`1.10.0 > 1.2.0`）。
- 读真实源码要区分「意图」与「字面实现」：[Actions.py:129](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L129) 的 `tag.strip`（无括号）与过滤条件里的 `tag.strip()`（有括号）写法不一致，实际运行行为需本地验证。

## 7. 下一步学习建议

- **继续学习 [u3-l3](u3-l3-version-compatibility.md)（语义版本兼容性校验 CheckCompatibility）**：本讲反复提到的 `CheckCompatibility` 会在「跳过已存在依赖」和「克隆后」被调用，它是 2.1.0 的核心新增特性。学完它你能完整理解「检出 + 校验」的闭环。
- **接着读 [u3-l4](u3-l4-url-replacement.md)（URL 替换机制与扩展点）**：本讲提到普通克隆分支与子模块分支对 `URL_REPLACEMENTS` 的应用不对称，下一讲会展开 `PSI_GFA_HTTPS_TO_SSH` 与如何新增自定义替换规则。
- **回头巩固 [u2-l2](u2-l2-versionnr.md)（VersionNr）**：如果你对 `max(tagList)` 为何只需 `__gt__` 还不完全清楚，重读 `VersionNr` 的富比较方法与反射回退机制。
- **源码延伸阅读**：动手追踪一条完整调用链——`ExecMain`（[Actions.py:160-170](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L160-L170)）→ `Checkout`（[Actions.py:93-133](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L93-L133)）→ `Dependency.GetParentDir`（[Dependency.py:28-33](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Dependency.py#L28-L33)）→ `CheckCompatibility`（[Actions.py:42-61](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L42-L61)），把本讲与上一讲串成一条完整数据流。
