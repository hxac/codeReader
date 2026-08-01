# IRBuilder：以代码构造 IR

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `IRBuilder` 是什么、解决了什么问题，以及它「构造 + 插入」一体的设计。
- 理解**插入点（Insertion Point）**——它由「哪个 `BasicBlock`」加「块内哪个位置」共同决定，并能用 `SetInsertPoint` / `saveIP` / `restoreIP` 精确控制新指令落在哪里。
- 用 `IRBuilder` 的 `CreateXxx` 系列方法生成运算、内存、控制流、类型转换、比较等各类指令。
- 理解 `IRBuilder` 的**常量折叠（Constant Folding）**行为：当操作数全是编译期常量时，它可能直接返回一个 `Constant` 而不产生任何指令。
- 独立用 C++ 代码构造出一段「可被 `verifyFunction` 校验通过」的 IR。

## 2. 前置知识

本讲建立在前几讲的认知之上，复用以下概念（不再重复展开）：

- **IR 的层次结构**（u3-l1）：`Module ⊃ Function ⊃ BasicBlock ⊃ Instruction`，每层用带符号表的链表管理所有权；指令只有「插进某个 `BasicBlock`」才真正归属于 IR。
- **Value / Use**（u3-l2）：`Instruction` 是 `Value` 的子类，构造出的指令本身就是可被引用的值；指令的操作数是 `Value*`。
- **Type 与 Constant**（u3-l3）：`Type` 在 `LLVMContext` 内唯一化；`Constant`（如 `ConstantInt`）**本身就是 `Value`**，不需要插入到任何基本块里即可被引用。这一条是理解本讲「常量折叠」的关键。
- **`.ll` 文本 IR**（u2-l2）：基本块、`alloca`/`store`/`load`/`ret` 等指令的文本形态。

一个核心直觉先放在这里：**手写指令需要两步——`new` 出指令对象、再把它插进某个 `BasicBlock`。** `IRBuilder` 把这两步合并成一个 `CreateXxx` 调用，并额外帮你管理「插到哪里」和「能不能折叠成常量」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [llvm/include/llvm/IR/IRBuilder.h](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/IRBuilder.h) | `IRBuilder` 与 `IRBuilderBase` 的主体定义，包含所有 `CreateXxx` 方法与插入点管理（本讲主战场）。 |
| [llvm/lib/IR/IRBuilder.cpp](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/IR/IRBuilder.cpp) | 部分复杂方法的实现（如 `CreateSelect`、intrinsic 折叠、`createCallHelper`）。 |
| [llvm/include/llvm/IR/IRBuilderFolder.h](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/IRBuilderFolder.h) | 折叠策略的抽象接口 `IRBuilderFolder`，定义了 `FoldBinOp` / `FoldCast` 等纯虚方法。 |
| [llvm/include/llvm/IR/ConstantFolder.h](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/ConstantFolder.h) | 默认折叠策略 `ConstantFolder`：操作数全为常量时返回折叠后的 `Constant`，否则返回 `nullptr`。 |
| [llvm/include/llvm/IR/NoFolder.h](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/NoFolder.h) | 「不折叠」策略 `NoFolder`：所有 `FoldXxx` 都返回 `nullptr`，强制生成真实指令。 |
| [llvm/examples/Kaleidoscope/Chapter3/toy.cpp](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/examples/Kaleidoscope/Chapter3/toy.cpp) | 官方教学示例，展示 `IRBuilder<>` 在真实前端里如何被实例化与使用。 |

---

## 4. 核心概念与源码讲解

### 4.1 IRBuilder 是什么：构造 IR 的便捷 API

#### 4.1.1 概念说明

如果没有 `IRBuilder`，要往一个 `BasicBlock` 里加一条加法指令，你得这样写：

```cpp
// 伪代码：手写两步
Instruction *I = BinaryOperator::CreateAdd(L, R);
I->insertInto(TheBB, TheBB->end());   // 还要自己算插入位置
I->setName("sum");
```

这段代码有三个痛点：① 指令的「构造」和「插入」是分开的两步，容易漏掉插入导致指令悬空（不归属任何块）；② 插入位置要自己维护；③ 当 `L`、`R` 都是常量时，本可以直接折叠成一个 `Constant`、根本不该生成指令，但手写代码不会自动处理。

`IRBuilder` 把这三件事打包成一个一致的接口。它的核心设计是「**三件套**」：

- **`IRBuilderBase`**：持有状态（当前插入点、`LLVMContext`、一个折叠器、一个插入器），并提供海量 `CreateXxx` 方法。
- **Folder（折叠器）**：一个实现 `IRBuilderFolder` 接口的对象，决定「这次操作能不能折叠成常量」。默认是 `ConstantFolder`。
- **Inserter（插入器）**：一个实现 `InsertHelper` 的对象，决定「新指令插到哪里」。默认是 `IRBuilderDefaultInserter`（插到当前插入点）。

这三者通过模板参数组合，最终用户只需写 `IRBuilder<> Builder(...)`。

#### 4.1.2 核心流程

一条 `CreateXxx` 调用的统一套路是「**先折叠，后插入**」：

```
CreateXxx(操作数...)
  │
  ├─ 调用 Folder.FoldXxx(操作数...) 尝试折叠
  │     ├─ 折叠成功 → 返回一个 Constant，什么都不插入（CreateXxx 直接 return）
  │     └─ 折叠失败（返回 nullptr）→ 继续
  │
  ├─ new 出对应的 Instruction 对象
  │
  └─ Insert(I, Name)
        ├─ Inserter.InsertHelper(I, Name, InsertPt)  // 把 I 插到当前插入点
        └─ SetInstDebugLocation(I)                   // 附上当前调试位置
```

注意第二步里那个「折叠成功就直接 return」的分支：这正是常量折叠的本质——**结果是一个 `Constant`（本身就是 `Value`，见 u3-l3），不需要也不应该被插入到任何基本块里**。

#### 4.1.3 源码精读

先看文件头对这个类的定位（[llvm/include/llvm/IR/IRBuilder.h:9-11](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/IRBuilder.h#L9-L11)）：「`IRBuilder` 是创建 LLVM 指令的一种便捷、一致的接口」。

`IRBuilderBase` 持有的核心状态在 [IRBuilder.h:119-124](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/IRBuilder.h#L119-L124)：

```cpp
protected:
  BasicBlock *BB;                 // 当前插入到哪个基本块
  BasicBlock::iterator InsertPt;  // 块内插入位置（一个迭代器）
  LLVMContext &Context;
  const IRBuilderFolder &Folder;        // 折叠器（引用）
  const IRBuilderDefaultInserter &Inserter; // 插入器（引用）
```

「先折叠、后插入」在 `Insert` 模板里体现得最清楚。模板主版本负责插入真实指令（[IRBuilder.h:145-150](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/IRBuilder.h#L145-L150)）：

```cpp
template<typename InstTy>
InstTy *Insert(InstTy *I, const Twine &Name = "") const {
  Inserter.InsertHelper(I, Name, InsertPt);  // 插到当前插入点
  SetInstDebugLocation(I);                   // 附调试位置
  return I;
}
```

而关键在于那个**针对 `Constant*` 的空操作重载**（[IRBuilder.h:153-155](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/IRBuilder.h#L153-L155)）：

```cpp
/// No-op overload to handle constants.
Constant *Insert(Constant *C, const Twine& = "") const {
  return C;   // 常量无需插入，原样返回
}
```

这个重载是「常量不进基本块」的安全网：当某个 `CreateXxx` 路径把折叠结果（一个 `Constant`）传进 `Insert` 时，它什么都不做，直接把常量还回去。而 `Insert(Value*)`（[IRBuilder.h:157-162](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/IRBuilder.h#L157-L162)）则用 `dyn_cast` 在「指令」与「常量」两条路之间分发。

> 为什么折叠要在 `CreateXxx` 里做、而不是等优化 pass 来做？因为构造期折叠能让生成的 IR 一开始就更精简，避免产生「只引用常量的无用指令」。这也呼应 u3-l3 里讲过的 `ConstantExpr` 构造时即尝试折叠的设计。

#### 4.1.4 代码实践

**实践目标**：在源码里亲眼确认「常量不进基本块」这条结论。

**操作步骤**：

1. 打开 [ConstantFolder.h:44-54](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/ConstantFolder.h#L44-L54)，阅读 `FoldBinOp`。
2. 对照 [IRBuilder.h:1422-1429](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/IRBuilder.h#L1422-L1429) 的 `CreateAdd`，看清它先调 `Folder.FoldNoWrapBinOp`、折叠成功就 `return V`。

**需要观察的现象**：`CreateAdd` 在「折叠成功」与「折叠失败」两条路径上，只有后者才会走到 `Insert` / `CreateInsertNUWNSWBinOp`。

**预期结果**：你能用一句话说出——「当两个操作数都是 `Constant` 时，`CreateAdd` 不会生成任何 `add` 指令，而是直接返回折叠后的常量」。具体运行行为见 4.2.4 与综合实践，此处为「源码阅读型实践」，无需运行。

#### 4.1.5 小练习与答案

**练习 1**：`IRBuilderBase` 里的 `Folder` 和 `Inserter` 为什么是「引用」而非「对象」？

**参考答案**：它们由派生类模板 `IRBuilder<FolderTy, InserterTy>` 作为**值成员**持有（见 [IRBuilder.h:2894-2896](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/IRBuilder.h#L2894-L2896)），再把引用传给基类 `IRBuilderBase`。这样基类的 `CreateXxx` 通过引用调用虚函数（多态），而对象的生命周期由最派生的 `IRBuilder` 管理，避免了对象切片，也保证基类构造时引用已绑定到具体策略。

**练习 2**：`Insert(Constant*, ...)` 为什么必须存在？删掉它会怎样？

**参考答案**：因为部分 `CreateXxx`（例如 [IRBuilder.h:2295](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/IRBuilder.h#L2295) 的 `CreatePointerCast`）会把折叠器返回的 `Constant*` 直接喂给 `Insert`。没有这个空操作重载，模板主版本会试图把一个 `Constant` 当作 `Instruction` 去插入，引发类型错误或断言失败。

---

### 4.2 插入点（InsertPoint）与基本块的关系

#### 4.2.1 概念说明

「插入点」回答一个问题：**下一条 `CreateXxx` 生成的指令，会落到哪个基本块的哪个位置？** 它由两个量唯一确定：

- `BB`：目标 `BasicBlock*`；
- `InsertPt`：该块内的一个 `BasicBlock::iterator`（迭代器），新指令插在它**之前**。

构造一个 `IRBuilder` 时若指定了 `BasicBlock*`，插入点默认是「该块末尾」（`BB->end()`）；若指定了某条 `Instruction*`，插入点是「该指令之前」。每个 `IRBuilder` 对象**自带一份**插入点状态，所以多个 builder 可以同时瞄准不同位置互不干扰。

#### 4.2.2 核心流程

插入点的生命周期与典型用法：

```
构造 IRBuilder(BB)        → 插入点 = BB 末尾
   │
   ├─ CreateXxx ...        → 连续往末尾追加（最常见：顺序生成）
   │
   ├─ SetInsertPoint(X)    → 把插入点搬到别处
   │     • X 是 BasicBlock*  → 搬到该块末尾
   │     • X 是 Instruction* → 搬到该指令之前
   │     • (BB, iterator)    → 搬到精确位置
   │
   ├─ saveIP() / restoreIP() → 临时切到别处插几条，再恢复原位
   │
   └─ ClearInsertionPoint()  → 清空：之后生成的指令不会被插入（悬空）
```

当需要在「别的地方临时插几条指令、完事再回到原处继续」时（这在写 pass 改写 IR 时极其常见），`saveIP()` / `restoreIP()` 是标准手段；若想用 RAII 自动恢复，则用 `InsertPointGuard`。

#### 4.2.3 源码精读

插入点的几个核心方法都在 [IRBuilder.h:170-218](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/IRBuilder.h#L170-L218)。读其中三个：

```cpp
// 清空插入点：之后生成的指令不会被插入到任何块
void ClearInsertionPoint() {
  BB = nullptr;
  InsertPt = BasicBlock::iterator();
}

// 查询：当前插入点
BasicBlock *GetInsertBlock() const { return BB; }
BasicBlock::iterator GetInsertPoint() const { return InsertPt; }

// 追加到某块末尾
void SetInsertPoint(BasicBlock *TheBB) {
  BB = TheBB;
  InsertPt = BB->end();
}
```

`SetInsertPoint(Instruction *I)`（[IRBuilder.h:188-193](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/IRBuilder.h#L188-L193)）则把插入点定位到「该指令之前」，并顺手把调试位置同步成该指令的位置；`SetInsertPointPastAllocas`（[IRBuilder.h:215-218](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/IRBuilder.h#L215-L218)）专门把插入点放到入口块里所有静态 `alloca` 之后——这是「插 `alloca` 该放哪」的官方答案。

`InsertPoint` 是一个可保存的「插入点快照」（[IRBuilder.h:246-263](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/IRBuilder.h#L246-L263)），配合三件套使用：

```cpp
InsertPoint saveIP() const { return InsertPoint(GetInsertBlock(), GetInsertPoint()); }
void restoreIP(InsertPoint IP) {
  if (IP.isSet()) SetInsertPoint(IP.getBlock(), IP.getPoint());
  else            ClearInsertionPoint();
}
```

若不想手动 restore，用 RAII 守卫 `InsertPointGuard`（[IRBuilder.h:364-382](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/IRBuilder.h#L364-L382)）：构造时存档，析构时自动恢复插入点**和**调试位置。注意它是不可拷贝的（删除了拷贝构造），保证一个守卫对应一次恢复。

> 插入点为什么用「迭代器」而非「指令指针」？因为基本块内的指令会随插入而增减，迭代器能稳定指向「某条指令之前」这个缝隙；直接存 `Instruction*` 在该指令被删除时会悬空。`InsertPointGuard` 内部用 `AssertingVH<BasicBlock>`（一种会自检的句柄，见 u3-l2 的 ValueHandle）来保证块被删除时能及时报错。

#### 4.2.4 代码实践

**实践目标**：亲眼看到「插入点决定指令落点」「常量折叠让指令消失」。

**操作步骤**：

1. 阅读官方示例 [llvm/examples/Kaleidoscope/Chapter3/toy.cpp:496-498](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/examples/Kaleidoscope/Chapter3/toy.cpp#L496-L498)：先 `BasicBlock::Create(...)` 建入口块，再 `Builder->SetInsertPoint(BB)` 把插入点搬过去。
2. 阅读 [toy.cpp:529-530](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/examples/Kaleidoscope/Chapter3/toy.cpp#L529-L530)：`IRBuilder<>` 用一个 `LLVMContext` 构造；此时没有插入点（`ClearInsertionPoint`），直到 `SetInsertPoint(BB)` 才生效。

**需要观察的现象**：在 `SetInsertPoint(BB)` **之前**调用任何 `CreateXxx`，生成的指令不会进入任何基本块（除非该指令可折叠成常量）。

**预期结果**：你能解释为什么 Kaleidoscope 的 `FunctionAST::codegen` 必须在调用 `Body->codegen()`（后者会调 `CreateXxx`）之前先 `SetInsertPoint(BB)`。本项为源码阅读型实践，运行验证留给综合实践（第 5 节）。

#### 4.2.5 小练习与答案

**练习 1**：以下两段代码产出的 IR 有何不同？

- (a) `IRBuilder<> B(Ctx); B.CreateRet(B.getInt32(42));`
- (b) `IRBuilder<> B(BB); B.CreateRet(B.getInt32(42));`

**参考答案**：(a) 中 `B` 没有设置插入点（构造时 `ClearInsertionPoint`），`CreateRet` 生成的 `ret` 指令无处可插，成为悬空指令（内存泄漏且不归属任何块，模块打印时看不到它）；(b) 中插入点设在 `BB` 末尾，`ret i32 42` 被正确插入 `BB`。

**练习 2**：`InsertPointGuard` 和手动 `saveIP`/`restoreIP` 相比，优势是什么？

**参考答案**：异常安全。即便函数中途提前 `return` 或抛异常，RAII 析构也会恢复插入点与调试位置；手动写法在这种路径上容易漏 restore。代价是它额外保存了调试位置（`DbgLoc`），这点手动 `restoreIP` 不会自动恢复。

---

### 4.3 创建各类指令：运算、内存、控制流

#### 4.3.1 概念说明

`IRBuilder` 把 LLVM IR 的指令大致分成几大类，每类对应一组 `CreateXxx`：

| 类别 | 代表方法 | 产出指令 |
| --- | --- | --- |
| 整数运算 | `CreateAdd` / `CreateSub` / `CreateMul` / `CreateShl` / `CreateAnd` | `add` / `sub` / `mul` / `shl` / `and` … |
| 浮点运算 | `CreateFAdd` / `CreateFSub` / `CreateFMul` | `fadd` / `fsub` / `fmul` |
| 内存 | `CreateAlloca` / `CreateLoad` / `CreateStore` | `alloca` / `load` / `store` |
| 控制流（终结指令） | `CreateRet` / `CreateBr` / `CreateCondBr` / `CreateSwitch` | `ret` / `br` / `switch` |
| 类型转换 | `CreateZExt` / `CreateTrunc` / `CreateBitCast` / `CreateIntToPtr` | `zext` / `trunc` / `bitcast` / `inttoptr` |
| 比较 | `CreateICmpEQ` / `CreateFCmpOLT` … / `CreateICmp` / `CreateFCmp` | `icmp` / `fcmp` |
| 派生指针运算 | `CreateGEP` / `CreateInBoundsGEP` | `getelementptr` |
| 选择/调用 | `CreateSelect` / `CreateCall` / `CreatePHI` | `select` / `call` / `phi` |

它们在用法上高度一致：传入操作数（`Value*`）和可选的名字（`Twine`），返回表示结果的 `Value*`（多数情况下其实是 `Instruction*`）。名字只是给指令起个可读的寄存器名（u2-l2 讲过 `%名字` 与 `%数字` 等价），不影响语义。

#### 4.3.2 核心流程

以内存三件套（`alloca` → `store` → `load`）为例，这是实现「一个可变局部变量」的标准模式：

```
%slot = alloca i32            ; CreateAlloca(i32Ty)        → 返回 ptr（不透明指针）
store i32 42, ptr %slot       ; CreateStore(getInt32(42), slot)
%v = load i32, ptr %slot      ; CreateLoad(i32Ty, slot)    → 返回 i32
ret i32 %v                    ; CreateRet(v)
```

注意三点：① 现代 LLVM 用**不透明指针**（u3-l3），`alloca` 与 `store`/`load` 里的指针类型都打印为 `ptr`；② `CreateLoad` 需要**显式给出加载类型**（`i32`），因为单凭 `ptr` 推不出元素类型；③ `alloca` 返回的指针本身也是一个 `Value`，可作为后续 `store`/`load` 的操作数。

整数运算大多带可选的溢出标志。以 `add` 为例，`CreateAdd(L, R, Name, HasNUW, HasNSW)`（[IRBuilder.h:1422-1429](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/IRBuilder.h#L1422-L1429)）可附加 `nuw`（No Unsigned Wrap，无符号不回绕）/ `nsw`（No Signed Wrap，有符号不回绕）。这两者告诉优化器「这个运算不会溢出」，从而允许更激进的变换。带溢出标志的数学含义：

- 无符号回绕：结果对 \( 2^{N} \) 取模（\( N \) 为位宽）；
- `nuw`/`nsw` 断言：在给定解释下结果未溢出，即若溢出则该值为 **poison**（可被优化器任意假定）。

控制流指令是**终结指令**（terminator），每个基本块必须且只能以一条终结指令结尾（u2-l2）。`CreateRet` / `CreateBr` / `CreateCondBr` 直接产出 `ret` / `br`。注意 `CreateCondBr` 还能附带分支权重元数据（`BranchWeights`），供概率性优化（如 PGO）使用。

#### 4.3.3 源码精读

**内存三件套**。`CreateAlloca`（[IRBuilder.h:1886-1892](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/IRBuilder.h#L1886-L1892)）：

```cpp
AllocaInst *CreateAlloca(Type *Ty, Value *ArraySize = nullptr,
                         const Twine &Name = "") {
  const DataLayout &DL = BB->getDataLayout();
  Align AllocaAlign = DL.getPrefTypeAlign(Ty);   // 对齐取自数据布局
  unsigned AddrSpace = DL.getAllocaAddrSpace();
  return Insert(new AllocaInst(Ty, AddrSpace, ArraySize, AllocaAlign), Name);
}
```

注意它依赖 `BB->getDataLayout()`——所以调用 `CreateAlloca` 前必须有有效的插入块 `BB`。`CreateStore`（[IRBuilder.h:1925-1927](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/IRBuilder.h#L1925-L1927)）转发到 `CreateAlignedStore`，后者在未指定对齐时按 `DataLayout` 的 ABI 对齐补默认值（[IRBuilder.h:1953-1960](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/IRBuilder.h#L1953-L1960)）。`CreateLoad`（[IRBuilder.h:1910-1912](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/IRBuilder.h#L1910-L1912)）同理转发到 `CreateAlignedLoad`（[IRBuilder.h:1944-1951](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/IRBuilder.h#L1944-L1951)），同样在未给对齐时补默认。

**整数运算**。`CreateAdd`（[IRBuilder.h:1422-1429](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/IRBuilder.h#L1422-L1429)）是「先折叠、后插入」的标准范例：

```cpp
Value *CreateAdd(Value *LHS, Value *RHS, const Twine &Name = "",
                 bool HasNUW = false, bool HasNSW = false) {
  if (Value *V =
          Folder.FoldNoWrapBinOp(Instruction::Add, LHS, RHS, HasNUW, HasNSW))
    return V;                                     // 折叠成功：直接返回常量
  return CreateInsertNUWNSWBinOp(Instruction::Add, LHS, RHS, Name,
                                 HasNUW, HasNSW);  // 否则造指令并插入
}
```

带 `nuw`/`nsw` 的插入由私有助手 `CreateInsertNUWNSWBinOp` 完成（[IRBuilder.h:1366-1374](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/IRBuilder.h#L1366-L1374)）：先 `Insert(BinaryOperator::Create(...))`，再按标志位调 `setHasNoUnsignedWrap()` / `setHasNoSignedWrap()`。

**终结指令**。`CreateRet` / `CreateRetVoid` / `CreateBr` / `CreateCondBr` 都很短（[IRBuilder.h:1187-1221](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/IRBuilder.h#L1187-L1221)），例如：

```cpp
ReturnInst *CreateRet(Value *V) { return Insert(ReturnInst::Create(Context, V)); }
UncondBrInst *CreateBr(BasicBlock *Dest) { return Insert(UncondBrInst::Create(Dest)); }
```

它们不经过折叠器（返回/跳转没有「常量结果」可折叠），直接 `Insert`。

**真实前端的用法**。Kaleidoscope Ch3 的 `BinaryExprAST::codegen` 用 `IRBuilder` 把 AST 节点翻译成 IR（[toy.cpp:430-440](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/examples/Kaleidoscope/Chapter3/toy.cpp#L430-L440)）：

```cpp
case '+': return Builder->CreateFAdd(L, R, "addtmp");
case '<':
  L = Builder->CreateFCmpULT(L, R, "cmptmp");
  return Builder->CreateUIToFP(L, Type::getDoubleTy(*TheContext), "booltmp");
```

这里 `Builder` 是一个全局 `std::unique_ptr<IRBuilder<>>`（[toy.cpp:404](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/examples/Kaleidoscope/Chapter3/toy.cpp#L404)），在 `main` 里由 `std::make_unique<IRBuilder<>>(*TheContext)` 构造（[toy.cpp:530](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/examples/Kaleidoscope/Chapter3/toy.cpp#L530)）。这段代码集中体现了本讲三个要点：用 `CreateXxx` 构造运算与类型转换指令、用名字（`"addtmp"`）给中间值起名、以及结果以 `Value*` 流转。

#### 4.3.4 代码实践

**实践目标**：阅读并复述「`add` 的 `nuw`/`nsw` 标志如何被设置」。

**操作步骤**：

1. 阅读 [IRBuilder.h:1422-1429](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/IRBuilder.h#L1422-L1429)（`CreateAdd`）与 [IRBuilder.h:1366-1374](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/IRBuilder.h#L1366-L1374)（`CreateInsertNUWNSWBinOp`）。
2. 对照便捷方法 `CreateNSWAdd` / `CreateNUWAdd`（[IRBuilder.h:1431-1437](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/IRBuilder.h#L1431-L1437)），看它们如何把对应标志置真后转调 `CreateAdd`。

**需要观察的现象**：标志位是在「指令对象已创建、尚未返回」之间通过 setter 设置的，而非构造函数参数。

**预期结果**：你能说出 `Builder.CreateNSWAdd(a, b)` 与 `Builder.CreateAdd(a, b, "", /*HasNUW=*/false, /*HasNSW=*/true)` 完全等价，最终产出 `add nsw i32 ...`。本项为源码阅读型实践。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `CreateLoad` 必须显式传「加载类型」`Ty`，而 `CreateStore` 不需要？

**参考答案**：在不透明指针模型下，指针统一为 `ptr`，单凭它推不出指向对象的类型。`store` 的「被存值」自带类型（`Val->getType()`），故可从中推断；`load` 只有指针、没有「结果值」，必须由调用方显式告诉它要加载成什么类型。

**练习 2**：`CreateRet` 为什么不经过 `Folder`，而 `CreateAdd` 要？

**参考答案**：`add` 是可折叠的纯计算——两个常量相加可得到一个常量结果；`ret` 是终结指令，其「结果」是控制流转移，没有可在编译期求值的常量等价物，故无折叠可言。

---

### 4.4 常量折叠：Folder 策略与 NoFolder

#### 4.4.1 概念说明

「常量折叠」指：当一条指令的所有操作数都是编译期已知的 `Constant` 时，直接在编译期算出结果常量，**不生成该指令**。例如 `1 + 2` 直接得 `3`，而不产生 `add i32 1, 2`。

`IRBuilder` 把「是否折叠、如何折叠」抽成了一个可替换的策略——`IRBuilderFolder` 接口。仓库内置三个实现：

- **`ConstantFolder`**（默认）：操作数全为常量时返回折叠结果，否则返回 `nullptr`。只做「与目标无关的最小折叠」。
- **`TargetFolder`**：在 `ConstantFolder` 基础上结合 `DataLayout` 做更多折叠（如结合目标指针大小折叠 `ptrtoint`）。
- **`NoFolder`**：所有 `FoldXxx` 恒返回 `nullptr`，**绝不折叠**，强制每条操作都生成真实指令。

为什么需要 `NoFolder`？源码注释直言：它是给「想看清 IR 到底长什么样、不想被折叠藏起细节」的学习者和调试者用的。在改写 IR 的 pass 里，有时你也希望保留指令的原始形态而不被提前折叠。

#### 4.4.2 核心流程

`CreateXxx` 与 Folder 的协作：

```
CreateXxx(L, R)
  │
  ├─ Folder.FoldXxx(L, R)
  │     ConstantFolder:  if (isa<Constant>(L) && isa<Constant>(R)) 折叠; else nullptr
  │     NoFolder:        恒返回 nullptr
  │
  ├─ 返回非空 → 直接 return 该 Constant（无指令）
  └─ 返回 nullptr → new 指令 → Insert
```

折叠的判定极其朴素：**「两个操作数是否都是 `Constant`」**。若是，调 `ConstantFoldBinaryInstruction`（或 `ConstantExpr::get`）算出结果；若否，返回 `nullptr` 表示「折叠不了，请照常生成指令」。

#### 4.4.3 源码精读

`IRBuilderFolder` 是纯抽象接口，列出一组 `FoldXxx` 纯虚方法（[IRBuilderFolder.h:26-90](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/IRBuilderFolder.h#L26-L90)）。文件头注释点明它的三个实现（[IRBuilderFolder.h:9-11](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/IRBuilderFolder.h#L9-L11)）：`ConstantFolder`（默认）、`TargetFolder`、`NoFolder`。

`ConstantFolder::FoldBinOp`（[ConstantFolder.h:44-54](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/ConstantFolder.h#L44-L54)）就是「双常量才折叠」的典型：

```cpp
Value *FoldBinOp(Instruction::BinaryOps Opc, Value *LHS, Value *RHS) const override {
  auto *LC = dyn_cast<Constant>(LHS);
  auto *RC = dyn_cast<Constant>(RHS);
  if (LC && RC) {
    if (ConstantExpr::isDesirableBinOp(Opc))
      return ConstantExpr::get(Opc, LC, RC);          // 保留为常量表达式
    return ConstantFoldBinaryInstruction(Opc, LC, RC); // 直接算出常量
  }
  return nullptr;   // 至少一个非常量 → 折叠不了
}
```

这里出现两个分支值得注意（呼应 u3-l3）：`isDesirableBinOp` 为真时，刻意保留成 `ConstantExpr`（让后续优化能看见这层结构）；否则直接求值成具体常量。类型转换的折叠 `FoldCast`（[ConstantFolder.h:175-183](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/ConstantFolder.h#L175-L183)）同理。

`NoFolder` 的实现则是清一色的「返回 `nullptr`」（[NoFolder.h:49-52](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/NoFolder.h#L49-L52)）：

```cpp
Value *FoldBinOp(Instruction::BinaryOps Opc, Value *LHS, Value *RHS) const override {
  return nullptr;   // 永不折叠
}
```

> 文件头注释（[NoFolder.h:9-17](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/NoFolder.h#L9-L17)）还点出一个有趣的细节：由于常量本身无法「不折叠」（常量就是常量），`NoFolder` 在该返回常量的地方会改成返回一条指令。换句话说，`NoFolder` 让 `IRBuilder` 的行为尽量贴近「你写什么就生成什么」。

`IRBuilder` 模板的第一个参数就是 Folder 类型（[IRBuilder.h:2891-2893](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/IRBuilder.h#L2891-L2893)）：

```cpp
template <typename FolderTy = ConstantFolder,
          typename InserterTy = IRBuilderDefaultInserter>
class IRBuilder : public IRBuilderBase { ... };
```

所以 `IRBuilder<>` 等价于 `IRBuilder<ConstantFolder, IRBuilderDefaultInserter>`；要禁用折叠就写 `IRBuilder<NoFolder>`。注意构造时要把 Folder 对象传进去（见 [IRBuilder.h:2905-2908](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/IRBuilder.h#L2905-L2908)）。

折叠也发生在 intrinsic（内联函数）上。`CreateIntrinsic`（[IRBuilder.cpp:964-978](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/IR/IRBuilder.cpp#L964-L978)）先 `Folder.FoldIntrinsic(...)`，折叠不了才调 `CreateIntrinsicWithoutFolding` 真正造 `call`。而带「WithoutFolding」后缀的方法（[IRBuilder.cpp:944-951](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/IR/IRBuilder.cpp#L944-L951)）刻意绕过折叠，内部用 `createCallHelper`（[IRBuilder.cpp:181-188](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/IR/IRBuilder.cpp#L181-L188)）落出 `CallInst`。

#### 4.4.4 代码实践

**实践目标**：对比 `IRBuilder<>`（默认折叠）与 `IRBuilder<NoFolder>`（不折叠）对同一组操作的产出差异。

**操作步骤**：

1. 假设有 `Value *a = Builder.getInt32(40); Value *b = Builder.getInt32(2);`（两者都是常量）。
2. 分别用 `IRBuilder<>` 和 `IRBuilder<NoFolder>` 执行 `Builder.CreateAdd(a, b, "sum")`。

**需要观察的现象**：

- `IRBuilder<>`：`CreateAdd` 内 `FoldNoWrapBinOp` 命中（双常量），直接返回 `ConstantInt(42)`，**没有任何 `add` 指令**进入基本块；`"sum"` 这个名字也不会出现。
- `IRBuilder<NoFolder>`：`FoldNoWrapBinOp` 恒返回 `nullptr`，于是生成一条真实的 `%sum = add i32 40, 2` 并插入基本块。

**预期结果**：你得出结论——**想让构造出的 IR 里保留每一条指令（例如教学、调试、或对照优化前后形态），就改用 `IRBuilder<NoFolder>`**。本项为「修改局部参数并说明应观察什么」的源码阅读型实践，具体输出文本待本地验证（见综合实践可运行的对照版本）。

#### 4.4.5 小练习与答案

**练习 1**：用默认 `IRBuilder<>` 执行 `Builder.CreateAdd(Builder.getInt32(40), Builder.getInt32(2))`，返回值的 `isa<ConstantInt>()` 是真还是假？基本块里多了几条指令？

**参考答案**：返回值是 `ConstantInt`，`isa<ConstantInt>()` 为真；基本块里**多了 0 条指令**（`add` 被折叠掉了）。注意 `getInt32(40)` / `getInt32(2)` 本身也不产生指令——它们直接返回已存在的 `ConstantInt` 单例（见 u3-l3）。

**练习 2**：为什么写 pass 改写 IR 时，有人偏好 `IRBuilder<NoFolder>`？

**参考答案**：在改写阶段，你通常已经清楚自己想要生成什么指令；默认折叠可能把你想保留的中间指令消掉，或改变指令的形态让后续匹配失效。`NoFolder` 保证「写什么得什么」，便于精确控制。此外，构造期的最小折叠交给后续 `instcombine` 等 pass 统一做更稳妥。

---

## 5. 综合实践

把本讲四个最小模块串起来：用 `IRBuilder` 从零构造一个 `i32 @main()` 函数，里面「分配一个 i32 变量 → 存入常量 42 → 再加载出来 → 返回」，并用 `verifyFunction` 校验通过、用 `Module::print` 打印 `.ll` 文本。

下面是示例代码（**示例代码**，非项目原有文件，需要链接已构建好的 LLVM 库才能运行）：

```cpp
// build_ir.cpp —— 用 IRBuilder 构造并打印一段 IR
#include "llvm/IR/IRBuilder.h"
#include "llvm/IR/LLVMContext.h"
#include "llvm/IR/Module.h"
#include "llvm/IR/Verifier.h"
#include "llvm/Support/raw_ostream.h"

using namespace llvm;

int main() {
  LLVMContext Ctx;
  Module M("demo", Ctx);

  // 函数类型：i32 ()
  FunctionType *FT = FunctionType::get(Type::getInt32Ty(Ctx), /*isVarArg*/false);
  Function *F = Function::Create(FT, Function::ExternalLinkage, "main", M);

  // 入口基本块
  BasicBlock *BB = BasicBlock::Create(Ctx, "entry", F);

  // 把插入点设到 entry 末尾（构造时即指定 BasicBlock*）
  IRBuilder<> Builder(BB);

  // 1) 分配一个 i32 变量（栈上）
  AllocaInst *Slot = Builder.CreateAlloca(Type::getInt32Ty(Ctx), nullptr, "slot");

  // 2) 存入常量 42（getInt32 不产生指令，直接返回 ConstantInt）
  Builder.CreateStore(Builder.getInt32(42), Slot);

  // 3) 再加载出来
  Value *V = Builder.CreateLoad(Type::getInt32Ty(Ctx), Slot, "v");

  // 4) 返回它
  Builder.CreateRet(V);

  // 校验：返回 true 表示函数非法
  if (verifyFunction(*F)) {
    errs() << "校验失败：生成的 IR 不合法\n";
    return 1;
  }

  // 打印模块（.ll 文本）
  M.print(outs(), nullptr);
  return 0;
}
```

**操作步骤**：

1. 准备一个已构建并安装好 LLVM 的环境（参见 u1-l3，用 `cmake -B build -DLLVM_ENABLE_PROJECTS=... -DCMAKE_INSTALL_PREFIX=...` 构建）。
2. 用如下 `CMakeLists.txt` 编译上述程序（示例代码）：

   ```cmake
   cmake_minimum_required(VERSION 3.20)
   project(build_ir)
   find_package(LLVM REQUIRED CONFIG)
   add_definitions(${LLVM_DEFINITIONS})
   include_directories(${LLVM_INCLUDE_DIRS})
   add_executable(build_ir build_ir.cpp)
   target_link_libraries(build_ir PRIVATE LLVMCore LLVMSupport)
   # 若报「符号未定义」，按提示追加 LLVMAnalysis 等 LLVM 组件库
   ```
3. 运行 `./build_ir`。

**需要观察的现象与预期结果**：

- 程序应打印出类似下面的 IR（指针类型在不透明指针下显示为 `ptr`）：

  ```llvm
  ; ModuleID = 'demo'
  source_filename = "demo"

  define i32 @main() {
  entry:
    %slot = alloca i32
    store i32 42, ptr %slot
    %v = load i32, ptr %slot
    ret i32 %v
  }
  ```

- **关键观察 1（插入点）**：四条指令按 `alloca → store → load → ret` 的顺序出现在 `entry` 块里，正是因为构造 builder 时把插入点设成了 `BB` 末尾，每次 `CreateXxx` 都追加到末尾。
- **关键观察 2（常量不进块）**：`42` 没有产生单独的指令——`getInt32(42)` 直接返回 `ConstantInt`；`store` 的右值就是它。而 `store`/`load` 经过内存，不会被折叠掉。
- **关键观察 3（对照折叠）**：如果把第 2、3 步换成 `Value *V = Builder.CreateAdd(Builder.getInt32(40), Builder.getInt32(2), "sum");`，默认 builder 下你**看不到** `%sum = add ...`，取而代之的是 `ret i32 42`（被折叠）；此时若改用 `IRBuilder<NoFolder> Builder(BB);`，就能看到 `%sum = add i32 40, 2` 被保留下来。
- `verifyFunction` 返回 0（合法），程序退出码为 0。

> 若尚未构建 LLVM，无法实际运行，可只做源码阅读：把上面 `main` 的逻辑与 [Kaleidoscope Ch3 的 FunctionAST::codegen](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/examples/Kaleidoscope/Chapter3/toy.cpp#L505-L513)（同样以 `CreateRet` + `verifyFunction` 收尾）对照，确认套路一致。运行结果待本地验证。

## 6. 本讲小结

- `IRBuilder` 把「构造指令」与「插入基本块」合并为一致的 `CreateXxx` 接口，核心设计是「`IRBuilderBase` + Folder + Inserter」三件套，Folder 与 Inserter 通过模板参数替换（默认 `ConstantFolder` + `IRBuilderDefaultInserter`）。
- **插入点**由「`BasicBlock*` + 块内迭代器」共同决定；`SetInsertPoint` 定位、`saveIP`/`restoreIP` 或 RAII 的 `InsertPointGuard` 临时切换并恢复。未设置插入点时生成的指令会悬空。
- `CreateXxx` 覆盖运算、内存（`alloca`/`load`/`store`）、控制流（`ret`/`br`）、类型转换、比较、`GEP`、`select`/`call`/`phi` 等；命名一致的「传操作数、收 `Value*`」模式。整数运算可带 `nuw`/`nsw` 溢出标志。
- **常量折叠**遵循「先折叠、后插入」：操作数全为常量时，Folder 返回一个 `Constant`，`CreateXxx` 直接返回它而不生成指令；`Insert(Constant*)` 这个空操作重载是「常量不进基本块」的安全网。
- 三种 Folder 策略：`ConstantFolder`（默认，最小折叠）、`TargetFolder`（结合数据布局）、`NoFolder`（永不折叠，便于学习/调试/精确控制）。`IRBuilder<NoFolder>` 可强制保留每条指令。
- 构造出的 IR 用 `verifyFunction` 校验合法性，用 `Module::print` 输出 `.ll` 文本——这与 Kaleidoscope 教程的收尾方式完全一致。

## 7. 下一步学习建议

- **进入 Pass 世界（u4）**：本讲你已学会「用代码造 IR」。下一单元 u4-l1 讲新 Pass 管理器（New PM）与 `PassBuilder`，u4-l4 讲如何编写你自己的 pass——在那里你会用 `IRBuilder` 在 pass 内部**改写**已有 IR（典型场景：遍历指令、用 `IRBuilder` 替换某条指令、处理 `PreservedAnalyses`）。
- **阅读真实 codegen**：浏览 [llvm/lib/Transforms/InstCombine/InstructionCombining.cpp](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/Transforms/InstCombine/InstructionCombining.cpp)，看一个成熟 pass 如何在指令点用 `IRBuilder` 重写表达式。
- **Clang CodeGen（u5-l5）**：Clang 把 AST 翻译成 IR 时大量使用 `IRBuilder`，学完 u5-l5 你会把「前端如何用 builder」和「builder 内部如何工作」两头接上。
- **深入 def-use**：构造指令时它会被自动登记到操作数的 use-list（u3-l2）；试着在综合实践后用 `V->users()` 遍历 `Slot` 的使用者，巩固 SSA def-use 链的直觉。
