# Kaleidoscope 教程导览：从源码到 IR

## 1. 本讲目标

本讲是整个学习手册的「总览篇」。我们不会深入 LLVM 的任何单一机制，而是借 LLVM 官方自带的 **Kaleidoscope** 教程，把一个语言从源码到执行的**完整主线**先在脑子里走一遍。

学完本讲，你应该能够：

- 说出 Kaleidoscope 教程的 **10 个章节** 各自做了什么，以及它们如何递进。
- 在源码中辨认出一个最小语言前端包含的几个阶段：**Lexer（词法分析）→ Parser（语法分析）→ AST（抽象语法树）→ Codegen（生成 IR）**。
- 建立从 AST 到 LLVM IR 的代码生成直觉：理解 `codegen()` 这种「每个 AST 节点自己负责发 IR」的设计。
- 知道如何把 `examples/Kaleidoscope` 编译出来并亲手运行，看到一段真实生成的 `.ll` 风格 IR 输出。

> 本讲只做「导览」，为后续 u3（IR 数据结构）、u4（Pass 优化）、u5（Clang 前端）、u8（JIT）等单元埋下伏笔。每个具体机制都会在后续讲义里展开。

---

## 2. 前置知识

阅读本讲前，请确认你已掌握（来自 u1、u2）：

- **三段式编译器**：前端（源码→IR）、中端/优化器（IR→优化后 IR）、后端（IR→机器码）。参见讲义 u2-l1。
- **LLVM IR 的三种形态**：内存中的 `Module` 对象、人类可读的 `.ll` 文本、紧凑的 `.bc` 位码。参见讲义 u1-l4。
- **基本 IR 语法**：能看懂一段简单 `.ll` 里的函数、基本块、`%` 寄存器、SSA。参见讲义 u2-l2。
- **目录与构建**：知道 `llvm/examples/` 存放示例，知道 CMake 构建 LLVM 的基本流程。参见讲义 u1-l2、u1-l3。

本讲还会用到几个新术语，先在此解释：

- **Lexer（词法分析器 / scanner）**：把字符流切成一个个 **Token（记号）**，例如把 `def fib(x)` 切成 `def`、`fib`、`(`、`x`、`)`。
- **Parser（语法分析器）**：按语法规则把 Token 流组织成 **AST（抽象语法树）**。
- **AST 节点**：树上的一个对象，代表一段语法结构（如「一个数字」「一次加法」「一次函数调用」）。
- **codegen（代码生成）**：把 AST 翻译成 LLVM IR 的过程。
- **递归下降解析 / 运算符优先级解析**：两种常用的手写解析技术，Kaleidoscope 同时用到了它们。
- **REPL**：交互式「读取—求值—打印」循环，Kaleidoscope 运行后会显示 `ready>` 提示符等待你输入。

---

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [llvm/examples/Kaleidoscope/Chapter2/toy.cpp](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/examples/Kaleidoscope/Chapter2/toy.cpp) | **Lexer + AST + Parser** 的最小实现（约 446 行），还不含 IR 生成。本讲用来观察语言前端的前半段。 |
| [llvm/examples/Kaleidoscope/Chapter3/toy.cpp](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/examples/Kaleidoscope/Chapter3/toy.cpp) | 在第 2 章基础上**新增 Codegen**，能把 AST 变成 LLVM IR 并打印出来（约 625 行）。本讲「AST→IR」主线的主要阅读对象。 |
| [llvm/docs/tutorial/MyFirstLanguageFrontend/index.rst](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/docs/tutorial/MyFirstLanguageFrontend/index.rst) | 官方教程**目录**，列出全部 10 章，是了解章节结构的权威入口。 |
| [llvm/docs/tutorial/MyFirstLanguageFrontend/LangImpl01.rst](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/docs/tutorial/MyFirstLanguageFrontend/LangImpl01.rst) | 第 1 章：语言介绍 + Lexer，给出 Kaleidoscope 语言的样子与示例。 |

> 说明：规格里给出的「关键源码」是 Chapter2 的 `toy.cpp` 与 `LangImpl01.rst`。但 Chapter2 **只到 Parser 为止、并不生成 IR**。为了真正讲清「AST 到 IR 的主线」，本讲额外引用了同目录下的 **Chapter3/toy.cpp**——它正是在第 2 章基础上加上了 Codegen。这样既忠于规格指定的文件，又能让读者真的看到 IR。

---

## 4. 核心概念与源码讲解

本讲包含两个最小模块：

1. **Kaleidoscope 章节结构**：理解官方教程的 10 章递进关系。
2. **AST 到 IR 的主线**：理解 `codegen()` 设计，看一段 AST 是如何变成 LLVM IR 的。

### 4.1 Kaleidoscope 章节结构

#### 4.1.1 概念说明

**Kaleidoscope**（取「美、形、观」之意）是 LLVM 官方为教学设计的一门玩具语言。它的特点被刻意简化到极致，目的是让你把注意力放在「编译器各阶段」而非语言本身：

- 它是过程式语言，可定义函数、写条件与循环。
- **唯一的数据类型是 64 位浮点数 `double`**，所有值隐式为 `double`，因此**不需要类型声明**。
- 关键字只有两个：`def`（定义函数）、`extern`（声明外部函数，如调用 C 标准库的 `sin`/`cos`）。

官方教程的精髓在于**迭代式构建**：不是一开始就给你一个完整编译器，而是分 10 章，每章只往前推进一步。官方明确说，全部讲完「不到 1000 行非空非注释代码」，就能得到一个包含手写 Lexer、Parser、AST、静态与 JIT 代码生成的非平凡小语言。

#### 4.1.2 核心流程

10 章的主线如下（每章在前一章代码上增量修改，所以每章都有一个完整的 `toy.cpp`）：

```text
Ch1  语言介绍 + Lexer（词法分析）
Ch2  Parser + AST（递归下降 + 运算符优先级）   ← 本讲精读的 Chapter2
Ch3  生成 LLVM IR（Codegen）                  ← 本讲精读的 Chapter3
Ch4  加入 JIT 与优化器（让代码能立即运行）
Ch5  扩展控制流：if/then/else、for 循环（引出 SSA 构造）
Ch6  用户自定义运算符（可指定优先级）
Ch7  可变变量与赋值（证明前端无需自己构造 SSA）
Ch8  编译为目标文件（.o，静态编译）
Ch9  调试信息（支持断点、查看变量）
Ch10 总结与延伸主题（GC、异常等）
```

把这条主线和我们前面学过的「三段式」对应起来：

- Ch1~Ch3 属于**前端**：源码 → Lexer → Parser → AST → IR。
- Ch4 的优化器属于**中端**：IR → 优化后的 IR（这部分正是 u4 要讲的 Pass）。
- Ch8 的目标文件属于**后端**：IR → 机器码（这部分正是 u6 要讲的后端流水线）。
- Ch4 的 JIT 属于**执行引擎**（u8 会专门讲 ORC JIT）。

> 也就是说，Kaleidoscope 教程本身就是我们这本学习手册后续各单元的「缩微预演」。本讲的任务只是先看清这条线。

#### 4.1.3 源码精读

**(1) 语言长什么样——第 1 章的斐波那契示例**

官方在第 1 章就给出了 Kaleidoscope 的样貌，下面是计算斐波那契数的例子：

```text
# Compute the x'th fibonacci number.
def fib(x)
  if x < 3 then
    1
  else
    fib(x-1)+fib(x-2)

# This expression will compute the 40th number.
fib(40)
```

这段示例出自教程文档 [LangImpl01.rst:L24-L34](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/docs/tutorial/MyFirstLanguageFrontend/LangImpl01.rst#L24-L34)，它展示了：`#` 开头是注释、`def` 定义函数、函数体是一个表达式（这里用了 `if/then/else`，Ch5 才实现，第 1 章先画出愿景）、支持递归调用 `fib(...)`。同一文档还展示了用 `extern` 声明后调用 `sin`/`cos`/`atan2`（[LangImpl01.rst:L42-L47](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/docs/tutorial/MyFirstLanguageFrontend/LangImpl01.rst#L42-L47)）。

**(2) 10 章目录——index.rst**

完整章节清单见 [index.rst:L33-L75](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/docs/tutorial/MyFirstLanguageFrontend/index.rst#L33-L75)。例如其中对第 3、4 章的一句话描述：

- Ch3：*Code generation to LLVM IR — with the AST ready, we show how easy it is to generate LLVM IR.*
- Ch4：*Adding JIT and Optimizer Support — One great thing about LLVM is its support for JIT compilation...*

index 还特别提醒：教程代码**故意违反软件工程最佳实践**（全局变量满天飞、不用 visitor 模式等），只为把焦点集中在编译技术与 LLVM 本身（[index.rst:L22-L28](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/docs/tutorial/MyFirstLanguageFrontend/index.rst#L22-L28)）。读源码时请带着这个前提。

**(3) Chapter2 的代码分区**

打开 [Chapter2/toy.cpp](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/examples/Kaleidoscope/Chapter2/toy.cpp)，你会看到文件被几条 `//===---===//` 注释条清晰地分成几大段。这正好对应一个语言前端的各个阶段：

| 代码段 | 行范围 | 对应阶段 |
| --- | --- | --- |
| `// Lexer` | L10–L80 | 词法分析 |
| `// Abstract Syntax Tree (aka Parse Tree)` | L82–L157 | AST 节点定义 |
| `// Parser` | L159–L369 | 语法分析 |
| `// Top-Level parsing` | L371–L424 | 顶层 REPL 循环 |
| `// Main driver code.` | L426–L446 | 程序入口 |

> 注意：Chapter2 里**没有任何 `llvm/IR` 的头文件**，也没有 `codegen`。它的 `HandleDefinition()` 只是 `fprintf(stderr, "Parsed a function definition.\n")`（见 [Chapter2/toy.cpp:L375-L382](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/examples/Kaleidoscope/Chapter2/toy.cpp#L375-L382)）。也就是说，第 2 章能「认出」你输入的函数，但还不会生成任何 IR——这正是第 3 章要做的事。

#### 4.1.4 代码实践

**实践目标**：在源码层面确认 Chapter2 只包含 Lexer / AST / Parser 三个阶段，且不产出 IR。

**操作步骤**：

1. 打开 [Chapter2/toy.cpp](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/examples/Kaleidoscope/Chapter2/toy.cpp)。
2. 浏览文件顶部的 `#include`（[L1-L8](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/examples/Kaleidoscope/Chapter2/toy.cpp#L1-L8)），确认其中**没有**任何 `llvm/IR/...` 头文件。
3. 找到三条分隔注释 `//===---===//`，记下它们各自对应的段落标题。
4. 找到 `HandleDefinition`、`HandleExtern`、`HandleTopLevelExpression`（[L375-L401](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/examples/Kaleidoscope/Chapter2/toy.cpp#L375-L401)），观察它们解析成功后**只是打印一行字符串**，没有调用任何生成 IR 的函数。

**需要观察的现象**：整份文件是一段纯 C++ 标准库代码（`<cstdio>`、`<map>`、`<memory>` 等），与 LLVM 库完全无关；它能独立编译，运行后是一个只做「解析并回显」的 REPL。

**预期结果**：你能用一句话总结——「Chapter2 = Lexer + AST + Parser，无 Codegen」。

> 是否真的运行：本步骤是源码阅读型实践，无需运行即可得出结论。若你想运行它，方法见 4.2.4。

#### 4.1.5 小练习与答案

**练习 1**：Kaleidoscope 只有一种数据类型，是什么？为什么这样设计？

> **答案**：唯一类型是 64 位浮点 `double`。这样设计的目的是让教学代码极度简化——所有值都是 `double`，语言就**不需要类型声明、不需要类型检查**，读者可以把全部注意力放在「词法/语法/代码生成」这些编译器核心阶段上（见 [LangImpl01.rst:L17-L23](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/docs/tutorial/MyFirstLanguageFrontend/LangImpl01.rst#L17-L23)）。

**练习 2**：对照 [index.rst](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/docs/tutorial/MyFirstLanguageFrontend/index.rst) 的章节列表，哪一章开始加入「IR 优化」？哪一章开始能产出「目标文件（.o）」？

> **答案**：Ch4（*Adding JIT and Optimizer Support*）加入优化器；Ch8（*Compiling to Object Files*）开始编译为目标文件。

---

### 4.2 AST 到 IR 的主线

#### 4.2.1 概念说明

第 2 章结束时，我们手里有一棵 AST（用 C++ 类表示的语法树），但它和 LLVM 还毫无关系。第 3 章的任务是：**把这棵 AST 翻译成 LLVM IR**。

Kaleidoscope 采用了一种极其直观的设计，叫做 **「每个 AST 节点自己会生成 IR」**：

- 给每个 AST 类（`NumberExprAST`、`BinaryExprAST`、`CallExprAST`、`FunctionAST` 等）添加一个虚函数 `codegen()`。
- `codegen()` 的职责是：**「发出我这个节点对应的 IR，并返回表示结果的那个 IR 值」**。
- 由于 AST 是树，对根节点调用一次 `codegen()`，就会**递归地**触发所有子节点的 `codegen()`，最终整棵树都被翻译成 IR。

`codegen()` 返回的类型是 `llvm::Value *`。`Value` 是 LLVM IR 中「几乎一切对象的基类」——一个 SSA 值（一个被计算出来、不可变的结果）。这个类我们在 u3-l2 会专门精读，这里只需知道：**每条指令的产物就是一个 `Value`**。

要发 IR，还需要几样「工具」，它们在第 3 章被定义成全局对象：

- `LLVMContext`：IR 对象所属的上下文（可以理解为 IR 对象的「容器/工厂」）。
- `Module`：一个完整的 IR 模块，装着所有函数与全局变量（对应一个 `.ll` 文件）。
- `IRBuilder`：用来**逐条构造指令**的便捷工具（创建加法、调用、返回等指令都靠它）。
- `NamedValues`：当前函数参数的名字→`Value` 映射，用来在生成变量引用时查到对应的 SSA 值。

这四个对象分别属于 u3 单元（Module/Value/Type）与 u3-l4（IRBuilder）的内容，本讲只用它们的「直觉」用法。

#### 4.2.2 核心流程

从用户输入到看到 IR，整体流程是：

```text
用户在 REPL 输入:  def foo(a) a + 1
        │
        ▼  Lexer
   Token 流:  def  foo  (  a  )  a  +  1
        │
        ▼  Parser（递归下降 + 运算符优先级）
   AST:  FunctionAST
           └─ PrototypeAST(foo, [a])
           └─ BinaryExprAST('+',
                ├─ VariableExprAST(a)
                └─ NumberExprAST(1.0))
        │
        ▼  FunctionAST::codegen()  ← 第 3 章新增，递归触发子节点
   LLVM IR（内存 Module 对象）:
        define double @foo(double %a) {
          entry:
            %addtmp = fadd double %a, 1.000000e+00
            ret double %addtmp
        }
        │
        ▼  FnIR->print(errs())   ← 把内存 Module 打印成文本
   终端上看到这段 .ll 文本
```

关键点：`FunctionAST::codegen()` 内部会先建函数与入口基本块，再调用 `Body->codegen()`（即那个 `BinaryExprAST` 的 `codegen`），后者又会调用左右子节点的 `codegen`——**递归就这样自然发生了**。最后用 `CreateRet` 收尾、用 `verifyFunction` 校验 IR 合法性。

#### 4.2.3 源码精读

以下全部出自 [Chapter3/toy.cpp](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/examples/Kaleidoscope/Chapter3/toy.cpp)。

**(1) 引入 LLVM 头并打开命名空间**

相比 Chapter2，Chapter3 顶部多了大量 `llvm/IR/...` 头文件与一句 `using namespace llvm;`：

[Chapter3/toy.cpp:L1-L21](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/examples/Kaleidoscope/Chapter3/toy.cpp#L1-L21) —— 引入 `Module`、`Function`、`IRBuilder`、`LLVMContext`、`Verifier` 等 IR 相关头文件，并 `using namespace llvm` 以便直接写 `Value`、`Function` 等短名。这是「接入 LLVM」的标志。

**(2) 给 AST 类挂上 codegen 虚函数**

基类 `ExprAST` 多了一个纯虚函数，每个子类各自 `override`：

[Chapter3/toy.cpp:L102-L107](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/examples/Kaleidoscope/Chapter3/toy.cpp#L102-L107) —— `virtual Value *codegen() = 0;` 声明「每个表达式节点都能发 IR，结果是一个 `Value`」。这正是「节点自生成 IR」设计的契约。

**(3) 四个全局工具对象**

[Chapter3/toy.cpp:L402-L405](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/examples/Kaleidoscope/Chapter3/toy.cpp#L402-L405) —— 定义 `TheContext`（上下文）、`TheModule`（模块）、`Builder`（指令构造器）、`NamedValues`（变量名→值表）。它们在 `InitializeModule()` 里被创建（[L524-L531](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/examples/Kaleidoscope/Chapter3/toy.cpp#L524-L531)），其中 `TheModule` 被命名为 `"my cool jit"`。

**(4) 叶子节点：数字**

[Chapter3/toy.cpp:L412-L414](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/examples/Kaleidoscope/Chapter3/toy.cpp#L412-L414) —— `NumberExprAST::codegen()` 用 `ConstantFP::get` 直接返回一个浮点常量 `Value`。叶子节点不依赖别的节点，最简单。

**(5) 二元运算：用 IRBuilder 发指令**

[Chapter3/toy.cpp:L424-L444](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/examples/Kaleidoscope/Chapter3/toy.cpp#L424-L444) —— `BinaryExprAST::codegen()` 先递归算出左右操作数 `L`、`R`，再按运算符用 `Builder` 创建对应 IR 指令：`+` → `CreateFAdd`、`-` → `CreateFSub`、`*` → `CreateFMul`、`<` → `CreateFCmpULT`（再把 bool 转成 double）。这里能清楚看到「递归 + 用 Builder 发指令」的模式。

**(6) 函数调用**

[Chapter3/toy.cpp:L446-L464](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/examples/Kaleidoscope/Chapter3/toy.cpp#L446-L464) —— `CallExprAST::codegen()` 在 `TheModule` 中按名字查到被调函数，逐个递归生成实参，最后 `Builder->CreateCall` 发出一条 `call` 指令。

**(7) 函数原型与函数体**

[Chapter3/toy.cpp:L466-L481](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/examples/Kaleidoscope/Chapter3/toy.cpp#L466-L481) —— `PrototypeAST::codegen()` 构造函数类型 `double(double, ...)` 并 `Function::Create` 在模块里新建一个函数，给参数取名。

[Chapter3/toy.cpp:L483-L518](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/examples/Kaleidoscope/Chapter3/toy.cpp#L483-L518) —— `FunctionAST::codegen()` 是主线中的主线：建入口基本块 `entry`、`Builder->SetInsertPoint(BB)` 设定插入点、把参数登记进 `NamedValues`、调用 `Body->codegen()` 递归生成函数体、`Builder->CreateRet(RetVal)` 发返回指令、最后 `verifyFunction` 校验；若函数体出错则 `eraseFromParent` 回滚。

**(8) 把 IR 打印到终端**

[Chapter3/toy.cpp:L533-L544](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/examples/Kaleidoscope/Chapter3/toy.cpp#L533-L544) —— `HandleDefinition()` 在解析并 `codegen()` 成功后，调用 `FnIR->print(errs())` 把生成的函数 IR 直接打印到 stderr。这就是你在终端看到 `.ll` 文本的来源。

[Chapter3/toy.cpp:L603-L625](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/examples/Kaleidoscope/Chapter3/toy.cpp#L603-L625) —— `main` 里调用 `InitializeModule()` 建好模块，REPL 结束后再 `TheModule->print(errs(), nullptr)` 把整个模块（所有已定义函数）汇总打印一遍。

> 小结：第 3 章相对第 2 章，**只是给每个 AST 节点加了 `codegen()`，并引入了 `Module`/`IRBuilder` 等几个对象**。 Lexer 和 Parser 几乎原封不动——这正说明「前端解析」与「IR 生成」是可以干净解耦的两层。

#### 4.2.4 代码实践

**实践目标**：亲手把 Chapter3 编译并运行，输入一段 Kaleidoscope 代码，观察它生成的 LLVM IR 文本。

> ⚠️ 关于规格里的实践描述：规格写的是「阅读 **Chapter2** 的 toy.cpp……并运行它生成一段 IR 输出」。但如前所述，Chapter2 并不生成 IR。要看到 IR 输出，必须使用 **Chapter3**。因此本实践分为两步：先用 Chapter2 做源码阅读（已在 4.1.4 完成），再用 Chapter3 做运行实践。

**操作步骤（运行型实践）**：

1. 按讲义 u1-l3 的方法配置一个 LLVM 构建目录，并额外打开示例开关：

   ```bash
   cmake -G Ninja -S llvm -B build \
         -DLLVM_BUILD_EXAMPLES=ON \
         -DLLVM_TARGETS_TO_BUILD=X86 \
         -DCMAKE_BUILD_TYPE=Release
   ```

   > 说明：`LLVM_BUILD_EXAMPLES` 默认是 **OFF**（见 [llvm/CMakeLists.txt:L907-L908](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/CMakeLists.txt#L907-L908)），不加这个开关就不会编译 `examples/`。`add_llvm_example` 宏会在该开关关闭时直接跳过（见 `llvm/cmake/modules/AddLLVM.cmake` 中 `add_llvm_example` 的实现）。

2. 只编译第 3 章这个目标（不必编完整个 LLVM）：

   ```bash
   cmake --build build --target Kaleidoscope-Ch3
   ```

   > 目标名来自 [Chapter3/CMakeLists.txt](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/examples/Kaleidoscope/Chapter3/CMakeLists.txt) 里的 `add_kaleidoscope_chapter(Kaleidoscope-Ch3 toy.cpp)`，它链接 `Core` 与 `Support` 两个 LLVM 组件。

3. 运行可执行文件（路径通常在 `build/bin/Kaleidoscope-Ch3`），在 `ready>` 提示符后输入：

   ```text
   def foo(a) a + 1;
   ```

4. 按回车后继续输入 `;`（空表达式）或直接结束输入（Ctrl-D）退出 REPL。

**需要观察的现象**：

- 输入 `def foo(a) a + 1;` 后，程序会立即打印 `Read function definition:`，紧跟着一段形如以下的 IR：

  ```text
  define double @foo(double %a) {
  entry:
    %addtmp = fadd double %a, 1.000000e+00
    ret double %addtmp
  }
  ```

- 退出 REPL 后，`main` 末尾的 `TheModule->print` 还会把整个模块再汇总打印一次。

**预期结果**：你能亲眼看到「`a + 1`」这段 AST 被翻译成了一条 `fadd` 指令和一个 `entry` 基本块，并且函数签名是 `double @foo(double %a)`——因为 Kaleidoscope 一切皆 `double`。

> 待本地验证：上述 IR 文本是基于源码逻辑（[Chapter3/toy.cpp:L424-L444](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/examples/Kaleidoscope/Chapter3/toy.cpp#L424-L444) 的 `CreateFAdd` + [L466-L481](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/examples/Kaleidoscope/Chapter3/toy.cpp#L466-L481) 的 `double` 类型）推断的典型输出；寄存器名（`%addtmp`）、常量书写形式（`1.000000e+00`）可能因 LLVM 版本而略有差异，请以你本机的实际输出为准。

#### 4.2.5 小练习与答案

**练习 1**：为什么对根节点 `FunctionAST` 调用一次 `codegen()`，就能生成整个函数体的 IR？

> **答案**：因为 `codegen()` 是递归的。`FunctionAST::codegen()` 在建好函数框架后调用 `Body->codegen()`；`Body`（如 `BinaryExprAST`）的 `codegen()` 又会调用左右子节点的 `codegen()`……如此沿 AST 自顶向下递归，每个节点只负责发出自己那部分 IR 并返回一个 `Value`，整棵树的 IR 也就自然生成了（见 [Chapter3/toy.cpp:L424-L444](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/examples/Kaleidoscope/Chapter3/toy.cpp#L424-L444) 与 [L483-L518](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/examples/Kaleidoscope/Chapter3/toy.cpp#L483-L518)）。

**练习 2**：`BinaryExprAST::codegen()` 里，运算符 `<` 的处理和 `+` 有什么不同？为什么？

> **答案**：`+` 直接用 `CreateFAdd` 发出浮点加法并返回；而 `<` 先用 `CreateFCmpULT` 得到一个 **bool（i1）** 结果，再用 `CreateUIToFP` 把它**转成 double**（0.0 或 1.0）才返回。原因是 Kaleidoscope 所有值都是 `double`，比较结果也必须表现为 `double`，所以多做一次类型转换（见 [Chapter3/toy.cpp:L437-L440](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/examples/Kaleidoscope/Chapter3/toy.cpp#L437-L440)）。

**练习 3**：`FunctionAST::codegen()` 末尾调用的 `verifyFunction` 起什么作用？如果删掉它会怎样？

> **答案**：`verifyFunction` 会对刚生成的 IR 做合法性校验（如基本块是否以终结指令结尾、SSA 是否正确等），便于在开发期尽早发现前端写错导致的非法 IR。删掉它，程序仍能运行，但你会失去一道重要的「自我检查」防线——非法 IR 可能一路流到后端才报出难以理解的错误（见 [Chapter3/toy.cpp:L509-L510](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/examples/Kaleidoscope/Chapter3/toy.cpp#L509-L510)）。

---

## 5. 综合实践

**任务**：把本讲的两个模块串起来，亲手走一遍「源码 → AST → IR」的完整阅读与运行。

1. **阅读 Chapter2**：对照 4.1.3 的代码分区表，在 [Chapter2/toy.cpp](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/examples/Kaleidoscope/Chapter2/toy.cpp) 中找到 Lexer、AST、Parser 三段，记录你认为最关键的各一个函数：
   - Lexer：`gettok`（[L32-L80](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/examples/Kaleidoscope/Chapter2/toy.cpp#L32-L80)）——逐字符切 Token。
   - Parser：`ParseBinOpRHS`（[L272-L305](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/examples/Kaleidoscope/Chapter2/toy.cpp#L272-L305)）——运算符优先级解析的核心。
   - 顶层循环：`MainLoop`（[L404-L424](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/examples/Kaleidoscope/Chapter2/toy.cpp#L404-L424)）——REPL。
2. **diff 第 2 章与第 3 章**：用 `git diff` 或直接对比两个 `toy.cpp`，确认第 3 章相对第 2 章**新增**的内容主要是：
   - 顶部的 `llvm/IR` 头与 `using namespace llvm`；
   - 每个 AST 类的 `codegen()` 方法；
   - `TheContext/TheModule/Builder/NamedValues` 四个全局对象与 `InitializeModule()`；
   - `Handle*` 函数里新增的 `FnIR->print(errs())`。
3. **运行第 3 章**（按 4.2.4 的步骤），分别输入以下两段，观察并记录 IR：
   - `def foo(a) a + 1;` —— 观察一条 `fadd`。
   - `def bar(a b) a * b + 2;` —— 观察运算符优先级如何体现在 IR 上（先 `fmul` 后 `fadd`）。
4. **画一张图**：把上面 `bar` 的输入画出对应的 AST（注意 `*` 优先级高于 `+`），并对照实际打印的 IR，验证「树的结构 → 指令顺序」的一致性。

> 如果无法本地构建，可退化为纯源码阅读型实践：依据 `BinaryExprAST::codegen()`（[L424-L444](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/examples/Kaleidoscope/Chapter3/toy.cpp#L424-L444)）与运算符优先级表（[Chapter3/toy.cpp:L604-L609](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/examples/Kaleidoscope/Chapter3/toy.cpp#L604-L609)：`<`=10、`+`/`-`=20、`*`=40），手工推导 `bar` 应生成的指令顺序，标注「待本地验证」。

---

## 6. 本讲小结

- Kaleidoscope 是 LLVM 官方的教学玩具语言：**唯一类型 `double`**，只有 `def`/`extern` 两个关键字，刻意极简以聚焦编译技术本身。
- 官方教程分 **10 章**迭代构建：Ch1 Lexer → Ch2 Parser+AST → Ch3 生成 IR → Ch4 JIT+优化 → Ch5~Ch7 扩展语言 → Ch8 目标文件 → Ch9 调试信息 → Ch10 总结。
- Chapter2 的 `toy.cpp` 只含 **Lexer / AST / Parser** 三段，**不生成 IR**；它的 `Handle*` 函数只做解析回显。
- 「AST 到 IR」的核心设计是：**给每个 AST 节点加一个 `codegen()` 虚函数**，对根节点调用一次即递归生成整棵树的 IR，结果用 `Value` 表示。
- 生成 IR 依赖四个全局对象：`LLVMContext`（上下文）、`Module`（模块）、`IRBuilder`（指令构造器）、`NamedValues`（变量名表）。
- Chapter3 用 `FnIR->print(errs())` 把内存中的 IR 打印成 `.ll` 文本，并 `verifyFunction` 自检——要让示例真正编译出来，需要 CMake 加 `-DLLVM_BUILD_EXAMPLES=ON`，目标是 `Kaleidoscope-Ch3`。

---

## 7. 下一步学习建议

本讲只是「飞过一遍」编译器全流程。接下来建议：

1. **深入 IR 本身**：进入 u3 单元。本讲里出现的 `Module`、`Function`、`BasicBlock`、`Value`、`IRBuilder`，分别在 u3-l1（IR 层次结构）、u3-l2（Value/Use/SSA）、u3-l4（IRBuilder）中精读。你会理解为什么 `codegen()` 返回的 `Value` 是「几乎所有 IR 对象的基类」。
2. **理解 IR 优化**：本讲的 Ch4 提到了「优化器」，那正是 u4 单元（Pass 管理器与优化框架）。学完 u4，你就能回到 Kaleidoscope 的 Ch4 代码，看懂它如何用一个 `FunctionPassManager` 把 IR 优化掉。
3. **看一个真实前端**：Kaleidoscope 是手写的玩具前端；u5 单元带你走进 **Clang** 这个工业级 C/C++ 前端，你会看到同样的「Lexer → Parser → AST → Codegen」主线在真实编译器里的实现。
4. **继续 Kaleidoscope 后续章节**：如果你跟着官方教程走，建议接着读 Ch4（JIT）与 Ch5（控制流/SSA），它们分别对应本手册的 u8（执行引擎）与 u3（SSA 概念）。
