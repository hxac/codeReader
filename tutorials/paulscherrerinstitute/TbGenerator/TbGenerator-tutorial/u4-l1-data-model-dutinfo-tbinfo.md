# 数据模型：DutInfo 与 TbInfo

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清 `DutInfo` 与 `TbInfo` 两个数据模型各自负责什么、它们之间的数据如何流动。
- 解释 `DutInfo.__init__` 是如何把 `VhdlFile` 的原始解析结果「再加工」成可直接查询的字段的（实体名、库归类、文件级标签）。
- 读懂 `dutLibrary` 视图与 `LibraryDeclarations` 如何把 DUT 的 `library … use …` 翻译进 testbench 顶部。
- 理解 `GetPortValue` 作为「端口初始值单一真相源」的实现逻辑。
- 读懂 `TbInfo.__init__` 如何把文件级标签翻译成生成参数（`tbName` / `tbProcesses` / `isMultiCaseTb` / `tbUserPackages`）。
- 区分三类包声明方法 `UserPkgDelcaration` / `TbPkgDeclaration` / `TbCaseDeclaration` 各自写给谁。

本讲只讲「数据模型层」。至于这些模型被生成器方法如何逐段消费，属于 u4-l2（`Generate` 主流程）与 u4-l3（时钟/复位/进程）的内容，本讲只在必要时承接提及，不展开。

## 2. 前置知识

本讲站在 u3-2 与 u2-3 的肩膀上，复用以下已建立的概念：

- **`VhdlFile` 的产出（u3-2）**：`VhdlFile(filePath)` 读一份 VHDL，产出 `.entity`（含 `.generics` / `.ports`，每项带 `.name` / `.type` / `.comment`）、`.usestatements`（每条带 `.library` / `.element` / `.object`）与 `.commentLines`（独立注释行，每条带 `.comment` 文本）。本讲不再展开解析文法，只把这些产物当成 `DutInfo` 的「原料」。
- **`$$ … $$` 标签系统（u2-l1、u2-l3）**：标签写在注释里，`DutInfo._ParseTags` 用 pyparsing 把一段注释解析成 `dict`，键统一小写；单值返回字符串、列表返回 `list`。文件级标签（`PROCESSES` / `TESTCASES` / `DUTLIB` / `TBPKG`）写在独立注释行，描述整个 testbench 而非单个端口。
- **`FileWriter`（外部库 PsiPyUtils）**：负责带缩进的文件写入，链式方法 `WriteLn().IncIndent().DecIndent()`，可作为上下文管理器使用（见 `TbGen.py` 第 228 行 `with FileWriter(tbPath + "/" + ..., overwrite=overwrite) as f:`）。本讲把它当成「输出管道」，不深入其实现。

一句话回顾数据流的「形状」：`VHDL 文件 → VhdlFile（解析） → DutInfo（DUT 数据模型） → TbInfo（测试台模型） → 生成器（TbGenerator）`。本讲聚焦中间两格。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用到的主要成员 |
| --- | --- | --- |
| [DutInfo.py](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/DutInfo.py) | DUT 的数据模型：封装 `VhdlFile`、归类库、收集文件级标签，并提供标签工具与端口取值方法 | `DutInfo.__init__`、`dutLibrary`、`GetPortValue`、`LibraryDeclarations` |
| [TbInfo.py](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbInfo.py) | 测试台的数据模型：吃一个 `DutInfo`，翻译出 TB 名称、过程、用例、用户包等生成参数 | `TbInfo.__init__`、`UserPkgDelcaration`、`TbPkgDeclaration`、`TbCaseDeclaration` |
| [UtilFunc.py](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/UtilFunc.py) | 输出格式化小工具 | `VhdlTitle`（写段落标题，被 `LibraryDeclarations` 调用） |

辅助示例文件（仅用于观察真实标签，本讲不解析）：

- `example/simpleTb/psi_common_async_fifo.vhd`：单用例示例，含 `-- $$ PROCESSES=Input,Output $$`。
- `example/multiCaseTb/psi_common_async_fifo.vhd`：多用例示例，额外含 `-- $$ TESTCASES=Full,Empty $$`。

## 4. 核心概念与源码讲解

### 4.1 DutInfo：DUT 的数据模型

#### 4.1.1 概念说明

`DutInfo` 是「被测设计（DUT）」在 Python 世界里的代言对象。它在 u3-2 的 `VhdlFile` 之上做了一层「再加工」：

- `VhdlFile` 是**按文法忠实还原**出来的原始结构，尽量贴近 VHDL 源文本。
- `DutInfo` 则面向**生成器的使用习惯**重新组织数据——把 `use` 语句按库归到字典里、把散落的文件级标签汇成一个 `fileScopeTags` 字典、给实体名起一个简短的别名 `name`。

可以把 `VhdlFile` 想成「原始快递箱」，`DutInfo` 是「拆箱分拣后摆上架」的货架：生成器方法不需要再满箱子翻找，直接从货架上取即可。

#### 4.1.2 核心流程

`DutInfo.__init__(filePath)` 顺序做三件事：

1. **解析 VHDL**：`self.parseInfo = VhdlFile(filePath)`，并把实体名提到 `self.name`。
2. **按库归类 `use` 语句**：遍历 `parseInfo.usestatements`，以 `s.library` 为键聚合成 `self.libraries`，值是该库下的 use 语句列表。
3. **收集文件级标签**：遍历 `parseInfo.commentLines`，对每条 `.comment` 调 `_ParseTags`，用 `dict.update` 合并进 `self.fileScopeTags`。

```
filePath
   │
   ▼  VhdlFile(...)
parseInfo ──► self.name = entity.name
   │
   ├─ usestatements  ──► 按 library 聚合 ──► self.libraries : {lib: [use, ...]}
   │
   └─ commentLines   ──► 逐行 _ParseTags ──► update 合并 ──► self.fileScopeTags : {tag: val}
```

> 关于 `_ParseTags` 的 pyparsing 文法细节，已在 u2-l1 讲透，本讲直接复用其结论：键统一小写，单值返回 `str`、列表返回 `list`，多块按 key 合并、同名后写覆盖先写。

#### 4.1.3 源码精读

构造函数整体（注释即三件事）：

[DutInfo.py:36-51](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/DutInfo.py#L36-L51) —— 构造 `DutInfo`：解析 VHDL、归类库、收集文件级标签。

其中「按库归类」这一小段值得细看：

[DutInfo.py:40-45](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/DutInfo.py#L40-L45) —— 把扁平的 use 语句列表，聚合成 `{library: [use,...]}` 字典；首次见到某库就建空列表再 `append`。

这是一个经典的「按字段分组」写法。归类后，`self.libraries` 就成了 4.2 节 `LibraryDeclarations` 的直接数据源。

而实体名、generics、ports 则通过只读属性直通 `parseInfo`，避免重复存储：

[DutInfo.py:53-59](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/DutInfo.py#L53-L59) —— `generics` / `ports` 是 `@property`，直接转发到 `parseInfo.entity`，不复制数据。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：确认 `DutInfo.__init__` 对 simpleTb 示例产出的三个字段长什么样。
2. **操作**：在仓库根目录用 Python 直接构造（示例代码，**未实际运行**）：

   ```python
   # 示例代码：在仓库根目录执行 python -i demo_dutinfo.py
   from DutInfo import DutInfo
   d = DutInfo("example/simpleTb/psi_common_async_fifo.vhd")
   print("name:", d.name)
   print("libraries:", {k: [u.object for u in v] for k, v in d.libraries.items()})
   print("fileScopeTags:", d.fileScopeTags)
   ```

3. **观察**：依据源码推断，预期 `name` 为 `psi_common_async_fifo`；`libraries` 的键应为 `{'ieee': [...], 'work': [...]}`；`fileScopeTags` 至少含 `{'processes': ['Input', 'Output']}`。
4. **预期结果**：与上面推断一致。
5. **运行结果**：待本地验证（依赖本机已安装 `pyparsing` 与 `PsiPyUtils`）。

#### 4.1.5 小练习与答案

- **练习 1**：如果同一文件里有两块独立的 `$$ PROCESSES=… $$` 注释，最终 `fileScopeTags["processes"]` 会是哪一块的值？
  - **答案**：是后一块的值。`__init__` 用 `dict.update` 逐行合并，同名 key 后写覆盖先写。
- **练习 2**：为什么 `generics`/`ports` 用 `@property` 转发，而不是在 `__init__` 里复制一份？
  - **答案**：保持单一数据源（`parseInfo`），避免两份数据不一致；`parseInfo` 已持有全部信息，转发即可零成本取用。

---

### 4.2 dutLibrary 视图与 LibraryDeclarations

#### 4.2.1 概念说明

DUT 的 VHDL 顶部通常会写 `library work;`。但「work」只是 VHDL 的默认工作库名，真实综合/仿真时这个库可能叫别的名字（比如项目自定义库名）。`DutInfo` 通过文件级标签 `DUTLIB` 让用户声明「这个 DUT 实际所在的库」，并提供 `dutLibrary` 这个带默认值的「视图」。

`LibraryDeclarations` 则是第一个「会向文件里写 VHDL」的方法：它把 `self.libraries` 翻译成 testbench 顶部的 `library …; use …;` 段落，并把其中的 `work` 字样替换成 `dutLibrary`。它也是 `Generate` 写出的**第一个**实质段落（紧跟 Header 之后）。

#### 4.2.2 核心流程

1. 写一个 level-1 段落标题 `Libraries`（由 `VhdlTitle` 输出三行 60 横杠包裹的标题）。
2. 对 `self.libraries` 的键**排序**后遍历（保证输出稳定、可 diff）。
3. 每个库写一行 `library <库名>;`，其中库名里的 `work` 被替换成 `dutLibrary`。
4. 缩进一级，逐条写出该库下的 `use <库>.<元素>.<对象>;`，库名同样替换 `work`。
5. 退缩进、空一行。

其中 `dutLibrary` 的取值规则：

- 若文件级标签含 `DUTLIB`，用其值；
- 否则返回字符串 `"work"`（默认）。

#### 4.2.3 源码精读

`dutLibrary` 是一个 `@property`，对 `fileScopeTags` 做带默认值的取值：

[DutInfo.py:61-66](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/DutInfo.py#L61-L66) —— `dutLibrary`：有 `DUTLIB` 标签就用标签值，否则默认 `"work"`。

`LibraryDeclarations` 把库归类数据写成 VHDL，并替换 `work` 字样：

[DutInfo.py:82-90](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/DutInfo.py#L82-L90) —— 排序遍历库，写 `library`/`use` 行，`l.replace("work", self.dutLibrary)` 把默认库名替换为真实库名。

两个细节值得注意：

- `sorted(self.libraries)`：对字典的键排序，因此输出顺序固定（如 `ieee` 在 `work` 之前），便于版本对比。
- `.replace("work", self.dutLibrary)` 是**子串替换**而非整词匹配。在默认 `dutLibrary="work"` 时它是恒等操作（无副作用）；只有当用户通过 `DUTLIB` 指定别的库名时，`work` 才被真正替换。这也意味着它不会新增任何 `library` 声明，只是改写既有 `work` 字样。

`VhdlTitle` 的输出形态可对照：

[UtilFunc.py:10-19](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/UtilFunc.py#L10-L19) —— level=1 时写「60 横杠 / `-- 标题` / 60 横杠」三行。

#### 4.2.4 代码实践（源码阅读型）

1. **目标**：理解「加 `DUTLIB` 标签」会如何改变库段落输出。
2. **操作**：阅读 `LibraryDeclarations` 与 `dutLibrary` 两段源码，在纸上推断以下两种输入各自的 `library`/`use` 输出：
   - (a) simpleTb 示例（无 `DUTLIB` 标签，`libraries` 含 `ieee`、`work`）。
   - (b) 在 (a) 基础上，于文件级注释里加一行 `-- $$ DUTLIB=psi_lib $$`。
3. **观察**：关注 (b) 中哪些行的 `work` 被换成了 `psi_lib`，哪些行（如 `ieee`）保持不变。
4. **预期结果**：
   - (a) `library ieee;` + `library work;`，`use` 行库名保持原样。
   - (b) `library work;` 变为 `library psi_lib;`，`use work.psi_common_…` 的 `work` 也变为 `psi_lib`；`ieee` 相关行不受影响。
5. **运行结果**：待本地验证（可由本讲「综合实践」脚本实际写出文件确认）。

#### 4.2.5 小练习与答案

- **练习 1**：`DUTLIB` 标签的大小写敏感吗？写 `-- $$ DutLib=psi_lib $$` 会生效吗？
  - **答案**：会生效。`_ParseTags` 把键统一小写（`tag.get("tag").lower()`），`Tags.DUTLIB` 常量也是小写 `"dutlib"`，二者匹配。值 `psi_lib` 保留原始大小写。
- **练习 2**：为什么 `LibraryDeclarations` 排序库、却不排序库内的 use 语句？
  - **答案**：库的顺序影响段落可读性与 diff 稳定性，按字母排；而同一库内 use 语句的逻辑顺序应尊重作者在源文件里的写法，故保持原序。

---

### 4.3 GetPortValue：端口初始值的单一真相源

#### 4.3.1 概念说明

testbench 里要给大量信号赋初值：复位信号要赋「有效」值、普通输入信号要赋「无效」值。但「有效/无效」是逻辑意图，需要翻译成 VHDL 字面量——而且高有效信号和低有效信号（`LOWACTIVE`）的翻译方向相反。`GetPortValue(port, active)` 就是这段翻译逻辑的**唯一集中实现**，被 `_DutSignals` / `_Resets` / `_Processes` / `_TbControl` 多处复用。改它一处，初值处处一致变化。

> 本节只讲它作为「数据模型方法」的实现；这些调用方各自如何使用初值，是 u4-l2/u4-l3 的内容。

#### 4.3.2 核心流程

输入：一个端口 `port`（`VhdlPortDeclaration`，带 `.type.name` 与 `.comment` 标签）和一个布尔 `active`（要「有效」还是「无效」值）。

1. **判断极性**：查端口是否有 `LOWACTIVE=true` 标签（大小写不敏感）。
   - 低有效时：`active=True → '0'`，`active=False → '1'`。
   - 高有效（默认）时：`active=True → '1'`，`active=False → '0'`。
2. **按类型包装**：
   - `std_logic`：直接返回 `'0'` / `'1'`。
   - `std_logic_vector`：包装成 `(others => '0')` / `(others => '1')`。
   - 其它类型：抛 `UnknownVhdlType`。

用真值表概括（`initVal` 为单比特字面量）：

| `active` | 高有效（无 LOWACTIVE） | 低有效（`LOWACTIVE=true`） |
| --- | --- | --- |
| `True`（取有效值） | `'1'` | `'0'` |
| `False`（取无效值） | `'0'` | `'1'` |

向量类型在两端各套一层 `(others => …)`。

#### 4.3.3 源码精读

[DutInfo.py:68-79](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/DutInfo.py#L68-L79) —— `GetPortValue`：先按 `LOWACTIVE` 决定单比特字面量，再按类型包装；未知类型抛 `UnknownVhdlType`。

注意它调用的是类方法 `DutInfo.HastTagValue(port, Tags.LOWACTIVE, "true")`（方法名 `HastTag` 是源码原拼写，含一个 `t`）。这意味着「极性」完全由端口的 `$$ LOWACTIVE=true $$` 标签驱动，是 u2 标签系统在本方法的直接落地。

`UnknownVhdlType` 在文件顶部定义，是一个空异常类，方便上层捕获：

[DutInfo.py:13](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/DutInfo.py#L13) —— 仅 `class UnknownVhdlType(Exception): pass`，作为「遇到不支持的 VHDL 类型」的信号。

#### 4.3.4 代码实践（源码阅读型）

1. **目标**：验证真值表的推断。
2. **操作**：阅读 [DutInfo.py:68-79](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/DutInfo.py#L68-L79)，在纸上填写下表（端口 `Rst_n : in std_logic`，带 `-- $$ LOWACTIVE=true $$`）：

   | 调用 | 返回值 |
   | --- | --- |
   | `GetPortValue(Rst_n, True)` | ? |
   | `GetPortValue(Rst_n, False)` | ? |
3. **预期结果**：`True → '0'`，`False → '1'`（低有效翻转）。若端口是 `std_logic_vector(7 downto 0)` 且高有效，`GetPortValue(p, False)` 应返回 `(others => '0')`。
4. **运行结果**：待本地验证。

#### 4.3.5 小练习与答案

- **练习 1**：一个 `integer` 类型的端口调用 `GetPortValue` 会发生什么？
  - **答案**：`port.type.name` 既非 `std_logic` 也非 `std_logic_vector`，落入 `else` 分支抛 `UnknownVhdlType`。这从机制上限制了 `GetPortValue` 只服务于位类型端口。
- **练习 2**：为什么把这段翻译逻辑集中到 `DutInfo`，而不是在每个生成方法里各自写？
  - **答案**：保证「有效/无效」语义在全 testbench 内一致——同一个复位信号在 `_Resets`（释放到无效）、`_Processes`（等待复位无效）、`_TbControl`（等待复位无效）里用的是同一套判定，避免分散实现导致各处极性不一致。

---

### 4.4 TbInfo：把 DutInfo 翻译成生成参数

#### 4.4.1 概念说明

`TbInfo` 是「测试台（TB）」的代言对象。它**不重新读 VHDL**，而是吃一个已经构造好的 `DutInfo`，把其中的实体名与文件级标签「翻译」成生成器直接需要的参数：

- `tbName`：测试台实体名（实体名 + `_tb`）。
- `tbProcesses`：要生成哪些测试进程。
- `isMultiCaseTb` / `testCases`：是否是多用例 TB、有哪些用例。
- `tbUserPackages`：用户要求 TB 额外 `use` 哪些包。
- `dutInfo`：反向持有 `DutInfo` 引用，便于方法内回查端口。

`DutInfo` 与 `TbInfo` 的职责边界很清晰：`DutInfo` 描述「被测的东西是什么」，`TbInfo` 描述「要搭一个什么样的测试台」。

#### 4.4.2 核心流程

`TbInfo.__init__(info : DutInfo)` 顺序翻译：

1. **多用例判定**：`isMultiCaseTb = Tags.TESTCASES in info.fileScopeTags`——只看键是否存在，是「模式开关」而非数量判断。若是多用例，把 `testCases` 归一成 `list`；否则置 `None`。
2. **TB 名称**：`tbName = info.name + "_tb"`。
3. **进程列表**：默认 `["Stimuli"]`；若文件级标签有 `PROCESSES` 则覆盖，并归一成 `list`。
4. **用户包**：读 `TBPKG` 标签，归一成 `list` 后，按「库.包」拆分，聚合成 `{lib: [pkg, ...]}` 字典 `tbUserPackages`。
5. 反向持有 `self.dutInfo = info`。

其中 `TBPKG` 的拆分规则在源码里是 `lib, pkgName = tuple(pkg.split("."))`——**要求每个包名严格是 `库.包` 两段**。

#### 4.4.3 源码精读

[TbInfo.py:14-45](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbInfo.py#L14-L45) —— 整个 `TbInfo.__init__`：把 `DutInfo` 翻译成 TB 生成参数。

几个关键片段单独点出：

[TbInfo.py:15-22](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbInfo.py#L15-L22) —— `isMultiCaseTb` 只判键是否存在；`testCases` 做字符串→列表归一。

[TbInfo.py:26-30](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbInfo.py#L26-L30) —— `tbProcesses` 缺省 `["Stimuli"]`，有 `PROCESSES` 标签则覆盖并归一。

[TbInfo.py:32-43](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbInfo.py#L32-L43) —— `TBPKG` 拆 `库.包`，按库聚合成 `tbUserPackages` 字典。

附带一个跨模型的小桥：`GetPortsForProcess` 把 `TbInfo` 与 `DutInfo` 的标签工具连起来——按 `PROC` 标签筛端口：

[TbInfo.py:47-48](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbInfo.py#L47-L48) —— `GetPortsForProcess` 直接转发到 `DutInfo.FilterForTag(dutInfo.ports, PROC, process)`，体现 `TbInfo` 反向持有 `dutInfo` 的用途。

#### 4.4.4 代码实践（源码阅读型）

1. **目标**：对比单用例与多用例两个示例经 `TbInfo` 翻译后的参数差异。
2. **操作**：分别对 `example/simpleTb/...vhd`（含 `PROCESSES=Input,Output`，无 `TESTCASES`）与 `example/multiCaseTb/...vhd`（额外含 `TESTCASES=Full,Empty`）在纸上推断 `tbName` / `tbProcesses` / `isMultiCaseTb` / `testCases` 四个值。
3. **预期结果**：

   | 示例 | `tbName` | `tbProcesses` | `isMultiCaseTb` | `testCases` |
   | --- | --- | --- | --- | --- |
   | simpleTb | `psi_common_async_fifo_tb` | `['Input','Output']` | `False` | `None` |
   | multiCaseTb | `psi_common_async_fifo_tb` | `['Input','Output']` | `True` | `['Full','Empty']` |
4. **运行结果**：待本地验证（可由本讲「综合实践」脚本打印确认）。

#### 4.4.5 小练习与答案

- **练习 1**：若 VHDL 里既不写 `PROCESSES` 也不写 `TESTCASES`，`tbProcesses` 和 `isMultiCaseTb` 分别是什么？
  - **答案**：`tbProcesses = ['Stimuli']`（缺省值），`isMultiCaseTb = False`。即退化为最简单的单进程单用例 TB。
- **练习 2**：`TBPKG=work.psi_common_math_pkg` 与 `TBPKG=work.pkg_a, work.pkg_b` 在 `tbUserPackages` 里分别长什么样？
  - **答案**：前者拆成 `{'work': ['psi_common_math_pkg']}`；后者先归一为 `['work.pkg_a', 'work.pkg_b']`，再聚合成 `{'work': ['pkg_a', 'pkg_b']}`（同库合并到同一列表）。

---

### 4.5 包声明三件套：UserPkg / TbPkg / TbCase

#### 4.5.1 概念说明

多用例与扩展场景下，testbench 需要 `use` 三类来源不同的包。`TbInfo` 用三个方法分别写出它们的 `library …; use …;` 段落，命名虽然都是「Declaration」，但服务对象不同：

| 方法 | 数据来源 | 写给谁 | 典型用途 |
| --- | --- | --- | --- |
| `UserPkgDelcaration(f)` | `self.tbUserPackages`（来自 `TBPKG` 标签） | 主 TB / 各 case 文件 | 注入用户自带的工具包 |
| `TbPkgDeclaration(f)` | `self.tbName`（固定） | 各 case 文件 | 引用本 TB 自身的 `*_pkg` |
| `TbCaseDeclaration(f)` | `self.testCases` | 主 TB | 在主 TB 里引用每个用例的 case 包 |

> 方法名 `UserPkgDelcaration` 是源码原拼写（`Delc` 而非 `Decl`），调用时需照搬。

#### 4.5.2 核心流程

三者结构相似：都是「写 `library …;` → 缩进 → 逐条 `use …;` → 退缩进 → 空行」，但取值与归属不同：

- **`UserPkgDelcaration`**：遍历 `tbUserPackages.items()`，每库写一行 `library <lib>;`，缩进后写 `use <lib>.<pkg>.all;`。
- **`TbPkgDeclaration`**：固定写 `library work;` 与 `use work.<tbName>_pkg.all;`（让 case 文件能引用主 TB 的包）。
- **`TbCaseDeclaration`**：固定写 `library work;`，再为每个 `testCase` 写 `use work.<tbName>_case_<case>.all;`（让主 TB 能引用每个用例包）。

#### 4.5.3 源码精读

`UserPkgDelcaration`（注意方法名拼写）：

[TbInfo.py:50-55](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbInfo.py#L50-L55) —— 遍历 `tbUserPackages`，每库写 `library`，缩进写 `use <lib>.<pkg>.all;`。

`TbPkgDeclaration`：

[TbInfo.py:57-60](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbInfo.py#L57-L60) —— 固定引用本 TB 的 `work.<tbName>_pkg.all`。

`TbCaseDeclaration`：

[TbInfo.py:62-66](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbInfo.py#L62-L66) —— 为每个用例写 `use work.<tbName>_case_<case>.all;`，让主 TB 能调用各用例的过程。

可以体会出一条对称关系：`TbPkgDeclaration` 让 **case 文件**找到主 TB 包，`TbCaseDeclaration` 让 **主 TB** 找到各 case 包——这是多用例 TB 中主文件与用例文件相互 `use` 的两根「双向接线」。

#### 4.5.4 代码实践（源码阅读型）

1. **目标**：判断三个方法在「单用例 TB」中各自是否会被调用。
2. **操作**：对照源码与本讲「职责表」，推断在 simpleTb（`isMultiCaseTb=False`，无 `TBPKG`）生成时，三个方法的输出分别是什么。
3. **预期结果**：
   - `UserPkgDelcaration`：`tbUserPackages` 为空，遍历不执行，**无输出**。
   - `TbPkgDeclaration`：固定输出 `library work;` + `use work.psi_common_async_fifo_tb_pkg.all;`。
   - `TbCaseDeclaration`：`testCases` 为 `None`，遍历 `self.testCases` 会报错——因此在单用例流程中**不会被调用**（是否调用由 `Generate` 的多用例分支控制，见 u5）。
4. **运行结果**：待本地验证。

#### 4.5.5 小练习与答案

- **练习 1**：`TbCaseDeclaration` 为什么不能用 `isMultiCaseTb` 为 `False` 的 `TbInfo` 调用？
  - **答案**：单用例时 `self.testCases = None`，方法体 `for c in self.testCases:` 直接对 `None` 迭代会抛 `TypeError`。它只设计给多用例场景使用。
- **练习 2**：`TbPkgDeclaration` 写出的包名 `work.<tbName>_pkg.all` 对应的包文件由谁生成？
  - **答案**：由 `MultiFileTb.WriteTbPkg` 生成（见 u5），本讲的 `TbPkgDeclaration` 只负责在需要的地方 `use` 它，二者通过固定的命名约定（`<tbName>_pkg`）对接。

## 5. 综合实践

**任务**：写一个最小脚本，亲手构造 `DutInfo` → `TbInfo`，把数据模型层的几个关键值打印出来，并调用 `LibraryDeclarations` 真正写出一段库声明到文件，亲眼看到「数据模型 → VHDL 文本」这一步。

1. **实践目标**：把本讲的 `DutInfo` / `TbInfo` / `dutLibrary` / `LibraryDeclarations` 串成一条可运行链路。
2. **操作步骤**：

   在仓库根目录新建 `demo_datamodel.py`（示例代码），内容如下：

   ```python
   # 示例代码：构造数据模型并打印关键字段、写出库声明
   import os
   from DutInfo import DutInfo
   from TbInfo import TbInfo
   from PsiPyUtils import FileWriter

   SRC = "example/simpleTb/psi_common_async_fifo.vhd"

   dut = DutInfo(SRC)
   tb = TbInfo(dut)

   print("dutLibrary   :", dut.dutLibrary)
   print("tbName       :", tb.tbName)
   print("tbProcesses  :", tb.tbProcesses)
   print("isMultiCaseTb:", tb.isMultiCaseTb)

   # 调用 LibraryDeclarations，把库声明写到一个临时文件
   outPath = "demo_libs.vhd"
   with FileWriter(outPath) as f:           # 构造方式参考 TbGen.py:228
       dut.LibraryDeclarations(f)
   print("written to", outPath)
   ```

   运行：`python demo_datamodel.py`，然后查看 `demo_libs.vhd` 的内容。

3. **需要观察的现象**：
   - 终端打印的四个值是否与本讲 4.2 / 4.4 的推断一致（`work` / `psi_common_async_fifo_tb` / `['Input','Output']` / `False`）。
   - `demo_libs.vhd` 顶部是否有 `Libraries` 标题（三行 60 横杠），其下依次是 `library ieee;` 及两条 `use ieee.…;`、`library work;` 及两条 `use work.psi_common_…;`，且 `use` 行比 `library` 行多一级缩进。
4. **预期结果**：打印值与上表一致；`demo_libs.vhd` 内容形如：

   ```vhdl
   ------------------------------------------------------------
   -- Libraries
   ------------------------------------------------------------
   library ieee;
       use ieee.std_logic_1164.all;
       use ieee.numeric_std.all;
   library work;
       use work.psi_common_logic_pkg.all;
       use work.psi_common_math_pkg.all;
   ```
5. **进阶变式**：把脚本里的 `SRC` 换成 `example/multiCaseTb/psi_common_async_fifo.vhd`，重跑并对比 `isMultiCaseTb`、`tb.testCases` 的变化；再在 simpleTb 的 VHDL 文件级注释里临时加一行 `-- $$ DUTLIB=psi_lib $$`（仅用于本练习，**练习结束请还原，勿提交对源码的修改**），重跑观察 `demo_libs.vhd` 中 `work` 被替换的位置。
6. **运行结果**：待本地验证（依赖本机已安装 `pyparsing` 与 `PsiPyUtils ≥ 3.0.0`）。

## 6. 本讲小结

- `DutInfo` 是 DUT 数据模型，在 `VhdlFile` 之上做三件再加工：提实体名 `name`、按库归类 `use` 语句成 `libraries` 字典、汇总文件级标签成 `fileScopeTags` 字典。
- `dutLibrary` 是 `DUTLIB` 标签的带默认值 `"work"` 视图；`LibraryDeclarations` 据此写出 testbench 第一个实质段落（库/use 声明），并做 `work → dutLibrary` 的子串替换。
- `GetPortValue(port, active)` 是端口初始值的单一真相源：`LOWACTIVE` 标签决定极性、端口类型决定包装（位 / 向量），未知类型抛 `UnknownVhdlType`。
- `TbInfo` 吃一个 `DutInfo`，翻译出 `tbName` / `tbProcesses`（缺省 `["Stimuli"]`）/ `isMultiCaseTb`（仅判 `TESTCASES` 键存在）/ `tbUserPackages`（按 `库.包` 聚合），并反向持有 `dutInfo`。
- 三个包声明方法分工明确：`UserPkgDelcaration` 写用户包（源 `TBPKG`）、`TbPkgDeclaration` 写本 TB 包（供 case 文件引用）、`TbCaseDeclaration` 写各 case 包（供主 TB 引用，仅多用例可用）。
- 数据流主线：`VHDL → VhdlFile → DutInfo → TbInfo → 生成器`；本讲覆盖中间两格，为 u4-l2 的 `Generate` 主流程提供全部被消费的数据。

## 7. 下一步学习建议

- **u4-l2（Generate 主流程与单文件 TB 骨架）**：看 `TbGenerator.Generate` 如何按固定顺序调用一串 `_Xxx()` 方法，把这些数据模型逐段消费成一份完整的 `*_tb.vhd`。本讲的 `LibraryDeclarations` 就是其中的第一个实质段落。
- **u4-l3（时钟、复位、进程与控制信号生成）**：深入 `_Clocks` / `_Resets` / `_Processes` 如何用 `TYPE` / `FREQ` / `CLK` / `PROC` 标签驱动，并大量复用本讲的 `GetPortValue` 与 `FilterForTag`。
- **u5（多文件多用例 testbench）**：当 `isMultiCaseTb=True` 时，本讲的 `TbCaseDeclaration` / `tbProcesses` / `testCases` 如何与 `MultiFileTb.WriteTbPkg` / `WriteCasePkg` 协作，生成主 TB + TB 包 + 每用例一个 case 包的多文件结构。
