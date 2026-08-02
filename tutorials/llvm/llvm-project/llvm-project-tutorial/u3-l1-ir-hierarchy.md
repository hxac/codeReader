# Module / Function / BasicBlock：IR 的层次结构

## 1. 本讲目标

本讲是「LLVM IR 核心数据结构」单元的第一讲。在前两个单元里，我们一直把 LLVM IR 当作文本（`.ll`）来读写；从本讲开始，我们要走进 IR 的**内存对象模型**——也就是 `.ll` 文件被读进内存后，到底变成了一棵怎样的对象树。

学完本讲，你应当能够：

- 说出 `Module`、`Function`、`BasicBlock`、`Instruction` 四者之间的**包含关系**（谁拥有谁），以及它们各自在 `include/llvm/IR/` 下的定义文件。
- 区分两套「层次」：**包含层次**（Module 拥有 Function 列表，Function 拥有 BasicBlock 列表……）与**继承层次**（Function、BasicBlock、Instruction 都是 `Value`）。
- 理解 LLVM 用 `SymbolTableList` 这种「带符号表的侵入式链表」来组织这些列表，既管所有权又管按名查找。
- 能用 C++ 遍历一个 `Module`：取出每个 `Function`、再取出每个 `BasicBlock`，并统计指令数量。

## 2. 前置知识

本讲承接 [u2-l2 阅读与编写 LLVM IR（.ll 文本格式）](u2-l2-read-write-ir.md)，请先确认你理解下面这些概念：

- **模块（Module）**：一个 `.ll`/`.bc` 文件在内存里对应一个 `Module`，它是所有 IR 对象的顶层容器。
- **基本块（BasicBlock）与终结指令（terminator）**：基本块是一段顺序执行的指令，末尾有且仅有一条终结指令（`ret`/`br`/`switch` 等）。
- **SSA 与 `%` 值**：每个 `%name` 只被定义一次。

此外请回忆 [u1-l4 核心命令行工具一览](u1-l4-core-tools.md) 里的一个关键结论：IR 有三种形态——内存 `Module`、`.ll` 文本、`.bc` 位码，三者以内存 `Module` 为中介互转。本讲关心的，正是这个「内存 `Module`」内部长什么样。

一个贯穿全讲的核心直觉：**`.ll` 文本的缩进结构，几乎就是内存对象树的 1:1 投影。** 下面这段 `.ll`：

```llvm
define i32 @add(i32 %a, i32 %b) {      ; ← 一个 Function
entry:                                  ; ← 一个 BasicBlock
  %s = add i32 %a, %b                   ; ← 一条 Instruction
  ret i32 %s                            ; ← 终结指令
}
```

读进内存后，`@add` 是一个 `Function` 对象，`entry` 是它内部的一个 `BasicBlock` 对象，`%s = add ...` 和 `ret` 是该块里的两条 `Instruction` 对象。本讲就是把这种直觉「对象化」。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [llvm/include/llvm/IR/Module.h](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/Module.h) | `Module` 类声明：顶层容器，定义它拥有哪些列表、如何遍历。 |
| [llvm/lib/IR/Module.cpp](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/IR/Module.cpp) | `Module` 的实现：构造/析构、按名查找、`getInstructionCount()` 等遍历逻辑。 |
| [llvm/include/llvm/IR/Function.h](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/Function.h) | `Function` 类声明：一个函数 = 基本块列表 + 参数 + 属性。 |
| [llvm/include/llvm/IR/BasicBlock.h](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/BasicBlock.h) | `BasicBlock` 类声明：指令的顺序容器，类型是 `LabelTy`。 |
| [llvm/include/llvm/IR/InstIterator.h](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/InstIterator.h) | `inst_iterator`：把「函数 → 基本块 → 指令」的两层循环拍平成一个迭代器。 |

> 说明：`Instruction` 类本身（以及 `Value`/`User`/`Use` 这条 def-use 链）是下一讲 [u3-l2 Value / User / Use：SSA 与 def-use 链](u3-l2-value-use-ssa.md) 的主题，本讲只在「包含层次」里把它当作基本块里的一条叶子节点来对待。

---

## 4. 核心概念与源码讲解

### 4.1 Module：IR 的顶层容器

#### 4.1.1 概念说明

`Module` 是 LLVM IR 对象树的**根**。官方注释一句话定位了它的角色：

> A Module instance is used to store all the information related to an LLVM module. Modules are the top level container of all other LLVM Intermediate Representation (IR) objects.

一个 `Module` 对应一个「翻译单元级别」的 IR 单位（典型情况下对应一个被编译的源文件，或在 LTO 场景下对应一组被合并的模块）。你在 `.ll` 文件里看到的一切——全局变量、函数、别名、命名元数据、`target triple`、`datalayout`——都是 `Module` 直接或间接拥有的成员。

这里有一个**贯穿全讲、必须先分清的关键区分**：LLVM 的 IR 对象同时处在两套「层次」里。

1. **包含层次（ownership / has-a）**：`Module` **拥有**一个函数列表、一个全局变量列表……函数又拥有基本块列表，基本块拥有指令列表。这是一棵严格的树：每个子节点只属于一个父节点（每个 `Function` 只有一个 `getParent()` 返回的 `Module`）。
2. **继承层次（inheritance / is-a）**：`Function` 继承自 `GlobalObject → GlobalValue → Constant → User → Value`；`BasicBlock` 直接继承自 `Value`。也就是说，函数和基本块**本身也是一个 `Value`**（是 SSA 值，可以被别的指令引用，例如分支指令引用基本块作为跳转目标）。

本讲（4.1、4.2）聚焦**包含层次**；继承层次中 `Value` 作为公共根基类的设计，留给下一讲。但你要记住：正是因为 `Function`/`BasicBlock` 也是 `Value`，它们才能被「装进」列表、被命名、被引用——这是两套层次交汇的地方。

#### 4.1.2 核心流程

`Module` 内部持有一组「列表」成员，每类 IR 顶层对象各占一个：

```
Module
 ├── FunctionList   : SymbolTableList<Function>        // 函数
 ├── GlobalList     : SymbolTableList<GlobalVariable>  // 全局变量
 ├── AliasList      : SymbolTableList<GlobalAlias>     // 别名
 ├── IFuncList      : SymbolTableList<GlobalIFunc>     // 间接函数 (ifunc)
 ├── NamedMDList    : ilist<NamedMDNode>               // 命名元数据 (!llvm.dbg.cu 等)
 ├── GlobalScopeAsm : 模块级内联汇编
 ├── ValSymTab      : ValueSymbolTable                 // 名字 → Value 的符号表
 └── Context / TargetTriple / DL / ModuleID ...        // 上下文与目标信息
```

这里有两点设计值得记住：

- **这些列表都是 `SymbolTableList<T>`**——一种「带符号表的侵入式链表（intrusive list）」。它既负责**所有权**（节点被插入即被该列表拥有，删除时回收），又负责**按名查找**：每当一个带名字的节点被插入，它会被同时登记到 `Module` 的 `ValueSymbolTable` 里，于是 `getFunction("add")`、`getNamedValue(...)` 才能成立。链表节点通过 `ilist_node` 钩子串接，并能通过 `ilist_node_with_parent` 反查到自己的 `Parent`。
- **遍历 API 高度统一**：`Module` 把 `begin()/end()` 直接绑定到函数列表（因为函数是模块最「主要」的内容），所以 `for (Function &F : M)` 就能遍历所有函数；其余对象则用专门的 range：`globals()`、`aliases()`、`ifuncs()`、`named_metadata()`。这种「主对象用 `begin/end`，其余用命名 range」的模式在 `Function`（主对象是 `BasicBlock`）、`BasicBlock`（主对象是 `Instruction`）里完全复用。

整个模块的非调试指令总数，就是各函数指令数之和，即

\[
I_{\text{module}} \;=\; \sum_{F \in \text{Module}} \; \sum_{BB \in F} |BB|
\]

这个公式不是抽象数学——它就是 `Module::getInstructionCount()` 的实现（见 4.1.3）。

#### 4.1.3 源码精读

**`Module` 类的定义与定位**。类声明前的注释写明了「顶层容器」定位，类本身就是一个普通类（注意没有默认构造函数，必须提供名字）：

- [llvm/include/llvm/IR/Module.h:L56-L67](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/Module.h#L56-L67) — 注释说明 `Module` 是所有 IR 对象的顶层容器；`class LLVM_ABI Module {` 开始类定义。注意类前那段文档强调它「directly contains a list of globals variables, a list of functions, ...」。

**它拥有的列表成员**。下列成员变量就是 4.1.2 那张图的来源：

- [llvm/include/llvm/IR/Module.h:L215-L247](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/Module.h#L215-L247) — `Context`、`GlobalList`、`FunctionList`、`AliasList`、`IFuncList`、`NamedMDList`、`GlobalScopeAsm`、`ValSymTab`、`ModuleID`、`SourceFileName`、`TargetTriple`、`DL` 等成员。注意它们各自的类型：函数/全局/别名/ifunc 四个列表都是 `SymbolTableList<...>`，命名元数据是普通 `ilist<NamedMDNode>`。

**这些列表的类型别名**。`using` 把 `SymbolTableList<Function>` 等起短名，并定义对应的迭代器：

- [llvm/include/llvm/IR/Module.h:L72-L84](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/Module.h#L72-L84) — `FunctionListType = SymbolTableList<Function>` 等类型别名，以及 `global_iterator`、`iterator`（注意：`iterator` 默认就是**函数**迭代器）。

**构造与析构**。构造时必须给定 `ModuleID` 和 `LLVMContext`，并把自身登记到 Context：

- [llvm/lib/IR/Module.cpp:L73-L77](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/IR/Module.cpp#L73-L77) — 构造函数初始化 `Context`、新建 `ValueSymbolTable`、保存 `ModuleID`/`SourceFileName`，并调用 `Context.addModule(this)`。`LLVMContext` 是全局上下文，持有类型与常量的唯一化表（详见 u3-l3），一个 Module 必须从属于某个 Context。
- [llvm/lib/IR/Module.cpp:L118-L125](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/IR/Module.cpp#L118-L125) — 析构时先 `dropAllReferences()`（解除所有引用，避免循环引用导致无法释放），再逐个 `clear()` 各列表。

**遍历 API**。`begin()/end()` 绑定到函数列表，所以 `for (Function &F : *M)` 直接遍历函数：

- [llvm/include/llvm/IR/Module.h:L803-L819](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/Module.h#L803-L819) — `begin()/end()/size()/empty()` 直接转发给 `FunctionList`；`functions()` 返回一个 `iterator_range`，写 `for (Function &F : M.functions())` 与 `for (Function &F : M)` 等价。
- [llvm/include/llvm/IR/Module.h:L822-L830](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/Module.h#L822-L830) — `getFunctionDefs()` 用 `make_filter_range` 过滤掉声明（`isDeclaration()`），只返回有函数体的「定义」。这是日常优化遍历里很常用的入口。

其余列表用命名 range，模式一致：

- [llvm/include/llvm/IR/Module.h:L785-L797](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/Module.h#L785-L797) — 全局变量遍历：`global_begin()/global_end()` 与 `globals()`。
- [llvm/include/llvm/IR/Module.h:L872-L889](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/Module.h#L872-L889) — `global_objects()`（函数 + 全局变量）和 `global_values()`（函数 + 全局变量 + 别名 + ifunc）用 `concat` 把多个 range 拼成一个，方便统一遍历「所有全局可见的值」。

**按名查找依赖符号表**。`getNamedValue` 转发到 `ValueSymbolTable::lookup`，这正是 `SymbolTableList` 维护名字的用处：

- [llvm/lib/IR/Module.cpp:L177-L179](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/IR/Module.cpp#L177-L179) — `getNamedValue` 通过符号表按名查找任意全局值。
- [llvm/lib/IR/Module.cpp:L211-L226](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/IR/Module.cpp#L211-L226) — `getOrInsertFunction`：先 `getNamedValue` 查，找不到就用 `Function::Create(... , Name, this)` 新建并插入（注意它把 `this` 作为 Parent 传进去，函数就此归属到本模块）。

**`getInstructionCount()` 正是上面那条公式的实现**：

- [llvm/lib/IR/Module.cpp:L624-L629](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/IR/Module.cpp#L624-L629) — 遍历 `FunctionList`，累加每个 `F.getInstructionCount()`。这就是 \( I_{\text{module}} = \sum_F I_F \)。

#### 4.1.4 代码实践

**实践目标**：用最朴素的方式确认「`Module` 的 `begin()/end()` 遍历的就是函数」，并看清一个真实模块里有哪些顶层对象。

**操作步骤**（命令行/源码阅读型，无需写 C++）：

1. 写一个含两个函数和一个全局变量的 C 文件 `m.c`：
   ```c
   int g = 1;
   int add(int a, int b) { return a + b; }
   int sub(int a, int b) { return a - b; }
   ```
2. 用 clang 生成文本 IR：`clang -S -emit-llvm m.c -o m.ll`。
3. 打开 `m.ll`，对照本节那张「Module 拥有的列表」图，逐项辨认：`@g` 属于 `GlobalList`，`@add`/`@sub` 属于 `FunctionList`，文件头的 `target triple`/`target datalayout` 对应 `TargetTriple`/`DL`。
4. 阅读上面引用的 `Module::getInstructionCount()`（[Module.cpp:L624-L629](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/IR/Module.cpp#L624-L629)），确认它就是对函数列表的累加。

**需要观察的现象**：`m.ll` 顶层（不缩进）出现的符号，恰好对应 `Module` 直接拥有的列表成员；函数体（缩进在 `{ }` 内）则不属于「模块直接拥有」，而是属于对应 `Function`。

**预期结果**：你能用一句话把 `.ll` 文件的每个顶层段落对应到 `Module` 的某个成员变量。

**关于运行**：步骤 2 需要本机有 clang。若没有可构建环境，跳过运行、只做源码阅读同样达成目标——本实践的核心是「建立 `.ll` 顶层结构与 `Module` 成员的对应关系」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Module` 没有默认构造函数（无参 `Module()`）？必须传 `ModuleID` 和 `LLVMContext`。

> **参考答案**：每个 Module 必须从属于一个 `LLVMContext`（类型/常量在那里唯一化，见构造函数 [Module.cpp:L73-L77](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/IR/Module.cpp#L73-L77) 里的 `Context.addModule(this)`），也需要一个可读的 `ModuleID`（用于打印、RNG 加盐、调试）。没有这两者，Module 无法成立。

**练习 2**：`for (Function &F : M)` 和 `for (Function &F : M.functions())` 有区别吗？`M` 还可能拥有哪些对象，它们各自怎么遍历？

> **参考答案**：没有区别——`begin()/end()` 与 `functions()` 都绑定到同一个 `FunctionList`（见 [Module.h:L803-L819](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/Module.h#L803-L819)）。模块还可能拥有全局变量（`M.globals()`）、别名（`M.aliases()`）、ifunc（`M.ifuncs()`）、命名元数据（`M.named_metadata()`）。

---

### 4.2 Function 与 BasicBlock：函数体与基本块

#### 4.2.1 概念说明

**Function（函数）**。`Function` 表示一个函数/过程。文件头注释概括得很清楚：「A function basically consists of a list of basic blocks, a list of arguments, and a symbol table.」也就是说，剥去属性、调用约定、参数等「元信息」后，函数体的核心就是**一个基本块列表**。

`Function` 的继承链是 `Function → GlobalObject → GlobalValue → Constant → User → Value`：

- [llvm/include/llvm/IR/GlobalObject.h:L9-L11](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/GlobalObject.h#L9-L11) 与 [L28](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/GlobalObject.h#L28) — `GlobalObject` 表示「独立的、有地址的对象」（函数或全局变量，但不是别名）。
- [llvm/include/llvm/IR/GlobalValue.h:L9-L14](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/GlobalValue.h#L9-L14) 与 [L49](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/GlobalValue.h#L49) — `GlobalValue` 是「全局可定义对象」的公共基类（`Function`、`GlobalVariable`、`GlobalAlias` 的共同祖先），继承自 `Constant`。这条链最终到 `Value`，所以**函数本身也是一个 SSA 值**——你可以取它的地址、把它当回调传给 `call`/`invoke`。

正因如此，`Function` 既能被装进 `Module` 的函数列表（包含层次），又是一个 `Value`（继承层次）。

**BasicBlock（基本块）**。基本块是「一段顺序执行、无内部跳转的指令序列」，末尾有且仅有一条终结指令。官方注释强调了两点：基本块也是 `Value`（因为分支/switch 指令要把基本块当跳转目标引用，所以它的类型是 `Type::LabelTy`）；规范的基本块是「若干非终结指令 + 一条终结指令」，验证器（verifier）会保证这一点。

- [llvm/include/llvm/IR/BasicBlock.h:L46-L62](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/BasicBlock.h#L46-L62) — 注释说明基本块是指令的顺序容器、也是 `Value`（类型为 `LabelTy`），并定义良好形式；`class BasicBlock final : public Value, public ilist_node_with_parent<BasicBlock, Function>`。

注意 `BasicBlock` 直接继承 `Value`（不像 `Function` 那样经过 `GlobalValue` 一长串链），因为它不是「全局值」——它没有全局符号、不能被链接器看见，只是函数内部的局部结构。

**终结指令（terminator）**。基本块的「最后一条指令」必须是终结指令（`ret`/`br`/`switch`/`unreachable` 等）。`getTerminator()` 直接取列表末尾元素——这是 O(1) 的，因为终结指令恒在末尾。

#### 4.2.2 核心流程

Function 与 BasicBlock 把 4.1 里「主对象用 `begin/end`」的模式再套了两层：

```
Function                      BasicBlock
 ├── BasicBlocks : SymbolTableList<BasicBlock>     ├── InstList : SymbolTableList<Instruction>
 ├── Arguments  : Argument[]   （惰性构造）          └── Parent   : Function*
 └── SymTab     : 函数内符号表（局部值命名）
```

遍历一个函数的所有指令，标准写法是两层循环：

```text
for (BasicBlock &BB : F)          // Function::begin/end → BasicBlock
    for (Instruction &I : BB)     // BasicBlock::begin/end → Instruction
        访问 I;
```

如果嫌两层循环啰嗦，LLVM 提供了 `inst_iterator`（见 4.2.3），把这两层拍平：`for (Instruction &I : instructions(F))`。

几个贯穿后续所有 Pass 编写的关键事实：

- **每个节点都能反查父节点**：`Function::getParent()` 返回所属 `Module`；`BasicBlock::getParent()` 返回所属 `Function`；`BasicBlock::getModule()` 进一步透传到 `Module`。这让任何一条指令都能 O(1) 找到自己所在的函数与模块。
- **列表用 `SymbolTableList` 维护命名**：函数内的指令 `%foo = ...` 会被登记到函数的 `ValueSymbolTable`，所以局部命名查找也是 O(1)/O(log) 的。
- **创建即插入**：`Function::Create(Ty, Linkage, Name, Parent)` 和 `BasicBlock::Create(Ctx, Name, Parent, InsertBefore)` 都接受一个可选的 `Parent`，传了就自动挂到父列表末尾——所有权随之转移。
- **基本块标号与编号**：`Function` 给每个新加入的基本块分配一个 `Number`（[Function.h:L84](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/Function.h#L84) 里的 `NextBlockNum`），用于分析（如支配树）中按编号索引块。

#### 4.2.3 源码精读

**Function 的定义与核心成员**：

- [llvm/include/llvm/IR/Function.h:L65-L67](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/Function.h#L65-L67) — `class Function : public GlobalObject, public ilist_node<Function>`，`BasicBlockListType = SymbolTableList<BasicBlock>`。
- [llvm/include/llvm/IR/Function.h:L79-L93](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/Function.h#L79-L93) — 成员：`BasicBlocks`（基本块列表）、`NextBlockNum`（块编号计数器）、`Arguments`（参数数组，惰性构造）、`SymTab`（函数级符号表）、`AttributeSets`（属性）。注释「Important things that make up a function!」点明函数的三大组成。
- [llvm/include/llvm/IR/Function.cpp:L481-L489](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/IR/Function.cpp#L481-L489) — 构造函数委托给 `GlobalObject(...)`，并用 `NumArgs(Ty->getNumParams())` 记录参数个数。注意构造时不立即创建基本块——空函数体是合法的（对应「声明 declaration」）。

**创建函数（工厂方法）**：

- [llvm/include/llvm/IR/Function.h:L168-L179](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/Function.h#L168-L179) — `Function::Create(Ty, Linkage, AddrSpace, N, M)`：传入可选 `Module *M`，若提供则函数被自动插入该模块。这是「创建即归属」的典型入口。

**遍历基本块与参数**：

- [llvm/include/llvm/IR/Function.h:L830-L840](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/Function.h#L830-L840) — `begin()/end()/size()/empty()/front()/back()` 转发给 `BasicBlocks` 列表。`front()` 就是入口块。
- [llvm/include/llvm/IR/Function.h:L786-L787](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/Function.h#L786-L787) — `getEntryBlock()` 返回 `front()`：函数的第一个基本块即入口块，执行从这里开始。
- [llvm/include/llvm/IR/Function.h:L845-L879](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/Function.h#L845-L879) — 参数遍历 `arg_begin()/arg_end()/args()`。注意 `CheckLazyArguments()`：参数列表是**惰性**构造的，第一次访问时才真正分配（`hasLazyArguments()` 标记），避免无谓开销。

**`Function::getInstructionCount()` 是两层循环的范本**：

- [llvm/lib/IR/Function.cpp:L361-L366](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/IR/Function.cpp#L361-L366) — 遍历 `BasicBlocks`，累加每个 `BB.size()`。这正是本讲实践任务要你手写的那段逻辑的标准实现：`for (const BasicBlock &BB : BasicBlocks) NumInstrs += BB.size();`。

**BasicBlock 的定义与核心成员**：

- [llvm/include/llvm/IR/BasicBlock.h:L64-L77](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/BasicBlock.h#L64-L77) — `InstListType = SymbolTableList<Instruction, ...>`；私有成员 `InstList`（指令列表）与 `Parent`（所属函数指针）。基本块靠这两个成员记住「我装了哪些指令、我属于哪个函数」。

**创建基本块、反查父节点**：

- [llvm/include/llvm/IR/BasicBlock.h:L206-L210](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/BasicBlock.h#L206-L210) — `BasicBlock::Create(Ctx, Name, Parent, InsertBefore)`：传 `Parent` 即自动挂到该函数（默认末尾，或插在 `InsertBefore` 之前）。
- [llvm/include/llvm/IR/BasicBlock.h:L213-L214](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/BasicBlock.h#L213-L214) — `getParent()` 返回所属 `Function`（可能为 `nullptr`，表示尚未挂入任何函数）。
- [llvm/include/llvm/IR/BasicBlock.h:L220-L224](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/BasicBlock.h#L220-L224) — `getModule()` 透传到 `Module`：块→函数→模块，一行就能拿到最顶层容器。

**终结指令**：

- [llvm/include/llvm/IR/BasicBlock.h:L232-L244](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/BasicBlock.h#L232-L244) — `hasTerminator()` 判断「列表非空且末尾是终结指令」；`getTerminator()` 直接取 `InstList.back()`。因为规范基本块的终结指令恒在末尾，所以这是 O(1)。

**遍历指令**：

- [llvm/include/llvm/IR/BasicBlock.h:L461-L475](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/BasicBlock.h#L461-L475) — `begin()/end()` 转发给 `InstList`。（实现里还会设置一个 `HeadBit`，用于新式调试信息 `RemoveDIs`，初学可忽略。）
- [llvm/include/llvm/IR/BasicBlock.h:L482-L487](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/BasicBlock.h#L482-L487) — `size()/empty()/front()/back()`：`size()` 给出本块指令条数（含调试指令），这就是上面 `getInstructionCount` 累加的对象。

**拍平两层循环的 `inst_iterator`**。当你只想「扫描整个函数的每条指令，不关心它属于哪个块」时，用这个工具：

- [llvm/include/llvm/IR/InstIterator.h:L32-L117](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/InstIterator.h#L32-L117) — `InstIterator` 模板：内部维护「基本块迭代器 `BB`」和「指令迭代器 `BI`」，`operator++` 在当前块走到 `end()` 时自动跳到下一个块（`advanceToNextBB`）。
- [llvm/include/llvm/IR/InstIterator.h:L129-L156](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/InstIterator.h#L129-L156) — 便捷函数 `inst_begin(F)`/`inst_end(F)`/`instructions(F)`：`for (Instruction &I : instructions(F))` 一行遍历函数全部指令。

**类型识别（isa/cast）**。`Function` 和 `BasicBlock` 都提供了 `classof`，让 LLVM 的 `isa<>`/`dyn_cast<>` 体系能通过 `Value::ValueID` 区分它们：

- [llvm/include/llvm/IR/BasicBlock.h:L590-L592](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/BasicBlock.h#L590-L592) — `BasicBlock::classof` 检查 `Value::BasicBlockVal`。
- [llvm/include/llvm/IR/Function.h:L947-L949](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/Function.h#L947-L949) — `Function::classof` 检查 `Value::FunctionVal`。

这意味着当你手里只有一个 `Value *v` 时，可以用 `isa<Function>(v)`、`isa<BasicBlock>(v)` 判断它到底是函数还是基本块——这是继承层次带来的好处。

#### 4.2.4 代码实践

**实践目标**（本讲的主实践任务）：编写一小段 C++，加载一个 `Module`，遍历每个 `Function`、再遍历每个 `BasicBlock` 并打印指令数量。

下面这段是**示例代码**（非项目原有代码），用 `parseIRFile` 读入一个 `.ll`/`.bc`，然后套用本节讲的两层循环：

```cpp
// count_instrs.cpp —— 示例代码：遍历 Module → Function → BasicBlock，统计指令数
// 编译示例（需已构建 LLVM）：
//   clang++ count_instrs.cpp $(llvm-config --cxxflags --ldflags --libs core irreader) -o count_instrs
#include "llvm/IR/Module.h"
#include "llvm/IR/Function.h"
#include "llvm/IR/BasicBlock.h"
#include "llvm/IR/Instruction.h"
#include "llvm/IRReader/IRReader.h"      // parseIRFile
#include "llvm/Support/SourceMgr.h"      // SMDiagnostic
#include "llvm/Support/raw_ostream.h"
using namespace llvm;

int main(int argc, char **argv) {
  if (argc < 2) return 1;
  LLVMContext Ctx;
  SMDiagnostic Err;
  std::unique_ptr<Module> M = parseIRFile(argv[1], Err, Ctx);
  if (!M) { Err.print(argv[0], errs()); return 1; }

  errs() << "Module: " << M->getModuleIdentifier()
         << "  functions=" << M->size() << "\n";   // M->size() = 函数个数

  for (Function &F : *M) {                          // Module::begin/end → Function
    if (F.isDeclaration()) continue;                // 跳过只有声明、没有函数体的
    errs() << "Function @" << F.getName()
           << "  blocks=" << F.size() << "\n";      // F.size() = 基本块个数
    for (BasicBlock &BB : F) {                      // Function::begin/end → BasicBlock
      errs() << "  BB: instrs=" << BB.size() << "\n"; // BB.size() = 指令条数
    }
  }
  return 0;
}
```

**操作步骤**：

1. 用 4.1.4 里的 `m.c` 生成 `m.ll`：`clang -S -emit-llvm m.c -o m.ll`。
2. 编译上面这段示例（命令见注释，需本机已构建 LLVM 并有 `llvm-config`）。
3. 运行 `./count_instrs m.ll`。

**需要观察的现象**：输出里每个 `Function` 后面跟着若干 `BB` 行，每行的 `instrs` 就是该基本块的指令数；把一个函数下所有 `BB` 的 `instrs` 相加，应等于「该函数指令总数」。

**预期结果**：对 `@add` 这种单基本块函数，会看到一行 `blocks=1`、其下一个 `BB: instrs=2`（`add` + `ret` 两条）。

**关于运行**：本实践需要可链接的 LLVM 库。若当前环境没有构建好的 LLVM，可改为**源码阅读型实践**：直接对照标准实现 [Function.cpp:L361-L366](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/IR/Function.cpp#L361-L366) 的 `getInstructionCount()`，确认示例代码里 `BB.size()` 的累加与它完全一致——这两段代码做的是同一件事。运行结果待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`F.size()` 和 `F.getInstructionCount()` 返回的分别是哪一层的「大小」？

> **参考答案**：`F.size()` 是 `BasicBlocks` 列表的长度，即**基本块个数**（见 [Function.h:L835](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/Function.h#L835)）；`F.getInstructionCount()` 是各基本块指令数之和，即**指令总数**（见 [Function.cpp:L361-L366](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/IR/Function.cpp#L361-L366)）。两者不在同一层。

**练习 2**：给定一个 `Instruction &I`，如何只用本讲学到的 API 找到它所在的 `Function` 和 `Module`？（提示：指令的父节点是基本块。）

> **参考答案**：指令的 `getParent()` 返回所属 `BasicBlock *`（`Instruction` 的 API，下一讲细讲）；再 `BB->getParent()` 得到 `Function *`（[BasicBlock.h:L213](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/BasicBlock.h#L213)）；再 `F->getParent()` 得到 `Module *`，或直接 `BB->getModule()`（[BasicBlock.h:L220](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/BasicBlock.h#L220)）。这体现了「每个节点都能 O(1) 反查父节点」。

**练习 3**：为什么 `BasicBlock` 直接继承 `Value`，而 `Function` 要经过 `GlobalObject → GlobalValue → Constant → User → Value` 一长串？

> **参考答案**：函数是**全局值**——它有全局符号、有链接属性（linkage）、能被链接器看到、能取地址当常量传给 `call`，所以需要 `GlobalValue`/`GlobalObject` 这些基类来承载这些「全局可见对象」的公共属性。基本块只是函数内部的局部跳转目标，没有全局符号、不参与链接，只需要「是一个可被分支指令引用的值」，所以直接继承 `Value`（类型 `LabelTy`）即可。

---

## 5. 综合实践

把本讲的两层包含层次串起来，完成下面这个小任务：

**任务**：给你任意一个 `.ll` 文件（可以用 4.1.4 的 `m.ll`，也可以用 `clang -S -emit-llvm` 处理一个你自己的多函数 C 文件），产出一张「函数 → 基本块数 → 指令数」的统计表，并验证全表指令数之和等于 `Module::getInstructionCount()` 的返回值。

**要求**：

1. 用 4.2.4 的示例程序（或你自己写的等价 Pass）遍历 `Module → Function → BasicBlock`，对每个函数打印「函数名、基本块数、该函数指令总数」。
2. 单独调用 `M->getInstructionCount()`（[Module.cpp:L624-L629](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/IR/Module.cpp#L624-L629)），把它与你累加得到的总和对比，确认相等。
3. 选一个含分支/循环的函数（例如 `if` 或 `for`），观察它的 `blocks=` 为什么大于 1，并对照 `.ll` 文本找出每个基本块对应的标签（`entry:`、`if.then:`、`if.end:` 等）。把「`.ll` 里的标签」与「内存里 `BasicBlock` 对象」一一对应起来——这正是本讲「`.ll` 缩进结构是内存对象树的投影」这一直觉的落地。

**进阶（可选）**：把两层循环替换成一层 `for (Instruction &I : instructions(F))`（[InstIterator.h:L143-L147](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/InstIterator.h#L143-L147)），重新统计指令总数，确认结果不变——体会「拍平迭代器」与「显式两层循环」的等价性。

> 若无构建环境无法运行 C++，可降级为纯源码阅读：直接读懂 `Module::getInstructionCount`、`Function::getInstructionCount`、`BasicBlock::size` 三者的实现，并口述它们如何层层累加；再用肉眼数一个 `.ll` 文件里某函数的指令条数来「验证」。

## 6. 本讲小结

- LLVM IR 在内存里是一棵**包含树**：`Module ⊃ Function ⊃ BasicBlock ⊃ Instruction`，每一层都靠 `SymbolTableList`（带符号表的侵入式链表）既管所有权、又管按名查找。
- 同时存在一套**继承层次**：`Function`（经 `GlobalObject → GlobalValue → Constant → User`）和 `BasicBlock` 都最终是 `Value`——这是它们能被命名、被引用、被 `isa<>` 识别的根本原因。
- 遍历 API 高度统一：每一层都把「最主要的子对象」绑定到 `begin()/end()`——`Module` 遍历函数、`Function` 遍历基本块、`BasicBlock` 遍历指令；其余对象用 `globals()`/`aliases()` 等命名 range。
- 每个节点都能 O(1) 反查父节点：`BasicBlock::getParent()` → `Function`、`Function::getParent()` → `Module`；`getTerminator()` 取终结指令也是 O(1)（恒在列表末尾）。
- 指令总数是层层累加：\( I_{\text{module}} = \sum_F \sum_{BB\in F} |BB| \)，这正是 `Module::getInstructionCount` 与 `Function::getInstructionCount` 的实现。
- `inst_iterator` / `instructions(F)` 把「函数 → 基本块 → 指令」两层循环拍平成一层，是写 Pass 时最常用的遍历利器。

## 7. 下一步学习建议

本讲把 IR 的**包含层次**讲清楚了，但刻意留下了一条线没有展开：**`Value` 作为公共根基类，以及 `User`/`Use` 如何构成 SSA 的定义-使用（def-use）双向链**。这恰恰是下一讲的主题：

- **[u3-l2 Value / User / Use：SSA 与 def-use 链](u3-l2-value-use-ssa.md)**：理解为什么 `Function`、`BasicBlock`、`Instruction` 都叫「Value」，以及如何从一条指令出发找到它「用了哪些值」（use-def）和「它被哪些指令用到」（def-use）。这是后续所有优化 Pass 改写 IR 的基础。

建议在进入下一讲前，先做两件事巩固本讲：

1. 重读 [Module.cpp:L583-L595](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/IR/Module.cpp#L583-L595) 的 `dropAllReferences()`——你会看到它用 `for (Function &F : *this)` 遍历函数，正是本讲的遍历模式在真实代码里的运用。
2. 浏览 [llvm/include/llvm/IR/Value.h](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/Value.h)，扫一眼 `Value` 类的成员（不必读懂细节），为下一讲建立「根基类长什么样」的初印象。
