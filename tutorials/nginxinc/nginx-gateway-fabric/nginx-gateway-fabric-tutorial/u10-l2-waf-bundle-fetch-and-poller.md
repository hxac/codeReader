# u10-l2 Bundle 拉取、轮询与下发

## 1. 本讲目标

本讲承接 [u10-l1](./u10-l1-wafpolicy-and-bundles.md)，把视角从「WAFPolicy 与 bundle 的数据模型」推进到「bundle 真正被拉下来、被周期检查、被推到数据面」的运行时链路。

读完本讲，你应当能够：

- 说清 `fetch.Fetcher` 接口的四个方法各自干什么，以及 HTTP/NIM/N1C 三种来源在 `HTTPFetcher` 内部如何被同一个 `dispatch` 分流。
- 说清条件请求（`If-None-Match` / `If-Modified-Since`）与「只取 checksum」两种省流量手段的区别与适用场景。
- 说清 `s3.Fetcher`（PLM 的 SeaweedFS 拉取器）与 `HTTPFetcher` 是两条独立路径，以及为什么 PLM **不经过 poller**。
- 说清一个 `poller`（每个 WAFPolicy 一个）如何用最小间隔 ticker 轮询多个 bundle 源、如何用 checksum 判变、如何在变化时把文件推给数据面 Deployment。
- 说清 `pollerManager` 如何管理所有 poller 的生命周期，以及 bundle 首次可用时如何通过 `WAFBundleReconcileEvent` 主动「戳」一下事件循环，触发一次完整的图重建与配置下发。

本讲最终会落到一张完整的「checksum 变化 → 数据面 Pod」通知链路图上。

## 2. 前置知识

本讲假设你已经读过：

- [u4-2 事件循环与批处理](./u4-l2-event-loop-and-batching.md)：知道 `eventCh` 是一条多源（控制器 + WAF poller）汇聚、单消费者（`EventLoop`）的管线，以及 `WAFBundleReconcileEvent` 是第三种事件类型。
- [u4-3 EventHandler 编排](./u4-l3-event-handler-orchestration.md)：知道 `sendNginxConfig` 的「图 → 配置 → 生成文件 → 下发 Agent → 入队状态」编排，以及 `defer reconcileWAFPollers`。
- [u7-2 配置文件下发与 Deployment 管理](./u7-l2-config-file-push-and-deployments.md)：知道 `Deployment`、`configVersion`、`Broadcaster.Send`、`FileLock` 这套「推清单 + Agent 反拉 + 同步屏障」的语义。
- [u10-l1 WAFPolicy 与 Bundle 模型](./u10-l1-wafpolicy-and-bundles.md)：知道四种来源（HTTP / NIM / N1C / PLM）、各自的鉴权头、bundle 用统一 SHA-256 校验、PLM 由 APPolicy/APLogConf CRD 驱动。

几个本讲反复用到的术语，先集中解释：

- **bundle**：编译后的 WAF 规则包（`.tgz`，对应 NGINX App Protect v5）。CRD 本身只是「指路牌」，真正的规则在 bundle 里。
- **checksum**：bundle 字节的 SHA-256（小写十六进制）。它是「这个 bundle 变没变」的唯一裁判。
- **条件请求（conditional GET）**：HTTP 协议里用 `If-None-Match`（带 ETag）或 `If-Modified-Since`（带时间）问服务端「自我上次拉取后，内容变了吗」，没变则服务端回 `304 Not Modified` 且不传正文，省带宽。
- **PLM**：Policy Lifecycle Manager，一种把 WAF 规则托管在 S3 兼容存储（SeaweedFS）里、由集群内 CRD（`APPolicy`/`APLogConf`）事件驱动的来源。**PLM 是事件驱动，不是轮询。**

## 3. 本讲源码地图

| 文件 | 角色 | 一句话职责 |
|---|---|---|
| `internal/framework/waf/fetch/fetch.go` | 拉取器（HTTP/NIM/N1C） | 定义 `Fetcher` 接口与 `HTTPFetcher` 实现，把三种来源统一成「拉取 + 校验 + 重试 + 条件请求」 |
| `internal/framework/waf/fetch/s3/s3.go` | 拉取器（PLM/S3） | 定义 `s3.Fetcher`，专门从 SeaweedFS 拉 bundle，**不实现 `Fetcher` 接口、不被 poller 使用** |
| `internal/framework/waf/poller/poller.go` | 单策略轮询器 | 每个 WAFPolicy 一个 `poller`，周期拉取、判变、推数据面 |
| `internal/framework/waf/poller/manager.go` | 轮询器总管 | `pollerManager` 管理所有 poller 生命周期，并在首次拿到 bundle 时注入 `WAFBundleReconcileEvent` |

配套但不在本讲重点的衔接点：

- [`internal/framework/events/event.go:27-33`](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/events/event.go#L27-L33)：`WAFBundleReconcileEvent` 的定义。
- [`internal/controller/handler.go:342-423`](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler.go#L342-L423)：`reconcileWAFPollers`，每轮事件都调一次来调和 poller。
- [`internal/controller/manager.go:393-427`](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L393-L427)：`createWAFPollerManager`，把 fetcher、DeploymentStore、statusQueue、eventCh 装配进 manager。

## 4. 核心概念与源码讲解

### 4.1 Fetcher 接口：HTTPFetcher 如何拉取并校验 bundle

#### 4.1.1 概念说明

`Fetcher` 是 WAF 子系统对「把一个 bundle 从远端拉到内存」这件事的统一抽象。它只关心四件事：拉策略 bundle、拉日志 profile bundle、只取策略 bundle 的 checksum、只取日志 profile bundle 的 checksum。

关键设计是「**同一份代码处理三种来源**」。`spec.type` 决定了走哪种协议（见 u10-l1），但在 fetcher 内部，HTTP / NIM / N1C 被收敛到一组 `Request` 字段 + 一个 `dispatch` 开关。这样上层（poller）只需要调 `FetchPolicyBundle`，不需要关心对方是 NGINX Instance Manager 还是 NGINX One Console。

另一个关键设计是「**判变优先于下载**」。bundle 可能很大，而绝大多数轮询周期里它并没有变化。所以 fetcher 提供了两条省流量的路：

1. **HTTP 来源**：用条件请求（ETag / Last-Modified），服务端回 `304` 就当作没变，正文不传。
2. **NIM / N1C 来源**：先调一个「只返回 hash、不返回正文」的元数据接口，hash 没变就根本不下载正文。

这两条路是后面 poller 能高频轮询却几乎不耗带宽的基础。

#### 4.1.2 核心流程

`FetchPolicyBundle` 的执行链（`FetchLogProfileBundle` 同构）：

```
FetchPolicyBundle(ctx, req)
  └─ fetch(ctx, req, dispatch)            # 统一入口
       ├─ validateAndNormalizeRequest     # 互斥校验 + checksum 归一化为小写
       ├─ buildHTTPClient                 # 默认 client；有 CA/insecure 时另建并 CloseIdleConnections
       ├─ wait.ExponentialBackoff(        # 重试外壳：只重试 transient 错误
       │     dispatch ──► switch {
       │         N1C.Namespace != ""  -> fetchN1C        # 异步编译 + 轮询状态 + 下载
       │         PolicyName/PolicyUID -> fetchNIM        # 先元数据定 version，再下载
       │         default              -> fetchHTTP       # 条件 GET + 可选 .sha256 校验
       │     }
       │  )
       └─ ExpectedChecksum 终检           # 调用方硬性校验（若设置了）
```

重试策略用 `k8s.io/apimachinery/pkg/util/wait` 的指数退避，参数固定：

\[
\text{delay}_n = \min\!\big(30\text{s},\; 1\text{s} \times 2^{\,n}\big) \quad (\text{含抖动}),\quad n \in [0,\, \text{RetryAttempts}]
\]

即基础 1s、倍率 2、上限 30s、步数 `RetryAttempts+1`（保证哪怕 `RetryAttempts=0` 也至少试一次）。

判错分两类，这是能否重试的红线：

- **transient（可重试）**：网络错误、HTTP 5xx。
- **non-transient（不重试）**：HTTP 4xx、checksum 不匹配。后者用专门的 `nonTransientError` 包装，重试循环一遇到就立刻退出不再尝试。

#### 4.1.3 源码精读

**接口定义**——四个方法，两两对称（策略 / 日志 × 全量 / 仅 checksum）：

[`internal/framework/waf/fetch/fetch.go:166-188`](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/waf/fetch/fetch.go#L166-L188) 定义 `Fetcher` 接口。注意注释里写明：`FetchPolicyBundleChecksum` 只支持 NIM/N1C，对纯 HTTP 来源直接返回错误（HTTP 该用条件请求）。

**结果与请求结构**：

[`fetch.go:71-77`](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/waf/fetch/fetch.go#L71-L77) 的 `Result` 同时承载 `Data`（正文）、`Checksum`、`ETag`/`LastModified`（供下次条件请求回带）、`Unchanged`（304 标志）。

[`fetch.go:90-136`](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/waf/fetch/fetch.go#L90-L136) 的 `Request` 把三种来源的字段都塞进一个结构：`URL`（HTTP 基址）、`NIM`（PolicyUID）、`N1C`（Namespace/各种 ObjectID）、`PolicyName`/`LogProfileName`（按名查）、`ETag`/`LastModified`（条件请求）、`ExpectedChecksum`/`VerifyChecksum`（两种校验，互斥）、`RetryAttempts`、`TLSCAData`、`InsecureSkipVerify`。

**三分流**——一个 switch 决定走哪条路：

[`fetch.go:380-389`](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/waf/fetch/fetch.go#L380-L389) 的 `dispatch`：`N1C.Namespace != ""` 走 N1C，`PolicyName`/`PolicyUID` 走 NIM，否则纯 HTTP。判定顺序很重要——N1C 优先。

**重试外壳**：

[`fetch.go:316-378`](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/waf/fetch/fetch.go#L316-L378) 的 `fetch`：用 `errors.As(fetchErr, &nte)` 识别 `nonTransientError`，命中就 `return false, fetchErr` 立即终止重试；其余错误记为 `lastErr` 继续退避。重试耗尽后用 `wait.Interrupted` 区分「重试耗尽」与「真错误」。末尾还有一道 `ExpectedChecksum` 终检（仅当 `!result.Unchanged`）。

> ⚠️ 注意源码里的 [`FIXME(ciarams87)` 注释（fetch.go:337-340）](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/waf/fetch/fetch.go#L337-L340)：重试循环是**同步**跑在 `Process()` 里的，会阻塞事件处理器，拖慢所有 NGINX 配置下发直到重试完成。这是已知的待优化点（计划改为异步）。

**条件请求（HTTP 省流量路 1）**：

[`fetch.go:477-534`](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/waf/fetch/fetch.go#L477-L534) 的 `fetchHTTP`：`req.ETag` 非空就带 `If-None-Match`，否则 `req.LastModified` 非空就带 `If-Modified-Since`（ETag 优先）。`doGetWithHeaders` 同时接受 `200` 和 `304` 为成功；拿到 `304` 就返回 `Result{Unchanged: true}`，并且**仍把响应里新的 ETag/Last-Modified 带回去**——这样服务端轮换 token 时不会丢。`VerifyChecksum=true` 时会额外 GET 一个 `<url>.sha256` 旁车文件做校验，校验失败包成 `nonTransientError`。

**只取 checksum（NIM/N1C 省流量路 2）**：

[`fetch.go:1388-1394`](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/waf/fetch/fetch.go#L1388-L1394) 的 `Request.SupportsChecksumOnlyFetch()`：NIM 策略、N1C 策略与日志都返回 `true`（有元数据接口），但 **NIM 日志 profile 返回 `false`**——因为它没有元数据接口，只能下全量再算 hash。这个布尔位是 poller 决定「先探 hash 还是直接下载」的开关。

**鉴权头**：

[`fetch.go:1408-1464`](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/waf/fetch/fetch.go#L1408-L1464) 的 `doGetWithHeaders`：按 `APIToken`（N1C，`Authorization: APIToken …`）→ `BearerToken`（NIM）→ `Username`（HTTP Basic）的优先级设置鉴权头，呼应 u10-l1 的鉴权矩阵。

#### 4.1.4 代码实践

**目标**：用源码阅读 + 单测断言，确认「条件请求」与「只取 checksum」两条省流路径的行为差异。

**操作步骤**：

1. 打开 [`internal/framework/waf/fetch/fetch.go`](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/waf/fetch/fetch.go)，定位 `fetchHTTP`（L477）与 `dispatchChecksum`（L278）。
2. 阅读 `fetchHTTP` 里 `statusCode == http.StatusNotModified` 分支，确认 `304` 时返回 `Unchanged: true` 且不读正文。
3. 对照 `poller.go` 里 `SupportsChecksumOnlyFetch()` 的两个调用点（见 4.3），看 NIM/N1C 走 `FetchPolicyBundleChecksum`、HTTP 走带 ETag 的全量请求。
4. 在 `internal/framework/waf/fetch/` 下找针对 `fetchHTTP` 与 `fetchNIMChecksum` 的测试（文件名形如 `fetch_test.go`），读其中「服务端返回 304」「hash 未变」的用例，看断言如何验证 `Result.Unchanged` 与「未发起正文下载」。

**需要观察的现象**：`fetchHTTP` 在 304 分支不调用 `io.ReadAll`；`fetchNIMChecksum` 只解析 JSON 元数据、不解 base64 正文。

**预期结果**：你能用一句话说清——HTTP 来源靠 304 省正文、NIM/N1C 来源靠「先查 hash」省正文，而 NIM 日志 profile 因无元数据接口只能下全量。

**待本地验证**：若你想实际跑测试，执行 `cd internal/framework/waf/fetch && go test -run TestFetchHTTP -v`（具体用例名以仓库当前测试为准）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ExpectedChecksum` 与 `VerifyChecksum` 设计成互斥？

**参考答案**：两者都是「下载后校验」手段，但来源不同——`ExpectedChecksum` 是调用方在 CRD 里写死的 hash，`VerifyChecksum` 是去拉一个 `.sha256` 旁车文件。同时设置会出现两个相互独立的期望值，语义混乱且可能冲突。`validateAndNormalizeRequest` 因此只允许其一；且 `VerifyChecksum` 仅对纯 HTTP 有意义（NIM/N1C 用平台返回的 hash 自动校验）。

**练习 2**：一个 N1C 来源的拉取返回了 HTTP 502，会重试吗？返回 404 呢？

**参考答案**：502 是 5xx，属于 transient，会按指数退避重试最多 `RetryAttempts` 次。404 是 4xx，`doGetWithHeaders` 把它包成 `nonTransientError`，重试循环 `errors.As` 命中后立即返回，**不重试**。

---

### 4.2 S3 拉取：PLM 的 SeaweedFS fetcher

#### 4.2.1 概念说明

`s3.Fetcher` 是一个**独立的、专用的**拉取器，只服务 PLM 来源（见 u10-l1：PLM 把 bundle 托管在 S3 兼容的 SeaweedFS 里）。它和 4.1 的 `Fetcher` 接口**没有任何关系**——它不实现那个接口，方法签名也完全不同。

最容易踩的坑就在这里：**S3 fetcher 不被 poller 使用**。原因是 PLM 是「事件驱动」而非「轮询驱动」：

- PLM 的 bundle 变更由集群里的 `APPolicy` / `APLogConf` CRD 状态变化触发（PLM 组件编译完会更新这些 CRD 的 status，带上 S3 location 与 sha256）。
- 因此图构建阶段（`BuildGraph` → `fetchPLMPolicyBundle`）会**同步**调用 `s3.Fetcher.FetchBundle`，而不是起一个 poller 定时去问 S3。

证据在 `reconcileWAFPollers` 的开头：

```go
// PLM policies use event-driven watches, not polling.
if wafPolicy.Spec.Type == ngfAPI.PolicySourceTypePLM {
    continue
}
```

（[`handler.go:359-362`](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler.go#L359-L362)）

所以本节讲的是「PLM 专用通道」，它与 4.3 的 poller 是平行关系，不是上下游。

#### 4.2.2 核心流程

`FetchBundle` 的执行链：

```
FetchBundle(ctx, location, expectedSHA256, creds, tlsCfg)
  ├─ parseS3URI(location)        # s3://bucket/key → bucket, key
  ├─ buildClient(creds, tlsCfg)  # 每次调用新建一个 S3 client
  │     ├─ validateTLSConfig     # CA/证书对成对校验
  │     ├─ 静态凭证 或 匿名凭证
  │     ├─ buildTLSTransport     # 有 CA/insecure/mTLS 时克隆 DefaultTransport
  │     └─ region="us-east-1", UsePathStyle=true   # SeaweedFS 不关心 region、需 path-style
  ├─ client.GetObject(bucket, key)
  ├─ io.ReadAll(body)
  └─ expectedSHA256 != "" → SHA-256 比对（大小写不敏感）
```

两个值得注意的工程取舍：

1. **每次调用新建 client**。注释明确写：「fetches are infrequent and event-driven, so client caching is unnecessary」。PLM 拉取低频且由事件驱动，没必要复用连接池。
2. **region 写死 `us-east-1`、`UsePathStyle=true`**。因为对面是 SeaweedFS，不是真 AWS S3，它不校验 region、且要求 path-style 寻址（`host/bucket/key` 而非 `bucket.host/key`）。

#### 4.2.3 源码精读

**结构与构造**——endpoint 与 skipVerify 在启动时由 CLI flag 固定：

[`internal/framework/waf/fetch/s3/s3.go:44-58`](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/waf/fetch/s3/s3.go#L44-L58)。注意 `Fetcher` 只有 `FetchBundle` 一个方法，与 `fetch.Fetcher` 接口的四个方法截然不同——这从类型层面就堵死了「误把 S3 fetcher 塞进 poller」的可能。

**主方法**：

[`s3.go:65-123`](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/waf/fetch/s3/s3.go#L65-L123) 的 `FetchBundle`：解析 URI → 建 client → `GetObject` → `ReadAll` → 可选 SHA-256 校验。`creds` 为 `nil` 时走匿名访问（`aws.AnonymousCredentials{}`），`tlsCfg` 为 `nil` 时用系统 CA。

**URI 解析**：

[`s3.go:126-157`](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/waf/fetch/s3/s3.go#L126-L157) 的 `parseS3URI`：要求 `s3://` 前缀，且必须有 bucket 和 key（拒绝 `s3://bucket/` 这种尾斜杠空 key）。

**TLS 与 mTLS**：

[`s3.go:217-254`](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/waf/fetch/s3/s3.go#L217-L254) 的 `buildTLSTransport`：克隆 `http.DefaultTransport` 以保留默认代理/超时/连接复用；CA 追加进系统 cert 池；同时提供 `CertData`+`KeyData` 时装配客户端证书做 mTLS。`InsecureSkipVerify` 由 CLI flag 控制（代码里 `//nolint:gosec` 标注为有意识行为）。

**装配点**——S3 fetcher 在控制面启动时单独创建：

[`internal/controller/manager.go:1169-1183`](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L1169-L1183) 的 `createPLMFetcher`：仅当 `cfg.PLMStorageConfig != nil`（即配了 `--plm-storage-*` flag）才创建，否则返回 `(nil, nil)`。它和 `createWAFFetcher`（[manager.go:1163-1165](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L1163-L1165)，返回 `fetch.NewHTTPFetcher`）是两条互不相交的装配线。两个 fetcher 分别注入 `ChangeProcessorConfig` 的 `WAFFetcher` 与 `PLMFetcher` 字段（[manager.go:179-180](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L179-L180)）。

#### 4.2.4 代码实践

**目标**：用调用链跟踪，确认「PLM bundle 在图构建阶段被同步拉取，而非由 poller 轮询」。

**操作步骤**：

1. 从 [`internal/controller/state/graph/policies.go`](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/policies.go) 的 `fetchPLMPolicyBundle`（约 L1368）出发，确认它直接调用 `wafInput.PLMFetcher.FetchBundle(...)`（约 L1420）。
2. 顺着 `fetchPLMPolicyBundle` 的调用者回溯到 `BuildGraph`，确认这条拉取发生在**图构建期间**（即 `processor.Process()` 内），而不是任何 ticker 循环里。
3. 对比 `HTTPFetcher` 的使用方式：在 [`internal/controller/manager.go:220`](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L220)，`wafFetcher`（HTTPFetcher）被传给 `createWAFPollerManager`，进入 poller 路径；而 `plmFetcher` 只进 ChangeProcessor。

**需要观察的现象**：`PLMFetcher.FetchBundle` 的调用栈里没有任何 `time.Ticker`、没有 `poller.run`，只有图构建的同步调用。

**预期结果**：你能画出一句话结论——**HTTP/NIM/N1C = poller 异步轮询；PLM = 图构建同步拉取**。这正是 `reconcileWAFPollers` 里 `PolicySourceTypePLM` 直接 `continue` 的根因。

#### 4.2.5 小练习与答案

**练习 1**：既然 PLM 由 CRD 事件驱动，那「bundle 变了」这件事是怎么被 NGF 察觉的？

**参考答案**：PLM 组件（独立运行的控制器）把新编译的 bundle 写入 SeaweedFS 后，会更新 `APPolicy`/`APLogConf` CRD 的 `status`（带上新的 S3 location 与 sha256）。NGF watch 这些 CRD，status 变化触发 `UpsertEvent` → 图重建 → `fetchPLMPolicyBundle` 用新的 location 重新拉取。所以「触发器」是 CRD status 变更事件，而不是定时轮询。

**练习 2**：为什么 `s3.Fetcher` 每次 `FetchBundle` 都新建一个 client，而 `HTTPFetcher` 复用 `defaultClient`？

**参考答案**：PLM 拉取低频且由事件驱动，新建 client 的成本可忽略，复用反而要管理连接池生命周期；而 HTTPFetcher 服务 poller，轮询相对高频，复用 `defaultClient` 能利用连接复用降低开销。两种取舍都匹配各自的访问频率。

---

### 4.3 poller 轮询管理：从单源轮询到多策略生命周期

#### 4.3.1 概念说明

poller 子系统负责「**周期性地**检查 bundle 有没有变、变了就推给数据面」。它只服务非 PLM 来源（HTTP/NIM/N1C），分两层：

- **`poller`**（poller.go）：每个 WAFPolicy 一个。一个 WAFPolicy 可能同时有 1 个策略 bundle + N 个日志 profile bundle，所以一个 poller 内部管着 `[]BundleSource`，用**所有源里最小的 interval** 作为统一 ticker，再按各自 interval 决定这一拍要不要拉某个源。
- **`pollerManager`**（manager.go）：全局唯一，管理所有 poller 的生老病死——创建、按需重启、停止；同时是 poller 与「事件循环 / 状态队列 / 图缓存」之间的桥。

判变的核心永远是 checksum：把本次拉到的 checksum 与上次记录的比对，相同就什么都不做，不同才推送。这保证了「高频轮询、低频实际下发」。

#### 4.3.2 核心流程

**单次轮询一个源**（`pollSource`）的判定树：

```
pollSource(src):
  last = bundleStates[src.BundleKey]          # 上次的 checksum + ETag/LastModified
  if src.Request.SupportsChecksumOnlyFetch(): # NIM/N1C
      changed = checksumChanged(...)          # 只取远端 hash
      if not changed: reportStatus(ok); return
  result = downloadBundle(src, last)          # 全量下载（HTTP 带条件请求头）
  if result.Unchanged (304): 保存 token; return
  if result.Checksum == last.checksum:        # 内容同、token 轮换
      保存 token; return
  # 真的变了 ↓
  pushBundleToDeployments(bundleKey, result.Data)   # 推数据面
  bundleUpdateCallback(bundleKey, data, checksum)   # → manager 缓存 + 可能注入事件
  saveBundleState(...)
  reportStatus(newChecksum, nil)
```

**poller 主循环**（`run`）：启动时**立刻**把所有源各拉一次（不等第一个 tick），然后 `time.NewTicker(minInterval)`，每拍检查「距离上次拉这个源是否已满它的 interval」。

**manager 的生命周期判定**（`ReconcilePoller`）：

```
ReconcilePoller(cfg):
  if 存在 poller 且 sources 没变 (reflect.DeepEqual):
      只更新 targetDeployments             # 不重启，避免无谓 churn
  else:
      startPoller (停旧的、起新的)
```

`startPoller` 会先 `cancel` 掉旧 poller、清掉它名下的缓存与错误，再用新 sources 起一个。poller 退出时有个「只有仍是同一个 poller 才删 map 项」的守卫（防止被替换后误删新 poller）。

**首次可用触发图重建**（这是最关键的一环）：当某个 bundle **第一次**被成功缓存进 manager 的 `bundleCache` 时，manager 会往 `eventCh` 投一个 `WAFBundleReconcileEvent`。这条事件被 `EventLoop` 批处理后，handler 调 `processor.ForceRebuild()` 强制重建图——因为此时 bundle 从「没有」变成「有」，图里该策略的 `BundlePending` 才能被清除，fail-closed 才能放开流量。

#### 4.3.3 源码精读

**BundleSource 与状态**：

[`internal/framework/waf/poller/poller.go:33-44`](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/waf/poller/poller.go#L33-L44) 的 `BundleSource` 把「拉什么（`Request`）」「怎么标识（`BundleKey`）」「多久拉一次（`Interval`）」「是策略还是日志（`Type`）」打包。[`poller.go:48-52`](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/waf/poller/poller.go#L48-L52) 的 `bundleState` 记录上次 checksum 与条件请求 token。

**主循环**：

[`poller.go:119-167`](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/waf/poller/poller.go#L119-L167) 的 `run`：选 `minInterval`（非法则回退 `defaultPollingInterval = 5 * time.Minute`，见 [L22](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/waf/poller/poller.go#L22)）；启动时 `for _, src := range p.sources { p.pollSource(ctx, src) }` 立即拉一次；之后 `select { ctx.Done | ticker.C }`，每拍按 `now.Sub(lastPoll[key]) >= src.Interval` 判定。

**判变与推送**：

[`poller.go:215-260`](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/waf/poller/poller.go#L215-L260) 的 `pollSource`：先 `SupportsChecksumOnlyFetch()` 走 [`skipIfChecksumUnchanged`（L265-279）](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/waf/poller/poller.go#L265-L279)；否则全量下载。三道「没变」的早退（304 / checksum 相同 / 仅 token 轮换）都只 `saveBundleState` 不推送；只有真变化才 `pushBundleToDeployments` + `bundleUpdateCallback`。`saveBundleState`（[L307-322](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/waf/poller/poller.go#L307-L322)）有个细节：响应里没带新 token 时**保留旧 token**，避免服务端偶尔省略 validator 导致后续全量 GET。

**推数据面**：

[`poller.go:360-391`](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/waf/poller/poller.go#L360-L391) 的 `pushBundleToDeployments`：对每个目标 Deployment，持 `deployment.FileLock.Lock()` → `deployment.UpdateWAFBundle(bundlePath, data)` → `GetBroadcaster().Send(msg)` → `Unlock()`。这里的 `FileLock` 与 u7-2 主配置下发用的是**同一把锁**，保证「bundle 文件更新」与「主配置下发」不会交错破坏文件视图一致性。`bundlePath` 由 `config.GenerateWAFBundleFileName(WAFBundleID(bundleKey))` 生成。`UpdateWAFBundle`（[deployment.go:224-253](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/deployment.go#L224-L253)）按路径查找替换或追加，仅在内容 hash 变化时返回非 nil 广播消息——**内容没变就连 Send 都不调用**。

**Source 构建**：

[`poller.go:395-446`](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/waf/poller/poller.go#L395-L446) 的 `BuildBundleSources`：遍历 `spec.PolicySource` 与每个 `spec.SecurityLogs`，**只把 `Polling.Enabled == true` 的源**收进切片。这解释了「为什么没开 polling 的策略不会有 poller」——`sources` 为空时 manager 直接不起 poller。

**manager 生命周期**：

[`manager.go:144-163`](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/waf/poller/manager.go#L144-L163) 的 `ReconcilePoller`：用 `sourcesEqual`（即 `reflect.DeepEqual`，[poller.go:202-204](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/waf/poller/poller.go#L202-L204)）判定是否需要重启。[`manager.go:430-458`](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/waf/poller/manager.go#L430-L458) 的 `StopPollersNotIn`：每轮 `reconcileWAFPollers` 末尾调用，停掉所有不在 `activePolicies` 集合里的 poller——这是删除 WAFPolicy 后回收 poller 的机制。

**首次可用注入事件**：

[`manager.go:305-336`](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/waf/poller/manager.go#L305-L336) 的 `cacheBundleUpdate`：写缓存前先记 `_, alreadyCached := m.bundleCache[bundleKey]`；**首次**（`!alreadyCached`）才构造 `WAFBundleReconcileEvent`。关键细节是「**先释放锁再发事件**」——用 `select { case m.eventCh <- *event: case <-m.ctx.Done(): }`，`m.ctx.Done()` 是关闭时的逃生通道，防止 poller goroutine 在已排空的事件 channel 上无限阻塞。注释也诚实说明：poller 重启清缓存后再次首抓也会触发该事件，属「无害的多余重建」。

**handler 侧消费事件**：

[`internal/controller/handler.go:854-872`](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler.go#L854-L872)：`WAFBundleReconcileEvent` 分支先做防陈旧守卫（`HasPoller` 为假就跳过——策略可能已被删），再 `processor.ForceRebuild()`。注释点明**不调 `CaptureUpsertChange`**，否则会用元数据 stub 覆盖真实策略对象、污染下次建图。

**状态回流**：

- [`handler.go:730-772`](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler.go#L730-L772) `mergeWAFPollErrors`：读 `GetAllPollErrors()`，给「之前成功过、这次轮询失败」的策略加 `StaleBundleWarning` 条件（旧 bundle 仍兜底）。
- [`handler.go:777-817`](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler.go#L777-L817) `mergeWAFBundleUpdates`：读 `GetAllBundleUpdates()`，给「轮询检测到变化并已派发」的策略加 `BundleUpdated` 条件，且只覆盖「健康」状态、不掩盖错误。

#### 4.3.4 代码实践

**目标**：通过阅读 `manager_test.go` / `poller_test.go`，验证「首次缓存注入事件」与「内容不变不广播」两个关键行为。

**操作步骤**：

1. 打开 `internal/framework/waf/poller/manager_test.go`，找针对 `cacheBundleUpdate` 或 `WAFBundleReconcileEvent` 的用例，看测试如何断言「第一次写缓存时 eventCh 收到一个事件、第二次写同一 key 时不产生事件」。
2. 打开 `internal/framework/waf/poller/poller_test.go`，找 `pushBundleToDeployments` 或 `pollSource` 的用例，确认当 fake fetcher 返回相同 checksum 时 `Broadcaster.Send` 的调用次数为 0。
3. 注意测试里大量使用 counterfeiter 生成的 fake（`pollerfakes/fake_manager.go`、`fetch/fakes` 等，由源码顶部 `//go:generate go tool counterfeiter -generate` 驱动），这是 NGF 标准的单测替换手段（见 u13-l1）。

**需要观察的现象**：首次 `cacheBundleUpdate` 后 `eventCh` 多一条 `WAFBundleReconcileEvent`；checksum 相同时 `Send` 调用计数不增加。

**预期结果**：你能解释这两个「幂等」保证——(a) 事件只在首抓注入，避免每轮轮询都重建图；(b) 内容不变时连广播都不发，避免无谓 reload。

**待本地验证**：具体用例名以仓库当前测试为准；可执行 `cd internal/framework/waf/poller && go test ./... -run TestPoller -v` 观察。

#### 4.3.5 小练习与答案

**练习 1**：一个 poller 有两个源，interval 分别是 5 分钟和 10 分钟，ticker 间隔是多少？10 分钟那个源多久被拉一次？

**参考答案**：ticker 间隔取最小值 5 分钟。但 10 分钟那个源每两拍才被拉一次——因为 `run` 里用 `now.Sub(lastPoll[key]) >= src.Interval` 判定，5 分钟一拍时该源只满足了一半间隔，要等第二拍（累计 10 分钟）才触发。所以最小 interval 决定「检查频率」，各源 interval 决定「实际拉取频率」。

**练习 2**：`reconcileWAFPollers` 为什么每轮事件都用 `defer` 调用（见 u4-3），而不是只在成功下发后调？

**参考答案**：用 `defer` 保证无论 `sendNginxConfig` 从哪个早返回分支（无 Gateway、Gateway 无效等）退出，poller 都被调和。否则被删除或失效策略的 poller 会因为没走到末尾而泄漏——`StopPollersNotIn` 不会被调用，旧 poller 的 goroutine 与缓存会一直留着。

**练习 3**：bundle 第一次可用时，为什么 poller 既「直接推数据面」又「注入事件触发图重建」？两者不重复吗？

**参考答案**：不重复，各管一段。直接推数据面解决「bundle 文件本身」的更新（让数据面有新文件）；注入事件触发图重建解决「配置里对 bundle 的引用」——bundle 第一次出现前，图里该策略是 `BundlePending`、配置是 fail-closed（ withhold ），必须重建图、重新生成并下发**主配置**才能让流量真正放行。文件推送管「数据」，图重建管「开关」，缺一不可。

---

## 5. 综合实践

**任务**：梳理「一个已轮询的 bundle 的 checksum 发生变化时，从 poller 到数据面 Pod 的完整通知链路」，并区分「首抓」与「后续变化」两条子路径。

请按下面的骨架，结合本讲引用的源码行号，把每一步的**函数名 + 文件:行号 + 做了什么**填上，最后画成一张时序图。

```
[数据源侧 checksum 变化]
   │
   ├─ A. poller 检测变化（poller.go: pollSource, L215）
   │     1. pollSource 读 last.checksum
   │     2. NIM/N1C: skipIfChecksumUnchanged → checksumChanged(远端 hash) 变了
   │        HTTP: downloadBundle 带条件请求头 → 非 304
   │     3. result.Checksum != last.checksum
   │
   ├─ B. 推数据面（poller.go: pushBundleToDeployments, L360）
   │     1. FileLock.Lock()
   │     2. deployment.UpdateWAFBundle(path, data) → rebuildFileOverviews
   │     3. Broadcaster.Send(msg) → Agent 反拉 GetFile → 写盘 → reload
   │     4. FileLock.Unlock()
   │
   ├─ C. 缓存 + 可能注入事件（manager.go: cacheBundleUpdate, L305）
   │     - 首抓(!alreadyCached): 注入 WAFBundleReconcileEvent 进 eventCh
   │     - 后续变化(alreadyCached): 只更新 bundleCache，不注入事件
   │
   ├─ D. 状态回流
   │     1. reportStatus → wrappedCallback → recordPollResult(BundleUpdate)
   │     2. statusCallback(manager.go:411) → statusQueue.Enqueue(UpdateAll)
   │     3. handler.mergeWAFBundleUpdates → BundleUpdated 条件
   │
   └─ E.（仅首抓）事件驱动的主配置重建
         1. EventLoop 批处理 WAFBundleReconcileEvent
         2. handler.parseAndCaptureEvent(handler.go:854) → ForceRebuild
         3. processor.Process → 重建图 → BundlePending 清除
         4. sendNginxConfig → 生成主配置 → UpdateConfig → Agent reload → 流量放行
```

**需要你判断的关键问题**：

1. 如果是「后续变化」（非首抓），上面哪一步（E）不会发生？为什么这不会导致数据面配置不一致？（提示：后续变化只改 bundle 文件内容，主配置里对该 bundle 文件路径的引用没变，所以只需推文件、不必重生成主配置。）
2. `Broadcaster.Send` 返回 `false`（无订阅者）时，bundle 还会被保存吗？（看 [poller.go:382-388](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/waf/poller/poller.go#L382-L388) 的日志分支：文件已存进 Deployment，只是暂时没人拉，等 Agent 订阅时会拿到初始配置。）
3. 整条链路里出现了几次 `FileLock`？为什么它们必须串行？（pushBundleToDeployments 一次；主配置下发一次。共用同一把锁，保证「bundle 文件」与「主配置文件」的视图不会交错。）

**预期产出**：一张包含 poller、pollerManager、EventLoop、handler、nginxUpdater、Agent 六个角色的时序图，并标注「首抓」与「后续变化」的分叉点在 `cacheBundleUpdate` 的 `alreadyCached` 判定处。

## 6. 本讲小结

- **两个 fetcher、两条路径**：`fetch.Fetcher`（`HTTPFetcher`）统一处理 HTTP/NIM/N1C，被 poller 异步轮询；`s3.Fetcher` 专处理 PLM，在图构建阶段被同步调用。PLM 因事件驱动而在 `reconcileWAFPollers` 里被直接 `continue`，不进 poller。
- **判变优先于下载**：HTTP 靠条件请求（`If-None-Match`/`If-Modified-Since` → 304），NIM/N1C 靠「只取 checksum」的元数据接口（`SupportsChecksumOnlyFetch`），NIM 日志 profile 因无元数据接口只能下全量。
- **重试只重 transient**：指数退避（1s→30s，倍率 2，含抖动）只对网络错误与 5xx 生效；4xx 与 checksum 不匹配包成 `nonTransientError` 立即失败。注意同步重试会阻塞事件处理器的已知 FIXME。
- **checksum 是唯一裁判**：poller 每源记 `bundleState`，本次 checksum 与上次相同则什么都不做（连广播都不发），不同才推送——这是「高频轮询、低频下发」的根本。
- **pollerManager 管生命周期 + 当桥**：`ReconcilePoller` 用 `reflect.DeepEqual` 判 sources 决定重启与否；`StopPollersNotIn` 回收已删策略的 poller；`cacheBundleUpdate` 在首抓时注入 `WAFBundleReconcileEvent` 主动触发图重建。
- **首抓 vs 后续变化**：首抓既要推文件、又要注入事件重建主配置（清 `BundlePending`、放开 fail-closed）；后续变化只推文件即可，因为主配置对 bundle 路径的引用没变。

## 7. 下一步学习建议

- **向下游走**：bundle 文件下发到数据面后，NGINX App Protect 如何加载它、生成器如何把 WAFPolicy 翻译成 NAP 指令——可阅读 `internal/controller/nginx/config/policies/waf/generator.go`，对应学习路线里的策略生成单元（u8-l3）。
- **向状态走**：`StaleBundleWarning` / `BundleUpdated` / `BundlePending` 等 condition 如何在 u8-l1 的状态队列里被异步写回资源，以及 leader 切换时 poller 的行为。
- **向并发走**：本讲多次出现的 `FileLock`、`sync.RWMutex`（`targetMu`/`stateMu`/`mu`）、`select { case ch <- : case <-ctx.Done() }` 都是 NGF 并发控制的典型手法，可在 u13-l2（并发与可靠性设计）系统梳理。
- **向测试走**：counterfeiter 生成的 `fake_manager` / `fake_fetcher` 是理解本讲行为的最佳入口，结合 u13-l1 掌握 NGF 的 fake + envtest 测试体系。
