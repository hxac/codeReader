# P/D 分离：概念与最小 disagg 拓扑

## 1. 本讲目标

学完本讲，你应该能够：

1. 用自己的话解释**为什么**要把 prefill 与 decode 拆成两个独立的 GPU 池，以及分离在什么条件下反而会亏。
2. 对照 `disagg.sh` 说出分离式拓扑里**每个进程的角色**（frontend、prefill worker、decode worker）、它们如何互相发现、以及 `agg.sh` 与 `disagg.sh` 的本质差异。
3. 在源码中**定位 prefill 与 decode 之间传递 KV 的组件**：角色如何声明（`DisaggregationMode` → `WorkerType`）、交接句柄如何流动（`disaggregated_params` → `PrefillResult` / `BootstrapInfo`）、真正的 KV 字节为什么根本不经过 frontend。
4. 完成一次故障实验：分别只杀 prefill 进程和只杀 decode 进程，观察请求失败模式与恢复过程，并写出报告。

本讲是「分离式服务与 KV 传输」单元的第一篇：只讲**概念与最小拓扑**，prefill 路由的切换条件（conditional disagg、admission）留给 u7-l2，NIXL/NCCL/memcpy 三种传输后端留给 u7-l3。

## 2. 前置知识

本讲建立在已学讲义之上，先快速回顾四个关键概念：

- **prefill 与 decode 两阶段**（u1-l1）：一条 LLM 请求分两步——prefill 一次性吃进整个 prompt 算出 KV cache 并产出第一个 token；decode 逐个自回归生成后续 token。KV cache 是「prompt 在注意力里的记忆」，prefill 算出来之后 decode 每步都要读它。
- **WorkerType 与就绪判定**（u4-l4）：每个 worker 注册时在 Model Deployment Card 里声明自己的角色（`worker_type`）和依赖（`needs`，DNF 形式：外层 OR、内层 AND）。frontend 的 discovery 层据此计算模型是否 ready——有活 worker 且 `missing`、`ambiguous` 均为空才算就绪。**P/D 成对存在是就绪的前提**，这一点在本讲的故障实验里会反复出现。
- **KV 感知路由**（u6-l1/u6-l2）：frontend 内嵌的 Rust 路由器根据 KV 重合度与负载为请求选 worker。本讲的 `PrefillRouter` 是叠加在它之上的一个「前向算子」——它先替请求跑一次 prefill，再把结果塞进 decode 请求。
- **九步请求流程 S1–S9**（u1-l1）：其中 S5 是「回传 `disaggregated_params` 元数据」、S7 是「经 NIXL 点对点直传 KV 数据」。本讲就是把这两步拆开看清楚。

另外两个术语澄清：

- **`WorkerType`（Rust 枚举）**：worker 的角色标签，规范定义在 `dynamo-kv-router` crate。
- **`DisaggregationMode`（Rust 枚举，Python 侧同名）**：worker **启动时**的角色开关（`--disaggregation-mode`），是 `WorkerConfig` 上的元数据；`Worker` 在注册时把它翻译成 `WorkerType` + `needs`。一个说「我以什么身份启动」，一个说「我在服务目录里以什么身份被发现」。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
|------|------|-----------|
| `docs/fern/pages/developer-guide/knowledge-base/concepts/system-architecture/disaggregated-serving.md` | 分离式服务的官方概念文档 | 为什么要分离、三步执行模型、NIXL 传输、容量代价 |
| `examples/backends/sample/launch/disagg.sh` | sample 后端的分离式启动脚本 | 三个进程如何编排 |
| `examples/backends/sample/launch/agg.sh` | sample 后端的聚合式启动脚本 | 与 disagg.sh 对照 |
| `lib/kv-router/src/worker_type.rs` | `WorkerType` 规范定义 | 四种角色、线格式 |
| `lib/llm/src/worker_type.rs` | 对上文件的再导出 | 为什么规范定义放在 kv-router |
| `lib/backend-common/src/disagg.rs` | `DisaggregationMode` 定义 | 启动期角色开关 |
| `lib/backend-common/src/worker.rs` | Rust `Worker` 注册逻辑 | 角色 → (`worker_type`, `needs`) 的翻译 |
| `components/src/dynamo/common/backend/sample_engine.py` | sample 引擎的分离调度 | prefill 截断/盖章、decode 校验 |
| `components/src/dynamo/common/backend/disagg.py` | 分离调度的小工具函数 | `enforce_prefill_max_tokens` 等三个 helper |
| `lib/llm/src/kv_router/prefill_router/mod.rs` | 前端 `PrefillRouter` | 交接句柄如何从 prefill 流到 decode |
| `lib/llm/src/protocols/common/llm_backend.rs` | 引擎输出/后端输出协议 | `disaggregated_params` 字段契约 |
| `lib/llm/src/protocols/common/preprocessor.rs` | `PreprocessedRequest` 定义 | `PrefillResult` / `bootstrap_info` 字段 |
| `examples/common/launch_utils.sh` | 启动脚本共享工具 | `wait_any_exit` 的「一死全死」语义（故障实验必须绕开它） |

## 4. 核心概念与源码讲解

### 4.1 为什么要分离：两阶段的计算特征不一样

#### 4.1.1 概念说明

prefill 和 decode 虽然跑同一个模型，但资源画像截然不同：

- **prefill 是计算密集型**：一次前向要处理整个 prompt（可能几千上万 token），矩阵乘大、GPU 利用率高，吃 **算力**。
- **decode 是访存密集型**：每步只处理 1 个 token，但要读全部历史 KV cache，瓶颈是 **显存带宽与 KV 容量**，吃 **显存**。

把两者混在一个进程里会产生两类互相伤害：

1. **长 prompt 阻塞短生成**：一条长上下文请求的 prefill 挤占迭代步，正在 decode 的请求 ITL（token 间隔）被拉长。
2. **无法按阶段配硬件**：prefill 适合小 TP（张量并行）配大批 token；decode 适合大 TP 腾出更多显存放 KV——混部时只能取一个折中。

分离之后，两个池各自优化，代价是多了一次 **KV 交接**（KV transfer）。

#### 4.1.2 核心流程

官方文档把分离式执行归纳为三步：

```text
1. prefill 引擎计算 prefill 阶段，产出 KV cache
2. prefill 引擎把 KV cache 传输给 decode 引擎
3. decode 引擎计算 decode 阶段
```

关键在于第 2 步：KV 的**字节**走引擎间的直达通道（NIXL，GPU 显存到显存），而且**非阻塞**——传输期间 prefill 引擎可以继续服务其他请求；frontend/路由器只搬运一个很小的**句柄**（`disaggregated_params`），告诉 decode 端「KV 在哪、怎么取」。

一个容易忽略的警告（文档 "Capacity effects of disaggregation" 一节）：**分离改变的是计算和显存被消耗的位置，不会让任何一个阶段变免费**。低并发时聚合 worker 反而省掉传输开销；只有当 prefill 与 decode 在持续负载下互相争抢时，分离才靠延迟隔离赚回成本。

#### 4.1.3 源码精读

[disaggregated-serving.md:L7](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/docs/fern/pages/developer-guide/knowledge-base/concepts/system-architecture/disaggregated-serving.md#L7) 用一段话给出动机：两阶段「计算特征与显存占用不同」，分离后可以「decode 用更大 TP、prefill 用更小 TP」，并且长上下文请求的 prefill 不再阻塞在途 decode。

[disaggregated-serving.md:L9-L12](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/docs/fern/pages/developer-guide/knowledge-base/concepts/system-architecture/disaggregated-serving.md#L9-L12) 是三步执行模型；[L16-L18](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/docs/fern/pages/developer-guide/knowledge-base/concepts/system-architecture/disaggregated-serving.md#L16-L18) 指出高性能分离的关键是高效 KV 传输：NIXL 直接从 prefill 引擎的显存传到 decode 引擎的显存，且传输是非阻塞的。

[L22-L44](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/docs/fern/pages/developer-guide/knowledge-base/concepts/system-architecture/disaggregated-serving.md#L22-L44) 的时序图把编排者点名为 `PrefillRouter`：frontend 收请求 → 路由器选 prefill worker → prefill 算 KV 并回传 `disaggregated_params` → 路由器选 decode worker、把交接元数据注入 decode 请求 → decode 与 prefill 之间做 NIXL 传输 → decode 流式出 token。[L46-L52](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/docs/fern/pages/developer-guide/knowledge-base/concepts/system-architecture/disaggregated-serving.md#L46-L52) 展开为四步：worker 选择（KV 感知或简单负载均衡）→ prefill 执行 → decode 路由 → KV 传输。

[L56-L62](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/docs/fern/pages/developer-guide/knowledge-base/concepts/system-architecture/disaggregated-serving.md#L56-L62) 说明交接元数据**按后端各不相同**：SGLang 用 `bootstrap_info`（host/port/room_id 三元组做 RDMA bootstrap，prefill 可后台跑）；vLLM 用 `kv_transfer_params`（块 ID + 远端连接信息，prefill 同步跑）；TensorRT-LLM 用 `opaque_state`（序列化内部元数据）。所以 Dynamo 把这个字段设计成**引擎自有**的不透明 JSON（见 4.4.3）。

[L65-L72](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/docs/fern/pages/developer-guide/knowledge-base/concepts/system-architecture/disaggregated-serving.md#L65-L72) 是运行时可重配的 **xPyD**（x 个 prefill、y 个 decode）：worker 加减只需向服务发现注册/注销，路由器自动跟进——这是本讲故障实验「恢复过程」的文档依据。

最后 [L117-L129](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/docs/fern/pages/developer-guide/knowledge-base/concepts/system-architecture/disaggregated-serving.md#L117-L129) 给出诚实的容量账：低并发聚合更优、争抢时分离改善延迟隔离、decode KV 容量是瓶颈时把 GPU 划给 prefill 池反而会降低总容量；池间再平衡交给 Planner，池内选点交给路由器。

#### 4.1.4 代码实践

**实践目标**：把官方时序图与本手册 u1-l1 的九步请求流程（S1–S9）对上号。

**操作步骤**：

1. 打开 [disaggregated-serving.md:L24-L44](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/docs/fern/pages/developer-guide/knowledge-base/concepts/system-architecture/disaggregated-serving.md#L24-L44) 的 mermaid 时序图。
2. 回忆 u1-l1 的 S1–S9（S5 = 回传 `disaggregated_params`，S7 = NIXL 直传 KV）。
3. 做一张两列对照表：左列是 mermaid 图的每一跳，右列写对应的 S 编号；无法对应的跳标「新增/细化」。

**需要观察的现象**：图中 `Prefill-->>Router: disaggregated_params` 与 `Decode<<->>Prefill: KV transfer (NIXL)` 是两条**不同的边**——一条经过路由器（控制信息），一条不经过（数据本身）。

**预期结果**：控制流（谁跟谁说话）与数据流（KV 字节）在分离式服务里是分离的；这正是本讲 4.4 的主题。此实践为纯阅读型，无需运行。

#### 4.1.5 小练习与答案

**练习 1**：某集群 decode 池的 KV 容量已是瓶颈，运维想再加 2 个 prefill 副本缓解长 prompt 排队。依据文档的容量分析，这个决定一定正确吗？

**答案**：不一定。文档 "Capacity effects of disaggregation" 明确指出：当 decode KV 容量是瓶颈时，把 GPU 划给 prefill 池会减少 decode 总容量，除非延迟收益能补偿被移走的 GPU。应该先用观测确认瓶颈在延迟隔离还是在容量。

**练习 2**：为什么 KV 传输必须设计成非阻塞（传输期间 prefill 还能继续服务其他请求）？

**答案**：prefill 池的吞吐依赖流水线重叠。若传输阻塞 prefill 引擎的前向，每个请求的交接时间都会串行占用 prefill 算力，长上下文场景下分离带来的吞吐收益会被传输串行化吃掉；非阻塞传输让「为 A 传 KV」与「为 B 算 prefill」并行。

**练习 3**：三个后端的交接元数据格式为什么可以互不相同？

**答案**：因为 `disaggregated_params` 是引擎自有的不透明负载（`serde_json::Value`），Dynamo 框架只负责搬运、不解释内容；每个后端塞自己传输栈需要的句柄（SGLang 的 bootstrap 三元组、vLLM 的 `kv_transfer_params`、TRT-LLM 的 `opaque_state`）。

### 4.2 角色声明：DisaggregationMode 与 WorkerType

#### 4.2.1 概念说明

一个 worker 「以 prefill 身份启动」需要两层表达：

- **启动期**：`--disaggregation-mode {agg,prefill,decode,encode}` 传给 worker 进程，落到 `WorkerConfig.disaggregation_mode`。这是**唯一事实来源**——Rust `Worker` 在注册时读取它做两个决定：以什么 `worker_type`/`ModelType` 注册、是否广告本地 KV indexer。
- **发现期**：frontend 在服务目录里看到的是 `WorkerType`。它有四个值：`Prefill`、`Decode`、`Encode`、`Aggregated`——每个 worker 恰好一个角色，`Aggregated` 表示一个进程兼做两阶段。

注意 `Encode` 是多模态编码角色，位于 P/D **之前**（E/P/D 拓扑）。本讲只做交叉引用，细节在 u8-l9。

#### 4.2.2 核心流程

角色翻译发生在 worker 注册时，伪代码如下：

```text
输入: config.disaggregation_mode, config.route_to_encoder

resolve_model_type:
    Prefill  -> ModelType::Prefill        # 旧标记位，无 OpenAI 面
    Encode   -> ModelType::empty()        # 无公开服务面，仅参与就绪
    其他     -> 解析 --endpoint-types

resolve_worker_type_and_needs:  # needs 是 DNF: 外层 OR, 内层 AND
    Prefill    -> (Prefill,  [[Decode]] 或 [[Decode, Encode]] 若 route_to_encoder)
    Decode     -> (Decode,   [[Prefill]])
    Aggregated -> (Aggregated, [] 或 [[Encode]] 若 route_to_encoder)
    Encode     -> (Encode,   [[Prefill, Decode], [Aggregated]])
```

要点：

- **依赖是双向的**：prefill 需要 decode 对等点，decode 也需要 prefill 对等点。结合 u4-l4 的就绪判定（`missing` 非空即不 ready），这就是「P/D 成对是就绪前提」在源码里的落点。
- **线格式统一小写字符串**：`"prefill"`/`"decode"`/`"encode"`/`"aggregated"`，serde、日志、指标标签共用，解析大小写不敏感。

#### 4.2.3 源码精读

[lib/llm/src/worker_type.rs:L9](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/worker_type.rs#L9) 只有一行再导出。文件头注释解释了为什么规范定义放在 `dynamo-kv-router`：让 policy provider 和所有 router host 共享同一类型而不引入依赖环——这是 u1-l3「crate 分组」里见过的依赖拓扑技巧。

规范定义在 [lib/kv-router/src/worker_type.rs:L16-L23](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/kv-router/src/worker_type.rs#L16-L23)：四值枚举，`serde(rename_all = "lowercase")`；[L36-L45](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/kv-router/src/worker_type.rs#L36-L45) 的 `default_selector_label` 保留了历史兼容：内置选点器只区分 prefill 池与其余池（`Decode | Encode | Aggregated` 都映射 `"decode"` 标签），打分与指标标签契约不变。[L68-L82](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/kv-router/src/worker_type.rs#L68-L82) 的 `FromStr` 大小写不敏感地解析四个规范名，其余报 `unrecognized worker_type`。

启动期的角色开关在 [lib/backend-common/src/disagg.rs:L27-L47](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/backend-common/src/disagg.rs#L27-L47)：`DisaggregationMode` 四值，`Aggregated` 是默认值（别名 `agg`/`aggregated`），`clap::ValueEnum` 派生让它直接对接 `--disaggregation-mode` 旗标。[L64-L69](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/backend-common/src/disagg.rs#L64-L69) 的 `discovery_component()` 还决定角色注册在哪个发现组件下：prefill 注册为 `prefill` 组件，decode/agg/encode 共享 `backend` 组件——这是「frontend 能对 prefill/decode 交接分开路由」的目录基础（sample 后端用 `--component` 显式覆盖成 `sample-prefill`/`sample-decode`，见 4.3）。

翻译逻辑本体在 [lib/backend-common/src/worker.rs:L1851-L1859](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/backend-common/src/worker.rs#L1851-L1859)（`resolve_model_type`：prefill 注册旧标记位 `ModelType::Prefill` 双发以兼容跨版本混部，encode 注册无服务面）与 [L1871-L1898](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/backend-common/src/worker.rs#L1871-L1898)（`resolve_worker_type_and_needs`：把四节伪代码里的 DNF 依赖真正写出来）。注意 [L1900-L1912](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/backend-common/src/worker.rs#L1900-L1912) 的守卫：`--route-to-encoder` 只对 agg/prefill 有意义，配在 decode/encode 上是启动即失败的配置错误。

#### 4.2.4 代码实践

**实践目标**：验证角色翻译表，并跑通守护它的单元测试。

**操作步骤**：

1. 阅读 [lib/backend-common/src/worker.rs:L1871-L1898](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/backend-common/src/worker.rs#L1871-L1898)。
2. 手工推导四种 `disaggregation_mode`（各取 `route_to_encoder` 真/假）对应的 `(WorkerType, needs)`，写成 8 行表格。
3. 运行仓库已有的测试核对：`cargo test -p dynamo-backend-common worker_type`（命中 `worker.rs` 内断言这张表的单测）与 `cargo test -p dynamo-kv-router worker_type`（命中枚举解析/线格式单测）。

**需要观察的现象**：测试输出里 `resolve_worker_type_and_needs` 相关用例断言的 needs 值（如 `vec![vec![WorkerType::Decode]]`）与你表格里的一致。

**预期结果**：8 行表格与测试断言完全吻合；`Encode` 的 needs 是 `[[Prefill, Decode], [Aggregated]]`（两条 OR 分支）。若 `cargo test` 因环境（如 macOS 需 `--no-default-features`）跑不起来，静态核对 [L2583-L2635](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/backend-common/src/worker.rs#L2583-L2635) 附近的断言即可，标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `WorkerType` 的规范定义放在 `lib/kv-router` 而不是 `lib/llm`？

**答案**：`lib/llm/src/worker_type.rs` 的注释说明：policy provider 与所有 router host（kv-router 一侧）都要共享这个类型，若定义在 `dynamo-llm` 会让 kv-router 反向依赖 llm，形成依赖环；所以规范类型下沉到 kv-router，llm 再导出。

**练习 2**：decode worker 的 `needs` 是什么？这如何影响 frontend 的就绪判定？

**答案**：`[[Prefill]]`（一个 AND 分支、只含 Prefill）。按 u4-l4 的就绪规则，`missing` 非空即不 ready——所以 prefill 全部下线时，即使 decode 还活着，模型也不就绪。这是本讲故障实验的关键预期。

**练习 3**：`default_selector_label` 为什么把 `Encode` 和 `Aggregated` 都标成 `"decode"`？

**答案**：内置选点器历史上只区分 prefill 池与其余池；引入类型化角色后为了保住既有打分与指标标签契约，非 prefill 角色统一落到 `"decode"` 池标签，自定义策略再按 `WorkerType` 细分。

### 4.3 disagg.sh：三个进程的最小分离拓扑

#### 4.3.1 概念说明

[examples/backends/sample/launch/disagg.sh](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/examples/backends/sample/launch/disagg.sh) 用 sample（回显）后端搭出**无 GPU 的最小分离拓扑**，共三个进程：

1. **frontend**（`python3 -m dynamo.frontend`）：OpenAI 兼容 HTTP 入口 + 内嵌路由器（含 `PrefillRouter`）。
2. **prefill worker**：`sample_main --disaggregation-mode prefill --component sample-prefill`。
3. **decode worker**：`sample_main --disaggregation-mode decode --component sample-decode`。

与 [agg.sh](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/examples/backends/sample/launch/agg.sh) 的差异只有一个维度：**worker 数量与角色**。agg.sh 起一个默认 `Aggregated` 模式的 worker，prefill+decode 在同一进程内完成，没有交接；disagg.sh 把它一分为二，请求因此要走完整的 prefill→decode 交接链路。脚本头注释明说这是「统一分离路径的 CI 冒烟测试」——sample 引擎用**合成的** `disaggregated_params` 盖章，不搬真 KV，但走的是与真实后端相同的框架代码路径。

#### 4.3.2 核心流程

disagg.sh 的编排逻辑：

```text
source launch_utils.sh            # 取 wait_any_exit / banner
trap 'kill 0' EXIT                # 退出时杀整个进程组
python3 -m dynamo.frontend &                        # 进程 1
DYN_SYSTEM_PORT=8081 sample_main --disaggregation-mode prefill \
                                --component sample-prefill &   # 进程 2
DYN_SYSTEM_PORT=8082 sample_main --disaggregation-mode decode \
                                --component sample-decode &   # 进程 3
wait_any_exit                     # 任一子进程退出 => 全组 teardown
```

sample 引擎在 `generate()` 里按模式分叉（引擎侧调度，框架不管）：

```text
DECODE 且非健康探测:  require_prefill_result(request)   # 缺 prefill_result 立即报错
PREFILL:              enforce_prefill_max_tokens(request) # max_tokens 截到 1
正常生成:  逐 token 循环; 最后一个 chunk 上:
    PREFILL -> 额外盖上 disaggregated_params = {sample_handle, completed_tokens}
```

#### 4.3.3 源码精读

[disagg.sh:L5-L13](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/examples/backends/sample/launch/disagg.sh#L5-L13) 的头注释概括了全部机制：prefill worker 注册为 `WorkerType::Prefill`（统一 Rust `Worker` 按 `WorkerConfig.disaggregation_mode` 改写注册），frontend 的 `PrefillRouter` 负责把合成的 `disaggregated_params` 句柄从 prefill 转发给 decode。

[disagg.sh:L54](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/examples/backends/sample/launch/disagg.sh#L54) 起 frontend；[L61-L66](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/examples/backends/sample/launch/disagg.sh#L61-L66) 起 prefill worker，[L69-L74](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/examples/backends/sample/launch/disagg.sh#L69-L74) 起 decode worker。三个细节值得注意：每个 worker 有独立的 `DYN_SYSTEM_PORT`（8081/8082，避免并行 CI 的指标端口冲突）；两个 worker 用**不同的 `--component` 名**（`sample-prefill`/`sample-decode`），注释说明这是为了让两类 worker 在服务发现里分开可见，镜像 vLLM/SGLang/TRT-LLM 的按角色组件划分；额外的未知参数经 `EXTRA_ARGS` 同时透传给两个 worker（[L61-L74](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/examples/backends/sample/launch/disagg.sh#L61-L74) 的 `"${EXTRA_ARGS[@]}"`）——所以 `--delay 0.05` 这类参数对两边同时生效。

[disagg.sh:L77](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/examples/backends/sample/launch/disagg.sh#L77) 的 `wait_any_exit` 加 [L48](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/examples/backends/sample/launch/disagg.sh#L48) 的 `kill 0` 陷阱构成「一死全死」语义：[launch_utils.sh:L92-L106](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/examples/common/launch_utils.sh#L92-L106) 用 `wait -n` 盯**所有**后台子进程，任何一个退出就向整个进程组发 TERM 并传播其退出码。这对冒烟测试是正确行为（故障快速暴露），但意味着**不能**直接用 disagg.sh 做单进程击杀实验——综合实践里我们会绕开它手动拉起三个进程。

对照 [agg.sh:L44-L49](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/examples/backends/sample/launch/agg.sh#L44-L49)：frontend + 一个不传 `--disaggregation-mode` 的 worker（默认 `Aggregated`），组件名也用默认值 `sample`。两脚本唯一的结构性差异就是 worker 的拆分方式。

引擎侧调度在 [sample_engine.py:L93-L103](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/components/src/dynamo/common/backend/sample_engine.py#L93-L103) 的类 docstring 里写得很完整：PREFILL 截断到一个 token 并在终止 chunk 盖合成 `disaggregated_params`；DECODE 要求请求带 `prefill_result`，否则说明「frontend 忘了路由经过 prefill 对等点」。[L144-L149](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/components/src/dynamo/common/backend/sample_engine.py#L144-L149) 是 `--disaggregation-mode` 参数定义（四选一，默认 agg）。真正的分叉点在 [L342-L348](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/components/src/dynamo/common/backend/sample_engine.py#L342-L348)：健康探测（canary probe，u12-l4 金丝雀机制的探针）绕过检查按聚合路径跑；decode 模式调 `require_prefill_result`；prefill 模式调 `enforce_prefill_max_tokens`。盖章发生在 [L423-L427](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/components/src/dynamo/common/backend/sample_engine.py#L423-L427)：最后一个 chunk 上写入 `{"sample_handle": <uuid>, "completed_tokens": [...]}`——真实后端在这里塞的是 NIXL/RDMA 句柄，sample 用一个 UUID 假装。

三个 helper 在 [disagg.py:L31-L39](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/components/src/dynamo/common/backend/disagg.py#L31-L39)（`enforce_prefill_max_tokens`：把 `stop_conditions.max_tokens` 原位截到 1——prefill 只需为 prompt 填充 KV，多产 token 是浪费计算且破坏交接契约）、[L42-L49](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/components/src/dynamo/common/backend/disagg.py#L42-L49)（`extract_prefill_result`：取可选的 `prefill_result` 字典，聚合模式为 None）与 [L52-L66](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/components/src/dynamo/common/backend/disagg.py#L52-L66)（`require_prefill_result`：decode 模式的规范前置条件，缺失时抛出点名「prefill 路由器应转发 disaggregated_params」的错误）。

#### 4.3.4 代码实践

**实践目标**：并排跑通 agg 与 disagg 两种拓扑，从进程与日志层面确认差异。

**操作步骤**：

1. 终端 A：`cd examples/backends/sample/launch && ./agg.sh`，待 banner 出现后按提示 curl 一条 chat 请求，然后 Ctrl+C。
2. 终端 B：`./disagg.sh`，同样 curl 一条，注意 `--max-tokens 32` 时响应仍返回约 32 个 token。
3. 两次运行各执行 `ps -ef | grep sample_main`，数进程、抄组件名；对比两边 frontend 日志里出现的组件/端点字符串。

**需要观察的现象**：agg 只有一个 `sample_main` 进程（组件 `sample`）；disagg 有两个（`sample-prefill`、`sample-decode`），且各自监听不同的 `DYN_SYSTEM_PORT` 指标端口。disagg 下前端日志能看到 prefill 与 decode 两个不同 worker 的注册记录。

**预期结果**：两种拓扑对 curl 客户端**完全同形**——同样的 OpenAI 请求、同样条数的流式响应；分离对客户端透明，`max_tokens` 恢复由前端负责（见 4.4）。若在你的环境里 curl 返回条数与预期不符，记录实际值并核对是否走了 `--delay` 缺省路径（0.01s/token）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 prefill worker 要把 `max_tokens` 截到 1，而不是干脆传 0？

**答案**：截到 1 是 helper 的固定实现（`stop["max_tokens"] = 1`）。prefill 仍需产出**一个** token：既验证前向可用，也构成 `disaggregated_params` 里 `completed_tokens` 的内容；0 会让某些引擎把请求当空生成处理。注释原话：prefill 只需要为 prompt 填充 KV，多产 token 是浪费且破坏交接契约。

**练习 2**：客户端要求 32 个 token，prefill 只出 1 个，最终响应为什么还是有 32 个？

**答案**：前端 `PrefillRouter` 在派发 prefill 前**保存了原始 `max_tokens`**，prefill 请求只是 `clone` 出来截到 1 的副本；发给 decode 的请求会恢复原值（见 4.4.3 的 `original_max_tokens`）。

**练习 3**：为什么两个 worker 要用不同的 `--component` 名？

**答案**：脚本注释说明：不同的组件名让两类 worker 在服务发现里分开可见，镜像 vLLM/SGLang/TRT-LLM 按角色划分组件的做法；否则两个同路径实例会被当作同一端点的多副本做负载均衡，而不是两个不同角色。

### 4.4 KV 移交：PrefillRouter 与 disaggregated_params

#### 4.4.1 概念说明

分离式服务里最容易误解的一点：**KV cache 的字节从不经过 frontend**。frontend 只搬运一个小的 JSON 句柄，数据面由引擎之间点对点直传：

```text
                    ┌─ 控制面（小、经过路由器）─────────────┐
client → frontend → PrefillRouter → prefill worker
                            │  disaggregated_params（句柄）
                            ▼
                       decode worker  ──→ 流式 token 回 frontend
                    └─ 数据面（大、不经过路由器）──────────┘
            prefill GPU 显存 ═══ NIXL/RDMA ═══> decode GPU 显存
```

承担「搬运句柄」职责的组件是 **`PrefillRouter`**（`lib/llm/src/kv_router/prefill_router/mod.rs`）：一个位于 Migration 与 decode 路由器之间的**前向算子**——它可选地先让请求在 prefill worker 上跑一遍，从 prefill 响应里抽出 `disaggregated_params`，注入 decode 请求，再把 decode 请求交给下游（decode 路由器/引擎）。

句柄的载体分三段：

1. **prefill 的终止 chunk** 上：`LLMEngineOutput.disaggregated_params`（引擎自有、框架不解释）。
2. **decode 请求**上：`PreprocessedRequest.prefill_result`（结构化的 `PrefillResult`）或 `bootstrap_info`（SGLang 后台 prefill 路径）。
3. **客户端响应**上：**没有**——`disaggregated_params` 在回传前被剥掉，它是引擎间契约，不是公开响应通道。

#### 4.4.2 核心流程

`PrefillRouter::generate` 的主干（省略 conditional disagg 分支，那是 u7-l2 的主题）：

```text
输入: req (PreprocessedRequest), context

1. original_max_tokens = req.stop_conditions.max_tokens     # 先存原值
2. 若生命周期非 Active（无 prefill worker 或全部死亡）:
     直接 next.generate(req)                                 # 旁路分离
3. 构造 prefill_req = req.clone(), max_tokens = 1
4. 选择并派发 prefill:
     router.select_and_dispatch_prefill(...)
5. 消费 prefill 流, 得到三种结局之一:
     Terminal   -> prefill 自己终止(如首 token 即 EOS):
                    剥掉 disaggregated_params 后直接把该输出发回客户端, 流程结束
     Completed  -> 拿到完整 PrefillResult(含 disaggregated_params)
     Bootstrap  -> 响应里带 bootstrap_info(SGLang 风格):
                    prefill 后台继续跑, decode 立即启动, KV 传输与之并行
6. 组装 decode_req:
     Completed -> decode_req.prefill_result = Some(result)
     Bootstrap -> decode_req.bootstrap_info = Some(info)
     两者都记 routing.prefill_worker_id
7. decode_req.stop_conditions.max_tokens = original_max_tokens   # 恢复
8. decode 路由强制"仅看负载"(overlap 信用清零), next.generate(decode_req)
```

decode worker 收到后，`require_prefill_result` 取出句柄交给引擎的 resume-from-KV 调用（真实后端），sample 引擎则只是确认它存在。

另一个关键设计（源码注释里的 NVBugs 5969206 记录）：prefill 完成后**即使 context 已被取消，decode 路由也必须继续**——否则进行中的 KV 传输会变成无接收者的孤儿，永久泄漏 KV 块。

#### 4.4.3 源码精读

[prefill_router/mod.rs:L159-L179](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/kv_router/prefill_router/mod.rs#L159-L179) 的类型 docstring 给出三种工作模式（query-only 查询选点、pre-routed 外部指定 worker、normal 由路由决策）与一条重要边界：客户端可见的 logprobs **不应**放进 `disaggregated_params`——那是「引擎自有的 KV 交接契约，不是公共响应通道」。

[L297-L305](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/kv_router/prefill_router/mod.rs#L297-L305)：先保存原始 `max_tokens`，然后是生命周期门——`PrefillRouter` 未激活（没发现 prefill worker）或已去激活（全部死亡）时直接把请求透传给下游；注释强调模型准入仍由已注册 worker 拓扑在**更早阶段**把关。这是故障实验「杀 prefill」分支的第一现场。

[L380-L382](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/kv_router/prefill_router/mod.rs#L380-L382)：prefill 请求是 `req.clone()` 出来的副本，`max_tokens` 截到 1（与引擎侧 `enforce_prefill_max_tokens` 双保险：前端截一次，引擎再截一次）。[L392-L394](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/kv_router/prefill_router/mod.rs#L392-L394) 为 prefill 上下文传播 `first_response_guard`（u3-l4 讲过的守卫，保住前端持有的注册内存到首个响应）。

[L416-L433](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/kv_router/prefill_router/mod.rs#L416-L433)：`select_and_dispatch_prefill` 选择并派发 prefill；若派发准备阶段就能取到 `bootstrap_info`，则 `spawn_prefill_task` 把 prefill 丢到后台、decode 立刻出发——「prefill 与 decode 并行，等 prefill 再启动 decode 会死锁 KV 传输」这一纪律的直接实现。[L435-L456](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/kv_router/prefill_router/mod.rs#L435-L456) 否则同步消费 prefill 流，`Handoff` 结局里用 `extract_bootstrap_info` 从 `disaggregated_params` 里识别 bootstrap 形态（SGLang），区分 `Bootstrap`/`Completed` 两种结局。

[L485-L494](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/kv_router/prefill_router/mod.rs#L485-L494)：`Terminal` 结局的直通路径——prefill 自己终止时（例如一步上下文就命中 EOS），把响应剥净后直接返回客户端，不再发起一个缺句柄的 decode 请求。剥净用的是 [L146-L153](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/kv_router/prefill_router/mod.rs#L146-L153) 的 `strip_terminal_disaggregated_params`，保证引擎间句柄永不泄漏到客户端。

[L518-L545](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/kv_router/prefill_router/mod.rs#L518-L545)：交接的落点——`Completed` 结局写 `decode_req.prefill_result = Some(result)` 并带上 `migration_link`（OTel 跨进程链路），`Bootstrap` 结局写 `decode_req.bootstrap_info`，两者都记录 `routing.prefill_worker_id`；最后 `max_tokens` 恢复原值。[L547-L555](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/kv_router/prefill_router/mod.rs#L547-L555) 给 decode 请求盖上「仅看负载」的路由覆盖（正常分离下 decode 的 overlap 信用清零——KV 已经在路上了，decode 选点不该再按重合度打分），然后 `next.generate(decode_req)` 进入 decode 路由。

字段契约：[llm_backend.rs:L120-L126](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/protocols/common/llm_backend.rs#L120-L126) 定义 `disaggregated_params: Option<serde_json::Value>`，注释明确这是**引擎自有**负载——各后端塞自己的 KV 传输格式（vLLM `kv_transfer_params`、SGLang bootstrap 三元组、TRT-LLM 编码后的 `LlmDisaggregatedParams`），「Dynamo 不向该字段注入框架元数据」（框架侧的追踪链接走旁边的 `worker_trace_link`）。[L202-L204](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/protocols/common/llm_backend.rs#L202-L204) 是 `LLMEngineOutput` 上的同名字段——prefill 终止 chunk 盖章的位置。

接收端：[preprocessor.rs:L194-L203](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/protocols/common/preprocessor.rs#L194-L203) 的 `PrefillResult` 结构体（`disaggregated_params` + 可选 `prompt_tokens_details`，框架「读而不释」地透传给推理引擎）；[L323-L326](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/protocols/common/preprocessor.rs#L323-L326) 是 `PreprocessedRequest.prefill_result` 字段，[L351-L354](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/protocols/common/preprocessor.rs#L351-L354) 是 `bootstrap_info` 字段。

#### 4.4.4 代码实践

**实践目标**：静态追踪一条请求的 `disaggregated_params` 全程，确认「控制面经过路由器、数据面不经过」。

**操作步骤**：

1. 在仓库根执行 `grep -rn "disaggregated_params" lib/llm/src | grep -v test`，列出所有触点。
2. 按本节引用的五个文件排序：`llm_backend.rs`（字段定义）→ `sample_engine.py`（盖章）→ `prefill_router/mod.rs`（抽取/注入/剥除）→ `preprocessor.rs`（载体结构）→ `disagg.py`（引擎侧消费）。
3. 用 `git grep -n "NIXL\|nixl" lib/llm/src/block_manager/block/transfer/ | head` 粗看数据面的所在（nixl.rs 等传输后端，u7-l3 精读）。
4. 画一张时序图：frontend → PrefillRouter → prefill worker → （句柄回）→ PrefillRouter → decode worker →（NIXL 直传，虚线画在 prefill 与 decode 之间）→ token 流回。

**需要观察的现象**：grep 结果里 `disaggregated_params` 的所有读写点都在**控制路径**（协议结构、路由器、引擎的 generate 分叉）上；任何 KV 块数据的搬运代码都不引用这个字段，而在 `block_manager/block/transfer/` 下。

**预期结果**：时序图与官方文档 [disaggregated-serving.md:L24-L44](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/docs/fern/pages/developer-guide/knowledge-base/concepts/system-architecture/disaggregated-serving.md#L24-L44) 的 mermaid 图一致；能指着自己图的某条边说出对应的源码行号。

#### 4.4.5 小练习与答案

**练习 1**：如果客户端在 prefill 刚完成、KV 传输还在途时断开连接，`PrefillRouter` 会中止 decode 路由吗？

**答案**：不会。[prefill_router/mod.rs:L496-L507](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/kv_router/prefill_router/mod.rs#L496-L507) 明确：context 被取消后 decode 路由仍继续（仅记日志），因为阻塞 decode 会让 KV 传输成为无接收者的孤儿、永久泄漏 KV 块；清理交给 decode 侧的 `kv_transfer_complete_event` 守卫。

**练习 2**：decode 请求发出前，`PrefillRouter` 做了哪两件「恢复原状」的事？

**答案**：恢复 `stop_conditions.max_tokens` 为客户端原值（prefill 副本被截到 1）；盖 `router_config_override` 让 decode 路由仅看负载（overlap 信用清零），因为 KV 已在传输途中，decode 选点再按重合度打分没有意义。

**练习 3**：为什么 `disaggregated_params` 是 `serde_json::Value` 而不是一个强类型结构？

**答案**：因为它是引擎间的不透明契约，格式由各后端自定义（vLLM/SGLang/TRT-LLM 各不相同）；框架只搬运不解释，用 `Value` 保持中立。强类型化会把框架与每个后端的传输细节耦合。文档对应的论述在 [disaggregated-serving.md:L56-L62](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/docs/fern/pages/developer-guide/knowledge-base/concepts/system-architecture/disaggregated-serving.md#L56-L62)。

## 5. 综合实践

### 分离式拓扑故障实验报告（杀 prefill vs 杀 decode）

**背景与关键陷阱**：`disagg.sh` 的 `wait_any_exit` + `kill 0` 是「一死全死」——任何一个进程退出，整个进程组被拆掉。要做**单进程**击杀实验，必须绕开脚本手动拉起三个进程。

**第 0 步：手动搭拓扑**（三个终端；沿用 u1-l2/u2-l1 的零依赖发现方式，`--discovery-backend file`，事件面缺省即 zmq，无需 etcd/NATS）：

```bash
# 终端 1：frontend
python3 -m dynamo.frontend --discovery-backend file

# 终端 2：prefill worker（记为 P）
python3 -m dynamo.common.backend.sample_main \
  --discovery-backend file \
  --model-name sample-model \
  --component sample-prefill \
  --disaggregation-mode prefill \
  --delay 0.05

# 终端 3：decode worker（记为 D）
python3 -m dynamo.common.backend.sample_main \
  --discovery-backend file \
  --model-name sample-model \
  --component sample-decode \
  --disaggregation-mode decode \
  --delay 0.05
```

（模型名沿用 u1-l2 的建议：若前端加载分词器报错，换一个本地已缓存的真实 HF 模型名。`--delay 0.05` 让 token 出得更慢，便于在生成中途击杀。）

**第 1 步：基线**。用 banner 里同款的 curl 发一条 `max_tokens: 32` 的请求，确认流式返回约 32 个 token。再发一条并在两份 worker 日志里找同一条 request id——确认请求确实先后经过 P 与 D。

**第 2 步：只杀 decode（D）**。新请求发出后观察：

- frontend 的行为（报错形态、状态码）；
- frontend 日志中模型就绪状态的变化——对照 u4-l4：P 的卡片 `needs=[[Decode]]`，D 死后 `missing` 非空，模型应转为不就绪；同时对照 4.4.3 的生命周期门（[prefill_router/mod.rs:L300-L305](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/kv_router/prefill_router/mod.rs#L300-L305)）判断哪一道先拦住请求；
- file 发现后端的租约语义（u1-l2：TTL 10 秒，死亡注册至多 10 秒消失）——记录从击杀到 frontend 稳定报错隔了多久。

**第 3 步：恢复 decode**。用原命令重拉 D，持续 curl，记录从重启到请求恢复成功的时间，并与第 2 步的 10 秒对照。

**第 4 步：只杀 prefill（P）**。重复观察：D 的卡片 `needs=[[Prefill]]`，P 死后模型是否同样不就绪？特别注意：即便生命周期门会把请求透传给下游（L300-L305），D 的 `require_prefill_result`（[disagg.py:L52-L66](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/components/src/dynamo/common/backend/disagg.py#L52-L66)）理论上会报「no prefill_result」——**实际哪一道防线生效？**这是报告里最值得写清楚的问题（源码注释的答案是准入门在更早阶段把关，请以本地观察验证）。

**第 5 步：写报告**。一张表：击杀对象 × （失败现象 / 生效防线及源码行号 / 恢复方式 / 恢复耗时），加一段结论：为什么 P/D 分离拓扑的可用性是「成对可用」，以及这对部署（副本数规划）意味着什么。

**预期结果（供对照，具体报错文案待本地验证）**：任一角色死亡都应让新请求被拒（就绪门），区别在生效路径与日志特征；恢复都是「重启进程 + 等注册传播」，秒级完成。如果观察到与预期不符的行为（例如请求仍成功），那是更有价值的发现——回头核对 u4-l4 的就绪判定与本讲的生命周期门，找出真正的裁决顺序。

## 6. 本讲小结

- **分离的动机**是两阶段资源画像不同（prefill 计算密集、decode 访存密集），收益是延迟隔离与按阶段配硬件；但它不免费——低并发下聚合反而更优，容量账要算（Planner 管池间再平衡）。
- 分离式执行三步：prefill 算 KV → **KV 传输** → decode 生成；KV 字节走 NIXL 点对点直传（非阻塞、不经过 frontend），frontend 只搬运 `disaggregated_params` 句柄。
- 角色声明的两层：启动期 `--disaggregation-mode`（`DisaggregationMode`，`WorkerConfig` 上的唯一事实来源）在注册时被翻译成 `WorkerType` + DNF `needs`（prefill↔decode 双向依赖，成对才就绪）；`WorkerType` 规范定义在 `dynamo-kv-router` 以避免依赖环。
- **`disagg.sh` 拓扑** = frontend + `sample-prefill` + `sample-decode` 三个进程；sample 引擎 prefill 截 `max_tokens` 到 1 并在终止 chunk 盖合成句柄，decode 用 `require_prefill_result` 强制校验；`agg.sh` 只是把同一引擎跑在默认 `Aggregated` 模式。
- **`PrefillRouter` 是交接的编排者**：存原 `max_tokens` → 克隆截 1 派发 prefill → 三种结局（Terminal 直通 / Completed 注入 `prefill_result` / Bootstrap 注入 `bootstrap_info`）→ 恢复 `max_tokens`、decode 仅看负载 → 下游；取消不中止 decode 路由，防 KV 块泄漏。
- `disaggregated_params` 是**引擎自有**的不透明 JSON（vLLM/SGLang/TRT-LLM 各自定义格式），回传客户端前被剥掉。

## 7. 下一步学习建议

- **u7-l2 Prefill 路由与条件分离**：本讲跳过的 conditional disagg 分支——`PrefillRouter` 何时决定不走远端 prefill、直连 decode worker 本地完成 prefill+decode，以及 admission 准入控制。读 [prefill_router/mod.rs:L314-L370](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/kv_router/prefill_router/mod.rs#L314-L370) 前建议先回顾 u6-l2 的负载上下文。
- **u7-l3 KV 传输引擎**：本讲反复说「KV 字节不经过 frontend」，那它到底怎么走？`lib/llm/src/block_manager/block/transfer/` 下的 nixl.rs / nccl.rs / memcpy.rs 三种后端。
- **u8-l9 前端图像解码与 E/P/D 多模态分离**：本讲遇到的 `Encode` 角色与 E/P/D 拓扑（`needs=[[Prefill, Decode], [Aggregated]]`、`route_to_encoder`）的完整展开。
- 继续阅读建议：`lib/backend-common/src/disagg.rs` 与 `worker.rs` 的注册段是理解任何新后端接入分离拓扑的起点；官方文档 [disaggregated-serving.md](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/docs/fern/pages/developer-guide/knowledge-base/concepts/system-architecture/disaggregated-serving.md) 的 "Performance Characteristics" 三小节值得在跑过故障实验后重读一遍。
