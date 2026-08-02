# IR 总体结构导览

## 1. 本讲目标

第 1 单元（u1）我们已经能够「读、写、跑」一段 `.mlir` 文本，但文本只是 IR 在内存里的一种打印形式。本讲是第 2 单元「核心数据结构」的开篇，目标是帮你把脑海里的文本语法，映射到 MLIR 在内存中的真实数据结构，建立一个**全局心智模型**。

学完本讲你应该能够：

- 用一句话说清 `Operation`、`Region`、`Block`、`Value` 四者之间的**包含层次**（谁装在谁里面）。
- 区分两条贯穿 IR 的「边」：一条是**结构嵌套链**（Operation → Region → Block → Operation），另一条是**定义-使用链**（Value 的 def-use）。
- 解释 `Dialect`、`Operation`、`Type`、`Attribute` 之间是什么关系，为什么 MLIR 要把它们分成这四类。
- 对照官方文档里的 IR 结构图，在 `include/mlir/IR/` 头文件里找到每个概念对应的类，并能读懂官方用来遍历 IR 的那个示例 pass。

本讲是「导览」，重在建立全局地图，**不深挖任何一个类的内存布局细节**——那是 u2-l2 ~ u2-l6 各篇要做的事。本讲会让你知道：每张地图上每条路通向哪一篇后续讲义。

## 2. 前置知识

在进入源码前，先确认你已经具备下面这些来自 u1 的认知（本讲会直接使用，不再重复解释）：

- **operation（操作）** 是 MLIR 里最基本的积木，文本语法骨架为 `%results = "dialect.op"(operands) {attrs} : (types) -> (types)`。
- **dialect（方言）** 是操作/类型/属性的命名空间，操作名里的 `.` 前半部分就是方言名（如 `arith.addi`）。
- **IR 有两种落盘形式**：文本（`.mlir`）和字节码，二者在内存中是同一份结构。
- **`mlir-opt`** 是「读 IR → 跑 pass → 写 IR」的试验台。

此外，本讲会用到一个编译原理常识：**SSA（Static Single Assignment，静态单赋值）**。SSA 的核心规则是「每个变量只被赋值一次」。在 MLIR 文本里，`%a`、`%b`、`%0` 这类以 `%` 开头的名字就是 SSA 值的名字；一个 SSA 值要么是某个操作的**结果（result）**，要么是某个基本块的**参数（block argument）**。这正是后面 `Value` 抽象的来源。

> 一个容易混淆的点：MLIR 文本里的 `%0` 这种「值名」**不会持久化到内存**。它只是为了让人读起来方便而临时编的编号；真正在内存里表示「这个值」的是 `Value` 对象。同一个 IR 反复打印，`%` 名字可能不一样，但结构不变。

## 3. 本讲源码地图

本讲只读取「定义 IR 骨架」的几个最关键文件，先把骨架立起来：

| 文件 | 作用 |
| --- | --- |
| `docs/LangRef.md` | 语言参考。其中 `## High-Level Structure` 一节是 IR 总体结构的权威定义。 |
| `docs/Tutorials/UnderstandingTheIRStructure.md` | 官方「理解 IR 结构」教程，用一个打印嵌套的 pass 把整张图走了一遍。本讲大量引用它。 |
| `include/mlir/IR/Operation.h` | 定义 `Operation` 类——MLIR 的「基本执行单元」。 |
| `include/mlir/IR/Region.h` | 定义 `Region` 类——一组基本块的容器。 |
| `include/mlir/IR/Block.h` | 定义 `Block` 类——操作的有序列表。 |
| `include/mlir/IR/Value.h` | 定义 `Value` 类——统一表示 SSA 值（结果或块参数）。 |
| `include/mlir/IR/Visitors.h` | 定义 `walk()` 遍历工具，是「不用手写递归就能走遍 IR」的关键。 |
| `test/lib/IR/TestPrintNesting.cpp` | 上面那个官方教程 pass 的真实源码，可以直接拿 `mlir-opt -test-print-nesting` 跑。 |

> 小提示：`Operation.h` 顶部 `#include` 了 `Block.h`、`Region.h`，说明这几个头文件本身就是一套紧密耦合的「IR 骨架」，最好放在一起理解。

## 4. 核心概念与源码讲解

### 4.1 Operation-Region-Block-Value 的层次结构

#### 4.1.1 概念说明

MLIR 的官方语言参考开篇就给出了 IR 的最顶层定义（[docs/LangRef.md:L30-L38](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/docs/LangRef.md#L30-L38)）：

> MLIR is fundamentally based on a graph-like data structure of nodes, called *Operations*, and edges, called *Values*.

翻译过来：MLIR 本质上是一张**图**——

- **节点（node）** 叫 `Operation`（操作）。
- **边（edge）** 叫 `Value`（值），边把数据从一个操作「流」到另一个操作。

而这张图同时还有一棵**树**来组织结构（[docs/LangRef.md:L33-L38](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/docs/LangRef.md#L33-L38)）：操作装在块里、块装在区域里、而操作又可以再包含区域，于是形成了层次嵌套：

\[ \text{Operation} \rightarrow \text{Region} \rightarrow \text{Block} \rightarrow \text{Operation} \rightarrow \cdots \]

也就是说，「图」描述数据怎么流，「树」描述代码怎么嵌套。理解 MLIR 的 IR，就是把这两套结构叠在一起看：

- `Operation`：基本执行单元，也是 IR 里**一切遍历的根**。一个 pass 的入口 `getOperation()` 拿到的通常是一个顶层 `Operation`（最常见的是 `builtin.module`）。
- `Region`：操作里的一段「作用域容器」，本身**不存别的东西，只存一串 Block**。
- `Block`：操作的有序列表，同时携带若干个**块参数（BlockArgument）**和一个**终止符（terminator）**。
- `Value`：SSA 值。要么是某个 `Operation` 的结果（`OpResult`），要么是某个 `Block` 的参数（`BlockArgument`）。

一句话记忆：**Operation 是「盒子」，Region 是「隔层」，Block 是「抽屉」，Value 是抽屉之间传递的「卡片」。**

#### 4.1.2 核心流程

官方教程用一个三方法互递归来遍历这棵树（[docs/Tutorials/UnderstandingTheIRStructure.md:L25-L28](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/docs/Tutorials/UnderstandingTheIRStructure.md#L25-L28)），用伪代码描述就是：

```
printOperation(op):
    打印 op 的名字、操作数、结果、属性
    对 op 的每个 region:
        printRegion(region)

printRegion(region):
    对 region 的每个 block:   # region 里只有一串 block
        printBlock(block)

printBlock(block):
    打印 block 的参数、后继、操作数
    对 block 的每个 operation:   # block 里只有一串 operation
        printOperation(op)      # 回到起点 → 形成递归
```

关键点：递归的「齿轮」是 `Operation ↔ Region ↔ Block`。`printRegion` 只做一件事——遍历 block；`printBlock` 只做一件事——遍历 operation。这样三步首尾相接，就能从一个顶层 operation 走到任意深度的嵌套结构。

> 注意 `region.getBlocks()` 拿到的是一个 `iplist<Block>`（侵入式链表），而 `block.getOperations()` 同样是一个操作链表。所以「Region 装一串 Block」「Block 装一串 Operation」在数据结构层面就是两个链表。

#### 4.1.3 源码精读

**① Operation 是基本执行单元，且持有 Region**

[include/mlir/IR/Operation.h:L30](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/include/mlir/IR/Operation.h#L30) 的注释一句话定位了它：

```cpp
/// Operation is the basic unit of execution within MLIR.
```

紧接着 [include/mlir/IR/Operation.h:L68-L70](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/include/mlir/IR/Operation.h#L68-L70) 说明了 Region/Block/Operation 如何形成树：

```cpp
/// An Operation may optionally contain one or multiple Regions, stored in a
/// tail allocated array. Each `Region` is a list of Blocks. Each `Block` is
/// itself a list of Operations. This structure is effectively forming a tree.
```

「tail allocated array」（尾随分配数组）是一种把变长子对象紧贴着主对象一次性 `malloc` 出来的技巧，目的是减少内存分配次数、提高缓存局部性。你只需记住：一个 operation 的多个 region 在内存里是紧挨着它存放的数组。这个内存布局细节会在 u2-l2 详讲。

**② Operation 暴露的「部件」访问器**

同一个头文件里，`Operation` 类把上面那些部件都暴露成了访问器（[include/mlir/IR/Operation.h:L83-L87](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/include/mlir/IR/Operation.h#L83-L87) 是类声明）：

```cpp
class alignas(8) Operation final
    : public llvm::ilist_node_with_parent<Operation, Block>,
      private llvm::TrailingObjects<Operation, detail::OperandStorage,
                                    detail::OpProperties, BlockOperand, Region,
                                    OpOperand> {
```

`TrailingObjects<Operation, ..., Region, OpOperand>` 这一行就告诉我们：operation 的尾随对象里包含 `Region`（区域数组）和 `OpOperand`（操作数）。对应的访问方法：

- 操作数个数与获取：[include/mlir/IR/Operation.h:L371-L373](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/include/mlir/IR/Operation.h#L371-L373)、[include/mlir/IR/Operation.h:L403-L406](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/include/mlir/IR/Operation.h#L403-L406)
- 结果个数与获取：[include/mlir/IR/Operation.h:L429-L432](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/include/mlir/IR/Operation.h#L429-L432)、[include/mlir/IR/Operation.h:L440-L443](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/include/mlir/IR/Operation.h#L440-L443)
- 区域个数与获取：[include/mlir/IR/Operation.h:L698-L708](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/include/mlir/IR/Operation.h#L698-L708)

```cpp
unsigned getNumOperands() { ... }
operand_range getOperands() { ... }
unsigned getNumResults() { return numResults; }
result_range getResults() { ... }
MutableArrayRef<Region> getRegions() {
  if (numRegions == 0) return MutableArrayRef<Region>();
  return getTrailingObjects<Region>(numRegions);
}
```

`getRegions()` 直接返回尾随分配的那个 `Region` 数组——这就是「Operation 持有若干 Region」在 C++ 层面的落点。

**③ Region 只装一串 Block**

[include/mlir/IR/Region.h:L24-L26](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/include/mlir/IR/Region.h#L24-L26) 给出定义：

```cpp
/// This class contains a list of basic blocks and a link to the parent
/// operation it is attached to.
class Region {
```

它的核心成员就是 `getBlocks()`（[include/mlir/IR/Region.h:L44-L45](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/include/mlir/IR/Region.h#L44-L45)），返回一个 `iplist<Block>`；另外有 `getParentOp()`（[include/mlir/IR/Region.h:L213](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/include/mlir/IR/Region.h#L213)）指回挂在哪个 operation 上。所以 Region 的全部职责就是「保管一串 block + 知道自己挂在哪个 op 上」。

**④ Block 装一串 Operation（外加参数）**

[include/mlir/IR/Block.h:L31-L33](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/include/mlir/IR/Block.h#L31-L33) 一句话定位：

```cpp
/// `Block` represents an ordered list of `Operation`s.
class alignas(8) Block : public IRObjectWithUseList<BlockOperand>,
                         public llvm::ilist_node_with_parent<Block, Region> {
```

注意 `ilist_node_with_parent<Block, Region>`：每个 block「知道自己属于哪个 region」。它对外提供：参数列表 `getArguments()`（[include/mlir/IR/Block.h:L111](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/include/mlir/IR/Block.h#L111)）、操作列表 `getOperations()`（[include/mlir/IR/Block.h:L161](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/include/mlir/IR/Block.h#L161)）、终止符 `getTerminator()`（[include/mlir/IR/Block.h:L248](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/include/mlir/IR/Block.h#L248)）和后继个数 `getNumSuccessors()`（[include/mlir/IR/Block.h:L287](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/include/mlir/IR/Block.h#L287)）。

> 「后继（successor）」就是分支类操作（如 `cf.cond_br`）跳转到的下一个 block。MLIR 用 **block argument** 代替了 LLVM IR 里的 PHI 节点——值在「进入 block 的入口」处通过参数传入，而不是在每个前驱处分别列一遍。

**⑤ Value 是「边」**

[include/mlir/IR/Value.h:L87-L90](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/include/mlir/IR/Value.h#L87-L90) 给出 SSA 值的统一定义：

```cpp
/// This class represents an instance of an SSA value in the MLIR system,
/// representing a computable value that has a type and a set of users. An SSA
/// value is either a BlockArgument or the result of an operation.
```

底层的 `ValueImpl` 用一个 `Kind` 枚举区分这两种来源（[include/mlir/IR/Value.h:L45-L60](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/include/mlir/IR/Value.h#L45-L60)）：

```cpp
enum class Kind {
  InlineOpResult = 0,    // 操作结果（前几个，内联存储）
  OutOfLineOpResult = 6, // 操作结果（超出内联范围）
  BlockArgument = 7      // 块参数
};
```

> 这里的 `InlineOpResult` / `OutOfLineOpResult` 是一种内存优化：前几个结果把「自己是第几个结果」压缩进指针里，省掉一个额外字段。u2-l3 会专门讲。

#### 4.1.4 代码实践

**实践目标**：用一个现成的 pass，把一段多层嵌套的 IR「摊平」打印出来，亲眼看到 Operation-Region-Block 的层次。

**操作步骤**：

1. 找到官方教程 pass 的真实源码：[test/lib/IR/TestPrintNesting.cpp:L16-L74](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/test/lib/IR/TestPrintNesting.cpp#L16-L74)。它的 `runOnOperation` 入口在 [test/lib/IR/TestPrintNesting.cpp:L23-L27](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/test/lib/IR/TestPrintNesting.cpp#L23-L27)。

2. 在本地构建好的 MLIR 里运行（该 pass 属于 `MLIRTestIR` 等测试库，需要构建时打开测试，参考 u1-l3 的构建说明）：

   ```bash
   mlir-opt -test-print-nesting \
            -allow-unregistered-dialect \
            test/IR/print-ir-nesting.mlir
   ```

   其中测试输入文件可在仓库里找到：`mlir/test/IR/print-ir-nesting.mlir`（内容与 [docs/Tutorials/UnderstandingTheIRStructure.md:L98-L114](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/docs/Tutorials/UnderstandingTheIRStructure.md#L98-L114) 给出的示例一致）。

3. 如果本地没有可用二进制，则改为**源码阅读型实践**：对照 [docs/Tutorials/UnderstandingTheIRStructure.md:L118-L151](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/docs/Tutorials/UnderstandingTheIRStructure.md#L118-L151) 给出的示例输出，逐行核对：每个 `visiting op` 后面的 `N nested regions`、`Region with M blocks`、`Block with X arguments ... and Y operations`，分别对应 pass 里 `printOperation` / `printRegion` / `printBlock` 的哪一次调用。

**需要观察的现象**：

- 输出的缩进会随嵌套深度增加——这正是「Operation→Region→Block→Operation」递归下降的体现。
- `dialect.op2` 下面有 **2 个 region**：第一个含 1 个 block，第二个含 3 个 block（其中一个有 2 个 successors，说明它是分支目标）。

**预期结果**：你能对着输出，画出一张「module → op2 → region2 → bb1/bb2/bb3」的树状缩进图，并指出每个叶子节点是一个没有子 region 的 operation。

> 如果本地无法运行，明确标注「待本地验证」输出，但树状结构可以靠阅读源码和文档示例静态推导出来。

#### 4.1.5 小练习与答案

**练习 1**：为什么说 MLIR 的 IR「既是图又是树」？分别对应哪两个概念？

> **参考答案**：「图」对应数据流——节点是 `Operation`、边是 `Value`；「树」对应结构嵌套——`Operation` 含 `Region`、`Region` 含 `Block`、`Block` 含 `Operation`，递归形成层次。

**练习 2**：`Region` 类自己存储了哪些东西？它为什么「很轻」？

> **参考答案**：`Region` 只存一串 `Block`（`getBlocks()` 返回 `iplist<Block>`）和一个指向父 operation 的指针（`getParentOp()`）。它本身不存 operation，所以「很轻」——真正的操作在它下属的 block 里。

**练习 3**：一个 `Value` 在内存里有哪两种「身份」？由什么决定？

> **参考答案**：要么是 `OpResult`（某个操作的结果），要么是 `BlockArgument`（某个块的参数）。由底层 `ValueImpl` 的 `Kind` 枚举（`InlineOpResult` / `OutOfLineOpResult` / `BlockArgument`）区分。

---

### 4.2 Dialect-Operation-Type-Attribute 关系

#### 4.2.1 概念说明

如果说上一节讲的是 IR 的「骨架」（Operation/Region/Block/Value），那么这一节讲的是「血肉」——一个 operation 到底由哪些信息「拼」出来的。官方把它们分成四大类（[docs/LangRef.md:L243](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/docs/LangRef.md#L243) 的 Dialects 一节、[docs/LangRef.md:L286](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/docs/LangRef.md#L286) 的 Operations 一节）：

- **Dialect（方言）**：命名空间。它把一组 operation / type / attribute 归到一个名字下，比如 `arith`、`func`、`memref`、`affine`。操作名 `arith.addi` 里 `.` 前的 `arith` 就是方言名。
- **Operation（操作）**：具体的一类操作（如 `arith.addi`）。注意区分大小写：`Operation`（C++ 类）是**运行时实例**，而 `arith.addi` 是**操作名（OperationName）**。同一个操作名可以实例化出成千上万个 `Operation*` 对象。
- **Type（类型）**：值的类型。MLIR 的类型系统是**开放**的——除了内置的 `i32`、`index`、`tensor<...>`、`memref<...>`（builtin types），任何方言都能定义自己的类型。
- **Attribute（属性）**：编译期常量，附加在操作上。例如 `arith.constant` 上的值 `42 : i32` 就是一个属性。属性也分两类：**固有属性（inherent）**——操作的语义必须的（如 `cmpi` 的谓词）；**可丢弃属性（discardable）**——带方言前缀的附加信息。

四者的关系可以用一句话串起来：

\[ \text{Dialect} \;\supseteq\; \{\text{Operation},\ \text{Type},\ \text{Attribute}\} \]

一个方言就是「操作 + 类型 + 属性」的一个集合，加上一个名字。这就是 MLIR 可扩展性的根基——**新领域 = 新方言**。

> 关于 Traits / Interfaces：[docs/LangRef.md:L47-L56](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/docs/LangRef.md#L47-L56) 指出，为了让通用变换（pass）能在「任意方言的任意操作」上工作，MLIR 允许用 **Traits**（特质）和 **Interfaces**（接口）抽象地描述操作语义。这是把「无限多种操作」收敛成「有限几种行为」的关键，本讲只点到为止，u8 会专门讲接口系统。

#### 4.2.2 核心流程

从一个操作名到一个内存中的 `Operation`，大致经过：

```
字符串 "arith.addi"
   │  由 '.' 拆出 dialect 名和 op 名
   ▼
OperationName  ── 持有 ──►  指向 Dialect 的引用（可空，若方言未加载）
   │
   ▼  构造 Operation 时填入
Operation {
    name: OperationName           // 我是谁
    operands: [OpOperand]          // 我吃哪些 Value（边）
    results:  [OpResult]           // 我产出哪些 Value（边）
    regions:  [Region]             // 我内嵌哪些层次
    attrs:    DictionaryAttr       // 我身上挂了哪些编译期常量
    location: Location             // 我源自源码哪里（可调试性）
    block:    Block*               // 我被装在哪个抽屉里
}
```

注意几点：

- **类型（Type）不直接挂在 Operation 上**，而是挂在 `Value` 上——每个 operand/result 都带类型。所以「一个操作的类型签名」其实是它所有 operand/result 的类型组合，对应文本语法里那个不能省的 `: (operand-types) -> (result-types)`。
- **属性（Attribute）挂在 Operation 上**，但只有「非固有」的部分存在 `attrs` 字典里；固有属性可以存为更高效的「properties」（见 u1-l4 提到的 `<{...}>`）。
- **操作名决定方言**：通过 `Operation::getDialect()`（[include/mlir/IR/Operation.h:L237](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/include/mlir/IR/Operation.h#L237)）可以拿到所属方言。

#### 4.2.3 源码精读

**① 操作名 = 方言名 + 操作名**

[include/mlir/IR/Operation.h:L36-L39](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/include/mlir/IR/Operation.h#L36-L39) 解释了操作名的解析规则：

```cpp
/// An Operation is defined first by its name, which is a unique string. The
/// name is interpreted so that if it contains a '.' character, the part before
/// is the dialect name this operation belongs to, and everything that follows
/// is this operation name within the dialect.
```

这正是 `arith.addi` → 方言 `arith` + 操作 `addi` 的来源。访问器在 [include/mlir/IR/Operation.h:L115](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/include/mlir/IR/Operation.h#L115)：

```cpp
OperationName getName() { return name; }
```

**② Operation 的「核心字段」**

把 `Operation` 类底部的私有成员集中看（[include/mlir/IR/Operation.h:L1069-L1101](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/include/mlir/IR/Operation.h#L1069-L1101)），就是上面流程图里的那些部件：

```cpp
Block *block = nullptr;       // 装在哪个 block 里
Location location;            // 源码位置
const unsigned numResults;    // 结果个数
const unsigned numSuccs;      // 后继个数
const unsigned numRegions : 23;
bool hasOperandStorage : 1;
unsigned char propertiesStorageSize : 8;  // 固有属性(properties)大小
OperationName name;           // 操作名 → 方言
DictionaryAttr attrs;         // 可丢弃属性字典
```

可以看到：类型（Type）确实**不在这里**——它跟着每个 result/operand 走；属性被拆成了 `attrs`（可丢弃）和 `propertiesStorage`（固有）。

**③ 属性访问的双轨制**

[include/mlir/IR/Operation.h:L478-L482](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/include/mlir/IR/Operation.h#L478-L482) 给出「可丢弃属性」的入口：

```cpp
/// Access a discardable attribute by name, returns a null Attribute if the
/// discardable attribute does not exist.
Attribute getDiscardableAttr(StringRef name) { return attrs.get(name); }
```

而 `getAttr()`（[include/mlir/IR/Operation.h:L559-L572](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/include/mlir/IR/Operation.h#L559-L572)）会先看 properties 里有没有固有属性，再退回到 `attrs` 字典——这就是「双轨制」的体现：统一的访问入口，背后区分了两种存储。

> 完整的属性 / properties 体系会在 u2-l5（Type 与 Attribute 体系）和 u3 单元（用 TableGen 定义）里展开。本节只需记住：**属性 = 操作上的编译期常量，分固有 / 可丢弃两种存储**。

**④ Type 挂在 Value 上**

回头再看 [include/mlir/IR/Value.h:L62-L63](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/include/mlir/IR/Value.h#L62-L63)，类型是 Value 自己的属性：

```cpp
/// Return the type of this value.
Type getType() const { return typeAndKind.getPointer(); }
```

而 `Operation` 暴露的 `getResultTypes()` / `getOperandTypes()`（[include/mlir/IR/Operation.h:L422](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/include/mlir/IR/Operation.h#L422)、[include/mlir/IR/Operation.h:L453](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/include/mlir/IR/Operation.h#L453)）只是「把所有 result/operand 的类型汇总成一个 range」，本质还是从每个 Value 上取。

#### 4.2.4 代码实践

**实践目标**：在一段真实 IR 上，把一个 operation 的「四要素」（操作名、操作数、结果、属性）和「归属（方言、类型、位置）」一一指认出来。

**操作步骤**（源码阅读型 + 命令行验证）：

1. 看下面这段来自官方教程的精简 IR（节选自 [docs/Tutorials/UnderstandingTheIRStructure.md:L98-L113](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/docs/Tutorials/UnderstandingTheIRStructure.md#L98-L113)）：

   ```mlir
   %results:4 = "dialect.op1"() {"attribute name" = 42 : i32} : () -> (i1, i16, i32, i64)
   ```

2. 对照 [test/lib/IR/TestPrintNesting.cpp:L32-L50](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/test/lib/IR/TestPrintNesting.cpp#L32-L50) 的 `printOperation`，为这个 operation 填表：

   | 要素 | 在 IR 里的值 | 对应的 C++ 访问器 |
   | --- | --- | --- |
   | 操作名 | `dialect.op1` | `op->getName()` |
   | 操作数个数 | 0 | `op->getNumOperands()` |
   | 结果个数 | 4 | `op->getNumResults()` |
   | 结果类型 | `i1, i16, i32, i64` | `op->getResultTypes()` |
   | 属性 | `attribute name = 42 : i32` | `op->getAttrs()` |
   | 所属方言 | `dialect` | `op->getDialect()` |
   | 子区域数 | 0 | `op->getNumRegions()` |

3. 若本地有 `mlir-opt`，运行 `-test-print-nesting`，确认 pass 打印的 `0 operands`、`4 results`、`1 attributes` 与你填的表一致。

**需要观察的现象**：操作名里的 `.` 把「方言」和「操作」一分为二；属性 `attribute name = 42 : i32` 里的 `42 : i32` 本身是一个带类型的属性值。

**预期结果**：你能复述「一个 operation = 操作名（含方言）+ 操作数（Value）+ 结果（Value，带 Type）+ 属性 + 区域 + 位置」，并指出每个部件的 C++ 访问器。运行结果对照官方教程输出应完全一致；若无法运行，标注「待本地验证」即可。

#### 4.2.5 小练习与答案

**练习 1**：`Operation`（大写 C++ 类）和「操作名」是什么关系？

> **参考答案**：`Operation` 是运行时的**实例对象**（一个 `Operation*`），「操作名」（`OperationName`，如 `arith.addi`）是它的**身份标识**。同一个操作名可以对应无数个 `Operation` 实例；操作名里的 `.` 前半段就是所属方言。

**练习 2**：为什么 `Operation` 类的字段里**找不到** `Type`？

> **参考答案**：类型不挂在操作上，而是挂在每个 `Value`（operand/result）上——`Value::getType()`。操作的「类型签名」是它所有 operand/result 类型的聚合，所以操作只通过 `getResultTypes()` / `getOperandTypes()` 间接提供类型。

**练习 3**：inherent（固有）属性和 discardable（可丢弃）属性在存储上有何不同？

> **参考答案**：可丢弃属性存在 `Operation::attrs`（一个 `DictionaryAttr`）里，名字带方言前缀；固有属性可存为更紧凑的「properties」（由 `propertiesStorageSize` 标记的内联存储），是操作语义的一部分。统一的 `getAttr()` 会先查 properties 再查 `attrs`。

---

### 4.3 IR 结构总览图对照源码（遍历与 def-use）

#### 4.3.1 概念说明

前两节分别讲了「嵌套树」和「操作的四要素」。但真正在脑子里建立「总览图」，还需要补上两件利器：

1. **遍历（walk）**：手写 `printOperation`/`printRegion`/`printBlock` 的三方法递归很啰嗦。MLIR 提供了 `walk()` 工具，一行代码就能「深度优先走遍所有嵌套 operation」。
2. **def-use 链（定义-使用链）**：除了「谁包含谁」的树，还有「Value 被谁用」的图。这是优化的基础——做常量折叠、死代码消除都要顺着 use 链走。

官方教程在 [docs/Tutorials/UnderstandingTheIRStructure.md:L217-L225](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/docs/Tutorials/UnderstandingTheIRStructure.md#L217-L225) 把 def-use 关系讲得很清楚：

> each Value is either a `BlockArgument` or the result of exactly one `Operation`... The users of a `Value` are `Operation`s, through their arguments: each `Operation` argument references a single `Value`.

即：一个 Value 有**唯一一个定义者**（producing op 或 block），但可以有**多个使用者**（user op）。从「定义」找「使用」叫 use 链，从「使用」找「定义」叫 def 链。两者合起来就是 SSA 的核心。

#### 4.3.2 核心流程

**walk 的工作方式**（[include/mlir/IR/Visitors.h:L136-L154](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/include/mlir/IR/Visitors.h#L136-L154)）：

```
walk(op, callback, order):
    if order == PreOrder:  callback(op)        // 先访问父
    for region in op.regions:
        for block in region.blocks:
            for nestedOp in block.operations:
                walk(nestedOp, callback, order)  # 递归
    if order == PostOrder: callback(op)         # 后访问父
```

默认是 **PostOrder（后序）**：先访问所有子，再访问父。这对很多变换很重要——比如删除操作时，必须先把它的内部都处理掉。

回调可以**按操作类型过滤**：传 `[](ReturnOp op){...}` 就只会对 `ReturnOp` 调用，等价于「在全树里找所有 return」。

**def-use 的两条基本方向**：

- 给定一个 `Operation*`，看它的每个 operand 是谁生产的：`operand.getDefiningOp()`（可能是 `nullptr`，说明是 block argument）。
- 给定一个 result Value，看它的所有 user：`result.getUsers()`。

#### 4.3.3 源码精读

**① walk 的对外入口与回调形式**

[include/mlir/IR/Visitors.h:L257-L281](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/include/mlir/IR/Visitors.h#L257-L281) 是用户应优先调用的模板（注释也说了「Users should favor the direct `walk` methods on the IR classes」）：

```cpp
template <WalkOrder Order = WalkOrder::PostOrder, ...>
walk(Operation *op, FuncTy &&callback) { ... }
```

注意默认 `WalkOrder::PostOrder`。`WalkOrder` 枚举定义在 [include/mlir/IR/Visitors.h:L27-L28](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/include/mlir/IR/Visitors.h#L27-L28)：

```cpp
enum class WalkOrder { PreOrder, PostOrder };
```

而 `Operation` 类把它们转发成成员方法 [include/mlir/IR/Operation.h:L817-L824](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/include/mlir/IR/Operation.h#L817-L824)，于是你可以写 `op->walk([&](Operation *o){ ... })`。

回调还可以返回 `WalkResult::interrupt()` 来**提前中断**遍历，官方示例见 [docs/Tutorials/UnderstandingTheIRStructure.md:L201-L215](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/docs/Tutorials/UnderstandingTheIRStructure.md#L201-L215)。

**② 按 op 类型过滤的 walk**

[include/mlir/IR/Visitors.h:L295-L310](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/include/mlir/IR/Visitors.h#L295-L310) 展示了「回调参数是具体派生 op 类型」的重载，内部用一个 wrapper 做了 `dyn_cast`：

```cpp
auto wrapperFn = [&](Operation *op) {
  if (auto derivedOp = dyn_cast<ArgT>(op))
    callback(derivedOp);
};
```

这就是为什么 `op->walk([](LinalgOp linalgOp){ ... })` 能只命中 `LinalgOp`。

**③ def-use 链的遍历**

[docs/Tutorials/UnderstandingTheIRStructure.md:L230-L244](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/docs/Tutorials/UnderstandingTheIRStructure.md#L230-L244) 给出从 operand 反查生产者的写法：

```cpp
for (Value operand : op->getOperands()) {
  if (Operation *producer = operand.getDefiningOp()) {
    // 有定义操作
  } else {
    // 没有定义操作 → 一定是 BlockArgument
    auto blockArg = cast<BlockArgument>(operand);
  }
}
```

而 [docs/Tutorials/UnderstandingTheIRStructure.md:L250-L268](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/docs/Tutorials/UnderstandingTheIRStructure.md#L250-L268) 给出从 result 正查所有 user 的写法：遍历 `op->getResults()`，对每个 result 调 `result.getUsers()`。对应的 C++ 入口在 `Operation` 类里：`getUsers()`（[include/mlir/IR/Operation.h:L898](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/include/mlir/IR/Operation.h#L898)）、`getUses()`（[include/mlir/IR/Operation.h:L871](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/include/mlir/IR/Operation.h#L871)）。

> use 和 user 的区别：一个 **use** 是「一次具体引用」（`OpOperand`，包含「哪个操作的哪个操作数位置」），一个 **user** 是「使用它的操作」。多个 use 可能来自同一个 user（一个操作在两个操作数位置用了同一个 Value）。

**④ use 链的双向链表与 RAUW**

[docs/Tutorials/UnderstandingTheIRStructure.md:L278-L282](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/docs/Tutorials/UnderstandingTheIRStructure.md#L278-L282) 提到：所有 use 被串成**双向链表**，这让「把一个 Value 的所有使用替换成另一个 Value」（Replace All Uses With，**RAUW**）变得高效。`Operation` 上的 `replaceAllUsesWith` 在 [include/mlir/IR/Operation.h:L296-L299](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/include/mlir/IR/Operation.h#L296-L299)：

```cpp
template <typename ValuesT>
void replaceAllUsesWith(ValuesT &&values) {
  getResults().replaceAllUsesWith(std::forward<ValuesT>(values));
}
```

这是后续讲义里模式重写（u6）、CSE（u5-l3）反复用到的基础原语。

#### 4.3.4 代码实践

**实践目标**：用 `walk()` 写一段最简遍历，并沿着 def-use 链走一遭，把「树」和「图」两条路径都跑通。

**操作步骤**（源码阅读型 / 可选编译验证）：

1. **遍历侧**：下面是一段示例代码（**非项目原有代码，仅作演示**），对照 [include/mlir/IR/Visitors.h:L273-L281](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/include/mlir/IR/Visitors.h#L273-L281) 阅读它的回调形式：

   ```cpp
   // 示例代码：统计一个 module 里所有 operation 的数量
   moduleOp->walk([](Operation *op) {
     llvm::outs() << "op: " << op->getName() << "\n";
   });
   ```

   预期：每个嵌套层级的 operation 名字都会被打印一次，顺序是后序（子先于父）。

2. **def-use 侧**：再写一段示例（**非项目原有代码**），给定一个 `Operation*`，沿着它的 operand 找到生产者：

   ```cpp
   // 示例代码：打印每个 operand 的来源
   for (Value operand : op->getOperands()) {
     if (Operation *producer = operand.getDefiningOp())
       llvm::outs() << "来自操作: " << producer->getName() << "\n";
     else
       llvm::outs() << "来自块参数\n";
   }
   ```

3. **观察现象**：把这两段逻辑和你阅读 [docs/Tutorials/UnderstandingTheIRStructure.md:L230-L268](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/docs/Tutorials/UnderstandingTheIRStructure.md#L230-L268) 的理解对照——前者走「树」，后者走「图」。

**预期结果**：你能用一句话区分 `walk()`（结构遍历）与 `getDefiningOp()/getUsers()`（def-use 遍历）解决的是两类不同的问题。

> 若想真正运行，需要把这些片段编进一个 pass（参考 [test/lib/IR/TestPrintNesting.cpp:L16-L27](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/test/lib/IR/TestPrintNesting.cpp#L16-L27) 的 pass 骨架），用 `mlir-opt` 加载。本地未构建则标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`walk()` 默认是前序还是后序？为什么删除操作时后序更安全？

> **参考答案**：默认后序（`WalkOrder::PostOrder`）。后序先访问子操作再访问父操作，这样在回调里删除某个操作时，它内部的嵌套已经被处理过，不会留下悬空引用——[include/mlir/IR/Visitors.h:L96-L100](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/include/mlir/IR/Visitors.h#L96-L100) 的注释明确指出「A callback ... is allowed to erase that block or operation only if the walk is in post-order」。

**练习 2**：`use` 和 `user` 有什么区别？一个 Value 有可能 use 比 user 多吗？

> **参考答案**：use 是一次具体的引用（一个 `OpOperand`，带位置），user 是引用它的操作。可能：同一个操作在多个操作数位置用了同一个 Value，此时 user 只有 1 个但 use 有多个。

**练习 3**：`replaceAllUsesWith`（RAUW）为什么能做到高效？

> **参考答案**：因为所有 use 被组织成围绕 Value 的双向链表（基于 `IRObjectWithUseList`），替换时只需遍历这条链表把每个 `OpOperand` 改指向新 Value，无需扫描整张 IR。

---

## 5. 综合实践

把本讲三节串起来，完成下面这个「画图 + 标注」任务：

**任务**：阅读 [docs/Tutorials/UnderstandingTheIRStructure.md:L98-L151](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/docs/Tutorials/UnderstandingTheIRStructure.md#L98-L151) 的示例 IR 与对应输出，画两张图：

1. **结构嵌套图（树）**：以 `builtin.module` 为根，画出 `dialect.op1`、`dialect.op2`，再画出 `op2` 下的两个 region、第二个 region 下的三个 block（`^bb0/^bb1/^bb2`），以及每个 block 里的 operation。在每个节点旁边标注它对应的头文件类名：
   - operation → `Operation.h`
   - region → `Region.h`
   - block → `Block.h`
2. **数据流图（图的边）**：挑出 `%results:4`（`op1` 的 4 个结果），画出它如何被 `dialect.innerop1`（用了 `#0`、`#1`）和 `dialect.innerop3`（用了 `#0`、`#2`、`#3`）引用，标注每个引用是一个 `Value`，`%results` 本身是 `OpResult`（来源 `Value.h`）。

**验收标准**：

- 你的树图里，每个 `Operation` 节点都恰好属于某个 `Block`，每个 `Block` 都属于某个 `Region`，每个 `Region` 都挂在某个 `Operation` 上——形成闭环。
- 你的边图里，`%results` 有**唯一一个定义者**（`op1`）和**多个使用者**（`innerop1`、`innerop3`）。
- 你能在图上指出：哪些操作有 `successors`（如 `innerop3` 带 `[^bb1, ^bb2]`），对应 `Block::getNumSuccessors()`。

完成后，你拥有的这张「树 + 图」就是后续 u2-l2 ~ u2-l6 所有讲义共同的「底图」——接下来每一篇，都是在图上的某个节点里钻进去看内存细节。

## 6. 本讲小结

- MLIR 的 IR **既是图又是树**：图由 `Operation`（节点）和 `Value`（边）组成；树由 `Operation → Region → Block → Operation` 递归嵌套形成。
- `Operation` 是基本执行单元，持有操作数（operand）、结果（result）、区域（region）、属性（attribute）、位置（location），并知道自己挂在哪个 `Block` 里。
- `Region`「很轻」，只装一串 `Block` + 一个父操作指针；`Block` 装一串 `Operation` + 块参数 + 终止符/后继。
- `Value` 是统一的 SSA 值抽象，只有两种身份：`OpResult`（操作结果）或 `BlockArgument`（块参数）。
- 一个 `Dialect` 是「操作 + 类型 + 属性」的命名空间；操作名里的 `.` 把方言名和操作名拆开；**类型挂在 Value 上、属性挂在 Operation 上**。
- 遍历 IR 用 `walk()`（默认后序，可按 op 类型过滤、可中断）；沿数据流走用 `getDefiningOp()` / `getUsers()`，use 链是双向链表，使 RAUW 高效。

## 7. 下一步学习建议

本讲建立了「全局地图」，接下来第 2 单元的后续讲义会带你**逐个钻进**图里的节点：

- 想搞清 `Operation` 在内存里到底怎么布局、尾随对象（trailing objects）怎么用 → 读 **u2-l2（Operation 与操作支持结构）**。
- 想搞清 `Value` 的两种子类、use-def 链细节 → 读 **u2-l3（Value：OpResult 与 BlockArgument）**。
- 想搞清 `Block` 的参数化、`Region` 的语义分类、后继关系 → 读 **u2-l4（Block 与 Region）**。
- 想搞清 Type/Attribute 的存储与唯一化 → 读 **u2-l5（Type 与 Attribute 体系）**。
- 想知道这一切由谁「管着」、怎么用 Builder 方便地造 IR → 读 **u2-l6（MLIRContext、Builder 与 Location）**。

建议在进入 u2-l2 之前，先把本讲「综合实践」的图画完——它会是你后续阅读源码时反复回看的参照系。
