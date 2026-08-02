# MLIR 文本 IR 语法速览

## 1. 本讲目标

上一讲（u1-l3）我们已经搞清楚 `mlir-opt` 的入口骨架：它「读 IR → 可选跑 pass → 写 IR」。但那只是「管道」，管道里流动的「水」——也就是 `.mlir` 文本本身——我们还没正式学过怎么读、怎么写。本讲就来补上这一环。

学完本讲，你应当能够：

- 读懂一段标准的 MLIR 文本 IR，并指出其中的操作、操作数、结果、类型、属性各是什么。
- 默写出 operation 的通用语法骨架，理解 `%result = dialect.op(args) {attrs} : (types) -> (types)` 每一段的含义。
- 写出一个带函数、基本块、注释和位置（loc）信息的最小 `.mlir` 文件，并用 `mlir-opt` 验证它能被正确解析。

本讲是「读」和「写」MLIR 的第一课，只覆盖文本语法的皮相，不深入每个方言的具体操作语义——那些留给后续讲义。

## 2. 前置知识

在动手之前，先用最朴素的语言解释几个术语，避免被符号吓到。

- **IR（Intermediate Representation，中间表示）**：编译器内部用来描述程序的数据结构。MLIR 的 IR 既可以序列化成「人能读的文本」，也可以序列化成「紧凑的二进制（字节码）」。本讲只谈文本形式。
- **SSA（Static Single Assignment，静态单赋值）**：一种约定——每个变量（值）只被赋值一次。MLIR 的值名都以 `%` 开头，例如 `%0`、`%sum`，一旦定义就不再被改写。
- **Operation（操作）**：MLIR 里最基本的「积木」，可以是一行加法、一个函数定义、一个循环，乃至一个完整的模块。文本里它就是一行（可能很长）。
- **Type（类型）**：每个值都有一个类型，比如 `i32`（32 位整数）、`f32`、`tensor<4xf32>`。
- **Attribute（属性）**：附在操作上的「常量数据」，编译期就确定，例如 `arith.cmpi` 的比较谓词。
- **Block（基本块）**与 **Region（区域）**：操作被装进基本块，基本块被装进区域，形成层次结构。这些会在 u2 单元精读，本讲只需要会「读」它们在文本里的样子。

如果你对 `mlir-opt` 怎么跑还陌生，请先回到 u1-l3 复习「读 IR → 跑 pass → 写 IR」的三段式。

## 3. 本讲源码地图

本讲主要参考 MLIR 官方文档与示例，引用的「源码」是文档与测试用例（它们同样是仓库里受版本管理的文件，可永久链接）：

| 文件 | 作用 |
| --- | --- |
| `docs/LangRef.md` | MLIR 语言参考，是文本语法的权威定义，所有 EBNF 文法都在这里。 |
| `docs/Tutorials/MlirOpt.md` | 官方 `mlir-opt` 使用教程，提供了可直接跑的 IR 示例和命令行。 |
| `test/Examples/mlir-opt/ctlz.mlir` | 教程引用的真实测试文件，含一段最小 `func.func`。 |
| `examples/minimal-opt/README.md` | 说明 `mlir-cat`、`mlir-minimal-opt` 等最小二进制的职责，对应本讲的实践入口。 |
| `include/mlir/IR/BuiltinLocationAttributes.td` | 位置（Location）属性的 TableGen 定义，用于讲解 `loc(...)` 语法。 |

> 提示：本讲不深入 C++ 实现（那是 u2 单元的事），只把文档和示例当作「语法说明书」来读。

## 4. 核心概念与源码讲解

### 4.1 操作（Operation）的通用语法：泛型形式与自定义形式

#### 4.1.1 概念说明

MLIR 没有一份写死的「全部操作清单」——操作是可无限扩展的。但不管操作多花哨，它在文本里都遵循同一套**通用语法（generic form）**，这是 IR 能可靠地「打印 → 再解析回来（round-trip）」的根基。

通用形式的关键直觉是：一行操作 = 「谁来接收结果」+「操作叫什么名字」+「给它哪些输入」+「它的常量属性」+「它的子区域」+「输入输出的类型签名」。其中最不可省略的是结尾的**函数类型签名**（冒号后那一段），它同时说明了操作数和结果的类型。

除了通用形式，方言可以为「已知操作」注册一种更顺眼的**自定义装配形式（custom assembly form）**。例如 `arith.addi %a, %b : i32` 比它等价的通用形式 `"arith.addi"(%a, %b) : (i32, i32) -> i32` 更好读。两种形式在内存里是同一个操作，只是打印风格不同。

#### 4.1.2 核心流程

通用操作的 EBNF 文法骨架（来自 LangRef）：

```
operation             ::= op-result-list? (generic-operation | custom-operation)
                          trailing-location?
generic-operation     ::= string-literal `(` value-use-list? `)`  successor-list?
                          dictionary-properties? region-list? dictionary-attribute?
                          `:` function-type
custom-operation      ::= bare-id custom-operation-format
op-result-list        ::= op-result (`,` op-result)* `=`
```

把它翻译成「从左到右读一行操作」的步骤：

1. **结果列表**（可选）：`%r = ` 或 `%a, %b = ` 或 `%r:2 = `。多结果时可以用 `%r:2` 表示「这个操作产 2 个结果」，也可以给每个结果单独命名。
2. **操作名**：通用形式里是带引号的字符串 `"foo.div"`；自定义形式里是裸标识符 `arith.addi`。
3. **操作数列表**：`(value-use-list?)`，用逗号分隔的 `%` 值，比如 `(%a, %b)`。
4. **后继列表**（可选）：`[^bb1, ^bb2]`，用于控制流，本讲先不展开。
5. **属性/属性字典**（可选）：`<{fruit = "banana"}>`（properties）或 `{some_attr = "value"}`（属性字典）。
6. **区域列表**（可选）：`( region (, region)* )`，用大括号包住的子 IR，如函数体。
7. **类型签名**（必填）：`: function-type`，形如 `(i32, i32) -> i32`。
8. **位置**（可选）：`loc(...)`，见 4.5 节。

#### 4.1.3 源码精读

LangRef 用一段 EBNF 正式定义了 `operation` 文法，这段是我们「写操作」的宪法：

[docs/LangRef.md:290-305](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/docs/LangRef.md#L290-L305) —— 定义 `operation`、`generic-operation`、`custom-operation`、`op-result-list`、`dictionary-attribute`、`trailing-location` 的完整文法，注意 `function-type` 前的冒号是必填的。

紧接着的例子把抽象文法落成具体文本，值得逐行对照：

[docs/LangRef.md:326-340](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/docs/LangRef.md#L326-L340) —— 演示了 `%result:2 = "foo_div"() : () -> (f32, i32)`（多结果）、带 properties 的 `"tf.scramble"(...)<{fruit = "banana"}>`、带属性字典的 `{some_attr = "value", other_attr = 42 : i64}` 三种写法。

随后一句解释了「自定义形式」从何而来：

[docs/LangRef.md:342-344](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/docs/LangRef.md#L342-L344) —— 说明方言可以为已注册操作提供 *custom assembly form*，这正是 `arith.addi` 能比 `"arith.addi"` 更简洁的原因。

#### 4.1.4 代码实践

**实践目标**：亲手把一个操作的「通用形式」和「自定义形式」互相印证。

**操作步骤**：

1. 写一个最小文件 `add.mlir`，内容是通用形式：
   ```mlir
   module {
     func.func @add(%a : i32) -> i32 {
       %r = "arith.addi"(%a, %a) : (i32, i32) -> i32
       func.return %r : i32
     }
   }
   ```
2. 运行 `mlir-opt add.mlir`（不带任何 pass）。
3. 观察输出。

**需要观察的现象**：`mlir-opt` 会把通用形式「归一化」打印成自定义形式，即你大概率会看到 `%r = arith.addi %a, %a : i32`，引号和括号被去掉。

**预期结果**：两种形式解析后是同一个操作，所以 round-trip 后打印成哪种形式由方言注册的自定义装配格式决定。

**待本地验证**：如果你构建的是 `examples/minimal-opt`，它默认只注册了最小方言集合，可能不认识 `arith`。此时可改用完整的 `mlir-opt`，或在通用形式上保留 `"arith.addi"` 并配合 `--allow-unregistered-dialect`（参见 u1-l3 关于注册白名单的说明）。

#### 4.1.5 小练习与答案

**练习 1**：`%x:2 = "foo.div"() : () -> (i32, i64)` 这行操作有几个结果？分别是什么类型？
**答案**：2 个结果，第 1 个是 `i32`，第 2 个是 `i64`。`%x:2` 中的 `:2` 表示结果数量，与签名 `-> (i32, i64)` 对应。

**练习 2**：为什么通用形式结尾的 `: function-type` 不可省略？
**答案**：它同时给出了所有操作数和所有结果的类型，是解析器判断 IR 是否良构（类型是否匹配）的依据；自定义形式虽然写法不同，但类型信息同样必须存在（如 `arith.addi %a, %a : i32`）。

### 4.2 类型系统与 function-type

#### 4.2.1 概念说明

MLIR 的类型系统是**开放的**——没有一份写死的类型清单，方言可以任意定义新类型。但所有操作的类型签名都共享同一种写法：**函数类型 `function-type`**，它描述「操作数类型列表 → 结果类型列表」。

值得特别记住的一点（u1-l1 也提过）：MLIR 的整数类型是 **signless（无符号约定）** 的。`i32` 只表示「32 位」，它本身不携带「有符号/无符号」语义——符号由具体操作解释（例如 `arith.addi` 与无符号/有符号比较分开成不同操作/属性）。这是它与 LLVM IR 的一个显著差异。

#### 4.2.2 核心流程

函数类型与列表的文法：

```
type-list-no-parens ::=  type (`,` type)*
type-list-parens    ::= `(` `)` | `(` type-list-no-parens `)`
function-type       ::= (type | type-list-parens) `->` (type | type-list-parens)
```

读类型签名的要点：

- 箭头 `->` 左边是操作数类型，右边是结果类型。
- 单个类型可以不加括号，多个类型必须用 `(t1, t2)` 包起来；零个操作数/结果用 `()` 表示。
- 常见 builtin 类型：`i32`/`i64`（整数）、`f32`/`f64`（浮点）、`tensor<4xf32>`（张量）、`memref<10xf32>`（可寻址缓冲区）、`index`（平台相关的下标类型）。

#### 4.2.3 源码精读

[docs/LangRef.md:645-667](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/docs/LangRef.md#L645-L667) —— 给出 `type`、`type-list-parens`、`ssa-use-and-type`、`function-type` 的完整文法，是写类型签名的权威依据。

一个真实的类型签名出现在教程示例里：

[test/Examples/mlir-opt/ctlz.mlir:1-9](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/test/Examples/mlir-opt/ctlz.mlir#L1-L9) —— 其中 `func.func @main(%arg0: i32) -> i32` 与 `%0 = math.ctlz %arg0 : i32` 都展示了「值 : 类型」和函数签名 `(i32) -> i32` 的写法。

#### 4.2.4 代码实践

**实践目标**：通过给同一操作换不同类型，体会「类型签名」是操作的一部分。

**操作步骤**：在 `add.mlir` 基础上，把函数改成接收两个 `f32`，调用 `arith.addf`：

```mlir
module {
  func.func @addf(%a : f32, %b : f32) -> f32 {
    %r = arith.addf %a, %b : f32
    func.return %r : f32
  }
}
```

**需要观察的现象**：`addf` 是浮点加法，类型必须是 `f32`；如果你故意把 `addf` 配上 `i32`，`mlir-opt` 会在验证阶段报类型不匹配。

**预期结果**：类型一致时无错输出；类型不一致时打印诊断错误（diagnostic），并指出哪个值的类型不符。

**待本地验证**：错误的具体措辞以本地 `mlir-opt` 版本为准。

#### 4.2.5 小练习与答案

**练习 1**：写出「接收一个 `i32`、一个 `f64`，返回 `tensor<4xi32>`」的函数签名。
**答案**：`(%a : i32, %b : f64) -> tensor<4xi32>`，函数类型写作 `(i32, f64) -> tensor<4xi32>`。

**练习 2**：为什么 `i32` 不区分有符号/无符号？
**答案**：MLIR 让整数类型 signless，把符号语义交给操作本身（如比较操作通过谓词属性区分有符号/无符号），这样可以避免在类型层面冗余地携带符号信息、减少等价但写法不同的 IR。

### 4.3 属性（Attribute）与 properties

#### 4.3.1 概念说明

**属性（Attribute）**是附在操作上的「编译期常量数据」。它出现在变量永远不被允许出现的地方——例如 `arith.cmpi` 的「比较谓词」（小于？等于？……）必须是个常量，不能是个运行时值。

每个操作都有一个**属性字典**，把一组「名字 → 属性值」挂在身上。属性值本身也很丰富：builtin 方言提供了整数、字符串、数组、字典、稠密张量等。

这里有两个容易混的概念，务必分清：

- **inherent attributes（固有属性）**：操作语义本身就要求的属性，名字不带方言前缀，例如 `arith.cmpi` 的 `predicate`。
- **discardable attributes（可丢弃属性）**：由外部（通常是某个方言）定义语义，名字必须带方言前缀（如 `gpu.container_module`），由对应方言负责校验。

近年来 MLIR 引入了 **properties**：它把 inherent 属性存成操作 C++ 类的直接数据成员，既更高效，也能被接口暴露。properties 在文本里用 `<{...}>` 打印，而可丢弃属性仍留在 `{...}` 字典里。

#### 4.3.2 核心流程

属性的文法：

```
attribute-entry      ::= (bare-id | string-literal) `=` attribute-value
attribute-value      ::= attribute-alias | dialect-attribute | builtin-attribute
dictionary-attribute ::= `{` (attribute-entry (`,` attribute-entry)*)? `}`
dictionary-properties::= `<` dictionary-attribute `>`
```

读属性字典的要点：

- 整个字典用花括号 `{}` 包住，里面是逗号分隔的 `key = value`。
- 属性值可以带类型，例如 `42 : i64` 表示「值为 42、类型为 i64」的整数属性。
- properties 用 `<{...}>`，普通属性字典用 `{...}`。

#### 4.3.3 源码精读

[docs/LangRef.md:787-803](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/docs/LangRef.md#L787-L803) —— 定义 `attribute-entry`、`attribute-value` 文法，并说明属性用于「在变量不允许处指定常量数据」。

[docs/LangRef.md:810-819](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/docs/LangRef.md#L810-L819) —— 区分 inherent 与 discardable 属性，并以 `arith.cmpi` 的 `predicate` 和 `gpu.container_module` 为例。

[docs/LangRef.md:778-785](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/docs/LangRef.md#L778-L785) —— 介绍 properties：操作类的直接数据成员，可序列化为 Attribute 以便通用打印。

带属性的真实写法见 4.1.3 引用的 LangRef 例子，其中 `{some_attr = "value", other_attr = 42 : i64}` 是属性字典，`<{fruit = "banana"}>` 是 properties。

#### 4.3.4 代码实践

**实践目标**：在文本里给一个操作挂上一个属性，并验证它被保留。

**操作步骤**：写 `attr.mlir`：

```mlir
module {
  func.func @cmp(%a : i32, %b : i32) -> i1 {
    // cmpi 的谓词是一个 inherent 属性，这里 0 表示 "eq"（等于）
    %r = arith.cmpi eq, %a, %b : i32
    func.return %r : i1
  }
}
```

运行 `mlir-opt attr.mlir`。

**需要观察的现象**：`arith.cmpi` 的谓词 `eq` 在自定义形式里很简洁；但如果你让它打印成通用形式（许多变换后或带某些打印选项时），会看到谓词以属性形式出现，类似 `<{predicate = 0 : i64}>`。

**预期结果**：round-trip 后谓词语义不变；你可以尝试故意写一个非法谓词名，观察验证器报错。

**待本地验证**：谓词对应的整数值（`eq=0` 等）以本地 `arith` 方言文档为准。

#### 4.3.5 小练习与答案

**练习 1**：`{fruit = "banana"}` 和 `<{fruit = "banana"}>` 有什么区别？
**答案**：前者是「属性字典（dictionary-attribute）」，通常用于可丢弃属性；后者是 `dictionary-properties`，表示这些数据作为 properties 直接存在操作对象上（多为 inherent 属性）。

**练习 2**：inherent 属性和 discardable 属性在命名上如何区分？
**答案**：inherent 属性名不带方言前缀（如 `predicate`），discardable 属性名必须带方言前缀（如 `gpu.container_module`）。

### 4.4 函数（func.func）与基本块（Block）

#### 4.4.1 概念说明

MLIR 里的「函数」并不是语言级特例，而是一个普通的操作——`func.func`（来自 func 方言）。它的「函数体」是一个 **Region（区域）**，区域里是若干 **Block（基本块）**，每个块是一串按顺序执行的操作，并以一个**终止符（terminator）**结尾（如 `func.return`）。

MLIR 在表达控制流时做了一个重要选择：**用块参数（block arguments）代替传统 SSA 的 PHI 节点**。也就是说，分支跳转时传递的值，写成目标块的「入口参数」，而不是在块首放一排 PHI。这让函数参数也变得很自然——函数参数其实就是入口块的参数。

块用 `^名字` 标记（称为 caret-id，如 `^bb1`），块的参数写在标签后面的括号里。

#### 4.4.2 核心流程

块的文法：

```
block        ::= block-label operation+
block-label  ::= block-id block-arg-list? `:`
block-id     ::= caret-id
caret-id     ::= `^` suffix-id
block-arg-list ::= `(` value-id-and-type-list? `)`
```

读懂一个含分支的函数：

1. `func.func @name(参数列表) -> 返回类型 { ... }`：整体是一个操作，大括号内是它的区域。
2. 区域里第一个块是**入口块**，可以不写标签；其它块以 `^bbN(...):` 开头。
3. 每个块最后必须是终止符：`cf.br ^bb2(...)`（无条件跳转）、`cf.cond_br %cond, ^t, ^f`（条件跳转）、`func.return %v : T`（返回）。
4. 跳转可以在括号里向目标块传值，这些值绑定到目标块的块参数。

#### 4.4.3 源码精读

[docs/LangRef.md:357-368](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/docs/LangRef.md#L357-L368) —— 定义 `block`、`block-label`、`caret-id`、`block-arg-list` 文法。

[docs/LangRef.md:370-396](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/docs/LangRef.md#L370-L396) —— 说明块是一串操作；SSACFG 区域里每个块是基本块，必须以终止符结尾；并解释了「块参数代替 PHI」的设计。

LangRef 给出的分支示例几乎涵盖了你需要会读的所有控制流写法：

[docs/LangRef.md:400-423](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/docs/LangRef.md#L400-L423) —— `@simple` 函数：`^bb0(%a, %cond)` 入口块带参数、`cf.cond_br` 条件分支、`cf.br ^bb3(%a : i64)` 跳转传值、`^bb3(%c: i64)` 接收前驱传来的参数、最后 `return` 收尾。逐行读这段是本节最重要的练习。

一个没有显式分支的、最朴素的函数（单入口块）见教程测试文件：

[test/Examples/mlir-opt/ctlz.mlir:5-8](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/test/Examples/mlir-opt/ctlz.mlir#L5-L8) —— `func.func @main(%arg0: i32) -> i32 { ... func.return %0 : i32 }`，展示了「函数 = 一个区域 = 一个入口块 + return 终止符」的最简形态。

#### 4.4.4 代码实践

**实践目标**：手写一个含条件分支的函数，理解块参数如何替代 PHI。

**操作步骤**：写 `branch.mlir`：

```mlir
module {
  func.func @abs_or_double(%x : i32, %neg : i1) -> i32 {
    cf.cond_br %neg, ^neg, ^pos

  ^neg:
    %zero = arith.constant 0 : i32
    %sub = arith.subi %zero, %x : i32
    cf.br ^done(%sub : i32)

  ^pos:
    %dbl = arith.addi %x, %x : i32
    cf.br ^done(%dbl : i32)

  ^done(%result : i32):
    func.return %result : i32
  }
}
```

运行 `mlir-opt branch.mlir`。

**需要观察的现象**：`^done(%result : i32)` 这个块接收一个参数，它的值在运行时由两条 `cf.br` 之一传入（`%sub` 或 `%dbl`）。这正是传统 SSA 里 PHI 节点干的事，但在 MLIR 里写成了「块参数 + 跳转传值」。

**预期结果**：解析通过、round-trip 无错；如果把 `^done` 的参数类型写成 `i64`，会因与传入值类型不符而报错。

**待本地验证**：`arith.constant`、`arith.subi`、`cf.cond_br`、`cf.br` 的具体拼写以本地注册的方言为准（这些都是标准方言操作）。

#### 4.4.5 小练习与答案

**练习 1**：在 `@abs_or_double` 里，`%result` 的值是从哪来的？
**答案**：它来自两条前驱分支的跳转——`^neg` 里 `cf.br ^done(%sub : i32)` 传 `%sub`，`^pos` 里 `cf.br ^done(%dbl : i32)` 传 `%dbl`。运行时实际取哪个，取决于 `%neg` 的值。

**练习 2**：为什么 MLIR 不用 PHI 节点而用块参数？
**答案**：块参数让函数参数不再是特例（它就是入口块参数），也使「并行拷贝语义」更直观，减少了 IR 的特殊情况（详见 LangRef 引用的 Rationale）。本讲只要求会读，深度理由在后续单元讨论。

### 4.5 注释、标识符与位置（loc）信息

#### 4.5.1 概念说明

最后一块拼图是「边角料」：注释、标识符命名规则和位置信息。这些不影响 IR 的语义正确性，但决定你写出来的文本能否被解析、以及出错时能否被定位。

- **注释**：MLIR 用 BCPL 风格 `//`，到行尾结束，和 C/C++/Java 一样。
- **标识符（identifier）**：值名以 `%` 开头（如 `%0`、`%sum`），函数/符号名以 `@` 开头（如 `@main`），块名以 `^` 开头，类型别名以 `!` 开头，属性别名以 `#` 开头。这套「前缀（sigil）」保证标识符永远不会和未来的关键字撞车。注意：值名只是文本里的「昵称」，IR 本身不持久化它——打印器会重新分配匿名名字（如 `%42`）。
- **位置（location）**：每个操作都可以带一个 `loc(...)`，记录它在源代码里的出处（文件:行:列）。位置是**可调试性**的核心：诊断信息、错误定位都依赖它。常见形式是 `loc("foo.mlir":10:5)`，也可以是 `loc(unknown)`。

#### 4.5.2 核心流程

读位置信息的要点：

1. 位置出现在操作的最末尾，文法上是可选的 `trailing-location`，即 `loc( location )`。
2. 最常用的内置位置是 **FileLineColLoc**（更准确的内部名是 `FileLineColRange`），文本写作 `loc("文件名":行:列)`。
3. 位置本身也是一种**属性**（location attribute），由 builtin 方言提供，所以它能像别的属性一样被序列化和解析。

#### 4.5.3 源码精读

[docs/LangRef.md:162-181](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/docs/LangRef.md#L162-L181) —— 定义整数字面量、浮点字面量、字符串字面量，并明确「注释用 `//`，到行尾」。

[docs/LangRef.md:195-226](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/docs/LangRef.md#L195-L226) —— 定义 `value-id`（`%` 开头）、`symbol-ref-id`（`@` 开头）等标识符文法，并解释「sigil 防撞」与「值名不持久化」两条关键规则。

[docs/LangRef.md:304](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/docs/LangRef.md#L304) —— 定义 `trailing-location ::= loc ( location )`，说明位置挂在操作尾部。

位置属性的具体语法由 TableGen 定义，是最权威的「loc 写法」来源：

[include/mlir/IR/BuiltinLocationAttributes.td:66-91](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/include/mlir/IR/BuiltinLocationAttributes.td#L66-L91) —— `FileLineColRange` 的定义，给出文法 `filelinecol-location ::= string-literal : integer-literal : integer-literal ...`，并举例 `loc("mysource.cc":10:8 to 12:18)`。它支持 `file:line`、`file:line:column`、乃至一个范围。

#### 4.5.4 代码实践

**实践目标**：给操作显式附加位置信息，并观察 `mlir-opt` 是否保留它。

**操作步骤**：写 `loc.mlir`：

```mlir
module {
  func.func @add(%a : i32) -> i32 {
    %r = arith.addi %a, %a : i32 loc("add.mlir":3:12)
    func.return %r : i32
  }
}
```

运行 `mlir-opt loc.mlir`。

**需要观察的现象**：默认打印常常会省略位置（为减少噪声），但位置信息确实存在于内存 IR 中。如果你让工具打印诊断（例如故意制造一处类型错误），错误信息里会出现 `add.mlir:3:12` 这样的出处，说明位置被用上了。

**预期结果**：附加位置本身不会改变操作的语义；它的价值体现在出错诊断和调试工具（如 `mlir-lsp-server`，将在 u9 介绍）里。

**待本地验证**：默认打印是否带 `loc(...)` 取决于打印选项（如 `print-debuginfo` 之类），不同版本表现可能不同。

#### 4.5.5 小练习与答案

**练习 1**：`%sum` 这个名字会被存进 IR 吗？
**答案**：不会。值名只是文本里的便利昵称，解析后 IR 只保留 SSA 值的「定义-使用」关系；打印器会重新分配匿名名（如 `%42`）。

**练习 2**：为什么 MLIR 的标识符都要带 `%`、`@`、`^`、`#`、`!` 这样的前缀？
**答案**：前缀（sigil）把「用户标识符」和「语言关键字」隔离开，未来新增关键字也不会与已有标识符冲突，保证语法稳定。

## 5. 综合实践

把本讲五个模块串起来，完成一个「最小可运行 IR」的撰写与验证。

**任务**：手写一个文件 `mini.mlir`，要求同时包含：

1. 一个 `module { ... }` 顶层容器（builtin 方言的操作）。
2. 一个 `func.func`，接收一个 `i32` 参数，返回 `i32`。
3. 函数体内**两个算术运算**（例如先 `arith.constant` 一个常数，再做一次 `arith.addi` 或 `arith.muli`）。
4. 一个 `func.return` 返回结果。
5. 至少一行 `//` 注释，给关键操作附一个 `loc("mini.mlir":行:列)` 位置。

参考答案（自己先写，再对照）：

```mlir
// 一个最小的 MLIR 文本 IR：计算 (x + x) * 3 的某种近似
module {
  func.func @calc(%x : i32) -> i32 {
    // 先把 x 翻倍
    %dbl = arith.addi %x, %x : i32
    // 再乘以常量 3
    %three = arith.constant 3 : i32
    %r = arith.muli %dbl, %three : i32 loc("mini.mlir":7:10)
    func.return %r : i32
  }
}
```

**验证步骤**：

1. 用 `mlir-opt mini.mlir`（不带 pass）做 round-trip。若无错误且能打印回 IR，即说明文本良构——这正是官方教程推荐的「测试输入是否 well-formed」的方法（[docs/Tutorials/MlirOpt.md:28-31](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/docs/Tutorials/MlirOpt.md#L28-L31)）。
2. 进一步，可以加一个 pass 体验「变换」，例如 `mlir-opt --pass-pipeline="builtin.module(canonicalize)" mini.mlir`（`canonicalize` 是规范化 pass，详见 u6 单元），观察常量折叠后 IR 如何变化。
3. 如果你构建了 `examples/minimal-opt`（见 [examples/minimal-opt/README.md:1-13](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/examples/minimal-opt/README.md#L1-L13)），对比它和完整 `mlir-opt` 的差异：`mlir-minimal-opt` 带了 pass 基础设施但方言注册更少，因此可能不认识 `arith`——这正好印证 u1-l3 讲过的「注册白名单」本质。

**关于 `--verify-diagnostics`**：练习规格里提到「`--verify-diagnostics` 或无报错即可」。需要澄清：`--verify-diagnostics` 主要用于测试场景，配合文件里的 `// expected-error` / `// expected-remark` 标记来断言诊断输出；如果你的文件不含这些标记，只需保证普通 round-trip 不报错即可判定良构。

**待本地验证**：以上命令均需在你本地构建好的 MLIR 上运行；不同版本的方言注册情况与 pass 名字可能略有差异。

## 6. 本讲小结

- MLIR 文本 IR 的根基是 **operation**，通用语法骨架为 `%results = "op"(operands) <{properties}> {attrs} : (operand-types) -> (result-types) loc(...)`，其中类型签名不可省略。
- 方言可为已注册操作提供更顺眼的**自定义装配形式**（如 `arith.addi`），它与通用形式在内存里是同一个操作。
- **类型系统是开放的**，所有操作共享 `function-type`（`(types) -> types`）写法；整数 `i32` 是 signless，符号由操作解释。
- **属性**是编译期常量数据，分 inherent（不带方言前缀）和 discardable（带前缀）；新近的 **properties** 把固有属性存为操作类的直接成员，文本用 `<{...}>`。
- 控制流用**块参数代替 PHI**：`func.func` 的函数体是一个区域，区域里是基本块，跳转 `cf.br ^bb(args)` 向目标块传值。
- **注释**用 `//`；标识符靠 sigil（`%@^#!`）防撞且值名不持久化；**位置 `loc("file":line:col)`** 是可调试性的基础。

## 7. 下一步学习建议

本讲你只学了「读和写文本」，还没碰这些文本在内存里到底长什么样。下一步建议进入 **u2 单元（核心数据结构）**：

- 先读 **u2-l1 IR 总体结构导览**，把 Operation/Region/Block/Value 的层次关系和官方结构图对上号。
- 再按顺序精读 **u2-l2 Operation**、**u2-l3 Value**、**u2-l4 Block 与 Region**、**u2-l5 Type 与 Attribute**，看看你今天写的每一行文本，在 C++ 里对应什么样的对象和内存布局。

如果你更想先动手跑 pass，也可以先跳到 **u5-l1 Pass 与 PassManager**，但理解 IR 数据结构会让后面的变换讲义事半功倍。

推荐的延伸阅读：`docs/LangRef.md` 全文（本讲只摘了核心文法），以及 `docs/Tutorials/Toy/` 系列（一个完整的从语言到 MLIR 的前端示例）。
