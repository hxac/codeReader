# 项目定位：PSI FPGA 库依赖管理工具

> 本讲是《PsiFpgaLibDependencies 学习手册》的第一篇，面向第一次接触这个项目的读者。
> 本讲不要求你写过一行这个项目的代码，只要求你能在读完之后回答一个问题：
> **这个项目到底解决什么问题、它怎么工作、它对外提供哪三类动作。**

---

## 1. 本讲目标

读完本讲，你应当能够：

1. 用一句话说清楚 **PsiFpgaLibDependencies** 解决的依赖管理问题是什么。
2. 识别 PSI FPGA 库 README 中 **依赖声明的标准格式**（`# Dependencies` 段、缩进表示文件夹层级、`[name](url)(version)` 表示一条依赖、`**[lib]**` 标记当前库）。
3. 说出依赖可以被执行的三类动作：**列出（list）**、**检查（check）**、**检出（checkout）**，并知道它们各自的用途。
4. 知道这个项目目前的版本号、维护者与基本发布形式。

本讲是后续所有讲义的地基。后续讲义会逐层深入到解析器、数据模型和动作实现，但前提是你先在脑海中建立起上面这张「全局地图」。

---

## 2. 前置知识

本讲几乎不要求技术背景，但下面几个概念能帮你更快理解：

- **依赖（dependency）**：一个项目 A 在运行或构建时需要另一个项目 B，就说 A 依赖 B。在 FPGA 库的世界里，一个库经常依赖好几个其他库。
- **Git 仓库 / 子模块（submodule）**：依赖通常是一个个独立的 Git 仓库。你可以把它们直接 clone 下来，也可以用 `git submodule add` 把它们「挂」到主仓库里，后者称为子模块。
- **语义化版本号（semver）**：形如 `2.1.0`，三段分别是 `主版本.次版本.修订号`（major.minor.bugfix）。它用来标识一个库「到了哪个版本」。
- **README.md**：项目根目录的说明文件，Markdown 格式。PSI 约定在 README 里用一段固定的写法来声明依赖，本项目的作用就是自动读懂这段写法。

如果你对 Git 和 Markdown 完全陌生也没关系，本讲会用通俗语言解释所有术语。

---

## 3. 本讲源码地图

本讲主要阅读文档与配置类文件，理解「项目是什么」。涉及到的文件如下：

| 文件 | 作用 | 本讲用它来 |
| --- | --- | --- |
| `README.md` | 项目说明文档，含定位、安装、打包说明 | 理解项目定位与 PSI 依赖声明格式 |
| `Changelog.md` | 版本变更记录 | 了解当前版本号与新增特性 |
| `Actions.py` | 三个动作与命令行入口的实现（补充阅读） | 直观确认三类动作到底是什么 |
| `Parse.py` | README 解析器（补充阅读） | 看到一段真实的 PSI 依赖声明示例 |

> 说明：本讲规格里列出的关键源码是 `README.md` 与 `Changelog.md`。为了把「三类动作」讲准确，本讲会补充引用 `Actions.py`；为了让你看到一段真实的依赖声明示例，会补充引用 `Parse.py` 里的文档字符串。这些都是真实存在的文件。

永久链接 base（本讲所有链接都基于当前 HEAD `d78d525`）：

```
https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/
```

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **项目描述与定位** —— 这个项目是什么、解决什么问题。
2. **依赖声明格式** —— PSI README 里依赖该怎么写。
3. **三类动作** —— 读懂依赖之后，能对依赖做什么。

### 4.1 项目描述与定位

#### 4.1.1 概念说明

PSI（Paul Scherrer Institute，瑞士保罗谢勒研究所）维护着一组 FPGA 库（例如 `psi_common` 等）。这些库彼此之间存在依赖关系：库 A 可能需要库 B 和库 C 才能综合、仿真。

如果完全靠人来管理，很快会遇到这些问题：

- 「这个库到底依赖哪些库？」要去翻文档，容易漏。
- 「我本地有没有把所有依赖都拉下来？」要一个个目录去看。
- 「我拉的依赖版本对不对？」要手动比对 tag。

**PsiFpgaLibDependencies** 就是用来解决这些问题的。它的核心思路是：

> **让每个库在自己的 README.md 里，用一段统一格式的「依赖声明」来描述自己依赖了谁；再用一个工具自动读懂这段声明，并执行列出 / 检查 / 检出动作。**

注意一个关键点：**这个项目本身没有依赖**。打开它自己的 README，依赖段写的就是 `None`：

[README.md:L15-L16](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/README.md#L15-L16) —— 本包自身的 `## Dependencies` 段声明为 `None`，说明工具自身不依赖其他库。

#### 4.1.2 核心流程

从「问题」到「解决方案」，可以用下面这条极简流程表示：

```text
[各 FPGA 库的 README.md（标准依赖声明）]
              │
              ▼
   PsiFpgaLibDependencies（解析 + 动作）
              │
   ┌──────────┼──────────────┐
   ▼          ▼              ▼
 list      check         checkout
（列出）  （检查是否存在）（拉取/检出）
```

也就是说，工具的输入是「符合 PSI 标准的 README」，输出是「对依赖执行某种动作」。

#### 4.1.3 源码精读

项目的定位写在 README 的 `# Description` 段：

[README.md:L25-L35](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/README.md#L25-L35) —— 这一段用一句话概括了项目能力：「解析标准 README.md 中的 PSI 库依赖，然后对依赖执行列出、检出或作为子模块添加」。

其中最关键的一句是第 26 行：

```text
This package allows parsing PSI library dependencies from the standard README.md files.
Dependencies can then be listed, checked out or added as submodule to a project.
```

这一句已经把「输入（README）」「能力（解析）」「输出（列出 / 检出 / 子模块）」三件事都说清楚了。

要了解项目当前处于哪个版本、最近加了什么特性，看 Changelog：

[Changelog.md:L1-L3](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Changelog.md#L1-L3) —— 当前版本 `2.1.0`，新增特性是「为已经检出的依赖做语义版本号检查」。

[Changelog.md:L5-L10](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Changelog.md#L5-L10) —— `2.0.0` 为开源重命名了库（让名字专属于 FPGA 库），`1.0.0` 是首次发布。

> 小结：项目定位 = 「解析 PSI 标准 README 中的依赖声明，并执行列出 / 检查 / 检出动作」。当前版本 2.1.0。

#### 4.1.4 代码实践

**实践目标**：用你自己的话复述项目定位，并确认当前版本。

**操作步骤**：

1. 打开 [README.md:L25-L35](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/README.md#L25-L35)，找到 `# Description` 段。
2. 打开 [Changelog.md:L1-L3](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Changelog.md#L1-L3)，确认当前版本号。
3. 用一句话写下：项目要解决的依赖管理问题是什么。

**需要观察的现象**：你会看到 README 把能力拆成「解析」+「三种结果动作」，而 Changelog 标明了版本。

**预期结果**：你写下的句子应当包含三个关键词 ——「README 依赖声明」「解析」「列出 / 检查 / 检出」。版本号应为 `2.1.0`。

> 本实践为纯阅读型实践，不需要运行任何命令。

#### 4.1.5 小练习与答案

**练习 1**：本包自己有没有依赖？从哪里能看出来？
**答案**：没有。在 [README.md:L15-L16](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/README.md#L15-L16)，本包的 `## Dependencies` 段写的是 `None`。

**练习 2**：`2.1.0` 相对之前最大的变化是什么？
**答案**：根据 [Changelog.md:L1-L3](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Changelog.md#L1-L3)，新增了对「已检出依赖」的语义版本号检查（这一特性会在进阶讲义 u3-l3 中深入讲解）。

---

### 4.2 依赖声明格式

#### 4.2.1 概念说明

工具要能「读懂」依赖，前提是每个库的 README 都按同一套格式来写。这套格式就叫 **PSI 标准 README 依赖声明**。

README 里用四条规则总结了这套标准：

[README.md:L28-L33](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/README.md#L28-L33) —— PSI README 必须满足的四条格式要求。

把这四条规则翻译成大白话：

| 规则（README 原文） | 含义 |
| --- | --- |
| Separate section **# Dependencies** | README 里必须有一个独立的 `# Dependencies` 段 |
| dependencies are listed in a bullet list | 段内用一个 bullet 列表（每行以 `*` 开头）罗列依赖 |
| Add indent for every folder level | 用缩进表示文件夹层级：多缩进一级 = 深一层文件夹 |
| Add submodules in the form `[name](url)(1.0.0)` | 一条子模块依赖写成 `库名(url)(最低版本)` 的形式 |

一条具体的依赖长这样：

```text
[name](url)(1.0.0)
```

它的三个字段含义是：

| 片段 | 字段 | 含义 |
| --- | --- | --- |
| `name` | libraryName | 依赖库的名字（也是 clone 时的目标目录名） |
| `url` | url | 远程 Git 仓库地址 |
| `1.0.0` | minVersion | 所需的最低语义版本号 |

还有一个特殊标记：**当前库**（也就是「这段依赖声明是属于谁的」）要用粗体方括号 `**[库名]**` 来标注。解析器会以这个库为「参照原点」，把其他依赖的相对路径都换算到它身上。

#### 4.2.2 核心流程

一段依赖声明从「文本」到「被理解」的过程：

```text
# Dependencies        ← 1. 工具先定位这一段
* A folder             ← 2. 普通文本 + bullet = 一个「文件夹」
  * Subfolder          ←    缩进更深 = 子文件夹
    * [some_lib] (url) (1.0.0)   ← 3. [名](url)(版本) = 一条依赖
    * [other_lib] (url) (1.2.3)  ←    另一条依赖
  * OtherFolder
    * [**this_lib**]   ← 4. **[名]** = 当前库（参照原点）
```

判断一行是「文件夹」还是「依赖」，靠的是行内有没有方括号 `[`：

- 有 `[`：是一条依赖（或当前库标记）。
- 没有 `[`：是一个文件夹的名字。

缩进的多少（行首 `*` 出现的位置）则决定它挂在哪一层文件夹下面。这些细节会在进阶讲义 u2-l3、u2-l4 中逐行讲解，本讲你只需要记住「缩进 = 文件夹层级」即可。

#### 4.2.3 源码精读

README 第 32 行给出了依赖的标准写法：

[README.md:L32-L32](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/README.md#L32) —— 子模块依赖的形式 `**\[name\]\(url\)(1.0.0)**`（README 用反斜杠转义了 Markdown 符号，实际写出来就是 `[name](url)(1.0.0)`）。

一个真实的、完整的依赖声明示例，出现在解析器的文档字符串里：

[Parse.py:L68-L90](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Parse.py#L68-L90) —— `Parse.FromReadme` 的文档字符串，给出了 PSI 依赖声明的标准示例（见其中的 `Example` 部分）。

其中示例片段如下（取自 [Parse.py:L79-L86](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Parse.py#L79-L86)）：

```markdown
# Dependencies

* A folder
  * Subfolder
    * [some_lib] (url) (1.0.0 or higher)
    * [other_lib] (url) (1.2.3)
  * OtherFolder
    * [**this_lib**]
```

读这段示例时注意三点：

1. 顶层 `* A folder` 是一个文件夹。
2. 缩进更深的 `* [some_lib] (url) (1.0.0 or higher)` 是一条依赖，最低版本是 `1.0.0`。
3. `* [**this_lib**]` 用 `**` 标出，表示「这段声明属于 `this_lib` 这个库」。如果整个依赖段里找不到任何 `**[...]**`，解析器会抛出异常：

[Parse.py:L152-L153](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Parse.py#L152-L153) —— 没有标记当前库时，抛出 `Active repository not marked with **[repo]**`。

> 也就是说，`**[lib]**` 不是装饰，而是解析器必须找到的「原点」。

#### 4.2.4 代码实践

**实践目标**（本讲的主实践）：手写一段符合 PSI 标准的依赖声明，并逐字段说明含义。

**操作步骤**：

1. 新建一个文本文件 `my_deps.md`，写入下面的内容（你可以把 `url` 换成任意一个 Git 地址）：

   ```markdown
   # Dependencies

   * Ch XX
     * [my_helper] (https://example.com/my_helper.git) (1.0.0)
     * [**my_project**]
   ```

2. 逐字段标注含义。参考下面的对照表填写：

   | 片段 | 它代表什么 |
   | --- | --- |
   | `# Dependencies` | 依赖段开始，解析器据此定位 |
   | `* Ch XX` | 一个名为 `Ch XX` 的文件夹（bullet + 普通文本，无方括号） |
   | `* [my_helper] (...) (1.0.0)` | 一条子模块依赖：库名 `my_helper`、url、最低版本 `1.0.0` |
   | `* [**my_project**]` | 当前库（参照原点），用 `**` 标记 |

3. **（可选拓展）验证格式是否合法**：在第 5 讲（u2-l3）你会学到用 `Parse.FromReadme` 解析文件。如果你想现在就验证，可以写两行 Python：

   ```python
   # 示例代码：验证你写的依赖声明能否被解析（依赖后续讲义的内容）
   import PsiFpgaLibDependencies as P
   deps = P.Parse.FromReadme("my_deps.md")
   for d in deps:
       print(d.libraryName, d.url, d.minVersion, d.relativePath)
   ```

   如果你能看到 `my_helper` 被打印出来，说明你的格式写对了。

**需要观察的现象**：

- 缺少 `# Dependencies` 段时，解析器读不到任何依赖。
- 去掉 `**`（把 `[**my_project**]` 改成 `[my_project]`）后，解析器找不到当前库，会抛异常。

**预期结果**：你写出的片段至少包含「一个文件夹 + 一条 `[name](url)(version)` 依赖 + 一个 `**[lib]**` 当前库标记」，并能口头说出每个字段的含义。
如果运行了可选拓展代码，预期打印出一行 `my_helper` 及其版本与相对路径。

> 如果你暂时无法安装本包运行拓展代码，请明确标注「待本地验证」 —— 格式是否正确可以通过对照 [Parse.py:L79-L86](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Parse.py#L79-L86) 的示例来判断。

#### 4.2.5 小练习与答案

**练习 1**：下面两行，哪一行是「文件夹」，哪一行是「依赖」？为什么？
```text
* DataPath
* [fifo] (url) (2.0.0)
```
**答案**：第一行 `* DataPath` 是文件夹（bullet + 普通文本，没有方括号）；第二行 `* [fifo] (url) (2.0.0)` 是依赖（含方括号 `[`，符合 `[name](url)(version)` 形式）。

**练习 2**：如果一段依赖声明里完全没有 `**[...]**` 标记，会发生什么？
**答案**：解析器找不到「当前库」原点，会抛出 `Active repository not marked with **[repo]**` 异常（见 [Parse.py:L152-L153](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Parse.py#L152-L153)）。

**练习 3**：依赖写法 `[name](url)(1.0.0)` 里的三个括号段分别对应数据模型的哪几个字段？
**答案**：`name` → `libraryName`；`url` → `url`；`1.0.0` → `minVersion`（数据模型 `Dependency` 会在 u2-l1 详讲）。

---

### 4.3 三类动作：列出、检查、检出

#### 4.3.1 概念说明

解析器读懂依赖声明后，得到的是「一串依赖」。光有列表还不够，用户真正想做的是三件事，对应 **三类动作**：

| 动作 | 英文 | 回答的问题 |
| --- | --- | --- |
| 列出 | list | 「我这个库到底依赖了哪些库？」 |
| 检查 | check | 「我本地把依赖都准备好了吗？版本对吗？」 |
| 检出 | checkout | 「帮我把缺的依赖拉下来。」 |

这三类动作正好对应命令行的三个开关：`-list`、`-check`、`-checkout`。

#### 4.3.2 核心流程

三类动作的关系与分工：

```text
          依赖列表（来自 README 解析）
                    │
      ┌─────────────┼──────────────┐
      ▼             ▼              ▼
   -list         -check        -checkout
      │             │              │
  打印每条      切到 rootdir，     对每条依赖：
  依赖的        逐条看目录         · 已存在 → 跳过并检查版本
  名/url/版本    是否存在          · 不存在 → git clone
                               （或 git submodule add）
```

- **列出**：最轻量，只打印，不做任何文件操作。
- **检查**：会切换工作目录到 `rootdir`，逐个判断依赖目录是否存在；存在则进一步做版本兼容性检查，不存在则报 `ERROR`。
- **检出**：会真正执行 `git clone`（或 `git submodule add`），把依赖拉到正确的相对路径下。

#### 4.3.3 源码精读

三个动作的实现都在 `Actions.py`。

**列出动作** —— 遍历依赖列表，按「库名 - url - 最低版本」格式逐行打印：

[Actions.py:L66-L72](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L66-L72) —— `ListDependencies`，对应 `-list`。关键一行是：

```python
print("{} - {} - {}".format(dep.libraryName, dep.url, dep.minVersion))
```

**检查动作** —— 切换到 `rootdir`，逐条判断依赖目录是否存在，存在则进一步做版本兼容性检查，不存在则打印 `ERROR`：

[Actions.py:L74-L91](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L74-L91) —— `CheckDependency`，对应 `-check`。关键判断是：

```python
if os.path.isdir(depPathAbs):
    CheckCompatibility(rootdir, dep, True)      # 存在 → 查版本
else:
    print("ERROR: Dependency {} does not exist".format(dep.relativePath))
```

**检出动作** —— 对每条依赖：已存在则跳过并查版本，不存在则创建父目录并 `git clone`（或作为子模块添加）：

[Actions.py:L93-L133](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L93-L133) —— `Checkout`，对应 `-checkout`。关键分支是：

```python
if os.path.exists(dep.relativePath):
    print("> skipped, already exists, checking version")
    CheckCompatibility(rootdir, dep, True)
else:
    ...
    os.system("git clone --recurse-submodules {} {}".format(url, dep.libraryName))
```

最后，命令行入口 `ExecMain` 把 `-list / -check / -checkout` 三个开关分别分发到上面三个函数：

[Actions.py:L151-L170](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L151-L170) —— `ExecMain` 中根据 `args.list / args.check / args.checkout` 调用对应动作。

> 注意：本讲只让你「看清三类动作分别做什么」，不要求理解每行实现。`CheckCompatibility`、`CHECKOUT_MODE`、URL 替换等细节会在进阶讲义（u3-l1 ~ u3-l4）展开。

#### 4.3.4 代码实践

**实践目标**：把命令行开关和它实际触发的动作对上号。

**操作步骤**：

1. 打开 [Actions.py:L151-L170](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L151-L170)。
2. 在纸上画一张映射表，把左列填满：

   | 命令行开关 | 调用的函数 | 动作含义 |
   | --- | --- | --- |
   | `-list` | `ListDependencies` | 打印依赖列表 |
   | `-check` | ? | ? |
   | `-checkout` | ? | ? |

3. 再打开三个函数的实现（[L66-L72](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L66-L72)、[L74-L91](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L74-L91)、[L93-L133](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L93-L133)），核对你的描述是否准确。

**需要观察的现象**：你会看到 `-check` 里有「目录不存在 → ERROR」的逻辑，`-checkout` 里有「已存在 → 跳过」的逻辑，二者都顺带调用了 `CheckCompatibility` 做版本检查。

**预期结果**：你的映射表应能准确说出 `-check` 对应 `CheckDependency`（检查依赖是否存在并校验版本），`-checkout` 对应 `Checkout`（拉取缺失的依赖）。

> 本实践为源码阅读型实践，不运行命令。关于「为什么 check 和 checkout 都会顺带做版本检查」，答案在 Changelog 2.1.0 的新特性里（[Changelog.md:L1-L3](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Changelog.md#L1-L3)），进阶讲义会展开。

#### 4.3.5 小练习与答案

**练习 1**：哪个动作**不会**对磁盘做任何写入或拉取操作？
**答案**：`-list`（`ListDependencies`）。它只调用 `print` 打印信息（见 [Actions.py:L66-L72](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L66-L72)）。

**练习 2**：当某个依赖目录已经存在时，`-checkout` 会怎么做？
**答案**：不会重复 clone，而是打印 `> skipped, already exists, checking version` 并调用 `CheckCompatibility` 检查版本（见 [Actions.py:L108-L110](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L108-L110)）。

**练习 3**：`-check` 发现某依赖目录不存在时，会打印什么？
**答案**：`ERROR: Dependency <relativePath> does not exist`（见 [Actions.py:L89-L89](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L89-L89)）。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个综合任务。

**任务背景**：假设你要为一个名为 `my_fpga_lib` 的 FPGA 库写 README，并预测工具会怎么处理它。

**步骤**：

1. **写依赖声明**。新建 `my_fpga_lib/README.md`，写一段符合 PSI 标准的依赖段，要求：
   - 至少包含两层文件夹（例如 `BSP` 和 `BSP/Ch XX`）。
   - 至少包含两条 `[name](url)(version)` 形式的依赖，版本号不同。
   - 用 `**[my_fpga_lib]**` 标出当前库。

   示例骨架（请你自己补全 url）：

   ```markdown
   # Dependencies

   * BSP
     * Ch XX
       * [lib_a] (url_a) (1.0.0)
       * [lib_b] (url_b) (2.3.1)
     * [**my_fpga_lib**]
   ```

2. **逐字段说明**。为 `lib_a` 这条依赖，写出它的 `libraryName`、`url`、`minVersion` 各是什么。

3. **预测三类动作的输出**。假设 `rootdir` 指向 `my_fpga_lib` 所在目录，且本地**还没有**拉取任何依赖，请预测：
   - 执行 `-list` 会打印几行？每行长什么样？
   - 执行 `-check` 会打印什么？（提示：依赖目录都不存在）
   - 执行 `-checkout` 会触发几次 `git clone`？

4. **（可选）核对**。如果环境允许，安装本包后写一小段脚本调用 `Parse.FromReadme("my_fpga_lib/README.md")` 得到依赖列表，再调用 `Actions.ListDependencies(deps)`，对比你第 3 步的预测是否一致。无法运行请标注「待本地验证」。

**预期结果**：

- `-list` 应打印两行（`lib_a` 和 `lib_b`），每行格式为 `库名 - url - 版本`。
- `-check` 应对每个依赖打印一行 `ERROR: Dependency ... does not exist`（因为还没检出）。
- `-checkout` 应触发两次 `git clone`（`lib_a`、`lib_b` 各一次）。

这个综合练习把「格式 → 解析 → 三类动作」整条链路走了一遍，是后续深入源码前的最佳热身。

---

## 6. 本讲小结

- **项目定位**：PsiFpgaLibDependencies 解析 PSI 标准 README 中的依赖声明，并对依赖执行 list / check / checkout 三类动作，解决多 FPGA 库之间的依赖管理问题（[README.md:L25-L35](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/README.md#L25-L35)）。
- **依赖声明格式**：必须有 `# Dependencies` 段；bullet 列表表示依赖；缩进表示文件夹层级；依赖写作 `[name](url)(version)`；当前库用 `**[lib]**` 标记（[README.md:L28-L33](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/README.md#L28-L33)，示例见 [Parse.py:L79-L86](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Parse.py#L79-L86)）。
- **三类动作**：`-list` 只打印；`-check` 检查依赖是否存在并校验版本；`-checkout` 拉取缺失的依赖（分别见 [Actions.py:L66-L72](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L66-L72)、[L74-L91](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L74-L91)、[L93-L133](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Actions.py#L93-L133)）。
- **当前版本**：`2.1.0`，核心新特性是「为已检出依赖做语义版本检查」（[Changelog.md:L1-L3](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/Changelog.md#L1-L3)）。
- **本包自身无依赖**（[README.md:L15-L16](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/README.md#L15-L16)）。

---

## 7. 下一步学习建议

本讲建立了「全局地图」，接下来建议按下面的顺序继续：

1. **u1-l2（安装、打包与目录结构）**：学会用 `pip3 install` 安装本包、用 `setup.py` 打包，理清仓库里每个文件的职责。这是动手之前的基本功。
2. **u1-l3（包入口与客户端集成方式）**：理解本包「没有独立 main、作为库被客户端调用」的集成模式，搞清楚 `__init__.py` 导出了什么、`ExecMain` 在哪里。
3. 之后再进入第二单元（u2），从 `Dependency`、`VersionNr` 数据模型出发，深入 `Parse` 解析器的实现细节。

> 推荐先读：[README.md:L37-L42](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/README.md#L37-L42)（安装方式）和 [setup.py](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies/blob/d78d525150281c6bef3fd0dec5848baac4b62af5/setup.py)，为下一讲做准备。
