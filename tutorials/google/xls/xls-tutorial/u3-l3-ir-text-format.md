# IR 文本格式：解析与打印

## 1. 本讲目标

本讲聚焦 XLS IR 的**人类可读文本格式**（即 `.ir` 文件里那种文本），以及让它"能读、能写、能往返（round-trip）"的解析器（`ir_parser`）和打印器（`DumpIr`）。学完后你应当能够：

1. 拿到一段 `.ir` 文本，逐行说清 `package`、`fn` 签名、参数、节点行、`ret` 各自的含义。
2. **手写**一小段合法的 IR 文本，而不依赖任何前端工具生成。
3. 说清解析器 `Parser` 如何用「扫描器 + 函数/块/过程签名 + 节点行」三段式，把一段文本重新建构成内存里的 `Package`。
4. 理解"往返不变"——为什么 `解析(打印(P)) == P`、`打印(解析(s)) == s` 是 XLS 的一个设计目标，以及它在哪里被测试。
5. 能读懂解析器的报错信息，并据此定位自己手写 IR 中的语法/语义错误。

## 2. 前置知识

本讲承接 [u3-l1（IR 总览：Package、Function、Node、Value）](u3-l1-ir-overview.md)，默认你已经知道：

- XLS IR 在内存里是一张**数据流的 SSA 图**（sea-of-nodes），而不是控制流图。这一点官方文档讲得很清楚：硬件天然"所有时刻都在并行发生"，所以选了更贴近硬件的数据流表示，并且 SSA 性质由"函数式"自动维持，不需要显式的 SSA 更新（见 [docs_src/ir_overview.md:L9-L33](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/docs_src/ir_overview.md#L9-L33)）。
- 三层包含关系：`Package` 拥有 `Function`/`Proc`/`Block`（统称 `FunctionBase`），`FunctionBase` 持有一堆 `Node`，`Node` 之间靠 `operands`/`users` 指针连成图。
- `Op` 是一个枚举，每个 `Node` 都带一个 `Op` 标签和它产出的 `Type`（见 [u3-l2 IR 运算符体系](u3-l2-ir-operations.md)）。
- 一个 `Package` 有唯一的 **top 实体**。

本讲要回答的新问题是：**当 IR 被写成一个文本文件时，它长什么样？机器又怎么把这个文本还原回内存结构？**

两个朴素的直觉先建立起来：

- **文本 IR 是给人看的，也是给测试用的。** 它不是 IR 在内存里的形态，而是一种可读的"序列化"。解析器头文件的注释开宗明义：这是"便于调试和构造小测试用例"的便利功能，也可以让别的"前端不必完整链接 XLS"就能瞄准 XLS（见 [xls/ir/ir_parser.h:L15-L20](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/ir_parser.h#L15-L20)）。
- **打印和解析必须互为逆操作。** 这条约束决定了文本格式的几乎所有细节：每个字段都能被原样读回来。这条性质用一句话写出来就是

  \[ \text{DumpIr}(\text{ParsePackage}(s)) = s \quad\text{（对"规范写法"的文本成立）} \]

  以及更重要的

  \[ \text{ParsePackage}(\text{DumpIr}(P)) \cong P \quad\text{（结构等价）} \]

  后面你会看到这条性质被直接写成单元测试。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [xls/ir/ir_parser.h](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/ir_parser.h) / [xls/ir/ir_parser.cc](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/ir_parser.cc) | 解析器 `Parser` 的全部声明与实现：从文本重建 `Package`/`Function`/`Proc`/`Block`。本讲主角。 |
| [xls/ir/ir_scanner.h](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/ir_scanner.h) | 词法扫描器 `Scanner`，把字符串切成 `Token`（关键字、标识符、字面量、标点等）。 |
| [xls/ir/op.h](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/op.h) / [xls/ir/op.cc](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/op.cc) | `OpToString`/`StringToOp`：运算符枚举 ↔ 文本串的双向转换，解析与打印都要用。 |
| [xls/ir/node.cc](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/node.cc) | `Node::ToStringInternal`、`Node::GetName`：打印端，决定"一个节点行长什么样"。 |
| [xls/ir/function.cc](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/function.cc) / [xls/ir/function_base.cc](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/function_base.cc) | `Function::DumpIr`、`FunctionBase::DumpFunctionBaseNodes`：打印端，把整张图排版成文本。 |
| [xls/ir/package.h](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/package.h) | `Package` 容器；`DumpIr`/`SetTop` 等顶层入口。 |
| [xls/tools/opt_main.cc](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/tools/opt_main.cc) | `opt_main` 工具：读 `.ir` → 优化 → 写 `.ir`，本讲实践用的"试金石"。 |
| [xls/ir/testdata/ir_parser_round_trip_test_*.ir](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/testdata) | 一批真实的最小 `.ir` 样例，是本讲"标准答案"的来源。 |

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：先认识**文本语法本身**（4.1），再看**打印端**如何把内存图变成这种文本（4.2，它是往返的"出口"），最后看**解析端**如何把文本重建回内存图（4.3，往返的"入口"）。

### 4.1 IR 文本语法总览

#### 4.1.1 概念说明

IR 文本是**分层缩进**的，结构上几乎和内存模型一一对应：

- 顶层一个 `package <名字>` 声明（可选，单函数片段可省略，见后）。
- 包内可以并列多个 `fn`（函数）、`proc`（过程）、`block`（块）、`chan`（通道）、`file_number`（源文件号映射）等成员。
- 每个 `fn` 由**签名**（名字、参数、返回类型）和**函数体**（一串节点行）组成。
- 每个节点行就是图里的一个 `Node`，写成形如 `名字: 类型 = 运算符(参数...)`。

我们用仓库自带的真实样例来看。下面这个文件是「计算 2+2」的函数，它同时被用作解析往返测试的输入：

```
fn two_plus_two() -> bits[32] {
  literal.1: bits[32] = literal(value=2, id=1)
  literal.2: bits[32] = literal(value=2, id=2)
  ret add.3: bits[32] = add(literal.1, literal.2, id=3)
}
```

来源：[xls/ir/testdata/ir_parser_round_trip_test_ParseTwoPlusTwo.ir:L1-L5](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/testdata/ir_parser_round_trip_test_ParseTwoPlusTwo.ir#L1-L5)

逐项拆解：

| 片段 | 含义 |
| --- | --- |
| `fn two_plus_two() -> bits[32]` | 定义函数 `two_plus_two`，无参，返回 `bits[32]`。 |
| `literal.1: bits[32] = literal(value=2, id=1)` | 一个 `kLiteral` 节点，产出类型 `bits[32]`，常量值 `2`，节点 id 为 `1`。 |
| `ret add.3: bits[32] = add(literal.1, literal.2, id=3)` | 一个 `kAdd` 节点，两个操作数是上面两个字面量；行首 `ret` 表示**这个节点是函数的返回值**。 |

几个关键约定：

- **节点的"名字"有两种**：要么是开发者起的真名（如 `foo`），要么是自动生成的 `<运算符>.<id>`（如 `add.3`、`literal.1`）。后者表示"这个节点没有有意义的名字"。这一点决定了 4.3 节里解析器对名字的特殊处理。
- **`id=N`** 是一个可选的关键字参数，用来把节点的整数 id 固定下来（往返时保持一致）。
- **操作数按位置引用其他节点的名字**，例如 `add(literal.1, literal.2)`。
- **`ret` 只能出现在函数里**，且只能出现一次；`proc` 用 `next` 而不是 `ret`（4.3 详述）。
- **类型**写在节点名和参数表里：`bits[32]`、`(bits[32], bits[8])` 元组、`bits[8][4]` 数组等。

#### 4.1.2 核心流程

一段"完整的"IR 文本，语法骨架可以用下面的产生式刻画（非完整文法，仅展示主干）：

```
<package>   ::= "package" <ident>
                ( <top-entity> | "chan" ... | "file_number" ... )*
<top-entity>::= ["top"] ( "fn" <function> | "proc" <proc> | "block" <block>
                          | "scheduled_fn" ... | "scheduled_proc" ... | "scheduled_block" ... )
<function>  ::= <ident> "(" <params> ")" "->" <type> "{" <node-line>* "}"
<params>    ::= "" | <param> ("," <param>)*
<param>     ::= <ident> ":" <type> ["id=" <int>]
<node-line> ::= ["ret" | "next"] <name> ":" <type> "=" <op> "(" <args> ")"
<name>      ::= <ident> | <ident> "." <int>        // 真名 或 自动名 op.id
<args>      ::= <operand> ("," <operand>)* ("," <kw-arg>)*
<kw-arg>    ::= <ident> "=" <value>
<type>      ::= "bits[" <int> "]" | "(" <type> ("," <type>)* ")"
                | <type> "[" <int> "]" | "token" | ...
```

读这张图的两条主线：

1. **自顶向下**：`package` → 多个顶层成员 → 每个 `fn` 内是若干节点行。缩进只是排版，解析器并不靠缩进分块，而是靠 `{}`
2. **横向引用**：每个节点行的操作数是**前面已定义节点的名字**。因此节点行之间存在隐式的定义顺序（数据流方向），这与 IR 的 SSA 性质一致——先有定义，后有使用。

#### 4.1.3 源码精读

**带 `package` 头的多函数文件**长这样（注意顶层并列了两个 `fn`，没有 `top` 标记时由调用方指定入口）：

```
package MultiFunctionPackage

fn two_plus_two() -> bits[32] {
  literal.1: bits[32] = literal(value=2, id=1)
  literal.2: bits[32] = literal(value=2, id=2)
  ret add.3: bits[32] = add(literal.1, literal.2, id=3)
}

fn seven_and_five() -> bits[32] {
  literal.4: bits[32] = literal(value=7, id=4)
  literal.5: bits[32] = literal(value=5, id=5)
  ret and.6: bits[32] = and(literal.4, literal.5, id=6)
}
```

来源：[xls/ir/testdata/ir_parser_round_trip_test_ParseMultiFunctionPackage.ir:L1-L14](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/testdata/ir_parser_round_trip_test_ParseMultiFunctionPackage.ir#L1-L14)

**带位置参数与带关键字参数的运算符**：有些运算符除了位置操作数，还带命名关键字。最典型的就是位切片 `bit_slice`，它有一个位置操作数 `x`，外加 `start=`/`width=` 两个关键字：

```
fn bitslice(x: bits[32] id=3) -> bits[14] {
  ret bit_slice.1: bits[14] = bit_slice(x, start=7, width=14, id=1)
}
```

来源：[xls/ir/testdata/ir_parser_round_trip_test_ParseBitSlice.ir:L1-L3](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/testdata/ir_parser_round_trip_test_ParseBitSlice.ir#L1-L3)

注意签名里的参数也带了 `id=3`：参数节点（`kParam`）和普通节点一样有 id。

**带"带类型字面量"的值**：常量值写作 `value=2` 这种"人读形式"（`ToHumanString`）。还有一种带嵌入类型的写法（`bits[32]:0x42`），用于脱离类型上下文单独传值，解析器单独提供 `ParseTypedValue` 来读它——见 [xls/ir/ir_parser.h:L163-L172](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/ir_parser.h#L163-L172) 的注释与示例。

> 小结：IR 文本的"形状"由两类规则决定——**结构规则**（`package`/`fn`/`{}`/`ret`）和**节点行规则**（`名字: 类型 = op(参数)`）。后者正是 4.3 节 `ParseNode` 要逐字符吃掉的东西。

#### 4.1.4 代码实践（阅读型）

**目标**：在脑中把一段真实 IR 文本"跑"一遍，确认你读懂了语法。

**步骤**：

1. 打开 [xls/ir/testdata/ir_parser_round_trip_test_ParseExtendOps.ir:L1-L5](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/testdata/ir_parser_round_trip_test_ParseExtendOps.ir#L1-L5)，内容是：

   ```
   fn foo(x: bits[8] id=5) -> bits[32] {
     zero_ext.1: bits[32] = zero_ext(x, new_bit_count=32, id=1)
     sign_ext.2: bits[32] = sign_ext(x, new_bit_count=32, id=2)
     ret xor.3: bits[32] = xor(zero_ext.1, sign_ext.2, id=3)
   }
   ```

2. 不看答案，回答三个问题：
   - 参数 `x` 的位宽是多少？它的 id 是多少？
   - `zero_ext` 的位置操作数是谁？它的关键字参数是什么？
   - 哪一行是返回值？为什么？

**需要观察的现象 / 预期结果**：`x` 是 `bits[8]`、id=5；`zero_ext` 的位置操作数是 `x`，关键字参数是 `new_bit_count=32`；`ret xor.3` 是返回值，因为它行首有 `ret`，且其类型 `bits[32]` 与签名返回类型一致。

#### 4.1.5 小练习与答案

**练习 1**：下面这行节点行里，"名字"是什么？运算符是什么？哪些是位置参数、哪些是关键字参数？

```
ret hi: bits[32] = bit_slice(prod, start=32, width=32, id=6)
```

> **答案**：名字是 `hi`（一个开发者起的真名，带 `ret` 表示它是返回值）；运算符是 `bit_slice`；位置参数是 `prod`；关键字参数是 `start=32`、`width=32`、`id=6`。

**练习 2**：为什么 `add.3` 这种名字里有个点加数字，而 `hi` 没有？

> **答案**：`add.3` 是**自动生成的名字**，格式为 `<运算符>.<id>`，表示该节点没有被赋予有意义的名字；`hi` 是开发者显式起的名字。两者在解析时走不同分支（见 4.3.3 的 `SplitNodeName`）。

---

### 4.2 打印端：DumpIr 与节点行的格式化

#### 4.2.1 概念说明

文本 IR 不是凭空设计的，而是由**打印器**（`DumpIr`）按固定规则"排版"出来的。先看打印端有个好处：**解析器要做的事，就是把打印器产出的每个字段再原样吃回来**。所以理解了打印，就理解了解析的目标。

打印分三层：

- **包级**：`Package::DumpIr()` 输出 `package <名字>` 头，再依次输出每个函数/过程/块/通道。
- **函数级**：`Function::DumpIr()` 输出签名 `fn name(params) -> type {`、函数体、`}`。
- **节点级**：每个节点由 `Node::ToString()` 输出一行 `名字: 类型 = 运算符(参数)`。

#### 4.2.2 核心流程

节点行的核心拼接逻辑只有一行，但决定了整个文本格式的长相：

```cpp
std::string ret = absl::StrCat(GetName(), ": ", GetType()->ToString(), " = ",
                               OpToString(op_));
// 随后把操作数和关键字参数依次拼进 ( ... )
```

即 `名字 + ": " + 类型 + " = " + 运算符`，再加上括号里的参数列表。

节点"名字"的取法很关键，它要同时支持"真名"和"自动名"两种情况：

```cpp
std::string Node::GetName() const {
  if (HasAssignedName()) {            // 开发者起过真名？
    return *name_;
  }
  return absl::StrFormat("%s.%d", OpToString(op()), id());  // 否则 op.id
}
```

也就是说：**没起名的节点，其文本名字就是 `运算符.整数id`**——这正是 4.1 里看到的 `add.3`、`literal.1` 的来源。

函数返回值的 `ret` 前缀由一个专门的"注解器"加上去：在打印函数体时，给等于返回值的那个节点挂上 `{.prefix = "ret"}`。

#### 4.2.3 源码精读

**节点行的拼接**——`Node::ToStringInternal`，开头的 `StrCat` 就是上面那句"骨架"，后面 `switch (op_)` 按运算符追加各自的特殊参数（如 `literal` 加 `value=`、`bit_slice` 加 `start=`/`width=`）：

[xls/ir/node.cc:L613-L623](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/node.cc#L613-L623)——这段先拼出 `名字: 类型 = 运算符`，再把每个操作数的名字（必要时附带类型）推进 `args`。

**节点名字的取法**——`Node::GetName`，真名优先，否则回退到 `OpToString(op) + "." + id`：

[xls/ir/node.cc:L571-L577](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/node.cc#L571-L577)

**运算符 ↔ 文本串**——`OpToString`/`StringToOp` 是双向的。打印用 `OpToString`（`switch` 由宏 `XLS_FOR_EACH_OP_TYPE` 展开），解析用 `StringToOp`（一张静态哈希表，键是文本串、值是 `Op`）。两者都由 [xls/ir/op_list.h](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/op_list.h) 里那张四元组表唯一驱动：

- 打印：[xls/ir/op.cc:L60-L68](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/op.cc#L60-L68)（`OpToString`）
- 解析：[xls/ir/op.cc:L70-L82](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/op.cc#L70-L82)（`StringToOp`，遇到不认识的串返回 `Unknown operation for string-to-op conversion: <串>`）
- 声明：[xls/ir/op.h:L56-L59](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/op.h#L56-L59)

**函数签名与 `ret` 的排版**——`Function::DumpIr` 先拼 `fn name(...) -> type {`（注意 `top ` 和 `scheduled_` 前缀的拼接），参数按 `名字: 类型 id=N` 排版；然后用 `AddRetAnnotator` 给返回值节点挂上 `ret` 前缀：

[xls/ir/function.cc:L96-L109](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/function.cc#L96-L109)（签名与参数排版）、[xls/ir/function.cc:L126-L137](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/function.cc#L126-L137)（`AddRetAnnotator` 把 `.prefix = "ret"` 挂到返回值节点）。

一个边界情况值得注意：如果**返回值恰好是一个参数**（函数直接返回某个入参），它不会出现在普通节点遍历里，所以要单独补一行 `ret`——见 [xls/ir/function.cc:L149-L154](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/function.cc#L149-L154)。

**遍历节点并逐行打印**——`FunctionBase::DumpFunctionBaseNodes`：对图里每个节点调用 `node->ToString()` 并加上注解前缀。这是"打印一张图"的核心循环：

[xls/ir/function_base.cc:L266-L355](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/function_base.cc#L266-L355)（非调度情况下，在 L344-L352 遍历 `nodes()` 逐个 `node->ToString()`）。

`DumpIr` 作为 `FunctionBase` 的纯虚接口声明在这里：

[xls/ir/function_base.h:L231-L233](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/function_base.h#L231-L233)——注释明确说"DumpIr 以**可解析的**分层文本格式输出 IR"，这句话就是"往返"的设计契约。

#### 4.2.4 代码实践（阅读 + 推演型）

**目标**：把"打印规则"当公式，手工预测一段内存图会打印成什么。

**步骤**：

1. 设想一个函数 `f`，参数 `x: bits[4]`（id=2），函数体只有一个节点：对 `x` 取反（运算符 `not`，id=5），且它是返回值、没有起名。
2. 套用 4.2.2 的两条规则（节点行骨架 + `GetName` 的自动名规则 + `ret` 前缀），**手写**出你预测的 `.ir` 文本。
3. 拿你的预测和 [xls/ir/testdata/ir_parser_round_trip_test_ParseFunction.ir:L1-L3](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/testdata/ir_parser_round_trip_test_ParseFunction.ir#L1-L3) 对比（注意那是 `sub` 不是 `not`，但结构相同）。

**需要观察的现象 / 预期结果**：未命名 `not` 节点的名字应为 `not.5`；整行应为 `ret not.5: bits[4] = not(x, id=5)`；签名应为 `fn f(x: bits[4] id=2) -> bits[4] {`。

#### 4.2.5 小练习与答案

**练习 1**：如果一个节点既没有被起名、id 又是 7、运算符是 `and`，`GetName()` 会返回什么？

> **答案**：`and.7`。因为 `HasAssignedName()` 为假，回退到 `OpToString(op) + "." + id`。

**练习 2**：为什么打印端要把参数写成 `x: bits[4] id=2`，而把普通节点写成 `not.5: bits[4] = not(x, id=5)`？两者都带 `id`，区别在哪？

> **答案**：参数节点（`kParam`）的 `id=2` 写在签名里，紧跟类型；普通节点的 `id=5` 是节点行括号里的一个**关键字参数**。两者都是为了让往返后 id 一致，但出现在不同语法位置（签名 vs 节点行参数表）。

---

### 4.3 解析器 ir_parser：从文本重建 Package

#### 4.3.1 概念说明

解析器 `Parser` 的职责是 4.2 的逆过程：吃进一段文本，重建出内存里的 `Package`。它的入口是一组静态方法，最常用的是 `ParsePackage`：

[xls/ir/ir_parser.h:L96-L98](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/ir_parser.h#L96-L98)——`ParsePackage(input_string, filename)` 返回一个 `unique_ptr<Package>`。还有 `ParsePackageWithEntry`（指定入口）、`ParseFunction`/`ParseProc`/`ParseBlock`（往已有包里加一个实体）等。

解析器在内部采用经典的**「扫描器 + 递归下降」**两段式：

- `Scanner` 先把字符串切成 `Token` 流。词法类别由枚举 `LexicalTokenType` 列举：`kIdent`（标识符）、`kKeyword`（关键字，如 `fn`/`ret`/`package`）、`kLiteral`（数字字面量）、以及各种标点（`kColon`、`kEquals`、`kParenOpen`、`kRightArrow`、`kCurlOpen` …）——见 [xls/ir/ir_scanner.h:L34-L57](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/ir_scanner.h#L34-L57)。
- `Parser` 持有一个 `Scanner`，用"peek 前瞻 + drop/pop 消费"的方式递归下降，按语法逐段吃掉 Token，并用 `FunctionBuilder`/`ProcBuilder`/`BlockBuilder` 把节点一个个"建"进图里（构造期的句柄叫 `BValue`）。

#### 4.3.2 核心流程

解析一个包的整体流程是：

```
ParsePackage(text)
  └─ Scanner::Create(text)            # 切词
  └─ ParsePackageName()                # 吃掉 "package <名字>"，新建 Package
  └─ while 未到 EOF:                    # 逐个顶层成员分派
        MaybeParseOuterAttributes()    #   可选的 #[...] 属性
        peek 下一个 token
        若是 "top"  -> 标记为入口实体，再 peek
        若是 "fn"   -> ParseFunction()      -> SetTop 若 is_top
        若是 "proc" -> ParseProc()          -> SetTop 若 is_top
        若是 "block"-> ParseBlock()         -> SetTop 若 is_top
        若是 "scheduled_fn/proc/block" -> 调度版
        若是 "chan" -> ParseChannel()
        若是 "file_number" -> ParseFileNumber()
        否则 -> 报错 "Expected ... declaration"
  └─ SetUnassignedNodeIds(package)     # 给没写 id 的节点补 id
```

其中 `ParseFunction` 内部三步：

1. `ParseFunctionSignature`：吃 `fn name(params) -> type {`，为每个参数用 builder 建一个 `Param` 节点，登记到 `name_to_value` 表（名字 → BValue）。
2. `ParseBody`：循环到 `}` 为止，每行要么是 `reg`/`instantiation`/`chan` 等特殊声明，要么是 `ret`/`next` 修饰的或普通的**节点行**（交给 `ParseNode`）。
3. `BuildWithReturnValue(...)`：收尾，把 builder 的产物固化成一个 `Function`。

节点行 `ParseNode` 的契约在源码注释里一句话说清——`<output_name>: <type> = op(...)`：

[xls/ir/ir_parser.cc:L747-L760](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/ir_parser.cc#L747-L760)——依次弹出"输出名"、冒号、类型、等号、运算符 token；接着用 `StringToOp` 把运算符文本串转成 `Op` 枚举。

#### 4.3.3 源码精读

**包名解析**——`ParsePackageName`：先 `DropKeywordOrError("package")`，再弹出一个"标识符或关键字"token 当包名（注释说明：此处关键字是上下文相关的，也允许当包名用）：

[xls/ir/ir_parser.cc:L2331-L2338](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/ir_parser.cc#L2331-L2338)

**顶层成员分派**——这是整个解析的"主循环"，按 peek 到的关键字决定调用哪个子解析器（`fn`/`proc`/`block`/`scheduled_*`/`chan`/`file_number`），并在 `top` 前缀出现时把该实体设为入口：

[xls/ir/ir_parser.h:L448-L550](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/ir_parser.h#L448-L550)（`ParseDerivedPackageNoVerify` 模板里的 `while (!parser.AtEof())` 循环，例如 L467-L475 处理 `fn`、L532-L544 处理 `chan`/`file_number`）。

**函数签名解析**——`ParseFunctionSignature`：弹函数名、建 `FunctionBuilder`（带 `should_verify=false`，这样解析器才能为了测试构造出"畸形"IR）、吃 `(`、解析参数列表、吃 `)`、吃 `->`、解析返回类型、吃 `{`。每个参数建为 `Param` 节点并登记到 `name_to_value`：

[xls/ir/ir_parser.cc:L2075-L2113](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/ir_parser.cc#L2075-L2113)

**节点名解析的关键技巧——`SplitNodeName`**：解析器拿到"输出名"后，要判断它到底是真名还是自动名 `op.id`。办法是按 `.` 切分，若最后一段能被 `SimpleAtoi` 解析成整数，就认为这是自动名（`op_name` + `node_id`），此时**不给节点起真名**：

[xls/ir/ir_parser.cc:L667-L682](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/ir_parser.cc#L667-L682)（`SplitName` 结构与 `SplitNodeName` 函数）。它在 `ParseNode` 里被这样使用：

[xls/ir/ir_parser.cc:L771-L776](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/ir_parser.cc#L771-L776)——若 `split_name.has_value()`，则 `node_name = ""`（即不赋真名）；否则把整个输出名当真名。

> 这也解释了为什么"自动名"里的 `op` 部分会被校验：后面会检查 `split_name->op_name` 是否等于该节点真正的运算符串（见 [xls/ir/ir_parser.cc:L1480-L1491](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/ir_parser.cc#L1480-L1491)）。所以你不能给一个 `zero_ext` 节点起形如 `foo.3` 的自动名——`foo` ≠ `zero_ext` 会报错。

**`ret` 的解析**——`ParseBody` 在循环里先 `TryDropKeyword("ret")`，若成功则把随后 `ParseNode` 建出的节点记为函数返回值（且 `ret` 只允许出现在函数里、只允许出现一次）：

[xls/ir/ir_parser.cc:L2024-L2033](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/ir_parser.cc#L2024-L2033)（`saw_ret` 为真时把 bvalue 存进 `return_value`，并对非函数实体报 "ret keyword only supported in functions"）。

**带关键字参数的运算符示例——扩展运算符**：`zero_ext`/`sign_ext` 解析时声明一个 `new_bit_count` 关键字参数槽，吃一个位置操作数，并校验标注类型必须与 `new_bit_count` 一致：

[xls/ir/ir_parser.cc:L1075-L1091](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/ir_parser.cc#L1075-L1091)。这正是 4.1.4 里 `ParseExtendOps.ir` 能被读回来的依据。

**报错形态**：解析器大量使用 `absl::InvalidArgumentError(absl::StrFormat(..., peek.pos().ToHumanString()))`，把出错位置（行列号）带进消息。一个最典型的错误是"不认识的运算符"：

[xls/ir/ir_parser_error_test.cc:L110-L123](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/ir_parser_error_test.cc#L110-L123)——当节点行写成 `foo_op(x, z)`，`StringToOp("foo_op")` 失败，报 `Unknown operation for string-to-op conversion: foo_op`。

#### 4.3.4 代码实践（跟踪型）

**目标**：跟踪 `ret add.3: bits[32] = add(literal.1, literal.2, id=3)` 这一行在解析器里的旅程。

**步骤**：

1. 在 [xls/ir/ir_parser.cc:L747](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/ir_parser.cc#L747) 的 `ParseNode` 处，确认它依次弹出：输出名 `add.3`、冒号、类型 `bits[32]`、等号、运算符 `add`。
2. 跟到 [L762](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/ir_parser.cc#L762) 的 `StringToOp("add")`，确认它返回 `Op::kAdd`。
3. 跟到 [L771-L776](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/ir_parser.cc#L771-L776)：`SplitNodeName("add.3")` 返回 `{op_name="add", node_id=3}`，于是 `node_name = ""`（不赋真名）。
4. 注意 `ret` 是在 `ParseBody`（[L1973](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/ir_parser.cc#L1973)）里先被 `TryDropKeyword` 吃掉的，早于 `ParseNode`。

**需要观察的现象 / 预期结果**：`add.3` 被当作"无名、id=3"的节点；操作数 `literal.1`、`literal.2` 通过 `name_to_value` 表解析成之前建好的字面量节点；最终这个 `add` 节点因为是 `ret` 行，被登记为函数返回值。整条调用链是 `ParseBody → ParseNode → FunctionBuilder::Add`。

#### 4.3.5 小练习与答案

**练习 1**：如果我手写一行 `qux: bits[4] = not(x, id=9)`（名字是 `qux`，无点），解析器会把它当作真名还是自动名？节点最终会被赋名为 `qux` 吗？

> **答案**：当作**真名**。`SplitNodeName("qux")` 按点切分只有一段 `qux`，`SimpleAtoi("qux")` 失败，返回 `nullopt`；于是 `node_name = "qux"`，节点被赋真名 `qux`，`HasAssignedName()` 为真。

**练习 2**：解析器看到 `fn foo() bits[32] {`（漏写了 `->`）会发生什么？

> **答案**：`ParseFunctionSignature` 在 [L2106-L2107](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/ir_parser.cc#L2106-L2107) 处 `DropTokenOrError(kRightArrow, "'->' in function signature")` 会失败，返回带位置信息的 `InvalidArgument` 错误。

**练习 3**：为什么解析器在 `ParseFunctionSignature` 里建 builder 时要传 `should_verify=false`？

> **答案**：为了让解析器能**构造出故意畸形的 IR**（供测试用）。注释明确说"The parser does its own verification so pass should_verify=false"（[L2081-L2082](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/ir_parser.cc#L2081-L2082)）。配套的还有 `ParsePackageNoVerify`（[xls/ir/ir_parser.h:L148-L151](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/ir_parser.h#L148-L151)）专供"想要畸形 IR"的测试。

---

## 5. 综合实践

把本讲的三块知识串起来：**手写一个"两数相乘再取高位"的 IR，用 `opt_main` 验证它能被解析并重新打印，再故意制造一个语法错误观察报错。**

### 背景：为什么这样写"取高位"

两个 32 位无符号数相乘，完整乘积是 64 位。XLS 的普通乘法 `umul` 是**同宽截断乘法**（结果与操作数同宽，见 [docs_src/ir_semantics.md:L351](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/docs_src/ir_semantics.md#L351)），直接 `umul(a,b)` 会丢掉高位。

> 注意别踩坑：另有一对运算符 `umulp`/`smulp`（部分积乘法）返回一个二元组，但它只保证"两元素之和等于乘积"，**两元素本身并未被约束**（见 [docs_src/ir_semantics.md:L369-L371](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/docs_src/ir_semantics.md#L369-L371)），所以不能拿它的某个元素当"高位"。

正确做法：先把两个操作数**零扩展**到 64 位（`zero_ext`），再做 64 位 `umul`（不会溢出），最后 `bit_slice` 取高 32 位。这正好用到本讲讲过的三类节点行：扩展运算符、普通二元运算符、带关键字参数的位切片。

### 步骤 1：手写 IR 文件

把下面内容存为 `/tmp/mul_hi.ir`（**示例代码，由本讲作者手写，非项目自带文件**）：

```
package mul_hi

fn mul_hi(a: bits[32] id=1, b: bits[32] id=2) -> bits[32] {
  aw: bits[64] = zero_ext(a, new_bit_count=64, id=3)
  bw: bits[64] = zero_ext(b, new_bit_count=64, id=4)
  prod: bits[64] = umul(aw, bw, id=5)
  ret hi: bits[32] = bit_slice(prod, start=32, width=32, id=6)
}
```

逐行自检（用 4.1、4.2 的规则）：

- `package mul_hi`：包名。
- `fn mul_hi(a: bits[32] id=1, b: bits[32] id=2) -> bits[32]`：两入参，返回 `bits[32]`；参数带 `id`。
- `aw`/`bw`/`prod`/`hi` 都是**真名**（无点），分别做零扩展、零扩展、64 位乘法、取高 32 位。
- `zero_ext(..., new_bit_count=64)`：与 [ParseExtendOps.ir](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/testdata/ir_parser_round_trip_test_ParseExtendOps.ir#L1-L5) 同构。
- `bit_slice(prod, start=32, width=32)`：与 [ParseBitSlice.ir](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/testdata/ir_parser_round_trip_test_ParseBitSlice.ir#L1-L3) 同构。
- `ret hi`：返回值行。

### 步骤 2：用 opt_main 解析并回打

`opt_main` 的工作链是"读文件 → 解析成 Package → 跑优化管线 → 重新打印 IR"——读输入与写输出的逻辑在：

[xls/tools/opt_main.cc:L200-L243](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/tools/opt_main.cc#L200-L243)（默认输入 `/dev/stdin`、`GetFileContents` 读入、`OptimizeIrForTop` 优化、`output_path == "-"` 时打到 stdout）。

运行（**待本地验证**：以下命令预期可执行，但具体输出需在你本机构建后确认）：

```bash
# 假设已按 u1-l2 构建：bazel build -c opt //xls/tools:opt_main
$(bazel info bazel-bin)/xls/tools/opt_main /tmp/mul_hi.ir
# 或显式指定输入输出：
opt_main /tmp/mul_hi.ir --output_path=-
```

**需要观察的现象 / 预期结果**：

- **若你手写的 IR 合法**：命令成功退出，并把一段结构相同的 IR 打到 stdout——说明解析器完整接受了你的文本（`package` 头、签名、四个节点行、`ret` 全部通过）。
- **关于"逐字节相同"**：`opt_main` 会先**优化**再打印，因此输出不必与输入逐字符相同（优化器可能做乘法窄化等变换、可能重排或重编号节点）。这一点和"纯往返"不同——纯往返（解析后**不**优化、直接 `DumpIr`）才是逐字节不变，仓库里用断言 `EXPECT_EQ(package->DumpIr(), program)` 来保证，见 [xls/ir/ir_parser_test.cc:L867-L877](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/ir_parser_test.cc#L867-L877)（`IrParserTest.NodeNames`：先 `ParsePackage(program)`，再断言 `package->DumpIr() == program`）。本实践中你应重点关注"能否被成功解析并重新打印"，而不是字节级一致。

### 步骤 3：故意制造语法错误，观察解析器报错

把 `/tmp/mul_hi.ir` 复制一份为 `/tmp/mul_hi_bad.ir`，把运算符 `umul` 改成一个不存在的名字 `mull`（或把 `bit_slice` 改成 `bit_slce`）：

```
  prod: bits[64] = mull(aw, bw, id=5)
```

再跑：

```bash
opt_main /tmp/mul_hi_bad.ir
```

**需要观察的现象 / 预期结果**（**待本地验证**）：命令以非零状态退出，stderr 给出 `InvalidArgument` 错误，且消息里包含 `Unknown operation for string-to-op conversion: mull`（与 [xls/ir/ir_parser_error_test.cc:L110-L123](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/ir_parser_error_test.cc#L110-L123) 的 `InvalidOp` 用例一致）。这就验证了 4.3 里"`StringToOp` 对未知串报错、并带位置信息"的行为。

> 进阶尝试：再故意删掉签名里的 `->`，观察报错信息是否变成 `Expected '->' in function signature`（对应 4.3.5 练习 2）。

### 步骤 4（可选，源码阅读型）：找到往返的"黄金断言"

打开 [xls/ir/ir_parser_test.cc:L867-L877](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/ir_parser_test.cc#L867-L877)，确认那条 `EXPECT_EQ(package->DumpIr(), program)`——这是"打印与解析互为逆操作"这条设计契约的可执行化身。整张 `xls/ir/testdata/ir_parser_round_trip_test_*.ir` 都是被这类往返测试驱动的样例。

## 6. 本讲小结

- IR 文本是 IR 内存结构的**可读序列化**，分层排版：`package` → 多个 `fn`/`proc`/`block`/`chan`/`file_number` → 每个 `fn` 内是若干**节点行** `名字: 类型 = 运算符(参数)`。
- 节点名字有两种：真名（如 `hi`）和自动名 `<运算符>.<id>`（如 `add.3`，表示"无有意义名字"）；`ret` 前缀标记函数返回值，`proc` 用 `next`。
- **打印端**由 `Node::ToStringInternal`（行骨架）+ `Node::GetName`（真名/自动名）+ `Function::DumpIr`（签名与 `ret`）+ `FunctionBase::DumpFunctionBaseNodes`（遍历）组成；`OpToString` 把 `Op` 枚举变成文本串。
- **解析端** `Parser` 用 `Scanner` 切词 + 递归下降，入口 `ParsePackage`，主循环按关键字分派到 `ParseFunction`/`ParseProc`/`ParseBlock`/`ParseChannel`；节点行由 `ParseNode` 处理，用 `StringToOp` 把串转回 `Op`，用 `SplitNodeName` 区分真名与自动名。
- **往返是设计契约**：`DumpIr` 注释明说要输出"可解析的"文本，并被 `EXPECT_EQ(package->DumpIr(), program)` 这类测试守护；解析器还提供 `ParsePackageNoVerify`、builder 关 `verify` 等手段以构造畸形 IR 供测试。
- 解析器的报错是带**位置信息**（行列号）的 `InvalidArgument`，典型如 `Unknown operation for string-to-op conversion: <串>`。

## 7. 下一步学习建议

- **向下游走**：本讲只讲"读/写 IR 文本"。接下来可以学 [u3-l4 从 DSLX 到 IR 的转换](u3-l4-dslx-to-ir-conversion.md)，看 `function_converter` 如何**产出**这些节点行——你会更深刻地理解为什么文本格式是这样设计的。
- **向优化走**：能读 IR 文本后，[u4-l1 优化 Pass 框架](u4-l1-optimization-pass-framework.md) 会展示 `opt_main` 里那个"优化管线"到底对图做了什么，解释本讲步骤 2 里"输出会变"的原因。
- **源码延伸阅读**：
  - 想看更复杂的节点行（数组、元组、`select`、`counted_for`），扫一遍 `xls/ir/testdata/ir_parser_round_trip_test_*.ir`，再回 [xls/ir/node.cc:L613 起](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/node.cc#L613) 的 `switch (op_)` 对照每种运算符的打印规则。
  - 想理解 `proc`/`block` 的文本差异，看 [xls/ir/ir_parser.cc:L2115 起](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/ir_parser.cc#L2115) 的 `ParseProcSignature` 与 `ParseBody` 对 `next`/`reg`/`instantiation` 的处理，为 [u3-l5 Proc、Channel 与状态化通信](u3-l5-proc-channel-state.md) 做铺垫。
