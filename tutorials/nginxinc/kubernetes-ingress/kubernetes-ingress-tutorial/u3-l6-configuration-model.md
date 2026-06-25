# 内存模型 Configuration 与引用/冲突检查

## 1. 本讲目标

上一讲（u3-l5）我们看清了 `sync(task)` 如何按 `Kind` 把任务分发到 `syncIngress` / `syncVirtualServer` 等函数，并且知道这些函数会调用 `configuration.AddOrUpdateX(...)` 与 `configurator`。本讲我们就钻进这个被反复调用、却一直被「当作黑盒」的对象——`Configuration`。

读完本讲，你应该能够：

- 说清 `Configuration` 在内存里用哪些 map 同时维护「原始资源」和「计算后的 host 索引」，以及为什么要分两层。
- 跟踪一次 `AddOrUpdateVirtualServer` 从「写入 map」到「产出 `ResourceChange` 与 `ConfigurationProblem`」的完整路径。
- 解释两个资源抢占同一个 host 时，控制器如何判定输赢、如何上报冲突、为什么不会生成坏配置。
- 理解 `reference_checkers.go` 如何回答「Service / Secret / Policy 变了，哪些路由资源受影响」这个反向查询问题。

本讲是配置生成（第 4 单元）之前的最后一道「决策层」——它只决定「哪些资源该被生成配置、哪些该被拒绝」，不负责真正写 nginx.conf。

## 2. 前置知识

- **控制器三层结构**（承接 u3-l5）：感知层（Informer）→ 调度层（taskQueue）→ 调谐层（`sync` → `Configuration` → `Configurator`）。本讲讲的是调谐层里 `Configuration` 这一段。
- **Resource / Kind / Key**（承接 u3-l3）：task 只带 `{Kind, Key}`，调谐时用 Key 回查 Lister 拿到真实对象。
- **CRD 资源模型**（承接 u2-l1 / u2-l2）：Ingress、VirtualServer、VirtualServerRoute、TransportServer、GlobalConfiguration、Policy。本讲会频繁出现这些名字。
- **NGINX 的 host 概念**：一个 HTTP `server { server_name <host>; }` 块对应一个 host。NGINX 不允许两个 server 用完全相同的 `server_name + 端口`，所以控制器必须在生成配置前解决 host 冲突，否则 reload 会失败。

一个贯穿全讲的直觉：**`Configuration` 是控制器的「世界模型」**。它把集群里所有相关资源装进内存，并随时算出一份「谁拥有哪个 host」的裁决表。`Configurator` 只需要照着这份裁决表把赢家翻译成 nginx.conf 即可。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `internal/k8s/configuration.go` | 内存模型本体：`Configuration` 结构、`AddOrUpdate*` / `Delete*` 入口、`rebuildHosts` 重建与冲突裁决、`ResourceChange` / `ConfigurationProblem` 两个核心产物类型 |
| `internal/k8s/reference_checkers.go` | 反向引用查询：给定一个 Service / Secret / Policy，找出所有引用它的路由资源 |
| `internal/k8s/controller.go` | 调用方：`syncVirtualServer` 等把 `Configuration` 的产物交给 `processChanges` / `processProblems` |
| `internal/k8s/configuration_test.go` | 黄金测试，其中 `TestHostCollisions` 是本讲综合实践的事实依据 |

## 4. 核心概念与源码讲解

### 4.1 Configuration 结构：一个对象，两层 map

#### 4.1.1 概念说明

`Configuration` 是一个被 `sync.RWMutex` 保护的大结构体。它的字段可以分成两类 map，理解这两类的区别是理解整个文件的前提：

1. **原始资源仓库**（按资源 key 存原始对象）：`ingresses`、`virtualServers`、`virtualServerRoutes`、`transportServers`、`globalConfiguration`。它们是「用户写了什么」的忠实记录，key 是 `namespace/name`。
2. **计算后的索引**（按 host / listener 存「裁决结果」）：`hosts`（host → `Resource`）、`listenerHosts`（listener+host → TransportServer 配置）、`minionsByHost`（host → minion ingress 集合）。它们是「经过冲突裁决后，谁真正拥有这个 host」的结论，只能由 `rebuildHosts` 重新计算得出，不能手动改。

为什么要分两层？因为「用户写的资源」和「最终生效的资源」不是一一对应的：两个 VirtualServer 可能写了同一个 host，但只有一个能生效；一个 minion Ingress 没有 master 就不生效；一个 VirtualServerRoute 没有被任何 VirtualServer 引用就是孤儿。第二层 map 专门记录这些裁决结论。

此外还有两个「问题登记簿」：`hostProblems` 和 `listenerProblems`，记录冲突、孤儿、被忽略等 `ConfigurationProblem`，供上层回写 status。

#### 4.1.2 核心流程

构造期只建空 map 和各种 checker，不做任何计算：

```text
NewConfiguration(...)
  ├─ 初始化所有 map（hosts / ingresses / virtualServers / ...）
  ├─ 创建各 reference checker（secret / service / endpoint / policy / ...）
  └─ startupComplete = false   ← 关键：启动期标志
```

注意 `NewConfiguration` **不会**填充任何资源，也不会调用 `rebuildHosts`。资源的填入完全由后续的 `AddOrUpdate*` 驱动。

#### 4.1.3 源码精读

`Configuration` 结构体定义：[internal/k8s/configuration.go:379-431](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/configuration.go#L379-L431) —— 注意前两个字段 `hosts`、`listenerHosts` 是「裁决索引」，中间一组 `ingresses`/`virtualServers`/... 是「原始仓库」，末尾 `hostProblems`/`listenerProblems` 是「问题簿」，`lock sync.RWMutex` 守护并发访问。

`Resource` 接口是「能被放进 `hosts` 的东西」的抽象：[internal/k8s/configuration.go:46-52](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/configuration.go#L46-L52)。三种实现分别是 `IngressConfiguration`、`VirtualServerConfiguration`、`TransportServerConfiguration`，它们都实现了 `GetObjectMeta` / `GetKeyWithKind` / `Wins` / `AddWarning` / `IsEqual` 五个方法。

构造函数：[internal/k8s/configuration.go:434-482](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/configuration.go#L434-L482) —— 全是 `make(...)` 与 checker 装配，`startupComplete` 保持零值 `false`。

> 术语解释：**checker（引用检查器）** 是一组小对象，专门回答「某个 Service/Secret/Policy 是否被某个路由资源引用」。它们在 4.4 节详讲。

#### 4.1.4 代码实践

1. **实践目标**：建立「两层 map」的空间直觉。
2. **操作步骤**：打开 [internal/k8s/configuration.go:379-431](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/configuration.go#L379-L431)，把字段手动分成三组：(a) 裁决索引、(b) 原始仓库、(c) 问题簿。
3. **需要观察的现象**：哪些字段是 `map[string]Resource` / `map[listenerHostKey]*TransportServerConfiguration`（索引层），哪些是 `map[string]*conf_v1.VirtualServer`（仓库层）。
4. **预期结果**：你能指出 `hosts` 属于索引层、`virtualServers` 属于仓库层，并解释为什么 `hosts` 的 value 类型是接口 `Resource` 而仓库层是具体类型。
5. 待本地验证：无，纯阅读型实践。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `hosts` 的 value 是接口 `Resource`，而 `virtualServers` 的 value 是具体类型 `*conf_v1.VirtualServer`？

**答案**：`hosts` 是裁决索引，一个 host 可能被 Ingress、VirtualServer、TransportServer 三种资源中的任意一种赢得，所以必须用统一接口 `Resource` 存放；`virtualServers` 是原始仓库，只存 VirtualServer 这一类对象，用具体类型即可，也方便直接访问其字段。

**练习 2**：`Configuration` 用一把 `sync.RWMutex`，读操作（如 `FindResourcesForService`）和写操作（如 `AddOrUpdateVirtualServer`）分别用什么锁？

**答案**：写操作用 `c.lock.Lock()`（见 `AddOrUpdateVirtualServer` 首行），读操作用 `c.lock.RLock()`（见 `IsServiceReferencedByVirtualServer` 等），允许多读单写。

---

### 4.2 增删改与变更追踪：rebuildHosts 的产物

#### 4.2.1 概念说明

`Configuration` 对外暴露的写接口很统一：`AddOrUpdateIngress` / `AddOrUpdateVirtualServer` / `AddOrUpdateVirtualServerRoute` / `AddOrUpdateTransportServer` / `AddOrUpdateGlobalConfiguration`，以及对应的 `Delete*`。它们全部返回同一个签名：

```go
([]ResourceChange, []ConfigurationProblem)
```

这是本节的主角——`Configuration` 给上层的**两个产物**：

- **`ResourceChange`**：一次「需要在 NGINX 配置上执行的变更」，要么 `Delete`（删掉某个 host 的配置），要么 `AddOrUpdate`（新增或更新某个 host 的配置）。上层 `processChanges` 据此调用 `Configurator` 写文件并 reload。
- **`ConfigurationProblem`**：一个「需要上报给用户的问题」，含 `Object`（出问题的资源）、`IsError`（是错误还是警告）、`Reason`、`Message`。上层 `processProblems` 据此发 Kubernetes Event 并回写 `.status.state`。

> **错误 vs 警告**：`IsError=true` → status 置为 `Invalid`（通常是 CRD 校验失败）；`IsError=false` → status 置为 `Warning`（通常是 host 冲突、孤儿资源）。见 `processProblems` 中的分支。

#### 4.2.2 核心流程

以 `AddOrUpdateVirtualServer` 为例，流程是「先改仓库，再重建索引，再算 diff」：

```text
AddOrUpdateVirtualServer(vs)
  ├─ lock.Lock()
  ├─ key = namespace/name
  ├─ if 不是正确的 IngressClass → 从 virtualServers 删除（资源不属于本控制器）
  ├─ else:
  │    ├─ ValidateVirtualServer(vs) → validationError
  │    ├─ 若校验失败 → 从 virtualServers 删除
  │    └─ 若校验通过 → balanceUpstreamProxies + virtualServers[key] = vs   ← 只改仓库
  ├─ if !startupComplete → 直接返回（不重建，启动期优化）
  ├─ changes, problems = rebuildHosts()                                      ← 重建索引 + diff
  ├─ 若 validationError != nil → 把错误挂到对应 change，或补一条 problem
  └─ return changes, problems
```

`rebuildHosts` 是整个文件的「心脏」，它做四件事：

```text
rebuildHosts()
  ├─ newHosts, newResources = buildHostsAndResources()   ← 从仓库重算「谁拥有哪个 host」
  ├─ updateActiveHostsForIngresses()                     ← 标记 Ingress 的 ValidHosts
  ├─ detectChangesInHosts(old, new)                      ← 三类 diff: removed/updated/added
  ├─ createResourceChangesForHosts() + squashResourceResults()  ← 组装 ResourceChange（delete 在前）
  ├─ 让 change 指向「最新版本」的资源（保证 warning 不丢）
  ├─ 计算新一批 ConfigurationProblem:
  │    ├─ addProblemsForResourcesWithoutActiveHost()     ← host 被抢走的资源
  │    ├─ addProblemsForOrphanMinions()                  ← 没有主人的 minion Ingress
  │    ├─ addProblemsForOrphanOrIgnoredVsrs()            ← 没被 VS 引用 / 被忽略的 VSR
  │    └─ addWarningsForVirtualServersWithMissConfiguredListeners()
  ├─ detectChangesInProblems(old, new)                   ← 只上报「新增或变化」的问题
  └─ 替换 c.hosts 与 c.hostProblems
```

两个设计要点：

1. **每次都全量重算**。`rebuildHosts` 不做增量，而是从仓库重建一份全新的 `newHosts`，再和老的 `c.hosts` 做 diff。这避免了维护增量状态的各种边界 bug，代价是 O(N)——这也是为什么启动期要用 `startupComplete` 跳过它。
2. **delete changes 永远排在 addOrUpdate 前面**。否则一个「释放 host」的删除和一个「抢占同 host」的新增若乱序，中间状态会让 NGINX 配置出现重复 `server_name`，导致 reload 失败。

#### 4.2.3 源码精读

两个产物类型：[internal/k8s/configuration.go:63-83](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/configuration.go#L63-L83) —— `ResourceChange` 有 `Op / Resource / Error`，`ConfigurationProblem` 有 `Object / IsError / Reason / Message`。

`AddOrUpdateVirtualServer` 主体：[internal/k8s/configuration.go:572-634](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/configuration.go#L572-L634) —— 注意三段式：IngressClass 判断 → 校验与入库 → `rebuildHosts` + 错误兜底。

`rebuildHosts` 全貌：[internal/k8s/configuration.go:1209-1244](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/configuration.go#L1209-L1244)。

「delete 在前」的实现：[internal/k8s/configuration.go:1491-1532](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/configuration.go#L1491-L1532) —— 函数末尾 `return append(deleteChanges, changes...)`，并有长注释解释原因。

`completeStartup` 与启动期优化：[internal/k8s/configuration.go:898-904](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/configuration.go#L898-L904) —— 启动期所有 `AddOrUpdate*` 都提前 return（见各方法里的 `if !c.startupComplete` 分支，如 [L506-517](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/configuration.go#L506-L517)），等队列排空后由 `CompleteStartup()` 做唯一一次 `rebuildHosts`。

#### 4.2.4 代码实践

1. **实践目标**：看清「校验失败」如何同时反映在 `changes` 与 `problems` 两条通道。
2. **操作步骤**：阅读 [internal/k8s/configuration.go:604-633](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/configuration.go#L604-L633) 中 `if validationError != nil` 这段兜底逻辑。
3. **需要观察的现象**：它先在 `changes` 里找有没有针对该 VS 的 change（说明该 VS 之前有活跃 host，`rebuildHosts` 生成了一个「删除它」的 change），把错误挂上去；若没找到，就单独构造一条 `ConfigurationProblem`。
4. **预期结果**：能解释为什么校验错误要「双保险」地同时走 change 与 problem 两条路——因为失效资源可能仍占用 host（走 change 删除路径），也可能本就没有活跃 host（走 problem 单独上报）。
5. 待本地验证：无。

#### 4.2.5 小练习与答案

**练习 1**：`rebuildHosts` 每次都全量重建 `newHosts`，为什么不担心性能？

**答案**：单 worker 串行 sync（承接 u3-l3）保证 `rebuildHosts` 不会并发执行；同时启动期通过 `startupComplete=false` 跳过逐次重建，只在 `CompleteStartup` 时做一次。稳态下每次 sync 只重建一次，规模可控。

**练习 2**：`detectChangesInProblems` 为什么只返回「新增或变化」的问题，而不是全部问题？

**答案**：为了避免对同一个不变的问题反复发 Event、反复回写 status。只有当问题的 `IsError/Reason/Message` 相对上次发生变化时才上报，减少对 API Server 的无谓请求。

---

### 4.3 主机索引与冲突检测：谁赢得 host

#### 4.3.1 概念说明

这是本讲最核心的机制：**多个资源声明同一个 host 时，如何裁决谁赢**。NGINX 配置里一个 `(host, port)` 只能有一个 `server` 块，所以控制器必须在写配置前定出唯一赢家，其余资源被标记为冲突并拒绝生成配置。

裁决规则写在 `chooseObjectMetaWinner` 里，极其简洁：

- **先比创建时间** `CreationTimestamp`：更早创建的资源赢（先到先得）。
- **时间相同则比 UID**：UID 字符串更大的赢（确定性 tiebreak，避免随机）。

这套规则保证：无论控制器重启多少次、资源以什么顺序被 Informer 推送，裁决结果都稳定且可复现。

冲突的「后果」分三处体现，对应上一节的三个产物通道：

1. **输家资源上挂一条 warning**（`AddWarning`），随资源一起进入 `changes`，最终在 Event 里可见。
2. **输家资源得到一条 `ConfigurationProblem`**（`IsError=false` → Warning），上层把它的 `.status.state` 置为 `Warning`，并清除其 status 里的 LB 地址。
3. **输家永远不会出现在 `hosts` 索引里**，所以 `Configurator` 根本不会为它生成 nginx.conf——这就是「坏配置不会落地」的根本保障。

#### 4.3.2 核心流程

裁决发生在 `buildHostsAndResources` 中，三类资源依次处理：

```text
buildHostsAndResources()
  ├─ Step 1: 遍历排序后的 ingresses（跳过 minion）
  │    for each rule.Host:
  │       if newHosts[host] 为空 → 直接占用
  │       else → 竞争：若 holder.Wins(新资源) 则新资源挂 warning；否则新资源抢占，旧 holder 挂 warning
  ├─ Step 2: 遍历排序后的 virtualServers
  │    同样的 holder.Wins 竞争逻辑（host = vs.Spec.Host）
  └─ Step 3: 若开启 TLS passthrough，遍历 transportServers
       同样的竞争逻辑
```

关键的一行竞争逻辑（以 VS 为例）：

```go
holder, exists := newHosts[vs.Spec.Host]
if !exists {
    newHosts[vs.Spec.Host] = resource
    continue
}
warning := fmt.Sprintf("host %s is taken by another resource", vs.Spec.Host)
if !holder.Wins(resource) {   // holder 输了 → 新资源抢占
    newHosts[vs.Spec.Host] = resource
    holder.AddWarning(warning)
} else {                      // holder 赢了 → 新资源被拒
    resource.AddWarning(warning)
}
```

冲突问题由 `addProblemsForResourcesWithoutActiveHost` 统一收口：它检查每个资源，如果该资源声明的 host 在 `c.hosts` 里指向的不是自己（即自己没抢到），就生成一条 `"Host is taken by another resource"` 的 problem。

> 注意 host 冲突的 problem 是 **Warning 不是 Error**：资源本身语法合法，只是被别的资源抢了，一旦赢家被删除，输家会自动「复活」。

#### 4.3.3 源码精读

裁决规则本体：[internal/k8s/configuration.go:54-60](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/configuration.go#L54-L60) —— `chooseObjectMetaWinner`，先 `CreationTimestamp.Equal` 判等，再 `Before` 比早晚，相等则 `UID > UID`。

`Wins` 方法只是转调它，例如 VirtualServer 版：[internal/k8s/configuration.go:248-250](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/configuration.go#L248-L250)。

`buildHostsAndResources` 全流程：[internal/k8s/configuration.go:1625-1740](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/configuration.go#L1625-L1740)，VS 段的竞争逻辑在 [L1693-L1707](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/configuration.go#L1693-L1707)。

冲突 problem 的生成：[internal/k8s/configuration.go:1316-1362](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/configuration.go#L1316-L1362) —— `addProblemsForResourcesWithoutActiveHost`，注意 `VirtualServerConfiguration` 分支比较 `c.hosts[host].GetKeyWithKind() != r.GetKeyWithKind()`。

Ingress 的多 host 细节：[internal/k8s/configuration.go:1246-1258](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/configuration.go#L1246-L1258) —— `updateActiveHostsForIngresses` 用 `ValidHosts map[string]bool` 记录「一个 Ingress 的每个 host 是否赢」，因此一个 Ingress 可能部分 host 有效、部分被抢，这与 VS（单 host）不同。

#### 4.3.4 代码实践

这是本讲的主实践，详见第 5 节「综合实践」。这里先做一个热身：

1. **实践目标**：用现成的测试验证裁决规则的「先到先得」语义。
2. **操作步骤**：打开 [internal/k8s/configuration_test.go:1888](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/configuration_test.go#L1888) 的 `TestHostCollisions`，阅读它依次加入 TransportServer、VirtualServer、Ingress（都抢占 `foo.example.com`）的过程。
3. **需要观察的现象**：每次 `AddOrUpdateX` 返回的 `expectedChanges` 里，输家的 change 带有 `Warnings: []string{"host foo.example.com is taken by another resource"}`，并且 `expectedProblems` 里多出一条 `Message: "Host is taken by another resource"`。
4. **预期结果**：能复述「赢家进 `AddOrUpdate` change，输家进 problem，且 delete change 排在 addOrUpdate 之前」。
5. 运行命令（待本地验证）：`make test`（或 `go test -tags=aws,helmunit ./internal/k8s/ -run TestHostCollisions`）。

#### 4.3.5 小练习与答案

**练习 1**：两个 VirtualServer `vs-a` 和 `vs-b` 声明同一个 host，`CreationTimestamp` 完全相同、UID 都为空。按 `chooseObjectMetaWinner`，谁赢？

**答案**：先比时间相等，再比 `UID > UID` 即 `"" > ""`，结果为 `false`，返回 `false`，即 holder（先占据 host 的那个）「不赢」。注意这里的语义：`chooseObjectMetaWinner(holder, challenger)` 返回 `false` 表示 holder 输，所以 **challenger（后处理但抢占的资源）赢**。按 key 排序 `vs-a` 先入 `newHosts` 成为 holder，`vs-b` 作为 challenger 抢占成功，因此 `vs-b` 赢、`vs-a` 得到 warning 与 problem。（待本地验证时间戳是否同秒。）

**练习 2**：为什么 Ingress 用 `ValidHosts map[string]bool` 而 VirtualServer 不需要？

**答案**：一个 Ingress 的 `Rules` 可以含多个 host，可能部分 host 赢、部分 host 输，需要逐 host 标记有效性；VirtualServer 的 `Spec.Host` 是单值，要么整个赢、要么整个输，用「是否出现在 `c.hosts` 且指向自己」即可判断，不需要额外字段。

---

### 4.4 引用检查：Service/Secret/Policy 变了影响谁

#### 4.4.1 概念说明

`Configuration` 还要回答一个反向问题：**当一个 Service / Secret / Policy / Endpoint 发生变化时，哪些路由资源需要重新生成配置？** 这就是 `reference_checkers.go` 的职责。

为什么需要它？Kubernetes 的 Informer 是按资源类型分别监听的：Service 变化只会触发 Service 的 handler。但 Service 本身不产生 NGINX 配置，真正产生配置的是引用该 Service 的 VirtualServer / Ingress。所以控制器拿到一个 Service 变更后，必须「反查」出所有引用它的路由资源，再把它们重新入队。这个反查就交给 reference checker。

设计上用一个统一接口 `resourceReferenceChecker`，定义了五个方法，对应五种「可能引用者」：

```go
type resourceReferenceChecker interface {
    IsReferencedByIngress(namespace, name string, ing *networking.Ingress) bool
    IsReferencedByMinion(namespace, name string, ing *networking.Ingress) bool
    IsReferencedByVirtualServer(namespace, name string, vs *conf_v1.VirtualServer) bool
    IsReferencedByVirtualServerRoute(namespace, name string, vsr *conf_v1.VirtualServerRoute) bool
    IsReferencedByTransportServer(namespace, name string, ts *conf_v1.TransportServer) bool
}
```

每种「被引用物」（Secret、Service、Endpoint、Policy、AppProtect 资源、DoS 资源）各有一个实现。`Configuration.findResourcesForResourceReference` 是统一驱动：遍历当前所有生效的 host 资源，对每个资源调用 checker 的对应方法，命中就收集。

#### 4.4.2 核心流程

以「Service 变化」为例的反查链路：

```text
syncService(task)                                ← 在 controller.go
  └─ configuration.FindResourcesForService(ns, name)
       └─ findResourcesForResourceReference(ns, name, serviceReferenceChecker)
            ├─ 遍历 c.hosts 里每个生效资源
            │    ├─ IngressConfiguration → checker.IsReferencedByIngress / Minion
            │    ├─ VirtualServerConfiguration → checker.IsReferencedByVirtualServer / VirtualServerRoute
            │    └─ TransportServerConfiguration → checker.IsReferencedByTransportServer
            ├─ 遍历 c.listenerHosts 里的 TransportServer（L4 资源）
            └─ 返回命中的 Resource 列表
  └─ 把命中的资源重新入队 → 触发各自的 sync → AddOrUpdate* → 重新生成配置
```

每种 checker 的「引用判定」逻辑不同，例如：

- **secretReferenceChecker**：Ingress 看 `spec.tls[].secretName` 与 BasicAuth/JWT 注解；VS 看 `spec.tls.secret`；TS 看 `spec.tls.secret`。
- **serviceReferenceChecker**：VS 遍历 `spec.upstreams[].service`（含 `backup`），若 `UseClusterIP=true` 且检查器是 endpoint 检查器（`hasClusterIP=true`）则跳过——因为这种情况下 Service 名匹配不能用于 Endpoint 变更反查。
- **policyReferenceChecker**：VS 看 `spec.policies` 与每条 route 的 `policies`；Ingress 看 `nginx.org/policies` / `nginx.com/policies` 注解。

一个特别的机制：`serviceReferenceChecker` 内含一个 `policyServices` 映射，用于追踪「ExternalAuth Policy 背后引用的 auth Service」。因为 Policy 间接引用 Service（通过 ExternalAuth 配置），当一个被 Policy 间接引用的 Service 变化时，也需要反查到引用该 Policy 的 VS。`UpdatePolicyServiceRef` / `DeletePolicyServiceRef` 负责维护这个映射。

#### 4.4.3 源码精读

统一接口：[internal/k8s/reference_checkers.go:13-19](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/reference_checkers.go#L13-L19)。

Secret 检查器的 VS 判定：[internal/k8s/reference_checkers.go:79-89](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/reference_checkers.go#L79-L89) —— 命名空间相等且 `vs.Spec.TLS.Secret == secretName`。

Service 检查器的 VS 判定：[internal/k8s/reference_checkers.go:152-174](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/reference_checkers.go#L152-L174) —— 遍历 upstreams，`UseClusterIP` 跳过逻辑，以及通过 `isPolicyServiceReferenced` 处理 Policy 间接引用。

Policy 间接引用追踪：[internal/k8s/reference_checkers.go:199-219](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/reference_checkers.go#L199-L219) 与 Configuration 侧的维护方法 [internal/k8s/configuration.go:1117-1128](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/configuration.go#L1117-L1128)。

统一驱动函数：[internal/k8s/configuration.go:1150-1202](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/configuration.go#L1150-L1202) —— `findResourcesForResourceReference`，先遍历 `c.hosts`，再遍历 `c.listenerHosts`（让 L4 TransportServer 也能被反查到）。

`Configuration` 对外的反查入口：[internal/k8s/configuration.go:1066-1112](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/configuration.go#L1066-L1112) —— `FindResourcesForService` / `FindResourcesForEndpoints` / `FindResourcesForSecret` / `FindResourcesForPolicy` 等，全是 `findResourcesForResourceReference` 的薄封装，区别仅在传入哪个 checker。

#### 4.4.4 代码实践

1. **实践目标**：跟踪一个 Secret 变化如何「反向」触发引用它的 VS 重新生成配置。
2. **操作步骤**：
   - 在 [internal/k8s/controller.go:1394](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L1394) 附近找到 `FindResourcesForSecret` 的调用点（Secret 同步逻辑）。
   - 跟进到 `findResourcesForResourceReference`，再跟进到 `secretReferenceChecker.IsReferencedByVirtualServer`。
3. **需要观察的现象**：一个 Secret 变化 → 反查得到一组 `Resource`（含引用它的 VS）→ 这些 VS 被重新入队 → 各自的 `syncVirtualServer` → `AddOrUpdateVirtualServer` → `processChanges` 重生成配置。
4. **预期结果**：能画出「Secret 变化 → 反查 → 命中 VS → 重生成」的完整调用链，并解释为什么不能只靠 Secret 自己的 handler 去改配置（因为 Secret 不产生配置，配置归属在 VS）。
5. 待本地验证：无，纯阅读型实践。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `serviceReferenceChecker` 要区分 `hasClusterIP` 两种构造（见 `NewConfiguration` 里 `newServiceReferenceChecker(false, ...)` 与 `newServiceReferenceChecker(true, ...)`）？

**答案**：一个给 `FindResourcesForService`（`hasClusterIP=false`，Service 变化时按 Service 名反查），一个给 `FindResourcesForEndpoints`（`hasClusterIP=true`，Endpoint 变化时反查）。当 upstream 设置了 `UseClusterIP=true`，NGINX 直接连 ClusterIP、不再依赖 Endpoint 切片，所以 Endpoint 变化时这类 upstream 应被跳过（见 [reference_checkers.go:155-157](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/reference_checkers.go#L155-L157)），因此需要 `hasClusterIP` 标志区分两种反查语义。

**练习 2**：`findResourcesForResourceReference` 既遍历 `c.hosts` 又遍历 `c.listenerHosts`，为什么？

**答案**：`c.hosts` 里放的是 HTTP / TLS-passthrough 资源（Ingress、VS、passthrough TS），而 TCP/UDP 的 TransportServer 配置放在 `c.listenerHosts`（按 `listenerHostKey` 索引）。要反查 Service/Secret 引用时，两类资源都要覆盖，否则 L4 TransportServer 引用的 Service 变化会被漏掉。

---

## 5. 综合实践

**任务**：构造两个 VirtualServer 抢占同一个 host 的场景，亲手验证 `Configuration` 如何识别并报告冲突，并把三个产物（change / warning / problem）都对上号。

### 步骤 1：理解现成的参照测试

先阅读 [internal/k8s/configuration_test.go:1888-1991](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/configuration_test.go#L1888-L1991) 的 `TestHostCollisions`。它用现成 helper 构造资源：

- `createTestConfiguration()`：返回一个已完成启动（`startupComplete=true`）的 `Configuration`，见 [configuration_test.go:20-24](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/configuration_test.go#L20-L24)。
- `createTestVirtualServer(name, host)`：返回带 `IngressClass: "nginx"`、`CreationTimestamp: metav1.Now()` 的 VS，见 [configuration_test.go:4438-4450](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/configuration_test.go#L4438-L4450)。

### 步骤 2：写一个最小冲突测试（示例代码）

在 `internal/k8s/configuration_test.go` 末尾仿照现有测试新增一个函数（**这是示例代码，非项目原有代码**）：

```go
// 示例代码：验证两个 VirtualServer 抢占同一 host 的冲突上报
func TestTwoVirtualServersSameHost(t *testing.T) {
    configuration := createTestConfiguration()

    vsA := createTestVirtualServer("vs-a", "foo.example.com")
    vsB := createTestVirtualServer("vs-b", "foo.example.com")

    // 第一个 VS 正常占用 host
    changes, problems := configuration.AddOrUpdateVirtualServer(vsA)
    // 预期：一条 AddOrUpdate 的 change，无 problem
    if len(changes) != 1 || changes[0].Op != AddOrUpdate {
        t.Errorf("vs-a 应产生 1 条 AddOrUpdate change，got: %+v", changes)
    }
    if len(problems) != 0 {
        t.Errorf("vs-a 不应产生 problem，got: %+v", problems)
    }

    // 第二个 VS 抢同一 host —— 触发冲突
    changes, problems = configuration.AddOrUpdateVirtualServer(vsB)
    // 预期：发生 host 占有权转移（见步骤 3 的分析）
    //  - changes: [Delete vs-a(带 warning), AddOrUpdate vs-b]
    //  - problems: [vs-a "Host is taken by another resource"]
    t.Logf("changes = %+v", changes)
    t.Logf("problems = %+v", problems)
}
```

### 步骤 3：预测并验证结果

根据 4.3 节的裁决规则推演（两个 VS 的 `CreationTimestamp` 用 `metav1.Now()` 多半落在同一秒、UID 都为空）：

1. `vs-a` 先按 key 排序进入 `newHosts`，成为 `foo.example.com` 的 holder。
2. `vs-b` 处理时触发竞争：`chooseObjectMetaWinner(vsA, vsB)` 因时间相等、UID 相等返回 `false`（holder 输），所以 `vs-b` 抢占成功，`vs-a` 被挂上 warning。
3. 于是 `detectChangesInHosts` 发现 `foo.example.com` 的持有者从 `vs-a` 变成 `vs-b` → 生成 `Delete vs-a` + `AddOrUpdate vs-b`（delete 在前）。
4. `addProblemsForResourcesWithoutActiveHost` 发现 `vs-a` 不再拥有 host → 生成一条 `Message: "Host is taken by another resource"`、`IsError: false` 的 problem。

**运行命令**（待本地验证）：

```bash
make test
# 或精确到单个测试：
go test -tags=aws,helmunit ./internal/k8s/ -run TestTwoVirtualServersSameHost -v
```

### 步骤 4：需要观察的现象与预期

- `changes` 里第一条是 `Op: Delete`、`Resource` 是带 `Warnings` 的 `vs-a`；第二条是 `Op: AddOrUpdate`、`Resource` 是 `vs-b`。这印证「delete 在前，避免中间态出现重复 server_name」。
- `problems` 里有且有一条针对 `vs-a` 的 problem，`IsError=false`（Warning，不是 Invalid）。
- 把 `vs-b` 删掉（`configuration.DeleteVirtualServer("default/vs-b")`）后，`vs-a` 应当「复活」——再次出现在 `AddOrUpdate` change 里，且 problem 消失。这印证冲突是动态的、输家会自动接班。

> **边界提示**：如果两次 `metav1.Now()` 跨了秒边界，`vs-a` 时间更早则会赢，此时 `changes` 可能为空、problem 落在 `vs-b` 上。机制不变，只是赢家对调。若想完全确定性，可在构造 VS 后手动设置 `CreationTimestamp` 与 `UID`。这一处「待本地验证」。

## 6. 本讲小结

- `Configuration` 是控制器的「世界模型」，用两类 map 分别存「原始资源仓库」（`virtualServers` 等）与「裁决索引」（`hosts`、`listenerHosts`），外加「问题簿」（`hostProblems` 等），全由一把 `sync.RWMutex` 保护。
- 所有 `AddOrUpdate*` / `Delete*` 走统一三段式：改仓库 → `rebuildHosts` 全量重算索引 → 做 diff 产出 `[]ResourceChange` 与 `[]ConfigurationProblem`。
- host 冲突由 `chooseObjectMetaWinner` 裁决（先比 `CreationTimestamp`，再比 `UID`），赢家进 `hosts`、输家挂 warning 并得到一条 Warning 级 problem，输家永远不会生成 nginx.conf——这是「坏配置不落地」的根本保障。
- `delete` change 永远排在 `addOrUpdate` 之前，并经 `squashResourceChanges` 合并，避免 reload 中间态出现重复 `server_name`。
- 启动期靠 `startupComplete` 跳过逐次 `rebuildHosts`，待 `CompleteStartup()` 做唯一一次重建，避免 O(N) 重复开销。
- `reference_checkers.go` 用统一接口回答「Service/Secret/Policy 变了影响谁」，由 `findResourcesForResourceReference` 驱动，遍历 `hosts` 与 `listenerHosts` 两层索引完成反查。

## 7. 下一步学习建议

- 下一讲 **u3-l7 Leader 选举与 Status 回写**：本讲反复提到的「上层把 problem 回写 `.status.state`」「发 Kubernetes Event」正是 `processProblems` + `statusUpdater` 的职责，那里会讲清 status 回写的统一模式与启动期延后刷新优化。
- 进入第 4 单元 **u4-l1 Configurator**：本讲的 `ResourceChange` 是 `Configurator` 的输入，看 `processChanges` 如何把 `VirtualServerConfiguration` 转成 `VirtualServerEx` 并真正渲染 nginx.conf。
- 若想加深对裁决稳定性的理解，可继续阅读 `internal/k8s/configuration.go` 中 minion 路径冲突（`buildMinionConfigs` 的 `ValidPaths`）与 VSR 孤儿判定（`addProblemsForOrphanOrIgnoredVsrs`），它们是 host 冲突机制在更细粒度上的复用。
