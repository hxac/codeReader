# 类型系统与 Value

## 1. 本讲目标

本讲承接 [u2-l1 IR 层次结构](u2-l1-ir-hierarchy.md)，把视角从「IR 的树状结构」下沉到这棵树里**每一个节点到底是什么东西**。

读完本讲，你应该能够：

- 说清 **`Type`** 是什么、为什么它「不可变且全局唯一」，以及如何用静态工厂方法拿到整型、指针、向量、函数等类型；
- 说清 **`Value`** 作为「所有可被引用对象的基类」意味着什么，以及 `Value` / `User` / `Use` 三者的分工与连接方式；
- 区分 **`Constant`** 体系，理解 `ConstantInt`、`ConstantFP` 的底层存储（`APInt` / `APFloat`）以及常量的「按结构等价共享」特性；
- 对照源码，画出 `Type` 与 `Value` 两棵继承树，并准确标注 `IntegerType` 与 `ConstantInt` 的位置。

本讲对应大纲中的最小模块：**Type 体系与整型/指针/向量/函数类型**、**Value / User / Use 关系**、**Constant 与 ConstantInt / ConstantFP**。

---

## 2. 前置知识

### 2.1 回顾：IR 的四层结构与归属

在 [u2-l1](u2-l1-ir-hierarchy.md) 中我们建立了 `Module → Function → BasicBlock → Instruction` 的树状归属模型，并知道 `Function`、`BasicBlock`、`Instruction` 都是 `Value`。本讲要回答一个更根本的问题：**为什么它们都是 `Value`？「是一个 Value」到底带来了什么能力？**

### 2.2 什么是 SSA

LLVM IR 是 **静态单赋值（Static Single Assignment，SSA）** 形式。通俗地说：

> 每一个变量（「值」）只被赋值一次，之后所有用到它的地方都直接引用那次定义。

这就意味着 IR 里的「值」本质上是一个**有唯一定义点的对象**，使用它的指令只是「指向」这个对象。这种「定义—使用」的指针关系，正是本讲 `Value` / `Use` 要建模的东西。形式上，若把值 \( X \) 的所有使用记为集合 \( \mathrm{Uses}(X) \)，则 SSA 要求：

\[
\forall u \in \mathrm{Uses}(X),\; u \text{ 被 } X \text{ 的定义所支配（dominate）}
\]

不用死记公式，只需记住一句话：**「谁定义了我」和「谁用了我」是双向可查的**。这是 LLVM 几乎所有优化（复制传播、死代码消除、内联……）的基础。

### 2.3 不可变与唯一化（immutable & uniqued）

你会反复在源码注释里看到两个词：

- **immutable（不可变）**：对象一旦创建，其内容不再改变。
- **uniqued（唯一化）/ interned**：相同的东西全局只存在一份。

这两个性质合起来的直接好处是：**判断两个对象是否相等，只需比较指针**（地址相同即相等），不需要逐字段比较。`Type` 与 `Constant` 都满足这两条。

### 2.4 isa / cast / dyn_cast

LLVM 用一套自定义的类型识别与转换机制（`isa`、`cast`、`dyn_cast`），它不依赖 C++ 的 RTTI/vtable，而是靠每个对象里存的一个小整数 ID 来判别子类。这一点在 [u2-l1](u2-l1-ir-hierarchy.md) 已介绍过 `ValueTy` 与 `classof`，本讲会看到它们的真相来源。

---

## 3. 本讲源码地图

本讲涉及的核心头文件都位于 `include/llvm/IR/`：

| 文件 | 作用 |
| --- | --- |
| `Type.h` | 类型系统的根类 `Type`，以及 `TypeID` 枚举、各种基本类型的工厂方法 |
| `DerivedTypes.h` | 「派生类型」：`IntegerType`、`FunctionType`、`PointerType`、`VectorType`、`StructType`、`ArrayType`、`TargetExtType` 等 |
| `Value.h` | 所有「可被引用对象」的基类 `Value`，含 `use` 列表、`ValueTy` 枚举 |
| `Use.h` | 连接「值的定义」与「使用者」的边——`Use` |
| `User.h` | 「使用了别的 Value 的 Value」——`User`，管理 operand 列表 |
| `Constant.h` | 常量基类 `Constant`（继承自 `User`） |
| `Constants.h` | 各种具体常量：`ConstantInt`、`ConstantFP`、`UndefValue`、`PoisonValue` 等 |
| `Value.def` | 用 X-Macro 集中枚举所有 `Value` 子类，是 `ValueTy` 的真相来源 |

此外，实践环节会用到示例 `examples/ModuleMaker/ModuleMaker.cpp`，它示范了 `Type` 与 `ConstantInt` 的真实用法。

> 本讲的永久链接基址为
> `https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/`
> （即当前 HEAD `4e924a6`）。

---

## 4. 核心概念与源码讲解

### 4.1 Type 体系：整型 / 指针 / 向量 / 函数类型

#### 4.1.1 概念说明

`Type` 是 LLVM 类型系统的根。它有几个关键性质：

1. **`Type` 不是 `Value`。** 源码注释明确写道：*"All Values have a Type. Type is not a subclass of Value."*（[include/llvm/IR/Value.h:L62-L74](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/Value.h#L62-L74)）。类型描述「值长什么样」，但它本身不参与运算、不能当操作数。

2. **不可变 + 全局唯一。** 注释里说：*"The instances of the Type class are immutable: once they are created, they are never changed. Also note that only one instance of a particular type is ever created."*（[include/llvm/IR/Type.h:L38-L44](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/Type.h#L38-L44)）。因此比较两个类型是否相同，**比指针即可**。

3. **用 `TypeID` 区分种类。** `Type` 内部用一个枚举 `TypeID` 标明自己属于哪一类，分为「基本类型（Primitive）」与「派生类型（Derived）」两大组。

#### 4.1.2 核心流程

获取一个 `Type` 的标准姿势是**调用静态工厂方法**，而不是 `new`：

- 基本类型：`Type::getInt32Ty(C)`、`Type::getFloatTy(C)`、`Type::getVoidTy(C)` 等；
- 整数位宽：`Type::getIntNTy(C, N)`（任意位宽）；
- 派生类型：`PointerType::get(C, AddrSpace)`、`FunctionType::get(Ret, Params, IsVarArg)`、`VectorType::get(EltTy, EC)` 等。

这些工厂方法都会把请求委托给 `LLVMContext`：**由 Context 负责查表去重**——如果同款类型已存在就直接返回旧指针，否则新建并存表。这样保证「同款类型全局唯一」。流程可概括为：

```
调用 Type::getXxxTy(Context, ...) ──► Context 查唯一化表
                                          │
                              命中？ ──是──► 返回旧 Type*
                                          │
                                         否
                                          ▼
                                  新建 Type*，登记入表，返回
```

#### 4.1.3 源码精读

**(1) `Type` 类与 `TypeID` 枚举**

[`class Type`](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/Type.h#L46) 内部用 `TypeID` 标识种类，其中前半段是基本类型（各种浮点、`void`、`label`、`metadata`、`token` 等），后半段是派生类型：

```cpp
enum TypeID {
  // PrimitiveTypes     —— 基本类型
  HalfTyID = 0,  // 16-bit IEEE 浮点
  ...
  VoidTyID,       // 无大小
  LabelTyID,      // 基本块标签
  MetadataTyID,
  TokenTyID,

  // Derived types      —— 派生类型，定义在 DerivedTypes.h
  IntegerTyID,        // 任意位宽整数
  FunctionTyID,       // 函数
  PointerTyID,        // 指针
  StructTyID,         // 结构体
  ArrayTyID,          // 数组
  FixedVectorTyID,    // 定长 SIMD 向量
  ScalableVectorTyID, // 可缩放 SIMD 向量（如 RISC-V V / SVE）
  TargetExtTyID,      // 目标扩展类型
};
```

> 见 [include/llvm/IR/Type.h:L55-L81](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/Type.h#L55-L81)。`getTypeID()` 直接返回这个内部 ID（[L138](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/Type.h#L138)）。一系列 `isXxxTy()` 判别方法（如 `isIntegerTy()`、`isPointerTy()`、`isVectorTy()`）就是对 `TypeID` 的简单比较。

**(2) 工厂方法集中在 `Type` 上**

基本类型都由 `Type` 提供静态方法，典型如：

```cpp
static Type *getVoidTy(LLVMContext &C);
static Type *getFloatTy(LLVMContext &C);
static Type *getDoubleTy(LLVMContext &C);
static IntegerType *getInt32Ty(LLVMContext &C);
static IntegerType *getIntNTy(LLVMContext &C, unsigned N);
```

> 见 [include/llvm/IR/Type.h:L463-L488](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/Type.h#L463-L488)。注意返回类型：整数类工厂返回的是 **`IntegerType*`** 而非 `Type*`，因为派生类信息更具体。

**(3) `IntegerType`：把位宽藏进 SubclassData**

`IntegerType` 继承 `Type`，把位宽塞进父类的 `SubclassData` 位域，从而不新增成员、节省内存：

```cpp
class IntegerType : public Type {
protected:
  explicit IntegerType(LLVMContext &C, unsigned NumBits) : Type(C, IntegerTyID){
    setSubclassData(NumBits);   // 位宽存进 SubclassData
  }
public:
  static IntegerType *get(LLVMContext &C, unsigned NumBits);  // 唯一化入口
  unsigned getBitWidth() const { return getSubclassData(); }  // 取回位宽
};
```

> 见 [include/llvm/IR/DerivedTypes.h:L42-L66](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/DerivedTypes.h#L42-L66)、[L82](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/DerivedTypes.h#L82)。最大位宽受 `MAX_INT_BITS = (1<<23)` 限制（[L52-L59](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/DerivedTypes.h#L52-L59)）。

**(4) `FunctionType`：返回类型 + 参数列表**

`FunctionType` 把「返回类型」和「参数类型数组」放进父类的 `ContainedTys`（`Type` 里那个 `ContainedTys`/`NumContainedTys` 字段，见 [Type.h:L107-L115](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/Type.h#L107-L115)）。第 0 个是返回类型，其余是参数：

```cpp
class FunctionType : public Type {
public:
  static FunctionType *get(Type *Result, ArrayRef<Type *> Params, bool isVarArg);
  bool isVarArg() const { return getSubclassData()!=0; }
  Type *getReturnType() const { return ContainedTys[0]; }
  unsigned getNumParams() const { return NumContainedTys - 1; }
};
```

> 见 [include/llvm/IR/DerivedTypes.h:L165-L204](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/DerivedTypes.h#L165-L204)。`getNumParams() = NumContainedTys - 1` 正说明返回类型占了 `ContainedTys[0]` 这一个槽位。

**(5) `PointerType`：不透明指针 + 地址空间**

现代 LLVM 已经全面使用**不透明指针（opaque pointer）**，即指针不再记录「指向什么类型」，只记录**地址空间（address space）**：

```cpp
class PointerType : public Type {
public:
  // 在指定地址空间构造一个不透明指针
  static PointerType *get(LLVMContext &C, unsigned AddressSpace);
  // 地址空间 0 的便捷写法
  static PointerType *getUnqual(LLVMContext &C) { return PointerType::get(C, 0); }
  unsigned getAddressSpace() const { return getSubclassData(); }
};
```

> 见 [include/llvm/IR/DerivedTypes.h:L758-L787](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/DerivedTypes.h#L758-L787)。在文本 IR 里它就写成 `ptr`（默认地址空间）或 `ptr addrspace(1)` 等。

**(6) `VectorType`：定长与可缩放**

向量类型有一个公共基类 `VectorType`，再派生为 `FixedVectorType`（如 `<4 x i32>`）与 `ScalableVectorType`（如 `<vscale x 4 x i32>`，元素总数是运行时常量 `vscale` 的倍数）：

```cpp
class VectorType : public Type {              // 基类
  static VectorType *get(Type *ElementType, ElementCount EC);
};
class FixedVectorType : public VectorType {   // 定长
  static FixedVectorType *get(Type *ElementType, unsigned NumElts);
};
class ScalableVectorType : public VectorType {// 可缩放
  static ScalableVectorType *get(Type *ElementType, unsigned MinNumElts);
};
```

> 见 [include/llvm/IR/DerivedTypes.h:L490-L526](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/DerivedTypes.h#L490-L526)、[FixedVectorType:L650-L693](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/DerivedTypes.h#L650-L693)、[ScalableVectorType:L697-L750](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/DerivedTypes.h#L697-L750)。

#### 4.1.4 代码实践

**实践目标**：验证「同款类型全局唯一」，并熟记常用类型的工厂方法。

**操作步骤**（源码阅读型，结合 ModuleMaker）：

1. 打开 [examples/ModuleMaker/ModuleMaker.cpp:L38-L39](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/examples/ModuleMaker/ModuleMaker.cpp#L38-L39)，看它如何构造 `int ()` 函数类型：

   ```cpp
   FunctionType *FT =
     FunctionType::get(Type::getInt32Ty(Context), /*not vararg*/false);
   ```

   这里 `getInt32Ty` 返回 `IntegerType*`，被当作 `Type*` 传入 `FunctionType::get` 的返回类型参数。

2. 设想在同一段代码里**连续调用两次** `Type::getInt32Ty(Context)`：
   - 预测两次返回的指针是否相同？
3. 想象把上面的 `getInt32Ty` 换成 `getIntNTy(Context, 32)`，问二者得到的 `Type*` 是否相等？

**需要观察的现象**：

- 两次 `getInt32Ty(Context)` 应当返回**同一个地址**（唯一化）。
- `getInt32Ty(C)` 与 `getIntNTy(C, 32)` 也应当返回**同一个地址**，因为它们描述的是同款类型。

**预期结果**：指针完全相等，验证了 [Type.h:L38-L44](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/Type.h#L38-L44) 的「only one instance of a particular type is ever created」。若你想真正打印确认，可写一个最小程序对比两个 `Type*` 是否 `==`。**待本地验证**（取决于你是否已按 [u1-l2](u1-l2-build-and-layout.md) 完成本地 CMake 构建）。

#### 4.1.5 小练习与答案

**练习 1**：`Type::getInt32Ty(C)` 的返回类型是 `IntegerType*` 还是 `Type*`？为什么不是 `Type*`？

**参考答案**：返回 `IntegerType*`。因为 `IntegerType` 比 `Type` 多了「位宽」这一具体信息（`getBitWidth()`），工厂方法直接返回更具体的子类指针，能让调用方免去一次 `cast<IntegerType>`。源码见 [DerivedTypes.h:L482-L488](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/Type.h#L482-L488)。

**练习 2**：`FunctionType::getNumParams()` 为什么是 `NumContainedTys - 1`？

**参考答案**：因为 `ContainedTys` 的第 0 个槽位存的是**返回类型**，真正的参数从 `ContainedTys[1]` 开始，所以参数个数 = 总数 - 1。见 [DerivedTypes.h:L204](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/DerivedTypes.h#L204)。

---

### 4.2 Value / User / Use 关系

#### 4.2.1 概念说明

`Value` 是 LLVM 里**最重要的基类**。源码注释直言：*"This is a very important LLVM class."*（[Value.h:L62-L74](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/Value.h#L62-L74)）。一句话定义：

> **凡是可以被别的指令当作操作数引用的「东西」，都是 `Value`。**

`Instruction`、`Function`、`BasicBlock`、`Argument`、`Constant`……全都是 `Value`。每个 `Value` 都带：

- 一个 **`Type`**（`getType()`）；
- 一张 **use 列表**，记录「谁正在用我」。

围绕 `Value` 有两个紧邻的概念：

| 概念 | 一句话定义 | 关键能力 |
| --- | --- | --- |
| `Value` | 「被引用的对象」 | 维护 use 列表，回答「谁用了我」 |
| `User` | 「使用了别的 Value 的 Value」 | 维护 operand 列表，回答「我用了谁」 |
| `Use` | 连接「使用者」与「被引用值」的**边** | 既能跳到被引用的 `Value`，也能跳回所属的 `User` |

注意：`User` **也是** `Value`（`class User : public Value`，[User.h:L44](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/User.h#L44)）。也就是说，一条指令既是「使用者」（它读别人的值），又是「被引用的对象」（别的指令可能读它的结果）。`Argument`、`BasicBlock` 则是「只被引用、不引用别人」的 `Value`（不是 `User`）。

#### 4.2.2 核心流程

`Use` 是 `Value` 与 `User` 之间的「边」。源码把它描述为一张**二维双向链表**（[Use.h:L29-L34](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/Use.h#L29-L34)）：

- 从 `User` 一侧看：`User` 持有一串 `Use`（operand 列表），每个 `Use` 指向一个被引用的 `Value`；
- 从 `Value` 一侧看：被引用的 `Value` 也持有一条 `Use` 链，串联起所有引用它的 `Use`。

当给某条指令设置一个操作数时，会自动把对应的 `Use` 节点**登记到被引用值的 use 列表**里。整个过程如下：

```
User(指令) 设置 operand[i] = X
        │
        ▼
Use::set(X)                       // Use.h 里的 set()
   1. removeFromList()             // 先把自己从旧的 use 链摘下
   2. Val = X                      // 记住「我引用谁」
   3. X->addUse(*this)             // 把自己挂到 X 的 use 链头部
        │
        ▼
此后 X->users() 能遍历到这条指令；指令->operand(i) 能取回 X
```

这正是 SSA 「定义—使用」双向可达的实现。一个高频应用是 `replaceAllUsesWith`：把某个值的所有使用一次性改指向另一个值（[Value.h:L300](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/Value.h#L300)），它正是优化里「替换」操作（如复制传播、CSE）的基石。

#### 4.2.3 源码精读

**(1) `Value` 持有类型与 use 链**

```cpp
class Value {
  const unsigned char SubclassID;     // 子类 ID（供 isa/dyn_cast 用）
  ...
  Type *VTy;                          // 我的类型
  Use *UseList = nullptr;             // 引用我的那些 Use 组成的链表头
public:
  Type *getType() const { return VTy; }
  LLVMContext &getContext() const { return VTy->getContext(); }
};
```

> 见 [include/llvm/IR/Value.h:L75-L119](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/Value.h#L75-L119)、[`getType()` 在 L255](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/Value.h#L255)。注意它**通过类型持有 Context**（`VTy->getContext()`），所以每个 `Value` 都隐式关联到一个 `LLVMContext`。

**(2) 遍历 use / user**

`Value` 提供两套等价的迭代器：`uses()` 遍历 `Use`（边），`users()` 遍历 `User`（使用者本身）：

```cpp
iterator_range<use_iterator> uses();   // 遍历 Use
iterator_range<user_iterator> users(); // 遍历 User*（= Use->getUser()）
bool use_empty() const { ... return UseList == nullptr; }
bool hasOneUse() const { return UseList && hasSingleElement(uses()); }
```

> 见 [Value.h:L346-L349](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/Value.h#L346-L349)（`use_empty`）、[L380-L387](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/Value.h#L380-L387)（`uses`）、[L426-L433](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/Value.h#L426-L433)（`users`）、[L439](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/Value.h#L439)（`hasOneUse`）。

**(3) `Use` 的四个字段与 `set()`**

```cpp
class Use {
  Value *Val = nullptr;     // 我引用谁
  Use *Next = nullptr;      // 同一条 use 链上的下一个
  Use **Prev = nullptr;     // 指向前一个节点的 next 指针（双向链表）
  User *Parent = nullptr;   // 我属于哪个 User（哪条指令）
public:
  Value *get() const { return Val; }
  User *getUser() const { return Parent; }
  inline void set(Value *Val);
};
```

> 见 [include/llvm/IR/Use.h:L35-L85](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/Use.h#L35-L85)。`set()` 的完整实现其实写在 `Value.h` 末尾，三步：`removeFromList()` → 赋值 `Val` → `V->addUse(*this)`（[Value.h:L874-L879](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/Value.h#L874-L879)）。`addToList` / `removeFromList` 是经典双向链表插入/删除（[Use.h:L87-L105](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/Use.h#L87-L105)）。

**(4) `User` 管理 operand 列表**

`User` 提供操作数读写接口，并强调**常量不可用 `setOperand` 改操作数**：

```cpp
class User : public Value {
public:
  Value *getOperand(unsigned i) const { ... return getOperandList()[i]; }
  void setOperand(unsigned i, Value *Val) {
    assert((!isa<Constant>((const Value*)this) ||
            isa<GlobalValue>((const Value*)this)) &&
           "Cannot mutate a constant with setOperand!");
    ...
  }
  unsigned getNumOperands() const { return NumUserOperands; }
};
```

> 见 [User.h:L207-L229](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/User.h#L207-L229)。那个 assert 印证了「常量不可变」的设计：除了 `GlobalValue`（函数/全局变量的地址）这类特例，普通常量一旦建好就不许改。`User::classof` 则定义了「谁是 User」：要么是 `Instruction`，要么是 `Constant`（[User.h:L336-L338](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/User.h#L336-L338)）。

**(5) `ValueTy` 与 `classof`：类型识别的真相**

`Value` 用一个 `ValueTy` 枚举来标记具体子类，其内容由 X-Macro 文件 `Value.def` 集中生成：

```cpp
enum ValueTy {
#define HANDLE_VALUE(Name) Name##Val,
#include "llvm/IR/Value.def"
  // 标记常量区间的首尾
#define HANDLE_CONSTANT_MARKER(Marker, Constant) Marker = Constant##Val,
#include "llvm/IR/Value.def"
};
unsigned getValueID() const { return SubclassID; }
```

> 见 [Value.h:L524-L545](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/Value.h#L524-L545)。展开 `Value.def` 后，枚举值依次是 `UndefValueVal, PoisonValueVal, …, ConstantIntVal, ConstantFPVal, …, FunctionVal, …, ArgumentVal, BasicBlockVal, InstructionVal` 等（见 [Value.def:L79-L127](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/Value.def#L79-L127)）。关键是文件里还放了**区间标记**：`ConstantFirstVal=UndefValue`、`ConstantLastVal=ConstantPtrAuth`、`ConstantDataFirstVal/LastVal`、`ConstantAggregateFirstVal/LastVal`（[Value.def:L106-L111](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/Value.def#L106-L111)）。

利用「连续编号」这一布局，`classof` 只需一次区间比较就能判别大类，例如 `isa<Constant>(v)`：

```cpp
template <> struct isa_impl<Constant, Value> {
  static inline bool doit(const Value &Val) {
    static_assert(Value::ConstantFirstVal == 0, "...");
    return Val.getValueID() <= Value::ConstantLastVal;
  }
};
```

> 见 [Value.h:L961-L967](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/Value.h#L961-L967)。因为常量被故意排在编号最前面（`ConstantFirstVal == 0`），所以 `getValueID() <= ConstantLastVal` 一条判断就够。`Instruction` 则排在最后（`InstructionVal` 是最高编号），所以 `isa<Instruction>` 是 `getValueID() >= InstructionVal`（[Value.h:L996-L1000](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/Value.h#L996-L1000)）。这就是 LLVM 不依赖 RTTI 也能 `isa/cast/dyn_cast` 的秘密。

#### 4.2.4 代码实践

**实践目标**：通过跟踪一段调用链，理解「设置操作数」如何把 `Use` 登记到 use 列表。

**操作步骤**（源码阅读型）：

1. 阅读 [examples/ModuleMaker/ModuleMaker.cpp:L50-L58](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/examples/ModuleMaker/ModuleMaker.cpp#L50-L58)：

   ```cpp
   Value *Two = ConstantInt::get(Type::getInt32Ty(Context), 2);
   Value *Three = ConstantInt::get(Type::getInt32Ty(Context), 3);
   Instruction *Add = BinaryOperator::Create(Instruction::Add, Two, Three, "addresult");
   Add->insertInto(BB, BB->end());
   ```

2. 跟踪 `BinaryOperator::Create(Add, Two, Three, ...)` 内部最终会给 `Add` 的两个 operand 分别赋值为 `Two` 和 `Three`。请你在源码中追踪这条链：
   - `User::setOperand(i, Val)` → `getOperandList()[i] = Val`（[User.h:L212-L218](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/User.h#L212-L218)）；
   - 这会调用 `Use::operator=` → `Use::set`（[Value.h:L881-L884](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/Value.h#L881-L884)）；
   - `set` 内部 `X->addUse(*this)`（[Value.h:L874-L879](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/Value.h#L874-L879)）。
3. 据此回答：执行完上述代码后，`Two->users()` 里应当出现谁？

**需要观察的现象 / 预期结果**：

- `Two`（值为 2 的 `ConstantInt`）的 `users()` 应当**恰好包含**那条 `Add` 指令；同理 `Three->users()` 也包含它。
- 由于 `2` 和 `3` 是常量、按结构等价共享，若别处也用到整数 `2`，它们会共用同一个 `ConstantInt` 对象，于是该对象的 `users()` 会**多于一个**。

> 小提示：`ConstantInt` 属于 `ConstantData`，而 `ConstantData`「没有 use 列表」、`use_empty()` 恒为 true（见 [Constants.h:L48-L80](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/Constants.h#L48-L80) 与 [Value.h:L344](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/Value.h#L344) 的 `hasUseList()`）。这是常量为了极致省内存而做的特例——常量在 use 链上的「使用关系」由使用方维护，本讲作为概念理解即可，不必纠结这个细节。**待本地验证**（若要真正打印 `users()`，需先有可运行环境）。

#### 4.2.5 小练习与答案

**练习 1**：`Argument` 是 `User` 吗？`BasicBlock` 是 `User` 吗？为什么？

**参考答案**：都不是。在 [Value.def:L113-L114](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/Value.def#L113-L114) 中，`Argument` 与 `BasicBlock` 用 `HANDLE_VALUE` 登记，说明它们是「只被引用、不引用别人」的 `Value`，没有 operand，所以不是 `User`。而 `Instruction`、`Constant` 才是 `User`（[User.h:L336-L338](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/User.h#L336-L338)）。

**练习 2**：为什么 `User::setOperand` 里要加 `assert(!isa<Constant>(this) ...)`？

**参考答案**：因为常量按设计是**不可变**的（[Constant.h:L37-L42](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/Constant.h#L37-L42)），且全局按结构等价共享。如果允许随便改某个常量的操作数，就会破坏「结构等价 ⇒ 地址相同」这一不变量，导致所有共享它的地方都被错误影响。所以普通常量禁止用 `setOperand` 改操作数（`GlobalValue` 因代表「地址」而例外）。

---

### 4.3 Constant 与 ConstantInt / ConstantFP

#### 4.3.1 概念说明

`Constant` 继承自 `User`（`class Constant : public User`，[Constant.h:L43](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/Constant.h#L43)）。这有点反直觉——常量不是「叶子」吗？为什么是 `User`？因为**有些常量本身由其它常量组成**（如常量数组、常量结构体、`ConstantExpr`），它们「使用」了别的常量作为操作数，所以是 `User`。

`Constant` 的核心性质（[Constant.h:L26-L42](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/Constant.h#L26-L42)）：

- **不可变**：创建后不再改变；
- **按结构等价共享（structurally uniqued）**：两个结构相同的常量地址相同；
- **按需创建、永不删除**：调用方无需关心生命周期。

`Constant` 体系分两大支：

| 分支 | 基类 | 特点 | 代表 |
| --- | --- | --- | --- |
| 无操作数常量 | `ConstantData` | 数据直接内嵌，无 operand，**无 use 列表** | `ConstantInt`、`ConstantFP`、`ConstantPointerNull`、`ConstantAggregateZero`、`UndefValue`、`PoisonValue` |
| 有操作数常量 | `ConstantAggregate` | 由其它常量组成，有 operand | `ConstantArray`、`ConstantStruct`、`ConstantVector` |

此外还有 `ConstantExpr`（常量表达式，如编译期 GEP）、`BlockAddress`、以及作为 `Constant` 的 `GlobalValue`（`Function`、`GlobalVariable`……它们的地址在运行期不变，所以也是常量）。

#### 4.3.2 核心流程

构造一个整型常量最常见的方式是 `ConstantInt::get`：

```cpp
// 方式一：给定「类型 + 数值」
ConstantInt *get(IntegerType *Ty, uint64_t V, bool IsSigned = false, ...);
// 方式二：直接给 Context 与任意位宽的 APInt
ConstantInt *get(LLVMContext &Context, const APInt &V);
// 便捷：布尔
static ConstantInt *getTrue(LLVMContext &Context);
static ConstantInt *getFalse(LLVMContext &Context);
```

`ModuleMaker` 里正是用第一种：

```cpp
Value *Two = ConstantInt::get(Type::getInt32Ty(Context), 2);
```

`ConstantInt` 内部用一个 `APInt`（任意精度整数）保存数值；`ConstantFP` 对应地用 `APFloat`。`APInt`/`APFloat` 是 LLVM 自己实现的任意精度算术类型，能表达超过 64 位的整数与各种浮点语义。

#### 4.3.3 源码精读

**(1) `Constant`：常量的公共基类**

```cpp
class Constant : public User {
protected:
  Constant(Type *ty, ValueTy vty, AllocInfo AllocInfo) : User(ty, vty, AllocInfo) {}
public:
  static Constant *getNullValue(Type *Ty);   // 该类型的「零值」
  static Constant *getAllOnesValue(Type *Ty);// 全 1
  bool isNullValue() const { return SubclassOptionalData & IsNullValue; }
  static bool classof(const Value *V) {
    return V->getValueID() <= ConstantLastVal;   // 一次区间比较
  }
};
```

> 见 [Constant.h:L43-L55](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/Constant.h#L43-L55)、[L187-L190](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/Constant.h#L187-L190)、[L204](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/Constant.h#L204)。注意 `getNullValue` 对不同类型返回不同的「零」：整型是 0、浮点是 +0.0、指针是 `null`、聚合类型是 `ConstantAggregateZero`，这正是「类型决定零值」的体现。

**(2) `ConstantInt`：整数常量**

```cpp
class ConstantInt final : public ConstantData {
  APInt Val;                       // 任意精度整数值
public:
  static Constant *get(Type *Ty, uint64_t V, bool IsSigned = false,
                       bool ImplicitTrunc = false);
  static ConstantInt *get(IntegerType *Ty, uint64_t V, ...);
  static ConstantInt *getSigned(IntegerType *Ty, int64_t V, ...);
  const APInt &getValue() const { return Val; }
  unsigned getBitWidth() const { return Val.getBitWidth(); }
  uint64_t getZExtValue() const { return Val.getZExtValue(); }  // 零扩展取值
  int64_t  getSExtValue() const { return Val.getSExtValue(); }  // 符号扩展取值
  bool isZero() const { return isNullValue(); }
  bool isOne() const  { return Val.isOne(); }
  bool isMinusOne() const { return Val.isAllOnes(); }
};
```

> 见 [Constants.h:L87-L159](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/Constants.h#L87-L159)（`get` 在 [L116-L127](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/Constants.h#L116-L127)、`getSigned` 在 [L135-L138](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/Constants.h#L135-L138)）。要点：
> - `ConstantInt` 继承自 **`ConstantData`**（无 operand 那一支），[Constants.h:L56](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/Constants.h#L56)。
> - 值用 `APInt` 存（[L91](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/Constants.h#L91)），位宽来自类型，所以 `getBitWidth()` 与类型的位宽一致。
> - 取值要区分 `getZExtValue()`（无符号）与 `getSExtValue()`（有符号），因为内部统一按无规范化的 `APInt` 存。

**(3) `ConstantFP`：浮点常量**

```cpp
class ConstantFP final : public ConstantData {
  APFloat Val;                     // 任意精度/多语义浮点值
public:
  static ConstantFP *get(Type *Ty, double V);
  static ConstantFP *get(Type *Ty, const APFloat &V);
  const APFloat &getValueAPF() const { return Val; }
  bool isZero() const     { return Val.isZero(); }
  bool isNaN() const      { return Val.isNaN(); }
  bool isInfinity() const { return Val.isInfinity(); }
  bool isExactlyValue(const APFloat &V) const;  // 逐位比较（避免 -0.0 == 0.0）
};
```

> 见 [Constants.h:L420-L508](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/Constants.h#L420-L508)。注意 `isExactlyValue` 的注释特意强调：**不要用 `double` 的 `==`** 比较浮点常量，因为 `0.0 == -0.0` 在 IEEE 浮点里为真，但二者是不同的位模式。这正是 LLVM 用 `APFloat` 精确建模浮点的理由。

**(4) 几个特殊常量**

- `UndefValue`（[Constants.h:L1631](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/Constants.h#L1631)）：表示「未指定位内容」的值，文本 IR 写作 `undef`。
- `PoisonValue`（[Constants.h:L1679](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/Constants.h#L1679)）：继承 `UndefValue`，表示「一旦被使用就触发未定义行为」的毒值（`poison`），比 `undef` 更安全、更利于推测执行优化。
- `ConstantPointerNull`（[Constants.h:L716](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/Constants.h#L716)）：空指针常量（`null`）。
- `ConstantAggregateZero`（[Constants.h:L514](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/Constants.h#L514)）：聚合类型的「全零」（`zeroinitializer`）。

**(5) 这些常量在 `ValueTy` 里的位置**

回顾 [Value.def:L79-L111](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/Value.def#L79-L111)：`ConstantInt`、`ConstantFP` 都属于 `ConstantData` 段（编号最小的一段），`ConstantArray/Struct/Vector` 属于 `ConstantAggregate` 段。所以 `isa<ConstantData>(v)` 也是一次 `<= ConstantDataLastVal` 的区间判断（[Value.h:L969-L975](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/Value.h#L969-L975)）。把常量排在最前，正是为了让最高频的 `isa<Constant>` 判断只用一条比较指令。

#### 4.3.4 代码实践

**实践目标**：在真实示例里定位常量的构造点，理解 `Type` 与 `ConstantInt` 的配合。

**操作步骤**：

1. 打开 [examples/ModuleMaker/ModuleMaker.cpp:L50-L51](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/examples/ModuleMaker/ModuleMaker.cpp#L50-L51)：

   ```cpp
   Value *Two   = ConstantInt::get(Type::getInt32Ty(Context), 2);
   Value *Three = ConstantInt::get(Type::getInt32Ty(Context), 3);
   ```

   - 先用 `Type::getInt32Ty(Context)` 拿到唯一化的 `i32` 类型；
   - 再用 `ConstantInt::get` 把数值 `2`/`3` 绑定到该类型，得到共享的 `ConstantInt`。

2. 设想做一处**最小修改**（结合 [u1-l4 综合实践](u1-l4-module-maker.md)）：把 `Three` 改成 `ConstantInt::getSigned(Type::getInt32Ty(Context), -3)`。
3. 预测：生成的 IR 里这条 `add` 会变成什么？结果类型还是 `i32` 吗？

**需要观察的现象 / 预期结果**：

- 由于 `-3` 是负数，`getSigned` 会做**符号扩展**到 32 位；最终 IR 仍写作 `i32 -3`（如 `add nsw i32 2, -3`）。
- 这印证了「类型（位宽）由 `Type` 决定、数值与符号解释由 `APInt`/`getSigned` 决定」。

> 若你想顺手验证「常量共享」，可在修改后的程序里再 `ConstantInt::get(Type::getInt32Ty(Context), 2)` 一次，比较两次返回的指针是否相同。**待本地验证**（需要先按 [u1-l2](u1-l2-build-and-layout.md) 配置好构建并用 `llvm-dis` 查看输出）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `Constant` 继承自 `User`，而 `ConstantInt` 却继承自 `ConstantData`？

**参考答案**：`Constant` 要覆盖「由其它常量组成」的常量（如 `ConstantArray`、`ConstantExpr`），它们有 operand、需要 `User` 的能力，所以 `Constant : public User`。而 `ConstantInt` 是「叶子」常量，数值直接内嵌在 `APInt Val` 里、没有 operand，因此归于 `ConstantData`（无 operand、无 use 列表那一支）。见 [Constants.h:L56](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/Constants.h#L56) 与 [L87](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/Constants.h#L87)。

**练习 2**：判断对错，并说明依据——"`isa<Constant>(someInstruction)` 与 `isa<Instruction>(someConstant)` 不可能同时为真。"

**参考答案**：正确。在 `ValueTy` 的编号布局里，常量排在最前（`<= ConstantLastVal`），指令排在最后（`>= InstructionVal`），两者区间不重叠（见 [Value.def:L79-L127](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/Value.def#L79-L127)）。所以同一个 `Value` 不可能既是 `Constant` 又是 `Instruction`。

**练习 3**：`Function` 是 `Constant` 吗？为什么？

**参考答案**：是。`Function` 经 `GlobalValue → GlobalObject → Constant → User → Value` 继承（`Function` 的地址在运行期不可变，故视为常量）。`Value.def` 里也用 `HANDLE_GLOBAL_VALUE(Function)` 把它归入常量段（[Value.def:L96](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/Value.def#L96)）。这也解释了为什么函数可以当 `ConstantExpr`（如编译期 GEP 取函数地址）的操作数。

---

## 5. 综合实践

**任务**：对照源码，画出 `Type` 与 `Value` 两棵继承关系图，并准确标注 `IntegerType` 与 `ConstantInt` 的位置。这是把本讲三个最小模块「串起来」的练习——你需要同时用到 4.1（Type 体系）、4.2（Value/User/Use）、4.3（Constant 体系）的知识。

**操作步骤**：

1. **画 `Type` 树**。以 `Type`（[Type.h:L46](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/Type.h#L46)）为根，把 `DerivedTypes.h` 里所有 `public Type` 的派生类挂上去。参考答案（行号为定义处）：

   ```
   Type (Type.h:46)
   ├── IntegerType        (DerivedTypes.h:42)   ◄── 标注点
   ├── FunctionType       (DerivedTypes.h:165)
   ├── StructType         (DerivedTypes.h:278)
   ├── ArrayType          (DerivedTypes.h:458)
   ├── PointerType        (DerivedTypes.h:758)
   ├── TargetExtType      (DerivedTypes.h:833)
   └── VectorType         (DerivedTypes.h:490)
       ├── FixedVectorType    (DerivedTypes.h:650)
       └── ScalableVectorType (DerivedTypes.h:697)
   ```

2. **画 `Value` 树**。以 `Value`（[Value.h:L75](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/Value.h#L75)）为根。注意两条主干：`User`（有用 operand 的能力）与非 `User` 的叶子 `Value`。参考答案：

   ```
   Value (Value.h:75)
   ├── Argument                         (非 User，无 operand)
   ├── BasicBlock                       (非 User，无 operand)
   └── User (User.h:44)                 ◄── 「我用了谁」
       ├── Instruction                  (Value.def:127；也是 User)
       └── Constant (Constant.h:43)
           ├── ConstantAggregate (Constants.h:565)        ← 有 operand 的常量
           │   ├── ConstantArray    (Constants.h:590)
           │   ├── ConstantStruct   (Constants.h:622)
           │   └── ConstantVector   (Constants.h:674)
           ├── ConstantExpr          (Constants.h:1316)
           ├── BlockAddress         (Constants.h:1088)
           └── ConstantData (Constants.h:56)              ← 无 operand 的常量
               ├── ConstantInt            (Constants.h:87)  ◄── 标注点
               ├── ConstantFP             (Constants.h:420)
               ├── ConstantPointerNull    (Constants.h:716)
               ├── ConstantAggregateZero  (Constants.h:514)
               ├── ConstantDataArray      (Constants.h:865)
               ├── ConstantDataVector     (Constants.h:951)
               ├── ConstantTokenNone      (Constants.h:1035)
               └── UndefValue             (Constants.h:1631)
                   └── PoisonValue        (Constants.h:1679)
   ```

   > 提示：`Function`、`GlobalVariable` 等通过 `GlobalValue → GlobalObject → Constant` 也挂在这棵树上（见练习 3），为避免拥挤可单独注明，不一定要画进主图。

3. **标注两个关键类**：
   - `IntegerType` 在 **`Type` 树**下，是 `Type` 的直接子类（`class IntegerType : public Type`，[DerivedTypes.h:L42](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/DerivedTypes.h#L42)）。它**不是** `Value`。
   - `ConstantInt` 在 **`Value` 树**下，路径为 `Value → User → Constant → ConstantData → ConstantInt`（[Constants.h:L87](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/Constants.h#L87)）。它**持有**一个 `IntegerType` 作为自己的 `getType()`。

4. **用一句话写清二者的关系**：`IntegerType` 是「类型」，描述 `ConstantInt` 这类值「长什么样」；`ConstantInt` 是「值」，内部通过 `Value::VTy` 指向某个 `IntegerType`。这正是本讲开篇那句 *"All Values have a Type. Type is not a subclass of Value."* 的具象化。

**预期结果**：两张清晰的继承树，`IntegerType` 标在 `Type` 子层、`ConstantInt` 标在 `Value → User → Constant → ConstantData` 链路末端，并能在图上指出「`ConstantInt` 的类型字段指向 `IntegerType`」这条跨树的引用关系。完成后，你已经把本讲最核心的「类型 vs 值」二元结构内化了。

---

## 6. 本讲小结

- **`Type` 是类型系统的根，不是 `Value`**。它不可变、由 `LLVMContext` 全局唯一化，比较相等只需比指针；用 `TypeID` 区分基本类型与派生类型，通过 `Type::getInt32Ty(C)`、`PointerType::get(C, AS)`、`FunctionType::get(...)`、`VectorType::get(...)` 等静态工厂获取。
- **派生类型按需「包含」其它类型**（如 `FunctionType` 的返回类型与参数、`ArrayType`/`VectorType` 的元素），统一存在 `Type::ContainedTys` 里。
- **`Value` 是「所有可被引用对象」的基类**，每个 `Value` 持有一个 `Type` 和一条 use 列表；`User`（继承 `Value`）持有 operand 列表，`Use` 是连接二者的「边」，构成双向可达的 def-use 关系。
- **类型识别不靠 RTTI**：`ValueTy` 枚举由 `Value.def` 集中生成，配合 `classof` 的区间比较实现高效的 `isa/cast/dyn_cast`；常量被故意排在编号最前，使 `isa<Constant>` 只需一条比较。
- **`Constant` 继承自 `User`**，分「无 operand 的 `ConstantData`」与「有 operand 的 `ConstantAggregate`」两支；`ConstantInt` 用 `APInt` 存值、`ConstantFP` 用 `APFloat` 存值，二者都按结构等价全局共享、不可变。
- **`IntegerType` 属于 `Type` 树，`ConstantInt` 属于 `Value` 树**；一个 `ConstantInt` 通过 `getType()` 指向某个 `IntegerType`，这是「值持有类型」而非「类型继承值」。

---

## 7. 下一步学习建议

- 下一讲 [u2-l3 用 IRBuilder 构建 IR](u2-l3-irbuilder.md) 将把本讲的 `Type`、`ConstantInt`、`Value`/`Use` 串成「构造指令」的高层 API：`IRBuilder` 会在内部替你处理「选类型、造常量、插入指令、维护插入点与常量折叠」等细节，是手工 `BinaryOperator::Create` + `insertInto`（见 [u1-l4](u1-l4-module-maker.md)）的升级版。
- 想加深对 use-def 的理解，建议继续阅读 `include/llvm/IR/Value.h` 中 `replaceAllUsesWith`、`replaceUsesWithIf` 的注释（[Value.h:L295-L321](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/Value.h#L295-L321)），它们是后续优化 Pass（单元 3、4）最常调用的接口。
- 对常量折叠感兴趣的同学，可提前浏览 `lib/IR/Constants.cpp` 与 `ConstantExpr::get` 系列，看看「两个常量运算 → 一个新常量」是如何在编译期完成的。
