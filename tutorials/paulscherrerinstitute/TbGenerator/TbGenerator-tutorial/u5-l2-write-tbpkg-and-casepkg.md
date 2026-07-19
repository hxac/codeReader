# WriteTbPkg / WriteCasePkg 与过程方向

## 1. 本讲目标

本讲承接 u5-l1。在上一讲里，我们已经知道：一旦 VHDL 文件里出现 `$$ TESTCASES=... $$`，`TbInfo.isMultiCaseTb` 就被置为真，`Generate` 会在主 TB 之外**额外**写出两类文件——

- 一个 **TB 包**（`<tbName>_pkg.vhd`），由 `WriteTbPkg` 生成；
- 每个用例一个 **case 包**（`<tbName>_case_<case>.vhd`），由 `WriteCasePkg` 生成。

本讲学完后你应该能够：

1. 说清 `WriteTbPkg` 如何把导出的 generic 打包成 `Generics_t` 记录、把未导出的 generic 写成常量。
2. 说清 `WriteCasePkg` 如何为每个用例生成 `procedure` 的声明（header）与空实现（body）。
3. 复述 `PortDirectionForProcedure` 依据端口方向、`PROC` 标签首个值、`TYPE=CLK` 三条规则推断过程参数方向（`in` 还是 `inout`）。
4. 解释 `GetPortsForProcess` 如何用 `PROC` 端口标签把端口绑定到某个测试过程。

## 2. 前置知识

阅读本讲前，请确保你已经掌握（见 u2、u4、u5-l1）：

- **标签系统**：`$$ KEY=VAL $$` 注解标签写在 VHDL 注释里；端口级 `PROC` 标签把一个端口绑定到某个测试过程，文件级 `TESTCASES` 标签打开多用例模式。
- **`TbInfo` 模型**：`tbName = 实体名 + "_tb"`；`tbProcesses` 来自 `PROCESSES`（缺省 `["Stimuli"]`）；`isMultiCaseTb` 只看 `testcases` 键是否存在。
- **`Generate` 主流程**：单文件 TB 按固定段落顺序写出；多用例 TB 在末尾追加 `WriteTbPkg` + 循环 `WriteCasePkg`，且主 TB 里的测试进程改为按 `NextCase` 调度各 case 的 procedure。
- **VHDL 过程（procedure）**：可带 `signal` 参数的子程序。`signal ... in` 只读、`signal ... inout` 可读可写（过程内部能给它赋值）。VHDL 允许 procedure 的数组参数**不带范围约束**（unconstrained），实际范围由调用处的实参决定——本讲会用到这一点。

一句话定位：本讲拆开的是多用例 TB 里**被主 TB `use` 的那两个包文件**的生成细节，以及它们为什么需要这样一个 procedure 接口。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
|------|------|-----------|
| `MultiFileTb.py` | 多文件 TB 的专用生成器（模块级函数，非类方法） | `WriteTbPkg`、`PortDirectionForProcedure`、`WriteCasePkg` |
| `TbInfo.py` | testbench 数据模型 | `GetPortsForProcess`（按 `PROC` 选端口）、`TbPkgDeclaration`（case 包引用 TB 包） |
| `DutInfo.py` | DUT 数据模型与标签工具方法 | `FilterForTag`、`GetTagAsList`、`HastTagValue`、`Tags` 常量 |
| `TbGen.py` | `TbGenerator` 主类 | `Generate` 末尾调用 `WriteTbPkg`/`WriteCasePkg` 的分流点 |
| `example/multiCaseTb/psi_common_async_fifo.vhd` | 多用例示例 DUT | 端口 `PROC`/`TYPE` 标签、generic `EXPORT`/`CONSTANT` 标签 |

## 4. 核心概念与源码讲解

### 4.1 GetPortsForProcess：把端口绑定到过程

#### 4.1.1 概念说明

多用例 TB 里，每个测试过程（如 `Input`、`Output`）最终会被实现成 VHDL 的 `procedure`，而 procedure 需要一个**参数列表**——也就是「这个过程中要碰哪些 DUT 端口」。这个「端口 → 过程」的绑定关系，就是由端口级 `PROC` 标签声明的。

例如示例里：

```vhdl
OutRdy : in std_logic := '1'; -- $$ PROC=Output,Input $$
```

表示 `OutRdy` 同时被 `Output` 和 `Input` 两个过程使用。而：

```vhdl
InData : in std_logic_vector(Width_g-1 downto 0) := (others => '0'); -- $$ PROC=Input $$
```

表示 `InData` 只属于 `Input` 过程。

#### 4.1.2 核心流程

`TbInfo.GetPortsForProcess(process)` 的实现极简，就是把 `PROC` 标签的「存在性 + 值匹配」交给 `DutInfo.FilterForTag`：

```python
def GetPortsForProcess(self, process : str) -> List[VhdlPortDeclaration]:
    return DutInfo.FilterForTag(self.dutInfo.ports, Tags.PROC, process)
```

`FilterForTag`（详见 u2-l1、u4-l1）的语义是：遍历 `ports`，解析每个端口的 `$$ ... $$` 注释，凡是带 `PROC` 标签、且标签值（按逗号拆成列表、大小写不敏感）包含 `process` 的端口，都入选。

要点：

- `PROC` 标签的值可以是单个过程名，也可以是逗号分隔的列表（如 `Output,Input`），`FilterForTag` 内部会把字符串归一成列表再匹配。
- 没有 `PROC` 标签的端口（如 `InRst`、`OutRst`、各种 status 端口）**不会**出现在任何过程的参数列表里——它们只在主 TB 的并发语句里被使用，不进 procedure。
- 匹配大小写不敏感：示例里故意把同一个 `Input` 写成 `Input`、`INPUT`、`input` 三种大小写，都能被正确归到 `Input` 过程。

#### 4.1.3 源码精读

[`TbInfo.py:47-48`](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbInfo.py#L47-L48) 即 `GetPortsForProcess` 全部实现，说明它只是 `FilterForTag` 的一层薄封装。

被它依赖的 [`DutInfo.py:147-166`](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/DutInfo.py#L147-L166)（`FilterForTag`）做实际筛选：当 `value` 不为 `None` 时，把标签值归一成小写列表后判断 `value.lower()` 是否在列表中。

#### 4.1.4 代码实践

1. **实践目标**：手工预测 `GetPortsForProcess("Output")` 会返回哪些端口。
2. **操作步骤**：打开 `example/multiCaseTb/psi_common_async_fifo.vhd`，逐个端口看 `PROC` 标签。
3. **预期结果**（待本地验证）：应包含 `OutClk`（`Proc=Output`）、`OutData`、`OutVld`、`OutRdy`（`Output,Input`）、`OutFull`（`Input,Output`）。注意 `OutRst` 只有 `TYPE=RST; CLK=OutClk`、**没有** `PROC`，所以不在内。
4. **观察现象**：你可以用一段最小 Python 验证：
   ```python
   from DutInfo import DutInfo
   from TbInfo import TbInfo
   d = DutInfo("example/multiCaseTb/psi_common_async_fifo.vhd")
   t = TbInfo(d)
   print([p.name for p in t.GetPortsForProcess("Output")])
   ```
   输出应与你手工列出的集合一致（顺序遵循源文件端口声明顺序）。

#### 4.1.5 小练习与答案

**练习 1**：`GetPortsForProcess("Input")` 是否包含 `InRdy`？为什么？
**答案**：包含。`InRdy` 的注释是 `-- not full $$PROC=input$$`，带 `PROC=input`，大小写不敏感匹配到 `Input`。注意此处的标签写在「not full」文字之后，`_ParseTags` 用 `scanString` 扫描整行注释，仍能提取到。

**练习 2**：如果一个端口没有 `PROC` 标签，它会进入任何过程的参数列表吗？
**答案**：不会。`FilterForTag` 只保留「标签存在且命中值」的元素；无 `PROC` 标签的端口被过滤掉，只能作为主 TB 内部的普通信号被驱动/采样。

---

### 4.2 PortDirectionForProcedure：推断过程参数方向

#### 4.2.1 概念说明

`GetPortsForProcess` 只回答了「**哪些**端口进过程」，却没回答「**用什么方向**声明它们」。这看似简单——DUT 的 `in` 端口在过程里就该是 `in` 呗？其实不然。

考虑 `OutRdy`：它是 DUT 的**输入**（`in std_logic`），被 `Output` 与 `Input` 两个过程共用。在 `Output` 过程里，TB 要**驱动**它（向 DUT 表示「输出端准备好接收」），所以过程必须能给它赋值 → 需要 `signal ... inout`。而在 `Input` 过程里，TB 只想**读**它（例如判断能否继续写），不应驱动它 → `signal ... in` 即可。

也就是说：同一个端口在不同过程中可能方向不同。决定方向的不是「端口是 in 还是 out」，而是「**这个过程是不是驱动这个信号的主**」。`PortDirectionForProcedure` 就是把这条业务规则翻译成代码的函数。

#### 4.2.2 核心流程

判定规则可归纳为下表（`processName` 为当前过程名，`procsTag` 为该端口 `PROC` 标签解析出的列表）：

| 端口方向（DUT 视角） | 条件 | 过程参数方向 |
|---|---|---|
| `out` / `buffer` | 任何情况 | `in`（TB 只读 DUT 输出） |
| `in` / `inout` | `procsTag[0]` ≠ `processName`（不是首owner） | `in`（只读） |
| `in` / `inout` | `procsTag[0]` == `processName` 且 `TYPE=CLK` | `in`（时钟由时钟进程驱动） |
| `in` / `inout` | `procsTag[0]` == `processName` 且非 `TYPE=CLK` | `inout`（本过程是驱动者） |

直观理解三条特例：

1. **DUT 输出恒为 `in`**：DUT 的输出端由 DUT 驱动，过程只能读，所以无论它在 `PROC` 列表里排第几，方向都是 `in`。
2. **非首位过程只读**：`PROC` 列表里**第一个**过程名被当作「信号的主人」（driver），其余过程只能读，方向降级为 `in`。这避免了多个过程同时驱动一个信号造成的冲突。
3. **时钟恒为 `in`**：即使某时钟端口（`TYPE=CLK`）出现在某过程的 `PROC` 列表首位，它也不会被 `inout` 声明——因为时钟由专用的 `p_clock_*` 进程驱动（见 u4-l3），测试过程绝不能去动它。

#### 4.2.3 源码精读

[`MultiFileTb.py:46-56`](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/MultiFileTb.py#L46-L56) 是整个函数：

```python
def PortDirectionForProcedure(processName : str, port : VhdlPortDeclaration) -> str:
    portDir = port.direction.lower()
    if portDir in ["in", "inout"]:
        procsTag = DutInfo.GetTagAsList(port, Tags.PROC)
        if procsTag[0].lower() != processName.lower():
            return "in"
        if DutInfo.HastTagValue(port, Tags.TYPE, "clk"):
            return "in"
        return "inout"
    else:
        return "in"
```

逐行对应上表：

- `port.direction` 来自 [`VhdlParse.py:142`](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/VhdlParse.py#L142)（`VhdlPortDeclaration._Parse` 里的 `self.direction = parts.get("dir")`）。
- `DutInfo.GetTagAsList`（[`DutInfo.py:140-145`](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/DutInfo.py#L140-L145)）保证把单个字符串也包成列表，所以 `procsTag[0]` 总能取到首位过程名——前提是这个端口确实来自 `GetPortsForProcess`（即必带 `PROC` 标签），调用方保证了这一点。
- `HastTagValue(port, TYPE, "clk")`（[`DutInfo.py:114-123`](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/DutInfo.py#L114-L123)）大小写不敏感地判断 `TYPE=CLK`。

#### 4.2.4 代码实践

1. **实践目标**：把 4.2.2 的规则套到 `OutRdy`（`in std_logic`、`PROC=Output,Input`、无 `TYPE`）上，预测它在两个过程中的方向。
2. **操作步骤**：
   - 对 `processName="Output"`：`procsTag=["Output","Input"]`，首位 `"output"=="output"`，非 `TYPE=CLK` → 应得 `"inout"`。
   - 对 `processName="Input"`：首位 `"output" != "input"` → 应得 `"in"`。
3. **观察现象**：在生成的某个 case 包里找 `OutRdy`，确认 `Output` 过程里是 `signal OutRdy : inout std_logic`、`Input` 过程里是 `signal OutRdy : in std_logic`。
4. **预期结果**：与上述预测一致。本讲 4.4 节会给出它在源码里被写出的位置。无法本地运行仿真也无妨，只读生成的 `.vhd` 即可验证。

#### 4.2.5 小练习与答案

**练习 1**：`OutClk`（`in std_logic`、`PROC=Output`、`TYPE=CLK; FREQ=125e6`）在 `Output` 过程里方向是什么？
**答案**：`in`。虽然 `OutClk` 是 `Output` 过程的 `PROC` 首位（owner），但因为 `TYPE=CLK`，第三条规则把它强制成 `in`——时钟只能由 `p_clock_OutClk` 进程驱动。

**练习 2**：`OutFull`（`out std_logic`、`PROC=Input,Output`）在 `Output` 过程里方向是什么？为什么和 `OutRdy` 不同？
**答案**：`in`。`OutFull` 是 DUT 的输出端口，按第一条规则「DUT 输出恒为 `in`」，与 `PROC` 列表里排第几无关。`OutRdy` 是 DUT 的**输入**端口，才会走到 owner 判定而可能成为 `inout`。

**练习 3**：假如把 `OutRdy` 的标签改成 `$$ PROC=Input,Output $$`（顺序互换），它在两个过程中的方向会怎样变化？
**答案**：`Input` 过程成为 owner → `signal OutRdy : inout std_logic`；`Output` 过程降级为只读 → `signal OutRdy : in std_logic`。这说明 `PROC` 列表的**顺序**具有语义：第一个名字决定谁是驱动者。

---

### 4.3 WriteTbPkg：生成 TB 包与 Generics_t 记录

#### 4.3.1 概念说明

多用例 TB 把每个用例的实现拆成独立的 case 包，但这些 case 包要共享同一份「generic 配置」。为了避免在主 TB 与各 case 包之间传递一堆零散的 generic 参数，工具用一个**记录（record）类型** `Generics_t` 把所有「导出的」generic 打包成一个整体，再以单个 `constant Generics_c : Generics_t` 在过程间传递。

TB 包（`<tbName>_pkg.vhd`）就承担两件事：

1. 定义 `Generics_t` 记录类型（仅含 `EXPORT=true` 的 generic）；
2. 把**未导出**的 generic 声明成包级常量（值取 `CONSTANT` 标签或 DUT 默认值）。

回顾 generic 三分类（u2-l2、u4-l2）以理解「为什么这样切」：

| 分类 | 标签 | 在 TB 中的归宿 |
|---|---|---|
| 导出 | `EXPORT=true`（仅认字符串 `"true"`） | 进 TB 实体的 generic 子句 + `Generics_t` 记录字段 |
| 固定常量 | `CONSTANT=值` | 不进实体 generic，作内部常量，值取标签 |
| 用默认值 | 无 `EXPORT`/`CONSTANT` | 不进实体 generic，作内部常量，值取 DUT `default` |

`WriteTbPkg` 处理的是后两类（作为常量）以及第一类的**类型定义**（记录字段）。

#### 4.3.2 核心流程

`WriteTbPkg` 写出一个标准 VHDL 包的骨架，段落顺序如下：

```
版权头 CopyrightNotice
↓
库声明 LibraryDeclarations + 用户包 UserPkgDelcaration
↓
package <tbName>_pkg is
    └─ Generics Record: type Generics_t is record ... end record;   [EXPORT=true 的 generic 作字段]
    └─ Not exported Generics: constant X : T := V;                   [其余 generic 作常量]
end package;
↓
package body <tbName>_pkg is
end;   [空 body]
```

两个值得注意的实现细节：

- **空记录兜底**：VHDL 不允许空 record。若没有任何 `EXPORT=true` 的 generic，则插入一个占位字段 `Dummy : boolean;`，保证文法合法。
- **「未导出」集合用差集计算**：代码用 `set(dutInfo.generics) - set(导出的 generics)` 得到未导出集合，再逐个写常量；常量值优先取 `CONSTANT` 标签，否则取 `g.default`。

#### 4.3.3 源码精读

[`MultiFileTb.py:13-44`](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/MultiFileTb.py#L13-L44) 是 `WriteTbPkg` 全部代码。关键片段：

包名与文件名（[`MultiFileTb.py:14-15`](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/MultiFileTb.py#L14-L15)）：

```python
pkgName = tbInfo.tbName + "_pkg"
with FileWriter(path + "/" + pkgName + extension, overwrite=overwrite) as f:
```

`Generics_t` 记录（[`MultiFileTb.py:24-30`](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/MultiFileTb.py#L24-L30)）：

```python
f.WriteLn("type Generics_t is record").IncIndent()
generics = DutInfo.FilterForTag(dutInfo.generics, Tags.EXPORT, "true")
for g in generics:
    f.WriteLn("{} : {};".format(g.name, str(g.type)))
if len(generics) is 0:
    f.WriteLn("Dummy : boolean; -- required since empty records are not allowed")
f.DecIndent().WriteLn("end record;")
```

- 这里用 `str(g.type)`（**含**范围，见 [`VhdlParse.py:117-121`](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/VhdlParse.py#L117-L121)），所以 `positive`、`natural` 这类标量类型照原样写出。
- `len(generics) is 0` 是用 `is` 比较整数（依赖 CPython 小整数缓存，仅对 0 这类小整数成立）。这是一处可读性瑕疵但功能正确——读到时不必困惑。

未导出常量（[`MultiFileTb.py:32-39`](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/MultiFileTb.py#L32-L39)）：

```python
for g in set(dutInfo.generics) - set(DutInfo.FilterForTag(dutInfo.generics, Tags.EXPORT, "true")):
    if DutInfo.HasTag(g, Tags.CONSTANT):
        value = DutInfo.GetTag(g, Tags.CONSTANT)
    else:
        value = g.default
    f.WriteLn("constant {} : {} := {};".format(g.name, str(g.type), value))
```

- `CONSTANT` 标签优先于 `default`，这与 u4-l2 `_GenericConstants` 的取值口径一致，保证 TB 包与主 TB 内部常量**同源**。

#### 4.3.4 代码实践

对照示例 DUT（`example/multiCaseTb/psi_common_async_fifo.vhd`）的 generic 声明，预测 `psi_common_async_fifo_pkg.vhd` 的内容：

| generic | 标签 | 在 TB 包中的形态 |
|---|---|---|
| `Width_g : positive := 16` | `EXPORT=true` | `Generics_t` 记录字段 `Width_g : positive;` |
| `Depth_g : positive := 32` | `EXPORT=true; funky=bla` | `Generics_t` 记录字段 `Depth_g : positive;` |
| `AlmFullOn_g : boolean := false` | `EXPORT=false,funky=blubb` | 常量 `constant AlmFullOn_g : boolean := false;` |
| `AlmFullLevel_g : natural := 28` | `CONSTANT=12` | 常量 `constant AlmFullLevel_g : natural := 12;` |
| `AlmEmptyOn_g : boolean := false` | 无 | 常量 `constant AlmEmptyOn_g : boolean := false;` |
| `AlmEmptyLevel_g : natural := 4` | 无 | 常量 `constant AlmEmptyLevel_g : natural := 4;` |

**操作步骤**：运行 `example/multiCaseTb/run.bat`，打开生成的 `tb/psi_common_async_fifo_pkg.vhd`，按上表逐行核对。

**预期结果**（待本地验证）：`Generics_t` 记录里只有 `Width_g` 与 `Depth_g` 两个字段；其余四个 generic 出现在「Not exported Generics」段落作为常量，`AlmFullLevel_g` 的值为 `12`（来自 `CONSTANT`）而非源文件的 `28`。

#### 4.3.5 小练习与答案

**练习 1**：如果一个 DUT 完全没有 `EXPORT=true` 的 generic，`Generics_t` 记录会变成什么样？
**答案**：会写出一个仅含 `Dummy : boolean;` 字段的记录，并附注释 `-- required since empty records are not allowed`，因为 VHDL 语法禁止空 record。case 包的 procedure 仍会带 `constant Generics_c : Generics_t` 参数，只是这个记录实际无业务字段。

**练习 2**：`Depth_g` 的标签是 `EXPORT=true; funky=bla`，它会被算作导出吗？`funky` 标签会影响生成吗？
**答案**：会被算作导出。`FilterForTag(EXPORT, "true")` 只看 `export` 标签的值是否为 `true`，`funky` 是另一个互不相干的标签，本工具不识别它，因此被忽略、不影响任何生成结果。同理 `AlmFullOn_g` 的 `EXPORT=false,funky=blubb` 因 `export` 值为 `false` 而归入未导出。

---

### 4.4 WriteCasePkg：生成用例包的 procedure 声明与空实现

#### 4.4.1 概念说明

TB 包定义了「数据结构」，case 包则定义每个用例的「行为」——具体说，是为 `tbProcesses` 里的每个过程名生成一个 `procedure`，签名里列出该过程要访问的端口（方向由 4.2 节决定），末尾追加一个 `constant Generics_c : Generics_t` 用于读取 generic 配置。

每个 case 包生成**两份同名的 `procedure`**：

- 一份在 `package ... is`（**声明/header**）：只写签名；
- 一份在 `package body ... is`（**实现/body**）：签名后接 `is begin ... end procedure;`，body 里目前只放一条 `assert false report "..." severity warning;`，提醒用户「这里还没有填测试内容」。

这就是「空实现」的含义——工具搭好脚手架，把真正的测试激励留给用户在每个 case 包的 body 里手写。主 TB 的测试进程（`p_Input`/`p_Output`）通过 `NextCase` 调度，会调用当前用例对应的 case 包里的这些过程（见 u5-l1）。

#### 4.4.2 核心流程

case 包的写作顺序：

```
版权头 + 库声明
↓
use work.<tbName>_pkg.all;   [TbPkgDeclaration：引用 TB 包，才能用到 Generics_t]
use 用户包                    [UserPkgDelcaration]
↓
package <tbName>_case_<case> is
    for p in tbProcesses:        [对 Input, Output 各一遍]
        procedure p (
            signal <port> : <dir> <type.name>;   [GetPortsForProcess + PortDirectionForProcedure]
            ...
            constant Generics_c : Generics_t);
end package;
↓
package body <tbName>_case_<case> is
    for p in tbProcesses:
        procedure p (...) is
        begin
            assert false report "Case <CASE> Procedure <P>: No Content added yet!" severity warning;
        end procedure;
end;
```

两个关键点：

- **case 包必须 `use` TB 包**：因为 procedure 签名用到了 `Generics_t` 类型，而它定义在 TB 包里。这是 `TbPkgDeclaration`（[`TbInfo.py:57-60`](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbInfo.py#L57-L60)）的职责。
- **类型用 `s.type.name` 而非 `str(s.type)`**：注意是 `type.name`（仅类型名），**不带范围**。所以 `InData : std_logic_vector(Width_g-1 downto 0)` 在过程参数里变成 `signal InData : inout std_logic_vector`——VHDL 允许 procedure 的数组参数无约束，范围由调用处实参自动绑定。

#### 4.4.3 源码精读

[`MultiFileTb.py:59-91`](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/MultiFileTb.py#L59-L91) 是 `WriteCasePkg` 全部代码。

case 包名（[`MultiFileTb.py:60`](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/MultiFileTb.py#L60)）：`tbInfo.tbName + "_case_" + case`，所以 `Full` 用例得到 `psi_common_async_fifo_case_Full`。

引用 TB 包与用户包（[`MultiFileTb.py:64-66`](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/MultiFileTb.py#L64-L66)）：

```python
dutInfo.LibraryDeclarations(f)
tbInfo.TbPkgDeclaration(f)      # use work.<tbName>_pkg.all;
tbInfo.UserPkgDelcaration(f)
```

procedure 声明（header，[`MultiFileTb.py:70-76`](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/MultiFileTb.py#L70-L76)）：

```python
for p in tbInfo.tbProcesses:
    f.WriteLn("procedure {} (".format(p)).IncIndent()
    for s in tbInfo.GetPortsForProcess(p):
        procDir = PortDirectionForProcedure(p, s)
        f.WriteLn("signal {} : {} {};".format(s.name, procDir, s.type.name))
    f.WriteLn("constant Generics_c : Generics_t);")
    f.WriteLn().DecIndent()
```

- 外层遍历 `tbProcesses`（`["Input", "Output"]`），内层用 4.1 的 `GetPortsForProcess` 取端口、4.2 的 `PortDirectionForProcedure` 取方向。
- 末尾恒为 `constant Generics_c : Generics_t);`，闭合参数列表。

procedure 实现（body，[`MultiFileTb.py:81-90`](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/MultiFileTb.py#L81-L90)）：签名与 header 几乎一致，差别仅在末行 `... Generics_c : Generics_t) is`（多了 ` is`），随后：

```python
f.WriteLn("begin").IncIndent()
f.WriteLn("assert false report \"Case {} Procedure {}: No Content added yet!\" severity warning;".format(case.upper(), p.upper()))
f.DecIndent().WriteLn("end procedure;")
```

- `assert false ... severity warning` 是经典的「占位提示」：仿真一跑到这里就打印一条警告，提醒用户该用例该过程还没填实现。

**调用入口**：这两个函数只在多用例模式下被调用，见 [`TbGen.py:254-260`](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L254-L260)：

```python
if self.tbInfo.isMultiCaseTb:
    WriteTbPkg(tbPath, self.dutInfo, self.tbInfo, extension, overwrite)
    for case in self.tbInfo.testCases:
        WriteCasePkg(tbPath, self.dutInfo, self.tbInfo, case, extension, overwrite)
```

先用 `WriteTbPkg` 生成 1 个 TB 包，再对 `testCases`（如 `["Full", "Empty"]`）逐个 `WriteCasePkg`，共生成 `1 + N` 个包文件，加上主 TB 即 u5-l1 所说的 `2 + N` 个文件。

#### 4.4.4 代码实践（本讲核心实践任务）

> 本任务对应大纲指定的实践：解释 `OutRdy`（`PROC=Output,Input`）在某 case 包过程里为何是 `inout` 还是 `in`，并为该 case 的过程填入一行真实的测试激励。

1. **实践目标**：
   - 在生成的 case 包中**验证** `OutRdy` 在 `Output` 过程是 `inout`、在 `Input` 过程是 `in`，并解释原因；
   - 给某个 case 的 `Input` 过程填入一行真实的测试激励代码。
2. **操作步骤**：
   1. 运行 `example/multiCaseTb/run.bat`，生成 `tb/` 目录。
   2. 打开 `tb/psi_common_async_fifo_case_Full.vhd`，定位 `package` header 里的两个 `procedure`（`Input` 与 `Output`）。
   3. 在 `Output` 过程参数里找 `OutRdy`，确认是 `signal OutRdy : inout std_logic`；在 `Input` 过程参数里找 `OutRdy`，确认是 `signal OutRdy : in std_logic`。
   4. **填入激励**：滚动到 `package body` 里的 `procedure Input`，在 `assert false ...` 那行**之前**插入一行对 FIFO 写端的驱动。例如先把写握手拉满再发一拍数据：
      ```vhdl
      -- 示例代码（用户手填，非工具生成）
      InVld <= '1';
      InData <= (others => '1');
      wait until rising_edge(InClk) and InRdy = '1';
      InVld <= '0';
      ```
      注意：因为 `InVld`/`InData` 在 `Input` 过程里被声明为 `inout`（它们是 `PROC=Input` 的首位 owner 端口），过程内可以直接给它们赋值；而 `InRdy` 是 DUT 的 `out`，方向为 `in`，只能读、不能赋值，所以用它做 `wait` 条件是合法的。
3. **观察现象**：
   - 未填实现时，仿真每个用例都会打印 `Case FULL Procedure INPUT: No Content added yet!` 警告。
   - 填入上述激励后，`Full` 用例的 `Input` 过程不再只报警告，而是真正向 FIFO 写入一拍数据。
4. **预期结果**：`OutRdy` 方向符合 4.2 的预测；填入的激励能在仿真中观察到 `InVld`/`InData` 的变化（具体波形待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：`InData` 是 `in std_logic_vector(Width_g-1 downto 0)`、`PROC=Input`，它在 `Input` 过程签名里会写成什么样？为什么范围不见了？
**答案**：写成 `signal InData : inout std_logic_vector`——方向 `inout`（`Input` 是其 `PROC` 首位 owner、且非 `TYPE=CLK`），类型只有名字 `std_logic_vector`，**没有 `(Width_g-1 downto 0)`**。因为代码用的是 `s.type.name`（[`MultiFileTb.py:74`](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/MultiFileTb.py#L74)）而非 `str(s.type)`。VHDL 允许 procedure 的数组参数不带约束，实际范围由调用处的实参信号绑定。

**练习 2**：为什么 case 包顶部要 `use work.<tbName>_pkg.all;`，而主 TB 不需要（主 TB 直接定义了相关常量）？
**答案**：case 包的 procedure 签名里出现了 `Generics_t` 这个类型名，它定义在 TB 包里，所以 case 包必须 `use` TB 包才能编译通过。主 TB 里的常量由 `_GenericConstants` 直接在本文件内声明（见 u4-l2），并自己实例化 `Generics_c`，不依赖外部包定义，因此主 TB 不需要 `use` 自己的 TB 包。

**练习 3**：`testCases = ["Full", "Empty"]` 时，两个 case 包里 `Input` 过程的签名是否完全相同？为什么还要生成两份？
**答案**：签名完全相同（都由同一个 `dutInfo`/`tbInfo` 推导出来）。生成两份的原因是**实现不同**：用户会在 `case_Full` 与 `case_Empty` 的 body 里写不同的测试激励（一个测满、一个测空），主 TB 通过 `NextCase` 在运行时切换调用对应 case 包的过程体。签名相同、行为不同，正是多用例 TB 的设计意图。

---

## 5. 综合实践

把本讲四个最小模块串起来，完成一次「**给新端口打标签 → 预测 case 包输出 → 实际生成并核对**」的闭环：

1. **改标签**：在 `example/multiCaseTb/psi_common_async_fifo.vhd` 里，给 DUT 输出端口 `InLevel`（`out std_logic_vector(...)`，原本无 `PROC` 标签）加上 `-- $$ PROC=Input,Output $$`。
2. **预测**（动手前先写下来）：
   - `InLevel` 现在会出现在哪些过程的参数列表里？（用 4.1 的 `GetPortsForProcess` 思路）
   - 它在 `Input` 与 `Output` 过程里的方向分别是什么？（用 4.2 的规则表——注意它是 DUT 的 `out`）
   - 它的类型在签名里会怎么写？（注意 `type.name`）
3. **生成并核对**：运行 `run.bat`，打开任一 case 包，确认你的预测。
4. **解释差异**：把 `InLevel` 与 `OutRdy` 对比——两者 `PROC` 列表形状相似（都是 `X,Y`），但 `InLevel` 在两个过程里方向都是 `in`，而 `OutRdy` 在 owner 过程里是 `inout`。请用 4.2 的第一条规则解释这种差异的根源（提示：DUT 视角的 `in` vs `out`）。
5. **（进阶）回退**：删掉你加的标签，确认 `InLevel` 又从所有过程参数里消失，验证「无 `PROC` 标签的端口不进 procedure」。

> 注意：本实践会临时修改示例 VHDL 文件。如果你不想污染仓库，请先 `git stash` 或复制一份到别处再改；本讲义的 worker 不应改动源码，这一步由读者在本地练习时完成。

## 6. 本讲小结

- **`GetPortsForProcess`**（`TbInfo.py`）是 `FilterForTag(ports, PROC, process)` 的薄封装，按端口级 `PROC` 标签（支持逗号列表、大小写不敏感）筛选出某过程的端口集合；无 `PROC` 标签的端口不进任何过程。
- **`PortDirectionForProcedure`**（`MultiFileTb.py`）用三条规则定方向：DUT 输出恒 `in`；DUT 输入在非 owner 过程里 `in`；owner 过程里若非 `TYPE=CLK` 则 `inout`、否则 `in`。`PROC` 列表首个名字即 owner。
- **`WriteTbPkg`** 生成 TB 包：把 `EXPORT=true` 的 generic 写进 `Generics_t` 记录（空则塞 `Dummy` 占位），其余 generic 作常量（`CONSTANT` 标签优先、否则取默认值）。
- **`WriteCasePkg`** 为每个用例生成 case 包：对每个过程写 `procedure` 声明（参数 = 过程端口 + `Generics_t` 常量）与空 body（仅一条 `assert ... warning` 占位），类型用 `type.name` 不带范围。
- **依赖链**：case 包 `use` TB 包（`TbPkgDeclaration`）以获得 `Generics_t`；主 TB 在 `isMultiCaseTb` 时按 `WriteTbPkg` → 循环 `WriteCasePkg` 的顺序生成 `1 + N` 个包文件。

## 7. 下一步学习建议

- 本讲止步于「多用例 TB 的包文件结构」。若想看清主 TB 如何**调用**这些 procedure（`NextCase`/`ProcessDone` 的双向握手、`Generics_c` 如何被实例化并传入），回头精读 `TbGen.py` 里 `_Processes` 与 `_TbControl` 的多用例分支（u5-l1 已铺垫，u6 扩展实践会再次用到）。
- 进入 u6 单元：u6-l1 讲 GUI 如何复用 `TbGenerator`；u6-l2 讲 CLI 的 `-mrg` 合并文件机制（它会把多文件 TB 的若干 `.vhd` 合并成单个 `.mrg`，与本讲的多文件产物直接相关）；u6-l3 是综合扩展实践，建议你尝试新增一个端口标签并让它贯穿到 case 包的 procedure 签名，作为对本讲的检验。
- 建议继续精读的源码：`MultiFileTb.py`（已全读，仅这两个函数）、`TbInfo.py` 的 `TbCaseDeclaration`/`TbPkgDeclaration`（理解主 TB 如何反向 `use` 这些包）、以及 `DutInfo.py` 的 `FilterForTag`/`GetTagAsList`/`HastTagValue`——它们是本讲一切标签判断的底层原语。
