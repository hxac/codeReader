# 限流、在途、延迟与授权

## 1. 本讲目标

在前面的讲义里，我们已经走通了「信号 → 投影 → 决策 → 选模型」这条主链路。但一个能在生产环境里跑的路由器，除了会"选得准"，还要会"管得住"：谁来访问、能不能放行、会不会把后端压垮、缓存在该热的时候热不热。这些**请求路径上的治理控制（governance controls）**就是本讲的主题。

学完本讲你应该能够：

- 说清 **限流链（ratelimit）** 的"首拒语义（first-deny）"——多个 provider 如何串成一条链、谁先拒绝谁说了算，以及 RPM/TPM 两种预算的区别。
- 解释 **在途计数（inflight）** 为什么不会因为漏调 `End` 而永远泄漏——它是如何"自愈（self-healing）"的，以及它如何成为负载感知选择的输入。
- 理解 **缓存热度（cache warmth）** 如何只用 TTFT（首 token 延迟）这一个观测量，去估计"这次请求大概命中了 KV 缓存"的概率，并且知道这条估计何时可信、何时该回到先验 0.5。
- 理解 **授权（authz）** 信号如何套用 Kubernetes 的 RoleBinding 模式，把"用户身份 + 组成员"翻译成角色，并拒绝"无身份却配置了绑定"这种静默绕过。

本讲聚焦"四类治理控制各自是什么、数学/机制上怎么算、在请求链路上哪里被调用"，不重复 u4-l2 的 Registry 容器与 u5 的主链路细节。

## 2. 前置知识

- **首 token 延迟（TTFT，Time To First Token）**：从请求发出到收到模型第一个输出 token 的耗时。在 LLM 推理里，如果这次请求的 prompt 前缀在上一次已经被算过并缓存（KV cache 命中），首 token 会回来得很快；否则模型要从头算 prefill，TTFT 会明显变大。所以 **TTFT 是"缓存热不热"的一个间接信号**——这是本讲第 3 个模块的核心直觉。
- **RPM / TPM**：每分钟请求数（Requests Per Minute）/ 每分钟 token 数（Tokens Per Minute）。RPM 数的是"来了几次请求"，TPM 数的是"真正消耗了多少 token"。后者更贴合 LLM 的真实成本，但有个难点：发请求时还不知道输出会产生多少 token，只能先估、后补。
- **首拒（first-deny）语义**：把多个检查者排成一条链，逐个问"放不放行"，只要有一个说"不"，整条链立刻拒绝并短路返回；只有所有人都说"行"才放行。这与网络防火墙"任一规则命中即阻断"是同一个思路。
- **Kubernetes RoleBinding 模式**：`Subject（谁）→ RoleBinding（绑定关系）→ Role（能干什么）`。本讲的 authz 把 `Subject` 换成"用户 ID + 组"，把 `Role` 当作路由决策能引用的信号名。
- **fail-closed / fail-open**：检查器自己出故障（比如限流服务连不上）时怎么办？fail-closed（默认）= 宁可错杀，按"拒绝"处理；fail-open = 宁可放过，按"放行"处理，只在"可用性比限流准确度更重要"时才开。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `pkg/ratelimit/provider.go` | 限流抽象：`Provider` 接口、`Context`（请求信息）、`Decision`（放行/拒绝决策）、`TokenUsage`（实际 token 消耗）。包注释说明了"首拒"总纲。 |
| `pkg/ratelimit/chain.go` | `RateLimitResolver`：把多个 Provider 串成链，实现 first-deny、fail-open/closed、合并最严格配额。 |
| `pkg/ratelimit/local_provider.go` | `LocalLimiter`：进程内限流器，支持 RPM + TPM 两种规则，滑动窗口计数，响应后补报输出 token。 |
| `pkg/ratelimit/envoy_provider.go` | `EnvoyRLSProvider`：通过 gRPC 调用外部 Envoy Rate Limit Service，用 descriptor（user/model/group）做全局限流。 |
| `pkg/inflight/tracker.go` | 在途请求计数器：`Begin/End/Get/Snapshot`，靠时间戳老化实现自愈，是负载感知选择的数据源。 |
| `pkg/latency/cache.go` | TTFT/TPOT 的历史统计缓存（EWMA 平滑 + 滑动窗口 + 百分位）。`UpdateTTFT` 是缓存热度的数据入口。 |
| `pkg/latency/warmth.go` | `EstimateCacheProbability`：从 TTFT 历史估计缓存命中概率，含可靠性（reliability）加权与先验回退。 |
| `pkg/classification/authz_classifier.go` | `AuthzClassifier`：启动期校验并规范化 RBAC 绑定，请求期把身份/组匹配成角色。 |
| `pkg/classification/classifier_signal_authz.go` | authz 信号在分类编排里的接入点：从 header 取身份、调 `Classify`、把结果写回 `SignalResults`。 |
| `pkg/extproc/router_resolvers.go` | `buildRateLimitResolver`：从配置组装限流链；`knownRateLimitProviderTypes` 是合法 provider 类型白名单。 |
| `pkg/extproc/processor_req_body_prepare.go` | 请求阶段：`inflight.Begin` 入册、`applyRateLimitAndCacheChecks` 跑限流检查与缓存短路。 |
| `pkg/extproc/processor_res_*.go` | 响应阶段：记录 TTFT 并估缓存热度、`inflight.End` 出册、`RateLimiter.Report` 补报 token。 |

## 4. 核心概念与源码讲解

### 4.1 限流链（Rate Limit Chain）

#### 4.1.1 概念说明

限流解决的是"别让某个用户/某个模型把后端打爆，也别让账单失控"。Semantic Router（下称 SR）把限流设计成一条**可插拔的 Provider 链**，而不是单一限流器。原因有二：

1. **限流判断可能来自不同来源**：既要支持"进程内本地计数"（无需外部依赖、按 user×model 细粒度），也要支持"外部 Envoy Rate Limit Service"做跨实例的全局限流。两种各有所长，常常要同时启用。
2. **统一在一处短路**：无论哪一种来源说"不行"，都应该立刻把请求挡掉，回一个干净的 429，并带上 `retry-after`、剩余配额等标准头。

`provider.go` 的包注释把这条总纲讲得很清楚——链采用 first-deny 语义，任一 provider 拒绝即以 429 拒绝：

> [ratelimit/provider.go:15-16](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/ratelimit/provider.go#L15-L16) —— 包注释声明 first-deny 语义：任一 provider 拒绝即返回 429。

#### 4.1.2 核心流程

抽象层面，限流链的运行分两个时机：

```
请求阶段（Check）：
  对链中每个 provider 顺序调用 Check(ctx)
    ├─ 出错 → failOpen? 放行并记日志 : 拒绝（429）
    ├─ 返回 Allowed=false → 立即拒绝（短路），带上该 provider 的配额头
    └─ 返回 Allowed=true  → 累计"最严格"的 remaining/limit/reset
  全部放行 → 返回合并后的允许决策

响应阶段（Report）：
  把真实 token 用量广播给所有 provider → 本地 TPM 规则据此补扣输出 token
```

两条边界值得记住：

- **拒绝永远优先于错误**：一个 provider 明确 `Allowed=false`，无论 fail-open 还是 fail-closed 都会立刻挡掉；fail-open/closed 只在 provider **自己抛错**时才有区别。
- **合并取最严**：全放行时，返回的 `remaining/limit` 取所有 provider 里最小的，这样客户端拿到的配额头反映"最紧的那条约束"。

#### 4.1.3 源码精读

**(1) Provider 接口与数据结构**

限流的三个核心类型都很薄：

> [ratelimit/provider.go:29-42](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/ratelimit/provider.go#L29-L42) —— `Provider` 接口：`Name()`、`Check(ctx)`（求解放行与否）、`Report(ctx, usage)`（响应后回填真实 token）。注释明确"error 表示 provider 故障，不是限流拒绝"。

`Context` 携带请求维度的判定信息（`UserID/Groups/Model/Headers/TokenCount`），其中 `TokenCount` 是分类阶段估出来的输入 token，供 TPM 规则预扣：

> [ratelimit/provider.go:44-51](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/ratelimit/provider.go#L44-L51) —— `Context`，`TokenCount` 为分类期估算的输入 token。

`Decision` 既回答"放不放行"，也带配额元数据（`Remaining/Limit/ResetAt/RetryAfter/Provider`），这些直接用于构造 429 响应头。

**(2) RateLimitResolver：first-deny 的实现**

`Check` 是 first-deny 的心脏。注意三个分支：

> [ratelimit/chain.go:59-122](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/ratelimit/chain.go#L59-L122) —— `Check` 顺序遍历 provider：错误按 failOpen 决定放行/拒绝（[L76-89](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/ratelimit/chain.go#L76-L89)）；明确拒绝立即短路返回（[L93-98](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/ratelimit/chain.go#L93-L98)）；全放行则合并最严格配额（[L100-109](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/ratelimit/chain.go#L100-L109)）。

关键片段：

```go
for _, p := range r.providers {
    d, err := p.Check(ctx)
    if err != nil {
        if r.failOpen { /* 记 warn，continue 放行 */ } 
        // fail-closed：返回拒绝 + RetryAfter 5s
        return &Decision{Allowed: false, Provider: p.Name(), RetryAfter: 5 * time.Second}, err
    }
    if !d.Allowed { d.Provider = p.Name(); return d, nil }   // 首拒，短路
    // 否则合并：取最小 remaining / limit / 最早 resetAt
}
```

`Report` 是尽力而为的广播，错误只记日志不上抛：

> [ratelimit/chain.go:126-135](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/ratelimit/chain.go#L126-L135) —— `Report` 把 token 用量转发给所有 provider，错误仅 warn。

**(3) LocalLimiter：RPM + TPM 双预算**

`LocalLimiter` 用滑动窗口计数，每个桶（bucket）按"规则名|用户|模型|rpm/tpm"复合键隔离，做到 per-user per-model 粒度：

> [ratelimit/local_provider.go:195-197](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/ratelimit/local_provider.go#L195-L197) —— `bucketKey` 复合键，把计数隔离到 (规则,用户,模型,种类)。

`Check` 对每条命中的规则分别处理 RPM（扣 1）和 TPM（扣估算输入 token）：

> [ratelimit/local_provider.go:64-146](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/ratelimit/local_provider.go#L64-L146) —— `Check` 逐规则评估，任一预算耗尽即拒；存活则累计最严格配额。

TPM 的关键巧思在 **"先估后补"**：请求阶段 `Check` 只能拿到估算的输入 token，输出 token 还没产生；到响应阶段 `Report` 才把真实**输出 token**补扣进去（输入那部分 Check 已经扣过了，不重复）：

> [ratelimit/local_provider.go:151-171](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/ratelimit/local_provider.go#L151-L171) —— `Report` 只补扣 `OutputTokens`，注释点明"Check 已扣估算输入，这里补未知的输出部分"。

滑动窗口由 `tryConsume` 实现：窗口过期就清零重开，否则在余量内扣减：

> [ratelimit/local_provider.go:210-226](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/ratelimit/local_provider.go#L210-L226) —— `tryConsume` 滑动窗口：超时清零，余量不足拒绝。

**(4) EnvoyRLSProvider：外部全局限流**

它把请求信息翻译成 Envoy RLS 的 descriptor（`user_id`、`model`、每个 `group` 一条），通过 gRPC 询问外部限流服务，并把 `OverallCode == OK` 翻译成 `Allowed`：

> [ratelimit/envoy_provider.go:56-93](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/ratelimit/envoy_provider.go#L56-L93) —— `Check` 发 `ShouldRateLimit`，按 `OverallCode` 判定，并从 status 提取 remaining/limit/reset。

注意它的 `Report` 是空操作——外部 RLS 靠自己的 descriptor 计数跟踪用量，token 维度交给本地限流器，**两者职责互补**。

**(5) 链如何从配置组装**

`buildRateLimitResolver` 读配置、校验、构造 provider、设 fail-open：

> [pkg/extproc/router_resolvers.go:130-163](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/extproc/router_resolvers.go#L130-L163) —— `buildRateLimitResolver`：无 provider 返回 nil（限流禁用）；校验失败记错并禁用；构造成功才建链。

合法 provider 类型由白名单 `knownRateLimitProviderTypes` 收敛（当前两种）：

> [pkg/extproc/router_resolvers.go:19-22](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/extproc/router_resolvers.go#L19-L22) —— `envoy-ratelimit`、`local-limiter` 两种合法类型。

请求阶段，`applyRateLimitAndCacheChecks` 在选完模型后、缓存检查前调用链：

> [pkg/extproc/processor_req_body_prepare.go:161-183](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/extproc/processor_req_body_prepare.go#L161-L183) —— `r.RateLimiter.Check(...)`：出错或拒绝都走 `createRateLimitResponse` 回 429，并把 `rlCtx` 存进 `ctx.RateLimitCtx` 供响应阶段 `Report` 复用。

429 响应由 `createRateLimitResponse` 生成，带 `retry-after`、`x-ratelimit-limit/remaining/reset` 标准头：

> [pkg/extproc/processor_req_body_memory.go:301-344](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/extproc/processor_req_body_memory.go#L301-L344) —— `createRateLimitResponse`：`ImmediateResponse` + HTTP 429 + 标准限流头。

配置长这样（基于 [config/config.go:138-162](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/config/config.go#L138-L162) 的结构，示例代码）：

```yaml
# 示例代码：仅展示结构，非仓库默认配置
rate_limit:
  fail_open: false
  providers:
    - type: envoy-ratelimit
      address: ratelimit-service:8081
      domain: semantic-router
    - type: local-limiter
      rules:
        - name: free-tier-rpm
          match: { group: "free" }
          requests_per_unit: 60
          unit: minute
        - name: power-tpm
          match: { user: "*" }
          tokens_per_unit: 100000
          unit: minute
```

#### 4.1.4 代码实践

**实践目标**：亲手验证 first-deny 语义与 RPM/TPM 双预算的"先估后补"。

**操作步骤**（源码阅读 + 单测实验型实践）：

1. 打开 `pkg/ratelimit/ratelimit_test.go`，找到针对 `RateLimitResolver.Check` 的用例，确认它构造了"一个 allow、一个 deny"的 provider 链，并断言结果是 deny。
2. 在 `pkg/ratelimit/` 下写一个临时测试（不修改生产代码，可放在临时目录），构造两个 `LocalLimiter`：A 的规则 `requests_per_unit: 1`，B 的规则 `requests_per_unit: 100`，用 `NewRateLimitResolver(A, B)` 串起来，连续调两次 `Check`：
   - 第一次：A 放行（余 0），B 放行 → 合并后返回的 `Remaining` 应是 0（取最严）。
   - 第二次：A 拒绝 → 链短路，`Decision.Provider == "local-limiter"`，`Allowed == false`。
3. 验证 TPM 补报：设一条 `tokens_per_unit: 100` 的 TPM 规则，`Check` 时 `Context.TokenCount = 30`（扣 30，余 70），随后 `Report(ctx, TokenUsage{InputTokens:30, OutputTokens: 60})`，再 `Check` 一次——应被拒绝（70 - 60 = 10 < 30 的下次请求估算）。注意 `Report` 只补扣 `OutputTokens`。

**需要观察的现象**：第二次请求被 A 拒绝时，B 的 `Check` **不会被调用**（first-deny 短路）；日志里能看到 `Rate limit DENIED by provider "local-limiter"`。

**预期结果**：合并放行时 `Remaining` 反映最严格的那个 provider；任一拒绝即短路。TPM 在响应阶段补扣输出 token 后，预算被进一步压低。若你无法本地跑测试，记为「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：如果两个 provider 都返回 `Allowed=true`，但 A 的 `Remaining=5`、B 的 `Remaining=2`，链返回的 `Remaining` 是多少？为什么？

**答案**：是 2。`Check` 在全放行分支合并时取所有 provider 里**最小**的 remaining（[chain.go:101-103](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/ratelimit/chain.go#L101-L103)），把"最紧约束"透传给客户端。

**练习 2**：把 `fail_open` 设成 `true`，然后让 `EnvoyRLSProvider` 连一个不存在的地址（必然超时）。请求会被拒绝还是放行？`LocalLimiter` 仍会评估吗？

**答案**：放行。fail-open 时 provider 出错只记 warn 后 `continue`，链继续评估后面的 provider（[chain.go:79-82](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/ratelimit/chain.go#L79-L82)）。所以 `LocalLimiter` 仍会被检查；若本地也拒绝，最终仍拒绝——因为"拒绝永远优先于错误"。

---

### 4.2 在途计数（Inflight Tracking）

#### 4.2.1 概念说明

"在途（inflight）"指**此刻正在被后端处理、还没返回的请求数**。它有两个用途：

1. **负载感知选择**：当多个等价模型可选时，"当前在途请求少"的那个通常排队更短、更快。SR 的 `multi_factor` 选择器就把 `inflight.Get(model)` 当作一个因子（见 [selection/multi_factor.go:114](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/selection/multi_factor.go#L114)，传入的是 `inflight.Get` 函数本身）。
2. **可观测性**：Prometheus 指标 `llm_model_inflight_requests` 暴露每模型在途数，用于排障和容量规划。

朴素实现是一个"++/--"计数器：请求开始 `+1`，结束 `-1`。但 LLM 流式响应很容易**漏调结束**——客户端断开、stream 中途 panic、某个错误分支忘了 `End`——计数器就会**永远偏高**，越积越多，最终把负载选择彻底带偏。`pkg/inflight` 的核心价值就是**自愈（self-healing）**：即便漏调 `End`，最多 `DefaultMaxAge`（10 分钟）后计数自动归零。

#### 4.2.2 核心流程

```
Begin(model)：
  生成递增 id，记录 start = now，写入 entries[id]
  返回 id 作为 token（model 为空则返回 0，后续 End 是 no-op）

Get(model) / Snapshot()：
  先驱逐：把 start 距今 > maxAge 的 entry 删掉（自愈）
  再返回剩余 entry 数 = 在途数

End(model, token)：
  按 (model, token) 精确删除对应 entry；空 map 顺带删 model 键
  token==0 或已不存在 → no-op，绝不 panic
```

自愈的代价是：被驱逐的"幽灵"请求在最多 10 分钟内仍被计入。这个窗口被刻意选得"宽裕地超过最长的合理 LLM 流式完成时间"，保证真实长请求不会被误杀。

#### 4.2.3 源码精读

包注释把设计意图讲得非常直白：

> [pkg/inflight/tracker.go:17-28](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/inflight/tracker.go#L17-L28) —— 自愈说明：每个 `Begin` 记时间戳，超过 `DefaultMaxAge` 的视为弃置并剔除，"漏掉的 End 在最多 DefaultMaxAge 内自纠，而非像朴素计数器那样永远泄漏"。还点明它**刻意镜像 `pkg/latency` 的全局状态模式**，让选择器能用包级读函数而不必把句柄穿过 `SelectionContext`。

数据结构：每个 model 一份 `modelState`，里面是"递增 id → entry(含 start)"的 map：

> [pkg/inflight/tracker.go:42-56](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/inflight/tracker.go#L42-L56) —— `entry{id,start}`、`modelState{nextID,entries}`、全局 `mu`/`states`/`maxAge`。

`Begin` 在锁内分配 id 并记时间戳；空 model 返回 0：

> [pkg/inflight/tracker.go:73-89](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/inflight/tracker.go#L73-L89) —— `Begin`。

自愈的核心是 `evictAndCount`——**驱逐和计数在一次加锁内完成**，驱逐后若 map 空了就连 model 键一起删，避免内存累积：

> [pkg/inflight/tracker.go:150-165](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/inflight/tracker.go#L150-L165) —— `evictAndCount`：`now.Sub(e.start) > maxAge` 即删；空则清 model 键。

注意 `Get` 用的是**写锁**（`mu.Lock`）而非读锁，因为它要边驱逐边读——驱逐是写操作。这是刻意为之的正确性取舍。

**在请求链路上的接入**：`Begin` 发生在模型选定之后、放行给后端之前：

> [pkg/extproc/processor_req_body_prepare.go:124](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/extproc/processor_req_body_prepare.go#L124) —— `ctx.InflightToken = inflight.Begin(selectedModel)`，token 存进请求上下文。

`End` 在多个收尾点都被调用（流式结束、非流式 usage 处理、核心清理、各种早退路径），形成一张"出口网"。代表性几处：

> [pkg/extproc/processor_res_body_streaming.go:116](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/extproc/processor_res_body_streaming.go#L116) 与 [pkg/extproc/processor_res_usage.go:113](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/extproc/processor_res_usage.go#L113) —— 响应收尾时 `inflight.End`；早退路径见 [processor_req_body_prepare.go:126,139,144](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/extproc/processor_req_body_prepare.go#L126-L144)。

请求上下文里 `InflightToken` 的注释也强调了"零值即从未入册，`End` 是 no-op"这一安全约定：

> [pkg/extproc/request_context.go:118-122](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/extproc/request_context.go#L118-L122) —— `InflightToken` 字段说明。

**指标暴露**：Prometheus 采集器**直接调 `inflight.Snapshot()`** 而非另开一套 inc/dec gauge，注释明说这避免了"接线点漏 panic/漏调"导致的漂移，`pkg/inflight` 始终是唯一真相源：

> [pkg/observability/metrics/inflight_collector.go:42-46](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/observability/metrics/inflight_collector.go#L42-L46) —— `Collect` 在 scrape 时读 `Snapshot`，指标名 `llm_model_inflight_requests`。

#### 4.2.4 代码实践

**实践目标**：验证自愈——模拟"漏调 End"，观察计数如何被驱逐。

**操作步骤**（单测实验型）：

1. 打开 `pkg/inflight/tracker_test.go`，找到验证自愈/驱逐的用例，阅读它如何用 `SetMaxAge` 把窗口调小（比如 50ms）来加速测试。
2. 参照其模式写一个临时实验（在临时目录，不改生产代码）：
   - `inflight.Reset()`；`inflight.SetMaxAge(50 * time.Millisecond)`。
   - 调 `Begin("m1")` 3 次，**故意不调 End**。
   - 立即 `Get("m1")` → 应为 3。
   - `sleep 80ms` 后再 `Get("m1")` → 应为 0（三条 entry 因超龄被驱逐，自愈生效）。
   - 再 `Snapshot()` → 整个 map 已空（model 键也被删）。

**需要观察的现象**：不调 `End` 也不会泄漏；等待超过 `maxAge` 后计数自动归零。

**预期结果**：3 → 0。若你无法本地跑，记为「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `Get` 用写锁（`mu.Lock`）而不是读锁（`mu.RLock`）？

**答案**：因为 `Get` 内部调用 `evictAndCount`，驱逐超龄 entry 是**写操作**（改 map）。用读锁会引发并发写冲突，所以即使看起来是"读计数"，也必须用写锁。这是正确性优先于读吞吐的取舍。

**练习 2**：如果一个请求真实耗时 12 分钟（超过 `DefaultMaxAge=10min`），它对在途计数的影响是什么？

**答案**：在第 10 分钟时它会被驱逐，即"提前从计数里消失"。所以 `DefaultMaxAge` 被刻意设得**宽裕超过最长的合理 LLM 流式完成时间**（见 [tracker.go:36-40](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/inflight/tracker.go#L36-L40) 注释），以尽量让真实长请求在其生命期内一直被正确计入；只有异常长（疑似泄漏）的请求才被当作"弃置"清掉。

---

### 4.3 缓存热度（Cache Warmth）

#### 4.3.1 概念说明

LLM 推理引擎（如 vLLM）有 **KV cache / prefix cache**：如果两次请求的 prompt 前缀一样，第二次可以复用上次算好的注意力键值，省掉昂贵的 prefill，**TTFT 会显著变小**。反过来，TTFT 大通常意味着 cache miss。

于是 SR 用一个朴素的因果关系做估计：**TTFT 越接近该模型的"快档（warm 分位）"，越可能命中了缓存；越接近"慢档（cold 分位）"，越可能是 miss**。`EstimateCacheProbability` 就是把这条直觉量化成一个 [0,1] 概率。

但光有原始分数还不够——**证据不足时不能瞎猜**。如果一个模型只观测过两次、或冷热分布太接近分不出来、或数据是几分钟前陈旧的，估计都不可信，应该向先验 `0.5`（"不知道"）靠拢。这就是"可靠性（reliability）加权"的来由。

#### 4.3.2 核心流程

估计分四步（数学记号见下文）：

```
1. 取该模型的 TTFT 历史，排序后读三个分位：warm=p20、ref=p50、cold=p80
2. 算原始热度分 raw = clamp((cold - ttft) / effectiveScale, 0, 1)
       其中 effectiveScale = max(cold - warm, ref * 0.10)   # 防 spread 过小
3. 算可靠性 reliability = countReliability × spreadReliability × freshnessReliability
4. 融合 prob = reliability × raw + (1 - reliability) × 0.5
```

关键公式（独立公式块）：

原始热度分——TTFT 越小（越接近 warm），\(raw\) 越接近 1：

\[
raw = clamp\!\left(\frac{cold - ttft}{effectiveScale},\ 0,\ 1\right),\quad
effectiveScale = max(cold - warm,\ ref \cdot 0.10)
\]

可靠性是三个因子的乘积，任一不足都会把整体可靠性拉低：

\[
reliability = R_{count} \cdot R_{spread} \cdot R_{fresh}
\]

\[
R_{count} = clamp\!\left(\frac{N - 5}{50 - 5},\ 0,\ 1\right),\quad
R_{spread} = clamp\!\left(\frac{(cold-warm)/ref}{0.15},\ 0,\ 1\right),\quad
R_{fresh} = \exp\!\left(-\ln 2 \cdot \frac{age}{60}\right)
\]

最终融合——可靠性高就信原始分，可靠性低就回先验 0.5：

\[
prob = clamp\!\left(reliability \cdot raw + (1 - reliability) \cdot 0.5,\ 0,\ 1\right)
\]

直觉解读：\(R_{count}\) 要求观测够多（5 起步、50 满分）；\(R_{spread}\) 要求冷热分得开（相对 spread 达 15% 才满分，否则 TTFT 抖动会盖过信号）；\(R_{fresh}\) 是 60 秒半衰期——数据越陈旧越不可信。三者相乘意味着"必须同时充足"才敢相信 raw。

#### 4.3.3 源码精读

常量集中定义，方便调参（注释里给了语义）：

> [pkg/latency/warmth.go:20-35](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/latency/warmth.go#L20-L35) —— 先验 `CacheWarmthPrior=0.5`、计数起止 `5/50`、目标相对 spread `0.15`、新鲜度半衰期 `60s`、最小相对尺度地板 `0.10`。

`EstimateCacheProbability` 的主体，注意它如何处理"证据缺失/不可靠就回先验"：

> [pkg/latency/warmth.go:84-143](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/latency/warmth.go#L84-L143) —— 主函数。模型名为空或 ttft≤0 直接回先验（[L85-88](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/latency/warmth.go#L85-L88)）；无历史快照回先验（[L95-98](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/latency/warmth.go#L95-L98)）；冷热分位不可用或 cold≤warm 回先验（[L103-107](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/latency/warmth.go#L103-L107)）。

关键计算段：

```go
spread := cold - warm
effectiveScale := math.Max(spread, ref*MinRelativeScaleFloor)   // 防 spread 过小除零/放大噪声
raw := clamp((cold-input.TTFTSeconds)/effectiveScale, 0, 1)

countReliability := clamp(float64(obs-5)/float64(50-5), 0, 1)
spreadReliability := clamp((spread/ref)/0.15, 0, 1)
freshnessReliability := math.Exp(-math.Ln2*age/60.0)

reliability := countReliability * spreadReliability * freshnessReliability
prob := clamp(reliability*raw+(1.0-reliability)*CacheWarmthPrior, 0, 1)
```

数据来源是 TTFT 历史缓存。`UpdateTTFT` 用 **指数加权移动平均（EWMA）** 平滑均值，并维护一个最多 1000 个的滑动窗口供百分位计算：

> [pkg/latency/cache.go:233-284](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/latency/cache.go#L233-L284) —— `UpdateTTFT`：EWMA 公式 `new_avg = 0.3·new + 0.7·old`（[L270](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/latency/cache.go#L270)），窗口裁剪到 `MaxTTFTHistorySize=1000`。

`ModelTTFTStats` 同时保存均值、最近值、滑动窗口与观测计数：

> [pkg/latency/cache.go:219-225](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/latency/cache.go#L219-L225) —— `ModelTTFTStats`。

**在请求链路上的接入**：响应头到达时（即首 token 已回，TTFT 可测）记录并估计。两处实现——非流式走响应头处理，流式走流式首块处理：

> [pkg/extproc/processor_res_header_runtime.go:101-105](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/extproc/processor_res_header_runtime.go#L101-L105) —— `latency.UpdateTTFT(...)` 紧跟 `EstimateCacheProbability(...)`，结果写进 `ctx.CacheWarmthEstimate`。

流式分支同样在首块记 TTFT（[processor_res_body_streaming.go:67-68](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/extproc/processor_res_body_streaming.go#L67-L68)）。

估计结果存进请求上下文，供后续会话/选择逻辑消费：

> [pkg/extproc/request_context.go:128](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/extproc/request_context.go#L128) —— `CacheWarmthEstimate float64`，注释"0.5 = unknown"。

注意时序要点：缓存热度是**在响应阶段才算出来**的（因为要先测到 TTFT），所以它影响的是**下一次**同模型/同会话请求的选择，而非当前这次。TTFT 本身用 `ctx.ProcessingStartTime` 起算到响应头/首块到达（[processor_res_header_runtime.go:93](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/extproc/processor_res_header_runtime.go#L93)）。

#### 4.3.4 代码实践

**实践目标**：用现成单测理解"原始分 + 可靠性融合"如何随观测数和新鲜度变化。

**操作步骤**（阅读 + 推演型）：

1. 打开 `pkg/latency/warmth_test.go`，重点读三个用例：
   - `TestEstimateCacheProbability_WarmObservation`：喂一组偏小的 TTFT 历史，再测一个更小的 ttft，断言概率偏高。
   - `TestEstimateCacheProbability_FewObservations`：只喂 2 个观测，断言概率**贴近 0.5**（因 \(R_{count}\) 为 0）。
   - `TestEstimateCacheProbability_FreshnessDecay`：对比"刚更新"与"陈旧"两份相同历史，断言陈旧者更靠近 0.5。
2. 手工推演：假设某模型历史观测的 p20=0.1s、p50=0.2s、p80=0.4s，共 50 次且刚更新。对一个 `ttft=0.12s` 的请求：
   - `spread = 0.3`，`effectiveScale = max(0.3, 0.2·0.1) = 0.3`。
   - `raw = (0.4 - 0.12)/0.3 = 0.933`（接近 warm，热度高）。
   - \(R_{count}=1\)、\(R_{spread}=clamp((0.3/0.2)/0.15,0,1)=1\)、\(R_{fresh}\approx1\) → `reliability≈1`。
   - `prob ≈ 1·0.933 + 0·0.5 ≈ 0.933`。

**需要观察的现象**：观测少时概率被拉回 0.5；数据陈旧时也被拉回 0.5；TTFT 越接近 warm 分位概率越高。

**预期结果**：与上述推演一致。若你跑测试，确认 `WarmObservation` 通过、`FewObservations` 的输出在 0.5 附近。记为「待本地验证」若无法执行。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `effectiveScale` 要取 `max(spread, ref * 0.10)` 而不是直接用 `spread`？

**答案**：如果某模型的 TTFT 分布很集中（warm≈cold，spread 极小），直接用 spread 当分母会把 \(raw\) 放大成噪声（微小 TTFT 抖动就被判成极热/极冷）。设一个 `ref·10%` 的地板，相当于"分布太平时就别假装能精确分辨"，配合 \(R_{spread}\) 把可靠性压低，最终让概率回先验——一个保守的安全阀。

**练习 2**：如果某模型上线后第一次请求，`EstimateCacheProbability` 会返回什么？为什么这是合理的？

**答案**：返回 `0.5`。因为 `getTTFTSnapshot` 找不到该模型的历史（[warmth.go:95-98](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/latency/warmth.go#L95-L98)）。冷启动时"不知道"是最诚实的答案，0.5 作为先验不偏不倚，等数据攒够了再逐步过渡到有信息的估计。

---

### 4.4 授权（Authorization）

#### 4.4.1 概念说明

authz 信号回答"**这个请求是谁发的、他属于哪些组、因此该被认作什么角色**"。它套用 Kubernetes 的 RoleBinding 模式：

- **Subject（主体）**：来自可信 auth 后端（Authorino、Envoy Gateway JWT 等）注入的 header——`x-authz-user-id`（用户 ID）和 `x-authz-user-groups`（逗号分隔的组列表）。
- **RoleBinding（绑定）**：配置里声明的"哪个主体 → 哪个角色"映射。
- **Role（角色）**：被当作信号名输出（如 `vip`、`internal`），决策引擎的 ROUTE 可以写 `authz("vip")` 来命中，从而把不同角色路由到不同模型。

关键安全姿态是 **"无身份绝不静默放行"**：一旦配置了 role_bindings，而请求里却拿不到用户 ID，`Classify` 直接返回错误——这会把"auth 后端没正确注入身份头"暴露成显式失败，而不是让人悄悄以"匿名/默认"身份穿过策略。authz 在请求链路里属于**硬错误**（返回 403，见 u5 摘要），与限流的 429、缓存的短路并列。

#### 4.4.2 核心流程

```
启动期（NewAuthzClassifier）：
  遍历 role_bindings，校验：name 非空且唯一、role 非空、至少一个 subject、
                          kind ∈ {User,Group}、name 非空
  规范化：kind 小写、name 去空白（保留大小写用于精确匹配）
  → 返回的 classifier 保证请求期 Classify 不会再因格式问题出错

请求期（Classify(userID, groups)）：
  if 配置了绑定但 userID 为空 → 返回 error（拒绝静默绕过）
  for 每条绑定：
      if 任一 subject 命中（User: name==userID；Group: name∈groups）→ 记下该绑定的 role
  去重 role（多条绑定可能授予同一角色）→ 写入 MatchedRules
```

#### 4.4.3 源码精读

启动期校验与规范化——注释强调"**此函数不报错返回，则请求期 Classify 必然正确**"，把所有可能出错的事前移到启动期：

> [pkg/classification/authz_classifier.go:57-105](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/classification/authz_classifier.go#L57-L105) —— `NewAuthzClassifier`：逐条校验，kind 小写、name trim。注意每个错误消息都附带"该怎么修"的提示（如"set the role field to the name used in decision conditions"）。

请求期的匹配与"无身份拒绝"——这是安全核心：

> [pkg/classification/authz_classifier.go:118-180](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/classification/authz_classifier.go#L118-L180) —— `Classify`：`userID=="" && len(bindings)>0` 即返回错误（[L123-127](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/classification/authz_classifier.go#L123-L127)）；匹配按"任一 subject 命中"（[L133-163](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/classification/authz_classifier.go#L133-L163)）；role 去重（[L165-168](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/classification/authz_classifier.go#L165-L168)）。

注意那个 `default` 分支的 `panic`——因为 kind 已在启动期校验过只能是 user/group，请求期若出现别的值就是编程 bug，于是"大声失败"而非悄悄忽略：

> [pkg/classification/authz_classifier.go:154-159](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/classification/authz_classifier.go#L154-L159) —— `default: panic(...)`，注释明说是 bug 而非运行时错误。

`AuthzResult` 只携带**匹配到的角色名**，刻意不包含权限——权限（选哪个模型）是决策引擎的事，分类器只负责"是谁"：

> [pkg/classification/authz_classifier.go:14-18](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/classification/authz_classifier.go#L14-L18) —— `AuthzResult{MatchedRules []string}`，注释"decision engine uses these to select models via modelRefs"。

**在信号编排里的接入**：`appendAuthzFromHeaders` 从 header 取身份、调 `Classify`、把结果写回 `SignalResults.MatchedAuthzRules`，并把错误透传给调用方（**不吞错**）：

> [pkg/classification/classifier_signal_authz.go:56-94](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/classification/classifier_signal_authz.go#L56-L94) —— 取 header（[L70-71](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/classification/classifier_signal_authz.go#L70-L71)）、`ParseUserGroups` 拆分组、`Classify`（[L73](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/classification/classifier_signal_authz.go#L73)）、错误即返回（[L81-85](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/classification/classifier_signal_authz.go#L81-L85)）。

值得注意的细节：authz 只在决策**真正引用**了 `authz` 信号时才求值（`isSignalTypeUsed`），这是 SR "只算用得着的信号"这一省算力惯例的体现；同时还有 `applyAuthzFailOpenOnClassifyError` 在分类层面提供一个可选的 fail-open 软开关（与限流链的 fail-open 类比，但层面不同）。

身份 header 名可配置，默认 `x-authz-user-id` / `x-authz-user-groups`：

> [pkg/config/config.go:113-131](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/config/config.go#L113-L131) —— `IdentityConfig` 与默认 header 名。

配置结构（`RoleBinding` 示例代码）：

> [pkg/config/signal_config.go:253-258](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/config/signal_config.go#L253-L258) —— `RoleBinding{Name, Description, Subjects[], Role}`。

```yaml
# 示例代码：authz 绑定，配合 decision 里 type:"authz", name:"vip" 使用
signals:
  authz:
    identity:
      user_id_header: "x-authz-user-id"
      user_groups_header: "x-authz-user-groups"
    role_bindings:
      - name: vip-binding
        description: "VIP 用户走高端模型"
        role: vip
        subjects:
          - { kind: Group, name: "premium" }
      - name: internal-binding
        role: internal
        subjects:
          - { kind: Group, name: "staff" }
```

#### 4.4.4 代码实践

**实践目标**：用现有单测确认匹配规则与"无身份拒绝"语义。

**操作步骤**（阅读型实践）：

1. 打开 `pkg/classification/authz_classifier_test.go`，找到 `TestAuthzClassifierClassify`（[authz_classifier_test.go:165](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/classification/authz_classifier_test.go#L165)），阅读它构造的绑定与断言的匹配角色。
2. 追踪一条链：决策里写 `authz("vip")` → 请求带 header `x-authz-user-groups: premium` → `appendAuthzFromHeaders` 取组 → `Classify` 命中 `vip-binding` → `MatchedRules=["vip"]` → 决策引擎 `matchesSignalType` 命中 → 路由到 vip 专属模型。
3. 构造反例（在临时测试里）：配置了绑定但**不发** `x-authz-user-id` 头，调用 `Classify("", nil)`，确认返回**非 nil error**，且错误消息提示"check authz.identity.user_id_header config"。

**需要观察的现象**：User 主体要求 name 精确等于 userID；Group 主体要求 name 出现在 groups 列表；多条绑定命中同一 role 会被去重；缺身份即报错而非返回空角色。

**预期结果**：匹配按"任一 subject 命中"；无身份报错。若无法执行，记为「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：两条绑定都把用户 `alice` 映射到 role `vip`，`Classify` 返回的 `MatchedRules` 里 `vip` 出现几次？

**答案**：1 次。匹配后用 `roleSet` 去重（[authz_classifier.go:165-168](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/classification/authz_classifier.go#L165-L168)），因为下游决策只关心"是不是 vip"，不关心有几条绑定授予它。

**练习 2**：为什么 `NewAuthzClassifier` 要在启动期把 `kind` 小写、把校验全做完，而不是请求期再做？

**答案**：把所有"可能因配置格式而出错"的事前移到启动期，让请求期的 `Classify` 成为一条"保证成功"的快路径（注释原话：[L44-45](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/classification/authz_classifier.go#L44-L45)）。这样请求期不需要重复 normalize 字符串、不会因格式问题在热路径上报错，符合"启动严格、运行快速"的分级原则（与 u4-l1 的启动序列设计一致）。

---

## 5. 综合实践

把四类治理控制串成一次完整的"请求生命周期"追踪，验证它们各自在哪一步生效、相互如何配合。

**任务**：阅读以下四个调用点，画出一次"命中限流→被拒"和一次"正常完成→更新统计"的时序，并标注每类控制出现的位置。

1. **请求阶段**（`processor_req_body_prepare.go` 的 `runRequestPreRoutingStages` → `applyRateLimitAndCacheChecks`）：
   - 先做决策求值（含 authz 信号，[L109-121](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/extproc/processor_req_body_prepare.go#L109-L121)）；authz 失败 → 403。
   - 选定模型后 `inflight.Begin` 入册（[L124](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/extproc/processor_req_body_prepare.go#L124)）。
   - `RateLimiter.Check`（[L161-183](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/extproc/processor_req_body_prepare.go#L161-L183)）→ 拒绝则回 429 并 `inflight.End` 出册（[L139-140](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/extproc/processor_req_body_prepare.go#L139-L140)）。
2. **响应阶段**（`processor_res_header_runtime.go` / `processor_res_body_streaming.go` / `processor_res_usage.go`）：
   - 测 TTFT → `UpdateTTFT` + `EstimateCacheProbability` 写入 `ctx.CacheWarmthEstimate`。
   - `RateLimiter.Report` 补报真实 token（[processor_res_usage.go:91-92](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/extproc/processor_res_usage.go#L91-L92)）。
   - `inflight.End` 出册。

**交付物**：

- 一张时序图，横轴是请求时间线，标出 authz（请求早期，403）、ratelimit（请求中期，429）、inflight Begin/End（包裹整个后端调用）、TTFT+warmth（响应首块）、Report（响应收尾）五个事件。
- 一段文字回答：为什么 `inflight.Begin` 在限流检查**之前**就开始了，而被限流拒绝时又必须立刻 `End`？（提示：Begin 的位置紧贴模型选定，限流是其后的一道闸门；拒绝意味着这次"在途"根本不会真正打到后端，必须立即出册，否则会让在途数虚高。）
- 一段文字回答：缓存热度为什么只能影响"下一次"选择而不能影响"这一次"？（提示：它依赖 TTFT，而 TTFT 要到响应阶段才测得到。）

**预期结果**：能清晰说明四类控制各自的发生时机与相互顺序，并解释两个"为什么"。运行/执行类步骤若无法本地完成，相关断言记为「待本地验证」。

## 6. 本讲小结

- **限流链**采用 first-deny 语义：多 provider 顺序 `Check`，任一拒绝即短路回 429；全放行则合并取最严格的 `remaining/limit`。provider 自身抛错时由 fail-open/closed 决定，但"明确拒绝"永远优先于"错误"。TPM 用"请求期估扣输入、响应期补扣输出"的两段式记账。
- **在途计数**靠时间戳老化自愈：`Begin` 记开始时间，`Get/Snapshot` 先驱逐超过 `DefaultMaxAge=10min` 的条目再计数，漏调 `End` 也不会永久泄漏。它是负载感知选择（`multi_factor`）与 Prometheus 指标 `llm_model_inflight_requests` 的唯一真相源。
- **缓存热度**用 TTFT 这一观测量估计 KV 缓存命中概率：原始分 `raw=(cold−ttft)/scale`（TTFT 越接近 warm 分位越热），再由"计数×分布×新鲜度"三因子 reliability 与先验 0.5 融合——证据不足时诚实回退到"不知道"。因依赖 TTFT，它只能影响下一次选择。
- **授权**套用 Kubernetes RoleBinding：Subject（用户/组）→ Role（信号名），决策用 `authz("role")` 命中。安全姿态是"无身份绝不静默放行"——配置了绑定却拿不到用户 ID 即报错（403），所有格式校验前移到启动期。
- 四者共享一套设计哲学：**请求期尽量薄、尽量安全**（authz 启动期校验、inflight 零值 no-op、限流拒绝优先）；**失败模式显式**（不吞错、不静默绕过、可观测）；**全局状态 + 包级读函数**（inflight、latency 都刻意采用此模式，免去把句柄穿过调用链）。

## 7. 下一步学习建议

- 若想看这些治理控制如何被**选择算法**消费，读 `pkg/selection/multi_factor.go`（消费 `inflight.Get`）与 `pkg/selection/latency_aware.go`（消费 TTFT 百分位），对应 u6-l2。
- 若想看 authz 的**凭证解析链**（`CredentialResolver`，与限流链结构对称），读 `pkg/authz/` 与 `buildCredentialResolver`（本讲的 [router_resolvers.go:25-61](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/extproc/router_resolvers.go#L25-L61)）。
- 若想看全部可观测性出口（指标、日志、追踪如何在 `main.go` 启动序列接入），继续学 u11-l4（可观测性）。
- 若想把限流/在途指标接到 Grafana，参考 `pkg/observability/metrics/`（本讲的 `inflight_collector.go` 是其中一个采集器范例）。
