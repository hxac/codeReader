# 命令行接口 ExecMain 与 argparse

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清 `ExecMain` 是整个包唯一的**命令行入口**：它用 `argparse` 从命令行解析开关，再分发到 `ListDependencies / CheckDependency / Checkout` 三个动作——但「仓库路径」和「依赖列表」不是命令行参数，而是由调用方以 Python 实参传入。
- 读懂 `ArgumentParser` 的**参数定义方式**：`add_argument` 的 `-list/-check/-checkout/-as_submodule` 四个开关都用 `action="store_true"`（开关型，无值），而 `-mode` 用默认的 `store`（带值）并配 `choices` 限定取值。
- 理解 **`store_true` 开关**与 **`choices` 取值约束**的语义：开关的「在场即真」、`choices` 的「越界即报错退出」。
- 解释 **`mode` 字符串到 `CHECKOUT_MODE` 枚举的映射**（`if/elif/else`），以及末尾那句 `raise Exception("Illegel -mode: ...")` 为什么在正常命令行下**永远不会被执行**（被 `choices` 提前拦截）。
- 为 `ExecMain` **新增一个 `-version` 开关**并验证 `argparse` 自动生成的帮助文本与分发逻辑。

## 2. 前置知识

本讲是「动作执行」单元的收口，承接前置讲义：

- **u1-l3 包入口与客户端集成方式**：本包没有可独立运行的 `main`，命令行入口就是 `Actions.ExecMain(repoPath, dependencies)`。关键点在于——`repoPath` 与 `dependencies` 由客户端（典型如 `psi_common`）**以 Python 实参传入**，而 `-list/-check/...` 这些**开关**才是 `argparse` 从命令行读的。本讲要把「Python 实参 vs 命令行参数」这道分界讲透。
- **u3-l1 列出与检查依赖**：`ListDependencies`（`-list` 的目标）与 `CheckDependency`（`-check` 的目标）的实现。本讲讲的是「谁在什么条件下调用它们」。
- **u3-l2 检出依赖与检出模式**：`Checkout`（`-checkout` 的目标）与 `CHECKOUT_MODE` 枚举（`Master/LatestRelease/SpecifiedRelease`）。本讲的 `-mode` 映射就是把命令行字符串翻译成这个枚举。

此外需要一点 Python `argparse` 的常识：

- **ArgumentParser**：标准库 `argparse` 提供的「命令行解析器」。你先用 `add_argument` 声明它接受哪些参数，再调用 `parse_args()` 让它去读 `sys.argv`（命令行实参列表），返回一个装好了解析结果的 `Namespace` 对象。
- **选项（option）vs 位置参数（positional）**：以 `-` 或 `--` 开头的是「选项」，可有可无、顺序自由；本讲的五个参数全是选项。
- **开关型选项（flag / switch）**：像 `-l`、`--verbose` 这种「只要出现就生效、后面不跟值」的参数。`argparse` 用 `action="store_true"` 表达它。

> 名词解释：**分发（dispatch）** 指解析完参数后，根据参数取值决定「调用哪个函数、传什么实参」的过程。`ExecMain` 的后半段就是一段手写的分发逻辑（一串 `if`），把命令行意图翻译成对 `ListDependencies / CheckDependency / Checkout` 的实际调用。

## 3. 本讲源码地图

本讲几乎全部围绕 `Actions.py` 末尾的 `ExecMain` 函数，并回看顶部的 `CHECKOUT_MODE` 枚举与 `argparse` 的导入：

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [Actions.py](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py) | 全部动作与命令行入口 | `import ArgumentParser`（L11）、`CHECKOUT_MODE` 枚举（L31–L37）、`ExecMain` 主体（L135–L170）：参数定义（L141–L146）、解析（L147）、三段分发（L151–L170） |
| [setup.py](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/setup.py#L26) | 打包配置 | `version="2.1.0"`（L26）——综合实践中 `-version` 开关要打印的版本号来源 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块，正好对应 `ExecMain` 从上到下的三段：

1. **ArgumentParser 参数定义**——用 `add_argument` 声明五个选项（四个 `store_true` 开关 + 一个带 `choices` 的 `-mode`）。
2. **参数分发**——`parse_args()` 把命令行读成 `args` 对象，再用三段**独立的 `if`**（不是 `elif`）把 `args.list/check/checkout` 映射到动作函数。
3. **mode 映射**——`-mode` 的字符串值经 `if/elif/else` 翻译成 `CHECKOUT_MODE` 枚举，传给 `Checkout`；并理解末尾那句 `raise` 为何是「防御性的死分支」。

---

### 4.1 ArgumentParser 参数定义

#### 4.1.1 概念说明

`ExecMain` 一进来就建一个解析器，然后连着 `add_argument` 五次，声明本程序能在命令行上接受哪些选项。这五个选项分两类：

| 选项 | 类型 | `action` | 是否带值 | 默认值 | 含义 |
| --- | --- | --- | --- | --- | --- |
| `-list` | 开关 | `store_true` | 否 | `False` | 列出所有依赖 |
| `-check` | 开关 | `store_true` | 否 | `False` | 检查依赖是否齐全 |
| `-checkout` | 开关 | `store_true` | 否 | `False` | 检出依赖 |
| `-as_submodule` | 开关 | `store_true` | 否 | `False` | 以子模块形式检出（须配合 `-checkout`） |
| `-mode` | 带值选项 | `store`（默认） | 是 | `"latest_release"` | 检出模式，三选一 |

两类选项的根本差别是**「带不带值」**：

- **开关（前四个）**：命令行上只要**出现** `-list` 这个词就生效，后面**不跟值**。`action="store_true"` 告诉 `argparse`：「见到这个选项，就把对应的属性存成 `True`」。配合 `default=False`，得到一个干净的布尔语义——**在场为真、缺席为假**，于是后面可以写 `if args.list:`。
- **带值选项（`-mode`）**：命令行上要写成 `-mode master`，`master` 是它的**值**。没有指定 `action` 时默认是 `store`（把字符串值原样存进去），并用 `choices=["master", "latest_release", "specified_version"]` 把合法取值**钉死**成这三个。

> 名词解释：**`dest`（destination）** 指解析结果在 `args` 对象上的**属性名**。`add_argument("-list", dest="list", ...)` 表示解析后用 `args.list` 访问它。其实就算不写 `dest="list"`，`argparse` 也会从选项串 `-list` 自动推导出属性名 `list`（规则：去掉前导 `-`、把 `-` 换成 `_`）——所以这里的 `dest` 是**显式但冗余**的，作用是让人一眼看清属性名。

#### 4.1.2 核心流程

```text
建解析器  parser = ArgumentParser()
   │
   ├── add_argument("-list",        action="store_true", default=False)  → args.list
   ├── add_argument("-check",       action="store_true", default=False)  → args.check
   ├── add_argument("-checkout",    action="store_true", default=False)  → args.checkout
   ├── add_argument("-as_submodule",action="store_true", default=False)  → args.as_submodule
   └── add_argument("-mode",        choices=[...],        default="latest_release") → args.mode
   │
   ▼
parse_args()  读 sys.argv，返回 args（Namespace）
```

对单个开关，其取值可以用分段函数表达（在场与否决定真假）：

\[
\text{args.list} = \begin{cases}
\texttt{True}, & \text{命令行中出现 } \texttt{-list} \\
\texttt{False}, & \text{未出现（取 default）}
\end{cases}
\]

对 `-mode`，合法值集合为：

\[
\text{choices} = \{\,\texttt{"master"},\ \texttt{"latest\_release"},\ \texttt{"specified\_version"}\,\}
\]

命令行给的值若不在此集合内，`argparse` 会在 `parse_args()` 阶段直接报错并退出进程（退出码 2），根本走不到后面的分发逻辑。

#### 4.1.3 源码精读

导入解析器（[Actions.py:11](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L11)）：

```python
from argparse import ArgumentParser
```

枚举定义（[Actions.py:31-37](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L31-L37)）——`-mode` 字符串最终要翻译成的三个枚举成员：

```python
class CHECKOUT_MODE(Enum):
    Master = 0
    LatestRelease = 1
    SpecifiedRelease = 2
```

`ExecMain` 的签名与文档串（[Actions.py:135-140](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L135-L140)）——注意两个参数都是 **Python 实参**，不是命令行参数：

```python
def ExecMain(repoPath : str, dependencies : List[Dependency]):
    """
    Execute program as main and parse arguments from command line
    :param repoPath: Path of the repository to check dependencies
    :param dependencies: List of dependencies
    """
```

参数定义五连（[Actions.py:141-146](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L141-L146)）——本模块的核心：

```python
parser = ArgumentParser()
parser.add_argument("-list", dest="list", help="List all dependencies", required=False, default=False, action="store_true")
parser.add_argument("-check", dest="check", help="Check if all dependencies are present", required=False, default=False, action="store_true")
parser.add_argument("-checkout", dest="checkout", help="Checkout dependencies", required=False,default=False, action="store_true")
parser.add_argument("-as_submodule", dest="as_submodule", help="Add dependencies as submodules (must be used together with -checkout)", required=False, default=False, action="store_true")
parser.add_argument("-mode", dest="mode", help="Checkout mode", choices=["master", "latest_release", "specified_version"], required=False, default="latest_release")
```

逐项拆解 `add_argument` 用到的关键字：

- **`-list`（位置首参）**：选项名。以单 `-` 开头，故是「单横杠选项」（`argparse` 不区分单/双横杠的合法性，只影响推导出的 `dest`）。
- **`dest="list"`**：解析后访问属性名。如前所述，对本例的五个选项而言，显式 `dest` 与自动推导结果**一致**，属冗余但清晰。
- **`help="..."`**：该选项的说明文字，会被 `argparse` 自动拼进 `-h/--help` 帮助页（见 4.1.4 实践）。
- **`required=False`**：声明该选项可省略。**这其实是默认行为**——选项（带 `-` 的）默认就是可选的，所以 `required=False` 也是冗余的，写出来只是显式表达意图。
- **`default=False`**：缺席时的取值。对 `store_true` 而言，`argparse` **本身就以 `False` 为默认**，故这里的 `default=False` 同样冗余——即使删掉，`args.list` 在未给开关时仍是 `False`。
- **`action="store_true"`**：开关语义，见到选项就存 `True`。
- **`choices=[...]`（仅 `-mode`）**：限定合法取值。`-mode` 没有写 `action`，故用默认的 `store`，存的是**字符串值**而非布尔。

> 🔎 **带着批判眼光读源码（冗余的 kwargs）**：四个开关都同时写了 `required=False`、`default=False`、`dest=...`，而这三者在 `store_true` + 单横杠选项的情境下**全是 `argparse` 的默认行为**，删掉任何一项结果都不变。它们的作用更像「显式文档」——让读者一眼看清「这是可选开关、缺省为假、属性名叫这个」。这在团队代码里是常见取舍：**靠冗余换可读性**。理解了这一点，你以后读到精简写法 `add_argument("-list", action="store_true")` 时就不会困惑。

> ⚠️ **两条入口的默认 mode 不一致（承接 u3-l2）**：`-mode` 的命令行默认是 `"latest_release"`（[Actions.py:146](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L146)），而 `Checkout` 函数签名的默认却是 `CHECKOUT_MODE.Master`（[Actions.py:93](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L93)）。也就是说：「直接调 `Checkout(...)`」与「走命令行 `-checkout`」在用户没显式指定 mode 时，行为**不同**。这是二次开发时容易踩的坑——务必记住你走的是哪条入口。

#### 4.1.4 代码实践

**实践目标**：用一个**自包含的最小解析器**（不改任何源码、不联网、不依赖 git），亲眼看清 `store_true` 的布尔语义、`choices` 的越界报错，以及 `argparse` 自动生成的 `-h` 帮助页。

**操作步骤**（示例代码，纯标准库，可直接 `python3` 运行）：

```python
from argparse import ArgumentParser

# 复刻 ExecMain 里 -mode 与一个开关的定义，便于孤立观察 argparse 行为
p = ArgumentParser(prog="demo")
p.add_argument("-list", action="store_true")          # 开关
p.add_argument("-mode", choices=["master", "latest_release", "specified_version"],
               default="latest_release")              # 带值 + choices

# 1) 不给任何参数：开关为 False，mode 取 default
print("空参    ->", p.parse_args([]))

# 2) 只给开关：list 变 True
print("带 -list ->", p.parse_args(["-list"]))

# 3) 给合法 mode
print("合法 mode->", p.parse_args(["-mode", "master"]))

# 4) 给非法 mode：argparse 会打印错误并 sys.exit(2)
try:
    p.parse_args(["-mode", "foo"])
except SystemExit as e:
    print("非法 mode 触发 SystemExit，退出码 =", e.code)
```

**需要观察的现象**：

- 第 1 行：`空参 -> Namespace(list=False, mode='latest_release')`——开关缺省为 `False`、`-mode` 缺省为 `latest_release`。
- 第 2 行：`带 -list -> Namespace(list=True, mode='latest_release')`——开关在场即 `True`。
- 第 3 行：`合法 mode -> Namespace(list=False, mode='master')`。
- 第 4 行：stderr 打印形如 `error: argument -mode: invalid choice: 'foo' (choose from 'master', 'latest_release', 'specified_version')`，并抛出 `SystemExit(2)`。

**预期结果**：如上四条。另外，单独运行 `python3 demo.py -h`（或脚本里 `p.parse_args(["-h"])`）会看到 `argparse` **自动生成**的帮助页——其中 `-list` 与 `-mode` 的说明文字直接来自 `help=...`，`-mode` 后还标注了 `{master,latest_release,specified_version}`。**待本地验证**：`argparse` 行为是确定的，请实跑确认 `SystemExit` 的捕获与帮助页排版。

#### 4.1.5 小练习与答案

**练习 1**：把 `-list` 的 `default=False` 删掉，`args.list` 在用户没写 `-list` 时会变成什么？

**参考答案**：仍是 `False`。因为 `action="store_true"` 本身就以 `False` 为缺席默认值，`default=False` 只是把这个隐含默认显式写出来，删掉不改变行为。

**练习 2**：`-mode` 没有 `action="store_true"`，如果用户在命令行只写 `-mode` 而不给值（`prog -mode`），会发生什么？

**参考答案**：`-mode` 走默认的 `store`，**期望后面跟一个值**。只写 `-mode` 不给值时，`argparse` 会报错（形如 `error: argument -mode: expected one argument`）并以退出码 2 退出。这正是「开关型」与「带值型」选项在命令行用法上的差别。

**练习 3**：为什么说 `-list` 上写的 `dest="list"` 是冗余的？什么情况下 `dest` 才**非写不可**？

**参考答案**：因为从选项串 `-list` 自动推导出的 `dest` 就是 `list`（去前导 `-`），显式写出来与推导结果一致，故冗余。`dest` 非写不可的典型场景是：选项名不适合做属性名（例如想用 `--input-file` 这个选项名、但希望属性叫 `args.input_file` 时，推导其实也能得到 `input_file`）；真正必须手写的场景是**只给了短选项且想用一个不同的属性名**，或选项名与 Python 关键字冲突想改写属性名时。

---

### 4.2 参数分发（parse_args 与动作 if 块）

#### 4.2.1 概念说明

参数定义好之后，第二件事就是**解析**与**分发**：

- **解析**：`args = parser.parse_args()`。`parse_args()` 不带实参时，默认去读进程的 `sys.argv[1:]`——也就是**真正启动这个 Python 进程时敲的命令行**。它把结果装进一个 `Namespace` 对象，属性名就是前面 `dest` 决定的那些。
- **分发**：拿到 `args` 后，用一串 `if` 判断每个开关是否被按下，按下就调用对应的动作函数。

`ExecMain` 的分发有一个**很容易看走眼的细节**：三段动作用的是**三个独立的 `if`**，而**不是** `if/elif/else` 链。这意味着它们**不是互斥的**——如果用户同时敲了 `-list -check`，两段都会执行。这和「单选」式的 `if/elif` 行为完全不同。

另外要再次强调那道分界线（承接 u1-l3）：`parse_args()` 读的是**命令行**（`sys.argv`），只决定「做哪些动作」；而动作需要的**数据**——`repoPath` 与 `dependencies`——是 `ExecMain` 的 Python 实参，由客户端传入。命令行只负责「意图」，数据负责「对象」。

> 名词解释：**`Namespace`** 是 `argparse` 返回的轻量对象，本质上就是一个「用属性访问的字典」。`parse_args()` 返回后，你写 `args.list`、`args.mode` 就能取到各选项的值。

#### 4.2.2 核心流程

```text
args = parser.parse_args()           # 读 sys.argv → Namespace

if args.list:    ListDependencies(dependencies)     # 用调用方传入的 dependencies
if args.check:   CheckDependency(repoPath, dependencies)
if args.checkout: Checkout(repoPath, dependencies, mode, args.as_submodule)
```

设被按下的开关集合为 \(S \subseteq \{\text{list}, \text{check}, \text{checkout}\}\)，则被调用的动作集合就是 \(S\) 本身（一一对应、互不排斥）：

\[
\text{被调动作} = S, \qquad
\text{每个动作独立触发，执行顺序固定为 list} \to \text{check} \to \text{checkout}
\]

若 \(S = \varnothing\)（用户啥开关都没给），则**三段都不执行**——`ExecMain` 静默返回，什么也不做。这是「无副作用默认」。

`-as_submodule` 是个**从属开关**：它只在 `if args.checkout:` 块内被读取（作为 `Checkout` 的第四个实参 `args.as_submodule`）。所以**单独敲 `-as_submodule` 而不敲 `-checkout` 什么都不会发生**——它的 `help` 文字也明说了「must be used together with `-checkout`」。

#### 4.2.3 源码精读

解析这一行（[Actions.py:147](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L147)）——注意它**不传实参**，故默认读 `sys.argv`：

```python
args = parser.parse_args()
```

`-list` 分发块（[Actions.py:151-153](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L151-L153)）：

```python
if args.list:
    print("*** Dependencies ***")
    ListDependencies(dependencies)
```

`-check` 分发块（[Actions.py:155-157](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L155-L157)）：

```python
if args.check:
    print("*** Dependency Check ***")
    CheckDependency(repoPath, dependencies)
```

`-checkout` 分发块的开头（[Actions.py:160-161](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L160-L161)）：

```python
if args.checkout:
    print("*** Checkout ***")
    ...
```

关键点：

- **三个 `if` 各自独立**：源码里写的是 `if` / `if` / `if`（[Actions.py:151](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L151)、[L155](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L155)、[L160](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L160)），不是 `if/elif`。这是「可同时执行多个动作」的实现根源。
- **`print("*** ... ***")`**：每段动作先打印一个**小标题**，让用户在一屏输出里能区分「这是 list 的结果、那是 check 的结果」。
- **传给动作的实参**：`ListDependencies(dependencies)` 只用到依赖列表；`CheckDependency(repoPath, dependencies)` 与 `Checkout(repoPath, ...)` 还要用到仓库路径——二者都来自 `ExecMain` 的 Python 实参，**不是** `args` 里的东西。
- **`-as_submodule` 只在 checkout 块里被消费**：见 [Actions.py:170](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L170) 的 `Checkout(repoPath, dependencies, mode, args.as_submodule)`。

> 🔎 **「独立 `if`」带来一个真实后果**：因为不是互斥的，客户端完全可以一次敲 `-list -check`（甚至 `-list -check -checkout`），三段会**依次全部执行**。这在某些「先列一遍、再查一遍」的工作流里是有用的；但也意味着如果你只想做一个动作，就得**只给那一个开关**——本包没有「互斥校验」，多给也不会报错。

#### 4.2.4 代码实践

**实践目标**：在不修改源码的前提下，用一个驱动脚本**驱动真正的 `ExecMain`**（而不是 4.1.4 的替身解析器），观察「同时给多个开关会依次执行多段动作」这一行为。

**原理**：因为 `parse_args()` 默认读 `sys.argv`，所以只要在调用 `ExecMain` 之前**改写 `sys.argv`**，就能模拟「在命令行敲了某些开关」。这是从 Python 内部驱动命令行入口的标准手法。

**操作步骤**（示例代码，本包已按 u1-l2 安装、可被 `import`；本实践只触发 `-list` 与 `-check`，不做任何 git 操作，安全可跑）：

```python
import sys
from PsiFpgaLibDependencies import Actions
from PsiFpgaLibDependencies.Dependency import Dependency

# 构造两条假依赖（u2-l1 的数据模型）
deps = [
    Dependency("lib_a", "https://git.psi.ch/GFA/Lib/lib_a", "deps/lib_a", "1.2.0"),
    Dependency("lib_b", "https://git.psi.ch/GFA/Lib/lib_b", "deps/lib_b", "0.9.0"),
]

# 关键：改写 sys.argv，模拟用户敲了 "-list -check"
sys.argv = ["demo", "-list", "-check"]

# repoPath 用当前目录；-check 会发现 deps/lib_a 等目录不存在 → 打印 ERROR（不会崩溃）
Actions.ExecMain(".", deps)
```

**需要观察的现象**：

- 先打印 `*** Dependencies ***`，随后逐行列出两条依赖（`lib_a - <url> - 1.2.0`、`lib_b - <url> - 0.9.0`）——这是 `if args.list:` 块的产物。
- 紧接着打印 `*** Dependency Check ***`，并对每条依赖打印 `-- lib_a --` 后跟 `ERROR: Dependency deps/lib_a does not exist`——这是 `if args.check:` 块的产物（因为当前目录下并没有这些依赖目录）。
- **两段都执行了**——证明三个 `if` 是独立的、可叠加的，而不是互斥的 `if/elif`。

**预期结果**：`-list` 与 `-check` 的输出**先后都出现**在屏幕上。**待本地验证**：`-check` 在依赖目录不存在时只打印 `ERROR` 不抛异常（u3-l1 已说明），故本实践安全；请在本地实跑确认两段输出确实都出现。

> 进阶试验（可选）：把 `sys.argv` 改成 `["demo"]`（不给任何开关），再调一次 `ExecMain`——你会看到**什么也不打印**，因为三个 `if` 的条件全是 `False`。这就是「空参 = 无操作」的默认行为。

#### 4.2.5 小练习与答案

**练习 1**：如果把三个 `if` 改成 `if/elif/elif`，行为会怎样变化？这种改变对 `-list -check` 的用户是好是坏？

**参考答案**：改成 `if/elif` 后三段变为**互斥**——只会执行第一个被按下的开关对应的动作，其余被跳过。对 `-list -check` 用户而言，`-check` 会被静默忽略（只看到 list 的输出）。这取决于产品意图：若希望「一次只做一件事」就改 `elif` 并最好加互斥校验；若希望「可组合」就保留独立 `if`。本包选择了后者。

**练习 2**：为什么 `repoPath` 与 `dependencies` 不能也用 `add_argument` 从命令行读？

**参考答案**：因为 `dependencies` 是**结构化对象列表**（每条含 `libraryName/url/relativePath/minVersion`，且 `minVersion` 还要包成 `VersionNr`），在命令行上手工敲既繁琐又易错；而它本就由每个库的 `README.md` 声明、由 `Parse.FromReadme` 解析得来（见 u2-l3~u2-l5）。客户端的职责正是「解析 README 得到列表、确定仓库路径」，然后把这两者作为 Python 实参喂给 `ExecMain`。命令行只适合表达「轻量的动作意图」（开关、模式），不适合承载复杂数据。

**练习 3**：单独敲 `-as_submodule`（不给 `-checkout`），`Checkout` 会被调用吗？为什么？

**参考答案**：不会。`args.as_submodule` 只在 `if args.checkout:` 块内被读取（[Actions.py:170](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L170)）。若 `args.checkout` 为 `False`，整个块被跳过，`args.as_submodule` 即使为 `True` 也无人读取——这正是 `help` 文字强调「must be used together with `-checkout`」的原因。

---

### 4.3 mode 映射（字符串 → CHECKOUT_MODE）

#### 4.3.1 概念说明

`-checkout` 块在调用 `Checkout` 之前，要先解决一个「**翻译**」问题：命令行上的 `-mode` 给的是**字符串**（如 `"latest_release"`），而 `Checkout` 函数要的是 `CHECKOUT_MODE` **枚举**（如 `CHECKOUT_MODE.LatestRelease`）。两者之间需要一个映射。

为什么需要两层表示？

- **命令行层用字符串**：因为命令行就是文本，`"latest_release"` 对用户最直观、可读性最好。
- **函数层用枚举**：因为枚举（`CHECKOUT_MODE`）是受限的、可被 IDE 检查的、不会拼错的类型（见 [Actions.py:31-37](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L31-L37)），适合作为函数参数在代码内部传递。

于是 `ExecMain` 用一段 `if/elif/else` 把三个字符串翻译成三个枚举成员，再把翻译结果 `mode` 连同 `args.as_submodule` 一起传给 `Checkout`。

这里有一个**看似多余、实则意味深长**的细节：`if/elif` 链末尾还挂了一个 `else: raise Exception("Illegel -mode: ...")`。它为什么多余？因为 `-mode` 的合法取值已经被 `choices=[...]`（[Actions.py:146](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L146)）**钉死**了——任何不在这三个值里的输入，早在 `parse_args()` 阶段就被 `argparse` 拦下并退出进程了，根本走不到这段 `if/elif`。所以这个 `else` 在正常命令行路径下**永远不会被执行**，它是一段**防御性的死分支（dead code）**。

> 名词解释：**死分支（dead code / unreachable branch）** 指在程序的正常执行路径下永远无法到达的代码。它不一定是「错误」——有时作者故意写它作为「防御性编程」：万一将来约束被放松（例如有人去掉了 `choices`、或这段映射逻辑被别的入口复用），这行 `raise` 还能兜底报错，而不是让 `mode` 悄悄变成未定义。

#### 4.3.2 核心流程

```text
if args.mode == "latest_release":    mode = CHECKOUT_MODE.LatestRelease
elif args.mode == "master":          mode = CHECKOUT_MODE.Master
elif args.mode == "specified_version": mode = CHECKOUT_MODE.SpecifiedRelease
else:                                raise Exception("Illegel -mode: ...")   # 正常 CLI 下不可达

Checkout(repoPath, dependencies, mode, args.as_submodule)
```

映射可写成 piecewise 函数（定义域即 `choices` 集合）：

\[
\text{map}(m) = \begin{cases}
\text{Master}, & m = \texttt{"master"} \\
\text{LatestRelease}, & m = \texttt{"latest\_release"} \\
\text{SpecifiedRelease}, & m = \texttt{"specified\_version"} \\
\text{（不可达）raise}, & \text{otherwise}
\end{cases}
\]

注意：由于 `choices` 的存在，输入 \(m\) 必然落在前三行之一，第四行恒不可达。

#### 4.3.3 源码精读

`-checkout` 块的 mode 映射与最终调用（[Actions.py:160-170](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L160-L170)）——本模块的核心：

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

逐行解读：

- **`if args.mode == "latest_release":`** 等：把字符串逐一比对，命中则赋对应的枚举成员给局部变量 `mode`。`if/elif` 在这里保证**至多命中一支**。
- **`else: raise Exception("Illegel -mode: ...")`**（[Actions.py:168-169](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L168-L169)）：兜底分支。如前所述，在 `-mode` 受 `choices` 约束的前提下**不可达**。
- **`Checkout(repoPath, dependencies, mode, args.as_submodule)`**（[Actions.py:170](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L170)）：把仓库路径、依赖列表、翻译好的 `mode` 枚举、以及 `as_submodule` 布尔一起传给 `Checkout`，正式进入 u3-l2 讲过的检出流程。

> 🔎 **源码里藏着一个拼写错误**：[Actions.py:169](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L169) 的异常信息写的是 `"Illegel -mode: ..."`，正确拼写应是 **`Illegal`**。由于它是死分支，这个 typo 不会在任何正常运行中暴露，于是「侥幸」存活了下来。这是读源码时常见的一类现象：**越是不可能执行的代码，越容易藏着没人发现的问题**。

> ⚠️ **「双重防线」的设计意图**：`choices` 是第一道防线（`argparse` 层，提前拦截非法值并退出），`else: raise` 是第二道防线（业务层，理论上不可达）。这种「在数据入口和业务入口各校验一次」的写法，是为应对「将来这段映射代码可能被别的入口复用、而那个入口未必有 `choices`」的情形。你可以把它理解成：**`argparse` 的约束只对命令行入口有效，业务代码不应假设上游一定校验过**。

#### 4.3.4 代码实践

**实践目标**：验证「`-mode` 的合法值都能正确映射到对应枚举」，并利用一个**绕过 `choices` 的小技巧**，亲眼看一看那段「不可达」的 `else: raise` 长什么样、抛什么错。

**原理**：`choices` 只在 `parse_args()` 读命令行时生效。如果我们**手工构造一个 `args` 对象**（绕过 `parse_args`），就能塞进一个非法的 `mode` 值，从而触达那段死分支——用来验证它确实如预期地 `raise`。这属于「为了理解代码而刻意构造异常路径」的阅读型实践。

**操作步骤**（示例代码，只测映射与异常，不真正检出，安全可跑）：

```python
from PsiFpgaLibDependencies.Actions import CHECKOUT_MODE
import types

# 复刻 ExecMain 里的映射逻辑（4.3.3），便于孤立测试
def map_mode(s):
    if s == "latest_release":
        return CHECKOUT_MODE.LatestRelease
    elif s == "master":
        return CHECKOUT_MODE.Master
    elif s == "specified_version":
        return CHECKOUT_MODE.SpecifiedRelease
    else:
        raise Exception("Illegel -mode: {}".format(s))

# 1) 三个合法值应分别映射到三个枚举成员
assert map_mode("master")           == CHECKOUT_MODE.Master
assert map_mode("latest_release")   == CHECKOUT_MODE.LatestRelease
assert map_mode("specified_version")== CHECKOUT_MODE.SpecifiedRelease
print("三个合法 mode 映射正确")

# 2) 构造一个绕过 choices 的非法值，触发「死分支」
for bad in ["foo", "", "Master", "LATEST_RELEASE"]:
    try:
        map_mode(bad)
        print("UNEXPECTED: 未抛异常 ->", repr(bad))
    except Exception as e:
        print("非法值 {!r:>18} -> 抛出: {}".format(bad, e))
```

**需要观察的现象**：

- 步骤 1 的三条断言全部通过，证明字符串到枚举的逐一映射正确。
- 步骤 2 的四个非法值（`"foo"`、空串、大小写错误的 `"Master"`、`"LATEST_RELEASE"`）**每一个都抛出异常**，信息形如 `Illegel -mode: foo`（注意原样保留了源码里的 `Illegel` 拼写）。
- 特别留意 `"Master"`（大写 M）与 `"LATEST_RELEASE"`（大写）也会被拒——映射是**大小写敏感**的精确匹配，命令行必须严格写小写的 `master` 等。

**预期结果**：合法值映射正确、非法值落入 `else` 分支并抛出带 `Illegel` 字样的异常。**待本地验证**：映射逻辑是确定的，请实跑确认大小写敏感与异常文案。

> 阅读型收获：这个实践同时证明了两个命题——(a) `choices` 一旦被绕过，`else` 分支确实能兜底报错（防御性有效）；(b) 但在正常命令行路径下，`argparse` 永远不会让非法值走到这里（死分支）。两者并不矛盾。

#### 4.3.5 小练习与答案

**练习 1**：既然 `else: raise` 在正常命令行下不可达，作者为什么还要写它？删掉有什么风险？

**参考答案**：写它是出于**防御性编程**——万一将来有人去掉了 `-mode` 的 `choices`，或把这段映射逻辑抽出来给别的入口复用（那个入口可能不做 `choices` 校验），这行 `raise` 还能在运行时把非法值暴露成显式异常，而不是让 `mode` 保持未定义、在后续 `Checkout` 里引发更难排查的错误。删掉的风险正是「失去兜底」：约束一旦被放松，bug 会从「明确的异常」退化成「静默的未定义行为」。

**练习 2**：`-mode` 默认值是 `"latest_release"`，它对应的枚举是哪一个？这意味着「用户敲了 `-checkout` 但没敲 `-mode`」时，会走哪种检出模式？

**参考答案**：映射到 `CHECKOUT_MODE.LatestRelease`（[Actions.py:162-163](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L162-L163)）。所以「`-checkout` 不带 `-mode`」时，会走 `LatestRelease` 模式（用 `git tag` 取最大语义版本并 `git checkout` 它，见 u3-l2）。注意这**不同于**直接调 `Checkout(...)` 时的默认 `Master`——再次印证 4.1.3 指出的「两条入口默认 mode 不一致」。

**练习 3**：如果将来要新增第四种检出模式（例如 `"head"` 表示检出当前 HEAD），需要改 `ExecMain` 里的哪几处？

**参考答案**：至少三处——(a) `add_argument("-mode", choices=[...])` 的 `choices` 列表里加 `"head"`（[Actions.py:146](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L146)）；(b) `if/elif` 映射链里加一支 `elif args.mode == "head": mode = CHECKOUT_MODE.Head`（[Actions.py:162-167](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L162-L167)）；(c) 在 `CHECKOUT_MODE` 枚举里新增 `Head` 成员（[Actions.py:31-37](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L31-L37)），并在 `Checkout` 里为它实现实际行为（见 u3-l2）。三处缺一不可：漏了 `choices` 则命令行无法传入；漏了映射支则即使传入也被 `else` 拒；漏了枚举则无目标可映射。

---

## 5. 综合实践

把三个模块串起来：为 `ExecMain` **新增一个 `-version` 开关**，让它打印当前版本号，并验证 `argparse` 自动生成的帮助文本与分发逻辑。

**任务背景**：很多命令行工具都有 `--version` 开关，敲一下就打印版本号并退出。本包目前没有这个开关——版本号只写在 [setup.py:26](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/setup.py#L26)（`version="2.1.0"`）。你要给 `ExecMain` 加上它。

> 说明：本实践需要你在**本地副本**里修改 `Actions.py`（这是学习练习，不属于「不改源码」的约束——你改的是自己的工作副本，不是上游仓库）。下面所有改动均标注为**示例代码**。

**步骤 1：确定版本号来源**

版本号当前唯一写在 [setup.py:26](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/setup.py#L26)：

```python
version="2.1.0",
```

有两种取值策略（任选其一，建议先 A 再试 B）：

- **策略 A（简单，但重复）**：在 `Actions.py` 顶部加一个模块常量 `__version__ = "2.1.0"`，`-version` 时打印它。缺点是和 `setup.py` 的版本号**重复**，将来升级时容易忘记同步。
- **策略 B（不重复，推荐）**：用标准库从**已安装包的元数据**里读版本号，避免重复——

  ```python
  from importlib.metadata import version
  __version__ = version("PsiFpgaLibDependencies")   # 读 pip 安装时记录的版本
  ```

  前提是本包已按 u1-l2 用 `pip3 install` 安装过（元数据里才有版本号）。

**步骤 2：在 `ExecMain` 里新增 `-version` 开关（示例代码）**

参照 4.1 讲的 `store_true` 写法，在 `parser = ArgumentParser()` 之后、其它 `add_argument` 旁边加一行：

```python
parser.add_argument("-version", dest="version", help="Print version and exit",
                    required=False, default=False, action="store_true")
```

**步骤 3：在分发段加一个「打印即退出」的块（示例代码）**

`--version` 的惯例是「打印后立即退出、不执行其它动作」。所以在三段动作 `if` 的**最前面**加一段，打印后 `return`：

```python
if args.version:
    print(__version__)
    return
```

放在 [Actions.py:147](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L147)（`parse_args()`）之后、[L151](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L151)（`if args.list:`）之前即可。

**步骤 4：验证帮助文本与分发逻辑**

```python
import sys
from PsiFpgaLibDependencies import Actions
from PsiFpgaLibDependencies.Dependency import Dependency

deps = [Dependency("lib_a", "https://git.psi.ch/GFA/Lib/lib_a", "deps/lib_a", "1.2.0")]

# (a) 验证 -version：打印版本号后直接返回，不会触发后面的 -list
sys.argv = ["demo", "-version", "-list"]
Actions.ExecMain(".", deps)
# 期望：只打印版本号（如 2.1.0），不出现 "*** Dependencies ***"

# (b) 验证帮助页：argparse 会把新开关的 help 文本拼进去
sys.argv = ["demo", "-h"]
try:
    Actions.ExecMain(".", deps)
except SystemExit:
    pass
# 期望：帮助页里能看到一行 -version 的说明
```

**需要观察的现象**：

- 步骤 4(a)：屏幕上**只**打印版本号（`2.1.0`），**没有** `*** Dependencies ***`——证明 `return` 让 `-version` 提前结束、阻止了后续动作（与 `--version` 惯例一致）。
- 步骤 4(b)：`argparse` 自动生成的帮助页里多出一行，形如 `-version     Print version and exit`——证明 `help="..."` 被正确采集。
- 若用策略 B：打印的版本号与 `pip3 show PsiFpgaLibDependencies` 显示的 Version 一致。

**预期结果**：`-version` 打印版本号并短路返回；`-h` 帮助页含新开关说明。**待本地验证**：步骤 1–3 需要在本地副本改代码并重新 `import`（或重启解释器）才能生效；策略 B 还要求本包已安装。请实跑确认。

**反思题**：

1. 为什么把 `if args.version:` 放在三段动作 `if` 的**最前面**，而不是最后面？如果放最后会怎样？
2. 用策略 A（常量）时，`setup.py` 与 `Actions.py` 两处版本号会「双写」。除了策略 B 的 `importlib.metadata`，你还能想到什么办法让版本号「单一来源」？（提示：让 `setup.py` 反过来从包里读 `__version__`。）
3. `argparse` 自带的 `-h/--help` 也是「打印即退出」的开关。你的 `-version` 与它在行为上有哪些相似、哪些不同？

## 6. 本讲小结

- `ExecMain` 是本包唯一的命令行入口：用 `argparse` 定义五个选项——`-list/-check/-checkout/-as_submodule` 四个 `store_true` **开关**（在场即真、缺席即假），外加 `-mode` 一个**带值选项**并用 `choices` 限定取值。
- 四个开关上的 `required=False`、`default=False`、显式 `dest` 其实都是 `argparse` 在 `store_true` + 单横杠情境下的**默认行为**，属「靠冗余换可读性」的显式写法；`-mode` 没写 `action` 故走默认 `store`，存的是字符串。
- 分发用**三个独立的 `if`**（不是 `elif`），因此 `-list -check` 等多开关会**依次全部执行**；`repoPath` 与 `dependencies` 是 Python 实参（由客户端传入），命令行只负责「动作意图」。
- `-as_submodule` 是**从属开关**：只在 `if args.checkout:` 块内被读取，单独敲它而不敲 `-checkout` 不产生任何效果。
- `-mode` 字符串经 `if/elif` 翻译成 `CHECKOUT_MODE` 枚举后传给 `Checkout`；末尾的 `else: raise Exception("Illegel -mode: ...")` 在正常命令行下**不可达**（已被 `choices` 拦截），是「防御性死分支」，且信息里的 `Illegel` 是个未暴露的拼写错误。
- **两条入口默认 mode 不一致**：命令行 `-mode` 默认 `"latest_release"`，而 `Checkout` 函数签名默认 `CHECKOUT_MODE.Master`；走不同入口、不指定 mode 时行为不同，二次开发须留意。

## 7. 下一步学习建议

- **回头串读 [u3-l6](u3-l6-packaging-release.md)（打包、版本与发布流程）**：本讲综合实践里的「版本号单一来源」问题，正是 u3-l6 的核心议题——`setup.py` 的 `CustomSdist` 如何在打包前清理产物、`version` 字段如何随 Tagging Policy 递增。把 `ExecMain.-version` 的版本来源与 `setup.py` 的版本发布流程连起来读，能看清「版本号在项目里从头到尾的生命周期」。
- **回头巩固 [u3-l1](u3-l1-list-check.md) 与 [u3-l2](u3-l2-checkout-modes.md)**：本讲的三个 `if` 分发块，目标分别是 `ListDependencies`、`CheckDependency`、`Checkout`。重读这两个动作的实现，能帮你把「命令行开关 → 动作函数 → 具体行为」这条链补全。
- **延伸思考（argparse 进阶）**：本讲的 `parser` 没有设置 `prog` 与 `description`，所以帮助页较朴素。试着给 `ArgumentParser(prog=..., description=..., epilog=...)` 加上项目说明，对比帮助页的变化；再思考能否用 `mutually_exclusive_group()` 把 `-list/-check/-checkout` 改成互斥组（对照 4.2.5 练习 1 的讨论）。
- **动手延伸**：参考 4.3.5 练习 3，真刀真枪地为 `Checkout` 新增第四种模式（例如 `head`）——同步改 `choices`、映射链、`CHECKOUT_MODE` 枚举与 `Checkout` 实现，跑通一次「命令行 → 枚举 → 检出行为」的完整闭环，检验你是否真正掌握了本讲的「参数定义 → 分发 → mode 映射」三段式。
