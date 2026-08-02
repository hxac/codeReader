# 新 Pass 管理器（New Pass Manager）

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清在新 Pass 管理器（New Pass Manager，简称新 PM）里，**pass（变换）**与 **analysis（分析）**这两种东西的本质区别，以及它们各自的缓存与失效机制。
- 顺着 `PassManager::run` 的源码主循环，讲明白「跑一个 pass → 失效分析 → 累积保留集」三件事是如何串起来的。
- 理解新 PM「没有 pass 基类」的设计哲学：任何带 `run` 方法的类都能当 pass，靠 CRTP mixin 和类型擦除（`PassConcept` / `PassModel`）来统一调度。
- 知道 `PassInstrumentation` 提供了哪些插桩回调点（BeforePass / AfterPass / BeforeAnalysis …），以及 `PassBuilder` 里注册 pass 和 analysis 的入口长什么样。

本讲是第 3 单元「Pass 与优化流水线」的第一讲，只讲管理器骨架，**不**教你怎么从零写一个 pass（那是 u3-l2 的事），也**不**讲流水线文本怎么解析（那是 u3-l3 的事）。

## 2. 前置知识

本讲假设你已经掌握 u2 系列建立的 LLVM IR 心智模型：

- **IR 的四层归属树**：Module → Function → BasicBlock → Instruction（见 u2-l1）。新 PM 的管理器正是按「作用在哪种 IR 单元上」来分门别类的。
- **值（Value）与类型（Type）**（见 u2-l2）：pass 在改 IR，analysis 在读 IR，读的都是这棵树上的对象。
- **`.ll` 文本与 `.bc` 位码**（见 u2-l4），以及 `opt` 是个把 IR 变 IR 的薄壳工具（见 u1-l3）。

几个本讲会用到的术语，先用大白话解释：

- **pass（通行/变换）**：一段「吃进 IR、吐出改过的 IR」的逻辑。它**修改** IR，并且必须声明自己「保住了哪些分析结果」（见 4.1）。
- **analysis（分析）**：一段「吃进 IR、吐出某种信息」的逻辑。它**只读不改** IR，结果会被缓存起来给别人反复用。比如「这个函数的支配树（DominatorTree）算出来是什么样」。
- **IR 单元（IRUnit）**：一段 IR 的层级范围。新 PM 内置支持 Module、Function、CGSCC（调用图的强连通分量）、Loop、MachineFunction 这几种粒度。
- **PreservedAnalyses（保留分析集）**：一个 pass 跑完后填写的「成绩单」，告诉管理器「我保证没破坏哪些分析结果」。这是新 PM 失效机制的核心数据结构。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [`include/llvm/IR/PassManager.h`](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/PassManager.h) | 新 PM 的「门面」。定义 `PassManager`、`AnalysisManager` 两个类模板，以及 `PassInfoMixin` / `AnalysisInfoMixin` 两个 CRTP mixin。 |
| [`include/llvm/IR/PassManagerInternal.h`](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/PassManagerInternal.h) | 类型擦除的实现细节：`PassConcept`（抽象接口）与 `PassModel`（具体包装）。用函数指针而非虚函数，省掉 vtable。 |
| [`include/llvm/IR/PassManagerImpl.h`](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/PassManagerImpl.h) | 两个管理器的**模板方法实现**。本讲最重要的 `PassManager::run` 主循环、`AnalysisManager` 的懒加载缓存与失效逻辑都在这里。 |
| [`include/llvm/IR/PassInstrumentation.h`](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/PassInstrumentation.h) | 插桩机制：`PassInstrumentationCallbacks`（注册回调）与 `PassInstrumentation`（执行回调）。 |
| [`include/llvm/IR/Analysis.h`](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/Analysis.h) | `PreservedAnalyses` 保留集，以及 `AnalysisInfoMixin`、`AnalysisKey` 的定义。 |
| [`include/llvm/Passes/PassBuilder.h`](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/Passes/PassBuilder.h) | 把 pass / analysis 注册进管理器、把 `-passes=...` 文本解析成流水线的总入口。 |

此外会用两个「真实 pass / analysis 样本」做例子：

| 文件 | 作用 |
| --- | --- |
| [`include/llvm/IR/Dominators.h`](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/Dominators.h) | `DominatorTreeAnalysis`——一个标准的 function analysis 样本。 |
| [`examples/IRTransforms/SimplifyCFG.cpp`](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/examples/IRTransforms/SimplifyCFG.cpp) | `SimplifyCFGPass`——一个标准的 function pass 样本，演示如何消费 analysis。 |

---

## 4. 核心概念与源码讲解

### 4.1 PassManager 与 AnalysisManager

#### 4.1.1 概念说明

新 PM 把「跑 pass」这件事拆成两个相互独立又彼此配合的管理器：

- **`PassManager<IRUnitT>`**：管理「一串 pass」的有序队列。它自己也是个合法的 pass（一个「容器 pass」），可以被外层管理器嵌套调用。常见别名是 `ModulePassManager` 和 `FunctionPassManager`。
- **`AnalysisManager<IRUnitT>`**：管理「一堆 analysis 的结果缓存」。它懒执行——只有当某个 pass 真正来「取」某个分析结果时，对应的 analysis 才会跑第一次；跑完的结果按 IR 单元的地址缓存，后续直接命中。

为什么必须分两个？因为 **pass 改 IR，analysis 只读 IR**。改 IR 会让之前缓存的分析结果「过期」，所以每个 pass 跑完都要告诉 `AnalysisManager`「我保住了哪些分析」，管理器据此把没保住的结果**失效（invalidate）**掉。这就是新 PM 相对老 PM 最关键的改进：失效是**精确到单个分析、按需驱动**的，而不是一刀切全清。

注意这里的层级关系：作用在 Module 上的 `ModulePassManager` 里如果塞了一个「函数级 pass」，需要用 **adaptor**（适配器，如 `ModuleToFunctionPassAdaptor`）包一层——它遍历 Module 里的每个 Function，对每个 Function 跑内层的 `FunctionPassManager`。本讲末尾会看到它的源码。

#### 4.1.2 核心流程

`PassManager::run` 的主循环（伪代码，对应真实源码 `PassManagerImpl.h:28-98`）：

```
PreservedAnalyses 总保留集 = all()                      # 一开始假设全保住
从 AnalysisManager 取出 PassInstrumentation PI           # 拿到插桩入口

for 每一个 pass in Passes:
    if not PI.runBeforePass(pass, IR):                  # 插桩点：可决定跳过
        continue
    本轮保留集 PA = pass.run(IR, AM, 额外参数...)         # 真正跑 pass
    AM.invalidate(IR, PA)                               # 据本轮保留集，失效过期分析
    PI.runAfterPass(pass, IR, PA)                       # 插桩点：跑完回调
    总保留集.intersect(本轮 PA)                          # 求交集，累积结果

总保留集.preserveSet<AllAnalysesOn<IRUnitT>>()           # 本单元内的分析已逐个处理过
return 总保留集
```

`AnalysisManager::getResult` 的懒加载缓存（对应 `PassManagerImpl.h:131-162`）：

```
查找缓存表 AnalysisResults[(分析ID, IR单元地址)]：
    若命中 → 直接返回缓存结果
    若未命中：
        查注册表，拿到这个 analysis 的类型擦除对象 P
        （若不是 pass-instrumentation 自身）PI.runBeforeAnalysis(P, IR)
        结果 = P.run(IR, 本管理器, ...)                  # 真正跑 analysis
        PI.runAfterAnalysis(P, IR)
        把结果存进缓存表
return 结果
```

失效 `AnalysisManager::invalidate`（对应 `PassManagerImpl.h:165-220`）：

```
若 PA 表示「本单元所有分析都保住了」→ 直接返回，啥也不删
构造一个 Invalidator（用于分析间相互声明依赖）
遍历本 IR 单元的每个缓存分析结果：
    调用 结果.invalidate(IR, PA, Invalidator)            # 让结果自己判断是否过期
        （结果内部可借 Invalidator 递归判定它依赖的别的分析是否过期）
        记录「这个分析是否失效」
把所有被判定为失效的结果从缓存表里删掉
```

如果用一张时序图概括 pass 跑一次的全过程：

```
        ┌─ runBeforePass (可跳过)
PassManager::run ──► pass.run ──► (内部可能 getResult 触发 analysis 缓存)
        │                              ├─ runBeforeAnalysis
        │                              ├─ analysis.run
        │                              └─ runAfterAnalysis
        ├─ AM.invalidate(IR, PA)  ──► 逐个判定 + 删除过期分析
        ├─ runAfterPass
        └─ 总保留集.intersect(PA)
```

#### 4.1.3 源码精读

先看两个管理器类模板的定义和别名。[PassManager.h:184-246](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/PassManager.h#L184-L246) 定义了 `PassManager`，它内部用一个 `std::vector<PassConcept 的 unique_ptr> Passes` 来存队列（见第 245 行）；[PassManager.h:257-267](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/PassManager.h#L257-L267) 给出最常用的两个别名 `ModulePassManager`、`FunctionPassManager`；[PassManager.h:575-583](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/PassManager.h#L575-L583) 给出对应的 `ModuleAnalysisManager`、`FunctionAnalysisManager`。

主循环是本讲最该精读的一段，[PassManagerImpl.h:56-95](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/PassManagerImpl.h#L56-L95)。关键三步：

```cpp
PreservedAnalyses PA = PreservedAnalyses::all();        // 总保留集初始化为「全保住」
...
for (auto &Pass : Passes) {
  ...
  if (!PI.runBeforePass<IRUnitT>(*Pass, IR))            // (1) 插桩：可跳过
    continue;
  PreservedAnalyses PassPA = Pass->run(IR, AM, ExtraArgs...);  // (2) 跑 pass
  AM.invalidate(IR, PassPA);                            // (3) 据保留集失效分析
  PI.runAfterPass<IRUnitT>(*Pass, IR, PassPA);          // (4) 插桩：跑完
  PA.intersect(std::move(PassPA));                      // (5) 累积保留集
}
PA.preserveSet<AllAnalysesOn<IRUnitT>>();               // 本单元内分析已逐个处理
```

注意第 95 行 `preserveSet<AllAnalysesOn<IRUnitT>>()` 的含义：循环里每跑一个 pass 就已经 `invalidate` 过本单元的分析了，所以循环结束时**仍然留在缓存里的那些分析都是有效的**，于是用一个「整组保住」的标记一次性声明，避免外层再逐个检查。

再看 analysis 的懒加载缓存 [PassManagerImpl.h:131-162](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/PassManagerImpl.h#L131-L162)。第 134 行 `try_emplace` 用「分析 ID + IR 单元地址」做键查表；`Inserted` 为真表示缓存未命中，于是查注册表（`lookUpPass`）、在 before/after 插桩之间真正跑 analysis（第 148-149 行 `P.run`）、再写回缓存。这就是「取一次、算一次、之后免费」的实现。

最后看失效 [PassManagerImpl.h:165-220](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/PassManagerImpl.h#L165-L220)。第 168 行先做快速判定：若 `PA` 保住了本单元的全部分析，直接 return；否则遍历每个缓存结果，调用 `Result.invalidate(IR, PA, Inv)` 让结果**自判**（第 195 行），最后在第 201-216 行把判定为失效的结果从两张表里删除。`Inv`（`Invalidator`）允许一个分析在自判时递归查询它所依赖的其他分析是否也过期——这样分析间的依赖关系是「按需现场建立」的，不需要预先维护一张全局依赖图。

#### 4.1.4 代码实践

**实践目标**：用一个现成的 function analysis（支配树）直观感受「analysis 被缓存、被打印」的过程。

**操作步骤**：

1. 写一个最小 IR 文件 `t.ll`，含一个带分支的小函数：

   ```llvm
   define i32 @f(i1 %c) {
   entry:
     br i1 %c, label %then, label %else
   then:
     br label %merge
   else:
     br label %merge
   merge:
   %1 = phi i32 [1, %then], [2, %else]
   ret i32 %1
   }
   ```

2. 用 `opt` 的 `print<domtree>` 打印支配树：

   ```bash
   opt -passes='print<domtree>' -disable-output t.ll
   ```

   `print<domtree>` 是一个**打印型 pass**，它内部会 `getResult<DominatorTreeAnalysis>` 触发支配树分析。

**需要观察的现象**：

- 终端会打印出 `DomTree` 的层级结构（`[0] %entry`、`[1] %then`/`%else`/`%merge` 之类）。
- 若把命令换成 `-passes='print<domtree>,print<domtree>'`（连跑两次打印），观察输出里是否出现两次支配树，但分析本身因缓存只该被**计算一次**（可在 `-debug` 下确认，见下）。

**预期结果**：两次打印都成功输出支配树，证明 analysis 结果在同一个 `FunctionAnalysisManager` 内被复用。

> 若想确认「只算了一次」，可加 `-debug`（需要带 ASSERTIONS 的构建）观察 `Running analysis: Dominator Tree` 出现的次数。具体输出依赖本地构建配置，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：如果某个 pass 跑完后返回的是 `PreservedAnalyses::all()`，循环里的 `AM.invalidate(IR, PassPA)` 会做什么？

> **答案**：`all()` 表示「所有分析都保住了」，[PassManagerImpl.h:168](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/PassManagerImpl.h#L168) 的 `PA.allAnalysesInSetPreserved<AllAnalysesOn<IRUnitT>>()` 会直接命中，`invalidate` 立即返回，**不删除任何缓存**。

**练习 2**：为什么 `PassManager::run` 末尾要 `preserveSet<AllAnalysesOn<IRUnitT>>()`？

> **答案**：循环里每跑一个 pass 已经对本单元的分析做过逐个失效，循环结束时仍留在缓存里的分析都是有效的；用「整组保住」标记告知**外层**管理器「本单元内的分析不必再逐个检查」，省开销。

---

### 4.2 Pass 基类与 Result 模型

#### 4.2.1 概念说明

新 PM 一个反直觉但很重要的设计：**没有一个叫 `Pass` 的基类让你去继承**。文件开头的注释写得很直白——「There is no 'pass' interface in LLVM per se」（[PassManager.h:10-13](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/PassManager.h#L10-L13)）。只要一个类提供了符合签名的 `run` 方法，它就是个 pass。

那管理器怎么统一调度五花八门的 pass？答案是两层：

1. **CRTP mixin 提供元信息**。pass 继承 [`PassInfoMixin<自己>`](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/PassManager.h#L88-L99)，analysis 继承 [`AnalysisInfoMixin<自己>`](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/PassManager.h#L117-L139)。CRTP（Curiously Recurring Template Pattern，奇异递归模板）指的是「`struct Foo : Mixin<Foo>`」这种把自身作为模板参数传给父类的写法，让父类能拿到子类的真实类型，从而自动生成 `name()` 等信息。

2. **类型擦除统一存储**。`PassManager` 的队列里存的是 `PassConcept` 的指针（[PassManager.h:241-245](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/PassManager.h#L241-L245)），把「`T::run`」这个模板调用擦除成一个统一的函数指针调用。这样不同类型的 pass 能塞进同一个 `vector`。

**Result 模型**是 analysis 特有的约定：每个 analysis 类要用 `using Result = 某种类型;` 声明它的产物类型，并提供一个 `static AnalysisKey Key;` 作为身份 ID。管理器用这个 `Key` 的地址（`ID()`）作为缓存的键。pass 不需要 Result，它直接返回 `PreservedAnalyses`。

> 术语解释：**类型擦除（type erasure）**——用一个统一的「概念接口」隐藏具体类型，典型例子是 `std::function` 把各种可调用对象包装成同一类型。新 PM 的 `PassConcept` 干的是同样的事。

#### 4.2.2 核心流程

一个 pass 从「写出来」到「被管理器跑起来」：

```
你写的 struct MyPass : PassInfoMixin<MyPass> { PreservedAnalyses run(...) {...} }
                                    │
                  addPass(MyPass()) │  （PassManager.h:218-224）
                                    ▼
        PassModel::create 把 MyPass 包成 PassConcept（类型擦除）
                                    │
                                    ▼
              push 进 PassManager::Passes 这个 vector<unique_ptr<PassConcept>>
                                    │
        PassManager::run 遍历时     ▼
        PassConcept::run  ──函数指针──►  PassModel::runImpl  ──►  MyPass::run
```

对 analysis，注册侧的流程是「按 ID 登记一个可构造对象」：

```
AnalysisManager::registerPass([] { return DominatorTreeAnalysis(); })
        │  以 DominatorTreeAnalysis::Key 的地址为键存进 AnalysisPasses 表
        ▼
某 pass 调用 AM.getResult<DominatorTreeAnalysis>(F)
        │  getResultImpl 未命中缓存 → lookUpPass(ID) 拿到登记的对象 → P.run(F) 算结果 → 缓存
```

实际项目里你几乎不会手写 `registerPass`——`PassBuilder` 会批量帮你注册（见 4.2.3 末尾和代码实践）。

#### 4.2.3 源码精读

**CRTP mixin**。[PassManager.h:88-99](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/PassManager.h#L88-L99) 是 `PassInfoMixin`，它靠 `detail::InfoMixin<DerivedT>` 用类型名自动生成 `name()`；还有一个静态 `isRequired()` 表示「这个 pass 能否被插桩跳过」。两个子类 [`RequiredPassInfoMixin`](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/PassManager.h#L102-L105)（不可跳过，`isRequired()` 恒真）和 [`OptionalPassInfoMixin`](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/PassManager.h#L108-L111)（可跳过）。analysis 用的 [`AnalysisInfoMixin`](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/PassManager.h#L117-L139) 多了一个 `ID()`，返回子类必须提供的 `static AnalysisKey Key` 的地址，作为这个 analysis 类型的唯一身份。

**类型擦除**。[`PassConcept`](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/PassManagerInternal.h#L42-L102) 注释里点明了它「**不用虚函数**，避免 vtable 在 PIC 构建中的重定位开销和分发时多一次间接跳转」（第 38-40 行）。它的核心是几个函数指针成员：`Run`、`Destroy`、`PrintPipeline`（第 62-64 行），`run` 方法只是转调 `Run`（第 82-85 行）。[`PassModel`](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/PassManagerInternal.h#L108-L130) 是把具体 pass 包进 `PassConcept` 的模板包装，`runImpl` 第 125-129 行就是「转调具体 pass 的 `run`」。

**addPass**。[PassManager.h:218-224](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/PassManager.h#L218-L224) 里，`addPass` 用 `std::enable_if` 区分两种情况：普通 pass 被 `PassModel::create` 包一层塞进队列；若塞进来的是同类型 `PassManager`，则把它的 pass 直接「搬」过来（第 231-236 行），避免无意义的嵌套。

**真实 pass 样本：`SimplifyCFGPass`**。[SimplifyCFG.cpp:371-391](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/examples/IRTransforms/SimplifyCFG.cpp#L371-L391) 是一个最小可参照的 function pass：

```cpp
struct SimplifyCFGPass : public OptionalPassInfoMixin<SimplifyCFGPass> {
  PreservedAnalyses run(Function &F, FunctionAnalysisManager &FAM) {
    ...
    DominatorTree &DT = FAM.getResult<DominatorTreeAnalysis>(F);  // 消费 analysis
    ...
    return PreservedAnalyses::none();                              // 声明啥也没保住
  }
};
```

第 378 行 `FAM.getResult<DominatorTreeAnalysis>(F)` 正是 4.1 里讲的懒加载缓存的调用点——取支配树，没有就算、算完缓存。

**真实 analysis 样本：`DominatorTreeAnalysis`**。[Dominators.h:241-251](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/Dominators.h#L241-L251) 展示了 analysis 的标准三件套：

```cpp
class DominatorTreeAnalysis : public AnalysisInfoMixin<DominatorTreeAnalysis> {
  friend AnalysisInfoMixin<DominatorTreeAnalysis>;
  LLVM_ABI static AnalysisKey Key;        // (1) 身份 ID
public:
  using Result = DominatorTree;            // (2) Result 模型：产物类型
  LLVM_ABI DominatorTree run(Function &F, FunctionAnalysisManager &);  // (3) 计算
};
```

**注册入口：`PassBuilder`**。你不用手动注册每个 analysis。[PassBuilder.h:157-189](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/Passes/PassBuilder.h#L157-L189) 提供 `registerModuleAnalyses` / `registerFunctionAnalyses` / `registerCGSCCAnalyses` / `registerLoopAnalyses` / `registerMachineFunctionAnalyses` 五个入口，一次性把所有内置 analysis 塞进对应管理器。其实现 [PassBuilder.cpp:737-756](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/lib/Passes/PassBuilder.cpp#L737-L756) 借一个 X-Macro 文件 `PassRegistry.def` 展开 `FUNCTION_ANALYSIS(NAME, CREATE_PASS)` 宏，对每个 analysis 调 `FAM.registerPass(...)`。`crossRegisterProxies`（[PassBuilder.h:142-149](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/Passes/PassBuilder.h#L142-L149)）则负责把不同层级的管理器用 proxy analysis 互相挂接，让 function pass 能拿到 module 级分析。

#### 4.2.4 代码实践

> 这正是本讲规格里指定的实践任务。

**实践目标**：在 `PassBuilder.h` 里找到「注册」相关的入口，并分别给出一个 FunctionPass 和一个 FunctionAnalysis 的真实例子。

**操作步骤**：

1. 打开 [`include/llvm/Passes/PassBuilder.h`](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/Passes/PassBuilder.h)，定位 `PassBuilder` 类（[第 114 行](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/Passes/PassBuilder.h#L114)）。在它的 public 接口里找到注册入口，注意区分两类：
   - **analysis 的注册**：`registerFunctionAnalyses(FunctionAnalysisManager &FAM)`（[第 173 行](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/Passes/PassBuilder.h#L173)）——把 analysis 登记进管理器。
   - **pass 的注册（解析回调）**：`registerPipelineParsingCallback`（[第 590-614 行](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/Passes/PassBuilder.h#L590-L614)）——告诉解析器「遇到某个名字就构造哪个 pass」。
2. 翻看注册用的宏文件 [`lib/Passes/PassRegistry.def`](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/lib/Passes/PassRegistry.def)，它是 X-Macro 风格的「名单」。

**需要观察的现象与预期结果**：在 `PassRegistry.def` 中你能找到成对的 `FUNCTION_ANALYSIS` 与 `FUNCTION_PASS` 条目。各举一个真实例子：

- **FunctionAnalysis**：`FUNCTION_ANALYSIS("domtree", DominatorTreeAnalysis())`（[PassRegistry.def:372](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/lib/Passes/PassRegistry.def#L372)）——名字 `"domtree"`，由 `registerFunctionAnalyses` 登记进 `FunctionAnalysisManager`。
- **FunctionPass**：`FUNCTION_PASS("adce", ADCEPass())`（[PassRegistry.def:417](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/lib/Passes/PassRegistry.def#L417)）——名字 `"adce"`（死代码消除），可在命令行用 `-passes=adce` 调用。

**关键区分**（务必想清楚）：`FUNCTION_ANALYSIS` 既被 `registerFunctionAnalyses` 用来注册，也被解析器用来识别 `-passes='print<domtree>'` 这类写法；而 `FUNCTION_PASS` 只用于流水线解析（构造 pass 对象塞进 `PassManager`），它**不**进 `AnalysisManager`——因为 pass 不缓存、不共享，每次按流水线构造。

#### 4.2.5 小练习与答案

**练习 1**：`SimplifyCFGPass` 继承的是 `OptionalPassInfoMixin`，`DominatorTreeVerifierPass` 继承的是 `RequiredPassInfoMixin`（[Dominators.h:265-268](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/Dominators.h#L265-L268)）。这两者在 `runBeforePass` 的插桩里会有什么差别？

> **答案**：`isRequired()` 为真的（`RequiredPassInfoMixin`）不会被跳过——[PassInstrumentation.h:237-241](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/PassInstrumentation.h#L237-L241) 里只有 `!isRequired(Pass)` 时才会去问 `ShouldRunOptionalPassCallbacks`，验证器这种必须跑的 pass 直接放行。

**练习 2**：为什么 `DominatorTreeAnalysis` 要单独声明 `using Result = DominatorTree`，而 `SimplifyCFGPass` 不需要 Result？

> **答案**：analysis 的产物要被**缓存**并**被别的 pass 取用**，管理器需要一个统一的「结果」类型来存储和返回（`getResult` 的返回类型就是 `PassT::Result`）。pass 不缓存、不共享产物，它的「产物」就是返回的 `PreservedAnalyses`，已固定在 `run` 签名里，不需要 Result 约定。

**练习 3**：`PassConcept` 为什么坚持不用虚函数？

> **答案**：注释（[PassManagerInternal.h:38-40](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/PassManagerInternal.h#L38-L40)）说，vtable 在 PIC（位置无关代码）构建里要占不少需要重定位的存储，且分发时多一次间接跳转。新 PM 用手工函数指针做类型擦除，省掉这些开销。

---

### 4.3 PassInstrumentation 与回调

#### 4.3.1 概念说明

`PassInstrumentation`（pass 插桩）是新 PM 的「可观测性 + 可插拔」层。它给 pass / analysis 的执行前后埋了一组**回调点**，让外部逻辑能在不修改 pass 源码的前提下，做这些事：

- **计时与统计**：`-time-passes` 就是挂了个 `AfterPass` 回调来累计每个 pass 的耗时。
- **调试追踪**：崩溃时打印「正在哪个 pass 上」（`PrettyStackTraceEntry`）。
- **控制执行**：`BeforePass` 回调返回 `false` 可以**跳过**一个可选 pass（比如 `-opt-bisect-limit` 二分定位 bug）。
- **改变量监控**：对比 pass 前后的 IR，统计指令数变化。

这里有两个类，别搞混：

- [`PassInstrumentationCallbacks`](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/PassInstrumentation.h#L74-L202)：**注册中心**。你往它里面 `registerXxxCallback` 塞各种回调，它帮你存起来。
- [`PassInstrumentation`](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/PassInstrumentation.h#L206-L339)：**执行入口**。`PassManager::run` 拿着它在各个埋点调用 `runBeforePass` / `runAfterPass` 等，内部转调注册中心里登记的回调。

巧妙的配合是：`PassInstrumentation` 本身是作为一个 **analysis**（`PassInstrumentationAnalysis`）发给管理器的——`PassManager::run` 一开始就从 `AnalysisManager` 把它「取」出来（[PassManagerImpl.h:62-64](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/PassManagerImpl.h#L62-L64)），这样它顺着现成的 analysis 机制流到每个 pass 管理器手里，无需额外传参。

#### 4.3.2 核心流程

`PassManager::run` 里每个 pass 的完整插桩时序（与 4.1.2 的循环对应）：

```
对每个 pass：
  shouldRun = PI.runBeforePass(pass, IR)
      ├─ 若 pass 是可选的：依次问所有 ShouldRunOptionalPassCallbacks，任一返回 false 则 shouldRun=false
      ├─ shouldRun 为真：触发所有 BeforeNonSkippedPassCallbacks
      └─ shouldRun 为假：触发所有 BeforeSkippedPassCallbacks，跳过本 pass
  若 shouldRun：
      PassPA = pass.run(...)
      AM.invalidate(...)
      PI.runAfterPass(pass, IR, PassPA)        # 触发 AfterPassCallbacks（可拿到 PA）
```

analysis 侧也有对应埋点（在 `getResultImpl` 里）：`runBeforeAnalysis` → `analysis.run` → `runAfterAnalysis`；当分析结果被失效删除时还有 `runAnalysisInvalidated`。

各回调点的语义对照表：

| 回调点 | 触发时机 | 能否控制执行 | 签名关键参数 |
| --- | --- | --- | --- |
| `ShouldRunOptionalPass` | 可选 pass 跑之前 | **能**（返回 false 跳过） | `(pass 名, IR)` |
| `BeforeNonSkippedPass` | 确定要跑的 pass 之前 | 否 | `(pass 名, IR)` |
| `BeforeSkippedPass` | 被跳过的 pass 之前 | 否 | `(pass 名, IR)` |
| `AfterPass` | pass 跑完、IR 仍有效 | 否 | `(pass 名, IR, PA)` |
| `BeforeAnalysis` / `AfterAnalysis` | analysis 计算前后 | 否 | `(analysis 名, IR)` |
| `AnalysisInvalidated` | 分析结果被失效时 | 否 | `(analysis 名, IR)` |

#### 4.3.3 源码精读

**回调类型签名**集中定义在 [PassInstrumentation.h:86-94](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/PassInstrumentation.h#L86-L94)，注意 IR 单元是用 `llvm::Any` 包装的 `const IRUnitT*` 传进去的（第 86、88 行），刻意用 `const` 防止回调里误改 IR。注册方法是一组模板，比如 [PassInstrumentation.h:118-124](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/PassInstrumentation.h#L118-L124) 的 `registerAfterPassCallback`、[PassInstrumentation.h:103-106](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/PassInstrumentation.h#L103-L106) 的 `registerShouldRunOptionalPassCallback`。

**最该读的是 `runBeforePass`**：[PassInstrumentation.h:232-252](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/PassInstrumentation.h#L232-L252)。

```cpp
template <typename IRUnitT, typename PassT>
bool runBeforePass(const PassT &Pass, const IRUnitT &IR) const {
  if (!Callbacks) return true;                       // 没注册任何回调，直接放行
  bool ShouldRun = true;
  if (!isRequired(Pass))                              // 只对可选 pass 问「要不要跳过」
    for (auto &C : Callbacks->ShouldRunOptionalPassCallbacks)
      ShouldRun &= C(Pass.name(), llvm::Any(&IR));
  if (ShouldRun)
    for (auto &C : Callbacks->BeforeNonSkippedPassCallbacks) C(...);
  else
    for (auto &C : Callbacks->BeforeSkippedPassCallbacks) C(...);
  return ShouldRun;                                   // 返回值决定 pass 是否真的跑
}
```

第 238 行的 `!isRequired(Pass)` 呼应 4.2.5 练习 1：必跑 pass 不参与「跳过」投票。`runAfterPass` 与 analysis 的 `runBeforeAnalysis`/`runAfterAnalysis` 结构相同，只是遍历各自的回调列表（[PassInstrumentation.h:257-292](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/PassInstrumentation.h#L257-L292)）。

**调用点**就在 4.1.3 的主循环里：[PassManagerImpl.h:73](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/PassManagerImpl.h#L73)（`runBeforePass`）、[第 84 行](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/PassManagerImpl.h#L84)（`runAfterPass`）。崩溃追踪用的是同一段循环里的 `StackTraceEntry`（[PassManagerImpl.h:30-54](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/PassManagerImpl.h#L30-L54)），它的 `print` 会输出「Running pass "xxx" on 函数名」。

**`PassInstrumentationAnalysis`**：[PassInstrumentation.h:346-365](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/PassInstrumentation.h#L346-L365)。它是一个「伪 analysis」——`run` 啥也不算，只是把持有 `Callbacks` 指针的 `PassInstrumentation` 当结果返回（第 362-364 行）。这样管理器就能用 `getResult` 把插桩入口取出来，复用整套 analysis 基础设施。它的 `invalidate` 恒返回 `false`（第 317-321 行），因为它永远不过期。

#### 4.3.4 代码实践

**实践目标**：用现成的 `-time-passes` 选项体验 `AfterPass` 回调的产物，直观看见插桩的「可观测」能力。

**操作步骤**：

1. 复用 4.1.4 的 `t.ll`（或任意 `.ll`）。
2. 跑一个带几个 pass 的流水线并计时：

   ```bash
   opt -passes='mem2reg,instcombine' -time-passes -disable-output t.ll
   ```

**需要观察的现象**：

- 终端会打印一张 `=== ... Pass execution timing report ===` 的表格，列出每个 pass 的耗时（如 `mem2reg`、`instcombine`）。

**预期结果**：表格里能看到本次跑过的每个 pass 名字及其耗时。这正是因为 `opt` 的驱动在 `PassInstrumentationCallbacks` 上注册了 `BeforeNonSkippedPass` / `AfterPass` 回调来记时间戳，而 `PassManager::run` 在主循环里按时调用了这些埋点。

> `-time-passes` 默认在新 PM 下开启；具体表格格式与精度随版本和构建配置略有差异，**待本地验证**确切输出。

#### 4.3.5 小练习与答案

**练习 1**：`PassInstrumentationAnalysis::invalidate` 恒返回 `false`（[PassInstrumentation.h:317-321](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/PassInstrumentation.h#L317-L321)）。为什么它绝不能被失效？

> **答案**：`PassManager::run` 开头就要 `getResult<PassInstrumentationAnalysis>` 取插桩入口（[PassManagerImpl.h:62-64](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/PassManagerImpl.h#L62-L64)），后续每个 pass 都依赖它。若它被失效删除，下个 pass 取的时候会重算（虽然结果一样），更重要的是语义上插桩入口必须始终在场。

**练习 2**：如果一个 `ShouldRunOptionalPassCallback` 返回 `false`，那个 pass 还会触发 `AfterPass` 回调吗？

> **答案**：不会。`runBeforePass` 返回 `false` 后，主循环第 73-74 行 `continue` 直接跳过该 pass，根本不会执行 `Pass->run` 和第 84 行的 `runAfterPass`；只会触发 `BeforeSkippedPassCallbacks`。

---

## 5. 综合实践

把本讲三个模块串起来，做一个「**源码阅读 + 命令行验证**」的闭环任务：

**任务背景**：下面这条命令把本讲涉及的几乎所有概念都串了起来：

```bash
opt -passes='mem2reg,instcombine' -time-passes -disable-output t.ll
```

请你完成以下三件事，画出一条「数据/控制流」并标注每一步对应的源码：

1. **注册阶段（对应 4.2 + 4.3）**：在 `PassBuilder.h` 里找到 `registerFunctionAnalyses` 和 `registerPipelineParsingCallback`，说明它们分别负责「把 analysis 登记进 `FunctionAnalysisManager`」和「把 `mem2reg`/`instcombine` 这样的名字映射到具体 pass 构造」。

2. **运行阶段（对应 4.1）**：参照 [`PassManagerImpl.h:56-95`](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/PassManagerImpl.h#L56-L95) 的主循环，按顺序写出 `mem2reg` 这个 pass 被跑起来时经历的 5 个动作（`runBeforePass` → `pass.run` → `AM.invalidate` → `runAfterPass` → `PA.intersect`），并指出 `mem2reg` 内部若取支配树，会走到 [`getResultImpl`](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/PassManagerImpl.h#L131-L162) 的缓存逻辑。

3. **观测阶段（对应 4.3）**：实际运行上面的命令，把 `-time-passes` 打印出的表格贴出来；然后回答：表格里 `mem2reg` 和 `instcombine` 各自的耗时数字，是挂在 `PassInstrumentationCallbacks` 的哪个回调点上采集到的？（答：`BeforeNonSkippedPass` 记开始时间戳、`AfterPass` 记结束时间戳并求差。）

**验收标准**：你能用一句话说清「pass 改 IR、analysis 读 IR 且被缓存、插桩在两者前后埋点」这三件事各自的源码入口，本讲就过关了。

## 6. 本讲小结

- 新 PM 把工作分成两个管理器：`PassManager` 管有序的 pass 队列并改 IR，`AnalysisManager` 管懒加载的分析结果缓存且只读 IR。
- 主循环 [`PassManager::run`](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/PassManagerImpl.h#L28-L98) 的五步动作（BeforePass → run → invalidate → AfterPass → intersect）是新 PM 的心脏。
- 失效是**精确到单个分析、按需驱动**的：每个 pass 用 `PreservedAnalyses` 声明保住了什么，管理器据此删除过期分析；分析间依赖通过 `Invalidator` 现场递归判定。
- 新 PM **没有 pass 基类**：靠 CRTP mixin（`PassInfoMixin`/`AnalysisInfoMixin`）给元信息，靠 `PassConcept`/`PassModel` 做类型擦除（用函数指针而非虚函数）统一存储与调度。
- analysis 用 `using Result = ...` 声明产物、用 `static AnalysisKey Key` 做身份；`getResult` 触发懒加载缓存。
- `PassInstrumentation` 通过一组 Before/After 回调点提供可观测性与可插拔能力（`-time-passes`、二分 bisect 等），它本身伪装成一个永不过期的 analysis 流转。

## 7. 下一步学习建议

- **u3-l2 编写一个 Pass**：本讲只读了 `SimplifyCFGPass` 的皮毛，下一讲会带你亲手写一个 `PassInfoMixin` pass，并把它注册进流水线跑通。
- **u3-l3 Pass 流水线与 PassBuilder**：本讲的 `PassBuilder` 只讲了「注册」这一半，下一讲深入 [`parsePassPipeline`](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/Passes/PassBuilder.h#L364)，看 `-passes=...` 文本如何变成 pass 队列，以及默认 O1/O2/O3 流水线如何组装。
- **延伸阅读**：`PassManager.h` 文件头注释里提到的 Sean Parent 演讲「Value Semantics and Concept-based Polymorphism」是理解 `PassConcept` 类型擦除设计的最佳背景资料；想看真实 pass 如何消费 analysis，可继续精读 [`SimplifyCFG.cpp`](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/examples/IRTransforms/SimplifyCFG.cpp) 的三个版本（v1/v2/v3）。
