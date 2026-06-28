# ngx.shared：跨 worker 共享字典

## 1. 本讲目标

学完本讲，你应当能够：

- 说清「为什么 nginx 多 worker 模型下，普通的 JS 变量无法在请求之间、worker 之间共享状态」，以及 `ngx.shared` 是如何解决这个问题的。
- 看懂 `js_shared_dict_zone` 指令的全部参数（`zone=name:size`、`type=string|number`、`timeout=`、`evict`、`state=`）以及它们各自启用哪条代码路径。
- 在源码层面理解一块共享内存是如何被组织成「slab 池 + 两棵红黑树 + 一把读写锁」的，以及 TTL 过期与 LRU 驱逐分别是如何实现的。
- 跟踪一次 `ngx.shared.foo.get/set/incr` 调用，从 JS 方法逐层进入引擎无关的核心函数，并指出类型校验、加锁、过期判定发生在哪一行。
- 独立设计一段 `nginx.conf`，用一个 `type=number` 的共享 zone 配合 `js_periodic` 做计数，再用 `js_content` 读出结果。

## 2. 前置知识

在进入源码前，先建立三个直觉。

**第一，nginx 是多进程模型。** nginx 启动后有一个 master 进程和若干 worker 进程（通常等于 CPU 核数）。每个 worker 是一个**独立的操作系统进程**，拥有自己独立的虚拟内存空间——worker A 的内存地址 `0x7f...` 与 worker B 的同名地址指向完全不同的物理页。请求被内核负载均衡到各 worker，因此连续两次请求很可能落在不同 worker 上。

**第二，JS 运行时是「按请求克隆」的。** 回顾 [u8-l2](u8-l2-http-js-module.md)：每个 HTTP 请求都会从模板 VM 克隆出一个独立的 njs VM（`engine->clone`），请求结束即销毁。这意味着即便你在模块顶层写了 `var count = 0;`，它也只是某个 worker 内、某次请求内的一份拷贝——既跨不了请求，更跨不了 worker。这是本讲要解决的核心痛点。

**第三，nginx 的共享内存（shared memory, shm）。** nginx 提供 `ngx_shared_memory_add` 机制：master 在 fork worker 之前，用 `mmap` 映射一段内存，并设置 `MAP_SHARED` 标志。fork 之后，所有 worker 都把这段物理内存映射进自己的地址空间，于是「同一块物理页」对所有 worker 可见、可写、且改动即时互见。这块内存由 nginx 的 slab 分配器（`ngx_slab_pool_t`）按页/槽管理，并配一把自旋锁防并发破坏。`ngx.shared` 字典就建在这块共享内存之上。

> 小贴士：红黑树（rbtree）是 nginx 内核里到处用的自平衡二叉搜索树，查找/插入/删除都是 \(O(\log n)\)。本讲的字典用**两棵**红黑树：一棵按 key 存值，一棵按过期时间排序——这是理解 TTL 与 LRU 的关键。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [nginx/ngx_js_shared_dict.c](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_shared_dict.c) | 全部实现：共享内存布局、双红黑树、过期/驱逐、所有字典 API 的 njs 与 QuickJS 双实现、`js_shared_dict_zone` 指令解析、状态文件持久化 |
| [nginx/ngx_js_shared_dict.h](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_shared_dict.h) | 对外导出的少量接口（全局属性回调、worker 初始化、模块声明） |
| [nginx/ngx_js.h](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js.h) | `ngx_js_main_conf_t` 与 `NGX_JS_COMMON_MAIN_CONF` 宏——`dicts` 链表的归属 |
| [nginx/ngx_http_js_module.c](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c) | `js_shared_dict_zone` 指令的注册表项与薄包装；`js_periodic` 指令（综合实践会用到） |

> 全局对象 `ngx` 本身由 [nginx/ngx_js.c](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js.c) 创建，`ngx.shared` 这个 getter 则由本讲文件挂上去——这一点会在 4.3 详细说明。

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：**zone 配置**（配置期发生什么）、**共享内存布局与 LRU**（运行期数据结构）、**字典 API**（JS 如何读写）。

### 4.1 zone 配置：js_shared_dict_zone 指令解析

#### 4.1.1 概念说明

`js_shared_dict_zone` 是一条 `http` 块级指令，用来**声明**一块共享字典区域。注意「声明」二字：它在配置解析期只做两件事——解析参数、向 nginx 申请一块共享内存并登记进 `jmcf->dicts` 链表。真正的数据结构初始化（红黑树、slab 池）发生在 nginx 启动后期、worker fork 之前的 `init` 回调里。

指令完整语法：

```nginx
js_shared_dict_zone zone=NAME:SIZE
                    [type=string|number]
                    [timeout=TIME]
                    [evict]
                    [state=FILE];
```

各参数的语义与启用路径：

| 参数 | 含义 | 启用的代码路径 |
|---|---|---|
| `zone=NAME:SIZE` | 区域名与字节数（必填），`SIZE` 最少 `8 * ngx_pagesize` | 申请 shm、登记 `dicts` 链表 |
| `type=string` | 值是字符串（默认） | `dict->type = NGX_JS_DICT_TYPE_STRING` |
| `type=number` | 值是数字，可用 `incr()` | `dict->type = NGX_JS_DICT_TYPE_NUMBER` |
| `timeout=TIME` | 启用 TTL，每个 key 可带过期时间 | 建第二棵红黑树 `rbtree_expire` |
| `evict` | 空间不足时按最久过期淘汰旧 key | `dict->evict = 1`，要求 `timeout` |
| `state=FILE` | 把字典内容持久化到文件，重启后恢复 | 注册 `save_event` 定时器 |

#### 4.1.2 核心流程

```text
配置解析期：
  ngx_http_js_shared_dict_zone()               (模块薄包装)
    └── ngx_js_shared_dict_zone()              (本文件，真正解析)
          ├── 逐个解析 zone=/type=/timeout=/evict=/state= 参数
          ├── 校验：size >= 8 页；evict 必须配 timeout；32 位下 state 不能配 timeout
          ├── ngx_shared_memory_add(name, size)   ← 向 nginx 申请共享内存
          ├── ngx_pcalloc(sizeof(ngx_js_dict_t))  ← 分配 per-zone 描述符
          ├── 头插 jmcf->dicts 链表
          └── shm_zone->init = ngx_js_dict_init_zone   ← 登记延迟初始化回调

nginx 启动期（fork worker 之前，对每个 zone 调一次）：
  ngx_js_dict_init_zone()
    ├── shpool = shm->addr                       ← slab 池就是这块共享内存的首部
    ├── sh = ngx_slab_calloc(sizeof(ngx_js_dict_sh_t))  ← 在池里分配共享头
    ├── ngx_rbtree_init(rbtree, sentinel, ngx_str_rbtree_insert_value)
    ├── 若 timeout：ngx_rbtree_init(rbtree_expire, ..., ngx_rbtree_insert_timer_value)
    └── ngx_js_dict_load()                       ← 若有 state 文件则加载恢复
```

#### 4.1.3 源码精读

指令注册在 http 模块的命令表里（薄包装只是把 `tag` 传给共享实现）：

- [nginx/ngx_http_js_module.c:714-717](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L714-L717) —— `js_shared_dict_zone` 指令表项。
- [nginx/ngx_http_js_module.c:10004-10008](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L10004-L10008) —— 包装函数，转调 `ngx_js_shared_dict_zone(cf, cmd, conf, &ngx_http_js_module)`，`tag` 用来区分 http 与 stream 各自的 zone。

真正的解析逻辑在共享层。先看参数解析的关键片段（`zone=` 与大小校验）：

[nginx/ngx_js_shared_dict.c:2971-3008](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_shared_dict.c#L2971-L3008) —— 解析 `zone=name:size`：用 `ngx_strchr` 找到冒号切分名字与大小，`ngx_parse_size` 把大小字符串转成字节数，并强制 `size >= 8 * ngx_pagesize`（否则报 "zone is too small"）。

紧接着是两条重要互斥校验：

[nginx/ngx_js_shared_dict.c:3067-3079](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_shared_dict.c#L3067-L3079) —— `evict` 必须搭配 `timeout=`（因为驱逐要从过期树里摘节点，没有 timeout 就没有过期树）；以及 32 位平台下 `state=` 不能搭配 `timeout=`（过期时间是 64 位毫秒，32 位下持久化语义有歧义）。

校验通过后，申请共享内存并登记：

[nginx/ngx_js_shared_dict.c:3081-3110](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_shared_dict.c#L3081-L3110) —— `ngx_shared_memory_add` 向 nginx 登记一块名为 `name`、大小为 `size` 的共享内存；`ngx_pcalloc` 分配 per-zone 描述符 `ngx_js_dict_t`，**头插**进 `jmcf->dicts` 链表（`dict->next = jmcf->dicts; jmcf->dicts = dict;`），并记下 `evict/timeout/type` 三个开关与 `save_event`。

`jmcf->dicts` 这个链表头来自 main 配置：

- [nginx/ngx_js.h:128-129](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js.h#L128-L129) —— `NGX_JS_COMMON_MAIN_CONF` 宏展开后的第一个字段就是 `ngx_js_dict_t *dicts;`。
- [nginx/ngx_js.h:214-216](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js.h#L214-L216) —— `ngx_js_main_conf_t` 仅由这个宏组成，所以整个 main_conf 的核心就是这条 dicts 链表。

延迟初始化回调（worker fork 前执行，对每块 zone 调一次）：

[nginx/ngx_js_shared_dict.c:2904-2925](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_shared_dict.c#L2904-L2925) —— 这段是「把一块裸共享内存变成字典」的关键：`shm->addr` 这段共享内存的首部天然就是一个 `ngx_slab_pool_t`；先在池里 `ngx_slab_calloc` 出共享头 `ngx_js_dict_sh_t`，再用 `ngx_rbtree_init` 初始化主树（按字符串 key 插入，`ngx_str_rbtree_insert_value`），若 `dict->timeout` 则再初始化过期树（按时间 key 插入，`ngx_rbtree_insert_timer_value`）。

> 注意 2981-2902 行的 `if (prev)` 分支：nginx 支持 `reload`（SIGHUP）时复用旧 worker 留下的共享内存。此时直接继承 `prev->sh` 与 `prev->shpool`，但会校验新配置的 `type`/`timeout` 与旧的一致，否则报错——这就是为什么你 `reload` 时不能改 zone 类型的原因。

#### 4.1.4 代码实践

**实践目标：** 用源码确认「为什么 `evict` 必须搭配 `timeout`」。

**操作步骤：**

1. 打开 [ngx_js_shared_dict.c:3067-3071](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_shared_dict.c#L3067-L3071)，读这段校验。
2. 顺着 `evict` 字段的使用处往下找，定位到 [ngx_js_dict_alloc (1387-1414)](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_shared_dict.c#L1387-L1414)：当 `dict->evict` 为真且 slab 分配失败时，会调 `ngx_js_dict_evict(dict, 16)` 淘汰节点再重试。
3. 再跳到 [ngx_js_dict_evict (1817-1856)](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_shared_dict.c#L1817-L1856)，看它遍历的是 `&dict->sh->rbtree_expire`。

**需要观察的现象：** 驱逐函数只从 `rbtree_expire` 取节点，而 `rbtree_expire` 仅在 [2901-2925 行](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_shared_dict.c#L2921-L2925) 当 `dict->timeout` 为真时才被初始化。

**预期结果：** 你能用自己的话解释——「没有 timeout 就没有过期树，驱逐无处下手，所以 `evict` 必须依赖 `timeout`」。这是一条由数据结构依赖推导出的配置约束。

#### 4.1.5 小练习与答案

**练习 1：** 为什么 `js_shared_dict_zone zone=foo:4k` 会被拒绝？

> **答：** 在 [3002-3006 行](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_shared_dict.c#L3002-L3006)，大小必须 `>= 8 * ngx_pagesize`（通常即 32KB）。4KB 太小，连 slab 池首部和共享头都难以容纳。

**练习 2：** 同名 zone 在 http 和 stream 里各声明一次，会共享同一份数据吗？

> **答：** 不会。[3081 行](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_shared_dict.c#L3081) 的 `ngx_shared_memory_add(cf, &name, size, tag)` 用 `(name, tag)` 二元组唯一标识一块 shm；http 模块传的 `tag` 是 `&ngx_http_js_module`，stream 传的是 `&ngx_stream_js_module`，二者被视为两块不同的共享内存。

### 4.2 共享内存布局：slab 池、双红黑树与 LRU 驱逐

#### 4.2.1 概念说明

一块共享 zone 在内存里长什么样？分三层。

最底层是 nginx slab 池（`ngx_slab_pool_t`），它是共享内存的首部，负责按页/槽分配小内存——所有字典节点、key 字符串、value 字符串都从它这里切出来。它在所有 worker 之间共享，自带一把自旋锁。

中间层是**共享头 `ngx_js_dict_sh_t`**，它本身也是 slab 池里 `calloc` 出来的一小块（见 4.1.3 的 2911 行），存着字典的全部「共享状态」：两棵红黑树的根、两棵树的哨兵、一把读写锁、两个脏标志位。

最上层是 **per-zone 描述符 `ngx_js_dict_t`**，它存在**普通进程内存**里（配置期 `ngx_pcalloc` 分配），每个 worker 各有一份，不共享。它持有指向共享头的指针 `sh`、指向 slab 池的指针 `shpool`，以及 `timeout/evict/type` 等配置开关——相当于「这个 worker 视角下对这块共享字典的句柄」。

数据节点 `ngx_js_dict_node_t` 是真正存 key-value 的单元，它**同时挂在两棵红黑树上**：`sn.node` 挂在主树（按 key 排序），`expire` 挂在过期树（按过期时间排序）。这种「一个节点、两棵树」的设计是 TTL 字典的经典手法。

#### 4.2.2 核心流程

读写并发模型靠 `ngx_rwlock`：所有 worker 共享同一把锁（锁字段在共享头里）。读操作（`get/has/keys/items/size/ttl`）持读锁 `ngx_rwlock_rlock`（多读并发），写操作（`set/incr/delete/clear`）持写锁 `ngx_rwlock_wlock`（独占）。所有 slab 操作都在持锁期间完成，因此 slab 自带的竞争在这层被屏蔽。

TTL 过期有两条独立路径，注意区分：

```text
惰性过期（lazy expiry）——读/写时顺手清：
  ngx_js_dict_expire(dict, now)
    从 rbtree_expire 的最小节点开始遍历
    while 节点->key <= now:                 ← 已过期
        从两棵树都摘除该节点，free
    一旦遇到节点->key > now 就停下（树有序，后面都不会过期）

主动驱逐（LRU evict）——空间不够时强行淘汰：
  ngx_js_dict_evict(dict, count)
    从 rbtree_expire 的最小节点开始，强制摘除最多 count 个节点
    （不管是否过期，越早过期的越先被踢——这就是 LRU 语义）
```

为什么驱逐「最早过期」等于「最久未使用」？因为每次 `set/incr` 命中已有 key 时，[ngx_js_dict_update](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_shared_dict.c#L1565-L1611) 与 [ngx_js_dict_incr](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_shared_dict.c#L1667-L1715) 都会把节点从过期树删掉、用 `now + timeout` 重新插入——即「续期」。于是过期树的最小节点，正是最久没被访问的那个。

#### 4.2.3 源码精读

**共享头 `ngx_js_dict_sh_t`**——这块内存在所有 worker 间共享：

[nginx/ngx_js_shared_dict.c:14-24](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_shared_dict.c#L14-L24) —— `rbtree`（按 key 存值）+ `sentinel`、`rwlock`（跨 worker 同步）、`rbtree_expire` + `sentinel_expire`（按过期时间排序）、`dirty`/`writing` 两个脏标志位（供状态文件持久化用）。

**per-zone 描述符 `ngx_js_dict_t`**——每个 worker 进程各一份，不共享：

[nginx/ngx_js_shared_dict.c:27-50](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_shared_dict.c#L27-L50) —— 持有 `shm_zone`、共享头指针 `sh`、slab 池指针 `shpool`、`timeout`/`evict`/`type` 三个开关、`save_event` 定时器与 `state_file` 路径、以及串成链表的 `next`。注意 41-42 行的两个类型宏：`NGX_JS_DICT_TYPE_STRING=0`、`NGX_JS_DICT_TYPE_NUMBER=1`。

**数据节点 `ngx_js_dict_node_t`**——「一节点双挂两棵树」的关键：

[nginx/ngx_js_shared_dict.c:59-63](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_shared_dict.c#L59-L63) —— `sn`（`ngx_str_node_t`，内含主树红黑树节点 + 字符串 key）、`expire`（过期树红黑树节点，`key` 字段存绝对过期时间）、`value`（字符串或数字的 union）。同一个节点既以 `sn.node` 进主树、又以 `expire` 进过期树。

**节点查找 `ngx_js_dict_lookup`**——所有 API 的公共前缀：

[nginx/ngx_js_shared_dict.c:1373-1384](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_shared_dict.c#L1373-L1384) —— 用 `ngx_crc32_long` 算 key 的哈希，再在主树里调 nginx 内建的 `ngx_str_rbtree_lookup`。注意这函数**不加锁**，调用方负责持锁（读或写）。

**惰性过期 `ngx_js_dict_expire`**：

[nginx/ngx_js_shared_dict.c:1795-1813](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_shared_dict.c#L1795-L1813) —— 从过期树最小节点起遍历；只要 `rn->key <= now` 就摘节点。注意 1803-1804 行用 `offsetof(ngx_js_dict_node_t, expire)` 从 `expire` 成员的地址反推出整个节点的首地址——因为遍历拿到的是 `expire` 字段，而要释放整个节点必须回到节点头部。摘除时要从两棵树都 `delete`（1808 删过期树、1810 删主树），最后 `node_free`。

**主动驱逐 `ngx_js_dict_evict`**：

[nginx/ngx_js_shared_dict.c:1833-1853](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_shared_dict.c#L1833-L1853) —— 与 `expire` 几乎一样的遍历，但**不看 `rn->key <= now`**，而是无脑淘汰最多 `count` 个最小节点。这就是 LRU。

**驱逐的触发点 `ngx_js_dict_alloc`**：

[nginx/ngx_js_shared_dict.c:1387-1414](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_shared_dict.c#L1387-L1414) —— 若 `dict->evict` 为真，slab 分配失败时不立刻报错，而是循环调 `ngx_js_dict_evict(dict, 16)` 每次淘汰 16 个最久未用的 key，再重试分配，直到成功或确已无可淘汰。

#### 4.2.4 代码实践

**实践目标：** 在源码里验证「续期 = LRU 的更新」这一论断。

**操作步骤：**

1. 打开 [ngx_js_dict_update:1605-1608](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_shared_dict.c#L1605-L1608) 与 [ngx_js_dict_incr:1699-1703](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_shared_dict.c#L1699-L1703)。
2. 观察这两处共同的模式：`ngx_rbtree_delete(rbtree_expire, &node->expire)` → `node->expire.key = now + timeout` → `ngx_rbtree_insert(rbtree_expire, &node->expire)`。

**需要观察的现象：** 每次写命中都会把节点从过期树「拔出来再插回最右端」（红黑树按 key 升序，`now + timeout` 是当前最大值），即该节点变成「最新」。

**预期结果：** 你能据此解释——一个频繁被访问的 key 永远不会成为 `ngx_js_dict_evict` 的牺牲品，因为它的 `expire.key` 总是被推到最大；驱逐总是先踢最久没写的 key。这正是 LRU 的语义。

> 待本地验证：若你已编译好带 njs 的 nginx，可参考 `nginx/t/js_shared_dict_evict.t` 构造一个 `evict` 的 zone，灌满后再写入，观察旧 key 是否被淘汰。

#### 4.2.5 小练习与答案

**练习 1：** 共享头 `ngx_js_dict_sh_t` 与描述符 `ngx_js_dict_t`，哪个跨 worker 共享？为什么这样设计？

> **答：** `ngx_js_dict_sh_t` 共享（在 slab 池里分配），`ngx_js_dict_t` 不共享（在配置期 `ngx_pcalloc` 进程内存里分配，每 worker 一份）。设计动机：可变状态（树、锁、节点）必须共享才能互通；而指向共享头的指针、配置开关等只读句柄没必要共享，放进程内存更简单、访问更快。

**练习 2：** 一个 `type=string` 且**没有** `timeout` 的 zone，它的 `rbtree_expire` 存在吗？

> **答：** 结构体字段永远存在（编译期固定），但不会被初始化、也不参与任何逻辑。[2921 行](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_shared_dict.c#L2921) 的 `if (dict->timeout)` 守卫了 `ngx_rbtree_init(rbtree_expire, ...)`，且所有遍历过期树的代码（`expire`/`evict`/节点插入）都受 `if (dict->timeout)` 保护。所以无 timeout 的 zone 实质上只用主树一棵。

### 4.3 字典 API：从 ngx.shared 到 get/set/incr/expire

#### 4.3.1 概念说明

从 JS 视角看，`ngx.shared` 是一个对象，它的每个属性名就是一个 zone 名，属性值是一个 `SharedDict` 对象。拿到 `SharedDict` 后，可调用一组方法读写共享内存。这套 API 在两引擎下外形一致，但绑定机制不同——这正呼应了 [u6-l2](u6-l2-dual-engine-module-pattern.md) 的「双引擎 = 双份代码」铁律。

整条调用链有两层解耦，值得牢记：

1. **JS 绑定层**（引擎相关）：负责把 JS 调用翻译成 C 调用，做类型校验、参数解析，从 `this` 取出 `shm_zone`。
2. **引擎无关核心层**（`ngx_js_dict_*` 函数）：拿 `ngx_js_dict_t *dict` 与 `ngx_str_t *key`，只管加锁、查树、改值。

正因为核心层与引擎无关，[4.2](#42-共享内存布局slab-池双红黑树与-lru-驱逐) 里讲的 `ngx_js_dict_get/set/incr/...` 才同时服务 njs 与 QuickJS 两份绑定。

#### 4.3.2 核心流程

以一次 `ngx.shared.foo.set('k', 'v')` 为例：

```text
JS:  ngx.shared.foo.set('k', 'v')
       │
       ├─ ngx.shared.foo  →  解析属性 foo，返回包了 shm_zone 的 SharedDict 对象
       │     njs:   njs_js_ext_global_shared_prop()   (遍历 jmcf->dicts 匹配名字)
       │     qjs:   ngx_qjs_shared_own_property()     (遍历 jmcf->dicts 匹配名字)
       │
       └─ .set('k','v')  →  从 this 取 shm_zone，校验类型，转调核心层
             njs:   njs_js_ext_shared_dict_set()   → ngx_js_dict_set()
             qjs:   ngx_qjs_ext_shared_dict_set()  → ngx_qjs_dict_set()
                                                          ↓
                               引擎无关核心：ngx_js_dict_set()
                                 ├── ngx_rwlock_wlock
                                 ├── ngx_js_dict_lookup(key)
                                 ├── 不存在 → ngx_js_dict_add()（或 MUST_NOT_EXIST 拒绝）
                                 │   存在 → ngx_js_dict_update()（或 MUST_EXIST 拒绝）
                                 ├── sh->dirty = 1
                                 └── ngx_rwlock_unlock
```

几个语义要点：

- **`set/add/replace` 共用 `ngx_js_dict_set`**，靠 flags 区分：`add` 用 `NGX_JS_DICT_FLAG_MUST_NOT_EXIST`（key 必须不存在，否则返回 false），`replace` 用 `NGX_JS_DICT_FLAG_MUST_EXIST`（必须存在），`set` 不设 flag（无所谓）。
- **类型校验在 JS 绑定层**：`set` 会按 `dict->type` 校验值是 string 还是 number；`incr` 直接拒绝非 number 字典。
- **过期判定在读路径里**：`get/has` 查到节点后，若 `dict->timeout` 且 `now >= node->expire.key`，视为不存在（惰性过期，不主动删）。

#### 4.3.3 源码精读

**`ngx.shared.<name>` 的解析（njs 引擎）**：

[nginx/ngx_js_shared_dict.c:522-563](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_shared_dict.c#L522-L563) —— `njs_js_ext_global_shared_prop` 是 `ngx.shared` 对象的属性 getter。它从 VM 元数据取 `ngx_js_main_conf_t`（即 `jmcf`，含 `dicts` 链表），遍历链表用名字匹配 `shm_zone->shm.name`，命中后用 `njs_vm_external_create` 创建一个包装了 `shm_zone` 的外部对象（原型为 `ngx_js_shared_dict_proto_id`）；找不到返回 `NJS_DECLINED`（属性为 null）。

**`SharedDict` 的方法表（njs 引擎）**：

[nginx/ngx_js_shared_dict.c:206-399](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_shared_dict.c#L206-L399) —— `ngx_js_ext_shared_dict[]` 是一张 `njs_external_t` 静态声明表（回顾 [u5-l4](u5-l4-external-objects-and-native-functions.md) 的外部对象机制）。注意 `add`（224 行）与 `replace`（353 行）共用同一个 native 函数 `njs_js_ext_shared_dict_set`，仅靠 `magic8 = NGX_JS_DICT_FLAG_MUST_NOT_EXIST` 或 `MUST_EXIST` 区分。

**`SharedDict` 的方法表（QuickJS 引擎）**：

[nginx/ngx_js_shared_dict.c:465-487](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_shared_dict.c#L465-L487) —— `ngx_qjs_ext_shared_dict[]` 是等价的 `JSCFunctionListEntry` 表，`add`/`replace` 同样靠 `JS_CFUNC_MAGIC_DEF` 的 magic 复用同一个 C 函数。`ngx.shared` 本身则由 [ngx_qjs_ext_ngx_shared:3261-3265](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_shared_dict.c#L3261-L3265) 创建一个 `NGX_QJS_CLASS_ID_SHARED` 类对象，由 [ngx_qjs_shared_own_property:3164-3213](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_shared_dict.c#L3164-L3213) 解析属性名。

**`incr` 的 JS 绑定（类型校验范例）**：

[nginx/ngx_js_shared_dict.c:899-979](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_shared_dict.c#L899-L979) —— `njs_js_ext_shared_dict_incr`：921-924 行先校验 `dict->type == NGX_JS_DICT_TYPE_NUMBER`，否则抛 "shared dict is not a number dict"；接着校验 `delta`、`init` 是数字；若给了第 4 个 timeout 参数则要求 zone 声明了 `timeout`。最后转调核心 `ngx_js_dict_incr`，把新值写回 `retval`。

**`incr` 的核心实现**：

[nginx/ngx_js_shared_dict.c:1667-1715](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_shared_dict.c#L1667-L1715) —— `ngx_js_dict_incr`：持写锁；查不到节点则用 `init + delta` 作为初值调 `ngx_js_dict_add` 新增；查到则 `node->value.number += delta` 直接改值（number 存在 union 里，无需分配）。若带了 timeout 则做一次「删过期树→重算→插回」的续期（1699-1703），即 4.2 所说的 LRU 更新。整个修改在写锁保护下原子完成，因此 `incr` 是跨 worker 安全的计数原语。

**`set` 的核心实现**：

[nginx/ngx_js_shared_dict.c:1432-1487](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_shared_dict.c#L1432-L1487) —— `ngx_js_dict_set`：持写锁查节点；按 flags 走 add 或 update；成功后 `sh->dirty=1` 并按需 arm `save_event`。失败走 `memory_error` 标签，抛出 `SharedMemoryError`（[1484 行](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_shared_dict.c#L1484) `njs_vm_error3(vm, ngx_js_shared_dict_error_id, ...)`）。

**`get` 的核心实现（含惰性过期）**：

[nginx/ngx_js_shared_dict.c:1718-1755](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_shared_dict.c#L1718-L1755) —— `ngx_js_dict_get`：持**读**锁；查到节点后，1735-1742 行检查「若 timeout 且 `now >= node->expire.key` 则当未找到」——这就是惰性过期（读到过期值不返回，但也不在此删节点，删除交给 `ngx_js_dict_expire` 在写入路径顺手做）。最后 `ngx_js_dict_copy_value_locked` 把共享内存里的字符串/数字拷贝成 njs 值返回。

> 模块初始化：njs 侧的 `ngx_js_shared_dict_proto_id` 与 `SharedMemoryError` 构造器在 [preinit/init:3129-3159](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_shared_dict.c#L3129-L3159) 注册；QuickJS 侧的三个类与 `ngx.shared` getter 在 [ngx_qjs_ngx_shared_dict_init:4358-4451](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_shared_dict.c#L4358-L4451) 注册。

#### 4.3.4 代码实践

**实践目标：** 跟踪一次 `dict.get('missing')` 与 `dict.get('expired')` 的差异。

**操作步骤：**

1. 打开 [ngx_js_dict_get:1718-1755](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_shared_dict.c#L1718-L1755)。
2. 对照下面两段 JS（来自 `nginx/t/js_shared_dict.t` 的真实用法）：

```javascript
// 取得某个 zone 的 SharedDict 对象
var dict = ngx.shared[r.args.dict];

// 情况 A：key 从来没写过
dict.get('missing');        // lookup 返回 NULL → 走 not_found → 返回 undefined

// 情况 B：key 写过但已过期（timeout=2s，等 3s 后）
dict.get('expired');        // lookup 命中 node，但 now >= node->expire.key
                            // → 仍走 not_found → 返回 undefined
```

**需要观察的现象：** 两种「拿不到值」的情况在 1731-1742 行汇合到同一个 `not_found` 标签，最终都 `njs_value_undefined_set(retval)`。

**预期结果：** 你能解释——从 JS 侧无法区分「key 不存在」和「key 已过期」，二者都返回 `undefined`；若需区分，应改用 `dict.has(key)` 配合带 timeout 的 zone，或用 `dict.ttl(key)`（[1280-1330 行](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_shared_dict.c#L1280-L1330)，过期返回 undefined、未过期返回剩余毫秒）。

#### 4.3.5 小练习与答案

**练习 1：** 为什么 `incr` 只允许 `type=number` 的 zone？

> **答：** 见 [921-924 行](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_shared_dict.c#L921-L924) 的类型校验。`incr` 的核心 [1696 行](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_shared_dict.c#L1696) 直接对 `node->value.number` 做加法——字符串值没有「加」的语义，且字符串修改要 realloc 共享内存，无法做成原子的读改写。所以自增计数必须用 number 字典。

**练习 2：** `set` 和 `add` 的返回值有何不同？

> **答：** 看 [1207-1213 行](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_shared_dict.c#L1207-L1213)：当 `flags` 非零（即 `add`/`replace`）时返回布尔值表示成功与否；`flags` 为零（即 `set`）时返回 `this`（字典本身），于是可以链式调用 `dict.set(k,v).get(k)`——这正是 `nginx/t/js_shared_dict.t` 里 `chain` 测试用到的写法。

## 5. 综合实践

**任务：** 用 `type=number` 的共享字典实现一个「跨 worker 请求计数器」——每秒由 `js_periodic` 自增，每次 HTTP 请求读出当前值返回，并解释为什么不能用模块顶层变量做这件事。

**第一步：准备 JS 模块 `counter.js`。**

```javascript
// counter.js
function tick() {
    // js_periodic 调用：每秒把 counters zone 里的 count 加 1
    // init 参数省略时默认 0；incr 是跨 worker 原子的
    ngx.shared.counters.incr('hits', 1, 0);
}

function show(r) {
    // js_content 调用：读出当前计数并返回
    var n = ngx.shared.counters.get('hits');
    r.return(200, 'hits: ' + (n === undefined ? 0 : n));
}

export default { tick, show };
```

**第二步：写 `nginx.conf`（关键片段）。**

```nginx
# 1) 声明一块 number 类型的共享 zone，名为 counters
js_shared_dict_zone zone=counters:32k type=number;

http {
    js_import counter.js;

    js_engine qjs;   # 推荐使用 QuickJS

    server {
        listen 127.0.0.1:8080;

        # 2) 每秒触发一次 counter.tick，interval 单位毫秒，默认 5000
        location /_tick {
            js_periodic counter.tick interval=1s;
        }

        # 3) 普通请求读取计数
        location / {
            js_content counter.show;
        }
    }
}
```

> 说明：`js_periodic` 指令注册在 [nginx/ngx_http_js_module.c:591-596](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L591-L596)，解析函数 [ngx_http_js_periodic:9654](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_http_js_module.c#L9654) 设默认 `interval=5000`（毫秒），可写成 `interval=1s`。它会在每个 worker 里周期性地克隆 VM 执行给定方法——因此 `tick` 同样要靠共享字典才能汇总跨 worker 的计数。

**第三步：运行并观察。**

1. 启动 nginx（`worker_processes` 设为大于 1，例如 4），多次 `curl http://127.0.0.1:8080/`。
2. 观察返回的 `hits` 随时间持续增长——每秒约增加 `worker_processes` 次（每个 worker 各自跑一次 `tick`）。
3. 反复请求 `/`，值只增不减、跨 worker 一致。

**第四步（关键）：解释为什么不能用模块作用域变量。**

把 `counter.js` 改成下面这样会**失败**：

```javascript
var hits = 0;                 // ❌ 模块顶层变量
function tick() { hits++; }   // 各 worker 各加各的，且 reload 后归零
function show(r) { r.return(200, 'hits: ' + hits); }
```

原因有三层，正好对应本讲前置知识：

1. **跨 worker 不可见**：`hits` 是某个 worker 进程内存里的普通变量，其他 worker 根本看不到——4 个 worker 会有 4 份独立的 `hits`。
2. **请求隔离**：即便不考虑多 worker，回顾 [u8-l2](u8-l2-http-js-module.md)，`js_content` 的 VM 是按请求克隆的；模块顶层变量在克隆后是一份独立的拷贝，`tick` 改的是自己那份，`show` 读的是另一份。
3. **生命周期**：worker 重启 / `reload` 后进程内存重置，计数归零；而共享字典的生命周期独立于 worker（甚至可用 `state=` 持久化到磁盘，重启恢复）。

只有 `ngx.shared` 这条路：它的存储在 master 申请的 `MAP_SHARED` 共享内存里（4.1），由所有 worker 映射；写操作靠共享头里的 `rwlock` 保证原子（4.2）；`incr` 在写锁内完成「读-改-写」是一次原子原语（4.3），因此能正确汇总所有 worker 的计数。

> 待本地验证：上述 `nginx.conf` 中的路径、`js_import`、`js_periodic` 写法需在你本地的 nginx+njs 环境中实际跑通；最小回归用例可参考 `nginx/t/js_shared_dict.t` 中 `incr` 与 `get` 的测试写法。

## 6. 本讲小结

- nginx 多 worker + 按请求克隆 VM 的模型，决定了普通 JS 变量既跨不了 worker 也跨不了请求；`ngx.shared` 通过 master 预先 `mmap` 的 `MAP_SHARED` 共享内存打破这道墙，是跨 worker 通信的标准方式。
- `js_shared_dict_zone` 在配置解析期只做参数解析与 shm 登记（[2945-3126 行](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_shared_dict.c#L2945-L3126)）；真正建树在 fork 前的 `ngx_js_dict_init_zone`（[2871-2942 行](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_shared_dict.c#L2871-L2942)）。
- 一块 zone = slab 池 + 共享头 `ngx_js_dict_sh_t`（主树 + 过期树 + 读写锁）+ per-worker 描述符 `ngx_js_dict_t`；数据节点 `ngx_js_dict_node_t` 同时挂在两棵红黑树上。
- TTL 靠过期树：惰性过期 `ngx_js_dict_expire` 在写路径顺手清，主动驱逐 `ngx_js_dict_evict` 在 `evict` zone 空间不足时按最久未用淘汰；续期（删-改-插）使驱逐天然具备 LRU 语义。
- API 分两层：引擎相关绑定层（njs 的 `njs_js_ext_*`、QuickJS 的 `ngx_qjs_ext_*`）做参数/类型校验并取出 `shm_zone`，引擎无关核心层（`ngx_js_dict_*`）加锁改树——`set/add/replace` 共用 `ngx_js_dict_set` 靠 flags 分叉，`incr` 仅限 number 字典且是跨 worker 原子计数原语。
- 读写靠共享头里的 `ngx_rwlock`：读操作并发持读锁，写操作独占持写锁；所有 slab 分配/释放都在锁内完成。

## 7. 下一步学习建议

- **状态持久化**：本讲只点到 `state=` 与 `save_event`。建议阅读 [ngx_js_dict_save (2507-2648)](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_shared_dict.c#L2507-L2648) 与 [ngx_js_dict_load (2651-2813)](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/ngx_js_shared_dict.c#L2651-L2813)，看脏标志 `dirty` 与写标志 `writing` 如何配合定时器实现「写临时文件→rename」的原子落盘，配套测试在 `nginx/t/js_shared_dict_state.t`。
- **运行回归测试**：用 `prove -I <tests-lib> nginx/t/js_shared_dict.t` 跑全套字典测试，并用 `TEST_NGINX_GLOBALS_HTTP='js_engine qjs;'` 切到 QuickJS 再跑一遍，对照本讲的「双绑定」理解两引擎各自的代码路径。
- **衔接 [u9-l3](u9-l3-subrequest-form-requestbody.md)**：共享字典常与 subrequest、表单解析组合成有状态服务（如去重、限流、会话）。下一讲讲请求体相关的高级能力，可与本讲的计数器/缓存模式结合练习。
- **回到 [u8-l1](u8-l1-ngx-js-shared-layer.md)**：若你对 `ngx.shared` 如何挂到全局 `ngx` 对象、`jmcf` 如何经 VM 元数据流转仍想加深理解，可重读共享绑定层一讲，把「配置期收集 → 请求期取用」的链路与本讲的 `jmcf->dicts` 串起来。
