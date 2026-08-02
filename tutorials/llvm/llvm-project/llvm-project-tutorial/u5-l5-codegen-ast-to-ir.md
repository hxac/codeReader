# CodeGen：从 AST 到 LLVM IR

## 1. 本讲目标

前四讲（u5-l1 到 u5-l4）我们走完了 Clang 的「半边天」：Driver 编排动作、Lexer 切 Token、Parser 搭起 AST、Sema 给 AST 做完语义校验。但到目前为止，产物还是一棵 Clang 自有的、跟具体机器无关的抽象语法树（AST）。真正让 LLVM「看得懂」的，是 LLVM IR。本讲要回答的核心问题是：**Clang 怎样把这棵校验过的 AST「翻译」成一整个 `llvm::Module`？这条翻译流水线由谁触发、由谁执行、又在哪里把成果交接给后端？**

学完本讲，你应当能够：

- 说清 CodeGen 在 Clang 里的**触发方式**：它是一个 `FrontendAction`，经 `ParseAST` 把 AST 喂给一个 `ASTConsumer`（`BackendConsumer`），再委托给 `CodeGenerator`。
- 理解**整条代码生成主线**：`CodeGenAction` → `BackendConsumer` → `CodeGeneratorImpl` → `CodeGenModule` → `CodeGenFunction`，并能说清每一棒各干什么。
- 掌握 **`CodeGenModule` 的职责**：模块级状态（mangling、Clang 类型→LLVM 类型、各语言 Runtime）、顶层声明派发、以及「延迟发射（deferred emission）」到不动点的机制。
- 掌握 **`CodeGenFunction` 的职责**：函数级状态、用它持有的 **`IRBuilder`（就是 u3-l4 那个 IRBuilder！）** 逐条把 AST 的 `Stmt`/`Expr` 翻译成 `Instruction`，并理解 `EmitStmt` 的「大 switch」分发模型。
- 会用 `clang -S -emit-llvm` 真实地拿到 CodeGen 的输出，并能把每一条 IR 指令反推回它的 AST 来源。

## 2. 前置知识

进入源码前，先用通俗语言建立四个直觉。

**第一，AST 与 IR 是两套世界（承接 u5-l3、u3-l1）。** Clang 的 AST 节点是 `Decl`（声明）和 `Stmt`/`Expr`（语句/表达式）两大谱系，节点里还留着「源码长什么样」的丰富信息（位置、属性、模板等）。LLVM IR 则是 u3-l1 讲过的 `Module ⊃ Function ⊃ BasicBlock ⊃ Instruction` 那棵树，外加 SSA 的 def-use 链（u3-l2）。**CodeGen 的工作，本质上就是一次「树到树」的翻译**：遍历 AST，为每个有代码含义的节点，用 IRBuilder 在当前函数里造出对应的 IR 指令。AST 里很多节点（类型声明、`using`、模板本身）不产生任何 IR，CodeGen 遇到它们直接跳过。

**第二，谁来遍历 AST？——`ASTConsumer`（承接 u5-l1、u5-l3）。** 回顾 u5-l3：`ParseAST` 每解析出一组顶层声明（`DeclGroupRef`），就回调 `ASTConsumer::HandleTopLevelDecl` 把它「流式」推给下游。换句话说，**AST 的遍历不是 CodeGen 自己驱动的，而是 Parser 边解析边「喂数据」给 CodeGen**。CodeGen 只要实现 `ASTConsumer` 这套回调接口，就能在解析过程中增量地把 AST 变成 IR。这正是 `BackendConsumer` 存在的原因——它是一个 `ASTConsumer`。

**第三，模块级与函数级要分开（这是本讲最重要的结构认知）。** 翻译工作被切成两个粒度：

- **模块级（`CodeGenModule`）**：管「整个翻译单元」级别的事——给函数/变量起 mangled 名字、把 Clang 的 `QualType` 映射成 `llvm::Type`、决定哪些声明该真正发射、处理全局变量/虚表/各语言 Runtime。它持有那个最终的 `llvm::Module`。
- **函数级（`CodeGenFunction`）**：每翻译一个函数体，就**临时 new 一个** `CodeGenFunction`，用它内部持有的 `IRBuilder` 逐语句生成 IR；函数体生成完，这个 `CodeGenFunction` 也就销毁。所以 `CodeGenModule` 是「长寿」的（贯穿整个 TU），`CodeGenFunction` 是「短命」的（一个函数一个）。

**第四，IRBuilder 再次登场（承接 u3-l4）。** u3-l4 讲过 `IRBuilder` 是「构造指令 + 插入基本块」的便捷 API。本讲你会看到：`CodeGenFunction` 里那个 `Builder` 成员，**正是 `IRBuilder` 的一个子类**（`CGBuilderTy`），夹带了一个会自动给指令贴调试信息/位置的自定义插入器。所以理解了 u3-l4 的 `CreateAdd`/`CreateLoad`/`CreateStore`/`SetInsertPoint`，你就理解了 CodeGen 「造指令」的全部手法——区别只在于「现在由 Clang 的 AST 来驱动调用这些 API」。

> 关键术语：CodeGen（代码生成）、`FrontendAction`、`ASTConsumer`、`BackendConsumer`、`CodeGenerator`/`CodeGeneratorImpl`、`CodeGenModule`（CGM）、`CodeGenFunction`（CGF）、mangling（名字改写）、`CGFunctionInfo`、延迟发射（deferred emission）、`EmitStmt`/`EmitDecl`（语句/声明发射）、插入点（Insertion Point）。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `clang/include/clang/CodeGen/CodeGenAction.h` | `CodeGenAction` 类声明——作为 `ASTFrontendAction` 的子类，是 CodeGen 的触发入口；并列出 `EmitAssemblyAction`/`EmitLLVMAction`/`EmitObjAction` 等具体动作。 |
| `clang/lib/CodeGen/BackendConsumer.h` | `BackendConsumer` 类——一个 `ASTConsumer`，持有 `CodeGenerator`，把 AST 回调转发给真正的代码生成器，并在 TU 末尾把生成的 `Module` 交给后端。 |
| `clang/lib/CodeGen/CodeGenAction.cpp` | `CodeGenAction` 的实现：`CreateASTConsumer`（造 `BackendConsumer`）、`ExecuteAction`（驱动 ParseAST）、`BackendConsumer` 的各回调与 `emitBackendOutput` 衔接。 |
| `clang/lib/CodeGen/ModuleBuilder.cpp` | `CodeGeneratorImpl`——连接 `BackendConsumer` 与 `CodeGenModule` 的桥；拥有 `llvm::Module` 与 `CodeGenModule`，把 `HandleTopLevelDecl`/`HandleTranslationUnit` 转译为 `CodeGenModule::EmitTopLevelDecl`/`Release`。 |
| `clang/lib/CodeGen/CodeGenModule.h` | `CodeGenModule` 类声明——模块级 CodeGen 的核心，持有 `ASTContext`、`llvm::Module`、`CodeGenTypes`、各 Runtime、延迟发射表等。 |
| `clang/lib/CodeGen/CodeGenModule.cpp` | `CodeGenModule` 的实现：`EmitTopLevelDecl`（顶层声明派发）、`EmitGlobal`/`EmitGlobalDefinition`/`EmitGlobalFunctionDefinition`（声明→定义发射）、`Release`/`EmitDeferred`（TU 末尾收尾与延迟发射到不动点）。 |
| `clang/lib/CodeGen/CodeGenFunction.h` | `CodeGenFunction` 类声明——函数级 CodeGen 的核心，持有 `CodeGenModule &CGM`、`CGBuilderTy Builder`（IRBuilder）、当前 `llvm::Function *CurFn`。 |
| `clang/lib/CodeGen/CodeGenFunction.cpp` | `CodeGenFunction` 的实现：`GenerateCode`（生成一个函数的入口）、`EmitFunctionBody`（发射函数体）。 |
| `clang/lib/CodeGen/CGStmt.cpp` | `EmitStmt`——语句发射的总分发器（大 `switch`），是 AST `Stmt` → IR 的主入口。 |
| `clang/lib/CodeGen/CGDecl.cpp` | `EmitDecl`/`EmitVarDecl`/`EmitAutoVarDecl`/`EmitAutoVarAlloca`——声明发射，局部变量如何变成 `alloca`+`store`。 |
| `clang/lib/CodeGen/CGBuilder.h` | `CGBuilderTy`——`IRBuilder<TargetFolder, CGBuilderInserter>` 的子类，确认 CodeGen 用的就是 u3-l4 的 IRBuilder。 |

> 说明：本讲规格的「关键源码」指定了 `CodeGenModule.cpp`、`CodeGenFunction.cpp`、`CodeGenAction.cpp` 三个文件；为保证主线完整与行号准确，本讲按真实代码补充引用了 `BackendConsumer.h`、`ModuleBuilder.cpp`、`CodeGenModule.h`、`CodeGenFunction.h`、`CGStmt.cpp`、`CGDecl.cpp`、`CGBuilder.h`——它们正是这条主线真正的实现与契约所在。

## 4. 核心概念与源码讲解

本讲按「从外到内」拆成三个最小模块：4.1 讲 CodeGen 如何被触发并桥接到消费者；4.2 讲模块级 `CodeGenModule`；4.3 讲函数级 `CodeGenFunction` 与 AST→IR 的逐条映射。

### 4.1 触发与桥接：CodeGenAction 与 BackendConsumer

#### 4.1.1 概念说明

在 u5-l1 里我们已经建立了 Clang 的「动作（Action）」模型：一条 `clang` 命令最终被翻译成一组 `Action`，而真正执行编译的是 `FrontendAction`。**CodeGen 本身就是一种 `FrontendAction`**——准确说，`CodeGenAction` 继承自 `ASTFrontendAction`。你日常用的几个开关，其实对应 `CodeGenAction` 的不同子类（见源码地图表）：

- `clang -S -emit-llvm a.c` → `EmitLLVMAction`（产出可读 `.ll`）
- `clang -emit-llvm -c a.c` → `EmitBCAction`（产出位码 `.bc`）
- `clang -S a.c` → `EmitAssemblyAction`（产出汇编 `.s`）
- `clang -c a.c` → `EmitObjAction`（产出目标文件 `.o`）

这些子类只决定「后端输出成什么格式」，前端「把 AST 变成 IR」这一段是共享的，都在基类 `CodeGenAction` 里。

但 `ASTFrontendAction` 自己并不懂 IR——它只负责驱动 `ParseAST`（u5-l3）并把解析出的 AST 喂给一个 `ASTConsumer`。于是 `CodeGenAction` 需要造一个**既懂 AST 回调、又懂如何启动 CodeGen** 的中间人：`BackendConsumer`。它实现了 `ASTConsumer` 接口，内部却持有一个 `CodeGenerator`（`Gen` 成员），把收到的 AST 声明逐批转交给它。

于是整条链可以画成：

```
clang -cc1
  └─ FrontendAction = CodeGenAction (例如 EmitLLVMAction)
       ├─ ExecuteAction()  ──► ASTFrontendAction::ExecuteAction()
       │                        └─ ParseAST()   边解析边回调 Consumer
       └─ CreateASTConsumer() ──► 返回 BackendConsumer
                                    └─ Gen : CodeGenerator  (内部包着 CodeGenModule)
```

#### 4.1.2 核心流程

`BackendConsumer` 作为 `ASTConsumer`，主要实现两个回调（u5-l3 已介绍 `ASTConsumer` 的生命周期）：

1. **`HandleTopLevelDecl(DeclGroupRef)`**：Parser 每解析出一组顶层声明就调一次。`BackendConsumer` 不自己翻译，而是 `Gen->HandleTopLevelDecl(D)` 转交。这一步是**增量**的——IR 在解析过程中逐批生成。
2. **`HandleTranslationUnit(ASTContext)`**：整个 TU 解析完后调一次。这里做两件事：(a) `Gen->HandleTranslationUnit(C)` 让代码生成器「收尾」（发射所有延迟的声明）；(b) 调 `emitBackendOutput(...)` 把最终 `Module` 交给 LLVM 后端，按 `Action` 类型产出 `.ll`/`.bc`/`.s`/`.o`。

注意一个关键边界：**`BackendConsumer` 只负责「转发 + 衔接后端」，真正的 AST→IR 翻译在 `CodeGenerator`（下一棒的 `CodeGeneratorImpl`）及其内部的 `CodeGenModule` 里**。这样的分层让「Clang 前端框架」与「LLVM IR 生成器」解耦。

#### 4.1.3 源码精读

先看 `CodeGenAction` 的类声明，确认它是个 `ASTFrontendAction`，并留意它持有的 `TheModule`、`VMContext`（`LLVMContext`）两个关键成员：

`CodeGenAction` 继承 `ASTFrontendAction`，重写了 `CreateASTConsumer`、`ExecuteAction`、`EndSourceFileAction` 等，并暴露 `takeModule()` 取走生成的 `Module`：[clang/include/clang/CodeGen/CodeGenAction.h:L25-L33](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/include/clang/CodeGen/CodeGenAction.h#L25-L33)（`CodeGenAction` 是一个 `ASTFrontendAction`，内部持有 `TheModule` 与 `VMContext`）。

具体的「输出格式」子类都极薄，只是把一个 `BackendAction` 枚举值传给基类构造函数：[clang/include/clang/CodeGen/CodeGenAction.h:L69-L103](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/include/clang/CodeGen/CodeGenAction.h#L69-L103)（`EmitAssemblyAction`/`EmitBCAction`/`EmitLLVMAction`/`EmitObjAction` 等子类）。

`ExecuteAction` 对普通源文件（非 `.ll`/`.bc` 输入）直接委托给基类 `ASTFrontendAction::ExecuteAction()`——后者会跑 `ParseAST` 并触发 `BackendConsumer` 的回调：[clang/lib/CodeGen/CodeGenAction.cpp:L1164-L1168](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/CodeGen/CodeGenAction.cpp#L1164-L1168)（非 LLVM_IR 输入时，把执行交给 `ASTFrontendAction`，由它驱动解析与 CodeGen）。

`CreateASTConsumer` 负责造出 `BackendConsumer`（同时拿到输出流 `OS`）：[clang/lib/CodeGen/CodeGenAction.cpp:L1033-L1036](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/CodeGen/CodeGenAction.cpp#L1033-L1036)（构造 `BackendConsumer` 并保存到 `BEConsumer`）。

再看 `BackendConsumer` 的声明：它继承 `ASTConsumer`，最关键的成员是 `std::unique_ptr<CodeGenerator> Gen`：[clang/lib/CodeGen/BackendConsumer.h:L28-L46](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/CodeGen/BackendConsumer.h#L28-L46)（`BackendConsumer : public ASTConsumer`，持有 `Gen`、输出流 `AsmOutStream`、`Action` 等）。

`HandleTopLevelDecl` 几乎只是把声明组转发给 `Gen`（计时相关代码可先忽略）：[clang/lib/CodeGen/CodeGenAction.cpp:L170-L185](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/CodeGen/CodeGenAction.cpp#L170-L185)（核心一行 `Gen->HandleTopLevelDecl(D)`）。

`HandleTranslationUnit` 是「收尾 + 交接后端」的所在：先 `Gen->HandleTranslationUnit(C)` 让代码生成器完成所有延迟发射，再用 `emitBackendOutput(...)` 把 `Module` 交给后端：[clang/lib/CodeGen/CodeGenAction.cpp:L241-L252](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/CodeGen/CodeGenAction.cpp#L241-L252)（调 `Gen->HandleTranslationUnit(C)`，这是前端 IR 生成的终点）：[clang/lib/CodeGen/CodeGenAction.cpp:L322-L324](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/CodeGen/CodeGenAction.cpp#L322-L324)（`emitBackendOutput(...)` 把生成的 `Module` 交给 LLVM 后端，按 `Action` 输出最终产物）。

那么 `Gen`（`CodeGenerator`）到底是什么？它由 `CreateLLVMCodeGen` 工厂函数创建，真正的实现类是 `CodeGeneratorImpl`，它同时拥有一个 `llvm::Module M` 和一个 `CodeGenModule Builder`：[clang/lib/CodeGen/ModuleBuilder.cpp:L36-L66](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/CodeGen/ModuleBuilder.cpp#L36-L66)（`CodeGeneratorImpl` 同时持有 `M`（`llvm::Module`）与 `Builder`（`CodeGenModule`）——这就是「桥」的两端）。

`CodeGeneratorImpl::Initialize` 在收到 `ASTContext` 时，给 `Module` 装上 target triple / data layout，并 `new` 出 `CodeGenModule`：[clang/lib/CodeGen/ModuleBuilder.cpp:L158-L173](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/CodeGen/ModuleBuilder.cpp#L158-L173)（设置 triple/datalayout，并构造 `CodeGenModule`）。

`HandleTopLevelDecl` 遍历声明组里的每个声明，调 `Builder->EmitTopLevelDecl(I)`：[clang/lib/CodeGen/ModuleBuilder.cpp:L188-L204](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/CodeGen/ModuleBuilder.cpp#L188-L204)（对每个声明调 `Builder->EmitTopLevelDecl(I)`，把控制权交给 `CodeGenModule`）。

`HandleTranslationUnit` 则调 `Builder->Release()` 收尾，发生错误时清空 `Module`：[clang/lib/CodeGen/ModuleBuilder.cpp:L312-L326](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/CodeGen/ModuleBuilder.cpp#L312-L326)（`Builder->Release()` 是 TU 级收尾）。

至此桥接层讲完：`BackendConsumer` 把 AST 回调转给 `CodeGeneratorImpl`，后者再转给 `CodeGenModule`。真正的翻译从下一节开始。

#### 4.1.4 代码实践

**实践目标**：用 `clang -###` 观察一次 `clang -S -emit-llvm` 被展开成哪些动作，确认 CodeGen 是 cc1 内部的一个 `FrontendAction`。

1. 写一个最小 C 文件 `sample.c`：
   ```c
   int add(int a, int b) { return a + b; }
   ```
2. 运行（只打印命令、不真正执行）：
   ```bash
   clang -### -S -emit-llvm sample.c
   ```
3. 在输出的长命令行里找到 `clang ... -cc1 ...` 那一行。

**需要观察的现象**：`-cc1` 命令里会出现 `-emit-llvm`（以及通常的 `-S`）。这正对应上面讲的 `EmitLLVMAction`——也就是说，Driver 把「输出 LLVM IR 文本」翻译成了 cc1 的 `-emit-llvm` 标志，cc1 据此选择 `CodeGenAction` 的 `EmitLLVMAction` 子类。

**预期结果**：看到一条形如 `"...clang" "-cc1" ... "-emit-llvm" ... "sample.c"` 的命令。（不同发行版/版本的 clang 路径与附加参数不同，关键是确认 `-cc1` 与 `-emit-llvm` 同时存在。）若想真正生成 IR，去掉 `-###` 直接跑 `clang -S -emit-llvm sample.c -o sample.ll` 即可得到 `sample.ll`，为 4.3 节的实践做准备。

#### 4.1.5 小练习与答案

**练习 1**：`BackendConsumer::HandleTopLevelDecl` 自己翻译 AST 吗？如果不是，它做了什么？
> **答案**：不翻译。它只把 `DeclGroupRef` 转发给成员 `Gen->HandleTopLevelDecl(D)`（外加一些计时包装）。真正翻译在 `CodeGenModule::EmitTopLevelDecl`。

**练习 2**：为什么 `CodeGenAction` 要继承 `ASTFrontendAction` 而不是自己直接遍历 AST？
> **答案**：`ASTFrontendAction` 已经封装了「驱动 `ParseAST` + 把 AST 流式喂给 `ASTConsumer`」的标准流程。继承它，CodeGen 只需提供一个 `ASTConsumer`（`BackendConsumer`）即可拿到增量 AST，不必重复实现解析与回调编排。

**练习 3**：`HandleTranslationUnit` 里有两个关键调用 `Gen->HandleTranslationUnit(C)` 与 `emitBackendOutput(...)`，分别属于「前端」还是「后端」？
> **答案**：前者属于前端（Clang CodeGen 的收尾，完成所有延迟的 IR 发射）；后者属于后端（把完整 `Module` 交给 LLVM 后端流水线产出最终文件）。两者衔接点就是「完整的 `llvm::Module`」。

### 4.2 模块级 CodeGen：CodeGenModule

#### 4.2.1 概念说明

`CodeGenModule`（常简称 **CGM**）是「跨函数的模块级状态管理者」，注释写得很直白：它组织「生成 LLVM 代码时所用的、跨函数的状态」。可以把它理解成一个**翻译单元范围内的大总管**，掌管四类事务：

1. **持有产物与上下文**：那个最终的 `llvm::Module`、`ASTContext`、各种编译选项（`CodeGenOptions`/`LangOptions`/`TargetOptions`）。
2. **类型与名字映射**：通过 `CodeGenTypes`（成员 `Types`）把 Clang 的 `QualType` 映射成 `llvm::Type`；通过 `getMangledName` 给每个函数/全局变量算出唯一的符号名（mangling），这是链接的前提。
3. **声明派发与发射决策**：拿到一个顶层声明，判断它「要不要发射、现在发射还是延迟发射」，并把函数/变量分别路由到对应的发射函数。
4. **语言特性 Runtime**：持有 ObjC/OpenCL/OpenMP/CUDA/HLSL 等各自的 Runtime 对象、虚表（`CodeGenVTables`）、调试信息（`CGDebugInfo`）——这些是「跨函数、跨声明」共享的。

注意 `CodeGenModule` 自己**不**直接翻译函数体。它决定「这个函数要发射」之后，会临时创建一个 `CodeGenFunction` 去干函数体的活（见 4.3）。

#### 4.2.2 核心流程

顶层声明进入 `CodeGenModule` 的主线是「派发 → 决策 → 发射」三步：

```
EmitTopLevelDecl(D)            ── 按 D 的 Decl::Kind 大 switch
   ├─ FunctionDecl/VarDecl  ──► EmitGlobal(GD)
   │                              ├─ 先处理别名/ifunc/CUDA/OpenMP 等特殊情况
   │                              ├─ 若只是声明（非定义）：登记/取地址后返回
   │                              ├─ MustBeEmitted && MayBeEmittedEagerly → 立即 EmitGlobalDefinition
   │                              └─ 否则：放入延迟表 DeferredDecls / DeferredDeclsToEmit
   │
   └─ Namespace/Record/...   ──► 递归 EmitTopLevelDecl 或仅写调试信息

EmitGlobalDefinition(GD)       ── 区分函数/变量
   └─ FunctionDecl          ──► EmitGlobalFunctionDefinition(GD)
                                  └─ CodeGenFunction(*this).GenerateCode(GD, Fn, FI)

（TU 末尾）Release()            ── 收尾
   └─ EmitDeferred()           ── 反复处理 DeferredDeclsToEmit，直到不再变化（不动点）
```

最值得理解的是**延迟发射（deferred emission）**。C/C++ 里很多声明「写出来了但不一定会用到」（比如 `static` 函数、内联函数）。为了避免生成无用代码，`EmitGlobal` 默认不立即翻译它们，而是登记到 `DeferredDecls`（按 mangled 名字索引）。只有当某个声明「必须发射」（`MustBeEmitted`，例如有外部链接的定义）或「被引用到了」时，才搬进 `DeferredDeclsToEmit` 队列。等到 TU 末尾 `Release()` 调 `EmitDeferred()`，用一个**循环到不动点（fixpoint）**的过程把队列里所有声明真正发射——因为发射一个函数可能让它引用的另一个延迟声明变得需要发射，所以必须反复扫描直到队列不再增长。

#### 4.2.3 源码精读

`CodeGenModule` 的类声明与注释：[clang/lib/CodeGen/CodeGenModule.h:L334-L358](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/CodeGen/CodeGenModule.h#L334-L358)（注释点明它组织「跨函数状态」，成员含 `ASTContext &Context`、各 `LangOpts`）。

它持有的「延迟发射」三张表，是理解模块级行为的关键：[clang/lib/CodeGen/CodeGenModule.h:L414-L431](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/CodeGen/CodeGenModule.h#L414-L431)（`DeferredDecls`（名字→声明）、`DeferredDeclsToEmit`（已确定要发射的队列）、`EmittedDeferredDecls`（已发射记录））。

`EmitTopLevelDecl` 是顶层声明派发的大 `switch`。函数与变量都走 `EmitGlobal`，模板/命名空间等则递归或仅处理调试信息：[clang/lib/CodeGen/CodeGenModule.cpp:L7962-L7979](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/CodeGen/CodeGenModule.cpp#L7962-L7979)（`switch (D->getKind())`：`Function`/`CXXMethod` 等走 `EmitGlobal`，`Var` 也走 `EmitGlobal`）。

`EmitGlobal` 的「立即发射 vs 延迟」决策，是这一节的核心逻辑：[clang/lib/CodeGen/CodeGenModule.cpp:L4784-L4815](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/CodeGen/CodeGenModule.cpp#L4784-L4815)（`MustBeEmitted(Global) && MayBeEmittedEagerly(Global)` 为真才立即 `EmitGlobalDefinition`；否则按是否已被引用，分别放入 `DeferredDeclsToEmit` 或 `DeferredDecls`）。

`EmitGlobalDefinition` 区分函数与变量，函数进一步走 `EmitGlobalFunctionDefinition`：[clang/lib/CodeGen/CodeGenModule.cpp:L4995-L5028](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/CodeGen/CodeGenModule.cpp#L4995-L5028)（`FunctionDecl` → `EmitGlobalFunctionDefinition(GD, GV)`，`VarDecl` → `EmitGlobalVarDefinition`）。

`EmitGlobalFunctionDefinition` 是模块级到函数级的「交接点」——它准备好 `llvm::Function`（设链接性、可见性），然后**临时构造一个 `CodeGenFunction` 并调用 `GenerateCode`**：[clang/lib/CodeGen/CodeGenModule.cpp:L6995-L7031](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/CodeGen/CodeGenModule.cpp#L6995-L7031)（关键一行 `CodeGenFunction(*this).GenerateCode(GD, Fn, FI)`——`CodeGenFunction` 是临时对象，生成完即销毁）。

TU 末尾的 `Release()` 调 `EmitDeferred()` 完成延迟发射：[clang/lib/CodeGen/CodeGenModule.cpp:L1131-L1135](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/CodeGen/CodeGenModule.cpp#L1131-L1135)（`Release()` 一开头就 `EmitDeferred()`）。

`EmitDeferred` 的「到不动点」循环逻辑（注释明确说明「iterate until no changes are made」）：[clang/lib/CodeGen/CodeGenModule.cpp:L4013-L4046](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/CodeGen/CodeGenModule.cpp#L4013-L4046)（先把 `DeferredDeclsToEmit` `swap` 出来再逐个 `EmitGlobalDefinition`，新产生的延迟项进入新队列，由外层循环再次处理）。

> **数学/算法注记**：延迟发射本质上是一个**工作表（worklist）到不动点**的算法。把「需要发射的声明集合」看作不断扩张的近似：每发射一个声明 $d$，可能解锁一批新声明 $N(d)$ 加入待发射集。设 $S_0$ 为初始必须发射的集合，迭代规则为 $S_{k+1} = S_k \cup \bigcup_{d\in (S_k\setminus S_{k-1})} N(d)$。当 $S_{k+1}=S_k$ 时到达不动点 $S^*$，即所有「可达且必须发射」的声明都被覆盖。由于声明集合有限且单调增长，该过程必然终止。

#### 4.2.4 代码实践

**实践目标**：通过对比「用到 / 没用到」的 `static` 函数，亲眼看到延迟发射的效果。

1. 写 `defer.c`：
   ```c
   static int helper(int x) { return x * 2; }   // 定义了，但没人调用
   static int used_helper(int x) { return x + 1; }
   int entry(int n) { return used_helper(n); }   // 只用了 used_helper
   ```
2. 分别生成 IR：
   ```bash
   clang -S -emit-llvm defer.c -o defer.ll
   ```
3. 打开 `defer.ll` 查看 `define` 出来的函数。

**需要观察的现象**：`@entry` 和 `@used_helper` 会有函数体（`define ... @used_helper(...)`）；而 `@helper`（从未被引用的 `static` 函数）**不会**出现在最终 IR 中——它虽然「定义了」，但在 `EmitGlobal` 被判为可延迟、且从未被引用，于是 `EmitDeferred` 里没有它。

**预期结果**：`defer.ll` 里能搜到 `define.*@entry` 与 `define.*@used_helper`，但搜不到 `@helper` 的定义。这正是 `MustBeEmitted`/延迟发射机制的结果：未被引用的内部链接函数不会进入 `DeferredDeclsToEmit`。（注意：`-O0` 下行为如此；若开启优化 `-O1` 以上，还会有死代码消除等后端 pass 进一步影响，本实践请用 `-O0`。）

#### 4.2.5 小练习与答案

**练习 1**：为什么 `EmitGlobal` 不直接翻译所有声明，而要搞一套延迟机制？
> **答案**：C/C++ 有大量「定义了未必用到」的声明（`static` 函数、内联函数、模板实例化）。立即翻译会生成大量无用代码、拖慢编译并增大产物。延迟机制让「真正发射」推迟到确认被引用或必须发射时，TU 末尾再用不动点循环补齐。

**练习 2**：`EmitGlobalFunctionDefinition` 里这一句 `CodeGenFunction(*this).GenerateCode(GD, Fn, FI)` 为什么是「临时构造」？这反映了 `CodeGenModule` 与 `CodeGenFunction` 怎样的寿命关系？
> **答案**：这是一个未绑定的临时对象，`GenerateCode` 返回后立即析构。它说明 `CodeGenFunction` 是「每个函数体一个」的短命对象，而 `CodeGenModule` 是贯穿整个 TU 的长寿对象——后者持有前者需要的模块级状态（`*this`）。

**练习 3**：`DeferredDecls`、`DeferredDeclsToEmit`、`EmittedDeferredDecls` 三张表分别存什么？
> **答案**：`DeferredDecls`：已见到、按 mangled 名字索引、尚未确定要发射的声明；`DeferredDeclsToEmit`：已确定要发射、待 `EmitDeferred` 处理的队列；`EmittedDeferredDecls`：已经发射完成的记录（用于增量编译等场景的去重/重发射判断）。

### 4.3 函数级 CodeGen：CodeGenFunction 与 AST→IR 映射

#### 4.3.1 概念说明

`CodeGenFunction`（常简称 **CGF**）是「单个函数体」的翻译器，注释说它组织「生成 LLVM 代码时所用的、**逐函数**状态」。每翻译一个函数，`CodeGenModule` 就 `new` 一个 `CGF`，它自带三样法宝：

- **`CodeGenModule &CGM`**：反指模块级总管，需要类型映射、mangling、Runtime 时都通过它。
- **`CGBuilderTy Builder`**：**这就是 IRBuilder**（u3-l4）！所有 `alloca`/`load`/`store`/`add`/`br`/`ret` 都由它产出，并自动插入到「当前基本块」的当前插入点。
- **`llvm::Function *CurFn`**：当前正在生成的那个 `llvm::Function`。

所以函数体翻译的心智模型极其简洁：**遍历函数体的 AST，对每条 `Stmt`/`Expr` 调用对应的 `EmitXxx`，里面用 `Builder.CreateXxx` 造 IR 指令**。表达式产出 `RValue`/`LValue`（CodeGen 对「值」的抽象），语句则主要产生副作用（改变控制流、写内存）。

#### 4.3.2 核心流程

函数体生成从 `GenerateCode` 开始，分四步：

```
GenerateCode(GD, Fn, FnInfo)
  1. BuildFunctionArgList(GD, Args)        ── 收集形参（含 this、隐式参数）
  2. StartFunction(GD, ResTy, Fn, ...)      ── 函数 prologue：
        ├─ 在 Fn 里建 entry 基本块，Builder.SetInsertPoint 指向它
        ├─ 给每个形参 CreateParamAlloca 并 store 形参值（-O0）
        └─ 设置调试信息、PGO 计数器
  3. EmitFunctionBody(Body)                 ── 发射函数体
        └─ 对 Body（CompoundStmt）逐条 EmitStmt
  4. FinishFunction()                        ── epilogue：补 return、写 lifetime.end 等
```

其中 `EmitStmt` 是 AST→IR 翻译的**总入口**，它是一个按 `Stmt::StmtClass` 分发的大 `switch`：

- 控制流语句各有专门方法：`EmitIfStmt`、`EmitForStmt`、`EmitWhileStmt`、`EmitReturnStmt`、`EmitSwitchStmt`……它们会用 `Builder.CreateBr`/`CreateCondBr` 造基本块和跳转，从而把 C 的控制流翻译成 IR 的基本块图。
- **表达式**走统一入口：`switch` 里用一个宏把所有 `EXPR` 类别归到同一段，调 `EmitIgnoredExpr(cast<Expr>(S))`（表达式作为语句时，其结果被丢弃）。`EmitIgnoredExpr` 再按表达式的具体类型（如 `BinaryOperator`、`CallExpr`、`DeclRefExpr`）分派到具体的 `EmitXxxExpr`，最终都是 `Builder.CreateXxx` 造指令。
- **声明语句（`DeclStmt`）**在 `EmitSimpleStmt` 阶段就处理了，它会对其中每个 `VarDecl` 调 `EmitDecl` → `EmitVarDecl` →（局部变量）`EmitAutoVarDecl`，后者用 `Builder.CreateAlloca` 申请栈空间、再用 `store` 写入初值。

局部变量是连接 4.2 与本节的好例子：一个 `int sum = a + b;` 会被拆成「`alloca`（声明本身）+ 求值右边的表达式 `a+b` 得到一个 `Value` + `store` 把它写进 alloca 的地址」。这正是 `-O0` 下你会在 IR 里看到大量 `alloca`/`load`/`store` 的原因（优化 pass 之后才会消除它们，那是 u4 的事）。

#### 4.3.3 源码精读

先确认 `CodeGenFunction` 用的就是 IRBuilder。`CGBuilderTy` 的定义：[clang/lib/CodeGen/CGBuilder.h:L49-L57](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/CodeGen/CGBuilder.h#L49-L57)（`CGBuilderBaseTy = IRBuilder<TargetFolder, CGBuilderInserterTy>`，`CGBuilderTy` 继承它——正是 u3-l4 的 `IRBuilder`）；其自定义插入器会把每条指令转给 `CodeGenFunction::InsertHelper` 贴上调试/位置元数据：[clang/lib/CodeGen/CGBuilder.h:L32-L45](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/CodeGen/CGBuilder.h#L32-L45)。

`CodeGenFunction` 类的声明与三个关键成员：[clang/lib/CodeGen/CodeGenFunction.h:L256-L298](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/CodeGen/CodeGenFunction.h#L256-L298)（注释「per-function state」，成员 `CodeGenModule &CGM`、`CGBuilderTy Builder`）：当前函数指针在：[clang/lib/CodeGen/CodeGenFunction.h:L356](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/CodeGen/CodeGenFunction.h#L356)（`llvm::Function *CurFn = nullptr;`）。

`GenerateCode` 的开头——收集形参并做 ABI 检查：[clang/lib/CodeGen/CodeGenFunction.cpp:L1470-L1477](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/CodeGen/CodeGenFunction.cpp#L1470-L1477)（`BuildFunctionArgList` 拿到形参列表 `Args` 与返回类型 `ResTy`）。

`GenerateCode` 中段——发射 prologue、再发射函数体。`StartFunction` 建 entry 块与形参 alloca：[clang/lib/CodeGen/CodeGenFunction.cpp:L1565-L1566](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/CodeGen/CodeGenFunction.cpp#L1565-L1566)（`StartFunction(...)` 完成标准 prologue）；随后按函数种类分发，普通函数走：[clang/lib/CodeGen/CodeGenFunction.cpp:L1632-L1633](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/CodeGen/CodeGenFunction.cpp#L1632-L1633)（`else if (Body) EmitFunctionBody(Body);`）。

`EmitFunctionBody`：函数体若是 `CompoundStmt`（大括号块）就 `EmitCompoundStmtWithoutScope`，否则直接 `EmitStmt`：[clang/lib/CodeGen/CodeGenFunction.cpp:L1380-L1387](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/CodeGen/CodeGenFunction.cpp#L1380-L1387)。

**`EmitStmt` 是本节最重要的函数**——AST `Stmt` → IR 的总分发器：[clang/lib/CodeGen/CGStmt.cpp:L58-L97](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/CodeGen/CGStmt.cpp#L58-L97)（先 `EmitSimpleStmt` 处理简单语句，再进入 `switch (S->getStmtClass())`）。

`EmitStmt` 大 `switch` 里把**所有表达式类**统一归到一段、调 `EmitIgnoredExpr`：[clang/lib/CodeGen/CGStmt.cpp:L120-L131](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/CodeGen/CGStmt.cpp#L120-L131)（`EXPR` 宏展开覆盖所有表达式类别，统一走 `EmitIgnoredExpr(cast<Expr>(S))`）。

控制流语句则各自有专门方法，例如 `ReturnStmt`：[clang/lib/CodeGen/CGStmt.cpp:L157-L162](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/CodeGen/CGStmt.cpp#L157-L162)（`IfStmt`/`WhileStmt`/`ForStmt`/`ReturnStmt`/`SwitchStmt` 各调 `EmitXxxStmt`）。

声明侧：`EmitDecl` 也是一个按 `Decl::Kind` 分发的大 `switch`：[clang/lib/CodeGen/CGDecl.cpp:L52-L53](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/CodeGen/CGDecl.cpp#L52-L53)（`switch (D.getKind())`）。

`VarDecl` 走 `EmitVarDecl`，按存储期分流——静态存储走 `EmitStaticVarDecl`，自动存储（普通局部变量）走 `EmitAutoVarDecl`：[clang/lib/CodeGen/CGDecl.cpp:L211-L239](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/CodeGen/CGDecl.cpp#L211-L239)（`getStorageDuration() != SD_Automatic` → `EmitStaticVarDecl`，否则 `EmitAutoVarDecl(D)`）。

普通局部变量三步走：`EmitAutoVarAlloca`（alloca）→ `EmitAutoVarInit`（写初值）→ `EmitAutoVarCleanups`（析构等）：[clang/lib/CodeGen/CGDecl.cpp:L1356-L1360](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/CodeGen/CGDecl.cpp#L1356-L1360)。

`EmitAutoVarAlloca` 负责为局部变量分配栈空间（`alloca`），这是 `-O0` 下 IR 里满天飞的 `%x = alloca i32` 的来源：[clang/lib/CodeGen/CGDecl.cpp:L1490-L1496](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/CodeGen/CGDecl.cpp#L1490-L1496)（取出类型、对齐，准备分配地址）。

#### 4.3.4 代码实践

**实践目标**：对一段含函数与局部变量的 C 代码，用 `clang -S -emit-llvm` 拿到 IR，并把**每一条 IR 反推回它的 AST 节点与对应的 `EmitXxx` 源码位置**。

1. 写 `add.c`：
   ```c
   int add(int a, int b) {
     int sum = a + b;
     return sum;
   }
   ```
2. 生成 `-O0` IR：
   ```bash
   clang -O0 -S -emit-llvm add.c -o add.ll
   ```
3. 同时看一下 AST（对照用）：
   ```bash
   clang -Xclang -ast-dump -fsyntax-only add.c
   ```

**预期 IR（`-O0`，不同版本的寄存器编号与属性会略有差异，编号以本地为准——待本地验证）**：

```llvm
define dso_local i32 @add(i32 noundef %0, i32 noundef %1) #0 {
entry:
  %a.addr = alloca i32, align 4        ; ← EmitAutoVarAlloca：为形参 a 建地址（prologue）
  %b.addr = alloca i32, align 4        ; ← EmitAutoVarAlloca：为形参 b 建地址
  %sum = alloca i32, align 4           ; ← EmitAutoVarAlloca：局部变量 sum 的 alloca
  store i32 %0, ptr %a.addr, align 4   ; ← StartFunction：把形参 %0 存入 a.addr
  store i32 %1, ptr %b.addr, align 4   ; ← StartFunction：把形参 %1 存入 b.addr
  %2 = load i32, ptr %a.addr, align 4  ; ← BinaryOperator 的左操作数 DeclRefExpr(a)
  %3 = load i32, ptr %b.addr, align 4  ; ← BinaryOperator 的右操作数 DeclRefExpr(b)
  %4 = add nsw i32 %2, %3              ; ← BinaryOperator(+)  →  Builder.CreateAdd
  store i32 %4, ptr %sum, align 4      ; ← EmitAutoVarInit：把 a+b 存进 sum
  %5 = load i32, ptr %sum, align 4     ; ← ReturnStmt 里的 DeclRefExpr(sum)
  ret i32 %5                           ; ← EmitReturnStmt  →  Builder.CreateRet
}
```

**需要做的对照（把 IR 反推回 AST 与源码）**：

| IR 指令 | 对应 AST 节点 | 触发的 CodeGen 入口（本讲源码） |
|---------|--------------|--------------------------------|
| `define ... @add`、`entry:`、`%a.addr/%b.addr = alloca`、`store 形参` | `FunctionDecl` 与 `ParmVarDecl` | `EmitGlobalFunctionDefinition`→`GenerateCode`→`StartFunction`（[CodeGenFunction.cpp:L1565-L1566](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/CodeGen/CodeGenFunction.cpp#L1565-L1566)） |
| `%sum = alloca i32` | `DeclStmt` 里的 `VarDecl sum` | `EmitStmt`→`EmitDecl`→`EmitVarDecl`→`EmitAutoVarDecl`→`EmitAutoVarAlloca`（[CGDecl.cpp:L1356-L1360](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/CodeGen/CGDecl.cpp#L1356-L1360)） |
| `%2=load`、`%3=load`、`%4=add` | `BinaryOperator(+)`，左右是 `DeclRefExpr` | `EmitStmt`→`EmitIgnoredExpr`/求值表达式→`Builder.CreateLoad`×2、`Builder.CreateAdd`（[CGStmt.cpp:L120-L131](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/CodeGen/CGStmt.cpp#L120-L131)） |
| `store i32 %4, ptr %sum` | `VarDecl sum` 的初始化部分 | `EmitAutoVarInit`（[CGDecl.cpp:L1356-L1360](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/CodeGen/CGDecl.cpp#L1356-L1360)） |
| `%5=load`、`ret i32 %5` | `ReturnStmt`，内含 `DeclRefExpr(sum)` | `EmitStmt`→`EmitReturnStmt`→`Builder.CreateRet`（[CGStmt.cpp:L162](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/CodeGen/CGStmt.cpp#L162)） |

**需要观察的现象**：`-O0` 下 IR「啰嗦」——所有局部变量都走 `alloca`+`load`+`store`，即便 `a+b` 本可只算一次。这正是 CodeGen 朴素直译 AST 的结果；消除这些冗余是 u4 优化 pass（如 `mem2reg`、`instcombine`）的工作。可以用 `clang -O1 -S -emit-llvm add.c -o add.opt.ll` 对比，会看到 `%sum` 的 alloca/load/store 全部消失，函数体塌缩成一行 `ret i32 %0 + %1` 风格的代码。

#### 4.3.5 小练习与答案

**练习 1**：`-O0` 下，为什么访问一个局部变量 `sum` 会出现一对 `load`/`store`，而不是直接用寄存器？
> **答案**：因为 CodeGen 把每个局部变量实现在栈上（`alloca`），对它的读写都通过内存（`store` 写、`load` 读）。`-O0` 不做「提升到寄存器」的优化。把 `alloca` 提升为 SSA 寄存器是后续 `mem2reg` pass 的事（u4）。

**练习 2**：`EmitStmt` 的大 `switch` 里，所有 `Expr` 子类为什么能被同一段代码（调 `EmitIgnoredExpr`）统一处理？
> **答案**：因为「表达式作为语句出现」时，其结果值总是被丢弃（例如 `a + b;` 这条语句算了白算）。所以无需对每种表达式单独写 `case`，统一调 `EmitIgnoredExpr` 即可；表达式内部的具体求值（加法、调用、取地址）由表达式层自己按类型分派。

**练习 3**：`CodeGenFunction` 里的 `Builder` 和 u3-l4 讲的 `IRBuilder` 是什么关系？
> **答案**：`Builder` 的类型 `CGBuilderTy` 正是 `IRBuilder<TargetFolder, CGBuilderInserterTy>` 的子类（见 `CGBuilder.h`）。也就是说 CodeGen 造指令用的就是 u3-l4 的同一套 `CreateAdd`/`CreateLoad`/`CreateStore` API，只是多夹带了一个会自动贴调试信息的自定义插入器，并且由 Clang 的 AST 来驱动对它的调用。

## 5. 综合实践

把本讲三节串起来，完成一次「从命令行到 IR 指令」的完整追踪。

**任务**：对下面这段含控制流的 C 代码，走完「触发 → 模块级派发 → 函数级映射」的全程解释。

```c
// clamp.c
int clamp(int x, int lo, int hi) {
  int r = x;
  if (r < lo) r = lo;   // 分支语句
  return r;
}
```

**操作步骤**：

1. **触发层（对应 4.1）**：运行 `clang -### -S -emit-llvm clamp.c`，确认 cc1 带 `-emit-llvm`，对应 `EmitLLVMAction` → `BackendConsumer` → `CodeGeneratorImpl` → `CodeGenModule`。
2. **模块级（对应 4.2）**：运行 `clang -O0 -S -emit-llvm clamp.c -o clamp.ll`。在 `clamp.ll` 里定位 `define dso_local i32 @clamp(...)`——这一行的存在，说明 `EmitTopLevelDecl` 把 `FunctionDecl clamp` 派发给了 `EmitGlobal`，又因 `clamp` 有外部链接定义（`MustBeEmitted` 为真）而进入 `EmitGlobalFunctionDefinition`，进而 `CodeGenFunction(*this).GenerateCode(...)`。
3. **函数级（对应 4.3）**：阅读 `clamp.ll` 的函数体，找出三类产物并各举一例：
   - `alloca`/`store`/`load`（来自 `StartFunction` 的形参处理与 `int r = x;` 的 `EmitAutoVarDecl`）；
   - `icmp` + `br`（来自 `if (r < lo)` 的 `EmitIfStmt`，它用 `Builder.CreateICmpSLT` 造比较、`CreateCondBr` 造条件跳转，从而产生多个基本块）；
   - `ret`（来自 `return r;` 的 `EmitReturnStmt`）。
4. **对照 AST**：`clang -Xclang -ast-dump -fsyntax-only clamp.c`，把 IR 里的基本块结构与 AST 里的 `IfStmt`（含 `cond`/`then`/`else`）一一对应。

**预期结果与现象**：

- `clamp.ll` 里 `@clamp` 会有**多个基本块**（如 `entry`、`if.then`、`if.end`），块之间用 `br`/`br i1` 连接——这是 `EmitIfStmt` 用 `Builder` 造出的控制流图。
- `int r = x;` 表现为一次 `alloca`（r）+ 一次 `store`（把形参 x 存进去）。
- `if (r < lo)` 表现为一次 `load`（读 r）、一次 `icmp slt`、一次 `condbr`。
- 用 `clang -O1 -S -emit-llvm clamp.c -o clamp.opt.ll` 对比：分支可能被简化、`alloca` 被消除，体会「CodeGen 朴素直译 vs 后端优化」的分工。

**如果无法运行**：以上 IR 细节（基本块命名、寄存器编号）标注为「待本地验证」；但「`if` 产生 `icmp`+`br` 与多个基本块、局部变量产生 `alloca`」这些结构性结论由 `EmitIfStmt`/`EmitAutoVarDecl` 的源码逻辑保证，与具体版本无关。

## 6. 本讲小结

- **触发与桥接**：CodeGen 是一个 `CodeGenAction`（`ASTFrontendAction` 子类）。`ExecuteAction` 驱动 `ParseAST`，`CreateASTConsumer` 造出 `BackendConsumer`；`BackendConsumer` 把 `HandleTopLevelDecl`/`HandleTranslationUnit` 转发给 `CodeGenerator`，并在 TU 末尾用 `emitBackendOutput` 把 `Module` 交给后端。`CodeGeneratorImpl` 同时拥有 `llvm::Module` 与 `CodeGenModule`，是这两者之间的桥。
- **模块级（`CodeGenModule`）**：管跨函数状态（`Module`/`ASTContext`/类型映射/mangling/Runtime/虚表）。`EmitTopLevelDecl` 按 `Decl::Kind` 派发，函数与变量走 `EmitGlobal`；`EmitGlobal` 用 `MustBeEmitted`/`MayBeEmittedEagerly` 决定立即发射或延迟；`EmitGlobalFunctionDefinition` 临时构造 `CodeGenFunction` 并 `GenerateCode`；TU 末尾 `Release`→`EmitDeferred` 用工作表到不动点补齐所有延迟声明。
- **函数级（`CodeGenFunction`）**：管逐函数状态，核心是 `CGBuilderTy Builder`（就是 u3-l4 的 `IRBuilder`）与 `CurFn`。`GenerateCode` → `StartFunction`（prologue）→ `EmitFunctionBody` → 逐条 `EmitStmt`。
- **AST→IR 映射模型**：`EmitStmt` 是按 `Stmt::StmtClass` 的大 `switch`——控制流各有 `EmitXxxStmt`（用 `Builder` 造基本块与跳转），表达式统一走 `EmitIgnoredExpr`，声明经 `EmitDecl`→`EmitVarDecl`→`EmitAutoVarDecl`（`alloca`+`store`）。每条 IR 都能反推回某个 AST 节点。
- **与前后讲的衔接**：上游承接 u5-l1（`FrontendAction`/`ASTConsumer`）、u5-l3/u5-l4（AST 与校验后的产物）；下游产出的 `llvm::Module` 正是 u3（IR 数据结构）、u4（Pass 优化）、u6（后端代码生成）的输入；造指令手法直接复用 u3-l4 的 `IRBuilder`。

## 7. 下一步学习建议

- **顺着「函数体」继续深入表达式层**：本讲只点到 `EmitIgnoredExpr`。真正求值表达式的是 `CGExpr.cpp`（`EmitExpr` 分发）、`CGExprScalar.cpp`（标量表达式的 `EmitBinOp`/`EmitUnaryOp`）、`CGExprAgg.cpp`（聚合返回值）、`CGExprCXX.cpp`（C++ 表达式）。挑一个 `BinaryOperator`，从 `EmitStmt` 一路追到 `Builder.CreateAdd`，能把 4.3 节彻底吃透。
- **看一个完整的调用如何发射**：函数调用 `CallExpr` 是 CodeGen 最复杂的部分之一，入口在 `CGCall.cpp`（`EmitCall`/`EmitCallArgs`），涉及 `CGFunctionInfo` 与 ABI 约定，和 4.2 节的 `arrangeGlobalDeclaration` 呼应。
- **进入后端（u6）**：本讲停在「完整的 `llvm::Module`」。u6-l1 会讲这个 `Module` 如何进入后端流水线（指令选择、寄存器分配、MC 发射），与 `emitBackendOutput` 对接。
- **对比官方文档**：`clang/docs/InternalsManual.rst` 的「CodeGen」一节与 `clang/docs/ItaniumMangleAbiImages.rst`（mangling）是权威补充，可对照本讲的结构性结论。
- **动手扩展**：在掌握 4.3 后，可尝试用 libClang 或直接写一个最简 `FrontendAction`（参考 `EmitLLVMOnlyAction`），自己拿到 `takeModule()` 返回的 `Module` 并用 `Module::print` 打印——这是把「命令行 clang」变成「可编程地生成 IR」的关键一步，也为将来写自定义分析/插桩工具打基础。
