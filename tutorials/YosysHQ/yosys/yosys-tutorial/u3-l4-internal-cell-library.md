# Yosys 内部单元库：celltypes、constids、newcelltypes

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚 Yosys 的「内部单元库」是什么，它由哪两套细胞（`$...` 与 `$_..._`）组成、各自用在综合流程的哪个阶段。
- 看懂 `$and`、`$mux`、`$dff`、`$adff`、`$mem` 等典型单元的端口约定，并能从源码里查到任意一个 `$` 单元的端口定义。
- 理解 `constids.inc` 这个 X-macro 列表与 `ID()` / `ID::xxx` 宏如何把「知名标识符」变成编译期常量，从而让单元名/端口名在运行时几乎零开销。
- 理解单元类型登记的两条路线：旧的运行时表 `CellTypes`（`celltypes.h`）与新的编译期表 `StaticCellTypes` / `NewCellTypes`（`newcelltypes.h`），以及全局对象 `yosys_celltypes` 如何让任意 Pass 统一查询「某个端口是不是输入」。

本讲是 u3 单元（RTLIL 核心数据结构）的收尾：u3-l1 教你怎么**构造**一个 Cell，本讲告诉你 Yosys **认识哪些 Cell、它们的端口长什么样、这些知识从哪里来**。掌握之后，你才能真正读懂 `proc`、`opt`、`techmap` 等 Pass 在重写什么。

## 2. 前置知识

阅读本讲前，请确保已经理解：

- **RTLIL 与 Cell 的基本概念**（见 u2-l3、u3-l1）：一个 Cell 用 `type`（如 `$and`）说明种类，用 `connections_` 字典以「端口名 → SigSpec」表达接线。
- **IdString 的内部化（interning）**（见 u3-l3）：`RTLIL::IdString` 对象本体只存一个整数 `index_`，相等/比较/哈希都退化为整数运算；`\name` 是公有名、`$name` 是 Yosys 自动生成的内部名。本讲会把这套机制和单元库「缝合」起来。
- **Yosys 的综合数据流**（见 u1-l1、u1-l4）：前端读 HDL → 一串 Pass 在 RTLIL 上变换 → 后端写出。本讲关心的就是这条流水线上「流通的货物」——内部单元。

一个直觉性的比喻：如果把 RTLIL 比作一门「汇编语言」，那么内部单元库就是这门语言的「指令集」。所有前端都把自己的 HDL 翻译成这套指令集，所有 Pass 都在这套指令集内部做改写，所有后端再把这套指令集翻译成 Verilog 网表、SMT、C++ 等。本讲就是给你这份「指令集手册」的目录页。

## 3. 本讲源码地图

本讲涉及的关键文件都在 `kernel/` 下：

| 文件 | 作用 |
| --- | --- |
| [kernel/constids.inc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/constids.inc) | 一个巨大的 X-macro 列表，**按严格 ASCII 序**枚举所有「知名标识符」（单元名、端口名、参数名）。它本身不是代码，而是被多次 `#include`、每次换一种 `X(...)` 定义来生成不同结构。 |
| [kernel/rtlil.h](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h) | 三次 `#include "constids.inc"`：生成 `StaticId` 枚举、`ID::` 命名空间下的 `constexpr` 常量、以及用于二分查找的 `IdTable`；并定义 `ID(...)` 宏。 |
| [kernel/rtlil.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.cc) | `IdString::prepopulate()` 第四次 `#include` 它，为每个知名 id 预留全局字符串槽位，固定「整数下标 ↔ 字符串」的映射。 |
| [kernel/celltypes.h](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/celltypes.h) | **旧**的运行时单元类型表 `CellTypes`：构造时调用一堆 `setup_type(...)` 把每个 `$`/`$_` 单元的端口填进 `dict`。 |
| [kernel/newcelltypes.h](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/newcelltypes.h) | **新**的编译期单元类型表 `StaticCellTypes`（`constexpr`）与运行时封装 `NewCellTypes`；定义全局 `yosys_celltypes`。 |
| [kernel/yosys.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/yosys.cc) | 实例化全局 `yosys_celltypes`，并在 `yosys_setup()` 里启用静态表。 |

记忆要点：`constids.inc` 是「名字清单」，`celltypes.h`/`newcelltypes.h` 是「端口清单」，二者通过 `ID()` 宏和整数下标缝合在一起。

## 4. 核心概念与源码讲解

本讲对应三个最小模块：**内部单元命名**、**constids 与 ID 宏**、**单元类型登记**。

### 4.1 内部单元命名：Yosys 的「门级字母表」

#### 4.1.1 概念说明

Yosys 不依赖任何具体工艺，它定义了一套**固定的、与工艺无关的内部单元**，作为所有前端产出的统一目标。你可以把这套单元分成两大类：

1. **参数化高层单元 `$...`**：位宽可变、带参数。综合的「早中期」几乎只和它们打交道。例如：
   - 组合逻辑：`$and $or $xor $not $mux $pmux $bmux $reduce_and $logic_and $lt $eq $add $mul $shl ...`
   - 时序逻辑（触发器/锁存器）：`$dff $adff $sdff $dffe $dffsr $dlatch $sr ...`
   - 存储器：`$mem $memrd $memwr $meminit`（及 `_v2` 变体）
   - 形式验证/辅助：`$assert $assume $equiv $initstate $anyconst $anyseq ...`

2. **单位宽标准门 `$_..._`**：单比特、无参数的「门级」细胞，名字两端带下划线。例如 `$_AND_ $_OR_ $_NOT_ $_MUX_ $_DFF_N_ $_DFF_P_`。它们通常在 `techmap` / `abc` 之后、面向具体工艺映射时才大量出现，是更接近物理网表的表示。

一句话区分：`$and` 是「一个 8 位的与门」可以表达的东西，`$_AND_` 是「一个 1 位的与门」。`$` 单元是 RTLIL 的「高级中间表示」，`$_` 单元更接近「门级网表」。

#### 4.1.2 核心流程

一个典型组合单元（如 `$and`）的端口约定是：

```
$and:  输入 A、B  → 输出 Y      （位宽由参数 A_WIDTH/B_WIDTH/Y_WIDTH 决定）
$mux:  输入 A、B、S → 输出 Y    （S=0 选 A，S=1 选 B）
$dff:  输入 CLK、D → 输出 Q     （D 的位宽 = Q 的位宽）
$adff: 输入 CLK、ARST、D → 输出 Q（带异步复位 ARST）
$mem:  读端口 RD_* + 写端口 WR_* → RD_DATA
```

时序单元的「极性」（上升/下降沿、复位高/低有效）在 `$` 单元里**用参数表达**（如 `CLK_POLARITY`、`ARST_POLARITY`），所以一个 `$adff` 就能涵盖所有极性组合；而对应的 `$_..._` 单元则**把极性编进名字**，例如 `$_DFF_PN0_` 表示「上升沿触发、复位低有效、复位到 0」。

#### 4.1.3 源码精读

`$and` 这类二元运算单元的端口登记在 `setup_internals_eval()` 里，用一个循环批量声明：

[kernel/celltypes.h:127-140](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/celltypes.h#L127-L140) —— 把 `$and $or $xor ... $add $mul ...` 等二元运算符统一登记为「输入 A、B，输出 Y，可常量求值（`true`）」。

多路选择器的登记在：

[kernel/celltypes.h:142-143](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/celltypes.h#L142-L143) —— `$mux $pmux $bwmux` 的端口是 `{A, B, S} → {Y}`。这就是本讲实践要查的 `$mux` 端口定义。

触发器族登记在 `setup_internals_ff()`：

[kernel/celltypes.h:154-172](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/celltypes.h#L154-L172) —— 注意 `$dff`（`{CLK, D} → {Q}`）与 `$adff`（`{CLK, ARST, D} → {Q}`）的端口差异。`$adff` 在第 [162 行](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/celltypes.h#L162)。

存储器单元 `$mem` 的端口很长，把读/写端口的时钟、使能、地址、数据都摊平成独立端口：

[kernel/celltypes.h:189](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/celltypes.h#L189) —— `$mem` 的输入是 `{RD_CLK, RD_EN, RD_ADDR, WR_CLK, WR_EN, WR_ADDR, WR_DATA}`，输出是 `{RD_DATA}`。

单位宽标准门 `$_..._` 登记在 `setup_stdcells_eval()` / `setup_stdcells_mem()`：

[kernel/celltypes.h:206-214](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/celltypes.h#L206-L214) —— `$_AND_ $_OR_ $_MUX_` 等，端口名变成了 `A/B/S → Y`（注意 `$_` 单元的时钟端口叫 `C` 而非 `CLK`，见下文 `setup_stdcells_mem`）。

[kernel/celltypes.h:225-302](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/celltypes.h#L225-L302) —— 用 `for (c1 : {'N','P'}) ...` 的多重循环批量生成 `$_DFF_P_`、`$_DFF_NN0_` 这种「极性编进名字」的单元。这一段很好地解释了为什么 `$_` 单元种类这么多：每一种极性组合都是一个独立类型。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：建立「HDL 结构 → `$` 单元」的直觉。
2. **步骤**：打开 [kernel/celltypes.h](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/celltypes.h)，在 `setup_internals_eval()`（第 119 行起）里找到与下列 HDL 对应的单元：
   - `assign y = a & b;` → 应是 `$and`；
   - `assign y = a + b;` → 应是 `$add`；
   - `assign y = s ? b : a;` → 应是 `$mux`；
   - `assign y = &a;`（缩位与）→ 应是 `$reduce_and`。
3. **观察**：注意它们都遵循「二元 → `{A,B}→Y`，一元 → `{A}→Y`」的统一命名，端口名 `A/B/S/Y` 来自 `constids.inc`（见 4.2）。
4. **预期结果**：你能口述「Verilog 的某个运算符会落到哪个 `$` 单元」。

### 4.2 constids.inc 与 ID() 宏：知名标识符的编译期常量

#### 4.2.1 概念说明

上一模块里，`setup_type(ID($mux), {ID::A, ID::B, ID::S}, ...)` 用到了两种写法：`ID($mux)` 和 `ID::A`。它们为什么不是字符串 `"\\$mux"`、`"\\A"`？因为那样每次都要做字符串哈希、查全局表，开销大。

Yosys 的做法是：把所有「写代码时就已知名字」的标识符（单元名、端口名、参数名、常用属性名）集中列在一个文件里——[kernel/constids.inc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/constids.inc)，然后利用 C 预处理器（**X-macro 技巧**）把这一份清单「展开」成多种结构，从而让这些标识符变成**编译期常量**，运行时引用它们只是取一个整数下标，几乎零开销。

清单的每一行形如 `X($and)`、`X(A)`、`X(CLK)`、`X($_AND_)`，文件第一行就强调：

[kernel/constids.inc:1](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/constids.inc#L1) —— "These must be in perfect ASCII order!!!"（必须严格按 ASCII 序排列）。为什么必须有序，见 4.2.2。

#### 4.2.2 核心流程：一份清单，四次展开

`constids.inc` 被不同文件、用不同 `X(...)` 定义包含 4 次，每次「变」出一种结构。理解这 4 次展开，就理解了整个知名 IdString 机制。

**第 1 次：生成枚举 `StaticId`（赋整数下标）**

[kernel/rtlil.h:87-93](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h#L87-L93) —— `#define X(N) N,` 后 `#include "constids.inc"`，把 `X($and) X($or) X(A) ...` 展开成枚举成员 `$and, $or, A, ...`。于是每个知名标识符获得一个**稳定的小整数**（它在清单里的序号），承接 u3-l3 讲过的「IdString 内部只存 `index_`」。

**第 2 次：生成 `ID::` 命名空间下的 `constexpr` 常量**

[kernel/rtlil.h:684-688](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h#L684-L688) —— `#define X(_id) constexpr IdString _id(StaticId::_id);` 展开 `constexpr IdString $add(StaticId::$add);` 等。这样 `ID::A`、`ID::$mux` 就是编译期常量——不分配、不哈希、直接是那个整数下标。这是「最快」的引用方式。

**第 3 次：生成有序查找表 `IdTable`，支持二分查找**

[kernel/rtlil.h:696-700](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h#L696-L700) —— `#define X(_id) {#_id, ID::_id},` 生成一个 `(字符串名 → IdString)` 数组。因为清单是 ASCII 有序的，这个数组天然有序，于是可以用二分查找：

[kernel/rtlil.h:702-716](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h#L702-L716) —— `lookup_well_known_id(name)` 对 `IdTable` 做二分，把名字字符串映射回下标，复杂度 \( O(\log n) \)。这正是 `constids.inc` 必须「perfect ASCII order」的原因：二分查找要求表有序。

**第 4 次：启动时预留全局字符串槽位**

[kernel/rtlil.cc:57-67](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.cc#L57-L67) —— 在 `IdString::prepopulate()` 里，`#define X(N) populate("\\" #N);` 为每个知名 id 在全局表里存好带 `\` 前缀的字符串，把「整数下标 ↔ 字符串」的双向映射固定下来。这一步在 `yosys_setup()` 早期完成。

**`ID(...)` 宏：把上面三者粘起来**

[kernel/rtlil.h:740-749](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h#L740-L749) —— 写 `ID($mux)` 时，宏先用 `lookup_well_known_id("$mux")` 在**编译期**二分查找；若命中（`$mux` 在 `constids.inc` 里），就返回那个 `constexpr` 常量（零开销）；若没命中，退化为创建一个「immortal」IdString。所以 `ID(...)` 是「安全又快」的写法——你随手写 `ID($foo)`，是知名的就零开销，不是知名的就走运行时路径。

下表小结四种引用方式的代价：

| 写法 | 何时求值 | 代价 | 前提 |
| --- | --- | --- | --- |
| `ID::A`、`ID::$mux` | 编译期 | 取整数下标，近乎 0 | 名字必须在 `constids.inc` 中 |
| `ID($mux)` | 编译期决定走哪条路 | 命中知名则 0，否则运行时建 IdString | 任意 token |
| `escape_id("A")` 等运行时构造 | 运行期 | 字符串哈希 + 查全局表 | 任意字符串 |

#### 4.2.3 源码精读（清单里到底有什么）

`constids.inc` 的内容按类别看（都在 [kernel/constids.inc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/constids.inc)）：

- `$_..._` 标准门：从 [第 17 行](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/constids.inc#L17) `$_ALDFFE_NNN_` 开始，到 `$_XOR_`（[第 166 行](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/constids.inc#L166)）。
- `$...` 内部高层单元：从 `$abc9_flops`（[175](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/constids.inc#L175)）到 `$xor`（[294](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/constids.inc#L294)）。例如 `$and` 在 [185](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/constids.inc#L185)、`$mux` 在 [248](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/constids.inc#L248)、`$adff` 在 [177](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/constids.inc#L177)、`$mem` 在 [237](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/constids.inc#L237)。
- 端口名：`A` 在 [295](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/constids.inc#L295)、`B` 在 [346](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/constids.inc#L346)、`CLK` 在 [408](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/constids.inc#L408)、`D` 在 [434](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/constids.inc#L434)、`Q` 在 [679](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/constids.inc#L679)、`S` 在 [708](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/constids.inc#L708)、`Y` 在 [803](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/constids.inc#L803)。
- 还有大量参数名（`WIDTH`、`Y_WIDTH`、`A_SIGNED` ...）与属性名（`blackbox`、`src`、`init` ...）。

一个有趣的细节：第 3-15 行有几段 `#undef`，是为了规避平台宏冲突（如 macOS 的 `OVERFLOW`、Windows 的 `IN/OUT`），保证这些标识符能安全进入清单。

#### 4.2.4 代码实践（源码阅读型）

1. **目标**：亲手验证「同一份清单被展开成多种结构」。
2. **步骤**：
   - 在 [kernel/constids.inc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/constids.inc) 里找到 `X($mux)`（第 248 行）和 `X(A)`（第 295 行）。
   - 打开 [kernel/rtlil.h](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h)，分别看第 87-93、684-688、696-700 三处 `#include "constids.inc"` 之前的 `#define X(...)`，在脑中把 `X($mux)` 代入：
     - 第 1 处变成枚举成员 `$mux,`；
     - 第 2 处变成 `constexpr IdString $mux(StaticId::$mux);`（即 `ID::$mux`）；
     - 第 3 处变成 `{`"$mux"`, ID::$mux},`（`IdTable` 的一行）。
3. **观察**：同一行 `X($mux)`，换一个 `X` 定义就变成完全不同的代码——这就是 X-macro。
4. **预期结果**：你能解释为什么新增一个知名单元名，只需在 `constids.inc` 按字母序插一行 `X($newcell)`，三处结构就同时更新，无需改任何注册代码。

### 4.3 单元类型登记：从 celltypes.h 到 newcelltypes.h

#### 4.3.1 概念说明

光有「名字清单」还不够。Pass 在改写网表时，常常需要问：**「这个 cell 的某个端口是输入还是输出？」「这个 cell 是不是触发器？」「这个 cell 能不能做常量求值？」** 这些「关于 cell 类型的元信息」需要一个表来登记。

Yosys 历史上用 `CellTypes`（`celltypes.h`）做这件事，它是一个**运行时构建的 `dict`**。新代码迁移到了 `StaticCellTypes` / `NewCellTypes`（`newcelltypes.h`），后者是一个**编译期构建的数组表**，查询更快、还能在编译期做集合运算。两者登记的内容一致：每个单元的输入端口集、输出端口集，以及一组布尔特征（可求值、是触发器、是存储器、是标准门……）。

#### 4.3.2 核心流程

**旧表 `CellTypes`（运行时 `dict`）**

[kernel/celltypes.h:37-66](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/celltypes.h#L37-L66) —— 持有 `dict<IdString, CellType> cell_types`。`setup()`（[第 50 行](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/celltypes.h#L50)）依次调用 `setup_internals/setup_internals_mem/setup_internals_anyinit/setup_stdcells/setup_stdcells_mem`，每个函数做一串 `setup_type(...)`，把端口写进字典。`CellType` 结构定义在 [第 28-35 行](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/celltypes.h#L28-L35)，含 `inputs`、`outputs` 两个 `pool` 与三个 bool 特征。查询接口如 `cell_known`（[309](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/celltypes.h#L309)）、`cell_input`、`cell_output` 都是对 `dict` 的 `find`。

**新表 `StaticCellTypes`（编译期数组）**

这是性能关键，值得展开。`StaticCellTypes` 不用 `dict`，而是用「以 `IdString::index_` 为下标的定长数组」：

[kernel/newcelltypes.h:14-19](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/newcelltypes.h#L14-L19) —— `MAX_CELLS = 300`、`MAX_PORTS = 20`。注释点明 `MAX_CELLS` 要「不小于 constids.inc 里最后一个内部单元类型的下标」——因为下标是按 `constids.inc` 的 ASCII 全局序给的（单元名和端口名、参数名混排），所以即便单元种类只有上百个，最后一个单元的下标也可能较大，故预留 300。

[kernel/newcelltypes.h:20-62](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/newcelltypes.h#L20-L62) —— `CellTableBuilder` 用 `constexpr` 函数 `setup_type(...)` 填充 `std::array<CellInfo, MAX_CELLS>`。`PortList` 是最多 20 个端口的定长数组 + 计数；`Features` 是一组 bool（[42-51](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/newcelltypes.h#L42-L51)）。

[kernel/newcelltypes.h:406-419](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/newcelltypes.h#L406-L419) —— `constexpr CellTableBuilder builder{};` 是一个**编译期对象**，它的构造函数（全 `constexpr`）在编译阶段就把整张表算好，运行时 `.rodata` 里直接是填好的数组。

然后由这张表派生出两张「按 `index_` 直查」的查找表与一组布尔分类：

[kernel/newcelltypes.h:421-442](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/newcelltypes.h#L421-L442) —— `PortInfo` 把每个单元的输入/输出端口表放到「以 `index_` 为下标」的数组里，于是「某端口是不是输入」退化为 `port_info.inputs[type.index_].contains(port)`：一次 \( O(1) \) 数组下标 + 一次 ≤20 项的小线性扫描，**无需哈希、无需 dict**。

[kernel/newcelltypes.h:444-519](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/newcelltypes.h#L444-L519) —— `Categories` 是一组 `std::array<bool, MAX_CELLS>` 位集：`is_known / is_evaluable / is_ff / is_mem_noff / is_stdcell / is_anyinit / is_tristate`，并支持编译期集合运算 `join`（并）、`meet`（交）、`complement`（补）。比如「内部单元全集」= `is_known ∧ ¬is_stdcell`（见 `Compat` 命名空间，[第 522-535 行](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/newcelltypes.h#L522-L535)）。「某单元是不是触发器」就是一次 `categories.is_ff(type)` → `data[type.index_]`，一个数组读。

第 [537-543 行](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/newcelltypes.h#L537-L543) 的 `static_assert` 是编译期自检：`$and` 可求值且非触发器，`$dffsr` 是触发器——这些断言在编译时就验证了表的正确性。

**运行时封装 `NewCellTypes` 与全局 `yosys_celltypes`**

[kernel/newcelltypes.h:555-645](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/newcelltypes.h#L555-L645) —— `NewCellTypes` 把「内置单元走静态表」与「用户/设计自定义单元走 `unordered_map custom_cell_types`」统一在一个接口下。`cell_known`（[604](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/newcelltypes.h#L604)）= 先查静态位集，再查自定义表。

[kernel/newcelltypes.h:647](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/newcelltypes.h#L647) 声明、[kernel/yosys.cc:95](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/yosys.cc#L95) 定义全局唯一的 `yosys_celltypes`。它在 `yosys_setup()` 里被启用：

[kernel/yosys.cc:266](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/yosys.cc#L266) —— `yosys_celltypes.static_cell_types = StaticCellTypes::categories.is_known;`，即「所有内置单元都视为已知」。

**谁在用它**

RTLIL::Cell 的便捷方法直接委托给这个全局表，于是**任意 Pass 都能用统一的接口查询任意 cell 的端口方向**，不论它是内置 `$` 单元还是用户模块：

[kernel/rtlil.cc:4366-4385](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.cc#L4366-L4385) —— `Cell::known()` 先问 `yosys_celltypes.cell_known(type)`（命中静态表），否则看 design 里有没有同 type 的模块；`Cell::input(port)` 同理，内置单元走静态表，用户模块查模块端口的 `port_input` 属性。`Cell::output`、`Cell::port_dir` 在紧随其后（[4387-4402](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.cc#L4387-L4402)）。

这就是「单元类型登记」的最终意义：**把一份静态清单（constids.inc + 编译期表）变成一个全局查询服务，让 RTLIL 的每个 Cell 自带端口元信息。**

#### 4.3.3 源码精读：`$mux` 与 `$adff` 的新表登记（本讲指定查找点）

在新表里，`$mux` 登记在 `setup_internals_eval()`：

[kernel/newcelltypes.h:120-121](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/newcelltypes.h#L120-L121) —— `$mux $pmux $bwmux` 的端口是 `{ID::A, ID::B, ID::S} → {ID::Y}`，特征 `is_evaluable=true`。与旧表 [celltypes.h:142](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/celltypes.h#L142) 完全一致。

`$adff` 登记在 `setup_internals_ff()`：

[kernel/newcelltypes.h:141](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/newcelltypes.h#L141) —— `setup_type(ID($adff), {ID::CLK, ID::ARST, ID::D}, {ID::Q}, features)`，其中 `features.is_ff=true`。对照旧表 [celltypes.h:162](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/celltypes.h#L162)，端口一致。也就是说：`$adff` 有三个输入 `CLK`（时钟）、`ARST`（异步复位）、`D`（数据），一个输出 `Q`。

> 小贴士：在源码里搜一个 `$` 单元的端口，最省力的方式是直接在 `newcelltypes.h` 里 `Ctrl-F` 它的 `setup_type` 行；旧代码也可能用 `celltypes.h` 里的同名登记。两者端口定义一致，新表多了「特征位」。

#### 4.3.4 代码实践（源码阅读 + 跟踪调用链）

1. **目标**：验证「`Cell::input()` 的查询最终落到静态数组」。
2. **步骤**：
   - 读 [kernel/rtlil.cc:4375-4385](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.cc#L4375-L4385) 的 `Cell::input()`，看到它调用 `yosys_celltypes.cell_input(type, port)`。
   - 跟到 [kernel/newcelltypes.h:617-624](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/newcelltypes.h#L617-L624)，看到它对内置单元调用 `StaticCellTypes::port_info.inputs(type).contains(port)`。
   - 再看 [kernel/newcelltypes.h:421-442](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/newcelltypes.h#L421-L442)，确认 `inputs(type)` 取的是 `data[type.index_]`。
3. **观察**：整条链路没有任何 `dict::find`、没有字符串哈希；核心是一次数组下标。
4. **预期结果**：你能用一句话讲清「为什么 `cell->input(ID::CLK)` 对 `$adff` 返回 true、对 `$adff` 的 `Q` 返回 false」——因为编译期表里 `inputs[$adff.index_] = {CLK, ARST, D}`，`outputs[$adff.index_] = {Q}`。

### 4.4 三模块串联小结

把三个模块串起来，整条逻辑链是：

1. `constids.inc` 列出所有知名标识符（含单元名、端口名），按 ASCII 序排列；
2. 经 4 次 X-macro 展开，得到整数下标、`ID::` 编译期常量、有序查找表、全局字符串槽；
3. `newcelltypes.h` 用同样的 `ID::xxx` 常量，在**编译期**把每个单元的端口集填进「按下标索引的数组」；
4. 全局 `yosys_celltypes` 暴露查询接口；`RTLIL::Cell::known/input/output` 委托给它；
5. 于是任意 Pass 都能零开销地知道「这个 `$` 单元的端口长什么样」。

## 5. 综合实践

**任务**：用本讲学到的「查表能力」和 u3-l1 的「读 RTLIL 能力」，把一段含 `if` 的 Verilog 综合成内部单元，再逐一对照源码确认每个单元的端口。

我们直接用现成的 [examples/cmos/counter.v](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/examples/cmos/counter.v)（一个带同步复位 `rst` 和使能 `en` 的 3 位计数器，含 `if/else if`）。

1. **准备脚本**（示例代码，请自行保存为 `u3l4.ys`）：

   ```tcl
   # 示例脚本（非项目原有文件）
   read_verilog examples/cmos/counter.v
   hierarchy -top counter
   proc
   opt -purge
   write_rtlil u3l4_after_proc.rtlil
   stat
   ```

2. **运行**：

   ```bash
   ./build/yosys u3l4.ys
   ```

   如果尚未构建 yosys，请先按 u1-l2 完成 `cmake -B build . && cmake --build build`。

3. **观察 `write_rtlil` 输出**：打开 `u3l4_after_proc.rtlil`，找出综合后出现的 `$` 单元。对这个 `always @(posedge clk) if(rst)... else if(en)...` 结构，预期会出现：
   - 一个触发器单元（保存 `count`），类型可能是 `$dff`、`$sdff`、`$sdffe` 之一——因为 `rst`/`en` 都是同步条件；
   - 一个 `$add`（`count + 3'd1`）；
   - 一个 `$mux` 或 `$pmux`（由 `if/else if` 链翻译而来）。
   - **待本地验证**：具体落到哪个 `$dff` 变体，取决于 yosys 版本与 `proc`/`opt` 的简化结果；以你本地 `write_rtlil` 实际输出为准，不要假设。

4. **对照源码确认端口**：对你在输出里看到的每个 `$` 单元，回到本讲引用的源码点核对端口：
   - `$mux` → [celltypes.h:142](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/celltypes.h#L142) / [newcelltypes.h:120](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/newcelltypes.h#L120)，端口 `A/B/S → Y`；
   - `$add` → [celltypes.h:131](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/celltypes.h#L131)（在二元运算清单内），端口 `A/B → Y`；
   - `$dff`/`$sdff`/`$sdffe` → [celltypes.h:158-168](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/celltypes.h#L158-L168) / [newcelltypes.h:137-147](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/newcelltypes.h#L137-L147)，端口含 `CLK, D → Q`，`$sdff`/`$sdffe` 多 `SRST`/`EN`。

5. **进阶**：在 `counter.v` 里加一个 `case` 语句（例如把计数器改成按 `mode` 选择 `+1`/`-1`/`清零`），重新综合，观察多路分支是否会让 `$mux` 升级成 `$pmux`（[celltypes.h:142](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/celltypes.h#L142) 里 `$pmux` 与 `$mux` 同组，端口也是 `A/B/S → Y`，但 `B` 是多路拼接、`S` 位宽更大）。

6. **预期结果**：你能指着 `write_rtlil` 输出的每一行 `cell $xxx ...`，准确说出它的每个端口方向，并能在 `newcelltypes.h` 里找到对应的登记行——这就证明你真正掌握了「内部单元库」。

## 6. 本讲小结

- Yosys 的内部单元库是 RTLIL 的「字母表」：`$...` 是参数化高层单元（综合早中期），`$_..._` 是单位宽标准门（工艺映射后），前端产出它们、Pass 改写它们、后端消费它们。
- 典型端口约定：二元运算 `A/B → Y`，多路器 `A/B/S → Y`，触发器 `CLK/D(,/ARST,/EN) → Q`，存储器 `$mem` 端口最多。
- `constids.inc` 是按 ASCII 序排列的 X-macro 清单，被包含 4 次，分别生成 `StaticId` 枚举（整数下标）、`ID::` 编译期常量、有序 `IdTable`（供二分）、运行时全局字符串槽。
- `ID(...)` 宏在编译期二分查找：命中知名 id 则零开销返回 `constexpr`，否则退化为运行时构造——写 `ID($x)` 总是安全且尽量快。
- 单元类型登记有新旧两套：`CellTypes`（运行时 dict）与 `StaticCellTypes`/`NewCellTypes`（编译期按下标索引的数组 + 位集分类）；全局 `yosys_celltypes` 统一暴露，`Cell::known/input/output` 委托给它，让任意 Pass 零开销查询端口方向。
- 新增一个内部单元，只需在 `constids.inc` 插一行、在 `newcelltypes.h` 加一条 `setup_type`，无需改动任何中心注册代码。

## 7. 下一步学习建议

- **横向**：回到 u3-l1，试着用 `module->addCell` 手工构造一个 `$adff`，按本讲查到的端口（`CLK/ARST/D → Q`）接好线，感受「登记表」与「构造接口」如何配套。
- **纵向（推荐下一步）**：进入 u4 单元学习 Pass 系统。当你看到 `proc`（u6-l2）把 `always` 翻成 `$mux/$dff`、`opt`（u6-l3）合并冗余 `$and`、`techmap`（u6-l5）把 `$add` 拆成门时，本讲的单元库就是它们的「操作对象字典」。
- **深入阅读**：想了解 `$` 单元的完整语义（尤其是触发器各极性参数、`$mem` 的读端口时序），可读 [docs/source/yosys_internals/formats/rtlil_rep.rst](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/docs/source/yosys_internals/formats/rtlil_rep.rst) 与 [techlibs/common/simcells.v](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/common/simcells.v)（用 Verilog 给每个 `$` 单元下了行为定义，是另一份权威「手册」）。
