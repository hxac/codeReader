# 用 IRBuilder 构建 IR

## 1. 本讲目标

本讲教你如何用 `IRBuilder`「安全且便捷」地在 C++ 里生产 LLVM IR。

学完后你应该能够：

- 说清楚 `IRBuilder` 解决了手工构造 IR 时的哪两个痛点（**插入点**与**常量折叠**）。
- 用 `SetInsertPoint` / `saveIP` / `restoreIP` / `InsertPointGuard` 控制指令落到哪个基本块的哪个位置。
- 用 `CreateAdd` / `CreateSub` / `CreateICmp` / `CreateCondBr` / `CreateRet` / `CreateCall` / `CreatePHI` 等方法构造算术、控制流与调用指令。
- 看懂 `examples/Fibonacci` 示例的 IR 构造思路，并理解「为什么它没有用 IRBuilder」。
- 独立用 `IRBuilder` 构造一个计算 \( n! \) 的函数，并用 `Verifier` 校验生成的 IR。

## 2. 前置知识

本讲承接 **u2-l1（IR 层次结构）** 和 **u2-l2（类型系统与 Value）**，并升级 **u1-l4（ModuleMaker）** 里的手工构造方式。请先回忆三件事：

1. **四层归属树**：`Module → Function → BasicBlock → Instruction`，子节点必须被「挂」到父节点上才算合法（u2-l1）。
2. **类型先行**：造指令前要先有 `Type`，常量用 `ConstantInt::get(Type, value)` 这种静态工厂获取（u2-l2）。
3. **创建与插入是两步**：ModuleMaker 里 `BinaryOperator::Create(...)` 只是造出一条「游离」指令，还必须 `Instruction::insertInto(...)` 才真正挂进基本块（u1-l4）。

`IRBuilder` 的全部价值就浓缩在第三点：**它把「创建 + 插入」合并成一个原子操作，并顺手做常量折叠**。本讲就是围绕这两件事展开。

> 一个关键术语：**插入点（Insertion Point）** = 「下一条新指令应该插在哪里」，由「某个基本块 + 块内一个迭代器位置」共同确定。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [include/llvm/IR/IRBuilder.h](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/IRBuilder.h) | `IRBuilder` 的核心：插入点管理 + 一整套 `Create*` 工厂方法，全是内联模板/方法。 |
| [include/llvm/IR/ConstantFolder.h](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/ConstantFolder.h) | 默认的「折叠器」：操作数都是常量时直接算出结果常量，不产生指令。 |
| [include/llvm/IR/NoFolder.h](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/NoFolder.h) | 关闭折叠的折叠器，便于学习者观察「未被折叠」的原始指令。 |
| [include/llvm/IR/IRBuilderFolder.h](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/IRBuilderFolder.h) | 折叠器的抽象基类（`FoldBinOp` / `FoldCmp` 等纯虚接口）。 |
| [lib/IR/IRBuilder.cpp](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/lib/IR/IRBuilder.cpp) | `IRBuilder` 中较复杂、不适合内联的方法的实现（如 `CreateGlobalString`、各种 intrinsic 包装）。 |
| [examples/Fibonacci/fibonacci.cpp](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/examples/Fibonacci/fibonacci.cpp) | 用**手工**方式构造递归 `fib` 函数并交给 JIT 执行的最小示例（本讲的对比对象）。 |
| [include/llvm/IR/Verifier.h](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/Verifier.h) | `verifyModule` / `verifyFunction`：校验生成的 IR 是否合法（SSA、类型、终结指令等）。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **4.1 IRBuilder 的两大职责：插入点与折叠** —— 讲清 IRBuilder 到底替你做了什么。
2. **4.2 构造算术与控制流指令** —— 把 `Create*` 方法按用途分类讲透。
3. **4.3 Fibonacci 示例解析：手工构造 vs IRBuilder** —— 用真实示例对比两种写法。

---

### 4.1 IRBuilder 的两大职责：插入点与折叠

#### 4.1.1 概念说明

回顾 ModuleMaker 的痛点：每造一条指令都要自己操心「插到哪个块」「在块里的什么位置」，而且很容易写出 `add i32 1, 2` 这种本可在编译期算成 `3` 的冗余指令。`IRBuilder` 用两个机制把这两件事自动化：

- **插入点（Insertion Point）**：Builder 内部记住「当前写到哪个基本块、写到块内的第几个位置」。每次你调一个 `Create*` 方法，它就把新指令自动插到这个位置，并把插入点向后移一位。等价于「模块里有一支自动前进的笔」。
- **常量折叠（Constant Folding）**：每次构造运算前，Builder 先问折叠器「这两个操作数是不是都是常量？能不能在编译期算出来？」如果能，就直接返回一个 `Constant`，连指令都不创建；不能才真正 `new` 出一条指令。

所以一句话总结：

> **IRBuilder = 自动插入 + 自动折叠**。它不是新指令类型，而是一层「会记账、会化简」的便捷外壳。

这两件事之所以能被「策略化」，是因为 `IRBuilder` 是一个带两个模板参数的模板类：

```cpp
template <typename FolderTy = ConstantFolder,
          typename InserterTy = IRBuilderDefaultInserter>
class IRBuilder : public IRBuilderBase { ... };
```

- `FolderTy` 决定**怎么折叠**：默认 `ConstantFolder`（折叠），可换成 `NoFolder`（不折叠）。
- `InserterTy` 决定**怎么插入**：默认 `IRBuilderDefaultInserter`（插到当前插入点），可换成带回调的 `IRBuilderCallbackInserter`（插完后额外做点事，比如记录每条指令）。

平时写 `IRBuilder<> B(...)`，两个尖括号留空，用的就是上面这两个默认值。

#### 4.1.2 核心流程

一条 `Create*` 调用的内部走向可以用下面这串伪代码概括：

```
CreateAdd(L, R, Name="x"):
    V = Folder.FoldNoWrapBinOp(Add, L, R, ...)   # 第 1 步：先尝试折叠
    if V != null: return V                        #    折叠成功 → 直接返回常量，结束
    BO = BinaryOperator::Create(Add, L, R)        # 第 2 步：造一条游离指令
    Insert(BO, Name)                              # 第 3 步：插入到插入点 + 命名
    return BO
```

其中 `Insert` 做的事是：

```
Insert(I, Name):
    Inserter.InsertHelper(I, Name, InsertPt)      # 把 I 插到「当前基本块/当前迭代器」处
    SetInstDebugLocation(I)                       # 顺带补上调试位置
    return I
```

注意一个精妙的细节：`Insert` 对常量是**空操作**。折叠成功时返回的是 `Constant`，根本不会走到 `InsertHelper`，所以「折叠」与「插入」在代码层面自然合流——这正是折叠能省掉指令的根本原因。

#### 4.1.3 源码精读

**插入器与 `InsertHelper`**：默认插入器把指令插到插入点，并给指令起名（`setName`）：

[include/llvm/IR/IRBuilder.h:L61-L70](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/IRBuilder.h#L61-L70) —— 默认插入器：若插入点有效，就 `I->insertInto(块, 位置)`，再 `setName`。这正是 ModuleMaker 里手写的 `insertInto` 的自动化版本。

如果你想在每条指令被插进去之后做点额外处理（例如收集日志），换用回调插入器即可：

[include/llvm/IR/IRBuilder.h:L75-L89](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/IRBuilder.h#L75-L89) —— `IRBuilderCallbackInserter`：先执行默认插入，再调用你传入的回调，回调拿到刚插入的 `Instruction *`。

**Builder 的核心成员与 `Insert` 模板**：

[include/llvm/IR/IRBuilder.h:L114-L124](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/IRBuilder.h#L114-L124) —— `IRBuilderBase` 持有 `BB`（当前基本块）、`InsertPt`（块内迭代器）、`Context`、`Folder`、`Inserter`。插入点的全部状态就是 `BB + InsertPt` 这一对。

[include/llvm/IR/IRBuilder.h:L144-L162](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/IRBuilder.h#L144-L162) —— 三个 `Insert` 重载：(a) 对 `Instruction` 调 `InsertHelper` 真正插入；(b) 对 `Constant` 直接原样返回（折叠的关键，空操作）；(c) 对通用 `Value *` 用 `dyn_cast<Instruction>` 区分二者。**记住这个 `Constant *` 的空操作重载——它是「折叠成功就不插指令」的落地点。**

**插入点管理 `SetInsertPoint`**：四个常用重载，决定「笔尖」落在何处：

[include/llvm/IR/IRBuilder.h:L179-L210](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/IRBuilder.h#L179-L210) —— 分别支持：追加到块尾（`SetInsertPoint(BasicBlock*)`）、插到某条指令之前（`SetInsertPoint(Instruction*)`）、指定块与迭代器、或仅指定一个可解引用的迭代器。注意 `ClearInsertionPoint()` 会把插入点清空，此时新建的指令**不会被插入**（`InsertHelper` 里 `InsertPt.isValid()` 为假）。

**保存/恢复插入点 `saveIP` / `restoreIP` 与 RAII 守卫 `InsertPointGuard`**：当你需要在「填一半 A 块」时临时跳到 B 块写两条指令、再回到 A 块继续，就用这套机制：

[include/llvm/IR/IRBuilder.h:L245-L283](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/IRBuilder.h#L245-L283) —— `InsertPoint` 是一个「块 + 迭代器」的快照值类型；`saveIP()` 拍快照、`restoreIP()` 回放。

[include/llvm/IR/IRBuilder.h:L364-L380](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/IRBuilder.h#L364-L380) —— `InsertPointGuard` 在构造时存快照、析构时自动 `restoreIP`，是典型的 RAII「作用域内临时改插入点，出了作用域自动还原」。

**折叠器 `ConstantFolder` 与 `NoFolder`**：

[include/llvm/IR/ConstantFolder.h:L44-L54](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/ConstantFolder.h#L44-L54) —— `FoldBinOp`：只有当 `LHS`、`RHS` **都是** `Constant` 时才折叠（`ConstantFoldBinaryInstruction`），否则返回 `nullptr` 表示「折不动」。

[include/llvm/IR/ConstantFolder.h:L99-L105](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/ConstantFolder.h#L99-L105) —— `FoldCmp`：比较运算同理，两边都是常量就提前算出 `i1 true/false`。

[include/llvm/IR/NoFolder.h:L35-L52](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/NoFolder.h#L35-L52) —— `NoFolder` 永远返回 `nullptr`，即「永不折叠」。源码注释直言它「对想搞懂 IR 工作原理、不想被折叠隐藏细节的学习者很有用」——这正是本讲拿它做对比实验的依据。

折叠接口本身定义在抽象基类 `IRBuilderFolder` 里，`ConstantFolder` 与 `NoFolder` 是它的两个具体实现：

[include/llvm/IR/IRBuilderFolder.h:L28-L79](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/IRBuilderFolder.h#L28-L79) —— `FoldBinOp` / `FoldExactBinOp` / `FoldNoWrapBinOp` / `FoldCmp` / `FoldGEP` 等纯虚方法，构成「折叠策略」的统一接口。

#### 4.1.4 代码实践：直观看见「折叠」

**实践目标**：用同一段构造代码，分别搭配默认 `ConstantFolder` 和 `NoFolder`，观察折叠是否真的省掉了指令。

**操作步骤**：

1. 阅读下面的示例代码（不是项目原有代码，标注为「示例代码」），预测两种 Builder 各自会产出什么 IR。

```cpp
// 示例代码（非项目原有）：演示常量折叠
#include "llvm/IR/IRBuilder.h"
#include "llvm/IR/LLVMContext.h"
#include "llvm/IR/Module.h"
#include "llvm/IR/Verifier.h"
#include "llvm/Support/raw_ostream.h"
using namespace llvm;

template <typename FolderTy>
static void build(const char *Tag) {
  LLVMContext C;
  Module M("demo", C);
  FunctionType *FTy = FunctionType::get(Type::getInt32Ty(C), {}, false);
  Function *F = Function::Create(FTy, Function::ExternalLinkage, "f", M);
  BasicBlock *BB = BasicBlock::Create(C, "entry", F);

  IRBuilder<FolderTy> B(BB);                       // ① 在 entry 末尾写
  Value *One = ConstantInt::get(Type::getInt32Ty(C), 1);
  Value *Two = ConstantInt::get(Type::getInt32Ty(C), 2);
  Value *X = B.CreateAdd(One, Two, "x");           // ② 1 + 2
  B.CreateRet(X);                                  // ③ return x

  errs() << "=== " << Tag << " ===\n" << M << "\n";
}

int main() {
  build<ConstantFolder>("ConstantFolder(默认)");    // 折叠
  build<NoFolder>("NoFolder");                      // 不折叠
  return 0;
}
```

2. 若想真正运行：可仿照 `examples/Fibonacci/CMakeLists.txt` 把它注册成一个 `add_llvm_example`（链接 `Core`、`Support` 组件），或放入一个 out-of-tree 工程，配置 CMake 后编译运行。**编译运行的精确输出「待本地验证」**。

**需要观察的现象 / 预期结果**：

- `ConstantFolder` 版：`1 + 2` 在编译期被算成 `3`，函数体里**没有 `add` 指令**，直接是：

  ```llvm
  define i32 @f() {
  entry:
    ret i32 3
  }
  ```

- `NoFolder` 版：折叠被关闭，会真的多出一条 `add`：

  ```llvm
  define i32 @f() {
  entry:
    %x = add i32 1, 2
    ret i32 %x
  }
  ```

  这恰好印证 `NoFolder` 源码注释——它「为学习者保留未被折叠的原始指令」。

> 这个对比也解释了为什么很多 pass 内部用 `IRBuilder<NoFolder>`：它们要**精确控制**生成的每一条指令，不希望 Builder 自作主张地化简。

#### 4.1.5 小练习与答案

**练习 1**：`IRBuilder` 的「插入点」由哪两个字段共同决定？清空插入点后调用 `CreateAdd` 会发生什么？

**参考答案**：由 `IRBuilderBase` 的 `BB`（当前基本块）和 `InsertPt`（块内 `BasicBlock::iterator`）共同决定。清空后 `InsertPt` 无效，`InsertHelper` 里的 `if (InsertPt.isValid())` 为假，新指令**不会被插入**到任何块（但仍被 `new` 出来，造成游离对象，通常是你不想要的）。

**练习 2**：为什么 `Insert` 对 `Constant *` 是空操作？它和「折叠」有什么关系？

**参考答案**：因为折叠成功时 `Create*` 直接返回一个 `Constant`，而 `Constant`（如 `ConstantInt 3`）不是 `Instruction`、不需要插进任何基本块；`Insert(Constant*)` 重载原样返回它即可。正是因为这个空操作重载，「折叠成功就不产生指令」这件事在类型层面被自然表达出来了。

**练习 3**：说出两种「临时切换插入点、用完自动还原」的写法。

**参考答案**：(a) `auto IP = B.saveIP(); ... B.SetInsertPoint(别处); ... B.restoreIP(IP);`；(b) 用 RAII 守卫 `{ InsertPointGuard IPG(B); B.SetInsertPoint(别处); ... }`，出作用域自动还原。

---

### 4.2 构造算术与控制流指令

#### 4.2.1 概念说明

`IRBuilder` 提供的 `Create*` 方法可以按用途分成几族：

| 族 | 代表方法 | 对应 IR |
| --- | --- | --- |
| 整数算术 | `CreateAdd` / `CreateSub` / `CreateMul` / `CreateUDiv` / `CreateSDiv` | `add` / `sub` / `mul` / `udiv` / `sdiv` |
| 按位/移位 | `CreateAnd` / `CreateOr` / `CreateXor` / `CreateShl` | `and` / `or` / `xor` / `shl` |
| 比较 | `CreateICmpEQ` / `CreateICmpSLT` / `CreateICmp(P,...)` / `CreateFCmp*` | `icmp eq` / `icmp slt` 等 |
| 内存 | `CreateAlloca` / `CreateLoad` / `CreateStore` | `alloca` / `load` / `store` |
| 控制流 | `CreateBr` / `CreateCondBr` / `CreateSwitch` / `CreateRet` / `CreateRetVoid` | `br` / `br cond` / `switch` / `ret` |
| 调用 | `CreateCall(FTy, Callee, Args)` | `call` |
| 聚合 | `CreatePHI` / `CreateSelect` / `CreateExtractValue` | `phi` / `select` 等 |

两个共同特点：

1. **返回类型是 `Value *`**（少数是具体类型如 `ReturnInst *`）。因为折叠后可能返回常量，所以统一返回基类指针最自然。
2. **签名里几乎都带一个 `const Twine &Name = ""`**：给新指令/结果起名，便于在 `.ll` 里辨认。

#### 4.2.2 核心流程

以整数加法为例，所有「带 NoWrap 标志」的二元运算走同一条路径（折叠 → 造指令 → 插入 → 设标志）：

```
CreateAdd(L, R, Name, HasNUW=false, HasNSW=false):
    V = Folder.FoldNoWrapBinOp(Add, L, R, HasNUW, HasNSW)   # 折叠
    if V: return V
    BO = Insert(BinaryOperator::Create(Add, L, R), Name)    # 造+插
    if HasNUW: BO->setHasNoUnsignedWrap()                   # 设溢出标志
    if HasNSW: BO->setHasNoSignedWrap()
    return BO
```

控制流则更直白：比较→条件分支→各分支返回值用 `phi` 汇聚。注意 IRBuilder **不会**替你检查「每个基本块是否以终结指令收尾」「phi 是否放在块首」等结构性约束——那是 `Verifier`（4.3 与综合实践会用）的职责。

#### 4.2.3 源码精读

**算术运算的统一模式**：`CreateAdd` / `CreateSub` / `CreateMul` 三者结构完全一致，都是「先 `FoldNoWrapBinOp`，折不动就走 `CreateInsertNUWNSWBinOp`」：

[include/llvm/IR/IRBuilder.h:L1422-L1429](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/IRBuilder.h#L1422-L1429) —— `CreateAdd`：先折叠，再 `CreateInsertNUWNSWBinOp`。`CreateNSWAdd` / `CreateNUWAdd` 只是把 `HasNSW` / `HasNUW` 置 `true` 的便捷封装。

[include/llvm/IR/IRBuilder.h:L1366-L1374](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/IRBuilder.h#L1366-L1374) —— `CreateInsertNUWNSWBinOp`：`Insert(BinaryOperator::Create(Opc, LHS, RHS))` 把「造 + 插」合并，再按需设 `NoUnsignedWrap` / `NoSignedWrap` 标志（即 IR 里的 `nuw` / `nsw`）。

[include/llvm/IR/IRBuilder.h:L1439-L1463](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/IRBuilder.h#L1439-L1463) —— `CreateSub` 与 `CreateMul`：与 `CreateAdd` 完全同构，可对照阅读。

**比较 `CreateICmp`**：所有 `CreateICmpEQ`/`SLT`/... 都委托给一个通用的 `CreateICmp(P, L, R)`：

[include/llvm/IR/IRBuilder.h:L2485-L2490](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/IRBuilder.h#L2485-L2490) —— `CreateICmp`：先 `Folder.FoldCmp`（两边常量就提前出 `i1`），否则 `Insert(new ICmpInst(P, L, R))`。

[include/llvm/IR/IRBuilder.h:L2375-L2412](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/IRBuilder.h#L2375-L2412) —— `CreateICmpEQ`…`CreateICmpSLE`：九个谓词的薄封装，每个一行转调 `CreateICmp(对应谓词, L, R)`。

**控制流 `CreateBr` / `CreateCondBr` / `CreateRet`**：

[include/llvm/IR/IRBuilder.h:L1186-L1194](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/IRBuilder.h#L1186-L1194) —— `CreateRetVoid` / `CreateRet(V)`：`Insert(ReturnInst::Create(...))`，每个基本块必须以这类终结指令收尾（u2-l1）。

[include/llvm/IR/IRBuilder.h:L1209-L1221](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/IRBuilder.h#L1209-L1221) —— `CreateBr(Dest)`（无条件）与 `CreateCondBr(Cond, True, False)`（条件）：后者还能挂分支权重等元数据。

**调用 `CreateCall`**：

[include/llvm/IR/IRBuilder.h:L2554-L2563](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/IRBuilder.h#L2554-L2563) —— `CreateCall(FTy, Callee, Args, Name)`：`CallInst::Create(FTy, Callee, Args, DefaultOperandBundles)` 后 `Insert`。若涉及浮点还会补 fast-math 属性。注意第一个参数 `FTy` 是函数类型（u2-l2 的 `FunctionType`），用于在不透明指针时代精确描述被调者签名。

**`phi` 节点 `CreatePHI`**：

[include/llvm/IR/IRBuilder.h:L2540-L2546](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/IRBuilder.h#L2540-L2546) —— `CreatePHI(Ty, NumReservedValues)`：只创建 phi 节点骨架，`NumReservedValues` 是为 incoming 边预分配的容量（性能提示）；真正的「每条前驱块对应一个值」要随后用 `PHINode::addIncoming(Value*, BasicBlock*)` 补上。

#### 4.2.4 代码实践：用 IRBuilder 造一个 `max(a, b)`

**实践目标**：用比较 + 条件分支 + `phi` 三件套构造 `i32 max(i32 a, i32 b)`，体会「控制流写起来比 ModuleMaker 简洁多少」。

**操作步骤**：阅读下方示例代码（非项目原有），预测其 IR；若本地有构建环境，编译运行并打印模块验证。

```cpp
// 示例代码（非项目原有）：i32 max(i32 %a, i32 %b)
static Function *CreateMaxFn(Module *M, LLVMContext &C) {
  FunctionType *FTy = FunctionType::get(
      Type::getInt32Ty(C), {Type::getInt32Ty(C), Type::getInt32Ty(C)}, false);
  Function *F = Function::Create(FTy, Function::ExternalLinkage, "max", M);
  auto Args = F->args().begin();
  Argument *A = &Args[0]; A->setName("a");
  Argument *B = &Args[1]; B->setName("b");

  BasicBlock *Entry = BasicBlock::Create(C, "entry", F);
  BasicBlock *RetA   = BasicBlock::Create(C, "ret_a", F);
  BasicBlock *RetB   = BasicBlock::Create(C, "ret_b", F);

  IRBuilder<> B(Entry);
  Value *Cond = B.CreateICmpSGT(A, B, "cmp");   // a > b ?
  B.CreateCondBr(Cond, RetA, RetB);             // 真→ret_a，假→ret_b

  B.SetInsertPoint(RetA);  B.CreateRet(A);      // return a
  B.SetInsertPoint(RetB);  B.CreateRet(B);      // return b
  return F;
}
```

**预期结果**（精确输出「待本地验证」）：生成的 IR 形如

```llvm
define i32 @max(i32 %a, i32 %b) {
entry:
  %cmp = icmp sgt i32 %a, %b
  br i1 %cmp, label %ret_a, label %ret_b
ret_a:
  ret i32 %a
ret_b:
  ret i32 %b
}
```

**需要观察的现象**：用 `IRBuilder` 时你**完全没写过** `Instruction::insertInto`、也没手动 `new ICmpInst(...)` 再挂块——这些全被 `CreateICmpSGT` / `CreateCondBr` / `CreateRet` 在内部消化了。对比 ModuleMaker（u1-l4）就能直观感受到便捷性的来源。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `CreateAdd` 返回 `Value *` 而不是 `BinaryOperator *`？

**参考答案**：因为折叠成功时它返回的是 `Constant`（如 `ConstantInt 3`），并不是 `BinaryOperator`。统一返回基类 `Value *` 才能同时覆盖「折成常量」和「真造了指令」两种结果。

**练习 2**：`CreateNSWAdd` 和 `CreateAdd` 有什么区别？它会影响生成的 IR 吗？

**参考答案**：`CreateNSWAdd(L,R)` 等价于 `CreateAdd(L,R, /*HasNUW*/false, /*HasNSW*/true)`，会给 `add` 加上 `nsw`（No Signed Wrap）标志，即声明「有符号运算不会溢出」。它会改变 IR（出现 `add nsw`），并把额外的语义信息暴露给后续优化。

**练习 3**：`CreatePHI` 之后还必须做什么，phi 节点才算完整？

**参考答案**：还要为每一条「流入该块的前驱边」调用 `PHINode::addIncoming(Value *V, BasicBlock *BB)`，指定「从 `BB` 过来时取值 `V`」。`CreatePHI` 只是建了空壳。

---

### 4.3 Fibonacci 示例解析：手工构造 vs IRBuilder

#### 4.3.1 概念说明

`examples/Fibonacci` 是一个「在内存里构造递归 `fib` 函数并交给 JIT 执行」的最小示例。值得特别强调的是：**它没有用 `IRBuilder`**，而是用最底层的指令构造函数（`ICmpInst`、`BinaryOperator::CreateSub`、`CallInst::Create`、`ReturnInst::Create`、`CondBrInst::Create`）手工拼装——和 ModuleMaker（u1-l4）同一套手法。

拿它做对比对象有两个好处：

1. 它的逻辑足够简单（`if x<=2 return 1; return fib(x-1)+fib(x-2)`），能清楚展示「手工写」要操心多少插入细节。
2. 它正好用到了本讲讲过的全部指令族：比较、条件分支、减法、调用、加法、返回——是 4.2 的天然综合例题。

#### 4.3.2 核心流程

示例想要构造的函数等价于：

```c
int fib(int x) {
  if (x <= 2) return 1;
  return fib(x - 1) + fib(x - 2);
}
```

构造步骤（见 `CreateFibFunction`）：

1. 建签名 `FunctionType::get(i32, {i32})` → 建 `Function` → 建入口块 `entry`、返回块 `return`、递归块 `recurse`。
2. 取常量 `One=1`、`Two=2`，取参数 `AnArg`。
3. `entry`：`AnArg <= 2 ? → return : → recurse`。
4. `return`：直接 `ret i32 1`。
5. `recurse`：算 `fib(x-1)`、`fib(x-2)`，相加后 `ret`。两个调用都设了 `tail` 标记（尾调用）。

随后 `main` 创建 `LLVMContext` + `Module`，调用 `CreateFibFunction`，再用 `verifyModule` 校验，最后用 MCJIT 执行并打印结果。

#### 4.3.3 源码精读

**目标函数的伪代码注释**：

[examples/Fibonacci/fibonacci.cpp:L9-L22](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/examples/Fibonacci/fibonacci.cpp#L9-L22) —— 注释里写明了要构造的 C 语义 `fib`，以及「构造完后用 JIT 编译并执行」的整体意图。

**手工构造控制流与算术（核心对照点）**：

[examples/Fibonacci/fibonacci.cpp:L75-L80](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/examples/Fibonacci/fibonacci.cpp#L75-L80) —— `entry`：`new ICmpInst(BB, ICMP_SLE, ArgX, Two, "cond")` 造比较指令并**显式**插入块 `BB`；`CondBrInst::Create(Cond, RetBB, RecurseBB, BB)` 造条件分支并插入。注意每个构造都把目标块作为最后参数，这就是「手工指定插入点」。

[examples/Fibonacci/fibonacci.cpp:L82-L94](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/examples/Fibonacci/fibonacci.cpp#L82-L94) —— `recurse`：`BinaryOperator::CreateSub(ArgX, One, "arg", RecurseBB)`、`CallInst::Create(FibF, Sub, "fibx1", RecurseBB)`、`setTailCall()`、`CreateAdd(...)`，每一步都把 `RecurseBB` 显式传进去当插入位置——而 `IRBuilder` 版本只需 `B.SetInsertPoint(RecurseBB)` 一次，后续全部自动落到此处。

[examples/Fibonacci/fibonacci.cpp:L96-L99](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/examples/Fibonacci/fibonacci.cpp#L96-L99) —— `recurse` 末尾 `ReturnInst::Create(Context, Sum, RecurseBB)` 收尾。

**构造完用 Verifier 校验、再用 JIT 执行**：

[examples/Fibonacci/fibonacci.cpp:L129-L137](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/examples/Fibonacci/fibonacci.cpp#L129-L137) —— `if (verifyModule(*M))` 校验：返回非零表示 IR 有错，直接报错退出；通过后打印整段模块。

[examples/Fibonacci/fibonacci.cpp:L139-L145](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/examples/Fibonacci/fibonacci.cpp#L139-L145) —— 用 `GenericValue` 装入参数 `n`，`EE->runFunction(FibF, Args)` 执行，打印 `Result`。

> **对比小结**：把 fibonacci.cpp 的 `BinaryOperator::CreateSub(ArgX, One, "arg", RecurseBB)` 翻译成 IRBuilder，就是 `B.SetInsertPoint(RecurseBB); B.CreateSub(ArgX, One, "arg");`——插入位置从「每条指令都传一次」变成「设一次、之后自动」。Fibonacci 选手工写法只是为了让示例不依赖 `IRBuilder`、更直接地展示「指令是怎么一块块拼起来的」。

#### 4.3.4 代码实践：把 `recurse` 块改写成 IRBuilder 版

**实践目标**：在不改变生成 IR 的前提下，把 `CreateFibFunction` 里 `recurse` 块（[fibonacci.cpp:L82-L97](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/examples/Fibonacci/fibonacci.cpp#L82-L97)）的手工构造替换成 `IRBuilder` 写法，验证「二者产出等价 IR」。

**操作步骤**：

1. 在文件顶部 `#include "llvm/IR/IRBuilder.h"`。
2. 在 `CreateFibFunction` 内构造 `IRBuilder<> B(Context);`，对 `entry`/`return`/`recurse` 三个块分别 `B.SetInsertPoint(...)` 后用 `CreateICmpSLE` / `CreateCondBr` / `CreateRet` / `CreateSub` / `CreateCall` / `CreateAdd` 重写。例如 `recurse`：

   ```cpp
   // 示例改写（非项目原有片段）
   B.SetInsertPoint(RecurseBB);
   Value *Sub1 = B.CreateSub(ArgX, One, "arg");
   CallInst *CallFibX1 = B.CreateCall(FibFTy, FibF, {Sub1}, "fibx1");
   CallFibX1->setTailCall();
   Value *Sub2 = B.CreateSub(ArgX, Two, "arg");
   CallInst *CallFibX2 = B.CreateCall(FibFTy, FibF, {Sub2}, "fibx2");
   CallFibX2->setTailCall();
   Value *Sum = B.CreateAdd(CallFibX1, CallFibX2, "addresult");
   B.CreateRet(Sum);
   ```

   （其中 `FibFTy` 即函数开头已建的 `FunctionType`。）
3. 按 `examples/Fibonacci/CMakeLists.txt` 的方式构建（[L1-L13](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/examples/Fibonacci/CMakeLists.txt#L1-L13)：它用 `add_llvm_example` 并链接 `Core`/`ExecutionEngine`/`MCJIT`/`Support`/`nativecodegen` 等组件），运行 `Fibonacci 10`。

**需要观察的现象 / 预期结果**：改写前后打印出的模块文本应**一致**（同样的 `icmp`、`br`、两次 `call ... fibx1/fibx2`、`addresult`、`ret`）；运行 `Fibonacci 10` 应输出 `Result: 55`（`fib(10)=55`）。具体数值「待本地验证」。

> 这是「源码改造型」实践：你不引入新指令、只换构造方式，从而把 IRBuilder 的便捷性量化成「行数更少、不用每处都传块」。

#### 4.3.5 小练习与答案

**练习 1**：fibonacci.cpp 里 `new ICmpInst(BB, ICMP_SLE, ArgX, Two, "cond")` 的最后一个块参数 `BB`，对应到 `IRBuilder` 里是哪一步？

**参考答案**：对应 `B.SetInsertPoint(BB)`。手工写法把「插入到哪个块」作为构造函数的尾参逐条传递；IRBuilder 把它抽象成 Builder 的状态，设一次即可。

**练习 2**：示例里两次 `call` 都调了 `setTailCall()`。如果用 `IRBuilder::CreateCall` 得到 `CallInst *`，还能设尾调用吗？

**参考答案**：能。`CreateCall(FTy, Callee, Args, Name)` 返回 `CallInst *`（见 4.2.3 的源码，它 `Insert` 后返回 `CallInst *`），所以可以直接 `B.CreateCall(...)->setTailCall()`。

**练习 3**：为什么 `verifyModule` 要在「交给 JIT 之前」调用？

**参考答案**：因为 JIT 会真正编译并执行 IR，若 IR 不合法（如某基本块没有终结指令、phi 放错位置、类型不匹配），轻则崩溃重则产生错误机器码。`verifyModule` 返回 `true` 表示模块已损坏，示例据此提前报错退出，避免把坏 IR 送进执行引擎。

---

## 5. 综合实践：用 IRBuilder 构造 `fact(n) = n!` 并校验

把本讲三块内容串起来：用 `IRBuilder` 构造一个递归阶乘函数，用 `Verifier` 校验合法性，再写出位码用 `llvm-dis` 复核（呼应 u1-l3 的工具链与 u1-l4 的「写出位码」）。

**实践目标**：独立完成「建类型 → 建函数与基本块 → 设插入点 → 比较与分支 → 递归调用 → 相乘返回 → 校验 → 序列化」的完整闭环。

**操作步骤**：

1. 新建一个 `.cpp`，包含下面示例代码（非项目原有）。它仿照 fibonacci.cpp 的骨架，但全程用 `IRBuilder`，并去掉了 JIT 部分，改为写位码。

   ```cpp
   // 示例代码（非项目原有）：用 IRBuilder 构造 fact(n) = n!
   #include "llvm/IR/IRBuilder.h"
   #include "llvm/IR/LLVMContext.h"
   #include "llvm/IR/Module.h"
   #include "llvm/IR/Verifier.h"
   #include "llvm/Bitcode/BitcodeWriter.h"
   #include "llvm/Support/raw_ostream.h"
   using namespace llvm;

   static Function *CreateFactFunction(Module *M, LLVMContext &C) {
     // i32 fact(i32 n)
     FunctionType *FTy = FunctionType::get(
         Type::getInt32Ty(C), {Type::getInt32Ty(C)}, false);
     Function *F = Function::Create(FTy, Function::ExternalLinkage, "fact", M);
     Argument *N = &*F->arg_begin(); N->setName("n");

     BasicBlock *Entry = BasicBlock::Create(C, "entry", F);
     BasicBlock *Base  = BasicBlock::Create(C, "base", F);
     BasicBlock *Recur = BasicBlock::Create(C, "recur", F);

     IRBuilder<> B(Entry);
     Value *One = ConstantInt::get(Type::getInt32Ty(C), 1);
     Value *Cond = B.CreateICmpSLE(N, One, "cmp");   // n <= 1 ?
     B.CreateCondBr(Cond, Base, Recur);

     B.SetInsertPoint(Base);
     B.CreateRet(One);                               // return 1

     B.SetInsertPoint(Recur);
     Value *Nm1  = B.CreateSub(N, One, "nm1");       // n - 1
     Value *Call = B.CreateCall(FTy, F, {Nm1}, "rec"); // fact(n-1)
     Value *Prod = B.CreateMul(N, Call, "prod");     // n * fact(n-1)
     B.CreateRet(Prod);
     return F;
   }

   int main() {
     LLVMContext C;
     Module M("fact-demo", C);
     Function *FactF = CreateFactFunction(&M, C);

     if (verifyModule(M, &errs())) {                 // 校验：返回 true 表示坏
       errs() << "IR 不合法！\n";
       return 1;
     }
     errs() << "verifying... OK\n" << M;             // 打印模块

     std::error_code EC;
     raw_fd_ostream Out("fact.bc", EC);              // 写出位码
     WriteBitcodeToFile(M, Out);
     return 0;
   }
   ```

2. 按 fibonacci 的 `CMakeLists.txt` 范式把它配成一个 example（链接 `Core`、`BitWriter`、`Support`），构建并运行；或放入 out-of-tree 工程用 `llvm-config` 链接 LLVM 库。
3. 运行后用 `llvm-dis fact.bc -o -` 查看文本 IR（u1-l3）。

**需要观察的现象 / 预期结果**：

- 控制台先打印 `verifying... OK`，再打印整段模块。
- IR 应包含三个基本块：`entry`（`icmp sle` + `condbr`）、`base`（`ret i32 1`）、`recur`（`sub` + `call @fact` + `mul` + `ret`）。
- `recur` 块里**没有** `add i32 ... 1` 这类被折叠掉的痕迹——因为 `N` 是参数（非常量），`CreateSub(N, One)` 折不动；这正是 4.1 讲的「只有两边都是常量才折叠」。
- 若你故意把 `B.CreateRet(...)` 漏写一个（比如删掉 `Base` 块的返回），`verifyModule` 会报「基本块未以终结指令结束」之类的错——亲手验证 Verifier 的把关作用。

> 完整构建运行的精确输出「待本地验证」。若暂无构建环境，也可作为「源码阅读型实践」：对照 4.1–4.3 的源码，逐行推断每条 `Create*` 调用会落到哪个块、会不会折叠。

## 6. 本讲小结

- **`IRBuilder` 的本质**：在手工构造之上加两层自动化——**自动插入**（记住插入点，新指令自动落位）和**自动折叠**（操作数都是常量时直接算成常量、不产生指令）。
- **插入点 = `BB + InsertPt`**：用 `SetInsertPoint` 设定，用 `saveIP`/`restoreIP` 或 RAII `InsertPointGuard` 临时切换并还原；清空插入点后新建的指令不会被插入。
- **折叠是策略化的**：模板参数 `FolderTy` 默认 `ConstantFolder`（折叠），可换 `NoFolder`（永不折叠）。`Insert(Constant*)` 的空操作重载是「折叠成功就不插指令」的落点。
- **`Create*` 方法分族**：算术（`CreateAdd/Sub/Mul`，带 `nuw`/`nsw`）、比较（`CreateICmp*`）、控制流（`CreateBr/CreateCondBr/CreateRet`）、调用（`CreateCall`）、聚合（`CreatePHI`）；大多返回 `Value *` 并接受 `Name`。
- **Fibonacci 示例用的是手工构造**（`BinaryOperator::CreateSub` / `CallInst::Create` 等，逐条显式传块），正好用来对照「IRBuilder 把插入点抽象成状态、写起来更短」。
- **`verifyModule` 在交付前把关**：检查 SSA、终结指令、类型等结构性约束，避免把坏 IR 送给优化器或执行引擎。

## 7. 下一步学习建议

- **本讲之后**，IR 的「生产」侧已基本掌握。建议进入 **u2-l4（IR 的文本与位码格式）**，理解 `llvm-as`/`llvm-dis`/`IRReader` 背后的解析与序列化链路，把「写出的 `.bc`」和「读回的 `Module`」打通。
- **想深入构造细节**：阅读 [include/llvm/IR/IRBuilder.h](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/IRBuilder.h) 中尚未展开的 `CreateGEP` / `CreateCast` / `CreateMemSet` 等方法，它们在后端与 pass 里极常用。
- **为学 Pass 铺路**：进入 u3（Pass 与优化流水线）后你会发现，绝大多数**变换类 pass** 内部都用 `IRBuilder` 来重写 IR——本讲是那一系列讲义的直接前置。
- **延伸阅读**：[docs/tutorial/MyFirstLanguageFrontend](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/docs/tutorial/)（Kaleidoscope 教程）几乎全程用 `IRBuilder` 构造一个完整语言的 IR，是本讲最好的大型综合练习。
