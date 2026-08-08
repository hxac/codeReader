# IR 运算符体系

## 1. 本讲目标

上一讲（u3-l1）我们把 XLS IR 建立成「数据流图 + 类型化值」的静态心智模型：`Package` 是容器，`Function` 是可计算单元，`Node` 是图的顶点，`Value`/`Bits` 是运行期内容。但那讲刻意回避了一个最基本的问题——**图里的每个 `Node` 到底在「算什么」？**

答案就是本讲的主角：**运算符（Op）**。本讲学完后，你应当能够：

1. 说清楚 `Op` 这个枚举从哪里来、由哪一处「唯一真相」生成。
2. 把全部 79 个运算符按类别（算术 / 比较 / 位运算 / 选择 / 数组与元组 / 副作用）归类，并能判断一个运算符是否可结合、可交换、有副作用。
3. 理解「从 `Op` 枚举值定位到具体的 `Node` 子类」这条反向映射链，看懂解析器为什么能用一个 `IsOpClass<T>()` 模板把文本里的 `add`、`eq` 分派到不同的构造路径。
4. 手写一段同时包含 `kAdd`、`kEq`、`kArrayIndex` 的 IR 文本，并用工具验证它能被正确解析。

## 2. 前置知识

- **上一讲的 IR 三层结构**：`Package → Function/Proc/Block → Node`，`Node` 靠 `operands`（入边）与 `users`（出边）表达数据依赖。本讲只关注「单个 `Node` 内部」的属性，不再展开图结构。
- **SSA（静态单赋值）**：每个 `Node` 产出一次、可被多个后继引用，所以「运算」本身是无状态的，运算的语义完全由 `Op` 标签 + 操作数 + 少量属性决定。
- **位（bits）类型**：XLS 的核心数据是任意位宽的位串；算术 / 比较运算的「符号性」（有符号 / 无符号）由运算符本身决定，而不是由值的类型决定（回忆 u3-l1：`Bits` 自身不记符号性）。
- **位掩码（bitmap）**：用一个整数的不同二进制位表示若干个「是 / 否」属性，再用按位或 `|` 组合、按位与 `&` 检测。本讲会用它来描述运算符类别。

> 一个值得先记住的直觉：**XLS 的运算符体系是「一份宏定义，派生万物」**。下文会反复回到这一点。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [xls/ir/op_list.h](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/op_list.h) | **唯一真相**：用一个宏 `XLS_FOR_EACH_OP_TYPE` 列出全部运算符及其四元组属性，同时定义类别位掩码。 |
| [xls/ir/op.h](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/op.h) | 从宏派生出 `Op` 枚举、`kAllOps` 数组、各类属性查询函数声明，以及关键的 `IsOpClass<T>()` 模板。 |
| [xls/ir/op.cc](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/op.cc) | 实现字符串 ↔ `Op`、proto ↔ `Op` 转换，以及「按位掩码检测类别」的属性函数。 |
| [xls/ir/nodes.h](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/nodes.h) | 定义每个 `Node` 子类（`BinOp`、`CompareOp`、`ArrayIndex` …），每个子类用一个 `static constexpr kOps` 数组声明「我负责哪些 `Op`」。 |
| [xls/ir/ir_parser.cc](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/ir_parser.cc) | IR 文本解析器：把 `.ir` 文本里的运算符字符串，经 `IsOpClass<T>()` 分派到对应的 `Node` 构造路径。 |

本讲的三个最小模块——**Op 枚举**、**运算符分类**、**Node 子类**——恰好对应「宏定义 → 类别属性 → 反向映射到节点类」这条主线。

## 4. 核心概念与源码讲解

### 4.1 Op 枚举：一份宏定义，派生万物

#### 4.1.1 概念说明

每个 `Node` 都带一个 `Op` 标签，回答「我在执行哪一种运算」。它是 `int8_t` 的强类型枚举：

```cpp
enum class Op : int8_t { ... };
```

`Op` 的取值是**封闭且固定**的——它就是 XLS 内置的全部运算种类，目前共 **79 个**。你不需要记住这 79 个名字，但要记住一个更重要的工程事实：**这 79 个名字、它们的字符串拼写、它们的 proto 枚举名、它们的类别属性，全部来自同一个宏 `XLS_FOR_EACH_OP_TYPE`**。

这样设计的好处是：新增一个运算符时，只改一处（宏），枚举、序列化、字符串转换、类别查询会同时自动更新，绝不会出现「枚举里加了、字符串表里忘了」这类不一致。

#### 4.1.2 核心流程

宏的每一项是一个四元组：

\[
\texttt{(枚举名,\ proto 名,\ IR 文本串,\ 类别位掩码)}
\]

例如加法是 `(kAdd, OP_ADD, "add", kAssociative | kCommutative)`。把宏喂给不同的「展开宏」`F`，就能派生出不同的产物：

```text
XLS_FOR_EACH_OP_TYPE(F)
   │
   ├── F = MAKE_ENUM         →  enum class Op { kAdd, kAfterAll, ... }      （op.h）
   ├── F = MAKE_ENUM_REF     →  kAllOps = { Op::kAdd, Op::kAfterAll, ... } （op.h）
   ├── F = TO_OP_STRING      →  OpToString：kAdd → "add"                    （op.cc）
   ├── F = FROM_OP_STRING    →  StringToOp："add" → kAdd                    （op.cc）
   ├── F = TO_OP_PROTO       →  ToOpProto：kAdd → OP_ADD                    （op.cc）
   └── F = TO_OP_TY          →  GetOpTypes：kAdd → 类别位掩码               （op.cc）
```

这是一条**自上而下的单向派生链**：上游改一处，下游全部跟着变。

#### 4.1.3 源码精读

先看「唯一真相」本身——宏 `XLS_FOR_EACH_OP_TYPE`。注意它的项是按**枚举名字典序**排列的（`kAdd`、`kAfterAll`、`kAnd`…），这种规整排列让你能按字母快速定位某个运算符：

[xls/ir/op_list.h:L32-L48](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/op_list.h#L32-L48) —— 宏的开头几项，能看到四元组的完整结构，例如 `kAdd` 的类别是 `kAssociative | kCommutative`。

再看 `op.h` 如何从这个宏生成枚举与 `kAllOps`：

[xls/ir/op.h:L34-L45](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/op.h#L34-L45) —— 把宏的「第一字段」抽出来生成 `enum class Op`，再把每个枚举值收集进 `kAllOps` 数组（`kAllOps` 常用于「遍历全部运算符」的测试与校验）。

最后看 `op.cc` 里字符串转换的实现——同一个宏，换个展开方式即可：

[xls/ir/op.cc:L60-L83](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/op.cc#L60-L83) —— `OpToString` 取「第三字段」（IR 文本串，如 `"add"`）；`StringToOp` 反向建一张 `string → Op` 的哈希表。你在 `.ir` 文件里看到的 `add(...)`、`eq(...)`、`array_index(...)` 这些小写助记符，就是从这里来的。

#### 4.1.4 代码实践（源码阅读型）

**实践目标**：亲手验证「四元组 → 各种派生物」的对应关系。

**操作步骤**：

1. 打开 [xls/ir/op_list.h](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/op_list.h)，找到 `kShll` 这一行，读出它的四元组。
2. 打开 [xls/ir/op.cc 的 OpToString / StringToOp](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/op.cc#L60-L83)，确认 `kShll` 的第三字段会被用作 IR 文本串。
3. 在任意一个 `.ir` 文件（如 `xls/ir/testdata/ir_parser_round_trip_test_ParseTwoPlusTwo.ir`）里，确认出现的运算符单词与宏的第三字段完全一致。

**需要观察的现象**：`kShll` 行形如 `F(kShll, OP_SHLL, "shll", op_types::kStandard)`。

**预期结果**：

- 枚举名：`kShll`
- proto 名：`OP_SHLL`
- IR 文本串：`"shll"`（所以在 `.ir` 里写左移逻辑移位应写作 `shll(...)`）
- 类别位掩码：`kStandard`（即没有任何特殊类别标记）

**待本地验证**：你可以在仓库里 `grep -rn "shll" xls/ir/testdata/` 看是否真有 `.ir` 用例使用了这个字符串。

#### 4.1.5 小练习与答案

**练习 1**：`kUDiv`（无符号除法）的 IR 文本串和 proto 名分别是什么？

**参考答案**：在 [op_list.h:L114](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/op_list.h#L114) 可查到 `F(kUDiv, OP_UDIV, "udiv", ...)`，故 IR 文本串是 `"udiv"`，proto 名是 `OP_UDIV`。

**练习 2**：如果你想新增一个运算符 `kFoo`，需要修改几个文件里的「运算符清单」？

**参考答案**：**一处**——只需在 `XLS_FOR_EACH_OP_TYPE` 宏（`op_list.h`）里加一行 `F(kFoo, OP_FOO, "foo", ...)`。枚举、`kAllOps`、字符串/proto 转换、类别查询会全部自动派生。当然，还要去 `nodes.h` 给它安排一个 `Node` 子类（见 4.3），但「运算符清单」本身只此一处。

---

### 4.2 运算符分类：用一个位掩码描述五类属性

#### 4.2.1 概念说明

光知道「`kAdd` 是加法」还不够。优化 Pass 常常需要问更抽象的问题：这个运算**可结合吗**？**可交换吗**？**是比较运算吗**？**有副作用吗**？这些属性决定了 Pass 能做什么图变换——例如「可结合 + 可交换」意味着可以任意重排、合并操作数；「有副作用」的节点不能被死代码消除（DCE）随意删除。

XLS 把这些「是 / 否」属性编码进一个 **8 位类别位掩码**，作为宏四元组的第四字段。一共定义了 6 个位标志：

| 标志 | 含义 | 举例 |
| --- | --- | --- |
| `kStandard` | 无任何特殊类别（值就是 0） | `kSub`、`kNeg` |
| `kComparison` | 比较运算，输出 1 位 | `kEq`、`kULt` |
| `kAssociative` | 可结合：(a∘b)∘c == a∘(b∘c) | `kAdd`、`kAnd` |
| `kCommutative` | 可交换：a∘b == b∘a | `kAdd`、`kEq` |
| `kBitWise` | 按位逻辑运算 | `kAnd`、`kOr`、`kNot` |
| `kSideEffecting` | 有副作用，不能随意移动/删除 | `kAssert`、`kSend`、`kParam` |

一个运算符可以同时具有多个属性，用按位或 `|` 组合。

#### 4.2.2 核心流程

检测某个属性的方法是经典的「按位与」：

\[
\text{HasProperty}(op) \;=\; \bigl(\,\text{bitmap}(op)\ \&\ \text{mask}\,\bigr)\ ==\ \text{mask}
\]

设位掩码的第 \(i\) 位为 \(b_i\)（\(i \in \{0,\dots,5\}\) 对应上表六类），则运算符 \(op\) 的类别向量 \(\mathbf{b}(op) \in \{0,1\}^6\)，且

\[
\text{bitmap}(op) = \sum_{i=0}^{5} b_i(op)\cdot 2^{i}.
\]

例如 `kOr` 的位掩码是 `kBitWise | kCommutative | kAssociative`，即第 3、2、1 位为 1：

\[
\text{bitmap}(kOr) = 2^3 + 2^2 + 2^1 = 8 + 4 + 2 = 14.
\]

于是 `OpIsBitWise(kOr)` 计算 \(14\ \&\ 8 = 8 == 8\)，为真。

属性函数对每个 `Op` 只查表一次（编译期常量），没有任何运行期开销。

#### 4.2.3 源码精读

位标志定义在 `op_list.h` 的 `namespace op_types` 里，每个是一个 `uint8_t` 常量，占一个二进制位：

[xls/ir/op_list.h:L21-L28](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/op_list.h#L21-L28) —— 六个位标志定义。注意 `kStandard = 0`，意味着「标准」运算只是「没有别的标记」的默认态。

`op.cc` 里先用宏取出每个 `Op` 的位掩码，再用按位与实现五个属性查询函数：

[xls/ir/op.cc:L85-L115](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/op.cc#L85-L115) —— `GetOpTypes` 仍是「宏换一种展开」得到每个 `Op` 的位掩码；`OpIsCompare`/`OpIsAssociative`/`OpIsCommutative`/`OpIsBitWise`/`OpIsSideEffecting` 五个函数，每个都是同一句「按位与后比较」。

在 `op.h` 里这五个函数的声明集中在一起，是公开 API：

[xls/ir/op.h:L61-L74](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/op.h#L61-L74) —— 五个属性查询函数的声明，注释里各举了一个例子（如 `OpIsAssociative` 注释提到 `kAdd`、`kOr`）。

#### 4.2.4 代码实践（分类实操型）

**实践目标**：凭位掩码把代表性运算符正确归类，并能预测属性函数的返回值。

**操作步骤**：

1. 打开 [xls/ir/op_list.h 的宏清单](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/op_list.h#L32-L126)，逐项填写下表第 2 列。
2. 对每一项，套用 4.2.2 的公式，预测五个属性函数的取值（第 3–7 列）。

| 运算符 | 位掩码（第四字段） | Compare? | Associative? | Commutative? | BitWise? | SideEffecting? |
| --- | --- | --- | --- | --- | --- | --- |
| `kAdd` | `kAssociative \| kCommutative` | 否 | 是 | 是 | 否 | 否 |
| `kSub` | `kStandard` | 否 | 否 | 否 | 否 | 否 |
| `kAnd` | `kAssociative \| kCommutative \| kBitWise` | 否 | 是 | 是 | 是 | 否 |
| `kNand` | `kBitWise \| kCommutative` | 否 | 否 | 是 | 是 | 否 |
| `kEq` | `kComparison \| kCommutative` | 是 | 否 | 是 | 否 | 否 |
| `kSLt` | `kComparison` | 是 | 否 | 否 | 否 | 否 |
| `kAssert` | `kSideEffecting` | 否 | 否 | 否 | 否 | 是 |
| `kParam` | `kSideEffecting` | 否 | 否 | 否 | 否 | 是 |

**需要观察的现象**：注意几个「反直觉」的点——`kSub` 不可结合也不可交换；`kNand` 虽然是位运算、可交换，却**不可结合**；`kParam` 被标成有副作用（因为它代表外部输入 / 状态，不能被当成纯数据随意删除）。

**预期结果**：你手算的「按位与」结果，应当与 [op.cc 的属性函数](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/op.cc#L96-L115) 的逻辑完全吻合。

**待本地验证**：如有编译环境，可写一小段 C++ 调 `OpIsAssociative(Op::kNand)` 等打印结果对照。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `kNand`（与非）不可结合，而 `kAnd`（与）可结合？

**参考答案**：结合律要求 \((a \circ b) \circ c = a \circ (b \circ c)\)。对 `kAnd`，两侧都等于 `a & b & c`，成立。对 `kNand`，\((a \uparrow b) \uparrow c = \lnot(\lnot(a\&b)\&c)\) 与 \(a \uparrow (b \uparrow c) = \lnot(a\&\lnot(b\&c))\) 一般不相等（例如 \(a=b=c=1\) 时前者为 1、后者为 1，但 \(a=1,b=1,c=0\) 时前者为 0、后者为 1）。因此 XLS 据实只在 `kAnd/kOr/kXor` 上标了 `kAssociative`，`kNand/kNor` 只标 `kBitWise|kCommutative`（见 [op_list.h:L36-L37 与 L69,L74](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/op_list.h#L36-L37)）。

**练习 2**：为什么 `kEq`、`kNe` 标了 `kCommutative`，而 `kSLt`、`kULt` 等有序比较没有？

**参考答案**：相等 / 不等与操作数顺序无关（`a==b` 即 `b==a`），故可交换。而 `a < b` 与 `b < a` 结果相反，不可交换。因此宏里 `kEq`/`kNe` 带 `kCommutative`，四个有序比较（`slt/sgt/sle/sge` 及无符号版）只带 `kComparison`（见 [op_list.h:L57,L70 与 L93-L96](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/op_list.h#L57-L70)）。

**练习 3**：`kLiteral`（字面量常量）和 `kParam`（参数）哪个有副作用？

**参考答案**：`kParam` 有副作用（`kSideEffecting`），`kLiteral` 没有（`kStandard`）。常量是纯数据，可被随意复制 / 公共子表达式消除；而参数代表函数输入或 Proc 状态读，语义上属于「外部世界」，故标记为有副作用以防被错误地消除或重排（见 [op_list.h:L66,L82](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/op_list.h#L66-L82)）。

---

### 4.3 Node 子类：从 Op 反向定位到节点类

#### 4.3.1 概念说明

`Op` 枚举回答了「算什么」，但图里的每个顶点在 C++ 里是一个**对象**，对象需要有具体的类型（`Node` 的某个子类）来承载它的操作数与属性。于是存在一条**反向映射**：

\[
\text{Op 枚举值} \;\longrightarrow\; \text{某个 Node 子类}
\]

这条映射的关键观察是：**子类的划分依据是「结构形状」，而不是「语义」**。「结构形状」指的是

- 有几个操作数（arity）；
- 带哪些额外属性（如乘法的 `width`、位切片的 `start/width`、断言的 `message`）。

凡是「操作数个数相同、额外属性相同」的运算，会被合并进**同一个** `Node` 子类，具体运算种类再靠 `Op` 标签区分。这样做能让访问者模式（visitor）、解析器、打印器都只面对少数几种「形状」，而不是 79 种各不相同。

举例：

- `kAdd`、`kSub`、`kUDiv`、`kShll`… 都是「2 个操作数、无额外属性」，共用 `BinOp`。
- `kUMul`、`kSMul` 是「2 个操作数 + 一个 `width` 属性」（乘法结果位宽可与操作数不同），所以单独成 `ArithOp`。
- 全部 10 个比较运算都是「2 个操作数、输出 1 位、无额外属性」，共用 `CompareOp`。

#### 4.3.2 核心流程

每个 `Node` 子类都用一个静态常量数组声明「我负责哪些 `Op`」：

```cpp
class CompareOp final : public Node {
  static constexpr std::array<Op, 10> kOps = {
      Op::kEq, Op::kNe, Op::kSLe, Op::kSGe, Op::kSLt,
      Op::kSGt, Op::kULe, Op::kUGe, Op::kULt, Op::kUGt};
  ...
};
```

注意这是「**子类 → Op 集合**」的方向。反过来要回答「某个 `Op` 属于哪个子类」，就遍历候选子类的 `kOps` 看是否包含它。这个反向判断被封装成模板：

```cpp
template <typename OpT>
constexpr bool IsOpClass(Op op);   // 遍历 OpT::kOps，命中即返回 true
```

IR 文本解析器正是靠一连串 `IsOpClass<T>()` 判断，把读到的运算符分派到对应的构造方法。以「二元 / 一元 / 比较 / N 元 / 位归约」这一族形状相近的运算为例，分派逻辑高度规整：

```text
读到一个运算符字符串（如 "add"）
   │  StringToOp → Op（如 Op::kAdd）
   ▼
IsOpClass<BinOp>(op)?            是 → AddBinOp(...)    （取 2 个操作数）
IsOpClass<UnOp>(op)?             是 → AddUnOp(...)     （取 1 个操作数）
IsOpClass<CompareOp>(op)?        是 → AddCompareOp(...)
IsOpClass<NaryOp>(op)?           是 → AddNaryOp(...)   （取可变个操作数）
IsOpClass<BitwiseReductionOp>?   是 → AddBitwiseReductionOp(...)
其余带属性的（array_index / bit_slice / assert …）→ 各自专门的解析分支
```

> 这里有个**对称之美**值得品味：宏 `XLS_FOR_EACH_OP_TYPE` 是「属性」的唯一真相，而每个子类的 `kOps` 数组是「形状归属」的唯一真相。两者交叉验证——若你给 `kFoo` 标了 `kAssociative` 却没把它放进任何 `kOps`，编译/链接期就会暴露问题。

#### 4.3.3 源码精读

先看 `op.h` 里的反向判断模板 `IsOpClass`，它就是「在 `OpT::kOps` 里线性查找」：

[xls/ir/op.h:L80-L91](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/op.h#L80-L91) —— 模板约束 `OpT` 必须是 `Node` 的子类，然后遍历 `OpT::kOps` 比对。因为 `kOps` 是 `constexpr` 小数组，这个查找在编译期即可折叠。

再看 `nodes.h` 里几个「形状分组」子类的 `kOps`，它们正是 `IsOpClass` 的查询目标：

[xls/ir/nodes.h:L249-L263](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/nodes.h#L249-L263) —— `BinOp` 声明它负责 9 个运算：`kAdd, kSDiv, kSMod, kShll, kShrl, kShra, kSub, kUDiv, kUMod`。注意 `kShll/kShrl/kShra`（左移 / 逻辑右移 / 算术右移）也在其中——它们和加减除结构相同。

[xls/ir/nodes.h:L321-L335](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/nodes.h#L321-L335) —— `CompareOp` 负责全部 10 个比较运算（`kEq, kNe, kSLe, kSGe, kSLt, kSGt, kULe, kUGe, kULt, kUGt`）。**这正是本讲实践任务的答案**：所有比较类运算符对应的 Node 类就是 `CompareOp`。

[xls/ir/nodes.h:L740-L750](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/nodes.h#L740-L750) —— `NaryOp` 负责 5 个可变元按位运算：`kAnd, kNand, kNor, kOr, kXor`。它们都接受任意 ≥2 个操作数。

对比一下「带属性」的子类，体会「形状不同就要单独成类」：

[xls/ir/nodes.h:L68-L86](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/nodes.h#L68-L86) —— `ArithOp`（`kUMul, kSMul`）比 `BinOp` 多一个 `width_` 成员与 `width()` 访问器，因为乘法允许指定与操作数位宽不同的结果位宽。

[xls/ir/nodes.h:L118-L151](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/nodes.h#L118-L151) —— `ArrayIndex` 是「1:1」子类（只负责 `kArrayIndex`），操作数语义是「数组 + 一串索引」，并带 `assumed_in_bounds_` 这样的专属属性，所以独立成类。

最后看解析器如何用 `IsOpClass` 做分派：

[xls/ir/ir_parser.cc:L626-L659](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/ir_parser.cc#L626-L659) —— `BuildBinaryOrUnaryOp` 依次用 `IsOpClass<BinOp>`、`IsOpClass<UnOp>`、`IsOpClass<CompareOp>`、`IsOpClass<NaryOp>`、`IsOpClass<BitwiseReductionOp>` 判断，命中后调用对应 builder 方法并按需取 1 / 2 / 可变个操作数。这正是 4.3.2 那段伪代码的真实实现。

#### 4.3.4 代码实践（手写 IR + 工具验证）

**实践目标**：完成规格要求——列出全部比较类运算符并定位其 Node 类；再手写一段同时含 `kAdd`、`kEq`、`kArrayIndex` 的 IR。

**操作步骤**：

1. **列出比较类运算符**。在 [op_list.h](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/op_list.h) 中检索带 `kComparison` 标志的行，共 10 个：

   | Op | IR 文本串 | 符号性 / 方向 |
   | --- | --- | --- |
   | `kEq` | `eq` | 相等（可交换） |
   | `kNe` | `ne` | 不等（可交换） |
   | `kSLt` / `kSLe` / `kSGt` / `kSGe` | `slt` / `sle` / `sgt` / `sge` | 有符号 <, ≤, >, ≥ |
   | `kULt` / `kULe` / `kUGt` / `kUGe` | `ult` / `ule` / `ugt` / `uge` | 无符号 <, ≤, >, ≥ |

2. **定位 Node 类**。在 [nodes.h 的 `CompareOp::kOps`](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/nodes.h#L321-L335) 中确认：这 10 个比较运算**全部**映射到同一个 `CompareOp` 类。

3. **手写 IR**。把下面这段保存为 `/tmp/my_ops.ir`（语法参考了仓库里的 `ParseTwoPlusTwo.ir`、`ParseULessThan.ir`、`ParseArrayIndex.ir` 三个用例，确保格式合法）：

   ```ir
   package my_ops

   fn example(arr: bits[8][4] id=1, b: bits[8] id=2) -> bits[1] {
     literal.3: bits[32] = literal(value=2, id=3)
     array_index.4: bits[8] = array_index(arr, indices=[literal.3], id=4)
     add.5: bits[8] = add(array_index.4, b, id=5)
     literal.6: bits[8] = literal(value=7, id=6)
     ret eq.7: bits[1] = eq(add.5, literal.6, id=7)
   }
   ```

   解读：`arr` 是 4 个 `bits[8]` 组成的数组；用常量索引 `2` 做 `array_index` 取出第 3 个元素；与参数 `b` 相加（`kAdd`）；再与常量 `7` 比较（`kEq`），返回 1 位结果。这段同时用到了 `kArrayIndex`、`kAdd`、`kEq` 三种运算，分别属于 `ArrayIndex`、`BinOp`、`CompareOp` 三个 Node 子类。

4. **验证解析**。用 `opt_main` 让 XLS 读入并回打（解析即验证文本合法）：

   ```bash
   bazel-bin/xls/tools/opt_main /tmp/my_ops.ir
   ```

**需要观察的现象**：

- 解析成功，说明 `add`、`eq`、`array_index` 三个字符串都被 `StringToOp` 正确识别，并被 `IsOpClass` 正确分派到 `BinOp`/`CompareOp`/`ArrayIndex` 的构造路径。
- 回打出的 IR 保留这三个运算符（因为 `b` 是运行期参数，`add(array_index.4, b)` 与 `eq(..., 7)` 都无法被常量折叠消除）。

**预期结果**：回打内容与输入在语义上一致（节点名 / id 可能重排，但 `add`、`eq`、`array_index` 三类运算仍在）。

**待本地验证**：`opt_main` 的确切回打文本依赖本地构建版本与默认优化等级，请以本机实际输出为准；若把 `b` 换成常量 `literal`，你会观察到 `add`/`eq` 被常量折叠掉——这正好引出下一单元（优化 Pass）的内容。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `kAdd` 和 `kSub` 共用 `BinOp`，而 `kUMul` 却要单独用 `ArithOp`？

**参考答案**：因为「形状」不同。`kAdd`/`kSub` 都是「2 个操作数、结果位宽 = 操作数位宽、无额外属性」，结构完全一致，故共用 `BinOp`。而乘法允许结果位宽与操作数位宽不同（需要额外存一个 `width` 属性，并提供 `width()` 访问器），形状多了一项，所以单独成 `ArithOp`（对比 [nodes.h:L249-L263 的 BinOp](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/nodes.h#L249-L263) 与 [L68-L86 的 ArithOp](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/nodes.h#L68-L86)）。

**练习 2**：解析器读到字符串 `"xor"` 时，依次会命中哪个 `IsOpClass` 判断？为什么不是 `BinOp`？

**参考答案**：`"xor"` → `Op::kXor`。`kXor` 不在 `BinOp::kOps`（那是 `kAdd/kSub/kSDiv/.../kUMod` 共 9 个）里，所以 `IsOpClass<BinOp>` 为假；而 `kXor` 在 `NaryOp::kOps = {kAnd, kNand, kNor, kOr, kXor}` 里（[nodes.h:L740-L750](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/nodes.h#L740-L750)），于是命中 `IsOpClass<NaryOp>`，走 `AddNaryOp` 路径并取可变个操作数。这也解释了为何 `.ir` 里可以写 `xor(x, x, x, x)` 这种多于两个操作数的形式（见 `ParseNaryXor.ir`）。

**练习 3**：`IsOpClass<CompareOp>(Op::kEq)` 在编译期能求值吗？这意味着什么？

**参考答案**：能。因为 `CompareOp::kOps` 是 `static constexpr std::array`，`Op::kEq` 也是常量，`IsOpClass` 内部是纯循环比较，在 `constexpr` 上下文中可被编译器完全折叠。这意味着解析器里这一串 `if (IsOpClass<T>(op))` 没有运行期查找开销，等价于一组编译期常量判断。

## 5. 综合实践

把本讲三个模块串起来，完成一个「运算符体检」小任务。

**任务**：自行设计一个 `bits[16]` 上的小函数，让它同时覆盖以下五类运算各至少一个——算术（如 `add`）、比较（如 `ult`）、按位（如 `and`）、选择（如 `sel` 或 `one_hot_sel`）、数组或元组（如 `array_index` 或 `tuple_index`）。

**建议步骤**：

1. 先在 [op_list.h](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/op_list.h) 里选定 5 个具体运算符，记录各自的四元组（枚举名 / proto 名 / IR 文本串 / 类别位掩码）。
2. 手算每个运算符的五个类别属性（Compare / Associative / Commutative / BitWise / SideEffecting），填一张表。
3. 在 [nodes.h](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/nodes.h) 里为每个运算符找出它归属的 Node 子类（用 `IsOpClass<T>` 的思路：哪个子类的 `kOps` 包含它）。
4. 仿照 4.3.4 的格式，手写一段合法的 `.ir` 文本用到这 5 个运算符；类型与操作数位宽要自洽（参考 `xls/ir/testdata/ir_parser_round_trip_test_*.ir` 的写法）。
5. 用 `opt_main` 读入回打，确认 5 个运算都被正确解析。

**自检标准**：

- 你选的「可交换」运算，其位掩码确实带 `kCommutative`；
- 你选的「有副作用」运算（本任务里大概率没有，除非用 `assert`），确实带 `kSideEffecting`；
- 5 个运算分别落在 5 个**不同**的 Node 子类里（验证「形状决定子类」）。

> 提示：选择类运算在 [nodes.h](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/nodes.h) 中有 `Select`（`kSel`）、`OneHotSelect`（`kOneHotSel`）、`PrioritySelect`（`kPrioritySel`）三个 1:1 子类，以及位归约用的 `OneHot`（`kOneHot`），可任选其一。

## 6. 本讲小结

- **一份宏派生万物**：`XLS_FOR_EACH_OP_TYPE`（`op_list.h`）是全部 79 个运算符的唯一真相，枚举、`kAllOps`、字符串 / proto 转换、类别位掩码都从它派生，新增运算符只需改一处。
- **每个运算符的四元组**：`(枚举名, proto 名, IR 文本串, 类别位掩码)`；`.ir` 文件里的小写助记符（`add`/`eq`/`array_index`…）就是第三字段。
- **类别是位掩码**：六个位标志（Standard / Comparison / Associative / Commutative / BitWise / SideEffecting）按位或组合，五个属性函数用按位与检测，决定 Pass 能否做相应变换。
- **Node 子类按「形状」而非「语义」划分**：操作数个数 + 额外属性相同的运算共用一个子类（如 9 个算术移位运算共用 `BinOp`，10 个比较运算共用 `CompareOp`），具体语义靠 `Op` 标签区分。
- **反向映射靠 `kOps` + `IsOpClass<T>()`**：每个子类用 `static constexpr kOps` 声明负责的运算，`IsOpClass<T>(op)` 在 `T::kOps` 里查找；解析器用一连串这种判断把文本分派到对应构造路径。
- **关键对比**：`kAdd`/`kSub` 共用 `BinOp`（无属性），`kUMul`/`kSMul` 单独成 `ArithOp`（带 `width`），`kEq` 等 10 个比较运算共用 `CompareOp`——这组对比浓缩了「形状决定子类」的全部要点。

## 7. 下一步学习建议

- **横向**：如果你还没熟悉 `.ir` 文本格式的完整语法（`package` / `fn` / `ret` / 各种带属性运算的写法），下一讲 **u3-l3「IR 文本格式：解析与打印」** 会专门讲 [ir_parser](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/ir_parser.h) 与打印器，本讲 4.3 的解析分派正是其中的关键一环。
- **纵向**：本讲建立的「`Op` + 类别属性」是后续 **第四单元（优化 Pass）** 的基石——例如 `OpIsAssociative`/`OpIsCommutative` 直接驱动算术化简与公共子表达式消除，`OpIsSideEffecting` 直接决定死代码消除的边界。读到 `xls/passes/arith_simplification_pass.h`、`cse_pass.h`、`dce_pass.h` 时你会频繁回看本讲的类别表。
- **动手**：在进入下一讲前，建议把本讲 4.3.4 的 `.ir` 真正在本机构建出的 `opt_main` 上跑一遍，亲眼看 `add`/`eq`/`array_index` 被解析与回打——这是理解整条 IR 流水线最直接的体感。
