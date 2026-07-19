# 时钟、复位、进程与控制信号生成

## 1. 本讲目标

学完本讲后，你应该能够：

- 逐行说清 `_Clocks` 如何把一个带 `TYPE=CLK; FREQ=100e6` 的端口变成 `p_clock_<name>` 进程，并解释半周期公式 \( T_{\text{half}} = 0.5 \cdot \dfrac{1\,\text{sec}}{f} \) 的来历。
- 说清 `_Resets` 如何用 `CLK` 标签把复位「挂」到归属时钟，并解释「等两个上升沿再释放」的用意。
- 默写出 `_TbControlSignals` 声明的五类脚手架（`TbRunning` / `NextCase` / `ProcessDone` / `AllProcessesDone_c` / `TbProcNr_<p>_c`），并说清它们的位宽从哪里来。
- 画出 `ProcessDone = AllProcessesDone_c` 这一条件如何把所有测试进程「汇合」到 `p_tb_control`，再由它把 `TbRunning` 置 `false`，最终让时钟进程跳出 `while TbRunning loop` 结束仿真。
- 理解 `FilterForTag` 这个「三连招原语」（筛选 → 检查 → 取值）如何被 `_Clocks` / `_Resets` / `_Processes` / `_TbControl` / `_DutInstantiation` 反复复用。
- 区分单用例 TB 与多用例 TB 在 `_Processes` / `_TbControl` 里的代码分支差异。

本讲是 u4 的「实现深潜」：u4-l2 讲了 `Generate` 的**整体写作顺序**，把 `_Clocks` / `_Resets` / `_Processes` / `_TbControl` 的内部细节留到了本讲；u2-l2 讲了标签如何**影响**这些段落的生成，本讲则逐行打开这些段落本身的生成代码，把「标签」与「最终 VHDL」之间的最后一段实现补齐。

## 2. 前置知识

本讲承接 u4-l2 建立的「线性写作器」心智模型：

> `Generate` 不做计算决策，只按 VHDL 物理顺序自上而下誊抄；每个 `_Xxx()` 方法接收并返回同一个 `FileWriter`，借 `WriteLn` / `IncIndent` / `DecIndent` / `RemoveFromLastLine` 拼出格式化 VHDL。

进入本讲前，请确认你已掌握（来自前置讲义）：

- **标签语义**（u2-l2）：`TYPE=CLK` 配 `FREQ` 驱动时钟；`TYPE=RST` 配 `CLK`（引用另一个端口名）驱动复位；`PROC=<name>` 把端口绑定到测试过程；`LOWACTIVE` 翻转有效/无效极性。
- **`GetPortValue(port, active)`**（u4-l1 / u2-l2）：端口初值的单一真相源，`active=True` 返回有效值、`active=False` 返回无效值，未知类型抛 `UnknownVhdlType`。
- **`FilterForTag(list, tag, value=None, casesensitive=False)`**（u2-l1）：从一个端口/generic 列表里筛出「带某标签（且值匹配）」的子集。
- **`TbInfo` 模型**（u2-l3 / u4-l1）：`tbProcesses`（缺省 `["Stimuli"]`）、`isMultiCaseTb`（仅判 `TESTCASES` 键是否存在）、`testCases`、`tbName`、`GetPortsForProcess(p)`。
- **`Generate` 的并发语句段顺序**（u4-l2）：

  ```
  _DutInstantiation → _TbControl → _Clocks → _Resets → _Processes
  ```

一个贯穿全讲的直觉：**这一讲的每个方法，本质都是「遍历一个 `FilterForTag` 的结果列表，逐个写一段 VHDL」**。掌握了 `FilterForTag`，这五个方法的骨架就都透明了；剩下的只是「每一段具体写什么」。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `TbGen.py` | 引擎核心，定义 `TbGenerator` 类 | `_DutInstantiation` / `_Clocks` / `_Resets` / `_Processes` / `_TbControl` / `_TbControlSignals` 六个写作方法，以及 `Generate` 里它们的调用顺序 |
| `DutInfo.py` | DUT 数据模型 | `FilterForTag`（本讲反复复用的筛选原语）、`GetPortValue`（初值真相源）、`HasTag` / `GetTag` / `HastTagValue`（存在性检查与取值）、`Tags` 常量类 |
| `TbInfo.py` | TB 数据模型 | `isMultiCaseTb` / `tbProcesses` / `testCases` / `GetPortsForProcess`（多用例进程参数来源） |
| `example/simpleTb/psi_common_async_fifo.vhd` | 单用例示例 DUT | 两个时钟（`InClk` 100 MHz、`OutClk` 125 MHz）、两个复位（`InRst`、`OutRst`）、`PROCESSES=Input,Output` |
| `example/multiCaseTb/psi_common_async_fifo.vhd` | 多用例示例 DUT | 在 simpleTb 基础上多了 `$$ TESTCASES=Full,Empty $$`，用来对照多用例分支 |

> 说明：`FileWriter` 来自外部依赖 `PsiPyUtils`（不在本仓库内），其精确实现不在本讲范围。我们只从它在 `TbGen.py` 中的链式调用方式推断其「对外契约」：写一行、增减缩进、回改上一行字符。

## 4. 核心概念与源码讲解

### 4.1 FilterForTag：贯穿全讲的「筛选」原语

#### 4.1.1 概念说明

本讲六个写作方法里，有五个的开头都是同一句话：「先用 `FilterForTag` 把目标端口/generic 筛出来」。所以正式进入各方法前，先把这块「公共地砖」铺平。

`FilterForTag` 是 `DutInfo` 的类方法，语义是：

> 给我一个对象列表（通常是 `self.dutInfo.ports` 或 `self.dutInfo.generics`），一个标签名，以及可选的标签值；我把列表里**注释中带这个标签（且值匹配）**的对象挑出来，返回一个新列表。

它的两个关键设计点：

1. **`value=None` 时只判存在性**：只关心「有没有这个标签」，不看值。`_Clocks` 筛 `TYPE=clk` 用的是「带值」模式；`_DutInstantiation` 筛 `CONSTANT` 用的是「无值」模式（只要 generic 标了 `CONSTANT` 就要进 `generic map`，不管值是多少）。
2. **值匹配默认大小写不敏感**：这与整个标签系统一致（标签名统一小写、`HastTagValue` 默认 `casesensitive=False`）。所以示例里 `PROC=INPUT`、`PROC=Input`、`PROC=input` 都能被筛到同一个进程。

#### 4.1.2 核心流程

```
FilterForTag(list, tag, value=None, casesensitive=False):
  结果 = []
  tag = tag.lower()
  for e in list:                          # e 是端口或 generic 对象
      tags = _ParseTags(e.comment)        # 解析该对象的 $$..$$ 注释
      if tag in tags:
          if value is None:               # 无值模式：存在即可
              结果.append(e)
          else:                           # 带值模式：值要匹配
              把 tags[tag] 归一成 list（单值也包成单元素 list）
              若 value（按大小写策略）命中该 list，则 结果.append(e)
  return 结果
```

注意一个细节：值匹配时，标签值会先被**归一成列表**再判断 `in`。这解释了为什么 `PROC=Output,Input`（列表值）这种「一个端口属于多个进程」的写法能正确命中 `Output` 或 `Input` 任一进程。

#### 4.1.3 源码精读

`FilterForTag` 的完整实现（注意它把单值 `str` 与列表 `list` 两种形态都归一成列表再匹配）：

[DutInfo.py:147-166](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/DutInfo.py#L147-L166) —— 遍历列表，逐个解析注释；`value is None` 分支只判标签存在，带值分支把标签值归一成列表后做大小写不敏感的 `in` 匹配。

配合使用的三个工具方法（本讲各处都会用到）：

- [DutInfo.py:125-131](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/DutInfo.py#L125-L131) `HasTag` —— 判存在性，用于 `_Clocks` 检查 `FREQ`、`_Resets` 检查 `CLK`。
- [DutInfo.py:133-138](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/DutInfo.py#L133-L138) `GetTag` —— 取值，缺失时抛异常；用于取 `FREQ` 数值、取 `CLK` 引用的时钟名。
- [DutInfo.py:114-123](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/DutInfo.py#L114-L123) `HastTagValue` —— 判「标签是否等于某值」，`_DutSignals` 用它区分 clk/rst/普通端口。

标签名常量集中声明在 `Tags` 类，本讲主要用到这几个：

[DutInfo.py:16-32](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/DutInfo.py#L16-L32) —— `TYPE`（CLK/RST/SIG）、`FREQ`、`CLK`、`PROC`（端口级），以及 `EXPORT` / `CONSTANT`（generic 级，`_DutInstantiation` 会用到）。

#### 4.1.4 代码实践（源码阅读型）

**目标**：亲手验证 `FilterForTag` 的「带值 / 无值」两种模式与大小写不敏感特性。

1. 打开 `example/simpleTb/psi_common_async_fifo.vhd`，找到四个控制端口 [example/simpleTb/psi_common_async_fifo.vhd:39-42](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/example/simpleTb/psi_common_async_fifo.vhd#L39-L42)。
2. 用脑子「跑」一遍：对 `self.dutInfo.ports` 调 `FilterForTag(..., Tags.TYPE, "clk")`，结果应是 `[InClk, OutClk]`；调 `FilterForTag(..., Tags.TYPE, "rst")` 应是 `[InRst, OutRst]`。
3.（可选）在仓库根目录写一个临时脚本，`from DutInfo import DutInfo, Tags`，构造 `DutInfo("example/simpleTb/psi_common_async_fifo.vhd")`，打印上述两个筛选结果，对照你的预测。

**预期结果**：时钟两个、复位两个，顺序与端口声明顺序一致（`FilterForTag` 保持原列表顺序）。

#### 4.1.5 小练习与答案

**Q1**：`_DutInstantiation` 里筛 generic 用的是 `FilterForTag(generics, Tags.CONSTANT)`（没传 `value`），而筛导出 generic 用的是 `FilterForTag(generics, Tags.EXPORT, "true")`（传了 `value`）。为什么 `CONSTANT` 不传值？

**答**：`CONSTANT` 标签的语义是「这个 generic 用标签里写的值固定下来」，只要标了 `CONSTANT` 就要进 `generic map`，具体值由 `GetTag(g, Tags.CONSTANT)` 另取，所以筛选阶段只需判存在性（`value=None`）；而 `EXPORT` 的值可能是 `true` / `false`，只有 `true` 才导出，所以必须带值 `"true"` 精确匹配。

**Q2**：示例里 `OutRdy` 的注释是 `$$ PROC=Output,Input $$`。`FilterForTag(ports, Tags.PROC, "Input")` 会把它筛进 `Input` 进程吗？

**答**：会。值匹配阶段会把列表值 `["Output", "Input"]` 归一后做 `in` 判断，`"input"` 命中，所以 `OutRdy` 同时属于 `Input` 和 `Output` 两个进程。

---

### 4.2 _Clocks：FREQ 标签如何变成时钟进程

#### 4.2.1 概念说明

`_Clocks` 负责为每一个标了 `TYPE=CLK` 的端口生成一个独立的、**自运行**的时钟进程 `p_clock_<端口名>`。它的全部输入就是 `FilterForTag` 筛出的时钟端口列表，外加每个端口的 `FREQ` 标签。

这里有一个强约束（在代码里以异常形式表达）：**时钟端口必须带 `FREQ` 标签，否则报错**。没有频率就没法算半周期，时钟进程就写不出来。

#### 4.2.2 核心流程

对每个时钟端口 `clk`：

```
1. 筛选：FilterForTag(ports, TYPE, "clk")  →  [InClk, OutClk, ...]
2. 检查：每个 clk 必须有 FREQ 标签，否则 raise
3. 写一个进程：
   p_clock_<name> : process
       constant Frequency_c : real := real(<FREQ>);
   begin
       while TbRunning loop           -- 关键：受 TbRunning 控制
           wait for 0.5*(1 sec)/Frequency_c;
           <name> <= not <name>;       -- 每过半周期翻转一次
       end loop;
       wait;                           -- TbRunning 变 false 后永久挂起
   end process;
```

半周期由频率换算而来。若频率为 \( f \)（Hz），则周期 \( T = 1/f \) 秒，半周期：

\[
T_{\text{half}} = \frac{T}{2} = \frac{0.5 \cdot 1\,\text{sec}}{f}
\]

例如 `FREQ=100e6`：\( T_{\text{half}} = 0.5 / 10^{8}\,\text{sec} = 5\,\text{ns} \)，对应周期 10 ns、100 MHz。`FREQ=125e6` 则得 4 ns 半周期。

`while TbRunning loop` 是整段仿真收尾机制的「一半」：时钟是否继续翻转，取决于 `TbRunning` 这个 boolean 信号；它由 `_TbControl`（4.7 节）在所有测试进程结束时置 `false`。

#### 4.2.3 源码精读

[TbGen.py:51-66](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L51-L66) —— `_Clocks` 全貌：

- **L53** 用 `FilterForTag(..., Tags.TYPE, "clk")` 筛出所有时钟端口。
- **L54-55** `if not HasTag(clk, FREQ): raise` —— 缺 `FREQ` 直接报错。
- **L56-57** 写进程头与局部常量 `Frequency_c : real := real(<FREQ>)`（`GetTag` 取到的是字符串如 `"100e6"`，拼进 `real(...)` 由 VHDL 在仿真时求值）。
- **L59-61** `while TbRunning loop` → `wait for 0.5*(1 sec)/Frequency_c;` → `<name> <= not <name>;` —— 半周期翻转。
- **L62-63** `end loop;` 之后一句 `wait;` —— `TbRunning` 变 `false` 后跳出循环，进程在此永久挂起。

注意 `_DutSignals`（u4-l2）已把时钟信号初值设为「有效」（`GetPortValue(sig, True)`，默认 `'1'`），注释里写明原因：**clocks start active so they are rising edge aligned**（时钟从有效电平起步，保证第一个翻转产生的是上升沿），见 [TbGen.py:183-184](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L183-L184)。

#### 4.2.4 代码实践

**目标**：追踪 `InClk`（`TYPE=CLK; FREQ=100e6; PROC=Input`）从标签到 `p_clock_InClk` 进程的完整路径。

1. 在 [example/simpleTb/psi_common_async_fifo.vhd:39](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/example/simpleTb/psi_common_async_fifo.vhd#L39) 确认 `InClk` 的标签。
2. 追踪调用链：`Generate` → `_Clocks(f)`（见 [TbGen.py:250](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L250)）→ `FilterForTag` 命中 `InClk` → `HasTag(FREQ)` 为真 → 写出进程。
3. 在生成的 `tb/psi_common_async_fifo_tb.vhd` 里找到 `p_clock_InClk`，确认 `Frequency_c` 值为 `real(100e6)`、`wait for` 行为 `0.5*(1 sec)/Frequency_c`。
4. 手算半周期，确认是 5 ns。

**预期结果**：生成的进程与上面伪代码一一对应；`p_clock_OutClk` 同理，`Frequency_c` 为 `real(125e6)`，半周期 4 ns。**若你无法本地运行生成，相关输出数值标注「待本地验证」。**

#### 4.2.5 小练习与答案

**Q1**：如果把 `InClk` 的 `FREQ` 标签删掉再生成，会发生什么？

**答**：`_Clocks` 在 L54-55 抛出 `Exception("Clock InClk has not FREQ tag!")`，`Generate` 失败，CLI 打印 `ERROR: ...` 并 `exit(-1)`（见 [TbGen.py:312-314](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L312-L314)）。

**Q2**：时钟进程为什么用 `while TbRunning loop` 而不是无限循环？

**答**：为了能受控停止。当所有测试进程完成、`p_tb_control` 把 `TbRunning` 置 `false` 时，时钟进程跳出循环、执行 `wait;` 永久挂起，仿真不再有事件，从而自然结束。若用无限循环，仿真将永远跑下去。

---

### 4.3 _Resets：CLK 标签如何把复位挂到归属时钟

#### 4.3.1 概念说明

`_Resets` 为每个标了 `TYPE=RST` 的端口生成一个复位进程 `p_rst_<端口名>`，负责「上电时保持复位有效，过一会儿再释放」。

与时钟类似，复位也有一个强约束：**复位端口必须带 `CLK` 标签**，且这个 `CLK` 的值是**另一个端口的名字**（即这个复位归属哪个时钟域）。这是异步 FIFO 这类多时钟设计的必然要求：复位的释放必须与某个具体时钟的边沿对齐，才能被该时钟域可靠采样。

#### 4.3.2 核心流程

对每个复位端口 `rst`：

```
1. 筛选：FilterForTag(ports, TYPE, "rst")  →  [InRst, OutRst, ...]
2. 检查：每个 rst 必须有 CLK 标签，否则 raise
3. 取值：clkName = GetTag(rst, CLK)        -- 例如 "InClk"
4. 写一个进程：
   p_rst_<name> : process
   begin
       wait for 1 us;                      -- 先保持复位有效一段时间
       -- Wait for two clk edges to ensure reset is active for at least one edge
       wait until rising_edge(<clkName>);   -- 等第 1 个上升沿
       wait until rising_edge(<clkName>);   -- 等第 2 个上升沿
       <name> <= <无效值>;                   -- 释放复位（置为 inactive）
       wait;
   end process;
```

为什么是「两个上升沿」？注释说得直白：**保证复位至少被采样到一个边沿**。复位信号在仿真开始时由 `_DutSignals` 初始化为「有效值」（`GetPortValue(rst, True)`），释放前先等两个归属时钟的上升沿，确保 DUT 内部寄存器在复位有效期间至少经历一次完整的时钟采样，然后再释放到无效值 `GetPortValue(rst, False)`。

#### 4.3.3 源码精读

[TbGen.py:68-84](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L68-L84) —— `_Resets` 全貌：

- **L70** `FilterForTag(..., Tags.TYPE, "rst")` 筛出复位端口。
- **L71-72** `if not HasTag(rst, CLK): raise` —— 缺 `CLK` 报错。
- **L73** `clkName = GetTag(rst, Tags.CLK)` —— 取归属时钟名（字符串，直接当 VHDL 信号名用）。
- **L76** `wait for 1 us;` —— 先维持有效 1 µs。
- **L77-79** 注释 + 两次 `wait until rising_edge(<clkName>);`。
- **L80** `<name> <= <GetPortValue(rst, False)>;` —— 释放到无效值（普通高有效复位 → `'0'`；`LOWACTIVE=true` 的复位 → `'1'`）。
- **L81** `wait;` —— 一次性进程，释放后永久挂起（复位只释放一次，不像时钟那样循环）。

注意：`GetPortValue(rst, False)` 在这里被复用——它正是 u4-l1 / u2-l2 强调的「初值单一真相源」。改 `LOWACTIVE` 标签，这里的释放值、`_DutSignals` 的初始值、`_Processes`/`_TbControl` 里等复位失效的表达式会**一起**翻转。

#### 4.3.4 代码实践

**目标**：追踪 `InRst`（`TYPE=RST; CLK=InClk`）的复位释放过程。

1. 在 [example/simpleTb/psi_common_async_fifo.vhd:40](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/example/simpleTb/psi_common_async_fifo.vhd#L40) 确认 `InRst` 的标签，注意 `CLK=InClk` 引用的是另一个端口。
2. 在生成结果中找到 `p_rst_InRst`，确认它 `wait until rising_edge(InClk)` 两次后执行 `InRst <= '0';`。
3. 回溯 `InRst` 没有标 `LOWACTIVE`，所以 `GetPortValue(InRst, False)='0'`（无效），`GetPortValue(InRst, True)='1'`（有效）；对照 `_DutSignals` 里 `InRst` 的初值应为 `'1'`（复位上电即有效）。

**预期结果**：`InRst` 初值 `'1'` → 1 µs 后等 `InClk` 两个上升沿 → 释放为 `'0'`。**若无法本地运行，标注「待本地验证」。**

#### 4.3.5 小练习与答案

**Q1**：为什么 `CLK` 标签的值是一个**端口名**而不是一个频率数值？

**答**：因为复位释放需要与「某个已有时钟信号」的边沿同步（`wait until rising_edge(<信号>)` 要求参数是信号名）。时钟信号由 `_Clocks` 以端口名命名并驱动，所以复位只要引用对应时钟端口名即可对齐到该时钟域。

**Q2**：若把 `InRst` 的 `CLK` 标签删掉，生成会怎样？

**答**：`_Resets` 在 L71-72 抛 `Exception("Reset InRst has not CLK tag!")`，生成失败。

---

### 4.4 _TbControlSignals：仿真脚手架信号

#### 4.4.1 概念说明

前面两节生成了「会自己跑」的时钟和「一次性」的复位，但仿真要**有序地开始**（等复位释放）、**有序地结束**（所有测试进程完成），还需要一组「脚手架」信号。`_TbControlSignals` 就是声明这组信号的地方。它写在架构的声明区（u4-l2 已点明它在 `_GenericConstants` 之后、`_DutSignals` 之前），本身不产生并发语句，只是把后面 `_TbControl` / `_Processes` / `_Clocks` 要用到的控制信号先声明好。

#### 4.4.2 核心流程

固定写出五行（最后一行按进程数量循环）：

```
signal TbRunning : boolean := True;                                    -- 仿真是否继续
signal NextCase : integer := -1;                                       -- 多用例：当前用例编号
signal ProcessDone : std_logic_vector(0 to N-1) := (others => '0');    -- 每进程一比特，完成置 1
constant AllProcessesDone_c : std_logic_vector(0 to N-1) := (others => '1');  -- 全完成的掩码
constant TbProcNr_<p0>_c : integer := 0;                               -- 每个进程的比特序号
constant TbProcNr_<p1>_c : integer := 1;
...
```

其中 \( N = \text{len(tbProcesses)} \)，即测试进程的数量。`ProcessDone` 与 `AllProcessesDone_c` 是**等宽**向量，前者初值全 `'0'`、后者常量全 `'1'`。

#### 4.4.3 源码精读

[TbGen.py:166-174](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L166-L174) —— `_TbControlSignals` 全貌：

- **L168** `TbRunning : boolean := True` —— 一开始为真，时钟进程的 `while TbRunning loop` 因此能转起来。
- **L169** `NextCase : integer := -1` —— 初值 `-1`，多用例时 `p_tb_control` 会把它依次置 `0, 1, ...` 触发各用例；单用例下它虽然声明了但无人写、也无人读。
- **L170** `ProcessDone` 向量宽度 `0 to len(tbProcesses)-1` —— simpleTb 里 `tbProcesses=["Input","Output"]`，宽度即 `0 to 1`。
- **L171** `AllProcessesDone_c` 同宽、全 `'1'` —— 这就是 4.7 节「仿真结束」判据的右值。
- **L172-173** 循环为每个进程发一个 `TbProcNr_<p>_c : integer := <i>` 常量，把进程名映射到 `ProcessDone` 向量里的比特位。simpleTb 会得到 `TbProcNr_Input_c := 0`、`TbProcNr_Output_c := 1`。

关键点：**脚手架的「规模」完全由 `tbProcesses` 决定**。这意味着「进程数」是整个 TB 控制机制的「模数」——`PROCESSES` 文件级标签改变进程数，`ProcessDone` 宽度与所有 `TbProcNr_*_c` 常量都会跟着变。

#### 4.4.4 代码实践

**目标**：验证脚手架位宽随 `PROCESSES` 标签变化。

1. 看 simpleTb：`PROCESSES=Input,Output` → `ProcessDone` 宽度 `0 to 1`，两个 `TbProcNr_*_c`。
2. 假想把文件级标签改成 `PROCESSES=Stimuli`（或删掉该标签走缺省 `["Stimuli"]`）：预测 `ProcessDone` 宽度变为 `0 to 0`，只剩 `TbProcNr_Stimuli_c := 0`。
3. 在 [TbInfo.py:26-30](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbInfo.py#L26-L30) 确认 `tbProcesses` 的来源（`PROCESSES` 标签，缺省 `["Stimuli"]`）。

**预期结果**：脚手架规模 = 进程数；预测与生成结果一致。

#### 4.4.5 小练习与答案

**Q1**：`NextCase` 在单用例 TB 里有用吗？为什么还是声明了它？

**答**：没有用——单用例下既没人写它也没人读它（`_TbControl` 单用例分支直接 `wait until ProcessDone = AllProcessesDone_c`）。它被无条件声明，是因为 `_TbControlSignals` 不区分单/多用例；多用例才用得上。这是一种「声明统一、使用分流」的取舍。

**Q2**：`ProcessDone` 与 `AllProcessesDone_c` 为什么必须等宽？

**答**：因为 `p_tb_control` 用 `wait until ProcessDone = AllProcessesDone_c` 判断「所有进程完成」，这要求两边是同维向量才能逐比特比较；等宽由两者都用 `len(tbProcesses)-1` 作上界保证。

---

### 4.5 _DutInstantiation：把 DUT 接进 testbench

#### 4.5.1 概念说明

`_DutInstantiation` 生成 DUT 的实例化语句 `i_dut : entity <lib>.<name>`，并补上 `generic map` 与 `port map`。本讲把它放在这里讲，是因为它的 `generic map` 同样是 `FilterForTag` 的典型用例，且它和 `_DutSignals` 一起构成了「端口接线」的全貌。

`generic map` 里出现哪些 generic，由两条规则合并：

- `EXPORT=true` 的 generic（导出给 TB 实体）；
- 带 `CONSTANT` 标签的 generic（在 TB 内部固定）。

其余 generic（既没导出也没固定、用 DUT 默认值的）**不进 `generic map`**，它们以内部常量形式由 `_GenericConstants` 声明。

`port map` 则简单粗暴：**所有端口一一接上同名信号**（信号由 `_DutSignals` 声明，名字与端口一致）。

#### 4.5.2 核心流程

```
1. 写 "DUT Instantiation" 标题
2. i_dut : entity <dutLibrary>.<name>
3. eg = FilterForTag(generics, EXPORT, "true") + FilterForTag(generics, CONSTANT)
4. if eg 非空:
       generic map (
           <g0> => <g0>,   -- 每个导出/固定 generic 一行，末尾逗号
           ...
       )                   -- 回改去掉最后一行尾逗号
5. port map (
       <p0> => <p0>,       -- 每个端口一行，接同名信号
       ...
   );                      -- 回改去尾逗号，闭合
```

#### 4.5.3 源码精读

[TbGen.py:33-49](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L33-L49) —— `_DutInstantiation` 全貌：

- **L35** 实例化行用 `dutInfo.dutLibrary`（`DUTLIB` 标签的带默认值 `"work"` 视图，见 [DutInfo.py:61-66](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/DutInfo.py#L61-L66)）。
- **L37** `eg = FilterForTag(..., EXPORT, "true") + FilterForTag(..., CONSTANT)` —— 这正是 4.1.5 Q1 讨论的「带值 + 无值」两种模式拼接：导出的要值 `true`，固定的只要存在。
- **L39-43** `generic map` 段；**L42** `RemoveFromLastLine(1)` 去掉最后一个 generic 行尾的逗号（VHDL 语法不允许末尾逗号）。
- **L44-48** `port map` 段同理：遍历**所有**端口（`self.dutInfo.ports`，无筛选），每个接同名信号；末尾 `RemoveFromLastLine(1)` 去尾逗号后闭合 `);`。

`RemoveFromLastLine` 是 `FileWriter` 的「回改」能力：先无脑每行写尾逗号，最后把最后一行的逗号抹掉，比单独判断「是不是最后一个」更简洁。这是整套写作器里反复出现的手法。

#### 4.5.4 代码实践

**目标**：对照 simpleTb 的 generic 标签，预测 `generic map` 的内容。

1. 看 [example/simpleTb/psi_common_async_fifo.vhd:30-35](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/example/simpleTb/psi_common_async_fifo.vhd#L30-L35)：
   - `Width_g`：`EXPORT=true` → 进 `generic map`
   - `Depth_g`：`EXPORT=true; funky=bla` → 进 `generic map`（带值匹配 `true` 命中）
   - `AlmFullOn_g`：`EXPORT=false,...` → **不进**（值是 `false`）
   - `AlmFullLevel_g`：`CONSTANT=12` → 进 `generic map`（无值筛选命中）
   - `AlmEmptyOn_g` / `AlmEmptyLevel_g`：无标签 → **不进**（用默认值，由 `_GenericConstants` 声明为内部常量）
2. 预测 `generic map` 含三行：`Width_g => Width_g, Depth_g => Depth_g, AlmFullLevel_g => AlmFullLevel_g`。
3. 在生成结果中验证，并确认 `port map` 把全部端口都接上了同名信号。

**预期结果**：`generic map` 三项，`port map` 全端口。**若无法本地运行，标注「待本地验证」。**

#### 4.5.5 小练习与答案

**Q1**：为什么 `port map` 不做任何筛选，而 `generic map` 要筛选？

**答**：DUT 的每个端口都必须在实例化时连到一个信号，否则 VHDL 编译不过，所以 `port map` 全连接。generic 则有三类不同处理（导出 / 固定 / 用默认值），只有前两类需要出现在 `generic map` 里，用默认值的 generic 不写 `generic map` 项即等于取其默认值。

**Q2**：`RemoveFromLastLine(1)` 在这里解决什么问题？

**答**：循环里每行都写 `name => name,`（带尾逗号），但 VHDL 不允许最后一项后还有逗号。`RemoveFromLastLine(1)` 在循环结束后抹掉最后一个字符（逗号），避免单独写「是否最后一项」的分支判断。

---

### 4.6 _Processes：PROC 标签绑定与单/多用例分支

#### 4.6.1 概念说明

`_Processes` 为 `tbProcesses` 里的每个进程名生成一个测试进程 `p_<name>`。这是整个 TB 里**唯一由用户填写激励代码**的地方（单用例下会留 `assert ... "Insert your code here!"` 占位）。

`_Processes` 最大的特点是**单用例与多用例走完全不同的两套分支**：

- **单用例**（`isMultiCaseTb == False`）：每个进程在等复位释放后，留一段用户代码占位，结束时把自己的 `ProcessDone` 比特置 1。
- **多用例**（`isMultiCaseTb == True`）：每个进程不再含用户代码，而是**按 `NextCase` 调度**，依次调用各用例 package 里同名 procedure，每跑完一个用例就把自己的 `ProcessDone` 比特回置 1。

注意一个在 u2-l2 已点明、这里再次印证的事实：**`PROC` 标签只在多用例分支里被消费**（通过 `GetPortsForProcess` 决定 procedure 的参数列表）。单用例分支里 `PROC` 完全不被读取——所有进程都长一个样（占位 + 置完成位）。务必把端口级单数 `PROC` 与文件级复数 `PROCESSES` 区分开：前者绑端口到进程，后者定义进程名清单。

#### 4.6.2 核心流程

```
for p in tbProcesses:                       # 例如 ["Input", "Output"]
    写 "p_<p> : process / begin"
    if 多用例:
        for i, c in enumerate(testCases):    # 例如 ["Full", "Empty"]
            wait until NextCase = i;
            ProcessDone(TbProcNr_<p>_c) <= '0';     # 开始本用例：清完成位
            args = GetPortsForProcess(p) 的端口名 join ", "
            work.<tb>_case_<c>.<p>(<args>, Generics_c);   # 调用本用例 procedure
            wait for 1 ps;
            ProcessDone(TbProcNr_<p>_c) <= '1';     # 本用例完成：置完成位
    else (单用例):
        rsts = FilterForTag(ports, TYPE, "rst")
        if rsts 非空:
            wait until (<rst0> = <inactive> and <rst1> = <inactive> ...);   # 等复位释放
        -- User Code
        assert False report "Insert your code here!" severity note;          # 占位
        ProcessDone(TbProcNr_<p>_c) <= '1';            # 本进程完成
    wait;
    end process;
```

#### 4.6.3 源码精读

[TbGen.py:86-120](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L86-L120) —— `_Processes` 全貌：

- **L87-90** 标题在多用例下加 `!DO NOT EDIT!`（因为进程体由工具全权管理，用户改的是 case 包里的 procedure），单用例则不带（用户要在进程里填代码）。
- **L92-94** 遍历 `tbProcesses`，每个进程先写 level-2 标题再写进程头。
- **L96-104** 多用例分支：`wait until NextCase = i` 同步起步 → 清完成位 → 拼 `args`（**L101** `GetPortsForProcess(p)`，即 `FilterForTag(ports, PROC, p)`，见 [TbInfo.py:47-48](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbInfo.py#L47-L48)）→ 调 `work.<tb>_case_<c>.<p>(args, Generics_c)` → `wait for 1 ps`（给信号传递一个 δ 周期）→ 置完成位。
- **L105-116** 单用例分支：**L106** `FilterForTag(ports, TYPE, "rst")` 取复位端口；**L108-110** 若有复位，`wait until` 所有复位都等于其 `GetPortValue(r, False)`（无效值）——即等复位释放；**L113** 占位 `assert`；**L116** 置完成位。
- **L117-118** 每个进程末尾 `wait;` 后 `end process;`。

注意单用例分支里那段「等复位释放」的 `rstLogic` 拼接：`" and ".join([r.name + " = " + GetPortValue(r, False) for r in rsts])`。对 simpleTb 会得到 `InRst = '0' and OutRst = '0'`，于是进程在两个复位都释放后才开始跑用户代码。这套「等复位」逻辑在 `_TbControl`（4.7）里还有一份。

#### 4.6.4 代码实践

**目标**：对照 simpleTb（单用例）与 multiCaseTb（多用例）的 `_Processes` 输出差异。

1. 单用例 simpleTb：`PROCESSES=Input,Output`、无 `TESTCASES` → 生成 `p_Input` / `p_Output` 两个进程，各自含「等复位释放 + 占位 assert + 置 `ProcessDone(TbProcNr_Input_c/Output_c)`」。
2. 多用例 multiCaseTb：多了 [example/multiCaseTb/psi_common_async_fifo.vhd:25](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/example/multiCaseTb/psi_common_async_fifo.vhd#L25) 的 `TESTCASES=Full,Empty` → 同样的两个进程名，但进程体变成「等 `NextCase=0` 调 `..._case_Full.Input(...)`、再等 `NextCase=1` 调 `..._case_Empty.Input(...)`」。
3. 在 multiCaseTb 的生成结果里确认 `p_Input` 的 `args` 来自 `GetPortsForProcess("Input")`，即所有标了 `PROC=Input`（含 `PROC=Output,Input` 的 `OutRdy`）的端口名。

**预期结果**：单用例进程含用户占位、多用例进程含 case 调度；多用例的 `args` 与 `PROC` 标签一致。**详细 procedure 签名留待 u5，本讲只需确认 `args` 来源。** 若无法本地运行，标注「待本地验证」。

#### 4.6.5 小练习与答案

**Q1**：单用例 TB 里，给某端口加 `PROC=Foo` 标签会影响生成的 `p_Stimuli` 进程吗？

**答**：不会。单用例分支（L105-116）根本不调用 `GetPortsForProcess`，`PROC` 标签被完全忽略；所有进程都长成「等复位 + 占位 + 置完成位」的同一个模样。`PROC` 只在多用例分支决定 procedure 参数。

**Q2**：多用例分支里 `ProcessDone(TbProcNr_<p>_c) <= '0'` 之后为何要 `wait for 1 ps` 再置 `'1'`？

**答**：先清零表示「本用例开始」，跑完 procedure 后给一个微小延迟（1 ps，一个 δ 量级的仿真时间）让信号稳定传播，再置 1 表示「本用例完成」。配合 `p_tb_control` 的 `wait until ProcessDone = AllProcessesDone_c`，构成「所有进程都跑完当前用例 → 推进到下一用例」的握手。

---

### 4.7 _TbControl：让仿真有序结束的总控进程

#### 4.7.1 概念说明

`_TbControl` 生成唯一的总控进程 `p_tb_control`，它是整个仿真的「指挥」：负责决定**何时开始等待结束**、**何时宣告结束**。它把 `_TbControlSignals` 声明的脚手架与各测试进程的 `ProcessDone` 比特串成一个闭环：

> 各进程完成 → 把自己的 `ProcessDone` 比特置 1 → `p_tb_control` 检测到 `ProcessDone = AllProcessesDone_c`（全 1）→ 把 `TbRunning` 置 `false` → 时钟进程跳出 `while TbRunning loop` → 仿真无事件 → 结束。

这就是本讲核心问题「`ProcessDone = AllProcessesDone_c` 如何让仿真结束」的完整答案。

#### 4.7.2 核心流程

单用例：

```
p_tb_control : process
begin
    -- (若有复位) wait until 所有复位 = 无效值;     # 等复位释放再开始计时
    wait until ProcessDone = AllProcessesDone_c;    # 等所有测试进程完成
    TbRunning <= false;                              # 关掉时钟
    wait;
end process;
```

多用例：

```
p_tb_control : process
begin
    -- (若有复位) wait until 所有复位 = 无效值;
    for i, c in enumerate(testCases):
        NextCase <= i;                               # 通知所有进程：跑第 i 个用例
        wait until ProcessDone = AllProcessesDone_c; # 等所有进程跑完该用例
    TbRunning <= false;                              # 全部用例跑完，关时钟
    wait;
end process;
```

两条路径最后都收敛到 `TbRunning <= false; wait;`。多用例只是把「等一次全完成」换成「每个用例等一次全完成，期间用 `NextCase` 推进」。

#### 4.7.3 源码精读

[TbGen.py:122-141](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L122-L141) —— `_TbControl` 全貌：

- **L124-125** 进程头 `p_tb_control : process / begin`。
- **L126-129** 复位等待（与 `_Processes` 单用例分支同款 `rstLogic` 拼接）：若有复位端口，先 `wait until` 它们都到无效值。这保证「计时」从复位释放后才开始。
- **L130-134** 多用例分支：遍历 `testCases`，每个用例 `NextCase <= i` 后 `wait until ProcessDone = AllProcessesDone_c`。
- **L135-136** 单用例分支：直接 `wait until ProcessDone = AllProcessesDone_c`。
- **L138-139** `TbRunning <= false;` 然后 `wait;` —— 这一行是仿真结束的「总闸」。

把 4.4 / 4.6 / 4.7 三节合起来看，闭环是这样的（以单用例、两进程为例）：

1. 上电：`TbRunning=True`，`ProcessDone="00"`，各复位信号为有效值。
2. 复位进程 `p_rst_*` 在 1 µs 后等两个时钟沿，把复位释放到无效值。
3. 各测试进程 `p_Input` / `p_Output` 与 `p_tb_control` 都在 `wait until 复位=无效` 处解除阻塞。
4. 测试进程跑完用户代码，分别置 `ProcessDone(0)<='1'`、`ProcessDone(1)<='1'`，`ProcessDone` 变 `"11"`。
5. `p_tb_control` 的 `wait until ProcessDone = AllProcessesDone_c`（`"11" = "11"`）解除，执行 `TbRunning <= false`。
6. 两个时钟进程的 `while TbRunning loop` 条件失效，跳出循环，执行 `wait;` 永久挂起。
7. 全部进程挂起、无事件，仿真器结束仿真。

#### 4.7.4 代码实践

**目标**：在生成的 simpleTb TB 里，按上述 7 步把「结束链」走一遍。

1. 在生成结果里定位：`signal TbRunning`（4.4）、`p_clock_InClk` / `p_clock_OutClk` 的 `while TbRunning loop`（4.2）、`p_Input` / `p_Output` 末尾的 `ProcessDone(TbProcNr_*_c) <= '1'`（4.6）、`p_tb_control` 的 `wait until ProcessDone = AllProcessesDone_c` 与 `TbRunning <= false`（4.7）。
2. 用笔在 `ProcessDone` 的位宽（`0 to 1`）与两个 `TbProcNr_*_c`（`Input=0`、`Output=1`）之间对齐：两进程都置位后 `ProcessDone = "11" = AllProcessesDone_c`。
3. 解释：若用户在 `p_Input` 里删掉最后的 `ProcessDone(TbProcNr_Input_c) <= '1';`，仿真会怎样？

**预期结果**：能画出「测试进程置位 → `p_tb_control` 检测全 1 → 关 `TbRunning` → 时钟停 → 仿真结束」的因果链；第 3 问的结论是 `ProcessDone` 永远到不了 `"11"`，仿真会**卡死不结束**（时钟一直转、`p_tb_control` 一直等）。

#### 4.7.5 小练习与答案

**Q1**：`AllProcessesDone_c` 是常量、`ProcessDone` 是信号，二者比较的物理含义是什么？

**答**：`ProcessDone` 第 *i* 位为 `'1'` 表示第 *i* 个测试进程已完成；`AllProcessesDone_c` 是全 `'1'` 掩码。两者相等当且仅当**所有**进程的完成位都为 `'1'`，即「全部完成」。这是一个用向量按位相等实现的「与汇集」。

**Q2**：为什么 `p_tb_control` 最后要跟一句 `wait;`？

**答**：`TbRunning <= false` 之后，`p_tb_control` 自身的使命已结束。`wait;` 让这个进程永久挂起，否则进程会从头重新执行（process 在末尾会回到 `begin`），反复写 `TbRunning`。`wait;` 把它「冻结」在一次执行上。

---

## 5. 综合实践

**任务**：端到端追踪一个 `TYPE=CLK; FREQ=100e6; PROC=Input` 的端口（simpleTb 的 `InClk`），画出它「从标签到仿真结束」的完整生命线，并解释 `ProcessDone = AllProcessesDone_c` 如何收尾。

请按以下步骤完成（可本地运行，也可纯源码阅读）：

1. **起点（标签）**：在 [example/simpleTb/psi_common_async_fifo.vhd:39](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/example/simpleTb/psi_common_async_fifo.vhd#L39) 确认 `InClk` 的三条标签 `TYPE=CLK; FREQ=100e6; PROC=Input`。
2. **筛选**：追踪 `_Clocks`（[TbGen.py:51-66](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L51-L66)）如何用 `FilterForTag(ports, TYPE, "clk")` 把 `InClk` 选出来。
3. **检查与取值**：`HasTag(InClk, FREQ)` 通过 → `GetTag(InClk, FREQ)` 得 `"100e6"` → 拼进 `constant Frequency_c : real := real(100e6);`。
4. **进程生成**：写出 `p_clock_InClk`，含 `while TbRunning loop` / `wait for 0.5*(1 sec)/Frequency_c;` / `InClk <= not InClk;`。手算半周期 = 5 ns。
5. **脚手架**：因为 `PROCESSES=Input,Output`，`_TbControlSignals`（[TbGen.py:166-174](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L166-L174)）声明 `ProcessDone(0 to 1)`、`AllProcessesDone_c`、`TbProcNr_Input_c:=0`、`TbProcNr_Output_c:=1`。
6. **收尾链**：`p_Input` / `p_Output` 跑完各自置 `ProcessDone(0/1) <= '1'` → `p_tb_control`（[TbGen.py:122-141](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L122-L141)）检测 `ProcessDone = AllProcessesDone_c`（`"11"="11"`）→ `TbRunning <= false` → `p_clock_InClk` 跳出 `while TbRunning loop` → 仿真结束。

**交付物**：一张标注了「标签 → 筛选 → 检查 → 取值 → 进程体 → 脚手架 → 收尾」的流程图（手绘或文字版均可），并写出对「`ProcessDone = AllProcessesDone_c` 如何让仿真结束」的一句话解释。

> 若本机已装 `pyparsing` / `PyQt5` 且能定位 `PsiPyUtils`，可实际运行 `py TbGen.py -src example/simpleTb/psi_common_async_fifo.vhd -dst ./tb -clear -force` 生成 `tb/psi_common_async_fifo_tb.vhd`，再在产物上验证上述每一步。**若无法运行，所有「生成产物中的具体内容」标注「待本地验证」，但流程图与因果链可基于源码独立完成。**

## 6. 本讲小结

- `_Clocks` / `_Resets` / `_Processes` / `_TbControl` / `_DutInstantiation` 的开头都是同一个动作：**用 `FilterForTag` 把目标端口/generic 筛出来**——筛选是这一层的公共地砖。
- 时钟进程靠 `FREQ` 算半周期 \( T_{\text{half}} = 0.5/f \)，并用 `while TbRunning loop` 受控翻转；缺 `FREQ` 直接报错。
- 复位进程靠 `CLK`（引用另一端口名）归属到具体时钟域，等**两个上升沿**再释放到无效值，保证至少被采样一次；缺 `CLK` 直接报错。
- `_TbControlSignals` 声明五类脚手架，规模由 `tbProcesses` 决定；`ProcessDone` 与 `AllProcessesDone_c` 等宽，是仿真收尾的判据。
- `_Processes` 单用例走「等复位 + 占位 + 置完成位」，多用例走「按 `NextCase` 调度各 case procedure」；`PROC` 标签**只在多用例**经 `GetPortsForProcess` 决定 procedure 参数。
- 仿真收尾闭环：各进程置 `ProcessDone` 比特 → `p_tb_control` 检测全 1 → `TbRunning <= false` → 时钟停 → 仿真结束。

## 7. 下一步学习建议

- **进入 u5（多文件多用例 TB）**：本讲多次提到「多用例分支的 procedure 细节留待 u5」。u5-l1 会讲 `TESTCASES` 如何触发 `isMultiCaseTb`、`NextCase` 如何调度多个用例；u5-l2 会逐行打开 `WriteTbPkg` / `WriteCasePkg`，讲清 `Generics_t` 记录、case 包 procedure 签名，以及 `PortDirectionForProcedure` 如何依据 `PROC`/`TYPE` 推断过程参数方向（本讲里 `args` 只是端口名列表，方向问题在 u5-l2 解决）。
- **回顾 u2-l2**：如果你对本讲的 `GetPortValue`、`LOWACTIVE`、generic 三分类仍觉含糊，回到 u2-l2 把「标签如何影响生成」对照着看，本讲是它的实现侧补充。
- **尝试扩展**：在读懂 `_Clocks` 后，可以构思「如果要支持差分时钟（两个端口一对）标签该怎么设计、`_Clocks` 该怎么改」——这是 u6-l3「添加新标签与新 VHDL 类型」要做的练习的预热。
