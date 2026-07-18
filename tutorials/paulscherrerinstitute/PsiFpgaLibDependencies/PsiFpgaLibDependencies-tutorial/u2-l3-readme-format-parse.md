# README 依赖格式与 Parse.FromReadme 入口

> 本讲属于进阶层（intermediate）。在学完 [u2-l1 依赖数据模型 Dependency](u2-l1-dependency-model.md) 之后，你已经知道 `Dependency` 用四个字段描述一条依赖。本讲回答一个更前面的问题：**这堆 `Dependency` 对象是从哪里来的？** 答案是——从每个 FPGA 库自己的 `README.md` 里「读」出来的。读完本讲，你就能写出一份能被解析器正确识别的 PSI 标准 README，并理解解析器在文件里如何「定位段落」「识别当前库」。

## 1. 本讲目标

学完本讲，你应当能够：

- 说出 PSI 标准 README 中 `# Dependencies` 段落的**写法约定**：独立段落、bullet 列表、缩进表示文件夹层级、`[name](url)(minVersion)` 表示一条依赖、`[**name**](url)` 表示「当前库」。
- 解释 `Parse.FromReadme` 是如何**读取文件**、**定位依赖段落**、并在遇到下一个 `#` 标题时停止的。
- 理解 **thisRepo 标记**（`**`）的作用：它告诉解析器「这一条就是正在被解析的库本身」，从而把所有其它依赖的相对路径换算到它身上；如果整个段落里没有任何 `**` 标记，解析器会抛出 `Active repository not marked with **[repo]**` 异常。

本讲**只**覆盖三个最小模块：依赖段格式、段落定位、thisRepo 标记。文件夹树的构建（缩进如何变成 Folder/Repo）放在 [u2-l4](u2-l4-folder-tree.md)，最终的相对路径换算放在 [u2-l5](u2-l5-path-resolution.md)。

## 2. 前置知识

- **Markdown 的 bullet 列表与缩进**：`*` 开头的行是列表项，缩进（行首空格）越多，表示嵌套越深。本项目的解析器正是利用缩进来还原「文件夹/子库」的树形结构。
- **正则表达式基础**：会用 `\[...\]` 匹配方括号、`\(...\)` 匹配圆括号即可。本讲会逐行解释源码里出现的正则，不要求你事先精通。
- **Python 字符串方法**：`str.replace`、`str.split`、`str.find`、`str.startswith`，以及标准库 `re.findall`。
- **u2-l1 中的 `Dependency` 四字段**：`libraryName`、`url`、`relativePath`、`minVersion`。本讲末尾组装出来的对象就是 `Dependency`。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用到的地方 |
| --- | --- | --- |
| `Parse.py` | 全项目最复杂的文件。`Parse.FromReadme` 是把 README 文本翻译成 `Dependency` 列表的入口。内部还定义了 `Folder`/`Repo` 两个内部类（本讲只做背景介绍，细节在 u2-l4）。 | `FromReadme` 的读取、定位、识别 thisRepo |
| `README.md` | 项目自己的说明文档。它既规定了「PSI 标准 README」的格式，本身也带有一个 `## Dependencies` 段（内容是 `None`，可作为一个「空依赖」的真实样例）。 | 格式约定的文字说明、空依赖段样例 |

> 提醒：`README.md` 里写的格式说明是「约定」，真正「执行」这套约定的是 `Parse.py`。当二者出现细微出入时，以源码行为为准（本讲会指出几处）。

## 4. 核心概念与源码讲解

### 4.1 依赖段格式

#### 4.1.1 概念说明

PSI 的每个 FPGA 库都把自己的依赖写在 `README.md` 的一个**独立段落**里，段落标题是 `# Dependencies`（依赖）。这个段落用一份 **bullet 列表**描述「仓库里会有哪些文件夹、每个文件夹里放哪些子库」。约定有三条（来自 [README.md:28-33](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/README.md#L28-L33)）：

1. 必须有独立的 `# Dependencies` 段落；
2. 段落内用 bullet 列表罗列依赖，**每多一级文件夹就多一级缩进**；
3. 每个子库写成 `[name](url)(minVersion)` 的形式。

`Parse.FromReadme` 的文档字符串里给出了一份示例（[Parse.py:78-86](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Parse.py#L78-L86)）：

```
# Dependencies

* A folder
  * Subfolder
    * [some\_lib] (url) (1.0.0 or higher)
    * [other\_lib (url) (1.2.3)
  * OtherFolder
    * [**this\_lib**]
```

把示例里的要素拆开看：

| 写法 | 含义 | 解析后映射到 |
| --- | --- | --- |
| `* A folder` | 一行不以 `[` 开头的 bullet → 一个**文件夹** | 内部 `Folder`（u2-l4 详讲） |
| `[some\_lib]` | 方括号里是**库名**（`\_` 是 Markdown 转义的下划线） | `Dependency.libraryName` |
| `(url)` | 第一个圆括号组是 **git 远程地址** | `Dependency.url` |
| `(1.0.0 or higher)` | 后续圆括号里**以数字开头**的部分是最低版本 | `Dependency.minVersion` |
| `[**this\_lib**]` | 名字以 `**` 开头 → 这是「当前库」标记 | `thisRepo`（见 4.3） |

一条完整依赖的标准写法是：

```
* [库的名称](git仓库地址)(最低版本号)
```

注意：示例里为了易读把 `url` 写成了占位符；在真实仓库里它会是一个完整的 `https://...git` 地址（详见 4.1.3 关于「url 必须存在」的源码说明）。

#### 4.1.2 核心流程

`FromReadme` 在识别出「这是一条库依赖（bullet 文本里含有 `[`）」之后，会用三步把一行文本切成三个字段：

```text
输入: 一行去掉 bullet 后的文本 text，例如 "[dep_a](https://github.com/foo/dep_a.git)(1.2.0)"

步骤1 取库名:  用正则匹配第一个 [ ... ] 内的内容，再去掉反斜杠   -> "dep_a"
步骤2 取 url:  用正则匹配第一个 ( ... ) 内的内容                  -> "https://github.com/foo/dep_a.git"
步骤3 取版本:  用正则匹配「( 后紧跟数字/点」的一段                -> "1.2.0"
            (若名字以 ** 开头，则版本直接设为字符串 "None")
```

这里有一个对初学者很关键的设计：**库名和 url 用「成对符号」匹配（方括号配方括号、圆括号配圆括号），而版本只匹配「圆括号里以数字开头」的那一组**。这就允许版本写成 `(1.0.0 or higher)` 这种带文字说明的形式——解析器只摘取前缀的数字段 `1.0.0`，忽略后面的 `or higher`。

#### 4.1.3 源码精读

判断「这一行是库依赖还是文件夹」只看一个条件：去掉空格后的 bullet 文本里**有没有 `[`**（[Parse.py:117-118](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Parse.py#L117-L118)）：

```python
#*** Repo ***
if "[" in text:
```

接下来三行分别取 name / url / version（[Parse.py:119-124](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Parse.py#L119-L124)）：

```python
name = re.findall("\[([^\]]+)]", text)[0].replace("\\", "")
url  = re.findall("\(([^\)]+)\)", text)[0]
if name.startswith("**"):
    version = "None"
else:
    version = re.findall("\(([0-9\.]+)", text)[0]
```

逐行说明：

- **name**：`\[([^\]]+)]` 匹配第一个 `[`，捕获到下一个 `]` 之前的全部字符。`.replace("\\", "")` 再把反斜杠去掉，于是 Markdown 里转义下划线用的 `some\_lib` 还原成 `some_lib`。
- **url**：`\(([^\)]+)\)` 匹配**第一个** `(...)` 组。⚠️ 注意这行是**无条件执行**的：只要这一行里有 `[`，就一定会去找一个 `(...)`。因此，即使是 `**` 标记的「当前库」那一行，也**必须带一个 `(url)`**，否则 `re.findall` 返回空列表、`[0]` 会抛 `IndexError`。文档字符串示例里写的 `[**this\_lib**]`（省略了 url）只是为了简洁，直接照抄会让程序在这里报错——真实使用时要写成 `[**name**](url)`。
- **version**：`\(([0-9\.]+)` 匹配「`(` 后紧跟若干位数字或点」，所以 `(1.0.0 or higher)` 只会被摘成 `1.0.0`。当名字以 `**` 开头（即当前库）时，版本直接赋成字符串 `"None"`（4.3 节展开）。

> 小结：标准格式 `[name](url)(minVersion)` 能被这三行正则完整解析；其中 url 是**强制**的，minVersion 是「以数字开头才取」的。

#### 4.1.4 代码实践

**目标**：在不运行程序的前提下，根据 4.1.3 的三条正则，手工预测解析结果，培养「读源码即可推断行为」的能力。

**操作步骤**：

1. 阅读下面这行 bullet（已去掉行首 bullet 与空格后的 `text`）：

   ```text
   [psi\_foo](https://github.com/x/psi_foo.git)(2.3.0 or higher)
   ```

2. 分别套用 name / url / version 三条规则，写出你预测的三个字段。

**需要观察的现象**：留意 `\_)` 如何被还原、`or higher` 如何被丢弃。

**预期结果（待本地验证）**：

- `name` = `psi_foo`（`\_` → `_`）
- `url` = `https://github.com/x/psi_foo.git`
- `version` = `2.3.0`（`or higher` 被忽略）

#### 4.1.5 小练习与答案

**练习 1**：把版本写成 `(v1.2.0)`（前面多了个字母 `v`），`version` 字段会被解析成什么？

**答案**：会抛 `IndexError`。因为 `re.findall("\(([0-9\.]+)", text)` 要求 `(` 后**立即**是数字或点；`(v1.2.0)` 的 `(` 后是字母 `v`，匹配失败返回空列表，`[0]` 越界。这正是本项目要求版本号以数字开头的原因。

**练习 2**：为什么 name 的正则后面要跟 `.replace("\\", "")`？

**答案**：PSI 仓库名常含下划线，在 Markdown 里为了避免斜体通常写成 `\_`。解析器需要把转义符去掉，还原成真实库名（`some\_lib` → `some_lib`），否则 `Dependency.libraryName` 里会混入反斜杠。

---

### 4.2 段落定位

#### 4.2.1 概念说明

一份 README 通常很长，除了 `# Dependencies` 还有 `# Description`、`# Installation` 等很多段落。`FromReadme` 必须**先找到依赖段落的起点，再在段落结束时停下**，否则会把无关文本误当成依赖。这一节讲的就是这「一头一尾」的定位逻辑。

定位规则可以用一句话概括：**从上往下扫描所有行，遇到「依赖标题」就开始，遇到「下一个 `#` 标题」就结束**。

#### 4.2.2 核心流程

```text
打开文件，逐行读取 (lines)
初始化: startFound = False

对每一行 line:
  1. 先把 line 去掉所有空格、转小写，看是否包含子串 "#dependencies"
       是 -> startFound = True，跳过本行（标题行本身不参与解析）
       否 且 还没开始 -> 跳过本行（段落之前的内容一律忽略）
  2. （已经进入段落之后）若本行原文以 "#" 开头 -> 遇到下一个标题，break 结束扫描
  3. 若本行去掉空白后以 "*" 开头 -> 这是一条 bullet，进入解析（4.1 / 4.3）
       否则 -> 不是 bullet（空行、普通文字），忽略
```

要点：**起点**靠「去空格+小写后的子串匹配」识别，所以 `# Dependencies`、`#Dependencies`、`## dependencies` 等写法都能命中（甚至大小写、空格随意）；**终点**靠「原文以 `#` 开头」识别，所以遇到下一个 Markdown 标题就停。

#### 4.2.3 源码精读

文件读取很直接（[Parse.py:92-94](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Parse.py#L92-L94)）：

```python
#Read File
with open(readmeFile) as f:
    lines = f.readlines()
```

进入循环前的状态初始化（[Parse.py:96-101](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Parse.py#L96-L101)）：

```python
startFound = False
rootFolder = cls.Folder("ROOT", None, -2)
lastFolder = rootFolder
allRepos = []
thisRepo = None
```

其中 `rootFolder`/`lastFolder` 属于 u2-l4 的文件夹树，本讲只需知道它们被初始化好；`thisRepo = None` 是 4.3 节的关键初值。

**起点定位**——找到依赖标题（[Parse.py:104-109](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Parse.py#L104-L109)）：

```python
#Skip until dependencies found
if "#dependencies" in line.replace(" ", "").lower():
    startFound = True
    continue
elif not startFound:
    continue
```

说明：`line.replace(" ", "").lower()` 把整行去掉空格再转小写，然后判断是否**包含**子串 `"#dependencies"`。因此以下写法都会命中：

- `# Dependencies`
- `#Dependencies`（无空格）
- `## Dependencies`（二级标题，因 `##dependencies` 仍包含子串）
- `#  dependencies`（多空格）

命中后 `startFound = True` 并 `continue`（标题行本身不当作 bullet 处理）。若尚未命中，则 `elif not startFound: continue` 把段落之前的所有内容跳过。

**终点定位**——遇到下一个标题就停（[Parse.py:110-112](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Parse.py#L110-L112)）：

```python
#Stop at next section
if line.startswith("#"):
    break
```

注意这里用的是**原始 line**（不去空格、不转小写）。因为标题标题行一定以 `#` 开头，而 bullet 行（`  * ...`）不会。又因为起点判断在前一步已经 `continue` 掉了标题行本身，所以这里不会把「依赖标题」误判成终点。

**bullet 识别**（[Parse.py:113-116](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Parse.py#L113-L116)）：

```python
#Parse
if re.sub(r"\s", "", line).startswith("*"):
    indent = line.find("*")
    text = line.split("*", 1)[1].replace(" ", "")
```

- `re.sub(r"\s", "", line)` 删掉**所有**空白字符后判断是否以 `*` 开头，从而识别出「任意缩进层级」的 bullet。
- `indent = line.find("*")`：在**原始行**里找第一个 `*` 的位置，这个位置（行首空格数）就代表缩进层级，是 u2-l4 构建文件夹树的依据。
- `text = line.split("*", 1)[1].replace(" ", "")`：按**第一个** `*` 切一刀，取其后半段再去掉空格，得到供 4.1 节解析的 `text`。

> 本项目自己的 `README.md` 就是一个「空依赖段」的真实样例：[README.md:15-16](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/README.md#L15-L16) 写着 `## Dependencies` / `None`。把它喂给 `FromReadme` 会命中起点（`##dependencies` 含子串），但段落里没有任何 bullet、也没有 `**` 标记，最终在 4.3 节的校验处抛出异常。也就是说，**本包无法解析它自己的 README**——这也是为什么本讲的实践任务要求你自己造一份 README。

#### 4.2.4 代码实践

**目标**：验证「起点定位」对空格和大小写的宽容度。

**操作步骤**：

1. 准备两份最小 README，标题分别写成 `# Dependencies` 与 `##dependencies`，正文都包含同一个 `**` 当前库标记和一个普通依赖。
2. 分别调用 `Parse.FromReadme` 解析。

**需要观察的现象**：两种标题写法是否都能成功进入段落并解析出依赖。

**预期结果（待本地验证）**：两种写法都能解析成功，得到相同的一条依赖。这印证了「去空格 + 小写 + 子串匹配」的识别逻辑。

#### 4.2.5 小练习与答案

**练习 1**：如果在 `# Dependencies` 之前还有一段 `# Description`，里面碰巧有一行写着 `see #dependencies list below`，会发生什么？

**答案**：因为起点判断是「子串包含」，这行 `... #dependencies ...` 去空格小写后仍包含 `#dependencies`，会**提前**把 `startFound` 置真。这是该匹配方式的一个弱点：它不要求 `#dependencies` 出现在行首。真实仓库里应避免在标题前出现这样的文字。

**练习 2**：终点判断为什么用原始 `line.startswith("#")`，而不是像起点那样先去空格？

**答案**：bullet 行的特征是「若干空格后跟 `*`」，去掉空格后**会**变成以 `*` 开头，但**不会**以 `#` 开头；而 Markdown 标题行一定以 `#` 开头。用原始行的 `startswith("#")` 既能精确识别「下一个标题」，又不会把 bullet 误判成终点。如果反过来去掉空格再判断 `#`，反而可能把含 `#` 的普通文本误当成标题。

---

### 4.3 thisRepo 标记

#### 4.3.1 概念说明

依赖段落里通常会列出**很多**库，但其中**有一个**是「正在被解析的库自己」——也就是这份 README 所属的那个仓库。解析器必须知道「哪一条是我自己」，原因有二：

1. 自己不需要被当成依赖去 clone，要从结果里**排除**；
2. 其它依赖的 `relativePath` 是**相对于「自己」所在位置**计算的（u2-l5 详讲），所以需要「自己」作为参照原点。

PSI 的约定是：在「自己」那条的名字前加 Markdown 加粗符 `**`，即写成 `[**my_lib**](url)`。解析器把它记作 `thisRepo`。

#### 4.3.2 核心流程

```text
对每一条识别出来的库依赖 (name/url/version):
  若 name 以 "**" 开头:
     version = "None"        # 自己不需要最低版本要求
     thisRepo = 这条 repo    # 记住「我就是原点」

扫描结束后，做一次校验:
  若 thisRepo 仍是 None:
     抛异常 "Active repository not marked with **[repo]**"
```

也就是说：`**` 标记**不是可选的装饰**，而是必需的锚点。整个段落里一条都没有，解析就会失败。

#### 4.3.3 源码精读

在取完 name 后，用 `name.startswith("**")` 判断当前这条是不是「自己」（[Parse.py:121-124](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Parse.py#L121-L124)）：

```python
if name.startswith("**"):
    version = "None"
else:
    version = re.findall("\(([0-9\.]+)", text)[0]
```

注意这里的 `name` 是 4.1.3 里从方括号中取出的内容。要让 `name` 以 `**` 开头，`**` 必须**在方括号内部**、紧跟在 `[` 之后，即写成 `[**name**]`。把 `**` 写在方括号外面（`**[name]**`）的话，取出的 `name` 是 `name`，不会以 `**` 开头，就无法被识别为 thisRepo。

随后在创建 repo 对象时，再次用同样的条件把这条记录为 `thisRepo`（[Parse.py:132-133](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Parse.py#L132-L133)）：

```python
if name.startswith("**"):
    thisRepo = repo
allRepos.append(repo)
```

扫描全部结束后，做一次强制性校验（[Parse.py:151-153](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Parse.py#L151-L153)）：

```python
#Check
if thisRepo == None:
    raise Exception("Active repository not marked with **[repo]**")
```

由于初始化时 `thisRepo = None`（见 4.2.3），只要整个段落没有任何一条以 `**` 开头，这里就会抛出异常，且异常信息明确告诉用户「请用 `**[repo]**` 标记当前仓库」。

> 设计含义：`**` 标记是 PSI 依赖体系的「坐标原点」。没有它，相对路径无从算起（u2-l5），所以解析器选择**快速失败**而不是悄悄给出错误结果。

#### 4.3.4 代码实践

**目标**：亲手触发「未标记当前库」的异常，理解它是 fail-fast 设计。

**操作步骤**：

1. 写一份只含普通依赖、**故意不带 `**` 标记**的 `# Dependencies` 段。
2. 调用 `Parse.FromReadme` 解析它。

**需要观察的现象**：观察抛出的异常类型与异常信息文本。

**预期结果（待本地验证）**：抛出 `Exception`，信息为 `Active repository not marked with **[repo]**`。这正是 [Parse.py:153](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Parse.py#L153) 抛出的内容。

#### 4.3.5 小练习与答案

**练习 1**：如果把当前库写成 `**[my_lib**](url)`（`**` 在方括号**外面**），`thisRepo` 会被设置吗？

**答案**：不会。因为 name 是从方括号内部取的，此时 `name = "my_lib"`，不以 `**` 开头；于是这条被当成普通依赖，`thisRepo` 保持 `None`，最终在 [Parse.py:152-153](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Parse.py#L152-L153) 抛出「未标记」异常。正确写法是 `[**my_lib**](url)`。

**练习 2**：为什么「自己」这一条的 version 要被设成字符串 `"None"`，而不是去解析一个版本号？

**答案**：`**` 标记的是「正在被解析的仓库本身」，它对自己没有「最低版本要求」的概念；而且这一条之后会被 `if repo != thisRepo` 排除在最终的 `Dependency` 列表之外（见 [Parse.py:162-165](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Parse.py#L162-L165)），所以 version 字段对它无意义，给一个占位值 `"None"` 即可。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个端到端的小任务。

**实践目标**：编写一份符合 PSI 标准的 `README.md`，用 `Parse.FromReadme` 解析并打印每条依赖；再故意去掉 `**` 标记，观察抛出的异常。

**操作步骤**：

1. 新建一个临时目录，在其中创建 `README.md`，内容如下（注意当前库 `my_lib` 用 `[**my_lib**](url)` 标记）：

   ````markdown
   # General Information
   这是一个演示库。

   # Dependencies

   * DemoProject
     * [**my_lib**](https://github.com/foo/my_lib.git)
     * [dep_a](https://github.com/foo/dep_a.git)(1.2.0)
     * sub
       * [dep_b](https://github.com/foo/dep_b.git)(0.9.0 or higher)

   # Description
   这里是后续段落，解析器遇到本标题即停止。
   ````

2. 写一个最小驱动脚本，体验 u1-l3 介绍的「库式调用」（本包作为库被导入，而非命令行直接运行）：

   ```python
   # 示例代码：需先 pip3 install 本包归档，或把仓库根目录作为包目录导入
   import PsiFpgaLibDependencies as PFD

   deps = PFD.Parse.FromReadme("README.md")
   for d in deps:
       print(d.libraryName, "|", d.url, "|", d.relativePath, "|", d.minVersion)
   ```

3. 运行脚本，记录输出。
4. 把 `README.md` 里 `[**my_lib**](...)` 的两个 `**` 删掉（变成 `[my_lib](...)`），再次运行脚本。

**需要观察的现象**：

- 第 3 步应打印出**除 `my_lib` 之外**的依赖（`my_lib` 是 thisRepo，被排除）。
- 第 4 步应抛出异常，信息为 `Active repository not marked with **[repo]**`。
- 同时注意：`# General Information`、`# Description` 两段的内容都被忽略，印证了 4.2 的段落定位。

**预期结果（待本地验证）**：

- 第 3 步打印两行（顺序与 README 中出现顺序一致）：

  ```text
  dep_a | https://github.com/foo/dep_a.git | ../../DemoProject/dep_a | 1.2.0
  dep_b | https://github.com/foo/dep_b.git | ../../DemoProject/sub/dep_b | 0.9.0
  ```

  其中 `relativePath` 里的 `../../` 前缀来自 u2-l5 的 `levelsToRoot` 计算（`my_lib` 在 `DemoProject` 这一层、距 `ROOT` 两级），本讲只需观察到「路径被换算出来了」即可，原理留给下一讲。

- 第 4 步抛出 `Exception: Active repository not marked with **[repo]**`。

> 若你尚未安装本包，可参考 [u1-l2](u2-l1-dependency-model.md) 的安装方式；导入细节参见 [u1-l3 包入口与客户端集成方式](u1-l3-entry-integration.md)。

## 6. 本讲小结

- PSI 标准 README 用一个独立的 `# Dependencies` 段落、bullet 列表、缩进表示文件夹层级，每条依赖写成 `[name](url)(minVersion)`。
- `Parse.FromReadme` 先读整个文件，再**逐行**扫描：用「去空格 + 小写 + 子串 `#dependencies`」定位起点，用「原始行以 `#` 开头」定位终点（下一个标题）。
- 每条库依赖的 name/url/version 由三条正则切出：name 取方括号内容并去反斜杠，url 取**第一个**圆括号组（强制存在），version 取「圆括号里以数字开头」的一段。
- `[**name**](url)` 标记「当前库」（thisRepo）：`**` 必须在方括号内部；`name.startswith("**")` 为真时把 version 设为 `"None"`，并把这条记录为 `thisRepo`。
- thisRepo 是相对路径换算的**坐标原点**，也是后续从结果里排除「自己」的依据；若整段没有 `**` 标记，`FromReadme` 会抛 `Active repository not marked with **[repo]**`。
- 本包自己的 README（`Dependencies: None`）没有 `**` 标记，无法被 `FromReadme` 解析，所以实践中需要自行构造一份带标记的 README。

## 7. 下一步学习建议

- 下一讲 [u2-l4 基于缩进的文件夹树构建](u2-l4-folder-tree.md)：本讲把 bullet 行分成了「库依赖」和「文件夹」两类，但**没有**讲文件夹是如何根据 `indent` 组装成树的。u2-l4 会精读 `Folder`/`Repo` 内部类与 `GetParentByChildIndent`。
- 之后 [u2-l5 相对路径解析与依赖列表组装](u2-l5-path-resolution.md)：讲解本讲末尾出现的 `levelsToRoot`、`../../` 前缀（`pathPrefix`）和「排除 thisRepo 自身」的最终组装逻辑。
- 想验证本讲行为，可直接阅读 [Parse.py](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Parse.py) 的 `FromReadme`（[L68-L167](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Parse.py#L68-L167)），对照本讲的「起点—终点—识别 thisRepo」三段走一遍。
