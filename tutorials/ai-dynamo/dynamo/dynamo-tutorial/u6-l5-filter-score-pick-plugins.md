# u6-l5 filter–score–pick 策略框架与自定义路由插件

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 **filter–score–pick 三段式策略模型**：`WorkerFilter`、`WorkerScorer`、`WorkerPicker` 三个 trait 各自的职责、调用顺序与约束。
2. 解释 `WorkerSelectionPolicyRegistry` 的 **provider → factory → policy 三级生命周期**，以及 `type` / `name` / 角色（aggregated/prefill/decode/encode）三层命名如何对应到一条 YAML 配置。
3. 追踪 `SelectionService` 如何 **直接从 `WorkerSelectionPolicyRegistry` 构造**，并理解 ext-proc（Envoy 扩展进程）与 Python 前端为什么复用同一个注册表。
4. 照着 `examples/router/custom-policy-example/simple-filter-score-pick` 写出一个自己的策略（例如「优先选在途请求最少的 worker」），编译进 catalog 插件，并用 mocker 拓扑验证路由结果确实变了。

## 2. 前置知识

本讲是「KV 感知路由」单元的收官篇，默认你已读过前几讲：

- **u6-l2（RoutingHost）**：你已经知道内嵌路由的决策心脏是 `RoutingHost`，它统一拥有选点、分发与清理的生命周期。本讲会揭示一个关键事实：`RoutingHost` 是**对选择器泛型**的（`RoutingHost<Sel = DefaultWorkerSelector>`），而 `WorkerSelectionPolicy` 正是 `WorkerSelector` 的另一个实现——这就是「内置 KV 路由」与「自定义策略」能插进同一个插槽的原因。
- **u6-l4（基数树索引）**：索引器产出的是每个 worker 的**原始重合度事实**（`OverlapScores`）。本讲会看到：自定义策略读到的 `device_overlap_blocks` 是**未经加权的原始值**，与内置打分器用的「有效重合度」不同——这是刻意设计。
- **Rust 基础**：trait 对象（`Box<dyn Trait>`）、`Arc<dyn Fn(...)>` 类型的工厂闭包、`OnceCell`。不需要精通，遇到时我们会用一句话解释。
- **一个容易混淆的名词**：本讲出现的 `policy_class`（调度策略类，如 latency/agents/batch 队列）与「worker-selection policy（选点策略）」是**两个正交概念**——前者决定请求在队列里的优先级，后者决定请求发给哪个 worker。阅读源码时务必分开。

几个术语先行解释：

| 术语 | 含义 |
|---|---|
| **filter（过滤器）** | 硬性门槛：不满足条件的 worker 直接出局，连打分都不参与 |
| **scorer（打分器）** | 给每个幸存 worker 一个「越低越好」的有限成本；多个 scorer 的成本**相加** |
| **picker（挑选器）** | 从已打分的候选行里挑出最终一行 |
| **catalog（目录）** | 「策略类型名 → 构造函数」的注册表；也是可以被整包替换的构建插槽名 |
| **provider** | 解析一份 YAML 参数并返回 factory 的闭包，进程启动时只调用一次 |

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `lib/kv-router/src/scheduling/selector/policy.rs` | 三段式模型的核心：`WorkerFilter`/`WorkerScorer`/`WorkerPicker` trait、`WorkerSelectionPolicy` 及候选收集算法 |
| `lib/kv-router/src/scheduling/selector/mod.rs` | 宿主侧选点路径 `select_worker_with_policy`：资格检查、候选表构建、picker 结果校验 |
| `lib/kv-router/src/services/selection/policy_registry.rs` | 策略类型注册表：注册、按角色解析、错误分类 |
| `lib/kv-router/src/services/selection/catalog.rs` | `WorkerCatalog`：选点服务的 worker 目录（注意：尽管文件名叫 catalog，它管的是 **worker**，不是策略） |
| `lib/kv-router/src/services/selection/core/mod.rs` | `SelectionCore`：按 `(model, routing_group)` 分区的选点内核，factory 在这里被调用 |
| `lib/kv-router/src/services/selection/service.rs` | `SelectionServiceBuilder`/`SelectionService`：从 registry 构造整个选点服务 |
| `lib/kv-router/src/services/selection/mod.rs` | 上述模块的公开再导出清单 |
| `lib/router-plugins/catalog/src/lib.rs` | 默认空 catalog：自定义镜像要替换的构建插槽 |
| `examples/router/custom-policy-example/` | 完整示例：三个策略 crate + 一个 catalog crate + 一个 EPP 二进制 + README 构建指南 |
| `lib/bindings/python/rust/lib.rs` | Python 扩展如何在 `custom-policy` feature 下安装 catalog |
| `deploy/inference-gateway/ext-proc/src/selector.rs` | ext-proc 如何拿着 registry 构造同一个选点服务 |
| `docs/fern/pages/developer-guide/knowledge-base/modular-components/router/custom-worker-selection.mdx` | 官方自定义选点策略指南（输入组、方法契约、错误处理） |

## 4. 核心概念与源码讲解

### 4.1 三段式策略模型：Filter / Scorer / Picker

#### 4.1.1 概念说明

Dynamo 把「为一条请求选一个 worker」这件事拆成了三个正交阶段：

- **Filter（过滤）**：回答「这个 worker **有没有资格**参与」。例如「设备层 KV 重合度至少 N 块」。被过滤掉的 worker 不会进入打分，若所有 worker 都被过滤，选点直接失败（HTTP 503）。
- **Scorer（打分）**：回答「这个 worker **有多贵**」。返回一个有限的、越低越好的成本；多个 scorer 栈叠时成本相加。
- **Picker（挑选）**：回答「最终**选哪一行**」。最常见是取最小成本，但也可以做亲和（例如工具调用回合选重合度最高的 worker）。

这套拆分解决的问题是：**路由策略的多样性不应要求修改路由器本身**。负载感知、KV 感知、会话亲和、污点偏好……都可以表达为「过滤条件 + 成本函数 + 挑选规则」的组合。Dynamo 保留了对进程更致命的部分——发现、资格校验、排队、预约、记账、指标——而把「纯函数式的决策核心」开放给插件作者。README 把这个边界总结得很直白：**「Dynamo 拥有发现、资格、排队、校验、预约、记账与指标；策略只看到有资格的 worker，并返回一行候选。」**

一个关键纪律是「**声明式输入**」：filter/scorer/picker 想读哪类 worker 信号，必须先在 `required_worker_inputs()` 里声明；没声明的信号宿主根本不会物化（省掉热路径开销），读到的会是 `None`。这防止策略作者偷偷依赖未契约化的数据。

#### 4.1.2 核心流程

一次自定义选点的执行过程（宿主 = 调度器队列 actor）：

```text
请求到达调度器
  │
  ├─ 宿主先做资格（eligibility）检查：pin/allowlist/污点/过载 ── 策略无权也无需做
  │
  ├─ 对每个有资格的 worker (w)：
  │     ① 依声明物化候选行（CACHE / LOAD / PREFERRED_TAINT …）
  │     ② 依次调用每个 filter.keep(w) ─── 任一返回 false ⇒ w 出局
  │     ③ 对幸存者依次调用每个 scorer.score(w) 并累加成本
  │
  ├─ picker.pick(candidates) 返回一个行号
  │     行号越界 ⇒ InvalidPickerRow 错误（宿主校验，不 panic）
  │
  └─ 宿主构造 WorkerSelectionResult（所需块数、有效重合度、缓存 token 数…）
```

成本累加的数学表达：设候选集合为 \( W \)（过滤后），scorer 序列为 \( s_1,\dots,s_k \)，则

\[ \text{cost}(w) = \sum_{i=1}^{k} s_i(w), \qquad w \in W \]

且要求每一项与部分和都是**有限值**（`is_finite`），否则报 `NonFiniteCost`。最低成本挑选即 \( \arg\min_{w \in W} \text{cost}(w) \)；但 picker 完全可以不取最小值——比如按会话触发器改用最大设备重合度。

两个必须遵守的约定（来自模块内文档与示例 AGENTS.md）：

1. **候选行顺序未指定**：picker 不能假设第 0 行是某个特定 worker，必须看候选数据本身。
2. **filter/scorer/picker 必须无副作用且不阻塞**：不做容量预约、不改队列、不做 I/O、不 panic；有状态策略的状态存活在 policy 对象里，由一个调度器队列 actor 串行调用。

#### 4.1.3 源码精读

先看三个 trait 的定义。打分器：

[lib/kv-router/src/scheduling/selector/policy.rs:108-121](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/kv-router/src/scheduling/selector/policy.rs#L108-L121)

`WorkerScorer` 只有一个 `score` 方法，返回「一行合格候选的有限、越低越好的成本贡献」；默认 `required_worker_inputs` 返回 `NONE`（不要任何可选信号）。

过滤器：

[lib/kv-router/src/scheduling/selector/policy.rs:122-138](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/kv-router/src/scheduling/selector/policy.rs#L122-L138)

`WorkerFilter::keep` 返回 `true` 表示把该 worker 留在候选集中。注意文档注释明确说：**filter 按声明顺序对每个候选执行，但不同候选之间、以及 filter 与 scorer 之间的回调交错顺序未指定**——实现不能依赖它们被交错调用。

挑选器：

[lib/kv-router/src/scheduling/selector/policy.rs:139-154](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/kv-router/src/scheduling/selector/policy.rs#L139-L154)

`WorkerPicker::pick` 返回候选表中的一个**行下标**；注释再次强调行序未指定。

可选输入的分组由位标志 `WorkerInputs` 表达：

[lib/kv-router/src/scheduling/selector/policy.rs:47-71](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/kv-router/src/scheduling/selector/policy.rs#L47-L71)

四个可请求的组：`CACHE`（KV 重合度）、`LOAD`（活跃负载）、`PREFERRED_TAINT`（污点偏好系数）、`OCCUPANCY`（宿主持有的在途请求计数）。用 `|` 组合，用 `contains` 检查。

构造一个自定义策略的入口是 `WorkerSelectionPolicy::new_with_filters`：

[lib/kv-router/src/scheduling/selector/policy.rs:380-413](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/kv-router/src/scheduling/selector/policy.rs#L380-L413)

这段代码在构造时就**折叠出三份输入清单**：`picker_inputs`（picker 声明的）、`filter_inputs`（所有 filter 声明的并集）、`scorer_picker_inputs`（scorer 与 picker 声明的并集）。这个折叠在 4.1.2 流程里对应一个精细优化：**filter 阶段只物化 filter 声明的输入，通过 filter 后才物化 scorer/picker 需要的额外输入**——被拒之门外的 worker 不会付出信号物化成本。

成本累加与有限性检查在 `push_scored_candidate`：

[lib/kv-router/src/scheduling/selector/policy.rs:429-464](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/kv-router/src/scheduling/selector/policy.rs#L429-L464)

每个 scorer 贡献一项，任何一项非有限立即报 `NonFiniteCost`（带上 scorer 下标与行号，方便定位是哪个 scorer 返回了 NaN/∞）。

候选收集的总控是 `collect_custom_candidates`：

[lib/kv-router/src/scheduling/selector/policy.rs:520-574](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/kv-router/src/scheduling/selector/policy.rs#L520-L574)

对每个有资格的 worker：先构造只含 filter 输入的 `filter_candidate`，跑完所有 `filter.keep`；全部通过后再构造补充输入的 `additional` 行，合并成完整候选并交给 scorer。注意函数返回的是「**是否存在有资格 worker**」而非「是否有候选」——这两个信息在宿主里用来区分「全被过滤」（`AllEligibleWorkersFiltered`）和「根本没有可用 worker」（`NoEndpoints`）。

宿主侧的完整路径在：

[lib/kv-router/src/scheduling/selector/mod.rs:383-428](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/kv-router/src/scheduling/selector/mod.rs#L383-L428)

这是 `select_worker_with_policy` 的 `Custom` 分支：收集候选 → 构建 `WorkerInputView`（按 picker 声明附带 cache/load 列）→ 调 `picker.pick` → 校验行号合法 → 返回 `(worker, cost)`。行号越界会得到 `InvalidPickerRow`，而不是 panic。

还有两处「原始事实 vs 内置公式」的分界值得注意。候选行的物化逻辑：

[lib/kv-router/src/scheduling/selector/mod.rs:165-226](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/kv-router/src/scheduling/selector/mod.rs#L165-L226)

自定义策略拿到的 `device_overlap_blocks` 走 `select_device_overlap = |_, raw| raw`，即**上报的原始值**；而 u6-l2 里内置 KV 打分用的「有效重合度」（带层级加权、随负载衰减）是另一条路径。配套测试 `custom_policy_does_not_receive_effective_overlap_as_device_overlap`（[policy.rs:876-916](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/kv-router/src/scheduling/selector/policy.rs#L876-L916)）固定了这一契约：**对外只暴露原始事实，不暴露依赖内置公式的中间量**。

最后，`WorkerSelectionPolicy` 实现了通用的 `WorkerSelector` trait：

[lib/kv-router/src/scheduling/selector/policy.rs:581-616](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/kv-router/src/scheduling/selector/policy.rs#L581-L616)

而 u6-l2 学过的 `RoutingHost` 正是对这个 trait 泛型的：[lib/llm/src/kv_router/routing_host.rs:188-190](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/kv_router/routing_host.rs#L188-L190) 定义 `pub struct RoutingHost<Sel = DefaultWorkerSelector>`。所以同一个 `WorkerSelectionPolicy` 对象既能被内嵌 `RoutingHost` 用（见 4.5.3 的 Python 绑定别名），也能被本讲的独立选点服务用。

#### 4.1.4 代码实践：用内置测试验证输入契约

policy.rs 自带一组「契约测试」，是最好的学习材料。

1. **实践目标**：通过运行内置测试，验证「声明了才物化」「行序不可依赖」两条契约，并读懂一个最小自定义 picker 的写法。
2. **操作步骤**：
   ```bash
   # 在仓库根目录
   cargo test -p dynamo-kv-router --lib scheduling::selector::policy
   ```
   重点阅读 `custom_picker_receives_requested_cache_inputs`（[policy.rs:679-732](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/kv-router/src/scheduling/selector/policy.rs#L679-L732)）：测试内定义了一个 `HighestOverlapPicker`，声明 `WorkerInputs::CACHE`，在 `pick` 里用 `input.cache()` 取按行对齐的缓存输入，选 `device_overlap_blocks` 最大的行。整个过程不依赖行序。
3. **需要观察的现象**：测试全部通过；若给 `HighestOverlapPicker` 删掉 `required_worker_inputs` 的重写，`input.cache()` 会变成 `None` 而 `expect` 失败——直观感受「不声明就拿不到」。
4. **预期结果**：`custom_picker_receives_requested_cache_inputs`、`policy_requirements_union_all_components`、`rejected_filters_do_not_materialize_scorer_inputs` 等测试绿。（具体输出以本地为准，待本地验证。）

#### 4.1.5 小练习与答案

**练习 1**：如果一个 scorer 的某次 `score` 返回 `f64::NAN`，会发生什么？请求会被路由到随机 worker 吗？

**答案**：不会。`push_scored_candidate`（policy.rs:441-450）在累加后检查 `contribution.is_finite() || cost.is_finite()`，任一不满足立即返回 `WorkerSelectionPolicyError::NonFiniteCost`，带上 scorer 下标与行号，整个选点以错误收场；请求不会被静默路由。设计意图是让策略 bug 在启动后第一条请求就暴露，而不是污染路由分布。

**练习 2**：为什么 `WorkerPicker::pick` 返回「行号」而不是直接返回 `WorkerWithDpRank`？

**答案**：行号是对候选表的间接引用，宿主保留了对最终数据的校验权——行号越界由宿主以 `InvalidPickerRow` 拒绝（mod.rs:419-425），picker 无法伪造一个不存在于候选表中的 worker，也无法绕过宿主在候选表里维护的成本与元数据（如 `preferred_taint_multiplier`）。同时行号天然与按声明物化的 cache/load 输入列对齐，picker 需要这些列时才能拿到索引一致的视图。

**练习 3**：filter 阶段与 scorer 阶段请求的输入组不同，Dynamo 为什么要分两次物化？

**答案**：性能。`collect_custom_candidates` 先用 `filter_inputs` 构造候选行跑过滤，被拒的 worker 完全不付出 scorer/picker 所需输入（如 LOAD 投影、偏好系数）的物化成本；只有幸存者才用 `additional_inputs = scorer_picker_inputs.without(filter_inputs)` 构造补充行（policy.rs:520-560）。测试 `rejected_filters_do_not_materialize_scorer_inputs` 断言被拒候选上 `cache()/load()` 均为 `None`。

### 4.2 selection catalog：WorkerCatalog（选点服务的 worker 世界）

#### 4.2.1 概念说明

先澄清一个**名字陷阱**：`services/selection/catalog.rs` 里的 `WorkerCatalog` 管理的不是策略，而是 **worker 的注册目录**——「当前有哪些 worker、它们是否可调度、属于哪个模型与路由组」。策略目录是 4.3 的 `policy_registry.rs`。两者一个面向**被路由的对象**，一个面向**做路由的算法**。

`WorkerCatalog` 要解决的问题是：独立选点服务不依赖 Dynamo 的发现面（etcd），worker 的上下线由**外部控制面**通过 HTTP API 显式注入（upsert/patch/delete）。目录需要回答四类问题：

1. 某 worker 现在处于什么**生命周期状态**（`Schedulable` / `Draining` / `Unschedulable` / `Incomplete`）？
2. 某个 `(model_name, routing_group)` 分区下有哪些**可调度**的 worker 及其调度配置？
3. 选中的 worker 的 **endpoint** 是什么（选点结果的最终交付物）？
4. 服务整体是否 **ready**（至少有一个可调度 worker）？

#### 4.2.2 核心流程

```text
外部控制面（ext-proc 的 reconciler / HTTP 客户端）
        │ upsert_worker / patch_worker / delete_worker
        ▼
WorkerCatalog（RwLock<HashMap<WorkerId, WorkerCatalogRecord>>）
        │ 记录生命周期：Incomplete ⇄ Schedulable ⇄ Draining → Unschedulable
        ▼
SelectionCore::reconcile_worker
        │ 缺元数据 ⇒ Incomplete（带原因列表）
        │ 通过 ⇒ 注册 KV 事件监听器 + ensure_entry + 置 Schedulable
        ▼
publish_scheduler_config：把该分区的 {WorkerId → SelectionWorkerConfig}
        经 watch channel 推给 LocalScheduler（workers_tx → workers_rx）
        ▼
调度器选点时只看 Schedulable 集合；选完经 schedulable_endpoint 回查
```

`RoutingPartitionId`（模型名 + 路由组）是贯穿始终的分区键——worker 目录、选点入口（`SelectionEntry`）、KV 索引器都按它切分。

#### 4.2.3 源码精读

目录本体只是一张带读写锁的表：

[lib/kv-router/src/services/selection/catalog.rs:16-31](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/kv-router/src/services/selection/catalog.rs#L16-L31)

`upsert` 返回 `(旧记录, 新记录)` 二元组——旧记录供上层 reconcile 判断「这个 worker 之前是否可调度、是否需要清理旧注册」。

按分区查可调度 worker 是选点的前提：

[lib/kv-router/src/services/selection/catalog.rs:102-120](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/kv-router/src/services/selection/catalog.rs#L102-L120)

`scheduler_configs_for_key` 过滤条件是三连：生命周期为 `Schedulable`、模型名匹配、路由组匹配；产出 `HashMap<WorkerId, SelectionWorkerConfig>`，正是推给调度器的那张表。

选点结果的 endpoint 回查也走目录：

[lib/kv-router/src/services/selection/catalog.rs:130-144](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/kv-router/src/services/selection/catalog.rs#L130-L144)

注意这个方法的防御性：**即便策略选出了某个 worker，返回 endpoint 前还要重新验证它此刻仍是 Schedulable 且属于目标分区**——选点与回查之间存在时间窗，worker 可能刚被置为 Draining。这是「策略无权决定终点，目录说了算」的又一体现。

`has_schedulable_for_key` 与 `schedulable_count`（[catalog.rs:94-100](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/kv-router/src/services/selection/catalog.rs#L94-L100)、[catalog.rs:122-128](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/kv-router/src/services/selection/catalog.rs#L122-L128)）分别支撑「分区内有没有人」与「服务整体 ready」两个判断。

目录之上的 reconcile 编排（状态迁移的判断分支）在：

[lib/kv-router/src/services/selection/core/mod.rs:361-414](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/kv-router/src/services/selection/core/mod.rs#L361-L414)

`reconcile_worker` 的顺序是：若旧记录可调度 ⇒ 先置 `Draining`、发布配置、清理索引注册（保证不留半死状态）；再检查缺失元数据（缺 ⇒ `Incomplete` + 原因列表）；然后 `ensure_entry` 建分区选点入口、（启用 KV 事件时）注册索引监听；最后置 `Schedulable` 并发布。任何一步失败都会把 worker 标成带原因的 `Incomplete`。

#### 4.2.4 代码实践：从测试观察目录行为

1. **实践目标**：通过 core 模块的测试，观察 worker 注册后目录与调度器配置发布的联动。
2. **操作步骤**：
   ```bash
   cargo test -p dynamo-kv-router --lib services::selection::core
   ```
   重点阅读 `upsert_moves_global_worker_id_between_routing_groups`（[core/mod.rs:1446-1498](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/kv-router/src/services/selection/core/mod.rs#L1446-L1498)）：同一个 worker_id 先后注册到 group-a 与 group-b，断言旧组目录清空、新组就位，且对旧组的 `select` 返回 `NotReady`、对新组成功。
3. **需要观察的现象**：测试通过；从断言可以读出「同一 worker 全局唯一，换组等于整体迁移」的目录语义。
4. **预期结果**：该测试与其余 selection core 测试全绿。（待本地验证。）

#### 4.2.5 小练习与答案

**练习 1**：为什么 `patch` 成功后要把生命周期强制置回 `Incomplete` 并清空 `not_schedulable_reasons`？

**答案**：见 [catalog.rs:33-49](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/kv-router/src/services/selection/catalog.rs#L33-L49)。patch 修改了 worker 的元数据（如 endpoint、dp rank），旧的可调度状态不再可信，必须重新走一遍 `reconcile_worker` 的完整校验（元数据齐备性、入口、监听器）才能回到 `Schedulable`。清空原因列表则是为了让 reconcile 重新计算，而不是保留上次的陈旧诊断。

**练习 2**：`schedulable_endpoint` 为什么不直接返回 `record.endpoint`，而要先做三重条件检查？

**答案**：因为「策略选中的瞬间」与「返回 endpoint 的瞬间」之间 worker 状态可能变化（被 patch、被删除、开始 Drain）。三重检查（Schedulable + 模型匹配 + 路由组匹配）保证不会把请求发往一个已不属于该分区的 worker；检查失败时上层返回 `Internal("selected worker ... is no longer schedulable")`（core/mod.rs:790-798），让调用方重试而非拿到坏地址。

### 4.3 policy_registry：策略类型的注册与解析

#### 4.3.1 概念说明

有了策略对象，还需要一套机制回答：**「一个 YAML 里写的策略名，如何变成进程里活着的策略对象？」** 这就是 `WorkerSelectionPolicyRegistry` 的职责。它是一个**只在启动期使用**的注册表，运行期不再变化——策略是静态链接进二进制的 Rust 代码，不是动态加载的 .so。

三层命名必须分清（这是本模块最容易搞混的地方）：

| 层 | 例 | 谁定义 | 作用 |
|---|---|---|---|
| **policy type（类型）** | `simple-filter-score-pick` | 策略 crate 的 `register()` 函数 | 选中一个编译期链接的 provider |
| **instance name（实例名）** | `filter-score-pick-cache-affinity` | YAML 配置 | 同一类型带不同参数的多份实例 |
| **角色选择** | `worker_selection.prefill: ...` | YAML 或环境变量 | 每种 worker 池（aggregated/prefill/decode/encode）各选一个实例 |

与之配套的是**三级生命周期**：

1. **provider**：`Fn(&WorkerSelectionPolicyParameters) -> Result<Factory, Error>`，启动时对选中的实例各调用一次，解析并校验 YAML 参数；
2. **factory**：`Fn(&KvRouterConfig, WorkerType, RoutingPartitionRef) -> WorkerSelectionPolicy`，对每个 `(model, routing_group)` 分区调用一次；
3. **policy**：分区私有的 filter/scorer/picker 集合，由该分区的调度器串行调用。

「default」是被保留的类型名/实例名，永远映射到 Dynamo 内置选点器——默认 catalog 刻意为空也是为了保住这条语义。

#### 4.3.2 核心流程

```text
启动：
  镜像构建时：catalog::register(&mut registry)          ← 把每个类型塞进 providers
  进程启动时：SelectionServiceBuilder::build()
        │ registry.resolve_for_worker_type(config, worker_type)
        │   ├─ 读 YAML worker_selection.{aggregated,prefill,decode,encode}
        │   │   （可被 DYN_ROUTER_* 环境变量覆盖）
        │   ├─ 找到 instance ⇒ 查 providers[type] ⇒ 调 provider(parameters)
        │   │   参数非法 ⇒ Provider 错误，启动失败
        │   └─ 未配置或选 "default" ⇒ None（回落内置）
        ▼
  Option<Factory> 存入 SelectionCore
运行：
  首个 worker 上线 ⇒ ensure_entry ⇒ factory(config, worker_type, partition)
        ⇒ 每个分区各得一份独立的 policy 状态
```

环境变量覆盖关系（policy_registry.rs 顶部再导出）：`DYN_ROUTER_WORKER_SELECTION_POLICY` 覆盖所有角色；`DYN_ROUTER_PREFILL_POLICY` / `DYN_ROUTER_DECODE_POLICY` 只覆盖对应阶段。

#### 4.3.3 源码精读

provider 与参数类型：

[lib/kv-router/src/services/selection/policy_registry.rs:21-46](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/kv-router/src/services/selection/policy_registry.rs#L21-L46)

`WorkerSelectionPolicyParameters` 包装一个 `serde_yaml::Value`，只暴露 `deserialize::<T>()`——**参数的具体形状由策略 crate 自己定义**（用一个 `#[derive(Deserialize)]` 的结构体），注册表不掺和。`deny_unknown_fields` 是示例中的推荐做法，能在 YAML 写错字段名时立刻报错而不是静默忽略。

注册表本体与守门规则：

[lib/kv-router/src/services/selection/policy_registry.rs:99-117](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/kv-router/src/services/selection/policy_registry.rs#L99-L117)

三条拒绝规则：空名、保留名 `default`、重复注册。重复注册尤其重要——如果两个 catalog 都注册了 `foo`，YAML 里的 `type: foo` 就有了二义性，宁可启动失败。

按角色解析（单池宿主用这个，避免为不构造的池加载策略）：

[lib/kv-router/src/services/selection/policy_registry.rs:134-148](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/kv-router/src/services/selection/policy_registry.rs#L134-L148)

最终落到单实例解析：

[lib/kv-router/src/services/selection/policy_registry.rs:213-248](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/kv-router/src/services/selection/policy_registry.rs#L213-L248)

错误分类非常讲究：实例名不存在报 `UnknownInstance`（附上已配置实例列表）、类型没链接进镜像报 `UnknownType`（附上已链接类型列表）、参数校验失败报 `Provider`。三者都是**启动失败**而非运行期降级——README 明说「未知策略类型、重复注册、非法参数都会停止启动」。

多角色合成的总装在 `resolve_selections`：

[lib/kv-router/src/services/selection/policy_registry.rs:150-190](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/kv-router/src/services/selection/policy_registry.rs#L150-L190)

它为四种角色各解析一份 factory（同名实例缓存复用），再包成一个**按 `WorkerType` 分派**的总 factory：角色配了实例就走该实例，没配就回落 `WorkerSelectionPolicy::default`。配套测试 `resolved_factory_dispatches_role_specific_instances`（[policy_registry.rs:383-431](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/kv-router/src/services/selection/policy_registry.rs#L383-L431)）断言了 prefill 拿阈值 1、decode 拿阈值 2 的分派结果。

#### 4.3.4 代码实践：亲手触发三类启动错误

1. **实践目标**：用测试复现「配置了未链接的策略类型 ⇒ 启动失败」，理解错误信息如何指导排障。
2. **操作步骤**：
   ```bash
   cargo test -p dynamo-kv-router --lib services::selection::service
   ```
   阅读 `configured_custom_policy_requires_linked_policy_type_at_construction`（[service.rs:451-488](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/kv-router/src/services/selection/service.rs#L451-L488)）：测试把一份 `type: acme` 的 YAML 写进临时文件，用空注册表构造 builder，断言 `build()` 报错且错误串包含 `unknown worker-selection policy type "acme"`。
3. **需要观察的现象**：测试通过。再对照 policy_registry.rs 的 `rejects_invalid_and_unknown_names` 测试（[policy_registry.rs:472-535](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/kv-router/src/services/selection/policy_registry.rs#L472-L535)），它覆盖了空名、保留名、重复、未知实例、未知类型、非法参数六种分支。
4. **预期结果**：全部绿；错误信息里都能看到「可用列表」提示（如 `linked policy types: alpha`）。（待本地验证。）

#### 4.3.5 小练习与答案

**练习 1**：为什么 `resolve_for_worker_type`（按角色）与 `resolve`（全角色）要分成两个方法？ext-proc 用哪个？

**答案**：单池宿主（如独立 EPP 只构造 `WorkerType::Aggregated` 一个池）用 `resolve_for_worker_type`，可以避免为根本不会构造的池加载策略类型——测试 `role_scoped_resolution_ignores_unlinked_policy_types_for_other_roles`（policy_registry.rs:434-470）演示了「prefill 配了一个未链接类型，但宿主只构造 aggregated 池」时按角色解析成功、全量解析报 `UnknownType` 的差异。ext-proc 的 `Selector::new` 走 `SelectionServiceBuilder`，后者调用的是 `resolve_for_worker_type`（service.rs:101-103）。

**练习 2**：`provider` 与 `factory` 为什么必须是两个闭包，而不是一个直接构造 policy 的函数？

**答案**：它们的调用时机与参数集不同。provider 只在启动时调用一次，只拿到 YAML 参数——所以参数校验（如阈值非负、有限）放这里，一次失败全局失败。factory 在运行期为**每个分区**调用，拿到 `KvRouterConfig`、类型化 `WorkerType` 与分区引用——同一实例可以在不同分区持有独立状态（例如每分区一个计数器），而参数已在校验后捕获进闭包（示例 lib.rs:47 的 `let min_device_overlap_blocks = ...`）。

**练习 3**：配置里 `type: default` 会发生什么？

**答案**：`resolve_selected` 对 `selected == "default"` 直接返回 `Ok(None)`（policy_registry.rs:221-223），宿主收到 `None` 后回落到内置选点器（`resolve_selections` 包装的闭包里对未配置角色调 `WorkerSelectionPolicy::default`，见 policy.rs:184-187 处的 `default` 构造）。同时 `register("default", ...)` 被禁止（`ReservedDefault`），保证「default 永远是内置」不会被劫持。

### 4.4 SelectionService：从 registry 构造选点服务（ext-proc 共用）

#### 4.4.1 概念说明

`services/selection` 是一个 **runtime-free（不依赖 dynamo-runtime）** 的选点服务：模块级 AGENTS 明确禁止它依赖 Dynamo 的请求面/发现面/事件面，协议保持可被「不跑任何 Dynamo 组件的进程」使用。这就是它能同时被三类宿主复用的前提：

1. **ext-proc**（Envoy 扩展进程，GAIE 拓扑，u10-l3 展开）；
2. **Python 前端 / `python -m dynamo.select_service`**（经 PyO3 桥）；
3. 任何静态链接 `dynamo-kv-router`（带 `standalone-selection` feature）的第三方 Rust 进程。

`SelectionServiceBuilder` 把 4.3 的 registry 作为**必需构造参数**——「选点服务直接从 policy registry 构造」正是字面意思：没有旁路，想要服务就得给注册表（哪怕给一个空的 `WorkerSelectionPolicyRegistry::default()`）。

服务内部按 `RoutingPartitionId`（模型 + 路由组）切分出 `SelectionEntry`，每个 entry 自带：KV 索引器、worker 配置的 watch channel、`LocalScheduler`（调度器队列 actor）与 **policy 对象**（由 factory 在此创建）。

还有一个容易误读的地方：`SelectionCore` 里的 `SelectionScheduler` 类型别名（[core/mod.rs:52-57](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/kv-router/src/services/selection/core/mod.rs#L52-L57)）把 `WorkerSelectionPolicy` 作为 `LocalScheduler` 的选择器类型参数**固化**了进去——在这套服务里，三段式 policy 不是调度器的可选插件，而是其类型组成的一部分。

#### 4.4.2 核心流程

以一次 `select_and_reserve`（选点并预约）为例：

```text
客户端 → SelectionService::select_and_reserve(req)
  │
  ├─ RoutingPartitionId::new(model_name, routing_group) 定位 entry
  ├─ ready_entry：目录里该分区必须有 Schedulable worker，否则 NotReady
  ├─ prepare_selection_inputs：归一化 prompt（token→块哈希/序列哈希）、
  │     查索引器得分层重合 TieredMatchDetails、算 OverlapSignals
  ├─ 组装 ScheduleRequest（mode = Tracked{request_id}）
  │     └─ 内含 policy_class（调度类）、session、pin、约束等
  ├─ entry.scheduler.schedule_request(...)
  │     └─ 调度器 actor 内部：admission → WorkerSelectionPolicy 选点 → 容量预约
  ├─ catalog.schedulable_endpoint(best_worker) 回查地址
  └─ 返回 SelectResponse { worker_id, dp_rank, endpoint, overlap,
                            effective_prefill_tokens, ... }
```

注意：**factory 的调用不发生在请求路径上**，而发生在首个 worker 让分区 entry 诞生的 `ensure_entry` 里——每个分区只创建一次 policy。

#### 4.4.3 源码精读

builder 的签名已经说明了 registry 的地位：

[lib/kv-router/src/services/selection/service.rs:58-74](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/kv-router/src/services/selection/service.rs#L58-L74)

`SelectionServiceBuilder::new(kv_router_config, worker_type, worker_selection_policy_registry)` 三要素：路由配置、本宿主构造的 worker 池角色、策略注册表。

`build()` 里 registry 被解析成 factory：

[lib/kv-router/src/services/selection/service.rs:97-130](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/kv-router/src/services/selection/service.rs#L97-L130)

第 101-103 行调 `resolve_for_worker_type`，得到的 `Option<Factory>` 传入 `SelectionCore::new_inner`。顺带一提 `build()` 还做了副本同步（replica sync）的初始化与一个 `StartupGuard`（构建中途失败自动取消 cancel token，防止半初始化服务泄漏后台任务）。

factory 真正被消费的地方在 `ensure_entry`：

[lib/kv-router/src/services/selection/core/mod.rs:483-507](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/kv-router/src/services/selection/core/mod.rs#L483-L507)

第 483-486 行是整个框架的「合龙点」：有 factory 就 `factory(&self.kv_router_config, self.worker_type, key.as_ref())`，没有就用 `WorkerSelectionPolicy::default`。产出的 selector 随后进入 `LocalScheduler::new_with_policy_profile`（第 491-507 行），成为该分区调度器的一部分。同一个 entry 还持有 `workers_tx`（worker 配置的 watch 发送端），4.2 的目录发布与调度器消费在此对接。

ext-proc 一侧的构造（u10-l3 的入口，这里先见一面）：

[deploy/inference-gateway/ext-proc/src/selector.rs:109-147](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/deploy/inference-gateway/ext-proc/src/selector.rs#L109-L147)

`Selector::new_with_kv_router_config` 在第 116 行先调 `warn_for_unserved_worker_selection_policies`（对配置了策略却没有对应 worker 池的角色发出警告，定义在 [service.rs:38-56](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/kv-router/src/services/selection/service.rs#L38-L56)），第 136-147 行用同一个 `SelectionServiceBuilder::new(kv_router_config, WorkerType::Aggregated, policy_registry)` 建服务。**独立 EPP 的池是 Aggregated**（它面对的是「完整请求 worker 池」），这也解释了为什么 `disagg-filter-score-pick` 示例会拒绝 EPP 的 Aggregated 角色。

请求路径上「选点 → 回查 endpoint → 返回」的收尾：

[lib/kv-router/src/services/selection/core/mod.rs:783-837](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/kv-router/src/services/selection/core/mod.rs#L783-L837)

`tokio::select!` 让排队中的选点能被关停信号打断；拿到 best_worker 后经目录回查 endpoint，再计算 `effective_prefill_tokens` 并组装 `SelectResponse`。

#### 4.4.4 代码实践：观察「factory 每分区一次」

1. **实践目标**：从源码与日志两头确认 policy 是按分区创建的，而不是按请求。
2. **操作步骤**：
   - 静态分析：在 [core/mod.rs:434-532](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/kv-router/src/services/selection/core/mod.rs#L434-L532) 的 `ensure_entry` 中找到 `OnceCell` 模式——同一 `RoutingPartitionId` 的 entry 只初始化一次，factory 只会在这第一次被调用；对比 `schedule_selection`（每请求一次）确认两者频率差。
   - 动态观察（可选）：给 4.5 的示例策略 factory 加一行 `tracing::info!("policy created for {worker_type} @ {partition}")`，跑两个不同 `routing_group` 的 mocker worker，数日志条数。
3. **需要观察的现象**：静态上，factory 调用点被 `get_or_try_init` 包住；动态上（若做了日志），每个分区恰好一条「policy created」日志，与请求数无关。
4. **预期结果**：两个 worker 分属两个路由组 ⇒ 两条创建日志；同组加第二个 worker ⇒ 不再新增。（动态部分待本地验证。）

#### 4.4.5 小练习与答案

**练习 1**：为什么 `SelectionService` 不自己去做服务发现，而要靠外部 upsert worker？

**答案**：`services/` 的 AGENTS 规定这些服务不得依赖 `dynamo-runtime`、不得假设 Dynamo 的三个面存在，协议要能被不跑 Dynamo 组件的进程使用。worker 目录的注入权交给宿主：ext-proc 用 Kubernetes `InferencePool`/EndpointSlice 发现后经 reconciler 注入，Python select_service 用 HTTP API 注入。这样选点内核保持纯 Rust、可单测、可嵌入任意网关。

**练习 2**：`warn_for_unserved_worker_selection_policies` 想解决什么坑？

**答案**：用户可能在 YAML 里给 prefill 配了自定义策略，但宿主（例如独立 EPP）只构造 Aggregated 池——这份配置会被静默忽略。该函数（service.rs:38-56）对比「配置了策略的角色集合」与「宿主实际构造的角色集合」，对差集打 warning。静默忽略比报错温和，因为多角色配置文件被单角色宿主复用是合法场景（见 policy_registry.rs:434-470 的测试）。

**练习 3**：`SelectionServiceConfig::service_builder`（service.rs:173-192）与直接 `SelectionServiceBuilder::new` 有什么差别？

**答案**：前者是「配置 → builder」的便捷封装，把 `SelectionServiceConfig`（端口、线程数、索引对等点、副本同步、缓存配置）逐项搬进 builder，调用方只需再提供 `worker_type` 与 registry；后者是裸构造入口，测试与自定义宿主常用。两者最终都走同一个 `build()`。

### 4.5 router-plugins catalog 与自定义镜像：把策略编译进去

#### 4.5.1 概念说明

最后一块拼图：**策略是 Rust 代码，如何进入一个正在运行的 frontend 或 EPP？** 答案是**编译期链接**（没有运行时动态加载）。Dynamo 用一个「catalog 插槽」机制组织它：

- 包名为 `dynamo-worker-selection-policy-catalog` 的 crate 是一个**依赖别名插槽**——Python 扩展 crate 永远以这个名字依赖它；
- 仓库自带的默认 catalog（`lib/router-plugins/catalog`）**刻意为空**，只为保住 `default` 语义；
- 自定义镜像把别名指向自己的 catalog crate，从而把自己的一批策略类型链接进二进制。

配套的 feature 开关是 `custom-policy`：Python 扩展以 `--features custom-policy` 重建后，`import dynamo._core` 时就会执行 catalog 的 `register`。官方示例 `examples/router/custom-policy-example` 是这一流程的完整样板，README 称之为「自定义 Rust worker-selection 策略的构建指南」，其构建链是：

```text
policy crate → catalog crate → router-policy YAML → frontend 或 EPP 二进制
```

#### 4.5.2 核心流程

以「Python 前端 + mocker」为例的完整链路：

```text
① 写策略 crate（filter/scorer/picker + provider + register）
② catalog crate 依赖所有策略 crate，register 里逐个注册
③ 让 Python 扩展依赖你的 catalog（依赖别名指向它）
④ maturin develop --features custom-policy 重建 _core
⑤ import dynamo._core ⇒ 注册表填充（进程级一次）
⑥ YAML 写实例与角色选择（或用 DYN_ROUTER_* 环境变量）
⑦ frontend 启动 ⇒ SelectionServiceBuilder ⇒ resolve ⇒ 每分区一个 policy
⑧ 请求到达 ⇒ 三段式执行 ⇒ 日志中的 logit = 策略成本
```

EPP 路径类似，只是③换成「EPP 的 main 里显式调用 catalog::register 后把 Some(registry) 传给 run()」。

#### 4.5.3 源码精读

先看被替换的空插槽：

[lib/router-plugins/catalog/src/lib.rs:10-18](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/router-plugins/catalog/src/lib.rs#L10-L18)

默认 `register` 是空操作，注释说明了意图：默认 catalog 故意为空，`default` 永远选中 Dynamo 内置选点器。其包名（[Cargo.toml:5](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/router-plugins/catalog/Cargo.toml#L5)）就是依赖别名 `dynamo-worker-selection-policy-catalog`。

再看示例策略的完整三件套。过滤器（设备重合度门槛）：

[examples/router/custom-policy-example/simple-filter-score-pick/src/filter.rs:10-32](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/examples/router/custom-policy-example/simple-filter-score-pick/src/filter.rs#L10-L32)

声明 `WorkerInputs::CACHE`，`keep` 读 `candidate.cache()` 的 `device_overlap_blocks` 与参数阈值比较。注意它对「输入未物化」的处理是返回错误而非解包 panic。

打分器（在途请求数即成本）：

[examples/router/custom-policy-example/simple-filter-score-pick/src/scorer.rs:10-30](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/examples/router/custom-policy-example/simple-filter-score-pick/src/scorer.rs#L10-L30)

声明 `WorkerInputs::LOAD`，成本就是 `active_requests`——一个整数值的「越少越好」。

挑选器（请求感知：工具回合走缓存亲和，其余走最低成本）：

[examples/router/custom-policy-example/simple-filter-score-pick/src/picker.rs:11-50](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/examples/router/custom-policy-example/simple-filter-score-pick/src/picker.rs#L11-L50)

它展示了 picker 读**请求级上下文**的能力：`context.session_context().input_trigger()` 为 `ToolResult` 时改选 `device_overlap_blocks` 最大的行（工具结果通常延续同会话前缀，缓存亲和收益高）；否则选 `cost` 最小的行。两种分支都不依赖行序。

组装与注册：

[examples/router/custom-policy-example/simple-filter-score-pick/src/lib.rs:25-70](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/examples/router/custom-policy-example/simple-filter-score-pick/src/lib.rs#L25-L70)

五个要点：① `Parameters` 带 `#[serde(deny_unknown_fields)]`；② 参数校验在 provider 内、factory 创建前（`validate_min_device_overlap_blocks` 连 NaN 都挡掉）；③ 校验后的值被 `move` 进 factory 闭包；④ factory 内用 `WorkerSelectionPolicy::new_with_filters` 把三段组装起来，`worker_type.as_str()` 只在构造边界转字符串；⑤ `register` 暴露稳定的类型名。同目录的 `simple-stacked-score-pick/src/lib.rs:24-42`（[链接](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/examples/router/custom-policy-example/simple-stacked-score-pick/src/lib.rs#L24-L42)）演示无 filter、双 scorer 栈叠的变体。

示例 catalog 聚合三个策略并带防重复测试：

[examples/router/custom-policy-example/catalog/src/lib.rs:10-41](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/examples/router/custom-policy-example/catalog/src/lib.rs#L10-L41)

EPP 二进制的链接点只有三行：

[examples/router/custom-policy-example/epp/src/main.rs:7-12](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/examples/router/custom-policy-example/epp/src/main.rs#L7-L12)

`register` 后把 `Some(registry)` 传给 `run`。

Python 侧的对应机制在绑定层：

[lib/bindings/python/rust/lib.rs:331-350](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/bindings/python/rust/lib.rs#L331-L350)

`custom-policy` feature 下，`_core` 这个 pymodule 换成 `register_core_with_custom_worker_selection_policy`：先建空注册表、调 `dynamo_worker_selection_policy_catalog::register`（即别名插槽）、存进进程级 `OnceCell`，再走常规模块注册。也就是说**注册发生在 `import dynamo._core` 的瞬间**。而内嵌前端取 factory 的入口是：

[lib/bindings/python/rust/lib.rs:292-313](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/bindings/python/rust/lib.rs#L292-L313)

无 `custom-policy` feature 的构建里，如果 YAML 配了实例会直接报错提示「rebuild with --features custom-policy」——配置不会静默失效。此外 `RoutingHost` 在该 feature 下被特化为 `RoutingHost<WorkerSelectionPolicy>`（[lib/bindings/python/rust/llm/kv.rs:51-58](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/bindings/python/rust/llm/kv.rs#L51-L58)），把 4.1 的「同一插槽」结论落到 u6-l2 学过的内嵌路由上。

策略 crate 自身的依赖声明（示例）：

[examples/router/custom-policy-example/simple-filter-score-pick/Cargo.toml:14-16](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/examples/router/custom-policy-example/simple-filter-score-pick/Cargo.toml#L14-L16)

对 `dynamo-kv-router` 开启 `standalone-selection` feature——这是把 selection 服务（及本讲的注册表）暴露出来所需的特性开关。

#### 4.5.4 代码实践：跑通示例的构建与测试

1. **实践目标**：不改任何策略代码，先把示例的「测试 + 构建」链路跑通，确认环境可用。
2. **操作步骤**（来自示例 README「Build and Test」节，[README.md:189-202](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/examples/router/custom-policy-example/README.md#L189-L202)）：
   ```bash
   # 仓库根目录
   cargo test \
     -p dynamo-custom-policy-example-simple-filter-score-pick \
     -p dynamo-custom-policy-example-disagg-filter-score-pick \
     -p dynamo-custom-policy-example-simple-stacked-score-pick \
     -p dynamo-custom-policy-example-catalog
   cargo build -p dynamo-custom-policy-example-epp
   ```
3. **需要观察的现象**：`simple-filter-score-pick` 的参数校验测试、`catalog` 的防重复注册测试（`registers_all_policies`）通过；EPP 二进制编译成功。
4. **预期结果**：全部通过即说明工具链与 workspace 配置就绪，为第 5 节的综合实践铺路。（待本地验证。）

#### 4.5.5 小练习与答案

**练习 1**：为什么默认 catalog 刻意留空，而不是把三个示例策略放进去？

**答案**：catalog 类型名是 YAML 契约的一部分。若默认镜像里就带 `simple-filter-score-pick` 等类型，用户的 YAML 会在「官方镜像」与「自定义镜像」之间产生行为漂移；空 catalog + `ReservedDefault` 保护保证 `default` 在任何镜像里都指向同一内置选点器（[lib/router-plugins/catalog/src/lib.rs:11-13](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/router-plugins/catalog/src/lib.rs#L11-L13) 的注释原话是「intentionally empty so `default` always selects Dynamo's built-in worker selector」）。

**练习 2**：示例 AGENTS.md 要求「每个 filter/scorer/picker 读取的信号都要用 `required_worker_inputs` 声明」。如果漏声明会发生什么？

**答案**：编译期不会有任何报错（trait 方法有默认实现），运行期读到的是 `None`——示例代码里表现为 `WorkerSelectionPolicyError::failed("cache input unavailable")` 之类的错误，测试 `custom_picker_receives_requested_cache_inputs` 的 `expect("requested cache inputs")` 也会 panic。更隐蔽的是宿主的优化（4.1.3 的两段物化）会假定「未声明 = 不需要」，漏声明与偷偷读的组合不可能静默成功，这正是该纪律的执行机制。

**练习 3**：`disagg-filter-score-pick` 为什么要在 factory 里按 `WorkerType` 分支，并且拒绝 `Aggregated`？

**答案**：分离式拓扑里 prefill 与 decode 池的选点逻辑通常不同（一个看 prefill 负载、一个看 decode 足迹与 KV 迁移成本），所以 factory 收到类型化 `WorkerType` 后分别构造两套三段式组合（README「disagg-filter-score-pick」一节，[README.md:35-37](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/examples/router/custom-policy-example/README.md#L35-L37)）。遇到无法区分阶段的 `Aggregated` 池时显式报错而不是静默套用 prefill 逻辑——「不支持的角色停下，而不是悄悄用错的政策」。

## 5. 综合实践

**任务：写一个 `least-active-requests` 策略并端到端验证路由确实变了。**

目标策略行为：不看 KV 缓存、不做硬过滤，永远把请求发给「当前在途请求最少」的 worker（把大纲建议的「事件数最少」落实为 `active_requests`，这是 LOAD 输入组里最直接的在途负载信号）。它与示例的差异点有二：**没有 filter**、**picker 不做工具回合的缓存亲和分支**——因此同样的流量下选择结果与 `logit` 数值都应不同。

**第一步：创建策略 crate。** 在 `examples/router/custom-policy-example/` 下新建 `least-active-requests/`（或按 README「Create the Policy Crate」一节在 checkout 外建，[README.md:41-59](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/examples/router/custom-policy-example/README.md#L41-L59)；放进 workspace 时照示例的 Cargo.toml 写，含 `features = ["standalone-selection"]`）。三个文件（示例代码，非仓库原有）：

```rust
// src/scorer.rs —— 唯一的打分器：在途请求数即成本
use dynamo_kv_router::{
    WorkerCandidate, WorkerInputs, WorkerScorer, WorkerSelectionContext,
    WorkerSelectionPolicyError,
};

pub(crate) struct ActiveRequestsScorer;

impl WorkerScorer for ActiveRequestsScorer {
    fn required_worker_inputs(&self) -> WorkerInputs {
        WorkerInputs::LOAD                      // 声明后才拿得到 load 输入
    }
    fn score(
        &mut self,
        _context: &WorkerSelectionContext<'_>,
        candidate: &WorkerCandidate,
    ) -> Result<f64, WorkerSelectionPolicyError> {
        let load = candidate
            .load()
            .ok_or_else(|| WorkerSelectionPolicyError::failed("load input unavailable"))?;
        Ok(load.active_requests() as f64)
    }
}
```

```rust
// src/picker.rs —— 最低成本挑选，无任何缓存亲和分支
use dynamo_kv_router::{
    WorkerInputView, WorkerInputs, WorkerPicker, WorkerSelectionContext,
    WorkerSelectionPolicyError,
};

pub(crate) struct LowestCostPicker;

impl WorkerPicker for LowestCostPicker {
    fn required_worker_inputs(&self) -> WorkerInputs {
        WorkerInputs::NONE
    }
    fn pick(
        &mut self,
        _context: &WorkerSelectionContext<'_>,
        input: WorkerInputView<'_>,
    ) -> Result<usize, WorkerSelectionPolicyError> {
        input
            .candidates()
            .iter()
            .enumerate()
            .min_by(|(_, l), (_, r)| l.cost().total_cmp(&r.cost()))
            .map(|(row, _)| row)
            .ok_or_else(|| WorkerSelectionPolicyError::failed("no eligible worker"))
    }
}
```

```rust
// src/lib.rs —— 参数为空、无 filter、单 scorer（示例代码）
use std::sync::Arc;
use dynamo_kv_router::services::selection::{
    WorkerSelectionPolicyFactory, WorkerSelectionPolicyParameters,
    WorkerSelectionPolicyProviderError, WorkerSelectionPolicyRegistry,
    WorkerSelectionPolicyRegistryError,
};
use dynamo_kv_router::{KvRouterConfig, WorkerSelectionPolicy};

#[derive(serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct Parameters {}

fn provider(
    parameters: &WorkerSelectionPolicyParameters,
) -> Result<WorkerSelectionPolicyFactory, WorkerSelectionPolicyProviderError> {
    let _: Parameters = parameters.deserialize()?;
    Ok(Arc::new(|config: &KvRouterConfig, worker_type, _partition| {
        WorkerSelectionPolicy::new(
            config.clone(),
            worker_type.as_str(),
            vec![Box::new(ActiveRequestsScorer)],
            Box::new(LowestCostPicker),
        )
    }))
}

pub fn register(
    registry: &mut WorkerSelectionPolicyRegistry,
) -> Result<(), WorkerSelectionPolicyRegistryError> {
    registry.register("least-active-requests", Arc::new(provider))
}
```

再加一个注册测试（照 catalog 的 `registers_all_policies` 模式：注册一次成功、再注册报 `Duplicate`）。

**第二步：进 catalog。** 在示例 `catalog/src/lib.rs` 的 `register` 里加一行 `least_active_requests_policy::register(registry)?;` 并补依赖，然后：

```bash
cargo test -p dynamo-custom-policy-example-catalog
```

**第三步：写 YAML。** 照 README「Configure a Policy Instance」（[README.md:146-187](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/examples/router/custom-policy-example/README.md#L146-L187)）在 `/tmp/worker-selection.yaml` 增加：

```yaml
worker_selection:
  aggregated: least-active-requests
  instances:
    - name: least-active-requests
      type: least-active-requests
      parameters: {}
```

**第四步（纯 Rust 验证层）**：`cargo test` 你的策略 crate 与 catalog，确认注册与参数解析正确。这一层不需要 Python、不需要 GPU。

**第五步（端到端验证层，可选）**：按 README「Run With the Python Frontend」（[README.md:204-232](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/examples/router/custom-policy-example/README.md#L204-L232)）把依赖别名指向示例 catalog、`maturin develop --uv --features custom-policy` 重建扩展；再按「Try the Policies End to End With Mocker」（[README.md:268-311](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/examples/router/custom-policy-example/README.md#L268-L311)）起两个 aggregated mocker worker，用 curl 发几条请求。

**需要观察的现象与判据**：

1. frontend 日志里 `Selected worker` 行的 `logit` 字段等于被选 worker 的 `active_requests`（整数成本），而不是 `simple-stacked-score-pick` 那种「在途请求 + 未缓存块」的复合值——数值形态本身就能证明换策略生效了。
2. 并发压几条请求（mocker 的 `--delay` 会拉开在途时间窗），观察选中 worker 在两个 mocker 实例间随在途计数摆动，而不是按 KV 重合度聚集。
3. 故意把 YAML 里 `type` 写成 `least-active-request`（拼错）：frontend 启动应失败并打印 `unknown worker-selection policy type ... linked policy types: ...`——验证 4.3 的启动期防线。

**预期结果**：路由分布从「重合度优先」变为「在途最少优先」，`logit` 为整数在途计数；拼错类型名时启动失败。（本实践的第 4.5 步需要 maturin 与 Python 环境，具体输出待本地验证；第 1-4 步只需 cargo。）

## 6. 本讲小结

- **三段式模型**：`WorkerFilter`（硬门槛，出局即不参与打分）→ `WorkerScorer`（有限、越低越好的成本，多 scorer 相加）→ `WorkerPicker`（从候选表挑一行）。候选行序未指定，三段都必须无副作用、不阻塞。
- **声明式输入**：想读 CACHE/LOAD/PREFERRED_TAINT 等信号必须先在 `required_worker_inputs` 声明；未声明的信号宿主不物化，且自定义策略拿到的是**原始事实**（如未加权的 `device_overlap_blocks`），不是内置公式的中间量。
- **注册表三级生命周期**：provider（启动一次，解析并校验 YAML 参数）→ factory（每 `(model, routing_group)` 分区一次，捕获校验后的参数）→ policy（分区私有状态，调度器串行调用）；`default` 是保留名，永远回落内置选点器；未知类型/重复注册/非法参数一律启动失败。
- **服务与目录分工**：`WorkerCatalog` 管 worker 的注册目录与生命周期（含选点后的 endpoint 防御性回查），`SelectionCore` 按分区组装索引器+调度器+policy，`SelectionServiceBuilder` 把 registry 作为必需参数构造整个服务——ext-proc（`WorkerType::Aggregated`）与 Python 前端/`select_service` 复用同一条构造路径。
- **打包机制**：策略靠**编译期链接**进入镜像——空默认 catalog 是名为 `dynamo-worker-selection-policy-catalog` 的依赖别名插槽；Python 扩展以 `--features custom-policy` 重建后，`import dynamo._core` 时填充进程级注册表；EPP 则在 `main` 里显式 `register` 后传入 `run(Some(registry))`。
- **同一插槽两种宿主**：`WorkerSelectionPolicy` 实现 `WorkerSelector`，而 `RoutingHost<Sel = DefaultWorkerSelector>` 对选择器泛型——自定义策略既可进独立选点服务，也可替换 u6-l2 内嵌路由的选择器。

## 7. 下一步学习建议

- **u10-l3（Inference Gateway：ext-proc 与 EPP）**：本讲只见了 ext-proc 构造选点服务的一面；下一阶段完整看 GAIE 拓扑下请求如何经 Gateway → EPP → ext-proc 选点 → frontend → worker，以及 #13811 之后的副本同步机制如何把多个 EPP 的 `SelectionService` 状态同步起来。
- **精读官方指南**：`docs/fern/pages/developer-guide/knowledge-base/modular-components/router/custom-worker-selection.mdx` 是 selection 模块 AGENTS 指定的配套文档，列出了全部可用上下文与 worker 信号（示例 README 中的旧相对链接已失效，以本路径为准）。
- **源码延伸**：`lib/kv-router/src/services/selection/tests.rs` 与 `scheduling/selector/policy.rs` 测试模块是策略契约的「活文档」；`simple-stacked-score-pick/src/scorer/`（active_requests + uncached_blocks 两个 scorer 分文件）是多成本栈叠的最小范本。
- **回到调度器**：本讲的 policy 被嵌进 `LocalScheduler`——想理解选点前后的排队（policy_class、DRR 类队列、容量预约），重读 u6-l2 的 scheduler 部分并结合 `lib/kv-router/src/scheduling/` 的模块级 AGENTS.md。
