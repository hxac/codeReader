# upstream 调度算法模块

## 1. 本讲目标

本讲是 upstream（反向代理/负载均衡）子系统的第四讲，承接 u7-l2 的 round_robin。u7-l2 讲了「默认策略」round_robin 怎么选 peer，本讲把视野放大：nginx 还内置了 `hash`、`least_conn`、`random`、`keepalive`、`zone` 等一组调度相关模块，它们各自解决 round_robin 解决不好的问题。学完本讲你应当能够：

- 说清这些调度模块**没有一个是从零写**的——它们都先调 round_robin 建 peer 表，再覆写 `peer.get`，只替换「怎么选」这一环；并理解 `keepalive` 更特殊，它用「装饰器」方式包在任意调度器外面。
- 读懂 `hash` 模块两种模式的实现：普通 `hash $key` 的 crc32 累加重哈希，以及 `hash $key consistent` 的**一致性哈希**（含 160 倍虚拟节点、排序、二分查找）。
- 读懂 `least_conn` 用「交叉相乘」比较 `conns/weight` 的技巧，以及 `random two` 如何用「两次随机取轻载」廉价近似 least_conn。
- 理解 `keepalive` 如何在不改调度算法的前提下，把空闲后端连接缓存复用，省去反复握手。
- 理解 `zone` 如何把 peer 表从「每 worker 一份私有内存」搬进**共享内存**，让多 worker 看同一份状态，并用 `config` 代际号处理「请求进行中 peer 集合变了」的并发问题。

## 2. 前置知识

本讲默认你已经掌握：

- **round_robin 三回调与 RR peer 结构**（u7-l2）：upstream 框架要求每个调度器提供配置期 `init_upstream`（建表）、每请求 `init`（装 get/free）、每请求 `get`/`free`（选/还）。peer 表节点是 `ngx_http_upstream_rr_peer_t`（含 `weight`/`effective_weight`/`current_weight`/`conns`/`fails`/`checked`），容器是 `ngx_http_upstream_rr_peers_t`（含主表 `peer` 链、备表 `next`、`total_weight`、`single`、`tried` 位图）。本讲所有模块都直接复用这套结构。
- **平滑加权轮询算法**（u7-l2）：每轮 `current_weight += effective_weight`，选最大者，扣去本轮总权重——这是 least_conn、random、一致性哈希在「并列处理」时都会复用的子程序。
- **共享内存、slab 分配器、rwlock**（u4-l3）：`zone` 模块把 peer 表放进 `mmap MAP_SHARED` 的共享内存，用 slab 分配节点、用读写锁保护并发——这些概念直接来自 u4-l3。
- **红黑树、侵入式 queue**（u2-l3）：keepalive 的空闲连接缓存用 `ngx_queue_t` 维护 LRU。

几个关键词先建立直觉：

- **一致性哈希（consistent hashing）**：把后端和 key 都映射到一个 0~2³² 的环上，key 顺时针找最近的后端；增删一台后端只影响相邻区段的 key，迁移量小。
- **虚拟节点**：一台后端在环上放很多个点（而非一个），让负载分布均匀，避免数据倾斜。
- **最小连接（least connections）**：不按固定权重，而按「当前活跃连接数」选后端，谁最闲选谁。
- **装饰器（decorator）**：keepalive 不替换调度算法，而是「包」在算法外层，在 get/free 前后插入缓存逻辑。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [src/http/modules/ngx_http_upstream_hash_module.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_upstream_hash_module.c) | `hash` 指令实现：普通哈希（crc32 重哈希）+ 一致性哈希（虚拟节点环） |
| [src/http/modules/ngx_http_upstream_least_conn_module.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_upstream_least_conn_module.c) | `least_conn` 指令实现：按 `conns/weight` 最小者选择 |
| [src/http/modules/ngx_http_upstream_random_module.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_upstream_random_module.c) | `random` 指令实现：加权随机 + `random two` 两次随机取轻载 |
| [src/http/modules/ngx_http_upstream_keepalive_module.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_upstream_keepalive_module.c) | `keepalive` 指令实现：装饰任意调度器，缓存空闲后端连接 |
| [src/http/modules/ngx_http_upstream_zone_module.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_upstream_zone_module.c) | `zone` 指令实现：把 peer 表搬进共享内存，支持跨 worker 共享与 DNS 动态增删 |
| [src/http/ngx_http_upstream_round_robin.h](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_upstream_round_robin.h) | RR peer/peers 结构、per-request 数据、锁宏、`config` 代际号字段 |
| [src/http/ngx_http_upstream.h](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_upstream.h) | `uscf->flags` 位定义（调度器声明它支持哪些 `server` 指令参数） |
| [src/http/ngx_http_upstream.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_upstream.c) | 框架侧：装配时挑 `init_upstream`、每请求调 `peer.init` 的调用点 |

## 4. 核心概念与源码讲解

### 4.1 通用骨架：调度器都站在 round_robin 的肩膀上

#### 4.1.1 概念说明

读这五个模块的源码，最先要建立一个全局印象：**除 `zone`、`keepalive` 外，调度算法模块（hash/least_conn/random）的实现套路高度一致**——它们都不是另起炉灶写一套 peer 表，而是：

1. 配置期先调 `ngx_http_upstream_init_round_robin(cf, us)`，让 round_robin 把 `upstream {}` 块编译成主/备 peer 链表（u7-l2 详述）。
2. 再把自己的「每请求 init」函数塞进 `us->peer.init`。
3. 每请求 init 里再调 `ngx_http_upstream_init_round_robin_peer(r, us)`（准备 `tried` 位图、拍 `config` 快照），最后把 `r->upstream->peer.get` 覆写成自己的选择函数。

于是**新算法只需关心「怎么选」，建表、失败计数、冷却、`tried` 位图全部白嫖 round_robin**。`free`（结果反馈）甚至都不用改——它仍是 round_robin 的 `free_round_robin_peer`，负责 `fails++`、降 `effective_weight`、`conns--`。所以你会在 hash/least_conn/random 的源码里看到：它们只有 `get`，没有自己的 `free`。

此外每个调度指令在注册时都会设置 `uscf->flags`，这是它向框架**声明「我支持哪些 `server` 指令参数」**。每个 flag 对应 `server` 指令的一个可选参数：

[src/http/ngx_http_upstream.h:122-129](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_upstream.h#L122-L129) — 例如 `WEIGHT`(支持 `weight=`)、`BACKUP`(支持 `backup`)、`MAX_CONNS`、`MAX_FAILS`、`FAIL_TIMEOUT`、`DOWN`。若调度器没声明某个 flag，你在 `server` 里写对应参数就会在 `nginx -t` 报错。

> **关键结论一**：调度算法模块 = round_robin 的建表/反馈逻辑 + 自己的 `get` 选择逻辑。读懂本讲的关键，就是看每个模块的 `get` 如何不同地「选」。
>
> **关键结论二**：`keepalive` 是异类——它不替换 `get`，而是把任意已注册调度器的 `get`/`init` 保存起来再包一层，属于**装饰器**模式（见 4.4）。

#### 4.1.2 核心流程

**框架何时挑调度器**：在 `ngx_http_block` 装配 http 配置的末尾，遍历每个 upstream 块，挑它的 `peer.init_upstream`；没显式指定就用 round_robin：

[src/http/ngx_http_upstream.c:7303-7306](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_upstream.c#L7303-L7306) — 这行就是 u7-l2 强调的「默认 round_robin」来源。任何 LB 模块想接管，只需在配置期把 `peer.init_upstream` 指向自己的 init 函数。

**调度指令的 set 回调做两件事**——设 `init_upstream` + 设 `flags`。以 `least_conn` 为例：

[src/http/modules/ngx_http_upstream_least_conn_module.c:318-342](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_upstream_least_conn_module.c#L318-L342) — 注意 `flags` 含 `NGX_HTTP_UPSTREAM_BACKUP`（least_conn 支持 backup，而 hash/random 不支持——对比 [hash 模块的 flags](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_upstream_hash_module.c#L766-L772) 就少了这一位）。若已存在 `init_upstream` 还会 WARN「load balancing method redefined」，说明一个 upstream 块里只能写一种调度算法。

**调度器的 init 函数永远是「先建表再覆写」**。三个算法模块的第一行都一样：

- [hash: ngx_http_upstream_init_hash](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_upstream_hash_module.c#L115-L125) — `init_round_robin` 建表，再 `us->peer.init = ngx_http_upstream_init_hash_peer`。
- [least_conn: ngx_http_upstream_init_least_conn](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_upstream_least_conn_module.c#L65-L79) — 同构。
- [random: ngx_http_upstream_init_random](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_upstream_random_module.c#L99-L117) — 同构（额外还建了一张 `ranges[]` 权重前缀和表，见 4.3）。

**每请求 init 同样「先复用再覆写」**：

[src/http/modules/ngx_http_upstream_least_conn_module.c:82-96](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_upstream_least_conn_module.c#L82-L96) — 调 `init_round_robin_peer`（准备 `tried` 位图与 `config` 快照），再 `peer.get = ngx_http_upstream_get_least_conn_peer`。

框架在请求开始时调这个每请求 init：

[src/http/ngx_http_upstream.c:840-844](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_upstream.c#L840-L844) — `uscf->peer.init(r, uscf)` 进入所选调度器的每请求初始化。

#### 4.1.3 代码实践

**目标**：在源码里验证「调度器 = round_robin 建表 + 自己的 get」这个骨架。

**操作**：

1. 打开 [hash 模块的 init_hash](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_upstream_hash_module.c#L115-L125)、[least_conn 的 init_least_conn](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_upstream_least_conn_module.c#L65-L79)、[random 的 init_random](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_upstream_random_module.c#L99-L117)。
2. 对照确认：三者第一行都是 `if (ngx_http_upstream_init_round_robin(cf, us) != NGX_OK)`，第二行都是 `us->peer.init = ...`。

**预期**：你会看到三段几乎一模一样的「壳」，差异只在 `peer.init` 指向谁。这正是「站在 round_robin 肩膀上」的代码证据。运行结果待本地验证。

#### 4.1.4 小练习与答案

**练习 1**：为什么 hash/random 的 `uscf->flags` 里没有 `NGX_HTTP_UPSTREAM_BACKUP`？
**答**：一致性哈希与随机选择都依赖「环上均匀分布」或「全量随机池」，backup 的「主表耗尽才启用」语义会破坏这种均匀性，故这两个模块不支持 `backup` 参数；least_conn 则保留了 backup（见 4.3 的备表降级）。

**练习 2**：这些调度模块为什么不需要写自己的 `peer.free`？
**答**：失败计数、`effective_weight` 降权、`conns--` 这些「还 peer」逻辑与「怎么选」无关，round_robin 的 `free_round_robin_peer` 已经通用，直接复用即可。

### 4.2 hash 模块：从普通哈希到一致性哈希

#### 4.2.1 概念说明

很多场景需要**会话粘性（session stickiness）**：来自同一用户/同一 key 的请求始终落到同一台后端（缓存命中、会话连续）。round_robin 做不到这点，`hash` 模块解决它。`hash` 有两种模式：

- **普通哈希** `hash $key;`：对 key 算哈希，按哈希值选后端。同一 key 永远落同一台。缺点：**增删一台后端会让几乎所有 key 重新映射**——因为 `hash % N` 里的 `N` 变了。
- **一致性哈希** `hash $key consistent;`：把后端和 key 都映射到 0~2³² 的环上，key 顺时针找最近的后端。增删一台后端只影响环上相邻区段的 key，迁移量约 \(1/N\)。为避免一台后端恰好占据大区段导致倾斜，每台后端在环上放**很多个虚拟节点**。

一致性哈希的迁移代价对比（\(N\) 台后端，增删一台）：

\[
\text{普通哈希迁移量} \approx \frac{N-1}{N}\text{ 的 key}, \qquad
\text{一致性哈希迁移量} \approx \frac{1}{N}\text{ 的 key}
\]

#### 4.2.2 核心流程（普通哈希）

普通哈希的选择函数 `ngx_http_upstream_get_hash_peer` 用「累加 + 重哈希」保证可重试：

[src/http/modules/ngx_http_upstream_hash_module.c:214-245](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_upstream_hash_module.c#L214-L245) — 核心算法（兼容 Perl 的 `Cache::Memcached`）：

```c
ngx_crc32_init(hash);
if (hp->rehash > 0) {                         // 第 2 次起，把重哈希序号当前缀
    size = ngx_sprintf(buf, "%ui", hp->rehash) - buf;
    ngx_crc32_update(&hash, buf, size);
}
ngx_crc32_update(&hash, hp->key.data, hp->key.len);
ngx_crc32_final(hash);
hash = (hash >> 16) & 0x7fff;                 // 只取中间 15 位
hp->hash += hash;                             // 累加到本轮总哈希
hp->rehash++;

w = hp->hash % hp->rrp.peers->total_weight;   // 在权重空间里定位
peer = hp->rrp.peers->peer;
while (w >= peer->weight) { w -= peer->weight; peer = peer->next; }
```

选 peer 用的是「在 `total_weight` 里取模、沿链表减权重」——和 round_robin 的权重空间定位同构。若选中的 peer「已试过 / down / 冷却中 / 满连接」，就 `goto next` 重哈希（`hp->rehash++` 改变哈希值再算），最多重试 20 次，超限就退化交给 round_robin：

[src/http/modules/ngx_http_upstream_hash_module.c:279-284](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_upstream_hash_module.c#L279-L284) — `if (++hp->tries > 20)` 后 `return hp->get_rr_peer(pc, &hp->rrp);`。`hp->get_rr_peer` 在每请求 init 时被设为 round_robin 的 `get_round_robin_peer`（[L161](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_upstream_hash_module.c#L161)），作为兜底。

key 本身来自配置期的「复值」编译（`hash $request_uri;` 这种含变量的 key）：

[src/http/modules/ngx_http_upstream_hash_module.c:149-152](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_upstream_hash_module.c#L149-L152) — `ngx_http_complex_value(r, &hcf->key, &hp->key)` 在请求时求值出真实 key 字符串。复值机制见 u6-l7。

#### 4.2.3 核心流程（一致性哈希）

一致性哈希的核心是**配置期把所有虚拟节点算好、排序**，运行期只做二分查找。

**配置期建环** `ngx_http_upstream_update_chash`：

[src/http/modules/ngx_http_upstream_hash_module.c:362](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_upstream_hash_module.c#L362) — `npoints = peers->total_weight * 160;` 整个 upstream 一共 `总权重 × 160` 个点。160 是经验值，足够多让分布均匀，又不至于内存爆炸。

[src/http/modules/ngx_http_upstream_hash_module.c:421-447](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_upstream_hash_module.c#L421-L447) — 每个 peer 生成 `weight × 160` 个点，哈希公式兼容 `Cache::Memcached::Fast`：

```c
ngx_crc32_init(base_hash);
ngx_crc32_update(&base_hash, host, host_len);     // 主机名
ngx_crc32_update(&base_hash, (u_char *) "", 1);   // 分隔符 \0
ngx_crc32_update(&base_hash, port, port_len);     // 端口

prev_hash.value = 0;
npoints = peer->weight * 160;
for (j = 0; j < npoints; j++) {
    hash = base_hash;
    ngx_crc32_update(&hash, prev_hash.byte, 4);   // 把上一轮哈希当输入
    ngx_crc32_final(hash);
    points->point[points->number].hash = hash;
    points->point[points->number].server = server;
    ...
    prev_hash.value = hash;                        // 链式：下一点基于本点
}
```

`prev_hash` 链式递推让一台后端的 160 个点散布在环各处（而非聚成一团），这是虚拟节点均衡负载的关键。

[src/http/modules/ngx_http_upstream_hash_module.c:450-461](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_upstream_hash_module.c#L450-L461) — `ngx_qsort` 按 `hash` 升序排，再去重相邻相同点。排序后环就是一个单调递增的数组，可二分查找。

**运行期二分查找定位** `ngx_http_upstream_find_chash_point`：

[src/http/modules/ngx_http_upstream_hash_module.c:489-518](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_upstream_hash_module.c#L489-L518) — 标准「找第一个 `>= hash` 的点」的二分，\(O(\log n)\)。

**运行期选择** `ngx_http_upstream_get_chash_peer`：

[src/http/modules/ngx_http_upstream_hash_module.c:621-679](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_upstream_hash_module.c#L621-L679) — 由 `point[hp->hash % points->number].server` 定位到某台**逻辑 server**，再在该 server 的所有 peer 实例（同一 `server` 名可能解析出多个 IP 地址）中用平滑加权选一个：

```c
server = point[hp->hash % points->number].server;   // 环定位到的逻辑后端
...
for (peer = hp->rrp.peers->peer, i = 0; peer; peer = peer->next, i++) {
    ...                                            // 跳过 tried/down/冷却/满连接
    if (peer->server.len != server->len || ngx_strncmp(...) != 0)
        continue;                                  // 只在同 server 的 peer 里选
    peer->current_weight += peer->effective_weight; // 平滑加权
    total += peer->effective_weight;
    ...
    if (best == NULL || peer->current_weight > best->current_weight) { best = peer; ... }
}
if (best) { best->current_weight -= total; goto found; }
hp->hash++;                                        // 本 server 无可用 peer，环上前进一步
```

这里有个精妙之处：一致性哈希定位的是「逻辑 server 名」，而一台 `server` 可能对应多个 IP（peer），所以在同 server 的 peer 间再用 round_robin 的平滑加权挑一个——把「环上选谁」与「同 server 多 IP 间均衡」两层职责分开。

#### 4.2.4 一致性哈希的数学直觉

把环 \([0, 2^{32})\) 想象成一个圆，\(N\) 台后端各投 \(160w_i\) 个点（\(w_i\) 为权重）。一个 key 落到环上位置 \(h\)，顺时针遇到的第一台后端即命中。各后端命中的概率（即环上管辖的弧长占比）约为：

\[
P_i \approx \frac{w_i}{\sum_j w_j}
\]

虚拟节点数（\(160w_i\)）越大，弧长方差越小，负载越均衡。增删一台后端时，只有它管辖的那段弧被邻居接管，迁移 key 数正比于它的概率份额——这就是 \(\approx 1/N\) 迁移量的来源。

#### 4.2.5 代码实践

**目标**：手算一致性哈希的「增删后端迁移量」，体会相对普通哈希的优势。

**操作**：

1. 假设 3 台后端，普通哈希 `hash $key;`，key 空间均匀。记下每个 key 当前落到哪台。
2. 增加 1 台（变 4 台），普通哈希下 `hash % 4` 与 `hash % 3` 几乎全不同，约 \(\frac{3}{4}\) 的 key 被重映射。
3. 改用 `hash $key consistent;`，同样加 1 台：新后端只在环上插入它自己的 \(160\) 个点，只「抢走」相邻区段的 key，约 \(\frac{1}{4}\) 的 key 迁移。

**预期**：一致性哈希的迁移量显著小于普通哈希。可写个小脚本用 crc32 模拟两种模式对比。运行结果待本地验证。

#### 4.2.6 小练习与答案

**练习 1**：普通哈希为什么用「累加 `hp->hash += hash` + 重哈希」而不是直接重选？
**答**：直接换哈希值会让相邻重试落在随机位置，无法保证「尽量覆盖不同 peer」。累加 + 把 `rehash` 序号当 crc32 前缀，让每次重哈希产生**可重复且单调递进**的新位置，配合 `tried` 位图跳过已试 peer，20 次内尽量覆盖更多 peer。

**练习 2**：一致性哈希为什么要 `prev_hash` 链式递推，而不是对 `host:port:j` 直接哈希？
**答**：为与 `Cache::Memcached::Fast` 协议兼容（跨客户端一致），且链式递推让一台后端的多个虚拟节点充分分散——若直接对序号哈希，分布也均匀但不是 nginx 选择的兼容方案。

### 4.3 least_conn 与 random：最小连接与两次随机

#### 4.3.1 概念说明

round_robin 与 hash 都是「按固定权重分配」，看不到后端的**实时负载**。`least_conn` 改为「选当前活跃连接数最少的」，让请求流向最闲的后端，天然适应后端处理速度差异（快的后端连接释放快，自然接更多）。

`random` 则走另一极端：纯随机，\(O(1)\) 选择，在 Peer 数极多时调度开销最小。但纯随机不看负载，可能扎堆。于是有 `random two`（两次随机取轻载）：随机抽两个，取 `conns` 较小的——这是「**两次选择的力量**（power of two choices）」经典结论：随机抽两个再取优，效果接近全量 least_conn，但代价只有两次随机。

#### 4.3.2 核心流程（least_conn）

`ngx_http_upstream_get_least_conn_peer` 的选择标准是**最小化 `conns / weight`**（连接数除以权重，权重大的后端允许更多连接）。为避免浮点除法，用**交叉相乘**比较：

[src/http/modules/ngx_http_upstream_least_conn_module.c:175-191](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_upstream_least_conn_module.c#L175-L191) — 比较 `peer->conns * best->weight < best->conns * peer->weight`，即判断 `peer.conns/peer.weight < best.conns/best.weight`：

```c
if (best == NULL
    || peer->conns * best->weight < best->conns * peer->weight)
{
    best = peer; many = 0; p = i;        // 找到更轻的
} else if (peer->conns * best->weight == best->conns * peer->weight) {
    many = 1;                             // 出现并列
}
```

交叉相乘把 `a/b < c/d` 变成 `a*d < c*b`，整数运算、无精度损失、无除零风险。

**并列处理**：若多台后端 `conns/weight` 相同（最常见于全部 `conns=0` 的初始态），`many=1`，进入第二趟：

[src/http/modules/ngx_http_upstream_least_conn_module.c:200-248](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_upstream_least_conn_module.c#L200-L248) — 在并列者之间用 round_robin 的**平滑加权**选一个（`current_weight += effective_weight`，选最大，扣 total）。所以 least_conn 在并列时退化为加权轮询，保证公平。末尾 `best->current_weight -= total`（[L248](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_upstream_least_conn_module.c#L248)）正是 u7-l2 那套算法。

**备表降级**：least_conn 支持 backup（`flags` 含 `BACKUP`），主表全失败时递归切备表：

[src/http/modules/ngx_http_upstream_least_conn_module.c:280-304](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_upstream_least_conn_module.c#L280-L304) — `rrp->peers = peers->next;` 切备表，清 `tried`，递归调自己。

#### 4.3.3 核心流程（random）

random 用一张配置期建好的**权重前缀和表 `ranges[]`** 实现 \(O(\log n)\) 的加权随机：

[src/http/modules/ngx_http_upstream_random_module.c:147-153](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_upstream_random_module.c#L147-L153) — 建表时 `ranges[i].range = total_weight` 累加，记录每段的权重起点。

[src/http/modules/ngx_http_upstream_random_module.c:472-495](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_upstream_random_module.c#L472-L495) — 运行期：`x = ngx_random() % total_weight` 取一个随机数，二分 `ranges[]` 找到 `x` 落在哪段，即选出 peer。权重大的段更长，被命中概率更高——加权随机。

`random;`（单随机）直接用选中的 peer（[get_random_peer](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_upstream_random_module.c#L215-L333)）。`random two;`（双随机）抽两个再比轻载：

[src/http/modules/ngx_http_upstream_random_module.c:390-430](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_upstream_random_module.c#L390-L430) — 抽第一个 `prev = peer`，再抽第二个，比较 `peer->conns * prev->weight > prev->conns * peer->weight`（同样交叉相乘），保留较轻的——这就是「power of two choices」。注意 `random two` 还可加 `least_conn` 参数（[L562](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_upstream_random_module.c#L562)），目前仅作语法校验（语义已默认是取轻载）。

#### 4.3.4 代码实践：对比 hash 与 least_conn 的 get_peer 差异（呼应实践任务）

**目标**：从「选 peer 的依据」与「数据结构」两维度对比 hash 与 least_conn。

**操作**：

1. 打开 [hash 的 get_hash_peer](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_upstream_hash_module.c#L167-L314) 与 [least_conn 的 get_least_conn_peer](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_upstream_least_conn_module.c#L99-L315)。
2. 填下表（答案见「预期」）：

   | 维度 | hash（普通） | least_conn |
   |---|---|---|
   | 选 peer 的依据 | ? | ? |
   | 是否看实时负载 | ? | ? |
   | 选不到时如何重试 | ? | ? |
   | 是否支持 backup | ? | ? |
   | 是否会粘性 | ? | ? |

3. 对应源码：hash 依据 `hash % total_weight`（[L237](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_upstream_hash_module.c#L237)），least_conn 依据 `conns * best->weight < best->conns * peer->weight`（[L182](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_upstream_least_conn_module.c#L182)）。

**预期答案**：

| 维度 | hash | least_conn |
|---|---|---|
| 依据 | key 的 crc32 哈希取模 | 实时 `conns/weight` 最小 |
| 看实时负载 | 否（只看 key 与权重） | 是（看 conns） |
| 重试 | 重哈希（改 `rehash` 再算），≤20 次 | 遍历自然选次轻的；主表空则递归切 backup |
| backup | 否 | 是 |
| 粘性 | 是（同 key 同后端） | 否 |

运行结果待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：least_conn 在所有后端 `conns=0` 时会怎样？
**答**：所有 peer 的 `conns/weight` 都为 0，全部并列，`many=1`，进入第二趟用平滑加权轮询选——即初始无负载时退化为 round_robin。

**练习 2**：`random two` 为什么比纯 `random` 更接近 least_conn？
**答**：纯随机不看负载，可能扎堆到忙的后端；抽两个取轻载的，用极小代价（多一次随机 + 一次比较）引入了对负载的偏好，理论上方差显著降低——这是「power of two choices」的经典权衡。

### 4.4 keepalive 模块：长连接缓存（装饰器模式）

#### 4.4.1 概念说明

到此为止的调度器都回答「选哪台后端」，但每次请求都新建一条到后端的 TCP 连接（若 HTTPS 还要 TLS 握手），开销巨大。`keepalive` 模块解决另一个问题：**把用完的后端连接缓存起来，下次给同一后端的请求直接复用**，省去握手。

关键在于 keepalive **不是调度器**——它不决定选哪台后端，而是**包在任意调度器外面**：保留原调度器的 `get`/`free`，在外层插入「先查空闲连接池」与「用完放回池」的逻辑。这就是**装饰器模式**。

四个配套指令：`keepalive N`（缓存上限）、`keepalive_requests`（单连接最大复用次数）、`keepalive_time`（单连接最大存活时长）、`keepalive_timeout`（空闲超时）。

#### 4.4.2 核心流程

**注册时机与众不同**：其他调度指令在解析到 `upstream {}` 里某条指令时立即设 `init_upstream`；keepalive 却推迟到 **http 配置全部解析完**（`init_main_conf`）才动手——因为它要等「真正的调度器」先注册好，再包装它：

[src/http/modules/ngx_http_upstream_keepalive_module.c:557-575](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_upstream_keepalive_module.c#L557-L575) — 装饰的核心三步：

```c
kcf->original_init_peer = uscfp[i]->peer.init;        // 1. 保存原调度器的每请求 init
uscfp[i]->peer.init = ngx_http_upstream_init_keepalive_peer;  // 2. 换成自己的
/* allocate cache items and add to free queue */
cached = ngx_pcalloc(cf->pool, sizeof(...) * kcf->max_cached); // 3. 预分配 N 个缓存槽
ngx_queue_init(&kcf->cache); ngx_queue_init(&kcf->free);
for (j = 0; j < kcf->max_cached; j++) {
    ngx_queue_insert_head(&kcf->free, &cached[j].queue);  // 全部先入 free 队列
}
```

于是 keepalive 维护**两条队列**：`cache`（空闲可用连接，LRU）与 `free`（空闲缓存槽）。预分配 `max_cached` 个槽，运行期零 malloc。

**每请求 init：层层包装**：

[src/http/modules/ngx_http_upstream_keepalive_module.c:174-186](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_upstream_keepalive_module.c#L174-L186) — 先调 `original_init_peer`（让真调度器装好它的 `get`/`free`），再把真调度器的 `get`/`free` 存为 `original_get_peer`/`original_free_peer`，最后把 `peer.get`/`free` 换成 keepalive 版：

```c
if (kcf->original_init_peer(r, us) != NGX_OK) { return NGX_ERROR; }  // 真调度器先装
kp->original_get_peer  = r->upstream->peer.get;
kp->original_free_peer = r->upstream->peer.free;
r->upstream->peer.get  = ngx_http_upstream_get_keepalive_peer;       // 我包在外层
r->upstream->peer.free = ngx_http_upstream_free_keepalive_peer;
```

**get：先让真调度器选地址，再查缓存要连接**：

[src/http/modules/ngx_http_upstream_keepalive_module.c:217-273](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_upstream_keepalive_module.c#L217-L273) —

```c
rc = kp->original_get_peer(pc, kp->data);   // 1. 真调度器选 sockaddr
if (rc != NGX_OK) { return rc; }

for (q = ...; q != sentinel; q = next(q)) { // 2. 在 cache 队列按地址找空闲连接
    if (ngx_memn2cmp(&item->sockaddr, &pc->sockaddr, ...) == 0) {
        ngx_queue_remove(q); ngx_queue_insert_head(&kp->conf->free, q);
        goto found;
    }
}
return NGX_OK;                                // 没找到，正常建新连接

found:
    c->idle = 0; ...
    pc->connection = c; pc->cached = 1;       // 命中：告诉框架别 connect
    return NGX_DONE;                          // NGX_DONE 跳过 ngx_event_connect 的连接建立
```

`pc->cached = 1` 配合返回 `NGX_DONE`，让 upstream 框架跳过 `ngx_event_connect`，直接复用 `c`。

**free：判断连接是否值得缓存，值得则入池，不值得则交还真调度器**：

[src/http/modules/ngx_http_upstream_keepalive_module.c:290-384](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_upstream_keepalive_module.c#L290-L384) — 一长串「不合格」判定（任一命中就 `goto invalid`，交还 `original_free_peer`）：

```c
if (state & NGX_PEER_FAILED || c == NULL
    || c->read->eof || c->read->error || c->read->timedout
    || c->write->error || c->write->timedout) goto invalid;   // 连接已坏
if (c->requests >= kp->conf->requests)            goto invalid;  // 超单连接复用上限
if (ngx_current_msec - c->start_time > kp->conf->time) goto invalid; // 超单连接存活上限
if (!u->keepalive)                                 goto invalid;  // 上游响应未同意 keepalive
if (!u->request_body_sent)                         goto invalid;  // 请求体未发完
if (ngx_terminate || ngx_exiting)                  goto invalid;  // 进程正在退出
if (ngx_handle_read_event(c->read, 0) != NGX_OK)   goto invalid;
```

合格则入池：若 `free` 队列空（池满），淘汰 `cache` 尾部（LRU 最旧）的连接关掉，腾出槽；把当前连接挂到 `cache` 头，起读定时器 `keepalive_timeout`，并换 handler：

[src/http/modules/ngx_http_upstream_keepalive_module.c:334-376](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_upstream_keepalive_module.c#L334-L376) — 关键收尾：`ngx_add_timer(c->read, kp->conf->timeout)`（空闲超时关连接）、`c->read->handler = ngx_http_upstream_keepalive_close_handler`、`c->idle = 1`。

**探测后端主动断开**：空闲连接可能被后端单方面关闭（如后端设了更短的 keepalive）。keepalive 用 `recv MSG_PEEK` 偷看一眼：

[src/http/modules/ngx_http_upstream_keepalive_module.c:414-424](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_upstream_keepalive_module.c#L414-L424) — `n = recv(c->fd, buf, 1, MSG_PEEK)`；返回 0 表示对端已关（EOF），返回 -1 且 EAGAIN 表示仍健康。`MSG_PEEK` 不消费数据，只探测。

#### 4.4.3 代码实践

**目标**：验证 keepalive 复用后端连接（减少握手）。

**操作**：

1. 配置：
   ```nginx
   upstream demo {
       server 127.0.0.1:8080;
       keepalive 16;            # 最多缓存 16 条空闲连接
   }
   server {
       location / {
           proxy_pass http://demo;
           proxy_http_version 1.1;          # HTTP/1.1 才默认支持 keepalive
           proxy_set_header Connection "";  # 清除 Connection: close
       }
   }
   ```
2. 在后端 8080 用 `tcpdump -i lo port 8080` 抓包，连续 `curl` 10 次。
3. 对照 [get_keepalive_peer 的 found 分支](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_upstream_keepalive_module.c#L253-L273)，每次命中会打印 `get keepalive peer: using connection %p`（需 `--with-debug`）。

**预期**：抓包只见一次 TCP 三次握手，后续 9 次请求复用同一连接（同一源端口）；debug 日志可见多次 `using connection` 命中。运行结果待本地验证。

#### 4.4.4 小练习与答案

**练习 1**：为什么 keepalive 必须在 `init_main_conf` 而非自己的指令 set 回调里包装 `peer.init`？
**答**：`keepalive` 指令通常写在调度指令（如 `least_conn`）之后，但配置解析顺序不保证谁先谁后，且 keepalive 需要「真调度器已注册」才能包装它。推迟到整个 http 配置解析完的 `init_main_conf` 阶段，才能确定 `peer.init` 已是最终调度器，安全地包一层。

**练习 2**：`pc->cached = 1` 与返回 `NGX_DONE` 各起什么作用？
**答**：`NGX_DONE` 让 upstream 框架跳过 `ngx_event_connect`（不建新连接、不发 connect 系统调用）；`pc->cached = 1` 告诉框架「这条连接来自缓存」，后续的连接建立与首次读处理走复用路径，避免对已建连接重复初始化。

### 4.5 zone 模块：共享内存里的 peer 表与 config 代际号

#### 4.5.1 概念说明

到此为止有个隐含前提：**peer 表在每个 worker 的私有内存里**。每个 worker 在自己的 `init_round_robin_peer` 时各有一份 peer 链表副本——这意味着 worker A 把某后端标为「冷却中」，worker B 完全不知道。`fails`、`conns`、`effective_weight` 这些动态状态在 worker 间不同步。

`zone` 模块解决这个：它把整张 peer 表搬进**共享内存**，所有 worker 看同一份，读写用 rwlock 保护。额外收益是支持 **DNS 动态增删后端**（`server example.com resolve;` 必须配 `zone` 才生效，因为解析结果要被所有 worker 看到）。

#### 4.5.2 核心流程

**注册共享内存**：

[src/http/modules/ngx_http_upstream_zone_module.c:80-130](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_upstream_zone_module.c#L80-L130) — `zone name [size];` 指令：用 `ngx_shared_memory_add` 登记一块共享区（u4-l3），最小 8 页（[L108](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_upstream_zone_module.c#L108)），把 `init` 钩子设为 `ngx_http_upstream_init_zone`。注意 `noreuse = 1`（[L127](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_upstream_zone_module.c#L127)）：reload 时这块共享内存**不复用旧映射**，因为结构可能变。

**初始化：首次复制 / reload 重连**：

[src/http/modules/ngx_http_upstream_zone_module.c:133-228](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_upstream_zone_module.c#L133-L228) — `shm.exists`（共享区已被旧 master 建好）则只把 `uscf->peer.data` 重新指向共享区里已有的 peers（[L147-162](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_upstream_zone_module.c#L147-L162)）；否则调 `copy_peers` 用 slab 分配器把整张表复制进共享内存。

[src/http/modules/ngx_http_upstream_zone_module.c:231-281](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_upstream_zone_module.c#L231-L281) — `copy_peers` 关键几步：

```c
config = ngx_slab_calloc(shpool, sizeof(ngx_uint_t));   // 建共享的 config 代际号
peers  = ngx_slab_alloc(shpool, sizeof(...peers_t));
ngx_memcpy(peers, uscf->peer.data, sizeof(...));          // 拷贝容器
peers->shpool = shpool;                                   // 标记「在共享内存里」
peers->config = config;
for (peerp = &peers->peer; *peerp; ...) {
    peer = ngx_http_upstream_zone_copy_peer(peers, *peerp); // 逐个深拷贝（slab 分配每个字段）
    (*peers->config)++;                                     // 每加一台，代际号 +1
}
```

`copy_peer` 把 `sockaddr`/`name`/`server` 等每个指针字段都在 slab 里重新分配并拷贝（[L370-493](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_upstream_zone_module.c#L370-L493)），因为共享内存的地址在各进程一致，而原配置池的地址不是。

**`peers->shpool` 是「是否加锁」的总开关**。RR 的锁宏定义如下：

[src/http/ngx_http_upstream_round_robin.h:135-164](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_upstream_round_robin.h#L135-L164) — 没配 zone 时 `shpool == NULL`，所有锁宏退化成空操作；配了 zone 则用 u4-l3 的 rwlock 真加锁：

```c
#define ngx_http_upstream_rr_peers_rlock(peers)  \
    if (peers->shpool) { ngx_rwlock_rlock(&peers->rwlock); }
#define ngx_http_upstream_rr_peers_wlock(peers)  \
    if (peers->shpool) { ngx_rwlock_wlock(&peers->rwlock); }
#define ngx_http_upstream_rr_peer_lock(peers, peer)  \
    if (peers->shpool) { ngx_rwlock_wlock(&peer->lock); }
```

u7-l2 看到的「配了 zone 才真正加锁」正是这条。同一套 `get` 源码因此对「私有表/共享表」都成立。

**config 代际号：处理「请求进行中 peer 集合变了」**。共享 peer 表可被 DNS resolver 或别的 worker 动态增删，若一个请求刚拍完快照、选 peer 进行到一半，表变了，基于旧快照的索引/位置可能失效。解法是代际号：

- 共享表有个 `*config` 计数器，每次增删 peer 都 `(*peers->config)++`（[zone 删除 peer L644](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_upstream_zone_module.c#L644)、[DNS 新增 peer L1003](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_upstream_zone_module.c#L1003)）。
- 每请求 init 时拍快照 [src/http/ngx_http_upstream_round_robin.c:538](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_upstream_round_robin.c#L538)：`rrp->config = rrp->peers->config ? *rrp->peers->config : 0;`
- 每次 `get` 校验 [src/http/ngx_http_upstream_round_robin.c:716](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_upstream_round_robin.c#L716)：`if (peers->config && rrp->config != *peers->config) goto busy;` —— 不一致就放弃本次（返回 `NGX_BUSY` 或退化交给 round_robin），让框架重试。

hash/least_conn/random 的 `get` 里都有同样的 `config` 校验（如 [hash:192-195](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_upstream_hash_module.c#L192-L195)、[least_conn:128-131](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_upstream_least_conn_module.c#L128-L131)）——这是 zone 场景下所有调度器的统一护栏。

**引用计数防止「使用中的 peer 被删」**：DNS 解析删某 peer 时，若某请求正持有它，不能直接 free，否则 use-after-free。解法是 `refs` 引用计数与 `zombie` 标志：

[src/http/ngx_http_upstream_round_robin.h:167-226](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_upstream_round_robin.h#L167-L226) — `peer_ref` 选 peer 时 `refs++`；删除时若 `refs > 0` 则只标 `zombie=1` 暂不释放，等 `peer_unref` 把 `refs` 减到 0 才真正 `slab_free`。

#### 4.5.3 代码实践：zone 如何让多 worker 共享 peer 状态（呼应实践任务）

**目标**：验证配 zone 后多 worker 共享同一份 `fails`/`conns`，故障隔离与负载统计跨 worker 汇总。

**操作**：

1. 配置（注意 `worker_processes 2;`）：
   ```nginx
   upstream demo {
       zone demo 64k;                       # 把 peer 表放进共享内存
       server 127.0.0.1:8001 max_fails=1 fail_timeout=30s;
       server 127.0.0.1:8002;
   }
   ```
2. 8001 指向一个未监听端口（必失败）。连续 `curl` 若干次，让请求分散到两个 worker。
3. 对照 [copy_peers 的 config 分配与 peer 拷贝](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_upstream_zone_module.c#L231-L281)：peer 表只有一份，在共享内存里；两个 worker 的 `get`/`free` 都通过 `rwlock` 操作同一份 `fails`/`conns`/`effective_weight`。
4. 说明流程：worker A 第一次连 8001 失败 → `free` 里 `fails++`（写共享表）→ 达 `max_fails`；worker B 下一次 `get` 读到同一份共享表，看到 8001 冷却中，直接选 8002——**无需自己再失败一次**。若没配 zone，worker B 会各自独立失败一次才隔离 8001。

**预期**：配 zone 时 8001 被「一次失败即全局隔离」；不配 zone 时每个 worker 各失败一次才各自隔离。这正是「共享 peer 状态」的收益。运行结果待本地验证。

#### 4.5.4 小练习与答案

**练习 1**：为什么锁宏里要判断 `if (peers->shpool)`？
**答**：同一份 `get` 源码要同时服务「无 zone（私有表，无需锁）」和「有 zone（共享表，需 rwlock）」两种情况。`shpool` 非空是「在共享内存里」的标志，非空才加锁，空则锁是 no-op，避免无 zone 时的无谓原子操作。

**练习 2**：`config` 代际号解决什么问题？
**答**：共享 peer 表可能在一个请求处理过程中被 DNS resolver 或别的 worker 增删节点。代际号让请求能检测到「我拍照之后表变了」，从而放弃基于旧索引的选择、重新走选择流程，避免用过期的 peer 位置/权重。

## 5. 综合实践

把本讲五个模块串起来，搭一个「一致性哈希 + 共享状态 + 长连接复用」的完整 upstream，追踪一次请求。

1. **配置**：
   ```nginx
   upstream demo {
       zone demo 64k;                       # 4.5：peer 表进共享内存
       hash $request_uri consistent;        # 4.2：一致性哈希，按 URI 粘性
       keepalive 16;                        # 4.4：复用后端连接
       server 127.0.0.1:8001 weight=3;
       server 127.0.0.1:8002;
       server 127.0.0.1:8003;
   }
   server {
       location / { proxy_pass http://demo; proxy_http_version 1.1; proxy_set_header Connection ""; }
   }
   ```
2. **装配追踪**：对照 4.1，确认配置期装配顺序——`hash` 指令把 `init_upstream` 设为 `init_chash`（建虚拟节点环）；`zone` 登记共享内存、`init_zone` 把 peer 表搬进 slab；`keepalive` 在 `init_main_conf` 末尾包住 `peer.init`。最终每请求 init 链是：`keepalive 的 init →（调用）original_init_peer = hash 的 init_chash_peer`。
3. **一次请求的 get 链**：`peer.get` 是 `get_keepalive_peer` → 先调 `original_get_peer = get_chash_peer`（按 URI 哈希在环上定位 server、同 server 多 peer 间平滑加权选 sockaddr）→ 再在 keepalive cache 队列按该 sockaddr 找空闲连接。命中则 `NGX_DONE` 复用，未命中则建新连接。
4. **跨 worker 共享**：停掉 8001，连续发请求。因配了 `zone`，任一 worker 检测到 8001 失败后，`fails` 写入共享表，所有 worker 立即知晓，一致性哈希环上 8001 的虚拟节点失效，受影响 key 顺时针落到 8002/8003——观察 `config` 代际号 `++` 与各 worker 行为一致。
5. **对比验证**：把 `hash ... consistent` 换成 `least_conn`，按 4.3.4 的表格重新填一遍 get 差异；把 `zone` 去掉，按 4.5.3 验证故障隔离不再跨 worker 共享。

**预期产物**：一张标注「装配顺序 / get 调用链 / 共享状态位置」的图，加一份与源码行号对应的 debug 日志片段（`consistent hash peer`、`get keepalive peer: using connection`、`config` 校验等）。运行结果待本地验证。

## 6. 本讲小结

- 五个模块都**站在 round_robin 肩膀上**：hash/least_conn/random 的 init 都先调 `init_round_robin` 建表、再覆写 `peer.get`，复用 RR 的建表、失败计数、`tried` 位图；它们只决定「怎么选」，`free` 直接用 round_robin 的。
- `uscf->flags` 是调度器向框架声明「支持哪些 `server` 参数」的位掩码；least_conn 含 `BACKUP` 而 hash/random 不含，解释了为何后两者不接受 `backup`。
- `hash` 两种模式：普通哈希用 crc32 累加 + 重哈希（兼容 `Cache::Memcached`）；一致性哈希在配置期生成 `total_weight × 160` 个虚拟节点（`crc32(HOST\0PORT PREV_HASH)` 链式递推）、排序、运行期二分定位，把增删后端的 key 迁移量从 \(\approx (N-1)/N\) 降到 \(\approx 1/N\)。
- `least_conn` 用**交叉相乘** `peer->conns * best->weight < best->conns * peer->weight` 最小化 `conns/weight`，整数无除法；并列时退化为平滑加权轮询。`random two` 是「power of two choices」：抽两个取轻载，廉价近似 least_conn。
- `keepalive` 是**装饰器**：在 `init_main_conf` 末尾把任意调度器的 `peer.init` 包一层，`get` 先让真调度器选地址、再查 `cache` 队列复用空闲连接（`pc->cached=1` + `NGX_DONE` 跳过握手），`free` 据健康度决定入池或交还；用 `recv MSG_PEEK` 探测后端主动断开。
- `zone` 把 peer 表搬进共享内存（slab 深拷贝每个字段），让多 worker 共享同一份 `fails`/`conns`/`effective_weight`，锁宏以 `shpool` 非空为加锁开关；`config` 代际号处理「请求进行中表变了」的并发，`refs`/`zombie` 引用计数防止使用中的 peer 被释放；zone 还是 DNS 动态增删后端（`server ... resolve`）的前提。

## 7. 下一步学习建议

- **sticky 模块与会话粘性**：本讲的 `hash` 提供「哈希粘性」，nginx 商业版另有 `sticky cookie`/`sticky route`/`sticky learn` 基于 cookie 或 SID 的粘性，思路类似（在 get 里按 hint 选 peer），可对照阅读 `ngx_http_upstream_sticky_module.c`（若你的构建启用）与 [ngx_http_upstream_get_rr_peer_by_sid](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_upstream_round_robin.c) 的 SID 路由。
- **ip_hash 模块**：`hash` 的前身，固定用客户端 IP 前 3 段做 key，是「无配置的 hash」特例，可作为对照阅读 `ngx_http_upstream_ip_hash_module.c`。
- **u7-l3 proxy 模块详解**：proxy 是这些调度器的最大调用方，它决定何时调 `peer.free`、何时按 `proxy_next_upstream` 重试，是把本讲的「选 peer」放进真实请求生命周期的关键续篇。
- **u10 缓存与限流**：`zone` 共享内存 + slab + 锁这套四件套（u4-l3）同样是 `limit_req_zone`、`proxy_cache` 的底层，回看 u4-l3 与本讲 4.5 会发现完全一致的「共享状态」骨架。
- 想深入一致性哈希，可手写一个小程序复刻 `update_chash` 的虚拟节点生成与二分查找，验证不同 `weight` 下的环上分布方差。
