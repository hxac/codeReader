# 时钟、复位、进程与控制信号生成

## 1. 本讲目标

本讲是「数据模型与 testbench 生成主流程」单元的第三讲。在 u4-l2 里我们已经看到 `Generate` 是一台**线性写作器**：它不计算、不决策，只按 VHDL 语法顺序自上而下誊抄，内容由标签系统预先决定。本讲要回答的问题是：**这些被誊抄的「动态段落」——时钟、复位、测试进程、仿真控制——内部到底是怎么写出来的？标签是如何一字一句地变成 VHDL 代码的？**

学完本讲你应当能够：

- 看懂 `_Clocks` 如何把 `FREQ` 标签换算成时钟半周期，并手算出任意频率的半周期值。
- 看懂 `_Resets` 如何用 `CLK` 标签把复位「归属」到正确的时钟域，并解释「等两个上升沿」的安全意义。
- 解释 `_TbControlSignals` 与 `_TbControl` 之间「声明—消费」的契约，以及 `ProcessDone = AllProcessesDone_c` 如何让仿真自动结束。
- 理解 `_Processes` 在单用例与多用例两种模式下的分支差异，以及 `PROC` 标签如何绑定端口到过程。
- 理解 `_DutInstantiation` 如何用 `FilterForTag` 汇聚 `EXPORT`/`CONSTANT` 两类 generic 拼出 `generic map`。
- 把 `FilterForTag` 当作贯穿所有生成方法的「统一筛选器」来使用。

---

## 2. 前置知识

本讲假设你已经掌握以下概念（在前序讲义中已建立，这里只做一句话回顾）：

- **标签系统**：`$$ TYPE=CLK; FREQ=100e6 $$` 这类注释标签是工具的输入契约（见 u2-l1、u2-l2）。
- **GetPortValue(port, active)**：端口初值的单一真相源。`active=True` 返回「有效」电平，`active=False` 返回「无效」电平；`LOWACTIVE` 标签会成对翻转极性，向量类型自动包成 `(others => ...)`（见 u2-l2、u4-l1）。
- **Generate 是线性写作器**：各 `_Xxx()` 方法接收并返回同一个 `FileWriter`，靠 `WriteLn/IncIndent/DecIndent/RemoveFromLastLine` 自管理缩进，链式拼出 VHDL（见 u4-l2）。
- **DutInfo / TbInfo 数据模型**：`dutInfo.ports`、`dutInfo.generics`、`tbInfo.tbProcesses`、`tbInfo.isMultiCaseTb` 等字段是本讲所有方法的「原料」（见 u4-l1）。

> 一个贯穿全讲的**调用骨架**先放在这里。`Generate` 在并发语句区按如下顺序调用本讲涉及的方法（见 [TbGen.py:248-252](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L248-L252)，这段代码展示了 `architecture ... begin` 之后五个并发段落的写出顺序）：

```
self._DutInstantiation(f)   # 1. DUT 实例化
self._TbControl(f)          # 2. 仿真主控进程（负责结束仿真）
self._Clocks(f)             # 3. 每个时钟一个 p_clock_* 进程
self._Resets(f)             # 4. 每个复位一个 p_rst_* 进程
self._Processes(f)          # 5. 每个测试过程一个 p_<name> 进程
```

注意输出顺序（`_TbControl` 在 `_Clocks` 之前）与**概念依赖顺序**（`_TbControl` 要等 `_Processes` 置位 `ProcessDone`）是相反的——这正是「线性誊抄器」的特点：写在前面的代码，运行时可能等在后面。

---

## 3. 本讲源码地图

| 文件 | 本讲关注的内容 |
| --- | --- |
| [TbGen.py](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py) | `TbGenerator` 类的六个生成方法：`_DutInstantiation`、`_Clocks`、`_Resets`、`_Processes`、`_TbControl`、`_TbControlSignals`，以及 `Generate` 的调用顺序。 |
| [DutInfo.py](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/DutInfo.py) | `FilterForTag`（以及配套的 `HasTag`/`GetTag`/`HastTagValue`）——所有生成方法共用的标签筛选器；`GetPortValue`、`Tags` 常量类。 |
| [TbInfo.py](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbInfo.py) | `GetPortsForProcess`——`_Processes` 在多用例模式下用来收集过程参数列表。 |
| [example/simpleTb/psi_common_async_fifo.vhd](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/example/simpleTb/psi_common_async_fifo.vhd) | 异步 FIFO 示例 DUT，含两个时钟域、两个复位、`PROCESSES=Input,Output`，是本讲所有实践的靶子。 |

---

## 4. 核心概念与源码讲解

本讲按「**先讲筛选器地基，再讲六个生成方法**」的顺序展开。六个方法之间并非孤立，它们共享同一套「**筛选 → 检查 → 取值 → 誊抄**」的四步套路，所以我们先把这套套路的引擎 `FilterForTag` 讲透。

### 4.1 FilterForTag —— 标签驱动的统一筛选器

#### 4.1.1 概念说明

u2-l1 已经介绍过 `FilterForTag` 的**用途**（按标签从一堆端口/generic 里筛出符合条件的子集）。本讲从**实现**角度再深挖一层，因为本讲后面每个生成方法的第一行几乎都是它。

设计意图：端口和 generic 在数据模型里是**无序集合**（一个 list），而生成器需要的是「所有时钟端口」「所有复位端口」「所有需要导出的 generic」这样的**子集**。`FilterForTag` 就是这个「按标签切片」的操作。它的关键能力有三：

1. **标签存在性筛选**：只传 `tag` 不传 `value` 时，筛出「带这个标签」的全部对象。
2. **标签值匹配筛选**：同时传 `tag` 和 `value` 时，进一步要求标签值等于给定值。
3. **值类型归一**：标签值在解析层可能是 `str`（单值）也可能是 `list`（列表），匹配时统一当成列表处理，所以 `PROC=Output,Input` 这种列表也能命中 `value="input"`。

#### 4.1.2 核心流程

```
输入: list（端口或 generic 的可迭代对象）, tag, value=None
对 list 中每个元素 e:
    1. 解析 e.comment 里的 $$ ... $$ 标签 → tags 字典
    2. 若 tag 不在 tags 中            → 跳过
    3. 若 value is None               → 直接收下 e（存在性筛选）
    4. 否则把 tags[tag] 归一成 list:
         - 若是 str → 包成 [str]
         - 若是 list → 原样
    5. 大小写不敏感地比较 value 是否在归一列表中 → 命中则收下 e
输出: 收下的元素组成的新 list
```

注意第 4 步的归一：`casesensitive=False`（默认）时，会把列表里每个值 `.lower()` 后再与 `value.lower()` 比较，这就是「标签名和值都大小写不敏感」的来源。

#### 4.1.3 源码精读

`FilterForTag` 的完整实现（[DutInfo.py:147-166](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/DutInfo.py#L147-L166)，这段代码展示了「存在性筛选」与「值匹配筛选」两条分支，以及值类型归一与大小写归一）：

```python
@classmethod
def FilterForTag(cls, list : Iterable, tag : str, value : str = None, casesensitive : bool = False) -> List:
    l = []
    tag = tag.lower()
    for e in list:
        tags = cls._ParseTags(e.comment)
        if tag in tags:
            if value is None:
                l.append(e)
            else:
                tagValue = tags[tag]
                tagValueList = [tagValue] if type(tagValue) is str else tagValue
                tagValueListLower = [x.lower() for x in tagValueList]
                if casesensitive:
                    if value in tagValueList:
                        l.append(e)
                else:
                    if value.lower() in tagValueListLower:
                        l.append(e)
    return l
```

它的三个常用搭档（[DutInfo.py:114-138](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/DutInfo.py#L114-L138)，依次为 `HastTagValue`、`HasTag`、`GetTag`）构成「检查—取值」的另一半：

- `HasTag(obj, tag)`：obj 是否带某标签。
- `HastTagValue(obj, tag, value)`：obj 的某标签值是否等于 value（默认大小写不敏感）。
- `GetTag(obj, tag)`：取出 obj 的某标签值（不存在则抛异常）。

本讲后续会反复出现这个「三连招」：先 `FilterForTag` 切片，再 `HasTag` 检查必填项，最后 `GetTag` 取具体值。

#### 4.1.4 代码实践

**目标**：亲手验证 `FilterForTag` 的值匹配与大小写不敏感行为。

**步骤**（源码阅读型 + 可选运行）：

1. 打开示例 DUT，找到这几个端口的标签：
   - `InClk`：`$$ TYPE=CLK; FREQ=100e6; PROC=Input $$`
   - `OutClk`：`$$ TYPE=CLK; FREQ=125e6; Proc=Output $$`（注意 `Proc` 大写首字母）
   - `OutRdy`：`$$ PROC=Output,Input $$`
2. 预测 `DutInfo.FilterForTag(ports, Tags.PROC, "input")` 会返回哪几个端口。注意 `OutClk` 的 `Proc=Output`（不是 `input`），`OutRdy` 的列表含 `Input`。
3. 若本机已装好 `PsiPyUtils`/`pyparsing`，可在项目根目录写一个临时脚本（**示例代码，非项目原有文件**）：

```python
from DutInfo import DutInfo
d = DutInfo("example/simpleTb/psi_common_async_fifo.vhd")
hit = DutInfo.FilterForTag(d.ports, "proc", "input")
print([p.name for p in hit])
```

**需要观察的现象**：返回结果应包含 `InClk`、`InData`、`InVld`、`InRdy`、`OutRdy`，且**不包含** `OutClk`（它是 `Proc=Output`）。

**预期结果**：列表里同时出现 `OutRdy`（`PROC=Output,Input`，列表归一后命中 `input`），证明列表值与大小写不敏感都生效。若环境不可用，按上面逻辑手推即可，结果标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：`FilterForTag(ports, Tags.TYPE, "clk")` 与 `FilterForTag(ports, Tags.TYPE)`（不传 value）有何区别？

**答案**：前者只收 `TYPE` 值为 `clk` 的端口（值匹配筛选）；后者收**所有带 `TYPE` 标签**的端口，不论值是 `clk`、`rst` 还是 `sig`（存在性筛选）。

**练习 2**：为什么 `OutRdy` 的标签写成 `PROC=Output,Input`，用 `FilterForTag(..., "proc", "output")` 和 `...("proc", "input")` 都能命中它？

**答案**：因为 `_ParseTags` 把逗号分隔的值解析成 list `["Output", "Input"]`，`FilterForTag` 第 158 行把它归一成列表后逐元素比较，两个值都能命中。

---

### 4.2 _DutInstantiation —— EXPORT 与 CONSTANT 的汇聚点

> 我们先讲 `_DutInstantiation`，因为它最直接地展示了 `FilterForTag` 如何驱动生成，且它产出的 `generic map` 是理解 generic 三分类（u4-l2）的落点。

#### 4.2.1 概念说明

`_DutInstantiation` 生成一段「直接实体实例化」（VHDL 的 `i_dut : entity <lib>.<name>` 语法），把 TB 顶层的信号连到 DUT 端口，并把该传的 generic 传进去。它的关键决策只有一个：**哪些 generic 要进 `generic map`？**

答案是 u4-l2 讲过的 generic 三分类中的前两类：

- `EXPORT=true`：从 TB 实体 generic 传入（对外可配）。
- `CONSTANT=值`：在 TB 内部固定为常量。

两者**都要**出现在 `generic map` 里（因为它们都是「TB 显式驱动」的 generic）。第三类（两者皆无）则**不进** `generic map`，让 DUT 用自己的默认值。端口则无差别地全部进 `port map`，名字一一对应（TB 信号与 DUT 端口同名）。

#### 4.2.2 核心流程

```
1. 写标题 "DUT Instantiation"
2. 写 "i_dut : entity <dutLibrary>.<name>" 并缩进
3. 收集要进 generic map 的 generic:
     eg = FilterForTag(generics, EXPORT, "true") + FilterForTag(generics, CONSTANT)
4. 若 eg 非空:
     写 "generic map ("，对每个 g 写 "g.name => g.name,"，删最后一行尾逗号，写 ")"
5. 写 "port map ("，对每个端口写 "p.name => p.name,"，删尾逗号，写 ");"
```

注意 `g.name => g.name`：左右同名，意味着 TB 里有一个与 generic 同名的常量/实体 generic 喂给 DUT。`RemoveFromLastLine(1)` 用来回改最后一个多余的分号/逗号——这是 `FileWriter` 的尾标点回改手法（见 u4-l2）。

#### 4.2.3 源码精读

完整方法（[TbGen.py:33-49](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L33-L49)，这段代码展示了 generic map 由 EXPORT 与 CONSTANT 两类 generic 拼接、port map 全量端口直连）：

```python
def _DutInstantiation(self, f : FileWriter) -> FileWriter:
    VhdlTitle("DUT Instantiation", f)
    f.WriteLn("i_dut : entity {}.{}".format(self.dutInfo.dutLibrary, self.dutInfo.name)).IncIndent()
    generics = self.dutInfo.generics
    eg = (DutInfo.FilterForTag(generics, Tags.EXPORT, "true") + DutInfo.FilterForTag(generics, Tags.CONSTANT))
    if len(eg) > 0:
        f.WriteLn("generic map (").IncIndent()
        for g in eg:
            f.WriteLn("{} => {},".format(g.name, g.name))
        f.RemoveFromLastLine(1)
        f.DecIndent().WriteLn(")")
    f.WriteLn("port map (").IncIndent()
    for p in self.dutInfo.ports:
        f.WriteLn("{} => {},".format(p.name, p.name))
    f.RemoveFromLastLine(1)
    f.DecIndent().WriteLn(");").DecIndent()
    return f
```

第 37 行的 `+` 是 Python 列表拼接：把「导出类」和「固定常量类」两个子集首尾相接，合成一个 `generic map` 顺序。对照 simpleTb 的 generic（[example/simpleTb/psi_common_async_fifo.vhd:30-35](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/example/simpleTb/psi_common_async_fifo.vhd#L30-L35)，这段 VHDL 标注了每个 generic 的 `EXPORT`/`CONSTANT` 标签）：

| generic | 标签 | 是否进 generic map | 理由 |
| --- | --- | --- | --- |
| `Width_g` | `EXPORT=true` | 是 | 导出类 |
| `Depth_g` | `EXPORT=true; funky=bla` | 是 | 导出类（`funky` 是无关标签，不影响） |
| `AlmFullOn_g` | `EXPORT=false,funky=blubb` | 否 | 值不是字符串 `"true"` |
| `AlmFullLevel_g` | `CONSTANT=12` | 是 | 固定常量类 |
| `AlmEmptyOn_g` | 无 | 否 | 第三类，用 DUT 默认值 |
| `AlmEmptyLevel_g` | 无 | 否 | 第三类，用 DUT 默认值 |

所以最终 `generic map` 里会出现 `Width_g`、`Depth_g`、`AlmFullLevel_g` 三项。

> 关键细节：第 37 行对 `EXPORT` 的匹配用了**精确字符串 `"true"`**，而 `AlmFullOn_g` 的值是 `EXPORT=false,funky=blubb`——经过 `_ParseTags` 它会被切成列表 `["false", "blubb"]`，其中没有 `"true"`，故被排除。这就是为什么 `EXPORT=false` 等同于「不导出」。

#### 4.2.4 代码实践

**目标**：在不运行的情况下，预测 simpleTb 生成的 `generic map` 内容，再对照真实生成结果。

**步骤**：

1. 按 4.2.3 的表格，手写出你预期的 `generic map` 三行。
2. 如果环境可用，运行 `py TbGen.py -src example/simpleTb/psi_common_async_fifo.vhd -dst tb -clear -force`，打开 `tb/psi_common_async_fifo_tb.vhd`，定位 `DUT Instantiation` 段。
3. 用 diff 工具或肉眼对比你的预测与实际输出。

**需要观察的现象**：`generic map` 仅含 `Width_g`、`Depth_g`、`AlmFullLevel_g`；`port map` 含全部端口（含 `InData`、`OutRdy` 等）。

**预期结果**：与表格一致。若 `AlmFullLevel_g` 的值是 `12`（来自 `CONSTANT=12`），证明固定常量类确实进了 map。环境不可用时标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `AlmFullOn_g` 的标签改成 `EXPORT=true`，`generic map` 会如何变化？

**答案**：`AlmFullOn_g` 会被 `FilterForTag(generics, EXPORT, "true")` 命中，加入 `eg`，于是 `generic map` 多出一行 `AlmFullOn_g => AlmFullOn_g,`，同时它也会出现在 TB 实体的 `generic` 子句里（由 `_EntityDeclaration` 处理）。

**练习 2**：为什么 `port map` 里所有端口都写 `p.name => p.name`，而不需要像 generic 那样分类？

**答案**：因为 TB 为每个 DUT 端口都生成了一个同名 signal（见 4.6 与 u4-l2 的 `_DutSignals`），端口可以无差别地一一对接；generic 才有「是否由 TB 驱动」的三分类问题。

---

### 4.3 _Clocks —— FREQ 标签如何变成时钟半周期

#### 4.3.1 概念说明

`_Clocks` 为每个 `TYPE=CLK` 的端口生成一个 `p_clock_<name>` 进程，该进程在仿真期间持续翻转信号、产生方波时钟。它**强制要求**每个时钟端口必须带 `FREQ` 标签（缺了直接抛异常），因为半周期必须由频率算出来。

这里有一个关键的设计约束：时钟进程**不是无限运行**的，它受 `TbRunning` 信号控制——`while TbRunning loop`。当仿真主控进程把 `TbRunning` 拉低（见 4.5），所有时钟进程的循环退出，执行 `wait;` 永久挂起，仿真才得以结束。这是 TbGenerator 让仿真「自动收尾」的第一条线索。

#### 4.3.2 核心流程

每个时钟端口的生成步骤：

```
1. 标题 "Clocks !DO NOT EDIT!"
2. 对每个 clk in FilterForTag(ports, TYPE, "clk"):
     a. 若 clk 无 FREQ 标签 → 抛 "Clock <name> has not FREQ tag!"
     b. 写 "p_clock_<name> : process"
     c. 写 "constant Frequency_c : real := real(<FREQ 值>);"
     d. 写 "begin"
     e. 写 "while TbRunning loop"
     f. 写 "wait for 0.5*(1 sec)/Frequency_c;"   ← 半周期
     g. 写 "<name> <= not <name>;"               ← 翻转
     h. 写 "end loop;"
     i. 写 "wait;"                                ← TbRunning 变 false 后挂起
     j. 写 "end process;"
```

**半周期数学**：设频率为 \(f\)（Hz），则周期 \(T = 1/f\)，半周期为：

\[
t_{\text{half}} = \frac{T}{2} = \frac{1}{2f}
\]

代码里把 `1 sec`（VHDL 的字面时间量）作为分子，避免手写单位换算：

\[
t_{\text{half}} = \frac{0.5 \times 1\,\text{s}}{f}
\]

以 simpleTb 的两个时钟为例：

| 端口 | FREQ | 代入公式 | 半周期 |
| --- | --- | --- | --- |
| `InClk` | \(100\times10^{6}\) | \(0.5 / 10^{8}\,\text{s}\) | \(5\,\text{ns}\) |
| `OutClk` | \(125\times10^{6}\) | \(0.5 / (1.25\times10^{8})\,\text{s}\) | \(4\,\text{ns}\) |

`real(...)` 把 `FREQ` 标签里的原始字符串（如 `100e6`）转成浮点数 `100000000.0`，VHDL 用 `1 sec / real` 得到正确的时间量纲。

#### 4.3.3 源码精读

完整方法（[TbGen.py:51-66](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L51-L66)，这段代码展示了 FREQ 必填检查、Frequency_c 常量声明、`while TbRunning` 受控翻转与半周期等待）：

```python
def _Clocks(self, f : FileWriter) -> FileWriter:
    VhdlTitle("Clocks !DO NOT EDIT!", f)
    for clk in DutInfo.FilterForTag(self.dutInfo.ports, Tags.TYPE, "clk"):
        if not DutInfo.HasTag(clk, Tags.FREQ):
            raise Exception("Clock {} has not FREQ tag!".format(clk.name))
        f.WriteLn("p_clock_{} : process".format(clk.name)).IncIndent()
        f.WriteLn("constant Frequency_c : real := real({});".format(DutInfo.GetTag(clk, Tags.FREQ))).DecIndent()
        f.WriteLn("begin").IncIndent()
        f.WriteLn("while TbRunning loop").IncIndent()
        f.WriteLn("wait for 0.5*(1 sec)/Frequency_c;")
        f.WriteLn("{name} <= not {name};".format(name=clk.name))
        f.DecIndent().WriteLn("end loop;")
        f.WriteLn("wait;").DecIndent()
        f.WriteLn("end process;")
        f.WriteLn()
    return f
```

这正是 4.1 提到的「三连招」范本：

- `FilterForTag(..., TYPE, "clk")`：切片，拿到所有时钟端口。
- `HasTag(clk, FREQ)`：检查必填项。
- `GetTag(clk, FREQ)`：取出频率值。

第 56-57 行有个缩进细节值得注意：先 `IncIndent()` 写进程头，再 `DecIndent()` 写 `Frequency_c`（让常量声明比进程头少一级缩进，对齐 VHDL 声明区），这体现了「誊抄器」对格式精度的控制。

#### 4.3.4 代码实践

**目标**：追踪 `InClk`（`TYPE=CLK; FREQ=100e6; PROC=Input`）从标签到 `p_clock_InClk` 进程的完整路径，并手算半周期。

**步骤**：

1. **解析层**：`InClk` 的 `.comment` 是 `-- $$ TYPE=CLK; FREQ=100e6; PROC=Input $$`，经 `_ParseTags` 得到 `{"type":"clk", "freq":"100e6", "proc":"Input"}`。
2. **筛选层**：`FilterForTag(ports, TYPE, "clk")` 因 `type=="clk"` 命中，`InClk` 进入循环。
3. **检查层**：`HasTag(InClk, FREQ)` 为真，不抛异常。
4. **取值层**：`GetTag(InClk, FREQ)` 返回字符串 `"100e6"`。
5. **誊抄层**：`real(100e6)` → `100000000.0`，写出 `wait for 0.5*(1 sec)/Frequency_c;`。
6. **数学层**：\(0.5 / 10^{8}\,\text{s} = 5\,\text{ns}\)。
7. 若环境可用，生成 TB 后在 `p_clock_InClk` 进程里确认 `Frequency_c` 与半周期语句。

**需要观察的现象**：生成的 `p_clock_InClk` 与 `p_clock_OutClk` 各自的 `Frequency_c` 分别是 `1.0e8` 与 `1.25e8`；`InClk` 每 5 ns 翻转一次，`OutClk` 每 4 ns 翻转一次。

**预期结果**：两个时钟进程都在 `while TbRunning loop` 内翻转。环境不可用时，步骤 1-6 的手推结果即答案（标注「待本地验证」生成部分）。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `InClk` 的 `FREQ` 标签删掉只留 `TYPE=CLK`，运行 `Generate` 会发生什么？

**答案**：第 54-55 行的 `HasTag` 返回 False，抛出 `Exception("Clock InClk has not FREQ tag!")`，CLI 捕获后打印 `ERROR: ...` 并 `exit(-1)`（见 [TbGen.py:312-314](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L312-L314)）。

**练习 2**：为什么用 `wait for 0.5*(1 sec)/Frequency_c` 而不是直接写 `wait for 5 ns`？

**答案**：因为频率由标签驱动、在生成时才确定，写死 `5 ns` 就失去了「频率可配」的灵活性；用公式让任意 `FREQ` 都能自动换算出正确半周期，且 `1 sec` 保证了 VHDL 时间量纲正确。

---

### 4.4 _Resets —— CLK 标签如何把复位归属到正确时钟域

#### 4.4.1 概念说明

`_Resets` 为每个 `TYPE=RST` 的端口生成一个 `p_rst_<name>` 进程。复位的核心问题是**归属**：异步 FIFO 有两个独立的时钟域（`InClk`/`OutClk`），每个复位必须等到「它所属的那个时钟」采样到它，复位才有意义。这个归属关系由 `CLK` 标签显式声明（如 `InRst` 标 `CLK=InClk`，`OutRst` 标 `CLK=OutClk`）。

复位信号的**初始值**不在 `_Resets` 里设，而是在 `_DutSignals` 里设为**有效**电平（见 u4-l2 与 4.6）。`_Resets` 只负责「在合适时机把复位释放为无效」。

#### 4.4.2 核心流程

```
1. 标题 "Resets"
2. 对每个 rst in FilterForTag(ports, TYPE, "rst"):
     a. 若 rst 无 CLK 标签 → 抛 "Reset <name> has not CLK tag!"
     b. clkName = GetTag(rst, CLK)
     c. 写 "p_rst_<name> : process" / "begin"
     d. 写 "wait for 1 us;"                          ← 复位保持一段时间
     e. 写注释 "-- Wait for two clk edges ..."
     f. 写 "wait until rising_edge(<clkName>);"（两遍）← 等两个上升沿
     g. 写 "<rst> <= <GetPortValue(rst, False)>;"     ← 释放为无效
     h. 写 "wait;" / "end process;"
```

**为什么要等两个上升沿？** 复位信号初始为有效（在 `_DutSignals` 中设）。`wait for 1 us` 保证复位在仿真启动初期就有效；随后**连续等两个** `rising_edge(<clkName>)`，再释放为无效。两个沿（而非一个）给采样留出余量：即便第一个沿恰好与复位释放竞争，第二个沿也能确保 DUT 的时序逻辑至少完整采样到一次「复位有效」。这是 testbench 复位的经典稳妥写法。

**释放值**：`GetPortValue(rst, False)` 返回**无效**电平。对于高有效的 `InRst`（无 `LOWACTIVE` 标签），无效值是 `'0'`；若标了 `LOWACTIVE=true`，无效值翻转为 `'1'`（见 [DutInfo.py:68-79](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/DutInfo.py#L68-L79)，`GetPortValue` 据 `LOWACTIVE` 与 `active` 参数决定电平）。

#### 4.4.3 源码精读

完整方法（[TbGen.py:68-84](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L68-L84)，这段代码展示了 CLK 必填检查、归属时钟名取出、`wait for 1 us` 与两个 `rising_edge` 的释放时序）：

```python
def _Resets(self, f : FileWriter) -> FileWriter:
    VhdlTitle("Resets", f)
    for rst in DutInfo.FilterForTag(self.dutInfo.ports, Tags.TYPE, "rst"):
        if not DutInfo.HasTag(rst, Tags.CLK):
            raise Exception("Reset {} has not CLK tag!".format(rst.name))
        clkName = DutInfo.GetTag(rst, Tags.CLK)
        f.WriteLn("p_rst_{} : process".format(rst.name))
        f.WriteLn("begin").IncIndent()
        f.WriteLn("wait for 1 us;")
        f.WriteLn("-- Wait for two clk edges to ensure reset is active for at least one edge")
        f.WriteLn("wait until rising_edge({});".format(clkName))
        f.WriteLn("wait until rising_edge({});".format(clkName))
        f.WriteLn("{} <= {};".format(rst.name, self.dutInfo.GetPortValue(rst, False)))
        f.WriteLn("wait;").DecIndent()
        f.WriteLn("end process;")
        f.WriteLn()
    return f
```

注意 `clkName` 是一个**字符串**（`"InClk"`），它直接被插进 `rising_edge(...)`——这要求 `CLK` 标签引用的端口名必须与实际时钟信号名一致。这是 TbGenerator 的一个隐含契约：**复位的 `CLK` 标签值必须是另一个端口的 `name`**。

对照 simpleTb：`InRst` 标 `CLK=InClk`（[example/simpleTb/psi_common_async_fifo.vhd:40](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/example/simpleTb/psi_common_async_fifo.vhd#L40)，`InRst` 声明及其 `TYPE=RST; CLK=InClk` 标签），故生成 `wait until rising_edge(InClk);` 两次，然后 `InRst <= '0';`。

#### 4.4.4 代码实践

**目标**：验证复位「归属」与「释放值」都正确。

**步骤**：

1. 对 `InRst`（`TYPE=RST; CLK=InClk`，无 `LOWACTIVE`）手写出生成的 `p_rst_InRst` 进程体，重点写对 `rising_edge(InClk)` 与 `InRst <= '0';`。
2. 对 `OutRst`（`TYPE=RST; CLK=OutClk`）同样手写，确认归属到 `OutClk`。
3. 思考实验：如果把 `InRst` 加上 `LOWACTIVE=true`，释放行会变成什么？

**需要观察的现象**：`p_rst_InRst` 与 `p_rst_OutClk` 分别等待不同时钟的上升沿；释放值都是 `'0'`（高有效复位）。

**预期结果**：第 3 步中，`GetPortValue(InRst, False)` 因 `LOWACTIVE=true` 返回 `'1'`（低有效复位的「无效」是高电平），故释放行变为 `InRst <= '1';`，同时 `_DutSignals` 里 `InRst` 的初始值也会翻转为 `'0'`（有效）。环境不可用时标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `_Resets` 用 `GetPortValue(rst, False)`（无效）而不是 `True`（有效）？

**答案**：因为复位的**有效**初值已由 `_DutSignals` 设定（`active=True`），`_Resets` 的职责是在两个上升沿后**释放**复位，所以写的是无效电平（`active=False`）。

**练习 2**：若一个 `TYPE=RST` 端口漏写了 `CLK` 标签，会怎样？

**答案**：第 71-72 行 `HasTag` 返回 False，抛 `Exception("Reset <name> has not CLK tag!")`，生成中止。这说明 `CLK` 是复位端口的必填标签，正如 `FREQ` 是时钟端口的必填标签。

---

### 4.5 _TbControlSignals 与 _TbControl —— 仿真终止的契约

> 这两个方法是一对：`_TbControlSignals` 在声明区**声明**一组控制信号，`_TbControl` 在并发区**消费**它们。它们共同回答「仿真什么时候、怎么结束」。

#### 4.5.1 概念说明

一个 testbench 必须有明确的终止条件，否则仿真不会停。TbGenerator 用一个简单的**握手协议**实现自动收尾：

- 每个**测试进程**（`p_Input`、`p_Output` 等）跑完自己的任务后，把自己在 `ProcessDone` 向量里的那一比特置 `'1'`。
- 一个**主控进程** `p_tb_control` 等待 `ProcessDone = AllProcessesDone_c`（即所有比特全 `'1'`），然后把 `TbRunning` 拉低。
- `TbRunning` 拉低后，所有 `while TbRunning loop` 的时钟进程退出循环、执行 `wait;` 挂起，仿真再无事件，即告结束。

这套协议需要五个控制信号/常量来支撑，全部由 `_TbControlSignals` 声明。

#### 4.5.2 核心流程

**`_TbControlSignals`**（声明区）：

```
1. 标题 "TB Control"
2. 写 "signal TbRunning : boolean := True;"        ← 仿真运行标志，初值 True
3. 写 "signal NextCase : integer := -1;"           ← 多用例调度指针，初值 -1
4. 写 "signal ProcessDone : std_logic_vector(0 to N-1) := (others => '0');"   ← N = 进程数
5. 写 "constant AllProcessesDone_c : std_logic_vector(0 to N-1) := (others => '1');"
6. 对每个进程 p（带下标 i）写 "constant TbProcNr_<p>_c : integer := i;"
```

**`_TbControl`**（并发区）：

```
1. 标题 "Testbench Control !DO NOT EDIT!"
2. 写 "p_tb_control : process" / "begin"
3. 若有复位端口: wait until <所有复位同时为无效>      ← 等复位释放再开始计时
4. 若 isMultiCaseTb:
     对每个用例 i: NextCase <= i; wait until ProcessDone = AllProcessesDone_c;
   否则:
     wait until ProcessDone = AllProcessesDone_c;
5. 写 "TbRunning <= false;"                          ← 结束仿真
6. 写 "wait;" / "end process;"
```

**向量的宽度**由 `len(self.tbInfo.tbProcesses)` 决定。simpleTb 的 `PROCESSES=Input,Output` → `tbProcesses = ["Input", "Output"]` → `N = 2`（见 [TbInfo.py:26-30](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbInfo.py#L26-L30)，`tbProcesses` 的缺省值与赋值）。于是：

- `ProcessDone : std_logic_vector(0 to 1)`
- `AllProcessesDone_c : std_logic_vector(0 to 1) := (others => '1')`（即 `"11"`）
- `TbProcNr_Input_c : integer := 0`、`TbProcNr_Output_c : integer := 1`

**终止条件**：当 `p_Input` 置 `ProcessDone(0) <= '1'` **且** `p_Output` 置 `ProcessDone(1) <= '1'` 后，`ProcessDone = "11" = AllProcessesDone_c` 成立，`p_tb_control` 解除等待，`TbRunning <= false`。

> 第 3 步的 `wait until <所有复位无效>` 是个小细节：主控进程也要先等复位释放（与每个测试进程开头的等待逻辑一致），避免在复位仍有效时就开始统计 `ProcessDone`。其 `rstLogic` 拼法见 4.6 的 `_Processes`，二者用同一段代码。

#### 4.5.3 源码精读

**`_TbControlSignals`**（[TbGen.py:166-174](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L166-L174)，这段代码声明了 TbRunning/NextCase/ProcessDone/AllProcessesDone_c 四个信号，并按 `tbProcesses` 顺序为每个进程生成 `TbProcNr_<p>_c` 常量）：

```python
def _TbControlSignals(self, f : FileWriter) -> FileWriter:
    VhdlTitle("TB Control", f, 2)
    f.WriteLn("signal TbRunning : boolean := True;")
    f.WriteLn("signal NextCase : integer := -1;")
    f.WriteLn("signal ProcessDone : std_logic_vector(0 to {}) := (others => '0');".format(len(self.tbInfo.tbProcesses)-1))
    f.WriteLn("constant AllProcessesDone_c : std_logic_vector(0 to {}) := (others => '1');".format(len(self.tbInfo.tbProcesses)-1))
    for i, p in enumerate(self.tbInfo.tbProcesses):
        f.WriteLn("constant TbProcNr_{}_c : integer := {};".format(p, i))
    return f
```

注意第 170-171 行用 `len(...)-1` 作为向量上界：长度为 2 时上界为 1，即 `0 to 1`，这是 VHDL `to` 范围的标准写法。

**`_TbControl`**（[TbGen.py:122-141](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L122-L141)，这段代码展示了复位等待、单/多用例分支、`ProcessDone = AllProcessesDone_c` 收尾条件与 `TbRunning <= false`）：

```python
def _TbControl(self, f : FileWriter) -> FileWriter:
    VhdlTitle("Testbench Control !DO NOT EDIT!", f)
    f.WriteLn("p_tb_control : process")
    f.WriteLn("begin").IncIndent()
    rsts = DutInfo.FilterForTag(self.dutInfo.ports, Tags.TYPE, "rst")
    if len(rsts) > 0:
        rstLogic = " and ".join([r.name + " = " + self.dutInfo.GetPortValue(r, False) for r in rsts])
        f.WriteLn("wait until {};".format(rstLogic))
    if self.tbInfo.isMultiCaseTb:
        for i, c in enumerate(self.tbInfo.testCases):
            f.WriteLn("-- {}".format(c))
            f.WriteLn("NextCase <= {};".format(i))
            f.WriteLn("wait until ProcessDone = AllProcessesDone_c;")
    else:
        f.WriteLn("wait until ProcessDone = AllProcessesDone_c;")
    #end of TB
    f.WriteLn("TbRunning <= false;")
    f.WriteLn("wait;")
    f.DecIndent().WriteLn("end process;")
    return f
```

第 128 行的 `rstLogic` 是一个 Python 字符串拼接：把每个复位端口拼成 `名 = '0'`，再用 ` and ` 连起来。对 simpleTb 得到 `InRst = '0' and OutRst = '0'`，即「两个复位都释放」才继续。

多用例分支（第 130-134 行）会在 u5 详细展开，这里只需理解：它把 `NextCase` 依次置为 `0,1,2,...`，每置一次就等所有进程处理完该用例（`ProcessDone = AllProcessesDone_c`），再进入下一用例。

#### 4.5.4 代码实践

**目标**：解释 `ProcessDone = AllProcessesDone_c` 如何让仿真结束。

**步骤**：

1. 在生成的 TB 中找到 `ProcessDone`、`AllProcessesDone_c`、`TbProcNr_Input_c`、`TbProcNr_Output_c` 四处声明，确认向量宽度是 `0 to 1`。
2. 找到 `p_Input` 与 `p_Output` 进程末尾的 `ProcessDone(TbProcNr_Input_c) <= '1';` 与 `... <= '1';`（这两行由 `_Processes` 生成，见 4.6）。
3. 找到 `p_tb_control` 中的 `wait until ProcessDone = AllProcessesDone_c;` 与 `TbRunning <= false;`。
4. 画出时序：两个测试进程先后置位 → `ProcessDone` 变 `"11"` → 主控进程解除等待 → `TbRunning` 变 false → 时钟进程退出 `while TbRunning loop` → 仿真无事件，结束。

**需要观察的现象**：只有**两个**测试进程都置位后，仿真才会结束；只要还有一个没跑完，`ProcessDone` 就不全 `'1'`，主控进程一直等待。

**预期结果**：手动模拟 `ProcessDone` 从 `"00"` → `"01"`（`p_Input` 先完成）→ `"11"`（`p_Output` 也完成）→ `TbRunning <= false`。环境不可用时标注「待本地验证」。

#### 4.5.5 小练习与答案

**练习 1**：如果 `PROCESSES` 标签声明了 3 个进程，`ProcessDone` 的宽度是多少？`AllProcessesDone_c` 的值是什么？

**答案**：`len(tbProcesses)-1 = 2`，所以 `ProcessDone : std_logic_vector(0 to 2)`，宽度 3；`AllProcessesDone_c := (others => '1')` 即 `"111"`。

**练习 2**：为什么 `p_tb_control` 开头也要 `wait until <复位无效>`？

**答案**：确保仿真「计时」从复位释放后才开始，避免在复位仍有效、测试进程尚未真正运行时就误判 `ProcessDone`，保证收尾握手发生在正确的仿真阶段。

---

### 4.6 _Processes —— PROC 标签绑定与 ProcessDone 信令

#### 4.6.1 概念说明

`_Processes` 为 `tbInfo.tbProcesses` 里的每个名字生成一个 `p_<name>` 进程。这是用户写测试激励的地方——生成器只搭好骨架（含 `begin`/`end process`、复位等待、`ProcessDone` 信令），中间留一段 `-- User Code` 让用户填。

它有两条分支：

- **单用例**（`isMultiCaseTb == False`，本讲重点）：每个进程先等复位释放，再留 User Code 占位，最后置 `ProcessDone` 比特。
- **多用例**（`isMultiCaseTb == True`，详见 u5）：每个进程按 `NextCase` 调度多个用例，每个用例调用对应 case 包里的 procedure，调用完置 `ProcessDone`。

本讲聚焦单用例分支，但会指出多用例分支如何用 `PROC` 标签收集 procedure 参数。

#### 4.6.2 核心流程

**单用例分支**（每个进程 `p`）：

```
1. 子标题 p（level 2）
2. 写 "p_<p> : process" / "begin"
3. 若有复位端口:
     写注释 "-- start of process !DO NOT EDIT"
     rstLogic = 每个复位拼 "名 = 无效值"，用 " and " 连接
     写 "wait until <rstLogic>;"            ← 等复位释放
4. 写 "-- User Code" 占位（含 assert 提示）
5. 写 "ProcessDone(TbProcNr_<p>_c) <= '1';"  ← 本进程完成信令
6. 写 "wait;" / "end process;"
```

**多用例分支**（每个进程 `p`，遍历每个用例 `c`）：

```
对每个用例 c（带下标 i）:
     写 "wait until NextCase = i;"
     写 "ProcessDone(TbProcNr_<p>_c) <= '0';"            ← 开始前清零
     args = GetPortsForProcess(p) 里所有端口名逗号连接
     写 "work.<tb>_case_<c>.<p>(<args>, Generics_c);"   ← 调用 case 包的 procedure
     写 "wait for 1 ps;"
     写 "ProcessDone(TbProcNr_<p>_c) <= '1';"            ← 用例完成信令
```

`GetPortsForProcess(p)` 就是 `FilterForTag(ports, PROC, p)`（见 [TbInfo.py:47-48](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbInfo.py#L47-L48)，它直接转发给 `DutInfo.FilterForTag`），即「所有 `PROC=<p>` 的端口」。所以在多用例 TB 里，`PROC` 标签决定了一个端口出现在哪些过程的 procedure 参数列表里。这正是 u2-l2 提到的「`PROC` 仅在多用例 TB 中决定 procedure 参数」的落点。

#### 4.6.3 源码精读

完整方法（[TbGen.py:86-120](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L86-L120)，这段代码展示了单/多用例分支、复位等待、User Code 占位与 `ProcessDone` 信令）：

```python
def _Processes(self, f : FileWriter) -> FileWriter:
    if self.tbInfo.isMultiCaseTb:
        VhdlTitle("Processes !DO NOT EDIT!", f)
    else:
        VhdlTitle("Processes", f)
    #Generate processes
    for p in self.tbInfo.tbProcesses:
        VhdlTitle(p, f, 2)
        f.WriteLn("p_{} : process".format(p))
        f.WriteLn("begin").IncIndent()
        if self.tbInfo.isMultiCaseTb:
            for i, c in enumerate(self.tbInfo.testCases):
                f.WriteLn("-- {}".format(c))
                f.WriteLn("wait until NextCase = {};".format(i))
                f.WriteLn("ProcessDone(TbProcNr_{}_c) <= '0';".format(p))
                args = ", ".join(port.name for port in self.tbInfo.GetPortsForProcess(p))
                f.WriteLn("work.{tb}_case_{case}.{proc}({args}, Generics_c);".format(tb=self.tbInfo.tbName, case=c, proc=p, args=args))
                f.WriteLn("wait for 1 ps;")
                f.WriteLn("ProcessDone(TbProcNr_{}_c) <= '1';".format(p))
        else:
            rsts = DutInfo.FilterForTag(self.dutInfo.ports, Tags.TYPE, "rst")
            if len(rsts) > 0:
                f.WriteLn("-- start of process !DO NOT EDIT")
                rstLogic = " and ".join([r.name + " = " + self.dutInfo.GetPortValue(r, False) for r in rsts])
                f.WriteLn("wait until {};".format(rstLogic))
            f.WriteLn()
            f.WriteLn("-- User Code")
            f.WriteLn("assert False report \"Insert your code here!\" severity note;")
            f.WriteLn()
            f.WriteLn("-- end of process !DO NOT EDIT!")
            f.WriteLn("ProcessDone(TbProcNr_{}_c) <= '1';".format(p))
        f.WriteLn("wait;")
        f.DecIndent().WriteLn("end process;")
        f.WriteLn()
    return f
```

注意第 109 行的 `rstLogic` 拼法与 4.5 中 `_TbControl` 第 128 行**完全相同**——同一段 `r.name + " = " + GetPortValue(r, False)` 用 ` and ` 连接。这是工具内的一致约定：凡是要等「复位释放」的地方，都要求所有复位同时为无效电平。

第 113 行的 `assert False report "Insert your code here!" severity note;` 是一个温和的占位符：仿真时会打印一条 note 提醒用户这里还没填代码，但**不阻断**仿真（`severity note` 不是 `error`）。

#### 4.6.4 代码实践

**目标**：把 `_Processes` 与 `_TbControl` 串起来，确认二者通过 `ProcessDone` 握手。

**步骤**：

1. 在生成的 TB 中找到 `p_Input` 与 `p_Output` 两个进程（来自 `PROCESSES=Input,Output`）。
2. 在每个进程末尾确认有 `ProcessDone(TbProcNr_Input_c) <= '1';` 与 `ProcessDone(TbProcNr_Output_c) <= '1';`。
3. 回到 `p_tb_control`，确认它 `wait until ProcessDone = AllProcessesDone_c;` 后才 `TbRunning <= false;`。
4. 把这两段联起来读：测试进程是「生产者」（置位），主控进程是「消费者」（等全 1）。

**需要观察的现象**：`p_Input` 与 `p_Output` 进程开头都先 `wait until InRst = '0' and OutRst = '0';`（复位等待），中间是 User Code 占位，末尾是 `ProcessDone` 置位。

**预期结果**：两个进程的结构完全对称，仅 `TbProcNr_*_c` 的下标不同（0 与 1）。环境不可用时标注「待本地验证」。

#### 4.6.5 小练习与答案

**练习 1**：单用例 TB 里，如果一个端口标了 `PROC=Input`，它会影响生成结果吗？

**答案**：在单用例 TB 里基本不影响——`PROC` 标签只在多用例分支（第 96-104 行）被 `GetPortsForProcess` 消费，用来拼 procedure 参数；单用例分支（第 105 行起）完全不读 `PROC`。所以 simpleTb 虽然端口标了各种 `PROC`，但生成的 `p_Input`/`p_Output` 进程体里并不出现这些端口（它们只是普通 DUT 信号）。

**练习 2**：为什么 User Code 占位用 `assert ... severity note` 而不是 `severity error`？

**答案**：`note` 级别只打印提示、不中断仿真，让用户能先把骨架跑通再逐步填代码；若用 `error`，骨架一启动就会被 assert 拦住，无法验证基础设施（时钟、复位、握手）是否正常。

---

## 5. 综合实践

把本讲六个模块串成一个完整的「**生成前预测 + 生成后核对**」任务，靶子仍是 simpleTb 的异步 FIFO。

**任务**：在**不运行**生成器的前提下，先在纸上写出以下五项预测，然后（如环境可用）运行生成并逐项核对。

1. **`_DutInstantiation`**：列出 `generic map` 里会出现的所有 generic 名（提示：用 4.2.3 的表格）。
2. **`_Clocks`**：写出 `p_clock_InClk` 与 `p_clock_OutClk` 的 `Frequency_c` 值，并算出各自的时钟半周期。
3. **`_Resets`**：写出 `p_rst_InRst` 进程体里两处 `rising_edge(...)` 的参数，以及释放行的完整 VHDL。
4. **`_TbControlSignals`**：写出 `ProcessDone` 的向量范围、`AllProcessesDone_c` 的值、两个 `TbProcNr_*_c` 常量的值。
5. **`_Processes` 与 `_TbControl` 联动**：用一段话说明从「两个测试进程跑完」到「仿真结束」之间发生的全部事件顺序。

**参考答案**（用于自检）：

1. `Width_g`、`Depth_g`、`AlmFullLevel_g`。
2. `Frequency_c` 分别为 `real(100e6)`=`1.0e8`、`real(125e6)`=`1.25e8`；半周期分别为 \(5\,\text{ns}\)、\(4\,\text{ns}\)。
3. 两处均为 `rising_edge(InClk)`；释放行 `InRst <= '0';`。
4. `ProcessDone : std_logic_vector(0 to 1)`；`AllProcessesDone_c := (others => '1')`（即 `"11"`）；`TbProcNr_Input_c := 0`、`TbProcNr_Output_c := 1`。
5. `p_Input` 与 `p_Output` 各自末尾置 `ProcessDone(0/1) <= '1'` → 当二者皆 `'1'`，`ProcessDone = "11" = AllProcessesDone_c` → `p_tb_control` 解除 `wait until` → `TbRunning <= false` → 所有 `p_clock_*` 退出 `while TbRunning loop` 并 `wait;` → 仿真无事件，结束。

如果环境可用，运行：

```bash
py TbGen.py -src example/simpleTb/psi_common_async_fifo.vhd -dst tb -clear -force
```

打开 `tb/psi_common_async_fifo_tb.vhd`，按 `DUT Instantiation` → `Testbench Control` → `Clocks` → `Resets` → `Processes` 的段落顺序逐项核对。任何与预测不符之处，回到对应模块的源码精读段查原因。若环境不可用，上述「纸面预测」本身就是一次完整的源码阅读型实践（生成部分标注「待本地验证」）。

---

## 6. 本讲小结

- **`FilterForTag` 是所有生成方法的统一筛选器**：支持「存在性筛选」与「值匹配筛选」，并对单值/列表、大小写做归一，是「筛选 → 检查 → 取值」三连招的第一步。
- **`_DutInstantiation`** 用 `FilterForTag` 把 `EXPORT=true` 与 `CONSTANT=值` 两类 generic 拼进 `generic map`，第三类（两者皆无）不进 map；端口则全部同名直连。
- **`_Clocks`** 强制每个 `TYPE=CLK` 端口带 `FREQ`，用 \( t_{\text{half}} = 0.5/f \) 算半周期，进程受 `while TbRunning` 控制。
- **`_Resets`** 强制每个 `TYPE=RST` 端口带 `CLK`，靠 `CLK` 标签把复位归属到正确时钟域，「等两个上升沿」保证复位至少被采样一次，释放值取 `GetPortValue(rst, False)`（无效）。
- **`_TbControlSignals` 与 `_TbControl` 是一对契约**：前者声明 `TbRunning`/`NextCase`/`ProcessDone`/`AllProcessesDone_c`/`TbProcNr_*_c`，后者等 `ProcessDone = AllProcessesDone_c` 后拉低 `TbRunning` 结束仿真。
- **`_Processes`** 在单用例下生成带复位等待与 `ProcessDone` 信令的进程骨架（中间留 User Code），在多用例下按 `NextCase` 调度并用 `PROC` 标签（经 `GetPortsForProcess`）收集 procedure 参数。

---

## 7. 下一步学习建议

- **进入 u5（多文件多用例 testbench）**：本讲多次提到 `_Processes` 与 `_TbControl` 的多用例分支被「留到 u5」。下一讲会讲清 `TESTCASES` 如何触发 `isMultiCaseTb`、`NextCase` 如何调度各用例、以及 `WriteTbPkg`/`WriteCasePkg` 如何生成 TB 包与 case 包。
- **动手做 u6-l3 的扩展实践**：如果你想自己加一个标签（比如让复位可配「保持时长」），现在的你已经具备了完整的链路视角——从 `Tags` 常量（[DutInfo.py:16-32](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/DutInfo.py#L16-L32)）到 `_Resets` 的生成逻辑都能改。
- **重读 u4-l2 的 `Generate`**：带着本讲对六个方法内部细节的理解，再回看 `Generate` 的调用顺序（[TbGen.py:221-260](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L221-L260)），你会对「线性誊抄器」如何把数据模型变成完整 VHDL 有更立体的认识。
