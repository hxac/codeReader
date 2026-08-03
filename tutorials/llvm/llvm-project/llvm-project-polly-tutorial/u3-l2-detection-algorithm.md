# u3-l2 检测算法与合法性判定

> 承接 [u3-l1 SCoP 概念与 ScopDetection 设计](u3-l1-scop-detection-design.md)：上一讲建立了「SCoP = 静态控制子图 + 单入单出 Region + 仿射约束」的概念，并说明了 `ScopDetection::detect()` 用「先试整体、不合法再下降、找到后向上扩展」的算法把**最大合法区域**填入 `ValidRegions`。本讲顺着这条线索往下钻：**具体是哪一行代码拒绝了你的循环，拒绝依据是什么**。

## 1. 本讲目标

学完本讲，你应当能够：

- 画出从 `detect()` 到每一条 `isValid*` 判定函数的**分派骨架**，知道一个区域被检查的先后顺序。
- 说清「控制流合法性」这一层：分支条件为何必须是**仿射整数比较**、循环为何必须有**单出口**、CFG 为何必须**可归约**。
- 说清「别名与基址分析」这一层：基址为何必须**在区域内不变**、下标为何必须是**仿射访问函数**、别名冲突为何要靠**运行时检查**兜底。
- 能根据一段 LLVM IR **预测**它能否成为 SCoP；预测错了，能用 `-polly-print-detect` 与 `isValid*` 的判定点反查原因。
- 理解「拒绝诊断」与「非仿射子区域兜底（box）」两套机制如何让检测既严格又可扩展。

## 2. 前置知识

本讲假设你已掌握 u3-l1 的全部结论，特别是：**SCoP**、**Region/RegionInfo**、**仿射（affine）**、**SCEV 把 IR 值翻译成符号表达式**、`ValidRegions` 的含义。在此基础上补充三个 IR 层面的概念：

1. **终结指令（Terminator）**。每个基本块的最后一条指令决定控制流走向，常见有：
   - `br i1 %cond, label %T, label %F`（条件分支，`CondBrInst`）
   - `br label %T`（无条件分支，`UncondBrInst`）
   - `switch`（多路分支，`SwitchInst`）
   - `ret`（返回）、`unreachable`、`indirectbr`（间接跳转）、`callbr`（如内联汇编跳转）
   
   Polly 能处理的是前三种里条件可被仿射化的那部分。

2. **ICmp 与 SCEV 比较**。一个条件分支的判断条件通常是 `icmp slt i64 %i, %N`。`icmp` 的两个操作数会被 `ScalarEvolution` 翻译成两个 SCEV 表达式，Polly 要求这两个表达式都在循环迭代变量与不变参数上是**线性**的。一个仿射表达式形如

   \[ f(i,j,\dots) = c_0 + c_1\,i + c_2\,j + \dots \]

   其中系数 \(c_k\) 是整数（或对 SCoP 不变的量），\(i,j\) 是循环迭代变量。`i < N` 是仿射的；`A[i] < B[i]`（两边都含指针且非单基址）、`i * i < N`（二次项）不是。

3. **别名（aliasing）与基址（base pointer）**。一次 `load`/`store` 访问的地址 SCEV 形如 `base + offset`，其中 `base` 是 `SCEVUnknown`（最终指向某个 SSA 值），`offset` 是偏移量。两个不同基址的访问**可能别名**，意味着 Polly 无法在编译期证明它们指向不重叠的内存——这时要么插入运行时别名检查，要么直接拒绝该 SCoP。

> 关键复习：检测只判定「能不能」，建模（`ScopInfo`/`ScopBuilder`，U4）才构造真正的多面体对象。本讲全程停留在「判定」阶段。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [include/polly/ScopDetection.h](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/include/polly/ScopDetection.h) | `ScopDetection` 类声明、`DetectionContext` 结构、全部 `isValid*` 接口 |
| [lib/Analysis/ScopDetection.cpp](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Analysis/ScopDetection.cpp) | 检测算法的全部实现，本讲的主战场 |
| [include/polly/ScopDetectionDiagnostic.h](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/include/polly/ScopDetectionDiagnostic.h) | `Report*` 拒绝原因类层次（`ReportNonAffBranch`、`ReportNonAffineAccess`、`ReportAlias` 等） |

本讲涉及的核心函数速查表（均在 `ScopDetection.cpp`）：

| 函数 | 行号 | 职责 |
|------|------|------|
| `detect()` | 341 | 函数入口：过滤函数 → `findScops` → 剔除不盈利区域 |
| `findScops()` | 1584 | 区域树递归：试整体、不合法下降子区域、合法再向上扩展 |
| `isValidRegion()` | 1773 | 区域级闸门：入口/出口/间接前驱/函数入口块 |
| `allBlocksValid()` | 1650 | 逐基本块：先查循环（`isValidLoop`）再查 CFG 再查指令 |
| `isValidCFG()` | 654 | 终结指令分派到分支/switch |
| `isValidBranch()` / `isValidSwitch()` | 574 / 550 | 条件仿射性判定 |
| `isValidLoop()` | 1321 | 循环出口数、回边计数能否被 ISL 计算 |
| `isReducibleRegion()` | 1860 | DFS 染色判可归约性 |
| `isValidInstruction()` | 1222 | 指令级分派：调用/内存/其它 |
| `isValidCallInst()` / `isValidIntrinsicInst()` | 688 / 756 | 副作用调用与内存 intrinsic 的放行 |
| `isValidMemoryAccess()` / `isValidAccess()` | 1199 / 1072 | 访存合法性：基址、仿射、别名 |
| `isAffine()` | 538 | SCEV 是否仿射（包装 `isAffineExpr`） |
| `addOverApproximatedRegion()` | 450 | 非仿射子区域兜底（box） |
| `invalid<RR>()` | 394 | 统一的「记录拒绝原因并返回 false」模板 |

## 4. 核心概念与源码讲解

### 4.1 逐级判定骨架：从 detect 到 isValid\* 的分派

#### 4.1.1 概念说明

SCoP 检测本质上是一棵**判定树**：一个区域要合法，必须同时满足一组**分层、彼此独立**的条件——区域结构合法、每个循环合法、每条控制流边合法、每条指令合法、每次访存合法。任何一层失败都意味着该区域不能整体成为 SCoP。

Polly 的策略不是「失败就放弃整个函数」，而是：

1. 先用最大的区域去试；
2. 失败就**下降到子区域**继续试（递归）；
3. 在子区域里找到合法的之后，再尝试**向上扩展**成更大的（非规范）区域。

这套「试—下降—扩展」就是 u3-l1 提到的算法，本讲看清它的代码骨架，以及每层判定**调用谁、按什么顺序**。

#### 4.1.2 核心流程

```
detect(F)                              # 函数级入口
 ├── 过滤：函数名/属性/无循环
 ├── findScops(TopRegion)               # 在区域树上递归
 │     ├── isValidRegion(Context) ──┐
 │     │   ├── 区域结构检查          │  4.1.3
 │     │   ├── allBlocksValid(Context)
 │     │   │     ├── 对每个循环: isValidLoop()       # 4.2
 │     │   │     ├── 对每个块:   isValidCFG()        # 4.2
 │     │   │     ├── 对每条指令: isValidInstruction()# 4.3
 │     │   │     └── hasAffineMemoryAccesses()       # 4.3
 │     │   └── isReducibleRegion()                   # 4.2
 │     ├── 合法 → insert ValidRegions，return
 │     └── 非法 → removeCachedResults，对每个子区域递归 findScops
 │                然后对已合法子区域调 expandRegion() 尝试向上扩展
 └── 剔除不盈利区域（isProfitableRegion）
```

两个值得记住的设计：

- **短路 vs 坚持（`KeepGoing`）**。默认情况下，`isValid*` 一旦发现非法就立刻短路返回（`KeepGoing=false`）；开启 `-polly-detect-keep-going` 后，会收集**全部**拒绝原因再返回，便于一次性看清所有问题。
- **判定与盈利分两遍**。`findScops` 只管「合法不合法」，盈利性（`isProfitableRegion`）在 `detect()` 最后单独剔除——这样可以让盈利阈值等策略独立调整，不影响判定逻辑。

#### 4.1.3 源码精读

函数级入口 `detect()`：先做函数级过滤，再调 `findScops`，最后剔除不盈利区域。

[lib/Analysis/ScopDetection.cpp:341-392](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Analysis/ScopDetection.cpp#L341-L392) —— `detect()` 的过滤顺序值得注意：`PollyProcessUnprofitable` 关闭时若函数无循环直接 return；`-polly-only-func`/`-polly-ignore-func` 用正则过滤函数名；`isValidFunction(F)` 会跳过带 OpenMP 子函数标记等属性的函数。

`findScops()` 是「试—下降—扩展」三段式：

[lib/Analysis/ScopDetection.cpp:1584-1648](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Analysis/ScopDetection.cpp#L1584-L1648) —— 关键三段：

```cpp
// ① 先试当前区域整体
DidBailout = !isValidRegion(Context);
...
if (Context.IsInvalid) {
  removeCachedResults(R);
} else {
  ValidRegions.insert(&R);
  return;                       // 合法就登记并返回，不再下钻
}
// ② 不合法：对每个子区域递归
for (auto &SubRegion : R)
  findScops(*SubRegion);
// ③ 对已合法的子区域，尝试向上扩展成更大的区域
for (Region *CurrentRegion : ToExpand) {
  ...
  Region *ExpandedR = expandRegion(*CurrentRegion);
  if (!ExpandedR) continue;
  R.addSubRegion(ExpandedR, true);
  ValidRegions.insert(ExpandedR);
}
```

注意第 ③ 步：区域树默认只含**规范区域**（canonical region），一些能成 SCoP 的非规范区域不会直接出现，`expandRegion()` 通过反复调用 `Region::getExpandedRegion()` 逐步放大并重跑 `allBlocksValid` 来捕捉它们（[lib/Analysis/ScopDetection.cpp:1504-1561](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Analysis/ScopDetection.cpp#L1504-L1561)）。

区域级闸门 `isValidRegion()`：

[lib/Analysis/ScopDetection.cpp:1773-1831](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Analysis/ScopDetection.cpp#L1773-L1831) —— 在进入逐块检查之前，它先把几条**区域级**硬约束挡掉，每条都对应一个明确的 `Report*` 拒绝原因：

| 检查 | 拒绝原因 | 含义 |
|------|----------|------|
| 顶层区域且未开 `-polly-detect-full-functions` | 直接置无效 | 默认不把整个函数当 SCoP |
| 出口块以 `unreachable` 结尾 | `ReportUnreachableInExit` | 没有正常的单出口 |
| 前驱终结符是 `indirectbr`/`callbr` | `ReportIndirectPredecessor` | 间接跳转破坏静态控制流 |
| 区域入口就是函数入口块 | `ReportEntry` | 代码生成要在入口插 alloca，故入口块不能进 SCoP |
| `allBlocksValid` 失败 | 各类 `isValid*` 内部上报 | 见 4.2 / 4.3 |
| `isReducibleRegion` 失败 | `ReportIrreducibleRegion` | 含不可归约控制流（goto 式） |

逐块检查 `allBlocksValid()`：

[lib/Analysis/ScopDetection.cpp:1650-1697](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Analysis/ScopDetection.cpp#L1650-L1697) —— 这是分派骨架的核心。它**两遍扫描**区域内的基本块：

- **第一遍**只看循环头：若区域**完整包含**某循环则调 `isValidLoop`；若只包含部分 latch 则上报 `ReportLoopOnlySomeLatches`。
- **第二遍**对每个块：先 `isValidCFG`（看终结指令），跳过 error block 后，再对块内每条非终结指令调 `isValidInstruction`。
- 最后整体 `hasAffineMemoryAccesses` 复核访存（见 4.3）。

```cpp
for (BasicBlock *BB : CurRegion.blocks()) {
  bool IsErrorBlock = isErrorBlock(*BB, CurRegion);
  if (!isValidCFG(*BB, false, IsErrorBlock, Context) && !KeepGoing)
    return false;
  if (IsErrorBlock) continue;
  for (BasicBlock::iterator I = BB->begin(), E = --BB->end(); I != E; ++I)
    if (!isValidInstruction(*I, Context)) { ... }
}
```

注意 `--BB->end()`：循环指令时**跳过终结指令**（它已由 `isValidCFG` 负责），两者分工不重叠。

#### 4.1.4 代码实践

**目标**：亲手跑通「检测」阶段，看到 `Valid Region for Scop` 输出，建立对 `-polly-print-detect` 与 `polly-custom<detect>` 用法的肌肉记忆。

**操作步骤**：

1. 准备一段最简单的双重循环（示例代码）：

   ```c
   // 示例代码：example.c
   void matmul(int *C, int *A, int *B, int N) {
     for (int i = 0; i < N; i++)
       for (int j = 0; j < N; j++)
         for (int k = 0; k < N; k++)
           C[i*N+j] += A[i*N+k] * B[k*N+j];
   }
   ```

2. 用 clang 生成 LLVM IR（保留调试信息便于定位）：

   ```bash
   clang -O1 -Xclang -disable-llvm-passes -emit-llvm -S example.c -o example.ll
   ```

3. 只跑「检测」阶段并打印结果：

   ```bash
   opt -load-pass-plugin=LLVMPolly.so \
       -passes='polly-custom<detect>' -polly-print-detect \
       -disable-output example.ll 2>&1
   ```

   > 真实可参考的 RUN 行见仓库测试 [test/ScopDetect/intrinsics_1.ll:1](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/test/ScopDetect/intrinsics_1.ll#L1)，它用 `%loadNPMPolly` 代替手写 `-load-pass-plugin`。

**需要观察的现象**：输出形如

```
Detected Scops in Function matmul
Valid Region for Scop: for.cond => for.end
```

**预期结果**：三重循环区域被识别为单一合法 SCoP。若输出只有 `Detected Scops in Function matmul` 而没有 `Valid Region` 行，说明被拒——这正是后续模块要追查的情形。

**待本地验证**：`%loadNPMPolly` 的具体拼写依赖你的构建（CMake 是否定义该 lit 变量）；若手跑 `opt`，请用你本地 `LLVMPolly.so` 的实际路径。

#### 4.1.5 小练习与答案

**练习 1**：`allBlocksValid` 为什么把循环判定（第一遍）和指令判定（第二遍）拆成两次扫描，而不是混在一次里？

> **参考答案**：循环判定需要先确认「区域是否完整包含该循环」，这决定了用 `isValidLoop` 还是上报 `ReportLoopOnlySomeLatches`；它是区域级的结构判定。指令判定是逐条 IR 的局部判定。先结构后局部、两次扫描，使得循环结构错误能尽早暴露，且 `isValidCFG`/`isValidInstruction` 的实现无需各自再去判断循环归属。

**练习 2**：`findScops` 在子区域递归之后，为何还要再跑一遍 `expandRegion`？

> **参考答案**：区域树默认只含规范区域，某些跨规范边界但本身合法的「非规范区域」不会出现在树里。递归只能找到规范子区域里的 SCoP；`expandRegion` 对已合法的规范子区域逐步放大并重检，从而捕捉这些更大的非规范合法区域，保证找到的是「最大」合法区域。

---

### 4.2 控制流合法性：分支、循环与可归约性

> **最小模块：控制流合法性**。这一层回答：区域里的每一条控制流边，是否都能被多面体模型静态刻画。

#### 4.2.1 概念说明

多面体模型用**线性不等式组**刻画「在什么条件下执行哪条语句」（即语句的迭代域 Domain）。这要求控制流在编译期**完全确定**，且能写成迭代变量与参数的线性组合。具体落到 LLVM IR，对控制流有三条硬约束：

1. **条件必须是仿射整数比较**。`if (i < N)` 可以，因为 `i` 与 `N` 的 SCEV 都仿射；`if (A[i])` 里的条件若 `A[i]` 不能提升为不变量，就不仿射。
2. **每个循环必须有单一确定的出口与可计算的回边计数**。多面体域构造算法假设一个循环对应一个子区域、单一 exit block；多出口循环无法表达。
3. **CFG 必须可归约（reducible）**。即控制流可以用 `for`/`if` 嵌套写出，不含 `goto` 式的交叉跳转。不可归约流会让域的「前向传播」失效。

> 名词解释：**可归约（reducible）**——一个 CFG 可归约，当且仅当它的每个循环都有一个**支配其所有入口的循环头**。直观上就是「没有从循环外跳进循环中间」的 goto。Polly 用 DFS 三色染色法判定。

#### 4.2.2 核心流程

```
isValidCFG(BB)                         # 看终结指令
 ├── UnreachableInst (允许时) → true
 ├── ReturnInst (仅顶层区域)   → true
 ├── UncondBrInst              → true
 ├── CondBrInst → isValidBranch()      # 仿射条件判定
 ├── SwitchInst  → isValidSwitch()
 └── 其它 → ReportInvalidTerminator

isValidBranch(BB, BI, Cond)            # 条件递归拆解
 ├── Cond 是 ConstantInt   → true
 ├── Cond 是 And/Or        → 递归判两个子条件
 ├── Cond 是 PHI(常数 0/1) → true
 ├── Cond 是 Load(可不变量)→ 记入 RequiredILS，true
 ├── Cond 不是 ICmp        → ReportInvalidCond（或 box）
 ├── 任一操作数 Undef      → ReportUndefOperand
 ├── 无符号比较且未放行    → box
 ├── 多指针比较            → false
 ├── LHS/RHS 都仿射        → true
 └── 否则                  → ReportNonAffBranch（或 box）

isValidLoop(L)                         # 循环结构
 ├── 无 exiting block      → ReportLoopHasNoExit
 ├── 多个不同 exit block    → ReportLoopHasMultipleExits
 ├── canUseISLTripCount    → true   # ISL 能算回边计数
 └── 否则                  → box 或 ReportLoopBound

（区域级）isReducibleRegion(R)          # DFS 三色染色
```

#### 4.2.3 源码精读

`isValidCFG` 是个纯粹的终结指令分派器：

[lib/Analysis/ScopDetection.cpp:654-686](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Analysis/ScopDetection.cpp#L654-L686) —— `CondBrInst` 与 `SwitchInst` 的条件若是 `UndefValue` 会立刻上报 `ReportUndefCond`，其余转交 `isValidBranch`/`isValidSwitch`。注意 `isValidCFG` 接受一个 `AllowUnreachable` 形参，error block 内的 `unreachable` 可被放行。

`isValidBranch` 是这一层最值得精读的函数，因为它体现了「仿射」的工程化判定：

[lib/Analysis/ScopDetection.cpp:574-652](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Analysis/ScopDetection.cpp#L574-L652) —— 递归处理几个特例后，核心是对 `ICmp` 的判定：

```cpp
ICmpInst *ICmp = cast<ICmpInst>(Condition);
...
Loop *L = LI.getLoopFor(&BB);
const SCEV *LHS = SE.getSCEVAtScope(ICmp->getOperand(0), L);
const SCEV *RHS = SE.getSCEVAtScope(ICmp->getOperand(1), L);
...
if (isAffine(LHS, L, Context) && isAffine(RHS, L, Context))
  return true;                    // ← 仿射条件放行
```

几点要点：

- **递归拆 `And`/`Or`**：`i < N && j < M` 会被拆成两个子条件分别判定，都仿射才放行。
- **PHI 常数折叠**：若某 PHI 在区域内取唯一非 error 值且为 0/1，视为常数。
- **无符号比较**：`icmp ult` 默认不放行（`PollyAllowUnsignedOperations` 控制可开），因无符号语义难纳入有符号的多面体模型；放不开则尝试 box。
- **多指针比较**：`if (p == q)` 这类两边都含不同基址的等值比较、或关系比较含多基址，会被 `involvesMultiplePtrs` 拒掉（[lib/Analysis/ScopDetection.cpp:504-537](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Analysis/ScopDetection.cpp#L504-L537)）。

`isValidLoop` 守住循环的结构前提：

[lib/Analysis/ScopDetection.cpp:1321-1380](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Analysis/ScopDetection.cpp#L1321-L1380) —— 三道关卡：

```cpp
if (!hasExitingBlocks(L))
  return invalid<ReportLoopHasNoExit>(...);          // 无限循环
...
for (BasicBlock *ExitBB : ExitBlocks)
  if (TheExitBlock != ExitBB)
    return invalid<ReportLoopHasMultipleExits>(...);  // 多出口
if (canUseISLTripCount(L, Context))
  return true;                                       // ISL 能算回边计数
...
return invalid<ReportLoopBound>(..., SE.getBackedgeTakenCount(L));
```

`canUseISLTripCount`（[lib/Analysis/ScopDetection.cpp:1294-1319](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Analysis/ScopDetection.cpp#L1294-L1319)）对循环的所有 exiting block 与 latch 重跑 `isValidCFG(..., IsLoopBranch=true, ...)`，确认循环控制流本身可被建模。

最后，区域级的**可归约性**检查在 `isValidRegion` 末尾：

[lib/Analysis/ScopDetection.cpp:1860-1932](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Analysis/ScopDetection.cpp#L1860-L1932) —— 用经典 DFS 三色染色：`WHITE`（未访问）→`GREY`（在栈上）→`BLACK`（完成）。发现指向 `GREY` 节点的回边时，若目标**不支配**源（即非自然循环头），则判定为不可归约，记录 `ReportIrreducibleRegion`：

```cpp
} else if (BBColorMap[SuccBB] == GREY) {
  // GREY 表示控制流成环
  if (!DT.dominates(SuccBB, CurrBB)) {   // 目标不支配源 → 不可归约
    DbgLoc = TInst->getDebugLoc();
    return false;
  }
}
```

#### 4.2.4 代码实践

**目标**：用一个「非常量、运行期才知的循环边界」触发控制流层的拒绝，并定位到 `isValidLoop`。

**操作步骤**：

1. 用仓库里现成的真实测试作为脚本（示例代码，可直接复用其 RUN 行）。它对应的 C 源是：

   ```c
   // 示例代码（对应 test/ScopDetect/non-affine-loop-condition-dependent-access.ll）
   void f(int *restrict A, int *restrict C) {
     int j = 0;
     for (int i = 0; i < 1024; i++) {
       while ((j = C[j]))   // 内层 while 的边界 j=C[j] 运行期才知
         A[j]++;
     }
   }
   ```

2. 直接运行该测试文件（它已含三条对照 RUN 行）：

   ```bash
   # 在你的 Polly 构建目录下：
   llvm-lit test/ScopDetect/non-affine-loop-condition-dependent-access.ll
   # 或手动跑其中一条 RUN：
   opt -load-pass-plugin=LLVMPolly.so -aa-pipeline=basic-aa \
       -polly-allow-nonaffine-branches -polly-allow-nonaffine-loops=false \
       -passes='polly-custom<detect>' -polly-print-detect -disable-output \
       test/ScopDetect/non-affine-loop-condition-dependent-access.ll
   ```

3. 把 `-polly-allow-nonaffine-loops` 从 `false` 改成 `true`，再改回 `false` 对比输出。

**需要观察的现象**：

- `-polly-allow-nonaffine-loops=false` 时输出**没有** `Valid Region for Scop: bb1 => bb13`（REJECTNONAFFINELOOPS 前缀断言 `NOT: Valid`）。
- `=true` 且加 `-polly-allow-nonaffine` 时出现 `Valid Region for Scop: bb1 => bb13`（ALLOWNONAFFINELOOPSANDACCESSES 断言）。

**预期结果**：内层 `while (C[j])` 的边界依赖运行期加载值，`isValidLoop` 里 `canUseISLTripCount` 无法证明回边计数可表达为仿射式，于是（在不开 box 时）上报 `ReportLoopBound`。开启非仿射循环放行后，循环被 box，区域才得以放行。

#### 4.2.5 小练习与答案

**练习 1**：下面哪几个分支条件能通过 `isValidBranch`？(a) `i < N`；(b) `i*i < N`；(c) `A[i] < B[i]`；(d) `i < N && j < M`。

> **参考答案**：(a) 通过（两边仿射）。(d) 通过（`And` 拆成两个仿原子条件，各自仿射）。(b) 不通过：`i*i` 是 SCEV 的二次 `MulRec`，非仿射，触发 `ReportNonAffBranch`。(c) 通常不通过：两侧都含 `A`/`B` 不同基址，`involvesMultiplePtrs` 命中关系比较分支返回 false。

**练习 2**：一个循环有 `break` 提前退出（生成第二个 exit block），会被哪个 `Report*` 拒绝？

> **参考答案**：`ReportLoopHasMultipleExits`。`isValidLoop` 收集所有 exit block，发现存在与第一个不同的 exit block 即拒绝——多面体域构造假设循环单出口。

**练习 3**：`isReducibleRegion` 用支配关系 `DT.dominates(SuccBB, CurrBB)` 判断回边是否「自然」。如果一个区域里存在 `goto` 从循环外跳进循环体中间，这条边会怎样？

> **参考答案**：DFS 走到该回边时目标（循环体内的 GREY 节点）不支配源（循环外的跳转点），返回 false，区域被判为不可归约，上报 `ReportIrreducibleRegion`。

---

### 4.3 别名与基址分析：访存合法性的逐层闸门

> **最小模块：别名与基址分析**。这一层回答：每一次 `load`/`store` 的地址，是否都能被多面体模型刻画为「不变基址 + 仿射偏移」，以及基址之间是否可能别名。

#### 4.3.1 概念说明

多面体模型里，一次内存访问被表示成一条**访问关系（Access Relation）**：从语句的迭代点到被访问内存单元的映射。要构造它，Polly 要求地址 SCEV 能分解为：

\[ \text{address} = \text{BasePtr} + \text{AffineOffset}(i, j, \dots) \]

这引出四条要求：

1. **基址在区域内不变（invariant）**。`BasePtr` 在整个 SCoP 执行期间不能变，否则访问关系无法写成固定基址 + 偏移。`A[i]` 里 `A` 是参数（不变）✅；`p->data[i]` 若 `p` 在循环里被改 ❌。
2. **偏移仿射**。下标必须是迭代变量的线性式。`A[i]`、`A[i+1]`、`A[2*i]` ✅；`A[C[j]]`（下标依赖运行期加载值）❌，除非能 deinearization 或当作不变量提升。
3. **可去线性化（delinearization）**。多维数组 `A[i][j]` 在 IR 里实际是一维 `A + i*DIM + j`。Polly 默认开启 `-polly-delinearize`，把这种「看似非仿射的多维访问」还原成各维仿射下标（详见 [include/polly/ScopInfo.h](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/include/polly/ScopInfo.h) 与 U4）。
4. **别名可解**。若两个不同基址可能指向重叠内存，编译期无法证明安全时，要么能构造**运行时别名检查**（runtime alias check），要么拒绝。

> 名词解释：**运行时别名检查**——在 SCoP 入口插入一段代码，比较各基址的地址区间是否重叠；不重叠才执行优化版本，重叠则回退原代码。这是 Polly 用动态手段弥补静态别名分析不足的关键机制（U8 代码生成会真正生成它）。

#### 4.3.2 核心流程

```
isValidInstruction(Inst)                # 指令分派
 ├── 操作数类型/来源检查
 ├── LandingPad/Resume → false
 ├── CallInst → isValidCallInst()       # 副作用调用放行（见下）
 ├── 不访存且非 alloca → true
 ├── AllocaInst（区域内）→ ReportAlloca
 ├── MemAccInst（load/store）
 │     ├── 非 simple → ReportNonSimpleMemoryAccess
 │     └── isValidMemoryAccess()
 │           └── isValidAccess()        # ← 访存合法性主闸
 └── 其它未知 → ReportUnknownInst

isValidAccess(Inst, AF, BP)
 ├── BP 为空 → ReportNoBasePtr
 ├── 基址 Undef → ReportUndefBasePtr
 ├── IntToPtr → ReportIntToPtr
 ├── 基址不 invariant → ReportVariantBasePtr
 ├── 偏移仿射判定 + deinearization 登记
 │     └── 非仿射（且未 box）→ ReportNonAffineAccess
 └── 别名：AliasSet 非 must-alias
       ├── 能构造运行时检查 → true
       └── 否则 → ReportAlias

isValidCallInst(CI)                     # 副作用调用
 ├── doesNotReturn → false
 ├── doesNotAccessMemory (readnone) → true
 ├── intrinsic 且 isValidIntrinsicInst → true
 ├── 间接调用 (CalledFunction==null) → false
 └── AllowModrefCall + 已知 modref 行为 → 放行（登记 unknown 访问）
```

#### 4.3.3 源码精读

指令分派 `isValidInstruction`：

[lib/Analysis/ScopDetection.cpp:1222-1281](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Analysis/ScopDetection.cpp#L1222-L1281) —— 它把每条指令分流到三类：

```cpp
// ① 调用：单独走 isValidCallInst
if (CallInst *CI = dyn_cast<CallInst>(&Inst)) {
  if (isValidCallInst(*CI, Context)) return true;
  return invalid<ReportFuncCall>(Context, true, &Inst);
}
// ② 不访存：除 alloca 外都放行（纯计算）
if (!Inst.mayReadOrWriteMemory()) {
  if (!isa<AllocaInst>(Inst)) return true;
  return invalid<ReportAlloca>(Context, true, &Inst);
}
// ③ 访存：load/store 走 isValidMemoryAccess
if (auto MemInst = MemAccInst::dynapi(Inst)) {
  if (!MemInst.isSimple())
    return invalid<ReportNonSimpleMemoryAccess>(...);
  return isValidMemoryAccess(MemInst, Context);
}
return invalid<ReportUnknownInst>(Context, true, &Inst);
```

注意几个细节：`alloca` 被拒（`ReportAlloca`），因为栈分配在区域内不可表达；`!MemInst.isSimple()`（如 atomic/volatile load）被拒（`ReportNonSimpleMemoryAccess`）。

副作用调用 `isValidCallInst`：

[lib/Analysis/ScopDetection.cpp:688-754](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Analysis/ScopDetection.cpp#L688-L754) —— 放行规则层层递进：`doesNotAccessMemory`（readnone，如 `sqrt`/`ceil` 等数学库函数）直接放行；`memcpy`/`memset`/`memmove` 走 `isValidIntrinsicInst`（[lib/Analysis/ScopDetection.cpp:756-800](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Analysis/ScopDetection.cpp#L756-L800)）单独验证源/目的/长度都仿射；**间接调用**（`getCalledFunction()==nullptr`）一律拒绝——这正是本讲实践里「间接调用」被拒的根因；`AllowModrefCall` 开启时，已知只读或仅参数点影响的函数可放行（登记为 unknown 访问）。

访存主闸 `isValidAccess`——这是本模块的核心：

[lib/Analysis/ScopDetection.cpp:1072-1197](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Analysis/ScopDetection.cpp#L1072-L1197) —— 三段：

**第一段，基址**（去掉偏移后的纯基址必须合法）：

```cpp
if (!BP) return invalid<ReportNoBasePtr>(...);
if (isa<UndefValue>(BV)) return invalid<ReportUndefBasePtr>(...);
if (IntToPtrInst *Inst = dyn_cast<IntToPtrInst>(BV))
  return invalid<ReportIntToPtr>(...);
if (!isInvariant(*BV, Context.CurRegion, Context))
  return invalid<ReportVariantBasePtr>(...);   // 基址在区域内变化
```

**第二段，仿射偏移与去线性化**：

```cpp
AF = SE.getMinusSCEV(AF, BP);   // 去掉基址，剩下纯偏移
...
bool IsAffine = !IsVariantInNonAffineLoop && isAffine(AF, Scope, Context);
if (isa<MemIntrinsic>(Inst) && !IsAffine) {
  return invalid<ReportNonAffineAccess>(...);          // 内存 intrinsic 必须仿射
} else if (PollyDelinearize && !IsVariantInNonAffineLoop) {
  Context.Accesses[BP].push_back({Inst, AF});
  if (!IsAffine)
    Context.NonAffineAccesses.insert({BP, LI.getLoopFor(Inst->getParent())});
} else if (!AllowNonAffine && !IsAffine) {
  return invalid<ReportNonAffineAccess>(...);          // 非仿射且未放行 → 拒
}
```

这里有个关键设计：**非仿射访问先不立刻拒**，而是登记到 `NonAffineAccesses`，留给稍后的 `hasAffineMemoryAccesses`（[lib/Analysis/ScopDetection.cpp:1054-1071](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Analysis/ScopDetection.cpp#L1054-L1071)）尝试用**去线性化**把同一基址的一组非仿射访问**联合**还原成多维仿射下标。只有去线性化也失败才真正拒绝。这是 Polly 能处理 `A[i][j]` 的关键。

**第三段，别名**：

```cpp
AliasSet &AS = Context.AST.getAliasSetFor(...);
if (!AS.isMustAlias()) {
  if (PollyUseRuntimeAliasChecks) {
    ... // 不动点迭代：尝试把可提升的 load 当不变量
    if (CanBuildRunTimeCheck) return true;
  }
  return invalid<ReportAlias>(Context, true, Inst, AS);
}
```

`isAffine` 本身是个薄包装：

[lib/Analysis/ScopDetection.cpp:538-548](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Analysis/ScopDetection.cpp#L538-L548) —— 它把真正的仿射判定委托给 `ScopHelper` 的 `isAffineExpr`（SCEV→仿射的细节在 [u4-l4 SCEVAffinator](u4-l4-scev-to-isl.md)），并附带要求「若该仿射式依赖某些 load，这些 load 必须能作为不变量提升（`onlyValidRequiredInvariantLoads`）」。后者正是 `-polly-invariant-load-hoisting` 的入口：开它，`while (C[j])` 里那种「条件是 load」的情形才有机会被放行（见 [lib/Analysis/ScopDetection.cpp:468-502](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Analysis/ScopDetection.cpp#L468-L502)）。

#### 4.3.4 代码实践

**目标**：用一个「非仿射下标 + 间接基址」的循环触发访存层拒绝，对照 `isValidAccess` 定位拒绝点。

**操作步骤**：

1. 写一段间接寻址的循环（示例代码）：

   ```c
   // 示例代码：indirect.c
   void scatter(int *restrict A, int *restrict B, int *restrict Idx, int N) {
     for (int i = 0; i < N; i++)
       A[Idx[i]] = B[i];   // 下标 Idx[i] 是运行期加载值，非仿射
   }
   ```

2. 生成 IR 并只跑检测：

   ```bash
   clang -O1 -Xclang -disable-llvm-passes -emit-llvm -S indirect.c -o indirect.ll
   opt -load-pass-plugin=LLVMPolly.so -aa-pipeline=basic-aa \
       -passes='polly-custom<detect>' -polly-print-detect -disable-output indirect.ll
   ```

3. 为看清**具体拒绝原因**，加 `-polly-detect-track-failures`（默认已开）并查看 OptimizationRemark，或对照源码：`Idx[i]` 使 `A` 的偏移 SCEV 含一个 `LoadRec`，`isAffine` 失败 → 登记为 `NonAffineAccesses`，去线性化无法救（不同 `i` 之间无线性关系）→ `hasAffineMemoryAccesses` 拒绝，最终上报 `ReportNonAffineAccess`。

4. 对比实验：加 `-polly-allow-nonaffine` 重跑，观察是否放行（放行后该访问被当作 unknown 处理）。

**需要观察的现象**：默认输出无 `Valid Region`；加 `-polly-allow-nonaffine` 后可能放行（取决于是否还有其它约束）。

**预期结果**：非仿射下标 `A[Idx[i]]` 被 `ReportNonAffineAccess` 拒绝，根因在 `isValidAccess` 第二段的仿射判定。

**待本地验证**：`-polly-allow-nonaffine` 放行与否还受盈利性与别名影响，请以本地实际输出为准。

#### 4.3.5 小练习与答案

**练习 1**：`A[i]` 与 `A[i][j]` 在 IR 里哪个看起来「非仿射」？Polly 如何救回 `A[i][j]`？

> **参考答案**：`A[i]` 的地址 SCEV 是 `A + 4*i`，天然仿射。`A[i][j]` 经前端展开成一维后是 `A + D*i + j`（`D` 是第二维大小），当 `D` 是循环不变量时仍仿射；但当多维访问混用且维大小不规则时，单条访问的 SCEV 可能呈现「非仿射」表象。Polly 开 `-polly-delinearize` 后，把同一基址的一组访问登记到 `NonAffineAccesses`，由 `hasAffineMemoryAccesses`/`hasBaseAffineAccesses` **联合**推导出各维仿射下标，从而救回。

**练习 2**：一次 `call void @f(ptr %p)`（`f` 无 `readnone` 属性）会被 `isValidCallInst` 如何处理？

> **参考答案**：`doesNotAccessMemory` 不成立；若非 intrinsic，`getCalledFunction()` 非 null（直接调用）但函数非纯；在 `AllowModrefCall` 关闭（默认）时返回 false，于是 `isValidInstruction` 上报 `ReportFuncCall`。开启 `AllowModrefCall` 且 `AA.getMemoryEffects` 表明 `f` 只读或仅参数点影响时才可能放行（登记 unknown 访问）。

**练习 3**：为什么 `isValidAccess` 里非仿射访问**先登记不立刻拒**？

> **参考答案**：因为单看一条 `A[i][j]` 的展开式可能判为非仿射，但同一基址的多条访问**联合**做去线性化后，往往能还原成多维仿射下标。先登记到 `NonAffineAccesses`，再在 `hasAffineMemoryAccesses` 里统一尝试去线性化，能避免「误杀」合法的多维数组访问。

---

### 4.4 拒绝诊断机制与非仿射子区域兜底

#### 4.4.1 概念说明

前三个模块的每一条 `isValid*` 在失败时几乎都调用了同一个模板 `invalid<ReportXxx>(...)`。这一模块把它单拎出来讲清两件事：

1. **拒绝诊断（RejectLog）**。每个 `DetectionContext` 持有一个 `RejectLog`，`invalid<>` 把失败原因（一个 `RejectReason` 子类）记进去。`-polly-detect-track-failures`（默认开）控制是否记录；`-polly-detect-keep-going` 控制是否收集全部原因而非首个就短路。这套机制是 u3-l3「检测诊断」讲的 `-polly-print-detect`、opt-viewer、`regionIsInvalidBecause` 的数据来源。

2. **非仿射子区域兜底（box）**。严格仿射会让很多真实代码（含 `while`、运行期条件、间接下标）被拒。Polly 提供一个逃生舱：当 `-polly-allow-nonaffine-branches`/`-polly-allow-nonaffine-loops`/`-polly-allow-nonaffine` 开启时，非法的分支/循环/访问不直接拒，而是把包含它的子区域**整体过近似（over-approximate）**为一个「盒子」——其内部控制流被当作「可能执行任意路径」处理，不精确建模但仍能纳入 SCoP。这些 loop 被加入 `BoxedLoopsSet`。

> 名词解释：**box / over-approximation**——把一段无法精确建模的控制流，保守地当作「在这些迭代里，内部任何路径都可能发生」来处理。它牺牲精度（损失部分优化机会）换取「能进入 Polly」的机会。

#### 4.4.2 核心流程

```
任意 isValid* 失败
 └── invalid<ReportXxx>(Context, Assert, args...)
       ├── 非 verify 模式：构造 ReportXxx，记入 Context.Log，置 IsInvalid=true，返回 false
       └── verify 模式：assert(!Assert)

非仿射分支/循环且开启 Allow*（在 isValidBranch / isValidLoop / isValidAccess 里）
 └── addOverApproximatedRegion(RI.getRegionFor(BB), Context)
       ├── 该子区域加入 NonAffineSubRegionSet
       ├── 区域内所有循环加入 BoxedLoopsSet
       └── 返回 true（放行，但精度降低）
```

#### 4.4.3 源码精读

`invalid<>` 模板——所有拒绝的统一出口：

[lib/Analysis/ScopDetection.cpp:394-413](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Analysis/ScopDetection.cpp#L394-L413) ——

```cpp
template <class RR, typename... Args>
inline bool ScopDetection::invalid(DetectionContext &Context, bool Assert,
                                   Args &&...Arguments) const {
  if (!Context.Verifying) {
    RejectLog &Log = Context.Log;
    std::shared_ptr<RR> RejectReason = std::make_shared<RR>(Arguments...);
    Context.IsInvalid = true;
    Log.report(RejectReason);                       // ← 记录拒绝原因
    POLLY_DEBUG(dbgs() << RejectReason->getMessage());
  } else {
    assert(!Assert && "Verification of detected scop failed");
  }
  return false;
}
```

要点：

- **统一返回 false**：调用处可直接 `return invalid<ReportXxx>(...)`，省去重复的 `return false`。
- **`Verifying` 分支**：`verifyAnalysis` 会以 verify 模式重跑检测，此时不再记日志，而是用 `assert` 保证已检测的 SCoP 仍合法（`-polly-detect-verify` 触发）。
- **`Report*` 类层次**：拒绝原因是一个小类型系统，基类 `RejectReason`，聚合类如 `ReportCFG`（[include/polly/ScopDetectionDiagnostic.h:197](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/include/polly/ScopDetectionDiagnostic.h#L197)）、`ReportAffFunc`（[:312](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/include/polly/ScopDetectionDiagnostic.h#L312)）、`ReportOther`（[:738](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/include/polly/ScopDetectionDiagnostic.h#L738)）。本讲涉及的叶子类包括 `ReportNonAffBranch`（[:404](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/include/polly/ScopDetectionDiagnostic.h#L404)）、`ReportNonAffineAccess`（[:503](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/include/polly/ScopDetectionDiagnostic.h#L503)）、`ReportLoopBound`（[:559](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/include/polly/ScopDetectionDiagnostic.h#L559)）、`ReportLoopHasMultipleExits`（[:619](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/include/polly/ScopDetectionDiagnostic.h#L619)）、`ReportFuncCall`（[:675](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/include/polly/ScopDetectionDiagnostic.h#L675)）、`ReportAlias`（[:699](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/include/polly/ScopDetectionDiagnostic.h#L699)）、`ReportVariantBasePtr`（[:478](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/include/polly/ScopDetectionDiagnostic.h#L478)）、`ReportIrreducibleRegion`（[:232](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/include/polly/ScopDetectionDiagnostic.h#L232)）。这套分类直接对应你在 `-polly-print-detect` / opt-viewer 里看到的原因名。

非仿 Region 兜底 `addOverApproximatedRegion`：

[lib/Analysis/ScopDetection.cpp:450-466](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Analysis/ScopDetection.cpp#L450-L466) ——

```cpp
bool ScopDetection::addOverApproximatedRegion(Region *AR, DetectionContext &Context) const {
  if (!Context.NonAffineSubRegionSet.insert(AR))
    return true;                          // 已知该 box，直接成功
  for (BasicBlock *BB : AR->blocks()) {
    Loop *L = LI.getLoopFor(BB);
    if (AR->contains(L))
      Context.BoxedLoopsSet.insert(L);    // 区域内循环统统 box
  }
  return (AllowNonAffineSubLoops || Context.BoxedLoopsSet.empty());
}
```

它被三处调用兜底：`isValidBranch`（非仿射条件分支，[:643](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Analysis/ScopDetection.cpp#L643)）、`isValidSwitch`（[:566](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Analysis/ScopDetection.cpp#L566)）、`isValidLoop`（[:1369](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Analysis/ScopDetection.cpp#L1369)）。注意最后一行的含义：**box 一个含循环的区域，必须同时开 `AllowNonAffineSubLoops`**（即 `-polly-allow-nonaffine-loops`），否则不允许把循环 box 进去——这就是 4.2.4 实践里 `-polly-allow-nonaffine-loops` 开关的决定性作用。

#### 4.4.4 代码实践

**目标**：用 `-polly-detect-keep-going` 一次性看清一个函数里**所有**被拒区域的原因，体会 `RejectLog` 的价值。

**操作步骤**：

1. 写一段「混合」函数，含多种问题（示例代码）：

   ```c
   // 示例代码：mixed.c
   extern int unknown(int);            // 非纯函数
   void mixed(int *A, int *B, int N) {
     for (int i = 0; i < N; i++) {
       A[i] = unknown(i);              // 非纯调用 → ReportFuncCall
     }
     for (int i = 0; i < N; i++) {
       A[B[i]] = i;                    // 非仿射下标 → ReportNonAffineAccess
     }
   }
   ```

2. 生成 IR 并加 `-polly-detect-keep-going`：

   ```bash
   clang -O1 -Xclang -disable-llvm-passes -emit-llvm -S mixed.c -o mixed.ll
   opt -load-pass-plugin=LLVMPolly.so -aa-pipeline=basic-aa \
       -passes='polly-custom<detect>' -polly-print-detect \
       -polly-detect-keep-going -disable-output mixed.ll 2>&1
   ```

3. 对照源码：逐个循环推断它会被哪个 `Report*` 拒绝，再与输出/remark 比对。

**需要观察的现象**：开启 `keep-going` 后，两个循环各自独立的拒绝原因都会被记录（而非只看到第一个就停）。

**预期结果**：第一个循环因 `unknown` 非纯调用被 `ReportFuncCall` 拒；第二个循环因 `A[B[i]]` 非仿射下标被 `ReportNonAffineAccess` 拒。

**待本地验证**：具体 remark 文本与是否随 LLVM 版本变化，请以本地输出为准；也可用 `-polly-process-unprofitable` 排除盈利性干扰。

#### 4.4.5 小练习与答案

**练习 1**：`invalid<RR>()` 在 `Verifying=true` 时的行为与平时有何不同？为什么需要这种不同？

> **参考答案**：verify 模式下不写 `RejectLog`、不置 `IsInvalid`，而是 `assert(!Assert)`。因为 verify 是在变换**之后**重跑检测以确认 SCoP 仍合法，此时若失败属于「编译器内部一致性错误」，应用 assert 报错而非静默记日志。

**练习 2**：`addOverApproximatedRegion` 末尾为什么要求 `AllowNonAffineSubLoops || BoxedLoopsSet.empty()`？

> **参考答案**：若一个被 box 的非仿 Region **内部含循环**，则这些循环会被加入 `BoxedLoopsSet`。把循环 box 掉意味着放弃精确建模其迭代次数，损失更大，故需要用户显式开 `-polly-allow-nonaffine-loops`（`AllowNonAffineSubLoops`）才允许；若 box 区域内无循环（`BoxedLoopsSet.empty()`），仅 box 一个分支条件，代价小，默认即可。

**练习 3**：一个用户抱怨「我的循环没被 Polly 优化」，列出你会建议他依次检查的三个开关/工具。

> **参考答案**：(1) 先用 `-passes='polly-custom<detect>' -polly-print-detect` 确认是否被检测为 SCoP；(2) 若没检测到，开 `-polly-detect-keep-going` 看拒绝原因（`Report*`）；(3) 根据原因对症：非纯调用考虑 `-polly-allow-modref-calls`、非仿射循环考虑 `-polly-allow-nonaffine-loops`、不变量 load 考虑 `-polly-invariant-load-hoisting`、整函数考虑 `-polly-detect-full-functions`。

---

## 5. 综合实践

把本讲四条线索串起来：**给定一段 IR，预测并验证它能否成为 SCoP，再为每个被拒区域定位到精确的 `isValid*` 判定点。**

**任务**：分析下面这段 C 代码（示例代码），它故意揉进了控制流、调用、访存、别名四类问题。

```c
// 示例代码：final.c
extern int side_effect(int);           // 非纯
void final(int *A, int *B, int *C, int N, int M) {
  // 循环 1：仿射、纯访存 —— 期望被检测
  for (int i = 0; i < N; i++)
    A[i] = B[i] + C[i];

  // 循环 2：含 break（多出口）
  for (int i = 0; i < N; i++) {
    if (A[i] == 0) break;              // 第二个 exit block
    B[i] = A[i] * 2;
  }

  // 循环 3：非纯调用
  for (int i = 0; i < M; i++)
    C[i] = side_effect(i);

  // 循环 4：非仿射下标 + A/C 可能别名
  for (int i = 0; i < M; i++)
    A[C[i]] += B[i];
}
```

**步骤**：

1. 生成 IR：`clang -O1 -Xclang -disable-llvm-passes -emit-llvm -S final.c -o final.ll`。
2. 检测并打印：`opt -load-pass-plugin=LLVMPolly.so -passes='polly-custom<detect>' -polly-print-detect -disable-output final.ll`。
3. 对每个循环填表（先预测，后用 `-polly-detect-keep-going` 验证）：

| 循环 | 预测判定函数 | 预测 `Report*` | 验证结果 |
|------|--------------|----------------|----------|
| 1 | （应通过） | — | 待本地验证 |
| 2 | `isValidLoop` | `ReportLoopHasMultipleExits` | 待本地验证 |
| 3 | `isValidInstruction`→`isValidCallInst` | `ReportFuncCall` | 待本地验证 |
| 4 | `isValidAccess` | `ReportNonAffineAccess` / `ReportAlias` | 待本地验证 |

4. 选一个被拒循环，尝试用对应开关「救回」（如循环 3 加 `-polly-allow-modref-calls` 但需 `side_effect` 有合适属性；循环 4 加 `-polly-allow-nonaffine`），观察检测输出变化，并解释精度损失。

**预期结果**：循环 1 被检测为合法 SCoP；循环 2/3/4 分别因多出口、非纯调用、非仿射下标/别名被拒，且拒绝点可精确对应到本讲讲解的 `isValid*` 函数。

## 6. 本讲小结

- SCoP 检测是一棵**分层判定树**：`detect` → `findScops`（试—下降—扩展）→ `isValidRegion` → `allBlocksValid`，分派到「循环 → CFG → 指令 → 访存」四层 `isValid*`。
- **控制流合法性**（`isValidCFG`/`isValidBranch`/`isValidSwitch`/`isValidLoop`）要求：条件是仿射整数比较、循环单出口且回边计数可被 ISL 计算、整个区域可归约（DFS 三色染色 + 支配判定）。
- **别名与基址分析**（`isValidInstruction`/`isValidMemoryAccess`/`isValidAccess`）要求：基址在区域内不变、偏移仿射（多维访问靠 `-polly-delinearize` 救回）、别名能靠运行时检查或 must-alias 解决。
- 所有失败统一走 `invalid<ReportXxx>()`，记入 `RejectLog`，是 `-polly-print-detect`、opt-viewer、`regionIsInvalidBecause` 的数据来源；`-polly-detect-keep-going` 收集全部原因。
- **非仿 Region 兜底（box）** 是逃生舱：开启 `Allow*` 系列开关后，非法分支/循环/访问不直接拒，而是把子区域整体过近似（循环进 `BoxedLoopsSet`），牺牲精度换取进入 Polly 的机会。
- 间接调用（`indirectbr`/`callbr`/`getCalledFunction()==null`）、区域内 `alloca`、volatile/atomic 访存、`IntToPtr`、可变长向量类型等都会被明确拒绝。

## 7. 下一步学习建议

- 想更系统地利用本讲的拒绝诊断排查问题？继续 [u3-l3 检测诊断与 CFG 图示](u3-l3-diagnostics-and-graph.md)，讲 `OptimizationRemark`、`-polly-dot`/`-polly-show` 如何把 `RejectLog` 可视化。
- 检测只是「能不能」。想看一个合法区域如何被构造成真正的多面体对象（Domain/Schedule/Access Relation），进入 U4，从 [u4-l1 Scop/ScopStmt/MemoryAccess 核心数据结构](u4-l1-scop-core-data-structures.md) 开始。
- 对本讲反复出现的 SCEV 仿射判定（`isAffine`/`isAffineExpr`）想追根溯源，看 [u4-l4 SCEV 到 ISL：SCEVAffinator 与 SCEVValidator](u4-l4-scev-to-isl.md)。
- 想直接读真实拒绝案例的测试，浏览 [test/ScopDetect/](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/test/ScopDetect) 目录下 76 个 `.ll` 文件，每个都对应一种被拒或被放行的情形。
