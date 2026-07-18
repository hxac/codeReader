# 解析 entity/generic/port 与 VhdlFile 读取

## 1. 本讲目标

本讲紧接 u3-l1。上一讲我们搭好了 [VhdlParse.py](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/VhdlParse.py) 的「地基」——`PP_*` 积木、`PrToStr`、`VhdlConstruct` 基类。本讲要把这套地基真正用起来，回答一个具体问题：

> 一份完整的 VHDL 文件，是怎么被读成程序里的 `entity.name`、`generics`、`ports`、`use` 语句、注释行的？

学完本讲你应当能够：

1. 说出 `VhdlType`/`VhdlRange` 如何解析一个类型名及其 `(左 to/downto 右)` 范围，并理解 `to` 与 `downto` 在 `low`/`high` 上的取值差异。
2. 说出 `VhdlGenericDeclaration`/`VhdlPortDeclaration` 的文法如何同时抓取「名字、类型、（端口独有的）方向、默认值、行尾注释」，并理解那个行尾 `.comment` 为什么是连接 u2「标签系统」的桥梁。
3. 说出 `VhdlEntityDeclaration` 如何把 generic 子句和 port 子句嵌进 `entity … is … end`，并用「命名子结果 + 过滤」的手法从混杂注释的 port 列表里只挑出真正的端口。
4. 说出 `VhdlFile` 的三段读取主流程（entity → use 语句 → 注释行），以及它为什么用 `scanString` 而不是 `parseString`。
5. 能对 `example/simpleTb` 的真实 VHDL 文件实例化 `VhdlFile`，打印出实体名、全部 generics（含类型与默认值）、全部 ports（含方向）和全部 use 语句。

本讲是把「文本」变成「可查询模型」的最后一公里；下一单元 u4 的 `DutInfo` 会直接消费 `VhdlFile` 产出的这些属性。

## 2. 前置知识

### 2.1 回顾：VhdlConstruct 的两段式套路

u3-l1 已经讲过，`VhdlParse.py` 里所有 `Vhdl*` 子类都遵循同一个套路（详见 [VhdlParse.py:43-65](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/VhdlParse.py#L43-L65)）：

- **类属性 `PP_DEFINITION`**：本构件的 pyparsing 文法（怎么认）。
- **方法 `_Parse(self, parts)`**：从解析结果 `parts` 里取字段、挂到 `self`（认出来之后存什么）。

基类 `__init__` 负责把文法跑一遍并调 `_Parse`；类方法 `PP()` 把文法包成 `Group` 供父文法嵌套引用；`__str__` 把 `self.code` 略做清理后还原成文本。本讲的五个模块全部是这套套路的实例，所以我们会反复看到「文法里 `expr("名字")` 起名 → `_Parse` 里 `parts.get("名字")` 取值」的配合。

### 2.2 几个本讲要用到的积木

本讲的文法会反复用到 u3-l1 讲过的这些积木，先列在这里方便对照（定义见 [VhdlParse.py:24-31](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/VhdlParse.py#L24-L31)）：

| 积木 | 匹配什么 | 本讲用途 |
|------|----------|----------|
| `PP_IDENTIFIER` | 字母/数字/下划线组成的词 | generic/port/类型的名字 |
| `PP_DIRECTION` | `in`/`out`/`inout`/`buffer`（大小写不敏感） | 端口方向 |
| `PP_RANGEDIR` | `to`/`downto`（大小写不敏感） | 范围方向 |
| `PP_EXPRESSION` | 任意一坨文本（遇关键字/分号/括号/注释停，支持嵌套括号） | 默认值、范围左右界 |
| `PP_COMMENT` | `--` 加本行剩余，正文记到 `text` | generic/port 行尾注释 |

> 关键回忆：`PP_EXPRESSION` 用「否定前瞻踩刹车 + `Forward()` 递归括号」实现（[VhdlParse.py:15-22](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/VhdlParse.py#L15-L22)）。正因为它会在分号、括号前停下，本讲的「默认值」「范围左右界」才能被精确切出来，而不会越界吞掉后续结构。

### 2.3 VHDL 的 entity 长什么样

如果你没写过 VHDL，只要先建立一个直观印象即可。一个 entity（实体）描述了模块对外的「接口插座」，典型结构是（取自示例 [example/simpleTb/psi_common_async_fifo.vhd:28-68](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/example/simpleTb/psi_common_async_fifo.vhd#L28-L68)）：

```vhdl
entity psi_common_async_fifo is
    generic (
        Width_g : positive := 16;     -- $$ EXPORT=true $$
        ...
    );
    port (
        InClk  : in  std_logic;        -- $$ TYPE=CLK; FREQ=100e6 $$
        InData : in  std_logic_vector(Width_g-1 downto 0) := (others => '0');
        InRdy  : out std_logic;
        ...
    );
end entity;
```

三件事要记住：

- **generic**：编译期参数（如位宽 `Width_g`），有「类型」和「默认值」。
- **port**：运行期端口（如时钟 `InClk`），比 generic 多一个「方向」`in`/`out`/…。
- **`-- ...`**：行尾注释。本工具把 `$$ ... $$` 标签写在这些注释里（u2 已讲），所以「能否抓到行尾注释」直接决定了标签能否进入数据模型。

本讲的目标，就是让程序把上面这段文本读成结构化的 Python 对象。

## 3. 本讲源码地图

本讲只涉及一个文件，但聚焦它的「上层构件」与「文件入口」：

| 文件 | 角色 | 本讲关注 |
|------|------|----------|
| [VhdlParse.py](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/VhdlParse.py) | 基于 pyparsing 的 VHDL 解析模块 | `VhdlRange`/`VhdlType`、`VhdlGenericDeclaration`/`VhdlPortDeclaration`、`VhdlEntityDeclaration`、`VhdlFile` |

按自底向上的依赖顺序，本讲涉及五个构件，依赖关系如下：

```
        VhdlRange ──┐
                    ├──► VhdlType ──┐
        VhdlRangeFromTo ────────────┘    │
                                         ├──► VhdlGenericDeclaration ──┐
                                         │     VhdlPortDeclaration  ────┤
                                         │                              ▼
                                         │                   VhdlEntityDeclaration
                                         │                              │
                                         ▼                              ▼
                                  (类型/范围)                       VhdlFile（文件入口）
```

一句话：`VhdlType` 复用 `VhdlRange`；两种声明复用 `VhdlType`；`VhdlEntityDeclaration` 复用两种声明；`VhdlFile` 复用 `VhdlEntityDeclaration`（外加 `VhdlUseStatement`、`VhdlCommentLine`，后两者已在 u3-l1 讲过）。所以我们按这条链路从底往上讲。

> 顺带说明：本讲会出现两个「范围」构件——`VhdlRange` 匹配带括号的 `(左 dir 右)`，`VhdlRangeFromTo` 匹配 `range 左 dir 右`（无括号，常见于子类型 `subtype … range 0 to 7`）。后者在本工具的实际示例里几乎不出现，但文法里作为 `VhdlType` 的可选项保留着，我们会在 4.1 一并带过。

## 4. 核心概念与源码讲解

### 4.1 VhdlType 与 VhdlRange：类型名与范围

#### 4.1.1 概念说明

VHDL 的端口/generic 都有一个「类型」。类型分两种情况：

- **标量类型**：如 `std_logic`、`positive`、`boolean`、`natural`——只有一个类型名，没有范围。
- **带范围的类型**：如 `std_logic_vector(Width_g-1 downto 0)`、`unsigned(7 downto 0)`——类型名后面跟一个括号范围。

范围的核心信息是三件：**左界、方向（`to`/`downto`）、右界**。方向的语义决定了谁是「下界（low）」、谁是「上界（high）」：

- `a to b`：左是下界、右是上界（`low=a, high=b`），常用于升序枚举/地址。
- `a downto b`：左是上界、右是下界（`low=b, high=a`），这是 VHDL 里最常见的向量写法（高位在左）。

`VhdlRange` 把这个方向语义直接算出来存成 `low`/`high`，省得下游每次都要自己判断方向——这是它存在的核心价值。

#### 4.1.2 核心流程

`VhdlRange` 的解析与建模流程：

```
文本 "(Width_g-1 downto 0)"
        │  文法: "(" + 左界 + 方向 + 右界 + ")"
        ▼
  parts = { left, dir, right }
        │  _Parse:
        ▼
  self.left / self.right / self.direction
  方向==to     → low=left,  high=right
  方向==downto → low=right, high=left
```

`VhdlType` 则是「类型名 + 可选范围 + 可选 range-from-to」的组合：

```
VhdlType 文法: 类型名 + Optional(VhdlRange) + Optional(VhdlRangeFromTo)
        │
        ▼
  self.name = 类型名
  self.range = VhdlRange(...) 或 None
  __str__: 有范围 → name + range；无范围 → name
```

#### 4.1.3 源码精读

先看带括号的范围 `VhdlRange`：

[VhdlParse.py:82-96](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/VhdlParse.py#L82-L96) —— `VhdlRange`：文法是「`(` + 左界表达式 + 方向 + 右界表达式 + `)`」，并把方向翻译成 low/high。

> 这段代码做了：`PP_DEFINITION` 用 `PP_EXPRESSION` 抓左右界（所以左界可以是 `Width_g-1`、`log2ceil(Depth_g)` 这种含函数调用与嵌套括号的复杂表达式），用 `PP_RANGEDIR` 抓 `to`/`downto`。`_Parse` 里把方向 `lower()` 归一化后，按 `to`/`downto` 分别算出 `self.low`/`self.high`——注意 `downto` 时 low 取右界、high 取左界。既不是 `to` 也不是 `downto` 就抛异常（理论上 `PP_RANGEDIR` 已限制不会走到这里，这是防御性代码）。

```python
class VhdlRange(VhdlConstruct):
    PP_DEFINITION =  pp.Literal("(") + PP_EXPRESSION("left") + PP_RANGEDIR("dir") + PP_EXPRESSION("right") + pp.Literal(")")

    def _Parse(self, parts : pp.ParseResults):
        self.left = parts.get("left")
        self.right = parts.get("right")
        self.direction = str(parts.get("dir")).lower()
        if self.direction == "to":
            self.low = self.left
            self.high = self.right
        elif self.direction == "downto":
            self.low = self.right
            self.high = self.left
        else:
            raise Exception("Illegal range: {}".format(self.code))
```

> 数学化地看，`low`/`high` 是对「左/右」按方向做的一次重排。设左界为 \(L\)、右界为 \(R\)、方向为 \(d\in\{\texttt{to},\texttt{downto}\}\)，则
> \[
> (\text{low},\text{high}) =
> \begin{cases} (L,R), & d=\texttt{to}\\ (R,L), & d=\texttt{downto}\end{cases}
> \]
> 这样无论原文本是 `0 to 7` 还是 `7 downto 0`，下游始终能用 `low`/`high` 拿到「最小/最大下标」，不必关心书写方向。

再看无括号的 `VhdlRangeFromTo`（仅作了解，本工具示例中基本不触发）：

[VhdlParse.py:98-104](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/VhdlParse.py#L98-L104) —— `VhdlRangeFromTo`：匹配 `range 左 dir 右`，只存 left/right/direction，不算 low/high。

> 这段代码做了：文法以关键字 `range` 开头（而不是括号），用于子类型声明里的约束范围。它比 `VhdlRange` 简单——只记录左右界和方向，没有计算 low/high（因为这种场景下游不需要）。

```python
class VhdlRangeFromTo(VhdlConstruct):
    PP_DEFINITION = pp.Literal("range") + PP_EXPRESSION("left") + PP_RANGEDIR("dir") + PP_EXPRESSION("right")

    def _Parse(self, parts : pp.ParseResults):
        self.left = parts.get("left")
        self.right = parts.get("right")
        self.direction = str(parts.get("dir")).lower()
```

最后看把「类型名 + 范围」组合起来的 `VhdlType`：

[VhdlParse.py:106-121](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/VhdlParse.py#L106-L121) —— `VhdlType`：类型名 + 可选括号范围 + 可选 range-from-to。

> 这段代码做了：`PP_DEFINITION` 先用 `PP_IDENTIFIER("vhdlType")` 抓类型名（如 `std_logic_vector`），再 `Optional` 地接一个 `VhdlRange.PP()`（带括号范围），最后再 `Optional` 接一个 `VhdlRangeFromTo.PP()`。`_Parse` 里：`self.name` 存类型名；若抓到了范围子结果，就用 `VhdlRange(range)` **重建**一个范围对象挂到 `self.range`，否则 `self.range=None`。注意这里又一次出现了 u3-l1 讲过的「父构件把子片段以 `ParseResults` 形式喂给子构件构造函数」的模式——`VhdlRange(range)` 内部会先用 `PrToStr` 把它转回字符串再解析。`__str__` 则根据有无范围决定输出 `name+range` 还是 `name`。

```python
class VhdlType(VhdlConstruct):
    PP_DEFINITION = PP_IDENTIFIER("vhdlType") + pp.Optional(VhdlRange.PP()("range")) + pp.Optional(VhdlRangeFromTo.PP())

    def _Parse(self, parts : pp.ParseResults):
        self.name = parts.get("vhdlType")
        range = parts.get("range")
        if range is not None:
            self.range = VhdlRange(range)
        else:
            self.range = None

    def __str__(self):
        if self.range is not None:
            return self.name + str(self.range)
        else:
            return self.name
```

> 对真实示例的理解：对于 `InData : in std_logic_vector(Width_g-1 downto 0)`，`VhdlType` 会得到 `name="std_logic_vector"`、`range.left="Width_g-1"`、`range.right="0"`、`range.direction="downto"`，进而 `range.low="0"`、`range.high="Width_g-1"`。对于 `InClk : in std_logic`，则 `name="std_logic"`、`range=None`。

#### 4.1.4 代码实践

**实践目标**：亲手解析几种典型类型，验证 `low`/`high` 随方向翻转，并体会「`PP_EXPRESSION` 能吃下含函数调用的复杂左界」。

**操作步骤**（在仓库根目录启动 Python，便于 `from VhdlParse import ...`）：

```python
# 示例代码：观察 VhdlType / VhdlRange 的解析结果
from VhdlParse import VhdlType

samples = [
    "std_logic",                          # 标量，无范围
    "std_logic_vector(7 downto 0)",       # 经典降序向量
    "unsigned(0 to 7)",                   # 升序
    "std_logic_vector(log2ceil(Depth_g)-1 downto 0)",  # 复杂左界
]
for s in samples:
    t = VhdlType(s)
    if t.range is not None:
        print(f"{s!r:50} -> name={t.name!r}, dir={t.range.direction}, "
              f"low={t.range.low!r}, high={t.range.high!r}")
    else:
        print(f"{s!r:50} -> name={t.name!r}, range=None")
```

**需要观察的现象**：

- `downto` 时 `low` 是右界、`high` 是左界；`to` 时反过来。
- 复杂左界 `log2ceil(Depth_g)-1` 被整段抓下（说明 `PP_EXPRESSION` 的嵌套括号能力生效）。
- 标量类型 `std_logic` 的 `range` 为 `None`。

**预期结果**：

- `"std_logic"` → `name='std_logic', range=None`
- `"std_logic_vector(7 downto 0)"` → `name='std_logic_vector', dir='downto', low='0', high='7'`
- `"unsigned(0 to 7)"` → `name='unsigned', dir='to', low='0', high='7'`
- 复杂左界那条 → `dir='downto'`，`low='0'`，`high` 为含 `log2ceil(Depth_g)-1` 的字符串。

**若无法确定运行结果**：复杂左界还原后的精确字符串（是否带多余空格）依赖 pyparsing 版本，可标「待本地验证」；但 `name`、`direction`、`low`/`high` 的归属是确定的。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `VhdlRange._Parse` 要把 `dir` 先 `lower()` 再比较？如果原文写成 `DOWNTO` 会怎样？

**参考答案**：因为 `PP_RANGEDIR` 用的是 `CaselessKeyword`，匹配成功但保留原文大小写；原文可能是 `DOWNTO`/`Downto`。先 `lower()` 归一化成 `to`/`downto` 再比较，保证大小写不敏感。若不 `lower()`，`DOWNTO` 会落到 `else` 分支抛 `Illegal range`。

**练习 2**：`VhdlType` 的文法里，`VhdlRange` 和 `VhdlRangeFromTo` 都加了 `Optional`。如果一个类型同时写了括号范围和 `range …`，会发生什么？

**参考答案**：二者都会被匹配并出现在 `parts` 里，但 `_Parse` 只处理了 `parts.get("range")`（即括号范围 `VhdlRange`），完全忽略 `VhdlRangeFromTo` 那段。实际 VHDL 里这两种写法不会同时出现在一个端口类型上，所以不会出问题；这属于「文法宽松、取值克制」的设计。

---

### 4.2 VhdlGenericDeclaration：generic 声明

#### 4.2.1 概念说明

一条 generic 声明形如 `Width_g : positive := 16; -- $$ EXPORT=true $$`，包含五段信息：

1. **名字** `Width_g`
2. **类型** `positive`（复用 4.1 的 `VhdlType`）
3. **默认值** `16`（可选，`:= ...`）
4. **分号** `;`（可选，因为最后一条 generic 可能不带分号——但本工具文法里分号也设成 Optional 以容错）
5. **行尾注释** `-- $$ EXPORT=true $$`（可选）

其中第 5 段是关键：generic 的 `$$ ... $$` 标签（如 `EXPORT=true`、`CONSTANT=12`）就写在行尾注释里。u2 讲过这些标签如何驱动 `TbGenerator` 的生成行为（`EXPORT` 决定是否进 TB 实体 generic、`CONSTANT` 决定是否固定为某值），而本讲要强调的是：**标签能进入数据模型，前提是解析器把行尾注释抓到了 `self.comment`**。这就是 4.2/4.3 两个声明构件与 u2 标签系统之间的物理连接点。

#### 4.2.2 核心流程

```
文本 "Width_g : positive := 16; -- $$ EXPORT=true $$"
   文法: 名字 + ":" + 类型 + Optional(":=" + 默认值) + Optional(";") + Optional(注释)
        │  _Parse:
        ▼
  self.name   = "Width_g"
  self.type   = VhdlType("positive")
  self.default= "16"            （若无 := ... 则为 None）
  self.comment= "$$ EXPORT=true $$"   （取 -- 之后的部分；若无注释则为 None）
```

注意三个「可选」对应三种现实写法：

- 无默认值：`AlmEmptyLevel_g : natural`（`default=None`）
- 无行尾注释：`AlmEmptyOn_g : boolean := false`（`comment=None`）
- 既无默认也无注释。

#### 4.2.3 源码精读

[VhdlParse.py:123-134](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/VhdlParse.py#L123-L134) —— `VhdlGenericDeclaration`：一条 generic 的完整文法与字段提取。

> 这段代码做了：`PP_DEFINITION` 依次是 `PP_IDENTIFIER("name")` + `":"` + `VhdlType.PP()("type")` + `Optional(":=" + PP_EXPRESSION("default"))` + `Optional(";")` + `Optional(PP_COMMENT("comment"))`。`_Parse` 里：`self.name` 取名字；`self.type` 用 `VhdlType(parts.get("type"))` 重建类型对象；默认值用 `parts.get("default")[0]` 取（注意 `[0]`——因为 `PP_EXPRESSION` 是 `Group(Combine(...))`，`[0]` 取出 Combine 后的那一个字符串），无则 `None`；注释取 `parts.get("comment").get("text")`（即 `--` 之后的正文），无则 `None`。

```python
class VhdlGenericDeclaration(VhdlConstruct):
    PP_DEFINITION = PP_IDENTIFIER("name") + ":" + VhdlType.PP()("type") + pp.Optional(":=" + PP_EXPRESSION("default")) + pp.Optional(";") + pp.Optional(PP_COMMENT("comment"))

    def _Parse(self, parts : pp.ParseResults):
        self.name = parts.get("name")
        self.type = VhdlType(parts.get("type"))
        self.default = None
        if parts.get("default") is not None:
            self.default = parts.get("default")[0]
        self.comment = None
        if parts.get("comment") is not None:
            self.comment = parts.get("comment").get("text")
```

两个细节值得记住：

- **默认值为何是 `[0]`**：`PP_EXPRESSION` 外层套了 `Group`（[VhdlParse.py:22](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/VhdlParse.py#L22)），所以 `parts.get("default")` 拿到的是一个含单元素的子结果，`[0]` 才是真正的字符串。
- **`.comment` 是标签的入口**：例如示例 [example/simpleTb/psi_common_async_fifo.vhd:30](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/example/simpleTb/psi_common_async_fifo.vhd#L30) 的 `Width_g` 行，注释正文就是 `" $$ EXPORT=true $$"`。u4 的 `DutInfo` 会逐条 generic/port 调用 `_ParseTags(self.comment)` 把它变成标签字典（详见 u2-l1）。

#### 4.2.4 代码实践

**实践目标**：解析一条带标签的 generic，确认 `name/type/default/comment` 四个字段都被正确填充，并亲眼看到 `.comment` 里就是标签文本。

**操作步骤**：

```python
# 示例代码：解析单条 generic 声明
from VhdlParse import VhdlGenericDeclaration

g = VhdlGenericDeclaration('Width_g : positive := 16; -- $$ EXPORT=true $$')
print("name    :", g.name)
print("type    :", g.type.name)      # positive（无范围）
print("default :", repr(g.default))  # 16（可能带尾部空格，见下方观察）
print("comment :", repr(g.comment))  # 标签正文

# 再试一条没有默认值、没有注释的
g2 = VhdlGenericDeclaration("AlmEmptyLevel_g : natural")
print("default2:", g2.default)       # 预期 None
print("comment2:", g2.comment)       # 预期 None
```

**需要观察的现象**：

- `g.comment` 应当是 `--` 之后的内容（即 `$$ EXPORT=true $$`，可能带前导空格）——这正是 u2 标签的载体。
- `g.default` 可能带尾部空格（因为 `PP_EXPRESSION` 会吃到分号前的空白）。这正是为什么下游取默认值时往往需要在意空格。

**预期结果**：`name=Width_g`、`type.name=positive`、`default` 为 `"16"`（可能 ` "16 "`）、`comment` 含 `EXPORT=true`；第二条 `default=None`、`comment=None`。

**若无法确定运行结果**：默认值与注释正文是否带首尾空格依赖 pyparsing 对 `Combine`/`restOfLine` 的处理细节，可标「待本地验证」；四个字段「有/无」与归属是确定的。

#### 4.2.5 小练习与答案

**练习 1**：把 `pp.Optional(";")` 从文法里去掉，对 `generic(...)` 里「中间多条 generic、每条都带分号」的常见写法，会有什么影响？

**参考答案**：去掉后分号变成必选。中间几条 generic 都带分号，仍能匹配；但本工具的 `VhdlEntityDeclaration` 是把整个 `(...)` 里「多条 generic」作为 `OneOrMore(VhdlGenericDeclaration.PP() | ...)` 来匹配的，分号是每条声明自带的结尾。把分号设成 Optional 是为了对「最后一条不带分号」等非标准写法更宽容，去掉后容错性下降，但标准 VHDL 仍可解析。

**练习 2**：为什么 `default` 用 `parts.get("default")[0]` 取，而 `comment` 用 `parts.get("comment").get("text")` 取？两者结构差在哪？

**参考答案**：`default` 的文法是 `PP_EXPRESSION("default")`，而 `PP_EXPRESSION` 是 `Group(Combine(...))`，外层是 Group，所以要 `[0]` 进到内层 Combine 的那一个字符串。`comment` 的文法是 `PP_COMMENT("comment")`，而 `PP_COMMENT` 内部已经用 `pp.restOfLine("text")` 给正文起了名 `text`（[VhdlParse.py:28](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/VhdlParse.py#L28)），所以直接 `.get("text")`。结构不同，取法不同。

---

### 4.3 VhdlPortDeclaration：port 声明

#### 4.3.1 概念说明

port 声明比 generic 多了一个**方向**。一条 port 形如 `InClk : in std_logic; -- $$ TYPE=CLK; FREQ=100e6 $$`，六段信息：

1. **名字** `InClk`
2. **方向** `in`（这是 port 独有、generic 没有的）
3. **类型** `std_logic`（复用 `VhdlType`，可以是带范围的向量）
4. **默认值**（可选，如 `OutRdy : in std_logic := '1'`）
5. **分号**（可选）
6. **行尾注释**（可选，承载 `TYPE`/`FREQ`/`CLK`/`PROC` 等端口标签）

和 generic 一样，行尾注释 `.comment` 是 u2 端口标签（`TYPE=CLK`、`PROC=Input` 等）的载体——u2-l2 已讲过这些标签如何驱动时钟进程、复位归属、过程绑定，本讲只确认「它们能被解析到」。

#### 4.3.2 核心流程

```
文本 "InClk : in std_logic; -- $$ TYPE=CLK; FREQ=100e6 $$"
   文法: 名字 + ":" + 方向 + 类型 + Optional(":=" + 默认值) + Optional(";") + Optional(注释)
        │  _Parse:
        ▼
  self.name     = "InClk"
  self.direction= "in"
  self.type     = VhdlType("std_logic")
  self.default  = None
  self.comment  = "$$ TYPE=CLK; FREQ=100e6 $$"
```

对比 generic：仅多了一个 `PP_DIRECTION("dir")` 夹在 `":"` 与类型之间，对应多出的 `self.direction` 字段。其余结构与取值手法与 generic 完全一致——这正是 `VhdlConstruct` 套路的好处。

#### 4.3.3 源码精读

[VhdlParse.py:136-148](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/VhdlParse.py#L136-L148) —— `VhdlPortDeclaration`：在 generic 文法基础上插入方向 `PP_DIRECTION`。

> 这段代码做了：`PP_DEFINITION` = `PP_IDENTIFIER("name")` + `":"` + `PP_DIRECTION("dir")` + `VhdlType.PP()("type")` + `Optional(":=" + PP_EXPRESSION("default"))` + `Optional(";")` + `Optional(PP_COMMENT("comment"))`。与 generic 的唯一文法差异就是 `":"` 之后多了 `PP_DIRECTION("dir")`。`_Parse` 把方向存为 `self.direction`（注意没有 `lower()`——`PP_DIRECTION` 用 `CaselessKeyword`，保留原文大小写；本工具示例里方向都写小写 `in`/`out`）。其余字段（`type`/`default`/`comment`）的取法与 `VhdlGenericDeclaration` 逐字相同。

```python
class VhdlPortDeclaration(VhdlConstruct):
    PP_DEFINITION = PP_IDENTIFIER("name") + ":" + PP_DIRECTION("dir") + VhdlType.PP()("type") +  pp.Optional(":=" + PP_EXPRESSION("default")) + pp.Optional(";") + pp.Optional(PP_COMMENT("comment"))

    def _Parse(self, parts : pp.ParseResults):
        self.name = parts.get("name")
        self.type = VhdlType(parts.get("type"))
        self.direction = parts.get("dir")
        self.default = None
        if parts.get("default") is not None:
            self.default = parts.get("default")[0]
        self.comment = None
        if parts.get("comment") is not None:
            self.comment = parts.get("comment").get("text")
```

> 对真实示例的理解：[example/simpleTb/psi_common_async_fifo.vhd:45](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/example/simpleTb/psi_common_async_fifo.vhd#L45) 的 `InData : in std_logic_vector(Width_g-1 downto 0) := (others => '0'); -- $$ PROC=Input$$`，会被解析为 `name="InData"`、`direction="in"`、`type.name="std_logic_vector"`（带 downto 范围）、`default` 为 `(others => '0')` 还原串、`comment` 含 `PROC=Input`。注意这里的默认值 `(others => '0')` 是嵌套括号表达式，正是 `PP_EXPRESSION` 递归括号能力的用武之地。

#### 4.3.4 代码实践

**实践目标**：解析一条带「向量类型 + 默认值 + 标签」的复杂 port，确认方向、类型范围、默认值、注释都被正确拆出。

**操作步骤**：

```python
# 示例代码：解析一条复杂 port
from VhdlParse import VhdlPortDeclaration

p = VhdlPortDeclaration("InData : in std_logic_vector(Width_g-1 downto 0) := (others => '0'); -- $$ PROC=Input$$")
print("name     :", p.name)
print("direction:", p.direction)
print("type name:", p.type.name)
print("range    :", p.type.range.direction, "low=", p.type.range.low, "high=", p.type.range.high)
print("default  :", repr(p.default))
print("comment  :", repr(p.comment))

# 一个最简单的输出端口
p2 = VhdlPortDeclaration("InRdy : out std_logic")
print("dir2     :", p2.direction, " type2:", p2.type.name, " range2:", p2.type.range)
```

**需要观察的现象**：

- `direction` 为 `in`/`out`（保留原文小写）。
- 向量类型的 `range` 不为 `None`，且 `downto` 时 low/high 颠倒。
- 默认值 `(others => '0')` 这种嵌套括号被完整抓下（验证 `PP_EXPRESSION` 递归能力）。

**预期结果**：`name=InData`、`direction=in`、`type.name=std_logic_vector`、`range.direction=downto`、`range.low=0`、`range.high=Width_g-1`；`default` 为含 `(others => '0')` 的字符串；`comment` 含 `PROC=Input`。第二条 `direction=out`、`type.name=std_logic`、`range=None`。

**若无法确定运行结果**：默认值 `(others => '0')` 还原后的精确空格分布依赖 pyparsing 版本，可标「待本地验证」；方向、类型名、范围归属是确定的。

#### 4.3.5 小练习与答案

**练习 1**：`VhdlPortDeclaration` 与 `VhdlGenericDeclaration` 文法几乎相同，唯一区别是端口在 `":"` 后多了 `PP_DIRECTION`。如果把一条 generic 误用 `VhdlPortDeclaration` 去解析 `Width_g : positive := 16`，会怎样？

**参考答案**：`PP_DIRECTION` 会尝试在 `":"` 后匹配 `in`/`out`/`inout`/`buffer`，而原文是 `positive`，匹配失败，整个声明解析抛异常。这说明方向是端口与 generic 在文法上的硬性区分点。

**练习 2**：`self.direction` 没有 `lower()`，而 `VhdlRange` 的 `direction` 做了 `lower()`。下游代码若想判断「是否输出端口」，应该怎么写比较稳？

**参考答案**：因为这里没归一化，`direction` 可能保留原文大小写。下游判断时最好自己 `str(p.direction).lower() == "out"`，避免 `OUT`/`Out` 漏判。`VhdlRange` 之所以主动 `lower()`，正是因为它内部要按方向做分支；两者策略不同，下游使用时要注意。

---

### 4.4 VhdlEntityDeclaration：把 generic/port 装进 entity

#### 4.4.1 概念说明

有了 generic 声明和 port 声明，就可以拼出整个 entity。一个 entity 的骨架是：

```vhdl
entity <名字> is
    [generic (...);]
    [port (...);]
end [entity] [<名字>];
```

`generic (...)` 和 `port (...)` 都是可选的（有的实体只有 port、有的两者都有）。难点在于：括号里是「多条声明 + 散落注释」的混合流。例如示例的 port 列表里就夹着 `-- Control Ports`、`-- Input Data` 这样的独立注释行（[example/simpleTb/psi_common_async_fifo.vhd:38](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/example/simpleTb/psi_common_async_fifo.vhd#L38) 等处）。`VhdlEntityDeclaration` 要做两件事：

1. 用 `OneOrMore(...)` 把括号里的「声明 / 注释」混合流整段吃下。
2. 在 `_Parse` 里把「真正的声明」挑出来，丢掉散落注释，重建为 `VhdlGenericDeclaration`/`VhdlPortDeclaration` 对象列表。

#### 4.4.2 核心流程

```
文本 = "entity Foo is generic(...); port(...); end entity;"
        │  文法（顺序）:
        ▼
  entity + 名字 + is
    + Optional( generic + "(" + OneOrMore(generic声明 | Suppress(注释)) + ")" + ";" )
    + Optional( port    + "(" + OneOrMore(port声明("port") | 注释("comment")) + ")" + ";" )
    + end + Optional(entity) + Optional(名字) + ";"
        │  _Parse:
        ▼
  self.name     = "Foo"
  self.generics = [VhdlGenericDeclaration(...), ...]   （无则 []）
  self.ports    = [VhdlPortDeclaration(...), ...]      （过滤掉注释，无则 []）
```

注意 generic 子句和 port 子句处理注释的两种不同手法（这是个值得细品的设计差异，4.4.3 会展开）。

#### 4.4.3 源码精读

[VhdlParse.py:150-165](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/VhdlParse.py#L150-L165) —— `VhdlEntityDeclaration`：entity 骨架文法与 generics/ports 列表重建。

> 这段代码做了：`PP_DEFINITION` 按 `entity + 名字 + is + [generic(...);] + [port(...);] + end [entity] [名字] ;` 的顺序拼出。generic 子句用 `pp.OneOrMore(VhdlGenericDeclaration.PP() | pp.Suppress(PP_COMMENT))("generics")`——遇到独立注释直接 `Suppress`（丢弃）。port 子句用 `pp.OneOrMore(VhdlPortDeclaration.PP()("port") | PP_COMMENT("comment"))("ports")`——注释没被 Suppress，而是记成 `("comment")` 命名结果，**留到 `_Parse` 里再过滤**。`_Parse` 里：若 `parts` 含 `generics`，逐个 `VhdlGenericDeclaration(gd)` 重建；若含 `ports`，则用 `if pd.getName() == "port"` 只挑出真正端口的子结果，逐个 `VhdlPortDeclaration(pd)` 重建。

```python
class VhdlEntityDeclaration(VhdlConstruct):
    PP_DEFINITION = pp.CaselessKeyword("entity") + PP_IDENTIFIER("name") + pp.CaselessKeyword("is") + \
                    pp.Optional(pp.CaselessKeyword("generic") + "(" + pp.OneOrMore(VhdlGenericDeclaration.PP() | pp.Suppress(PP_COMMENT))("generics") + ")" + ";") + \
                    pp.Optional(pp.CaselessKeyword("port") + "(" + pp.OneOrMore(VhdlPortDeclaration.PP()("port")|PP_COMMENT("comment"))("ports") + ")" + ";" + pp.Optional(PP_COMMENT)) + \
                    pp.CaselessKeyword("end") + pp.Optional(pp.CaselessKeyword("entity")) + pp.Optional(PP_IDENTIFIER) + ";"

    def _Parse(self, parts : pp.ParseResults):
        self.name = parts.get("name")
        if "generics" in parts:
            self.generics = [VhdlGenericDeclaration(gd) for gd in parts.get("generics")]
        else:
            self.generics = []
        if "ports" in parts:
            self.ports = [VhdlPortDeclaration(pd) for pd in parts.get("ports") if pd.getName() == "port"]
        else:
            self.ports = []
```

三处关键设计：

- **「命名子结果 + 过滤」**（第 162–163 行）：port 子句里每条端口声明都标了 `("port")`，注释标了 `("comment")`，二者都被收进 `("ports")` 这个列表型结果。`_Parse` 用 `pd.getName() == "port"` 判断每个子结果是不是端口——是才重建。这样夹在端口之间的 `-- Control Ports` 这类注释就被自然剔除，不会污染端口列表。
- **generic 用 `Suppress`、port 用「命名 + 过滤」**：两种手法效果相近（都不让注释进 generics/ports 列表），但 port 多保留了一步命名。为什么端口要保留命名而 generic 直接 Suppress？一个可观察的差别是：port 子句末尾还多挂了一个 `Optional(PP_COMMENT)`（第 153 行行尾），用来吃掉 `port(...); -- 行尾注释` 这种写法；保留命名机制让文法对注释位置更宽容。这是源码里两处不完全对称的细节，理解时把握「目的都是剔除独立注释、只留声明」即可。
- **结尾 `end [entity] [名字] ;`**（第 154 行）：覆盖了 `end entity;`、`end entity Foo;`、`end Foo;` 三种合法写法。示例 [psi_common_async_fifo.vhd:68](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/example/simpleTb/psi_common_async_fifo.vhd#L68) 用的是 `end entity;`。

> 还要注意：`if "generics" in parts` 用的是 `in` 判断，而不是 `parts.get("generics") is not None`。在 pyparsing 里，一个 `Optional` 且没匹配到的命名结果，用 `get` 可能返回 `""`（空字符串）而非 `None`，所以这里用 `"generics" in parts` 判断「这个结果名是否存在」更稳妥。这是 pyparsing 使用中一个容易踩的坑。

#### 4.4.4 代码实践

**实践目标**：解析一段夹着注释的完整 entity，确认 `name`、`generics`、`ports` 都被正确拆分，且注释被剔除干净。

**操作步骤**：

```python
# 示例代码：解析一个迷你 entity（含夹在端口间的注释）
from VhdlParse import VhdlEntityDeclaration

code = """entity mini is
    generic (
        Width_g : positive := 8     -- $$ EXPORT=true $$
    );
    port (
        -- 时钟与复位
        Clk : in  std_logic;
        Rst : in  std_logic;
        -- 数据
        Din : in  std_logic_vector(7 downto 0);
        Dout: out std_logic_vector(7 downto 0)
    );
end entity;"""

ent = VhdlEntityDeclaration(code)
print("entity name:", ent.name)
print("generics   :", [(g.name, str(g.type), g.default) for g in ent.generics])
print("ports      :", [(p.name, p.direction, str(p.type)) for p in ent.ports])
print("port count :", len(ent.ports), "(应为 4，注释不计入)")
```

**需要观察的现象**：

- `ports` 里只有 4 个端口（Clk/Rst/Din/Dout），夹在中间的 `-- 时钟与复位`、`-- 数据` 注释被剔除。
- `generics` 里只有 1 个 generic（`Width_g`）。

**预期结果**：`name=mini`；`generics=[('Width_g', 'positive', '8')]`；`ports` 4 条，方向分别是 `in/in/in/out`，`Din`/`Dout` 的类型含 `(7 downto 0)`；`port count=4`。

**若无法确定运行结果**：`str(g.type)`/`str(p.type)` 中范围部分的空格还原依赖 pyparsing 版本，可标「待本地验证」；名字、方向、数量是确定的。

#### 4.4.5 小练习与答案

**练习 1**：port 子句用 `if pd.getName() == "port"` 过滤。如果不加这个过滤，直接 `[VhdlPortDeclaration(pd) for pd in parts.get("ports")]`，会发生什么？

**参考答案**：`ports` 里混有 `("comment")` 命名的注释子结果。`VhdlPortDeclaration` 的构造函数会尝试把注释文本当 port 声明去解析，必定失败抛异常。所以这个过滤是把「混合流」清洗成「纯端口列表」的必需步骤。

**练习 2**：为什么第 158 行用 `if "generics" in parts` 而不是 `if parts.get("generics")`？

**参考答案**：pyparsing 中，`Optional` 且未匹配的命名结果，用 `.get()` 可能返回空字符串 `""` 而非 `None`，`if ""` 虽为假恰好也能工作，但语义不清晰、且在某些结果结构下可能拿到非空占位。用 `"generics" in parts` 直接判断「该结果名是否存在」最稳妥，能可靠区分「没有 generic 子句」与「有 generic 子句但解析为空」。

---

### 4.5 VhdlFile：读取整份文件的入口

#### 4.5.1 概念说明

前面四个构件都是在「一段文本」上工作。真正面对一整份 `.vhd` 文件的是 `VhdlFile`。它的职责很纯粹：读文件，然后从中提取三样东西——

1. **`self.entity`**：唯一的 entity 声明（一个 `VhdlEntityDeclaration` 对象）。
2. **`self.usestatements`**：所有 `use 库.元素.对象;` 语句（一组 `VhdlUseStatement` 对象）。
3. **`self.commentLines`**：所有「独立成行的注释」（一组 `VhdlCommentLine` 对象）。

这三样正是下游 `DutInfo`（u4）建模 DUT 所需的全部原料：entity 提供 generics/ports/名字；use 语句决定 testbench 要 `use` 哪些库包；commentLines 里可能藏着文件级标签（如 `$$ PROCESSES=Input,Output $$`、`$$ TESTCASES=... $$`，见 u2-l3）。

#### 4.5.2 核心流程

`VhdlFile.__init__` 的三段式：

```
1. 读文件 → code（字符串），并把 Tab 替换为空格
2. 用 VhdlEntityDeclaration.PP().scanString(code) 扫描 entity：
       取第一个匹配 → self.entity；一个都没有 → 抛 "Syntax error"
3. 用 VhdlUseStatement.PP().scanString(code) 扫描所有 use 语句 → self.usestatements 列表
4. 用 VhdlCommentLine.PP().scanString(code) 扫描所有行首注释 → self.commentLines 列表
```

两个要点：

- **为什么用 `scanString` 而不是 `parseString`**：`parseString` 要求整段文本从头到尾都符合文法，但一份 VHDL 文件里还有 architecture、process、component 实例化等大量「本工具不关心」的内容。`scanString` 是「在全文里扫描所有匹配处」，只捡出关心的片段，忽略其余——这正是它能跳过 architecture 直接定位 entity 的原因。
- **entity 只取第一个**：第 177–179 行 `for ... : self.entity = ...; break`，扫到第一个 entity 就停。一般一份文件只有一个 entity，这是合理的。

#### 4.5.3 源码精读

[VhdlParse.py:168-191](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/VhdlParse.py#L168-L191) —— `VhdlFile`：文件读取入口，三段式扫描。

> 这段代码做了：`__init__` 先 `open` 读全文，`code.replace("\t", " ")` 把 Tab 转空格（为 pyparsing 的空白处理兜底）。然后三段扫描：(1) `VhdlEntityDeclaration.PP().scanString(code)` 找 entity——注意 `scanString` 每次返回 `(tokens, start, end)`，这里用 `code[s:e]` 切出原文区间再交给 `VhdlEntityDeclaration(...)` 重新解析；`for…else` 的 `else` 在「一次都没匹配」时执行，抛 `Syntax error in VHDL Code!`。(2) `VhdlUseStatement.PP().scanString(code)` 把每处 use 语句建成 `VhdlUseStatement` 追加进 `self.usestatements`。(3) `VhdlCommentLine.PP().scanString(code)` 把每处行首注释建成 `VhdlCommentLine` 追加进 `self.commentLines`。

```python
class VhdlFile:

    def __init__(self, fileName : str):
        # Read File
        with open(fileName, "r") as f:
            code = f.read()
        code = code.replace("\t", " ")

        # Parse Entity Declaration
        for t, s, e in VhdlEntityDeclaration.PP().scanString(code):
            self.entity = VhdlEntityDeclaration(code[s:e])
            break
        else:
            raise Exception("Syntax error in VHDL Code!")

        # Parse Library Definitions
        self.usestatements = []
        for t, s, e in VhdlUseStatement.PP().scanString(code):
            self.usestatements.append(VhdlUseStatement(code[s:e]))

        # Parse comment Lines
        self.commentLines = []
        for t,s,e in VhdlCommentLine.PP().scanString(code):
            self.commentLines.append(VhdlCommentLine(code[s:e]))
```

几处值得细看：

- **`code[s:e]` 而非直接用 `tokens`**：`scanString` 返回的 `tokens` 是 `ParseResults`，但作者选择用 `code[s:e]` 切出原始子串、再交给构件构造函数重新解析一遍。这样做的好处是：构件的 `self.code`（也是 `__str__` 的来源）保留的是干净的原文片段，而非 `PrToStr` 拼回的、可能带多余空格的版本。这与 u3-l1 讲过的「`PrToStr` 会插空格、需要 `__str__` 清理」相呼应。
- **`for…else` 找不到 entity 就抛异常**：`for…else` 的 `else` 仅在循环**没有 break**（即一次都没匹配）时执行。所以没有 entity 的文件会直接报错——这是 `VhdlFile` 的硬性前置条件。
- **`code.replace("\t", " ")`**：示例文件里 use 语句前是 Tab 缩进（[psi_common_async_fifo.vhd:16](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/example/simpleTb/psi_common_async_fifo.vhd#L16)），转成空格后，`VhdlUseStatement` 的 `LineStart().leaveWhitespace()` + 默认空白跳过才能稳定匹配到 `use`。

> 数据流向（衔接 u4）：`VhdlFile` 产出的 `entity`/`usestatements`/`commentLines` 会被 `DutInfo.__init__` 接收——`DutInfo` 在其上归类 libraries、用 `_ParseTags` 解析 `commentLines` 的文件级标签、把 entity 的 generics/ports 连同行尾 `.comment` 的标签组织成可查询模型。这条链路在 u4-l1 会完整展开。

#### 4.5.4 代码实践（本讲核心实践）

**实践目标**：对 `example/simpleTb` 的真实 VHDL 文件实例化 `VhdlFile`，打印实体名、全部 generics（含类型与默认值）、全部 ports（含方向）与全部 use 语句——把本讲五个模块一次性串起来。

**操作步骤**（在仓库根目录运行，确保能 `import VhdlParse` 且能找到示例文件）：

1. 新建临时脚本 `play_vhdlfile.py`（**不要**提交进仓库，跑完即删）：

```python
# 示例代码：用 VhdlFile 读取 simpleTb 示例
from VhdlParse import VhdlFile

vhdl = VhdlFile("example/simpleTb/psi_common_async_fifo.vhd")

# 1) 实体名
print("=== Entity ===")
print(vhdl.entity.name)

# 2) 所有 generics（名字 / 类型 / 默认值）
print("\n=== Generics ===")
for g in vhdl.entity.generics:
    rng = "" if g.type.range is None else f"[range {g.type.range.direction}]"
    print(f"  {g.name:18} : {g.type.name}{rng:20} := {g.default}")

# 3) 所有 ports（名字 / 方向 / 类型）
print("\n=== Ports ===")
for p in vhdl.entity.ports:
    print(f"  {p.name:12} : {p.direction:5} {p.type.name}"
          + (f"({p.type.range.left} {p.type.range.direction} {p.type.range.right})"
             if p.type.range is not None else ""))

# 4) 所有 use 语句
print("\n=== Use statements ===")
for u in vhdl.usestatements:
    print(f"  {u.library}.{u.element}.{u.object}")
```

2. 运行：

```bash
cd <仓库根目录>
python play_vhdlfile.py
```

**需要观察的现象**：

- 实体名应为 `psi_common_async_fifo`。
- generics 应有 6 条：`Width_g/Depth_g/AlmFullOn_g/AlmFullLevel_g/AlmEmptyOn_g/AlmEmptyLevel_g`，类型分别是 `positive/positive/boolean/natural/boolean/natural`，默认值分别是 `16/32/false/28/false/4`。
- ports 应有 20 条，方向为 `in`/`out`；`InData/OutData/InLevel/OutLevel` 是带 `(… downto …)` 范围的 `std_logic_vector`。
- use 语句应有 4 条：`ieee.std_logic_1164.all`、`ieee.numeric_std.all`、`work.psi_common_logic_pkg.all`、`work.psi_common_math_pkg.all`。

**预期结果**（要点）：

```
=== Entity ===
psi_common_async_fifo

=== Generics ===
  Width_g          : positive             := 16
  Depth_g          : positive             := 32
  AlmFullOn_g      : boolean              := false
  AlmFullLevel_g   : natural              := 28
  AlmEmptyOn_g     : boolean              := false
  AlmEmptyLevel_g  : natural              := 4

=== Ports ===
  InClk        : in    std_logic
  ...
  InData       : in    std_logic_vector(Width_g-1 downto 0)
  ...
  InLevel      : out   std_logic_vector(log2ceil(Depth_g) downto 0)
  ...

=== Use statements ===
  ieee.std_logic_1164.all
  ieee.numeric_std.all
  work.psi_common_logic_pkg.all
  work.psi_common_math_pkg.all
```

**若无法确定运行结果**：默认值与范围左右界还原后的精确空格（如 `16 ` 是否带尾空格、`Width_g-1` 是否被插空格）依赖 pyparsing 版本与 `Combine` 行为，可标「待本地验证」；但**实体名、generic/port 的数量、方向、类型名、4 条 use 语句三元组**是确定的，可作为验收依据。若某项与预期不符，先检查本机 pyparsing 版本与示例文件是否被改动。

#### 4.5.5 小练习与答案

**练习 1**：`VhdlFile` 找 entity 时用 `for ... break`，只取第一个。如果一份文件里有两个 entity（VHDL 允许 entity 与 architecture 分文件，但偶尔同一文件里会有多个设计单元），会发生什么？

**参考答案**：只取 `scanString` 命中的第一个 entity，后续 entity 被忽略。对本工具而言通常没问题（一份 DUT 文件一般只描述一个顶层 entity），但若文件里第一个匹配到的不是你想要的顶层，结果会出错——这是使用时需要注意的前提。

**练习 2**：`VhdlFile` 用 `code[s:e]` 重新构造每个构件，而不是直接用 `scanString` 返回的 `tokens`。结合 u3-l1 讲过的 `PrToStr`，说说这样做的好处。

**参考答案**：`tokens` 是 `ParseResults`，若直接拿它存为 `self.code`，经 `PrToStr` 还原会引入多余空格（u3-l1 已验证），导致 `__str__` 输出与原文有差异。改用 `code[s:e]` 切原始子串，`self.code` 就是干净的原文片段，`__str__` 输出更忠于原文。这是对「`PrToStr` 会扰动空格」这一已知特性的规避。

**练习 3**：`VhdlFile` 为何把 Tab 替换成空格（`code.replace("\t", " ")`）？不替换会有什么风险？

**参考答案**：pyparsing 默认按「空白字符集合」跳过空白，Tab 一般也在默认集合里，但某些构件（如 `VhdlUseStatement` 的 `LineStart().leaveWhitespace()`、`VhdlCommentLine` 的 `pp.lineStart`）对行首空白较敏感。统一把 Tab 转空格，可以消除 Tab/空格混排带来的行首定位歧义，让 `use` 必须顶格、行首注释等规则更稳定。

## 5. 综合实践

把本讲五个模块串起来，完成下面这个**贯通任务**：

> **目标**：手写一个微型 VHDL entity 字符串，依次用 `VhdlEntityDeclaration` 解析它，再把结果与「用 `VhdlFile` 读真实示例文件」的结果做对照，验证你对五个模块的理解。

建议步骤：

1. **写一段微型 entity 文本**（含 1 个带范围与默认值的 generic、3 个不同方向的 port、夹一条独立注释），字符串内容自拟，但务必包含一个 `downto` 向量类型和一个带 `:=` 默认值的端口。
2. **用 `VhdlEntityDeclaration` 解析**：打印 `name`、`generics`（名字/类型/默认值）、`ports`（名字/方向/类型/默认值），并验证「夹在中间的独立注释」没有混进 `ports` 列表（4.4 讲的过滤生效）。
3. **验证 `VhdlRange` 的方向语义**：对你写的那个 `downto` 向量端口，打印 `p.type.range.low` 与 `p.type.range.high`，确认 low 取了右界、high 取了左界。
4. **验证 `.comment` 承载标签**：给某个 port 加一行尾注释 `-- $$ TYPE=CLK $$`，解析后打印 `p.comment`，确认它就是 u2 标签的载体。
5. **与真实文件对照**：运行 4.5.4 的脚本读 `example/simpleTb/psi_common_async_fifo.vhd`，把你手写 entity 的解析输出格式与真实文件的输出格式对齐，确认两者用同一套属性（`entity.name`、`entity.generics[].{name,type,default}`、`entity.ports[].{name,direction,type}`、`usestatements[].{library,element,object}`）。

验收标准：

- 能说出从「一段 entity 文本」到「`self.name`/`self.generics`/`self.ports`」经过了哪些构件（`VhdlEntityDeclaration` → `VhdlGenericDeclaration`/`VhdlPortDeclaration` → `VhdlType` → `VhdlRange`）。
- 能解释「为什么独立注释不会污染 ports 列表」（4.4 的命名 + 过滤）。
- 能解释「为什么 `VhdlFile` 能跳过 architecture 直接读到 entity」（`scanString` 只捡匹配片段）。
- 把 4.5.4 的脚本输出作为「标准答案」，与你对真实文件结构的人工预期逐一核对。

> 提示：这道综合实践本质上就是 `DutInfo.__init__` 在做的事的前半段——`DutInfo` 拿到 `VhdlFile` 后，正是遍历 `entity.generics`/`entity.ports` 并对每个 `.comment` 调 `_ParseTags` 来建立标签索引的。做完本实践，你已经站在了 u4 的门口。

## 6. 本讲小结

- `VhdlRange`（[L82-96](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/VhdlParse.py#L82-L96)）解析 `(左 dir 右)`，用 `PP_EXPRESSION` 抓左右界（能吃下含函数调用的复杂表达式），并把方向翻译成 `low`/`high`：`to` 时 low=左、`downto` 时 low=右。`VhdlType`（[L106-121](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/VhdlParse.py#L106-L121)）是「类型名 + 可选括号范围」，无范围时 `range=None`。
- `VhdlGenericDeclaration`（[L123-134](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/VhdlParse.py#L123-L134)）抓「名字/类型/默认值/行尾注释」；`VhdlPortDeclaration`（[L136-148](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/VhdlParse.py#L136-L148)）比它多一个 `PP_DIRECTION` 方向。两者的「默认值取 `[0]`、注释取 `.get('text')`」手法一致，且行尾 `.comment` 正是 u2 `$$ ... $$` 标签进入数据模型的入口。
- `VhdlEntityDeclaration`（[L150-165](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/VhdlParse.py#L150-L165)）按 `entity 名 is [generic(...);] [port(...);] end [entity] [名];` 拼出骨架；generic 子句用 `Suppress` 丢注释、port 子句用「命名 `("port")` + `if getName()=='port'` 过滤」剔除夹在端口间的独立注释；用 `"generics" in parts` 判断子句是否存在以避开 pyparsing 空字符串坑。
- `VhdlFile`（[L168-191](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/VhdlParse.py#L168-L191)）是文件入口，三段式 `scanString`：取第一个 entity（找不到则抛 `Syntax error`）、收集所有 use 语句、收集所有行首注释；它用 `code[s:e]` 切原文重建构件以保留干净 `self.code`，并先把 Tab 转空格以稳定行首匹配。
- 整条解析链是 **`VhdlFile` → `VhdlEntityDeclaration` → (`VhdlGenericDeclaration`/`VhdlPortDeclaration`) → `VhdlType` → `VhdlRange`**，每一步都是 u3-l1「`PP_DEFINITION` + `_Parse`」套路的实例，反复出现「文法里起名 → `_Parse` 里按名取值」的配合。
- `VhdlFile` 产出的 `entity`/`usestatements`/`commentLines` 正是 u4 `DutInfo` 的全部原料：entity 提供 generics/ports/名字，use 语句决定 testbench 引用哪些库包，commentLines 承载文件级标签。

## 7. 下一步学习建议

本讲把「文本 → 可查询模型」这条解析链走完了。下一步建议：

1. **进入 u4-l1「数据模型：DutInfo 与 TbInfo」**：看 `DutInfo.__init__` 如何接收 `VhdlFile`、归类 libraries、遍历 generics/ports 并对每个 `.comment` 调 `_ParseTags` 建立标签索引——本讲的 `entity.generics[i].comment` 在那里被翻译成 u2 讲过的标签字典。
2. **回看 u2 三个讲义的「标签从哪来」**：现在你已经知道 `$$ ... $$` 是从 `VhdlGenericDeclaration.comment`/`VhdlPortDeclaration.comment`（行尾）和 `VhdlFile.commentLines`（文件级）进入程序的，可以重读 u2-l1~u2-l3，把「标签语法/语义」与「标签的物理载体」对上号。
3. **动手扩展（预告 u6-l3）**：等学完 u4 的 `Generate` 主流程，你可以尝试给 `VhdlPortDeclaration` 的行尾注释里加一个自定义标签（如 `$$ INITVAL=1 $$`），并跟踪它如何经 `DutInfo._ParseTags` → `GetPortValue` → `_DutSignals` 影响生成结果。本讲已为你定位了这条链路的「入口」。
4. **可继续精读的源码位置**：若想加深对 pyparsing 的理解，可对比 `VhdlRange`（算 low/high）与 `VhdlRangeFromTo`（不算）的差异（[L82-104](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/VhdlParse.py#L82-L104)），并思考「为什么 entity 文法里 generic 用 `Suppress` 而 port 用命名过滤」（[L152-L153](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/VhdlParse.py#L152-L153)）。
