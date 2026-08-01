# Type 系统与常量

## 1. 本讲目标

本讲紧接 [u3-l1（Module/Function/BasicBlock）](u3-l1-ir-hierarchy.md) 与 [u3-l2（Value/Use）](u3-l2-value-use-ssa.md)。上一讲我们已经知道：IR 是一棵「模块⊃函数⊃基本块⊃指令」的树，而这一切对象最终都派生自 `Value`，并通过 `Use` 链互相引用。

但每条指令、每个值都还有两样「身份属性」本讲要专门讲清：

1. **它的类型是什么**（`Value::getType()` 返回什么）。
2. **它的值如果是编译期已知的，在内存里怎么表示**（即常量 `Constant`）。

学完本讲你应当能够：

- 说出 LLVM IR 的主要类型类别（整数、浮点、指针、数组、向量、结构体、函数类型、目标扩展类型），以及它们各自的工厂方法与 `.ll` 文本形式。
- 理解 `Type` 的「在 `LLVMContext` 内唯一化（uniqued）」设计，并能解释为什么比较两个类型只需比较指针。
- 区分 `Constant`、`ConstantInt`、`ConstantExpr`、`ConstantDataSequential` 等常量子类，掌握常量的唯一化与常量折叠（constant folding）机制。
- 能用 C++ API 构造 `i32`、`ptr`、`<4 x float>` 等类型，并构造一个 `ConstantInt` 与一个 `ConstantExpr`，打印它们的文本形式。

## 2. 前置知识

- **LLVMContext 是「所有 IR 对象的家」**：从 [u2-l3 的 Kaleidoscope](u2-l3-kaleidoscope-tour.md) 中你已经见过，构造任何 IR 都要先有一个 `LLVMContext`。本讲你会看到，类型与常量的「唯一化表」正是挂在 `LLVMContext` 内部的 `LLVMContextImpl`（简称 `pImpl`）上。
- **Value 携带一个 Type**：上一讲讲过 `Value` 是 IR 对象的根基类。每个 `Value` 都有一个 `Type*`，可通过 `getType()` 取到。所以「类型」是理解任何 `Value` 的前提。
- **`.ll` 文本 IR 的基本读法**：参见 [u2-l2](u2-l2-read-write-ir.md)。本讲会频繁用 `.ll` 文本来展示类型和常量的「长相」。
- **isa/dyn_cast**：上一讲提过 `isa<>`/`dyn_cast<>` 靠 `SubclassID` 派发。本讲你会看到 `Type` 与 `Constant` 也用同样的机制做类型识别。

一个贯穿全讲的关键区分：

> **Type 不是 Value，Constant 是 Value。** 类型描述「值的形态」，而常量本身就是值（可作为指令的操作数）。两者都是不可变（immutable）且在 context 内唯一化的，但分属两条独立的类层次。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `llvm/include/llvm/IR/Type.h` | `Type` 基类声明、`TypeID` 枚举、所有原始类型的工厂方法声明（`getInt32Ty` 等）。 |
| `llvm/include/llvm/IR/DerivedTypes.h` | 派生类型类：`IntegerType`、`FunctionType`、`StructType`、`ArrayType`、`VectorType`/`FixedVectorType`/`ScalableVectorType`、`PointerType`、`TargetExtType`。 |
| `llvm/lib/IR/Type.cpp` | 上述类型类的实现，重点是各工厂方法如何走 `pImpl` 完成唯一化。 |
| `llvm/include/llvm/IR/Constants.h` | 常量子类声明：`Constant`、`ConstantData`、`ConstantInt`、`ConstantPointerNull`、`ConstantDataSequential`、`ConstantExpr` 等。 |
| `llvm/lib/IR/Constants.cpp` | 常子类工厂实现（`ConstantInt::get`、`ConstantExpr::getAdd` 等），含唯一化与折叠入口。 |
| `llvm/lib/IR/ConstantFold.cpp` | 常量折叠的具体规则（`ConstantFoldBinaryInstruction`、`ConstantFoldCastInstruction`），解释 `ConstantExpr` 何时会被折叠掉。 |

---

## 4. 核心概念与源码讲解

### 4.1 类型体系

#### 4.1.1 概念说明

LLVM IR 的类型系统是**静态、显式、与具体语言无关**的。不管前端是 C、C++、Rust 还是 Kaleidoscope，最终落到 IR 上的类型都来自同一套固定集合。

类型分两大类：

- **原始类型（Primitive）**：`void`、各种浮点（`half`/`float`/`double`/`fp128`/…）、`label`/`metadata`/`token`/`x86_amx`。
- **派生类型（Derived）**：由其它类型组合而成，包括整数、指针、数组、向量、结构体、函数类型、目标扩展类型。派生类型又细分为「有内含类型的」——比如数组内含「元素类型」、函数类型内含「返回类型 + 参数类型列表」。`Type` 基类用一个 `ContainedTys` 数组统一描述这些内含类型。

> **直觉**：你可以把 `Type` 看成一张「由类型节点构成的、有向无环的依赖图」。`[10 x [4 x i32]]` 这个类型节点，内含一个 `[4 x i32]` 节点，后者又内含 `i32` 节点。所有节点都被唯一化，所以这张图在整个 `LLVMContext` 里是共享的。

所有类型用 `Type::TypeID` 这个枚举来标识种类（[llvm/include/llvm/IR/Type.h:55-81](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/Type.h#L55-L81) 下称「种类 ID」）。一个最关键的设计写在类头注释里（[llvm/include/llvm/IR/Type.h:38-46](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/Type.h#L38-L46)）：

> 类型的实例是**不可变**的：一旦创建永不改变；并且**每个特定类型在 context 内只存在一个实例**，因此比较两个类型是否相等只需做一次指针比较。类型只能通过静态工厂方法创建，且一旦分配永不释放。

这就是本模块最核心的概念：**唯一化（uniquing）**。

#### 4.1.2 核心流程

构造一个类型对象的标准流程是：

1. 用户调用静态工厂，例如 `Type::getInt32Ty(C)` 或 `ArrayType::get(EltTy, 10)`。
2. 工厂方法内部都委托给 `LLVMContextImpl`（`C.pImpl`）里维护的「唯一化表」。
3. **原始类型**：直接返回 `pImpl` 中预置的单例成员（如 `pImpl->Int32Ty`），它们随 context 一起出生，根本不存在「第二次构造」。
4. **派生类型**：按「结构键」在对应的 map 里查（整数按位宽、数组按「元素类型 + 元素数」、函数按「返回类型 + 参数列表 + 是否变参」…）。命中就返回同一指针；未命中才 `new` 一个。
5. 由于结构相同的类型共享同一对象，判等只需 `Ty1 == Ty2`，无需深比较；这也是 `isa<>`/`dyn_cast<>` 仅凭 `TypeID` 即可派发的前提。

下面用伪代码概括派生类型的「查表或创建」：

```
T* T::get(...结构键 Key...) {
  Entry& slot = C.pImpl->TTable[Key];   // 唯一化表
  if (!slot)
    slot = new (...) T(...);             // 仅未命中时才分配
  return slot;
}
```

向量化类型的大小是一个值得记住的简单关系：一个 `N` 个 `s` 位元素的定长向量，其位宽为 \( \text{size} = N \times s \)。可伸缩向量 `<vscale x N x T>` 的实际元素数则是编译期未知量 `vscale` 的整数倍，只有最小值 `N` 已知。

#### 4.1.3 源码精读

**(1) `TypeID` 枚举——类型种类的总目录**

[llvm/include/llvm/IR/Type.h:55-81](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/Type.h#L55-L81) 列出了全部种类 ID，例如 `IntegerTyID`、`PointerTyID`、`ArrayTyID`、`FixedVectorTyID`、`ScalableVectorTyID`、`StructTyID`、`FunctionTyID`、`TargetExtTyID`。`Type` 把它压缩进一个 8 位的 `ID : 8` 位域（[llvm/include/llvm/IR/Type.h:87](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/Type.h#L87)），所有 `isXxxTy()` 判定（`isPointerTy()`、`isVectorTy()`…）都基于它做一次比较，开销极低。

**(2) 原始类型的工厂——直接返回 context 单例**

[llvm/lib/IR/Type.cpp:282-293](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/IR/Type.cpp#L282-L293) 展示了最典型的唯一化写法：

```cpp
Type *Type::getVoidTy(LLVMContext &C)   { return &C.pImpl->VoidTy; }
Type *Type::getHalfTy(LLVMContext &C)   { return &C.pImpl->HalfTy; }
Type *Type::getFloatTy(LLVMContext &C)  { return &C.pImpl->FloatTy; }
Type *Type::getDoubleTy(LLVMContext &C) { return &C.pImpl->DoubleTy; }
```

整数类型同理，`getInt1Ty`…`getInt128Ty` 也是返回 `pImpl` 预置成员（[llvm/lib/IR/Type.cpp:306-311](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/IR/Type.cpp#L306-L311)）。这些方法的头文件声明集中在 [llvm/include/llvm/IR/Type.h:482-488](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/Type.h#L482-L488)。

**(3) 任意位宽整数——查表唯一化**

非预置位宽（如 `i42`）走 `IntegerType::get`，在 `pImpl->IntegerTypes[NumBits]` 这个 map 里查（[llvm/lib/IR/Type.cpp:348-370](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/IR/Type.cpp#L348-L370)）：

```cpp
IntegerType *IntegerType::get(LLVMContext &C, unsigned NumBits) {
  // ... 先把 1/8/16/32/64/128 这些常见位宽转给预置单例 ...
  IntegerType *&Entry = C.pImpl->IntegerTypes[NumBits]; // 查唯一化表
  if (!Entry)
    Entry = new (C.pImpl->Alloc) IntegerType(C, NumBits); // 未命中才分配
  return Entry;
}
```

注意 `IntegerType` 把位宽存进 `SubclassData`（24 位空间），所以类自身极小；位宽上限 `MAX_INT_BITS = 1<<23`（[llvm/include/llvm/IR/DerivedTypes.h:52-59](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/DerivedTypes.h#L52-L59)）。

**(4) 指针——不透明指针 + 地址空间**

现代 LLVM 使用**不透明指针**（opaque pointer），即所有指针都打印为 `ptr`，不再携带被指类型。`PointerType` 唯一的可变属性是**地址空间**（address space），存在 `SubclassData` 里（[llvm/include/llvm/IR/DerivedTypes.h:782](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/DerivedTypes.h#L782)）。工厂 `PointerType::get` 对地址空间 0 做了特化加速（[llvm/lib/IR/Type.cpp:911-921](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/IR/Type.cpp#L911-L921)）：

```cpp
PointerType *PointerType::get(LLVMContext &C, unsigned AddressSpace) {
  LLVMContextImpl *CImpl = C.pImpl;
  // 地址空间 0 最常见，单独缓存
  PointerType *&Entry = AddressSpace == 0 ? CImpl->AS0PointerType
                                          : CImpl->PointerTypes[AddressSpace];
  if (!Entry) Entry = new (CImpl->Alloc) PointerType(C, AddressSpace);
  return Entry;
}
```

`getUnqual(C)` 是 `get(C, 0)` 的简写，返回默认地址空间的 `ptr`。

**(5) 数组与向量——按「元素类型 + 元素数」唯一化**

[llvm/lib/IR/Type.cpp:817-827](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/IR/Type.cpp#L817-L827)（数组）与 [llvm/lib/IR/Type.cpp:867-883](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/IR/Type.cpp#L867-L883)（定长向量）都遵循同一套「以 `std::make_pair(元素类型, 元素数)` 为键查表」的范式。向量分两类：`FixedVectorType`（`<4 x float>`）与 `ScalableVectorType`（`<vscale x 4 x i32>`，用于 ARM SVE / RISC-V V），二者用同一张表但键里的 `ElementCount` 携带「是否可伸缩」标志（[llvm/lib/IR/Type.cpp:867-905](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/IR/Type.cpp#L867-L905)）。

**(6) 函数类型——把返回类型和参数列表塞进 `ContainedTys`**

`FunctionType` 的实现很有代表性：它把「返回类型 + 各参数类型」连续放在对象后面的内存里，再让基类的 `ContainedTys` 指过去，`NumContainedTys = 参数数 + 1`（[llvm/lib/IR/Type.cpp:412-429](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/IR/Type.cpp#L412-L429)）。工厂 `FunctionType::get` 同样按结构键在 `pImpl->FunctionTypes` 里唯一化（[llvm/lib/IR/Type.cpp:432-456](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/IR/Type.cpp#L432-L456)）。这就是为什么 `Type` 基类要提供 `subtypes()` / `getContainedType(i)` 这类遍历接口（[llvm/include/llvm/IR/Type.h:107-115](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/Type.h#L107-L115)）——派生类型的「内含类型」一律走这个统一出口。

**(7) 结构体——字面（literal）与命名（identified）两种**

结构体是少数「不总是按结构唯一化」的类型，需要特别留意（[llvm/include/llvm/IR/DerivedTypes.h:258-299](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/DerivedTypes.h#L258-L299) 的注释说得很清楚）：

- **字面结构体** `{ i32, float }`：用 `StructType::get(Context, {EltTypes})` 创建，按结构唯一化、必须有体（[llvm/lib/IR/Type.cpp:477-502](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/IR/Type.cpp#L477-L502)）。
- **命名结构体** `%st = type { i32, float }`：用 `StructType::create(Context, Name)` 创建一个「身份」，可以先不设体（opaque，打印为 `opaque`），之后再 `setBody`，按名字注册在 context 里（[llvm/lib/IR/Type.cpp:683-688](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/IR/Type.cpp#L683-L688)）。这是表达「递归/自引用类型」（如链表节点）的唯一途径：先 create 一个 opaque 身份，再 setBody 引用自身。

**(8) 目标扩展类型——给目标「留口子」**

`TargetExtType` 用一个字符串名字（如 `target("spirv.Image")`、`aarch64.svcount`、`wasm.externref`）描述目标专用类型，对目标无关优化「不可内省」，仅由目标后端解释（[llvm/lib/IR/Type.cpp:960-992](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/IR/Type.cpp#L960-L992)）。它是类型系统中较新的扩展点，初学者了解其存在即可。

> **类型类别速查表**

| 类别 | TypeID | 代表工厂方法 | `.ll` 文本 |
|---|---|---|---|
| 整数 | `IntegerTyID` | `IntegerType::get(C,N)` / `getInt32Ty(C)` | `i1`、`i32`、`i128` |
| 浮点 | `FloatTyID`/`DoubleTyID`/… | `getFloatTy(C)` / `getDoubleTy(C)` | `float`、`double`、`fp128` |
| 指针 | `PointerTyID` | `PointerType::get(C,AS)` / `getUnqual(C)` | `ptr`、`ptr addrspace(1)` |
| 数组 | `ArrayTyID` | `ArrayType::get(Elt,N)` | `[10 x i32]` |
| 定长向量 | `FixedVectorTyID` | `FixedVectorType::get(Elt,N)` | `<4 x float>` |
| 可伸缩向量 | `ScalableVectorTyID` | `ScalableVectorType::get(Elt,Min)` | `<vscale x 4 x i32>` |
| 字面结构体 | `StructTyID` | `StructType::get(C,{...})` | `{ i32, float }` |
| 命名结构体 | `StructTyID` | `StructType::create(C,"name")` | `%st = type { i32, float }` |
| 函数 | `FunctionTyID` | `FunctionType::get(Ret,{Params},VarArg)` | `i32 (i32, i32)` |
| 目标扩展 | `TargetExtTyID` | `TargetExtType::get(C,"name",...)` | `target("spirv.Image")` |
| 无返回 | `VoidTyID` | `getVoidTy(C)` | `void` |

#### 4.1.4 代码实践

**实践目标**：亲手构造几种典型类型，验证「同结构即同一对象」的唯一化性质，并确认文本形式。

**操作步骤（无需编译版，先建立直觉）**：

1. 编写一段极简 `.ll`，故意写多种类型，用 `llvm-as` 验证语法、`llvm-dis` 回看规范打印。把下面内容存为 `types.ll`：

   ```llvm
   ; 字面结构体 + 命名结构体 + 数组 + 向量 + 函数类型
   %node = type { i32, ptr }                      ; 命名结构体（可自引用）
   @g = global [4 x i32] zeroinitializer          ; 数组类型 [4 x i32]
   @v = global <4 x float> zeroinitializer        ; 定长向量 <4 x float>
   declare <vscale x 4 x i32> @f(<4 x i32>)       ; 可伸缩向量 + 函数类型
   ```

2. 执行 `llvm-as types.ll -o types.bc` 再 `llvm-dis types.bc -o -`，观察每类类型的文本形式是否与上表一致。

**操作步骤（构造型，需要已按 [u1-l3 构建系统](u1-l3-build-system.md) 构建过 LLVM，进阶）**：

下面这小段 C++（**示例代码**，需链接 `LLVMCore`）构造类型并用指针相等验证唯一化：

```cpp
#include "llvm/IR/LLVMContext.h"
#include "llvm/IR/Type.h"
#include "llvm/IR/DerivedTypes.h"
#include "llvm/Support/raw_ostream.h"
using namespace llvm;

int main() {
  LLVMContext C;
  Type *I32  = Type::getInt32Ty(C);
  Type *Ptr  = PointerType::getUnqual(C);                 // ptr
  Type *Vec  = FixedVectorType::get(Type::getFloatTy(C), 4); // <4 x float>

  errs() << "I32 = "; I32->print(errs()); errs() << "\n";
  errs() << "Ptr = "; Ptr->print(errs()); errs() << "\n";
  errs() << "Vec = "; Vec->print(errs()); errs() << "\n";

  // 唯一化验证：两次获取应是同一指针
  bool same = (Type::getInt32Ty(C) == I32);
  errs() << "getInt32Ty twice, same pointer? " << same << "\n";
  return 0;
}
```

**需要观察的现象 / 预期结果**：三个类型应分别打印 `i32`、`ptr`、`<4 x float>`；`same` 应为 `1`（true），证明同一 context 内相同类型共享对象。（确切输出格式以本地运行结果为准，**待本地验证**。）

#### 4.1.5 小练习与答案

**练习 1**：为什么判断两个 `Type` 是否相同只需比较指针？

**参考答案**：因为类型在 `LLVMContext` 内被唯一化（uniqued），结构相同的类型只存在一个实例（见 [Type.cpp:348-370](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/IR/Type.cpp#L348-L370) 的查表逻辑），指针相等即类型相等，无需逐字段比较。

**练习 2**：`[10 x [4 x i32]]` 是什么类型？用工厂 API 怎么构造？

**参考答案**：它是「元素类型为 `[4 x i32]`、元素数为 10」的 `ArrayType`，通过嵌套构造：`ArrayType::get(ArrayType::get(Type::getInt32Ty(C), 4), 10)`。

**练习 3**：字面结构体 `{i32, float}` 和命名结构体 `%st = type {i32, float}` 在创建 API 与唯一化行为上有何区别？

**参考答案**：字面结构体用 `StructType::get(Context, {I32Ty, FloatTy})` 创建，按结构唯一化、创建时必须有体；命名结构体用 `StructType::create(Context, "st")` 创建独立「身份」，可先保持 opaque 再 `setBody`（支持自引用），按名字注册在 context 中，相同结构但不同名字是不同对象。

---

### 4.2 常量表示

#### 4.2.1 概念说明

上一模块讲了「值的类型」；本模块讲「值本身」。LLVM 里有一类特殊值——**常量（Constant）**：它们的值在编译期就已完全确定。

常量有几个根本特性（[llvm/include/llvm/IR/Constants.h:8-16](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/Constants.h#L8-L16) 的文件注释）：

- **不可变**：一旦创建永不改变。
- **按结构等价完全共享**：两个结构等价的常量地址相同。
- **按需创建、永不释放**：调用者无需关心其生命周期。
- **常量本身就是 `Value`**：`class Constant : public User`（[llvm/include/llvm/IR/Constants.h:43](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/Constants.h#L43)）。回忆 [u3-l2](u3-l2-value-use-ssa.md)：`User = Instruction ∪ Constant`，所以常量可以、并且常常作为指令的操作数出现（例如 `add i32 %x, 1` 里的 `1` 就是一个 `ConstantInt`）。

常量子类大体分三层：

1. **`Constant`（基类，`public User`）**：所有常量的根，提供 `isNullValue()`、`isOneValue()` 等通用查询。
2. **`ConstantData`（无操作数的常量）**：直接把数据「内联」存自己体内，没有 use-list（[llvm/include/llvm/IR/Constants.h:48-81](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/Constants.h#L48-L81)）。典型代表是 `ConstantInt`、`ConstantPointerNull`。
3. **有操作数的常量**：`ConstantExpr`（常量表达式）、`ConstantStruct`/`ConstantArray`/`ConstantVector`（聚合常量），它们像指令一样引用其它 `Constant` 作为操作数。

> **两个最常用的常量子类**：
> - `ConstantInt`：整数/布尔常量，内部就一个 `APInt Val`（[llvm/include/llvm/IR/Constants.h:87-93](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/Constants.h#L87-L93)）。`.ll` 里写作 `i32 42`、`i1 true`。
> - `ConstantExpr`：编译期表达式，复用普通指令的操作码（`Add`/`BitCast`/`PtrToInt`/`GetElementPtr`…）。`.ll` 里写作 `ptrtoint (ptr @g to i64)` 这种带操作码的形式。

#### 4.2.2 核心流程

构造常量同样走静态工厂，但相比类型多了**常量折叠（constant folding）**这一步：

1. 调用 `ConstantXxx::get(...)`，例如 `ConstantInt::get(Int32Ty, 42)`。
2. 在 `LLVMContextImpl` 的对应表里查结构等价项（`IntConstants` / `ExprConstants` / `ArrayConstants` …）。命中返回同一对象——这是「结构等价共享」的来源。
3. 对于 `ConstantInt`，零值和一值还有专门的快速表 `IntZeroConstants` / `IntOneConstants`，因为它们出现频率极高。
4. 对于 `ConstantExpr`，工厂会**先尝试 `ConstantFold`**：若能折叠成更简单的常量，就直接返回后者，根本不构造 `ConstantExpr` 节点。例如 `ConstantExpr::getAdd(1, 2)` 最终返回的是 `ConstantInt(3)`，而不是一个 `add` 表达式节点。
5. 折叠不掉的才落成真正的 `ConstantExpr`，并按「操作码 + 操作数 + 类型」做唯一化。

判断「能不能折叠」的规则集中在 [llvm/lib/IR/ConstantFold.cpp](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/IR/ConstantFold.cpp)：二元运算先处理单位元（identity，如 `x + 0 = x`）与 poison 传播；cast 运算对空指针等已知量折叠。

> **一个直觉**：你可以把 `ConstantExpr` 理解为「一个内联在常量位置上的微型指令」。优化器希望在编译期就算出它的值；算得出就折叠掉，算不出（如全局变量的地址 `ptrtoint(ptr @g to i64)`，地址运行期才知）就保留为表达式节点。

#### 4.2.3 源码精读

**(1) `Constant : public User`——常量也是值**

[llvm/include/llvm/IR/Constants.h:43](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/Constants.h#L43) 的继承关系 `class Constant : public User` 决定了常量与指令在 IR 模型里「同级」：都能有类型、都能被 `Use` 引用、都能作为操作数。文件头注释（[Constants.h:8-16](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/Constants.h#L8-L16)）再次强调了不可变、结构等价共享、永不释放三原则——和类型如出一辙。

**(2) `ConstantInt` 的唯一化**

`ConstantInt` 的核心数据成员只有一个 `APInt Val`（[llvm/include/llvm/IR/Constants.h:91](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/Constants.h#L91)）。工厂 `ConstantInt::get(Context, APInt)` 的实现（[llvm/lib/IR/Constants.cpp:940-954](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/IR/Constants.cpp#L940-L954)）很值得读：

```cpp
ConstantInt *ConstantInt::get(LLVMContext &Context, const APInt &V) {
  LLVMContextImpl *pImpl = Context.pImpl;
  // 零和一走专用表，其它走 IntConstants
  std::unique_ptr<ConstantInt> &Slot =
      V.isZero()  ? pImpl->IntZeroConstants[V.getBitWidth()]
      : V.isOne() ? pImpl->IntOneConstants[V.getBitWidth()]
                  : pImpl->IntConstants[V];
  if (!Slot) {
    IntegerType *ITy = IntegerType::get(Context, V.getBitWidth());
    Slot.reset(new ConstantInt(ITy, V));   // 未命中才创建
  }
  return Slot.get();
}
```

注意它顺手用 `IntegerType::get` 取类型——这正好印证「常量先有类型，再有值」。而 `ConstantInt::get(IntegerType*, uint64_t)` 只是把 `uint64_t` 包成 `APInt` 再转给上面这个方法（[llvm/lib/IR/Constants.cpp:988-992](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/IR/Constants.cpp#L988-L992)）。读取则用 `getZExtValue()` / `getSExtValue()`（[Constants.h:168-174](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/Constants.h#L168-L174)）。

**(3) 空指针与紧凑数据常量**

`ConstantPointerNull` 把「是否为 null 值」记在 `SubclassOptionalData`，`.ll` 打印为 `ptr null`（[llvm/include/llvm/IR/Constants.h:716-744](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/Constants.h#L716-L744)）。而 `ConstantDataSequential`（`ConstantDataArray` / `ConstantDataVector` 的基类）则把整型/浮点元素**按字节紧密打包**存在自身里，而不是当作一串 `Value*` 操作数，从而大幅节省内存（[llvm/include/llvm/IR/Constants.h:755-857](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/Constants.h#L755-L857) 的注释解释了它的设计动机）。

**(4) `ConstantExpr`——先折叠、后唯一化**

`ConstantExpr` 把操作码存在 `Value::SubclassData`，复用普通 `Instruction` 的操作码集合（[llvm/include/llvm/IR/Constants.h:1316-1331](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/Constants.h#L1316-L1331)）。它的一大堆静态工厂（`getAdd`/`getBitCast`/`getPtrToInt`/`getCast`/`getGetElementPtr`…，见 [Constants.h:1348-1366](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/Constants.h#L1348-L1366)）最终都汇入二元运算入口 `ConstantExpr::get(Opcode, C1, C2)`（[llvm/lib/IR/Constants.cpp:2503-2543](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/IR/Constants.cpp#L2503-L2543)）：

```cpp
Constant *ConstantExpr::get(unsigned Opcode, Constant *C1, Constant *C2,
                            unsigned Flags, Type *OnlyIfReducedTy) {
  // ... 断言操作数类型一致、操作码合法 ...
  if (Constant *FC = ConstantFoldBinaryInstruction(Opcode, C1, C2))
    return FC;                          // 关键：先尝试折叠
  if (OnlyIfReducedTy == C1->getType()) return nullptr;
  Constant *ArgVec[] = {C1, C2};
  ConstantExprKeyType Key(Opcode, ArgVec, Flags);
  return C1->getContext().pImpl->ExprConstants.getOrCreate(C1->getType(), Key);
}
```

第一行的 `ConstantFoldBinaryInstruction` 就是折叠入口：能算出结果就直接返回常量。它的实现先处理单位元与 poison 传播（[llvm/lib/IR/ConstantFold.cpp:637-654](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/IR/ConstantFold.cpp#L637-L654)）。cast 类工厂同理：`getBitCast` 先走 `getFoldedCast`（[llvm/lib/IR/Constants.cpp:2484-2494](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/IR/Constants.cpp#L2484-L2494)），后者再委托 `ConstantFoldCastInstruction`（[ConstantFold.cpp:163-190](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/IR/ConstantFold.cpp#L163-L190)）。

**(5) 折叠的边界——什么时候不会被折叠**

`ConstantFoldCastInstruction` 在 [ConstantFold.cpp:178-180](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/IR/ConstantFold.cpp#L178-L180) 只对「值为 null 的常量」做折叠（→ 零值）；对一个普通全局变量 `@g`，它的地址在编译期未知，所以 `ptrtoint (ptr @g to i64)` 不会被折叠，会作为真正的 `ConstantExpr` 保留下来。这正是后续实践中用来观察「不可折叠常量表达式」的典型例子。

> **常量类别速查表**

| 类别 | 类 | `.ll` 示例 | 要点 |
|---|---|---|---|
| 整数/布尔常量 | `ConstantInt` | `i32 42`、`i1 true` | 内含 `APInt`，零/一走专用表 |
| 浮点常量 | `ConstantFP` | `double 3.140000e+00` | 内含 `APFloat` |
| 空指针 | `ConstantPointerNull` | `ptr null` | `ConstantData`，无操作数 |
| 紧凑数组/向量 | `ConstantDataArray`/`ConstantDataVector` | `[i32 1, i32 2]` | 字节紧密打包存储 |
| 聚合常量 | `ConstantStruct`/`ConstantArray`/`ConstantVector` | `{ i32 1, i32 2 }` | 由子 `Constant` 操作数组成 |
| 零值聚合 | `ConstantAggregateZero` | `zeroinitializer` | 结构等价共享 |
| 常量表达式 | `ConstantExpr` | `ptrtoint (ptr @g to i64)` | 复用指令操作码，先折叠后唯一化 |

#### 4.2.4 代码实践

**实践目标**：构造一个 `ConstantInt`，再分别构造一个「会被折叠」和一个「不会被折叠」的 `ConstantExpr`，打印文本并验证其类型归属。

**操作步骤（构造型，需已构建 LLVM，链接 `LLVMCore`）**：

```cpp
#include "llvm/IR/LLVMContext.h"
#include "llvm/IR/Module.h"
#include "llvm/IR/Constants.h"
#include "llvm/IR/DerivedTypes.h"
#include "llvm/IR/GlobalVariable.h"
#include "llvm/Support/raw_ostream.h"
using namespace llvm;

int main() {
  LLVMContext C;
  Type *I32 = Type::getInt32Ty(C);

  // (1) 普通整数常量
  ConstantInt *FortyTwo = ConstantInt::get(I32, 42);          // i32 42
  errs() << "int const  : "; FortyTwo->print(errs()); errs() << "\n";

  // (2) 会被折叠的常量表达式：1 + 2 -> ConstantInt(3)
  Constant *Folded = ConstantExpr::getAdd(
      ConstantInt::get(I32, 1), ConstantInt::get(I32, 2));
  errs() << "folded add : "; Folded->print(errs()); errs() << "\n";
  errs() << "isa<ConstantInt>? " << isa<ConstantInt>(Folded) << "\n";
  errs() << "isa<ConstantExpr>? " << isa<ConstantExpr>(Folded) << "\n";

  // (3) 不会被折叠的常量表达式：全局变量地址 ptrtoint
  Module M("demo", C);
  new GlobalVariable(M, I32, /*isConstant=*/false,
                     GlobalValue::ExternalLinkage, FortyTwo, "g");
  GlobalVariable *GV = M.getGlobalVariable("g");
  Constant *NonFolded = ConstantExpr::getPtrToInt(GV, Type::getInt64Ty(C));
  errs() << "non-fold   : "; NonFolded->print(errs()); errs() << "\n";
  errs() << "isa<ConstantExpr>? " << isa<ConstantExpr>(NonFolded) << "\n";
  return 0;
}
```

**需要观察的现象 / 预期结果**：

- `int const` 应为 `i32 42`。
- `folded add` 应为 `i32 3`，且 `isa<ConstantInt>` 为 `1`、`isa<ConstantExpr>` 为 `0`——证明 `getAdd` 折叠后返回的是 `ConstantInt`，并没有产生 `ConstantExpr` 节点。
- `non-fold` 应为 `ptrtoint (ptr @g to i64)`，`isa<ConstantExpr>` 为 `1`——因为全局地址编译期未知，无法折叠，保留为真正的常量表达式。

（确切文本格式以本地运行结果为准；上述为预期结果，**待本地验证**。）

**无构建环境时的替代实践（阅读型）**：在任一真实 `.ll`（例如用 `clang -S -emit-llvm` 生成）中搜索 `ptrtoint`、`getelementptr`、`zeroinitializer`、`i32 0` 等字样，逐一辨认它们分别属于哪种常量子类，并用本节的速查表自检。

#### 4.2.5 小练习与答案

**练习 1**：`ConstantExpr::getAdd(ConstantInt::get(I32,1), ConstantInt::get(I32,2))` 返回的对象上 `isa<ConstantExpr>` 为真还是假？为什么？

**参考答案**：为**假**。`getAdd` 内部会先调用 `ConstantFoldBinaryInstruction`（见 [Constants.cpp:2532](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/IR/Constants.cpp#L2532)），`1 + 2` 可在编译期算出 `3`，于是直接返回 `ConstantInt(3)`，根本不构造 `ConstantExpr`。

**练习 2**：为什么 `ConstantInt`、`ConstantPointerNull` 没有 use-list？

**参考答案**：它们派生自 `ConstantData`——无操作数、把数据直接内联在自身（见 [Constants.h:48-64](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/Constants.h#L48-L64)）。这类常量在无关模块间共享、从不基于 `GlobalValue`，对它们做 RAUW 没有意义，因此 `use_empty()` 恒为真，也不允许检查其 uses。

**练习 3**：给定一个全局变量 `@g`，举出一个**不会被折叠**的 `ConstantExpr`，并解释原因。

**参考答案**：`ConstantExpr::getPtrToInt(GV, Int64Ty)`，对应 `ptrtoint (ptr @g to i64)`。因为 `@g` 的最终地址在链接/加载后才确定、编译期未知，`ConstantFoldCastInstruction` 不会把它折叠成整数常量，于是保留为 `ConstantExpr`。

---

## 5. 综合实践

把本讲两个模块串起来：编写一个完整的小程序，**既构造多种类型、又构造常量与常量表达式，并把它们全部打印出来**。这正是本讲规格要求的「获取或构造 `i32`、`ptr`、`<4 x float>` 等类型，并构造一个 `ConstantInt` 与一个 `ConstantExpr`」的完整落地。

```cpp
#include "llvm/IR/LLVMContext.h"
#include "llvm/IR/Module.h"
#include "llvm/IR/Constants.h"
#include "llvm/IR/DerivedTypes.h"
#include "llvm/IR/GlobalVariable.h"
#include "llvm/Support/raw_ostream.h"
using namespace llvm;

int main() {
  LLVMContext C;

  // —— 类型构造 —— 全部经 context 唯一化
  Type *I32 = Type::getInt32Ty(C);                          // i32
  Type *Ptr = PointerType::getUnqual(C);                    // ptr
  Type *V4F = FixedVectorType::get(Type::getFloatTy(C), 4); // <4 x float>
  Type *Arr = ArrayType::get(I32, 10);                      // [10 x i32]

  // —— 常量构造 ——
  ConstantInt *CI = ConstantInt::get(I32, 42);              // i32 42
  Constant *Null = ConstantPointerNull::get(Ptr);           // ptr null

  // —— 常量表达式 ——
  Module M("demo", C);
  new GlobalVariable(M, I32, false, GlobalValue::ExternalLinkage, CI, "g");
  Constant *CE = ConstantExpr::getPtrToInt(M.getGlobalVariable("g"),
                                           Type::getInt64Ty(C));

  // —— 打印文本形式 ——
  auto put = [](const char *label, Type *T) {
    errs() << label; T->print(errs()); errs() << "\n";
  };
  auto putv = [](const char *label, Value *V) {
    errs() << label; V->print(errs()); errs() << "\n";
  };
  put("type i32      : ", I32);
  put("type ptr      : ", Ptr);
  put("type <4 x f>  : ", V4F);
  put("type [10xi32] : ", Arr);
  putv("const int     : ", CI);
  putv("const null    : ", Null);
  putv("const expr    : ", CE);

  // —— 唯一化自检 ——
  errs() << "i32 uniqued? " << (Type::getInt32Ty(C) == I32) << "\n";
  errs() << "42 uniqued ? " << (ConstantInt::get(I32, 42) == CI) << "\n";
  return 0;
}
```

**任务要求**：

1. 读懂程序，回答：哪些是 `Type`、哪些是 `Constant`？`CE` 为什么是 `ConstantExpr` 而非 `ConstantInt`？
2. 运行后核对类型与常量的文本输出是否与本讲的速查表一致。
3. 把 `CE` 改成 `ConstantExpr::getAdd(ConstantInt::get(I32,1), ConstantInt::get(I32,2))`，重新运行并解释输出为何从 `ptrtoint ...` 变成了一个整数常量。

（运行需先按 [u1-l3](u1-l3-build-system.md) 构建 LLVM 并把本程序链接到 `LLVMCore`；输出以本地结果为准。）

## 6. 本讲小结

- LLVM IR 的类型由 `Type` 及 `DerivedTypes.h` 中的派生类表示，种类由 `TypeID` 枚举标识；常用类别有整数、浮点、指针、数组、定长/可伸缩向量、结构体、函数类型、目标扩展类型。
- **类型在 `LLVMContext` 内唯一化**：原始类型是 `pImpl` 预置单例，派生类型按结构键查表，结构相同即同一对象——所以类型判等只需比指针。
- 结构体分字面（`StructType::get`，按结构唯一化、必有体）与命名（`StructType::create`，有身份、可先 opaque 再 setBody 以支持自引用）两种。
- **常量 `Constant : public User` 本身是值**，可作操作数；同样不可变、按结构等价共享、永不释放。
- `ConstantInt` 内含 `APInt`，零值/一值走专用表；`ConstantDataSequential` 把元素紧密打包存储以省内存。
- `ConstantExpr` 复用指令操作码，**构造时先尝试常量折叠**：算得出就退化为更简单的常量（如 `1+2 → 3`），算不出（如 `ptrtoint(ptr @g to i64)`）才保留为表达式节点。

## 7. 下一步学习建议

- 下一讲 [u3-l4 IRBuilder](u3-l4-irbuilder.md) 会大量用到本讲内容：`IRBuilder` 每创建一条指令都要指定返回 `Type`，而操作数里的编译期已知量都来自 `Constant`。把本讲的类型与常量记牢，是理解 `IRBuilder` 的前提。
- 之后 [u3-l5 AsmParser 与 Bitcode](u3-l5-asm-bitcode.md) 会讲这些类型与常量如何被序列化成 `.ll` 文本与 `.bc` 位码——届时你会看到，文本里那串 `i32`、`ptr null`、`ptrtoint (...)` 正是本讲这些内存对象的打印产物。
- 建议带着本讲的速查表，回头重读 [u2-l2](u2-l2-read-write-ir.md) 里的 `.ll` 示例，逐一辨认其中的类型与常量子类，巩固「内存对象 ↔ 文本」的对应关系。
