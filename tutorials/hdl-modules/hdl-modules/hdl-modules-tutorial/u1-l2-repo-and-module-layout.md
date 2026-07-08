# 仓库布局与单个模块的目录约定

## 1. 本讲目标

上一讲（u1-l1）我们建立了对 hdl-modules 项目的全局认知：它是一组可复用、同行评审的 VHDL-2008 构建块。本讲把镜头从「项目是什么」推进到「项目长什么样」——也就是仓库的目录布局。

学完本讲，你应当能够：

1. 说出仓库**顶层目录**（`modules`、`tools`、`test`、`doc`、`hdl_modules`）各自的职责分工。
2. 拿到任意一个模块目录，能识别其中 `src`、`test`、`sim`、`doc`、`rtl`、`scoped_constraints` 各子目录的作用，并判断哪些文件该进**综合工程**、哪些只该进**仿真工程**。
3. 理解两条贯穿全项目的硬约定：**VHDL 库名与模块名同名**、**所有文件按 VHDL-2008 处理**。

这三点是后续阅读任何模块源码的「地图」，没有这张地图，面对上百个 `.vhd` 文件会无从下手。

## 2. 前置知识

- **FPGA 工程的两类文件集合**：一个 FPGA 项目通常要区分两套文件清单——
  - **综合工程（synthesis/build project）**：最终会被综合成硬件电路的文件，也就是真正「上芯片」的代码。
  - **仿真工程（simulation project）**：用来跑测试台（testbench）的文件集合，除了综合工程的文件，还包含只用于仿真、不会上芯片的测试代码。
- **测试台（testbench）**：一段不综合、只在仿真时运行的 VHDL 代码，用来给被测实体喂输入、检查输出。
- **BFM（Bus Functional Model，总线功能模型）**：仿真专用的「假」主/从设备，用来模拟 AXI 等总线的对端，不会综合成硬件。
- **VHDL 库（library）**：VHDL 把代码组织进不同的「库」里，引用别的库要先写 `library <名字>;`。库名是一个编译期概念。
- **VHDL-2008**：VHDL 语言标准的一个版本，相比老版本增加了不少便捷语法（如 `else generate`、条件赋值中的函数调用等）。本项目所有文件都按这个标准编译。
- **约束文件（constraint, `.tcl`/`.xdc`）**：告诉综合/布局布线工具关于时序的额外要求（例如「这条路径不需要满足常规时序」），不是 VHDL 代码。

如果你对 generic、AXI、CDC 这些词还陌生，不用担心，它们会在后续讲义中专门讲解；本讲只关心**文件放在哪里、归到哪个工程**。

## 3. 本讲源码地图

本讲主要依据项目自带的「入门指南」，并实地查看 `resync`、`fifo` 两个模块的真实目录结构。涉及的关键文件：

| 文件 / 目录 | 作用 |
| --- | --- |
| `doc/sphinx/getting_started.rst` | 项目官方入门文档，明确写出了目录约定与「手动流程」规则。本讲的「规则依据」主要来自这里。 |
| `hdl_modules/__init__.py` | Python 包入口，定义 `get_hdl_modules()`，并显式声明了库名约定。 |
| `modules/resync/` | 跨时钟域（CDC）模块，本讲的核心示例。包含 `src`、`test`、`scoped_constraints`、`doc`。 |
| `modules/fifo/` | FIFO 模块，比 resync 多出一个 `rtl/` 目录，用来对比说明 `rtl` 的用途。 |
| `modules/resync/readme.rst`、`modules/fifo/readme.rst` | 每个模块的简短说明文件。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**4.1 顶层目录布局**、**4.2 单个模块内的子目录约定**、**4.3 库名与 VHDL-2008 约定**。

### 4.1 顶层目录的职责分工

#### 4.1.1 概念说明

把仓库克隆下来后，根目录下最值得关注的顶层目录有五个：

| 顶层目录 | 职责 |
| --- | --- |
| `modules/` | **核心**。所有可复用 VHDL 构建块都住在这里，每个子目录是一个模块（如 `modules/resync`、`modules/fifo`）。 |
| `tools/` | Python 工具脚本入口，负责驱动仿真、综合、构建、文档生成、发版等流程（如 `simulate.py`、`build_fpga.py`、`synthesize.py`）。 |
| `test/` | 项目自身（非单个模块）的 Python 测试，例如版权头检查、Python lint（`test/lint/`）。 |
| `doc/` | Sphinx 文档源文件，用来生成 hdl-modules.com 网站。 |
| `hdl_modules/` | Python 包，提供 `get_hdl_modules()` 入口，让 tsfpga 能把 `modules/` 下的模块扫描进来。 |

此外根目录还有 `readme.rst`（项目首页）、`license.txt`（BSD 3-Clause 许可证）、`pyproject.toml`（Python 打包与工具配置）。

一句话记忆：**`modules/` 是「货」，`tools/` 是「流水线」，`hdl_modules/` 是把货登记进流水线的「清单接口」，`test/` 与 `doc/` 则是项目自身的质检与说明**。

#### 4.1.2 核心流程

从「拿到代码」到「跑起来」，顶层目录是这样协作的（tsfpga 流程）：

```text
克隆仓库
  │
  ├─ 设置 PYTHONPATH 指向仓库根目录
  │
  ▼
hdl_modules/__init__.py :: get_hdl_modules()
  │   扫描 modules/ 目录，返回 ModuleList
  ▼
tools/simulate.py   → 取出模块 → 编译 src + test → 跑仿真
tools/build_fpga.py → 取出模块 → 只编译 src → 综合/构建
tools/build_docs.py → 读取 doc/ + 各模块 doc/ → 生成网站
  │
  ▼
test/ 里的 Python 测试在 CI 中检查项目自身规范
```

即使是「手动流程」（不用 tsfpga），顶层目录的分工也不变：你从 `modules/<某模块>/src/` 里挑文件加进自己的工程即可。

#### 4.1.3 源码精读

官方入门文档在「Source code」一节里说明了如何获取源码，并给出了 tsfpga 与手动两种方式：

[doc/sphinx/getting_started.rst:34-49](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/doc/sphinx/getting_started.rst#L34-L49) —— 说明使用 tsfpga 时调用 `get_hdl_modules()` 即可拿到模块对象，随后可用 `get_synthesis_files()`、`get_simulation_files()` 等方法取文件清单。

而 `get_hdl_modules()` 的定义就在 Python 包入口里：

[hdl_modules/__init__.py:28-50](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/hdl_modules/__init__.py#L28-L50) —— 该函数把 `modules/` 目录作为扫描根，返回 `tsfpga` 的 `ModuleList`。注意它把 `REPO_ROOT` 定位为本文件上一层目录（即仓库根），所以无论你在哪个工程里引用它，`modules/` 的位置都是确定的。

#### 4.1.4 代码实践

**实践目标**：建立顶层目录的肌肉记忆。

**操作步骤**：

1. 在本地克隆仓库后，进入仓库根目录，列出顶层条目。
2. 对照本讲表格，给每个顶层目录写一句「它装的是什么」。

**需要观察的现象**：你应当看到 `modules/`、`tools/`、`test/`、`doc/`、`hdl_modules/` 五个目录都在；`modules/` 下有 14 个子目录（从 `axi` 到 `sine_generator`）。

**预期结果**：能与本讲表格一一对应。若你只看到部分目录，说明克隆不完整或身处子目录。

> 待本地验证：具体目录名以你本地 `ls` 结果为准。

#### 4.1.5 小练习与答案

**练习 1**：如果有人问你「hdl-modules 的源码主体放在哪个顶层目录？」你怎么回答？

**参考答案**：`modules/`。每个子目录是一个独立的可复用模块。

**练习 2**：`hdl_modules/`（带下划线的 Python 包）和 `modules/`（带斜杠的源码目录）有什么区别？

**参考答案**：`modules/` 是真正的 VHDL 源码所在地；`hdl_modules/` 是一个 Python 包，里面几乎没有 VHDL，只提供 `get_hdl_modules()` 这个「登记接口」，让 tsfpga 能发现并扫描 `modules/` 下的模块。

---

### 4.2 单个模块内的子目录约定

#### 4.2.1 概念说明

进入任意一个模块目录（例如 `modules/resync/`），你会看到一组**职责固定**的子目录与文件。这是全项目统一的约定，记住这套约定，阅读任何模块都能快速定位：

| 子目录 / 文件 | 是否综合 | 是否仿真 | 作用 |
| --- | --- | --- | --- |
| `src/` | ✅ 加入综合 | ✅ 加入仿真 | **可综合源码**，模块真正的硬件实现。 |
| `test/` | ❌ 不综合 | ✅ 加入仿真 | **测试台**（`tb_*.vhd`），只在仿真里跑。 |
| `sim/` | ❌ 不综合 | ✅ 加入仿真 | **仿真模型 / BFM**，模拟总线对端，不综合。 |
| `scoped_constraints/` | 仅综合期应用 | ❌ | **作用域约束**（`.tcl`），按实例施加时序约束。 |
| `doc/` | ❌ | ❌ | 模块文档（`.rst` + 图片），供 Sphinx 生成网页。 |
| `rtl/` | ❌（仅构建期） | 通常不加入 | **构建/资源回归用的顶层封装**，只在 netlist 构建里用，不属于用户综合工程。 |

> 注意：**并非每个模块都拥有全部子目录**。例如 `resync` 没有 `sim/` 和 `rtl/`；`fifo` 有 `rtl/` 但没有 `sim/`；`bfm` 模块则只有 `sim/`（它本身就是仿真模型）。子目录「按需出现」。

还有两个固定文件：

- `readme.rst`：模块的一句话说明，例如 [modules/resync/readme.rst](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/readme.rst) 写明它「包含一组用于 CDC 的实体」，并指向网站文档。
- `module_<名字>.py`：模块的 Python 描述文件（如 `module_resync.py`），继承自 tsfpga 的 `BaseModule`，用来配置仿真与构建（下一讲 u1-l4 详讲）。

#### 4.2.2 核心流程

判断「某个文件该进哪个工程」的核心决策，可以用下面这张流程图概括（这也是 `getting_started.rst` 在「Manual workflow」里写明的规则）：

```text
              一个 .vhd / .tcl 文件
                      │
          ┌───────────┴───────────┐
          ▼                       ▼
     在 src/ 里吗？          在 test/ 或 sim/ 里吗？
          │                       │
     是 ───┴─── 否            是 ───┴─── 否
      │           │             │         │
      ▼           │             ▼         ▼
  综合 + 仿真     │          只进仿真     在 scoped_constraints/ 里吗？
                 │                       │
                 │                  是 ───┴─── 否
                 │                  │          │
                 │                  ▼          ▼（多半是 doc/ 或 .py）
                 │            仅综合期应用    不进工程
```

浓缩成三条规则：

1. **`src/` → 综合工程 + 仿真工程都要**。
2. **`test/` 和 `sim/` → 只进仿真工程，绝不进综合工程**。
3. **`scoped_constraints/*.tcl` → 不是 VHDL，不参与编译，只在综合期通过工具命令「按实例」施加**。

#### 4.2.3 源码精读

官方文档的「Manual workflow」小节把上述规则写得非常明确：

[doc/sphinx/getting_started.rst:51-64](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/doc/sphinx/getting_started.rst#L51-L64) —— 其中写到：可综合源码在每个模块的 `src` 目录里，应同时加入仿真和构建工程；测试台在 `test` 目录；仿真代码（BFM）在 `sim` 目录，**应加入仿真工程但不加入构建工程**；所有文件按 VHDL-2008 处理。

**`src/` 的样子**——以 resync 的电平同步实体为例，它的文件头注释直接说明这是一个用两个 `async_reg` 寄存器做跨时钟域同步的可综合实体，并且显式提示它配有约束文件：

[modules/resync/src/resync_level.vhd:9-22](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/src/resync_level.vhd#L9-L22) —— 这是会上芯片的真实硬件实现，属于「综合 + 仿真」都要的文件。

**`test/` 的样子**——测试台引用了 `vunit_lib`（VUnit 测试框架）和别的模块库，定义了一个 `tb_...` 实体：

[modules/resync/test/tb_resync_twophase.vhd:14-33](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/test/tb_resync_twophase.vhd#L14-L33) —— 注意它 `use vunit_lib.check_pkg.all;`，因此依赖 VUnit（与入门文档「Dependencies」一节一致）；它只进仿真工程。

**`scoped_constraints/` 的样子**——`.tcl` 文件，与对应实体同名（`resync_level.tcl` ↔ `resync_level`），内容是 Vivado 时序命令：

[modules/resync/scoped_constraints/resync_level.tcl:18-53](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/scoped_constraints/resync_level.tcl#L18-L53) —— 它用 `get_cells`/`get_clocks` 找到同步链上的寄存器，再用 `set_max_delay` 或退路的 `set_false_path` 施加约束。它不是 VHDL，只在综合期被加载。

**「scoped（作用域）」的含义**——官方文档专门解释：约束「作用域化」指它相对于某个实体的**每一个实例**施加，这样就不必在整个设计层次里大海捞针地找信号。手动流程下用如下命令加载（`-ref` 指定实体实例）：

[doc/sphinx/getting_started.rst:79-93](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/doc/sphinx/getting_started.rst#L79-L93) —— 示例命令 `read_xdc -ref asynchronous_fifo .../asynchronous_fifo.tcl`。

**`rtl/` 的样子**（仅部分模块有）——以 fifo 为例，这是一个只暴露「骨架」端口的封装，文件头注释写明用途：

[modules/fifo/rtl/fifo_netlist_build_wrapper.vhd:9-11](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/rtl/fifo_netlist_build_wrapper.vhd#L9-L11) —— 注释说它「只引出 FIFO 的 barebone 端口，用于 netlist 构建中的面积断言」。也就是说它是一个**构建期测试夹具**，用来在综合后检查资源占用，不属于用户真正要综合进产品的源码。

#### 4.2.4 代码实践

这是本讲的核心实践任务：**以 `resync` 模块为例，列出其各文件的归属类别**。

**实践目标**：把 resync 目录下的真实文件填进「综合 / 仿真」分类表，检验你是否掌握了 4.2.2 的三条规则。

**操作步骤**：

1. 进入 `modules/resync/`，列出 `src/`、`test/`、`scoped_constraints/` 下的文件（可用 `git ls-files modules/resync`）。
2. 对照规则，把每个文件归入下表的某一列。

下面给出已核对过的真实文件清单与参考分类：

| 文件 | 所在子目录 | 进综合工程？ | 进仿真工程？ |
| --- | --- | --- | --- |
| `src/resync_level.vhd` | `src/` | ✅ | ✅ |
| `src/resync_counter.vhd` | `src/` | ✅ | ✅ |
| `src/resync_pulse.vhd` | `src/` | ✅ | ✅ |
| `src/resync_twophase.vhd` | `src/` | ✅ | ✅ |
| `src/resync_twophase_handshake.vhd` | `src/` | ✅ | ✅ |
| `src/resync_cycles.vhd` | `src/` | ✅ | ✅ |
| `src/resync_slv_level.vhd` 等（其余 `src/*.vhd`） | `src/` | ✅ | ✅ |
| `test/tb_resync_twophase.vhd` | `test/` | ❌ | ✅ |
| `test/tb_resync_counter.vhd` | `test/` | ❌ | ✅ |
| `test/tb_resync_pulse.vhd` | `test/` | ❌ | ✅ |
| `test/tb_resync_twophase_handshake.vhd` | `test/` | ❌ | ✅ |
| `scoped_constraints/resync_level.tcl` | `scoped_constraints/` | 仅综合期应用（非编译单元） | ❌ |
| `scoped_constraints/resync_counter.tcl` 等 | `scoped_constraints/` | 仅综合期应用 | ❌ |
| `doc/resync.rst`、`module_resync.py`、`readme.rst` | 其他 | ❌ | ❌ |

**需要观察的现象**：

- `resync` 模块**没有 `sim/` 目录**——这正常，因为它不提供 BFM。
- `src/` 下每个可综合实体，几乎都能在 `scoped_constraints/` 找到**同名**的 `.tcl`（如 `resync_level` ↔ `resync_level.tcl`）。这种同名配对是「作用域约束」机制的体现。
- 所有 `test/` 文件名都以 `tb_` 开头，并以 `_tb` 之外的实体名为后缀，指明它测的是哪个实体。

**预期结果**：综合工程只收 `src/` 的 `.vhd`；仿真工程收 `src/` + `test/` 的 `.vhd`；约束 `.tcl` 单独用 `read_xdc -ref` 加载；`doc/` 与 `.py` 都不进工程。

> 待本地验证：用 `git ls-files modules/resync` 自行列出，并与上表对照。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `test/` 里的文件不能进综合工程？

**参考答案**：测试台依赖 VUnit 框架（`vunit_lib`），且包含不会综合成硬件的检查/等待逻辑；把它们综合进芯片既无意义也会报错。它们只用于仿真验证。

**练习 2**：`scoped_constraints/resync_level.tcl` 和 `src/resync_level.vhd` 文件名相同是巧合吗？

**参考答案**：不是巧合，而是约定。「作用域约束」要求 `.tcl` 文件与实体同名，工具才能通过 `-ref <实体名>` 把约束自动定位到该实体的每一个实例。

**练习 3**：`fifo` 模块比 `resync` 多了一个 `rtl/` 目录，这个目录里的文件该进综合工程吗？

**参考答案**：不该。`rtl/` 里是 netlist 构建用的顶层封装（如 `fifo_netlist_build_wrapper.vhd`），只在「资源占用回归」的综合检查里使用，不属于用户产品的综合源码。

---

### 4.3 VHDL 库名与 VHDL-2008 处理约定

#### 4.3.1 概念说明

除了目录划分，项目还有两条影响「如何编译」的硬约定：

1. **库名 = 模块名**。每个模块编译进一个与它同名的 VHDL 库。例如 `resync` 模块的源码编译进名为 `resync` 的库；`common` 模块编译进名为 `common` 的库。注意是「裸名」——不带 `lib` 后缀（即不是 `resync_lib`）。
2. **所有文件按 VHDL-2008 处理**。无论综合还是仿真，编译器都要开 VHDL-2008 模式，否则部分语法会编译失败。

这两条约定让你在引用别的模块时很省心：要用 resync 的实体，就在文件头写 `library resync;`，不需要去查它的库到底叫什么。

#### 4.3.2 核心流程

库名是如何被确定的？在 tsfpga 流程下：

```text
get_hdl_modules()
  │  扫描 modules/ 下每个子目录
  │  调用 tsfpga.get_modules(..., library_name_has_lib_suffix=False)
  │                                  ↑ 关键参数：库名不加 lib 后缀
  ▼
每个模块对象 → library_name 属性 = 模块目录名（如 "resync"、"common"）
  ▼
编译时：该模块 src/ 下所有文件 → 编译进名为 <模块名> 的库
```

而每个 `.vhd` 文件内部用 `library <名字>;` 来引用别的库。由于库名就是模块名，这种引用天然可读。

#### 4.3.3 源码精读

库名不加后缀的约定在 Python 入口里是显式声明的：

[hdl_modules/__init__.py:45-50](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/hdl_modules/__init__.py#L45-L50) —— 调用 `get_modules(..., library_name_has_lib_suffix=False)`，把扫描根设为 `REPO_ROOT / "modules"`。`library_name_has_lib_suffix=False` 正是「库名不带 lib 后缀、等于模块名」的来源。

官方文档同样写明：

[doc/sphinx/getting_started.rst:56-64](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/doc/sphinx/getting_started.rst#L56-L64) —— 「The library name is the same as the module name」，以及「All files must be handled as VHDL-2008」。

在真实源码里验证一下「库名 = 模块名」。resync 的测试台引用了 `common` 模块的工具包：

[modules/resync/test/tb_resync_twophase.vhd:18-19](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/test/tb_resync_twophase.vhd#L18-L19) —— `library common;` 后接 `use common.time_pkg.to_real_s;`。这里的 `common` 正是 `modules/common` 模块的库名，与模块目录名完全一致。

#### 4.3.4 代码实践

**实践目标**：验证「库名 = 模块名」在源码里处处成立。

**操作步骤**：

1. 打开 `modules/resync/test/tb_resync_twophase.vhd`，找到所有 `library ...;` 行。
2. 对每个库名，去 `modules/` 下找是否有同名目录。

**需要观察的现象**：你会看到 `ieee`（标准库）、`vunit_lib`（VUnit 框架库）、`common`（本项目模块库）。其中 `common` 对应 `modules/common/`。

**预期结果**：除标准库与第三方库外，本项目的库名都能在 `modules/` 下找到同名目录。

> 待本地验证：可对 `modules/resync/test/` 下所有文件搜索 `^library ` 开头的行，逐一核对。

#### 4.3.5 小练习与答案

**练习 1**：如果要在某个文件里使用 `math` 模块的 `math_pkg`，该写哪两行？

**参考答案**：

```vhdl
library math;
use math.math_pkg.all;
```

因为库名与模块名相同，都是 `math`。

**练习 2**：为什么项目强调「所有文件按 VHDL-2008 处理」？

**参考答案**：源码里用到了 VHDL-2008 才支持的语法（如条件/层次化 `generate`、`else generate` 等）。若编译器按老标准（如 VHDL-2002）编译，这些构造会报错。手动建工程时必须在编译选项里显式开启 VHDL-2008。

## 5. 综合实践

把本讲三个最小模块串起来，完成下面这张「全分类」任务。

**任务**：选取 `fifo` 模块（因为它同时拥有 `src/`、`test/`、`rtl/`、`scoped_constraints/`、`doc/`，结构比 resync 更全），制作一张完整的「文件归属表」，要求：

1. 用 `git ls-files modules/fifo` 列出全部文件。
2. 给每个文件标注：**进综合工程 / 只进仿真工程 / 仅综合期约束应用 / 不进工程**。
3. 任选一个 `src/` 文件和一个 `test/` 文件，打开后核对它的 `library ...;` 行，确认引用的本项目库名都能在 `modules/` 下找到同名目录。
4. 思考题：`fifo` 有 `rtl/fifo_netlist_build_wrapper.vhd`，但 resync 没有 `rtl/` 目录。结合 4.2.3 中该文件头注释的「size assertions in netlist builds」，推测为什么有的模块需要 `rtl/`、有的不需要。

**参考思路**：

- `src/asynchronous_fifo.vhd`、`src/fifo.vhd`、`src/fifo_wrapper.vhd` → 综合 + 仿真。
- `test/tb_fifo.vhd`、`test/tb_asynchronous_fifo.vhd` → 只仿真。
- `scoped_constraints/asynchronous_fifo.tcl` → 仅综合期应用。
- `rtl/fifo_netlist_build_wrapper.vhd` → 仅 netlist 构建用，不进用户综合工程。
- `doc/`、`module_fifo.py`、`readme.rst` → 不进工程。
- 思考题提示：`rtl/` 封装是为「资源占用回归」准备的可综合顶层夹具；当一个实体本身的端口已经足够干净、能直接作为综合顶层时，就不必再包一层 `rtl/`。fifo 因为要按不同 generic 组合做面积断言，所以提供了统一封装。

## 6. 本讲小结

- 仓库顶层分为 `modules/`（源码主体）、`tools/`（流程脚本）、`test/`（项目自测）、`doc/`（文档源）、`hdl_modules/`（Python 登记接口）。
- 单个模块内部按职责划分子目录：`src/`（可综合）、`test/`（测试台）、`sim/`（BFM）、`scoped_constraints/`（作用域约束）、`doc/`（模块文档）、`rtl/`（构建期封装）。子目录按需出现，不是每个模块都全有。
- 三条归类规则：`src/` 进综合+仿真；`test/` 与 `sim/` 只进仿真；`scoped_constraints/*.tcl` 不是 VHDL，只在综合期按实例施加。
- 「作用域约束」要求 `.tcl` 与实体同名，用 `read_xdc -ref <实体名>` 加载，避免在全设计层次里找信号。
- 库名 = 模块名（不带 `lib` 后缀），由 `get_hdl_modules()` 的 `library_name_has_lib_suffix=False` 决定，引用别的模块只需 `library <模块名>;`。
- 所有文件一律按 VHDL-2008 编译，手动建工程时必须开启该模式。

## 7. 下一步学习建议

本讲让你看清了「文件放哪、归哪个工程」，但还没讲「这些工具脚本到底怎么把工程跑起来」。建议下一步：

1. **学习 u1-l3《工具链与依赖：如何仿真与构建》**：搞清 `tools/` 下 `simulate.py`、`build_fpga.py` 的入口作用，以及 tsfpga / VUnit / hdl-registers 依赖关系。
2. **学习 u1-l4《Python 入口与 tsfpga Module 模式》**：深入 `module_*.py` 里 `setup_vunit` 与 `get_build_projects` 两个钩子，理解本讲提到的 `rtl/` 封装是如何被 `get_build_projects` 当作综合顶层使用的。
3. **动手预热**：在本地用 `git ls-files modules/<任意模块>` 多看几个模块的目录结构，验证「子目录按需出现」与「库名=模块名」两条规律。
