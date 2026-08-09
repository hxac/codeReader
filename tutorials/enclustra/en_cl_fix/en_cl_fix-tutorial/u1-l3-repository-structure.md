# 仓库目录结构与代码组织

## 1. 本讲目标

学完本讲，你应当能够：

- 说出 `hdl/`、`bittrue/models/`、`bittrue/cosim/`、`bittrue/tests/`、`tb/`、`sim/`、`lib/en_tb/` 这几个顶层目录各自的职责。
- 解释为什么 RTL 与 testbench 要用不同的 VHDL 标准（VHDL-93 vs VHDL-2008）。
- 看懂 Python 参考模型包 `en_cl_fix_pkg` 是如何通过 `__init__.py` 的 `from … import *` 把五个模块拼装成一个对外统一接口的。
- 读懂 `sim/run.py` 如何用 VUnit 把「Python 黄金参考生成」与「VHDL 仿真对拍」串成一条流水线，并理解它在其中注册了哪三个库。
- 画出 Python 模型、cosim 脚本、VHDL testbench 与 en_tb 之间的调用与文件流向图。

## 2. 前置知识

本讲是**结构导航课**，不涉及任何定点算法细节，只要具备以下直觉即可（这些在 u1-l1、u1-l2 已建立）：

- **定点数 / [S, I, F] 格式**：用「符号位 S + 整数位 I + 小数位 F」描述一个二进制定点数，总位宽为 `S+I+F`。
- **RTL 与 testbench 的区分**：RTL（Register Transfer Level，寄存器传输级）是可以被综合工具转成真实电路的代码；testbench 是只用于仿真、用来给 RTL 喂输入并检查输出的代码，不可综合。
- **Co-simulation（协同仿真）**：让 Python 算出「正确答案」（黄金参考），再用 VHDL 仿真跑同样的输入，最后比对二者是否位级一致（bit-true）。

补充两个本讲会用到的 Python / VHDL 工程概念：

- **Python 包（package）**：一个含 `__init__.py` 的目录。`__init__.py` 是包的「入口文件」，在其中写 `from .xxx import *`，可以把子模块里的名字「提升」到包的顶层，这样外部用 `from en_cl_fix_pkg import *` 就能一次性拿到全部公开 API。
- **VHDL library / context**：VHDL 编译时会把代码归入某个「库」（library），使用别的库要先 `library xxx;`。VHDL-2008 引入了 `context`（上下文），把一组 `library` + `use` 子句打包，引用一句即可，en_tb 库就大量使用了 context。

> 术语提示：目录名 `bittrue` 来自 **bit-true（位级精确）**，指这套软件模型是与硬件逐位一致的参考实现。这是理解整个目录命名的一把钥匙。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 / 目录 | 作用 |
| --- | --- |
| `README.md` | 项目说明，含「如何运行测试」与 RTL/testbench 的 VHDL 标准约定。 |
| `hdl/` | 可综合 RTL 源码（VHDL-93），即真正会变成电路的代码。 |
| `bittrue/models/python/en_cl_fix_pkg/` | Python 参考模型包，本讲重点看 `__init__.py`。 |
| `bittrue/models/matlab/` | MATLAB 包装器，逐一对应 Python 接口。 |
| `bittrue/cosim/` | 协同仿真脚本：穷举格式、生成黄金参考数据文件。 |
| `bittrue/tests/` | Python / MATLAB 单元测试。 |
| `tb/` | VHDL testbench（VHDL-2008），读数据文件、跑 VHDL 函数、对拍。 |
| `tb/util/` | testbench 辅助包，例如文件 I/O 包装。 |
| `sim/run.py` | 仿真主入口，用 VUnit 组织 testbench 并触发 cosim。 |
| `sim/common.py` | 仿真器与 VHDL 标准配置。 |
| `sim/cosim_runner.py` | 线程安全地「只跑一次」cosim 脚本的运行器。 |
| `lib/en_tb/` | 内嵌的 Enclustra 通用 testbench 库（文件 I/O 等基础设施）。 |

## 4. 核心概念与源码讲解

### 4.1 顶层目录划分

#### 4.1.1 概念说明

打开 en_cl_fix 仓库，你会看到几个并列的顶层目录。它们不是随意堆放的，而是严格对应「**一个算法，三种语言，四类产物**」的设计：

- **一个算法**：定点加法、乘法、舍入、饱和……每种运算都有一份确定的数学定义。
- **三种语言**：VHDL（给硬件用）、Python（给软件开发和仿真参考用）、MATLAB（给算法工程师用）。
- **四类产物**：① 可综合 RTL；② 软件参考模型；③ 验证用的协同仿真与 testbench；④ 仿真基础设施。

理解目录划分，就是理解「我改一个地方，会影响哪几个目录、要不要同步改另外的语言」。

#### 4.1.2 核心流程

一个典型运算（比如加法 `cl_fix_add`）在仓库里的「镜像」分布如下：

```text
定义同一份运算 cl_fix_add：
  ├─ hdl/en_cl_fix_pkg.vhd           → VHDL 实现（可综合 RTL 的函数版）
  ├─ bittrue/models/python/...       → Python 实现（参考模型，黄金参考来源）
  ├─ bittrue/models/matlab/cl_fix_add.m → MATLAB 包装（转调 Python）
  ├─ bittrue/cosim/cl_fix_add/cosim.py  → 用 Python 穷举并生成期望数据
  └─ tb/cl_fix_add_tb.vhd            → VHDL testbench，读数据并比对
```

也就是说，**同一个运算名横跨五个目录**。这正是 u1-l1 提到的「三语言 API 一一对应」在文件层面的体现。

#### 4.1.3 源码精读

先看 README 对 RTL 与 testbench 标准的明确约定：

[`README.md:13-17`](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/README.md#L13-L17) — 说明支持的语言，并用脚注强调：**所有 RTL 代码遵循 VHDL-93（为了最大兼容综合工具），testbench 遵循 VHDL-2008**。这条规则直接决定了后面 `hdl/` 与 `tb/` 是用不同标准编译的。

再看「如何运行测试」一节，它揭示了三个测试入口分别位于何处：

[`README.md:190-198`](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/README.md#L190-L198) — 明确写明：Python 测试在 `./bittrue/tests/python/`，MATLAB 测试在 `./bittrue/tests/matlab/`，**VHDL testbench 在 `./tb/`，但要从 `./sim/` 目录执行**。这一句点破了 `tb/` 与 `sim/` 的分工：testbench 代码住在 `tb/`，但「按哪个按钮启动」控制在 `sim/`。

据此可以把顶层目录归纳成下表（结合实际 `git ls-files` 的结果）：

| 目录 | 语言 / 标准 | 是否可综合 | 职责 |
| --- | --- | --- | --- |
| `hdl/` | VHDL-93 | 是 | 可综合 RTL：核心包 `en_cl_fix_pkg.vhd`、私有包、以及 round/saturate/resize 三个实体组件 |
| `bittrue/models/python/` | Python | —（软件） | 位级精确参考模型，黄金参考来源 |
| `bittrue/models/matlab/` | MATLAB | —（软件） | 逐个对应 Python 接口的 `.m` 包装器 |
| `bittrue/cosim/` | Python | —（脚本） | 每个运算一个子目录，含 `cosim.py`，生成 `data/` 下的数据文件 |
| `bittrue/tests/` | Python / MATLAB | —（软件） | 各语言单元测试 |
| `tb/` | VHDL-2008 | 否 | testbench：读文件、重生成输入、调 VHDL 函数、对拍 |
| `tb/util/` | VHDL-2008 | 否 | testbench 辅助包（如 `en_cl_fix_fileio_pkg.vhd` 文件 I/O 包装） |
| `sim/` | Python | —（脚本） | 仿真编排：`run.py` / `common.py` / `cosim_runner.py` |
| `lib/en_tb/` | VHDL-2008 | 否 | 内嵌的 Enclustra 通用 testbench 库（文件 I/O 基础设施） |
| `doc/` | 文档/图片 | — | 图示与说明 |

注意两个容易混淆的点：

1. `hdl/` 里**既有包（函数）也有实体（组件）**。函数版用于 testbench 里直接计算；实体版（`en_cl_fix_round.vhd` 等）才是真正可综合成流水线的硬件组件（u7 会专门讲）。
2. `lib/en_tb/` 是一个**独立子项目**（它自己有 `README.md`、`CHANGELOG.md`、`sim/`、`tb/`），en_cl_fix 把它整包内嵌进来当依赖用，主要取其文件 I/O 能力。

#### 4.1.4 代码实践（源码阅读型）

**目标**：亲手核对「一个运算名横跨多个目录」这件事。

**步骤**：

1. 在仓库根目录执行 `git ls-files`（或用编辑器全局搜索）。
2. 找出所有文件名/路径中含 `cl_fix_round` 的文件。
3. 按下面的表格分类填写。

**需要观察的现象**：同一个 `cl_fix_round` 应当至少出现在五个位置——VHDL 包函数、Python 模型、MATLAB 包装、cosim 脚本、testbench。如果某处缺失，说明该运算没有完整的三语言/验证覆盖。

**预期结果**（基于当前 HEAD 实际存在）：

| 类别 | 文件 |
| --- | --- |
| VHDL RTL 包 | `hdl/en_cl_fix_pkg.vhd` |
| VHDL RTL 实体 | `hdl/en_cl_fix_round.vhd` |
| Python 模型 | `bittrue/models/python/en_cl_fix_pkg/narrow_fix.py`（round 实现） |
| MATLAB 包装 | `bittrue/models/matlab/cl_fix_round.m` |
| cosim 脚本 | `bittrue/cosim/cl_fix_round/cosim.py` |
| VHDL testbench | `tb/cl_fix_round_tb.vhd` |
| Python 测试 | `bittrue/tests/python/cl_fix_round_test.py` |

#### 4.1.5 小练习与答案

**练习 1**：为什么 `hdl/` 要用更老的 VHDL-93，而 `tb/` 可以用更新的 VHDL-2008？

**参考答案**：因为 RTL 会被综合工具转成真实电路，各家综合工具对 VHDL-93 的支持最稳定、最广，用老标准能获得最大兼容性；而 testbench 永远不会被综合，只跑在仿真器里，现代仿真器对 VHDL-2008（如 `context`、增强的 `std.textio`）支持良好，用新标准能写更简洁的测试代码。

**练习 2**：`lib/en_tb/` 目录里又出现了 `hdl/`、`tb/`、`sim/` 这样的子目录，这说明它和整个 en_cl_fix 是什么关系？

**参考答案**：`lib/en_tb/` 本身就是一个结构完整的独立 VHDL testbench 子库（自带 README、CHANGELOG、sim、tb），en_clustra 把它**作为依赖整体内嵌**到主仓库的 `lib/` 下，主仓库的 `sim/run.py` 会把它编译成一个名为 `en_tb` 的 VHDL library 供 testbench 调用，主要借用其文件 I/O 能力。

---

### 4.2 Python 包的模块拆分与 `__init__` 导出

#### 4.2.1 概念说明

Python 参考模型住在 `bittrue/models/python/en_cl_fix_pkg/`，这是一个**包**（含 `__init__.py` 的目录）。它的设计哲学是「内部分五个模块，对外只露一张脸」：

- **内部模块化**：把不同关注点拆到不同 `.py` 文件，便于维护。
- **对外统一**：使用者只需 `from en_cl_fix_pkg import *`，就能拿到全部公开符号，不必关心某个函数到底定义在哪个子模块。

这种「门面（facade）」式的导出，是大型 Python 库的常见做法。

#### 4.2.2 核心流程

包的五个子模块及其职责（本讲只做结构介绍，实现细节留待后续讲义）：

```text
en_cl_fix_pkg/                     ← Python 包目录
  ├─ __init__.py                   ← 门面：聚合并导出
  ├─ en_cl_fix_types.py            ← 核心类型：FixFormat / FixRound / FixSaturate
  ├─ narrow_fix.py                 ← ≤53 位快速表示（float64 内部存储）
  ├─ wide_fix.py                   ← 任意精度表示（Python 大整数存储）
  ├─ en_cl_fix.py                  ← 主接口：cl_fix_* 函数与 narrow/wide 分发
  └─ matlab_interface.py           ← MATLAB 桥接用的 uint64 分块工具
```

加载顺序很关键：`__init__.py` 里的 `import *` 是**自上而下**执行的，先导入类型定义，再导入依赖类型的两个表示类，最后导入依赖它们的主接口与桥接工具。顺序错了会报「名字找不到」。

#### 4.2.3 源码精读

整个门面只有五行有效代码，但它是理解 Python API 的总开关：

[`bittrue/models/python/en_cl_fix_pkg/__init__.py:20-24`](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/__init__.py#L20-L24) — 依次从五个子模块用 `from .xxx import *` 导入全部公开名字。顺序为：类型 → 两个表示类 → 主接口 → MATLAB 桥接。

逐行拆解这五行的含义：

1. `from .en_cl_fix_types import *` — 导入 `FixFormat`、`FixRound`、`FixSaturate` 这三个最基础的类型（u2-l1 详解）。
2. `from .narrow_fix import *` — 导入 `NarrowFix`（≤53 位路径，float64 存储）。
3. `from .wide_fix import *` — 导入 `WideFix`（任意精度路径，大整数存储）。
4. `from .en_cl_fix import *` — 导入全部 `cl_fix_*` 函数（add/mult/round/resize/from_real…），这是**用户最常用的一层**。
5. `from .matlab_interface import *` — 导入 MATLAB 桥接工具（给 `.m` 包装器用，普通用户不直接用）。

> 这也解释了 u1-l1 里的结论：**位宽 ≤ 53 位时走更快的 NarrowFix 路径，否则走任意精度 WideFix**——这两种表示正是这个包里并列的两个模块，而 `en_cl_fix.py` 里的 `cl_fix_*` 函数会在它们之间自动分发（u6 详解）。

cosim 脚本和测试都是怎么「找到」这个包的？看一个真实例子：

[`bittrue/cosim/cl_fix_add/cosim.py:29-30`](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/cosim/cl_fix_add/cosim.py#L29-L30) — cosim 脚本通过 `sys.path.append(join(root, "../../models/python"))` 把模型目录加进搜索路径，再 `from en_cl_fix_pkg import *` 拿到全部 API。也就是说，**包没有被 `pip install`，而是靠相对路径动态加载**——这是阅读本项目脚本时反复出现的模式。

#### 4.2.4 代码实践

**目标**：验证「一次导入，五模块全到手」。

**步骤**：

1. 安装依赖：`python -m pip install -r requirements.txt`（需 numpy）。
2. 在仓库根目录启动 Python，执行：

```python
import sys
sys.path.append("bittrue/models/python")
from en_cl_fix_pkg import *          # 一句拿到全部公开 API

# 来自 en_cl_fix_types 模块
fmt  = FixFormat(1, 4, 8)
rnd  = FixRound.Trunc_s
sat  = FixSaturate.None_s
print(fmt)                            # 应打印类似 [1,4,8]

# 来自 en_cl_fix 模块（内部会用 NarrowFix 表示）
r = cl_fix_from_real(2.5, fmt)
print(cl_fix_to_real(r, fmt))         # 应打印 2.5
```

**需要观察的现象**：`FixFormat`、`FixRound`、`cl_fix_from_real`、`cl_fix_to_real` 这些名字来自不同子模块，但你只写了一句 `from en_cl_fix_pkg import *` 就能全部使用——这就是 `__init__.py` 门面的效果。

**预期结果**：打印出 `[1,4,8]` 和 `2.5`。如果你故意把 `__init__.py` 里某一行注释掉再重试，对应的符号就会变成 `NameError`，从而直观体会到每一行 `import *` 各自负责哪一类名字。（本步骤仅作理解，**不要真的修改源码**；可在脑子里推演。）

#### 4.2.5 小练习与答案

**练习 1**：如果有人把 `__init__.py` 里的 `from .narrow_fix import *` 删掉，调用 `cl_fix_from_real` 时会发生什么？

**参考答案**：表面看 `cl_fix_from_real` 来自 `en_cl_fix` 模块，删除 narrow_fix 的导入**不一定会立即报 NameError**；但当 `cl_fix_from_real` 内部需要构造 `NarrowFix` 时，由于该名字不在包命名空间，且 en_cl_fix.py 内部是直接 `import` 自己所需符号的，所以更可能的后果是：在运行到具体 narrow 路径时抛出 `NameError: name 'NarrowFix' is not defined` 之类的错误。这道题的核心是让你意识到 `__init__.py` 的导入顺序与完整性会影响包对外暴露的 API 面。

**练习 2**：为什么 cosim 脚本要用 `sys.path.append(...)` 而不是 `pip install` 这个包？

**参考答案**：因为这个 Python 包是随仓库源码分发的参考模型，没有被发布到 PyPI。脚本通过相对路径（`../../models/python`）把它临时加到 `sys.path`，就能在任何机器上不经安装直接 import，方便在仿真环境里即取即用。

---

### 4.3 `sim/run.py` 与仿真「三件套」的组织

#### 4.3.1 概念说明

`sim/` 目录是整个 VHDL 验证的「驾驶舱」，它由三个 Python 脚本组成（俗称仿真三件套）：

- **`run.py`**：主入口。用 [VUnit](https://vunit.github.io/) 这个 Python 驱动的 VHDL 验证框架，把 RTL 源码、testbench、testbench 库都登记进来，并为每个 testbench 绑定一个「仿真前」钩子（pre_config），用来触发对应的 cosim 脚本。
- **`common.py`**：公共配置。解析命令行参数（选哪个仿真器、路径在哪、要不要覆盖率），并根据仿真器设定 RTL / testbench 分别用哪个 VHDL 标准。
- **`cosim_runner.py`**：cosim 运行器。保证每个 cosim 脚本在整轮仿真里**只被执行一次**，即便 VUnit 并发起了多个 test，也线程安全。

#### 4.3.2 核心流程

一次完整的 VHDL 验证，时序如下：

```text
python sim/run.py --simulator=ghdl ...
        │
        ├─ common.py 解析参数、设 VUnit 环境变量、定 VHDL 标准
        │
        ├─ run.py::create_test_suite()
        │     ├─ 编译三个库：
        │     │    1) en_tb  ← lib/en_tb/hdl/*.vhd        (VHDL-2008)
        │     │    2) lib    ← hdl/*.vhd                  (VHDL-93 / RTL 标准)
        │     │              ← tb/util/*.vhd + tb/*.vhd   (VHDL-2008)
        │     │
        │     └─ 为每个 testbench（如 cl_fix_add_tb）注册：
        │          pre_config = cl_fix_add_cosim.run
        │
        ├─ VUnit 开始仿真某个 test
        │     ├─ 仿真前回调：cosim_runner.run()
        │     │    └─ 首次调用时执行 cosim.py::run()
        │     │         → Python 算黄金参考 → 写 data/*.txt
        │     │
        │     └─ VHDL testbench 启动：
        │          读 data/*.txt → 重生成输入 → 调 VHDL 函数 → 与期望对拍
        │
        └─ 仿真结束，post_run 汇总（可选合并覆盖率）
```

关键直觉：**cosim 脚本不是手动跑的，而是被 VUnit 在仿真前自动触发的**。testbench 假定它要读的数据文件已经由 cosim 准备好了。

#### 4.3.3 源码精读

**(a) 三个库的编译与 VHDL 标准区分** —— 这是 `run.py` 的核心结构：

[`sim/run.py:30-50`](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/sim/run.py#L30-L50) — `create_test_suite()` 的开头：先加入 VUnit 自带库（OSVM/VM、verification components、random），再加入 `en_tb` 库，最后创建 `lib` 库并把 RTL、testbench 辅助包、testbench 三类源码分别用不同标准编译进去。

注意三个 `add_source_files` 的 `vhdl_standard` 参数不同（详见下表），这正是 README 那条「RTL 用 93、testbench 用 2008」约定的落地：

| 源文件 | 编译到的库 | VHDL 标准 |
| --- | --- | --- |
| `lib/en_tb/hdl/*.vhd` | `en_tb` | `vhdl_standard_tb`（2008） |
| `hdl/*.vhd` | `lib` | `vhdl_standard_rtl` |
| `tb/util/*.vhd`、`tb/*.vhd` | `lib` | `vhdl_standard_tb`（2008） |

而 `vhdl_standard_rtl` / `vhdl_standard_tb` 这两个变量的取值是在 `common.py` 里根据仿真器决定的：

[`sim/common.py:82-89`](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/sim/common.py#L82-L89) — Modelsim/Questa 下 RTL 用 `93`、testbench 用 `2008`；GHDL/NVC 下两者都用 `2008`（因为这两个开源仿真器对 VHDL-93 的支持反而不如 2008 全面，所以统一用 2008）。这解释了为什么标准要做成变量而不是写死。

**(b) 把 testbench 与 cosim 脚本绑定** —— `run.py` 的第二部分，对每个运算都重复同一段模式。以 `cl_fix_add` 为例：

[`sim/run.py:56-74`](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/sim/run.py#L56-L74) — 先定义 `COSIM_PATH` 指向 `bittrue/cosim`，再用一个内部类 `cosim(cosim_runner)` 把它特化到具体子目录（如 `cl_fix_add`）；接着取出名为 `cl_fix_add_tb` 的 testbench，给它每个 `test` 配置一个 `pre_config=cl_fix_add_cosim.run` 回调。这一段就是「**把 VHDL testbench 和 Python cosim 脚本用 pre_config 钩子缝在一起**」的接合点。

后面 `cl_fix_sub`、`cl_fix_mult`、`cl_fix_round`……每个运算都复制了同一段模式，只是换了名字。`cl_fix_round/saturate/resize` 这三个还会额外对 `meta_width` 枚举出多个配置（见 `run.py:156-193`），这是它们作为可综合组件特有的「旁路元数据位宽」参数（u7 详解）。

**(c) cosim 只跑一次的保证**：

[`sim/cosim_runner.py:31-72`](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/sim/cosim_runner.py#L31-L72) — `cosim_runner` 类用线程锁（`Lock`）和「自我失能」（`self.enable = False`）双重保险，保证即便 VUnit 并发起多个 test，同一个 cosim 脚本的 `run()` 也只被执行一次——因为黄金参考数据文件只需生成一次，多跑是浪费。

**(d) 数据文件的产生与消费** —— 闭环的关键：

产生端（Python cosim 脚本），把期望输出和格式写进 `data/`：

[`bittrue/cosim/cl_fix_add/cosim.py:142-152`](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/cosim/cl_fix_add/cosim.py#L142-L152) — 调用 Python 的 `cl_fix_add(...)` 算出期望结果 `r`，再用 `np.savetxt(...)` 写成 `testN_output.txt`。随后（`cosim.py:166-177`）把所有 `a_fmt/b_fmt/r_fmt` 和舍入/饱和模式也分别写文件。

消费端（VHDL testbench），通过 `tb/util/en_cl_fix_fileio_pkg.vhd` 提供的包装过程读回这些文件。而这个包装包本身是对 `en_tb` 库的二次封装：

[`tb/util/en_cl_fix_fileio_pkg.vhd:95-106`](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/tb/util/en_cl_fix_fileio_pkg.vhd#L95-L106) — 它 `library en_tb;` 并 `use en_tb.en_tb_fileio_text_pkg.all;`，把 en_tb 的通用文本 I/O 能力引入，再加上 `use work.en_cl_fix_pkg.all;` 拿到定点格式定义，从而能写出 `cl_fix_read_file` / `cl_fix_read_format_file` 这种「懂定点格式」的读文件函数（实现见 `en_cl_fix_fileio_pkg.vhd:286-294`）。

到这里，闭环就清晰了：**Python（参考模型）→ 文件 → VHDL（testbench）对拍**，而 `sim/run.py` 是把这条流水线编排起来的总指挥。

#### 4.3.4 代码实践（源码阅读型，本讲的指定实践）

**目标**：画出 Python 模型、cosim 脚本、VHDL testbench、en_tb 之间的调用与文件流草图。

**步骤**：

1. 阅读下面四个文件的关键片段（行号已在上面给出）：
   - `bittrue/cosim/cl_fix_add/cosim.py`（数据**产生**端）
   - `sim/run.py`（**编排**端：注册库 + 绑定 pre_config）
   - `sim/cosim_runner.py`（**只跑一次**保证）
   - `tb/util/en_cl_fix_fileio_pkg.vhd`（数据**消费**端的包装）
2. 在纸上（或文本里）画出下面这张关系图，并用箭头标注「调用」「数据写入」「数据读取」。

**参考草图**（你可以把它当作答案对照）：

```text
        ┌───────────────────────────── bittrue/models/python ─────────────────────────────┐
        │  en_cl_fix_pkg (经 __init__.py 导出):  cl_fix_add, FixFormat, NarrowFix, ...     │
        └───────────────▲──────────────────────────────────────────────────────▲──────────┘
                        │ import                                            │ import
        ┌───────────────┴───────────────┐                  ┌───────────────────┴──────────┐
        │ bittrue/cosim/cl_fix_add/      │                  │ bittrue/tests/python/*.py     │
        │ cosim.py  (黄金参考生成)        │                  │  (Python 单元测试)            │
        └───────────────┬───────────────┘                  └───────────────────────────────┘
                        │ np.savetxt 写出
                        ▼
              ┌─────────────────────┐
              │ data/*.txt          │   ← 文件交换层（output/fmt/rnd/sat）
              └──────────▲──────────┘
                         │ cl_fix_read_file / cl_fix_read_format_file 读回
              ┌──────────┴──────────────────────────┐
              │ tb/cl_fix_add_tb.vhd  (VHDL 对拍)    │
              │  调用 hdl/en_cl_fix_pkg.vhd 的函数    │
              └──────────▲──────────────────────────┘
                         │ use（依赖其文件 I/O）
              ┌──────────┴──────────────────────────┐
              │ tb/util/en_cl_fix_fileio_pkg.vhd     │  ← 定点专用 I/O 包装
              └──────────▲──────────────────────────┘
                         │ use en_tb.en_tb_fileio_text_pkg
              ┌──────────┴──────────────────────────┐
              │ lib/en_tb  (通用 testbench 库)        │  ← 编译为 VHDL library "en_tb"
              └───────────────────────────────────────┘

  编排层（独立于上图的数据流，负责「按按钮」）：
        sim/run.py  ──注册库 + pre_config──▶  VUnit
        sim/run.py  ──pre_config 钩子──────▶ cosim_runner.run() ──▶ cosim.py::run()
        sim/common.py ──决定 VHDL 标准 / 仿真器
```

**需要观察的现象 / 预期结果**：在你的图里应当能清晰地看到「**两条独立的依赖链**」：

1. **数据链**（横向）：Python 模型 → cosim 写文件 → testbench 读文件 → 对拍。
2. **编译/调用链**（纵向）：testbench → `en_cl_fix_fileio_pkg` → `en_tb` 库；以及 `sim/run.py` 在外面驱动这一切。

如果你的图里把 `sim/run.py` 画成了「直接调用 testbench」，那是错的——`run.py` 只负责**编译与配置**，并通过 pre_config 触发 cosim，真正的 testbench 执行是 VUnit 交给仿真器去做的。

#### 4.3.5 小练习与答案

**练习 1**：`sim/run.py` 里把 `hdl/*.vhd` 与 `tb/*.vhd` 都加进了名为 `lib` 的同一个 VUnit 库，但用了不同的 `vhdl_standard`。同一个库里能用两种标准编译吗？

**参考答案**：VUnit 允许**按源文件**指定 VHDL 标准，但实际能否在同一个库里混用取决于仿真器。大多数情况下，VHDL-93 是 VHDL-2008 的子集，testbench（2008）可以引用 RTL（93）里编译进来的包；本项目的做法就是把 RTL 和 testbench 都放进 `lib` 库，分别编译，让 testbench 能直接 `use work.en_cl_fix_pkg.all`。

**练习 2**：如果删掉 `pre_config=cl_fix_add_cosim.run` 这一行绑定，运行 `cl_fix_add_tb` 会怎样？

**参考答案**：testbench 依然会被编译和仿真，但仿真前不会有人去生成 `data/` 下的黄金参考文件。testbench 在尝试 `cl_fix_read_file(...)` 读取这些文件时会因为文件不存在或为空而报错（或读到空数组导致对拍失败）。这条练习说明 pre_config 钩子是「Python 黄金参考 → VHDL 对拍」闭环的关键粘合剂。

**练习 3**：`cosim_runner` 为什么需要线程锁？

**参考答案**：VUnit 可以并发仿真多个 test。当多个 test 线程同时进入 `pre_config` 回调时，都可能在「首个」`run()` 完成前通过 enable 检查。线程锁（外加进入锁后的二次 enable 检查 + 执行后 `self.enable = False`）确保**只有一个线程真正执行 cosim 的 `run()`**，其余线程要么等锁、要么在拿到锁后发现已被禁用而直接返回，从而避免重复生成数据文件的竞争与浪费。

## 5. 综合实践

把本讲三个模块串起来的小任务：**给一个新运算建立完整的「五目录镜像」心智清单**。

设想你要给 en_cl_fix 新增一个运算 `cl_fix_dummy`（仅作练习，**不要真的改动源码**）。请回答：

1. 按本讲梳理的目录划分，你需要新建/修改哪五个目录下的哪些文件，才能让它和现有运算（如 `cl_fix_add`）保持同构？
   - 参考答案要点：① `hdl/en_cl_fix_pkg.vhd` 加函数；② `bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py` 加 Python 实现；③ `bittrue/models/matlab/cl_fix_dummy.m` 加 MATLAB 包装；④ `bittrue/cosim/cl_fix_dummy/cosim.py` 加黄金参考生成；⑤ `tb/cl_fix_dummy_tb.vhd` 加 testbench，并在 `sim/run.py` 里注册它、把它的 `pre_config` 绑到新建的 cosim。
2. 在 `sim/run.py` 里，你会复用 `run.py:56-74` 的哪段模式？需要改哪几个名字？
   - 参考答案：复用「定义 `cosim` 子类 → `lib.test_bench(...)` 取 testbench → 给每个 test 设 `pre_config=xxx_cosim.run`」这段模式，把 `cl_fix_add` 全部替换成 `cl_fix_dummy`。
3. 这个新 testbench 读取的数据文件由谁、在什么时候生成？
   - 参考答案：由 `bittrue/cosim/cl_fix_dummy/cosim.py` 的 `run()` 生成（写 `data/*.txt`），由 `cosim_runner` 在 VUnit 的 pre_config 阶段、testbench 真正仿真之前触发执行一次。

完成上述清单后，你就把「目录划分 → Python 包导出 → sim/run.py 编排」三件事打通了。

## 6. 本讲小结

- 仓库顶层目录按「**可综合 RTL（`hdl/`）/ 软件模型（`bittrue/models/`）/ 验证（`bittrue/cosim`、`tb/`、`sim/`）/ 基础设施（`lib/en_tb/`）**」四类划分；同一个运算名会横跨多个目录，体现三语言 API 一一对应。
- RTL 用 VHDL-93（最大综合兼容性），testbench 用 VHDL-2008；这条约定在 `sim/common.py` 里按仿真器动态设定为 `vhdl_standard_rtl` / `vhdl_standard_tb`。
- Python 参考模型包用 `__init__.py` 的五句 `from .xxx import *` 做**门面导出**，把类型、NarrowFix、WideFix、主接口、MATLAB 桥接五模块统一对外。
- `sim/run.py` 是验证驾驶舱：编译 `en_tb`、`lib` 两个库，用 `pre_config` 钩子把每个 testbench 与对应 cosim 脚本缝合成「Python 写文件 → VHDL 读文件对拍」的闭环。
- `sim/cosim_runner.py` 用线程锁 + 自我失能，保证每个 cosim 脚本整轮只跑一次；`tb/util/en_cl_fix_fileio_pkg.vhd` 包装 `en_tb` 库提供定点专用的文件 I/O。
- 理解本讲后，你应该能在仓库里「按目录定位职责」，不再迷路——这是后续深入任何模块的前提。

## 7. 下一步学习建议

- **横向打通接口**：下一讲 u1-l4「快速上手：运行 Python 与 MATLAB 测试」会带你真的跑起来，建议紧接着做，把本讲的静态地图变成动态体验。
- **进入核心类型**：完成入门单元后，建议进入 u2-l1「核心类型：FixFormat、FixRound、FixSaturate」，那时你会回头精读本讲提到的 `en_cl_fix_types.py`。
- **想提前理解验证闭环**：可以跳读 u8-l1「cosim 验证流程总览」，它会展开本讲 4.3 节里 cosim 脚本的穷举与黄金参考生成细节。
- **想理解文件 I/O 底层**：可先读 `lib/en_tb/doc/index.md`（en_tb 的文档入口），了解 `en_tb_fileio_context` 等 context 的设计，再回看 `en_cl_fix_fileio_pkg.vhd` 这层包装。
