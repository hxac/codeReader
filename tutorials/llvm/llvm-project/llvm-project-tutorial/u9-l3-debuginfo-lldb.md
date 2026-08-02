# 调试信息与 LLDB

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清楚「调试信息」在 LLVM 里到底是什么形态——它不是机器码的一部分，而是一套寄生在 IR 元数据（metadata）上的旁路信息。
- 读懂 `clang -g` 生成的 `.ll` 里那些 `!DICompileUnit`、`!DISubprogram`、`!DILocalVariable`、`#dbg_value`、`!dbg !N` 到底各自承担什么角色。
- 掌握 `DIBuilder` 这套构造 API 的典型用法，并理解它为什么要 `finalize()`。
- 描述调试信息从 IR 一路落到目标文件里 DWARF 段的完整流水线：前端构造 → 后端 `AsmPrinter`/`DwarfDebug` 发射 → LLDB 的 `SymbolFileDWARF` 读回。
- 用 `clang -g` 与 `llvm-dwarfdump` 自己跑一遍这条链路，并把 DWARF 输出对应回源码。

## 2. 前置知识

本讲假定你已经掌握：

- **LLVM IR 的内存对象模型**（u3-l1）：Module ⊃ Function ⊃ BasicBlock ⊃ Instruction 的包含层次。
- **Value/Use 与元数据**（u3-l2）：IR 对象除了指令之外，还有一类叫 `Metadata` 的「注解」对象，它不参与 def-use 链，也不影响代码生成。
- **类型系统与常量**（u3-l3）：尤其是「类型在 `LLVMContext` 内唯一化」这一设计。
- **Clang CodeGen：从 AST 到 IR**（u5-l5）：知道 `CodeGenModule`/`CodeGenFunction` 如何把 AST 翻译成 IR。

本讲会用到两个你可能不熟的术语，先做个铺垫：

- **DWARF**：一种标准的调试信息二进制格式（`.debug_info`、`.debug_line` 等段），由 DWARF 委员会制定，是 Linux/ELF/Mach-O 生态里 GDB、LLDB 通用的事实标准。它用一棵 **DIE（Debugging Information Entry，调试信息条目）树** 来描述编译单元、类型、函数、变量及其关系。
- **CodeView**：微软的调试信息格式，Windows/COFF 生态使用，由 Visual Studio 等消费。LLVM 把 DWARF 与 CodeView 视为两套并列的「后端消费者」。

一个贯穿全讲的直觉：**调试信息是一条「编码—解码」的往返链**。前端把「源程序的抽象语法树如何映射到 IR」编码成元数据；优化器和后端尽量保留它、最后翻译成标准 DWARF；调试器再把 DWARF 解码回源码视图。它对生成的机器码本身没有任何影响——这是一个刻意为之的设计决策。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `llvm/include/llvm/IR/DebugInfoMetadata.h` | 全部调试元数据类的定义：`DINode`/`DIScope`/`DIType`/`DICompileUnit`/`DISubprogram`/`DILocation`/`DILocalVariable`/`DIExpression` 等。 |
| `llvm/include/llvm/IR/DIBuilder.h` | `DIBuilder` 类声明：构造调试元数据的便捷 API。 |
| `llvm/lib/IR/DIBuilder.cpp` | `DIBuilder` 的实现：`createCompileUnit`/`createFunction`/`insertDbgValue`/`finalize` 等。 |
| `llvm/lib/IR/DebugInfoMetadata.cpp` | 调试元数据类的成员实现（打印、判等等）。 |
| `llvm/lib/IR/DebugInfo.cpp` | 调试信息工具函数：查询某 `Value` 上的 debug 记录、剥离调试信息等。 |
| `clang/lib/CodeGen/CGDebugInfo.{h,cpp}` | Clang 侧驱动 `DIBuilder` 的总管类 `CGDebugInfo`。 |
| `llvm/lib/CodeGen/AsmPrinter/DwarfDebug.{h,cpp}`、`AsmPrinter.cpp` | 后端把元数据翻译成 DWARF DIE 并发射的引擎。 |
| `lldb/source/Plugins/SymbolFile/DWARF/SymbolFileDWARF.{h,cpp}` | LLDB 读取 DWARF、按需重建符号信息的插件。 |
| `llvm/docs/SourceLevelDebugging.md` | LLVM 调试信息的官方权威文档。 |

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **调试元数据**：用 `metadata` 表达「源程序长什么样」。
2. **DIBuilder**：构造这些元数据的脚手架 API。
3. **从 IR 到 DWARF 再到 LLDB**：调试信息的发射与消费，闭环往返链。

### 4.1 调试元数据：用 metadata 表达源程序

#### 4.1.1 概念说明

优化器看到的 IR 是「计算图」，里面只有寄存器、指令、基本块，没有任何关于「这一行机器码对应源文件第几行」「这个 `%2` 是源码里的哪个变量」的信息。调试信息要补的正是这种「元信息」。

LLVM 的做法是：**不改动 IR 的核心语义，而是把源程序的结构作为「元数据（metadata）」旁挂在 IR 上**。官方文档把设计哲学概括为四点（[llvm/docs/SourceLevelDebugging.md:12-49](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/llvm/docs/SourceLevelDebugging.md#L12-L49)）：

- 调试信息对编译器的其余部分影响极小——变换、分析、代码生成都不应因调试信息而改写。
- 调试信息要与优化有**良好定义的、可描述的**交互方式。
- LLVM 支持任意源语言，因此 LLVM-to-LLVM 的工具不需要懂源语言语义。
- 通过代码生成器支持，能产出标准的 DWARF/CodeView，兼容 GDB、LLDB、Visual Studio 等传统调试器。

也就是说，调试信息是一条**与主计算流并行的旁路（side channel）**：它随 IR 流水线流动、被尽量保留，最后在代码发射阶段才翻译成标准格式。

#### 4.1.2 核心流程

调试元数据在 IR 里大致分两层：

1. **描述层（一张描述源程序的对象图）**：用一整套以 `DINode` 为根的元数据节点，描述编译单元、文件、类型、函数、变量及其作用域嵌套关系。这棵图最终会被翻译成 DWARF 的 DIE 树。
2. **挂载层（把描述「贴」到 IR 上）**：每条指令带一个 `!dbg` 指向 `DILocation`（说清它对应源码第几行第几列、属于哪个作用域）；并用 `#dbg_value`/`#dbg_declare` 等**调试记录（debug record）**说明某个 SSA 值/地址对应哪个源变量。

后者是关键：调试记录「与指令交错出现，但本身不是指令，对生成的代码没有任何作用」（[llvm/docs/SourceLevelDebugging.md:204-209](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/llvm/docs/SourceLevelDebugging.md#L204-L209)）。

一条完整的「源码 → IR 调试信息」映射，可用下面这段对

```c
int add(int a, int b) {
  int sum = a + b;
  return sum;
}
```

生成的 IR（示意，以本地 `clang -g -O0 -S -emit-llvm` 实际输出为准）来理解：

```llvm
define dso_local i32 @add(i32 noundef %a, i32 noundef %b) !dbg !10 {
entry:
  %a.addr = alloca i32, align 4, !dbg !16        ; 每条指令带 !dbg
  %b.addr = alloca i32, align 4, !dbg !16
  store i32 %a, ptr %a.addr, align 4, !dbg !16
  ...
  #dbg_declare(ptr %a.addr, !14, !DIExpression(), !16)   ; 调试记录：变量 a 住在 %a.addr
  ...
}
!llvm.dbg.cu = !{!0}                ; 整个模块的编译单元入口
!llvm.module.flags = !{!7}          ; 含 "Debug Info Version"
!0 = distinct !DICompileUnit(language: DW_LANG_C99, file: !1, ...)
!1 = !DIFile(filename: "add.c", directory: "/home/me")
!10 = distinct !DISubprogram(name: "add", ...)   ; 函数 add
!14 = !DILocalVariable(name: "a", arg: 1, scope: !10, ...) ; 参数 a
!16 = !DILocation(line: 2, column: 13, scope: !10)        ; 第 2 行第 13 列
```

注意几个特征：`@`/`%` 仍是普通 IR 符号，而所有 `!` 开头的都是元数据；调试记录 `#dbg_declare` 缩进打印、以 `#dbg_` 前缀区分于真正的指令。

#### 4.1.3 源码精读

**(a) `DINode`：带 DWARF tag 的元数据节点**

所有调试元数据的根基类是 `DINode`，它本身是 `MDNode` 的子类，额外带一个 **DWARF tag**（如 `DW_TAG_compile_unit`、`DW_TAG_subprogram`），存在 `SubclassData16` 槽里（[llvm/include/llvm/IR/DebugInfoMetadata.h:140-151](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/llvm/include/llvm/IR/DebugInfoMetadata.h#L140-L151)）。tag 决定了这个节点在语义上是「编译单元」还是「函数」还是「变量」。注释里点明它叫 `DINode`（而非 `DWARFNode`）是因为它**可能用于非 DWARF 输出**——这印证了「描述层与发射格式解耦」的设计。

`DINode` 还定义了 `DIFlags` 位掩码（`FlagPrivate`/`FlagPublic`/`FlagStaticMember` 等，[DebugInfoMetadata.h:179-187](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/llvm/include/llvm/IR/DebugInfoMetadata.h#L179-L187)），用来表达可见性、是否静态成员等属性。

**(b) `DIScope` 作用域层次与 `DIFile`**

`DIScope`（[DebugInfoMetadata.h:527-577](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/llvm/include/llvm/IR/DebugInfoMetadata.h#L527-L577)）是几乎所有「有作用域」节点的基类，它的 `classof` 列出了一大串子类（`DIFile`/`DICompileUnit`/`DISubprogram`/`DILexicalBlock`/`DINamespace`/各类 `DIType`…）。`DIScope::getRawFile()`（[DebugInfoMetadata.h:550-553](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/llvm/include/llvm/IR/DebugInfoMetadata.h#L550-L553)）有个精巧细节：`DIFile` 本身就是 `DIScope`，所以「文件即作用域」时返回 `this`，其余子类都把文件指针放在第一个操作数里。这正是作用域链「能一路向上问到文件」的实现基础。

`DIFile`（[DebugInfoMetadata.h:583-646](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/llvm/include/llvm/IR/DebugInfoMetadata.h#L583-L646)）除了文件名/目录，还能带**校验和**（`ChecksumInfo`，支持 MD5/SHA1/SHA256，[DebugInfoMetadata.h:592-617](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/llvm/include/llvm/IR/DebugInfoMetadata.h#L592-L617)）和**源文本**（`Source`）。后者让调试器在找不到源文件时仍能显示源码。

**(c) `DIType` 与 `DICompileUnit`**

类型描述由 `DIType`（[DebugInfoMetadata.h:721-810](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/llvm/include/llvm/IR/DebugInfoMetadata.h#L721-L810)）统领，下分 `DIBasicType`（`int`/`float` 这种基本类型，带 DWARF 编码 `DW_ATE_*`）、`DIDerivedType`（指针/引用/typedef/成员，[DebugInfoMetadata.h:1275](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/llvm/include/llvm/IR/DebugInfoMetadata.h#L1275)）、`DICompositeType`（struct/union/array/class，[DebugInfoMetadata.h:1616](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/llvm/include/llvm/IR/DebugInfoMetadata.h#L1616)）、`DISubroutineType`（函数类型，[DebugInfoMetadata.h:1978](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/llvm/include/llvm/IR/DebugInfoMetadata.h#L1978)）。注意类型描述**不是** u3-l3 里那个 `llvm::Type`（那个只描述位的布局），而是「带名字、带行号、带成员偏移」的源语言类型视图。

`DICompileUnit`（编译单元，[DebugInfoMetadata.h:2037-2056](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/llvm/include/llvm/IR/DebugInfoMetadata.h#L2037-L2056)）是整张调试信息图的**锚点**。它带一个 `DebugEmissionKind` 枚举（[DebugInfoMetadata.h:2042-2048](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/llvm/include/llvm/IR/DebugInfoMetadata.h#L2042-L2048)），决定发射多少调试信息：

```
NoDebug          完全不发调试信息
FullDebug        全量（变量、类型、行号都发），-g 默认
LineTablesOnly   仅行号表，-gline-tables-only
DebugDirectivesOnly 仅指令，-gmodules
```

每个 `Module` 只能有一个 CU，它在 IR 里被挂到名为 `llvm.dbg.cu` 的命名元数据节点上（见 4.2.3）。

**(d) `DILocation`：行号、列号、作用域、内联链**

`DILocation`（[DebugInfoMetadata.h:2668-2733](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/llvm/include/llvm/IR/DebugInfoMetadata.h#L2668-L2733)）记录 `line`/`column`/`scope`/`inlinedAt` 四要素，它**直接派生自 `MDNode`**（不是 `DINode`），因为它是「贴在指令上的位置注解」而非「源程序结构的一部分」。`getScope()` 返回 `DILocalScope`（一个函数或词法块），`inlinedAt` 则指向另一条 `DILocation`——若这段代码是被内联进来的，就构成一条**内联位置链**，让调试器能重建「这行代码源自哪个被内联函数、又是从哪一层调用内联进来的」。

每条 `Instruction` 通过 `!dbg` 持有一个 `DILocation`。后端的 `DwarfDebug` 正是从这些位置生成 `.debug_line` 行号表。

**(e) 调试记录与 `DILocalVariable`/`DIExpression`**

源变量本身用 `DILocalVariable`（[DebugInfoMetadata.h:4179-4263](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/llvm/include/llvm/IR/DebugInfoMetadata.h#L4179-L4263)）描述，带名字、类型、`Arg`（参数序号，非 0 即参数）、作用域。`isValidLocationForIntrinsic`（[DebugInfoMetadata.h:4257-4259](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/llvm/include/llvm/IR/DebugInfoMetadata.h#L4257-L4259)）体现了一条硬约束：变量与它所在的位置必须在**同一个 subprogram** 里。

光有变量描述还不够——还要说清「这个变量此刻的值/地址在哪」。这正是调试记录的职责：

- `#dbg_declare(地址, 变量, 表达式, 位置)`：描述变量的**地址**（典型是入口块的一个 `alloca`）。
- `#dbg_value(值, 变量, 表达式, 位置)`：描述变量的**值**（优化把变量从内存提升为 SSA 值后用它）。

其语法与语义见 [llvm/docs/SourceLevelDebugging.md:223-294](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/llvm/docs/SourceLevelDebugging.md#L223-L294)。第三个参数 `DIExpression`（[DebugInfoMetadata.h:3472](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/llvm/include/llvm/IR/DebugInfoMetadata.h#L3472)）是一串 DWARF 表达式操作码（`DW_OP_plus`、`DW_OP_deref`、`DW_OP_LLVM_fragment` 等），它把「基础地址/值」加工成「真正的变量位置」——比如 `#dbg_declare(ptr %buffer, var, !DIExpression(DW_OP_plus, 64), ...)` 表示变量住在 `buffer+64` 处。

后续优化 pass 需要反向查询「某个 `Value` 上挂着哪些 debug 记录」时，就调用 `llvm/lib/IR/DebugInfo.cpp` 里的工具，例如 `findDVRDeclares`（[llvm/lib/IR/DebugInfo.cpp:48-60](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/llvm/lib/IR/DebugInfo.cpp#L48-L60)）——它先检查该值是否被任何元数据引用，再筛出类型为 `Declare` 的记录。这正说明调试信息是「可被流水线其他阶段查询、改写的旁路数据」。

#### 4.1.4 代码实践：读一份带调试信息的 IR

1. **实践目标**：亲手把一个 C 函数变成带调试信息的 IR，并指认每段元数据的角色。
2. **操作步骤**：
   - 写一个 `add.c`，内容就是 4.1.2 里的 `int add(int a, int b){ int sum=a+b; return sum; }`。
   - 执行 `clang -g -O0 -S -emit-llvm add.c -o add.ll`（`-g` 开启调试信息，`-O0` 关闭优化以保证 `alloca` 还在）。
   - 用编辑器打开 `add.ll`。
3. **需要观察的现象**：依次找到并标注——`!llvm.dbg.cu`、`!llvm.module.flags` 里的 `!"Debug Info Version"`、`!DICompileUnit`、`!DIFile`、`!DISubprogram`、各参数与 `sum` 的 `!DILocalVariable`、入口块里的 `#dbg_declare` 记录、以及每条指令末尾的 `!dbg !N`。
4. **预期结果**：你应当能在 IR 里清晰看到「描述层（DINode 图）」与「挂载层（!dbg / #dbg_）」两层，并能解释每个 `!N` 指向的节点描述了什么。
5. 若本机无 clang，则改为纯阅读：在仓库内 `llvm/test/DebugInfo/` 下任选一个 `.ll` 测试，完成同样的指认练习。运行结果「待本地验证」。

#### 4.1.5 小练习与答案

- **练习 1**：`DILocation` 为什么直接继承 `MDNode` 而不是 `DINode`？
  - **答案**：`DINode` 是「源程序结构描述」（带 DWARF tag，要翻译成 DIE），而 `DILocation` 是「贴在指令上的位置注解」，它只是 `(line, col, scope, inlinedAt)` 的四元组，不是源程序实体，因而不需要 tag、也不进 DIE 类型树。
- **练习 2**：为什么 `DICompileUnit` 要专门挂到 `llvm.dbg.cu` 这个命名元数据上？
  - **答案**：它是整张调试信息图的入口锚点；后端发射器与各工具都从 `llvm.dbg.cu` 找到 CU，再由 CU 出发遍历其下属的 subprogram/global/类型列表。
- **练习 3**：`#dbg_declare` 与 `#dbg_value` 各描述变量的什么？优化器如何在这两者间切换？
  - **答案**：`#dbg_declare` 描述变量的**地址**（通常 `-O0` 下变量住在 `alloca` 里）；`#dbg_value` 描述变量的**值**。当优化把变量从内存提升为 SSA 值（mem2reg）时，会删掉 `#dbg_declare`、改插 `#dbg_value` 跟踪其在寄存器/SSA 中的值。

---

### 4.2 DIBuilder：构造调试元数据的脚手架

#### 4.2.1 概念说明

手写上面那些 `!DIXxx(...)` 节点既繁琐又容易出错（结构体自引用、循环、临时节点、唯一化）。`DIBuilder`（声明见 [llvm/include/llvm/IR/DIBuilder.h:43-1259](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/llvm/include/llvm/IR/DIBuilder.h#L43-L1259)）是 LLVM 提供的**便捷构造门面**，地位类似于构造指令的 `IRBuilder`（u3-l4）：你调一个个 `createXxx` 方法，它在背后正确地建出元数据节点并理清它们的关系。

它的核心职责有三：

1. 提供「按种类构造」的工厂方法（`createFile`/`createCompileUnit`/`createBasicType`/`createFunction`/`createAutoVariable`/`insertDbgValue`…）。
2. 处理**自引用类型与临时节点**——比如链表节点 `struct Node { struct Node *next; }` 在构造时 `Node` 还没定义完就要引用自己，`DIBuilder` 用「临时节点 + 延迟解析」解决循环。
3. 在 `finalize()` 时把散落的子表（枚举类型、保留类型、全局变量、宏、各 subprogram 的本地变量）汇总回 CU/subprogram。

#### 4.2.2 核心流程

用 `DIBuilder` 给一个函数挂调试信息的典型步骤是：

```
1. DIBuilder DB(M);                                   // 绑定到 Module
2. DIFile *F    = DB.createFile("add.c", "/home/me");
3. DICompileUnit *CU = DB.createCompileUnit(C99, F, "clang", /*isOpt=*/false, ...);
4. DISubroutineType *ST = DB.createSubroutineType(...);
5. DISubprogram *SP = DB.createFunction(CU, "add", "add", F, line, ST, ...);
6. DILocalVariable *A = DB.createParameterVariable(SP, "a", /*ArgNo=*/1, F, line, IntTy);
7. DIExpression *Expr = DB.createExpression();
8. DILocation *Loc = DILocation::get(Ctx, line, col, SP);
9. DB.insertDeclare(AllocaOfA, A, Expr, Loc, InsertPt); // 贴上 #dbg_declare
...
10. DB.finalize();                                     // 收尾、解析循环
```

其中第 5 步 `createFunction` 内部会决定节点是「distinct（唯一身份）」还是「uniqued（按结构去重）」——**定义**用 distinct（因为每个函数定义是独一无二的），声明用 uniqued。第 9 步现代实现直接产出 `DbgVariableRecord`（调试记录），而非旧的 `call llvm.dbg.value` 内联函数。

`finalize()` 不可省：它在 CU 上挂回 `replaceEnumTypes`/`replaceRetainedTypes`/`replaceGlobalVariables`/`replaceMacros`，并 `finalizeSubprogram` 把每个函数收集到的本地变量写进它的 `retainedNodes`（[llvm/lib/IR/DIBuilder.cpp:74-103](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/llvm/lib/IR/DIBuilder.cpp#L74-L103)）。

#### 4.2.3 源码精读

**(a) `createCompileUnit`：建锚点并登记到 `llvm.dbg.cu`**

```cpp
DICompileUnit *DIBuilder::createCompileUnit(...) {
  assert(!CUNode && "Can only make one compile unit per DIBuilder instance");
  CUNode = DICompileUnit::getDistinct(VMContext, Lang, File, Producer, ...);
  NamedMDNode *NMD = M.getOrInsertNamedMetadata("llvm.dbg.cu");
  NMD->addOperand(CUNode);
  trackIfUnresolved(CUNode);
  return CUNode;
}
```

见 [llvm/lib/IR/DIBuilder.cpp:145-165](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/llvm/lib/IR/DIBuilder.cpp#L145-L165)。两个要点：(1) `assert` 强制一个 `DIBuilder` 只能建一个 CU；(2) CU 用 `getDistinct` 创建（distinct 节点有唯一身份、不去重），并加进模块的 `llvm.dbg.cu` 命名元数据。`trackIfUnresolved`（[DIBuilder.cpp:44-52](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/llvm/lib/IR/DIBuilder.cpp#L44-L52)）把尚未解析的临时节点登记到 `UnresolvedNodes`，供 `finalize` 收尾。

**(b) `createFunction`：定义走 distinct、声明走 uniqued**

```cpp
bool IsDefinition = SPFlags & DISubprogram::SPFlagDefinition;
auto *Node = getSubprogram(/*IsDistinct=*/IsDefinition, VMContext,
                           getNonCompileUnitScope(Context), Name, ...);
AllSubprograms.push_back(Node);
trackIfUnresolved(Node);
```

见 [llvm/lib/IR/DIBuilder.cpp:1057-1074](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/llvm/lib/IR/DIBuilder.cpp#L1057-L1074)，配合辅助 `getSubprogram`（[DIBuilder.cpp:1050-1055](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/llvm/lib/IR/DIBuilder.cpp#L1050-L1055)）。`SPFlagDefinition` 等 `DISPFlags`（[DebugInfoMetadata.h:2300-2317](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/llvm/include/llvm/IR/DebugInfoMetadata.h#L2300-L2317)）记录「是否定义、是否局部于本单元、是否优化、虚函数性」等 subprogram 专属属性。`DISubprogram` 本身派生自 `DILocalScope`（[DebugInfoMetadata.h:2285](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/llvm/include/llvm/IR/DebugInfoMetadata.h#L2285)），所以函数同时「是一个作用域」——本地变量的 `scope` 就指向它。

**(c) 局部变量与位置表达式**

`createAutoVariable`（[DIBuilder.cpp:1004-1014](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/llvm/lib/IR/DIBuilder.cpp#L1004-L1014)）/`createParameterVariable`（[DIBuilder.cpp:1016-1026](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/llvm/lib/IR/DIBuilder.cpp#L1016-L1026)）都断言 `Scope` 必须是 `DILocalScope`，并把节点登记到该 subprogram 的跟踪表（保证 `finalizeSubprogram` 时能汇总）。`createExpression`（[DIBuilder.cpp:1046-1048](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/llvm/lib/IR/DIBuilder.cpp#L1046-L1048)）只是 `DIExpression::get` 的薄包装；若要描述一个无地址的常量值，可用 `createConstantValueExpression`（[DIBuilder.h:980-983](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/llvm/include/llvm/IR/DIBuilder.h#L980-L983)），它生成 `DW_OP_constu, Val, DW_OP_stack_value`。

**(d) `insertDbgValue`/`insertDeclare`：生成调试记录**

```cpp
DbgRecord *DIBuilder::insertDbgValue(Value *Val, DILocalVariable *VarInfo,
                                     DIExpression *Expr, const DILocation *DL,
                                     InsertPosition InsertPt) {
  DbgVariableRecord *DVR =
      DbgVariableRecord::createDbgVariableRecord(Val, VarInfo, Expr, DL);
  insertDbgVariableRecord(DVR, InsertPt);
  return DVR;
}
```

见 [llvm/lib/IR/DIBuilder.cpp:1197-1204](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/llvm/lib/IR/DIBuilder.cpp#L1197-L1204)。注意它**不再造 `call` 指令**，而是造一个 `DbgVariableRecord`，由 `BasicBlock::insertDbgRecordBefore` 插入（[DIBuilder.cpp:1239-1249](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/llvm/lib/IR/DIBuilder.cpp#L1239-L1249)）。`insertDeclare`（[DIBuilder.cpp:1206-1219](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/llvm/lib/IR/DIBuilder.cpp#L1206-L1219)）额外断言「位置的 subprogram 必须与变量的 subprogram 一致」，即 4.1 里提到的硬约束。这是 LLVM 近年把 debug intrinsics 改造成「非指令的调试记录」（见 `llvm/docs/RemoveDIsDebugInfo.md`）的体现——记录不再是 `Instruction`，从而更干净地从指令流里分离出来。

#### 4.2.4 代码实践：源码阅读型——跟踪 Clang CodeGen 的构造链

1. **实践目标**：看清 Clang 前端如何驱动 `DIBuilder` 建 CU，把 4.2.2 的步骤对应到真实源码。
2. **操作步骤**：
   - 打开 `clang/lib/CodeGen/CGDebugInfo.h`，找到 `class CGDebugInfo`（[clang/lib/CodeGen/CGDebugInfo.h:59](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/clang/lib/CodeGen/CGDebugInfo.h#L59)），留意它持有 `llvm::DIBuilder DBuilder`（[CGDebugInfo.h:67](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/clang/lib/CodeGen/CGDebugInfo.h#L67)）与 `llvm::DICompileUnit *TheCU`（[CGDebugInfo.h:68](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/clang/lib/CodeGen/CGDebugInfo.h#L68)）。
   - 跳到 `CGDebugInfo::CreateCompileUnit`（声明 [CGDebugInfo.h:763](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/clang/lib/CodeGen/CGDebugInfo.h#L763)），在 `clang/lib/CodeGen/CGDebugInfo.cpp` 第 898 行附近看到 `TheCU = DBuilder.createCompileUnit(...)`（[CGDebugInfo.cpp:898](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/clang/lib/CodeGen/CGDebugInfo.cpp#L898)）。
3. **需要观察的现象**：Clang 把「源语言、文件、是否优化、命令行 flags」等信息收集后，一次性传给 `DIBuilder::createCompileUnit`——这正是 4.2.3 (a) 那段断言与登记的入参来源。
4. **预期结果**：你能在脑中画出 `CGDebugInfo → DIBuilder::createCompileUnit → DICompileUnit::getDistinct → llvm.dbg.cu` 这条调用链。
5. 运行结果「待本地验证」（本实践为源码阅读，无需编译运行）。

#### 4.2.5 小练习与答案

- **练习 1**：为什么 `createFunction` 对「定义」用 distinct、对「声明」用 uniqued？
  - **答案**：定义是独一无二的实体（每个函数只定义一次），用 distinct 保证身份唯一、可被 `trackIfUnresolved` 跟踪；声明是可重复的结构化描述，按结构 uniqued 能去重、节省空间。
- **练习 2**：若忘记调用 `finalize()` 会怎样？
  - **答案**：CU 的 `enumTypes`/`retainedTypes`/`globalVariables`/`macros` 等列表不会被回填，各 subprogram 收集到的本地变量也不会写入 `retainedNodes`，导致下游（如 DWARF 发射）看不到这些类型/变量，最终 DWARF 信息残缺。

---

### 4.3 从 IR 到 DWARF 再到 LLDB：闭环往返链

#### 4.3.1 概念说明

调试信息描述层用的是 **LLVM 自己的元数据格式**（受 DWARF 启发，但不等于 DWARF）。它最终要变成**目标文件里的标准 DWARF 段**（或 Windows 上的 CodeView），调试器才能消费。官方文档明确列出两个后端消费者：`DwarfDebug`（产出供 GDB/LLDB 等使用的 DWARF）与 `CodeViewDebug`（产出供 Visual Studio/WinDBG 使用的 CodeView）（[llvm/docs/SourceLevelDebugging.md:60-66](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/llvm/docs/SourceLevelDebugging.md#L60-L66)）。

这就形成了完整往返链：

```
源码
 │  Clang 前端（CGDebugInfo 驱动 DIBuilder）
 ▼
IR 元数据（DINode 图 + #dbg 记录 + !dbg 位置）
 │  后端 AsmPrinter 选中 DwarfDebug（或 CodeViewDebug）作为 handler
 ▼
目标文件中的 DWARF 段（.debug_info / .debug_line / .debug_abbrev …）
 │  调试器 LLDB 的 SymbolFileDWARF 插件读回
 ▼
源码级调试视图（函数、类型、变量、断点、单步）
```

关键洞察：**描述层（DINode）与发射层（DIE）是同构的两棵树**。后端的工作本质是「逐节点翻译」——把 `DICompileUnit` 翻成 `DW_TAG_compile_unit` 的 DIE、把 `DISubprogram` 翻成 `DW_TAG_subprogram` 的 DIE，以此类推。调试器则是反向「逐 DIE 解析」重建出能被用户理解的符号。

#### 4.3.2 核心流程

后端发射由 **`AsmPrinter`**（u6 讲过的 MC 层之前的「打印机」）驱动：

1. `AsmPrinter` 在初始化时，根据目标/对象格式决定创建哪个 debug handler——ELF/Mach-O 上建 `DwarfDebug`，COFF 上建 `CodeViewDebug`。
2. handler 被加入 `Handlers` 列表；随后 AsmPrinter 在模块/函数生命周期的各个回调点（`beginModule`/`endModule`/`beginFunction`/`endFunction`）通知所有 handler。
3. `DwarfDebug` 把 CU 的 `DINode` 树翻译成 `DIE` 树：每个编译单元对应一个 `DwarfCompileUnit`（继承自 `DwarfUnit`），它有 `getOrCreateSubprogramDIE`、`getOrCreateTypeDIE` 等方法，按需把元数据节点物化成 DIE。
4. 行号表来自每条 `MachineInstr` 携带的 `DebugLoc`（源自 `DILocation`）；变量位置来自对 `#dbg_value`/`#dbg_declare` 记录的「位置翻译」（把 SSA 值/地址映射成寄存器或栈偏移）。
5. 最终这些 DIE 与行号表被编码进目标文件的 DWARF 段。

LLDB 一侧，`SymbolFileDWARF` 插件在用户调试时**惰性**地把 DIE 读回：当用户要函数列表就调 `ParseFunctions`，要类型就调 `ParseTypes`，要某作用域的变量就调 `ParseVariablesForContext`——按需物化、避免一次性解析整个 DWARF。

#### 4.3.3 源码精读

**(a) AsmPrinter 选择并注册 debug handler**

```cpp
DwarfDebug *AsmPrinter::createDwarfDebug() { return new DwarfDebug(this); }
```

见 [llvm/lib/CodeGen/AsmPrinter/AsmPrinter.cpp:514-515](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/llvm/lib/CodeGen/AsmPrinter/AsmPrinter.cpp#L514-L515)。注册处则在 [AsmPrinter.cpp:635-639](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/llvm/lib/CodeGen/AsmPrinter/AsmPrinter.cpp#L635-L639)：COFF 目标 `Handlers.push_back(std::make_unique<CodeViewDebug>(this))`（635 行），否则 `DD = createDwarfDebug(); Handlers.push_back(std::unique_ptr<DwarfDebug>(DD))`（638-639 行）。这段就是「按对象格式二选一」的物理体现。

**(b) `DwarfDebug`：发射引擎**

`DwarfDebug` 继承自 `DebugHandlerBase`（[llvm/lib/CodeGen/AsmPrinter/DwarfDebug.h:352](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/llvm/lib/CodeGen/AsmPrinter/DwarfDebug.h#L352)），以模块/函数生命周期方法驱动：

- `beginModule(Module *M)`（声明 [DwarfDebug.h:784](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/llvm/lib/CodeGen/AsmPrinter/DwarfDebug.h#L784)）：模块开始，扫描 CU、为每个 CU 建 `DwarfCompileUnit` 与初始 DIE。
- `endModule()`（声明 [DwarfDebug.h:787](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/llvm/lib/CodeGen/AsmPrinter/DwarfDebug.h#L787)，实现 [DwarfDebug.cpp:1473](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/llvm/lib/CodeGen/AsmPrinter/DwarfDebug.cpp#L1473)）：模块结束，完成 DIE 树、写各 DWARF 段。
- `beginFunctionImpl(const MachineFunction *MF)`（[DwarfDebug.cpp:2800](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/llvm/lib/CodeGen/AsmPrinter/DwarfDebug.cpp#L2800)）：逐函数处理，收集变量位置与行号。

DIE 树的构建落在 `DwarfUnit`/`DwarfCompileUnit`：`getOrCreateSubprogramDIE`/`getOrCreateTypeDIE` 等方法把 `DISubprogram`/`DIType` 元数据逐个翻译成 DIE——这就是「同构两棵树」翻译的核心场所。

**(c) LLDB 的 DWARF 读取插件**

`class SymbolFileDWARF : public SymbolFileCommon`（[lldb/source/Plugins/SymbolFile/DWARF/SymbolFileDWARF.h:66](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/Plugins/SymbolFile/DWARF/SymbolFileDWARF.h#L66)）是 LLDB 解析 DWARF 的入口。它按需提供符号：

- `ParseFunctions`（[SymbolFileDWARF.h:117](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/Plugins/SymbolFile/DWARF/SymbolFileDWARF.h#L117)）
- `ParseTypes`（[SymbolFileDWARF.h:131](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/Plugins/SymbolFile/DWARF/SymbolFileDWARF.h#L131)）
- `ParseVariablesForContext`（[SymbolFileDWARF.h:139](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/Plugins/SymbolFile/DWARF/SymbolFileDWARF.h#L139)）

同目录下 `DWARFDebugInfo.cpp`/`DWARFDIE.cpp`/`DIERef.cpp`/`DWARFASTParserClang.cpp` 等协同：先把 DIE 索引化（`DIERef`），再在解析时用 `DWARFASTParserClang` 把 DIE 重建为 Clang AST 节点，供 LLDB 的表达式求值器复用。这正好闭环——前端用 Clang AST → IR metadata → DWARF，调试器又把 DWARF → Clang AST 还原回来。

#### 4.3.4 代码实践：跑通「`clang -g` → DWARF → 对应源码」

1. **实践目标**：亲手产出 DWARF，并用 `llvm-dwarfdump` 把它对应回源码，完成一次往返观察。
2. **操作步骤**：
   - 仍用 4.1 的 `add.c`，先 `clang -g -O0 add.c -o add`（注意这次 `-o add` 产出可执行文件，而不是只到 IR）。
   - 运行 `llvm-dwarfdump add` 查看全部 DWARF 段；或更聚焦地 `llvm-dwarfdump --debug-info add` 看 DIE 树、`llvm-dwarfdump --debug-line add` 看行号表。
3. **需要观察的现象**：
   - 在 `.debug_info` 里找到一个 `DW_TAG_compile_unit`，其 `DW_AT_name` 应为 `add.c`、`DW_AT_language` 应为 `DW_LANG_C99`，对应 4.1 里的 `!DICompileUnit`。
   - 其下应有 `DW_TAG_subprogram`，`DW_AT_name` 为 `"add"`，并有 `DW_AT_decl_line`（对应 `DISubprogram` 与 `DILocation` 的行号）。
   - 该 subprogram 下应有 `DW_TAG_formal_parameter`（参数 `a`、`b`）和 `DW_TAG_variable`（`sum`），分别对应 4.1 的 `DILocalVariable`。
   - `.debug_line` 行号表把每条机器指令地址映射到 `(文件, 行, 列)`，正是 `DILocation` 经 `DwarfDebug` 翻译的产物。
4. **预期结果**：你能在 `llvm-dwarfdump` 输出里逐一指认 CU/subprogram/参数/变量/行号表，并把每个 DIE 关联回 IR 里的 `!DIXxx` 节点与源码行。
5. 若本机缺 `clang`/`llvm-dwarfdump`，可改为在仓库 `llvm/test/DebugInfo/` 下找一个 `.s` 或 `.o` 测试样本阅读其 `CHECK` 行，理解 DWARF 字段含义。运行结果「待本地验证」。

#### 4.3.5 小练习与答案

- **练习 1**：为什么 LLVM 不直接把 DWARF 写进 IR，而要先用自己的 `DINode` 元数据？
  - **答案**：IR 要与具体调试格式解耦——同一份元数据既能发 DWARF（ELF/Mach-O）又能发 CodeView（COFF），还能在优化时被查询/改写（如 `findDVRDeclares`）。直接写死 DWARF 会把优化器绑死到单一格式。
- **练习 2**：`DwarfDebug` 的 `endModule` 与 `beginFunctionImpl` 各负责什么？
  - **答案**：`beginFunctionImpl` 在进入每个函数时收集该函数的变量位置与行号；`endModule` 在整个模块结束时完成 DIE 树并写出所有 DWARF 段。前者是「逐函数增量」，后者是「模块级收尾」。
- **练习 3**：LLDB 的 `SymbolFileDWARF` 为什么是「按需解析」而不是一次性把整个 DWARF 读进来？
  - **答案**：大型程序的 DWARF 体积远超可执行代码本身，一次性解析会带来巨大的内存与启动延迟。`ParseFunctions`/`ParseTypes`/`ParseVariablesForContext` 等按需物化策略只解析用户当前真正需要的符号。

---

## 5. 综合实践

把本讲三个模块串起来，完成一次「全链路追踪」：

1. 写一个稍微复杂点的 C 程序，至少含：一个结构体、一个带局部变量的函数、一次函数调用。例如：

   ```c
   struct Point { int x; int y; };
   int dist_sq(struct Point *p) {
       int dx = p->x;
       int dy = p->y;
       return dx * dx + dy * dy;
   }
   ```

2. 用 `clang -g -O0 -S -emit-llvm` 生成 `.ll`，完成下列指认（对应 **4.1**）：
   - 找到 `struct Point` 的 `DICompositeType`，指认其成员 `DIDerivedType`（成员名、偏移 `offsetInBits`）。
   - 找到参数 `p`、局部变量 `dx`/`dy` 的 `DILocalVariable`，以及对应的 `#dbg_declare`/`#dbg_value` 记录。
   - 追一条 `!dbg !N`，确认它指向的 `DILocation` 的 `line` 与源码一致。
3. 用 `clang -g -O0` 生成可执行文件，再用 `llvm-dwarfdump --debug-info`（对应 **4.3**）：
   - 找到 `DW_TAG_structure_type`（`Point`）及其 `DW_TAG_member`，验证成员偏移与 IR 里的 `DIDerivedType` 一致。
   - 找到 `DW_TAG_subprogram`（`dist_sq`）及其参数/变量 DIE。
4.（进阶，可选）开 `lldb` 加载该程序，在 `dist_sq` 设断点运行到断点，用 `frame variable` 查看变量——此时 LLDB 正是通过 `SymbolFileDWARF` 的 `ParseVariablesForContext` 把 DWARF 还原成你看到的变量视图。
5. 写一段话，把「源码 → `CGDebugInfo`/`DIBuilder` → IR 元数据 → `DwarfDebug` → DWARF → `SymbolFileDWARF` → 调试器视图」这条链上你亲眼见到的对应关系串起来。

> 若本机无 clang/llvm-dwarfdump/lldb，则第 2–3 步改在仓库 `llvm/test/DebugInfo/` 与 `lldb/test/` 下挑选已有样本完成指认；运行类步骤标注「待本地验证」。

## 6. 本讲小结

- 调试信息是一条**与主计算流并行的旁路**：寄生在 IR 元数据上，不影响代码生成，由官方文档确立了「低侵入、与优化良好交互、语言无关、可发标准格式」四项设计原则。
- 描述层的根基是带 DWARF tag 的 `DINode`，其下 `DIScope`（`DIFile`/`DICompileUnit`/`DISubprogram`/`DILexicalBlock`/`DIType`…）构成作用域与类型树；`DICompileUnit` 是整图锚点，挂在 `llvm.dbg.cu` 上。
- 挂载层有两件东西：贴在每条指令上的 `DILocation`（行/列/作用域/内联链，产出 `.debug_line`），以及把 SSA 值/地址映射到源变量的 `#dbg_value`/`#dbg_declare` 调试记录（配 `DILocalVariable`+`DIExpression`）。
- `DIBuilder` 是构造门面：`createCompileUnit` 建锚点、`createFunction` 建 subprogram、`insertDbgValue`/`insertDeclare` 造调试记录，最后必须 `finalize()` 回填各列表并解析自引用循环。
- 发射侧由 `AsmPrinter` 按对象格式二选一注册 `DwarfDebug`（ELF/Mach-O）或 `CodeViewDebug`（COFF）；`DwarfDebug` 把 `DINode` 树翻译成同构的 DIE 树写入目标文件。
- 消费侧 LLDB 的 `SymbolFileDWARF` 插件按需（`ParseFunctions`/`ParseTypes`/`ParseVariablesForContext`）把 DIE 读回、甚至重建为 Clang AST，完成「编码—解码」往返闭环。

## 7. 下一步学习建议

- **深入优化与调试信息的交互**：阅读 [llvm/docs/HowToUpdateDebugInfo.md](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/llvm/docs/HowToUpdateDebugInfo.md)，理解写一个会改写 IR 的 Pass 时该如何正确维护 `#dbg_value` 与 `DILocation`。
- **指令引用调试信息（InstrRef）**：阅读 [llvm/docs/InstrRefDebugInfo.md](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/llvm/docs/InstrRefDebugInfo.md)，了解 LLVM 用「指令引用」取代传统 `DW_OP_entry_value` 来在优化后更可靠地追踪变量位置。
- **Assignment Tracking**：阅读 `llvm/docs/AssignmentTracking/AssignmentTracking.md`，了解基于 `#dbg_assign` 的更精细变量位置模型。
- **DWARF 发射实现**：精读 `llvm/lib/CodeGen/AsmPrinter/DwarfCompileUnit.cpp` 与 `DwarfUnit.cpp` 的 `getOrCreateSubprogramDIE`/`getOrCreateTypeDIE`，看清 `DINode → DIE` 翻译细节。
- **LLDB 侧**：精读 `lldb/source/Plugins/SymbolFile/DWARF/DWARFASTParserClang.cpp`，理解 DIE 如何被重建为可在表达式求值中复用的 Clang AST。
- 下一讲 **u9-l4「添加一个新后端」**会回到后端工程实践；本讲建立的「元数据/对象格式/插件」视角将帮助你理解新后端在 MC 层与调试信息发射上需要做什么。
