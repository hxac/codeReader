# 丢包检测与 NAK：LossList

## 1. 本讲目标

本讲聚焦 UDT 可靠性机制里「**丢包是怎么被发现、怎么上报、怎么触发重传的**」这一整条链路中最关键的数据结构与编码。

学完后你应该能够：

- 说清 `LossList` 为什么用「区间」而不是「单点」来存丢失的序列号，并能读懂它的 `insert / remove / remove_all / pop_after / peek_after` 五个方法。
- 说清 NAK 包的 `loss_info` 字段如何用 `0x8000_0000` 这个最高位区分「单个丢失」与「一段丢失区间」，并能在收发两端之间手工往返编码。
- 读懂发送方 `process_ctrl` 的 `Nak` 分支：收到 NAK 后如何写入 `snd_loss_list`、如何做 `broken` 合法性判定、如何立即触发重传调度。
- 理解收发两侧各维护一份 `LossList`（接收侧的 `rcv_loss_list` 与发送侧的 `snd_loss_list`）的分工。

本讲依赖你已经学过 [u4-l4 序列号与循环算术](u4-l4-seq-number.md)（循环加减、`Sub` 返回 `i32`）与 [u6-l2 接收数据与 ACK 生成](u6-l2-recv-and-ack.md)（`process_data` 如何检测缺口、`send_ack` 如何用 `peek_after` 钳制水位）。

## 2. 前置知识

### 2.1 为什么需要一张「丢失表」

UDT 跑在不可靠的 UDP 之上，丢包是常态。可靠传输要求接收方「知道缺了哪些包」，并把这个信息反馈给发送方去重传。最朴素的办法是给每一个丢失的序列号建一条记录，但现实里丢包往往是**成串**的（网络抖动一次丢一整段），用「区间 \([a,b]\)」存比用单点省内存，也更接近 UDT 协议线上 NAK 的表达方式。

所以 tokio-udt 用一个通用结构 `LossList`，**同时**服务于两个角色：

| 角色 | 字段名（在 `SocketState` 里） | 谁写它 | 干什么 |
|---|---|---|---|
| 接收侧 | `rcv_loss_list` | 接收方在 `process_data` 发现缺口时 `insert`，补到包时 `remove` | 记录「我还缺哪些包」，用来生成 NAK、并钳制 ACK 水位 |
| 发送侧 | `snd_loss_list` | 发送方收到 NAK 时 `insert`，收到 ACK 时 `remove_all` | 记录「对端说缺哪些包」，用来在 `next_data_packets` 里优先重传 |

这两份表类型完全相同（都是 `LossList`），只是填写者与消费者不同。理解这一点很重要——**同一个结构，在收发两端扮演镜像角色**。

### 2.2 循环序列号的两套比较语义（复习）

`SeqNumber` 是 31 位循环空间上的数，值域 \([0,\, 2^{31}-1]\)，加减自动对 \(2^{31}\) 取模。它在 [u4-l4](u4-l4-seq-number.md) 里讲过两套比较语义，本讲会反复用到，先复习：

- **派生 `Ord`（原始 `u32` 顺序）**：`BTreeMap` 的键序、`range(..)` 范围查询都用它。它把序列号当成普通整数排序，**不考虑回绕**。
- **循环 `Sub`（返回 `i32`）**：给出环上带符号最短距离，正负表示方向，用于「缺口有多大」「A 是否在 B 之前」这类判断。

`LossList` 的精妙之处在于：**它把这两套语义混着用**——结构（`BTreeMap`）靠原始顺序组织区间，而「相邻/缺口」判断靠循环减法。下面的源码精读会逐一指出哪里用了哪一套。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [src/loss_list.rs](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/loss_list.rs) | `LossList` 结构与全部区间操作；自带单元测试 |
| [src/control_packet.rs](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/control_packet.rs) | `NakInfo`（`Vec<u32>` 的 `loss_info`）的序列化/反序列化、`new_nak` 构造 |
| [src/socket.rs](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs) | `process_data`（接收侧发现缺口并发 NAK）、`process_ctrl` 的 `Nak` 分支（发送侧吃 NAK）、`next_data_packets`（用 `snd_loss_list` 重传） |
| [src/state/socket_state.rs](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/state/socket_state.rs) | `SocketState` 持有 `rcv_loss_list` 与 `snd_loss_list` 两个字段 |

## 4. 核心概念与源码讲解

### 4.1 LossList：用区间存储丢失序列号

#### 4.1.1 概念说明

`LossList` 要解决的问题是：**高效地维护一个会不断增删的「丢失序列号集合」，并支持「找下一个 ≥ 某值的丢失点」这类循环查询**。

设计上有三个关键决策：

1. **存区间而非单点**。内部是 `BTreeMap<SeqNumber, (SeqNumber, SeqNumber)>`：键是区间起点，值是 `(起点, 终点)`。一段连续丢失 `[5,10]` 只占一条记录。
2. **用 `BTreeMap` 按起点排序**。这样「找某个值附近的所有区间」可以用 `range(..)` / `range(x..)` 这类对数时间查询，是合并、拆分、`peek_after` 的基础。
3. **插入时自动合并相邻/重叠区间**。保证任意两个区间既不重叠也不相邻（相邻即 `前一段.end + 1 == 后一段.start`），这是后续「区间编码」能正确工作的前提。

#### 4.1.2 核心流程

五个方法的职责一句话概括：

- `insert(n1, n2)`：把区间 `[n1, n2]` 并入集合，**吸收**所有被它覆盖或与之相邻的旧区间。
- `remove(num)`：删掉**单个**序列号 `num`；若它落在某个区间内部，则把该区间**拆成两段**。
- `remove_all(n1, n2)`：删掉 `[n1, n2]` 内所有点（逐个调 `remove`，支持回绕）。
- `pop_after(after)`：返回环上「≥ `after` 的下一个丢失点」并把它删掉（供发送侧重传取号）。
- `peek_after(after)`：同上但**只看不动**（供接收侧 `send_ack` 钳制 ACK 水位）。

`insert` 的合并逻辑伪代码（最复杂的一个）：

```
insert(n1, n2):
    若 n1 在原始顺序上 > n2：           # 跨越 MAX 边界的回绕区间
        拆成 [n1, MAX] 与 [0, n2] 两次 insert，返回

    # 1) 吸收起点落在 (n1, n2] 内的旧区间，必要时把 n2 向后撑大
    对每个起点 k ∈ (n1, n2] 的旧区间 (k, e)：
        删掉它；若 e > n2，令 n2 = e

    # 2) 看紧贴 n1 之前的那段是否与 [n1, n2] 相邻/重叠
    p = 起点最大的且 ≤ n1 的旧区间 (s, e)
    若存在且 e ≥ n1 - 1：              # 相邻（e == n1-1）或重叠（e ≥ n1）
        把它的 end 扩成 max(e, n2)，返回   # 并入旧区间，不新建

    # 3) 否则新建一条
    插入 (n1, (n1, n2))
```

`remove` 的拆分逻辑：

```
remove(num):
    p = 起点最大的且 ≤ num 的区间 (s, e)
    若 s == num：                      # num 正好是起点
        删掉 (s, e)；若 e > num，补回 (num+1, e)
    否则若 e ≥ num：                   # num 在区间内部或恰为终点
        把当前 end 改成 num-1
        若 e > num，再补一条 (num+1, e)   # 拆成两段
```

#### 4.1.3 源码精读

结构定义只有一行字段（[src/loss_list.rs:L4-L7](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/loss_list.rs#L4-L7)）：

```rust
pub(crate) struct LossList {
    sequences: BTreeMap<SeqNumber, (SeqNumber, SeqNumber)>,
}
```

**`insert` 的回绕处理**（[src/loss_list.rs:L19-L23](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/loss_list.rs#L19-L23)）——注意这里用的是 `number()`（原始 `u32`）比较，**不是**循环 `Sub`：

```rust
if n1.number() > n2.number() {
    self.insert(n1, SeqNumber::max());
    self.insert(SeqNumber::zero(), n2);
    return;
}
```

为什么用原始比较？因为 `BTreeMap` 是按原始顺序排键的，一个「跨过 MAX 边界」的区间（如 `[0x7fff_ffff, 3]`）在原始序里是断开的两段，必须拆成 `[MAX, MAX]` 与 `[0, 3]` 才能存进去。这是「结构靠原始序」的典型体现。

**吸收内部区间**（[src/loss_list.rs:L25-L37](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/loss_list.rs#L25-L37)）：`range((n1 + 1)..=n2)` 找出所有起点严格大于 `n1` 且不超过 `n2` 的旧区间，逐个删除，并把 `n2` 撑到能覆盖的最远终点。这里的 `(n1 + 1)` 用的是循环 `Add<i32>`，`range` 边界用的是派生 `Ord`——两种语义在同一句里协作。

```rust
for (key, (_start, end)) in self.sequences.range((n1 + 1)..=n2) {
    keys_to_remove.push(*key);
    if *end > n2 { n2 = *end; }
}
```

**前缀合并**（[src/loss_list.rs:L39-L45](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/loss_list.rs#L39-L45)）：`range_mut(..=n1).next_back()` 取起点 ≤ `n1` 的最后一段。`*end >= n1 - 1` 是判定「相邻或重叠」的关键——`n1 - 1` 是循环减法（`Sub<i32>`），得到 `n1` 在环上的前一个槽位；`end` 达到这个槽位就说明两段连起来了，直接把旧区间的 `end` 扩成 `max(end, n2)` 即可，不必新建。

```rust
if let Some((_, (_start, end))) = self.sequences.range_mut(..=n1).next_back() {
    if *end >= n1 - 1 {
        *end = std::cmp::max(*end, n2);
        return;
    }
}
self.sequences.insert(n1, (n1, n2));
```

> 仓库已有的单测 `test_insert_overlapping_sequence`（[src/loss_list.rs:L186-L193](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/loss_list.rs#L186-L193)）与 `test_insert_with_multiple_overlapping_sequences`（[src/loss_list.rs:L196-L204](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/loss_list.rs#L196-L204)）正是验证这两段合并逻辑：插入重叠区间后 `sequences.len()` 不增，且端点被正确撑大。

**`remove` 的拆分**（[src/loss_list.rs:L48-L65](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/loss_list.rs#L48-L65)）——单测 `test_remove_seq_inside_sequence`（[src/loss_list.rs:L217-L231](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/loss_list.rs#L217-L231)）演示了删除区间内部点 `[1,10]` 删 `5` 后裂成 `[1,4]` 与 `[6,10]` 两段：

```rust
} else if *end >= num {
    let current_end = *end;
    *end = num - 1;                         // 左半段终点收缩
    if current_end > num {
        self.sequences.insert(num + 1, (num + 1, current_end)); // 右半段
    }
}
```

**`pop_after` / `peek_after` 的「环形找下一个」**（[src/loss_list.rs:L121-L160](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/loss_list.rs#L121-L160)）是本结构最巧妙的部分，分三档兜底：

```rust
// 第 1 档：after 落在某个区间内部 → after 自己就是丢失点
if let Some((_, (_start, end))) = self.sequences.range(..=after).next_back() {
    if *end >= after { ... return Some(after); }
}
// 第 2 档：after 之后还有区间 → 取最小的那个起点
if let Some((_, (start, _end))) = self.sequences.range(after..).next() {
    ... return Some(*start);
}
// 第 3 档：环回绕到整表最小的起点（处理 after 之后原始序里没有、但环上有的情况）
if let Some((_, (start, _end))) = self.sequences.iter().next() {
    ... return Some(*start);
}
```

这三档对应循环空间里「下一个丢失点」的完整定义：先看 `after` 自身是否丢失，再看它之后有没有，最后回绕到表头。`pop_after` 多做一步 `remove`，`peek_after` 不动结构。

#### 4.1.4 代码实践

**实践目标**：用单元测试验证 `LossList` 的两个边界行为——**跨 MAX 回绕插入**与**相邻区间合并**。这两个用例仓库目前没有，补上后能加深你对「原始序 vs 循环减法」混用的理解。

**操作步骤**（在 `src/loss_list.rs` 末尾的测试区追加，或在你的练习 crate 里复现 `LossList`）：

```rust
// 示例代码：建议追加到 src/loss_list.rs 的 #[test] 区域
use crate::seq_number::SeqNumber;

#[test]
fn test_insert_wraps_around_max() {
    let mut ll = LossList::new();
    let max = SeqNumber::max();
    ll.insert(max, 3.into()); // 跨越 MAX 的回绕区间
    // 应拆成 [MAX, MAX] 与 [0, 3] 两条
    assert_eq!(ll.sequences.len(), 2);
    let items: Vec<_> = ll.sequences.clone().into_iter().collect();
    assert_eq!(items, [
        (0.into(), (0.into(), 3.into())),
        (max, (max, max)),
    ]);
}

#[test]
fn test_insert_adjacent_sequences_merge() {
    let mut ll = LossList::new();
    ll.insert(1.into(), 4.into());
    ll.insert(5.into(), 10.into()); // 4 与 5 相邻
    assert_eq!(ll.sequences.len(), 1);
    let items: Vec<_> = ll.sequences.into_iter().collect();
    assert_eq!(items, [(1.into(), (1.into(), 10.into()))]);
}
```

**需要观察的现象**：

1. `test_insert_wraps_around_max`：`insert(max, 3)` 命中 [L19-L23](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/loss_list.rs#L19-L23) 的回绕分支，递归成 `insert(max, max)` 与 `insert(0, 3)`，最终两条记录。
2. `test_insert_adjacent_sequences_merge`：第二次插入命中 [L39-L45](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/loss_list.rs#L39-L45) 的前缀合并，`end(4) >= n1-1(4)` 成立，`end` 被扩成 10，不新建记录。

**预期结果**：`cargo test --lib test_insert_wraps_around_max test_insert_adjacent_sequences_merge` 两条用例均通过。运行结果待本地验证（回环环境下 `SeqNumber::max()` 即 `0x7fff_ffff`，断言不依赖网络）。

#### 4.1.5 小练习与答案

**练习 1**：`insert(1, 10)` 之后再 `insert(5, 20)`，`sequences.len()` 是多少？为什么不需要走前缀合并那条分支？

> **答案**：`len() == 1`，结果是 `(1, (1, 20))`。因为 `[5,20]` 的起点 `5` 落在 `[1,10]` 的 `(n1, n2] = (1, 10]` 内部，先被 [L25-L37](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/loss_list.rs#L25-L37) 的「吸收内部区间」分支删掉并把 `n2` 撑到 20；随后前缀合并分支取到 `(1, 20)` 自身，`end(20) >= n1-1(4)` 成立，原地扩端点。这与仓库单测 `test_insert_overlapping_sequence` 一致。

**练习 2**：`pop_after(after)` 为什么需要第 3 档 `self.sequences.iter().next()`？去掉它会有什么后果？

> **答案**：第 3 档处理「`after` 在原始序里很靠后、但环上『下一个』丢失点其实是表头最小值」的回绕情形（例如 `after` 接近 MAX，而丢失点在 `0` 附近）。去掉它会导致这种情况下返回 `None`，发送方误以为没有需要重传的包，造成丢失包永远不被重传。

---

### 4.2 NAK 的 loss_info 编码：单点与区间

#### 4.2.1 概念说明

NAK（Negative Acknowledgement）是接收方主动告诉发送方「我缺这些包」的控制包。它的载荷 `loss_info` 在线上是一个 `Vec<u32>`——一个扁平的 `u32` 数组，没有任何长度前缀或类型字段。那接收方怎么知道某个 `u32` 是「单个丢失的序列号」还是「一段区间的起点」？

答案是一个**复用最高位**的小技巧：`SeqNumber` 只有 31 位（值域 \([0, 2^{31}-1]\)），最高位（`0x8000_0000`）永远是 0，于是可以无偿拿它当标志位：

- **单个丢失包** `S`：写入一个 `u32`，最高位为 0，即直接写 `S`。
- **一段丢失区间** \([A, B]\)：写入**两个** `u32`——第一个是 `A | 0x8000_0000`（最高位置 1，标记「我是一段区间的起点，请再读一个」），第二个是 `B`（最高位 0）。

这样接收方解析时只要看每个 `u32` 的最高位：是 0 就当单点，是 1 就连同下一个 `u32` 当作区间。一个 NAK 里可以混排任意多个单点和区间。

#### 4.2.2 核心流程

**发送侧（接收方生成 NAK）**：当 `process_data` 发现到达的包 `seq` 与当前期望水位 `curr_rcv_seq_number` 之间有缺口（`seq - curr_rcv_seq_number > 1`），缺的是 \([curr+1,\; seq-1]\)：

```
缺口 = [curr_rcv_seq_number + 1,  seq_number - 1]
若 缺口只有一个点（curr+1 == seq-1）：
    loss_info = [ seq - 1 ]                          # 单点
否则：
    loss_info = [ (curr+1) | 0x8000_0000,  seq - 1 ] # 区间
```

**接收侧（发送方解析 NAK）**：逐个读 `u32`：

```
对 loss_info 中每个 loss：
    若 loss 的最高位 == 1：       # 区间
        seq_start = loss & 0x7fff_ffff   # 剥掉标志位得到起点
        seq_end   = 下一个 u32
    否则：                        # 单点
        seq_start = seq_end = loss
    得到一段丢失 [seq_start, seq_end]
```

#### 4.2.3 源码精读

`NakInfo` 本身极简，载荷就是一个公开字段 `loss_info: Vec<u32>`（[src/control_packet.rs:L339-L342](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/control_packet.rs#L339-L342)）。序列化/反序列化只是把 `u32` 数组与大端字节流互转，**完全感知不到「单点/区间」的语义**——语义全靠那个最高位隐式编码（[src/control_packet.rs:L344-L363](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/control_packet.rs#L344-L363)）：

```rust
pub fn serialize(&self) -> Vec<u8> {
    self.loss_info.iter().flat_map(|x| x.to_be_bytes()).collect()
}
```

`new_nak` 构造函数把 `Vec<u32>` 直接塞进 `NakInfo`（[src/control_packet.rs:L28-L38](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/control_packet.rs#L28-L38)）；控制包类型码 `Nak => 0x0003`（[src/control_packet.rs:L175](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/control_packet.rs#L175)），反序列化时 `0x0003` 走 `NakInfo::deserialize`（[src/control_packet.rs:L199](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/control_packet.rs#L199)）。

**生成侧**在 `process_data` 里（[src/socket.rs:L729-L738](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L729-L738)）：

```rust
let loss_list = {
    if state.curr_rcv_seq_number + 1 == seq_number - 1 {
        vec![(seq_number - 1).number()]                       // 单点
    } else {
        vec![
            (state.curr_rcv_seq_number + 1).number() | 0x8000_0000, // 区间起点
            (seq_number - 1).number(),                              // 区间终点
        ]
    }
};
UdtControlPacket::new_nak(loss_list, self.peer_socket_id().unwrap_or(0))
```

注意判定单点的条件 `curr+1 == seq-1`：化简即 `curr + 2 == seq`，也就是只差一个包。其余情况都按区间编码。这段同时把缺口 `insert` 进了 `rcv_loss_list`（[src/socket.rs:L724-L726](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L724-L726)），可见**写入丢失表与编码 NAK 是同一件事的两面**。

#### 4.2.4 代码实践

**实践目标**：手工在纸面上完成一次 NAK 编码 ↔ 解码的往返，确认你对最高位标志的理解无误。

**操作步骤**：

1. 设 `curr_rcv_seq_number = 5`，到达包 `seq = 8`。算出缺口并写出 `loss_info`。
2. 设 `curr_rcv_seq_number = 5`，到达包 `seq = 7`。再写一次。
3. 把你得到的 `loss_info` 当作收到的 NAK，套用 4.2.2 的解析规则，还原出 `(seq_start, seq_end)`。

**需要观察的现象与预期结果**：

- 情形 1：缺口 `[6, 7]`，两点，是区间 → `loss_info = [6 | 0x8000_0000, 7] = [0x8000_0006, 0x0000_0007]`。解析时读到 `0x8000_0006` 最高位为 1，剥掉得 `seq_start = 6`，再读一个得 `seq_end = 7`，还原 `[6,7]`。
- 情形 2：缺口 `[6, 6]`，单点 → `loss_info = [6]`。解析时读到 `6` 最高位为 0，单点，`(6, 6)`。

结论：单点其实可以看作「退化的区间」（起点==终点），两种编码能无损往返。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `SeqNumber` 是 31 位这件事，对 NAK 编码至关重要？

> **答案**：正因为序列号只有 31 位、最高位恒为 0，才能无偿挪用 `0x8000_0000` 当「这是区间起点」的标志位，而不与任何合法序列号冲突。若序列号是完整 32 位，就无法这样复用，必须额外引入长度/类型字段。

**练习 2**：`NakInfo::serialize` 根本没有「单点/区间」的概念，这样设计有什么好处和隐患？

> **答案**：好处是序列化层极简、零分支，`u32` 数组直接铺成字节流；语义全部集中在收发两端的业务代码（`process_data` 与 `process_ctrl`）。隐患是格式是「隐式」的——若一方写错了最高位，另一方的解析就会错位（把区间起点当单点、或漏读一个 `u32`），而序列化层不会报错。这正是 4.3 节要做 `broken` 合法性判定的原因之一。

---

### 4.3 发送方收到 NAK：process_ctrl 的 Nak 分支

#### 4.3.1 概念说明

发送方收到 NAK 后要做三件事，且**顺序很关键**：

1. **拥塞反应**：立刻通知 `RateControl`「发生丢包了」，触发 AIMD 的乘性回退（窗口缩小）。这一步必须最先做，因为丢包是网络拥塞的最强信号。
2. **登记重传**：把对端报告的丢失区间写入**发送侧**的 `snd_loss_list`，让 `next_data_packets` 在下一轮调度时优先重传这些包。
3. **立即催发**：调 `update_snd_queue(true)` 立即重排发送队列，不必等下一个定时滴答。

同时还要做**合法性判定**：如果 NAK 报告的丢失超出了发送方「已经发出去的最大序列号」，说明双方状态已经对不上，直接把连接标记为 `Broken`。

#### 4.3.2 核心流程

```
收到 NAK：
  若 loss_info 为空：忽略返回（防御）
  on_loss(loss_info[0] 剥掉最高位)     # 拥塞控制：乘性回退
  cc_update()

  逐条解析 loss_info（单点 / 区间）：
      (seq_start, seq_end) = 解析得到的一段
      若 seq_start - seq_end > 0            # 区间反向，非法
         或 seq_end - curr_snd_seq_number > 0  # 报告的丢失超过已发最大号
          → broken = true，跳出
      若 seq_start >= last_ack_received（循环意义）：
          snd_loss_list.insert(seq_start, seq_end)          # 整段都没确认
      否则若 seq_end >= last_ack_received：
          snd_loss_list.insert(last_ack_received, seq_end)  # 截掉已确认的前半段

  若 broken：状态置 Broken，返回
  update_snd_queue(true)                  # 立即触发重传调度
```

这里出现三个发送侧水位，要分清（均定义在 [src/state/socket_state.rs:L20-L26](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/state/socket_state.rs#L20-L26)）：

- `curr_snd_seq_number`：已经交给发送队列的**最大**序列号（已发出去的边界）。
- `last_ack_received`：收到的 ACK 里**最高的连续确认水位**。
- `last_data_ack_processed`：`next_data_packets` 里 `pop_after` 用的基准（详见 [u6-l1](u6-l1-send-main-flow.md)）。

#### 4.3.3 源码精读

整个 `Nak` 分支在 [src/socket.rs:L602-L652](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L602-L652)。

**第一步：拥塞反应**（[src/socket.rs:L606-L614](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L606-L614)）。空 `loss_info` 直接返回；否则取**第一个**丢失项（剥掉最高位 `& 0x7fff_ffff`）喂给 `rate_control.on_loss`，再 `cc_update()` 刷新拥塞窗口。注意只取第一项做拥塞触发——一次 NAK（即使含多段丢失）只算一次拥塞事件，避免一次丢包引发多轮回退。

```rust
if nak.loss_info.is_empty() { ... return Ok(()); }
rate_control.on_loss((nak.loss_info[0] & 0x7fff_ffff).into());
```

**第二步：解析 + 合法性判定 + 登记重传**（[src/socket.rs:L616-L643](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L616-L643)）。解析循环用 `loss_iter` 手动推进，遇到区间起点就连读下一个 `u32`：

```rust
while let Some(loss) = loss_iter.next() {
    let (seq_start, seq_end) = {
        if loss & 0x8000_0000 != 0 {                 // 区间
            if let Some(seq_end) = loss_iter.next() {
                ((loss & 0x7fff_ffff).into(), (*seq_end).into())
            } else {
                broken = true; break;                // 区间起点后缺终点 → 非法
            }
        } else {
            ((*loss).into(), (*loss).into())         // 单点
        }
    };
    if (seq_start - seq_end > 0) || (seq_end - state.curr_snd_seq_number > 0) {
        broken = true; break;                        // 见下方说明
    }
    if seq_start - state.last_ack_received >= 0 {
        state.snd_loss_list.insert(seq_start, seq_end);
    } else if seq_end - state.last_ack_received >= 0 {
        let last_ack_received = state.last_ack_received;
        state.snd_loss_list.insert(last_ack_received, seq_end); // 截掉已确认部分
    }
}
```

**`broken` 判定条件**（[src/socket.rs:L633-L636](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L633-L636)）有两项，任一成立即判定连接损坏：

1. `seq_end - curr_snd_seq_number > 0`：报告的丢失终点**超过了发送方已发出的最大序列号**。接收方不可能丢失一个发送方根本没发过的包，出现这种情况只能说明双方序列号空间已脱节。
2. `seq_start - seq_end > 0`：区间「反向」（起点循环地落在终点之后）。合法区间应有 `start <= end`，循环差应 ≤ 0；为正说明区间被编码错了。

此外 `loss_iter.next()` 读区间终点时若已无数据（`else { broken = true }`），也判损坏——这对应 4.2 练习 2 提到的「隐式格式错位」隐患。

一旦 `broken`，[src/socket.rs:L645-L649](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L645-L649) 把状态置 `UdtStatus::Broken` 并返回，后续交给全局 `Udt` 的 GC 清理（见 [u8-l2](u8-l2-close-linger-gc.md)）。

**登记时的「截掉已确认部分」**很关键：若 NAK 报告的区间起点 `seq_start` 已经在 `last_ack_received` 之前（说明这段前半部分其实已经被 ACK 过了——可能是一个迟到/重复的 NAK），不能傻乎乎地从 `seq_start` 开始重传，否则会重发已确认的数据。代码用 `else if` 把起点钳到 `last_ack_received`，只重传尚未确认的后半段 `[last_ack_received, seq_end]`。

**第三步：立即催发**（[src/socket.rs:L651](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L651)）：`update_snd_queue(true)` 把 `reschedule=true` 传给发送队列，让发送 worker 立刻醒来重新调度。真正的重传发生在 `next_data_packets` 里——`snd_loss_list.pop_after(last_data_ack_processed)` 取出待重传序号（[src/socket.rs:L248-L253](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L248-L253)），这是 [u6-l1](u6-l1-send-main-flow.md) 讲过的「重传优先」入口：

```rust
state.snd_loss_list.pop_after(last_data_ack_processed)
    .map(|seq| (seq, seq - last_data_ack_processed))
```

#### 4.3.4 代码实践

**实践目标**：把「收到 NAK → 写入 snd_loss_list → 重传 → ACK 清理」这条完整生命周期在源码里串一遍，并回答两个关键问题。

**操作步骤（源码阅读型实践）**：

1. 打开 [src/socket.rs](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs)，定位下面四处对 `snd_loss_list` 的操作，按发生顺序排列：
   - 写入：收到 NAK 时 `insert`（[L638/L641](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L637-L642)）。
   - 写入：EXP 超时重传时 `insert(last_ack_received, curr_snd_seq_number)`（[L956-L962](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L956-L962)）。
   - 消费：`next_data_packets` 里 `pop_after`（[L248-L253](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L248-L253)）。
   - 清理：收到 ACK 时 `remove_all(last_data_ack_processed, seq-1)`（[L549-L551](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L549-L551)）。
2. 回答下面「需要观察的现象」中的两个问题。

**需要观察的现象 / 需要回答的问题**：

- **问题 A（单点 vs 区间）**：`process_ctrl` 解析 `loss_info` 时，凭什么区分一个 `u32` 是单点还是区间起点？若一个区间起点 `u32` 之后没有更多数据，代码如何处理？
- **问题 B（broken 判定）**：列出把连接置为 `Broken` 的全部条件。其中哪一条是「对端报告了不可能存在的丢包」？

**预期结果（参考答案）**：

- 问题 A：看该 `u32` 的最高位 `loss & 0x8000_0000`。为 0 即单点，`(loss, loss)`；为 1 即区间起点，剥掉最高位得 `seq_start`，再调 `loss_iter.next()` 取 `seq_end`，若取不到则 `broken = true`。
- 问题 B：共三种触发 `broken` 的情形——(1) 区间终点缺失（`loss_iter.next()` 为 `None`）；(2) `seq_end - curr_snd_seq_number > 0`，即**对端报告的丢失超过了发送方已发出的最大序列号**（不可能存在的丢包，正是本题答案）；(3) `seq_start - seq_end > 0`，区间反向。任一成立都置 `Broken`。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `on_loss` 只用 `loss_info[0]`（且剥掉最高位），而不是遍历所有丢失项？

> **答案**：一次 NAK 往往携带一次「丢包事件」（可能含多段区间），但从拥塞控制的角度，它只代表「网络出现了一次拥塞」，应只触发**一次**乘性回退。若对每段区间都调 `on_loss`，一次丢包会被惩罚多次，窗口收缩过猛。剥掉最高位是因为第一项可能是区间起点（最高位置 1），要还原成真实序列号。

**练习 2**：NAK 登记重传时，为什么会有 `else if seq_end - last_ack_received >= 0` 这条「截断」分支？去掉它、直接 `insert(seq_start, seq_end)` 会怎样？

> **答案**：这条分支处理「迟到的重复 NAK」——报告的区间前半段 `seq_start < last_ack_received` 其实已被 ACK 确认过。若直接 `insert(seq_start, seq_end)`，会把已确认的序列号也塞进 `snd_loss_list`，导致 `next_data_packets` 去重传早被确认的旧数据（浪费带宽，且 `pop_after` 换算 `SndBuffer` 偏移时可能越界或读到错误数据）。截断到 `last_ack_received` 保证只重传尚未确认的部分。

**练习 3**：`rcv_loss_list`（接收侧）和 `snd_loss_list`（发送侧）都叫「丢失表」，它们的写入时机和消费者分别是什么？

> **答案**：`rcv_loss_list` 由接收方在 `process_data` 发现缺口时 `insert`、补到包时 `remove`，消费者是 `send_ack`（用 `peek_after` 钳制 ACK 水位，绝不越过缺口）；`snd_loss_list` 由发送方在收到 NAK（或 EXP 超时）时 `insert`、收到 ACK 时 `remove_all`，消费者是 `next_data_packets`（用 `pop_after` 取号重传）。两者类型相同、逻辑镜像。

## 5. 综合实践

**任务：跟踪一个数据包的「丢失 → 上报 → 重传 → 清理」完整生命周期。**

设定：发送方连续发出 `seq = 10, 11, 12, 13, 14`，其中 `12` 在网络中丢失，其余正常到达接收方；接收方初始 `curr_rcv_seq_number = 9`（握手 ISN 相关，参见 [u3-l2](u3-l2-udt-socket-state.md)）。

请按时间顺序，在源码中找到每一步对应的函数与行号，并回答括号里的问题：

1. **接收方收到 `seq=10`**：`process_data` 检测 `10 - 9 = 1`，无缺口，`curr_rcv_seq_number` 推进到 10。
2. **接收方收到 `seq=11`**：同样无缺口，推进到 11。
3. **接收方收到 `seq=13`**（`12` 丢了）：
   - 走进 [src/socket.rs:L719-L742](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L719-L742) 的缺口分支。
   - 缺口是多少？是单点还是区间？（答：`[12, 12]` 单点，因为 `curr+1(12) == seq-1(12)`）
   - 写入 `rcv_loss_list` 的内容？（答：`insert(12, 12)`）
   - 发出的 NAK `loss_info`？（答：`[12]`，单点编码）
4. **接收方收到 `seq=14`**：仍 `14 - 11 > 1`，但 `[12,12]` 已在 `rcv_loss_list` 里。`send_ack` 用 `peek_after` 把 ACK 水位钳在 `11`（不越过 12 这个缺口）。（参见 [u6-l2](u6-l2-recv-and-ack.md)）
5. **发送方收到这个 NAK**：走 [src/socket.rs:L602-L652](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L602-L652)。
   - `on_loss(12)` 触发拥塞回退；
   - 解析 `loss_info=[12]` → 单点 `(12,12)`，不触发 broken（`12 <= curr_snd_seq_number(14)`）；
   - `snd_loss_list.insert(12, 12)`；
   - `update_snd_queue(true)` 立即催发。
6. **发送方重传**：`next_data_packets` 调 `snd_loss_list.pop_after(last_data_ack_processed)` 取回 `12`（[L248-L253](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L248-L253)），从 `SndBuffer` 读出旧 payload 重打新时间戳发出（参见 [u6-l1](u6-l1-send-main-flow.md)）。
7. **重传的 `12` 到达接收方**：`process_data` 里 `12 - curr_rcv_seq_number(11) = 1`... 注意此时 `curr_rcv_seq_number` 仍是 11（因为 13、14 到达时缺口未补，水位被钳在 11）。`seq(12) - curr(11) > 0` 成立，于是走 [src/socket.rs:L753](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L753) 的 `rcv_loss_list.remove(12)`（补到了，从丢失表移除），水位随后逐步推进过 13、14。
8. **发送方收到覆盖 12 的 ACK**：[src/socket.rs:L549-L551](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L549-L551) 的 `snd_loss_list.remove_all(...)` 把 `12` 从发送侧丢失表清掉，重传闭环完成。

完成后，你应该能用一张图画出 `seq=12` 在 `rcv_loss_list` 与 `snd_loss_list` 两张表里的进出时间点，并指出每个步骤用到了 `LossList` 的哪个方法（`insert` / `peek_after` / `pop_after` / `remove` / `remove_all`）。

## 6. 本讲小结

- `LossList` 用 `BTreeMap<SeqNumber, (start,end)>` 存**区间**而非单点，插入时自动**吸收/合并**相邻重叠区间，删除内部点时会**拆分**——这让成串丢包的存储与编码都很紧凑。
- 它混用两套比较语义：`BTreeMap` 的结构、`range` 查询、回绕判定靠**原始 `u32` 顺序**；相邻/缺口判断靠**循环 `Sub`（返回 `i32`）**。读源码时要分清当前用的是哪一套。
- `pop_after` / `peek_after` 用「≤after / ≥after / 回绕到表头」三档兜底，正确表达循环空间里「下一个丢失点」；前者供发送侧取号重传，后者供接收侧钳制 ACK 水位。
- NAK 的 `loss_info` 是扁平 `Vec<u32>`，靠**最高位 `0x8000_0000`** 区分单点（一位）与区间（两位：起点置最高位 + 终点）；这能work的前提是 `SeqNumber` 只有 31 位。
- 收发两侧各持一份 `LossList`：接收侧 `rcv_loss_list`（发现缺口 `insert`、补到 `remove`，并钳 ACK），发送侧 `snd_loss_list`（收 NAK `insert`、收 ACK `remove_all`，供重传 `pop_after`）。
- 发送方 `process_ctrl` 的 `Nak` 分支顺序固定：先 `on_loss` 做拥塞回退，再解析并登记重传（含「截掉已确认部分」与 `broken` 合法性判定），最后 `update_snd_queue(true)` 立即催发；`broken` 的核心判定是「报告的丢失超过了 `curr_snd_seq_number`」。

## 7. 下一步学习建议

- 本讲只讲了「丢失是怎么登记的」，**重传包如何实际发出**已在 [u6-l1 socket 发送主流程](u6-l1-send-main-flow.md) 讲过（`next_data_packets` 的「重传优先」），建议回看对照 `pop_after` 的消费端。
- 丢失反馈的另一面——**ACK 如何用 `rcv_loss_list.peek_after` 钳制水位、ACK2 如何测量 RTT**——见 [u6-l2](u6-l2-recv-and-ack.md) 与 [u6-l4 ACK2、AckWindow 与 RTT 测量](u6-l4-ack2-rtt.md)。
- 拥塞控制对丢包的反应（`on_loss` 的 AIMD 乘性回退、`avg_nak_num`、`dec_random`）留待 [u7-l2 速率控制 RateControl](u7-l2-rate-control.md)。
- EXP 超时也会往 `snd_loss_list` 塞整个未确认区间（本讲 [4.3.3](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L956-L962) 提到），完整定时器逻辑见 [u7-l3 定时器：EXP、keep-alive 与超时重传](u7-l3-timers.md)。
- 建议继续精读 `src/loss_list.rs` 末尾的全部单元测试，以及 `src/socket.rs` 中 `process_data` / `process_ctrl` 的 `Nak` / `MsgDropRequest` 分支，把「丢包检测—NAK 编码—重传登记」三段彻底打通。
