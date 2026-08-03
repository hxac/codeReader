# SCoP 概念与 ScopDetection 设计

> 本讲是「SCoP 检测」单元（U3）的第一篇。在 u2-l1 我们已经看过 Polly 的完整阶段流水线，知道 `detect` 是其中第二个阶段。本讲放大这一个阶段，回答两个根本问题：**什么样的代码片段有资格被 Polly 优化？** 以及 **Polly 用什么类、什么数据结构把「有资格的片段」找出来并交给后续阶段？**

## 1. 本讲目标

学完本讲，你应当能够：

1. 用一句话说清 SCoP 是什么，并复述它必须满足的数学前提（仿射、规整控制流）。
2. 解释为什么「仿射」是 Polly 能否处理一段循环的关键判据，以及 SCEV 在其中的角色。
3. 理解 LLVM 的 Region / RegionInfo 概念，看清它如何充当 SCoP 的「控制流边界」。
4. 读懂 `ScopDetection` 类的对外接口：构造函数要哪些分析、`ValidRegions` 是什么、迭代器怎么用、`isMaxRegionInScop` 做什么。
5. 说清「**检测**」与「**建模**」两个阶段的分工边界——这是理解 U3、U4 整体的钥匙。

本讲只讲**设计与概念层面**，不深入逐条合法性判定（`isValid*` 系列的细节留给 u3-l2），也不讲多面体模型如何被真正构造出来（那是 U4 的工作）。

## 2. 前置知识

本讲默认你已经从 u1-l1、u2-l1 知道：

- **Polly 是多面体循环优化器**：吃 LLVM IR、吐优化后的 LLVM IR，核心思想是用「迭代域 + 访问关系 + 调度」三件套把循环核抽象成数学对象。
- **Polly 的流水线**：`prepare → detect → scops → flatten → deps → … → codegen`。本讲聚焦 `detect` 阶段。

此外，本讲会用到几个 LLVM 的基础概念，这里先做通俗解释：

- **控制流图（CFG）**：把函数里每个基本块（Basic Block）看作一个点，跳转关系看作边，连成的图。
- **基本块（Basic Block）**：一段没有内部跳转、只能从顶部进入、从底部退出的线性指令序列。
- **SCEV（ScalarEvolution）**：LLVM 的一个分析，专门用来回答「这个标量值随循环迭代如何变化」。例如对循环变量 `i`，SCEV 能告诉你它是「从 0 开始、每次加 1」的递推，并给出一个符号表达式。Polly 几乎完全依赖 SCEV 来判断一段代码是否「规整」。
- **支配（Dominate）**：在 CFG 里，若所有到达块 B 的路径都必经块 A，则称 A 支配 B。支配树（DominatorTree）是 LLVM 区域分析的基础。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用到哪部分 |
|------|------|----------------|
| [include/polly/ScopDetection.h](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/include/polly/ScopDetection.h) | `ScopDetection` 类的声明、SCoP 的定义注释、`ValidRegions`、`DetectionContext`、对外接口 | 几乎全文 |
| [lib/Analysis/ScopDetection.cpp](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Analysis/ScopDetection.cpp) | `ScopDetection` 的实现：构造、`detect()`、`findScops()`、`isMaxRegionInScop()`、`isValidRegion()` 等 | 构造与几个对外方法 |
| [lib/Pass/PhaseManager.cpp](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Pass/PhaseManager.cpp) | u2-l1 讲过的阶段流水线总调度，此处展示它如何调用 `detect` | 检测阶段调用点 |
| [test/ScopDetect/non-affine-loop.ll](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/test/ScopDetect/non-affine-loop.ll) | 一个被检测拒绝的「非仿射循环」回归测试 | 实践环节 |

## 4. 核心概念与源码讲解

### 4.1 SCoP 的定义与「仿射」要求

#### 4.1.1 概念说明

**SCoP** 全称 **Static Control Part**（静态控制部分），是 Polly 的核心术语。它指的是函数控制流图（CFG）的一个子图，这个子图里的控制流**在编译时就能完全确定**，因此可以用多面体模型精确描述。

直观地说，一个 SCoP 就是一段「**只用 `for` 和 `if` 写出来、没有 `goto`/`break`/`continue`、循环边界和数组下标都是线性表达式**」的代码片段。源码文件开头的注释给出了精确定义：

> A static control part (Scop) is a subgraph of the control flow graph (CFG) that only has statically known control flow and can therefore be described within the polyhedral model.

为什么 Polly 必须挑出这样的片段？因为多面体模型用**线性不等式组**来刻画循环的迭代空间和访问关系。线性不等式组定义的空间是一个**凸多面体（整数多面体）**，这正是 ISL（Integer Set Library）能处理的对象。只要控制流或下标里混进「非线性」成分（比如循环边界依赖某个数组元素 `i < A[i]`），就再也写不成线性不等式组，多面体模型立刻失效。

这就引出本讲最重要的判据——**仿射（affine）**。一个表达式是仿射的，当且仅当它能写成循环迭代变量与参数的**线性组合加常数**：

\[
f(\vec{i},\vec{p}) \;=\; c_0 + \sum_{j=1}^{m} c_j\, i_j + \sum_{k=1}^{n} d_k\, p_k
\]

其中 \(i_j\) 是各层循环的迭代变量，\(p_k\) 是**参数**（在 SCoP 执行期间不变的标量，例如数组长度 `N`、循环上界参数），\(c_j, d_k, c_0\) 都是整常数。`2*i + j`、`5*i - 3`、`N` 都仿射；`i*j`、`A[i]`、`i*i` 都**不**仿射。

#### 4.1.2 核心流程：SCoP 必须满足的限制

源码注释把 SCoP 的前提归纳为四条限制（对应文件开头 `Every Scop fulfills these restrictions` 注释）：

1. **单入口单出口区域**：整个 SCoP 只有一个进入块、一个退出块（这是 Region 的要求，见 4.2）。
2. **循环边界仿射**：每个自然循环的迭代次数必须是循环迭代变量或参数的仿射函数。
3. **分支条件仿射**：`if` / 循环退出条件里的比较，两边都必须是仿射表达式。
4. **循环与条件完美嵌套**：控制流必须能仅用 `for`、`if` 表达，不允许 `goto`/`break`/`continue`。
5. **无副作用的函数调用**：只允许 `readnone`（不读写内存）的调用，外加 `memset`/`memcpy`/`memmove` 这类内存内联函数。

把这五条翻译成「能不能进 SCoP」的判定流程：

```
对一个候选区域 R：
  ├─ R 是不是单入口单出口的 Region？        否 → 拒绝
  ├─ R 内每个循环的边界是不是仿射？          否 → 拒绝（或按开关装箱为非仿射子区域）
  ├─ R 内每个分支/退出条件是不是仿射比较？    否 → 拒绝（同上）
  ├─ R 内每条指令是不是 Polly 能处理的？     否 → 拒绝
  └─ R 内所有数组访问是不是仿射下标？        否 → 拒绝
全部通过 → R 是一个合法 SCoP
```

注意第 2、3 条里有一个「逃生阀」：当开启 `-polly-allow-nonaffine-branches` / `-polly-allow-nonaffine-loops` 时，Polly 可以把非仿射的小块**装箱（box）**成一个「过近似子区域」，对其内部不再精确建模。这部分细节属于 u3-l2 的判定算法，本讲只需知道有这么一个机制。

#### 4.1.3 源码精读

SCoP 的定义写在头文件开头的注释里，这是 Polly 文档级的最权威定义：

[include/polly/ScopDetection.h:9-43](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/include/polly/ScopDetection.h#L9-L43) — 这段注释逐条列出 SCoP 的限制，特别强调了「循环迭代次数必须是仿射线性函数」与「分支条件必须是仿射线性表达式比较」。读源码时把它当作一份规格说明。

「仿射」判定的真正入口在 `isValidBranch` 里：拿到 `if`/循环条件的两个操作数后，用 SCEV 求出它们的符号表达式，再调用 `isAffine` 检查：

```cpp
// lib/Analysis/ScopDetection.cpp:619-641（节选）
Loop *L = LI.getLoopFor(&BB);
const SCEV *LHS = SE.getSCEVAtScope(ICmp->getOperand(0), L);
const SCEV *RHS = SE.getSCEVAtScope(ICmp->getOperand(1), L);
...
if (isAffine(LHS, L, Context) && isAffine(RHS, L, Context))
  return true;
```

[lib/Analysis/ScopDetection.cpp:619-641](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Analysis/ScopDetection.cpp#L619-L641) — `SE.getSCEVAtScope` 让 LLVM 把操作数翻译成 SCEV 表达式，`isAffine` 再判定它能否写成迭代变量的线性组合。这就是「SCEV 之于仿射判定」的关系：**SCEV 负责把 IR 里的值变成符号表达式，Polly 负责判断这个表达式是否线性**。

> 提示：检测阶段只判断「能不能仿射」，并不真正把表达式翻译成 ISL 对象——后者是 U4 的 `SCEVAffinator`（见 u4-l4）。这里出现了本讲的另一个分工线索：判定（detection）轻量、建模（modeling）重，见 4.5。

#### 4.1.4 代码实践

**实践目标**：用一个真实回归测试体会「非仿射导致 SCoP 被拒」。

**操作步骤**：

1. 阅读 [test/ScopDetect/non-affine-loop.ll](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/test/ScopDetect/non-affine-loop.ll)。它对应的 C 代码（注释里给出）是：

   ```c
   void f(int *A) {
     for (int i = 0; i < A[i]; i++)  // 循环上界依赖 A[i] —— 数据相关，非仿射
       A[-1]++;
   }
   ```

   注意循环条件 `i < A[i]`：上界 `A[i]` 随 `i` 变化且来自内存，写不成仿射函数。

2. 看该文件的 RUN 行，它用四组不同开关跑同一份 IR，并用 `FileCheck` 验证 `Valid Region` 是否出现。

**需要观察的现象**：

- `REJECTNONAFFINELOOPS` / `ALLOWNONAFFINELOOPS` / `ALLOWNONAFFINEREGIONSANDACCESSES` 三个前缀都带 `-NOT: Valid`，说明默认情况下（或只放宽部分条件时）这段代码**不被**检测为 SCoP。
- 只有 `ALLOWNONAFFINELOOPSANDACCESSES`（同时放宽非仿射循环、非仿射分支、非仿射访问）才出现 `Valid Region`。
- `PROFIT` 前缀即便放宽了仿射要求，仍带 `-NOT: Valid`，因为它额外关掉了 `-polly-process-unprofitable`，被「可获利性」过滤掉了（见 4.4.1）。

**预期结果**：你会直观看到「非仿射循环边界」是 SCoP 检测的硬障碍，必须靠多个「放宽」开关一起放行才能勉强接受，而即便接受也可能因不划算被剔除。本实践为「源码阅读型」实践，运行命令的具体输出**待本地验证**（需要先编译出带 Polly 的 opt）。

#### 4.1.5 小练习与答案

**练习 1**：下列哪些循环边界是仿射的？（设 `N`、`M` 是参数，`i`、`j` 是循环变量）
(a) `i < N`  (b) `i < N*M`  (c) `i*i < N`  (d) `i < A[i]`  (e) `i + j < N`

> **答案**：(a) 仿射（\(i < N\) 即 \(1\cdot i + 1\cdot N\)，系数为常数）。(b) 仿射——`N*M` 是两个**参数**之积，但参数本身在 SCoP 内不变，`N*M` 视为单一参数，故仍是线性。(c) 不仿射（\(i^2\) 非线性）。(d) 不仿射（数据相关）。(e) 仿射（\(i+j\) 是迭代变量的线性组合）。

**练习 2**：为什么「`for (int i = 0; i < n; i++)` 的边界 `i < n` 是仿射」，而「`for (int i = 0; i < A[i]; i++)` 的边界不是」？用本讲的「参数」概念解释。

> **答案**：`n` 在循环内不被修改，是**参数**，所以 `i < n` 可写成迭代变量 \(i\) 与参数 \(n\) 的线性式，仿射。而 `A[i]` 的值随 `i` 变化，既不是迭代变量本身也不是不变参数，无法写成线性组合，故非仿射。

---

### 4.2 Region 与 RegionInfo：SCoP 的控制流边界

#### 4.2.1 概念说明

SCoP 是 CFG 的一个子图，但不是随便一个子图——它必须是 **Region**。在 LLVM 里：

> **Region** 是 CFG 中满足「**单入口单出口（Single Entry Single Exit, SESE）**」的最大子图。整张 CFG 有且只有一个入口块和一个出口块的区域称为**顶层区域（top-level region）**，它覆盖整个函数。

「单入口」意味着从外面进入这个子图只能走一个块；「单出口」意味着离开只能从一个块出去。这样的子图内部可以任意复杂，但对外只有两个接口点，非常适合作为一个独立单元被分析、被变换、被替换。

RegionInfo 分析会把整张 CFG 划分成一棵**区域树（Region Tree）**：顶层区域包含若干子区域，子区域再包含子子区域，层层嵌套。这棵树是 Polly 检测算法的骨架——Polly 沿着它自顶向下搜索合法 SCoP。

一个关键点：**SCoP 必须是 Region，但不是所有 Region 都是 SCoP**。Region 只保证「单入口单出口」这一个几何性质；SCoP 在此之上还要求仿射、规整控制流（4.1）。所以检测的本质是：**在 Region 树上挑选那些额外满足数学前提的 Region**。

#### 4.2.2 核心流程：从函数到区域树

```
Function F
   │  RegionInfoAnalysis
   ▼
顶层 Region（覆盖整个 F）
   │  递归划分
   ├── 子 Region A（某个 for 的循环体）
   │     └── 子子 Region A1（循环内的 if 块）
   └── 子 Region B（另一个循环体）
```

Polly 拿到 `RegionInfo` 后，从顶层区域出发，调用 `getTopLevelRegion()` 拿到根，再递归遍历它的子区域（每个子区域都能用 `for (auto &SubRegion : R)` 这种 Region 的迭代器枚举）。

#### 4.2.3 源码精读

`ScopDetection` 把 `RegionInfo` 作为成员之一保存（见 4.3.3）。检测入口 `detect()` 第一步就是取出顶层区域，再交给 `findScops`：

[lib/Analysis/ScopDetection.cpp:341-359](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Analysis/ScopDetection.cpp#L341-L359) — 注意 `Region *TopRegion = RI.getTopLevelRegion();` 这一行：整个检测都建立在这棵区域树上。`findScops(*TopRegion)` 递归遍历它。

值得对比的是 PhaseManager 里的用法。u2-l1 讲过，Polly 流水线里 RegionInfo 不能用 Pass Manager 缓存的版本，必须每次重新算：

[lib/Pass/PhaseManager.cpp:100-106](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Pass/PhaseManager.cpp#L100-L106) — 注释明说「ScopDetection is modifying RegionInfo, do not cache it」（检测会改写区域树，因为 `expandRegion` 会往里加非规范子区域）。于是 PhaseManager 自己 `RegionInfo RI = RegionInfoAnalysis().run(F, FAM);` 新算一份，再 `ScopDetection SD(DT, SE, LI, RI, AA, ORE); SD.detect(F);`。这条调用链正是 u2-l1 流水线里 `detect` 阶段的落地。

#### 4.2.4 代码实践

**实践目标**：感受「Region 是 SCoP 的容器」。

**操作步骤**：

1. 在 `detect()` 里（[lib/Analysis/ScopDetection.cpp:341](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Analysis/ScopDetection.cpp#L341)）确认：检测的起点永远是顶层 Region，而**不是**从某条指令或某个循环开始。这说明 Polly 把「区域」而非「循环」作为优化单元。
2. 跟踪 `findScops` 里 `for (auto &SubRegion : R) findScops(*SubRegion);`（见 4.4.3）这一行——它就是在沿区域树下降。

**需要观察的现象**：SCoP 的边界与 Region 的边界一致；一个 SCoP 总是「某个 Region」。

**预期结果**：能口述「Polly 先用 RegionInfo 把函数切成区域树，再用 ScopDetection 在树上挑合法 SCoP」。

#### 4.2.5 小练习与答案

**练习 1**：「单入口单出口」的「出口」指的是哪个基本块？

> **答案**：指 Region 的 **exit 块**——它是 Region **之外**、但被 Region 内部跳转指向的第一个块。注意它不属于 Region 内部，只是边界。`detect()` 里就有一处对出口块的检查：若出口块以 `unreachable` 结尾则拒绝（见 `isValidRegion` 中 `CurRegion.getExit()` 的判断）。

**练习 2**：为什么顶层 Region 默认**不**被当作 SCoP？

> **答案**：顶层 Region 覆盖整个函数，其入口就是函数入口块；而代码生成需要在函数入口插入 `alloca`（见 `isValidRegion` 里 `ReportEntry` 的拒绝理由），所以默认拒绝把整个函数当 SCoP。要打开需加 `-polly-detect-full-functions`（对应 `PollyAllowFullFunction` 开关）。

---

### 4.3 ScopDetection 类：构造、ValidRegions 与迭代接口

#### 4.3.1 概念说明

`ScopDetection` 是检测阶段的**总指挥类**。它的职责很纯粹：

- 输入：一个函数 + 一组 LLVM 分析。
- 输出：一份「**合法 SCoP 区域的集合**」`ValidRegions`，外加每个被拒区域的原因（`RejectLog`，留给 u3-l3 诊断讲）。

它**不**构造多面体模型、**不**改写 IR、**不**做任何变换。它是只读的「判定器」。这一点决定了它的接口形态：对外暴露的主要是「查询这些区域是否合法」和「枚举合法区域」。

`ScopDetection` 同时也是 LLVM 的一个**函数分析（Function Analysis）**，名叫 `ScopAnalysis`，可以挂在 New Pass Manager 上被其他 pass 取用。

#### 4.3.2 核心流程：构造 → 检测 → 迭代

```
                  ┌─────────────────────────────────┐
   6 个分析引用 ──▶│  ScopDetection(DT,SE,LI,RI,AA,ORE) │  构造，只存引用
                  └────────────────┬────────────────┘
                                   │  detect(F)
                                   ▼
                  ┌─────────────────────────────────┐
                  │  遍历 Region 树，判定每个区域      │
                  │  合法 → 插入 ValidRegions          │
                  └────────────────┬────────────────┘
                                   │
              ┌────────────────────┴────────────────────┐
              ▼                                          ▼
   for (const Region *R : SD)          isMaxRegionInScop(R)
   逐个枚举合法区域                    查询 R 是否为「最大」SCoP 区域
```

`ValidRegions` 的类型是 `RegionSet = SetVector<const Region *>`——一组**不重复**的 `Region*`。检测完成后，外部代码用 `ScopDetection` 的 `begin()/end()` 迭代器逐个取出这些区域。

#### 4.3.3 源码精读

**(1) 构造函数：6 个分析引用**

[include/polly/ScopDetection.h:514-515](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/include/polly/ScopDetection.h#L514-L515) 声明构造函数：

```cpp
ScopDetection(const DominatorTree &DT, ScalarEvolution &SE, LoopInfo &LI,
              RegionInfo &RI, AAResults &AA, OptimizationRemarkEmitter &ORE);
```

实现只是把这 6 个分析存为成员引用：

[lib/Analysis/ScopDetection.cpp:336-339](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Analysis/ScopDetection.cpp#L336-L339) — `: DT(DT), SE(SE), LI(LI), RI(RI), AA(AA), ORE(ORE) {}`。

这 6 个分析各自的用途：

| 分析 | 缩写 | 在检测里的作用 |
|------|------|----------------|
| `DominatorTree` | DT | 判断支配关系，支撑 Region 概念、错误块识别 |
| `ScalarEvolution` | SE | 把循环变量、下标翻译成符号表达式，判定仿射性（4.1） |
| `LoopInfo` | LI | 识别自然循环、循环头/锁存块、循环嵌套 |
| `RegionInfo` | RI | 提供区域树，SCoP 的几何边界（4.2） |
| `AAResults` | AA | 别名分析，判断两个指针是否可能指向同一内存 |
| `OptimizationRemarkEmitter` | ORE | 发出优化诊断 remark（「为什么没优化这块」） |

成员声明在 [include/polly/ScopDetection.h:207-211](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/include/polly/ScopDetection.h#L207-L211)（`ORE` 单独放在类底部 [L617](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/include/polly/ScopDetection.h#L617)）。

> 重要约束（来自 PhaseManager 的注释 [lib/Pass/PhaseManager.cpp:67-73](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Pass/PhaseManager.cpp#L67-L73)）：这些分析引用必须在整个逐-SCoP 处理过程中保持有效，**不能被 Pass Manager 失效重算**，因为 `ScopDetection` 存的是旧引用。这正是 u2-l1 强调「手动保持 LI/DT 有效」的根因。

**(2) `ValidRegions` 与迭代器**

[include/polly/ScopDetection.h:134-137](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/include/polly/ScopDetection.h#L134-L137) 定义了核心数据结构：

```cpp
using RegionSet = SetVector<const Region *>;
// Remember the valid regions
RegionSet ValidRegions;
```

`SetVector` 保证区域唯一且按插入顺序可遍历。迭代器就是直接代理到这个集合：

[include/polly/ScopDetection.h:555-563](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/include/polly/ScopDetection.h#L555-L563) — `begin()/end()` 让你能写 `for (const Region *R : SD)`。PhaseManager 正是这样把所有合法区域塞进工作列表的（[lib/Pass/PhaseManager.cpp:160-161](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Pass/PhaseManager.cpp#L160-L161)）。

**(3) `isMaxRegionInScop`：查询「最大」区域**

[include/polly/ScopDetection.h:527-535](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/include/polly/ScopDetection.h#L527-L535) 声明：

```cpp
bool isMaxRegionInScop(const Region &R, bool Verify = true);
```

实现在 [lib/Analysis/ScopDetection.cpp:415-434](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Analysis/ScopDetection.cpp#L415-L434)。它做两件事：

1. 先查 `R` 是否在 `ValidRegions` 里（`ValidRegions.count(&R)`）——不在就直接返回 `false`。
2. 若 `Verify=true`，则**重新构造一个 `DetectionContext` 并重跑一遍 `isValidRegion`** 来确认它在变换后仍然合法（因为别的 SCoP 代码生成后可能改写了 CFG）。

`ValidRegions` 与 `isMaxRegionInScop` 的关系是本讲实践题的核心，归纳如下：

| 方面 | `ValidRegions` | `isMaxRegionInScop(R)` |
|------|----------------|------------------------|
| 是什么 | 检测产出的**所有**合法区域集合 | 对**单个**区域 R 的查询 |
| 是否最大 | 集合里的元素**都是最大区域**（检测算法已保证，见 4.4） | 名字里的「Max」指 R 是不是某个 SCoP 的最大外框，而非「比别的大」 |
| 是否可重检 | 静态结果 | 可选 `Verify=true` 当场重判 |
| 典型用法 | 枚举全部 SCoP | PhaseManager 在工作列表里逐个确认（[L175](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Pass/PhaseManager.cpp#L175) 用 `Verify=false`） |

简单说：**`ValidRegions` 是检测的「成品清单」，`isMaxRegionInScop` 是事后核对清单上某项是否仍有效（或确认某区域是否在清单上）的单点查询**。「最大」的含义是——同一片代码不会被拆成两个互相重叠的 SCoP，而是合并成能合并的最大那一块（详见 4.4）。

**(4) 作为 LLVM 分析：`ScopAnalysis`**

[include/polly/ScopDetection.h:620-628](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/include/polly/ScopDetection.h#L620-L628) 把 `ScopDetection` 包装成标准 NPM 分析 `ScopAnalysis`，其 `run` 函数向 FAM 索要这 6 个分析、构造 `ScopDetection` 并调用 `detect`：

[lib/Analysis/ScopDetection.cpp:2011-2022](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Analysis/ScopDetection.cpp#L2011-L2022) — 这正是「6 个分析 → ScopDetection → detect」这一流程在分析层的形式化。注意 PhaseManager 实际上**没有**走 `ScopAnalysis` 这条分析路径，而是直接手工 new 了一个 `ScopDetection`（见 4.2.3），原因就是 RegionInfo 不能用缓存版本。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：完成规格中要求的两件事——列出构造所需分析、解释 `ValidRegions` 与 `isMaxRegionInScop` 的关系。

**操作步骤**：

1. 打开 [include/polly/ScopDetection.h](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/include/polly/ScopDetection.h)，定位构造函数声明（L514-515），写出 6 个参数：`DominatorTree &DT`、`ScalarEvolution &SE`、`LoopInfo &LI`、`RegionInfo &RI`、`AAResults &AA`、`OptimizationRemarkEmitter &ORE`。
2. 核对私有成员（L207-211 + L617），确认 `ORE` 也是成员引用。
3. 阅读 `isMaxRegionInScop`（L527-535 声明、L415-434 实现），注意它先 `ValidRegions.count(&R)`、再按 `Verify` 决定是否重跑 `isValidRegion`。
4. 在 PhaseManager 里找到它的使用点 [lib/Pass/PhaseManager.cpp:175](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Pass/PhaseManager.cpp#L175)，注意这里传的是 `Verify=false`——因为 `ValidRegions` 才刚算出来，没必要重判。

**需要观察的现象**：

- 6 个分析引用一次性在构造时绑定，之后 `ScopDetection` 内部所有判定都靠它们。
- `ValidRegions` 是检测的输出（集合），`isMaxRegionInScop` 是查询接口（单点）；二者都围绕「Region 是否合法」展开，但一个是结果枚举，一个是有效性核对。

**预期结果**：你能用自己的话回答：「`ValidRegions` 装着检测认定的全部最大合法 SCoP 区域；`isMaxRegionInScop(R)` 用来确认某个 R 是不是这批最大区域之一（且可选地当场重判以应对 CFG 已被改动的情况）。」两者关系——**集合本身保证最大性，函数负责单点查询与可选复核**。

> 本实践为源码阅读型，无需运行，结论可直接从源码得出。

#### 4.3.5 小练习与答案

**练习 1**：`isMaxRegionInScop` 的 `Verify` 参数默认是 `true`，但 PhaseManager 调用时传 `false`。为什么不一致？

> **答案**：默认 `true` 是给「检测之后又经过若干变换、CFG 可能已变」的场景留的安全网，会重跑一遍 `isValidRegion` 复核。PhaseManager 在 `detect` 刚结束、尚未改 IR 时立刻用，结果可信，传 `false` 省掉重判的开销。源码注释（L530-532）还警告：若该区域的 `DetectionContext` 仍被某个待处理的 `ScopInfo` 引用，就**不能**用 `Verify=true`。

**练习 2**：为什么 `ValidRegions` 用 `SetVector` 而非 `std::set` 或 `vector`？

> **答案**：`SetVector` = 「去重」+「保插入顺序」。`std::set` 会按指针排序、丢失检测顺序信息；纯 `vector` 不去重，`expandRegion` 等流程可能把同一区域插多次。`SetVector` 兼顾唯一性与稳定遍历顺序，对回归测试的可重复性很重要。

---

### 4.4 detect 与 findScops：寻找「最大」SCoP 的算法

#### 4.4.1 概念说明

检测不是「逐条指令判断」，而是「**在区域树上挑出最大的合法区域**」。「最大」是关键：如果一片代码合法，Polly 宁可把它整个当成一个 SCoP，也不愿切成若干小 SCoP——大 SCoP 给后续变换（分块、并行）更多腾挪空间。

但区域树（RegionInfo 产物）只含**规范区域（canonical regions）**：在每个层级上是「最大」的 SESE 子图。而真正最大的合法 SCoP 有时是一个**非规范区域**——由若干相邻规范区域拼成。因此检测算法分两步：

1. **自顶向下递归**：先试顶层区域；不合法就降到它的子区域再试；都不行再降到子子区域。
2. **向上扩展**：找到一个合法的规范子区域后，尝试把它**扩张**成更大的（可能非规范的）区域，只要扩张后仍合法就保留更大的。

此外还有一道**可获利性（profitability）**筛子：即便区域合法，若它太「轻」（如只有一两个简单循环、计算量小），Polly 也可能跳过——优化它带来的收益抵不过编译时间开销。这由 `-polly-process-unprofitable` 开关控制。

#### 4.4.2 核心流程

```
detect(F):
  TopRegion = RI.getTopLevelRegion()
  findScops(TopRegion)          # 递归 + 扩展，填充 ValidRegions
  对每个 ValidRegion 做可获利性过滤 → 不划算的从 ValidRegions 移除

findScops(R):
  if R 合法 (isValidRegion):
      ValidRegions.insert(R); 返回        # 找到一个，停下
  否则:
      for 子区域 Sub in R: findScops(Sub)  # 下降一层
      for 每个仍合法的子区域: expandRegion()  # 尝试向上扩张成非规范大区域
```

#### 4.4.3 源码精读

**(1) `detect` 的骨架**

[lib/Analysis/ScopDetection.cpp:341-392](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Analysis/ScopDetection.cpp#L341-L392)。几个要点：

- L342 `assert(ValidRegions.empty())`：检测只能跑一次。
- L347 取顶层区域，L359 调 `findScops`。
- L363-378 **可获利性过滤**：遍历所有缓存的 `DetectionContext`，对仍在 `ValidRegions` 里的区域调 `isProfitableRegion`，不划算就从 `ValidRegions.remove(&DC.CurRegion)`。这就是 4.1.4 里 `PROFIT` 前缀被剔除的原因。
- L384-385 若开启 `-polly-detect-track-failures`，调 `emitMissedRemarks` 报告被拒原因（u3-l3 诊断）。

**(2) `findScops` 的递归与扩展**

[lib/Analysis/ScopDetection.cpp:1584-1648](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Analysis/ScopDetection.cpp#L1584-L1648) 是算法的心脏，逐段读：

```cpp
// L1591-1611：先试当前区域 R
if (!PollyProcessUnprofitable && regionWithoutLoops(R, LI))
  invalid<ReportUnprofitable>(Context, /*Assert=*/true, &R);
else
  DidBailout = !isValidRegion(Context);

if (Context.IsInvalid) {
  removeCachedResults(R);
} else {
  ValidRegions.insert(&R);   // R 合法 → 收下并返回（不降级）
  return;
}

// L1613-1614：R 不合法 → 下降到子区域
for (auto &SubRegion : R)
  findScops(*SubRegion);

// L1622-1647：再尝试把找到的合法子区域向上扩张成非规范大区域
for (Region *CurrentRegion : ToExpand) {
  ...
  Region *ExpandedR = expandRegion(*CurrentRegion);
  if (!ExpandedR) continue;
  R.addSubRegion(ExpandedR, true);          # 改写区域树（故 RI 不可缓存）
  ValidRegions.insert(ExpandedR);
  removeCachedResults(*CurrentRegion);      # 用大区域替换原子区域
}
```

注意 `R.addSubRegion(ExpandedR, true)`——这一行**修改了区域树**，正是 4.2.3 里「RegionInfo 不能用缓存版本」的根因。

**(3) `expandRegion` 与 `isValidRegion` 的角色**

[lib/Analysis/ScopDetection.cpp:1504-1561](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Analysis/ScopDetection.cpp#L1504-L1561) — `expandRegion` 反复调 `R.getExpandedRegion()` 拿到更大的候选，对每个候选建 `DetectionContext` 并跑 `allBlocksValid`，只要不报错就保留「目前最大的合法区域」并继续扩张，直到扩张失败。

[lib/Analysis/ScopDetection.cpp:1773-1831](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Analysis/ScopDetection.cpp#L1773-L1831) — `isValidRegion` 是单区域合法性总入口：先挡掉「顶层区域」「出口 unreachable」「入口是函数入口块」等结构性问题，再调 `allBlocksValid` 逐块判定（内部含 `isValidLoop`/`isValidCFG`/`isValidInstruction`/`hasAffineMemoryAccesses`，这些细节属 u3-l2），最后用 `isReducibleRegion` 排除不可规约控制流。本讲只需把它理解成「对一个区域做全套合法性检查的函数」。

#### 4.4.4 代码实践

**实践目标**：看清「最大区域」是如何被搜索出来的。

**操作步骤**：

1. 在 [lib/Analysis/ScopDetection.cpp:1584](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Analysis/ScopDetection.cpp#L1584) 的 `findScops` 中，标注三段：试当前区域（L1591-1611）、下降子区域（L1613-1614）、向上扩展（L1622-1647）。
2. 用 POLLY_DEBUG 调试：源码里大量 `POLLY_DEBUG(dbgs() << ...)`（如 [L1509](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Analysis/ScopDetection.cpp#L1509) 处的 `Expanding ...`）。`DEBUG_TYPE` 在 [L91](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Analysis/ScopDetection.cpp#L91) 定义为 `"polly-detect"`，故可用 `-debug-only=polly-detect` 观察扩张过程。

**需要观察的现象**：调试输出会打印每个被尝试的区域名（如 `Checking region: ...`、`Expanding ...`、`to ...`），展示「先整体、不行再分、找到再扩」的全过程。

**预期结果**：能画出对一个含嵌套循环函数的检测流程示意。具体调试输出**待本地验证**（需带 Assertions 与 Debug 的 opt 构建）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `findScops` 找到一个合法区域就 `return`，不再继续看它的子区域？

> **答案**：因为要「最大」。当前区域 R 合法，意味着它整体能当 SCoP，子区域只是 R 的一部分——若也收下会和 R 重合，造成重复优化单元。所以一旦 R 合法就停下；只有 R 不合法时才下降，在更小的子区域里找「次大」的合法块。

**练习 2**：`detect()` 末尾的 `isProfitableRegion` 过滤（L363-378）与 `findScops` 里的 `ReportUnprofitable`（L1592）都在处理「不划算」，二者有何不同？

> **答案**：`findScops` 里那处针对「区域里完全没有循环」的极端情况（`regionWithoutLoops`），在 `-polly-process-unprofitable=false` 时直接判负；`detect()` 末尾那处是更细粒度的全局过滤，对已入选 `ValidRegions` 的区域再用「循环数≥2 / 可分布 / 计算量足够」等启发式（`isProfitableRegion`，L1737-1771）二次筛除。两者都受同一开关 `PollyProcessUnprofitable` 控制。

---

### 4.5 「检测」与「建模」两阶段的分工

#### 4.5.1 概念说明

初学者常以为「Polly 检测出一个 SCoP，就等于建好了多面体模型」。**不是**。Polly 把这两件事严格分成两个阶段、由两个不同的类负责：

| 阶段 | 主类 | 产物 | 改 IR？ | 开销 |
|------|------|------|---------|------|
| **检测（Detection）** | `ScopDetection` | `ValidRegions`（合法区域集合）+ 拒绝原因 | 否 | 较低 |
| **建模（ScopInfo / ScopBuilder）** | `ScopInfo`、`ScopBuilder`（U4） | 完整的 `Scop` 多面体对象（域/调度/访问） | 否（仅构造内存对象） | 较高 |

检测只回答「**能不能**」——这片区域满足 SCoP 的数学前提吗？它**不**真正构造 ISL 集合、**不**计算访问关系。建模才回答「**具体是什么**」——把每条语句的迭代域、调度、读写访问翻译成 ISL 对象，供后续变换使用。

为什么要拆开？因为检测要尽量便宜：它要在整棵区域树上反复试探、扩张、回退，若每次都完整建模会极其昂贵。所以检测用「够用即可」的轻量判定（仿射吗？控制流规整吗？），把真正昂贵的多面体构造推迟到已经确定要优化的少数区域上。

#### 4.5.2 核心流程：检测如何为建模铺路

```
detect (ScopDetection)            scops (ScopInfo)
   │ 产出 ValidRegions               │ 取出每个 ValidRegion
   ▼                                  ▼
[Region₁, Region₂, ...]   ──────▶   ScopBuilder(R) 逐指令建模
                                       │
                                       ▼
                                    Scop 对象（域/调度/访问）→ 喂给后续变换
```

#### 4.5.3 源码精读

在 PhaseManager 里，这两个阶段是相邻的、用两个不同的类：

- **检测**：[lib/Pass/PhaseManager.cpp:105-106](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Pass/PhaseManager.cpp#L105-L106) `ScopDetection SD(...); SD.detect(F);`
- **建模**：[lib/Pass/PhaseManager.cpp:138](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Pass/PhaseManager.cpp#L138) `ScopInfo Info(DL, SD, SE, LI, AA, DT, AC, ORE);` —— 注意它**接收 `SD` 作为参数**，正是「检测为建模提供合法区域清单」的体现。

`ScopInfo` 随后在 [L164-166](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Pass/PhaseManager.cpp#L164-L166) 对每个区域调 `Info.getScop(R)` 真正构造 `Scop`。`getScop` 内部会再次调用 `SD.isMaxRegionInScop`（[L175](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Pass/PhaseManager.cpp#L175)）确认——这正呼应 4.3.3：检测的成果（`ValidRegions`/`isMaxRegionInScop`）是建模阶段的入口凭证。

注释 [L131-133](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Pass/PhaseManager.cpp#L131-L133) 写道「Can't do anything after this without ScopInfo」——再次印证：检测只是门槛，真正的多面体对象在 `ScopInfo` 阶段才诞生。

#### 4.5.4 代码实践

**实践目标**：在源码里划清两阶段边界。

**操作步骤**：

1. 在 PhaseManager.cpp 里用两个断点（或两处 `POLLY_DEBUG`）分别标记 L105（检测）与 L138（建模）。
2. 对照本讲的表格，把检测阶段（`ScopDetection`）只做判定、不改 IR，建模阶段（`ScopInfo`）才造 `Scop` 对象这两件事写下来。

**需要观察的现象**：检测与建模是**两次独立的、可分别开关的阶段**（`PassPhase::Detection` 与 `PassPhase::ScopInfo`，见 u2-l1）。你甚至可以用 `-passes='polly-custom<detect>'` 只跑到检测、不建模来观察 `ValidRegions`。

**预期结果**：能说清「检测产出区域清单，建模消费清单造多面体对象」，并指出二者在 PhaseManager 里的代码分界。源码阅读型实践，结论可直接得出。

#### 4.5.5 小练习与答案

**练习 1**：如果只对「检测」感兴趣（想看哪些区域合法），需要开建模阶段吗？

> **答案**：不需要。检测阶段独立产出 `ValidRegions`，可用 `-passes='polly-custom<detect>' -polly-print-detect` 只跑检测并打印结果。这也说明两阶段解耦良好——这正是 PhaseManager 把它们设计成两个独立 `PassPhase` 的好处。

**练习 2**：检测阶段已经判过仿射了，为什么建模阶段（U4）还要 `SCEVAffinator`/`SCEVValidator` 再处理一次 SCEV？

> **答案**：检测只判「能不能仿射」（一个 bool），并不把表达式真正翻译成 ISL 对象；建模才需要把每个下标、每个边界**具体地**翻译成 ISL 仿射表达式（`SCEVAffinator`）并施加更严格的可接受性校验（`SCEVValidator`）。前者是粗筛，后者是精译，二者粒度不同。详见 u4-l4。

## 5. 综合实践

把本讲的知识串起来，完成下面这个端到端阅读任务：

**任务**：用一段含两个嵌套循环的矩阵乘 C 代码，追踪它从「函数」到「合法 SCoP 区域」的完整检测路径。

**建议步骤**：

1. 写一个三重循环的矩阵乘函数（参考 u1-l3 给出的形式），用 u1-l3 学过的方法生成 IR（`-polly-dump-before-file`）。
2. 在脑中（或用纸）画出该函数的 CFG 骨架，标出顶层 Region 与循环体对应的子 Region。
3. 对照 4.4 的 `findScops` 算法，推演：顶层区域会被接受还是下降？为什么矩阵乘通常能作为一个较大的 SCoP 被收下？
4. 列出 `ScopDetection` 构造所需的 6 个分析（4.3.3 表格），并说明每个在检测矩阵乘时大概起什么作用（例如 SE 用于判定 `i < N`、`j < M` 是否仿射）。
5. 写下 `ValidRegions` 与 `isMaxRegionInScop` 在这条路径上各自的「出场时刻」：`ValidRegions` 在 `detect` 结束时被填充；`isMaxRegionInScop` 在 PhaseManager 取出每个区域准备建模时被调用（`Verify=false`）。
6.（可选，需本地构建）用 `-passes='polly-custom<detect>' -polly-print-detect` 验证你预测的合法区域，与推演结果对照；若不一致，用 `-debug-only=polly-detect` 看 `Checking region` / `Expanding` 日志定位差异。

**交付物**：一张「函数 → 区域树 → 检测判定 → ValidRegions → 建模入口」的流程图，并附 6 个分析的用途说明。运行结果**待本地验证**。

## 6. 本讲小结

- **SCoP** 是 CFG 中控制流在编译时完全确定、可用多面体模型描述的子图；它必须是**单入口单出口的 Region**，且循环边界、分支条件、数组下标都要**仿射**。
- **仿射** = 迭代变量与参数的线性组合加常数；这是多面体模型用线性不等式组刻画迭代空间的数学前提，SCEV 负责把 IR 值变成可判定的符号表达式。
- **Region / RegionInfo** 提供 SCoP 的几何边界；Polly 沿区域树自顶向下搜索，且检测会改写区域树，故 RegionInfo 不能用 Pass Manager 的缓存版本。
- `ScopDetection` 构造时绑定 **6 个分析**（DT/SE/LI/RI/AA/ORE），`detect()` 把合法区域填入 **`ValidRegions`**，对外用迭代器和 **`isMaxRegionInScop`** 暴露结果。
- 检测算法追求**最大合法区域**：先整体试、不合法再下降、找到后再向上扩展成非规范大区域，最后用**可获利性**筛除不划算的区域。
- **检测（`ScopDetection`）只判定「能不能」，建模（`ScopInfo`/`ScopBuilder`）才构造真正的多面体对象**；二者是相邻但独立的阶段，前者为后者提供合法区域清单。

## 7. 下一步学习建议

本讲只在设计与概念层面接触了 SCoP 与 `ScopDetection`，刻意回避了逐条合法性判定的细节。接下来：

- **u3-l2（检测算法与合法性判定）**：深入 `ScopDetection.cpp` 的 `isValid*` 系列（`isValidRegion`/`isValidInstruction`/`isValidAccess`/`isValidBranch`/`isValidCFG`/`isValidLoop`），看清每一类 IR 构造被接受或拒绝的具体规则——这是本讲 4.1.2 那张判定表的源码级展开。
- **u3-l3（检测诊断与 CFG 图示）**：学 `RejectLog`/`OptimizationRemark` 与 `-polly-dot`/`-polly-show`，掌握「为什么我的循环没被优化」的排查方法。
- **U4（多面体模型构建）**：跨过检测门槛，进入建模阶段，看 `ScopInfo`/`ScopBuilder` 如何把合法区域变成 `Scop` 对象，以及 u4-l4 的 `SCEVAffinator` 如何把 SCEV 精译为 ISL。
