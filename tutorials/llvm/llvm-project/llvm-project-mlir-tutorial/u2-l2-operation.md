# Operation 与操作支持结构

## 1. 本讲目标

u2-l1 已经建立了全局地图：MLIR 的 IR「既是图又是树」——图由 `Operation`（节点）和 `Value`（边）表达数据流，树由 `Operation → Region → Block → Operation` 表达嵌套。但当时我们刻意**不深挖任何一个类的内存布局**。本讲就钻进这棵树里最重要的那个节点——`Operation`，把它在内存里到底长什么样彻底讲清。

学完本讲你应该能够：

- 画出 `Operation` 对象在内存中的**完整字节布局**：哪些东西在它「前面」（低地址），哪些在它「后面」（高地址），为什么这样排。
- 说清一个 `Operation` 的 **result / operand / region / successor** 各自是怎么存的：result 用「前缀 + 反序」，operand 用「尾随数组 + 可外迁的动态扩容」，region 和 successor 用「尾随数组」。
- 读懂 `Operation::create` 这一组工厂函数，能列出**创建一个 Operation 必须提供的要素**，并复述它一次性 `malloc` 出整块内存、再用 placement-new 逐个填充子对象的流程。
- 区分 `Operation`（核心类）与 `OperationSupport`（辅助设施）：`OperationState` 是「建操作的物料清单」，`OperandStorage` 是「操作数仓库」，`OpOperand`/`BlockOperand` 是挂在 use-def 双向链表上的使用记录。

本讲是第 2 单元的「承重墙」：后续 u2-l3（Value）会接着这里讲的 result 存储继续往下，u2-l4（Block/Region）会接着 region 的存储继续往下。本讲只讲**「怎么存」**，把「值/块的语义」留给那两篇。

## 2. 前置知识

本讲默认你已经掌握 u2-l1 建立的认知，这里只补充两个本讲要用到的工程常识：

- **SSA 值（Value）**：MLIR 里每个 `%name` 在内存中是一个 `Value` 对象。一个 `Value` 要么是某个 `Operation` 的**结果（OpResult）**，要么是某个 `Block` 的**参数（BlockArgument）**。本讲大量出现「result」，指的就是前者。
- **C++ 对象的内存布局**：一个 `struct/class` 的实例在内存中是一段连续字节，成员变量按声明顺序（受对齐影响）排列。普通 `new` 会把对象放在堆上；而 **placement-new**（`new (ptr) T(...)`）则是在**你已经分配好**的内存地址 `ptr` 上构造对象，不再额外分配。MLIR 的 `Operation` 大量使用 placement-new，这是理解本讲的关键。

> 一个关键直觉：**`Operation` 不能用普通 `new` 创建，也不能用普通 `delete` 销毁。** 它是一段「定制的、变长的、一次性 `malloc` 出来的」内存，前后都挂着额外子对象。本讲几乎所有设计都围绕这个事实展开。

还要预告一个反复出现的术语——**trailing objects（尾随对象）**：这是 LLVM 里的一个惯用法（`llvm::TrailingObjects` 模板），用来把「若干个变长子数组」紧贴在某个主对象**后面**一次性分配，省去多次 `malloc`、提升缓存局部性。下面会反复见到它。

## 3. 本讲源码地图

本讲围绕 `Operation` 的「定义—实现—辅助」三层文件展开：

| 文件 | 作用 |
| --- | --- |
| [include/mlir/IR/Operation.h](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/include/mlir/IR/Operation.h) | `Operation` 类的声明。开头那段长注释是全篇最权威的内存布局说明，类的私有成员则揭示了它持有的所有字段。 |
| [lib/IR/Operation.cpp](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/lib/IR/Operation.cpp) | `Operation` 的实现。`create`/构造/析构/`destroy`/`clone`/顺序维护都在这里，是本讲精读的重点。 |
| [lib/IR/OperationSupport.cpp](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/lib/IR/OperationSupport.cpp) | 辅助类型的实现，最关键的是 `OperandStorage`（操作数仓库）的构造与扩容逻辑。 |
| [include/mlir/IR/OperationSupport.h](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/include/mlir/IR/OperationSupport.h) | `OperationState`（建操作的「物料清单」）与 `OperandStorage` 的声明。 |
| [include/mlir/IR/Value.h](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/include/mlir/IR/Value.h) | `OpResultImpl`/`InlineOpResult`/`OutOfLineOpResult`（result 的存储实现）与 `OpOperand` 的声明，以及 `Kind` 枚举。 |
| [lib/IR/Value.cpp](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/lib/IR/Value.cpp) | `OpResultImpl::getOwner` 的指针算术，是理解「result 反序存储」的最佳入口。 |
| [include/mlir/IR/BlockSupport.h](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/include/mlir/IR/BlockSupport.h) | `BlockOperand`（后继块引用）的声明。 |
| [include/mlir/IR/UseDefLists.h](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/include/mlir/IR/UseDefLists.h) | `IROperand` 基类，揭示了 operand 如何挂进 Value 的 use-def 双向链表。 |

> 提示：result/operand 的「存储」放本讲，而 Value 的「语义抽象」、use-def 链的遍历方式会在 u2-l3 详讲；Block/Region 的「语义」会在 u2-l4 详讲。本讲只在「Operation 怎么持有它们」这个层面触碰这些概念。

## 4. 核心概念与源码讲解

### 4.1 Operation 的内存表示：trailing objects 与「前缀 results」

#### 4.1.1 概念说明

`Operation` 是 MLIR 里「基本的执行单元」（the basic unit of execution）。但和普通 C++ 对象不同，它在内存里不是「孤零零一个 struct」，而是**一整段定制布局的连续内存**。这段内存由三部分组成：

1. **前缀区（prefix，低地址）**：存放这个操作的所有 **result**（结果值），且是**反序**排的。
2. **主对象（`Operation` 本体）**：存放固定字段（名字、位置、各类计数、属性字典等）。我们拿到的 `Operation*` 就指向这里。
3. **尾随区（trailing objects，高地址）**：紧贴在本体后面，依次存放 `OperandStorage`（操作数仓库）、`OpProperties`（属性化属性 properties）、`BlockOperand[]`（后继块引用）、`Region[]`（区域）、`OpOperand[]`（操作数）。

为什么要这么折腾？因为 `Operation` 是 IR 里**数量最多**的对象（一段普通函数可能有成千上万个），对它做内存优化收益极大：

- **一次性 `malloc`**：把变长的 results、operands、regions、successors 全部和本体一起分配，避免「一个操作 N 次小 `malloc`」，既省内存（少分配器开销）又提升缓存局部性。
- **result 反序前置**：让 result 0 离本体最近，从而**从任意一个 result 用指针算术就能 O(1) 找回它的 owner operation**（见 4.2.1）。这是 use-def 链高效运作的基础。

代价是：`Operation` **必须堆分配**，构造走工厂函数 `create`，销毁走专门的 `destroy`——普通 `new`/`delete` 完全用不上。

#### 4.1.2 核心流程

把一段 `Operation` 内存从低地址到高地址画出来就是：

```
┌─────────────────────────── 前缀区（results，反序）──────────────────────────┐
│ OutOfLineOpResult[k] .. OutOfLineOpResult[0] │ InlineOpResult[5] .. InlineOpResult[0] │
└──────────────────────────────────────────────┴──────────────────────────────┘
                                                  ↑ 若结果数 ≤ 6，只有 Inline 部分
┌─── 本体：Operation ───┐
│ block / location / name / attrs / numResults / numSuccs / numRegions ... │
└────────────────────────┘
┌─── 尾随区（trailing objects，正序）──────────────────────────────────────┐
│ OperandStorage(1 个) │ OpProperties(变长) │ BlockOperand[numSuccs] │ Region[numRegions] │ OpOperand[numOperands] │
└──────────────────────────────────────────────────────────────────────────┘
```

记忆口诀：**「结果在前、本体居中、操作数与区域在后」**，且**「前缀反序、尾随正序」**。

构造时 `create` 会：先按上面三段算出总字节数 → `malloc` 一次 → 把本体用 placement-new 放到「前缀区之后」的地址 → 再用 placement-new 逐个填前缀的 results 和尾随的各类子对象。这套流程在 4.4 精读。

#### 4.1.3 源码精读

`Operation` 类的声明本身就揭示了它的布局。本体同时继承自 `llvm::ilist_node_with_parent`（让它能挂进 Block 的链表，见 4.4）和 `llvm::TrailingObjects`（声明尾随区的五种子对象）（[include/mlir/IR/Operation.h:L83-L87](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/include/mlir/IR/Operation.h#L83-L87)）：

```cpp
class alignas(8) Operation final
    : public llvm::ilist_node_with_parent<Operation, Block>,
      private llvm::TrailingObjects<Operation, detail::OperandStorage,
                                    detail::OpProperties, BlockOperand, Region,
                                    OpOperand> {
```

`TrailingObjects<Operation, OperandStorage, OpProperties, BlockOperand, Region, OpOperand>` 这个模板参数列表，**顺序就是尾随区在内存里的排列顺序**：先 `OperandStorage`，再 `OpProperties`，再 `BlockOperand[]`，再 `Region[]`，最后 `OpOperand[]`。每种各自有多少个，由下面的 `numTrailingObjects` 重载告诉框架（[include/mlir/IR/Operation.h:L1119-L1128](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/include/mlir/IR/Operation.h#L1119-L1128)）：

```cpp
size_t numTrailingObjects(OverloadToken<detail::OperandStorage>) const {
  return hasOperandStorage ? 1 : 0;          // 操作数仓库：0 或 1 个
}
size_t numTrailingObjects(OverloadToken<BlockOperand>) const { return numSuccs; }
size_t numTrailingObjects(OverloadToken<Region>) const { return numRegions; }
size_t numTrailingObjects(OverloadToken<detail::OpProperties>) const {
  return getPropertiesStorageSize();         // properties 的字节数（见 4.3）
}
```

> 注意 `OpOperand`（操作数本身）没有出现在 `numTrailingObjects` 里——因为它的数量由 `OperandStorage` 自己管理（`OpOperand[]` 是 `OperandStorage` 的「内部数组」），框架不直接统计它。这也是 4.2.2 要讲的「操作数可外迁扩容」的伏笔。

本体持有的固定字段在私有区一览无余（[include/mlir/IR/Operation.h:L1069-L1101](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/include/mlir/IR/Operation.h#L1069-L1101)）：

```cpp
Block *block = nullptr;          // 所在的基本块（反向指针）
Location location;               // 源位置（可调试性）
mutable unsigned orderIndex = 0; // 块内序号，用于 O(1) 支配判断（见 4.4）
const unsigned numResults;
const unsigned numSuccs;
const unsigned numRegions : 23;
bool hasOperandStorage : 1;      // 是否分配了操作数仓库
unsigned char propertiesStorageSize : 8;  // properties 字节数 / 8
OperationName name;              // 操作名（含方言信息）
DictionaryAttr attrs;            // 可丢弃属性字典
```

这些字段几乎都是 `const` 或位域——一旦构造完成，**result/successor/region 的数量永远不变**（要变只能整个销毁重建）。这是 MLIR IR 「形状不可变」设计的体现。

至于「前缀区」的大小，由静态成员 `prefixAllocSize` 计算（[include/mlir/IR/Operation.h:L1001-L1014](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/include/mlir/IR/Operation.h#L1001-L1014)），它把「inline 结果」和「out-of-line 结果」两类（见 4.2.1）分别乘以各自的大小再求和。类开头那段长注释则用一张图说明了反序布局（[include/mlir/IR/Operation.h:L41-L49](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/include/mlir/IR/Operation.h#L41-L49)）：

```
[Result2, Result1, Result0, Operation]
                            ^ `Operation*` 指向这里
```

#### 4.1.4 代码实践

**实践目标**：用源码确认「尾随区的五种子对象及其顺序」，并验证它们的数量来源。

**操作步骤**：

1. 打开 [include/mlir/IR/Operation.h:L83-L87](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/include/mlir/IR/Operation.h#L83-L87)，抄下 `TrailingObjects` 的 5 个模板类型参数，这就是尾随区顺序。
2. 跳到 [L1119-L1128](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/include/mlir/IR/Operation.h#L1119-L1128)，对每个类型，记下 `numTrailingObjects` 返回的是哪个字段（如 `Region → numRegions`、`BlockOperand → numSuccs`）。
3. 注意 `OpOperand` 没有自己的 `numTrailingObjects` 重载，思考：它的数量由谁掌管？（答：`OperandStorage`。）

**需要观察的现象**：你会发现「能直接被框架统计数量」的尾随对象只有 4 类（OperandStorage、OpProperties、BlockOperand、Region），而 OpOperand 是「寄养」在 OperandStorage 里的。

**预期结果**：得到一张「尾随对象 → 数量字段」对照表，理解为什么操作数可以动态增减而 region/successor 不能。

#### 4.1.5 小练习与答案

**练习 1**：为什么 result 要放在 `Operation` 本体**前面**（低地址），而不是像 operand/region 那样放在后面？

> **答案**：放在前面且反序，使得「result 0 紧贴本体」。这样从一个 result 指针出发，只需向前跳过 `(resultNumber + 1)` 个 result 的位置，就能 O(1) 算出 owner operation 的地址（见 4.2.1 的 `getOwner`）。如果放在后面，由于 result 数量在编译期未知、且要和其它变长数组混排，定位 owner 就麻烦得多。operand 不需要这种「从 operand 反查 owner」的频繁操作（operand 本身就记录了 owner），所以可以放后面。

**练习 2**：`numResults`、`numSuccs`、`numRegions` 都是 `const`，这对 IR 的可变性意味着什么？

> **答案**：一个 `Operation` 一旦创建，它的「形状」（几个结果、几个后继、几个区域）就**终身不变**。想增减结果或区域，只能销毁旧操作、创建新操作。唯一能在生命周期内动态变化的「形状」是**操作数**（operand），这也是 `OperandStorage` 设计成可扩容仓库的原因（4.2.2）。

---

### 4.2 四类子对象的存储：Result / Operand / Region / Successor

本节逐个拆解 `Operation` 持有的四类子对象在内存里到底怎么放。它们各有各的存储策略，理解了这四者，4.1 的布局图就完全落地了。

#### 4.2.1 概念说明：result 的两类存储

一个操作可能有 0 个、1 个甚至上百个 result。如果每个 result 都存一个完整的「类型 + 索引」结构，对只有 1~2 个结果的常见操作就很浪费。MLIR 的优化是：

- **前 6 个 result 用 `InlineOpResult`**：它的索引（0~5）**不单独占字段**，而是塞进 `ValueImpl` 的「类型指针」的低位里（利用指针未用的低位 bit，即 `PointerIntPair`）。这样每个 inline result 只占一个指针大小（类型指针 + 隐含的 3 bit 索引）。
- **第 7 个及以后的 result 用 `OutOfLineOpResult`**：它额外存一个 `outOfLineIndex` 字段，因为低位 bit 已经不够表示大索引了。

这套机制由 `ValueImpl` 里的 `Kind` 枚举驱动（[include/mlir/IR/Value.h:L45-L60](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/include/mlir/IR/Value.h#L45-L60)）：

```cpp
enum class Kind {
  // 前 N 个 kind（0~5）都是 inline 操作结果，kind 本身就代表 result 编号
  OutOfLineOpResult = 6,
  BlockArgument = 7
};
```

所以「最多 inline 几个」由 `OutOfLineOpResult = 6` 决定——`getMaxInlineResults()` 返回 6，即 **result 0~5 共 6 个是 inline**。

> ⚠️ 一个真实的「读源码」发现：`Operation.h` 头部注释里写的是「the first 5 results」（[L56-L60](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/include/mlir/IR/Operation.h#L56-L60)），但代码里 `OutOfLineOpResult = 6`，实际 inline 的是 **6 个**（编号 0~5）。注释已经过时，以代码为准。这正是「不要只读注释、要对照实现」的好例子。

#### 4.2.2 核心流程：从 result 反查 owner

result 反序前置的最大收益，体现在 `OpResultImpl::getOwner()` 里（[lib/IR/Value.cpp:L115-L142](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/lib/IR/Value.cpp#L115-L142)）。它完全靠指针算术，没有任何查表：

- 对 inline result：`this` 向前跳 `resultNumber + 1` 个 `InlineOpResult` 的位置，就到本体。
- 对 out-of-line result：先跳过自己及之前的 out-of-line 结果，再跳过全部 6 个 inline 结果，到本体。

内存示意（来自该函数注释）：

```
| Out-of-Line results | Inline results | Operation |
```

#### 4.2.3 源码精读：result 与 operand/region/successor 的存储

`Operation` 内部定位 result 的三个私有函数体现了「inline/out-of-line 分流」（[include/mlir/IR/Operation.h:L1023-L1047](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/include/mlir/IR/Operation.h#L1023-L1047)）：

```cpp
// inline 结果：从 this 向前（低地址）数 resultNumber+1 个
detail::InlineOpResult *getInlineOpResult(unsigned resultNumber) {
  return reinterpret_cast<detail::InlineOpResult *>(this) - ++resultNumber;
}
// out-of-line 结果：从「最后一个 inline 结果」位置再向前数
detail::OutOfLineOpResult *getOutOfLineOpResult(unsigned resultNumber) { ... }
// 入口：根据编号判断走哪条
detail::OpResultImpl *getOpResultImpl(unsigned resultNumber) {
  unsigned maxInlineResults = detail::OpResultImpl::getMaxInlineResults();
  if (resultNumber < maxInlineResults) return getInlineOpResult(resultNumber);
  return getOutOfLineOpResult(resultNumber - maxInlineResults);
}
```

`OutOfLineOpResult` 的实现确认它多了一个 `outOfLineIndex` 字段（[include/mlir/IR/Value.h:L401-L419](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/include/mlir/IR/Value.h#L401-L419)）：

```cpp
class OutOfLineOpResult : public OpResultImpl {
  unsigned getResultNumber() const { return outOfLineIndex + getMaxInlineResults(); }
  uint64_t outOfLineIndex;   // ← 多出来的字段
};
```

**Operand 的存储**则完全不同。操作数存在 `OperandStorage` 这个「仓库」里，仓库本身是尾随区里的 1 个对象，而它管理的 `OpOperand[]` 数组**初始时**紧贴在尾随区最末尾（也是一次 `malloc` 的一部分）。但当操作数数量增长超过初始容量时，`OpOperand[]` 会被整体搬到一个**单独 `malloc` 的动态缓冲区**，由 `isStorageDynamic` 标志标记（详见 4.3）。访问入口（[include/mlir/IR/Operation.h:L371-L411](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/include/mlir/IR/Operation.h#L371-L411)）：

```cpp
unsigned getNumOperands() {
  return LLVM_LIKELY(hasOperandStorage) ? getOperandStorage().size() : 0;
}
MutableArrayRef<OpOperand> getOpOperands() {
  return LLVM_LIKELY(hasOperandStorage) ? getOperandStorage().getOperands()
                                        : MutableArrayRef<OpOperand>();
}
```

**Region 的存储**最直白：就是一个尾随的 `Region[]` 数组，数量恒为 `numRegions`（[include/mlir/IR/Operation.h:L699-L714](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/include/mlir/IR/Operation.h#L699-L714)）：

```cpp
unsigned getNumRegions() { return numRegions; }
MutableArrayRef<Region> getRegions() {
  if (numRegions == 0) return {};
  return getTrailingObjects<Region>(numRegions);   // 直接取尾随数组
}
```

每个 `Region` 在构造时被 placement-new 出来，并把 owner 设为本操作（见 4.4 的 create）。Region 本身「很轻」，内部装的是一个 `Block` 链表——那是 u2-l4 的主题。

**Successor 的存储**：后继块引用存为尾随的 `BlockOperand[]` 数组，数量为 `numSuccs`（[include/mlir/IR/Operation.h:L720-L737](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/include/mlir/IR/Operation.h#L720-L737)）。只有终止符操作（terminator）才会有后继，否则 `numSuccs == 0`：

```cpp
MutableArrayRef<BlockOperand> getBlockOperands() {
  return getTrailingObjects<BlockOperand>(numSuccs);
}
```

`BlockOperand` 和 `OpOperand` 一样，都继承自 `IROperand`，都挂在「被引用对象」的 use-def 双向链表上——区别只是 `OpOperand` 引用的是 `Value`，`BlockOperand` 引用的是 `Block*`（[include/mlir/IR/BlockSupport.h:L28-L39](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/include/mlir/IR/BlockSupport.h#L28-L39)）。

#### 4.2.4 代码实践

**实践目标**：用一个 IR 例子，亲手「数」出一个操作的 result/operand/region/successor 各有几个，并对应到存储方式。

**操作步骤**：

1. 写一段 `.mlir`（u1-l4 已学过语法）：
   ```mlir
   func.func @demo(%a: i32) -> i32 {
     %0 = arith.addi %a, %a : i32
     func.return %0 : i32
   }
   ```
2. 对 `%0 = arith.addi` 这个操作，回答：
   - 几个 result？（1 个 → inline，编号 0）
   - 几个 operand？（2 个 → 尾随 `OpOperand[2]`）
   - 几个 region？（0 个 → 不分配尾随 `Region[]`）
   - 几个 successor？（0 个 → 不分配 `BlockOperand[]`）
3. 再看 `func.return`：它有 1 个 operand（`%0`）、0 个 result、0 个 region，但是**终止符**——它的「successor」是函数出口，对 `func.return` 而言没有块后继，所以 `numSuccs` 仍是 0；换成一个 `cf.cond_br` 才会有 2 个 successor。

**需要观察的现象**：同一个「操作」概念，不同具体操作的四个计数差异很大，从而内存布局也差异很大——这正是「一次性按需 `malloc`」的意义。

**预期结果**：能对任意一行 `.mlir` 操作说出它的四类计数，并判断 result 是 inline 还是 out-of-line。运行验证可用 `mlir-opt --mlir-print-op-generic`（待本地验证具体输出）。

#### 4.2.5 小练习与答案

**练习 1**：一个有 8 个 result 的操作，它的 result 在前缀区怎么排？

> **答案**：result 0~5 是 inline（共 6 个），result 6、7 是 out-of-line。前缀区从低到高为：`OutOfLineOpResult[1](result7), OutOfLineOpResult[0](result6), InlineOpResult[5], InlineOpResult[4], ..., InlineOpResult[0], Operation`。inline 部分和 out-of-line 部分各自反序，out-of-line 整体在 inline 之前（更低地址）。

**练习 2**：为什么 `getNumOperands()` 里要用 `LLVM_LIKELY(hasOperandStorage)` 分支？

> **答案**：因为某些操作带有 `ZeroOperands` trait 且确实没有操作数时，`create` 会**完全不分配** `OperandStorage`（`hasOperandStorage=false`，见 4.4），此时尾随区里根本没有这个仓库对象，直接访问会是未定义行为。这个分支就是为这种「省掉整个仓库」的优化兜底。`LLVM_LIKELY` 提示分支预测器「绝大多数操作有仓库」。

---

### 4.3 OperationSupport 辅助结构：OperationState 与 OperandStorage

`Operation.h`/`.cpp` 是「主角」，而 `OperationSupport` 是「配角群」——它们让 `Operation` 的创建和管理变得规整。本节讲两个最重要的辅助结构。

#### 4.3.1 概念说明

- **`OperationState`（操作状态）**：创建一个 `Operation` 需要一长串参数（位置、名字、操作数、结果类型、属性、后继、区域、properties……）。直接把这些一股脑塞进 `create` 的形参表既难读又易错。`OperationState` 就是一个**「建操作的物料清单」**：把这些要素先收集到一个结构体里，再整体交给 `create`。它也是 `OpBuilder` 构造操作时的内部载体。
- **`OperandStorage`（操作数仓库）**：管理一个操作的所有 `OpOperand`。它的核心能力是「**初始用尾随数组，不够了再外迁到动态缓冲区**」，让操作数可以在操作生命周期内增减，而 region/result 不行。
- **`OpOperand` / `BlockOperand`**：一条「使用记录」。它不仅记录「我用了哪个 Value/Block」，还**自动把自己挂进被用对象的 use-def 双向链表**，从而支持高效的 users/uses 遍历和 RAUW（replaceAllUsesWith）。

#### 4.3.2 核心流程

`OperandStorage` 的扩容策略（`resize`）是本节的算法重点，流程如下：

1. 若新数量 ≤ 当前数量：原地析构多余的操作数即可。
2. 若新数量 ≤ 当前 `capacity`（初始 capacity = 创建时的操作数个数）：在尾随数组里原地 placement-new 新操作数。
3. 否则（超出尾随数组容量）：`malloc` 一块更大的缓冲区（新容量取 `NextPowerOf2(capacity+2)` 与 `newSize` 的较大值），把旧 `OpOperand` 全部移动过去，析构旧的，置 `isStorageDynamic=true`。

这套策略保证了「**只在操作数首次增长超容量时付一次 `malloc`，之后按 2 的幂扩容摊销为 O(1)**」——和 `std::vector` 的扩容思想一致。

至于 `OpOperand` 如何挂进 use-def 链：它继承的 `IROperand` 在构造/赋值时会自动调用 `insertIntoCurrent()`（[include/mlir/IR/UseDefLists.h:L127-L147](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/include/mlir/IR/UseDefLists.h#L127-L147)），把自己链入 Value 维护的使用链表；`set(newValue)` 时先 `removeFromCurrent()` 再 `insertIntoCurrent()`，保证链表始终正确。这就是 u2-l1 提到的「use 链为双向链表使 RAUW 高效」的底层实现。

#### 4.3.3 源码精读

`OperationState` 的字段就是一份「物料清单」（[include/mlir/IR/OperationSupport.h:L966-L984](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/include/mlir/IR/OperationSupport.h#L966-L984)）：

```cpp
struct OperationState {
  Location location;
  OperationName name;
  SmallVector<Value, 4> operands;
  SmallVector<Type, 4> types;          // 结果类型
  NamedAttrList attributes;
  SmallVector<Block *, 1> successors;
  SmallVector<std::unique_ptr<Region>, 1> regions;
  Attribute propertiesAttr;            // 用 Attribute 形式提供 properties（未注册操作用）
private:
  PropertyRef properties;              // 用强类型指针提供 properties（注册操作用）
  ...
};
```

它还提供 `getOrAddProperties<T>()` 模板（[L1012-L1037](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/include/mlir/IR/OperationSupport.h#L1012-L1037)），让方言代码以**类型安全**的方式准备 properties——这是 MLIR 较新的「properties」机制（把固有属性存为操作的直接成员，见 u1-l4），用以替代老的 inherent attribute。

`OperandStorage` 的声明展示了它的「容量 + 是否动态 + 数量 + 指针」四件套（[include/mlir/IR/OperationSupport.h:L1135-L1178](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/include/mlir/IR/OperationSupport.h#L1135-L1178)）：

```cpp
class alignas(8) OperandStorage {
public:
  OperandStorage(Operation *owner, OpOperand *trailingOperands, ValueRange values);
  void setOperands(Operation *owner, ValueRange values);
  MutableArrayRef<OpOperand> getOperands() { return {operandStorage, size()}; }
  unsigned size() { return numOperands; }
private:
  MutableArrayRef<OpOperand> resize(Operation *owner, unsigned newSize);
  unsigned capacity : 31;
  unsigned isStorageDynamic : 1;     // ← 是否已外迁到动态缓冲区
  unsigned numOperands;
  OpOperand *operandStorage;          // ← 指向尾随数组 或 动态缓冲区
};
```

构造函数确认「初始时操作数走尾随数组」（[lib/IR/OperationSupport.cpp:L233-L240](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/lib/IR/OperationSupport.cpp#L233-L240)）：

```cpp
OperandStorage::OperandStorage(Operation *owner, OpOperand *trailingOperands,
                               ValueRange values)
    : isStorageDynamic(false), operandStorage(trailingOperands) {
  numOperands = capacity = values.size();
  for (unsigned i = 0; i < numOperands; ++i)
    new (&operandStorage[i]) OpOperand(owner, values[i]);   // placement-new + 自动挂 use 链
}
```

`resize` 的「外迁」分支（[lib/IR/OperationSupport.cpp:L349-L376](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/lib/IR/OperationSupport.cpp#L349-L376)）则展示了「超过容量就 `malloc` 新缓冲区、移动旧操作数、置 `isStorageDynamic=true`」的全过程：

```cpp
unsigned newCapacity = std::max(unsigned(llvm::NextPowerOf2(capacity + 2)), newSize);
OpOperand *newOperandStorage = (OpOperand *)malloc(sizeof(OpOperand) * newCapacity);
std::uninitialized_move(origOperands.begin(), origOperands.end(), newOperands.begin());
...
operandStorage = newOperandStorage;
capacity = newCapacity;
isStorageDynamic = true;
```

析构时（[L242-L249](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/lib/IR/OperationSupport.cpp#L242-L249)），只有 `isStorageDynamic` 为真时才 `free` 那块动态缓冲区——否则操作数数组是「借住」在 `Operation` 那次 `malloc` 里的大内存，不该单独释放。

#### 4.3.4 代码实践

**实践目标**：跟踪一次「操作数扩容」，确认它何时触发 `malloc`。

**操作步骤**：

1. 在 [lib/IR/OperationSupport.cpp:L327](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/lib/IR/OperationSupport.cpp#L327) 的 `resize` 入口处设想加一行日志（**示例代码，勿改真实源码**）：
   ```cpp
   llvm::errs() << "resize: " << numOperands << " -> " << newSize
                << " dynamic=" << isStorageDynamic << "\n";
   ```
2. 设想一个有 2 个操作数的操作被 `insertOperands` 反复插入，画出 `numOperands / capacity / isStorageDynamic` 的变化。
3. 推导：第一次扩容发生在「新数量 > 初始 capacity」时，之后 capacity 走 2 的幂。

**需要观察的现象**：`isStorageDynamic` 一旦置真就永不回假；capacity 单调递增。

**预期结果**：复述出「尾随数组 → 超容量 → 动态缓冲区」的状态机。运行验证需自行编译并加日志（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：`OperandStorage` 的 `capacity` 初始值是多少？为什么之后按 2 的幂增长？

> **答案**：初始 `capacity = values.size()`（创建时的操作数个数）。之后按 2 的幂（`NextPowerOf2`）增长，是为了让「多次插入」的**摊还复杂度为 O(1)**——和 `std::vector` 一样，偶尔一次扩容 O(n) 被 n 次插入分摊。

**练习 2**：`OpOperand` 构造时为什么不需要手动调用「把自己加到 Value 的 use 链」？

> **答案**：因为 `OpOperand` 继承的 `IROperand` 构造函数自动调用了 `insertIntoCurrent()`（[UseDefLists.h:L130-L133](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/include/mlir/IR/UseDefLists.h#L130-L133)），`set()` 也会自动先删后插。这种「构造即入链」的设计让 use-def 链永远不会因为忘记维护而悬空。

---

### 4.4 Operation 的创建、销毁与生命周期

前三节讲了 `Operation` 「长什么样」「存了什么」。本节把它们串起来，看一个操作如何被**创建出来**、如何在块之间**移动**、如何被**销毁**。这也是本讲综合实践的落脚点。

#### 4.4.1 概念说明

- **工厂模式创建**：`Operation` 没有公开构造函数，只能通过一组静态 `create(...)` 工厂方法创建。多个 `create` 重载最终都收敛到一个「真正分配器」。
- **链表节点身份**：`Operation` 继承 `ilist_node_with_parent`，意味着它本质是 LLVM 侵入式链表 `iplist<Operation>` 的节点。一个 Block 持有这样一个链表，操作就「住」在块里。`block` 这个反向指针由链表 trait 自动维护。
- **块内顺序号**：每个操作有一个 `orderIndex`，用于在**同一个块内**做 O(1) 的「谁在前」判断（支配关系的局部基础）。它采用「懒重算 + 等距编号 + 中点插入」策略，避免每次插入都全量重排。
- **定制销毁**：因为有前缀区，`destroy()` 必须**先把指针退回到整块 `malloc` 的真实起点**再 `free`，普通 `delete` 会释放错地址。

#### 4.4.2 核心流程

**创建主流程**（收敛到 `create(..., DictionaryAttr, ...)`，4.4.3 精读）：

```
用户调用 create / OpBuilder
   └─ OperationState 收集物料（location/name/operands/types/attrs/...）
       └─ create(state) 解包，转调 create(..., DictionaryAttr, ...)
            ├─ 1. 校验（无空结果类型、非终止符不该有后继）
            ├─ 2. 算各类计数 + 是否需要操作数仓库
            ├─ 3. 算总字节数（前缀 + 本体 + 尾随）→ malloc 一次
            ├─ 4. placement-new 本体（构造函数只填字段 + 初始化 properties）
            ├─ 5. placement-new 前缀 results（inline + out-of-line）
            ├─ 6. placement-new 尾随 regions（每个 Region 设 owner）
            ├─ 7. placement-new OperandStorage（进而构造 OpOperand[]，挂 use 链）
            ├─ 8. placement-new 尾随 BlockOperand[]（后继）
            └─ 9. setAttrs（把固有属性拆进 properties）
```

**块内顺序号的「中点插入」**：当要在两个已有操作 A、B 之间插入新操作时，若 `prevOrder + 1 == nextOrder`（中间无空位），就触发整块重算（`recomputeOpOrder`，等距步长 `kOrderStride=5`）；否则取中点：

\[
\text{orderIndex} = \text{prevOrder} + \frac{\text{nextOrder} - \text{prevOrder}}{2}
\]

这和「顺序维护结构（order-maintenance）」的经典做法一致：只要相邻两数之间有空隙，插入就是 O(1)；空隙耗尽才偶发一次 O(n) 重排，摊还开销很低。

#### 4.4.3 源码精读

四个 `create` 重载层层收敛。最上层是「从 `OperationState` 建」（[lib/IR/Operation.cpp:L33-L47](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/lib/IR/Operation.cpp#L33-L47)），它把 state 里的字段解包，转交给「带 `DictionaryAttr`」的重载：

```cpp
Operation *Operation::create(const OperationState &state) {
  Operation *op = create(state.location, state.name, state.types, state.operands,
                         state.attributes.getDictionary(state.getContext()),
                         state.properties, state.successors, state.regions);
  // 若用 Attribute 形式提供 properties，建好后再回填
  if (LLVM_UNLIKELY(state.propertiesAttr)) {
    op->setPropertiesFromAttribute(state.propertiesAttr, /*emitError=*/nullptr);
  }
  return op;
}
```

中间两个重载（[L49-L75](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/lib/IR/Operation.cpp#L49-L75)）负责「填默认属性」「接管现成 `DictionaryAttr`」，最终都落到**真正的分配器**（[L79-L149](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/lib/IR/Operation.cpp#L79-L149)）。这是本讲最重要的一段，逐段看：

**(a) 算计数与是否需要仓库**（[L86-L97](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/lib/IR/Operation.cpp#L86-L97)）：

```cpp
unsigned numTrailingResults = OpResult::getNumTrailing(resultTypes.size()); // out-of-line 数
unsigned numInlineResults   = OpResult::getNumInline(resultTypes.size());   // inline 数
unsigned numSuccessors = successors.size();
unsigned numOperands   = operands.size();
int opPropertiesAllocSize = llvm::alignTo<8>(name.getOpPropertyByteSize());
bool needsOperandStorage =
    operands.empty() ? !name.hasTrait<OpTrait::ZeroOperands>() : true;
```

> 注意 `needsOperandStorage` 的精妙：只有「操作数本来就空」**并且**「操作声明了 `ZeroOperands` trait」时，才彻底不分配仓库。这保证未来不会无操作数、却仍留个空仓库；也保证可能动态加操作数的操作一定有仓库。

**(b) 一次性 `malloc` + 放本体**（[L101-L116](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/lib/IR/Operation.cpp#L101-L116)）：

```cpp
size_t byteSize = totalSizeToAlloc<detail::OperandStorage, detail::OpProperties,
                                   BlockOperand, Region, OpOperand>(
    needsOperandStorage ? 1 : 0, opPropertiesAllocSize, numSuccessors,
    numRegions, numOperands);                 // 尾随区总字节
size_t prefixByteSize = llvm::alignTo(
    Operation::prefixAllocSize(numTrailingResults, numInlineResults),
    alignof(Operation));                       // 前缀区总字节
char *mallocMem = (char *)malloc(byteSize + prefixByteSize);
void *rawMem = mallocMem + prefixByteSize;     // 本体放在前缀之后
Operation *op = ::new (rawMem) Operation(location, name, numResults,
    numSuccessors, numRegions, opPropertiesAllocSize, attributes,
    properties, needsOperandStorage);
```

**(c) 依次 placement-new 子对象**（[L121-L146](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/lib/IR/Operation.cpp#L121-L146)）：先填 results，再填 regions（`new (&op->getRegion(i)) Region(op)`，把 owner 设为本操作），再构造 `OperandStorage`（它再构造 `OpOperand[]` 并自动挂 use 链），再填 `BlockOperand[]`，最后 `setAttrs`。

**构造函数**本身很轻（[L151-L171](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/lib/IR/Operation.cpp#L151-L171)）：只填字段、把 properties 字节数换算成「8 字节单位」存进位域、在 debug 模式校验方言已注册、若有 properties 则用 `name.initOpProperties` 从传入的 `PropertyRef` 拷贝初始化。

**销毁**分两层。析构函数（[L175-L201](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/lib/IR/Operation.cpp#L175-L201)）显式调用各子对象的析构（`OperandStorage`、`BlockOperand`、`Region`、properties），但**不释放内存**；`destroy()`（[L204-L211](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/lib/IR/Operation.cpp#L204-L211)）才负责算回真实 `malloc` 起点（`this` 减去对齐后的前缀大小）并 `free`：

```cpp
void Operation::destroy() {
  char *rawMem = reinterpret_cast<char *>(this) -
                 llvm::alignTo(prefixAllocSize(), alignof(Operation));
  this->~Operation();
  free(rawMem);
}
```

**链表与顺序**：操作通过 `ilist_traits` 与 Block 联动。`addNodeToList`（[L494-L500](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/lib/IR/Operation.cpp#L494-L500)）在操作加入块时设置 `block` 指针并把 `orderIndex` 置为无效；`removeNodeFromList` 清空 `block`；`transferNodesFromList` 处理跨块移动并失效目标块顺序。`isBeforeInBlock`/`updateOrderIfNecessary`（[L379-L452](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/lib/IR/Operation.cpp#L379-L452)）实现上面说的「中点插入 + 懒重算」。

**移除与擦除**：`remove()` 只从块里摘下来不释放，`erase()` 摘下来并销毁（[L530-L541](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/lib/IR/Operation.cpp#L530-L541)）：

```cpp
void Operation::erase() {
  if (auto *parent = getBlock()) parent->getOperations().erase(this);
  else destroy();
}
```

**克隆**：`clone`（[L721-L755](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/lib/IR/Operation.cpp#L721-L755)）通过 `IRMapping` 重映射操作数和后继，再 `create` 一个新操作，可选地把各 region 递归 `cloneInto` 进去。

#### 4.4.4 代码实践

**实践目标**：对照源码，写出「创建一个 `Operation` 需要的要素清单」与「分配器 9 步流程」。这是本讲综合实践的核心预热。

**操作步骤**：

1. 打开 [lib/IR/Operation.cpp:L79-L149](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/lib/IR/Operation.cpp#L79-L149)。
2. 列出形参，得到「要素清单」：`location`、`name`、`resultTypes`、`operands`、`attributes(DictionaryAttr)`、`properties`、`successors`、`numRegions`。
3. 把函数体注释成 9 个阶段（校验 → 算计数 → 算字节 → malloc → 建本体 → 建 results → 建 regions → 建 operands → 建 successors → setAttrs），与 4.4.2 的流程图对照。
4. （可选）写一段**示例代码**，用 `OperationState` 构造一个最简操作（**未编译运行，待本地验证**）：
   ```cpp
   // 示例代码：仅示意 API 形态，依赖已编译的 MLIR 库
   using namespace mlir;
   OpBuilder builder(context);
   Location loc = builder.getUnknownLoc();
   OperationState state(loc, "arith.addi");
   state.addOperands({a, b});          // a, b 是已有 Value
   state.addTypes(builder.getI32Type());
   Operation *op = builder.create(state);
   ```

**需要观察的现象**：要素清单与 4.3.1 的 `OperationState` 字段一一对应——`OperationState` 就是把这组要素打包的容器。

**预期结果**：得到一张「要素 → 形参 → OperationState 字段」三列对照表。

#### 4.4.5 小练习与答案

**练习 1**：`destroy()` 为什么要先 `this - prefixAllocSize()` 再 `free`？

> **答案**：因为 `malloc` 返回的起点是前缀区的最低地址，而 `this`（`Operation*`）指向的是前缀区**之后**的本体地址。如果直接 `free(this)`，会释放错误的地址（未定义行为）。必须先减去对齐后的前缀大小，回到真正的 `malloc` 起点。

**练习 2**：在两个 `orderIndex` 分别为 10 和 11 的操作之间插入新操作，会发生什么？

> **答案**：因为 `prevOrder + 1 == nextOrder`（10+1==11），中间没有空位，无法取中点，于是触发 `block->recomputeOpOrder()`，对整块操作按步长 `kOrderStride=5` 重新等距编号，之后再赋值。这正是「懒重算」的触发条件。

**练习 3**：`erase()` 和 `remove()` 的区别是什么？

> **答案**：`remove()` 只把操作从父块的链表里摘除（`block` 置空），**不释放内存**，调用者还可以再把它插回别处；`erase()` 则在摘除之后调用 `destroy()` **释放内存**。对一个没有父块的操作，`erase()` 直接 `destroy()`。

---

## 5. 综合实践

把本讲四个模块串起来，完成下面这个「**给一段 IR 做内存尸检**」的任务。

**任务背景**：给你这段 `.mlir`（来自 u1-l4 的语法）：

```mlir
func.func @f(%a: i32, %b: i32) -> i32 {
  %c = arith.addi %a, %b : i32
  %r = arith.muli %c, %c : i32
  func.return %r : i32
}
```

**要求**：针对 `%r = arith.muli %c, %c` 这个操作，产出一份数据表，逐项填空并给出对应的源码依据（永久链接 + 行号）：

| 项目 | 值 | 依据 |
| --- | --- | --- |
| result 个数 / 是 inline 还是 out-of-line | ? | `OpResult::getNumInline` [lib/IR/Value.cpp:L195-L205](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/lib/IR/Value.cpp#L195-L205) |
| operand 个数 / 存在哪里 | ? | `OperandStorage` [lib/IR/OperationSupport.cpp:L233-L240](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/lib/IR/OperationSupport.cpp#L233-L240) |
| region 个数 / 是否分配尾随数组 | ? | `getRegions` [include/mlir/IR/Operation.h:L702-L708](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/include/mlir/IR/Operation.h#L702-L708) |
| successor 个数 | ? | `getBlockOperands` [include/mlir/IR/Operation.h:L720-L722](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/include/mlir/IR/Operation.h#L720-L722) |
| `hasOperandStorage` 是否为真 | ? | `create` [lib/IR/Operation.cpp:L96-L97](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/lib/IR/Operation.cpp#L96-L97) |

**进阶**：画出这个操作的字节布局图（前缀 results / 本体 / 尾随 OperandStorage+OpOperand[2]），标注 `Operation*` 指向的位置；并回答——如果之后给这个 `muli` 再 `insertOperands` 加 3 个操作数，`OperandStorage` 会经历什么（参考 4.3.3 的 `resize`）？

**预期产出**：一张填好的表 + 一张布局图 + 一段对 `resize` 行为的预测（操作数从 2 增到 5，超过初始 capacity 2，触发一次 `malloc` 外迁到容量至少 8 的动态缓冲区，`isStorageDynamic` 置真）。运行验证可用带 `--mlir-print-op-generic` 的 `mlir-opt`（待本地验证）。

## 6. 本讲小结

- `Operation` 在内存里是「**前缀 results（反序）+ 本体 + 尾随对象（正序）**」三段连续布局，**一次性 `malloc`** 出来；不能用普通 `new`/`delete`。
- **result** 分两类：前 6 个是 `InlineOpResult`（索引塞进类型指针低位，不占额外字段），第 7 个起是 `OutOfLineOpResult`（多一个 `outOfLineIndex`）；反序前置使得从任一 result 可 O(1) 算回 owner。
- **operand** 存在 `OperandStorage` 里，初始是尾随数组，超容量时外迁到动态缓冲区（按 2 的幂扩容），是唯一可在生命周期内增减的「形状」；`OpOperand` 构造即自动挂入 Value 的 use-def 双向链表。
- **region** 是恒定数量的尾随 `Region[]` 数组，每个 Region 在构造时设 owner 为本操作；**successor** 是终止符才有的尾随 `BlockOperand[]` 数组。
- `OperationState` 是「建操作的物料清单」，把创建所需的全部要素打包；多个 `create` 重载收敛到一个真正分配器，走「算计数 → 算字节 → malloc → 逐个 placement-new」九步流程。
- 销毁走定制 `destroy()`（退回到 `malloc` 真实起点再 `free`）；块内顺序用「等距编号 + 中点插入 + 懒重算」实现摊还 O(1) 的前后判断。

## 7. 下一步学习建议

本讲把「`Operation` 这棵树的根节点」讲透了，接下来自然分两条路：

- **往「边」走 → u2-l3《Value：OpResult 与 BlockArgument》**：本讲反复出现的 result、use-def 链、`OpOperand`，将在 u2-l3 从「值」的视角统一起来。你会看到 `Value` 如何统一表示 OpResult 与 BlockArgument，以及 users/uses 遍历和 RAUW 的完整用法。
- **往「树」走 → u2-l4《Block 与 Region》**：本讲只讲了 `Operation` 如何**持有** region，而 region 内部的 `Block`、块参数、终止符与后继的**控制流语义**留给 u2-l4。
- **平行阅读**：想巩固「一次性 `malloc` + 尾随对象」这个模式，可以翻看 LLVM 的 `llvm/ADT/STLExtras.h` 与 `llvm/Support/TrailingObjects.h`（在 llvm-project 的 llvm 子项目里，非本目录），对照理解 MLIR 借鉴的工程手法。

读完 u2-l3 和 u2-l4，你就完成了第 2 单元「核心数据结构」的三大支柱（Operation / Value / Block-Region），届时再进入 u2-l5（Type 与 Attribute）和 u2-l6（Context/Builder/Location）就会非常顺畅。
