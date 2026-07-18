# FileOperations：正则通配符文件操作

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清楚「按文件名匹配文件」这件事，PsiPyUtils 为什么选择**正则表达式**而不是大家更熟悉的 `glob`/`fnmatch`，以及两者在 `.`、`*` 含义上的关键差别。
- 独立阅读 `RemoveWithWildcard`、`FindWithWildcard`、`OpenWithWildcard` 三个函数的源码，并能预测给定 pattern 会命中哪些文件。
- 理解 `OpenWithWildcard` 的「唯一匹配」约束：命中多于一个或零个时分别抛什么异常、为什么这样设计。
- 认识这三个函数「**不递归子目录**」的边界，以及它会对目录条目（含子目录）一视同仁带来的潜在坑。
- 掌握 `AbsPathLinuxStyle` 如何把任意路径统一成「正斜杠绝对路径」，以及它为何对 SDK/Vivado 这类工具重要。

## 2. 前置知识

本讲默认你已经具备 u1-l2 建立的包结构认知（知道 `FileOperations` 是被 `__init__.py` 以**模块**形式重导出的，见 [__init__.py:8](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/__init__.py#L8)），并能用两种写法导入它。

在进入源码前，先用三段话建立直觉：

1. **什么是「通配符」**。在命令行里我们常用 `*.txt` 表示「所有 `.txt` 文件」，`*` 就是通配符。但「通配符」在不同工具里语义并不相同：Shell 的 `glob`、Python 标准库的 `fnmatch` 是一套规则；正则表达式（regex）是另一套更强大也更易踩坑的规则。`FileOperations` 用的是**正则**。

2. **`re.search` 与「包含」语义**。`re.search(pattern, string)` 在 `string` 里**任意位置**寻找第一个匹配，找到就返回匹配对象，找不到返回 `None`。它**不要求**整段字符串都匹配——这等价于「文件名里**包含**某段子串」。这跟很多人以为的「从头到尾完全匹配」不一样。

3. **`os.listdir` 是「浅扫描」**。`os.listdir(dir)` 只返回 `dir` 这一层**直接**的条目（文件和子目录混在一起），不会下钻到子目录里去。本讲的三个函数都建立在这个浅扫描之上，所以它们的「通配」是「**一层目录内**的通配」。

> 术语速查：`re.search`（正则搜索）、`re.match`（仅从开头匹配）、`os.listdir`（列出目录条目）、`os.remove`（删除文件）、`os.path.join`（按当前系统拼接路径）。

## 3. 本讲源码地图

本讲只涉及两个文件，体量都很小：

| 文件 | 作用 | 行数 |
| --- | --- | --- |
| [FileOperations.py](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/FileOperations.py) | 全部实现：4 个函数，无类、无状态 | 约 64 行 |
| [Tests/TestFileOperations.py](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/Tests/TestFileOperations.py) | 用 `unittest` 为 4 个函数各写了「命中一个 / 命中多个 / 命中零个」用例 | 约 83 行 |

`FileOperations.py` 一共定义 4 个顶层函数，**没有类**、**没有可变状态**，是整库里最「纯函数」风格的模块：

- `RemoveWithWildcard(dir, pattern)` —— 删除目录里**名字匹配** pattern 的文件。
- `FindWithWildcard(dir, pattern)` —— 返回目录里**名字匹配** pattern 的文件名列表。
- `OpenWithWildcard(dir, pattern, mode="r")` —— 打开目录里**唯一**匹配 pattern 的文件。
- `AbsPathLinuxStyle(path)` —— 把任意路径转成正斜杠的绝对路径。

前三个共享同一套「`os.listdir` + `re.search`」的匹配骨架，所以本讲会先抽出这个公共骨架单独讲，再分别展开三个函数。

## 4. 核心概念与源码讲解

### 4.1 re.search 文件名匹配：为什么用正则而不是 glob

#### 4.1.1 概念说明

「我想处理名字满足某种规律的文件」是日常脚本里极常见的需求。Python 标准库提供了两条路：

- **glob / fnmatch**：`*` 代表「任意个字符」，`?` 代表「一个字符」，`.` 是普通字符。简单、直觉，但表达力弱。
- **正则（`re`）**：`*` 是量词（「前面的东西重复若干次」），`.` 是「任意一个字符」，`.*` 才是「任意一段字符」。表达力强，但每个元字符都有含义，容易写错。

PsiPyUtils 选了**正则**这条路，从函数文档里就能看出来——它明确写「python regex pattern (`.*` means any number of any characters)」，而不是写 glob 的 `*`。

为什么选正则？因为这套工具的目标用户是写代码的工程师（u1-l1 提到的通用工具库定位），他们更需要「按复杂规则挑文件」（如 `^data_2024_.*\.csv$`）而不是简单的 `*`，正则一步到位。

#### 4.1.2 核心流程

理解这个模块的关键，是先看清「给定一个 pattern 和一个文件名，怎么判定匹配」。流程是：

1. 拿到目录里某个条目的名字 `file`（字符串）。
2. 调用 `re.search(pattern, file)`。
3. 返回值非 `None` ⇒ **匹配**；为 `None` ⇒ **不匹配**。

这里有两个最容易踩的坑，务必记住：

- **`re.search` 是「包含」语义，不是「完全匹配」**。`re.search("Bunny", "FunnyBunny.txt")` 会命中，因为 "Bunny" 这段子串确实出现在里面。所以 docstring 里写的 `.*Bunny.*` 其实**前后两个 `.*` 都是多余的**——用 `search` 的话光写 `"Bunny"` 也一样命中。要「以…开头」得写 `^Funny`，要「以…结尾」得写 `\.txt$`。
- **`.` 匹配任意一个字符，不是普通点**。写 `"data.txt"` 会**同时**命中 `data.txt`、`dataXtxt`、`data9txt`——因为那个 `.` 把 `X`、`9` 都吃掉了。要匹配真实的点号，必须转义写成 `data\.txt`。这正是「正则 vs glob」最现实的差别：glob 里的 `.` 是字面量，正则里不是。

| 你想要 | glob 写法 | 本模块正则写法 | 备注 |
| --- | --- | --- | --- |
| 名字里含 Bunny | `*Bunny*` | `.*Bunny.*` 或直接 `Bunny` | search 已是包含语义 |
| 以 Funny 开头 | `Funny*` | `^Funny.*` | 不加 `^` 也会命中 `NotFunny…` |
| 所有 .csv 文件 | `*.csv` | `.*\.csv$` | 注意 `\.` 和 `$` |
| 名字形如 data_数字 | （不易表达） | `data_\d+` | 正则的表达力优势 |

#### 4.1.3 源码精读

匹配动作本身只在一行里发生，三处函数都用的是同一个 `re.search` 调用。以 `FindWithWildcard` 为例：

[FileOperations.py:33-37](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/FileOperations.py#L33-L37) —— 遍历目录条目，用 `re.search(pattern, file)` 判定，命中就收集进列表。

```python
l = []
for file in os.listdir(dir):
    if re.search(pattern, file):
        l.append(file)
return l
```

注意第 35 行的 `if re.search(...)`：`re.search` 返回的是**匹配对象**（真值）或 `None`（假值），直接放进 `if` 正好做布尔判断——这是 Python 里很地道的写法。`RemoveWithWildcard` 的 [FileOperations.py:19-21](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/FileOperations.py#L19-L21) 用的是同一行判定，只是命中后改调 `os.remove`。

#### 4.1.4 代码实践

**实践目标**：亲手验证「`re.search` 是包含语义」和「`.` 匹配任意字符」这两点，而不是只看文档。

**操作步骤**：

1. 在终端进入任意目录，启动 `python3`。
2. 依次执行下面这段**示例代码**（不是项目原有代码，仅用于体会正则语义）：

   ```python
   import re
   names = ["FunnyBunny.txt", "FunnyBird.txt", "dataXtxt", "NotFunny.log"]
   for p in ["Bunny", "Funny.*", "data.txt", r"data\.txt"]:
       print(p, "->", [n for n in names if re.search(p, n)])
   ```

**需要观察的现象**：

- `"Bunny"` 即便没有 `.*` 也命中 `FunnyBunny.txt`（包含语义）。
- `"Funny.*"` 命中 `FunnyBunny.txt`、`FunnyBird.txt`，**也命中** `NotFunny.log`（没加 `^`，Funny 在中间也算）。
- `"data.txt"` 命中 `dataXtxt`（`.` 吃掉了 X）。
- `r"data\.txt"`（转义点号）则不再命中 `dataXtxt`。

**预期结果**：你会清楚看到「不加锚点 = 包含」「不转义点号 = 任意字符」。如果你所在环境无法运行，标注「待本地验证」，但结论是确定的。

#### 4.1.5 小练习与答案

**练习 1**：写一个 pattern，让 `re.search(pattern, "report_2024_03.csv")` 命中，但 `re.search(pattern, "report_2024_03.txt")` 不命中。

答案：`r"\.csv$"`（或 `r"report_.*\.csv$"`）。关键是结尾的 `$` 锚定扩展名。

**练习 2**：为什么不写 `^` 时，pattern `"Funny.*"` 会误命中 `NotFunny.txt`？

答案：因为 `re.search` 是「在任意位置找匹配」，`NotFunny.txt` 中从第 2 个字符起就出现了 `Funny`，随后 `.*` 吃掉 `.txt`，于是命中。要让「以 Funny 开头」生效，必须写 `^Funny.*`。

---

### 4.2 非递归扫描与 FindWithWildcard / RemoveWithWildcard

#### 4.2.1 概念说明

`FindWithWildcard` 和 `RemoveWithWildcard` 是两个最直接的函数：一个**只查**、一个**查到就删**。它们共享完全相同的「扫描 + 匹配」骨架，差别仅在命中后做什么——这是典型的「同结构、不同副作用」。

两者都建立在一个关键前提上：**`os.listdir(dir)` 是浅扫描**。它只列出 `dir` 这一层的全部条目，**不进子目录**。函数文档里也专门强调了一句「Note that the function does not recurse into subdirectories」（见 [FileOperations.py:14-15](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/FileOperations.py#L14-L15)）。所以本讲的「通配」是「**单层目录内**的通配」。

另一个容易被忽略的细节：`os.listdir` 返回的条目里**文件和子目录混在一起**，而且不带路径前缀，只是裸名字。这意味着如果某个**子目录的名字**恰好匹配 pattern，`RemoveWithWildcard` 会去 `os.remove` 一个目录——而 `os.remove` 不能删目录（会抛 `IsADirectoryError`/`PermissionError`）。这是「非递归 + 文件目录混判」埋下的一个小坑，使用时要心里有数。

#### 4.2.2 核心流程

两个函数的控制流几乎一样，可以合并成下面这张伪代码：

```
对 dir 目录做一次浅扫描（os.listdir），得到条目列表 entries
for file in entries:                      # file 只是裸文件名，不带路径
    if re.search(pattern, file):          # 用正则在名字里找子串
        full = os.path.join(dir, file)    # 拼成可操作的完整路径
        【Find】  把 file 追加进结果列表
        【Remove】 os.remove(full)        # 直接删，删前不询问、不计数
【Find】  return 结果列表（可能为空）
【Remove】无返回值（隐式 None）
```

要点：

- **匹配的是「裸文件名」**，不含目录部分，所以 pattern 里不要写路径分隔符。
- **`os.path.join` 按当前系统拼路径**：Linux 下用 `/`，Windows 下用 `\`，跨平台行为正确（这也是后面 `AbsPathLinuxStyle` 存在的原因之一——拼出来的路径可能仍是 Windows 风格）。
- `RemoveWithWildcard` 是**幂等但不可逆**的：命中的文件直接删掉，没有回收站、没有确认；不命中任何文件时它什么都不做，也不报错。

#### 4.2.3 源码精读

先看「只查」的 `FindWithWildcard`：

[FileOperations.py:23-37](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/FileOperations.py#L23-L37) —— 浅扫描 + 正则匹配 + 收集，返回文件名列表。返回类型标注为 `List[str]`，是整库里少见的带类型注解的函数。

```python
def FindWithWildcard(dir : str, pattern : str) -> List[str]:
    ...
    l = []
    for file in os.listdir(dir):
        if re.search(pattern, file):
            l.append(file)
    return l
```

注意它 `append` 的是**裸文件名 `file`**，不是 `os.path.join` 拼出的完整路径——所以返回值是 `["FunnyBunny.txt", ...]`，调用方若要进一步操作，得自己再拼目录。

再看「查到就删」的 `RemoveWithWildcard`：

[FileOperations.py:10-21](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/FileOperations.py#L10-L21) —— 判定逻辑与上一函数完全相同，命中后改用 `os.remove(os.path.join(dir, file))` 删除。

```python
def RemoveWithWildcard(dir : str, pattern : str):
    ...
    for file in os.listdir(dir):
        if re.search(pattern, file):
            os.remove(os.path.join(dir, file))
```

对比这两个函数：第 19-20 行与第 34-35 行**逐字相同**，只有「命中后做什么」那一行不同（`os.remove(...)` vs `l.append(file)`）。这是一处明显的代码重复——4.2.5 的小练习会让你想想怎么重构，也呼应 u5-l3「批判性读源码」的主题。

测试里对这两个函数都覆盖了「命中一个 / 多个 / 零个」三种情形，值得对照看。例如 `testRemoveWithWildcard_Multiple` 用 pattern `"Funny.*"` 一次删掉两个文件：

[Tests/TestFileOperations.py:42-45](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/Tests/TestFileOperations.py#L42-L45) —— 用 `"Funny.*"` 删除后，`TestDir` 应为空。

```python
def testRemoveWithWildcard_Multiple(self):
    RemoveWithWildcard("TestDir", "Funny.*")
    files = os.listdir("TestDir")
    self.assertEqual(0, len(files))
```

而 `testFindWithWildcard_Multiple` 因为 `os.listdir` 的返回顺序不保证，巧妙地用 `set` 比较来避开顺序问题（见 [Tests/TestFileOperations.py:57-59](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/Tests/TestFileOperations.py#L57-L59)）——这是一个很实用的测试写法技巧。

#### 4.2.4 代码实践

**实践目标**：用一个最小目录，亲手体验「返回子集」「删除一组」两个函数，并验证它们不进子目录。

**操作步骤**（**示例代码**，基于本模块 API 编写）：

1. 新建一个工作目录，进入它，启动 `python3`。
2. 准备目录结构（含 3 个文件 + 1 个子目录，子目录里也放一个会匹配 pattern 的文件）：

   ```python
   import os
   os.makedirs("demo/sub", exist_ok=True)
   for n in ["data_1.txt", "data_2.txt", "readme.md"]:
       open(n, "w").write("x")
   open("sub/data_3.txt", "w").write("x")   # 子目录里也放一个 data_*.txt
   ```
3. 导入并调用（用测试里那种裸导入方式，需先把仓库根目录加进 `sys.path`）：

   ```python
   import sys; sys.path.append("<仓库根目录路径>")
   from FileOperations import FindWithWildcard, RemoveWithWildcard

   print(FindWithWildcard("demo", r"data_.*\.txt"))  # 期望: ['data_1.txt', 'data_2.txt']
   ```

**需要观察的现象**：

- `FindWithWildcard("demo", r"data_.*\.txt")` 只返回 `demo` 本层的 `data_1.txt`、`data_2.txt`，**不包含** `sub/data_3.txt`——印证「非递归」。
- 接着执行 `RemoveWithWildcard("demo", r"data_.*\.txt")`，再 `os.listdir("demo")`，应只剩 `readme.md` 和 `sub`。
- 子目录 `sub` 本身及其内部文件都不受影响。

**预期结果**：两个函数都只在 `demo` 一层生效；删除是不可逆的，所以请放在临时目录里做。

#### 4.2.5 小练习与答案

**练习 1**：`FindWithWildcard` 返回的是裸文件名还是完整路径？如果调用方想接着 `open` 这个文件，需要做什么？

答案：返回**裸文件名**（如 `"data_1.txt"`）。要打开它，需要用 `os.path.join(dir, filename)` 自己拼上目录，再传给 `open`——这正是 `OpenWithWildcard` 替你封装的事（见 4.3）。

**练习 2**：`RemoveWithWildcard` 和 `FindWithWildcard` 的匹配循环几乎完全重复。如果要消除重复，你会怎么重构？（开放题）

答案（参考思路）：抽一个内部生成器 `_iter_matching(dir, pattern)`，`yield` 命中的裸文件名；`FindWithWildcard` 用 `list(_iter_matching(...))`，`RemoveWithWildcard` 在循环里对每个 yield 出的名字调 `os.remove`。注意这只是「读源码时想到的改进」，**不要真的去改源码**（u5-l3 会专门讨论这类取舍）。

---

### 4.3 OpenWithWildcard：唯一匹配约束与异常分支

#### 4.3.1 概念说明

`OpenWithWildcard` 是三个通配函数里最有「态度」的一个：它不是返回一堆，而是**要求 pattern 恰好命中一个文件**，然后把它打开并返回文件对象。命中多于一个说明 pattern 太宽泛（有歧义），命中零个说明没找到——这两种情况它都**主动抛异常**，而不是默默返回 `None` 或随便挑一个。

这种「唯一匹配」设计在生成脚本里很有用：很多场景下你**期望**某个目录里只有一个 `*.bit`（FPGA 比特流）或只有一个 `report_*.csv`，多一个或少一个都意味着上游出了问题，越早 fail 越好。这符合「快速失败（fail fast）」的工程原则。

另一个值得注意的点是：`OpenWithWildcard` **复用**了 4.2 的 `FindWithWildcard` 来做查找，而不是再写一遍循环——这是本模块内部唯一一处「函数调用函数」的复用关系，和 `RemoveWithWildcard` 的「复制粘贴」形成了有意思的对比。

#### 4.3.2 核心流程

```
fileNames = FindWithWildcard(dir, pattern)     # 复用 4.2 的查找，拿到命中列表
if len(fileNames) > 1:                          # 多于一个 → 歧义
    raise Exception("Pattern matches more than one file: " + 列出文件名)
if len(fileNames) < 1:                          # 零个 → 没找到
    raise Exception("Pattern does not match any file")
return open(os.path.join(dir, fileNames[0]), mode=mode)   # 恰好一个 → 打开返回
```

要点：

- **异常类型是通用 `Exception`**，不是自定义异常类（对比 u3-l3 的 `TextReplace` 会定义专门的 `TagsNotFoundError`）。所以调用方若要区分这两种错误，只能靠**异常消息文本**去判断，这在 u5-l3 里会被当作「接口设计可改进点」再次提到。
- **歧义异常的消息会列出所有命中文件名**（用换行 + 缩进拼接），方便排查到底是哪些文件撞车了。
- 返回值是 `open(...)` 的文件对象，因此天然支持 `with OpenWithWildcard(...) as f:` 的写法——测试里正是这么用的。
- 默认 `mode="r"`（只读），但可以传 `"a"`（追加）、`"w"`（覆盖写）等任意 `open` 合法模式。

#### 4.3.3 源码精读

[FileOperations.py:39-54](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/FileOperations.py#L39-L54) —— 这是整个模块最值得逐行读的函数，三段逻辑层次分明。

```python
def OpenWithWildcard(dir : str, pattern : str, mode : str = "r"):
    ...
    fileName = FindWithWildcard(dir, pattern)     # 复用查找
    if len(fileName) > 1:
        raise Exception("\n    ".join(
            ["Pattern matches more than one file: "] + fileName))
    if len(fileName) < 1:
        raise Exception("Pattern does not match any file")
    return open(os.path.join(dir, fileName[0]), mode=mode)
```

几个精读要点：

- 第 49 行直接复用 `FindWithWildcard`，体现了「查找是基础能力，打开是查找之上的策略」的分层思路。
- 第 50-51 行：`["Pattern matches more than one file: "] + fileName` 把提示语和命中文件名拼成一个列表，再用 `"\n    ".join(...)` 串起来——每个文件名占一行（带缩进），多文件时报错信息很 readable。
- 第 54 行：`os.path.join(dir, fileName[0])` 之所以必要，正是因为 `FindWithWildcard` 返回的是裸文件名（4.2.5 练习 1 伏笔在此）；拼好后传给 `open`。
- 注意变量名：函数内用的是单数 `fileName`，但它实际指向 `FindWithWildcard` 返回的**列表**，命名为 `fileNames` 会更准确——这类命名小瑕疵在读源码时要能识别（u5-l3 主题）。

测试里对三个分支都有覆盖。歧义分支用 `assertRaises(Exception)` 捕获：

[Tests/TestFileOperations.py:70-72](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/Tests/TestFileOperations.py#L70-L72) —— pattern `"Funny.*"` 同时命中两个文件，应抛异常。

```python
def testOpenWithWildcard_Multiple(self):
    with self.assertRaises(Exception):
        OpenWithWildcard("TestDir", "Funny.*")
```

而追加模式用例展示了「打开 + 写 + 关闭 + 再读」的典型流程：

[Tests/TestFileOperations.py:78-82](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/Tests/TestFileOperations.py#L78-L82) —— 用 `mode="a"` 打开后写 `" Jumps"`，文件内容应变成 `"Bunny Jumps"`。

```python
def testOpenWithWildcard_Append(self):
    with OpenWithWildcard("TestDir", ".*Bunny.*", "a") as f:
        f.write(" Jumps")
    with open("TestDir/FunnyBunny.txt", "r") as f:
        self.assertEqual(f.read(), "Bunny Jumps")
```

这个用例正好对应本讲综合实践里「以 append 模式追加内容」的任务。

#### 4.3.4 代码实践

**实践目标**：构造一个会命中多个文件的场景，亲眼看到异常被抛出；再用 `mode="a"` 追加内容。

**操作步骤**（**示例代码**）：

```python
import os, sys
sys.path.append("<仓库根目录路径>")
from FileOperations import OpenWithWildcard

os.makedirs("demo2", exist_ok=True)
for n in ["data_1.txt", "data_2.txt"]:
    open(os.path.join("demo2", n), "w").write("hello")

# 1) 多文件 → 期望抛异常
try:
    OpenWithWildcard("demo2", r"data_.*\.txt")
except Exception as e:
    print("捕获到异常:", repr(e))

# 2) 精确命中一个 → append 追加
with OpenWithWildcard("demo2", r"data_1\.txt", "a") as f:
    f.write(" world")
print(open(os.path.join("demo2", "data_1.txt")).read())  # 期望: hello world
```

**需要观察的现象**：第 1 步抛出的异常消息里会**列出两个文件名**；第 2 步用更窄的 pattern `data_1\.txt` 命中唯一文件，追加成功。

**预期结果**：`data_1.txt` 最终内容为 `hello world`，且 `data_2.txt` 不受影响。若不确定本地环境，标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`OpenWithWildcard` 在「命中零个」和「命中多个」时分别抛什么异常？调用方想区分二者，能靠异常类型做到吗？

答案：两种情况抛的都是通用 `Exception`，只是**消息文本**不同（`"Pattern does not match any file"` vs `"Pattern matches more than one file: ..."`）。**不能**靠异常类型区分，只能解析消息文本——这是接口设计的可改进点（可改为两个自定义异常类）。

**练习 2**：为什么 `OpenWithWildcard` 的返回值可以放进 `with ... as f`，而 `TempWorkDir`（u2-l1）通常不写 `as`？

答案：`OpenWithWildcard` 返回的是 `open(...)` 的**文件对象**，文件对象本身就是上下文管理器，所以能 `with`。`TempWorkDir` 的 `__enter__` 隐式返回 `None`（u2-l1 讲过），靠的是进程全局副作用（切换工作目录），故不带 `as`。两者范式不同。

---

### 4.4 AbsPathLinuxStyle：跨平台绝对路径

#### 4.4.1 概念说明

第四个函数 `AbsPathLinuxStyle` 和前三个没有调用关系，是个独立的小工具，但解决了 PSI 这类研究所很现实的问题：很多 EDA 工具（Xilinx Vivado、Intel Quartus、各种 FPGA SDK）即便跑在 Windows 上，**配置文件和脚本里也只认 Linux 风格路径**（正斜杠 `/`）。如果你把一个 `C:\proj\data.txt` 原样喂给它们，往往解析失败。

`AbsPathLinuxStyle` 就干一件事：把**任意**输入路径（相对/绝对、Windows/Linux 风格），转成**正斜杠的绝对路径**，让它在这些工具里可用。

#### 4.4.2 核心流程

```
abs_path = os.path.abspath(path)   # 1) 先转成绝对路径（按当前工作目录解析相对路径）
return abs_path.replace("\\", "/") # 2) 再把所有反斜杠换成正斜杠
```

两步各有职责：

- **第一步 `os.path.abspath`**：把相对路径解析成绝对路径。它会在**当前工作目录**下解读 `path`，所以同样的输入在不同 `cwd` 下结果不同——这点和 u2-l1 的 `TempWorkDir` 联动时有意义：在 `TempWorkDir` 内调用，`abspath` 解析出的就是临时目录下的绝对路径。
- **第二步 `replace("\\", "/")`**：在 Windows 上，`abspath` 会返回带 `\` 的路径（如 `C:\proj\data.txt`）；在 Linux 上本来就没有 `\`，`replace` 是 no-op（空操作）。所以这一行是「**按最差情况（Windows）写，对 Linux 无副作用**」的跨平台写法，和 u2-l2 讲过的「按最严平台约束写代码」是同一种思路。

> 注意：此函数只**规范分隔符**，**不改变盘符**。Windows 上的结果会是 `C:/proj/data.txt`（盘符还在，只是斜杠转正）。它不负责把 Windows 路径「翻译」成 Linux 路径——那需要挂载点映射，超出本函数职责。

#### 4.4.3 源码精读

[FileOperations.py:56-64](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/FileOperations.py#L56-L64) —— 整个函数体只有一行返回表达式，是整库最精简的函数之一。

```python
def AbsPathLinuxStyle(path : str) -> str:
    """
    This function returns an absolute path in linux style (forward slashes),
    also if it is executed on Windows.
    ...
    """
    return os.path.abspath(path).replace("\\", "/")
```

值得品味的两点：

- **链式调用**：`abspath(...)` 返回字符串，字符串直接 `.replace(...)`，一行搞定。可读性来自于两个步骤语义都很单一。
- **类型注解齐全**：`path: str` 与 `-> str` 都有，和 `FindWithWildcard` 一样是带注解的函数；但 `RemoveWithWildcard`、`OpenWithWildcard` 就**没有**返回类型注解——同一文件内风格不统一，这是读源码时可以注意到的细节。

（`AbsPathLinuxStyle` 没有对应的 `testAbsPathLinuxStyle` 用例——它属于本模块里**未被测试覆盖**的函数之一，u5-l3 会专门盘点测试缺口。）

#### 4.4.4 代码实践

**实践目标**：观察 `abspath` 依赖当前工作目录、以及 `replace` 把反斜杠转正的效果。

**操作步骤**（**示例代码**）：

```python
import os, sys
sys.path.append("<仓库根目录路径>")
from FileOperations import AbsPathLinuxStyle

print(os.getcwd())                    # 记下当前工作目录
print(AbsPathLinuxStyle("a/b/c.txt")) # 相对路径 → 会以 cwd 为前缀的绝对路径
# 在 Windows 上构造反斜杠输入：
print(AbsPathLinuxStyle(r"..\demo\data.txt"))
```

**需要观察的现象**：

- `AbsPathLinuxStyle("a/b/c.txt")` 的输出会以当前 `os.getcwd()` 开头，结尾是 `/a/b/c.txt`。
- 在 Windows 上，`abspath` 内部可能产出反斜杠，但 `replace` 之后**全部变成正斜杠**；在 Linux 上输入本来就是正斜杠，输出不变。

**预期结果**：无论平台，返回值都是「全正斜杠、绝对」的路径，不含 `\`。若你在 Linux 上手头没有反斜杠输入，可手动构造 `path = "a\\\\b"`（即含字面反斜杠的字符串）来观察 `replace` 效果。无法确定时标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：如果在 `with TempWorkDir("subdir"):` 内部调用 `AbsPathLinuxStyle("file.txt")`，结果会以哪个目录为前缀？

答案：以 `subdir`（已切换后的当前工作目录）为前缀。因为 `TempWorkDir.__enter__` 执行了 `os.chdir`（u2-l1），而 `os.path.abspath` 解析相对路径时用的是**当前** `cwd`，此时已是 `subdir`。

**练习 2**：为什么不直接写 `path.replace("\\", "/")`，而要先 `os.path.abspath`？

答案：因为输入可能是**相对路径**，不先 `abspath` 就无法得到绝对路径；而且只有先 `abspath` 才能保证在 Windows 上把盘符和完整层级都拼出来。先规范成绝对、再统一分隔符，顺序不能反。

---

## 5. 综合实践

把本讲的「正则匹配」「非递归扫描」「唯一匹配约束」「跨平台路径」串起来，完成下面这个小任务。它复刻了测试文件 `TestFileOperations.py` 的结构，但要求你**自己建目录、自己写断言**。

**任务背景**：假设你在写一个生成脚本，工作目录 `work/` 里会产出若干 `report_*.csv` 文件，以及若干 `log_*.txt` 文件。你需要用 `FileOperations` 来管理它们。

**操作步骤**（**示例代码**，请放在一个临时目录里运行）：

```python
import os, sys, shutil
sys.path.append("<仓库根目录路径>")
from FileOperations import (
    FindWithWildcard, RemoveWithWildcard, OpenWithWildcard, AbsPathLinuxStyle
)

# 1) 准备现场：3 个 csv + 2 个 txt
root = os.path.abspath("work")
shutil.rmtree(root, ignore_errors=True)
os.makedirs(root)
for n in ["report_2024.csv", "report_2025.csv", "report_draft.csv"]:
    open(os.path.join(root, n), "w").write("header\n")
for n in ["log_run.txt", "log_err.txt"]:
    open(os.path.join(root, n), "w").write("ok\n")

# 2) 用 FindWithWildcard 列出所有 report_*.csv（预期 3 个）
csvs = FindWithWildcard(root, r"report_.*\.csv")
assert len(csvs) == 3, csvs

# 3) 用 RemoveWithWildcard 删除所有草稿（report_draft.csv）
RemoveWithWildcard(root, r"report_draft\.csv")
assert len(FindWithWildcard(root, r"report_.*\.csv")) == 2

# 4) 用 OpenWithWildcard 以 append 模式给「正式报告」补一行
#    —— 此时 report_2024.csv / report_2025.csv 仍多于一个，预期抛异常
try:
    OpenWithWildcard(root, r"report_.*\.csv", "a")
    raised = False
except Exception as e:
    raised = True
    print("正确地抛了异常：", str(e).splitlines()[0])
assert raised, "应因匹配多个文件而抛异常"

# 5) 把范围收窄到唯一文件，append 一行
with OpenWithWildcard(root, r"report_2024\.csv", "a") as f:
    f.write("row1\n")
assert open(os.path.join(root, "report_2024.csv")).read() == "header\nrow1\n"

# 6) 用 AbsPathLinuxStyle 打印 Linux 风格绝对路径
print(AbsPathLinuxStyle(root))
```

**需要观察的现象与预期结果**：

- 第 2 步返回 3 个 csv 文件名（顺序不定，不要按位置断言）。
- 第 3 步后只剩 2 个 csv。
- 第 4 步**必须抛异常**（pattern 太宽，命中 2 个）——这验证了你对「唯一匹配约束」的理解。
- 第 5 步收窄 pattern 后追加成功，文件内容多出一行。
- 第 6 步打印的路径全部是正斜杠。

**对照源码**：第 4 步抛出的异常，正是 [FileOperations.py:50-51](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/FileOperations.py#L50-L51) 的逻辑；第 5 步的 append 行为，对应 [Tests/TestFileOperations.py:78-82](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/Tests/TestFileOperations.py#L78-L82) 的 `testOpenWithWildcard_Append`。

> 提示：因为 `RemoveWithWildcard` 删除不可逆，请务必在临时目录里做本实践，跑完用 `shutil.rmtree(root)` 清理。

## 6. 本讲小结

- `FileOperations` 用**正则 `re.search`**（而非 glob）匹配文件名；`re.search` 是「**包含**」语义，不锚定首尾，`.` 是「任意字符」而非普通点号——写 pattern 时要转义 `.`、必要时加 `^`/`$`。
- `FindWithWildcard`（只查）与 `RemoveWithWildcard`（查到就删）共享 `os.listdir + re.search` 骨架，且都是**非递归**的浅扫描；它们之间有明显的代码重复。
- `os.listdir` 返回的条目**文件与子目录混在一起且是裸名字**，所以 `RemoveWithWildcard` 若误匹到一个子目录会在 `os.remove` 处失败——这是「非递归 + 混判」的隐性坑。
- `OpenWithWildcard` 要求 pattern **恰好命中一个**文件，多于一个或零个都抛通用 `Exception`（靠消息文本区分），内部复用 `FindWithWildcard` 并返回 `open(...)` 文件对象，故支持 `with as f`。
- `AbsPathLinuxStyle = os.path.abspath(path).replace("\\", "/")`，先转绝对、再统一成正斜杠，为 Windows 上跑 Vivado/SDK 等 Linux 风格路径工具服务；`replace` 对 Linux 是 no-op。
- 本模块风格上有几处「读源码值得注意」的点：类型注解不统一、`OpenWithWildcard` 用通用 `Exception` 而非自定义异常、变量名 `fileName` 实为列表、`AbsPathLinuxStyle` 无测试——这些都会在 u5-l3「批判性读源码」里系统讨论。

## 7. 下一步学习建议

- 下一讲 **u3-l2「跨平台路径处理」** 会把本讲的 `AbsPathLinuxStyle` 与 `EnvVariables.AddToPathVariable` 放在一起，集中讲「路径分隔符 / 斜杠方向 / `sys.platform` 分支」的跨平台策略，建议紧接着读。
- 如果你对「正则替换」场景感兴趣，可以跳读 **u3-l3「TextReplace.TaggedReplace」**，那里会用到 `re.DOTALL` 和非贪婪 `.*?`，是正则在本库的另一个用武之地。
- 想把「唯一匹配」「跨平台路径」放进更大图景的读者，建议先完成本讲综合实践，再带着「这些函数没被测试覆盖的部分」去读 **u5-l3「测试组织与批判性读源码」**——本讲多次埋下的命名/异常/测试缺口伏笔，会在那一讲系统回收。
- 继续阅读建议：把 [FileOperations.py](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/FileOperations.py) 与 [Tests/TestFileOperations.py](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/Tests/TestFileOperations.py) 对照再读一遍，尝试为 `AbsPathLinuxStyle` 补一个 `unittest` 用例（见 u5-l3 的补测策略）。
