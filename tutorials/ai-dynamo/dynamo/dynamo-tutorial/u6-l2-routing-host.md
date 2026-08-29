# u6-l2 Rust 路由核心:routing_host 与调度

## 1. 本讲目标

学完本讲,你应该能够:

1. 追踪一次路由决策从请求进入到选中 worker 的**完整调用链**,并能说出 KV 路径与 builtin(非 KV)路径在哪一步分叉。
2. 解释 **KV 重合度如何影响最终得分**:overlap 不是"加分项",而是从负载中扣除的"信用"(credit),理解 `logit` 成本模型的每一项。
3. 说出 **KvScheduler 在路由侧的作用**:准入(能否现在跑)、排队(策略类队列与 DRR)、记账(负载投影与队列指标),并澄清它与引擎侧 dynamic batching 的区别。
4. 学会打开路由器的调试日志,把一条请求在每个候选 worker 上的得分整理成表格。

## 2. 前置知识

本讲承接 u6-l1(路由概览与 `dynamo.router` 独立进程),并用到 u3-l4(PushRouter)与 u4-l3(块哈希)的结论。开始前请确认理解以下概念:

- **RouterMode 的七种模式**:`round_robin`、`random`、`power_of_two_choices`、`least_loaded`、`kv`、`direct`、`device_aware_weighted`。u6-l1 讲过它们由 Python 的 `ROUTER_MODE_MAP` 映射为 Rust 枚举。
- **PushRouter**:u3-l4 讲过,它拥有服务发现、故障检测与传输,负责"把请求真的发出去"。本讲的 RoutingHost 站在它之上。
- **块哈希**:u4-l3 讲过"相同前缀 ⇒ 相同前缀块哈希",这是本讲 KV 重合度计算的数据来源。同时记得 u1-l2 的结论:**sample 后端每条请求生成全新块哈希,相同前缀不会命中**;要做 KV 重合实验必须用 mocker 等会发布真实 KV 事件的后端。
- **成本模型(cost/logit)**:本讲的"得分"是**成本**,越小越好。这与直觉中的"分数越高越好"相反,初次阅读时务必留意。

再引入四个本讲新术语:

| 术语 | 含义 |
|------|------|
| 选择器(WorkerSelector) | 输入 worker 集合与请求上下文,输出一个选中的 worker。策略的载体。 |
| 挑选器(Picker) | 选择器内部最后"从候选里挑一个"的那一步,如取最小成本、softmax 采样。 |
| 占用(occupancy) | 路由器侧为每个 worker 维护的"在途请求数"计数器,供 `least_loaded`/`power_of_two_choices` 等负载感知策略使用。 |
| 准入(admission) | 请求进入调度队列前的一道闸门:判断容量、登记记账,决定"立即调度"还是"排队等待"。 |

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [lib/llm/src/kv_router/routing_host.rs](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/routing_host.rs) | `RoutingHost` 结构体、`RoutingPolicy` 策略枚举、`generate` 引擎入口 |
| [lib/llm/src/kv_router/routing_host/builtin.rs](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/routing_host/builtin.rs) | builtin 路径全实现:`BuiltinWorkerSelector`、内置四策略、LoRA 过滤、四种分发分支 |
| [lib/llm/src/kv_router/routing_host/kv.rs](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/routing_host/kv.rs) | KV 路径三步:选择(select)、记账(track)、分发(dispatch) |
| [lib/llm/src/kv_router/routing_host/kv_selection.rs](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/routing_host/kv_selection.rs) | KV 选点决策:显式 pin / 会话亲和 / best-match 三种来源的合并 |
| [lib/llm/src/kv_router/routing_host/occupancy.rs](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/routing_host/occupancy.rs) | `HostedOccupancy`:占用账本的"选择并预订"原子操作 |
| [lib/llm/src/kv_router/scheduler.rs](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/scheduler.rs) | `KvScheduler`:`lib/llm` 对 `lib/kv-router` LocalScheduler 的包装,负责装配与指标 |
| [lib/llm/src/kv_router.rs](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router.rs) | `KvRouter`:打分入口 `find_best_match_*` 所在,连接 indexer 与 scheduler |
| [lib/kv-router/src/scheduling/selector/default.rs](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/kv-router/src/scheduling/selector/default.rs) | **打分公式本体**:`DefaultWorkerSelector` 与 `worker_logit` |
| [lib/runtime/src/pipeline/network/egress/push_router.rs](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/runtime/src/pipeline/network/egress/push_router.rs) | `RouterMode` 枚举定义与 `requires_occupancy` 判定 |

一句话导航:**routing_host 管生命周期,builtin.rs 与 kv.rs 是两条平行的选点路径,default.rs 算分,scheduler.rs 排队记账**。

## 4. 核心概念与源码讲解

### 4.1 RoutingHost:路由生命周期的统一所有者

#### 4.1.1 概念说明

u6-l1 讲过三种路由部署形态(frontend 内嵌 Rust 路由、Python processor、独立 `dynamo.router` 进程)。本讲进入它们的**共同底座**——`RoutingHost`。

`RoutingHost` 的职责边界在源码文档注释里写得很清楚:三方分工。

- `PushRouter`(u3-l4)拥有:服务发现、故障检测、传输;
- `KvRouter` 拥有:可选的 KV 候选状态(indexer);
- `RoutingHost` 拥有:**不论哪个策略选中了 worker,请求生命周期的公共部分**——选点、分发、响应流监控、失败时的清理。

这就是"Host"(宿主)一词的含义:策略可以换(轮询、随机、KV……),但"从选点到响应收尾"这条流水线只有一条。

#### 4.1.2 核心流程

`RoutingHost` 内部用一个私有枚举 `RoutingPolicy` 表达"当前挂的是哪种策略",四个变体对应四类路径:

```text
RoutingPolicy
├── Kv(Arc<KvRouter>)          → KV 感知路径(kv.rs + kv_selection.rs)
├── Builtin(BuiltinWorkerSelector) → round_robin / random / power_of_two / least_loaded
├── Direct                      → 显式指定 worker,路由器不做选择
└── DeviceAwareWeighted         → 异构设备(CPU/GPU)加权路由
```

一次 `generate` 调用的顶层流程:

```text
generate(request)
  ├─ 策略是 Kv? ──否──→ select_and_dispatch_builtin(按请求阶段 phase)
  │                        (builtin.rs 的完整路径,见 4.2)
  └─ 是
      ├─ 请求带 query_instance_id 注解? ──是──→ 只返回最佳 worker,不路由、不更新状态
      ├─ select_with_affinity → KV 选点(见 4.3)
      ├─ track_selection → 把路由决策写回 indexer(记账,见 4.3.3)
      └─ dispatch_selection → 经 PushRouter 发给 worker,响应流套上监控(见 4.3.3)
```

注意 query-only 模式:u6-l1 讲过独立路由器的 `best_worker_id` 端点"纯查询不记账",它的 Rust 端实现就在这里——`query_instance_id` 注解让选点走 `select_without_admission`,返回的响应里带上 `routing_data`(worker 信息 + token),请求本身不会被发送。

#### 4.1.3 源码精读

先看类型定义。`RoutingPolicy` 枚举与 `RoutingHost` 结构体:

- [lib/llm/src/kv_router/routing_host.rs:146-154](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/routing_host.rs#L146-L154) — 四变体策略枚举 `RoutingPolicy`,这是理解全文件的总纲。
- [lib/llm/src/kv_router/routing_host.rs:183-198](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/routing_host.rs#L183-L198) — `RoutingHost` 结构体:字段恰好对应职责三分——`inner`(PushRouter)、`policy`(策略)、`request_metrics`(请求指标)、`affinity`(可选会话亲和协调器)、`hosted_occupancy`(可选占用账本)、`lora`(可选 LoRA 过滤)。
- [lib/llm/src/kv_router/routing_host.rs:200-204](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/routing_host.rs#L200-L204) — 类型别名 `KvPushRouter = RoutingHost`,兼容旧调用方的名字,1.x 系列保留。

再看两个构造函数如何决定策略:

- [lib/llm/src/kv_router/routing_host.rs:210-220](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/routing_host.rs#L210-L220) — KV 版构造 `new`:接收现成的 `Arc<KvRouter>`,策略固定为 `RoutingPolicy::Kv`。frontend 内嵌 KV 路由走这里。
- [lib/llm/src/kv_router/routing_host.rs:257-284](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/routing_host.rs#L257-L284) — builtin 版构造 `new_builtin_with_capabilities` 的核心:按 `inner.router_mode()` 匹配策略(`Direct`/`DeviceAwareWeighted` 直取,其余四种由 `BuiltinWorkerSelector::new` 接管,失败说明该模式不是一方策略);同时用 `required_worker_inputs()` 声明本策略需要哪些输入(如占用),需要占用的策略会在此处创建 `HostedOccupancy`。

最后是引擎入口。`RoutingHost` 实现了 `AsyncEngine` trait(u3-l3 讲过的引擎抽象),所以它能直接挂在请求面上:

- [lib/llm/src/kv_router/routing_host.rs:493-530](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/routing_host.rs#L493-L530) — `generate` 的文档注释详尽描述了三种行为(query-only / 精确 pin / 常规 KV 选点),代码 L520-529 是非 KV 策略的快捷出口:按请求的 `phase`(Prefill/Decode/Aggregated)直接进 builtin 路径。
- [lib/llm/src/kv_router/routing_host.rs:532-586](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/routing_host.rs#L532-L586) — query-only 分支:选点后不 dispatch,而是把 worker 信息塞进 `LLMEngineOutput::routing_data`,用单元素流返回;同时记录 KV 命中、ISL、队列深度等指标。这就是独立路由器 `best_worker_id` 的底层实现。
- [lib/llm/src/kv_router/routing_host.rs:588-610](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/routing_host.rs#L588-L610) — KV 常规分支的三步曲:`track_selection`(记账)→ `dispatch_selection`(分发)→ 若有会话亲和操作则 `operation.into_stream` 把亲和绑定与响应流绑定。

响应流的收尾监控也在这里:

- [lib/llm/src/kv_router/routing_host.rs:79-128](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/routing_host.rs#L79-L128) — `monitor_response_stream`:用 `tokio::select!` 同时盯"客户端取消"(`context.stopped()`)与"响应流下一帧";流正常结束调 `guard.finish()`(释放记账),取消或失败调 `guard.abort()`。这是"选点—分发—清理"生命周期的最后一环。

#### 4.1.4 代码实践

**实践目标**:不改任何代码,验证 `RoutingHost` 的策略匹配逻辑与测试覆盖。

1. 打开 [lib/llm/src/kv_router/routing_host/tests.rs:74-86](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/routing_host/tests.rs#L74-L86),阅读 `builtin_policies_declare_capabilities` 与 `builtin_host_constructs_only_declared_capabilities` 两个测试。
2. 运行:`cargo test -p dynamo-llm routing_host::tests::builtin`。
3. 观察哪些策略声明了 `OCCUPANCY` 能力、哪些没有;测试如何断言"构造出的 host 只包含声明的能力"。

**预期结果**:测试全部通过;你会发现 `power_of_two_choices` 与 `least_loaded` 声明了占用输入,而 `round_robin`/`random` 没有——这正是 4.2 节的伏笔。(运行结果待本地验证,本环境未执行。)

#### 4.1.5 小练习与答案

**练习 1**:`RoutingHost` 为什么要把策略做成枚举字段,而不是为每种模式写一个独立的引擎类型?

**答案**:因为选点只是差异点,而"分发、取消传播、失败清理、指标记录、会话亲和绑定"这些生命周期环节对所有策略完全相同。枚举字段让公共路径只写一遍(`select_and_dispatch_builtin`/`select_and_dispatch_kv_prefill` 两个入口),新增策略只需增加一个枚举变体与对应的选点实现,不碰生命周期代码。

**练习 2**:`kv_router_if_enabled()` 为什么返回 `Option`,而 `kv_router()` 直接 `expect`?

**答案**:见 [routing_host.rs:333-345](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/routing_host.rs#L333-L345):只有 `RoutingPolicy::Kv` 变体持有 `KvRouter`,builtin/Direct/DeviceAwareWeighted 都没有。调用方若不确定策略类型应先用 `kv_router_if_enabled()` 判空;`kv_router()` 仅供确知在 KV 路径上的代码使用,误用即 panic,把编程错误尽早暴露。

### 4.2 builtin 策略:四种内置选点器与占用感知选择

#### 4.2.1 概念说明

builtin.rs 承载 `round_robin`、`random`、`power_of_two_choices`、`least_loaded` 四种"一方策略"。它们共同点是**不看 KV 缓存**——选点依据只有 worker 列表(和可选的占用计数),因此 `selection()` 构造的结果里 overlap 全是 0。

四种策略的差异:

| 策略 | 决策依据 | 需要占用输入? |
|------|----------|----------------|
| round_robin | 轮转指针 | 否 |
| random | 随机 | 否 |
| power_of_two_choices | 随机抽两个,选占用低的那个 | 是 |
| least_loaded | 全体候选中占用最低者 | 是 |

"占用"由 `HostedOccupancy`(occupancy.rs)管理:它在 `RoutingOccupancyState` 上提供"**选择并预订**"(`select_and_reserve`)的原子操作——读取占用、选点、给选中 worker 加一,三步一次完成,避免并发请求看到过期计数。预订返回一个 RAII 式的 `OccupancyReservation`,随响应流结束释放(u3-l4 讲过的"选择与占用记账原子耦合"在落地处)。

#### 4.2.2 核心流程

builtin 路径完整流程(`select_and_dispatch_builtin`):

```text
select_and_dispatch_builtin(request, phase)
  1. 显式目标检查:direct 模式无显式 target → 400
  2. LoRA 过滤:请求带 lora_name → 先按 LoRA 分配缩小候选集
  3. 会话亲和包装:有 session → acquire 一个亲和目标(可作为 target_constraint)
  4. select_hosted_worker(按策略四分支):
       Direct            → 直接用 target
       Builtin+OCCUPANCY → occupancy.select_and_reserve(原子选择+预订)
       Builtin 无占用     → selector.select_worker(纯函数选点)
       DeviceAwareWeighted→ select_device_aware_and_reserve
  5. 创建 RequestGuard(生命周期记账)
  6. 四种 dispatch 分支(按约束强度):
       direct + 无亲和    → direct_within_prepared(允许 fallback)
       有 target_constraint → dispatch_exact(精确发送)
       uses_occupancy      → dispatch_preselected_prepared(占用策略专用)
       其余                → direct_within_prepared(普通直发)
  7. retarget guard、记 tracker、输出 "Selected worker" 日志
  8. 响应流套监控,亲和操作绑定流
```

#### 4.2.3 源码精读

- [lib/llm/src/kv_router/routing_host/builtin.rs:13-29](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/routing_host/builtin.rs#L13-L29) — `BuiltinWorkerSelector::new` 把 `RouterMode` 映射到 `BuiltinRoutePicker`(来自 dynamo-runtime):四种模式各取一个 picker,其余模式返回 `None`(表示"不是一方策略")。
- [lib/runtime/src/pipeline/network/egress/push_router.rs:189-199](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/runtime/src/pipeline/network/egress/push_router.rs#L189-L199) — `RouterMode` 枚举定义本体;[L279-284](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/runtime/src/pipeline/network/egress/push_router.rs#L279-L284) 的 `requires_occupancy` 明确 `power_of_two_choices | least_loaded | device_aware_weighted` 三种需要占用。
- [lib/llm/src/kv_router/routing_host/builtin.rs:62-84](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/routing_host/builtin.rs#L62-L84) — `WorkerSelector` trait 实现:`required_worker_inputs` 按模式声明能力;`select_worker` 取出(hosted 输入的)worker 列表与占用函数,交给 picker 选择,无候选则报 `NoEndpoints`。
- [lib/llm/src/kv_router/routing_host/builtin.rs:86-94](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/routing_host/builtin.rs#L86-L94) — `selection()`:builtin 选点的结果 overlap 全为 0——**KV 数据根本不进入 builtin 路径**,这是它与 KV 路径最本质的区别。
- [lib/llm/src/kv_router/routing_host/builtin.rs:168-261](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/routing_host/builtin.rs#L168-L261) — `select_hosted_worker` 四分支:Direct 要求显式 target;带 OCCUPANCY 的 builtin 走 `occupancy.select_and_reserve`(返回预订凭据);无占用的 builtin 在 `with_selectable_worker_ids` 闭包里纯函数选点;DeviceAwareWeighted 走 PushRouter 的设备感知选择并采集遥测(is_cpu、embedding_cache_hit 等)。
- [lib/llm/src/kv_router/routing_host/occupancy.rs:32-67](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/routing_host/occupancy.rs#L32-L67) — `select_and_reserve`:pin 的 worker 直接 `reserve`;否则把候选集与占用读取器一起交给 `RoutingOccupancyState`,由它在占用账本内部完成"读占用→选点→加一"的原子序列。
- [lib/llm/src/kv_router/routing_host/builtin.rs:263-332](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/routing_host/builtin.rs#L263-L332) — `select_and_dispatch_builtin` 前半:direct 模式缺 target 报 400、LoRA 候选过滤、会话亲和包装、`select_hosted_worker` 选点、`RequestGuard::new_builtin` 创建记账凭据。
- [lib/llm/src/kv_router/routing_host/builtin.rs:337-411](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/routing_host/builtin.rs#L337-L411) — 四个 dispatch 分支的完整实现。注意每个分支里都有 `guard.retarget_worker(worker_id)`:传输层可能在 fallback 时换了 worker(如首选不可达),guard 的记账目标必须跟着改。
- [lib/llm/src/kv_router/routing_host/builtin.rs:441-450](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/routing_host/builtin.rs#L441-L450) — `info` 级日志 `"Selected worker"`:带 `router_mode`、`worker_id`、`candidate_count`、`occupancy`、`transport_fallback` 字段。**这条日志默认级别即可看到**,是观察 builtin 路由行为最便宜的窗口。

#### 4.2.4 代码实践

**实践目标**:用内置单测验证四种策略的行为差异,特别是"power_of_two_choices 只读两次占用"这个性能特性。

1. 阅读 [lib/llm/src/kv_router/routing_host/builtin.rs:482-541](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/routing_host/builtin.rs#L482-L541) 的五个测试:`round_robin_uses_hosted_selector_input`(轮转顺序 10→20→10)、`random_uses_hosted_selector_input`、`occupancy_policies_require_lazy_occupancy_input`(缺占用输入报错)、`least_loaded_reads_hosted_occupancy`(选低占用者)、`power_of_two_choices_reads_only_two_occupancies`(用 `Cell` 计数证明占用读取器只被调用 2 次)。
2. 运行:`cargo test -p dynamo-llm routing_host::builtin`。
3. 对照 `least_loaded_reads_hosted_occupancy`:手工代入占用 {10: 4, 20: 1},验证选中的是 20。

**预期结果**:五个测试全部通过;`power_of_two_choices` 的占用读取次数断言为 2,说明它是为大规模集群设计的 O(1) 占用探测策略。(运行结果待本地验证。)

#### 4.2.5 小练习与答案

**练习 1**:为什么 `power_of_two_choices` 在几百个 worker 的集群里比 `least_loaded` 更实用?

**答案**:`least_loaded` 每次选点要遍历全部候选读占用(读 N 次);`power_of_two_choices` 只随机抽两个候选、读 2 次占用选较低者。经典"两种选择的力量"(power of two choices)结论:即便只比较两个随机样本,负载均衡效果也接近全局最优,而代价从 O(N) 降到 O(1)。内置测试用计数器证明读取恰为 2 次。

**练习 2**:builtin 路径的 `select_and_dispatch_builtin` 里,`dispatch_exact` 与 `direct_within_prepared` 有何区别?各在什么条件下使用?

**答案**:见 [builtin.rs:337-411](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/routing_host/builtin.rs#L337-L411)。`dispatch_exact` 用于存在 `target_constraint`(显式 pin 或会话亲和)时——必须发给指定 worker,不做 fallback;`direct_within_prepared` 用于自由选点场景,允许传输层在首选不可达时按 fallback 集合(`lora_fallback`)换目标。占用策略另有一条 `dispatch_preselected_prepared`,因为"已预订占用的选择"需要与传输协调释放。

### 4.3 KV 路径:从块哈希到 best match 的完整调用链

#### 4.3.1 概念说明

KV 路径是本讲的主菜。它与 builtin 路径的根本区别:选点依据多了一维——**每个 worker 的 KV 缓存里已经持有本请求多少前缀块**。

这一维数据从哪来?u6-l1 埋过伏笔,完整的链在下一讲(u6-l3 KV 事件流)展开,这里先记住结论:worker 的引擎在处理请求时会**发布 KV 事件**(哪些块存下了),路由器的 indexer 据此维护"哪个前缀缓存在哪个 worker"的索引(u6-l4 讲 radix tree 实现)。KV 路径选点时,拿请求的块哈希序列去 indexer 查一遍,得到每个候选 worker 的重合块数。

注意 `KvRouter` 的自我定位(源码注释):"A KvRouter only decides which worker you should use. It doesn't send you there."——选点与发送是分离的,发送由 RoutingHost 通过 PushRouter 完成。

#### 4.3.2 核心流程

KV 路径五步(全部在 kv.rs 与 kv_selection.rs 中):

```text
1. select_request(kv.rs)
   └─ 组装 RoutingRequestParts(token_ids + 多模态块信息)、policy_class、
      会话上下文,包上 tracing span,套上"客户端取消即中止"保护

2. select_worker(kv_selection.rs)
   ├─ 收集约束:显式 pin(routing hints 的 prefill/decode_worker_id)、
   │  LoRA、cache_namespace、优先级、迁移排除集、亲和 pin
   ├─ 无 pin → 走 KV best-match 打分选点
   └─ 有 pin → 解析 dp_rank,校验资格后仍进 best-match,
               但把 pinned_worker 传进去(打分退化为对单 worker 估值)

3. select_best_match → KvRouter::find_best_match_details_with_policy_class_inner
   ├─ compute_block_hash_for_seq:token → 块哈希(u4-l3 的链式哈希)
   ├─ query_tiered_matches:indexer 查询,得分层重合
   │   (device 显存 / host_pinned CPU / disk 三层,另有共享缓存命中)
   ├─ OverlapAnalysis:整理成重合信号
   └─ scheduler.schedule_request:进入调度器 → 选点 + 记账(4.4/4.5 讲)

4. track_selection(kv.rs)
   └─ 把"这条请求路由到了 worker W、带着这些块哈希"写回 indexer
      (路由决策记录,让"路由器预测缓存状态"模式成为可能),
      同时记录 KV 命中率、ISL、队列深度等指标

5. dispatch_selection(kv.rs)
   └─ 把 dp_rank、router_hint 写进请求 → dispatch_exact(精确)或
      direct(直发)→ 响应流套上监控
```

其中第 3 步打分细节、第 4 步的调度排队分别在 4.4 与 4.5 展开。

#### 4.3.3 源码精读

**第 1、2 步——选点入口与约束合并:**

- [lib/llm/src/kv_router/routing_host/kv.rs:10-41](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/routing_host/kv.rs#L10-L41) — `select_request`:取 context id、`policy-class` 元数据、agent 会话上下文,构造 `RoutingRequestParts`(token 与多模态块信息),在 `kv_router.select_worker` span 里调 `select_worker`,外层用 `cancel_on_stop` 保证客户端断连即中止选点。
- [lib/llm/src/kv_router/routing_host/kv_selection.rs:30-53](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/routing_host/kv_selection.rs#L30-L53) — 两个关键小结构:`WorkerSelection`(选点结果:worker、overlap、cached_tokens、routing_hashes、router_hint)与 `RoutingRequestParts`(请求侧输入)。
- [lib/llm/src/kv_router/routing_host/kv_selection.rs:136-183](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/routing_host/kv_selection.rs#L136-L183) — `select_worker` 开头:提取 routing hints 里的显式 pin、LoRA 名、优先级、迁移排除集;若在迁移重试中,把已失败的 worker 从候选里剔除,候选耗尽则直接报错。
- [lib/llm/src/kv_router/routing_host/kv_selection.rs:345-365](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/routing_host/kv_selection.rs#L345-L365) — `pinned_worker_hint`:按请求阶段取 pin 的来源——Prefill 用 `prefill_worker_id`,Decode/Aggregated 用 `decode_worker_id`,都缺省时回退 `backend_instance_id`。P/D 分离时这是 prefill→decode 交接的"地址条"。

**第 3 步——best match 主流程:**

- [lib/llm/src/kv_router/routing_host/kv_selection.rs:83-133](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/routing_host/kv_selection.rs#L83-L133) — `select_best_match`:调用 `find_best_match_details_with_policy_class_inner` 并携带 `WithAdmission{track_lifecycle: true}`(要进调度队列记账),把 `Routed` 结果转成 `WorkerSelection`。
- [lib/llm/src/kv_router.rs:1292-1302](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router.rs#L1292-L1302) — best-match 第一步:`compute_block_hash_for_seq` 把 token 切块算哈希(块大小、多模态信息、LoRA、eagle 都影响哈希域),随后 `log_routing_input_hashes` 在 debug 级输出 `[ROUTING_INPUT] request local hashes`——观察路由输入的第一个日志窗口。
- [lib/llm/src/kv_router.rs:1334-1370](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router.rs#L1334-L1370) — 第二步:`query_tiered_matches` 并行查本地 indexer 与可选的外部共享缓存,得到分层匹配;`OverlapAnalysis::new(...).signals()` 把原始匹配整理为重合信号。
- [lib/llm/src/kv_router.rs:1388-1437](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router.rs#L1388-L1437) — 第三步:组装 `ScheduleRequest`(含 overlap、优先级、pin、约束、共享缓存命中),按有无准入分别调 `schedule_request` 或 `select_without_admission`;队列被拒绝时返回 `QueueRejected`。
- [lib/llm/src/kv_router.rs:1487-1497](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router.rs#L1487-L1497) — 最终 `FindBestMatchOutcome::Routed`:返回 best worker、`effective_overlap_blocks`(浮点,四舍五入得 `overlap_blocks`)、cached_tokens 等。

**每请求一条的"路由结论"日志**(实践任务的关键观察点):

- [lib/llm/src/kv_router/routing_host/kv_selection.rs:242-262](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/routing_host/kv_selection.rs#L242-L262) — debug 日志 `[ROUTING] Best: worker_{id} dp_rank={rank} with {overlap}/{total} blocks overlap`,并附结构化字段(request_id/worker_id/dp_rank/overlap_blocks/total_blocks)。注释特别说明 `tests/utils/router_logs.py` 会解析这些字段——e2e 测试就是靠它验证路由行为的。
- [tests/utils/router_logs.py:15-18](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/tests/utils/router_logs.py#L15-L18) — 解析 `[ROUTING]...with X/Y blocks overlap` 的正则,可直接复用来写你自己的日志分析脚本。

**第 4、5 步——记账与分发:**

- [lib/llm/src/kv_router/routing_host/kv.rs:55-104](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/routing_host/kv.rs#L55-L104) — `track_selection` 的核心:若 indexer 支持记录路由决策,用 `TokensWithHashes` 重新(或复用 selection 携带的)计算块哈希与序列哈希,调 `record_routing_decision_hashes` 写回 indexer——这是"路由器自己预测缓存状态"(`use_kv_events=false`)模式的数据来源;写入失败仅告警不阻断请求。
- [lib/llm/src/kv_router/routing_host/kv.rs:129-147](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/routing_host/kv.rs#L129-L147) — `track_selection` 末段:向请求 tracker 记录 KV 命中(`record_kv_hit`,重叠块/总块数)、ISL、选中 worker、队列深度,并观察命中率直方图。
- [lib/llm/src/kv_router/routing_host/kv.rs:158-236](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/routing_host/kv.rs#L158-L236) — `dispatch_selection`:写入 `dp_rank`、剥离只给路由用的 `router_hint` 参数并按能力附加到后端请求;`exact=true` 走 `dispatch_exact`(P/D 交接必须精确),否则 `direct`;失败时先释放 guard 记账再返回错误,`kv_router.route_request` span 里带全 worker/overlap 上下文。

#### 4.3.4 代码实践

**实践目标**:静态追踪一条 KV 路由请求的调用链,标出每一步的源码位置与日志窗口。

1. 从 [routing_host.rs:588](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/routing_host.rs#L588) 的 `track_selection` 调用出发,依次打开 kv.rs、kv_selection.rs、kv_router.rs 中 4.3.3 列出的六处代码。
2. 在笔记里画一条竖线,从上到下标注:`generate → select_request → select_worker → (算块哈希 → 查 indexer → 重合分析 → schedule_request) → track_selection → dispatch_selection`。
3. 在每一步旁边标注它对应的日志:`[ROUTING_INPUT]`(kv_router.rs:486)、`Formula for worker_id=`(default.rs,4.4 节)、`[ROUTING] Best`(kv_selection.rs:248)、`kv_router.route_request` span(kv.rs:211)。

**预期结果**:得到一张"调用链 × 日志"对照表。这张表就是 4.4 与综合实践的基础——三处日志分别覆盖路由的输入、每个候选的得分、最终结论。

#### 4.3.5 小练习与答案

**练习 1**:为什么 `track_selection` 要把路由决策(块哈希)写回 indexer?KV 事件模式(`use_kv_events=true`)下这一步是多余的吗?

**答案**:两种数据来源并存:(a) KV 事件模式,worker 主动上报实际缓存的块,信息最准但有事件延迟;(b) 路由决策记录,路由器自己记"我把这个前缀发给过谁",据此预测该 worker 大概率会缓存它。即便在事件模式,决策记录也可在事件到达前做乐观预测;在 `use_kv_events=false`(测试或事件面不可用时)它是唯一的缓存状态来源。见 [kv.rs:77-127](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/routing_host/kv.rs#L77-L127)。

**练习 2**:`dispatch_selection` 里 `exact=true` 与 `exact=false` 分别对应什么场景?

**答案**:KV prefill 路径(`select_and_dispatch_kv_prefill`,kv.rs:306)恒传 `exact=true` 走 `dispatch_exact`——因为选点是带 dp_rank 的精确决策(P/D 分离时 prefill→decode 的交接地址不能被传输层悄悄换掉);decode 侧常规路径传 `exact=false` 走 `direct`,允许故障切换。

### 4.4 打分公式:KV 重合度如何变成成本

#### 4.4.1 概念说明

前面把请求送进了 `scheduler.schedule_request`,现在回答本讲第二个学习目标:**重合度到底怎么影响选点**。

打分逻辑在 `lib/kv-router` crate 的 `DefaultWorkerSelector`(这就是 `Sel = DefaultWorkerSelector` 类型参数的默认实参,scheduler.rs 里 re-export)。核心思想是**成本模型**:给每个候选 worker 算一个 `logit`(成本),越低越好;KV 重合度不是加分,而是**信用(credit),从该 worker 的待做工作量里扣除**——直觉是"这个 worker 已经替你算好了一部分 prefill,剩余工作量变小了"。

prefill 侧主公式(记号:`\(L_{prefill}\)` 为该 worker 的 prefill 积压块数,`\(L_{decode}\)` 为 decode 积压,`\(R\)` 为活跃请求数,`\(B_{\*}\)` 为各层重合块数):

\[
\text{logit} \;=\; \underbrace{s \cdot \max\!\bigl(0,\; L_{prefill} - C_{overlap}\bigr)}_{\text{调整后 prefill 成本}} \;+\; \underbrace{L_{decode}}_{\text{decode 积压}} \;+\; \underbrace{w_{req} \cdot R}_{\text{活跃请求代价}}
\]

其中重合信用 `\(C_{overlap}\)` 按缓存层加权:

\[
C_{overlap} \;=\; c_{eff} \cdot B_{device} \;+\; w_{host} \cdot B_{host\_pinned} \;+\; w_{disk} \cdot B_{disk} \;+\; m_{shared} \cdot B_{shared}
\]

- `\(B_{device}\)`:命中 worker 显存(GPU)的重合块,信用乘子 `\(c_{eff}\)` 默认 1.0;
- `\(B_{host\_pinned}\)` / `\(B_{disk}\)`:命中 CPU / 磁盘层的重合块,乘子默认 0.75 / 0.25——低层缓存"能省但没那么省",因为取回还需要搬运;
- `\(B_{shared}\)`:外部共享 KV 缓存命中(超出 device 命中的部分),乘子 `\(m_{shared}\)`;
- `\(max(0, \cdot)\)`:信用最多把 prefill 成本扣到 0,不会变负(4.4.3 源码注释解释了原因:下游的污点乘子假定非负)。

`\(c_{eff}\)` 还可以随该 worker 的相对负载衰减(防止"热缓存但已过载"的 worker 被反复选中):

\[
c_{eff} \;=\; \frac{c}{1 + \lambda \cdot \dfrac{(L_{prefill} - L_{min})/b}{Q}}
\]

`\(L_{min}\)` 是全体合格候选中最轻者的 prefill 积压,`\(b\)` 是块大小,`\(Q\)` 是本请求的块数,`\(\lambda\)`(`overlap_score_credit_decay`)默认 0 即不衰减。

**decode 池的特殊分支**:decode 路由器通常通过 per-request 覆盖把 `\(c\)` 置 0(纯负载均衡);但条件分离(conditional disagg,u7-l2)会保留正的 overlap credit,此时公式改为 `\(\text{logit} = \max(0,\; L_{decode} - C_{overlap}) + w_{req} \cdot R\)`——"缓存热的 decode worker 优先,但仍计 decode 积压"。

最后一步是**挑选**:

- 温度 `\(T = 0\)`(默认):确定性取最小 logit,平局随机打破;
- 温度 `\(T > 0\)`:按 softmax 概率采样,`\(T\)` 越大越随机——用于避免全局共振(所有请求涌向同一个"最优"worker)。

#### 4.4.2 核心流程

```text
DefaultWorkerSelector::select_worker(input)
  ├─ input.into_configured():取出 worker 配置表、调度请求、资格集、块大小
  ├─ select_worker_with_policy(...)
  │    ├─ 资格过滤:overloaded / allowed / pinned / 污点约束
  │    ├─ 对每个合格 worker 算 worker_logit(公式见上)
  │    └─ pick:
  │        T=0 → minimum_cost_index(平局随机)
  │        T>0 → softmax 采样
  └─ 返回 WorkerSelectionResult(worker + required_blocks + overlap + ...)
```

默认参数(Python 侧 CLI 默认值,环境变量同名加 `DYN_` 前缀):

| 参数 | CLI 标志 | 默认值 | 含义 |
|------|----------|--------|------|
| `overlap_score_credit` `\(c\)` | `--router-kv-overlap-score-credit` | 1.0 | device 重合信用乘子;置 0 = 忽略前缀匹配 |
| `overlap_score_credit_decay` `\(\lambda\)` | `--router-kv-overlap-score-credit-decay` | 0.0 | 负载上升时信用衰减率 |
| `prefill_load_scale` `\(s\)` | `--router-prefill-load-scale` | 1.0 | 扣除信用后对 prefill 成本的整体缩放 |
| `decode_active_request_weight` `\(w_{req}\)` | `--router-decode-active-request-weight` | 0.0 | 每个活跃请求折算的块代价 |
| `host_cache_hit_weight` `\(w_{host}\)` | `--router-host-cache-hit-weight` | 0.75 | CPU 层重合信用 |
| `disk_cache_hit_weight` `\(w_{disk}\)` | `--router-disk-cache-hit-weight` | 0.25 | 磁盘层重合信用 |
| `router_temperature` `\(T\)` | `--router-temperature` | 0.0 | softmax 采样温度 |

#### 4.4.3 源码精读

- [lib/kv-router/src/scheduling/selector/default.rs:93-102](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/kv-router/src/scheduling/selector/default.rs#L93-L102) — `DefaultWorkerSelector` 结构(注释指明"对应 Python 的 _cost_function")与内部 `DefaultWorkerScorer`。打分(scorer)与挑选(picker)是分开的两个组件。
- [lib/kv-router/src/scheduling/selector/default.rs:198-220](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/kv-router/src/scheduling/selector/default.rs#L198-L220) — `selection_weights`:把全局配置与 per-request 覆盖(`RouterConfigOverride`,请求可自带权重)合并成 `LogitWeights`。这就是"conditional disagg 可以按请求保留 overlap credit"的机制入口。
- [lib/kv-router/src/scheduling/selector/default.rs:273-311](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/kv-router/src/scheduling/selector/default.rs#L273-L311) — `worker_logit` 前半:取出各层重合块,计算 `shared_overlap_blocks` 与负载衰减 `overlap_credit_decay`(L294-306 的注释:"把超出最轻者的积压按本请求大小归一化,有理衰减在缓存局部性与 prefill 均衡之间软权衡"),合成 `overlap_credit_blocks`——即公式里的 `\(C_{overlap}\)`。
- [lib/kv-router/src/scheduling/selector/default.rs:313-343](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/kv-router/src/scheduling/selector/default.rs#L313-L343) — decode 特殊分支:`max(0, decode_cost - overlap_credit) + active_request_cost`,clamp 原因写在注释里(污点乘子假定非负)。**L333-341 的 debug 日志把整条算式连同每个数值打出来**。
- [lib/kv-router/src/scheduling/selector/default.rs:345-347](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/kv-router/src/scheduling/selector/default.rs#L345-L347) — prefill 主公式本体,三行对应公式的三大项。
- [lib/kv-router/src/scheduling/selector/default.rs:349-386](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/kv-router/src/scheduling/selector/default.rs#L349-L386) — **本讲实践的钥匙**:`Formula for worker_id=... with X effective cached blocks: logit = ...` 的 debug 日志,按是否命中共享缓存分两种格式,把 logit 分解式逐项打印;注释解释了为什么要在宏内手动盖 request_id/worker_type 戳(这些行从 SchedulerQueueActor 任务发出,日志层无法附加上下文)。
- [lib/kv-router/src/scheduling/selector/default.rs:418-439](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/kv-router/src/scheduling/selector/default.rs#L418-L439) — `minimum_cost_index`:T=0 的挑选,平局时用随机替换(`tie_count` 递增,`1/tie_count` 概率保留新者)——这是"确定性但不偏心"的经典蓄水池式平局打破。
- [lib/kv-router/src/scheduling/selector/default.rs:513-553](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/kv-router/src/scheduling/selector/default.rs#L513-L553) — `pick_default_worker` 的两条挑选路径:T=0 遍历取最小;T>0 收集全部 (worker, cost) 后 softmax 采样(L41-90 的 `softmax_sample_index` 实现了数值稳定的归一化采样)。
- [lib/kv-router/src/scheduling/selector/default.rs:611-632](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/kv-router/src/scheduling/selector/default.rs#L611-L632) — trait 实现:`required_worker_inputs` 声明 `CACHE | LOAD`(它需要缓存重合与负载两类输入,对比 builtin 策略的 `NONE`/`OCCUPANCY`),`select_worker` 委托给 `select_worker_with_policy`。
- 参数默认值的 Python 侧出处:[components/src/dynamo/common/configuration/groups/kv_router_args.py:305-317](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/components/src/dynamo/common/configuration/groups/kv_router_args.py#L305-L317)(overlap credit 默认 1.0)、[L365-391](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/components/src/dynamo/common/configuration/groups/kv_router_args.py#L365-L391)(host 0.75 / disk 0.25)、[L392-402](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/components/src/dynamo/common/configuration/groups/kv_router_args.py#L392-L402)(温度默认 0.0)。Rust 侧字段定义见 [lib/kv-router/src/scheduling/config.rs:703-731](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/kv-router/src/scheduling/config.rs#L703-L731) 与 [L79-104](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/kv-router/src/scheduling/config.rs#L79-L104) 的默认函数。

**用官方单测验证公式**(强烈建议亲手代入一遍):

- [lib/kv-router/src/scheduling/selector/default.rs:1382-1468](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/kv-router/src/scheduling/selector/default.rs#L1382-L1468) — `test_shared_cache_hits_scoring`:请求 4 块,worker0 device 命中 2、共享缓存再多 2(`\(m_{shared}=0.5\)`),手算 logit = 1×(4−2−0.5×2) = 1.0;worker1 无 device 命中、共享 4 块,logit = 4−2 = 2.0;worker0 胜。测试注释把算式写全了,是最佳的手算教材。
- [lib/kv-router/src/scheduling/selector/default.rs:1546-1615](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/kv-router/src/scheduling/selector/default.rs#L1546-L1615) — `test_overlap_credit_above_one_can_prefer_colocated_worker`:信用乘子从 1.0 提到 1.5,选择从冷 worker 翻转到热 worker——直观展示 `\(c\)` 的杠杆作用。
- [lib/kv-router/src/scheduling/selector/default.rs:1704-1773](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/kv-router/src/scheduling/selector/default.rs#L1704-L1773) — `test_overlap_credit_decay_can_prefer_less_loaded_cold_worker`:衰减开启后,"缓存热但积压重"的 worker 输给"缓存冷但空闲"的 worker。

#### 4.4.4 代码实践

**实践目标**:不开服务、不装 GPU,用单元测试亲手验证打分公式。

1. 运行 `cargo test -p dynamo-kv-router selector::default`。
2. 打开 `test_shared_cache_hits_scoring`([default.rs:1382-1468](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/kv-router/src/scheduling/selector/default.rs#L1382-L1468)),按 4.4.1 的公式手算两个 worker 的 logit,与断言比对。
3. 把测试里 `shared_cache_multiplier` 从 0.5 改成 1.0(只改这一处),重跑,观察断言是否仍然通过、为什么(提示:两边 logit 都变小,但排序是否变?)。改完记得还原——本讲不改源码入库。

**预期结果**:第 2 步手算 1.0 与 2.0,与测试注释一致;第 3 步 worker0 变为 4−2−2=0、worker1 变为 4−4=0,两者打平,平局随机打破——断言"选中 worker0"可能开始偶发失败,这正好让你体会 T=0 平局随机打破的真实含义。(第 3 步的结论待本地验证。)

#### 4.4.5 小练习与答案

**练习 1**:把 `overlap_score_credit` 设为 0 会发生什么?这与 `round_robin` 模式等价吗?

**答案**:置 0 后 `\(C_{overlap}=0\)`,logit 退化为纯负载成本(`\(s \cdot L_{prefill} + L_{decode} + w_{req}R\)`),即"忽略前缀匹配的负载均衡路由"。但它仍走 KV 调度路径(准入、排队、生命周期记账都在),与 `round_robin` 不等价:后者不进 KV scheduler、无队列、无 active-sequence 跟踪。一个是"看负载的 KV 路由",一个是"无状态轮询"。

**练习 2**:为什么 host/disk 层的重合信用(0.75/0.25)比 device 层(1.0)低?

**答案**:device 层命中意味着 KV 已在该 worker 的显存里,直接可用,省掉全部 prefill 计算;host/disk 层命中只说明数据" nearby"——worker 仍需把块搬回显存(u9 的 KVBM 分层),省的是重算而非搬运。信用权重近似"省下工作量占整块 prefill 成本的比例",所以逐层递减。

**练习 3**:温度 `\(T>0\)` 时 softmax 采样的输入是 logit 本身,logit 越小被采中概率越高(成本取负指数)。这解决了什么问题?

**答案**:确定性 argmin 在全体路由器/请求间会产生"羊群效应":同一时刻所有请求都算出同一个最优 worker,瞬间把它打满。温度采样让次优 worker 以合理概率分担流量,`\(T\)` 控制探索强度;源码用"先归一化到 min/max 范围再指数化"保证数值稳定(default.rs L41-90)。

### 4.5 KvScheduler:准入、排队与队列指标

#### 4.5.1 概念说明

本讲第三个学习目标是 scheduler 的作用。先澄清一个容易误解的点:**lib/llm 的 `KvScheduler` 不做引擎侧的 dynamic batching**(把多个请求拼成一个 batch 交给 GPU 那种,那是 vLLM/SGLang 引擎内部的调度)。它在路由侧做三件事,引自 scheduling 模块自己的文档:"**决定请求现在能不能跑、发给哪个 worker、运行期间如何记账**":

1. **准入(admission)**:请求到来时判断容量——立即调度,还是进策略类队列排队;队列出超限直接拒绝(对应 HTTP 503 语义,u4-l2 讲过 `InflightPermit` 与 Ready→Draining 的关系)。
2. **排队与调度**:`lib/kv-router` 的 `LocalScheduler` 内部是"一个 SchedulerQueueActor 拥有一个 PolicyQueue,后者按策略类(latency/agents/batch 等)各建一个队列,用 **DRR(赤字轮转)** 在类间公平分配派发机会"。这就是路由侧的"动态批处理"准确含义:**负载投影(potential load)与多类排队的动态管理**,不是 token 拼批。
3. **记账**:active-sequence 跟踪(哪些请求在哪个 worker 上、prefill 完成没)、队列指标导出。

`lib/llm/src/kv_router/scheduler.rs` 里的 `KvScheduler` 是对 `LocalScheduler` 的**薄包装**:补上与 lib/llm 世界的接线(多 worker 序列发布器、运行时配置监听、指标句柄)并把 `lib/kv-router` 的类型 re-export 出来。真正的排队/打分在 `lib/kv-router` 里(其中打分已在 4.4 讲完)。

#### 4.5.2 核心流程

`KvScheduler::start` 的装配流程:

```text
start(endpoint, block_size, workers_with_configs, selector, config, ...)
  1. create_multi_worker_sequences:
     为每个已发现 worker 建立"活跃序列"跟踪(向 worker 的序列端点
     注册,router_replica_sync 决定多副本行为),拿到 slots
  2. watch_worker_configs = true:持续监听 worker 配置变化,
     晚加入的 worker 也能进入路由(skip_initial_worker_wait 只影响启动阻塞)
  3. 解析 policy_profile(策略类列表,来自 router_policy_config)
  4. LocalScheduler::new_with_policy_profile(...):
     装配队列(PolicyQueue + 各类队列)、选择器、prefill 负载估计器、
     overlap 刷新器、过载/可用 worker 提供者
  5. prefill 池额外安装 NonMaxOverlapSelectionObserver:
     每次选中"非最高重合"的 worker 时发 debug 日志 + 指标
  6. 启动后台任务:订阅队列变更 + 60s 定时,把各队列的
     pending_requests / pending_isl_tokens / pending_cached_tokens
     刷到 Prometheus 指标
```

运行期的两个入口:

- `schedule_request`(带准入):立即调度或排队,返回 `SchedulingResponse`(含 best_worker、effective_overlap_blocks、cached_tokens);
- `select_without_admission`(纯查询):u6-l1 的 `best_worker_id` / query-only 路径用,**不占容量、不改状态**。

#### 4.5.3 源码精读

- [lib/llm/src/kv_router/scheduler.rs:37-45](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/scheduler.rs#L37-L45) — `KvScheduler` 结构:内部一个 `Arc<LocalScheduler<...>>` 加两组指标句柄(每个策略类一组)。泛型参数 `Sel`(选择器)与 `RF`(overlap 刷新器)都带默认值。
- [lib/llm/src/kv_router/scheduler.rs:69-87](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/scheduler.rs#L69-L87) — `start` 开头:快照初始 worker 配置、`create_multi_worker_sequences` 建立序列槽位;L86-87 的注释是关键设计:**`skip_initial_worker_wait` 只控制启动阻塞,调度器必须持续监听配置变化**,晚加入的 worker 才能进路由——内置单测正是验证这一点。
- [lib/llm/src/kv_router/scheduler.rs:89-121](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/scheduler.rs#L89-L121) — 解析 policy profile、按类建指标句柄、构造 `LocalScheduler::new_with_policy_profile`(入参一长串:槽位、配置监听、profile、块大小、选择器、prefill 负载估计器、overlap 刷新、过载/可用 provider、重查间隔、取消令牌)。
- [lib/llm/src/kv_router/scheduler.rs:122-147](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/scheduler.rs#L122-L147) — prefill 池的 `NonMaxOverlapSelectionObserver`:当选择偏离最高重合 worker 时,debug 日志打出"选中的 vs 最高的"两组 worker/重合数及 `overlap_blocks_lost`,并观察指标。这是诊断"为什么 KV 路由没选缓存最热的 worker"(通常因为负载)的直接工具。
- [lib/llm/src/kv_router/scheduler.rs:149-177](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/scheduler.rs#L149-L177) — 队列指标后台任务:`tokio::select!` 三路(取消/队列变更通知/60 秒 tick)刷新指标。
- [lib/llm/src/kv_router/scheduler.rs:186-193](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/scheduler.rs#L186-L193) — `schedule_request`:转发给 LocalScheduler 后调 `observe_schedule_result` 观察拒绝。
- [lib/llm/src/kv_router/scheduler.rs:333-353](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/scheduler.rs#L333-L353) — `observe_schedule_result`:按拒绝种类(请求数超限/原始 ISL token 超限/缓存 token 超限)递增对应计数器,随后刷新队列指标。
- [lib/llm/src/kv_router/scheduler.rs:356-361](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/scheduler.rs#L356-L361) — `select_without_admission`:纯查询入口,无准入无记账。
- [lib/llm/src/kv_router/scheduler.rs:371-392](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/scheduler.rs#L371-L392) — 生命周期记账 API:`mark_prefill_completed`(prefill 完成,decode 可接管)、`free`/`free_if_worker`(释放预订;后者带 worker 匹配条件,防止 ABA 竞态下释放别人的预订——这是 4.1 节 `RequestGuard` 清理路径的底层)。
- [lib/llm/src/kv_router/scheduler.rs:444-464](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/scheduler.rs#L444-L464) — `update_queue_metrics`:把 `ClassQueueStats`(pending 数/ISL token/缓存 token)写进各指标句柄。
- 队列内部架构(Actor/PolicyQueue/DRR/每类堆)的权威描述见模块文档 [lib/kv-router/src/scheduling/AGENTS.md](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/kv-router/src/scheduling/AGENTS.md),其中的时序图与 `PolicyClassQueue` 结构图是理解排队的最佳材料(内部实现主要在 `lib/kv-router/src/scheduling/queue*.rs`,深入留给 u6-l5 的策略框架)。

#### 4.5.4 代码实践

**实践目标**:验证"晚加入的 worker 能进入路由"这一调度器核心行为。

1. 阅读 [lib/llm/src/kv_router/scheduler.rs:516-575](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/scheduler.rs#L516-L575) 的测试 `skip_initial_worker_wait_still_monitors_worker_config_updates`:先以单 worker(且 `skip_initial_worker_wait: true`)启动调度器,再通过 `watch::channel` 发送双 worker 配置,循环轮询 `get_potential_loads` 直到 worker 1 出现,1 秒超时。
2. 运行:`cargo test -p dynamo-llm scheduler::tests`。
3. 思考:如果调度器只在启动时快照一次 worker 配置,这个测试会在哪一步挂起?

**预期结果**:测试通过,worker 1 在 1 秒内出现在 potential loads 中。这解释了生产中"先起 frontend 后扩 worker 副本"为什么可行——调度器靠 watch 通道跟上集群变化。(运行结果待本地验证。)

#### 4.5.5 小练习与答案

**练习 1**:为什么不把排队逻辑也塞进 `DefaultWorkerSelector`?

**答案**:模块纪律(见 scheduling 模块的 guardrails):**选择器必须无副作用**——不预留容量、不改队列、不迁移状态。选择器只回答"给这些候选和请求,选谁最划算";容量预留、队列变更、生命周期记账全部由 `SchedulerQueueActor::admit_one` 统一串行执行。这样打分可以被安全地重放(出队时用新鲜 overlap 重打分,即 overlap refresh)而不留脏状态。

**练习 2**:`free_if_worker(request_id, worker)` 相比 `free(request_id)` 多了什么保障?什么场景必须用它?

**答案**:释放预订前先核对"这条请求当前是否仍记在那个 worker 上",不匹配则不动(所有权不符 = 无操作)。请求失败重试/迁移时,旧 worker 的清理可能与新一次预定的建立交错:无条件 `free` 可能把新预定误释放(ABA 问题)。`RequestGuard` 在失败路径统一走 `free_if_worker`(见 [scheduler.rs:384-392](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/scheduler.rs#L384-L392))。

**练习 3**:`NonMaxOverlapSelectionObserver` 只装在 prefill 池(`worker_type == WORKER_TYPE_PREFILL`),为什么 decode 池不需要?

**答案**:decode 池路由默认把 overlap credit 置 0(4.4 的 decode 分支注释:"decode 路由器通常通过 per-request 覆盖强制 overlap_score_credit=0,保持纯负载路由")。decode 选点本来就不追求最大重合,记录"没选最高重合"没有诊断意义;prefill 池的重合偏离(通常因负载均衡牺牲局部性)才是值得观测的信号。

## 5. 综合实践

把本讲全部内容串起来:**让路由器自己告诉你它给每个候选 worker 打了多少分**。规格任务是"发送带相同前缀的一组请求,把每条请求的候选 worker 得分整理成表格"——得益于源码里现成的 debug 日志,不需要修改任何代码。

### 路径 A:本地日志实验(推荐,无 GPU 要求)

**准备**:按 u1-l2/u1-l4 完成 `ai-dynamo` 本地安装;准备两个终端窗口。**必须用 mocker 后端**(u1-l2 讲过 sample 后端每条请求生成全新块哈希,相同前缀不会产生 KV 命中,观察不到重合度变化;mocker 会发布真实 KV 事件,u6-l1 已验证)。

**步骤**:

1. **启动两个 mocker worker**(两个终端,或参考 [examples/backends/sample/launch/agg.sh](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/examples/backends/sample/launch/agg.sh) 的模板把 worker 行复制一份;mocker 的参数参照 [examples/backends/mocker/deploy/agg.yaml](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/examples/backends/mocker/deploy/agg.yaml) 中 decode 组件的 `python3 -m dynamo.mocker --model-name ...` 形式;本地缺省参数若报错,按报错补齐,如 planner profile 路径):

   ```bash
   DYN_DISCOVERY_BACKEND=file python3 -m dynamo.mocker --model-path <model> --model-name <model>
   ```

2. **启动 KV 路由的 frontend**,关键是 `--router-mode kv` 与 `DYN_LOG=debug`(日志级别开关见 [lib/runtime/src/config/environment_names.rs:26-35](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/runtime/src/config/environment_names.rs#L26-L35)):

   ```bash
   DYN_LOG=debug DYN_DISCOVERY_BACKEND=file \
     python3 -m dynamo.frontend --http-port 8000 --router-mode kv
   ```

3. **发送 5 条相同前缀请求**(共享一段 ~200 token 的系统提示,后缀各不同):

   ```bash
   COMMON="You are a careful assistant. <再补约 150 词固定文本> Now answer question"
   for q in "What is 2+2" "What is 3+3" "What is 4+4" "What is 5+5" "What is 6+6"; do
     curl -s http://localhost:8000/v1/completions -H 'Content-Type: application/json' \
       -d "{\"model\":\"$MODEL\",\"prompt\":\"$COMMON $q\",\"max_tokens\":8}" > /dev/null
   done
   ```

4. **从 frontend 日志抓三类事件**,整理成表格:

   | 日志事件 | 位置 | 说明 |
   |----------|------|------|
   | `[ROUTING_INPUT] request local hashes` | [kv_router.rs:486-493](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router.rs#L486-L493) | 请求的块哈希输入 |
   | `Formula for worker_id=...` | [default.rs:349-386](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/kv-router/src/scheduling/selector/default.rs#L349-L386) | **每个候选 worker 的 logit 分解式**(表格主数据) |
   | `[ROUTING] Best: worker_X ... blocks overlap` | [kv_selection.rs:248-259](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/routing_host/kv_selection.rs#L248-L259) | 最终选中的 worker 与重合块数 |

   按请求整理,每行一个请求,列为:请求序号、worker A 的 logit、worker B 的 logit、选中者、overlap/total。解析可直接借用 [tests/utils/router_logs.py:15-18](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/tests/utils/router_logs.py#L15-L18) 的正则。

5. **对照实验**:换 `--router-mode round-robin` 重跑,确认 `Formula` 日志消失(该模式不进 KV 打分),只剩 builtin 的 `Selected worker` info 日志([builtin.rs:441-450](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/routing_host/builtin.rs#L441-L450))。

**需要观察的现象**:

- 第 1 条请求:两个候选 logit 接近(缓存都冷),选中者由负载平局随机决定;
- 第 2 条起:前一条选中的 worker 对共同前缀出现 device 重合,`overlap_credit_blocks` 变大 → 其 logit 显著变小 → **后续请求大概率连续落在同一 worker**(缓存局部性);
- `[ROUTING] Best` 的 overlap 数应随前缀长度稳定(共同前缀块数不变)。

**预期结果**:得到一张 5 行的得分表,能清晰看到"重合度上升 → logit 下降 → 选中"的因果链;这正是 4.4 公式的实证版。若行为不符,优先检查后端是否真的发 KV 事件(第 2 条起 overlap 应非零;sample 后端会一直是 0)。

### 路径 B:无运行环境的静态替代

若本地无法启动服务:

1. 手算 4.4.4 的 `test_shared_cache_hits_scoring` 全部分支,写出两个 worker 的 logit 分解表;
2. 阅读 [default.rs:1382-1468](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/kv-router/src/scheduling/selector/default.rs#L1382-L1468) 与 [tests.rs:701](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/kv_router/routing_host/tests.rs#L701)(`router_request_counters_follow_admission_and_completion_lifecycle`,展示一次请求在指标上的完整生命周期),整理成"请求 → 候选得分 → 选中 → 记账"的纸面表格。所有运行结论标注"待本地验证"。

## 6. 本讲小结

- `RoutingHost` 是所有路由形态(内嵌/独立 router)的共同底座,用 `RoutingPolicy` 枚举挂载策略,统一拥有"选点 → 分发 → 响应监控 → 清理"的生命周期;`PushRouter` 管传输、`KvRouter` 管 KV 状态,三者职责分离。
- builtin 路径(`round_robin`/`random`/`power_of_two_choices`/`least_loaded`)完全不看 KV:选点结果 overlap 恒为 0;需要占用的两种策略经 `HostedOccupancy` 完成"选择并预订"的原子操作,凭据随响应流释放。
- KV 路径五步:取请求部件 → 合并 pin/亲和/迁移约束 → best-match(算块哈希 → 查 indexer 分层匹配 → 进调度器)→ 把路由决策写回 indexer → 精确/直发分发;query-only 注解让它退化为纯查询(u6-l1 `best_worker_id` 的底座)。
- 打分是**成本模型**:logit = prefill 负载(扣除各层重合信用)× 缩放 + decode 积压 + 活跃请求代价;重合信用按层加权(device 1.0 / host 0.75 / disk 0.25),可随相对负载衰减;温度 0 取最小、温度大于 0 走 softmax 采样。
- `KvScheduler` 是 `LocalScheduler` 的薄包装,做的是路由侧的**准入、策略类排队(DRR)与负载记账**,不是引擎侧 dynamic batching;选择器纪律要求其无副作用,容量预留统一在 `admit_one` 完成。
- 三处现成日志构成路由观测三件套:`[ROUTING_INPUT]`(输入哈希)、`Formula for worker_id=`(逐候选得分)、`[ROUTING] Best`(结论);`DYN_LOG=debug` 一并打开。

## 7. 下一步学习建议

- **u6-l3(KV 事件流)**:本讲的 indexer 重合数据从哪来——publisher、batching、ZMQ 线格式,补全"worker 如何告诉路由器自己缓存了什么"。
- **u6-l4(radix tree 与 KV 索引)**:本讲反复调用的 `query_tiered_matches` 背后的数据结构,以及并发基数树、近似 LRU 的取舍。
- **u6-l5(filter–score–pick 策略框架)**:本讲的 `DefaultWorkerSelector` 只是注册表里的默认策略;如何用 `WorkerSelectionPolicyRegistry` 写自己的三段式策略,并把 `Formula` 日志换成你自己的打分。
- 若想先看运维面:**u12-l1(可观测性)** 里本讲提到的队列指标(`pending_requests`/`pending_isl_tokens`)与 `NonMaxOverlapSelection` 指标如何在 Grafana 上呈现。
