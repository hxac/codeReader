# 工具链与依赖：如何仿真与构建

## 1. 本讲目标

本讲是第 1 单元的第三篇，承接 u1-l2「仓库布局」，回答一个非常实际的问题：

> 我已经把仓库克隆下来了，那么**到底要装什么、配置什么、运行哪个脚本**，才能跑起来仿真和综合？

读完本讲，你应该能够：

1. 说出 hdl-modules 依赖的四个外部组件：`tsfpga`、`VUnit`、`hdl-registers`、Vivado，并区分它们的角色。
2. 看懂 `tools/tools_env.py` 如何用 `Path(__file__)` 推导出仓库根目录与生成目录，作为整个工具链的「路径单一信息源」。
3. 理解 `tools/tools_pythonpath.py` 的 `sys.path.insert` 引导机制，以及它为什么「优先本地检出、回退到 pip 安装」。
4. 区分两种使用方式：**手动添加源码** 与 **通过 tsfpga 的 `get_hdl_modules()` 集成**，并知道何时该用哪种。

本讲只讲「环境和入口」，不深入任何具体 VHDL 模块的实现——那是第 2 单元之后的事。

## 2. 前置知识

本讲假设你已经读过 u1-l1 与 u1-l2，知道：

- hdl-modules 是一组 VHDL-2008 构建块，每个模块下分 `src/`（可综合）、`test/`（测试台）、`sim/`（BFM）等子目录。
- 库名等于模块名（裸名，无 `lib` 后缀）。

此外，先建立两个直觉：

**直觉一：VHDL 工程离不开「宿主」语言。**
VHDL 本身只描述硬件，但「把哪些 `.vhd` 文件以什么库编译、用什么 generic、跑哪些测试」这套工程编排，hdl-modules 选择用 **Python** 来做。所以你会看到仓库里除了 VHDL，还有大量 Python 脚本和 `tools/` 目录。这套 Python 工具并不自己造轮子，而是站在另外三个 Python 包的肩膀上：`tsfpga`、`VUnit`、`hdl-registers`。

**直觉二：Python「找得到模块」全靠 `sys.path`。**
Python 在 `import xxx` 时，会按顺序搜索 `sys.path` 列表里的目录。理解了这一点，本讲的 `tools_pythonpath.py` 就只是一句「往搜索路径前面插了几个目录」而已，并不神秘。

几个术语：

| 术语 | 一句话解释 |
|------|-----------|
| `PYTHONPATH` | 一个环境变量，Python 启动时会把它的值加进 `sys.path`，让你能 `import` 仓库外的代码。 |
| `sys.path` | Python 运行时实际使用的模块搜索路径列表，可以用 `insert`/`append` 动态修改。 |
| Vivado | Xilinx 的 FPGA 综合与实现工具，hdl-modules 的构建流程以它为目标。 |
| BFM | Bus Functional Model，总线功能模型，仿真用的「假」master/slave，详见 u8-l1。 |

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
|------|------|
| [doc/sphinx/getting_started.rst](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/doc/sphinx/getting_started.rst) | 官方「上手指南」，声明依赖、讲清手动流程与 tsfpga 流程的区别。 |
| [pyproject.toml](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/pyproject.toml) | Python 工程配置。注意：本项目的它**只配置了 ruff 代码检查**，并没有列出运行时依赖。 |
| [tools/tools_env.py](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/tools_env.py) | 定义仓库根目录、模块目录、生成目录等「路径常量」，是全工具链的单一信息源。 |
| [tools/tools_pythonpath.py](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/tools_pythonpath.py) | 把 tsfpga / hdl-registers / vunit 的本地检出路径插入 `sys.path`。 |
| [tools/simulate.py](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/simulate.py) | 仿真入口脚本，本讲实践任务的运行对象。 |
| [hdl_modules/__init__.py](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/hdl_modules/__init__.py) | 提供 `get_hdl_modules()`，供 tsfpga 用户集成时调用。 |

> 说明：本仓库根目录的 `readme.rst`、`license.txt`、`hdl_modules/about.py` 在 u1-l1 已讲过，本讲不再重复。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

1. **依赖全景**：四个外部组件各自负责什么。
2. **路径单一信息源**：`tools_env.py`。
3. **PYTHONPATH 引导**：`tools_pythonpath.py`。
4. **入口脚本与两种集成方式**：`simulate.py` 与「手动 vs tsfpga」。

### 4.1 依赖全景：tsfpga / VUnit / hdl-registers / Vivado

#### 4.1.1 概念说明

hdl-modules 的 VHDL 代码本身是「纯」的——只要按 VHDL-2008 编译就能用。但项目附带的**测试台、BFM、仿真/综合/构建脚本**依赖外部组件。可以把它们分成两类：

- **Python 工具链（工程编排）**：`tsfpga`、`VUnit`、`hdl-registers`。它们决定「怎么编译、怎么跑仿真、怎么生成寄存器代码」。
- **EDA 工具（实际综合/仿真后端）**：Vivado。`tsfpga` 和 `VUnit` 在需要时去调用它。

四个组件的职责分工：

| 组件 | 角色 | 何时需要 |
|------|------|---------|
| `VUnit` | VHDL 测试框架，提供 `vunit_lib`、断言、测试枚举、运行器 | 跑任何测试台、或用 `bfm` 模块时必需 |
| `tsfpga` | FPGA 工程编排框架，扫描 `modules/`、管理库/文件、驱动 Vivado | 用 `tools/*.py` 脚本或 `get_hdl_modules()` 时必需 |
| `hdl-registers` | 寄存器接口代码生成器（生成 VHDL/C/C++） | 使用带寄存器的模块（如 `dma_axi_write_simple`）时必需 |
| Vivado | 综合/实现/仿真后端 | 跑综合（`synthesize.py`/`build_fpga.py`）或 Vivado 仿真时需要 |

一个关键事实：**如果只想要可综合源码，且不用测试台和 BFM，那么没有任何依赖**——直接拿 `src/` 下的 `.vhd` 走你自己的流程即可。

#### 4.1.2 核心流程

依赖的「按需引入」流程可以用一句话概括：

```text
你打算用项目的哪一部分？  ──►  决定你要装哪些依赖
```

- 只用 `src/` 可综合源码 → 零依赖（手动流程）。
- 要跑测试台 / 用 BFM → 装 VUnit（5.0.0+ 预发布版）。
- 要用 Python 脚本自动化 → 再加 tsfpga。
- 要用带寄存器的模块 → 再加 hdl-registers（自动生成 VHDL）。
- 要真的下板综合 → 还要装 Vivado。

#### 4.1.3 源码精读

依赖声明写在文档里，而不是 `pyproject.toml` 里。先看官方上手指南的 Dependencies 小节：

[doc/sphinx/getting_started.rst:24-31](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/doc/sphinx/getting_started.rst#L24-L31) —— 说明 `bfm` 模块和所有测试台依赖 VUnit 5.0.0+，目前是预发布版，安装命令是：

```
python -m pip install vunit-hdl==5.0.0.dev5
```

并明确：**排除 `bfm` 模块和测试台后，不再需要任何依赖**。

再看 `pyproject.toml`——这里有一个容易踩坑的点。它的第一行配置是：

[pyproject.toml:2-4](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/pyproject.toml#L2-L4) —— `[tool.ruff]`，`line-length = 100`。

整份 `pyproject.toml` 从头到尾只有 `[tool.ruff]`、`[tool.ruff.lint]`、`[tool.ruff.lint.isort]`、`[tool.ruff.lint.pylint]` 几节，**没有 `[project]` 节，也没有 `dependencies` 字段**。也就是说：

> 这个 `pyproject.toml` 是给 ruff 代码检查用的，它**并不声明** tsfpga / VUnit / hdl-registers 这些运行时依赖。

所以你不能指望 `pip install -e .` 自动把依赖装好——必须按 `getting_started.rst` 的指示手动安装。

#### 4.1.4 代码实践（源码阅读型）

**目标**：确认依赖声明的「事实来源」。

**步骤**：

1. 打开 `pyproject.toml`，确认其中只有 ruff 相关配置。
2. 打开 `doc/sphinx/getting_started.rst`，找到 `.. _dependency_vunit:` 锚点下的 Dependencies 小节。
3. 记下 VUnit 的最低版本号与安装命令。

**预期结果**：你会发现依赖的「真相」在文档里，而不在 `pyproject.toml` 里。这一点会影响你后面写自动化脚本时的依赖安装策略。

**待本地验证**：不同时间点 VUnit 的预发布版本号可能变化，请以你克隆时 `getting_started.rst` 中的实际文字为准（当前 HEAD 指向 `5.0.0.dev5`）。

#### 4.1.5 小练习与答案

**练习 1**：同事问你「`pip install -e .` 能不能把 hdl-modules 的全部依赖装好？」你怎么回答？

> **答案**：不能。`pyproject.toml` 里没有 `[project].dependencies`，它只配置了 ruff。tsfpga、VUnit、hdl-registers 需要按 `getting_started.rst` 单独安装。

**练习 2**：如果你的设计只用 `fifo` 模块的 `src/fifo.vhd`，且完全不走测试台，你需要安装 VUnit 吗？

> **答案**：不需要。文档明确：排除 BFM 和测试台后零依赖，直接把 `src/` 下的文件按 VHDL-2008 编译即可。

---

### 4.2 路径单一信息源：tools_env.py

#### 4.2.1 概念说明

整个 Python 工具链需要一个「大家都知道仓库根目录在哪里」的约定。如果把根目录路径硬编码、或者在每个脚本里各算各的，一旦仓库移动位置就会到处出错。hdl-modules 的做法是：写一个极小的 `tools/tools_env.py`，用「脚本自身位置」反推出仓库根目录，再把所有派生路径集中定义在这里，作为**单一信息源（Single Source of Truth）**。

#### 4.2.2 核心流程

推导逻辑非常简洁：

```text
tools_env.py 文件自身位置 (__file__)
        │  .parent          → tools/
        │  .parent          → 仓库根目录 REPO_ROOT
        ▼
REPO_ROOT / "modules"   → HDL_MODULES_DIRECTORY   （VHDL 模块源码）
REPO_ROOT / "doc"       → HDL_MODULES_DOC          （Sphinx 文档源）
REPO_ROOT / "generated" → HDL_MODULES_GENERATED    （生成产物输出）
```

因为用的是 `Path(__file__).parent.parent.resolve()`，所以**无论你在哪个目录执行脚本、无论仓库克隆到哪，路径都能正确推导**——`.resolve()` 还会把符号链接解析成绝对路径。

#### 4.2.3 源码精读

整个文件只有寥寥几行，但它是后续所有脚本的基础：

[tools/tools_env.py:10-16](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/tools_env.py#L10-L16) —— 定义四个路径常量：

- 第 12 行：`REPO_ROOT = Path(__file__).parent.parent.resolve()`，由文件自身位置反推根目录。
- 第 13 行：`HDL_MODULES_DIRECTORY = REPO_ROOT / "modules"`，指向 VHDL 模块主体，是 `get_modules()` 的扫描根。
- 第 15 行：`HDL_MODULES_DOC`，Sphinx 文档源目录。
- 第 16 行：`HDL_MODULES_GENERATED`，综合/仿真/文档生成物的输出目录（仓库默认不存在该目录，首次运行时创建）。

注意：这个文件**不 import 任何第三方包**，只依赖标准库 `pathlib`。这是刻意为之——它要被其它脚本在最早期 `import`，所以必须零外部依赖、零副作用。

#### 4.2.4 代码实践（源码阅读型）

**目标**：体会「单一信息源」带来的好处。

**步骤**：

1. 在仓库内任意子目录（例如 `modules/fifo/`）启动 Python，执行：
   ```python
   import sys; sys.path.insert(0, "<仓库根目录绝对路径>")
   from tools import tools_env
   print(tools_env.REPO_ROOT)
   print(tools_env.HDL_MODULES_GENERATED)
   ```
2. 再换到另一个目录重复一次。

**需要观察的现象**：无论你在哪个目录运行，`REPO_ROOT` 总是指向同一个绝对路径；`HDL_MODULES_GENERATED` 是 `<根目录>/generated`。

**预期结果**：因为路径来自 `__file__` 而非当前工作目录，所以结果与「你在哪运行」无关。这正是把它集中放在 `tools_env.py` 的意义。

**待本地验证**：请用你本地的真实仓库路径替换上面的占位符后运行。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `tools_env.py` 只用标准库、不 import tsfpga？

> **答案**：因为它要被其它脚本在最早期导入（甚至早于设置 `PYTHONPATH` 的 `tools_pythonpath`）。若它依赖第三方包，就会在那些包还没就位时失败，形成「先有鸡还是先有蛋」的死结。

**练习 2**：`HDL_MODULES_GENERATED` 目录在刚克隆的仓库里存在吗？

> **答案**：通常不存在。它由 `REPO_ROOT / "generated"` 推导而来，首次运行生成脚本时才被创建。它一般也会被 `.gitignore` 忽略。

---

### 4.3 PYTHONPATH 引导：tools_pythonpath.py

#### 4.3.1 概念说明

hdl-modules 的开发者通常会把几个相关仓库（`tsfpga`、`hdl-registers`、`vunit`）以**兄弟目录**的形式并排检出，方便跨仓库联调。为了让 Python 能 `import tsfpga`、`import vunit`、`import hdl_registers`，需要一个机制把这些本地检出加进 `sys.path`。

`tools/tools_pythonpath.py` 就是干这件事的。它有一句关键注释道破了设计意图：

> 用 `insert()` 而非 `append()`，是为了让本地检出优先于任何 pip 安装。

也就是说：如果你既 pip 装了 tsfpga、又在本地检出了 tsfpga，脚本会**优先用本地那个**。这对「改了 tsfpga 源码、马上在 hdl-modules 里验证」的开发场景非常重要。

#### 4.3.2 核心流程

```text
import tools.tools_pythonpath
        │
        ├── 计算 REPO_ROOT（来自 tools_env）
        │
        ├── REPO_ROOT.parent.parent / "tsfpga" / "tsfpga"        → insert(0, ...)
        ├── REPO_ROOT.parent.parent / "hdl-registers" / "hdl-registers"  → insert(0, ...)
        └── REPO_ROOT.parent.parent / "vunit" / "vunit"          → insert(0, ...)
```

期望的目录布局（兄弟仓库）是：

```text
<某个父目录>/
├── hdl-modules/        ← 本仓库（REPO_ROOT 在它里面的子目录）
│   └── tools/tools_pythonpath.py
├── tsfpga/tsfpga/      ← PATH_TO_TSFPGA
├── hdl-registers/hdl-registers/  ← PATH_TO_HDL_REGISTERS
└── vunit/vunit/        ← PATH_TO_VUNIT
```

注意 `insert(0, ...)` 的顺序：先插 tsfpga、再插 hdl-registers、最后插 vunit。由于每次都插到最前面（索引 0），最终 `sys.path` 最前面的顺序是 vunit → hdl-registers → tsfpga。不过这三个包互不重名，顺序对结果无影响，重要的是「都在最前面、优先于 pip 安装」。

一个温和的回退行为：如果上述本地路径**不存在**（你没有检出兄弟仓库），`sys.path.insert` 一个不存在的目录并不会报错——Python 只是在那里找不到模块，于是回退到 `sys.path` 后面的 pip 安装位置。所以这个文件对「纯 pip 安装」的用户也是安全的。

#### 4.3.3 源码精读

[tools/tools_pythonpath.py:14-16](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/tools_pythonpath.py#L14-L16) —— 导入 `sys` 与从 `tools_env` 拿到 `REPO_ROOT`。

[tools/tools_pythonpath.py:18-24](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/tools_pythonpath.py#L18-L24) —— 第 18 行注释说明「用 insert 优先本地检出」；第 23 行算出 `PATH_TO_TSFPGA`，第 24 行 `sys.path.insert(0, ...)`。

[tools/tools_pythonpath.py:29-30](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/tools_pythonpath.py#L29-L30) —— 对 `hdl-registers` 做同样处理。

[tools/tools_pythonpath.py:35-36](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/tools_pythonpath.py#L35-L36) —— 对 `vunit` 做同样处理。

#### 4.3.4 代码实践（源码阅读型）

**目标**：亲眼看到 `import tools_pythonpath` 之后 `sys.path` 的变化。

**步骤**（在仓库根目录执行）：

```python
import sys
before = list(sys.path)
import tools.tools_pythonpath  # noqa
after = sys.path
# 找出新增的、以 tsfpga/hdl-registers/vunit 结尾的路径
added = [p for p in after if p not in before]
print(added)
```

**需要观察的现象**：`added` 列表里会出现三条形如 `.../tsfpga/tsfpga`、`.../hdl-registers/hdl-registers`、`.../vunit/vunit` 的路径；如果你没检出这些兄弟仓库，路径仍会被插入（只是目录不存在）。

**预期结果**：三个本地路径被插到 `sys.path` 最前面。

**待本地验证**：实际输出取决于你是否检出了兄弟仓库以及它们的真实路径。

#### 4.3.5 小练习与答案

**练习 1**：为什么用 `insert(0, ...)` 而不是 `append(...)`？

> **答案**：`insert(0, ...)` 把本地检出放到搜索路径最前面，使其优先于已 pip 安装的版本，方便跨仓库联调；`append` 会让 pip 版本优先，达不到这个效果。

**练习 2**：如果你只通过 pip 安装了 tsfpga，没有本地检出，`import tools_pythonpath` 会报错吗？

> **答案**：不会。`sys.path.insert` 一个不存在的目录是无害的，Python 找不到就跳过，回退到 pip 安装位置。

---

### 4.4 入口脚本与两种集成方式

#### 4.4.1 概念说明

`tools/` 目录下有几个「入口脚本」，它们是用户直接用 `python tools/xxx.py` 运行的程序：

| 脚本 | 作用 |
|------|------|
| `simulate.py` | 跑 VUnit 仿真 |
| `synthesize.py` | 把某个实体快速综合成 netlist，做设计反馈 |
| `build_fpga.py` | 驱动完整的 FPGA 构建 |
| `build_docs.py` | 生成文档与寄存器代码 |
| `tag_release.py` | 发版 |

这些脚本都遵循**同一个引导套路**（以 `simulate.py` 为例）：

1. 用 `Path(__file__).parent.parent.resolve()` 算出 `REPO_ROOT` 并 `sys.path.insert(0, ...)`，让自己能被 `import`。
2. 紧接着 `import tools.tools_pythonpath`，把 tsfpga 等加进路径。
3. 之后才 `from tsfpga... import ...`。

这个顺序在 ruff 配置里有专门豁免——`tools/**/*.py` 允许 `E402`（import 不在文件顶部），就是因为必须先插路径再 import 第三方包：

[pyproject.toml:67-74](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/pyproject.toml#L67-L74) —— 对 `tools/**/*.py` 关闭 `E402`，注释解释：必须先 `import tools_pythonpath` 再 import 外部包。

而 tsfpga 还把 `tools_pythonpath` 当成一个自定义 isort 分组，排在标准库之后、第三方库之前：

[pyproject.toml:96-109](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/pyproject.toml#L96-L109) —— 定义 `tools_pythonpath` 分组及其在 import 顺序中的位置。

#### 4.4.2 核心流程：手动流程 vs tsfpga 流程

`getting_started.rst` 把使用方式明确分成两条路：

**A. tsfpga 流程（推荐）**：

```text
你的工程脚本
   │  sys.path.append(<hdl-modules 根>)   或设 PYTHONPATH
   │  from hdl_modules import get_hdl_modules
   ▼
get_hdl_modules()  ──►  返回 ModuleList（扫描 modules/）
   │
   ▼
调用每个模块的 get_synthesis_files() / get_simulation_files()
（库管理、文件归属、约束加载、寄存器生成都自动处理）
```

**B. 手动流程**：

```text
自己读 getting_started.rst 的规则
   │
   ├── src/*.vhd         → 加入「综合 + 仿真」工程，库名=模块名
   ├── test/*.vhd        → 只加入「仿真」工程
   ├── sim/*.vhd (BFM)   → 只加入「仿真」工程
   ├── scoped_constraints/*.tcl → 用 read_xdc -ref <实体> 加载
   └── 寄存器模块         → 自己跑 hdl-registers 生成 VHDL/C/C++
```

tsfpga 流程省心的关键，是 `get_hdl_modules()` 内部调用了 tsfpga 的 `get_modules()`，并传入了 `library_name_has_lib_suffix=False`（这就是 u1-l2 讲过的「库名等于模块名、无 lib 后缀」约定的来源）：

[hdl_modules/__init__.py:28-50](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/hdl_modules/__init__.py#L28-L50) —— `get_hdl_modules()` 包装 tsfpga 的 `get_modules()`，扫描 `REPO_ROOT/"modules"`，并设置 `library_name_has_lib_suffix=False`。注释说明：tsfpga 在某些系统上可能不可用，所以 `tsfpga` 的 import 放在函数体内而非文件顶部。

#### 4.4.3 源码精读

入口脚本的标准引导套路，以仿真入口为例：

[tools/simulate.py:14-19](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/simulate.py#L14-L19) —— 第 15-16 行算出 `REPO_ROOT` 并插入 `sys.path`；第 18-19 行注释「先 import 它，因为它会修改 PYTHONPATH」，随后 `import tools.tools_pythonpath`。

[tools/simulate.py:21-28](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/simulate.py#L21-L28) —— 在路径就绪后，才从 `tsfpga.examples.simulation_utils` 与 `tsfpga.module` 导入所需符号，并 `from tools import tools_env`。

[tools/simulate.py:31-55](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/simulate.py#L31-L55) —— `main()`：用 tsfpga 的 `get_arguments_cli` 解析命令行，默认输出目录设为 `tools_env.HDL_MODULES_GENERATED`；用 `get_modules(modules_folder=tools_env.HDL_MODULES_DIRECTORY)` 扫描模块；构建 `SimulationProject` 并加入模块、加入 Vivado simlib，最后交给 VUnit 的 `main()` 运行。注意第 32 行和第 35 行都直接复用了 `tools_env` 里的常量——这就是 4.2 节「单一信息源」被消费的地方。

再看 `getting_started.rst` 对两条流程的权威描述：

[doc/sphinx/getting_started.rst:37-49](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/doc/sphinx/getting_started.rst#L37-L49) —— tsfpga 流程：调用 `get_hdl_modules()`，并提示调用 `get_simulation_files()` 时把 `include_tests` 设为 `False`，以免白白跑测试台。

[doc/sphinx/getting_started.rst:51-64](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/doc/sphinx/getting_started.rst#L51-L64) —— 手动流程：`src/` 进综合+仿真工程，`test/`、`sim/` 只进仿真工程，所有文件按 VHDL-2008 处理，库名等于模块名。

#### 4.4.4 代码实践（源码阅读型）

**目标**：验证「所有入口脚本共用同一个引导套路」。

**步骤**：

1. 打开 `tools/simulate.py`、`tools/build_fpga.py`、`tools/synthesize.py`。
2. 对比每个文件的开头 10～20 行。
3. 找到这三处都出现的两行代码：`sys.path.insert(0, str(REPO_ROOT))` 和 `import tools.tools_pythonpath`。

**需要观察的现象**：三个脚本的引导部分几乎一模一样。

**预期结果**：你会确认 hdl-modules 的工具脚本遵循统一模板——先把仓库根加进路径，再 import `tools_pythonpath`，最后才 import 第三方包。以后你新写一个 `tools/` 脚本，照抄这个开头即可。

#### 4.4.5 小练习与答案

**练习 1**：`get_hdl_modules()` 为什么不把 `from tsfpga.module import get_modules` 写在文件顶部，而写在函数体里？

> **答案**：因为 tsfpga 在某些只取用 VHDL 源码的系统上可能未安装。把 import 延迟到函数内部，意味着只有真正调用 `get_hdl_modules()`（即确实要用 tsfpga 集成）的人才需要装 tsfpga，其他人 import `hdl_modules` 包本身不会失败。

**练习 2**：tsfpga 流程相比手动流程，省掉了哪些手工步骤？

> **答案**：省掉了按 `src/test/sim` 手动分类文件、手动设库名、手动用 `read_xdc -ref` 加载作用域约束、手动跑 hdl-registers 生成寄存器代码等步骤——这些都由 tsfpga 在 `get_synthesis_files()` / `get_simulation_files()` 与构建流程中自动处理。

---

## 5. 综合实践

把本讲四个模块串起来，完成一次「环境就绪性自检」。这是本讲的主实践任务。

**实践目标**：亲手让仿真入口脚本的 `--help` 跑通，从而一次性验证「依赖已装、PYTHONPATH 已通、入口可用」。

**前提**：已按 u1-l1 克隆仓库；本机有 Python 环境。

**操作步骤**：

1. **安装 VUnit 预发布版**（依赖来源见 [doc/sphinx/getting_started.rst:24-28](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/doc/sphinx/getting_started.rst#L24-L28)）：
   ```bash
   python -m pip install vunit-hdl==5.0.0.dev5
   ```

2. **安装 tsfpga**（`simulate.py` 第 21-26 行 import 了 `tsfpga`，因此必需；安装方法见 tsfpga 官方 installation 文档）：
   ```bash
   python -m pip install tsfpga
   ```
   > 说明：`tools/tools_pythonpath.py` 期望 tsfpga 可被 import。如果你用的是兄弟目录本地检出而非 pip，请把它放在 `<父目录>/tsfpga/tsfpga`，`tools_pythonpath` 会自动发现。

3. **配置 PYTHONPATH 指向仓库根目录**（让 `import tools.tools_pythonpath` 与 `from hdl_modules...` 可用）：
   ```bash
   export PYTHONPATH="<仓库根目录的绝对路径>"
   ```
   > 小提示：其实 `simulate.py` 自己会在第 15-16 行把 `REPO_ROOT` 插进 `sys.path`，所以从仓库根目录直接 `python tools/simulate.py` 通常也能跑；显式设 `PYTHONPATH` 对你在任意目录、或在自己的脚本里集成时更稳妥。

4. **运行 `--help` 确认入口可用**：
   ```bash
   python tools/simulate.py --help
   ```

**需要观察的现象**：

- 命令不应报 `ModuleNotFoundError: No module named 'tsfpga'` 或 `'vunit'`。
- 应打印出 tsfpga 的 `get_arguments_cli` 提供的命令行帮助（包含输出路径、测试过滤、Vivado 选项等参数），默认输出路径里会体现 `tools_env.HDL_MODULES_GENERATED`（即 `<根目录>/generated`）。

**预期结果**：看到完整的帮助文本，且默认输出路径指向仓库下的 `generated/` 目录。

**如果失败，按本讲学到的排错**：

| 报错 | 可能原因 | 对照本节 |
|------|---------|---------|
| `No module named 'tools'` | 没设 PYTHONPATH，或不在仓库根 | 4.4 / 步骤 3 |
| `No module named 'tsfpga'` | tsfpga 未装，且无本地检出 | 4.3 / 步骤 2 |
| `No module named 'vunit'` | VUnit 未装 | 4.1 / 步骤 1 |

**待本地验证**：本讲不假装已替你运行上述命令。不同机器上 VUnit/tsfpga 的可安装版本与 Vivado 是否在 `PATH` 中都会影响结果；请以你本地实际输出为准。`--help` 本身不需要 Vivado，是检验「Python 侧依赖与路径」的最轻量探针。

## 6. 本讲小结

- hdl-modules 的运行时依赖是 **tsfpga、VUnit（5.0.0+ 预发布，`vunit-hdl==5.0.0.dev5`）、hdl-registers**，外加 EDA 工具 **Vivado**；但只要不用测试台与 BFM，可综合源码**零依赖**。
- 依赖写在 `doc/sphinx/getting_started.rst`，**不在** `pyproject.toml`——后者只配置 ruff 代码检查，没有 `[project].dependencies`。
- `tools/tools_env.py` 用 `Path(__file__).parent.parent.resolve()` 推导出 `REPO_ROOT` 及 `HDL_MODULES_DIRECTORY`/`HDL_MODULES_DOC`/`HDL_MODULES_GENERATED`，是全工具链的路径单一信息源，且零外部依赖。
- `tools/tools_pythonpath.py` 用 `sys.path.insert(0, ...)` 把 tsfpga/hdl-registers/vunit 的本地检出插到最前，**优先本地检出、回退 pip 安装**。
- `tools/` 下每个入口脚本都遵循「算 REPO_ROOT → insert sys.path → import tools_pythonpath → 再 import 第三方包」的统一引导套路；ruff 用 `E402` 豁免配合这条约定。
- 使用方式分两条路：**tsfpga 流程**（调 `get_hdl_modules()`，省心，自动管库/约束/寄存器）与**手动流程**（按 `src/test/sim` 自己分类文件、按 VHDL-2008 编译、库名等于模块名）。

## 7. 下一步学习建议

环境打通后，下一步是把焦点从「Python 工具链」转回「VHDL 模块本身」：

- **u1-l4（Python 入口与 tsfpga Module 模式）**：继续在工具链层面，深入 `get_hdl_modules()` 的扫描机制与每个模块 `module_*.py` 的 `setup_vunit`/`get_build_projects` 钩子，承接本讲的入口讨论。
- **u2-l1（握手约定）**：进入第 2 单元，学习贯穿全项目的 ready/valid 握手——这是理解 `fifo`、`axi_stream` 等几乎所有模块的前提。
- **想先试跑一个真实仿真**：可在完成本讲综合实践后，挑选 `modules/common` 或 `modules/fifo`，用 `python tools/simulate.py --minimal --gui` 等参数（具体以 `--help` 输出为准）跑一个测试台，直观感受 VUnit 流程。

建议按 u1-l4 → u2 单元的顺序推进，先补齐「模块在 Python 侧如何被声明与配置」，再进入 VHDL 设计本身。
