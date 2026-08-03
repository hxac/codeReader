# MCPlus：在 MCInst 之上挂载元数据的扩展机制

## 1. 本讲目标

BOLT 处理的是**已经编译完成的机器指令**（`MCInst`），但优化过程又需要给每条指令额外挂上很多「侧信息」：这条指令是不是尾调用？它用的跳转表地址是多少？它的运行时分支计数是多少？这些信息在 `MCInst` 本身里并没有地方可放。

本讲要讲清楚 BOLT 解决这个问题的核心创新——**MCPlus**。读完本讲你应该能够：

- 说清楚 MCPlus 是怎样在 `MCInst` 末尾「追加额外 operand」来承载元数据（annotation，注释）的，以及用什么 operand 类型标记注释的起点。
- 区分**一等注释（first-class）**和**补充注释（supplement）**：前者影响程序语义、删了就出错；后者可随时丢弃、删了只影响优化质量。
- 理解 `MCPlusBuilder` 作为「目标无关的注释读写接口」的角色，以及 `allocator id` 如何在多线程并行时避免内存分配冲突。

本讲承接 u2-l2（`BinaryFunction` 与状态机）。`MCInst` 是 `BinaryFunction` 指令表里存放的元素，而 MCPlus 正是把这些裸指令变成「带侧信息的可优化对象」的关键。

## 2. 前置知识

在进入 MCPlus 之前，需要先建立几个直觉。

**什么是 MCInst / MCOperand？**
LLVM 的 MC 层（Machine Code 层）用 `MCInst` 表示一条机器指令，用 `MCOperand` 表示指令的一个操作数。一条指令由若干 operand 组成，例如 `add rax, rbx` 有两个 operand（`rax`、`rbx`）。`MCOperand` 可以是寄存器（`isReg()`）、立即数（`isImm()`）、表达式（`isExpr()`）、子指令（`isInst()`，用于某些 VLIW/双字架构）等。

**为什么不能新建一个类来存侧信息？**
BOLT 几乎所有数据结构和算法都直接操作 `MCInst`（来自 LLVM MC 层，无法随意改其内存布局）。如果为「带侧信息的指令」单独造一个类，就要复制/包装大量 LLVM 既有代码。MCPlus 的巧妙之处在于：**复用 `MCInst` 自带的 operand 列表**，在末尾追加几个「不会参与汇编」的 operand 当作元数据容器，既不破坏 `MCInst` 的二进制兼容，又能承载任意注释。

**「一等」与「补充」的区别（先记住结论）**
- **一等注释**：删掉它会导致程序行为改变（语义错误）。例如「这条 call 有异常处理 landing pad」「这条 jmp 是跳转表」。
- **补充注释**：删掉它程序依然正确运行，只是优化决策可能变差。例如「这条内存访问指令的 profile 计数」「调试用的偏移量」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `include/bolt/Core/MCPlus.h` | MCPlus 的核心定义：注释的存储原理、`MCAnnotation`/`MCSimpleAnnotation` 类、`isAnnotationSentinel`、`getNumPrimeOperands` 等内联工具函数，以及顶部那段权威说明注释。 |
| `include/bolt/Core/MCPlusBuilder.h` | `MCPlusBuilder` 类声明：目标无关的注释创建/解析/修改接口，64 位编码函数、`AnnotationAllocator` 与 allocator id 机制、`addAnnotation`/`getAnnotationAs` 等模板。 |
| `lib/Core/MCPlusBuilder.cpp` | `MCPlusBuilder` 的实现：`hasAnnotation`、`removeAnnotation`、`stripAnnotations`、`printAnnotations`，以及 jump table / EH / tail call 等一等注释的具体读写。 |

辅助认知（本讲会少量引用，但不深入）：`lib/Profile/DataReader.cpp`、`lib/Passes/ReorderData.cpp` 展示了「补充注释」的真实用法；X86/AArch64/RISCV 的 `*MCPlusBuilder.cpp` 是目标相关实现（u7-l1 详讲）。

## 4. 核心概念与源码讲解

### 4.1 MCPlus 注释的存储原理：kInst sentinel 与 64 位编码

#### 4.1.1 概念说明

MCPlus 的全部秘密可以用一句话概括：**在 `MCInst` 已有的 operand 列表末尾，追加一组「伪 operand」来当注释容器**。这些追加的 operand 不参与真正的指令汇编，只供 BOLT 内部读写。

为了让「真 operand」和「注释 operand」能被区分开，需要一个**哨兵（sentinel）**来标记「从这里开始是注释」。MCPlus 选择的哨兵是：一个 `isInst()` 为真、但其内部 inst 指针为 `nullptr` 的 operand——即「空的子指令 operand」。这种 operand 在真实指令里不可能合法出现（真实的子指令 operand 指针非空，例如 Hexagon duplex 指令），所以可以安全地拿来当标记。

#### 4.1.2 核心流程

一条带注释的 `MCInst`，其 operand 列表在逻辑上是这样的布局：

```
[ prime operands ... | sentinel | annotation imm 0 | annotation imm 1 | ... ]
                      ^          ^
                      |          第一个注释开始 (index = FirstAnnotationOpIndex)
                      kInst(null) 哨兵，标记注释起点
```

读写注释的流程：

1. **判断有无注释**：从 operand 列表末尾向前扫，寻找 sentinel；若不存在，则该指令无注释，所有 operand 都是 prime operand。
2. **定位注释起点**：sentinel 的下一个 operand 就是第一条注释。
3. **每条注释是一个 immediate operand**，其 64 位值被拆成两段编码：高 8 位是**注释种类索引（Index）**，低 56 位是**注释值（Value）**。
4. **读注释**：按 Index 在 [sentinel+1, 末尾) 区间内线性查找匹配的 immediate，再解码出 Value。
5. **写注释**：若已有同 Index 的注释则原地改写其 imm；否则在末尾追加一个新的 imm operand（首次还需先追加 sentinel）。

#### 4.1.3 源码精读

**顶部权威说明**。MCPlus.h 顶部注释直接讲清了存储原理与两类注释的划分，这是本讲最权威的一手资料：

[include/bolt/Core/MCPlus.h:L33-L49](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/include/bolt/Core/MCPlus.h#L33-L49) —— 说明：注释通过「不参与汇编的额外 operand」附加到 `MCInst`；**第一个额外 operand 必须是 `kInst` 类型且值为空（nullptr）**，作为注释起点哨兵；其余注释 operand 都是 Immediate 类型，把信息编码进立即数的值里。

**哨兵判定**。`isAnnotationSentinel` 同时要求 `isInst()` 且 `getInst() == nullptr`：

[include/bolt/Core/MCPlus.h:L116-L121](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/include/bolt/Core/MCPlus.h#L116-L121) —— 说明：判断一个 operand 是不是注释哨兵。注释特意指出「非空的 MCInst operand（如 Hexagon duplex 子指令）是合法的 prime operand」，所以必须用「空 inst」来当哨兵，避免误伤。

**计算 prime operand 数量**。`getNumPrimeOperands` 从末尾向前扫描：

[include/bolt/Core/MCPlus.h:L125-L133](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/include/bolt/Core/MCPlus.h#L125-L133) —— 说明：从最后一个 operand 往前扫，若遇到 sentinel 就返回它的下标（prime 区间为 `[0, sentinel)`）；若遇到一个「既不是 inst 也不是 imm」的真实 operand（寄存器/表达式），说明根本没注释，返回全部 operand 数。

**64 位编码**。Index 与 Value 被打包进一个 64 位立即数：

[include/bolt/Core/MCPlusBuilder.h:L108-L126](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/include/bolt/Core/MCPlusBuilder.h#L108-L126) —— 说明：`encodeAnnotationImm` 把 Value 截断到低 56 位、把 Index 放进高 8 位；`extractAnnotationIndex` 取高 8 位；`extractAnnotationValue` 取低 56 位并做符号扩展。如果 Value 超出 56 位表示范围，会直接 `report_fatal_error`。

64 位立即数的位布局如下：

| bit 63 … 56 | bit 55 … 0 |
| --- | --- |
| Index（注释种类，8 位） | Value（注释值，56 位） |

8 位 Index 最多支持 256 种注释；56 位 Value 足以装下一个指针（平台上指针通常 ≤ 56 位）或一个地址/偏移量。

**写注释**。`setAnnotationOpValue` 负责把 (Index, Value) 落到 operand 列表上：

[include/bolt/Core/MCPlusBuilder.h:L151-L170](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/include/bolt/Core/MCPlusBuilder.h#L151-L170) —— 说明：若指令还没有任何注释（找不到 sentinel），就先追加一个 `createInst(nullptr)` 哨兵，再追加编码后的 imm；若已有同 Index 的注释 imm，则原地改写；否则在末尾追加一条新 imm。

**定位注释起点**：

[include/bolt/Core/MCPlusBuilder.h:L128-L137](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/include/bolt/Core/MCPlusBuilder.h#L128-L137) —— 说明：`getFirstAnnotationOpIndex` 返回 sentinel 之后第一个注释的下标；若无注释返回 `nullopt`。

#### 4.1.4 代码实践

> **实践目标**：亲手验证「kInst(null) 哨兵 + 64 位 imm 编码」这套存储机制。

1. 打开 [include/bolt/Core/MCPlus.h:L33-L61](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/include/bolt/Core/MCPlus.h#L33-L61)，通读顶部注释。
2. 找到 `isAnnotationSentinel`（L119），确认它要求两个条件**同时**成立。
3. 打开 [include/bolt/Core/MCPlusBuilder.h:L108-L126](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/include/bolt/Core/MCPlusBuilder.h#L108-L126)，对照 `encodeAnnotationImm` 画出 64 位立即数的位布局。
4. 跟踪 `setAnnotationOpValue`（L151）在「无注释」分支里先 `addOperand(MCOperand::createInst(nullptr))` 再 `addOperand(MCOperand::createImm(...))` 的两步动作。

**需要观察的现象**：第一条注释写入时，operand 列表会增长 2（sentinel + imm）；后续新增同 Index 不增长（原地改写），新增不同 Index 才再增长 1。

**预期结果**：你能用一句话说清「MCPlus 用哪种 operand 类型、什么值来标记注释起点」——答案是 `kInst`（`isInst()`）类型、`nullptr`（空 inst）值的 operand。

待本地验证：若想直观看到 operand 列表的变化，可在调试器里对一条已带注释的指令观察 `Inst.getNumOperands()` 与各 operand 的 `isInst()/isImm()`，确认末尾确实是「空 inst + 若干 imm」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 MCPlus 选「空的子指令 operand」当哨兵，而不选一个特殊的立即数（比如 `-1`）？

> **答案**：立即数在真实指令里非常常见（如 `mov rax, 42`），用 `-1` 当哨兵会和合法的立即数 operand 混淆，`getNumPrimeOperands` 无法可靠区分。而「`isInst()` 为真但 inst 指针为空」的 operand 在合法汇编里不会出现（真实的子指令 operand 指针非空），所以是天然无歧义的标记。

**练习 2**：`getNumPrimeOperands` 从末尾向前扫，遇到「既不是 inst 也不是 imm」的 operand 就直接返回「全部 operand 数」，为什么这是对的？

> **答案**：注释区只可能由 imm（注释值）和 inst（仅哨兵这一个，且为空）组成。一旦从末尾回扫遇到一个寄存器/表达式这种「真实」operand，说明此前没有哨兵、整条指令没有任何注释，因此 prime operand 就是全部 operand。

### 4.2 两类注释：一等注释（影响语义）vs 补充注释（可丢弃）

#### 4.2.1 概念说明

注释按「删掉是否影响正确性」被显式分成两组：

- **一等注释（first-class）**：用**预留的固定索引**（reserved index）表示，影响指令语义。例如「这条 call 关联了哪个异常处理 landing pad」「这条间接跳转是某个跳转表」。这些信息一旦丢失，BOLT 重写出来的二进制就会行为错误（异常无法传播、跳转表无法识别）。
- **补充注释（supplement）**：用**按名字动态分配的索引**（generic index）表示，是「附加情报」，丢弃不影响程序正确性。典型如 profile 计数、内存访问画像、调试用的偏移量、pass 内部的临时标记。

MCPlus.h 的 `MCAnnotation::Kind` 枚举把所有**一等注释**列在前半部分，并用 `kGeneric` 作为「补充注释」的起点分界：

[include/bolt/Core/MCPlus.h:L64-L81](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/include/bolt/Core/MCPlus.h#L64-L81) —— 说明：枚举前半段（`kEHLandingPad`、`kJumpTable`、`kTailCall`、`kConditionalTailCall`、`kOffset`、`kLabel`、`kSize` 等）都是影响语义的一等注释；最后的 `kGeneric` 是「第一个补充注释」的起点，所有补充注释的索引都 `>= kGeneric`。

补充注释的索引不是写死的，而是按名字**懒分配**的——第一次用到某个名字时才分配一个新索引：

[include/bolt/Core/MCPlusBuilder.h:L2254-L2264](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/include/bolt/Core/MCPlusBuilder.h#L2254-L2264) —— 说明：`getOrCreateAnnotationIndex(Name)` 先查 `AnnotationNameIndexMap`，没找到就在互斥锁保护下分配一个新索引（= 已有名字数 + `kGeneric`），并把名字记进 `AnnotationNames`。

#### 4.2.2 核心流程

两类注释在「存什么」上有细微但关键的差别：

| 维度 | 一等注释 | 补充注释 |
| --- | --- | --- |
| 索引来源 | 预留枚举（`kJumpTable` 等，固定值） | 按名字动态分配（`>= kGeneric`） |
| 值字段装什么 | 通常是**小数据本身**（地址、偏移、bool、`MCSymbol*` 指针） | 通常是一个**指向 arena 对象的指针**（可承载任意复杂类型） |
| 写入入口 | `setAnnotationOpValue(Inst, Kxxx, value)` | `addAnnotation<ValueType>(Inst, "Name", val)` 模板 |
| 丢弃后果 | **语义错误**（程序行为改变） | **仅优化质量下降**（程序仍正确） |
| 生命周期 | 全程保留，发射前仍可能需要 | 可随时 `removeAnnotation` / `stripAnnotations` |

补充注释存「复杂类型」的机制是 `addAnnotation` 模板：它在 arena 上 `new` 一个 `MCSimpleAnnotation<ValueType>` 对象，把**对象指针**塞进 imm 的 Value 字段（`reinterpret_cast<int64_t>(A)`）：

[include/bolt/Core/MCPlusBuilder.h:L2268-L2283](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/include/bolt/Core/MCPlusBuilder.h#L2268-L2283) —— 说明：补充注释的 Value 字段实际存的是一个 `MCSimpleAnnotation<ValueType>*` 指针；`assert(Index >= kGeneric)` 强制补充注释只能用 `kGeneric` 及以上的索引。

#### 4.2.3 源码精读

**一等注释的真实例子——跳转表**。`setJumpTable` 用预留索引 `kJumpTable` 直接存跳转表地址：

[lib/Core/MCPlusBuilder.cpp:L271-L278](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Core/MCPlusBuilder.cpp#L271-L278) —— 说明：跳转表地址走一等注释 `kJumpTable`（值就是地址本身）；而同一个跳转表指令的「索引寄存器」`JTIndexReg` 却用**补充注释**（按名字 `"JTIndexReg"` 存 `uint16_t`）。一个逻辑特征被拆成两类注释，正体现了「影响语义的用一等、辅助细节用补充」的设计。

**补充注释的真实例子——内存访问 profile**。`DataReader` 把 profile 里的内存访问画像作为**补充注释**挂到指令上：

[lib/Profile/DataReader.cpp:L303-L310](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Profile/DataReader.cpp#L303-L310) —— 说明：`getOrCreateAnnotationAs<MemoryAccessProfile>(Inst, "MemoryAccessProfile")` 把一个含「地址访问信息 + 计数」的结构体作为补充注释挂上去。`ReorderData.cpp` 等下游 pass 再用 `tryGetAnnotationAs` 读出来做数据布局优化（见 [lib/Passes/ReorderData.cpp:L187-L199](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Passes/ReorderData.cpp#L187-L199)）。

**统一删除入口**。`stripAnnotations` 把指令上**所有**注释一次性抹掉（可选保留 tail call 标记）：

[lib/Core/MCPlusBuilder.cpp:L416-L423](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Core/MCPlusBuilder.cpp#L416-L423) —— 说明：内部调 `removeAnnotations(Inst)`，从 sentinel 起把整段注释 operand 擦除。它能安全地「一刀切」，正是因为补充注释删了不影响正确性；而一等注释通常不会走到这里（它们在专门的清理 pass 里被有意识地处理）。

#### 4.2.4 代码实践

> **实践目标**：列举两类注释的真实例子，并论证「把 profile 计数做成补充注释是安全的」。

1. 在 [include/bolt/Core/MCPlus.h:L64-L81](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/include/bolt/Core/MCPlus.h#L64-L81) 里挑出 3 个一等注释（如 `kEHLandingPad`、`kJumpTable`、`kTailCall`）。
2. 在代码库里搜索补充注释的名字，例如用关键字 `"MemoryAccessProfile"`、`"JTIndexReg"`、`"DeleteMe"`，确认它们都通过 `addAnnotation`/`getOrCreateAnnotationAs` 按名字存取。
3. 阅读 [lib/Profile/DataReader.cpp:L303-L310](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Profile/DataReader.cpp#L303-L310)，确认 `MemoryAccessProfile` 是补充注释。

**需要观察的现象**：profile/内存画像这类信息**只被优化 pass 读取**（用来决定怎么排布代码），从不参与指令的字节级编码或控制流重建。

**预期结果（解释为什么 profile 做成补充注释是安全的）**：profile 计数只影响「优化决策的质量」（哪段代码更热、要不要分裂），不参与「程序语义」（控制流、异常处理、跳转表）。即使把所有 profile 注释删光，BOLT 产出的二进制依然能正确运行——只是退化为「按布局无 profile 优化」。因此把 profile 放在可随时丢弃的补充注释里是天然安全的；反之，若把 EH/跳转表这种一等信息也做成可丢弃，就会产出行为错误的二进制。

待本地验证：可在一个 lit 测试里对比「带 profile」与「不带 profile」两次 `llvm-bolt` 的输出，确认产物都能正常运行、只是 `-dyno-stats` 数值不同。

#### 4.2.5 小练习与答案

**练习 1**：同一个跳转表指令，为什么「跳转表地址」用一等注释、「索引寄存器」用补充注释？

> **答案**：跳转表地址是识别/重写该跳转表的关键语义信息，丢了就无法正确处理这个间接跳转，必须用一等注释 `kJumpTable` 保证不被误删；而索引寄存器只是辅助细节（处理过程中需要、发射前可丢弃），用补充注释 `"JTIndexReg"` 更合适，也避免了占用宝贵的预留索引。

**练习 2**：`addAnnotation` 里为什么 `assert(Index >= MCPlus::MCAnnotation::kGeneric)`？

> **答案**：模板化的 `addAnnotation` 专门服务于补充注释（按名字、存复杂类型），用 `assert` 防止调用方误把一等注释的预留索引传进来——一等注释应当走 `setAnnotationOpValue` 那条直接存值的路径，二者职责不同、不能混用。

### 4.3 MCPlusBuilder：目标无关的注释接口与 allocator id

#### 4.3.1 概念说明

直接操作「哨兵 + 64 位 imm」太底层、太容易出错。BOLT 用 `MCPlusBuilder` 把这些细节封装成一套**目标无关**的高层接口：`setJumpTable`、`getEHInfo`、`isTailCall`、`addAnnotation<ValueType>`、`hasAnnotation`……上层 pass 只跟这些接口打交道，不需要知道 imm 怎么编码。

`MCPlusBuilder` 同时也是**注释对象的内存管家**。补充注释需要在堆/arena 上分配 `MCSimpleAnnotation<ValueType>` 对象，这些对象由 `MCPlusBuilder` 内部的 `AnnotationAllocator`（一个 `BumpPtrAllocator` + 一个 `AnnotationPool`）统一分配与回收。

这里有个为**多线程并行**而设计的机制——**allocator id**。BOLT 大量并行处理函数（见 u5-l1 的 `ParallelUtilities`），如果所有线程都往同一个 `BumpPtrAllocator` 里分配，会因锁竞争拖慢速度。为此 `MCPlusBuilder` 维护一个「id → allocator」的映射，每个并行任务可以申请自己的专属 allocator，互不干扰。

#### 4.3.2 核心流程

注释对象的生命周期与 allocator 的关系：

1. **启动**：`MCPlusBuilder` 构造时，预创建一个默认 allocator，其 id = 0。
2. **申请专属 allocator**：并行任务调用 `initializeNewAnnotationAllocator()` 拿到一个新的、自增的 id（1, 2, 3, …）。
3. **按 id 写补充注释**：`addAnnotation(Inst, Index, Val, AllocatorId)` 用指定 id 的 `BumpPtrAllocator` 分配 `MCSimpleAnnotation` 对象。
4. **按 id 回收**：任务结束后调 `freeValuesAllocator(AllocatorId)`，析构该 allocator 池里所有注释对象并 `Reset()` 分配器，一次性释放整块内存。
5. **析构**：`MCPlusBuilder` 析构时 `freeAnnotations()` 把所有 allocator 全部清掉。

#### 4.3.3 源码精读

**默认 allocator 与 allocator 数据结构**：

[include/bolt/Core/MCPlusBuilder.h:L78-L78](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/include/bolt/Core/MCPlusBuilder.h#L78-L78) —— 说明：`AllocatorIdTy` 就是 `uint16_t`，注释 allocator 的 id 类型。

[include/bolt/Core/MCPlusBuilder.h:L96-L105](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/include/bolt/Core/MCPlusBuilder.h#L96-L105) —— 说明：`AnnotationAllocator` = `BumpPtrAllocator`（分配注释对象）+ `unordered_set<MCAnnotation*>`（记录非平凡类型的对象指针，便于析构）；`AnnotationAllocators` 是 id→allocator 的映射；`MaxAllocatorId` 是自增计数器。

**构造时预建 id=0 的默认 allocator**：

[include/bolt/Core/MCPlusBuilder.h:L359-L368](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/include/bolt/Core/MCPlusBuilder.h#L359-L368) —— 说明：构造函数里 `AnnotationAllocators.emplace(0, ...)` 建好默认 allocator 并把 `MaxAllocatorId` 置 1。单线程场景下所有 `addAnnotation` 不传 id 时默认用 0。

**并行任务申请专属 allocator**：

[include/bolt/Core/MCPlusBuilder.h:L380-L383](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/include/bolt/Core/MCPlusBuilder.h#L380-L383) —— 说明：`initializeNewAnnotationAllocator` 插入一个新 allocator，返回它的 id 并自增计数器。并行 pass 拿到这个 id 后，线程内所有注释分配都走自己的 allocator，互不加锁。

**按 id 分配注释对象**：

[include/bolt/Core/MCPlusBuilder.h:L2268-L2283](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/include/bolt/Core/MCPlusBuilder.h#L2268-L2283) —— 说明：`addAnnotation` 根据 `AllocatorId` 取出对应 `AnnotationAllocator`，在其 `ValueAllocator`（BumpPtrAllocator）上 `new` 一个 `MCSimpleAnnotation<ValueType>`；若类型非平凡（`!is_trivial`）还把它登记进 `AnnotationPool` 以便后续析构。

**按 id 回收**：

[include/bolt/Core/MCPlusBuilder.h:L398-L405](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/include/bolt/Core/MCPlusBuilder.h#L398-L405) —— 说明：`freeValuesAllocator` 析构该 id 池里所有注释对象、清空池、`Reset()` 分配器。这就是并行任务结束后「整块释放」的入口。

**查/删注释的统一入口**：

[lib/Core/MCPlusBuilder.cpp:L397-L414](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Core/MCPlusBuilder.cpp#L397-L414) —— 说明：`hasAnnotation` 复用 `getAnnotationOpValue` 判存在性；`removeAnnotation` 在注释区找到匹配 Index 的 imm operand 并 `erase`。注意它们都按 Index 工作，与 allocator id 无关（id 只管「对象在哪分配/何时回收」，不影响「operand 里存什么」）。

#### 4.3.4 代码实践

> **实践目标**：理解 allocator id 为何存在、何时被使用。

1. 阅读 [include/bolt/Core/MCPlusBuilder.h:L359-L368](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/include/bolt/Core/MCPlusBuilder.h#L359-L368) 与 [L380-L383](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/include/bolt/Core/MCPlusBuilder.h#L380-L383)，确认默认 id=0、自增分配新 id。
2. 在代码库里搜索 `initializeNewAnnotationAllocator` 的调用点，看是哪些并行 pass 在用它。
3. 对照 `setJumpTable` 的签名 [lib/Core/MCPlusBuilder.cpp:L271-L278](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Core/MCPlusBuilder.cpp#L271-L278)，注意它把 `AllocId` 透传给 `getOrCreateAnnotationAs<uint16_t>(..., AllocId)`——这就是「补充注释按指定 allocator 分配」的真实例子。

**需要观察的现象**：`AllocId` 参数只在写**补充注释**（`addAnnotation`/`getOrCreateAnnotationAs`）时出现；一等注释（`setJumpTable` 的地址部分、`setEHInfo` 等）完全不需要 allocator id，因为它们直接把值塞进 imm、不在 arena 上分配对象。

**预期结果**：你能解释「为什么一等注释不需要 allocator id、补充注释需要」——一等注释存的是小值/指针本身（直接进 imm，无堆分配），补充注释存的是 arena 对象（需要按线程隔离分配器）。这也呼应了 u5-l1 会讲到的 `runOnEachFunctionWithUniqueAllocId`：并行反汇编/优化时，每个线程拿一个 unique alloc id，避免 MCPlus allocator 冲突。

待本地验证：若开启多线程（默认）并对比单线程（如 `-no-threads`，待确认该选项名），观察 BOLT 在处理大量函数时的耗时差异，可间接体会 allocator 隔离带来的并行收益。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `BumpPtrAllocator` 适合用来分配注释对象？

> **答案**：`BumpPtrAllocator` 是「指针碰撞式」分配器，分配开销极低、且整块回收（`Reset()`）非常快。注释对象数量巨大（每条指令可能多个）、生命周期成批结束，正好匹配「批量分配、批量释放」的访问模式；用通用 `new/delete` 会因每对象的分配/释放开销和碎片化而显著变慢。

**练习 2**：如果两个并行线程都用默认 allocator id=0 去 `addAnnotation`，会发生什么？

> **答案**：它们会竞争同一个 `BumpPtrAllocator`（`BumpPtrAllocator` 本身不是线程安全的），导致数据竞争或崩溃。这正是 `initializeNewAnnotationAllocator` 存在的意义——给每个并行任务发一个独立 id，让分配落在不同 allocator 上，从根上避免锁竞争与冲突。单线程场景下才安全地共用 id=0。

## 5. 综合实践

把本讲三个最小模块串起来，完成下面这个「跟踪一条带跳转表的指令」的综合任务：

1. **定位存储机制**：在 [include/bolt/Core/MCPlus.h:L33-L61](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/include/bolt/Core/MCPlus.h#L33-L61) 用一句话写出 MCPlus 用哪种 operand 标记注释起点（答：`kInst` 类型、`nullptr` 值的空子指令 operand）。
2. **区分两类注释**：跟踪 `setJumpTable`（[lib/Core/MCPlusBuilder.cpp:L271-L278](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Core/MCPlusBuilder.cpp#L271-L278)），指出它一次写入了「一个一等注释（`kJumpTable`，跳转表地址）」和「一个补充注释（`"JTIndexReg"`，索引寄存器）」，并说明各自删除的后果（前者导致跳转表无法识别→语义错误；后者只丢辅助细节→无害）。
3. **解释 allocator**：指出 `setJumpTable` 把 `AllocId` 透传给了补充注释 `getOrCreateAnnotationAs`（用于在指定 arena 分配 `uint16_t` 对象），而一等注释的地址直接进 imm、不需要 allocator。说明这种差别如何让并行 pass 给每个线程分配独立 allocator。
4. **写一段小结**（3–5 句）：用自己的话回答本讲的核心问题——MCPlus 是怎样在不改 `MCInst` 内存布局的前提下挂载元数据的？为什么 profile 可以做成可丢弃的补充注释？

待本地验证：可选地用 `bat-dump` 或带 `-print-only=<func>` 的 `llvm-bolt` 跑一个含 switch（跳转表）的小程序，在反汇编输出里观察带注释指令的呈现形式（具体输出格式以本地实际版本为准）。

## 6. 本讲小结

- MCPlus 通过在 `MCInst` 的 operand 列表末尾追加「不参与汇编的额外 operand」来挂载元数据，**不改 `MCInst` 的内存布局**，从而与 LLVM MC 层完全兼容。
- 注释起点用一个**哨兵 operand** 标记：`isInst()` 为真、inst 指针为 `nullptr` 的「空子指令」；其后每个注释是一个 immediate，把 (Index, Value) 编码进 64 位（高 8 位 Index、低 56 位 Value）。
- 注释分两类：**一等注释**（预留索引如 `kJumpTable`/`kEHLandingPad`/`kTailCall`，影响语义、删了出错）与**补充注释**（按名字动态分配、`>= kGeneric`，可丢弃、删了只降优化质量）。
- 补充注释的 Value 字段存的是 arena 上 `MCSimpleAnnotation<ValueType>` 对象的指针，可承载任意复杂类型；profile/内存画像等正是这样挂上去的，所以做成补充注释是安全的。
- `MCPlusBuilder` 是目标无关的高层接口，同时用 `AnnotationAllocator`（BumpPtrAllocator + pool）统一管理注释对象的生命周期；**allocator id** 机制让每个并行任务拥有独立分配器，避免多线程下的分配器竞争。

## 7. 下一步学习建议

- **MCPlusBuilder 的目标相关实现**：本讲只讲了目标无关的接口层，X86/AArch64/RISCV 三个 `*MCPlusBuilder.cpp` 如何实现 `analyzeIndirectBranch`、`createLongJmp` 等目标相关能力，将在 **u7-l1（MCPlusBuilder 后端抽象与新增后端）** 详讲。
- **MCPlus 在主链路里的使用**：`processIndirectBranch` 如何用一等注释 `kJumpTable` 标记跳转表，将在 **u3-l3（反汇编与 CFG 重建）** 展开。
- **补充注释在优化 pass 里的典型用法**：如 `IndirectCallPromotion` 的 `"CallProfile"`、`ShrinkWrapping` 的 `"AccessesDeletedPos"`，可在阅读 **u6（核心优化 pass）** 时留意它们如何被 `tryGetAnnotationAs` 读出。
- 建议先把本讲的「哨兵 + 64 位编码」图和「两类注释对比表」记牢，后续阅读任何 pass 源码时，遇到 `BC.MIB->xxxAnnotation(...)` 调用都能立刻对应到本讲的存储模型。
