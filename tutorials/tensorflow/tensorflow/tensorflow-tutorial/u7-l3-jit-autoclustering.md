# JIT 自动聚类

> 本讲承接 [u7-l2 XLA / StableHLO 与 tf2xla](u7-l2-xla-stablehlo-tf2xla.md)。上一讲回答了「一个 TF 子图如何被翻译成 XLA 计算」，本讲回答它的前一个问题：**运行时怎么自动从一整张计算图里挑出「值得编译」的子图，并把它们交给 XLA，同时保证不支持的 op 不出错。**

---

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `MarkForCompilationPass` → `EncapsulateSubgraphsPass` → `BuildXlaOpsPass` 这条「自动聚类」流水线的分工与执行顺序；
- 读懂 `build_xla_ops_pass.cc`，说清它如何把一个被标记的簇函数调用改写成 `_XlaCompile` + `_XlaRun`；
- 读懂 `xla_cluster_util.h`，说清它为聚类提供了哪些「判定 + 安全检查」工具；
- 判断**哪些 op 会被聚成一个 XLA 可编译簇**，以及**遇到 XLA 不支持的 op 时运行时如何回退**到普通 TensorFlow 执行。

本讲覆盖两个最小模块：`compiler.jit.build_xla_ops_pass` 与 `compiler.jit.xla_cluster_util`。

---

## 2. 前置知识

### 2.1 什么是 JIT、为什么要聚类

XLA（见 u7-l2）能把一段计算编译成高度优化的设备代码（算子融合、缓冲区复用）。但 XLA 不是「要么全图编译、要么不编译」——一整张训练图里常常夹杂着 XLA 暂不支持的 op、必须落在 host 的 op、副作用 op。如果把它们硬塞进一个 XLA 计算，编译会直接失败。

所以 TF 采取的策略是 **auto-clustering（自动聚类）**：在图被执行前，运行一组「图优化 pass」，把图里**连续的、可编译的 op** 合并成一个一个的「簇（cluster）」，每个簇单独交给 XLA 编译；簇之外的 op 照常用 TF 原生 kernel 跑。这就把「用 XLA 加速」做成了对用户透明的、尽力而为（best-effort）的行为。

> 关键直觉：聚类 = 把图切成「XLA 块」和「TF 块」交替的拼图，块与块之间用普通张量边相连。

### 2.2 图优化 pass 是什么

在 u3-l2 中我们见过，`DirectSession::Run` 在真正调度 op 之前会跑一组「图优化 pass」（包括 u6-l3 讲的 Grappler）。本讲的几个 pass 也注册在同一个 `OptimizationPassRegistry` 里，只是阶段不同。一个 pass 就是一个继承 `GraphOptimizationPass`、实现 `Run(options)` 的类，按 `(阶段, 序号)` 全局排序依次执行。`BuildXlaOpsPass` 正是这样一个 pass（见其基类定义 [build_xla_ops_pass.h:28-41](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/jit/build_xla_ops_pass.h#L28-L41)）。

### 2.3 几个会反复出现的属性名

聚类是靠给图节点打「属性标签」来传递信息的。先把这几个字符串记住，后面源码里都是它们：

| 属性名 | 含义 | 定义处 |
|---|---|---|
| `_XlaCluster` | 节点所属的簇名（如 `cluster_0`） | `xla_cluster_util.cc:68` |
| `_XlaCompile` | 用户显式要求该 op 编译（`true`/`false`） | `defs.h:32` |
| `_XlaMustCompile` | 必须 编译，否则报错 | `defs.h:28` |
| `_XlaCompiledKernel` | 封装 pass 给函数调用节点打的「这是 XLA 簇」标记 | `encapsulate_subgraphs_pass.cc:60` |
| `_XlaNumConstantArgs` / `_XlaNumResourceArgs` | 簇的输入里有多少是常量 / 资源变量 | `encapsulate_subgraphs_pass.cc:61-62` |

---

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `tensorflow/compiler/jit/jit_compilation_pass_registration.cc` | 把三个聚类 pass 按顺序注册进优化 pass 注册表——本讲「流水线」的源头 |
| `tensorflow/compiler/jit/mark_for_compilation_pass.cc` | **决定哪些 op 聚成一个簇**，给节点打 `_XlaCluster` 属性（上游判定，本讲需对照阅读） |
| `tensorflow/compiler/jit/xla_cluster_util.h` / `.cc` | **本讲重点模块之一**：聚类用到的判定与安全检查工具（簇属性读写、环检测、JIT 等级、ref 变量安全） |
| `tensorflow/compiler/jit/encapsulate_subgraphs_pass.cc` | 把每个簇封装成一个 TF 函数调用节点，并打 `_XlaCompiledKernel` |
| `tensorflow/compiler/jit/build_xla_ops_pass.cc` | **本讲重点模块之二**：把带 `_XlaCompiledKernel` 的函数调用改写成 `_XlaCompile` + `_XlaRun` |
| `tensorflow/compiler/jit/ops/xla_ops.cc` | `_XlaCompile` / `_XlaRun` / `_XlaMerge` 三个 op 的声明（`REGISTER_OP`） |
| `tensorflow/compiler/jit/flags.h` | 控制聚类行为的命令行 flag（`--tf_xla_auto_jit`、`tf_xla_enable_lazy_compilation` 等） |
| `tensorflow/compiler/jit/compilability_check_util.h` | `RecursiveCompilabilityChecker`，递归判定一个 op（含其调用的函数）能否被 XLA 编译 |

---

## 4. 核心概念与源码讲解

### 4.1 自动聚类的整体流水线

#### 4.1.1 概念说明

「自动聚类」不是单一 pass 完成的，而是**三个 pass 接力**：

1. **MarkForCompilationPass（标记）**：扫描全图，判断每个 op 能否被 XLA 编译，把连续的可编译 op 用「边收缩」合并成簇，给簇内每个节点打上 `_XlaCluster = "cluster_N"` 属性。**它只做标记，不改图结构。**
2. **EncapsulateSubgraphsPass（封装）**：把每个簇（同一 `_XlaCluster` 值的连通子图）封装成一个独立的 TF 函数，并在原图里用一个函数调用节点代替整簇；同时给这个调用节点打上 `_XlaCompiledKernel = true`。
3. **BuildXlaOpsPass（改写，本讲主角）**：找到所有带 `_XlaCompiledKernel = true` 的函数调用节点，把它们替换成 `_XlaCompile`（编译）+ `_XlaRun`（执行）这对 op，从而在运行期真正触发 XLA 编译与执行。

理解这条接力链至关重要：`build_xla_ops_pass` **并不自己判断哪些 op 该编译**，它只是流水线的最后一棒，消费前两棒的产物（`_XlaCompiledKernel` 标记）。

#### 4.1.2 核心流程

三个 pass 的注册顺序写在同一个文件里，靠 `REGISTER_OPTIMIZATION(阶段, 序号, Pass类)` 宏登记，运行时按序号从小到大执行：

```text
POST_REWRITE_FOR_EXEC 阶段:
   5  CloneConstantsForBetterClusteringPass   ← 克隆常量，改善聚类
   9  ClusterScopingPass                        ← 预先划定 _XlaScope 边界
  10  MarkForCompilationPass    ★ 判定+标记 _XlaCluster
  12  ForceXlaConstantsOnHostPass
  20  IncreaseDynamismForAutoJitPass
  30  PartiallyDeclusterPass                    ← 把不该在簇里的 op 拆出去
  40  ReportClusteringInfoPass                  ← 汇总聚类决策并广播
  50  EncapsulateSubgraphsPass  ★ 封装成函数 + 打 _XlaCompiledKernel
  60  BuildXlaOpsPass           ★ 改写为 _XlaCompile/_XlaRun
```

序号的含义是「依赖关系的时间化」：50 必须在 10 之后（要先有簇才能封装），60 必须在 50 之后（要先封装出函数调用才能改写）。它们都注册在 **`POST_REWRITE_FOR_EXEC`** 阶段——也就是「为执行而改写」阶段，发生在放置（placement）之后、分区（partition）之前（参见 u3-l2 讲的 `CreateGraphs` 流程），所以聚类结果会随图一起被分区下发到各设备。

#### 4.1.3 源码精读

注册代码全部集中在这个短文件里：

[compiler/jit/jit_compilation_pass_registration.cc:46-82](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/jit/jit_compilation_pass_registration.cc#L46-L82) — 把三个核心 pass 按 `POST_REWRITE_FOR_EXEC` 阶段的 10 / 50 / 60 三个序号注册。注释也点明了它们的先后约束：

```cpp
REGISTER_OPTIMIZATION(OptimizationPassRegistry::POST_REWRITE_FOR_EXEC, 10,
                      MarkForCompilationPass);
// ...
// The EncapsulateSubgraphs pass must run after the MarkForCompilationPass.
REGISTER_OPTIMIZATION(OptimizationPassRegistry::POST_REWRITE_FOR_EXEC, 50,
                      EncapsulateSubgraphsPass);
// Must run after EncapsulateSubgraphsPass.
REGISTER_OPTIMIZATION(OptimizationPassRegistry::POST_REWRITE_FOR_EXEC, 60,
                      BuildXlaOpsPass);
```

#### 4.1.4 代码实践

**实践目标**：用源码确认三个 pass 的相对顺序与阶段（纯阅读型实践）。

**操作步骤**：

1. 打开 [jit_compilation_pass_registration.cc:46-82](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/jit/jit_compilation_pass_registration.cc#L46-L82)。
2. 找到三处 `REGISTER_OPTIMIZATION(... MarkForCompilationPass / EncapsulateSubgraphsPass / BuildXlaOpsPass)`。
3. 记下每处的阶段名与序号。

**需要观察的现象**：三个 pass 都在同一个阶段、序号依次递增（10 < 50 < 60）。

**预期结果**：你能用一句话复述——「标记(10) → 封装(50) → 改写(60)」严格按序号排序执行，序号即依赖。结论可直接从源码读出，无需运行。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `BuildXlaOpsPass` 的注册序号从 60 改成 5（跑到 `MarkForCompilationPass` 之前），会发生什么？

**参考答案**：`BuildXlaOpsPass::Run` 靠 `IsXlaCompiledKernel(*n)` 寻找目标节点，而 `_XlaCompiledKernel` 属性是 `EncapsulateSubgraphsPass`（序号 50）才打上的。若 60 提前到 5，此时图里没有任何带该属性的节点，待改写列表为空，pass 啥也不做就返回——XLA 永远不会被触发。这正是「序号即依赖」的含义。

**练习 2**：聚类为什么放在 `POST_REWRITE_FOR_EXEC`（放置之后）而不是更早的 `PRE_PLACEMENT`？

**参考答案**：聚类判定（能不能编译、设备是否兼容、簇内是否同设备）依赖每个节点的 `assigned_device_name`，而设备是放置 pass 才分配的，所以聚类只能放在放置之后。

---

### 4.2 `xla_cluster_util`：聚类判定的辅助工具与安全分析

#### 4.2.1 概念说明

`xla_cluster_util` 是聚类子系统的**公共工具箱**。`MarkForCompilationPass`（判定）和 `BuildXlaOpsPass`（改写）都会调用它。它本身不做聚类决策，但提供四类能力：

1. **簇属性的读写**：`_XlaCluster` 属性的读取（`GetXlaClusterForNode`）与清除（`RemoveFromXlaCluster`）；
2. **环检测图构造**：聚类要把多个 op 合并，必须保证合并后不产生环（否则执行会死锁）；
3. **全局 JIT 等级判定**：决定这张图到底开不开 XLA（`GetGlobalJitLevelForGraph`）；
4. **安全分析**：ref 变量（引用型张量）相关节点的传播分析、聚类摘要统计。

把它单独拎出来讲，是因为它把聚类要面对的几个难点**显式地**变成了可调用的 C++ 函数，是理解聚类「为什么安全」的钥匙。

#### 4.2.2 核心流程

**簇属性读写**很简单——节点带不带 `_XlaCluster`、值是多少，决定它属于哪个簇：

```text
GetXlaClusterForNode(node):
    在 node.attrs() 里找 "_XlaCluster"
    找到 → 返回簇名字符串
    没找到 → 返回 nullopt
```

**环检测**是聚类正确性的命脉。聚类通过「边收缩」把两个相邻 op 合并，一旦合并跨过了图里已有的依赖路径，就会形成环。`CreateCycleDetectionGraph` 把整张图复制成一个专用于找环的 `GraphCycles` 结构，每次尝试收缩前先问它「加这条边会不会成环」。难点是 TF 图合法地含循环（while loop），朴素判环会误报，于是它把每个循环折叠成一个 frame 节点、并打断 `NextIteration` 回边。

**全局 JIT 等级**回答「这张图要不要聚类」，综合三处来源：`ConfigProto` 里的 `global_jit_level`、命令行 flag `--tf_xla_auto_jit`、以及「是否单 GPU 图」。只有最终等级 ≠ `OFF` 时，标记 pass 才会真正去聚类。

#### 4.2.3 源码精读

簇属性常量定义在这里，是整个子系统引用同一字符串的「唯一真相源」：

[compiler/jit/xla_cluster_util.cc:68-70](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/jit/xla_cluster_util.cc#L68-L70)：

```cpp
const char* const kXlaClusterAttr = "_XlaCluster";
const char* const kXlaCompileTimeConstantInputsAttr =
    "_XlaCompileTimeConstantInputs";
```

读取节点所属簇——就是查属性，注意它还做了类型校验（属性必须是 string）：

[compiler/jit/xla_cluster_util.cc:226-236](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/jit/xla_cluster_util.cc#L226-L236)：

```cpp
std::optional<absl::string_view> GetXlaClusterForNode(const Node& node) {
  const AttrValue* attr_value = node.attrs().Find(kXlaClusterAttr);
  if (attr_value == nullptr) return std::nullopt;
  absl::Status s = AttrValueHasType(*attr_value, "string");
  if (!s.ok()) return std::nullopt;
  return attr_value->s();
}
```

环检测图构造是最精巧的一段，核心思想在注释里说得很清楚——把循环折叠成 frame 节点、打断回边，让循环内部对环检测器「不可见」：

[compiler/jit/xla_cluster_util.cc:135-224](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/jit/xla_cluster_util.cc#L135-L224) — `CreateCycleDetectionGraph`：

```cpp
// To handle loops, we alter the structure of the cycle detection graph,
// disconnecting each loop from the enclosing graph. Specifically, we:
// * add a new "frame" node for each loop.
// * replace edges to "Enter" nodes, and edges from "Exit" nodes with edges
//   to/from the corresponding frame node. ... collapse the loop into a
//   single node ...
// * ... break loop backedges (edges outgoing from "NextIteration" nodes).
```

当它遇到无法处理的环时返回 `false`，调用方（标记 pass）会据此整体放弃聚类，保证安全。

全局 JIT 等级的综合判定——注意单 GPU 图与一般图可以取不同等级，最后据 `IsSingleGpuGraph` 二选一：

[compiler/jit/xla_cluster_util.cc:314-332](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/jit/xla_cluster_util.cc#L314-L332) — `GetGlobalJitLevelForGraph`：

```cpp
OptimizerOptions::GlobalJitLevel result =
    IsSingleGpuGraph(**options.graph) ? xla_global_jit_level.single_gpu
                                      : xla_global_jit_level.general;
```

聚类摘要——把整张图的聚类结果统计成一个 protobuf，供 `ReportClusteringInfoPass` 上报：

[compiler/jit/xla_cluster_util.cc:384-418](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/jit/xla_cluster_util.cc#L384-L418) — `GetXlaAutoClusteringSummary`，遍历所有节点，按 `GetXlaClusterForNode` 是否有值分为「已聚类 / 未聚类」两堆并各做 op 直方图。

ref 变量安全分析——用「向前 + 向后两个方向迭代到不动点」的方式，找出所有和 ref 变量有可达路径的节点，供封装 pass 打 `_XlaHasReferenceVars` 属性：

[compiler/jit/xla_cluster_util.cc:609-620](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/jit/xla_cluster_util.cc#L609-L620) — `GetNodesRelatedToRefVariables`。

> 这些函数的头文件声明见 [xla_cluster_util.h:58-93](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/jit/xla_cluster_util.h#L58-L93)，注释里说明了每个函数的契约。

#### 4.2.4 代码实践

**实践目标**：搞清「这张图到底开了 XLA 没有」由哪几个因素决定（纯阅读型实践）。

**操作步骤**：

1. 阅读 [xla_cluster_util.cc:254-280](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/jit/xla_cluster_util.cc#L254-L280) 里的 `GetXlaGlobalJitLevel`，列出它读取的三个来源。
2. 阅读 [flags.h:33-46](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/jit/flags.h#L33-L46) 里 `XlaAutoJitFlag` 的注释，弄清 `--tf_xla_auto_jit` 取 `0/-1/1/2` 分别是什么含义。
3. 用一句话写出三者的优先级。

**需要观察的现象**：`--tf_xla_auto_jit`（非 DEFAULT 时）如何覆盖 `ConfigProto` 的设置；`DEFAULT` 时默认是关的。

**预期结果**：优先级为「`--tf_xla_auto_jit`（非 DEFAULT 时）> `ConfigProto.global_jit_level` > 默认 OFF」。即用户代码里 `tf.config.optimizer.set_jit(True)` 会被环境变量 `TF_XLA_FLAGS="--tf_xla_auto_jit=2"` 覆盖。结论可直接从源码读出。

#### 4.2.5 小练习与答案

**练习 1**：`CreateCycleDetectionGraph` 为什么要打断 `NextIteration` 节点的出边？

**参考答案**：`NextIteration` 是 while 循环的回边，它让图合法地含环。找环算法若看到这条回边会误判「有环」从而禁止一切聚类。打断回边、并把整个循环折叠成一个 frame 节点，既消除了假环，又保留了「循环作为一个整体与外部图的关系」用于真正的环检测。

**练习 2**：`GetXlaClusterForNode` 返回 `nullopt` 代表什么？后续 pass 会如何对待这样的节点？

**参考答案**：代表该节点不属于任何 XLA 簇（未被标记或被主动 decluster）。`EncapsulateSubgraphsPass` 不会把它封装进函数，`BuildXlaOpsPass` 也不会改写它——它将照常用 TF 原生 kernel 执行。

---

### 4.3 `build_xla_ops_pass`：把簇函数调用改写为 `_XlaCompile` + `_XlaRun`

#### 4.3.1 概念说明

经过前两棒，图里每个 XLA 簇已经被 `EncapsulateSubgraphsPass` 变成了一个「函数调用节点」，并带着 `_XlaCompiledKernel = true` 标记。但「调用一个 TF 函数」本身并不会触发 XLA 编译——它只是普通地执行那个函数体（里面还是原来的 TF op）。

`BuildXlaOpsPass` 的职责就是**把这层「普通函数调用」替换成真正的 XLA 编译与执行入口**：

- `_XlaCompile`：接收簇的输入，把对应的 TF 函数编译成 XLA 可执行体，产出一个字符串 `key`（用于查找编译产物）和一个 `compilation_successful` 布尔。
- `_XlaRun`：接收输入和 `key`，查到编译产物并真正在设备上跑出结果。

> 直觉：`_XlaCompile` 是「编译器售票处」，`_XlaRun` 是「检票上车」。`build_xla_ops_pass` 就是把原来的「步行（普通函数调用）」改写成「先去售票处买票、再上车」。

#### 4.3.2 核心流程

`BuildXlaOpsPass::Run` 的主干非常短，分三步：

```text
1. 扫描全图，挑出所有 IsXlaCompiledKernel(*n)==true 的节点
   （跳过 Send/Recv/控制流节点），存入 xla_compiled_kernels。
2. 读取 lazy compilation 配置（默认开启）。
3. 对每个目标节点，调用 ReplaceNodeWithXlaCompileAndXlaRun 改写。
```

而 `ReplaceNodeWithXlaCompileAndXlaRun` 内部有**两条路径**，由 `requires_compilation` 决定：

- **严格编译（strict）**：当设备要求必须编译（XlaDevice，`autoclustering_policy == kAlways`），或显式关闭了 lazy 编译时走这条。建一个 `_XlaCompile` + 一个 `_XlaRun`，`_XlaRun` 的输出直接顶替原节点的输出，原节点删除。编译失败就报错。
- **惰性编译（lazy，默认）**：建 `_XlaCompile` 后，用一个 `Switch` 节点以 `compilation_successful` 为条件把 `key` 一分为二——编译成功走 `_XlaRun`，编译失败/决定不编译走原来的 `StatefulPartitionedCall`，两路输出用 `_XlaMerge` 合并后顶替原节点输出。

惰性路径是「尽力而为」的关键：**它永远保留了一条 TF 原生执行的退路**。

#### 4.3.3 源码精读

先看主干 `Run`。注意第一步如何用 `IsXlaCompiledKernel` 筛选目标——这是本 pass 与封装 pass 之间的「握手协议」：

[compiler/jit/build_xla_ops_pass.cc:576-623](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/jit/build_xla_ops_pass.cc#L576-L623)：

```cpp
absl::Status BuildXlaOpsPass::Run(const GraphOptimizationPassOptions& options) {
  Graph* graph = options.graph->get();
  // Copy out ... to avoid modifying the graph while we iterate ...
  std::vector<Node*> xla_compiled_kernels;
  absl::c_copy_if(graph->op_nodes(), std::back_inserter(xla_compiled_kernels),
                  [](const Node* n) {
                    if (n->IsSend() || n->IsRecv() || n->IsControlFlow())
                      return false;
                    // Only compile nodes that are marked for compilation ...
                    return IsXlaCompiledKernel(*n);
                  });
  ...
  for (Node* n : xla_compiled_kernels) {
    TF_RETURN_IF_ERROR(ReplaceNodeWithXlaCompileAndXlaRun(...));
  }
```

`IsXlaCompiledKernel` 的实现——就是查 `_XlaCompiledKernel` 属性：

[compiler/jit/encapsulate_subgraphs_pass.cc:1326-1332](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/jit/encapsulate_subgraphs_pass.cc#L1326-L1332)：

```cpp
bool IsXlaCompiledKernel(const Node& node) {
  bool is_compiled = false;
  bool has_compilation_attr =
      TryGetNodeAttr(node.attrs(), kXlaCompiledKernelAttr, &is_compiled) &&
      is_compiled;
  return has_compilation_attr ? is_compiled : false;
}
```

而 `_XlaCompiledKernel` 这个属性是封装 pass 在把簇封装成函数时打上的，同时还打上了常量/资源参数个数：

[compiler/jit/encapsulate_subgraphs_pass.cc:1297-1300](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/jit/encapsulate_subgraphs_pass.cc#L1297-L1300)：

```cpp
        AddNodeAttr(kXlaCompiledKernelAttr, true, node);
        AddNodeAttr(kXlaNumConstantArgsAttr, num_consts, node);
        AddNodeAttr(kXlaNumResourceArgsAttr, num_resources, node);
```

再看 `requires_compilation` 怎么定——`DeviceRequiresCompilation` 查设备的 `autoclustering_policy`，若非 `kAlways` 且 lazy 编译开启，就走 lazy：

[compiler/jit/build_xla_ops_pass.cc:483-488](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/jit/build_xla_ops_pass.cc#L483-L488)：

```cpp
  bool requires_compilation;
  TF_RETURN_IF_ERROR(DeviceRequiresCompilation(*device_info_cache, device,
                                               &requires_compilation));
  if (!lazy_compilation_enabled) {
    requires_compilation = true;  // 关掉 lazy → 全部强制编译
  }
```

`_XlaCompile` 节点的创建——注意它把簇的输入分成 `constants`、`args`、`resources` 三类（这个分类来自封装 pass 打的 `_XlaNumConstantArgs`/`_XlaNumResourceArgs` 属性）：

[compiler/jit/build_xla_ops_pass.cc:499-504](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/jit/build_xla_ops_pass.cc#L499-L504)：

```cpp
  ops::_XlaCompile xla_compile(root.WithOpName("xla_compile"),
                               /*constants=*/cluster_info.constant_inputs,
                               /*args=*/cluster_info.non_constant_inputs,
                               /*resources=*/cluster_info.resource_inputs,
                               /*must_compile=*/requires_compilation,
                               cluster_info.function);
```

惰性路径的核心——`Switch` 按 `compilation_successful` 分流，`_XlaRun` 走 true 分支，原函数调用走 false 分支，最后 `_XlaMerge` 合流：

[compiler/jit/build_xla_ops_pass.cc:525-551](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/jit/build_xla_ops_pass.cc#L525-L551)（注释里的伪代码图极有参考价值）：

```cpp
    // We generate the following graph:
    //   (use_tf_call, use_xla_run) =
    //       Switch(pred=xla_compile.compilation_successful,
    //              value=xla_compile.key)
    //   tf_call_outputs = cluster_N(..., ^use_tf_call)
    //   xla_run_outputs = _XlaRun(..., key=use_xla_run)
    //   outputs = Merge(tf_call_outputs, xla_run_outputs).
    ops::Switch s(root.WithOpName("predicated_compilation_key"),
                  xla_compile.key, xla_compile.compilation_successful);
    Output predicated_compilation_key = s.output_true;          // 编译成功时有效
    Output inverse_predicated_compilation_key = s.output_false; // 编译失败时有效

    ops::_XlaRun xla_run(root.WithOpName("xla_run"), xla_run_args,
                         predicated_compilation_key, n->output_types());
```

`MergeOutgoingDataEdges` 里正是用 `_XlaMerge` 把「原函数调用输出」与「`_XlaRun` 输出」合并：

[compiler/jit/build_xla_ops_pass.cc:160-163](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/jit/build_xla_ops_pass.cc#L160-L163)：

```cpp
      ops::_XlaMerge xla_merge_op(s.WithOpName("merge_oidx_", oidx),
                                  Output(old_node, oidx), new_output);
      merged_output = merged_outputs[oidx] = xla_merge_op.output;
```

这三个 op 的声明也印证了它们的契约，尤其 `_XlaCompile` 的 `compilation_successful` 输出正是「运行时回退」的开关：

[compiler/jit/ops/xla_ops.cc:64-91](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/jit/ops/xla_ops.cc#L64-L91) — `_XlaCompile` 的文档明说：当 `must_compile=false` 时，它**可以**根据盈利性启发式决定不编译，此时 `compilation_successful=false`：

```
compilation_successful: If the `must_compile` attr is false the _XlaCompile op
   can decide not to compile the clusters based on some profitability
   heuristics.  In that case `compilation_successful` is false ...
```

#### 4.3.4 代码实践

**实践目标**：在源码层面区分「严格编译」与「惰性编译」两条路径各自生成的图结构（纯阅读型实践）。

**操作步骤**：

1. 打开 [build_xla_ops_pass.cc:516-570](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/jit/build_xla_ops_pass.cc#L516-L570)。
2. 在严格分支（`if (requires_compilation)`）里，列出它创建了哪些节点、原节点 `n` 的去向。
3. 在惰性分支（`else`）里，列出 `Switch` / `_XlaRun` / `StatefulPartitionedCall` / `_XlaMerge` 各自的角色。
4. 画出两种分支的数据流草图。

**需要观察的现象**：严格分支删掉原节点 `n`（`g->RemoveNode(n)`），惰性分支保留它（改成 `StatefulPartitionedCall` 作为退路）并 `n->ClearAttr(kXlaCompiledKernelAttr)`。

**预期结果**：严格分支 = 单条 `_XlaCompile→_XlaRun` 链；惰性分支 = `_XlaCompile` 产出 `compilation_successful`，经 `Switch` 分成 XLA 与 TF 两路，再 `_XlaMerge` 合流。结论可直接从源码与注释读出，无需运行。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `_XlaCompile` 被声明为 `.SetIsStateful()`？

**参考答案**：因为编译结果要存进进程级的编译缓存（同一个簇不必重复编译），`_XlaCompile` 的行为依赖并修改这份全局状态，是「有状态」的 op。`_XlaRun` 同理（XLA 随机数生成等也有状态）。有状态 op 不会被常量折叠等优化随意重排或消除。

**练习 2**：惰性路径里，为什么控制边要用 `MergeOutgoingControlEdges` 做一套「控制↔数据↔控制」的转换，而不能直接合并？

**参考答案**：TF 没有原生的「控制边 Merge」。于是代码先把两路控制边各自经 `Const` 转成数据（`ControlToData`），用普通 `Merge` 合并数据，再把合并后的数据经 `Identity` 转回控制（`DataToControl`）。注释里画了完整的接线图，可对照阅读 [build_xla_ops_pass.cc:78-105](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/jit/build_xla_ops_pass.cc#L78-L105)。

---

### 4.4 哪些 op 会被聚类、不支持时如何回退

#### 4.4.1 概念说明

本模块把前面三节串起来，直接回答实践任务的两个问题：

1. **哪些 op 会被聚成一个 XLA 可编译簇？**——由 `MarkForCompilationPass` 判定，依据是「设备支不支持编译」+「op 本身可不可编译」+「簇够不够大」+「不破坏安全约束」。
2. **遇到不支持的 op 怎么办？**——分两层回退：编译期，不可编译的 op 根本不会被吸进簇（标记 pass 直接跳过）；运行期，即使进了簇，`_XlaCompile` 仍可在惰性模式下决定「不编译」，此时 `_XlaMerge` 把执行回退到普通 TF 函数调用。

> 一句话：聚类是「双层尽力而为」——编译期尽量挑、挑漏了或不想编的，运行期还有 TF 原生兜底。

#### 4.4.2 核心流程

**编译期判定（每个 op 是否成为聚类候选）**，依次过这几关，任何一关失败就跳过：

```text
对图中每个 node:
  ① 设备能找到 JIT 编译设备吗? (XlaOpRegistry::GetCompilationDevice)
       └ 否 → 跳过（该设备类型根本不支持 XLA）
  ② autoclustering_policy + global_jit_level 决定要不要编译? (ShouldCompile)
       └ 否 → 跳过
  ③ 递归判定该 op 可编译吗? (RecursiveCompilabilityChecker::IsCompilableNode)
       └ 否 → 跳过（op 或它调用的子函数里有 XLA 不支持的东西）
  ④ 特殊排除：DT_STRING 的 Const、不在 allowlist 里的 op ...
       └ 命中 → 跳过
  → 全部通过，加入 compilation_candidates
```

随后，「边收缩」把相邻候选合并成簇，但只有 **有效规模 ≥ 最小簇规模**（或含函数式控制流、或用户显式 `_XlaCompile=true`）的簇才会真正被打上 `_XlaCluster` 属性——太小的簇不值得为它付出编译开销。

**运行期回退（惰性编译）**：

```text
_XlaCompile 运行:
   must_compile=false 时，可按盈利性启发式决定编不编
   → 产出 compilation_successful (true/false)

Switch(pred=compilation_successful, value=key):
   true  → _XlaRun 用编译好的 XLA 可执行体算结果   (快路径)
   false → StatefulPartitionedCall 跑原 TF 函数    (回退路径)

_XlaMerge(回退输出, XLA输出) → 真正给下游的值
```

无论走哪条，对下游消费者都透明——它们只看到 `_XlaMerge` 的输出。

#### 4.4.3 源码精读

编译期判定的四关，集中在 `MarkForCompilationPassImpl` 里：

[compiler/jit/mark_for_compilation_pass.cc:1387-1418](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/jit/mark_for_compilation_pass.cc#L1387-L1418)：

```cpp
    // ① 找编译设备
    const XlaOpRegistry::DeviceRegistration* registration;
    if (!XlaOpRegistry::GetCompilationDevice(device_type.type(), &registration)) {
      VLOG(2) << "Rejecting " << node->name() << ": could not find JIT device ...";
      continue;
    }
    // ② 策略 + 等级
    auto policy = registration->autoclustering_policy;
    if (!ShouldCompile(is_xla_compile_attr_true, device_type, policy)) continue;
    // ③ 递归可编译性
    RecursiveCompilabilityChecker checker(filter, DeviceType{...});
    if (!checker.IsCompilableNode(*node, lib_runtime)) continue;
```

`RecursiveCompilabilityChecker` 的声明——注意「递归」二字：它会跟进 op 调用的子函数一并检查：

[compiler/jit/compilability_check_util.h:58](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/jit/compilability_check_util.h#L58) — `class RecursiveCompilabilityChecker`。

「簇够大才标记」的门槛——`effective_cluster_size` 与 `min_cluster_size`、函数式控制流、显式标记四者满足其一即可：

[compiler/jit/mark_for_compilation_pass.cc:1032-1051](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/jit/mark_for_compilation_pass.cc#L1032-L1051)：

```cpp
    if (cluster->effective_cluster_size() >= debug_options_.min_cluster_size ||
        cluster->has_functional_control_flow() ||
        cluster->is_xla_compile_attr_true()) {
      ...
      n->AddAttr(kXlaClusterAttr, name);   // 正式打上簇标记
      n->AddAttr(kXlaAlreadyClustered, true);
    }
```

运行期回退的最终落点——即 4.3.3 节的 `Switch` + `_XlaMerge`。再补一处佐证：惰性分支末尾会把原节点改写成 `StatefulPartitionedCall` 作为回退执行体：

[compiler/jit/build_xla_ops_pass.cc:562-566](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/jit/build_xla_ops_pass.cc#L562-L566)：

```cpp
    n->ClearAttr(kXlaCompiledKernelAttr);  // 原节点不再是"XLA 节点"
    TF_ASSIGN_OR_RETURN(Node* const pco, ReplaceFunctionCallWithPartitionedCall(
                                             options, flib_def, n, g,
                                             cluster_info.function, root));
```

`ReplaceFunctionCallWithPartitionedCall` 创建的就是退路——一个普通的 `StatefulPartitionedCall`：

[compiler/jit/build_xla_ops_pass.cc:297-346](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/jit/build_xla_ops_pass.cc#L297-L346)。

把两层回退总结成一张表：

| 层级 | 发生时机 | 负责的代码 | 不支持 op 的归宿 |
|---|---|---|---|
| 编译期回退 | 标记 pass（序号 10） | `IsCompilableNode` 返回 false | op 不进簇，原样用 TF kernel 跑 |
| 运行期回退 | 执行时（惰性模式） | `_XlaCompile` 返回 `compilation_successful=false` | `Switch` 路由到 `StatefulPartitionedCall`，`_XlaMerge` 透传结果 |

#### 4.4.4 代码实践

**实践目标**（即讲义规格指定的实践）：对照 `xla_cluster_util.h` 与相关源码，说明哪些 op 会被聚成一个 XLA 可编译簇，遇到不支持的 op 时运行时如何处理。

**操作步骤**：

1. 打开 [xla_cluster_util.h](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/jit/xla_cluster_util.h)，通读其中声明的工具函数，注意它们服务于「判定 / 安全」而非「下结论」——真正下结论的是 `mark_for_compilation_pass.cc`。
2. 跟随 `GetXlaClusterForNode`（[xla_cluster_util.cc:226](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/jit/xla_cluster_util.cc#L226)）理解「一个 op 属于哪个簇」如何被读取，这是聚类信息的标准查询入口。
3. 对照 [mark_for_compilation_pass.cc:1387-1418](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/jit/mark_for_compilation_pass.cc#L1387-L1418) 写出「成为候选」的四道关卡。
4. 对照 [build_xla_ops_pass.cc:525-551](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/jit/build_xla_ops_pass.cc#L525-L551) 写出「运行期不支持时的回退」链路。

**需要观察的现象**：`xla_cluster_util` 提供判定辅助（环检测、ref 变量安全、JIT 等级），但「这个 op 可不可编译」的最终裁决在 `RecursiveCompilabilityChecker` + `MarkForCompilationPass`。

**预期结果**：能口头复述——「op 要进簇需过四关（设备、策略等级、递归可编译、特殊排除）；即便进了簇，惰性模式下 `_XlaCompile` 仍可拒绝编译，由 `_XlaMerge` 回退到 `StatefulPartitionedCall`，全程对下游透明」。本实践为源码阅读型实践，结论可直接从源码读出。

#### 4.4.5 小练习与答案

**练习 1**：一个 op 满足了可编译性，但它单独成一个只有 1 个节点的簇（其余邻居都不可编译），它会被编译吗？为什么？

**参考答案**：通常不会。`CreateClusters` 要求簇的 `effective_cluster_size >= min_cluster_size`，太小的簇不值得为它付出编译开销，于是不会被打上 `_XlaCluster`。除非该 op 被用户显式标了 `_XlaCompile=true`，或含函数式控制流——这两种情况豁免规模门槛（见 [mark_for_compilation_pass.cc:1032-1034](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/jit/mark_for_compilation_pass.cc#L1032-L1034)）。

**练习 2**：假设 XLA 在运行期发现某个簇「编译太不划算」而放弃，下游消费者会感知到吗？

**参考答案**：不会。惰性路径用 `Switch` + `_XlaMerge` 把「XLA 执行」与「TF 原生执行」两路输出合并后再交给下游；无论 `compilation_successful` 是真是假，下游拿到的都是 `_XlaMerge` 的同一个输出张量，行为对消费者完全透明。这正是 auto-clustering「尽力而为」的设计目标。

**练习 3**：`build_xla_ops_pass` 能否脱离 `xla_cluster_util` 独立工作？举一个它依赖聚类工具箱/约定的地方。

**参考答案**：不能完全脱离。`build_xla_ops_pass` 消费的 `_XlaCluster`/`_XlaCompiledKernel`/`_XlaNumConstantArgs` 等属性常量与封装、标记 pass 共享同一套约定；它改写时用到的设备推断（`PickDeviceForXla`、`DeviceInfoCache`）与聚类正确性所依赖的环检测、ref 变量安全分析同属一个紧耦合子系统。三棒（判定、封装、改写）共享同一套工具与属性词汇表。

---

## 5. 综合实践

**任务**：用一段真实的 Python 代码触发自动聚类，并从产物层面验证「聚类发生了、且有不编译的退路」。

**操作步骤**：

1. 写一个以矩阵运算为主的小模型（密集 matmul + 激活），这类 op 是 XLA 偏好的对象：

   ```python
   import tensorflow as tf
   # 示例代码：开启全局 JIT 并跑一段 matmul 密集计算
   tf.config.optimizer.set_jit(True)   # 等价于把 global_jit_level 设为 ON_1

   @tf.function
   def block(x):
       y = tf.matmul(x, x)
       return tf.nn.relu(tf.matmul(y, x))

   x = tf.random.normal([512, 512])
   for _ in range(3):
       block(x)   # 多次调用以触发 tracing + 编译
   ```

2. 打开 XLA 的调试日志，观察聚类与编译事件：

   ```bash
   TF_XLA_FLAGS="--tf_xla_auto_jit=2 --tf_xla_clustering_debug=1" \
   XLA_FLAGS="--xla_dump_to=/tmp/xla_log" \
   python your_script.py
   ```

   - `--tf_xla_auto_jit=2` 的取值语义见 [flags.h:33-46](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/jit/flags.h#L33-L46)（`2` = 对一切可编译的 op 都尝试）。
   - `--tf_xla_clustering_debug=1` 会让相关 pass 调用 `DumpGraphToFile`，例如 [build_xla_ops_pass.cc:618-620](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/jit/build_xla_ops_pass.cc#L618-L620) 的 `DumpGraphToFile("build_xla_ops", ...)`，把改写后的图落盘。

3. **待本地验证**：检查 `/tmp/xla_log` 下是否出现聚类前后的图快照与 HLO 文本（`.hlo` / `*.pb`），并在其中找到形如 `cluster_*` 的簇名、以及 `_XlaCompile`/`_XlaRun`/`_XlaMerge` 节点。

**需要观察的现象**：

- 改写后的图快照里应能看到 `_XlaCompile`、`_XlaRun` 节点；若开了惰性编译（默认），还能看到 `Switch`、`StatefulPartitionedCall`、`_XlaMerge` 这条退路。
- HLO dump 里应能看到与每个簇对应的 XLA 计算（`matmul` + `relu` 被融合）。

**预期结果**：你能在落盘的图里把 4.3、4.4 节描述的节点结构一一对应上，从而在「源码 ↔ 实际产物」之间形成闭环。若环境无法编译 TF 或无 GPU，可退化为纯源码阅读：对照 `build_xla_ops_pass_test.cc` 里的断言理解期望的图结构（待本地验证）。

> 关于「自动聚类开了但某些 op 没进簇」：可以用 `GetXlaAutoClusteringSummary`（[xla_cluster_util.cc:384](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/jit/xla_cluster_util.cc#L384)）打印的「已聚类 / 未聚类 op 直方图」来核对——它会列出每个簇含哪些 op、以及哪些 op 被留在了簇外。

---

## 6. 本讲小结

- 自动聚类是 **三个 pass 接力**：`MarkForCompilationPass`(10，判定+打 `_XlaCluster`) → `EncapsulateSubgraphsPass`(50，封装成函数+打 `_XlaCompiledKernel`) → `BuildXlaOpsPass`(60，改写为 `_XlaCompile`/`_XlaRun`)，注册顺序见 `jit_compilation_pass_registration.cc`。
- `xla_cluster_util` 是聚类的**公共工具箱**：提供簇属性（`_XlaCluster`）读写、处理循环的环检测图（`CreateCycleDetectionGraph`）、全局 JIT 等级判定（`GetGlobalJitLevelForGraph`）、ref 变量安全分析、聚类摘要（`GetXlaAutoClusteringSummary`）。
- `build_xla_ops_pass` 靠 `IsXlaCompiledKernel` 找到目标节点，再分**严格 / 惰性**两条路径改写：严格路径删原节点只留 `_XlaCompile→_XlaRun`；惰性路径用 `Switch(compilation_successful)` + `_XlaMerge` 保留一条 TF 原生执行退路。
- **哪些 op 进簇**由 `MarkForCompilationPass` 把四关：设备有 JIT 编译设备、策略+等级允许、`RecursiveCompilabilityChecker::IsCompilableNode` 通过、不在特殊排除名单；且簇要达到最小规模才被打标记。
- **遇到不支持的 op**有两层回退：编译期该 op 不进簇、照常用 TF kernel；运行期惰性模式下 `_XlaCompile` 可拒绝编译，经 `_XlaMerge` 透明回退到 `StatefulPartitionedCall`。
- 聚类发生在 `POST_REWRITE_FOR_EXEC` 阶段（放置后、分区前），是**尽力而为、对用户透明**的加速机制。

---

## 7. 下一步学习建议

- 下一讲 [u7-l4 TFRT 新一代运行时](u7-l4-tfrt-runtime.md) 将从「编译器」转回「运行时」，讨论 TFRT 如何与既有 DirectSession/执行器共处，可对照本讲的 `POST_REWRITE_FOR_EXEC` pass 链理解「优化产物如何被不同运行时消费」。
- 想深入「为什么有些 op 不能编译」，建议阅读 `tensorflow/compiler/jit/compilability_check_util.cc` 中 `RecursiveCompilabilityChecker` 的实现，以及 `xla_op_registry` 中各类 op 的注册（它决定了 `GetCompilationDevice` 与 `OperationFilter` 的内容）。
- 想理解簇封装成函数的细节，可读 `encapsulate_subgraphs_pass.cc` 中设置 `_XlaCompiledKernel`/`_XlaNumConstantArgs` 的段落（[encapsulate_subgraphs_pass.cc:1297-1300](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/jit/encapsulate_subgraphs_pass.cc#L1297-L1300)），那是 `build_xla_ops_pass` 改写所依赖的属性来源。
- 对「安全分析」感兴趣可延伸阅读 `deadness_analysis.h`（聚类用它保证两路执行谓词一致）与 `resource_operation_safety_analysis.h`（保证 resource 变量并发语义）。
