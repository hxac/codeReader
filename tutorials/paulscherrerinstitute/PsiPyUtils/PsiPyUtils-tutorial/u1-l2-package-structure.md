# 包结构与导入方式：扁平布局与 __init__ 重导出

## 1. 本讲目标

上一篇（u1-l1）我们在 Changelog 里看到 3.0.0 的一条「破坏性变更」：import 写法从

```python
from PsiPyUtils.FileWriter import FileWriter   # 旧写法
```

变成了

```python
from PsiPyUtils import FileWriter               # 新写法
```

当时我们留下了一个悬念：**旧写法在 3.0.x 里到底还能不能用？** 这一篇就来回答它。读完本讲，你应该能够：

- 说清楚 PsiPyUtils 的**扁平布局**长什么样，以及它为什么仍然算一个「合法的 Python 包」；
- 解释 `setup.py` 里 `package_dir={"PsiPyUtils": "."}` 和 `packages=["PsiPyUtils"]` 这两行**如何把仓库根目录映射成 `PsiPyUtils` 包**；
- 读懂 [`__init__.py`](__init__.py) 的「统一重导出」逻辑，并指出哪些名字导出的是**类**、哪些导出的是**模块**，二者在使用上的差别；
- 亲手验证**新、旧两种 import 写法都能工作**，并解释 3.0.0 为什么要做这次改动；
- 对比 **pip 包**与 **git submodule** 两种使用方式各自的特点。

本讲只读两个文件：[`__init__.py`](__init__.py) 与 [`setup.py`](setup.py)。它们都不实现业务逻辑，却决定了「这个库怎么被导入、怎么被打包」。

## 2. 前置知识

阅读本讲前，建议你具备以下基础概念（都很通俗）：

- **什么是 Python 包**：一个目录，里面有一堆 `.py` 模块文件，外加一个 `__init__.py`，就可以被当作一个整体导入。`__init__.py` 是这个包的「门面」，导入包时它最先被执行。
- **相对导入 `from . import X`**：包内部模块之间的导入写法。点号 `.` 表示「当前包」，所以 `from .FileWriter import FileWriter` 的意思是「从当前包的 `FileWriter` 模块里导入 `FileWriter` 这个名字」。
- **`sys.path` 与导入路径**：Python 找模块时，会去 `sys.path` 列出的目录里翻。`pip install` 装包，本质上就是把包放到一个已在 `sys.path` 里的目录（如 `site-packages/`）。
- **`setuptools` 与 `package_dir`**：`setup.py` 里 `package_dir={"包名": "目录"}` 告诉打包工具「这个包的源码在哪个目录」。这是本讲的关键。
- **git submodule**：把另一个 git 仓库「嵌」进当前仓库的某个子目录里使用。PsiPyUtils 历史上就是这样被引用的。

> 如果你已经熟悉上面这些，可以直接跳到第 4 节。前置概念不清楚没关系，下面会结合真实代码边讲边对照。

## 3. 本讲源码地图

本讲只涉及两个文件，它们共同回答「**为什么根目录里散落的 8 个 `.py` 文件，能被当作一个名叫 `PsiPyUtils` 的包来导入**」：

| 文件 | 行数 | 作用 | 本讲用来讲什么 |
| --- | --- | --- | --- |
| [`__init__.py`](__init__.py) | 14 行（含版权头） | 包的门面：把 8 个子模块里的名字统一重导出到包顶层 | 新旧两种 import 写法为什么都成立、类与模块两种重导出的差别 |
| [`setup.py`](setup.py) | 38 行 | 打包脚本：把仓库根目录映射成 `PsiPyUtils` 包 | 扁平布局如何成为「合法包」、`package_dir`/`packages` 的作用 |

根目录其余 8 个功能模块（`FileWriter.py`、`TempWorkDir.py`、`ExtAppCall.py`、`EnvVariables.py`、`FileOperations.py`、`TempFile.py`、`TextReplace.py`、`XmlToolbox.py`）是后续讲义的主题，本讲只在「它们怎么被导入」这一层面上提到它们，不展开实现。

## 4. 核心概念与源码讲解

本讲按两个最小模块组织：**4.1 扁平布局如何变成一个合法包（`setup.py` 的 `package_dir`/`packages`）**，**4.2 `__init__.py` 的统一重导出与新旧 import 写法**。

### 4.1 扁平布局如何变成一个合法包：package_dir 与 packages

#### 4.1.1 概念说明

很多 Python 项目把源码放在一个与包同名的子目录里，例如：

```
my_project/
├── setup.py
└── my_package/          ← 与包同名的子目录
    ├── __init__.py
    ├── module_a.py
    └── module_b.py
```

这种「**包目录 = 同名子目录**」的布局，让 `setuptools` 默认就能找到包内容，不需要额外说明。

PsiPyUtils **没有**这样的子目录。它的所有模块文件**直接摊在仓库根目录**：

```
PsiPyUtils/  (仓库根)
├── __init__.py
├── setup.py
├── FileWriter.py        ┐
├── TempWorkDir.py       │
├── ExtAppCall.py        │
├── EnvVariables.py      │  这 8 个 .py 就是包的内容
├── FileOperations.py    │
├── TempFile.py          │
├── TextReplace.py       │
├── XmlToolbox.py        ┘
└── Tests/
```

这种叫**扁平布局（flat layout）**。问题来了：仓库根目录的名字并不叫 `PsiPyUtils`（它是你 clone 时自己起的，比如 `paulscherrerinstitute-PsiPyUtils`），那 `FileWriter.py` 凭什么变成 `PsiPyUtils.FileWriter`？

答案就在 `setup.py` 的 `package_dir`：它**显式地把「当前目录 `.`」映射成包 `PsiPyUtils`**。一旦这么映射，根目录里的每个 `.py` 文件就自动成为 `PsiPyUtils` 包的一个子模块。

#### 4.1.2 核心流程

把一个扁平布局的目录变成可分发、可导入的包，流程是：

1. **声明包名与源码位置**：`setup.py` 里写 `package_dir={"PsiPyUtils": "."}`——把仓库当前目录 `.` 当作包 `PsiPyUtils` 的根。
2. **列出要打包的包**：`packages=["PsiPyUtils"]`——告诉 `setuptools` 要发布 `PsiPyUtils` 这一个包（注意：只列了顶层包，`Tests/` 不在其中，见下方说明）。
3. **执行打包**：`python3 setup.py sdist`，`setuptools` 据此把根目录的 `.py` 文件（连同 `__init__.py`）收进 `dist/PsiPyUtils-<version>.tar.gz`。
4. **安装**：`pip install` 这个 `.tar.gz` 后，包被放进 `site-packages/PsiPyUtils/`，于是 `from PsiPyUtils import ...` 在任何项目里都能用。

可以用一张「映射对照表」把扁平布局下「仓库里的文件」和「安装后的导入路径」对应起来：

| 仓库根目录里的文件 | 安装后的模块全名 |
| --- | --- |
| `__init__.py` | `PsiPyUtils`（包本身） |
| `FileWriter.py` | `PsiPyUtils.FileWriter` |
| `TempWorkDir.py` | `PsiPyUtils.TempWorkDir` |
| `ExtAppCall.py` | `PsiPyUtils.ExtAppCall` |
| `EnvVariables.py` | `PsiPyUtils.EnvVariables` |
| `FileOperations.py` | `PsiPyUtils.FileOperations` |
| `TempFile.py` | `PsiPyUtils.TempFile` |
| `TextReplace.py` | `PsiPyUtils.TextReplace` |
| `XmlToolbox.py` | `PsiPyUtils.XmlToolbox` |

> 注意：`Tests/` 目录**不在** `packages` 列表里，所以它不会被装进分发包。也就是说，`pip install` 装好后，你的环境里**没有** `PsiPyUtils.Tests`。测试只存在于源码仓库中（下一篇 u1-l3 会讲怎么跑它们）。

#### 4.1.3 源码精读

打包的核心两行就在 `setup.py` 的 `setuptools.setup(...)` 调用里：

> [setup.py:26-27](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/setup.py#L26-L27) —— `package_dir={"PsiPyUtils": "."}` 把当前目录映射为包 `PsiPyUtils` 的源码根；`packages=["PsiPyUtils"]` 声明只发布这一个顶层包。

- **`package_dir={"PsiPyUtils": "."}`** 是整篇讲义的「钥匙」。字典的键是**包名** `PsiPyUtils`，值是**这个包的源码在哪个目录**——`.` 就是仓库根目录。这一行等价于在告诉打包工具：「别去找与包同名的 `PsiPyUtils/` 子目录了，包内容就在当前目录里。」
- **`packages=["PsiPyUtils"]`** 列出要打包的包。这里只有一项，且只到顶层。如果项目还有子包（比如 `PsiPyUtils.utils`），就得在这里追加，或用 `find_packages()` 自动发现。PsiPyUtils 没有子包，所以手写一项即可。

版本号也在这同一个调用里（与 u1-l1 读到的 Changelog 3.0.1 对应）：

> [setup.py:18-27](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/setup.py#L18-L27) —— `setuptools.setup(...)`：包名 `PsiPyUtils`、版本 `3.0.1`，并给出 `package_dir`/`packages` 映射。

打包前的清理逻辑（u1-l1 已讲过，这里只回顾它在流程里的位置）：

> [setup.py:8-15](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/setup.py#L8-L15) —— `CustomSdist`：在标准 `sdist.run()` 之前，先用 `shutil.rmtree` 清掉旧的 `dist/` 和 `PsiPyUtils.egg-info/`，再从上级目录视角重新构建。

> [setup.py:35-37](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/setup.py#L35-L37) —— `cmdclass={"sdist": CustomSdist}` 把上面的自定义命令注册到 `sdist`，这样 `python3 setup.py sdist` 就会先清理、后构建。

还有一个**值得专门留意**的细节，本讲先用结论、深究留到 u5-l2：`setup.py` 声明了运行时依赖 `lxml`——

> [setup.py:28-30](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/setup.py#L28-L30) —— `install_requires=["lxml"]`。

但如果你用 `grep` 检查所有模块的 `import`，会发现**没有任何一个模块真的 `import lxml`**。例如 `XmlToolbox.py` 用的是 Python 标准库的 `xml.etree.ElementTree`：

> [XmlToolbox.py:7-8](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/XmlToolbox.py#L7-L8) —— 只 `import os` 和标准库 `xml.etree.ElementTree as ET`，并不依赖 lxml。

对**本讲**而言，这个事实有一个直接的好处：导入 `PsiPyUtils`（执行 `__init__.py`）**只需要标准库**，即使你机器上没装 lxml，下一节的两种 import 写法也照样能跑。至于「声明了却没用」的依赖是否冗余、是否该删，留到 u5-l2（打包与发布）专门讨论。

#### 4.1.4 代码实践

这是一个**可运行型实践**（需要本机有 Python 3 与 `setuptools`）。

1. **实践目标**：亲手验证 `package_dir` 的映射效果——打包后文件名、安装后能否导入，都由它决定。
2. **操作步骤**：
   - 在仓库根目录执行 `python3 setup.py sdist`。
   - 查看 `dist/` 下生成的文件名（应含 `3.0.1`）。
   - 把生成的 `.tar.gz` 装进一个临时目录（避免污染系统环境），例如：
     ```bash
     pip install --target=/tmp/psitest --no-deps dist/PsiPyUtils-3.0.1.tar.gz
     ```
     （`--no-deps` 是为了跳过 lxml，因为本讲已确认它不是导入所必需的。）
3. **需要观察的现象**：`CustomSdist` 会先清掉旧 `dist/`，再重新构建；安装完成后 `/tmp/psitest/PsiPyUtils/` 里应能看到 `FileWriter.py`、`__init__.py` 等 8 个模块文件，但**没有** `Tests/`。
4. **预期结果**：生成 `dist/PsiPyUtils-3.0.1.tar.gz`；`/tmp/psitest/PsiPyUtils/` 下正好是 8 个模块加 `__init__.py`，不含测试。这印证了 `packages=["PsiPyUtils"]` 只打包顶层包。
5. **如果无法确定运行结果**：若本机不便打包/安装，可直接打开仓库里已提交的 `dist/PsiPyUtils-3.0.1.tar.gz`，用 `tar tzf dist/PsiPyUtils-3.0.1.tar.gz` 查看它收录了哪些文件，同样能验证 `Tests/` 是否被打包（结论应为：不含 `Tests/`）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `package_dir={"PsiPyUtils": "."}` 删掉、只保留 `packages=["PsiPyUtils"]`，打包会发生什么？

> **参考答案**：`setuptools` 默认会去找与包同名的 `PsiPyUtils/` 子目录作为源码根，但本仓库是扁平布局、没有这个子目录，于是打包出来的 `PsiPyUtils` 包会是空的（找不到任何子模块），`from PsiPyUtils import FileWriter` 会失败。这正是 `package_dir` 不可或缺的原因（见 [setup.py:26-27](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/setup.py#L26-L27)）。

**练习 2**：为什么安装后的环境里找不到 `PsiPyUtils.Tests`？

> **参考答案**：因为 `packages=["PsiPyUtils"]` 只声明了顶层包，没有把 `Tests/` 列为子包。`setuptools` 只打包被声明的包，所以 `Tests/` 不进入分发包（见 [setup.py:27](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/setup.py#L27)）。

### 4.2 __init__.py 统一重导出：新旧两种 import 写法

#### 4.2.1 概念说明

有了 4.1 的铺垫，我们知道：扁平布局下，`FileWriter.py` 是包的一个**真实子模块** `PsiPyUtils.FileWriter`。所以下面这种「连模块路径一起写出来」的导入方式，**天然就能用**：

```python
from PsiPyUtils.FileWriter import FileWriter   # 旧写法：从子模块 FileWriter 里取类 FileWriter
```

这种写法在 2.x 时代就成立，因为文件一直都在。

3.0.0 在 [`__init__.py`](__init__.py) 里做了一件**增量**的事：**把 8 个子模块里的名字，统一重导出到包顶层**。重导出（re-export）的意思是——在 `__init__.py` 里把子模块的名字 `import` 进来，于是这些名字在「包的顶层」也能被访问到。这样使用者就多了一种更短的写法：

```python
from PsiPyUtils import FileWriter              # 新写法：直接从包顶层取类 FileWriter
```

> **回答 u1-l1 留下的悬念**：读完 [`__init__.py`](__init__.py) 你会发现，3.0.0 的改动是**增加**了顶层名字，而**没有删除**任何子模块文件。也就是说，**新旧两种 import 写法在 3.0.x 里都能工作**，它们最终引用的甚至是同一个类对象。Changelog 把它标为「不向后兼容」，更多是**风格层面**的提示——维护者希望使用者统一改用更短的新写法，并配合 3.0.0 一并引入的 pip 分发模型。这正是「读源码要批判性看文档」的一个小练习：**Changelog 里的「breaking」不一定等于「旧代码立刻报错」**，需要回到源码确认。

#### 4.2.2 核心流程

[`__init__.py`](__init__.py) 的工作流程，是「**导入时一次性把子模块的名字搬到顶层**」：

1. Python 执行 `import PsiPyUtils`（或任何 `from PsiPyUtils ...`）时，**最先运行** `PsiPyUtils/__init__.py`。
2. `__init__.py` 用相对导入，把 8 个子模块里的名字逐个 `import` 进来。
3. 这些名字因此成为**包顶层**的属性，于是 `from PsiPyUtils import <名字>` 成立。

但要小心一个**关键区分**：[`__init__.py`](__init__.py) 对 8 个子模块用了**两种不同的导入写法**，对应「重导出的是类」还是「重导出的是模块」：

| 子模块 | `__init__.py` 里的写法 | 重导出的东西 | 使用方式 |
| --- | --- | --- | --- |
| `FileWriter` | `from .FileWriter import FileWriter` | **类** `FileWriter` | `from PsiPyUtils import FileWriter` → 直接 `FileWriter(...)` |
| `TempWorkDir` | `from .TempWorkDir import TempWorkDir` | **类** `TempWorkDir` | 直接用 |
| `TempFile` | `from .TempFile import TempFile` | **类** `TempFile` | 直接用 |
| `ExtAppCall` | `from .ExtAppCall import ExtAppCall` | **类** `ExtAppCall` | 直接用 |
| `XmlToolbox` | `from .XmlToolbox import XmlToolbox` | **类** `XmlToolbox` | 直接用 |
| `EnvVariables` | `from . import EnvVariables` | **模块** `EnvVariables` | `from PsiPyUtils import EnvVariables` → `EnvVariables.AddToPathVariable(...)` |
| `FileOperations` | `from . import FileOperations` | **模块** `FileOperations` | `FileOperations.FindWithWildcard(...)` |
| `TextReplace` | `from . import TextReplace` | **模块** `TextReplace` | `TextReplace.TaggedReplace(...)` |

规律很清楚：**5 个模块导出的是与模块同名的「类」**（所以写 `from .X import X`，把类搬上来）；**3 个模块导出的是「模块本身」**（写 `from . import X`，因为它们对外提供的是一堆函数，没必要逐个搬）。

这个区分会直接影响你**怎么调用**重导出后的名字，是本讲最容易踩坑的地方。

#### 4.2.3 源码精读

整个 [`__init__.py`](__init__.py) 除了版权头，就是 8 行导入语句：

> [__init__.py:6-13](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/__init__.py#L6-L13) —— 8 行相对导入，把 8 个子模块的名字统一搬到 `PsiPyUtils` 包顶层；前 5 行（按上面表格）导入的是类，后 3 行导入的是模块。

具体看两类写法的代表：

> [__init__.py:9](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/__init__.py#L9) —— `from .FileWriter import FileWriter`：从当前包的 `FileWriter` 模块里，把**类** `FileWriter` 引入顶层。于是 `from PsiPyUtils import FileWriter` 拿到的就是那个可以直接实例化的类。

> [__init__.py:6](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/__init__.py#L6) —— `from . import EnvVariables`：把**模块** `EnvVariables` 整个引入顶层。于是 `from PsiPyUtils import EnvVariables` 拿到的是模块对象，使用时要再点出函数名，如 `EnvVariables.AddToPathVariable(...)`。

把这段和 4.1 联起来看，就能完整解释「为什么两种 import 写法都对」：

- **新写法** `from PsiPyUtils import FileWriter` 之所以成立，是因为 [`__init__.py:9`](__init__.py) 把类 `FileWriter` 搬到了顶层。
- **旧写法** `from PsiPyUtils.FileWriter import FileWriter` 之所以**仍然成立**，是因为 `FileWriter.py` 本身就是包的一个真实子模块（由 [setup.py:26-27](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/setup.py#L26-L27) 的 `package_dir` 决定），它不依赖 `__init__.py` 里有没有重导出。

两种写法拿到的是**同一个类对象**（可以 `PsiPyUtils.FileWriter.FileWriter is PsiPyUtils.FileWriter` 验证），不存在「两份实现」。

> 另外注意 [`ExtAppCall.py:11`](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/ExtAppCall.py#L11) `from .TempWorkDir import TempWorkDir`：**包内部模块之间**也用相对导入。这说明本库把「相对导入」作为内部约定——这也是 [`__init__.py`](__init__.py) 能用 `from .X import X` 的前提（包内部必须用相对或绝对包名导入，不能用「裸模块名」）。这个复用关系（ExtAppCall 复用 TempWorkDir）会在 u4 详细讲。

#### 4.2.4 代码实践

这是本讲的**主实践任务（可运行型）**，目标是把上面的结论亲手验证一遍。

1. **实践目标**：确认 3.0.1 下「新、旧两种 import 写法都能工作」，并理解为什么。
2. **操作步骤**：
   - 先按 4.1.4 的办法把包装进一个临时目录（或直接 `pip install dist/PsiPyUtils-3.0.1.tar.gz`）。
   - 写一个约 5 行的脚本（设临时目录已在 `PYTHONPATH`，或已正式安装）：
     ```python
     from PsiPyUtils.FileWriter import FileWriter   # 旧写法
     from PsiPyUtils import FileWriter as FW2        # 新写法
     print("旧写法 OK:", FileWriter)
     print("新写法 OK:", FW2)
     print("同一个类?:", FileWriter is FW2)
     ```
3. **需要观察的现象**：两行导入都不报错；最后一行打印 `True`，说明两种写法引用的是同一个类对象。
4. **预期结果**：两次导入都成功，`FileWriter is FW2` 为 `True`。由此可下结论：**3.0.0 的 import 改动是增量式的，旧写法仍然兼容**。
5. **如果无法确定运行结果**：本讲已通过核对源码确认——导入 `PsiPyUtils` 只依赖标准库（[`XmlToolbox.py:7-8`](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/XmlToolbox.py#L7-L8) 等均不 `import lxml`），所以即便不装 lxml 也能跑通；若你用 `pip install`（不加 `--no-deps`）会顺带装上 lxml，不影响结论。如本机环境受限无法安装，可标注「待本地验证」，但结论可由源码推导得出。

> **思考题（呼应实践任务里的「3.0.0 为何要做这次改动」）**：既然旧写法没坏，为什么还要改？因为新写法更短、更稳定——使用者不再需要记住「类在哪个文件里」，只要 `from PsiPyUtils import <类名>` 即可。这降低了模块内部文件改名时对使用者的冲击（维护者日后调整内部文件划分，使用者代码不受影响）。结合 3.0.0 同步引入的 pip 分发，这次改动让库的**对外接口**从「一堆文件路径」收敛为「一个包名」，是库走向规范化发布的自然一步。

#### 4.2.5 小练习与答案

**练习 1**：下面两行代码，哪一行能直接 `ExtAppCall(...)` 实例化？为什么？

```python
from PsiPyUtils import ExtAppCall          # (a)
from PsiPyUtils import EnvVariables        # (b)
```

> **参考答案**：(a) 可以直接实例化，因为 [`__init__.py:7`](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/__init__.py#L7) 用 `from .ExtAppCall import ExtAppCall` 重导出的是**类**。(b) 拿到的是**模块对象**，不能当类用，要再点出函数名，如 `EnvVariables.AddToPathVariable(...)`（见 [`__init__.py:6`](__init__.py)）。

**练习 2**：有人说「3.0.0 改了 `__init__.py`，所以旧写法 `from PsiPyUtils.FileWriter import FileWriter` 在 3.0.x 里会报错。」这句话对吗？请用本讲读到的源码反驳或支持它。

> **参考答案**：不对。旧写法访问的是**子模块** `PsiPyUtils.FileWriter`，它是否成立取决于文件 `FileWriter.py` 是不是包的子模块——而这一点由 [setup.py:26-27](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/setup.py#L26-L27) 的 `package_dir`/`packages` 决定，与 `__init__.py` 里有没有重导出无关。3.0.0 只是在 `__init__.py` 里**增加**了顶层名字（[`__init__.py:6-13`](__init__.py)），并没有删掉子模块，所以旧写法仍然有效。

**练习 3**：如果你只想用 `TextReplace` 里的 `TaggedReplace`，最省事的导入写法是哪种？

> **参考答案**：`from PsiPyUtils import TextReplace` 然后 `TextReplace.TaggedReplace(...)`。因为 [`__init__.py:12`](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/__init__.py#L12) 用 `from . import TextReplace` 重导出的是**模块**，不是某个具体函数，所以拿到的是模块对象，再点出函数名即可。

## 5. 综合实践

把本讲两个最小模块串起来，完成下面这个贯穿性任务：**对比 pip 包与 git submodule 两种使用方式，并各写一段导入代码验证**。

**背景**：README 明确说这个库既能当 pip 包用，也能继续当 git submodule 用（见 [README.md:33-40](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/README.md#L33-L40)）。请你从「**导入方式、版本管理、升级影响**」三个角度对比两者。

要求：

1. **pip 包方式**：按 4.1.4 安装后，写一行 `from PsiPyUtils import FileWriter` 并实例化（或仅打印类），确认能用。记录「版本由谁决定」（答案：由 `pip install` 的那个 `.tar.gz` 决定，其版本号来自 [setup.py:20](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/setup.py#L20)）。
2. **git submodule 方式**：假设把本仓库作为 submodule 嵌进某项目的 `extern/PsiPyUtils/` 子目录。此时导入写法取决于「该子目录是否在 `sys.path` 上、以及它的目录名」。请说明：若子目录名就叫 `PsiPyUtils`，则 `from PsiPyUtils import FileWriter` 同样成立；若目录名不同，则需要把该目录加进 `sys.path` 或改 import 路径。这种方式下「版本」由 **git 指向的 commit** 决定，升级即更新 submodule 指针。
3. **写一张对比表**（不少于 3 行），覆盖：版本来源、升级方式、对 3.0.0 import 变更的敏感度。

参考对比表（写完后可与之对照）：

| 维度 | pip 包 | git submodule |
| --- | --- | --- |
| 版本来源 | `.tar.gz` 里的 [setup.py:20](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/setup.py#L20) | submodule 指向的 commit |
| 升级方式 | `pip install` 新 `.tar.gz` | `git submodule update` 或切到新 commit |
| 导入写法 | `from PsiPyUtils import ...`（标准） | 同左，但要求子目录名/路径被 `sys.path` 识别 |
| 对 3.0.0 变更的敏感度 | 高（旧项目升级到 3.x 后建议改 import） | 低（README 说保留 submodule 方式正是为了**向后兼容**旧项目，见 [README.md:40](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/README.md#L40)） |

最后用一句话总结：**pip 包是 3.0.0 起推荐的「规范化」用法，submodule 是为照顾旧项目而保留的「向后兼容」用法**（[README.md:40](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/README.md#L40)）。

## 6. 本讲小结

- PsiPyUtils 采用**扁平布局**：8 个功能模块文件直接摊在仓库根目录，没有与包同名的子目录（见 [本讲源码地图](#3-本讲源码地图)）。
- 扁平布局之所以仍是一个合法包，靠的是 [setup.py:26-27](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/setup.py#L26-L27) 的 `package_dir={"PsiPyUtils": "."}` 把当前目录映射成包 `PsiPyUtils`；`packages=["PsiPyUtils"]` 只打包顶层包，因此 `Tests/` 不进入分发包。
- [`__init__.py:6-13`](__init__.py) 把 8 个子模块的名字统一重导出到包顶层：其中 5 个导出的是**类**（`from .X import X`），3 个导出的是**模块**（`from . import X`），使用方式不同。
- **3.0.0 的 import 改动是增量式的**：新写法 `from PsiPyUtils import FileWriter` 成立是因为 `__init__.py` 重导出了类；旧写法 `from PsiPyUtils.FileWriter import FileWriter` 仍然成立，是因为文件本身仍是真实子模块（由 `package_dir` 决定），两者引用同一个类对象。
- 导入 `PsiPyUtils` 只依赖标准库（如 [`XmlToolbox.py:7-8`](https://github.com/paulscherrerinstitute/PsiPyUtils/blob/6adbdb1a446754028d294102ca9d17753e84d944/XmlToolbox.py#L7-L8) 用 `xml.etree.ElementTree`），`install_requires` 里的 `lxml` 并非导入必需（是否冗余留待 u5-l2 讨论）。
- 读源码要**批判性对照文档**：Changelog 标的「不向后兼容」不等于「旧代码立刻报错」，需要回到 `__init__.py` 与 `setup.py` 确认——这是本讲对 u1-l1 悬念的最终回答。

## 7. 下一步学习建议

本讲把「包怎么组装、怎么导入」讲清了，接下来建议按顺序继续：

1. **u1-l3（运行测试套件）**：学会用 `Tests/RunAll.py` 跑通整套测试。你会看到测试文件里用 `sys.path.append('..')` 把上级目录（源码）加进路径，从而能 `from FileWriter import FileWriter`——这是与本章「包内相对导入」不同的一种「裸模块名」导入技巧，对比着读会很有收获。
2. **进入 u2 单元（上下文管理器三剑客）**：从 `TempWorkDir` 开始，正式进入功能模块的源码。届时你会大量用到本讲确认的导入写法，例如 `from PsiPyUtils import TempWorkDir`。
3. **u5-l2（打包、发布与版本管理）**：本讲埋下的「`install_requires=["lxml"]` 是否冗余」「`CustomSdist` 的清理-构建流程细节」都会在那里深入。
