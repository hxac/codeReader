# IR 总览：Package、Function、Node、Value

## 1. 本讲目标

在上一讲（u1-l5）里，我们把一个 `.x` 文件沿着 `ir_converter_main → opt_main → codegen_main` 走了一遍，看到了一段形如 `fn ... { add(...) }` 的文本。那串文本背后，XLS 真正在内存里持有的，是一套精心设计的数据结构——**XLS IR**。

学完本讲，你应该能够：

- 建立「XLS IR = 数据流图（sea of nodes）+ 类型化值」的整体心智模型；
- 说清 **Package、Function、Node** 三层的职责与包含关系：Package 拥有 Function，Function 持有一张由 Node 组成的数据流图；
- 理解 **Value 与 Bits** 如何表示任意位宽的运行期数据，以及它们与编译期 **Type** 的区别；
- 拿到一段 `.ir` 文本，能准确指出哪一行是 package、哪一行是函数、哪些是参数节点、哪些是运算节点，以及节点之间的数据流边是如何表达的。

本讲只讲「数据结构是什么」，不讲「怎么优化、怎么调度」——那些是第四单元的内容。

## 2. 前置知识

### 2.1 什么是 IR

IR 是 **Intermediate Representation（中间表示）** 的缩写。编译器不会把源码一次性翻译成目标代码，而是先翻译成一种「中间语言」，再在中间语言上反复分析和改写，最后才生成目标。XLS 只用**一套** IR，从前端（DSLX）一直用到接近 RTL 的代码生成阶段。

这一点很重要：很多编译器会在不同阶段换不同的 IR（dialect），而 XLS 刻意只用一套，换来的是「分析与变换组件可以最大程度复用」。

### 2.2 SSA 与 Sea of Nodes

- **SSA（Static Single Assignment，静态单赋值）**：每个值只被定义一次。如果你写 `x = x + 1`，SSA 会把它拆成 `x2 = x1 + 1`。这让数据依赖一目了然。
- **Sea of Nodes（节点之海）**：传统编译器用「控制流图（CFG）+ 基本块」来组织代码，因为 CPU 是**串行**执行的。但硬件的本质是「**所有东西同时发生、天然并行**」。所以 XLS 不用 CFG，而是用一张扁平的、由数据依赖连边的有向无环图（DAG）来表示计算，这就是 sea of nodes。

官方文档 [docs_src/ir_overview.md:9-27](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/docs_src/ir_overview.md#L9-L27) 把这两点点得很清楚：IR 是「dataflow-oriented」且「not control-flow-graph based」。

### 2.3 编译期类型 vs 运行期值

这是本讲最容易混淆的一对概念，务必先分清：

| 概念 | 代表 | 回答的问题 | 举例 |
|------|------|-----------|------|
| **Type（类型）** | `Type*`，由 `TypeManager` 管理 | 「这个值**长什么样**？几位？有没有符号？」 | `bits[32]`、`(bits[8], bits[8])` |
| **Value（值）** | `Value` 对象，内部用 `Bits` | 「这个值**具体是多少**？」 | `bits[32]:42`、`bits[8]:0xAB` |

类型在**编译期**就完全确定（决定电路连线宽度），值在**运行期/解释执行**时才有具体数字。Node 持有一个 `Type*`（它产出什么类型），而 `literal` 节点还会额外持有一个 `Value`（它产出的具体常量）。

## 3. 本讲源码地图

| 文件 | 角色 |
|------|------|
| [xls/ir/package.h](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/package.h) | 定义 `Package`——IR 的**顶层容器**，拥有所有函数、类型、通道、文件号表。 |
| [xls/ir/function_base.h](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/function_base.h) | 定义 `FunctionBase`——`Function`/`Proc`/`Block` 的**共同基类**，是真正持有「一堆 Node」的地方。 |
| [xls/ir/function.h](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/function.h) | 定义 `Function`——**无状态、偏组合**的可计算单元，额外有一个「返回值节点」。 |
| [xls/ir/node.h](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/node.h) | 定义 `Node`——数据流图里的**一个节点**，维护 operands（入边）与 users（出边）。 |
| [xls/ir/value.h](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/value.h) | 定义 `Value`——**运行期值**，可以是 bits / 元组 / 数组 / token。 |
| [xls/ir/bits.h](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/bits.h) | 定义 `Bits`——**任意位宽的二进制串**，是 `Value` 最核心的载荷。 |
| [xls/ir/op.h](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/op.h) | 定义 `Op` 枚举——每个节点「做什么运算」的标签。 |

记忆口诀：**Package 装 Function，Function 是一堆 Node 的容器，Node 产出 Type、Literal 节点还附带 Value。**

## 4. 核心概念与源码讲解

### 4.1 Package：IR 的顶层容器

#### 4.1.1 概念说明

`Package` 是整个 IR 的**根**。当你用 `ir_converter_main` 把一个 `.x` 转成 `.ir` 时，产物顶层那句 `package <名字>` 对应的就是一个 `Package` 对象。

它解决的问题是：「一个设计里可能有多个函数、多个进程、若干通道、一套类型、若干源文件位置——这些东西需要一个共同的拥有者（owner）来统一管理生命周期和命名。」

`Package` 承担四类职责：

1. **持有可计算单元**：函数（`Function`）、进程（`Proc`）、块（`Block`），用 `unique_ptr` 拥有它们；
2. **持有类型**：通过一个 `TypeManager` 竞技场（arena）统一分配/去重 `Type`；
3. **持有通道**：进程间通信用的 `Channel`；
4. **记录元信息**：源文件号表（`Fileno` ↔ 文件名）、节点 ID 计数器、顶层实体（top）、变换度量。

> **小知识**：源码里特意把 `Package` 设为**不可拷贝、不可移动**，因为函数内部有指回所属 `Package` 的父指针，移动会破坏这个不变量。要复制得显式调用 `ClonePackage`。

#### 4.1.2 核心流程

一个 `Package` 的生命期大致是：

```
构造 Package(name)
        │
        ├── 添加类型：GetBitsType(32) / GetTupleType(...)  → 由 TypeManager 去重并拥有
        ├── 添加函数：AddFunction(unique_ptr<Function>)     → 所有权转移到 Package
        ├── 设置顶层：SetTopByName("gcd")                   → 指定哪个函数/进程是入口
        ├── （后续 Pass）反复增删 Node                        → 每个 Node 向 Package 申请唯一 id
        │
        └── DumpIr()                                         → 把整张图序列化成可读文本
```

**顶层实体（top）** 是 `Package` 里一个非常关键的概念：流水线最终只为 top 服务。`GetTop()` 返回一个 `std::optional<FunctionBase*>`，说明「也可能没设顶层」。

#### 4.1.3 源码精读

`Package` 类的定义与构造：

[package.h:81-83](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/package.h#L81-L83) 定义了 `Package` 类，构造函数只接受一个名字 `name`。

添加函数/进程/块的三件套——所有权（`unique_ptr`）转入 `Package`：

[package.h:164-166](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/package.h#L164-L166) `AddFunction`/`AddProc`/`AddBlock`，注释写明「Ownership is transferred to the package」。

设置与查询顶层实体：

[package.h:94-102](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/package.h#L94-L102) `GetTop()` / `HasTop()` / `SetTop()` / `SetTopByName()`。

类型管理（委托给 `TypeManager`，下文 4.2 会再讲 `Type` 的来源）：

[package.h:128-141](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/package.h#L128-L141) `GetBitsType`、`GetArrayType`、`GetTupleType`、`GetFunctionType` 等，全部转发给内部的 `type_manager_`。

序列化与名字访问：

[package.h:286-294](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/package.h#L286-L294) `name()` 与 `DumpIr()`——后者「Dumps the IR in a parsable text format」，正是 `.ir` 文本的来源。

私有成员（看这里能一眼看清 `Package` 到底「拥有」什么）：

[package.h:487-500](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/package.h#L487-L500) `top_`（顶层）、`name_`、`next_node_id_`（下一个节点 id，初值 1）、三个 `vector<unique_ptr<...>>` 分别持有 functions/procs/blocks、`type_manager_`。

> 节点 id 从 1 开始递增，由 [package.h:233](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/package.h#L233) 的 `GetNextNodeIdAndIncrement()` 发放——这就是你在 `.ir` 里看到 `id=3` 这种编号的由来。

#### 4.1.4 代码实践（源码阅读型）

**目标**：在源码里验证「Package 把 functions / procs / blocks 分三个独立 vector 存放」这一事实，并理解为何这么设计。

**步骤**：

1. 打开 [package.h:264-279](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/package.h#L264-L279)，阅读 `functions()`、`procs()`、`blocks()` 三个访问器。
2. 再看私有成员 [package.h:495-497](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/package.h#L495-L497)，确认它们对应三个独立的 `vector`。
3. 注意源码里 [package.h:283-284](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/package.h#L283-L284) 有一条 TODO：「Consider holding functions and procs in a common vector」——说明历史上它们确实分开存，统一存放是未来可能的重构。

**需要观察的现象**：三类可计算单元各自独立存放；而 [package.h:284](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/package.h#L284) 的 `GetFunctionBases()` 把它们合并返回一个 `vector<FunctionBase*>`——这暗示三者有共同基类（见 4.2）。

**预期结果**：理解 Package 内部「按类型分桶存放 + 提供统一基类视图」的双重设计。

#### 4.1.5 小练习与答案

**练习 1**：`Package` 为什么被设计成不可拷贝（`delete` 了拷贝构造）？

> **答案**：见 [package.h:89-90](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/package.h#L89-L90) 的注释——函数内部持有指回 `Package` 的父指针（parent pointer），拷贝/移动会让这些指针失效。需要复制时改用 `clone_package.h` 的 `ClonePackage`。

**练习 2**：一个 `Package` 里可以有几个 top？

> **答案**：至多一个。`top_` 的类型是 `std::optional<FunctionBase*>`（[package.h:487](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/package.h#L487)），`optional` 表示「可能有，也可能没有」，但绝不会同时有两个。

---

### 4.2 Function、FunctionBase 与 Node：数据流图的持有者与节点

#### 4.2.1 概念说明

这一节是本讲的核心。先点破两个事实：

1. **`Function` 不是直接持有 Node 的那个类。** 真正「持有一堆 Node」的基类叫 **`FunctionBase`**，`Function`、`Proc`、`Block` 都继承自它。源码注释原话：[function_base.h:171](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/function_base.h#L171) ——「Base class for Functions and Procs. A holder of a set of nodes.」
2. **`Node` 是数据流图里的一个顶点。** 它知道自己有哪些**操作数（operands，入边）**，也被它的**使用者（users，出边）** 所知——这是一对**对称维护**的指针关系，构成了整张图的边。

为什么要把 Node 存进 `FunctionBase` 而不是直接 `Function`？因为进程（Proc）和块（Block）也都是「一张由 Node 组成的图」，只是语义不同：Function 是无状态组合、Proc 是有状态时序、Block 是带寄存器/端口的硬件实体。把「持有图」这件事抽到基类，避免三份重复代码。

**`Function` 相比 `FunctionBase` 多了什么？** 只多一样东西：一个**返回值节点** `return_value_`。因为函数是「输入→输出」的单值映射，必须指明哪个 Node 的结果作为函数输出；进程则用 `Next` 节点表达状态演化，不需要单一返回值。

#### 4.2.2 核心流程

**图的构造（建图）**：通常不是手写 `new Node`，而是通过 `FunctionBuilder`（下一讲会遇到）。最终都会落到：

```
FunctionBuilder::Add(...) 
   └─> 构造一个 Node 子类对象（unique_ptr）
       └─> FunctionBase::MakeNode<NodeT>(...)   [function_base.h:322]
              ├─ AddNode(unique_ptr)            [function_base.h:309]
              │     └─ AddNodeInternal：把节点塞进 nodes_ 链表
              └─ VerifyNode(新节点)              // 检查类型/操作数是否自洽
```

**Node 的边（operand / user 对称性）**：当一个 Node A 成为 Node B 的操作数时，必须同时：
- 在 B 的 `operands_` 里记录 A；
- 在 A 的 `users_` 里记录 B。

[function_base.h:562-563](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/function_base.h#L562-L563) 显示 Node 用 `std::list<std::unique_ptr<Node>>` 存储——选 `list` 而非 `vector` 是因为**节点会被频繁插入/删除**（优化 Pass 天天干这事），而 `list` 的删除不影响其它迭代器；同时配一个 `flat_hash_map<Node*, iterator>` 用于 O(1) 定位。

**图的序列化（DumpIr）**：遍历 `nodes_`，把每个 Node 打成一行文本，返回值那一行加 `ret` 前缀。

**图的查询**：任给一个 Node，可以 `operands()` 取它的输入、`users()` 取谁用它——这就是「sea of nodes」的全部导航能力，不需要基本块、不需要控制流边。

#### 4.2.3 源码精读

`FunctionBase` 是图的真正持有者：

[function_base.h:171-174](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/function_base.h#L171-L174) 类注释与 `NodeList` 类型别名（`std::list<std::unique_ptr<Node>>`）。

三种可计算单元用枚举区分：

[function_base.h:177-181](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/function_base.h#L177-L181) `enum class Kind { kFunction, kProc, kBlock }`。

访问节点集合：

[function_base.h:284-296](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/function_base.h#L284-L296) `node_count()` 与 `nodes()`（用 `UnwrappingIterator` 把 `unique_ptr<Node>` 解包成 `Node*` 供外部遍历）。

新增节点的统一入口：

[function_base.h:309-329](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/function_base.h#L309-L329) `AddNode<T>`（直接加）与 `MakeNode<NodeT, Args...>`（构造+加+校验），注释强调「verifies the newly constructed node after it is added」。

`Function` 在此基础上增加「返回值」语义：

[function.h:43-49](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/function.h#L43-L49) `class Function : public FunctionBase`，构造函数只多传一个 `Package*`。

[function.h:65-72](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/function.h#L65-L72) `return_value()` / `return_type()` / `set_return_value()`——函数的输出由一个特定 Node 担当。

[function.h:127](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/function.h#L127) `HasImplicitUse(node)` 直接判 `node == return_value()`——返回值节点是一种「隐式使用」，不会被任何其它节点当操作数，但绝不能被当成死代码删掉。

`Node` 的核心字段与方法：

[node.h:94-108](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/node.h#L94-L108) 每个 Node 都有：产出类型 `GetType()`、运算种类 `op()`、所属函数 `function_base()`、源位置 `loc()`。

[node.h:111-117](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/node.h#L111-L117) `operands()`——返回**入边**（操作数视图，刻意做成只读 `Span`，避免有人改了操作数却忘了同步 user 集）。

[node.h:284-285](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/node.h#L284-L285) `users()`——返回**出边**（使用者视图，按 node id 排序以保证稳定）。

[node.h:297-301](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/node.h#L297-L301) `operand(i)` 与 `operand_count()`——按下标取某个操作数。

[node.h:389-393](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/node.h#L389-L393) 私有字段 `operands_` 与 `users_` 都是 `absl::InlinedVector<Node*, 2>`——因为「大多数节点不超过 2 个操作数」，内联向量能避免堆分配，提升性能。

边的维护（修改图时如何保持对称）：

[node.h:121-125](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/node.h#L121-L125) `ReplaceOperand`——换掉某个操作数，会同步更新旧/新操作数的 user 集。

[node.h:135-150](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/node.h#L135-L150) `ReplaceUsesWith`——把「所有用到我」的地方换成另一个节点，这是优化 Pass 最常用的图改写原语。

`Op` 枚举（节点的「动作标签」，详见下一讲 u3-l2）：

[op.h:34-38](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/op.h#L34-L38) `enum class Op`，由宏 `XLS_FOR_EACH_OP_TYPE` 展开。每个具体运算（`kAdd`、`kEq`、`kParam`、`kLiteral`…）都是一个枚举值。

[op.h:62-74](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/op.h#L62-L74) 一组查询函数：`OpIsAssociative`（可结合）、`OpIsCommutative`（可交换）、`OpIsSideEffecting`（有副作用）——优化 Pass 据此判断能否重排、能否消除。

#### 4.2.4 代码实践（源码阅读型）

**目标**：亲手在一行真实的 `.ir` 文本里，把「节点」与「operand/user 边」对上号。

**步骤**：阅读这段来自仓库的真实测试 IR（`two_plus_two`，一个无参函数，返回 2+2）：

```ir
fn two_plus_two() -> bits[32] {
  literal.1: bits[32] = literal(value=2, id=1)
  literal.2: bits[32] = literal(value=2, id=2)
  ret add.3: bits[32] = add(literal.1, literal.2, id=3)
}
```

对照说明（每行 = 一个 `Node`）：

| 文本片段 | 对应 Node | op() | operands() | users() |
|----------|-----------|------|------------|---------|
| `literal.1 ... = literal(value=2)` | 常量节点，值=2 | `kLiteral` | （无） | `{add.3}` |
| `literal.2 ... = literal(value=2)` | 常量节点，值=2 | `kLiteral` | （无） | `{add.3}` |
| `ret add.3 ... = add(literal.1, literal.2)` | 加法节点 | `kAdd` | `[literal.1, literal.2]` | （隐式使用：函数返回值） |

**需要观察的现象**：
- `add(literal.1, literal.2)` 括号里的两个名字，就是 `add.3` 这个 Node 的 **operands**——数据从 `literal.1`、`literal.2` 流向 `add.3`。
- 反过来，`add.3` 就是 `literal.1` 和 `literal.2` 的 **user**。
- 前缀 `ret` 标记 `add.3` 是函数返回值（即 [function.h:65](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/function.h#L65) 的 `return_value_`），它没有显式 user，但有「隐式使用」，所以不会被当成死代码删掉。

**预期结果**：你能用 `operands()`/`users()` 这两个词，描述任意一行 `.ir` 里节点之间的有向边。这就是 sea of nodes 的全部导航语法。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `operands_` 和 `users_` 都用 `absl::InlinedVector<Node*, 2>`？

> **答案**：见 [node.h:389-393](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/node.h#L389-L393) 的注释「Most nodes have <= 2 operands」。内联 2 个元素能让绝大多数节点（add、and、eq…）零堆分配；超过 2 个时自动退化为堆存储，语义不变。这是为图遍历性能做的微观优化。

**练习 2**：`Function` 和 `Proc` 都继承自 `FunctionBase`，但只有 `Function` 有 `return_value()`。Proc 的「输出」靠什么表达？

> **答案**：Proc 靠 `Next` 节点表达「状态的下一拍取值」，靠 send/recv 通道与外界通信，不需要单一的返回值节点。`FunctionBase::HasImplicitUse` 是纯虚函数（[function_base.h:446](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/function_base.h#L446)），Function 和 Proc 各自实现自己的「隐式使用」语义——Function 判返回值，Proc 判状态/token。

**练习 3**：`FunctionBase` 用 `std::list` 而不是 `std::vector` 存 Node，主要原因是什么？

> **答案**：优化 Pass 会频繁增删节点。`std::list` 删除一个元素不会使其它迭代器失效（指针稳定性），而 `vector` 的 erase 会导致大量搬移并失效迭代器。配合 [function_base.h:563](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/function_base.h#L563) 的 `flat_hash_map<Node*, iterator>`，可在 O(1) 时间内定位并删除任一节点。

---

### 4.3 Value 与 Bits：运行期值与任意位宽数据

#### 4.3.1 概念说明

前面两节讲的都是「编译期结构」（Package、Function、Node、Type）。这一节讲**运行期数据**：当解释器真正去算 `2+2` 时，那个「2」在内存里是什么？答案是 **`Value`**，而 `Value` 里最核心的载荷是 **`Bits`**。

- **`Bits`**：一段**任意位宽**的二进制串（位宽 `bit_count` 可以是 1、8、32、1024……任意值）。硬件里数据天然就是定宽二进制，`Bits` 正是它的直接对应物。
- **`Value`**：一个带标签（tag）的变体（variant），可以是四种之一：
  - `kBits`——一个 `Bits`；
  - `kTuple`——多个 `Value` 的有序组合（可异构）；
  - `kArray`——多个**同类型** `Value` 的有序组合（同构）；
  - `kToken`——特殊的「令牌」，用于 Proc 里的时序同步。

为什么 `Value` 要做成变体而不是类继承？因为值是**大量、短暂**的对象（解释器每步都产生新值），变体（`std::variant`）比多态继承更省内存、更快，且值语义方便拷贝。

> **关键区分（再强调一次）**：`Type` 描述「形状」，`Value` 描述「内容」。一个 `literal` 节点的 `GetType()` 返回 `bits[32]`（形状），而它携带的 `Value` 是「具体的 32 位二进制」。`bits[32]:42` 这种文本写法里，`bits[32]` 是类型、`42` 是值。

#### 4.3.2 核心流程

**从文本到 Value**：当 `.ir` 文本里出现 `literal(value=2)` 时，解析器会构造一个 `Value`。对一个无符号整数 2，它走的是 `Bits` 路径：

```
文本 "2"
  └─> BitsOperations: UBits(value=2, bit_count=32)   // 构造一个 32 位 Bits
        └─> Value(Bits)                                // 包成 kBits 的 Value
              └─> Literal 节点持有这个 Value           // 节点的常量载荷
```

**Value 的组合**：元组和数组是递归构造的。例如 `(bits[8]:1, bits[8]:2)` 会先构造两个 `Value`，再用 `Value::Tuple({v1, v2})` 组装；数组则要求所有元素同类型（`Value::Array` 会校验）。

**位宽与硬件**：`Bits` 的位宽直接对应电路连线根数。`bits[32]` 是 32 根线，`bits[1]` 是 1 根线。这正是 XLS「改位宽 = 改电路」的底层原因——位宽不是抽象，是物理事实。

数据量上，一个 `bits[N]` 的总位数为 N，可用 `GetFlatBitCount()` 统计一个 `Value`（含嵌套元组/数组）占多少 bit。

#### 4.3.3 源码精读

`ValueKind` 枚举——四种值的标签：

[value.h:40-53](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/value.h#L40-L53) `enum class ValueKind { kInvalid, kBits, kTuple, kArray, kToken }`，注释特别指出「Arrays must be homogeneous」（数组元素必须同类型）。

`Value` 类与静态工厂：

[value.h:62-118](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/value.h#L62-L118) `Value::Tuple`、`Value::Array`、`Value::Token`、`Value::Bool` 等静态构造方法。其中 `Bool(bool)` 内部转成一个 1 位的 `Bits`（[value.h:115-118](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/value.h#L115-L118)）——布尔只是 `bits[1]` 的语法糖。

从 `Bits` 构造 `Value`：

[value.h:122-123](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/value.h#L122-L123) `explicit Value(Bits bits)`——把一个 `Bits` 包成 `kBits` 类型的值。这是最常用的构造路径。

类型查询与取值：

[value.h:141-148](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/value.h#L141-L148) `kind()`、`IsBits()`、`IsTuple()`、`IsArray()`、`bits()`——其中 `bits()` 返回内部持有的 `const Bits&`。

元组/数组元素的访问：

[value.h:152-157](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/value.h#L152-L157) `elements()`、`element(i)`、`size()`。

内部存储（看这里就懂 `Value` 为何是「变体」）：

[value.h:249-250](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/value.h#L249-L250) `kind_` + `payload_`，其中 `payload_` 是 `std::variant<std::nullptr_t, std::vector<Value>, Bits>`——要么是 Bits，要么是子 Value 的 vector，要么是空。

`Bits` 类——任意位宽二进制串：

[bits.h:50-58](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/bits.h#L50-L58) 注释「a vector of bits with a given width (bit_count)」，`explicit Bits(int64_t bit_count)` 构造一个**零初始化**的指定位宽对象。

特殊值的构造：

[bits.h:73-78](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/bits.h#L73-L78) `Bits::AllOnes(n)`（全 1）、`Bits::MaxSigned(n)`（有符号最大）、`Bits::MinSigned(n)`（有符号最小）——注意有符号/无符号不是 `Bits` 自身属性，而是运算怎么解释它。

从字节构造：

[bits.h:87-89](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/bits.h#L87-L89) `Bits::FromBytes(bytes, bit_count)`——底层用 `InlineBitmap`，这也是 `Bits` 的真正存储引擎（紧凑位图，省内存）。

> 类型如何由 Package 分配？回到 [package.h:158-160](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/package.h#L158-L160) 的 `GetTypeForValue(value)`——给定一个 `Value`，能反推出它对应的 `Type`。这就是「值 ↔ 类型」的桥。

#### 4.3.4 代码实践（源码阅读型 + 文本观察）

**目标**：在真实 `.ir` 文本里辨认四种 `Value`，并理解 `Bits` 位宽。

**步骤**：

1. 阅读这两段真实测试 IR：

```ir
// 来自 ir_parser_round_trip_test_ParseParamReturn.ir —— 单参函数
package ParseParamReturn
fn simple_neg(x: bits[2] id=2) -> bits[2] {
  ret x: bits[2] = param(name=x, id=2)
}
```

```ir
// 来自 ir_parser_round_trip_test_ParseTwoPlusTwo.ir —— 含两个 literal
fn two_plus_two() -> bits[32] {
  literal.1: bits[32] = literal(value=2, id=1)
  literal.2: bits[32] = literal(value=2, id=2)
  ret add.3: bits[32] = add(literal.1, literal.2, id=3)
}
```

2. 把文本里的值对应到 `Value`/`Bits`：
   - `value=2` → 一个 `Value`，其 `kind()` 为 `kBits`，内部 `Bits` 的 `bit_count()` 为 32，内容是 `...00000010`。
   - `bits[2]` → 一个 2 位宽的 `Type`；如果它也是个常量，对应的 `Bits` 只有 2 位。
3. 想象一个元组常量 `(bits[8]:1, bits[8]:2)` 会怎么存：一个 `kTuple` 的 `Value`，`elements()` 返回两个 `kBits` 子 `Value`。

**需要观察的现象**：`.ir` 文本里的字面量写法（如 `value=2`）省略了类型前缀，因为节点声明的 `bits[32]` 已经给出了类型；这与 [value.h:178-179](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/value.h#L178-L179) `ToHumanString` 的设计一致——「bit count is already known」。

**预期结果**：看到任意 `.ir` 字面量，你能立刻在脑中拆出「Type（形状）+ Value（内容，内部是 Bits）」。

#### 4.3.5 小练习与答案

**练习 1**：`Value::Bool(true)` 在内存里到底是什么？

> **答案**：见 [value.h:115-118](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/value.h#L115-L118)，它等价于 `Value(UBits(1, /*bit_count=*/1))`，即一个 `kBits` 的 `Value`，内部是位宽为 1、值为 1 的 `Bits`。在 XLS 里布尔就是 `bits[1]`，没有独立的 bool 类型。

**练习 2**：`Bits` 自身记录「有符号还是无符号」吗？`2 == 0b10` 在 `bits[2]` 里是无符号 2 还是有符号 -2？

> **答案**：不记录。`Bits` 只有「位宽」和「每位的 0/1」，符号性由**运算**决定（如 `kSMul` vs `kUMul`、`kSGe` vs `kUGe`）。`bits[2]` 内容 `10` 在无符号运算里是 2，在有符号运算里是 -2。这也是 [bits.h:73-78](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/bits.h#L73-L78) 同时提供 `MaxSigned`/`MinSigned` 的原因——同一组位，按不同解释得到不同「最值」。

**练习 3**：一个 `Value` 既能装 `Bits` 又能装 `vector<Value>`，源码用什么 C++ 机制实现的？

> **答案**：`std::variant`。见 [value.h:250](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/value.h#L250)，`payload_` 的类型是 `std::variant<std::nullptr_t, std::vector<Value>, Bits>`，配合 [value.h:141-148](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/value.h#L141-L148) 的 `IsBits()` 等查询方法做类型安全的取用。

---

## 5. 综合实践

**任务**：用 `ir_converter_main` 把一个简单 DSLX 函数转成 `.ir`，然后在产物里把本讲三大模块（Package / Function+Node / Value）全部标注出来，并解释节点间的数据流边。这是把本讲知识串起来的「端到端」练习。

**操作步骤**：

1. 新建一个最小 DSLX 文件 `/tmp/mini.x`（承接 u1-l5 的风格）：

```dslx
fn add_one(x: u8) -> u8 {
    x + u8:1
}
```

2. 用上一讲构建好的工具把它转成 IR（若尚未构建，参考 u1-l2）：

```bash
./bazel-bin/xls/dslx/ir_converter_main /tmp/mini.x
```

3. 在打印出的 `.ir` 文本里，逐项找出并标注：

   | 要找的东西 | 对应概念 | 在文本里的样子（示例） |
   |------------|----------|------------------------|
   | 顶层容器 | **Package** | `package <名字>` 这一行 |
   | 可计算单元 | **Function** | `fn add_one(x: bits[8] ...) -> bits[8] { ... }` |
   | 参数节点 | **Node**（op=`kParam`） | `x: bits[8] = param(...)` |
   | 常量节点 | **Node**（op=`kLiteral`） | `literal.X: bits[8] = literal(value=1)` |
   | 运算节点 | **Node**（op=`kAdd`） | `ret add.Y: bits[8] = add(x, literal.X)` |
   | 运行期值 | **Value/Bits** | `literal(value=1)` 里的 `1`，是一个位宽 8 的 `Bits` |

4. 描述数据流边：用一句话写出「`add` 节点的 **operands** 是哪两个节点」「这两个节点各自被谁 **use**」。

**需要观察的现象**：

- 产物以 `package ...` 开头（一个 `Package`）。
- 函数体每一行非空内容，都对应一个 `Node`；行首 `ret` 标记返回值节点。
- `add(...)` 括号里出现的名字，正是它的操作数（入边）；这些被引用的节点，users 里就包含这个 `add`。
- `literal(value=1)` 携带的 `1` 即一个 `Value`（内部是 8 位的 `Bits`），而节点声明的 `bits[8]` 是它的 `Type`。

**预期结果**（结构示意，具体节点名/id 以本地输出为准——id 由 [package.h:233](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/ir/package.h#L233) 的计数器分配，不同环境可能不同）：

```
package <包名>

fn add_one(x: bits[8] ...) -> bits[8] {
  //   └─ Param 节点（入参 x）
  literal.N: bits[8] = literal(value=1, ...)
  //   └─ Literal 节点，携带 Value=bits[8]:1
  ret add.M: bits[8] = add(x, literal.N, ...)
  //   └─ Add 节点；operands = {x, literal.N}；x 和 literal.N 的 users 都含 add.M
}
```

> 如果暂时无法本地构建，可改为「源码阅读型」完成：直接阅读仓库自带的 `xls/ir/testdata/ir_parser_round_trip_test_ParseFunction.ir` 与 `..._ParseTwoPlusTwo.ir`，按上表逐项标注，效果等同。具体节点编号「待本地验证」。

## 6. 本讲小结

- **XLS IR = 数据流图（sea of nodes）+ 类型化值**。它刻意不是 CFG，因为硬件天然并行；它具备 SSA 性质，每个值只定义一次。
- **Package 是顶层容器**：拥有 functions/procs/blocks、类型（经 `TypeManager`）、通道、文件号表和节点 id 计数器，并标记唯一的 top 实体；不可拷贝。
- **`FunctionBase` 才是「持有一堆 Node」的基类**，`Function`/`Proc`/`Block` 都继承自它；`Function` 额外多一个「返回值节点」。Node 用 `std::list` 存是为了增删时指针稳定。
- **Node 是图的顶点**，靠 `operands()`（入边）和 `users()`（出边）这对对称指针表达数据依赖；常见节点 ≤2 个操作数，故用 `InlinedVector<2>` 优化。每个 Node 带一个 `Op` 标签和产出 `Type`。
- **`Value` 是运行期值**，用 `std::variant` 装 `Bits` 或 `vector<Value>`，分 bits/元组/数组/token 四类；`Bits` 是任意位宽的紧凑二进制串，符号性由运算而非 `Bits` 自身决定。
- **Type vs Value** 是贯穿全讲的区分：Type 是编译期「形状」，Value 是运行期「内容」，二者由 `GetTypeForValue` 桥接。

## 7. 下一步学习建议

本讲建立的是 IR 的「静态骨架」。接下来建议：

1. **u3-l2 IR 运算符体系**：深入 `op.h` / `op_list.h` / `nodes.h`，把本讲只点到为止的 `Op` 枚举展开，看清每个 `Op` 对应哪个 `Node` 子类、有什么属性（结合律/交换律/副作用）。
2. **u3-l3 IR 文本格式**：本讲的 `.ir` 文本是用 `DumpIr` 生成的，下一讲讲它的反向——`ir_parser` 如何把文本**重建**成内存里的 `Package`，做到读写往返。
3. **u3-l5 Proc、Channel 与状态化通信**：本讲重点在 `Function`（组合、无状态）；想理解 XLS 如何表达时序状态，去看 `Proc` 与 `Channel`，那时 `FunctionBase` 作为共同基类的设计会再次凸显价值。

阅读源码时，建议把本讲的「源码地图」表留在手边——后面所有讲义都会反复回到 `package.h`、`function_base.h`、`node.h` 这三个文件。
