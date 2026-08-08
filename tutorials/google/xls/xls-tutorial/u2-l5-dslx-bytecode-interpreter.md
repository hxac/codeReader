# DSLX 字节码解释器

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚 DSLX 的「字节码（bytecode）」是什么，以及它和 AST、XLS IR 的关系。
- 看懂 `Bytecode` 类里一条「指令」的结构：操作码（`Op`）+ 可选数据（`Data`）。
- 跟踪 `BytecodeEmitter` 如何用访问者模式（`ExprVisitor`）把一棵表达式 AST 翻译成线性字节码序列，并能手工推导简单函数的字节码。
- 理解 `BytecodeInterpreter` 的「栈帧（frame）+ 操作数栈」执行模型，知道 `Run` 主循环如何取指、分派、执行。
- 解释 `#[test]` / `#[quickcheck]` 为什么最终都跑在这套字节码解释器上。

## 2. 前置知识

本讲需要你已经掌握下面几个概念（前几讲已建立）：

- **DSLX 是表达式式语言**（u2-l1）：函数体本身就是一个表达式，`let` / `if` / `match` / `for` 都「有值」。这条性质是字节码能做成「栈式」的关键——后面会看到，函数最终在栈顶留下一个值，就是返回值。
- **前端会产出无类型 AST**（u2-l2）：源码经 Scanner、Parser 变成一棵 `AstNode` 树，遍历靠 `AstNodeVisitor` / `AcceptExpr`。
- **类型推导会产出 `TypeInfo`**（u2-l3）：字节码发射**必须**在类型检查之后进行，因为很多指令需要类型信息才能定下来（例如加法到底是无符号 `uadd` 还是有符号 `sadd`，字面量到底是几位）。
- **编译期求值（constexpr）复用运行期解释器**（u2-l4）：`ConstexprEvaluator` 会调用 `InterpretExpr` 发射字节码、再交给本讲要讲的 `BytecodeInterpreter` 求值。所以本讲的解释器不仅服务于测试，也服务于编译期。

接下来只需要补一个底层概念：**栈式虚拟机（stack machine）**。

很多虚拟机（JVM、Python 的 CPython、PostScript……）都采用栈式执行：指令不写显式的寄存器名，而是约定「操作数放在一个栈上」。一条二元运算指令（比如 `add`）的含义就是「弹出栈顶两个数，相加，把结果压回栈顶」。XLS 的 DSLX 字节码正是这种风格。源码注释里用了两个缩写来描述栈：

> 在这些描述中，"TOS1" 指次栈顶元素（second-to-top-stack element），"TOS0" 指栈顶元素（top stack element）。
>
> [xls/dslx/bytecode/bytecode.h:44-45](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/bytecode/bytecode.h#L44-L45)

所以 `kUAdd` 就是「把 TOS1 和 TOS0 相加」。理解了这一点，整篇讲义的指令语义就一目了然。

> **一条易混点**：DSLX 字节码 **不是** XLS IR。XLS IR（`xls/ir`，u3 单元）是整个编译器的枢纽，最终会被优化、调度、生成 Verilog；而 DSLX 字节码（`xls/dslx/bytecode`）是**前端自己的**一种线性中间表示，专门用来**在主机上解释执行** DSLX（跑测试、跑 quickcheck、做编译期求值）。它不会流向硬件，所以它的设计目标是「简单、好执行」，而不是「好综合」。

## 3. 本讲源码地图

本讲涉及的关键文件都在 `xls/dslx/bytecode/` 下：

| 文件 | 作用 |
| --- | --- |
| [xls/dslx/bytecode/bytecode.h](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/bytecode/bytecode.h) / [.cc](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/bytecode/bytecode.cc) | 定义单条指令 `Bytecode`（操作码 `Op` + 数据 `Data`）与 `BytecodeFunction`（一个函数的全部字节码），以及文本打印/解析。 |
| [xls/dslx/bytecode/bytecode_emitter.h](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/bytecode/bytecode_emitter.h) / [.cc](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/bytecode/bytecode_emitter.cc) | `BytecodeEmitter`：把类型检查后的 AST 发射成字节码序列。本讲的「翻译器」。 |
| [xls/dslx/bytecode/bytecode_interpreter.h](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/bytecode/bytecode_interpreter.h) / [.cc](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/bytecode/bytecode_interpreter.cc) | `BytecodeInterpreter`：取指—分派—执行字节码，本讲的「虚拟机」。 |
| [xls/dslx/bytecode/frame.h](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/bytecode/frame.h) / [.cc](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/bytecode/frame.cc) | `Frame`：一次函数调用的执行上下文（PC、局部槽位 slots、所属 `BytecodeFunction`）。 |
| [xls/dslx/bytecode/interpreter_stack.h](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/bytecode/interpreter_stack.h) | `InterpreterStack`：全局共享的操作数栈，存 `InterpValue`。 |
| [xls/dslx/run_routines/run_routines.cc](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/run_routines/run_routines.cc) | `RunDslxTestFunction`：把「发射 + 解释」串起来跑 `#[test]` 的胶水代码。 |

整体数据流（也是本讲的主线）：

```
DSLX 源码
   │  (前端, u2-l2)
   ▼
无类型 AST ──(类型推导, u2-l3)──▶ 带类型 AST + TypeInfo
   │
   │  BytecodeEmitter::Emit          ← 4.2 字节码发射
   ▼
BytecodeFunction（一组 Bytecode）
   │
   │  BytecodeInterpreter::Interpret ← 4.3 字节码解释器
   ▼
栈顶的 InterpValue（即结果）
```

## 4. 核心概念与源码讲解

本讲按数据流拆成三个最小模块：**4.1 字节码定义**（「指令」长什么样）、**4.2 字节码发射**（AST 怎么变成指令）、**4.3 字节码解释器**（指令怎么被执行）。

### 4.1 字节码定义

#### 4.1.1 概念说明

要执行 DSLX，最朴素的做法是「直接在 AST 树上递归求值」——遇到 `Binop` 就递归算左右子树、再把运算符作用上去。这种树遍历解释器写起来直观，但有缺点：控制流（`if`/`match`）、跳转、函数调用不好统一表达，而且每次执行都要重新在树上分派。

XLS 选择了更经典的方案：先把 AST **线性化**成一串扁平的「指令」，再让一个紧凑的循环去执行这串指令。这条指令就是 `Bytecode`。这样做的好处是：

- 指令序列可以**缓存**、可以**文本往返**（`BytecodesToString` / `BytecodesFromString`）。
- 控制流用**跳转指令**统一表达（`if` 被编译成 `jump_if`）。
- 求值器（解释器）非常小，核心就是一个 `switch`。

一条 `Bytecode` 由两部分组成：一个**操作码** `Op`（「做什么」），和一个**可选的数据载荷** `Data`（「对谁做」）。比如 `literal u8:42` 的操作码是 `kLiteral`、数据是 `InterpValue(42)`；`load 0` 的操作码是 `kLoad`、数据是槽位号 `0`。

#### 4.1.2 核心流程

`Bytecode` 指令体系按用途大致分几类：

- **常量/变量**：`kLiteral`（压字面量）、`kLoad`（从局部槽位取值压栈）、`kStore`（弹栈存入槽位）。
- **算术/位运算**：`kUAdd`/`kSAdd`、`kUSub`/`kSSub`、`kUMul`/`kSMul`、`kDiv`/`kMod`、`kAnd`/`kOr`/`kXor`、`kShl`/`kShr`、`kInvert`/`kNegate`……（有符号/无符号各一套）。
- **比较**：`kEq`/`kNe`/`kLt`/`kLe`/`kGt`/`kGe`。
- **聚合**：`kCreateArray`/`kCreateTuple`（把栈顶 N 个值装箱）、`kExpandTuple`/`kIndex`/`kTupleIndex`（拆箱/索引）。
- **控制流**：`kJumpRel`（无条件相对跳）、`kJumpRelIf`（条件相对跳）、`kJumpDest`（跳转落点标记）。
- **函数调用**：`kCall`。
- **失败/追踪**：`kFail`、`kTraceFmt`/`kTraceArg`。
- **Proc 通信**：`kSend`/`kRecv`/`kRecvNonBlocking`、`kSpawn`、`kRead`/`kWrite`。

栈深度的一个核心不变式：对二元运算，**先压左操作数，再压右操作数**，所以执行运算时左在 TOS1、右在 TOS0。对于单条指令，可以用一个微型代数描述栈效果（`Δdepth` 表示执行后栈深度变化，正为净压入）：

\[ \text{二元运算: } \Delta\text{depth} = -1 \quad (\text{弹 } 2 \text{ 压 } 1) \]
\[ \text{kLiteral/kLoad: } \Delta\text{depth} = +1 \quad (\text{压 } 1) \]
\[ \text{kStore: } \Delta\text{depth} = -1 \quad (\text{弹 } 1) \]

整段字节码执行完，合法的函数会在栈顶留下**恰好一个**值——那就是返回值（返回 unit 的函数除外）。

#### 4.1.3 源码精读

**操作码枚举**。所有指令种类集中在 `Bytecode::Op` 这个 `enum class` 里，每条都带详细注释，是理解指令语义的「权威文档」：

[xls/dslx/bytecode/bytecode.h:46-194](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/bytecode/bytecode.h#L46-L194) —— 定义全部操作码，例如 `kUAdd`（L48）、`kCall`（L56）、`kLiteral`（L112）、`kLoad`（L115）、`kStore`（L175）、`kJumpRel`/`kJumpRelIf`/`kJumpDest`（L104-108）。

**数据载荷 `Data`**。操作码只说「做什么」，很多指令还需要附带信息。XLS 用一个 `std::variant` 把所有可能的载荷收在一起：

[xls/dslx/bytecode/bytecode.h:382-384](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/bytecode/bytecode.h#L382-L384) —— `Data` 变体，包括 `InterpValue`（字面量）、`SlotIndex`（槽位号）、`JumpTarget`（跳转偏移）、`NumElements`（装箱元素数）、`Type`（转型目标类型）、`InvocationData`（调用信息）、`MatchArmItem`、`SpawnData`、`TraceData`、`ChannelData`。

注意几个**强类型整数**（`XLS_DEFINE_STRONG_INT_TYPE`），避免把「槽位号」和「跳转偏移」等不同含义的整数混用：

[xls/dslx/bytecode/bytecode.h:198-206](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/bytecode/bytecode.h#L198-L206) —— `JumpTarget`、`NumElements`、`SlotIndex` 都是带类型的 `int64_t` 别名。

**指令的构造与工厂**。无数据的指令直接用 `Bytecode(span, op)` 构造；带数据的用一组 `MakeXxx` 工厂（如 `MakeLiteral`、`MakeLoad`、`MakeStore`、`MakeJumpRel` 等）：

[xls/dslx/bytecode/bytecode.h:386-406](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/bytecode/bytecode.h#L386-L406) —— 工厂方法列表。
[xls/dslx/bytecode/bytecode.h:412-422](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/bytecode/bytecode.h#L412-L422) —— 两个构造函数：不带数据 / 带可选 `Data`。注意 `Bytecode` **不可拷贝、只能移动**（L425-427），因为里面的 `Data` 可能持有 `unique_ptr`。

**跳转的占位与回填**。发射 `if` 时，跳转目标在「当时」还不知道（要等中间那段代码发射完才数得清偏移），所以先用占位值 `kPlaceholderJumpAmount = -1` 发射，事后再 `PatchJumpTarget` 回填。这是经典的「两遍」处理：

[xls/dslx/bytecode/bytecode.h:449-463](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/bytecode/bytecode.h#L449-L463) —— 占位常量与 `PatchJumpTarget` 的注释说明。

**`BytecodeFunction`**。一个 DSLX 函数对应一个 `BytecodeFunction`，它把字节码序列连同所属 `Module`、源 `Function`、`TypeInfo` 打包在一起：

[xls/dslx/bytecode/bytecode.h:482-513](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/bytecode/bytecode.h#L482-L513) —— `BytecodeFunction` 持有 `bytecodes_` 向量并提供 `Create` 工厂。它只原样保存传入的字节码序列、不额外追加指令（见 4.3.3 对尾部 `pop` 出现条件的澄清）。

**文本表示**。每条指令可以打印成 `<op> <data>` 的形式，操作码名字由 `OpToString` 给出（`kUAdd→"uadd"`、`kLiteral→"literal"`、`kLoad→"load"`……）：

[xls/dslx/bytecode/bytecode.cc:219-333](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/bytecode/bytecode.cc#L219-L333) —— `OpToString`，是「操作码 ↔ 名字」的总对照表。
[xls/dslx/bytecode/bytecode.cc:335-346](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/bytecode/bytecode.cc#L335-L346) —— `BytecodesToString` 给整个序列加 `%03d` 的行号前缀，例如 `000 load 0`。
[xls/dslx/bytecode/bytecode.cc:599-688](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/bytecode/bytecode.cc#L599-L688) —— `Bytecode::ToString` 主体：跳转指令格式为 `jump_if +3`（带符号偏移），其余为 `op data`。

#### 4.1.4 代码实践：读懂一段字节码文本

**实践目标**：建立「操作码 + 栈效果」的直觉，能手工跟踪一段字节码的执行。

**操作步骤**：

1. 阅读下面的 DSLX 函数（**示例代码**，非仓库文件）：

   ```dslx
   fn add(a: u8, b: u8) -> u8 {
       a + b
   }
   ```

2. 假设参数 `a`、`b` 分别占用槽位 0、1（原因见 4.2.3 的 `Init`）。它发射出的字节码大致是（**手工推导，示例**）：

   ```
   000 load 0      # 把槽位 0（a）压栈       栈: [a]
   001 load 1      # 把槽位 1（b）压栈       栈: [a, b]
   002 uadd        # 弹出 b、a，压入 a+b     栈: [a+b]
   ```

3. 用前面给出的栈深度公式逐条验证：`load` 各 `+1`，`uadd` 为 `-1`，净深度 `+1`，栈顶恰为返回值。

**需要观察的现象**：每条 `load` 让栈多一个值；`uadd` 把两个值合并成一个。

**预期结果**：执行到序列末尾时，栈深度为 1，栈顶即 `a+b`，正是 `add` 的返回值。

**待本地验证**：上面是据源码手工推导的序列。你可以在 4.3.4 用 `--v=5` 打印实际发射结果来逐字核对。注意只有当语句块**以分号结尾**（块的值为 unit）时才会多出尾部 `pop`，见 4.3.3 的说明；本例不以分号结尾，故没有多余指令。

#### 4.1.5 小练习与答案

**练习 1**：`kEq`（相等比较）执行后，栈深度如何变化？它的两个操作数分别在哪？

> **答**：净变化 `-1`（弹 2 压 1）。左操作数在 TOS1、右操作数在 TOS0，结果（一个 1 位的 bool）压回栈顶。

**练习 2**：为什么需要 `kJumpDest` 这个「没有执行逻辑」的指令？（提示见 [bytecode.h:101-103](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/bytecode/bytecode.h#L101-L103)）

> **答**：它只用作跳转落点的标记，用于控制流完整性检查——解释器在 `Run` 里会断言「任何非顺序推进的 PC 必须落在一个 `kJumpDest` 上」（见 4.3.3 的 [bytecode_interpreter.cc:374-380](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/bytecode/bytecode_interpreter.cc#L374-L380)），防止跳到指令中间。

---

### 4.2 字节码发射

#### 4.2.1 概念说明

`BytecodeEmitter` 的职责是「翻译」：输入一棵**带类型**的 DSLX AST（一个 `Function` 的函数体表达式），输出一段线性的 `Bytecode` 序列。它实现的是 `ExprVisitor` 接口（u2-l2 讲过访问者模式），即「每种 AST 节点对应一个 `HandleXxx` 方法」。

设计上有两个要点：

1. **后序遍历**。对于 `a + b` 这样的 `Binop`，发射器先递归处理左子树 `a`、再处理右子树 `b`，**最后**才发射 `uadd`。这天然符合栈式语义：先把操作数都压好，再发运算指令。
2. **槽位（slot）= 局部变量**。函数的参数和 `let` 绑定都需要一个「地方」来存。发射器给每个 `NameDef` 分配一个整数槽位号，记在 `namedef_to_slot_` 映射里；引用变量就发 `load <槽位>`，定义变量（`let`）就发 `store <槽位>`。

#### 4.2.2 核心流程

发射一个函数的整体流程（伪代码）：

```
EmitInternal(f):
    创建 emitter，绑定 type_info、caller_bindings
    Init(f):                 # 为每个参数分配槽位 0,1,2,...
    f.body()->AcceptExpr(emitter)   # 后序遍历函数体，逐节点 HandleXxx
    返回 BytecodeFunction(序列)
```

几类典型节点的翻译规则：

| AST 节点 | 翻译成的字节码 |
| --- | --- |
| `Number`（字面量） | `literal <值>` |
| `NameRef`（变量引用） | `load <槽位>`（局部变量）或直接 `literal`（常量/函数引用） |
| `Binop(lhs, op, rhs)` | `<发射 lhs> <发射 rhs> <op指令>`（如 `uadd`） |
| `Let(pattern, rhs)` | `<发射 rhs>` 然后 `DestructureLet`（`store <槽位>` / `pop` / `expand_tuple`） |
| `StatementBlock` | 顺序发射各语句；语句间用 `pop` 丢弃非末尾表达式结果 |
| `Conditional`（if） | `test` + `jump_if` + `alternate` + `jump` + `jump_dest` + `consequent` + `jump_dest`（跳转回填） |

#### 4.2.3 源码精读

**入口 `Emit` 与 `EmitInternal`**。公开入口 `Emit` 只是转发到 `EmitInternal`，后者创建 emitter、调 `Init`、然后对函数体做 `AcceptExpr`（触发访问者遍历），最后把收集到的 `bytecode_` 打包成 `BytecodeFunction`：

[xls/dslx/bytecode/bytecode_emitter.cc:160-168](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/bytecode/bytecode_emitter.cc#L160-L168) —— `Emit` 转发。
[xls/dslx/bytecode/bytecode_emitter.cc:191-212](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/bytecode/bytecode_emitter.cc#L191-L212) —— `EmitInternal`：注意 L205 `Init(f)`、L206 `f.body()->AcceptExpr(this)`、L208 `LogEmittedFunction`（在 `--v=5` 时打印整段字节码，是实践的观察利器）。

**槽位初始化 `Init`**。把函数的每个参数 `NameDef` 顺序映射到一个槽位号（从 0 开始）：

[xls/dslx/bytecode/bytecode_emitter.cc:152-158](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/bytecode/bytecode_emitter.cc#L152-L158) —— 这就是为什么 4.1.4 里 `a` 是槽位 0、`b` 是槽位 1。`namedef_to_slot_` 与 `next_slotno_` 这两个字段定义在 [bytecode_emitter.h:207-208](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/bytecode/bytecode_emitter.h#L207-L208)。

**算术：`HandleBinop`**。这是「后序遍历」最典型的体现：先 `lhs()->AcceptExpr(this)`，再 `rhs()->AcceptExpr(this)`，最后根据 `binop_kind()` 推一条运算指令。注意加/减/乘会查类型来决定**有符号还是无符号**：

[xls/dslx/bytecode/bytecode_emitter.cc:343-419](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/bytecode/bytecode_emitter.cc#L343-L419) —— `HandleBinop`。L344-345 先递归左右子树；L347-351 处理 `kAdd`，用 `IsBitsTypeNodeSigned` 选 `kSAdd`/`kUAdd`。这也解释了为什么发射**必须在类型检查之后**。

**字面量：`HandleNumber`**。借助 `TypeInfo` 拿到位宽和符号性，把源码里的数字文本变成一个确定位宽的 `InterpValue`，再发 `kLiteral`：

[xls/dslx/bytecode/bytecode_emitter.cc:1582-1609](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/bytecode/bytecode_emitter.cc#L1582-L1609) —— `HandleNumber`/`HandleNumberInternal`：L1602 取位宽、L1603 取符号性、L1607 `MakeBits` 造值。

**变量引用：`HandleNameRef` 与 `AddResult`**。引用一个名字时，要先判断它是「局部变量（走槽位，发 `load`）」还是「常量/函数（直接发 `literal`）」。`AddResult` 这个小函数把这两种结果统一翻译成对应字节码：

[xls/dslx/bytecode/bytecode_emitter.cc:1457-1470](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/bytecode/bytecode_emitter.cc#L1457-L1470) —— `HandleNameRef` 调 `HandleNameRefInternal` 拿到 `InterpValue | SlotIndex`，再交给 `AddResult`：若是 `InterpValue` 发 `MakeLiteral`，若是 `SlotIndex` 发 `MakeLoad`。
[xls/dslx/bytecode/bytecode_emitter.cc:1515-1543](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/bytecode/bytecode_emitter.cc#L1515-L1543) —— `HandleNameRefInternal` 的分派逻辑：内置名→函数值、函数定义→`InterpValue`、常量→取 constexpr 值、局部名→查 `namedef_to_slot_` 返回槽位、参数化变量→从 `caller_bindings_` 取值。

**`let` 绑定：`HandleLet` 与 `DestructureLet`**。`let` 先发射右值，然后按模式（pattern）解构：单个名字就 `store <槽位>`，元组模式就 `expand_tuple` 逐层拆开，通配符 `_` 就 `pop` 丢弃：

[xls/dslx/bytecode/bytecode_emitter.cc:1446-1455](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/bytecode/bytecode_emitter.cc#L1446-L1455) —— `HandleLet`：发射右值后取其类型，交给 `DestructureLet`。
[xls/dslx/bytecode/bytecode_emitter.cc:1368-1440](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/bytecode/bytecode_emitter.cc#L1368-L1440) —— `DestructureLet`：L1377-1383 给新名字分配槽位并 `store`；L1388 元组用 `expand_tuple` 展开。

**语句块：`HandleStatementBlock`**。顺序发射，并在两条「表达式语句」之间插入 `pop` 丢弃前一条的结果（因为只有最后一条的值才是块的值）：

[xls/dslx/bytecode/bytecode_emitter.cc:421-461](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/bytecode/bytecode_emitter.cc#L421-L461) —— 注意 L429-431：若上一条是表达式语句，先 `Add(MakePop(...))`。

**`if` 表达式：`HandleConditional`**（跳转回填的经典样板）。源码注释把目标结构画得很清楚。流程是：发射 `test` → 发一条占位的 `jump_if` → 发射 `alternate`（else 分支）→ 发一条占位的 `jump`（跳过 consequent）→ 发 `jump_dest`（consequent 入口）→ 发射 `consequent`（then 分支）→ 发 `jump_dest`（join 汇合点）→ 最后用 `PatchJumpTarget` 回填两条占位跳转：

[xls/dslx/bytecode/bytecode_emitter.cc:1762-1807](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/bytecode/bytecode_emitter.cc#L1762-L1807) —— L1789-1790 发占位 `jump_if`；L1793-1794 发占位 `jump`；L1796、L1803 发两个 `jump_dest`；L1804-1805 回填两条跳转的相对偏移。注意 L1765-1774：**编译期已知的 if**（`node->IsConst()`）会直接只发射命中分支，不产生任何跳转——这呼应了 u2-l4 的 constexpr。

> 旁注：`EmitExpression`（[bytecode_emitter.cc:232-270](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/bytecode/bytecode_emitter.cc#L232-L270)）是给「裸表达式」用的入口，u2-l4 的 constexpr 求值就是用它把一个表达式连同常量环境一起编译成字节码的。

#### 4.2.4 代码实践：手工推导并与发射器对照

**实践目标**：亲手把一个带 `let` 的函数翻译成字节码，再去源码里验证。

**操作步骤**：

1. 给定 DSLX（**示例代码**）：

   ```dslx
   fn add_one(x: u8) -> u8 {
       let y = x + u8:1;
       y
   }
   ```

2. 推导槽位：`Init` 给参数 `x` 分配槽位 0；`let y` 在 `DestructureLet` 里给 `y` 分配槽位 1。

3. 逐节点翻译（**手工推导，示例**）：

   ```
   000 load 0       # x        (NameRef → load)
   001 literal u8:1 # 字面量 1 (Number → literal)
   002 uadd         # x + 1    (Binop, 无符号 → uadd)
   003 store 1      # 存入 y   (DestructureLet → store 槽位1)
   004 load 1       # y        (末尾表达式 NameRef → load)
   ```

4. 验证逻辑：`HandleStatementBlock` 在「`let`」和「`y`」之间**不会**插 `pop`，因为 `let` 把 `last_expression` 置空了（[bytecode_emitter.cc:439-441](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/bytecode/bytecode_emitter.cc#L439-L441)），所以末尾 `y` 的值留在栈顶作返回值。

**需要观察的现象**：`let` 对应「算右值 + `store`」两步；末尾表达式对应一次 `load`。

**预期结果**：序列末尾栈顶为 `y = x+1`，即返回值。

**待本地验证**：把上述函数写进一个 `.x` 文件并配一个 `#[test]` 调用它，用 `interpreter_main --v=5 your_file.x` 运行，在日志里找形如 `Emitted add_one with TI ...:` 之后缩进的各行，对照本推导。本例函数体不以分号结尾，序列应恰好为上述 5 条、无多余指令（关于「何时会出现尾部 `pop`」见 4.3.3）。

#### 4.2.5 小练习与答案

**练习 1**：`let (a, b) = t`（元组解构）会发射哪些字节码来拆分元组？

> **答**：先发射右值 `t` 压栈，然后 `DestructureLet` 检测到 `TuplePattern`，发一条 `expand_tuple`（把元组拆开，各元素逆序压栈），再对每个子名字分别 `store`。详见 [bytecode_emitter.cc:1384-1388](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/bytecode/bytecode_emitter.cc#L1384-L1388)。

**练习 2**：为什么 `HandleBinop` 要在发射 `uadd`/`sadd` 之前调用 `IsBitsTypeNodeSigned`？如果没有类型信息会怎样？

> **答**：因为 DSLX 的加法在硬件上有「无符号回绕」和「有符号回绕」之分，对应的字节码是 `kUAdd` 和 `kSAdd` 两条不同指令；只有查 `TypeInfo` 才能知道这个 `Binop` 节点的类型是否带符号。没有类型信息就定不下来发哪条——这也是发射必须在类型检查之后的原因。

---

### 4.3 字节码解释器

#### 4.3.1 概念说明

发射器产出了字节码序列，剩下的就是执行它。`BytecodeInterpreter` 是一台「栈 + 帧」的虚拟机：

- **操作数栈 `InterpreterStack`**：全局共享（跨函数调用），存 `InterpValue`。所有运算都在它上面发生。
- **帧 `Frame`**：对应**一次函数调用**，持有该调用的 PC（程序计数器）、局部槽位 `slots`（存参数和 `let` 变量）、以及正在执行的 `BytecodeFunction`。函数调用 = 压一个新帧；函数返回 = 弹帧。
- **取指—分派循环**：`Run` 不断从当前帧取下一条字节码，用一个巨大的 `switch` 分派到对应的 `EvalXxx`。

这套模型让 DSLX 的 `#[test]` 和 `#[quickcheck]` 都能直接在主机上跑：测试函数被发射成字节码，解释器执行它，`assert_eq` 失败时就通过 `kFail` 指令汇报。

#### 4.3.2 核心流程

主循环（伪代码，对应 `Run`）：

```
Run():
    while 帧栈非空:
        frame = 当前帧
        while frame.pc < 字节码长度:
            bytecode = bytecodes[frame.pc]
            EvalNextInstruction()      # switch 分派到 EvalXxx
            # 若是 kCall：新帧已压入，循环自然切到新帧
            # 若 PC 不是 +1 递进：断言落点是 kJumpDest
        # 当前函数执行完（PC 越界）
        弹出当前帧                      # 返回值已在操作数栈顶
    结束
```

几条关键指令的执行语义：

- `kLiteral`：把数据里的 `InterpValue` 压栈。
- `kLoad <slot>`：从**当前帧**的 `slots[slot]` 取值压栈。
- `kStore <slot>`：弹栈顶，写入当前帧的 `slots[slot]`。
- 二元运算（如 `kUAdd`）：弹 rhs、弹 lhs，算 `lhs op rhs`，压结果。
- `kJumpRelIf`：弹栈顶条件；为真则 `PC += 偏移`，否则 PC 顺序 +1。
- `kCall`：弹 callee、弹参数，构造一个新 `Frame`（参数成为新帧的初始 `slots`），压入帧栈；新帧从 PC=0 开始跑。 callee 是内置函数时直接调 `RunBuiltinFn` 而不压帧。

#### 4.3.3 源码精读

**入口 `Interpret` 与帧初始化**。`Interpret` 是静态入口；`InitFrame` 把入口帧压入 `frames_`，参数直接成为该帧的 `slots`（注意：`Frame` 构造函数用 `args` 初始化 `slots_`）：

[xls/dslx/bytecode/bytecode_interpreter.h:272-277](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/bytecode/bytecode_interpreter.h#L272-L277) —— `Interpret` 静态签名。
[xls/dslx/bytecode/bytecode_interpreter.cc:323-337](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/bytecode/bytecode_interpreter.cc#L323-L337) —— `InitFrame`：L333-335 用参数构造入口帧。`stack_`、`frames_` 两个字段见 [bytecode_interpreter.h:434-435](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/bytecode/bytecode_interpreter.h#L434-L435)。

**帧 `Frame`**。`pc_` 是程序计数器、`slots_` 是局部存储、`bf_` 指向当前 `BytecodeFunction`。`StoreSlot` 还会在槽位不够时自动补占位 `MakeToken()`（处理「条件分支里声明的变量可能没被执行到」的情况）：

[xls/dslx/bytecode/frame.h:32-69](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/bytecode/frame.h#L32-L69) —— `Frame` 类与 `pc_`/`slots_`/`bf_` 字段。
[xls/dslx/bytecode/frame.cc:29-40](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/bytecode/frame.cc#L29-L40) —— 构造函数：`slots_(std::move(args))`，**这就是「参数即槽位」的实现**。
[xls/dslx/bytecode/frame.cc:42-51](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/bytecode/frame.cc#L42-L51) —— `StoreSlot` 的自动扩容。

**操作数栈 `InterpreterStack`**。`Pop`/`Push`/`PeekOrDie` 是基础操作，元素其实是 `FormattedInterpValue`（值 + 可选格式描述符，用来在 `assert_eq` 失败时打印更友好的信息）：

[xls/dslx/bytecode/interpreter_stack.h:43-91](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/bytecode/interpreter_stack.h#L43-L91) —— `Pop`（L54-57）、`Push`（L72-76）、`PeekOrDie`（L86-91）。

**主循环 `Run`**。双层 `while`：外层遍历帧栈，内层在当前帧内逐条执行。注意三个细节：①每条指令前后都有 VLOG 打印栈深度（调试利器）；②`kCall` 之后要重新取 `frames_.back()`（因为压了新帧）；③任何「非 +1 递进」的 PC 必须断言落在 `kJumpDest`；④函数执行完后会调用可选的 `post_fn_eval_hook`（JIT 比对用）并弹帧：

[xls/dslx/bytecode/bytecode_interpreter.cc:352-420](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/bytecode/bytecode_interpreter.cc#L352-L420) —— `Run`。L356 内层条件 `frame->pc() < ...size()`；L368 `EvalNextInstruction()`；L372-380 `kCall` 与跳转落点断言；L386-403 post-fn hook；L416 弹帧。

**分派 `EvalNextInstruction`**。一个以 `bytecode.op()` 为判别的大 `switch`，每个 `case` 调一个 `EvalXxx`：

[xls/dslx/bytecode/bytecode_interpreter.cc:432-539](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/bytecode/bytecode_interpreter.cc#L432-L539) —— 分派开头片段。例如 `kUAdd→EvalAdd(...,false)`（L444-446）、`kJumpDest→空 break`（L524-525）、`kJumpRel→set_pc(pc+target)`（L526-529）、`kJumpRelIf→EvalJumpRelIf`（L531-538）。

**二元运算的统一实现 `EvalBinop`**。所有二元运算共享同一段「弹 rhs、弹 lhs、算、压结果」逻辑，区别只在传入的 lambda：

[xls/dslx/bytecode/bytecode_interpreter.cc:674-683](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/bytecode/bytecode_interpreter.cc#L674-L683) —— `EvalBinop`：L678 弹 rhs、L679 弹 lhs（顺序与栈一致）、L680 算、L681 压栈。这正好印证 4.1 的 TOS0/TOS1 约定。
[xls/dslx/bytecode/bytecode_interpreter.cc:685-707](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/bytecode/bytecode_interpreter.cc#L685-L707) —— `EvalAdd`：调 `lhs.Add(rhs)`，并在启用 `rollover_hook` 时检查溢出。

**存取与字面量**：

[xls/dslx/bytecode/bytecode_interpreter.cc:1097-1114](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/bytecode/bytecode_interpreter.cc#L1097-L1114) —— `EvalLiteral`（压字面量）、`EvalLoad`（从 `frames_.back().slots()` 取值压栈）。
[xls/dslx/bytecode/bytecode_interpreter.cc:1448-1458](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/bytecode/bytecode_interpreter.cc#L1448-L1458) —— `EvalStore`：弹栈顶 → `frames_.back().StoreSlot(slot, value)`。注意读写都作用在**当前帧**的 slots。

**条件跳转 `EvalJumpRelIf`**：

[xls/dslx/bytecode/bytecode_interpreter.cc:1460-1469](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/bytecode/bytecode_interpreter.cc#L1460-L1469) —— 弹条件；为真返回 `pc + target`（新 PC），否则返回 `nullopt`（PC 顺序 +1）。

**函数调用 `EvalCall`**（最复杂也最重要）。流程：弹 callee → 若是内置函数，直接 `RunBuiltinFn`；否则从字节码里取 `InvocationData`（含参数化绑定），经 `BytecodeCache` 拿到（或即时发射）被调函数的 `BytecodeFunction`，处理 `self` 方法的特殊首参，把返回 PC+1 存回当前帧，最后**用参数构造一个新 `Frame` 压入帧栈**：

[xls/dslx/bytecode/bytecode_interpreter.cc:740-808](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/bytecode/bytecode_interpreter.cc#L740-L808) —— `EvalCall`：L743 弹 callee；L744-747 内置函数短路；L759-761 经缓存取被调函数字节码；L780 `IncrementPc`（保存返回地址）；L790-794 收集参数（`PopArgsRightToLeft`，因为栈顶是最右参数）；L805-806 用参数构造新帧压栈。被调函数执行结束后，`Run` 的外层循环弹掉这个帧，返回值已在栈顶，控制回到「返回 PC」。
[xls/dslx/bytecode/bytecode_interpreter.cc:715-738](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/bytecode/bytecode_interpreter.cc#L715-L738) —— `GetBytecodeFn`：经 `BytecodeCacheInterface::GetOrCreateBytecodeFunction` 取/造被调函数字节码，参数化函数会带上 callee 绑定。

**`#[test]` 如何落地到这里**。`run_routines.cc` 的 `RunDslxTestFunction` 就是「发射测试函数 → 解释执行」的胶水：装一个 `BytecodeCache`、`BytecodeEmitter::Emit` 出测试函数的 `BytecodeFunction`、再 `BytecodeInterpreter::Interpret` 跑它：

[xls/dslx/run_routines/run_routines.cc:170-186](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/run_routines/run_routines.cc#L170-L186) —— `RunDslxTestFunction`：L174-175 装缓存；L178-181 发射；L182-185 解释执行。`interpreter_main`（u1-l5）跑测试时，每个 `#[test]` 最终都走这条路；`#[quickcheck]` 则在这之上再套一层随机输入生成。

> 关于「何时会出现尾部 `pop`」：`BytecodeFunction::Create` 只是原样保存字节码向量，**不会**在末尾追加任何指令（见 [bytecode.cc:759-773](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/bytecode/bytecode.cc#L759-L773)）。尾部的 `kPop` 只来自 `HandleStatementBlock`：当语句块**以分号结尾**（`trailing_semi()` 为真，即块的值被规定为 unit）时，它会先 `pop` 掉最后一条表达式的值、再压一个 `literal unit`（[bytecode_emitter.cc:463-470](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/bytecode/bytecode_emitter.cc#L463-L470)）。本讲 `add` / `add_one` / `max` 的函数体都以「末尾表达式」结尾、不带尾分号，所以**没有**多余指令，留在栈顶的值就是返回值。

#### 4.3.4 代码实践：观察真实字节码与栈深度

**实践目标**：用日志开关亲眼看一段字节码的发射与执行，并跟踪栈深度变化。

**操作步骤**：

1. 把 4.2.4 的 `add_one` 连同一个测试写进 `/tmp/bc_demo.x`（**示例代码**）：

   ```dslx
   fn add_one(x: u8) -> u8 {
       let y = x + u8:1;
       y
   }

   #[test]
   fn add_one_test() {
       assert_eq(u8:3, add_one(u8:2));
   }
   ```

2. 用 `--v=5` 跑测试（`--v` 是 Abseil 日志级别，对所有 XLS 二进制可用；发射器在 `VLOG_IS_ON(5)` 时打印整段字节码，见 [bytecode_emitter.cc:214-230](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/bytecode/bytecode_emitter.cc#L214-L230)）：

   ```bash
   bazel-bin/xls/dslx/interpreter_main --v=5 /tmp/bc_demo.x 2>&1 | grep -A8 "Emitted add_one"
   ```

3. 想看执行过程（每条指令前后的栈深度），用 `--v=3` 并过滤 `stack depth`（见 [bytecode_interpreter.cc:365-370](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/bytecode/bytecode_interpreter.cc#L365-L370)）：

   ```bash
   bazel-bin/xls/dslx/interpreter_main --v=3 /tmp/bc_demo.x 2>&1 | grep "stack depth"
   ```

**需要观察的现象**：

- `--v=5` 下能看到形如 `Emitted add_one with TI ...:` 后跟缩进的 `load 0` / `literal ...` / `uadd` / `store 1` / `load 1` / `pop`。
- `--v=3` 下能看到每条指令执行前后 `stack depth N [...]` 的变化，验证 4.1.2 的栈深度公式。

**预期结果**：实际发射序列与 4.2.4 的手工推导一致；栈深度随 `load` 上升、随 `uadd`/`store` 下降，规律吻合。

**待本地验证**：确切的日志前缀和行格式取决于本机构建版本；若 grep 不到，可去掉过滤直接看 `--v=5` 全量日志，定位 `Emitted` 与 `PC:` 关键字。`interpreter_main` 的路径依你 u1-l2 的构建结果而定（如 `./bazel-bin/xls/dslx/interpreter_main`）。

#### 4.3.5 小练习与答案

**练习 1**：函数 `add_one` 被调用时，新帧的 `slots` 里一开始有哪些值？`x` 在哪个槽位？

> **答**：`EvalCall` 用收集到的参数构造新 `Frame`（[bytecode_interpreter.cc:805-806](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/bytecode/bytecode_interpreter.cc#L805-L806)），`Frame` 构造函数把这些参数直接作为 `slots_`（[frame.cc:35](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/bytecode/frame.cc#L35)）。所以调用 `add_one(2)` 时新帧 `slots = [2]`，`x` 在槽位 0；`y`（槽位 1）在执行到 `store 1` 时才被填入。

**练习 2**：如果 `kCall` 调用的是内置函数（如 `assert_eq`），解释器会压新帧吗？

> **答**：不会。`EvalCall` 在 L744 检测到 `callee.IsBuiltinFunction()` 时，只是 `IncrementPc` 然后调 `RunBuiltinFn` 直接在当前上下文里执行内置逻辑（可能消费若干栈上参数），不构造新帧。

**练习 3**：为什么 `Run` 的外层循环在弹帧后，被调函数的返回值「自动」对调用者可见？

> **答**：因为操作数栈 `stack_` 是**跨帧共享**的（[bytecode_interpreter.h:434](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/bytecode/bytecode_interpreter.h#L434)）。被调函数把返回值留在栈顶，弹帧只是去掉调用上下文，栈顶的值依旧在，调用者继续执行时就能用到。

---

## 5. 综合实践

把「发射 + 解释」整条链亲手走一遍，并解释一个含控制流的函数。

**任务**：给定下面的 `max` 函数（**示例代码**），完成 (1) 手工推导字节码；(2) 用 `HandleConditional` 的结构验证跳转回填；(3) 实际运行核对。

```dslx
fn max(a: u8, b: u8) -> u8 {
    if a > b { a } else { b }
}
```

**步骤 1 — 推导槽位**：`a`→槽位 0，`b`→槽位 1。

**步骤 2 — 套用 `HandleConditional` 的模板**（[bytecode_emitter.cc:1775-1806](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/bytecode/bytecode_emitter.cc#L1775-L1806)）。`test` 是 `a > b`（`load 0; load 1; gt`），`consequent` 是 `a`（`load 0`），`alternate` 是 `b`（`load 1`）。手工推导（**示例**）：

```
000 load 0        # a            ┐ test: a > b
001 load 1        # b            │
002 gt            # a > b        ┘
003 jump_if +3    # 若真跳到 006 (consequent)
004 load 1        # b   (alternate)
005 jump_rel +3   # 跳到 008 (join)
006 jump_dest     # consequent 入口
007 load 0        # a   (consequent)
008 jump_dest     # join 汇合点
```

**步骤 3 — 验证回填偏移**：`consequent_index = 6`、`jump_if_index = 3` → `jump_if` 偏移 `+3`（L1804）；`join` 的 `jumpdest_index = 8`、`jump_index = 5` → `jump_rel` 偏移 `+3`（L1805）。与上面推导一致。

**步骤 4 — 模拟执行**：

- `a=5, b=3`：`gt` 得真 → `jump_if +3` 跳到 006 → `load 0` 压 `a=5` → 008 结束，返回 5。✓
- `a=2, b=7`：`gt` 得假 → 顺序到 004 `load 1` 压 `b=7` → `jump_rel +3` 跳到 008，返回 7。✓

**步骤 5 — 实际核对**：把 `max` 配一个 `#[test]`（断言 `max(5,3)==5`、`max(2,7)==7`），用 `interpreter_main --v=5 /tmp/max.x` 运行，在 `Emitted max ...:` 之后核对序列与跳转偏移是否与推导一致（本例无尾部 `pop`，原因见 4.3.3）。

**预期结果**：推导、源码模板、实际日志三者吻合；`max` 测试通过。

**待本地验证**：日志确切格式与是否额外内联展开（如 `gt` 的实现细节）依版本而异，以本机 `--v=5` 输出为准。

## 6. 本讲小结

- DSLX 字节码是**前端自己的线性中间表示**，用于主机端解释执行（测试、quickcheck、constexpr），与流向硬件的 XLS IR 不是一回事。
- 一条 `Bytecode` = 操作码 `Op` + 可选数据载荷 `Data`；指令遵循栈式语义，二元运算「左在 TOS1、右在 TOS0」。
- `BytecodeEmitter` 是个 `ExprVisitor`，对 AST 做**后序遍历**：先发操作数再发运算；参数和 `let` 绑定用整数**槽位**管理（`load`/`store`）；`if` 编译成 `jump_if`/`jump`/`jump_dest` 并用「占位 + 回填」处理跳转偏移。
- `BytecodeInterpreter` 是「操作数栈 + 帧栈」虚拟机：`Run` 双层循环取指—分派—执行；`kCall` 压新帧、函数结束弹帧，返回值经共享操作数栈回传。
- 发射必须在类型检查之后（有/无符号、位宽都依赖 `TypeInfo`）；`BytecodeCache` 让被调函数的字节码只发射一次。
- `interpreter_main --v=5` 能打印发射出的字节码，`--v=3` 能打印每条指令前后的栈深度，是观察这套机制的最直接窗口。

## 7. 下一步学习建议

- **进入 IR 世界（u3 单元）**：本讲的字节码只用于主机执行；真正流向优化和代码生成的是 XLS IR。建议接着读 u3-l1（IR 总览），对比「DSLX 字节码」与「XLS IR 节点」两种表示的取舍。
- **看 IR 解释器（u6-l1）**：XLS IR 也有自己的解释器（`xls/interpreter`），与本讲的 DSLX 字节码解释器对照阅读，能看清「前端执行」与「IR 执行」两套机制的异同。
- **读 JIT（u6-l2）**：`BytecodeInterpreterOptions` 里的 `post_fn_eval_hook` 正是为了把字节码解释器的结果和 JIT 比对（[bytecode_interpreter_options.h:56-63](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/bytecode/bytecode_interpreter_options.h#L56-L63)）；学完 JIT 会更理解这个钩子的用途。
- **深入 Proc 执行（u7-l1）**：本讲提到的 `kSend`/`kRecv`/`kSpawn` 与 `ProcInstance`、`ProcHierarchyInterpreter` 是 Proc 在主机上运行的基础，u7-l1 会展开讲。
