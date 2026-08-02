# 语义分析 Sema

## 1. 本讲目标

上一讲（u5-l3）我们看到，Parser 把 Token 流组织成了语法正确的 AST「半成品」，并在每解析出一个结构时调用形如 `Actions.ActOnXxx(...)` 的方法把半成品递交给 Sema。本讲要回答的核心问题是：**Sema（语义动作对象）拿到这些半成品之后，到底做了哪些事？它如何判断「这样写对不对」，又如何把错误报告给用户？**

学完本讲，你应当能够：

- 说清 `Sema` 在 Clang 中的角色定位——它是 Parser 的「语义动作」接口，也是 AST 节点的真正构造者与所有语义检查（名字查找、类型检查、重载、模板）的执行者。
- 理解**名字查找（name lookup）**如何沿作用域链向上搜索、用 `LookupResult` 汇报结果。
- 理解**类型检查（type checking）**在表达式与声明这两条主线上的入口（`ActOnBinOp`→`BuildBinOp`→`CreateBuiltinBinOp`，以及 `HandleDeclarator`），并知道 Sema 如何用 `Diag` 报错。
- 掌握 C++ **重载决议（overload resolution）**的「候选—隐式转换序列—择优」三步流程，以及它在源码中的对应位置。
- 认识**模板实例化（template instantiation）**的「用到才实例化 + 延迟队列」机制，理解 `PerformPendingInstantiations` 如何在翻译单元末尾排空待实例化队列。
- 会用 `clang -fsyntax-only` 真实地触发 Sema 的诊断，并对照源码定位报错的位置。

## 2. 前置知识

在进入源码前，先用通俗语言建立四个直觉。

**第一，「语法」与「语义」的分工（承接 u5-l3）。** Parser 只回答「这串 Token 合不合文法」（例如 `int a = b + ;` 少了右操作数，语法就错）。但还有一类问题文法管不了：`int a = "hello" + 3;` 语法上完全合法（一个表达式加一个整数），可「字符串加整数」在类型层面站不住。这类「写得出来但含义不对」的问题，全部交给 Sema。可以把 Parser 想成「语法裁判」，Sema 是「语义裁判 + 真正动手造 AST 节点的人」。

**第二，什么是「名字查找」。** 源码里每一个标识符（变量名、函数名、类型名）都要回答一个问题：「这个名字指的是哪个声明？」C/C++ 有作用域规则：内层名字会**遮蔽（shadow）**外层同名名字。Sema 的名字查找就是从当前作用域开始，沿作用域链一层层向外找，命中第一个可见的声明即停。例如函数体内的 `x` 通常指最近的局部变量 `x`，而不是全局的 `x`。

**第三，什么是「重载决议」。** C++ 允许同名函数有多个版本（重载），如 `print(int)`、`print(double)`。调用 `print(x)` 时，编译器要决定「该用哪一个」。规则是：先收集一批**候选函数**，再为每个候选算一条「把实参转成形参类型」的**隐式转换序列（Implicit Conversion Sequence, ICS）**，最后按一套严格的择优规则挑出「最佳」者；如果挑不出唯一最佳者，就报「歧义（ambiguous）」错误。

**第四，什么是「模板实例化」。** 模板（`template <typename T> ...`）写的是「代码的模板」，本身还不是可编译的代码。只有当你用具体类型（如 `T=int`）去用它时，编译器才「照着模板把 `T` 替换成 `int`」生成一份真实代码，这叫实例化。难点在于：实例化可能引发连锁反应（实例化一个函数会触发它调用的另一个模板），所以 Clang 采用「**用到时记录、稍后批量执行**」的延迟策略。

> 关键术语：Sema（语义动作）、SemaBase（Sema 与各特性模块的共同基类）、名字查找（Lookup）、`LookupResult`、作用域（Scope）、遮蔽（shadow）、类型检查（Type Checking）、`Diag`（诊断发射）、重载决议（Overload Resolution）、候选函数（Candidate）、隐式转换序列（ICS）、模板实例化（Template Instantiation）、翻译单元（Translation Unit, TU）。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `clang/include/clang/Sema/Sema.h` | `Sema` 类声明——Clang 中体量最大的头文件之一，集中声明了几乎所有语义检查方法。 |
| `clang/include/clang/Sema/SemaBase.h` | `SemaBase` 类：`Sema` 与各 `SemaXxx` 特性模块的共同基类，提供 `Diag`、`getASTContext` 等公共工具与诊断构造器。 |
| `clang/lib/Sema/Sema.cpp` | `Sema` 的构造、`Initialize()`（建立内建类型等）与析构。 |
| `clang/lib/Sema/SemaBase.cpp` | `SemaBase::Diag` 的实现——所有诊断信息的统一出口。 |
| `clang/lib/Sema/SemaLookup.cpp` | 名字查找实现，核心是 `Sema::LookupName`。 |
| `clang/include/clang/Sema/Lookup.h` | `LookupResult` 与 `LookupResultKind` 枚举。 |
| `clang/lib/Sema/SemaDecl.cpp` | 声明的语义处理，入口 `ActOnDeclarator`/`HandleDeclarator`。 |
| `clang/lib/Sema/SemaExpr.cpp` | 表达式的语义处理与类型检查，含 `ActOnBinOp`/`BuildBinOp`/`CreateBuiltinBinOp`。 |
| `clang/lib/Sema/SemaOverload.cpp` | 重载决议的全部实现：收集候选、计算 ICS、择优。 |
| `clang/include/clang/Sema/Overload.h` | `OverloadingResult` 枚举、`ImplicitConversionSequence`、`OverloadCandidateSet`。 |
| `clang/lib/Sema/SemaTemplateInstantiateDecl.cpp` | 模板实例化核心，`InstantiateFunctionDefinition`、`PerformPendingInstantiations`。 |
| `clang/lib/Sema/SemaTemplateInstantiate.cpp` | 类型/表达式的模板代换（`TreeTransform` 子类 `TemplateInstantiator`）。 |
| `clang/include/clang/Sema/Template.h` | `TemplateDeclInstantiator`（基于 `DeclVisitor` 的访问者，负责实例化各种声明）。 |
| `clang/include/clang/AST/ASTContext.h` | `ASTContext`：所有 AST 节点与类型的内存竞技场，并持有翻译单元根。 |

> 说明：本讲引用的 `SemaLookup.cpp`、`SemaOverload.cpp`、`SemaTemplate*.cpp`、`Template.h` 等文件未出现在本讲规格的「关键源码」清单里，但它们是 Sema 各机制的真正实现所在（名字查找、重载、模板分别被拆到了独立的 `.cpp`），为确保行号与链接准确，本讲按真实代码组织补充引用。

## 4. 核心概念与源码讲解

### 4.1 Sema 的角色：Parser 的语义动作接口与诊断出口

#### 4.1.1 概念说明

`Sema` 是 Clang 语义分析阶段的「大脑」。回顾 u5-l3：Parser 每解析出一个语法结构，就回调 `Actions.ActOnXxx(...)`。这里的 `Actions` 就是 `Sema&`。所以可以把 Sema 看成两顶帽子合一：

1. **AST 节点的工厂**：Parser 只递过来「半成品信息」（Token、位置、语法类别），真正 `new` 出 `VarDecl`、`BinaryOperator` 等节点并挂到树上的，是 Sema。
2. **语义裁判**：在构造节点的同时，Sema 顺带做名字查找、类型检查、访问控制、模板处理等，发现问题就用 `Diag` 报错。

因此你会看到 Sema 的方法命名有两条惯例：

- `ActOnXxx`：Parser 在「还没拿到完整语义」时调用的动作钩子（如 `ActOnDeclarator`、`ActOnBinOp`），通常只做轻量分发。
- `BuildXxx` / `CreateXxx` / `CheckXxx`：真正干活的内部函数（如 `BuildBinOp`、`CreateBuiltinBinOp`、`CheckAssignmentOperands`），命名暗示「真正构造 / 检查」。

Clang 还把这些海量方法按主题拆到几十个 `.cpp` 文件里（`SemaDecl.cpp`、`SemaExpr.cpp`、`SemaOverload.cpp`、`SemaTemplate*.cpp`、`SemaCUDA.cpp` ……），但它们扩展的是**同一个 `Sema` 类**——这正是 `Sema.h` 体量巨大（上万行）的原因。较新的版本把公共工具抽到 `SemaBase`，让 `Sema` 和若干 `SemaXxx`（如 `SemaOpenACC`）都继承它。

所有 AST 节点并不用全局 `new` 分配，而是统一从 `ASTContext` 的内存竞技场（bump pointer allocator）里分配，编译完一个 TU 一次性释放；`ASTContext` 同时持有翻译单元根 `TranslationUnitDecl`。

#### 4.1.2 核心流程

```text
ParseAST
  └── new Sema(PP, Ctx, Consumer)        // 见 u5-l3：在 ParseAST 里构造 Sema
        ├── Sema 构造(Preprocessor, ASTContext, ASTConsumer)
        │     └── Initialize()           // 建立内建类型表、预定义声明等
        ├── 解析过程中被 Parser 反复回调：
        │     Actions.ActOnDeclarator(...)  ──►  HandleDeclarator(...)  ──► new VarDecl(...)
        │     Actions.ActOnBinOp(...)       ──►  BuildBinOp(...)         ──► CreateBuiltinBinOp(...) ──► new BinaryOperator(...)
        │     遇到错误：Diag(Loc, diag::err_xxx) << ...     // 经 SemaBase::Diag 统一出口
        └── 节点全部从 ASTContext 的 BumpAlloc 竞技场分配，挂到 TranslationUnitDecl 之下
```

要点：`ActOn` 是薄壳入口，真正的构造与检查下沉到 `Build`/`Create`/`Check`；诊断一律走 `Diag`；节点归宿是 `ASTContext`。

#### 4.1.3 源码精读

`Sema` 类的声明揭示了它的定位与基类：

[clang/include/clang/Sema/Sema.h:867-869](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/include/clang/Sema/Sema.h#L867-L869) —— 注释写明「This implements semantic analysis and AST building for C」，类声明 `class Sema final : public SemaBase`。紧跟其后的「Table of Contents」注释（873 行起）逐一列出了 `Sema.cpp`、`SemaAPINotes.cpp`、`SemaAccess.cpp`、`SemaAttr.cpp`……印证「一个类、多文件」的组织方式。

公共基类 `SemaBase` 提供共享工具与诊断构造器：

[clang/include/clang/Sema/SemaBase.h:36-45](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/include/clang/Sema/SemaBase.h#L36-L45) —— `class SemaBase` 持有 `Sema &SemaRef`，并暴露 `getASTContext()`、`getDiagnostics()`、`getLangOpts()`、`getCurContext()` 等公共方法。这样 `Sema` 及各 `SemaXxx` 特性模块都能复用同一套上下文访问与诊断发射能力。

`Sema` 的构造与初始化：

[clang/lib/Sema/Sema.cpp:277-278](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Sema/Sema.cpp#L277-L278) —— 构造函数接收 `Preprocessor &`、`ASTContext &`、`ASTConsumer &` 三大件，把词法、AST 内存、下游消费者串到一起。

[clang/lib/Sema/Sema.cpp:376-377](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Sema/Sema.cpp#L376-L377) —— `void Sema::Initialize()` 在构造之后被调用，负责建立内建类型（`int`、`double` 等）、预定义声明与上下文，是后续一切查找与检查的前提。

诊断的统一出口在 `SemaBase::Diag`：

[clang/lib/Sema/SemaBase.cpp:61-64](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Sema/SemaBase.cpp#L61-L64) —— `SemaBase::Diag(SourceLocation Loc, unsigned DiagID)` 接收「源码位置 + 诊断 ID」，内部判断该不该立即报（CUDA 等场景会延迟诊断），最终返回一个「诊断构造器」对象。典型的调用形如 `Diag(Loc, diag::err_xxx) << 实参 << 范围;`——用 `<<` 往里塞参数与高亮范围，构造器析构时真正发射。这就是 Sema 所有错误/警告信息的共同出口。

AST 节点的归宿——内存竞技场：

[clang/include/clang/AST/ASTContext.h:223](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/include/clang/AST/ASTContext.h#L223) —— `class ASTContext`。注释（承接上方）说明它「持有语义分析期间长期存在的 AST 节点（类型、声明）」。

[clang/include/clang/AST/ASTContext.h:782](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/include/clang/AST/ASTContext.h#L782) —— 成员 `mutable llvm::BumpPtrAllocator BumpAlloc;`，这就是所有节点批量分配、整体回收的「竞技场」（u5-l3 已述其动机）。

[clang/include/clang/AST/ASTContext.h:1303](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/include/clang/AST/ASTContext.h#L1303) —— `getTranslationUnitDecl()` 返回整个翻译单元的 AST 根，Sema 构造的所有顶层声明最终都挂在这里。

#### 4.1.4 代码实践

**实践目标**：在源码层面确认「Parser 回调 Sema 的 `ActOn` 方法、`ActOn` 转发到 `Build`/`Create`、出错走 `Diag`」这条主线。

**操作步骤**：

1. 打开 `clang/include/clang/Sema/Sema.h` 第 869 行，确认 `class Sema final : public SemaBase`，并浏览其后「Table of Contents」注释，体会 Sema 方法被拆到多少个 `.cpp`。
2. 打开 `clang/include/clang/Sema/SemaBase.h` 第 36 行，确认 `SemaBase` 提供 `getASTContext()`、`Diag` 等公共工具。
3. 在 `clang/lib/Sema/SemaBase.cpp` 第 61 行阅读 `Diag` 实现，理解「位置 + DiagID → 构造器」的统一模式。

**需要观察的现象**：`Diag` 内部会根据语言选项决定立即发射还是延迟（CUDA 的设备/主机诊断分流），但对外都是同一个 `Diag(Loc, diag::xxx) << ...` 接口。

**预期结果**：你能用自己的话说清「Sema 既是 AST 工厂又是语义裁判，所有诊断都从 `SemaBase::Diag` 这一个口子出去，节点都进 `ASTContext` 竞技场」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Sema.h` 会有上万行、却分属几十个 `.cpp` 文件？

> **参考答案**：因为 Sema 把「声明、表达式、语句、重载、模板、CUDA、HLSL、ObjC ……」等几乎全部语义检查方法都集中声明在**同一个 `Sema` 类**里（这些方法彼此共享大量内部状态，所以没有进一步拆成多个类），于是头文件极长；但实现上按主题拆到多个 `.cpp` 以便分模块维护与并行编译。最近的版本开始把一部分抽出为继承 `SemaBase` 的 `SemaXxx`，但主干仍集中在 `Sema`。

**练习 2**：`ActOnXxx` 和 `BuildXxx`/`CreateXxx` 这两组命名各代表什么？

> **参考答案**：`ActOnXxx` 是 Parser 在拿到部分语法信息时回调 Sema 的「动作钩子」，通常只做轻量分发与转译（如把 `TokenKind` 转成 `BinaryOperatorKind`）；`BuildXxx`/`CreateXxx`/`CheckXxx` 才是真正构造节点或做检查的内部实现，命名暗示其职责。

---

### 4.2 名字查找与类型检查

#### 4.2.1 概念说明

这是 Sema 最高频的两项工作，几乎每条语句都要做。

**名字查找（name lookup）** 解决「这个名字指谁」。给定一个标识符与一个起始 `Scope`（作用域），Sema 沿作用域链向上搜索，找到匹配的 `NamedDecl`。在 C 中这是纯词法过程（从内层块向外层、再到全局）；在 C++ 中还要考虑名字空间、`using` 指示、基类子对象等。查找结果统一封装在 `LookupResult` 对象里，其 `LookupResultKind` 表明是「没找到 / 找到一个 / 找到一组重载 / 歧义」等情形。

**类型检查（type checking）** 解决「这个操作类型上成不成立、结果类型是什么」。以二元运算 `a + b` 为例，Sema 不仅要判断 `a`、`b` 的类型能否相加（比如指针加整数合法、两个结构体相加通常非法），还要算出整个表达式的结果类型，并据此构造 `BinaryOperator` 节点。类型不匹配时，Sema 会尝试**隐式转换**（如 `int` 提升为 `double`），实在不行就 `Diag` 报错。

名字查找与类型检查常常交织：比如 `a + b` 里要先查找 `a`、`b` 各自指向哪个声明、得到它们的类型，才能做类型检查。

#### 4.2.2 核心流程

名字查找（C/Objective-C 的简化路径）：

```text
Sema::LookupName(LookupResult &R, Scope *S)
   ├── 取出待查名字 Name = R.getLookupName()
   ├── C 路径：用 IdentifierResolver 枚举所有可见的同名声明
   │     for (每个候选 D)  若 R.getAcceptableDecl(D) 满足条件：
   │           加入结果；命中即可能停止（取决于 LookupNameKind）
   └── 把结果写回 R（NotFound / Found / FoundOverloaded / Ambiguous ...）
```

类型检查（以二元运算为例）：

```text
Parser 解析到 "a + b"
   └── Actions.ActOnBinOp(S, TokLoc, tok::plus, LHS, RHS)
          ├── ConvertTokenKindToBinaryOpcode(tok::plus)  →  BO_Add
          └── BuildBinOp(S, OpLoc, BO_Add, LHS, RHS)         // 分发器
                 ├── 若是 C++ 重载运算符或类型依赖  →  BuildOverloadedBinOp(...)   （见 4.3）
                 └── 否则（内建类型）              →  CreateBuiltinBinOp(...)
                        ├── switch (Opc): 按运算符分派
                        │     BO_Assign → CheckAssignmentOperands(...)
                        │     BO_Add    → 检查算术/指针规则，算 ResultTy
                        │     ...
                        └── 类型不匹配且无法隐式转换 → Diag(OpLoc, diag::err_...) 报错
```

#### 4.2.3 源码精读

名字查找的总入口：

[clang/lib/Sema/SemaLookup.cpp:2217-2218](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Sema/SemaLookup.cpp#L2217-L2218) —— `bool Sema::LookupName(LookupResult &R, Scope *S, ...)`，返回是否找到。

[clang/lib/Sema/SemaLookup.cpp:2224-2235](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Sema/SemaLookup.cpp#L2224-L2235) —— 注释点明「C/Objective-C 的非限定名字查找是纯词法的」，即沿作用域链搜索。第 2235 行的 `FindLocalExternScope FindLocals(R)` 用于在作用域查找时把局部 `extern` 声明也算进来。

[clang/lib/Sema/SemaLookup.cpp:2243-2246](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Sema/SemaLookup.cpp#L2243-L2246) —— C 路径用 `IdentifierResolver` 枚举同名声明，对每个候选调用 `R.getAcceptableDecl(*I)` 过滤出可接受的，命中后加入结果。这正是「内层遮蔽外层、命中即可能停止」的实现基础。

查找结果的种类：

[clang/include/clang/Sema/Lookup.h:39-65](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/include/clang/Sema/Lookup.h#L39-L65) —— `enum class LookupResultKind`，取值有 `NotFound`（没找到）、`Found`（找到单个）、`FoundOverloaded`（找到一组重载）、`Ambiguous`（歧义）等。查找函数把结果写进 `LookupResult`，调用方据此决定下一步（找不到 → 报「未声明的标识符」；`FoundOverloaded` → 进入重载决议）。

声明的语义入口（名字/类型检查的另一面）：

[clang/lib/Sema/SemaDecl.cpp:6310-6311](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Sema/SemaDecl.cpp#L6310-L6311) —— `Decl *Sema::ActOnDeclarator(Scope *S, Declarator &D)`，是 Parser 处理声明时回调 Sema 的入口。

[clang/lib/Sema/SemaDecl.cpp:6493-6512](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Sema/SemaDecl.cpp#L6493-L6512) —— `NamedDecl *Sema::HandleDeclarator(...)` 是真正构造声明的内部函数。它取出名字、校验、把声明挂到当前 `DeclContext`。第 6503-6507 行展示了典型的「名字缺失即报错」分支：

```cpp
} else if (!Name) {
  if (!D.isInvalidType())  // Reject this if we think it is valid.
    Diag(D.getDeclSpec().getBeginLoc(), diag::err_declarator_need_ident)
        << D.getDeclSpec().getSourceRange() << D.getSourceRange();
  return nullptr;
}
```

这就是「构造节点的同时做检查、检查不过就 `Diag` 报错」的标准写法。`Diag` 经 4.1 讲过的 `SemaBase::Diag` 出口发射，`<<` 塞入需要高亮的源码范围。

表达式类型检查的三段式：

[clang/lib/Sema/SemaExpr.cpp:15997-16014](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Sema/SemaExpr.cpp#L15997-L16014) —— `ExprResult Sema::ActOnBinOp(...)`：先把 `tok::plus` 之类的 `TokenKind` 转成 `BinaryOperatorKind`（`BO_Add`），做一点优先级相关的告警，然后调用 `BuildBinOp(...)`。注意它本身不构造 `BinaryOperator`，是典型的薄壳入口。

[clang/lib/Sema/SemaExpr.cpp:16067-16077](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Sema/SemaExpr.cpp#L16067-L16077) —— `ExprResult Sema::BuildBinOp(...)` 是分发器，注释明确写出三种归宿：「`checkAssignment`（LHS 是伪对象）/ `BuildOverloadedBinOp`（C++ 重载或类型依赖）/ `CreateBuiltinBinOp`（其余情况）」。本节关注第三条内建路径，重载路径见 4.3。

[clang/lib/Sema/SemaExpr.cpp:15527-15590](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Sema/SemaExpr.cpp#L15527-L15590) —— `ExprResult Sema::CreateBuiltinBinOp(...)` 是内建二元运算类型检查的真正实现。它先声明 `QualType ResultTy`（结果类型）等变量，第 15588 行 `switch (Opc)` 按运算符分派：例如第 15589-15590 行 `BO_Assign` 分支调用 `CheckAssignmentOperands(...)` 算 `ResultTy`。其它分支同理处理算术、比较、逻辑等运算；当类型既不匹配又无法隐式转换时，调用 `Diag(OpLoc, diag::err_typecheck_...)` 报错（这类诊断 ID 在该文件中大量出现，如 `err_typecheck_illegal_increment_decrement`）。

#### 4.2.4 代码实践

**实践目标**：亲手触发 Sema 的类型检查报错，并在源码中定位它。

**操作步骤**：

1. 准备文件 `err.c`：
   ```c
   struct S { int x; };
   int f(void) {
     struct S a;
     return a + 1;   /* 结构体加整数：类型非法 */
   }
   ```
2. 只跑前端语义分析、不生成代码：
   ```bash
   clang -fsyntax-only err.c
   ```
   > 说明：`-fsyntax-only` 让编译停在「词法 + 语法 + 语义」之后，是观察 Sema 诊断最干净的方式（见 u5-l1 的 driver/cc1 概念）。
3. 阅读报错信息，注意它形如 `error: invalid operands to binary expression ('struct S' and 'int')`。记下这个诊断文本。
4. 在源码里定位：在 `clang/lib/Sema/SemaExpr.cpp` 中搜索 `invalid operands to binary`（或 `err_typecheck` 系列诊断 ID），找到 `CreateBuiltinBinOp` 报出该错误的分支，确认它就是第 15527 行起的函数内。

**需要观察的现象**：报错信息会指出**具体的源码位置**（文件:行:列）与**涉及的类型**（`struct S` 与 `int`）。这说明 `Diag` 把「位置 + DiagID + 实参（两个类型）」组合成了一条带类型信息的诊断。

**预期结果**：你能解释「`a + 1` 经 `ActOnBinOp` → `BuildBinOp` → `CreateBuiltinBinOp`，在 `switch(Opc)` 的算术分支里发现 `struct S` 与 `int` 不能相加、也无法隐式转换，于是 `Diag` 报 `invalid operands`」。

**待本地验证**：不同 Clang 版本的具体诊断措辞可能略有差异，但 `invalid operands to binary expression` 这一说法长期稳定。

#### 4.2.5 小练习与答案

**练习 1**：调用 `print(x)` 时，名字查找返回 `FoundOverloaded`（找到一组重载），接下来 Sema 走哪条路？

> **参考答案**：名字查找只回答「有哪些同名声明」，不决定用哪一个。当结果是 `FoundOverloaded` 时，Sema 把这些候选连同实参交给**重载决议**（见 4.3）去挑出最佳函数；若挑出唯一最佳者，就用它；否则报「无 viable 函数」或「歧义」。

**练习 2**：为什么 `ActOnBinOp` 不直接构造 `BinaryOperator`，而要先转调 `BuildBinOp`？

> **参考答案**：因为同一个运算符在 C 与 C++ 下走不同路径——C++ 的运算符可能被用户重载（需走 `BuildOverloadedBinOp`），表达式也可能是类型依赖的（模板里）；而内建类型走 `CreateBuiltinBinOp`。`BuildBinOp` 这个分发器集中处理「选哪条路」的判断，`ActOnBinOp` 只负责把 Parser 的 `TokenKind` 翻译成语义层的 `BinaryOperatorKind` 并做少量前置告警，职责清晰。

---

### 4.3 重载决议

#### 4.3.1 概念说明

C++ 的重载决议（overload resolution）是 Sema 中规则最复杂的一块，但其骨架可以归纳为三步：

1. **收集候选（candidates）**：把名字查找找到的所有同名函数、以及实参依赖查找（ADL）找到的函数，统统放入一个 `OverloadCandidateSet`。
2. **算隐式转换序列（ICS）**：对每个候选，为「把每个实参转成对应形参类型」各算一条隐式转换序列。ICS 分几类：标准转换（最弱，如算术转换）、用户定义转换（经构造/转换函数）、省略号转换（`...`）。一条 ICS 越「省钱」越好；若根本无法转换，则该候选**不可行（not viable）**。
3. **择优（best viable）**：在所有可行候选中两两比较，套用 C++ 标准 [over.match.best] 的规则挑出「严格优于其它所有候选」的那一个。若存在唯一最佳 → 选用；若无任何可行候选 → `OR_No_Viable_Function`；若两个候选难分高下 → `OR_Ambiguous`（歧义）；若选中的是被 `= delete` 的 → `OR_Deleted`。

判定「候选 A 是否优于 B」的核心是比较两者的 ICS：标准转换优于用户定义转换；同为标准转换时再看「精度」——例如 `int→int`（精确匹配）优于 `int→double`（浮点提升）优于 `double→int`（可能损失精度）。这就是为什么 `f(int)` 与 `f(double)` 同时存在时，调用 `f(1)`（`1` 是 `int`）会选中 `f(int)`。

#### 4.3.2 核心流程

```text
a + b（C++ 且至少一侧可重载）或 print(args)
   └── BuildBinOp / ActOnCallExpr
          └── CreateOverloadedBinOp(...) / BuildOverloadedCallExpr(...)
                 ├── 构造 OverloadCandidateSet
                 ├── 对每个候选函数：
                 │     AddOverloadCandidate(...)
                 │       └── 为每个实参算 ImplicitConversionSequence，标记是否 Viable
                 │     （若函数模板，先 DeduceTemplateArguments 推导模板参数，见 4.4）
                 ├── 全部加入集合后：
                 │     CandidateSet.BestViableFunction(S, Loc, Best)
                 │       └── BestViableFunctionImpl(...)
                 │             for 每个候选 C：若 isBetterOverloadCandidate(S, C, Best, ...)
                 │                   Best = C
                 │             返回 OR_Success / OR_Ambiguous / OR_No_Viable_Function / OR_Deleted
                 └── 据 Best 构造 CallExpr / CXXOperatorCallExpr；或报「歧义 / 无匹配」错误
```

ICS 的择优用一条偏序关系描述。若记两条 ICS 的优劣关系为「\(\prec\)」（\(X \prec Y\) 表示 \(X\) 更优），则候选 \(A\) 优于 \(B\) 的必要条件是：对每个实参位置 \(i\)，\(A\) 的第 \(i\) 条 ICS \(\preceq\) \(B\) 的第 \(i\) 条 ICS，且至少有一个位置严格更优。

\[ \text{Better}(A, B) \;\Longleftrightarrow\; \bigl(\forall i,\; \mathrm{ICS}_{A,i} \preceq \mathrm{ICS}_{B,i}\bigr) \;\wedge\; \bigl(\exists j,\; \mathrm{ICS}_{A,j} \prec \mathrm{ICS}_{B,j}\bigr) \]

#### 4.3.3 源码精读

重载结果的四种结局：

[clang/include/clang/Sema/Overload.h:48-62](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/include/clang/Sema/Overload.h#L48-L62) —— `enum OverloadingResult`，取值 `OR_Success`、`OR_No_Viable_Function`、`OR_Ambiguous`、`OR_Deleted`，正好对应上面四类结局。

ICS 的表示：

[clang/include/clang/Sema/Overload.h:622](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/include/clang/Sema/Overload.h#L622) —— `class ImplicitConversionSequence`，其内部 `Kind` 枚举区分 `StandardConversion`、`UserDefinedConversion`、`EllipsisConversion`、`AmbiguousConversion`、`BadConversion` 等。每个候选对每个实参都算一条这样的序列。

重载运算符的入口：

[clang/lib/Sema/SemaOverload.cpp:15502-15504](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Sema/SemaOverload.cpp#L15502-L15504) —— `ExprResult Sema::CreateOverloadedBinOp(...)`，处理 C++ 重载运算符；当发现没有可用的重载时，它会回退调用 `CreateBuiltinBinOp(...)`（即 4.2 的内建路径），体现「先试重载、不行再走内建」的兜底。

函数调用的重载入口：

[clang/lib/Sema/SemaOverload.cpp:15061](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Sema/SemaOverload.cpp#L15061) —— `ExprResult Sema::BuildOverloadedCallExpr(...)`，对函数调用表达式（含函数名指向重载集的情形）发起重载决议，最终转交 `FinishOverloadedCallExpr`。

往集合里添加候选并算 ICS：

[clang/lib/Sema/SemaOverload.cpp:7285-7286](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Sema/SemaOverload.cpp#L7285-L7286) —— `void Sema::AddOverloadCandidate(FunctionDecl *Function, ...)`，把一个候选加入 `OverloadCandidateSet` 并为各实参计算 ICS、判定可行性。这是「收集 + 算 ICS」步骤的核心。

比较两条 ICS：

[clang/lib/Sema/SemaOverload.cpp:4470](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Sema/SemaOverload.cpp#L4470) —— `static ... CompareImplicitConversionSequences(Sema&, SourceLocation, const ICS& ICS1, const ICS& ICS2)`，按 C++ [over.ics.rank] 比较两条隐式转换序列的优劣，返回「谁更好 / 等价 / 无法判定」。

择优算法的核心：

[clang/lib/Sema/SemaOverload.cpp:10978-10981](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Sema/SemaOverload.cpp#L10978-L10981) —— `bool clang::isBetterOverloadCandidate(Sema&, const OverloadCandidate &Cand1, const OverloadCandidate &Cand2, ...)`，实现 C++ [over.match.best] 的择优规则，是整个重载决议的「心脏」。第 10982-10987 行先把「可行优于不可行」作为最基础的判据，随后才进入 ICS 的细致比较。

挑出最佳候选的驱动循环：

[clang/lib/Sema/SemaOverload.cpp:11604-11626](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Sema/SemaOverload.cpp#L11604-L11626) —— `OverloadingResult OverloadCandidateSet::BestViableFunction(Sema&, SourceLocation, iterator &Best)`，是择优的对外入口；它最终调用 `BestViableFunctionImpl`，后者遍历所有候选、反复用 `isBetterOverloadCandidate` 维护「当前最佳」，返回最终的 `OverloadingResult`。

#### 4.3.4 代码实践

**实践目标**：亲手构造一个重载决议场景，观察「最佳候选」被选出（或报歧义），并定位择优源码。

**操作步骤**：

1. 准备文件 `ovl.cpp`：
   ```cpp
   void f(int);
   void f(double);
   int main() {
     f(1);   // 1 是 int：应选中 f(int)
   }
   ```
2. 编译并要求打印被选中函数：
   ```bash
   clang++ -fsyntax-only -Xclang -fdump-overloads ovl.cpp
   clang++ -c -Xclang -ast-dump ovl.cpp 2>/dev/null | grep -A2 CallExpr
   ```
   > 说明：`-Xclang -ast-dump` 透传给 cc1 的 AST 转储（见 u5-l3），可以看到 `CallExpr` 实际绑定到哪个 `FunctionDecl`。
3. 把实参从 `1`（`int`）改成 `1.0`（`double`），重新观察：这次应选中 `f(double)`。
4. 再加一个 `void f(long);`，用 `f(1)` 调用：现在 `f(int)` 仍优于 `f(long)`（精确匹配优于转换），不会歧义。
5. 改成同时提供 `void f(long);` 与 `void f(float);`，调用 `f(1)`：`int→long` 与 `int→float` 同属「转换」且难分高下，应报 `ambiguous` 错误。
6. 定位源码：在 `clang/lib/Sema/SemaOverload.cpp` 第 10978 行阅读 `isBetterOverloadCandidate`，理解它如何比较两个候选的 ICS。

**需要观察的现象**：

- 第 2 步：AST 中 `CallExpr` 的 callee 指向 `FunctionDecl f 'void (int)'`。
- 第 3 步：callee 变为 `f 'void (double)'`。
- 第 5 步：编译器报 `error: call to 'f' is ambiguous`，并列出所有候选。

**预期结果**：你能用「精确匹配优于转换、同为转换时可能歧义」解释每一步选中（或失败）的原因，并知道择优逻辑在 `isBetterOverloadCandidate` / `BestViableFunction`。

**待本地验证**：`-fdump-overloads` 这类调试开关在不同版本可用性不一；若不可用，改用 `-ast-dump` 观察 `CallExpr` 绑定的 `FunctionDecl` 即可达到同样目的。

#### 4.3.5 小练习与答案

**练习 1**：`OR_Deleted` 表示什么？编译器为什么不直接在选中 `= delete` 函数时立刻报「未定义」？

> **参考答案**：`OR_Deleted` 表示重载决议「成功选中」了一个被 `= delete` 标记删除的函数。Clang 仍把它视为合法候选参与择优（这样可以精确报「使用了被删除的函数」，而不是误报「无匹配」），只在最终确认它被选中后，才针对**使用点**给出「call to deleted function」的诊断，便于用户定位。

**练习 2**：为什么 `f(int)` 与 `f(double)` 同时存在时，`f(1)` 选 `f(int)`，而 `f(1.0)` 选 `f(double)`？

> **参考答案**：`1` 是 `int`，到 `f(int)` 是精确匹配（ICS 最优），到 `f(double)` 需要 `int→double` 的浮点转换，故前者严格优于后者；`1.0` 是 `double`，同理选 `f(double)`。`isBetterOverloadCandidate` 通过比较两者的 ICS 得出这一结论。

---

### 4.4 模板实例化

#### 4.4.1 概念说明

模板（`template <typename T> ...`）本身不是代码，只有实例化（用具体类型替换模板参数）后才生成真实代码。Sema 处理模板有三个关键设计：

1. **模板实参推导（deduction）**：调用 `twice(3)` 时，编译器从实参 `3`（`int`）反推出模板参数 `T = int`。这一步由 `DeduceTemplateArguments` 完成，是「函数模板」参与重载决议的前提——只有推导成功，它才作为一个候选加入 4.3 的候选集。

2. **用到才实例化（lazy / on-demand）**：声明一个模板特化（如 `std::vector<int> v;`）通常只需实例化类定义；而函数体的实例化往往推迟到真正「需要定义」时（如取地址、被调用且需要定义）。这样能避免实例化大量用不着的代码，缩短编译时间。

3. **延迟队列（pending instantiations）**：实例化一个模板可能触发新的实例化需求（实例化函数 A 发现它调用了模板 B），而当前可能正处在不能立即深入的状态。Clang 把这些「待实例化」请求压入 `PendingInstantiations` 队列，等合适时机（如翻译单元末尾）由 `PerformPendingInstantiations` 统一排空。这种「记录—稍后批量执行」避免了递归实例化时的状态冲突。

实例化的实现分两类：**声明**层面用 `TemplateDeclInstantiator`（一个 `DeclVisitor` 访问者，对每种声明节点有对应处理）；**类型/表达式**层面用 `TemplateInstantiator`（`TreeTransform` 的子类，遍历并代换类型与表达式里的模板参数）。

#### 4.4.2 核心流程

```text
源码中出现 max<int>(a, b)
   ├── 名字查找找到函数模板 max
   ├── 重载决议前先推导：DeduceTemplateArguments(...)
   │     └── T 推导为 int → 把 max<int> 作为候选加入 OverloadCandidateSet（参与 4.3 择优）
   ├── 选中 max<int> 后，需要其定义时：
   │     若定义尚未实例化 → 记录一个「待实例化」请求，压入 PendingInstantiations 队列
   │     （也可在条件允许时立即实例化）
   └── 时机成熟（如 TU 末尾）：
         PerformPendingInstantiations()
           while 队列非空：
             取出 (Function, TSK)
               └── InstantiateFunctionDefinition(Point, Function, ...)
                     ├── 取出模板的定义（Pattern）
                     ├── TemplateDeclInstantiator / TemplateInstantiator 把 T=int 代换进定义
                     └── 生成 max<int> 的真实定义，递归触发的实例化继续入队
```

#### 4.4.3 源码精读

待实例化队列：

[clang/include/clang/Sema/Sema.h:14141-14143](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/include/clang/Sema/Sema.h#L14141-L14143) —— `std::deque<PendingImplicitInstantiation> PendingInstantiations;`，注释写明「需要但尚未执行的隐式模板实例化队列」。这是一个双端队列，先入先出地排空。

模板实参推导：

[clang/lib/Sema/SemaTemplateDeduction.cpp:2850-2852](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Sema/SemaTemplateDeduction.cpp#L2850-L2852) —— `TemplateDeductionResult Sema::DeduceTemplateArguments(TemplateParameterList *TemplateParams, ArrayRef<TemplateArgument> Ps, ArrayRef<TemplateArgument> As, ...)`，把形参模式 `Ps` 与实参模式 `As` 配对，推导出 `Deduced`。推导结果是 `TemplateDeductionResult` 枚举（成功 / 失败的各种原因）。函数模板只有在推导成功后才成为重载候选。

实例化声明的访问者：

[clang/include/clang/Sema/Template.h:586-589](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/include/clang/Sema/Template.h#L586-L589) —— `class TemplateDeclInstantiator : public DeclVisitor<TemplateDeclInstantiator, Decl *>`。基于访问者模式：对每种 `Decl` 子类提供一个重载，用当前模板实参代换出该声明实例化后的版本。这是「声明层实例化」的骨架。

代换类型/表达式的树变换：

[clang/lib/Sema/SemaTemplateInstantiate.cpp:1312-1315](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Sema/SemaTemplateInstantiate.cpp#L1312-L1315) —— `class TemplateInstantiator : public TreeTransform<TemplateInstantiator>`，持有 `const MultiLevelTemplateArgumentList &TemplateArgs`。`TreeTransform` 是 Clang 的 AST 树重写框架，`TemplateInstantiator` 复用它来「把模板参数代换进类型与表达式」，是「表达式/类型层实例化」的实现。

实例化函数定义：

[clang/lib/Sema/SemaTemplateInstantiateDecl.cpp:5594-5613](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Sema/SemaTemplateInstantiateDecl.cpp#L5594-L5613) —— `void Sema::InstantiateFunctionDefinition(SourceLocation PointOfInstantiation, FunctionDecl *Function, bool Recursive, bool DefinitionRequired, bool AtEndOfTU)`。它先做一系列短路判断：无效声明直接返回（5599 行）、显式特化不再实例化（5604-5607 行，`TSK_ExplicitSpecialization`）、内建函数通常不需要函数体（5609-5613 行）。通过这些检查后，才取出模板的 Pattern、代换模板实参、生成真实定义。`PointOfInstantiation` 记录「在源码哪里被用到」，`AtEndOfTU` 表示是否在 TU 末尾才补做。

排空队列的驱动循环：

[clang/lib/Sema/SemaTemplateInstantiateDecl.cpp:7260-7277](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Sema/SemaTemplateInstantiateDecl.cpp#L7260-L7277) —— `void Sema::PerformPendingInstantiations(bool LocalOnly, bool AtEndOfTU)`。核心是第 7262-7263 行的 `while (!PendingLocalImplicitInstantiations.empty() || (!LocalOnly && !PendingInstantiations.empty()))` 循环：不断从队列取出待实例化项，若是 `FunctionDecl` 则调用 `InstantiateFunctionDefinition`（第 7277 行起）。注意：实例化过程可能往同一个队列里**继续压入新请求**（如实例化 A 触发 B），循环因此会多转几圈，直到队列真正清空——这正确处理了连锁实例化。

#### 4.4.4 代码实践

**实践目标**：观察模板实例化被触发，并定位「延迟队列」与「排空」两段源码。

**操作步骤**：

1. 准备文件 `tmpl.cpp`：
   ```cpp
   template <typename T>
   T twice(T x) { return x + x; }

   int main() {
     return twice(3);   // 隐式实例化 twice<int>
   }
   ```
2. 只做前端语义分析，并用模板调试选项观察：
   ```bash
   clang++ -fsyntax-only -Xclang -fdelayed-template-parsing tmpl.cpp
   clang++ -c -Xclang -ast-dump tmpl.cpp 2>/dev/null | grep -i "FunctionDecl.*twice"
   ```
   > 说明：`-fdelayed-template-parsing`（MSVC 兼容场景常用）会让函数模板体延迟解析，正好对应「用到再实例化」的思路。
3. 在 AST 转储里，你应该能看到两份与 `twice` 相关的 `FunctionDecl`：一份是模板本身，一份是 `twice<int>` 的特化（带 `T=int` 实例化的定义）。
4. 在源码里对照两条线索：
   - `clang/include/clang/Sema/Sema.h` 第 14143 行的 `PendingInstantiations` 队列；
   - `clang/lib/Sema/SemaTemplateInstantiateDecl.cpp` 第 7260 行的 `PerformPendingInstantiations`，确认它的 `while` 循环如何排空该队列，并在第 7277 行附近对 `FunctionDecl` 调用 `InstantiateFunctionDefinition`。

**需要观察的现象**：即使源码里只写了模板 `twice<T>`，AST 中也会出现一个具体的 `twice<int>` 定义——这正是实例化的产物。若把 `main` 里的 `twice(3)` 删掉（模板没被用到），AST 里通常不会出现 `twice<int>` 的定义，印证「用到才实例化」。

**预期结果**：你能讲清「`twice(3)` 触发推导 `T=int` → 重载选中 → 需要定义时把请求入队 `PendingInstantiations` → `PerformPendingInstantiations` 排空队列 → `InstantiateFunctionDefinition` 代换出 `twice<int>` 定义」。

**待本地验证**：模板实例化的具体 AST 形态（是否带 `fsyntax-only` 时就生成定义）受 `-fdelayed-template-parsing`、优化等级与语言标准影响；以本地 `clang++` 实际输出为准。核心「队列 + 排空」的源码结构长期稳定。

#### 4.4.5 小练习与答案

**练习 1**：为什么 Clang 要用「延迟队列」而不是在用到模板的那一刻立刻、递归地实例化到底？

> **参考答案**：立刻递归实例化有两个风险：一是当前可能正处在语义分析的中间状态（如正在做某个表达式的检查），深入递归实例化会破坏正在使用的临时上下文；二是同一模板可能在很多处被用到，集中排队可以合并、去重并控制顺序。把请求压入 `PendingInstantiations`、稍后由 `PerformPendingInstantiations` 统一排空，既避免了状态冲突，又能正确处理「实例化 A 又触发 B」的连锁（新请求继续入队，循环多转几圈）。

**练习 2**：函数模板是如何「融入」4.3 的重载决议的？

> **参考答案**：函数模板先经过 `DeduceTemplateArguments` 推导出模板实参（如由实参 `3` 推出 `T=int`）；推导成功后，这个具化的 `twice<int>` 才作为一个普通候选加入 `OverloadCandidateSet`，与其它非模板候选一起参与择优。也就是说，模板推导是重载决议的「前置步骤」，而非独立流程。

---

## 5. 综合实践

**任务**：用一段同时包含「类型错误」与「重载决议」的 C++ 代码，把本讲四个模块串起来验证：触发 Sema 的诊断、定位源码、解释背后的机制。

**准备文件** `sema_demo.cpp`：

```cpp
#include <iostream>

void print(int v)        { std::cout << "int: "    << v << "\n"; }
void print(double v)     { std::cout << "double: " << v << "\n"; }

struct Point { int x, y; };

template <typename T>
T add(T a, T b) { return a + b; }   // 对 Point 无意义：T+T 非法

int main() {
  print(1);            // (A) 重载决议：选 print(int)
  print(2.0);          // (B) 重载决议：选 print(double)
  print(3.0f);         // (C) float→double 转换，仍选 print(double)

  int s = add(4, 5);   // (D) 模板推导 T=int，实例化 add<int>
  std::cout << s << "\n";

  Point p{1, 2}, q{3, 4};
  Point r = add(p, q); // (E) 模板推导 T=Point，实例化 add<Point>，
                       //     函数体 p+q 类型非法 → Sema 在实例化体内报错
}
```

**操作步骤**：

1. 只做前端语义分析：
   ```bash
   clang++ -fsyntax-only sema_demo.cpp
   ```
2. 观察输出：
   - (A)/(B)/(C) 不应报错——重载决议成功（用 4.3 的 `isBetterOverloadCandidate` 选出最佳）。
   - (E) 应报类似 `error: invalid operands to binary expression ('Point' and 'Point')` 的错误，且**指出错误位于模板 `add` 的函数体内**（注意报错位置会标注「in instantiation of 'add<Point>' requested here」之类的实例化栈）。
3. 把 (E) 注释掉重新编译，确认 (A)–(D) 全部通过；再用 `-Xclang -ast-dump` 看 `CallExpr` 绑定到了哪个 `print` 重载、`add<int>` 的特化是否出现在 AST 中。
4. 对照源码定位三处机制：
   - 类型检查的报错来源：`clang/lib/Sema/SemaExpr.cpp` 第 15527 行 `CreateBuiltinBinOp`（`p + q` 即 `Point+Point`，走的是 `+` 不是重载运算符，故经内建路径报 `invalid operands`）。
   - 重载择优：`clang/lib/Sema/SemaOverload.cpp` 第 10978 行 `isBetterOverloadCandidate` 解释 (A)/(B) 为何各选一个 `print`。
   - 模板实例化：`clang/lib/Sema/SemaTemplateInstantiateDecl.cpp` 第 5594 行 `InstantiateFunctionDefinition` 把 `T=Point` 代换进 `add` 的函数体，从而在**实例化后的体内**触发上面的类型检查报错。

**需要观察的现象与预期结果**：

- 报错信息会附带「模板实例化栈」（指出 `add<Point>` 是在哪一行被实例化、又是在模板体的哪一行出错），这正是 4.4「延迟实例化」+「`PointOfInstantiation` 记录用到位置」的直接体现。
- 通过 `(A)` 与 `(B)` 的差异，你能用 ICS 的优劣讲清「精确匹配优于转换」。
- 你能把整条链路讲圆：名字查找找到 `print`/`add` →（模板先推导）→ 重载决议挑出唯一最佳 → 模板按需实例化 → 在实例化体里再做类型检查 → 不通过则 `Diag` 报错。

**进阶**：把 `print(int)` 删掉，只留 `print(double)`，调用 `print(1)`——仍能编译（`int→double` 转换），体会「只有一个可行候选时无需择优」。再同时加 `print(long)` 与 `print(float)` 调 `print(1)`，复现 4.3 的 `ambiguous` 错误。

**待本地验证**：`<iostream>` 引入的 `operator<<` 重载集合庞大，极端情况下 `-ast-dump` 输出很长；若只想聚焦 `print`/`add`，可去掉 `<iostream>`、改用返回值与 `extern "C"` 的 `printf`，让 AST 更干净。

## 6. 本讲小结

- `Sema`（`class Sema final : public SemaBase`，定义于 `Sema.h`）是 Parser 的「语义动作」接口：它既是 AST 节点的工厂，又是语义裁判；方法按主题拆到 `Sema*.cpp` 数十个文件，但扩展的是同一个类。
- 所有诊断走统一出口 `SemaBase::Diag(Loc, DiagID) << ...`（`SemaBase.cpp:61`）；所有节点从 `ASTContext` 的 `BumpPtrAllocator` 竞技场分配，最终挂到 `TranslationUnitDecl`。
- **名字查找**：`Sema::LookupName`（`SemaLookup.cpp:2217`）沿作用域链搜索，结果存入 `LookupResult`（`LookupResultKind`：`NotFound`/`Found`/`FoundOverloaded`/`Ambiguous`…）。
- **类型检查**：表达式走 `ActOnBinOp`→`BuildBinOp`→`CreateBuiltinBinOp`（`SemaExpr.cpp:15997/16067/15527`），声明走 `ActOnDeclarator`→`HandleDeclarator`（`SemaDecl.cpp:6310/6493`）；不过就 `Diag` 报错。
- **重载决议**三步——收集候选（`AddOverloadCandidate`）、算 ICS、择优（`isBetterOverloadCandidate` `SemaOverload.cpp:10978` + `BestViableFunction` `11604`），结局为 `OR_Success`/`OR_No_Viable_Function`/`OR_Ambiguous`/`OR_Deleted`。
- **模板实例化**：先 `DeduceTemplateArguments` 推导（函数模板才能参与重载），需要定义时把请求入队 `PendingInstantiations`（`Sema.h:14143`），由 `PerformPendingInstantiations`（`SemaTemplateInstantiateDecl.cpp:7260`）排空，逐个调 `InstantiateFunctionDefinition`（`5594`），用 `TemplateDeclInstantiator`/`TemplateInstantiator` 代换模板实参；连锁实例化靠循环自然收敛。

## 7. 下一步学习建议

本讲到「Sema 完成语义检查、构造出完整且语义合法的 AST」为止。这棵 AST 接下来要被翻译成 LLVM IR，这正是下一讲 **u5-l5 CodeGen：从 AST 到 LLVM IR** 的主题：`CodeGenModule`/`CodeGenFunction` 会遍历本讲产出的 `Decl`/`Stmt` 树，逐节点生成 LLVM IR。建议结合本讲理解：CodeGen 消费的是「Sema 已校验过的」AST，因此很多语义保证（如重载已决议、类型已确定）在 CodeGen 阶段可以直接信赖。

若你对 C++ 模板、概念（concepts）的语义实现感兴趣，可继续阅读 `clang/lib/Sema/SemaTemplate.cpp`、`SemaConcept.cpp`，深入模板代换与约束求解。

若想横向对照「语义分析」在不同语言前端的差异，可浏览 `clang/lib/Sema/SemaDeclCXX.cpp`、`SemaDeclObjC.cpp`、`SemaHLSL.cpp` 等，体会同一套 Sema 框架如何承载多种语言规则。

源码精读建议：选一条你最熟悉的语句（如 `int x = a + f(b);`），从 `ActOnBinOp`、`BuildOverloadedCallExpr`、`HandleDeclarator` 一路跟踪到 `Diag`，亲手走通「Token → Sema 动作 → 类型检查 → 节点构造」的完整链路。
