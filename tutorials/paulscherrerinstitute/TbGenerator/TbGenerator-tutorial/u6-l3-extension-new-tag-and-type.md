# 扩展实践：添加新标签与新 VHDL 类型

## 1. 本讲目标

本讲是全手册的「毕业实践」。前面几讲我们一直以**读者**身份跟随源码走完了「VHDL 注解 → 解析 → 数据模型 → testbench 生成」的完整链路。本讲要换一个视角：以**二次开发者**身份，动手给 TbGenerator 增加一个新特性，并确保它从标签一路贯穿到最终的 VHDL 输出。

学完后你应该能够：

1. 准确说出 TbGenerator 的**三类扩展点**——标签解析层、数据模型层、生成器层——以及它们各自位于哪个文件。
2. 判断一个新需求（新标签 or 新类型）到底要改哪一层，避免「一改就动 parser」的过度修改。
3. 实现一个自定义端口标签（如 `$$ INITVAL=1 $$`）或为 `GetPortValue` 支持一个新 VHDL 类型，并用 `simpleTb` 示例验证生成结果。
4. 理解「不破坏既有行为」的两道安全网：`UnknownVhdlType` 异常的优雅降级，以及 `FilterForTag` 的存在性筛选。

> 重要约定：本讲的所有代码修改都属于**教学示例**，要求你在**自己的工作副本（fork 或本地克隆）**上操作，不要改动你拿来阅读的权威仓库。本讲义标注为「示例代码」的片段都不属于当前仓库，是为你演示扩展手法而写的。

## 2. 前置知识

本讲假设你已经读完以下三讲（本讲直接复用它们建立的术语，不再重复定义）：

- **u2-l2 端口与 generic 标签详解**：你已知 `GetPortValue(port, active)` 是端口初值的单一真相源，`LOWACTIVE` 决定极性，未知类型抛 `UnknownVhdlType`。
- **u3-l2 解析 entity/generic/port 与 VhdlFile 读取**：你已知 `VhdlType` 把类型拆成「名字 + 可选范围」，端口方向由 `VhdlPortDeclaration` 解析，`.comment` 是标签进入数据模型的入口。
- **u4-l3 时钟、复位、进程与控制信号生成**：你已知 `_DutSignals` 遍历端口、按 `TYPE=CLK/RST` 取 active/inactive 初值，并用 `try/except UnknownVhdlType` 兜底。

补充一个贯穿本讲的关键认知：TbGenerator 的数据流是**单向管线**：

```
VHDL 注释中的 $$ ... $$
        │  DutInfo._ParseTags (pyparsing)
        ▼
端口/generic 的 .comment  →  Python 字典 {tag: value}
        │  生成器方法消费 (HasTag / GetTag / FilterForTag / GetPortValue)
        ▼
FileWriter 写出的 testbench .vhd
```

加一个新特性，本质就是在这条管线上**找一个或几个注入点**。本讲要做的事，就是给你一张「注入点地图」。

## 3. 本讲源码地图

| 文件 | 在本讲中的角色 |
|------|---------------|
| [DutInfo.py](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/DutInfo.py) | 标签解析层。`Tags` 常量集中声明合法标签名；`_ParseTags` 把注释翻成字典；`HasTag`/`GetTag`/`FilterForTag` 是查询工具；`GetPortValue` 是初值真相源。**新标签扩展的主战场。** |
| [VhdlParse.py](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/VhdlParse.py) | 数据模型层。`VhdlType` 解析「类型名 + 范围」，`VhdlPortDeclaration` 把类型挂到端口上。判断「要不要动 parser」的关键。 |
| [TbGen.py](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py) | 生成器层。`_DutSignals` 消费 `GetPortValue` 并处理 `UnknownVhdlType` 兜底。**新标签/新类型效果的落地处。** |
| [UtilFunc.py](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/UtilFunc.py) | `VhdlTitle` 输出段标题，生成器各方法都用它打标题，本讲顺带引用以定位段落。 |
| [example/simpleTb/psi_common_async_fifo.vhd](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/example/simpleTb/psi_common_async_fifo.vhd) | 实践用的 DUT。本讲要在它的副本上加标签/加端口来验证扩展效果。 |

## 4. 核心概念与源码讲解

在进入各模块之前，先记住这张「三类扩展点」对照表，它是本讲的总纲：

| 你想加的东西 | 主要改动层 | 改动文件 | 要不要动 pyparsing 文法？ |
|--------------|-----------|----------|--------------------------|
| 一个新**端口/generic 标签**（如 `INITVAL`） | 标签解析层 + 生成器层 | `DutInfo.py`（加常量）+ `TbGen.py`（加消费分支） | **通常不用**，只要标签名是纯字母 |
| 一个新**标量 VHDL 类型**（如 `boolean`、`integer`） | 生成器层 | `DutInfo.py`（`GetPortValue` 加分支） | **不用**，`VhdlType` 已能解析任意标识符 |
| 一个带**新语法**的类型（如 record、二维数组） | 数据模型层 | `VhdlParse.py`（扩 `VhdlType`/新构件） | **要**，需新增文法 |

下面四个最小模块分别打开这张表里的四个关键代码点：`Tags` 常量、`VhdlType`、`GetPortValue`、`_DutSignals`。

### 4.1 Tags 常量：标签的中央注册表

#### 4.1.1 概念说明

`Tags` 是一个普通 Python 类，**只装字符串常量**，集中声明工具认识的所有合法标签名。它的价值不是「强制约束」，而是「单一真相源 + 拼写防错」：

- 生成器方法引用 `Tags.LOWACTIVE` 而不是到处写魔法字符串 `"lowactive"`，改名只动一处。
- 标签按作用对象分三组：**Generic 标签**（`EXPORT`、`CONSTANT`）、**Port 标签**（`LOWACTIVE`、`TYPE`、`CLK`、`FREQ`、`PROC`）、**File-scope 标签**（`PROCESSES`、`TESTCASES`、`DUTLIB`、`TBPKG`）。

一个**关键且容易误解**的点：`Tags` 常量对解析器**没有强制力**。`_ParseTags` 的文法接受**任何**纯字母键（见 4.1.3），哪怕你没把它登记进 `Tags`，它也会被解析进字典。所以「加一个新标签」在解析层往往是**零改动**——你要做的只是登记一个常量，然后在生成器里消费它。

#### 4.1.2 核心流程

新增一个端口标签（以 `INITVAL` 为例）的标准流程：

1. 在 `Tags` 类里加一行常量：`INITVAL = "initval"`。
2. 确认标签名是**纯字母**（`initval` ✓）；若是 `initval2` 含数字，会被解析器静默丢弃（见 4.1.3 的 `Word(alphas)`）。
3. 在某个生成器方法里用 `DutInfo.HasTag(port, Tags.INITVAL)` / `DutInfo.GetTag(port, Tags.INITVAL)` 消费它（见 4.4）。
4. 用 `simpleTb` 跑一次，确认输出符合预期。

注意：`Tags` 常量的值统一是**小写**，因为 `_ParseTags` 会把键名 `.lower()` 后再存（见 4.1.3 末尾），查询时工具方法也会先 `.lower()`。这是大小写不敏感的来源。

#### 4.1.3 源码精读

`Tags` 类的完整定义，按三组分区：

[DutInfo.py:16-32](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/DutInfo.py#L16-L32) —— `Tags` 类集中声明所有合法标签名（均为小写字符串常量），按 Generic/Port/File-scope 分组。

`_ParseTags` 把一段注释解析成字典，是理解「为什么加标签通常不用动 parser」的核心：

[DutInfo.py:93-112](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/DutInfo.py#L93-L112) —— `_ParseTags` 用 pyparsing 把 `$$ key=value; ... $$` 翻译成 `{key: value}` 字典。

其中文法定义在 [DutInfo.py:95-99](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/DutInfo.py#L95-L99)。两个要点：

- **标签名**用 `pp.Word(pp.alphas)` 匹配——**只允许字母**。所以 `initval` 能解析，`initval2` 会因含数字而被跳过（这是 u2-l1 已确立的隐含约束）。
- **值**分单值（`CharsNotIn(";$")`，遇 `;`/`$` 停）与列表（逗号分隔）两种，由 `LIST_VALUE | SINGLE_VALUE` 二选一。

最后在 [DutInfo.py:111](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/DutInfo.py#L111) `tags[tag.get("tag").lower()] = val` —— 键名统一小写后入字典，这就是「标签名大小写不敏感」的实现。

查询侧的三个工具方法（本讲新增标签会用到 `HasTag`/`GetTag`）：

- [DutInfo.py:125-131](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/DutInfo.py#L125-L131) `HasTag` —— 只判存在性。
- [DutInfo.py:133-138](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/DutInfo.py#L133-L138) `GetTag` —— 取值；不存在则抛异常。
- [DutInfo.py:114-123](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/DutInfo.py#L114-L123) `HastTagValue` —— 判「键存在且值相等」（注意源码原拼写 `HastTagValue`，多了一个 `t`，调用时须照抄）。

#### 4.1.4 代码实践

**实践目标**：验证「登记一个新标签常量 + 不动 parser」就能让标签被解析出来。

**操作步骤**（在自己的工作副本上，纯读取式验证，先不改源码）：

1. 在仓库根目录启动 Python，直接调用 `_ParseTags`：
   ```python
   from DutInfo import DutInfo
   print(DutInfo._ParseTags("-- $$ INITVAL=1; TYPE=SIG $$"))
   ```
2. 观察输出里 `initval` 键是否存在、值是什么。

**需要观察的现象**：即使 `Tags` 类里**根本没有** `INITVAL` 常量，`_ParseTags` 也照样把 `initval` 解析进字典。

**预期结果**：`{'initval': '1', 'type': 'SIG'}`。这证实了「解析层接受任意纯字母键」，从而 4.1.2 第 1 步的 `Tags.INITVAL = "initval"` 只是给生成器一个**符号化引用**，而非解析前提。

> 若你愿意进一步动手：给 `Tags` 加一行 `INITVAL = "initval"`，再 `print(Tags.INITVAL)` 确认引用生效。**待本地验证**（取决于本机是否装好 `pyparsing`）。

#### 4.1.5 小练习与答案

**练习 1**：如果把标签写成 `$$ INITVAL2=1 $$`，`_ParseTags` 会得到什么？
**答案**：得到 `{}`（空字典）。因为标签名文法是 `Word(alphas)`，`INITVAL2` 含数字不匹配，整块标签解析失败被跳过。结论：新标签名必须是纯字母。

**练习 2**：为什么 `Tags` 常量的值都写成小写？
**答案**：`_ParseTags` 存字典前对键名 `.lower()`，`HasTag`/`GetTag` 查询前也 `.lower()`。常量用小写可以保证「登记名 == 字典键 == 查询键」三者一致，避免大小写不敏感机制下的对不上号。

---

### 4.2 VhdlType：类型解析与「要不要动 parser」

#### 4.2.1 概念说明

`VhdlType` 负责把一段类型文本拆成「类型名 + 可选范围」，例如：

- `std_logic` → 名字 `std_logic`，无范围。
- `std_logic_vector(7 downto 0)` → 名字 `std_logic_vector`，范围 `low=0, high=7`（`downto` 时 low 取右界，见 u3-l2）。
- `boolean` → 名字 `boolean`，无范围。

对本讲最重要的结论：**`VhdlType` 的类型名用的是通用标识符文法 `PP_IDENTIFIER`，能匹配任意字母/数字/下划线组成的标识符**。所以 `boolean`、`integer`、`unsigned` 这些「新类型」**根本不需要改 parser**——它们已经被正确解析成 `name='boolean', range=None`。真正需要你动手的，是下游 `GetPortValue` 对 `name` 的分派（见 4.3）。

只有当类型的**语法形式**是 parser 没见过的（比如 VHDL record、二维数组、带 `range ...` 的子类型），才需要回头扩 `VhdlParse.py` 的文法。本讲的两个实践场景都不涉及，所以 parser 全程不动。

#### 4.2.2 核心流程

判断「支持一个新类型要不要动 parser」的决策树：

```
新类型的源码写法，VhdlType 现有文法能解析吗？
        │
   ┌────┴────┐
   能         不能（record / 二维数组 / 新语法）
   │         │
   ▼         ▼
只改 GetPortValue   先扩 VhdlParse（VhdlType 或新增构件）
（加一个 elif）     再改 GetPortValue
```

本讲实践走左边那条路：`boolean` 能被 `PP_IDENTIFIER` 直接吃下，所以只改 `GetPortValue`。

#### 4.2.3 源码精读

[VhdlParse.py:106-121](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/VhdlParse.py#L106-L121) —— `VhdlType`：`PP_DEFINITION` 由「标识符 + 可选 `(范围)` + 可选 `range ...`」组成；`_Parse` 把名字存进 `self.name`、范围存进 `self.range`（无范围则为 `None`）。

注意它的文法构件：

- [VhdlParse.py:26](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/VhdlParse.py#L26) `PP_IDENTIFIER = pp.Word(pp.alphanums+"_")` —— 类型名允许字母/数字/下划线，所以 `boolean`、`unsigned`、`t_my_array` 都能匹配。
- [VhdlParse.py:30](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/VhdlParse.py#L30) `PP_RANGEDIR` —— `to`/`downto` 方向关键字。

`VhdlType` 最终挂在端口上，是 `GetPortValue` 的分派依据。挂载点在端口构件里：

[VhdlParse.py:136-148](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/VhdlParse.py#L136-L148) —— `VhdlPortDeclaration` 解析端口；其中 [VhdlParse.py:141](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/VhdlParse.py#L141) `self.type = VhdlType(parts.get("type"))` 把类型文本构造成 `VhdlType` 对象。后续 `GetPortValue` 读取的 `port.type.name` 就是这里存的 `self.name`。

#### 4.2.4 代码实践

**实践目标**：确认 `boolean` 类型能被现有 parser 正确解析，无需改 `VhdlParse.py`。

**操作步骤**：

1. 用 Python 直接构造一个 `VhdlType`：
   ```python
   from VhdlParse import VhdlType
   t1 = VhdlType("boolean")
   t2 = VhdlType("std_logic_vector(7 downto 0)")
   print(t1.name, t1.range)   # 预期 boolean None
   print(t2.name, t2.range.low, t2.range.high)  # 预期 std_logic_vector 0 7
   ```

**需要观察的现象**：`boolean` 被解析成 `name='boolean', range=None`，与 `std_logic` 的解析结构完全同构。

**预期结果**：第一行打印 `boolean None`。这证明新增标量类型**不需要动 parser**——下游只要像对待 `std_logic` 一样，按 `port.type.name` 分派即可。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`unsigned(15 downto 0)` 会被解析成什么？
**答案**：`name='unsigned'`，`range.low=0`、`range.high=15`（`downto` 时 low 取右界）。结构上和 `std_logic_vector` 一样。

**练习 2**：如果要让工具支持 VHDL `type` 声明里的 record 类型作为端口，要改哪一层？
**答案**：要改**数据模型层** `VhdlParse.py`——record 的语法（`record ... end record`）是 `VhdlType` 现有文法不认识的，必须先扩 parser。这超出本讲两个实践场景的范围，但决策树（4.2.2）能帮你定位。

---

### 4.3 GetPortValue：初值的单一真相源

#### 4.3.1 概念说明

`GetPortValue(port, active)` 是「端口的 VHDL 初始字面量」的**唯一计算点**。它被 `_DutSignals`、`_Resets`、`_Processes`、`_TbControl` 共同复用（u2-l2 已指出），所以**改这一处，多处输出一致变化**——这正是把它设计成单一真相源的好处。

它的两个输入维度：

- **`active: bool`**：调用方表达「我想要这个端口处于有效还是无效状态」。例如时钟/复位信号取「有效」初值（对齐上升沿），普通信号取「无效」。
- **`LOWACTIVE` 标签**：把「有效/无效」的逻辑意图翻译成具体的 `'1'`/`'0'`。

两者合成的极性真值表：

| `LOWACTIVE` 标签 | `active` 参数 | 返回的 `initVal`（std_logic） |
|------------------|---------------|-------------------------------|
| 无 / `false` | `True` | `'1'` |
| 无 / `false` | `False` | `'0'` |
| `true` | `True` | `'0'` |
| `true` | `False` | `'1'` |

随后按 `port.type.name` 分派：`std_logic` 直接返回 `initVal`；`std_logic_vector` 包成 `(others => initVal)`；**其它类型一律抛 `UnknownVhdlType`**。

这个「其它类型抛异常」就是**本讲扩展新类型的注入点**：只要在这里加一个 `elif`，新类型就获得了初值能力。

#### 4.3.2 核心流程

为 `boolean` 类型增加初值支持的流程：

1. 确认 parser 已能解析 `boolean`（4.2 已验证，无需改 parser）。
2. 在 `GetPortValue` 的类型分派链里，于 `else: raise` 之前插入：
   ```python
   elif port.type.name == "boolean":
       return "true" if initVal == "'1'" else "false"
   ```
   即把 std_logic 的 `'1'`/`'0'` 映射成 VHDL boolean 的 `true`/`false`。
3. 用一个带 `boolean` 端口的 DUT 副本生成 TB，验证输出。

注意映射的语义：`active=True` 对应 std_logic 的 `'1'`，对应 boolean 的 `true`，保持「有效 = 真」的一致直觉。

#### 4.3.3 源码精读

[DutInfo.py:68-79](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/DutInfo.py#L68-L79) —— `GetPortValue` 完整实现：先用 `LOWACTIVE` 算出 `initVal`，再按 `port.type.name` 分派。

关键两段：

- [DutInfo.py:70-73](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/DutInfo.py#L70-L73) —— 极性计算：`LOWACTIVE=true` 时有效/无效的 `'1'`/`'0'` 成对翻转。
- [DutInfo.py:74-79](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/DutInfo.py#L74-L79) —— 类型分派：`std_logic` 裸返，`std_logic_vector` 包 `(others => ...)`，其余抛 `UnknownVhdlType`。**新类型扩展点就在 `L78-L79` 的 `else` 之前。**

异常类本身定义在文件顶部 [DutInfo.py:13](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/DutInfo.py#L13) `class UnknownVhdlType(Exception): pass`。它不只是报错，更是 `_DutSignals` 优雅降级的信号（见 4.4）。

#### 4.3.4 代码实践

**实践目标**：在工作副本上为 `GetPortValue` 增加 `boolean` 支持，并理解「改前」的行为。

**操作步骤**（先观察现状，再改）：

1. 复制 `example/simpleTb/psi_common_async_fifo.vhd` 到你的实验目录，在 port 区加一个 boolean 输出端口：
   ```vhdl
   -- 示例代码：在 port ( ) 内新增一行
   TestFlag : out boolean   -- $$ PROC=Input $$
   ```
2. **不改 `GetPortValue`**，先生成一次 TB，打开生成的 `*_tb.vhd`，定位 `-- *** DUT Signals ***` 段，看 `TestFlag` 那行。
3. 然后在 `GetPortValue` 的 `else` 之前加入 4.3.2 的 `boolean` 分支，重新生成，再看同一行。

**需要观察的现象**：

- 改前：`signal TestFlag : boolean;`（**无初值**）——因为 `GetPortValue` 抛 `UnknownVhdlType`，被 `_DutSignals` 的 `try/except` 吞掉，`default` 退化为空串（见 4.4）。注意：这仍然是**合法 VHDL**，工具不会崩。
- 改后：`signal TestFlag : boolean := false;`（输出端口、非 clk/rst，取 inactive → `false`）。

**预期结果**：改前那行没有 `:=`，改后那行有 `:= false`。**待本地验证**（需要本机有 PsiPyUtils、pyparsing 且能运行 `TbGen.py`）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `boolean` 类型在改 `GetPortValue` 之前不会让生成报错退出？
**答案**：因为 `_DutSignals` 用 `try/except UnknownVhdlType` 包住了 `GetPortValue` 调用，异常被捕获后 `default = ""`，于是写出无初值的合法信号声明。`UnknownVhdlType` 同时承担「硬报错」与「优雅降级」两种角色，取决于调用方有没有 catch。

**练习 2**：若要让 `integer` 端口的初值为 `0`，`GetPortValue` 该加什么分支？
**答案**：加 `elif port.type.name == "integer": return "0"`（忽略 `active`，因为整数没有明确的「有效/无效」极性，直接给一个安全的 `0`）。这也说明：并非所有类型都要复用 `initVal`，`active` 语义对非逻辑类型可以不生效。

---

### 4.4 _DutSignals：生成器消费扩展点

#### 4.4.1 概念说明

`_DutSignals` 是 `_DutSignals` 段（生成的 TB 里 `-- *** DUT Signals ***` 之下）的写作器。它遍历所有端口，为每个端口写一行 `signal <name> : <type> := <初值>;`。它是 `GetPortValue` 最集中的消费者，也是**新标签效果最直接的落地处**。

它的三条初值规则（u4-l3 已建立）：

- `TYPE=RST` 端口 → 取 **active** 初值（复位一开始就有效）。
- `TYPE=CLK` 端口 → 取 **active** 初值（注释说「让时钟起始就对齐上升沿」）。
- 其它端口 → 取 **inactive** 初值。
- 任何端口若 `GetPortValue` 抛 `UnknownVhdlType` → `default = ""`，写出无初值的信号。

新增 `INITVAL` 标签的扩展点，就是在最前面**插入一条最高优先级分支**：凡是带 `INITVAL` 的端口，直接用标签指定的初值，绕过 `active`/`LOWACTIVE` 逻辑。这条分支只动 `_DutSignals` 一个方法、用现成的 `HasTag`/`GetTag`，不影响时钟/复位/进程的初值逻辑——**作用域可控、不破坏既有行为**。

#### 4.4.2 核心流程

`_DutSignals` 对每个端口的决策流程（虚线框为本讲新增的 `INITVAL` 分支）：

```
对每个端口 sig:
  ┌─ 有 INITVAL 标签? ──→ default := GetTag(sig, INITVAL) 翻译后的字面量   【新增】
  ├─ TYPE=RST ?        ──→ default := GetPortValue(sig, active=True)
  ├─ TYPE=CLK ?        ──→ default := GetPortValue(sig, active=True)
  └─ 其它              ──→ default := GetPortValue(sig, active=False)
  任意上述 GetPortValue 抛 UnknownVhdlType ──→ default := ""（降级）
写出: signal <sig.name> : <str(sig.type)><default>;
```

把 `INITVAL` 放在最高优先级，意味着它对**任意类型**的端口都生效（只要你给了合法字面量），并且会覆盖 `TYPE` 与 `LOWACTIVE` 的默认推断——这是「用户显式指定优先」的合理语义。

#### 4.4.3 源码精读

[TbGen.py:176-190](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L176-L190) —— `_DutSignals` 完整实现：遍历端口，按 `TYPE` 选 active/inactive，调 `GetPortValue`，最后写一行信号声明。

关键三处：

- [TbGen.py:180-188](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L180-L188) —— `try/except` 块：三条 `TYPE` 分支 + `except UnknownVhdlType: default = ""`。**`INITVAL` 新分支要插在 L181 的 `if` 之前。**
- [TbGen.py:182](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L182) 与 [TbGen.py:184](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L184) —— 时钟/复位取 `active=True`；[TbGen.py:186](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L186) —— 其余取 `active=False`。
- [TbGen.py:187-188](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L187-L188) —— 优雅降级：`except UnknownVhdlType: default = ""`。
- [TbGen.py:189](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L189) —— 最终输出行 `"signal {} : {}{};"`，`str(sig.type)` 负责把类型（含范围）还原成文本（依赖 `VhdlType.__str__`，见 4.2.3）。

段落标题由 [UtilFunc.py:10-19](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/UtilFunc.py#L10-L19) `VhdlTitle(..., level=2)` 打出 `-- *** DUT Signals ***`，便于你在生成的 TB 里快速定位验证点。

下面是**示例代码**（不属于当前仓库），展示 `INITVAL` 扩展的最小改法：

```python
# 示例代码：在 _DutSignals 的 try 块最前面插入 INITVAL 分支
def _DutSignals(self, f : FileWriter) -> FileWriter:
    VhdlTitle("DUT Signals", f, 2)
    for sig in self.dutInfo.ports:
        try:
            if DutInfo.HasTag(sig, Tags.INITVAL):            # 【新增】用户显式指定优先
                raw = DutInfo.GetTag(sig, Tags.INITVAL).strip()
                literal = "'1'" if raw == "1" else "'0'"      # 把 1/0 翻成 std_logic 字面量
                default = " := " + literal
            elif DutInfo.HastTagValue(sig, Tags.TYPE, "rst"):
                default = " := " + self.dutInfo.GetPortValue(sig, True)
            elif DutInfo.HastTagValue(sig, Tags.TYPE, "clk"):
                default = " := " + self.dutInfo.GetPortValue(sig, True)
            else:
                default = " := " + self.dutInfo.GetPortValue(sig, False)
        except UnknownVhdlType:
            default = ""
        f.WriteLn("signal {} : {}{};".format(sig.name, str(sig.type), default))
    return f
```

注意它**只用了现成 API**（`HasTag`/`GetTag`/`HastTagValue`/`GetPortValue`）和现成异常类型，没有引入新依赖；标签名 `initval` 是纯字母，parser 无需改动（4.1.3）。

#### 4.4.4 代码实践

**实践目标**：在工作副本上完成 `INITVAL` 扩展，并对一个具体端口预测 + 验证生成结果。

**操作步骤**：

1. 在 `Tags` 类加常量（4.1.2 第 1 步）：`INITVAL = "initval"`。
2. 按上面示例代码修改 `_DutSignals`。
3. 在 DUT 副本里挑一个普通 `std_logic` 输出端口（如 [psi_common_async_fifo.vhd:55](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/example/simpleTb/psi_common_async_fifo.vhd#L55) `InFull : out std_logic`），把行尾注释改成 `-- $$ INITVAL=1 $$`。
4. 运行 `py TbGen.py -src .\psi_common_async_fifo.vhd -dst .\tb -clear -force`（等价于 `example/simpleTb/run.bat`）。
5. 打开生成的 `tb/psi_common_async_fifo_tb.vhd`，在 `-- *** DUT Signals ***` 段找 `InFull` 行。

**需要观察的现象**：

- 改前（无 `INITVAL`）：`InFull` 是普通输出端口 → inactive → `signal InFull : std_logic := '0';`。
- 改后（`INITVAL=1`）：被新分支截获 → `signal InFull : std_logic := '1';`。
- 其它没有 `INITVAL` 标签的端口（如 `InEmpty`）**完全不变**，仍是 `:= '0'`——证明扩展是「opt-in」、不破坏既有行为。

**预期结果**：仅 `InFull` 那一行的初值由 `'0'` 变 `'1'`，其余 DUT 信号行逐字符不变。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：如果用户给一个 `std_logic_vector` 端口写 `$$ INITVAL=1 $$`，上面的示例代码会生成什么？合理吗？
**答案**：会生成 `signal X : std_logic_vector(...) := '1';`——**不合理**，因为向量信号不能用标量 `'1'` 赋初值（应为 `(others => '1')`）。这说明示例代码是为 `std_logic` 设计的；要让 `INITVAL` 通用于向量，需按 `port.type.name` 分支包装字面量，或直接让用户写 `$$ INITVAL=(others => '1') $$` 做字面量透传（见综合实践的进阶选项）。

**练习 2**：为什么把 `INITVAL` 分支放在 `try` 块**内部**、且在最前面？
**答案**：放最前面是为了「用户显式指定优先于 TYPE/LOWACTIVE 推断」；放 `try` 内部是为了——万一未来 `GetTag` 或字面量翻译抛错，仍能被 `except UnknownVhdlType` 之外的处理捕获并保持降级语义一致（当前示例不会抛 `UnknownVhdlType`，但留在 `try` 内符合原代码的作用域惯例，便于阅读）。

---

## 5. 综合实践

把本讲四个模块串起来，完成一个**端到端**的扩展：让 std_logic 端口支持 `$$ INITVAL=1 $$` 指定初值，并在 `simpleTb` 上验证。

**任务**：在你的工作副本上，按下列清单完成改造，并在每一处用本讲给的源码行号定位。

1. **标签层（4.1）**：在 [DutInfo.py:16-32](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/DutInfo.py#L16-L32) 的 `Tags` 类里新增常量 `INITVAL = "initval"`。确认标签名纯字母，**不动 `_ParseTags`**。
2. **类型层（4.2）**：确认本场景**无需改动** `VhdlParse.py`——`INITVAL` 不涉及新 VHDL 类型语法。把这一步显式写成「评估后：跳过」，体会决策树（4.2.2）的价值。
3. **生成器层（4.4）**：按 4.4.3 的示例代码修改 [TbGen.py:176-190](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/TbGen.py#L176-L190) `_DutSignals`，插入 `INITVAL` 最高优先级分支。
4. **验证**：在 [psi_common_async_fifo.vhd:55](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/example/simpleTb/psi_common_async_fifo.vhd#L55) 的 `InFull` 行尾注释改成 `-- $$ INITVAL=1 $$`，运行生成，确认 `signal InFull : std_logic := '1';` 且其它信号不变。

**进阶选项（二选一）**：

- **A. 让 `INITVAL` 支持向量类型**：在 `_DutSignals` 新分支里按 `sig.type.name` 分派——`std_logic` 用 `'1'/'0'`，`std_logic_vector` 包装成 `(others => '1')`。给 `InData : in std_logic_vector`（[psi_common_async_fifo.vhd:45](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/example/simpleTb/psi_common_async_fifo.vhd#L45)）加 `$$ INITVAL=1 $$`，验证生成 `(others => '1')`。
- **B. 改走「新类型」路线（4.3）**：撤掉 `INITVAL` 改动，转而为 `GetPortValue` 增加 `boolean` 支持（4.3.2），在 DUT 副本加一个 boolean 端口验证 `:= false`。注意这需要让 boolean 端口**不被** `INITVAL` 分支截获，体会两条扩展路线的区别。

**验收标准**：

- 改动只涉及「`Tags` 加一行常量 + `_DutSignals` 加一个分支」（A 选项再加类型分派），**不动 `VhdlParse.py`、不动 `_Clocks`/`_Resets`/`_Processes`**。
- 生成结果中，**只有带 `INITVAL` 标签的端口行发生变化**，其余输出逐字符不变——这是「不破坏既有行为」的硬指标。
- 若改动了 parser 或其它生成器方法，说明你的扩展作用域失控了，回看 4.2.2 决策树。

> 所有运行结果**待本地验证**：取决于本机是否安装 `PsiPyUtils`、`pyparsing`，以及 `TbGen.py` 能否被 `py`/`python` 正确调用。`example/simpleTb/run.bat` 是 Windows 下的单行封装，Linux/macOS 下用等价的 `python TbGen.py -src ... -dst ... -clear -force`。

## 6. 本讲小结

- TbGenerator 有**三类扩展点**：标签解析层（`DutInfo.py` 的 `Tags`/`_ParseTags`/查询工具）、数据模型层（`VhdlParse.py` 的 `VhdlType`）、生成器层（`TbGen.py` 的 `_DutSignals` 与 `DutInfo.py` 的 `GetPortValue`）。
- 加一个**新标签**通常**不动 parser**：`_ParseTags` 接受任意纯字母键，你只需在 `Tags` 登记常量 + 在某个生成器方法消费它（`HasTag`/`GetTag`）。
- 加一个**新标量类型**（如 `boolean`）**不动 parser**：`VhdlType` 用通用 `PP_IDENTIFIER` 已能解析，只需在 `GetPortValue` 的类型分派链加一个 `elif`。
- 只有类型**语法形式**是 parser 没见过的（record、二维数组等），才需要回头扩 `VhdlParse.py` 文法——这是「要不要动 parser」决策树的关键判据。
- `GetPortValue` 是初值的**单一真相源**，改一处多处一致变化；`LOWACTIVE` 决定极性，`active` 参数决定有效/无效。
- 不破坏既有行为的两道安全网：`_DutSignals` 的 `try/except UnknownVhdlType` 优雅降级，以及「新分支只对带新标签的端口生效、其余逐字符不变」的 opt-in 设计。

## 7. 下一步学习建议

- **回归测试**：本仓库目前没有自动化测试。学完本讲后，建议你为一个扩展（如 `INITVAL`）写一个最小的 Python 断言脚本——对示例 DUT 跑 `ReadHdl` + `Generate`，再用字符串匹配检查输出行，体会「TbGenerator 缺少测试」这个二次开发风险点。
- **深入多用例链路**：本讲的扩展都落在单用例路径（`_DutSignals`）。如果你把 `INITVAL` 用到 [example/multiCaseTb](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/example/multiCaseTb) 的 DUT 上，会发现初值同样生效——但要理解 case 包 procedure 签名如何受 `PROC` 影响，需回看 u5-l2 的 `PortDirectionForProcedure`。
- **阅读 `MultiFileTb.py`**：本讲没动它，但它是又一个生成器层扩展点。`WriteTbPkg`/`WriteCasePkg` 的写法与 `_DutSignals` 同构（接收并返回 `FileWriter`），可作为「新增一种生成段落」的范本。
- **跨层贯通挑战**：尝试实现一个**真正需要动 parser** 的小特性——例如支持 `subtype` 声明的端口类型，借此跑通 4.2.2 决策树右侧那条「先扩 `VhdlParse`、再改 `GetPortValue`」的完整路径。
