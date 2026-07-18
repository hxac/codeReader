# Generate 主流程与单文件 TB 骨架

## 1. 本讲目标

学完本讲后，你应该能够：

- 用一句话说清 `TbGenerator.Generate` 做了什么：它是一个**按固定顺序、自上而下**把 testbench 写成 VHDL 文件的「线性写作器」。
- 默写出 `Generate` 的完整调用链：Header → 库声明 → 实体 → 架构（常量 / 控制信号 / DUT 信号）→ `begin` → DUT 实例化 → TB 控制 → 时钟 → 复位 → 进程 → `end;`。
- 拿到任意一个由 TbGenerator 生成的 `*_tb.vhd`，能反推出每一段是由哪个 `_Xxx()` 方法产出的。
- 理解 `VhdlTitle` / `CopyrightNotice` 如何给输出打「段落标题」，以及 `FileWriter` 的 `WriteLn` / `IncIndent` / `DecIndent` / `RemoveFromLastLine` 如何用缩进与链式调用拼出格式化 VHDL。

本讲只聚焦**单文件 TB**（即未声明 `TESTCASES` 的情况）。多用例（多文件）模式在主流程里只多出几个分支，其细节留到 u5 展开；时钟、复位、进程的内部实现细节留到 u4-l3。

## 2. 前置知识

本讲承接 u4-l1 建立的「数据流主线」：

> VHDL → `VhdlFile` → `DutInfo` → `TbInfo` → **生成器（本讲）**

在进入本讲前，请确认你已经理解以下概念（来自前置讲义）：

- **`DutInfo`** 封装了实体名 `name`、按库归类的 `libraries`、文件级标签 `fileScopeTags`，并通过 `generics` / `ports` 两个 `@property` 转发到 `VhdlFile` 的解析结果。
- **`TbInfo`** 吃一个 `DutInfo`，翻译出 `tbName`（实体名 + `_tb`）、`tbProcesses`（缺省 `["Stimuli"]`）、`isMultiCaseTb`（仅判 `TESTCASES` 键是否存在）。
- **`dutLibrary`** 是 `DUTLIB` 标签的带默认值 `"work"` 的视图。
- **`GetPortValue(port, active)`** 是端口初值的单一真相源，由 `LOWACTIVE` 标签决定极性。
- **`FileWriter`** 来自外部包 `PsiPyUtils`（不在本仓库内），负责真正落盘与缩进管理。

如果你对上面任何一项感到陌生，建议先回顾 u4-l1、u2-l2 与 u1-l2。

一个贯穿全讲的关键直觉：**`Generate` 不做任何「计算决策」，它只做「按顺序誊抄」**。所有判断（端口是不是时钟、generic 要不要导出）都已经在 `DutInfo` / `TbInfo` / 标签系统里做好了，`Generate` 只是把结论一段一段写进文件。理解了这一点，整段主流程就不再神秘。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `TbGen.py` | 引擎核心，定义 `TbGenerator` 类 | `Generate` 主流程与 `_Header` / `_EntityDeclaration` / `_GenericConstants` / `_TbControlSignals` / `_DutSignals` 等写作方法 |
| `UtilFunc.py` | 输出格式化小工具 | `VhdlTitle`（段落标题）、`CopyrightNotice`（版权头） |
| `DutInfo.py` | DUT 数据模型（u4-l1 详讲） | 本讲只用到 `LibraryDeclarations` 这一个写作方法 |
| `TbInfo.py` | TB 数据模型（u4-l1 详讲） | 本讲只用到 `UserPkgDelcaration` 等包声明方法 |
| `example/simpleTb/psi_common_async_fifo.vhd` | 示例 DUT（异步 FIFO） | 用来预测与对照生成结果 |
| `example/simpleTb/run.bat` | 示例运行脚本 | 一行 CLI 封装：`py ..\..\TbGen.py -src ... -dst .\tb -clear -force` |

> 说明：`FileWriter` 来自外部依赖 `PsiPyUtils`（本仓库未 vendored），其精确内部实现不在本讲范围内。我们只从它在 `TbGen.py` 中的**链式调用方式**推断其「对外契约」（写一行、增减缩进、回改上一行），这对理解 `Generate` 完全够用。

## 4. 核心概念与源码讲解

### 4.1 Generate：生成主流程的总调度与 FileWriter 缩进机制

#### 4.1.1 概念说明

`Generate` 是 `TbGenerator` 对外的第二个公开方法（第一个是 `ReadHdl`，见 u1-l3）。它的职责非常窄：

> 打开一个输出文件，然后**按 testbench 应有的物理顺序**，把各段内容自上而下写进去，最后关闭文件。

你可以把它想象成一个「填表格」的过程：testbench 的 VHDL 骨架是一张固定的表格，每一格由一个 `_Xxx()` 私有方法负责填写。`Generate` 本身只决定**填写的先后顺序**，不决定**填写的内容**——内容由数据模型（`DutInfo` / `TbInfo`）和标签决定。

这里有一个贯穿全类的设计模式：**所有 `_Xxx()` 方法都接收并返回同一个 `FileWriter` 对象 `f`**，因此可以在 `Generate` 里写成链式调用：

```python
self._Header(f).WriteLn()
```

每个 `_Xxx()` 往 `f` 里追加若干行后把 `f` 返回，下一个方法接着写。`f` 内部维护着一个「当前缩进级别」，所以方法之间不需要互相传递缩进状态——这正是 `FileWriter` 的核心价值。

#### 4.1.2 核心流程

`Generate` 的执行流程可以用下面的伪代码概括（单文件路径，即 `isMultiCaseTb == False`）：

```
Generate(tbPath, extension, overwrite):
    1. 校验：dutInfo 不能为 None（否则抛 "call ReadHdl() first!"）
    2. 若 tbPath 目录不存在，则 os.mkdir 创建
    3. with FileWriter(打开 "{tbPath}/{tbName}{extension}", overwrite) as f:
         a. _Header(f)                         # 版权 + 生成说明
         b. dutInfo.LibraryDeclarations(f)     # library / use 语句
         c. tbInfo.UserPkgDelcaration(f)       # 用户额外包（TBPKG）
            [多用例才走] TbPkgDeclaration / TbCaseDeclaration
         d. _EntityDeclaration(f)              # entity ... end entity
         e. VhdlTitle("Architecture")          # 架构标题
            "architecture sim of <tbName> is"
         f. _GenericConstants(f)               # generic 三分类常量
         g. _TbControlSignals(f)               # TbRunning / ProcessDone 等
         h. _DutSignals(f)                     # 每个 port 一个 signal
            DecIndent
            "begin"
         i. _DutInstantiation(f)               # i_dut : entity ...
         j. _TbControl(f)                      # p_tb_control 进程
         k. _Clocks(f)                         # p_clock_* 进程
         l. _Resets(f)                         # p_rst_* 进程
         m. _Processes(f)                      # p_<name> 测试进程
            DecIndent
            "end;"
    4. [多用例才走] 额外生成 TB 包与每个 case 包（本讲不展开）
```

注意步骤 3 的子顺序与一份手写 testbench 的物理布局**完全一致**：先是文件头与库声明，再是实体声明，然后进入架构的声明区（常量/信号），`begin` 之后是并发语句（实例化与各进程），最后 `end;` 收尾。这不是巧合——`Generate` 的写作顺序就是 VHDL 的语法顺序。

> 关于缩进：步骤 f–h 处于 `architecture ... is` 之后的「声明区」，所以先 `IncIndent` 再写；步骤 i–m 处于 `begin` 之后的「并发语句区」，缩进级别相同。`_DutInstantiation` 等方法内部还会**临时**增减缩进来排版 `port map (...)` 这类嵌套结构，写完后恢复原级别——这就是 `FileWriter` 让方法「自管理缩进」的体现。

#### 4.1.3 源码精读

先看 `Generate` 的方法签名与开头的两道防线：

[TbGen.py:221-228](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L221-L228) —— `Generate` 先校验「必须先读后写」，再确保目标目录存在，然后用 `with` 语句打开 `FileWriter`。文件名为 `{tbPath}/{tbName}{extension}`（例如 `tb/psi_common_async_fifo_tb.vhd`），`overwrite` 参数直接透传给 `FileWriter`。

[TbGen.py:229-236](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L229-L236) —— 「库声明」三连：先写头（`_Header`），再让 `DutInfo.LibraryDeclarations` 写 `library` / `use`，最后让 `TbInfo.UserPkgDelcaration` 写用户通过 `TBPKG` 注入的额外包；多用例模式才会额外走 `TbPkgDeclaration` / `TbCaseDeclaration`（本讲示例不触发）。

[TbGen.py:237-253](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L237-L253) —— 这是本讲的「核心段」。请逐行对照 4.1.2 的伪代码阅读：第 238 行写实体声明；第 241–242 行写架构标题与 `architecture sim of <tbName> is` 并 `IncIndent` 进入声明区；第 243–245 行依次写常量、控制信号、DUT 信号；第 246 行 `DecIndent`、第 247 行写 `begin` 并再次 `IncIndent` 进入并发语句区；第 248–252 行依次写实例化、TB 控制、时钟、复位、进程；第 253 行 `DecIndent` 后写 `end;` 收尾。**整个单文件 TB 的骨架就在这 16 行里调度完毕。**

[TbGen.py:254-260](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L254-L260) —— `with` 块结束后，仅当 `isMultiCaseTb` 为真才额外生成 TB 包与每个 case 包（调用 `WriteTbPkg` / `WriteCasePkg`，见 u5）。单文件模式直接跳过。

把上面三段拼起来，就能得到一张「段落 ↔ 方法」对照表（本讲的实践任务就是填这张表）：

| 生成的 VHDL 段落 | 由谁产出 | 源码行 |
| --- | --- | --- |
| 版权头 + 生成说明 | `_Header` | TbGen.py:215-219 |
| `library` / `use` 语句 | `DutInfo.LibraryDeclarations` | DutInfo.py:82-90 |
| 用户包（`TBPKG`） | `TbInfo.UserPkgDelcaration` | TbInfo.py:50-55 |
| `entity ... end entity` | `_EntityDeclaration` | TbGen.py:194-213 |
| `architecture sim of ... is` | `Generate` 直写 | TbGen.py:241-242 |
| Fixed / Not Assigned Generics | `_GenericConstants` | TbGen.py:143-164 |
| TB 控制信号（`TbRunning` 等） | `_TbControlSignals` | TbGen.py:166-174 |
| DUT 信号（每端口一个 `signal`） | `_DutSignals` | TbGen.py:176-190 |
| `i_dut : entity ...` | `_DutInstantiation` | TbGen.py:33-49 |
| `p_tb_control` 进程 | `_TbControl` | TbGen.py:122-141 |
| `p_clock_*` 进程 | `_Clocks` | TbGen.py:51-66 |
| `p_rst_*` 进程 | `_Resets` | TbGen.py:68-84 |
| `p_<name>` 测试进程 | `_Processes` | TbGen.py:86-120 |

> 提示：表格里后七行（实例化及之后）的内部实现细节是 u4-l3 的主题，本讲只需记住「它们各自负责并发语句区的一段」即可。

#### 4.1.4 代码实践

**实践目标**：不运行代码，纯靠阅读 `Generate`，预测 `simpleTb` 示例生成的 `*_tb.vhd` 的「段落顺序」。

**操作步骤**：

1. 打开 [TbGen.py:221-260](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L221-L260)，把 `with FileWriter(...)` 块里每一行 `self._Xxx(f)` 抄下来。
2. 在纸上按顺序写下你预期会看到的「段落标题」（`VhdlTitle` 会把这些标题写进文件，见 4.2）。
3. 对照上面的「段落 ↔ 方法」对照表，给每个标题标注是哪个方法写的。

**需要观察的现象**：你会得到一串形如 `Libraries → Entity Declaration → Architecture → Fixed Generics → Not Assigned Generics (default values) → TB Control → DUT Signals → DUT Instantiation → Testbench Control !DO NOT EDIT! → Clocks !DO NOT EDIT! → Resets → Processes` 的序列。

**预期结果**：这个序列与你之后真正运行生成的 `tb/psi_common_async_fifo_tb.vhd` 里自上而下出现的注释标题**一一对应**。如果将来你修改了 `Generate` 里某行的顺序，生成的文件里对应段落也会跟着搬家——这就是「线性写作器」的可预测性。

> 若你想真正跑一遍：在 `example/simpleTb/` 下执行 `run.bat`（或 `python ../../TbGen.py -src ./psi_common_async_fifo.vhd -dst ./tb -clear -force`），需要本机已安装 `PsiPyUtils`、`pyparsing`。若环境不具备，本实践作为「源码阅读型实践」同样成立。

#### 4.1.5 小练习与答案

**练习 1**：如果把 [TbGen.py:248](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L248) 的 `self._DutInstantiation(f)` 整行删掉，生成的 TB 还能编译吗？为什么？

> **答案**：不能正常编译。`_DutInstantiation` 负责把 DUT 实例化（`i_dut : entity ...`）。删掉后，架构里声明了一堆 DUT 信号却没有任何东西驱动/消费它们（信号会被综合/仿真器视为悬空），而且测试进程里对这些信号的赋值也会失去意义。更重要的是，`_DutInstantiation` 是 `begin` 之后的第一条并发语句，它缺失会让整个 TB 失去被测对象。

**练习 2**：`Generate` 为什么要先 `IncIndent`（第 242 行）再写 `_GenericConstants` / `_TbControlSignals` / `_DutSignals`，然后在第 246 行 `DecIndent`？

> **答案**：因为这三段是 `architecture ... is` 与 `begin` 之间的**声明区**，VHDL 语法要求它们缩进在架构名之下；`IncIndent` 让随后 `WriteLn` 的每一行都自动带上这一级缩进。写完声明区、准备进入 `begin` 之后的并发语句区前，先 `DecIndent` 回到架构顶层，再写 `begin`，保证 `begin` 与 `architecture` 对齐、并发语句又重新缩进一级。

---

### 4.2 _Header 与标题/版权工具（VhdlTitle / CopyrightNotice）

#### 4.2.1 概念说明

testbench 文件最顶部通常是版权声明与一段「这是什么文件」的说明。`_Header` 就是写这段头部的方法，而它本身几乎不包含逻辑——它只是把两个格式化工具函数 `CopyrightNotice` 和 `VhdlTitle` 串起来调用。

`VhdlTitle` 与 `CopyrightNotice` 都定义在 `UtilFunc.py`，是本仓库里**唯二**的输出格式化工具。它们的共同特点是：接收一个 `FileWriter`，往里写若干行 VHDL 注释，然后把同一个 `FileWriter` 返回（从而支持链式调用）。理解了这两个函数，你就理解了生成文件里所有「标题横线」和「`-- *** xxx ***`」是怎么来的。

#### 4.2.2 核心流程

```
_Header(f):
    CopyrightNotice(f)                      # 三行横线 + 版权 + 空行
    VhdlTitle("Testbench generated by TbGen.py", f)   # level=1 标题
    f.WriteLn("-- see Library/Python/TbGenerator")    # 指向工具出处
```

`VhdlTitle` 有两个级别：

- **level=1**（默认）：三行结构——一行 60 个 `-`、一行 `-- 标题文本`、再一行 60 个 `-`。用于大段落（Libraries、Entity Declaration、Architecture、DUT Instantiation、Clocks、Resets、Processes、Testbench Control）。
- **level=2**：单行 `-- *** 标题 ***`。用于大段落内部的子段（Fixed Generics、TB Control、DUT Signals 等）。

`CopyrightNotice` 则固定写：一行 `-`×60、一行当年的版权、一行 `-- All rights reserved.`、一行 `-`×60、一个空行。其中「当年」由 `datetime.now().year` 动态生成。

#### 4.2.3 源码精读

[TbGen.py:215-219](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L215-L219) —— `_Header` 的全部实现：先调 `CopyrightNotice(f)`，再用 `VhdlTitle` 写一行「Testbench generated by TbGen.py」，最后补一句指向工具位置的注释 `-- see Library/Python/TbGenerator`。

[UtilFunc.py:10-19](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/UtilFunc.py#L10-L19) —— `VhdlTitle` 的实现：`level is 1` 时写「横线—标题—横线」三行；`level is 2` 时写单行 `-- *** title ***`；其它级别直接抛 `Exception("Illegel VHDL Title level")`（注意源码原拼写为 "Illegel"）。结尾 `return f` 保证可链式调用。

[UtilFunc.py:21-27](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/UtilFunc.py#L21-L27) —— `CopyrightNotice` 的实现：用 `dt.now().year`（`datetime` 在文件头被 `from datetime import datetime as dt` 引入）填入版权年份，固定四行加一个空行。

> 代码细节：`VhdlTitle` 里用的是 `if level is 1:` / `elif level is 2:`，对整数使用 `is` 比较。这在 CPython 中对小整数可行（整数缓存），但严格说应使用 `==`。这是一个可改进点，但不会影响当前生成结果。

#### 4.2.4 代码实践

**实践目标**：亲手验证 `VhdlTitle` 两个级别的输出差异。

**操作步骤**：

1. 在仓库根目录启动 Python（需已安装 `PsiPyUtils`）。
2. 执行下面的「示例代码」，把输出重定向到一个临时文件再查看：

```python
# 示例代码（非项目原有）
from PsiPyUtils import FileWriter
from UtilFunc import VhdlTitle, CopyrightNotice

with FileWriter("/tmp/title_demo.vhd", overwrite=True) as f:
    CopyrightNotice(f)
    VhdlTitle("A Big Section", f)          # level=1
    VhdlTitle("A Small Subsection", f, 2)  # level=2
    f.WriteLn("-- body line")
```

**需要观察的现象**：level=1 的标题被两行 60 个 `-` 夹住；level=2 的标题是单行 `-- *** ... ***`；版权年份是当前年份。

**预期结果**：`/tmp/title_demo.vhd` 内容大致如下（年份随当前时间变化）：

```vhdl
------------------------------------------------------------
-- Copyright (c) 2026 by Paul Scherrer Institute, Switzerland
-- All rights reserved.
------------------------------------------------------------

------------------------------------------------------------
-- A Big Section
------------------------------------------------------------
-- *** A Small Subsection ***
-- body line
```

> 若本机没有 `PsiPyUtils`，可改为纯阅读型实践：在生成的 `*_tb.vhd` 里数一数 level=1 与 level=2 标题各出现几次，并与 `TbGen.py` 中各 `_Xxx()` 调用 `VhdlTitle` 的级别参数对照。

#### 4.2.5 小练习与答案

**练习 1**：生成的 TB 里 `-- *** Fixed Generics ***` 是 level 几？由哪个方法写？

> **答案**：level=2（单行 `-- *** ... ***` 形式），由 `_GenericConstants` 在 [TbGen.py:146](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L146) 调用 `VhdlTitle("Fixed Generics", f, 2)` 写出。

**练习 2**：为什么 `Clocks` 段的标题是 `Clocks !DO NOT EDIT!`（[TbGen.py:52](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L52)），而 `Resets` 段只是 `Resets`？

> **答案**：这是作者用标题文本来提醒用户「哪些段可以手动改、哪些不能」。`_Clocks` 生成的是由 `FREQ` 标签精确计算半周期的时钟进程，手动改动会破坏与标签的契约，所以标 `!DO NOT EDIT!`；`_Resets` 的释放时序相对宽松（见 u4-l3），允许用户在此基础上微调，因此没有加警告。`_Processes` 在多用例模式下也会带 `!DO NOT EDIT!`，单用例则不带。

---

### 4.3 _EntityDeclaration：实体声明

#### 4.3.1 概念说明

testbench 本身也是一个 VHDL entity（顶层模块）。`_EntityDeclaration` 负责写出这个 TB 实体的声明：`entity <tbName> is ... end entity;`。

它的关键逻辑只有一个：**只有被标记 `EXPORT=true` 的 generic 才会出现在 TB 实体的 `generic (...)` 子句里**，从而允许仿真工具或上层在实例化 TB 时从外部配置这些参数。其它 generic（`CONSTANT` 或未指定）不会出现在实体声明里——它们会在架构内部被定义成常量（见 4.4）。

#### 4.3.2 核心流程

```
_EntityDeclaration(f):
    VhdlTitle("Entity Declaration")                 # level=1
    f.WriteLn("entity <tbName> is"); IncIndent
    eg = FilterForTag(generics, EXPORT, "true")     # 只挑导出型 generic
    if eg 非空:
        WriteLn("generic ("); IncIndent
        for g in eg:
            line = "<name> : <type>"
            if g.default is not None: line += " := <default>;"
            else:                       line += ";"
            WriteLn(line)
        RemoveFromLastLine(1)                        # 去掉最后一个多余分号? 见下
        DecIndent; WriteLn(");")
    DecIndent
    WriteLn("end entity;"); WriteLn()                # 空行收尾
```

#### 4.3.3 源码精读

[TbGen.py:194-213](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L194-L213) —— `_EntityDeclaration` 全文。注意三点：

1. 第 198 行用 `FilterForTag(..., Tags.EXPORT, "true")` 挑出导出型 generic。回顾 u2-l2，`EXPORT` 只认字符串 `"true"`，所以 `AlmFullOn_g`（标了 `EXPORT=false`）**不会**进实体声明。
2. 第 202-208 行拼接每个 generic 行：格式为 `name : type`，若该 generic 在原 DUT 里有默认值则追加 ` := default;`，否则只加 `;`。
3. 第 209 行 `RemoveFromLastLine(1)` 回改最后一行——每个 generic 行都以 `;` 结尾，但 VHDL `generic (...)` 子句里最后一个 generic 不应有尾分号（分号改到闭括号前）。这里删掉最后一个 `;`，再在第 210 行 `DecIndent().WriteLn(");")` 写出闭括号（VHDL-2008 风格 `);` 单独成行）。

以 `simpleTb` 为例，导出型 generic 是 `Width_g`、`Depth_g`，所以生成的实体声明为：

```vhdl
entity psi_common_async_fifo_tb is
	generic (
		Width_g : positive := 16;
		Depth_g : positive := 32
	);
end entity;
```

（`AlmFullOn_g` 因 `EXPORT=false` 被排除在外。）

> 关于 `RemoveFromLastLine`：它是 `FileWriter`（外部 `PsiPyUtils`）提供的方法，作用是**回退修改已经写入的最后一行**——从末尾删去 N 个字符，可选地追加一段文本。`_EntityDeclaration` 这里用它去掉尾分号，`_DutInstantiation` / `_GenericConstants` 里用它去掉尾逗号。其精确参数语义（`keepNewline` 等）是 `PsiPyUtils` 的实现细节，本讲只需记住「它能回改上一行」这一对外效果。

#### 4.3.4 代码实践

**实践目标**：观察 `EXPORT` 标签如何改变实体声明。

**操作步骤**：

1. 复制 `example/simpleTb/psi_common_async_fifo.vhd` 到一个临时目录。
2. 找到 `AlmFullOn_g : boolean := false;-- $$ EXPORT=false,funky=blubb $$`，把 `EXPORT=false` 改成 `EXPORT=true`。
3. 重新运行生成（`python TbGen.py -src ./...vhd -dst ./tb -clear -force`）。
4. 打开新的 `*_tb.vhd`，定位 `Entity Declaration` 段。

**需要观察的现象**：`generic (...)` 子句里多出一行 `AlmFullOn_g : boolean := false`。

**预期结果**：因为现在 `AlmFullOn_g` 通过了 `FilterForTag(..., EXPORT, "true")` 的筛选，它进入了 `eg` 列表，从而被写进实体声明。反之，若把 `Width_g` 的 `EXPORT=true` 删掉，它就会从实体声明里消失（转而变成架构内的常量，见 4.4）。> 若无法运行，可改为阅读型实践：直接对照 [TbGen.py:198](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L198) 解释「为什么 `EXPORT=false` 的 generic 不出现在实体里」。

#### 4.3.5 小练习与答案

**练习 1**：如果 DUT 里所有 generic 都没有 `EXPORT=true` 标签，`_EntityDeclaration` 生成的实体会是什么样？

> **答案**：`eg` 为空，`if len(eg) > 0:` 不成立，整个 `generic (...)` 子句被跳过。实体退化为最简形式：`entity <tbName> is` 紧跟 `end entity;`（中间只有缩进控制，无 generic 子句）。

**练习 2**：`RemoveFromLastLine(1)` 在这里删掉的是哪个字符？如果不删会怎样？

> **答案**：删掉最后一个 generic 行末尾的 `;`（一个字符）。如果不删，`generic (...)` 子句会变成 `Depth_g : positive := 32;` 后面紧跟 `);`，即 `... := 32;;`——多出一个分号，这在 VHDL 里是语法错误。

---

### 4.4 _GenericConstants：generic 三分类常量

#### 4.4.1 概念说明

架构声明区里，每个 generic 都要有一个对应的「可见对象」供 TB 内部使用。`_GenericConstants` 根据 generic 上的标签把它们分成三类，分别用不同方式处理（回顾 u2-l2 的 generic 三分类）：

1. **`CONSTANT=值`**：用户在标签里**固定**了该 generic 的值。生成 `constant <name> : <type> := <标签值>;`，值取自标签而非 DUT 默认值。
2. **未指定（既无 `EXPORT=true` 也无 `CONSTANT`）**：使用 DUT 原始默认值，生成 `constant <name> : <type> := <DUT默认值>;`，作为 TB 内部常量。
3. **`EXPORT=true`**：这些 generic 已经在实体声明里对外暴露（见 4.3），**不**在架构里重复定义常量；但在多用例模式下，会把它们汇总成一个 `Generics_c : Generics_t` 记录常量，方便传递给各 case 包。

#### 4.4.2 核心流程

```
_GenericConstants(f):
    gConst = FilterForTag(generics, CONSTANT)              # 第 1 类
    gExp   = FilterForTag(generics, EXPORT, "true")        # 第 3 类
    VhdlTitle("Fixed Generics", level=2)
    for g in gConst:                                        # 用标签值
        WriteLn("constant <name> : <type> := <CONSTANT标签值>;")
    VhdlTitle("Not Assigned Generics (default values)", level=2)
    for g in generics:                                      # 第 2 类：有默认值且不在上两类
        if g.default is not None and g not in gConst and g not in gExp:
            WriteLn("constant <name> : <type> := <DUT默认值>;")
    if isMultiCaseTb:                                       # 本讲示例不触发
        VhdlTitle("Exported Generics", level=2)
        WriteLn("constant Generics_c : Generics_t := ("); IncIndent
        for g in gExp: WriteLn("<name> => <name>,")
        RemoveFromLastLine(1, keepNewline=True, append=");")
        DecIndent
```

#### 4.4.3 源码精读

[TbGen.py:143-164](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L143-L164) —— `_GenericConstants` 全文。要点：

- 第 144-145 行先把两类 generic 各自筛出来：`gConst`（有 `CONSTANT` 标签）和 `gExp`（`EXPORT=true`）。注意 `FilterForTag` 不传 `value` 时只判断「标签是否存在」，所以 `CONSTANT=12` 会被选中。
- 第 147-148 行写「Fixed Generics」：值用 `DutInfo.GetTag(g, Tags.CONSTANT)`，即标签里写的字面量（如 `12`），而**不是** DUT 默认值（`28`）。这正是 u2-l2 强调的「`CONSTANT` 用标签值」。
- 第 150-153 行写「Not Assigned Generics (default values)」：遍历**所有** generic，但只输出「有默认值、且既不是 `CONSTANT` 也不是 `EXPORT=true`」的那些，值用 `g.default`。
- 第 154-163 行是**多用例专属**的「Exported Generics」聚合记录，把所有导出 generic 汇总成 `Generics_c : Generics_t := (...)`。本讲示例 `isMultiCaseTb == False`，整段跳过。其中第 160-161 行有个边界处理：若 `gExp` 为空，写入一个占位字段 `Dummy => true`，保证记录非空（VHDL 空记录非法）。第 162 行 `RemoveFromLastLine(1, keepNewline=True, append=");")` 同时删掉尾逗号并追加分号与闭括号。

以 `simpleTb` 为例（`Width_g`/`Depth_g` 是 `EXPORT=true`，`AlmFullLevel_g` 是 `CONSTANT=12`，其余无标签）：

```vhdl
	-- *** Fixed Generics ***
	constant AlmFullLevel_g : natural := 12;

	-- *** Not Assigned Generics (default values) ***
	constant AlmFullOn_g : boolean := false;
	constant AlmEmptyOn_g : boolean := false;
	constant AlmEmptyLevel_g : natural := 4;
```

注意 `Width_g` / `Depth_g` 在这里**不出现**——它们是导出型，已经在实体声明的 `generic (...)` 里了；而 `AlmFullLevel_g` 用标签值 `12` 而非 DUT 默认值 `28`。

#### 4.4.4 代码实践

**实践目标**：验证 `CONSTANT` 标签值优先于 DUT 默认值。

**操作步骤**：

1. 在 `simpleTb` 的 VHDL 里，`AlmFullLevel_g` 同时有 DUT 默认值 `:= 28` 和标签 `$$CONSTANT=12$$`。
2. 运行生成后，打开 `*_tb.vhd`，定位 `-- *** Fixed Generics ***`。
3. 把标签改成 `$$CONSTANT=50$$`，重新生成，再观察同一行。

**需要观察的现象**：常量值随标签变化（`12` → `50`），与 DUT 默认值 `28` 无关。

**预期结果**：生成 `constant AlmFullLevel_g : natural := 50;`。这证明 `_GenericConstants` 对 `CONSTANT` 类 generic 取的是 `GetTag(g, Tags.CONSTANT)` 的返回值，而非 `g.default`。

> 阅读型替代实践：直接对照 [TbGen.py:148](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L148) 与 [TbGen.py:153](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L153)，解释「为什么两类 generic 的取值来源不同」。

#### 4.4.5 小练习与答案

**练习 1**：一个 generic 既没有 `CONSTANT` 也没有 `EXPORT=true`，且在 DUT 里没有默认值（`default is None`），它会出现在 `_GenericConstants` 的输出里吗？

> **答案**：不会。第 152 行的条件 `if (g.default is not None) and (g not in gConst) and (g not in gExp)` 第一个子句就排除了它。这样的 generic 在架构里既不是常量也不在实体声明里，相当于在 TB 内部「不可见」——这是一个潜在的坑，添加新 generic 时需留意。

**练习 2**：为什么多用例模式下 `gExp` 为空时要写一个 `Dummy => true` 字段？

> **答案**：VHDL 记录类型（record）不能是空的。当所有导出 generic 都不存在时，`Generics_t` 聚合如果没有字段会触发语法错误。`Dummy => true` 是一个占位字段，让记录始终至少有一个成员，从而保证生成代码合法。

---

### 4.5 _TbControlSignals：TB 控制信号

#### 4.5.1 概念说明

每个 testbench 都需要一组「基础设施信号」来控制仿真的启停与进程间同步。`_TbControlSignals` 负责声明这些信号与常量。它们与具体 DUT 无关，是 TbGenerator 生成的所有 TB 共有的「脚手架」。

四类控制对象：

- **`TbRunning`**（boolean，初值 `True`）：仿真运行标志。时钟进程在 `while TbRunning loop` 里翻转时钟（见 [TbGen.py:59](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L59)）；一旦被置 `false`，时钟停振、仿真收尾。
- **`NextCase`**（integer，初值 `-1`）：多用例模式下，`p_tb_control` 用递增的 `NextCase` 调度各用例（见 u5）。单用例模式下它仍被声明但基本闲置。
- **`ProcessDone`**（`std_logic_vector`，初值全 `'0'`）：每个测试进程对应一比特，进程完成本职后把自己的比特置 `'1'`。
- **`AllProcessesDone_c`**（常量，全 `'1'`）与 **`TbProcNr_<name>_c`**（每个进程一个整数常量，给出它在 `ProcessDone` 向量里的下标）。

#### 4.5.2 核心流程

```
_TbControlSignals(f):
    VhdlTitle("TB Control", level=2)
    WriteLn("signal TbRunning : boolean := True;")
    WriteLn("signal NextCase : integer := -1;")
    WriteLn("signal ProcessDone : std_logic_vector(0 to N-1) := (others => '0');")
    WriteLn("constant AllProcessesDone_c : std_logic_vector(0 to N-1) := (others => '1');")
    for i, p in enumerate(tbProcesses):
        WriteLn("constant TbProcNr_<p>_c : integer := <i>;")
```

其中 \( N \) 是测试进程的数量，即 `len(self.tbInfo.tbProcesses)`。向量宽度为 \( 0 \text{ to } N-1 \)，所以位宽是 \( N \)。

#### 4.5.3 源码精读

[TbGen.py:166-174](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L166-L174) —— `_TbControlSignals` 全文。注意向量宽度由 `len(self.tbInfo.tbProcesses)-1` 决定：

- 第 170 行 `ProcessDone` 的上界是 `len(...)-1`，所以 `simpleTb`（`tbProcesses = ["Input", "Output"]`，长度 2）生成 `std_logic_vector(0 to 1)`，位宽 2。
- 第 171 行 `AllProcessesDone_c` 用同样的上界，初值 `(others => '1')`，作为「所有进程都完成」的比较基准。
- 第 172-173 行用 `enumerate` 给每个进程分配下标：`TbProcNr_Input_c : integer := 0;`、`TbProcNr_Output_c : integer := 1;`。这些常量在 `_Processes` / `_TbControl` 里被用来索引 `ProcessDone` 的对应比特。

> 设计要点：`AllProcessesDone_c` 与 `ProcessDone` 宽度相同，所以 `ProcessDone = AllProcessesDone_c`（见 [TbGen.py:136](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L136)）只有当**所有**进程都把自己的比特置 1 时才为真——这就是 u1-l3 提到的「全部进程完成才结束仿真」机制的信号层基础。

#### 4.5.4 代码实践

**实践目标**：观察进程数量如何影响控制信号的宽度。

**操作步骤**：

1. 在 `simpleTb` 的 VHDL 里，文件级标签是 `-- $$ PROCESSES=Input,Output $$`（2 个进程）。
2. 先运行一次，记录 `ProcessDone` / `AllProcessesDone_c` 的向量范围与 `TbProcNr_*_c` 的个数。
3. 把标签改成 `-- $$ PROCESSES=Stimuli $$`（1 个进程，也是缺省值），重新生成，再对比。

**需要观察的现象**：进程数从 2 变 1 时，向量范围从 `0 to 1` 变成 `0 to 0`，`TbProcNr_*_c` 常量也只剩一个。

**预期结果**：第一次生成 `std_logic_vector(0 to 1)` 与两个 `TbProcNr_` 常量；第二次生成 `std_logic_vector(0 to 0)` 与一个 `TbProcNr_Stimuli_c : integer := 0;`。这印证了控制信号宽度完全由 `tbProcesses` 长度决定。

#### 4.5.5 小练习与答案

**练习 1**：`NextCase` 在单用例 TB 里有用吗？为什么仍然声明它？

> **答案**：单用例模式下 `NextCase` 基本闲置（`p_tb_control` 不写它，测试进程也不读它，见 [TbGen.py:135-136](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L135-L136)）。仍然声明它是因为 `_TbControlSignals` 不区分单/多用例——它总是输出同一套脚手架信号。这是一种「以少量冗余换取代码统一」的取舍。

**练习 2**：`ProcessDone` 的位宽公式是 `len(tbProcesses)`，但源码里写的是 `len(tbProcesses)-1` 作为上界。两者矛盾吗？

> **答案**：不矛盾。VHDL `std_logic_vector(0 to N-1)` 的上界是 `N-1`，但位宽是 \( N \)（从下标 0 到 N-1 共 \( N \) 位）。所以「上界 = `len-1`」与「位宽 = `len`」是同一件事的两种说法。

---

### 4.6 _DutSignals：DUT 信号声明

#### 4.6.1 概念说明

DUT 的每个端口在 TB 里都需要一个对应的 `signal`，既用来连接 DUT 实例（`port map`），也供测试进程读写激励与响应。`_DutSignals` 负责一次性声明所有这些信号。

它的精妙之处在于初值的确定：复用 u4-l1 讲过的 `GetPortValue(port, active)` 这个「端口初值单一真相源」，并按端口的 `TYPE` 标签分三种情况给初值：

- **复位信号（`TYPE=RST`）**：取「有效」值 `GetPortValue(sig, True)`——复位在上电时应处于有效态。
- **时钟信号（`TYPE=CLK`）**：同样取「有效」值，注释说明是为了「与上升沿对齐」（clocks start active so they are rising edge aligned）。
- **其它信号**：取「无效」值 `GetPortValue(sig, False)`——数据/控制类信号上电时处于无效态，避免误触发。
- **未知类型**：捕获 `UnknownVhdlType` 异常，不给初值（只写 `signal <name> : <type>;`）。

#### 4.6.2 核心流程

```
_DutSignals(f):
    VhdlTitle("DUT Signals", level=2)
    for sig in dutInfo.ports:
        try:
            if TYPE=RST:  default = " := " + GetPortValue(sig, True)
            elif TYPE=CLK: default = " := " + GetPortValue(sig, True)
            else:          default = " := " + GetPortValue(sig, False)
        except UnknownVhdlType:
            default = ""
        WriteLn("signal <name> : <type><default>;")
```

#### 4.6.3 源码精读

[TbGen.py:176-190](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L176-L190) —— `_DutSignals` 全文。注意：

- 第 178 行 `sigs = self.dutInfo.ports`，即遍历 DUT 的**全部**端口，每个端口生成一个同名 `signal`。
- 第 181-186 行用 `HastTagValue` 判定 `TYPE`，分别走复位、时钟、普通三条初值路径。`GetPortValue` 内部再根据 `LOWACTIVE` 标签决定极性、根据端口类型（`std_logic` / `std_logic_vector`）决定是否包 `(others => ...)`（详见 u4-l1）。
- 第 187-188 行的 `except UnknownVhdlType` 兜底：若端口类型既不是 `std_logic` 也不是 `std_logic_vector`（例如自定义类型），`GetPortValue` 抛异常，这里捕获后 `default = ""`，于是只声明信号、不给初值。**这是一个静默降级**：陌生类型的端口不会让生成失败，但也不会有初值（这正是 u6-l3「扩展新 VHDL 类型」实践的切入点）。
- 第 189 行 `str(sig.type)` 把 `VhdlType` 对象还原成 VHDL 类型文本（如 `std_logic_vector(Width_g-1 downto 0)`，回顾 u3-l2 的 `VhdlType.__str__`）。

以 `simpleTb` 为例，部分生成结果：

```vhdl
	signal InClk : std_logic := '1';      -- 时钟，取 active
	signal InRst : std_logic := '0';      -- 复位（InRst 无 LOWACTIVE，active='1'? 待本地验证极性）
	signal InData : std_logic_vector(Width_g-1 downto 0) := (others => '0');
```

> 说明：`InRst` 的具体初值取决于 `GetPortValue(InRst, True)` 在 `LOWACTIVE` 缺省时的返回（高有效时 active=`'1'`）。若你想确认极性，可对照 [DutInfo.py:68-79](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/DutInfo.py#L68-L79) 自行推演，或运行生成后查看实际输出。

#### 4.6.4 代码实践

**实践目标**：验证 `LOWACTIVE` 标签如何翻转 DUT 信号的初值。

**操作步骤**：

1. 在 `simpleTb` 的 VHDL 里，给某个 `std_logic` 输出端口（如 `InFull`）追加标签 `$$ LOWACTIVE=true $$`。
2. 运行生成，打开 `*_tb.vhd`，定位 `-- *** DUT Signals ***`，找到 `InFull` 那一行，记录其初值。
3. 再把标签改成 `$$ LOWACTIVE=false $$`（或删掉），重新生成，对比初值。

**需要观察的现象**：`LOWACTIVE=true` 时，`InFull`（非 CLK/RST，走 `GetPortValue(sig, False)`）的初值会从 `'0'` 翻转成 `'1'`。

**预期结果**：低有效器件的「无效态」是 `'1'`，所以 `LOWACTIVE=true` 时普通信号的初值（inactive）变为 `'1'`。这一处改动只动了标签，`_DutSignals` 与 `GetPortValue` 的代码完全没变，体现了「单一真相源」的复用价值。

#### 4.6.5 小练习与答案

**练习 1**：一个 `std_logic_vector` 类型的普通端口（非 CLK/RST），其信号初值长什么样？

> **答案**：走 `GetPortValue(sig, False)` 分支，对 `std_logic_vector` 返回 `(others => '0')`（无 `LOWACTIVE` 时 inactive=`'0'`）。所以生成形如 `signal <name> : std_logic_vector(...) := (others => '0');`。

**练习 2**：如果给某端口指定了一个 `GetPortValue` 不认识的类型（比如 `unsigned`），`_DutSignals` 会崩溃吗？

> **答案**：不会。`GetPortValue` 抛 `UnknownVhdlType`，`_DutSignals` 在第 187 行 `except UnknownVhdlType` 捕获它，令 `default = ""`，于是生成 `signal <name> : unsigned;`（无初值）。生成照常完成，只是该信号没有初值——这正是后续 u6-l3「扩展新类型」要改进的地方。

---

## 5. 综合实践

**任务**：把 `simpleTb` 示例从头到尾跑一遍，然后在生成的 `tb/psi_common_async_fifo_tb.vhd` 上做一次「段落溯源」标注。

**步骤**：

1. 进入 `example/simpleTb/`，执行 `run.bat`（或 `python ../../TbGen.py -src ./psi_common_async_fifo.vhd -dst ./tb -clear -force`）。若本机缺 `PsiPyUtils`，则改为**纯阅读型实践**：直接基于 [TbGen.py:229-253](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L229-L253) 在纸上推导输出结构。
2. 打开生成的 `tb/psi_common_async_fifo_tb.vhd`。
3. 用注释或一张表，为文件里出现的**每一个**段落标题标注它由哪个方法产出。至少应覆盖以下标题（按出现顺序）：
   - `Testbench generated by TbGen.py` → `_Header`
   - `Libraries` → `DutInfo.LibraryDeclarations`
   - `Entity Declaration` → `_EntityDeclaration`
   - `Architecture` → `Generate` 直写 + `VhdlTitle`
   - `Fixed Generics` / `Not Assigned Generics (default values)` → `_GenericConstants`
   - `TB Control` → `_TbControlSignals`
   - `DUT Signals` → `_DutSignals`
   - `DUT Instantiation` → `_DutInstantiation`
   - `Testbench Control !DO NOT EDIT!` → `_TbControl`
   - `Clocks !DO NOT EDIT!` → `_Clocks`
   - `Resets` → `_Resets`
   - `Processes` → `_Processes`
4. **进阶验证**：在 `Architecture` 段里，确认 `_GenericConstants` / `_TbControlSignals` / `_DutSignals` 三个子段都缩进在 `architecture sim of ... is` 之下；而 `_DutInstantiation` 等并发语句缩进在 `begin` 之下。这印证了 4.1 讲的「声明区 vs 并发语句区」缩进差异。
5. **因果追踪**：找到 `ProcessDone` 的声明（`std_logic_vector(0 to 1)`），解释为什么上界是 1——因为 `tbProcesses = ["Input", "Output"]` 长度为 2（来自文件级标签 `PROCESSES=Input,Output`）。

**预期结果**：你得到一份「生成文件 ↔ 源码方法」的完整对照清单。此后无论 `Generate` 如何被调用，你都能在生成的 TB 里快速定位「这段是谁写的、为什么这么写」。

## 6. 本讲小结

- **`Generate` 是一个线性写作器**：它不做决策，只按 VHDL 物理顺序（头 → 库 → 实体 → 架构声明区 → `begin` → 并发语句 → `end;`）把各 `_Xxx()` 方法的输出拼成一个文件，核心调度逻辑集中在 [TbGen.py:229-253](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L229-L253) 这一段。
- **每个 `_Xxx()` 拥有一个段落**，通过接收并返回同一个 `FileWriter` 实现链式调用；段落标题由 `VhdlTitle`（level 1 大段、level 2 子段）统一生成。
- **`_Header` + `CopyrightNotice`** 负责文件顶部的版权与生成说明，版权年份由 `datetime.now().year` 动态填充。
- **`_EntityDeclaration`** 只把 `EXPORT=true` 的 generic 写进实体的 `generic (...)` 子句，并用 `RemoveFromLastLine` 修掉尾分号。
- **`_GenericConstants`** 把 generic 分三类：`CONSTANT` 用标签值、未指定用 DUT 默认值、`EXPORT=true` 不重复定义（多用例下才汇总成 `Generics_c` 记录）。
- **`_TbControlSignals`** 声明共有的脚手架（`TbRunning` / `NextCase` / `ProcessDone` / `AllProcessesDone_c` / `TbProcNr_*_c`），向量宽度由 `tbProcesses` 长度决定。
- **`_DutSignals`** 为每个端口生成一个 `signal`，初值复用 `GetPortValue`，按 `TYPE` 分 CLK/RST（取 active）与普通（取 inactive）两路，未知类型静默降级为无初值。
- **`FileWriter`（外部 `PsiPyUtils`）** 提供 `WriteLn` / `IncIndent` / `DecIndent` / `RemoveFromLastLine` 四类原语，让方法自管理缩进并回改尾标点；其精确内部实现不在本仓库内。

## 7. 下一步学习建议

- **下一讲 u4-l3《时钟、复位、进程与控制信号生成》** 会钻进本讲只点到为止的 `_Clocks` / `_Resets` / `_Processes` / `_TbControl` / `_DutInstantiation` 的内部实现，讲清 `FREQ` 如何算半周期、复位如何归属时钟、`ProcessDone = AllProcessesDone_c` 如何让仿真结束。建议先把本讲的「段落 ↔ 方法」对照表记熟，再进入细节。
- **横向阅读**：对照 [DutInfo.py:82-90](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/DutInfo.py#L82-L90) 的 `LibraryDeclarations` 与 [TbInfo.py:50-66](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbInfo.py#L50-L66) 的三个包声明方法，补全本讲跳过的「库声明」与「包声明」写作细节。
- **向后再看 u5**：本讲多次提到「多用例模式额外生成 TB 包与 case 包」，那是 u5 的主题；学完 u4-l3 后再去读，会更顺。
- **想动手扩展的读者**：`_DutSignals` 对未知类型的静默降级（[TbGen.py:187-188](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L187-L188)）是 u6-l3「扩展新 VHDL 类型」实践的天然入口，可以提前留意。
