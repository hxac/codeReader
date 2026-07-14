# 编码规范与阅读一个实体

## 1. 本讲目标

Open Logic 是一个由许多实体（entity）组成的库。在阅读任何一个实体的源码之前，先看懂全库共用的「编码规范」，会让后面的阅读事半功倍。

本讲学完后，你应该能够：

- 根据**命名后缀**（`_g`、`_c`、`_t`、`_v` 等）一眼判断一个标识符是泛型、常量、类型还是变量。
- 看懂 Open Logic 通用的 **AXI4-Stream（AXI-S）Valid/Ready 握手**和**同步高有效复位**写法，理解为什么复位要写在进程「末尾覆盖」而不是「开头分支」。
- 知道**可选端口与泛型都带默认值**这一约定，从而能快速实例化一个实体。
- 拿到一个标准 Open Logic 实体文件后，按「文件结构」快速定位版权、描述、库、实体、架构各段。

本讲全程以 `olo_base_pl_stage.vhd`（带反压的流水线寄存器）为真实样本，对照 `doc/Conventions.md` 逐条印证。

## 2. 前置知识

在进入本讲前，建议你已经了解（详见 u1-l1、u1-l2）：

- **VHDL**：用来描述数字硬件的语言。一个 `entity` 描述对外的「端口」，一个 `architecture` 描述内部「怎么连/怎么算」。
- **泛型（Generic）**：实例化时才确定值的参数（如总线宽度、流水线级数），相当于硬件的「编译期常量」。
- **寄存器与时钟**：时序逻辑在时钟上升沿更新；`process(Clk)` + `if rising_edge(Clk)` 是写寄存器的标准骨架。
- **区域（area）**：Open Logic 把源码分成 base / axi / intf / fix 四区，实体名形如 `olo_<area>_<function>`。

本讲还会用到两个新术语，先建立直觉：

- **握手（Handshake）**：发送方拉高 `Valid` 表示「数据有效」，接收方拉高 `Ready` 表示「我准备好收」，二者在同一时钟沿同时为高，数据才算成功传递一次。这就是 AXI4-Stream（简称 AXI-S）的 `TVALID/TREADY` 约定。
- **反压（Back-pressure）**：当下游来不及处理时，下游把 `Ready` 拉低，上游就必须把数据「按住」不丢，这叫反压。

## 3. 本讲源码地图

本讲涉及两个关键文件：

| 文件 | 作用 |
| --- | --- |
| [doc/Conventions.md](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/Conventions.md) | 全库统一的编码规范文档：命名后缀、AXI-S 握手、复位写法、默认值、TDM、字节使能等。本讲的「规则来源」。 |
| [src/base/vhdl/olo_base_pl_stage.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pl_stage.vhd) | 一个带 AXI-S 反压的流水线寄存器，是 base 区最常用的「打断时序路径」构件。本讲的「规范样本」。 |

补充参考（理解样本里引用的常量来源）：

| 文件 | 作用 |
| --- | --- |
| [src/base/vhdl/olo_base_pkg_attribute.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pkg_attribute.vhd) | 把跨工具的综合属性（如 `shreg_extract`）封装成统一常量，`olo_base_pl_stage` 里带 `_c` 后缀的标识符大多来自这里。 |

## 4. 核心概念与源码讲解

### 4.1 命名规范

#### 4.1.1 概念说明

一个大库会被编译进同一个 VHDL 库（Open Logic 约定库名为 `olo`），用户代码也可能编进同一个库。如果没有统一的命名规则，名字很容易撞车。Open Logic 用**后缀**来标注每个标识符的「身份」，让你不点开声明也能猜出它是泛型、常量、类型还是变量。

#### 4.1.2 核心流程

各标识符的命名规则一览（规则均来自 Conventions.md）：

| 类别 | 规则 | 例子 |
| --- | --- | --- |
| 实体 | `olo_<area>_<function>` | `olo_base_pl_stage`、`olo_base_fifo_async` |
| 端口 | `<接口>_<信号>`，**不加** `_i/_o` 方向后缀 | `In_Data`、`Out_Ready` |
| 函数 | lowerCamelCase | `binaryToGray` |
| 常量 | `_c` 后缀 | `DataWidth_c` |
| 泛型 | `_g` 后缀 | `Width_g` |
| 变量 | `_v` 后缀 | `IsStuck_v` |
| 类型 | `_t` 后缀 | `Data_t` |
| FSM 类型 | `<标识符>Fsm_t`，状态值带 `_s` 后缀 | `CtrlFsm_t` / `Idle_s` |
| 验证组件（VC） | `snail_case`，无强制后缀 | `axi_stream_master_t` |

另外的格式约定：缩进用 **4 个空格**（不用 Tab）、文件末尾不留多余空格；强调用下划线 `_`，无序列表用短横线 `-`。

> 小贴士：验证组件（VC）位于 `test/tb`，沿用 VUnit 的 snail_case 风格，是为了能和 VUnit 原生组件混用。得益于 VHDL 大小写不敏感，在测试台里实例化 VC 时仍写成 Open Logic 风格（`Some_Port => …`），linter 也不会报错。

#### 4.1.3 源码精读

打开 `olo_base_pl_stage.vhd`，最顶部的实体声明就是命名规范的活教材。

泛型全部带 `_g` 后缀，且后两个给了默认值：

[src/base/vhdl/olo_base_pl_stage.vhd:34-38](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pl_stage.vhd#L34-L38) —— 三个泛型 `Width_g`、`UseReady_g`、`Stages_g`，后缀 `_g` 即「generic」。

端口采用 `<接口>_<信号>` 形式，输入接口前缀 `In_`、输出接口前缀 `Out_`，且**没有任何 `_i/_o` 方向后缀**，方向由 `in/out` 关键字表达：

[src/base/vhdl/olo_base_pl_stage.vhd:39-51](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pl_stage.vhd#L39-L51) —— `In_Valid`/`In_Ready`/`In_Data` 与 `Out_Valid`/`Out_Ready`/`Out_Data`。

类型用 `_t` 后缀。下方定义了一个数组类型用于串联多级流水线：

[src/base/vhdl/olo_base_pl_stage.vhd:78-78](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pl_stage.vhd#L78-L78) —— `type Data_t is array (natural range <>) of std_logic_vector(...)`。

常量用 `_c` 后缀。本文件里最典型的 `_c` 标识符其实是「综合属性常量」，来自公共包 `olo_base_pkg_attribute`，用于跨厂商统一行为：

[src/base/vhdl/olo_base_pl_stage.vhd:249-253](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pl_stage.vhd#L249-L253) —— `ShregExtract_SuppressExtraction_c`、`SynSrlstyle_FlipFlops_c` 等，均带 `_c`。它们在 [src/base/vhdl/olo_base_pkg_attribute.vhd:35-65](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pkg_attribute.vhd#L35-L65) 中以 `constant … _c` 形式定义。

对应的规则文本在 Conventions.md：

[doc/Conventions.md:34-48](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/Conventions.md#L34-L48) —— 常量 `_c`、泛型 `_g`、变量 `_v`、类型 `_t` 的成文规定。

#### 4.1.4 代码实践

> **源码阅读型实践：在样本里「盖章」。**

1. **目标**：把 Conventions.md 的每条命名规则，落到 `olo_base_pl_stage.vhd` 的具体行号上。
2. **步骤**：
   - 在 `olo_base_pl_stage.vhd` 中找出 3 个带 `_g` 后缀的泛型（提示：34–38 行）。
   - 找出带 `_c` 后缀的常量（提示：249–266 行，注意它们来自哪个 `use` 的包，见 138 行）。
   - 找出带 `_t` 后缀的类型（提示：78 行）和一个带 `_v` 后缀的变量（提示：185–186 行的 `p_comb` 进程里）。
3. **观察**：注意端口没有 `_i/_o` 后缀，方向只靠 `in/out` 关键字体现。
4. **预期结果**：你会得到一张「规则 → 行号」对照表，证明这个实体严格遵循了命名规范。
5. 结果标注：待本地核对行号是否与上方一致（GitHub 链接已锚定到当前 HEAD）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 Open Logic 的端口不加 `_i/_o` 方向后缀？
**答案**：方向已经由 entity 声明里的 `in`/`out` 关键字明确表达，再加后缀是冗余；去掉后缀还能让同一信号在端口表里更短、更易读（见 Conventions.md 第 27–28 行）。

**练习 2**：FSM 的状态值为什么要带 `_s` 后缀，而非 FSM 的枚举值却不带？
**答案**：`_s` 表示「state（状态）」，专门标注 FSM 的各个状态；非 FSM 的枚举值只是「取值」，不是状态机状态，因此不混淆使用 `_s`（见 Conventions.md 第 50–55 行）。

---

### 4.2 握手与复位约定

#### 4.2.1 概念说明

Open Logic 凡是需要「数据传递」的地方，都采用工业界事实标准的 **AXI4-Stream 握手**：`Valid`（对应 `TVALID`）+ `Ready`（对应 `TREADY`）。需要注意的是，**数据信号不一定叫 `TData`**，会按功能取名（本实体里就叫 `In_Data`/`Out_Data`）。

复位方面，Open Logic 有三条强约定：

1. **同步复位**（在时钟沿生效），**高有效**（`'1'` 表示复位）。
2. **只复位有状态的寄存器**；纯流水线寄存器不复位，以降低复位信号的扇出（fanout）。
3. **复位写成进程末尾的「覆盖」**，而不是进程开头的 `if` 分支。

#### 4.2.2 核心流程

AXI-S 握手一次成功传递的充要条件：

\[ \text{传递成功} \iff (\text{Valid} = 1) \land (\text{Ready} = 1) \;\text{在同一个上升沿} \]

两种复位写法的对比（这是本讲最重要的反例）：

```
推荐（覆盖写法）                      不推荐（开头分支写法）
process(Clk)                          process(Clk)
begin                                 begin
  if rising_edge(Clk) then              if rising_edge(Clk) then
    A <= x;                               if Rst='1' then
    B <= y;                                 A <= '0';
    if Rst = '1' then     -- 末尾覆盖     else
       A <= '0';                              A <= x;
    end if;                                   B <= y;
  end if;                                  end if;
end;                                    end if;
                                      end;
A: 带复位的 D 触发器                   A: 带复位的 D 触发器
B: 不带复位的 D 触发器                 B: 以 Rst 为使能的 D 触发器（扇出被拉高）
```

为什么覆盖写法更好？在「覆盖」写法里，`A` 综合成「带复位的 D 触发器」、`B` 综合成「普通 D 触发器」，复位线只连到 `A`。而在「开头分支」写法里，`B` 会被综合成「以 `Rst` 为时钟使能（clock enable）的 D 触发器」，复位信号被迫连到 `B`，**复位扇出不必要地变大**，时序和布线都更差。

#### 4.2.3 源码精读

握手约定来自规范文档：

[doc/Conventions.md:131-135](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/Conventions.md#L131-L135) —— 凡需握手的接口都用 `Valid`/`Ready`，数据信号按功能命名。

复位约定（同步、高有效、只复状态寄存器、末尾覆盖）：

[doc/Conventions.md:138-151](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/Conventions.md#L138-L151) —— 规则正文；推荐/不推荐写法示例见 [doc/Conventions.md:155-187](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/Conventions.md#L155-L187)。

在 `olo_base_pl_stage` 的「无反压」分支里，能看到最干净的复位覆盖写法，同时印证「只复状态寄存器」：

[src/base/vhdl/olo_base_pl_stage.vhd:270-279](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pl_stage.vhd#L270-L279) —— 先 `DataReg <= In_Data; VldReg <= In_Valid;`，再在末尾用 `if Rst = '1' then VldReg <= '0';` 覆盖。结果：`VldReg`（有效位，状态）带复位，`DataReg`（纯数据流水线）**不带**复位，正好符合「只复状态」。

带反压分支里的复位覆盖（两进程法的时序进程）：

[src/base/vhdl/olo_base_pl_stage.vhd:229-239](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pl_stage.vhd#L229-L239) —— 先 `r <= r_next;` 把整组寄存器更新，再在末尾 `if Rst = '1'` 只覆盖三个有状态的域（`DataMainVld`、`DataShadVld`、`In_Ready`），其余域不复位。

握手端口本身（注意 `In_Data` 不叫 `TData`）：

[src/base/vhdl/olo_base_pl_stage.vhd:43-50](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pl_stage.vhd#L43-L50) —— `In_Valid`/`In_Ready`/`In_Data` 与 `Out_Valid`/`Out_Ready`/`Out_Data` 成对的 AXI-S 接口。

#### 4.2.4 代码实践

> **源码阅读型实践：定位两种复位覆盖写法。**

1. **目标**：在样本里找到「末尾覆盖」的复位写法，并验证它符合「只复状态寄存器」。
2. **步骤**：
   - 打开 270–279 行（`g_nordy` 分支的 `p_stg` 进程），确认 `DataReg` 不在复位覆盖列表里、`VldReg` 在。
   - 打开 229–239 行（`g_rdy` 分支的 `p_seq` 进程），列出被复位覆盖的字段名。
3. **观察**：被复位的字段是否都代表「状态」（有效位、就绪位），而纯数据是否都被跳过。
4. **预期结果**：两处都符合「末尾覆盖 + 只复状态」的约定；`DataReg`/`r.DataMain` 这类纯数据通路不被复位。
5. 结果标注：待本地验证（可用综合工具查看复位扇出，或读波形确认复位时数据通路是否被清零——预期**不被清零**）。

#### 4.2.5 小练习与答案

**练习 1**：如果系统给的是**低有效**复位（`Rst_n`），该怎么接到 Open Logic 实体？
**答案**：在实体**外部**先把信号反相成高有效 `Rst <= not Rst_n;`，再接到 `Rst` 端口。Open Logic 内部一律按高有效处理（见 Conventions.md 第 145–146 行）。

**练习 2**：为什么选同步复位而不是异步复位？
**答案**：复位信号可能由普通逻辑产生（含毛刺），同步复位只在时钟沿采样，天然抗毛刺；此外它还允许「在正常工作中用复位冲洗 FIFO」这类用法（见 Conventions.md 第 141–143 行）。

---

### 4.3 默认值与可选端口

#### 4.3.1 概念说明

Open Logic 的设计哲学之一是「Ease of Use」（见 u1-l1）：**所有可选的泛型和端口都带默认值**。这意味着如果你不需要某个可选功能，完全可以「装作它不存在」——不必去查该填什么值，也不必在实例化时把它逐个列出。VHDL 会自动用默认值兜底。

#### 4.3.2 核心流程

带默认值的端口/泛型，在实例化时可以省略 `port map` / `generic map` 的对应项。判断「可选」的方法很简单：

- 泛型声明形如 `Name_g : <类型> := <默认值>` → 可选，不写就用默认值。
- 端口声明形如 `Name : in <类型> := <默认值>` → 可选输入，不连就用默认值。

在 `olo_base_pl_stage` 中，下面这些都可以省略：

| 标识符 | 默认值 | 含义 | 省略后的效果 |
| --- | --- | --- | --- |
| `UseReady_g` | `true` | 是否实现 Ready 反压 | 默认带反压 |
| `Stages_g` | `1` | 流水线级数 | 默认 1 级 |
| `In_Valid` | `'1'` | 输入恒有效 | 上游无握手，数据总是有效 |
| `Out_Ready` | `'1'` | 输出恒就绪 | 下游无握手，总是能收 |

#### 4.3.3 源码精读

规则原文：

[doc/Conventions.md:189-192](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/Conventions.md#L189-L192) —— 所有可选泛型与端口都有默认值。

样本中的可选泛型：

[src/base/vhdl/olo_base_pl_stage.vhd:35-37](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pl_stage.vhd#L35-L37) —— `UseReady_g : boolean := true` 与 `Stages_g : natural := 1` 都带默认值（只有 `Width_g : positive` 无默认值，是必填）。

样本中的可选端口：

[src/base/vhdl/olo_base_pl_stage.vhd:44-49](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pl_stage.vhd#L44-L49) —— `In_Valid : in std_logic := '1'` 与 `Out_Ready : in std_logic := '1'` 都带默认值；因此可以把它当成「纯组合逻辑打断器」使用：上下游都不接握手，只接 `Clk`/`Rst`/`In_Data`/`Out_Data` 即可工作。

一个最小实例化（示例代码，非项目原文件）：

```vhdl
-- 示例代码：把 pl_stage 当一拍纯数据寄存器用，省略所有可选项
s : entity olo.olo_base_pl_stage
    generic map ( Width_g => 16 )          -- 其余泛型走默认值
    port map (
        Clk => Clk, Rst => Rst,
        In_Data => din, Out_Data => dout
        -- In_Valid/Out_Ready 走默认 '1'，不接握手
    );
```

#### 4.3.4 代码实践

> **配置/实例化型实践：用最少连线让实体跑起来。**

1. **目标**：体会「默认值」如何简化实例化。
2. **步骤**：
   - 写一个最小顶层，按上面示例代码实例化 `olo_base_pl_stage`，只接 `Clk`/`Rst`/`In_Data`/`Out_Data`，并给 `Width_g => 8`。
   - 让 `In_Data` 每拍自增，观察 `Out_Data` 是否延迟一拍。
3. **观察**：因为 `In_Valid` 默认 `'1'`、`Out_Ready` 默认 `'1'`，数据应每拍顺利通过，仅多一拍延迟。
4. **预期结果**：`Out_Data` = 上一拍的 `In_Data`，功能等同一个普通数据寄存器。
5. 结果标注：待本地仿真验证（可参照第 4.4 节给出的测试台运行方式）。

#### 4.3.5 小练习与答案

**练习 1**：`Width_g` 为什么没有默认值？
**答案**：数据宽度因设计而异，不存在「合理的通用默认值」，所以它被声明为 `Width_g : positive`（无默认值），是**必填**泛型。

**练习 2**：把 `UseReady_g` 设成 `false` 会进入哪段代码？
**答案**：会进入 `g_nordy`（`not UseReady_g`）分支（244–285 行），该分支不实现 Ready 反压，`In_Ready` 恒为 `'1'`。

---

### 4.4 实体文件结构（含两进程法与 shadow 寄存器）

#### 4.4.1 概念说明

一个标准的 Open Logic 实体文件，从上到下通常分成清晰的几段：版权头 → 描述（含文档链接）→ 库与 use → 实体声明 → 架构。有些文件还会把一个**私有辅助实体**（以 `olo_private_` 开头）放在同一文件里，仅供本文件的公开实体实例化。

`olo_base_pl_stage` 内部用了 **两进程法（two-process method）** 来实现带反压的流水线寄存器。这是 Open Logic 中复杂时序块常用的写法，理解它就理解了「shadow 寄存器」为何存在。

#### 4.4.2 核心流程

文件结构骨架（以 `olo_base_pl_stage.vhd` 为例）：

```
1.  版权头注释          (1-5 行)
2.  描述 + 文档链接      (7-19 行)
3.  库与 use 子句        (21-28 行)
4.  公开实体 olo_base_pl_stage      (33-52 行)
5.  公开架构 rtl（多级 generate）   (57-126 行)
6.  第二组库与 use 子句（含 olo_base_pkg_attribute） (128-138 行)
7.  私有实体 olo_private_pl_stage_single (单级实现)  (143-161 行)
8.  私有架构 rtl（两进程法 + 无反压分支） (166-287 行)
```

**两进程法**的核心思想：把一个时序块的全部寄存器收进一个 `record`（这里叫 `TwoProcess_r`），用两个进程协作：

- `p_comb`（组合进程，`process(all)`）：根据当前状态 `r` 和输入，算出下一状态 `r_next`。所有「下一拍会怎样」的逻辑都写在这里，先 `v := r;`（默认保持），再按条件修改 `v`。
- `p_seq`（时序进程，`process(Clk)`）：在上升沿把 `r <= r_next`，并在末尾做复位覆盖。

**shadow（影子）寄存器为什么必要？**

带反压时，`In_Ready` 是一个**寄存器输出**，意味着「这一拍决定拉低，下一拍才生效」。考虑这个冲突时刻：

- 主寄存器 `DataMain` 里有数据且有效（`DataMainVld=1`）；
- 下游不收（`Out_Ready=0`），所以 `DataMain` 里的数据「卡住」无法交给下游；
- 而这一拍 `In_Ready` 还没来得及拉低，上游可能正好送来一个新数据。

如果新数据没地方放，就会丢。`DataShad`（影子寄存器）就是为这一拍准备的「缓冲位」：把卡住期间到达的数据存进影子寄存器，同时把 `In_Ready` 拉低，阻止后续数据；等下游恢复接收、`DataMain` 腾空后，影子寄存器里的数据再「补位」进 `DataMain`。这样既不丢数据，又只多用了一个寄存器的开销。

「卡住」判定（见源码 192 行）：

\[ \text{IsStuck} \iff (\text{DataMainVld}=1) \land (\text{Out\_Ready}=0) \land (\text{In\_Valid}=1 \lor \text{DataShadVld}=1) \]

#### 4.4.3 源码精读

文件各段的位置（结构地图）：

- 版权头与描述：[src/base/vhdl/olo_base_pl_stage.vhd:1-19](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pl_stage.vhd#L1-L19)
- 公开实体 `olo_base_pl_stage`：[src/base/vhdl/olo_base_pl_stage.vhd:33-52](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pl_stage.vhd#L33-L52)
- 多级展开（用 `for…generate` 把单级实体串成 `Stages_g` 级）：[src/base/vhdl/olo_base_pl_stage.vhd:88-117](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pl_stage.vhd#L88-L117)
- 零级直通（`Stages_g = 0` 时不建寄存器，直接连线）：[src/base/vhdl/olo_base_pl_stage.vhd:120-124](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pl_stage.vhd#L120-L124)
- 私有单级实体 `olo_private_pl_stage_single`：[src/base/vhdl/olo_base_pl_stage.vhd:143-161](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pl_stage.vhd#L143-L161)

两进程法的 record（注意它用 `_r` 后缀，是两进程法承载「寄存器组」的传统写法）：

[src/base/vhdl/olo_base_pl_stage.vhd:169-177](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pl_stage.vhd#L169-L177) —— `TwoProcess_r` 含主/影两组数据及其有效位，再加 `In_Ready`；声明 `r, r_next : TwoProcess_r`。

组合进程先判定「卡住」，再处理输出交接，再决定新数据落主寄存器还是影子寄存器：

[src/base/vhdl/olo_base_pl_stage.vhd:192-192](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pl_stage.vhd#L192-L192) —— `IsStuck_v` 的判定式。

[src/base/vhdl/olo_base_pl_stage.vhd:201-212](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pl_stage.vhd#L201-L212) —— 新数据到达时：若卡住则存入 `DataShad`（影子），否则直接进 `DataMain`（主）。这正是「shadow 寄存器防止丢数据」的关键 12 行。

时序进程 + 复位覆盖：

[src/base/vhdl/olo_base_pl_stage.vhd:229-239](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pl_stage.vhd#L229-L239) —— `r <= r_next;` 后用 `if Rst='1'` 覆盖三个状态域。

#### 4.4.4 代码实践

> **源码阅读型实践：复述 shadow 寄存器的一生。**

1. **目标**：用样本里的行号，把 shadow 寄存器「存入—补位」的完整路径串起来。
2. **步骤**：
   - 找到 `IsStuck_v` 的判定式（192 行），列出它为真的三个条件。
   - 找到「卡住时新数据存影子」的分支（204–207 行）。
   - 找到「主寄存器腾空后，影子补位到主」的交接逻辑（195–199 行：`v.DataMain := r.DataShad` 等）。
   - 找到复位时影子有效位被清零（235 行 `r.DataShadVld <= '0'`）。
3. **观察**：在没有 shadow 的情况下，`IsStuck` 那一拍到达的数据会落到哪里？结论是——无处可落，会丢失。
4. **预期结果**：你能用一句话讲清 shadow 的作用：「卡住期间到达的那一拍数据，先进 shadow，等主寄存器腾出再补位，从而不丢数据、且只多花一个寄存器」。
5. 结果标注：待本地用波形验证（建议在下游长时间拉低 `Out_Ready` 期间，持续给 `In_Valid`，观察 `DataShadVld` 出现脉冲）。

#### 4.4.5 小练习与答案

**练习 1**：为什么公开实体 `olo_base_pl_stage` 和私有实体 `olo_private_pl_stage_single` 能放在同一个 `.vhd` 文件里？
**答案**：VHDL 允许一个文件含多个设计单元；私有实体以 `olo_private_` 前缀表明「仅供内部使用」，公开实体通过 `component` 声明（60–75 行）实例化它，并用 `for…generate` 串成多级。

**练习 2**：`Stages_g = 0` 时还会消耗寄存器吗？
**答案**：不会。`g_zero` 分支（120–124 行）是纯组合直通（`Out_Data <= In_Data` 等），不生成任何寄存器，等于一根导线。

---

## 5. 综合实践

把本讲四个模块串起来，完成一次**规范审计 + 行为理解**的小任务：

1. **审计命名**：打开 `olo_base_pl_stage.vhd`，建一张三列表格「标识符 / 后缀类别（`_g`/`_c`/`_t`/`_v`/端口）/ 行号」，至少填入 8 行，并核对每条都符合 Conventions.md。
2. **审计复位**：在文件里找出**所有** `if Rst = '1' then` 覆盖块（提示有两处：235–237 行附近、275–277 行附近），逐一确认它们都在进程**末尾**，且只覆盖状态类寄存器，纯数据通路未被复位。
3. **审计默认值**：列出实体里所有带 `:= ` 默认值的泛型和端口，说明「省略它们后实体分别退化成什么」（例如省略 `Out_Ready` 即下游恒就绪）。
4. **理解行为**：用一段话解释 shadow 寄存器，并在解释中引用 192、204–207、195–199 三处行号作为证据。
5. **（可选）跑测试台**：本项目为该实体提供了 VUnit 测试台 [test/base/olo_base_pl_stage/olo_base_pl_stage_tb.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/base/olo_base_pl_stage/olo_base_pl_stage_tb.vhd)，它用 `axi_stream_master_t`/`axi_stream_slave_t` 验证组件并支持 `RandomStall_g`（随机反压）。按 u1-l4 的方式运行它，随机反压下若全绿，就间接证明了 shadow 寄存器确实没让数据丢失。

> 结果标注：第 1–4 步可在阅读中完成并填表；第 5 步的仿真结果为「待本地验证」。

## 6. 本讲小结

- Open Logic 用**后缀**标注标识符身份：泛型 `_g`、常量 `_c`、类型 `_t`、变量 `_v`、端口无方向后缀；实体统一为 `olo_<area>_<function>`。
- 数据传递一律用 **AXI-S 的 Valid/Ready 握手**，但数据信号按功能命名（如 `In_Data`）。
- 复位是**同步、高有效**，且写成**进程末尾的覆盖**而非开头分支，并**只复位状态寄存器**，目的是降低复位扇出。
- **所有可选泛型/端口都带默认值**，不需要的功能可直接省略，降低使用门槛。
- 一个标准实体文件按「版权/描述/库/实体/架构」分段；复杂时序块常用**两进程法**，用 record 收纳寄存器、用 shadow 寄存器吸收「反压卡住瞬间」的数据，做到不丢数据。
- linter（VSG）会强制执行这些规范——PR 中所有 lint 的 error 与 warning 都必须清零。

## 7. 下一步学习建议

- **进入 base 区的具体构件**：本讲的 `olo_base_pl_stage` 是后续多讲的基础。建议下一站读 u2-l2「流水线阶段与 AXI-S 握手」，会更深入地分析两进程法与 shadow 寄存器的时序细节。
- **先补齐公共包**：若你对 `use work.olo_base_pkg_attribute.all` 这类依赖好奇，可先读 u2-l1「base 包体系」，了解 math/logic/array/string/attribute 五个包提供了哪些「积木」。
- **想动手写一个规范实体**：对照 [doc/HowTo.md](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/HowTo.md) 配置好 linter，按本讲的文件结构骨架从零写一个小实体，再用 linter 清掉所有告警，是巩固规范最快的方式。
