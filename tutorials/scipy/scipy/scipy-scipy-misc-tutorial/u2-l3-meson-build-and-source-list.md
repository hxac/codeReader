# Meson 构建：py3.install_sources 与源码清单

## 1. 本讲目标

前三讲我们一直在「运行时」看 `scipy.misc`——导入它、捕获它的弃用警告、还原它曾经的功能。本讲换一个视角，站到**构建时（build time）**来看这个模块：`scipy/misc/` 目录里那四个文件是如何被打包进你的 `site-packages`，从而让 `import scipy.misc` 还能成功的？

读完本讲，你应当能够：

1. 读懂 `scipy/misc/meson.build` 里的 `python_sources` 列表和 `py3.install_sources(...)` 调用，说清每一行的作用。
2. 理解 Meson 的 `subdir` 与 Python 安装路径（`site-packages/scipy/misc`）之间的对应关系。
3. 解释一个看似矛盾的现象：**为什么一个全是弃用桩、没有任何实际功能的模块，仍然必须在构建系统里保留一条目，直到 SciPy 2.0.0 彻底删除？**
4. 自己动手为一个新的子包目录写一份 `meson.build`，并能预测「漏写某个文件」会在运行时造成什么后果。

## 2. 前置知识

本讲会用到一些构建系统的术语，先用最直白的方式解释一遍。

- **构建系统（build system）**：把「源码目录」变成「可安装的软件包」的工具。旧版 SciPy 用 `setup.py`（基于 `distutils`/`setuptools`），从某个版本起改用了 **Meson**。Meson 的配置写在 `meson.build` 文件里——注意，它用的是 Meson **自己的领域语言（DSL）**，不是 Python，所以里面的语法（如 `subdir('misc')`）看起来像函数调用，但本质是 Meson 的声明。
- **site-packages**：Python 安装第三方包的目录。`pip install scipy` 之后，`scipy` 这个包就躺在类似 `.../site-packages/scipy/` 的路径下。运行 `import scipy` 时，Python 解释器就是去这些路径里找磁盘上的真实文件。
- **安装（install）**：在 Meson 语境里，「安装一个 `.py` 文件」= 把它**复制**到目标 `site-packages` 子目录。没有被「安装」的文件，运行时就**不在磁盘上**，也就**无法被 import**。
- **声明式源码清单**：Meson 不会自动安装目录下的所有 `.py`。你必须显式列出「要安装哪些文件」——这就是 `python_sources` 列表存在的意义。
- **递归子目录（subdir）**：一个大的 `meson.build` 可以用 `subdir('misc')` 跳进 `misc/` 子目录，去执行那里的 `meson.build`，从而把每个子模块的构建逻辑分散到各自目录里。

一句话总结本讲的核心因果链：

> **构建清单（`python_sources`）决定哪些文件被复制到 `site-packages`；运行时 `import` 只认磁盘上的文件。** 所以「写没写进 `meson.build`」直接决定「能不能 import」。

如果你对 Meson 完全陌生，只要记住上面这条因果链即可，本讲会用 `scipy.misc` 这个极简例子把它讲透。

## 3. 本讲源码地图

本讲涉及的关键文件如下（全部已确认存在于当前 HEAD `de190e7fde`）：

| 文件 | 作用 |
| --- | --- |
| `scipy/misc/meson.build` | **本讲主角**。声明 `scipy/misc` 要安装的 `.py` 文件清单，并调用 `py3.install_sources` 把它们装到 `site-packages/scipy/misc`。 |
| `scipy/misc/__init__.py` | 弃用桩文件之一（前几讲已精读）。它是 `import scipy.misc` 的入口，**必须被安装**，否则导入直接失败。 |
| `scipy/misc/common.py` / `scipy/misc/doccer.py` | 另外两个弃用桩文件。它们是子模块，需要各自能被 `import scipy.misc.common` / `scipy.misc.doccer` 访问到，因此也必须出现在安装清单里。 |
| `scipy/meson.build`（父级） | 通过 `subdir('misc')` 把 `scipy/misc/meson.build` 纳入整体构建；并在文件顶部用 `py3 = import('python').find_installation(...)` 定义了 `py3` 这个「Python 安装对象」。 |
| `meson.build`（仓库根） | 第 21 行定义了全局可用的 `py3`。 |
| `scipy/datasets/meson.build`（对比材料） | 一个**有真实内容**的子包构建文件，结构和 `misc` 几乎一模一样，用来做对比阅读。 |

## 4. 核心概念与源码讲解

本讲把 `meson.build` 拆成三个递进的最小模块来学：先看「装哪些文件」（源码清单），再看「怎么装、装到哪」（`install_sources` 与 `subdir`），最后回答「为什么不能删」（构建与运行时的耦合）。

### 4.1 源码清单 `python_sources`：告诉构建系统「要装哪些文件」

#### 4.1.1 概念说明

Meson **不会**自动把一个目录下的所有 `.py` 都打包进最终安装包。它要求你显式给出一份「源码清单」。在 SciPy 里，这份清单几乎总是用一个名叫 `python_sources` 的变量（一个字符串列表）来表示，每个字符串是该目录下一个 `.py` 文件的名字。

这背后的设计哲学是：**显式优于隐式**。哪些文件对外发布、哪些只是本地试验脚本，必须由维护者明说，避免把内部杂文件误打包给用户。对 `scipy.misc` 这种退役模块，这份清单的作用就更微妙了——它列出的全是「弃用桩」，维护者必须**有意识地**决定：这些桩还要不要继续发布。

#### 4.1.2 核心流程

`scipy/misc/meson.build` 的执行流程可以画成两步：

```text
1. 定义变量：python_sources = ['__init__.py', 'common.py', 'doccer.py']
        │  （一份「相对当前目录」的文件名列表）
        ▼
2. 等待下一步用 py3.install_sources(python_sources, ...) 消费它
   （本模块只负责「列出名字」，真正复制动作在下一个模块讲）
```

要点：

- 列表里的名字是**相对当前 `meson.build` 所在目录**的相对路径（这里就是 `scipy/misc/`）。
- 顺序无所谓——这只是个集合语义的清单。
- **没列进来的 `.py` 不会被安装**，运行时也就 import 不到（这点会在 4.3 详细展开）。

#### 4.1.3 源码精读

整个文件只有 10 行，前 5 行就是源码清单：

[scipy/misc/meson.build:1-5](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/misc/meson.build#L1-L5) —— 定义 `python_sources` 列表，把三个弃用桩文件显式列为待安装源码。

```meson
python_sources = [
  '__init__.py',
  'common.py',
  'doccer.py'
]
```

这恰好对应 `scipy/misc/` 目录里除 `meson.build` 自身之外的**全部三个 `.py` 文件**。也就是说：维护者确认了「这三个桩文件都要继续发布给用户」。

作为对比，看一下一个**有真实内容**的子包——`scipy/datasets/meson.build`，它的清单就长得多了：

[scipy/datasets/meson.build:1-7](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/datasets/meson.build#L1-L7) —— `datasets` 子包要安装 `_fetchers.py`、`_registry.py` 等多个真实模块，结构却和 `misc` 完全一致。

```meson
python_sources = [
  '__init__.py',
  '_fetchers.py',
  '_registry.py',
  '_download_all.py',
  '_utils.py'
]
```

可见，无论模块是「退役的空壳」还是「功能完整」，`python_sources` 这种「先列清单、再统一安装」的写法是 SciPy 全仓库统一的模式。

#### 4.1.4 代码实践（源码阅读型）

**实践目标**：体会「清单 = 对外发布的文件集合」，并验证 `scipy/misc` 的清单与磁盘文件一一对应。

**操作步骤**：

1. 在仓库根目录列出 `scipy/misc` 下的所有文件（例如用 `git ls-files scipy/misc`）。
2. 把磁盘上的 `.py` 文件集合，和上面 `python_sources` 列表对照。
3. 再打开 `scipy/datasets/meson.build`，数一数它的清单里有几个文件，并思考：如果有人新增了一个 `scipy/datasets/_foo.py` 但忘了加进清单，会发生什么？

**需要观察的现象**：

- `git ls-files scipy/misc` 应当返回 4 个文件：`__init__.py`、`common.py`、`doccer.py`、`meson.build`。其中前三个就是 `python_sources` 的内容，`meson.build` 本身不需要被「安装」（它是构建配置，不是运行时代码）。

**预期结果**：清单 = 目录里所有「需要随包发布」的 `.py`，构建配置文件 `meson.build` 本身不在清单里。

**待本地验证**：第 3 步的「漏写」后果，建议等到 4.3 节学完后再下结论，那里会给出精确答案。

#### 4.1.5 小练习与答案

**练习 1**：如果维护者想让 `scipy.misc` **立刻**在下一个版本里彻底消失，除了删除三个 `.py` 文件外，还需要动 `meson.build` 吗？

> **答案**：需要。删了文件后，`python_sources` 里仍写着这三个名字，Meson 安装时会因为找不到文件而报错（除非同时删掉或清空 `meson.build`）。彻底移除一个子包时，`meson.build` 和源码必须一起处理，还要在父级 `scipy/meson.build` 里去掉对应的 `subdir('misc')`（见 4.2）。

**练习 2**：`python_sources` 是一个变量名，叫别的名字（比如 `my_files`）会影响构建吗？

> **答案**：不会。它只是个普通 Meson 变量，名字是约定俗成（SciPy 全仓库都用 `python_sources`，便于统一维护和检索）。真正起作用的是把它传给 `py3.install_sources(...)` 那一行。

---

### 4.2 `py3.install_sources` 与 `subdir`：把文件装进正确的 Python 包目录

#### 4.2.1 概念说明

光列出文件名还不够，还要告诉 Meson「把这些文件复制到哪个安装目录」。这一动作由 `py3.install_sources(...)` 完成。其中 `py3` 是一个**代表「目标 Python 解释器/安装位置」的对象**，它在仓库根的 `meson.build` 里被定义一次，全仓库通用。

`install_sources` 最关键的一个参数是 `subdir`：它指定「在 Python 包根目录之下，再往下走哪一层子目录」。这个 `subdir` 字符串和 Python 运行时的**导入路径**是严格对应的——`subdir: 'scipy/misc'` 就意味着文件最终落在 `site-packages/scipy/misc/`，于是 `import scipy.misc` 才能找到它们。

#### 4.2.2 核心流程

把构建链路和运行时链路拼起来看：

```text
【构建时】
仓库根 meson.build:  py3 = import('python').find_installation(pure: false)   ← 定义 py3
        │
scipy/meson.build:   subdir('misc')   ← 跳进 misc/ 子目录执行它的 meson.build
        │
scipy/misc/meson.build:
   py3.install_sources(python_sources, subdir: 'scipy/misc')
        │   复制 __init__.py / common.py / doccer.py
        ▼
site-packages/scipy/misc/__init__.py
site-packages/scipy/misc/common.py
site-packages/scipy/misc/doccer.py

【运行时】
import scipy.misc        ──► Python 在 site-packages/scipy/misc/__init__.py 找到入口 ✓
import scipy.misc.common ──► Python 在 site-packages/scipy/misc/common.py 找到子模块 ✓
```

两个关键认识：

1. **`subdir` 的值 `'scipy/misc'` 是从 `site-packages` 算起的相对路径**，它直接等于「这个子包在 Python 里的导入点」。
2. **`subdir('misc')`（Meson 递归）和 `subdir: 'scipy/misc'`（安装目标）是两件不同的事**：前者是「去执行 `misc/meson.build`」，后者是「文件安装到哪个目录」。别被同一个词 `subdir` 搞混。

#### 4.2.3 源码精读

先看本模块的安装调用（`meson.build` 的后半段）：

[scipy/misc/meson.build:7-10](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/misc/meson.build#L7-L10) —— 调用 `py3.install_sources`，把上面列出的三个桩文件安装到 `site-packages/scipy/misc` 目录。

```meson
py3.install_sources(
  python_sources,
  subdir: 'scipy/misc'
)
```

这里的 `py3` 从哪来？它定义在仓库根的 `meson.build`：

[meson.build:21-21](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/meson.build#L21-L21) —— 用 Meson 内置 `python` 模块的 `find_installation` 创建一个 Python 安装对象，全局命名为 `py3`。

```meson
py3 = import('python').find_installation(pure: false)
```

> `pure: false` 表示这不是一个「纯 Python」安装——SciPy 含有编译扩展（C/Fortran），需要装到**平台相关**的目录（`platlib`）而非纯 Python 目录（`purelib`），这样编译产物和 `.py` 文件才能落在同一个 `scipy` 包里被一起找到。对 `misc` 这种纯桩文件来说，它只是「搭便车」跟着整个 `scipy` 包一起被安装。

而「跳进 `misc` 子目录执行本文件」的动作，发生在父级 `scipy/meson.build` 的末尾：

[scipy/meson.build:719-739](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/meson.build#L719-L739) —— 父级构建文件按顺序对每个子包调用 `subdir(...)`，`misc` 排在最后一个（第 739 行）。

```meson
subdir('_build_utils')
subdir('_external')
...
subdir('datasets')
subdir('misc')      # ← 这一行让 Meson 去执行 scipy/misc/meson.build
```

可以看到 `misc` 被排在所有子包的**最后**——这是合理的，因为它没有任何被别人依赖的内容，纯粹是历史包袱，放在最末尾不影响其它模块的构建并行度（注释里说明了「重的子包先排、便于并行」的顺序原则）。

顺带一提，父级 `scipy/meson.build` 自己也用同样的 `python_sources` + `install_sources` 模式来安装 `scipy` 包根目录下的文件，可作为同类写法的旁证：

[scipy/meson.build:453-466](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/meson.build#L453-L466) —— `scipy` 包根目录的 `__init__.py` 等文件也通过 `py3.install_sources(..., subdir: 'scipy')` 安装，与子包写法完全一致。

#### 4.2.4 代码实践（可运行）

**实践目标**：亲眼看到「被 `install_sources` 的文件」确实落在了 `site-packages/scipy/misc` 里，从而理解 `subdir` 与导入路径的对应。

**操作步骤**（前提：本地已 `pip install` 一个可用的 SciPy）：

1. 找到 SciPy 的安装目录：
   ```bash
   python -c "import scipy, os; print(os.path.dirname(scipy.__file__))"
   ```
2. 列出 `misc` 子目录的内容：
   ```bash
   ls "$(python -c "import scipy, os; print(os.path.dirname(scipy.__file__))")/misc"
   ```
3. 确认里面正好是 `__init__.py`、`common.py`、`doccer.py` 三个文件（外加可能有的 `__pycache__`）。

**需要观察的现象**：磁盘上 `site-packages/scipy/misc/` 的文件集合，正好等于 `python_sources` 列表。

**预期结果**：三个 `.py` 文件都在，证明 `subdir: 'scipy/misc'` 把它们精确地装到了「Python 能 import 到 `scipy.misc`」的位置。

**待本地验证**：若你装的是**可编辑（editable）开发安装**，看到的可能是源码目录的软链接/查找器映射，文件集合仍一致但路径形态不同；这属于开发模式的细节，不影响结论。

#### 4.2.5 小练习与答案

**练习 1**：把 `subdir: 'scipy/misc'` 误写成 `subdir: 'misc'`，会发生什么？

> **答案**：文件会被装到 `site-packages/misc/` 而不是 `site-packages/scipy/misc/`。于是 `import scipy.misc` 在 `site-packages/scipy/` 下找不到 `misc` 子目录，会抛 `ModuleNotFoundError: No module named 'scipy.misc'`；同时系统里反而多出一个顶层的 `misc` 包，污染命名空间。`subdir` 必须从包根（`site-packages`）算起，写全整条导入路径。

**练习 2**：`subdir('misc')`（递归子目录）和 `subdir: 'scipy/misc'`（安装参数）有什么区别？

> **答案**：前者是 Meson 的内置函数，意思是「现在去执行 `misc/meson.build` 这个文件」，控制的是**构建过程的走向**；后者是 `install_sources` 的一个键值参数，意思是「把文件安装到 `site-packages/scipy/misc`」，控制的是**安装目标路径**。两者恰好都叫 `subdir`，但完全不是一回事。

---

### 4.3 构建条目与运行时导入的耦合：为什么弃用桩模块不能删 `meson.build`

#### 4.3.1 概念说明

这是本讲最想让你带走的一个认识：**「模块在源码里被弃用」不等于「模块在构建里被移除」。**

弃用的本意（见前几讲）是**软着陆**——让老代码 `import scipy.misc` 仍然能跑，只是多一条 `DeprecationWarning`，给用户时间迁移。要做到「能跑」，那些桩 `.py` 文件就**必须在用户的 `site-packages` 里真实存在**；而它们能存在，又**必须**有一条 `meson.build` 把它们 `install_sources` 进去。

于是出现一个反直觉但合理的结论：一个**功能上已经被掏空、只剩一行警告**的模块，它的构建条目**不能提前删**。提前删掉 `meson.build`（或从清单里去掉桩文件），就等于把「软弃用」变成了「硬删除」，老代码会直接 `ModuleNotFoundError`，这违背了 SciPy「先警告、跨版本保留、到 2.0.0 才真正删」的弃用约定（见 u2-l2）。

#### 4.3.2 核心流程

把「漏装一个文件」的后果推演清楚（这正是本讲综合实践要验证的命题）：

```text
假设：python_sources 漏写了 'common.py'
        │
        ▼
构建/安装时：只有 __init__.py、doccer.py 被复制到 site-packages/scipy/misc/
        │
        ▼
运行时：
  import scipy.misc         ✓  （__init__.py 在，包本身能导入，只发弃用警告）
  import scipy.misc.common ✗  （site-packages/scipy/misc/common.py 不存在）
        │
        ▼
  ModuleNotFoundError: No module named 'scipy.misc.common'
```

关键点：漏装**子模块**文件，不会让父包 `import scipy.misc` 失败（因为 `__init__.py` 还在），只会让**该子模块**的导入失败。这是一种「部分可用」的隐蔽 bug——维护者很容易在自测时只 import 了父包而漏掉子模块，从而没发现清单写漏。

#### 4.3.3 源码精读

桩文件本身极简，但它和构建文件的**配合**才是它能软着陆的关键。先回顾 `__init__.py`：

[scipy/misc/__init__.py:1-6](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/misc/__init__.py#L1-L6) —— 整个文件唯一的有效动作就是发一条弃用警告；它必须被 `meson.build` 安装到 `site-packages/scipy/misc/`，`import scipy.misc` 才会执行到这一行。

```python
import warnings
warnings.warn(
    "scipy.misc is deprecated and will be removed in 2.0.0",
    DeprecationWarning,
    stacklevel=2
)
```

子模块 `common.py` 同理：

[scipy/misc/common.py:1-6](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/misc/common.py#L1-L6) —— 子模块桩文件，必须出现在 `python_sources` 里，否则 `import scipy.misc.common` 会失败。

把这条「文件 ↔ 能否 import」的对应关系列成表：

| 场景 | `__init__.py` 被装 | `common.py` 被装 | 运行时行为 |
| --- | :-: | :-: | --- |
| 当前正确配置 | ✅ | ✅ | `import scipy.misc` 与 `import scipy.misc.common` 均成功并发警告 |
| 漏装 `common.py` | ✅ | ❌ | `import scipy.misc` 成功；`import scipy.misc.common` → **ModuleNotFoundError** |
| 删掉整个 `meson.build` 条目 | ❌ | ❌ | `import scipy.misc` → **ModuleNotFoundError**（提前硬删除，违背软弃用约定） |

这张表就是「为什么桩文件模块仍需保留构建条目」的完整答案：**为了让软弃用期间的每一次 `import` 都能成功并发出警告，构建清单必须完整保留，直到 2.0.0 才连桩带构建条目一并删除。**

#### 4.3.4 代码实践（推理 + 待本地验证）

**实践目标**：在不真的破坏 SciPy 的前提下，推理出「漏写清单」的运行时后果，并设计一个最小本地复现。

**操作步骤**：

1. **推理**：对照上面那张表，说出「`python_sources` 删掉 `'common.py'` 这一项后」会发生什么。
2. **最小复现**（不依赖重新编译 SciPy）：在一个临时目录里造一个最小包，模拟漏装：
   ```bash
   mkdir -p /tmp/demo/mypkg/sub
   # 写两个文件 mypkg/__init__.py 和 mypkg/sub/__init__.py（内容随意，如 print）
   ```
   然后故意**只把 `mypkg/__init__.py` 复制**到另一个目录 `site_root/mypkg/`（模拟「`sub` 没被装」），再把 `site_root` 加进 `PYTHONPATH`：
   ```bash
   PYTHONPATH=/tmp/site_root python -c "import mypkg; import mypkg.sub"
   ```
3. 观察第 2 步的报错。

**需要观察的现象**：第 2 步应在 `import mypkg.sub` 处抛 `ModuleNotFoundError: No module named 'mypkg.sub'`，而 `import mypkg` 本身成功。

**预期结果**：复现了「父包可用、漏装的子模块不可用」的现象，从而印证「清单写漏 = 子模块 import 失败」。

**待本地验证**：若要直接在 SciPy 上复现「漏装 `common.py`」，需要修改 `scipy/misc/meson.build` 后重新 `meson install`（成本较高，且会改动源码——本讲**不允许改源码**），因此推荐用第 2 步的最小包复现来等效验证。可编辑安装模式下，文件可能从源码树直接解析、掩盖该问题，故结论以**常规（wheel）安装**为准。

#### 4.3.5 小练习与答案

**练习 1**：既然 `scipy.misc` 已经没有真实功能，为什么不在 PR #21864（移除大部分内容那次）里**顺手**把 `meson.build` 也删了？

> **答案**：因为那次移除的是**内容**，留下的三个桩文件还需要继续发布，才能对老代码做软弃用。删 `meson.build` 等于删了安装条目，桩文件装不进 `site-packages`，`import scipy.misc` 会立刻 `ModuleNotFoundError`——这就不是「弃用」而是「删除」了。构建条目必须等到 SciPy 2.0.0 连桩文件一起删除时才能一并去掉（同时去掉父级的 `subdir('misc')`）。

**练习 2**：「漏装 `common.py`」为什么是个**隐蔽**的 bug？

> **答案**：因为 `import scipy.misc`（父包）仍然成功——`__init__.py` 还在。如果维护者或 CI 只测了父包导入、没测子模块导入，就发现不了 `common.py` 缺失。正确的做法是在 CI 里把**每个子模块**都 import 一遍（这正是 SciPy 的公共 API 测试在做的事）。

---

## 5. 综合实践

把本讲三块知识串起来，完成下面这个综合任务。

### 任务：为假设的 `scipy/myutil` 子包写一份 `meson.build`，并预测「漏写」后果

**背景**：假设你新建了一个子包 `scipy/myutil`，目录下有两个文件：包入口 `__init__.py` 和工具模块 `helper.py`。你希望用户能 `import scipy.myutil` 和 `import scipy.myutil.helper`。

**第 1 步——仿照 `scipy/misc/meson.build` 写出构建文件**。

参考答案（示例代码，非项目原有文件）：

```meson
python_sources = [
  '__init__.py',
  'helper.py'
]

py3.install_sources(
  python_sources,
  subdir: 'scipy/myutil'
)
```

要点自检：

- `python_sources` 列出了**所有**需要对外发布的 `.py`（这里是 `__init__.py` 和 `helper.py`）。
- `subdir: 'scipy/myutil'` 从 `site-packages` 算起，正好等于导入路径。
- 没有忘记 `py3` 来自仓库根的全局定义，本文件无需再定义。

**第 2 步——把它接入父级构建**。

还要在 `scipy/meson.build` 的 `subdir(...)` 列表里加一行 `subdir('myutil')`（参考 4.2.3 的 `subdir('misc')`），否则这个子目录的 `meson.build` 根本不会被执行。

**第 3 步——回答「漏写」问题（本讲的核心命题）**。

> 如果你在 `python_sources` 里**忘记写 `'common.py'`**（回到 `scipy.misc` 的场景），`import scipy.misc.common` 会发生什么？

**参考答案**：会抛 `ModuleNotFoundError: No module named 'scipy.misc.common'`。原因：`common.py` 没被列进 `python_sources`，`py3.install_sources` 就不会把它复制到 `site-packages/scipy/misc/`；运行时 Python 在该目录下找不到 `common.py`，于是子模块导入失败。注意此时 `import scipy.misc`（父包）仍能成功，因为 `__init__.py` 仍被安装——这正是该 bug 隐蔽的原因（详见 4.3）。

**第 4 步（可选，待本地验证）**：用 4.3.4 第 2 步的最小包复现法，亲手验证「漏装子模块 → `ModuleNotFoundError`」的现象。

## 6. 本讲小结

- `scipy/misc/meson.build` 只有 10 行：前 5 行用 `python_sources` 列出待安装的 `.py` 清单，后 4 行用 `py3.install_sources(..., subdir: 'scipy/misc')` 把它们装进 `site-packages/scipy/misc`。
- `python_sources` 是**显式源码清单**——Meson 不会自动安装目录下所有文件；没列进来的文件运行时 import 不到。
- `py3` 是在仓库根 `meson.build` 用 `import('python').find_installation(pure: false)` 定义的全局「Python 安装对象」；`subdir: 'scipy/misc'` 决定安装目标路径，它**等于** Python 的导入路径。
- 要区分两个 `subdir`：父级的 `subdir('misc')` 是「去执行子目录的 `meson.build`」，`install_sources` 的 `subdir:` 是「文件装到哪个目录」。
- **弃用桩模块不能提前删构建条目**：删了就等于把软弃用变成硬删除，`import scipy.misc` 会立刻 `ModuleNotFoundError`，违背 SciPy 的弃用约定。构建条目要和桩文件一起留到 2.0.0。
- 漏装某个子模块文件，会让该子模块的 `import` 抛 `ModuleNotFoundError`，但父包导入仍成功——这是个容易被 CI 漏掉的隐蔽 bug。

## 7. 下一步学习建议

- 想看「一个真正有内容的子包」是怎么用同样的模式构建的，去精读 `scipy/datasets/meson.build`（本讲已作为对比引用），它还多了一行 `subdir('tests')`，展示了「子包里再嵌套测试子目录」的写法。
- 想理解「软弃用」在 API 层面（而非桩文件）是怎么做的，回到 u2-l2 精读的 `scipy/_lib/deprecation.py`，对比 `_sub_module_deprecation`（保留旧入口、软重定向）与 `scipy.misc` 这种「只剩裸 `warnings.warn` 的墓碑桩」的差异。
- 下一讲的 u3 系列会从构建侧回到**迁移侧**：当 `scipy.misc` 在 2.0.0 连构建条目一起被删除后，依赖它的老代码该如何迁移到 `scipy.datasets` 等现代等价物。建议先读 `scipy/datasets/_fetchers.py`，理解新数据集子包的按需下载机制，为综合迁移实践打底。
