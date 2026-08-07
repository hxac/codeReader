# ISA 概览：寄存器组与状态寄存器

## 1. 本讲目标

本讲是「指令集架构（ISA）规范」单元的第一讲。我们不写一行 RTL，而是以 ZipCPU 的官方规范 `doc/src/spec.tex` 为主线，先把「CPU 要做什么」讲清楚——具体到程序员可见的三件事：

1. **寄存器组**：CPU 内部有哪些寄存器，为什么有用户/监管**两套**。
2. **状态寄存器 CC**：那 16 位状态字里的每一位分别是什么含义。
3. **运行模式**：supervisor 模式与 user 模式的区别，以及它们和中断、寄存器组切换的关系。

学完本讲，你应当能够：

- 画出 ZipCPU 的两套寄存器组，并指出哪些寄存器有特殊硬件含义（PC、CC、SP、LR、FP）。
- 默写出状态寄存器 CC 低 16 位的功能位图，并解释 GIE、SLEEP、STEP 这几个关键控制位。
- 说清楚一次「中断进入」时 CPU 自动完成了什么，为什么 ZipCPU 不需要中断向量表。

> 本讲只讲 ISA（规范层面）。寄存器组与 CC 在硬件里**如何实现**，是第 3 单元 `zipcore.v` 的主题；本讲会在关键处给出一两处 RTL 锚点作为「规范落地证据」，但不展开流水线细节。

## 2. 前置知识

在进入正题前，先用一句话建立直觉：

- **ISA（指令集架构）**：CPU 与软件之间的「合同」。它规定有哪些寄存器、指令长什么样、状态如何反映。ISA 是抽象的，同一份 ISA 可以有多种硬件实现。
- **寄存器（register）**：CPU 内部最快的存储单元，软件用名字（如 `R1`）引用它。
- **程序计数器 PC**：记录「下一条要执行的指令在哪里」的寄存器。
- **状态/条件码寄存器 CC**：记录上次运算结果特征（是否为 0、是否溢出等）和控制 CPU 行为（是否开中断、是否单步）的寄存器。
- **特权级 / 运行模式**：很多 CPU 分「内核态」和「用户态」，用户态不能执行某些敏感操作。ZipCPU 对应地叫 **supervisor mode**（监管态）和 **user mode**（用户态）。
- **中断（interrupt）**：硬件打断当前程序、强制 CPU 跳去处理某事件（如外设就绪）的机制。

如果你读过本手册的 [u1-l1 项目概览](u1-l1-project-overview.md)，应该记得 ZipCPU 的一个独特设计：**用两套寄存器组替代中断向量表**。本讲正是要把这句话拆开讲透。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用到的地方 |
| --- | --- | --- |
| `doc/src/spec.tex` | ISA 官方规范（LaTeX 源）。本讲的绝对主线。 | Operating Modes、Register Set、The Status Register CC 三个子节 |
| `README.md` | 项目总览，用通俗语言重述关键设计目标。 | 双模式、双寄存器组的动机说明 |
| `rtl/core/zipcore.v` | CPU 内核的 RTL 实现（本讲仅用作「规范落地证据」）。 | 寄存器堆声明、CC 位拼接两处锚点 |

本讲的规范原文都在 spec.tex 的「Instruction Set Architecture」一章内，其结构如下（行号为本讲引用依据）：

- ISA 章首与指令通式：`spec.tex` 第 335 行起
- `\subsection{Operating Modes}`：第 363 行
- `\subsection{Register Set}`：第 379 行
- `\subsection{The Status Register, CC}`：第 435 行

## 4. 核心概念与源码讲解

### 4.1 运行模式：supervisor 与 user

#### 4.1.1 概念说明

ZipCPU 是一台**两模式（two-mode）机器**：supervisor mode（监管态）与 user mode（用户态），两者的访问级别不同。这一点在项目 README 的设计目标清单里写得很直白：

> A two mode machine: supervisor and user, with each mode having a different access level.

参见 [README.md:L22-L22](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/README.md#L22-L22) —— 这一行确立了「双模式」是设计目标，而非实现副作用。

这两个模式与中断紧密绑定，理解的关键是下面这张「模式 ↔ 中断」对照：

- 处于 **user 模式**时，中断**总是开启**的（CPU 随时可被打断）。
- 处于 **supervisor 模式**时，中断**总是关闭**的（中断处理代码不会被打断）。
- **CPU 上电/复位后进入 supervisor 模式**，所以引导程序（bootloader）天然拥有最高权限。

#### 4.1.2 核心流程

模式之间如何切换？规范用一段话讲清了整条链路：

```
复位 → 进入 supervisor 模式（中断关）
        │
        │  执行 RTU（Return to Userspace）指令
        ▼
     user 模式（中断开）
        │
        │  遇到 中断 或 异常(fault)
        ▼
     回到 supervisor 模式，且「停在离开时的那条指令上」
```

注意三个要点：

1. 进入 user 模式靠软件主动执行 `RTU` 指令；从 user 回 supervisor 则由**中断或异常**自动触发。
2. 回到 supervisor 时，PC 指向「离开 user 模式时正在执行的那条指令」——这意味着中断返回后只要再执行一次 `RTU` 就能继续用户程序，不需要专门的中断向量地址。
3. **ZipCPU 不支持中断向量表**。没有「中断号 → 处理函数地址」的查表过程，中断统一回到 supervisor 上下文，由 supervisor 用软件判断原因。

#### 4.1.3 源码精读

规范的原文如下（Operating Modes 子节）：

[doc/src/spec.tex:L363-L378](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L363-L378) —— 这一段规定了双模式的全部规则：user 模式中断常开、supervisor 模式中断常关、复位进 supervisor、`RTU` 切到 user、中断/异常回到 supervisor 并停在原指令、无中断向量。

README 则用一段更通俗的话给出「为什么要这样设计」的动机——双模式配合双寄存器组，让中断现场保存几乎零成本：

[README.md:L34-L34](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/README.md#L34-L34) —— 解释了「没有中断向量、只有两套寄存器组；中断时只是从 user 寄存器组切到 supervisor 寄存器组」，supervisor 的上下文在两次中断之间被自动保留。

#### 4.1.4 代码实践

- **实践目标**：用规范原文验证「模式切换的方向」与「中断开关」的对应关系。
- **操作步骤**：
  1. 打开 `doc/src/spec.tex`，跳到第 363 行的 `Operating Modes` 子节。
  2. 找到描述 user 模式与 supervisor 模式各自中断是否开启的那两句。
  3. 找到「The CPU boots into supervisor mode」这句，确认复位后的初始模式。
  4. 找到 `RTU` 指令的作用描述。
- **需要观察的现象**：你会看到「user 模式 always enabled / supervisor 模式 always disabled」这种**绝对化**的措辞——这意味着模式本身就是中断开关，不需要单独的「开关中断」指令。
- **预期结果**：复位后处于 supervisor（中断关）；执行 `RTU` 进入 user（中断开）；发生中断/异常自动回 supervisor。
- **说明**：本实践为纯源码阅读，无需运行；结论可直接从规范得出。

#### 4.1.5 小练习与答案

**练习 1**：为什么 ZipCPU 不需要类似 x86 `cli`/`sti`（关/开中断）这样的专用指令？

> **参考答案**：因为「是否开中断」与「当前模式」绑定——supervisor 模式即中断关、user 模式即中断开。切换模式（`RTU` 或中断返回）本身就完成了开/关中断，所以不必再设独立指令。

**练习 2**：一段运行在 user 模式的程序里发生了除零异常，CPU 接下来会停在什么模式的什么位置？

> **参考答案**：会回到 supervisor 模式，并且 PC 指向「触发异常的那条除法指令」。supervisor 处理完后重新执行 `RTU` 即可重试或跳过该指令。

---

### 4.2 寄存器组：两套各 16 个通用寄存器

#### 4.2.1 概念说明

ZipCPU 有**两套**「16 个 32 位通用寄存器」：一套给 supervisor 模式，一套给 user 模式。这是本 ISA 最具特色的设计之一。

- 每套 16 个寄存器，编号 `R0`–`R15`，每个 32 位（所有寄存器、地址、指令都是 32 位，见 [README.md:L5-L5](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/README.md#L5-L5)）。
- supervisor 套记作 `sR0`–`sR15`，user 套记作 `uR0`–`uR15`。
- 任意一次模式切换，都会**整体**从一套切换到另一套——不是只换一两个寄存器。

其中有两个寄存器在**硬件层面**是特殊的：

- **PC（R15）**：程序计数器。
- **CC（R14）**：状态寄存器（下一节专题）。

另外三个寄存器是**工具链约定**（编译器遵守，硬件并不强制）：

- **R0 = LR（Link Register）**：子程序返回地址。
- **R12 = FP（Frame Pointer）**：栈帧指针（编译器需要时才用）。
- **R13 = SP（Stack Pointer）**：栈指针；使用压缩指令集（CIS）时，指令编码对 R13 的偏移做了优化，方便压栈/出栈。

#### 4.2.2 核心流程

「切换模式 = 切换整套寄存器」是理解中断现场保存的关键。下图是规范里的寄存器堆示意（左右两套）：

```
   Supervisor Register Set            User Register Set
   （编号 0–15，中断关）                 （编号 16–31，中断开）
   sR0 (LR)   sR8                      uR0 (LR)   uR8
   sR1        sR9                      uR1        uR9
   sR2        sR10                     uR2        uR10
   sR3        sR11                     uR3        uR11
   sR4        sR12(FP)                 uR4        uR12(FP)
   sR5        sSP                      uR5        uSP
   sR6        sCC                      uR6        uCC
   sR7        sPC                      uR7        uPC
```

> 注意上图里 `sR6/sCC`、`sR7/sPC` 与规范图（`sCC`、`sPC` 占 R14/R15）在视觉排布上等价：CC 就是 R14、PC 就是 R15。

寄存器如何被「寻址」到正确的那一套？规范在第 4.3 节里点破了一个精巧的细节：**GIE 位（CC 的第 5 位）同时也是寄存器地址的第 5 位**。也就是说，一个寄存器的「物理编号」是一个 5 位值：

\[ \text{寄存器物理编号} \;=\; \{\,\text{GIE},\; \text{Rn}[3{:}0]\,\} \;\in\; [0,\,31] \]

- GIE = 0（supervisor）→ 编号 0–15，即 `sR0`–`sR15`。
- GIE = 1（user）→ 编号 16–31，即 `uR0`–`uR15`。

这正是上图标注「0–15 / 16–31」的由来。也正因如此，supervisor 想访问 user 寄存器时，只需在 `MOV` 指令里把第 5 位置 1（用 `uR0`–`uR15` 这种写法），就能跨套读写；除此之外两套互不干扰，编译器甚至「不知道」第二套寄存器的存在。

#### 4.2.3 源码精读

规范的 Register Set 子节给出了寄存器堆图与全部约定：

[doc/src/spec.tex:L379-L433](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L379-L433) —— 这一段包含：两套 16×32 寄存器、PC=R15/CC=R14 的硬件特殊性、CIS 对 R13 偏移的优化，以及 R0=LR / R12=FP / R13=SP 的工具链约定。

我们再给一处 RTL 锚点，证明规范确实落地到了硬件——`zipcore.v` 里寄存器堆的声明：

[rtl/core/zipcore.v:L166-L166](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L166-L166) —— `reg [31:0] regset [0:(OPT_USERMODE)? 31:15];`。当构建参数 `OPT_USERMODE` 打开时，寄存器堆是 **32 项**（0–15 给 supervisor、16–31 给 user）；关闭用户模式时退化为 16 项。这一行就是把「两套寄存器组」写进硅片的证据。

#### 4.2.4 代码实践

- **实践目标**：从规范与 RTL 两处确认「模式切换会换掉哪些寄存器」。
- **操作步骤**：
  1. 读 `doc/src/spec.tex` 第 405–409 行（Register Set 子节中关于「Any switch … will also cause a sudden shift from one register set to the other」的描述）。
  2. 读 `rtl/core/zipcore.v` 第 166 行，确认寄存器堆在 `OPT_USERMODE` 下是 32 项。
  3. 在草稿纸上把 `sR0…sR15` 与 `uR0…uR15` 左右对照列出，圈出 PC、CC、SP、LR、FP 五个特殊寄存器。
- **需要观察的现象**：你会确认「切换」是**整套 16 个寄存器同时换**，而不是逐个保存。
- **预期结果**：从 user 态进入 supervisor 态时，`uR0…uR15`（含 uPC/uCC/uSP/uLR/uFP）整组「冻结」在原处，CPU 改用 `sR0…sR15`；由于 supervisor 套在中断期间被完整保留，所以中断处理不需要把用户寄存器逐个压栈。
- **说明**：纯阅读型实践，无需运行。

#### 4.2.5 小练习与答案

**练习 1**：编译器生成的代码里出现了 `R5`，它指的是 `sR5` 还是 `uR5`？

> **参考答案**：指「当前活动那一套」里的 R5。规范明确：除 supervisor 用 `MOV` 显式写 `uR0`–`uR15` 之外，任何寄存器引用都指向当前活动寄存器组。而且「编译器对第二套寄存器组一无所知」，所以编译器只会用当前组。

**练习 2**：为什么把 PC 和 CC 也放进通用寄存器编号空间（R15、R14），而不是做成完全独立的寄存器？

> **参考答案**：这样它们就能和通用寄存器用同一套 `MOV` 通路读写，也自动获得「两套」（uPC/sPC、uCC/sCC）。中断时 user 的 PC/CC 被完整保留在 user 套里，supervisor 拿到的是自己上次离开时的 sPC/sCC——这正是「停在离开时的那条指令」能成立的物理基础。

---

### 4.3 状态寄存器 CC：16 位的功能位图

#### 4.3.1 概念说明

状态寄存器 **CC（R14）** 之所以单独成节，是因为它的每一位都有专门含义。CC 只有**低 16 位**有意义，高 16 位保留。

按用途，这 16 位可以分成三组：

1. **ALU 条件标志（第 0–3 位）**：Z / C / N / V，反映最近一次（设标志的）ALU 运算结果。它们是条件执行的判断依据（下一讲 [u2-l4 条件执行](u2-l4-conditional-execution.md) 会展开）。
2. **控制位（第 4–7 位）**：SLEEP / GIE / STEP / BREAK，软件写它们来控制 CPU 行为。
3. **状态/异常位（第 8–15 位）**：非法指令、陷阱、总线错误、除零、浮点异常、压缩指令相位、清缓存命令位等，多为硬件置位、软件读取。

#### 4.3.2 核心流程

下表汇总了 CC 各位（规范 + RTL 双重印证，见 4.3.3）。R = 只读，W = 只写（读回为 0），R/W = 可读可写。

| 位 | 读写 | 名称 | 含义 |
| ---: | :---: | :--- | :--- |
| 0 | R/W | **Z** Zero | 最近一次 ALU 运算结果为 0 |
| 1 | R/W | **C** Carry | 无符号进位/溢出；移位指令用它捕获最后移出的一位 |
| 2 | R/W | **N** Negative | 结果符号位（最高位）为 1 |
| 3 | R/W | **V** Overflow | 有符号算术溢出 |
| 4 | R/W | **SLEEP** | 睡眠位。GIE=1（用户态）时 = `WAIT` 等中断；GIE=0（监管态）时 = `HALT` 停机 |
| 5 | R/W | **GIE** | 全局中断使能 / 用户模式位。=1 用户态、=0 监管态；**同时也是寄存器组的第 5 位地址** |
| 6 | R/W | **STEP** | 单步位（仅存在于 user 的 CC；supervisor 的 CC 该位恒 0）。置位后，进入 user 态只执行一条指令便回到 supervisor |
| 7 | R/W | **BREAK** | user CC：遇到 `BREAK` 指令则置位；supervisor CC：断点使能（break-enable） |
| 8 | R | Illegal | 非法指令标志 |
| 9 | R | Trap | 陷阱/软中断标志；任何返回 user 态时清零 |
| 10 | R | Bus Error | 总线错误标志（仅 load/store，取指错误归入 illegal） |
| 11 | R | Div by 0 | 除零异常标志 |
| 12 | R | FPU err | （预留）浮点异常 |
| 13 | R | CIS phase | 压缩指令前半段标志（状态位） |
| 14 | W | Clear I-Cache | 写 1 清指令缓存；读回恒 0；user 态写无效 |
| 15 | W | Clear D-Cache | 写 1 清数据缓存；读回恒 0；user 态写无效 |
| 31–16 | — | Reserved | 保留 |

关于「模式」与 CC 的一个关键细节：**GIE 位在读回时是被「强制」的**——

- 读 supervisor 的 CC（sCC）：第 5 位（GIE）**永远读回 0**。
- 读 user 的 CC（uCC）：第 5 位（GIE）**永远读回 1**。

也就是说，软件不能靠「写 GIE」来骗过模式判断；硬件保证 sCC.GIE=0、uCC.GIE=1，让「读 CC 的第 5 位」等价于「问当前是不是在 user 模式」。

#### 4.3.3 源码精读

规范的 CC 位定义表（bitlist）在这里：

[doc/src/spec.tex:L440-L464](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L440-L464) —— 这就是本讲实践任务要对照的「CC 定义表」原文，逐位给出读写属性与含义。

几个最关键控制位的详细阐述在规范里各占一段：

- 第 4 位 SLEEP：[doc/src/spec.tex:L490-L502](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L490-L502) —— 解释了同一位在 user/ supervisor 下分别实现 `WAIT` 与 `HALT`，以及 `OPT_CLKGATE` 可在睡眠时停时钟。
- 第 5 位 GIE：[doc/src/spec.tex:L504-L522](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L504-L522) —— 解释了 GIE 既是中断开关又是模式位，还是寄存器地址的第 5 位；并明确 sCC 恒读 0、uCC 恒读 1。
- 第 6 位 STEP：[doc/src/spec.tex:L523-L539](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L523-L539) —— 解释了 STEP 让 user 态「单步即返回」，用于用一个程序调试另一个程序；并指出压缩指令与 `LOCK` 是单步的例外。

规范之外，再给一处强有力的 RTL 印证。`zipcore.v` 在构造 user/supervisor 两份 CC 时，几乎是把上表逐位拼出来的：

[rtl/core/zipcore.v:L2440-L2447](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L2440-L2447) —— `w_uflags`（user CC）与 `w_iflags`（supervisor CC）的位拼接。对照可读出：第 15–14 位是 `2'b00`（清缓存位读回为 0）、第 13 位是压缩指令相位、第 12 位浮点异常、第 11 位除零、第 10 位总线错、第 9 位 trap、第 8 位非法指令、第 7 位 user 下是 `ubreak`/supervisor 下是 `break_en`、第 6 位 user 下是 `!gie && user_step`（故 supervisor 读到 0）、**第 5 位 user 恒为 `1'b1`、supervisor 恒为 `1'b0`**、第 4 位 sleep、第 3–0 位是 4 个 ALU 标志。这行 RTL 与规范表格一一对应，是「规范即实现」的明证。

ALU 标志（Z/C/N/V，第 3–0 位）也是按当前模式从两套里选一套的：

[rtl/core/zipcore.v:L1334-L1334](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L1334-L1334) —— `assign op_Fl = (op_gie)?(w_uflags[3:0]):(w_iflags[3:0]);`：user 模式用 user 套的 4 个标志，supervisor 模式用 supervisor 套的 4 个标志。这与「两套寄存器组各自带一份 CC」完全一致。

> 小提示：`zipcore.v` 第 169 行有一行注释 `// (BUS, TRAP,ILL,BREAKEN,STEP,GIE,SLEEP ), V, N, C, Z`，是作者对控制/状态位顺序的速记，可作为本节的「速查口诀」。

#### 4.3.4 代码实践

- **实践目标**：在 spec.tex 的 CC 定义表里定位 GIE / SLEEP / STEP，并把它们与「模式切换」联系起来。
- **操作步骤**：
  1. 打开 `doc/src/spec.tex`，跳到第 440–464 行的 CC 位定义表。
  2. 在表中找到 **GIE**（第 5 位）、**SLEEP**（第 4 位）、**STEP**（第 6 位），记下它们的读写属性与含义。
  3. 再读第 504–522 行（GIE 详解）与第 523–539 行（STEP 详解）。
  4. 结合 4.2 节，回答：从 user 态进入 supervisor 态时，哪些寄存器会被切换？
- **需要观察的现象**：你会看到 SLEEP 的含义「随模式而变」（user 下是 WAIT，supervisor 下是 HALT），以及 STEP 在 supervisor 的 CC 里恒为 0。
- **预期结果**（本任务答案可直接从规范得出）：
  - GIE = **第 5 位**，R/W：全局中断使能兼用户模式位（=1 user/中断开，=0 supervisor/中断关），同时是寄存器地址的第 5 位。
  - SLEEP = **第 4 位**，R/W：GIE=1 时为 WAIT（等中断），GIE=0 时为 HALT（停机）。
  - STEP = **第 6 位**，R/W：仅 user 的 CC 有意义；置位后进入 user 态只执行一条指令即回 supervisor。
  - **从 user 态进入 supervisor 态时，整套 16 个通用寄存器全部切换**：由 user 组 `uR0`–`uR15`（编号 16–31，含 uPC/uCC/uSP/uLR/uFP）切到 supervisor 组 `sR0`–`sR15`（编号 0–15，含 sPC/sCC/sSP/sLR/sFP）。user 组原地冻结，故 supervisor 处理中断无需手动压栈保存现场。
- **说明**：本实践为源码阅读，预期结果已由规范与 RTL 双重确定，无需运行；若你想在模拟器里眼见为实，可在学完 [u1-l4](u1-l4-first-simulation.md) 与调试端口相关内容后，通过调试端口读 sCC/uCC 验证第 5 位的差异（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：软件在 user 模式下读 CC 的第 5 位，会读到什么？为什么不能靠「写这一位为 0」来关中断？

> **参考答案**：读到 1。因为 user 的 CC（uCC）第 5 位硬件强制恒为 1。而且「关中断」等价于「切到 supervisor 模式」，user 态软件无权自行降级——只能通过 `TRAP`（置第 9 位，见规范第 570–573 行）请求 supervisor，或等中断/异常把自己「打回」supervisor。

**练习 2**：第 14、15 位标注为 W（只写，读回 0）。为什么清缓存要用「只写」位而不是普通可读位？

> **参考答案**：这两位是「命令位」：写 1 触发一次清缓存动作，动作完成后位本身不需要保持状态，所以读回设计为 0。这也让软件能用「读回是否为 0」无副作用地感知命令已被接受，同时 user 态写它们无效——只有 supervisor 才能清缓存。

**练习 3**：取指发生总线错误时，会置 CC 的第 10 位（Bus Error）吗？

> **参考答案**：不会。规范明确（第 590–591 行）取指流水线的总线错误被当作**非法指令**上报，因此置的是第 8 位（Illegal），而不是第 10 位。第 10 位只反映 load/store 的总线错误。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个「中断进入全景」小任务。

**任务**：用一段文字 + 一张时序草图，描述「一段 user 程序正在运行时，外部中断到达」之后，到「supervisor 开始执行中断处理第一条指令」之前，CPU 自动发生的全部变化。要求覆盖：

1. 模式与中断开关：从哪个模式、中断从开到关的转折点（对应 GIE 位的变化）。
2. 寄存器组切换：哪 16 个寄存器被整体换掉、PC/CC 落到哪一套。
3. CC 里会被硬件置位/影响的状态位（提示：思考 trap、step、sleep 是否参与）。

**参考要点**（你可以拿自己的答案和下面对照）：

- 中断到达时 CPU 处于 user 模式（GIE=1，中断开）。中断被接受后，GIE 被清 0 → 进入 supervisor 模式、中断关。
- 整套寄存器从 user 组（`uR0`–`uR15`）切到 supervisor 组（`sR0`–`sR15`）。user 的 PC/CC 被原样留在 user 组里（uPC 指向被打断的那条指令），supervisor 用的是自己上次离开时的 sPC/sCC——因此中断「返回」到 supervisor 后，PC 自然停在「离开 user 时的那条指令」上。
- 注意：外部硬件中断**不会**置 trap 位（trap 是 user 主动请求时才置，见第 570–573 行）；STEP 位若被 supervisor 提前置位，会在 user 程序「单步一条」后才触发返回，与本场景的中断返回是两套机制；SLEEP 位不参与中断进入，但中断是「把 CPU 从 sleep 唤醒」的事件源（第 490–492 行）。
- 结论：这次「自动现场保存」的代价几乎是零——没有寄存器压栈、没有向量查表，全靠硬件双寄存器组完成。这就是 ZipCPU 用双寄存器组替代中断向量表的核心收益。

## 6. 本讲小结

- ZipCPU 是**双模式**机器：supervisor（中断关，复位进入）与 user（中断开，靠 `RTU` 进入）；中断/异常会自动把 CPU 从 user 打回 supervisor，并停在原指令处。**没有中断向量表**。
- 寄存器组有**两套各 16 个 32 位通用寄存器**，编号 `R0`–`R15`；硬件特殊的是 PC（R15）与 CC（R14），工具链约定 R0=LR、R12=FP、R13=SP。
- 模式切换 = **整套 16 个寄存器整体换组**；GIE 位（CC 第 5 位）兼作寄存器地址的第 5 位，决定选 user 组（16–31）还是 supervisor 组（0–15）。
- 状态寄存器 CC 只有低 16 位有意义：第 0–3 位是 Z/C/N/V 四个 ALU 标志；第 4–7 位是 SLEEP/GIE/STEP/BREAK 控制位；第 8–15 位是非法指令/陷阱/总线错/除零/浮点/CIS 相位/清缓存等状态或命令位。
- 一个关键不变量：sCC 的 GIE 恒读 0、uCC 的 GIE 恒读 1，软件无法伪造当前模式。
- 规范与 RTL 高度一致——`zipcore.v` 第 166 行的 32 项寄存器堆、第 2440–2447 行的 CC 位拼接，几乎是 spec.tex CC 表的逐位翻译。

## 7. 下一步学习建议

本讲建立了「寄存器 + 状态字 + 模式」的程序员视图。接下来建议：

1. **先横向读完 ISA**：按本单元顺序，下一讲 [u2-l2 指令格式与编码](u2-l2-instruction-format-encoding.md) 会讲清 32 位指令字的字段划分，让你能手工解析一条指令；其后是寻址/访存、条件执行、中断双寄存器组深入。
2. **回到规范做交叉**：本讲引用的 spec.tex 第 363–634 行，建议你完整通读一遍，对照本讲的表格查漏补缺。
3. **为第 3 单元埋线**：等进入 `rtl/core/zipcore.v` 时，回头重看本讲给的两处 RTL 锚点（第 166、2440–2447 行），你会更清楚地看到「规范里的每一句话在硬件里对应什么」。
