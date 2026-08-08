# 优化 Pass 框架

## 1. 本讲目标

经过第三单元，你已经知道 XLS IR 是一张「数据流图 + 类型化值」。那么，把 `.ir` 变得更小、更快、更利于综合，是谁在做？答案是 `xls/passes/` 下的一整套**优化 Pass（optimization pass）**。

本讲不教你某个具体优化（那是 u4-l2 的事），而是带你理解**承载所有优化的那套框架**。学完本讲你应当能够：

- 说清一个 Pass 的「统一接口」是什么：输入 IR、输出「是否改动」布尔值，以及框架在 `Run()` 前后替你做的度量、校验、冗余跳过。
- 理解「复合 Pass（compound pass）」与「不动点（fixedpoint）迭代」这两个组合原语，以及 `opt_level` 包装器如何按优化等级开关 Pass。
- 看懂数据驱动的标准管线：Pass 先用 `REGISTER_OPT_PASS` 宏注册进单例注册表，标准管线则由 `optimization_pass_pipeline.txtpb` 这份文本描述，运行时才拼装成内存中的 Pass 树。
- 亲手用 `opt_main --list_passes` 列出全部 Pass，并在管线描述里定位它们的相对顺序。

## 2. 前置知识

- **编译器 Pass 的直觉**：一个 Pass 就是「读入 IR 图、按某种规则改写它、返回一个布尔值表示这次有没有改」。把很多 Pass 串起来跑，IR 就被一步步精简。这和 LLVM 的 Pass、GCC 的 GIMPLE pass 是一类东西。
- **不动点（fixed point）**：如果一遍跑下来「没有改动」，说明再跑也不会变了——就到达了不动点。许多优化要反复跑到不动点（例如常量折叠可能又制造出新的可消除代码）。
- **u3-l1 的 IR 心智模型**：`Package` 持有 `Function/Proc/Block`，它们都继承自 `FunctionBase`，内部用 `std::list<Node>` 存数据流图节点。本讲框架里「作用域」概念（函数级 / 进程级 / 块级）就建立在 `FunctionBase` 之上。
- **`opt_main` 的角色（u1-l5）**：它是「薄壳」，真正干活的是库函数 `OptimizeIrForTop`；本讲会从这层壳一路追到 Pass 框架内部。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [xls/passes/pass_base.h](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/pass_base.h) | Pass 的**通用基类**：`PassBase` 模板、`CompoundPassBase`、`FixedPointCompoundPassBase`，以及函数/进程/块级 Pass 基类。框架的「骨架」全在这里。 |
| [xls/passes/pass_registry.h](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/pass_registry.h) | `PassRegistry` 模板：「名字 → 生成器」的线程安全查表容器。 |
| [xls/passes/optimization_pass.h](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/optimization_pass.h) | 把通用模板**具现化**为优化专用的类型别名（`OptimizationPass` 等）、`OptimizationPassOptions`、`OptimizationContext`、按 `opt_level` 开关 Pass 的包装器。 |
| [xls/passes/optimization_pass_registry.h](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/optimization_pass_registry.h) / [.cc](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/optimization_pass_registry.cc) | 优化 Pass 的单例注册表 `GetOptimizationRegistry()`、`REGISTER_OPT_PASS` 宏、把 `.txtpb` 注册成管线的 `RegisterPipelineProto`。 |
| [xls/passes/pipeline_generator.h](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/pipeline_generator.h) | `PipelineGeneratorBase`：把文本/proto 形式的管线描述**拼装**成内存中的复合 Pass 树。 |
| [xls/passes/optimization_pass_pipeline.h](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/optimization_pass_pipeline.h) / [.cc](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/optimization_pass_pipeline.cc) | 构造并运行「标准优化管线」的入口：`CreateOptimizationPassPipeline`、`RunOptimizationPassPipeline`。 |
| [xls/passes/optimization_pass_pipeline.txtpb](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/optimization_pass_pipeline.txtpb) | 标准管线的**真实顺序定义**：一份文本 proto，列出所有复合 Pass 与 `default_pipeline`。 |
| [xls/tools/opt_main.cc](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/tools/opt_main.cc) / [opt.cc](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/tools/opt.cc) | 命令行入口（`--list_passes` 等）与库实现（`OptimizeIrForTop`）。 |
| [xls/passes/dce_pass.h](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/dce_pass.h) / [arith_simplification_pass.h](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/arith_simplification_pass.h) | 两个具体 Pass 范例：死代码消除（`dce`）与算术化简（`arith_simp`）。 |

---

## 4. 核心概念与源码讲解

### 4.1 Pass 基类与统一接口

#### 4.1.1 概念说明

XLS 的 Pass 框架刻意做成**与「优化」解耦的通用基础设施**。它定义在 `pass_base.h` 里，是一个被 `OptionsT`（选项类型）和 `ContextT...`（可变上下文类型）参数化的模板。把选项换成 `OptimizationPassOptions`、上下文换成 `OptimizationContext`，就得到了优化专用的 Pass 体系（见 4.3）。

每一个 Pass，无论多简单或多复杂，最终都遵守同一条契约：

> 给定 `Package* ir`、一份不可变 `options`、一个可写的 `PassResults* results`（以及若干上下文），返回 `absl::StatusOr<bool>`——其中 `bool` 表示**这一趟有没有改动 IR**。

这个布尔值是整个框架的「信号量」：复合 Pass 用它判断要不要再跑一轮（不动点），框架用它做冗余跳过、用它决定要不要跑不变式校验。

#### 4.1.2 核心流程

一个 Pass 被调用时的总流程（由基类 `PassBase::Run` 统一把关，子类只实现 `RunInternal`）：

```text
Run(ir, options, results, context...)
  │
  ├─ 1. 触顶 bisect_limit ?  → 直接返回 false（编译器「燃料」耗尽）
  ├─ 2. 在 skip_passes 黑名单里 ?  → 返回 false
  ├─ 3. 计算 redundancy_signature；若 IsKnownRedundant → 标记跳过、不真正跑
  ├─ 4. 计时 + 记录节点数（ScopedPassInvocation）
  ├─ 5. 调用子类的 RunInternal(...)  ← 真正的图变换在这里
  ├─ 6. 断言：返回 true ⇔ 节点数确有变化（DEBUG 下做完整文本比对）
  └─ 7. 若改动且非复合 Pass → 跑 invariant checkers（不变式校验）
```

关键在于：**子类只写第 5 步**，1~4 和 6~7 全部由基类代办。这是「统一接口」的真正含义。

#### 4.1.3 源码精读

**入口 `Run()` 与「返回 true ⇔ 真的变了」的断言。** 这是整个框架最值得读的一段，它把度量、跳过、校验全集中到了一处：

[pass_base.h:414-466](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/pass_base.h#L414-L466) —— `Run()` 前半段：bisect_limit 与 skip_passes 的提前返回、构造 `ScopedPassInvocation`、查冗余签名。

```cpp
absl::StatusOr<bool> Run(Package* ir, const OptionsT& options,
                         PassResults* results, ContextT&... context) const {
  XLS_RET_CHECK(results != nullptr) << "Results cannot be null";
  if (options.bisect_limit &&
      results->total_invocations() >= options.bisect_limit) {
    return false;   // (1) 编译燃料耗尽
  }
  // ... (2) skip_passes 黑名单检查 ...
  // ... (3) 冗余签名检查，命中则 set_skipped(kKnownRedundant) ...
  ScopedPassInvocation invocation(results, PassInfo{...}, ir);
  // ... (5) 真正调用子类逻辑：
  XLS_ASSIGN_OR_RETURN(
      invocation.changed(),
      RunInternal(ir, options, results, context...), ...);
```

[pass_base.h:483-487](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/pass_base.h#L483-L487) —— 第 6 步的快速一致性断言：如果 Pass 声称「没改」，那节点数必须真没变，否则直接 `RET_CHECK` 失败。这是防止「忘了把 `changed` 设成 true」类 bug 的第一道防线。

[pass_base.h:493-503](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/pass_base.h#L493-L503) —— 第 7 步：只有「确实改动」且「非复合 Pass」时才跑不变式校验器（复合 Pass 会在自己内部各处校验，跑外面这层是冗余）。

**子类要实现的唯一纯虚函数：**

[pass_base.h:578-581](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/pass_base.h#L578-L581) —— `RunInternal` 是子类真正的「干活」入口。

```cpp
virtual absl::StatusOr<bool> RunInternal(Package* ir, const OptionsT& options,
                                         PassResults* results,
                                         ContextT&... context) const = 0;
```

**按作用域遍历的便利基类。** 大多数优化不需要遍历整个 `Package`，而是「对每个 Function/Proc/Block 各做一次」。框架提供了 `FunctionBasePass`（同时覆盖 Function/Proc/Block）、`FunctionPass`、`ProcPass`、`BlockPass`，把遍历逻辑抽走，子类只需实现 `RunOnFunctionBaseInternal`：

[pass_base.h:928-942](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/pass_base.h#L928-L942) —— `FunctionBasePass::RunInternal`：遍历 `p->GetFunctionBases()`，逐个调用子类的 `RunOnFunctionBaseInternal`，并把各次的 `changed`「或」起来。

**一个真实例子：死代码消除（DCE）。** 看 `dce_pass.h` 就能验证上面这套抽象如何落地：

[dce_pass.h:142-162](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/dce_pass.h#L142-L162) —— `DeadCodeEliminationPass` 继承 `OptimizationFunctionBasePass`（即具现化后的 `FunctionBasePass`），声明 `kName = "dce"`、`IsIdempotent() = true`、`GetRedundancyGuard()` 返回 `CanSkip()`，并只实现 `RunOnFunctionBaseInternal`。注意它**完全不需要**关心 bisect_limit、skip_passes、校验、计时——这些都由 `Run()` 包办了。

> 关键结论：`IsIdempotent()` 与 `RedundancyGuard::CanSkip()` 是一对性能优化声明——告诉框架「我连跑两遍、中间 IR 没变的话，第二遍一定是 no-op，可以跳过」。DCE、算术化简等大量重复出现的 Pass 都启用了它，这是标准管线里同一个 `dce` 能出现几十次却不会拖慢编译的原因之一。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：验证「子类只实现 `RunInternal` / `RunOnFunctionBaseInternal`，其余由基类代办」这一论断。
2. **步骤**：
   - 打开 `xls/passes/dce_pass.h` 与 `xls/passes/dce_pass.cc`，确认 `DeadCodeEliminationPass` 没有重写 `Run()`。
   - 打开 `xls/passes/arith_simplification_pass.h`，对比它的结构与 DCE 是否一致（同样继承 `OptimizationFunctionBasePass`、同样有 `kName`）。
3. **观察现象**：两个 Pass 的头文件结构几乎一模一样，差异只在 `kName` 的取值（`"dce"` vs `"arith_simp"`）和具体算法。
4. **预期结果**：你会确信「写一个新 Pass = 继承某个作用域基类 + 实现 `RunOnXxxInternal` + 声明 `kName`」，框架其余部分无需触碰。
5. 待本地验证（无需运行，纯阅读）。

#### 4.1.5 小练习与答案

**练习 1**：如果一个 Pass 改了 IR 却忘了让 `RunInternal` 返回 `true`，会发生什么？
**参考答案**：`Run()` 第 6 步的快速断言（[pass_base.h:483-487](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/pass_base.h#L483-L487)）会因「声明未改但节点数变了」而 `RET_CHECK` 失败；在 DEBUG 构建里还会做完整文本比对并报 `InternalError`。所以框架会强制你诚实返回。

**练习 2**：`PassOptionsBase` 里的 `bisect_limit` 有什么用？
**参考答案**：它是一个「最多允许执行多少趟 Pass」的上限（[pass_base.h:71-76](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/pass_base.h#L71-L76)），用作编译器「燃料」。一旦累计调用数达到上限，后续 Pass 全部 `return false` 不再改动。它是二分定位「哪个 Pass 引入问题」时的核心手段（见 `opt_main` 的 `--passes_bisect_limit`）。

---

### 4.2 复合 Pass、不动点迭代与 opt_level 包装

#### 4.2.1 概念说明

单个 Pass 能力有限，真实优化是「一串 Pass」的组合。框架用两类**复合 Pass**来表达组合：

- **`CompoundPassBase`（普通复合）**：按顺序跑完自己的子 Pass 们，把各趟 `changed` 或起来返回。像「跑一遍 simplification」。
- **`FixedPointCompoundPassBase`（不动点复合）**：把子 Pass 们**反复跑到没有改动为止**。像「反复 simplification 直到收敛」。

此外，`OptimizationPassOptions` 里的 `opt_level`（0~3）控制优化激进程度。框架用一组**包装器（wrapper）Pass**把它实现为「按条件启用/封顶」——`IfOptLevelAtLeast<k>`（不到 k 级就跳过）、`CapOptLevel<k>`（封顶到 k 级）、`WithOptLevel<k>`（强制设成 k 级）。这样管线里可以直接声明「这段只在 `opt_level >= 3` 时跑」，而不必在每个 Pass 内部手写判断。

#### 4.2.2 核心流程

**不动点循环**是本模块最核心的算法。`FixedPointCompoundPassBase::RunNested` 的逻辑可写成：

```text
local_changed = true
while local_changed:                       # 还在变就继续
    local_changed = 依次跑完所有子 Pass 的「或」结果
    若到达 bisect_limit → break
    若 local_changed:
        DumpIr（可选）
        RestartCurrentInvocation()         # 重新计数这一轮
返回「迭代次数 > 1 或 最后一轮有改动」
```

这是一个经典的**单调不动点求值**：只要图还在变就继续，直到一轮里所有子 Pass 都返回 `false`——图达到稳定形态。记 `C(I)` 为「跑一遍子 Pass 序列」对图 `I` 的变换，则循环结束时满足 \( C(I^*) = I^* \)，即 \( I^* \) 是 \( C \) 的不动点。因为每个化简 Pass 都不会让图变大（节点数单调不增），循环必然终止。

**opt_level 如何起作用**：以 `CapOptLevel` 为例，它包装一个内层 Pass，运行前把传给内层的 `options.opt_level` 改成 `min(k, options.opt_level)`。`IfOptLevelAtLeast<k>` 则在 `options.opt_level < k` 时直接 `return false` 跳过内层。于是「优化等级」成了一个可在管线描述里随处摆放的开关。

#### 4.2.3 源码精读

**普通复合 Pass：顺序遍历子 Pass。**

[pass_base.h:886-900](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/pass_base.h#L886-L900) —— `CompoundPassBase::RunNested` 的主循环：对 `passes_` 里每个子 Pass 调 `Run()`，把结果「或」进 `changed`。注意开头还会先跑一遍 weak/invariant checkers（[pass_base.h:877-884](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/pass_base.h#L877-L884)），结尾若有改动再跑一遍——这就是「不变式校验夹住每个 Pass」的实现。

[pass_base.h:689-703](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/pass_base.h#L689-L703) —— `Add<T>(args...)`：复合 Pass 用这个模板方法添加子 Pass，内部构造后存进 `passes_`（`unique_ptr` 持有）和 `pass_ptrs_`（裸指针视图）。

**不动点复合 Pass：while 循环到收敛。**

[pass_base.h:798-841](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/pass_base.h#L798-L841) —— `FixedPointCompoundPassBase::RunNested`：`while (local_changed)` 反复调用父类的 `RunNested`，每轮有改动就 `RestartCurrentInvocation()`（让度量记录这是同一复合 Pass 的第 N 次迭代）。注意它把 `IsIdempotent()` 重写为 `true`（[pass_base.h:796](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/pass_base.h#L796)）——不动点收敛后当然再跑也不变。

**opt_level 包装器。**

[optimization_pass.h:682-701](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/optimization_pass.h#L682-L701) —— `CapOptLevel<kLevel, InnerPass>` 与 `IfOptLevelAtLeast<kLevel, InnerPass>`：模板化的包装器，前者封顶 opt_level，后者作为最低门槛开关。

[optimization_pass.h:778-787](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/optimization_pass.h#L778-L787) —— 两个门槛函数：`SplitsEnabled(level) = level >= 3`、`NarrowingEnabled(level) = level >= 2`。即「分裂类优化」要 3 级、「收窄类优化」要 2 级才启用。具体 Pass 内部会查 `options.narrowing_enabled()` 决定要不要做某类变换。

#### 4.2.4 代码实践（源码阅读型）

1. **目标**：在标准管线描述里找到「不动点」与「opt_level 封顶」的真实使用。
2. **步骤**：
   - 打开 `xls/passes/optimization_pass_pipeline.txtpb`。
   - 找到 `short_name: "fixedpoint_simp"` 这条复合 Pass（[txtpb:91-100](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/optimization_pass_pipeline.txtpb#L91-L100)）：它 `passes: ["simp"]` 且 `fixedpoint: true`，即「把 simplification 跑到不动点」。
   - 找到 `short_name: "simp(2)"`（[txtpb:113-124](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/optimization_pass_pipeline.txtpb#L113-L124)）：它 `options: { cap_opt_level: 2 }`，即「跑 simplification，但 opt_level 封顶到 2（不做分裂类优化）」。
3. **观察现象**：同一份 `simp`（simplification 复合 Pass）被以不同「包装」反复复用——有的包成不动点、有的封顶到 2 或 3 级。
4. **预期结果**：你会理解到「复合 Pass + 包装器」是一个高度可组合的体系：少量基础复合 Pass 经由 `fixedpoint` 与 `cap_opt_level` 修饰，就能拼出标准管线上百处用法。
5. 待本地验证（无需运行，纯阅读）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `FixedPointCompoundPassBase` 要把 `IsIdempotent()` 重写成 `true`？
**参考答案**：因为它会一直跑到「一轮没有任何改动」才返回，返回时图已在其子 Pass 序列的不动点上。再跑一遍输入完全相同，必然仍无改动，故幂等。声明幂等让框架的「冗余跳过」可以把它整段省掉。

**练习 2**：`CapOptLevel<2>` 和 `IfOptLevelAtLeast<2>` 在「用户指定 `--opt_level=1`」时分别表现如何？
**参考答案**：`CapOptLevel<2>` 会把内层 opt_level 钳到 `min(2,1)=1` 然后照常跑（不跳过）；`IfOptLevelAtLeast<2>` 看到 `1 < 2`，直接 `return false` 跳过内层 Pass。

---

### 4.3 Pass 注册表与数据驱动的标准管线

#### 4.3.1 概念说明

了解了「单个 Pass」与「复合 Pass」两类积木，剩下的问题就是：**这些积木从哪里来、按什么顺序拼？** XLS 给出的答案非常工程化——**数据驱动**：

1. 每个 Pass 类通过宏 `REGISTER_OPT_PASS` 在程序启动时把自己**注册**进一个全局单例注册表 `GetOptimizationRegistry()`，键就是它的 `kName`（如 `"dce"`、`"arith_simp"`）。
2. 标准管线的**顺序**不再写死在 C++ 里，而是写在一份文本 proto `optimization_pass_pipeline.txtpb` 里。这份 proto 同样在启动时被注册成一个名为 `default_pipeline` 的「复合 Pass 生成器」。
3. 运行 `opt_main` 时，框架向注册表要 `default_pipeline` 的生成器，生成出整棵 Pass 树，再交给 4.1/4.2 的 `Run()` 驱动。

这套设计的好处：加一个新 Pass 只需「写类 + `REGISTER_OPT_PASS` + 在 BUILD 里登记」；调整管线顺序只改 `.txtpb`，无需重写 C++、无需手动维护一份 `Add<XxxPass>()` 的硬编码列表。

#### 4.3.2 核心流程

**注册阶段（程序启动，由 module initializer 驱动）：**

```text
每个 xxx_pass.cc:  REGISTER_OPT_PASS(XxxPass)
        └─ module initializer ──> RegisterOptimizationPass<XxxPass>("xxx", ...)
                                        └─ GetOptimizationRegistry().Register("xxx", generator)

BUILD(管道宏):      optimization_pass_pipeline.txtpb
        └─ 生成的 pipeline_registration.cc ──> RegisterOptimizationPipelineProtoData(...)
                └─ RegisterPipelineProto(proto)
                        ├─ 每个 compound_passes[i]  → Register(short_name, CompoundPassAdder)
                        └─ default_pipeline         → Register("default_pipeline", CompoundPassAdder)
```

**构造与运行阶段（opt_main 调用时）：**

```text
OptimizeIrForTop
  └─ CreateOptimizationPassPipeline
        └─ TryCreateOptimizationPassPipeline
              └─ registry.Generator("default_pipeline")->Generate()   # 生成整棵树
                    └─ CompoundPassAdder::Generate 递归：对每个名字查 registry.Generator(name)
        └─ 顶层 top = OptimizationCompoundPass，挂上生成的树 + invariant checkers
  └─ pipeline->Run(package, opt_options, &results, context)
        └─ Run/RunNested/FixedPoint 递归驱动（见 4.1 / 4.2）
```

注意 `CompoundPassAdder::Generate` 是**递归**的：复合 Pass 的子元素若仍是复合 Pass 名（如 `simp`），它会再查表、再生成，于是从 `default_pipeline` 出发能还原出整棵多层嵌套的 Pass 树。

#### 4.3.3 源码精读

**注册表本体：名字 → 生成器。**

[pass_registry.h:41-76](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/pass_registry.h#L41-L76) —— `PassGenerator` 抽象：唯一的虚方法是 `Generate()`，返回一个新构造的 `PassBase` 实例。注册表存的就是这种「能造 Pass 的工厂对象」。

[pass_registry.h:80-279](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/pass_registry.h#L80-L279) —— `PassRegistry` 模板：内部是 `absl::flat_hash_map<string, GeneratorPtr>`，用 `absl::Mutex` 保证线程安全；核心方法 `Register(name, gen)`（[pass_registry.h:130-139](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/pass_registry.h#L130-L139)）、`Generator(name)`（[pass_registry.h:142-146](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/pass_registry.h#L142-L146)）、`GetRegisteredNames()`（[pass_registry.h:148-151](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/pass_registry.h#L148-L151)，`--list_passes` 就是靠它）。`NotFound` 时的错误信息会列出全部已注册名字（[pass_registry.h:221-226](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/pass_registry.h#L221-L226)），对排错极友好。

**单例注册表与「注册一个 Pass」的便捷函数。**

[optimization_pass_registry.cc:33-36](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/optimization_pass_registry.cc#L33-L36) —— `GetOptimizationRegistry()`：函数内 `static` 局部变量，天然线程安全、避免静态初始化顺序问题。

[optimization_pass_registry.h:126-132](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/optimization_pass_registry.h#L126-L132) —— `RegisterOptimizationPass<PassT>(name, args...)`：构造一个 `Adder` 生成器（它保存了构造 Pass 所需的参数元组）并注册。

**`REGISTER_OPT_PASS` 宏：一行完成注册。**

[optimization_pass_registry.h:156-161](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/optimization_pass_registry.h#L156-L161) —— 宏展开成一个 module initializer，启动时调用 `RegisterOptimizationPass<ty>(ty::kName, ...)`。这就是为什么每个 Pass 类都要有 `static constexpr std::string_view kName`（见 dce_pass.h 的 `kName = "dce"`）。它还顺带记录类名/头文件信息，用于自动生成文档。

**把 `.txtpb` 注册成管线。**

[optimization_pass_registry.cc:121-144](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/optimization_pass_registry.cc#L121-L144) —— `RegisterPipelineProto`：遍历 proto 的 `compound_passes`，每个都注册成一个 `CompoundPassAdder` 生成器（键为它的 `short_name`，如 `simp`、`fixedpoint_simp`）；最后把 `default_pipeline` 数组也封装成一个名为 `default_pipeline` 的复合 Pass 并注册。注意它把文本里的 `cap_opt_level`/`min_opt_level`/`resource_sharing_required` 翻译成 `WrapPassWithOptions` 的包装（[optimization_pass_registry.cc:38-59](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/optimization_pass_registry.cc#L38-L59)）。

[optimization_pass_registry.cc:62-111](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/optimization_pass_registry.cc#L62-L111) —— `CompoundPassAdder::Generate`：根据 proto 的 `fixedpoint` 标志选择构造普通复合还是不动点复合，然后对其 `passes` 列表里的每个名字**递归**调 `registry().Generator(pass)->Generate()`——这正是「复合 Pass 名也能解析」的原因。

**标准管线入口：向注册表要 `default_pipeline`。**

[optimization_pass_pipeline.cc:43-61](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/optimization_pass_pipeline.cc#L43-L61) —— `TryCreateOptimizationPassPipeline`：`registry.Generator(kDefaultPassPipelineName)->Generate()` 得到管线树，再包一层名为 `"ir"` 的顶层 `OptimizationCompoundPass`，并挂上不变式校验器（`debug_optimizations` 时用严格的 `VerifierChecker`+`QueryEngineChecker`，否则只挂 weak 的 `VerifierChecker`）。

**`--list_passes` 的实现：直接读注册表。**

[opt_main.cc:77-78](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/tools/opt_main.cc#L77-L78) 与 [opt_main.cc:269-282](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/tools/opt_main.cc#L269-L282) —— `--list_passes` 分支：构造一个 `OptimizationPassPipelineGenerator`，调 `GetAvailablePassesStr()` 打印所有已注册 Pass 名。

[optimization_pass_pipeline.cc:94-114](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/optimization_pass_pipeline.cc#L94-L114) —— `GetAvailablePassesStr` / `GetAvailablePasses`：直接 `registry_.GetRegisteredNames()`，排序后输出。所以「可用 Pass 列表」完全是注册表在运行时的快照。

**标准管线的真实顺序（数据，非代码）。**

[optimization_pass_pipeline.txtpb:442-446](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/optimization_pass_pipeline.txtpb#L442-L446) —— `default_pipeline` 只有三大段：

```text
default_pipeline: [
  "simplify-and-inline",   # 反复内联 + 简化（本身是 fixedpoint）
  "post-inlining",         # 内联后的重头优化：窄化、BDD 化简、proc state、select 处理等
  "prepare-for-scheduling" # 为调度做收尾清理
]
```

这三段都是上面 `compound_passes` 里定义的复合 Pass 名。其中 [txtpb:376-385](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/optimization_pass_pipeline.txtpb#L376-L385) 的 `simplify-and-inline` 是 `fixedpoint: true`，即整个「边内联边简化」会跑到收敛。

> 关键结论：标准管线的「顺序」并不在 C++ 里，而在 `.txtpb`。要理解 `opt_main` 实际跑了什么，应该读 `optimization_pass_pipeline.txtpb`，而不是 `optimization_pass_pipeline.cc`（后者只是「把 txtpb 装配成内存树」的机械代码）。

#### 4.3.4 代码实践（命令行 + 阅读型）⭐ 本讲主实践

本实践对应任务：用 `opt_main --list_passes` 列出全部 Pass，挑三个查它们的 short/long name，并在标准管线中定位其相对顺序。

1. **实践目标**：把「注册表 → 管线描述」这条数据链路亲手验证一遍。
2. **操作步骤**：
   - 在已构建好的仓库里执行（参见 u1-l2 的构建方式）：
     ```bash
     bazel-bin/xls/tools/opt_main --list_passes
     ```
   - 从输出的列表里挑三个名字，建议选：`dce`、`arith_simp`、`cse`（公共子表达式消除）。
   - 验证 short/long name：每个 Pass 类的 `kName` 是 short name；long name 是构造时传给基类的第二个参数。例如：
     - `dce` → `DeadCodeEliminationPass`，long name `"Dead Code Elimination"`（[dce_pass.h:144-146](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/dce_pass.h#L144-L146)）。
     - `arith_simp` → `ArithSimplificationPass`，long name `"Arithmetic Simplifications"`（[arith_simplification_pass.h:167-169](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/arith_simplification_pass.h#L167-L169)）。
   - 定位相对顺序：在 `optimization_pass_pipeline.txtpb` 里搜索这三个名字。
     - `dce`、`arith_simp` 都出现在 `simp` 复合 Pass 内（[txtpb:26-85](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/optimization_pass_pipeline.txtpb#L26-L85)），相对顺序大致是 `dce` 反复穿插、`arith_simp` 在某段 `dce` 之后。
     - `cse` 出现在 `post-inlining-opt` 这一大段里（[txtpb:225-357](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/optimization_pass_pipeline.txtpb#L225-L357)），属于内联之后的高层优化。
3. **需要观察的现象**：
   - `--list_passes` 输出的是**所有叶子 Pass 名 + 所有复合 Pass 名**的并集（如 `simp`、`fixedpoint_simp(3)`、`default_pipeline` 这类「复合名」也在列表里）。
   - 同一个 `dce` 在 `.txtpb` 里出现几十次——它们都指向同一个生成器，每次 `Generate()` 都造一个**新的** `DeadCodeEliminationPass` 实例挂进树里。
4. **预期结果**：你能口头复述「`dce`/`arith_simp` 属于简化段、`cse` 属于后内联段，三大段顺序是 simplify-and-inline → post-inlining → prepare-for-scheduling」。
5. **若无法构建/运行**：标注「待本地验证」。退而求其次，直接阅读 `optimization_pass_pipeline.txtpb` 与各 `*_pass.h` 的 `kName` 即可完成顺序定位（纯阅读也能达成目标 3）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `--list_passes` 里既有 `dce`（叶子 Pass）又有 `simp`（复合 Pass）？
**参考答案**：因为叶子 Pass 和复合 Pass 都被注册进**同一个**注册表——叶子用 `REGISTER_OPT_PASS` 注册，复合 Pass 用 `RegisterPipelineProto` 注册成 `CompoundPassAdder`。`GetRegisteredNames()` 不区分二者，所以都列出来了。区分它们要靠「名字能否被解析为复合」（即 `.txtpb` 里 `compound_passes` 出现过的）。

**练习 2**：假如你想把一个新写的 `MyPass` 接入标准管线，需要改哪几处？
**参考答案**：① 写 `MyPass` 类，给它 `static constexpr std::string_view kName = "my_pass";` 并继承某个作用域基类；② 在其 `.cc` 里写 `REGISTER_OPT_PASS(MyPass);`；③ 在 `xls/passes/BUILD` 的 `oss_optimization_passes`（[BUILD:66 起](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/BUILD#L66-L99)）里加 `":my_pass"`；④ 在 `optimization_pass_pipeline.txtpb` 的合适复合段里加入 `"my_pass"`。完全不用动 `optimization_pass_pipeline.cc`。

**练习 3**：`default_pipeline` 顶层是 `OptimizationCompoundPass`（普通复合）而不是不动点复合，但为什么标准优化仍能收敛？
**参考答案**：因为顶层虽只跑一遍三大段，但其中 `simplify-and-inline` 本身是 `fixedpoint: true`（[txtpb:380](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/optimization_pass_pipeline.txtpb#L380)），`post-inlining` 内部又大量使用 `fixedpoint_simp` 等不动点段。不动点的「收敛」被下放到了各个子复合 Pass，顶层只需保证它们按合理顺序各司其职。

---

## 5. 综合实践

把本讲三块知识串起来，完成一次「从命令到 Pass 树」的端到端追踪：

1. 准备一个最小 IR（可复用 u1-l5 的 `simple_add.x` 经 `ir_converter_main` 产出的 `.ir`）。
2. 用自定义管线只跑两个 Pass，验证「数据驱动」：
   ```bash
   bazel-bin/xls/tools/opt_main input.ir --passes "dce [ arith_simp dce ]" --output_path out.ir
   ```
   - 这里 `--passes` 接受一段迷你语法（[opt_flags.cc:89-100](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/tools/opt_flags.cc#L89-L100)）：空格分词，`[ ... ]` 表示「括号内跑到不动点」（[pipeline_generator.h:79-125](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/pipeline_generator.h#L79-L125)）。所以该命令 = 先 `dce`，再把 `arith_simp`+`dce` 反复跑到不动点。
   - 指定 `--passes` 后，标准 `default_pipeline` **被完全忽略**（[opt_flags.cc:96-97](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/tools/opt_flags.cc#L96-L97)）。
3. 对照阅读 [tools/opt.cc:152-171](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/tools/opt.cc#L152-L171)：当 `options.pass_pipeline` 存在时走 `GetOptimizationPipelineGenerator(...).GeneratePipeline(*options.pass_pipeline)`，否则走 `TryCreateOptimizationPassPipeline(...)`——正是上面两种分支的源头。
4. 用 `--passes_bisect_limit 1` 再跑一次（[opt_flags.cc:117-119](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/tools/opt_flags.cc#L117-L119)），观察输出与无限制时的差异，体会「编译燃料」如何在前述 `Run()` 第 1 步截断 Pass 执行。
5. **预期产出**：一段你自己的话，说清「我输入的 `--passes` 字符串 → `GeneratePipeline` 拼成的复合 Pass 树 → `Run()` 如何驱动它（含不动点与 bisect）」。无法构建时标注「待本地验证」。

## 6. 本讲小结

- **统一接口**：所有 Pass 遵守 `Run() → bool(是否改动)` 契约；子类只实现 `RunInternal`（或作用域版 `RunOnFunctionBaseInternal`），度量、bisect、跳过、校验全由 `PassBase::Run` 包办。
- **复合与不动点**：`CompoundPassBase` 顺序串接，`FixedPointCompoundPassBase` 反复跑到收敛；二者构成可组合的编排原语，标准管线大量复用。
- **opt_level 开关**：通过 `CapOptLevel`/`IfOptLevelAtLeast`/`WithOptLevel` 等包装器把「优化等级」变成可随处摆放的开关，`NarrowingEnabled(>=2)`、`SplitsEnabled(>=3)` 是常见门槛。
- **数据驱动注册**：每个 Pass 用 `REGISTER_OPT_PASS` 注册进单例 `GetOptimizationRegistry()`；标准管线顺序写在 `optimization_pass_pipeline.txtpb`，启动时注册成 `default_pipeline` 复合 Pass。
- **运行时拼装**：`CreateOptimizationPassPipeline` 向注册表要 `default_pipeline` 生成器，`CompoundPassAdder` 递归地把名字解析成内存 Pass 树，最后交给 `Run()` 驱动。
- **观察手段**：`opt_main --list_passes` 直接读注册表列出全部可用 Pass；`--passes` 可用迷你语法临时指定自定义管线（`[ ]` 表不动点）。

## 7. 下一步学习建议

- **u4-l2 关键优化 Pass 详解**：本讲只讲了框架，下一步进入框架里跑的那些**具体变换**——算术化简（`arith_simp`）、CSE、DCE、常量折叠、内联，看它们各自如何实现 `RunOnFunctionBaseInternal`、如何返回 `changed`。
- **u4-l3 查询与分析引擎**：很多优化 Pass 要靠精确的位级信息才能做决策，下一讲讲 `bdd_query_engine`、区间分析等为 Pass 服务的分析层，以及 `OptimizationContext` 如何在管线内**共享**这些查询引擎（本讲已埋下伏笔）。
- **延伸阅读**：通读 `optimization_pass_pipeline.txtpb` 全文，并对照 `docs_src/passes_list.md`（由 `generate_documentation_md.py` 从注册表自动生成），你会得到一份带注释的完整标准管线地图。
