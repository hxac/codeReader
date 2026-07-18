# 标签语法与解析原理

## 1. 本讲目标

本讲是「VHDL 注解标签系统」单元的第一讲。学完后你应当能够：

- 看懂 DUT 源码里 `$$ ... $$` 注解标签的两种写法（单值与列表），并知道标签名大小写不敏感。
- 说清 `DutInfo._ParseTags` 是如何用 pyparsing 把一段 VHDL 注释扫描成一个 Python 字典的。
- 熟练使用 `HasTag` / `GetTag` / `GetTagAsList` / `HastTagValue` / `FilterForTag` 这一组工具方法，并理解它们在生成器（`TbGenerator`）里的真实调用方式。

本讲只讲「标签是什么、怎么解析、怎么查询」，**不**讲每个具体标签（`TYPE`/`FREQ`/`PROC`/`EXPORT` 等）如何左右生成结果——那是下一讲（u2-l2）的主题。

## 2. 前置知识

在学习本讲前，你需要先建立以下几个直觉（来自 u1-l1、u1-l2、u1-l3）：

- **DUT 与 Testbench**：DUT（Design Under Test，被测设计）是一段 VHDL 实体；Testbench（测试台）是用来驱动 DUT、观察输出的 VHDL 代码。TbGenerator 的工作就是「读 DUT，写 Testbench 骨架」。
- **`$$ ... $$` 标签**：TbGenerator 不去猜 DUT 的意图，而是要求设计者在 DUT 源码的 VHDL **注释**里写下形如 `$$ TYPE=CLK; FREQ=100e6 $$` 的「注解标签」，用来告诉生成器「这个端口是时钟、频率多少」。标签是整个工具的**输入契约**。
- **pyparsing**：一个流行的 Python 解析库，用「组合小的匹配规则」来描述文法。本讲会用到的构件包括 `Word`、`Literal`、`CharsNotIn`、`OneOrMore`、`Optional`、`Group`、`scanString`。
- **数据流全景**（本讲会反复用到）：

```
VHDL 源码（含 -- 注释）
      │  VhdlFile 解析
      ▼
port.comment / generic.comment   ←── 行尾注释（端口、generic 级标签）
commentLines[]                   ←── 独立注释行（文件级标签）
      │  DutInfo 读取
      ▼
fileScopeTags（字典） + 按需调用 HasTag/GetTag/FilterForTag
      │  TbGenerator 生成
      ▼
testbench 骨架 *.vhd
```

一句话：**标签住在 VHDL 注释里，`VhdlFile` 把注释拆下来挂到端口/generic 或文件上，`DutInfo` 再用 pyparsing 把 `$$...$$` 翻译成字典供生成器查询。**

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
|------|------|-----------|
| [DutInfo.py](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/DutInfo.py) | DUT 数据模型 | `Tags` 常量、`_ParseTags` 文法、`HasTag/GetTag/...` 工具方法 |
| [VhdlParse.py](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/VhdlParse.py) | VHDL 解析（pyparsing） | `PP_COMMENT`、`VhdlCommentLine`、端口/generic 如何挂载 `.comment` |
| [TbGen.py](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py) | 生成器主类 | 在 `_Clocks`/`_Resets`/`_DutInstantiation` 中如何消费标签 |
| [example/simpleTb/psi_common_async_fifo.vhd](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/example/simpleTb/psi_common_async_fifo.vhd) | 示例 DUT | 大量真实的 `$$...$$` 标签样例 |

## 4. 核心概念与源码讲解

### 4.1 Tags 常量类与 `$$` 标签语法

#### 4.1.1 概念说明

TbGenerator 的所有「可识别标签」都在一个叫 `Tags` 的类里集中声明。它的本质非常简单：**一组小写字符串常量**，每个常量是一个标签名。这样做有两个好处：

1. **避免拼写错误**：代码里写 `Tags.FREQ` 而不是裸字符串 `"freq"`，IDE 能帮你检查。
2. **作为「合法标签」的清单**：看一眼 `Tags` 类就知道工具认得哪些标签。

标签在 VHDL 里的外观是写在注释中的 `$$ ... $$`，例如：

```vhdl
InClk : in std_logic;  -- $$ TYPE=CLK; FREQ=100e6; PROC=Input $$
```

这里有三个要点：

- `$$` 和 `$$` 是标签块的**定界符**，一块里可以放多个 `键=值`，用 `;` 分隔。
- **值有两种形态**：单值（`FREQ=100e6`）和列表（`PROCESSES=Input,Output`，用逗号分隔）。
- **标签名大小写不敏感**：`PROC`、`Proc`、`proc` 是同一个标签；但值的原始大小写会被**保留**。

#### 4.1.2 核心流程

`Tags` 类把标签按「作用对象」分成三组：

```
Generic 标签   ──  作用于 generic（如 EXPORT、CONSTANT）
Port 标签      ──  作用于端口  （如 TYPE、FREQ、CLK、PROC、LOWACTIVE）
File-scope 标签 ── 作用于整个文件（如 PROCESSES、TESTCASES、DUTLIB、TBPKG）
```

一条标签从「写在哪」到「被谁读取」的对应关系：

| 标签写在哪 | 由谁解析 | 进入哪里 |
|-----------|---------|---------|
| 端口/generic 行尾注释 | `VhdlPortDeclaration` / `VhdlGenericDeclaration` 捕获 `.comment` | 生成时按端口/generic 查询 |
| 独立的 `-- ...` 整行注释 | `VhdlCommentLine` 进入 `commentLines[]` | `DutInfo.__init__` 汇总进 `fileScopeTags` |

#### 4.1.3 源码精读

`Tags` 类的全部定义（注意所有值都是小写字符串）：

```python
#Tags in the form $$ BLA=5; BLUBB=1,2,3 $$
class Tags:
    #Generic Tags
    EXPORT = "export"
    CONSTANT = "constant"
    #Port Tags
    LOWACTIVE = "lowactive"
    TYPE = "type"      #CLK, RST, SIG
    CLK = "clk"
    FREQ = "freq"
    PROC = "proc"
    #File scope tags
    PROCESSES = "processes"
    TESTCASES = "testcases"
    DUTLIB = "dutlib"
    TBPKG = "tbpkg"
```

> 见 [DutInfo.py:15-32](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/DutInfo.py#L15-L32)。顶部注释 `$$ BLA=5; BLUBB=1,2,3 $$` 正是本讲要讲的两种值形态的缩影：`BLA=5` 是单值，`BLUBB=1,2,3` 是列表。

来看示例 DUT 里的真实标签。文件级标签（独立注释行）定义了测试过程名：

```vhdl
-- $$ PROCESSES=Input,Output $$
```

> 见 [example/simpleTb/psi_common_async_fifo.vhd:23](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/example/simpleTb/psi_common_async_fifo.vhd#L23)。`PROCESSES=Input,Output` 是列表值，意味着这个 TB 有 `Input`、`Output` 两个测试过程。

generic 行尾标签（注意 `AlmFullOn_g` 那行用了列表值 `EXPORT=false,funky=blubb`，而 `funky` 是一个工具并不识别的「杂项」标签，仅用于演示解析健壮性）：

```vhdl
Width_g        : positive := 16;    -- $$ EXPORT=true $$
Depth_g        : positive := 32;    -- $$ EXPORT=true; funky=bla $$
AlmFullOn_g    : boolean  := false; -- $$ EXPORT=false,funky=blubb $$
AlmFullLevel_g : natural  := 28;    --$$CONSTANT=12$$
```

> 见 [example/simpleTb/psi_common_async_fifo.vhd:30-33](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/example/simpleTb/psi_common_async_fifo.vhd#L30-L33)。注意 `AlmFullLevel_g` 那行 `--$$CONSTANT=12$$` **没有任何空格**——这说明定界符 `$$` 本身才是关键，空格可有可无。

端口行尾标签（同一块里放多个 `键=值`）：

```vhdl
InClk : in std_logic;  -- $$ TYPE=CLK; FREQ=100e6; PROC=Input $$
OutClk : in std_logic; -- $$ TYPE=CLK; FREQ=125e6; Proc=Output $$
```

> 见 [example/simpleTb/psi_common_async_fifo.vhd:39-41](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/example/simpleTb/psi_common_async_fifo.vhd#L39-L41)。注意 `InClk` 写 `PROC=Input`、`OutClk` 写 `Proc=Output`——大小写不同却是同一个 `proc` 标签，这正是「标签名大小写不敏感」的体现。

#### 4.1.4 代码实践

**实践目标**：建立「标签 → 作用对象 → Tags 常量」的直觉。

**操作步骤**：

1. 打开 [example/simpleTb/psi_common_async_fifo.vhd](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/example/simpleTb/psi_common_async_fifo.vhd)。
2. 搜索 `$$`，把所有标签块找出来。
3. 对每个标签块，判断它属于 generic、port 还是 file-scope（看它写在行尾还是独立行）。
4. 把每个标签名映射到 `Tags` 类里的常量（如 `TYPE` → `Tags.TYPE`）。

**需要观察的现象**：

- 同一个标签名（如 `PROC`）出现在多个端口上，且大小写不一（`PROC`、`Proc`、`proc`）。
- 有些值是单值（`FREQ=100e6`），有些是列表（`PROC=Output,Input`）。

**预期结果**：你能列出一张表，形如「`InClk` 端口 / `TYPE=CLK`, `FREQ=100e6`, `PROC=Input` / Port 标签」。如果你无法确定某个标签的归类，标注「待确认」。

#### 4.1.5 小练习与答案

**练习 1**：`Tags` 类里 `TYPE = "type"` 后面注释了 `#CLK, RST, SIG`。这表示 `TYPE` 标签可能取哪些值？这些值是标签名还是标签值？

> **答案**：`CLK`、`RST`、`SIG` 是 `TYPE` 这个标签的**值**（即 `TYPE=CLK`），不是新的标签名。它们用来告诉生成器这个端口的角色（时钟、复位、普通信号）。

**练习 2**：示例里 `funky=bla` 和 `funky=blubb` 会被解析吗？会触发生成行为吗？

> **答案**：会被 `_ParseTags` 解析进字典（键为 `"funky"`），但 `Tags` 类里没有 `funky` 常量，生成器代码里也没有任何地方查询 `funky`，所以它被解析却不被使用——不会触发任何生成行为。这正是「解析层与使用层分离」的体现。

---

### 4.2 `_ParseTags`：用 pyparsing 把注释变成字典

#### 4.2.1 概念说明

`_ParseTags` 是整个标签系统的「翻译器」：输入是一段**注释字符串**（已经剥掉了 `--`），输出是一个 Python 字典。它解决两个问题：

1. **定位**：注释里可能混着普通文字（如 `not empty`），需要从中精确捞出 `$$ ... $$` 块。
2. **结构化**：把 `键=值; 键=值` 翻译成 `{键: 值}`，并区分单值与列表。

它是 `DutInfo` 的一个 **类方法**（`@classmethod`），所以你可以直接 `DutInfo._ParseTags("$$ ... $$")` 调用它，不需要先实例化 `DutInfo`。

#### 4.2.2 核心流程

`_ParseTags` 的执行流程：

```
输入: string（一段注释文本）
  │
  ├── 1. 定义「单值」规则 SINGLE_VALUE：匹配除 ; 和 $ 外的任意字符
  ├── 2. 定义「列表」规则 LIST_VALUE：一个或多个「词 + ,」、最后再一个词
  ├── 3. ANY_VALUE = LIST_VALUE 优先，否则 SINGLE_VALUE（pyparsing 的 | 是有序选择）
  ├── 4. TAGS = "$$" + 一个或多个(标签名 "=" 值 可选";") + "$$"
  ├── 5. scanString 扫描整个 string，逐个命中 $$...$$ 块
  └── 6. 对每个标签：listVal→用逗号还原成列表；singleVal→strip 去空白；
            键统一 lower()（小写）
输出: dict，例如 {"type": "CLK", "freq": "100e6", "proc": "Input"}
```

关键设计点：

- **键小写、值保留原样**：`tag.get("tag").lower()` 只对键做小写。所以 `TYPE=CLK` 解析后是 `{"type": "CLK"}`——键变小写、值仍是 `CLK`。
- **列表 vs 单值的判定**：值的解析用 `LIST_VALUE | SINGLE_VALUE`。只要值里出现「词,词,...」的结构，就会被判成列表；否则是单值字符串。
- **大小写不敏感在查询层兜底**：值的大小写不敏感并不是在 `_ParseTags` 里做的，而是在 `HastTagValue`/`FilterForTag` 查询时把两边都 `lower()` 再比较（见 4.3）。

#### 4.2.3 源码精读

完整方法（逐行标注）：

```python
@classmethod
def _ParseTags(cls, string : str) -> dict:
    SINGLE_VALUE = pp.CharsNotIn(";$")                       # ① 单值：非 ; 非 $ 的任意字符
    LIST_VALUE = pp.OneOrMore(                               # ② 列表：若干「词,」+ 末尾一个词
                    pp.Word(pp.alphanums + "_.") + pp.Literal(",")) \
                + pp.Word(pp.alphanums + "_.")
    ANY_VALUE = pp.Group(LIST_VALUE("listVal") | SINGLE_VALUE("singleVal"))  # ③ 先试列表，再试单值
    TAGS = "$$" + pp.OneOrMore(                              # ④ $$ + 一个或多个「名=值;」
                pp.Group(pp.Word(pp.alphas)("tag") + "=" + ANY_VALUE("value") + pp.Optional(";"))
           )("tags") + "$$"

    tags = {}
    for t, s, e in TAGS.scanString(string):                  # ⑤ 扫描整段注释，逐块命中
        for tag in t.get("tags"):
            val = tag.get("value")
            if "listVal" in val.keys():                      # ⑥ 列表：拼回字符串再按逗号切
                val = "".join(val).split(",")
            elif "singleVal" in val.keys():                  #    单值：去首尾空白
                val = val.get("singleVal").strip()
            else:
                raise Exception("Illegal Tag Format")
            tags[tag.get("tag").lower()] = val               # ⑦ 键统一小写
    return tags
```

> 见 [DutInfo.py:93-112](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/DutInfo.py#L93-L112)。

几个 pyparsing 构件的直觉解释：

| 构件 | 含义 |
|------|------|
| `pp.CharsNotIn(";$")` | 贪心匹配「不是 `;` 也不是 `$`」的连续字符，用来吞下单值（如 `100e6`、`true`）。 |
| `pp.Word(pp.alphas)` | 只由**字母**组成的词——这就是**标签名只能用字母**的根源（`TYPE` 可以，`TYPE2` 不行）。 |
| `pp.Word(pp.alphanums + "_.")` | 由字母、数字、下划线、点组成的词，用来匹配列表元素（如 `Input`、`work.psi_common_xxx`）。 |
| `pp.Literal(",")` | 精确匹配一个逗号。 |
| `A("name")` | 给匹配结果取名（`setResultsName` 的简写），之后用 `t.get("name")` 取出。 |
| `pp.Group(expr)` | 把匹配结果包成一个子结构（`ParseResults`），方便整体取用。 |
| `A | B` | 有序选择：先试 A，A 不行再试 B。 |
| `TAGS.scanString(string)` | 在整段字符串里**扫描**所有匹配，逐个返回 `(tokens, start, end)`。 |

注意 ④ 处 `pp.Word(pp.alphas)("tag")`：标签名只能由字母构成。这是一个隐含约束——如果你想自定义一个名为 `INIT0` 的标签，名字里的数字会让它在 ④ 处根本匹配不上，从而被静默忽略。

注释是怎么变成 `string` 参数传进来的？看 VHDL 解析侧。`PP_COMMENT` 抓取 `--` 及其后整行，并以 `text` 命名：

```python
PP_COMMENT = pp.Group(pp.Literal("--") + pp.restOfLine("text"))
```

> 见 [VhdlParse.py:28](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/VhdlParse.py#L28)。`restOfLine` 匹配 `--` 之后到行尾的全部内容，所以 `.comment` 里**不包含** `--` 本身，但**包含** `$$...$$`。

端口和 generic 声明文法末尾用 `pp.Optional(PP_COMMENT("comment"))` 把行尾注释挂到对象上：

```python
# VhdlPortDeclaration.PP_DEFINITION 末尾:
... + pp.Optional(":=" + PP_EXPRESSION("default")) + pp.Optional(";") + pp.Optional(PP_COMMENT("comment"))
```

> 见 [VhdlParse.py:136-148](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/VhdlParse.py#L136-L148)（端口）与 [VhdlParse.py:123-134](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/VhdlParse.py#L123-L134)（generic）。在 `_Parse` 里 `self.comment = parts.get("comment").get("text")`，把注释文本存为 `.comment` 属性。

独立的整行注释（文件级标签）由 `VhdlCommentLine` 捕获，`VhdlFile` 把它们收集进 `commentLines`：

```python
class VhdlCommentLine(VhdlConstruct):
    PP_DEFINITION = pp.lineStart + PP_COMMENT
    def _Parse(self, parts):
        self.comment = parts[0].get("text")
```

> 见 [VhdlCommentLine](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/VhdlParse.py#L67-L72) 与 `VhdlFile` 里的收集循环 [VhdlParse.py:188-191](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/VhdlParse.py#L188-L191)。

最后看 `DutInfo.__init__` 如何把文件级注释汇总成 `fileScopeTags`：

```python
self.fileScopeTags = {}
for c in self.parseInfo.commentLines:
    tags = self._ParseTags(c.comment)      # 每行独立解析
    self.fileScopeTags.update(tags)        # 合并进同一个字典
```

> 见 [DutInfo.py:48-51](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/DutInfo.py#L48-L51)。注意：所有独立注释行都会被扫描，普通说明文字（无 `$$`）会被 `_ParseTags` 解析成空字典 `{}`，`update({})` 不影响结果——所以普通注释和标签可以安全混排。

#### 4.2.4 代码实践

**实践目标**：手工模拟 `_ParseTags`，验证你对文法的理解。

**操作步骤**：对下面两个注释字符串，**先合上代码**手写出 `_ParseTags` 应返回的字典，再对照 4.2.3 的规则核对。

1. `"$$ TYPE=RST; CLK=InClk $$"`
2. `"$$ PROC=Output,Input $$"`

**需要观察的现象**：

- 第 1 个里两个标签都是单值，键被小写。
- 第 2 个里 `Output,Input` 是列表值，结果应是 Python 列表。

**预期结果**（你可以此自检）：

1. `{"type": "RST", "clk": "InClk"}`
2. `{"proc": ["Output", "Input"]}`

> 这两个例子分别来自示例 DUT 的 [InRst 端口（第 40 行）](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/example/simpleTb/psi_common_async_fifo.vhd#L40) 与 [OutRdy 端口（第 52 行）](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/example/simpleTb/psi_common_async_fifo.vhd#L52)。注意 OutRdy 的 `PROC=Output,Input` 表示这个端口同时参与 `Output` 和 `Input` 两个过程——这正是「列表值」的真实用途。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `FREQ=100e6` 被解析成**单值字符串** `"100e6"`，而不是出问题？

> **答案**：`LIST_VALUE` 要求至少出现一个「词 + `,`」。`100e6` 后面是 `;` 不是 `,`，所以 `LIST_VALUE` 匹配失败，回退到 `SINGLE_VALUE`，后者用 `CharsNotIn(";$")` 匹配到 `100e6`（遇 `;` 停止），再 `strip()` 得到 `"100e6"`。

**练习 2**：如果有人写了 `$$ M2=foo $$`（标签名里含数字），`_ParseTags` 会怎样？

> **答案**：标签名规则是 `pp.Word(pp.alphas)`（仅字母）。`M2` 里的 `2` 不是字母，所以 `M` 之后标签名匹配就停在 `M`，随后期望的 `=` 对不上 `2`，整个 `$$...$$` 块匹配失败，`scanString` 不会命中它，结果是空字典 `{}`。这是一个容易踩的坑：**自定义标签名不要带数字**。待本地验证：可写一行 `print(DutInfo._ParseTags("$$ M2=foo $$"))` 确认返回 `{}`。

---

### 4.3 标签工具方法：`HasTag` / `GetTag` / `HastTagValue` / `GetTagAsList` / `FilterForTag`

#### 4.3.1 概念说明

`_ParseTags` 只是「把注释翻成字典」，但生成器代码里几乎不会直接调它——而是调这一层更高阶的**工具方法**。它们的共同特点：

- 都是 `@classmethod`，调用形式如 `DutInfo.HasTag(port, Tags.FREQ)`。
- 第一个参数 `object` 是一个**端口或 generic 声明对象**，方法内部去读它的 `.comment`。
- 它们在内部**再次调用** `_ParseTags(object.comment)`，所以每次查询都会重新解析一次（这是简单直白的设计，标签量不大时无需缓存）。

> 小提示：方法名 `HastTagValue` 里 `Hast` 是一个拼写偏差（应为 `Has`），但它就是工具的正式接口，调用时照写 `DutInfo.HastTagValue(...)` 即可。

#### 4.3.2 核心流程

五个方法的分工：

```
HasTag(obj, tag)               → 该对象是否有这个标签？          返回 bool
GetTag(obj, tag)               → 取该标签的值；没有则抛异常       返回 str 或 list
GetTagAsList(obj, tag)         → 取值并保证是列表                返回 list
HastTagValue(obj, tag, value)  → 是否有该标签且值匹配？          返回 bool（默认大小写不敏感）
FilterForTag(list, tag, value) → 从一批对象里筛出有该标签的       返回 list
```

值匹配的大小写处理（关键）：

- **键**：在 `_ParseTags` 里已经小写化，工具方法入口又会 `tag.lower()`，所以传 `Tags.FREQ`（`"freq"`）或 `"FREQ"` 都一样。
- **值**：`HastTagValue` 和 `FilterForTag` 默认 `casesensitive=False`，会把**存档的值与期望值都 `lower()` 后再比较**，所以 `PROC=Input` 与查询 `"input"` 能匹配上。

#### 4.3.3 源码精读

**判定是否存在**——`HasTag`：

```python
@classmethod
def HasTag(cls, object, tag : str):
    tag = tag.lower()
    tags = cls._ParseTags(object.comment)
    if tag not in tags:
        return False
    return True
```

> 见 [DutInfo.py:125-131](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/DutInfo.py#L125-L131)。

**取值**——`GetTag`（不存在则抛异常，所以先 `HasTag` 守门）：

```python
@classmethod
def GetTag(cls, object, tag : str) -> str:
    if not cls.HasTag(object, tag):
        raise Exception("object {} has not tag {}".format(object.name, tag))
    tags = cls._ParseTags(object.comment)
    return tags[tag]
```

> 见 [DutInfo.py:133-138](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/DutInfo.py#L133-L138)。`_Clocks` 里正是先用 `HasTag` 检查 `FREQ`，缺失就抛清晰的错误信息。

**取值并归一为列表**——`GetTagAsList`（单值字符串会被包成单元素列表，省去调用方判断类型）：

```python
@classmethod
def GetTagAsList(cls, object, tag : str) -> List[str]:
    tagVal = cls.GetTag(object, tag)
    if type(tagVal) is str:
        return [tagVal]
    return tagVal
```

> 见 [DutInfo.py:140-145](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/DutInfo.py#L140-L145)。

**判断值是否匹配**——`HastTagValue`（注意默认大小写不敏感）：

```python
@classmethod
def HastTagValue(cls, object, tag : str, value : str, casesensitive : bool = False) -> bool:
    tag = tag.lower()
    tags = cls._ParseTags(object.comment)
    if tag not in tags:
        return False
    if casesensitive:
        return tags[tag] == value
    else:
        return tags[tag].lower() == value.lower()
```

> 见 [DutInfo.py:114-123](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/DutInfo.py#L114-L123)。这解释了为什么 `TYPE=CLK`、查询 `"clk"` 能匹配：两边都 `lower()` 后都是 `"clk"`。

**批量筛选**——`FilterForTag`（生成器里用得最多）。它遍历一组对象，留下含指定标签的；若给了 `value`，还要值匹配（同样支持列表值，如 `PROC=Output,Input` 里查 `"Input"` 也算命中）：

```python
@classmethod
def FilterForTag(cls, list, tag, value=None, casesensitive=False):
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
                    if value in tagValueList:        l.append(e)
                else:
                    if value.lower() in tagValueListLower:  l.append(e)
    return l
```

> 见 [DutInfo.py:147-166](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/DutInfo.py#L147-L166)。注意 `[tagValue] if type(tagValue) is str else tagValue` 这一行——它把单值也当成「长度为 1 的列表」来查，所以 `PROC=Input`（单值）和 `PROC=Output,Input`（列表）都能用同一套 `in` 逻辑判断。

**真实消费者**：这些方法在生成器里被大量调用。例如 `_Clocks` 用三连招「筛选 → 检查 → 取值」：

```python
for clk in DutInfo.FilterForTag(self.dutInfo.ports, Tags.TYPE, "clk"):   # 筛出所有时钟端口
    if not DutInfo.HasTag(clk, Tags.FREQ):                                # 必须有 FREQ
        raise Exception("Clock {} has not FREQ tag!".format(clk.name))
    ...
    f.WriteLn("constant Frequency_c : real := real({});".format(DutInfo.GetTag(clk, Tags.FREQ)))  # 取值
```

> 见 [TbGen.py:53-57](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L53-L57)。`_Resets` 的写法几乎一样，只是换成 `TYPE=rst` 并取 `CLK` 标签（[TbGen.py:70-73](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L70-L73)）。`_DutInstantiation` 用 `FilterForTag(generics, Tags.EXPORT, "true")` 决定哪些 generic 进 `generic map`（[TbGen.py:37](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L37)）。

#### 4.3.4 代码实践

**实践目标**：把「标签 → 查询结果」串起来，体会工具方法的返回类型。

**操作步骤**：基于示例 DUT 的真实标签，回答下列问题（假设 `ports` 已是 `dutInfo.ports`）：

1. `DutInfo.FilterForTag(ports, Tags.TYPE, "clk")` 会返回哪几个端口？
2. `DutInfo.HasTag(InClk_port, Tags.FREQ)` 返回什么？`DutInfo.GetTag(InClk_port, Tags.FREQ)` 呢？
3. `DutInfo.GetTagAsList(OutRdy_port, Tags.PROC)` 返回什么类型？

**需要观察的现象**：

- `FilterForTag` 返回的是**对象列表**（端口声明本身），不是字符串。
- `GetTagAsList` 对列表值返回多元素列表，对单值返回单元素列表。

**预期结果**：

1. 返回 `InClk` 与 `OutClk` 两个端口（它们都有 `TYPE=CLK`）。
2. `HasTag(...)` 返回 `True`；`GetTag(...)` 返回 `"100e6"`。
3. 返回 `["Output", "Input"]`（`OutRdy` 的 `PROC=Output,Input`，是列表值）。

> 若你尚未实例化 `DutInfo`，无法直接运行这几行，可标注「待本地验证」并改用 4.2 的纯字符串方式验证解析部分。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `GetTag` 在标签缺失时要抛异常，而不是返回 `None`？

> **答案**：这是一种「快速失败」设计。生成器里 `GetTag` 总是用在「该标签本应存在」的场合（前面已经 `FilterForTag` 或 `HasTag` 守过门）。如果走到 `GetTag` 还能缺标签，说明 DUT 源码写错了，应当立刻抛错让设计者修正，而不是返回 `None` 让 bug 静默扩散到生成的 VHDL 里。

**练习 2**：`OutRdy` 端口的 `PROC=Output,Input`，用 `FilterForTag(ports, Tags.PROC, "Input")` 能筛出它吗？为什么？

> **答案**：能。`FilterForTag` 在判断值时把标签值统一成列表（`tagValueList = [tagValue] if type(tagValue) is str else tagValue`），`PROC=Output,Input` 解析后本就是 `["Output", "Input"]`，`"Input"` 在其中，故命中。这正是一个端口同时参与多个过程时的查询基础。

---

## 5. 综合实践

把本讲三个模块串起来：**先肉眼预测，再用 Python 验证**。

### 5.1 任务

在 [example/simpleTb/psi_common_async_fifo.vhd](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/example/simpleTb/psi_common_async_fifo.vhd) 中挑出下面 5 处 `$$ ... $$` 标签，**先合上一切资料**，在纸上写出 `_ParseTags` 解析后应得到的字典；然后运行下面的脚本核对。

挑选的 5 处（覆盖单值、列表、多标签、无空格、大小写混合）：

| # | 出处（行号） | 注释原文 |
|---|------|---------|
| 1 | 第 23 行（文件级） | `$$ PROCESSES=Input,Output $$` |
| 2 | 第 30 行（Width_g） | `$$ EXPORT=true $$` |
| 3 | 第 39 行（InClk） | `$$ TYPE=CLK; FREQ=100e6; PROC=Input $$` |
| 4 | 第 33 行（AlmFullLevel_g） | `$$CONSTANT=12$$` |
| 5 | 第 52 行（OutRdy） | `$$ PROC=Output,Input $$` |

### 5.2 操作步骤

**第一步**：对每一行，写出你预测的字典。重点关注：

- 键是否小写了？
- 单值是字符串、列表值是 Python 列表吗？
- 第 3 行的多标签是否都进了字典？

**第二步**：运行下面的脚本验证（**示例代码**，需要 `pyparsing` 可用；若 `from DutInfo import ...` 因缺少 `PsiPyUtils` 失败，请改用紧随其后的「纯 pyparsing 独立版本」）。

```python
# 示例代码：直接调用真实方法验证
from DutInfo import DutInfo   # 需要 pyparsing 与 PsiPyUtils 均可导入

samples = [
    "$$ PROCESSES=Input,Output $$",
    "$$ EXPORT=true $$",
    "$$ TYPE=CLK; FREQ=100e6; PROC=Input $$",
    "$$CONSTANT=12$$",
    "$$ PROC=Output,Input $$",
]
for s in samples:
    print(repr(s), "->", DutInfo._ParseTags(s))
```

如果 `DutInfo` 因依赖无法导入，用这个**等价的独立脚本**（只依赖 `pyparsing`，文法与真实方法逐字一致）：

```python
# 示例代码：仅依赖 pyparsing 的独立验证脚本
import pyparsing as pp

def ParseTags(string):
    SINGLE_VALUE = pp.CharsNotIn(";$")
    LIST_VALUE = pp.OneOrMore(pp.Word(pp.alphanums + "_.") + pp.Literal(",")) + pp.Word(pp.alphanums + "_.")
    ANY_VALUE = pp.Group(LIST_VALUE("listVal") | SINGLE_VALUE("singleVal"))
    TAGS = ("$$" + pp.OneOrMore(
                pp.Group(pp.Word(pp.alphas)("tag") + "=" + ANY_VALUE("value") + pp.Optional(";")))("tags") + "$$")
    tags = {}
    for t, s, e in TAGS.scanString(string):
        for tag in t.get("tags"):
            val = tag.get("value")
            if "listVal" in val.keys():
                val = "".join(val).split(",")
            elif "singleVal" in val.keys():
                val = val.get("singleVal").strip()
            tags[tag.get("tag").lower()] = val
    return tags

for s in ["$$ PROCESSES=Input,Output $$",
          "$$ TYPE=CLK; FREQ=100e6; PROC=Input $$",
          "$$ PROC=Output,Input $$"]:
    print(repr(s), "->", ParseTags(s))
```

### 5.3 预期结果与现象

运行后应得到（若不一致，回到 4.2.3 的文法规则排查）：

```
'$$ PROCESSES=Input,Output $$'            -> {'processes': ['Input', 'Output']}
'$$ EXPORT=true $$'                       -> {'export': 'true'}
'$$ TYPE=CLK; FREQ=100e6; PROC=Input $$'  -> {'type': 'CLK', 'freq': '100e6', 'proc': 'Input'}
'$$CONSTANT=12$$'                         -> {'constant': '12'}
'$$ PROC=Output,Input $$'                 -> {'proc': ['Output', 'Input']}
```

需要重点确认的三个现象：

- **键小写、值保留大小写**：`TYPE=CLK` → 键 `type`、值仍是 `'CLK'`。
- **单值是 `str`、列表是 `list`**：`EXPORT=true` 是字符串 `'true'`，`PROCESSES=Input,Output` 是列表。
- **无空格不影响**：`$$CONSTANT=12$$` 与有空格的写法结果一致，定界符 `$$` 才是关键。

> 若本机未安装 `pyparsing`，上述脚本无法运行，请先 `pip install pyparsing`；运行结果标注为「待本地验证」。

## 6. 本讲小结

- **标签是输入契约**：设计者在 VHDL 注释里用 `$$ 键=值; 键=值 $$` 描述 DUT 的测试意图，`Tags` 常量类（[DutInfo.py:15-32](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/DutInfo.py#L15-L32)）集中声明了所有合法标签名。
- **值有两种形态**：单值（`FREQ=100e6`）与列表（`PROCESSES=Input,Output`）；标签名**大小写不敏感**，值的原始大小写被保留。
- **`_ParseTags` 是翻译器**：用 pyparsing 把注释扫描成字典，键统一小写，单值留字符串、列表切分成 `list`（[DutInfo.py:93-112](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/DutInfo.py#L93-L112)）。
- **注释来自 `VhdlParse`**：行尾注释挂到端口/generic 的 `.comment`，独立注释行进入 `commentLines`，再由 `DutInfo.__init__` 汇总成 `fileScopeTags`。
- **工具方法分两层**：`HasTag/GetTag/GetTagAsList/HastTagValue/FilterForTag` 封装了「存在性 / 取值 / 归一列表 / 值匹配 / 批量筛选」，是生成器真正调用的接口（[DutInfo.py:114-166](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/DutInfo.py#L114-L166)）。
- **易踩的坑**：标签名只能用字母（`pp.Word(pp.alphas)`），含数字的标签名会被静默忽略。

## 7. 下一步学习建议

本讲你掌握了「标签长什么样、怎么解析、怎么查询」，但还**没有**讲每个具体标签如何改变生成的 testbench。建议接下来：

- **进入 u2-l2《端口与 generic 标签详解》**：逐一拆解 `TYPE(CLK/RST/SIG)`、`FREQ`、`CLK`、`PROC`、`LOWACTIVE`、`EXPORT`、`CONSTANT` 如何驱动 `_Clocks`、`_Resets`、`_Processes`、`_DutSignals` 的生成。学完后，你能解释 u1-l3 综合实践里观察到的所有差异。
- **之后进入 u2-l3《文件级标签与 TbInfo 建模》**：讲清 `PROCESSES`、`TESTCASES`、`DUTLIB`、`TBPKG` 如何作用于整个测试台。
- **想深入解析层**：可跳到 u3-l1、u3-l2 学习 `VhdlParse.py` 的 pyparsing 框架（本讲只用到其中的 `PP_COMMENT` 与 `VhdlCommentLine`）。
