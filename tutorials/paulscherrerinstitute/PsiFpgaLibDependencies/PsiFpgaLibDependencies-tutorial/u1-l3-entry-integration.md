# 包入口与客户端集成方式

## 1. 本讲目标

本讲承接 u1-l1（项目定位）和 u1-l2（安装、打包与目录结构）。前两讲已经让读者知道「这个工具是干什么的、怎么装、目录长什么样」，但留了一个关键问题悬而未决：**到底从哪里「启动」这个工具？**

很多初学者会本能地去找一个 `main.py` 或者 `bin/xxx` 可执行文件，结果在仓库里翻一圈也找不到——因为本包**根本没有独立的 main 文件**。它是一个**库（library）**，设计上由别的项目（客户端，例如 `psi_common`）在自己的脚本里调用。

学完本讲，读者应该能够：

- 看懂 [`__init__.py`](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/__init__.py) 里三行 `from ... import ...` 各自导出了什么，并能解释一个容易踩坑的细节：`Actions` 是模块，而 `Dependency`、`Parse` 是类。
- 说出 `Actions.ExecMain(...)` 才是真正的命令行入口，但它**不是**一个可以单独运行的命令——它需要调用方传参。
- 用一句话讲清楚「库式调用」与「命令行直接运行」的区别，并理解 `psi_common` 这类客户端是如何把本包接进自己工程的。
- 独立编写一个最小脚本：导入 `PsiFpgaLibDependencies`，手动构造一个依赖列表，调用 `Actions.ListDependencies` 打印，亲眼看到「库被当作库用」。

---

## 2. 前置知识

- **Python 的 `__init__.py`**：一个目录里有 `__init__.py`，这个目录就被 Python 当作一个「包（package）」。`import 包名` 时，Python 实际执行的就是这个 `__init__.py`。它决定了包对外「露出」哪些名字。
- **模块（module）与类（class）的区别**：模块是一整个 `.py` 文件，里面可能定义了很多函数和类；类是一个具体的类型，可以用来创建对象（实例）或调用它的方法。`import` 模块后，要通过 `模块.名字` 去访问里面的内容；而拿到类之后，可以直接 `类(...)` 构造对象。
- **`from X import Y` 的两种含义**：`from . import Actions` 是「从当前包导入子模块 `Actions`」；`from .Dependency import Dependency` 是「从子模块 `Dependency` 里导入名字 `Dependency`（一个类）」。前者绑定的是模块，后者绑定的是模块里的一个类——这个差异是本讲的核心要点之一。
- **argparse**：Python 标准库里用来解析命令行参数的工具。它把 `python 脚本.py -list -check` 里的 `-list`、`-check` 这些「开关」翻译成 Python 变量，供程序判断该做什么。u1-l1 已提到本工具有 `-list / -check / -checkout` 三类动作，本讲会看到它们在代码里是怎么被 argparse 接收并分发的。
- **客户端（client）/ 集成（integration）**：本包不面向最终用户直接运行，而是面向「写 FPGA 库的工程师」。他们会在自己库的仓库里写一个小脚本，调用本包来管理自己库的依赖。这个「自己库的仓库」就是客户端，把本包「接」进去的过程就是集成。

> 提醒：u1-l2 已经讲过 `setup.py` 里 `package_dir = {"PsiFpgaLibDependencies": "."}` 的含义——「仓库根目录本身就是包目录」。所以本包的名字是 `PsiFpgaLibDependencies`，导入它就是 `import PsiFpgaLibDependencies`。本讲默认读者已经装好了这个包（`pip3 install dist/PsiFpgaLibDependencies-2.1.0.tar.gz`）。

---

## 3. 本讲源码地图

本讲只围绕三个文件，而且重点非常集中。

| 文件 | 角色 | 本讲关注点 |
| --- | --- | --- |
| [__init__.py](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/__init__.py) | 源码（包入口） | 三行 import 决定了包对外导出 `Actions / Dependency / Parse`，以及「模块 vs 类」的绑定差异 |
| [Actions.py](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py) | 源码（动作与 CLI） | `ExecMain(repoPath, dependencies)` 用 argparse 接收命令行开关并分发到 `ListDependencies / CheckDependency / Checkout` |
| [README.md](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/README.md) | 文档 | 第 35 行明确指出「具体用法参考 `psi_common`」——这就是本包的典型客户端 |

此外会顺带提到 [`Dependency.py`](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Dependency.py) 的构造函数签名，因为代码实践里要手动构造 `Dependency` 对象；其字段含义的深入讲解在 u2-l1。

---

## 4. 核心概念与源码讲解

本讲按三个最小模块推进：先看「包露出了什么」（`__init__.py` 导出），再看「命令行入口藏在哪」（`ExecMain`），最后把两者串起来讲「客户端怎么把它接进自己工程」。

### 4.1 `__init__.py` 的导出结构

#### 4.1.1 概念说明

一个 Python 包的「公共 API 表面」由它的 `__init__.py` 决定。`__init__.py` 里 `import` 了哪些名字，外部使用者就能（也应该只）通过这些名字来使用这个包。

本包的 `__init__.py` 极其精简，只有三行有效的 import，分别导出了三个名字：`Actions`、`Dependency`、`Parse`。这三者就是本包对外提供的全部能力：

- `Actions` —— 动作的集合（列出、检查、检出依赖），也是命令行入口所在。
- `Dependency` —— 一条依赖的数据模型（库能不能用、最低版本是多少）。
- `Parse` —— 从 README 里解析出依赖列表的解析器。

但这里有一个**初学者极容易踩坑**的细节：这三个名字里，`Actions` 是一个**模块**，而 `Dependency` 和 `Parse` 是**类**。所以访问它们的方式并不对称。理解这一点，才能写出正确的调用代码。

#### 4.1.2 核心流程

当客户端执行 `import PsiFpgaLibDependencies`（习惯上起个别名 `PFD`）时，Python 会执行 `__init__.py`，发生如下绑定：

```text
import PsiFpgaLibDependencies as PFD
        │
        ▼  执行 __init__.py 的三行 import
        │
        ├─ from . import Actions            →  PFD.Actions   绑定为【模块 Actions.py】
        ├─ from .Dependency import Dependency → PFD.Dependency 绑定为【类 Dependency】
        └─ from .Parse import Parse          →  PFD.Parse     绑定为【类 Parse】
        │
        ▼  由此决定调用写法
        │
        ├─ PFD.Dependency(...)              ✓ 直接构造对象（类就在包级）
        ├─ PFD.Actions.ListDependencies(...) ✓ 经由模块再点出函数
        └─ PFD.Parse.FromReadme(...)         ✓ 直接调用类方法
```

把上面的差异整理成一张表，务必记住：

| 包级名字 | `__init__.py` 里的语句 | 绑定到的东西 | 正确的访问方式 |
| --- | --- | --- | --- |
| `PFD.Actions` | `from . import Actions` | **模块**（整个 `Actions.py`） | `PFD.Actions.ListDependencies(...)` |
| `PFD.Dependency` | `from .Dependency import Dependency` | **类**（`Dependency` 类） | `PFD.Dependency(...)` 直接构造 |
| `PFD.Parse` | `from .Parse import Parse` | **类**（`Parse` 类） | `PFD.Parse.FromReadme(...)` |

为什么会有这种不对称？因为 `from . import Actions` 里的 `Actions` 指的是「名为 Actions 的子模块」；而 `from .Dependency import Dependency` 里，左边的 `.Dependency` 是子模块、右边的 `Dependency` 是该模块里定义的**同名类**——import 语句把「类」这个名字拉到了包级，覆盖了「模块」的位置。这是一个合法但容易让人迷惑的写法，本讲后续的代码实践会让读者亲手验证它。

#### 4.1.3 源码精读

整个 `__init__.py` 的有效逻辑只有三行：

[__init__.py:6-8](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/__init__.py#L6-L8)：包入口三行 import，分别导出 `Actions` 模块、`Dependency` 类、`Parse` 类。

```python
from . import Actions
from .Dependency import Dependency
from .Parse import Parse
```

- 第 6 行 `from . import Actions`：把子模块 `Actions` 绑定到包级名字 `Actions`。
- 第 7 行 `from .Dependency import Dependency`：从子模块 `Dependency` 里取出**类** `Dependency`，绑定到包级名字 `Dependency`。
- 第 8 行 `from .Parse import Parse`：同理，把**类** `Parse` 绑定到包级名字 `Parse`。

注意这里**没有** `from . import VersionNr`。也就是说 `VersionNr`（版本号模型）**没有**被导出到包级。客户端若想直接用 `VersionNr`，得自己 `from PsiFpgaLibDependencies.VersionNr import VersionNr`。这是「公共 API 表面」的一个有意取舍：版本号被视为内部细节，通常只通过 `Dependency` 间接使用。

#### 4.1.4 代码实践

**实践目标**：用代码亲手验证「`Actions` 是模块、`Dependency` 和 `Parse` 是类」这一结论。

**操作步骤**：

1. 确保已按 u1-l2 安装好本包：`pip3 install dist/PsiFpgaLibDependencies-2.1.0.tar.gz`。
2. 在任意目录新建 `probe_export.py`（示例代码，非项目原有文件）：

   ```python
   import PsiFpgaLibDependencies as PFD

   # 看三个名字分别是什么类型
   print("Actions   ->", PFD.Actions)      # 期望：module
   print("Dependency->", PFD.Dependency)   # 期望：class
   print("Parse     ->", PFD.Parse)        # 期望：class
   ```

3. 运行：`python3 probe_export.py`。

**需要观察的现象**：打印结果里，`Actions` 会显示成 `<module 'PsiFpgaLibDependencies.Actions' ...>`，而 `Dependency` 和 `Parse` 会显示成 `<class 'PsiFpgaLibDependencies.Dependency'>` 和 `<class 'PsiFpgaLibDependencies.Parse'>`。

**预期结果**：`Actions` 是 `module` 类型，`Dependency`、`Parse` 是 `class` 类型。如果手痒写成 `PFD.Dependency.ListDependencies(...)` 会立刻 `AttributeError`——因为 `Dependency` 是类，不是模块。

> 如果无法确定运行结果，请标注「待本地验证」再下结论；不要假装已经跑过。

#### 4.1.5 小练习与答案

**练习 1**：为什么客户端写 `PFD.Dependency("foo", "url", ".", "1.0.0")` 能直接构造对象，而 `PFD.ListDependencies(...)` 却会报错？

**答案**：因为 `Dependency` 被 `__init__.py` 直接绑定为**类**，所以包级就能用它构造对象；而 `ListDependencies` 是定义在 `Actions` **模块**里的函数，并没有被单独拉到包级，必须写成 `PFD.Actions.ListDependencies(...)`。

**练习 2**：`VersionNr` 没有出现在 `__init__.py` 的导出里。客户端若想校验版本号，应该怎么导入它？

**答案**：绕过包级，直接从子模块导入：`from PsiFpgaLibDependencies.VersionNr import VersionNr`。

---

### 4.2 `ExecMain`：命令行入口的真身

#### 4.2.1 概念说明

u1-l1 已经说过本工具有 `-list / -check / -checkout` 三类动作。那么「谁负责接收这些命令行开关、并分发到对应的动作函数」？答案就是 `Actions.ExecMain`。

但 `ExecMain` 有一个最关键的特征，它和读者平常写的 `python xxx.py` 很不一样：**它不是一个能被直接运行的命令**。证据有三条：

1. 全项目里**没有** `if __name__ == "__main__":` 这样的入口块（可在仓库内搜索 `__main__` 验证，结果为空）。
2. `setup.py` 里**没有** `entry_points` / `console_scripts`，因此安装后不会生成任何命令行可执行程序。
3. `ExecMain` 的签名是 `ExecMain(repoPath, dependencies)`——它**要求调用方先传进来**「仓库路径」和「依赖列表」。

换句话说，`ExecMain` 是「为客户端准备好的命令行处理函数」，但触发它的权力交给了客户端。客户端在自己的脚本里调用 `ExecMain(...)`，`ExecMain` 就去读取该脚本被调用时的命令行参数（`sys.argv`），完成 `-list/-check/-checkout` 的分发。

#### 4.2.2 核心流程

`ExecMain` 内部的执行流程：

```text
客户端调用 ExecMain(repoPath, dependencies)
        │
        ▼
1. 构建 ArgumentParser，定义命令行开关：
     -list / -check / -checkout / -as_submodule   （store_true 开关）
     -mode  choices=[master, latest_release, specified_version]  默认 latest_release
        │
        ▼
2. parser.parse_args()  解析 sys.argv
        │
        ▼
3. 三个「if」分别判断（注意：是 if，不是 elif，可同时触发多个动作）：
     if args.list:     → ListDependencies(dependencies)
     if args.check:    → CheckDependency(repoPath, dependencies)
     if args.checkout: → 把 mode 字符串映射成 CHECKOUT_MODE，再 Checkout(repoPath, dependencies, mode, args.as_submodule)
        │
        ▼
4. 一个开关都没给 → 什么都不做（安静退出）
```

几个值得注意的设计点：

- `-list / -check / -checkout` 都是 `action="store_true"`，即「不写就是 `False`，写了就是 `True`」的开关；都不写时，三段 `if` 全不成立，程序静默退出，没有任何输出。
- 三个判断用的是 `if` 而非 `elif`，所以理论上 `python 脚本.py -list -check` 会**依次**执行列出和检查两个动作。
- `-mode` 用 `choices=[...]` 限定取值，又在 `ExecMain` 里把字符串映射成 `CHECKOUT_MODE` 枚举；若出现非法值会抛 `Exception`（这是双保险）。

#### 4.2.3 源码精读

[Actions.py:135-147](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L135-L147)：`ExecMain` 的签名与 argparse 参数定义。注意 `repoPath` 和 `dependencies` 必须由调用方传入。

```python
def ExecMain(repoPath : str, dependencies : List[Dependency]):
    parser = ArgumentParser()
    parser.add_argument("-list",   ..., action="store_true", default=False)
    parser.add_argument("-check",  ..., action="store_true", default=False)
    parser.add_argument("-checkout",..., action="store_true", default=False)
    parser.add_argument("-as_submodule", ..., action="store_true", default=False)
    parser.add_argument("-mode", ..., choices=["master", "latest_release", "specified_version"], default="latest_release")
    args = parser.parse_args()
```

[Actions.py:151-170](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L151-L170)：把解析到的开关分发到具体动作函数。

```python
if args.list:
    print("*** Dependencies ***")
    ListDependencies(dependencies)

if args.check:
    print("*** Dependency Check ***")
    CheckDependency(repoPath, dependencies)

if args.checkout:
    print("*** Checkout ***")
    if args.mode == "latest_release":   mode = CHECKOUT_MODE.LatestRelease
    elif args.mode == "master":         mode = CHECKOUT_MODE.Master
    elif args.mode == "specified_version": mode = CHECKOUT_MODE.SpecifiedRelease
    else: raise Exception("Illegel -mode: {}".format(args.mode))
    Checkout(repoPath, dependencies, mode, args.as_submodule)
```

可以看到：`repoPath` 只在 `args.check` / `args.checkout` 时才被真正用上（传给 `CheckDependency` / `Checkout`），`-list` 只打印 `dependencies` 本身、不需要路径。这也解释了为什么 `ExecMain` 必须由客户端传参——依赖列表和仓库根路径都只有客户端自己才知道。

#### 4.2.4 代码实践

**实践目标**：用「库式调用」的方式驱动 `ExecMain`，体会「同一个命令行入口，却能从 Python 内部触发」。

**操作步骤**：

1. 新建 `drive_execmain.py`（示例代码）：

   ```python
   import PsiFpgaLibDependencies as PFD

   deps = [PFD.Dependency("demo_lib", "https://example.com/demo.git", "../demo_lib", "1.2.0")]
   # ExecMain 会自己去读 sys.argv；这里我们什么都不传给 -mode，让它用默认值
   PFD.Actions.ExecMain(repoPath=".", dependencies=deps)
   ```

2. 用 `-list` 开关运行：`python3 drive_execmain.py -list`。
3. 不带任何开关再运行一次：`python3 drive_execmain.py`。
4. 用 `-h` 看自动生成的帮助：`python3 drive_execmain.py -h`。

**需要观察的现象**：

- 第 2 步应打印一行标题 `*** Dependencies ***`，再打印 `demo_lib - https://example.com/demo.git - 1.2.0`。
- 第 3 步应**没有任何输出**（三个开关都没触发）。
- 第 4 步应打印 argparse 自动生成的用法说明，列出全部参数。

**预期结果**：`ExecMain` 行为完全像一个命令行程序，但它是由我们的脚本「代为调用」的——这就是本包「库式调用」的精髓。

> 注意：`repoPath="."` 在本练习里无害，因为 `-list` 不依赖它。若改用 `-check`，`CheckDependency` 会真的 `chdir(".")` 并检查 `../demo_lib` 是否存在，多半会报「不存在」的 ERROR——这正好是下一模块和 u3-l1 的内容。

#### 4.2.5 小练习与答案

**练习 1**：把三个动作判断从 `if` 改成 `elif`，会改变 `python 脚本.py -list -check` 的行为吗？

**答案**：会。`if` 版本会依次执行「列出」和「检查」两个动作；改成 `elif` 后，只要前面某个分支成立，后面的就不再判断，因此 `-list -check` 可能只执行列出、跳过检查。可见原作者有意允许多个动作同时触发。

**练习 2**：`-mode` 不写时默认是 `master` 还是 `latest_release`？依据在哪一行？

**答案**：默认 `latest_release`，依据 [Actions.py:146](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L146) 的 `default="latest_release"`。

---

### 4.3 客户端集成模式（库式调用）

#### 4.3.1 概念说明

把前两个模块的结论合起来：本包对外露出 `Actions / Dependency / Parse` 三个名字，命令行入口是 `ExecMain`，但它必须被客户端「调用」、并喂以「仓库路径」和「依赖列表」。那么一个真实的客户端（FPGA 库工程）到底是怎么把它接进来的？

README 第 35 行已经给出了标准答案：

[README.md:35](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/README.md#L35)：明确指出「具体用法参考 `psi_common` 库」，即 `psi_common` 是本包的典型客户端。

> For the exact usage, refer to the [psi_common Library](https://github.com/paulscherrerinstitute/psi_common).

所谓「集成」，就是客户端在自己仓库里放一个**驱动脚本**，完成两件事：

1. **准备好依赖列表**——通常是调用 `PFD.Parse.FromReadme("README.md")`，把自己 README 里的依赖解析出来（`Parse.FromReadme` 的内部机制是 u2-l3 的主题，本讲只用、不讲细节）。
2. **把控制权交给 `ExecMain`**——调用 `PFD.Actions.ExecMain(repoPath, dependencies)`，让本包去读命令行开关、执行 `-list/-check/-checkout`。

这样，客户端的使用者只需对客户端的脚本下达命令（如 `python check_dep.py -check`），背后就由本包完成所有工作。**本包自己永远不直接面对最终用户**——这正是「库」与「可执行程序」的本质区别。

#### 4.3.2 核心流程

一个典型的客户端集成脚本（以 `psi_common` 风格为例）：

```text
客户端仓库里有一个驱动脚本（如 check_dep.py）
        │
        ▼
import PsiFpgaLibDependencies as PFD
        │
        ├─ 1. 确定仓库根路径 repoPath（通常是脚本所在目录）
        ├─ 2. 解析自己 README 的依赖：deps = PFD.Parse.FromReadme("README.md")
        └─ 3. 交权：PFD.Actions.ExecMain(repoPath, deps)
                │
                ▼  ExecMain 读 sys.argv，按开关分发：
                    -list     → ListDependencies(deps)
                    -check    → CheckDependency(repoPath, deps)
                    -checkout → Checkout(repoPath, deps, mode, as_submodule)
```

对比两种「调用姿态」：

| 维度 | 命令行直接运行（本包**不支持**） | 库式调用（本包**唯一**支持） |
| --- | --- | --- |
| 触发方式 | `psi_fpga_deps -list`（一个可执行命令） | `python 客户端脚本.py -list`（脚本内部调 `ExecMain`） |
| 谁解析 README | 命令自己找 README | 客户端脚本先用 `Parse.FromReadme` 解析好 |
| 是否需要客户端写代码 | 不需要 | 需要（写一个驱动脚本） |
| 本包是否提供 | 否（无 `console_scripts`） | 是（提供 `ExecMain` 等函数） |

#### 4.3.3 源码精读

本模块把前面两个模块的源码「拼」起来看调用关系：

[Actions.py:135](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L135)：`ExecMain(repoPath, dependencies)` 的两个参数正是客户端必须提供的「仓库路径」与「依赖列表」。

[Actions.py:66-72](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L66-L72)：`ListDependencies` 把每条依赖格式化成 `库名 - URL - 最低版本` 一行打印——这是 `-list` 动作的实际效果。

```python
def ListDependencies(deps : List[Dependency]):
    for dep in deps:
        print("{} - {} - {}".format(dep.libraryName, dep.url, dep.minVersion))
```

[Dependency.py:14-26](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Dependency.py#L14-L26)：`Dependency` 构造函数签名，四个字段 `libraryName / url / relativePath / minVersion`，其中 `minVersion` 是字符串、内部会被包成 `VersionNr` 对象。

```python
def __init__(self, libraryName, url, relativePath, minVersion):
    self.libraryName = libraryName
    self.url = url
    self.relativePath = relativePath
    self.minVersion = VersionNr(minVersion)
```

把三段连起来读，就能解释 `-list` 的输出从何而来：客户端构造（或 `Parse` 解析出）`Dependency` 列表 → `ExecMain` 在 `args.list` 时调用 `ListDependencies` → 逐条 `print` 出 `库名 - URL - VersionNr`。其中版本号那一列，是 `VersionNr.__str__` 渲染出来的（详见 u2-l2，其格式为 `major.minor.bugfix`）。

#### 4.3.4 代码实践（本讲主实践任务）

**实践目标**：编写一个最小 Python 脚本，导入 `PsiFpgaLibDependencies`，手动构造一个依赖列表，调用 `Actions.ListDependencies` 打印，亲手体验「库式调用」而非「命令行直接运行」。

**操作步骤**：

1. 确保已安装本包（u1-l2）。
2. 新建 `demo_library_call.py`（示例代码，非项目原有文件）：

   ```python
   import PsiFpgaLibDependencies as PFD

   # 注意写法：Dependency 是「类」，可直接构造；ListDependencies 在「模块」Actions 里
   deps = [
       PFD.Dependency("psi_common",
                      "https://git.psi.ch/GFA/PsiCommon.git",
                      "../psi_common",
                      "1.0.0"),
       PFD.Dependency("psi_hw",
                      "https://git.psi.ch/GFA/PsiHw.git",
                      "./deps/psi_hw",
                      "2.3.1"),
   ]

   # 库式调用：直接调用函数，不经过任何命令行解析
   PFD.Actions.ListDependencies(deps)
   ```

3. 运行：`python3 demo_library_call.py`。

**需要观察的现象**：脚本没有读取任何命令行参数，也没有解析任何 README，纯粹靠「构造对象 + 调用函数」就打印出了依赖清单。

**预期结果**（两行）：

```text
psi_common - https://git.psi.ch/GFA/PsiCommon.git - 1.0.0
psi_hw - https://git.psi.ch/GFA/PsiHw.git - 2.3.1
```

第三列之所以是 `1.0.0` / `2.3.1` 而非别的形式，是因为 `minVersion` 在构造时被包成了 `VersionNr`，而 [VersionNr.py:39-40](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/VersionNr.py#L39-L40) 的 `__str__` 把它渲染成 `major.minor.bugfix`。

**对比实验**（体会「本包没有命令行」）：试着把本包当成一个命令直接运行，比如 `python3 -m PsiFpgaLibDependencies -list`——它会失败（本包没有 `__main__.py`，也没有 `__main__` 块）。这正是本包「只能库式调用」的最直观证据。

> 若你的环境里 `import PsiFpgaLibDependencies` 报 `ModuleNotFoundError`，请回到 u1-l2 确认是否真的 `pip3 install` 成功；不要靠把仓库根目录塞进 `sys.path` 来「绕过」，因为 `package_dir={"PsiFpgaLibDependencies":"."}` 让直接导入很别扭，安装才是正路。

#### 4.3.5 小练习与答案

**练习 1**：如果把上面脚本里的 `PFD.Actions.ListDependencies(deps)` 误写成 `PFD.ListDependencies(deps)`，会发生什么？为什么？

**答案**：会抛 `AttributeError`。因为 `ListDependencies` 是 `Actions` **模块**里的函数，没有被 `__init__.py` 单独拉到包级；包级只有 `Actions / Dependency / Parse` 三个名字。

**练习 2**：`ExecMain` 需要客户端传 `dependencies`，而 `-list` 动作其实只用到了这个列表、不需要 `repoPath`。这说明了一个什么设计事实？

**答案**：说明本包从不自己解析 README——解析依赖（`Parse`）和确定仓库路径都是**客户端的职责**。本包只负责「拿到列表后」的列出/检查/检出动作。这也正是它「是库、不是命令」的根本原因。

**练习 3**：为什么不建议客户端靠 `sys.path` 直接导入仓库源码，而要走 `pip3 install`？

**答案**：因为 `setup.py` 里 `package_dir = {"PsiFpgaLibDependencies": "."}` 把「仓库根目录」映射成包目录，导入名 `PsiFpgaLibDependencies` 在磁盘上并没有同名子目录。直接 `sys.path` 操作很容易导致包名与目录对不上；`pip3 install` 让 setuptools 处理好这层映射，是最省心的方式（详见 u1-l2）。

---

## 5. 综合实践

把本讲三个模块串起来，模拟一次「客户端集成」。

**任务**：写一个迷你的客户端驱动脚本 `mini_client.py`，它不解析 README（那部分留给 u2-l3），而是**手动构造**两条依赖，然后把命令行完全交给本包的 `ExecMain` 处理，支持 `-list` 和 `-check` 两个开关。

**参考骨架**（示例代码）：

```python
import os
import PsiFpgaLibDependencies as PFD

# 客户端「知道」的两件事：仓库根路径 + 依赖列表
repoPath = os.path.dirname(os.path.abspath(__file__))
deps = [
    PFD.Dependency("lib_a", "https://example.com/a.git", "../lib_a", "1.0.0"),
    PFD.Dependency("lib_b", "https://example.com/b.git", "lib_b",        "0.9.0"),
]

# 把命令行交权给本包
PFD.Actions.ExecMain(repoPath, deps)
```

**操作步骤与观察**：

1. `python3 mini_client.py -list`：应看到 `*** Dependencies ***` 标题，以及两行 `lib_a - ... - 1.0.0`、`lib_b - ... - 0.9.0`。
2. `python3 mini_client.py -check`：因为 `../lib_a` 和 `lib_b` 在脚本所在目录下并不存在，[Actions.py:74-91](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L74-L91) 的 `CheckDependency` 会为每条依赖打印 `-- lib_x --` 和 `ERROR: Dependency ... does not exist`。
3. `python3 mini_client.py`（无开关）：应无任何输出。

**思考题**：这个迷你脚本同时体现了本讲的三个最小模块——

- 它 `import PsiFpgaLibDependencies` 用到了 4.1 的导出结构；
- 它调用 `ExecMain` 用到了 4.2 的命令行入口；
- 它「由客户端写脚本、构造依赖、再交权」正是 4.3 的集成模式。

如果你把它和 u1-l1 里的「`psi_common` 是典型客户端」对应起来，就会发现：真实的 `psi_common` 里那个管理依赖的脚本，做的事情和这个 `mini_client.py` 本质相同，只是把「手动构造依赖」换成了「`PFD.Parse.FromReadme` 解析 README」。

> 待本地验证：第 2 步的 ERROR 文案是否完全一致，取决于本地目录结构；若你在脚本同级手动建一个空 `lib_b` 目录再跑 `-check`，会看到 ERROR 消失、改成走 `CheckCompatibility` 分支（它会再尝试 `git describe`，可能报子进程错误）——这恰好引出 u3-l1 与 u3-l3 的内容。

---

## 6. 本讲小结

- 本包**没有独立的 main 文件**：全仓库搜不到 `__main__` 块，`setup.py` 也没有 `entry_points` / `console_scripts`，安装后不产生任何命令行可执行程序。
- [`__init__.py`](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/__init__.py) 对外只导出三个名字：`Actions`（**模块**）、`Dependency`（**类**）、`Parse`（**类**）。这个「模块 vs 类」的不对称是初学者最易踩的坑。
- 真正的命令行入口是 [`Actions.ExecMain(repoPath, dependencies)`](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L135)，它用 argparse 接收 `-list/-check/-checkout/-as_submodule/-mode`，但**必须由客户端传参调用**。
- 「库式调用」是本包唯一支持的使用方式：客户端写一个驱动脚本，准备依赖列表，再把命令行交权给 `ExecMain`。
- 典型客户端是 [`psi_common`](https://github.com/paulscherrerinstitute/psi_common)（见 [README.md:35](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/README.md#L35)），其用法就是本讲所讲的集成模式的「正式版」。
- 本包从不自己解析 README——`Parse` 解析、`repoPath` 定位都是客户端的职责，本包只做「拿到列表之后」的动作。

---

## 7. 下一步学习建议

本讲弄清了「入口在哪、怎么调用」，但刻意没有深究两件事：**依赖对象本身长什么样**、**README 是怎么被解析成依赖列表的**。这正是第二单元的主题：

- **u2-l1 依赖数据模型 Dependency**：精读 `Dependency.py` 的四个字段、`minVersion` 如何变成 `VersionNr`、`GetParentDir` 的作用——本讲里你已经在 `PFD.Dependency(...)` 里用过它，现在是时候看清它的内部。
- **u2-l2 语义版本号 VersionNr**：精读 `VersionNr.py`，弄懂本讲里第三列输出（`1.0.0`）背后的解析与比较逻辑。
- **u2-l3 README 依赖格式与 Parse.FromReadme 入口**：本讲多次提到「客户端用 `Parse.FromReadme` 解析 README」，那里正是它的全貌。

如果你更想先看「动作侧」，也可以跳到第三单元的 u3-l1（列出与检查依赖），但建议至少先读 u2-l1，否则对 `Dependency` 字段的理解会不够扎实。
