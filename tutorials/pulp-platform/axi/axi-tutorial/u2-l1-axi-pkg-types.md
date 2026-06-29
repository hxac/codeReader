# axi_pkg：类型与常量

## 1. 本讲目标

[u1-l3 AXI4 协议快速回顾](u1-l3-axi-protocol-primer.md) 已经带你扫过 `axi_pkg.sv` 里 `BURST_*`、`RESP_*` 的取值，并用 `num_bytes`/`beat_addr` 算过地址。本讲要把这本书的「目录」彻底读透——它的标题、它的页码体系、它每一类条目都从哪里来。读完本讲，你应该能够：

- 说出 `axi_pkg` 这个 `package` 里**每一类**公共内容（宽度常量、typedef、localparam）的来历，并能解释为什么它们要被集中放在一个 `package` 里；
- 对着源码把**每个 typedef 与它对应的位宽常量**一一对应起来（这是本讲的核心，也是本讲的代码实践任务）；
- 自己写一小段 SystemVerilog，用 `axi_pkg::resp_t`、`axi_pkg::RESP_DECERR` 这类带作用域的名字声明并赋值信号。

本讲**不**重复 u1-l3 已经讲透的 `BURST_*`/`RESP_*` 取值含义和地址计算函数；那些只作为「已知结论」被引用。本讲的重点是**类型系统的骨架**：宽度常量如何支撑 typedef，typedef 如何被全库复用，以及 `CACHE_*`、`ATOP_*` 这两族 u1-l3 没展开的 localparam。辅助函数（`num_bytes`、地址对齐计算）和配置结构体（`xbar_cfg_t`、`xbar_latency_e`）留给下一讲 [u2-l2](u2-l2-axi-pkg-funcs-cfg.md)。

## 2. 前置知识

- 你已经读过 [u1-l3](u1-l3-axi-protocol-primer.md)，知道 `BURST_FIXED/INCR/WRAP`、`RESP_OKAY/EXOKAY/SLVERR/DECERR` 的取值，知道 `len_t`/`size_t`/`burst_t`/`resp_t` 这些名字的大致用途。
- 你已经读过 [u1-l2 仓库结构与构建系统](u1-l2-repo-and-build.md)，知道 `axi_pkg.sv` 是全库唯一的 `package`、位于 **Level 0**（编译层级最底层、被几乎所有模块 `import`）。本讲会反复用到这个事实。
- 你看得懂 SystemVerilog 的 `package`/`endpackage`、`parameter`、`typedef`、`localparam`、`import`、`::`（作用域解析）这几个基本语法。不要求会写，能读懂即可。

几个词先对齐：

- **宽度常量（width parameter）**：形如 `parameter int unsigned RespWidth = 32'd2;` 的命名常量，只存「这个字段几比特宽」这一个数字。
- **类型别名（typedef）**：形如 `typedef logic [1:0] resp_t;`，给「2 位 logic」起一个有意义的名字 `resp_t`。
- **localparam**：模块/包内部不可外部覆盖的常量，本库里用来定义协议取值（如 `RESP_DECERR = 2'b11`）。
- **作用域（scope）**：`axi_pkg::resp_t` 表示「`axi_pkg` 这个包里的 `resp_t`」，`::` 是 SystemVerilog 的作用域解析运算符。

## 3. 本讲源码地图

本讲几乎只围绕一个文件展开，但它是全库的「类型根基」：

| 文件 | 作用 |
|------|------|
| [src/axi_pkg.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv) | 唯一的 `package axi_pkg`：宽度常量（L24–L45）、typedef（L47–L66 等）、`BURST_*`/`RESP_*`/`CACHE_*`/`ATOP_*` localparam、各类函数与配置结构体。本讲精读它的「类型/常量」部分。 |

为了证明这些类型确实被全库使用，本讲还会顺手引用几个「消费者」文件作为活样本：

| 文件 | 用到的 `axi_pkg::` 内容 |
|------|-------------------------|
| [src/axi_test.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_test.sv) | `import axi_pkg::*;` 通配导入整个包（L23）。 |
| [src/axi_lite_to_apb.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_to_apb.sv) | 用 `axi_pkg::resp_t` 声明响应信号（L91 等）。 |
| [src/axi_lite_xbar.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_xbar.sv) | 例化错误从端时传 `axi_pkg::RESP_DECERR`（L191）。 |
| [src/axi_burst_splitter_gran.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_burst_splitter_gran.sv) | 函数形参用 `axi_pkg::atop_t`/`burst_t`/`cache_t`（L105）。 |

> 一句话定位：`axi_pkg.sv` 是全库的「词典」。你在任何模块里看到的 `resp_t`、`BURST_INCR`、`RESP_DECERR`、`atop_t`……全都出生在这里。

---

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

- 4.1 `package axi_pkg`：全库的类型根基与组织结构
- 4.2 宽度常量 parameter ↔ typedef 的一一对应（核心模块）
- 4.3 localparam 全景一：`CACHE_*` 位标志与 `mem_type_t` 枚举
- 4.4 localparam 全景二：`ATOP_*` 与 `atop_t`（预告 u15）

### 4.1 `package axi_pkg`：全库的类型根基与组织结构

#### 4.1.1 概念说明

SystemVerilog 的 **package** 是一个命名空间，用来集中存放「多个模块都要共享」的声明——类型、常量、函数。一个 `package` 在整个编译里**只编译一次**，任何模块只要 `import` 它，就能用里面的名字。你可以把它理解成 C 的头文件 + 一份只读的全局词典。

`axi_pkg` 之所以是全库的根基，有两层原因：

1. **它没有依赖、却被所有人依赖**。正如 [u1-l2](u1-l2-repo-and-build.md) 所讲，本库按「被 `import` 的 `package` 必须先编译」做拓扑分层。`axi_pkg` 内部不引用任何其它文件，却被几乎所有 RTL 模块 `import`，所以它必然落在 **Level 0**——依赖图的最底层、第一个被编译。
2. **它定义了全库共用的「词汇」**。`BURST_INCR`、`RESP_DECERR`、`resp_t`、`atop_t`……这些名字若分散在各模块里各自定义，必然打架或漂移；集中在一个包里，就保证了「同一个概念在全库只有一个写法」。这正是「单一事实来源（single source of truth）」在硬件里的体现，也呼应了 [u1-l1](u1-l1-project-overview.md) 讲的「组合优于配置」哲学——类型契约统一了，模块才能可靠地背靠背拼接。

#### 4.1.2 核心流程

`axi_pkg` 内部按内容大致分五类，本讲覆盖前三类，后两类留给后续讲义：

| 类别 | 形式 | 行号区间 | 本讲/后续 |
|------|------|----------|-----------|
| 宽度常量 | `parameter int unsigned XxxWidth` | L24–L45 | **本讲 4.2** |
| 类型别名 | `typedef logic [...] xxx_t` | L47–L66（另见 L123、L229） | **本讲 4.2 / 4.3 / 4.4** |
| 协议取值 | `localparam`（`BURST/RESP/CACHE/ATOP`） | L68–L113、L380–L447 | **本讲 4.3 / 4.4**（`BURST/RESP` 见 u1-l3） |
| 函数 | `function automatic ...` | L116–L378 | u2-l2 |
| 配置结构体 | `typedef struct/enum`（`xbar_*`） | L449–L536 | u2-l2 |

使用这个包有两种写法，都在本库真实出现：

- **通配导入**：`import axi_pkg::*;`，之后直接写 `resp_t`、`RESP_DECERR`，不加前缀。适合一次性用很多名字的文件（如 testbench）。
- **作用域引用**：`axi_pkg::resp_t`、`axi_pkg::RESP_DECERR`，每次都带包名前缀。适合只想用一两个名字、或想避免命名污染的模块。

两种写法在语义上等价，编译器解析到的是同一个声明。

#### 4.1.3 源码精读

包的开头与结尾：

[src/axi_pkg.sv:21-23](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L21-L23) —— `package axi_pkg;` 的声明，上方注释一句话点明它的职责：「Contains all necessary type definitions, constants, and generally useful functions」（包含所有必要的类型定义、常量与常用函数）。这一句就是本讲的纲领。

[src/axi_pkg.sv:543](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L543) —— `endpackage`。从 L23 到 L543 之间的所有声明都属于这个包。

两种使用写法的真实样本：

[src/axi_test.sv:23](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_test.sv#L23) —— `import axi_pkg::*;`，通配导入。`axi_test` 是验证组件，里面大量使用包里的类型与常量，所以干脆全导入。

[src/axi_lite_to_apb.sv:91](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_to_apb.sv#L91) —— `axi_pkg::resp_t resp;`，作用域引用。这个模块只在一两处需要响应类型，于是带前缀写，不污染本模块的名字空间。

[src/axi_lite_xbar.sv:191](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_xbar.sv#L191) —— 例化错误从端时把 `axi_pkg::RESP_DECERR` 作为参数传进去。这是「常量也被当作值来用」的典型场景。

#### 4.1.4 代码实践

**实践目标**：确认「全库的类型/常量都从 `axi_pkg` 来」，并区分两种导入写法。

**操作步骤**（源码阅读型）：

1. 打开 [src/axi_pkg.sv:21-23](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L21-L23)，记住这个包的边界（L23 到 L543）。
2. 在 `src/` 下任选一个模块（例如 `axi_lite_to_apb.sv`），搜索 `axi_pkg::`，数一数它引用了包里的几个名字。
3. 再看 `src/axi_test.sv:23` 的 `import axi_pkg::*;`，对比两种写法的差异。

**需要观察的现象**：作用域引用（`axi_pkg::xxx`）每次都带前缀、用几个写几个；通配导入（`import axi_pkg::*`）一次导入、之后裸用名字。

**预期结果**：你会清楚地看到——无论哪种写法，这些类型和常量的「出生地」都是 `axi_pkg`。这印证了它作为「单一事实来源」的角色。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `axi_pkg` 必须是 Level 0，而不能是更高的层级？

> **参考答案**：因为 SystemVerilog 要求被 `import` 的 `package` 先于使用它的模块编译。`axi_pkg` 被几乎所有模块 `import`，所以它必须排在这些模块之前编译；而它自己又不依赖任何其它文件，于是自然落在最底层 Level 0。依据见 [u1-l2](u1-l2-repo-and-build.md) 对编译层级的定义。

**练习 2**：`import axi_pkg::*;` 和 `axi_pkg::resp_t` 这两种写法，在最终编译结果上有区别吗？

> **参考答案**：没有语义区别，二者都解析到 `axi_pkg` 里同一个 `typedef logic [1:0] resp_t`。区别只在书写风格与名字空间：通配导入省前缀但会把包里所有名字引入当前作用域；作用域引用显式、不污染名字空间。本库两种都用，按文件需要选择。

---

### 4.2 宽度常量 parameter ↔ typedef 的一一对应

#### 4.2.1 概念说明

这是本讲的核心，也是本讲代码实践任务直接对应的内容。打开 `axi_pkg` 的前几十行，你会发现一个高度规整的**成对模式**：先有一个**宽度常量**说「这个字段几比特」，紧跟一个**类型别名**用这个宽度声明一个有意义的类型。以响应字段为例：

```
parameter int unsigned RespWidth = 32'd2;   // 宽度常量：响应字段 2 比特
...
typedef logic [1:0] resp_t;                  // 类型别名：2 位 logic 叫 resp_t
```

这种「宽度常量 + 类型别名」成对出现的设计有三个好处：

1. **自文档化**。看到 `resp_t` 就知道是「响应」，不用去猜 `logic [1:0]` 到底是什么；看到 `RespWidth=2` 就知道响应字段固定 2 比特，这是 AXI 协议写死的。
2. **单一事实来源**。某天若协议演进、响应宽度要变，只需改 `RespWidth` 一处（虽然 AXI 里这些宽度其实是协议钉死的，但机制在那里）。
3. **宽度可被复用计算**。这些宽度常量不只是给人看的注释——它们被「通道宽度函数」（如 `aw_width`、`w_width`）相加求和，用来算每个 AXI 通道的总位宽。也就是说，类型系统的宽度同时也是位宽计算的输入。

注意一个**反例（gotcha）**：并非每个宽度常量都有对应的 typedef。最典型的是 `LockWidth=1`——锁字段只有 1 比特，本库没有给它起 `lock_t`，而是直接用 `logic` 表达。这是个有用的「陷阱」，小练习会考到。

#### 4.2.2 核心流程

把宽度常量与 typedef 拉成一张对照表（按源码顺序）：

| 宽度常量（parameter） | 值 | typedef | 含义 |
|------------------------|----|--------|------|
| `BurstWidth` | 2 | `burst_t = logic[1:0]` | 突发类型（FIXED/INCR/WRAP） |
| `RespWidth` | 2 | `resp_t = logic[1:0]` | 响应码（OKAY/EXOKAY/SLVERR/DECERR） |
| `CacheWidth` | 4 | `cache_t = logic[3:0]` | 内存属性位标志（4 个独立 bit） |
| `ProtWidth` | 3 | `prot_t = logic[2:0]` | 保护属性（特权/安全/指令） |
| `QosWidth` | 4 | `qos_t = logic[3:0]` | 服务质量（QoS） |
| `RegionWidth` | 4 | `region_t = logic[3:0]` | 区域标识 |
| `LenWidth` | 8 | `len_t = logic[7:0]` | 突发长度（拍数−1，最多 256 拍） |
| `SizeWidth` | 3 | `size_t = logic[2:0]` | 每拍字节数的 log2 |
| `LockWidth` | 1 | **（无 typedef）** | 锁字段，直接用 1 位 logic |
| `AtopWidth` | 6 | `atop_t = logic[5:0]` | AXI5 原子操作编码（见 4.4） |
| `NsaidWidth` | 4 | `nsaid_t = logic[3:0]` | AXI5 非安全地址标识符 |

这些宽度常量不只是「标签」。它们被通道宽度函数直接相加使用，例如 `aw_width` 把 `LenWidth + SizeWidth + BurstWidth + LockWidth + CacheWidth + ProtWidth + QosWidth + RegionWidth + AtopWidth` 全部加起来得到 AW 通道负载总宽。换句话说，**typedef 是给人读的语义层，宽度常量是给综合器/计算用的数值层，二者通过同一张表保持一致**。

#### 4.2.3 源码精读

宽度常量定义：

[src/axi_pkg.sv:24-45](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L24-L45) —— 11 个 `parameter int unsigned XxxWidth`，每个上方都有一行 `///` 注释说明含义。注意它们都写成 `32'dN`（32 位无符号字面量），这是本库一致的编码风格。

类型别名定义：

[src/axi_pkg.sv:47-66](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L47-L66) —— 与上面对应的 10 个 typedef（`lock` 例外，没有 typedef）。例如 L48 `typedef logic [1:0] burst_t;`、L50 `typedef logic [1:0] resp_t;`、L64 `typedef logic [5:0] atop_t;`。注意：typedef 里写的是**字面位宽**（如 `[1:0]`、`[5:0]`），并没有写成 `[RespWidth-1:0]` 这种用常量表达式驱动宽度的形式——这是本库有意识的选择，让类型定义直观可读；宽度常量则另起一行做「数值层」的单一事实来源。

宽度常量被「消费」的证据——通道宽度函数：

[src/axi_pkg.sv:321-334](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L321-L334) —— `aw_width` 与 `w_width`。看 `aw_width` 的返回表达式，它把 `LenWidth + SizeWidth + BurstWidth + LockWidth + CacheWidth + ProtWidth + QosWidth + RegionWidth + AtopWidth` 以及 `id_width + addr_width + user_width` 全部相加。这就是「宽度常量不只是注释」的铁证——它们直接参与位宽求和。（这些函数本身的用途属于 u2-l2 的「辅助函数」主题，这里只借用它们证明宽度常量被复用。）

类型被「消费」的证据——真实模块：

[src/axi_burst_splitter_gran.sv:105](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_burst_splitter_gran.sv#L105) —— 一个函数的形参同时用了 `axi_pkg::atop_t`、`axi_pkg::burst_t`、`axi_pkg::cache_t` 三个类型，说明这些 typedef 确实在全库被当作「类型」来声明端口与变量。

#### 4.2.4 代码实践

**实践目标**：本讲的主实践任务——把每个 typedef 对应的宽度常量列出来，并亲手用 `axi_pkg::resp_t` 与 `RESP_DECERR` 写一小段 SystemVerilog。

**操作步骤**（源码阅读 + 手写片段）：

1. 打开 [src/axi_pkg.sv:24-66](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L24-L66)。
2. 对照上面的表格，逐行确认每个 typedef 的位宽来自哪个宽度常量（如 `resp_t` ↔ `RespWidth=2`）。**特别留意**：`LockWidth=1` 没有对应的 typedef。
3. 手写下面这段最小 SystemVerilog（示例代码，不是仓库原有文件）：

```systemverilog
// 示例代码：演示如何引用 axi_pkg 的类型与常量
module demo_pkg_types;
  // 用包里的类型别名声明信号（作用域引用写法）
  axi_pkg::resp_t   resp_q;     // 2 位，等价于 logic [1:0]
  // 用包里的常量给信号赋值
  initial begin
    resp_q = axi_pkg::RESP_DECERR;   // 即 2'b11
    $display("resp_q = %b (expect 11)", resp_q);
  end
endmodule
```

4. 若你想确认这段片段能编译，可以把这段代码放进一个最小 testbench，用 u1-l4 讲过的仿真流程跑一下；但因为它依赖 `axi_pkg` 先编译（Level 0），需要把它和 `axi_pkg.sv` 一起喂给仿真器。是否实际运行标注「待本地验证」——即便不跑，对照源码也能确认 `resp_q` 是 2 位、`RESP_DECERR=2'b11`（[src/axi_pkg.sv:99-100](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L99-L100)）。

**需要观察的现象**：`axi_pkg::resp_t` 声明出的信号是 2 位；赋值 `axi_pkg::RESP_DECERR` 后它的值为 `2'b11`。

**预期结果**：打印 `resp_q = 11 (expect 11)`。这一步同时验证了两件事——typedef `resp_t` 的宽度确实是 2，常量 `RESP_DECERR` 的值确实是 `2'b11`，二者都来自 `axi_pkg`。

#### 4.2.5 小练习与答案

**练习 1**：`LockWidth=1`，但全包搜不到 `lock_t`。这是疏漏还是有意为之？

> **参考答案**：是有意为之。锁字段只有 1 比特，单独起一个 `lock_t` 类型收益不大，本库直接用 `logic` 表达。这说明「宽度常量」与「typedef」虽然通常是成对的，但并不是强制的——宽度常量更基本的角色是「为位宽计算提供数值」（`aw_width` 里就用了 `LockWidth`），typedef 是可选的语义增强。

**练习 2**：为什么 typedef 写成 `typedef logic [1:0] resp_t;`，而不是 `typedef logic [RespWidth-1:0] resp_t;`？

> **参考答案**：本库选择了「字面位宽 + 平行的宽度常量」两条独立线索，而不是用常量表达式驱动宽度。好处是 typedef 一眼可读（`[1:0]` 直接告诉你 2 位），宽度常量则单独服务位宽计算（如 `aw_width`）。代价是两条线索需要人工保持一致——但因为 AXI 这些宽度都是协议钉死的，实际上不会变，所以这个代价可忽略。

---

### 4.3 localparam 全景一：`CACHE_*` 位标志与 `mem_type_t` 枚举

#### 4.3.1 概念说明

[u1-l3](u1-l3-axi-protocol-primer.md) 已经讲过 `BURST_*` 和 `RESP_*`，这里不再重复它们的含义，只把它们作为「`axi_pkg` 里 localparam 的一类」锚定一下位置（取值见 [src/axi_pkg.sv:68-100](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L68-L100)）。本节要展开的是 u1-l3 没碰过的另一族：**`CACHE_*` 内存属性位标志**。

关键直觉：`cache_t`（4 位）**不是枚举值，而是 4 个独立标志位的按位或（bitmask）**。这和 `BURST_*`（3 选 1 的枚举）截然不同。4 个标志位各自表达一个「是否」的语义，可以任意组合：

| localparam | 值 | 语义 |
|------------|----|------|
| `CACHE_BUFFERABLE` | `4'b0001` | 可被中间组件缓冲/延迟 |
| `CACHE_MODIFIABLE` | `4'b0010` | 事务特征可被修改（可改性） |
| `CACHE_RD_ALLOC` | `4'b0100` | 建议读分配（非强制） |
| `CACHE_WR_ALLOC` | `4'b1000` | 建议写分配（非强制） |

因为它们是位标志，所以可以写 `cache = CACHE_BUFFERABLE | CACHE_MODIFIABLE` 来组合。本库还提供了两个判断函数 `bufferable(cache)` 和 `modifiable(cache)`，用来检测某个 cache 值里是否置了对应位。

4 位理论上有 16 种组合，手写 `4'b0011` 这种魔法数字既难读又易错。于是 `axi_pkg` 又提供了一个枚举类型 `mem_type_t`，把 12 种**有意义的常见组合**起成名字（如 `WTHRU_RWALLOCATE` = 写穿透 + 读写分配），再提供 `get_arcache`/`get_awcache` 两个函数把枚举名翻译回具体的 4 位 cache 值。

#### 4.3.2 核心流程

从「枚举名」到「cache 位」的翻译流程：

```
mem_type_t 枚举值（语义名，如 WTHRU_RALLOCATE）
        │
        ├── get_arcache(...)  → 4 位 AR_CACHE（读通道用的 cache 值）
        └── get_awcache(...)  → 4 位 AW_CACHE（写通道用的 cache 值）
```

这里有个**容易忽略的细节**：读通道（AR）和写通道（AW）的 cache 编码**不完全相同**。同一种内存类型，`get_arcache` 和 `get_awcache` 可能给出不同的 4 位值。比如 `WTHRU_RALLOCATE`：`get_arcache` 返回 `4'b1110`，`get_awcache` 返回 `4'b0110`——二者只在 bit[3]（写分配位）上有别。这正是需要两个函数、而不是一个的原因（这个差异乍看反直觉，详见 4.3.4 的解释）。

随后，下游模块拿到 4 位 cache 值后，可以用 `bufferable()`/`modifiable()` 把单个标志位抠出来做判断。

#### 4.3.3 源码精读

4 个 cache 位标志：

[src/axi_pkg.sv:102-113](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L102-L113) —— `CACHE_BUFFERABLE`/`CACHE_MODIFIABLE`/`CACHE_RD_ALLOC`/`CACHE_WR_ALLOC`，每个都是单独一位，可按位或组合。

判断函数：

[src/axi_pkg.sv:218-226](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L218-L226) —— `bufferable(cache)` 和 `modifiable(cache)`，用 `|(cache & CACHE_xxx)` 检测某一位是否置位。这是「位标志」被消费的最基本方式。

`mem_type_t` 枚举：

[src/axi_pkg.sv:228-242](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L228-L242) —— 12 个枚举值，从 `DEVICE_NONBUFFERABLE` 到 `WBACK_RWALLOCATE`，覆盖 device/normal、写穿透/写回、是否分配的常见组合。

枚举 → cache 值的翻译函数：

[src/axi_pkg.sv:244-261](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L244-L261) —— `get_arcache(mtype)`，把 `mem_type_t` 翻译成 AR 通道的 4 位 cache 值。

[src/axi_pkg.sv:263-280](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L263-L280) —— `get_awcache(mtype)`，翻译成 AW 通道的 4 位 cache 值。对比两个函数对 `WTHRU_RALLOCATE`（L252 vs L271）的返回值，就能看到读/写分配位的差异。

> 这一节把 typedef（`cache_t`、`mem_type_t`）、localparam（`CACHE_*`）和函数（`bufferable`/`get_arcache`/`get_awcache`）串成了一个完整的「内存属性」小系统。这三位一体正是 `axi_pkg` 组织内容的典型范式：类型给语义、常量给取值、函数给计算。更一般的「辅助函数」话题（地址计算等）见 u2-l2。

#### 4.3.4 代码实践

**实践目标**：亲眼看清「同一种内存类型，AR 与 AW 的 cache 值可能不同」。

**操作步骤**（源码阅读型）：

1. 同时打开 [src/axi_pkg.sv:244-261](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L244-L261)（`get_arcache`）和 [src/axi_pkg.sv:263-280](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L263-L280)（`get_awcache`）。
2. 找到 `WTHRU_RALLOCATE` 这一行：`get_arcache` 返回 `4'b1110`（L252），`get_awcache` 返回 `4'b0110`（L271）。
3. 用 4.3.1 的位标志表拆解这两个值，看哪一位不同、为什么。

**需要观察的现象**：两个返回值只在最高位 bit[3]（即 `CACHE_WR_ALLOC`，`4'b1000`）上不同——AR 侧 `get_arcache` 返回 `4'b1110`（bit[3]=1），AW 侧 `get_awcache` 返回 `4'b0110`（bit[3]=0）；其余三位（含读分配位 bit[2]）两边相同。

**预期结果**：你会发现这乍看有点反直觉——「写分配位」居然在读通道 AR 上是 1、在写通道 AW 上是 0。但这并非笔误，而是 AMBA 规范里 AWCACHE/ARCACHE 两张编码表本身就是**不对称**的：同一种内存类型在读写两个方向上、按「是否建议分配」给出不同的 4 位编码，本库只是忠实翻译了协议表。正因为两侧编码会不同，才必须提供 `get_arcache` 与 `get_awcache` 两个函数，而不能用一个函数敷衍两个通道。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `CACHE_*` 设计成「位标志」而不是像 `BURST_*` 那样的「互斥枚举值」？

> **参考答案**：因为 4 个内存属性彼此**正交、可任意组合**——一个事务可以「既可缓冲、又可修改、还建议读分配」，这正好对应 4 个独立 bit 的按位或。若做成互斥枚举，就得为每种组合列一个名字（本库的 `mem_type_t` 其实就为常见组合这么做了，共 12 种），而且无法用 `|` 灵活组合。互斥场景（如突发类型只能是 3 选 1）才适合枚举值。

**练习 2**：`bufferable(CACHE_BUFFERABLE | CACHE_MODIFIABLE)` 的返回值是什么？

> **参考答案**：返回 1（真）。因为 `bufferable(cache)` 的实现是 `|(cache & CACHE_BUFFERABLE)`（[src/axi_pkg.sv:219-221](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L219-L221)），输入值里置了 `CACHE_BUFFERABLE` 那一位，与出来非零，归约或为 1。

---

### 4.4 localparam 全景二：`ATOP_*` 与 `atop_t`（预告 u15）

#### 4.4.1 概念说明

`atop_t` 是 `axi_pkg` 里最「宽」的字段类型之一：6 比特（`AtopWidth=6`），编码 AXI5 的**原子操作（Atomic Operations，ATOPs）**。本节只讲它的**编码结构与常量命名**，帮助你在源码里看懂这些名字；ATOPs 的完整协议语义、以及如何用 `axi_atop_filter` 处理它们，留给 [u15-l1 ATOPs 原子操作与 axi_atop_filter](u15-l1-atops-and-atop-filter.md)。

理解 `atop_t` 的关键，是把它看作**三个子字段拼接**而成，而不是一个扁平的 6 位值：

| 子字段位 | 名字 | localparam 代表值 | 含义 |
|----------|------|--------------------|------|
| `[5:4]` | 操作大类 | `ATOP_NONE=2'b00`、`ATOP_ATOMICSTORE=2'b01`、`ATOP_ATOMICLOAD=2'b10`（`ATOP_ATOMICSWAP`/`ATOP_ATOMICCMP` 这两类 `[5:4]=2'b11`） | 决定是「非原子」「存而不返回」「原子并返回」还是「交换/比较交换」 |
| `[3]` | 端序 | `ATOP_LITTLE_END=1'b0`、`ATOP_BIG_END=1'b1` | 算术运算的大小端（仅算术类有效） |
| `[2:0]` | 算子 | `ATOP_ADD/CLR/EOR/SET/SMAX/SMIN/UMAX/UMIN` | 加、清、异或、置、有/无符号最大最小 |

之所以有 `ATOP_ATOMICSWAP=6'b110000`、`ATOP_ATOMICCMP=6'b110001` 这种**完整 6 位**写法，是因为交换/比较交换是自成一类的特殊操作（`[5:4]=2'b11`），不像算术类那样还要细分 `[3]`/`[2:0]`，所以直接给出全 6 位更清楚。

另一个极重要的常量是 `ATOP_R_RESP`：它**不是 atop 的某个取值，而是一个位下标**（值 = 5，即 bit 5）。它的用途是判断「这次原子操作是否会产生读响应」——因为 bit 5 恰好是操作大类 `[5:4]` 的高位，凡是会返回数据给 Master 的原子操作（交换、比较交换、AtomicLoad），bit 5 都是 1。于是源码里常见这样的写法（见 `axi_pkg` 自己的注释示例）：

```systemverilog
if (req_i.aw.atop[axi_pkg::ATOP_R_RESP]) begin   // 即 atop[5]
  // 这次原子写会带一个读响应，需要额外处理 R 通道
end
```

这个判别位是 [u1-l1](u1-l1-project-overview.md) 提到「ATOP_R_RESP 的原子写会同时产生 B 和 R 响应」在源码层面的落点。

#### 4.4.2 核心流程

判别一次原子操作「会不会返回数据」的流程：

```
读 AW 通道的 aw.atop（6 位）
        │
        ├── 测 atop[5]  （= atop[axi_pkg::ATOP_R_RESP]）
        │       │
        │       ├── 1 → 会产生 R 响应（SWAP / CMP / AtomicLoad）
        │       └── 0 → 只产生 B 响应，无数据返回（AtomicStore / 非原子）
        │
        └── 进一步看 [5:4] / [3] / [2:0] 区分具体操作
```

这条判别是后续 `axi_atop_filter`（u15-l1）决定是否拦截某次原子写、以及互联组件决定是否为它预留 R 通道资源的依据。

#### 4.4.3 源码精读

`atop_t` 类型与其宽度：

[src/axi_pkg.sv:42-43](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L42-L43) —— `AtopWidth=6`；[src/axi_pkg.sv:63-64](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L63-L64) —— `typedef logic [5:0] atop_t;`。这是本节所有常量的类型基础。

`ATOP_*` 常量全集（按子字段分组）：

[src/axi_pkg.sv:380-397](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L380-L397) —— 完整 6 位的 `ATOP_ATOMICSWAP=6'b110000`、`ATOP_ATOMICCMP=6'b110001`，每条注释详细说明了交换/比较交换的语义（发送什么、返回什么）。

[src/axi_pkg.sv:398-415](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L398-L415) —— `[5:4]` 操作大类：`ATOP_NONE=2'b00`、`ATOP_ATOMICSTORE=2'b01`、`ATOP_ATOMICLOAD=2'b10`。

[src/axi_pkg.sv:416-423](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L416-L423) —— `[3]` 端序位：`ATOP_LITTLE_END=1'b0`、`ATOP_BIG_END=1'b1`。

[src/axi_pkg.sv:424-444](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L424-L444) —— `[2:0]` 算子：`ATOP_ADD/CLR/EOR/SET/SMAX/SMIN/UMAX/UMIN`，注释说明了每个算子的运算语义与是否有符号。

`ATOP_R_RESP` 判别位：

[src/axi_pkg.sv:445-447](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L445-L447) —— `ATOP_R_RESP = 32'd5`，注释直接给了用法示例 `req_i.aw.atop[axi_pkg::ATOP_R_RESP]`。注意它是**位下标 5**，不是 atop 的取值。

#### 4.4.4 代码实践

**实践目标**：用 4.4.1 的子字段结构，拆解一个完整 atop 值。

**操作步骤**（源码阅读 + 心算）：

1. 打开 [src/axi_pkg.sv:387](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L387)，记下 `ATOP_ATOMICSWAP = 6'b110000`。
2. 按子字段拆这 6 位：`[5:4]=2'b11`（交换类）、`[3]=0`（小端）、`[2:0]=3'b000`。
3. 单独看 bit 5：它是 1，所以 `atop[ATOP_R_RESP]` 为真——说明原子交换会产生读响应。

**需要观察的现象**：`ATOP_ATOMICSWAP` 的 bit 5 为 1，与其「交换会把原值返回给 Master」的语义一致。

**预期结果**：你得到结论——交换类操作 `[5:4]=11`，bit 5 自然为 1，因此 `atop[ATOP_R_RESP]` 为真，互联必须为它准备 R 通道。对比 `ATOP_ATOMICSTORE=2'b01`（`[5:4]=01`，bit 5 为 0），它只写不返回，不产生 R 响应。真要在仿真里打印确认，可在 testbench 里 `$display` 这些常量（待本地验证），但对照源码拆位已足以确认。

#### 4.4.5 小练习与答案

**练习 1**：`ATOP_R_RESP` 是一个 6 位的 atop 取值吗？

> **参考答案**：不是。它是一个**位下标常量**，值 = 5（[src/axi_pkg.sv:447](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L447)）。它的用途是写 `aw.atop[ATOP_R_RESP]` 来测试 bit 5，从而判断该原子操作是否会产生读响应。把它命名为常量而不是裸写 `atop[5]`，是为了可读性。

**练习 2**：为什么 `ATOP_ATOMICSTORE`（`2'b01`）写成 2 位，而 `ATOP_ATOMICSWAP`（`6'b110000`）写成 6 位？

> **参考答案**：因为 `ATOP_ATOMICSTORE` 只用到 `[5:4]` 这两位来表示「存而不返回」这个大类（其余位由算子/端序另行决定或为 0），所以给 2 位片段即可；而 `ATOP_ATOMICSWAP` 是 `[5:4]=11` 这一类里不再细分的完整操作，直接给出全 6 位值更不容易和算术类的拼接写法混淆。两种写法都是本库为了清晰而做的取舍。

---

## 5. 综合实践

把本讲四个模块串起来，完成下面这个任务（对应本讲规格里的代码实践任务）。

**任务**：列全 `axi_pkg` 中每个 typedef 对应的位宽常量，然后写一小段 SystemVerilog，**同时**用到本讲讲过的几类内容——宽度常量/typedef（4.2）、`CACHE_*` 位标志（4.3）、`atop_t`/`ATOP_R_RESP`（4.4），并保证它能通过 `axi_pkg` 这个 Level 0 包的作用域引用正确编译。

**建议步骤**：

1. **列表**：对照 [src/axi_pkg.sv:24-66](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L24-L66)，写出本讲 4.2.2 那张对照表（11 个宽度常量 ↔ 10 个 typedef，`LockWidth` 无 typedef）。这一步直接完成「列出每个 typedef 对应的位宽常量」。
2. **写片段**：仿照 4.2.4 的写法，扩展成下面这段综合示例（示例代码，非仓库原有文件）：

```systemverilog
// 示例代码：综合演示 axi_pkg 的类型、CACHE 位标志、ATOP 判别位
module demo_axi_pkg_types (
  output axi_pkg::resp_t  o_resp,     // 4.2: resp_t  <-> RespWidth=2
  output axi_pkg::cache_t o_cache,    // 4.2: cache_t <-> CacheWidth=4
  output axi_pkg::atop_t  o_atop      // 4.2: atop_t  <-> AtopWidth=6
);
  always_comb begin
    // 4.2: 用常量给 resp 赋值
    o_resp  = axi_pkg::RESP_DECERR;                 // 2'b11
    // 4.3: 用位标志按位或组合 cache 属性
    o_cache = axi_pkg::CACHE_BUFFERABLE | axi_pkg::CACHE_MODIFIABLE;  // 4'b0011
    // 4.4: 给一个原子交换值，并示意如何判别它会带读响应
    o_atop  = axi_pkg::ATOP_ATOMICSWAP;             // 6'b110000
  end

  // 4.4: 编译期即可判别——交换操作的 bit5 为真，说明它会产生 R 响应
  // （u15-l1 会讲如何用 axi_atop_filter 处理这类操作）
  localparam logic SwapHasRResp = axi_pkg::ATOP_ATOMICSWAP[axi_pkg::ATOP_R_RESP];
endmodule
```

3. **自检**：逐行确认每个名字都来自 `axi_pkg`——`resp_t`/`cache_t`/`atop_t` 是 4.2 的 typedef，`RESP_DECERR` 是 u1-l3 讲过的响应常量，`CACHE_BUFFERABLE`/`CACHE_MODIFIABLE` 是 4.3 的位标志，`ATOP_ATOMICSWAP`/`ATOP_R_RESP` 是 4.4 的原子操作常量与判别位。

**自检要点**（做完后对照）：

- 表格里 `LockWidth=1` 没有对应 typedef，你能解释为什么（见 4.2.5 练习 1）。
- `o_cache = CACHE_BUFFERABLE | CACHE_MODIFIABLE` 是合法的，因为 cache 是**位标志**而非互斥枚举（见 4.3.5 练习 1）。
- `SwapHasRResp` 这个 localparam 在 elaboration 时就应为 1，因为 `ATOP_ATOMICSWAP=6'b110000` 的 bit 5 是 1（见 4.4.4）。
- 这段代码若要仿真，必须和 `axi_pkg.sv` 一起编译（后者是 Level 0，先编译），具体流程见 [u1-l4](u1-l4-compile-sim-synth.md)；实际运行标注「待本地验证」。

## 6. 本讲小结

- `axi_pkg` 是全库唯一的 `package`，位于编译层级 **Level 0**：它不依赖任何人，却被几乎所有模块 `import`，是全库类型与常量的「单一事实来源」。
- 使用它有两种等价写法：通配导入 `import axi_pkg::*;`（如 `axi_test.sv`）和作用域引用 `axi_pkg::resp_t`（如 `axi_lite_to_apb.sv`）。
- 本库用「**宽度常量 parameter ↔ typedef**」成对模式组织字段类型：11 个 `XxxWidth` 常量对应 10 个 `xxx_t` 类型别名（`LockWidth` 是没有 typedef 的例外）；这些宽度常量还被通道宽度函数（如 `aw_width`）相加复用，不只是注释。
- `CACHE_*` 是 4 个**可按位或的位标志**（BUFFERABLE/MODIFIABLE/RD_ALLOC/WR_ALLOC），不是互斥枚举；`mem_type_t` 给 12 种常见组合命名，`get_arcache`/`get_awcache` 把它翻译成 AR/AW 两侧（可能不同）的 cache 值。
- `atop_t`（6 位）按 `[5:4]` 大类 / `[3]` 端序 / `[2:0]` 算子三段编码 AXI5 原子操作；`ATOP_R_RESP` 是位下标 5，用来判别「该原子操作是否产生读响应」，其完整语义与 `axi_atop_filter` 留给 u15-l1。
- `BURST_*`/`RESP_*` 的取值已在 u1-l3 讲透，本讲只把它们锚定为 localparam 的一类；辅助函数与配置结构体（`xbar_cfg_t` 等）留给 u2-l2。

## 7. 下一步学习建议

- 下一讲 [u2-l2 axi_pkg：辅助函数与配置结构体](u2-l2-axi-pkg-funcs-cfg.md) 会把本讲只点了名的「函数」和「配置结构体」读完：`num_bytes`/地址对齐计算、`xbar_cfg_t`、`xbar_rule_64_t/32_t`、`xbar_latency_e`。建议带着本讲对「typedef/localparam/函数三位一体」的理解过去。
- 如果你想立刻看到这些类型在真实模块里被使用，可以读 [src/axi_lite_to_apb.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_to_apb.sv)，看它如何用 `axi_pkg::resp_t` 把 APB 的错误码翻译成 `RESP_DECERR`/`RESP_SLVERR`——这是本讲 4.2 最直白的应用。
- 想了解 `atop_t`/`ATOP_*` 的完整协议语义，直接跳到 [u15-l1 ATOPs 原子操作与 axi_atop_filter](u15-l1-atops-and-atop-filter.md)；不过按学习路线，建议先在 U2–U3 把接口（`axi_intf`）、宏（`typedef/assign/port`）、验证组件（`axi_test`）读完再进入进阶层。
