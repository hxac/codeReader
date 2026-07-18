# 依赖数据模型 Dependency

## 1. 本讲目标

本讲承接 u1-l3（包入口与客户端集成方式）。上一讲已经让读者亲手写过 `PFD.Dependency("psi_common", "https://...", "../psi_common", "1.0.0")` 这样的调用，也看到了 `-list` 把它打印成 `库名 - URL - 版本` 一行。但我们当时刻意**只字未提** `Dependency` 内部到底长什么样——这一讲就来把它拆开看清楚。

`Dependency` 是本包的**核心数据模型**：无论是 `Parse.FromReadme` 从 README 里解析依赖，还是 `Actions` 里的列出/检查/检出动作，传递的都是「一条条 `Dependency`」。可以说，理解了 `Dependency` 的四个字段，就握住了本包所有模块之间流通的「共同语言」。

学完本讲，读者应该能够：

- 说出 [`Dependency`](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Dependency.py) 的四个字段 `libraryName / url / relativePath / minVersion` 各自描述什么，并能解释为什么是这四个、而不是三个或五个。
- 解释构造函数里最关键的一行 `self.minVersion = VersionNr(minVersion)`：传入的是字符串，存下来的却是 `VersionNr` 对象，这层「包装」的目的和后果是什么。
- 会用 `GetParentDir()` 推导一条依赖「该被克隆到哪个父目录」，并能预测 `os.path.dirname` 在不同深度 `relativePath` 下的返回值。

---

## 2. 前置知识

- **Python 类与 `__init__` 构造函数**：类是一种「蓝图」，`__init__` 是「按蓝图造对象时自动执行的初始化方法」。`Dependency(...)` 的四个参数就是在这里被逐一存到对象身上的。
- **相对路径与 `..`**：路径里的 `..` 表示「上一级目录」。例如 `../common/libA` 意思是「从当前位置回到上一级，再进入 `common` 目录下的 `libA`」。本包里依赖的 `relativePath` 经常以一连串 `..` 开头，原因会在 4.3 解释，并在 u2-l5 深入。
- **`os.path.dirname`**：Python 标准库函数，作用是「砍掉路径的最后一段，留下它所在的目录」。例如 `os.path.dirname("../common/libA")` 得到 `"../common"`。本讲的 `GetParentDir` 就是直接转发给它。
- **类型注解（type hint）**：源码里写 `def __init__(self, libraryName : str, ...)`，冒号后的 `str` 是「提示这个参数应该是字符串」。它只是给人和工具看的约定，Python 运行时**不会**因此做强制检查。
- **承接 u1-l3 的两点共识**：① 本包对外导出 `Dependency` 这个**类**（不是模块），所以可以直接 `PFD.Dependency(...)` 构造对象；② 本包自己不解析 README，`Dependency` 列表要么由客户端手动构造，要么由 `PFD.Parse.FromReadme(...)` 解析出来。本讲专注「对象本身」，解析过程留给 u2-l3。

> 提醒：`Dependency.py` 顶部有 `from .VersionNr import VersionNr`（相对导入），所以它**必须作为包的一部分被导入**，不能把 `Dependency.py` 单独拷出来当脚本跑。请确认已按 u1-l2 用 `pip3 install dist/PsiFpgaLibDependencies-2.1.0.tar.gz` 装好本包。

---

## 3. 本讲源码地图

本讲围绕一个核心文件，并顺带触及「谁构造它」「谁消费它」的两端。

| 文件 | 角色 | 本讲关注点 |
| --- | --- | --- |
| [Dependency.py](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Dependency.py) | 源码（本讲主角） | 整个 `Dependency` 类：四个字段、`minVersion` 包装、`GetParentDir` |
| [VersionNr.py](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/VersionNr.py) | 源码（被依赖） | `VersionNr` 的构造与解析，解释「字符串如何变成可比较的版本号」；深入比较逻辑在 u2-l2 |
| [Parse.py](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Parse.py) | 源码（生产者） | 第 164 行是全项目唯一真正构造 `Dependency` 的地方，能反推出四个字段从哪来 |
| [Actions.py](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py) | 源码（消费者） | `ListDependencies / CheckDependency / Checkout / CheckCompatibility` 如何读取 `Dependency` 的各个字段 |

---

## 4. 核心概念与源码讲解

本讲按三个最小模块推进：先看「`Dependency` 用哪四个字段描述一条依赖」（构造函数与字段），再看「最低版本号为什么不是字符串而是 `VersionNr`」（minVersion 转 VersionNr），最后看「如何从一条依赖算出它的父目录」（GetParentDir）。

### 4.1 构造函数与字段

#### 4.1.1 概念说明

要管理「库与库之间的依赖」，每一条依赖至少要回答四个问题：

1. **它叫什么？**（`libraryName`）——拿到名字才能打印、才能作为克隆出来的目录名。
2. **去哪里拉它？**（`url`）——这是 git 远程仓库地址，检出时 `git clone` 的目标。
3. **它该放在本地哪个位置？**（`relativePath`）——相对于「正在解析依赖的那个库」的路径，决定了克隆产物落在目录树的哪一层。
4. **最低要求哪个版本？**（`minVersion`）——低于这个版本的依赖视为不满足要求（u3-l3 的兼容性检查就靠它）。

这四个维度缺一不可，也几乎没有冗余：少了 `url` 没法拉取，少了 `relativePath` 不知道放哪，少了 `minVersion` 没法校验版本，少了 `libraryName` 连打印都不完整。所以 `Dependency` 用这四个字段来「完整刻画一条依赖」。

#### 4.1.2 核心流程

构造一条 `Dependency` 的过程非常直白：四个参数原样赋值给同名的实例属性，唯独 `minVersion` 做了一次「包装」（下一模块细讲）。

```text
Dependency(libraryName, url, relativePath, minVersion)
        │
        ▼  执行 __init__
        │
        ├─ self.libraryName  = libraryName        （原样）
        ├─ self.url          = url                （原样）
        ├─ self.relativePath = relativePath       （原样）
        └─ self.minVersion   = VersionNr(minVersion)  ← 字符串被包成 VersionNr 对象
```

四个字段在对象身上全部是**公开属性**（没有下划线前缀），意味着本包其它模块都直接 `dep.libraryName`、`dep.url` 这样读它们——没有 getter。这是个小工程里很常见、也很务实的写法。

#### 4.1.3 源码精读

[Dependency.py:9-26](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Dependency.py#L9-L26)：整个 `Dependency` 类的定义与构造函数，四个字段在这里被一一赋值。

```python
class Dependency:
    """
    This class describes a dependency
    """

    def __init__(self, libraryName : str, url : str, relativePath : str, minVersion : str):
        self.libraryName = libraryName
        self.url = url
        self.relativePath = relativePath
        self.minVersion = VersionNr(minVersion)
```

- 第 14 行：构造函数签名，四个参数都带了 `str` 类型注解——注意 **`minVersion` 传入时是字符串**（比如 `"1.2.3"`），但存下来的并不是字符串（见第 26 行）。
- 第 23–25 行：前三个字段 `libraryName / url / relativePath` **原样**赋值，没有任何转换。
- 第 26 行：唯一做了「加工」的一行，`minVersion` 被包成 `VersionNr` 对象后再赋值——这是下一个模块的主题。

为了证明这四个字段真的「够用」，可以去消费侧看一眼：[Actions.py:66-72](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L66-L72) 的 `ListDependencies` 恰好用到了其中的 `libraryName / url / minVersion` 三个（`relativePath` 在 `-list` 里用不到，留给检查/检出动作）。

```python
def ListDependencies(deps : List[Dependency]):
    for dep in deps:
        print("{} - {} - {}".format(dep.libraryName, dep.url, dep.minVersion))
```

再看 `relativePath` 的两个消费点：[Actions.py:85](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L85) 在 `CheckDependency` 里用它判断依赖目录是否存在，[Actions.py:108](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L108) 在 `Checkout` 里用它判断是否需要克隆。四个字段各有去处，没有一个是「摆设」。

#### 4.1.4 代码实践

**实践目标**：亲手构造一个 `Dependency`，把四个字段逐一打印出来，确认它们就是你传入的值（`minVersion` 除外，下一模块再验证它的类型）。

**操作步骤**：

1. 确保已按 u1-l2 安装好本包。
2. 新建 `probe_dependency.py`（示例代码，非项目原有文件）：

   ```python
   import PsiFpgaLibDependencies as PFD

   dep = PFD.Dependency("psi_common",
                        "https://git.psi.ch/GFA/PsiCommon.git",
                        "../common/psi_common",
                        "1.2.3")

   print("libraryName :", dep.libraryName)
   print("url         :", dep.url)
   print("relativePath:", dep.relativePath)
   print("minVersion  :", dep.minVersion)
   ```

3. 运行：`python3 probe_dependency.py`。

**需要观察的现象**：前三行打印的，正是构造时传入的字符串原值；第四行 `minVersion` 打印出来是 `1.2.3`——看起来「像是」字符串，但它的真实身份其实是 `VersionNr` 对象（下一模块会证明）。

**预期结果**：

```text
libraryName : psi_common
url         : https://git.psi.ch/GFA/PsiCommon.git
relativePath: ../common/psi_common
minVersion  : 1.2.3
```

> 第四行之所以显示成 `1.2.3`，是因为 `print` 会调用对象的 `__str__`，而 `VersionNr.__str__` 恰好把版本号渲染回 `major.minor.bugfix` 的字符串形态。详见 [VersionNr.py:39-40](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/VersionNr.py#L39-L40)。

#### 4.1.5 小练习与答案

**练习 1**：`Dependency` 的四个字段里，`-list` 动作用到了哪几个？为什么 `relativePath` 不在其中？

**答案**：用到了 `libraryName / url / minVersion` 三个（见 [Actions.py:72](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L72)）。因为 `-list` 只是「把依赖清单打印出来给人看」，不需要触碰本地文件系统；而 `relativePath` 描述的是「依赖在本地该放哪」，只有在检查/检出（要实际操作磁盘）时才用得上。

**练习 2**：构造函数签名上 `minVersion : str`，这是否意味着 `dep.minVersion` 的运行时类型就是 `str`？

**答案**：不是。类型注解只描述「调用方应该传进来什么」，而 `__init__` 内部第 26 行把它包装成了 `VersionNr`，所以 `dep.minVersion` 在对象身上其实是 `VersionNr` 实例。注解和实际存储类型在这里**不一致**——这正是下一个模块要讲的关键点。

---

### 4.2 `minVersion` 转 `VersionNr`

#### 4.2.1 概念说明

为什么 `minVersion` 不直接存字符串，而要包成 `VersionNr`？因为**字符串比较和语义化版本比较不是一回事**。

以 `minVersion` 为例，本包需要回答「已检出的版本 `1.10.0` 是否满足最低要求 `1.2.0`？」。如果直接用字符串比较，`"1.10.0" < "1.2.0"` 会得到 `True`（因为字符 `'1'` < `'2'`），从而**错误地**判定版本过低。正确的语义比较应当是「按 major、minor、bugfix 三段分别比整数」——这正是 `VersionNr` 提供的能力（它的 `__eq__` / `__gt__` 实现详见 u2-l2，本讲只关注「包装」这一动作）。

所以构造函数第 26 行 `self.minVersion = VersionNr(minVersion)` 的目的就是：**把一个「给人看的字符串」转成一个「能正确比较的版本号对象」**，让后续的兼容性检查（`CheckCompatibility`，u3-l3）可以直接写 `versionFound < dep.minVersion` 这样的自然表达式。

#### 4.2.2 核心流程

「字符串 → `VersionNr`」的过程发生在构造的一瞬间，并且**不容忍非法输入**：

```text
VersionNr("1.2.3")
        │
        ▼  split(".")  →  ["1", "2", "3"]
        │
        ▼  段数 < 3 ?
        │
        ├─ 是 → raise Exception("Got illegal version number: 1.2.3")
        └─ 否 → self.major=1, self.minor=2, self.bugfix=3
```

也就是说，`Dependency` 的构造函数「顺便」承担了一次版本号校验：传入 `"1.2"` 或 `"abc"` 这种非法版本号，会在构造时就地抛异常，而不是等到后面比较时才暴雷。这是一种**fail-fast（早失败）**的设计——越早发现错误，越容易定位。

#### 4.2.3 源码精读

[Dependency.py:26](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Dependency.py#L26)：构造函数里唯一的「加工」——把字符串 `minVersion` 包成 `VersionNr` 对象。

```python
self.minVersion = VersionNr(minVersion)
```

这一行依赖 [Dependency.py:7](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Dependency.py#L7) 顶部的 `from .VersionNr import VersionNr`（相对导入，所以本文件必须作为包成员被导入）。

`VersionNr` 的构造逻辑在 [VersionNr.py:8-14](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/VersionNr.py#L8-L14)：

```python
def __init__(self, version : str):
    parts = version.strip().split(".")
    if len(parts) < 3:
        raise Exception("Got illegal version number: {}".format(version))
    self.major = int(parts[0])
    self.minor = int(parts[1])
    self.bugfix = int(parts[2])
```

- 第 9 行：先 `strip()` 去掉首尾空白，再按 `.` 切成几段。
- 第 10–11 行：如果切出来**少于 3 段**（例如 `"1.2"` 切成 2 段），立即抛异常。注意条件是 `< 3`，所以多于 3 段（如 `"1.2.3.4"`）并不会在这里被拦——它只会取前三段，第 4 段被忽略。
- 第 12–14 行：把前三段分别 `int()` 成整数，存为 `major / minor / bugfix`。若某段不是数字（如 `"1.x.3"`），`int()` 会抛 `ValueError`。

包装之后，`dep.minVersion` 就是一个 `VersionNr` 对象。它的好处在消费侧立刻体现：[Actions.py:52-56](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L52-L56) 的 `CheckCompatibility` 直接拿它做语义比较——`dep.minVersion.major` 读 major 段、`versionFound < dep.minVersion` 做整体比较。

```python
if versionFound.major > dep.minVersion.major:
    print("WARNING: Major mismatch ...")
if versionFound < dep.minVersion:
    print("ERROR: Version lower than required ...")
```

如果 `minVersion` 还是个字符串，这两行根本写不出来——字符串既没有 `.major` 属性，也做不了正确的版本比较。

#### 4.2.4 代码实践

**实践目标**：证明 `dep.minVersion` 是 `VersionNr` 对象而非字符串，并验证非法版本号会在构造时 fail-fast。

**操作步骤**：

1. 新建 `probe_version_wrap.py`（示例代码，非项目原有文件）：

   ```python
   import PsiFpgaLibDependencies as PFD

   dep = PFD.Dependency("lib", "https://example.com/lib.git", "../lib", "1.2.3")

   # ① 看它的真实类型
   print("type(dep.minVersion) ->", type(dep.minVersion))

   # ② 它能像 VersionNr 一样读出 major 段（字符串可没有 .major）
   print("dep.minVersion.major  ->", dep.minVersion.major)

   # ③ 逆操作：VersionNr.__str__ 把它渲染回字符串
   print("str(dep.minVersion)   ->", str(dep.minVersion))
   ```

2. 运行：`python3 probe_version_wrap.py`。
3. 再做一个**非法输入**实验，新建 `probe_illegal_version.py`（示例代码）：

   ```python
   import PsiFpgaLibDependencies as PFD

   # 只有 2 段，缺少 bugfix —— 期望构造时立即抛异常
   PFD.Dependency("lib", "https://example.com/lib.git", "../lib", "1.2")
   ```

4. 运行：`python3 probe_illegal_version.py`。

**需要观察的现象**：

- 第 2 步：`type(dep.minVersion)` 应显示 `VersionNr` 类型，而不是 `str`；`dep.minVersion.major` 能直接读出整数 `1`。
- 第 4 步：程序应在构造 `Dependency` 的那一行就抛出异常，异常信息形如 `Got illegal version number: 1.2`。

**预期结果**：

```text
# 第 2 步
type(dep.minVersion) -> <class 'PsiFpgaLibDependencies.VersionNr.VersionNr'>
dep.minVersion.major  -> 1
str(dep.minVersion)   -> 1.2.3
```

第 4 步抛出 `Exception: Got illegal version number: 1.2`。这证明版本校验是**在 `Dependency` 构造的那一刻**借 `VersionNr` 完成的。

#### 4.2.5 小练习与答案

**练习 1**：为什么把 `minVersion` 存成 `VersionNr` 而不是直接存字符串？用 `"1.10.0"` 和 `"1.2.0"` 举一个反例。

**答案**：字符串比较是逐字符的，`"1.10.0" < "1.2.0"` 为真（因为第二个字符 `'.'` 之后 `'1'` < `'2'`），会被错误判定为「版本过低」。而 `VersionNr` 把版本号拆成 major/minor/bugfix 三段整数来比，`1.10.0` 的 minor 段是 `10 > 2`，正确得到「更高」。`CheckCompatibility`（[Actions.py:55](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L55)）正是依赖这一点。

**练习 2**：传入 `minVersion="1.2.3.4"`（四段）会发生什么？

**答案**：不会抛异常。[VersionNr.py:10](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/VersionNr.py#L10) 的校验条件是 `len(parts) < 3`，四段满足「≥3」，于是只取前三段 `1/2/3`，第 4 段被默默丢弃，`dep.minVersion` 等同于 `"1.2.3"`。这是一个值得注意的「宽松」行为，完整的版本比较逻辑在 u2-l2。

---

### 4.3 `GetParentDir`：依赖该放在哪个父目录

#### 4.3.1 概念说明

`Dependency` 还提供了一个实例方法 `GetParentDir()`。它的作用从名字就能猜到：**返回这条依赖「所在的父目录」**。

为什么需要它？想象检出一條依赖 `dep` 时，要执行 `git clone <url> <libraryName>`，这条命令会在「当前目录」下新建一个名为 `libraryName` 的子目录。所以为了让依赖落到正确的位置，我们必须先 `cd` 进它的**父目录**，再 clone。`GetParentDir()` 就是用来给出这个父目录的——它直接复用 `os.path.dirname(self.relativePath)`，即「砍掉 `relativePath` 的最后一段，留下前面的目录部分」。

#### 4.3.2 核心流程

`GetParentDir` 的实现只有一行，但它的行为随 `relativePath` 的「深度」不同而变化。下面列出几种典型情形：

```text
relativePath              os.path.dirname(.)        含义
─────────────────────────────────────────────────────────────────────
"../common/libA"        → "../common"              标准用法：去掉最后一段库名
"../../vendor/libB"     → "../../vendor"           更深的相对路径，前缀保留
"libX"                  → ""                       没有斜杠 → 父目录是空串（当前目录）
"./libX"                → "."                      显式当前目录
```

这里的关键直觉是：`os.path.dirname` 只关心「路径里有没有分隔符 `/`」，**不关心** `..`。所以无论 `relativePath` 以多少个 `..` 开头，`GetParentDir` 都会原样保留这一串 `..`，只砍掉最后一段。

那为什么 `relativePath` 经常以一长串 `..` 开头？因为 `relativePath` 是「相对于正在被解析的那个库（`thisRepo`）」的路径，而这个库往往深埋在目录树里，需要先爬回根（ROOT）再下到目标依赖。`Parse` 用 `levelsToRoot` 数出要爬几层，拼出 `pathPrefix`（如 `"../../.."`），再接到依赖的真实路径前面——这正是 u2-l5 的主题，本讲只需知道「`relativePath` 里的 `..` 是 `Parse` 算出来的相对前缀」即可。

#### 4.3.3 源码精读

[Dependency.py:28-33](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Dependency.py#L28-L33)：`GetParentDir` 的全部实现，直接转发给 `os.path.dirname`。

```python
def GetParentDir(self) -> str:
    """
    Get parent directory
    :return: Parent directory of this dependency relative to the library the dependencies are resolved for
    """
    return os.path.dirname(self.relativePath)
```

`os` 来自 [Dependency.py:6](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Dependency.py#L6) 顶部的 `import os`。docstring 里特别强调返回值是「relative to the library the dependencies are resolved for」——即相对于「被解析依赖的那个库」，不是绝对路径。

去消费侧看它怎么被用：[Actions.py:107](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L107) 的 `Checkout` 用 `os.path.abspath` 把 `GetParentDir()` 的相对结果转成绝对路径，再据此建目录、`cd` 进去、最后 clone。

```python
parent = os.path.abspath(dep.GetParentDir())
...
if not os.path.exists(parent):
    os.makedirs(parent, exist_ok=True)
os.chdir(parent)
# 随后 git clone <url> <libraryName>，克隆产物正好落在 parent 下、名为 libraryName
```

把这几行和 `GetParentDir` 连起来读，就能完整解释「一条依赖是如何被放到正确目录的」：`relativePath` 描述目标位置 → `GetParentDir` 砍掉最后一段得到父目录 → `os.path.abspath` 转绝对路径 → `makedirs` 保证父目录存在 → `chdir` 进入 → `git clone` 把库克隆进父目录。

#### 4.3.4 代码实践

**实践目标**：构造三个不同 `relativePath` 深度的 `Dependency`，打印各自的 `GetParentDir()`，验证 `os.path.dirname` 的「砍最后一段」行为。

**操作步骤**：

1. 新建 `probe_parent_dir.py`（示例代码，非项目原有文件）：

   ```python
   import PsiFpgaLibDependencies as PFD

   deps = [
       # 浅：只有一级相对
       PFD.Dependency("libA", "https://git.psi.ch/GFA/libA.git", "../common/libA", "1.0.0"),
       # 中：两级相对
       PFD.Dependency("libB", "https://git.psi.ch/GFA/libB.git", "../../vendor/libB", "2.3.1"),
       # 深：三级相对
       PFD.Dependency("libC", "https://git.psi.ch/GFA/libC.git", "../../../external/deps/libC", "0.9.5"),
   ]

   for d in deps:
       print("name        :", d.libraryName)
       print("relativePath:", d.relativePath)
       print("GetParentDir:", d.GetParentDir())
       print("-" * 30)
   ```

2. 运行：`python3 probe_parent_dir.py`。

**需要观察的现象**：每一组的 `GetParentDir()` 都恰好是「`relativePath` 去掉最后一段 `/xxx`」，开头的 `..` 一律被原样保留。

**预期结果**：

```text
name        : libA
relativePath: ../common/libA
GetParentDir: ../common
------------------------------
name        : libB
relativePath: ../../vendor/libB
GetParentDir: ../../vendor
------------------------------
name        : libC
relativePath: ../../../external/deps/libC
GetParentDir: ../../../external/deps
------------------------------
```

> 拓展观察（可选）：再构造一个 `PFD.Dependency("libX", "...", "libX", "1.0.0")`（`relativePath` 不含斜杠），它的 `GetParentDir()` 会返回**空字符串** `""`，而不是 `"."`。这说明 `os.path.dirname` 对「没有目录分隔符」的输入返回空串——在 `Checkout` 里 `os.path.abspath("")` 会解析成当前工作目录，因此这种依赖会被克隆到「当前目录」下。

#### 4.3.5 小练习与答案

**练习 1**：`dep.GetParentDir()` 返回的是绝对路径还是相对路径？依据是什么？

**答案**：相对路径。因为 [Dependency.py:33](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Dependency.py#L33) 只是 `os.path.dirname(self.relativePath)`，而 `self.relativePath` 本身就是相对路径（以 `..` 开头）。`os.path.dirname` 不会把相对路径变成绝对路径；要拿绝对路径，得像 `Checkout` 那样再套一层 `os.path.abspath(...)`（见 [Actions.py:107](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L107)）。

**练习 2**：在 `Checkout` 里，为什么是「先 `makedirs(parent)`、再 `chdir(parent)`、最后 `git clone <url> <libraryName>`」，而不是直接 `git clone <url> <relativePath>`？

**答案**：因为 `git clone <url> <name>` 只会在**当前目录**下新建名为 `<name>` 的子目录，它不会自动创建多层父目录。所以必须先用 `GetParentDir()` 找到父目录、用 `makedirs` 把可能缺失的中间目录建出来、`chdir` 进去，再让 clone 把库落在里面。`libraryName` 被用作 clone 的目标目录名（[Actions.py:120](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L120)），这也解释了为什么 `libraryName` 是一个独立字段。

**练习 3**：如果一条依赖的 `relativePath` 是 `"../top/lib"`，那么 `GetParentDir()` 返回什么？克隆后这个库会出现在「被解析库」的什么相对位置？

**答案**：`GetParentDir()` 返回 `"../top"`。克隆后该库位于「被解析库」的上一级目录里的 `top/` 子目录下，名为 `lib`，即相对位置 `../top/lib`——正好等于 `relativePath`。这说明 `relativePath` 就是「依赖最终应落脚的完整相对路径」，而 `GetParentDir` 只是它的「父目录部分」。

---

## 5. 综合实践

把本讲三个模块串起来，做一次「手工模拟 `Parse` 的产物」：手动构造三条不同深度的依赖，像 `Actions` 一样把它们打印出来，并预测/验证它们的父目录。

**任务**：编写 `dep_inspect.py`（示例代码，非项目原有文件），完成下面三件事：

1. 手动构造 3 个 `Dependency` 对象，`relativePath` 深度依次为一层、两层、三层（如上面的 `libA / libB / libC`）。
2. 模仿 [Actions.py:71-72](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L71-L72) 的 `ListDependencies`，把每条依赖按 `库名 - URL - 最低版本` 打印一行。
3. 对每条依赖额外打印 `relativePath` 与 `GetParentDir()`，并用断言验证「`GetParentDir()` 等于 `relativePath` 去掉最后一段」。

**参考实现**：

```python
import os
import PsiFpgaLibDependencies as PFD

deps = [
    PFD.Dependency("libA", "https://git.psi.ch/GFA/libA.git", "../common/libA",                "1.0.0"),
    PFD.Dependency("libB", "https://git.psi.ch/GFA/libB.git", "../../vendor/libB",             "2.3.1"),
    PFD.Dependency("libC", "https://git.psi.ch/GFA/libC.git", "../../../external/deps/libC",   "0.9.5"),
]

print("*** Dependencies ***")
for d in deps:
    # ① 模仿 ListDependencies
    print("{} - {} - {}".format(d.libraryName, d.url, d.minVersion))

print("\n*** Parent Dirs ***")
for d in deps:
    parent = d.GetParentDir()
    print("{:6} relativePath={!r:35} parent={!r}".format(d.libraryName, d.relativePath, parent))
    # ② 断言：parent 应当等于把 relativePath 的最后一段去掉
    expected = os.path.dirname(d.relativePath)
    assert parent == expected, "parent mismatch for {}".format(d.libraryName)

print("\nAll assertions passed.")
```

**操作步骤与观察**：

1. 运行：`python3 dep_inspect.py`。
2. 第一段输出应与 u1-l3 里 `ListDependencies` 的格式完全一致（`库名 - URL - 版本`），版本号由 `VersionNr.__str__` 渲染。
3. 第二段应清晰展示「`relativePath` 越深，`GetParentDir` 保留的 `..` 前缀越多，但都只砍掉最后一段」。
4. 最后应打印 `All assertions passed.`，证明你对 `GetParentDir` 行为的预测与源码一致。

**思考题**：这 3 个对象完全是你「手动」构造的，没有一个来自 README。但它们的字段含义、`minVersion` 的类型、`GetParentDir` 的行为，和 `Parse.FromReadme` 真正解析出来的 `Dependency` **完全相同**——因为 [Parse.py:164](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Parse.py#L164) 也是用同一个构造函数造出来的：

```python
dep = Dependency(repo.name, repo.url, pathPrefix + "/" + repo.GetPath(), repo.version)
```

也就是说，本讲搞懂了 `Dependency`，就同时搞懂了「本包所有模块之间传递的那个数据结构」。`Parse` 的工作只是「把 README 文本翻译成这四个参数」（u2-l3～u2-l5），`Actions` 的工作只是「按这四个字段去执行动作」（u3 单元）。

> 待本地验证：如果你的 Python 环境里 `assert` 被全局关闭（`python -O`），第 ② 步的断言不会执行；正常 `python3` 运行不受影响。

---

## 6. 本讲小结

- [`Dependency`](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Dependency.py) 用四个字段完整刻画一条依赖：`libraryName`（名字）、`url`（远程地址）、`relativePath`（相对落点）、`minVersion`（最低版本）——四个字段各有消费点，无一冗余。
- 构造函数 [Dependency.py:14-26](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Dependency.py#L14-L26) 里，前三个字段原样赋值，唯独 `minVersion` 被包成 `VersionNr` 对象（第 26 行）。
- 「包装」的目的是让版本号能做**语义化比较**而非字符串比较；同时 [VersionNr.py:10-11](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/VersionNr.py#L10-L11) 会在段数不足时就地抛异常，实现 fail-fast。
- `dep.minVersion` 的运行时类型是 `VersionNr` 而非 `str`（尽管构造签名写的是 `: str`），它支持 `.major` 属性和 `<` 比较，[CheckCompatibility](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L52-L56) 直接依赖这一点。
- [GetParentDir](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Dependency.py#L28-L33) 就是 `os.path.dirname(self.relativePath)`：砍掉最后一段、保留所有 `..` 前缀，返回相对父目录；`Checkout`（[Actions.py:107](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L107)）用它定位克隆的父目录。
- `Dependency` 是全包的「共同语言」：`Parse` 生产它、`Actions` 消费它，[Parse.py:164](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Parse.py#L164) 是它唯一真正的生产点。

---

## 7. 下一步学习建议

本讲把 `Dependency` 这个数据结构彻底讲透了，但有两个方向是刻意留给后续的：

- **u2-l2 语义版本号 VersionNr**：本讲只用到 `VersionNr` 的「包装」与 `.major`，没有展开它的 `__eq__` / `__gt__` 是怎么逐段比较的。想搞懂 `CheckCompatibility` 里 `versionFound < dep.minVersion` 的真正含义，就必须读这一讲。
- **u2-l3 README 依赖格式与 Parse.FromReadme 入口**：本讲多次提到「`Parse` 把 README 翻译成 `Dependency` 的四个参数」，[Parse.py:164](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Parse.py#L164) 就是翻译的终点。下一讲从 README 文本出发，看看这四个参数究竟是怎么「读」出来的。

如果你更想先看「动作侧」，也可以跳到 u3-l1（列出与检查依赖），那里会大量读取本讲讲的四个字段；但建议按 u2-l2 → u2-l3 的顺序，把数据模型和解析机制先补齐，再进入动作执行会更顺畅。
