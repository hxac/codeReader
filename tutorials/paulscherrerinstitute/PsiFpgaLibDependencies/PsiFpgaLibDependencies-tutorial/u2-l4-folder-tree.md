# 基于缩进的文件夹树构建

## 1. 本讲目标

本讲是「README 解析」专题的中段。在上一讲（u2-l3）里，我们已经知道 `Parse.FromReadme` 会扫描 `# Dependencies` 段落、识别每条 `[name](url)(version)` 依赖和用 `**[repo]**` 标记的当前库 `thisRepo`。但还有一个关键问题没有回答：**这些依赖到底落在哪些文件夹下？**

PSI 标准用 bullet 列表的**缩进**来表达文件夹层级。本讲要解决的就是「缩进 → 文件夹树」这步换算。学完后你应当能够：

1. 说出 `Parse.Folder` 与 `Parse.Repo` 两个内部类各自持有哪些字段、彼此如何互相引用。
2. 解释 `indent = line.find("*")` 为什么就等于「缩进深度」。
3. 用 `GetParentByChildIndent` 这个递归方法，手动推演「同级 / 降级 / 升级」三种层级关系下，一个新节点最终归属到哪个 folder。

本讲**只**讲树的构建，不涉及相对路径前缀（`../../`）和最终 `Dependency` 列表的组装——那是下一讲 u2-l5 的内容。

## 2. 前置知识

- **缩进即层级**：在 Markdown 的 bullet 列表里，`*` 前的空格越多，表示嵌套越深。PSI 标准规定「每个文件夹层级加一次缩进」。
- **Python 字符串方法**：`str.find(sub)` 返回子串 `sub` 首次出现的下标，找不到时返回 `-1`；`str.split(sep, maxsplit)` 按 `sep` 切分，`maxsplit` 限制切分次数。
- **递归**：一个函数在其内部调用自身。本讲的 `GetParentByChildIndent` 是一个沿父指针向上递归的小函数。
- **嵌套类（nested class）**：在 Python 里可以把一个类定义在另一个类体内，访问时写作 `Outer.Inner`。`Parse.Folder`、`Parse.Repo` 就是这种嵌套内部类。
- 建议先读完 u2-l3，了解 `FromReadme` 的段落定位与单行解析逻辑，再来读本讲。

## 3. 本讲源码地图

本讲几乎全部内容都集中在一个文件里：

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `Parse.py` | README 依赖解析器 | `Folder`/`Repo` 内部类、`indent` 计算、`GetParentByChildIndent` 与三/二分支 |

`Parse.py` 的整体结构：`Parse` 是对外类，`FromReadme` 是它的类方法入口；`Folder`、`Repo` 是 `Parse` 内部的两个嵌套类（源码注释明确写着 *Internal class, do not use*），仅服务于 `FromReadme` 的解析过程，不对外暴露。

## 4. 核心概念与源码讲解

### 4.1 Folder/Repo 内部类

#### 4.1.1 概念说明

PSI 的依赖声明本质上是一棵「文件夹树」：文件夹里既可以装子文件夹，也可以装 repo（即一个个被依赖的库）。`FromReadme` 在解析时，需要一个临时的内存结构把这棵树搭出来，搭完之后再「压扁」成一维的 `Dependency` 列表（u2-l5）。

这个临时结构就是两个内部类：

- **`Folder`**：文件夹节点。记住自己的名字、父节点、缩进级别，并维护两个列表：`subfolders`（子文件夹）和 `repos`（直接挂在自己名下的 repo）。
- **`Repo`**：一个被依赖的库。记住 `name`、`url`、`version`，以及自己挂在哪个 `folder` 下。

二者通过「父指针」互相连接：`Folder` 指向 `parent`，`Repo` 指向 `folder`。这样从任意一个叶子 repo 出发，都能沿着父指针一路向上回到根，从而拼出它的完整路径。

> 它们被标注为 *do not use*，意思是日常使用本包时不应直接构造它们；但在学习解析原理时，它们正是主角。

#### 4.1.2 核心流程

两个类的核心方法都是**沿父指针递归拼路径**：

- `Folder.GetPath()`：如果父节点是 `ROOT`，路径就是自己的名字；否则是「父亲的路径 + "/" + 自己的名字」。
- `Repo.GetPath()`：直接返回「所在 folder 的路径 + "/" + 自己的名字」。

直观地，路径拼接可写成：

\[ \text{path}(f) = \begin{cases} f.\text{name} & \text{若 } f.\text{parent}.\text{name} = \text{"ROOT"} \\ \text{path}(f.\text{parent}) + \text{"/"} + f.\text{name} & \text{否则} \end{cases} \]

`Folder` 还提供 `AddSubFolder` / `AddRepo` 两个写入方法，分别往 `subfolders`、`repos` 列表里追加节点。

#### 4.1.3 源码精读

`Folder` 类的定义与字段：[Parse.py:16-47](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Parse.py#L16-L47)。其中构造函数建立了 `name`、`parent`、`indent` 三个标量字段，以及 `subfolders`、`repos` 两个空列表：

```python
def __init__(self, name : str, parent, indent):
    self.name = name
    self.parent = parent
    self.indent = indent
    self.subfolders = []
    self.repos = []
```

`Folder.GetPath()` 的递归实现，注意它用 `parent.name == "ROOT"` 作为递归终止条件：[Parse.py:40-44](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Parse.py#L40-L44)。

`Repo` 类则更简单，只持有四个字段，`GetPath()` 直接复用所属 folder 的路径：[Parse.py:49-64](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Parse.py#L49-L64)。

```python
def GetPath(self):
    return self.folder.GetPath() + "/" + self.name
```

#### 4.1.4 代码实践

**实践目标**：用手工方式搭一棵最小的 Folder/Repo 树，验证 `GetPath()` 的递归拼接。

**操作步骤**（示例代码，可直接在装好本包的 Python 环境运行）：

```python
# 示例代码：手工搭建一棵最小树，验证 GetPath
from PsiFpgaLibDependencies import Parse

root  = Parse.Folder("ROOT", None, -2)
top   = Parse.Folder("Top", root, 0);   root.AddSubFolder(top)
mid   = Parse.Folder("Mid", top, 2);    top.AddSubFolder(mid)
repo  = Parse.Repo("lib_a", "url", "1.0.0", mid); mid.AddRepo(repo)

print("Folder Mid 路径:", mid.GetPath())   # 期望: Top/Mid
print("Repo   路径:", repo.GetPath())      # 期望: Top/Mid/lib_a
```

**需要观察的现象**：`mid.GetPath()` 只返回从 ROOT 之下开始的两段（`Top/Mid`，不含 `ROOT`），因为 `GetPath` 在父节点是 `ROOT` 时直接返回自身名字；`repo.GetPath()` 再追加一段库名。

**预期结果**：两行输出分别为 `Top/Mid` 与 `Top/Mid/lib_a`。

> 注意：`Parse.Folder` / `Parse.Repo` 是内部类，仅用于学习理解；正式代码里不要依赖它们。

#### 4.1.5 小练习与答案

**练习 1**：如果把上例中 `mid` 的 parent 误写成 `None`，调用 `mid.GetPath()` 会发生什么？

**答案**：`GetPath()` 会执行 `self.parent.name`，而 `self.parent` 是 `None`，于是抛出 `AttributeError: 'NoneType' object has no attribute 'name'`。这说明 `ROOT` 这个虚拟根节点是递归终止的关键，不能省略。

**练习 2**：`Folder.GetPath()` 为什么用 `parent.name == "ROOT"` 而不是 `parent is None` 来终止递归？

**答案**：因为根节点 `ROOT` 是一个真实存在的 `Folder` 对象（`parent` 指向它，不是 `None`），只是它的名字固定叫 `"ROOT"`。用名字判等可以在不引入 `None` 的前提下安全终止，同时让顶层真实文件夹的 `GetPath()` 从自身名字开始、不带上 `ROOT`。

---

### 4.2 indent 计算

#### 4.2.1 概念说明

整棵树是「按缩进搭起来」的，因此「如何把一行 bullet 换算成一个表示深度的数字」就是解析的基础。本包的做法极其直接：**缩进深度 = 这一行里第一个 `*` 出现的位置**。

例如 `    * [lib]` 这一行，`*` 在第 4 个字符位置（前面有 4 个空格），所以 `indent = 4`。

#### 4.2.2 核心流程

`FromReadme` 在主循环里对每一行做两步：

1. **判定是不是 bullet**：把整行去掉所有空白字符后，看是否以 `*` 开头。这一步只是「是 / 否」的判断，不改变原始行。
2. **算 indent 与 text**：在**原始行**上调用 `line.find("*")` 得到缩进；用 `line.split("*", 1)[1]` 取 `*` 之后的内容并去空格，得到 `text`。

关键点：第 1 步用的是「去空白后的行」只为了判断，而第 2 步的 `find` / `split` 都作用在**原始行**上，所以前导空格被完整保留并参与 `indent` 计算。

#### 4.2.3 源码精读

bullet 判定与 indent/text 提取紧挨在一起：[Parse.py:114-116](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Parse.py#L114-L116)。

```python
if re.sub(r"\s", "", line).startswith("*"):
    indent = line.find("*")
    text = line.split("*", 1)[1].replace(" ", "")
```

- `re.sub(r"\s", "", line)` 把**所有**空白（含行首空格、行内空格）删掉，再 `startswith("*")`，所以 `   * foo`、`*foo` 都能通过。
- `line.find("*")` 在原始行上定位首个 `*`，返回值就是前导空格数。
- `line.split("*", 1)[1]` 只在第一个 `*` 处切一刀，取右半段，再 `.replace(" ", "")` 去掉所有空格得到纯文本 `text`（后续用 `[` 是否出现在 `text` 里来区分 repo 还是 folder）。

#### 4.2.4 代码实践

**实践目标**：在 Python REPL 里手动验证 `find("*")` 与缩进的等价关系。

**操作步骤**：

```python
# 示例代码：手动计算若干行的 indent
for line in ["* Top", "  * Mid", "    * [lib_a](url)(1.0.0)", "not a bullet"]:
    stripped = "".join(line.split())            # 模拟 re.sub 去所有空白
    is_bullet = stripped.startswith("*")
    indent = line.find("*")
    print(f"{is_bullet!s:5} indent={indent!s:>3}  <- {line!r}")
```

**需要观察的现象**：前三行 `is_bullet=True`，`indent` 分别为 `0`、`2`、`4`；第四行 `find("*")` 返回 `-1` 且 `is_bullet=False`（因为去空白后不以 `*` 开头）。

**预期结果**：

```
True  indent=  0  <- '* Top'
True  indent=  2  <- '  * Mid'
True  indent=  4  <- '    * [lib_a](url)(1.0.0)'
False indent= -1  <- 'not a bullet'
```

注意 `-1` 这个返回值：它说明 `find` 找不到时返回 `-1`，这也是为什么解析器必须先用 `startswith("*")` 把非 bullet 行挡掉，否则 `-1` 的 indent 会扰乱层级判断。

#### 4.2.5 小练习与答案

**练习 1**：如果某行用 **Tab** 缩进（`\t* foo`），`indent` 会是多少？解析器还能正确处理吗？

**答案**：`line.find("*")` 会返回 `1`（Tab 占 1 个字符），所以 `indent=1`。但 README 里其它层级用的是空格（indent 为 0/2/4…），混用 Tab 会让 `1` 这个值对不上任何既有的 `lastFolder.indent`，从而可能把节点挂到错误的 folder。结论：**PSI README 必须统一用空格缩进**，这一点 README.md 第 31 行的规范「Add indent for every folder level」也是隐含要求。

**练习 2**：为什么 bullet 判定要用 `re.sub(r"\s", "", line).startswith("*")`，而不是直接 `line.startswith("*")`？

**答案**：因为带缩进的 bullet 行首是空格而不是 `*`，直接 `line.startswith("*")` 只能匹配顶级（indent=0）的行，会把所有缩进行漏掉。先去空白再判断，才能识别任意缩进层级的 bullet。

---

### 4.3 GetParentByChildIndent 与三种层级分支

#### 4.3.1 概念说明

有了 `indent` 数字，真正的难题来了：**当读到一行新节点时，怎么知道它该挂在哪个 folder 下？** 解析是「顺序读行」的，只能依靠「当前行的 indent」和「上一个 folder 的 indent」的相对大小来做判断。

这里有一个贯穿全程的游标变量：**`lastFolder`**——它是「最近一次创建的 folder」。注意一个重要事实：**只有遇到 folder 才会更新 `lastFolder`，遇到 repo 不会**。所以判断层级时，参照系始终是「最近的那个文件夹」，而不是上一个 repo。

`GetParentByChildIndent(k)` 是这套机制的核心：给定一个目标子节点缩进 `k`，从当前 folder 出发，沿父指针**向上**找到「应当作为该子节点父亲」的那个 folder。

#### 4.3.2 核心流程

**`GetParentByChildIndent` 的递归定义**：

\[ \text{findParent}(f, k) = \begin{cases} f & \text{若 } k > f.\text{indent} \quad(\text{当前 folder 比 } k \text{ 浅，它就是父}) \\ \text{findParent}(f.\text{parent}, k) & \text{若 } k \le f.\text{indent} \quad(\text{当前 folder 与 } k \text{ 同级或更深，继续上溯}) \end{cases} \]

直觉：目标缩进 `k` 越大表示越深。我们从某个深层 folder 出发往上走，**只要当前 folder 的 indent 不比 `k` 小，它就太深、不可能是父**；一旦遇到一个 indent 严格小于 `k` 的 folder，它就是父。

**遇到 Folder 时的三种分支**（比较 `indent` 与 `lastFolder.indent`）：

| 分支 | 条件 | 新 folder 的 parent | 含义 |
| --- | --- | --- | --- |
| 同级（Same） | `indent == lastFolder.indent` | `lastFolder.parent` | 新文件夹是上一个的**兄弟** |
| 降级（Lower） | `indent > lastFolder.indent` | `lastFolder` | 新文件夹是上一个的**孩子** |
| 升级（Higher） | `indent < lastFolder.indent` | `GetParentByChildIndent(indent)` | 新文件夹要回到**某层祖先**之下 |

> 一个值得注意的等价关系：把「同级」分支交给 `GetParentByChildIndent` 其实会得到同样的结果（因为 `k == lastFolder.indent` 时函数会再上溯一层到 `lastFolder.parent`）。代码里显式写出「同级」分支只是一个**直达捷径**，省去一次递归调用。

**遇到 Repo 时的两条分支**：

| 分支 | 条件 | repo 挂到哪个 folder |
| --- | --- | --- |
| 直接下挂 | `indent > lastFolder.indent` | `lastFolder`（最近 folder 就是它的宿主） |
| 上溯查找 | `indent <= lastFolder.indent` | `lastFolder.GetParentByChildIndent(indent)` |

在正常的嵌套写法里，repo 总比它所在的 folder 深一级，所以几乎总是走「直接下挂」分支；「上溯查找」分支处理 repo 与最近 folder 同级或更浅的边界情形。

**整体流程伪代码**：

```
rootFolder = Folder("ROOT", None, -2); lastFolder = rootFolder
对每一行 bullet:
    indent = line.find("*")
    if 是 repo（text 含 '['）:
        if indent > lastFolder.indent:
            宿主 = lastFolder                      # 直接下挂
        else:
            宿主 = lastFolder.GetParentByChildIndent(indent)   # 上溯
        把 repo 挂到 宿主.repos
        （不更新 lastFolder！）
    else: # 是 folder
        if indent == lastFolder.indent:  宿主 = lastFolder.parent      # 同级
        elif indent > lastFolder.indent: 宿主 = lastFolder             # 降级
        else:                            宿主 = lastFolder.GetParentByChildIndent(indent)  # 升级
        新建 folder(text, 宿主, indent); 挂到 宿主.subfolders
        lastFolder = 新 folder             # 只有 folder 才更新游标
```

`ROOT` 的 `indent` 被设成 `-2`，是一个**哨兵值**：任何真实 bullet 的 `indent >= 0` 都严格大于它，所以 `ROOT` 永远满足 `k > ROOT.indent`，可被 `GetParentByChildIndent` 作为最终祖先正确返回，也能在第一个顶级 folder 处落入「降级」分支成为它的父亲。

#### 4.3.3 源码精读

`GetParentByChildIndent` 的递归本体：[Parse.py:34-38](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Parse.py#L34-L38)。

```python
def GetParentByChildIndent(self, indent):
    if indent <= self.indent:
        return self.parent.GetParentByChildIndent(indent)
    else:
        return self
```

`ROOT` 与 `lastFolder` 游标的初始化（`ROOT.indent = -2`）：[Parse.py:98-100](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Parse.py#L98-L100)。

repo 的两条分支（注意它**不**更新 `lastFolder`）：[Parse.py:125-131](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Parse.py#L125-L131)。

```python
if indent > lastFolder.indent:
    repo = cls.Repo(name, url, version, lastFolder)
    lastFolder.AddRepo(repo)
else:
    fld = lastFolder.GetParentByChildIndent(indent)
    repo = cls.Repo(name, url, version, fld)
    fld.AddRepo(repo)
```

folder 的三种分支，末尾 `lastFolder = fold` 是唯一更新游标之处：[Parse.py:137-150](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Parse.py#L137-L150)。

```python
if indent == lastFolder.indent:          # 同级
    fold = cls.Folder(text, lastFolder.parent, indent)
    lastFolder.parent.AddSubFolder(fold)
elif indent > lastFolder.indent:         # 降级
    fold = cls.Folder(text, lastFolder, indent)
    lastFolder.AddSubFolder(fold)
else:                                    # 升级
    par = lastFolder.GetParentByChildIndent(indent)
    fold = cls.Folder(text, par, indent)
    par.AddSubFolder(fold)
lastFolder = fold
```

`FromReadme` docstring 里给出的标准依赖段示例（真实存在于源码注释中）：[Parse.py:79-86](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Parse.py#L79-L86)。

#### 4.3.4 代码实践

**实践目标**：给定一段覆盖「降级 / 同级 / 升级」三种分支的多层嵌套依赖声明，纸笔推演对应的 Folder/Repo 树，标注每个 repo 归属到哪个 folder，并说明判断依据。

**操作步骤**：请先**不要**运行代码，拿一张纸对下面这段 `# Dependencies` 段逐行分析。

```
# Dependencies

* Top
  * Mid
    * [lib_a](url)(1.0.0)
    * [lib_b](url)(2.0.0)
  * Side
    * [lib_c](url)(1.1.0)
* Other
  * [**this_lib**](url)
```

对每一行记录三件事：①`indent`；②`indent` 与 `lastFolder.indent` 的比较结果（同级/降级/升级，或 repo 的直接下挂/上溯）；③新节点挂在哪个 folder 下、`lastFolder` 是否变化。

**需要观察的现象**（参考答案）：

| 行 | indent | 比较（参照 `lastFolder`） | 归属 / 动作 | lastFolder 变化 |
| --- | --- | --- | --- | --- |
| `* Top` | 0 | 0 > ROOT(-2) → 降级 | Top 挂 ROOT | ROOT → **Top** |
| `  * Mid` | 2 | 2 > Top(0) → 降级 | Mid 挂 Top | Top → **Mid** |
| `    * [lib_a]…` | 4 | 4 > Mid(2) → repo 直接下挂 | lib_a 挂 Mid | 不变（Mid） |
| `    * [lib_b]…` | 4 | 4 > Mid(2) → repo 直接下挂 | lib_b 挂 Mid | 不变（Mid） |
| `  * Side` | 2 | 2 == Mid(2) → 同级 | Side 挂 Mid.parent=Top | Mid → **Side** |
| `    * [lib_c]…` | 4 | 4 > Side(2) → repo 直接下挂 | lib_c 挂 Side | 不变（Side） |
| `* Other` | 0 | 0 < Side(2) → 升级 | `Side.GetParentByChildIndent(0)`：Side(2)→Top(0)→ROOT(-2) 返回 ROOT；Other 挂 ROOT | Side → **Other** |
| `  * [**this_lib**]…` | 2 | 2 > Other(0) → repo 直接下挂 | this_lib 挂 Other（并成为 thisRepo） | 不变（Other） |

**最终树形结构**：

```
ROOT (-2)
├── Top (0)
│   ├── Mid (2)
│   │   ├── lib_a
│   │   └── lib_b
│   └── Side (2)
│       └── lib_c
└── Other (0)
    └── this_lib   ← thisRepo
```

**关键判断说明**：
- **降级**（`indent > lastFolder.indent`）：新 folder 是上一个的孩子，`parent = lastFolder`。
- **同级**（`indent == lastFolder.indent`）：新 folder 与上一个同父，`parent = lastFolder.parent`。
- **升级**（`indent < lastFolder.indent`）：需要 `GetParentByChildIndent` 上溯。本例 `* Other` 从 Side(2) 起上溯，越过同为 0 的 Top（因为 `0 <= 0` 继续上溯），停在 ROOT（因为 `0 > -2`），所以 Other 与 Top 成了 ROOT 下的兄弟。

**预期结果**：你纸笔画出的树应与上图一致；每个 repo 的宿主 folder 分别为 Mid、Mid、Side、Other。

**可选验证（运行型）**：把上面这段存成 `demo_readme.md`，调用 `Parse.FromReadme("demo_readme.md")` 并打印每条依赖的 `relativePath`。你会看到路径前缀恰好反映了上面的树（如 lib_a 的路径会落在 `Top/Mid/lib_a` 这一相对结构上，前缀 `../../` 的来历见 u2-l5）。若结果与你的纸笔推演一致，则说明三种分支的归属判断正确。（前缀换算机制在下一讲详细讲解，此处仅用于交叉验证树形。）

#### 4.3.5 小练习与答案

**练习 1**：在本例中，如果把 `* Other` 这一行误删了、让 `this_lib` 直接出现在 `Side` 同级，`thisRepo` 还能被正确识别吗？它会被挂到哪个 folder？

**答案**：仍能识别（`**` 标记与挂载位置无关，只看 `name.startswith("**")`）。此时 `this_lib` 的 indent=2，与 `lastFolder=Side`(indent=2) 同级，走 repo 的「上溯」分支：`Side.GetParentByChildIndent(2)` → 上溯到 `Side.parent=Top`(0)，因 `2 > 0` 返回 Top。所以 `this_lib` 会挂在 `Top` 下，成为 `lib_c` 的「叔辈」节点。

**练习 2**：为什么 repo 分支里没有像 folder 那样专门写一个「同级」分支，而是把 `indent <= lastFolder.indent` 一股脑交给 `GetParentByChildIndent`？

**答案**：因为 repo 不更新 `lastFolder`，多个 repo 经常连续出现在同一 folder 下、且 indent 相同。把「同级」与「升级」合并成 `indent <= lastFolder.indent` 一个条件交给 `GetParentByChildIndent`，就能用同一套上溯逻辑统一处理「repo 与最近 folder 同级（应挂到 folder 的父）」和「repo 更浅（挂到更高祖先）」两种情况，无需为 repo 单独维护游标，代码更简洁。

**练习 3**：`ROOT.indent` 改成 `-1` 而不是 `-2`，本例的解析结果会变吗？

**答案**：不会变。本例所有真实 indent 都 ≥ 0，无论是 `-1` 还是 `-2`，都满足「真实 indent 严格大于 ROOT.indent」，`ROOT` 都能被 `GetParentByChildIndent` 正确返回、也能在首个顶级 folder 处落入降级分支。`-2` 只是一个更「安全」的哨兵值，留出余量。

## 5. 综合实践

**任务**：自己设计一段「至少含两次升级、一次同级、三次降级」的 `# Dependencies` 声明，先纸笔画出 Folder/Repo 树并标注每个 repo 的宿主 folder；再把它写成 `my_readme.md`，运行下面的脚本，用解析结果反向核对你的树。

```python
# 示例代码：用解析结果反向核对纸笔推演的树
from PsiFpgaLibDependencies import Parse

for d in Parse.FromReadme("my_readme.md"):
    # relativePath 形如 ../../A/B/lib_x —— 去掉前缀后即是 lib_x 在树中的相对位置
    rel = d.relativePath.split("/../")[-1]   # 去掉 levelsToRoot 个 ".."（详见 u2-l5）
    print(f"{d.libraryName:12} -> {rel}   (minVersion={d.minVersion})")
```

**自检要点**：
1. 你画出的树里，每个 repo 的「宿主 folder 链」是否与脚本输出的 `rel` 路径一致？
2. 设计里那次「升级」是否被 `GetParentByChildIndent` 正确地送回到预期的祖先 folder 下？
3. 当前库（`**[lib]**`）有没有从输出里「消失」？（它作为 `thisRepo` 被排除，这是 u2-l5 的内容，但你可以提前观察到这一现象。）

> 关于 `relativePath` 里 `../../` 前缀的来历、`levelsToRoot` 的计算以及 `thisRepo` 为何被排除，全部在下一讲 u2-l5 展开。本实践的目的是让你先把「缩进 → 树」这一步彻底吃透。

## 6. 本讲小结

- `Parse.Folder` 与 `Parse.Repo` 是 `FromReadme` 解析过程中的两个**内部临时类**：Folder 记 `name/parent/indent/subfolders/repos`，Repo 记 `name/url/version/folder`，二者靠父指针互连，`GetPath()` 沿父指针递归拼路径。
- **缩进即深度**：`indent = line.find("*")`，等于行首空格数；判定 bullet 时先去全部空白再 `startswith("*")`，但 `find` 作用在原始行上。
- **`lastFolder` 游标只在遇到 folder 时更新**，repo 不更新；因此层级判断的参照系永远是「最近的文件夹」。
- **`GetParentByChildIndent(k)`** 沿父指针上溯，直到找到一个 indent 严格小于 `k` 的 folder 作为父；`ROOT.indent = -2` 是保证它总能作为顶端祖先的哨兵。
- folder 有**三种分支**（同级用 `lastFolder.parent`、降级用 `lastFolder`、升级用 `GetParentByChildIndent`）；repo 有**两条分支**（`indent > lastFolder.indent` 直接下挂，否则上溯）。其中「同级」分支其实是 `GetParentByChildIndent` 的一个直达捷径。

## 7. 下一步学习建议

下一讲 **u2-l5（相对路径解析与依赖列表组装）** 会接着本讲的树往下讲：

- 如何从 `thisRepo` 沿父指针数到 `ROOT`，得到 `levelsToRoot`；
- 如何用 `"/".join([".."] * levelsToRoot)` 生成 `../../` 前缀 `pathPrefix`；
- 如何遍历 `allRepos`、排除 `thisRepo` 自身、把每个 repo 的 `GetPath()` 拼上 `pathPrefix`，组装出最终的 `Dependency` 列表。

建议你带着本讲综合实践中已经观察到的「`thisRepo` 从输出中消失」「路径前缀反映树形」这两个现象去读 u2-l5，那里会给出完整的数学解释。在此之前，也可以回头对照 u2-l3，确认自己对「段落定位 / thisRepo 标记」的理解没有断层。
