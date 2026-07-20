# 用 FDPE 触发器存储固件编译日期

## 1. 本讲目标

本讲聚焦 fpga_base 最有特色的一个机制：**如何把"这次固件是什么时候编译的"这一信息，永久烧进 FPGA 配置比特流里**。

学完后你应当能够：

- 说清楚为什么用一个真实的 Xilinx 触发器（FDPE）来存一位日期，而不是用 VHDL 常量或普通信号。
- 解释 `dont_touch` 属性为什么是这套机制的"救命稻草"——没有它，整段日期逻辑会被综合器优化得无影无踪。
- 读懂 `g_generics` 与 `g_ngenerics` 两条 `generate` 分支，并指出它们分别对应"脚本写 generic"和"TCL 综合 hook 写 INIT"两种注入方式。

本讲是 u3 单元（版本与编译时间机制）的第一篇，只精读 [`hdl/fpga_base_date_package.vhd`](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_date_package.vhd) 这个日期存储组件，以及顶层 [`hdl/fpga_base_v1_0.vhd`](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_v1_0.vhd) 中实例化它的那一小段。下一篇 u3-l2 会接着讲那条 TCL hook 是怎么把每一位写进 FDPE 的 `INIT` 的。

## 2. 前置知识

在进入源码前，先用大白话理清三个概念。

### 2.1 为什么要"存编译时间"

在 PSI 这类大型实验装置里，一块 FPGA 板子上跑的固件（bitstream）会被反复编译、反复刷写。一旦现场出问题，工程师第一个要问的就是："**这块板子上现在跑的，到底是哪一次编译出来的固件？**"

如果固件里能"自带出生证"——一个读出来就是"年/月/日/时/分"的寄存器——那么排查时只要用软件或 JTAG 把它读出来，就能立刻对上版本。这正是 fpga_base 在寄存器偏移 `0x04 ~ 0x14`（即寄存器下标 1~5）提供的"固件编译日期时间"。回顾 u2-l3 的寄存器映射：这五个寄存器是**只读**的，因为它们的值不在运行期产生，而是在**编译期**就被焊死进了硬件。

难点在于：VHDL 是静态硬件描述语言，它本身不知道"现在几点"。需要一个外部动作（脚本）在编译流水线的某个环节，把当前时间"塞进"硬件。本讲讲的就是**塞进来之后，硬件这一侧用什么容器把它装住**。

### 2.2 FDPE 是什么

`FDPE` 是 Xilinx 7 系列及以后 FPGA 里的一个底层原语（primitive），全称是 **D Flip-Flop with Clock Enable and Asynchronous Preset**——一个带"时钟使能(CE)"和"异步置位(PRE)"的 D 触发器。它对应芯片里一个**真实的物理寄存器单元**，有明确的端口：

| 端口 | 方向 | 含义 |
|------|------|------|
| `C` | in | 时钟 |
| `CE` | in | 时钟使能，为 0 时哪怕来时钟沿也不采数 |
| `D` | in | 数据输入 |
| `PRE` | in | 异步置位，为 1 时立刻把 Q 拉成 1 |
| `Q` | out | 输出 |

它还有一个不在端口里、但极其关键的属性 **`INIT`**：FPGA 上电配置（Global Set/Reset 释放）瞬间，Q 的值就是 `INIT`。这是本讲的"主角属性"。

> 与之相对，`FDRE` 是带同步复位的版本，`FDCE` 是带异步清零的版本。本讲选用 `FDPE`（异步置位版）。

### 2.3 dont_touch 是什么

`dont_touch` 是 Vivado 的一个综合/实现属性。把它标到某个实例上，等于对工具下令："**这个单元，你不许优化、不许合并、不许删除，原样保留进网表。**" 本讲会看到，没有它，整套日期存储会塌掉。

## 3. 本讲源码地图

| 文件 | 角色 |
|------|------|
| [`hdl/fpga_base_date_package.vhd`](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_date_package.vhd) | 日期存储组件，本讲主角。定义了 `fpga_base_date` 实体，用 5×32 个 FDPE 存年/月/日/时/分，并提供 generic / INIT 两条输出分支。 |
| [`hdl/fpga_base_v1_0.vhd`](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_v1_0.vhd) | 顶层。实例化 `fpga_base_date`，把它的五个输出直接接到寄存器下标 1~5（即偏移 0x04~0x14），并用 `C_USE_INFO_FROM_SCRIPT` 决定走哪条分支。 |
| [`hdl/fpga_base_scripted_info_pkg.vhd`](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_scripted_info_pkg.vhd) | 脚本化信息包。定义 `BuildYear_c` 等带 `$$tag$$` 占位符的常量，是 generic 模式下日期数据的来源（u3-l3 详讲）。本讲仅在讲双模式时引用。 |
| [`fpga_base.tcl`](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/fpga_base.tcl) | 综合 hook 脚本。在综合后用 `set_property INIT` 把每一位日期写进对应 FDPE（u3-l2 详讲）。本讲仅在讲双模式时引用。 |

## 4. 核心概念与源码讲解

本讲按"最小模块"拆成三块：**4.1 FDPE 原语**、**4.2 dont_touch 属性**、**4.3 双模式分支**。三者层层递进——先用 FDPE 造出"一位 ROM"，再用 dont_touch 把它从综合器的剪刀下救回来，最后用双分支决定这一位 ROM 的值到底从哪来。

### 4.1 FDPE 原语：用一个真实触发器当一位 ROM

#### 4.1.1 概念说明

第一反应，存一个编译期就已确定的常量，最自然的写法是 VHDL 的 `constant`：

```vhdl
constant YEAR : std_logic_vector(31 downto 0) := X"000007EA";  -- 2026
```

但 fpga_base **没有**这么做。它给年的每一位都实例化了一个真实的 FDPE 触发器，共 32 个；月、日、时、分同样各 32 个，合计 5 × 32 = **160 个 FDPE**。

为什么放着 `constant` 不用，非要造 160 个真实触发器？根本原因在于**"综合后还能不能被脚本找到并修改"**：

- `constant` 在综合阶段会被当成静态值，被**常量折叠**（constant folding）、传播、化简掉。最终网表里根本没有一个"名叫 YEAR 的单元"可供你寻址修改。它早被溶进了下游逻辑。
- FDPE 是 Xilinx **底层原语**，对应一个**有确定层次路径的真实 cell**，例如 `.../gen_year[31].year_dfpe_inst`。综合后它仍然作为一个独立单元留在网表里，于是外部脚本（`fpga_base.tcl`）可以用 `get_cells` 按这个路径精确地找到它，再用 `set_property INIT` **改写它的上电初值**。

换句话说，FDPE 在这里不是当"会翻转的寄存器"用，而是被当成**一位只读存储(ROM)**来用：它的值在 FPGA 上电那一刻由 `INIT` 决定，之后再也不变。要改 ROM 的内容，不用重新写 HDL，只要改 `INIT` 即可。这正是这套"每次编译自动盖时间戳"机制能在硬件侧落地的关键。

#### 4.1.2 核心流程

先看 FDPE 在本设计里的接法，再推导它的行为。

端口连接（以 `year` 为例）：

```
PRE => '0'   -- 异步置位恒为 0，永不置位
CE  => '0'   -- 时钟使能恒为 0，永不在时钟沿采数
C   => i_clk -- 时钟照接（但因为 CE=0，时钟根本不起作用）
D   => '0'   -- 数据输入恒为 0
Q   => year(count)  -- 输出这一位
```

逐条推演 FDPE 的行为：

1. `PRE = 0` → 不会发生异步置位，Q 不会被强制拉 1。
2. `CE = 0` → 即使 `C` 来了上升沿，触发器也**不采样** `D`，Q 保持原值。
3. 于是运行期 Q **永远不会改变**，自始至终等于它上电那一刻的初值。
4. 而上电初值由属性 `INIT` 决定 → 因此 **Q ≡ INIT（永久）**。

结论：这个 FDPE 等效于一个"上电后永远输出 INIT 这一位"的 ROM 单元。逻辑上的等价电路是：

```
   ┌─────────┐
   │  FDPE   │   PRE=0, CE=0, D=0  =>  Q 永远 = INIT
   │  INIT=k │
   └────┬────┘
        │
        v
     year(count)   （这一位日期）
```

数学上，若把上电时刻记为 \(t_0\)，则对任意 \(t \geq t_0\)：

\[
Q(t) \;=\; \mathrm{INIT}
\]

即输出是与时间无关的常量。这正是"用触发器当 ROM"的全部精髓。

#### 4.1.3 源码精读

日期组件 `fpga_base_date` 的实体声明在 [`hdl/fpga_base_date_package.vhd:65-90`](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_date_package.vhd#L65-L90)。它有一组 generic（年月日时分 + 一个模式开关）和五个 32 位输出端口：

```vhdl
generic (
   C_DATE_YEAR        : integer := 2026;
   C_DATE_MONTH       : integer := 11;
   C_DATE_DAY         : integer := 3;
   C_DATE_HOUR        : integer := 19;
   C_DATE_MINUTE      : integer := 49;
   C_USE_GENERIC_DATE : boolean := false
);
port (
   i_clk    : in  std_logic;
   o_year   : out std_logic_vector(31 downto 0);
   o_month  : out std_logic_vector(31 downto 0);
   o_day    : out std_logic_vector(31 downto 0);
   o_hour   : out std_logic_vector(31 downto 0);
   o_minute : out std_logic_vector(31 downto 0)
);
```

> 注意：这些 generic 的**默认值**（2026/11/3 19:49）只是作者写文件那刻随手填的占位值，运行期真正生效的值来自顶层实例化时的覆盖（见 4.3）。

架构体里用 5 个几乎一样的 `generate` 循环，分别造年/月/日/时/分。以"年"为例，见 [`hdl/fpga_base_date_package.vhd:102-117`](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_date_package.vhd#L102-L117)：

```vhdl
gen_year : for count in 0 to 31 generate
   attribute dont_touch : string;
   attribute dont_touch of year_dfpe_inst: label is "true";
begin
   year_dfpe_inst: FDPE
   port map (
      PRE => '0',
      CE  => '0',
      C   => i_clk,
      D   => '0',
      Q   => year(count)   -- 第 count 位年的输出
   );
end generate;
```

这段代码做了三件事：

1. `for count in 0 to 31 generate` → 复制 32 份 FDPE，下标 `count` 从 0（最低位）到 31（最高位）。
2. 每个 FDPE 的 `Q` 接到内部信号 `year(count)`，即 `year` 的第 `count` 位。
3. 内联声明了一个 `dont_touch` 属性并绑到实例标签 `year_dfpe_inst` 上（4.2 详讲）。

月、日、时、分的 generate 完全同构，只是信号名换成 `month/day/hour/minute`、实例标签换成 `month_dfpe_inst` 等，见 [`hdl/fpga_base_date_package.vhd:119-185`](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_date_package.vhd#L119-L185)。注意**五个字段都造了完整的 32 位 FDPE**，哪怕"分"最多只需要 6 位（0~59）。这种"统一 32 位"是有意为之：让 TCL 脚本能用固定的 0..31 循环一次性处理所有字段，不用为每个字段单独算位宽。

> 关于 generate 与层次命名：`for count in 0 to 31` 配上实例标签 `year_dfpe_inst`，综合后在网表里会生成 32 个 cell，路径形如 `gen_year[0].year_dfpe_inst`、`gen_year[1].year_dfpe_inst`、……、`gen_year[31].year_dfpe_inst`。**正是这条可预测的命名规律，让下一篇 u3-l2 的 TCL 脚本能按 `gen_year[$x].year_dfpe_inst` 精确寻址每一位**。这里先记住这个命名，下讲会用到。

#### 4.1.4 代码实践

**实践目标**：亲手验证"FDPE 在本设计里等价于一位 ROM"，并理解它为何不能被常量替代。

**操作步骤**：

1. 打开 [`hdl/fpga_base_date_package.vhd:107-115`](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_date_package.vhd#L107-L115)，对照本讲 4.1.2 的行为推演表，逐行确认 `PRE/CE/C/D/Q` 的接法。
2. 数一下整个架构体里一共实例化了多少个 FDPE：5 个 generate × 32 = ?（答案：160）。
3. 思考实验：如果把这 160 个 FDPE 全部换成一行 `constant YEAR : std_logic_vector(31 downto 0) := ...`，综合后网表里还会不会有"一个可以被 `get_cells` 找到、再被 `set_property INIT` 修改的单元"？把你的结论写下来。

**需要观察的现象**：你会得出结论——`constant` 不会留下可寻址的物理单元；而 FDPE 会留下一个有明确层次路径的真实 cell。这正是作者选择原语而非常量的根本原因。

**预期结果**：理解"可寻址的真实 cell"是后续 TCL 注入 INIT 的前提。

**待本地验证**（可选，需要 Vivado）：把本组件单独综合，打开综合后的原理图（Schematic），应能看到 160 个 FDPE 实例；试着在 Tcl Console 里 `get_cells */gen_year[*].year_dfpe_inst`，应能列出 32 个。

#### 4.1.5 小练习与答案

**练习 1**：既然 `CE=0` 让 FDPE 永不采数，那时钟端口 `C => i_clk` 是不是可以不接？

> **答案**：从逻辑行为上看，`CE=0` 时时钟确实不起作用，Q 永远等于 INIT。但 FDPE 是 Xilinx 原语，其端口 `C` 是必需端口，不接会在综合时报错。此外，给它接一个真实时钟也利于时序/资源映射工具按常规寄存器放置，行为上无害。所以这里接 `i_clk` 主要是"语法要求 + 规整"，而非功能需要。

**练习 2**：为什么"分"只需要 6 位，却还要造 32 个 FDPE？

> **答案**：为了五个字段结构统一、TCL 脚本处理简单。如果每个字段位宽不同，综合后 cell 命名和数量都要单独计算，TCL 循环得为每个字段写不同逻辑。统一用 32 位让脚本可以用同一个 `0..31` 循环模板处理所有字段。多用几十个寄存器在 FPGA 里代价可忽略。

### 4.2 dont_touch 属性：骗过综合器，保住触发器

#### 4.2.1 概念说明

4.1 留了一个大问题没回答：一个 `D='0'`、`CE='0'`、`PRE='0'` 的 FDPE，输出 Q 必然恒为它的 INIT 值——而在默认情况下 INIT 也是 0（Xilinx 触发器默认 INIT=0）。那么对综合器来说，**这个触发器的输出就是一个确定的常量 '0'**。

综合器非常聪明，它做"常量传播"（constant propagation）时会这样推理：

```
D='0', CE='0', PRE='0'  =>  Q 恒为 '0'  =>  这个触发器是死的
                          =>  下游 year(count) = '0'
                          =>  整个日期字段被化简成全 0
                          =>  160 个 FDPE 全部被删除
```

如果听任综合器这么干，那么：

- 网表里不再有任何 `gen_year[x].year_dfpe_inst` 单元。
- u3-l2 的 TCL 脚本 `get_cells` 会找不到目标，`set_property INIT` 无的放矢。
- 所有日期寄存器读出来永远全 0，"自带出生证"彻底失效。

所以必须有一道"免死金牌"，命令综合器**不许动这些触发器**。这道金牌就是 `dont_touch` 属性。它的语义是：被标记的实例在综合和实现阶段**禁止被优化、合并、复制或删除**，必须原样保留进网表。这样 160 个 FDPE 就会乖乖留在网表里，等 TCL 来改写它们的 INIT。

一句话总结：**FDPE 提供了"可寻址的真实容器"，dont_touch 保证这个容器不被综合器当垃圾回收。** 两者缺一不可。

#### 4.2.2 核心流程

dont_touch 的工作链路：

```
[VHDL 源码]
   attribute dont_touch of year_dfpe_inst : label is "true";
        │  (Vivado 读到这条属性)
        ▼
[综合阶段 synth]
   综合器本来想把常量输入的 FDPE 删掉
   但看到 dont_touch=true  →  放弃优化，原样保留 cell
        │
        ▼
[综合后网表 netlist]
   仍然存在 gen_year[0..31].year_dfpe_inst 等 160 个 cell
   每个 cell 的 INIT 暂时还是默认值 0
        │
        ▼
[TCL hook（下讲 u3-l2 详讲）]
   set_property INIT <bit> [get_cells .../gen_year[x].year_dfpe_inst]
   逐位改写 INIT  →  cell 的上电初值变成真实日期位
```

关键点：`dont_touch` **只挡优化，不挡属性改写**。它阻止综合器删除 cell，但并不阻止后续脚本用 `set_property` 改这个 cell 的 `INIT`。这正是这套机制能成立的精妙之处。

#### 4.2.3 源码精读

`dont_touch` 的声明和绑定直接写在 generate 内部，紧贴实例化语句。`year` 段见 [`hdl/fpga_base_date_package.vhd:102-105`](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_date_package.vhd#L102-L105)：

```vhdl
gen_year : for count in 0 to 31 generate
   attribute dont_touch : string;
   attribute dont_touch of year_dfpe_inst: label is "true";
begin
   year_dfpe_inst: FDPE
   ...
```

三个细节值得注意：

1. **属性类型**：`attribute dont_touch : string;` 声明它是字符串型属性。
2. **绑定目标**：`of year_dfpe_inst: label` 表示把这个属性绑到**实例标签** `year_dfpe_inst`（`label` 是 VHDL 里指标签的关键字），而不是绑到信号或信号类。
3. **属性值**：`is "true"`——Vivado 识别的开启值。

因为属性声明在 `for ... generate` 内部，它会随循环被复制，所以生成的 32 个实例**每一个**都被打上了 `dont_touch`。月/日/时/分四段的写法完全平行，分别绑到 `month_dfpe_inst`、`day_dfpe_inst`、`hour_dfpe_inst`、`minute_dfpe_inst`，见 [`hdl/fpga_base_date_package.vhd:119-185`](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_date_package.vhd#L119-L185)。

> 小知识：dont_touch 也可以从命令行用 `set_property dont_touch true [get_cells ...]` 设置，但本设计选择**写进 HDL**，好处是"源码即契约"——任何人读 VHDL 都能立刻看到这些触发器是被刻意保护的，不会误以为是遗留代码。

#### 4.2.4 代码实践

**实践目标**：验证"如果没有 dont_touch，FDPE 会被优化掉"这一论断，并理解 dont_touch 与 INIT 改写并不冲突。

**操作步骤**：

1. 在 [`hdl/fpga_base_date_package.vhd:102-117`](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_date_package.vhd#L102-L117) 中定位 `attribute dont_touch ...` 这两行。
2. 思维实验：假设把这两行删掉（**仅思考，不要真改源码**），按 4.2.1 的推理，写出综合器会把 `year` 信号化简成什么、网表里还会不会有 FDPE cell。
3. 再思考：即使 dont_touch 把 FDPE 保下来了，它会不会阻止 u3-l2 的 `set_property INIT` 改写？结合 4.2.2 的结论回答。

**需要观察的现象**：你会确认两件事——(a) dont_touch 是 FDPE 不被删除的唯一屏障；(b) dont_touch 只挡"优化/删除"，不挡"属性改写"。

**预期结果**：能在脑子里完整跑通"没有 dont_touch → cell 被删 → TCL 找不到目标 → 日期失效"这条因果链。

**待本地验证**（可选，需要 Vivado）：复制一份组件、注释掉 dont_touch 两行，单独综合，对比有/无 dont_touch 时的 Schematic 里 FDPE 数量（应从 160 变为 0）。注意：此实验请在**仓库外的副本**上做，不要修改本仓库源码。

#### 4.2.5 小练习与答案

**练习 1**：dont_touch 写在 `generate` 内部和写在 `generate` 外部（架构体的声明区）有什么区别？

> **答案**：写在 `generate` 内部时，属性声明随循环体一起被复制，自然绑定到循环生成的每一个实例标签上，所以 32 个 `year_dfpe_inst` 每个都生效。如果写在架构体声明区，则需要另写 `attribute ... of <标签> : label is ...`，且通常只能绑到一个具体标签，无法自动覆盖 generate 出的全部实例。本设计的写法是处理"循环生成 + 逐实例打属性"的标准范式。

**练习 2**：如果一个信号既被 dont_touch 保护，综合器还能不能把它相邻的同类触发器合并（pack）进同一个 slice？

> **答案**：不能。dont_touch 的语义包含"禁止移动/合并"，所以被保护的 cell 在实现阶段会保持原样、不被与其他逻辑打包重组。这正是它"原样保留进网表并落到布局"的完整含义。

### 4.3 双模式分支：generic 注入 vs INIT 注入

#### 4.3.1 概念说明

4.1 和 4.2 解决了"用什么容器装日期位"——答案是"被 dont_touch 保护的 FDPE"。但还有最后一个问题：**这个日期位的值，到底从哪里来、什么时候被写进去？**

fpga_base 给出了**两条互斥的路径**，由一个布尔开关 `C_USE_GENERIC_DATE` 选择：

| 模式 | 开关 | 日期数据来源 | 注入时机 | 注入工具 |
|------|------|--------------|----------|----------|
| **INIT 模式**（默认） | `C_USE_GENERIC_DATE = false` | FDPE 的 `INIT` 初值 | 综合**之后** | `fpga_base.tcl`（u3-l2） |
| **generic 模式** | `C_USE_GENERIC_DATE = true` | VHDL generic 常量 | 综合**之前**（elaboration） | `update_version.py`（u3-l3） |

- **INIT 模式**：输出读 FDPE 的 Q（`o_year <= year`）。FDPE 的运行期值恒为 INIT，而 INIT 由 TCL 脚本在综合后逐位写好。这是 fpga_base 的**默认**模式，也是本讲和 u3-l2 的主线。
- **generic 模式**：输出直接读 generic 常量（`o_year <= to_unsigned(C_DATE_YEAR, 32)`）。这些 generic 由顶层从 `fpga_base_scripted_info_pkg.vhd` 里的 `BuildYear_c` 等常量传入，而这些常量上的 `$$year$$` 占位符会被 `update_version.py` 在综合前替换成真实时间。这条路径与"脚本化版本号"机制共用，u3-l3 详讲。

两条路径殊途同归——都是"外部脚本把当前时间塞进硬件"——但塞的**时机和对象**不同：一个改网表 cell 的属性，一个改 HDL 源码的常量再重新 elaborate。提供双模式是为了兼容 PSI 的两种工具链习惯。

> 注意一个容易混淆的点：在 generic 模式下，输出 `o_year` 直接来自 generic，**不再消费 FDPE 的 Q**。但 4.1/4.2 造的那 160 个 FDPE 因为有 dont_touch，**依然存在于网表中**（只是其输出未被使用）。这是一种被接受的轻微冗余，换来的是两种模式下网表结构一致、TCL 寻址路径不变。

#### 4.3.2 核心流程

`fpga_base_date` 架构体末尾用两个互斥的条件 generate 实现分支选择，见伪代码：

```
g_generics:  if C_USE_GENERIC_DATE generate
    o_year   <= to_unsigned(C_DATE_YEAR,   32)   -- 来自 generic
    o_month  <= to_unsigned(C_DATE_MONTH,  32)
    ...                                        -- generic 模式：读常量
end generate;

g_ngenerics: if (not C_USE_GENERIC_DATE) generate
    o_year   <= year;                            -- INIT 模式：读 FDPE 的 Q
    o_month  <= month;
    ...
end generate;
```

由于 `C_USE_GENERIC_DATE` 是布尔 generic， elaboration 时它的值就已确定，所以这两条 `generate` **永远只有一条被 elaboration 出来**，不存在运行期选择，也不存在冗余的硬件分支。

那 `C_USE_GENERIC_DATE` 又由谁决定？看顶层实例化。在 [`hdl/fpga_base_v1_0.vhd:241-264`](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_v1_0.vhd#L241-L264)，顶层把日期组件的 generic 直接挂到自己的 `C_USE_INFO_FROM_SCRIPT` 上：

```
C_USE_GENERIC_DATE => C_USE_INFO_FROM_SCRIPT
```

而 `C_USE_INFO_FROM_SCRIPT` 是顶层的泛型，默认 `false`，见 [`hdl/fpga_base_v1_0.vhd:37`](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_v1_0.vhd#L37)。同时它还控制版本号寄存器的来源，见 [`hdl/fpga_base_v1_0.vhd:233-234`](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_v1_0.vhd#L233-L234)：

```vhdl
reg_rdata(0) <= C_VERSION when not C_USE_INFO_FROM_SCRIPT else BuildGitHash_c;
```

于是这个开关是"**整套脚本化信息机制的总闸**"：

```
C_USE_INFO_FROM_SCRIPT = false（默认）
   ├── 版本号寄存器  <= C_VERSION         （用户给的版本号）
   └── 日期          <= FDPE Q（INIT 模式）  ← 本讲 + u3-l2 主线
       └── 由 fpga_base.tcl 在综合后写 INIT

C_USE_INFO_FROM_SCRIPT = true
   ├── 版本号寄存器  <= BuildGitHash_c     （git hash）
   └── 日期          <= generic（generic 模式）  ← u3-l3 主线
       └── 由 update_version.py 在综合前改 $$tag$$
```

两条线都通向"日期/版本寄存器读出真实编译信息"这同一个目标。

#### 4.3.3 源码精读

**分支一：generic 模式**，见 [`hdl/fpga_base_date_package.vhd:187-194`](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_date_package.vhd#L187-L194)：

```vhdl
g_generics : if C_USE_GENERIC_DATE generate
begin
   o_year   <= std_logic_vector(to_unsigned(C_DATE_YEAR,   32));
   o_month  <= std_logic_vector(to_unsigned(C_DATE_MONTH,  32));
   o_day    <= std_logic_vector(to_unsigned(C_DATE_DAY,    32));
   o_hour   <= std_logic_vector(to_unsigned(C_DATE_HOUR,   32));
   o_minute <= std_logic_vector(to_unsigned(C_DATE_MINUTE, 32));
end generate;
```

`to_unsigned(整数, 32)` 把 integer generic 转成 32 位无符号向量，输出直接来自 generic 常量。此时 FDPE 的 Q（内部信号 `year` 等）未被使用。

**分支二：INIT 模式**，见 [`hdl/fpga_base_date_package.vhd:196-203`](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_date_package.vhd#L196-L203)：

```vhdl
g_ngenerics : if not C_USE_GENERIC_DATE generate
begin
   o_year   <= year;
   o_month  <= month;
   o_day    <= day;
   o_hour   <= hour;
   o_minute <= minute;
end generate;
```

输出直接接 4.1 里那 160 个 FDPE 的 Q（内部信号 `year/month/day/hour/minute`）。运行期这些信号恒等于各自 FDPE 的 INIT，而 INIT 由 TCL 综合后写入。

**顶层实例化**：把日期组件的输出接到寄存器下标 1~5（对应偏移 0x04~0x14），并把模式开关交给 `C_USE_INFO_FROM_SCRIPT`，见 [`hdl/fpga_base_v1_0.vhd:241-264`](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_v1_0.vhd#L241-L264)：

```vhdl
fpga_base_date_inst: entity work.fpga_base_date
generic map (
   C_DATE_YEAR        => BuildYear_c,      -- 来自 scripted_info_pkg（$$year$$）
   C_DATE_MONTH       => BuildMonth_c,
   C_DATE_DAY         => BuildDay_c,
   C_DATE_HOUR        => BuildHour_c,
   C_DATE_MINUTE      => BuildMinute_c,
   C_USE_GENERIC_DATE => C_USE_INFO_FROM_SCRIPT   -- 总闸
)
port map (
   i_clk    => s00_axi_aclk,
   o_year   => reg_rdata( 1),   -- 偏移 0x04
   o_month  => reg_rdata( 2),   -- 偏移 0x08
   o_day    => reg_rdata( 3),   -- 偏移 0x0C
   o_hour   => reg_rdata( 4),   -- 偏移 0x10
   o_minute => reg_rdata( 5)    -- 偏移 0x14
);
```

这里能看到两个关键事实：

1. 五个日期输出**直接驱动** `reg_rdata(1..5)`——回忆 u2-l3，这些就是只读日期寄存器，软件/JTAG/EPICS 读它们就是读这五个值。
2. generic 模式下用到的 `BuildYear_c` 等常量来自 [`hdl/fpga_base_scripted_info_pkg.vhd:12-17`](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_scripted_info_pkg.vhd#L12-L17)，它们带 `$$year$$` 等占位符：

```vhdl
constant BuildYear_c    : integer := 0000; -- $$year$$
constant BuildMonth_c   : integer := 0;    -- $$month$$
...
```

这些占位符由 `update_version.py` 在综合前替换（u3-l3），从而 generic 模式下日期能在每次构建时自动更新。

而 INIT 模式下，日期的写入发生在综合后，由 [`fpga_base.tcl:46-68`](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/fpga_base.tcl#L46-L68) 完成，其"年"段的写 INIT 循环节选如下：

```tcl
set x 31
set y 0
while {$x>=0} {
   set val [string index $binYear $y]
   set reg */fpga_base_inst/U0/fpga_base_date_inst/gen_year[$x].year_dfpe_inst
   set_property -verbose INIT $val [get_cells $reg]
   set x [expr {$x - 1}]
   set y [expr {$y + 1}]
}
```

这段脚本按 `gen_year[$x].year_dfpe_inst` 找到每一位 FDPE cell，把 `INIT` 设成日期对应位的 0/1——**这正是 4.1 里"可预测的 generate 命名"和 4.2 里"dont_touch 保住 cell"共同 enabling 的结果**。完整的位映射与循环分析留到 u3-l2。

#### 4.3.4 代码实践

**实践目标**：把"为什么 D/CE/PRE 都接常量的 FDPE 在正常综合下会被优化掉、又是如何被保留下来的"以及"g_generics/g_ngenerics 各对应哪种使用场景"这两个问题彻底走通。

**操作步骤**：

1. **确认"会被优化掉"的推理**：打开 [`hdl/fpga_base_date_package.vhd:107-115`](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_date_package.vhd#L107-L115)，确认 FDPE 的 `PRE/CE/D` 三个端口都接常量 `'0'`。按 4.2.1 的常量传播推理，写下"如果没有 dont_touch，Q 会被化简成什么"。预期答案：Q 恒为 INIT，而默认 INIT=0，所以 Q 化简为常量 '0'，触发器被删除。

2. **确认"如何被保留"**：在同文件 [`hdl/fpga_base_date_package.vhd:103-104`](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_date_package.vhd#L103-L104) 找到 `dont_touch` 两行，明确它绑在实例标签 `year_dfpe_inst` 上，值为 `"true"`。据此解释：dont_touch 阻止综合器删除这个 cell，于是 160 个 FDPE 全部保号留在网表里，TCL 才有目标可写。

3. **区分两个分支的用途**：阅读 [`hdl/fpga_base_date_package.vhd:187-203`](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_date_package.vhd#L187-L203)，把下表填完整：

   | generate | 条件 | 输出来源 | 对应使用场景 | 注入工具/时机 |
   |----------|------|----------|--------------|----------------|
   | `g_generics` | `C_USE_GENERIC_DATE = ?` | generic 常量 | ？ | ？ |
   | `g_ngenerics` | `not C_USE_GENERIC_DATE` | FDPE 的 Q | ？ | ？ |

4. **追总闸**：从 [`hdl/fpga_base_v1_0.vhd:248`](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_v1_0.vhd#L248) 的 `C_USE_GENERIC_DATE => C_USE_INFO_FROM_SCRIPT` 追到 [`hdl/fpga_base_v1_0.vhd:37`](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_v1_0.vhd#L37) 的 `C_USE_INFO_FROM_SCRIPT : boolean := false`，确认默认值，据此说出"默认情况下日期走哪条模式"。

**需要观察的现象**：你能用自己的话完整复述三段因果——(a) 常量输入 → 综合器想删；(b) dont_touch → 综合器不删；(c) `C_USE_INFO_FROM_SCRIPT` 总闸 → 决定读 generic 还是读 FDPE。

**预期结果**：

- 练习 3 表格答案：`g_generics` 条件 `C_USE_GENERIC_DATE = true`，场景为"脚本化构建（`update_version.py` 改 `$$tag$$`）"，注入时机为综合前；`g_ngenerics` 场景为"默认的 TCL 综合 hook 模式（`fpga_base.tcl` 写 INIT）"，注入时机为综合后。
- 练习 4 答案：`C_USE_INFO_FROM_SCRIPT` 默认 `false` → 默认走 `g_ngenerics`（INIT 模式），即 fpga_base 默认依赖 `fpga_base.tcl` 在综合后写日期。

**待本地验证**：上述结论仅基于源码静态阅读。若要实证，需在 Vivado 中分别以两种 `C_USE_INFO_FROM_SCRIPT` 配置打包，综合后检查 `fpga_base.tcl` 是否被调用、日期寄存器读出值是否为当次编译时间。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `g_generics` 和 `g_ngenerics` 用 `if ... generate`，而不是用一个 `if-else` 在运行期选择？

> **答案**：因为 `C_USE_GENERIC_DATE` 是 generic（编译期常量），elaboration 时它的值已确定。`if ... generate` 会在 elaboration 期**静态展开**出唯一一条分支的硬件，另一条分支根本不存在于网表中。这比运行期 `if-else`（会生成多路选择器、两条分支硬件都在）更省资源，也更符合"两种互斥构建模式"的语义。VHDL-2008 之前没有 `elsif generate`，所以这里用两个互逆条件的独立 generate 来实现"二选一"。

**练习 2**：在 generic 模式（`g_generics` 生效）下，4.1/4.2 造的 160 个 FDPE 还有用吗？为什么它们仍然出现在网表里？

> **答案**：在 generic 模式下，输出 `o_year` 等直接来自 generic 常量，FDPE 的 Q（内部信号 `year` 等）**未被消费**，从功能上看这些 FDPE 是"死逻辑"。但它们带有 `dont_touch` 属性，综合器不会删除，因此仍出现在网表中。这是一种被接受的轻微冗余，换来的好处是两种模式下网表结构一致、cell 命名路径（`gen_year[x].year_dfpe_inst`）不变，便于工具链和脚本统一处理。

**练习 3**：如果把顶层 [`hdl/fpga_base_v1_0.vhd:248`](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_v1_0.vhd#L248) 的 `C_USE_GENERIC_DATE => C_USE_INFO_FROM_SCRIPT` 改成 `C_USE_GENERIC_DATE => false` 硬编码，会有什么后果？

> **答案**：日期组件会被永久锁定在 INIT 模式（`g_ngenerics`），无论 `C_USE_INFO_FROM_SCRIPT` 设成什么都只读 FDPE 的 Q。即使 `update_version.py` 把 `BuildYear_c` 等改成了正确时间，generic 模式分支也不会被启用，日期仍只依赖 `fpga_base.tcl` 写入的 INIT。版本号寄存器（仍由 `C_USE_INFO_FROM_SCRIPT` 控制）则会和日期"对不上口径"——一个走脚本、一个走 TCL。所以这行 generic 映射把"日期模式"和"版本号模式"绑定到同一个总闸上，是刻意的一致性设计。

## 5. 综合实践

把本讲三块知识串成一个完整的"探案"任务：**追踪一个日期位从源码到芯片的全旅程**。

任务背景：现场工程师报告某块板子的"固件年份"寄存器（偏移 `0x04`，即 `reg_rdata(1)`）读出来总是 0。请你根据本讲所学，列出**所有可能让这一位变 0 的原因**，并给出排查顺序。

建议步骤：

1. **从读出口倒推**：`reg_rdata(1)` 由 `fpga_base_date_inst` 的 `o_year` 驱动（[`hdl/fpga_base_v1_0.vhd:259`](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_v1_0.vhd#L259)）。
2. **判断模式**：`o_year` 走哪条分支取决于 `C_USE_GENERIC_DATE`（即 `C_USE_INFO_FROM_SCRIPT`）。默认 false → 走 `g_ngenerics` → `o_year <= year`（FDPE 的 Q）。
3. **检查 FDPE 是否保住**：`year(31..0)` 由 32 个 FDPE 驱动，靠 `dont_touch` 保号。如果 dont_touch 没生效（例如被某次重构去掉），FDPE 被优化，`year` 全 0。
4. **检查 INIT 是否被写**：即使 FDPE 保住，若 `fpga_base.tcl` 没在综合后跑（或寻址路径对不上），INIT 仍是默认 0，`year` 全 0。
5. **给出排查清单**：按"模式开关 → dont_touch 是否生效 → TCL 是否执行 → cell 路径是否匹配"的顺序列出检查点。

把你整理的排查清单写成一页文档，并与本讲 4.1~4.3 的结论对照自检。这个任务综合了"FDPE 当 ROM（4.1）+ dont_touch 保号（4.2）+ 双模式选择（4.3）"三块知识，是理解整套日期机制的最佳练手。

## 6. 本讲小结

- fpga_base 用 **5 × 32 = 160 个 FDPE 触发器**（而非 VHDL 常量）来存固件编译日期，因为只有真实的底层原语 cell 才能在综合后留下**可寻址、可被脚本改写 INIT 的单元**。
- 每个 FDPE 的 `D/CE/PRE` 都接常量 `'0'`，使其运行期永不翻转，**等效于一位 ROM**：输出 Q 永久等于上电初值 `INIT`。
- 若无保护，综合器的常量传播会把这种"输入全常量"的 FDPE 当死逻辑删除；`dont_touch` 属性绑在每个实例标签上，**强制综合器原样保留**这些 cell。
- 两个互斥的 `generate` 分支实现双模式：`g_generics`（`C_USE_GENERIC_DATE=true`）从 generic 常量读日期（对应 `update_version.py` 脚本化注入，u3-l3）；`g_ngenerics`（默认）从 FDPE 的 Q 读日期（对应 `fpga_base.tcl` 综合 hook 写 INIT，u3-l2）。
- 这个模式开关在顶层被统一连到 `C_USE_INFO_FROM_SCRIPT`，它同时控制版本号寄存器的来源，是"整套脚本化信息机制"的总闸。
- 两种模式下网表里始终保留同样命名规律的 FDPE cell（`gen_year[x].year_dfpe_inst` 等），为下一篇 u3-l2 的 TCL 逐位写 INIT 提供了可预测的寻址基础。

## 7. 下一步学习建议

- **紧接着读 u3-l2**：本讲反复提到的 `fpga_base.tcl` 会在综合后逐位写 `INIT`。下一篇将精读它的 `dec2bin` 函数和 `x` 从 31 递减、`y` 从 0 递增的位映射循环，搞清楚字符串二进制的最高位为何对应 `gen_year[31]`。
- **之后再读 u3-l3**：`update_version.py` 如何用 gitpython 读 git hash 和当前时间、替换 `$$tag$$` 占位符，以及它对"脏仓库"的特殊处理。
- **回看 u2-l3 的寄存器映射**：对照本讲的 `reg_rdata(1..5)` 接法，再次确认偏移 `0x04~0x14` 这五个只读寄存器为何只读——因为它们由 FDPE/generic 驱动，软件无法在运行期改写。
- **延伸阅读**：Xilinx UG953（7 系列 Libraries Guide）里 FDPE 原语条目，以及 UG903（Vivado 属性参考）里 `DONT_TOUCH` 与 `INIT` 属性条目，可帮你把本讲的"属性语义"沉淀成权威记忆。
