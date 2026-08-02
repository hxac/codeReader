# TableGen 与目标描述

## 1. 本讲目标

本讲是后端单元（u6）的收官篇，回答一个贯穿前面几讲的问题：**后端那成千上万条机器指令的描述、它们的二进制编码、汇编字符串、指令选择模式，到底是从哪里来的？**

学完本讲你应当能够：

- 说清 TableGen 这套领域特定语言（DSL）「**描述—生成**」的两段式工作方式：先用 `.td` 文件声明式地描述，再由 `llvm-tblgen` 把描述编译成大量 C++ 代码。
- 读懂一条目标指令在 `.td` 里是如何被定义的（指令格式、`outs`/`ins` 操作数、汇编字符串、`Pattern`），以及寄存器、寄存器类、调用约定的描述方法。
- 把本单元前面几讲看到的现象「接回源头」：u6-l2 里 SelectionDAG 的 `MatcherTable` 与 `OPC_MorphNodeTo`，正是 `-gen-dag-isel` 后端从 `.td` 的 `Pattern` 编译出来的；u6-l1 里目标类持有的 `InstrInfo`/`RegisterInfo`，正是 `.inc` 文件生成的。
- 理解 TableGen 为何能极大降低「新增一个目标后端」的成本，从而把工作量从 \(N \times K\)（N 个目标 × K 类代码生成关注点）降为 \(N + K\)（N 份 `.td` 描述 + K 个生成器）。

## 2. 前置知识

本讲默认你已掌握 u6-l1（后端流水线、`CodeGenPassBuilder`、MIR 层次）与 u6-l2（SelectionDAG 指令选择）的核心结论。在概念上还需要以下几点铺垫：

- **声明式 vs 命令式**：我们平时写的 C++ 是命令式的——一步步告诉机器「怎么做」。而 `.td` 是声明式的——只声明「是什么」（这条指令的编码长什么样、操作数是什么、匹配什么 IR 模式），由生成器决定怎么把它翻译成 C++。声明式描述的最大好处是**短**：一条指令可能只写 3 行 `.td`，却换来上百行生成的 C++ 表格与函数。

- **代码生成器（backend）这个词的双重含义**：在 LLVM 里，「后端」既指「目标后端」（X86/RISCV/ARM 这套把 IR 变机器码的东西），也指「TableGen 后端」（`-gen-instr-info` 这类把 `.td` 变成 `.inc` 的程序）。本讲里两者都会出现，注意结合上下文区分。

- **`.inc` 文件**：TableGen 的产物是 C++ 片段文件，习惯上以 `.inc` 结尾。它们**不是**手写的，文件头里写明「Automatically generated file, do not edit!」，靠 `#include` 被目标的 `.cpp`/`.h` 吞进去，再用宏开关（如 `GET_INSTRINFO_MC_DESC`）挑选要启用的片段。

- **dag（有向无环图）**：TableGen 里一种一等数据类型，写作 `(operator arg1, arg2:$name, ...)`，用来同时表达「指令的操作数列表」和「SelectionDAG 的匹配模式」。它是连接 `.td` 描述与 u6-l2 DAG 指令选择的纽带。

## 3. 本讲源码地图

本讲涉及的关键文件，按「语言核心 → 目标描述 → 生成器」三层组织：

| 层 | 文件 | 作用 |
| --- | --- | --- |
| 语言核心 | `llvm/include/llvm/TableGen/Record.h` | 定义 TableGen 的核心数据模型：`Record`（一条记录）、`RecordKeeper`（全部记录的容器）、`Init`/`DagInit`（值与 dag） |
| 语言核心 | `llvm/lib/TableGen/TGParser.cpp` | `.td` 文本的语法分析器，把 `class`/`def`/`defm`/`multiclass`/`let` 翻译成 `Record` |
| 目标描述 | `llvm/include/llvm/Target/Target.td` | **所有目标共享**的基类定义：`Register`、`RegisterClass`、`Instruction`、`Operand`、`Predicate` |
| 目标描述 | `llvm/include/llvm/Target/TargetCallingConv.td` | 调用约定相关基类：`CCIfType`、`CCAssignToReg`、`CallingConv`、`CalleeSavedRegs` |
| 目标描述 | `llvm/lib/Target/RISCV/*.td` | 一个真实目标（RISC-V）的描述，本讲的主要案例来源 |
| 生成器 | `llvm/utils/TableGen/llvm-tblgen.cpp` + `llvm/utils/TableGen/Basic/TableGen.cpp` | `llvm-tblgen` 可执行程序的入口与命令行注册 |
| 生成器 | `llvm/lib/TableGen/TableGenBackend.cpp` | TableGen 后端的公共工具，如生成「do not edit」文件头 |
| 生成器 | `llvm/utils/TableGen/InstrInfoEmitter.cpp` | `-gen-instr-info` 后端，把指令描述编译成 `*GenInstrInfo.inc` |
| 构建 | `llvm/lib/Target/RISCV/CMakeLists.txt` | 把「`.td` → 一组 `.inc`」的生成关系写进构建系统 |

> 说明：本讲选用 RISC-V（而不是更庞大的 X86）作为案例目标，因为它的 `.td` 干净、贴近手册；但讲到的机制对所有目标通用。

## 4. 核心概念与源码讲解

本讲按规格拆成两个最小模块：

- **4.1 TableGen `.td` 描述**：这门语言长什么样、语法分析后变成什么内存对象、如何用它描述寄存器/指令/调用约定。
- **4.2 后端代码生成**：`llvm-tblgen` 如何把描述编译成 `.inc`、生成哪些 C++ 接口、如何被构建系统接入。

---

### 4.1 TableGen `.td` 描述

#### 4.1.1 概念说明

TableGen 是 LLVM 自带的领域特定语言。它的核心思想是**把「目标相关的、高度重复的事实」从 C++ 代码里抽出来，用一种紧凑的声明式语言集中描述**，再由不同的「生成器」从同一份描述里各自榨取自己关心的信息，生成不同的 C++ 代码。

举一个直观的例子：一条「加法」机器指令，对**指令选择**来说关心的是「它匹配 IR 里的 `(add ...)` 模式」；对**汇编打印**来说关心的是「它的助记符是 `add`、操作数顺序是 `rd, rs1, rs2`」；对**二进制编码器**来说关心的是「`funct7=0000000, funct3=000, opcode=0110010` 这些位怎么摆」；对**反汇编器**又关心「怎么从一串字节识别出它」。这四个关注点共享**同一条**指令的事实，却需要四套不同的 C++ 实现。如果都手写，每新增一个目标都要把四套都重写一遍——这就是 \(N \times K\) 的灾难。

TableGen 的解法是：用**一份 `.td` 描述**把这条指令的全部事实写一次，再用**四个生成器**（`-gen-dag-isel`/`-gen-asm-writer`/`-gen-emitter`/`-gen-disassembler`）各自生成一份 `.inc`。新增目标只需写一份新的 `.td`（\(N\)），生成器对所有目标复用（\(K\)），于是工作量降为 \(N + K\)。

关键术语小结：

- **记录（Record）**：`.td` 里 `def` 出来的一个具体对象，例如一条指令、一个寄存器。它有名字、若干字段值、以及它继承的父类。
- **类（class）**：`.td` 里的「模板」，带模板参数与字段，本身**不会**被生成器直接当成一条记录；`def` 通过继承类并填参数来「实例化」出记录。
- **dag**：`(op a, b:$name)` 形式的值，用来描述操作数列表和 SelectionDAG 模式。

#### 4.1.2 核心流程

一份 `.td` 从文本到被生成器消费，经过一条固定的流水线：

```text
.td 文本
   │  ① TGLexer 切 Token
   ▼
Token 流
   │  ② TGParser 递归下降分析（class/def/defm/let/multiclass/foreach）
   ▼
RecordKeeper（全部 class 与 def 的容器，即「记录数据库」）
   │  ③ 某个生成器按需查询：getAllDerivedDefinitions("Instruction")
   ▼
挑选出的 Record 子集
   │  ④ 生成器遍历这些 Record，把它们字段里的值翻译成 C++ 文本
   ▼
*.inc 文件（C++ 片段）
```

注意第 ② 步：TableGen 的「执行」在**编译期**就完成了。`.td` 里的 `foreach`、`if`、`!cast<>`、`multiclass` 展开等，都是在 TGParser/Record 阶段求值的；等生成器拿到 `RecordKeeper` 时，所有模板早已展开成一张扁平的记录表。换句话说，**生成器看到的是一个已经「定型」的记录数据库**，它只做「读记录、吐 C++」这件事。

`.td` 语言的主要构件（关键字）如下表，它们都对应 TGParser 里的一个解析入口：

| 关键字 | 含义 | 举例 |
| --- | --- | --- |
| `class` | 定义带模板参数的模板（不产出记录） | `class ALU_ri<bits<3> f3, string s> { ... }` |
| `def` | 实例化**一个**具体记录 | `def ADDI : ALU_ri<0b000, "addi">;` |
| `multiclass` | 一段「批量 def」的模板 | `multiclass F3_12<string s, ...> { def rr:...; def ri:...; }` |
| `defm` | 实例化 multiclass，一次产出**多个**记录 | `defm ADD : F3_12<"add", ...>;` |
| `let ... in { }` | 在作用域内覆盖某些字段 | `let mayLoad=0, mayStore=0 in { ... }` |
| `defvar` / `foreach` / `if` | 编译期变量与控制流 | `defvar X = (add X1, X2);` |
| `assert` / `dump` | 编译期断言与调试输出 | `assert !eq(a, b), "msg";` |

#### 4.1.3 源码精读

**(1) 语法分析的入口与分发**

`TGParser::ParseObject` 是识别一个顶层语句的总分发器，它按首关键字决定调用哪个解析函数：

[llvm/lib/TableGen/TGParser.cpp:4720-L4724](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/lib/TableGen/TGParser.cpp#L4720-L4724) —— `ParseObject` 用 `switch` 按当前 Token 分发到 `ParseDef`/`ParseDefm`/`ParseClass`/`ParseMultiClass`/`ParseTopLevelLet` 等分支，错误提示里直接列出了全部合法关键字：`assert, class, def, defm, defset, dump, foreach, if, let`。这正是上一小节那张「关键字表」的源头。

[llvm/lib/TableGen/TGParser.cpp:3993-L4001](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/lib/TableGen/TGParser.cpp#L3993-L4001) —— `ParseDef` 的注释把文法写得很清楚：`DefInst ::= DEF ObjectName ObjectBody`。它先解析对象名、构造一个 `Record` 对象，再交给 `ParseObjectBody` 处理「`: 父类` + 字段赋值」的主体。

[llvm/lib/TableGen/TGParser.cpp:3962-L3988](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/lib/TableGen/TGParser.cpp#L3962-L3988) —— `ParseObjectBody` 揭示了 `def` 的三段结构：① 若遇到 `:` 就解析父类引用列表并 `AddSubClass`（即继承）；② `ApplyLetStack` 把外层 `let` 的字段覆盖压进来；③ `ParseBody` 解析大括号里的字段赋值。这正对应「`def 名字 : 父类<参数> { 字段=值 }`」的完整写法。

[llvm/lib/TableGen/TGParser.cpp:4351-L4389](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/lib/TableGen/TGParser.cpp#L4351-L4389) —— `ParseClass` 解析 `class`，注意它把记录的 `Kind` 标记为 `RK_Class`（而非 `RK_Def`），且 `class` 是「可前向声明、后补定义」的（看到重名就 `updateClassLoc`）。这印证了「class 本身不是一条记录，而是模板」。

[llvm/lib/TableGen/TGParser.cpp:4504-L4527](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/lib/TableGen/TGParser.cpp#L4504-L4527) 与 [llvm/lib/TableGen/TGParser.cpp:4603-L4620](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/lib/TableGen/TGParser.cpp#L4603-L4620) —— `ParseMultiClass`/`ParseDefm` 说明 `multiclass` 是「装着若干 `def` 的模板」，`defm` 则触发这些 `def` 的批量实例化。这就是「写一次 `defm`，生成 `XXXrr`/`XXXri` 多条指令」的机制所在。

**(2) 内存里的记录数据库**

语法分析的产物全部汇入 `RecordKeeper`。可以把它理解成「一张大表」，里面存着所有 `class` 和所有 `def`：

[llvm/include/llvm/TableGen/Record.h:1980-L1998](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/include/llvm/TableGen/Record.h#L1980-L1998) —— `RecordKeeper` 用两个 `std::map` 分别持有 `Classes` 和 `Defs`，并提供 `getClass`/`getDef` 按名查找。这是生成器访问记录的唯一入口。

[llvm/include/llvm/TableGen/Record.h:2059-L2059](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/include/llvm/TableGen/Record.h#L2059) —— `getAllDerivedDefinitions(ClassName)` 是生成器最常用的查询：返回所有继承自某个类的具体记录。例如 `-gen-instr-info` 会调用它拿到「全部 `Instruction`」，`-gen-register-info` 拿到「全部 `Register`」。生成器写法本质就是「查一张表 → 遍历 → 吐 C++」。

[llvm/include/llvm/TableGen/Record.h:1632-L1671](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/include/llvm/TableGen/Record.h#L1632-L1671) —— `Record` 类的字段揭示了记录的内部结构：`TemplateArgs`（模板参数）、`Values`（一组 `RecordVal`，即字段名+类型+值）、`DirectSuperClasses`（直接父类，且注释明确禁止菱形继承）、`Kind`（`RK_Def`/`RK_Class`/`RK_MultiClass`/`RK_AnonymousDef`）。一条指令在内存里就是一个 `Record`，它的 `outs`/`ins`/`AsmString`/`Pattern`/`Inst` 都是其中的 `Values`。

[llvm/include/llvm/TableGen/Record.h:286-L329](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/include/llvm/TableGen/Record.h#L286-L329) —— `Init` 是「值」的根基类，它的 `InitKind` 枚举列出了值的全部形态：`IntInit`（整数）、`StringInit`（字符串）、`BitsInit`（位串，用于指令编码）、`ListInit`（列表）、`DefInit`（指向另一条记录）、`DagInit`（dag）、各种 `*OpInit`（`!cast`/`!strconcat` 等 bang 运算）。

[llvm/include/llvm/TableGen/Record.h:1426-L1453](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/include/llvm/TableGen/Record.h#L1426-L1453) —— `DagInit` 是 dag 值的内存表示：一个 `Operator`（操作符，如 `outs`/`ins`/`add`/某条指令名）加上一组带名字的 `Args`/`ArgNames`。这正是 `(outs GPR:$rd)` 与 `(set GPR:$rd, (add GPR:$rs1, GPR:$rs2))` 在内存里的样子——操作数列表和指令选择模式用的是**同一种**数据结构。

**(3) 用 `.td` 描述一个真实目标（RISC-V）**

任何目标的 `.td` 都从 `include "llvm/Target/Target.td"` 开始，把公共基类引进来，再包含自己的子文件：

[llvm/lib/Target/RISCV/RISCV.td:9-L9](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/lib/Target/RISCV/RISCV.td#L9) —— RISC-V 顶层 `.td` 第一条就是引入公共的 `Target.td`。

[llvm/lib/Target/RISCV/RISCV.td:33-L36](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/lib/Target/RISCV/RISCV.td#L33-L36) —— 然后依次包含寄存器描述、调度、调用约定、指令描述。一份顶层 `.td` 就是把这些「分门别类」的子文件串成一份完整目标描述。

公共基类定义在 `Target.td`，它们是所有目标共同遵守的「契约」：

[llvm/include/llvm/Target/Target.td:173-L181](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/include/llvm/Target/Target.td#L173-L181) —— `Register` 类定义了一个物理寄存器：汇编名 `AsmName`、别名表 `Aliases`、子寄存器 `SubRegs`、DWARF 编号 `DwarfNumbers` 等。这些都是生成器关心的事实。

[llvm/include/llvm/Target/Target.td:292-L305](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/include/llvm/Target/Target.td#L292-L305) —— `RegisterClass` 把一组「类型相同、用途相近」的寄存器打包，并给出默认分配顺序。寄存器分配器（见 u6-l1 的 post-RA 阶段）就在这些类里挑物理寄存器。

[llvm/include/llvm/Target/Target.td:663-L676](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/include/llvm/Target/Target.td#L663-L676) —— `Instruction` 类是所有指令的根基类，关键字段一目了然：`OutOperandList`/`InOperandList`（输出/输入操作数 dag）、`AsmString`（汇编字符串）、`Pattern`（SelectionDAG 匹配模式 `list<dag>`）、`Uses`/`Defs`（隐式使用的寄存器）、以及一大批语义位（`isBranch`/`isCall`/`mayLoad`/`mayStore`/`isCommutable`…）。这些字段正是不同生成器各取所需的来源。

来看看 RISC-V 怎么实例化它们。**寄存器与寄存器类**：

[llvm/lib/Target/RISCV/RISCVRegisterInfo.td:14-L16](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/lib/Target/RISCV/RISCVRegisterInfo.td#L14-L16) —— RISC-V 用 `class RISCVReg<bits<5> Enc, ...> : Register<n>` 定义自己的寄存器子类，把 5 位硬件编码 `Enc` 直接写进 `HWEncoding` 字段。这就是「寄存器编号会进二进制编码」的来源。

[llvm/lib/Target/RISCV/RISCVRegisterInfo.td:308-L322](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/lib/Target/RISCV/RISCVRegisterInfo.td#L308-L322) —— `class GPRRegisterClass<dag regList>` 继承自一个 RISC-V 本地的 `RISCVRegisterClass`，声明寄存器类型为 `XLenVT`（XLEN 位整数）、对齐 32；而 `def GPR : GPRRegisterClass<(add (sequence "X%u", 10, 17), ...)>` 用 `(sequence ...)`、`(add ...)` 这种 dag 集合运算批量列出通用寄存器，并**给出了分配顺序**（先分配调用者保存的临时寄存器 `x10-x17`）。

**指令格式与指令定义**：

[llvm/lib/Target/RISCV/RISCVInstrFormats.td:306-L311](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/lib/Target/RISCV/RISCVInstrFormats.td#L306-L311) —— `class RVInst<...>` 声明了 `field bits<32> Inst`——一条 32 位编码字段。RISC-V 的指令格式类（I/R/S/B/U/J 型）就是在它基础上，用 `let Inst{...} = ...` 把 `funct3`/`funct7`/`opcode`/`rd`/`rs1`/`rs2` 这些位段钉到 `Inst` 的特定位上。

[llvm/lib/Target/RISCV/RISCVInstrInfo.td:686-L689](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/lib/Target/RISCV/RISCVInstrInfo.td#L686-L689) —— `class ALU_ri<bits<3> funct3, string opcodestr>` 是「立即数 ALU 运算」的指令模板：它继承 I 型格式 `RVInstI`，把 `funct3` 和 `OPC_OP_IMM` 传进去定编码，并把操作数声明为 `(outs GPR:$rd), (ins GPR:$rs1, simm12_lo:$imm12)`、汇编串 `"$rd, $rs1, $imm12"`、附带调度信息 `Sched<[WriteIALU, ReadIALU]>`。

[llvm/lib/Target/RISCV/RISCVInstrInfo.td:796-L797](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/lib/Target/RISCV/RISCVInstrInfo.td#L796-L797) —— 真正的 `ADDI` 指令定义只有一行：`def ADDI : ALU_ri<0b000, "addi">;`，外层 `let isReMaterializable = 1 ...` 再覆盖几个语义位。**一条 3 行 `.td` 就完整描述了 ADDI 的编码、操作数、汇编串、调度**——这就是声明式描述的威力。

[llvm/lib/Target/RISCV/RISCVInstrInfo.td:818-L821](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/lib/Target/RISCV/RISCVInstrInfo.td#L818-L821) —— 寄存器-寄存器版本同理：`def ADD : ALU_rr<0b0000000, 0b000, "add", Commutable=1>`。注意 `Commutable=1` 这个模板参数最终会被 `ALU_rr` 写进 `isCommutable` 字段（见 [RISCVInstrInfo.td:698-L703](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/lib/Target/RISCV/RISCVInstrInfo.td#L698-L703)），让后端优化知道 `add` 的两个源操作数可交换。

**调用约定**：

[llvm/include/llvm/Target/TargetCallingConv.td:30-L32](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/include/llvm/Target/TargetCallingConv.td#L30-L32) 与 [llvm/include/llvm/Target/TargetCallingConv.td:118-L120](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/include/llvm/Target/TargetCallingConv.td#L118-L120) —— `CCIfType<[类型], 动作>` 表示「若当前参数是指定类型，则执行动作」；`CCAssignToReg<[R0,R1]>` 表示「把参数分配到第一个可用寄存器」。组合起来 `CCIfType<[f32,f64], CCAssignToReg<[R0,R1]>>` 就声明了一段调用约定规则。`CallingConv<[动作列表]>`（[第 212 行](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/include/llvm/Target/TargetCallingConv.td#L212)）把这些规则聚合成一条完整的约定。

[llvm/include/llvm/Target/TargetCallingConv.td:235-L237](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/include/llvm/Target/TargetCallingConv.td#L235-L237) —— `CalleeSavedRegs<dag saves>` 描述一组被调用者保存寄存器。RISC-V 就用它定义各 ABI 的 CSR 列表，例如 [RISCVCallingConv.td:16-L19](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/lib/Target/RISCV/RISCVCallingConv.td#L16-L19) 里的 `def CSR_ILP32_LP64 : CalleeSavedRegs<(add CSR_ILP32E_LP64E, (sequence "X%u", 18, 27))>;`。（说明：RISC-V 的参数传递调用约定 `CC_RISCV` 较复杂，实际在 `RISCVISelLowering.cpp` 用 C++ 实现，`.td` 主要负责 CSR 与简单情形；本讲用官方文档里的 SPARC 例子讲解 `CCIfType`/`CCAssignToReg` 语义，原理一致。）

#### 4.1.4 代码实践

**目标**：亲手体验「`.td` 文本 → 解析 → 记录数据库 → 打印」这条链，建立对 TableGen 的直接体感。

**操作步骤**：

1. 在仓库里随便建一个最小 `.td` 文件（以下为**示例代码**，非项目原有文件）：

   ```text
   // 示例代码：mini.td
   class Shape<int sides> {
     int NumSides = sides;
     string Name = "shape";
   }
   def Triangle  : Shape<3> { let Name = "tri"; }
   def Square    : Shape<4>;
   ```

   它定义了一个模板 `Shape`，并实例化出两条记录 `Triangle`、`Square`。

2. 用构建好的 `llvm-tblgen` 把它解析后**原样打印**记录数据库：

   ```bash
   llvm-tblgen --print-records mini.td
   ```

3. 也可以只打印某类的所有派生记录名：

   ```bash
   llvm-tblgen --print-enums --class=Shape mini.td
   ```

**需要观察的现象**：

- `--print-records` 的输出里应能看到 `Triangle`、`Square` 两条 `def`，每条都带 `NumSides` 和 `Name` 字段值；注意 `Triangle` 的 `Name` 被 `let` 改成了 `"tri"`，而 `Square` 沿用了模板默认值 `"shape"`。
- `--print-enums` 会输出 `Triangle, Square,`——这正是生成器用 `getAllDerivedDefinitions("Shape")` 能拿到的对象列表。

**预期结果**：你会直观看到「`class` 是模板、`def` 是记录、`let` 是字段覆盖」，并理解生成器面对的就是这样一张扁平表。

**若没有现成的 `llvm-tblgen**：可参照 u1-l3 先做一个最小构建（`cmake -DLLVM_TARGETS_TO_BUILD=X86 -DLLVM_ENABLE_PROJECTS="" ...`），`llvm-tblgen` 会随核心一起产出；或在本地构建目录 `bin/` 下查找。若无构建环境，则把上述命令与预期输出记为「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `class` 本身不会出现在 `--print-records` 的输出里（只看到 `def`）？

> **参考答案**：`class` 是模板（`Record::RK_Class`），只用于被 `def` 继承与实例化；生成器和 `--print-records` 关心的是「具体记录」（`RK_Def`），即 `RecordKeeper::getDefs()`。模板在解析期展开完成后，其本身不再作为一条可生成的记录。

**练习 2**：RISC-V 的 `def ADDI : ALU_ri<0b000, "addi">;` 里，`0b000` 和 `"addi"` 分别填进了 `ALU_ri` 的哪个模板参数？它们最终影响 `ADDI` 记录的哪些字段？

> **参考答案**：分别填进 `funct3` 与 `opcodestr`。`funct3` 经 `RVInstI` 写入 `Inst` 位段的 `funct3` 区（影响二进制编码），`opcodestr` 成为汇编助记符 `addi`（影响汇编打印与匹配）。`outs`/`ins`/`AsmString` 则由 `ALU_ri` 模板统一提供（`rd/rs1/imm12`）。

**练习 3**：`(outs GPR:$rd)` 这个 dag 在内存里对应哪种 `Init` 子类？

> **参考答案**：`DagInit`。其操作符是 `outs`，带一个名为 `rd`、值为 `GPR`（一条 `RegisterClass` 记录的 `DefInit`）的参数。

---

### 4.2 后端代码生成

#### 4.2.1 概念说明

「后端代码生成」模块回答：记录数据库如何变成目标后端真正使用的 C++。

TableGen 的生成器是一组**彼此独立**的程序模块，每个对应 `llvm-tblgen` 的一个 `-gen-xxx` 选项。同一个 `RecordKeeper` 喂给不同的生成器，会产出不同用途的 `.inc`：

| `-gen-xxx` | 产物 `.inc` | 服务于 | 与前面讲义的联系 |
| --- | --- | --- | --- |
| `-gen-instr-info` | `*GenInstrInfo.inc` | 目标的 `InstrInfo` 类（指令描述符表） | u6-l1 目标类持有的 `getInstrInfo` |
| `-gen-register-info` | `*GenRegisterInfo.inc` | 目标的 `RegisterInfo`（寄存器/寄存器类表） | u6-l1 寄存器分配 |
| `-gen-dag-isel` | `*GenDAGISel.inc` | SelectionDAG 的 `SelectCode`/匹配表 | **u6-l2 的 `MatcherTable`/`OPC_MorphNodeTo`** |
| `-gen-asm-writer` | `*GenAsmWriter.inc` | `printInstruction`（汇编打印） | u6-l4 AsmPrinter→MCStreamer |
| `-gen-asm-matcher` | `*GenAsmMatcher.inc` | 汇编器指令匹配 | clang/llvm-mc 解析汇编 |
| `-gen-emitter` | `*GenMCCodeEmitter.inc` | `getBinaryCodeForInstr`（指令→字节） | u6-l4 MC 层编码 |
| `-gen-disassembler` | `*GenDisassemblerTables.inc` | 字节→指令的解码表 | 反汇编 |
| `-gen-callingconv` | `*GenCallingConv.inc` | 调用约定处理函数 | 本讲 4.1 的调用约定描述 |
| `-gen-subtarget` | `*GenSubtargetInfo.inc` | 子目标特性/调度模型 | `-mcpu`/`-mattr` |

**关键洞察**：一条 `def ADDI` 同时被好几个生成器读取——编码器读 `Inst` 位段、汇编器读 `AsmString`、选择器读 `Pattern`、指令信息表读语义位。**同一份描述，多视角消费**，这正是 TableGen 的核心价值。

#### 4.2.2 核心流程

从「写好 `.td`」到「`.inc` 被编译进目标库」，经过四步：

```text
① 注册：每个生成器用静态对象把名字("gen-instr-info")与回调注册进命令行
        │
② 调用：CMake 在构建期对每个目标调用 llvm-tblgen -gen-xxx，传入 *.td
        │
③ 生成：选中的回调拿到 RecordKeeper，查询+遍历，写出 *.inc（带 do-not-edit 头）
        │
④ 消费：目标的 *.cpp/*.h 用 #include "XxxGen*.inc" 吞进生成代码，并用宏挑选片段
```

第 ② 步尤其重要：`.inc` 是在**构建 LLVM 的过程中**生成的（构建期，不是运行期）。你编 LLVM 时看到的 `llvm-tblgen` 调用就是在做这件事；它先于 `llc`/`clang` 被编译。

#### 4.2.3 源码精读

**(1) `llvm-tblgen` 入口与命令行注册**

[llvm/utils/TableGen/llvm-tblgen.cpp:13-L18](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/utils/TableGen/llvm-tblgen.cpp#L13-L18) —— `llvm-tblgen` 的 `main` 只是转调 `tblgen_main`，注释解释这种间接是为了让 `llvm::cl::` 的静态变量同时链进 `llvm-tblgen` 与 `llvm-min-tblgen` 两个可执行文件。

[llvm/utils/TableGen/Basic/TableGen.cpp:58-L70](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/utils/TableGen/Basic/TableGen.cpp#L58-L70) —— 这是「内置/通用」生成器的注册表：一个 `Opt X[]` 数组把名字（如 `print-records`、`null-backend`、`dump-json`）和回调函数绑定，其中 `print-records` 标记为默认（第 4 个参数 `true`）。

**那么 `-gen-instr-info` 这些「目标相关」生成器在哪里注册？** 答案是：**每个生成器源文件的末尾**各注册自己。这是一种基于静态初始化的插件式注册：

[llvm/utils/TableGen/InstrInfoEmitter.cpp:1468-L1469](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/utils/TableGen/InstrInfoEmitter.cpp#L1468-L1469) —— `InstrInfoEmitter.cpp` 的最后一行用 `OptClass<InstrInfoEmitter> X("gen-instr-info", "Generate instruction descriptions");` 把自己注册为 `-gen-instr-info`。`OptClass` 模板会自动用 `InstrInfoEmitter` 的 `run(raw_ostream&)` 方法作为回调。其它生成器同理：

- `-gen-register-info`：[RegisterInfoEmitter.cpp:2186](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/utils/TableGen/RegisterInfoEmitter.cpp#L2186)
- `-gen-dag-isel`：[DAGISelEmitter.cpp:219](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/utils/TableGen/DAGISelEmitter.cpp#L219)
- `-gen-asm-writer`：[AsmWriterEmitter.cpp:1366](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/utils/TableGen/AsmWriterEmitter.cpp#L1366)
- `-gen-asm-matcher`：[AsmMatcherEmitter.cpp:4287](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/utils/TableGen/AsmMatcherEmitter.cpp#L4287)
- `-gen-subtarget`：[SubtargetEmitter.cpp:2371](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/utils/TableGen/SubtargetEmitter.cpp#L2371)
- `-gen-callingconv`：[CallingConvEmitter.cpp:396](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/utils/TableGen/CallingConvEmitter.cpp#L396)

[llvm/utils/TableGen/InstrInfoEmitter.cpp:55-L65](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/utils/TableGen/InstrInfoEmitter.cpp#L55-L65) —— `InstrInfoEmitter` 类持有 `const RecordKeeper &Records`，对外只暴露 `run(raw_ostream &OS)`。它的构造注释说得很直白：「Output the instruction set description」——拿到记录库，把指令描述写出去。这正是「查询 + 遍历 + 吐 C++」的典型形态。

**(2) 生成器的公共工具：文件头**

所有 `.inc` 顶部都有一段醒目的「do not edit」注释，由公共函数生成：

[llvm/lib/TableGen/TableGenBackend.cpp:93-L119](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/lib/TableGen/TableGenBackend.cpp#L93-L119) —— `emitSourceFileHeader` 负责画那段 `/*===- TableGen'erated file ...` 框，并写上「Automatically generated file, do not edit!」和源文件名。下次你在 `build/` 目录看到 `.inc` 时，就能认出它出自这里。

**(3) `.inc` 如何被消费：宏开关模式**

生成器产出的 `.inc` 不是「整文件直接编译」，而是**用预处理宏挑选片段**。同一段 `*GenInstrInfo.inc` 里塞了好几段被 `#ifdef GET_xxx` 包裹的代码，目标文件在 `#include` 前 `#define` 想要的那一个，就能只取出对应部分。例如官方指南描述的写法（见 `WritingAnLLVMBackend.md`）：

```c++
// 在 XXXInstrInfo.cpp 里，想要 getNamedOperandIdx 的定义：
#define GET_INSTRINFO_NAMED_OPS
#include "XXXGenInstrInfo.inc"
```

这种「一份 `.inc` + 多个宏开关」的设计，让一个生成器能同时给 `.h` 和 `.cpp`、给不同用途提供片段，而不必拆成多个文件。

**(4) 构建系统：把 `.td → .inc` 接进 CMake**

每个目标目录的 `CMakeLists.txt` 用两条命令完成接线：

[llvm/lib/Target/RISCV/CMakeLists.txt:3-L3](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/lib/Target/RISCV/CMakeLists.txt#L3) —— `set(LLVM_TARGET_DEFINITIONS RISCV.td)` 声明「这个目标的根 `.td` 是 `RISCV.td`」。

[llvm/lib/Target/RISCV/CMakeLists.txt:5-L20](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/lib/Target/RISCV/CMakeLists.txt#L5-L20) —— 一连串 `tablegen(LLVM RISCVGen*.inc -gen-*)` 调用，每条就是「用 `llvm-tblgen` 跑某个生成器，产出某个 `.inc`」。比如第 9 行 `-gen-dag-isel` 产出 `RISCVGenDAGISel.inc`（u6-l2 的匹配表就在这里），第 12 行 `-gen-instr-info` 产出 `RISCVGenInstrInfo.inc`，第 16 行 `-gen-register-info` 产出 `RISCVGenRegisterInfo.inc`。

> 这条 CMake 链正好把本讲与 u6-l2 串起来：u6-l2 你看到的 `SelectCodeCommon` 在 `RISCVISelDAGToDAG.cpp` 里 `#include "RISCVGenDAGISel.inc"`，而那个 `.inc` 就是这里的第 9 行生成的。源流闭环。

**(5) 生成器产出的 C++ 长什么样**

以官方 `WritingAnLLVMBackend.md` 为权威示例（避免编造），它给出 TableGen 从 `def AL : Register<"AL">` 生成的 `X86GenRegisterInfo.inc` 片段大致是：

```c++
// 由 def AL : Register<"AL">; 生成（示意，来自官方文档）
static const unsigned GR8[] = { X86::AL, ... };
const unsigned AL_AliasSet[] = { X86::AX, X86::EAX, X86::RAX, 0 };
const TargetRegisterDesc RegisterDescriptors[] = {
  { "AL", "AL", AL_AliasSet, Empty_SubRegsSet, ... }, ...
};
```

以及从指令的 `Pattern` 生成的 `XxxGenDAGISel.inc` 里的 `SelectCode`/`Select_ISD_STORE` 函数——后者正是 u6-l2 讲过的「字节码模式匹配机」的 C++ 体现。这些内容在 `llvm/docs/WritingAnLLVMBackend.md` 的「Instruction Set」「Instruction Selector」章节有完整说明，是理解「`.td` 字段 → C++ 接口」对应关系最可靠的参考。

#### 4.2.4 代码实践

**目标**（即本讲的 `practice_task`）：以 RISC-V 的 `ADDI` 为例，说清「一条指令是如何被定义的」，并追踪「它会生成哪些 C++ 接口/`.inc`」。

**操作步骤**：

1. 打开 [llvm/lib/Target/RISCV/RISCVInstrInfo.td:796-L797](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/lib/Target/RISCV/RISCVInstrInfo.td#L796-L797) 的 `def ADDI : ALU_ri<0b000, "addi">;`。
2. 沿继承链向上：`ALU_ri`（[686 行](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/lib/Target/RISCV/RISCVInstrInfo.td#L686)）→ `RVInstI`（I 型格式）→ `RVInst`（[306 行](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/lib/Target/RISCV/RISCVInstrFormats.td#L306)，`bits<32> Inst`）→ 公共 `Instruction`（[Target.td:663](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/include/llvm/Target/Target.td#L663)）。逐层记录 `ADDI` 最终获得了哪些字段。
3. 对照 [RISCV/CMakeLists.txt:5-L20](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/lib/Target/RISCV/CMakeLists.txt#L5-L20)，填写下表（即「它生成哪些接口」）：

   | `.td` 字段 / 语义 | 喂给的生成器 | 产出的 `.inc` | 生成的 C++ 接口/表 |
   | --- | --- | --- | --- |
   | `bits<32> Inst`（编码位段） | `-gen-emitter` | `RISCVGenMCCodeEmitter.inc` | `getBinaryCodeForInstr` |
   | `AsmString`（`addi $rd, ...`） | `-gen-asm-writer` | `RISCVGenAsmWriter.inc` | `printInstruction` |
   | `AsmString` + 操作数 | `-gen-asm-matcher` | `RISCVGenAsmMatcher.inc` | 汇编匹配表 |
   | `Pattern`（若有）/语义位 | `-gen-dag-isel` | `RISCVGenDAGISel.inc` | `SelectCode` 匹配表 |
   | 全部指令描述符 | `-gen-instr-info` | `RISCVGenInstrInfo.inc` | 指令描述符表 + `OpName` 枚举 |
   | `Inst` 位段（反向） | `-gen-disassembler` | `RISCVGenDisassemblerTables.inc` | 解码表 |

4. （可选，需构建）在有构建目录的前提下，查看真实生成物：

   ```bash
   # 在 LLVM 构建目录下
   ls lib/Target/RISCV/RISCVGen*.inc
   # 抽查指令信息表的片段
   grep -n "ADDI" lib/Target/RISCV/RISCVGenInstrInfo.inc | head
   ```

**需要观察的现象**：

- 一条 `ADDI` 的 `def` 只有寥寥几行，却能驱动 **6 个以上**生成器各产出一段 C++——这就是 \(N+K\) 的直观体现。
- `grep ADDI` 在不同 `.inc` 里都能命中，但内容完全不同：在 `GenInstrInfo.inc` 里是指令描述符行，在 `GenAsmWriter.inc` 里关联汇编串，在 `GenMCCodeEmitter.inc` 里关联编码位段。

**预期结果**：你能向别人讲清「ADDI 的 `.td` 定义 → 各字段 → 各生成器 → 各 `.inc` → 被 `RISCVInstrInfo.cpp`/`RISCVAsmPrinter.cpp`/`RISCVISelDAGToDAG.cpp` 消费」的完整链路。

**若没有构建目录**：第 4 步记为「待本地验证」；第 1–3 步纯源码阅读，现在即可完成。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `-gen-dag-isel`、`-gen-asm-writer` 等生成器没有出现在 `Basic/TableGen.cpp` 的 `Opt X[]` 数组里？

> **参考答案**：`Basic/TableGen.cpp` 里的 `X[]` 只登记「通用/内置」动作（如 `print-records`、`dump-json`）。目标相关生成器采用**分布式注册**——每个 `*Emitter.cpp` 末尾用一个静态 `OptClass`/`Opt` 对象自注册。只要这些 `.cpp` 被链进 `llvm-tblgen`，它们的静态对象就会在 `main` 之前把名字登记进同一个命令行解析器。

**练习 2**：如果你新增了一条机器指令的 `def`，但忘了给它写 `Pattern`，会怎样？

> **参考答案**：`-gen-dag-isel` 不会为它生成「从 IR 模式匹配到这条指令」的规则（除非另有 `def : Pat<...>`），于是 SelectionDAG 自动选不到它；但它的编码、汇编串、指令描述符照常由其它生成器产出。也就是说，这条指令能被汇编器/反汇编器/编码器认识，却不会在普通 IR 编译中被自动选用——通常需要目标在 `XXXISelDAGToDAG.cpp` 里手工选中，或补上 `Pat`。

**练习 3**：`emitSourceFileHeader` 写出的「do not edit」头，除了提示人工不要改，还有什么实际作用？

> **参考答案**：它也写入**源 `.td` 文件名**（`From: <filename>`），便于从生成物反查描述来源；同时统一了生成文件的格式边界，方便审查与 diff。更本质地，它强调这些文件是**构建期产物**——修改它们应在 `.td` 或生成器里做，然后重新构建。

---

## 5. 综合实践

把本讲两个模块串起来，完成一个「**追踪一条指令的完整生命周期**」的小任务：

1. **选一条指令**：以 RISC-V 的 `ADD`（[RISCVInstrInfo.td:818](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/lib/Target/RISCV/RISCVInstrInfo.td#L818)）为对象。
2. **描述层**（模块 4.1）：画出 `ADD` 的「字段来源树」——`funct7`/`funct3`/`opcode` 来自 `ALU_rr`→`RVInstR` 的编码位段；`outs`/`ins`/`AsmString` 来自 `ALU_rr`；`isCommutable=1` 来自模板参数；语义位来自外层 `let`。说明每个字段在 `Record` 的 `Values` 里如何表示（用 `DagInit`/`BitsInit`/`IntInit` 对号入座）。
3. **生成层**（模块 4.2）：列出 `ADD` 会出现在哪些 `.inc` 里、各自以什么形态出现，并指出这些 `.inc` 被 RISC-V 后端的哪些 `.cpp` 消费（提示：汇编打印看 `RISCVAsmPrinter.cpp`、指令选择看 `RISCVISelDAGToDAG.cpp`、指令信息看 `RISCVInstrInfo.cpp`）。
4. **回扣前讲**：用一句话说明 `ADD` 的 `Pattern`（若存在）如何经 `-gen-dag-isel` 变成 u6-l2 的 `MatcherTable`，最终在 `SelectCodeCommon` 里被 `OPC_MorphNodeTo` 选中。

完成后，你应当能用一张图把「`.td` 一行 `def` → `Record` → 多个 `.inc` → 后端 C++ → u6-l1 流水线里的某个阶段」完整贯通。这正是本单元（u6）从「流水线总览」一路到「描述源头」的收口。

## 6. 本讲小结

- TableGen 是一门**声明式** DSL，采用「**描述（`.td`）—生成（`llvm-tblgen`）**」两段式：一份描述，多个生成器各取所需，把目标相关的重复事实压缩到最少。
- `.td` 经 `TGLexer`→`TGParser` 解析后，变成一张扁平的 `RecordKeeper` 记录数据库；`class` 是模板、`def` 是记录、`defm`/`multiclass` 批量实例化、`let` 覆盖字段、`dag` 描述操作数与匹配模式。
- 一条指令 = 一个 `Record`，其 `outs`/`ins`/`AsmString`/`Pattern`/`bits<N> Inst`/语义位等字段，是编码器、汇编器、选择器、指令信息表等多个生成器的**共同数据源**。
- 每个 `-gen-xxx` 生成器在各自 `*Emitter.cpp` 末尾用静态对象自注册，运行时通过 `getAllDerivedDefinitions` 查询记录、遍历后写出带「do not edit」头的 `.inc`。
- CMake 用 `set(LLVM_TARGET_DEFINITIONS)` + 一串 `tablegen(... -gen-*)` 在**构建期**把 `.td` 编译成 `.inc`；目标代码再用「宏开关 + `#include`」消费这些片段。
- TableGen 的根本价值：把新增一个后端的工作量从 \(N \times K\) 降为 \(N + K\)——这正解释了 LLVM 为何能支持数十种目标架构。

## 7. 下一步学习建议

- **想动手写后端**：直接精读 [llvm/docs/WritingAnLLVMBackend.md](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/docs/WritingAnLLVMBackend.md)（本讲多次引用），它以 SPARC 为例给出从 `TargetMachine` 到寄存器、指令、选择、汇编打印、子目标的完整步骤。
- **想看一个「最小后端」**：仓库里的实验性目标（用 `-DLLVM_EXPERIMENTAL_TARGETS_TO_BUILD=Dummy` 可启用）是最小骨架；也可对照一个较简单的正式目标如 `MSP430` 或 `LoongArch` 通读其 `.td`。
- **承接 u9**：本单元结束后，u9（二次开发与工程实践）的「u9-l4 添加一个新后端」会以本讲为基础，展开后端必须实现的 `.td` 文件清单与 C++ 类清单，建议两讲连读。
- **深入 SelectionDAG 与 GlobalISel 的描述侧**：u6-l2 讲了选择器如何用匹配表，本讲讲了匹配表从哪来；若想看 GlobalISel 的等价物，可读 `-gen-global-isel` 产物（`RISCVGenGlobalISel.inc`，见 [RISCV/CMakeLists.txt:23](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/lib/Target/RISCV/CMakeLists.txt#L23)），对照 u6-l3 的四阶段流水线。
