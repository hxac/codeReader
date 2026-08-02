# MLIR 设计哲学与核心 IR

## 1. 本讲目标

本讲是 MLIR 单元（u7）的第一篇，目的是帮读者建立对 MLIR 的「全局心智模型」。学完后你应当能够：

- 说清楚 **MLIR 是什么**、它和本手册 u3 讲过的 LLVM IR 有何本质区别；
- 理解 MLIR 最核心的设计信条——**一切皆 Operation**（Everything is an Operation）；
- 画出 **Operation → Region → Block → Operation** 的嵌套层次，并解释为什么 MLIR 的 IR 是一棵「树」而不是 LLVM IR 那样的「扁平模块」；
- 认识**方言（Dialect）**这个可扩展机制带来的好处：任何人都可以定义自己的 IR 而无需修改 MLIR 核心；
- 打开一段 `.mlir` 文本，辨认其中的 Operation、Region、Block，并把它们和源码里的 C++ 字段对应起来。

本讲只读不改，是后续 u7-l2（方言机制）、u7-l3（Pass/Pattern/Conversion）、u7-l4（Toy 教程）的概念地基。

## 2. 前置知识

### 2.1 先回忆 LLVM IR 的世界（来自 u3）

在 u3 系列里你已经熟悉了 LLVM IR 的对象模型：一棵包含树 `Module ⊃ Function ⊃ BasicBlock ⊃ Instruction`，以及统一的根基类 `Value` 和 `Use/User` 构成的 SSA def-use 链。LLVM IR 是**一套写死的、固定的中间表示**——它的指令集（`add`、`load`、`call`……）、类型系统（`i32`、`ptr`、`<4 x float>`……）都是 LLVM 项目硬编码的。

这套设计很强，但有一个隐含约束：**所有语言、所有阶段，都必须最终坍缩到这一套统一的低级 IR 上**。这意味着一个高级抽象（比如「张量归约」「GPU 线程块」「量子门」）在被翻译成机器码之前，很早就失去了它的高级结构，优化器只能在很低层的形态上做通用优化。

### 2.2 MLIR 要解决的问题：多级、可扩展

MLIR（Multi-Level Intermediate Representation，多级中间表示）正是为打破这个约束而生。它的核心主张是：

> **编译过程不应该只有「一个」IR，而应该有一族「不同抽象层级」的 IR，编译就是把高层 IR 一步步「下降（lowering）」到低层 IR，直到最终落到 LLVM IR（乃至 LLVM 后端）上。**

举个真实的例子（来自 MLIR 官方 Toy 教程，详见 u7-l4），一个玩具语言的编译路径是这样的：

```
Toy AST  ──MLIRGen──▶  toy 方言  ──lowering──▶  affine 方言
        ──lowering──▶  vector / scf 方言  ──lowering──▶  llvm 方言  ──▶  LLVM IR
```

每一层方言都是合法的 MLIR IR，都可以被分析、被优化、被打印成文本。这和 LLVM IR「只有一个层级」形成鲜明对比。

### 2.3 三个关键术语

| 术语 | 一句话解释 |
|------|-----------|
| **Operation（操作）** | MLIR IR 的**唯一**基本构件，相当于 LLVM IR 的「指令」，但能力远强于指令。 |
| **Region / Block** | Operation 内部可以嵌套 Region，Region 里放 Block，Block 里又放 Operation——形成可嵌套的树。 |
| **Dialect（方言）** | 一组 Operation/Type/Attribute 的命名集合，用「点号」前缀区分（如 `func.func`、`arith.addi`）。方言是可扩展性的载体。 |

整篇讲义会反复回到这三点上。下面进入源码。

## 3. 本讲源码地图

本讲涉及的关键头文件都在 `mlir/include/mlir/IR/` 下，它们定义了 MLIR IR 的核心数据结构：

| 文件 | 作用 |
|------|------|
| [mlir/include/mlir/IR/Operation.h](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/IR/Operation.h) | 定义 `Operation` 类——MLIR IR 的唯一基本单元。本讲主角。 |
| [mlir/include/mlir/IR/OpDefinition.h](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/IR/OpDefinition.h) | 定义 `OpState`、`Op<>` 模板和 `OpTrait`。把通用的 `Operation*` 包装成类型安全的「具体 Op」（如 `FuncOp`、`AddIOp`）。 |
| [mlir/include/mlir/IR/Builders.h](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/IR/Builders.h) | 定义 `Builder` 与 `OpBuilder`，提供构造类型/属性/Operation 的便捷 API（对应 u3-l4 讲过的 LLVM `IRBuilder`）。 |
| [mlir/include/mlir/IR/Region.h](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/IR/Region.h) | 定义 `Region` 类——一个 Block 的链表，挂在某个 Operation 上。 |
| [mlir/include/mlir/IR/Block.h](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/IR/Block.h) | 定义 `Block` 类——一个有序的 Operation 列表，类似 LLVM 的 BasicBlock。 |
| [mlir/include/mlir/IR/OperationSupport.h](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/IR/OperationSupport.h) | 定义 `OperationState` 等辅助类型，是构造一个 Operation 时的「参数包」。 |

> 提示：MLIR 的 IR 头文件互相 `#include` 较多，阅读顺序建议先 `Block.h` → `Region.h` → `Operation.h` → `OpDefinition.h` → `Builders.h`，从最内层向外层。但讲义里我们按「先 Operation 这个主角，再嵌套层次」的顺序讲解。

## 4. 核心概念与源码讲解

### 4.1 MLIR 设计哲学：可扩展的多级 IR 与方言

#### 4.1.1 概念说明

如果说 LLVM IR 的设计哲学是「**一套 IR 统一所有语言与目标**」，那么 MLIR 的设计哲学可以浓缩成一句话：

> **IR 本身是一个「基础设施」，而不是某一种具体的 IR。** 它提供的是一套「如何描述、构造、变换、验证 IR」的框架；至于具体的 IR 长什么样——有哪些 Operation、哪些类型——交给**方言（Dialect）**来定义，而且任何人在不修改 MLIR 核心的前提下就能新增方言。

这种「基础设施 + 可插拔方言」的分层，带来的直接好处是：

- **不同抽象层共存**：源语言前端可以用高级方言表达张量、循环、算子；后端可以用低级方言表达寄存器、机器指令；中间还可以有 affine、vector、scf 等中间层。
- **可复用的工具链**：不管是哪个方言，都可以复用同一套文本解析器、验证器、Pass 管理器、Printer、模式重写引擎。
- **渐进式下降（Progressive Lowering）**：编译 = 把高层方言一档一档翻译成低层方言，每一步都是小而可验证的变换。

#### 4.1.2 核心流程：一段 MLIR 文本长什么样

在深入 C++ 之前，先用真实 `.mlir` 文本建立直觉。下面这段取自仓库测试文件的写法（来自 `mlir/test/Dialect/Arith/canonicalize.mlir` 的真实片段）：

```mlir
func.func @select_same_val(%arg0: i1, %arg1: i64) -> i64 {
  %0 = arith.select %arg0, %arg1, %arg1 : i64
  return %0 : i64
}
```

逐行解读，能立刻看到本讲的几个主角：

1. `func.func` 和 `arith.select`、`return` 都是 **Operation**，名字里的点号前缀（`func`、`arith`）就是**方言名**。
2. `func.func` 后面跟了一对花括号 `{ ... }`，这对花括号包起来的就是这个 Operation 的 **Region**（函数体）。
3. Region 里面是一段顺序的语句（`%0 = ...` 和 `return`），它们组成一个 **Block**。

把这段文本「翻译」成树状结构就是：

```
func.func @select_same_val        ← Operation（也是顶层 Op，含 1 个 Region）
└─ Region
   └─ Block（入口块，参数 %arg0, %arg1）
      ├─ %0 = arith.select ...    ← Operation
      └─ return %0                ← Operation（终结符 terminator）
```

这就是 MLIR IR 的典型形态：**Operation 套 Region，Region 套 Block，Block 套 Operation**——一棵树。

#### 4.1.3 源码精读：Operation 的名字就是方言入口

「点号分隔方言」这条规则不是约定俗成，而是写死在 `Operation` 类的文档注释里。看 [Operation.h:30-39](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/IR/Operation.h#L30-L39)：

```cpp
/// Operation is the basic unit of execution within MLIR.
///
/// An Operation is defined first by its name, which is a unique string. The
/// name is interpreted so that if it contains a '.' character, the part before
/// is the dialect name this operation belongs to, and everything that follows
/// is this operation name within the dialect.
```

这段注释同时宣告了 MLIR 的两条根本设计：

- **「Operation is the basic unit」**——一切皆 Operation；
- **名字带点号 → 前半是方言名**——这给了方言一个零成本的、与字符串绑定的命名空间，无需在 C++ 层面写复杂的注册表也能识别归属。

而 `Operation` 类只持有名字，不持有「具体是哪种 Op」的强类型信息——具体类型由 `OpDefinition.h` 里的 `Op<>` 模板提供（见 4.2）。这正是「核心基础设施」与「具体方言」解耦的体现：核心只认 `OperationName` 这个字符串，强类型化交给上层。

#### 4.1.4 代码实践：在一段 MLIR 文本里数方言与 Operation

1. **实践目标**：建立「一段 MLIR 文本 = 一组带方言前缀的 Operation」的直觉。
2. **操作步骤**：
   - 打开仓库里的 `mlir/test/Dialect/Arith/canonicalize.mlir`，阅读前若干行。
   - 把每个出现的 Operation 名字（点号前）抄下来，归类到不同方言。
3. **需要观察的现象**：你会看到同一个文件里至少出现 `func`、`arith` 两个方言的 Operation 共存于一段合法 IR 中——这印证了「多方言可共存」。
4. **预期结果**：例如上文的片段里，方言集合 = `{func, arith}`，Operation 至少有 `func.func`、`arith.select`、`func.return`。
5. 若想看更多方言，可浏览 `mlir/test/Dialect/` 下任意子目录的 `.mlir` 文件，列出其中出现的方言前缀。

#### 4.1.5 小练习与答案

**练习 1**：为什么说 MLIR 是「基础设施」而不是「一种 IR」？

> **参考答案**：因为 MLIR 核心提供的是描述/构造/变换 IR 的通用框架（Operation、Region、Block、Pass、Pattern 等），具体的 Operation 集合由可插拔的方言定义。换一组方言就得到一种「新的 IR」，所以它更像生成 IR 的「母版」，而非某一具体 IR。

**练习 2**：在 `arith.select` 这个名字里，方言名和操作名分别是什么？

> **参考答案**：点号前 `arith` 是方言名，点号后 `select` 是该方言内的操作名。

---

### 4.2 一切皆 Operation：Operation 抽象（最小模块 1）

#### 4.2.1 概念说明

MLIR 的 IR 只有一种基本构件：**Operation**。这听起来抽象，关键在于理解 Operation 在 LLVM IR 里「一人分饰多角」——它同时扮演了 LLVM 里的 Instruction、Call、BasicBlock 的角色，甚至扮演 Function、Module 的角色：

- `arith.addi` 这种像一条**指令**；
- `func.func` 这种像一个**函数定义**；
- `func.return` / `scf.yield` 这种是块的**终结符（terminator）**；
- 顶层的 `builtin.module` 像一个**模块**。

它们在内存里**都是同一个 C++ 类 `Operation` 的实例**，区别只在于它们的名字、有没有 Region、带几个结果等属性。这种统一带来的好处是：**所有针对 IR 的通用算法（遍历、模式匹配、打印、验证）只需写一遍**。

一个 Operation 由以下成分组成（这也是它在文本里的通用打印顺序）：

| 成分 | 含义 | 对应文本片段 |
|------|------|-------------|
| **name** | 操作名（带方言前缀） | `arith.addi` |
| **results** | 0 个或多个 SSA 结果值 | `%0 =` |
| **operands** | 0 个或多个操作数（别的 Operation 的结果或 Block 参数） | `(%a, %b)` |
| **attributes / properties** | 编译期常量属性，或固定结构的属性对象 | `{fastmath = ...}` |
| **regions** | 0 个或多个嵌套 Region | `({ ... })` |
| **successors** | 0 个或多个后继 Block（用于控制流） | 隐式或显式 |
| **location** | 源位置信息（可调试性是一等公民） | 通常隐式 |

#### 4.2.2 核心流程：构造一个 Operation 的步骤

无论用 C++ 还是文本解析，构造一个 Operation 都要走同一条路：

1. 准备一个 `OperationState`（参数包：位置、名字、操作数、结果类型、属性、Region 等）；
2. 调用 `Operation::create(...)`，它会一次性分配好内存（包括结果、操作数、Region 等尾部对象）；
3. 把新建的 Operation 插入到某个 Block 的插入点（由 `OpBuilder` 管理）；
4. 校验（verify）通过后，它就是合法 IR 的一部分了。

用一个伪流程表示：

```
OperationState{loc, "arith.addi", operands=[%a,%b], types=[i32]}
        │
        ▼
Operation::create(state)   ──▶  分配内存 + 初始化 results/operands/regions
        │
        ▼
OpBuilder::insert(op)       ──▶  放进某个 Block 的插入点
```

为什么 `Operation::create` 是 `static` 工厂方法、且对象必须堆分配？因为它的内存布局特殊——结果值**排在对象之前**，操作数和 Region **排在对象之后**（尾部分配）。这种「对象本体 + 前缀结果 + 尾部子对象」的设计让一次 `malloc` 拿下全部内存，下面源码精读会看到。

#### 4.2.3 源码精读

**(a) Operation 类的声明与文档。** 看 [Operation.h:83-87](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/IR/Operation.h#L83-L87)：

```cpp
class alignas(8) Operation final
    : public llvm::ilist_node_with_parent<Operation, Block>,
      private llvm::TrailingObjects<Operation, detail::OperandStorage,
                                    detail::OpProperties, BlockOperand, Region,
                                    OpOperand> {
```

两个要点：

- 它 `final`，且混入了 `TrailingObjects<...>`——说明 Operation 用「对象本体 + 尾部紧跟若干子对象」的紧凑内存布局，尾部依次放操作数存储、属性存储、后继 BlockOperand、Region、操作数。
- 它继承 `ilist_node_with_parent<Operation, Block>`——说明 Operation 是某个 `Block` 的链表节点（4.3 会用到这个父指针）。

**(b) 「结果排在对象之前」的特殊布局。** 看 [Operation.h:41-52](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/IR/Operation.h#L41-L52)：

```cpp
/// An Operation defines zero or more SSA `Value` that we refer to as the
/// Operation results. This array of Value is actually stored in memory before
/// the Operation itself in reverse order. ...
///   [Result2, Result1, Result0, Operation]
///                              ^ this is where `Operation*` pointer points to.
///
/// A consequence of this is that this class must be heap allocated, ...
```

这段注释解释了为什么 Operation 不能栈上构造、必须走工厂 `create`——结果数组在对象指针的「负方向」上，只有堆分配才能拿到那片前缀内存。结果对象本身分为 `InlineOpResult`（前 5 个，索引只用 3 bit，与 Type 指针打包）和 `OutOfLineOpResult`（第 6 个起）两种，是一种空间优化。

**(c) 四个 create 工厂重载。** 看 [Operation.h:92-112](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/IR/Operation.h#L92-L112)，它们接受不同的属性形式（`NamedAttrList` 还是已唯一化的 `DictionaryAttr`）和 Region 形式，最终都走同一条分配路径。其中最常用的是接收 `OperationState` 的那个（[Operation.h:104-105](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/IR/Operation.h#L104-L105)）。

**(d) Operation 的关键字段。** 看 [Operation.h:1069-1101](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/IR/Operation.h#L1069-L1101)：

```cpp
Block *block = nullptr;          // 所属的 Block（父指针）
Location location;               // 源位置
const unsigned numResults;       // 结果数（不可变）
const unsigned numSuccs;         // 后继数
const unsigned numRegions : 23;  // Region 数
bool hasOperandStorage : 1;      // 是否有操作数存储
OperationName name;              // 操作名（含方言信息）
DictionaryAttr attrs;            // 可丢弃属性字典
```

这张表正是 4.2.1 里那些「成分」在内存里的落点：`name`、`location`、`attrs`、`numResults/numRegions/numSuccs` 都是 Operation 本体的字段，而 `block` 是指向父 Block 的指针——有了它就能 O(1) 上溯（见 4.3）。

**(e) OperationState 参数包。** 看 [OperationSupport.h:966-976](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/IR/OperationSupport.h#L966-L976)：

```cpp
struct OperationState {
  Location location;
  OperationName name;
  SmallVector<Value, 4> operands;
  SmallVector<Type, 4> types;          // 结果类型
  NamedAttrList attributes;
  SmallVector<Block *, 1> successors;
  SmallVector<std::unique_ptr<Region>, 1> regions;
  ...
};
```

这正是构造 Operation 前要填的「一张表」。`OpBuilder::create(OperationState&)` 就是消费它（见 4.2.4 实践里的调用链）。

**(f) 从通用 Operation 到具体 Op：OpDefinition。** 真实代码里你很少直接操纵裸 `Operation*`，而是用 `Op<>` 模板包装出的具体类型（如 `func::FuncOp`）。看 [OpDefinition.h:1711-1712](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/IR/OpDefinition.h#L1711-L1712)：

```cpp
template <typename ConcreteType, template <typename T> class... Traits>
class Op : public OpState, public Traits<ConcreteType>... {
```

这是典型的 **CRTP + mixin** 设计：具体 Op（`ConcreteType`）通过继承 `Op<自己, 若干 Trait...>`，零开销地「混入」一组能力（如「单结果」「单 Region」「可交换」等），而这些能力（见 [OpDefinition.h:325](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/IR/OpDefinition.h#L325) 起的 `OpTrait` 命名空间）同时提供便捷访问器与自动校验。`OpState`（[OpDefinition.h:100-112](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/IR/OpDefinition.h#L100-L112)）只持有一个 `Operation*`，并提供 `operator->` 转发：

```cpp
Operation *getOperation() { return state; }
Operation *operator->() const { return state; }
```

也就是说，**所有具体 Op 类型在内存里都只是一个裸指针大小**（`static_assert(hasNoDataMembers())` 强制它不能新增数据成员，见 [OpDefinition.h:2110-2114](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/IR/OpDefinition.h#L2110-L2114)），真正的 IR 数据全在 `Operation` 对象里。这是 MLIR「强类型而不付运行时代价」的关键。

#### 4.2.4 代码实践：把文本 Operation 对应到源码字段

1. **实践目标**：用一个 Operation 的「通用打印形式」逐字段对应到 `Operation` 类的字段，验证你理解了 4.2.1 的成分表。
2. **操作步骤**：
   - 写一段最小 `.mlir`（示例代码，非项目原文件），用 MLIR 的**通用形式**显式打印一个 Operation：

     ```mlir
     // 示例代码：通用形式的 Operation
     %r = "arith.addi"(%a, %b) : (i32, i32) -> i32
     ```
   - 对照下表，把每个文本片段映射到 `Operation` 的字段：

     | 文本片段 | 含义 | Operation 字段 |
     |---------|------|---------------|
     | `"arith.addi"` | 操作名（含方言） | `name` (`OperationName`) |
     | `%a, %b` | 操作数 | 尾部 `OpOperand` 数组 |
     | `: (i32, i32)` | 操作数类型 | 由 operands 推导 |
     | `-> i32` | 结果类型 | results（前缀内存）的 Type |
     | `%r` | 结果 SSA 值 | `InlineOpResult`（前 5 个走此路径） |
3. **需要观察的现象**：通用形式把 Operation 的所有成分都摊开可见；而 `arith.addi %a, %b : i32` 这种「自定义形式」只是同一个 Operation 的更易读打印，成分完全相同。
4. **预期结果**：你能不依赖任何工具，口述出「一个 Operation 在内存里包含 name/operands/results/attrs/regions/location，其中 results 在对象之前分配」。
5. 若本地已构建 MLIR，可用 `mlir-opt` 对这段文本做 round-trip（`mlir-opt --mlir-print-op-generic input.mlir`）观察通用形式输出；若未构建，则本实践为「源码阅读型」，重点是把文本与 [Operation.h:1069-1101](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/IR/Operation.h#L1069-L1101) 的字段一一对应。运行结果：**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `Operation` 必须堆分配、不能写成栈对象？

> **参考答案**：因为结果值数组按逆序排在 `Operation` 对象**之前**的内存里（`[Result..., Operation]`），对象指针指向的是中间位置。要拿到这片「负方向」的前缀内存，只能由 `create` 工厂统一 `malloc`，栈对象做不到这一点。

**练习 2**：`func::FuncOp` 和 `arith::AddIOp` 在内存里的大小是一样的吗？为什么？

> **参考答案**：作为 C++ 对象，它们通常都只占一个指针大小（都派生自 `OpState`，且被 `static_assert` 禁止新增数据成员）。差异不在 C++ 对象本身，而在它们所指向的那个 `Operation` 实例内部填充了什么（名字、Region 数等）。「具体类型」只是对同一个 `Operation*` 的强类型视图。

**练习 3**：`Op<>` 模板通过什么机制给具体 Op 添加「单结果」「可交换」这类能力？

> **参考答案**：通过 CRTP + 可变模板混入一组 `Trait`（见 `class Op : public OpState, public Traits<ConcreteType>...`）。每个 Trait 既提供便捷方法（如 `getResult()`），又提供 `verifyTrait` 自动校验，且这一切都在编译期展开、零运行时开销。

---

### 4.3 Region 与 Block：嵌套层次结构（最小模块 2）

#### 4.3.1 概念说明

MLIR 的 IR 不是扁平的，而是一棵**可嵌套的树**。这棵树靠三个类串起来：

- **Operation**：叶子或中间节点（见 4.2）。
- **Region**：挂在一个 Operation 上的「块链表」。一个 Operation 可以有 0 个或多个 Region（例如 `func.func` 有 1 个函数体 Region；`scf.if` 有 2 个 Region——then 与 else）。
- **Block**：Region 内一个有序的 Operation 列表，类似 LLVM 的 `BasicBlock`。Block 可以带参数（`BlockArgument`），用于在 Region 入口接收值。

于是递归关系成立：

```
Operation 包含若干 Region；
Region   是 Block 的链表；
Block    是 Operation 的有序链表；
（Block 里的 Operation 又可包含 Region……如此嵌套）
```

这跟 LLVM IR 的 `Function ⊃ BasicBlock ⊃ Instruction` 看起来像，但有一个本质区别：**LLVM 的 Function 是 Module 的直接子节点，不能再嵌套函数；而 MLIR 的 Operation 可以无限嵌套**（函数里可以有「带 Region 的 Operation」，它的 Region 里又可以有别的函数式 Operation）。正是这种任意嵌套能力，让 MLIR 能表达「张量算子内含循环、循环内含线程块」这类高层结构。

#### 4.3.2 核心流程：从顶层 Operation 到某条指令的访问路径

假设你要访问 `func.func` 函数体里的第一条指令，访问路径是：

```
顶层 Operation (func.func)
  └─ 取它的第 0 个 Region        (Operation::getRegion(0))
      └─ 取该 Region 的入口 Block  (Region::front())
          └─ 取该 Block 的第一条 Operation (Block::front())
```

反过来，任意一个深层 Operation 都能 O(1) 找到它的「容器链」：

```
op->getBlock()           → 它所在的 Block
block->getParent()       → Block 所在的 Region
region->getParentOp()    → Region 所属的 Operation（容器）
……一直上溯到顶层
```

这条「向上找父」的链由三处 `getParent`/`container`/`block` 字段拼出来，下面源码精读会逐一指到。

#### 4.3.3 源码精读

**(a) Region = Block 链表 + 一个父 Operation 指针。** 看 [Region.h:24-26](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/IR/Region.h#L24-L26)：

```cpp
/// This class contains a list of basic blocks and a link to the parent
/// operation it is attached to.
class Region {
```

它的两个核心私有字段见 [Region.h:358-361](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/IR/Region.h#L358-L361)：

```cpp
BlockListType blocks;                 // Block 的侵入式链表
Operation *container = nullptr;       // 指向所属 Operation（即 Region 的「容器」）
```

`container` 就是 Region 向上回到 Operation 的那条边；而 `getParentOp()` 直接返回它（[Region.h:213](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/IR/Region.h#L213)）。`blocks` 是 `llvm::iplist<Block>`（[Region.h:44-45](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/IR/Region.h#L44-L45)），所以 Region 可以像容器一样 `begin()/end()`、`front()/back()`。

**(b) Block = Operation 的有序链表 + 参数列表。** 看 [Block.h:31-33](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/IR/Block.h#L31-L33)：

```cpp
/// `Block` represents an ordered list of `Operation`s.
class alignas(8) Block : public IRObjectWithUseList<BlockOperand>,
                         public llvm::ilist_node_with_parent<Block, Region> {
```

两个继承点值得注意：

- `ilist_node_with_parent<Block, Region>`：Block 是某个 Region 链表的节点，能通过 `getParent()`（[Block.h:53](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/IR/Block.h#L53)）回到它的 Region——这就是 Block 向上的那条边。
- `IRObjectWithUseList<BlockOperand>`：Block 可以被「后继操作」引用（控制流边），这正是 Operation 里 `successors` 字段所指向的目标。

Block 还能带参数（`BlockArgument`），见 [Block.h:109-126](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/IR/Block.h#L109-L126)。Block 参数让一个 Region 入口能像函数形参一样接收值，这是 MLIR 表达结构化控制流（如 `scf.for` 的循环归纳变量）的标准手段——LLVM 的 BasicBlock 没有这个能力，它只能靠 phi 节点。

**(c) Operation 向下的边：Region 的尾部分配。** 回到 Operation，它的 Region 数组是尾部紧跟分配的，访问接口见 [Operation.h:698-708](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/IR/Operation.h#L698-L708)：

```cpp
unsigned getNumRegions() { return numRegions; }
MutableArrayRef<Region> getRegions() {
  if (numRegions == 0)
    return MutableArrayRef<Region>();
  return getTrailingObjects<Region>(numRegions);
}
```

也就是说，Operation → Region 这条向下的边，是「尾部紧跟的 Region 数组」。

**(d) Operation 向上的边：block 字段。** [Operation.h:1070](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/IR/Operation.h#L1070) 的 `Block *block = nullptr;` 配合 `getBlock()`（[Operation.h:230](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/IR/Operation.h#L230)），是 Operation 向上回到 Block 的边。

把 (a)(b)(c)(d) 串起来，就得到完整的双向边：

```
Operation  ──getBlock()──▶  Block  ──getParent()──▶  Region  ──getParentOp()──▶  Operation
（向上）
Operation  ──getRegion(i)──▶  Region  ──front()/getBlocks()──▶  Block  ──front()/getOperations()──▶  Operation
（向下）
```

> 与 u3 对照：这和 LLVM IR 的 `getParent()` 链（Instruction→BasicBlock→Function→Module）思路一致，但 MLIR 多了一层「Region」，且每层都允许任意嵌套，而不是写死四层。

#### 4.3.4 代码实践：在嵌套 IR 里走一遍「向上 / 向下」路径

1. **实践目标**：在一个含 Region 的 Operation 上，手动走一遍「向下到叶子」和「向上到根」两条路径，验证你对层次的理解。
2. **操作步骤**：以下是一段示例 IR（示例代码）：

   ```mlir
   // 示例代码
   func.func @demo() -> i32 {
     %c = arith.constant 1 : i32
     return %c : i32
   }
   ```
   - **向下路径**：`func.func`(Operation) → `getRegion(0)`(函数体 Region) → `front()`(入口 Block) → 遍历其中 Operation（`arith.constant`、`func.return`）。
   - **向上路径**：取 `arith.constant` → `getBlock()` 得到入口 Block → `getParent()` 得到函数体 Region → `getParentOp()` 得到 `func.func`。
3. **需要观察的现象**：向下与向上应得到一致的「父子关系」；函数体 Region 的 `getParentOp()` 正是 `func.func` 这个 Operation。
4. **预期结果**：你能口述出「Operation 经 `getRegion` 下到 Region，Region 经 `front` 下到 Block，Block 经 `front` 下到 Operation；反向用 `getBlock/getParent/getParentOp` 上溯」。
5. 若已构建 MLIR，可用 `mlir-opt input.mlir --mlir-print-debuginfo` 或 `-debug` 观察遍历；否则本实践为「源码阅读型」，依据是 [Operation.h:230](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/IR/Operation.h#L230)、[Region.h:213](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/IR/Region.h#L213)、[Block.h:53](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/IR/Block.h#L53)。运行结果：**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：MLIR 的 `Block` 与 LLVM 的 `BasicBlock`（见 u3-l1）最大的不同之一是什么？

> **参考答案**：MLIR 的 Block 可以带**参数**（`BlockArgument`），让 Region 入口像函数形参一样接收值（如 `scf.for` 的归纳变量）；而 LLVM 的 BasicBlock 没有参数，跨块传值只能用 `phi` 节点。此外，Block 还可以任意深地嵌套在带 Region 的 Operation 里。

**练习 2**：`scf.if` 这个 Operation 通常有几个 Region？它们各自代表什么？

> **参考答案**：通常有 2 个 Region，分别对应 then 分支与 else 分支的语句块。（`scf.if` 若有结果，还会把两个 Region 的 yield 值汇成结果。）这体现了「一个 Operation 通过多个 Region 表达结构化控制流」。

**练习 3**：给定一个深层 Operation `op`，写出用 C++ API 上溯到「最顶层 Operation」的循环思路。

> **参考答案**：反复取 `op->getParentOp()`，直到返回 `nullptr` 为止（顶层 Operation 没有父 Operation）。沿途的 `getBlock() → getParent() → getParentOp()` 链即为其完整容器路径。`Operation` 类已内置 `getParentOfType<T>()`（见 [Operation.h:254-261](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/IR/Operation.h#L254-L261)）封装了这类向上查找。

---

## 5. 综合实践

把本讲三个模块（设计哲学、Operation、Region/Block）串起来，完成下面这个「读图 + 读码」小任务：

**任务**：阅读下面这段示例 IR（综合了多个方言与一层嵌套），完成「结构标注」与「源码对应」。

```mlir
// 示例代码
func.func @add_one(%x: i32) -> i32 {
  %one = arith.constant 1 : i32
  %sum = arith.addi %x, %one : i32
  return %sum : i32
}
```

1. **画嵌套树**：以 `func.func` 为根，画出 Operation → Region → Block → Operation 的树状结构，标出每层的类名（`Operation` / `Region` / `Block`）。
2. **数方言**：列出本段用到的所有方言，并说明每个 Operation 名字的「点号前 = 方言名」依据来自源码哪段注释。
3. **字段映射**：对 `arith.addi` 这条 Operation，把它的文本成分（名字、操作数、结果、位置）映射到 [Operation.h:1069-1101](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/IR/Operation.h#L1069-L1101) 里的字段。
4. **走双向边**：对 `arith.constant`，分别写出「向下没有子 Region」「向上经 `getBlock()/getParent()/getParentOp()` 回到 `func.func`」的路径，并指出每一步用到的方法定义在哪个头文件。
5. **对比 LLVM IR**：用一句话说明，为什么这段代码在 MLIR 里能保持「函数体是一个 Region」这种结构化形态，而翻译成 LLVM IR（u3）后就变成了 `Function ⊃ BasicBlock ⊃ Instruction` 的扁平三层、且不再有可任意嵌套的「Region」概念。

> 完成后，建议把你的树状图和字段映射表与同伴或自己回顾一次；若已本地构建 MLIR，可用 `mlir-opt` 对该文本做 round-trip 验证它的合法性。运行验证：**待本地验证**。

## 6. 本讲小结

- **MLIR 是「基础设施」而非某一种 IR**：它提供构造/变换/验证 IR 的通用框架，具体 IR 由可插拔的**方言**定义，因此支持多抽象层级共存与渐进式下降。
- **一切皆 Operation**：`Operation` 是 MLIR 唯一的基本 IR 单元，同时扮演指令、函数、模块、终结符等角色；它由 name、operands、results、attributes/properties、regions、successors、location 组成。
- **Operation 的内存布局很特殊**：结果数组排在对象**之前**（逆序），operands/Region 等排在对象**之后**（尾部分配 `TrailingObjects`），因此必须经 `Operation::create` 工厂堆分配。
- **强类型零开销**：具体 Op 类型（如 `FuncOp`）只是对 `Operation*` 的 CRTP+mixin 视图，对象本身仅一个指针大小。
- **IR 是可嵌套的树**：Operation 包 Region，Region 是 Block 链表，Block 是 Operation 链表，且能任意深度嵌套——这是 MLIR 区别于 LLVM IR「扁平三层」的根本。
- **双向 O(1) 边**：`getBlock()/getParent()/getParentOp()` 上溯，`getRegion()/front()/getOperations()` 下钻；Block 还可带参数（`BlockArgument`），比 LLVM 的 BasicBlock 更强。

## 7. 下一步学习建议

- **本单元下一篇 u7-l2《Dialect 方言机制》**：本讲只把方言当作「点号前缀」提及，u7-l2 会深入讲解如何用 `Dialect.h` 注册一个方言、如何定义自己的 Operation/Type/Attribute，并以 Toy 示例的 Ch2 为范本。
- **u7-l3《Pass、Pattern 与 Conversion》**：当你理解了 Operation 与 Region 结构后，下一步自然是「如何变换它」——MLIR 的 Pass 框架、RewritePattern 图重写、以及方言间的 Lowering/Conversion 都建立在本讲的 Operation 抽象之上。
- **u7-l4《Toy 教程：从语言到 MLIR 到 LLVM IR》**：把本讲概念放到一个端到端例子里，观察 MLIRGen 如何把 AST 翻成 Operation、再一路 lowering 到 LLVM 方言。
- **回头对照 u3**：建议时常把本讲的 Operation/Region/Block 与 u3 的 Module/Function/BasicBlock、Value/Use 做对比阅读——理解「同与不同」是掌握 MLIR 设计意图的捷径。
