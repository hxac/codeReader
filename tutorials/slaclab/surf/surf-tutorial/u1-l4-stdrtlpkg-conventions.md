# VHDL 约定与 StdRtlPkg 基础类型

## 1. 本讲目标

SURF 是一个由上千个 VHDL 文件组成的共享库。如果每个文件都各写各的类型、各取各的名字、各定各的复位方式，整个仓库会无法维护。SURF 用两件事来保持一致性：一个公共的类型/函数包 `StdRtlPkg.vhd`，和一套贯穿全仓库的命名/复位约定（记录在 `AGENTS.md` 里）。

学完本讲，你应当能够：

- 说出 `sl`、`slv` 这两个别名的来历，并能说出 `log2`、`ite`、`bitSize` 等高频工具函数的用途。
- 看到一个 SURF 标识符，能凭后缀判断它的角色：`_G` 是泛型、`_C` 是常量、`Type` 是记录类型、`Array` 是数组、`_INIT_C` 是初值常量。
- 读懂 `TPD_G`、`RST_POLARITY_G`、`RST_ASYNC_G` 这三个复位/时序泛型的语义和默认值，并能看懂一个真实实体是如何声明它们的。

> ⚠️ 一个重要的准确性提示：本讲的标题里把这三个泛型和 `StdRtlPkg.vhd` 放在一起讲，但**它们并不定义在 `StdRtlPkg.vhd` 里**。`StdRtlPkg.vhd` 只提供 `sl`/`slv` 别名、工具函数和数组类型；这三个泛型是**全仓库的命名约定**，几乎在每个实体里都按相同的名字和默认值重复声明一遍。本讲第 4.3 节会把这个区别讲清楚——这是初学者最容易踩坑的地方。

## 2. 前置知识

在进入源码前，先用最朴素的语言理清几个 VHDL 概念：

- **`std_logic` 与 `std_logic_vector`**：VHDL 里表示一根线和一束线的标准类型。`std_logic` 可以取 `'0'`、`'1'`、`'Z'`（高阻）、`'X'`（未知）等 9 个值。`std_logic_vector` 是它的数组。
- **subtype（子类型）**：给一个已有类型起个新名字，并不创建新类型。SURF 用 `subtype sl is std_logic;` 给 `std_logic` 起了个短别名。
- **package（包）**：把类型、常量、函数集中放在一起，其他文件 `use` 一下就能复用，避免到处重复定义。`StdRtlPkg` 就是 SURF 最底层的公共包。
- **generic（泛型）**：在实体实例化时才确定值的"参数"，常用来传位宽、深度、时序参数。泛型在综合/仿真时是常量。
- **record（记录）**：把多个相关信号打包成一个结构体，类似 C 的 `struct`。SURF 大量用记录来表示总线接口（后续 AXI/AXI-Stream 讲义会反复见到）。
- **复位（reset）**：把电路强制回到已知状态。复位可以是"高有效"（`'1'` 触发）或"低有效"（`'0'` 触发），也可以是"同步"（要等时钟沿）或"异步"（不等时钟沿立刻生效）。

如果你对"包"和"泛型"还比较生疏，本讲会边看源码边巩固。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲用来讲什么 |
|------|------|----------------|
| [base/general/rtl/StdRtlPkg.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/general/rtl/StdRtlPkg.vhd) | SURF 最底层的公共包，全仓库几乎每个文件都 `use` 它 | `sl`/`slv` 别名、`log2`/`ite`/`bitSize` 工具函数、数组类型 |
| [AGENTS.md](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/AGENTS.md) | SURF 的"贡献者宪法"，记录仓库级约定 | `_G`/`_C`/`Type`/`Array`/`_INIT_C` 命名规则、复位泛型约定 |
| [base/general/rtl/Arbiter.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/general/rtl/Arbiter.vhd) | 一个典型的"教科书式"小模块（仲裁器） | 作为真实范例，展示三个复位泛型在一个实体里如何声明、如何被使用 |

`Arbiter.vhd` 不在本讲规格列出的关键源码里，但它是最干净的、能同时体现"`StdRtlPkg` 复用 + 命名约定 + 复位泛型"的真实实体，所以本讲拿它当活样本。下一讲 u1-l5 会专门用它来讲双进程风格。

## 4. 核心概念与源码讲解

### 4.1 StdRtlPkg 别名：sl/slv 与高频工具函数

#### 4.1.1 概念说明

`StdRtlPkg.vhd` 是 SURF 的"地基包"。它解决一个很现实的问题：`std_logic` 和 `std_logic_vector` 这两个名字太长了，全仓库每个端口、每个信号都要写一遍，既啰嗦又难读。于是包里先用 `subtype` 给它们起两个短别名 `sl`（single line，一根线）和 `slv`（vector，一束线）。此后整个 SURF 仓库统一用 `sl`/`slv` 而不是 `std_logic`/`std_logic_vector`。

除了别名，包里还集中了一批"写 VHDL 时反复要用、但标准库没有"的小工具函数，例如求对数向上取整、判断 2 的幂、单行 if-then-else、归约或/与/异或、Gray 码编解码、 ones 计数等。把这些收进公共包的好处是：**叶子模块不再各自重复实现**，行为也全仓库一致。

#### 4.1.2 核心流程

`StdRtlPkg` 的结构很典型，是一个"声明 + 包体"两段式 VHDL 包：

1. **`package ... is`（声明区）**：列出所有 subtype、type、constant 和 function/procedure 的**签名**（名字 + 参数 + 返回类型）。这是外部 `use` 时真正"看得到"的接口。
2. **`package body ... is`（包体）**：给出每个 function 的**具体实现**。

使用者只需要 `use surf.StdRtlPkg.all;`，就能拿到全部别名和函数，不必关心实现细节。

包里还有一个很巧的常量 `IN_SIMULATION_C`，用"综合工具会删掉、仿真器会保留"的 `pragma` 注释来区分当前是仿真还是综合：

```text
IN_SIMULATION_C := false      -- 综合时：translate_off 块被删除，只剩 false
              or true         -- 仿真时：pragma 被当注释忽略，false or true = true
IN_SYNTHESIS_C  := not(IN_SIMULATION_C)
```

这样代码里可以用 `if IN_SIMULATION_C then ...` 写只在仿真生效的调试逻辑。

#### 4.1.3 源码精读

**sl / slv 别名**——整个仓库用线的习惯就来自这两行：

[base/general/rtl/StdRtlPkg.vhd:30-32](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/general/rtl/StdRtlPkg.vhd#L30-L32)：`subtype sl is std_logic; subtype slv is std_logic_vector;`，注释直言"打 std_logic(_vector) 太烦人"。

**仿真/综合区分常量**：

[base/general/rtl/StdRtlPkg.vhd:22-28](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/general/rtl/StdRtlPkg.vhd#L22-L28)：用 `pragma translate_off/translate_on` 包住 `or true`，实现"仿真为真、综合为假"的常量。

**`log2`——按 2 的幂向上取整**：声明在 [StdRtlPkg.vhd:60](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/general/rtl/StdRtlPkg.vhd#L60)，实现在 [StdRtlPkg.vhd:752-758](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/general/rtl/StdRtlPkg.vhd#L752-L758)。它返回的是

\[
\text{log2}(n) = \lceil \log_2 n \rceil
\]

所以 `log2(5) = log2(8) = 3`。这在算"表示 N 个元素需要几位地址"时极其常用，例如 `slv(log2(DEPTH_G)-1 downto 0)`。

**`bitSize`——表示一个数需要几位**：声明在 [StdRtlPkg.vhd:62](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/general/rtl/StdRtlPkg.vhd#L62)，实现在 [StdRtlPkg.vhd:779-790](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/general/rtl/StdRtlPkg.vhd#L779-L790)。注意它对 2 的幂额外 +1：表示 `0..8` 这 9 个值需要 4 位，所以 `bitSize(8) = 4`。`Arbiter.vhd` 里 `selected : out slv(bitSize(REQ_SIZE_G-1)-1 downto 0)` 就是用它来算"选中队列号"需要几位。

**`ite`——单行 if-then-else**：有 10 个重载（布尔、sl、slv、integer、real、time……），声明在 [StdRtlPkg.vhd:117-126](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/general/rtl/StdRtlPkg.vhd#L117-L126)。以 `slv` 版为例，实现在 [StdRtlPkg.vhd:1136-1139](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/general/rtl/StdRtlPkg.vhd#L1136-L1139)：`ite(条件, 真值, 假值)`。它最大的用处是在常量声明里"按泛型选不同初值"，因为 VHDL 的常量声明不能直接写 `if`。

**数组类型**：包里声明了一大批 `natural range <>` 的非约束数组，[StdRtlPkg.vhd:36-41](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/general/rtl/StdRtlPkg.vhd#L36-L41) 是 `IntegerArray`/`NaturalArray` 等内建类型数组；此外还按位宽预声明了 `Slv1Array` … `Slv512Array`（如 [StdRtlPkg.vhd:397](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/general/rtl/StdRtlPkg.vhd#L397) 的 `Slv32Array`），让你能直接写"一个数组、每个元素是 32 位 slv"，而不必每次自己声明。

#### 4.1.4 代码实践

**实践目标**：亲手确认 `sl`/`slv` 的来历，并体会 `ite` 在常量声明里的作用。

**操作步骤**：

1. 打开 [base/general/rtl/StdRtlPkg.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/general/rtl/StdRtlPkg.vhd)，定位第 31–32 行，确认 `sl`/`slv` 就是 `std_logic`/`std_logic_vector` 的 subtype。
2. 在任意 SURF 实体（如 `Arbiter.vhd` 第 31 行）里，你会看到 `clk : in sl;`——它之所以能这样写，正是因为文件顶部 `use surf.StdRtlPkg.all;`（[Arbiter.vhd:21](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/general/rtl/Arbiter.vhd#L21)）。
3. 下面这段**示例代码**演示如何用 `ite` 根据泛型给常量选初值（不是仓库已有文件，仅供阅读理解）：

```vhdl
-- 示例代码：演示 ite 在常量声明中的用法
library surf;
use surf.StdRtlPkg.all;

entity DemoWidth is
   generic (
      WIDE_G : boolean := false);
end entity;

architecture rtl of DemoWidth is
   -- 若 WIDE_G 为真则 8 位，否则 4 位
   constant WIDTH_C : integer := ite(WIDE_G, 8, 4);
   signal data      : slv(WIDTH_C-1 downto 0);
begin
end architecture;
```

**需要观察的现象**：把 `WIDE_G` 改成 `true`/`false`，`data` 的位宽应在 8 和 4 之间切换。

**预期结果**：`ite` 让"按泛型选位宽"在一行内完成；若没有它，常量声明里无法写 `if`，只能拆到 architecture 里。

**待本地验证**：如需确认编译行为，可把它加进一个临时实体用 GHDL 语法分析（见 u1-l2 的 `make analysis`），本讲不假装已运行。

#### 4.1.5 小练习与答案

**练习 1**：`log2(1)`、`log2(5)`、`log2(8)` 分别返回几？
**答案**：`log2(1)=1`（number<2 时直接返回 1）、`log2(5)=3`、`log2(8)=3`。注意它是向上取整到 2 的幂。

**练习 2**：为什么 `bitSize(8)` 返回 4 而不是 3？
**答案**：`bitSize` 算的是"表示 `0..number` 这些值需要的位数"。`0..8` 共 9 个值，3 位只能表示 8 个值（`0..7`），不够，所以需要 4 位；实现里对 2 的幂特意 `+1`（[StdRtlPkg.vhd:784-785](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/general/rtl/StdRtlPkg.vhd#L784-L785)）。

---

### 4.2 命名后缀约定：_G / _C / Type / Array / _INIT_C

#### 4.2.1 概念说明

SURF 用**标识符后缀**来表达"这个东西是什么角色"。这是一种团队约定，写进 `AGENTS.md` 后，全仓库统一遵守。它的价值在于：你看到一个名字，不必跳到声明处，就知道它是泛型还是常量、是记录还是数组、是不是初值常量。这极大降低了阅读陌生 SURF 文件的认知负担。

这套约定和 4.1 节的 `StdRtlPkg` 是配合关系：包提供类型与函数，命名约定让全仓库用它们的方式保持一致。

#### 4.2.2 核心流程

约定一览（权威来源是 [AGENTS.md:30](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/AGENTS.md#L30) 与 [AGENTS.md:55](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/AGENTS.md#L55)）：

| 后缀 | 角色 | 示例 |
|------|------|------|
| `_G` | 泛型（generic），实例化时可改 | `TPD_G`、`REQ_SIZE_G` |
| `_C` | 常量（constant），编译期固定 | `WIDTH_C`、`REG_INIT_C` |
| `Type` | 记录类型（record） | `RegType`、`AxiLiteReadMasterType` |
| `Array` | 数组类型 | `Slv32Array`、`AxiLiteReadMasterArray` |
| `_INIT_C` | 某记录/类型的初值常量 | `REG_INIT_C`、`AXI_LITE_READ_MASTER_INIT_C` |

补充约定：

- **模块名 PascalCase**：实体名每个单词首字母大写，如 `AxiVersion`、`SimpleDualPortRam`。
- **常量前缀全大写**：导出常量通常带全大写前缀，如 `AXI_RESP_DECERR_C`、`PGP2B_RX_OUT_INIT_C`。
- **非约束数组**：需要在 lane/通道/主控数量上伸缩的数组，用 `natural range <>` 声明（[AGENTS.md:58](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/AGENTS.md#L58)）。

#### 4.2.3 源码精读

**命名约定的"法条"原文**：

[AGENTS.md:30](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/AGENTS.md#L30)：generics 用 `_G`、constants 用 `_C`、record 用 `Type`、模块名 PascalCase。

[AGENTS.md:55](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/AGENTS.md#L55)：记录类型用 `Type` 后缀、数组用 `Array` 后缀、初值常量用 `_INIT_C` 后缀，并给出 `Pgp2bRxOutType` / `Pgp2bRxOutArray` / `PGP2B_RX_OUT_INIT_C` 这组范例。

**在真实实体里看这套约定的全貌**——`Arbiter.vhd` 几乎是个命名约定标本：

- 泛型全部 `_G` 结尾：[Arbiter.vhd:26-29](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/general/rtl/Arbiter.vhd#L26-L29)，`TPD_G`、`RST_POLARITY_G`、`RST_ASYNC_G`、`REQ_SIZE_G`。
- 常量 `_C` 结尾：[Arbiter.vhd:42](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/general/rtl/Arbiter.vhd#L42)，`SELECTED_SIZE_C : integer := bitSize(REQ_SIZE_G-1);`——注意它直接复用了 4.1 节的 `bitSize`。
- 记录类型 `Type` 结尾：[Arbiter.vhd:44-48](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/general/rtl/Arbiter.vhd#L44-L48)，`type RegType is record ... end record;`。
- 初值常量 `_INIT_C` 结尾：[Arbiter.vhd:50-53](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/general/rtl/Arbiter.vhd#L50-L53)，`constant REG_INIT_C : RegType := (...)`。

#### 4.2.4 代码实践

**实践目标**：用"看后缀猜角色"的方式快速读一个陌生实体。

**操作步骤**：

1. 打开 [base/general/rtl/Arbiter.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/general/rtl/Arbiter.vhd)。
2. 不看声明类型，只看名字后缀，给下面每个标识符填角色（泛型/常量/记录类型/初值常量）：`REQ_SIZE_G`、`SELECTED_SIZE_C`、`RegType`、`REG_INIT_C`、`r`、`rin`。
3. 然后再核对第 42–56 行确认。

**需要观察的现象**：`r` 和 `rin` **没有**任何约定后缀——它们是普通信号，名字本身（register / register-in）就是约定，这一点 u1-l5 会展开。

**预期结果**：`_G`→泛型，`_C`→常量，`Type`→记录，`_INIT_C`→初值常量，`r`/`rin`→信号。

**待本地验证**：纯源码阅读型实践，无需运行。

#### 4.2.5 小练习与答案

**练习 1**：看到 `AXI_LITE_READ_MASTER_INIT_C`，仅凭名字能推断出什么？
**答案**：`_INIT_C` 说明它是个初值常量；前缀 `AXI_LITE_READ_MASTER` 暗示它是某个 `AxiLiteReadMasterType` 记录的初值。三者（`...Type` / `...Array` / `..._INIT_C`）通常成组出现。

**练习 2**：`Slv32Array`（[StdRtlPkg.vhd:397](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/general/rtl/StdRtlPkg.vhd#L397)）的名字透露了什么？
**答案**：`Array` 后缀说明它是数组类型；`Slv32` 说明每个元素是 32 位的 `slv`。

---

### 4.3 复位与时序泛型约定：TPD_G / RST_POLARITY_G / RST_ASYNC_G

#### 4.3.1 概念说明

本节是本讲最容易出错的地方，先把事实摆清楚：

- `TPD_G`、`RST_POLARITY_G`、`RST_ASYNC_G` **不在 `StdRtlPkg.vhd` 里**。在包里搜索这三个名字是搜不到的。
- 它们是**全仓库的命名约定**：几乎每个有时钟/复位的实体，都会在自己的 `generic` 区用**完全相同的三个名字和默认值**重新声明一遍。
- 这套约定由 [AGENTS.md:31](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/AGENTS.md#L31) 和 [AGENTS.md:88](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/AGENTS.md#L88) 钉死："修改 HDL 时要保留 `TPD_G`、`RST_POLARITY_G`、`RST_ASYNC_G` 的复位习惯"。

为什么不把它们放进包里？因为 VHDL 的泛型必须声明在**实体内部**，不能从一个包"继承"过来。所以 SURF 选择用"约定 + 复制"的方式：名字、类型、默认值都固定，每个实体照抄一遍，效果等同于"全仓库共用"。

三个泛型各自的含义：

| 泛型 | 类型 | 默认值 | 含义 |
|------|------|--------|------|
| `TPD_G` | `time` | `1 ns` | "传播延迟"，仅仿真有效；给寄存器赋值加 `after TPD_G`，让波形看得清时序。综合时被忽略 |
| `RST_POLARITY_G` | `sl` | `'1'` | 复位有效电平：`'1'`=高有效，`'0'`=低有效 |
| `RST_ASYNC_G` | `boolean` | `false` | 复位是否异步：`false`=同步复位，`true`=异步复位 |

#### 4.3.2 核心流程

约定要求：实体声明这三个泛型 → 在 `comb`（组合）进程里处理**同步**复位 → 在 `seq`（时序）进程里处理**异步**复位和 `after TPD_G`。整体逻辑如下：

```text
声明：generic(TPD_G; RST_POLARITY_G; RST_ASYNC_G; ...)
端口：rst : in sl := not RST_POLARITY_G   -- 复位端口默认"未生效"

comb 进程（算次态）：
   if (RST_ASYNC_G = false 且 rst = RST_POLARITY_G) then
       v := REG_INIT_C          -- 同步复位：复位生效时把次态清成初值

seq 进程（打寄存器）：
   if (RST_ASYNC_G 且 rst = RST_POLARITY_G) then
       r <= REG_INIT_C after TPD_G     -- 异步复位：不等时钟沿
   elsif rising_edge(clk) then
       r <= rin after TPD_G            -- 正常：上升沿把次态写入
```

要点：

- **`RST_ASYNC_G` 决定复位在哪条路径生效**：`false` 时复位走 `comb`（同步），`true` 时复位走 `seq`（异步）。两者互斥。
- **`rst = RST_POLARITY_G`** 这种写法让"复位是否生效"与极性无关：无论高有效还是低有效，只要 `rst` 等于设定极性就是"复位生效"。
- **`after TPD_G`** 只影响仿真波形，综合时被去掉，不产生真实电路。

> 本节只讲"这三个泛型是什么、怎么声明、怎么被消费"。至于 `comb`/`seq` 双进程的完整写法（`RegType`/`REG_INIT_C`/`r`/`rin`/`v := r`），是下一讲 u1-l5 的主题，这里先不展开。

#### 4.3.3 源码精读

**约定的权威出处**：

[AGENTS.md:31](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/AGENTS.md#L31)：要求在寄存器逻辑里沿用 `RegType`/`REG_INIT_C`/`r`/`rin`/`comb`/`seq`，并"保留 `TPD_G`、`RST_POLARITY_G`、`RST_ASYNC_G` 复位习惯"。

[AGENTS.md:88](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/AGENTS.md#L88)：修改代码时复位行为要与既有的 `TPD_G`、`RST_POLARITY_G`、`RST_ASYNC_G` 及默认值保持兼容。

**真实实体的标准声明**——`Arbiter.vhd` 的泛型区就是模板：

[base/general/rtl/Arbiter.vhd:26-29](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/general/rtl/Arbiter.vhd#L26-L29)：声明 `TPD_G : time := 1 ns`、`RST_POLARITY_G : sl := '1'`、`RST_ASYNC_G : boolean := false`。注意 `RST_POLARITY_G` 的类型是 `sl`——这正是 4.1 节那个别名在起作用（包里没定义这个泛型，但泛型的类型来自包）。

[base/general/rtl/Arbiter.vhd:32](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/general/rtl/Arbiter.vhd#L32)：复位端口 `rst : in sl := not RST_POLARITY_G;`，默认值取反极性意味着"不接复位时默认不生效"，而且复位是**可选**的。

**同步复位在 `comb` 里**：

[base/general/rtl/Arbiter.vhd:69-71](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/general/rtl/Arbiter.vhd#L69-L71)：`if (RST_ASYNC_G = false and rst = RST_POLARITY_G) then v := REG_INIT_C; end if;`——仅当配置为同步复位且复位生效时，把次态清零。

**异步复位与时钟沿在 `seq` 里**：

[base/general/rtl/Arbiter.vhd:80-87](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/general/rtl/Arbiter.vhd#L80-L87)：先判异步复位 `r <= REG_INIT_C after TPD_G`，再 `elsif rising_edge(clk) then r <= rin after TPD_G`。注意每条寄存器赋值都带 `after TPD_G`。

#### 4.3.4 代码实践

**实践目标**：亲手验证"这三个泛型不在包里，而在实体里"，并理解 `RST_POLARITY_G` 默认值的影响。这也是本讲规格里要求的实践（已按真实情况改写）。

**操作步骤**：

1. 在 [base/general/rtl/StdRtlPkg.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/general/rtl/StdRtlPkg.vhd) 中找到 `sl`、`slv` 的定义（第 31–32 行）。确认它们存在。
2. 在**同一个包**里搜索 `TPD_G`、`RST_POLARITY_G`、`RST_ASYNC_G`——你将搜不到，证明它们不是包里定义的。
3. 转到 [base/general/rtl/Arbiter.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/general/rtl/Arbiter.vhd) 第 27 行，找到 `RST_POLARITY_G : sl := '1';`，确认默认极性是**高有效**。
4. 写一个**最小实体声明**，复用 `StdRtlPkg` 的别名和这三个约定泛型（示例代码，非仓库已有文件）：

```vhdl
-- 示例代码：复用 StdRtlPkg 别名 + 三个复位/时序泛型约定
library ieee;
use ieee.std_logic_1164.all;

library surf;
use surf.StdRtlPkg.all;   -- 引入 sl/slv/log2/bitSize/ite ...

entity TinyReg is
   generic (
      TPD_G          : time    := 1 ns;   -- 约定：仿真传播延迟
      RST_POLARITY_G : sl      := '1';    -- 约定：'1'=高有效复位
      RST_ASYNC_G    : boolean := false;  -- 约定：false=同步复位
      WIDTH_G        : positive := 8);
   port (
      clk : in  sl;
      rst : in  sl := not RST_POLARITY_G;  -- 可选复位，默认不生效
      d   : in  slv(WIDTH_G-1 downto 0);
      q   : out slv(WIDTH_G-1 downto 0));
end entity TinyReg;
```

**需要观察的现象**：

- 第 2 步在包里搜不到这三个泛型，验证了"约定 ≠ 包定义"。
- 第 3 步 `RST_POLARITY_G` 默认 `'1'`，意味着若上层不覆盖，复位默认是高有效。
- 示例实体里 `rst` 端口默认值 `not RST_POLARITY_G`，当极性为 `'1'` 时默认值为 `'0'`（未复位）。

**预期结果**：你能够清晰地分辨"`StdRtlPkg` 提供什么"（sl/slv/函数）和"仓库约定要求每个实体自带什么"（三个复位/时序泛型）。

**待本地验证**：若想确认示例实体可编译，可用 u1-l2 的 GHDL 语法分析流程；本讲不假装已运行。

#### 4.3.5 小练习与答案

**练习 1**：如果某上层把 `RST_POLARITY_G` 设成 `'0'`、`RST_ASYNC_G` 设成 `true`，`Arbiter.vhd` 的复位行为会变成什么样？
**答案**：极性 `'0'` 表示低有效——`rst = '0'` 时复位生效；`RST_ASYNC_G = true` 让复位走 `seq` 进程的异步分支（[Arbiter.vhd:82-83](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/general/rtl/Arbiter.vhd#L82-L83)），即 `rst` 一变 `'0'` 立刻把 `r` 清成 `REG_INIT_C`，不等时钟沿。

**练习 2**：为什么 `TPD_G` 的默认值 `1 ns` 在真实 FPGA 上"不存在"？
**答案**：`time` 类型和 `after ...` 是纯仿真构造，综合工具会忽略它们。它只让仿真波形里寄存器输出比时钟沿晚一点点，便于观察因果，不对应任何真实硬件延迟。

**练习 3**：把这三个泛型"复制到每个实体"而不是放进包里，根本原因是什么？
**答案**：VHDL 的 generic 必须在实体内部声明，无法从包继承。SURF 因此用"固定名字 + 固定类型 + 固定默认值"的约定来模拟"全仓库共享泛型"。

---

## 5. 综合实践

把本讲三块内容串起来，完成一个小任务：**给一个计数器写一份"符合 SURF 约定"的实体骨架**。

要求：

1. `use surf.StdRtlPkg.all;`，端口全部用 `sl`/`slv` 而不是 `std_logic`/`std_logic_vector`。
2. 声明三个约定泛型 `TPD_G`/`RST_POLARITY_G`/`RST_ASYNC_G`，名字、类型、默认值与 `Arbiter.vhd` 一致；再加一个 `WIDTH_G : positive := 8`。
3. 用 `ite` 写一个常量 `CNT_MAX_C : integer := ite(WIDTH_G >= 16, 65535, 255);`，体会 4.1 节 `ite` 的用法。
4. 用 `log2` 或 `bitSize` 算出计数器位宽，声明 `count : slv(WIDTH_G-1 downto 0);` 类型的内部信号（位宽用工具函数而非硬编码）。
5. 声明 `type RegType is record ... end record;` 和 `constant REG_INIT_C : RegType := (...);`，命名严格遵循 `_C`/`Type`/`_INIT_C` 约定。
6. 复位端口写成 `rst : in sl := not RST_POLARITY_G;`。

**自检清单**：

- 在 `StdRtlPkg.vhd` 里能找到 `sl`/`slv`/`log2`/`bitSize`/`ite` 吗？（应能）
- 在 `StdRtlPkg.vhd` 里能找到 `TPD_G`/`RST_POLARITY_G`/`RST_ASYNC_G` 吗？（应**不能**——它们是实体级约定）
- 你的标识符后缀是否与 `AGENTS.md:30`、`AGENTS.md:55` 一致？

完成后，你的实体骨架就具备了 SURF 模块最基本的三件套：公共包复用、命名约定、复位/时序泛型约定。`comb`/`seq` 的具体实现留到 u1-l5。

## 6. 本讲小结

- `StdRtlPkg.vhd` 是 SURF 的地基包，提供 `sl`/`slv` 别名（[StdRtlPkg.vhd:31-32](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/general/rtl/StdRtlPkg.vhd#L31-L32)）和高频工具函数 `log2`/`bitSize`/`ite` 等，全仓库 `use` 它以避免重复造轮子。
- `IN_SIMULATION_C` 用 `pragma translate_off/on` 区分仿真与综合，是个理解"pragma 影响编译"的好例子。
- 命名后缀是 SURF 的角色约定：`_G`=泛型、`_C`=常量、`Type`=记录、`Array`=数组、`_INIT_C`=初值常量，模块名 PascalCase（[AGENTS.md:30](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/AGENTS.md#L30)、[AGENTS.md:55](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/AGENTS.md#L55)）。
- `TPD_G`/`RST_POLARITY_G`/`RST_ASYNC_G` **不在包里**，而是全仓库约定：每个实体按固定名字/类型/默认值（`1 ns`/`'1'`/`false`）重复声明（[Arbiter.vhd:26-29](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/general/rtl/Arbiter.vhd#L26-L29)），因为 VHDL 泛型无法从包继承。
- `RST_ASYNC_G` 决定复位走同步路径（`comb`）还是异步路径（`seq`），`rst = RST_POLARITY_G` 的写法让复位判断与极性解耦。
- `after TPD_G` 是纯仿真构造，综合时被忽略，只影响波形可读性。

## 7. 下一步学习建议

- **紧接着学 u1-l5（双进程 RTL 风格）**：本讲的 `RegType`/`REG_INIT_C`/`r`/`rin` 和三个复位泛型，正是 u1-l5 的主角。届时你会看到 `comb` 进程如何用 `variable v := r;` 算次态、`seq` 进程如何打寄存器，并完整理解 [Arbiter.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/general/rtl/Arbiter.vhd) 的实现。
- **带着本讲的"命名后缀"去浏览任意 `*Pkg.vhd`**：例如 `axi/axi-lite/rtl/AxiLitePkg.vhd`（u3-l1 会讲），你会看到成组的 `...Type` / `...Array` / `..._INIT_C`，本讲的约定就是读懂它们的基础。
- **回头对照 `AGENTS.md`**：本讲引用了 `AGENTS.md` 的第 30、31、55、88 行；建议通读一遍它的 "VHDL Conventions" 和 "VHDL Package Conventions" 两节，那是 SURF 全部 HDL 约定的总纲。
