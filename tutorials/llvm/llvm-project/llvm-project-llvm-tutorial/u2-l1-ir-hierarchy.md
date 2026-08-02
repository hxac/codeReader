# IR 层次结构

## 1. 本讲目标

本讲承接「第一个 IR 程序：ModuleMaker」，从「**怎样用 IRBuilder/手写 API 拼出指令**」上升到「**怎样在源码层面理解一段 IR 的整体形状**」。

读完本讲你应该能够：

- 在源码层面说清 **Module → Function → BasicBlock → Instruction** 这四层包含关系，以及每一层对应的 C++ 类。
- 知道每一层用什么容器（侵入式链表 `SymbolTableList`）存储下一层，以及「**归属（ownership）**」与「**父指针（parent）**」是如何双向维系的。
- 掌握遍历这四层的常用迭代器：`Module` 上的函数迭代、`Function` 上的基本块迭代、`BasicBlock` 上的指令迭代，以及「跨基本块、扁平遍历整个函数指令」的 `inst_iterator`。
- 区分**普通指令**与**终结指令（terminator）**，理解为什么每个基本块必须以一条终结指令收尾。

> 本讲只讲 IR 的「**结构（骨架）**」，不涉及类型系统、`Value`/`User`/`Use` 细节（那是下一讲 u2-l2）和 IR 文本语法细节（u2-l4）。

## 2. 前置知识

### 2.1 什么是「中间表示（IR）」

在 LLVM 的三段式模型里，前端（如 Clang）把源代码翻译成一种与语言无关、与机器无关的格式——**LLVM IR**。优化器在 IR 上做变换，后端再把 IR 翻译成机器码。一段 IR 在内存里被组织成一棵树，本讲就是讲这棵树长什么样。

### 2.2 三个口语化术语

- **Module（模块）**：一段完整 IR 的「**根**」。一个 `.ll` / `.bc` 文件解析出来就是一个 Module。它里面装着若干函数、全局变量等。
- **Basic Block（基本块，简称 BB）**：一段**顺序执行、中间不发生跳转**的指令序列。控制流（`if`、`while`、`goto`）的边界就是基本块的边界。
- **SSA（静态单赋值）**：IR 里每个「值」只被赋值一次。本讲不展开 SSA，但要知道「**指令本身就是一个可以被后续引用的值**」这一点。

### 2.3 一个最小的心智模型

可以把一段 LLVM IR 想象成一个「文件夹结构」：

```
Module（仓库）
├── Function: @main（一个函数）
│   ├── BasicBlock: entry（入口块）
│   │   ├── Instruction: %a = add i32 1, 2
│   │   └── Instruction: ret i32 %a   ← 终结指令
│   └── BasicBlock: ...
├── Function: @helper（另一个函数）
│   └── ...
└── GlobalVariable: @g（全局变量，不在任何函数里）
```

记住这张图，下面所有源码讲解都是在解释「这张图是用哪些 C++ 类、哪些链表搭起来的」。

## 3. 本讲源码地图

本讲涉及的关键头文件如下。全部位于 `include/llvm/IR/`（及少量相邻目录）：

| 文件 | 作用 |
| --- | --- |
| `include/llvm/IR/Module.h` | 定义 `Module` 类——IR 的顶层容器 |
| `include/llvm/IR/Function.h` | 定义 `Function` 类——一个函数（含基本块列表 + 参数） |
| `include/llvm/IR/BasicBlock.h` | 定义 `BasicBlock` 类——基本块（含指令列表） |
| `include/llvm/IR/Instruction.h` | 定义 `Instruction` 类——所有指令的基类、opcode 分类 |
| `include/llvm/IR/Instruction.def` | 用 X-Macro 列出**全部指令 opcode**，是分类的真相来源 |
| `include/llvm/IR/InstIterator.h` | 提供 `inst_begin/inst_end/instructions`，扁平遍历整个函数的指令 |
| `include/llvm/IR/Value.h` / `Value.def` | `Value` 是这棵树里几乎所有节点的共同基类；`ValueTy` 枚举用于类型识别 |
| `include/llvm/IR/GlobalValue.h` | `GlobalValue`（`Function` 的祖先类），提供 `getParent()` 回到 Module |
| `include/llvm/IRReader/IRReader.h` | `parseIRFile()`——从 `.ll`/`.bc` 文件读出一个 Module，用于本讲实践 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **Module / Function 模型**——IR 树的最上两层。
2. **BasicBlock 与终结指令**——基本块的结构与「必须有且仅有一条终结指令」的规则。
3. **Instruction 派生与迭代器遍历**——指令的 opcode 分类，以及四种迭代器。

---

### 4.1 Module / Function 模型

#### 4.1.1 概念说明

**Module** 是一段 IR 的顶层容器。源码注释把它描述为「所有其它 IR 对象的顶层容器」，直接持有一张函数表、一张全局变量表、一张别名表等。

一个 Module 里大概有这些「直接子节点」：

- 一组 **Function**（函数）
- 一组 **GlobalVariable**（全局变量）
- 一组 **GlobalAlias / GlobalIFunc**（别名、间接函数）
- 一组 **NamedMDNode**（命名元数据，如 `!llvm.module.flags`）
- 一个 `DataLayout`（数据布局）、一个 `Triple`（目标三元组）
- 一个 `LLVMContext` 引用（类型与常量的「工厂」与唯一性保证）

**Function** 表示一个函数。它「**是**」一个 `GlobalValue`（因为函数有地址、可以被全局引用），同时它「**拥有**」一组基本块和一组参数。

#### 4.1.2 核心流程：归属与父指针

理解这棵树最关键的一点是「**归属（ownership）+ 双向链接**」：

- **父→子**：父对象用一个「侵入式链表」`SymbolTableList<子类型>` 持有子对象，**谁被挂进链表，谁就被父对象拥有**；销毁父对象会回收整棵子树。
- **子→父**：每个子对象保存一个「父指针」，能向上找回祖先：
  - `Function` 通过继承自 `GlobalValue` 的 `getParent()` 返回所属 `Module*`。
  - `BasicBlock::getParent()` 返回所属 `Function*`。
  - `Instruction::getParent()` 返回所属 `BasicBlock*`（由链表节点基类提供）。

可以用伪代码概括这条「向上寻根」的链路：

```
一条指令 I
  → I.getParent()            // BasicBlock*
    → BB.getParent()         // Function*
      → F.getParent()        // Module*
```

也就是说，从任意一条指令都能一路爬到它所在的 Module。

#### 4.1.3 源码精读

**（1）Module 类与它的「子节点列表」成员**

[include/llvm/IR/Module.h:56-67](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/Module.h#L56-L67) 是 `Module` 类的文档注释与声明。注释明确写了「Modules are the top level container of all other LLVM IR objects」（模块是所有其它 IR 对象的顶层容器）。

`Module` 把各类子节点分别存成成员变量，[include/llvm/IR/Module.h:215-223](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/Module.h#L215-L223)：

```cpp
LLVMContext &Context;           // 类型/常量的工厂
GlobalListType GlobalList;      // 全局变量
FunctionListType FunctionList;  // 函数           ← 本讲重点
AliasListType AliasList;        // 别名
IFuncListType IFuncList;        // 间接函数
NamedMDListType NamedMDList;    // 命名元数据
```

其中 `FunctionListType` 就是 `SymbolTableList<Function>`（见 [include/llvm/IR/Module.h:73-74](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/Module.h#L73-L74)）。`SymbolTableList` 是 LLVM 自研的侵入式链表，除了维护顺序，还会顺便维护一张「名字 → Value」的符号表，所以你能用 `M.getFunction("main")` 按名字查函数。

**（2）遍历 Module 里的函数**

`Module` 把 `begin()/end()` 直接定义成「函数迭代器」，这样就能用范围 for 循环遍历所有函数。[include/llvm/IR/Module.h:803-819](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/Module.h#L803-L819)：

```cpp
iterator       begin()       { return FunctionList.begin(); }
iterator       end  ()       { return FunctionList.end(); }
iterator_range<iterator> functions() {
  return make_range(begin(), end());
}
```

所以最常见的写法是：

```cpp
for (Function &F : M) {       // M 是一个 Module
  // 处理每个函数
}
```

> 注意：`for (Function &F : M)` 会同时遍历**函数定义**和**函数声明**（只有签名、没有函数体的外部函数）。如果你只想要有函数体的定义，可用 [Module.h:823-830](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/Module.h#L823-L830) 的 `M.getFunctionDefs()`，它用过滤器跳过 `isDeclaration()` 的函数。

**（3）Module 自己也提供「指令总数」便捷接口**

[include/llvm/IR/Module.h:296-299](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/Module.h#L296-L299) 提供了 `getInstructionCount()`，它等于「所有函数里非调试指令数之和」。这是个便捷统计，背后就是逐函数求和。

**（4）Function 类的继承与「拥有的基本块列表」**

[include/llvm/IR/Function.h:65](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/Function.h#L65) 给出 `Function` 的继承关系：

```cpp
class LLVM_ABI Function : public GlobalObject, public ilist_node<Function> {
```

两个要点：

- 继承 `GlobalObject → GlobalValue → Constant → User → Value`，所以**函数本身也是一个 `Value`**，可以被打包进 `ConstantExpr`、被取地址、被当作参数传递。`GlobalValue` 这一层的 `getParent()`（[include/llvm/IR/GlobalValue.h:711-712](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/GlobalValue.h#L711-L712)）返回所属 Module，这就是「子→父」指针。
- 继承 `ilist_node<Function>`，使 `Function` 自己能成为 Module 那条链表里的一个节点。

`Function` 持有的子节点见 [include/llvm/IR/Function.h:79-93](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/Function.h#L79-L93)：

```cpp
BasicBlockListType BasicBlocks;         // 基本块列表（本讲重点）
Argument *Arguments = nullptr;          // 形参
uint32_t NumArgs;
AttributeList AttributeSets;            // 属性
```

创建函数用工厂方法 `Function::Create`（[include/llvm/IR/Function.h:168-172](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/Function.h#L168-L172)），若传入 `Module*` 会自动挂到该模块的函数链表末尾——这正是 u1-l4 ModuleMaker 用到的「父对象作最后参数自动挂接」模式。

**（5）遍历 Function 里的基本块**

和 Module 同构，`Function` 的 `begin()/end()` 定义成「基本块迭代器」。[include/llvm/IR/Function.h:830-840](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/Function.h#L830-L840)：

```cpp
iterator begin()       { return BasicBlocks.begin(); }
iterator end  ()       { return BasicBlocks.end(); }
size_t  size() const   { return BasicBlocks.size(); }   // 基本块个数
const BasicBlock &front() const { return BasicBlocks.front(); }
```

入口块（函数执行时第一个执行的块）由 `getEntryBlock()`（[Function.h:786-787](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/Function.h#L786-L787)）返回，它就是基本块链表的第一个元素 `front()`。

#### 4.1.4 代码实践：源码阅读型——跟踪「向上寻根」链路

1. **实践目标**：不看文档，仅靠源码确认「一条指令 → Module」的向上链路确实存在。
2. **操作步骤**：
   - 打开 `Instruction.h`，找到 `getFunction()` / `getModule()`（约 [Instruction.h:208-222](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/Instruction.h#L208-L222)）与 `getParent()`（由基类提供）。
   - 打开 `BasicBlock.h`，确认 `getParent()` 返回 `Function*`（[BasicBlock.h:213-214](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/BasicBlock.h#L213-L214)）。
   - 打开 `GlobalValue.h`，确认 `getParent()` 返回 `Module*`（[GlobalValue.h:711-712](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/GlobalValue.h#L711-L712)）。
3. **需要观察的现象**：三层 `getParent()` 的返回类型正好是 `BasicBlock* / Function* / Module*`，逐级向上。
4. **预期结果**：在笔记里画出 `Instruction → BasicBlock → Function → Module` 的箭头图，并标注每一跳对应的方法名。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Function` 要同时继承 `GlobalObject` 和 `ilist_node<Function>` 两个？

**参考答案**：`GlobalObject`（→ `GlobalValue` → `Value`）让它「**是一个值**」，有地址、能被全局引用、能放进符号表；`ilist_node<Function>` 让它「**是一个链表节点**」，能被挂进 Module 的 `SymbolTableList<Function>`。前者解决「身份」，后者解决「归属」。

**练习 2**：`for (Function &F : M)` 和 `M.getFunctionDefs()` 的区别是什么？

**参考答案**：前者遍历**所有**函数条目，包含只有声明、没有函数体的「声明」（`isDeclaration()` 为真）；后者用过滤器跳过声明，只保留有函数体的「定义」。

---

### 4.2 BasicBlock 与终结指令

#### 4.2.1 概念说明

**BasicBlock（基本块）** 是「一段顺序执行、中间不分叉、不跳转」的指令序列。控制流只能从基本块的**第一条指令**进入，从**最后一条指令**离开。

关于基本块，有两个关键事实：

1. **基本块本身也是一个 `Value`**，它的类型是 `LabelTy`（一个「标签」类型）。原因：分支指令（`br`、`switch` 等）需要把「目标基本块」当作操作数引用，所以基本块必须能被当作值使用。
2. **一个「良构（well-formed）」的基本块，必须以恰好一条终结指令（terminator）结尾**，且终结指令不能出现在中间。终结指令决定了控制流下一步去哪个块，例如 `ret`（返回）、`br`（分支）、`switch`、`unreachable` 等。

> 容许「坏构」基本块存在（比如你正在构造 IR、临时还没加终结指令），但 IR 验证器（Verifier）最终会拒绝它。所以「构造完基本块后必须记得补一条终结指令」是写 IR 的常见收尾动作——u1-l4 的 ModuleMaker 就是用 `ReturnInst` 来收尾的。

#### 4.2.2 核心流程：基本块的内部结构

一个基本块只持两样东西（[BasicBlock.h:76-77](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/BasicBlock.h#L76-L77)）：

```
BasicBlock
├── InstListType InstList;   // 指令链表（SymbolTableList<Instruction>）
└── Function *Parent;        // 回指所属函数
```

判定与获取终结指令的逻辑很直接——就是看指令链表**最后一条**是不是终结指令：

```
hasTerminator()  ⇔  链表非空  且  链表.back().isTerminator()
getTerminator()  ⇔  链表.back()   （断言 hasTerminator 为真）
```

#### 4.2.3 源码精读

**（1）BasicBlock 类声明与文档**

[include/llvm/IR/BasicBlock.h:46-62](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/BasicBlock.h#L46-L62)：

```cpp
class BasicBlock final : public Value,                       // 基本块也是一个 Value
                         public ilist_node_with_parent<BasicBlock, Function> {
```

注释（约 L48–L60）写得很清楚：「A basic block is simply a container of instructions that execute sequentially」（基本块就是一组顺序执行的指令的容器）；「A well formed basic block is formed of a list of non-terminating instructions followed by a single terminator instruction」（良构基本块 = 一串非终结指令 + 一条终结指令）。

`ilist_node_with_parent<BasicBlock, Function>` 这个模板会自动给 `BasicBlock` 提供符合「父类型是 Function」语义的 `getParent()`，所以「子→父」指针不需要手写。

**（2）创建基本块**

工厂方法 [BasicBlock.h:206-210](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/BasicBlock.h#L206-L210)：

```cpp
static BasicBlock *Create(LLVMContext &Context, const Twine &Name = "",
                          Function *Parent = nullptr,
                          BasicBlock *InsertBefore = nullptr);
```

传入 `Parent` 即自动挂到该函数末尾；传入 `InsertBefore` 则插到指定块之前。

**（3）终结指令的判定与获取**

[BasicBlock.h:232-244](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/BasicBlock.h#L232-L244)：

```cpp
bool hasTerminator() const {
  return !InstList.empty() && InstList.back().isTerminator();
}
const Instruction *getTerminator() const {
  assert(hasTerminator() && "cannot get terminator of non-well-formed block");
  return &InstList.back();
}
```

注意 `getTerminator()` 带 `assert`：对没有终结指令的「坏构」块调用它，在断言开启的构建里会直接断言失败。还有更宽容的 `getTerminatorOrNull()`（[BasicBlock.h:248-253](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/BasicBlock.h#L248-L253)），不存在终结指令时返回 `nullptr`。

**（4）PHI 节点必须在块的最前面**

基本块对「PHI 节点」有特殊要求：所有 `phi` 指令必须**集中放在基本块的最前面**，终结指令之前。源码提供了 `getFirstNonPHIIt()` / `getFirstInsertionPt()`（[BasicBlock.h:307-347](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/BasicBlock.h#L307-L347)）来返回「第一个可以插入非 PHI 指令的位置」。这是因为 PHI 的语义依赖「从哪个前驱块跳过来」，必须先于其它指令求值。

#### 4.2.4 代码实践：用 `llc`/`opt` 观察基本块边界

1. **实践目标**：用人眼直观感受「基本块 = 一段顺序执行、末尾一条终结指令」。
2. **操作步骤**：
   - 写一段含分支的 C 或 IR，例如：
     ```llvm
     define i32 @f(i32 %x) {
     entry:
       %c = icmp sgt i32 %x, 0
       br i1 %c, label %pos, label %neg      ; ← 终结指令
     pos:
       ret i32 %x                             ; ← 终结指令
     neg:
       %r = sub i32 0, %x
       ret i32 %r                             ; ← 终结指令
     }
     ```
   - 把它存成 `f.ll`，运行 `llvm-dis f.ll` 或直接 `cat f.ll` 查看。
3. **需要观察的现象**：每个块（`entry`/`pos`/`neg`）的最后一条都恰好是一条终结指令（`br` 或 `ret`）；块内的非终结指令都在终结指令之前。
4. **预期结果**：三个基本块、三条终结指令，一一对应。若你删掉某块的 `ret`，再用 `opt -passes=verify f.ll`，验证器会报「Basic Block does not have terminator」之类的错误。**待本地验证**（具体报错文案以本机 LLVM 版本为准）。

#### 4.2.5 小练习与答案

**练习 1**：为什么基本块要继承自 `Value`？

**参考答案**：因为分支 / switch / 间接跳转等终结指令需要把「目标基本块」当作操作数引用。只有基本块本身是 `Value`，才能出现在指令的操作数列表里。

**练习 2**：`getTerminator()` 和 `getTerminatorOrNull()` 的区别与适用场景？

**参考答案**：前者假定块良构，对没有终结指令的块会触发断言（适合你确信块已构造完成时用）；后者返回可能为空的指针（适合在构造 / 变换中途、块可能还没补终结指令时用）。

---

### 4.3 Instruction 派生与迭代器遍历

#### 4.3.1 概念说明

**Instruction** 是所有指令的基类。一条指令「**是**」一个 `User`（它会使用若干操作数），也「**是**」一个 `Value`（它的结果可以被后续指令引用）。

LLVM 用一个**整数 opcode** 给所有指令分类。opcode 既决定了指令做什么（加、减、load、call、ret……），也决定了它属于哪一大类：

- **终结指令（TermOps）**：`ret`/`br`/`switch`/`invoke`/`unreachable`…，结束基本块。
- **一元运算（UnaryOps）**：如 `fneg`。
- **二元运算（BinaryOps）**：`add`/`sub`/`mul`/`shl`/`and`/`or`/`xor`…
- **内存指令（MemoryOps）**：`alloca`/`load`/`store`/`getelementptr`…
- **类型转换（CastOps）**：`trunc`/`zext`/`sext`/`bitcast`…
- **其它（OtherOps）**：`icmp`/`fcmp`/`phi`/`call`/`select`…

这些大类是用「**连续的 opcode 区间**」划分的，因此可以用「opcode 落在某区间内」来做 O(1) 判别，例如 `isTerminator()` 就是判断 opcode 是否落在 `[TermOpsBegin, TermOpsEnd)`。

#### 4.3.2 核心流程：opcode 编码与「区间判别」

关键设计：`Value` 内部有一个 `SubclassID`，对指令而言它存的就是 opcode。`Instruction::getOpcode()` 的实现非常巧妙（[Instruction.h:341](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/Instruction.h#L341)）：

```
getOpcode()  =  getValueID() - InstructionVal
```

也就是说，「指令的 ValueID」=「`InstructionVal` 这个基准值 + opcode」。这正是 `Value.h` 注释（[Value.h:538-542](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/Value.h#L538-L542)）强调的：没有 opcode==0 的指令；`InstructionVal` 必须是 `ValueTy` 枚举里最大的那个值。

于是判别大类就是一次区间比较：

```
isTerminator(op) ⇔ TermOpsBegin ≤ op < TermOpsEnd
isBinaryOp(op)   ⇔ BinaryOpsBegin ≤ op < BinaryOpsEnd
isCast(op)       ⇔ CastOpsBegin ≤ op < CastOpsEnd
…
```

#### 4.3.3 源码精读

**（1）Instruction 类声明**

[include/llvm/IR/Instruction.h:64-67](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/Instruction.h#L64-L67)：

```cpp
class Instruction : public User,
                    public ilist_node_with_parent<Instruction, BasicBlock,
                                                  ilist_iterator_bits<true>,
                                                  ilist_parent<BasicBlock>> {
```

继承 `User`（→ `Value`）让它既能使用操作数、又能被别人使用；`ilist_node_with_parent<Instruction, BasicBlock>` 让它能挂进基本块的指令链表，并自动获得返回 `BasicBlock*` 的 `getParent()`。

**（2）opcode 的获取与终结指令判别**

[Instruction.h:341-352](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/Instruction.h#L341-L352)：

```cpp
unsigned getOpcode() const { return getValueID() - InstructionVal; }
bool isTerminator() const { return isTerminator(getOpcode()); }
bool isBinaryOp()   const { return isBinaryOp(getOpcode()); }
bool isCast()       const { return isCast(getOpcode()); }
```

静态判别函数（[Instruction.h:360-362](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/Instruction.h#L360-L362)）：

```cpp
static inline bool isTerminator(unsigned Opcode) {
  return Opcode >= TermOpsBegin && Opcode < TermOpsEnd;
}
```

**（3）opcode 的「真相来源」——Instruction.def**

各 `Begin/End` 边界值来自 X-Macro 文件 `Instruction.def`。终结指令清单见 [include/llvm/IR/Instruction.def:126-139](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/Instruction.def#L126-L139)：

```
 FIRST_TERM_INST  ( 1)
HANDLE_TERM_INST  ( 1, Ret        , ReturnInst)
HANDLE_TERM_INST  ( 2, UncondBr   , UncondBrInst)
HANDLE_TERM_INST  ( 3, CondBr     , CondBrInst)
HANDLE_TERM_INST  ( 4, Switch     , SwitchInst)
HANDLE_TERM_INST  ( 5, IndirectBr , IndirectBrInst)
HANDLE_TERM_INST  ( 6, Invoke     , InvokeInst)
HANDLE_TERM_INST  ( 7, Resume     , ResumeInst)
HANDLE_TERM_INST  ( 8, Unreachable, UnreachableInst)
HANDLE_TERM_INST  ( 9, CleanupRet , CleanupReturnInst)
HANDLE_TERM_INST  (10, CatchRet   , CatchReturnInst)
HANDLE_TERM_INST  (11, CatchSwitch, CatchSwitchInst)
HANDLE_TERM_INST  (12, CallBr     , CallBrInst)
  LAST_TERM_INST  (12)
```

于是 `TermOpsBegin = 1`、`TermOpsEnd = 13`。同一文件往下依次给出 `UnaryOps(13)`、`BinaryOps(14–31)`、`MemoryOps(32–38)`、`CastOps(39–52)`、`FuncletPadOps(53–54)`、`OtherOps(55–69)`。`Instruction.h` 里把这些宏展开成枚举（[Instruction.h:1044-1091](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/Instruction.h#L1044-L1091)）。

**（4）四种迭代器**

把四层串起来遍历，最常用的写法：

```cpp
// 第 1 层：遍历 Module 的函数
for (Function &F : M) {

  // 第 2 层：遍历 Function 的基本块
  for (BasicBlock &BB : F) {
    outs() << "  基本块指令数 = " << BB.size() << "\n";

    // 第 3 层：遍历 BasicBlock 的指令
    for (Instruction &I : BB) {
      // 处理每条指令
    }
  }
}
```

其中 `BB.size()` 来自 [BasicBlock.h:482-487](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/BasicBlock.h#L482-L487)，`BB.begin()/end()` 来自 [BasicBlock.h:461-475](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/BasicBlock.h#L461-L475)。

**第 4 种迭代器**：当你不在意基本块边界、只想「扁平地」遍历整个函数的所有指令时，用 `inst_iterator`。它内部是一个「两层迭代器」——同时记住当前在哪个基本块、以及块内的哪条指令，跨块时自动跳到下一个块的起点。[include/llvm/IR/InstIterator.h:129-133](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/InstIterator.h#L129-L133) 提供了便捷函数：

```cpp
inline inst_iterator inst_begin(Function *F) { return inst_iterator(*F); }
inline inst_iterator inst_end  (Function *F) { return inst_iterator(*F, true); }
inline inst_range    instructions(Function *F) {
  return inst_range(inst_begin(F), inst_end(F));
}
```

于是：

```cpp
for (Instruction &I : instructions(F)) {   // 跨基本块、扁平遍历
  // ...
}
```

这种「两层迭代器」自动推进的逻辑在 `advanceToNextBB()`（[InstIterator.h:108-116](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/InstIterator.h#L108-L116)）：当前块读到 `end()` 就跳到下一个块的 `begin()`。

**（5）`ValueTy` 与 `classof`：如何用 `isa<>` 识别节点类型**

`Module/Function/BasicBlock/Instruction` 都各自实现了一个静态 `classof`，配合 `isa<>`/`cast<>`/`dyn_cast<>` 做类型识别。机制是查 `Value::SubclassID`（即 `getValueID()`）落在哪个枚举值。例如：

- `BasicBlock`：`isa<BasicBlock>(V)` 当且仅当 `V->getValueID() == Value::BasicBlockVal`（[BasicBlock.h:590-592](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/BasicBlock.h#L590-L592)）。
- `Function`：判断等于 `Value::FunctionVal`（[Function.h:947-949](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/Function.h#L947-L949)）。
- `Instruction`：用「大于等于」判断，因为指令的 ID 是「`InstructionVal` + opcode」一段区间（`Value.def` 把 `Instruction` 放在枚举最后，见 [Value.def:127](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/Value.def#L127)）。

这些枚举值由 `Value.def` 集中维护，`Value.h` 用 X-Macro 展开成 `ValueTy` 枚举（[Value.h:524-531](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/Value.h#L524-L531)）。更多 `Value`/`User`/`Use` 细节留给下一讲 u2-l2。

#### 4.3.4 代码实践：见「第 5 节 综合实践」

本小模块的实践并入第 5 节的统计小程序——它正好用到了本模块讲到的全部三种「逐层迭代」与「opcode 判别」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `Instruction::classof` 用「`>=` InstructionVal」而不是「`==`」？

**参考答案**：因为指令的 ValueID 不是单个值，而是「`InstructionVal` + opcode」的一段连续区间，每条具体指令的 ID 都不同（等于基准值加上它的 opcode）。所以判别「是不是指令」要用区间（`>= InstructionVal`）。

**练习 2**：`for (Instruction &I : BB)` 和 `for (Instruction &I : instructions(F))` 有何区别？

**参考答案**：前者只遍历**一个基本块**内的指令；后者用 `inst_iterator` **跨基本块、扁平地**遍历整个函数的全部指令，自动在块边界跳转，不需要你写嵌套循环。

**练习 3**：`isTerminator()` 是怎么用一次比较实现的？

**参考答案**：终结指令的 opcode 被分配在连续区间 `[TermOpsBegin, TermOpsEnd)` 内（`Instruction.def` 里 `FIRST_TERM_INST(1)`…`LAST_TERM_INST(12)`），所以只要判断 `TermOpsBegin ≤ opcode < TermOpsEnd` 即可，O(1)。

---

## 5. 综合实践：读入 `.ll` 文件，统计每个函数的基本块数与指令数

本任务把本讲三个模块串起来：用 IRReader 读入一个 Module（4.1），遍历它的每个 Function（4.1），统计每个函数的基本块数（4.2），再遍历指令并按 opcode 大类做简单分类（4.3）。

### 5.1 实践目标

写一个最小的命令行小工具 `ircount`：传入一个 `.ll` 文件路径，对每个函数打印「函数名、基本块数、指令总数」，并额外统计「终结指令 / 二元运算 / 内存指令 / 类型转换」各有多少条。

### 5.2 操作步骤

**步骤 1**：准备一段被统计的 IR。把下面的内容存为 `demo.ll`：

```llvm
define i32 @add(i32 %a, i32 %b) {
entry:
  %s = add i32 %a, %b
  ret i32 %s
}

define i32 @abs(i32 %x) {
entry:
  %c = icmp sgt i32 %x, 0
  br i1 %c, label %pos, label %neg
pos:
  ret i32 %x
neg:
  %r = sub i32 0, %x
  ret i32 %r
}
```

**步骤 2**：编写 `ircount.cpp`（**示例代码**，非项目原有文件）。核心逻辑只用了本讲讲过的 API：

```cpp
// 示例代码：ircount.cpp —— 统计每个函数的基本块数与指令数
// 编译（out-of-tree 风格，需先有 LLVM 构建产物）：
//   clang++ -std=c++17 ircount.cpp $(llvm-config --cxxflags --ldflags --libs core irreader) -o ircount
#include "llvm/IR/Function.h"
#include "llvm/IR/InstrTypes.h"      // for TerminatorInst 等（按需）
#include "llvm/IR/Module.h"
#include "llvm/IR/InstIterator.h"
#include "llvm/IR/LLVMContext.h"
#include "llvm/IRReader/IRReader.h"
#include "llvm/Support/raw_ostream.h"
#include "llvm/Support/SourceMgr.h"

using namespace llvm;

int main(int argc, char **argv) {
  if (argc < 2) {
    errs() << "用法: " << argv[0] << " <file.ll>\n";
    return 1;
  }

  LLVMContext Ctx;
  SMDiagnostic Err;
  // parseIRFile 同时支持 .ll（文本）与 .bc（位码），自动识别格式
  std::unique_ptr<Module> M = parseIRFile(argv[1], Err, Ctx);
  if (!M) {
    Err.print(argv[0], errs());
    return 1;
  }

  for (Function &F : *M) {                       // 第 1 层：Module → Function
    if (F.isDeclaration())                       // 跳过仅有声明的函数
      continue;

    unsigned bbCount = 0, termCount = 0;
    unsigned binCount = 0, memCount = 0, castCount = 0;

    for (BasicBlock &BB : F) {                   // 第 2 层：Function → BasicBlock
      ++bbCount;
      for (Instruction &I : BB) {                // 第 3 层：BasicBlock → Instruction
        if (I.isTerminator())      ++termCount;
        else if (I.isBinaryOp())   ++binCount;
        else if (I.isCast())       ++castCount;
        // 内存指令用 opcode 区间判别（与 Instruction.def 的 MemoryOps 对应）
        else switch (I.getOpcode()) {
          case Instruction::Alloca:
          case Instruction::Load:
          case Instruction::Store:
          case Instruction::GetElementPtr:
            ++memCount; break;
          default: break;
        }
      }
    }

    outs() << "函数 " << F.getName() << ":\n";
    outs() << "  基本块数 = " << bbCount << "\n";
    outs() << "  终结指令 = " << termCount
           << "，二元运算 = " << binCount
           << "，内存指令 = " << memCount
           << "，类型转换 = " << castCount << "\n";
  }
  return 0;
}
```

**步骤 3**：编译并运行：

```bash
clang++ -std=c++17 ircount.cpp \
  $(llvm-config --cxxflags --ldflags --libs core irreader) -o ircount
./ircount demo.ll
```

> 也可以用第 4 种迭代器把「指令总数」写得更短：`unsigned total = 0; for (Instruction &I : instructions(F)) ++total;`，它跨基本块扁平遍历。`instructions(F)` 来自 `InstIterator.h`。

### 5.3 需要观察的现象

- `@add` 应得到：基本块数 = 1，终结指令 = 1（`ret`），二元运算 = 1（`add`）。
- `@abs` 应得到：基本块数 = 3，终结指令 = 3（`br` + 两个 `ret`），二元运算 = 1（`sub`）。
- 声明函数（如果有）会被 `isDeclaration()` 跳过，不计入统计。

### 5.4 预期结果（输出大致如下）

```
函数 add:
  基本块数 = 1
  终结指令 = 1，二元运算 = 1，内存指令 = 0，类型转换 = 0
函数 abs:
  基本块数 = 3
  终结指令 = 3，二元运算 = 1，内存指令 = 0，类型转换 = 0
```

> **待本地验证**：`llvm-config` 的具体参数、链接库集合与编译器版本依本机环境而定。如果暂时无法编译，可改为「源码阅读型实践」：把上面的两层范围 for 循环当作伪代码，对照 `Function.h`、`BasicBlock.h`、`InstIterator.h` 确认每个方法签名都存在，并手算 `demo.ll` 的统计数字，验证与预期一致。

### 5.5 进阶（可选）

把统计口径从「按 opcode 大类」细化到「按具体 opcode」，用 `I.getOpcodeName()` 打印每条指令的名字，观察一个 `.ll` 里到底出现了哪些指令。这能帮你为后续学习 IRBuilder（u2-l3）和优化 pass（单元 3）建立「指令全集」的直观印象。

## 6. 本讲小结

- 一段 IR 在内存里是 **Module → Function → BasicBlock → Instruction** 的四层树；`Module` 是顶层容器，**不是** `Value`，而 `Function / BasicBlock / Instruction` 都是 `Value`。
- 父→子用**侵入式链表 `SymbolTableList`** 持有并拥有子对象；子→父用 **`getParent()`** 回指祖先，三层 `getParent()` 串起「指令 → 基本块 → 函数 → 模块」。
- **基本块** = 一串非终结指令 + **恰好一条终结指令**；`hasTerminator()/getTerminator()` 就是看链表最后一条；PHI 节点必须集中在块首。
- **指令**用整数 **opcode** 分类，各大类占据连续的 opcode 区间，可用 `isTerminator()/isBinaryOp()/isCast()` 等做 O(1) 判别；opcode 的真相来源是 X-Macro 文件 `Instruction.def`。
- 遍历有四把钥匙：`for (Function &F : M)`、`for (BasicBlock &BB : F)`、`for (Instruction &I : BB)`，以及跨块扁平的 `instructions(F)`（`inst_iterator`）。
- `isa<>`/`cast<>`/`dyn_cast<>` 的类型识别依赖 `Value::SubclassID`（`ValueTy` 枚举，由 `Value.def` 维护）与各类的 `classof`。

## 7. 下一步学习建议

- **u2-l2（类型系统与 Value）**：本讲只把 `Value` 当作「树节点的共同基类」一笔带过。下一讲会深入 `Type` 体系、`Value / User / Use` 的引用关系、以及 `Constant / ConstantInt / ConstantFP`，把「指令为什么既是值、又使用值」讲透。
- **u2-l3（用 IRBuilder 构建 IR）**：学完结构后，下一讲教你用 `IRBuilder` 更安全、更便捷地**构造**这棵树（算术、控制流、调用），并避免本讲提到的「忘记补终结指令」「PHI 放错位置」等坑。
- **u2-l4（IR 的文本与位码格式）**：理解 `.ll` 与 `.bc` 的读写链路，本讲综合实践里用到的 `parseIRFile` 正是 IRReader 这条统一入口。
- **单元 3（Pass 与优化流水线）**：当你能熟练遍历这棵 IR 树，就可以开始写「**遍历 + 改写**」IR 的 Pass 了——那正是 `inst_iterator` 与各类迭代器大显身手的地方。
