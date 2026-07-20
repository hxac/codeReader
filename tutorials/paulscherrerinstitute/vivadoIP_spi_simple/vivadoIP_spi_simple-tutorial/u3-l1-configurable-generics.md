# 可配置 generics 与 Vivado 参数化

## 1. 本讲目标

学完本讲后，你应当能够：

- 说出 `spi_simple` IP 全部可配置 generics 的**取值范围、类型与默认值**，并解释每一个 generic 在物理上控制什么。
- 理解一个 generic 从「Vivado GUI 里用户填的参数」一路传递到「综合进 RTL 的常量」所经过的**三层**：`PARAM_VALUE` → `MODELPARAM_VALUE` → VHDL generic。
- 读懂 `scripts/package.tcl` 用 `gui_create_parameter` / `gui_parameter_set_range` / `gui_parameter_set_widget_dropdown` 声明 GUI 参数的方式，以及自动生成的 `xgui/spi_simple_v1_4.tcl` 与 `component.xml` 各自承担什么角色。
- 理解**端口条件使能**机制：`spi_tri` 端口为何只在 `TriWiresSpi_g = true` 时才出现在 IP 外围接口上，以及它在 `package.tcl` 与 `component.xml` 中的两处落点。

本讲是「专家层」的第一讲，默认你已经读过进阶层（尤其是 [u2-l3 AXI4 从接口与寄存器映射](./u2-l3-axi-register-interface.md)），知道 `spi_vivado_wrp` 是顶层 wrapper、`spi_simple` 是核心、二者之间靠 generic map 透传参数。

## 2. 前置知识

在进入源码前，先用三段白话把概念立起来。

**什么是 generic（参数）？**
VHDL 里的 `generic` 是「综合时就定死、之后不能再改」的常量。它与「运行时可写的寄存器」是对立的两个概念：

- **generic**：综合期常量，决定硬件**长什么样**（比如 SPI 几位、接几个从机、FIFO 多深）。改它要重新综合。
- **寄存器**：运行期可变状态，决定硬件**当前在干什么**（比如现在选哪个从机、是否存 RX）。改它只要一次 AXI 写。

本讲只讲 generic。寄存器地图在 [u2-l1 寄存器地图](./u2-l1-register-map.md) 已讲过。

**什么是 Vivado IP-XACT 参数化？**
一个裸 VHDL 文件的 generic 只能靠改代码或例化时传参来设。但把 RTL「打包」成 Vivado IP 后，每个 generic 会变成 IP 定制 GUI 里的一个**可填字段**（数字框、下拉框、勾选框），用户在图形界面里填值，Vivado 把这个值灌进 RTL 再综合。这套「GUI 字段 ↔ generic」的映射，由三样东西维护：

- `scripts/package.tcl`：人写的**唯一数据源**，用 PsiIpPackage 的 Tcl 命令声明每个参数。
- `xgui/spi_simple_v1_4.tcl`：打包时**自动生成**的 GUI 布局脚本（决定字段排在哪个页面、用什么控件）。
- `component.xml`：打包时**自动生成**的 IP-XACT 清单（机器可读的参数表、端口表、文件表）。

> 关键认知：`xgui/*.tcl` 与 `component.xml` 都是 `package.tcl` 跑完后的**产物**，日常绝不该手改它们——要改参数化行为，只改 `package.tcl`，然后重新打包。这一点在 [u1-l2 目录结构](./u1-l2-directory-structure.md) 已经埋过伏笔。

**一个值要走几层？**
用户在 GUI 填的值，在 IP-XACT 里有**两个名字**：

- `PARAM_VALUE.Xxx_g`：用户直接编辑的值（`resolve="user"`），带 min/max/下拉选项，用于 GUI 校验。
- `MODELPARAM_VALUE.Xxx_g`：喂给 RTL 模型（即 VHDL generic）的值（`resolve="generated"`），由一段 xgui 回调过程从 `PARAM_VALUE` 拷过来。

所以一条 generic 的完整生命线是：

```
Vivado GUI 字段 (PARAM_VALUE)
        │  xgui 的 update_MODELPARAM_VALUE.Xxx_g 过程把值拷过去
        ▼
MODELPARAM_VALUE.Xxx_g
        │  Vivado 综合时把它当作 VHDL generic 的实参
        ▼
spi_vivado_wrp 的 generic  (wrapper 层)
        │  wrapper 的 generic map 透传
        ▼
spi_simple 的 generic      (核心层)
        │  spi_simple 的 generic map（部分改名）透传
        ▼
psi_common_spi_master 的 generic  (真正生成 SCK 的引擎)
```

记住这条链，本讲后面所有内容都是在讲这条链上的某一环。

## 3. 本讲源码地图

| 文件 | 角色 |
|---|---|
| [hdl/spi_vivado_wrp.vhd](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd) | 顶层 wrapper，**generic 的对外公共面**：所有用户可见 generic 在这里声明类型/范围/默认值，并在这里决定哪些透传给 `spi_simple`。 |
| [hdl/spi_simple.vhd](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_simple.vhd) | SPI 核心，接收透传来的 generic，并把它们（部分改名）再透传给 `psi_common_spi_master`。注意它**没有** `TriWiresSpi_g`。 |
| [scripts/package.tcl](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/package.tcl) | 人写的打包脚本，**参数化的唯一数据源**：声明 GUI 参数、范围、控件、端口条件使能。 |
| [xgui/spi_simple_v1_4.tcl](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/xgui/spi_simple_v1_4.tcl) | 自动生成的 GUI 布局脚本，定义 `init_gui`（字段排版）和一堆 `update_MODELPARAM_VALUE.*` / `validate_PARAM_VALUE.*` 回调。 |
| [component.xml](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/component.xml) | 自动生成的 IP-XACT 清单，机器可读地描述参数（`PARAM_VALUE`/`MODELPARAM_VALUE`）、端口（含宽度依赖与使能依赖）、文件集。 |

## 4. 核心概念与源码讲解

### 4.1 generic 取值范围与含义

#### 4.1.1 概念说明

`spi_simple` 一共暴露 **13 个用户可配 generic**（外加 1 个 AXI 内部参数 `C_S00_AXI_ID_WIDTH`）。它们可以按「控制什么」分成四组：

| 分组 | generic | 一句话作用 |
|---|---|---|
| **时序** | `ClockDivider_g`、`CsHighCycles_g`、`SpiCPOL_g`、`SpiCPHA_g` | 决定 SCK 频率、片选间隔、时钟极性与采样边沿（即 SPI Mode 0/1/2/3） |
| **数据格式** | `TransWidth_g`、`LsbFirst_g`、`MosiIdleState_g` | 每帧几位、先发 MSB 还是 LSB、空闲时 MOSI 电平 |
| **拓扑** | `SlaveCnt_g`、`FifoDepth_g` | 几个从机（决定 `spi_cs_n`/`spi_le` 位宽）、FIFO 多深 |
| **3-Wire 扩展** | `TriWiresSpi_g`、`ReadBitPol_g`、`TriStatePol_g`、`SpiDataPos_g` | 是否启用三线 SPI、读写位极性、三态极性、数据起始位 |

前两组在 [u2-l4 SPI 主控时序](./u2-l4-spi-master-timing.md) 已详细讲过时序含义，本讲聚焦在「取值范围」与「参数化机制」上。

#### 4.1.2 核心流程

generic 的**权威取值范围**写在 VHDL 实体声明里（wrapper 与核心各一份），GUI 与 IP-XACT 的范围都应当与它一致——但实际项目中三者偶尔会对不齐（见 4.1.4 实践）。判读一个 generic 要看三处：

1. **wrapper 实体** `spi_vivado_wrp.vhd` 的 `generic` 块：含 `range ... to ...` 子类型约束与 `:= 默认值`。这是综合器真正强制的边界。
2. **核心实体** `spi_simple.vhd` 的 `generic` 块：应当与 wrapper 同名同范围（但有些默认值不同，见下）。
3. **`package.tcl`** 的 `gui_parameter_set_range`：GUI 输入框的上下限，只做 GUI 层校验，不替代 VHDL 约束。

类型上要分清三种：

- `natural range 0 to 1` / `natural range 4 to 1_000_000`：整数，带范围。
- `positive`：≥1 的整数（无上界，靠 GUI 范围或工程约束补上界）。
- `boolean`：真/假，GUI 上是勾选框。
- `std_logic`：单比特，GUI 上是 `{0,1}` 下拉框。

#### 4.1.3 源码精读

先看 wrapper 的 generic 全家福（这是对外的公共契约）：

[wrapper generic 块：hdl/spi_vivado_wrp.vhd:24-43](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L24-L43) —— 13 个可配 generic 加 `C_S00_AXI_ID_WIDTH`，每行注释说明了物理含义。

逐条摘出来（行号指向 wrapper 声明处）：

| generic | 类型/范围/默认 | 行号 | 含义 |
|---|---|---|---|
| `ClockDivider_g` | `natural range 4 to 1_000_000 := 4`（须为 2 的倍数） | [L27](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L27) | AXI 时钟到 SCK 的分频比 |
| `TransWidth_g` | `positive := 32` | [L28](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L28) | 单次 SPI 事务的比特数 |
| `CsHighCycles_g` | `positive := 20` | [L29](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L29) | 两次事务间 CS_n 最少保持高的时钟周期数 |
| `SpiCPOL_g` | `natural range 0 to 1 := 0` | [L30](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L30) | SCK 空闲电平（0=低） |
| `SpiCPHA_g` | `natural range 0 to 1 := 0` | [L31](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L31) | 采样边沿（0=前导沿） |
| `SlaveCnt_g` | `positive := 1` | [L32](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L32) | 支持的从机数 |
| `LsbFirst_g` | `boolean := false` | [L33](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L33) | 是否低位先发 |
| `FifoDepth_g` | `positive := 256` | [L34](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L34) | TX/RX FIFO 深度 |
| `TriWiresSpi_g` | `boolean := false` | [L35](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L35) | 是否启用 3-Wire SPI（**仅 wrapper 有**） |
| `MosiIdleState_g` | `std_logic := '0'` | [L36](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L36) | 空闲时 MOSI 电平 |
| `ReadBitPol_g` | `std_logic := '1'` | [L37](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L37) | 3-Wire 下读写位的「读」极性 |
| `TriStatePol_g` | `std_logic := '1'` | [L38](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L38) | 3-Wire 三态信号的极性 |
| `SpiDataPos_g` | `positive := 8` | [L39](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L39) | 数据字中 SPI 数据的起始位（3-Wire 需要） |

`ClockDivider_g` 与 SCK 频率的关系（与 [u2-l4](./u2-l4-spi-master-timing.md) 一致）近似为：

\[
f_{\text{SCK}} \approx \frac{f_{\text{Clk}}}{\text{ClockDivider\_g}}
\]

且必须为 2 的倍数（注释 [L27](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L27) 明确写了 `Must be a multiple of two`）。引擎内部精确的计数逻辑属于 `psi_common_spi_master`，**待本地验证**。

再看核心 `spi_simple` 的 generic 块：

[核心 generic 块：hdl/spi_simple.vhd:28-41](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_simple.vhd#L28-L41) —— 注意两点关键差异：

1. **没有 `TriWiresSpi_g`**。3-Wire 的「总开关」只存在于 wrapper；核心永远把 `SpiTri` 端口接出来（见 [spi_simple.vhd:76](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_simple.vhd#L76)），是否对外暴露由 wrapper 的端口条件使能控制（见 4.3）。
2. **`SpiDataPos_g` 默认值是 `3`**（[L40](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_simple.vhd#L40)），而 wrapper 默认是 `8`（[spi_vivado_wrp.vhd:39](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L39)）。由于 wrapper 例化时显式传值（见下），核心的默认值实际用不到，但这种「同名不同默认」是阅读时要警惕的陷阱。

接着看 wrapper 如何把这些 generic **透传**给核心：

[wrapper→核心 generic map：hdl/spi_vivado_wrp.vhd:213-227](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L213-L227) —— 绝大多数同名直传（`ClockDivider_g => ClockDivider_g` …）。注意 `TriWiresSpi_g` 在这里**不出现**——它不是核心的 generic，只服务于端口使能。

最后看核心如何把 generic **改名透传**给真正的 SPI 引擎：

[核心→引擎 generic map：hdl/spi_simple.vhd:271-284](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_simple.vhd#L271-L284) —— 这里能看到「改名」：`ClockDivider_g => clk_div_g`、`SpiCPOL_g => spi_cpol_g`、`SpiCPHA_g => spi_cpha_g`、`CsHighCycles_g => cs_high_cycles_g`、`MosiIdleState_g => mosi_idle_state_g` 等。引擎 `psi_common_spi_master` 才是真正生成 SCK 波形的实体，本 IP 只是给它喂参数。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**，目标是亲手核对三层范围是否对齐，并发现潜在的不一致。

1. **实践目标**：建立一张「三层范围对照表」，验证 wrapper / 核心实体 / `package.tcl` 三处对同一 generic 的范围声明是否一致。
2. **操作步骤**：
   - 打开 [hdl/spi_vivado_wrp.vhd:24-43](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L24-L43)，抄下每个 generic 的类型与范围。
   - 打开 [hdl/spi_simple.vhd:28-41](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_simple.vhd#L28-L41)，对照同名 generic 的范围。
   - 打开 [scripts/package.tcl:69-117](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/package.tcl#L69-L117)，记录每个参数是否带 `gui_parameter_set_range`、上下限是多少。
3. **需要观察的现象**：
   - `ClockDivider_g` 三层都写 `4..1000000`，完全对齐。
   - `SpiDataPos_g`：wrapper 默认 `8`、核心默认 `3`、`package.tcl` 范围 `1..32`。**默认值在 wrapper 与核心之间不一致**——这就是你要发现的「陷阱」。
   - `CsHighCycles_g` 与 `FifoDepth_g` 在 `package.tcl` 里**没有** `gui_parameter_set_range`，意味着 GUI 不做上下限校验，唯一约束来自 VHDL 的 `positive`（≥1）。
4. **预期结果**：得到一张表，其中至少有两处「三层不对齐」被标红：`SpiDataPos_g` 默认值不一致、两个无范围参数。**待本地验证**：如果你真在 Vivado 里打包，观察 GUI 里 `CsHighCycles_g` 是否能填入 0 或负数（理论上 GUI 不拦，但综合会因 `positive` 报错）。
5. 结论记一句：**VHDL 实体的子类型约束才是最终权威**，`package.tcl` 的范围只是 GUI 友好的前置校验。

#### 4.1.5 小练习与答案

**练习 1**：`SlaveCnt_g = 3` 时，`spi_cs_n` 端口的位宽是多少？为什么不是 2 的幂也能工作？

**参考答案**：位宽是 3（`SlaveCnt_g-1 downto 0` = `2 downto 0`）。`spi_cs_n` 在 wrapper 声明为 `std_logic_vector(SlaveCnt_g-1 downto 0)`（[spi_vivado_wrp.vhd:50](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L50)），位宽直接由 generic 算出，不要求 2 的幂。注意：寄存器里 **Slave 字段**的位宽是 `log2ceil(SlaveCnt_g)`（向上取整，见 [u2-l2](./u2-l2-spi-core-architecture.md)），那是另一回事——端口位宽和命令字字段位宽用的是不同的数学。

**练习 2**：为什么 `SpiCPOL_g` 用 `natural range 0 to 1` 而不是 `boolean`？

**参考答案**：因为 SPI 协议里 CPOL/CPHA 是「Mode 0/1/2/3」组合里的一个数学分量，下游引擎 `psi_common_spi_master` 用整数算采样边沿（见 [spi_simple.vhd:276-277](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_simple.vhd#L276-L277) 的 `spi_cpol_g`/`spi_cpha_g`）；用整数 0/1 比布尔更便于参与算术与 IP-XACT 的 `{0,1}` 下拉枚举。而 `LsbFirst_g`、`TriWiresSpi_g` 是纯开关，所以用 `boolean`。

### 4.2 GUI 参数声明（package.tcl 与 xgui）

#### 4.2.1 概念说明

上一节讲了 generic 本身，这一节讲「generic 怎么变成 GUI 里的字段」。这里有三套命令需要分清（都属于 PsiIpPackage 框架，[u1-l3](./u1-l3-toolchain-and-dependencies.md) 介绍过 `PsiIpPackage` 是依赖之一）：

- `gui_create_parameter <名> <显示文本>`：声明一个参数，指定它在 GUI 里显示给人看的说明文字。
- `gui_parameter_set_range <min> <max>`：给数字参数设上下限（GUI 输入框校验）。
- `gui_parameter_set_widget_dropdown {…}` / `gui_parameter_set_widget_checkbox`：指定控件类型——下拉框（枚举值）或勾选框。
- `gui_add_parameter`：把上面配好的参数真正加进当前 GUI 页。

页面用 `gui_add_page "Configuration"` 创建，所有参数都挂在这一页下。

#### 4.2.2 核心流程

声明一个 GUI 参数的标准三步（或两步）：

```
gui_create_parameter <内部名> <给用户看的说明>   # 第1步：声明
gui_parameter_set_range <lo> <hi>                 # 第2步(可选)：数字范围
   或 gui_parameter_set_widget_dropdown {…}       #   或：下拉枚举
   或 gui_parameter_set_widget_checkbox           #   或：勾选框
gui_add_parameter                                 # 第3步：加入页面
```

打包时，PsiIpPackage 读取这些命令，**自动生成**两样东西：

1. `xgui/spi_simple_v1_4.tcl` 里的 `init_gui` 过程（字段在页面里的排版与控件类型）和一堆 `update_MODELPARAM_VALUE.*` 回调（把 `PARAM_VALUE` 拷到 `MODELPARAM_VALUE`）。
2. `component.xml` 里的 `<spirit:parameters>`（`PARAM_VALUE.*`，用户层）与 `<spirit:modelParameters>`（`MODELPARAM_VALUE.*`，模型层），以及 `<spirit:choices>`（下拉选项表）。

文件名里的 `v1_4` 对应 `package.tcl` 里 `set IP_VERSION 1.4`（[package.tcl:17](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/package.tcl#L17)）。这个 1.4 是**开发版**（当前 HEAD 引入了 3-Wire SPI，尚未写进 `Changelog.md`，Changelog 最新仍是 1.3.0）。

#### 4.2.3 源码精读

先看 `package.tcl` 怎么声明全部 13 个参数。先建页面：

[创建 GUI 页面：scripts/package.tcl:67](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/package.tcl#L67) —— `gui_add_page "Configuration"`，下面所有参数都进这一页。

典型四种声明范式（各举一例）：

- **带范围的数字框**（`ClockDivider_g`）：
  [package.tcl:69-71](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/package.tcl#L69-L71) —— `gui_create_parameter` + `gui_parameter_set_range 4 1000000` + `gui_add_parameter`。`TransWidth_g`（[L73-75](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/package.tcl#L73-L75)，1..32）、`SlaveCnt_g`（[L88-90](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/package.tcl#L88-L90)，1..128）、`SpiDataPos_g`（[L115-117](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/package.tcl#L115-L117)，1..32）同属此类。
- **无范围的数字框**（`CsHighCycles_g`、`FifoDepth_g`）：[L77-78](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/package.tcl#L77-L78)、[L96-97](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/package.tcl#L96-L97) —— 只有 create + add，没有 range。
- **下拉框**（`SpiCPOL_g`/`SpiCPHA_g`/`MosiIdleState_g`/`ReadBitPol_g`/`TriStatePol_g`）：
  [package.tcl:80-82](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/package.tcl#L80-L82) —— `gui_parameter_set_widget_dropdown {0 1}`，限定只能选 0 或 1。
- **勾选框**（`LsbFirst_g`/`TriWiresSpi_g`）：
  [package.tcl:92-94](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/package.tcl#L92-L94)、[L107-109](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/package.tcl#L107-L109) —— `gui_parameter_set_widget_checkbox`，对应布尔 generic。

> 13 个 generic 的「控件类型」速查表：

| generic | package.tcl 控件 | 行号 |
|---|---|---|
| `ClockDivider_g` | 数字框，range 4..1000000 | [L69-71](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/package.tcl#L69-L71) |
| `TransWidth_g` | 数字框，range 1..32 | [L73-75](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/package.tcl#L73-L75) |
| `CsHighCycles_g` | 数字框，无 range | [L77-78](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/package.tcl#L77-L78) |
| `SpiCPOL_g` | 下拉 {0,1} | [L80-82](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/package.tcl#L80-L82) |
| `SpiCPHA_g` | 下拉 {0,1} | [L84-86](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/package.tcl#L84-L86) |
| `SlaveCnt_g` | 数字框，range 1..128 | [L88-90](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/package.tcl#L88-L90) |
| `LsbFirst_g` | 勾选框 | [L92-94](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/package.tcl#L92-L94) |
| `FifoDepth_g` | 数字框，无 range | [L96-97](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/package.tcl#L96-L97) |
| `MosiIdleState_g` | 下拉 {0,1} | [L99-101](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/package.tcl#L99-L101) |
| `ReadBitPol_g` | 下拉 {0,1} | [L103-105](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/package.tcl#L103-L105) |
| `TriWiresSpi_g` | 勾选框 | [L107-109](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/package.tcl#L107-L109) |
| `TriStatePol_g` | 下拉 {0,1} | [L111-113](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/package.tcl#L111-L113) |
| `SpiDataPos_g` | 数字框，range 1..32 | [L115-117](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/package.tcl#L115-L117) |

再看自动生成的 `xgui` 脚本如何排版与搬运值。`init_gui` 把每个参数挂到 `Configuration` 页：

[xgui init_gui：xgui/spi_simple_v1_4.tcl:2-21](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/xgui/spi_simple_v1_4.tcl#L2-L21) —— 每行一个 `ipgui::add_param ... -name "Xxx_g" -parent ${Configuration}`，下拉类参数额外带 `-widget comboBox`（如 [L9](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/xgui/spi_simple_v1_4.tcl#L9) 的 `SpiCPOL_g`）。这就是你在 Vivado 「Re-customize IP」对话框里看到的那一页字段的来源。

最关键的一类回调是 `update_MODELPARAM_VALUE.*`——它把用户填的 `PARAM_VALUE` 拷给模型用的 `MODELPARAM_VALUE`，完成第 2 节图里「第一层→第二层」的搬运：

[ClockDivider 搬运回调：xgui/spi_simple_v1_4.tcl:150-153](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/xgui/spi_simple_v1_4.tcl#L150-L153) —— 主体就一行 `set_property value [get_property value ${PARAM_VALUE.ClockDivider_g}] ${MODELPARAM_VALUE.ClockDivider_g}`，即「读用户的值，写到模型的值」。其余 12 个 generic 各有一个结构完全相同的回调（[L155-218](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/xgui/spi_simple_v1_4.tcl#L155-L218)）。还有一类 `validate_PARAM_VALUE.*`（[L27-30](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/xgui/spi_simple_v1_4.tcl#L27-L30) 起）目前都直接 `return true`，是预留给二次开发写自定义校验的钩子。

最后看 `component.xml` 里同一个参数的两副面孔。以 `ClockDivider_g` 为例：

- 用户层 `PARAM_VALUE`（带范围与默认）：
  [component.xml:1478-1482](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/component.xml#L1478-L1482) —— `spirit:resolve="user"`、`minimum="4"`、`maximum="1000000"`、默认 `4`，这是 GUI 字段的真身。
- 模型层 `MODELPARAM_VALUE`（由 `update_MODELPARAM_VALUE` 写入）：
  [component.xml:1215-1219](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/component.xml#L1215-L1219) —— `spirit:resolve="generated"`、`id="MODELPARAM_VALUE.ClockDivider_g"`，综合器读这个值当作 VHDL generic 实参。

下拉选项在 `<spirit:choices>` 里集中定义：`{0,1}` 枚举是 `choice_list_98b8ce5c`（[L1293-1297](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/component.xml#L1293-L1297)），被 `SpiCPOL_g`/`SpiCPHA_g` 引用（[L1496](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/component.xml#L1496)、[L1501](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/component.xml#L1501)）；`{"0","1"}` 比特串枚举是 `choice_list_b71134be`（[L1305-1309](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/component.xml#L1305-L1309)），被 `MosiIdleState_g`/`ReadBitPol_g`/`TriStatePol_g` 引用（[L1526](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/component.xml#L1526)、[L1531](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/component.xml#L1531)、[L1536](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/component.xml#L1536)）。注意 CPOL/CPHA 用整数 `0/1`，而三个 std_logic 参数用比特串 `"0"/"1"`——这与 4.1.5 练习 2 里的类型选择一一对应。

#### 4.2.4 代码实践

**源码追踪型实践**：跟踪 `ClockDivider_g` 一个值穿过全部三层。

1. **实践目标**：亲眼确认第 2 节那张「三层链」图在源码里的每一步都有对应代码。
2. **操作步骤**（纯阅读，不运行）：
   - 第1层（用户值）：[component.xml:1478-1482](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/component.xml#L1478-L1482) 的 `PARAM_VALUE.ClockDivider_g`，默认 `4`、范围 `4..1000000`。
   - 第1→2层（搬运）：[xgui/spi_simple_v1_4.tcl:150-153](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/xgui/spi_simple_v1_4.tcl#L150-L153) 的 `update_MODELPARAM_VALUE.ClockDivider_g` 把 `PARAM_VALUE` 拷给 `MODELPARAM_VALUE`。
   - 第2层（模型值）：[component.xml:1215-1219](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/component.xml#L1215-L1219) 的 `MODELPARAM_VALUE.ClockDivider_g`。
   - 第2→3层（综合实参）：Vivado 综合时把 `MODELPARAM_VALUE.ClockDivider_g` 作为 [spi_vivado_wrp.vhd:27](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L27) 的 generic 实参。
   - 第3层之后：wrapper 经 [spi_vivado_wrp.vhd:215](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L215) 透传给核心，核心经 [spi_simple.vhd:273](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_simple.vhd#L273) 改名传给引擎的 `clk_div_g`。
3. **需要观察的现象**：每一步都应有据可查；五处代码点串成一条无断裂的链。
4. **预期结果**：你能用一句话讲清「用户把 4 改成 8 之后，这个 8 是怎么变成引擎的 `clk_div_g=8` 的」。
5. 如果你本地有 Vivado，可把 IP 加进工程、改 `ClockDivider_g`、重新打包，再用文本工具 diff 重生成的 `component.xml` 看 `MODELPARAM_VALUE.ClockDivider_g` 是否变成新值。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `package.tcl` 里 `CsHighCycles_g` 没有 `gui_parameter_set_range`？这会带来什么后果？

**参考答案**：作者没给它显式范围，只靠 VHDL 的 `positive`（[spi_vivado_wrp.vhd:29](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L29)）兜底。后果是 GUI 不阻止用户填入 0 或负数，直到综合时才因违背 `positive` 报错——反馈链路变长。对比 `component.xml` 里它的 `minimum="0"`（[L1491](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/component.xml#L1491)），IP-XACT 自动生成的下界 0 比 VHDL 的 `positive`（≥1）还宽松一处，是另一处「三层不对齐」。

**练习 2**：`xgui` 里 `validate_PARAM_VALUE.ClockDivider_g` 现在只 `return true`（[xgui:36-39](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/xgui/spi_simple_v1_4.tcl#L36-L39)）。如果要强制 `ClockDivider_g` 必须是 2 的倍数，你会怎么改？

**参考答案**：在那个过程里读出值并判断模 2，例如（示例代码，非项目原有）`return [expr {[get_property value ${PARAM_VALUE.ClockDivider_g}] % 2 == 0}]`，不满足时返回 `false` 让 GUI 标红。这是 xgui 钩子留给二次开发的标准用法。注意：这类自定义校验只挡 GUI，不替代 VHDL 注释里的 `Must be a multiple of two` 约束。

### 4.3 端口条件使能机制

#### 4.3.1 概念说明

有些端口不是永远需要的——比如 4 线 SPI 用不到 3-Wire 的 `spi_tri` 三态控制线。如果让它永远挂在外围接口上，用户在 block design 里就得连一根没用的线，或者留空引发警告。

Vivado IP-XACT 的**端口条件使能（port enablement）**就是解决这个：给端口写一个「依赖表达式」，当表达式为真时端口出现、需要连接；为假时端口从外围接口隐去。本 IP 里唯一用到这个机制的端口就是 `spi_tri`，依赖条件是 `TriWiresSpi_g = true`。

这里有个容易混淆的点要讲清楚：

- `spi_tri` 在 **VHDL 实体里永远存在**（wrapper [spi_vivado_wrp.vhd:53](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L53)、核心 [spi_simple.vhd:76](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_simple.vhd#L76)，内部也永远连着 `SpiTri => spi_tri`，见 [spi_vivado_wrp.vhd:261](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L261)）。
- 条件使能只管**外围 IP 边界**：`TriWiresSpi_g=false` 时，Vivado 在 block design 里不显示 `spi_tri`、也不要求连接；这个输出在内部照常驱动，只是对外悬空（输出悬空无害）。`true` 时端口显露，用户必须把它连到外部三态缓冲。

#### 4.3.2 核心流程

声明端口条件使能只需在 `package.tcl` 里一行：

```
add_port_enablement_condition <端口名> <依赖表达式>
```

打包时 PsiIpPackage 把它翻译成 `component.xml` 里该端口 `<spirit:vendorExtensions>` 下的 `<xilinx:enablement>` 块，形如：

```xml
<xilinx:isEnabled xilinx:resolve="dependent"
                  xilinx:id="PORT_ENABLEMENT.<端口名>"
                  xilinx:dependency="<表达式>">false</xilinx:isEnabled>
```

综合时 Vivado 求值这个表达式（用当时的 `MODELPARAM_VALUE`），决定端口是否出现在外围接口。

与之相关的另一类参数化是**端口宽度依赖**：`spi_cs_n` 与 `spi_le` 的位宽随 `SlaveCnt_g` 变化。这不是「使能」而是「宽度表达式」，在 `component.xml` 里写作 `dependency="(spirit:decode(id('MODELPARAM_VALUE.SlaveCnt_g')) - 1)"`。两者一起构成「generic 如何塑形综合后的接口」。

#### 4.3.3 源码精读

先看 `package.tcl` 里唯一一行使能声明：

[spi_tri 使能条件：scripts/package.tcl:124](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/package.tcl#L124) —— `add_port_enablement_condition "spi_tri" "\$TriWiresSpi_g = true"`（`\$` 是 Tcl 转义，实际表达式是 `$TriWiresSpi_g = true`）。注意它紧跟在 `# Optional Ports` 注释块下，整个项目里只有 `spi_tri` 一个可选端口。

再看自动生成的 `component.xml` 里对应的使能块：

[spi_tri 端口使能：component.xml:529-551](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/component.xml#L529-L551) —— 关键是 [L546-548](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/component.xml#L546-L548) 的 `<xilinx:enablement>`，里面 `xilinx:id="PORT_ENABLEMENT.spi_tri"`、`xilinx:dependency="$TriWiresSpi_g = true"`、默认值 `false`（因为 `TriWiresSpi_g` 默认 `false`，见 [L1519-1522](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/component.xml#L1519-L1522)）。这正是 `package.tcl` 那一行打包后的落点。

对比一下「端口宽度依赖」长什么样：

[spi_cs_n 宽度依赖：component.xml:486-502](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/component.xml#L486-L502) —— [L491](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/component.xml#L491) 的 `spirit:left` 写着 `dependency="(spirit:decode(id('MODELPARAM_VALUE.SlaveCnt_g')) - 1)"`，即左边界 = `SlaveCnt_g - 1`。`spi_le` 用的是完全相同的表达式（[L552-568](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/component.xml#L552-L568)，[L557](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/component.xml#L557)）。这与 wrapper 里 `std_logic_vector(SlaveCnt_g-1 downto 0)`（[spi_vivado_wrp.vhd:50](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L50)、[L54](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L54)）是同一件事的两种表达：VHDL 用 generic 算位宽，IP-XACT 用 dependency 表达式告诉 Vivado 工具位宽也随这个参数变。

最后确认 `TriWiresSpi_g` **只**用在端口使能、不进核心：在 [spi_vivado_wrp.vhd:213-227](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L213-L227) 的核心 generic map 里找不到 `TriWiresSpi_g`——它是纯粹的「wrapper 级综合开关」。真正影响 3-Wire 电气行为的是 `ReadBitPol_g`/`TriStatePol_g`/`SpiDataPos_g` 三个，它们被透传进引擎（[spi_simple.vhd:280-282](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_simple.vhd#L280-L282)），这部分细节属于下一讲 [u3-l2 3-Wire SPI 扩展](./u3-l2-three-wire-spi.md)。

#### 4.3.4 代码实践

这正是学习规格里指定的实践：**选定 `TriWiresSpi_g = true`，说明它激活哪个端口、影响哪些 generic，并追踪 `package.tcl` 与 `component.xml` 中对应的声明位置**。

1. **实践目标**：把 `TriWiresSpi_g` 这个总开关的「牵连范围」一次性理清。
2. **操作步骤**（源码阅读）：
   - **激活的端口**：`spi_tri`。开关在 [package.tcl:124](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/package.tcl#L124)，IP-XACT 落点在 [component.xml:546-548](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/component.xml#L546-L548)。置 `true` 后，Vivado 在 block design 里显出 `spi_tri` 引脚要求连接。
   - **直接影响的 generic**：`TriWiresSpi_g` 自身不改变 RTL 行为（它不进核心），但 3-Wire 模式下用户通常还要配 `ReadBitPol_g`、`TriStatePol_g`、`SpiDataPos_g` 三个，它们的 GUI 声明分别在 [package.tcl:103-105](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/package.tcl#L103-L105)、[L111-113](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/package.tcl#L111-L113)、[L115-117](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/package.tcl#L115-L117)。
   - **内部连接不变**：无论开关真假，`spi_tri` 在 RTL 内部都由 `SpiTri => spi_tri` 驱动（[spi_vivado_wrp.vhd:261](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L261)）。
3. **需要观察的现象**：`TriWiresSpi_g` 在整个 `hdl/` 目录里**只**出现在 wrapper 实体声明（[L35](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L35)），不出现在任何 generic map 里——印证它是纯综合开关。
4. **预期结果**：你能对同事说清——「勾上 3-Wire，只会让 `spi_tri` 端口显现；真正决定三线行为的是另外三个极性/位置 generic，它们一直被透传到引擎。」
5. **待本地验证**：在 Vivado 里把 `TriWiresSpi_g` 在 false/true 间切换，观察 block design 里 `spi_tri` 引脚的出现与消失。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `spi_tri` 的使能条件从 `TriWiresSpi_g` 改成依赖 `SlaveCnt_g > 1`，`package.tcl` 和 `component.xml` 各要改哪里？

**参考答案**：`package.tcl` 把 [L124](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/package.tcl#L124) 的表达式改成 `"\$SlaveCnt_g > 1"`，然后重新打包；`component.xml` 会自动重生成 [L547](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/component.xml#L547) 的 `xilinx:dependency` 为 `$SlaveCnt_g > 1`。**不要手改 `component.xml`**——它是产物，下次打包会被覆盖。

**练习 2**：`spi_tri` 是输出端口。`TriWiresSpi_g=false` 时它被禁用、对外悬空，这会引发综合警告或错误吗？

**参考答案**：不会报错。VHDL 里输出端口不接外部是合法的（悬空输出只是其驱动值不被使用）。这与输入端口不同——输入若被禁用，Vivado 通常需要给它一个默认驱动值（`component.xml` 里 `spi_tri` 带 `<spirit:defaultValue>0</spirit:defaultValue>`，[L540-542](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/component.xml#L540-L542)），正是为禁用时的 tie-off 预留的。

## 5. 综合实践

把三个模块串起来，做一次「给定需求 → 推出全套 generic 与综合后接口」的纸面配置。

**需求**：为一个有 **4 个从机**、每帧 **16 位**、要求 **3-Wire SPI**（共用单线收发）的子系统配置本 IP；AXI 时钟 100 MHz，期望 SCK 约 5 MHz。

**任务**：

1. **定时序参数**：由 \( f_{\text{SCK}} \approx f_{\text{Clk}}/\text{ClockDivider\_g} \)，100 MHz / 5 MHz = 20，且须为 2 的倍数——取 `ClockDivider_g = 20`。CPOL/CPHA 按你的从机手册定，假设 Mode 0 → `SpiCPOL_g=0`、`SpiCPHA_g=0`。
2. **定数据/拓扑参数**：`TransWidth_g = 16`、`SlaveCnt_g = 4`、`LsbFirst_g = false`、`FifoDepth_g = 256`（默认即可）。
3. **定 3-Wire 参数**：`TriWiresSpi_g = true`（激活 `spi_tri`），并按从机协议设 `ReadBitPol_g`、`TriStatePol_g`、`SpiDataPos_g`（参考 [u3-l2](./u3-l2-three-wire-spi.md)）。
4. **预测综合后的外围接口**：
   - `spi_cs_n` 位宽 = `SlaveCnt_g` = 4（依据 [component.xml:491](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/component.xml#L491) 的宽度表达式）。
   - `spi_le` 位宽 = 4（同上，[L557](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/component.xml#L557)）。
   - `spi_tri` **出现**（因 `TriWiresSpi_g=true`，[L547](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/component.xml#L547)）。
   - `spi_sck`/`spi_mosi`/`spi_miso`/`irq`/AXI 信号照常。
5. **验证三层一致**：对照 [hdl/spi_vivado_wrp.vhd:24-43](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L24-L43) 与 [scripts/package.tcl:69-117](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/package.tcl#L69-L117)，确认你选的每个值都落在 wrapper 的 `range` 与 `package.tcl` 的 GUI 范围内（例如 `TransWidth_g=16` 在 1..32 内、`SlaveCnt_g=4` 在 1..128 内）。

**交付物**：一张「generic 名 = 值」的配置表，加一段「综合后端口清单（含位宽与是否使能）」。完成后，你就把「取值范围」「GUI 声明」「端口使能」三件事在一个真实配置里打通了。**待本地验证**：在 Vivado 里按此表定制 IP，核对显出的端口与位宽是否与你预测一致。

## 6. 本讲小结

- 本 IP 共 13 个用户可配 generic，分**时序 / 数据格式 / 拓扑 / 3-Wire 扩展**四组；权威取值范围写在 [hdl/spi_vivado_wrp.vhd:24-43](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L24-L43) 的 VHDL 子类型约束里。
- 一个 generic 值要走**三层**：GUI 的 `PARAM_VALUE`（[component.xml:1477-1552](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/component.xml#L1477-L1552)）→ 经 xgui 回调（[xgui/spi_simple_v1_4.tcl:150-218](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/xgui/spi_simple_v1_4.tcl#L150-L218)）拷成 `MODELPARAM_VALUE`（[component.xml:1214-1285](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/component.xml#L1214-L1285)）→ 作为 VHDL generic 实参，再经 wrapper→核心→引擎两层 generic map 透传。
- `package.tcl` 是参数化的**唯一数据源**：`gui_create_parameter` 声明、`gui_parameter_set_range`/`_widget_dropdown`/`_widget_checkbox` 定控件、`gui_add_parameter` 入页（[scripts/package.tcl:67-117](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/package.tcl#L67-L117)）；`xgui/*.tcl` 与 `component.xml` 都是自动产物，勿手改。
- `spi_tri` 端口靠 `add_port_enablement_condition "spi_tri" "$TriWiresSpi_g = true"`（[package.tcl:124](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/package.tcl#L124)）条件使能，落点在 [component.xml:546-548](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/component.xml#L546-L548)；`TriWiresSpi_g` 是纯 wrapper 级综合开关，不进核心。
- generic 还能驱动**端口宽度**：`spi_cs_n`/`spi_le` 位宽随 `SlaveCnt_g` 变（[component.xml:491](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/component.xml#L491)、[L557](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/component.xml#L557)）。
- 阅读时要警惕「三层不对齐」：`SpiDataPos_g` 默认值在 wrapper(8) 与核心(3) 不一致；`CsHighCycles_g`/`FifoDepth_g` 在 GUI 无范围，仅靠 VHDL `positive` 兜底。

## 7. 下一步学习建议

- **下一讲 [u3-l2 3-Wire SPI 与三态/读写位扩展](./u3-l2-three-wire-spi.md)**：本讲只点到 `spi_tri` 的使能与三个 3-Wire generic 的存在；下一讲会钻进 `ReadBitPol_g`/`TriStatePol_g`/`SpiDataPos_g` 如何在 `psi_common_spi_master` 里决定共用单线的收发时序与 RW 位极性。
- **[u3-l3 LE 锁存使能输出时序](./u3-l3-latch-enable-output.md)**：看另一类「随 generic 变位宽」的端口 `spi_le` 的语义与 testbench 断言。
- **[u3-l4 IP 打包与发布流程](./u3-l4-ip-packaging.md)**：想动手新增一个 generic 时该改哪些地方（实体声明、`package.tcl` 的 create/range/widget、重打包后 `component.xml` 与 `xgui` 自动更新），那是对打包流程的完整走查。
- 继续精读的源码：`scripts/package.tcl` 全文（很短，是最好的 PsiIpPackage 用法示例），以及对照 `component.xml` 的 `<spirit:modelParameters>` / `<spirit:parameters>` / `<spirit:choices>` 三段，巩固「三层值」的机器可读表达。
