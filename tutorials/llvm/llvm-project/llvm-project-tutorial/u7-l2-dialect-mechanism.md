# Dialect 方言机制

## 1. 本讲目标

学完本讲，你应当能够：

- 说出 **Dialect（方言）** 到底封装了什么、它在 MLIR 的可扩展性里扮演什么角色。
- 读懂一个 Dialect 的两段式定义：用 TableGen/ODS 声明「有哪些 Operation / Type / Attribute」，再用 `initialize()` 把它们注册到 `Dialect` 对象上。
- 讲清 Dialect 的「注册（registry）」与「加载（load）」是两件事——前者只是把构造函数登记进 `DialectRegistry`，后者才由 `MLIRContext::getOrLoadDialect` 懒加载、并由 context 持有所有权。
- 认识 `arith`、`func`、`scf`、`linalg` 等常用内置方言各自的职责，以及它们如何串成一条「渐进式下降（Progressive Lowering）」的链条。

本讲是 u7-l1（MLIR 设计哲学与核心 IR）的直接延续。u7-l1 讲了「一切皆 Operation」与嵌套树，本讲回答：**这些 Operation 从哪里来、归谁管、何时进入 `MLIRContext`**——答案就是 Dialect。

## 2. 前置知识

- **Operation 与操作名**：回顾 u7-l1，每个 Operation 都有一个形如 `toy.constant` 的名字，点号 `.` 之前的部分就是它所属方言的 **namespace（命名空间）**。本讲的核心问题之一就是：这个 namespace 是怎么和一组 Operation 绑定的。
- **`MLIRContext`**：MLIR 的「世界」，所有 IR 对象（Operation、Type、Attribute）都在某个 context 内创建并唯一化，context 也负责管理已加载的方言。你可以把它类比成 u3 里 LLVM 的 `LLVMContext`。
- **TableGen / ODS**：MLIR 用一套声明式语言 ODS（Operation Definition Specification，本质是 TableGen 的方言）来描述 Operation/Type/Attribute/Dialect，再由 `mlir-tblgen` 在构建期生成大量 C++ 代码（`.inc` 片段）。这套「描述—生成」两段式与 u6-l5 讲的 LLVM TableGen 完全同构，只是这里用的是 `mlir-tblgen`、描述对象是 MLIR 的 IR。本讲只需理解「`.td` 描述 → `.inc` 生成 C++」这一因果，不必深究语法细节。
- **唯一化（uniquing）**：回顾 u3-l3，类型在 `LLVMContext` 内唯一化、判等只需比指针。MLIR 的 Type/Attribute 同样在 context 内唯一化，而唯一化表是**按方言**组织的。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `mlir/include/mlir/IR/Dialect.h` | `Dialect` 基类的定义：一个方言对象持有什么、提供哪些钩子（解析/打印/验证/接口）。 |
| `mlir/include/mlir/IR/DialectRegistry.h` | `DialectRegistry`：把「namespace → 构造函数」登记成一张表，解耦「可用」与「已加载」。 |
| `mlir/lib/IR/MLIRContext.cpp` | `MLIRContext::getOrLoadDialect` 的实现：方言的懒加载入口。 |
| `mlir/examples/toy/Ch2/include/toy/Ops.td` | Toy 方言的 ODS 描述：方言本身 + 一组 Operation 的声明。 |
| `mlir/examples/toy/Ch2/include/toy/Dialect.h` | Toy 方言的 C++ 头：引入 `mlir-tblgen` 生成的 `.inc`。 |
| `mlir/examples/toy/Ch2/mlir/Dialect.cpp` | Toy 方言的 C++ 实现：`ToyDialect::initialize()` 注册 Operation。 |
| `mlir/examples/toy/Ch2/toyc.cpp` | Toy 编译器入口：`context.getOrLoadDialect<ToyDialect>()` 真正加载方言。 |
| `mlir/lib/Dialect/Arith/IR/ArithDialect.cpp` | 内置 `arith` 方言的 `initialize()`，展示更完整的注册（操作/属性/接口）。 |
| `mlir/include/mlir/Dialect/Arith/IR/ArithBase.td` | `arith` 方言的 ODS 描述（含 `description`）。 |
| `mlir/lib/Dialect/Func/IR/CMakeLists.txt` | `func` 方言如何被构建成一个库并依赖 TableGen 生成产物。 |

## 4. 核心概念与源码讲解

本讲按三个最小模块推进：**4.1 Dialect 是什么**（定义）→ **4.2 注册与加载机制**（注册）→ **4.3 内置方言举例**（实战样本）。

### 4.1 Dialect 是什么：一组 Operation/Type/Attribute 的容器

#### 4.1.1 概念说明

在 u7-l1 里我们说过，MLIR 不规定「世界上有哪些 Operation」，具体 Operation 由**方言（Dialect）**定义。可以把方言理解成一个**命名空间下的 IR 组件包**：它圈定一组 Operation、Type（类型）、Attribute（属性），并为这整组对象提供公共行为——例如自定义的常量折叠钩子、汇编打印/解析钩子、验证钩子、以及一组方言级接口（interfaces）。

一个方言有一个**全局唯一的 namespace**。所有属于该方言的 Operation，其名字都以 `namespace.` 为前缀：`toy` 方言里的 constant 操作全名是 `toy.constant`，`arith` 方言里的加法是 `arith.addi`。这种「点号前缀 = 方言名」的约定，让解析器看到一个操作名就能立即定位它该交给哪个方言。

为什么要用方言这种粒度？因为它带来**可插拔的可扩展性**：

- 想支持一种新的高层抽象（比如神经网络计算图、多面体循环、GPU 启动核函数），就定义一个新方言，不必动 MLIR 核心。
- 不同方言可以共存于同一个 IR 里，再经「渐进式下降」逐步从高层方言转换（Lowering）到低层方言，最终落到 `llvm` 方言（与 LLVM IR 几乎一一对应）。
- 方言是天然的**编译单元边界**：一个方言可以单独编译成一个库（如 `MLIRFuncDialect`、`MLIRArithDialect`），按需链接。

#### 4.1.2 核心流程

一个方言对象的生命周期可以概括为四步：

1. **声明（ODS）**：在 `.td` 文件里用 `def Xxx_Dialect : Dialect { let name = "xxx"; ... }` 声明方言的名字、C++ 命名空间、描述、依赖的其他方言等。
2. **生成（tblgen）**：构建期 `mlir-tblgen` 把 ODS 描述编译成 C++ 片段 `XxxDialect.h.inc` / `XxxDialect.cpp.inc`，其中包含方言类（如 `ToyDialect`）的骨架与 `getDialectNamespace()` 等样板。
3. **注册（initialize）**：手写 `XxxDialect::initialize()`，在其中调用 `addOperations<>`、`addTypes<>`、`addAttributes<>`、`addInterfaces<>` 等，把具体的操作/类型/属性/接口登记到方言对象上。
4. **加载（load）**：通过 `MLIRContext::getOrLoadDialect<XxxDialect>()` 让 context 构造并拥有该方言对象；之后该方言的所有 Operation 才能被合法创建与解析。

第 1、3 步是「定义」，第 4 步是「加载」，二者由第 4.2 节讲的 `DialectRegistry` 解耦。本节先聚焦方言对象本身持有什么。

```
┌─────────────────────── Dialect 对象（由 MLIRContext 拥有）───────────────────────┐
│  name (namespace)        "toy"          ← getNamespace()                         │
│  dialectID               TypeID         ← getTypeID()，用于 isa<>/注册去重        │
│  context                 MLIRContext*   ← getContext()                           │
│  unknownOpsAllowed       bool           ← allowUnknownOperations()               │
│  registeredInterfaces    接口表          ← addInterfaces<>()                      │
│                                                                                   │
│  钩子（虚函数，派生类/手写 .cpp 可覆盖）:                                            │
│    parseType/printType, parseAttribute/printAttribute                              │
│    materializeConstant, getCanonicalizationPatterns                                │
│    verifyOperationAttribute, getParseOperationHook ...                             │
│                                                                                   │
│  注册入口（protected，在 initialize() 里调用）:                                       │
│    addOperations<...>(), addTypes<...>(), addAttributes<...>()                     │
└───────────────────────────────────────────────────────────────────────────────────┘
```

#### 4.1.3 源码精读

`Dialect` 基类定义在 [mlir/include/mlir/IR/Dialect.h:L38-L372](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/IR/Dialect.h#L38-L372)，文件顶部的注释一句话点明了方言的定位：

> Dialects are groups of MLIR operations, types and attributes, as well as behavior associated with the entire group.

方言对象的核心字段都是私有的（[Dialect.h:L342-L360](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/IR/Dialect.h#L342-L360)）：`name`（namespace）、`dialectID`（`TypeID`）、`context`（拥有它的 context 指针）、两个 `unknownXxxAllowed` 标志（是否允许「未注册」的操作/类型，用 `OpaqueType` 等兜底），以及一张接口表 `registeredInterfaces`。对应的访问器很朴素：

```cpp
// Dialect.h —— 方言的「身份证」三件套
MLIRContext *getContext() const { return context; }
StringRef getNamespace() const { return name; }
TypeID getTypeID() const { return dialectID; }
```
（[Dialect.h:L52-L57](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/IR/Dialect.h#L52-L57)）

注意构造函数是 `protected` 的（[Dialect.h:L272](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/IR/Dialect.h#L272)），说明你不能在用户代码里直接 `new Dialect(...)`，只能由派生类（通常是 `mlir-tblgen` 生成的 `XxxDialect`）调用；且拷贝/赋值被 `delete`（[Dialect.h:L323-L324](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/IR/Dialect.h#L323-L324)），方言对象由 context 独家拥有。

方言把「注册操作/类型/属性」做成 protected 模板方法，供派生类在自己的 `initialize()` 里调用。以注册操作为例：

```cpp
// Dialect.h —— 注册一组操作到本方言
template <typename... Args>
void addOperations() {
  (void)std::initializer_list<int>{
      0, (RegisteredOperationName::insert<Args>(*this), 0)...};
}
```
（[Dialect.h:L276-L284](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/IR/Dialect.h#L276-L284)）

这段用 `initializer_list` 做的可变参数展开，本质等价于对每个操作类型 `Args` 调一次 `RegisteredOperationName::insert<Args>(*this)`——也就是把「这个操作的元信息（名字、trait、接口等）」登记到 context 的全局操作注册表里，并标注它属于本方言。`addTypes<>`（[Dialect.h:L287-L294](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/IR/Dialect.h#L287-L294)）、`addAttributes<>`（[Dialect.h:L302-L309](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/IR/Dialect.h#L302-L309)）同理，会把类型/属性同时登记到方言并向 context 的唯一化器注册。

除了「装什么」，方言还提供一整套**钩子**来定义「整组对象的公共行为」。最常用的几类：

- **自定义汇编**：`parseType/printType`（[Dialect.h:L104-L109](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/IR/Dialect.h#L104-L109)）、`parseAttribute/printAttribute`（[Dialect.h:L94-L101](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/IR/Dialect.h#L94-L101)）、`getParseOperationHook/getOperationPrinter`（[Dialect.h:L115-L122](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/IR/Dialect.h#L115-L122)）——当文本 IR 里出现 `!toy.xxx` 这样的方言类型时，解析器就回调本方言的 `parseType`。
- **常量物化**：`materializeConstant`（[Dialect.h:L83-L86](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/IR/Dialect.h#L83-L86)）——告诉框架「本方言的常量长什么样」，这是模式重写器把属性物化成 Operation 时的回调。
- **规范化模式**：`getCanonicalizationPatterns`（[Dialect.h:L74](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/IR/Dialect.h#L74)）——注册「不属于任何单个操作」的方言级化简规则（典型如接口的化简）。
- **验证**：`verifyOperationAttribute` 等（[Dialect.h:L132-L150](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/IR/Dialect.h#L132-L150)）——校验挂在操作上的本方言属性是否合法。
- **接口**：`addInterface/addInterfaces`（[Dialect.h:L192-L204](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/IR/Dialect.h#L192-L204)）给方言挂上「方言级接口」（如内联接口 `DialectInlinerInterface`）。

最后，文件末尾 `namespace llvm` 里的一组 `isa_impl` / `cast_convert_val` 特化（[Dialect.h:L376-L425](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/IR/Dialect.h#L376-L425)）让 MLIR 的 `isa<ArithDialect>` / `dyn_cast` 能直接作用在 `Dialect&` 上——这正是靠 `TypeID` 比较实现的，与 u3-l2 里 LLVM 用 `SubclassID` 支撑 `isa<>` 的思路一致。

#### 4.1.4 代码实践

**目标**：从源码层面确认「一个方言对象 = 身份字段 + 一组钩子 + 一组注册方法」，并理解 namespace 与操作名前缀的关系。

**操作步骤**：

1. 打开 [Dialect.h:L38-L50](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/IR/Dialect.h#L38-L50)，阅读类注释，用一句话写下「方言封装了哪些东西」。
2. 跳到 [Dialect.h:L342-L360](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/IR/Dialect.h#L342-L360)，列出方言对象的全部私有字段。
3. 注意构造函数对 namespace 的要求（注释在 [Dialect.h:L265-L272](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/IR/Dialect.h#L265-L272)）：namespace **不能含 `.`**，且本方言所有操作名必须以 `namespace.` 开头。

**需要观察的现象**：方言类本身没有存储「操作列表」的字段——操作是通过 `addOperations` 登记到 context 的全局 `RegisteredOperationName` 表里的，方言只持有身份与钩子。

**预期结果**：你能解释为什么看到 `toy.constant` 时，解析器能凭 `toy` 这个前缀找到 `ToyDialect`、再凭 `constant` 找到具体操作。

#### 4.1.5 小练习与答案

**练习 1**：`Dialect` 类的析构函数是 `virtual`（[Dialect.h:L46](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/IR/Dialect.h#L46)），为什么？

> **答**：因为方言总是以多态方式使用——外部持有的总是 `Dialect*` 或 `Dialect&`，而实际对象是 `ToyDialect`、`ArithDialect` 等派生类。虚析构保证 context 删除方言对象时调用的是派生类的析构函数，不会对象切片。

**练习 2**：`allowsUnknownOperations()`（[Dialect.h:L62](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/IR/Dialect.h#L62)）允许什么？给一个使用场景。

> **答**：允许「带本方言前缀、但未通过 `addOperations` 注册」的操作存在。典型场景是工具想原样读入并打印某个尚不认识的方言 IR（例如把 `tf.Add` 当作不透明操作透传），不因缺定义而报错。

---

### 4.2 Dialect 的注册与加载：DialectRegistry 与 getOrLoadDialect

#### 4.2.1 概念说明

上一节我们看到，方言对象要被「构造 + initialize」之后才能用。但 MLIR 在这里做了一个关键设计：**把「方言可用」和「方言已加载」分离开**。

- **注册（register）**：把「namespace → 构造函数」登记进一张表 `DialectRegistry`。这一步**不构造任何方言对象**，只是声明「我的程序知道怎么造这个方言」。
- **加载（load）**：当某个方言真的被需要时（显式调用 `getOrLoadDialect`，或解析器遇到该方言的操作时），`MLIRContext` 才调用构造函数造出方言对象、跑 `initialize()`，并由 context 独家持有所有权。之后该方言就叫「已加载（loaded）」。

为什么要这么分？因为 MLIR 的方言非常多（核心就有几十个），一个具体工具通常只用其中几个。如果一开始就把所有方言全加载，既慢又浪费；而且**解析 IR 时应当能按需懒加载**——读到 `toy.constant` 才去加载 `toy` 方言。`DialectRegistry` 充当「可用方言目录」，`MLIRContext` 充当「已加载方言的拥有者」，二者解耦让这一切成为可能。

一个常被混淆的点：`MLIRContext` 内部自带一个 registry（构造时自动注册 `builtin` 等核心方言）。多数程序要么直接 `getOrLoadDialect`（依赖内置 registry），要么先建一个 `DialectRegistry`、`insert` 一堆方言、再交给 context。

#### 4.2.2 核心流程

```
        ┌─────────── DialectRegistry（目录：namespace → allocator）───────────┐
        │  insert<ToyDialect>()   ──登记──▶  "toy"  → []{ ctx->getOrLoadDialect<ToyDialect>(); }
        │  insert<ArithDialect>()            "arith"→ ...
        │  （此时没有任何方言对象被创建）                                          │
        └──────────────────────────────────────────────────────────────────────┘
                                    │  appendTo(context 的 registry) 或 context 自带
                                    ▼
   getOrLoadDialect<ToyDialect>()  ──▶  MLIRContext 查 loadedDialects：
        ┌─ 未加载 ─→ 调 allocator → ctor() 构造 ToyDialect → 跑 initialize()
        │            → context 用 unique_ptr 拥有 → applyExtensions() → 返回指针
        └─ 已加载 ─→ 直接返回已有指针（并用 TypeID 校验不是同名异方言）
```

要点：

1. `DialectRegistry::insert<T>()` 登记的 allocator 本质就是一个 lambda，它会去调用 `ctx->getOrLoadDialect<T>()`——这保证了 context 接管所有权、并触发延迟接口注册（见 [DialectRegistry.h:L162-L171](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/IR/DialectRegistry.h#L162-L171) 的注释）。
2. `getOrLoadDialect` 用 `loadedDialects` 这张 `namespace → unique_ptr<Dialect>` 的表保证「每个 namespace 在一个 context 里至多加载一次」。
3. 同一方言重复 `getOrLoadDialect` 是幂等的（返回同一对象），但若用**相同 namespace、不同 TypeID** 再次加载会 fatal error。

#### 4.2.3 源码精读

**`DialectRegistry`** 定义在 [DialectRegistry.h:L150-L310](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/IR/DialectRegistry.h#L150-L310)，类注释把它的定位说得非常清楚：

> The DialectRegistry maps a dialect namespace to a constructor for the matching dialect. This allows for decoupling the list of dialects "available" from the dialects loaded in the Context. The parser in particular will lazily load dialects in the Context as operations are encountered.

核心成员是 `MapTy registry`（[DialectRegistry.h:L151-L153](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/IR/DialectRegistry.h#L151-L153)）：一张 `namespace → (TypeID, allocator)` 的有序表。`insert` 的模板版本如下：

```cpp
// DialectRegistry.h —— 登记一个方言的构造方式（注意：尚未构造）
template <typename ConcreteDialect>
void insert() {
  insert(TypeID::get<ConcreteDialect>(),
         ConcreteDialect::getDialectNamespace(),
         static_cast<DialectAllocatorFunction>(([](MLIRContext *ctx) {
           // Just allocate the dialect, the context takes ownership of it.
           return ctx->getOrLoadDialect<ConcreteDialect>();
         })));
}
```
（[DialectRegistry.h:L162-L171](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/IR/DialectRegistry.h#L162-L171)）

注意三件事：登记的 key 是 `ConcreteDialect::getDialectNamespace()`（由 ODS 生成的字符串，如 `"toy"`）；allocator 是个 lambda，真正执行时回调 `getOrLoadDialect`；注释明说「context 接管所有权」。

**`getOrLoadDialect` 的实现**在 [MLIRContext.cpp:L481-L536](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/lib/IR/MLIRContext.cpp#L481-L536)，逻辑高度精炼：

```cpp
auto dialectIt = impl.loadedDialects.try_emplace(dialectNamespace, nullptr);
if (dialectIt.second) {                       // key 不存在 → 首次加载
  // ...
  std::unique_ptr<Dialect> &dialectOwned =
      impl.loadedDialects[dialectNamespace] = ctor();   // 构造（含 initialize）
  Dialect *dialect = dialectOwned.get();
  // ...刷新已存在的引用了本 namespace 的字符串属性...
  impl.dialectsRegistry.applyExtensions(dialect);       // 应用方言扩展
  return dialect;
}
// key 已存在 → 校验 TypeID 一致后返回已有对象
if (dialect->getTypeID() != dialectID)
  llvm::report_fatal_error("a dialect with namespace '" + dialectNamespace +
                           "' has already been registered");
return dialect.get();
```

关键细节：

- `try_emplace` 返回的 `second==true` 表示这是首次加载，于是调用 `ctor()`——而 `ctor` 最终会构造 `ToyDialect`、其构造函数会触发 `initialize()`（见 4.3 节 Toy 的 `initialize`）。构造出的对象用 `unique_ptr` 存进 `loadedDialects`，**这就是 context 拥有方言对象的地方**。
- 注意 [MLIRContext.cpp:L497-L502](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/lib/IR/MLIRContext.cpp#L497-L502) 的注释：先把表项置 `nullptr` 再调 `ctor()`，是为了处理「方言 A 的 initialize 里又触发加载方言 B」这种**递归加载**导致表 rehash 的情况。
- [MLIRContext.cpp:L516-L517](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/lib/IR/MLIRContext.cpp#L516-L517) 的 `applyExtensions` 会在新方言加载后立刻应用所有「方言扩展（DialectExtension）」——这是一种「当若干方言同时加载时才挂载额外功能」的机制（定义在同文件 [DialectRegistry.h:L45-L101](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/IR/DialectRegistry.h#L45-L101)）。
- Debug 模式下（[MLIRContext.cpp:L489-L496](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/lib/IR/MLIRContext.cpp#L489-L496)），如果在多线程 PassManager 执行期间首次加载方言，会直接 fatal error——提示你该方言应当被声明为 pass 的 `dependentDialects`，提前在单线程阶段加载好。

**一个真实的加载点**：Toy 编译器在生成任何 IR 之前，先加载自己的方言：

```cpp
// toyc.cpp —— dumpMLIR() 的开头
mlir::MLIRContext context;
// Load our Dialect in this MLIR Context.
context.getOrLoadDialect<mlir::toy::ToyDialect>();
```
（[mlir/examples/toy/Ch2/toyc.cpp:L76-L78](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/examples/toy/Ch2/toyc.cpp#L76-L78)）

这里没有显式 `DialectRegistry`，是因为 `MLIRContext` 内置的 registry 已能识别 `ToyDialect`（它由 `ToyDialect::getDialectNamespace()` 自报家门）。这行代码一执行，`ToyDialect` 对象就被构造、`initialize()` 被调用、所有权归 `context`。

#### 4.2.4 代码实践

**目标**：在源码里把「注册」与「加载」这两件事看得清清楚楚，理解它们何时发生。

**操作步骤**：

1. 读 [DialectRegistry.h:L137-L171](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/IR/DialectRegistry.h#L137-L171)，确认 `insert<>()` 只是登记、不构造。
2. 读 [MLIRContext.cpp:L481-L536](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/lib/IR/MLIRContext.cpp#L481-L536)，圈出「首次加载」分支里真正构造方言对象的那一行（`ctor()`）。
3. 读 [toyc.cpp:L76-L78](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/examples/toy/Ch2/toyc.cpp#L76-L78)，注意它在构造 `MLIRContext` 之后、生成任何 IR 之前就加载了 `ToyDialect`。

**需要观察的现象**：如果你在 `ToyDialect::initialize()`（见 4.3.3）里加一行日志，再运行 `toyc-ch2`，会发现日志只在第一次需要 Toy IR 时打印一次——即使后续多次创建 IR。

**预期结果**：你能向别人解释「为什么 MLIR 工具启动后不会自动加载所有方言，而是在用到时才加载」，并说出 `getOrLoadDialect` 的「幂等」性质来自哪张表。

**待本地验证**：实际加日志运行需要先按 u1-l3 构建 MLIR（含 `-DMLIR_ENABLE_EXAMPLES=ON`），得到 `toyc-ch2` 可执行文件。

#### 4.2.5 小练习与答案

**练习 1**：`DialectRegistry` 为什么不直接存 `Dialect` 对象，而存「构造函数」？

> **答**：因为方言对象**绑定到具体的 `MLIRContext`**（构造时要传 context 指针，且其类型/属性唯一化表归 context 管）。一个 registry 可能被多个 context 共享（经 `appendTo` 拷贝），所以它只能存「如何造」，由每个 context 在加载时各自造出属于自己的方言对象。

**练习 2**：在 Debug 构建下，若一个 Pass 的执行期间首次加载某方言会怎样？应如何避免？

> **答**：会触发 fatal error（[MLIRContext.cpp:L489-L496](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/lib/IR/MLIRContext.cpp#L489-L496)）。避免办法是在该 Pass 的 `getDependentDialects()` 里声明依赖方言，让 PassManager 在单线程配置阶段就提前加载好。

---

### 4.3 内置方言举例：从 Toy 到 arith/func

#### 4.3.1 概念说明

学了机制，我们来看「一个方言到底长什么样」。MLIR 自带一大批**内置方言（builtin dialects）**，每个负责一个抽象层级或一类功能，它们互相配合构成下降链：

| 方言 namespace | 职责 | 典型操作 |
| --- | --- | --- |
| `builtin` | 核心内置（模块、函数属性等），由 context 自动加载 | `builtin.module`, `builtin.func`(历史) |
| `func` | 通用的函数抽象：定义、调用、返回 | `func.func`, `func.call`, `func.return` |
| `arith` | 基础整型/浮点算术与比较、类型转换 | `arith.addi`, `arith.mulf`, `arith.constant`, `arith.cmpi` |
| `memref` | 内存缓冲区（带形状与布局的内存视图） | `memref.alloc`, `memref.load`, `memref.store` |
| `tensor` / `vector` | 值语义张量 / SIMD 向量 | `tensor.extract`, `vector.add` |
| `scf` | 结构化控制流（for/if/while） | `scf.for`, `scf.if`, `scf.yield` |
| `affine` | 多面体循环与映射 | `affine.for`, `affine.load` |
| `linalg` | 声明式线性代数（矩阵乘、卷积…） | `linalg.matmul`, `linalg.generic` |
| `llvm` | 与 LLVM IR 几乎一一对应的低层方言 | `llvm.add`, `llvm.alloca` |

这些方言之间存在典型的下降路径：高层语言先变成自己的方言（如 `toy`），再降到 `affine`/`scf`+`memref`，再到 `llvm` 方言，最后 `mlir-translate` 成 LLVM IR 交给 LLVM 后端（见 u7-l4）。这种「一层一层换方言」正是 u7-l1 提到的**渐进式下降**。

除了内置方言，Toy 教程展示了一个**自定义方言**的最小骨架——它和内置方言在机制上完全一样，只是规模小、且重用了 builtin 的张量类型。

#### 4.3.2 核心流程

定义一个方言，需要同时维护「描述（`.td`）」「生成的 C++ 骨架（`.inc`）」「手写的注册逻辑（`initialize`）」三处，并用 CMake 把 TableGen 生成串进构建：

```
        Ops.td                                  （人写：声明方言 + 操作）
         │ mlir-tblgen -gen-op-decls/-defs
         ▼
  Ops.h.inc / Ops.cpp.inc / Dialect.h.inc        （机器生成：ToyDialect 骨架、操作类）
         │
         ├── Dialect.h      #include "...Dialect.h.inc" + GET_OP_CLASSES / Ops.h.inc
         ├── Dialect.cpp    #include "...Dialect.cpp.inc"  ← 提供 ToyDialect::initialize 的「声明侧」
         │                  ToyDialect::initialize() { addOperations<GET_OP_LIST from Ops.cpp.inc>(); }
         └── CMakeLists     add_toy_chapter(... DEPENDS ToyCh2OpsIncGen ...)  ← 触发 tblgen
```

`initialize()` 是把「方言」和「它有哪些操作」真正拴起来的唯一手写入口。

#### 4.3.3 源码精读

**（a）ODS 描述方言本身。** Toy 的 [Ops.td:L23-L26](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/examples/toy/Ch2/include/toy/Ops.td#L23-L26) 用四行声明了一个方言：

```td
def Toy_Dialect : Dialect {
  let name = "toy";
  let cppNamespace = "::mlir::toy";
}
```

`name` 就是 namespace（操作名前缀 `toy.`）；`cppNamespace` 是生成 C++ 类所在的命名空间。`mlir-tblgen` 据此生成 `ToyDialect` 类，自动提供 `getDialectNamespace()` 返回 `"toy"`。对照内置方言 `arith` 的 [ArithBase.td:L15-L17](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/Dialect/Arith/IR/ArithBase.td#L15-L17)，结构完全一样，只是多了一个 `let description = [{ ... }]` 字段——这就是你能在文档里看到的方言说明文本的来源。

**（b）ODS 描述操作，并通过基类归到方言。** [Ops.td:L33-L34](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/examples/toy/Ch2/include/toy/Ops.td#L33-L34) 定义了一个操作的公共基类，把「父方言」写死成 `Toy_Dialect`：

```td
class Toy_Op<string mnemonic, list<Trait> traits = []> :
    Op<Toy_Dialect, mnemonic, traits>;
```

此后每条 `def XxxOp : Toy_Op<"xxx">`（如 [ConstantOp](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/examples/toy/Ch2/include/toy/Ops.td#L48)、[AddOp](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/examples/toy/Ch2/include/toy/Ops.td#L92)、[MulOp](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/examples/toy/Ch2/include/toy/Ops.td#L209)、[FuncOp](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/examples/toy/Ch2/include/toy/Ops.td#L115)、[GenericCallOp](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/examples/toy/Ch2/include/toy/Ops.td#L170)、[PrintOp](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/examples/toy/Ch2/include/toy/Ops.td#L232)、[ReshapeOp](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/examples/toy/Ch2/include/toy/Ops.td#L249)、[ReturnOp](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/examples/toy/Ch2/include/toy/Ops.td#L274)、[TransposeOp](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/examples/toy/Ch2/include/toy/Ops.td#L316)）都自动属于 `toy` 方言。

**关于「类型」的一个重要事实**：Ch2 的 Toy 方言**没有定义任何自定义类型**。你在 Ops.td 里看到的 `F64Tensor`、`F64ElementsAttr` 不是新类型，而是 ODS 的**类型/属性约束**，分别来自 MLIR 公共约束库 [CommonTypeConstraints.td:L821](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/IR/CommonTypeConstraints.td#L821)（`def F64Tensor : TensorOf<[F64]>;`，即「元素为 f64 的张量」）和 [CommonAttrConstraints.td:L535](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/IR/CommonAttrConstraints.td#L535)。也就是说，Toy 复用了 `builtin` 方言的张量类型，只在 ODS 里约束「我的操作数/结果必须是 f64 张量」。自定义方言类型（如 Toy 后续章节的 `StructType`）需要走 `addTypes<>` 注册，本讲先不展开。

**（c）生成的 C++ 骨架与手写的 initialize。** [Dialect.h:L26-L31](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/examples/toy/Ch2/include/toy/Dialect.h#L26-L31) 引入了两个生成文件：`Dialect.h.inc`（`ToyDialect` 类声明）和 `Ops.h.inc`（各操作类声明）。对应的实现侧 [Dialect.cpp:L35](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/examples/toy/Ch2/mlir/Dialect.cpp#L35) 引入 `Dialect.cpp.inc`，并在其后手写唯一的注册逻辑：

```cpp
// Dialect.cpp —— ToyDialect 的注册入口：把所有操作登记到本方言
void ToyDialect::initialize() {
  addOperations<
#define GET_OP_LIST
#include "toy/Ops.cpp.inc"
      >();
}
```
（[Dialect.cpp:L43-L48](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/examples/toy/Ch2/mlir/Dialect.cpp#L43-L48)）

`GET_OP_LIST` 是 `mlir-tblgen` 从 Ops.td 里所有操作展开出的「类型列表」宏，`#include "toy/Ops.cpp.inc"` 时被替换成形如 `<ConstantOp, AddOp, MulOp, ...>` 的实参传给 `addOperations`（即 4.1.3 讲过的 `RegisteredOperationName::insert`）。文件末尾 [Dialect.cpp:L321-L322](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/examples/toy/Ch2/mlir/Dialect.cpp#L321-L322) 再用 `GET_OP_CLASSES` 引入这些操作类的成员定义。

**（d）一个更完整的内置方言：arith。** 对照 Toy 的极简 `initialize`，`arith` 的注册展示了方言能挂载的全部内容（[ArithDialect.cpp:L42-L62](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/lib/Dialect/Arith/IR/ArithDialect.cpp#L42-L62)）：

```cpp
void arith::ArithDialect::initialize() {
  addOperations<  #define GET_OP_LIST ...>();     // 注册所有算术操作
  addAttributes<  #define GET_ATTRDEF_LIST ...>(); // 注册方言属性（如整数比较谓词）
  addInterfaces<ArithInlinerInterface>();          // 注册方言级内联接口
  declarePromisedInterface<ConvertToLLVMPatternInterface, ArithDialect>(); // 声明「将来会实现」的接口
  ...
}
```

它做了 Toy 没做的事：注册**方言属性**（`addAttributes`，[Dialect.h:L302](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/IR/Dialect.h#L302)）、注册**方言接口**（`addInterfaces`，[Dialect.h:L195](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/IR/Dialect.h#L195)），并用 `declarePromisedInterface`（[Dialect.h:L210](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/IR/Dialect.h#L210)）声明「我会实现到 LLVM 的转换接口」——这是一种「接口实现推迟到方言扩展加载时再挂」的前向承诺（若用前未实现，[Dialect.h:L228-L240](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/IR/Dialect.h#L228-L240) 会 fatal error 提示）。`arith` 还实现了 `materializeConstant`（[ArithDialect.cpp:L65-L72](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/lib/Dialect/Arith/IR/ArithDialect.cpp#L65-L72)），告诉框架本方言的常量是 `arith.constant`，这样新建常量时能自动落到正确操作上。

**（e）方言的构建：以 func 为例。** [Func/IR/CMakeLists.txt](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/lib/Dialect/Func/IR/CMakeLists.txt) 用 `add_mlir_dialect_library(MLIRFuncDialect ...)` 把 `func` 方言打包成独立库 `MLIRFuncDialect`，关键是 `DEPENDS MLIRFuncOpsIncGen`（触发 `mlir-tblgen` 生成 `.inc`）和一串 `LINK_LIBS PUBLIC`（声明它依赖 `MLIRFunctionInterfaces`、`MLIRCallInterfaces` 等接口库）。`func` 的 `initialize`（[FuncOps.cpp:L37-L47](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/lib/Dialect/Func/IR/FuncOps.cpp#L37-L47)）注册 `func.func`/`func.call`/`func.return` 等操作并声明若干接口承诺——它被几乎所有 MLIR 程序当作「函数抽象」复用。

#### 4.3.4 代码实践

**目标**：通读 Toy Ch2 的方言定义，列出它声明的全部 Operation，并确认它复用了 builtin 的张量类型。这是本讲的核心实践。

**操作步骤**：

1. 打开 [Ops.td:L23-L26](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/examples/toy/Ch2/include/toy/Ops.td#L23-L26)，记下方言 namespace 与 C++ 命名空间。
2. 通读 [Ops.td:L36-L333](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/examples/toy/Ch2/include/toy/Ops.td#L36-L333)，把每个 `def XxxOp : Toy_Op<"mnemonic">` 的「类名 + mnemonic」抄成一张表（见预期结果）。
3. 对每个操作，观察它的 `let arguments = (ins ...)` 和 `let results = (outs ...)` 用的类型约束。点开 [CommonTypeConstraints.td:L821](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/IR/CommonTypeConstraints.td#L821)，确认 `F64Tensor` 就是 `tensor<...xf64>` 这类 builtin 类型。
4. 打开 [Dialect.cpp:L43-L48](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/examples/toy/Ch2/mlir/Dialect.cpp#L43-L48)，确认这些操作是在 `initialize()` 里经 `addOperations<GET_OP_LIST>` 一次性注册的。

**需要观察的现象**：Toy Ch2 的操作结果类型在生成的 IR 里会显示成 `tensor<*xf64>`（无秩张量，见 [Dialect.cpp:L178](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/examples/toy/Ch2/mlir/Dialect.cpp#L178)、[L196](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/examples/toy/Ch2/mlir/Dialect.cpp#L196)、[L244](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/examples/toy/Ch2/mlir/Dialect.cpp#L244) 的 `UnrankedTensorType::get(builder.getF64Type())`）或 `tensor<f64>`（[ConstantOp::build](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/examples/toy/Ch2/mlir/Dialect.cpp#L109-L114)）——这些都是 builtin 类型，证明 Toy 方言自己没有引入新类型。

**预期结果**：Toy Ch2 声明的操作清单如下（共 9 个）：

| 类名 | mnemonic | 全名 | 说明 |
| --- | --- | --- | --- |
| `ConstantOp` | `constant` | `toy.constant` | 字面量转 SSA 值 |
| `AddOp` | `add` | `toy.add` | 逐元素加 |
| `MulOp` | `mul` | `toy.mul` | 逐元素乘 |
| `TransposeOp` | `transpose` | `toy.transpose` | 转置 |
| `ReshapeOp` | `reshape` | `toy.reshape` | 改形状 |
| `GenericCallOp` | `generic_call` | `toy.generic_call` | 调用用户函数 |
| `FuncOp` | `func` | `toy.func` | 定义函数（带函数体 region） |
| `ReturnOp` | `return` | `toy.return` | 函数返回（终结符） |
| `PrintOp` | `print` | `toy.print` | 打印张量 |

而「类型」一项的结论是：Ch2 未定义自定义方言类型，全部复用 builtin 的 `tensor` 类型，仅在 ODS 层用 `F64Tensor` 约束。

#### 4.3.5 小练习与答案

**练习 1**：如果要把 Toy 改成支持复数张量（`!toy.complex_tensor`）这样的**自定义类型**，需要在哪些地方动手？

> **答**：三处——(1) 在 `.td` 用 `def Toy_ComplexTensor_Type : DialectType<...>` 声明类型；(2) 写一个 C++ 类型类（继承 `Type::TypeBase`）并在 `initialize()` 里用 `addTypes<>` 注册；(3) 实现 `parseType/printType` 钩子，让 `!toy.complex_tensor` 能在文本 IR 里被解析与打印。（Ch2 不涉及，留待学习 Toy 后续章节。）

**练习 2**：`arith` 的 `declarePromisedInterface<ConvertToLLVMPatternInterface, ArithDialect>()`（[ArithDialect.cpp:L53](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/lib/Dialect/Arith/IR/ArithDialect.cpp#L53)）「承诺」了什么？为什么不直接 `addInterfaces`？

> **答**：它承诺「arith 方言将来会实现 `ConvertToLLVMPatternInterface` 接口」，但这个接口的真正实现在一个**方言扩展（dialect extension）**里，要等扩展加载后才挂上。不直接 `addInterfaces` 是为了避免在 `arith` 核心库里硬依赖转换库；承诺机制让框架在「接口被查询但扩展还没加载」时给出明确的 fatal error，而不是静默返回「不支持」（见 [Dialect.h:L228-L240](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/IR/Dialect.h#L228-L240)）。

## 5. 综合实践

**任务**：以 Toy Ch2 为样本，亲手追踪「一个方言从被声明到被加载」的完整路径，并把它画成一张时序图。

**步骤**：

1. **声明层**：阅读 [Ops.td:L23-L34](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/examples/toy/Ch2/include/toy/Ops.td#L23-L34)，写明 `Toy_Dialect` 的 namespace、C++ 命名空间，以及 `Toy_Op` 基类如何把操作绑定到 `Toy_Dialect`。
2. **生成层**：阅读 [Dialect.h:L26-L31](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/examples/toy/Ch2/include/toy/Dialect.h#L26-L31) 与 [Dialect.cpp:L35](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/examples/toy/Ch2/mlir/Dialect.cpp#L35)、[L321-L322](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/examples/toy/Ch2/mlir/Dialect.cpp#L321-L322)，标注哪些内容来自 `mlir-tblgen` 生成的 `.inc`、哪些是手写。
3. **注册层**：阅读 [Dialect.cpp:L43-L48](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/examples/toy/Ch2/mlir/Dialect.cpp#L43-L48)，说明 `initialize()` 把哪些操作注册进了方言。
4. **加载层**：阅读 [toyc.cpp:L76-L78](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/examples/toy/Ch2/toyc.cpp#L76-L78) 与 [MLIRContext.cpp:L481-L518](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/lib/IR/MLIRContext.cpp#L481-L518)，描述 `getOrLoadDialect<ToyDialect>()` 触发构造、`initialize`、所有权转移的全过程。
5. **产出**：画一张时序图，横轴是时间，画出 `Ops.td → tblgen → .inc → ToyDialect::initialize → getOrLoadDialect → MLIRContext.loadedDialects` 这条链，并在每个节点标注「这一步发生在构建期还是运行期」。

**自检问题**：如果有人问「`toy.constant` 这个名字里的 `toy` 是在源码的哪一行、由谁绑定的？」，你能准确回答吗？（提示：起点是 Ops.td 的 `let name = "toy"`，终点是解析 IR 时按 `.` 前缀在 `loadedDialects` 里查到 `ToyDialect`。）

**待本地验证**：若已构建 MLIR 并启用 examples（`-DMLIR_ENABLE_EXAMPLES=ON`），可用一段 `.toy` 源码运行 `toyc-ch2 -emit=mlir input.toy`，观察输出的 MLIR 中所有操作都带 `toy.` 前缀、类型都是 `tensor<...xf64>`，从而印证本讲的结论。

## 6. 本讲小结

- **Dialect 是 MLIR 可扩展性的基本单元**：它在一个 namespace 下封装一组 Operation/Type/Attribute，并配套提供解析、打印、验证、常量物化、规范化、接口等「整组公共行为」。`toy.constant` 的 `toy` 就是方言 namespace。
- **`Dialect` 类**持身份证三件套（`name`/`dialectID`/`context`）与一组钩子；具体的操作/类型/属性通过 protected 的 `addOperations`/`addTypes`/`addAttributes` 在 `initialize()` 里登记到 context 的全局表，方言对象由 `MLIRContext` 独家拥有。
- **定义一个方言 = 三处协同**：`.td`（ODS 声明）→ `mlir-tblgen` 生成 `.inc`（C++ 骨架）→ 手写 `initialize()`（注册）。
- **注册 ≠ 加载**：`DialectRegistry` 只登记「namespace → 构造函数」（可用目录），`MLIRContext::getOrLoadDialect` 才在首次需要时懒加载并拥有方言对象；解析器据此按需加载，工具不必启动即全载。
- **内置方言各司其职、串成下降链**：`func` 管函数、`arith` 管算术、`memref`/`tensor`/`vector` 管数据容器、`scf`/`affine` 管循环、`linalg` 管声明式计算、`llvm` 是通往 LLVM IR 的出口；自定义方言（如 `toy`）机制与它们完全一致，只是规模更小、且常复用 builtin 类型。

## 7. 下一步学习建议

- **继续 MLIR 单元**：本讲只讲了「方言如何被定义和加载」。下一讲 **u7-l3（Pass、Pattern 与 Conversion）** 会讲方言内部的变换机制——如何用 `Pass` 框架、`RewritePattern` 改写 Operation，以及如何把一个方言「下降（Lowering / Conversion）」成另一个方言（这正是 `arith` 里 `declarePromisedInterface<ConvertToLLVMPatternInterface>` 承诺要实现的东西）。
- **看 Toy 教程的后续章节**：Ch3（[ToyCombine.cpp](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/examples/toy/Ch3/mlir/ToyCombine.cpp)）展示在 Toy 方言上做模式优化；Ch4 之后引入自定义类型，补上本讲特意留空的「方言自定义类型」一环。
- **横向对照**：u6-l5 讲的是 **LLVM** 的 TableGen（描述目标后端），本讲讲的是 **MLIR** 的 ODS（描述方言 IR），二者「描述—生成」理念同构，对照阅读能加深对 TableGen 这套设计哲学的理解。
- **深入加载机制**：若对「方言扩展（DialectExtension）」「promised interface」感兴趣，可精读 [DialectRegistry.h:L45-L101](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/IR/DialectRegistry.h#L45-L101) 与 [Dialect.h:L206-L262](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/mlir/include/mlir/IR/Dialect.h#L206-L262)，它们是大规模 MLIR 工程（如多方言协同转换）的关键设施。
