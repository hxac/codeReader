# 语法分析 Parse 与 AST 构建

## 1. 本讲目标

上一讲（u5-l2）我们看到，预处理器的输出是一串 `Token`。本讲要回答的问题是：**Clang 如何把这串 Token 组织成一棵抽象语法树（AST）？**

学完本讲，你应当能够：

- 说清 `ParseAST` 作为语法分析总入口，是如何用一个循环把整个翻译单元逐个声明解析出来的。
- 理解 Clang 采用的**递归下降（recursive descent）**分析风格：每一条文法规则对应一个 `Parse...` 方法，方法之间相互调用，自然形成树状的调用栈。
- 掌握表达式解析中嵌入的**运算符优先级爬升（precedence climbing）**技巧。
- 认识 AST 的两大节点家族——`Decl`（声明）与 `Stmt`/`Expr`（语句与表达式），以及 `DeclContext`、`ASTContext` 的角色。
- 会用 `clang -Xclang -ast-dump` 真实地观察一段代码生成的 AST，并辨认其中的节点类型。

## 2. 前置知识

在进入源码前，先用通俗语言建立三个直觉。

**第一，什么是“语法分析”。** 编译器把源码变成可执行程序，要经过若干阶段。词法分析（Lexer）把字符流切成一个个 `Token`（如关键字 `int`、标识符 `add`、标点 `(`）；语法分析（Parser）则按语言的文法规则，把这些 `Token` 拼装成有层次的树结构——也就是 AST。可以类比：Token 是“单词”，文法是“语法”，AST 是“句子结构树”。

**第二，什么是“递归下降”。** C/C++ 的文法天然是嵌套的：函数体里有语句，语句里又有表达式，表达式里还可能嵌套函数调用。递归下降分析器为每条文法规则写一个函数：函数 `Parse函数()` 读到 `{` 后会调用 `Parse语句()`，`Parse语句()` 又可能调用 `Parse表达式()`，如此“下降”到最底层的 Token。代码读起来和文法几乎一一对应，这是 Clang 选择它的主要原因。

**第三，为什么“语法”和“语义”要分开。** Clang 把“能不能这样写”（语法，由 Parser 负责）和“这样写对不对”（语义，由 Sema 负责）拆成两套对象。Parser 只认 Token 和文法结构，每解析出一个语法单元，就把半成品交给 Sema（代码里叫 `Actions`）去真正**构造 AST 节点**并做类型检查。这样 Parser 可以相对通用，而 C/C++/Objective-C 各自的语言特性主要由 Sema 处理（Sema 细节留待 u5-l4）。

> 关键术语：Token、AST、Parser（语法分析器）、Sema（语义动作）、Decl（声明）、Stmt（语句）、Expr（表达式）、DeclContext（声明上下文）、ASTContext（AST 上下文）。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `clang/lib/Parse/ParseAST.cpp` | 语法分析的总入口 `clang::ParseAST`，负责创建 Parser 并循环驱动解析。 |
| `clang/lib/Parse/Parser.cpp` | Parser 的核心：顶层声明分发、`ParseTopLevelDecl`、`ParseExternalDeclaration`、函数定义解析。 |
| `clang/lib/Parse/ParseStmt.cpp` | 语句与复合语句（`{ ... }`）的递归下降解析。 |
| `clang/lib/Parse/ParseExpr.cpp` | 表达式解析，包括运算符优先级爬升循环。 |
| `clang/include/clang/Parse/Parser.h` | Parser 类声明，揭示它持有 `Preprocessor &` 与 `Sema &` 两个引用。 |
| `clang/include/clang/AST/DeclBase.h` | `Decl` 根类、`Decl::Kind` 枚举、`DeclContext` 的定义。 |
| `clang/include/clang/AST/Decl.h` | 各 `Decl` 子类：`NamedDecl`、`ValueDecl`、`VarDecl`、`FunctionDecl` 等。 |
| `clang/include/clang/AST/Stmt.h` | `Stmt` 根类、`StmtClass` 枚举、`children()` 遍历接口、`DeclStmt` 等。 |
| `clang/include/clang/AST/Expr.h` | `Expr`（继承自 `Stmt`）及其子类，如 `BinaryOperator`、`IntegerLiteral`。 |
| `clang/include/clang/AST/ASTContext.h` | `ASTContext`：AST 节点的内存竞技场，并持有翻译单元根 `TranslationUnitDecl`。 |

## 4. 核心概念与源码讲解

### 4.1 ParseAST：语法分析的总入口与 Parser/Sema 分工

#### 4.1.1 概念说明

`clang::ParseAST` 是“语法分析”这一步对外的总入口。它要做三件事：

1. **造一个 `Sema`**：Sema 是语义动作对象，负责构造 AST 节点、查名字、做类型检查。
2. **造一个 `Parser`**：把预处理器（`Preprocessor`）和 `Sema` 都交给它——Parser 只认 Token，每解析出一点结构就回调 Sema。
3. **循环解析顶层声明**：一个翻译单元（Translation Unit）本质是“一串顶层声明的序列”，于是用一个 `for` 循环反复调用 `ParseTopLevelDecl`，每得到一个声明就通过 `ASTConsumer` 报告给下游（最终会触发 CodeGen，见 u5-l5）。

这里体现了贯穿全讲的**Parser/Sema 分工**：Parser 是“Token 消费者 + 文法识别器”，它本身几乎不 new 出 AST 节点；真正的节点构造和合法性判断交给 `Actions`（即 Sema）。因此你会看到大量形如 `Actions.ActOnXxx(...)` 的调用，那是 Parser 把半成品递交给 Sema 的“动作（Action）”接口。

#### 4.1.2 核心流程

```text
ParseAST(PP, Consumer, Ctx)
   │
   ├── new Sema(PP, Ctx, Consumer)          // 语义动作对象
   ├── ParseAST(S, ...)                      // 进入第二个重载
   │     ├── new Parser(PP, S)              // Parser 持有 PP 与 S(=Actions)
   │     ├── PP.EnterMainSourceFile()        // 让预处理器进入主源文件
   │     ├── P.ConsumeToken()                // 预取第一个 Token
   │     └── for ( ... ParseTopLevelDecl(ADecl) ... )   // 循环解析顶层声明
   │            └── Consumer->HandleTopLevelDecl(ADecl)  // 把每个声明交给下游
   │
   └── Consumer->HandleTranslationUnit(Ctx)  // 整个 TU 解析完毕
```

要点：循环以 `ParseTopLevelDecl` 返回“到达 EOF”为终止条件；每解析出一个 `DeclGroup`（声明组），就立刻通过 `Consumer->HandleTopLevelDecl` 推送出去——这是一种**边解析边消费**的流式设计。

#### 4.1.3 源码精读

`ParseAST` 有两个重载。第一个负责构造 `Sema` 并转交：

[clang/lib/Parse/ParseAST.cpp:98-111](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Parse/ParseAST.cpp#L98-L111) —— 接收 `Preprocessor`、`ASTConsumer`、`ASTContext`，构造 `Sema`，再调用第二个重载。可以看到 `Sema` 把 `PP`、`Ctx`、`Consumer` 三者串了起来。

真正干活的是第二个重载。先看它如何创建 Parser 并启动预处理器：

[clang/lib/Parse/ParseAST.cpp:126-138](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Parse/ParseAST.cpp#L126-L138) —— `new Parser(S.getPreprocessor(), S, ...)` 把 Parser、Sema、Preprocessor 三者绑在一起；随后 `EnterMainSourceFile()` 让预处理器开始从主文件吐 Token。

核心的“循环解析顶层声明”在这里：

[clang/lib/Parse/ParseAST.cpp:158-172](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Parse/ParseAST.cpp#L158-L172) —— 先 `ConsumeToken()` 预取首 Token，再用 `for` 循环交替调用 `ParseFirstTopLevelDecl` 与 `ParseTopLevelDecl`，每得到一个非空声明就 `Consumer->HandleTopLevelDecl` 推送给下游。`AtEOF` 为真时循环结束。

Parser 持有 `Sema &` 的证据在类声明里：

[clang/include/clang/Parse/Parser.h:620-641](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/include/clang/Parse/Parser.h#L620-L641) —— Parser 同时持有 `Preprocessor &PP` 与 `Sema &Actions`。前者提供 Token 流，后者接收语义动作。这正是“语法/语义分离”在数据成员层面的体现。

#### 4.1.4 代码实践

**实践目标**：在源码层面确认“ParseAST → 循环 → ParseTopLevelDecl → Consumer”这条主线，而不去运行编译器。

**操作步骤**：

1. 打开 `clang/lib/Parse/ParseAST.cpp` 第 113 行起的第二个 `ParseAST` 重载。
2. 找到第 164 行的 `for` 循环，确认它的三个组成部分：初始调用 `ParseFirstTopLevelDecl`、终止条件 `!AtEOF`、步进 `ParseTopLevelDecl`。
3. 在循环体内找到第 169 行 `Consumer->HandleTopLevelDecl(ADecl.get())`，理解“每解析一个声明就上报一次”。
4. 思考：如果源文件里有 3 个顶层函数，这个循环体会执行几次 `HandleTopLevelDecl`？（提示：与 `ADecl` 是否为空有关，见第 169 行注释。）

**需要观察的现象**：`HandleTopLevelDecl` 的返回值被 `if` 检查（第 169 行 `!Consumer->HandleTopLevelDecl(...)` 为真则 `return`），说明消费者可以中途叫停解析。

**预期结果**：你能用自己的话讲清“ParseAST 用一个 for 循环把翻译单元拆成一个个顶层声明喂给 Consumer”。

#### 4.1.5 小练习与答案

**练习 1**：为什么 Parser 需要 `Sema &Actions` 而不是自己直接构造 AST 节点？

> **参考答案**：因为 Parser 只负责“语法层面能不能这样写”，而 AST 节点的构造涉及名字查找、类型记录等“语义”工作；把这两件事分别交给 Parser 和 Sema，可以让 Parser 相对通用、关注文法，而语言相关的语义规则集中在 Sema，便于维护和复用。

**练习 2**：`ParseAST` 里循环结束时（`AtEOF` 为真），还会执行哪个关键调用把“整个翻译单元解析完毕”的消息通知下游？

> **参考答案**：第 178 行 `Consumer->HandleTranslationUnit(S.getASTContext())`，它标志整个 TU 解析结束，是后续 CodeGen 等环节的重要触发点。

---

### 4.2 递归下降：一个文法规则对应一个 Parse 方法

#### 4.2.1 概念说明

这是本讲的核心模块。Clang 的 Parser 是**手写的递归下降分析器**（而非用工具自动生成）：C/C++ 文法中的每一条产生式，几乎都对应一个名为 `Parse...` 的成员函数。函数内部通常是一个 `switch (Tok.getKind())`，根据当前 Token 的种类决定走哪条分支，分支里再调用更细粒度的 `Parse...`，层层下降。

这种风格的优点是**可读、可控、便于错误恢复**：当源码有语法错误时，手写代码可以精细地决定“跳过哪些 Token、如何继续”，这正是 Clang 能给出高质量报错与修复建议（FixIt）的原因。

一个常被初学者忽略的细节：**表达式解析也属于递归下降，但用了“优先级爬升”技巧**来处理运算符优先级，而不是为每个优先级写一层函数（那样会过深）。这点放在本模块的源码精读里展开。

#### 4.2.2 核心流程

顶层声明的递归下降调用链示意：

```text
ParseTopLevelDecl()                      // 顶层：按首 Token 分发
   └── (default) ParseExternalDeclaration()
         └── ParseDeclarationOrFunctionDefinition()
               ├── 普通声明：ParseDeclaration() ... → Sema 构造 VarDecl 等
               └── 函数定义：ParseFunctionDefinition()
                     └── Parse函数体 { ... }
                           └── ParseCompoundStatement()        // 解析 { 语句列表 }
                                 └── 循环 ParseStatement()
                                       ├── 关键字语句：if/while/return 各有分支
                                       ├── 声明语句：ParseDeclaration() → ActOnDeclStmt
                                       └── 表达式语句：ParseExpression() → ... → ActOnBinOp 等
```

可见从“顶层声明”一路下降到“表达式里的一个运算符”，是一条连续的调用链，每层都消费若干 Token 并把结果交给 Sema。

#### 4.2.3 源码精读

**顶层分发的 switch**——`ParseTopLevelDecl` 按当前 Token 种类处理模块导入、EOF 等特殊情况，其余都落到 `ParseExternalDeclaration`：

[clang/lib/Parse/Parser.cpp:612-709](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Parse/Parser.cpp#L612-L709) —— 注意 `switch (Tok.getKind())` 的大结构：遇到 `eof` 返回 `true` 表示结束（第 678、694 行）；其余 `default` 分支在第 709 行调用 `ParseExternalDeclaration(DeclAttrs, DeclSpecAttrs)`。这是“按首 Token 分发”的典型写法。

`ParseExternalDeclaration` 又是一个大 switch，区分各种 `#pragma`、模板、命名空间等情况，最终把“声明或函数定义”交给：

[clang/lib/Parse/Parser.cpp:1154-1175](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Parse/Parser.cpp#L1154-L1175) —— `ParseDeclarationOrFunctionDefinition` 会构造一个 `ParsingDeclSpec`（正在解析的声明说明符，如 `int`、`static`），再进入内部实现，由 Sema 判断这到底是变量声明还是函数定义。

函数定义的解析入口（读到 `{` 后开始解析函数体）：

[clang/lib/Parse/Parser.cpp:1177-1226](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Parse/Parser.cpp#L1177-L1226) —— `ParseFunctionDefinition` 在确认当前是 `l_brace`（或 C++ 构造函数的 `:`/`try`）后，进入函数体的解析；若不是，则报 `err_expected_fn_body` 并尝试恢复。注意它处理了 K&R 风格参数声明等历史语法。

**语句的递归下降**在另一个文件里。`ParseStatement` 把工作转给 `ParseStatementOrDeclaration`，最终到带属性处理的版本，其 switch 分发各种语句：

[clang/lib/Parse/ParseStmt.cpp:134-255](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Parse/ParseStmt.cpp#L134-L255) —— `ParseStatementOrDeclarationAfterAttributes` 的 `switch (Kind)`：`tok::at` 走 ObjC、标识符带 `:` 走标号语句（第 164-172 行）；C/C++ 中允许“语句位置出现声明”时调用 `ParseDeclaration` 并用 `Actions.ActOnDeclStmt` 包装成声明语句（第 220-234 行）；最常见的情况落到第 253 行 `ParseExprStatement`。

**表达式的优先级爬升**。表达式入口 `ParseAssignmentExpression` 先解析一个 cast 表达式作为左值，再用“优先级爬升”循环处理后续运算符：

[clang/lib/Parse/ParseExpr.cpp:75-93](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Parse/ParseExpr.cpp#L75-L93) —— `ParseAssignmentExpression` 调 `ParseCastExpression` 得到 `LHS`，再调 `ParseRHSOfBinaryExpression(LHS, prec::Assignment)`。注意它把“最低允许优先级”`prec::Assignment` 作为参数传入——这是爬升算法的关键。

爬升循环本身：

[clang/lib/Parse/ParseExpr.cpp:316-334](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/lib/Parse/ParseExpr.cpp#L316-L334) —— `ParseRHSOfBinaryExpression` 的 `while (true)` 循环：先用 `getBinOpPrecedence` 查出下一个 Token 的优先级 `NextTokPrec`；若 `NextTokPrec < MinPrec`（低于允许解析的最低优先级）就返回当前 `LHS`（第 329-330 行）；否则吃掉运算符，递归解析更高优先级的右侧操作数，最后调 `Actions.ActOnBinOp`（见第 560 行）把左右操作数与运算符交给 Sema 构造 `BinaryOperator` 节点。

优先级爬升的本质可用一段递推描述：设当前已解析左部 \( L \)，允许解析的最低优先级为 \( p_{\min} \)，下一个运算符优先级为 \( p \)：

\[
\text{parse}(L,\ p_{\min}) =
\begin{cases}
L, & p < p_{\min} \\
\text{parse}(\text{ActOnBinOp}(L,\ op,\ \text{parseRHS}(p+1)),\ p_{\min}), & p \ge p_{\min}
\end{cases}
\]

即“只要下一个运算符优先级够高，就把它并进左部，并要求右侧操作数以更高优先级 \( p+1 \) 解析”，从而左结合地构造出正确结合的 AST。它的好处是不必为每个优先级层级写一个函数，调用栈更浅。

> 小结这一模块：**顶层声明 → 函数体 → 语句 → 表达式** 是一条递归下降的调用链；“按首 Token 的 switch 分发”是每层的骨架；表达式层额外用优先级爬升处理结合性；每解析出一个语法单元，都用 `Actions.ActOnXxx` 交给 Sema 落成 AST 节点。

#### 4.2.4 代码实践

**实践目标**：跟踪一条真实的递归下降调用链，看清“Token 如何被逐层消费”。

**操作步骤**：

1. 准备一个最小 C 文件 `add.c`，内容为：
   ```c
   int add(int a, int b) {
     return a + b;
   }
   ```
2. 在源码中按顺序定位以下函数（用本节给出的链接），在心里走一遍：
   - `ParseTopLevelDecl`（Parser.cpp:612）→ 看到 `int`，走 default → `ParseExternalDeclaration`（728）。
   - → `ParseDeclarationOrFunctionDefinition`（1154）→ 识别为函数定义 → `ParseFunctionDefinition`（1177）。
   - → 解析 `{` 后进入 `ParseCompoundStatement`（ParseStmt.cpp:997）→ 循环 `ParseStatement`（40）。
   - → 看到 `return` → 对应分支 → `ParseExpression` → `ParseAssignmentExpression`（ParseExpr.cpp:75）→ `ParseRHSOfBinaryExpression`（316）处理 `+`。
3. 在 `return a + b;` 这一行，列出 Parser 依次消费的 Token：`return`、`a`、`+`、`b`、`;`。

**需要观察的现象**：`+` 的优先级高于赋值，因此 `ParseRHSOfBinaryExpression` 在解析 `a + b` 时，会把 `a` 作为左操作数、`b` 作为右操作数合并成一个 `BinaryOperator`。

**预期结果**：你能画出 `add` 函数从 `ParseTopLevelDecl` 到 `ActOnBinOp` 的完整调用栈，并指出每个 `Parse...` 函数消费了哪些 Token。

#### 4.2.5 小练习与答案

**练习 1**：递归下降分析器为每条文法规则写一个函数。请根据本节源码，说出“语句”这一层是如何区分“声明语句”和“表达式语句”的。

> **参考答案**：在 `ParseStatementOrDeclarationAfterAttributes` 中（ParseStmt.cpp:134），当 C++ 或 C 的特定上下文允许声明时，会先判断当前位置是否像一个声明（`isDeclarationStatement()` 等）；若是声明则走 `ParseDeclaration` 并用 `ActOnDeclStmt` 包成 `DeclStmt`；否则落到 `ParseExprStatement` 当作表达式语句处理。

**练习 2**：为什么表达式解析不直接为每个运算符优先级写一层函数（如 `ParseMul`、`ParseAdd`、`ParseShift`……），而要用优先级爬升？

> **参考答案**：C/C++ 运算符优先级层级较多，逐层写函数会让调用栈过深、代码重复且难以维护；优先级爬升用一个带 `MinPrec` 参数的循环统一处理所有二元运算符，调用栈更浅、扩展更方便（新增运算符只需调整优先级表 `getBinOpPrecedence`）。

**练习 3**：`ParseRHSOfBinaryExpression` 中 `if (NextTokPrec < MinPrec) return LHS;` 这一行的作用是什么？

> **参考答案**：它是爬升算法的“刹车”——当遇到的下一个运算符优先级低于当前被允许解析的最低优先级时，就停止合并、把已构造的左部返回给上层调用者，从而保证运算符按正确优先级结合。

---

### 4.3 AST 节点体系：Decl 与 Stmt/Expr

#### 4.3.1 概念说明

Parser 把 Token 流交给 Sema 后，Sema 构造出来的就是 AST。AST 里几乎一切节点都归入两大谱系：

- **`Decl`（声明）家族**：表示“引入了一个名字”，如变量 `VarDecl`、函数 `FunctionDecl`、记录（struct/class）`RecordDecl`、翻译单元 `TranslationUnitDecl`。
- **`Stmt`（语句）家族**：表示“做了一件事”，如复合语句 `CompoundStmt`、`IfStmt`、`ReturnStmt`、声明语句 `DeclStmt`。**表达式 `Expr` 是 `Stmt` 的子类**——因为“表达式语句”（如 `a + b;`）本身就是语句，类型上让 `Expr` 继承 `Stmt` 可以统一处理。

还有两个“容器/环境”概念：

- **`DeclContext`（声明上下文）**：通过多重继承混入某些 `Decl`，表示“这个声明内部可以包含其他声明”。例如 `FunctionDecl` 既是 `Decl` 又是 `DeclContext`（函数体内可以有局部变量声明），`TranslationUnitDecl`、`RecordDecl`、`NamespaceDecl` 同理。
- **`ASTContext`（AST 上下文）**：所有 AST 节点的“内存竞技场”（arena allocator）和全局信息持有者。AST 节点不能用普通 `new` 分配，必须经 `ASTContext::Allocate` 申请；`ASTContext` 还持有翻译单元根 `TranslationUnitDecl`，是整棵 AST 的入口。

#### 4.3.2 核心流程

AST 的两大继承谱系（简化）：

```text
Decl 谱系（DeclBase.h / Decl.h）
Decl                                   ← 根，带 Kind 枚举
├── NamedDecl                          ← 有名字
│   └── ValueDecl                      ← 有类型
│       └── DeclaratorDecl
│           ├── VarDecl                ← 变量
│           └── FunctionDecl           ← 函数（同时是 DeclContext）
├── DeclContext（多重继承混入）         ← “我内部能装声明”
└── TranslationUnitDecl                ← 翻译单元根（同时是 DeclContext）

Stmt 谱系（Stmt.h / Expr.h）
Stmt                                   ← 根，带 StmtClass 枚举、children() 接口
├── ValueStmt
│   └── Expr                           ← 表达式是语句！
│       ├── IntegerLiteral
│       ├── DeclRefExpr                ← 引用一个 Decl（变量名）
│       └── BinaryOperator             ← a + b
├── CompoundStmt                       ← { 语句列表 }
├── ReturnStmt                         ← return ...
└── DeclStmt                           ← 一条声明作为语句
```

两条贯穿全树的设计：

1. **每个节点带一个枚举 ID**（`Decl::Kind`、`Stmt::StmtClass`），由 `clang/AST/DeclNodes.inc`、`StmtNodes.inc` 这两张表展开生成。这些 ID 是 `isa<>`/`dyn_cast<>`（LLVM 的 RTTI）判别节点类型的基础，也支撑 `-ast-dump` 打印节点名。
2. **统一的子节点遍历**：`Stmt` 提供 `children()` 返回子语句/子表达式范围；`DeclContext` 提供 `decls_begin()/decls_end()` 遍历内部声明。这让“遍历整棵 AST”可以用统一算法实现。

#### 4.3.3 源码精读

**Decl 根类与 Kind 枚举**：

[clang/include/clang/AST/DeclBase.h:86-97](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/include/clang/AST/DeclBase.h#L86-L97) —— `class alignas(8) Decl`，内含 `enum Kind { ... }`，该枚举由 `#include "clang/AST/DeclNodes.inc"` 展开：每种子类声明类型对应一个枚举值，构成 `isa<FunctionDecl>(d)` 这类判断的依据。

声明在 `DeclContext` 里的链表指针——这是“上下文包含声明”的物理实现：

[clang/include/clang/AST/DeclBase.h:251-260](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/include/clang/AST/DeclBase.h#L251-L260) —— `NextInContextAndBits` 把同一个 `DeclContext` 内的声明串成单向链表；注释明确指出 `decls_begin()/decls_end()` 就是遍历这条链表。

`DeclContext` 本身的定义：

[clang/include/clang/AST/DeclBase.h:1466](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/include/clang/AST/DeclBase.h#L1466) —— `class DeclContext`。它以多重继承的方式混入需要“内部装声明”的 `Decl` 子类。

带名字的声明 `NamedDecl`：

[clang/include/clang/AST/Decl.h:274-304](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/include/clang/AST/Decl.h#L274-L304) —— `NamedDecl : public Decl` 持有 `DeclarationName Name`，并提供 `getIdentifier()`、`getName()`。绝大多数我们在源码里“看到名字”的声明都是 `NamedDecl`。

`FunctionDecl` 的多重继承——它既是声明，又是声明上下文：

[clang/include/clang/AST/Decl.h:2027-2029](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/include/clang/AST/Decl.h#L2027-L2029) —— `class FunctionDecl : public DeclaratorDecl, public DeclContext, public Redeclarable<FunctionDecl>`。三重继承说明：函数是一个声明（`DeclaratorDecl`→`ValueDecl`→`NamedDecl`→`Decl`），同时是一个能容纳局部声明的 `DeclContext`，并且可被重复声明（`Redeclarable`，支持声明与定义分离）。

**Stmt 根类与 StmtClass 枚举**：

[clang/include/clang/AST/Stmt.h:85-96](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/include/clang/AST/Stmt.h#L85-L96) —— `class alignas(void *) Stmt`，内含 `enum StmtClass { ... }`，由 `#include "clang/AST/StmtNodes.inc"` 展开，机制与 `Decl::Kind` 完全对称。

Stmt 不能用普通 `new`：

[clang/include/clang/AST/Stmt.h:103-109](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/include/clang/AST/Stmt.h#L103-L109) —— 普通 `operator new` 被声明为 `llvm_unreachable`，强制 AST 节点必须经 `ASTContext` 分配。`Decl` 同样如此。这保证所有节点生于同一个竞技场，析构时整体回收，无需逐个 delete。

统一的子节点遍历接口：

[clang/include/clang/AST/Stmt.h:1585-1601](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/include/clang/AST/Stmt.h#L1585-L1601) —— 注释要求“所有子类必须实现 `children()`”，并提供 `child_range`/`child_begin()`/`child_end()`。这意味着只要实现一个递归访问 `children()` 的访客，就能遍历任意 `Stmt` 子树。

`DeclStmt` 如何把声明包成语句——它的 `children()` 直接返回声明组：

[clang/include/clang/AST/Stmt.h:1673-1676](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/include/clang/AST/Stmt.h#L1673-L1676) —— `DeclStmt::children()` 用 `child_iterator(DG.begin(), DG.end())` 把内部的 `DeclGroup` 暴露为子节点。这正是“一条声明语句”在 AST 里同时横跨 `Stmt` 与 `Decl` 两个谱系的体现。

`Expr` 继承自 `Stmt`：

[clang/include/clang/AST/Expr.h:112](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/include/clang/AST/Expr.h#L112) —— `class Expr : public ValueStmt`，而 `ValueStmt : public Stmt`，因此 `Expr` **是一个** `Stmt`。这就是为什么表达式语句、`return` 的返回值等都能用同一套 `Stmt` 接口处理。

`BinaryOperator` 与 `IntegerLiteral` 作为典型表达式节点：

[clang/include/clang/AST/Expr.h:1516](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/include/clang/AST/Expr.h#L1516) 与 [clang/include/clang/AST/Expr.h:4044](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/include/clang/AST/Expr.h#L4044) —— `IntegerLiteral`（整数字面量，内含 `APInt`）与 `BinaryOperator`（二元运算，持有左右两个 `Expr*` 与操作码）。它们正是上一节 `Actions.ActOnBinOp` 构造出来的节点类型。

**ASTContext 作为内存竞技场与 TU 根持有者**：

[clang/include/clang/AST/ASTContext.h:882](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/include/clang/AST/ASTContext.h#L882) —— `void *Allocate(size_t Size, unsigned Align = 8)`，所有 AST 节点经它分配。

[clang/include/clang/AST/ASTContext.h:752](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/include/clang/AST/ASTContext.h#L752) 与 [clang/include/clang/AST/ASTContext.h:1303](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/clang/include/clang/AST/ASTContext.h#L1303) —— `ASTContext` 持有 `TranslationUnitDecl *TUDecl` 并通过 `getTranslationUnitDecl()` 暴露，它是整棵 AST 的根：所有顶层声明都是它的子声明。

#### 4.3.4 代码实践

**实践目标**：把 `int add(int a, int b) { return a + b; }` 这段代码“手工翻译”成 AST 节点，对照源码类名加深理解。

**操作步骤**：

1. 对照本节的谱系图，为示例函数列出节点清单（顶层开始）：
   - `TranslationUnitDecl`（AST 根，由 `ASTContext` 持有）
     - `FunctionDecl` 名为 `add`（同时是 `DeclContext`，返回类型 `int`）
       - 参数：`ParmVarDecl`（`VarDecl` 子类）`a`、`b`
       - 函数体：`CompoundStmt`（`{ ... }`）
         - `ReturnStmt`
           - `BinaryOperator`（`+`）
             - 左：`DeclRefExpr` 指向 `ParmVarDecl a`
             - 右：`DeclRefExpr` 指向 `ParmVarDecl b`
2. 对每个节点，在源码里确认它的类名出现在 `Decl.h`/`Stmt.h`/`Expr.h`（如 `FunctionDecl` 在 Decl.h:2027，`BinaryOperator` 在 Expr.h:4044）。
3. 思考：`a` 这个标识符在 AST 里如何与参数声明 `a` 关联？（提示：`DeclRefExpr` 内部存有指向被引用 `ValueDecl` 的指针，这就是名字解析的结果。）

**需要观察的现象**：`ReturnStmt` 的子节点（通过 `children()`）是一个 `BinaryOperator`；`BinaryOperator` 的两个子节点分别是两个 `DeclRefExpr`。

**预期结果**：你能在一张纸上画出这棵以 `FunctionDecl` 为根的小 AST，并标注每个节点的 C++ 类名。本练习可与下一节“综合实践”的 `-ast-dump` 输出相互印证（**待本地验证**：实际 `-ast-dump` 输出还包含类型、源位置等更丰富的信息）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `FunctionDecl` 要同时继承 `Decl` 路线与 `DeclContext`？

> **参考答案**：函数本身是一个声明（有名字、有类型、可被引用），所以继承 `Decl` 家族；同时函数体内部可以包含局部变量等声明，需要“声明上下文”的能力来管理这些内部声明，因此又多重继承 `DeclContext`。这种“既是节点又是容器”的设计在 `TranslationUnitDecl`、`RecordDecl`、`NamespaceDecl` 上同样存在。

**练习 2**：`Expr` 继承自 `Stmt` 带来了什么好处？

> **参考答案**：许多语法位置既允许语句也允许表达式（如表达式语句、`return` 的操作数、`if` 的条件）。让 `Expr` 继承 `Stmt` 后，这些位置可以统一用 `Stmt*` 接口处理，遍历子节点的 `children()` 也能同时覆盖语句与表达式，避免了大量重复代码。

**练习 3**：为什么 AST 节点的普通 `operator new` 被禁用，必须走 `ASTContext::Allocate`？

> **参考答案**：编译过程中会构造海量 AST 节点，逐个 `new`/`delete` 既慢又容易内存碎片；统一用 `ASTContext` 这个竞技场批量分配、整体回收，性能更好、生命周期管理更简单（编译完一个 TU 一次性释放），也便于统计内存占用。

---

## 5. 综合实践

**任务**：用 `clang -Xclang -ast-dump` 真正观察一段 C 代码的 AST，并把本讲三个模块的知识串起来验证。

**准备文件** `demo.c`：

```c
int add(int a, int b) {
  int sum = a + b;
  return sum;
}
```

**操作步骤**：

1. 执行 AST 转储（无需链接，只看前端产物）：
   ```bash
   clang -Xclang -ast-dump -fsyntax-only demo.c
   ```
   > 说明：`-Xclang -ast-dump` 把 `-ast-dump` 这个 cc1 层选项透传给前端（参考 u5-l1 讲过的 driver/cc1 两层结构）；`-fsyntax-only` 让编译停在语法/语义分析之后，不生成代码。
2. 在输出中按缩进层次辨认节点，重点关注：
   - 顶层 `TranslationUnitDecl`。
   - `FunctionDecl` 一行：记下它的名字 `add`、返回类型、参数 `ParmVarDecl`。
   - 函数体 `CompoundStmt` 下的两条语句。
   - 第一条是 `DeclStmt`，里面是 `VarDecl` `sum`，带初始化器 `BinaryOperator`（`+`），其左右是两个 `DeclRefExpr`。
   - 第二条是 `ReturnStmt`，操作数是引用 `sum` 的 `DeclRefExpr`。
3. 对照 4.3.4 你手画的 AST，逐节点核对类名与父子关系。

**需要观察的现象与预期结果**：

- 输出是一棵带缩进的树，每个节点行首是节点类名（如 `FunctionDecl`、`CompoundStmt`、`BinaryOperator`），后面跟着源码片段、类型、源位置等信息。
- 你应能确认：`DeclStmt` 把 `VarDecl` 包成了语句；`BinaryOperator` 的子节点是两个 `DeclRefExpr`；`DeclRefExpr` 通过引用指向对应的 `ParmVarDecl`/`VarDecl`。
- 每个节点带源位置（如 `<col:x, col:y>`），印证“AST 节点记录了它在源码中的位置”，这是诊断信息与 FixIt 的基础。

**进阶**：把函数改成含 `if`/`while` 的版本，再次 `-ast-dump`，辨认 `IfStmt`/`WhileStmt` 及其条件表达式子节点，体会 4.2 讲的“关键字分支分发”是如何对应到这些 `Stmt` 子类的。（**待本地验证**：不同 Clang 版本的 `-ast-dump` 文本格式可能有细微差异，但节点类名与层次稳定。）

## 6. 本讲小结

- `clang::ParseAST` 是语法分析总入口：它构造 `Sema` 与 `Parser`，用 `for` 循环反复调用 `ParseTopLevelDecl`，每解析出一个顶层声明就经 `ASTConsumer` 推送下游。
- Parser 与 Sema 严格分工：Parser（持有 `Preprocessor &` 与 `Sema &Actions`）只认 Token 和文法，真正的 AST 构造与语义检查由 `Actions.ActOnXxx`/`BuildXxx` 完成。
- Clang 的 Parser 是**手写递归下降**：每条文法规则对应一个 `Parse...` 方法，方法内用 `switch (Tok.getKind())` 按首 Token 分发，层层下降到语句、表达式。
- 表达式层用**优先级爬升**（`ParseRHSOfBinaryExpression` 的 `while` 循环 + `MinPrec` 参数）统一处理所有二元运算符，避免为每个优先级写一层函数。
- AST 节点分两大谱系：`Decl`（声明）与 `Stmt`（语句），且 `Expr` 继承自 `Stmt`；每个节点带枚举 ID（`Decl::Kind`/`Stmt::StmtClass`），支撑 `isa<>`/`dyn_cast<>` 与 `-ast-dump`。
- `DeclContext`（多重继承混入）让 `FunctionDecl`、`TranslationUnitDecl` 等既“是声明”又“能装声明”；`ASTContext` 是所有节点的内存竞技场，并持有翻译单元根。

## 7. 下一步学习建议

本讲到“Parser 把结构交给 Sema、Sema 构造出 AST”为止，但**没有展开 Sema 内部做了什么**——它如何查名字、做类型检查、重载决议、模板实例化。这些正是下一讲 **u5-l4 语义分析 Sema** 的主题，建议紧接着学习，重点对照 `clang/lib/Sema/SemaExpr.cpp`、`SemaDecl.cpp` 理解 `ActOnBinOp`、`ActOnVarDecl` 等“动作”的内部实现。

若你想提前看到 AST 如何变成 LLVM IR，可以跳到 **u5-l5 CodeGen：从 AST 到 LLVM IR**，看 `CodeGenModule`/`CodeGenFunction` 如何遍历本讲构建的 `Decl`/`Stmt` 树生成 IR。

继续阅读建议：在源码中通读一遍 `clang/lib/Parse/ParseStmt.cpp` 里 `ParseStatement` 系列，亲手跟踪一条更复杂的语句（如嵌套 `if`），以巩固对“递归下降调用栈”的直觉。
