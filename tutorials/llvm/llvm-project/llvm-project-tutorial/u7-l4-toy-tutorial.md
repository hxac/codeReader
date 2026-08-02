# Toy 教程：从语言到 MLIR 到 LLVM IR

## 1. 本讲目标

本讲是 MLIR 单元的「合龙篇」。前面三讲（u7-l1 核心抽象、u7-l2 方言机制、u7-l3 Pass/Pattern/Conversion）已经把 MLIR 的零件逐一拆解过：Operation、Region/Block、Dialect、PassManager、RewritePattern、ConversionTarget。本讲把这些零件装回一台**完整的编译器**里。

学完本讲你应该能够：

- 说出 MLIR 官方 Toy 教程的七章结构，以及每一章在「源码 → MLIR → LLVM IR」主线中负责哪一段。
- 理解 `MLIRGen` 如何遍历一棵自定义语言（Toy）的 AST，用 `OpBuilder::create` 把每个 AST 节点翻译成一个 MLIR Operation。
- 读懂 `toyc.cpp` 里用 `PassManager` 编排的整条流水线：内联 → 形状推断 → 规范化 → 降到 affine → 降到 LLVM 方言 → 翻译成 LLVM IR / JIT 执行。
- 解释 MLIR 「渐进式下降（Progressive Lowering）」的核心理念——为什么要把高层方言一档一档地降到低层方言，而不是一步到位。

## 2. 前置知识

本讲假设你已经读过 u7-l1、u7-l2、u7-l3，熟悉以下概念。这里只做最简回顾：

- **Operation（操作）**：MLIR 的唯一基本单元，「一切皆 Operation」。一个函数是一个 Operation，一条加法也是一个 Operation。详见 u7-l1。
- **Dialect（方言）**：把一组 Operation/Type/Attribute 封装在一个 namespace 下。`toy.add` 表示 `add` 属于 `toy` 方言。详见 u7-l2。
- **Pass / RewritePattern / Conversion**：Pass 是调度外壳；RewritePattern 是「匹配 + 改写」的局部规则；Conversion 在其上加了「合法性目标」，用来做跨方言的成批改写。详见 u7-l3。
- **渐进式下降**：MLIR 不要求一种方言直接降到 LLVM。你可以 `toy → affine → llvm`，每一步只降一小段、保留语义、便于在合适的抽象层级做优化。这是 MLIR 相对单层 IR 的根本差异（对照 u3 的 LLVM IR 只有固定一层）。

此外，由于 Toy 教程的最终产物是 LLVM IR，了解 u3-l1（Module/Function/BasicBlock 层次）与 u2-l2（.ll 文本格式）有助于你看懂最后一节的输出。

> 关键术语速查：AST（抽象语法树）、OpBuilder（构造 Operation 的助手）、ConversionTarget（下降的「终点」目标）、TypeConverter（类型如何随之改写）、translateModuleToLLVMIR（把 LLVM 方言翻译成真正的 LLVM IR 模块）。

## 3. 本讲源码地图

本讲围绕 MLIR 自带的 Toy 示例，代码全部在 `mlir/examples/toy/` 下，按章节（Ch1–Ch7）渐进式构建。我们重点读这些文件：

| 文件 | 作用 |
| --- | --- |
| [mlir/docs/Tutorials/Toy/_index.md](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/mlir/docs/Tutorials/Toy/_index.md) | 官方教程目录，说明七章各自的主题。 |
| [mlir/examples/toy/Ch1/toyc.cpp](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/mlir/examples/toy/Ch1/toyc.cpp) | 第 1 章驱动：只做「解析源码 → dump AST」，是最简骨架。 |
| [mlir/examples/toy/Ch6/include/toy/AST.h](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/mlir/examples/toy/Ch6/include/toy/AST.h) | Toy 语言的 AST 节点定义（表达式、函数、模块）。 |
| [mlir/examples/toy/Ch2/mlir/MLIRGen.cpp](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/mlir/examples/toy/Ch2/mlir/MLIRGen.cpp) | 第 2 章核心：遍历 AST，发射 `toy` 方言的 MLIR。 |
| [mlir/examples/toy/Ch6/toyc.cpp](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/mlir/examples/toy/Ch6/toyc.cpp) | 第 6 章驱动：装配完整的优化 + 下降流水线，并能输出 LLVM IR / JIT。 |
| [mlir/examples/toy/Ch6/mlir/LowerToAffineLoops.cpp](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/mlir/examples/toy/Ch6/mlir/LowerToAffineLoops.cpp) | `toy → affine + arith + func + memref` 的部分下降（第 5 章）。 |
| [mlir/examples/toy/Ch6/mlir/LowerToLLVM.cpp](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/mlir/examples/toy/Ch6/mlir/LowerToLLVM.cpp) | 收尾下降：把 affine/arith/func/scf/memref 一并降到 LLVM 方言（第 6 章）。 |
| [mlir/examples/toy/Ch6/CMakeLists.txt](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/mlir/examples/toy/Ch6/CMakeLists.txt) | 第 6 章的构建脚本，列出所有参与编译的源文件。 |

阅读建议：先看 `_index.md` 建立七章的全局印象；然后从 Ch1 的 `toyc.cpp`（最简）到 Ch6 的 `toyc.cpp`（最全）对照，体会「同一份驱动如何随章节演进」。

## 4. 核心概念与源码讲解

### 4.1 Toy 语言与章节结构

#### 4.1.1 概念说明

Toy 是 MLIR 官方为教学设计的一门极简语言，刻意做成「小而全」：

- 唯一的数据类型是「张量」，元素类型为 `f64`（双精度浮点）。
- 语法上只有 `def`（定义函数）、`extern`（声明外部函数）等少数关键字。
- 支持多维数组字面量、变量声明、二元运算（`+` `*`）、函数调用、内置 `transpose` / `print`。

它模仿 LLVM 的 Kaleidoscope 教程（见 u2-l3），但目标不是生成 LLVM IR，而是**演示如何用 MLIR 的方言机制构建一门语言的前端**。Toy 想说明的核心命题是：你可以先定义一套贴近语言语义的高层方言（`toy`），在上面做语言专属优化，再一档一档降到通用的 affine、最终到 LLVM——而不是一开始就和底层机器细节纠缠。

#### 4.1.2 核心流程：七章的渐进式构建

官方教程分七章，每一章在前一章基础上加一块能力，对应 `mlir/examples/toy/Ch1` … `Ch7` 七个目录。`_index.md` 给出了每章的主题：

1. **Ch1**：定义 Toy 语言与它的 AST（词法 + 语法 + AST 数据结构）。
2. **Ch2**：遍历 AST 发射 MLIR 方言，引入 MLIR 基础概念。
3. **Ch3**：用模式重写系统（RewritePattern）做语言专属的高层优化。
4. **Ch4**：用接口（Interfaces）写与方言无关的通用变换（形状推断、内联）。
5. **Ch5**：部分下降到低层方言（toy → affine）。
6. **Ch6**：下降到 LLVM 方言并做代码生成（affine → LLVM 方言 → LLVM IR）。
7. **Ch7**：扩展 Toy，加入自定义复合类型。

把这七章映射到我们关心的主线，就是：

```
源码(.toy)
   │  Ch1: Lexer + Parser → AST
   ▼
AST
   │  Ch2: MLIRGen → toy 方言 MLIR
   ▼
toy 方言 MLIR
   │  Ch3: 模式优化（规范化）
   │  Ch4: 内联 + 形状推断（接口）
   ▼
优化后的 toy 方言 MLIR
   │  Ch5: 部分下降 → affine + arith + func + memref
   ▼
affine 方言 MLIR
   │  Ch6: 收尾下降 → LLVM 方言 → LLVM IR / JIT
   ▼
LLVM IR / 可执行
```

本讲聚焦其中三段：Ch2（MLIRGen）、Ch5（降到 affine）、Ch6（降到 LLVM 并翻译）。Ch3/Ch4 的优化在第 4.3 节顺带提及，详细机制已在 u7-l3 讲过。

#### 4.1.3 源码精读：Ch1 的最简驱动

在进入 MLIR 之前，先看最朴素的 Ch1 驱动，它**完全没有 MLIR**，只做「读文件 → 解析 → dump AST」。这能帮我们看清「骨架」，之后各章只是往这个骨架上挂更多的 `case`。

驱动入口定义了一个 `emit` 命令行选项，Ch1 只支持 `ast`：

```cpp
// mlir/examples/toy/Ch1/toyc.cpp
enum Action { None, DumpAST };

static cl::opt<enum Action>
    emitAction("emit", cl::desc("Select the kind of output desired"),
               cl::values(clEnumValN(DumpAST, "ast", "output the AST dump")));
```

> 参见 [Ch1/toyc.cpp:33-39](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/mlir/examples/toy/Ch1/toyc.cpp#L33-L39)：定义 `emit` 选项，Ch1 阶段唯一能做的就是 dump AST。

`main` 的逻辑非常短：解析命令行 → 读文件并解析成 `ModuleAST` → 按 `emitAction` 分派：

```cpp
// mlir/examples/toy/Ch1/toyc.cpp
int main(int argc, char **argv) {
  cl::ParseCommandLineOptions(argc, argv, "toy compiler\n");

  auto moduleAST = parseInputFile(inputFilename);
  if (!moduleAST)
    return 1;

  switch (emitAction) {
  case Action::DumpAST:
    dump(*moduleAST);
    return 0;
  default:
    llvm::errs() << "No action specified (parsing only?), use -emit=<action>\n";
  }
  return 0;
}
```

> 参见 [Ch1/toyc.cpp:56-72](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/mlir/examples/toy/Ch1/toyc.cpp#L56-L72)：Ch1 的 `main`，只做 parse + dump AST。注意此时还没有任何 `MLIRContext`。

`parseInputFile` 用 `LexerBuffer` + `Parser` 把源码文本变成 `std::unique_ptr<toy::ModuleAST>`：

> 参见 [Ch1/toyc.cpp:42-54](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/mlir/examples/toy/Ch1/toyc.cpp#L42-L54)：把文件读进 `MemoryBuffer`，交给 `Lexer`/`Parser`，产出 `ModuleAST`。

到 Ch6，这个 `enum Action` 会膨胀出 `DumpMLIR / DumpMLIRAffine / DumpMLIRLLVM / DumpLLVMIR / RunJIT`，对应主线上每一个中间产物——这正是第 4.3 节要看的。

#### 4.1.4 代码实践：观察 AST 形态

先把 Toy 的 AST 数据结构摸清楚，这对理解后面的 MLIRGen 至关重要。AST 的根是 `ModuleAST`，它装着一组 `FunctionAST`；每个函数由 `PrototypeAST`（函数签名）和函数体（`ExprASTList`，即表达式的列表）组成；表达式节点派生自 `ExprAST`，用一个枚举 `ExprASTKind` 标识种类（`Expr_Num`、`Expr_BinOp`、`Expr_Literal`、`Expr_Call`、`Expr_Var`、`Expr_VarDecl`、`Expr_Return`、`Expr_Print`）。

> 参见 [Ch6/include/toy/AST.h:35-59](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/mlir/examples/toy/Ch6/include/toy/AST.h#L35-L59)：`ExprAST` 基类，带 `ExprASTKind` 枚举与 LLVM 风格的 `classof` RTTI。

**实践目标**：理解「AST 节点种类」与「后面要生成的 Operation 种类」之间的对应关系。

操作步骤：

1. 打开 `Ch6/include/toy/AST.h`，逐一列出 `ExprASTKind` 的 8 个值。
2. 对每个种类，在脑中（或纸上）写下它将来应该映射到什么：例如 `Expr_BinOp` 的 `+` 会变成 `toy.add`，`Expr_Num`/`Expr_Literal` 会变成 `toy.constant`，`Expr_Call` 的 `transpose` 会变成 `toy.transpose`。

预期结果：你会得到一张「AST 节点 → toy 方言 Operation」对照表，这正是下一节 MLIRGen 要实现的东西。这步无需运行任何命令，是纯阅读型实践。

#### 4.1.5 小练习与答案

**练习 1**：Ch1 的 `toyc.cpp` 里没有任何 `mlir::` 的头文件，为什么它仍然能编译运行？

**答案**：因为 Ch1 只做词法分析和语法分析（Lexer + Parser），产物是 Toy 自定义的 `ModuleAST`，根本不涉及 MLIR 的 IR。MLIR 要到 Ch2 引入 `MLIRGen` 后才登场。Ch1 用最简骨架证明了「前端可以独立于 MLIR 存在」。

**练习 2**：`ModuleAST` 与 `FunctionAST`、`ExprAST` 是什么关系？

**答案**：组合（has-a）关系。`ModuleAST` 持有一个 `std::vector<FunctionAST>`；`FunctionAST` 持有 `PrototypeAST`（签名）和 `ExprASTList`（函数体）；`ExprASTList` 是 `std::vector<std::unique_ptr<ExprAST>>`。整棵 AST 由 `unique_ptr` 管理所有权，呈树状（见 [AST.h:230-240](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/mlir/examples/toy/Ch6/include/toy/AST.h#L230-L240)）。

---

### 4.2 MLIRGen：把 AST 翻译成 MLIR Operation

#### 4.2.1 概念说明

有了 AST，下一步要把它变成 MLIR。这一步叫 **MLIRGen**（MLIR 生成），是 Ch2 的核心，代码在 `Ch2/mlir/MLIRGen.cpp`（Ch6 沿用同样的实现）。

它的任务可以用一句话概括：**遍历 AST 树，对每个节点调用对应的 `OpBuilder::create`，产出一个 MLIR Operation**。这和 Kaleidoscope 里给每个 AST 节点挂一个 `codegen()` 虚函数的思路完全一致（对照 u2-l3），区别只在于产物从「LLVM IR 指令」换成了「MLIR Operation」，而且这些 Operation 属于 Toy 自己的 `toy` 方言（如 `toy.constant`、`toy.add`、`toy.mul`、`toy.transpose`、`toy.print`、`toy.return`）。

为什么先用 `toy` 方言而不是直接生成 affine 或 LLVM？因为 `toy.add` 这种高层操作携带了「这是逐元素张量加法」的完整语义，后面可以在这一层做形状推断、转置消除等语言专属优化。一旦过早降到循环，这些高层信息就丢了。这就是「渐进式下降」要保留高层方言的理由。

#### 4.2.2 核心流程

MLIRGen 的整体结构是一个带「重载分派」的访问者 `MLIRGenImpl`：

```
mlirGen(ModuleAST)        → 创建 ModuleOp，逐个函数调用 mlirGen(FunctionAST)，最后 verify
   └─ mlirGen(FunctionAST) → 创建 toy.FuncOp，建作用域，登记参数，codegen 函数体
        └─ mlirGen(ExprASTList) → 遍历语句列表，按语句种类分派
             └─ mlirGen(ExprAST&) → 用 ExprASTKind 做 switch，转到具体节点的 mlirGen
                  ├─ mlirGen(BinaryExprAST) → toy.add / toy.mul
                  ├─ mlirGen(LiteralExprAST) → toy.constant（带 DenseElementsAttr）
                  ├─ mlirGen(VariableExprAST) → 查符号表
                  ├─ mlirGen(CallExprAST)    → toy.transpose / toy.generic_call
                  └─ ...
```

几个贯穿始终的「助手」：

- `mlir::OpBuilder builder`：状态化的 Operation 构造器，记住当前插入点（Insertion Point）。这正是 u7-l1 提到的 `OpBuilder`，和 u3-l4 的 `IRBuilder` 同名同思路但面向 MLIR。
- `llvm::ScopedHashTable<StringRef, mlir::Value> symbolTable`：变量名 → Value 的符号表，进入函数体/块时压一层作用域，退出时弹出，天然支持作用域嵌套。
- `theModule`：一个 `mlir::ModuleOp`，是整份 MLIR 的根容器（对应一个 Toy 源文件）。

#### 4.2.3 源码精读

**(a) 顶层：把整个模块翻译成 ModuleOp**

公开入口 `mlirGen(ModuleAST &)` 创建空的 `ModuleOp`，逐个函数生成，最后用 `mlir::verify` 自检结构合法性：

```cpp
// mlir/examples/toy/Ch2/mlir/MLIRGen.cpp
mlir::ModuleOp mlirGen(ModuleAST &moduleAST) {
  theModule = mlir::ModuleOp::create(builder.getUnknownLoc());
  for (FunctionAST &f : moduleAST)
    mlirGen(f);
  if (failed(mlir::verify(theModule))) {
    theModule.emitError("module verification error");
    return nullptr;
  }
  return theModule;
}
```

> 参见 [Ch2/mlir/MLIRGen.cpp:65-82](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/mlir/examples/toy/Ch2/mlir/MLIRGen.cpp#L65-L82)：建 `ModuleOp`、逐函数生成、`verify` 校验。注意 `ModuleOp::create` 是工厂方法（u7-l1 讲过 Operation 必须经工厂堆分配）。

**(b) 函数：创建 toy.FuncOp 并登记参数**

`mlirGen(FunctionAST &)` 展示了「作用域 + 插入点」的经典用法。它先开一个新的符号表作用域，创建 `toy.FuncOp`，把入口块的参数与 AST 参数一一对应登记进符号表，再把插入点设到入口块开头，最后 codegen 函数体：

```cpp
// mlir/examples/toy/Ch2/mlir/MLIRGen.cpp
mlir::toy::FuncOp mlirGen(FunctionAST &funcAST) {
  ScopedHashTableScope<llvm::StringRef, mlir::Value> varScope(symbolTable);
  builder.setInsertionPointToEnd(theModule.getBody());
  mlir::toy::FuncOp function = mlirGen(*funcAST.getProto());
  ...
  mlir::Block &entryBlock = function.front();
  auto protoArgs = funcAST.getProto()->getArgs();
  for (const auto nameValue : llvm::zip(protoArgs, entryBlock.getArguments())) {
    if (failed(declare(std::get<0>(nameValue)->getName(), std::get<1>(nameValue))))
      return nullptr;
  }
  builder.setInsertionPointToStart(&entryBlock);
  if (mlir::failed(mlirGen(*funcAST.getBody()))) { function.erase(); return nullptr; }
  ...
}
```

> 参见 [Ch2/mlir/MLIRGen.cpp:129-160](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/mlir/examples/toy/Ch2/mlir/MLIRGen.cpp#L129-L160)：函数级 codegen。`ScopedHashTableScope` 是 RAII 作用域；`entryBlock.getArguments()` 拿到的是块参数（u7-l1 讲过 Block 可带参数），这里把它们和 Toy 的形参名绑定。

函数末尾还有个有意思的细节：如果函数体最后一条是带返回值的 `toy.return`，就反向给 `FuncOp` 补上返回类型——Toy 的返回类型是「推断」出来的而非声明的：

> 参见 [Ch2/mlir/MLIRGen.cpp:162-176](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/mlir/examples/toy/Ch2/mlir/MLIRGen.cpp#L162-L176)：根据 `ReturnOp` 是否带操作数，回填函数返回类型。

**(c) 表达式分派：switch + RTTI**

`mlirGen(ExprAST &)` 是分派枢纽，用 `ExprASTKind` 做 `switch`，把基类引用 `cast` 成具体子类再调对应重载：

```cpp
// mlir/examples/toy/Ch2/mlir/MLIRGen.cpp
mlir::Value mlirGen(ExprAST &expr) {
  switch (expr.getKind()) {
  case toy::ExprAST::Expr_BinOp:   return mlirGen(cast<BinaryExprAST>(expr));
  case toy::ExprAST::Expr_Var:     return mlirGen(cast<VariableExprAST>(expr));
  case toy::ExprAST::Expr_Literal: return mlirGen(cast<LiteralExprAST>(expr));
  case toy::ExprAST::Expr_Call:    return mlirGen(cast<CallExprAST>(expr));
  case toy::ExprAST::Expr_Num:     return mlirGen(cast<NumberExprAST>(expr));
  default: ... return nullptr;
  }
}
```

> 参见 [Ch2/mlir/MLIRGen.cpp:353-371](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/mlir/examples/toy/Ch2/mlir/MLIRGen.cpp#L353-L371)：表达式分派。这里用的 `cast`/`isa` 是 LLVM 风格 RTTI，由 `AST.h` 里每个子类的 `classof` 支撑（见 4.1.3 的 AST.h 引用）。

**(d) 二元运算：先递归操作数，再造 Operation**

`BinaryExprAST` 的 codegen 体现了「后序遍历」：先把左右子表达式递归生成（得到两个 `Value`），再根据运算符 `+`/`*` 选造 `AddOp` 还是 `MulOp`：

```cpp
// mlir/examples/toy/Ch2/mlir/MLIRGen.cpp
mlir::Value mlirGen(BinaryExprAST &binop) {
  mlir::Value lhs = mlirGen(*binop.getLHS());
  if (!lhs) return nullptr;
  mlir::Value rhs = mlirGen(*binop.getRHS());
  if (!rhs) return nullptr;
  auto location = loc(binop.loc());
  switch (binop.getOp()) {
  case '+': return AddOp::create(builder, location, lhs, rhs);
  case '*': return MulOp::create(builder, location, lhs, rhs);
  }
  ...
}
```

> 参见 [Ch2/mlir/MLIRGen.cpp:181-212](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/mlir/examples/toy/Ch2/mlir/MLIRGen.cpp#L181-L212)：二元运算 codegen。`AddOp::create(builder, location, lhs, rhs)` 是强类型的工厂调用（u7-l2 讲过具体 Op 类型是 Operation 的强类型视图）。

**(e) 字面量：用 Attribute 携带常量数据**

多维数组字面量最有代表性。它把嵌套数组展平成一维 `double` 数组，包进 `DenseElementsAttr`（稠密元素属性），附在 `toy.constant` 操作上。这正呼应了 u7-l1 所说「attributes 携带常量数据」：

```cpp
// mlir/examples/toy/Ch2/mlir/MLIRGen.cpp
mlir::Value mlirGen(LiteralExprAST &lit) {
  auto type = getType(lit.getDims());
  std::vector<double> data;
  data.reserve(llvm::product_of(lit.getDims()));
  collectData(lit, data);                      // 展平嵌套数组
  mlir::Type elementType = builder.getF64Type();
  auto dataType = mlir::RankedTensorType::get(lit.getDims(), elementType);
  auto dataAttribute =
      mlir::DenseElementsAttr::get(dataType, llvm::ArrayRef(data));
  return ConstantOp::create(builder, loc(lit.loc()), type, dataAttribute);
}
```

> 参见 [Ch2/mlir/MLIRGen.cpp:261-283](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/mlir/examples/toy/Ch2/mlir/MLIRGen.cpp#L261-L283)：字面量 → `toy.constant`。`collectData`（[L293-302](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/mlir/examples/toy/Ch2/mlir/MLIRGen.cpp#L293-L302)）递归把 `[[1,2],[3,4]]` 展平为 `[1,2,3,4]`。

**(f) 类型构造**

所有 Toy 值都是张量。`getType` 把一个形状（`std::vector<int64_t>`）变成 MLIR 类型：空形状→无秩张量（`UnrankedTensorType`，用于函数形参，类型待推断），否则→有秩张量：

> 参见 [Ch2/mlir/MLIRGen.cpp:430-442](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/mlir/examples/toy/Ch2/mlir/MLIRGen.cpp#L430-L442)：`getType` 的两种分支。

最后，对外暴露的 C 函数只是把工作转给实现类：

> 参见 [Ch2/mlir/MLIRGen.cpp:450-453](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/mlir/examples/toy/Ch2/mlir/MLIRGen.cpp#L450-L453)：公开 API `toy::mlirGen`，返回 `OwningOpRef<ModuleOp>`（拥有所有权的 ModuleOp）。

#### 4.2.4 代码实践：手工跟踪一段 Toy 代码的 MLIRGen

**实践目标**：把 MLIRGen 的递归过程在脑中跑一遍，体会「AST 节点 → Operation」的映射。

操作步骤：

1. 设想一段 Toy 源码：

   ```
   def main() {
     var a<2, 1> = [1, 2];
     print(a);
   }
   ```

2. 跟踪 `mlirGen` 调用链：`ModuleAST → FunctionAST(main) → ExprASTList`，在语句列表里先遇到 `VarDeclExprAST`（声明 `a`），再遇到 `PrintExprAST`。
3. 对 `var a<2,1> = [1,2]`：进入 `mlirGen(VarDeclExprAST)`（[L377-401](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/mlir/examples/toy/Ch2/mlir/MLIRGen.cpp#L377-L401)），它先 codegen 初值 `[1,2]`（走 `LiteralExprAST` 分支，造 `toy.constant`），由于声明带 `<2,1>` 形状，再补一个 `toy.reshape`，最后把结果登记进符号表。

需要观察的现象/预期结果：你能写出这段代码大致对应的 MLIR 文本，形如：

```
"toy.constant"() {value = dense<...>} : () -> tensor<2x1xf64>
```

（精确文本「待本地验证」，但结构应是「constant 给初值 → 可能的 reshape → 登记 a → print(a)」。）无需运行命令，这是源码阅读型实践。

#### 4.2.5 小练习与答案

**练习 1**：MLIRGen 里 `OpBuilder` 与符号表 `ScopedHashTable` 各自承担什么职责？为什么要分两个对象？

**答案**：`OpBuilder` 负责「构造 Operation 并放到当前插入点」（机制层）；`ScopedHashTable` 负责「维护变量名到 Value 的映射、随作用域进出栈」（语义层）。分开是因为构造 IR 和查名字是两件正交的事：同一个 builder 可以在不同函数间复用，而符号表必须随作用域严格嵌套；把它们耦合在一起会破坏作用域的清晰性。

**练习 2**：为什么 `mlirGen(ExprAST &)` 要用 `switch` 而不是 C++ 的虚函数（像 Kaleidoscope 那样给每个节点挂 `codegen()`）？

**答案**：两种做法都可行，Toy 选择 `switch + cast` 是为了让「AST 的定义」和「MLIR 生成的逻辑」解耦——AST 类（`AST.h`）不需要 `#include` 任何 MLIR 头文件，保持纯前端。把生成逻辑集中在一个 `MLIRGenImpl` 文件里，也便于一次性阅读整条翻译规则。

---

### 4.3 编译流水线编排：toyc.cpp 的 loadAndProcessMLIR

#### 4.3.1 概念说明

MLIRGen 只产出 `toy` 方言的 MLIR。要把它一路降到 LLVM，需要在驱动里用 `mlir::PassManager` 装配一条流水线。这一节看 Ch6 的 `toyc.cpp` 如何编排这条流水线——它是前面 u7-l3 讲的 PassManager 的真实用法。

Ch6 的 `toyc.cpp` 相比 Ch1 多了一整套 `emit` 选项，每多降一档就多一个观察点：

```cpp
// mlir/examples/toy/Ch6/toyc.cpp
enum Action {
  None, DumpAST, DumpMLIR, DumpMLIRAffine, DumpMLIRLLVM, DumpLLVMIR, RunJIT
};
```

> 参见 [Ch6/toyc.cpp:73-94](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/mlir/examples/toy/Ch6/toyc.cpp#L73-L94)：`emit` 选项枚举。从 `mlir` → `mlir-affine` → `mlir-llvm` → `llvm` → `jit`，恰好对应主线每一档。

#### 4.3.2 核心流程：流水线的三段

`loadAndProcessMLIR` 是整条流水线的编排函数，分三段：**优化 toy 方言**、**降到 affine**、**降到 LLVM 方言**。每一段是否执行取决于用户选的 `emit` 档位（用枚举大小比较 `emitAction >= Action::DumpMLIRAffine` 来判断「要不要继续往下走」）：

```
emit=mlir        → 只优化 toy（若 -opt）
emit=mlir-affine → 优化 toy + 降到 affine
emit=mlir-llvm   → 上面全部 + 降到 LLVM 方言
emit=llvm        → 上面全部（最后再 translate 到 LLVM IR）
emit=jit         → 上面全部（最后 JIT 执行）
```

#### 4.3.3 源码精读

**(a) 加载输入**

`loadMLIR` 根据 `-x` 选项（`toy` 或 `mlir`）决定是「解析 .toy 源码」还是「直接读一份 .mlir 文本」。若是 `.toy`，就走上一节的 `mlirGen`：

> 参见 [Ch6/toyc.cpp:113-142](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/mlir/examples/toy/Ch6/toyc.cpp#L113-L142)：`loadMLIR`。`.toy` 输入走 `parseInputFile` + `mlirGen`；`.mlir` 输入走 `mlir::parseSourceFile`。

**(b) 装配 PassManager**

整条流水线的核心：

```cpp
// mlir/examples/toy/Ch6/toyc.cpp
mlir::PassManager pm(module.get()->getName());
...
bool isLoweringToAffine = emitAction >= Action::DumpMLIRAffine;
bool isLoweringToLLVM   = emitAction >= Action::DumpMLIRLLVM;

if (enableOpt || isLoweringToAffine) {
  pm.addPass(mlir::createInlinerPass());                 // 把所有函数内联进 main
  mlir::OpPassManager &optPM = pm.nest<mlir::toy::FuncOp>();
  optPM.addPass(mlir::toy::createShapeInferencePass());  // 形状推断
  optPM.addPass(mlir::createCanonicalizerPass());        // 规范化
  optPM.addPass(mlir::createCSEPass());                  // 公共子表达式消除
}

if (isLoweringToAffine) {
  pm.addPass(mlir::toy::createLowerToAffinePass());      // toy → affine
  mlir::OpPassManager &optPM = pm.nest<mlir::func::FuncOp>();
  optPM.addPass(mlir::createCanonicalizerPass());
  optPM.addPass(mlir::createCSEPass());
  if (enableOpt) {
    optPM.addPass(mlir::affine::createLoopFusionPass());
    optPM.addPass(mlir::affine::createAffineScalarReplacementPass());
  }
}

if (isLoweringToLLVM) {
  pm.addPass(mlir::toy::createLowerToLLVMPass());        // → LLVM 方言
  pm.addPass(mlir::LLVM::createDIScopeForLLVMFuncOpPass());
}

if (mlir::failed(pm.run(*module)))
  return 4;
```

> 参见 [Ch6/toyc.cpp:144-198](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/mlir/examples/toy/Ch6/toyc.cpp#L144-L198)：`loadAndProcessMLIR`，流水线装配的全貌。

注意三个细节：

1. **`pm.nest<FuncOp>()`**：把后续 pass 嵌套到函数层级运行（u7-l3 讲过的 `OpPassManager` 分层）。注意 toy 方言层用的是 `toy::FuncOp`，降到 affine 后用的是 `func::FuncOp`——因为下降后函数已经变成标准 `func` 方言的了。
2. **内联先行**：`createInlinerPass` 把所有用户函数内联进 `main`，这样形状推断等优化才能跨函数边界看到完整信息。这也是后续 `LowerToAffineLoops` 注释里「expects that all calls have been inlined」的前提。
3. **下降后清理**：每次下降后都跟一轮 `Canonicalizer + CSE`，因为下降会暴露新的化简机会（u7-l3 讲过 canonicalizer 跑到不动点）。

**(c) 分派输出**

`main` 根据最终档位决定输出形式：MLIR 文本（`module->dump()`）、LLVM IR（`dumpLLVMIR`）或 JIT 执行（`runJit`）：

> 参见 [Ch6/toyc.cpp:291-332](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/mlir/examples/toy/Ch6/toyc.cpp#L291-L332)：`main` 的分派。注意 `main` 还会 `getOrLoadDialect<ToyDialect>`（[L309](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/mlir/examples/toy/Ch6/toyc.cpp#L309)），这正是 u7-l2 讲的「按需加载方言」。

#### 4.3.4 代码实践：对比不同档位的输出

**实践目标**：直观感受「渐进式下降」每一档的 IR 形态差异。

操作步骤（需已构建 toyc-ch6，构建方法见第 5 节综合实践；若未构建则跳到「源码阅读型」部分）：

1. 准备 `code.toy`：
   ```
   def main() {
     var a<2, 1> = [1, 2];
     var b<2, 1> = [3, 4];
     print(a + b);
   }
   ```
2. 依次运行：
   ```
   toyc-ch6 -emit=mlir        code.toy     # toy 方言
   toyc-ch6 -emit=mlir-affine code.toy     # 降到 affine 循环
   toyc-ch6 -emit=mlir-llvm   code.toy     # 降到 LLVM 方言
   ```

需要观察的现象：从 `toy.add`（一行高层运算）→ 一组嵌套 `affine.for` + `affine.load/store`（逐元素循环）→ `llvm.mlir` 系列操作（指针、GEP、call printf）。每降一档，IR 更贴近机器、更冗长、但高层语义逐步消失。

预期结果：`mlir` 档能看到 `toy.constant` / `toy.add` / `toy.print`；`mlir-affine` 档这些 `toy.*` 消失，换成 `affine.for` 和 `memref.alloc`；`mlir-llvm` 档连 `affine.*` 也消失，换成 `llvm.*`。精确输出「待本地验证」。

**源码阅读型补充**（无需构建）：直接对照 [loadAndProcessMLIR](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/mlir/examples/toy/Ch6/toyc.cpp#L144-L198) 解释「为什么 `-emit=mlir` 不会触发下降」——因为 `isLoweringToAffine` 为假，两个 `if` 块都被跳过，只跑了可选的 toy 层优化。

#### 4.3.5 小练习与答案

**练习 1**：为什么内联（`createInlinerPass`）要放在形状推断和下降之前？

**答案**：Toy 允许函数之间互相调用，而形状推断（`toy` 方言里张量的形状）需要看到完整的、跨函数的数据流。如果不先内联，形状信息会在函数边界处断开，无秩张量（`*xf64`）就无法被推断成有秩张量，后续下降到 affine 时就无法生成正确维度的循环嵌套。

**练习 2**：`pm.nest<mlir::toy::FuncOp>()` 和 `pm.nest<mlir::func::FuncOp>()` 为什么用了不同的函数类型？

**答案**：前者用在「下降前」，此时函数还是 `toy.FuncOp`（toy 方言）；后者用在「降到 affine 之后」，此时 `toy.FuncOp` 已被改写成标准的 `func.FuncOp`（func 方言）。`nest<T>` 要求 pass 锚定到具体的操作类型，因此必须随方言切换而切换。

---

### 4.4 渐进式 Lowering：Toy → Affine → LLVM 方言

#### 4.4.1 概念说明

本节是全讲的「下降」核心，对应官方教程 Ch5（降到 affine）和 Ch6（降到 LLVM）。这里把 u7-l3 讲的 Conversion 机制用到极致：每一步下降都由一个 Pass 完成，Pass 内部定义一个 `ConversionTarget`（终点：哪些操作合法）和一组 `OpConversionPattern`（怎么改写），然后用 `applyPartialConversion` 或 `applyFullConversion` 驱动。

两步下降的分工：

- **`LowerToAffineLoops.cpp`（toy → affine）**：把 `toy.add`/`toy.mul`/`toy.constant`/`toy.transpose` 这些逐元素张量运算，展开成「分配 memref + 嵌套 affine.for + affine.load/store」的循环。这是**部分下降**：故意保留 `toy.print` 不降（因为 print 涉及运行时，留到下一步）。
- **`LowerToLLVM.cpp`（→ LLVM 方言）**：把剩余的 affine/arith/func/scf/memref 一并降到 LLVM 方言，并补上 `toy.print` 的下降（展开成调用 `printf` 的循环）。这是**完全下降**：目标是「只剩 LLVM 方言」。

为什么分两步而不是一步到 LLVM？因为 affine 层是非常适合做循环优化（循环融合、标量替换）的抽象层级——在 `toy` 层循环还没显式化，在 LLVM 层循环已经被拍平成基本块，都做不了 affine 优化。**在合适的抽象层级做合适的事**，这就是渐进式下降的精髓。

#### 4.4.2 核心流程

**部分下降（toy → affine）的标准三件套**：

```
1. 定义 ConversionTarget：
     - 合法：affine / arith / func / memref / Builtin 方言
     - 非法：整个 toy 方言
     - 例外：toy.print 标记为「动态合法」（操作数类型不再是张量时才算合法）
2. 注册一组 OpConversionPattern：AddOpLowering / MulOpLowering / ConstantOpLowering
     / TransposeOpLowering / FuncOpLowering / ReturnOpLowering / PrintOpLowering
3. applyPartialConversion(module, target, patterns)
```

「部分下降」与「完全下降」的区别就在第 1 步：部分下降允许某些操作保留（用 `addDynamicallyLegalOp`），完全下降不允许（用 `applyFullConversion`，任何残留非法操作都会报错）。

#### 4.4.3 源码精读

**(a) 下降的「循环嵌套生成器」**

`LowerToAffineLoops.cpp` 里最关键的工具函数是 `lowerOpToLoops`：它接收一个逐元素运算，为结果分配一个 memref，然后按张量的每个维度生成一层 `affine.for`，在最内层用回调算出「这个位置该存什么值」：

```cpp
// mlir/examples/toy/Ch6/mlir/LowerToAffineLoops.cpp
static void lowerOpToLoops(Operation *op, PatternRewriter &rewriter,
                           LoopIterationFn processIteration) {
  auto tensorType = llvm::cast<RankedTensorType>((*op->result_type_begin()));
  auto loc = op->getLoc();
  auto memRefType = convertTensorToMemRef(tensorType);
  auto alloc = insertAllocAndDealloc(memRefType, loc, rewriter);   // 分配/释放

  SmallVector<int64_t, 4> lowerBounds(tensorType.getRank(), 0);
  SmallVector<int64_t, 4> steps(tensorType.getRank(), 1);
  affine::buildAffineLoopNest(                                    // 每维一层循环
      rewriter, loc, lowerBounds, tensorType.getShape(), steps,
      [&](OpBuilder &nestedBuilder, Location loc, ValueRange ivs) {
        Value valueToStore = processIteration(nestedBuilder, ivs); // 内层算值
        affine::AffineStoreOp::create(nestedBuilder, loc, valueToStore, alloc, ivs);
      });
  rewriter.replaceOp(op, alloc);
}
```

> 参见 [LowerToAffineLoops.cpp:78-106](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/mlir/examples/toy/Ch6/mlir/LowerToAffineLoops.cpp#L78-L106)：循环嵌套生成器。`insertAllocAndDealloc`（[L56-69](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/mlir/examples/toy/Ch6/mlir/LowerToAffineLoops.cpp#L56-L69)）把分配移到块首、释放移到块尾。

**`toy.add` 怎么降？** 它继承一个模板 `BinaryOpLowering`，回调里就是「取出左右操作数在当前下标的元素，做 `arith.addf`」：

```cpp
// mlir/examples/toy/Ch6/mlir/LowerToAffineLoops.cpp
template <typename BinaryOp, typename LoweredBinaryOp>
struct BinaryOpLowering : public OpConversionPattern<BinaryOp> {
  LogicalResult matchAndRewrite(BinaryOp op, OpAdaptor adaptor,
                                ConversionPatternRewriter &rewriter) const final {
    auto loc = op->getLoc();
    lowerOpToLoops(op, rewriter, [&](OpBuilder &builder, ValueRange loopIvs) {
      auto loadedLhs = affine::AffineLoadOp::create(builder, loc, adaptor.getLhs(), loopIvs);
      auto loadedRhs = affine::AffineLoadOp::create(builder, loc, adaptor.getRhs(), loopIvs);
      return LoweredBinaryOp::create(builder, loc, loadedLhs, loadedRhs);  // arith.addf / arith.mulf
    });
    return success();
  }
};
using AddOpLowering = BinaryOpLowering<toy::AddOp, arith::AddFOp>;
using MulOpLowering = BinaryOpLowering<toy::MulOp, arith::MulFOp>;
```

> 参见 [LowerToAffineLoops.cpp:113-138](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/mlir/examples/toy/Ch6/mlir/LowerToAffineLoops.cpp#L113-L138)：模板化的二元运算下降。`toy.AddOp → arith.AddFOp`，`toy.MulOp → arith.MulFOp`。模板参数 `OpAdaptor` 是 u7-l3 讲过的「已转换操作数」适配器。

**(b) 部分下降的目标与驱动**

`ToyToAffineLoweringPass::runOnOperation` 展示了「部分下降」的标准写法——关键是把 `toy.print` 标成动态合法，从而保留它：

```cpp
// mlir/examples/toy/Ch6/mlir/LowerToAffineLoops.cpp
ConversionTarget target(getContext());
target.addLegalDialect<affine::AffineDialect, BuiltinDialect,
                       arith::ArithDialect, func::FuncDialect,
                       memref::MemRefDialect>();
target.addIllegalDialect<toy::ToyDialect>();
// toy.print 暂不下降，但它的操作数要从 tensor 换成 memref
target.addDynamicallyLegalOp<toy::PrintOp>([](toy::PrintOp op) {
  return llvm::none_of(op->getOperandTypes(),
                       [](Type type) { return llvm::isa<TensorType>(type); });
});
RewritePatternSet patterns(&getContext());
patterns.add<AddOpLowering, ConstantOpLowering, FuncOpLowering, MulOpLowering,
             PrintOpLowering, ReturnOpLowering, TransposeOpLowering>(&getContext());
if (failed(applyPartialConversion(getOperation(), target, std::move(patterns))))
  signalPassFailure();
```

> 参见 [LowerToAffineLoops.cpp:325-362](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/mlir/examples/toy/Ch6/mlir/LowerToAffineLoops.cpp#L325-L362)：部分下降的目标与驱动。`PrintOpLowering`（[L244-256](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/mlir/examples/toy/Ch6/mlir/LowerToAffineLoops.cpp#L244-L256)）只更新操作数类型，不真正下降。

**(c) 收尾下降（→ LLVM 方言）**

`LowerToLLVM.cpp` 的策略不同：它不再自己为每种操作写模式，而是**复用 MLIR 内置的一堆「`populateXxx`」转换模式集合**，只亲手写一个真正与 Toy 语义绑定的 `PrintOpLowering`（因为 `toy.print` 要展开成调用 `printf` 的循环）：

```cpp
// mlir/examples/toy/Ch6/mlir/LowerToLLVM.cpp
LLVMConversionTarget target(getContext());
target.addLegalOp<ModuleOp>();
LLVMTypeConverter typeConverter(&getContext());          // memref → LLVM 类型的映射

RewritePatternSet patterns(&getContext());
populateAffineToStdConversionPatterns(patterns);         // affine → 标准循环
populateSCFToControlFlowConversionPatterns(patterns);     // scf → 控制流
mlir::arith::populateArithToLLVMConversionPatterns(typeConverter, patterns);
populateFinalizeMemRefToLLVMConversionPatterns(typeConverter, patterns);
cf::populateControlFlowToLLVMConversionPatterns(typeConverter, patterns);
populateFuncToLLVMConversionPatterns(typeConverter, patterns);
patterns.add<PrintOpLowering>(&getContext());             // 唯一手写的 toy 专属模式

auto module = getOperation();
if (failed(applyFullConversion(module, target, std::move(patterns))))
  signalPassFailure();
```

> 参见 [LowerToLLVM.cpp:193-232](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/mlir/examples/toy/Ch6/mlir/LowerToLLVM.cpp#L193-L232)：收尾下降。注意这里是 `applyFullConversion`（完全下降），目标是「只剩合法的 LLVM 方言操作」。

**(d) `toy.print` 的下降：一个完整的 match+rewrite 范例**

`PrintOpLowering` 是全讲最值得读的一段——它把一个高层 `toy.print` 展开成「为每个维度生成一层 scf.for 循环、对每个元素调用 `printf`」的完整代码序列，还演示了如何在模块里按需插入 `printf` 函数声明和全局字符串：

```cpp
// mlir/examples/toy/Ch6/mlir/LowerToLLVM.cpp
LogicalResult matchAndRewrite(toy::PrintOp op, OpAdaptor adaptor,
                              ConversionPatternRewriter &rewriter) const override {
  ...
  auto printfRef = getOrInsertPrintf(rewriter, parentModule);          // 按需插入 printf 声明
  Value formatSpecifierCst = getOrCreateGlobalString(..., "%f \0", ...);
  ...
  for (unsigned i = 0, e = memRefShape.size(); i != e; ++i) {          // 每维一层 scf.for
    ...
    auto loop = scf::ForOp::create(rewriter, loc, lowerBound, upperBound, step);
    ...
  }
  auto elementLoad = memref::LoadOp::create(rewriter, loc, op.getInput(), loopIvs);
  LLVM::CallOp::create(rewriter, loc, getPrintfType(context), printfRef,
                       ArrayRef<Value>({formatSpecifierCst, elementLoad}));
  rewriter.eraseOp(op);
  return success();
}
```

> 参见 [LowerToLLVM.cpp:64-118](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/mlir/examples/toy/Ch6/mlir/LowerToLLVM.cpp#L64-L118)：`PrintOpLowering` 的 `matchAndRewrite`。`getOrInsertPrintf`（[L133-145](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/mlir/examples/toy/Ch6/mlir/LowerToLLVM.cpp#L133-L145)）和 `getOrCreateGlobalString`（[L149-172](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/mlir/examples/toy/Ch6/mlir/LowerToLLVM.cpp#L149-L172)）展示了下降时如何在模块作用域「补造」辅助定义。这正是 u7-l3 强调的「改写必须经 PatternRewriter」的真实体现。

下降流水线整体可以用文件头注释里的那张图概括：

```
                         Affine --
                                  |
                                  v
                       Arithmetic + Func --> LLVM (Dialect)
                                  ^
                                  |
     'toy.print' --> Loop (SCF) --
```

> 参见 [LowerToLLVM.cpp:14-22](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/mlir/examples/toy/Ch6/mlir/LowerToLLVM.cpp#L14-L22)：文件头注释自带的下降示意图。

#### 4.4.4 代码实践：读懂一个 Conversion Pattern

**实践目标**：把 `ConstantOpLowering` 的改写逻辑读透，掌握「OpConversionPattern」的标准写法。

操作步骤：

1. 打开 [LowerToAffineLoops.cpp 的 ConstantOpLowering（L144-207）](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/mlir/examples/toy/Ch6/mlir/LowerToAffineLoops.cpp#L144-L207)。
2. 回答三个问题：
   - 它从 `toy.constant` 里取出了什么？（答：`DenseElementsAttr constantValue`，即常量数据。）
   - 它怎么存放这些数据？（答：分配一个 memref，用递归 `storeElements` 对每个下标做一次 `affine.store`。）
   - 它最后如何替换原操作？（答：`rewriter.replaceOp(op, alloc)`，用那个分配出来的 memref 替换原 `toy.constant` 的结果。）

需要观察的现象：你会发现 `toy.constant`（一个带属性的操作）被换成了「alloc + 一堆 store」的组合——常量数据从「属性」变成了「运行时填充的内存」。

预期结果：能用一句话复述 `ConstantOpLowering` 的改写：把编译期常量属性展开成运行时逐元素存入 memref 的指令序列。无需运行命令。

#### 4.4.5 小练习与答案

**练习 1**：`applyPartialConversion` 和 `applyFullConversion` 有什么区别？为什么 toy→affine 用前者、收尾降到 LLVM 用后者？

**答案**：`applyFullConversion` 要求目标里**所有**操作最终都合法，若有任何非法操作残留就算失败；`applyPartialConversion` 允许某些操作保持非法（只要它没被模式匹配到也不报错）。toy→affine 用部分下降，是因为故意保留 `toy.print`（标为动态合法）留到下一步处理；收尾降到 LLVM 用完全下降，是因为目标是「彻底只剩 LLVM 方言」，任何残留都意味着下降不完整、应当报错。

**练习 2**：`LowerToLLVM.cpp` 为什么大量复用 `populateXxxConversionPatterns`，而不是像 `LowerToAffineLoops.cpp` 那样手写每个模式？

**答案**：因为到收尾这一步，剩下的 affine/arith/func/scf/memref 都是 MLIR 内置的通用方言，MLIR 已经为它们准备好了成熟的下降模式集合。复用这些 `populate` 函数既避免重复造轮子，也保证和官方保持一致；Toy 只需手写真正属于自己的 `toy.print` 即可。这体现了 MLIR 「站在通用方言肩膀上」的设计红利。

---

### 4.5 收尾：LLVM 方言翻译为 LLVM IR 与 JIT

#### 4.5.1 概念说明

下降到「LLVM 方言」（`llvm.*` 操作）之后，IR 仍然是 MLIR，只是操作长得像 LLVM IR。要变成真正的 LLVM IR（u3 讲的那个 `llvm::Module`），需要一次**翻译（Translation）**，由 `mlir::translateModuleToLLVMIR` 完成。注意「下降（Conversion，方言之间）」和「翻译（Translation，MLIR→LLVM IR）」是两件不同的事——前者在 MLIR 世界内部，后者跨出 MLIR 进入 LLVM 世界。

Ch6 的 `toyc.cpp` 提供两个终点：`-emit=llvm`（dump LLVM IR 文本）和 `-emit=jit`（JIT 编译并执行 `main`）。

#### 4.5.2 核心流程

```
LLVM 方言 MLIR
   │  registerLLVMDialectTranslation(...)   // 注册「LLVM 方言 → LLVM IR」的翻译器
   │  translateModuleToLLVMIR(module, ctx)  // 翻译成 llvm::Module
   ▼
llvm::Module（LLVM IR）
   │  makeOptimizingTransformer(...)        // 可选：跑一遍 LLVM 优化流水线
   ├── emit=llvm  → 打印 LLVM IR 文本
   └── emit=jit   → ExecutionEngine::create + invokePacked("main")  // JIT 执行
```

#### 4.5.3 源码精读

**(a) 翻译并打印 LLVM IR**

`dumpLLVMIR` 先注册翻译器，再调用 `translateModuleToLLVMIR`，可选地跑一遍优化，最后打印：

```cpp
// mlir/examples/toy/Ch6/toyc.cpp
mlir::registerBuiltinDialectTranslation(*module->getContext());
mlir::registerLLVMDialectTranslation(*module->getContext());
llvm::LLVMContext llvmContext;
auto llvmModule = mlir::translateModuleToLLVMIR(module, llvmContext);
...
auto optPipeline = mlir::makeOptimizingTransformer(
    /*optLevel=*/enableOpt ? 3 : 0, /*sizeLevel=*/0, /*targetMachine=*/nullptr);
if (auto err = optPipeline(llvmModule.get())) { ... }
llvm::errs() << *llvmModule << "\n";
```

> 参见 [Ch6/toyc.cpp:214-256](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/mlir/examples/toy/Ch6/toyc.cpp#L214-L256)：`dumpLLVMIR`。`translateModuleToLLVMIR` 把 MLIR 的 LLVM 方言翻译成真正的 `llvm::Module`；`makeOptimizingTransformer` 是可选的 LLVM 端优化（对照 u4 的 pass 流水线）。

**(b) JIT 执行**

`runJit` 用 MLIR 的 `ExecutionEngine`（底层是 LLVM ORC JIT，对照 u8-l1）把模块即时编译并调用 `main`：

```cpp
// mlir/examples/toy/Ch6/toyc.cpp
auto optPipeline = mlir::makeOptimizingTransformer(enableOpt ? 3 : 0, 0, nullptr);
mlir::ExecutionEngineOptions engineOptions;
engineOptions.transformer = optPipeline;
auto maybeEngine = mlir::ExecutionEngine::create(module, engineOptions);
auto &engine = maybeEngine.get();
auto invocationResult = engine->invokePacked("main");
```

> 参见 [Ch6/toyc.cpp:258-289](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/mlir/examples/toy/Ch6/toyc.cpp#L258-L289)：`runJit`。`invokePacked("main")` 通过名字找到并执行 JIT 编译后的 `main` 函数。

到这里，从一行 Toy 源码到机器执行的全链路就闭合了。

#### 4.5.4 代码实践：观察从 MLIR 到 LLVM IR 的跃迁

**实践目标**：亲眼看到「LLVM 方言（MLIR）」和「LLVM IR（llvm::Module）」的区别。

操作步骤（需已构建 toyc-ch6）：

1. 用同一个 `code.toy`，分别输出最后两档：
   ```
   toyc-ch6 -emit=mlir-llvm code.toy     # LLVM 方言（仍是 MLIR 语法：{ llvm.* ... }）
   toyc-ch6 -emit=llvm     code.toy      # 真正的 LLVM IR（define i32 @main() ...）
   ```
2. 对比两份输出：前者是 MLIR 的通用语法（操作用 `"llvm.add"` 或 `llvm.add`，带 `%0 = ... : (...)` 类型标注）；后者是标准 LLVM IR 文本（`%1 = fadd double ...`，对照 u2-l2）。

需要观察的现象：`mlir-llvm` 档能看到 MLIR 的 `module` 容器、`llvm.func`、`llvm.mlir.constant` 等；`llvm` 档则变成 `define`、`fadd`、`@printf` 等 LLVM IR。两者描述的是几乎等价的计算，但语法体系不同。

预期结果：`llvm` 档的输出可以喂给 `lli`（u1-l4）执行，或对照 u2-l2 的 .ll 语法阅读。精确输出「待本地验证」。

#### 4.5.5 小练习与答案

**练习 1**：「下降（Conversion）」和「翻译（Translation）」在 MLIR 里有什么区别？

**答案**：下降是 MLIR 内部的方言到方言改写（如 `toy → affine → llvm` 方言），产物仍是 MLIR；翻译是从 MLIR 跨出到另一种 IR 体系（如把 LLVM 方言翻译成 `llvm::Module`），产物不再是 MLIR。本讲里 `LowerToLLVM.cpp` 是下降，`translateModuleToLLVMIR` 是翻译。

**练习 2**：为什么 `runJit` 必须先 `registerLLVMDialectTranslation` 才能 JIT？

**答案**：JIT 引擎底层执行的是 LLVM IR（由 ORC JIT 编译成机器码），而 `toyc` 手里的还是 LLVM 方言的 MLIR。必须先注册翻译器，`ExecutionEngine` 才知道如何把 LLVM 方言翻译成 LLVM IR 再交给 ORC。没有这一步，JIT 就找不到翻译入口。

---

## 5. 综合实践

把本讲所有知识串起来：**亲手跑通 Toy 从源码到执行的完整链路，并在每一档停下来观察 IR**。

### 前置：构建 toyc-ch6

Ch6 的构建依赖 JIT 支持，由 CMake 开关 `MLIR_ENABLE_EXECUTION_ENGINE` 守护（见 [Ch6/CMakeLists.txt:1-4](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/mlir/examples/toy/Ch6/CMakeLists.txt#L1-L4)）。配置 LLVM/MLIR 时需要带上：

```
cmake -G Ninja -DCMAKE_BUILD_TYPE=Release \
      -DLLVM_ENABLE_PROJECTS=mlir \
      -DMLIR_ENABLE_EXECUTION_ENGINE=ON \
      -DLLVM_TARGETS_TO_BUILD=host \
      <path-to-source>/llvm
ninja toyc-ch6
```

> 说明：`toyc-ch6` 由 [Ch6/CMakeLists.txt:22-36](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/mlir/examples/toy/Ch6/CMakeLists.txt#L22-L36) 定义，它把 `toyc.cpp`、`MLIRGen.cpp`、`Dialect.cpp`、`LowerToAffineLoops.cpp`、`LowerToLLVM.cpp`、`ShapeInferencePass.cpp`、`ToyCombine.cpp`、`AST.cpp` 一起编进同一个可执行文件。完整构建较耗时；若环境不允许，下面的「阅读型」任务同样完成本实践目标。

### 任务：在每一档观察 IR

准备一个稍复杂的 `matmul.toy`（Toy 自带测试样例可参考 `mlir/test/Examples/Toy/` 下的 `.toy` 文件）：

```
def main() {
  var a<2, 3> = [[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]];
  var b<3, 2> = [[1.0, 2.0], [3.0, 4.0], [5.0, 6.0]];
  var c<2, 2> = a * transpose(b);   # 注意：Toy 的 * 是逐元素，这里仅为触发 transpose + 运算
  print(c);
}
```

依次执行，并对照源码解释每一步：

1. `toyc-ch6 -emit=ast matmul.toy` —— 看 AST（对应 4.1）。
2. `toyc-ch6 -emit=mlir matmul.toy` —— 看 `toy` 方言 IR（对应 4.2，应出现 `toy.constant` / `toy.transpose` / `toy.mul` / `toy.print`）。
3. `toyc-ch6 -emit=mlir -opt matmul.toy` —— 开 `-opt` 后，看内联、形状推断、规范化（`ToyCombine` 的转置消除等）的效果（对应 4.3）。
4. `toyc-ch6 -emit=mlir-affine matmul.toy` —— 看 `toy.*` 消失、变成 `affine.for` + `memref`（对应 4.4 前半）。
5. `toyc-ch6 -emit=mlir-llvm matmul.toy` —— 看只剩 `llvm.*`（对应 4.4 后半）。
6. `toyc-ch6 -emit=llvm matmul.toy` —— 看真正的 LLVM IR（对应 4.5）。
7. `toyc-ch6 -emit=jit matmul.toy` —— JIT 执行，观察标准输出打印的数值。

### 阅读型替代任务（无需构建）

若无法构建，请完成以下源码追踪任务，同样达成「串联全链路」的目标：

- 从 [Ch6/toyc.cpp 的 main](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/mlir/examples/toy/Ch6/toyc.cpp#L291-L332) 出发，画出当用户输入 `toyc-ch6 -emit=mlir-affine -opt foo.toy` 时的完整函数调用链（`main → loadAndProcessMLIR → loadMLIR → parseInputFile + mlirGen → pm.run → ...`）。
- 在这条链上标注：哪一步对应 4.2（MLIRGen）、哪一步对应 4.3（优化）、哪一步对应 4.4（下降）。
- 写出结论：为什么 MLIR 要把这条链拆成这么多档，而不是像 Kaleidoscope（u2-l3）那样 AST → IR → JIT 一气呵成？

预期结果（结论要点）：因为每一档都是一个合适的优化抽象层级——`toy` 层做语言专属优化（形状推断、转置消除）、`affine` 层做循环优化（融合、标量替换）、`LLVM` 层做指令级优化。单层 IR 无法同时在所有层级都保持高效，这正是 MLIR 多级中间表示的核心价值。

## 6. 本讲小结

- Toy 教程分 7 章，主线是 **源码 → AST（Ch1）→ toy 方言 MLIR（Ch2）→ 高层优化（Ch3/Ch4）→ affine（Ch5）→ LLVM 方言 + LLVM IR/JIT（Ch6）**；本讲聚焦 MLIRGen、降到 affine、降到 LLVM 三段。
- **MLIRGen** 是一个带重载分派的 AST 访问者：用 `OpBuilder::create` 把每个 AST 节点翻译成 `toy` 方言的 Operation，靠 `ScopedHashTable` 管理变量作用域，最后用 `mlir::verify` 自检。
- **流水线编排** 在 Ch6 的 `toyc.cpp` 里完成：`PassManager` 按用户选的 `emit` 档位，依次挂上内联、形状推断、规范化、降到 affine、降到 LLVM 方言等 pass，用 `pm.nest<FuncOp>()` 在合适的层级运行。
- **渐进式下降** 的每一步都是「定义 `ConversionTarget` + 注册 `OpConversionPattern` + `applyPartial/FullConversion`」三件套；toy→affine 用部分下降（保留 `toy.print`），收尾降到 LLVM 用完全下降并大量复用内置 `populateXxx` 模式。
- **翻译与 JIT** 是跨出 MLIR 的最后一步：`translateModuleToLLVMIR` 把 LLVM 方言翻译成真正的 `llvm::Module`，`ExecutionEngine` 再用 ORC JIT 执行；注意「下降（方言间）」与「翻译（跨体系）」是两件不同的事。
- MLIR 的核心价值在于**在合适的抽象层级做合适的事**：高层方言保留语义便于语言专属优化，逐级下降把高层语义逐步「具象化」为机器能执行的指令。

## 7. 下一步学习建议

- **读本教程余下两章**：Ch3（[ToyCombine.cpp](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/mlir/examples/toy/Ch6/mlir/ToyCombine.cpp)）和 Ch4（`ShapeInferencePass.cpp`、接口）展示了 u7-l3 的 RewritePattern 与 u7-l2 的 Interface 的真实用法；Ch7 演示如何给 Toy 加自定义类型，是二次开发的范例。
- **进入执行引擎单元 u8**：本讲结尾的 `ExecutionEngine` + ORC JIT 正是 u8-l1 的主题。读过本讲后再看 u8-l1，你会更清楚「MLIR 的 LLVM 方言」如何衔接到「LLVM 的 JIT 基础设施」。
- **对照 Clang CodeGen（u5-l5）**：Clang 把 AST 翻译成 LLVM IR，Toy 把 AST 翻译成 MLIR——两者都是「AST → IR」的 codegen，但产物层级不同。对比阅读能加深对「为什么要有 MLIR 这一层」的理解。
- **动手扩展**：尝试给 Toy 加一个新的内置运算（如 `toy.sub`），从 `Ops.td` 定义、MLIRGen 发射、`ToyCombine` 优化、`LowerToAffineLoops` 下降一路改到能跑通 `-emit=jit`。这是检验你是否真正理解本讲全链路的最佳练习。
