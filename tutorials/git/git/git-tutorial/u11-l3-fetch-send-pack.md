# fetch-pack / send-pack 协商

## 1. 本讲目标

学完本讲，你应该能够：

- 说清「为什么 git 在传对象之前要先协商」，以及 `have` / `want` / `ACK` / `NAK` / `done` / `ready` 这几类报文各自代表什么。
- 读懂 `fetch-pack.c` 中两条协商路径：旧协议（v0/v1）的 `find_common` 多轮循环，与协议 v2 的 `do_fetch_pack_v2` 状态机。
- 理解 `fetch_negotiator` 虚表抽象，以及默认协商器（`negotiator/default.c`）用优先队列逐个「提交通告」提交（commit）的算法。
- 知道 `send-pack.c`（`git push` 的客户端）如何把对象打包并发送：调用 `pack-objects` 子进程、用 `^` 前缀排除双方都已有的对象。

本讲是传输与协议单元（u11）的收尾，承接 u11-l1（pkt-line / connect 建连）与 u11-l2（协议 v2 / serve 服务端分发），把视角从「服务端如何列 ref、分发命令」推进到「客户端与服务端如何共同求出对象集的差集，并真正传输一个 pack」。

## 2. 前置知识

### 2.1 协商解决的根本问题

git 的仓库是一棵以提交（commit）为节点的有向无环图（DAG）。当客户端要 fetch 或 push 时，它并不需要把整棵 DAG 都传一遍——双方很可能已经共享了大量的历史。真正要传的，是「只在其中一方可达、而对方没有」的那些对象。

于是产生一个核心问题：**在传输之前，双方如何求出彼此共同拥有的提交集合？** 这个过程叫「协商（negotiation）」。求出共同集合后，发送方就可以只发送「不在共同集合可达范围内的对象」，pack 体积因此大幅缩小。

可以把它理解成两个人对照各自的书架，先互相报「我有哪些书（have）」，确认了哪些是两人都有的（common），最后只把对方缺的那几本寄过去（pack）。

### 2.2 三类关键报文

| 报文 | 方向 | 含义 |
|------|------|------|
| `want <oid>` | 客户端 → 服务端 | 「我想要这个提交」 |
| `have <oid>` | 客户端 → 服务端 | 「我本地有这个提交」 |
| `ACK <oid> [common\|continue\|ready]` | 服务端 → 客户端 | 「这个提交我也有」（可附状态） |
| `NAK` | 服务端 → 客户端 | 「这一批 have 里没有共同的，继续」 |
| `done` | 客户端 → 服务端 | 「我报完了，请把 pack 发给我」 |
| `ready` | 服务端 → 客户端 | 「信息够了，我接下来就发 pack」 |

注意「客户端 / 服务端」在 fetch 与 push 中角色相反：

- **fetch**：客户端运行 `fetch-pack`（本讲），服务端运行 `upload-pack`。
- **push**：客户端运行 `send-pack`（本讲），服务端运行 `receive-pack`。

本讲两份源码都是「客户端」侧，但二者一收一发，互为镜像。

### 2.3 你需要记住的旧知识

- **pkt-line**（u11-l1）：线协议的每个数据包前有 4 字节十六进制长度前缀；`0000` 是 flush，标志一段报文结束。
- **prio_queue 优先队列**（u7-l1）：`revision.c` 遍历提交时用它按提交时间弹出最新提交。协商器复用了同一套设施。
- **对象标志位（flags）**（u3-l1 / u7-l1）：对象的 29 位 `flags` 是全局共享的临时操作资源，用完要清。协商器会占用其中 4 位。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [fetch-pack.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/fetch-pack.c) | `git fetch` 的协议实现：与服务端 `upload-pack` 协商、接收 pack。 |
| [send-pack.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/send-pack.c) | `git push` 的协议实现：与服务端 `receive-pack` 协商、打包并发送 pack。 |
| [fetch-negotiator.h](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/fetch-negotiator.h) / [fetch-negotiator.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/fetch-negotiator.c) | 协商器的接口定义与工厂：用虚表把「如何决定下一批发哪些 have」与具体算法解耦。 |
| [negotiator/default.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/negotiator/default.c) | 默认（consecutive）协商算法：按提交时间从新到旧连续通告。 |
| [connect.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/connect.c) | 建立连接、协商协议版本（u11-l1 详讲），把 `fd[2]` 交给 fetch-pack / send-pack。 |

## 4. 核心概念与源码讲解

### 4.1 为什么需要对象协商

#### 4.1.1 概念说明

「协商」就是求「双方共同拥有的提交集合（common commits）」。求出后，要传输的对象集等价于一个集合差：

\[ \text{要发送的对象} = \text{want 可达的对象} \setminus \text{common 可达的对象} \]

`common` 越大、越准，要传的对象就越少。但协商本身要往返通信，是有代价的——每多一轮就多一次网络往返（round-trip）。因此协商算法的核心权衡是：**用尽量少的轮次，把共同集合探得尽量大**。

#### 4.1.2 核心流程

旧协议（v0/v1）的协商是一个「批量 have → 读 ACK」的循环：

1. 客户端先发若干 `want` 行，声明想要哪些远程 ref。
2. 客户端分批发 `have` 行，每批以一个 flush（`0000`）结尾。
3. 服务端对收到的 have 逐一回应：命中共同提交回 `ACK <oid> common`，全部没命中回 `NAK`。
4. 当服务端认为信息足够（或客户端主动放弃）后，客户端发 `done`，服务端打包发送。

协议 v2 把它收敛成命令式交互：每轮发一个 `command=fetch` 请求（内含若干 have），服务端回一个 `acknowledgments` 段；收到 `ready` 即转入 `packfile` 段（见 u11-l2）。

#### 4.1.3 源码精读

`git fetch` 进入协议层后，总入口是 `fetch_pack`，它按协商出的协议版本分流：

- [fetch-pack.c:2188-2199](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/fetch-pack.c#L2188-L2199) —— 这是协议版本分流点：若 `version == protocol_v2` 走 `do_fetch_pack_v2`，否则走 `do_fetch_pack`（v0/v1）。`version` 由 u11-l1 的 `connect.c` 的 `discover_version` 在握手阶段确定，作为参数传进来。

v0/v1 的协商主体在 `find_common`。下面这段是它的核心循环——一边从协商器取下一个 `have`，一边攒够一批就 flush 出去读 ACK：

```c
/* fetch-pack.c:514-535（节选） */
while ((oid = negotiator->next(negotiator))) {       // 1. 取下一个要通告的提交
    packet_buf_write(&req_buf, "have %s\n", oid_to_hex(oid));  // 2. 写成 have 报文
    print_verbose(args, "have %s", oid_to_hex(oid));
    in_vain++;                          // 3. 统计「未获进展」的 have 数
    haves++;
    if (flush_at <= ++count) {          // 4. 攒够一批就 flush
        ...
        packet_buf_flush(&req_buf);
        send_request(args, fd[1], &req_buf);
        ...
        flush_at = next_flush(args->stateless_rpc, count);  // 5. 下一批的批量大一点
```

- [fetch-pack.c:514-609](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/fetch-pack.c#L514-L609) —— `find_common` 的主循环：取 have、攒批、flush、读 ACK 的完整编排。
- [fetch-pack.c:225-253](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/fetch-pack.c#L225-L253) —— `get_ack`：把服务端一行报文解析成 `NAK` / `ACK` / `ACK_common` / `ACK_continue` / `ACK_ready` 五种语义（用 `strstr` 在行尾查找关键字）。

报文格式与状态码的对应关系，可对照 `get_ack` 与枚举定义：

```c
/* fetch-pack.c:197-203 */
enum ack_type {
    NAK = 0,
    ACK,
    ACK_continue,
    ACK_common,
    ACK_ready
};
```

#### 4.1.4 代码实践

**目标**：用 pkt-line 追踪，肉眼看一遍真实 fetch 的协商报文。

**步骤**：

1. 在任意有远程的仓库里执行：
   ```
   GIT_TRACE_PACKET=/tmp/fetch.log git fetch
   ```
2. 打开 `/tmp/fetch.log`，定位 `have `、`ACK`、`NAK`、`done` 等行。

**需要观察的现象**：

- 第一段是若干 `want <oid> <capability>`，对应 `find_common` 里的 `want %s` 写入。
- 随后是一批 `have <oid>`，每批以 `<=` 形式的 flush 行收尾（pkt-line 的 `0000`）。
- 服务端在每批之后回 `ACK <oid> common` 或 `NAK`。
- 最后能看到一行 `done`，之后开始接收 pack 数据。

**预期结果**：你能把日志里的每一类行，对应到 4.1.3 列出的源码位置。具体行号与内容**待本地验证**（取决于你的仓库历史与服务端实现）。

#### 4.1.5 小练习与答案

**练习 1**：为什么协商要在 `done` 之前进行多轮，而不是一次性把所有本地提交都报出去？

> **参考答案**：一次性全报会让 `have` 列表极大，首包体积爆炸、也浪费时间在服务端已知的提交上。多轮批量协商能「早停」——一旦服务端说 `ready`（信息够了），就可以停止报更多 have。这也是 `in_vain` / `MAX_IN_VAIN` 机制存在的意义（见 4.3）。

**练习 2**：`want` 和 `have` 在语义上有什么本质区别？

> **参考答案**：`want` 表达「目标」（我要把仓库推进到这个提交之后），驱动服务端计算「最终要传哪些对象」；`have` 表达「现状」（我本地已经有这些），驱动双方求共同集合、从而缩小要传的对象集。

---

### 4.2 fetch-pack 客户端协商

#### 4.2.1 概念说明

`fetch-pack.c` 是 `git fetch` 的协议主体，负责与服务端的 `upload-pack` 对话。它做两件事：**协商**（求出共同提交）和**接收 pack**（见 `get_pack`）。这两件事按协议版本走两条不同的代码路径：

- v0/v1：`do_fetch_pack` → `find_common`（协商）→ `get_pack`（接收）。
- v2：`do_fetch_pack_v2` 的状态机，协商与接收由状态切换串起来。

无论哪条路径，决定「下一批发哪个 have」的工作，都委托给一个协商器对象（`fetch_negotiator`，见 4.3）。fetch-pack 自己只负责「把协商器吐出来的 oid 包装成 `have` 报文、把服务端的 ACK 反馈给协商器」。

#### 4.2.2 核心流程

协议 v2 是一个清晰的状态机，定义在 `do_fetch_pack_v2` 里：

```
FETCH_CHECK_LOCAL        先在本地标记完全的提交、过滤 ref，看是否已经全有
      │ 若未全有
      ▼
FETCH_SEND_REQUEST       组装 command=fetch 请求（含 have），发给服务端
      │ 若服务端未表示 ready
      ▼
FETCH_PROCESS_ACKS       读 acknowledgments 段：处理 ACK/NAK，喂回协商器
      │ 收到 ready
      ▼
FETCH_GET_PACK           读 shallow-info / wanted-refs / packfile 段，落盘 pack
      ▼
FETCH_DONE
```

关键点：**协商是多轮的**。在 `FETCH_SEND_REQUEST` 与 `FETCH_PROCESS_ACKS` 之间会反复跳转，直到服务端发出 `ready`（`received_ready`）才进入 `FETCH_GET_PACK`。

每轮请求里，have 报文由 `add_haves` 生成；每轮响应里，ACK 由 `process_ack` 解析。

#### 4.2.3 源码精读

v2 状态机的核心：

- [fetch-pack.c:1771-1844](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/fetch-pack.c#L1771-L1844) —— `do_fetch_pack_v2` 的 `while (state != FETCH_DONE)` 主循环，四个 case 对应四个状态。

```c
/* fetch-pack.c:1822-1843（节选） */
case FETCH_PROCESS_ACKS:
    process_section_header(&reader, "acknowledgments", 0);
    while (process_ack(negotiator, &reader, &common_oid,
                       &received_ready)) {      // 1. 逐条读 ACK，喂回协商器
        in_vain = 0;
        seen_ack = 1;
        oidset_insert(&common, &common_oid);   // 2. 累积共同提交
    }
    ...
    if (received_ready)
        state = FETCH_GET_PACK;                 // 3. 服务端 ready → 收 pack
    else
        state = FETCH_SEND_REQUEST;             // 4. 否则再来一轮
    break;
```

每轮「发哪些 have」由 `add_haves` 决定：

- [fetch-pack.c:1344-1377](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/fetch-pack.c#L1344-L1377) —— `add_haves`：循环调用 `negotiator->next()` 取下一个 oid，写成 `have`，直到达到本轮预算 `haves_to_send`；并在末尾把预算翻倍（`next_flush(1, ...)`），让每轮发的 have 数量指数增长。

```c
/* fetch-pack.c:1367-1374 */
while ((oid = negotiator->next(negotiator))) {
    packet_buf_write(req_buf, "have %s\n", oid_to_hex(oid));
    if (++haves_added >= *haves_to_send)
        break;
}
/* Increase haves to send on next round */
*haves_to_send = next_flush(1, *haves_to_send);
```

ACK 的解析与反馈：

- [fetch-pack.c:1520-1574](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/fetch-pack.c#L1520-L1574) —— `process_ack`：识别 `NAK` / `ACK <oid>` / `ready` 三类行；遇到 `ACK` 时调 `negotiator->ack()` 把该提交标记为共同，遇到 `ready` 时置 `received_ready`。它还校验段尾的分隔符（`ready` 后应是 `delim`，否则应是 `flush`）。

协商前的本地预处理：

- [fetch-pack.c:791-865](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/fetch-pack.c#L791-L865) —— `mark_complete_and_common_ref`：协商开始前，先把所有本地 ref 及其祖先标记为 `COMPLETE`，然后把「远程已有、且本地也完整」的提交通过 `negotiator->known_common()` 告诉协商器（这些是「已知双方共有」，无需再问服务端）。
- [fetch-pack.c:293-310](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/fetch-pack.c#L293-L310) —— `mark_tips`：把本地所有 ref 的 tip 提交（或仅 `--negotiation-restrict` 指定的那几个）喂给协商器的 `add_tip()`，作为遍历起点。

批量大小（每轮发多少 have）由两个常量与 `next_flush` 控制：

- [fetch-pack.c:273-291](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/fetch-pack.c#L273-L291) —— `INITIAL_FLUSH=16` 是首轮批量；`next_flush` 在 stateless 模式下把批量翻倍（超过 `LARGE_FLUSH=16384` 后改为 ×1.1）。这意味着前若干轮累计通告的 have 数近似

  \[ H_k \approx 16 \cdot (2^k - 1) \quad (\text{在达到 } 16384 \text{ 之前}) \]

  即每轮覆盖的历史范围呈指数扩张，这正是「快速逼近分叉点」的关键。

#### 4.2.4 代码实践

**目标**：对比 v0/v1 与 v2 两次 fetch 的报文形态差异。

**步骤**：

1. 用默认协议 fetch 一次，记录日志：
   ```
   GIT_TRACE_PACKET=/tmp/v2.log git fetch
   ```
2. 强制旧协议再 fetch 一次：
   ```
   GIT_TRACE_PACKET=/tmp/v0.log git -c protocol.version=0 fetch
   ```

**需要观察的现象**：

- `/tmp/v0.log` 里能看到裸的 `want` / `have` / `ACK` / `done` 行（对应 `find_common`）。
- `/tmp/v2.log` 里能看到 `command=fetch`、`acknowledgments` 段头、`ready`、`packfile` 段头（对应 `do_fetch_pack_v2` 状态机）。

**预期结果**：两份日志的报文结构明显不同，能对上 4.2.2 的状态机与 `find_common` 的循环。具体内容**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：v2 状态机里，`FETCH_SEND_REQUEST` 与 `FETCH_PROCESS_ACKS` 之间的跳转何时终止？

> **参考答案**：当 `process_ack` 在某轮读到 `ready`（置 `received_ready=1`），`FETCH_PROCESS_ACKS` 把状态切到 `FETCH_GET_PACK`，跳转停止。另外若 `add_haves` 本轮一个 have 都没产出且无新 ACK，`send_fetch_request` 会返回非 0 直接进入 `FETCH_GET_PACK`。

**练习 2**：`process_ack` 在收到 `ACK <oid>` 后做了哪两件事？

> **参考答案**：(1) 把该 oid 插入本地 `common` 集合（累积共同提交）；(2) 调 `negotiator->ack(commit)` 通知协商器「服务端确认这个提交是共同的」，让协商器据此把它的祖先也标记为共同、从而不再通告。

---

### 4.3 negotiator 通告算法

#### 4.3.1 概念说明

协商器（negotiator）回答一个问题：**下一轮，客户端该把哪些提交作为 `have` 报出去？** 把这个决策抽出来，是因为它有多种策略，而 fetch-pack 的报文收发逻辑对所有策略都一样。于是 git 用一张虚表 `struct fetch_negotiator` 把「策略」与「协议收发」解耦：

- **策略层**（negotiator）：决定下一个 have 是谁、如何处理 ACK。
- **协议层**（fetch-pack.c）：把 oid 包成 `have`、把 ACK 反馈回来。

git 提供三种协商算法，由配置 `fetch.negotiationAlgorithm` 选择：

| 算法 | 文件 | 策略 |
|------|------|------|
| `consecutive`（默认） | `negotiator/default.c` | 按提交时间从新到旧，连续逐个通告 |
| `skipping` | `negotiator/skipping.c` | 跳跃式通告，历史越深跳得越远，更快覆盖长历史 |
| `noop` | `negotiator/noop.c` | 不主动通告（用于 `--refetch` 等，已知无共同历史） |

#### 4.3.2 核心流程

默认算法（`negotiator/default.c`）用优先队列维护一棵「待通告的提交列表」：

1. **初始化**：`default_negotiator_init` 建一个 `prio_queue`，比较函数设为按提交时间排序；本地 ref 的 tip 经 `add_tip` 入队。
2. **取下一个 have**：`get_rev` 从队列弹出**最新**的提交：
   - 若该提交已被标记 `COMMON`（双方共有），则**不发 have**，并把它未共有的祖先继续标记为 `COMMON`（`mark_common`）；
   - 否则把它的 oid 作为本轮要发的 `have` 返回，并把它的父亲按相同策略入队。
3. **处理 ACK**：服务端确认某提交为共同后，调 `ack` → `mark_common`，把该提交及其祖先标记为 `COMMON`。
4. **终止**：当队列空、或 `non_common_revs`（队列中尚不共有的提交数）降为 0，`get_rev` 返回 `NULL`，fetch-pack 的取 have 循环结束。

它用 4 个对象标志位（注意：这是全局共享的 flags 资源，见 u3-l1 / u7-l1）：

```c
/* negotiator/default.c:12-16 */
#define COMMON     (1U << 2)   /* 双方都已知拥有的提交 */
#define COMMON_REF (1U << 3)   /* 服务端通告过、本地也完整，故推断共有 */
#define SEEN       (1U << 4)   /* 已进入优先队列 */
#define POPPED     (1U << 5)   /* 已离开优先队列 */
```

「进度」与「放弃」机制（位于 fetch-pack.c，但服务于协商器反馈）：

- `in_vain`：每发一个未获回应的 have 就 +1；一旦服务端回了 `ACK_common`/`ACK_continue`（说明有进展）就清零。
- [fetch-pack.c:68](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/fetch-pack.c#L68) —— `MAX_IN_VAIN` 定义为 256；当「已有进展（`got_continue`）但之后连续 256 个 have 仍无新进展」时，主动放弃继续通告，转去发 `done`。

#### 4.3.3 源码精读

虚表接口定义：

- [fetch-negotiator.h:20-63](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/fetch-negotiator.h#L20-L63) —— `struct fetch_negotiator`：六个函数指针 `known_common` / `add_tip` / `next` / `ack` / `have_sent` / `release`，外加一个 `void *data`。文件顶部注释清楚说明了调用顺序：「init → known_common(0..n) → add_tip(0..n) → next/ack 反复 → release」。

工厂按配置选择实现：

- [fetch-negotiator.c:8-25](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/fetch-negotiator.c#L8-L25) —— `fetch_negotiator_init`：根据 `r->settings.fetch_negotiation_algorithm` 分派到 `skipping` / `noop` / `default` 三者之一。

默认算法的取 have 核心：

- [negotiator/default.c:108-148](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/negotiator/default.c#L108-L148) —— `get_rev`：弹最新提交，三分支决定「不发 / 发但忽略祖先 / 发且遍历祖先」。

```c
/* negotiator/default.c:127-136（节选） */
if (commit->object.flags & COMMON) {
    /* do not send "have", and ignore ancestors */
    commit = NULL;
    mark = COMMON | SEEN;
} else if (commit->object.flags & COMMON_REF)
    /* send "have", and ignore ancestors */
    mark = COMMON | SEEN;
else
    /* send "have", also for its ancestors */
    mark = SEEN;
```

把提交（及其祖先）标记为共同：

- [negotiator/default.c:57-103](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/negotiator/default.c#L57-L103) —— `mark_common`：沿祖先链标记 `COMMON`，同时维护 `non_common_revs` 计数（每把一个 `SEEN` 且未 `POPPED` 的提交标为共同，计数减一）。
- [negotiator/default.c:171-176](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/negotiator/default.c#L171-L176) —— `ack`：服务端确认后调 `mark_common(commit, 0, 1)`，并返回「此前是否已被本地判定为共有」，供上层判断这条 ACK 是否带来新信息。

初始化与入队：

- [negotiator/default.c:191-207](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/negotiator/default.c#L191-L207) —— `default_negotiator_init`：建 `prio_queue`、设比较函数为按提交时间排序、首次初始化时清掉残留 flags。
- [negotiator/default.c:150-162](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/negotiator/default.c#L150-L162) —— `known_common` / `add_tip`：前者用于「推断共有」的 tip（`COMMON_REF`），后者用于普通 tip（仅 `SEEN`）。

#### 4.3.4 代码实践

**目标**：感受不同协商算法对「轮次 / have 数量」的影响。

**步骤**：

1. 用默认算法抓一次，看 trace2 统计：
   ```
   GIT_TRACE2_PERF=/tmp/perf-def.log git fetch
   ```
2. 切到 skipping 再抓一次：
   ```
   git config fetch.negotiationAlgorithm skipping
   GIT_TRACE2_PERF=/tmp/perf-skip.log git fetch
   ```

**需要观察的现象**：在 perf 日志里搜索 `negotiation_v2` region 下的 `round` 与 `haves_added`、`in_vain` 数据点。

**预期结果**：对于历史较深、与服务端分叉较远的仓库，`skipping` 通常用更少的轮次覆盖更深的共同历史（因为它会随深度递增地「跳过」提交）。具体数值**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：默认协商器为什么用「按提交时间从新到旧」的顺序通告 have？

> **参考答案**：提交时间越新，越可能接近客户端与服务端的分叉点——分叉点附近正是共同历史的边界。从新到旧逼近，能最快命中第一个共同提交，进而用 `mark_common` 一次性把它的祖先都标为共有，迅速收敛协商。

**练习 2**：`non_common_revs` 这个计数器的作用是什么？

> **参考答案**：它记录「队列里尚不能确认为共有的提交数」。`get_rev` 每弹出一个非共同提交就减一，每 `mark_common` 把一个 `SEEN` 提交标为共同也减一。当它降为 0，说明队列里剩下的提交都已确认共有，`get_rev` 返回 `NULL`，表示协商器这一侧「没有更多有价值的 have 可报了」。

---

### 4.4 send-pack：推送客户端的协商与打包

#### 4.4.1 概念说明

先厘清一个容易混淆的角色定位：**`send-pack.c` 是 `git push` 的客户端实现**，与服务端运行的 `receive-pack` 对话。它和 `fetch-pack.c` 是镜像关系——fetch 客户端「接收」pack，push 客户端「发送」pack。所谓「打包」发生在推送客户端这一侧：send-pack 要把本地有、远端没有的对象打包成一个 pack 流，通过连接发给服务端。

传统 push 的对象范围确定比较简单：发送端把每条要更新的 ref 写成一行命令 `<old-oid> <new-oid> <refname>`，然后 spawn `git pack-objects` 子进程，把「新 tip 可达、但排除 old tip 可达」的对象打成 pack。排除逻辑通过喂给 pack-objects 的 `^old new`（前缀 `^` 表示 negative，即「排除这些可达的对象」）来表达——这与 4.1 的集合差公式是同一回事。

进一步优化：若开启 `push.negotiate`，send-pack 会先在后台跑一次 `git fetch --negotiate-only`，和服务端额外探出一批共同提交，再把这些 commons 也喂给 pack-objects 当作排除项，从而让要传的 pack 更小。

#### 4.4.2 核心流程

`send_pack` 的编排分三段：

1. **发命令**：为每条要更新的 ref 写 `<old> <new> <refname>` 行，附带 capability（首条命令行用 `\0` 分隔能力串），flush 出去。
2. **打包发送**：若 `need_pack_data`（即存在非删除的 ref 更新），spawn `pack-objects --revs --stdout`，通过它的 stdin 喂 `^old new` 之类的修订参数；pack-objects 的 stdout 直接接到通往服务端的 socket（或经 sideband 复用）。
3. **读回执**：从 `receive-pack` 读 `unpack ok` 与每条 ref 的更新结果（`ok` / `ng` / `option ...`）。

`pack-objects` 的输入语义：

- `^<oid>`（negative）：排除该提交可达的对象（服务端已有 / 已确认共有）。
- `<oid>`（positive）：包含该提交可达的对象（要推送的新内容）。

#### 4.4.3 源码精读

主入口与编排：

- [send-pack.c:510-515](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/send-pack.c#L510-L515) —— `send_pack` 签名：接收仓库、参数、`fd[2]`（连接）、`remote_refs`（要更新的 ref 列表）与 `extra_have`。
- [send-pack.c:745-773](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/send-pack.c#L745-L773) —— 在发完命令后、`need_pack_data && cmds_sent` 时调用 `pack_objects` 生成并发送 pack；若失败仍尝试读回执以便把失败状态回填到 ref。
- [send-pack.c:691-710](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/send-pack.c#L691-L710) —— 发送 ref 更新命令：首条命令用 `%s %s %s%c%s`（`%c` 是 `\0`，后接 capability 串），后续命令为 `%s %s %s`。

打包子进程：

- [send-pack.c:60-151](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/send-pack.c#L60-L151) —— `pack_objects`：以 `pack-objects --revs --stdout` 启动子进程，stdout 默认直接接到 `fd`（推送连接）；在 stateless 模式下则用 `send_sideband` 把 pack 数据复用到连接上。

喂入修订参数（决定要打哪些对象）：

- [send-pack.c:45-55](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/send-pack.c#L45-L55) —— `feed_object`：negative（排除）时先写一个 `^`，再写 oid 与换行；positive 时只写 oid。

```c
/* send-pack.c:45-55 */
static void feed_object(struct repository *r,
                        const struct object_id *oid, FILE *fh, int negative)
{
    if (negative && !odb_has_object(r->objects, oid, 0))
        return;
    if (negative)
        putc('^', fh);          /* ^old 表示排除该提交可达的对象 */
    fputs(oid_to_hex(oid), fh);
    putc('\n', fh);
}
```

- [send-pack.c:103-114](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/send-pack.c#L103-L114)（在 `pack_objects` 内）—— 实际喂参顺序：先把 `advertised` / `negotiated`（服务端已有或协商出的共同对象）以 `^`（negative）喂入，再逐条 ref 把 `old_oid` 当 negative、`new_oid` 当 positive 喂入。这正是集合差 \( \text{new 可达} \setminus (\text{common} \cup \text{old 可达}) \) 的直接编码。

push 协商（可选优化）：

- [send-pack.c:435-508](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/send-pack.c#L435-L508) —— `get_commons_through_negotiation`：spawn 一个 `git fetch --negotiate-only` 子进程，把它的输出（每行一个共同提交 oid）收集进 `commons`，供 `pack_objects` 作为排除项。
- [send-pack.c:549-556](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/send-pack.c#L549-L556) —— 仅当 `push.negotiate` 为真时调用；注意它在 501-507 处把失败降级为 `warning`（「push negotiation failed; proceeding anyway」），因为协商结果只是优化、不是必需。

#### 4.4.4 代码实践

**目标**：观察推送时 pack-objects 子进程被如何调用、传了哪些参数。

**步骤**：

1. 在有写权限的远端上执行：
   ```
   GIT_TRACE=1 GIT_TRACE_PACK=1 git push <remote> <branch>
   ```
   （也可加 `-v`。）
2. 关注日志中 `pack-objects` 这条 trace 行。

**需要观察的现象**：日志会显示 spawn 出来的 `pack-objects` 命令行（含 `--revs`、`--stdout`、可能的 `--thin` / `--delta-base-offset`），即 `pack_objects` 里 `strvec_push` 的那些参数。

**预期结果**：看到的 pack-objects 参数与 `send-pack.c:75-90` 的 `strvec_push` 列表一致。能否复现、参数细节**待本地验证**（取决于是否开 thin pack、远端是否 shallow 等）。

> 若没有可写的远端仓库，可改做**源码阅读型实践**：对照 [send-pack.c:45-55](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/send-pack.c#L45-L55) 与 [send-pack.c:103-114](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/send-pack.c#L103-L114)，手写一段 pack-objects 的 stdin 内容：假设要推送一个 old=`A`、new=`B` 的 ref，且协商出共同提交 `C`，写出三行 `^C`、`^A`、`B`，并解释每一行的含义。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `feed_object` 对 negative 的 oid 要先检查 `odb_has_object`？

> **参考答案**：negative 项（`^old` / `^common`）用于排除「对方已有」的对象，这些对象本地未必真有。如果本地没有该对象却把它当作排除基准，会让 pack-objects 的对象遍历出错；先检查可以静默跳过本地不存在的排除项（见第 48 行的 `return`）。

**练习 2**：`push.negotiate` 失败时，send-pack 为什么只是 warning 而不是报错中止？

> **参考答案**：协商出的 commons 只用来缩小 pack（让推送更省流量），并非推送成功的前提。即使协商失败，send-pack 仍可只靠每条 ref 的 `old_oid` 做排除、正常完成推送。因此把失败降级为 warning、继续推送，符合「优化非必需」的定位。

---

## 5. 综合实践

把本讲三块知识（协议收发、协商器、push 打包）串起来，做一次「报文 ↔ 源码」对账。

**任务**：在一个有远程的仓库执行下面这条命令，并把日志分段标注对应的源码位置：

```
GIT_TRACE_PACKET=/tmp/full.log GIT_TRACE2_PERF=/tmp/full-perf.log \
  git -c protocol.version=2 fetch
```

完成以下对账清单（在 `/tmp/full.log` 与 `/tmp/full-perf.log` 中各找到证据）：

1. **建连与版本协商**：日志开头的版本协商行，对应 [connect.c:143](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/connect.c#L143) 的 `discover_version`（u11-l1）。
2. **v2 命令分发**：`command=fetch` 行，对应 [fetch-pack.c:1385](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/fetch-pack.c#L1385) 的 `write_fetch_command_and_capabilities`。
3. **第一组 have**：`have <oid>` 行，对应 [fetch-pack.c:1344-1377](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/fetch-pack.c#L1344-L1377) 的 `add_haves`；其 oid 的选取由 [negotiator/default.c:108-148](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/negotiator/default.c#L108-L148) 的 `get_rev` 决定。
4. **ACK 反馈**：服务端回的 `ACK` / `ready` 行，对应 [fetch-pack.c:1520-1574](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/fetch-pack.c#L1520-L1574) 的 `process_ack`，以及反馈回协商器的 [negotiator/default.c:171-176](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/negotiator/default.c#L171-L176) 的 `ack`。
5. **协商轮次统计**：perf 日志里 `negotiation_v2` region 下的 `round` / `haves_added` / `in_vain` 数据点，对应 [fetch-pack.c:1805-1807](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/fetch-pack.c#L1805-L1807) 的 trace2 埋点。
6. **收 pack**：`packfile` 段，对应 [fetch-pack.c:1845-1883](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/fetch-pack.c#L1845-L1883) 的 `FETCH_GET_PACK` 分支。

把每一项的日志原行摘出来贴在清单旁，你就完成了一份「一次 fetch 在源码里走过的完整足迹」。

## 6. 本讲小结

- 协商的目的是求出双方共同提交集合，使要传的对象集等于 \( \text{want 可达} \setminus \text{common 可达} \)，从而把 pack 压到最小。
- `fetch-pack.c` 按协议版本分两路：v0/v1 的 `find_common` 用「批量 have → 读 ACK」的循环，v2 的 `do_fetch_pack_v2` 用四状态机（CHECK_LOCAL → SEND_REQUEST → PROCESS_ACKS → GET_PACK）。
- 决定「下一批发哪个 have」被抽成协商器虚表 `fetch_negotiator`，默认实现 `negotiator/default.c` 用优先队列按提交时间从新到旧通告，靠 `COMMON` 标志位与 `non_common_revs` 计数收敛。
- 批量大小由 `INITIAL_FLUSH=16` 起步、`next_flush` 指数翻倍，配合 `in_vain` / `MAX_IN_VAIN=256` 实现「快逼近 + 早放弃」。
- `send-pack.c` 是 push 的客户端：发完 ref 更新命令后 spawn `pack-objects --revs --stdout`，用 `^old new` 的修订参数编码集合差，把 pack 流送到服务端；`push.negotiate` 会先用 `fetch --negotiate-only` 额外探出 commons 以进一步缩小 pack。

## 7. 下一步学习建议

- **服务端视角**：本讲全程是客户端，对应的服务端实现是 `upload-pack.c`（fetch）与 `receive-pack.c`（push）。建议阅读 `upload-pack.c` 看 `have`/`want` 是如何被接收、`ACK` 是如何被算出来并回送的，与本讲互为镜像。
- **pack 的生成与格式**：send-pack 把打包委托给了 `pack-objects`（见 u3-l3 的 pack 文件格式）。可结合 `builtin/pack-objects.c` 与 `pack-bitmap.c`（u13-l2）理解 bitmap 如何加速「该传哪些对象」的计算。
- **性能埋点**：本讲反复出现的 `trace2_region_*` 与 `trace2_data_*` 属于 trace2 子系统，详见 u13-l3。学会读 trace2 输出，是定位 fetch/push 慢在哪一轮的利器。
- **partial clone 与协商**：若对 `--filter` / promisor remote 感兴趣，可顺着 `send_filter`（fetch-pack.c:312）与 `receive_packfile_uris` 继续读，理解协商之后还可能伴随的「按需对象拉取」。
