# prod_cons 广播变体：rc_ 读计数与 epoch 协议

## 1. 本讲目标

本讲承接 [u4-l3 单播变体](u4-l3-prod-cons-unicast.md)，进入 `prod_cons.h` 中最精妙的部分：**广播（broadcast）变体**。

一条消息在单播队列里只投递给一个消费者，所以 u4-l3 只需要「写一个、读一个」的环形游标。而广播要求**一条消息必须被所有在线接收者都读过之后，这个槽位才能被生产者复用**。这就带来了单播没有的核心难题：生产者怎么知道「所有人都读完了」？某个慢读者会不会卡住整条队列？读者崩溃后留下的「永远读不完」的槽位又该怎么办？

学完本讲你应当能够：

- 说清广播元素 `rc_` 这个 64 位字段的位域布局：读计数位图、epoch、（多生产者版本还有）中间计数器。
- 解释为什么需要 epoch：它如何让 `force_push` 一次性作废所有「陈旧未读」的槽位。
- 读懂 `single-multi-broadcast`（即 `route`）的 push/pop，并手动演算一次「最后一个读者」的判定。
- 读懂 `multi-multi-broadcast`（即 `channel`）在单写版本之上额外引入的 `ct_`/`f_ct_` 提交协议。
- 说清 `force_push` 如何通过断连（`disconnect_receiver`）「踢掉失效读者」，从而在队列满时强行推进。

## 2. 前置知识

本讲默认你已经掌握以下概念（来自前置讲义）：

- **连接位图 `cc_t`**：广播模式下每个接收者占 1 bit，`cc_` 是一个 `uint32`，最多 32 个接收者（见 [u2-l4](u2-l4-route-vs-channel.md)）。本讲里你会反复看到它。
- **座位号 `connected_id()` vs 身份证号 `cc_id_`**：`connected_id()` 是本接收者在 `cc_` 位图里那个**单一 bit**（座位号），用于广播读计数；`cc_id_` 是单调递增的身份证号，用于过滤自发消息（见 [u3-l1](u3-l1-send-recv-data-path.md)）。本讲只用到座位号。
- **两层回调委托**：`prod_cons_impl` 只负责「选槽 + 把槽位指针喂给回调」，真正的消息构造由回调 `::new(p) T(...)` 完成（见 [u4-l1](u4-l1-queue-abstraction.md)）。所以你会在本讲看到 push/pop 签名里的 `F&& f`、`R&& out` 这些回调。
- **`cursor()` 与本地读游标 `cur`**：每个接收者进程持有一个本地读游标 `cursor_`，`pop` 通过比较 `cur` 与共享的 `cursor()` 判断是否有新消息（见 [u4-l1](u4-l1-queue-abstraction.md)）。
- **退避 `yield(k)`**：CAS 失败时不立即 `sleep`，而是按 4/16/32 的阈值分级退避（空转→PAUSE→yield→sleep），来自 [rw_lock.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/rw_lock.h#L62-L74)。

还有一个关键事实先点明：**两个广播变体都被库实际编译**（见 [ipc.cpp 的实例化门](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L846-L850)）。这与 u4-l3 形成鲜明对比——u4-l3 的两个多消费者单播变体虽然算法写好了，却被 `// TBD` 挡在门外；而本讲的 `route`（single-multi-broadcast）和 `channel`（multi-multi-broadcast）都是**活的**，分别由下面两行实例化：

```cpp
template struct chan_impl<ipc::wr<relat::single, relat::multi , trans::broadcast>>; // route
template struct chan_impl<ipc::wr<relat::multi , relat::multi , trans::broadcast>>; // channel
```

所以本讲不是在讲「仓库里吃灰的死代码」，而是在讲你每次 `ipc::route`/`ipc::channel` 收发时真正跑的算法。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用到的部分 |
|------|------|----------------|
| [src/libipc/prod_cons.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/prod_cons.h) | 全部四类生产-消费者算法的本体 | 两个广播特化：L195–L291（single-multi）、L293–L433（multi-multi） |
| [src/libipc/circ/elem_def.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/circ/elem_def.h) | 连接头 `conn_head` 与连接位图运算 | `connections()`、广播 `disconnect()` |
| [src/libipc/queue.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/queue.h) | 队列抽象层，持有本地座位号 | `connected_id()` 返回本进程座位号 |
| [src/libipc/circ/elem_array.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/circ/elem_array.h) | 共享内存里的元素数组 | `disconnect_receiver()` 的入口 |
| [src/libipc/ipc.cpp](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp) | 通道实现与显式实例化门 | L849–L850 证明两变体被编译 |

本讲几乎只围绕 `prod_cons.h` 一个文件展开，其他文件只用来解释算法里调到的几个辅助函数。

## 4. 核心概念与源码讲解

### 4.1 rc_ 位域与 epoch：把「谁还没读」编码进一个原子量

#### 4.1.1 概念说明

广播队列的核心矛盾是：**一个槽位被生产者写入后，必须等所有接收者都读过，才能被复用**。怎么记录「谁还没读」？

最直观的想法是给每个槽位配一个集合，记录「还需要读这个槽位的接收者」。libipc 的做法更紧凑：用一个 64 位原子量 `rc_`，把「待读接收者位图」直接塞进低 32 位（正好复用 [u2-l4](u2-l4-route-vs-channel.md) 的连接位图 `cc_t`，每个接收者 1 bit），把一个 **epoch（纪元）** 计数器塞进高位。

epoch 解决的是 **`force_push` 造成的「陈旧未读」问题**。想象这个场景：生产者往槽位 X 写了消息 M1，接收者 A 读完了、接收者 B 还没读。此时队列满了，生产者调用 `force_push` 想强行覆盖 X。它怎么知道槽位 X 里那些「未清零的读位」是 B 真的还没读 M1，还是 M1 早被作废了？

答案是 epoch：**每次 `force_push` 先把 epoch 加 1**。生产者写入时把当前 epoch 一起写进槽位；判断「是否还有人在读」时，**只有当槽位的 epoch == 当前 epoch，槽位里的待读位才算数**。epoch 一旦前进，旧 epoch 下残留的待读位就被「整体作废」，生产者可以放心覆盖。这比单播里逐个断连要高效得多。

> 术语提示：epoch（纪元）是一个单调递增的整数，作用类似「版本号」。它让旧数据带的标记自动失效，是一种常见的无锁 ABA 防护与批量作废手段。

#### 4.1.2 核心流程

两个广播变体的 `rc_` 位域布局不同，单写版本（route）更简单，多写版本（channel）多了「中间计数器」。先看位域对照：

**single-multi-broadcast（route，单写多读）的 `rc_` 布局：**

| 位段 | 宽度 | 含义 | 掩码/增量 |
|------|------|------|-----------|
| `[31:0]` | 32 位 | 待读接收者位图（哪些 receiver 还需读本槽） | `ep_mask = 0xffffffff` |
| `[63:32]` | 32 位 | epoch 纪元计数 | `ep_incr = 0x100000000` |

**multi-multi-broadcast（channel，多写多读）的 `rc_` 布局：**

| 位段 | 宽度 | 含义 | 掩码/增量 |
|------|------|------|-----------|
| `[31:0]` | 32 位 | 待读接收者位图 | `rc_mask = 0xffffffff` |
| `[55:32]` | 24 位 | 中间计数器（intermediate counter，防 ABA） | `ic_incr = 0x100000000`，`ic_mask = 0xff000000ffffffff` |
| `[63:56]` | 8 位 | epoch 纪元计数 | `ep_incr = 0x0100000000000000`，`ep_mask = 0x00ffffffffffffff` |

多写版本多出的「中间计数器」是因为有多个生产者并发抢槽：读者每次清位、写者每次占位都会让这个计数器自增，保证 `rc_` 的值不会在「绕一圈后」与历史值完全相同（ABA），从而让 CAS 失败检测更可靠。这部分在 4.3 详讲。

整体协作流程（以单写版本为例）：

```text
push(写消息 M 到槽 X):
  1. cc = 当前所有在线接收者位图
  2. 读 X.rc_，取低 32 位 rem_cc = 旧的待读位
  3. 若 (cc & rem_cc) 且 (槽的 epoch == 当前 epoch) → 还有在线 receiver 没读旧消息 → 满，返回 false
  4. CAS 把 X.rc_ 写成 (当前 epoch | cc)   # 高位 epoch，低位新一批待读位
  5. 写数据，wt_++

pop(接收者 r 读槽 X):
  1. 读数据
  2. CAS 把 X.rc_ 的低位清掉自己的那一位：rc_ &= ~connected_id(r)
  3. 清完后若低位 == 0 → 我是最后一个读者 → 通知上层(可回收)
```

#### 4.1.3 源码精读

先看单写版本如何声明 `rc_t` 与位域常量（这是整个广播算法的「字母表」）：

[prod_cons.h:L198-L209](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/prod_cons.h#L198-L209) 定义了 `rc_t = uint64_t`、`ep_mask`（低 32 位掩码 = 待读位图）和 `ep_incr`（第 32 位 = epoch 自增步长），以及每个槽位的 `elem_t` —— 注意它比单播多了一个 `std::atomic<rc_t> rc_{0}`，这就是「读计数器」。同处还有两个 `alignas(cache_line_size)` 成员：`wt_`（写游标）和 `epoch_`（纪元，单写所以是普通 `rc_t`，非 atomic）。

读计数位图复用了连接位图 `cc_t`（`uint32`），定义在 [elem_def.h:L19-L20](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/circ/elem_def.h#L19-L20)，并注释明言「广播模式最多 32 连接」——这就是 `rc_` 低 32 位能装下全部接收者的根本原因。

#### 4.1.4 代码实践

**实践目标**：把单写版本的 `rc_` 位域布局画出来，建立「一个 64 位原子量同时编码 epoch 和待读位图」的直觉。

**操作步骤**：

1. 打开 [prod_cons.h:L198-L209](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/prod_cons.h#L198-L209)。
2. 在纸上画一个 64 格的条，标出 `[31:0]`（`ep_mask`）和 `[63:32]`（epoch）。
3. 假设当前有 3 个接收者，座位号分别是 bit0、bit1、bit2（即 `cc = 0b111`），`epoch_ = 0`。写出生产者 push 后 `rc_` 的值。

**预期结果**：`rc_ = epoch_ | cc = 0 | 0b111 = 0x0000000000000007`。低 3 位为 1，表示这 3 个接收者都还没读。

**需要观察的现象**：epoch 占了整整高 32 位，空间「浪费」很大——这是为了让 epoch 几乎不可能回绕（\(2^{32} \) 次 `force_push` 才回绕），换取正确性简单。待本地验证：可在阅读时自行估算一台机器要多久才能触发 epoch 回绕。

#### 4.1.5 小练习与答案

**练习 1**：为什么单写版本的 `epoch_` 可以是普通 `rc_t` 而非 `std::atomic`？

> **答案**：因为 `relat::single` 表示只有一个生产者线程/进程会写它。不存在并发写，自然不需要原子性保护。对比 4.3 的多写版本，`epoch_` 就变成了 `std::atomic<rc_t>`。

**练习 2**：`ep_mask` 和 `ep_incr` 这两个名字里的 `ep` 指什么？`ep_mask` 在 push 和 pop 里分别用来取什么？

> **答案**：`ep` = epoch。`ep_mask`（低 32 位）在 push 里用来取出槽位中「陈旧的待读位」`rem_cc`，在 pop 里用来判断「清完自己的位后低位是否归零」（即是否最后一个读者）。`ep_incr` 是 epoch 自增的步长（第 32 位）。

---

### 4.2 single-multi-broadcast 的 push/pop（route 的真身）

#### 4.2.1 概念说明

`route`（`wr<relat::single, relat::multi, trans::broadcast>`）是单写多读广播。它有一个生产者、多个接收者，一条消息要广播给所有接收者。这是两个广播变体里较简单的一个，因为它**只有一个写者**，不需要 u4-l3 那套 `ct_`/`f_ct_` 提交协议——写者独占 `wt_`，写完直接可见。

它的难点全在「读侧」：多个接收者并发读同一个槽，每个人读完都要清掉自己的位，而且要判断「我是不是最后一个」。这个「最后一个读者」的判定，正是广播引用计数思想的雏形（与 [u3-l3](u3-l3-large-message-storage.md) 大消息外存的引用计数同源，不过那是另一套独立机制，本讲末尾会对比）。

#### 4.2.2 核心流程

**push（写者侧）**：

```text
loop:
  cc = connections()           # 在线接收者位图
  if cc == 0: return false     # 没人听，不必发
  X = elems[index_of(wt_)]     # 下一个要写的槽
  cur_rc = X.rc_               # 读这个槽的旧 rc_
  rem_cc = cur_rc & ep_mask    # 旧的待读位
  if (cc & rem_cc) 且 (槽epoch == 当前epoch_):
      return false             # 有在线 receiver 还没读旧消息，队列满
  CAS(X.rc_, cur_rc, epoch_ | cc)   # 抢占：写入当前 epoch + 新一批待读位
  成功则 break
写数据到 X.data_
wt_.fetch_add(1)
```

关键点：第 4 步的「满」判定同时检查了 **(a) 有在线 receiver 仍待读**（`cc & rem_cc`）**且 (b) 槽属于当前 epoch**。条件 (b) 是 epoch 的核心价值——若槽的 epoch 落后于当前，说明它里面的消息已被 `force_push` 作废，残留的 `rem_cc` 不算数，可以覆盖。

**pop（读者侧，某个接收者 r）**：

```text
if 本地 cur == cursor(): return false    # 没有新消息（cursor 即 wt_）
X = elems[index_of(cur++)]
读数据
loop:
  cur_rc = X.rc_
  if (cur_rc & ep_mask) == 0:            # 待读位已空（别人已清完）
      out(true); return true
  nxt_rc = cur_rc & ~connected_id(r)     # 清掉自己的那一位
  CAS(X.rc_, cur_rc, nxt_rc)
  成功: out((nxt_rc & ep_mask)==0); return true   # 清完低位==0 即最后一个读者
```

`out(bool)` 是一个回调，布尔参数 =「这次 pop 是否是最后一个读者」。算法把这个信息暴露给上层；注意当前 [ipc.cpp 的 recv 路径](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L645-L653) 用的是不带回调的 `pop(msg)`（默认空回调 `[](bool){}`），所以「最后一个读者」标志在本库的消息收发链路里**目前并未被直接消费**——它是算法预留的通用钩子。`rc_` 本身真正的作用是**让生产者知道槽位何时可复用**（低位归零即可覆盖）。

#### 4.2.3 源码精读

push 全文在 [prod_cons.h:L218-L241](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/prod_cons.h#L218-L241)。其中第 222 行 `wrapper->elems()->connections()` 读的是共享连接位图 `cc_`，定义在 [elem_def.h:L48-L50](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/circ/elem_def.h#L48-L50)。第 228 行那句判定就是上面流程里的「满」双条件：

```cpp
if ((cc & rem_cc) && ((cur_rc & ~ep_mask) == epoch_)) {
    return false; // has not finished yet
}
```

`~ep_mask` 取出的是高 32 位（槽里的 epoch），与当前 `epoch_` 比较。第 232–234 行的 CAS 把新值写成 `epoch_ | static_cast<rc_t>(cc)`——当前 epoch 进高位、新一批 `cc` 进低位。

pop 全文在 [prod_cons.h:L272-L290](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/prod_cons.h#L272-L290)。第 274 行 `cur == cursor()` 用本地游标对比 `wt_` 判空，`cursor()` 见 [prod_cons.h:L214-L216](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/prod_cons.h#L214-L216)。第 283 行 `wrapper->connected_id()` 取本接收者的座位号（单一 bit），定义在 [queue.h:L72-L74](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/queue.h#L72-L74)，第 284 行 CAS 清掉这一位。

#### 4.2.4 代码实践

**实践目标**：手动演算 3 个接收者依次读同一个槽，验证「最后一个读者」判定。

**操作步骤**：

1. 设 `epoch_ = 0`，3 个接收者座位号 `cc_id` 分别为 `0x1`、`0x2`、`0x4`，`cc = 0b111 = 7`。
2. 生产者 push 后，`X.rc_ = 0 | 7 = 7`。
3. 模拟接收者 A（`connected_id=0x1`）pop：`nxt_rc = 7 & ~0x1 = 0b110 = 6`；`6 & ep_mask = 6 ≠ 0` → 不是最后读者，`out(false)`。
4. 接收者 B（`0x2`）pop：`nxt_rc = 6 & ~0x2 = 0b100 = 4`；`4 ≠ 0` → `out(false)`。
5. 接收者 C（`0x4`）pop：`nxt_rc = 4 & ~0x4 = 0`；`0 == 0` → `out(true)`，**C 是最后一个读者**。

**需要观察的现象**：无论三个接收者以什么顺序到达，**只有清完位后低位恰好归零的那个人**会得到 `out(true)`。

**预期结果**：3 次清位后 `X.rc_` 低位归零，生产者下次 push 时 `rem_cc = 0`，槽可立即被复用（不会再触发「满」）。

**待本地验证**：上述演算基于源码逻辑推导；若要实跑观察，可用 `ipc::route` 起一个 sender、三个 receiver，确认每个 receiver 都收到同一条消息（广播语义），但 `out(true)` 的内部触发不会直接打印——需自行加日志（见 4.4.4）。

#### 4.2.5 小练习与答案

**练习 1**：若接收者 A 读完清位后**崩溃**，再也没人来读，槽位 `X.rc_` 的低位会发生什么？生产者会被永久卡住吗？

> **答案**：A 崩溃前若已清掉自己的位，则 `rc_` 低位只剩 B、C 的位；只要 B、C 正常读，最终仍会归零，不会卡住。真正会卡住的是「A 在清位**之前**崩溃」且 `force_push` 从不触发——此时低位永远留着 A 的位。这正是 4.4 `force_push` 要解决的问题（通过断连 + epoch 作废来推进）。

**练习 2**：push 第 228 行的「满」判定为什么要同时加 `(cur_rc & ~ep_mask) == epoch_` 这个条件？去掉它会怎样？

> **答案**：这个条件保证「只有当前 epoch 下写的消息，其待读位才会阻挡写入」。若去掉，一个被 `force_push` 作废的旧消息残留的待读位会永远挡住生产者，队列在 force_push 后仍然「假满」。epoch 条件让旧 epoch 的残留位自动失效。

---

### 4.3 multi-multi-broadcast：ct_/epoch/f_ct_ 提交协议

#### 4.3.1 概念说明

`channel`（`wr<relat::multi, relat::multi, trans::broadcast>`）是多写多读广播——多个生产者、多个接收者。它在 4.2 的「读侧广播」之上，还要解决 **多个生产者并发抢槽** 的问题，这与 [u4-l3 的 multi-multi-unicast](u4-l3-prod-cons-unicast.md) 一脉相承：引入「预约 → 写入 → 提交 → 发布」的协议。

为此多写版本在槽位上多加了一个 `f_ct_`（commit flag），并新增了共享的提交索引 `ct_`。`epoch_` 也从普通成员升级为 `std::atomic`（因为多个写者都会改它，尤其是 `force_push`）。`rc_` 的位域相应变窄了 epoch（只留 8 位），腾出 24 位给「中间计数器」做 ABA 防护。

#### 4.3.2 核心流程

多写版本在 `rc_` 之外增加了两个帮手函数，用来安全地「自增中间计数器」：

```text
inc_rc(rc):  保留 rc 的低位(待读位)和高位(epoch)，把中间计数器 +1
inc_mask(rc): inc_rc(rc) 然后清掉低位(待读位)   # 写者占新槽时用
```

**push（多写者侧）**：

```text
epoch = epoch_.load(acquire)          # 先快照当前 epoch
loop:
  cc = connections(); cc==0 则返回 false
  cur_ct = ct_.load()                 # 提交索引 = 下一个可预约的位置
  X = elems[index_of(cur_ct)]
  cur_rc = X.rc_; rem_cc = cur_rc & rc_mask
  if (cc & rem_cc) 且 (槽epoch == epoch): return false   # 旧消息还有人没读
  else if !rem_cc:                    # 槽看起来空，再查提交标志
      if X.f_ct_ != cur_ct 且 X.f_ct_ != 0: return false # 被别的写者占了
  # 抢占：CAS rc_ 为 (epoch | 中间计数器+1 | 新 cc)，且 CAS epoch_ 验证 epoch 未变
  if CAS(X.rc_, ...) 且 CAS(epoch_, epoch, epoch): break
写者独占地: ct_ = cur_ct+1; 写数据; X.f_ct_ = ~cur_ct   # 提交
```

两个 CAS 的「与」是精髓：第一个 CAS 抢到槽位（自增中间计数器 + 写入新 `cc`），第二个 CAS `epoch_.compare_exchange_weak(epoch, epoch, ...)` 是一个**「读屏障 + epoch 未变校验」**——它把 `epoch` 写回自身（无副作用），但如果在此期间有别的写者 `force_push` 改了 `epoch_`，这个 CAS 会失败并把 `epoch` 刷新为新值，于是外层 loop 用新 epoch 重试。这就是多写者感知「并发 force_push」的方式。

**pop（读者侧）**：与 4.2 类似地清自己的位，但多了两处变化——

```text
X = elems[index_of(cur)]
if X.f_ct_ != ~cur: return false      # 写者还没提交这槽，算空
++cur; 读数据
loop:
  cur_rc = X.rc_
  if (cur_rc & rc_mask) == 0:         # 都读完了
      out(true); X.f_ct_ = cur+N-1; return true   # 释放槽位给写者复用
  nxt_rc = inc_rc(cur_rc) & ~connected_id(r)      # 自增中间计数器 + 清自己位
  last_one = ((nxt_rc & rc_mask)==0)
  if last_one: X.f_ct_ = cur+N-1      # 我是最后读者，释放槽位
  CAS(X.rc_, cur_rc, nxt_rc); out(last_one); return true
```

读者先用 `f_ct_ == ~cur` 确认「写者已提交」再读，避免读到写了一半的数据；读完若是最后一人，把 `f_ct_` 重置为释放标记（`cur + N - 1`，`N = elem_max = 256`），让写者知道此槽可回收。

> 简而言之：`ct_` 是写者们的「预约指针」，`f_ct_` 是每个槽的「提交/释放状态机」。`ct_` 由成功 CAS 的那个写者推进，因此同一时刻只有一个写者真正在写数据（源码注释 `only one thread/process would touch here at one time` 即指此）。

#### 4.3.3 源码精读

位域常量与 `inc_rc`/`inc_mask` 在 [prod_cons.h:L299-L327](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/prod_cons.h#L299-L327)。注意 `ic_mask = 0xff000000ffffffff` 保留了最低 32 位与最高 8 位、清空中间 24 位——这正是「自增中间计数器时不动 epoch 和待读位」的实现。

push 全文在 [prod_cons.h:L329-L364](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/prod_cons.h#L329-L364)。第 333 行先 `epoch_.load(acquire)` 快照；第 351–353 行就是那个「双 CAS」：

```cpp
if (el->rc_.compare_exchange_weak(
            cur_rc, inc_mask(epoch | (cur_rc & ep_mask)) | static_cast<rc_t>(cc),
            std::memory_order_relaxed) &&
    epoch_.compare_exchange_weak(epoch, epoch, std::memory_order_acq_rel)) {
    break;
}
```

`inc_mask(epoch | (cur_rc & ep_mask))` 的含义：用当前 epoch 覆盖高 8 位、把中间计数器 +1、清空旧待读位，最后 `| cc` 填入新待读位。第 359–362 行是写者独占区：推进 `ct_`、写数据、置 `f_ct_ = ~cur_ct` 提交。

pop 全文在 [prod_cons.h:L405-L432](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/prod_cons.h#L405-L432)。第 408–410 行用 `f_ct_` 判提交；第 416–419 行处理「已全部读完」并释放槽位；第 421 行 `inc_rc(cur_rc) & ~connected_id()` 同时「自增中间计数器 + 清自己位」。

#### 4.3.4 代码实践

**实践目标**：对照多写版本的 push，说清 rc_ 的「高位 epoch」与「低位读计数」如何配合，并定位 pop 里「最后一个读者」的判定。这也是本讲规格指定的主实践任务。

**操作步骤**：

1. 打开 [prod_cons.h:L299-L305](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/prod_cons.h#L299-L305)，列出 `rc_mask`、`ep_mask`、`ic_mask` 三个掩码各自覆盖的位段。
2. 在 [push:L329-L364](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/prod_cons.h#L329-L364) 里追踪一次成功占槽：`epoch` 从哪一行来？第 352 行写入 `rc_` 的新值，其高 8 位、中间 24 位、低 32 位分别变成了什么？
3. 在 [pop:L405-L432](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/prod_cons.h#L405-L432) 里找到「最后一个读者」的两处判定（第 416 行与第 422–424 行），说明它们各自的条件。

**需要观察的现象（应能用自己的话回答）**：

- **高位 epoch 与低位读计数如何配合**：push 第 341 行 `(cur_rc & ~ep_mask) == epoch` 取出槽里的高 8 位 epoch，只有当它等于当前 epoch、**且**低位 `rem_cc` 与在线 `cc` 有重叠时，才认定「旧消息还有在线读者没读」而返回满。epoch 一旦被 `force_push` 推进，旧槽的高 8 位不再等于当前 epoch，其低位待读位立即「作废」，写者可覆盖。换句话说，**低位读计数只在与当前 epoch 匹配时才「生效」**。
- **「最后一个读者」如何判定**：pop 第 421 行算出清位后的 `nxt_rc`，第 422–423 行 `last_one = ((nxt_rc & rc_mask) == 0)`——清掉自己这位后，若低 32 位待读位归零，我就是最后一人；最后一人额外把 `f_ct_` 重置为释放标记（第 424 行），让写者能复用该槽。

**预期结果**：你能不看源码复述「epoch 决定读计数是否生效、低位归零即最后读者、最后读者负责释放槽位」这三件事。

**待本地验证**：本实践为源码阅读型，结论由静态分析得出；若要观测运行时行为，可在 push/pop 的 CAS 成功分支临时加日志打印 `rc_` 的高低位变化（属于修改源码，仅建议在本地实验分支进行）。

#### 4.3.5 小练习与答案

**练习 1**：多写版本的 `epoch_` 为什么必须是 `std::atomic`，而单写版本不必？第二个 CAS（第 353 行 `epoch_.compare_exchange_weak(epoch, epoch, ...)`）起什么作用？

> **答案**：多写版本有多个写者，任一个 `force_push` 都会改 `epoch_`，故须原子。第二个 CAS 把快照值 `epoch` 写回自身——成功说明期间 epoch 未变（兼带 `acq_rel` 屏障），失败说明被并发 `force_push` 改过，于是 `epoch` 被刷新为新值，外层 loop 用新 epoch 重试。它本质是「检测并发 force_push 并刷新快照」。

**练习 2**：`inc_rc` 和 `inc_mask` 的差别是什么？为什么 pop 用 `inc_rc` 而 push 用 `inc_mask`？

> **答案**：`inc_mask(rc) = inc_rc(rc) & ~rc_mask`，即 `inc_mask` 在自增中间计数器后**还清空了低位待读位**。push 是写者占新槽，要清掉旧待读位再填入新 `cc`，故用 `inc_mask`；pop 是读者清自己一位，要**保留其他人的待读位**，故只用 `inc_rc`（不清低位，再用 `& ~connected_id` 仅清自己那一位）。

**练习 3**：pop 第 418 行和第 424 行都执行 `el->f_ct_.store(cur + N - 1, ...)`，`N` 是什么？这个 store 的作用？

> **答案**：`N = elem_max = 256`（槽位总数，见 [elem_array.h:L30](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/circ/elem_array.h#L30)）。它是「释放标记」——当最后一位读者读完，把提交标志重置为 `cur + N - 1`，表示该槽已可被写者回收复用；写者 push 第 345–348 行的 `f_ct_` 检查会据此判定槽是否空闲。

---

### 4.4 force_push：踢掉失效读者，强行推进

#### 4.4.1 概念说明

广播队列最怕「慢读者」或「崩溃读者」卡住整条队列：只要有一个在线接收者没读某个槽，生产者就不能复用它。当队列写满（push 持续返回 false）时，[u3-l4](u3-l4-wait-model.md) 讲过会先自旋等待；若等到超时（`send` 的 `tm` 到点），ipc 层会改调 `force_push` **强行发送**。

`force_push` 的策略是「**狠**」：既然有接收者跟不上，那就认定它们「失效」，把它们的连接位图清掉（断连），并作废旧 epoch，然后强行覆盖槽位写入新消息。这正是广播变体 force_push 与 [u4-l3 单播 force_push](u4-l3-prod-cons-unicast.md) 的根本区别——单播 force_push 只是「断掉唯一的读者、返回 false 表示没人读」，而广播 force_push **会真正写入新消息**，只是顺手把拖后腿的读者踢掉。

#### 4.4.2 核心流程

以单写版本为例（多写版本逻辑一致，只是 epoch 改用 `fetch_add`）：

```text
force_push:
  epoch_ += ep_incr              # ① 先作废所有旧 epoch 的未读槽
  loop:
    cc = connections()
    X = elems[index_of(wt_)]
    rem_cc = X.rc_ & ep_mask     # 这个槽的旧待读位
    if cc & rem_cc:              # ② 有在线 receiver 还没读这槽的旧消息
        cc = disconnect_receiver(rem_cc)   # ③ 把这些读者踢出连接位图
        if cc == 0: return false
    CAS(X.rc_, cur_rc, epoch_ | cc)        # ④ 用新 epoch + 剩余在线者 写入
    成功 break
  写数据; wt_++
  return true                    # 真的写进去了（不像单播 force_push 返回 false）
```

三步缺一不可：① epoch 前进让后续 push 的「满」判定不再被旧待读位阻挡；②③ `disconnect_receiver` 把慢读者从 `cc_` 里物理移除（之后它们 `pop` 会发现 `connected()` 为假而退出）；④ 用新 epoch 写入，保证新消息只等剩余在线读者。

#### 4.4.3 源码精读

单写版本 force_push 在 [prod_cons.h:L243-L270](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/prod_cons.h#L243-L270)。第 247 行 `epoch_ += ep_incr` 就是「作废旧纪元」。第 255–258 行是踢读者：

```cpp
if (cc & rem_cc) {
    cc = wrapper->elems()->disconnect_receiver(rem_cc); // disconnect all invalid readers
    if (cc == 0) return false; // no reader
}
```

`disconnect_receiver` 定义在 [elem_array.h:L115-L117](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/circ/elem_array.h#L115-L117)，它委托给广播特化的 `conn_head::disconnect`，后者用 `fetch_and(~cc_id)` 原子地清掉这些位，定义在 [elem_def.h:L73-L75](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/circ/elem_def.h#L73-L75)。注意 `disconnect_receiver` 返回的是**清位后的新连接位图**，所以第 257 行用它更新本地 `cc`，确保第 262 行 CAS 写入的 `cc` 不含被踢者。

多写版本 force_push 在 [prod_cons.h:L366-L403](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/prod_cons.h#L366-L403)，唯一区别在第 371 行 `epoch = epoch_.fetch_add(ep_incr, release) + ep_incr`——用原子 `fetch_add` 推进 epoch，并在 CAS 成功后再次检查 epoch 是否仍为自己推进的值（第 387 行），若被别的写者又改了则递归调用 `push` 或重新 fetch_add（第 390、393 行），保证多写者并发 force_push 的一致性。

#### 4.4.4 代码实践

**实践目标**：观察 `force_push` 如何在「慢读者」存在时强行推进，并理解它与普通 push 返回值的区别。

**操作步骤**（源码阅读 + 可选运行）：

1. 对比 [单写 push:L223](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/prod_cons.h#L223)（`cc == 0` 返回 false）与 [单写 force_push:L247](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/prod_cons.h#L247)（先 `epoch_ += ep_incr`）。
2. 追踪 force_push 末尾 [L267-L269](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/prod_cons.h#L267-L269)：它**写了数据、推进了 `wt_`、返回 true**——与 [u4-l3 单播 force_push](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/prod_cons.h#L55-L59)（只断连、返回 false）截然不同。
3. （可选运行）用 `ipc::channel` 起一个 sender 和一个 receiver，让 receiver 故意 `sleep` 不读，sender 短超时高频 `send`，观察 sender 在超时后是否仍返回 true（走 force_push）。

**需要观察的现象**：被踢掉的接收者，其 `connected_id()` 对应位被 `fetch_and(~cc_id)` 清除；此后该 receiver 再 `pop` 会因 `connected()` 为假而在 ipc 层触发重连或收到空消息退出。

**预期结果**：广播 `force_push` 返回 true 且消息确实入队；被踢读者失去该通道。这与单播 `force_push`「不写消息、返回 false」形成对照。

**待本地验证**：步骤 3 的超时阈值与吞吐需在本机实测；步骤 1–2 为纯源码阅读，结论确定。

#### 4.4.5 小练习与答案

**练习 1**：广播 `force_push` 与 u4-l3 单播 `force_push` 在「是否写入新消息」和「返回值」上有什么不同？为什么？

> **答案**：单播 `force_push`（[L55-L59](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/prod_cons.h#L55-L59)）断掉所有/唯一读者后**返回 false、不写消息**，因为单播没有读者就没意义；广播 `force_push` 会**踢掉部分失效读者、写入新消息并返回 true**，因为广播只要还有至少一个在线读者（`cc != 0`），消息就该送达。

**练习 2**：force_push 第 247 行 `epoch_ += ep_incr` 若放到 CAS 成功**之后**执行，会有什么问题？

> **答案**：那样旧 epoch 下的待读位在 CAS 时仍「生效」，第 255 行的 `cc & rem_cc` 会把更多本不该踢的读者也判定为「未读」而误断连；且新写入的槽仍带旧 epoch，后续 push 的满判定会错乱。epoch 必须在抢占前先前进，才能让旧待读位整体失效。

**练习 3**：为什么 `disconnect_receiver(rem_cc)` 传的是 `rem_cc`（待读位）而不是 `cc`（全部在线者）？

> **答案**：`force_push` 只想踢「拖后腿的读者」——即那些占着槽位却没读的（`rem_cc` 里的位）。传 `rem_cc` 精准清掉这些位、保留已读完的在线读者；若传 `cc` 会把所有在线者都踢掉，相当于把通道清空，过于激进。

---

## 5. 综合实践：画出一次「广播 + 慢读者 + force_push」的完整 rc_ 演化

把本讲四个模块串起来，做一次端到端的手动推演。场景：`route`（single-multi-broadcast），2 个接收者 A（bit0）、B（bit1），`cc = 0b11 = 3`，`epoch_` 初始为 0，队列只有一个槽（便于手算）。

**任务**：按下面时序，写出每一步后该槽 `X.rc_` 的完整 64 位值（用 `0x...` 表示），并标注「谁会被 force_push 踢掉」。

1. 生产者 push 消息 M1。
2. 接收者 A `pop` M1（B 还没读）。
3. 生产者想 push M2，但 B 没读 → push 返回什么？
4. 生产者 `force_push` M2：epoch 变成多少？B 是否被踢？`rc_` 变成什么？
5. 此时 B 调 `pop`，会发生什么？

**参考答案**：

1. push M1：`rc_ = epoch_(0) | cc(3) = 0x0000000000000003`。
2. A pop 清 bit0：`rc_ = 0x0000000000000003 & ~0x1 = 0x0000000000000002`；`0x2 & ep_mask = 2 ≠ 0` → A 不是最后读者。
3. push M2：`rem_cc = 2`，`cc & rem_cc = 3 & 2 = 2 ≠ 0`，且槽 epoch(0) == 当前 epoch_(0) → **满，返回 false**。
4. force_push M2：先 `epoch_ += ep_incr` → `epoch_ = 0x100000000`；`rem_cc = 2`，`cc & rem_cc = 2 ≠ 0` → `disconnect_receiver(2)` 踢掉 B（bit1），`cc` 变为 `1`（只剩 A）；CAS 写入 `rc_ = epoch_(0x100000000) | cc(1) = 0x0000000100000001`；写 M2，返回 true。**B 被踢。**
5. B 调 `pop`：ipc 层 [ipc.cpp:L646-L647](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L645-L653) 会发现 `que->connected()` 为假（B 的位已被清），触发 `reconnect` 或返回空 `buff_t`，B 不再收到 M2。

**待本地验证**：以上为静态推演；真实运行中 `epoch_` 的高 32 位会随每次 force_push 递增，且 `disconnect_receiver` 后被踢进程的具体退出路径（重连 vs 空消息）取决于 ipc 层状态，建议在本地用日志验证第 5 步。

> 对比 [u3-l3 大消息外存](u3-l3-large-message-storage.md)：那里也用「连接位图 + 每个接收者清自己的位 + 最后一人回收」做引用计数，但作用在 **chunk 级**（`recycle_storage`/`sub_rc`，决定大消息外存何时释放）；本讲的 `rc_` 作用在**队列槽位级**（决定槽何时可被生产者复用）。两者共用 [u2-l4](u2-l4-route-vs-channel.md) 的连接位图思想，是同一把锤子的两个钉子。

## 6. 本讲小结

- 广播要求「一条消息被所有在线接收者读完才能复用槽位」，为此每个槽位用 64 位原子量 `rc_` 把**待读接收者位图**（低 32 位）和 **epoch 纪元**（高位）编码在一起。
- **single-multi-broadcast（route）**：单写者独占 `wt_`，`rc_` 高 32 位是 epoch、低 32 位是待读位；push 用 epoch 判「是否真满」，pop 用 CAS 清自己位并判定「最后读者」。
- **multi-multi-broadcast（channel）**：多写者需 `ct_`/`f_ct_` 提交协议（预约→写→提交→发布），`rc_` 增加 24 位「中间计数器」防 ABA，`epoch_` 升级为原子并用「双 CAS」感知并发 force_push。
- **epoch 的作用**：`force_push` 先推进 epoch，让旧消息残留的待读位「整体失效」，从而允许覆盖——这是批量作废旧数据的无锁手段。
- **force_push 踢失效读者**：用 `disconnect_receiver(rem_cc)` 精准清掉拖后腿读者的连接位，既强行写入新消息（返回 true），又避免误伤已读完的在线者；这与单播 force_push「只断连不写、返回 false」形成对照。
- 两个广播变体都被库实际编译（`ipc.cpp` L849–L850），是 `route`/`channel` 的真身；而它们调用的 `connections()`、`connected_id()`、`disconnect_receiver()` 分别来自 `elem_def.h`、`queue.h`、`elem_array.h`。

## 7. 下一步学习建议

本讲把 `prod_cons.h` 的四类算法讲完了（u4-l3 单播 + 本讲广播）。接下来建议：

- **深入内存序**：本讲里 CAS 用了 `relaxed`/`acquire`/`release`/`acq_rel` 等多种内存序，为什么 `rc_` 的某些 CAS 用 `relaxed` 而 `f_ct_` 用 `release`/`acquire`？这正是 [u8-l1 内存序、伪共享与缓存行](u8-l1-memory-ordering.md) 的主题。
- **回看等待模型**：本讲的 push 返回 false 后会发生什么？答案在 [u3-l4 等待模型](u3-l4-wait-model.md) 的「先自旋后阻塞、超时改 force_push」。
- **对照大消息引用计数**：若想看清「连接位图清位 + 最后一人回收」在另一处（chunk 级）的应用，回看 [u3-l3 大消息外存](u3-l3-large-message-storage.md)。
- **动手扩展**：尝试在本地给 `prod_cons.h` 广播 push/pop 的 CAS 成功分支加临时日志，打印 `rc_` 高低位，验证第 5 节的推演（仅在实验分支修改，勿提交）。
