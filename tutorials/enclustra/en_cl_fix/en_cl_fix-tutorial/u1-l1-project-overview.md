# 项目总览与定位：什么是 en_cl_fix

## 1. 本讲目标

本讲是整本学习手册的第一篇，目标只有一个：**让你在不动手写算法的前提下，清楚 en_cl_fix 到底是什么、能做什么、由谁维护、依赖什么、怎么跑起来。**

学完本讲你应该能够：

- 用一两句话向同事解释 en_cl_fix 的定位（多语言定点数学库）。
- 说出它支持哪三种语言，以及 RTL 与 testbench 各自遵循的 VHDL 标准。
- 列出它提供的基本算术与格式转换（带舍入/饱和）能力。
- 知道它的开源许可（MIT）、维护方（Enclustra）和 Python 依赖（numpy、vunit-hdl）。
- 在本地装好依赖，并用 Python 打印一个 `FixFormat` 对象，确认环境就绪。

本讲**不**深入任何定点算法细节——舍入、饱和、格式推导等内容会留给后续讲义。本讲只读「项目的门牌」：`README.md`、`Changelog.md`、`LICENSE.md`、`requirements.txt`。

## 2. 前置知识

本讲面向零基础读者，但有几个名词先解释清楚会更顺：

- **定点数（fixed-point number）**：和浮点数相对。浮点数的小数点位置会「浮动」，而定点数的小数点位置是**固定**的——事先约定好一串二进制位里，哪几位是整数位、哪几位是小数位。en_cl_fix 就是一个专门处理这种数的库。具体格式 `[S, I, F]` 会在 [u1-l2](u1-l2-fixed-point-basics.md) 详细讲，这里你只要知道「它管定点数」即可。

- **FPGA / ASIC**：两种硬件实现方式。FPGA 是可编程的芯片，ASIC 是专用定制芯片。en_cl_fix 主要服务于这两类硬件开发中需要的数学运算。

- **HDL（硬件描述语言）**：用来描述数字电路的语言，本库用的是 VHDL。你可以把它理解成「写硬件的代码」。

- **RTL（Register Transfer Level）**：HDL 代码里**可被综合成真实电路**的那一层，对应本库中 VHDL-93 标准的代码。

- **Testbench（测试台）**：用来给 RTL 代码「喂输入、看输出」的仿真代码，本身不会被烧进芯片，对应本库中 VHDL-2008 标准的代码。

- **Co-simulation（协同仿真）**：用 Python 算出「正确答案（黄金参考）」，再让 VHDL 仿真跑一遍，两边对拍。这是本库的核心验证手段，会在单元 8 详讲。

不需要你现在就精通这些，带着这些关键词往下读即可。

## 3. 本讲源码地图

本讲只涉及仓库根目录下的四个「元数据」文件，它们是了解整个项目的入口：

| 文件 | 作用 | 本讲怎么用 |
|------|------|-----------|
| [README.md](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/README.md) | 项目的「门牌」：定位、能力、支持语言、许可、维护方、依赖、定点数基础 | 重点阅读 General Information / License / Dependencies 三节 |
| [Changelog.md](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/Changelog.md) | 版本演进记录，每个版本列 Features 与 Bugfixes | 梳理项目从 1.0.0 到 2.3.0 的能力变化脉络 |
| [LICENSE.md](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/LICENSE.md) | MIT 许可证全文 | 确认商用合规 |
| [requirements.txt](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/requirements.txt) | Python 依赖的精确版本锁 | 装依赖用 |

> 说明：本讲引用的永久链接都指向当前 HEAD `d2ce1a6`，行号与该 commit 一致。

## 4. 核心概念与源码讲解

按「最小模块」拆成三块：**4.1 README 总览（含许可与维护方）**、**4.2 Changelog 版本演进**、**4.3 requirements.txt 依赖**。

### 4.1 README General Information：项目是什么

#### 4.1.1 概念说明

打开任何开源项目，第一步都是读 README 的开头那几行——它用最精炼的话告诉你「这东西是干嘛的」。en_cl_fix 的自我定位是：

> 一个**免费、开源、多语言**的**定点数学库**，服务于 FPGA 和 ASIC 开发。

拆成三个关键词理解：

1. **定点数学库**：它不解决通用计算问题，只解决「定点数」的算术与格式转换。定点数是数字电路里最常用的数值表示方式之一。
2. **多语言**：同一套功能，在三种语言里都能用——VHDL（硬件）、Python（软件/验证）、MATLAB（算法原型）。三语言 API 是**镜像**的，函数名几乎一一对应。
3. **低层（low-level）**：它提供的是「砖块」级别的基本运算，不是「整栋楼」级别的信号处理框架。README 明确说，更上层的用法可以参考开源库 [psi_fix](https://github.com/paulschillerinstitute/psi_fix)，后者内部就是调用 en_cl_fix。

能力上，README 把功能归纳为两类：

- **基本算术**：加法、乘法等。
- **数格式转换**：带舍入（rounding）与饱和（saturation）的格式转换。

还有一条很关键的性能特性：**支持任意精度，但在位宽 ≤ 53 位时执行更快**。53 这个数字的由来（双精度浮点的尾数位数）会在 [u6-l1](u6-l1-narrow-fix.md) 的 NarrowFix 讲义里揭晓。

#### 4.1.2 核心流程

从一个使用者的视角，en_cl_fix 在工作流里的位置可以这样理解（伪流程）：

```
算法原型(MATLAB/Python)  ──┐
                           ├──> 都调用同一套 cl_fix_* 接口
RTL 实现(VHDL)          ──┘
                           │
                           v
        Co-simulation: Python 算黄金参考, VHDL 仿真对拍
```

要点：

- 三语言共享**同名**的接口（如 `cl_fix_add`、`cl_fix_from_real`），所以你在一个语言里写的算法，能近乎平移到另一个语言。
- VHDL 这一侧又被拆成两类代码：能综合成电路的 **RTL（VHDL-93）** 和只用于仿真的 **testbench（VHDL-2008）**。这个区分直接决定了哪些文件能进芯片、哪些只在验证时用。

#### 4.1.3 源码精读

逐段看 README 的关键行。

**项目定位**——第一句话就把身份说清楚了：

[README.md:L1-L5](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/README.md#L1-L5) —— 说明 en_cl_fix 是面向 FPGA/ASIC 的多语言定点数学库，提供基本算术与带舍入/饱和的格式转换。

**精度特性**——这一行解释了为什么后面会有 Narrow/Wide 两套实现：

[README.md:L7](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/README.md#L7) —— 「支持任意精度，但位宽 ≤ 53 位时执行更快」。53 位是浮点双精度的尾数宽度，是库内部分两条实现路径的边界。

**支持语言与标准**——这是本讲最重要的「能力清单」之一：

[README.md:L9-L17](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/README.md#L9-L17) —— 支持 VHDL / Python / MATLAB 三种语言；并明确：**所有 RTL 代码符合 VHDL-93**（为了最大兼容各综合工具链），**testbench 符合 VHDL-2008**。这一行也顺带说明了 C++（实验性，基于 GMP）和 SystemVerilog（2024 调研过，但工具链支持差）的现状。

> 名词解释：为什么 RTL 要用更老的 VHDL-93？因为综合工具（把代码变成电路的软件）对新标准支持参差不齐，用最通用的 VHDL-93 能保证「到处能综合」。而 testbench 不进芯片，只跑仿真，所以可以用更新、更方便的 VHDL-2008。

**用法示例**：

[README.md:L23-L27](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/README.md#L23-L27) —— 高层用法可参考 psi_fix（内部调用 en_cl_fix）；底层测试用例随仓库一起提供。

**许可**：

[README.md:L29-L32](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/README.md#L29-L32) —— MIT 许可，允许商用。完整许可全文见 [LICENSE.md:L1-L4](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/LICENSE.md#L1-L4)，版权归属 `2024 Enclustra GmbH, Switzerland`。

**维护方**：

[README.md:L34-L37](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/README.md#L34-L37) —— 由 Enclustra GmbH 维护，并在自家 FPGA 项目里实际使用了十年以上（截至 2024 年）。这是一个「被自己用着」的库，活跃度有保障。

#### 4.1.4 代码实践

**实践目标**：确认你读懂了 README 的定位描述，能用一句话复述。

**操作步骤**：

1. 打开仓库根目录的 `README.md`，只读前 40 行。
2. 找到「Supported Languages」一节，数一下它一共列了几种语言，并标注每种语言对应的 VHDL 标准（如果适用）。
3. 找到「License」与「Maintainers」两节，记录许可类型和维护公司。

**需要观察的现象**：

- Supported Languages 一节列出的语言里，只有 VHDL 带了上标星号 `*`，星号的脚注解释了 RTL 与 testbench 分别用哪个 VHDL 标准。

**预期结果**：

- 语言数：**3 种**（VHDL、Python、MATLAB）。
- VHDL 标准：RTL 用 VHDL-93，testbench 用 VHDL-2008。
- 许可：MIT；维护方：Enclustra GmbH。

这些事实都来自上面对应的永久链接，你可以点击核对。

#### 4.1.5 小练习与答案

**练习 1**：README 说库「在位宽 ≤ 53 位时执行更快」。请问这个 53 位指的是总位宽 `S+I+F`，还是别的什么？

> **参考答案**：指的是定点数的总位宽。当一个 `FixFormat` 的总宽度 \( S+I+F \leq 53 \) 时，库会走更快的 NarrowFix（双精度浮点）实现路径；超过 53 位则走任意精度的 WideFix 路径。详见 [u6-l1](u6-l1-narrow-fix.md)。

**练习 2**：为什么 RTL 用 VHDL-93 而 testbench 用 VHDL-2008？能不能反过来？

> **参考答案**：RTL 要被综合工具变成真实电路，而综合工具对新标准支持不一，用最通用的 VHDL-93 能保证可综合性。testbench 只在仿真时跑、不进芯片，所以可以用语法更便利的 VHDL-2008。反过来（RTL 用 2008）会降低可综合性，不建议。

---

### 4.2 Changelog 版本演进

#### 4.2.1 概念说明

`Changelog.md` 是项目的「成长日记」。读它的价值在于：**不用读一行代码，就能看出这个项目的能力是怎么一步步长出来的**。对于初学者，这是快速建立全局认知的好办法——你会看到哪些功能是后加的、哪些是重写过的，从而知道项目的「重心」在哪。

en_cl_fix 的版本号语义很朴素：

- **Features**：新增了什么能力。
- **Bugfixes**：修了什么问题（常带具体工具链名称，说明是在实战中踩出来的）。

#### 4.2.2 核心流程

把 Changelog 从下往上（从旧到新）读，可以梳理出一条清晰的能力演进时间线：

```
1.0.0  VHDL + MATLAB 实现（最早只有这两种语言）
  │
1.1.0  加入 Python 实现 + 单元测试
  │
1.2.0  加入「宽定点」(>53 位) Python 支持
  │
2.0.0  大重构：三语言对齐、Python 拆 Narrow/Wide、迁移到 VUnit 验证流程
  │
2.1.0  加入 testbench 文件 I/O（集成 lib/en_tb）、MATLAB 任意精度
  │
2.2.0  加入 round/saturate/resize 三个可综合 VHDL 组件
  │
2.3.0  加入 NVC 仿真器支持、Questa 三步流程、宽度检查
```

三个 takeaway：

1. **Python 是后来才有的**（1.1.0），现在却是验证体系的核心。
2. **2.0.0 是分水岭**——它把三语言统一、把验证迁移到 VUnit，今天你看到的代码结构基本是 2.0.0 定下的。
3. **可综合 RTL 组件**（round/saturate/resize）是 2.2.0 才补上的，说明库从「纯函数库」逐渐长出了「可直接例化的硬件模块」。

#### 4.2.3 源码精读

**当前最新版 2.3.0**——也就是本讲所基于的版本：

[Changelog.md:L1-L6](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/Changelog.md#L1-L6) —— 2.3.0 新增 NVC 仿真器支持、Questa 三步流程，并为 VHDL 加入宽度检查（确保数据位宽与格式匹配，对应 issue #33）。这也解释了 git 历史里 `Reduce NVC memory allocations`、`Avoid en_tb library name clash` 等近期提交的背景。

> 名词解释：NVC 和 Questa 都是 VHDL 仿真器；「三步流程（3-step flow）」指分析→精化→运行的仿真流程，比一步流程更适合大型设计。

**分水岭版本 2.0.0**——读懂它就读懂了今天的代码结构：

[Changelog.md:L24-L31](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/Changelog.md#L24-L31) —— 2.0.0 对 VHDL/MATLAB/Python 三套代码都做了大重构：MATLAB 现在只是调用 Python；Python 用两个独立类封装 Narrow 与 Wide 支持；验证流程迁移到 VUnit；并把格式字段从 `(Signed, IntBits, FracBits)` 改成了更简短的 `(S, I, F)`。

> 这条尤其重要：它解释了为什么你在代码里到处看到 `S, I, F` 三个字母——这是 2.0.0 之后的新约定，和上游 psi_fix 保持一致。

**可综合组件的加入**：

[Changelog.md:L11-L13](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/Changelog.md#L11-L13) —— 2.2.0 新增了 `en_cl_fix_round` / `en_cl_fix_saturate` / `en_cl_fix_resize` 三个 VHDL 组件（带握手端口的可综合实体），相关源码在 `hdl/` 下，会在 [u7-l1](u7-l1-rtl-components.md) 详讲。

**起点**：

[Changelog.md:L118-L120](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/Changelog.md#L118-L120) —— 1.0.0 是首个发布，只包含 VHDL 和 MATLAB 实现，还没有 Python。

#### 4.2.4 代码实践

**实践目标**：用 Changelog 建立一张「能力获得时间表」，体会项目的演进重心。

**操作步骤**：

1. 打开 `Changelog.md`，从最底部（1.0.0）往最顶部（2.3.0）读。
2. 准备一张表，只记录每个版本的 **一条最重要的 Feature**。
3. 特别留意：Python 实现是哪个版本引入的？「宽定点（>53 位）」支持是哪个版本？VUnit 验证流程是哪个版本？

**需要观察的现象**：

- 你会发现「修复（Bugfixes）」里频繁出现具体工具链名字（Quartus、Vivado、Modelsim、GHDL、Efinity、Gowin），说明这个库要兼容很多 FPGA 工具链。

**预期结果**（关键节点）：

| 版本 | 最重要 Feature |
|------|----------------|
| 1.0.0 | 首发含 VHDL + MATLAB |
| 1.1.0 | 加入 Python 实现 + 单元测试 |
| 1.2.0 | 宽定点（>53 位）Python 支持 |
| 2.0.0 | 三语言大重构，迁移到 VUnit |
| 2.1.0 | testbench 文件 I/O（集成 en_tb） |
| 2.2.0 | round/saturate/resize VHDL 组件 |
| 2.3.0 | NVC 仿真器、宽度检查 |

（完整明细以上面给出的永久链接为准。）

#### 4.2.5 小练习与答案

**练习 1**：今天的 MATLAB 实现是「原生」的吗？如果不是，从哪个版本开始变了？

> **参考答案**：不是原生的。从 2.0.0 起，MATLAB 代码经过大重构，**现在只是调用 Python 函数**（见 [Changelog.md:L24-L31](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/Changelog.md#L24-L31)）。这意味着三语言其实共享同一套 Python 计算内核。MATLAB↔Python 的桥接细节见 [u9-l1](u9-l1-matlab-bridge.md)。

**练习 2**：格式字段为什么从 `(Signed, IntBits, FracBits)` 变成了 `(S, I, F)`？这属于哪个版本的变化？

> **参考答案**：属于 2.0.0 大重构的一部分，目的是「更简短」，并与上游库 psi_fix 保持一致。这是今天所有 `FixFormat` 都用 `S, I, F` 的根因。

---

### 4.3 requirements.txt 依赖

#### 4.3.1 概念说明

`requirements.txt` 是 Python 项目的依赖清单——它列出「要让这个项目的 Python 部分跑起来，需要先装哪些第三方包」。en_cl_fix 的 Python 部分承担两个角色，所以依赖也分两类：

- **作为数学模型**：需要 `numpy`（数值计算基础库）。
- **作为仿真验证驱动**：需要 `vunit-hdl`（VHDL 仿真框架，用来组织 testbench、调用仿真器）。

注意：**只有用到 Python 模型或 VHDL 仿真时才需要这些依赖**。如果你只想读 VHDL 源码或把它综合进 FPGA，严格说不一定需要 Python 环境。但本手册的实践都依赖可运行的 Python 环境，所以建议装上。

#### 4.3.2 核心流程

依赖安装的标准流程（README 给出的官方命令）：

```
1. 确保有 Python 3（README 标注测试过 >= 3.10）
2. 在仓库根目录执行： python -m pip install -r requirements.txt
3. pip 会按 requirements.txt 里钉死的版本号安装 numpy 与 vunit-hdl
```

`requirements.txt` 用的是**精确版本锁定（`==`）**，而不是范围约束（`>=`）。这意味着：

- 好处：任何人装出来的依赖版本完全一致，复现性最好。
- 代价：你本机若已有不兼容版本，需要让它让位。

值得一提的是，README 的 Dependencies 小节写的是「测试过的最低版本」（numpy >= 1.24.3、vunit-hdl >= 5.0.0.dev6），而 `requirements.txt` 钉的是当前实际使用的较新版本——两者并不矛盾，前者是兼容下限，后者是锁定的精确版本。

#### 4.3.3 源码精读

**依赖清单本体**——整个文件只有两行：

[requirements.txt:L1-L2](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/requirements.txt#L1-L2) —— 钉死两个依赖：`numpy==2.3.2` 与 `vunit-hdl==5.0.0.dev6`。前者是数值计算库，后者是 VHDL 仿真框架。

**README 里对依赖的文字说明与安装命令**：

[README.md:L42-L55](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/README.md#L42-L55) —— 列出 Python 3（>= 3.10）、numpy（>= 1.24.3）、vunit-hdl（>= 5.0.0.dev6），并给出安装命令 `python -m pip install -r requirements.txt`。

> 对比提示：README 写 `numpy >= 1.24.3`，而 requirements.txt 锁定 `numpy==2.3.2`。这说明 1.24.3 是「兼容下限」，2.3.2 是「当前实际锁定版本」。装的时候以 requirements.txt 为准。

**MATLAB 与仿真器依赖**（顺带了解，无需在本讲安装）：

[README.md:L57-L65](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/README.md#L57-L65) —— MATLAB 测试过 R2023b；VHDL 仿真支持 VUnit 兼容的所有现代仿真器，实测过 GHDL 4.1.0、NVC 1.17.1、多种 Modelsim/Questa 版本。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：装好依赖，并让 Python 能成功 `import en_cl_fix_pkg`、打印一个 `FixFormat(1,4,8)`，从而确认环境就绪。

**操作步骤**：

1. 确认 Python 版本（建议 ≥ 3.10）：

   ```bash
   python --version
   ```

2. 在仓库根目录安装依赖：

   ```bash
   python -m pip install -r requirements.txt
   ```

   > 若网络受限或只想跑 Python 模型（暂不跑 VHDL 仿真），最少需要 `numpy`；`vunit-hdl` 只有在跑 `sim/run.py` 时才用到。

3. 让 `en_cl_fix_pkg` 可被导入。它位于 `bittrue/models/python/en_cl_fix_pkg/`，并不是一个用 pip 安装的包，所以需要把它所在的目录加进 Python 路径。两种等价做法任选其一：

   - **做法 A（进入目录）**：

     ```bash
     cd bittrue/models/python
     python -c "import en_cl_fix_pkg as f; print(repr(f.FixFormat(1,4,8)))"
     ```

   - **做法 B（设 PYTHONPATH，不切目录）**：

     ```bash
     python -c "import en_cl_fix_pkg as f; print(repr(f.FixFormat(1,4,8)))"
     ```
     （执行前先 `export PYTHONPATH="$PWD/bittrue/models/python"`）

4. 更完整地观察 `FixFormat` 的三种表示与位宽：

   ```bash
   cd bittrue/models/python
   python -c "import en_cl_fix_pkg as f; fmt=f.FixFormat(1,4,8); print(repr(fmt)); print(str(fmt)); print(fmt.width)"
   ```

**需要观察的现象**：

- `repr()` 给出「能被 `eval` 还原」的正式字符串。
- `str()` 给出更简短的可读字符串。
- `width` 是总位宽 \( S+I+F = 1+4+8 \)。

**预期结果**：

```
FixFormat(1, 4, 8)
(1, 4, 8)
13
```

这三行输出对应源码 [en_cl_fix_types.py:L365-L371](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py#L365-L371)（`__repr__` / `__str__`）和 [en_cl_fix_types.py:L378-L381](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py#L378-L381)（`width` 属性）。只要看到这三行，说明你的 Python 环境和 en_cl_fix 包都就绪了。

> ⚠️ 说明：本实践假设你能正常 `pip install`。若在你机器上跑出的依赖版本/输出与本讲不一致，或某些命令无法运行，请标注「待本地验证」并记录实际现象——不要假装已经跑过。

#### 4.3.5 小练习与答案

**练习 1**：`requirements.txt` 里用的是 `==` 还是 `>=`？这种写法的优缺点分别是什么？

> **参考答案**：用的是 `==`（精确锁定，`numpy==2.3.2`、`vunit-hdl==5.0.0.dev6`）。优点是所有人装出的依赖版本完全一致、复现性强；缺点是若你环境里已有不兼容的同名包，需要先处理冲突，且不会自动获得依赖的新版本。

**练习 2**：README 写 `numpy >= 1.24.3`，requirements.txt 写 `numpy==2.3.2`，这两者矛盾吗？

> **参考答案**：不矛盾。README 给的是「兼容下限」（实测能跑的最低版本），requirements.txt 给的是「当前锁定的精确版本」。2.3.2 ≥ 1.24.3，完全满足下限要求；安装时以 requirements.txt 为准。

**练习 3**：如果你只想用 en_cl_fix 的 VHDL 源码做综合、完全不碰 Python，是否必须装这两个依赖？

> **参考答案**：不是必须。`numpy` 与 `vunit-hdl` 只在跑 Python 模型或 VUnit 仿真验证时才需要。纯综合流程只需 VHDL 源码（`hdl/` 下）和你的综合工具链。但本手册的实践大多依赖可运行 Python 环境，故仍建议安装。

## 5. 综合实践

把本讲三块内容串起来，完成下面这个「五分钟摸清项目」的小任务：

**任务**：写一段「项目一句话介绍 + 环境自检报告」。

1. **读**：只读 `README.md` 的 General Information / License / Maintainers / Dependencies 四节，以及 `Changelog.md` 的 2.3.0 与 2.0.0 两段。
2. **装**：执行 `python -m pip install -r requirements.txt`。
3. **跑**：执行 4.3.4 中的命令，确认输出 `FixFormat(1, 4, 8)` / `(1, 4, 8)` / `13`。
4. **写**：用一句话回答——「en_cl_fix 是 ___，由 ___ 维护，许可证是 ___，Python 依赖 ___ 和 ___，我已确认环境就绪（打印出了 ___）。」

**参考填空答案**：

> en_cl_fix 是**一个免费、开源、多语言（VHDL/Python/MATLAB）的定点数学库**，由 **Enclustra GmbH** 维护，许可证是 **MIT**，Python 依赖 **numpy** 和 **vunit-hdl**，我已确认环境就绪（打印出了 `FixFormat(1, 4, 8)`）。

完成这一步，你就建立起了对整个项目的「顶层心智模型」，后续每一讲都是在往这个骨架上挂细节。

## 6. 本讲小结

- **定位**：en_cl_fix 是面向 FPGA/ASIC 的免费、开源、多语言（VHDL / Python / MATLAB）定点数学库，提供基本算术与带舍入/饱和的格式转换。
- **语言标准**：RTL 代码符合 VHDL-93（保证可综合性），testbench 符合 VHDL-2008（仿真便利）；这是区分「进芯片」与「只仿真」代码的关键。
- **精度边界**：支持任意精度，但位宽 ≤ 53 位时更快——这条线把实现分成 Narrow / Wide 两套（详见单元 6）。
- **许可与维护**：MIT 许可（可商用），由 Enclustra GmbH 维护并自用十年以上。
- **演进脉络**：Python 是 1.1.0 才加入的；2.0.0 是分水岭（三语言对齐、迁到 VUnit、改用 `S,I,F`）；可综合 RTL 组件是 2.2.0 才有的。
- **依赖**：`requirements.txt` 钉死 `numpy==2.3.2`、`vunit-hdl==5.0.0.dev6`；用 `python -m pip install -r requirements.txt` 安装。

## 7. 下一步学习建议

本讲只读了「门牌」，还没真正接触定点数本身。建议按以下顺序继续：

1. **下一讲 [u1-l2](u1-l2-fixed-point-basics.md)：定点数基础**——搞懂 `[S, I, F]` 三字段、位权和补码符号位权重，这是后续所有讲义的共同语言。
2. **然后 [u1-l3](u1-l3-repository-structure.md)：仓库目录结构**——弄清 `hdl/`、`bittrue/`、`tb/`、`sim/`、`lib/` 各自的职责，建立「代码地图」。
3. **再 [u1-l4](u1-l4-quick-start-tests.md)：快速上手运行测试**——把 Python 测试真正跑起来，验证环境。

读完单元 1 的四篇，你就具备了进入单元 2（核心类型与三语言 API 地图）的全部准备。如果你想提前感受「真实用法」，可以扫一眼 `bittrue/tests/python/` 下的测试文件，看看 `cl_fix_*` 函数是怎么被调用的——但不用现在读懂，那是后续讲义的任务。
