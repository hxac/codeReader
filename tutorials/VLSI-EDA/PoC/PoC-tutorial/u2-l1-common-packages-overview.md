# 公共包总览与 Common 上下文

## 1. 本讲目标

本讲是第 2 单元（公共包与配置机制）的起点。学完本讲，读者应该能够：

- 列举 `src/common/` 下各公共包（`utils` / `config` / `math` / `strings` / `vectors` / `physical` / `components` / `debug` / `fileio`）各自的职责；
- 理解 VHDL-2008 的 `context` 机制，以及 PoC 的 `context Common` 如何把一长串 `use` 子句打包成一次引用；
- 读懂 `common.files` 这份编译清单，特别是它如何根据 **VHDL 版本、工具链、运行环境** 有条件地选择编译哪些文件。

本讲只做“总览”：告诉你有哪些公共包、怎么一次性引用、按什么顺序编译。每个包的内部细节会在 `u2-l2` ~ `u2-l5` 逐个展开。

## 2. 前置知识

在进入源码前，先用通俗语言澄清三个概念：

- **VHDL 包（package）**：类似 C 语言的头文件 / 共享库。它把类型、常量、函数、元件声明集中放在一起，其它设计单元用 `use` 就能复用，不必重复定义。一个包通常由“声明（`package ... is`）”和“实现（`package body ... is`）”两部分组成。
- **`library` / `use` 子句**：`library PoC;` 表示“我要用一个叫 PoC 的库”，`use PoC.utils.all;` 表示“把 PoC 库里 utils 包的全部可见内容引入当前文件”。这是 VHDL 复用代码的基本手段。
- **统一编译进 PoC 库**：PoC 约定所有 VHDL 源码都编译进名为 `PoC` 的逻辑库（见 `u1-l2`）。因此公共包写好后，全库的核都能 `use PoC.<包名>.all;` 直接用。

还需要回忆前面两讲建立的认知：

- `u1-l2`：`src/common/` 是全库的“地基”，`tb/` 镜像 `src/`；`.files` 是 pyIPCMI 消费的编译清单；命名空间级包叫 `<ns>.pkg.vhdl`。
- `u1-l4`：源码用 `.vhdl` 后缀，命名空间前缀蛇形命名，可综合实现用 `architecture rtl`，文档头有固定格式。

本讲会用到但不在本讲深挖的两个东西（后续讲义专门讲）：`my_config` / `my_project` 模板（`u1-l3` 已演示如何创建，`u2-l3` 会讲它如何被 `config` 包解析），以及 pyIPCMI 如何实际消费 `.files`（`u5-l1` 详解）。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| `src/common/README.md` | 公共包的文字说明书：逐个说明每个包的职责，给出 `use` 示例，并指出 `context` 的存在。 |
| `src/common/common.vhdl` | 定义 VHDL-2008 的 `context Common`，把多个公共包的 `use` 子句打包到一起。 |
| `src/common/common.files` | pyIPCMI 消费的编译清单：规定编译顺序，并用 `if` 条件选择版本相关的文件。 |
| `src/common/*.vhdl`（各包源码） | 公共包本身的实现，本讲只确认它们的名字与职责。 |
| `tb/common/my_config.files` | 被 `common.files` 通过 `include` 引入的子清单，按板名选择板级配置文件。 |

## 4. 核心概念与源码讲解

### 4.1 公共包清单

#### 4.1.1 概念说明

PoC 是一个由数百个 IP 核组成的大库。如果每个核都自己定义“整数求最大值”“把深度换算成地址位宽”“表示一个频率”这类东西，代码会大量重复且容易出错。所以 PoC 把这些**跨核复用的基础能力**抽到 `src/common/`，做成一组 VHDL 包，称为“公共包”。

可以把公共包理解成 PoC 的“标准库”或“地基”：任何一个命名空间的核（`arith_*`、`fifo_*`、`ocram_*`……）几乎都会 `use` 其中几个公共包。后面几讲你会看到：

- `utils` 提供 `log2ceil`、`ite`、`imin`、`imax` 等高频辅助函数；
- `config` 把你在 `my_config.vhdl` 里填写的板/器件翻译成厂商信息（这是核能“按厂商自动选实现”的源头）；
- `physical` 提供频率 `FREQ` 等物理类型，让你在测试台里直接写 `100 MHz`。

公共包分两类：

1. **纯 VHDL、跨版本可移植的包**：`utils`、`config`、`math`、`strings`、`vectors`、`physical`、`components`、`debug`。它们对任何符合标准的仿真/综合工具都一样。
2. **与 VHDL 版本相关的包**：`fileio`（文件 IO）和 `protected`（受保护类型）。它们在不同 VHDL 版本下源码不同，因此有 `.v93.vhdl` / `.v08.vhdl` 两套文件。这正是 4.3 节 `.files` 要解决的问题。

#### 4.1.2 核心流程

公共包在整个库里处于“依赖图的根”。一个核使用公共能力的流程是：

```text
核源码（如 sortnet_OddEvenMergeSort.vhdl）
        │  写 library PoC; use PoC.utils.all; ...
        ▼
公共包（utils/config/...，已编译进 PoC 库）
        ▲
        │  pyIPCMI 按 common.files 的顺序把这些包编译进 PoC 库
common.files（编译清单）
```

换句话说，公共包必须**先于**使用它的核被编译进 `PoC` 库。这个“先于”的顺序，就是由 `common.files` 规定的（见 4.3）。

#### 4.1.3 源码精读

`src/common/README.md` 用一段列表交代了公共包的职责，这是最权威的“官方说明”：

[src/common/README.md:5-19](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/README.md#L5-L19) —— README 的 `## Packages` 小节，逐条列出公共包及其一句话职责（如 `PoC.physical` 实现频率 `FREQ`、波特率、存储等物理类型及转换函数）。

把 README 的说明和目录里的实际文件一一对应，得到下表（包名取自各文件里的 `package ... is` 声明）：

| 源文件 | 包名（package） | 职责（据 README） |
| --- | --- | --- |
| `utils.vhdl` | `utils` | 通用辅助函数（如 `log2ceil`、`ite`、`imin`、`imax`） |
| `config.vhdl` | `config`（另含 `config_private`） | PoC 的配置机制：把板/器件解析成厂商与器件信息 |
| `math.vhdl` | `math` | 扩展数学函数 |
| `strings.vhdl` | `strings` | 定长字符串上的字符串操作 |
| `vectors.vhdl` | `vectors` | 多维向量类型及转换函数 |
| `physical.vhdl` | `physical` | 物理类型：频率 `FREQ`、波特率、存储等 |
| `components.vhdl` | `components` | 可综合的常用门 / 触发器函数（如 `ffdre`、`ffse`） |
| `debug.vhdl` | `debug` | 调试辅助 |
| `fileio.v93.vhdl` / `fileio.v08.vhdl` | `FileIO` | 文件 IO（随 VHDL 版本二选一） |
| `protected.v08.vhdl` | `ProtectedTypes` | 受保护类型实现（仅 2008 版编译） |

注意两点：

- README 列了 **9 个**对外公共包：`config`、`components`、`debug`、`fileio`、`math`、`physical`、`strings`、`utils`、`vectors`。`ProtectedTypes` 没出现在 README 里，它是 `fileio` v08 版依赖的“支撑包”，普通用户不直接 `use`。
- `config.vhdl` 里同时声明了 `config` 和 `config_private` 两个包：前者是公开接口，后者是内部实现细节（下一讲 `u2-l3` 会拆开讲）。

举一个真实的“核引用公共包”的例子。排序网络核 `sortnet_OddEvenMergeSort` 顶部这样写：

[src/sort/sortnet/sortnet_OddEvenMergeSort.vhdl:35-40](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/sort/sortnet/sortnet_OddEvenMergeSort.vhdl#L35-L40) —— 该核逐条 `use` 了 `PoC.math`、`PoC.config`、`PoC.utils`、`PoC.vectors`、`PoC.components`。这说明公共包确实是各核的日常依赖。

#### 4.1.4 代码实践

**实践目标**：把 README 的文字描述与真实的包文件对应起来，并确认一个高频函数的位置。

**操作步骤**：

1. 打开 `src/common/README.md`，阅读 `## Packages` 小节。
2. 打开 `src/common/utils.vhdl`，定位 `log2ceil` 的**声明**：[src/common/utils.vhdl:128](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/utils.vhdl#L128) —— `function log2ceil(arg : positive) return natural;`。
3. 在同一文件里找到它的**实现**（`package body` 部分，约第 513 行附近）。

**需要观察的现象**：声明（在 `package` 里）和实现（在 `package body` 里）是分开的两段；`log2ceil` 接收一个正整数（如深度），返回向上取整的地址位宽（自然数）。

**预期结果**：你能说出 `log2ceil` 的签名 `arg : positive → natural`，并理解它正是“把 FIFO 深度换算成地址位宽”这类需求的来源（深度 30 → 位宽 5）。

> 待本地验证：如果你用仿真器（如 GHDL）实际调用 `log2ceil(30)`，预期返回 `5`；本讲不要求运行，确认到源码位置即可。

#### 4.1.5 小练习与答案

**练习 1**：README 一共列了几个对外公共包？分别是哪些？
**答案**：9 个：`config`、`components`、`debug`、`fileio`、`math`、`physical`、`strings`、`utils`、`vectors`。

**练习 2**：`protected.v08.vhdl` 里定义的包叫什么名字？它为什么没出现在 README 的包清单里？
**答案**：包名是 `ProtectedTypes`。它是 `fileio` v08 版所依赖的内部支撑包，普通使用者不直接 `use`，所以 README 没把它列为对外公共包。

**练习 3**：`fileio` 为什么有两个源文件（`.v93.vhdl` 和 `.v08.vhdl`）而 `utils` 只有一个？
**答案**：`fileio` 用到了 VHDL-2008 才标准化的“受保护类型（protected）”等特性，在 VHDL-1993 下无法用同一套源码表达，所以按版本分两套；`utils` 是纯 VHDL、跨版本可移植，一套源码即可。

---

### 4.2 Common context

#### 4.2.1 概念说明

即便有了公共包，仍然有个小烦恼：一个核如果要用 8 个公共包，就得在文件头写 8 行 `use PoC.<包名>.all;`，每个核都重复一遍，既啰嗦又容易漏。

VHDL-2008 为此引入了 **`context`（上下文）** 机制：把一组 `library` / `use` 子句用一个名字封装起来，别处引用这个名字，就等价于展开所有这些子句。可以把它类比成“预设的 import 套餐”或 C 里的一个“聚合头文件”。

PoC 在 `src/common/common.vhdl` 里就定义了一个名为 `Common` 的上下文，专门打包所有公共包的引用。

#### 4.2.2 核心流程

`context` 的用法分两步：

```text
1) 定义（一次性，写在 common.vhdl 里）：
     context Common is
       library PoC;
       use PoC.config.all;
       use PoC.math.all;
       ...（更多 use）
     end context;

2) 引用（在任意需要这些包的设计单元里，VHDL-2008 语法）：
     context PoC.common;        -- 等价于上面所有 library/use 一次性生效
     entity foo is ...
```

第二步的 `context PoC.common;` 是 VHDL-2008 标准规定的“上下文子句”，写在设计单元的上下文区域（也就是平时写 `library` / `use` 的位置）。展开后，等价于把定义里的全部 `library` / `use` 原样搬过来。

#### 4.2.3 源码精读

`common.vhdl` 的文档头点明了它的用途：

[src/common/common.vhdl:7-11](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/common.vhdl#L7-L11) —— 说明本文件“作为单个 context 提供 src/common 下的所有包”。

上下文本体如下：

[src/common/common.vhdl:31-41](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/common.vhdl#L31-L41) —— `context Common` 引用了 `config`、`debug`、`FileIO`、`math`、`physical`、`strings`、`utils`、`vectors` 共 **8 个**包。

这里有一个**容易被忽略、但很重要的细节**：`context Common` 里**没有** `use PoC.components.all;`。也就是说，`components` 包不在“套餐”内。这与 README 的 `Usage` 示例完全一致：

[src/common/README.md:21-33](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/README.md#L21-L33) —— README 的 `**Usage:**` 代码块同样列了 8 行 `use`，也**不含** `components`。注释里还特别标注 `fileio` 仅在厂商工具支持时才可用。

这带来一个实际结论：即便你用了 `context Common`，只要你用到了触发器/选择器这类原语，就还得**单独**补一句 `use PoC.components.all;`。回顾 4.1.3 里 `sortnet_OddEvenMergeSort` 的例子，它正是单独写了 `use PoC.components.all;`。

另外，从全库的实践看，PoC 自己的核大多**并没有**使用 `context Common`，而是像 `sortnet_OddEvenMergeSort` 那样**逐条手写**需要的 `use`。`context Common` 更多是一个 VHDL-2008 提供的便利设施，让你在愿意时可以少写几行。这一点 README 也有说明：

[src/common/README.md:36-38](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/README.md#L36-L38) —— `PoC.common` 提供一个“面向所有公共包的 VHDL-2008 上下文”。

#### 4.2.4 代码实践

**实践目标**：通过对比，验证 `context Common` 与 README 的 `Usage` 块“少了 `components`”，并理解核里为什么要单独引用它。

**操作步骤**：

1. 打开 `common.vhdl`，数一数 `context Common` 里有几条 `use`，记下涉及的包名。
2. 打开 `README.md` 的 `**Usage:**` 块，对比它列出的 `use` 行。
3. 打开任意一个用到触发器的核（例如 `src/sort/sortnet/sortnet_OddEvenMergeSort.vhdl`），看它是否单独写了 `use PoC.components.all;`。

**需要观察的现象**：`context Common` 与 README `Usage` 块都只有 8 个包，且都不含 `components`；而真正用到原语的核都额外补了 `use PoC.components.all;`。

**预期结果**：你能解释“为什么核里常见到单独的 `use PoC.components.all;`”——因为 `components` 不在 `Common` 上下文这个套餐里。

#### 4.2.5 小练习与答案

**练习 1**：`context Common` 一共打包了几个 `use`？
**答案**：8 个（`config`、`debug`、`FileIO`、`math`、`physical`、`strings`、`utils`、`vectors`）。

**练习 2**：哪个常用公共包**不在** `context Common` 里？用触发器的核该怎么补救？
**答案**：`components` 不在里面。核需要单独写 `use PoC.components.all;`。

**练习 3**：在设计单元里引用一个已定义的上下文，用的是什么 VHDL-2008 关键字？
**答案**：上下文子句关键字 `context`，写作 `context PoC.common;`（注意它和定义处的 `context Common is` 形似但作用不同：一个是引用，一个是定义）。

---

### 4.3 .files 编译清单

#### 4.3.1 概念说明

需要特别强调：**`.files` 不是 VHDL**，而是 PoC 的 Python 基础设施 pyIPCMI 自定义的一种清单语言。它的作用是告诉 pyIPCMI：要把哪些文件、按什么顺序、在什么条件下编译进哪个库。

为什么需要它？因为 PoC 要同时支持多家厂商工具（GHDL、ModelSim、Quartus、Vivado……）、多个 VHDL 版本（1993/2002/2008）、两种运行环境（综合 / 仿真），不同情况下该编译的文件并不一样。`.files` 用一套小语言把这些条件一次性写清楚，pyIPCMI 读取后结合当前配置求值，生成具体的编译列表。

`common.files` 用到的语法元素：

| 语法 | 含义 |
| --- | --- |
| `vhdl  poc  "路径"` | 把一个 VHDL 文件编译进 `poc`（即 `PoC`）库；三段分别是“语言 / 目标库 / 文件路径” |
| `include "路径"` | 引入另一份 `.files` 清单（可嵌套） |
| `if (条件) then ... elseif ... else ... end if` | 条件包含，条件为真才编译其中文件 |
| `report "..."` | 条件不满足时报错（类似断言失败） |
| 条件变量 | `VHDLVersion`、`ToolChain`、`Tool`、`Environment`、`BoardName` 等，由 pyIPCMI 根据当前配置注入 |

#### 4.3.2 核心流程

pyIPCMI 处理 `common.files` 的过程可以这样描述：

```text
读取 common.files
   │
   ├─ 顺次处理每条 vhdl / include / if
   │
   ├─ 遇到 if：用当前 VHDLVersion / ToolChain / Environment 等求值
   │     ├─ 真 → 把分支里的文件加入“待编译列表”
   │     └─ 假 → 跳过；若走到 report → 报错中止
   │
   └─ 输出：一个有序的文件列表 → 按序编译进 PoC 库
```

关键点：**顺序很重要**。被依赖的包（如 `utils`、`config`）必须排在用它们的包前面；`fileio` v08 依赖 `ProtectedTypes`，所以 `protected.v08.vhdl` 必须排在 `fileio.v08.vhdl` 前面。

#### 4.3.3 源码精读

整份 `common.files` 不长，可以分三段读。

**第一段：引入板/器件配置。**

[src/common/common.files:8](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/common.files#L8) —— 通过 `include` 引入 `tb/common/my_config.files`，把“板级 / 器件配置”先编译进来。这一份子清单会按 `BoardName` 在 `my_config_GENERIC.vhdl`、`my_config_KC705.vhdl` 等之间选一个（`u2-l3` 详解）。

**第二段：核心公共包（无条件编译）。**

[src/common/common.files:11-17](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/common.files#L11-L17) —— 按固定顺序编译 7 个可移植包：`utils` → `config` → `math` → `strings` → `vectors` → `physical` → `components`。注意这里**没有** `if`，说明它们在所有工具/版本/环境下都会编译。顺序也体现了依赖：`utils` 是最底层、排第一；`components` 排最后，因为它可能用到前面的类型。

**第三段：版本相关的条件编译（本讲重点）。**

[src/common/common.files:19-28](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/common.files#L19-L28) —— 这就是本讲实践任务要分析的 `fileio` 条件分支：

```text
if (ToolChain not in ["Altera_QuartusII", "Lattice_Diamond"]) then
    if (VHDLVersion < 2002) then
        编译 fileio.v93.vhdl
    elseif (VHDLVersion <= 2008) then
        先编译 protected.v08.vhdl，再编译 fileio.v08.vhdl
    else
        report "VHDL version not supported."
end if
```

逐层解读：

1. **外层 `ToolChain` 判断**：只有当工具链**不是** Altera QuartusII、**也不是** Lattice Diamond 时，才编译 `fileio`。换句话说，这两个厂商工具链下 PoC 根本不提供 `fileio`（这两家综合工具对仿真用的文件 IO 支持有限，或 PoC 选择不为它们编译）。
2. **内层按 VHDL 版本二选一**：
   - `VHDLVersion < 2002`（即 VHDL-1987 / 1993）：编译 `fileio.v93.vhdl`；
   - `VHDLVersion <= 2008`（即 2002 / 2008）：**先**编译 `protected.v08.vhdl`（`ProtectedTypes` 包），**再**编译 `fileio.v08.vhdl`；
   - 其它（比 2008 还新的未知版本）：`report` 报错。

**为什么不同 VHDL 版本要编译不同文件？** 因为 `fileio` 包内部要用 VHDL 的**受保护类型（protected type）**来封装文件句柄，使多个进程能安全地共享同一个文件。受保护类型是 VHDL-2000/2002 起引入、2008 标准化的特性，VHDL-1993 里**不存在**。于是：

- 2002/2008 版可以用受保护类型，所以 `fileio.v08.vhdl` 依赖 `protected.v08.vhdl`（`ProtectedTypes` 包），两者一起编译；
- 1993 版没有受保护类型，必须换一套不依赖它的实现，即 `fileio.v93.vhdl`，且不需要 `protected.v08.vhdl`。

这就是“同一份功能、两套源码、按版本条件选择”的根本原因。

**第四段：仿真环境额外包。**

[src/common/common.files:30-32](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/common.files#L30-L32) —— 仅当 `Environment = "Simulation"` 时，`include` 进 `src/sim/sim.files`，把仿真专用辅助包（`sim_*`，`u4-l1` 详解）一并编译。综合时这段会被跳过。

#### 4.3.4 代码实践（本讲主任务）

**实践目标**：在 `common.files` 中定位“按条件包含 `fileio` 包”的分支，并解释为什么不同 VHDL 版本会编译不同的文件。

**操作步骤**：

1. 打开 `src/common/common.files`，找到包含 `fileio` 字样的 `if` 块（即上面引用的第 19–28 行）。
2. 回答三个子问题：
   - 这个块受哪个**工具链**条件保护？哪两个工具链会被完全排除？
   - 在 `VHDLVersion < 2002` 时编译哪个文件？在 `<= 2008` 时编译哪两个文件？
   - 为什么 2008 版要额外先编译 `protected.v08.vhdl`，而 1993 版不需要？
3. 交叉验证：打开 `src/common/`，确认目录里确实同时存在 `fileio.v93.vhdl` 和 `fileio.v08.vhdl` 两个文件，且 `protected.v08.vhdl` 只有一个 2008 版本（没有 `.v93` 版）。

**需要观察的现象**：`fileio` 的编译被两层 `if` 包住；版本分支恰好对应磁盘上的两套 `fileio` 文件；`protected` 只有 v08 一个文件，正好只在 `<= 2008` 分支里出现。

**预期结果**：你能用自己的话讲清楚——受保护类型是 2008 才标准化的，所以 `fileio` 在 1993 和 2008 下用了两套实现，由 `common.files` 的条件分支自动选择；而 Altera/Lattice 这两条工具链下根本不编译 `fileio`。

> 待本地验证：若你想亲见求值效果，可用 pyIPCMI 选定一个工具链与 VHDL 版本（如 GHDL + 2008）运行，观察 pyIPCMI 生成的编译列表里是否包含 `protected.v08.vhdl` 与 `fileio.v08.vhdl`、是否不含 `.v93` 文件。本讲不要求运行。

#### 4.3.5 小练习与答案

**练习 1**：`common.files` 顶部 `include` 引入的是哪份子清单？它解决什么问题？
**答案**：引入 `tb/common/my_config.files`。它按 `BoardName` 在各 `my_config_<board>.vhdl` 之间选一个，把板/器件配置先编译进 PoC 库（细节见 `u2-l3`）。

**练习 2**：哪两个工具链在 `common.files` 里**完全不**编译 `fileio`？
**答案**：`Altera_QuartusII` 和 `Lattice_Diamond`（外层 `if` 把它们排除在外）。

**练习 3**：当 `Environment = "Simulation"` 时，`common.files` 会额外编译什么？
**答案**：会 `include "src/sim/sim.files"`，把仿真辅助包（`sim_*`）一并编译；综合环境下这段被跳过。

## 5. 综合实践

把本讲三个模块串起来，完成一个“从编译清单到 PoC 库”的全景梳理。

**任务**：画一张 `common.files` 的编译流程图，要求至少包含以下信息，并用自己的话写一段说明：

1. **无条件编译**的 7 个可移植公共包，按 `common.files` 规定的顺序排列（`utils` → `config` → `math` → `strings` → `vectors` → `physical` → `components`）。
2. **条件编译**的两处：`fileio`/`protected`（受 `ToolChain` 与 `VHDLVersion` 双重控制）和 `sim` 包（受 `Environment` 控制）。在图上标出“Altera/Lattice 不编译 fileio”“1993 用 v93、2008 用 v08 + protected”“仅仿真才编译 sim”三条分支。
3. **`context Common` 的位置**：在图旁注明它打包了哪 8 个包、**不含** `components`，并解释为什么用到触发器的核还得单独 `use PoC.components.all;`。
4. 选一个真实核（例如 `sortnet_OddEvenMergeSort`），在图上标出它 `use` 了哪些公共包，验证这些包确实都已经在它之前被 `common.files` 编译进了 PoC 库。

**预期成果**：一张能向别人解释“PoC 的公共基础设施是怎么组织、怎么编译、怎么被引用的”流程图。完成后，你就为下一讲深入 `utils` / `config` 等单个包打好了全局基础。

## 6. 本讲小结

- `src/common/` 是全库的“地基”，提供 9 个对外公共包：`utils`、`config`、`math`、`strings`、`vectors`、`physical`、`components`、`debug`、`fileio`；另有内部支撑包 `ProtectedTypes`。
- 公共包分两类：跨版本可移植的（一套源码）与版本相关的（`fileio` 有 `.v93` / `.v08` 两套）。
- `common.vhdl` 里的 `context Common` 是 VHDL-2008 的“`use` 套餐”，打包了 8 个公共包，但**不含** `components`——用触发器的核要单独 `use`。
- `.files` 不是 VHDL，而是 pyIPCMI 的编译清单语言，用 `vhdl` / `include` / `if` 描述编译顺序与条件。
- `common.files` 用 `if (VHDLVersion ...)` 在 `fileio.v93` 与 `fileio.v08 + protected.v08` 之间选择，原因是受保护类型只在 2008 标准化；并用 `ToolChain` / `Environment` 控制是否编译 `fileio` 与仿真包。
- PoC 自家的核大多逐条手写 `use`，`context Common` 是可选的便利设施。

## 7. 下一步学习建议

本讲只做了“总览”。接下来建议：

- **`u2-l2`（utils 包）**：深入本讲反复提到的 `utils.vhdl`，学 `log2ceil`、`ite`、`imin`、`imax` 等高频函数的细节。
- **`u2-l3`（config 配置机制）**：弄清 `common.files` 顶部 `include` 的 `my_config.files` / `my_config.vhdl` 最终是如何被 `config.vhdl` 解析成厂商（`VENDOR_XILINX` / `VENDOR_ALTERA` …）与器件信息的——这是后续“核按厂商自动选实现”的根。
- **`u4-l3`（VHDL 版本处理）**：若你想更系统地了解 `.v93` / `.v08` 后缀约定与受保护类型，可跳读这一讲。
- 阅读源码顺序建议：先把 `common.files` 与 `common.vhdl` 对照读熟（本讲），再挑 `utils.vhdl` 单独精读（`u2-l2`）。
