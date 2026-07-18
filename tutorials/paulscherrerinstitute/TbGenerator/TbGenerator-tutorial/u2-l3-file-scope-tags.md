# 文件级标签与 TbInfo 建模

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清「文件级标签」与「端口/generic 标签」的区别，并知道 `PROCESSES`、`TESTCASES`、`DUTLIB`、`TBPKG` 这四个文件级标签分别控制什么。
- 看懂 `DutInfo` 是如何把散落在 VHDL 注释里的 `$$ ... $$` 收集成一个 `fileScopeTags` 字典的。
- 理解 `TbInfo.__init__` 如何把这些原始标签「翻译」成生成器真正使用的参数：`tbName`、`tbProcesses`、`isMultiCaseTb`、`testCases`、`tbUserPackages`、`dutLibrary`。
- 能够通过增删文件级标签，预测生成出的 testbench 在结构、过程数量、文件数量上的变化。

本讲只讲「文件级」标签的收集与建模，**不**展开多用例 TB 内部 procedure 的生成细节（那是 u5 的内容），也**不**重复端口/generic 标签的语义（那是 u2-l2 的内容）。

## 2. 前置知识

在进入本讲前，请确认你已经了解以下概念（来自 u2-l1、u2-l2）：

- **标签（tag）**：写在 VHDL 注释里的 `$$ 键=值; 键=值 $$` 文本，是 TbGenerator 的输入契约。标签名大小写不敏感，值分单值（`FREQ=100e6`）与列表（`PROCESSES=Input,Output`）两种。
- **`_ParseTags`**：`DutInfo` 的类方法，用 pyparsing 把一段注释文本解析成 Python 字典，键统一小写。
- **标签的作用对象**：u2-l2 讲的是挂在**端口**或 **generic** 上的标签（如 `TYPE`、`FREQ`、`EXPORT`），它们依附于某个具体声明。本讲讲的标签作用对象是**整个文件**，写在独立的注释行里，描述的是「这个 DUT 对应的 testbench 应该长什么样」。
- **`DutInfo` 与 `TbInfo` 的分工**：`DutInfo` 封装「DUT 是什么」（实体名、端口、generic、库、文件级标签）；`TbInfo` 封装「testbench 应该是什么」（名字、过程、用例、包）。`TbInfo` 在构造时读取 `DutInfo`，把原始信息翻译成生成参数。

一个关键直觉：**端口/generic 标签回答「这个端口/generic 怎么处理」，文件级标签回答「整个 testbench 的骨架长什么样」**。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [DutInfo.py](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/DutInfo.py) | 定义 `Tags` 常量、`DutInfo` 数据模型；负责收集 `fileScopeTags`、提供 `dutLibrary` 与 `LibraryDeclarations`。 |
| [TbInfo.py](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbInfo.py) | 定义 `TbInfo`；在 `__init__` 中把 `DutInfo.fileScopeTags` 翻译成 testbench 参数，并提供三类包声明方法。 |
| [TbGen.py](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py) | 生成器主类；在 `Generate`、`_Processes`、`_TbControl`、`_DutInstantiation` 中消费 `TbInfo` 与 `dutLibrary`。 |
| [MultiFileTb.py](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/MultiFileTb.py) | 多用例模式下的 TB 包与 case 包生成；文件命名规则决定了「多用例模式会多出几个文件」。 |
| [VhdlParse.py](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/VhdlParse.py) | VHDL 解析器；`VhdlFile.commentLines` 是文件级标签的数据来源。 |
| [example/multiCaseTb/psi_common_async_fifo.vhd](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/example/multiCaseTb/psi_common_async_fifo.vhd) | 多用例示例 DUT，同时使用了 `PROCESSES` 与 `TESTCASES`。 |
| [example/simpleTb/psi_common_async_fifo.vhd](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/example/simpleTb/psi_common_async_fifo.vhd) | 单用例示例 DUT，只用了 `PROCESSES`，作为对照。 |

## 4. 核心概念与源码讲解

本讲按四个最小模块展开：

1. **fileScopeTags** —— 文件级标签是怎么被收集进一个字典的。
2. **dutLibrary** —— `DUTLIB` 标签如何改变 DUT 实例化与库声明里的库名。
3. **TbInfo.__init__** —— `PROCESSES`、`TESTCASES` 如何被翻译成 `tbName`、`tbProcesses`、`isMultiCaseTb`、`testCases`。
4. **tbUserPackages** —— `TBPKG` 标签如何注入额外的 `use` 包。

### 4.1 fileScopeTags：文件级标签的收集

#### 4.1.1 概念说明

端口/generic 标签写在某一行声明的**行尾注释**里，解析后挂在那个端口/generic 对象的 `.comment` 上（见 u2-l1）。而**文件级标签**写在**独立的注释行**里，不属于任何具体端口或 generic，它描述的是整个 testbench 的整体属性。

合法的文件级标签一共四个，集中在 `Tags` 类里：

```python
#File scope tags
PROCESSES = "processes"
TESTCASES = "testcases"
DUTLIB = "dutlib"
TBPKG = "tbpkg"
```

它们的含义一句话概括：

| 标签 | 值的形态 | 控制什么 |
| --- | --- | --- |
| `PROCESSES` | 列表（如 `Input,Output`） | testbench 里有哪些测试过程（`p_Input`、`p_Output`）。缺失时默认 `["Stimuli"]`。 |
| `TESTCASES` | 列表（如 `Full,Empty`） | 是否进入「多用例模式」，以及有几个用例。缺失即为单用例模式。 |
| `DUTLIB` | 单值（如 `mylib`） | DUT 实例化与库声明里，把 `work` 替换成哪个库名。缺失时默认 `"work"`。 |
| `TBPKG` | 列表（如 `tb_lib.my_pkg`） | 在 testbench 顶部额外注入哪些 `use` 包。缺失时不注入。 |

#### 4.1.2 核心流程

文件级标签的收集分三步：

1. **VhdlFile 解析阶段**：`VhdlFile` 用 `VhdlCommentLine` 文法扫描整段 VHDL 源码，把所有「以 `--` 开头的整行注释」收集进 `self.commentLines`（一个列表，每项的 `.comment` 是去掉 `--` 后的文本）。
2. **DutInfo 构造阶段**：`DutInfo.__init__` 遍历 `commentLines`，对每一条注释调用 `_ParseTags`，得到一个字典，然后用 `update` 合并进 `self.fileScopeTags`。
3. **消费阶段**：`fileScopeTags` 被两个地方读取——`DutInfo.dutLibrary` 属性（读 `DUTLIB`）和 `TbInfo.__init__`（读其余三个）。

伪代码：

```
fileScopeTags = {}
for 注释行 in parseInfo.commentLines:
    tags = _ParseTags(注释行.comment)   # {"processes": [...], "testcases": [...]}
    fileScopeTags.update(tags)          # 多块标签合并到一个字典
```

这里有两个**容易踩坑的细节**，务必记住：

- **合并而非覆盖整体**：`update` 是按 key 合并的。不同注释行里写不同的标签会汇总到一起；但如果两行写了**同名**标签，后写的会覆盖先写的。
- **单值 vs 列表的不一致**：`_ParseTags` 对「带逗号」的值返回 list（如 `Input,Output` → `["Input","Output"]`），对「不带逗号」的值返回 str（如 `Stimuli` → `"Stimuli"`）。所以 `PROCESSES=Stimuli`（只写一个过程）在 `fileScopeTags` 里是个**字符串**而不是列表。下游因此必须做归一化（见 4.3）。

#### 4.1.3 源码精读

文件级标签名集中在 `Tags` 类：[DutInfo.py:28-32](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/DutInfo.py#L28-L32) 声明了 `PROCESSES`、`TESTCASES`、`DUTLIB`、`TBPKG` 四个常量。

收集逻辑在 `DutInfo.__init__` 的最后一段：[DutInfo.py:47-51](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/DutInfo.py#L47-L51)。这段遍历 `self.parseInfo.commentLines`，逐行调 `_ParseTags`，再 `update` 进 `self.fileScopeTags`。

`commentLines` 的源头在 `VhdlFile`：[VhdlParse.py:188-191](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/VhdlParse.py#L188-L191) 用 `VhdlCommentLine.PP().scanString(code)` 扫出所有整行注释。`VhdlCommentLine` 的文法是「行首 + `PP_COMMENT`」：[VhdlParse.py:67-72](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/VhdlParse.py#L67-L72)，而 `PP_COMMENT` 就是 `--` 加本行剩余文本：[VhdlParse.py:28](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/VhdlParse.py#L28)。

在多用例示例里，这两行注释就是文件级标签的来源：

```
-- $$ PROCESSES=Input,Output $$
-- $$ TESTCASES=Full,Empty $$
```

对应 [example/multiCaseTb/psi_common_async_fifo.vhd:24-25](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/example/multiCaseTb/psi_common_async_fifo.vhd#L24-L25)。对照单用例示例只有一行 `PROCESSES`：[example/simpleTb/psi_common_async_fifo.vhd:23](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/example/simpleTb/psi_common_async_fifo.vhd#L23)。

解析后，多用例示例的 `fileScopeTags` 等价于：

```python
{"processes": ["Input", "Output"], "testcases": ["Full", "Empty"]}
```

#### 4.1.4 代码实践

**实践目标**：直接在 Python 里观察 `fileScopeTags`，验证你对「单值/列表」「多行合并」的理解，不必依赖完整的生成流程。

**操作步骤**（示例代码，需在本机 TbGenerator 根目录下运行）：

```python
# 示例代码：在项目根目录启动 python 后执行
from DutInfo import DutInfo

d = DutInfo("example/multiCaseTb/psi_common_async_fifo.vhd")
print("fileScopeTags =", d.fileScopeTags)
print("dutLibrary    =", d.dutLibrary)
```

**需要观察的现象**：

- `fileScopeTags` 应包含 `processes` 与 `testcases` 两个键，值都是列表。
- 因为该示例没有写 `DUTLIB`/`TBPKG`，字典里不应出现 `dutlib`/`tbpkg` 键。

**预期结果**：

```python
fileScopeTags = {'processes': ['Input', 'Output'], 'testcases': ['Full', 'Empty']}
dutLibrary    = work
```

若你想观察「单值形态」，可临时把示例里的 `-- $$ PROCESSES=Input,Output $$` 改成 `-- $$ PROCESSES=Stimuli $$`（只写一个、不带逗号），再次运行，应看到 `fileScopeTags['processes']` 变成字符串 `'Stimuli'` 而非列表。**待本地验证**（修改示例文件前建议先备份）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `-- $$ TESTCASES=Full,Empty $$` 拆成两行 `-- $$ TESTCASES=Full $$` 和 `-- $$ TESTCASES=Empty $$`，`fileScopeTags['testcases']` 最终是什么？

**参考答案**：是 `'Empty'`（字符串）。因为两条注释各自解析出 `{"testcases": "Full"}` 与 `{"testcases": "Empty"}`（单值、无逗号），`update` 按 key 覆盖，后者胜出，且因为是单值所以是 str 而非 list。这正说明：**列表必须写在一行里用逗号分隔**。

**练习 2**：为什么文件级标签不能写成 `-- $$ PROCESSES2=Input $$`？

**参考答案**：`_ParseTags` 的标签名文法是 `pp.Word(pp.alphas)`（见 u2-l1），只允许纯字母。`PROCESSES2` 含数字，整个标签块会被静默跳过，不会出现在 `fileScopeTags` 里。

---

### 4.2 dutLibrary：DUTLIB 标签如何改写库名

#### 4.2.1 概念说明

VHDL 里实例化一个实体通常写成 `entity work.my_entity`，其中 `work` 是「当前编译库」的默认别名。当你的 DUT 实际被编译到别的库（比如 `mylib`）时，生成的 testbench 就必须写 `entity mylib.my_entity`，否则仿真器找不到实体。

`DUTLIB` 标签就是用来告诉 TbGenerator：「请把所有 `work` 替换成我指定的库名」。它是一个**单值**标签，缺失时默认 `"work"`（即不变）。

注意一个反直觉点：`DUTLIB` **不会**凭空生成一条 `library X;` 声明。它只做「替换」——把 DUT 源码里已有的 `library work;` / `use work.xxx.all;` 里的 `work` 字样替换掉，并把 DUT 实例化行的库名改掉。

#### 4.2.2 核心流程

`dutLibrary` 的取值与消费流程：

1. `DutInfo.dutLibrary` 属性读 `fileScopeTags["dutlib"]`，没有则返回 `"work"`。
2. `DutInfo.LibraryDeclarations` 写库声明时，对每个库名和每条 `use` 语句里的 `work` 做 `.replace("work", self.dutLibrary)`。
3. `TbGenerator._DutInstantiation` 写 DUT 实例化行时，用 `entity {dutLibrary}.{name}`。

也就是说，`dutLibrary` 是「DUT 实例化库名」的单一真相源，库声明与实例化两处都引用它，保证一致。

#### 4.2.3 源码精读

`dutLibrary` 是一个 `@property`：[DutInfo.py:61-66](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/DutInfo.py#L61-L66) 在 `DUTLIB` 存在时返回其值，否则返回 `"work"`。

它在 `LibraryDeclarations` 里被消费：[DutInfo.py:82-90](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/DutInfo.py#L82-L90)。关键两行——[DutInfo.py:85](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/DutInfo.py#L85) 把 `library work;` 里的 `work` 替换为 `dutLibrary`，[DutInfo.py:88](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/DutInfo.py#L88) 把 `use work.elem.obj;` 里的 `work` 同样替换。

DUT 实例化行在生成器里：[TbGen.py:35](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L35) 写出 `i_dut : entity {dutLibrary}.{name}`。

#### 4.2.4 代码实践

**实践目标**：体会 `DUTLIB` 是「替换」而非「新增声明」。

**操作步骤**：

1. 复制 `example/simpleTb/psi_common_async_fifo.vhd` 到一个临时文件。
2. 在文件级注释区加一行：`-- $$ DUTLIB=psi_lib $$`。
3. 运行生成（见 4.3.4 或 u1-l3 的命令）。
4. 打开生成的 `*_tb.vhd`，查看 Libraries 段与 DUT Instantiation 段。

**需要观察的现象**：

- 原本 DUT 里的 `library work;` / `use work.psi_common_logic_pkg.all;` 在生成物里变成 `library psi_lib;` / `use psi_lib.psi_common_logic_pkg.all;`。
- DUT 实例化行变成 `i_dut : entity psi_lib.psi_common_async_fifo`。
- 不会多出一条新的 `library psi_lib;`（它就是由原来的 `library work;` 替换而来）。

**预期结果**：库声明数量不变，只是 `work` 字样被统一替换为 `psi_lib`。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：如果 DUT 源码里根本没有 `library work;`（只有 `library ieee;`），加了 `DUTLIB=psi_lib` 后，生成物里会出现 `library psi_lib;` 吗？

**参考答案**：不会。`LibraryDeclarations` 只遍历 DUT 里实际存在的库声明并对 `work` 做替换；没有 `work` 就没有替换目标。但 DUT 实例化行仍会写成 `entity psi_lib.{name}`——这会导致生成物引用了一个未声明的库，是一个潜在的不一致，使用时要注意 DUT 源码里至少要有一条 `library work;`。

**练习 2**：为什么 `dutLibrary` 用 `@property` 而不是在 `__init__` 里直接存成普通属性？

**参考答案**：因为它只是 `fileScopeTags["dutlib"]` 的一个带默认值的视图，用 property 可以避免重复存储、保持「单一真相源」；任何时候 `fileScopeTags` 变了（虽然实际不会变），`dutLibrary` 都能反映最新值。

---

### 4.3 TbInfo.__init__：标签到生成参数的建模

#### 4.3.1 概念说明

`fileScopeTags` 只是「原始键值」，生成器并不能直接用它们。例如 `PROCESSES=Input,Output` 在字典里是 `{"processes": ["Input", "Output"]}`，但生成器需要的是一个明确的 `tbProcesses` 列表、一个布尔型的「是否多用例」开关、一个 testbench 名字。

`TbInfo` 的职责就是做这层**翻译**：读 `DutInfo`，把原始标签加工成一组「生成参数」。这一翻译几乎全部发生在 `TbInfo.__init__` 里。

`__init__` 产出的关键属性：

| 属性 | 来源标签 | 含义 |
| --- | --- | --- |
| `tbName` | 实体名（非标签） | testbench 实体名 = `{DUT实体名}_tb`。 |
| `tbProcesses` | `PROCESSES` | 测试过程名列表；缺失时默认 `["Stimuli"]`。 |
| `isMultiCaseTb` | `TESTCASES`（是否存在） | 是否为多用例模式。 |
| `testCases` | `TESTCASES` | 用例名列表；单用例模式下为 `None`。 |
| `tbUserPackages` | `TBPKG` | 额外用户包，按库分组的字典（见 4.4）。 |
| `dutInfo` | —— | 反向持有 `DutInfo`，供后续方法查询端口/generic。 |

#### 4.3.2 核心流程

`TbInfo.__init__` 的翻译流程（按代码顺序）：

```
1. isMultiCaseTb = ("testcases" 是否在 fileScopeTags 中)
2. 若是多用例：testCases = fileScopeTags["testcases"]，并归一成 list
   否则：testCases = None
3. tbName = DUT实体名 + "_tb"
4. tbProcesses = fileScopeTags["processes"]（若有），并归一成 list；
   否则默认 ["Stimuli"]
5. 解析 TBPKG → tbUserPackages（见 4.4）
6. self.dutInfo = info   # 反向引用
```

**归一化（normalization）**是这里的重点。前面 4.1 说过，`_ParseTags` 对单值返回 str、对列表返回 list。而下游（如 `for p in self.tbInfo.tbProcesses`）需要的是「可迭代的列表」。所以 `__init__` 对 `testCases`、`tbProcesses`、`tbPackages` 都做了同一个处理：

```python
if type(值) is str:
    值 = [值]      # 单值包成单元素列表
```

这样无论用户写 `PROCESSES=Stimuli` 还是 `PROCESSES=Input,Output`，下游拿到的都是一个 list。

**多用例模式开关**是另一个重点。`isMultiCaseTb` 只看 `TESTCASES` 键**是否存在**，不看其值。这意味着哪怕写 `TESTCASES=OnlyOne`（只有一个用例），也会进入多用例模式（生成 TB 包 + 一个 case 包）。多用例与单用例是「模式」的区别，不是「数量」的区别。

#### 4.3.3 源码精读

整个翻译在 `TbInfo.__init__`：[TbInfo.py:14-45](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbInfo.py#L14-L45)。分段看：

- 多用例开关与用例列表：[TbInfo.py:15-22](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbInfo.py#L15-L22)。第 15 行 `self.isMultiCaseTb = Tags.TESTCASES in info.fileScopeTags` 只判存在性；第 19-20 行把单值 str 归一成 list。
- testbench 名字：[TbInfo.py:24](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbInfo.py#L24)，`tbName = info.name + "_tb"`。
- 过程列表与默认值：[TbInfo.py:26-30](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbInfo.py#L26-L30)。第 26 行先给默认值 `["Stimuli"]`，第 27-30 行在有 `PROCESSES` 标签时覆盖并归一。
- 反向引用：[TbInfo.py:45](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbInfo.py#L45)，`self.dutInfo = info`。

这些属性随后被生成器多处消费：

- `tbName` 决定输出文件名：[TbGen.py:228](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L228) 写出 `{tbPath}/{tbName}{extension}`。
- `tbProcesses` 驱动测试过程生成：[TbGen.py:92](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L92) `for p in self.tbInfo.tbProcesses`，每个过程生成一个 `p_{p}` 进程。
- `isMultiCaseTb` 在 `Generate` 里决定是否额外生成 TB 包/case 包：[TbGen.py:233-235](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L233-L235) 写额外的包声明，[TbGen.py:256-260](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L256-L260) 调 `WriteTbPkg` 并对每个 case 调 `WriteCasePkg`。
- `isMultiCaseTb` 还改变进程内部逻辑：[TbGen.py:96-104](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L96-L104) 的多用例分支按 `NextCase` 调度各 case，对照单用例分支 [TbGen.py:105-116](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L105-L116)；testbench 控制进程里的多用例调度见 [TbGen.py:130-134](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L130-L134)。

**生成文件数量**因此可精确推算。单用例模式只生成 1 个文件（`{tbName}.vhd`）。多用例模式生成：

- 1 个主 testbench：`{tbName}.vhd`
- 1 个 TB 包：`{tbName}_pkg.vhd`（[MultiFileTb.py:14-15](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/MultiFileTb.py#L14-L15)）
- 每个用例 1 个 case 包：`{tbName}_case_{case}.vhd`（[MultiFileTb.py:60-61](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/MultiFileTb.py#L60-L61)）

即多用例模式总文件数 = 2 + 用例数。多用例示例有 2 个用例（`Full`、`Empty`），故生成 4 个文件。

#### 4.3.4 代码实践

**实践目标**：通过增删 `PROCESSES` 与 `TESTCASES`，观察 `isMultiCaseTb`、`tbProcesses` 取值与生成文件数量的变化。这是本讲的主实践。

**操作步骤**：

1. **基线（多用例）**：在项目根目录运行多用例示例（Windows 下 `run.bat`，或等价命令）：

   ```
   py TbGen.py -src example/multiCaseTb/psi_common_async_fifo.vhd -dst example/multiCaseTb/tb -clear -force
   ```

   列出 `example/multiCaseTb/tb/` 下的所有文件。

2. **改单用例**：把示例 VHDL 文件级注释里的 `-- $$ TESTCASES=Full,Empty $$` 删掉（或注释掉），再次运行生成，重新列目录。

3. **Python 内省（不依赖生成）**：在两种配置下分别执行下面的示例代码，打印关键属性：

   ```python
   # 示例代码
   from DutInfo import DutInfo
   from TbInfo import TbInfo
   di = DutInfo("example/multiCaseTb/psi_common_async_fifo.vhd")
   ti = TbInfo(di)
   print("tbName        =", ti.tbName)
   print("tbProcesses   =", ti.tbProcesses)
   print("isMultiCaseTb =", ti.isMultiCaseTb)
   print("testCases     =", ti.testCases)
   ```

**需要观察的现象**：

| 配置 | `isMultiCaseTb` | `tbProcesses` | `testCases` | 生成文件数 |
| --- | --- | --- | --- | --- |
| 有 `TESTCASES=Full,Empty` | `True` | `['Input', 'Output']` | `['Full', 'Empty']` | 4（主 TB + TB 包 + 2 个 case 包） |
| 删除 `TESTCASES` | `False` | `['Input', 'Output']` | `None` | 1（仅主 TB） |
| 再删除 `PROCESSES` | `False` | `['Stimuli']` | `None` | 1（仅主 TB，过程名变成 `Stimuli`） |

**预期结果**：删除 `TESTCASES` 后，`tb/` 目录里只剩一个 `psi_common_async_fifo_tb.vhd`，不再有 `_pkg.vhd` 与 `_case_*.vhd`；主 TB 里的进程也不再按 `NextCase` 调度，而是回到单用例的「等待复位释放后插一行 assert」骨架。**待本地验证**（删改示例前先备份）。

#### 4.3.5 小练习与答案

**练习 1**：如果只写 `-- $$ TESTCASES=OnlyOne $$`（只有一个用例、没有逗号），`isMultiCaseTb` 与 `testCases` 分别是什么？会生成几个文件？

**参考答案**：`isMultiCaseTb = True`（只要键存在即为真），`testCases = ["OnlyOne"]`（单值 str 被第 19-20 行归一成单元素列表）。会生成 3 个文件：主 TB + TB 包 + 1 个 case 包。这印证了「多用例是模式开关，不是数量判断」。

**练习 2**：`tbProcesses` 的默认值为什么是 `["Stimuli"]` 而不是空列表？

**参考答案**：因为即便用户不写 `PROCESSES`，testbench 也至少要有一个测试过程来放用户代码；`Stimuli`（激励）是约定俗成的默认过程名。若默认为空列表，生成器会在 `for p in tbProcesses` 里一个进程都不生成，testbench 就失去了承载用户代码的地方。

**练习 3**：`tbName` 是依据标签还是依据实体名生成的？

**参考答案**：依据实体名。`tbName = info.name + "_tb"`，`info.name` 来自 `VhdlFile` 解析出的 `entity.name`，与任何标签无关。目前没有标签能覆盖 testbench 的命名。

---

### 4.4 tbUserPackages：TBPKG 标签注入额外 use 包

#### 4.4.1 概念说明

有时 testbench 需要使用 DUT 本身不引用的额外 VHDL 包——比如一个全工程共用的测试工具包 `tb_lib.psi_tb_pkg`。DUT 源码里没有它的 `use` 语句，所以 `LibraryDeclarations` 不会把它带进来。

`TBPKG` 标签就是为了让用户显式声明：「除了 DUT 自带的库，请在 testbench 顶部再帮我 `use` 这些包」。它的值是 `库.包` 形式的列表，如 `TBPKG=tb_lib.psi_tb_pkg,clk_lib.clk_pkg`。

#### 4.4.2 核心流程

`TBPKG` 的处理在 `TbInfo.__init__` 里分两步：

1. **归一化**：和 `PROCESSES`/`TESTCASES` 一样，先保证 `tbPackages` 是 list。
2. **按库分组**：把每个 `lib.pkg` 字符串用 `.` 拆成 `(lib, pkg)`，按 `lib` 聚合到字典 `tbUserPackages`，结构为 `{lib: [pkg1, pkg2, ...]}`。

消费发生在 `TbInfo.UserPkgDelcaration`（注意源码里这个方法名有拼写错误 `Delcaration`，但它是真实存在的方法名，使用时需照抄）：对每个库写一条 `library {lib};`，缩进后对每个包写一条 `use {lib}.{pkg}.all;`。

#### 4.4.3 源码精读

`tbUserPackages` 的构建：[TbInfo.py:32-43](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbInfo.py#L32-L43)。第 33-36 行读 `TBPKG` 并归一成 list；第 37-43 行遍历每个 `lib.pkg`，用 `pkg.split(".")` 拆成两段，按库聚合进字典。

注意第 39 行 `lib, pkgName = tuple(pkg.split("."))`：它假设每个值**恰好有一个点**。如果写成 `TBPKG=onlyname`（无点）或 `TBPKG=a.b.c`（多点），`split(".")` 的结果长度不是 2，元组解包会抛 `ValueError`。这是一个隐含的格式约束。

`UserPkgDelcaration` 把字典写回 VHDL：[TbInfo.py:50-55](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbInfo.py#L50-L55)。它在外层写 `library {lib};`，缩进后对每个包写 `use {lib}.{pkg}.all;`。

该方法在主生成流程里被调用：[TbGen.py:232](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L232)（在库声明之后、实体声明之前）。因此 `TBPKG` 注入的包会出现在 testbench 的顶部库声明区。

对照另外两个「包声明」方法（仅在多用例模式下使用）：

- `TbPkgDeclaration`：声明生成器自己产出的 TB 包 `work.{tbName}_pkg`：[TbInfo.py:57-60](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbInfo.py#L57-L60)。
- `TbCaseDeclaration`：声明每个 case 包 `work.{tbName}_case_{case}`：[TbInfo.py:62-66](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbInfo.py#L62-L66)。

这三者的区别：`tbUserPackages` 是**用户外部**包，`TbPkg`/`TbCase` 是**工具自己生成**的内部包。

#### 4.4.4 代码实践

**实践目标**：体会 `TBPKG` 如何按库分组并展开成 `library`/`use` 语句。

**操作步骤**：

1. 在示例 VHDL 文件级注释区加一行（包含两个不同库的包）：

   ```
   -- $$ TBPKG=tb_lib.psi_tb_pkg,clk_lib.clk_pkg $$
   ```

2. 运行 Python 内省（示例代码）：

   ```python
   from DutInfo import DutInfo
   from TbInfo import TbInfo
   di = DutInfo("example/multiCaseTb/psi_common_async_fifo.vhd")
   ti = TbInfo(di)
   print("tbUserPackages =", ti.tbUserPackages)
   ```

3. 重新生成 testbench，查看生成物顶部的库声明区。

**需要观察的现象**：

- `tbUserPackages` 是按库分组的字典：`{'tb_lib': ['psi_tb_pkg'], 'clk_lib': ['clk_pkg']}`。
- 生成物里出现：

  ```
  library tb_lib;
      use tb_lib.psi_tb_pkg.all;
  library clk_lib;
      use clk_lib.clk_pkg.all;
  ```

**预期结果**：每个库一条 `library` 声明，其下缩进一条或多条 `use ... all;`。若把两个包改成同库（如 `TBPKG=tb_lib.pkg_a,tb_lib.pkg_b`），它们会被聚合到同一个 `library tb_lib;` 下。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`TBPKG=tb_lib.psi_tb_pkg,clk_lib.clk_pkg` 解析后，`tbUserPackages` 的结构是什么？为什么是字典而不是列表？

**参考答案**：`{'tb_lib': ['psi_tb_pkg'], 'clk_lib': ['clk_pkg']}`。用字典是为了按库聚合：同一个库下的多个包应共享一条 `library X;` 声明，只在下面并列多条 `use`，这样生成的 VHDL 更规范。

**练习 2**：如果误写 `TBPKG=psi_tb_pkg`（漏掉 `lib.` 前缀），会发生什么？

**参考答案**：`pkg.split(".")` 返回 `['psi_tb_pkg']`（长度 1），第 39 行的 `tuple(...)` 解包成两个变量会抛 `ValueError`，整个 `TbInfo` 构造失败，进而 `ReadHdl` 抛异常。这表明 `TBPKG` 的值必须严格是 `库.包` 格式。

---

## 5. 综合实践

把四个文件级标签串起来，完成下面这个贯穿本讲的小任务：

**场景**：你要为一个编译到 `psi_lib` 库的 DUT 生成 testbench，测试分 `Input`/`Output` 两个过程，需要覆盖 `Full`/`Empty` 两个用例，并且 testbench 要额外使用 `tb_lib.psi_tb_pkg` 工具包。

**任务**：

1. 复制 `example/simpleTb/psi_common_async_fifo.vhd` 为 `my_dut.vhd`（放到一个新目录，避免污染示例）。
2. 在其文件级注释区补齐四条标签，使其满足上述场景：

   ```
   -- $$ PROCESSES=Input,Output $$
   -- $$ TESTCASES=Full,Empty $$
   -- $$ DUTLIB=psi_lib $$
   -- $$ TBPKG=tb_lib.psi_tb_pkg $$
   ```

3. 先用 Python 内省（4.1.4 的示例代码 + `TbInfo`）**预测**下列值，再实际运行验证：
   - `fileScopeTags` 的全部键值
   - `dutLibrary`、`tbName`、`tbProcesses`、`isMultiCaseTb`、`testCases`、`tbUserPackages`
   - 生成文件的数量与文件名
4. 运行 `py TbGen.py -src my_dut.vhd -dst ./tb -clear -force`，核对：
   - 库声明区 `work` 是否都被替换为 `psi_lib`，并出现 `library tb_lib;` / `use tb_lib.psi_tb_pkg.all;`。
   - DUT 实例化行是否为 `entity psi_lib.psi_common_async_fifo`。
   - `tb/` 下是否有 4 个文件：`psi_common_async_fifo_tb.vhd`、`psi_common_async_fifo_tb_pkg.vhd`、`psi_common_async_fifo_tb_case_Full.vhd`、`psi_common_async_fifo_tb_case_Empty.vhd`。

**验收标准**：你的预测与实际输出一致；能逐条解释每个文件级标签分别导致了哪一处生成差异。运行结果**待本地验证**。

## 6. 本讲小结

- **文件级标签**写在独立注释行里，描述整个 testbench 的骨架，共四个：`PROCESSES`、`TESTCASES`、`DUTLIB`、`TBPKG`。
- `DutInfo.__init__` 遍历 `VhdlFile.commentLines`，逐行调 `_ParseTags`，用 `update` 合并成 `self.fileScopeTags` 字典；同名标签后写覆盖先写，列表必须写在一行用逗号分隔。
- `dutLibrary` 是 `DUTLIB` 的带默认值（`"work"`）视图，被 `LibraryDeclarations` 与 `_DutInstantiation` 共同消费，作用是**替换** `work`，而非新增 `library` 声明。
- `TbInfo.__init__` 把原始标签翻译成生成参数：`tbName = 实体名 + "_tb"`；`tbProcesses` 来自 `PROCESSES`，缺失默认 `["Stimuli"]`；`isMultiCaseTb` 只看 `TESTCASES` 是否存在；`testCases`/`tbProcesses` 都做了 str→list 的归一化。
- `TBPKG` 被拆成 `库.包` 并按库聚合进 `tbUserPackages` 字典，再由 `UserPkgDelcaration` 展开成 `library`/`use` 语句注入 testbench 顶部。
- 多用例模式（存在 `TESTCASES`）会额外生成 1 个 TB 包 + 每用例 1 个 case 包，总文件数 = 2 + 用例数；单用例模式只生成 1 个主 TB 文件。

## 7. 下一步学习建议

- 本讲只解释了 `isMultiCaseTb`/`testCases` **如何被设置**，但多用例模式下进程内部如何按 `NextCase` 调度各 case、case 包里的 `procedure` 签名如何生成，留待 **u5-l1（多用例 TB 的触发与生成流程）** 与 **u5-l2（WriteTbPkg / WriteCasePkg 与过程方向）** 详细展开。
- 如果你想了解 `fileScopeTags` 背后的 pyparsing 文法细节（`scanString`、`Word(alphas)` 等），可回顾 **u2-l1** 与 **u3-l1**。
- 下一单元（u3）将进入 VHDL 源码解析层，讲解 `VhdlFile` 如何用 pyparsing 读出 entity、use 语句与注释行——也就是本讲里 `commentLines`/`usestatements` 的真正源头。
