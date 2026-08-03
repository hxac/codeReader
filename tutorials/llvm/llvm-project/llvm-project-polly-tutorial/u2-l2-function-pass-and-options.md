# PollyFunctionPass/ModulePass 与阶段选项解析

## 1. 本讲目标

上一篇（u2-l1）我们打开了 `PhaseManager::run()` 这只"黑盒"，看清了 Polly 内部 17 个实质阶段的执行顺序。本篇要回答它的**外层**两个问题：

1. 这整条阶段流水线，是怎样被"压"成**一个** LLVM Pass、挂进 New Pass Manager 的？——也就是 `PollyFunctionPass` / `PollyModulePass`。
2. 当你在命令行写 `-passes='polly<no-delicm;stopafter=ast>'` 时，这串文字是怎样变成"哪些阶段开、哪些阶段关"的？——也就是 `parsePollyOptions` 与 `PollyPassOptions` 位集。

学完本讲，你应当能够：

- 说清 `OptionalPassInfoMixin` 的作用，以及它为何让 Polly 通行证"可被跳过"。
- 画出从 `-passes='polly...'` 文本到 `PollyPassOptions` 位集的完整解析链路。
- 区分 `polly` 与 `polly-custom` 两种入口、`enableEnd2End` / `enableDefaultOpts` / `disableAfter` 三种预设。
- 手工推断任意一个 `-passes=polly<...>` 串最终会启用哪些阶段，并预测 `checkConsistency` 会拒绝哪些非法组合。

## 2. 前置知识

本讲假设你已经读过 u1-l4（插件注册）与 u2-l1（PhaseManager 全景）。需要回忆的关键概念：

- **New Pass Manager（NPM）**：LLVM 现行的 pass 框架。一个 pass 是一个带 `run(IRUnit, AnalysisManager)` 方法的类，通过混入（mixin）向框架声明自己的元信息。
- **`PassBuilder` 的文本流水线**：`-passes='foo<...>;bar'` 这种写法会被 `PassBuilder` 解析成一棵 pass 树。`<...>` 里的内容叫**该 pass 的参数（params）**，由 pass 自己提供的 parser 解析。
- **`PassPhase` 枚举**：Polly 的阶段标识，声明顺序即执行顺序（见 u2-l1）。本讲频繁引用它的成员名，如 `Detection`、`ScopInfo`、`DeLICM`、`AstGen`、`CodeGen`。
- **`PollyFunctionPass`**：上一篇提到它是 `PhaseManager` 的对外封装，本讲正式拆解。

一个易混点先澄清：本讲的"选项"指的是 `-passes='polly<...>'` **尖括号里**的阶段开关，它与 `-polly-position`、`-polly-vectorizer` 这些**命令行 cl::opt** 不是一回事——但二者会在 `parsePollyOptions` 里汇合。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [include/polly/Pass/PollyFunctionPass.h](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/include/polly/Pass/PollyFunctionPass.h) | 函数级封装 pass 的声明，继承 `OptionalPassInfoMixin`。 |
| [include/polly/Pass/PollyModulePass.h](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/include/polly/Pass/PollyModulePass.h) | 模块级封装 pass 的声明，同样是 `OptionalPassInfoMixin`。 |
| [lib/Pass/PollyFunctionPass.cpp](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Pass/PollyFunctionPass.cpp) | `run()` 一行转调 `runPollyPass`，并保守地返回保留分析。 |
| [lib/Pass/PollyModulePass.cpp](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Pass/PollyModulePass.cpp) | 遍历模块内每个函数，逐一调用 `runPollyPass`。 |
| [include/polly/Pass/PhaseManager.h](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/include/polly/Pass/PhaseManager.h) | `PassPhase` 枚举、`PollyPassOptions` 位集类的定义。 |
| [lib/Pass/PhaseManager.cpp](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Pass/PhaseManager.cpp) | `getPhaseName`/`parsePhase`/`dependsOnDependenceInfo`、位集三预设与 `checkConsistency` 的实现。 |
| [lib/Support/RegisterPasses.cpp](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Support/RegisterPasses.cpp) | `parsePollyOptions`（核心）、流水线解析回调、`buildCommonPollyPipeline`。 |
| [lib/Support/PollyPasses.def](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Support/PollyPasses.def) | X-Macro，登记 `polly`/`polly-custom` 两个名字到 pass 与 parser 的绑定。 |

## 4. 核心概念与源码讲解

本讲拆为四个最小模块：**4.1 OptionalPassInfoMixin**（pass 基类混入）、**4.2 PollyFunctionPass/ModulePass 封装**、**4.3 PollyPassOptions 位集与三预设**、**4.4 parsePollyOptions 参数解析**。前两个属于"如何封装为 LLVM Pass"，后两个属于"Pass 参数解析"。

### 4.1 OptionalPassInfoMixin：Polly 通行证的"身份证"

#### 4.1.1 概念说明

在 NPM 里，一个 pass 类要被框架认识，需要带一些**静态元信息**：自己叫什么名字（`name()`）、打印流水线时怎么显示（`printPipeline`）、以及一个关键问题——**这个 pass 是否"必须运行"（`isRequired()`）**。

LLVM 用 CRTP（Curiously Recurring Template Pattern）混入提供这套样板。其中 `isRequired()` 的语义是：

- 返回 `true`：该 pass **不可跳过**，即便看起来"没人需要它的结果"也照跑。
- 返回 `false`：该 pass **可以被跳过**。最典型的场景是 `-O0`：NPM 在 O0 下只保留 `isRequired()==true` 的 pass。

`OptionalPassInfoMixin` 就是把 `isRequired()` 钉死为 `false` 的那个混入。Polly 的两个封装 pass 都继承自它，等于向框架声明："我是可选的，别在不需要时硬跑我。"

#### 4.1.2 核心流程

继承关系如下（CRTP，把派生类自己作为模板参数传给基类）：

```
PassInfoMixin<PollyFunctionPass>          // 通用样板（name/printPipeline 等）
        ↑
OptionalPassInfoMixin<PollyFunctionPass>  // 覆盖 isRequired() = false
        ↑
PollyFunctionPass                         // Polly 的实际 pass
```

`isRequired()` 如何影响调度：

1. `PassBuilder` 构建 pass 管道。
2. 调度器在决定是否运行某个 pass 时，查询其 `isRequired()`。
3. Polly 通行证返回 `false` → 在 O0 或未被显式需要时可以被略过。
4. 但当用户**显式**写 `-passes='polly'` 时，它仍会按需运行（显式列入即"被需要"）。

#### 4.1.3 源码精读

LLVM 侧三个混入的定义，对照看就能理解"可选 vs 必需"是一组对称设计：

[llvm/include/llvm/IR/PassManager.h:88-111](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/llvm/include/llvm/IR/PassManager.h#L88-L111) —— `PassInfoMixin` 是基类（注释明确说"实际 pass 应继承 Required 或 Optional 之一"），`RequiredPassInfoMixin` 让 `isRequired()=true`，`OptionalPassInfoMixin` 让 `isRequired()=false`。

Polly 侧的继承就一行：

[include/polly/Pass/PollyFunctionPass.h:19-20](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/include/polly/Pass/PollyFunctionPass.h#L19-L20) 说明 `PollyFunctionPass` 继承 `OptionalPassInfoMixin<PollyFunctionPass>`，因此 `isRequired()` 为 `false`。`PollyModulePass` 完全对称，见 [include/polly/Pass/PollyModulePass.h:17](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/include/polly/Pass/PollyModulePass.h#L17)。

> 旁证：上一篇我们见过 `buildCommonPollyPipeline` 里有 `Level != OptimizationLevel::O0` 的判断——这与"可选 pass 在 O0 不跑"是同一条逻辑的两侧。

#### 4.1.4 代码实践

**实践目标**：确认两个 Polly 封装 pass 都是"可选"的，并理解其后果。

**操作步骤**（源码阅读型）：

1. 打开 `PollyFunctionPass.h` 与 `PollyModulePass.h`，确认二者都继承 `OptionalPassInfoMixin`。
2. 打开 `llvm/include/llvm/IR/PassManager.h` 第 108-111 行，确认 `OptionalPassInfoMixin::isRequired()` 返回 `false`。
3. 回想 u1-l4：`shouldEnablePollyForOptimization()` 含 `Level != O0` 守卫。

**需要观察的现象**：在 `-O0` 下，即使加载了 Polly 插件、写了 `-polly`，Polly 也不会真正优化——这既有 `Level != O0` 的代码守卫，也与 pass 本身 `isRequired()==false` 一致。

**预期结果**：两条独立防线（选项守卫 + mixin 标记）共同保证 Polly 不在 O0 误跑。

**待本地验证**：用 `clang -O0 -mllvm -polly` 与 `clang -O3 -mllvm -polly` 各编译同一段循环，对比是否出现 SCoP 优化痕迹。

#### 4.1.5 小练习与答案

**Q1**：如果想让 Polly pass 在任何优化级别都强制运行，应该改继承哪个混入？
**答**：改为继承 `RequiredPassInfoMixin`（`isRequired()==true`），但还需同时去掉 `Level != O0` 的守卫，二者缺一不可。

**Q2**：`OptionalPassInfoMixin` 与 `PassInfoMixin` 是什么关系？
**答**：前者继承后者，并在其基础上把 `isRequired()` 覆盖为 `false`；`PassInfoMixin` 本身也默认返回 `false`，但注释明确建议实际 pass 显式选择 Required 或 Optional 之一以表意。

### 4.2 PollyFunctionPass / PollyModulePass：把流水线压成一个 Pass

#### 4.2.1 概念说明

上一篇强调：`PhaseManager` 自身**不是** NPM 的 pass，它只是 `PollyFunctionPass` 内部的一个工具类。对外，LLV​M 看到的是**单个** pass——`PollyFunctionPass`（函数级）或 `PollyModulePass`（模块级）。

这样设计的好处：Polly 内部那条"检测→建模→变换→代码生成"的长流水线，对 NPM 而言是**原子**的。NPM 不需要知道 Polly 有 17 个阶段，只在调度到 `PollyFunctionPass` 时把控制权整体交出，跑完再收回。Polly 因此能自己手动维护跨阶段的 `LoopInfo`/`DominatorTree`/`ScopInfo`（这正是 u2-l1 讲的"ScopInfo 不能被 PM 失效"的实现前提）。

两个封装 pass 的分工：

- `PollyFunctionPass`：作用于单个 `Function`，对应函数级 EP（`-polly-position=before-vectorizer`）。
- `PollyModulePass`：作用于整个 `Module`，对应模块级 EP（`-polly-position=early`），内部对每个函数复用同一套逻辑。

#### 4.2.2 核心流程

```
NPM 调度 PollyFunctionPass::run(F, FAM)
        │
        ├─ runPollyPass(F, FAM, Opts)      // 自由函数，构造 PhaseManager 并 .run()
        │       └─ PhaseManager(F,FAM,Opts).run()   // ← u2-l1 的 17 阶段流水线
        │
        └─ 按是否改 IR 返回 PreservedAnalyses
              改了 → all() 不保留（none）
              没改 → all()    全保留

NPM 调度 PollyModulePass::run(M, MAM)
        │
        ├─ 取出 FunctionAnalysisManager
        ├─ for (Function &F : M):
        │       runPollyPass(F, FAM, Opts)   // 逐函数复用
        └─ 任一函数改了 IR → none()；否则 all()
```

注意 `Opts` 是 pass **构造时**就传入的成员，来源正是 4.4 要讲的 `parsePollyOptions`。

#### 4.2.3 源码精读

`PollyFunctionPass::run` 极简，核心就一行转调 + 保守地报告分析保留情况：

[lib/Pass/PollyFunctionPass.cpp:14-22](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Pass/PollyFunctionPass.cpp#L14-L22) 说明：调用 `runPollyPass`，若改了 IR 就返回 `PreservedAnalyses::none()`（什么都不保留，最保守），否则 `all()`。注释里的 FIXME 指出它无法触及 Module/CGSCC 层的分析——这正是函数级封装的固有局限。

`PollyModulePass::run` 的关键在于"拿到 FAM 再逐函数跑"：

[lib/Pass/PollyModulePass.cpp:15-29](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Pass/PollyModulePass.cpp#L15-L29) 说明：通过 `FunctionAnalysisManagerModuleProxy` 从 `MAM` 取出 `FAM`，然后 `for (Function &F : M)` 逐个调用同一个 `runPollyPass`。注释提到"尤其当并行函数被外联时"要保守返回 `none()`——指 `-polly-parallel` 会把并行体外联成新函数，这是模块级的结构改动。

`runPollyPass` 这个自由函数就是封装 pass 与 `PhaseManager` 之间的桥：

[lib/Pass/PhaseManager.cpp:438-441](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Pass/PhaseManager.cpp#L438-L441) 说明：构造一个临时 `PhaseManager` 并立即 `.run()`，返回是否修改 IR。`PhaseManager` 是匿名命名空间里的类，不对外暴露。

#### 4.2.4 代码实践

**实践目标**：看清"一个 pass = 整条流水线"的封装边界。

**操作步骤**（源码阅读型）：

1. 在 `PhaseManager.cpp` 找到 `runPollyPass`（438 行），确认它只是 `PhaseManager(...).run()`。
2. 在 `PollyFunctionPass.cpp` 与 `PollyModulePass.cpp` 分别确认 `run()` 的转调路径。
3. 对照 u2-l1 的 `PhaseManager::run()`（66-264 行），意识到 NPM 调用一次 `PollyFunctionPass::run`，内部其实跑了 17 个阶段。

**需要观察的现象**：从 NPM 视角，`PollyFunctionPass` 是"一个 pass"；从 Polly 视角，它内部是一个完整的子流水线。两套"阶段"概念不要混淆。

**预期结果**：能向别人解释"NPM 的 pass 列表里 Polly 只占一格，但这一格内含 17 阶段"。

**待本地验证**：无运行命令，属纯阅读实践。

#### 4.2.5 小练习与答案

**Q1**：为什么 `PollyModulePass::run` 要先从 `MAM` 取 `FAM`？
**答**：因为 `PhaseManager`/`runPollyPass` 只接受 `FunctionAnalysisManager`（它需要的 `LoopInfo`/`DominatorTree`/`ScalarEvolution` 等都是函数级分析），模块级 pass 必须先降级到函数级 FAM 才能复用同一套逻辑。

**Q2**：`PollyFunctionPass::run` 改了 IR 时返回 `none()`，会对后续 pass 有何影响？
**答**：`none()` 表示不保留任何分析，NPM 会令后续 pass 按需重算所有分析；这是最保守也最安全的策略，代价是可能多算几次分析。

### 4.3 PollyPassOptions：用一个位集表示"开哪些阶段"

#### 4.3.1 概念说明

`PollyPassOptions` 是 Polly 通行证的"配置包"。它最核心的字段是一个**位集（bitset）** `PhaseEnabled`——每个 `PassPhase` 对应一个 bit，1 表示该阶段要跑，0 表示跳过。这个位集就是 4.4 解析过程的最终产物，也是 `PhaseManager::run()` 里所有 `Opts.isPhaseEnabled(...)` 判断的依据（见 u2-l1）。

围绕位集，Polly 提供了三种**预设（preset）**来批量置位，外加一个一致性检查器：

| 成员 | 作用 |
| --- | --- |
| `enableEnd2End()` | 置位"端到端回写 IR"所必需的阶段（detect/scops/deps/ast/codegen）。 |
| `enableDefaultOpts()` | 置位默认优化阶段（prepare/simplify×2/optree/delicm/prune/opt-isl）。 |
| `disableAfter(P)` | 关掉 P **之后**的所有阶段（用于"只跑到某阶段为止"的回归测试）。 |
| `checkConsistency()` | 校验已启用阶段的前置依赖是否满足，不满足则返回 `Error`。 |

外加三个非位集字段：`ViewAll`、`ViewFilter`（控制 CFG 可视化）、`PrintDepsAnalysisLevel`（依赖打印粒度）。

#### 4.3.2 核心流程

位集的下标映射用了"省一位"的小技巧：`PassPhase::None` 占了枚举的 0 号位但从不使用，所以 bit 位置按以下公式平移：

\[
\text{bitpos}(P) = \text{idx}(P) - \text{idx}(\text{Prepare})
\]

其中 `Prepare` 是 `PassPhaseFirst`。这样位集数组大小正好等于"实质阶段数"，不浪费 `None` 那一位。

`isPhaseEnabled` / `setPhaseEnabled` 就是这个公式的两个方向（读/写）。三预设则是对一组 `setPhaseEnabled` 的封装。

`checkConsistency` 的依赖规则（用人话说）：

- 任何非 `Prepare`/`Detection` 的阶段，都需要 `Detection` 开。
- `ScopInfo` 及其后的阶段，还需要 `ScopInfo` 开。
- "依赖 DependenceInfo"的阶段，还需要 `Dependences` 开。
- 特别地：`CodeGen` 还额外要求 `AstGen` 开。

这些规则就是 4.4 里 `checkConsistency` 报错的依据。

#### 4.3.3 源码精读

位集字段的定义，注意模板参数是"首末阶段之差 + 1"：

[include/polly/Pass/PhaseManager.h:74-101](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/include/polly/Pass/PhaseManager.h#L74-L101) 说明：`PhaseEnabled` 是 `llvm::Bitset<N>`，大小 `PassPhaseLast - PassPhaseFirst + 1`；`isPhaseEnabled`/`setPhaseEnabled` 用 `assert(Phase != None)` 守卫，并按 `idx(Phase) - idx(PassPhaseFirst)` 计算位下标。注释点明"因为 `None` 未用，位位置整体左移一位"。

三预设的实现，可以一眼看清各自置了哪些位：

[lib/Pass/PhaseManager.cpp:373-398](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Pass/PhaseManager.cpp#L373-L398) 说明：
- `enableEnd2End()` 置 `Detection`/`ScopInfo`/`Dependences`/`AstGen`/`CodeGen`——即"把 IR 变回 IR"的最小闭环。
- `enableDefaultOpts()` 置 `Prepare`/`Simplify0`/`Optree`/`DeLICM`/`Simplify1`/`PruneUnprofitable`/`Optimization`——即默认开启的优化变换。
- `disableAfter(P)` 用 `enum_seq_inclusive(P, PassPhaseLast)` 遍历，跳过 `P` 自身、把其后的全部置 `false`。

`checkConsistency` 逐阶段检查依赖，任一不满足即构造带提示的 `StringError`：

[lib/Pass/PhaseManager.cpp:400-436](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Pass/PhaseManager.cpp#L400-L436) 说明：遍历所有已启用阶段；`Prepare`/`Detection` 无要求直接跳过；其余先要求 `Detection`，再（对 `ScopInfo` 及之后）要求 `ScopInfo`，再按 `dependsOnDependenceInfo` 要求 `Dependences`；最后单独检查 `CodeGen` 必须有 `AstGen`。返回 `Error::success()` 表示一致。

`dependsOnDependenceInfo` 决定哪些阶段"真正"依赖 `Dependences` 阶段：

[lib/Pass/PhaseManager.cpp:352-371](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Pass/PhaseManager.cpp#L352-L371) 说明：`Dependences` 及之前的阶段返回 `false`；显式列出的 `Simplify0`/`Optree`/`DeLICM`/`Simplify1`/`PruneUnprofitable`/`ImportJScop`/`ExportJScop`/`AstGen`/`CodeGen` 也返回 `false`；其余（`print-deps`/`dce`/`mse`/`opt-isl`）返回 `true`。注意 `AstGen`/`CodeGen` 虽然在 `run()` 里实际用到 `DA`，但这里被判为 `false`——它们对 `Dependences` **阶段**的依赖被视为可经 `opt-isl` 等传递满足，因此不会被 `checkConsistency` 强制（详见 4.4 的非法组合分析）。

#### 4.3.4 代码实践

**实践目标**：把"预设 = 一组置位"这件事在源码里落实。

**操作步骤**（源码阅读型）：

1. 读 `enableEnd2End()`，数一数它置了 5 个位。
2. 读 `enableDefaultOpts()`，数一数它置了 7 个位。
3. 注意二者**没有重叠**（`End2End` 管检测/建模/回写，`DefaultOpts` 管中间的优化变换）——这正好拼出一条完整流水线。

**需要观察的现象**：默认 `-passes='polly'`（IsCustom=false）会同时调用这两个预设，因此等价于"开 End2End + 开 DefaultOpts"，即 12 个阶段全开。

**预期结果**：能解释为何 `-passes='polly'` 不写任何尖括号参数就能跑完整流程——因为预设替你置好了位。

**待本地验证**：无运行命令，属纯阅读实践。

#### 4.3.5 小练习与答案

**Q1**：`enableEnd2End()` 与 `enableDefaultOpts()` 的并集是否覆盖了全部 17 个实质阶段？
**答**：未完全覆盖。二者并集含 detect/scops/deps/ast/codegen + prepare/simplify×2/optree/delicm/prune/opt-isl = 12 个；`flatten`、`dce`、`mse`、`import/export-jscop` 等不在默认预设里，需要单独开启。

**Q2**：`disableAfter(AstGen)` 会关掉哪些阶段？
**答**：会关掉 `AstGen` **之后**的阶段，即 `CodeGen`（`AstGen` 自身保留）。这正是 `stopafter=ast` 想要的效果——生成 AST 但不回写 IR。

### 4.4 parsePollyOptions：从 `-passes=polly<...>` 到位集

#### 4.4.1 概念说明

这是本讲的重头戏。`parsePollyOptions(Params, IsCustom)` 接收尖括号里的字符串 `Params`，输出一个填好的 `PollyPassOptions`。它要协调三类置位来源，并按固定顺序合并：

1. **命令行 cl::opt**：如 `-polly-print-scops`、`-polly-enable-delicm=false`，会被翻译成对某些阶段的初始置位。
2. **尖括号参数**：如 `no-delicm`、`stopafter=ast`、`default-opts`、`end2end`，以及任意阶段名。
3. **两种入口**：`polly`（IsCustom=false，默认开 End2End+DefaultOpts）与 `polly-custom`（IsCustom=true，**什么都不默认开**，全部 opt-in）。

合并顺序非常讲究，决定了"谁能覆盖谁"：

```
① 命令行 cl::opt → 写入临时数组 PassEnabled[]（三态：未设置/true/false）
② 隐式依赖：对每个显式/命令行置 true 的阶段，按规则连带置位其前置依赖
③ 预设：enableEnd2End()（若开）、enableDefaultOpts()（若开）
④ 显式覆盖：把 PassEnabled[] 里"显式设置过"的值再贴回去（于是 no-xxx 能覆盖预设）
⑤ stopafter：disableAfter(StopAfter)
⑥ checkConsistency：合法性校验，失败则返回 Error
```

关键直觉：**预设（③）会被显式开关（④）覆盖**。所以 `polly<no-delicm>` 是"先开全套默认、再把 delicm 关掉"，而不是"什么都没有"。而 `polly-custom` 因为 ③ 不执行，必须自己一项项开。

#### 4.4.2 核心流程

完整解析流程（对照源码行号）：

```text
parsePollyOptions(Params, IsCustom):
  EnableDefaultOpts = !IsCustom      # polly→true, polly-custom→false
  EnableEnd2End     = !IsCustom
  PassEnabled[]: optional<bool>      # 三态：nullopt / true / false
  StopAfter = None

  # ① 命令行 cl::opt 折算进 PassEnabled
  if (PollyPrintScops) PassEnabled[PrintScopInfo]=true
  if (!EnableDeLICM)   PassEnabled[DeLICM]=false
  ... (十余条)

  # 解析尖括号，逐段处理
  for Param in Params.split(';'):
    if Param == "stopafter=PHASE":  StopAfter = parsePhase(PHASE); continue
    Enabled = !Param.starts_with("no-")      # no- 前缀 → 关
    if Param in {"default-opts","end2end"}:  改对应总开关; continue
    if Param == "simplify":  同时置 Simplify0 与 Simplify1   # 快捷方式
    else:
      Phase = parsePhase(Param)
      if PrevPhase >= Phase: 报错"阶段必须按序、不可重复"      # 排序校验
      PassEnabled[Phase] = Enabled
    PrevPhase = Phase

  # ② 隐式依赖（仅对"为 true"的阶段连带上位）
  for P in 全部阶段:
    if PassEnabled[P] != true: continue
    if P > Detection:  set Detection
    if P > ScopInfo:   set ScopInfo
    if dependsOnDependenceInfo(P): set Dependences
    if P > AstGen:     set AstGen

  # ③ 预设
  if EnableEnd2End:    Opts.enableEnd2End()
  if EnableDefaultOpts: Opts.enableDefaultOpts()

  # ④ 显式覆盖（只贴"显式设过"的）
  for P in 全部阶段:
    if PassEnabled[P].has_value(): Opts.setPhaseEnabled(P, *PassEnabled[P])

  # ⑤ stopafter
  if StopAfter != None: Opts.disableAfter(StopAfter)

  # ⑥ 一致性
  if (Err = Opts.checkConsistency()) return Err
  return Opts
```

`polly` vs `polly-custom` 的入口绑定在 X-Macro 里：

[lib/Support/PollyPasses.def:17-18](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Support/PollyPasses.def#L17-L18) 说明：函数级下，`"polly"` 绑定 `parsePollyDefaultOptions`（IsCustom=false），`"polly-custom"` 绑定 `parsePollyCustomOptions`（IsCustom=true）；二者创建的都是同一个 `PollyFunctionPass(Opts)`，区别**只在 Opts**。模块级对称（第 4-5 行）。

两个包装函数只是固定 `IsCustom` 实参：

[lib/Support/RegisterPasses.cpp:415-423](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Support/RegisterPasses.cpp#L415-L423) 说明：`parsePollyDefaultOptions` 传 `false`，`parsePollyCustomOptions` 传 `true`。

#### 4.4.3 源码精读

`parsePollyOptions` 的开头确定两个总开关与三态数组：

[lib/Support/RegisterPasses.cpp:234-242](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Support/RegisterPasses.cpp#L234-L242) 说明：`EnableDefaultOpts=!IsCustom`、`EnableEnd2End=!IsCustom`；`PassEnabled` 是 `std::optional<bool>` 数组，下标直接用枚举整数值，长度 `PassPhaseLast+1`。三态设计是"显式覆盖"能成立的基础——只有"被显式设过"的位才会在第④步覆盖预设。

命令行 cl::opt 折算进三态数组的一长串映射，挑几条代表性：

[lib/Support/RegisterPasses.cpp:246-300](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Support/RegisterPasses.cpp#L246-L300) 说明：把 `-polly-print-scops`、`-polly-enable-delicm=false`、`-polly-code-generation=ast`、`-polly-optimizer=none` 等 cl::opt 折算成对 `PrintScopInfo`/`DeLICM`/`AstGen`/`Optimization` 等阶段的初始三态。注意它们只是"初值"，仍可在第④步被尖括号参数覆盖。

尖括号参数的逐段解析，含 `no-` 前缀与 `simplify` 快捷方式：

[lib/Support/RegisterPasses.cpp:302-363](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Support/RegisterPasses.cpp#L302-L363) 说明：`stopafter=PHASE` 特判（307-314）；`no-` 前缀剥离并翻转 `Enabled`（321-325）；`simplify` 同时置 `Simplify0`/`Simplify1`（339-343）；其余用 `parsePhase` 查枚举。352-358 行是**排序校验**：`PrevPhase >= Phase` 即报"phases must not be repeated and enumerated in-order"——强制阶段必须按枚举顺序列出且不得重复。

隐式依赖连带置位（注意只对值为 `true` 的阶段生效）：

[lib/Support/RegisterPasses.cpp:372-389](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Support/RegisterPasses.cpp#L372-L389) 说明：遍历所有阶段，`value_or(false)` 取出值；仅当为 `true` 时，按"P 在 Detection/ScopInfo/AstGen 之后"或"dependsOnDependenceInfo(P)"连带上位其前置。注释点明"先隐式上位，后面可被显式 on/off 覆盖"。

预设调用与显式覆盖的先后（③→④），这是"覆盖语义"的关键：

[lib/Support/RegisterPasses.cpp:391-407](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Support/RegisterPasses.cpp#L391-L407) 说明：先 `enableEnd2End()`/`enableDefaultOpts()`（若总开关开），再遍历把 `PassEnabled` 中**有值**的阶段贴回 `Opts`——所以 `no-delicm`（三态=false）会覆盖第③步预设开的 `DeLICM`。最后 `disableAfter(StopAfter)`。

收尾的一致性检查：

[lib/Support/RegisterPasses.cpp:409-412](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Support/RegisterPasses.cpp#L409-L412) 说明：调用 `Opts.checkConsistency()`，若返回 `Error` 则直接把错误透传出去（最终由 `ExitOnError` 转成致命报错）。

**一条命令的完整追踪**：`-passes='polly<no-delicm;stopafter=ast>'`（IsCustom=false）

1. `EnableDefaultOpts=true`、`EnableEnd2End=true`。
2. ①无相关 cl::opt（假设未额外给）。
3. 尖括号：`no-delicm` → `PassEnabled[DeLICM]=false`，`PrevPhase=DeLICM`；`stopafter=ast` → `StopAfter=AstGen`。
4. ②隐式：没有阶段为 `true`，跳过。
5. ③预设：`enableEnd2End()` 开 detect/scops/deps/ast/codegen；`enableDefaultOpts()` 开 prepare/simplify0/optree/**delicm**/simplify1/prune/opt-isl。
6. ④显式覆盖：`DeLICM` 有值（false）→ 关掉 delicm（覆盖第③步的开）。
7. ⑤`disableAfter(AstGen)` → 关掉 CodeGen（AstGen 保留）。
8. ⑥`checkConsistency`：所有已启用阶段的前置都满足，通过。

**最终启用**：prepare, detect, scops, deps, simplify-0, optree, ~~delicm~~, simplify-1, prune, opt-isl, ast，停在 ast、不回写 IR。

**对照**：把同样参数换成 `polly-custom`（IsCustom=true）→ 第③步两预设都不执行 → 没有 cl::opt → 也没有任何 `=true` 的阶段 → 最终**什么都不启用**。这就是 `polly-custom` 的"全 opt-in"特性：你必须自己把 detect/scops/... 一项项写出来。

**`checkConsistency` 会拒绝的非法组合**（在 `polly-custom` 下最容易构造，因为预设不兜底）：

- `polly-custom<opt-isl;no-detect>`：`opt-isl` 隐式上位 detect/scops/deps/ast，但 `no-detect` 在第④步把 detect 关掉 → `checkConsistency` 报 `"'opt-isl' requires 'detect' to be enabled"`。
- `polly-custom<codegen;no-scops>`：codegen 隐式上位 detect/scops/ast，`no-scops` 关掉 scops → 报 `"'codegen' requires 'scops' to be enabled"`。
- **解析期**就拒绝（不走 checkConsistency）：`polly-custom<ast;detect>` —— `ast` 在 `detect` 之前，违反"按序" → `PrevPhase(AstGen) >= Detection` → 报 `"phases must not be repeated and enumerated in-order"`。

#### 4.4.4 代码实践

**实践目标**：用真实命令验证上面的追踪，直观看到 `polly` 与 `polly-custom` 的差异，并亲手触发一次 `checkConsistency` 拒绝。

**操作步骤**：

1. 准备一段含嵌套循环的 C 代码 `mm.c`（矩阵乘即可）。
2. 生成 LLVM IR：
   ```bash
   clang -O1 -Xclang -disable-O0-optnone -emit-llvm -S mm.c -o mm.ll
   ```
3. 用**默认入口**跑，停在 ast（应能打印 SCoP 并生成 AST，但不回写 IR）：
   ```bash
   opt -passes='polly<no-delicm;stopafter=ast>' -polly-print-scops -polly-ast -S mm.ll 2>&1 | head
   ```
4. 用 **custom 入口**跑同一串参数（预期：什么都不输出，因为没有阶段被启用）：
   ```bash
   opt -passes='polly-custom<no-delicm;stopafter=ast>' -polly-print-scops -S mm.ll 2>&1 | head
   ```
5. 让 custom 真正干活，需显式列出阶段：
   ```bash
   opt -passes='polly-custom<detect;scops;print-scops>' -S mm.ll 2>&1 | head
   ```
6. 触发一次合法性拒绝，观察报错文本：
   ```bash
   opt -passes='polly-custom<opt-isl;no-detect>' -S mm.ll
   # 预期：报 "'opt-isl' requires 'detect' to be enabled"
   ```

**需要观察的现象**：

- 第 3 步应出现 `Printing analysis 'Polly - Create polyhedral description...'` 与 AST 输出；无 codegen 痕迹。
- 第 4 步**没有任何 Polly 输出**——印证 `polly-custom` 不开预设。
- 第 5 步重新出现 SCoP 打印——因为显式开了 detect/scops/print-scops。
- 第 6 步直接报错退出。

**预期结果**：与 4.4.3 的源码追踪完全吻合。

**待本地验证**：上述命令的具体输出形态依赖你本机的 LLVM/Polly 版本与 `mm.c` 内容；若 `-polly-print-scops` 等选项未在插件中暴露，请确认 LLVMPolly 已被正确加载（见 u1-l4）。

#### 4.4.5 小练习与答案

**Q1**：为什么 `-passes='polly-custom<no-delicm;stopafter=ast>'` 跑出来"什么都没做"？
**答**：因为 `polly-custom` 使 `EnableDefaultOpts` 与 `EnableEnd2End` 均为 `false`（第③步两预设都不执行），而尖括号里只有"关闭"指令（`no-delicm`）和"截断"指令（`stopafter`），没有任何"开启"指令，故没有任何阶段被置位。

**Q2**：`PassEnabled` 为何用 `std::optional<bool>` 而非 `bool`？
**答**：为了区分"未提及"与"显式设为 false"。只有"显式设过"的位才会在第④步覆盖预设；若用普通 `bool`，就无法区分"用户没说"和"用户说关"，覆盖语义就丢了。

**Q3**：`polly-custom<detect;detect;scops>` 会在哪一步报错？
**答**：在尖括号解析期（第②步之前）就报错。第二个 `detect` 使 `PrevPhase(Detection) >= Detection` 成立，触发"phases must not be repeated and enumerated-in-order"，根本走不到 `checkConsistency`。

## 5. 综合实践

把本讲四块知识串起来，做一次"纸面推演 + 命令验证"：

**任务**：给定命令 `-passes='polly-custom<dce;no-prune;stopafter=ast>'`（并假设命令行另给了 `-polly-print-deps`），请：

1. **纸面推演**：按 4.4.2 的六步流程，逐步写出 `PassEnabled` 三态数组的演化、②隐式连带了哪些阶段、③预设是否执行、④哪些被覆盖、⑤截断到哪、⑥是否通过 `checkConsistency`。最终列出"实际启用的阶段"清单。
2. **命令验证**：用 4.4.4 的 `mm.ll` 实跑这条命令（注意把 `-polly-print-deps` 也加上），观察：
   - 是否打印了依赖（验证 `-polly-print-deps` 折算成 `PrintDependences=true`，并连带要求 deps 阶段）？
   - 是否执行了 `dce`、跳过了 `prune`、停在 `ast`？
3. **故意改错**：把 `dce` 换成 `opt-isl` 并加上 `no-scops`，预测报错文本，再实跑核对。

**参考推演要点**（留给你对照）：`polly-custom` 不开预设；`dce=true` 经②连带上位 detect/scops/deps（因 `dependsOnDependenceInfo(dce)=true`）/ast（dce>ast? 否，dce 在 ast 之前，故 ast 不上位）；`-polly-print-deps` 使 `PrintDependences=true`，连带 detect/scops/deps；`no-prune` 仅显式关 prune；`stopafter=ast` 截断掉 ast 之后的 codegen。最终启用大致为 detect/scops/deps/print-deps/dce，prune 被关，停在 dce 之后但 ast 未启用（因 dce 在 ast 前、`stopafter=ast` 只关 ast 之后）——请自行核对 `checkConsistency` 是否通过、以及是否真的"跑到 ast"。

> 提示：`stopafter=ast` 的语义是 `disableAfter(AstGen)`，它只**关闭** AstGen 之后的阶段，并**不**自动开启 AstGen。若你的推演里 AstGen 没被②连带上位，那么"停在 ast"其实是"停在 ast 之前"。这正是本实践想让你察觉的细节——**待本地验证**实际打印内容。

## 6. 本讲小结

- `PollyFunctionPass`/`PollyModulePass` 都继承 `OptionalPassInfoMixin`，`isRequired()==false`，使 Polly 通行证"可被跳过"（尤其 O0）。
- 这两个 pass 把 Polly 内部 17 阶段流水线压成**一个** NPM pass；模块版只是逐函数复用 `runPollyPass`。
- `PollyPassOptions` 用一个 `llvm::Bitset` 表示"开哪些阶段"，位下标按 \(\text{bitpos}=\text{idx}(P)-\text{idx}(\text{Prepare})\) 平移。
- 三预设 `enableEnd2End`/`enableDefaultOpts`/`disableAfter` 是批量置位工具；`checkConsistency` 用依赖规则把非法组合挡在门外。
- `parsePollyOptions` 按"命令行 cl::opt → 隐式依赖 → 预设 → 显式覆盖 → stopafter → 一致性检查"六步合并，三态 `optional<bool>` 是"显式覆盖预设"的关键。
- `polly`（默认开两预设）与 `polly-custom`（全 opt-in）创建同一个 pass，差别只在 `Opts`；尖括号里的阶段必须按枚举顺序、不可重复。

## 7. 下一步学习建议

本讲把"Polly 如何作为一个 pass 被驱动、如何配置"讲透了。接下来沿主线向下：

- **进入检测阶段**：u3-l1（SCoP 概念与 ScopDetection 设计）。现在你已经知道 `Detection` 这个 bit 控制 `PhaseManager::run()` 里 `SD.detect(F)` 是否真正执行——u3 会展开 `detect` 内部到底怎么判定一个区域合法。
- **理解规范化**：u2-l3（规范化与代码准备阶段）会解释 `Prepare` 这个 bit 对应的 `runCodePreparation` 与一串规范化 pass，补上"为什么 Polly 必须吃规范化 IR"。
- **二次开发铺垫**：本讲的 `parsePollyOptions` 与 `PollyPasses.def` 是 u10-l4"添加自定义 Polly 阶段"的必经之路——要在 `-passes=polly<myphase>` 里识别新名字，就要回到这里的解析链路。
