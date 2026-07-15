# 目录结构与三语言镜像架构

## 1. 本讲目标

本讲承接 [u1-l1 项目定位](u1-l1-project-overview.md)，把视角从「这个库是什么」推进到「这个库的代码长什么样、放在哪里」。

学完本讲后，你应该能够：

- 看懂 `en_cl_fix` 仓库的顶层目录划分，并说出每个目录的职责。
- 理解 VHDL、Python、MATLAB **三套实现一一对应（镜像）** 的关系，知道哪一套是「源头」、哪一套是「参考」、哪一套是「薄封装」。
- 在仓库中快速定位某个操作（例如 `cl_fix_round`）在三种语言下分别对应哪个文件。
- 理解 `tb/`、`sim/`、`lib/en_tb/` 三者如何分工，串起一条完整的「写 RTL → 用 Python 生成黄金数据 → 在仿真器里逐拍比对」的验证链路。

> 本讲只关心**目录与文件的组织**，不深入定点数学的算法细节（那是 U2 及以后的内容）。阅读时重点放在「谁在哪里、为什么这样放」。

---

## 2. 前置知识

在阅读本讲前，建议你已经知道：

- **定点数（fixed-point number）**：用固定位数的二进制表示带小数点的数。`en_cl_fix` 就是一个专门处理定点数的库。
- **VHDL**：一种硬件描述语言，用来写可综合成 FPGA/ASIC 电路的代码（RTL）。`en_cl_fix` 的 RTL 全部用 VHDL 编写。
- **Python / MATLAB**：两种常用的软件脚本语言。`en_cl_fix` 用它们实现「与 HDL 行为完全一致」的参考模型。
- **参考模型（reference model）**：在硬件验证里，用一个已知正确的软件实现去对照硬件实现的输出，这个软件实现就叫参考模型。`en_cl_fix` 的 Python 实现就扮演这个角色。

一个贯穿全讲的**关键直觉**：

> 这个库的核心设计哲学是「同一套定点语义，在三种语言里各写一遍，且彼此行为完全一致」。于是仓库里会出现大量「同名函数、不同语言」的文件。看懂它们的对应关系，是后续阅读所有源码的前提。

---

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 / 目录 | 语言 | 作用 |
| --- | --- | --- |
| [README.md](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/README.md) | — | 项目说明：支持的语言、依赖、如何跑测试 |
| [hdl/en_cl_fix_pkg.vhd](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd) | VHDL | **主包**：类型定义 + 所有 `cl_fix_*` 公共函数 |
| [hdl/en_cl_fix_private_pkg.vhd](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_private_pkg.vhd) | VHDL | 私有工具函数包（不对外暴露） |
| [hdl/en_cl_fix_round.vhd](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_round.vhd) 等 3 个组件 | VHDL | 可流水线化（带寄存器）的实例化组件 |
| [bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py) | Python | **Python 主接口**：镜像 HDL 的 `cl_fix_*` 函数 |
| [bittrue/models/python/en_cl_fix_pkg/__init__.py](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/__init__.py) | Python | 包入口：把各子模块全部导出 |
| [bittrue/models/matlab/cl_fix_round.m](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/matlab/cl_fix_round.m) | MATLAB | **薄封装**：转数据 → 调 Python → 转回来 |
| [tb/](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/tb/cl_fix_round_tb.vhd) | VHDL | 测试台（testbench），驱动 UUT 并比对 |
| [sim/run.py](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/sim/run.py) | Python | 仿真装配脚本：组装 VUnit 工程并启动仿真 |
| [lib/en_tb/README.md](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/lib/en_tb/README.md) | — | Enclustra 测试台基础库的说明 |

---

## 4. 核心概念与源码讲解

本讲按 4 个最小模块展开：先看 RTL（`hdl/`），再看三语言模型（`bittrue/models/`），再看测试与仿真（`tb/` + `sim/`），最后看公共测试库（`lib/en_tb/`）。

### 4.1 hdl/ 目录：可综合的 VHDL 包与组件

#### 4.1.1 概念说明

`hdl/` 是整个仓库的**源头**：这里放的是会被真正综合成电路的 RTL 代码。`en_cl_fix` 所有定点运算的「金标准语义」都以这里的 VHDL 实现为准，Python 与 MATLAB 都是在「模仿」它。

`hdl/` 一共只有 5 个 `.vhd` 文件，分两类：

1. **包（package）**：把类型和函数集中在一起，供其他文件 `use`。
   - `en_cl_fix_pkg.vhd`：主包，对外公开的全部 `cl_fix_*` 函数都在这里。
   - `en_cl_fix_private_pkg.vhd`：私有包，只放主包内部用的小工具（如 `choose`、`maximum`），设计者不应依赖它。
2. **组件（entity）**：可以实例化、可以插入流水线寄存器的电路模块。
   - `en_cl_fix_round.vhd`：流水线化的舍入。
   - `en_cl_fix_saturate.vhd`：流水线化的饱和。
   - `en_cl_fix_resize.vhd`：流水线化的「先舍入后饱和」。

> 区别要点：**包里的函数是「纯计算」**，调用一次出一个结果；**组件是「带时钟的电路」**，有 `clk`/`rst`、有延迟、能插入寄存器，适合搭进真实数据通路。函数用于「算一下」，组件用于「做成硬件」。

#### 4.1.2 核心流程

包的结构遵循经典的 VHDL 写法：

```text
package en_cl_fix_pkg is        -- 包头：声明类型 + 函数签名（公开 API）
    -- 类型定义
    -- 函数声明（如 cl_fix_round、cl_fix_add_fmt ...）
end package;

package body en_cl_fix_pkg is   -- 包体：函数的具体实现
    -- 每个声明的函数在这里给出实现
end package body;
```

- **包头**告诉使用者「我提供哪些类型和函数」。
- **包体**告诉编译器「这些函数具体怎么算」。
- 组件文件则各自独立，开头是一段注释说明延迟策略，然后是 `entity` 声明。

#### 4.1.3 源码精读

先看主包开头的**类型定义**。这是理解整库的「数据模型基础」：

[hdl/en_cl_fix_pkg.vhd:39-73](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L39-L73) —— 定义了 `FixFormat_t`（格式 `[S,I,F]`）、`FixRound_t`（7 种舍入）、`FixSaturate_t`（4 种饱和）、`RegisterMode_t`（组件的寄存器策略）四种核心类型。

再看主包里 `cl_fix_round` 函数的**声明**（在包头里）：

[hdl/en_cl_fix_pkg.vhd:134-140](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L134-L140) —— 声明了舍入函数 `cl_fix_round`，参数为输入数据、输入格式、结果格式、舍入模式。这就是「黄金 API」之一，下面你会看到 Python、MATLAB 里有**同名同参数**的对应物。

它的**实现**在包体里：

[hdl/en_cl_fix_pkg.vhd:910-916](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L910-L916) —— `cl_fix_round` 的函数体，签名与上面声明完全一致。

最后看一个**组件**长什么样：

[hdl/en_cl_fix_round.vhd:50-58](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_round.vhd#L50-L58) —— `en_cl_fix_round` 实体的 `generic`，可以看到它把 `in_fmt_g`、`out_fmt_g`、`round_g`、`reg_mode_g` 作为参数暴露给使用者，这正是「可配置的流水线舍入器」。组件文件顶部的注释（[hdl/en_cl_fix_round.vhd:22-36](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_round.vhd#L22-L36)）清楚解释了 `Auto_s / Yes_s / No_s` 三种寄存器模式各自的延迟含义。

#### 4.1.4 代码实践

- **实践目标**：确认「函数 vs 组件」的分工，并找到 `cl_fix_round` 的声明与实现。
- **操作步骤**：
  1. 打开 [hdl/en_cl_fix_pkg.vhd](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd)，分别定位 `package en_cl_fix_pkg is`（包头）和 `package body`（包体）两段。
  2. 在包体里搜索 `function cl_fix_round`，确认它的实现起始行。
  3. 打开 [hdl/en_cl_fix_round.vhd](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_round.vhd)，确认它是一个带 `clk`/`rst` 端口的 `entity`，而不是普通函数。
- **需要观察的现象**：包体里的 `cl_fix_round` 没有时钟，是纯组合函数；组件 `en_cl_fix_round` 有 `clk`、`rst`、`in_valid`、`out_valid` 等端口。
- **预期结果**：你能用一句话区分「包里同名函数（纯计算）」与「`en_cl_fix_round.vhd` 组件（带寄存器的硬件）」。
- 若仅阅读源码无法在本机运行仿真，本步骤标注为「待本地验证」的延伸实验。

#### 4.1.5 小练习与答案

- **练习 1**：`hdl/` 里哪两个文件是「包」、哪三个是「组件」？
  - **答案**：包是 `en_cl_fix_pkg.vhd` 与 `en_cl_fix_private_pkg.vhd`；组件是 `en_cl_fix_round.vhd`、`en_cl_fix_saturate.vhd`、`en_cl_fix_resize.vhd`。
- **练习 2**：为什么 `en_cl_fix_private_pkg.vhd` 要单独成一个包？
  - **答案**：把内部辅助函数（如 `choose`、`maximum`、字符串处理）与公开 API 隔离，避免使用者依赖实现细节，也便于维护。

---

### 4.2 bittrue/models/：Python 实现与 MATLAB 薄封装

#### 4.2.1 概念说明

`bittrue/` 的名字来自 **bit-true（位真）**：这里的软件实现要做到「每一位都和硬件输出一致」。其中 `bittrue/models/` 存放三种语言的「模型」实现，是本模块的重点。

三种语言的角色完全不同：

| 语言 | 目录 | 角色 |
| --- | --- | --- |
| Python | `bittrue/models/python/en_cl_fix_pkg/` | **参考实现**：与 HDL 同名同语义，作为验证用的黄金参考模型 |
| MATLAB | `bittrue/models/matlab/` | **薄封装**：每个 `.m` 文件只是转一下数据格式，然后调用 Python |
| VHDL | `hdl/`（见 4.1） | **金标准 RTL**：真正综合成电路的实现 |

> 「镜像（mirror）」这个词的来源：`en_cl_fix.py` 文件顶部注释直接写明这个模块「designed to mirror the HDL implementation」（旨在镜像 HDL 实现）。也就是说，HDL 里有一个 `cl_fix_round`，Python 里就有一个签名、语义完全相同的 `cl_fix_round`。

#### 4.2.2 核心流程

**Python 模型**的组织：`en_cl_fix_pkg` 是一个 Python 包（目录里有 `__init__.py`），内部按职责拆成几个模块：

```text
en_cl_fix_pkg/
├── __init__.py          # 包入口：把下面所有模块导出，让 import en_cl_fix_pkg 后能直接用
├── en_cl_fix_types.py   # 类型：FixFormat / FixRound / FixSaturate（对应 VHDL 的三种类型）
├── en_cl_fix.py         # 主接口：所有 cl_fix_* 函数（镜像 HDL）
├── narrow_fix.py        # ≤53 位实现（用双精度浮点，快）
├── wide_fix.py          # 任意位宽实现（用大整数，慢但精确）
└── matlab_interface.py  # MATLAB↔Python 的数据打包/解包
```

**MATLAB 封装**的组织：`bittrue/models/matlab/` 下每个操作对应一个 `.m` 文件（约 36 个），它们几乎都是同一个套路——三段式：

```text
1. mat2py：把 MATLAB 数据归一化成 Python 能直接处理的格式
2. py.<...>：调用对应的 Python 函数（真正干活的是 Python）
3. py2mat：把 Python 结果转回 MATLAB 的数据格式
```

#### 4.2.3 源码精读

先看 **Python 主接口**如何自我描述其「镜像」定位：

[bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py:21-29](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L21-L29) —— 模块注释说明：本模块是 `en_cl_fix` 的主 Python 接口，**设计目标就是镜像 HDL 实现**；内部用 `NarrowFix`/`WideFix` 做数值计算，对外只暴露裸数据。

再看 Python 版的 `cl_fix_round`，注意它的签名与 VHDL 几乎一一对应：

[bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py:190-212](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L190-L212) —— Python 的 `cl_fix_round(a, a_fmt, r_fmt, rnd)`。把它和 [hdl/en_cl_fix_pkg.vhd:134-140](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L134-L140) 的 VHDL 声明放在一起看，会发现参数名与含义完全对齐——这就是「镜像」。

包入口 `__init__.py` 把所有子模块聚拢：

[bittrue/models/python/en_cl_fix_pkg/__init__.py:20-24](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/__init__.py#L20-L24) —— 一行 `from .en_cl_fix import *` 等五条导入，使得 `import en_cl_fix_pkg` 之后就能直接调用 `en_cl_fix_pkg.cl_fix_round(...)`。这也是 MATLAB 能用 `py.en_cl_fix_pkg.cl_fix_round(...)` 调用到它的原因。

最后看 **MATLAB 薄封装**的完整套路，这是「三语言镜像」最直观的体现：

[bittrue/models/matlab/cl_fix_round.m:1-47](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/matlab/cl_fix_round.m#L1-L47) —— 整个文件就是三步：第 33 行 `mat2py` 转换输入、第 36 行 `py.en_cl_fix_pkg.cl_fix_round(...)` 调用 Python、第 39 行 `py2mat` 转换输出。注释（[cl_fix_round.m:3-5](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/matlab/cl_fix_round.m#L3-L5)）也直白地写了「MATLAB wrapper for implementation in en_cl_fix_pkg.py」。

#### 4.2.4 代码实践

- **实践目标**：亲手验证「MATLAB 只是 Python 的薄封装」。
- **操作步骤**：
  1. 打开 [bittrue/models/matlab/cl_fix_round.m](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/matlab/cl_fix_round.m)。
  2. 数一数：除了数据格式转换（`mat2py`/`py2mat`）和向量形状修正，它有没有任何一行「真正算定点数」的逻辑？
  3. 再打开同目录的 `cl_fix_add.m`、`cl_fix_mult.m`，看它们是否也是同样的三段式套路。
- **需要观察的现象**：所有 `.m` 封装文件都没有自己的算法，核心只有一句 `py.en_cl_fix_pkg.<同名函数>(...)`。
- **预期结果**：你会确信 MATLAB 端没有任何独立的定点实现，真正干活的是 Python；这也意味着维护成本被集中到了 Python（和 VHDL）两处。

#### 4.2.5 小练习与答案

- **练习 1**：为什么 `en_cl_fix` 不为 MATLAB 单独写一套定点算法？
  - **答案**：因为三种语言必须「位真一致」。如果把算法在 MATLAB 和 Python 各写一套，维护两套等价代码成本高、出错风险大；让 MATLAB 调用 Python，算法只有一份，一致性天然得到保证。
- **练习 2**：Python 包里 `en_cl_fix_types.py` 与 VHDL 里的什么东西对应？
  - **答案**：对应 [hdl/en_cl_fix_pkg.vhd:39-66](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L39-L66) 里的 `FixFormat_t`、`FixRound_t`、`FixSaturate_t` 三种类型——Python 用类/枚举把同样的概念重新表达了一遍。

---

### 4.3 tb/ 与 sim/：测试台与仿真装配

#### 4.3.1 概念说明

有了 RTL（`hdl/`）和参考模型（Python），下一步就是验证「RTL 的输出和参考模型是否完全一致」。这需要两类文件配合：

- **`tb/`（testbench，测试台）**：用 VHDL-2008 写的测试台。它们例化待测组件（UUT，Unit Under Test），给它喂输入，再逐拍检查输出是否等于「黄金期望值」。
- **`sim/`（simulation，仿真）**：Python 脚本，负责把所有 VHDL 源文件、测试台、库组装成一个 [VUnit](https://vunit.github.io/) 仿真工程，然后调用仿真器（GHDL / NVC / Modelsim / Questa）跑起来。

> 简单记：`tb/` 是「考卷」，`sim/` 是「监考系统」。考卷由监考系统统一调度分发。

#### 4.3.2 核心流程

验证链路（高层）如下：

```text
  bittrue/cosim/  ──运行 Python 参考模型──▶  黄金数据文件(文本)
        cosim.py                                  (输入/期望输出/格式)
                                                    │
                                                    ▼ 读取
  tb/cl_fix_round_tb.vhd  ◀── 例化 UUT(en_cl_fix_round) ── hdl/
        │ 逐拍比对 in/out 与黄金数据
        ▼
  sim/run.py  ──组装 VUnit 工程──▶  仿真器(GHDL/NVC/...)  ──▶  PASS/FAIL
```

- `bittrue/cosim/` 下每个操作（`cl_fix_round`、`cl_fix_add`、… 共 12 个）都有一个 `cosim.py`，它用 Python 模型生成穷举输入和期望输出，落成文本文件。
- `tb/` 里的测试台读这些文本文件，驱动 UUT，逐拍比对。
- `sim/run.py` 把整件事装配起来跑。

#### 4.3.3 源码精读

`tb/` 目录下有两类文件：每个操作一个 `cl_fix_*_tb.vhd`，外加一个总的 `en_cl_fix_pkg_tb.vhd`，以及一个工具包 `util/en_cl_fix_fileio_pkg.vhd`（负责文件读写）：

[tb/cl_fix_round_tb.vhd](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/tb/cl_fix_round_tb.vhd) —— `cl_fix_round` 的测试台，是「输入进程 → UUT → 检查进程」三段式结构的范例。

[tb/util/en_cl_fix_fileio_pkg.vhd](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/tb/util/en_cl_fix_fileio_pkg.vhd) —— 把 cosim 生成的格式/数据文件读进测试台的工具包（文件 I/O 封装）。

再看 `sim/run.py` 的装配逻辑，这是理解整个仿真工程组织的关键：

[sim/run.py:30-50](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/sim/run.py#L30-L50) —— `create_test_suite()` 函数展示了工程如何分层组装：先加 VUnit 自带库（OSVVM 等），再加 `en_tb` 库，再创建 `lib` 库并把 `hdl/*.vhd`（RTL）、`tb/util/*.vhd`（文件 I/O 扩展）、`tb/*.vhd`（测试台）依次加进去。这段代码清楚地反映了 `hdl/`、`tb/`、`lib/en_tb/` 三个目录在仿真工程里的层级关系。

README 也明确说明了怎么跑这些测试：

[README.md:190-198](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/README.md#L190-L198) —— Running Tests 章节给出了 Python 测试、MATLAB 测试和 VHDL 仿真（如 `python run.py --simulator=ghdl ...`）的命令示例。

#### 4.3.4 代码实践

- **实践目标**：理清 `sim/run.py` 里「库 → 源文件」的装配顺序。
- **操作步骤**：
  1. 打开 [sim/run.py](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/sim/run.py)。
  2. 在 `create_test_suite()` 里依次找出：哪几行加了 `en_tb` 库？哪几行加了 `hdl/*.vhd`？哪几行加了 `tb/util/*.vhd` 和 `tb/*.vhd`？
  3. 思考：为什么 RTL 要和测试台加到不同的库（或用不同的 VHDL 标准编译）？（提示：看 `vhdl_standard_rtl` 与 `vhdl_standard_tb` 两个变量）
- **需要观察的现象**：RTL 用一种 VHDL 标准（面向综合），测试台用另一种（可用 VHDL-2008，方便文件 I/O）。
- **预期结果**：你能用一句话说明「`sim/run.py` 把 `hdl/`、`tb/`、`lib/en_tb/` 三个目录的文件按不同标准编译进不同库」。
- 若本机未安装仿真器，运行步骤标注为「待本地验证」。

#### 4.3.5 小练习与答案

- **练习 1**：为什么测试台和 RTL 要用不同的 VHDL 标准？
  - **答案**：RTL 必须严格用 VHDL-93 以保证对所有综合工具链可综合（见 [README.md:17](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/README.md#L17)）；测试台只跑仿真、不需要综合，可以用功能更强的 VHDL-2008（如文件 I/O）。
- **练习 2**：`tb/util/en_cl_fix_fileio_pkg.vhd` 解决了什么问题？
  - **答案**：它封装了「从文本文件读取黄金数据 / 写出结果」的操作，让每个测试台不用重复写文件读写代码，专注于「喂输入、比对输出」。

---

### 4.4 lib/en_tb/：Enclustra 测试台基础库

#### 4.4.1 概念说明

`lib/en_tb/` 是一个**独立的子库**，名叫 `en_tb`（Enclustra Testbench）。它和定点运算本身无关，但 `en_cl_fix` 的测试台依赖它来做文件 I/O、报告进度等通用测试台杂活。

它的关键定位写在自己的 README 里：

> This library contains reusable VHDL code for testbench development. **This library should never be used for RTL development since it contains non-synthesizable code.**

也就是说：`en_tb` 只能用于仿真，绝不能综合成电路。它和 `hdl/`（可综合 RTL）形成明确的分工边界。

#### 4.4.2 核心流程

`en_tb` 在工程里被当作一个**被编译成名为 `en_tb` 的 VHDL 库**来使用：

```text
使用方式（VHDL 侧）：
    library en_tb;
    context en_tb.en_tb_fileio_context;   -- 用 context 引入，而不是逐条 use

使用方式（sim/run.py 侧）：
    en_tb = vu.add_library("en_tb")
    en_tb.add_source_files("../lib/en_tb/hdl/*.vhd")
```

注意目录下还自带 `.gitignore` 和独立的 `README.md`/`CHANGELOG.md`，说明它本质是一个**内嵌的（vendored）独立仓库**——只是把源码直接拷进主仓库里方便一起编译。

#### 4.4.3 源码精读

`en_tb` 的自述定位：

[lib/en_tb/README.md:26-35](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/lib/en_tb/README.md#L26-L35) —— 说明本库只放可复用的测试台代码，**绝不能用于 RTL**（含不可综合代码），并规定其源文件必须编译进名为 `en_tb` 的库、用 `context` 引用。

在主工程的仿真脚本里，能看到它被如何挂进来：

[sim/run.py:37-41](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/sim/run.py#L37-L41) —— 用 `try/except ValueError` 包住 `add_library("en_tb")`，避免重复添加；然后把 `lib/en_tb/hdl/*.vhd` 加入该库。这正是 4.3 里那条装配链的一环。

`en_tb` 内部也提供自己的 `sim/`、`bittrue/cosim/`、`tb/`（例如 [lib/en_tb/bittrue/cosim/en_tb_fileio/cosim.py](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/lib/en_tb/bittrue/cosim/en_tb_fileio/cosim.py)），结构与主仓库如出一辙——可以把它理解成「主仓库验证基础设施的小型复刻」。

#### 4.4.4 代码实践

- **实践目标**：确认 `en_tb` 与主仓库 RTL 的「可综合 / 不可综合」边界。
- **操作步骤**：
  1. 打开 [lib/en_tb/README.md](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/lib/en_tb/README.md)，找到「never be used for RTL」这句警告。
  2. 在 [sim/run.py](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/sim/run.py) 里确认：`lib/en_tb/hdl/*.vhd` 被加进了哪个库、用的是哪个 VHDL 标准。
  3. 对比：`hdl/*.vhd`（主仓库 RTL）和 `lib/en_tb/hdl/*.vhd` 分别加到哪个库、哪个标准？为什么不同？
- **需要观察的现象**：`en_tb` 的文件进了 `en_tb` 库（测试台标准），主仓库 RTL 进了 `lib` 库（RTL 标准），二者井水不犯河水。
- **预期结果**：你能解释「为什么文件 I/O 这类不可综合的功能必须放在 `lib/en_tb` 而不能放进 `hdl/`」。
- 若仅阅读源码，本步骤标注为「待本地验证」。

#### 4.4.5 小练习与答案

- **练习 1**：如果有人不小心把 `lib/en_tb/` 里的某个文件加到 `hdl/` 去综合，会怎样？
  - **答案**：综合会失败或产生大量告警，因为 `en_tb` 包含不可综合结构（文件 I/O、仿真专用构造）。这正是 README 反复强调「never for RTL」的原因。
- **练习 2**：`sim/run.py` 里用 `try/except ValueError` 包住 `add_library("en_tb")`，是为了防什么？
  - **答案**：防止 `en_tb` 库被重复创建（例如该库已被别处先添加过）。捕获后打印「already created, skip it」，保证脚本的健壮性。

---

## 5. 综合实践

**任务：制作一张「`cl_fix_round` 在三种语言下的镜像映射表」**

把本讲的知识串起来。请在仓库中实地查找并填写下表（部分已给出示例）：

| 维度 | VHDL（金标准 RTL） | Python（参考模型） | MATLAB（薄封装） |
| --- | --- | --- | --- |
| 所在目录 | `hdl/` | `bittrue/models/python/en_cl_fix_pkg/` | `bittrue/models/matlab/` |
| 对应文件 | `en_cl_fix_pkg.vhd` | `en_cl_fix.py` | `cl_fix_round.m` |
| 关键行号（函数定义） | 声明在 `:134-140`，实现在 `:910` | `:190` | 整文件 `:1-47`，调用 Python 在 `:36` |
| 谁是「真正干活的人」 | ✅ 自己实现 | ✅ 自己实现（镜像 HDL） | ❌ 调用 Python |
| 永久链接 | [hdl/en_cl_fix_pkg.vhd:134-140](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L134-L140) | [en_cl_fix.py:190-212](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L190-L212) | [cl_fix_round.m:1-47](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/matlab/cl_fix_round.m#L1-L47) |

**进阶**：再为 `cl_fix_add`、`cl_fix_mult` 各做一张同样的映射表（提示：VHDL 在 `en_cl_fix_pkg.vhd`，Python 在 `en_cl_fix.py`，MATLAB 在 `cl_fix_add.m` / `cl_fix_mult.m`）。你会发现三张表的结构完全相同——这就是「三语言镜像架构」的直观含义：**每个定点操作，在三种语言里都按同样的位置、同样的命名各放一份**。

完成后，请回答一个总结性问题：*如果要在本仓库新增一个定点操作 `cl_fix_foo`，至少需要新增/修改哪几个文件？*（参考答案：`hdl/en_cl_fix_pkg.vhd` 加 VHDL 函数、`en_cl_fix.py` 加 Python 镜像函数、`bittrue/models/matlab/cl_fix_foo.m` 加薄封装、`tb/cl_fix_foo_tb.vhd` 加测试台、`bittrue/cosim/cl_fix_foo/cosim.py` 加黄金数据生成脚本、并在 `sim/run.py` 里登记配置。）

---

## 6. 本讲小结

- 仓库顶层分为：`hdl/`（可综合 RTL）、`bittrue/`（位真模型与测试）、`tb/`（VHDL 测试台）、`sim/`（仿真装配）、`lib/en_tb/`（测试台基础子库）。
- `hdl/` 只有 5 个 `.vhd`：2 个包（主包 + 私有包）+ 3 个可流水线化组件（round / saturate / resize）。
- 「三语言镜像」：VHDL 是金标准，Python 同名同语义地镜像它（用作参考模型），MATLAB 只是调用 Python 的薄封装——算法只维护两份（VHDL + Python）。
- `tb/`（考卷）＋ `sim/`（监考系统）＋ `bittrue/cosim/`（黄金数据生成）三者协作完成「Python 生成期望 → HDL 逐拍比对」的验证闭环。
- `lib/en_tb/` 是不可综合的测试台基础库，必须编译进 `en_tb` 库，与可综合的 `hdl/` RTL 严格隔离。
- RTL 用 VHDL-93（为综合兼容），测试台用 VHDL-2008（为文件 I/O 等仿真便利），二者在 `sim/run.py` 里被分别编译进不同库。

---

## 7. 下一步学习建议

理解了目录与镜像架构后，建议按以下顺序继续：

1. **U2（定点数据模型）**：本讲你只看到了 `FixFormat_t`、`FixRound_t` 等类型的「名字」，U2 会讲清 `[S,I,F]`、七种舍入、四种饱和的真正含义——这是后续所有源码阅读的语义基础。
2. **U4（Python 参考实现）**：想深入「镜像」的另一端，可以读 [en_cl_fix.py](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py) 的完整接口，理解它如何逐函数对应 VHDL。
3. **U5（VHDL 包内部实现）**：想看「金标准」端如何实现，可深入 [hdl/en_cl_fix_pkg.vhd](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd) 的包体。
4. **U7（协同仿真验证）**：对 `tb/`＋`sim/`＋`bittrue/cosim/` 的验证闭环感兴趣，可继续学习基于 VUnit 的 Python↔HDL 协同仿真流程。

在进入这些主题前，先用本讲的「综合实践」巩固一遍三语言文件定位，会让你后续读源码时事半功倍。
