# 跨平台路径处理：AbsPathLinuxStyle 与环境变量

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 Windows 与 Linux 在「路径分隔符」与「斜杠方向」上的两处差异，以及为什么这两处差异会让同一段代码在两个系统上行为不同。
- 读懂 [`FileOperations.AbsPathLinuxStyle`](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/FileOperations.py#L56-L64) 这一行函数，并解释它在 Linux 上为何几乎是「空操作」、在 Windows 上才有实际意义。
- 读懂 [`EnvVariables.AddToPathVariable`](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/EnvVariables.py#L9-L42) 的「按 OS 选分隔符 → 转斜杠 → 缺失则建 → 已存在则跳 → 否则追加」五步逻辑，并指出它的幂等性来自哪里。
- 理解 `sys.platform` 字符串前缀分支这一跨平台写法，并知道哪些操作系统会被本库拒绝。

本讲承接 u3-l1（`FileOperations` 的正则通配符函数族），把目光聚焦到该模块里与「跨平台」直接相关的 `AbsPathLinuxStyle`，并延伸到同主题的另一个模块 `EnvVariables`。

## 2. 前置知识

### 2.1 路径分隔符（path separator）vs 斜杠方向（slash direction）

这是两个容易被混淆的概念，本讲反复用到，务必先分清：

- **斜杠方向**指单条路径**内部**用什么字符连接各级目录：
  - Linux 用正斜杠 `/`，例如 `/home/user/proj`。
  - Windows 用反斜杠 `\`，例如 `C:\Users\proj`。
- **路径分隔符**指一个**环境变量里**同时存放多条路径时，用什么字符把多条路径隔开：
  - Linux 用冒号 `:`，例如 `PATH=/usr/bin:/usr/local/bin`。
  - Windows 用分号 `;`，例如 `PATH=C:\Windows;C:\Tools`。

记忆要点：分隔符是「路径之间」的分隔，斜杠是「路径之内」的连接。两者在两个系统上恰好都是相反的字符对，这正是跨平台代码要处理的全部麻烦。

### 2.2 绝对路径与 `os.path.abspath`

`os.path.abspath(path)` 把任意路径转成「以根开头的绝对路径」，并顺手做一遍当前操作系统的规范化（折叠多余的 `..`、`.`，补上当前工作目录前缀）。它的输出风格**取决于运行时的 OS**：在 Windows 上吐出反斜杠，在 Linux 上吐出正斜杠。`AbsPathLinuxStyle` 的核心就是在这之后强制把斜杠拉直。

### 2.3 环境变量与 `os.environ`

`os.environ` 是一个类似字典的对象，对应**当前进程**的环境变量。读写它只会影响当前进程及其**子进程**，不会回传给启动它的父 shell。`AddToPathVariable` 的全部操作都发生在 `os.environ` 上，因此它改的是「程序自己看到的 PATH」，而不是「你终端里的 PATH」——这一点在实践环节会亲手验证。

### 2.4 `sys.platform` 是什么

`sys.platform` 是一个字符串，标识 Python 解释器运行在哪种系统上。常见取值：

| 系统 | `sys.platform` 值 |
|---|---|
| Windows | `win32` |
| Linux（含 WSL） | `linux` |
| macOS | `darwin` |
| Cygwin | `cygwin` |

`AddToPathVariable` 正是用这个字符串的前缀来分派「用哪种分隔符、转哪个方向的斜杠」。

## 3. 本讲源码地图

本讲只涉及两个文件，都很短：

| 文件 | 作用 | 本讲关注点 |
|---|---|---|
| [FileOperations.py](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/FileOperations.py#L1-L64) | 正则通配符文件操作 + 一个跨平台路径函数 | `AbsPathLinuxStyle`（第 56–64 行） |
| [EnvVariables.py](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/EnvVariables.py#L1-L42) | 操作环境变量里的路径 | `AddToPathVariable`（第 9–42 行） |

提示：`FileOperations` 的其余三个函数（`RemoveWithWildcard` / `FindWithWildcard` / `OpenWithWildcard`）已在 u3-l1 讲过，本讲不再重复；`AbsPathLinuxStyle` 在 u3-l1 只是被点名带过，本讲才正式拆解。

## 4. 核心概念与源码讲解

### 4.1 AbsPathLinuxStyle：把任意路径变成正斜杠绝对路径

#### 4.1.1 概念说明

很多 EDA 工具（Xilinx SDK、Vivado 等）即使跑在 Windows 上，也只认「Linux 风格」的路径——即用正斜杠 `/` 连接、且最好是绝对路径。当你用 Python 在 Windows 上为这些工具生成配置文件或脚本时，手上的路径往往是 `..\..\src\top.v` 这种 Windows 相对路径，直接喂给工具会报错。

`AbsPathLinuxStyle` 就是解决这个错配的一行函数：**不管输入是相对还是绝对、是 Windows 风格还是 Linux 风格，统一吐出一个「正斜杠的绝对路径」**。它的名字本身就是规格说明——`Abs`（绝对）+ `Path` + `LinuxStyle`（正斜杠）。

注意它**只改字符串形态，不校验路径是否真实存在**：输入一个不存在的目录，它照样给你拼出一个看起来合法的绝对路径。

#### 4.1.2 核心流程

该函数只有两步，顺序固定：

```text
输入 path（相对或绝对，任意斜杠方向）
   │
   ▼
1) os.path.abspath(path)   →  转绝对路径，并按当前 OS 规范化
   │                         （Windows 上结果含 \，Linux 上结果含 /）
   ▼
2) .replace("\\", "/")     →  把所有反斜杠换成正斜杠
   │
   ▼
输出：正斜杠绝对路径
```

关键直觉：第 1 步的输出风格依赖 OS，第 2 步则把「OS 依赖」抹平，强制收敛到 Linux 风格。因此：

- **在 Linux 上**，第 1 步本就吐正斜杠，第 2 步找不到反斜杠可替换，整步是「空操作」（no-op）——除非文件名里恰好含字面意义的反斜杠（Linux 允许反斜杠作为普通文件名字符）。
- **在 Windows 上**，第 1 步吐反斜杠，第 2 步才真正起作用，把 `C:\a\b` 变成 `C:/a/b`。注意驱动器号的冒号 `C:` 会被保留，所以结果是「带盘符的正斜杠路径」，这正是 Vivado 等工具在 Windows 上能接受的形态。

#### 4.1.3 源码精读

完整函数如下（仅一行函数体）：

[FileOperations.py:L56-L64](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/FileOperations.py#L56-L64) —— 定义 `AbsPathLinuxStyle`，先把 `path` 转成当前系统下的绝对路径，再把所有反斜杠替换成正斜杠。

```python
def AbsPathLinuxStyle(path : str) -> str:
    """
    This function returns an absolute path in linux style (forward slashes),
    also if it is executed on Windows. ...
    """
    return os.path.abspath(path).replace("\\", "/")
```

要点拆解：

- 形参 `path: str` 与返回值 `-> str` 都有类型注解，语义是「字符串进、字符串出」。
- `os.path.abspath` 已在 2.2 节解释；它还顺带折叠 `..` 与 `.`，例如 `AbsPathLinuxStyle("/a/b/../c")` 会被规范化掉 `..`。
- `.replace("\\", "/")` 是字符串的**全文替换**（不是只替换一处），因此路径里有多少反斜杠都会被换掉。
- 函数体内没有任何 `if`、没有 OS 判断——它不需要，因为「把 `\` 换成 `/`」这个动作在两个系统上都是安全的：Linux 上要么没 `\` 可换（典型情况），要么把字面反斜杠换掉（罕见情况）；Windows 上正是想要的效果。

#### 4.1.4 代码实践

**实践目标**：亲手观察 `AbsPathLinuxStyle` 对「反斜杠相对路径」的转换结果，并对比它与裸 `os.path.abspath` 的差别。

**操作步骤**：

1. 在仓库根目录启动 `python3`。
2. 先导入函数：`from FileOperations import AbsPathLinuxStyle`。
3. 准备一个反斜杠相对路径字符串：`p = "subdir\\file.txt"`（注意 Python 字符串里 `\\` 表示一个字面反斜杠）。
4. 打印 `os.path.abspath(p)` 与 `AbsPathLinuxStyle(p)`，对比两者。

**需要观察的现象**：

- 裸 `os.path.abspath` 在 Linux 上会输出形如 `/当前工作目录/subdir\file.txt`——反斜杠**被当作文件名里的普通字符**保留下来，因为 Linux 上 `\` 不是分隔符。
- `AbsPathLinuxStyle` 的输出则会把这些反斜杠也换成 `/`，得到形如 `/当前工作目录/subdir/file.txt`。

**预期结果**：两者的区别**正是那一步 `.replace("\\", "/")`**。绝对路径前缀取决于你执行时的当前工作目录，具体前缀请以本机实际输出为准（前缀部分「待本地验证」，但「反斜杠→正斜杠」这一转换是确定的）。

**注意**：这个实践也顺带暴露了一个微妙事实——在 Linux 上，`subdir\file.txt` 本是一个单层文件名，转换后却「看起来」像两层目录。这说明 `AbsPathLinuxStyle` 的设计初衷是 Windows 场景，在 Linux 上虽然能跑，但语义并不完全对称。理解这一点比记住输出更重要。

#### 4.1.5 小练习与答案

**练习 1**：`AbsPathLinuxStyle` 既不判断 OS，也不读取 `sys.platform`，为什么仍然能在 Windows 与 Linux 上都给出「正斜杠」结果？

**参考答案**：因为它的核心动作「把 `\` 替换成 `/`」对两个系统都是安全且正确的——Windows 上 `\` 是需要换掉的分隔符，Linux 上要么没有 `\`、要么换掉也无害。它把「OS 差异」压缩成了「一个无条件字符串替换」，所以无需分支。

**练习 2**：调用 `AbsPathLinuxStyle("a/b/../c/d.txt")`，结果里还会出现 `..` 吗？为什么？

**参考答案**：不会。`os.path.abspath` 在转绝对路径时会规范化掉 `..`（把 `a/b/../c` 折成 `a/c`），替换斜杠这一步只改方向、不再改结构，所以最终结果里不含 `..`。

**练习 3**：如果想把函数名里「Linux 风格」的承诺再收紧成「纯 Unix 路径（连 Windows 盘符冒号都不要）」，仅靠现有这一行能实现吗？

**参考答案**：不能。现有函数会保留 `C:` 这样的盘符冒号，因为它只替换 `\`、不动冒号。要进一步去掉盘符语义，需要额外的字符串处理（例如切掉 `:` 及其前的盘符字母），这超出了本函数的职责边界。

---

### 4.2 AddToPathVariable：跨平台、幂等地向 PATH 类变量追加路径

#### 4.2.1 概念说明

PATH 类环境变量（如 `PATH`、`LD_LIBRARY_PATH`、`PYTHONPATH`）的特点是「一条变量里塞多条路径」，多条路径之间用 2.1 节定义的**路径分隔符**隔开。往这种变量里「加一条路径」远比看上去麻烦，因为你必须同时处理：

1. 用对**分隔符**（Windows `;` / Linux `:`）。
2. 用对**斜杠方向**（与 OS 一致）。
3. 变量**不存在**时要先创建，而不是假设它已经在那里。
4. 同一条路径**重复添加**时不应该出现两次（幂等）。

`AddToPathVariable(variable, path)` 正是封装了这四件事的助手。它的对外承诺写在 docstring 里：**若路径尚未存在则加入，已存在则跳过；变量不存在则创建；路径会在 Linux/Windows 风格间自动转换。**

#### 4.2.2 核心流程

函数分五个阶段，顺序不可乱：

```text
输入：variable（变量名）, path（要加的路径）
   │
   ▼
1) 按 sys.platform 选定：分隔符 varSep、斜杠转换方向 repFrom→repTo
   │   （Windows:  ";" , "/" → "\"   ；Linux: ":" , "\" → "/"）
   ▼
2) pathConv = path.replace(repFrom, repTo)        # 把路径拉到本系统风格
   │
   ▼
3) 若 variable 不在 os.environ：                 # 缺失则建
        os.environ[variable] = pathConv ; return
   │
   ▼
4) 若 pathConv 已在 os.environ[variable].split(varSep) 中：   # 已存在则跳
        return
   │
   ▼
5) 否则：os.environ[variable] += varSep + pathConv            # 否则追加
```

幂等性来自第 4 步：在追加前先把现有值按分隔符切成列表，做一次精确的**成员判断**，命中就直接返回。于是无论你调用一次还是十次同一个 `path`，变量里最终只出现一条。

一个小细节值得记下：`split(varSep)` 对「只有一条路径、没有分隔符」的值也成立——`"C:\\foo".split(";")` 得到 `["C:\\foo"]`，成员判断照常工作，所以第 4 步对「变量里本来就只有一条路径」的情况同样正确。

#### 4.2.3 源码精读

[EnvVariables.py:L9-L42](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/EnvVariables.py#L9-L42) —— 定义 `AddToPathVariable`：先用 OS 分支确定分隔符与斜杠方向，再把路径转成本系统风格，随后按「缺失则建、已存在则跳、否则追加」三段处理 `os.environ`。

```python
def AddToPathVariable(variable : str, path : str):
    #Get OS Settings
    if sys.platform.startswith("win"):
        varSep = ";"
        repFrom = "/"
        repTo = "\\"
    elif sys.platform.startswith("linux"):
        varSep = ":"
        repFrom = "\\"
        repTo = "/"
    else:
        raise Exception("OS Not Supported")
    #Convert Path
    pathConv = path.replace(repFrom, repTo)

    #If variable does not yet exist, create it
    if variable not in os.environ:
        os.environ[variable] = pathConv
        return

    #Check if path is already in os variable and return if this is the case
    if pathConv in os.environ[variable].split(varSep):
        return

    #Otherwise append
    os.environ[variable] += "{}{}".format(varSep, pathConv)
```

逐段说明：

- **OS 分支（第 19–28 行）**：见 4.3 节精读。它产出四个局部量 `varSep` / `repFrom` / `repTo`，并在遇到不支持的 OS 时抛 `Exception("OS Not Supported")`。
- **斜杠转换（第 30 行）**：`pathConv = path.replace(repFrom, repTo)`。注意这里用的是与 `AbsPathLinuxStyle` **相反**的方向——`AbsPathLinuxStyle` 永远把 `\` 转成 `/`（面向 Linux 风格工具）；而 `AddToPathVariable` 转向「当前系统的原生风格」，因为环境变量是给本系统的 shell / 程序读的，必须用 OS 原生斜杠。这是两个函数定位不同的关键。
- **缺失则建（第 33–35 行）**：`if variable not in os.environ` 直接赋值并 `return`，避免在「空变量」上做拼接。
- **已存在则跳（第 38–39 行）**：把现有值 `split(varSep)` 后做 `in` 判断。这是幂等性的核心。
- **否则追加（第 42 行）**：用 `"{}{}".format(varSep, pathConv)` 在前面拼上分隔符再追加，保证多条路径之间恰好有一个分隔符。

#### 4.2.4 代码实践

**实践目标**：用一个自定义变量名验证「加入两条不同路径」与「重复加入同一条路径」两种情形，亲眼看到幂等效果。

**操作步骤**：

1. 在仓库根目录启动 `python3`，导入函数：`from EnvVariables import AddToPathVariable`。
2. 选一个几乎不可能撞名的变量名，例如 `PSI_TUT_TEST_PATH`。
3. 调用 `AddToPathVariable("PSI_TUT_TEST_PATH", "/opt/libA")`，打印 `os.environ["PSI_TUT_TEST_PATH"]`。
4. 再调用 `AddToPathVariable("PSI_TUT_TEST_PATH", "/opt/libB")`，打印同一变量。
5. 第三次调用 `AddToPathVariable("PSI_TUT_TEST_PATH", "/opt/libA")`（重复第一次），再打印。

**需要观察的现象**：

- 第 3 步后变量值应为 `/opt/libA`（变量原本不存在，被新建）。
- 第 4 步后应为 `/opt/libA:/opt/libB`（用 `:` 追加，因为你在 Linux 上运行）。
- 第 5 步后**仍应保持** `/opt/libA:/opt/libB`——`/opt/libA` 没有被第二次加入，幂等性生效。

**预期结果**：最终值恰好是 `/opt/libA:/opt/libB`，没有重复条目。若你把变量名换成 `PATH`（系统已有），逻辑同样成立，只是初始值会很长。

**附带的批判性观察（衔接 u5-l3）**：第 4 步的成员判断是**精确字符串匹配**。在 Linux 上路径大小写敏感，这没问题；但在 Windows 上路径大小写不敏感，`C:\Foo` 与 `c:\foo` 会被判为「不同」从而都被加入——这是一个跨平台语义缺口，本库目前未处理。记住这一点，到 u5-l3「批判性读源码」会系统盘点。

#### 4.2.5 小练习与答案

**练习 1**：为什么第 4 步用 `os.environ[variable].split(varSep)` 而不是直接 `pathConv in os.environ[variable]` 做子串判断？

**参考答案**：因为子串判断会误判——若变量里已有 `/opt/lib`，用子串判断会认为 `/opt/lib` 已存在而错误跳过 `/opt/libXYZ`（后者是前者的子串）。`split` 后按「整段路径」做成员判断，才能精确到单条路径级别。

**练习 2**：假设变量已存在且值为空字符串 `""`，调用 `AddToPathVariable` 加一条路径，结果会是什么？有没有小瑕疵？

**参考答案**：会走第 5 步追加，结果是 `";<path>"`（或 Linux 上 `":<path>"`）——即开头多出一个分隔符。因为 `"".split(varSep)` 得到 `[""]`，而 `<path>` 不在其中，于是触发追加。这是一个边界瑕疵：理想做法是当原值为空时直接赋值而非拼接。本库未特殊处理这一情况。

**练习 3**：把 `AddToPathVariable` 的斜杠转换方向（第 30 行）与 `AbsPathLinuxStyle` 的方向对比，为什么两者相反？

**参考答案**：`AbsPathLinuxStyle` 面向「只认 Linux 风格的 EDA 工具」，所以永远输出 `/`；`AddToPathVariable` 面向「本系统的 shell 与程序」，所以必须输出当前 OS 的原生斜杠——Windows 上转成 `\`、Linux 上转成 `/`。用途不同，方向相反。

---

### 4.3 sys.platform 分支：用字符串前缀识别操作系统

#### 4.3.1 概念说明

`AddToPathVariable` 之所以能「按系统选分隔符、选斜杠方向」，靠的就是开头那个 `sys.platform` 分支。这是一种典型的、轻量级的跨平台写法：**不引入额外的抽象层，而是用一组 `if/elif/else` 把「OS → 行为参数」的映射直接写死在函数里**。

这里有一个值得注意的设计选择：分支条件用的是 `sys.platform.startswith("win")` 与 `sys.platform.startswith("linux")`，而不是 `sys.platform == "win32"` / `== "linux"`。用 `startswith` 是为了兼容未来可能出现的带后缀的 platform 字符串（例如假设出现 `win64`、`linux2` 这类变体），用前缀匹配能更稳地命中。

而 `else: raise Exception("OS Not Supported")` 是一条**显式的失败**：本库不打算默默猜 macOS / Cygwin 的行为，宁可立刻报错也不给出一个可能错误的路径风格。这是一个「快速失败」的工程取向。

#### 4.3.2 核心流程

分支本质上是一张「OS → 三个参数」的查表：

```text
sys.platform 前缀      varSep   repFrom → repTo     说明
─────────────────────────────────────────────────────────
"win"        →         ";"       "/"  →  "\"        Windows：分号分隔，转成正斜杠
"linux"      →         ":"       "\"  →  "/"        Linux ：冒号分隔，转成正斜杠
其它          →         raise Exception("OS Not Supported")
```

三个产出的局部量随后被 4.2 节的第 2、4、5 步消费：

- `varSep`：既用于第 4 步的 `split`（拆出已有路径列表），又用于第 5 步的拼接。
- `repFrom` / `repTo`：用于第 2 步的斜杠转换。

#### 4.3.3 源码精读

[EnvVariables.py:L19-L28](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/EnvVariables.py#L19-L28) —— `AddToPathVariable` 内的 OS 分支：用 `sys.platform.startswith(...)` 选定 `varSep` / `repFrom` / `repTo` 三个量，遇到 Windows 与 Linux 之外的系统直接抛异常。

```python
    if sys.platform.startswith("win"):
        varSep = ";"
        repFrom = "/"
        repTo = "\\"
    elif sys.platform.startswith("linux"):
        varSep = ":"
        repFrom = "\\"
        repTo = "/"
    else:
        raise Exception("OS Not Supported")
```

观察要点：

- `import sys` 出现在 [EnvVariables.py:L7](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/EnvVariables.py#L7)，`sys.platform` 才可用。
- 这段代码**只产出参数、不做 IO**，把「OS 差异」与「环境变量操作」解耦——这是它能在 4.2 节被干净复用的原因。
- 抛的是通用 `Exception`（不是自定义异常类），调用方只能靠异常消息文本 `"OS Not Supported"` 来区分——与 u3-l1 里 `OpenWithWildcard` 的做法一致，都是「无自定义异常」的写法，这也是 u5-l3 会盘点的代码风格议题之一。

#### 4.3.4 代码实践

**实践目标**：确认你当前机器的 `sys.platform` 取值，并推理在 macOS 上调用本函数会发生什么。

**操作步骤**：

1. 启动 `python3`，执行 `import sys; print(sys.platform)`。
2. 再执行 `sys.platform.startswith("win")` 与 `sys.platform.startswith("linux")`，各打印一次布尔值。
3. （纯阅读型）设想你在 macOS 上（`sys.platform == "darwin"`）调用 `AddToPathVariable("X", "/tmp")`，根据 4.3.3 的源码推断会发生什么。

**需要观察的现象**：

- 在本机（Linux）上，`sys.platform` 应为 `linux`，两个 `startswith` 分别为 `False` 与 `True`。
- 在 macOS 上，`startswith("win")` 与 `startswith("linux")` **都为 False**，于是落入 `else` 分支。

**预期结果**：本机两个布尔值为 `False`、`True`。macOS 推断结论是抛出 `Exception: OS Not Supported`——即本库明确不支持 macOS。这一结论无需真在 macOS 上跑，直接由源码的 `else` 分支得出。

#### 4.3.5 小练习与答案

**练习 1**：把 `sys.platform.startswith("linux")` 改成 `sys.platform == "linux"` 会不会更「严谨」？会有什么潜在风险？

**参考答案**：表面上看 `==` 更精确，但历史上 Python 在某些平台曾返回过 `linux2`（Python 2 时代）等带后缀的值。`startswith("linux")` 能兼容这类变体，鲁棒性更好；用 `==` 则可能在遇到变体时意外落入 `else` 抛「不支持」。这是一个用「前缀匹配」换取前向兼容的典型取舍。

**练习 2**：如果要在不修改源码的前提下，让本函数在 macOS 上也能工作，外部有没有简便办法？

**参考答案**：没有干净的运行时办法，因为分支基于 `sys.platform` 这个**只读的系统事实**。可行的是修改源码，给 `darwin` 增加一个分支（macOS 与 Linux 一样用 `:` 分隔、`/` 斜杠，可直接复用 Linux 分支的三个参数）。这正是「扩展点」所在。

**练习 3**：本分支抛的是 `Exception("OS Not Supported")` 而非自定义异常。从「调用方想区分各类错误」的角度，这会带来什么不便？

**参考答案**：调用方只能用 `except Exception` 捕获，再靠字符串匹配消息 `"OS Not Supported"` 来判断原因，既不安全（消息文本可能被改）也无法与其它 `Exception` 区分开。若改成自定义异常类（如 `OSNotSupportedError`），调用方就能用 `except OSNotSupportedError` 精确捕获——这是 u5-l3「批判性读源码」会再次提到的风格改进点。

---

## 5. 综合实践

设计一个把本讲三个最小模块串起来用的小任务：**为一个跑在 Windows 上的 Vivado 工具流，准备一份「Linux 风格的库搜索路径」并同步到环境变量。**

场景设定：你在 Windows 上用 Python 调用 Vivado，Vivado 只认正斜杠绝对路径；同时你想把这些库目录加进一个自定义环境变量 `VIVADO_LIB_PATH` 供子进程读取。请写一个脚本完成下列事情（可在本机 Linux 上模拟，重点是逻辑正确）：

1. 准备两条「Windows 风格」相对路径字符串：`"src\\ip\\libA"` 与 `"src\\ip\\libB"`。
2. 用 `AbsPathLinuxStyle` 把它们各转一遍，得到两条正斜杠绝对路径，打印出来——这是给 Vivado 用的。
3. 用 `AddToPathVariable("VIVADO_LIB_PATH", ...)` 把这两条路径加进环境变量（注意：`AddToPathVariable` 会按当前 OS 再转一次斜杠；在本机 Linux 上它不会改变正斜杠，恰好与第 2 步一致）。
4. 重复一次第 3 步对 `libA` 的加入，打印 `os.environ["VIVADO_LIB_PATH"]`，确认没有重复。
5. 写一段 2–3 行的结论说明：为什么「给工具的正斜杠路径」与「给环境变量的路径」在本机 Linux 上看起来一致，而在 Windows 上会**分歧**（一个永远 `/`，一个转成 `\`）。

**验收标准**：

- 第 2 步两条路径都被正确转成绝对路径并以正斜杠呈现（绝对前缀以本机为准，待本地验证）。
- 第 4 步后 `VIVADO_LIB_PATH` 中 `libA` 只出现一次。
- 第 5 步的结论能点出 `AbsPathLinuxStyle` 与 `AddToPathVariable` 斜杠方向相反的根本原因（见 4.2.5 练习 3）。

这个任务把三个模块都过了一遍：`AbsPathLinuxStyle`（4.1）负责「对外给工具」的路径，`AddToPathVariable`（4.2）负责「对内给本系统」的路径，而两者能协同的底座正是 `sys.platform` 分支（4.3）。

## 6. 本讲小结

- 跨平台路径的两处核心差异是**斜杠方向**（路径内部连接符）与**路径分隔符**（环境变量里多条路径之间的分隔符），Windows 用 `\` 与 `;`，Linux 用 `/` 与 `:`。
- `AbsPathLinuxStyle` = `os.path.abspath(path).replace("\\", "/")`，无条件把路径变成「正斜杠绝对路径」，面向 Vivado/SDK 等 Linux 风格工具；它在 Linux 上几乎是空操作，在 Windows 上才真正起作用。
- `AddToPathVariable` 按「选分隔符/斜杠方向 → 转换 → 缺失则建 → 已存在则跳 → 否则追加」五步幂等地修改 `os.environ`，且只影响当前进程及其子进程。
- 幂等性来自追加前的 `split(varSep)` + 精确成员判断；但该判断是大小写敏感的精确字符串匹配，在 Windows（路径大小写不敏感）上存在语义缺口。
- `sys.platform.startswith(...)` 分支是本库的跨平台分派中枢，用前缀匹配换取前向兼容，并以 `raise Exception("OS Not Supported")` 明确拒绝 macOS 等未适配系统。
- 两个函数都**没有测试**（`EnvVariables` 整个模块无测试，`AbsPathLinuxStyle` 在 `TestFileOperations` 中也未被覆盖）——这是 u5-l3「批判性读源码」要回收的测试缺口之一。

## 7. 下一步学习建议

- 下一讲 **u3-l3 TextReplace：标签间文本替换** 会把「字符串处理」主题推进到正则替换层面，讲解非贪婪 `.*?` 与 `re.DOTALL`，与本讲的 `replace` 字符串替换形成由浅入深的对照。
- 如果你对「无自定义异常、用通用 `Exception`」这一风格议题感兴趣，可先跳读 **u5-l3 测试组织与批判性读源码**，那里系统盘点全库的测试缺口与代码风格改进点。
- 想动手补测的读者，可以为 `EnvVariables.AddToPathVariable` 写 3 个用例（新建变量、去重、跨平台分隔符），这是本讲留下的、价值最高的练习之一。
