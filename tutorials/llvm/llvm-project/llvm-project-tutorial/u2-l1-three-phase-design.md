# 三段式编译器设计与 IR 的角色

## 1. 本讲目标

本讲是「LLVM IR 与三段式编译」单元的第一讲。读完本讲，你应该能够：

- 讲清楚编译器「前端 → IR → 后端」三段式架构里，每一段的**输入**和**输出**分别是什么。
- 说出 IR（中间表示）为什么是这套设计的核心：它把「M 种语言 × N 个目标」的组合爆炸从 \(M \times N\) 降为 \(M + N\)。
- 在源码里定位「clang 作为前端」「LLVM 核心作为后端/优化器」的位置，并能在一张流程图上指出 **IR 出现在哪些工具之间**。

本讲承接 u1-l4：那一讲介绍了 LLVM IR 的三种形态（内存 `Module`、`.ll` 文本、`.bc` 位码）以及 `opt`/`llc`/`lli` 等工具。本讲要回答的是更根本的问题——**为什么整个 LLVM 要围绕 IR 来设计？** u1-l1 曾勾勒过三段式的轮廓，这里我们深入到「为什么」与源码证据。

## 2. 前置知识

在进入源码之前，先用通俗语言建立三个直觉。

- **编译器做什么**：把一种语言（源码，贴近人）翻译成另一种（通常是机器能执行的代码，贴近机器）。
- **为什么不能「一口气」翻译**：源码和机器码之间的差距非常大，一步到位的翻译器会极其复杂，而且每换一种语言、每换一个 CPU 都要重写一遍，无法复用。
- **分而治之**：与其一步翻译，不如先翻译成一种**中间语言**，再从中间语言翻译到目标。这个「中间语言」就是 IR（Intermediate Representation，中间表示）。

回顾 u1-l4 的关键结论：LLVM IR 有三种等价形态——内存中的 `Module` 对象、人类可读的 `.ll` 文本、紧凑的 `.bc` 位码；`opt` 是「优化驱动」，`llc` 是「代码生成驱动」，二者最终都调用 `PassManager.run`。本讲会把这些工具摆到三段式的大图里，看清它们各自属于哪一段。

需要熟悉的术语：前端（Frontend）、中端 / 优化器（Middle-end / Optimizer）、后端（Backend）、目标（Target）、IR、模块（Module）。

## 3. 本讲源码地图

本讲只涉及两个文件，但它们正好分别代表「项目的自我定位」与「前端入口的真实代码」。

| 文件 | 作用 |
| --- | --- |
| `README.md` | 仓库顶层说明。它用一段话直接写明了「Clang 前端 → LLVM bitcode(IR) → 目标文件」的三段式，是最权威的「项目自述」级证据。 |
| `clang/tools/driver/driver.cpp` | 你在命令行敲 `clang` 时真正进入的入口文件。它揭示了 clang 内部的两层结构：外层 **Driver**（编排「该跑哪些动作」）+ 内层 **cc1**（真正的前端工作，产出 IR）。这让我们能在源码里「看见」前端与后端的分界线。 |

工具 `opt`/`llc`/`lli` 在 u1-l4 已经讲过，本讲把它们当作「中端 / 后端」一侧的引用，不再重复展开其内部实现。

## 4. 核心概念与源码讲解

本讲包含两个最小模块：**三段式架构** 与 **IR 的桥梁作用**。

### 4.1 三段式架构

#### 4.1.1 概念说明

现代「可重定向」（retargetable）编译器把工作切成三段，每段只懂一件事：

- **前端（Frontend）**：懂**源语言**，不懂目标机器。把源码翻译成 IR。LLVM 中 C/C++ 的前端是 Clang。
- **中端 / 优化器（Middle-end）**：既尽量不懂具体源语言，也尽量不懂具体目标机器。在 IR 上做与语言、平台无关的优化（常量折叠、死代码消除、循环优化……）。LLVM 中由一整套 Pass 承担，`opt` 是其驱动。
- **后端（Backend）**：懂**目标机器**，不懂源语言。把 IR 翻译成某个 CPU/平台的机器码（指令选择、寄存器分配、指令调度、代码发射）。LLVM 中由 `llc` 等承担，并按 target（X86 / ARM / RISCV / Wasm……）分别实现。

这套设计要解决的问题，是**编译器的复用**。如果每种语言都直通每种目标，工程量会爆炸；有了统一 IR，前端和后端就能各自独立扩展。

#### 4.1.2 核心流程

三段式各阶段的输入、输出与职责如下：

```
          ┌───────────────┐
源码       │   前端         │  词法 / 语法 / 语义分析 + CodeGen
.c/.cpp ─▶│   Clang (cc1) │
          └──────┬────────┘
                 │  LLVM IR
                 │  (.ll 文本 / .bc 位码 / 内存 Module)
          ┌──────▼────────┐
          │  中端（优化）  │  opt + 一系列 Pass
          │   IR ──▶ IR   │  与语言 / 平台无关的优化
          └──────┬────────┘
                 │  优化后的 LLVM IR
          ┌──────▼────────┐
          │   后端         │  llc：指令选择 / 寄存器分配 / 代码发射
          │  IR ──▶ 机器码 │  按 target (X86/ARM/…) 分别实现
          └──────┬────────┘
                 │
          目标代码 (.s 汇编 / .o 目标文件 / 可执行程序)
```

一句话抓住本质：**前端只负责产出 IR 就「下班」，后端只读 IR 就开工；IR 是两段之间的硬接口。**

为什么这能省力？设要支持 \(M\) 种源语言、\(N\) 个目标：

\[
\text{无统一 IR 的翻译路径数} = M \times N
\qquad
\text{有统一 IR 的翻译路径数} = M + N
\]

举例：\(M=3\) 种语言、\(N=4\) 个目标时，无 IR 需要写 \(3\times4=12\) 套「语言→目标」翻译；有 IR 则只需 \(3\) 个前端 + \(4\) 个后端 = \(7\) 套。语言和目标越多，差距越大。

#### 4.1.3 源码精读

**证据一：README 用一句话写死了三段式。**

[README.md:13-17](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/README.md#L13-L17) 说明核心「LLVM」包含「处理中间表示（intermediate representations）并把它们转换成目标文件（object files）」所需的全部工具、库与头文件——这正是「中端 + 后端」一侧的职责。

紧接着 [README.md:19-21](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/README.md#L19-L21) 直接点出三段式：

> "C-like languages use the Clang frontend. This component compiles C, C++, Objective-C, and Objective-C++ code **into LLVM bitcode** -- and **from there into object files, using LLVM**."

两个分句——「into LLVM bitcode」与「from there … using LLVM」——恰好就是「前端产出 IR」与「后端从 IR 出发」的两个边界。

**证据二：`driver.cpp` 展示前端内部还有「编排层 + 真正前端」两层。**

先看文件头注释 [clang/tools/driver/driver.cpp:9-11](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/clang/tools/driver/driver.cpp#L9-L11)：它自述只是「a thin wrapper for functionality in the Driver clang library」，印证 u1-l2 讲过的「命令行工具是薄壳」。

真正的入口是 [clang/tools/driver/driver.cpp:242](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/clang/tools/driver/driver.cpp#L242) 的 `clang_main`。它做了三件能对应到「编排」的事：

1. 构造外层编排者：
   ```cpp
   Driver TheDriver(Path, llvm::sys::getDefaultTargetTriple(), Diags,
                     /*Title=*/"clang LLVM compiler", VFS);
   ```
   见 [clang/tools/driver/driver.cpp:360-361](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/clang/tools/driver/driver.cpp#L360-L361)。
2. 把命令行解析成一个「编译计划」：`TheDriver.BuildCompilation(Args)`，见 [clang/tools/driver/driver.cpp:388](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/clang/tools/driver/driver.cpp#L388)。
3. 执行该计划：`TheDriver.ExecuteCompilation(*C, FailingCommands)`，见 [clang/tools/driver/driver.cpp:419](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/clang/tools/driver/driver.cpp#L419)。

那么「真正把源码变成 IR」的活在哪里？当命令行里出现 `-cc1` 时，会进入 [clang/tools/driver/driver.cpp:271-278](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/clang/tools/driver/driver.cpp#L271-L278) 的分支，调用 `ExecuteCC1Tool`，进而派发到 `cc1_main`：

```cpp
if (Tool == "-cc1")
  return cc1_main(ArrayRef(ArgV).slice(1), ArgV[0], GetExecutablePathVP);
```

见 [clang/tools/driver/driver.cpp:228-229](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/clang/tools/driver/driver.cpp#L228-L229)。

小结：Driver 层负责「这次该跑哪些动作（预处理/编译/汇编/链接）、按什么顺序、用什么工具链」；真正的前端工作（词法、语法、语义、CodeGen，最终产出 IR）在 `cc1` 里。所以**clang 作为「前端」时，产出物是 IR**；之后才轮到 LLVM 后端接手。

#### 4.1.4 代码实践

**实践目标**：亲手在「前端 → IR」边界上「截断」一次，亲眼看到 IR 作为独立产物出现。

**操作步骤**：

1. 准备一个最简 C 文件（示例代码）：
   ```c
   // 示例代码：add.c
   int add(int a, int b) { return a + b; }
   ```
2. 看 Driver 的编排计划（不真正执行）：
   ```bash
   clang -### -c add.c
   ```
   `-###` 让 Driver 只打印「打算执行哪些命令」而不真正运行。观察它把一次编译展开成了哪几步。
3. 在前端出口处截断，拿到 IR：
   ```bash
   clang -S -emit-llvm add.c -o add.ll
   ```
   这一步只跑到「前端产出 IR」为止，`add.ll` 就是三段式里「前端 → IR」边界的产物。
4. 打开 `add.ll`，辨认三样东西：模块头的 `target` 行、函数定义 `define ... @add(...)`、以及函数体里的 `ret` 指令。

**需要观察的现象**：第 2 步会打印出形如 preprocess / compile / assemble 的一系列子命令（待本地验证，具体条目随 clang 版本与平台而异）；第 3 步得到一个文本文件 `add.ll`，它**不是汇编**，而是 LLVM IR。

**预期结果**（`add.ll` 的关键片段，示例代码，待本地验证）：
```llvm
; 示例输出（实际以本地 clang 版本为准）
define dso_local i32 @add(i32 noundef %0, i32 noundef %1) #0 {
  %3 = add nsw i32 %1, %0
  ret i32 %3
}
```
能确认「前端确实把 `a + b` 变成了一条 IR 的 `add` 指令并以 `ret` 结尾」即可。

> 说明：本讲环境只安装了 `clang`（`/usr/bin/clang`），`opt`/`llc` 未安装。若你已按 u1-l3 自行构建了 LLVM，可继续 `llc add.ll -o add.s`（后端：IR→汇编）、`opt -passes=instcombine add.ll -o opt.ll`（中端：IR→优化后 IR）把整条三段式跑完；这两步的输出**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：若没有统一 IR，3 种语言、4 个目标需要多少套「语言→目标」的翻译路径？有了 IR 呢？
**答案**：无 IR 为 \(3\times4=12\) 套；有 IR 为 \(3\) 个前端 \(+\) \(4\) 个后端 \(=7\) 套。

**练习 2**：在 `driver.cpp` 里，「真正把源码编译成 IR」的逻辑更可能藏在 `clang_main`、`Driver TheDriver`、还是 `cc1_main` 里？为什么？
**答案**：在 `cc1_main` 里。`clang_main` 是总入口，`Driver TheDriver` 只负责编排（解析参数、规划动作、调度工具链），真正的前端（词法/语法/语义/CodeGen）发生在 `cc1` 中。

### 4.2 IR 的桥梁作用

#### 4.2.1 概念说明

如果说 4.1 讲清了「三段怎么切」，本模块要讲清「为什么切在 IR 这里最划算」。IR 之所以是整套设计的关键，有四条理由：

1. **它是一份稳定的契约**：前端只要会产出合规的 IR，后端只要会消费合规的 IR，两边互不关心对方的内部细节。
2. **「一次优化，处处受益」**：在 IR 层写的任何优化，对所有「(语言, 目标)」组合都自动生效——这是把优化从 M×N 个地方收敛到 1 处的根本来源。
3. **可持久化 / 可序列化**：IR 既能存在内存里，也能落盘成 `.ll`/`.bc`（回顾 u1-l4 的三种形态）。因此前端和后端可以拆成不同进程、不同时间，甚至不同机器——链接时优化（LTO）、bitcode 分发、JIT 执行都建立在此基础上。
4. **扩展成本极低**：新加一门语言只需写一个「该语言 → IR」的前端，完全不必碰后端；新加一个 CPU 只需写一个「IR → 该 CPU 机器码」的后端，完全不必碰任何前端。

#### 4.2.2 核心流程

IR 的桥梁作用，可以用一个「多对一、一对多」的漏斗来刻画：

```
 Clang (C/C++)       ─┐
 Flang (Fortran)     ─┤          ┌─▶ X86 后端
 Swift 前端          ─┼─▶ LLVM IR ─▶ ARM 后端
 Rust (llvm 后端)    ─┤   (统一)  ├─▶ RISCV 后端
 Kaleidoscope 教程   ─┘          └─▶ WebAssembly 后端
```

无论多少种语言从前端涌入、多少个目标从后端流出，**它们都汇合到同一份 IR**。这就是为什么 u1-l4 里 `opt`、`llc`、`lli` 能各自独立地读取同一个 `.bc`：因为 IR 是一份干净、可序列化的契约，前端和这些工具**根本不需要在同一个进程里**。也正是这层桥，让 `clang` 可以用 `-emit-llvm` 在前端「收工」，再由完全独立的工具继续后续工作。

#### 4.2.3 源码精读

本模块最强的一条源码证据，仍是 [README.md:19-21](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/README.md#L19-L21) 那句：

> "compiles C, C++, Objective-C … **into LLVM bitcode** -- and **from there into object files, using LLVM**."

前半句「into LLVM bitcode」是 Clang 的职责终点，后半句「from there … using LLVM」是 LLVM 后端的职责起点——**IR 就是横亘在「from there」前后的那座桥**。

代码侧，[clang/tools/driver/driver.cpp:228-229](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/clang/tools/driver/driver.cpp#L228-L229) 把控制权交给 `cc1_main`，正是「要过桥了」的那一刻：cc1 的 CodeGen 会生成 IR 这个「桥上的货物」，随后无论是继续在 cc1 里降到目标码，还是写回 `.bc` 交给独立的 `llc`，跨越的都是同一条 IR 边界。

#### 4.2.4 代码实践

**实践目标**：在工具链里**找出所有 IR 边界**，回答「哪些工具之间传递的是 IR，哪些不是」。

**操作步骤**：

1. `clang -S -emit-llvm add.c -o add.ll` —— 产出 `.ll`。这是「前端 → IR」边界，IR **第一次以可读文件**出现。
2. 若已有 `llvm-as` / `opt` / `llc`（需按 u1-l3 构建），依次：
   ```bash
   llvm-as add.ll -o add.bc      # IR 文本 → IR 位码（仍是 IR）
   opt -passes=instcombine add.bc -o opt.bc   # IR → 优化后 IR（仍是 IR）
   llc opt.bc -o opt.s           # IR → 汇编（从此不再是 IR）
   ```
3. 画一张流程图，在每个箭头上标注「此时是 IR 吗？(.ll/.bc/内存 Module) 还是已经离开 IR？(.s/.o)」。

**需要观察的现象**：注意第 2 步中 `llvm-as`、`opt` 的**输入和输出都是 IR**（只是换了形态或做了优化），唯独 `llc` 的输出 `.s` 已经是汇编、不再是 IR。这正说明**整个中端都在 IR 内部「打转」**。

**预期结果**：
- `clang`（emit-llvm）之前：源码，**不是** IR。
- `clang -emit-llvm` 之后、`llc` 之前：**全部是** IR（`.ll` 或 `.bc`）。
- `llc` 之后：汇编 / 目标码，**不再是** IR。

> 说明：本环境未安装 `llvm-as`/`opt`/`llc`，第 2 步的具体输出**待本地验证**；第 1 步可由已安装的 `clang` 真实完成。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `opt` 和 `llc` 可以分别独立开发、互不依赖对方的存在？
**答案**：因为它们都以 IR 为接口——`opt` 消费 IR 产出 IR，`llc` 消费 IR 产出机器码。只要 IR 这份契约不变，两边就能独立演进。这正是 IR 作为「公共接口」的价值。

**练习 2**：一门新语言想接入 LLVM 生态，最少要实现哪一段？一个新 CPU 呢？
**答案**：新语言只需写一个「该语言 → LLVM IR」的前端，不必碰任何后端；新 CPU 只需写一个「LLVM IR → 该 CPU 机器码」的后端，不必碰任何前端。这正是 \(M+N\) 公式的直接体现。

## 5. 综合实践

把本讲两个模块串起来，完成一张「三段式 + IR 边界」流程图。

要求在图上同时完成三件事：

1. **标出每个阶段的输入/输出文件后缀**：`.c` →（前端）→ `.ll`/`.bc` →（中端，可选）→ `.ll`/`.bc` →（后端）→ `.s`/`.o`。
2. **用一条虚线标出「IR 边界」**：明确写出哪些工具之间传递的是 IR（`clang -emit-llvm` 出口 → `llc` 入口之间，含 `opt`/`llvm-as`/`llvm-dis`/`lli` 的所有互转），哪些传递的不是（`clang` 之前是源码，`llc` 之后是汇编/目标码）。
3. **在图侧标注源码证据**：前端总入口在 [clang/tools/driver/driver.cpp:242](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/clang/tools/driver/driver.cpp#L242)（`clang_main`），真正的前端派发在 [clang/tools/driver/driver.cpp:228-229](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/clang/tools/driver/driver.cpp#L228-L229)（`cc1_main`），三段式的文字定义在 [README.md:19-21](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/README.md#L19-L21)。

最后，哪怕本地没有 `opt`/`llc`，也请用 `clang -S -emit-llvm add.c -o add.ll` **真实跑出**一段 `.ll`，把它作为「前端 → IR」边界的真实产物贴进图里——这是验证三段式不是纸上谈兵的最直接方式。

## 6. 本讲小结

- 三段式 = **前端（源码→IR）+ 中端（IR→优化 IR）+ 后端（IR→机器码）**，前后端通过 IR 解耦。
- IR 把 \(M\) 语言 × \(N\) 目标的复杂度从 \(M\times N\) 降为 \(M+N\)——这是 LLVM 作为「编译器**基础设施**」而非「某一语言的编译器」的根本原因。
- `README.md` 顶层一句话就写明了三段式；`driver.cpp` 显示 clang 内部还有 **Driver（编排）+ cc1（真正前端）** 两层。
- 真正产出 IR 的是 `cc1` 的 CodeGen；`clang -S -emit-llvm` 可在前端出口处「截断」，直接拿到 IR 文件。
- `opt`/`llc`/`lli` 能各自独立消费 `.bc`，正是因为 IR 是一份**稳定、可序列化的契约**。
- 下一讲 u2-l2 将进入 IR 本身：怎么读懂、怎么手写 `.ll` 文本。

## 7. 下一步学习建议

- **下一讲 u2-l2《阅读与编写 LLVM IR（.ll 文本格式）》**：既然 IR 是三段式的枢纽，下一步就亲手读懂、改写一段 `.ll`。
- **想看「前端如何一步步生成 IR」的完整小例子**：可先翻 u2-l3 的 Kaleidoscope 导览，它把一个最小语言的 AST→IR 过程讲得很清楚。
- **源码延伸阅读**（建议读完本单元再深入）：`clang/docs/DriverInternals.rst`（Driver 如何编排编译动作）、`clang/docs/InternalsManual.rst`（Clang 整体架构）。
