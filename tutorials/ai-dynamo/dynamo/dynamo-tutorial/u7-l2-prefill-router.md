# u7-l2 Prefill 路由与条件分离

## 1. 本讲目标

上一讲（u7-l1）我们搭起了 P/D 分离的最小拓扑，知道了 frontend 里的 PrefillRouter 负责「先找 prefill worker 算一步，再把句柄塞进 decode 请求」。本讲钻进这台机器的内部，学完后你应该能够：

1. 逐段讲出 `PrefillRouter::generate` 对一条请求做了什么：截断、派发、验收、重组。
2. 解释 prefill 路由器为什么需要「激活（activation）」这一步，以及 prefill worker 的模型卡片如何反过来决定这一跳的路由方式。
3. 说出 prefill 响应被验收（admission）后的三种结局：Terminal / Handoff / Bootstrap，以及各自的判定条件。
4. 描述条件分离（conditional disagg）的三种策略（`isl_bounding` / `prefill_load` / `isl_or_load`）各自依据什么信号把请求「抄近道」送到 decode worker 本地跑，并动手调整参数让长短请求走不同路径。

## 2. 前置知识

- **Operator（算子）模式**：Dynamo 的请求面把处理链拆成一串可组合的算子，每个算子收到 `(请求, next)`，可以在把请求交给下游之前/之后做加工。你在 u3-l3 已经见过引擎抽象，这里只需把 PrefillRouter 理解为「链上的一个中间件」。
- **disaggregated_params**：引擎私有的不透明 JSON，是 prefill 与 decode 之间的 KV 移交句柄（u7-l1 已建立：控制面走路由器、KV 字节走 NIXL 直传）。本讲会反复看到它的提取与注入。
- **KV 重合度与有效 ISL**：ISL 是 Input Sequence Length（输入序列长度）。KV 路由（u6-l2/u6-l4）能给每个候选 worker 算出「这段 prompt 有多少 token 已经缓存在它那」。**有效 ISL**（eff_isl）= prompt 总 token 数减去该 worker 上的缓存抵扣，即「真正要新算的 prefill 量」。这是本讲条件分离策略的核心变量。
- **advisory（纯查询）选点**：u6-l2 讲过「选择并预订」的原子性——正常路由在选定 worker 时会在调度器里记账。而本讲的条件分离在「还没决定要不要走分离」时就要先偷看 decode 路由器会选谁，此时**不能记账**，否则正式派发时会重复计账。这种只查不记的调用叫 advisory。
- **负载上下文**：u6-l2 引入的 `RoutingLoadContext` 让每个路由宿主自持负载状态。本讲会看到 prefill 这一跳用 `RouterLoadSource::Prefill` 启动自己的负载上下文。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [lib/llm/src/kv_router/prefill_router/mod.rs](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/kv_router/prefill_router/mod.rs) | PrefillRouter 主体：`Operator` 实现、生命周期状态机、条件分离入口、decode 请求重组 |
| [lib/llm/src/kv_router/prefill_router/activation.rs](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/kv_router/prefill_router/activation.rs) | 激活机制：解析 prefill worker 卡片通告（advertisement）、后台 `drive_target` 循环、构建绑定 |
| [lib/llm/src/kv_router/prefill_router/admission.rs](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/kv_router/prefill_router/admission.rs) | prefill 响应流的「验收」：把流分类为 Terminal / Handoff / Bootstrap |
| [lib/llm/src/kv_router/prefill_router/conditional_bypass.rs](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/kv_router/prefill_router/conditional_bypass.rs) | 条件分离的路由侧执行：偷看 decode 路由器、组装决策输入、decode-busy 闸门 |
| [lib/kv-router/src/conditional_disagg.rs](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/kv-router/src/conditional_disagg.rs) | 条件分离策略族：trait 定义、三种内置策略、决策输入结构 |
| [lib/kv-router/src/scheduling/config.rs](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/kv-router/src/scheduling/config.rs) | `KvRouterConfig`：条件分离各字段的定义、默认值与 `DYN_ROUTER_*` 环境变量覆盖 |
| [lib/kv-router/src/scheduling/types.rs](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/kv-router/src/scheduling/types.rs) | `AdvisoryWorkerLoad`：prefill/decode 忙线判定的两个百分比公式 |
| [components/src/dynamo/common/configuration/groups/kv_router_args.py](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/components/src/dynamo/common/configuration/groups/kv_router_args.py) | Python 侧 CLI：`--router-conditional-disagg` 与 JSON 配置参数的解析与校验 |
| [components/src/dynamo/vllm/handlers.py](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/components/src/dynamo/vllm/handlers.py) | worker 侧消费点：decode worker 看到 bypass 注解后切换为本地聚合执行 |

## 4. 核心概念与源码讲解

本讲的四个最小模块：**prefill_router（主流程）**、**activation（激活）**、**admission（验收）**、**conditional_disagg（条件分离）**。

### 4.1 prefill_router：分离链路的中间站

#### 4.1.1 概念说明

u7-l1 里我们说「frontend 的 PrefillRouter 编排交接」，现在给出它的准确定位。源码文档注释写得很清楚：

> PrefillRouter is a forward-only operator that sits between Migration and the decode router. It optionally calls a prefill worker before routing to decode, extracting disaggregated_params from the prefill response and injecting them into the decode request.

拆开读：

- **forward-only（只前向）**：它不直接服务请求，而是加工后交给链上的下一个算子（参数名就叫 `next`，即 decode 路由）。
- **optionally（可选地）**：prefill worker 不存在（未激活/已失活）时它直接放行，请求按聚合路径走。准入的兜底不在这里——模型能否服务由 u4-l4 讲过的 worker 拓扑就绪门把守。
- **三种工作模式**（同一文档注释列出）：Query-only（带 `query_instance_id` 注解时只返回 worker ID 不执行）；Pre-routed（请求已带 `prefill_worker_id`/`decode_worker_id`，按指定 worker 路由，典型来源是 EPP 外部路由器）；Normal（由路由器按 KV 状态决定）。

它还有一个**三态生命周期**，这是理解「什么时候走分离、什么时候直连」的第一把钥匙：

```rust
enum PrefillLifecycleState {
    Pending = 0,     // 还没找到 prefill worker
    Active = 1,      // 绑定可用，执行分离流程
    Unavailable = 2, // prefill 集合消失（如全部死亡）
}
```

#### 4.1.2 核心流程

`PrefillRouter::generate` 对一条请求的完整处理流程：

```text
收到 (req, context)
  │
  ├─ 1. 剥掉客户端伪造的 bypass 注解（安全防线）
  ├─ 2. 记住原始 max_tokens
  ├─ 3. 生命周期 != Active ？ ──是──► 直接 next.generate(req)，结束
  ├─ 4. 条件分离策略启用？
  │      └─ 偷看 decode 路由器 + 策略判定
  │           ├─ 判定 bypass ──► 注入 bypass 注解，next.generate()，结束
  │           ├─ 判定 Err  ───► 告警，继续走分离（fail-open 到远端 prefill）
  │           └─ 判定 None ───► 继续
  ├─ 5. 建 tracker，进入 Prefill 阶段（信号量栅栏）
  ├─ 6. 克隆请求，max_tokens 截为 1
  ├─ 7. 组装 prefill 上下文（传播 first_response_guard、会话亲和）
  ├─ 8. select_and_dispatch_prefill：选 prefill worker 并派发
  │      ├─ 预取到 bootstrap 信息 ──► 后台任务排水 → Bootstrap 结局
  │      └─ 否则同步消费响应流 ──────► 验收分类（见 4.3）
  ├─ 9. Terminal 结局？──是──► 剥除 disaggregated_params 后直接回客户端
  ├─ 10. 组装 decode 请求：注入 bootstrap_info 或 prefill_result
  │       恢复原始 max_tokens；合并拓扑约束
  ├─ 11. 给 decode 路由写 override（overlap 记账清零等）
  └─ 12. next.generate(decode_req)
```

其中第 11 步值得单独记：decode 这一跳**不应该**再把 prompt 侧负载算进自己的选点（prefill 已经算过了），所以正常分离时强制 `overlap_score_credit = 0`，让 decode 选点只看负载；而条件分离路径保留基础路由的重合度抵扣（因为 KV 就在那个 decode worker 上，抵扣是真实的）。

#### 4.1.3 源码精读

先看结构体上真正决定行为的几个字段——[mod.rs:180-215](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/kv_router/prefill_router/mod.rs#L180-L215)：`decode_router` 是指向 decode 侧 KvRouter 的引用，专门给条件分离「偷看缓存热worker」用（非 KV 路由时为 `None`）；`conditional_disagg_policy` 是策略对象；两个 `*_busy_threshold` 是忙线阈值；`lifecycle` 是那个原子三态。绑定本身（`binding` 字段）用 `ArcSwapOption` 存放，激活时一次性换入——这是 u4-l4 讲过的「ArcSwap 一次发布目录」模式在同一层的复用。

generate 的入口做了一件容易忽略的安全事——[mod.rs:293-298](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/kv_router/prefill_router/mod.rs#L293-L298)：先把请求注解里的 `x-bypass-remote-prefill` 全部剥掉再进入策略。注释说明了原因：bypass 是**路由器自己的决定**，如果信任客户端传入的标记，任何客户端都能恶意（或无意）跳过远端 prefill。也就是说这个注解只能由路由器在内部写入，不能从外部注入。

生命周期短路在 [mod.rs:303-305](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/kv_router/prefill_router/mod.rs#L303-L305)：非 Active 一律 `next.generate` 放行。

「截断为一步 prefill」在 [mod.rs:381-382](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/kv_router/prefill_router/mod.rs#L381-L382)：克隆请求后 `prefill_req.stop_conditions.max_tokens = Some(1)`——prefill worker 只需要算出第一个 token 就算完成了上下文建立，decode 从那里接着生成。这就是 u7-l1 说的「prefill 请求截 max_tokens 为 1」的落点。

prefill 上下文的组装在 [mod.rs:391-400](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/kv_router/prefill_router/mod.rs#L391-L400)。注意 [mod.rs:394](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/kv_router/prefill_router/mod.rs#L394) 的 `propagate_first_response_guard(&context, &mut prefill_context)?`：这是 #12004 引入的传播——前端为多模态解码注册的 NIXL 内存必须存活到目标 worker 的**首个响应**（机制详见 u8-l9 的 FirstResponseGuard），prefill 这一跳要把守卫带过去。同时会话亲和 ID 也在这里插入上下文。

派发与结局分叉在 [mod.rs:416-458](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/kv_router/prefill_router/mod.rs#L416-L458)：`select_and_dispatch_prefill` 选中 worker 后，如果 `prepare_prefill_dispatch` 已经从 ModelManager 拿到了 bootstrap 地址（见下），就把 prefill 流交给后台任务排水、立刻带着 `Bootstrap` 结局去组 decode 请求——**prefill 与 decode 并发**，不等 KV 传完；否则同步消费整个 prefill 流做验收。

decode 请求的重组在 [mod.rs:518-555](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/kv_router/prefill_router/mod.rs#L518-L555)：三种结局分别注入 `bootstrap_info`（SGLang 式）或 `prefill_result`（含 disaggregated_params 与 prompt_tokens_details），都把 `prefill_worker_id` 写进 routing 提示，然后恢复原始 `max_tokens`，最后经 [mod.rs:549-553](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/kv_router/prefill_router/mod.rs#L549-L553) 写入 decode 路由 override 交给 `next`。override 的构造函数 [mod.rs:660-677](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/kv_router/prefill_router/mod.rs#L660-L677) 里能看到那三条：正常分离强制 `overlap_score_credit=Some(0.0)`、无条件 `assume_kv_reuse=false`、`track_prefill_tokens=false`。

还有一个容错细节值得指出——[mod.rs:496-507](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/kv_router/prefill_router/mod.rs#L496-L507)：prefill 完成后即使发现 context 已被取消/杀死，**decode 路由仍然继续**。注释引了 NVBugs 5969206：此时 KV 传输可能在途，若在这里中止，传输会变成「无接收者」而永久泄漏 KV 块；decode 侧的 `kv_transfer_complete_event` 守卫会在 KV 收到后做清理。这是一个「明知道请求死了也要把善后走完」的典型案例。

#### 4.1.4 代码实践（源码阅读型）

**目标**：把一条请求在 PrefillRouter 手里的字段变化整理成表，验证你对流程的理解。

**步骤**：

1. 打开 [mod.rs:279-557](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/kv_router/prefill_router/mod.rs#L279-L557)，从上到下读一遍 `generate`。
2. 画一张表，列为：`max_tokens`、`disaggregated_params`、`bootstrap_info` / `prefill_result`、`routing.prefill_worker_id`、`router_config_override`；行为三个时刻：进入 PrefillRouter 时 / 发给 prefill worker 时 / 发给 decode 路由（`next`）时。
3. 对照 [mod.rs:660-677](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/kv_router/prefill_router/mod.rs#L660-L677) 的单测 `decode_router_override_disables_overlap_and_prefill_tracking`（[mod.rs:712-727](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/kv_router/prefill_router/mod.rs#L712-L727)）核对：传入 `overlap_score_credit=Some(0.5)` 且正常分离时，输出被改写为 `Some(0.0)` 而 `router_temperature` 原样保留——确认 override 是「定向覆盖而非整体替换」。

**需要观察的现象**：表格中 prefill 派发时刻 `max_tokens=1`、decode 时刻恢复原值；`disaggregated_params` 只存在于 prefill 响应中，decode 请求上换成了 `prefill_result` 或 `bootstrap_info`。

**预期结果**：一张 3 列 × 5 行的字段变迁表，能和 4.1.2 的流程图逐条对上。

#### 4.1.5 小练习与答案

**练习 1**：为什么 PrefillRouter 要在入口剥掉客户端传入的 bypass 注解，而不是在 worker 侧忽略它？

**答案**：bypass 注解改变的是「这条请求是否跳过远端 prefill」这一路由决策，决策权必须属于路由器（[mod.rs:291-295](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/kv_router/prefill_router/mod.rs#L291-L295) 的注释原文：防止普通分离请求被意外或恶意地跳过远端 prefill）。若在 worker 侧才忽略，路由器会以为走了分离路径而 decode worker 却收到了带注解的请求，两边状态不一致；在最靠近边界的地方清洗输入，也是后端安全的一般原则。

**练习 2**：`PrefillLifecycleState` 为什么用 `AtomicU8` 而不是 `Mutex<Enum>`？

**答案**：这个状态只在激活/失活这种极低频时刻被写，但在**每条请求**的热路径上被读（[mod.rs:303](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/kv_router/prefill_router/mod.rs#L303)）。原子加载无锁、无等待，避免了高并发下请求线程在锁上排队。配合 `Ordering::Release/Acquire`（写入/读取，见 [activation.rs:617](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/kv_router/prefill_router/activation.rs#L617) 与 [activation.rs:629](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/kv_router/prefill_router/activation.rs#L629)）保证读到 Active 时绑定数据也一定已发布。

**练习 3**：decode 路由为什么强制 `overlap_score_credit = 0`，条件分离路径又为什么允许保留？

**答案**：正常分离时 KV 在 prefill worker 上算、在 decode worker 上生成，decode 侧的「prompt 重合度」对应的 KV 并不在 decode worker 本地，把它当抵扣会重复计入负载（[mod.rs:666-672](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/kv_router/prefill_router/mod.rs#L666-L672) 的注释：decode 选点应只看负载）。条件分离时请求就在被选中的 decode worker 本地跑 prefill，该 worker 索引器里的重合度是真实可复用的缓存，保留抵扣才是正确语义。

### 4.2 activation：prefill 集合如何被看见

#### 4.2.1 概念说明

PrefillRouter 不是构造好就能用的：prefill worker 是动态的，可能比 frontend 晚启动、可能全部死掉再回来。「激活」就是把「发现到的 prefill endpoint」变成「一个可用的路由绑定（PrefillBinding）」的过程。

这一步还解决一个微妙的所有权问题：**这一跳用什么方式路由？** 你可能以为 prefill 池的路由方式就是 decode 池的复制品，但源码给出的规则更有趣——prefill worker 的模型卡片如果**通告**了自己的 `router_config`，就整体覆盖 decode 侧配置；如果什么都没说，才继承 decode 集合的模式。这个「通告（advertisement）」机制让同一套部署可以跑「KV 路由的 prefill + 轮询的 decode」这类混合拓扑。

#### 4.2.2 核心流程

```text
WorkerSet 发现 prefill endpoint 变化
  └─ set_target(Some(endpoint)) / set_target(None)
       ├─ target 为 None            → lifecycle = Unavailable，清空绑定
       ├─ 与现有绑定相同 endpoint    → 保持 Active（复用）
       └─ 新 endpoint               → 清空绑定，lifecycle = Pending
                                    → watch 通道通知 drive_target
drive_target（后台循环，持有 Weak 引用）
  └─ build_binding:
       ├─ endpoint.client() 建共享客户端
       ├─ 启动该 endpoint 的运行时配置 watcher（bootstrap 信息依赖它）
       ├─ resolve_prefill_advertisement:
       │    经发现面 EndpointModels 查询列出全部 prefill 卡片
       │    → resolve_advertisement_from_cards: 第一张卡片定模式，
       │      其余卡片若模式不一致只告警（滚动更新期间的第一票胜出）
       ├─ block size / 会话亲和 TTL 的「整体替换或继承」解析
       ├─ RoutingLoadContext::start(client, RouterLoadSource::Prefill, ...)
       ├─ prefill 卡通告的 KV 配置强制 router_track_active_blocks = false
       └─ 按解析出的模式建 KV 或 builtin 的 RoutingHost
  ├─ 成功 → binding.store(...)，lifecycle = Active，打 info 日志
  └─ 失败 → error 日志，睡眠 1 秒后重试（target 变化会打断重试）
```

「无卡片可读」被刻意做成**错误而非静默继承**——激活失败会重试，而用错的路由方式服务整个部署要一直错到绑定重建为止（源码注释原话：延迟激活远比错误策略便宜）。

#### 4.2.3 源码精读

通告结构的定义在 [activation.rs:40-54](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/kv_router/prefill_router/activation.rs#L40-L54)：`session_affinity_ttl` 的类型是 `Option<Option<Duration>>`——外层 `None` 表示「卡片什么都没说（继承）」，内层 `None` 表示「卡片明确说了不要亲和」。这种双层 Option 正是「整体替换而非合并」语义的类型化表达，配套单测 `session_affinity_ttl_is_taken_whole_or_not_at_all`（[activation.rs:707-730](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/kv_router/prefill_router/activation.rs#L707-L730)）专门锁定它。

解析规则本体在 [activation.rs:59-105](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/kv_router/prefill_router/activation.rs#L59-L105)：`mode_of` 闭包对每张卡片取「有 router_config 用它的，没有用 decode 模式」；空卡片列表直接 `bail!`；多卡片不一致时第一张胜出并打告警（[activation.rs:91-102](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/kv_router/prefill_router/activation.rs#L91-L102)，注释：静默分裂意味着一半机队按另一半的条款被路由）。

绑定构建 [activation.rs:289-424](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/kv_router/prefill_router/activation.rs#L289-L424) 里三个关键点：

- [activation.rs:318-325](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/kv_router/prefill_router/activation.rs#L318-L325)：用 `RouterLoadSource::Prefill` 启动这一跳专属的 `RoutingLoadContext`——u6-l2 讲的「每个路由上下文自持负载状态」在这里落地为 prefill 源。
- [activation.rs:331-338](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/kv_router/prefill_router/activation.rs#L331-L338)：即使 prefill 卡片通告了 KV 配置，也强制 `router_track_active_blocks = false`。注释解释：prefill 路由是 prompt 侧的，在这里记 decode 块账会双倍计负载。
- [activation.rs:345-354](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/kv_router/prefill_router/activation.rs#L345-L354)：每次激活打一条 info 日志，且刻意打**解析后**的值而非卡片要的值——「一张只报了模式没重申调参的卡片会静默拿到默认值」，这行日志让这类问题不用重编就能查。

发现面查询在 [activation.rs:434-467](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/kv_router/prefill_router/activation.rs#L434-L467)：用 `DiscoveryQuery::EndpointModels` 列出该 endpoint 全部实例的卡片，解析失败的卡片被跳过并打 debug 日志——注释提醒：解析不了的卡片不只是丢一个 EAGLE 提示，而是丢了一张「这一跳怎么路由」的选票。

失败重试的节奏在 [activation.rs:567-589](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/kv_router/prefill_router/activation.rs#L567-L589)：error 日志后 sleep 1 秒，且 `tokio::select!` 保证 target 一变就立刻放弃本轮重来。成功路径 [activation.rs:549-566](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/kv_router/prefill_router/activation.rs#L549-L566)：先复核 target 没被并发改掉，再 `binding.store` + 置 Active + 打「Prefill router target activated」。

#### 4.2.4 代码实践

**目标**：在无 GPU 的 sample 分离拓扑上观察激活与失活的全过程。

**步骤**：

1. 按 u7-l1 的方式启动 [examples/backends/sample/launch/disagg.sh](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/examples/backends/sample/launch/disagg.sh)。
2. 在 frontend 进程日志中查找两条消息：`Activating prefill router`（带 `prefill_router_mode`、`decode_router_mode`、`block_size`、`kv_tuning` 等字段）与 `Prefill router target activated`。
3. 杀掉 prefill worker 进程，观察 frontend 日志与后续请求行为；再重启 prefill worker，观察重新激活。
4. 记录 `Activating prefill router` 日志里 `prefill_router_mode` 的值，并解释它为什么是这个值（提示：sample 后端不通告 router_config，见 [router_args.py:151-155](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/components/src/dynamo/common/configuration/groups/router_args.py#L151-L155)：worker 省略 `--router-mode` 即「什么都不广告」，继承 frontend 配置；而 frontend 的历史默认是 `round-robin`）。

**需要观察的现象**：杀掉 prefill worker 后新请求不再走两跳（PrefillRouter 放行直连），且整个模型因 P/D 成对可用前提（u4-l4 的 needs 声明）可能整体不就绪；重启后重新出现激活日志。

**预期结果**：一份「时间线 + 日志摘录」的实验记录，标出 Pending → Active → Unavailable → Active 的迁移点。启动顺序带来的延迟激活属正常现象（待本地验证具体日志时序）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `drive_target` 持有的是 `Weak<PrefillRouter>` 而不是 `Arc`？

**答案**：后台激活循环生命周期可能长于路由器本身。持有 `Arc` 会让路由器被 drop 后仍被后台任务引用，形成无法回收的循环依赖（Self 引用问题的变体）。用 `Weak` 后每次升级失败即退出循环；配套单测 `dropping_pending_router_releases_activation_tasks`（[mod.rs:868-897](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/kv_router/prefill_router/mod.rs#L868-L897)）验证 drop 后强引用计数归零。

**练习 2**：滚动更新期间一半 prefill worker 广告 KV 模式、一半不广告，会发生什么？

**答案**：第一张可读卡片胜出，其余不一致的只计入告警（[activation.rs:90-102](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/kv_router/prefill_router/activation.rs#L90-L102)）。解析结果保持确定性（不依赖卡片读取顺序的偶然性），但确实意味着一部分机队按另一半的路由条款被服务——所以告警值得盯。单测 `first_card_wins_when_the_fleet_disagrees`（[activation.rs:681-696](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/kv_router/prefill_router/activation.rs#L681-L696)）锁定了这一行为。

**练习 3**：prefill 卡片通告的 `kv_cache_block_size` 为 0 时为什么不能采用？

**答案**：0 表示卡片从未声明块大小（见 [activation.rs:309-314](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/kv_router/prefill_router/activation.rs#L309-L314) 的分支），不可信，回落到 decode 集合的值。块大小决定这一池 KV 事件被索引的粒度（u4-l3/u6-l4），用 decode 集的块大小去切 prefill 池的事件会把索引切错粒度——反过来，卡片声明了非 0 值就必须用它。

### 4.3 admission：prefill 响应的三种结局

#### 4.3.1 概念说明

先澄清命名：`admission.rs` 这个文件名里的 admission 不是 u6-l2 讲的调度器「准入控制」，而是对 **prefill 响应流的验收**——拿到 prefill worker 返回的流之后，判定「这次 prefill 到底产出了什么」，并把结论归类。这是分离链路上最容易踩坑的一段，因为不同后端、不同情形下 prefill 的「完成形态」并不相同：

1. **Terminal（终局）**：请求在 prefill 这一步就结束了。典型例子是 max_tokens=1 的上下文步直接命中 EOS——第一个 token 就是最后一个，根本没有 decode 的必要。
2. **Handoff（移交）**：正常结局。prefill 输出里带 `disaggregated_params`（KV 移交句柄），装进 `PrefillResult` 交给 decode。
3. **Bootstrap（引导）**：SGLang 式分离的形态。prefill 输出里带 `bootstrap_host`/`bootstrap_port`/`bootstrap_room`，decode 用它建点对点连接；此模式下 prefill 流的剩余部分被丢到后台任务排水，**decode 不等 prefill 流结束就出发**。

#### 4.3.2 核心流程

```text
consume_prefill_stream(stream, tracker, task_guard)
  ├─ 取第一帧；流为空或帧带错误 → PrefillError
  ├─ tracker.record_prefill_complete()（记录 TTFT 相关的阶段完成）
  ├─ 从首帧 usage 里提取 prompt_tokens_details
  ├─ is_bootstrap = 首帧 disaggregated_params 同时含
  │     bootstrap_host + bootstrap_port + bootstrap_room 三键
  ├─ is_bootstrap？
  │   ├─ 是 → spawn 后台任务把流排干（保住 task_guard 到流结束）
  │   └─ 否 → 同步消费剩余帧，途中出错则报 PrefillError
  ├─ is_terminal = 首帧 finish_reason 存在且 ≠ Length
  │     （None ≈ TRT-LLM 的 not_finished，仍需移交）
  ├─ is_terminal？→ Terminal 结局
  ├─ 无 data / 无 disaggregated_params → NoDisaggregatedParams 错误
  ├─ TRT-LLM 防御：ctx_request_id 为 null、或 context_only 却缺 id → 拒绝
  └─ 其余 → Handoff 结局（携带 worker_trace_link）
```

判定矩阵（首帧驱动）：

| finish_reason | disaggregated_params | 结局 |
|---|---|---|
| EoS / Stop / Cancelled / Error / ContentFilter | 任意 | Terminal |
| Length 或缺失 | 含可用句柄 | Handoff |
| Length 或缺失 | 缺失或 id 不可用 | 错误（`NoDisaggregatedParams`） |
| 任意 | 含 bootstrap 三键 | Bootstrap（在派发侧已提前识别） |

#### 4.3.3 源码精读

Bootstrap 识别在 [admission.rs:60-70](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/kv_router/prefill_router/admission.rs#L60-L70)：三个键同时存在才算 bootstrap。为什么强调「并发」而不是「等 prefill 流结束再 decode」？回看 [mod.rs:168-179](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/kv_router/prefill_router/mod.rs#L168-L179) 的文档注释：分离式 SGLang 中若等 prefill 完成才启动 decode 会**死锁 KV 传输**——所以 bootstrap 结局一到手 decode 立即出发，prefill 流剩余部分由 [admission.rs:90-94](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/kv_router/prefill_router/admission.rs#L90-L94) 的后台任务排水，且该任务持有 `task_guard` 直到流真正结束（单测 `bootstrap_drain_retains_teardown_guard_until_stream_finishes`，[admission.rs:199-238](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/kv_router/prefill_router/admission.rs#L199-L238)，验证守卫不提前释放）。

Terminal 判定在 [admission.rs:100-111](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/kv_router/prefill_router/admission.rs#L100-L111)：`!matches!(reason, FinishReason::Length)` 是关键——Length 意味着「prefill 步撞到了长度上限」，但 KV 已经建立，decode 仍要接手（哪怕立刻也撞限）；`None` 按 TRT-LLM 的 not_finished 对待，同样走移交。Terminal 结局回到 mod.rs 后由 [mod.rs:146-153](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/kv_router/prefill_router/mod.rs#L146-L153) 的 `strip_terminal_disaggregated_params` 剥掉内部句柄再回客户端——引擎私有契约不外泄（u7-l1 已提过这条纪律）。

TRT-LLM 防御在 [admission.rs:123-141](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/kv_router/prefill_router/admission.rs#L123-L141)：非终局响应若 `ctx_request_id` 为 null（TRT-LLM 用 null 标记终局上下文响应），或 `request_type == "context_only"` 却缺 id，就在这里拒绝而不是等 decode 派发后在后端深处失败——错误越早显式化，诊断成本越低。

顺带一提 bootstrap 房间号的数学：`compute_bootstrap_room`（[mod.rs:645-658](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/kv_router/prefill_router/mod.rs#L645-L658)）在已知数据并行 `(rank, size)` 时把随机数折算成

\[ \text{room} = q \cdot \text{size} + \text{rank},\quad q \in \left[0,\ \left\lfloor \frac{\text{max\_room} - \text{rank}}{\text{size}} \right\rfloor\right] \]

保证 \(\text{room} \bmod \text{size} = \text{rank}\) 且不越过 `i64::MAX`——即房间号天然落在与 rank 同余的子集里，供对端的 DP 组寻址。无法得知 DP 信息时直接用随机数（单测 `bootstrap_room_respects_modulo_and_cap` 覆盖边界）。

#### 4.3.4 代码实践（源码阅读型）

**目标**：用文件内单测反向验证判定矩阵。

**步骤**：

1. 读 [admission.rs:271-373](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/kv_router/prefill_router/admission.rs#L271-L373) 的四个单测：`terminal_finish_reasons_without_handoff_are_returned_to_caller`、`length_limited_prefill_still_requires_handoff`、`unfinished_prefill_still_requires_handoff`、`non_terminal_prefill_without_usable_ctx_request_id_fails_before_decode`。
2. 对每个单测标注它验证了 4.3.2 判定矩阵的哪一格。
3. 运行它们：`cargo test -p dynamo-llm prefill_router::admission`（在仓库根目录；macOS 按 AGENTS.md 提示加 `--no-default-features`，待本地验证）。

**需要观察的现象**：五个终局 finish_reason 全部产出了 `PrefillCompletion::Terminal`；`Length` 与 `None` 都产出 `Handoff`；`ctx_request_id: null` 与裸 `context_only` 都被 `NoDisaggregatedParams` 拒绝且错误消息含 `no usable ctx_request_id`。

**预期结果**：判定矩阵的每一格都能指到一个具体断言上。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `FinishReason::Length` 不算 Terminal？

**答案**：Length 表示撞到长度上限。prefill 步被截断时 KV 上下文已建立、移交句柄已产生，decode 侧还需要接管去生成（并正确报告长度终止）。若在这里当 Terminal 直接回客户端，客户端会拿到一个只有 1 个 token 的「完整」响应。见 [admission.rs:98-106](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/kv_router/prefill_router/admission.rs#L98-L106) 的注释。

**练习 2**：Bootstrap 结局下 prefill 流的剩余帧被丢弃了吗？这样安全吗？

**答案**：没有丢弃，是被后台任务「排干」——逐帧读到流结束再退出（[admission.rs:90-94](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/kv_router/prefill_router/admission.rs#L90-L94)）。排干是必要的：流提前断开会让请求面的故障检测（u3-l4 的 PushRouter 心跳/连接管理）误判 worker 故障；持有 `task_guard` 则保证引擎关停等清理动作不抢在流结束前发生。帧内容本身在 bootstrap 模式下确实不再被消费。

**练习 3**：`extract_bootstrap_info`（[mod.rs:107-117](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/kv_router/prefill_router/mod.rs#L107-L117)）对 `bootstrap_port` 超出 u16 范围时的行为是什么，为什么这么设计？

**答案**：返回 `None`（`u16::try_from(...).ok()?`），于是这条响应被当作普通 Completed 结局处理而非崩溃。单测 `extract_bootstrap_info_rejects_out_of_range_port`（[mod.rs:926-935](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/kv_router/prefill_router/mod.rs#L926-L935)）锁定了它。拒绝而非静默截断，是把畸形数据挡在协议边界上——截断会得到一个「看起来合法」的错误端口。

### 4.4 conditional_disagg：按请求特征切换聚合与分离

#### 4.4.1 概念说明

u7-l1 讲过分离的代价：低并发、短 prompt 时聚合反而更优，且分离容量不是免费的。「条件分离」就是把这句话变成代码：**对每条请求单独判断**，划算就跳过远端 prefill，直接把请求钉到某个 decode worker 上本地跑完 prefill+decode（等价于让那个 worker 临时当聚合 worker 用）。

判断「划算」的依据被抽象成一个 trait（[conditional_disagg.rs:71-83](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/kv-router/src/conditional_disagg.rs#L71-L83)），输入是一组**汇总信号**而非原始状态——这是为了让未来策略可以即插即用而不改路由器调用点。内置三种：

| 策略 | 版本 | 依据 | 直觉 |
|---|---|---|---|
| `isl_bounding` | v1 | 有效 ISL 绝对值 + 占比 | 「新算的量又小、又大部分有缓存」→ 本地跑 |
| `prefill_load` | v1.5 | 被选中的 prefill worker 是否过忙线 | 「prefill 池忙不过来」→ 别去挤，本地消化 |
| `isl_or_load` | v1.5 组合 | 两者任一成立 | 两个逃生门都打开 |

决策输入 `ConditionalDisaggDecisionInput` 有四个字段（[conditional_disagg.rs:24-41](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/kv-router/src/conditional_disagg.rs#L24-L41)）：`prompt_tokens`、`decode_chosen_cached_tokens`（被选中 decode worker 上的加权缓存抵扣）、`prefill_chosen_worker_busy`（`Option<bool>`，None 表示信号不可用）、`decode_chosen_worker_busy`。

除策略本身外还有一道独立的 **decode-busy 闸门**：即使策略说 bypass，若被选中的 decode worker 已经过忙线，bypass 被否决——不能为了给 prefill 池减压把 decode 池压垮。

#### 4.4.2 核心流程

先看策略侧的数学。有效 ISL 定义在 [conditional_disagg.rs:63-68](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/kv-router/src/conditional_disagg.rs#L63-L68)：

\[ \text{eff\_isl} = p - \min(c,\ p) \]

其中 \(p\) 是 prompt token 数、\(c\) 是被选 decode worker 的缓存抵扣。`isl_bounding` 的 bypass 条件（[conditional_disagg.rs:155-166](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/kv-router/src/conditional_disagg.rs#L155-L166)）是严格不等式：

\[ \text{eff\_isl} < T_{\text{abs}} \ \wedge\ \frac{\text{eff\_isl}}{\max(p,1)} < T_{\text{ratio}} \]

默认 \(T_{\text{abs}} = 2048\)、\(T_{\text{ratio}} = 0.7\)。注意**边界取等时不 bypass**（单测 `boundary_eff_isl_equals_threshold_does_not_bypass`）。

两条忙线公式在 [scheduling/types.rs:163-174](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/kv-router/src/scheduling/types.rs#L163-L174)：

\[ \text{prefill 忙} \iff A_{\text{prefill}} > \theta_p \cdot C_{\text{prefill}} \]
\[ \text{decode 忙} \iff B_{\text{decode}} > \theta_d \cdot B_{\text{total}} \]

即「活跃 prefill token 数超过容量的某个百分比」「（算上本请求的）潜在 decode 块数超过总 KV 块数的某个百分比」。decode 侧返回 `Option<bool>`：worker 没上报总块数时信号不可用。

路由侧的完整决策链（[conditional_bypass.rs:71-302](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/kv_router/prefill_router/conditional_bypass.rs#L71-L302)）：

```text
select_decode_worker_for_conditional_disagg
  ├─ 前置否决（全部返回 None = 走正常分离）：
  │    decode 集不是 KV 模式 / 请求已带 prefill 钉定 / decode 路由器不可用
  │    / token 为空 / 请求钉定的 decode worker 解析不出 DP rank
  ├─ decode_router.find_best_match_details_without_admission(...)
  │    ── advisory 选点：查出会选谁、overlap_blocks、cached_tokens、
  │       potential_decode_blocks、selected_worker_load，但不记账
  ├─ 组装 ConditionalDisaggDecisionInput(prompt_tokens, cached_tokens)
  │    ├─ 策略 needs_prefill_worker_busy()？
  │    │    └─ peek_prefill_chosen_worker_busy：
  │    │         用同样方式 advisory 地问 prefill 路由器「你会选谁」，
  │    │         再对那个 worker 的负载跑 prefill_load_exceeds(θp)
  ├─ policy.should_bypass_remote_prefill(input) → policy_says_bypass
  ├─ decode 闸门：policy_says_bypass 且配置了 θd 时，
  │    decode_busy = decode_load_exceeds(potential_decode_blocks, θd)
  ├─ bypass = policy_says_bypass && (!闸门配置 || decode_busy == Some(false))
  │    └─ 注意：闸门已配置但信号 None → 否决（宁可不 bypass）
  └─ bypass？→ 返回 (worker, overlap_tokens, net_new_tokens)
```

拿到决策后 mod.rs 侧的执行在 [mod.rs:314-370](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/kv_router/prefill_router/mod.rs#L314-L370)：把 `decode_worker_id`/`dp_rank` 写进路由提示、打上 `x-bypass-remote-prefill` 注解、直接 `next.generate()`，并在返回流的前面链上一个同名注解帧（[mod.rs:352-359](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/kv_router/prefill_router/mod.rs#L352-L359)）——让下游观测也能识别这条流走过 bypass。策略判定本身抛错时只告警并回落到正常分离（[mod.rs:362-368](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/kv_router/prefill_router/mod.rs#L362-L368)，fail-open）。

最后是 worker 侧消费：decode 模式的 vLLM worker 检查注解，命中就把自身当作聚合 worker 执行——[handlers.py:3318-3328](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/components/src/dynamo/vllm/handlers.py#L3318-L3328) 把 `is_decode_only` 置回 False 并将 mode 切到 `DisaggregationMode.AGGREGATED`。SGLang 与 TRT-LLM 的 handler 有同名的常量与等价分支（分别见于 `components/src/dynamo/sglang/request_handlers/llm/decode_handler.py` 与 `components/src/dynamo/trtllm/request_handlers/handler_base.py`）。

**配置链**（从外到内三层，同一组字段名贯穿）：

1. **CLI / 环境变量**（[kv_router_args.py:569-597](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/components/src/dynamo/common/configuration/groups/kv_router_args.py#L569-L597)）：`--router-conditional-disagg`（env `DYN_ROUTER_CONDITIONAL_DISAGG`）开关；`--router-conditional-disagg-config`（env `DYN_ROUTER_CONDITIONAL_DISAGG_CONFIG`）收一个 JSON 对象，合法键为 `policy`、`eff_isl_threshold`、`eff_isl_ratio_threshold`、`prefill_busy_threshold`、`decode_busy_threshold`（键名到 Rust 字段的映射表在 [kv_router_args.py:80-86](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/components/src/dynamo/common/configuration/groups/kv_router_args.py#L80-L86)），未知键、类型不符都会在启动时报错（[kv_router_args.py:138-187](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/components/src/dynamo/common/configuration/groups/kv_router_args.py#L138-L187)）。
2. **Rust 配置字段与默认值**（[scheduling/config.rs:853-885](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/kv-router/src/scheduling/config.rs#L853-L885)）：`conditional_disagg_enabled` 默认 false、policy 默认 `isl_bounding`、阈值默认 2048/0.7、两个 busy 阈值默认 `None`。纯 Rust 部署也可用环境变量覆盖（[scheduling/config.rs:284-312](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/kv-router/src/scheduling/config.rs#L284-L312) 的 `DYN_ROUTER_CONDITIONAL_DISAGG*` 系列）。
3. **两处等价的「阈值回落」**：负载类策略需要 `prefill_busy_threshold`，用户没给时回落到 `router_queue_threshold`——Rust 侧在构造 PrefillRouter 时解析（[activation.rs:212-218](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/kv_router/prefill_router/activation.rs#L212-L218)：`.or(c.router_queue_threshold)`），Python 侧在 `apply_conditional_disagg_config` 里同样回落（[kv_router_args.py:253-274](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/components/src/dynamo/common/configuration/groups/kv_router_args.py#L253-L274)），且两边都没给时 Python 直接报错拒绝启动——把「忘了配阈值」从运行期静默失效提前成启动失败。

#### 4.4.3 源码精读（补充细节）

前置否决清单各有其理：decode 集必须是 KV 模式（[conditional_bypass.rs:82-84](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/kv_router/prefill_router/conditional_bypass.rs#L82-L84)，注释：条件分离要偷看缓存热的 decode worker，KV prefill 挳在非 KV decode 前面做不了这个决定）；请求已带外部钉定的 prefill worker 时尊重外部决策（[conditional_bypass.rs:86-96](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/kv_router/prefill_router/conditional_bypass.rs#L86-L96)）。

「为什么不记账」的注释值得精读——[conditional_bypass.rs:244-250](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/kv_router/prefill_router/conditional_bypass.rs#L244-L250)：正常 decode 路由在返回选点前会在调度器记账；条件分离此刻还在「要不要进这条路径」的决策阶段，这里记账会导致正式派发时双倍计账。代价是承认并发 bypass 可能同时挤同一阈值（代码用「advisory 而非预约」自况），mod.rs 里对应的 TODO（[mod.rs:349-351](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/kv_router/prefill_router/mod.rs#L349-L351)）指出彻底修法是调度器预约移交。

闸门函数本体只有一行——[conditional_bypass.rs:57-63](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/kv_router/prefill_router/conditional_bypass.rs#L57-L63)：

```rust
policy_says_bypass && (!decode_gate_configured || matches!(decode_busy, Some(false)))
```

注意 `Some(false)` 而非 `!unwrap_or(true)`：闸门**配置了但信号不可用（None）时是否决**（单测 `configured_decode_gate_signal_unavailable_vetoes_bypass`）——宁可放弃优化也不在信息不全时改变路径。决策标签字符串（`bypass_allowed_decode_not_busy` 等，[conditional_bypass.rs:264-274](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/kv_router/prefill_router/conditional_bypass.rs#L264-L274)）把每种否决原因编码进日志，配合 [conditional_bypass.rs:276-291](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/kv_router/prefill_router/conditional_bypass.rs#L276-L291) 的 debug 日志（含 `net_new_tokens`、`overlap_tokens`、两个 busy 信号、阈值与标签），这是排查「为什么这条请求没走 bypass」的第一现场。

预取 prefill 忙线的实现 [conditional_bypass.rs:304-401](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/kv_router/prefill_router/conditional_bypass.rs#L304-L401) 同样是 advisory 查询（`find_best_match_details_without_admission`，[kv_router.rs:1250](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/kv_router.rs#L1250)）；被调度器拒绝（`QueueRejected`）时视作 busy=true（[conditional_bypass.rs:394-400](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/kv_router/prefill_router/conditional_bypass.rs#L394-L400)）——队列都满了当然是忙。

#### 4.4.4 代码实践

**目标**：让同一拓扑下短请求走 bypass（本地聚合）、长请求走正常分离，用日志验证切换。

**原理**：无缓存命中时 \(c=0\)、\(\text{eff\_isl}=p\)、比值恒为 1.0。默认 \(T_{\text{ratio}}=0.7\) 下**任何**无缓存请求都不会 bypass。把 `eff_isl_ratio_threshold` 抬到略高于 1.0（如 1.01），则 bypass 与否完全由绝对阈值 \(T_{\text{abs}}=2048\) 决定：短请求 bypass、长请求（≥2048 token）走分离。

**步骤**：

1. 准备拓扑（无 GPU，沿用 u7-l1 的 sample 分离拓扑，直接跑脚本再单独控制 frontend 环境；注意 [disagg.sh](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/examples/backends/sample/launch/disagg.sh) 的额外参数只透传给两个 worker，frontend 靠环境变量配置）：

   ```bash
   # 示例命令（frontend 默认 router-mode 是 round-robin，需显式切到 kv，
   # 因为条件分离要求 decode 集为 KV 模式）
   DYN_ROUTER_MODE=kv \
   DYN_ROUTER_CONDITIONAL_DISAGG=1 \
   DYN_ROUTER_CONDITIONAL_DISAGG_CONFIG='{"eff_isl_ratio_threshold": 1.01}' \
   bash examples/backends/sample/launch/disagg.sh
   ```

   （环境变量名与 CLI 的对应关系来自 [kv_router_args.py:569-597](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/components/src/dynamo/common/configuration/groups/kv_router_args.py#L569-L597) 与 [router_args.py:222-227](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/components/src/dynamo/common/configuration/groups/router_args.py#L222-L227)。）

2. 发一条短请求：

   ```bash
   curl -s http://localhost:8000/v1/chat/completions -H "Content-Type: application/json" \
     -d '{"model":"sample-model","messages":[{"role":"user","content":"Hi"}],"max_tokens":16,"stream":false}'
   ```

3. 再发一条长请求（把 prompt 撑过 2048 token，例如用 Python 生成一段重复文本的 payload 后 `curl -d @long.json`）。
4. 在 frontend 日志里分别查找消息 `Conditional disagg routing to decode worker`（[mod.rs:326-333](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/kv_router/prefill_router/mod.rs#L326-L333)，info 级，带 `net_new_tokens`/`overlap_tokens` 字段）。若想看到完整决策（`decode_gate_decision` 标签、busy 信号），需把 Rust 日志级别调到 debug（tracing 体系常规用 `RUST_LOG` 控制；frontend 进程的具体调法以本地 `--help` 输出为准，待本地验证）。
5. 复盘实验：把 `eff_isl_threshold` 降到比你的短请求还小（如 `{"eff_isl_threshold": 4, "eff_isl_ratio_threshold": 1.01}`），确认短请求也回到分离路径——验证两个条件是**与**关系。

**需要观察的现象**：短请求日志出现 bypass 消息、`net_new_tokens` 约等于其 prompt token 数且小于 2048；长请求**没有**该消息但正常返回（走了 prefill→decode 两跳）。第 5 步复盘后短请求的 bypass 消息消失。

**预期结果**：一张「请求 → prompt_tokens → 是否出现 bypass 日志 → net_new_tokens 字段值」的对照表。sample 后端不产生 KV 事件，缓存抵扣为 0，`net_new_tokens` 应等于 prompt token 数（采样器/分词带来的少量出入属正常）。

### 4.4.5 小练习与答案

**练习 1**：`prefill_load` 策略下，如果 `prefill_busy_threshold` 和 `router_queue_threshold` 都没配置，会发生什么？

**答案**：Python 侧启动即报错（[kv_router_args.py:269-274](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/components/src/dynamo/common/configuration/groups/kv_router_args.py#L269-L274)：负载感知策略需要 prefill busy 阈值）。这是把配置缺失从「运行期静默永不 bypass」提前为启动失败的例子；两个阈值字段的存在也解释了为什么 `ConditionalDisaggDecisionInput.prefill_chosen_worker_busy` 是 `Option<bool>`——信号是否可用取决于配置与 worker 上报。

**练习 2**：为什么 decode-busy 闸门在「已配置但信号为 None」时选择否决 bypass，而 `prefill_load` 策略在信号 None 时同样不 bypass？两者共同体现什么设计倾向？

**答案**：见 [conditional_bypass.rs:62](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/kv_router/prefill_router/conditional_bypass.rs#L62)（`matches!(decode_busy, Some(false))`）与 [conditional_disagg.rs:208](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/kv-router/src/conditional_disagg.rs#L208)（`unwrap_or(false)`）。共同倾向是：**信息不全时维持默认路径（分离）**。bypass 是优化而非正确性所必需，拿不准就不做；对照 u6-l3 讲过的「普通 KV 事件 fail-open、Cleared 屏障 fail-closed」，可见「优化路径宽松、正确性路径严格」的一致取舍。

**练习 3**：bypass 请求回到 decode 集后，decode 选点还会重新挑 worker 吗？

**答案**：不会重新挑。路由提示里的 `decode_worker_id`/`dp_rank` 已写死（[mod.rs:342-344](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/kv_router/prefill_router/mod.rs#L342-L344)），条件分离的收益（缓存就在那个 worker 上）依赖钉定到被偷看的那个 worker。代价在源码注释中明说：这个 advisory 选点不预留 decode 容量，钉定派发若与并发决策竞争失败，正确的修法是调度器预约移交而非带着改过的请求重试（[mod.rs:349-351](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/kv_router/prefill_router/mod.rs#L349-L351)）。

## 5. 综合实践

**任务**：给 sample 分离拓扑做一份《条件分离决策报告》，覆盖本讲全部四个模块。

1. **搭环境**：按 4.4.4 启动带条件分离的 disagg 拓扑。
2. **验证激活**（对应 4.2）：摘录 `Activating prefill router` 日志的全部字段，逐字段注明来源（卡片通告 / decode 侧继承 / 默认值），并回答：`kv_tuning` 为什么是这个值？
3. **长短分流**（对应 4.4）：分别发送约 10、500、2000、4000 token 的四条请求，记录每条的 bypass 日志有无与 `net_new_tokens`，画出「prompt_tokens → 路径」的分界线，与 `eff_isl_threshold` 的配置值对照。
4. **触发忙线回落**（对应 4.4）：换用 `prefill_load` 策略并设一个很低的 `prefill_busy_threshold`（如 0.01），发送中等长度请求，观察 bypass 是否因 prefill 忙线而触发；再配置 `decode_busy_threshold` 观察闸门否决（日志中找 `decode_gate_decision` 相关标签，可能需要 debug 级别，待本地验证）。
5. **验收侧证据**（对应 4.3）：把 `max_tokens` 设为 1 发一条请求，观察它是否在 prefill 步即终局（Terminal 路径）并正常返回——这是判定矩阵第一行的活体演示。
6. **收尾**：杀掉 prefill worker（对应 4.1/4.2 的生命周期），确认 bypass 与非 bypass 请求各自的失败表现，写一段「条件分离是否改变了故障半径」的结论。

预期产出：一份含日志摘录、参数对照表与结论的 Markdown 报告。全程无需 GPU；若本地没有 sample 后端运行环境，第 3-5 步退化为源码阅读推演并标注「待本地验证」。

## 6. 本讲小结

- **PrefillRouter 是只前向的中间件**，位于 Migration 与 decode 路由之间；生命周期三态（Pending/Active/Unavailable）非 Active 时直接放行，准入兜底在更上游的拓扑就绪门。
- **主流程五步**：截 max_tokens 为 1 → 派发 prefill → 验收响应 → 组装 decode 请求（注入句柄、恢复 max_tokens、合并拓扑约束）→ 写 decode 路由 override（正常分离强制零重合抵扣，decode 选点只看负载）。
- **激活由 prefill 卡片「整体替换或继承」**：卡片通告 router_config 时这一跳按卡片路由（块大小、亲和 TTL 同理），什么都不说才继承 decode 集；无卡片可读是错误而非静默继承，失败以 1 秒退避重试。
- **验收分三种结局**：Terminal（finish_reason 非 Length 即终局，回客户端前剥除内部句柄）、Handoff（携 disaggregated_params 的正常移交）、Bootstrap（三键齐备即并发出发 decode，prefill 流后台排水防死锁）。
- **条件分离 = 策略 + 闸门**：`isl_bounding`（eff_isl 双阈值，严格小于）、`prefill_load`（忙线布尔）、`isl_or_load`（或）；decode-busy 闸门对策略结论有一票否决权，信号缺失时倾向维持默认分离路径；选点全部走 advisory 不记账。
- **配置三层贯穿**：CLI `--router-conditional-disagg[-config]` / 环境变量 `DYN_ROUTER_CONDITIONAL_DISAGG*` / Rust `KvRouterConfig` 字段，负载类策略的 busy 阈值缺省回落 `router_queue_threshold`，两侧都没给则启动失败。

## 7. 下一步学习建议

本讲结束后，分离式服务的「控制面」你已经读完了。下一讲 **u7-l3 KV 传输引擎：NIXL / NCCL / memcpy** 讲同一枚硬币的另一面：本讲反复出现的 disaggregated_params 句柄最终驱动的**数据面**——KV 块如何经 NIXL 从 prefill GPU 直传 decode GPU（建议先回顾 u3-l4 的 FirstResponseGuard 与本讲 4.1.3 提到的「prefill 与 decode 并发、不等 KV 传完」的约束，它们在传输侧都有对应的安全机制）。若你想先补控制面的周边，推荐回读 [lib/llm/src/kv_router.rs](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/llm/src/kv_router.rs) 中 `find_best_match_details_without_admission` 的完整签名（本讲只用了它的 advisory 语义），以及 u6-l2 讲过的调度器记账与之的差异。
