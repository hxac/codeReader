# JIT 自动聚类

## 1. 本讲目标

学完本讲，你应当能够：

- 说清「auto-clustering（自动聚类）」在 TensorFlow 执行链路里的**位置**：它发生在放置之后、真正执行之前，是一组在 `POST_REWRITE_FOR_EXEC` 阶段依次运行的图优化 pass。
- 复述一个 op 从「普通图节点」变成「XLA 编译簇成员」要经历的三道关：**能不能进簇（候选判定）→ 怎么和邻居合成一个簇（聚类算法）→ 簇被改写成什么 op（`_XlaCompile`/`_XlaRun`）**。
- 读懂 `xla_cluster_util.h` 这个工具箱里每个函数的职责，理解为什么聚类必须依赖**环检测图**、为什么要把和 ref variable 相关的节点单独标记。
- 读懂 `build_xla_ops_pass.cc`，理解「严格编译」与「懒惰编译」两条路径，以及后者如何用 `Switch`/`Merge` 实现「XLA 跑不动就回退到普通 TF」的安全网。
- 回答本讲的核心追问：**哪些 op 会被聚成一个 XLA 可编译簇？遇到不支持的 op 时运行时如何处理？**

## 2. 前置知识

在进入源码前，先用通俗语言建立几个直觉。

### 2.1 为什么要「聚类」

上一讲（u7-l2）我们讲过，XLA 的收益来自**算子融合**：把一连串小 op（比如 `MatMul → BiasAdd → ReLU`）编译成一段连续的设备代码，省掉中间结果在显存里的来回搬运。但 XLA 不会、也不能把整张 TF 计算图都吞下去——有些 op 它编译不了（比如某些 IO op、随机性 op），有些 op 跨设备了。

于是需要一个「打包」步骤：在图里找出**连续的、XLA 能编译的、落在同一设备上的**子图，把它们圈成一个簇（cluster），交给 XLA 整体编译。这个「找子图、打包成簇」的过程就叫 **auto-clustering（自动聚类）**。它是一种 best-effort（尽力而为）的优化——圈不进来的 op 照常用普通 TF 内核执行，不影响正确性。

### 2.2 聚类要解决的几个难题

把一堆节点「圈在一起」看似简单，实则要躲开几个陷阱：

- **不能引入环**。原本无环的计算图，如果乱合并可能产生依赖环导致死锁。所以聚类过程要一边合并、一边做环检测。
- **要保证控制流语义**。`Switch`/`Merge` 这类条件执行 op 有「死活（deadness）」概念——某条分支在某次运行里可能根本不该执行。把死活不同的节点并到一个簇里会改变语义。
- **要尊重设备边界**。簇里的所有节点最终会被编译成跑在同一台设备上的代码，跨设备的节点不能进同一簇。
- **要尊重 ref variable 的并发语义**。TF 的 ref variable 允许多处读写，XLA 对此建模有限，相关节点需要特殊处理。

本讲要讲的 `xla_cluster_util` 正是为解决这些难题而存在的工具集。

### 2.3 三个关键属性名（先记住）

聚类 pass 之间靠给节点打**属性（attribute）**来通信，理解本讲需要先认识几个属性名：

| 属性名 | 作用 | 定义处 |
|---|---|---|
| `_XlaCluster` | 标记节点属于哪个簇（值是簇名，如 `cluster_0`） | `xla_cluster_util.h` 的 `kXlaClusterAttr` |
| `_XlaCompile` / `_XlaMustCompile` | 用户/作用域显式要求编译某节点 | `defs.h` 的 `kXlaCompileAttr` / `kXlaMustCompileAttr` |
| `_XlaCompiledKernel` | 由 `EncapsulateSubgraphsPass` 打上，表示「这个函数调用节点来自一个 XLA 簇」 | `encapsulate_subgraphs_pass.cc` 的 `kXlaCompiledKernelAttr` |

这三个属性分别是聚类 pass 的**结果标记**（`_XlaCluster`）、**用户意图**（`_XlaCompile`）和**下游触发标记**（`_XlaCompiledKernel`），把整条流水线串了起来。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
|---|---|
| `tensorflow/compiler/jit/xla_cluster_util.h` | 聚类判定工具箱：环检测图、簇属性读写、ref variable 检测、全局 JIT 等级判定。本讲两个核心模块之一。 |
| `tensorflow/compiler/jit/xla_cluster_util.cc` | 上述工具的实现，尤其是 `CreateCycleDetectionGraph` 如何处理控制流环。 |
| `tensorflow/compiler/jit/build_xla_ops_pass.cc` | 把标记好的簇改写成 `_XlaCompile`/`_XlaRun` 节点。本讲两个核心模块之二。 |
| `tensorflow/compiler/jit/build_xla_ops_pass.h` | 该 pass 的类声明，说明它是 `GraphOptimizationPass`。 |
| `tensorflow/compiler/jit/mark_for_compilation_pass.cc` | **真正的聚类决策者**：决定哪些 op 能进簇、怎么合并成簇、给节点打 `_XlaCluster`。回答「哪些 op 会被聚类」必须读它。 |
| `tensorflow/compiler/jit/jit_compilation_pass_registration.cc` | 注册聚类相关 pass 的**运行顺序**，是理解整条流水线的索引。 |
| `tensorflow/compiler/jit/encapsulate_subgraphs_pass.cc` | 把同簇节点封装成一个 TF 函数，并打上 `_XlaCompiledKernel`。是聚类 pass 与 build_xla_ops pass 之间的桥梁。 |
| `tensorflow/compiler/jit/defs.h` | 定义 `_XlaCompile` 等属性常量。 |
| `tensorflow/compiler/jit/flags.h` | 定义 `--tf_xla_auto_jit`、簇大小上下限等可调旋钮。 |
| `tensorflow/compiler/tf2xla/xla_op_registry.h` | 定义 `AutoclusteringPolicy` 枚举（设备是否默认参与聚类）。 |

> 说明：规格点名的两个最小模块是 `build_xla_ops_pass` 与 `xla_cluster_util`。但要准确回答「哪些 op 会被聚成一个簇」「不支持的 op 怎么办」，必须把聚类的真正决策者 `mark_for_compilation_pass` 一并讲清，否则会失真。本讲会以这两个核心模块为主轴，辅以 `mark_for_compilation_pass` 的决策逻辑与整条流水线。

## 4. 核心概念与源码讲解

### 4.1 全景：auto-clustering 编译流水线与执行时机

#### 4.1.1 概念说明

回顾 u3-l2 讲过的执行链路：图在被 `DirectSession::Run` 执行前，会经历一串「图优化 pass」。这些 pass 按 `OptimizationPassRegistry` 里注册的**阶段（phase）+ 顺序号（order）**排队运行。auto-clustering 的一组 pass 全部注册在 `POST_REWRITE_FOR_EXEC` 阶段——也就是**放置（placement）之后、分区（partition）之前**。

为什么必须在放置之后？因为聚类要按节点**已分配的设备**来判断「能不能编译、能不能和邻居并簇」，设备信息只能等放置 pass 给每个节点定下 `assigned_device_name` 之后才确定。

为什么是一组 pass 而不是一个？因为聚类是个多步骤工程：先标范围、再圈簇、再拆簇、再封装成函数、最后改写成编译 op，每一步都是一个独立 pass，方便单独测试和插桩。

#### 4.1.2 核心流程

整条流水线按顺序号从小到大运行（见源码精读里的注册代码）：

```text
POST_REWRITE_FOR_EXEC 阶段：
  (5)  CloneConstantsForBetterClustering   # 复制常量，让它们能进更多簇
  (9)  ClusterScoping                      # 插入 _XlaInternalScope，引导分簇
  (10) MarkForCompilation  ★决策者★        # 决定哪些 op 进簇，打 _XlaCluster=<名字>
  (12) ForceXlaConstantsOnHost             # 把常量钉在 host 上
  (20) IncreaseDynamismForAutoJit          # 改写 shape 类 op，让簇更动态、少重编译
  (30) PartiallyDecluster                  # 把「只被簇外用到」的节点从簇里拆出来
  (40) ReportClusteringInfo                # 汇总聚类决策，广播给 listener
  (50) EncapsulateSubgraphs  ★桥梁★        # 每个簇 → 一个 TF 函数，call 节点打 _XlaCompiledKernel
  (60) BuildXlaOps          ★本讲主角★     # call 节点 → _XlaCompile + _XlaRun
```

一句话概括数据流：`MarkForCompilation` 给节点打 `_XlaCluster` 属性 → `EncapsulateSubgraphs` 把同簇节点装进函数、把调用节点打上 `_XlaCompiledKernel=true` → `BuildXlaOps` 发现这个属性，把调用节点替换成 `_XlaCompile`/`_XlaRun`。

#### 4.1.3 源码精读

pass 的注册顺序全部集中在一个文件里，注释也写得很清楚：

注册文件，第 10 号是决策者，第 60 号是本讲主角：[tensorflow/compiler/jit/jit_compilation_pass_registration.cc:46-83](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/compiler/jit/jit_compilation_pass_registration.cc#L46-L83)

其中两段关键注释值得留意：

- 第 73-78 行注释强调 `EncapsulateSubgraphsPass` 必须在 `MarkForCompilationPass` **之后**运行（因为要先有 `_XlaCluster` 标记才能封装）。
- 第 80-82 行注释强调 `BuildXlaOpsPass` 必须在 `EncapsulateSubgraphsPass` **之后**运行（因为它要消费 `_XlaCompiledKernel`）。

这三个 pass 的相对顺序构成了聚类流水线的「骨架」，理解了这张顺序表，就理解了本讲后续每个 pass 在做什么。

#### 4.1.4 代码实践

1. **实践目标**：在不运行的情况下，从源码确认 auto-clustering pass 都注册在哪个阶段、谁先谁后。
2. **操作步骤**：
   - 打开 [tensorflow/compiler/jit/jit_compilation_pass_registration.cc:46-83](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/compiler/jit/jit_compilation_pass_registration.cc#L46-L83)。
   - 数一数 `POST_REWRITE_FOR_EXEC` 阶段共有几个 `REGISTER_OPTIMIZATION`。
   - 找出 `MarkForCompilationPass`、`EncapsulateSubgraphsPass`、`BuildXlaOpsPass` 各自的顺序号。
3. **需要观察的现象**：三个 pass 的顺序号依次递增（10 < 50 < 60），且都排在同一个阶段常量 `POST_REWRITE_FOR_EXEC` 下。
4. **预期结果**：你会确认聚类发生在「放置之后、执行之前」，且三步严格有序。这是一个纯阅读型实践，无需运行。
5. 本步无需「待本地验证」，结论可直接从源码读出。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `BuildXlaOpsPass` 的顺序号改到 `EncapsulateSubgraphsPass` 之前（比如 45），会发生什么？

**参考答案**：`BuildXlaOps` 靠 `_XlaCompiledKernel` 属性发现要改写的节点，而这个属性是 `EncapsulateSubgraphs` 才打上的。顺序颠倒后，`BuildXlaOps` 运行时图里还没有任何带 `_XlaCompiledKernel` 的节点，于是它什么都不做，簇最终不会被编译成 `_XlaCompile`/`_XlaRun`，XLA 不会介入。

**练习 2**：聚类为什么放在 `POST_REWRITE_FOR_EXEC` 而不是更早的 `PRE_PLACEMENT`？

**参考答案**：聚类判定（能不能编译、设备是否兼容）依赖每个节点的 `assigned_device_name`，而设备是放置 pass（属于 `PRE_PLACEMENT` 之后）才分配的，所以聚类只能放在放置之后。

---

### 4.2 xla_cluster_util：聚类判定的工具箱

#### 4.2.1 概念说明

`xla_cluster_util.h`（及其 `.cc`）不直接做聚类决策，而是为聚类决策者（`mark_for_compilation_pass`）提供一整套**判定工具**。它的角色类似于一个「几何工具箱」：聚类 pass 需要频繁地问「这两个节点能不能并到一个簇里」「这个节点现在属于哪个簇」「合并会不会成环」，这些问题都交给这里的小函数回答。

把它单独拎出来讲，是因为它清晰地把聚类要面对的几个难点**显式地**变成了可调用的 C++ 函数，是理解聚类「为什么安全」的钥匙。

#### 4.2.2 核心流程

工具箱里的函数大致分四类：

1. **簇属性读写**：`GetXlaClusterForNode`（读 `_XlaCluster`）、`RemoveFromXlaCluster`（清掉，用于「拆簇」）、`kXlaClusterAttr`（属性名字符串）。
2. **环检测**：`CreateCycleDetectionGraph`，把整张图变成一个可用于并查集式合并的 `GraphCycles` 结构。
3. **安全性判定**：`HasForwardedRefInput`、`HasResourceInputOrOutput`、`IsShapeConsumerOp`、`GetNodesRelatedToRefVariables`。
4. **全局 JIT 等级判定**：`GetGlobalJitLevelForGraph`、`IsSingleGpuGraph`，决定 auto-clustering 整体开不开。

#### 4.2.3 源码精读

先看属性常量。聚类 pass 之间通信用的「簇名」就存在 `_XlaCluster` 属性里：[tensorflow/compiler/jit/xla_cluster_util.h:34-36](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/compiler/jit/xla_cluster_util.h#L34-L36)

读取与清除簇归属的两个便捷函数：[tensorflow/compiler/jit/xla_cluster_util.h:61-69](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/compiler/jit/xla_cluster_util.h#L61-L69)

`GetXlaClusterForNode` 在实现里就是查节点的 `_XlaCluster` 属性并校验类型为 string：[tensorflow/compiler/jit/xla_cluster_util.cc:226-236](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/compiler/jit/xla_cluster_util.cc#L226-L236)

环检测是这个工具箱里最精巧的部分。聚类要不断尝试「把两个簇合并」，每次合并都可能引入环，所以需要一张专门用来判环的图。难点是 TF 图本身可能含**控制流环**（while loop），如果直接判环会误报，于是这里做了特殊处理——把每个 loop 折叠成一个「frame」节点、并打断 `NextIteration` 的回边：[tensorflow/compiler/jit/xla_cluster_util.cc:135-224](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/compiler/jit/xla_cluster_util.cc#L135-L224)。函数返回 `StatusOr<bool>`：`false` 表示「图结构太复杂、聚类搞不定」（见第 199-205 行的 `b/127521408`），调用方会据此放弃聚类。

全局 JIT 等级判定决定 auto-clustering 整体是否启动。注意 TF 区分「单 GPU 图」与「一般图」两档策略：[tensorflow/compiler/jit/xla_cluster_util.h:74-81](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/compiler/jit/xla_cluster_util.h#L74-L81)

这两档对应 `--tf_xla_auto_jit` flag 的两种语法（见下方 flags）。`IsSingleGpuGraph` 的判定很直接——数图里出现的 GPU 设备数是否恰为 1：[tensorflow/compiler/jit/xla_cluster_util.cc:292-312](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/compiler/jit/xla_cluster_util.cc#L292-L312)

`GetGlobalJitLevelForGraph` 综合这三者（session 配置、flag、是否单 GPU）给出最终的全局 JIT 等级：[tensorflow/compiler/jit/xla_cluster_util.cc:314-332](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/compiler/jit/xla_cluster_util.cc#L314-L332)。等级为 `OFF` 时，auto-clustering 默认不启动（除非节点被显式标记）。

最后是 ref variable 安全性。`GetNodesRelatedToRefVariables` 用「向前 + 向后两个方向迭代到不动点」的方式，找出所有和 ref variable 有可达路径的节点：[tensorflow/compiler/jit/xla_cluster_util.cc:609-620](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/compiler/jit/xla_cluster_util.cc#L609-L620)。这个集合后来被 `EncapsulateSubgraphsPass` 用来给每个簇节点打上 `_XlaHasReferenceVars` 属性，供编译期做特殊处理。

#### 4.2.4 代码实践

1. **实践目标**：用 `xla_cluster_util` 里的函数名拼出「聚类为什么安全」的检查清单。
2. **操作步骤**：
   - 通读 [tensorflow/compiler/jit/xla_cluster_util.h:49-101](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/compiler/jit/xla_cluster_util.h#L49-L101)。
   - 把每个「判定类」函数对应到一个它防范的风险（环 / ref variable / 设备 / 控制流）。
3. **需要观察的现象**：你会发现工具箱里几乎没有「计算」类函数，全是「能不能、是不是」的布尔判定。
4. **预期结果**：你能填出一张「函数 → 防范风险」的对照表，例如 `CreateCycleDetectionGraph`→环、`GetNodesRelatedToRefVariables`→ref variable 并发语义、`HasResourceInputOrOutput`→设备/资源边界。纯阅读型实践，结论来自源码，无需运行。

#### 4.2.5 小练习与答案

**练习 1**：`CreateCycleDetectionGraph` 为什么要打断 `NextIteration` 的回边？

**参考答案**：TF 的 while loop 用 `NextIteration` 节点把循环体末尾连回首部，形成真实的图环。但聚类判环关心的是「合并两个簇会不会**新增**依赖环」，loop 自身的环是合法的、不该被当成错误。于是把每个 loop 折叠成一个 frame 节点、打断回边，让 loop 内部对环检测器「不可见」，从而只检测聚类引入的新环。

**练习 2**：`IsSingleGpuGraph` 返回 `true` 和 `false` 时，`--tf_xla_auto_jit` 的语义有什么不同？

**参考答案**：`--tf_xla_auto_jit` 支持两档：`optimization_level_single_gpu`（仅当图恰好用单 GPU 时生效）和 `optimization_level_general`（其他情况）。`IsSingleGpuGraph` 决定取哪一档，使得用户可以「只对单 GPU 模型激进开启 XLA，对多 GPU 谨慎」。

---

### 4.3 候选判定：哪些 op 能进 XLA 簇

#### 4.3.1 概念说明

这一节直接回答实践任务的前半句：**哪些 op 会被聚成一个 XLA 可编译簇？**

答案分两层。**第一层：单个 op 要先成为「编译候选（compilation candidate）」**，这要连过几道关。**第二层：候选之间还要能合并**——只有彼此相邻、设备兼容、死活一致、合并不成环的候选，才会被圈进同一个簇。单个 op 是候选不代表它最终一定进簇（比如它太小、孤立，达不到最小簇规模）。

候选判定由 `MarkForCompilationPassImpl::FindCompilationCandidates()` 完成，它定义在 `tensorflow/compiler/jit/mark_for_compilation_pass.cc`。

#### 4.3.2 核心流程

一个节点 `n` 要成为候选，要依次通过下面这些检查（任一失败就 `continue` 跳过）：

```text
对图中每个节点 n：
  ① 跳过 Send/Recv/控制流节点（在外层循环过滤）
  ② 若 _XlaCompile 属性为 false            → 跳过（用户明确不让编译）
  ③ 若 n 的设备没有注册「编译设备」          → 跳过（XLA 不认识这台设备）
  ④ 若 ShouldCompile(...) 为 false          → 跳过（设备策略 + 全局开关没开）
  ⑤ 若 RecursiveCompilabilityChecker
        .IsCompilableNode(n) 为 false       → 跳过（这个 op 没有对应的 XLA lowering）
  ⑥ 若 n 是 DT_STRING 的 Const             → 跳过（XLA 不支持字符串常量）
  ⑦ 若配置了白名单且 n 不在白名单            → 跳过（--tf_xla_ops_to_cluster）
  ⑧ 编译期常量约束检查（stateful op 喂 shape → 不进非平凡簇）
通过全部检查 → 加入 compilation_candidates_
```

其中第 ④ 步的 `ShouldCompile` 综合三个因素：节点是否被显式标记（`_XlaCompile`/`_XlaMustCompile`）、设备的 `AutoclusteringPolicy`、全局 JIT 等级。第 ⑤ 步是真正回答「这个 op XLA 编不编得动」的核心——它递归地（含被调函数）检查 op 是否有 XLA lowering。

候选之间合并成簇的算法是「贪心边收缩（greedy edge contraction）」：在 4.2 讲的环检测图上，反复尝试把相邻的两个候选簇合并，合并前检查设备兼容、死活一致、不引入跨设备依赖、不成环、不超最大簇规模。合并到不能合并为止，每个最终簇得到一个名字 `cluster_<序号>`。

#### 4.3.3 源码精读

候选判定的主循环，连续的 `continue` 对应上面 ②~⑦ 各道关：[tensorflow/compiler/jit/mark_for_compilation_pass.cc:1381-1433](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/compiler/jit/mark_for_compilation_pass.cc#L1381-L1433)

第 1387-1393 行：取设备对应的「编译设备注册信息」，取不到说明 XLA 不支持这台设备，跳过。这里用到的 `XlaOpRegistry::GetCompilationDevice` 是连接「TF 设备」和「XLA 编译设备」的桥梁（呼应 u7-l2 讲过的 tf2xla）。

第 1396-1403 行的 `ShouldCompile` 是开关总闸，它的逻辑很短但很关键：[tensorflow/compiler/jit/mark_for_compilation_pass.cc:339-350](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/compiler/jit/mark_for_compilation_pass.cc#L339-L350)

这里的 `AutoclusteringPolicy` 是设备在注册时声明的「聚类意愿」，枚举有三档：[tensorflow/compiler/tf2xla/xla_op_registry.h:127-138](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/compiler/tf2xla/xla_op_registry.h#L127-L138)

三档语义：

- `kIfExplicitlyRequested`：只有用户显式要求（`_XlaCompile`、jit_scope）才聚类。GPU/CPU 默认常是这档。
- `kIfEnabledGlobally`：显式要求 **或** 全局开了 JIT（`--tf_xla_auto_jit` 或 session 配置）就聚类。
- `kAlways`：只要 op 可编译就总是尝试聚类（典型是 XLA 专用设备，如 TPU）。

第 1413-1418 行：构造 `RecursiveCompilabilityChecker` 并调用 `IsCompilableNode`。它内部用一个 `OperationFilter` 控制哪些 op 允许编译（例如默认不允许在被调函数里出现 resource op、不允许字符串常量等），filter 的字段见：[tensorflow/compiler/jit/compilability_check_util.h:79-140](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/compiler/jit/compilability_check_util.h#L79-L140)

候选合并完成后，`CreateClusters()` 给达标的簇里每个节点打上 `_XlaCluster=<名字>`：[tensorflow/compiler/jit/mark_for_compilation_pass.cc:1032-1053](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/compiler/jit/mark_for_compilation_pass.cc#L1032-L1053)。注意第 1032-1034 行的门槛：簇要么「有效规模 ≥ 最小簇规模」、要么含函数式控制流、要么被显式标记，才会真正被打属性；太小或孤立的候选会被默默放弃（即不进簇、照常走 TF 内核）。最小/最大簇规模由 flag 控制：[tensorflow/compiler/jit/flags.h:60-63](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/compiler/jit/flags.h#L60-L63)

簇名的生成用到一个按图指纹（fingerprint）单调递增的序号，保证同一张图多次聚类名字稳定：[tensorflow/compiler/jit/mark_for_compilation_pass.cc:998-1000](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/compiler/jit/mark_for_compilation_pass.cc#L998-L1000)

#### 4.3.4 代码实践

1. **实践目标**：把 4.3.2 的「八道关」逐一对应到源码行，确认没有遗漏。
2. **操作步骤**：
   - 打开 [tensorflow/compiler/jit/mark_for_compilation_pass.cc:1381-1433](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/compiler/jit/mark_for_compilation_pass.cc#L1381-L1433)。
   - 给每个 `continue` 标注它对应 4.3.2 里哪一道关（② ~ ⑦）。
   - 特别留意第 1390-1392 行的日志 `"could not find JIT device"`，它正是「不支持的设备」被剔除的位置。
3. **需要观察的现象**：每个 `continue` 都配有一条 `VLOG` 说明拒绝原因，构成一份完整的「拒绝清单」。
4. **预期结果**：你能复述出「op 要进簇，必须设备被 XLA 认识 + 策略允许 + 有 XLA lowering + 不在黑名单 + 通过常量约束」这条链条。纯阅读型实践，无需运行。
5. 本步结论可直接从源码读出，无需「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：一个 op 有 XLA lowering，但它的设备 `AutoclusteringPolicy` 是 `kIfExplicitlyRequested`，且用户既没开全局 JIT 也没打 `_XlaCompile`。它会被聚类吗？

**参考答案**：不会。看 `ShouldCompile`（第 339-350 行）：`kIfExplicitlyRequested` 这档只在 `is_xla_compile_attr_true` 为真时才返回 true，而题目条件下它是 false，全局 JIT 也没开，于是第 ④ 关失败，直接跳过。

**练习 2**：为什么簇要有「最小规模」门槛？小于门槛的候选会怎样？

**参考答案**：因为 XLA 的收益来自融合，单 op 或极小簇的编译开销（编译时间、kernel 调度开销）可能抵消甚至超过融合收益，所以默认要求簇至少有一定规模才编译。小于门槛且无控制流、未显式标记的候选不会被 `_XlaCluster` 命中（见第 1032-1034 行），它们照常走普通 TF 内核。

---

### 4.4 build_xla_ops_pass：把标记后的簇改写成 _XlaCompile/_XlaRun

#### 4.4.1 概念说明

到目前为止，簇还只是「图里一组共享 `_XlaCluster` 名字的节点」。要真正让 XLA 编译并运行它们，还需要把这些节点替换成一对特殊的 op：

- `_XlaCompile`：接受簇的输入，**触发** XLA 编译，产出一个「编译键（compilation key）」。
- `_XlaRun`：接受编译键和输入，**运行**编译好的 XLA 可执行文件，产出结果。

`build_xla_ops_pass.cc` 做的就是这个替换。它是聚类流水线的最后一棒，把「打了标记的函数调用节点」翻译成「可被 XLA 运行时识别的 op 对」。

注意它处理的不是原始的簇内节点——那些已经被 `EncapsulateSubgraphsPass` 封装进了一个 TF 函数，簇被替换成了**一个调用该函数的节点**（带上 `_XlaCompiledKernel=true` 等属性）。`BuildXlaOps` 找的就是这种调用节点。

#### 4.4.2 核心流程

`BuildXlaOpsPass::Run` 的流程：

```text
遍历图中所有节点 n：
  若 n 是 Send/Recv/控制流 → 跳过
  若 IsXlaCompiledKernel(n) 为 false → 跳过   # 即没有 _XlaCompiledKernel=true 属性
  否则把 n 收集进待改写列表

对每个待改写的 n，调用 ReplaceNodeWithXlaCompileAndXlaRun(n)：
  ① GetXlaClusterInfo(n)：把 n 的输入拆成 constant / non-constant / resource 三类
  ② InferDeviceForCluster(n)：为这个簇选一台编译设备
  ③ DeviceRequiresCompilation(device)：这台设备的策略是不是 kAlways（必须编译）
  ④ 创建 _XlaCompile 节点（吃三类输入 + must_compile 标志 + 簇函数）
  ⑤ 分两条路径：
     - 严格编译（requires_compilation=true）：
         创建 _XlaRun，把 n 的出边全部改接到 _XlaRun，删除 n
     - 懒惰编译（requires_compilation=false）：
         用 Switch(_XlaCompile.compilation_successful) 分两路：
           成功 → _XlaRun 跑 XLA 结果
           失败 → 原来的 TF 函数调用（改写为 StatefulPartitionedCall）
         再用 _XlaMerge 把两路输出合并
```

「严格」与「懒惰」的区别是本模块的核心，也是下一节（回退机制）的伏笔。

#### 4.4.3 源码精读

入口 `BuildXlaOpsPass::Run`，过滤条件就是 `IsXlaCompiledKernel`：[tensorflow/compiler/jit/build_xla_ops_pass.cc:576-623](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/compiler/jit/build_xla_ops_pass.cc#L576-L623)。第 582-591 行的 lambda 正是「只挑出标记为 XLA 编译内核的函数调用节点」。`IsXlaCompiledKernel` 的实现很简单——查 `_XlaCompiledKernel` 属性是否为 true：[tensorflow/compiler/jit/encapsulate_subgraphs_pass.cc:1326-1332](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/compiler/jit/encapsulate_subgraphs_pass.cc#L1326-L1332)

而 `_XlaCompiledKernel` 这个属性是 `EncapsulateSubgraphsPass` 在把簇封装成函数时打上的，同时还打上了常量/资源参数个数：[tensorflow/compiler/jit/encapsulate_subgraphs_pass.cc:1297-1300](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/compiler/jit/encapsulate_subgraphs_pass.cc#L1297-L1300)

改写的核心函数 `ReplaceNodeWithXlaCompileAndXlaRun`，前半段做输入分类与设备判定：[tensorflow/compiler/jit/build_xla_ops_pass.cc:470-504](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/compiler/jit/build_xla_ops_pass.cc#L470-L504)。其中 `GetXlaClusterInfo`（第 475-476 行）把输入按 `_XlaNumConstantArgs`/`_XlaNumResourceArgs` 属性切成三类：[tensorflow/compiler/jit/build_xla_ops_pass.cc:228-264](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/compiler/jit/build_xla_ops_pass.cc#L228-L264)

`DeviceRequiresCompilation` 判定是否「必须编译」，依据正是 4.3 讲的 `AutoclusteringPolicy::kAlways`：[tensorflow/compiler/jit/build_xla_ops_pass.cc:286-294](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/compiler/jit/build_xla_ops_pass.cc#L286-L294)

随后创建 `_XlaCompile` 节点（第 499-504 行），它的 `must_compile` 参数来自上面的判定。

接下来是分叉点。**严格编译路径**（第 516-524 行）：直接造 `_XlaRun`，把原节点 `n` 的所有出边改接到 `_XlaRun`，然后删掉 `n`。这条路径下，XLA 编译是强制的——编译失败就报错。

**懒惰编译路径**（第 525-570 行）：核心思路在注释里画得很清楚（第 529-537 行）：

```text
(use_tf_call, use_xla_run) =
    Switch(pred=xla_compile.compilation_successful, value=xla_compile.key)

tf_call_outputs = cluster_N(..., ^use_tf_call)        # 走原 TF 函数
xla_run_outputs = _XlaRun(..., key=use_xla_run)       # 走 XLA
outputs = Merge(tf_call_outputs, xla_run_outputs)     # 二选一合并
```

`Switch` 用 `_XlaCompile.compilation_successful` 作为谓词：[tensorflow/compiler/jit/build_xla_ops_pass.cc:538-544](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/compiler/jit/build_xla_ops_pass.cc#L538-L544)。XLA 编译成功，结果走 `_XlaRun`；编译失败或被判定为「不划算」，就走原来的 TF 函数调用（第 564-566 行把它改写成 `StatefulPartitionedCall`）。两路输出再由 `_XlaMerge` 合并（见 `MergeOutgoingDataEdges`）：[tensorflow/compiler/jit/build_xla_ops_pass.cc:160-163](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/compiler/jit/build_xla_ops_pass.cc#L160-L163)

懒惰编译是否启用，由 flag `tf_xla_enable_lazy_compilation` 控制（默认 true）：[tensorflow/compiler/jit/build_xla_ops_pass.cc:593-596](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/compiler/jit/build_xla_ops_pass.cc#L593-L596)

调试用的三个开关（打印簇输出、检查输入/输出数值的 NaN/Inf）也在这里读取：[tensorflow/compiler/jit/build_xla_ops_pass.cc:601-606](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/compiler/jit/build_xla_ops_pass.cc#L601-L606)

#### 4.4.4 代码实践

1. **实践目标**：从源码看清「严格」与「懒惰」两条路径在图结构上的差别。
2. **操作步骤**：
   - 阅读 [tensorflow/compiler/jit/build_xla_ops_pass.cc:516-570](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/compiler/jit/build_xla_ops_pass.cc#L516-L570)。
   - 分别画出两条路径改写后的小图（用 `→` 表示数据边、`^` 表示控制边）。
3. **需要观察的现象**：严格路径改写后图里只有 `_XlaCompile`+`_XlaRun`；懒惰路径改写后图里多了 `Switch`、`StatefulPartitionedCall`、`_XlaMerge` 四种节点，且原 TF 函数调用被保留下来作为兜底。
4. **预期结果**：你会得到两张示意图——严格路径是一条直线，懒惰路径是一个「Y 形」分叉再汇合。纯阅读型实践，无需运行。
5. 结论可直接从源码与注释读出，无需「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：严格编译路径下，如果 XLA 编译失败，会发生什么？

**参考答案**：会直接报错。因为 `must_compile=true` 时 `_XlaCompile` 必须成功，且改写后图里没有 TF 兜底分支（出边全部接到 `_XlaRun`），所以编译失败 = 运行失败。这正是「严格」的含义：要么用 XLA，要么报错，绝不静默回退。

**练习 2**：懒惰编译路径里，为什么还要保留原来的 TF 函数调用节点？

**参考答案**：因为懒惰编译允许 `_XlaCompile` 在运行期根据「是否编译成功 / 是否划算」动态决定走哪条路。保留原 TF 函数调用（改写为 `StatefulPartitionedCall`）就是为了在 XLA 这条路不通时，用 `Switch` 的 false 分支切回普通 TF 执行，保证正确性不受影响。

---

### 4.5 回退机制：遇到不支持的 op 时运行时怎么办

#### 4.5.1 概念说明

这一节正面回答实践任务的后半句：**遇到不支持的 op 时运行时如何处理？**

关键认知是：「不支持」发生在**两个不同阶段**，对应两种完全不同的处理方式，不要混淆：

1. **聚类阶段的不支持**：某个 op 没有 XLA lowering（`IsCompilableNode` 为 false），或设备没注册编译设备，或被黑名单挡住。处理方式是**压根不让它进簇**——它留在普通 TF 图里，成为簇的边界。这不会报错，只是这个 op 不享受 XLA 加速。

2. **编译阶段的不支持**：簇已经形成并改写成了 `_XlaCompile`/`_XlaRun`，但在运行期真正编译时，XLA 可能因为某些原因（比如编译器判定不划算、或者簇内组合在编译时才暴露的问题）拒绝编译。处理方式取决于路径：严格路径会报错；懒惰路径会通过 `Switch`/`Merge` 回退到原 TF 函数调用。

此外还有一个 flag 可以强制「永远不编译、永远回退」：`tf_xla_always_defer_compilation`（见 flags.h），用于调试。

#### 4.5.2 核心流程

把两种「不支持」画成一张决策图：

```text
              ┌──────────── 聚类阶段 (MarkForCompilation) ────────────┐
              │                                                       │
   op 有 XLA lowering 且通过所有检查?                                 │
        ├─ 是 → 进入簇（_XlaCluster）                                │
        └─ 否 → 留在普通 TF 图（成为簇边界，照常用 TF 内核）─┐       │
                                                              │       │
              ┌──────────── 编译阶段 (BuildXlaOps + 运行期) ◀───────┘
              │
   _XlaCompile 运行期能编译成功?
        ├─ 严格路径（kAlways / 关闭 lazy）→ 必须成功，失败即报错
        └─ 懒惰路径（默认）
              ├─ 成功 → Switch true 分支 → _XlaRun（XLA 执行）
              └─ 失败 → Switch false 分支 → StatefulPartitionedCall（TF 执行）
```

所以「遇到不支持的 op」最常见、最安全的结局是：**它根本不进簇**，自然运行时也不会有「回退」动作——它从一开始就在走 TF 内核。只有当一整簇在运行期编译失败时，才轮到懒惰路径的 `Switch`/`Merge` 兜底。

#### 4.5.3 源码精读

聚类阶段的「拒绝」集中在 `FindCompilationCandidates`，每个拒绝点都有日志，尤其 4.3 已引用的 [tensorflow/compiler/jit/mark_for_compilation_pass.cc:1381-1433](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/compiler/jit/mark_for_compilation_pass.cc#L1381-L1433)。其中第 1390-1392 行的 `"could not find JIT device"` 对应「设备不支持」，第 1416 行的 `IsCompilableNode` 失败对应「op 没有 lowering」。

`PartiallyDeclusterPass`（流水线顺序号 30）会进一步把「只被簇外消费者用到的节点」从簇里**拆出来**退回 TF，这是一种「事后精简」的回退，配合 4.4 的懒惰回退构成多层安全网。

编译阶段的回退就是 4.4 讲的 `Switch`/`Merge` 机制，核心代码：[tensorflow/compiler/jit/build_xla_ops_pass.cc:538-570](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/compiler/jit/build_xla_ops_pass.cc#L538-L570)。注意第 562 行 `n->ClearAttr(kXlaCompiledKernelAttr)`——回退分支会清掉这个属性，表示「这个调用在运行期可能不走 XLA」。第 564-566 行把原节点改写成 `StatefulPartitionedCall`，使其成为合法的 TF 兜底执行路径。

强制「永远回退」的调试 flag 在公共 flags 结构里：[tensorflow/compiler/jit/flags.h:148-153](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/compiler/jit/flags.h#L148-L153)。`tf_xla_always_defer_compilation=true` 时 `_XlaCompile` 永远拒绝编译，配合懒惰路径就能让所有簇都走 TF 兜底，用来验证「回退后结果是否正确」。

#### 4.5.4 代码实践

1. **实践目标**：用两个 flag 分别触发两种「不支持」，观察行为差异。
2. **操作步骤**：
   - **场景 A（聚类阶段拒绝）**：写一个含 XLA 不支持 op 的小函数（例如某些只能在 host 跑的 op），用 `@tf.function(jit_compile=True)` 包裹，对照 4.3 的源码预测哪些 op 会被排除在簇外。
   - **场景 B（编译阶段回退）**：在开启 auto-clustering 的前提下，设置 `TF_XLA_FLAGS="--tf_xla_always_defer_compilation=true"`（见 [tensorflow/compiler/jit/flags.h:148-153](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/compiler/jit/flags.h#L148-L153)），运行一个已知可被 XLA 编译的模型，观察它是否仍能正确运行（只是不享受 XLA 加速）。
   - 可选：开启 `--vmodule=xla_compilation_cache=1` 确认 XLA 是否真的被绕过（这个 vmodule 提示来自 `ShouldCompileClusterImpl` 的一次性警告）。
3. **需要观察的现象**：场景 A 中，不支持的 op 不进簇、其余可编译 op 仍可能成簇；场景 B 中，模型结果应与不开启 XLA 时一致，证明回退路径正确。
4. **预期结果**：场景 B 的「结果一致」是验证回退机制正确性的关键判据。**待本地验证**：具体 flag 是否生效、vmodule 输出格式取决于本地构建与运行环境，若无法构建 TF 请回退到「源码阅读型实践」——只读 4.5.3 的代码路径并口述回退链路。
5. 若本地无法运行，请明确按「待本地验证」处理，不要伪造运行结果。

#### 4.5.5 小练习与答案

**练习 1**：聚类阶段被拒绝的 op，和编译阶段被回退的 op，对图结构的影响有什么本质区别？

**参考答案**：聚类阶段被拒的 op 从未进簇，它是**簇的边界节点**，图里只有一个普通 TF 节点；而编译阶段被回退的 op **已经进簇、簇已被封装成函数并改写**，图里会出现 `Switch`/`_XlaMerge`/`StatefulPartitionedCall` 这套兜底结构。前者是「没参与」，后者是「参与后被替换兜底」。

**练习 2**：为什么 `tf_xla_always_defer_compilation` 必须配合懒惰编译（`tf_xla_enable_lazy_compilation=true`）才能用作「回退验证」？

**参考答案**：严格编译路径下 `_XlaCompile` 的 `must_compile=true`，编译必成功否则报错，不存在回退分支；只有懒惰路径才生成了 `Switch`/`StatefulPartitionedCall` 兜底。`always_defer_compilation` 让 `_XlaCompile` 永远返回「不成功」，只有懒惰路径能把这个「不成功」导向 TF 兜底分支，从而实现「永远走 TF 但不报错」。

---

## 5. 综合实践

把本讲的三道关（候选判定 → 聚类改写 → 回退）串起来，完成下面这个贯穿性任务：

**任务**：给定一个含 5 个 op 的小计算图 `A → B → C → D → E`（均为 `float32` 张量上的算术 op，假设都放在 GPU 上），其中 `C` 是一个 XLA **不支持**的 op。请按本讲学到的知识，回答并画出：

1. **聚类阶段**：哪些 op 会成为编译候选？最终会形成几个簇？画出簇的边界（提示：`C` 会被 `IsCompilableNode` 拒绝，于是 `A,B` 和 `D,E` 被它隔开成两个互不相邻的候选组）。
2. **改写阶段**：假设两个簇都达到最小规模并被编译，分别画出它们经 `EncapsulateSubgraphsPass` + `BuildXlaOpsPass` 改写后的图结构（严格路径）。
3. **回退阶段**：如果把全局开关设为懒惰编译，且左簇（`A,B`）在运行期编译失败，画出此时左簇的执行路径（提示：走 `Switch` false 分支 → `StatefulPartitionedCall`）。

**操作建议**：

- 先只读源码完成 1、2 两问（纯纸笔推演），参考 [tensorflow/compiler/jit/mark_for_compilation_pass.cc:1381-1433](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/compiler/jit/mark_for_compilation_pass.cc#L1381-L1433) 与 [tensorflow/compiler/jit/build_xla_ops_pass.cc:516-570](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/compiler/jit/build_xla_ops_pass.cc#L516-L570)。
- 若本地可运行 TF，可构造等价的 `@tf.function`，用 `--tf_xla_clustering_debug`（见 [tensorflow/compiler/jit/flags.h:72](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/compiler/jit/flags.h#L72)）dump 出聚类前后的图，与你的推演对照。**待本地验证**：dump 的具体格式与能否成功取决于本地构建环境。

**评判标准**：能否准确说出「`C` 不进簇导致图被切成两段」、能否画出严格/懒惰两种改写后的拓扑、能否指出懒惰回退依赖 `Switch`/`Merge` 而非报错。

## 6. 本讲小结

- auto-clustering 是一组运行在 `POST_REWRITE_FOR_EXEC` 阶段（放置之后、分区之前）的图优化 pass，顺序为 `MarkForCompilation(10)` → `EncapsulateSubgraphs(50)` → `BuildXlaOps(60)`，靠 `_XlaCluster`、`_XlaCompiledKernel` 等属性串联。
- `xla_cluster_util` 是聚类判定的工具箱，提供环检测图（`CreateCycleDetectionGraph`，会折叠控制流 loop、打断回边）、簇属性读写、ref variable 检测、全局 JIT 等级（区分单 GPU / 一般图）等能力。
- 一个 op 要进簇需连过数关：设备被 XLA 认识、`AutoclusteringPolicy` + 全局开关允许（`ShouldCompile`）、有 XLA lowering（`IsCompilableNode`）、不在黑名单、通过编译期常量约束；候选再经贪心边收缩合并成簇，达最小规模才被打上 `_XlaCluster`。
- `build_xla_ops_pass` 把标记好的簇（函数调用节点）改写成 `_XlaCompile` + `_XlaRun`：严格路径强制编译、失败即报错；懒惰路径用 `Switch(compilation_successful)` 在 XLA 与原 TF 函数调用（`StatefulPartitionedCall`）之间二选一，再用 `_XlaMerge` 合并。
- 「不支持的 op」分两层处理：聚类阶段不支持 → 不进簇、留作 TF 边界节点（最常见、最安全）；编译阶段不支持 → 严格路径报错，懒惰路径经 `Switch`/`Merge` 静默回退到 TF；`tf_xla_always_defer_compilation` 可强制走回退以做正确性验证。

## 7. 下一步学习建议

- 本讲聚焦「怎么把 op 打包成 XLA 簇」，但**簇内部如何被编译成设备代码**属于 XLA 编译器本身。建议接着阅读 `tensorflow/compiler/jit/device_compiler.h` 与 `xla/service/` 下的 HLO 优化流水线，理解 `_XlaCompile` 触发后真正发生了什么（承接 u7-l2）。
- 若对「为什么有些 op 没有 XLA lowering」感兴趣，可读 `tensorflow/compiler/tf2xla/` 下的 kernel 注册（`XlaOpRegistry::RegisterOp`），看一个 op 如何声明自己的 XLA 实现。
- 想了解聚类决策的「可观测性」，可读 `tensorflow/compiler/jit/report_clustering_info_pass.cc`（顺序号 40）与 `xla_activity.proto`，它把聚类摘要通过 listener 广播出去，是线上排查「为什么我的 op 没被 XLA 编译」的官方抓手。
- 下一讲 u7-l4 将转向 TFRT 新一代运行时，届时可对比本讲的 `_XlaCompile`/`_XlaRun` 在传统 `DirectSession` 执行器里是如何被调度的。
