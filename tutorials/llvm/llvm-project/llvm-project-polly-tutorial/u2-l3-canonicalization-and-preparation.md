# 规范化与代码准备阶段

## 1. 本讲目标

学完本讲，你应当能够：

- 解释**为什么 Polly 必须吃规范化后的 IR**——说清 SCEV、SCoP 检测对 IR 形态的硬性要求。
- 逐条读懂 [`buildCanonicalicationPassesForNPM`](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Transform/Canonicalization.cpp#L71-L108) 注册的那一串规范化 pass（mem2reg、IndVarSimplify、LoopRotate……），并说出每个 pass 解决了什么阻碍检测的问题。
- 讲清 [`runCodePreparation`](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Transform/CodePreparation.cpp#L47-L50) 这个 `prepare` 阶段唯一做的事（拆分入口块），以及它为什么对后续代码生成必要。
- 看懂 `prepare` 阶段如何**保持** `DominatorTree`/`LoopInfo`，又为什么 `RegionInfo` 必须**重算**而不能缓存。
- 辨别一个重要的源码事实：`-polly-canonicalize` 作为独立 pass **已从代码中移除**（文档残留），并会用等价的、确实可用的命令完成同样的对比实践。

> 本讲是 u2-l1 流水线全景里 `prepare` 那一格的放大镜。它回答的是「在 `ScopDetection` 跑起来之前，IR 被塑造成了什么样子」。

## 2. 前置知识

本讲承接 u2-l1（PhaseManager 流水线）与 u1-l4（early / before-vectorizer 两个挂载点），默认你已经知道：

- **`PassPhase::Prepare`**：流水线里第一个阶段，函数级、整个函数跑一次。u2-l1 已指出它是仅有的两个「真正改写 LLVM-IR」的阶段之一（另一个是 `codegen`）。
- **SCEV（ScalarEvolution）**：LLVM 的「标量演化」分析，能把循环里递增的变量归纳成数学表达式。Polly 靠它把循环边界和数组下标翻译成仿射式（见 u4-l4）。
- **仿射（affine）**：形如 \(a_1 x_1 + a_2 x_2 + \dots + c\) 的「线性加常数」。一段循环能否被 Polly 处理，前提就是它的控制流和访存都能用仿射式描述（u1-l1、u3-l1）。
- **early 与 before-vectorizer 的差异**：early 位置在整条 `-O3` 流水线最前面，「前面啥都没有」，所以 Polly 得自己先跑一整套规范化；before-vectorizer 位置在向量器之前，LLVM 的 `-O3` 已经把绝大多数规范化做完了。

本讲新增两个最小模块：**LLVM 规范化 pass**（Polly 挑了哪几个、为什么挑这几个）与 **RegionInfo/LoopInfo/DominatorTree**（prepare 阶段怎么维护这三套结构分析）。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [lib/Transform/Canonicalization.cpp](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Transform/Canonicalization.cpp) | `buildCanonicalicationPassesForNPM()`：组装「Polly 专用规范化」的 FunctionPassManager。本讲的主体。 |
| [include/polly/Canonicalization.h](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/include/polly/Canonicalization.h) | 上面函数的声明，注释说明「这组 pass 部分取自 LLVM 默认优化流水线」。 |
| [lib/Transform/CodePreparation.cpp](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Transform/CodePreparation.cpp) | `runCodePreparation()`：拆分入口块，为代码生成预留 alloca 空间。 |
| [include/polly/CodePreparation.h](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/include/polly/CodePreparation.h) | `runCodePreparation` 声明，参数暴露它依赖 DT/LI/RI。 |
| [lib/Pass/PhaseManager.cpp](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Pass/PhaseManager.cpp) | `prepare` 阶段的调用点与 DT/LI/RI 的失效/保持策略。 |
| [lib/Support/RegisterPasses.cpp](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Support/RegisterPasses.cpp) | `buildEarlyPollyPipeline` / `buildLatePollyPipeline`：只有 early 位置才挂上规范化 pass。 |
| [lib/Support/ScopHelper.cpp](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Support/ScopHelper.cpp) | `splitEntryBlockForAlloca()`：prepare 阶段实际调用的拆块工具函数。 |

---

## 4. 核心概念与源码讲解

### 4.1 LLVM 规范化 pass

#### 4.1.1 概念说明

Polly 的检测器 `ScopDetection` 要做的事情非常苛刻：它必须把一段循环的**迭代空间**和**数组访问**都表达成仿射函数。比如一个双重循环：

\[ \mathcal{D} = \{\, (i,j) \mid 0 \le i < N \;\land\; 0 \le j < M \,\} \]

要让这个集合成立，循环变量 `i` 必须是一个「干净的」归纳变量（induction variable）：从某常数起步、每步加常数、只受一个上界约束。但 clang 在 `-O0` 下吐出的 IR 远不是这个样子——变量可能躺在栈槽里（alloca/load/store）、循环可能不是规范形式、冗余指令到处都是。**规范化（canonicalization）就是把 IR 整理成「Polly 与 SCEV 能看懂的标准形态」的一组清理 pass。**

关键直觉是：规范化是 SCoP 检测的**前置必要条件**，不是可选优化。没有 mem2reg，Polly 看到的是一堆对栈槽的内存访问，根本无从分析标量演化；没有 IndVarSimplify，循环可能用各种奇怪的方式递增计数器，凑不出仿射边界。

> 这组 pass 的设计来源在头文件注释里写得很直白：「部分取自/拷贝自 LLVM 的默认优化流水线」，目的是「把代码带进一个简化分析与优化的规范形态」——见 [include/polly/Canonicalization.h:16-22](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/include/polly/Canonicalization.h#L16-L22)。

#### 4.1.2 核心流程

规范化的整体流向是「把原始 IR 一层层拍成 Polly 友好的形态」：

```
原始 IR（-O0，满是栈槽/非规范循环）
   │  PromotePass (mem2reg)        → 栈槽提升为 SSA 寄存器
   │  EarlyCSEPass                 → 删冗余 load / 简单公共子表达式
   │  InstCombinePass              → 指令合并成规范形式
   │  SimplifyCFGPass              → 简化控制流图
   │  TailCallElimPass + SimplifyCFG
   │  ReassociatePass              → 重排操作数，利于常量折叠/CSE
   │  LoopRotatePass               → 循环旋转（条件前置）
   │  InstCombinePass（再来一轮）
   │  IndVarSimplifyPass           → 规范化归纳变量（Polly 最关键的一步）
   ▼
规范形态 IR：SSA、规范 IV、规整循环 → 可被 ScopDetection 接受
```

这条流水线**只挂在 early 位置**。在默认的 `before-vectorizer` 位置，LLVM 自己的 `-O3` 已经跑过等价甚至更多的规范化，Polly 不必重复——这就是 u1-l4 里「early 需要自己跑规范化、before-vectorizer 不需要」的根因。

#### 4.1.3 源码精读：规范化 pass 清单

整组 pass 由一个工厂函数装配，返回一个 `FunctionPassManager`：

[lib/Transform/Canonicalization.cpp:71-108](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Transform/Canonicalization.cpp#L71-L108) —— `buildCanonicalicationPassesForNPM`，按固定顺序 `addPass` 下列 pass。下表逐条说明每个 pass 对 SCoP 检测的意义：

| 代码行 | pass | 解决什么问题 |
| --- | --- | --- |
| [L77](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Transform/Canonicalization.cpp#L77) | `PromotePass()`（即 mem2reg） | 把 alloca/load/store 提升为 SSA 寄存器。**没有它，SCEV 看到的是不透明的内存访问，无法演化标量。** |
| [L78](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Transform/Canonicalization.cpp#L78) | `EarlyCSEPass(UseMemSSA=true)` | 删除冗余的 load 与简单公共子表达式，减少噪声指令。 |
| [L79](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Transform/Canonicalization.cpp#L79) / [L99](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Transform/Canonicalization.cpp#L99) | `InstCombinePass()`（跑两轮） | 把指令合并成规范算术形式，让下标表达式更可能成为仿射。 |
| [L80](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Transform/Canonicalization.cpp#L80) / [L82](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Transform/Canonicalization.cpp#L82) | `SimplifyCFGPass()`（跑两轮） | 合并/简化基本块，使控制流更规整、利于构成 Region。 |
| [L81](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Transform/Canonicalization.cpp#L81) | `TailCallElimPass()` | 尾递归消除。 |
| [L83](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Transform/Canonicalization.cpp#L83) | `ReassociatePass()` | 重排操作数（如把常量凑到一起），利于常量折叠与 CSE。 |
| [L84-L89](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Transform/Canonicalization.cpp#L84-L89) | `LoopRotatePass()`（包在 loop 适配器里） | 循环旋转：把循环体末尾的条件检查搬到开头，形成「guard + 单一回边」的规范结构。 |
| [L100-L105](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Transform/Canonicalization.cpp#L100-L105) | `IndVarSimplifyPass()`（包在 loop 适配器里） | **Polly 最依赖的一步**：把循环归纳变量规范化为单一、单调、常量步长的 IV，使迭代域可写成仿射集合。 |

注意两个循环 pass（`LoopRotate`、`IndVarSimplify`）都用 `createFunctionToLoopPassAdaptor` 包了一层，且显式传 `UseMemorySSA=false`——它们不需要 MemorySSA。

中间还有一个**可选的内联块**，受隐藏开关 `-polly-run-inliner` 控制（默认关）：

[lib/Transform/Canonicalization.cpp:36-39](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Transform/Canonicalization.cpp#L36-L39) —— `PollyInliner` 这个 `cl::opt`，配合 [L90-L98](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Transform/Canonicalization.cpp#L90-L98) 的条件分支：开启时，先把已装配的 FPM 跑一遍，再调用 [`buildInlinePasses`](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Transform/Canonicalization.cpp#L42-L69) 跑一个早期内联器，然后重新建一个 FPM 继续。这是给「希望 early 位置也能享受到一点内联红利」的高级用户留的口子。

**谁调用这个工厂函数？** 只有 early 位置：

[lib/Support/RegisterPasses.cpp:490](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Support/RegisterPasses.cpp#L490) —— `buildEarlyPollyPipeline` 第一行就调用 `buildCanonicalicationPassesForNPM(MPM, Level)`，把规范化 FPM 挂在 Polly 流水线最前面。对比 [`buildLatePollyPipeline`](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Support/RegisterPasses.cpp#L512-L529)（before-vectorizer 位置），它**完全没有**这步调用——直接 `buildCommonPollyPipeline`。这行代码差异就是两个位置规范化行为的全部来源。

#### 4.1.4 代码实践：观察规范化的前后差异

> ⚠️ **重要事实校正**：实践任务原本写的是 `opt -polly-canonicalize`。但这个 pass **已经从当前代码中移除**——它曾是旧版 Pass Manager（Legacy PM）时代的独立 pass，随提交 `7a0f7dbf2dcc [Polly] Introduce PhaseManager and remove LPM support` 一并删除。在当前 HEAD 中，`lib/` 与 `include/` 里已找不到任何 `-polly-canonicalize` 的注册（没有 `INITIALIZE_PASS`、不在 `PollyPasses.def`、无测试引用），只剩 `docs/` 下若干 `.rst` 与 `www/documentation/passes.html` 还残留旧名（文档滞后）。**直接跑 `opt -polly-canonicalize` 会报 unknown pass。** 下面给出确实可用的等价做法。

**实践目标**：拿一段原始 `-O0` IR，跑等价于 `buildCanonicalicationPassesForNPM` 的规范化，对比前后并找出至少 3 处对 SCoP 检测有利的改动。

**方法 A（推荐，最贴近真实流程）**：让 early 位置的 Polly 替你跑这组规范化，再用 dump 抓出「Polly 实际看到的 IR」。

```bash
# 1) 准备一段含嵌套循环的 C 代码（如矩阵乘），存为 matmul.c
# 2) 抓「Polly 看到的 IR」（early 位置会隐式跑 buildCanonicalicationPassesForNPM）
clang matmul.c -c -O3 -mllvm -polly -mllvm -polly-position=early \
      -mllvm -polly-dump-before-file=before-polly.ll -emit-llvm -S -o /dev/null
# before-polly.ll 即规范化后的 IR
```

文档 [docs/UsingPollyWithClang.rst:146-152](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/docs/UsingPollyWithClang.rst#L146-L152) 也确认了 early 位置会「隐式跑 `-polly-canonicalize` 那组 pass」。

**方法 B（手动跑等价 NPM 流水线，便于精确对比）**：把 [4.1.3](#413-源码精读规范化-pass-清单) 表里的 pass 逐个翻译成 New Pass Manager 文本，得到与源码等价的命令：

```bash
# 1) 生成原始 -O0 IR（关闭 optnone，否则多数 pass 会跳过该函数）
clang matmul.c -c -O0 -Xclang -disable-O0-optnone -emit-llvm -S -o matmul.raw.ll

# 2) 跑等价规范化（pass 名与 4.1.3 表一一对应）
opt -passes='function(mem2reg,early-cse<memssa>,instcombine,simplifycfg,tailcallelim,simplifycfg,reassociate,loop-rotate,instcombine,indvars)' \
    -S matmul.raw.ll -o matmul.canon.ll
```

> 上述 pass 串是对源码的**等价翻译**；个别 NPM 文本语法（如 loop pass 是否需显式 `loop(...)` 包裹）依 opt 版本而异，**若报错请以本地 `opt --help-passes` 列出的名字为准，属待本地验证项**。

**需要观察的现象 / 预期结果**：用 `diff matmul.raw.ll matmul.canon.ll`（或方法 A 的 `before-polly.ll`）对比，至少能指出这 3 类对检测有利的改动：

1. **栈槽消失**：`alloca`/`load`/`store` 大量减少，变量变成 SSA `%name = ...`——这是 `mem2reg` 的功劳，让 SCEV 能演化这些标量。
2. **归纳变量被规范**：循环计数器变成单一的 `%indvar = phi ..., %indvar.next`，步长为常量——这是 `IndVarSimplify` 的结果，使迭代域能写成 \(\{\,i \mid 0 \le i < N\,\}\)。
3. **循环结构变规整**：循环被旋转、基本块被合并，控制流出现清晰的 guard + 单回边形态——利于 `ScopDetection` 把它识别成一个 Region。

可选验证：对规范化前后的 IR 分别跑 `-polly-print-detect`，预期规范化后能检测到 SCoP、规范化前检测不到（或被拒）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `PromotePass`（mem2reg）排在规范化流水线的第一个？如果把它放到 `IndVarSimplifyPass` 之后会发生什么？

**参考答案**：mem2reg 把栈槽提升为 SSA 寄存器，是后续所有「按值分析」的前提。`IndVarSimplify` 要识别归纳变量，得先看到寄存器里的 `phi`，而不是一堆对同一 alloca 的 load/store。若反过来，IndVarSimplify 看不到干净的 phi，规范化归纳变量这一步基本失效。

**练习 2**：`buildCanonicalicationPassesForNPM` 里两个循环 pass 都传了 `UseMemorySSA=false`，为什么 Polly 这里不需要 MemorySSA？

**参考答案**：这组 pass 只做形态整理（旋转、归纳变量规范化），不依赖基于 MemorySSA 的消歧；关掉它能避免无谓地计算与维护 MemorySSA，降低编译开销。真正需要精确别名/消歧信息的是检测与变换阶段（用 `AAResults`），不在这组规范化 pass 里。

---

### 4.2 CodePreparation：入口块拆分

#### 4.2.1 概念说明

`buildCanonicalicationPassesForNPM` 解决的是「IR 形态」问题；而 `prepare` 阶段（`PassPhase::Prepare`）做的是另一件更具体的事——**拆分函数入口块**。它由自由函数 [`runCodePreparation`](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/include/polly/CodePreparation.h#L24-L25) 实现，被 `PhaseManager::run()` 在检测之前调用。

为什么需要拆入口块？因为代码生成阶段（u8-l2）会把一些标量值「物化」成栈上的 `alloca`。LLVM 规定 `alloca` 指令通常应放在入口基本块、且要支配所有使用点。如果入口块里同时混着原始代码的指令，新生成的 alloca 就会和 SCoP 区域搅在一起，污染检测/生成的区域边界。**把入口块在「已有 alloca 之后、其余指令之前」切开**，就能让后续插入的 alloca 安稳待在前半段，而后半段成为一个干净的、不含 alloca 的代码起点。

> 源码注释很坦诚：这个 pass「目前只是拆分入口块为 alloca 腾地方」，并标注 `XXX: 未来应移除这个 pass，把拆分并进代码生成阶段`——见 [lib/Transform/CodePreparation.cpp:9-15](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Transform/CodePreparation.cpp#L9-L15)。

#### 4.2.2 核心流程

```
runCodePreparation(F, DT, LI, RI)
  │
  ├─ 从入口块开头向后扫，跳过所有 AllocaInst，停在「第一条非 alloca 指令」I
  │
  ├─ 若 I 已经是 UncondBrInst（入口块只剩 alloca + 一条无条件跳转）
  │     → 无需拆分，return false
  │
  └─ 否则：splitEntryBlockForAlloca(...)  ← 在 I 处把入口块一分为二
        splitBlock 内部同步更新 DT、LI、RI
        return true（表示改了 IR）
```

注意：拆分点不是「入口块开头」，而是「跳过已有 alloca 之后」。这样原有 alloca 留在前块，其余指令整体搬到新块，新生成的 alloca 也能插进前块。

#### 4.2.3 源码精读：prepare 的全部实现

prepare 阶段的逻辑非常短，全部在这里：

[lib/Transform/CodePreparation.cpp:28-45](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Transform/CodePreparation.cpp#L28-L45) —— `runCodePreprationImpl`（注：源码函数名是这个拼写）。关键三步：

```cpp
auto &EntryBlock = F.getEntryBlock();
BasicBlock::iterator I = EntryBlock.begin();
while (isa<AllocaInst>(I))   // 跳过所有 alloca
  ++I;
if (isa<UncondBrInst>(I))    // 已经是「alloca + 无条件跳转」就无需拆
  return false;
splitEntryBlockForAlloca(&EntryBlock, DT, LI, RI);  // 拆，并更新 DT/LI/RI
```

实际拆块的工具函数在 Support 层：

[lib/Support/ScopHelper.cpp:197-207](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Support/ScopHelper.cpp#L197-L207) —— `splitEntryBlockForAlloca`，注释明确「`splitBlock` 会更新 DT、LI、RI」（[L205](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Support/ScopHelper.cpp#L205)），底层是 LLVM 的 `BasicBlock::splitBasicBlock` 的 Polly 封装版本，签名见 [include/polly/Support/ScopHelper.h:368-370](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/include/polly/Support/ScopHelper.h#L368-L370)。

`runCodePreparation` 只是对它的转发：

[lib/Transform/CodePreparation.cpp:47-50](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Transform/CodePreparation.cpp#L47-L50) —— 一行 `return runCodePreprationImpl(...)`。注意它**不接收 `RegionInfo` 的有效指针**——调用方在 PhaseManager 里传的是 `nullptr`（见下节）。

#### 4.2.4 代码实践：看 prepare 是否真的拆了块

**实践目标**：验证 `prepare` 阶段会在「入口块含有 alloca 之后的指令」时把它一分为二。

**操作步骤**：

1. 写一个含局部数组（会生成 alloca）且有循环的 C 函数，用方法 4.1.4-B 生成 `matmul.canon.ll`。
2. 用 early 位置跑 Polly 并分别 dump prepare 前后的 IR（`-polly-dump-before-file` 抓的是规范化后、Polly 流水线开始前的 IR；要精确看 prepare 单步效果，更直接的办法是读测试或加日志）。
3. 观察入口基本块：prepare 前，入口块里 `alloca` 之后还跟着别的工作指令；prepare 后，入口块应只剩 alloca 与一条无条件 `br label`，跳到一个新块。

**预期结果**：入口块被切成两段，原 alloca 全在前段，前段以一条无条件跳转结尾（满足 `runCodePreprationImpl` 里「拆完后再跑一次会命中 `isa<UncondBrInst>(I)` 而 return false」的幂等性）。

> 若本地难以隔离 prepare 单步输出，可采用「源码阅读型实践」：在 [CodePreparation.cpp:42](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Transform/CodePreparation.cpp#L42) 的 `splitEntryBlockForAlloca` 调用前后推演基本块结构，并说明为何第二次调用同样函数会返回 `false`（幂等）。

#### 4.2.5 小练习与答案

**练习 1**：如果函数入口块本身就已经是「只有若干 alloca + 一条无条件跳转」，`runCodePreparation` 会做什么？为什么这样设计？

**参考答案**：它命中 `isa<UncondBrInst>(I)` 分支，直接 `return false`，不拆分、不改 IR。这样设计既幂等（重复跑 prepare 不会反复拆出空块），也避免无意义的块拆分破坏已有结构。

**练习 2**：为什么拆分点定在「跳过 alloca 之后」而不是「入口块最开头」？

**参考答案**：已有的 alloca 必须留在入口块（它们要支配后续所有使用），只有「alloca 之后」的指令才能搬走。从开头拆会把 alloca 一起搬到新块，破坏 alloca 的支配性与栈对象布局。

---

### 4.3 分析保持：DT / LI / RI 在 prepare 阶段如何维护

#### 4.3.1 概念说明

拆块会改变 CFG，于是依附于 CFG 的三套结构分析——`DominatorTree`（支配树，DT）、`LoopInfo`（LI）、`RegionInfo`（RI）——都可能失效。但 u2-l1 强调过：`PhaseManager` 在所有阶段间必须**手动保持** `LoopInfo`/`DominatorTree`/`ScopInfo` 全程有效，因为 `ScopDetection` 会缓存对旧分析结果的引用，重算会导致悬挂引用。

`prepare` 阶段是这套「保持」策略的第一个实战场景，它的做法是**分别对待**：DT 和 LI 显式保持（preserve），RI 干脆不缓存、用的时候重算。

#### 4.3.2 核心流程

```
PhaseManager::run()
  │
  ├─ 先从 FAM 取出 LoopInfo、DominatorTree（全程持有，不重算）
  │
  ├─ prepare 阶段：
  │     runCodePreparation(...)  ← 内部 splitBlock 已更新 DT/LI/RI 的内部状态
  │     if (改了 IR) {
  │       PreservedAnalyses PA;
  │       PA.preserve<DominatorTreeAnalysis>();   // 告诉 FAM：DT 仍有效
  │       PA.preserve<LoopAnalysis>();            // 告诉 FAM：LI 仍有效
  │       FAM.invalidate(F, PA);                 // 其余分析失效，但 DT/LI 不动
  │     }
  │
  └─ 进入 detection 阶段前：
        RegionInfo RI = RegionInfoAnalysis().run(F, FAM);  // 现算一份，不缓存
        // 注释说明：ScopDetection 会修改 RegionInfo，所以不缓存
```

核心区分：DT/LI 是「改了之后仍准确、且后续阶段离不开」的，所以**保持**；RI 因为检测阶段自己会改它，所以**每次现算一份**，避免拿到脏缓存。

#### 4.3.3 源码精读：保持策略

调用点与保持逻辑全在 `PhaseManager::run()` 开头：

[lib/Pass/PhaseManager.cpp:74-89](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Pass/PhaseManager.cpp#L74-L89) —— 先取 `LI`/`DT`（[L74-L75](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Pass/PhaseManager.cpp#L74-L75)），prepare 调用 `runCodePreparation(F, &DT, &LI, nullptr)`（[L82](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Pass/PhaseManager.cpp#L82)，注意第 4 个参数 `RegionInfo*` 传的是 `nullptr`），随后：

```cpp
PreservedAnalyses PA;
PA.preserve<DominatorTreeAnalysis>();
PA.preserve<LoopAnalysis>();
FAM.invalidate(F, PA);
ModifiedIR = true;
```

`FAM.invalidate(F, PA)` 的语义是「按 PA 描述失效分析」：`PA` 里只 preserve 了 DT 和 LI，其余全失效。这与 `splitEntryBlockForAlloca` 内部「splitBlock 已同步更新 DT/LI/RI」相呼应——DT/LI 的内存对象被原地更新为新的正确状态，所以标记 preserve 是合法的。

RI 的「不缓存」策略，紧接着体现于检测阶段：

[lib/Pass/PhaseManager.cpp:100-105](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Pass/PhaseManager.cpp#L100-L105) —— 注释 [L100-L101](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Pass/PhaseManager.cpp#L100-L101) 直说「ScopDetection 会修改 RegionInfo，所以不缓存、也不用缓存版本」，于是 `RegionInfo RI = RegionInfoAnalysis().run(F, FAM);` 现算一份新的，再传给 `ScopDetection`。

源码里还有一条值得注意的 TODO，解释了为什么 `runCodePreparation` 的参数里其实并不需要这些分析：

[lib/Pass/PhaseManager.cpp:68-73](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Pass/PhaseManager.cpp#L68-L73) ——「这些分析必须在所有阶段间保持有效……CodePreparation 其实并不需要它们，只是顺带保持其最新；如果还没算，也可以在 prepare 之后再算」。

#### 4.3.4 代码实践：跟踪分析的有效性边界

**实践目标**：理解「为何 DT/LI 必须 preserve、而 RI 必须 recompute」，能从源码指认这条边界。

**操作步骤（源码阅读型）**：

1. 打开 [PhaseManager.cpp:78-89](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Pass/PhaseManager.cpp#L78-L89)，找出 prepare 之后被 `preserve` 的两类分析。
2. 跳到 [PhaseManager.cpp:100-106](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Pass/PhaseManager.cpp#L100-L106)，确认 `RegionInfo` 是用 `RegionInfoAnalysis().run(F, FAM)` 现算的，而不是从 FAM 缓存取。
3. 打开 [include/polly/ScopDetection.h](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/include/polly/ScopDetection.h)（u3-l1 详读），确认 `ScopDetection` 构造时按引用/指针持有 DT/SE/LI/RI/AA——这正是「重算会导致悬挂引用」的根因。

**预期结果**：能用自己的话解释——因为 `ScopDetection` 缓存了对 DT/LI 的引用，所以这俩必须原地保持有效；而 RI 由检测自己改写，索性每次重建。若把 RI 也 preserve，下一次跑会拿到被检测改过的脏 RegionInfo。

#### 4.3.5 小练习与答案

**练习 1**：`runCodePreparation` 被调用时 `RegionInfo*` 传的是 `nullptr`，但 [`splitEntryBlockForAlloca`](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Support/ScopHelper.cpp#L197-L207) 的实现里 `splitBlock` 仍接受 `RI`。传 `nullptr` 安全吗？

**参考答案**：安全。LLVM 的 `splitBlock` 对 `DT/LI/RI` 三个指针都做了空检查，传 `nullptr` 表示「我不关心这套分析、你别更新它」。这里传 `nullptr` 正是因为 RI 将在检测阶段整体重算，prepare 阶段没必要维护它。

**练习 2**：如果开发者在 prepare 之后忘记 `preserve<LoopAnalysis>()`，会发生什么？

**参考答案**：`FAM.invalidate` 会把 `LoopInfo` 当作失效，后续阶段再次取用时 FAM 会重算一份新的 `LoopInfo`。而 `ScopDetection` 内部持有的引用指向的是旧 `LoopInfo` 对象，从而产生悬挂引用/分析结果不一致，轻则检测出错，重则崩溃。这正是 u2-l1 强调「必须手动保持 LI/DT」的原因。

---

## 5. 综合实践：从原始 IR 到可检测 SCoP 的全程跟踪

把本讲三块知识串起来，完成一个端到端的小任务：

**任务**：取一段 clang `-O0` 直接吐出的「Polly 看不懂」的 IR，亲手把它推进到「ScopDetection 能接受」的形态，并在每一步对应到本讲的源码。

**步骤**：

1. 写一个矩阵乘 `matmul.c`，用 `clang -O0 -Xclang -disable-O0-optnone -emit-llvm -S` 生成 `matmul.raw.ll`。
2. 先对它跑一次 `-polly-print-detect`（通过 `opt -passes='polly'` 或文档推荐的逐步法），记录 SCoP 是否被检测到、被拒原因是什么。预期：很可能因为「栈槽 / 非规范 IV」被拒。
3. 跑 4.1.4 的等价规范化得到 `matmul.canon.ll`（等价于 `buildCanonicalicationPassesForNPM`）。
4. 模拟 `prepare`：检查入口块是否需要拆分（4.2 的判定），如需要则手工/借助 LLVM 工具拆分入口块。
5. 再对规范化+准备后的 IR 跑检测，对比第 2 步：SCoP 应被成功检测（或至少拒绝原因发生质变，从「IR 形态问题」变成「真正的仿射性问题」）。

**交付**：用一张表把每一步对应到源码位置——

| 步骤 | 对应源码 |
| --- | --- |
| 规范化 pass 清单 | [Canonicalization.cpp:71-108](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Transform/Canonicalization.cpp#L71-L108) |
| 仅 early 位置挂载规范化 | [RegisterPasses.cpp:490](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Support/RegisterPasses.cpp#L490) |
| prepare 拆入口块 | [CodePreparation.cpp:28-45](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Transform/CodePreparation.cpp#L28-L45) |
| DT/LI 保持、RI 重算 | [PhaseManager.cpp:74-106](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Pass/PhaseManager.cpp#L74-L106) |

> 本地若无 `clang`/`opt`，可降级为「源码阅读型」交付：写出第 2 步与第 5 步各自会命中 `ScopDetection` 哪条 `isValid*` 判定（见 u3-l2），并标「待本地验证」。

## 6. 本讲小结

- Polly 必须吃规范化 IR：检测器要靠 SCEV 把循环边界与下标表达成仿射式，而 `-O0` 原始 IR（栈槽、非规范 IV、冗余指令）根本无法被演化分析。
- `buildCanonicalicationPassesForNPM`（[Canonicalization.cpp:71-108](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Transform/Canonicalization.cpp#L71-L108)）装配了一串 pass，其中 `PromotePass`（mem2reg）与 `IndVarSimplifyPass` 对检测最关键。
- 这组规范化**只挂在 early 位置**（[RegisterPasses.cpp:490](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Support/RegisterPasses.cpp#L490)）；默认的 before-vectorizer 位置靠 LLVM `-O3` 已完成的规范化。
- `prepare` 阶段（[CodePreparation.cpp](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Transform/CodePreparation.cpp)）目前只做一件事：拆分入口块，为代码生成的 alloca 预留干净空间，且具备幂等性。
- 分析保持策略：DT/LI 在 prepare 后 `preserve`（因 `ScopDetection` 持有其引用），RI 不缓存、检测前重算（因检测会改写它）。
- **事实校正**：`-polly-canonicalize` 作为独立 pass 已随 LPM 移除（提交 `7a0f7dbf2dcc`），现行等价做法是 early 位置隐式跑、或手动跑 4.1.3 表中的 NPM pass 串；`docs/` 下的旧命令仅作参考。

## 7. 下一步学习建议

- 接下来读 **u3-l1（SCoP 概念与 ScopDetection 设计）**：本讲把 IR 整理成了「能被检测」的形态，下一讲就看检测器如何在这份规范化 IR 上圈出 SCoP，以及它为何要 DT/SE/LI/RI/AA 这一整套分析。
- 想理解 SCEV 如何把规范化后的下标变成 ISL 仿射式，可跳读 **u4-l4（SCEV 到 ISL）**，它会解释 `IndVarSimplify` 规范出的 IV 是怎样被 `SCEVAffinator` 翻译的。
- 若对 early/before-vectorizer 的取舍还想再巩固，可回看 **u1-l4** 的「两位置对比表」与本讲 4.1.3 的源码差异相互印证。
