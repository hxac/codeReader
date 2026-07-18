# VHDL 编码规范与命名约定

## 1. 本讲目标

学完本讲，你应该能够：

- 看到任意一个 PoC 源码文件名（如 `arith_addw.vhdl`），立刻说出它属于哪个命名空间、对应哪个实体、测试台叫什么。
- 知道 PoC 用 `rtl` 架构表示可综合实现、用 `_tb` 后缀 + `tb` 架构表示测试台，并理解为什么要这样分。
- 写出一个符合 PoC 规范的 VHDL 文件头（编辑器配置 + 文档头 + 许可证声明）。
- 掌握 PoC 在大小写、缩进（用 Tab 而非空格）、信号初始化、表达式括号、实例化方式上的硬性约定。
- 独立为一个虚拟核 `mylib_echo` 写出符合规范的文件头、`entity` 声明与 `rtl` 架构骨架。

## 2. 前置知识

在阅读本讲前，你需要先建立以下认知（来自前置讲义 u1-l2）：

- **命名空间（namespace）**：PoC 把数以百计的 IP 核按功能归类到 `src/` 下的子目录，如 `arith`（算术）、`fifo`（先入先出）、`mem`（存储器）、`misc/sync`（时钟域同步）等。目录名基本就是 VHDL 命名空间名。
- **每个核的三类伴生文件**：源码 `.vhdl`、编译清单 `.files`、命名空间级包 `<ns>.pkg.vhdl`。
- **测试台镜像源码**：`tb/` 目录是 `src/` 的镜像子集，源码里有一个 `src/arith/arith_addw.vhdl`，测试台目录里就有一个 `tb/arith/arith_addw_tb.vhdl`。

本讲不深入任何 IP 核的电路原理，只讲「这份代码长成这个样子」的约定。理解这些约定，是你后续阅读任何 PoC 源码的「通行证」。

> 一个关键直觉：PoC 是一个有数百个核的大型公共库，由多人长期协作维护。如果没有统一的命名与风格约定，源码会迅速退化成无法检索、无法批量处理的烂泥。所以 PoC 把约定写成了一份强制规范 [`vhdl_coding.md`](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/vhdl_coding.md)，本讲就是逐条拆解它。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲怎么用它 |
| --- | --- | --- |
| `vhdl_coding.md` | PoC 官方 VHDL 编码规范总纲 | 作为「规则原文」逐条引用，是本讲的权威依据 |
| `src/arith/arith_addw.vhdl` | 一个真实的可综合 IP 核（宽位加法器） | 作为「规范落地」的范例，验证每条规则在真实代码里的样子 |
| `tb/arith/arith_addw_tb.vhdl` | `arith_addw` 的测试台 | 验证 `_tb` 后缀与 `tb` 架构约定 |
| `src/arith/arith.pkg.vhdl` | `arith` 命名空间的组件声明包 | 说明「组件声明」如何与实体名对应 |
| `docs/Entity.template` | 每个核的 Sphinx 文档页模板 | 说明规范如何与文档生成工具配合 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**命名与文件约定**、**架构命名（rtl / tb）**、**文档头与代码风格**。

### 4.1 命名与文件约定

#### 4.1.1 概念说明

PoC 是一个大型公共 IP 核库。当库里有数百个核、分布在十几个命名空间时，最基本的问题是：**看到一个文件名，能不能立刻知道它是什么、属于哪里、该去哪找它的测试台？**

PoC 的答案是用一套严格的命名约定来回答这些问题。它要求：

1. 所有 VHDL 源码统一用 `.vhdl` 后缀（而不是 `.vhd`）。
2. 实体名采用「蛇形命名（snake_case）」，并且**以所属命名空间作为前缀**：`<namespace>_<entity>`。
3. **一个实体单独占一个文件**，文件名就是 `<entity>.vhdl`。

这样做的好处是：文件名本身就是「地址」。看到 `arith_addw.vhdl`，你就知道它是 `arith` 命名空间下的 `addw`（wide adder，宽位加法器）核，源码在 `src/arith/`，测试台一定叫 `arith_addw_tb.vhdl` 且在 `tb/arith/`。

#### 4.1.2 核心流程

把一个文件名拆解成定位信息的流程：

```text
文件名: arith_addw.vhdl
        ─┬─    ──┬──  ─┬─
         │       │     └─ 后缀 .vhdl  => 是 VHDL 源码（非 .vhd）
         │       └─ 实体名 addw       => wide adder，宽位加法器
         └─ 命名空间 arith            => 源码在 src/arith/，包是 arith.pkg.vhdl

推断:
  源码路径        src/arith/arith_addw.vhdl
  测试台实体名    arith_addw_tb
  测试台文件      tb/arith/arith_addw_tb.vhdl
  命名空间包      src/arith/arith.pkg.vhdl（在其中声明 component arith_addw）
```

#### 4.1.3 源码精读

规范原文在 [`vhdl_coding.md:8-17`](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/vhdl_coding.md#L8-L17) 的 Naming 小节，其中前三条就是命名与文件约定：

> 1. VHDL 源码文件用 `.vhdl` 后缀。
> 2. 实体名前加上所属命名空间，用蛇形命名，例如 `arith` 包里的宽位加法器叫 `arith_addw`。每个模块单独实现在自己的源码文件里，文件名是 `<entity>.vhdl`。
> 3. 可综合的模块实现放在名为 `rtl` 的架构里。（第 3 条留到 4.2 讲）

来看真实范例 [`src/arith/arith_addw.vhdl:54-71`](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith_addw.vhdl#L54-L71)：实体名与文件名完全一致，都是 `arith_addw`。

```vhdl
entity arith_addw is     -- 命名空间 arith + 实体 addw，与文件名 arith_addw.vhdl 一致
  generic (
    N : positive;                    -- Operand Width
    ...
  );
  port (
    a, b : in std_logic_vector(N-1 downto 0);
    ...
  );
end entity;
```

命名空间包 [`src/arith/arith.pkg.vhdl:42-54`](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith.pkg.vhdl#L42-L54) 里集中声明了该命名空间所有核的 `component`，组件名与实体名一一对应：

```vhdl
package arith is
  component arith_firstone is     -- component 名 = 实体名 = arith_firstone
    generic ( ... );
    port ( ... );
  end component;
  ...
```

> 这意味着：`arith.pkg.vhdl` 就是一份「arith 命名空间的核清单」。阅读一个新命名空间时，先打开它的 `<ns>.pkg.vhdl`，就能快速知道里面有哪些核、各自的端口长什么样。

#### 4.1.4 代码实践

1. **实践目标**：训练「从文件名反推定位信息」的能力。
2. **操作步骤**：
   - 在仓库里浏览 `src/` 下的各命名空间目录，挑出 3 个核文件，例如 `src/fifo/fifo_cc_got.vhdl`、`src/mem/ocram/ocram_sp.vhdl`、`src/misc/sync/sync_Bits.vhdl`。
   - 对每个文件，按 4.1.2 的流程写出：命名空间、实体名、推测的测试台文件名、推测的命名空间包文件名。
   - 用 Glob 或直接 `ls tb/` 验证你推测的测试台文件是否真的存在。
3. **需要观察的现象**：测试台文件名是否与你推测的 `<entity>_tb.vhdl` 完全一致；命名空间包里是否能找到同名 `component` 声明。
4. **预期结果**：例如对 `fifo_cc_got.vhdl`，应推测出测试台 `tb/fifo/fifo_cc_got_tb.vhdl`、包 `src/fifo/fifo.pkg.vhdl`，且包内应有 `component fifo_cc_got`。

#### 4.1.5 小练习与答案

**练习 1**：为什么 PoC 选择 `<namespace>_<entity>` 的前缀命名，而不是只用 `<entity>`？

> **参考答案**：因为不同命名空间里可能存在功能相似、名字相同的核（例如多个命名空间都可能有叫 `sp`、`tx`、`wrapper` 的实体）。加命名空间前缀可以保证全局唯一，避免编译进同一个 `PoC` 库时发生实体名冲突，同时也让文件名自带「地址」信息。

**练习 2**：`.vhdl` 和 `.vhd` 都是 VHDL 文件后缀，PoC 为什么固定用 `.vhdl`？

> **参考答案**：统一后缀是为了让 pyIPCMI 等工具链、`.files` 编译清单、文档生成脚本能用一个简单的规则识别 VHDL 源码，避免遗漏 `.vhd` 文件。这是一种「约定优于配置」的工程取舍。

---

### 4.2 架构命名：rtl 与 tb

#### 4.2.1 概念说明

一个 IP 核通常有两类代码：

- **可综合实现（synthesizable）**：能被综合工具变成真实电路门/查找表的代码，最终烧进 FPGA。这部分必须严格符合可综合子集。
- **测试台（testbench）**：用来给核喂激励、检查输出的代码，只在仿真器里跑，永远不综合。它可以写 `wait for 10 ns;`、可以用 `report` 打印、可以读文件。

这两类代码的「形状」完全不同。PoC 用**架构名**和**实体名后缀**把二者干净地分开：

- 可综合实现：架构名固定叫 **`rtl`**。
- 测试台：实体名加 **`_tb`** 后缀，架构名固定叫 **`tb`**。

#### 4.2.2 核心流程

| 角色 | 实体名 | 架构名 | 文件位置 | 文件名 |
| --- | --- | --- | --- | --- |
| 可综合核 | `arith_addw` | `rtl` | `src/arith/` | `arith_addw.vhdl` |
| 测试台 | `arith_addw_tb` | `tb` | `tb/arith/` | `arith_addw_tb.vhdl` |

```text
src/arith/arith_addw.vhdl      =>  entity arith_addw ... architecture rtl ...
tb/arith/arith_addw_tb.vhdl    =>  entity arith_addw_tb ... architecture tb ...
                                              └─ 例化 arith_addw 作为 DUT
```

#### 4.2.3 源码精读

规范原文见 [`vhdl_coding.md:13-17`](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/vhdl_coding.md#L13-L17)：

> 3. 可综合的模块实现通过名为 `rtl` 的架构提供。
> 4. 测试台实体的名字复制被测模块或包的名字，并追加 `_tb`；它的实现架构命名为 `tb`。

可综合侧——[`src/arith/arith_addw.vhdl:79`](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith_addw.vhdl#L79) 的架构头：

```vhdl
architecture rtl of arith_addw is    -- 'rtl' 表示这是可综合实现
```

测试台侧——[`tb/arith/arith_addw_tb.vhdl:46-50`](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/tb/arith/arith_addw_tb.vhdl#L46-L50)：

```vhdl
entity arith_addw_tb is     -- 实体名 = 被测核名 + '_tb'
end entity;

architecture tb of arith_addw_tb is   -- 架构名固定为 'tb'
```

注意测试台的文档头里，标签写的是 `Testbench:` 而不是 `Entity:`，见 [`tb/arith/arith_addw_tb.vhdl:8`](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/tb/arith/arith_addw_tb.vhdl#L8)，这也是区分源码与测试台的一个小信号。

> 小提示：测试台实体通常**没有端口**（如上面的 `entity arith_addw_tb is end entity;`），因为仿真顶层不连接任何外部引脚，所有激励都在架构内部生成。

#### 4.2.4 代码实践

1. **实践目标**：确认 `rtl` / `tb` 命名约定在整个仓库里是一致应用的。
2. **操作步骤**：
   - 打开 [`tb/arith/arith_addw_tb.vhdl`](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/tb/arith/arith_addw_tb.vhdl)，找到它的 `entity` 行和 `architecture` 行，确认分别是 `arith_addw_tb` 和 `tb`。
   - 用 Grep 在 `tb/` 目录搜索 `architecture tb of`，统计有多少个测试台遵循该约定。
   - 用 Grep 在 `src/` 目录搜索 `architecture rtl of`，确认可综合核都用 `rtl`。
3. **需要观察的现象**：绝大多数（理想情况下全部）测试台架构名都是 `tb`，可综合核架构名都是 `rtl`。
4. **预期结果**：两套命名高度一致；若发现个别例外，记录下来作为「待本地验证」的疑点（少数核可能因历史原因或厂商专用实现而不同）。

#### 4.2.5 小练习与答案

**练习 1**：为什么可综合核用 `rtl` 架构、测试台用 `tb` 架构，而不是都用默认架构名？

> **参考答案**：固定架构名让工具链和人类读者一眼就能分辨「这段代码会不会被综合」。综合流程可以只挑 `rtl` 架构处理；仿真流程可以只挑 `tb` 架构当顶层。这是一种「用名字编码语义」的工程习惯。

**练习 2**：一个测试台实体 `arith_addw_tb` 有没有 `port`？为什么？

> **参考答案**：通常没有。测试台是仿真顶层，激励（时钟、复位、输入数据）都在架构内部用信号生成并连接到被测核（DUT），不需要对外暴露物理引脚，所以 `entity arith_addw_tb is end entity;` 没有端口列表。

---

### 4.3 文档头与代码风格

#### 4.3.1 概念说明

命名解决了「文件叫什么」，这一节解决「文件内部长什么样」。PoC 对每个源码文件都有两个硬性要求：

1. **文件头**由两部分组成：
   - **编辑器配置行**：告诉常见编辑器（Emacs/Vim/Kate）本文件用 Tab 缩进、Tab 宽度为 2，避免不同贡献者用不同缩进把 diff 搞乱。
   - **文档头**：用注释块写明 `Authors` / `Entity`（或 `Testbench`/`Package`）/ `Description` / `SeeAlso` 等信息，并附上 Apache 2.0 许可证声明。

2. **代码风格**：大小写约定、信号初始化、表达式括号、实例化方式都有明确规则。

此外，PoC 还有一套 Sphinx 文档生成流程，每个核会根据 [`docs/Entity.template`](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/docs/Entity.template) 自动生成一个文档页，把 entity 声明片段和文档头描述渲染成 HTML。所以文档头不仅是给人看的，也是给工具消费的——格式不能乱。

#### 4.3.2 核心流程

一个标准 PoC 源码文件的结构（自上而下）：

```text
┌─────────────────────────────────────────────┐
│ 1. 编辑器配置行（3 行，Emacs/Vim/Kate）        │
├─────────────────────────────────────────────┤
│ 2. 文档头                                      │
│    -- ====...====   （上分隔线，≥16 个 =）     │
│    Authors:    ...                            │
│    Entity:     ...   （或 Testbench / Package）│
│    Description: ...                           │
│    License:                                              │
│    -- ====...====   （许可证块开始分隔线）       │
│    <Apache 2.0 许可证正文>                     │
│    -- ====...====   （下分隔线）                │
├─────────────────────────────────────────────┤
│ 3. library / use 子句                         │
├─────────────────────────────────────────────┤
│ 4. entity <name> is ... end entity;          │
├─────────────────────────────────────────────┤
│ 5. architecture rtl of <name> is ...         │
└─────────────────────────────────────────────┘
```

#### 4.3.3 源码精读

**编辑器配置行**——规范见 [`vhdl_coding.md:21-28`](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/vhdl_coding.md#L21-L28)，真实代码见 [`src/arith/arith_addw.vhdl:1-3`](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith_addw.vhdl#L1-L3)：

```vhdl
-- EMACS settings: -*-  tab-width: 2; indent-tabs-mode: t -*-
-- vim: tabstop=2:shiftwidth=2:noexpandtab
-- kate: tab-width 2; replace-tabs off; indent-width 2;
```

关键点：`indent-tabs-mode: t` 与 `noexpandtab` 表示**用真正的 Tab 字符缩进，而不是用空格**。这是 PoC 与很多项目相反的地方——它要求用 Tab。配合 [`vhdl_coding.md:53-56`](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/vhdl_coding.md#L53-L56) 的 Whitespace 规则：每级缩进一个 Tab、Tab 宽度按 2 个空格显示、消除所有行尾空白。

**文档头与分隔线**——规范要求分隔线匹配正则 `/^--\s*={16,}$/`（即 `--` 后跟至少 16 个 `=`）。真实范例见 [`src/arith/arith_addw.vhdl:4-44`](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith_addw.vhdl#L4-L44)，它有标准的 `Authors` / `Entity` / `Description` / `References` / `License` 段，并用三行 `-- ====...` 分隔：

```vhdl
-- =============================================================================
-- Authors:          Thomas B. Preusser
--
-- Entity:           arith_addw
--
-- Description:
-- -------------------------------------
-- Implements wide addition providing several options ...
--
-- License:
-- =============================================================================
-- Copyright 2007-2015 Technische Universitaet Dresden - Germany
-- ... (Apache License 2.0 正文)
-- =============================================================================
```

文档头的官方模板要求见 [`vhdl_coding.md:29-51`](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/vhdl_coding.md#L29-L51)。

**大小写约定**——规范见 [`vhdl_coding.md:60-66`](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/vhdl_coding.md#L60-L66)：

| 类别 | 大小写 | 示例 |
| --- | --- | --- |
| VHDL 关键字 | 全小写 | `process`、`case`、`if` |
| 标准类型 | 全小写 | `integer`、`std_logic_vector` |
| 常量 / generic 参数 | 全大写 | `N`、`K`、`ARCH`、`BLOCKING` |
| 信号 / 变量 / 标号 | 蛇形或驼峰均可，需一致 | `din`、`CLOCK_FREQ` |

在 [`src/arith/arith_addw.vhdl:55-70`](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith_addw.vhdl#L55-L70) 可以清楚看到这套约定：generic 全大写（`N`、`K`、`ARCH`、`BLOCKING`、`SKIPPING`、`P_INCLUSIVE`），port 全小写（`a`、`b`、`cin`、`s`、`cout`），关键字 `generic`/`port`/`in`/`out` 全小写。

**信号初始化**——规范见 [`vhdl_coding.md:68-73`](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/vhdl_coding.md#L68-L73)：所有表示时序逻辑（有状态）的信号都要给初值；若初值无所谓，用 don't-care，例如 `(others => '-')`；组合逻辑信号**绝不**手写初值。

**表达式与实例化**——规范见 [`vhdl_coding.md:75-87`](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/vhdl_coding.md#L75-L87)：运算符优先级不平凡时要加括号；不要在 `if/while` 条件外多套括号；布尔量直接求值（写 `B` 而非 `B = true`）；实例化时**不要用位置绑定**（必须用 `port map(name => sig)` 命名绑定）；如果命名空间包里已有 `component` 声明，优先实例化 component。

#### 4.3.4 代码实践

1. **实践目标**：学会用规范去「审阅」一段真实代码。
2. **操作步骤**：
   - 打开 [`docs/Entity.template`](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/docs/Entity.template)，理解它用 `{EntityName}`、`{EntityDescription}`、`{EntityDeclarationFromTo}` 等占位符，把每个核的文档头描述和 entity 声明片段渲染成一个文档页。这说明文档头格式必须稳定，否则模板渲染会出错。
   - 回到 [`src/arith/arith_addw.vhdl`](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith_addw.vhdl)，逐条核对：编辑器配置是否齐全？文档头分隔线是否符合 `={16,}`？generic 是否全大写、port 是否全小写？架构名是否为 `rtl`？
3. **需要观察的现象**：每一项是否都符合规范；如果用编辑器打开，缩进是否显示为 Tab 字符（而非空格）。
4. **预期结果**：`arith_addw.vhdl` 在以上所有条目上都合规，可作为你编写新核的「黄金样板」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 PoC 要求文档头的分隔线必须匹配 `/^--\s*={16,}$/` 这种严格格式？

> **参考答案**：因为文档头会被脚本和 Sphinx 文档生成流程（如 `docs/Entity.template`）自动解析。分隔线是「段落边界」的标记，格式一乱，工具就分不清 `Authors`/`Description`/`License` 各段从哪里到哪里，渲染出的文档页就会错乱。严格的正则保证机器可解析。

**练习 2**：规范说「组合逻辑信号不要手写初值，时序逻辑信号要给初值（无关时用 `-`）」，为什么这么区分？

> **参考答案**：组合逻辑由敏感表驱动，写出初值反而可能掩盖「 latch 误生成」等错误，且综合后初值通常无效。时序逻辑（寄存器）则不同：FPGA 上电时的初始状态会影响功能，显式给出初值（或复位值）能让仿真和上电行为一致；用 don't-care `(others => '-')` 则告诉综合工具「这一位随便」，便于工具优化。

**练习 3**：实例化时为什么禁止「位置绑定」（positional binding）？

> **参考答案**：位置绑定（如 `u1: comp port map (a, b, c);`）完全靠顺序对应端口，一旦被测 component 的端口顺序改动，实例化就会静默错配而难以发现。命名绑定（`port map (clk => clk, din => din, ...)`）显式写明对应关系，可读性强、抗改动，所以规范强制要求命名绑定。

## 5. 综合实践

把本讲三个模块串起来，完成下面这个贯通任务：**为虚拟核 `mylib_echo` 写出一份符合 PoC 规范的最小源码骨架。**

任务要求：

1. 命名与文件：核属于新命名空间 `mylib`，实体叫 `mylib_echo`，文件应命名为 `mylib_echo.vhdl`（放在 `src/mylib/` 下，本练习只是写代码，不必真去建目录改源码树）。
2. 架构：可综合实现，架构名用 `rtl`。
3. 文档头与风格：包含编辑器配置 3 行 + 标准文档头（带符合 `={16,}` 的分隔线 + Apache 2.0 许可证块）；generic 全大写、port 全小写；用 Tab 缩进；时序信号给 don't-care 初值。

下面是一份**示例代码**（不是项目原有代码，仅作示范），你可以对照检查每一处是否符合 4.1～4.3 的规范：

```vhdl
-- EMACS settings: -*-  tab-width: 2; indent-tabs-mode: t -*-
-- vim: tabstop=2:shiftwidth=2:noexpandtab
-- kate: tab-width 2; replace-tabs off; indent-width 2;
-- =============================================================================
-- Authors:          <你的名字>
--
-- Entity:           mylib_echo
--
-- Description:
-- -------------------------------------
--   示例代码：把输入 din 寄存一拍后输出到 dout 的最简回显核。
--
-- License:
-- =============================================================================
-- Copyright <年份> <你的单位>
--
-- Licensed under the Apache License, Version 2.0 (the "License");
-- you may not use this file except in compliance with the License.
-- You may obtain a copy of the License at
--
--              http://www.apache.org/licenses/LICENSE-2.0
--
-- Unless required by applicable law or agreed to in writing, software
-- distributed under the License is distributed on an "AS IS" BASIS,
-- WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
-- See the License for the specific language governing permissions and
-- limitations under the License.
-- =============================================================================

library	IEEE;
use			IEEE.std_logic_1164.all;

library	PoC;
use			PoC.utils.all;


entity mylib_echo is
  generic (
    N : positive := 8                       -- 数据位宽（generic 全大写）
  );
  port (
    clk  : in  std_logic;                   -- 端口名全小写
    rst  : in  std_logic;
    din  : in  std_logic_vector(N-1 downto 0);
    dout : out std_logic_vector(N-1 downto 0)
  );
end entity;


architecture rtl of mylib_echo is          -- 可综合实现 => 架构名 rtl
  signal reg : std_logic_vector(N-1 downto 0) := (others => '-');  -- 时序信号给 don't-care 初值
begin

  process(clk)
  begin
    if rising_edge(clk) then
      if rst = '1' then                     -- 布尔量直接求值，不写 rst = '1' 之外的冗余比较
        reg <= (others => '0');             -- 复位值与初值语义保持一致
      else
        reg <= din;
      end if;
    end if;
  end process;

  dout <= reg;

end architecture;
```

操作步骤与自检清单：

1. 把上面这段示例代码存成 `mylib_echo.vhdl`。
2. 逐条核对：编辑器配置 3 行是否齐全？三行分隔线 `-- ====...` 是否都不少于 16 个 `=`？generic `N` 是否大写、port 是否小写？架构名是否 `rtl`？`reg` 是否有 don't-care 初值？
3. 用编辑器打开，确认缩进是 Tab 字符（可开启「显示空白字符」）。
4. 进阶（可选）：仿照 [`tb/arith/arith_addw_tb.vhdl`](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/tb/arith/arith_addw_tb.vhdl) 写一个 `mylib_echo_tb`，实体名加 `_tb`、架构名用 `tb`、文档头标签写 `Testbench:`，并例化 `mylib_echo` 作为 DUT。

> 说明：本实践为「源码阅读 + 模仿编写」型任务，不要求你真的去运行综合或仿真。如果要把 `mylib_echo` 真正接入 PoC 的编译流程，还需要更新 `mylib.pkg.vhdl` 组件声明与 `.files` 清单，这部分留到第 5 单元「扩展 PoC」讲义展开。

## 6. 本讲小结

- PoC 用 `<namespace>_<entity>` 的蛇形命名 + 单实体单文件 + `.vhdl` 后缀，让文件名自带「地址」，看到一个名字就能定位源码、测试台和命名空间包。
- 可综合实现统一用 `architecture rtl`，测试台统一用 `<entity>_tb` 实体 + `architecture tb`，二者干净分离。
- 每个文件必须有编辑器配置行（强制用 Tab、Tab 宽 2）和带分隔线（匹配 `/^--\s*={16,}$/`）的文档头 + Apache 2.0 许可证块。
- 大小写约定：关键字和标准类型全小写，常量/generic 全大写，信号/变量名保持一致即可。
- 时序信号必须给初值（无关时用 don't-care `(others => '-')`），组合信号不给初值；实例化必须用命名绑定，禁止位置绑定。
- 文档头格式之所以严格，是因为它会被 `docs/Entity.template` 等脚本和 Sphinx 流程自动解析渲染。

## 7. 下一步学习建议

本讲只讲了「代码长什么样」，还没讲「代码依赖什么」。建议下一步：

- 进入 **u2-l1 公共包总览与 Common 上下文**：学习 `src/common/` 下 `utils`、`config`、`physical` 等公共包的职责，以及 `context Common` 如何一次性引用它们。你会开始理解本讲示例里 `use PoC.utils.all;` 这一行到底引入了什么。
- 如果你急于看一个「带厂商可移植选择」的真实核结构，可以先跳读 **u3-l2 厂商选择与可移植机制**，但建议先把第 2 单元的公共包基础打好。
- 想了解如何把一个新核（如本讲的 `mylib_echo`）真正接入 PoC 的编译与文档流程（更新 `.pkg.vhdl`、`.files`、文档模板），请等到 **u5-l6 扩展 PoC**。
