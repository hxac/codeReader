# 项目定位与目录结构：为什么 scipy.misc 看起来几乎为空

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清楚 `scipy.misc` 在当前 HEAD（`de190e7fde`）下的**真实面貌**：它已经不是一个功能模块，而是一个只剩下「弃用桩（deprecation stub）」的占位模块。
- 列出 `scipy/misc/` 目录下的**全部真实文件**，并解释每一个文件的作用。
- 解释为什么这个模块「看起来几乎为空」，并理解它已被计划在 **SciPy 2.0.0** 被完全移除。
- 独立运行一次 `import scipy.misc`，并能解释为什么导入会触发 `DeprecationWarning`。

> 本讲是整本手册的第一篇，不假设你已经熟悉 SciPy。我们会从「打开目录看到什么」开始。

## 2. 前置知识

在进入源码之前，先建立三个直觉。

### 2.1 什么是「模块（module）」

在 Python 里，一个目录只要包含 `__init__.py`，就可以被当作一个**包（package）**导入。例如 `scipy/misc/` 目录里有 `__init__.py`，于是我们可以写：

```python
import scipy.misc          # 实际执行的就是 scipy/misc/__init__.py 里的代码
```

这意味着：`__init__.py` 里写了什么，`import` 时就会发生什么。本讲的核心发现就是——`scipy/misc/__init__.py` 里现在**只剩下一句「我弃用了」的警告**。

### 2.2 什么是「弃用（deprecation）」

软件库演进时，会逐步淘汰旧的接口（函数、类、子模块）。为了不突然破坏别人的代码，通常会分两步走：

1. **弃用期（deprecated）**：接口还在，但一用就发出 `DeprecationWarning`，提醒你「这个接口以后会消失，请尽快换掉」。
2. **移除（removed）**：经过若干个版本后，把接口真正删除。

`scipy.misc` 当前正处于第 1 步的末尾：内容已经被搬走或删掉，只剩下了「喊一声弃用」的桩文件。

### 2.3 什么是 DeprecationWarning

`DeprecationWarning` 是 Python 标准库 `warnings` 里内置的一种警告类型，专门用来标记「过时的用法」。它和报错（异常）不同——**警告默认不会中断程序**，只是打印一行提示。在本讲的实践环节你会亲眼看到它。

如果你想暂时跳过数学符号细节：本讲几乎不涉及数学，唯一会用到的是把版本号当作时间轴来看（如 `1.10.0 → 2.0.0`），可以把它理解成一条从「开始弃用」到「彻底移除」的进度条。

## 3. 本讲源码地图

我们先从高空俯瞰整个 `scipy/misc/` 目录。当前 HEAD 下，这个目录里**真实存在的文件只有 4 个**（外加我们这本手册自己所在的 `scipy-scipy-misc-tutorial/` 目录）：

| 文件 | 行数 | 作用 |
| --- | --- | --- |
| `scipy/misc/__init__.py` | 6 行 | 包的入口。`import scipy.misc` 时执行，发出「整个模块弃用」的警告。 |
| `scipy/misc/common.py` | 6 行 | 子模块桩。`import scipy.misc.common` 时发出「common 子模块弃用」的警告。 |
| `scipy/misc/doccer.py` | 6 行 | 子模块桩。`import scipy.misc.doccer` 时发出「doccer 子模块弃用」的警告。 |
| `scipy/misc/meson.build` | 10 行 | 构建脚本。告诉构建系统 Meson「把上面三个 `.py` 文件安装到 `scipy/misc/` 目录」。 |

一个关键事实：**这三个 `.py` 文件加起来只有 18 行代码，而且全部只做一件事——发警告**。它们不提供任何函数、类或数据。这就是「看起来几乎为空」的直接原因。

> 小提示：你可能会问「那 `scipy.misc.face`、`scipy.misc.ascent` 这些著名函数去哪了？」答案是——它们已经在更早的版本里被搬到了 `scipy.datasets`，或者被彻底移除。这个搬迁故事属于后续讲义（见第 7 节）。

## 4. 核心概念与源码讲解

本讲聚焦两个最小模块：**`__init__.py`（及其孪生桩文件）** 与 **`meson.build`（构建条目）**。

### 4.1 `__init__.py`：一个只会喊「我弃用了」的桩文件

#### 4.1.1 概念说明

「桩文件（stub file）」是指：为了**保留一个导入路径**而存在的、几乎不含逻辑的文件。

为什么 `scipy.misc` 还需要保留桩文件，而不是直接整个目录删掉？因为如果直接删除，那么所有还在写 `import scipy.misc` 的旧代码会立刻拿到 `ModuleNotFoundError`——这是一种「硬断裂」。更友好的做法是：**保留空的占位文件，让它一被导入就大喊「我弃用了，将在 2.0.0 移除」**，给使用者一个迁移的缓冲期。

`scipy/misc/__init__.py` 就是这样一个桩文件。它的孪生兄弟 `common.py` 和 `doccer.py` 结构几乎一模一样，只是各自的提示信息里写的是对应子模块的名字。

#### 4.1.2 核心流程

当你在命令行或脚本里写下 `import scipy.misc` 时，发生的事情是：

```text
1. Python 定位到 scipy/misc/__init__.py 并开始执行它
2. 第 1 行：import warnings           ← 引入标准库的警告模块
3. 第 2-6 行：warnings.warn(            ← 主动发出一条警告
       message="scipy.misc is deprecated and will be removed in 2.0.0",
       category=DeprecationWarning,
       stacklevel=2
   )
4. 因为是「警告」而非「异常」，程序不会崩溃
5. Python 按默认过滤规则，决定是否把这条警告打印到屏幕
6. __init__.py 执行完毕，import 成功返回（但 scipy.misc 里啥也没有）
```

这里有一个重要的细节：`warnings.warn` 接收三个关键参数。

- `message`：警告文案，告诉用户「谁弃用了、什么时候移除」。
- `category`：警告类别，这里用 `DeprecationWarning`，这是 Python 专门为「过时用法」准备的类型。
- `stacklevel=2`：**栈层级**。它决定这条警告「算在谁的头上」。`stacklevel=1` 会把警告算到桩文件自己身上（这样提示就指向 `__init__.py` 内部，对用户毫无用处）；`stacklevel=2` 则往上抬一层，算到「谁触发了这次 import」头上，于是提示会指向用户自己写的 `import scipy.misc` 那一行。这正是我们想要的。

关于「程序会不会崩溃」，可以用一句话概括：

> 警告是「喊话」，异常是「拦路」。`warnings.warn` 默认只喊话，不拦路。

#### 4.1.3 源码精读

先看入口桩文件 `scipy/misc/__init__.py` 的全部内容（它本来就只有 6 行）：

[`scipy/misc/__init__.py:1-6`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/misc/__init__.py#L1-L6) —— 这是包的入口，整个文件只做「导入即警告」这一件事：

```python
import warnings
warnings.warn(
    "scipy.misc is deprecated and will be removed in 2.0.0",
    DeprecationWarning,
    stacklevel=2
)
```

- 第 1 行：引入标准库 `warnings`，这是发出警告的唯一依赖。
- 第 2-6 行：调用 `warnings.warn(...)`，文案里写明了「弃用」和「2.0.0 移除」两件最关键的事，并把 `stacklevel` 设为 2，让提示指向调用者。

再看它的两个孪生子模块。`common.py` 与 `doccer.py` 的结构完全相同，**唯一的区别是文案里的模块名**：

[`scipy/misc/common.py:1-6`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/misc/common.py#L1-L6) —— 子模块 `common` 的桩，文案改成 `scipy.misc.common`：

```python
import warnings
warnings.warn(
    "scipy.misc.common is deprecated and will be removed in 2.0.0",
    DeprecationWarning,
    stacklevel=2
)
```

[`scipy/misc/doccer.py:1-6`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/misc/doccer.py#L1-L6) —— 子模块 `doccer` 的桩，文案改成 `scipy.misc.doccer`：

```python
import warnings
warnings.warn(
    "scipy.misc.doccer is deprecated and will be removed in 2.0.0",
    DeprecationWarning,
    stacklevel=2
)
```

> 一个值得思考的问题：为什么 `common` 和 `doccer` 这两个子模块要**各自再喊一次**，而不是只在 `__init__.py` 里喊一次就够了？
>
> 因为用户完全可能**直接**写 `import scipy.misc.common`，这时 Python 不一定会先完整执行 `scipy/misc/__init__.py` 的副作用（取决于导入路径与缓存）。为了让「无论从哪条路径进来都一定收到警告」，最稳妥的做法就是让每个子模块都自带一句 `warnings.warn`。这种「每个文件都重复一份相同模式」的写法，是桩文件阶段的典型特征。

#### 4.1.4 代码实践

**实践目标**：亲手触发并观察 `scipy.misc` 的弃用警告，验证「导入即警告」。

**操作步骤**：

1. 确认你已克隆 scipy 仓库并切到本讲对应的 HEAD：
   ```bash
   git clone https://github.com/scipy/scipy.git
   cd scipy
   git checkout de190e7fde9d3d34400dbfe1eeacc9fc6d29cede
   ```
2. 在已经**安装好**这份 scipy 的环境里（注意：scipy 是编译型扩展包，需要先按官方文档用 Meson 构建安装；如果你用的是 PyPI 上版本号相同的预装 scipy，行为也应一致——待本地验证），运行：
   ```bash
   python -c "import scipy.misc"
   ```
3. 观察终端输出。

**需要观察的现象**：屏幕上应该出现一行类似下面的提示（路径前缀会是你本机的实际安装路径）：

```text
/path/to/scipy/misc/__init__.py:2: DeprecationWarning: scipy.misc is deprecated and will be removed in 2.0.0
  warnings.warn(
```

**预期结果**：程序**不会报错退出**（退出码为 0），但会打印一条 `DeprecationWarning`。这正是「桩文件」的标志——它不提供功能，只提醒你「我快没了」。

> 说明：Python 对 `DeprecationWarning` 有一条默认过滤规则——**默认只在警告由 `__main__`（也就是你直接运行的脚本）触发时才显示**。上面用 `python -c "..."` 运行时，`-c` 的代码就运行在 `__main__` 里，再加上 `stacklevel=2` 把警告归因到调用者，所以这条警告会被显示出来。如果你把 `import scipy.misc` 放进某个被导入的子模块里执行，可能就看不到输出了——这一点会在下一讲（`u1-l2`）详细讨论。

> 待本地验证：警告文案的精确排版（是否换行、是否带文件路径）取决于你的 Python 版本与 `warnings` 模块的渲染方式，但「出现一条 `DeprecationWarning` 且内容提到 2.0.0」这一点是确定的。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `__init__.py` 里的 `stacklevel=2` 改成 `stacklevel=1`，警告提示会指向哪里？为什么？

**参考答案**：`stacklevel=1` 会让警告归因到 `warnings.warn` 调用所在的那一行，也就是 `scipy/misc/__init__.py` 自身。这样的提示对用户毫无帮助，因为用户改不了 scipy 内部代码。`stacklevel=2` 才能把提示抬到「触发导入的代码」上，让用户看到该改的地方。

**练习 2**：`scipy/misc/` 下三个 `.py` 桩文件总共有几个**函数定义**、几个**类定义**？

**参考答案**：都是 **0 个**。三个文件里没有任何 `def` 或 `class`，只有 `import warnings` 和一句 `warnings.warn(...)`。这从代码层面证明了「scipy.misc 已不再是功能模块」。

**练习 3**：为什么作者不直接把 `scipy/misc/` 目录整个删掉，而要留下三个桩文件？

**参考答案**：为了**软着陆**。直接删除会让所有写 `import scipy.misc` 的旧代码立刻 `ModuleNotFoundError` 崩溃；保留桩文件则能在过渡期给出明确的弃用提示和迁移缓冲，等到 2.0.0 再彻底移除。

### 4.2 `meson.build`：为什么空模块也得有构建条目

#### 4.2.1 概念说明

`meson.build` 是构建系统 **Meson** 的配置文件。SciPy 已经从旧的 `setup.py` 迁移到了 Meson（配合 `meson-python`）来构建和打包。

你可能会觉得奇怪：既然 `scipy.misc` 三个文件全是「只会喊弃用」的桩，为什么不连构建条目也一起删掉？答案是：**只要这个目录还想被 `import`，它的文件就必须被「安装」到正确的包路径下**。`meson.build` 干的就是这件事——它告诉 Meson「请把这三个 `.py` 文件，安装到安装目录里的 `scipy/misc/` 子目录下」。

换句话说，桩文件负责「运行时喊话」，而 `meson.build` 负责「安装时把它们摆到正确的货架上」。两者缺一不可：没有桩文件就没有东西可喊；没有 `meson.build`，桩文件压根不会出现在安装包里，`import scipy.misc` 就会失败。

#### 4.2.2 核心流程

`scipy/misc/meson.build` 的执行可以拆成两步：

```text
1. 定义清单 python_sources = ['__init__.py', 'common.py', 'doccer.py']
   → 明确「本目录要打包哪些 Python 源文件」
2. 调用 py3.install_sources(python_sources, subdir: 'scipy/misc')
   → 把清单里的文件，安装到目标包的 scipy/misc/ 子目录
```

这里有两个关键概念：

- **`python_sources`**：一个普通的 Meson 列表，列出本目录所有需要安装的 `.py` 文件。注意它**只列了三个文件**，`meson.build` 自己不需要也不应该出现在这个清单里。
- **`subdir: 'scipy/misc'`**：指明安装到的相对路径。这个路径必须和 `import` 时期望的包路径**完全一致**，否则导入会找不到模块。
- **`py3`**：这是 Meson 提供的「Python 安装辅助对象」，由上层构建配置（根 `meson.build`）注入，专门用来处理 Python 源文件的安装。

一个反直觉但很重要的点：**即使模块已经弃用，只要它还在仓库里、还想被 `import`，构建条目就必须保留**。弃用是「运行时行为」，构建是「打包行为」，两者是独立的。

#### 4.2.3 源码精读

[`scipy/misc/meson.build:1-10`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/misc/meson.build#L1-L10) —— 整个构建文件只有 10 行，先列清单再安装：

```meson
python_sources = [
  '__init__.py',
  'common.py',
  'doccer.py'
]

py3.install_sources(
  python_sources,
  subdir: 'scipy/misc'
)
```

- 第 1-5 行：把三个桩文件登记进 `python_sources` 清单。这一步是「声明」，本身不产生安装动作。
- 第 7-10 行：`py3.install_sources(...)` 才是真正的「安装指令」，`subdir: 'scipy/misc'` 保证文件被放到正确的包子目录。

> 思考实验（不用真做）：假如有人不小心把 `'common.py'` 从 `python_sources` 清单里漏掉了，会发生什么？答案是——构建出来的安装包里**不会包含** `common.py`，于是 `import scipy.misc.common` 会抛 `ModuleNotFoundError`。可见这份看似「没用的」构建文件，其实是整个导入路径能成立的最后一道保障。

#### 4.2.4 代码实践

**实践目标**：理解「漏掉构建清单」的后果，从而体会 `meson.build` 不可省略。

**操作步骤**：

1. 打开 [`scipy/misc/meson.build`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/misc/meson.build)，确认 `python_sources` 里包含三个文件。
2. **在脑海中（或一份本地沙盒副本里，切勿改动仓库源码）** 把 `'doccer.py'` 从清单中划掉。
3. 推理：重新构建并安装后，运行 `python -c "import scipy.misc.doccer"` 会怎样？

**需要观察的现象**：你应当推断出，由于 `doccer.py` 没被安装，导入会失败。

**预期结果**：

```text
ModuleNotFoundError: No module named 'scipy.misc.doccer'
```

**待本地验证**：本实践为「源码阅读型推理」，不要求你真的破坏构建。如果你想验证，请在一个隔离的 git worktree 或副本里操作，**不要修改主仓库的源码**。

#### 4.2.5 小练习与答案

**练习 1**：`meson.build` 自己需要出现在 `python_sources` 清单里吗？为什么？

**参考答案**：**不需要**。`python_sources` 只列需要作为「Python 源文件」安装到包里的 `.py` 文件；`meson.build` 是构建配置文件，由 Meson 在构建期读取，不是 Python 运行时的一部分，不需要也不会被安装到 `scipy/misc/` 包目录。

**练习 2**：如果把 `subdir: 'scipy/misc'` 改成 `subdir: 'scipy/other'`，`import scipy.misc` 还能成功吗？

**参考答案**：**不能**（假设没有其它兜底）。`__init__.py` 会被安装到 `scipy/other/` 而不是 `scipy/misc/`，于是 Python 在 `scipy/misc/` 路径下找不到 `__init__.py`，`import scipy.misc` 就会失败。`subdir` 必须与期望的包路径严格对应。

## 5. 综合实践

把本讲的两个最小模块串起来，完成下面这个「体检任务」。

**任务**：给 `scipy.misc` 做一次完整体检，并用一段 150 字左右的中文说明，解释「为什么这个模块看起来几乎为空」。

**操作步骤**：

1. 列出目录的全部文件（应当正好是 4 个）：
   ```bash
   ls -A scipy/misc
   ```
   预期看到：`__init__.py  common.py  doccer.py  meson.build`（外加本手册目录，与本任务无关）。
2. 统计三个 `.py` 文件各自有多少行、是否包含 `def`/`class`（预期：各 6 行，0 个函数/类）。
3. 在已安装好该版本 scipy 的环境里运行，并记录警告：
   ```bash
   python -c "import scipy.misc"
   ```
4. 打开 `scipy/misc/meson.build`，确认 `python_sources` 清单与目录里的 `.py` 文件**一一对应**。
5. 撰写说明，要点应包含：
   - 三个 `.py` 文件都是**桩文件**，只发 `DeprecationWarning`，不含任何功能；
   - `meson.build` 只是保证这些桩文件被安装，让旧的 `import` 路径还能「软着陆」；
   - 模块计划在 **SciPy 2.0.0** 被完全移除，届时连这些桩文件也会消失。

**预期结果**：你会得到一个清晰的结论——`scipy.misc` 之所以「看起来几乎为空」，是因为它**已经完成了从功能模块到弃用占位模块的转变**，真实功能早已搬走，只留下「喊一声弃用」的桩和「把桩摆上货架」的构建条目，等待 2.0.0 的最终删除。

## 6. 本讲小结

- `scipy/misc/` 在当前 HEAD 下**只有 4 个文件**：`__init__.py`、`common.py`、`doccer.py`、`meson.build`。
- 三个 `.py` 文件都是**弃用桩**：结构相同，只调用 `warnings.warn(..., DeprecationWarning, stacklevel=2)` 发出警告，不含任何函数或类。
- `stacklevel=2` 的作用是把警告**归因到调用者**（写 `import` 的人），而不是桩文件自身。
- `meson.build` 通过 `py3.install_sources(python_sources, subdir: 'scipy/misc')` 把桩文件安装到正确路径；**只要目录还想被 import，构建条目就必须保留**。
- 「看起来几乎为空」的根因是：这是一个**正在退役**的模块，真实功能已搬走，只剩软着陆用的占位文件。
- 整个模块计划在 **SciPy 2.0.0** 被完全移除。

## 7. 下一步学习建议

本讲只让你「认清现状」。接下来建议按以下顺序继续：

1. **`u1-l2` 运行方式：导入即触发弃用警告**——学会用 `warnings.catch_warnings(record=True)` 和 `filterwarnings` 精确捕获、转异常或忽略这三条警告，真正掌握 `stacklevel` 在不同调用栈下的表现。
2. **`u1-l3` scipy.misc 的历史职能与退役原因**——用 `git show` 还原它曾经包含的示例数据集（`ascent`/`face`/`electrocardiogram`）、数值工具（`derivative`）等内容，理解「杂物箱模块」为何要被拆分。
3. 之后再进入进阶层（`u2`）精读弃用机制的实现细节，以及专家层（`u3`）的迁移与架构取舍。

> 建议你顺手保存本讲用到的两个永久链接：[`__init__.py`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/misc/__init__.py#L1-L6) 与 [`meson.build`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/misc/meson.build#L1-L10)，后续讲义会反复回到这两个文件。
