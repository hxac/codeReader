# 端口与 generic 标签详解

## 1. 本讲目标

上一讲（u2-l1）你学会了「标签长什么样、`_ParseTags` 怎么把它翻成字典、`HasTag/GetTag/FilterForTag` 怎么查询」。但那些都还只是**语法层**——本讲进入**语义层**：每一个具体的端口/generic 标签，到底**改变了生成结果的哪一段**。

学完本讲你应当能够：

- 解释 `TYPE=CLK` 配合 `FREQ` 如何驱动 `_Clocks` 生成时钟进程，并能算出半周期长度。
- 解释 `TYPE=RST` 配合 `CLK` 如何驱动 `_Resets` 生成复位进程，以及 `CLK` 标签如何把复位「归属」到某个时钟域。
- 解释 `PROC` 标签如何把端口绑定到某个测试过程，并知道它的可见效果出现在多用例 TB 中。
- 解释 `EXPORT` 与 `CONSTANT` 这两个 generic 标签如何决定 generic 在 testbench 里被「导出 / 固定 / 用默认值」三种处理方式。
- 解释 `GetPortValue` 与 `LOWACTIVE` 如何统一计算端口初始值，并被 `_DutSignals`、`_Resets` 等多处复用。

本讲对应的 5 个最小模块是：`GetPortValue`、`_Clocks`、`_Resets`、`_GenericConstants`、`_DutSignals`，并辅以 `PROC` 的绑定机制。

## 2. 前置知识

在进入本讲前，请确认你已经具备以下直觉（来自 u2-l1、u1-l3）：

- **标签是输入契约**：设计者在 VHDL 注释里用 `$$ 键=值 $$` 描述测试意图；标签名大小写不敏感、值保留原大小写。
- **三连招查询模式**：生成器消费标签的固定套路是「`FilterForTag` 筛选 → `HasTag` 检查 → `GetTag` 取值」。本讲会反复看到这个套路。
- **生成的段落顺序**（来自 u1-l3）：`Generate` 按固定顺序写出 Header → 库声明 → 实体 → 架构（常量 / 控制信号 / DUT 信号）→ DUT 实例化 → TB 控制 → 时钟 → 复位 → 进程。本讲的各个 `_Xxx()` 方法就分布在这条链路上。
- **两类容易混淆的标签**：端口级的 `PROC`（单数，作用于端口）和文件级的 `PROCESSES`（复数，定义过程名列表）。本讲讲前者，后者属于 u2-l3。

一个贯穿全讲的**心智模型**：

> **标签是方向盘，生成器方法是发动机。** 方向盘（标签）决定「要不要生成、生成几份、用什么参数」；发动机（`_Clocks`/`_Resets`/...）只负责按既定模板把 VHDL 文本写出来。理解一个标签，就是理解它**拨动了哪台发动机的哪个开关**。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
|------|------|-----------|
| [DutInfo.py](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/DutInfo.py) | DUT 数据模型 | `Tags` 常量、`GetPortValue`（初始值计算） |
| [TbGen.py](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py) | 生成器主类 | `_Clocks`、`_Resets`、`_GenericConstants`、`_DutSignals`、`_DutInstantiation`、`_EntityDeclaration` |
| [TbInfo.py](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbInfo.py) | TB 模型 | `GetPortsForProcess`（PROC 绑定入口） |
| [example/simpleTb/psi_common_async_fifo.vhd](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/example/simpleTb/psi_common_async_fifo.vhd) | 示例 DUT | 真实的 `TYPE`/`FREQ`/`CLK`/`PROC`/`EXPORT`/`CONSTANT` 标签 |

## 4. 核心概念与源码讲解

### 4.1 `GetPortValue` 与 `LOWACTIVE`：信号初始值是如何算出来的

#### 4.1.1 概念说明

testbench 里每个与 DUT 端口相连的信号都需要一个**初始值**。比如复位信号刚上电时应该是「有效」还是「无效」？一个普通的 `std_logic` 输出，TB 里的对应信号又该初始化成 `'0'` 还是 `'1'`？

这件事看起来简单，却有一个隐藏的复杂度：**「有效」与 `'\1'` 并不总是等价**。大多数信号是**高有效**（active-high，`'1'` 表示有效），但复位、使能等信号常常是**低有效**（active-low，`'0'` 表示有效）。

`DutInfo.GetPortValue(port, active)` 就是统一解决这个问题的方法：给它一个端口和一个「我想要有效还是无效」的布尔值，它就返回对应的 VHDL 字面量字符串。

- `LOWACTIVE=true` 标签：告诉生成器「这个端口低有效」，于是「有效」对应 `'0'`、「无效」对应 `'1'`。
- 没有 `LOWACTIVE`（默认）：高有效，「有效」对应 `'1'`、「无效」对应 `'0'`。

> 注意：`LOWACTIVE` 在 `Tags` 常量类里[有声明](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/DutInfo.py#L22)，且被 `GetPortValue` 使用，但**示例 DUT 里并没有任何端口真的写了 `LOWACTIVE=true`**——示例里所有信号都是高有效。所以本讲的综合实践会让你「亲手给一个端口加上 `LOWACTIVE=true`」，正是为了观察这个**示例里沉睡**的功能。

#### 4.1.2 核心流程

`GetPortValue(port, active)` 的决策表：

| `LOWACTIVE` 标签 | `active=True`（要有效） | `active=False`（要无效） |
|------------------|------------------------|-------------------------|
| 无 / `false`（高有效，默认） | `'1'` | `'0'` |
| `true`（低有效） | `'0'` | `'1'` |

随后根据端口**类型**包装返回值：

```
std_logic          → 直接返回 '0' 或 '1'
std_logic_vector   → 返回 (others => '0') 或 (others => '1')
其它类型           → 抛 UnknownVhdlType 异常
```

调用方只需说「我要这个端口有效 / 无效」，完全不用关心高低有效与位宽——这正是 `GetPortValue` 的价值：**把「逻辑意图」翻译成「具体字面量」**。

#### 4.1.3 源码精读

```python
def GetPortValue(self, port : VhdlPortDeclaration, active : bool):
    #Find initial value
    if DutInfo.HastTagValue(port, Tags.LOWACTIVE, "true"):   # ① 低有效？
        initVal = "'0'" if active else "'1'"
    else:                                                     # ② 默认高有效
        initVal = "'1'" if active else "'0'"
    if port.type.name == "std_logic":                         # ③ 标量
        return initVal
    elif port.type.name == "std_logic_vector":                # ④ 向量
        return "(others => {})".format(initVal)
    else:                                                     # ⑤ 不认识的类型
        raise UnknownVhdlType("Unknown VHDL Type {}".format(port.type.name))
```

> 见 [DutInfo.py:68-79](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/DutInfo.py#L68-L79)。注意 ① 处用的是上一讲讲过的 `HastTagValue`（默认大小写不敏感），所以 `LOWACTIVE=true` / `True` 都能识别。

`GetPortValue` 是**被复用最多的标签消费者**，至少出现在四个生成方法里：

| 调用方 | 代码位置 | 传的 `active` | 含义 |
|--------|---------|--------------|------|
| `_DutSignals` | [TbGen.py:181-186](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L181-L186) | 时钟/复位 `True`，其它 `False` | 信号声明初值 |
| `_Resets` | [TbGen.py:80](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L80) | `False` | 复位释放后驱动到「无效」 |
| `_Processes`（单用例） | [TbGen.py:109](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L109) | `False` | `wait until` 复位无效的条件 |
| `_TbControl` | [TbGen.py:128](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L128) | `False` | 同上 |

这意味着：**一旦你给某个复位端口加了 `LOWACTIVE=true`，以上四处产生的 `'0'`/`'1'` 字面量会同时翻转，保持语义一致**（复位始终是「先有效、后释放到无效」）。这是综合实践里你会亲眼看到的 diff。

#### 4.1.4 代码实践

**实践目标**：建立「逻辑意图 → 字面量」的直觉，并预测 `LOWACTIVE` 的影响面。

**操作步骤**：

1. 打开 [DutInfo.py 的 GetPortValue](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/DutInfo.py#L68-L79) 与示例 [OutRst 端口（第 42 行）](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/example/simpleTb/psi_common_async_fifo.vhd#L42)。
2. 假设 `OutRst` 当前是高有效（无 `LOWACTIVE`）。手工算出 `GetPortValue(OutRst, True)` 与 `GetPortValue(OutRst, False)`。
3. 再假设给 `OutRst` 加上 `LOWACTIVE=true`，重新算这两个值。
4. 对照 4.1.3 的「调用方」表格，列出哪些生成方法的输出会因此改变。

**需要观察的现象**：

- 高有效时，`active=True`→`'1'`、`active=False`→`'0'`；低有效时正好相反。
- 同一个 `LOWACTIVE=true` 改动会同时影响 `_DutSignals`（信号初值）、`_Resets`（释放值）、`_Processes` 与 `_TbControl`（等待条件）四处的字面量。

**预期结果**：

- 当前（高有效）：`GetPortValue(OutRst, True) = "'1'"`，`GetPortValue(OutRst, False) = "'0'"`。
- 加 `LOWACTIVE=true` 后：`GetPortValue(OutRst, True) = "'0'"`，`GetPortValue(OutRst, False) = "'1'"`。

#### 4.1.5 小练习与答案

**练习 1**：一个 `std_logic_vector(7 downto 0)` 的端口，没有 `LOWACTIVE`，`GetPortValue(port, False)` 返回什么？

> **答案**：高有效 + 无效 → `initVal = "'0'"`，再按向量包装成 `"(others => '0')"`。

**练习 2**：为什么 `GetPortValue` 对不认识的类型（如 `integer`）要抛 `UnknownVhdlType`，而不是返回某个默认值？

> **答案**：和 `GetTag` 的「快速失败」同理——如果静默返回默认值，生成的 TB 会出现错误的初始值而不报错，bug 会潜伏到仿真阶段才暴露。抛异常能立刻提示「这个类型生成器还不会处理」，迫使设计者要么换类型、要么扩展生成器（见 u6-l3 的扩展实践）。

---

### 4.2 `TYPE` 角色标签：`_Clocks`（CLK+FREQ）与 `_Resets`（RST+CLK）

#### 4.2.1 概念说明

`TYPE` 是端口最重要的标签，它告诉生成器这个端口在 testbench 里**扮演什么角色**。`Tags` 类里注释写得很清楚：

```python
TYPE = "type"   #CLK, RST, SIG
```

> 见 [DutInfo.py:23](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/DutInfo.py#L23)。`TYPE` 的取值是 `CLK`、`RST`、`SIG`（普通信号）。没有写 `TYPE` 的端口默认就是 `SIG`。

- `TYPE=CLK`：这是一个时钟端口。生成器会为它**单独创建一个时钟进程** `p_clock_<名字>`，让它自动翻转。它还必须配一个 `FREQ` 标签说明频率。
- `TYPE=RST`：这是一个复位端口。生成器会为它**单独创建一个复位进程** `p_rst_<名字>`，让它上电后保持有效一小段时间再释放。它还必须配一个 `CLK` 标签，说明复位属于哪个时钟域。

注意 `CLK` 与 `RST` 的「搭档标签」不同：**时钟用 `FREQ`（一个数值），复位用 `CLK`（一个对其它端口的引用）**。这是一个容易记混的点，下面分别讲。

#### 4.2.2 核心流程

**时钟**（`_Clocks`）：

```
对每个 TYPE=CLK 的端口 clk：
  1. 必须有 FREQ 标签，否则抛 "Clock <名字> has not FREQ tag!"
  2. 生成进程 p_clock_<clk.name>：
       constant Frequency_c : real := real(<FREQ>);
       while TbRunning loop
           wait for 0.5*(1 sec)/Frequency_c;   -- 半周期
           <clk.name> <= not <clk.name>;        -- 翻转
       end loop;
```

半周期的推导：设频率为 \( f \)，则周期 \( T = 1/f \)，半周期

\[
T_{\text{half}} = \frac{0.5}{f}
\]

示例里 `InClk` 的 `FREQ=100e6`（100 MHz）：

\[
T_{\text{half}} = \frac{0.5}{100\times10^{6}}\,\text{s} = 5\times10^{-9}\,\text{s} = 5\,\text{ns}
\]

`OutClk` 的 `FREQ=125e6`（125 MHz）：半周期为 \( 0.5/125\times10^{6} = 4\,\text{ns} \)。两个时钟各自独立翻转，互不同步——这正是「异步 FIFO」测试台需要的场景。

**复位**（`_Resets`）：

```
对每个 TYPE=RST 的端口 rst：
  1. 必须有 CLK 标签，否则抛 "Reset <名字> has not CLK tag!"
  2. clkName = GetTag(rst, CLK)          -- 复位归属的时钟名
  3. 生成进程 p_rst_<rst.name>：
       wait for 1 us;                      -- 先保持（信号初值已使其有效）
       -- 等两个时钟上升沿，保证复位至少覆盖一个边沿
       wait until rising_edge(<clkName>);
       wait until rising_edge(<clkName>);
       <rst.name> <= <GetPortValue(rst, False)>;   -- 释放到「无效」
```

`CLK` 标签在这里的作用是**复位归属**：复位进程不是凭空等待固定时间，而是**等到它所属时钟域的两个上升沿之后**才释放。这保证复位信号在被释放前，至少被该时钟采样过一次，避免仿真里的亚稳态/漏采。

#### 4.2.3 源码精读

**`_Clocks`**——典型的「筛选→检查→取值→套模板」：

```python
def _Clocks(self, f : FileWriter) -> FileWriter:
    VhdlTitle("Clocks !DO NOT EDIT!", f)
    for clk in DutInfo.FilterForTag(self.dutInfo.ports, Tags.TYPE, "clk"):  # 筛选时钟
        if not DutInfo.HasTag(clk, Tags.FREQ):                              # 检查 FREQ
            raise Exception("Clock {} has not FREQ tag!".format(clk.name))
        f.WriteLn("p_clock_{} : process".format(clk.name)).IncIndent()
        f.WriteLn("constant Frequency_c : real := real({});".format(DutInfo.GetTag(clk, Tags.FREQ))).DecIndent()  # 取 FREQ
        f.WriteLn("begin").IncIndent()
        f.WriteLn("while TbRunning loop").IncIndent()
        f.WriteLn("wait for 0.5*(1 sec)/Frequency_c;")                      # 半周期 = 0.5/f
        f.WriteLn("{name} <= not {name};".format(name=clk.name))             # 翻转
        f.DecIndent().WriteLn("end loop;")
        f.WriteLn("wait;").DecIndent()
        f.WriteLn("end process;")
        f.WriteLn()
    return f
```

> 见 [TbGen.py:51-66](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L51-L66)。注意 `real({})` 把 `FREQ` 标签值原样塞进 VHDL，所以 `FREQ=100e6` 在 VHDL 里就是合法的 `real(100e6)`——生成器不做单位换算，全靠 VHDL 的 `1 sec / Frequency_c` 在仿真时算出时间。

**`_Resets`**——结构几乎相同，只是把 `FREQ` 换成 `CLK`（引用另一个端口）：

```python
def _Resets(self, f : FileWriter) -> FileWriter:
    VhdlTitle("Resets", f)
    for rst in DutInfo.FilterForTag(self.dutInfo.ports, Tags.TYPE, "rst"):  # 筛选复位
        if not DutInfo.HasTag(rst, Tags.CLK):                              # 检查 CLK
            raise Exception("Reset {} has not CLK tag!".format(rst.name))
        clkName = DutInfo.GetTag(rst, Tags.CLK)                           # 取归属时钟名
        f.WriteLn("p_rst_{} : process".format(rst.name))
        f.WriteLn("begin").IncIndent()
        f.WriteLn("wait for 1 us;")
        f.WriteLn("-- Wait for two clk edges to ensure reset is active for at least one edge")
        f.WriteLn("wait until rising_edge({});".format(clkName))
        f.WriteLn("wait until rising_edge({});".format(clkName))
        f.WriteLn("{} <= {};".format(rst.name, self.dutInfo.GetPortValue(rst, False)))  # 释放到无效
        f.WriteLn("wait;").DecIndent()
        f.WriteLn("end process;")
        f.WriteLn()
    return f
```

> 见 [TbGen.py:68-84](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L68-L84)。第 80 行直接复用了 4.1 的 `GetPortValue(rst, False)`，所以 `LOWACTIVE` 同样会影响复位释放值。

回到示例的真实标签：

```vhdl
InClk  : in std_logic; -- $$ TYPE=CLK; FREQ=100e6; PROC=Input $$
InRst  : in std_logic; -- $$ TYPE=RST; CLK=InClk $$
OutClk : in std_logic; -- $$ TYPE=CLK; FREQ=125e6; Proc=Output $$
OutRst : in std_logic; -- $$ TYPE=RST; CLK=OutClk $$
```

> 见 [example/simpleTb/psi_common_async_fifo.vhd:39-42](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/example/simpleTb/psi_common_async_fifo.vhd#L39-L42)。于是生成器会产出 `p_clock_InClk`、`p_clock_OutClk` 两个时钟进程，以及 `p_rst_InRst`（等 `InClk` 两个上升沿后释放）、`p_rst_OutRst`（等 `OutClk` 两个上升沿后释放）两个复位进程。`InRst`/`OutRst` 各自归属到不同的时钟域，正是异步 FIFO 的写照。

#### 4.2.4 代码实践

**实践目标**：把「`TYPE` 标签 → 生成的进程」对应起来，并验证频率换算。

**操作步骤**：

1. 在示例 DUT 中确认两个时钟端口（[第 39、41 行](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/example/simpleTb/psi_common_async_fifo.vhd#L39-L41)）和两个复位端口（[第 40、42 行](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/example/simpleTb/psi_common_async_fifo.vhd#L40-L42)）。
2. 手算 `InClk` 与 `OutClk` 的半周期（ns）。
3. 预测生成的 testbench 里会有几个 `p_clock_*`、几个 `p_rst_*`，每个复位进程等待的是哪个时钟。

**需要观察的现象**：

- 时钟进程数量 = `TYPE=CLK` 端口数量；复位进程数量 = `TYPE=RST` 端口数量。
- 复位进程里 `rising_edge(...)` 引用的时钟名，正好等于该复位端口 `CLK=` 标签的值。

**预期结果**：`InClk` 半周期 5 ns，`OutClk` 半周期 4 ns；生成 `p_clock_InClk`、`p_clock_OutClk`、`p_rst_InRst`（引用 `InClk`）、`p_rst_OutRst`（引用 `OutClk`）。

> 若想实测，可运行 `py ..\..\TbGen.py -src .\psi_common_async_fifo.vhd -dst .\tb -clear -force`（见 [run.bat](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/example/simpleTb/run.bat)），再在 `tb/*.vhd` 里搜索 `p_clock_` 与 `p_rst_`。运行结果待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：如果删掉 `InClk` 上的 `FREQ` 标签，重新生成会发生什么？

> **答案**：`_Clocks` 在 [TbGen.py:54-55](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L54-L55) 检查到 `HasTag(clk, FREQ)` 为假，抛出 `Exception("Clock InClk has not FREQ tag!")`，CLI 捕获后打印 `ERROR: ...` 并 `exit(-1)`（见 [TbGen.py:312-314](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L312-L314)）。这是「时钟必须声明频率」这一硬约束的体现。

**练习 2**：为什么复位用 `CLK`（引用别的端口）而不是 `FREQ`（自己声明频率）？

> **答案**：复位进程本身不翻转、不需要频率，它只需要知道「等哪个时钟的上升沿」。所以它引用一个已经存在的时钟端口名（`CLK=InClk`），复用那个时钟的边沿。如果改成 `FREQ`，复位进程就得自己重新造一个时钟，既冗余又会和真正的时钟不同步。

---

### 4.3 `PROC` 标签：端口如何绑定到测试过程

#### 4.3.1 概念说明

一个 testbench 通常不止一个测试过程（process）。示例里就有 `Input`、`Output` 两个过程（由文件级 `PROCESSES=Input,Output` 定义，详见 u2-l3）。那么「某个端口归哪个过程管」由谁决定？答案就是端口级的 `PROC` 标签。

```vhdl
InData : in std_logic_vector(Width_g-1 downto 0); -- $$ PROC=Input $$
OutRdy : in std_logic := '1';                     -- $$ PROC=Output,Input $$
```

- `PROC=Input`：这个端口属于 `Input` 过程。
- `PROC=Output,Input`（列表值）：这个端口**同时**属于 `Output` 和 `Input` 两个过程（典型如握手信号 `OutRdy`，输入侧要驱动它、输出侧也要观察它）。

需要特别澄清一个**容易误解的点**：在**单用例** testbench（也就是 simpleTb 这种，没有 `TESTCASES` 文件级标签的情况）里，`PROC` 标签**不会改变生成的进程体**——所有端口都被 `_DutSignals` 统一声明为架构级信号，任何过程都能直接访问它们。`PROC` 的可见效果只出现在**多用例** testbench 中：那里每个过程会变成一个带参数的 procedure，而 `PROC` 正是用来决定「这个过程的过程参数列表里有哪些端口」。

#### 4.3.2 核心流程

`PROC` 的绑定入口在 `TbInfo.GetPortsForProcess`，它把「过程名」翻译成「端口列表」：

```
GetPortsForProcess(processName):
    return FilterForTag(dutInfo.ports, PROC, processName)
```

这复用了上一讲的 `FilterForTag`：它遍历所有端口，留下 `PROC` 标签值（可能是单值也可能是列表）里包含 `processName` 的那些。于是一个端口的 `PROC=Output,Input` 会让它**同时**出现在 `GetPortsForProcess("Output")` 和 `GetPortsForProcess("Input")` 的结果里。

这个端口列表在多用例 TB 里被 `_Processes` 拼成 procedure 调用的实参：

```
args = ", ".join(port.name for port in GetPortsForProcess(p))
work.<tb>_case_<case>.<p>(<args>, Generics_c);
```

#### 4.3.3 源码精读

绑定入口（一行就够）：

```python
def GetPortsForProcess(self, process : str) -> List[VhdlPortDeclaration]:
    return DutInfo.FilterForTag(self.dutInfo.ports, Tags.PROC, process)
```

> 见 [TbInfo.py:47-48](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbInfo.py#L47-L48)。注意它查询的是端口级 `Tags.PROC`（`"proc"`，单数），不是文件级 `Tags.PROCESSES`（`"processes"`，复数）——两者一字之差，作用对象完全不同。

它的唯一消费点（在 `_Processes` 的多用例分支里）：

```python
if self.tbInfo.isMultiCaseTb:
    for i, c in enumerate(self.tbInfo.testCases):
        ...
        args = ", ".join(port.name for port in self.tbInfo.GetPortsForProcess(p))   # ← PROC 在这里生效
        f.WriteLn("work.{tb}_case_{case}.{proc}({args}, Generics_c);".format(...))
```

> 见 [TbGen.py:96-104](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L96-L104)。而单用例分支（[TbGen.py:105-116](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L105-L116)）里**没有**调用 `GetPortsForProcess`——这就是「单用例 TB 里 PROC 不影响进程体」的代码根源。

> 小结：`PROC` 还有一处消费在 `MultiFileTb.py`（[第 49 行](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/MultiFileTb.py#L49) 的 `GetTagAsList(port, Tags.PROC)` 与 [第 72、83 行](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/MultiFileTb.py#L72-L83)），用于推断多用例 procedure 的参数方向，那属于 u5-l2 的内容，本讲先不展开。

#### 4.3.4 代码实践（源码阅读型）

**实践目标**：在示例 DUT 里把每个端口的 `PROC` 归属理清楚，并验证「一个端口可属于多个过程」。

**操作步骤**：

1. 列出示例 DUT 里所有写了 `PROC` 的端口（[第 39、41、45-47、50-52、62 行](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/example/simpleTb/psi_common_async_fifo.vhd#L39-L62)）。
2. 为 `Input`、`Output` 两个过程，分别写出 `GetPortsForProcess` 应返回的端口列表。
3. 找出哪些端口**同时**属于两个过程。

**需要观察的现象**：

- `PROC=Output,Input` 的 `OutRdy` 同时进入两个过程的列表。
- 没有写 `PROC` 的端口（如 `InFull`、`OutEmpty` 等状态端口）不会进入任何过程列表。
- 标签值的大小写不影响归属（`PROC=INPUT`、`PROC=input` 都归到 `Input` 过程）。

**预期结果**（部分）：

- `GetPortsForProcess("Input")` 含 `InClk`、`InData`、`InVld`、`InRdy`、`OutRdy`、`OutFull`。
- `GetPortsForProcess("Output")` 含 `OutClk`、`OutData`、`OutVld`、`OutRdy`、`OutFull`。
- `OutRdy` 与 `OutFull` 同时出现在两个列表里。

> 由于示例是单用例 TB，这些归属不会改变生成的 `tb/*.vhd` 的进程体。要看到 `PROC` 的实际效果，需要进入多用例模式（加 `TESTCASES` 文件级标签），那是 u5 的主题。若想立刻验证绑定逻辑本身，可手写 `FilterForTag(ports, "proc", "Input")` 调用——待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`PROC`（端口级，单数）与 `PROCESSES`（文件级，复数）有什么区别？

> **答案**：`PROCESSES` 写在文件级注释里，定义 testbench **有哪些过程**（如 `PROCESSES=Input,Output`），由 `TbInfo.__init__` 读进 `tbProcesses`（[TbInfo.py:26-30](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbInfo.py#L26-L30)）。`PROC` 写在端口行尾，定义**某个端口属于哪些过程**。前者枚举过程清单，后者把端口挂到清单里的某些过程上。`PROCESSES` 详见 u2-l3。

**练习 2**：为什么说单用例 TB 里 `PROC` 是「沉睡的」标签？

> **答案**：单用例 TB 的 `_Processes` 分支（[TbGen.py:105-116](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L105-L116)）不调用 `GetPortsForProcess`，所有端口都以架构级信号形式存在，任何过程都能直接用。`PROC` 标签虽然被解析进字典，但没有任何生成方法消费它，所以不影响输出。只有进入多用例 TB（procedure 化）后它才「醒来」。

---

### 4.4 generic 标签 `EXPORT` 与 `CONSTANT`：`_GenericConstants` 与 generic map 联动

#### 4.4.1 概念说明

DUT 的 generic（类属参数）在 testbench 里有三种截然不同的命运，由两个 generic 标签决定：

- **`EXPORT=true`**：把这个 generic **导出**为 testbench 实体的 generic，让使用这个 TB 的人可以在例化 TB 时再决定它的值。同时它也会进入 DUT 的 `generic map`。
- **`CONSTANT=值`**：把这个 generic **固定**为一个常量（用标签里给的值，而不是 DUT 声明的默认值），同样进入 DUT 的 `generic map`。
- **两个都没有**：这个 generic 既不导出也不固定，生成器用 DUT 声明里的**默认值**把它变成一个 TB 内部常量，**不**进入 `generic map`。

这三种处理方式分别服务于不同的测试需求：导出的 generic 让 TB 可参数化复用；固定的 generic 用于「在这个 TB 里就锁定某个配置」；用默认值的 generic 则是「这个参数对测试不重要，按 DUT 默认走」。

> 一个常被忽略的细节：`EXPORT` 标签的值必须是字符串 `"true"`（大小写不敏感）才算导出。`EXPORT=false` 或 `EXPORT=blubb` 都**不算**导出——生成器只认 `"true"`。

#### 4.4.2 核心流程

生成器对 generics 的分类发生在三个方法里，它们用的是同一组 `FilterForTag`：

```
gConst = FilterForTag(generics, CONSTANT)              # 所有有 CONSTANT 标签的
gExp   = FilterForTag(generics, EXPORT, "true")        # 所有 EXPORT=true 的
其它   = generics 里既非 gConst 也非 gExp 的
```

三类的去向：

| 分类 | 判定 | 在 `_GenericConstants` 里 | 在 `_EntityDeclaration` 里 | 在 `_DutInstantiation` 的 `generic map` 里 |
|------|------|--------------------------|---------------------------|-------------------------------------------|
| 导出 | `EXPORT=true` | 多用例下进入 `Generics_c` 记录 | ✅ 作为 TB 实体 generic | ✅ |
| 固定 | 有 `CONSTANT` | ✅ `constant X : T := <CONSTANT值>;` | ❌ | ✅ |
| 默认 | 两者皆无、且有 default | ✅ `constant X : T := <DUT默认值>;` | ❌ | ❌ |

关键点：**只有「导出」和「固定」两类进入 `generic map`**，「默认」类不进入（它只是 TB 内部常量）。

#### 4.4.3 源码精读

**`_GenericConstants`**——按三栏输出常量：

```python
def _GenericConstants(self, f : FileWriter) -> FileWriter:
    gConst = DutInfo.FilterForTag(self.dutInfo.generics, Tags.CONSTANT)
    gExp = DutInfo.FilterForTag(self.dutInfo.generics, Tags.EXPORT, "true")
    VhdlTitle("Fixed Generics", f, 2)
    for g in gConst:                                              # 固定类：用 CONSTANT 标签的值
        f.WriteLn("constant {} : {} := {};".format(g.name, g.type, DutInfo.GetTag(g, Tags.CONSTANT)))
    f.WriteLn()
    VhdlTitle("Not Assigned Generics (default values)", f, 2)
    for g in self.dutInfo.generics:                               # 默认类：用 DUT 默认值
        if (g.default is not None) and (g not in gConst) and (g not in gExp):
            f.WriteLn("constant {} : {} := {};".format(g.name, g.type, g.default))
    if self.tbInfo.isMultiCaseTb:                                 # 多用例：导出类进 Generics_c 记录
        ...
    return f
```

> 见 [TbGen.py:143-164](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L143-L164)。注意第 148 行用 `GetTag(g, Tags.CONSTANT)` 取**标签值**（不是 `g.default`），所以 `CONSTANT=12` 会让 generic 固定为 12，哪怕 DUT 默认值是 28。

**`_EntityDeclaration`**——只有导出类成为 TB 实体 generic：

```python
eg = DutInfo.FilterForTag(self.dutInfo.generics, Tags.EXPORT, "true")
if len(eg) > 0:
    f.WriteLn("generic (")
    ...
    for g in eg:
        line = "{} : {}".format(g.name, g.type)
        ...
```

> 见 [TbGen.py:198-210](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L198-L210)。

**`_DutInstantiation`**——导出类 + 固定类一起进 `generic map`：

```python
generics = self.dutInfo.generics
eg = (DutInfo.FilterForTag(generics, Tags.EXPORT, "true") + DutInfo.FilterForTag(generics, Tags.CONSTANT))
if len(eg) > 0:
    f.WriteLn("generic map (").IncIndent()
    for g in eg:
        f.WriteLn("{} => {},".format(g.name, g.name))
    ...
```

> 见 [TbGen.py:36-43](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L36-L43)。这里把两个筛选结果用 `+` 拼成一个列表，所以「导出」和「固定」两类都进入 `generic map`，而「默认」类不在其中。

对照示例的真实 generics：

```vhdl
Width_g        : positive := 16;    -- $$ EXPORT=true $$
Depth_g        : positive := 32;    -- $$ EXPORT=true; funky=bla $$
AlmFullOn_g    : boolean  := false; -- $$ EXPORT=false,funky=blubb $$
AlmFullLevel_g : natural  := 28;    -- $$CONSTANT=12$$
AlmEmptyOn_g   : boolean  := false;
AlmEmptyLevel_g: natural  := 4;
```

> 见 [example/simpleTb/psi_common_async_fifo.vhd:30-35](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/example/simpleTb/psi_common_async_fifo.vhd#L30-L35)。分类结果是：

- **导出**：`Width_g`、`Depth_g`（`EXPORT=true`；`Depth_g` 上的 `funky=bla` 被解析但不被任何生成方法使用）。
- **固定**：`AlmFullLevel_g`（`CONSTANT=12`，固定为 12，丢弃默认值 28）。
- **默认**：`AlmFullOn_g`（`EXPORT=false`，未被 `"true"` 筛中；用默认值 `false`）、`AlmEmptyOn_g`、`AlmEmptyLevel_g`（无标签，用各自默认值）。

> 关于 `AlmFullOn_g` 那行 `EXPORT=false,funky=blubb`：因为它不含 `EXPORT=true`、也不含 `CONSTANT`，且有默认值 `false`，所以无论这段注释内部如何被 `_ParseTags` 切分，生成器对它的处置都是确定的——归入「默认」类，用默认值 `false`。本讲不纠结这段含 `=` 的列表值如何解析（u2-l1 已讨论过标签健壮性）。

#### 4.4.4 代码实践

**实践目标**：把三个 generic 标签的「分类 → 去向」对应到示例的具体 generic 上。

**操作步骤**：

1. 对示例的 6 个 generic，逐一判断属于「导出 / 固定 / 默认」哪一类（依据 [第 30-35 行](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/example/simpleTb/psi_common_async_fifo.vhd#L30-L35)）。
2. 预测哪些会进入 `generic map`、哪些会出现在 TB 实体声明里、`AlmFullLevel_g` 的常量值是 12 还是 28。
3. （可选）运行生成，在 `tb/*.vhd` 里搜索 `generic map`、`generic (`、`AlmFullLevel_g` 验证。

**需要观察的现象**：

- `Width_g`、`Depth_g` 既在 TB 实体 `generic (...)` 里，也在 `generic map (...)` 里。
- `AlmFullLevel_g` 在 `generic map` 里且被固定为 `12`（不是 28）。
- `AlmEmptyLevel_g` 不在 `generic map` 里，只作为内部常量 `:= 4`。

**预期结果**：见 4.4.3 末尾的分类清单。运行结果待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：把 `AlmFullLevel_g` 的标签从 `CONSTANT=12` 改成 `EXPORT=true`，生成结果会有什么变化？

> **答案**：它从「固定」类变到「导出」类。`_GenericConstants` 不再输出 `constant AlmFullLevel_g : natural := 12;`（固定栏只列 `CONSTANT` 标签的），而是出现在 TB 实体 `generic (...)` 里作为可配置 generic，并继续留在 `generic map` 里。它的值由 TB 例化者决定（用 DUT 默认值 28 作为初值）。

**练习 2**：为什么「默认」类 generic 不进入 `generic map`？

> **答案**：因为它们既没有被导出（TB 不打算让使用者改），也没有被特别固定。生成器把它们变成 TB 内部常量后，DUT 例化时这些 generic 就**直接使用 DUT 自己声明的默认值**——VHDL 允许 `generic map` 省略某些 generic，省略的就用默认值。所以不写进 `generic map` 等价于「按 DUT 默认走」，避免了重复。

---

### 4.5 `_DutSignals`：端口如何变成 testbench 内部信号

#### 4.5.1 概念说明

DUT 的每个端口，在 testbench 里都需要一个**同名内部信号**与之相连（TB 不能直接驱动 DUT 端口，而是驱动一个信号，再在 `port map` 里把信号连到端口）。`_DutSignals` 就是把这些端口「翻译」成信号声明的段落。

这个翻译的关键在于**初始值**——这正是前面 4.1（`GetPortValue`）与 4.2（`TYPE`）的汇合点：

- `TYPE=RST` 的端口：信号初值取「有效」（`active=True`），让复位一上电就处于有效状态。
- `TYPE=CLK` 的端口：信号初值也取「有效」（`active=True`），让时钟从确定的电平开始，第一个翻转就是规整的上升沿。
- 其它端口（`SIG` 或无 `TYPE`）：信号初值取「无效」（`active=False`），即默认 `'0'`。

代码里有一句注释点明了时钟这么处理的原因：*"clocks start active so they are rising edge aligned"*（时钟从有效电平起步，是为了对齐上升沿）。

#### 4.5.2 核心流程

`_DutSignals` 对每个端口的处理：

```
对 dutInfo.ports 里每个端口 sig：
  try:
    若 TYPE=rst → default = " := " + GetPortValue(sig, True)     # 有效
    若 TYPE=clk → default = " := " + GetPortValue(sig, True)     # 有效（对齐上升沿）
    否则        → default = " := " + GetPortValue(sig, False)    # 无效
  except UnknownVhdlType:
    default = ""                                                  # 不认识的类型不给初值
  写出: signal <sig.name> : <sig.type><default>;
```

注意 `GetPortValue` 对非 `std_logic`/`std_logic_vector` 类型会抛 `UnknownVhdlType`，这里用 `try/except` 兜住——遇到不认识的类型就**不给初值**（生成 `signal X : <type>;`），而不是让整个生成崩溃。这是「能生成多少生成多少」的容错策略。

#### 4.5.3 源码精读

```python
def _DutSignals(self, f : FileWriter) -> FileWriter:
    VhdlTitle("DUT Signals",f , 2)
    sigs = self.dutInfo.ports
    for sig in sigs:
        try:
            if DutInfo.HastTagValue(sig, Tags.TYPE, "rst"):
                default = " := " + self.dutInfo.GetPortValue(sig, True)
            elif DutInfo.HastTagValue(sig, Tags.TYPE, "clk"):
                default = " := " + self.dutInfo.GetPortValue(sig, True)   #clocks start active so they are rising edge aligned
            else:
                default = " := " + self.dutInfo.GetPortValue(sig, False)
        except UnknownVhdlType:
            default = ""
        f.WriteLn("signal {} : {}{};".format(sig.name, str(sig.type), default))
    return f
```

> 见 [TbGen.py:176-190](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L176-L190)。这一段是本讲两条主线的交汇：`TYPE` 标签（4.2）决定走哪个分支，`GetPortValue`（4.1）决定具体字面量，而 `LOWACTIVE` 又隐藏在 `GetPortValue` 内部。

把示例端口代进去：

| 端口 | TYPE | `active` | `GetPortValue` 结果 | 生成的信号声明（节选） |
|------|------|---------|--------------------|-----------------------|
| `InClk` | CLK | True | `'1'` | `signal InClk : std_logic := '1';` |
| `InRst` | RST | True | `'1'` | `signal InRst : std_logic := '1';` |
| `InData` | （无） | False | `(others => '0')` | `signal InData : std_logic_vector(...) := (others => '0');` |
| `InRdy` | （无） | False | `'0'` | `signal InRdy : std_logic := '0';` |

> `InData` 的类型是 `std_logic_vector(Width_g-1 downto 0)`，所以 `str(sig.type)` 会带上范围，初值是 `(others => '0')`。

这条信号声明随后被 `_DutInstantiation` 的 `port map` 直接复用（每个端口 `p => p,`，[TbGen.py:44-48](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L44-L48)），形成「同名信号连同名端口」的简洁结构。

#### 4.5.4 代码实践

**实践目标**：验证 `TYPE` 标签如何决定信号初值，并预测 `LOWACTIVE` 在这里的差异。

**操作步骤**：

1. 对示例的几个代表性端口（`InClk`、`InRst`、`InData`、`InRdy`、`OutData`），预测 `_DutSignals` 生成的信号初值。
2. 假设给 `InRdy`（一个无 `TYPE` 的 `std_logic` 输出）加上 `LOWACTIVE=true`，预测它的信号初值会变成什么。
3. 假设给 `InRst` 加上 `LOWACTIVE=true`，预测它的信号初值变化。

**需要观察的现象**：

- 时钟与复位信号初值是「有效」（高有效时为 `'1'`），普通信号是「无效」（`'0'`）。
- `LOWACTIVE` 会把对应字面量翻转。

**预期结果**：

- `InRdy` 当前：`signal InRdy : std_logic := '0';`；加 `LOWACTIVE=true` 后：`:= '1';`（无效变 `'1'`）。
- `InRst` 当前：`:= '1';`（有效）；加 `LOWACTIVE=true` 后：`:= '0';`（低有效的「有效」是 `'0'`）。

> 若运行生成验证，注意 `InRdy` 是输出端口，TB 用一个内部信号观测它，初值差异主要影响仿真最开始那一瞬间的波形。运行结果待本地验证。

#### 4.5.5 小练习与答案

**练习 1**：为什么时钟信号要初始化成「有效」（`'1'`），而不是像普通信号那样 `'0'`？

> **答案**：时钟从 `'1'` 起步，半个周期后 `_Clocks` 进程把它翻转成 `'0'`，再半周期翻回 `'1'`——于是第一个完整边沿是干净的上升沿。如果时钟从 `'0'` 起步，第一个动作是翻成 `'1'`（也算上升沿），但整体相位会错开半个周期，复位进程「等两个上升沿」的时序就会和预期不符。源码注释 "rising edge aligned" 说的就是这个。

**练习 2**：`_DutSignals` 里 `except UnknownVhdlType: default = ""` 的设计有什么好处？

> **答案**：它让生成器对陌生类型（如 `integer`、自定义类型）**降级而非崩溃**——不给初值地生成 `signal X : <type>;`，VHDL 仿真器会给出该类型的默认值（`std_logic` 是 `'U'`）。这样即便 DUT 用了生成器不支持的类型，TB 骨架依然能生成，设计者可手工补初值。如果改成抛错，一个不支持的类型就会让整个 TB 生成失败。

---

## 5. 综合实践

把本讲的几条主线串起来：**改一个标签，看 diff，解释来源**。这是规格里要求的实践任务。

### 5.1 任务

任选下面**一个**改动（建议先做 A，最简单直观；做完再做 B 看放大效果），重新生成 testbench，对比改动前后的 diff，并用本讲学到的源码知识解释每一处变化。

- **改动 A（端口 + LOWACTIVE）**：给一个**无 `TYPE` 的 `std_logic` 端口**（例如 [OutVld 第 51 行](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/example/simpleTb/psi_common_async_fifo.vhd#L51) 或 [InRdy 第 47 行](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/example/simpleTb/psi_common_async_fifo.vhd#L47)）加上 `LOWACTIVE=true`。
- **改动 B（复位 + LOWACTIVE，放大效果）**：给一个**复位端口**（例如 [InRst 第 40 行](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/example/simpleTb/psi_common_async_fifo.vhd#L40)）加上 `LOWACTIVE=true`。
- **改动 C（generic 标签）**：把 [AlmFullLevel_g 第 33 行](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/example/simpleTb/psi_common_async_fifo.vhd#L33) 的 `CONSTANT=12` 改成 `EXPORT=true`，或反过来把 [Width_g 第 30 行](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/example/simpleTb/psi_common_async_fifo.vhd#L30) 的 `EXPORT=true` 改成 `CONSTANT=16`。

### 5.2 操作步骤

1. **先备份原始输出**：在未改动的示例上跑一次生成，保存结果。

   ```bat
   py ..\..\TbGen.py -src .\psi_common_async_fifo.vhd -dst .\tb_before -clear -force
   ```

   （`run.bat` 用的是 `-dst .\tb`，这里换个目录方便对比。见 [run.bat](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/example/simpleTb/run.bat)。）

2. **施加改动**：复制一份示例 VHDL，按 5.1 的 A/B/C 修改其中一个标签。

3. **再跑一次生成**到另一个目录：

   ```bat
   py ..\..\TbGen.py -src .\psi_common_async_fifo_modified.vhd -dst .\tb_after -clear -force
   ```

4. **diff 两个生成的 TB 文件**（示例命令，可按你的工具调整）：

   ```bash
   diff tb_before/psi_common_async_fifo_tb.vhd tb_after/psi_common_async_fifo_tb.vhd
   ```

5. **解释每一处变化**：对照本讲的源码定位「这行 diff 是哪个方法、因为哪个标签产生的」。

> 注意：本实践需要本机可运行 `py`/`python` 且已安装 `pyparsing` 与 `PsiPyUtils`。若环境不具备，请把 5.3 的「预期变化」当作「源码阅读型」结论，并标注「待本地验证」。

### 5.3 预期变化与解释

- **改动 A（普通 `std_logic` 端口加 `LOWACTIVE=true`）**：diff 应只有**一处**——`_DutSignals` 段落里该端口的信号初值从 `:= '0'` 变成 `:= '1'`。原因见 4.5：普通端口走 `GetPortValue(sig, False)` 分支，`LOWACTIVE` 把「无效」从 `'0'` 翻成 `'1'`（4.1）。

- **改动 B（复位端口加 `LOWACTIVE=true`）**：diff 应有**多处**，全部是 `'1'`↔`'0'` 的成对翻转，分布在：
  - `_DutSignals`：复位信号初值（`active=True`，从 `'1'` 变 `'0'`）——4.5；
  - `_Resets`：复位释放值 `InRst <= '0';` 变成 `<= '1';`（`active=False`）——4.2 的 [TbGen.py:80](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L80)；
  - `_Processes` 与 `_TbControl`：`wait until InRst = '0'` 变成 `= '1'`（`active=False`）——4.1 的调用方表。

  这些翻转**语义自洽**：复位始终是「先有效、后无效」，只是电平含义从高有效变成了低有效。这正是 `GetPortValue` 作为「单一真相源」的价值——改一个标签，四处字面量一致翻转。

- **改动 C（`CONSTANT=12` → `EXPORT=true`）**：
  - `AlmFullLevel_g` 从 `_GenericConstants` 的「Fixed Generics」栏（`constant ... := 12`）**消失**；
  - 它出现在 TB 实体 `generic (...)` 里（成为可配置 generic，初值用 DUT 默认 28）——4.4 的 `_EntityDeclaration`；
  - 它仍在 `generic map` 里（导出类也进 `generic map`）——4.4 的 `_DutInstantiation`。
  - 反向（`EXPORT=true` → `CONSTANT=16`）：`Width_g` 从 TB 实体 generic 中消失，转而作为 `constant Width_g : positive := 16;` 出现在「Fixed Generics」栏。

### 5.4 检查清单

完成实践后，确认你能回答：

- [ ] 改动 A 为何只影响一处，而改动 B 影响多处？（提示：`GetPortValue` 的调用方分布）
- [ ] 改动 B 的多处翻转为何不会互相矛盾？（提示：`active` 参数在不同调用点取 True/False）
- [ ] 改动 C 里，「进 `generic map`」与「进 TB 实体 generic」为什么是两件事？（提示：4.4 的三分类表）

## 6. 本讲小结

- **`TYPE` 是端口的角色标签**：`CLK`/`RST`/`SIG` 决定端口被如何对待。`TYPE=CLK` 配 `FREQ` 驱动 `_Clocks`（[TbGen.py:51-66](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L51-L66)）；`TYPE=RST` 配 `CLK` 驱动 `_Resets`（[TbGen.py:68-84](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L68-L84)）。时钟用频率数值，复位用对其它端口的引用，两者搭档标签不同。
- **时钟半周期**由 `FREQ` 算出：\( T_{\text{half}} = 0.5/f \)。复位则等其归属时钟的两个上升沿后释放，保证至少被采样一次。
- **`GetPortValue` + `LOWACTIVE` 是初始值的单一真相源**（[DutInfo.py:68-79](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/DutInfo.py#L68-L79)）：把「逻辑意图（有效/无效）」翻译成「字面量」，被 `_DutSignals`、`_Resets`、`_Processes`、`_TbControl` 共同复用。
- **`_DutSignals` 汇聚 `TYPE` 与 `GetPortValue`**（[TbGen.py:176-190](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L176-L190)）：时钟/复位信号取「有效」初值，普通信号取「无效」初值，对陌生类型降级为不给初值。
- **generic 三分类**：`EXPORT=true` 导出（进 TB 实体 generic 与 `generic map`）、`CONSTANT=值` 固定（用标签值、进 `generic map`）、两者皆无则用 DUT 默认值作内部常量（不进 `generic map`）。`_GenericConstants`（[TbGen.py:143-164](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L143-L164)）、`_EntityDeclaration`、`_DutInstantiation` 三处协同实现。
- **`PROC` 把端口绑定到过程**（[TbInfo.py:47-48](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbInfo.py#L47-L48)），但只在**多用例** TB 里有可见效果（决定 procedure 参数列表）；单用例 TB 里所有端口都是架构级信号，`PROC` 沉睡不动。注意区别于文件级的 `PROCESSES`。

## 7. 下一步学习建议

本讲你把**端口与 generic 标签**到生成结果的映射讲透了，但还有两块拼图：

- **文件级标签与 TbInfo 建模（u2-l3）**：本讲多次提到 `PROCESSES`（定义过程清单）、`TESTCASES`（切换多用例模式）等**文件级**标签。它们如何被 `TbInfo` 读进 `tbProcesses`、`isMultiCaseTb` 等模型字段，是下一讲的主题。学完后你会彻底理解「单用例 vs 多用例」的开关在哪。
- **多用例 testbench（u5）**：本讲指出 `PROC` 的可见效果在多用例 TB。如果你想亲眼看到 `PROC` 如何变成 procedure 参数、`PortDirectionForProcedure` 如何推断参数方向，学完 u2-l3 后可直接跳到 u5-l1、u5-l2。
- **想往下游走**：如果想看 `Generate` 如何把这些 `_Xxx()` 方法**按顺序**拼成完整 TB 文件，以及 `FileWriter` 的缩进机制如何格式化输出，进入 u4-l2《Generate 主流程与单文件 TB 骨架》。
