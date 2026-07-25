# JIT 自动聚类

> 本讲是「编译器与运行时」单元（u7）的第三讲，承接 u7-l2（XLA / StableHLO 与 tf2xla）。
> 上一篇我们讲了「**一个被点名的子图**如何被翻译成 XLA」；本讲回答另一个问题——
> **用户根本没点名时，TensorFlow 怎么自己决定把哪些 op 圈成一个 XLA 可编译簇？圈错了 / 圈不了的 op 又怎么办？**

## 1. 本讲目标

学完本讲，你应当能够：

1. 说出「auto-clustering（自动聚类）」要解决什么问题，以及它在整条优化流水线里的位置。
2. 对照 `xla_cluster_util` 与 `mark_for_compilation_pass`，讲清**哪些 op 会被聚成一个簇**（编译候选筛选 + 边收缩算法）。
3. 理解 `_XlaCluster` 属性如何作为聚类结果的「记账本」，以及 `GetXlaClusterForNode` / `GetXlaAutoClusteringSummary` 如何读取它。
4. 对照 `build_xla_ops_pass`，说明一个被标记的簇如何被物化成 `_XlaCompile` / `_XlaRun` 这对 op，以及**运行时回退**机制：编译失败时如何退回普通 TF 执行。

## 2. 前置知识

本讲默认你已经掌握以下内容（来自前置讲义）：

- **Op / Kernel / GraphDef**（u3-l1、u4-l1、u4-l2）：图由 `Node` 和 `Edge` 组成，每个 op 有名字、设备、属性（attr）。
- **优化流水线 OptimizationPassRegistry**（u6-l3 提到 Grappler）：TF 在「放置（placement）」前后会跑一系列图优化 pass。
- **XLA 是什么**（u7-l2）：一个线性代数编译器，把一段子图编译成高效设备代码；一段被 XLA 编译的子图必须**整体可编译**。

一个关键直觉（贯穿全讲）：

> XLA 的收益来自「**把多个 op 融合（fuse）成一个大 kernel**」，从而省掉中间张量的读写与内核启动开销。
> 但 XLA 不是无所不能——它只能编译「自己认得」的 op（有对应的 XLA kernel）。
> 因此自动聚类的本质是一道**图划分问题**：在一张大计算图里，尽可能把相邻的、XLA 认得的 op 划进同一个簇（cluster），同时保证：
>
> 1. 不引入环（deadlock）；
> 2. 不破坏 TF 的资源变量并发语义；
> 3. 簇不能太大（否则单次编译时间爆炸），也不能太小（否则没有融合收益）。

我们用「簇（cluster）」指代一个**将被整体交给 XLA 编译的连通子图**。

## 3. 本讲源码地图

本讲聚焦 `compiler/jit/` 下的自动聚类子系统。关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [tensorflow/compiler/jit/xla_cluster_util.h](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/jit/xla_cluster_util.h) / [.cc](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/jit/xla_cluster_util.cc) | **聚类公共工具**：`_XlaCluster` 属性、环检测图、JIT 等级判定、聚类摘要。本讲锚点之一。 |
| [tensorflow/compiler/jit/mark_for_compilation_pass.cc](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/jit/mark_for_compilation_pass.cc) | **核心聚类 pass**：决定哪些 op 进簇、怎么合并簇，并打上 `_XlaCluster` 属性。 |
| [tensorflow/compiler/jit/compilability_check_util.h](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/jit/compilability_check_util.h) | **可编译性检查器** `RecursiveCompilabilityChecker`：判定单个 op（含函数调用）能否被 XLA 编译。 |
| [tensorflow/compiler/jit/build_xla_ops_pass.cc](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/jit/build_xla_ops_pass.cc) | **本讲另一锚点**：把已标记的簇物化成 `_XlaCompile` / `_XlaRun`，并接入运行时回退。 |
| [tensorflow/compiler/jit/encapsulate_subgraphs_pass.cc](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/jit/encapsulate_subgraphs_pass.cc) | 把每个簇封装成一个 TF 函数调用，打上 `_XlaCompiledKernel` 标记。 |
| [tensorflow/compiler/jit/kernels/xla_ops.cc](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/jit/kernels/xla_ops.cc) | `_XlaCompile` / `_XlaRun` 的 C++ kernel 实现，运行时回退发生在这里。 |
| [tensorflow/compiler/jit/jit_compilation_pass_registration.cc](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/jit/jit_compilation_pass_registration.cc) | 把上述 pass 按编号注册进优化流水线。 |
| [tensorflow/compiler/jit/flags.cc](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/jit/flags.cc) | `--tf_xla_auto_jit` 等命令行开关。 |

属性字符串的定义集中在两个小文件里，建议先记住它们的「真名」：

- [tensorflow/compiler/jit/defs.cc:22-33](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/jit/defs.cc#L22-L33)：定义 `_XlaCompile`、`_XlaMustCompile`、`_XlaScope`、`_XlaInternalScope` 等属性字符串。
- [tensorflow/compiler/jit/encapsulate_subgraphs_pass.cc:60-65](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/jit/encapsulate_subgraphs_pass.cc#L60-L65)：定义 `_XlaCompiledKernel`、`_XlaNumConstantArgs`、`_XlaNumResourceArgs`、`_XlaHasReferenceVars`。
- 而本讲主角 `_XlaCluster` 定义在 [xla_cluster_util.cc:68](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/jit/xla_cluster_util.cc#L68)。

---

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：

- **4.1 自动聚类的全局流水线与 JIT 开关**（以 `xla_cluster_util` 的开关判定为锚）
- **4.2 哪些 op 会被聚成一个簇**（候选筛选 + 边收缩算法，`mark_for_compilation_pass` + `compilability_check_util`）
- **4.3 `build_xla_ops_pass`：簇的物化与运行时回退**（本讲第二锚点）

### 4.1 自动聚类的全局流水线与 JIT 开关

#### 4.1.1 概念说明

「auto-clustering」是 TF 的一种**图优化模式**：在图被执行前，自动把「XLA 认得的相邻 op」圈成若干个簇，每个簇整体交给 XLA 编译。它和 u7-l2 讲过的「`xla.compile()` 手动圈定子图」是**同一个编译后端（XLA）的两条不同入口**：

- 手动入口：用户用 `tf.xla.compile()` 显式包一段，强制编译。
- 自动入口（本讲）：用户只开一个开关（`--tf_xla_auto_jit` 或 ConfigProto），TF 自己找能编译的子图，**尽力而为（best-effort）**——编不了的 op 照常用 TF kernel 跑。

「尽力而为」这四个字是本讲的灵魂。它意味着：自动聚类必须能优雅地**跳过**它处理不了的 op（在聚类阶段），并在**编译失败时**退回 TF 执行（在运行阶段）。这两套「退路」分别在 4.2 和 4.3 讲。

整条自动聚类流水线由若干个 pass 串成，全部注册在 `POST_REWRITE_FOR_EXEC`（重写为可执行形式之后、真正放置之前）阶段。注意它们和 Grappler（u6-l3，`POST_PARTITION` 之后）不是同一拨——自动聚类发生在**更早**的 common_runtime 层。

#### 4.1.2 核心流程

注册顺序见 [jit_compilation_pass_registration.cc:48-82](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/jit/jit_compilation_pass_registration.cc#L48-L82)，可概括为：

```text
POST_REWRITE_FOR_EXEC 阶段（按编号递增执行）：
 5  CloneConstantsForBetterClusteringPass  // 复制常量，给聚类更多合并机会
 9  ClusterScopingPass                      // 打 _XlaInternalScope，限制簇规模
10  MarkForCompilationPass   ★本讲核心★   // 决定聚类，打 _XlaCluster
12  ForceXlaConstantsOnHostPass
20  IncreaseDynamismForAutoJitPass          // 让形状更动态，减少重编译
30  PartiallyDeclusterPass                  // 把簇里某些 op 「拆」出去（部分反聚类）
40  ReportClusteringInfoPass                // 汇总聚类决策，广播给监听器
50  EncapsulateSubgraphsPass                // 每个簇 → 一个 TF 函数调用，打 _XlaCompiledKernel
60  BuildXlaOpsPass          ★本讲核心★   // 函数调用 → _XlaCompile/_XlaRun（含回退）
```

把这条链路记住，本讲的两个锚点 `MarkForCompilationPass`（步骤 10）与 `BuildXlaOpsPass`（步骤 60）正是**首尾两端**：前者决定「圈谁」，后者把「圈好的结果」变成可在运行时调度的一对 op。

「是否启用」由一个全局 JIT 等级决定。这个等级有三种来源，优先级从高到低：

1. 命令行 `--tf_xla_auto_jit`（[flags.cc:93-102](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/jit/flags.cc#L93-L102)）；
2. ConfigProto 里的 `optimizer_options.global_jit_level`；
3. 默认 `OFF`。

`tf_xla_auto_jit` 的取值见其帮助文本与 setter：

- `0` = 用 ConfigProto 的设置；
- `-1` = 强制关闭；
- `1` = 开启，但只编译「很可能变快」的 op；
- `2` = 开启，编译一切能编译的；
- `fusible` = 只编译 XLA 知道如何融合的 op；
- `single-gpu(N)` = 仅对「单 GPU 图」按等级 N 开启，否则按 0 处理。

#### 4.1.3 源码精读

先看注册顺序（节选）：

```cpp
// POST_REWRITE_FOR_EXEC passes that support auto-clustering to enable XLA:
REGISTER_OPTIMIZATION(OptimizationPassRegistry::POST_REWRITE_FOR_EXEC, 10,
                      MarkForCompilationPass);
...
REGISTER_OPTIMIZATION(OptimizationPassRegistry::POST_REWRITE_FOR_EXEC, 50,
                      EncapsulateSubgraphsPass);
// Must run after EncapsulateSubgraphsPass.
REGISTER_OPTIMIZATION(OptimizationPassRegistry::POST_REWRITE_FOR_EXEC, 60,
                      BuildXlaOpsPass);
```
——见 [jit_compilation_pass_registration.cc:54-82](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/jit/jit_compilation_pass_registration.cc#L54-L82)。注意第 80-82 行那句注释「**Must run after EncapsulateSubgraphsPass**」：`BuildXlaOpsPass` 处理的对象是「已被封装成函数调用的簇」，所以必须在 `EncapsulateSubgraphsPass` 之后。

接着看 JIT 等级如何被解析。`--tf_xla_auto_jit` 的 setter 把字符串翻译成两个等级——`optimization_level_single_gpu`（单 GPU 图用）与 `optimization_level_general`（一般图用）：

```cpp
bool SetterForXlaAutoJitFlag(const std::string& value) {
  int32_t opt_level;
  if (absl::SimpleAtoi(value, &opt_level)) {
    mark_for_compilation_flags->xla_auto_jit_flag
        .optimization_level_single_gpu = opt_level;
    mark_for_compilation_flags->xla_auto_jit_flag.optimization_level_general =
        opt_level;
    return true;
  }
  ...
}
```
——见 [flags.cc:49-82](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/jit/flags.cc#L49-L82)。

`GetGlobalJitLevelForGraph`（[xla_cluster_util.cc:314-332](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/jit/xla_cluster_util.cc#L314-L332)）再根据「这张图是不是单 GPU 图」二选一。它先调 `GetXlaGlobalJitLevel`（[xla_cluster_util.cc:254-280](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/jit/xla_cluster_util.cc#L254-L280)）把 ConfigProto 与 flag 合并；若两者相等就直接返回，否则用 `IsSingleGpuGraph` 判定（[xla_cluster_util.cc:292-312](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/jit/xla_cluster_util.cc#L292-L312)）——单 GPU 图用 `single_gpu` 等级，多设备图用 `general` 等级。

> 关键结论：**只有 `GetGlobalJitLevelForGraph` 返回非 `OFF`，`MarkForCompilationPass` 才会真正去聚类**（见 4.2.3 的 `ShouldCompile`）。这就是「开关」的本质——它只控制「要不要尝试」，不控制「具体圈哪些 op」。

`IsSingleGpuGraph` 的判定逻辑很直白：遍历所有节点的 `assigned_device_name`，统计出现了几个 GPU 设备号，恰好 1 个才算单 GPU 图（[xla_cluster_util.cc:296-311](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/jit/xla_cluster_util.cc#L296-L311)）。

#### 4.1.4 代码实践

**实践目标**：把「开关 → 等级 → 是否聚类」这条链路在脑子里跑一遍，并验证 `tf_xla_auto_jit` 的取值含义。

**操作步骤**（源码阅读型，无需编译 TF）：

1. 打开 [flags.cc:93-102](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/jit/flags.cc#L93-L102)，抄下 `tf_xla_auto_jit` 的帮助文本。
2. 打开 [xla_cluster_util.cc:254-280](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/jit/xla_cluster_util.cc#L254-L280)，回答：当 ConfigProto 设为 `DEFAULT`、且 flag 也为 `0` 时，`single_gpu` 与 `general` 各被设成什么？这与「默认关闭」如何对应？
3. 打开 [jit_compilation_pass_registration.cc:48-82](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/jit/jit_compilation_pass_registration.cc#L48-L82)，把 9 个 pass 按编号排好，标注哪两个是本讲锚点。

**需要观察的现象 / 预期结果**：

- 第 2 步：两者都被设成 `OptimizerOptions::OFF`（见 [xla_cluster_util.cc:259-260](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/jit/xla_cluster_util.cc#L259-L260) 那行注释「To set compilation to be on by default, change the following line」），这正是「默认不开启自动聚类」的来源。
- 第 3 步：应得到 `MarkForCompilationPass(10) → ... → EncapsulateSubgraphsPass(50) → BuildXlaOpsPass(60)` 的顺序。

**补充（可选运行，待本地验证）**：如果你装了 pip 版 TF，可在脚本开头设环境变量 `TF_XLA_FLAGS="--tf_xla_auto_jit=2"` 或在 ConfigProto 里开 `global_jit_level`，再配合 `--tf_xla_clustering_debug`（[flags.cc:134-136](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/jit/flags.cc#L134-L136)）观察聚类产物。具体能否打出图文件取决于运行环境，标注为「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `tf_xla_auto_jit` 要区分 `single-gpu` 与 `general` 两个等级？
**答案**：因为在多设备（多 GPU / 多机）图里，跨设备的同步与放置代价更高，自动聚类未必划算；而单 GPU 图结构简单、收益明确，所以允许用更激进的等级。`GetGlobalJitLevelForGraph` 用 `IsSingleGpuGraph` 在两者间选择（[xla_cluster_util.cc:327-329](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/jit/xla_cluster_util.cc#L327-L329)）。

**练习 2**：`BuildXlaOpsPass` 的注册编号是 60，而 `MarkForCompilationPass` 是 10。如果有人把它们对调，会发生什么？
**答案**：`BuildXlaOpsPass` 依赖「簇已被封装成函数调用」这一前提（`_XlaCompiledKernel` 属性由 `EncapsulateSubgraphsPass(50)` 打上）。若它跑到 10，此时图中还没有任何带 `_XlaCompiledKernel` 的节点，`IsXlaCompiledKernel` 对所有节点都返回 false，该 pass 将什么也不做（详见 4.3.3）。

---

### 4.2 哪些 op 会被聚成一个簇：候选筛选与边收缩算法

#### 4.2.1 概念说明

聚类要回答两个子问题：

1. **候选筛选**：图里哪些 op **有资格**被编译？（资格 = 「这个 op 在这个设备上有 XLA kernel」且「不违反一系列安全规则」）
2. **簇的形成**：把这些有资格的 op 中**相邻**的合并成尽量大的簇，同时不违反约束。

这两个子问题分别由 `RecursiveCompilabilityChecker`（可编译性）和 `MarkForCompilationPassImpl` 的边收缩循环（合并）解决。

聚类结果记录在每个节点的一个属性上：`_XlaCluster`，它的值是簇的名字（如 `"cluster_3"`）。同一个簇里的所有节点共享同一个 `_XlaCluster` 值。这个属性就是聚类的「记账本」——后续 pass 靠它知道「谁和谁是一伙的」。

> 辨析三个容易混淆的属性：
> - `_XlaCluster`（[xla_cluster_util.cc:68](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/jit/xla_cluster_util.cc#L68)）：**聚类结果**，由 `MarkForCompilationPass` 写入，值是簇名。
> - `_XlaCompile`（[defs.cc:24](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/jit/defs.cc#L24)）：**用户输入**，`_XlaCompile=true` 表示「请把这个 op 编译」（强约束）。
> - `_XlaCompiledKernel`（[encapsulate_subgraphs_pass.cc:60](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/jit/encapsulate_subgraphs_pass.cc#L60)）：**封装标记**，由 `EncapsulateSubgraphsPass` 写入，表示「这个函数调用节点来自一个 XLA 簇」，`BuildXlaOpsPass` 靠它识别要改写的节点。

#### 4.2.2 核心流程

`MarkForCompilationPassImpl::Run`（[mark_for_compilation_pass.cc:1631-1652](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/jit/mark_for_compilation_pass.cc#L1631-L1652)）是总入口，五步走：

```text
Run():
 1. Initialize()
     ├─ FindCompilationCandidates()   // 筛出有资格的 op → compilation_candidates_
     ├─ CreateCycleDetectionGraph()   // 建一张「可加边判环」的 DAG（处理控制流循环）
     ├─ DeadnessAnalysis::Run()       // 分析每个值的「死活」，防止把不同时活的 op 合并
     └─ BuildInitialClusterSet()      // 每个候选 op 各自成为一个单节点 Cluster
 2. RunEdgeContractionLoop()          // 反复尝试合并相邻簇（核心算法）
 3. DeclusterNodes()                  // 把「明显有害」的 op 从簇里剔除（大常量、孤立 Fill）
 4. CreateClusters()                  // 给达标簇的每个节点写 _XlaCluster 属性
 5. DumpDebugInfo()                   // 打印聚类摘要
```

**候选筛选**（`FindCompilationCandidates`，[mark_for_compilation_pass.cc:1311](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/jit/mark_for_compilation_pass.cc#L1311)）对每个节点依次检查：

1. 是否被 `_XlaCompile=false` 显式禁止；
2. 该节点的设备是否有「JIT 编译设备」（`XlaOpRegistry::GetCompilationDevice`）；
3. 当前 JIT 策略下是否值得编译（`ShouldCompile`）；
4. **能否被 XLA 编译**（`RecursiveCompilabilityChecker::IsCompilableNode`）——这是最关键的一关。

**可编译性检查**（[compilability_check_util.h:180-185](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/jit/compilability_check_util.h#L180-L185)）的核心是 `HasXLAKernel`（[compilability_check_util.h:264-265](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/jit/compilability_check_util.h#L264-L265)）：到 XLA 的 kernel 注册表里查「这个 op 名 + 这个 JIT 设备」有没有对应的 XLA 实现。**没有就直接出局，永远不会进簇。** 此外还有一组布尔开关（`OperationFilter`，[compilability_check_util.h:79-154](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/jit/compilability_check_util.h#L79-L154)）用来排除「虽能编译但不该聚类」的 op，例如：

- `allow_stateful_rng_ops = false`：有状态随机数 op（`RandomUniform` 等，[compilability_check_util.h:252-256](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/jit/compilability_check_util.h#L252-L256)）默认不聚类，因为 XLA 的 RNG 种子行为与 TF 不一致；
- `allow_stack_ops` / `allow_tensor_array_ops`：不支持快照，默认不聚类；
- `allow_ops_producing_or_consuming_variant`：`DT_VARIANT` 进出簇尚未支持。

**簇的形成**（`RunEdgeContractionLoop`，[mark_for_compilation_pass.cc:777](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/jit/mark_for_compilation_pass.cc#L777)）采用**边收缩（edge contraction）**：初始每个候选 op 自成一簇，然后按拓扑后序遍历图的每条边，尝试把「相邻的两个簇」合并成一个。合并的合法性由 `TryToContractEdge` 逐条把关（见 4.2.3）。为了让聚类质量更高，循环分了三个阶段（phase 0/1/2），各自优先收缩不同类型的边：

- Phase 0（[mark_for_compilation_pass.cc:801-822](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/jit/mark_for_compilation_pass.cc#L801-L822)）：优先把 `Shape`/`Rank`/`Size` 这类「只消费形状」的 op 与其生产者合并——因为它们输出小标量，与生产者同簇可避免搬运大张量。
- Phase 1（[mark_for_compilation_pass.cc:824-872](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/jit/mark_for_compilation_pass.cc#L824-L872)）：避开 `NoOp`/标量自增这类会「拉错对象」的边，防止把梯度更新与变量自增错误合并。
- Phase 2（[mark_for_compilation_pass.cc:874-887](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/jit/mark_for_compilation_pass.cc#L874-L887)）：收缩剩余所有可收缩的边，达到「极大聚类（maximal clustering）」。

> 这是一种贪心的、基于并查集思想（实际用 `GraphCycles` 维护簇间 DAG）的图划分。它不追求全局最优簇划分（那是 NP-hard），而是用「后序遍历 + 多阶段启发式」快速得到一个足够好的解。

#### 4.2.3 源码精读

**(1) 候选筛选的关键片段**——`FindCompilationCandidates` 的循环体（节选）：

```cpp
const XlaOpRegistry::DeviceRegistration* registration;
if (!XlaOpRegistry::GetCompilationDevice(device_type.type(), &registration)) {
  VLOG(2) << "Rejecting " << node->name()
          << ": could not find JIT device for " << device_type.type();
  continue;   // 该设备无 JIT 编译设备 → 出局
}
...
auto policy = registration->autoclustering_policy;
if (!ShouldCompile(is_xla_compile_attr_true, device_type, policy)) {
  continue;   // 当前策略不值得编译 → 出局
}
...
RecursiveCompilabilityChecker checker(filter, DeviceType{...compilation_device_name...});
if (!checker.IsCompilableNode(*node, lib_runtime)) {
  continue;   // ★没有 XLA kernel 或违反 OperationFilter → 出局★
}
```
——见 [mark_for_compilation_pass.cc:1387-1418](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/jit/mark_for_compilation_pass.cc#L1387-L1418)。

`ShouldCompile`（[mark_for_compilation_pass.cc:339-350](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/jit/mark_for_compilation_pass.cc#L339-L350)）汇集了「要不要编译」的四种触发条件：

```cpp
bool ShouldCompile(bool is_xla_compile_attr_true, const DeviceType& device_type,
                   XlaOpRegistry::AutoclusteringPolicy policy) {
  return is_xla_compile_attr_true ||
         policy == XlaOpRegistry::AutoclusteringPolicy::kAlways ||
         (policy == AutoclusteringPolicy::kIfEnabledGlobally &&
          global_jit_level_ != OptimizerOptions::OFF) ||
         (device_type.type_string() == DEVICE_CPU && ... && cpu_global_jit_);
}
```
也就是说：用户显式 `_XlaCompile=true`、设备策略是 `kAlways`（XLA 设备）、或全局 JIT 已开启（`kIfEnabledGlobally`）——满足任一即尝试编译。这里再次印证 4.1 的结论：`global_jit_level_` 来自 `GetGlobalJitLevelForGraph`，是全局开关。

**(2) 边收缩的合法性检查**——`TryToContractEdge`（[mark_for_compilation_pass.cc:1560-1629](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/jit/mark_for_compilation_pass.cc#L1560-L1629)）逐条把关五个条件：

```cpp
// (a) 两簇的「死活谓词」必须一致 —— 不能把不同条件下才执行的 op 合并
if (from->deadness_predicate() != to->deadness_predicate()) { return false; }
// (b) 设备兼容
TF_ASSIGN_OR_RETURN(bool devices_compatible, AreDevicesCompatible(*from, *to));
if (!devices_compatible) { return ...false; }
// (c) XLA 作用域一致
if (from->xla_scope().has_value() && to->xla_scope().has_value() &&
    *from->xla_scope() != *to->xla_scope()) { return ...false; }
// (d) 合并后不超过最大簇规模
if (from->cluster_size() + to->cluster_size() > debug_options_.max_cluster_size) { ... }
// (e) 不引入跨设备依赖、不破坏资源变量并发语义
...
return MergeClusters(from, to);   // 全部通过才真正合并
```
五个条件全过，才调用 `MergeClusters`（[mark_for_compilation_pass.cc:1628](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/jit/mark_for_compilation_pass.cc#L1628)）。其中条件 (a) 的「死活（deadness）」来自 `DeadnessAnalysis`：它分析哪些值在同一条件下才「活着」，避免把「`tf.cond` 的两个分支」错误地并进一个簇——那会改变语义。

**(3) 把结果写进 `_XlaCluster`**——`CreateClusters`（[mark_for_compilation_pass.cc:1002-1057](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/jit/mark_for_compilation_pass.cc#L1002-L1057)）。它遍历每个候选，只对「达标」的簇写属性：

```cpp
if (cluster->effective_cluster_size() >= debug_options_.min_cluster_size ||
    cluster->has_functional_control_flow() ||
    cluster->is_xla_compile_attr_true()) {
  ...  // 生成簇名，如 "cluster_3"
  n->AddAttr(kXlaClusterAttr, name);     // ★记账本在这里写入★
  n->AddAttr(kXlaAlreadyClustered, true);
}
```
——见 [mark_for_compilation_pass.cc:1032-1052](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/jit/mark_for_compilation_pass.cc#L1032-L1052)。这里有个重要门槛：簇的**有效大小**（`effective_cluster_size`，排除常量/Identity 节点）必须达到 `min_cluster_size`（对应 `--tf_xla_min_cluster_size`，[flags.cc:103-107](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/jit/flags.cc#L103-L107)），否则不写属性——太小的簇没有融合收益，不值得编译开销。例外是「含函数式控制流（If/While）」或「显式 `_XlaCompile=true`」的簇，无条件写。

**(4) 读回聚类结果**——`GetXlaClusterForNode`（[xla_cluster_util.cc:226-236](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/jit/xla_cluster_util.cc#L226-L236)）就是读 `_XlaCluster` 属性：

```cpp
std::optional<absl::string_view> GetXlaClusterForNode(const Node& node) {
  const AttrValue* attr_value = node.attrs().Find(kXlaClusterAttr);
  if (attr_value == nullptr) return std::nullopt;  // 不在任何簇里
  ...
  return attr_value->s();   // 返回簇名
}
```
后续 pass（`EncapsulateSubgraphsPass`、`ReportClusteringInfoPass`）都靠它知道簇的归属。整图的聚类摘要由 `GetXlaAutoClusteringSummary`（[xla_cluster_util.cc:384-418](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/jit/xla_cluster_util.cc#L384-L418)）统计：簇数、每个簇的 op 直方图、未聚类的 op 直方图——这正是 `ReportClusteringInfoPass` 广播给监听器的内容。

#### 4.2.4 代码实践

**实践目标**：给定一张小图，手工判定哪些 op 会被聚成一个簇，哪些会被排除，从而把「`HasXLAKernel` + `OperationFilter` + 边收缩」串起来。

**操作步骤**（源码阅读 + 推理型）：

考虑下面这张逻辑小图（示例代码，非项目源码）：

```text
Const(a) ──┐
           ├──> MatMul ──> Relu ──> RandomUniform ──> Add ──> Shape
Const(b) ──┘
```

1. 打开 [compilability_check_util.h:252-256](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/jit/compilability_check_util.h#L252-L256)，确认 `RandomUniform` 属于 `IsStatefulRandomOp`。
2. 假设全局 JIT 已开启。逐个 op 判定「是否候选」：`Const`、`MatMul`、`Relu`、`Add`、`Shape` 通常有 XLA kernel 且不在排除名单 → **候选**；`RandomUniform` 因 `allow_stateful_rng_ops=false` → **非候选**。
3. 因为 `RandomUniform` 不是候选，它会把链条**切断**：`... → Relu` 和 `Add → Shape` 分属两个互不相邻的簇（`RandomUniform` 像一堵墙挡在中间，且 `Add` 还要等 `RandomUniform` 的输出）。
4. 对照 [mark_for_compilation_pass.cc:801-822](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/jit/mark_for_compilation_pass.cc#L801-L822) 的 Phase 0：`Shape` 是「只消费形状」的 op，会被优先并入 `Add` 所在簇。

**需要观察的现象 / 预期结果**：

- 最终应得到**两个簇**：`{Const×2, MatMul, Relu}` 与 `{Add, Shape}`，中间隔着一个**未聚类的** `RandomUniform`。
- 这正回答了「遇到不支持的 op 怎么办」的**聚类阶段**答案：**不支持的 op 天然不进簇，并把它两侧的候选切成不同的簇。** 簇之间仍按普通 TF 图执行（带张量传递），互不影响。

**预期结果（运行层面，待本地验证）**：若用 `--tf_xla_clustering_debug` 打印 `GetXlaAutoClusteringSummary`，应在 `unclustered_op_histogram` 里看到 `RandomUniform`，在 `clusters` 里看到上述两个簇。

#### 4.2.5 小练习与答案

**练习 1**：`effective_cluster_size` 与 `cluster_size` 有何区别？为什么聚类门槛用前者？
**答案**：`cluster_size` 是簇内所有 TF 节点数；`effective_cluster_size` 排除了常量和 Identity 这类「几乎不花算力」的节点（见 `Cluster` 类注释 [mark_for_compilation_pass.cc:191-192](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/jit/mark_for_compilation_pass.cc#L191-L192)）。用前者做门槛，可以防止「靠堆常量凑够规模」的虚假大簇被编译。

**练习 2**：`TryToContractEdge` 的条件 (a)「死活谓词一致」保护了什么语义？
**答案**：保护 `tf.cond` / `tf.while_loop` 的条件执行语义。如果两个 op 只在不同分支条件下才执行，把它们并进一个簇会让 XLA 把它们都算一遍，改变结果。`DeadnessAnalysis` 先算出每个值的「死活谓词」，`TryToContractEdge` 据此拒绝合并（[mark_for_compilation_pass.cc:1562-1573](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/jit/mark_for_compilation_pass.cc#L1562-L1573)）。

**练习 3**：`CreateCycleDetectionGraph` 为什么要「把控制流循环从外层图断开」？
**答案**：聚类绝不能给图引入环（否则执行时 deadlock）。但 TF 图本身允许有环——它们都经过控制流算子（`Enter`/`Exit`/`NextIteration`）。环检测图在构造时把这些循环「折叠」成单个 frame 节点、并打断 `NextIteration` 回边（[xla_cluster_util.cc:135-224](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/jit/xla_cluster_util.cc#L135-L224)），使剩余结构是无环 DAG，从而 `InsertEdge` 能在线性时间判环。

---

### 4.3 build_xla_ops_pass：簇的物化与运行时回退

#### 4.3.1 概念说明

`MarkForCompilationPass` 只是在节点上写了 `_XlaCluster` 属性——**图的结构没变**。要真正在运行时调用 XLA，还需要 `EncapsulateSubgraphsPass`（把每个簇封进一个 TF 函数调用，打 `_XlaCompiledKernel`）和本讲第二锚点 `BuildXlaOpsPass`（把函数调用改成 `_XlaCompile` / `_XlaRun` 这对 op）。

这对 op 分工明确：

- `_XlaCompile`：吃进簇的输入，**尝试编译**，输出两样东西——一个编译键（`compilation_key`，用来在闭包存储里取回可执行对象）和一个布尔值 `compilation_successful`（编译是否成功）。
- `_XlaRun`：吃进编译键与输入，**真正执行** XLA 编译产物，输出簇的计算结果。

关键设计：`_XlaCompile` 的「编译」可能**失败**（比如某个 op 在编译时才发现不支持，或在当前形状下无法编译）。为此 `BuildXlaOpsPass` 构造了两套图结构：

- **严格模式（strict / must-compile）**：簇所在设备要求必须编译（如 XLA 设备），或禁用了懒编译。此时 `_XlaCompile` 一旦失败就报错，`_XlaRun` 必然执行。
- **懒模式（lazy）**：用一张 `Switch`/`Merge` 图在运行时**二选一**——编译成功就走 `_XlaRun`（XLA 执行），编译失败就走原来的 `StatefulPartitionedCall`（退回普通 TF 执行）。**这就是运行时回退的核心机制。**

> 把 4.2 和 4.3 的两套「退路」对比记牢：
> - **聚类阶段的退路**：没有 XLA kernel 的 op 根本不进簇，两侧各自成簇照常执行。
> - **运行阶段的退路**：进了簇、但运行时编译失败的，靠懒模式的 `Switch`/`Merge` 退回 TF 函数调用。

#### 4.3.2 核心流程

`BuildXlaOpsPass::Run`（[build_xla_ops_pass.cc:576-623](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/jit/build_xla_ops_pass.cc#L576-L623)）：

```text
Run():
  收集所有 IsXlaCompiledKernel==true 的节点（即「来自某个簇的函数调用」）
  for 每个这样的节点 n:
      ReplaceNodeWithXlaCompileAndXlaRun(n):
        1. GetXlaClusterInfo(n)        // 分出常量/非常量/资源三类输入
        2. InferDeviceForCluster(n)    // 推断簇应跑在哪个设备
        3. DeviceRequiresCompilation() // 该设备是否「必须编译」
        4. 构造 _XlaCompile 节点
        5. if requires_compilation (严格):
               构造 _XlaRun，把 n 的出边改接到 _XlaRun，删掉 n
           else (懒):
               用 Switch(compilation_successful) 分流 →
                 真：_XlaRun    假：原 n（改成 StatefulPartitionedCall）
               用 _XlaMerge 把两路输出合并
```

「是否必须编译」由 `DeviceRequiresCompilation`（[build_xla_ops_pass.cc:286-294](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/jit/build_xla_ops_pass.cc#L286-L294)）判定：设备的 `autoclustering_policy == kAlways`（即 XLA 设备）即为必须。

运行时，`_XlaCompile` 的 kernel `XlaCompileOp::Compute`（[kernels/xla_ops.cc:761-878](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/jit/kernels/xla_ops.cc#L761-L878)）决定编译模式：

```cpp
DeviceCompileMode compile_mode = [&] {
  if (must_compile_) return DeviceCompileMode::kStrict;
  return GetXlaOpsCommonFlags()->tf_xla_async_compilation
             ? DeviceCompileMode::kAsync
             : DeviceCompileMode::kLazy;
}();
```
——见 [kernels/xla_ops.cc:777-784](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/jit/kernels/xla_ops.cc#L777-L784)。`kLazy` 即「先试编译，不行就回退」。当编译返回 `UNIMPLEMENTED` 时：

```cpp
if (status.code() == error::UNIMPLEMENTED) {
  LOG(WARNING) << "Compilation failed:" << status
               << ".  Falling back to TF function call.";
  ...
  executable = nullptr;   // 关键：置空，让下方把 compilation_successful 设为 false
  cannot_compile_cluster_ = true;   // 记住「这个簇编不了」，以后不再重试
}
...
if (!executable && !pjrt_executable) {   // 没编译出可执行对象
  compilation_successful.scalar<bool>()() = false;   // ★通知懒模式走 TF 回退★
  return;
}
```
——见 [kernels/xla_ops.cc:822-850](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/jit/kernels/xla_ops.cc#L822-L850)。`compilation_successful=false` 正是触发那张 `Switch` 图走「假分支」（原 TF 函数调用）的信号。

#### 4.3.3 源码精读

**(1) 找出要改写的节点**——`BuildXlaOpsPass::Run` 的收集逻辑：

```cpp
std::vector<Node*> xla_compiled_kernels;
absl::c_copy_if(graph->op_nodes(), std::back_inserter(xla_compiled_kernels),
                [](const Node* n) {
                  if (n->IsSend() || n->IsRecv() || n->IsControlFlow()) return false;
                  // 只改写被 MarkForCompilation + Encapsulate 标记过的节点
                  return IsXlaCompiledKernel(*n);
                });
```
——见 [build_xla_ops_pass.cc:581-591](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/jit/build_xla_ops_pass.cc#L581-L591)。`IsXlaCompiledKernel` 的实现就是查 `_XlaCompiledKernel` 属性（[encapsulate_subgraphs_pass.cc:1326-1332](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/jit/encapsulate_subgraphs_pass.cc#L1326-L1332)），而该属性是 `EncapsulateSubgraphsPass` 在封装簇时打上的（[encapsulate_subgraphs_pass.cc:1297](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/jit/encapsulate_subgraphs_pass.cc#L1297)）。这就把 4.1 流水线里的「步骤 50 → 步骤 60」接上了。

**(2) 严格模式的图改写**——构造 `_XlaCompile` 与 `_XlaRun`：

```cpp
ops::_XlaCompile xla_compile(root.WithOpName("xla_compile"),
                             cluster_info.constant_inputs,
                             cluster_info.non_constant_inputs,
                             cluster_info.resource_inputs,
                             /*must_compile=*/requires_compilation,
                             cluster_info.function);
...
if (requires_compilation) {
  ops::_XlaRun xla_run(root.WithOpName("xla_run"), xla_run_args,
                       xla_compile.key, n->output_types());
  MoveOutgoingEdges(g, /*old_node=*/n, /*new_node=*/xla_run.operation.node());
  g->RemoveNode(n);
}
```
——见 [build_xla_ops_pass.cc:499-524](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/jit/build_xla_ops_pass.cc#L499-L524)。注意 `_XlaCompile` 的 `must_compile` 入参直接来自 `requires_compilation`，它最终变成节点的 `must_compile` 属性，被 `XlaCompileOp` 构造函数读取（[kernels/xla_ops.cc:758](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/jit/kernels/xla_ops.cc#L758)、`MustCompileAttr` [kernels/xla_ops.cc:720-725](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/jit/kernels/xla_ops.cc#L720-L725)）。

**(3) 懒模式的回退图**——这是全讲最精妙的部分，注释里直接画出了目标图：

```cpp
// "Lazy" compilation: an _XlaCompile invocation may decide not to compile...
// We generate the following graph:
//
//   (use_tf_call, use_xla_run) =
//       Switch(pred=xla_compile.compilation_successful, value=xla_compile.key)
//
//   tf_call_outputs = cluster_N(..., ^use_tf_call)        // 假分支：退回 TF 执行
//   xla_run_outputs = _XlaRun(..., key=use_xla_run)        // 真分支：XLA 执行
//   outputs = Merge(tf_call_outputs, xla_run_outputs).     // 合并两路输出
ops::Switch s(root.WithOpName("predicated_compilation_key"),
              xla_compile.key, xla_compile.compilation_successful);
Output predicated_compilation_key = s.output_true;        // 编译成功时 key 有效
Output inverse_predicated_compilation_key = s.output_false; // 编译失败时走回退
ops::_XlaRun xla_run(root.WithOpName("xla_run"), xla_run_args,
                     predicated_compilation_key, n->output_types());
...
// 原节点 n 被改成 StatefulPartitionedCall，由假分支控制
TF_ASSIGN_OR_RETURN(Node* const pco, ReplaceFunctionCallWithPartitionedCall(
    options, flib_def, n, g, cluster_info.function, root));
```
——见 [build_xla_ops_pass.cc:525-569](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/jit/build_xla_ops_pass.cc#L525-L569)。`Switch` 以 `compilation_successful` 为谓词把编译键分流：成功时 `_XlaRun` 拿到有效 key 去执行 XLA；失败时 key 无效，控制流转向被改写成 `StatefulPartitionedCall` 的原节点 `n`，用普通 TF kernel 跑同一份计算。两路输出再用 `_XlaMerge`/`Merge` 合并（[build_xla_ops_pass.cc:116-172](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/jit/build_xla_ops_pass.cc#L116-L172) 的 `MergeOutgoingDataEdges`）。

**(4) 运行时编译成功的信号**——`XlaCompileOp::Compute` 的收尾：

```cpp
Tensor compilation_successful(cpu_allocator, DT_BOOL, TensorShape({}));
compilation_successful.flat<bool>()(0) = true;   // ★编译成功★
ctx->set_output(0, compilation_key);
ctx->set_output(1, compilation_successful);
```
——见 [kernels/xla_ops.cc:873-877](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/jit/kernels/xla_ops.cc#L873-L877)。这个 `true` 让懒模式的 `Switch` 走 `_XlaRun`；而 4.3.2 里编译失败时设的 `false` 让它走 `StatefulPartitionedCall`。两个分支共用同一套输入、产出同一套输出，对图的其余部分完全透明。

> 还有一个细节值得留意：编译失败后 `cannot_compile_cluster_` 被置 true（[kernels/xla_ops.cc:831-832](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/jit/kernels/xla_ops.cc#L831-L832)），之后该簇的每次执行都直接跳过编译、走回退——避免重复尝试一个注定失败的编译。

#### 4.3.4 代码实践

**实践目标**：把懒模式的 `Switch`/`_XlaMerge` 回退图在纸上画出来，并解释「同一个簇为什么能同时有 XLA 和 TF 两条执行路径」。

**操作步骤**（源码阅读型）：

1. 读 [build_xla_ops_pass.cc:525-569](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/jit/build_xla_ops_pass.cc#L525-L569)，把注释里那张 ASCII 图抄下来，标注：
   - `Switch` 的谓词来自哪个输出（`xla_compile.compilation_successful`）；
   - `output_true` 喂给谁（`_XlaRun`）；`output_false` 控制谁（`StatefulPartitionedCall`）。
2. 读 [kernels/xla_ops.cc:822-833](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/jit/kernels/xla_ops.cc#L822-L833)，回答：编译失败时 `_XlaCompile` 的两个输出分别是什么？为什么 `compilation_key` 此时是空的也无所谓？
3. 读 [build_xla_ops_pass.cc:160-162](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/jit/build_xla_ops_pass.cc#L160-L162) 的 `_XlaMerge`，对比普通 `Merge`：为什么数据边用 `_XlaMerge`、控制边用「ControlToData → Merge → DataToControl」三段式（[build_xla_ops_pass.cc:78-112](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/jit/build_xla_ops_pass.cc#L78-L112) 与 [176-215](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/jit/build_xla_ops_pass.cc#L176-L215)）？

**需要观察的现象 / 预期结果**：

- 第 2 步：失败时 `compilation_key` 是空字符串、`compilation_successful=false`。空 key 无所谓，因为 `Switch` 会让假分支（TF 回退）执行，`_XlaRun` 根本不会读到这个 key。
- 第 3 步：`_XlaMerge` 是专为「XLA 回退」定制的 Merge（可被 XLA 识别），数据边用它来合并两路输出；控制边不能直接合并，故先转成数据（`ControlToData`）、用普通 `Merge` 合并、再转回控制（`DataToControl`）。

**预期结果（运行层面，待本地验证）**：开启 `--tf_xla_async_compilation` 或默认懒编译时，若某簇首次编译失败，日志会出现 `Compilation failed: ... Falling back to TF function call.`（[kernels/xla_ops.cc:823-824](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/jit/kernels/xla_ops.cc#L823-L824) 字样），随后该步仍能正确出结果（走了 TF 回退）。

#### 4.3.5 小练习与答案

**练习 1**：严格模式下，如果 `_XlaCompile` 编译失败会发生什么？和懒模式有何不同？
**答案**：严格模式（`must_compile=true`）下，`compile_mode=kStrict`，编译失败时 `OP_REQUIRES_OK(ctx, status)` 会直接把失败状态上报为 op 错误（[kernels/xla_ops.cc:817-820](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/jit/kernels/xla_ops.cc#L817-L820)，注意条件 `compile_mode != kLazy`），**没有回退**——因为 XLA 设备上根本没有对应的 TF kernel 可退。懒模式则吞下 `UNIMPLEMENTED`、设 `compilation_successful=false`、走 TF 回退。

**练习 2**：为什么懒模式不直接「编译失败就报错」？保留 TF 回退有什么代价和好处？
**答案**：好处是**稳健性**——自动聚类是尽力而为，某些 op 在特定形状/配置下才不可编译，回退保证程序仍能跑通。代价是**性能**：回退路径用普通 TF kernel，没有 XLA 的融合与缓冲复用收益，且簇的输入输出要在 XLA 与 TF 之间来回搬运。因此 `cannot_compile_cluster_` 会缓存「编不了」的结论，避免每步都白试一次。

**练习 3**：`BuildXlaOpsPass` 改写后，原图里那个代表簇的函数调用节点 `n` 在两种模式下分别命运如何？
**答案**：严格模式下 `n` 被 `g->RemoveNode(n)` 删除，出边改接到 `_XlaRun`（[build_xla_ops_pass.cc:522-524](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/jit/build_xla_ops_pass.cc#L522-L524)）；懒模式下 `n` 不删，而是被改写成 `StatefulPartitionedCall` 作为回退路径保留（[build_xla_ops_pass.cc:562-566](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/jit/build_xla_ops_pass.cc#L562-L566)）。

---

## 5. 综合实践

**任务**：把本讲三个模块串起来，端到端解释「开启自动聚类后，一张含可编译与不可编译 op 的图会经历什么」。

**操作步骤**（源码阅读 + 推理，可选拥有 pip TF 做运行验证）：

1. **开关**：复习 [xla_cluster_util.cc:314-332](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/jit/xla_cluster_util.cc#L314-L332)，确认「开启」= `GetGlobalJitLevelForGraph` 返回非 `OFF`。写出两种开启方式（`--tf_xla_auto_jit=2` 或 ConfigProto 的 `global_jit_level`）。

2. **聚类**：对一个包含 `MatMul → Relu → RandomUniform → Add` 的小模型（`RandomUniform` 故意制造不可聚类点），按 4.2 的方法推出聚类结果应为两个簇、中间隔一个未聚类 op。用 [xla_cluster_util.cc:384-418](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/jit/xla_cluster_util.cc#L384-L418) 的 `GetXlaAutoClusteringSummary` 字段（`clustered_node_count`、`unclustered_node_count`、`clusters`、`unclustered_op_histogram`）描述你期望看到的摘要。

3. **物化**：追踪这两个簇经过 [jit_compilation_pass_registration.cc](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/jit/jit_compilation_pass_registration.cc) 的 pass 50（封装）→ 60（`BuildXlaOpsPass`），最终每个簇变成一组 `_XlaCompile`/`_XlaRun`（或懒模式下的 `Switch`/`_XlaMerge`/`StatefulPartitionedCall`）。

4. **回退**：假设其中一个簇在运行时首次编译失败（返回 `UNIMPLEMENTED`），按 4.3 描述它如何退回 TF 执行，并解释 `cannot_compile_cluster_` 如何避免后续重复尝试。

**预期结果（待本地验证）**：若环境允许，用 `--tf_xla_clustering_debug` 运行，应能观察到：

- 聚类摘要里 `RandomUniform` 出现在未聚类直方图；
- 两个簇各自有 op 直方图；
- 运行日志无 `Falling back` 警告（因为 `RandomUniform` 根本没进簇，簇内都是可编译 op）；
- 若人为往簇里塞一个运行时才暴露的不支持 op，则应看到一次 `Falling back to TF function call.` 警告，之后不再重复。

把这条「开关 → 聚类 → 物化 → 回退」的故事用自己的话写成一页纸的总结。

## 6. 本讲小结

- **自动聚类是一道图划分问题**：在全局 JIT 开启时，把相邻的、XLA 认得的 op 圈成簇，整体交给 XLA 编译，以获得融合收益。
- **流水线两端是两个锚点 pass**：`MarkForCompilationPass(10)` 决定「圈谁」并写 `_XlaCluster` 属性；`BuildXlaOpsPass(60)` 把封装好的簇物化成 `_XlaCompile`/`_XlaRun`。中间隔 `EncapsulateSubgraphsPass(50)` 等若干 pass（见 [jit_compilation_pass_registration.cc](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/jit/jit_compilation_pass_registration.cc)）。
- **聚类阶段的两道筛子**：`HasXLAKernel`（有没有 XLA kernel）+ `OperationFilter`（一系列安全/正确性开关）。没有 XLA kernel 的 op 天然不进簇，并切断两侧簇的连通——这是「不支持的 op」的第一道退路。
- **簇的形成靠边收缩**：`TryToContractEdge` 用死活谓词、设备兼容、作用域、规模、跨设备依赖、资源变量安全六重检查把关；多阶段（phase 0/1/2）后序遍历得到极大聚类。
- **运行时回退靠懒模式的 `Switch`/`_XlaMerge` 图**：`_XlaCompile` 编译成功走 `_XlaRun`，失败（`UNIMPLEMENTED`）走原 `StatefulPartitionedCall`，对图其余部分透明——这是「不支持的 op」的第二道退路。
- **`_XlaCluster` 是聚类的记账本**，`GetXlaClusterForNode` 读它，`GetXlaAutoClusteringSummary` 汇总它，是理解与调试聚类结果的核心入口。

## 7. 下一步学习建议

- **u7-l4（TFRT 新一代运行时）**：本讲的 `_XlaCompile`/`_XlaRun` 跑在传统 `DirectSession` 执行器上；TFRT 重新设计了执行模型，了解它有助于看清「XLA 编译产物如何被新一代运行时调度」。
- **继续精读聚类周边 pass**：`partially_decluster_pass.cc`（把簇里某些 op 拆出去，减少不必要的 XLA 化）、`increase_dynamism_for_auto_jit_pass.cc`（让形状更动态以减少重编译）、`clone_constants_for_better_clustering.cc`，它们都在 [jit_compilation_pass_registration.cc](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/jit/jit_compilation_pass_registration.cc) 的流水线里，是进阶理解自动聚化的好材料。
- **对照测试学行为**：[mark_for_compilation_pass_test.cc](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/jit/mark_for_compilation_pass_test.cc) 与 [build_xla_ops_pass_test.cc](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/jit/build_xla_ops_pass_test.cc) 里有大量「给定一张小图、断言聚类结果」的用例，是验证你理解的最佳习题集。
- **回到 u7-l2**：把本讲的「自动入口」与 u7-l2 的「手动 `xla.compile()` 入口」对照，体会它们共用 XLA 后端、却走不同的子图圈定策略。
