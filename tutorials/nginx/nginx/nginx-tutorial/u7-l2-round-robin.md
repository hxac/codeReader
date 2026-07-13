# round_robin 负载均衡

## 1. 本讲目标

本讲是 upstream（反向代理/负载均衡）子系统的第二讲，承接 u7-l1 的 upstream 框架。学完本讲你应当能够：

- 说清 nginx 默认负载均衡算法「平滑加权轮询」（smooth weighted round robin）的运作原理，能手算一个 3 后端的分配序列。
- 读懂 `ngx_http_upstream_round_robin.c` 中 `init` / `get` / `free` 三类回调的实现，并说清它们与 upstream 框架的对接点。
- 理解 `weight`、`effective_weight`、`current_weight` 三个权重的区别，以及故障后端如何被动态降权、冷却、再缓慢恢复。
- 掌握 `max_fails` / `fail_timeout` / `max_conns` / `backup` 等指令在源码层面是如何被落实的。

## 2. 前置知识

本讲默认你已经掌握：

- **upstream 框架的两段式回调**（u7-l1）：nginx 把「选哪台后端」这件事抽象成一张函数指针表。配置期每个 upstream 块注册一个「init_upstream」（负责建 peer 表），每个请求再注册一组「get / free」（负责选/还一台 peer）。本讲讲的就是默认实现 round_robin 如何填这三张表。
- **内存池与配置结构**（u2-l1、u3-x）：peer 表挂在配置阶段的 `cf->pool` 上，per-request 状态挂在请求池 `r->pool` 上。
- **红黑树 / 共享内存**（u2-l3、u4-l3）：当 upstream 配了 `zone`（共享内存）时，peer 表被多 worker 共享，读写要用 `rwlock`。本讲主体逻辑不依赖 zone，会标注哪些代码只在 `NGX_HTTP_UPSTREAM_ZONE` 下生效。

几个关键词先建立直觉：

- **peer（对端）**：一台后端服务器（一个 `IP:port`）。
- **轮询（round robin）**：依次把请求分给各后端。
- **加权（weighted）**：给每台后端一个 `weight`，请求量按权重比例分配（权重 5 的机器拿到的请求是权重 1 的 5 倍）。
- **平滑（smooth）**：不是「先连发 5 次给 A 再发 1 次给 B」，而是把 A 的 5 次穿插开，避免瞬时压力集中——这就是 nginx 用的算法。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [src/http/ngx_http_upstream_round_robin.h](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_upstream_round_robin.h) | peer 与 peers 结构体定义、per-request 数据结构、锁宏与函数原型 |
| [src/http/ngx_http_upstream_round_robin.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_upstream_round_robin.c) | `init_round_robin`（建表）、`init_round_robin_peer`（每请求初始化）、`get_round_robin_peer`（选 peer）、`free_round_robin_peer`（还 peer）全部实现 |
| [src/http/ngx_http_upstream.h](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_upstream.h) | upstream 框架定义的回调类型 `ngx_http_upstream_peer_t`，是 round_robin 与框架对接的「接口契约」 |
| [src/http/ngx_http_upstream.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_upstream.c) | 框架侧：默认选 round_robin、每请求调 `peer.init`、失败时调 `peer.free` 的调用点 |
| [src/event/ngx_event_connect.h](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_connect.h) | `ngx_peer_connection_t` 结构与 `NGX_PEER_FAILED` 等状态位定义 |

## 4. 核心概念与源码讲解

### 4.1 与 upstream 框架的对接：三类回调

#### 4.1.1 概念说明

round_robin 不是一个独立模块，而是 upstream 框架的「默认策略」。框架规定了一个 upstream 块必须提供三组回调，round_robin 把它们全部实现：

1. **配置期 init_upstream**：解析完 `upstream {}` 块后调用一次，把配置编译成内存里的 peer 表。round_robin 实现为 `ngx_http_upstream_init_round_robin`。
2. **每请求 init**：每个反向代理请求开始时调用，准备 per-request 的选择上下文（当前指针、已试位图、剩余尝试次数），并把 get/free 函数指针装进 `r->upstream->peer`。round_robin 实现为 `ngx_http_upstream_init_round_robin_peer`。
3. **每请求 get / free**：选一台后端 / 把这次尝试的结果（成功或失败）反馈回去。round_robin 实现为 `ngx_http_upstream_get_round_robin_peer` 与 `ngx_http_upstream_free_round_robin_peer`。

这套约定由框架侧的 `ngx_http_upstream_peer_t` 定义：

[src/http/ngx_http_upstream.h:91-95](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_upstream.h#L91-L95) — 这是负载均衡器与 upstream 框架之间的「接口契约」：`init_upstream`（建表）、`init`（每请求初始化）、`data`（建表阶段的产物，如表指针）。

#### 4.1.2 核心流程

配置阶段，nginx 在装配 http main 配置时，遍历每个 upstream 块，挑出它的负载均衡器；没显式指定（即没写 `hash` / `ip_hash` / `least_conn` 等指令）就用 round_robin：

[src/http/ngx_http_upstream.c:7303-7304](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_upstream.c#L7303-L7304) — 三元运算符就是「默认 round robin」的来源：任何 LB 模块若想接管，只需在配置期把自己的函数塞进 `peer.init_upstream`，框架就不再用 round_robin。**所以 round_robin 是「没有指令、默默生效」的默认值**，这正是它没有对应 `xxx_module` 指令的原因。

运行阶段，每个代理请求在 `ngx_http_upstream_init_request` 末尾调用每请求 init：

[src/http/ngx_http_upstream.c:840-852](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_upstream.c#L840-L852) — `uscf->peer.init(r, uscf)` 进入 round_robin 的每请求初始化；随后 `peer.tries` 被 `next_upstream_tries` 指令进一步收紧（限制最大重试次数）。

真正去连后端时，框架的 `ngx_event_connect` 调 `pc->get(pc, pc->data)` 选 peer，失败则由 `ngx_http_upstream_next` 调 `pc->free(...)` 反馈状态：

- [src/event/ngx_event_connect.c:34-37](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_connect.c#L34-L37) — `get` 不返回 `NGX_OK` 就直接返回，连接流程中止。
- [src/http/ngx_http_upstream.c:4606-4615](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_upstream.c#L4606-L4615) — 失败反馈时，框架根据失败类型翻译出 `state`：后端返回 **403/404** 记为 `NGX_PEER_NEXT`（「这台不合适，换下一台，但不计故障」），其它错误记为 `NGX_PEER_FAILED`（真故障，要降权）。

`state` 的取值在 [src/event/ngx_event_connect.h:17-19](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_connect.h#L17-L19) 定义：`NGX_PEER_KEEPALIVE=1`、`NGX_PEER_NEXT=2`、`NGX_PEER_FAILED=4`。

`pc`（`ngx_peer_connection_t`）就是回调之间传递的「连接句柄」，[src/event/ngx_event_connect.h:36-49](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_connect.h#L36-L49) 含 `sockaddr/socklen/name`（get 写入，告诉框架连哪）、`tries`（剩余尝试次数）、`get/free/data`（函数指针与私有数据）。

> **关键结论**：round_robin 只是一个「填三张表」的策略模块。读它的源码就是读这四个函数：`init_round_robin` 建表、`init_round_robin_peer` 装回调、`get_round_robin_peer` 选、`free_round_robin_peer` 还。

### 4.2 ngx_http_upstream_init_round_robin —— 配置期把 upstream 块编译成 peer 链表

#### 4.2.1 概念说明

这个函数在 `nginx -t` 或启动/reload 时对每个 upstream 块各调用一次。它的输入是配置解析阶段产出的 `us->servers`（一个 `ngx_http_upstream_server_t` 数组，对应 `server a:80 weight=5;` 这样的每条指令），输出是两张以 `ngx_http_upstream_rr_peer_t` 为节点的单向链表：**主表（primary）**与**备表（backup）**。

先把两个核心结构记住：

[src/http/ngx_http_upstream_round_robin.h:47-105](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_upstream_round_robin.h#L47-L105) — 单台后端 `ngx_http_upstream_rr_peer_t`，关键字段（去掉 zone/sid/ssl 等条件编译）：

```c
struct ngx_http_upstream_rr_peer_s {
    struct sockaddr  *sockaddr;   // 后端地址
    socklen_t         socklen;
    ngx_str_t         name;       // "1.2.3.4:80"
    ngx_str_t         server;     // 配置里写的原始 server 串

    ngx_int_t   current_weight;      // 运行期动态权重，选择算法的核心变量
    ngx_int_t   effective_weight;    // 受故障影响的「有效权重」，缓慢恢复
    ngx_int_t   weight;              // 配置的静态权重

    ngx_uint_t  conns;        // 当前活跃连接数
    ngx_uint_t  max_conns;    // 上限

    ngx_uint_t  fails;        // 连续失败次数
    time_t      accessed;     // 最近一次失败时间
    time_t      checked;      // 选择算法用的「上次检出」时间，控冷却窗口

    ngx_uint_t  max_fails;
    time_t      fail_timeout;

    ngx_uint_t  down;         // 配置的 down 标记
    ...
    ngx_http_upstream_rr_peer_t  *next;   // 串成链表
};
```

三个权重是本讲的灵魂，先给直觉（精确机制见 4.3、4.4）：

- `weight`：你写的 `weight=5`，恒定不变。
- `effective_weight`：初始等于 `weight`；故障时被减，正常时缓慢爬回 `weight`。
- `current_weight`：选择算法的「当前累计值」，每次选择都在动，决定谁被选中。

[src/http/ngx_http_upstream_round_robin.h:108-130](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_upstream_round_robin.h#L108-L130) — 一组 peer（主表或备表）`ngx_http_upstream_rr_peers_t`：`number`（节点数）、`total_weight`（权重和）、`tries`（可用节点数）、`single`（是否单节点，快路径开关）、`weighted`（是否非均权）、`peer`（链表头）、`next`（指向备表）。

#### 4.2.2 核心流程

`init_round_robin` 是一条「数两遍、填两遍」的装配线：

1. **登记每请求 init 回调**：第一行就把 `us->peer.init` 设为 round_robin 的每请求初始化函数。
2. **数主表**：第一遍循环统计主服务器（非 backup）的节点数 `n`、权重和 `w`、可用节点数 `t`。
3. **建主表**：分配 `peers` 容器与 `n` 个 peer 连续数组，第二遍循环把每个 `server` 的每个地址填成一个 peer。
4. **数备表、建备表**：同样两遍，建出 `backup`，挂在 `peers->next` 上。

[src/http/ngx_http_upstream_round_robin.c:51](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_upstream_round_robin.c#L51) — 登记 `us->peer.init = ngx_http_upstream_init_round_robin_peer;`，于是之后每个请求都会进 round_robin 的每请求初始化。

统计循环 [src/http/ngx_http_upstream_round_robin.c:65-90](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_upstream_round_robin.c#L65-L90)：跳过 backup、累加 `naddrs`（一个域名可能解析出多个 IP，每个 IP 是独立 peer）、累加权重 `naddrs * weight`。

填表循环里每个 peer 的初始化 [src/http/ngx_http_upstream_round_robin.c:215-226](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_upstream_round_robin.c#L215-L226)：

```c
peer[n].weight = server[i].weight;
peer[n].effective_weight = server[i].weight;   // 初始等于 weight
peer[n].current_weight = 0;                     // 从 0 起步
peer[n].max_conns = server[i].max_conns;
peer[n].max_fails = server[i].max_fails;
peer[n].fail_timeout = server[i].fail_timeout;
peer[n].down = server[i].down;
```

容器汇总 [src/http/ngx_http_upstream_round_robin.c:156-161](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_upstream_round_robin.c#L156-L161)：`single = (n==1)`、`weighted = (w != n)`（权重全为 1 时 `weighted=0`，可走简单路径）、`total_weight = w`、`tries = t`。

备表挂接 [src/http/ngx_http_upstream_round_robin.c:384](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_upstream_round_robin.c#L384) — `peers->next = backup;`。备表只在主表全军覆没时才启用（见 4.3.2）。

函数末尾还有一段「隐式 upstream」分支 [src/http/ngx_http_upstream_round_robin.c:390-453](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_upstream_round_robin.c#L390-L453)：当你直接写 `proxy_pass http://some-host:80;` 而没有 `upstream {}` 块时，`us->servers` 为空，走这条分支——它把主机名当场解析成地址，建一张权重全为 1、`max_fails=1`、`fail_timeout=10` 的默认表，且没有备表。

#### 4.2.3 代码实践

**目标**：验证「一个 server 多地址」会被拆成多个独立 peer。

**操作**：

1. 写一个最小配置 `upstream demo { server 127.0.0.1:8001; server 127.0.0.1:8002 weight=3; }`。
2. 在源码 [src/http/ngx_http_upstream_round_robin.c:215](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_upstream_round_robin.c#L215) 的填表循环里阅读：外层 `for (i...)` 遍历每条 `server` 指令，内层 `for (j=0; j<naddrs; j++)` 把每个地址各建一个 peer。
3. 想象 `server example.com weight=2;` 且 `example.com` 解析出 2 个 IP，那么 `n += naddrs` 让 `n` 增加 2，权重和 `w += 2 * 2 = 4`，即「这台逻辑服务器贡献 2 个 peer、各权重 2」。

**预期**：你会理解为何 `w += server[i].naddrs * server[i].weight`——权重是「每地址」的，多地址天然得到更多流量。运行结果待本地验证。

#### 4.2.4 小练习与答案

**练习 1**：配置了 `server a:80;`（无 weight）时，该 peer 的三个权重各是多少？
**答**：`weight=1`、`effective_weight=1`、`current_weight=0`。未写 weight 时框架默认补 1。

**练习 2**：为什么 `peers->single` 要单独存一个标志位？
**答**：单节点时可跳过整个加权选择算法，直接返回那唯一一个 peer（见 4.3.2），省去遍历与位图操作——这是热路径优化。

### 4.3 ngx_http_upstream_get_round_robin_peer —— 平滑加权选择

#### 4.3.1 概念说明

这是本讲的算法核心。nginx 用的不是朴素的「A A A A A B C」（连续发完权重数再轮下一个），而是**平滑加权轮询**：每轮给每个 peer 的 `current_weight` 累加自己的 `effective_weight`，选 `current_weight` 最大的那个，再把它减去「本轮所有人加的权重之和」。效果是按权重比例分配，且分配被打散，不会瞬时压垮一台。

算法伪代码（对应 `ngx_http_upstream_get_peer` 的主循环）：

```
total = 0
best  = NULL
for peer in peers（跳过已试/宕机/冷却/超连接数）:
    peer.current_weight += peer.effective_weight
    total               += peer.effective_weight
    if peer.effective_weight < peer.weight:
        peer.effective_weight += 1          # 缓慢恢复
    if best == NULL or peer.current_weight > best.current_weight:
        best = peer
best.current_weight -= total
return best
```

设权重 \(w_i\)，第 \(k\) 轮 peer \(i\) 的累计量为：

\[
c_i^{(k)} = c_i^{(k-1)} + e_i - \mathbb{1}[i=\text{best}]\sum_j e_j
\]

其中 \(e_i\) 是 `effective_weight`。可以证明，经过 \(\sum_i w_i\) 次选择后，各 peer 被选中的次数恰为 \(w_i\)，且 \(c_i\) 回到初值，构成一个完整周期——所以它既是「按比例」又是「可预测周期」的。

#### 4.3.2 核心流程

`get_round_robin_peer` 是入口，处理加锁、单节点快路径、多节点选择、失败降级四件事：

[src/http/ngx_http_upstream_round_robin.c:712-713](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_upstream_round_robin.c#L712-L713) — 进函数先取写锁（配了 zone 才真正加锁，否则宏为空），保护共享 peer 表。

**单节点快路径** [src/http/ngx_http_upstream_round_robin.c:721-741](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_upstream_round_robin.c#L721-L741)：只有一台后端时跳过加权算法，直接取 `peers->peer`，只检查 `down` 和 `max_conns`，通过则定它。

**多节点选择** [src/http/ngx_http_upstream_round_robin.c:747](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_upstream_round_robin.c#L747) — 调内部函数 `ngx_http_upstream_get_peer` 执行上面的加权算法。返回 `NULL` 说明没一台可用，`goto failed`。

**选中后** [src/http/ngx_http_upstream_round_robin.c:758-770](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_upstream_round_robin.c#L758-L770)：把 peer 的地址填进 `pc->sockaddr/socklen/name`（框架据此去 connect），`peer->conns++`，解锁返回 `NGX_OK`。

**降级到备表** [src/http/ngx_http_upstream_round_robin.c:772-796](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_upstream_round_robin.c#L772-L796)：主表全失败时，把 `rrp->peers` 切到 `peers->next`（备表），清空「已试」位图，**递归调用自己**在备表里再选。备表也选不出就返回 `NGX_BUSY`。

真正的加权算法在 `ngx_http_upstream_get_peer`，跳过条件是关键：

[src/http/ngx_http_upstream_round_robin.c:858-895](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_upstream_round_robin.c#L858-L895)：

```c
for (peer = rrp->peers->peer, i = 0; peer; peer = peer->next, i++) {
    n = i / (8 * sizeof(uintptr_t));
    m = (uintptr_t) 1 << i % (8 * sizeof(uintptr_t));

    if (rrp->tried[n] & m) { continue; }      // 本请求已试过
    if (peer->down) { continue; }              // 配置 down

    if (peer->max_fails
        && peer->fails >= peer->max_fails
        && now - peer->checked <= peer->fail_timeout) {   // 冷却中
        continue;
    }
    if (peer->max_conns && peer->conns >= peer->max_conns) { continue; }  // 满连接

    peer->current_weight += peer->effective_weight;     // 累加
    total += peer->effective_weight;

    if (peer->effective_weight < peer->weight) {        // 缓慢恢复
        peer->effective_weight++;
    }

    if (best == NULL || peer->current_weight > best->current_weight) {
        best = peer; p = i;
    }
}
...
best->current_weight -= total;   // 选中者扣总权重
```

四个 `continue` 就是四种「不选它」的判定；之后的累加与「缓慢恢复」就是算法本体。

[src/http/ngx_http_upstream_round_robin.c:919-926](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_upstream_round_robin.c#L919-L926)：选中后把对应位在 `tried` 位图里置 1（重试时跳过它），并刷新 `checked`——若距上次检出已超过 `fail_timeout`，就把 `checked` 推到 `now`，重置冷却窗口的起点。

**「已试」位图 `tried`** 用来支持 `proxy_next_upstream` 重试：一次请求依次尝试多台后端，已失败的位被置 1，下一轮 `get` 自动跳过。位图大小见每请求初始化 [src/http/ngx_http_upstream_round_robin.c:551-562](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_upstream_round_robin.c#L551-L562)：节点数不超过一个机器字（64 位机上 64 个）时直接用结构体内嵌的 `data` 字段，零分配；超过才在请求池上分配数组。

#### 4.3.3 一次完整的手算（平滑性验证）

设三台后端 A(weight=5)、B(weight=1)、C(weight=1)，全部健康（`effective_weight == weight`），`total = 7`。下表列出每一轮「累加后的 current_weight (A,B,C)」、「选中者」、以及「扣 total 后的状态」：

| 轮次 | 累加后 (A,B,C) | 选中 | 扣 7 后 (A,B,C) |
|---|---|---|---|
| 1 | (5,1,1) | A | (-2,1,1) |
| 2 | (3,2,2) | A | (-4,2,2) |
| 3 | (1,3,3) | B | (1,-4,3) |
| 4 | (6,-3,4) | A | (-1,-3,4) |
| 5 | (4,-2,5) | C | (4,-2,-2) |
| 6 | (9,-1,-1) | A | (2,-1,-1) |
| 7 | (7,0,0) | A | (0,0,0) |

7 轮里 A 被选中 5 次、B 1 次、C 1 次，正好按 5:1:1；序列 `A A B A C A A` 被打散，没有「连续 5 个 A」；第 7 轮结束后状态回到 (0,0,0)，周期闭合。这就是「平滑」二字的含义。

> 注：算法里比较用的是严格大于 `>`（见 [L891](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_upstream_round_robin.c#L891)），所以当多个 peer 的 `current_weight` 并列最大时，选链表中靠前的那个——这就是为什么权重相同时表现为「从头到尾顺序轮询」。

#### 4.3.4 代码实践

**目标**：用源码日志验证加权分配比例。

**操作**：

1. 起三个后端（例如三个不同端口的 `python3 -m http.server`），写：
   ```nginx
   upstream demo {
       server 127.0.0.1:8001 weight=5;
       server 127.0.0.1:8002 weight=1;
       server 127.0.0.1:8003 weight=1;
   }
   ```
2. 用 `--with-debug` 编译 nginx（见 u1-l2、u10-l5），在 `location /` 里 `proxy_pass http://demo;`，`error_log` 开 `debug_http`。
3. 连续发起 70 个请求：`for i in $(seq 70); do curl -s http://127.0.0.1/ -o /dev/null; done`。
4. 在 [src/http/ngx_http_upstream_round_robin.c:753-755](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_upstream_round_robin.c#L753-L755) 的 `ngx_log_debug2(... "get rr peer, current: %p %i", peer, peer->current_weight)` 处，每个请求会打印选中 peer 的地址与扣减后的 `current_weight`。

**预期**：debug 日志里 8001 出现约 50 次、8002 与 8003 各约 10 次；且同一后端不会连续出现 5 次（除非其它后端正好在冷却）。运行结果待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：如果三台后端权重都是 1，序列会是什么样？
**答**：`weighted=0`，`current_weight` 每轮各自加 1，第一台累加后最大被选、扣 3；实质就是 A B C A B C… 的严格顺序轮询。

**练习 2**：为什么选中后要 `best->current_weight -= total` 而不是 `-= best->effective_weight`？
**答**：减去「本轮总权重」让被选中者的累计量大幅下降（甚至变负），使下一轮其它 peer 更容易胜出，从而把高权重 peer 的多次命中**分散**到不同轮次——这正是「平滑」的数学来源。若只减自身权重，会退化成连续命中。

### 4.4 ngx_http_upstream_free_round_robin_peer —— 结果反馈与动态降权

#### 4.4.1 概念说明

`get` 负责「选」，`free` 负责「还」并反馈这次尝试的结果。框架在每次连接结束（无论成功失败）时调 `pc->free(&u->peer, u->peer.data, state)`，`state` 携带 `NGX_PEER_FAILED` / `NGX_PEER_NEXT` 等位。round_robin 据此做三件事：

1. **失败计数与冷却**：失败则 `fails++`，达 `max_fails` 后该 peer 在 `fail_timeout` 秒内被 `get` 跳过。
2. **动态降权**：失败时 `effective_weight` 下调；成功时缓慢回升（回升发生在 `get` 里）。
3. **释放连接**：`conns--`、`pc->tries--`。

这套机制让 nginx 不需要外部健康检查：它直接用真实请求的结果做被动健康探测。

#### 4.4.2 核心流程

`free_round_robin_peer`（[src/http/ngx_http_upstream_round_robin.c:1007-1019](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_upstream_round_robin.c#L1007-L1019)）只是个加锁包装；真正逻辑在 `_locked` 版本 [src/http/ngx_http_upstream_round_robin.c:1022-1100](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_upstream_round_robin.c#L1022-L1100)。

单节点快路径 [src/http/ngx_http_upstream_round_robin.c:1038-1054](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_upstream_round_robin.c#L1038-L1054)：只有一台后端时不做任何故障统计（反正没得换），只复位 `fails`、`conns--`、把 `pc->tries` 清零（不再重试）。

多节点失败分支 [src/http/ngx_http_upstream_round_robin.c:1056-1078](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_upstream_round_robin.c#L1056-L1078)：

```c
if (state & NGX_PEER_FAILED) {
    now = ngx_time();
    peer->fails++;
    peer->accessed = now;
    peer->checked = now;

    if (peer->max_fails) {
        peer->effective_weight -= peer->weight / peer->max_fails;   // 动态降权
        if (peer->fails >= peer->max_fails) {
            ngx_log_error(NGX_LOG_WARN, ... "upstream server temporarily disabled");
        }
    }
    if (peer->effective_weight < 0) {
        peer->effective_weight = 0;     // 不允许为负
    }
}
```

关键点：每次失败把 `effective_weight` 减 `weight/max_fails`（整除）。`max_fails` 次失败后，累计减了约 `weight`，于是 `effective_weight` 趋近 0；同时 `fails >= max_fails` 触发冷却——双管齐下把故障后端「冻住」。

成功分支 [src/http/ngx_http_upstream_round_robin.c:1080-1087](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_upstream_round_robin.c#L1080-L1087)：

```c
} else {
    /* mark peer live if check passed */
    if (peer->accessed < peer->checked) {
        peer->fails = 0;     // 复位失败计数
    }
}
```

`accessed` 是最近一次失败时间，`checked` 是最近一次被选中/检出的时间。`accessed < checked` 意为「这次成功请求的开始晚于上一次失败」，说明 peer 已恢复，于是清零 `fails`、退出冷却。

末尾公共收尾 [src/http/ngx_http_upstream_round_robin.c:1089-1099](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_upstream_round_robin.c#L1089-L1099)：`conns--`、`pc->tries--`（耗尽则框架不再重试）。

#### 4.4.3 故障→冷却→恢复的完整轨迹

设 A(weight=5, max_fails=2, fail_timeout=10s)，初始 `effective_weight=5, fails=0`：

1. **第 1 次失败**（state 含 `NGX_PEER_FAILED`）：`fails=1`，`accessed=checked=now`，`effective_weight -= 5/2 = 2` → **3**。此时 `fails(1) < max_fails(2)`，仍在轮询里。
2. **后续正常选择**：A 在 `get` 里被迭代时，因 `effective_weight(3) < weight(5)` 触发 `effective_weight++` → 4 → 5（每个被迭代的轮次 +1，缓慢爬升）。
3. **第 2 次失败**：`fails=2`，`effective_weight -= 2` → 再降，触发 `fails >= max_fails` 的 WARN「temporarily disabled」。
4. **冷却期**：`get` 的 `fails >= max_fails && now - checked <= fail_timeout` 命中，A 被 `continue` 跳过，不再参与分配，`effective_weight` 冻结（因为跳过的分支到不了 `++`）。
5. **10 秒后**：`now - checked > fail_timeout`，A 重新可被迭代，`effective_weight` 从冻结值开始每轮 `++` 缓慢回升，直到回到 5。
6. **首次成功**：`accessed < checked` 成立（这次成功晚于上次失败），`fails` 清零，完全恢复。

这就是 nginx 的「被动健康检查 + 慢启动恢复」：用真实流量探测，故障快速隔离（冷却），恢复时不是一刀切放回，而是让 `effective_weight` 渐进爬升，避免刚恢复的后端被瞬间打满。

#### 4.4.4 代码实践

**目标**：观察 `effective_weight` 的动态变化。

**操作**：

1. 配置 `upstream demo { server 127.0.0.1:8001 weight=5 max_fails=2 fail_timeout=10s; server 127.0.0.1:8002; }`，其中 8001 指向一个**会拒绝连接**的端口（如未监听）。
2. 开 `debug_http`，连续 `curl` 若干次。
3. 在 [src/http/ngx_http_upstream_round_robin.c:1072-1074](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_upstream_round_robin.c#L1072-L1074) 的 `ngx_log_debug2(... "free rr peer failed: %p %i", peer, peer->effective_weight)` 处观察 8001 每次失败后 `effective_weight` 的递减；在 [L753-L755](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_upstream_round_robin.c#L753-L755) 观察恢复期的递增。

**预期**：8001 的 `effective_weight` 从 5 降到 3（第 1 次失败），再次失败后触发 disabled 警告并进入 10 秒冷却；期间所有流量落到 8002；10 秒后 8001 重新出现，权重逐步爬升。运行结果待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：为什么 403/404 不会降低 `effective_weight`？
**答**：框架把 403/404 翻译成 `NGX_PEER_NEXT`（见 4.1.2）而非 `NGX_PEER_FAILED`，`state & NGX_PEER_FAILED` 为假，走成功分支，既不增 `fails` 也不降权——因为这类响应通常是「业务上这台不合适」（如无此资源），而非「后端坏了」。

**练习 2**：`effective_weight` 为何不允许小于 0？
**答**：`current_weight += effective_weight` 若加负数会让该 peer 永远垫底、几乎不可能再被选中；钳到 0 保证它仍有机会在其它 peer 也降权时被选中，避免「一次失败永久剔除」。

## 5. 综合实践

把本讲四个模块串起来，做一次「配置→选型→故障→恢复」的完整追踪。

1. **建表**：写一个含主备的 upstream：
   ```nginx
   upstream demo {
       server 127.0.0.1:8001 weight=3;
       server 127.0.0.1:8002 weight=1;
       server 127.0.0.1:8003 backup;
   }
   ```
   用 `nginx -t` 验证。对照 [src/http/ngx_http_upstream_round_robin.c:36-386](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_upstream_round_robin.c#L36-L386)，画出主表（8001、8002，`total_weight=4`）与备表（8003）两条链表，确认 `peers->next = backup`。

2. **正常分配**：用 `debug_http` 抓 8 个请求，按 4.3.3 的手算方法预测序列（应为 8001×3、8002×1 的某种平滑穿插，周期 4），与日志中 [L753](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_upstream_round_robin.c#L753) 打印的 `current: %p %i` 对照。

3. **故障降级**：停掉 8001 与 8002，让主表全军覆没。观察 [src/http/ngx_http_upstream_round_robin.c:772-796](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_upstream_round_robin.c#L772-L796) 的 `backup servers` 日志，确认请求递归切到备表 8003。

4. **恢复曲线**：只恢复 8001（8002 仍坏），观察 8001 的 `effective_weight` 在 [L1072](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_upstream_round_robin.c#L1072) 与 [L753](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_upstream_round_robin.c#L753) 处的降与升。

**预期产物**：一张标注了主/备链表、权重、冷却窗口的图，加一份与源码行号对应的请求分配日志片段。运行结果待本地验证。

## 6. 本讲小结

- round_robin 是 upstream 框架的**默认负载均衡策略**，靠「没有指令即生效」接管，它实现框架要求的 init_upstream / init / get / free 四个回调。
- 配置期 `init_round_robin` 把 `upstream {}` 块编译成主/备两条 peer 链表，记录 `total_weight`、`single`、`weighted`、`tries` 等汇总量；每个 peer 的 `current_weight=0`、`effective_weight=weight` 起步。
- 选择算法是**平滑加权轮询**：每轮累加 `effective_weight`，选 `current_weight` 最大者，再扣去本轮总权重；严格 `>` 比较保证同权时顺序轮询。7 次 5:1:1 的手算验证了「按比例 + 可周期 + 平滑」三性。
- 故障处理是**被动健康检查**：失败 `fails++` 并下调 `effective_weight -= weight/max_fails`，达 `max_fails` 后在 `fail_timeout` 内被 `get` 跳过（冷却）；恢复时 `effective_weight` 在 `get` 里缓慢 `++` 爬升，成功后 `fails` 清零——形成隔离与慢启动恢复。
- `tried` 位图支持 `proxy_next_upstream` 重试；主表耗尽递归切备表；单节点走快路径；403/404 记为 `NGX_PEER_NEXT` 不计故障。

## 7. 下一步学习建议

- **u7-l3 proxy 模块详解**：proxy 模块是 round_robin 最大的调用方，它会看 `peer.tries`、在失败时按 `proxy_next_upstream` 规则调 `peer.free` 并重试，是理解 round_robin 「被怎么用」的最佳续篇。
- **u7-l4 upstream 调度算法模块**：对比 `least_conn`、`hash`（一致性哈希）、`random` 如何各自实现同一套 init/get/free 接口——它们都「站在 round_robin 的肩膀上」，复用了 `ngx_http_upstream_rr_peer_t` 结构与失败计数逻辑。
- **u4-l3 共享内存与 slab**：当你给 upstream 加 `zone` 后，peer 表迁入共享内存，`get`/`free` 里的 `rlock`/`wlock`/`peer_lock` 宏才真正生效——届时回看本讲的锁宏会更有体会。
- 若想验证算法周期性，可把 4.3.3 的手算推广到任意权重，写个小脚本模拟 `current_weight` 演化，验证 \(\sum w_i\) 次后回归初值。
