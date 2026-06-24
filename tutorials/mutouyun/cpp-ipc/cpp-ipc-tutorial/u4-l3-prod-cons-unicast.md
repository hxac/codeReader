# prod_cons 单播变体：最简环形队列到多生产者提交

## 1. 本讲目标

本讲带你钻进 libipc 无锁队列的「算法核心」——`src/libipc/prod_cons.h`，专门看**单播（unicast）**这一条线：一条消息只投递给**一个**消费者。

学完后你应当能够：

- 说清最简环形队列（single-single）如何用 `rd_`/`wt_` 两个游标判满、判空，以及为什么容量是 255 而非 256。
- 解释当消费者变成多个（single-multi）时，`pop` 为什么必须改成「先拷贝到栈、再 CAS 抢占」的形态。
- 解释当生产者也变成多个（multi-multi）时，为什么需要新增 `ct_` 提交索引与每槽的 `f_ct_` 提交标志，并能画出「预约 → 写入 → 提交 → 发布」的四步协议。
- 说清 `force_push` 在单播里的独特语义（让位 + 断连，而不是广播里的「踢掉旧消息继续发」）。
- 知道这三个单播变体里，**只有 single-single 被真正编译进库**，另外两个算法虽已实现却被 `// TBD` 注释挡在链接门外。

---

## 2. 前置知识

本讲假设你已经读过 u4-l1（队列抽象层）与 u4-l2（`elem_array` 与连接位图）。在进入算法前，先把几个反复用到的积木复习一下：

- **`circ::u2_t` 与 `circ::u1_t`**：分别是 32 位与 8 位无符号整数。`rd_`/`wt_`/`ct_` 这些游标都是 `u2_t`（32 位），它们**单调递增、永不回绕**；只有当用作数组下标时，才用 `index_of` 截断成 8 位。见 [elem_def.h:16-24](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/circ/elem_def.h#L16-L24)。

- **`index_of(c)`**：就是 `static_cast<u1_t>(c)`，等价于 \(c \bmod 256\)，且**零分支**。环形队列的「回绕」完全靠它完成。

- **`elem_max = 256`**：元素块的槽位数，来自 [elem_array.h:27-33](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/circ/elem_array.h#L27-L33)，源于 `uint_t<8>` 的最大值 +1。环形数组一共 256 个槽。

- **两层回调委托**（u4-l1 重点）：算法层 `prod_cons_impl` 只负责「选哪个槽」，选好后把槽的 `data_` 指针喂给回调 `f`，由回调里执行 `::new (p) T(...)` 完成对象构造。所以你在 `push` 里看到的 `std::forward<F>(f)(&(elems[cur].data_))`，「构造」二字就藏在 `f` 里。入口见 [queue.h:184-190](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/queue.h#L184-L190)。

- **`ipc::yield(k)`**：四级阶梯退避（空转 / PAUSE / yield / sleep），阈值 4/16/32，定义在 [rw_lock.h:62-74](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/rw_lock.h#L62-L74)。CAS 失败时调它，避免把总线锁死。

- **单播的连接计数**：单播（`trans::unicast`）走 `conn_head<P, false>`，它**不分位、只计数**，`connect()` 是 `fetch_add(1)+1`，`disconnect(~0)` 清零、`disconnect(其他值)` 减一。见 [elem_def.h:89-115](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/circ/elem_def.h#L89-L115)。这跟广播的「位图」完全不同——单播一条消息只给一个消费者，无需区分谁读了。

> 提示：单播不区分读者身份，所以**没有 32 接收者位图那一套**；多消费者靠「抢」（CAS），多生产者靠「提交协议」，这是本讲的两条主线。

---

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用到什么 |
| --- | --- | --- |
| [src/libipc/prod_cons.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/prod_cons.h) | **唯一核心**。三个单播特化 + 两个广播特化，全部在这一个头文件里。 | single-single / single-multi / multi-multi 三个单播特化的 `push`/`pop`/`force_push`。 |
| [src/libipc/circ/elem_def.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/circ/elem_def.h) | 游标类型、`index_of`、`conn_head`。 | `u1_t/u2_t`、`index_of`、单播计数版 `conn_head<P,false>`。 |
| [src/libipc/circ/elem_array.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/circ/elem_array.h) | 把「连接头 + 策略头 + 256 槽块」拼成共享内存对象，并把 `push/pop` 委托给策略头。 | `elem_max`、`disconnect_receiver`、委托调用。 |
| [src/libipc/queue.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/queue.h) | `queue_base`/`queue` 封装层。 | `push`/`force_push`/`pop` 如何把构造回调传下去。 |
| [src/libipc/ipc.cpp](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp) | 库的编译单元，末尾的「实例化门」。 | 哪些 `wr` 组合被真正编译进库。 |
| [include/libipc/rw_lock.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/rw_lock.h) | `yield` 退避实现。 | CAS 失败后的让步策略。 |

三个单播特化是一条**继承链**，理解这张图就理解了本讲的骨架：

```
prod_cons_impl<single, single, unicast>   ← 基类：rd_ / wt_，最简 push/pop，force_push(全断)
        ▲ 继承
prod_cons_impl<single, multi , unicast>   ← 覆盖 pop(CAS抢) + force_push(断1)；push 仍用基类的
        ▲ 继承
prod_cons_impl<multi , multi, unicast>    ← 新增 ct_ + f_ct_；覆盖 push(提交协议) + pop(带help) + force_push(断1)
```

注意「覆盖」（C++ 名称隐藏）而非「虚函数」——这些都是模板结构体，编译期分派，零运行时开销。下文逐层展开。

---

## 4. 核心概念与源码讲解

### 4.1 单播基类：single-single 最简环形队列

#### 4.1.1 概念说明

`prod_cons_impl<wr<relat::single, relat::single, trans::unicast>>` 是整条继承链的根，也是**最经典的无锁单生产者-单消费者环形队列（SPSC ring）**：一个写者、一个读者、一个环形缓冲区。

它只维护两个游标：

- `wt_`：写游标，指向**下一个待写**的槽。
- `rd_`：读游标，指向**下一个待读**的槽。

判满与判空是环形缓冲区的灵魂：

- **空**：`index_of(rd_) == index_of(wt_)`，读写游标重合。
- **满**：`index_of(wt_) == index_of(rd_ - 1)`，写游标追到了读游标「前一格」。

为什么「满」要差一格？因为若允许写满到 `wt_ == rd_`，那「满」和「空」的判据就完全一样、无法区分了。这是教科书级的「牺牲一个槽换判据清晰」手法。于是 256 槽的可用容量是：

\[
\text{capacity} = \text{elem\_max} - 1 = 256 - 1 = 255
\]

#### 4.1.2 核心流程

**push（写）**：

```
load wt_（relaxed）  →  cur_wt = index_of(wt_)
load rd_（acquire）   →  若 index_of(wt) == index_of(rd-1)：满，返回 false
否则：回调 f 把对象构造进 elems[cur_wt].data_
wt_.fetch_add(1, release)   ← 发布：消费者据此 acquire 能看到数据
返回 true
```

**pop（读）**：

```
load rd_（relaxed）  →  cur_rd = index_of(rd_)
load wt_（acquire）  →  若 index_of(rd) == index_of(wt)：空，返回 false
否则：回调 f 从 elems[cur_rd].data_ 取出对象，out(true) 通知上层
rd_.fetch_add(1, release)   ← 推进读游标，给生产者腾位
返回 true
```

关键点：写者对 `wt_` 用 `release`，读者对 `wt_` 用 `acquire`——这一对内存序建立了「数据写入对读者可见」的 happens-before 关系；`rd_` 方向同理对称。这是 SPSC 队列**无需任何锁、也几乎无需 CAS** 的根本原因：单写者独占 `wt_`，单读者独占 `rd_`，彼此只读对方的游标。

#### 4.1.3 源码精读

基类的成员与最简 `push`（注意 `rd_ - 1` 在 32 位上先减、再 `index_of` 截断）：

[prod_cons.h:33-49](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/prod_cons.h#L33-L49) —— 两个 cache-line 对齐的游标 `rd_`/`wt_`（`alignas(cache_line_size)` 防伪共享），以及「判满 → 构造 → release 发布」的 `push`。

对应的 `pop` 完全对称：

[prod_cons.h:61-71](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/prod_cons.h#L61-L71) —— 「acquire 读 `wt_` 判空 → 取对象 → release 推进 `rd_`」。

注意 `cursor()` 在单播里恒返回 0：

[prod_cons.h:36-38](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/prod_cons.h#L36-L38) —— 单播的真正进度藏在共享的 `rd_`/`wt_` 里，这个 `cursor()` 在单播 `pop` 中并不被使用（参数 `cur` 被标记 `/*cur*/` 忽略）。

#### 4.1.4 代码实践（手动演算）

**实践目标**：亲手验证 255 的容量与满/空判据。

**操作步骤**：

1. 设想 `rd_ = 0, wt_ = 0`（全空）。
2. 连续「推」255 次：每次 `index_of(wt)` 都不等于 `index_of(rd-1)`，成功；推完后 `wt_ = 255`，`rd_ = 0`。
3. 第 256 次推：`index_of(wt=255) == 0`，而 `index_of(rd-1) = index_of(0-1) = index_of(0xFFFFFFFF) = 255`，`0 == 255` 不成立——咦？

**需要观察的现象**：第 3 步看似还能推？别急，再推一次到 `wt_ = 256`（`index_of = 0`），此时 `index_of(rd-1) = 255`，仍不等于 0……问题出在你没按代码顺序算。请严格按 [push](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/prod_cons.h#L40-L49) 的顺序：**先算 `cur_wt = index_of(wt_)`，再用此刻的 `rd_` 判满**。当 `wt_` 推到 255、即将写第 256 个时，`cur_wt = index_of(255) = 255`，`index_of(rd_-1) = index_of(-1) = 255`，`255 == 255` → 满，返回 false。

**预期结果**：容量确实是 255；第 256 次 `push` 被拒。此演算为「待本地验证」（可写一个 SPSC 测试断言 `push` 第 256 次返回 false）。

#### 4.1.5 小练习与答案

**Q1**：为什么 `rd_`/`wt_` 用 32 位却不回绕，而要靠 `index_of` 截断？
**A**：让游标单调递增，避免「回绕后判据歧义」。只要保证任意时刻 \(0 \le wt\_ - rd\_ \le 255\)（满判据保证了上界），低 8 位的相等/差一关系就唯一对应「空/满/有数据」，不会被一圈一圈绕回来的历史值干扰。

**Q2**：把 `wt_.fetch_add(1, release)` 改成 `relaxed` 会怎样？
**A**：数据写入与游标推进之间失去 happens-before，消费者用 `acquire` 读 `wt_` 时**可能看到新游标却读到旧数据**，出现脏读。release 是正确性的硬要求。

---

### 4.2 单生产者多消费者：single-multi 的 CAS 抢占式 pop

#### 4.2.1 概念说明

当消费者从 1 个变成多个（`relat::multi` 消费者），生产者仍是 1 个，问题出在 `pop`：多个消费者会**同时**读到同一个 `rd_`，若都以为「这个槽归我」就会重复消费。

解法是经典的 **CAS 抢占**：每个消费者「瞄一眼」`rd_`，拷贝数据到栈，再用 `compare_exchange_weak` 试图把 `rd_` 从「我看到的值」推进 1。CAS 只有一个赢家——赢家消费这槽，输家被告知值已变、重试下一槽。生产者侧只有一个，`push` 不变（直接继承基类）。

#### 4.2.2 核心流程

**single-multi 的 `pop`**：

```
loop:
    cur_rd = rd_.load(relaxed)
    若 index_of(cur_rd) == index_of(wt_.load(acquire))：空，返回 false
    memcpy(栈 buff, elems[index_of(cur_rd)].data_)   ← 先拷贝
    若 rd_.compare_exchange_weak(cur_rd, cur_rd+1, release) 成功：
        f(buff)        ← 赢家处理数据（用的是自己的栈副本）
        out(true)
        返回 true
    否则：yield(k)，重试        ← 输家：别的消费者抢走了，重来
```

**为什么要先 `memcpy` 到栈再 CAS？** 因为 `out` 回调可能很重（比如触发大消息的外存回收）。若先 CAS 成功、占着槽位再慢慢处理，会让生产者迟迟等不到「`rd_` 推进」而误判为满。先拷贝、再 CAS 推进，槽位立刻释放给生产者复用，吞吐更高。注意「先拷贝」是安全的：多个消费者可能都拷贝了同一槽，但只有 CAS 赢家真正消费，输家丢弃自己的栈副本即可。

#### 4.2.3 源码精读

single-multi 特化继承自 single-single，只覆盖 `force_push` 与 `pop`，`push` 与游标全部复用：

[prod_cons.h:74-103](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/prod_cons.h#L74-L103) —— 注意签名用了 `template <... template <std::size_t,std::size_t> class E, std::size_t DS, std::size_t AS>`，这是为了从元素模板 `E` 里萃取出 `DS`（DataSize），好在栈上开 `byte_t buff[DS]`。

核心的「先拷贝后 CAS」就在这段里：

[prod_cons.h:84-102](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/prod_cons.h#L84-L102) —— `std::memcpy(buff, ...)` 之后才 `rd_.compare_exchange_weak(...)`，CAS 失败则 `ipc::yield(k)` 退避重试。

#### 4.2.4 代码实践（并发推演）

**实践目标**：理解 CAS 如何防止重复消费。

**操作步骤**：

1. 设想 `rd_ = 10, wt_ = 12`（有 2 个待读槽 10、11），消费者 A 与 B 同时 `pop`。
2. A、B 都 `load` 到 `cur_rd = 10`，都 `memcpy` 了槽 10。
3. A 先 `CAS(rd_, 10→11)` 成功；B 的 `CAS(rd_, 10→11)` 失败（因为 `rd_` 已是 11，与 B 期望的 10 不符）。
4. B `yield` 后重试：`load` 到 `cur_rd = 11`，拷贝槽 11，`CAS(rd_, 11→12)` 成功。

**需要观察的现象**：槽 10 只被 A 消费、槽 11 只被 B 消费，无重复、无丢失。

**预期结果**：两个消费者合力把 2 条消息各消费一次。此为心智模型推演，标记「待本地验证」。

#### 4.2.5 小练习与答案

**Q1**：生产者侧为什么不需要改？
**A**：生产者仍只有一个，它独占 `wt_` 的推进，基类的简单 `push`（无 CAS）依然安全。多消费者只让 `pop` 端产生竞争，故只覆盖 `pop`。

**Q2**：如果去掉 `memcpy`，直接在 CAS 成功后用 `elems[cur_rd].data_` 调 `f`，会出什么问题？
**A**：功能上仍正确（CAS 已保证独占），但会**拖慢生产者**：处理数据期间 `rd_` 已推进、槽已可被复用，但你仍持有该槽指针在做事，生产者若复写它就会破坏你正在读的内容。先拷贝到栈就把「消费者处理时长」与「槽位生命周期」彻底解耦。

---

### 4.3 多生产者多消费者：multi-multi 的 ct_ 提交索引与 f_ct_ 标志

#### 4.3.1 概念说明

当生产者也变成多个（`relat::multi` 生产者 + `relat::multi` 消费者），最棘手的问题来了：**多个生产者并发写，消费者绝不能读到「写了一半」的槽**。

朴素想法是「多个生产者 CAS 抢 `wt_`」——但这有个经典漏洞：生产者 P1 抢到槽 10、正在慢慢写，生产者 P2 抢到槽 11、飞快写完并把 `wt_` 推到 12；此时消费者看到 `wt_=12`，去读槽 10，结果 P1 还没写完 → **脏读**。

libipc 的解法是把「写」拆成**两个游标 + 一个标志**：

- `ct_`（commit index）：**预约/提交**游标。生产者 CAS `ct_` 来「占座」，`ct_` 表示「已被预约写到哪」。
- `wt_`（write/publish index）：**发布**游标。表示「数据已就绪、消费者可读到哪」。消费者只看 `wt_`。
- `f_ct_`（commit flag，每槽一个）：标志「本槽数据是否写完」。其值为 `~cur_ct`（按位取反的预约序号）时表示「已就绪」，为 `0` 时表示「空」。

于是产生 **「预约 → 写入 → 提交（置标志） → 发布（推进 `wt_`）」** 四步协议：消费者永远只追 `wt_`，而 `wt_` 只在一个槽「真正写完」后才被推进，彻底杜绝脏读。

#### 4.3.2 核心流程

**multi-multi 的 `push`**：

```
// ① 预约：CAS 抢 ct_
loop:
    cur_ct = ct_.load(relaxed)
    若 index_of(cur_ct+1) == index_of(rd_.load(acquire))：满，返回 false
    若 ct_.CAS(cur_ct → cur_ct+1, acq_rel) 成功：break
    yield(k)
// ② 写入：把对象构造进 elems[index_of(cur_ct)]
f(&(el->data_))
// ③ 提交：置标志，~cur_ct 表示「本槽就绪」
el->f_ct_.store(~cur_ct, release)
// ④ 发布：若此刻轮到我（cur_ct == wt_），就推进 wt_，并级联发布后续已就绪的槽
while:
    若 cur_ct != wt_.load(relaxed)：不归我管，别人会发，return true
    若 ~f_ct_ != cur_ct（标志已变/被别人接管）：return true
    若 f_ct_.CAS(~cur_ct → 0) 失败：return true
    wt_.store(cur_ct+1, release)     ← 真正发布
    cur_ct++；移到下一槽，继续级联
```

第 ④ 步是点睛之笔——**级联发布（combining）**：当某个生产者写完、轮到自己发布时，它不仅发布自己的槽，还顺手把后面「连续已就绪」的槽一并推进 `wt_`。这样即使 P2（写第 11 槽）比 P1（写第 10 槽）先完成，`wt_` 也绝不会越过尚未完成的 10 槽——必须等 10 就绪、由「恰好轮到的人」把 10、11 一起发布。

**消费者也会「帮忙发布」**：在 `pop` 里，当读者追上 `wt_`（`id_rd == id_wt`）却发现该槽的 `f_ct_ == ~cur_wt`（已就绪但还没被生产者发布）时，读者会自己 CAS 掉标志、推进 `wt_`，然后继续读。见 [prod_cons.h:162-192](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/prod_cons.h#L162-L192) 的 `id_rd == id_wt` 分支。这让「发布」不被某个慢生产者卡住。

#### 4.3.3 源码精读

multi-multi 新增的成员——提交索引 `ct_` 与带标志的元素类型：

[prod_cons.h:109-117](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/prod_cons.h#L109-L117) —— `flag_t = uint64_t`，`elem_t` 在 `data_` 之外多了 `std::atomic<flag_t> f_ct_`，外加一个 `alignas(cache_line_size)` 的 `ct_`。

完整的「四步协议」`push`：

[prod_cons.h:119-154](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/prod_cons.h#L119-L154) —— ① CAS `ct_` 预约（注意「满」用 `ct_` 而非 `wt_` 判，因为生产者按预约量占座）；② `f` 写入；③ `store(~cur_ct, release)` 提交；④ while 循环级联推进 `wt_`。

标志的编码值得驻足：用 `~cur_ct`（而非简单的 1）表示就绪，是因为环形槽会被反复复用，`~cur_ct` 携带了**预约序号**这一「代际」信息——第 ④ 步与消费者的 help 分支都靠 `(~cac_ct) == cur_ct` 来确认「这个就绪标志确实属于当前这一轮」，避免把上一轮残留的标志误判为本轮就绪。

multi-multi 的 `pop` 既有 CAS 抢占（继承自 single-multi 的思路），又加了「追上 `wt_` 时帮一手发布」的逻辑：

[prod_cons.h:162-192](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/prod_cons.h#L162-L192) —— `id_rd == id_wt` 分支负责 help-publish，`else` 分支是标准的多消费者 CAS 抢占读。

#### 4.3.4 代码实践（本讲主任务：对比 single-single 与 multi-multi 的 push）

**实践目标**：亲手对比两个 `push`，说清 `ct_` 与 `f_ct_` 存在的必要性。

**操作步骤**：

1. 打开 [single-single push](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/prod_cons.h#L40-L49)：数一下它动用了哪几个共享变量（答案：只有 `wt_` 与 `rd_`）。它是单写者，写完直接 `wt_.fetch_add` 发布，**消费者读到 `wt_` 就一定有完整数据**，因为没人会和它抢着写。
2. 打开 [multi-multi push](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/prod_cons.h#L119-L154)：数一下它动用了几个（答案：`ct_`、`rd_`、`wt_`、`el->f_ct_` 四类）。
3. 回答：为什么 multi-multi 不能像 single-single 那样「写完直接推进 `wt_`」？

**需要观察的现象 / 预期结论**：

- 若 multi-multi 直接 CAS `wt_` 占座、写完不置标志：快的生产者会把 `wt_` 推到慢生产者尚未写完的槽之后，消费者追 `wt_` 时读到半成品 → 脏读。
- 引入 `ct_`：把「占座」和「发布」分离，`ct_` 只表示「预约到哪」，消费者不看它。
- 引入 `f_ct_`：让生产者写完后**显式置标志**声明「我这个槽真的写完了」；`wt_` 只在「前序槽都已就绪」时才被推进（级联发布）。
- 结论：`ct_` 解决「多生产者如何不撞车地占座」，`f_ct_` 解决「消费者如何确认某槽真写完、可安全读」。两者共同把 SPSC 的两游标模型安全升级到 MPMC。

**预期结果**：你能用自己的话写出「去掉 `ct_` 会撞座、去掉 `f_ct_` 会脏读」。这是源码阅读型实践，无需运行，标记「待本地验证」的是「实际启 multi-multi-unicast 做压测」（见 4.3.5 与综合实践）。

#### 4.3.5 小练习与答案

**Q1**：multi-multi 的「满」判据为什么用 `index_of(ct_+1) == index_of(rd_)`，而 single-single 用 `wt_`？
**A**：multi-multi 里生产者通过 CAS `ct_` 占座，占掉的槽即使数据还没写完，也不能再被别的生产者写，否则撞车。所以「是否还有空位」必须看「已预约到哪」（`ct_`）相对「已读到哪」（`rd_`）。若改看 `wt_`（已发布），就会把「已预约但未写完」的槽当成空闲再分配，覆盖正在写的数据。

**Q2**：第 ④ 步级联发布里，三个 `return true` 分别在什么情况下触发？
**A**：(a) `cur_ct != wt_`：当前不归我发布（前面还有别人没写完），我的活干完即可，等轮到的人发布；(b) `~cac_ct != cur_ct`：标志已被别人改过（说明有人接管了发布）；(c) `f_ct_.CAS` 失败：并发竞争下别人抢先清了标志接管发布。三者都表示「发布责任已转移」，本线程安全返回。

---

### 4.4 force_push 在单播中的语义：让位与断连

#### 4.4.1 概念说明

`force_push` 是「普通 `push` 失败（队列满 / 超时）时的兜底」。在**广播**里，它的含义是「踢掉没及时消费的旧消息、强行塞入新消息」（见 u4-l4）。但在**单播**里，语义截然不同——源码注释讲得很直白：

> In single-single-unicast, 'force_push' means 'no reader' or 'the only one reader is dead'. So we could just disconnect all connections of receiver, and return false.

单播里一条消息只给一个消费者，队列满**只可能**是因为那个消费者不消费了（多半已崩溃）。所以单播的 `force_push` **不强行塞消息**，而是「断掉这个失效消费者、放弃本条消息、返回 false」。这是与广播最大的区别。

#### 4.4.2 核心流程

三个单播变体的 `force_push` 行为：

| 变体 | 调用 | 含义 | 返回 |
| --- | --- | --- | --- |
| single-single | `disconnect_receiver(~0)` | 唯一读者已死，清空全部连接 | `false` |
| single-multi | `disconnect_receiver(1)` | 多读者场景，减掉 1 个读者 | `false` |
| multi-multi | `disconnect_receiver(1)` | 同上，减掉 1 个读者 | `false` |

注意 `disconnect_receiver` 最终落到单播计数版 `conn_head<P,false>::disconnect`：参数 `~0`（全 1）触发「清零」分支，其他值触发「`fetch_sub(1)` 减一」分支。见 [elem_def.h:96-105](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/circ/elem_def.h#L96-L105)。

#### 4.4.3 源码精读

single-single 的 `force_push`（带原注释）：

[prod_cons.h:51-59](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/prod_cons.h#L51-L59) —— 断开所有接收者、返回 false，**不写入任何数据**。

single-multi 与 multi-multi 的 `force_push`（减 1）：

[prod_cons.h:78-82](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/prod_cons.h#L78-L82) 与 [prod_cons.h:156-160](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/prod_cons.h#L156-L160) —— 都是 `disconnect_receiver(1)` 后返回 false。

`disconnect_receiver` 的入口在 `elem_array`，它按 `relat_trait` 选 checker、再委托给 `conn_head`：

[elem_array.h:115-117](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/circ/elem_array.h#L115-L117) —— `r_ckr_.disconnect(*this, cc_id)`。

#### 4.4.4 代码实践（对照注释理解语义）

**实践目标**：确认单播 `force_push` 不写数据、只断连。

**操作步骤**：

1. 读 [single-single force_push](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/prod_cons.h#L55-L59) 的注释与实现：注意它**完全忽略**传入的构造回调 `F&&`（参数连名字都没给，是 `F&&`），也不碰 `elems`。
2. 对比广播的 `force_push`（u4-l4 会讲）：广播版会 `epoch_ += ep_incr`、踢旧读者、然后**真的写入** `f(&(el->data_))` 并返回 `true`。

**需要观察的现象 / 预期结果**：单播 `force_push` 签名收了构造回调却不调用它——这就是「让位 + 断连、不塞消息」的铁证。请用一句话写出单播与广播 `force_push` 的本质差异。此为源码阅读型实践。

#### 4.4.5 小练习与答案

**Q1**：为什么 single-single 用 `~0` 而 single-multi/multi-multi 用 `1`？
**A**：single-single 只有一个读者，`~0` 是计数版 `conn_head` 的「清空全部」哨兵，正好清掉这唯一的读者；single-multi/multi-multi 有多个读者，`1`（任意非 `~0` 值）走 `fetch_sub(1)` 分支，只摘除一个读者，保留其余读者继续消费。

**Q2**：既然 `force_push` 返回 false、又没写入，那它「兜底」了什么？
**A**：它兜的是「**资源回收**」的底：当单播队列因读者失活而长期满载，`force_push` 把失效读者断开、让其座位可被复用，避免死锁式占位。消息本身被放弃，但通信通道得以自愈。

---

## 5. 综合实践

**任务：把三个单播变体串起来，并理解「实例化门」为何把它们挡在门外。**

1. **画继承与覆盖图**：在一张图上标出 single-single 定义了 `rd_`/`wt_`/`elem_t`/`push`/`pop`/`force_push`；single-multi 覆盖了 `pop`/`force_push`；multi-multi 新增 `ct_`/`f_ct_`、覆盖 `elem_t`/`push`/`pop`/`force_push`。用箭头标明哪些成员是「继承复用」、哪些是「名称隐藏覆盖」。

2. **追踪一条消息在 multi-multi 里的完整生命**：从一个生产者 `push` 开始，按「① CAS `ct_` 预约 → ② `f` 写入 → ③ `store(~cur_ct)` 提交 → ④ 级联推进 `wt_`」画出时序；再到一个消费者 `pop`，画出「load `rd_`/`wt_` → 若追上 `wt_` 则 help-publish → 否则 memcpy + CAS `rd_` 抢占」。标注每一步用的内存序，并解释为什么 `wt_.store` 用 `release`、消费者 `wt_.load` 用 `acquire`。

3. **解开「TBD」之谜**（承接 u8-l5）：打开 [ipc.cpp:846-850](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L846-L850)，你会看到只有 `single-single-unicast` 被实例化，而 `single-multi-unicast` 与 `multi-multi-unicast` 被注释为 `// TBD`。请回答：
   - 这意味着多消费者、多生产者的**单播**算法虽然已在 `prod_cons.h` 里写好，却**没有被编译进 `libipc.a`**。
   - 公开的 `route`/`channel` 都是广播，所以日常使用根本碰不到这两个单播变体。
   - **思考**：若要启用 multi-multi-unicast，需要做哪些事？（提示：取消注释实例化门、为它定义公共别名、确认 `disconnect_receiver`/`connections` 在单播计数语义下与提交协议配合正确。）评估工作量并指出潜在风险点（如 `force_push` 的「断 1」在多读者下是否会误杀活跃读者）。这是「待本地验证」的扩展实验。

**预期产出**：一张时序图 + 一段对「TBD」成因与启用成本的评估。

---

## 6. 本讲小结

- 三个单播特化是一条**编译期继承链**：single-single 是基类（`rd_`/`wt_` 两游标 SPSC 环形队列，容量 255），single-multi 覆盖 `pop` 为 CAS 抢占式，multi-multi 新增 `ct_`/`f_ct_` 并覆盖 `push`/`pop`。
- single-single 只需两个游标：单写者独占 `wt_`、单读者独占 `rd_`，靠 `release`/`acquire` 配对保证可见性，**无需 CAS**。
- single-multi 的 `pop` 用「先 `memcpy` 到栈、再 CAS `rd_`」抢占槽位，赢家消费、输家重试，既防重复消费又尽快释放槽位。
- multi-multi 引入 **「预约→写入→提交→发布」四步协议**：`ct_` 管「占座」、`f_ct_` 管「写完与否」、`wt_` 管「消费者可读到哪」；级联发布与消费者 help-publish 让 `wt_` 只在前序槽就绪时才前进，杜绝脏读。
- `force_push` 在单播里语义独特：**不写消息**，只断连失效读者并返回 false（single-single 断全部、single-multi/multi-multi 断 1 个），与广播的「踢旧塞新」截然不同。
- **实例化门**只编译了 single-single-unicast；single-multi-unicast 与 multi-multi-unicast 的算法已实现却被 `// TBD` 注释挡在库外，公开 API 用不到（承接 u8-l5）。

---

## 7. 下一步学习建议

- **下一讲 u4-l4**：读 `prod_cons_impl` 的两个**广播**变体（single-multi-broadcast、multi-multi-broadcast）。它们复用了本讲的 CAS、cache-line 对齐、`yield` 退避等手法，但把「一条消息给一个消费者」升级为「一条消息给所有连接的接收者」，引入 `rc_` 读计数位域与 `epoch` 代际协议——建议带着「单播没有位图、广播才有」的对比去读。
- **u8-l1（内存序）**：本讲多次出现的 `release`/`acquire`/`relaxed`/`acq_rel` 选择，在那里会有系统性的解释，尤其是「为什么 `f_ct_` 用 release/acquire、而某些 `rc_` 读用 relaxed」。
- **u8-l3（intrusive_stack / id_pool）**：本讲看到的「CAS + 退避」是无锁结构的通用范式，那里会讲另一组同源结构。
- **u8-l5（测试与扩展）**：若你对综合实践里的「启用 TBD 单播变体」感兴趣，那一讲会系统讨论模板实例化门与新增策略/平台的工作量。
