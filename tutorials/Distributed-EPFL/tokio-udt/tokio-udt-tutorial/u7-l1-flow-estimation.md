# 流量与带宽估计 UdtFlow

## 1. 本讲目标

UDT 的拥塞控制要把发送速率调到「恰好填满链路又不堵爆它」，要做到这一点，发送方必须知道两件事：**这条链路大概有多快**（带宽 / 包到达速率），以及**一个包往返要多久**（RTT）。可这两者都无法直接测量——它们只能从一个个到达的数据包里**估算**出来。

`src/flow.rs` 里的 `UdtFlow` 就是这个「估算器」。本讲读完之后，你应该能够：

1. 说清 `arrival_window`（相邻包到达间隔）与 `probe_window`（探针包对间隔）各自采的是什么、有什么区别。
2. 复述 `get_pkt_rcv_speed` / `get_bandwidth` 里「先取中位数、再用 `中位数/8 ~ 中位数×8` 过滤离群值、最后换算成速率」的三步做法，并解释为什么这样做。
3. 写出 `update_rtt` / `update_rtt_var` / `update_bandwidth` / `update_peer_delivery_rate` 四个 EWMA 更新方法的加权系数（\(7/8\)、\(3/4\)），并解释「指数加权移动平均」为什么适合平滑网络噪声。
4. 复述这三个估算值（RTT、带宽、包到达速率）是如何被打包进 full ACK 的 `AckOptionalInfo`、再上报给发送方的。

本讲只讲「怎么估」与「怎么平滑」，**不讲**这些估算值最终如何驱动拥塞窗口与发送节奏——那是下一讲 u7-l2（RateControl）的内容。

## 2. 前置知识

在进入源码之前，先建立四个直觉。它们都是网络测量里的经典概念。

**EWMA（指数加权移动平均）。** 网络里的 RTT、带宽时刻在抖动，如果直接拿最新一次的原始测量值去用，控制算法会被噪声带得忽上忽下。EWMA 的做法是：用一个「老估计」与一个「新样本」加权求和：

\[
\hat{x}_{new}=\alpha\cdot\hat{x}_{old}+(1-\alpha)\cdot x_{sample}
\]

\(\alpha\) 越接近 1，平滑越强（老估计的惯性大、对新样本反应慢）；\(\alpha\) 越接近 0，反应越灵敏但越抖。例如 \(\alpha=7/8\) 表示「新样本只占八分之一权重」。

**中位数（median）比均值更抗离群值。** 假如 10 次测量里有 9 次约 1ms、1 次是 200ms（某个包被路由器排队卡住了），取均值会被这一个尖峰拉高很多，但中位数几乎不动。所以在网络这种「偶发长尾」的场景里，用中位数当「代表值」更稳。

**Packet-pair（包对）带宽估计。** 经典原理：让发送方**背靠背**（back-to-back，中间不插入其它包的发送延迟）发出两个包。这对包经过链路上最窄的那个瓶颈时，会被「拉开」成间隔约 \(\frac{\text{包大小}}{\text{瓶颈带宽}}\) 的两个包到达接收方。于是**测量这对包在接收方的到达间隔，就能反推瓶颈带宽**。

**RTT 与 RTT variance（方差）。** RTT（round-trip time）是一个包从发出到被确认的往返时间；`rtt_var` 衡量 RTT 的「抖动幅度」。二者一起决定超时判定阈值（UDT 用 \(\text{rtt}+4\cdot\text{rtt\_var}\) 作为「该收到回应」的时间界，见 u6-l4 与本讲 4.4）。

> 前置讲义承接：本讲依赖 u3-l2（`UdtSocket` 的 `flow: Mutex<UdtFlow>` 字段）、u6-l2（接收数据与 full ACK 的 `AckOptionalInfo`）、u6-l4（ACK2 路径测 RTT 并调用本讲的 `update_rtt` / `update_rtt_var`）。本讲正是把 u6-l4 里「黑盒的 EWMA 方法」打开来看。

## 3. 本讲源码地图

| 文件 | 角色 | 关键符号 |
|---|---|---|
| [src/flow.rs](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/flow.rs) | **核心**：估算器本身 | `UdtFlow`、两个采样窗口、`get_pkt_rcv_speed`、`get_bandwidth`、四个 `update_*` |
| [src/socket.rs](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs) | 估算器的**喂料方**与**取料方** | `process_data`（喂到达/探针样本）、`send_ack`（取速率打包进 ACK）、`process_ctrl` 的 Ack/Ack2 分支（平滑对端上报值） |
| [src/control_packet.rs](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/control_packet.rs) | full ACK 的可选信息载体 | `AckOptionalInfo`（5 个 u32 字段） |
| [src/rate_control.rs](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/rate_control.rs) | 估算值的**下游消费者** | `init`、`set_bandwidth`、`set_rcv_rate`、`on_ack` |

一句话分工：`UdtFlow` 负责「估」与「平滑」，`socket.rs` 负责「喂数据进 `UdtFlow`」和「把 `UdtFlow` 的结果读出来塞进 ACK」，`rate_control` 拿到上报值后真正调发送参数。

## 4. 核心概念与源码讲解

本讲对应三个最小模块：**采样窗口（arrival/probe）**、**中位数过滤估速**、**EWMA 平滑**。为讲清楚探针机制，把第一个模块拆成 4.1（到达间隔）与 4.2（探针间隔）两节，二者合起来即「arrival/probe window」这一最小模块。

### 4.1 到达间隔采样：arrival_window

#### 4.1.1 概念说明

`arrival_window` 想回答的问题是：**「这个连接上，数据包大概是多长时间来一个？」**。知道这个，就能算出「每秒到多少个包」（packet receive rate），它是衡量接收侧吞吐的直接指标，也是 full ACK 上报给发送方的关键字段之一。

做法很朴素：**记录每两个相邻数据包到达的时间差**（\(\Delta t = t_{now}-t_{prev}\)），把这一串 \(\Delta t\) 存进一个固定长度的滑动窗口。窗口里攒够若干个间隔后，用统计方法（见 4.3）换算成每秒包数。

#### 4.1.2 核心流程

- 维护一个长度上限为 `ARRIVAL_WINDOW_SIZE = 16` 的 `VecDeque<Duration>`。
- 每来一个数据包，计算 `now - last_arrival_time`，push 到队尾。
- 若超过 16 个，把最老的（队首）弹掉——这就是「滑动」。
- 把 `last_arrival_time` 更新为 `now`，供下一个包算间隔。

#### 4.1.3 源码精读

常量与结构体定义：

[src/flow.rs:4-6](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/flow.rs#L4-L6) — 两个窗口大小常量；`PROBE_MODULO = 16` 是探针包的「每 16 个取一个」周期（4.2 用到）。

[src/flow.rs:8-20](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/flow.rs#L8-L20) — `UdtFlow` 结构体。注意它**同时持有两套采样工具**：`arrival_window` 配 `last_arrival_time`（采相邻包间隔），`probe_window` 配 `probe_time`（采探针对间隔）；外加四个被平滑后的输出量 `rtt` / `rtt_var` / `peer_bandwidth` / `peer_delivery_rate`。

[src/flow.rs:40-46](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/flow.rs#L40-L46) — `on_pkt_arrival`：推入「与上一个包的时间差」，超长则 `pop_front`，再更新 `last_arrival_time`。这就是滑动窗口的核心三行。

喂料点在接收主循环里——**每收到一个数据包都调用一次**：

[src/socket.rs:685-694](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L685-L694) — `process_data` 中先 `flow.on_pkt_arrival(now)`（无差别地对每个数据包采样），再判断是否为探针包（4.2）。注意这一整段在 `self.flow.write()` 的写锁内，保证采样原子。

#### 4.1.4 代码实践

**目标**：看清 `arrival_window` 里到底攒了什么。

1. 打开 [src/flow.rs:40-46](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/flow.rs#L40-L46)，在 `push_back` 后临时加一行（仅作学习观察，本任务不修改源码，可自行在本地副本上试）：
   ```rust
   eprintln!("[flow] arrival Δt = {:?}", now - self.last_arrival_time);
   ```
2. 用 u1-l2 的办法分别跑 `udt_receiver` 与 `udt_sender`。
3. **观察**：receiver 的 stderr 会持续打印一连串微秒级的 `Δt`。当发送速率稳定时，这些 `Δt` 应在一个值附近抖动；当发送方调慢 `pkt_send_period`（拥塞回退）时，`Δt` 会明显变大。
4. **预期结果**：`Δt` 的数量级与发送方的 `Period`（`udt_sender` 打印值）大致吻合——这验证了「相邻包到达间隔」确实反映了发送节奏。

> 待本地验证：回环（loopback）环境下 `Δt` 可能极小且不稳定，建议在有限速的真实链路或 `tc netem` 加延迟的环境下观察更明显。

#### 4.1.5 小练习与答案

**练习 1**：为什么窗口长度选 16，而不是 2 或 1000？

**答案**：太小（如 2）样本太少，统计毫无意义；太大（如 1000）会让估算对「很久以前的链路状态」反应迟钝——链路带宽变了要等很久才反映出来。16 是 UDT4 参考实现的经验值，在「够稳」与「够跟手」之间取折中。

**练习 2**：`on_pkt_arrival` 里为什么是 `pop_front`（弹最老）而不是 `pop_back`（弹最新）？

**答案**：滑动窗口要保留**最近**的样本、丢弃**最旧**的样本，所以超长时弹队首（最老）。若弹队尾，新样本会被立刻丢掉，窗口永远停留在 16 个旧值上，无法反映最新状态。

---

### 4.2 链路带宽采样：probe_window 与 probe 包对

#### 4.2.1 概念说明

`arrival_window` 采的是「相邻包间隔」，它反映的是**发送方当前发送节奏**，而不是**链路本身的能力**。举个例子：发送方因为拥塞主动降速到每秒 100 包，`arrival_window` 测出来就是 100 包/秒，但这并不代表链路只能跑 100 包/秒——链路可能能跑 10000 包/秒。

要知道链路的**真实容量**，UDT 用了前面讲的 **packet-pair 原理**：周期性地发出两个「背靠背」的探针包，在接收方测它们的到达间隔，反推瓶颈带宽。这就是 `probe_window` 与「probe 包」的用途。

#### 4.2.2 核心流程

探针机制是**收发双方配合**的：

1. **发送方**在 `next_data_packets` 里，每当新发出的最后一个包序列号满足 `seq % 16 == 0` 时，把它标记为探针包；探针包**不施加包间 pacing 延迟**，立即（`target_time = now`）发送，从而让它「尽快背靠背地」跟上后续包。
2. **接收方**对每个数据包检查：
   - `seq % 16 == 0` → 这是 probe1，记下到达时刻 `probe_time`。
   - `seq % 16 == 1` → 这是紧跟 probe1 的 probe2，计算 `now - probe_time`，把这对包的到达间隔 push 进 `probe_window`。
3. `probe_window` 长度上限 `PROBE_WINDOW_SIZE = 64`，同样滑动淘汰最旧。

于是 `probe_window` 里攒的是「一连串探针对的到达间隔」，每个间隔都大致反映一次瓶颈带宽。

#### 4.2.3 源码精读

发送方标记探针（socket.rs，`next_data_packets` 内）：

[src/socket.rs:323-325](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L323-L325) — 取到新数据后，若最新序列号 `% 16 == 0` 则置 `probe = true`。

[src/socket.rs:338-340](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L338-L340) — 探针包直接返回 `(packets, now)`，跳过下面的 `interpacket_interval` 节奏计算，即「立即发送、不等延迟」。这是 packet-pair 能成立的前提。

接收方配对采样（socket.rs，`process_data` 内）：

[src/socket.rs:685-694](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L685-L694) — `on_pkt_arrival` 之后，`seq % 16 == 0` 调 `on_probe1_arrival`（打时间戳），`seq % 16 == 1` 调 `on_probe2_arrival`（算间隔入窗口）。

时间戳的记录与入窗（flow.rs）：

[src/flow.rs:48-58](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/flow.rs#L48-L58) — `on_probe1_arrival` 只做一件事：`probe_time = Instant::now()`；`on_probe2_arrival` 把 `now - probe_time` 推进 `probe_window` 并滑动到 64 以内。

> 注意一个工程细节：发送方只对 `seq % 16 == 0` 主动去 pacing，而接收方对 `0` 和 `1` 分别取两个时间戳。也就是说「一对探针」就是序列号 \((16k,\ 16k{+}1)\) 这两个相邻包。若中途丢包（比如 `16k` 丢了、只收到 `16k{+}1`），`on_probe2_arrival` 会用到一个「陈旧的 `probe_time`」，导致该次间隔偏大；这类噪声正是 4.3 中位数过滤要剔除的对象。

#### 4.2.4 代码实践

**目标**：理解探针包的成对关系，并能解释为何用 `0` 和 `1`。

1. 读 [src/socket.rs:323-340](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L323-L340)，确认「发送方只标记 `seq%16==0`，并让它立即发送」。
2. 读 [src/socket.rs:689-693](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L689-L693)，确认「接收方对 `0` 打戳、对 `1` 算间隔」。
3. **思考并回答**：为什么不是发送方对 `0` 和 `1` 都标记、都立即发送？
4. **预期结论**：因为 `1` 是紧跟 `0` 的下一个包，发送方只要保证 `0` 不被延迟（立即发送），那么 `0→1` 这一对在发送侧就近似背靠背；接收方测到的 `0→1` 到达间隔就能反映瓶颈。只标记一个点即可达到 packet-pair 效果，实现更简单。

#### 4.2.5 小练习与答案

**练习 1**：`PROBE_MODULO` 改成 8 或 64，分别会有什么影响？

**答案**：改成 8 → 探针更密，`probe_window` 填充更快、带宽估计更跟手，但探针包占比翻倍，且更多「背靠背」突发可能轻微干扰正常 pacing；改成 64 → 探针稀疏，`probe_window` 慢慢才填满 64 个，带宽估计更新迟钝、对链路变化反应慢。16 是带宽估计刷新频率与开销的折中。

**练习 2**：`probe_window` 用 64 个样本，而 `arrival_window` 只用 16 个，为什么带宽窗口更大？

**答案**：探针对的间隔噪声更大（受丢包、跨核调度、单次排队影响显著），单次测量很不可靠，需要更多样本做中位数过滤才稳；而相邻包间隔相对规律，16 个已足够。样本越不可靠，越需要更大的窗口来「用数量换鲁棒性」。

---

### 4.3 速率换算：中位数 ±8 倍过滤

#### 4.3.1 概念说明

攒满窗口后，要把「一串时间间隔」换算成「每秒包数」。直觉是：把这些间隔加起来得到总时长 `T`，期间到了 `N` 个间隔（即 `N` 个「包到包」间隙），那么速率约为：

\[
\text{rate}=\frac{N}{T}\quad(\text{包/秒})
\]

但直接用全部样本求和会被离群值污染：只要有一个间隔特别大（比如某包被操作系统调度延迟了几毫秒），`T` 就被显著拉大，速率被严重低估。

UDT 的做法是**两步鲁棒化**：

1. 先求**中位数** `med`，作为「典型间隔」的代表。
2. 只保留落在 \((\text{med}/8,\ \text{med}\times 8)\) 区间内的样本——即「和典型值差不到 8 倍」的样本，把离群尖峰剔掉。
3. 用过滤后的样本再求 \(N/T\)。

为什么是 8 倍？这是一个经验阈值：正常网络抖动通常在一个数量级以内，超过 8 倍的间隔几乎可以肯定是异常（调度延迟、丢包补偿、探针错位等），剔除它们能让估计稳得多。

#### 4.3.2 核心流程

`get_pkt_rcv_speed`（基于 `arrival_window`）的步骤：

1. 克隆窗口，用 `select_nth_unstable(len/2)` 以 O(n) 找到中位数（不完整排序，只保证第 `len/2` 个位置落对了）。
2. 过滤：保留 `x > med/8 && x < med*8`。
3. 若过滤后样本数 `< ARRIVAL_WINDOW_SIZE/2 = 8`，认为数据不足，直接返回 0。
4. 否则 `rate = ceil(N_filtered / T_filtered)`，向上取整成 `u32`。

`get_bandwidth`（基于 `probe_window`）步骤几乎一样，**区别**有两点：窗口为空直接返回 0；没有「样本数不足返回 0」的下限检查（探针本就稀疏，不强制要求半满）。

#### 4.3.3 源码精读

[src/flow.rs:60-75](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/flow.rs#L60-L75) — `get_pkt_rcv_speed`。关键三处：

- `select_nth_unstable(length / 2)`：取中位数，注释 `// Returns a number of packets per second` 说明返回单位是「包/秒」。
- `.filter(|x| *x > median / 8 && *x < median * 8)`：±8 倍过滤。
- `if values.len() < ARRIVAL_WINDOW_SIZE / 2 { return 0; }`：半数下限保护。
- `(values.len() as f64 / total_duration.as_secs_f64()).ceil() as u32`：换算并向上取整。

[src/flow.rs:77-94](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/flow.rs#L77-L94) — `get_bandwidth`，结构与上面相同，但少了半数下限检查，多了 `total_duration.is_zero()` 的除零保护。

这两个值在 full ACK 里被打包上报（见 4.4.3 的 send_ack 引用，以及 [src/socket.rs:854-856](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L854-L856)）。

#### 4.3.4 代码实践

**目标**：手工验证中位数过滤剔除离群值的效果。

1. 假设 `arrival_window` 里有 16 个间隔（微秒）：15 个都是 `100`，1 个是 `100000`（一个明显的调度毛刺）。
2. 推算：
   - 中位数 `med = 100`（15 个 100 占多数，第 8 名一定是 100）。
   - 过滤区间：`(100/8, 100*8) = (12, 800)`。
   - `100` 落在区间内被保留（15 个），`100000` 被剔除。
   - 速率 \(\approx 15 / (15\times100\mu s) = 10000\) 包/秒。
3. **对比**：若不过滤，总时长 \(=15\times100 + 100000 = 101500\mu s\)，速率 \(\approx 16/101500\mu s \approx 158\) 包/秒——被一个毛刺低估了 60 多倍！
4. **预期结论**：中位数 ±8 倍过滤在此例中把误差从「差 60 倍」拉回到「基本准确」，这就是它存在的意义。

> 待本地验证：可在 `get_pkt_rcv_speed` 入口临时打印 `median` 与过滤前后的 `values.len()`，跑 sender/receiver 观察过滤剔除了多少样本。

#### 4.3.5 小练习与答案

**练习 1**：为什么用 `select_nth_unstable` 而不是先 `sort()` 再取中间元素？

**答案**：`select_nth_unstable` 只需 \(O(n)\) 就能把第 k 大的元素放到位（快排的分区思想），而完整排序是 \(O(n\log n)\)。这里只需要中位数那一个值，不需要整体顺序，用 `select_nth_unstable` 更省。而且这是在收包热路径上的调用，省常数很重要。

**练习 2**：`get_pkt_rcv_speed` 有「样本不足返回 0」保护，`get_bandwidth` 没有，这会不会让带宽估计更不可靠？

**答案**：理论上是的——样本很少时中位数本身就不稳，没有下限保护可能用两三个样本就给出一个带宽值。但探针本就每 16 个包才产生一对，`probe_window` 要填满较慢，强制半满会让带宽估计长期返回 0、无法上报；UDT 选择「宁可早点给个粗略值，也比没有强」，由 4.4 的 EWMA 平滑和 ACK 的 `SYN_INTERVAL` 节流（每 10ms 才采样一次速率）来抑制噪声。

---

### 4.4 EWMA 平滑：RTT、带宽与交付速率

#### 4.4.1 概念说明

前面两节得到的是「原始瞬时估计」。但网络测量噪声大，直接用会让控制算法抖动。`UdtFlow` 的四个 `update_*` 方法用 **EWMA** 把新样本柔和地融进老估计。

四个方法各自的语义：

| 方法 | 平滑的对象 | 输入「新样本」来自 | 加权系数 \(\alpha\)（老估计权重） |
|---|---|---|---|
| `update_rtt` | `rtt` | ACK2 实测 RTT，或 full ACK 里对端汇报的 `rtt` | \(7/8\) |
| `update_rtt_var` | `rtt_var`（RTT 方差） | 实测 RTT 与老 `rtt` 的绝对差，或对端汇报的 `rtt_variance` | \(3/4\) |
| `update_bandwidth` | `peer_bandwidth` | full ACK 里对端汇报的 `link_capacity` | \(7/8\) |
| `update_peer_delivery_rate` | `peer_delivery_rate` | full ACK 里对端汇报的 `pack_recv_rate` | \(7/8\) |

注意 RTT 的方差用 \(3/4\) 而不是 \(7/8\)——它对变化反应更快，因为方差的突变（链路突然开始抖）需要尽快被超时判定捕捉到。

#### 4.4.2 核心流程

通用 EWMA 更新公式（以 `update_rtt` 为例）：

\[
\text{rtt}_{new}=\frac{7\cdot\text{rtt}_{old}+\text{sample}}{8}
\]

四个方法都是这一行的变体（系数换成 \(7/8\) 或 \(3/4\)）。关键在于**谁在什么时候喂样本进来**——这正是 ACK / ACK2 两条反馈路径（u6-l4 已建立）：

- **ACK2 路径（接收方本地实测）**：接收方发 full ACK 时按下秒表，收到发送方回的 ACK2 时停表，得到一个 RTT 样本，调用 `update_rtt`；并用 \(|\text{sample}-\text{rtt}_{old}|\) 作为「方差样本」调用 `update_rtt_var`。
- **ACK 可选信息路径（发送方读对端汇报）**：发送方收到带 `AckOptionalInfo` 的 full ACK 后，把里面的 `rtt` / `rtt_variance` / `pack_recv_rate` / `link_capacity` 分别灌进对应 `update_*`，再做二次平滑。

两条路径互补：单向大流量传输时发送方自己不发数据、收不到 ACK2，只能依赖可选信息路径（详见 u6-l4）。

#### 4.4.3 源码精读

四个 EWMA 方法（极简，各自一行公式）：

[src/flow.rs:96-110](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/flow.rs#L96-L110) — `update_rtt` 用 \(7/8\)，`update_rtt_var` 用 \(3/4\)，`update_bandwidth` 与 `update_peer_delivery_rate` 都用 \(7/8\)。初始值见 [src/flow.rs:31-34](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/flow.rs#L31-L34)：`rtt=100ms`、`rtt_var=50ms`、`peer_bandwidth=1`、`peer_delivery_rate=16`。

ACK2 路径的喂料（接收方，socket.rs `process_ctrl` 的 Ack2 分支）：

[src/socket.rs:585-594](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L585-L594) — 算出 `rtt` 后，先求 `rtt_abs_diff = |rtt - flow.rtt|`（偏离当前估计的幅度），再 `update_rtt_var(rtt_abs_diff)`、`update_rtt(rtt)`。注意先更新 `rtt_var` 再更新 `rtt`，保证 `rtt_var` 用的是「更新前的老 rtt」算差。

ACK 可选信息路径的喂料（发送方，socket.rs `process_ctrl` 的 Ack 分支）：

[src/socket.rs:558-572](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L558-L572) — 收到 full ACK 的 `extra` 后：`update_rtt(extra.rtt)`、`update_rtt_var(extra.rtt_variance)`，并把带宽与交付速率平滑后立刻同步给 `rate_control`（`set_bandwidth` / `set_rcv_rate`）。

这些平滑值如何被打包上报（接收方，socket.rs `send_ack`）：

[src/socket.rs:840-858](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L840-L858) — 构造 `AckOptionalInfo`：`rtt` 与 `rtt_variance` 直接取 `flow.rtt` / `flow.rtt_var` 的微秒值；`pack_recv_rate` 与 `link_capacity` 仅在距上次上报超过 `SYN_INTERVAL = 10ms` 时才调用 `get_pkt_rcv_speed` / `get_bandwidth` 重新采样（避免每个 ACK 都做中位数过滤的开销）。

`AckOptionalInfo` 的线上结构（5 个 u32，见 [src/control_packet.rs:329-337](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/control_packet.rs#L329-L337)）：

| 字段 | 含义 | 来源 |
|---|---|---|
| `rtt` | RTT（微秒） | `flow.rtt` |
| `rtt_variance` | RTT 方差（微秒） | `flow.rtt_var` |
| `available_buf_size` | 接收缓冲可用包数 | `RcvBuffer::get_available_buf_size()`（≥2） |
| `pack_recv_rate` | 包到达速率（包/秒） | `flow.get_pkt_rcv_speed()` |
| `link_capacity` | 链路带宽（包/秒） | `flow.get_bandwidth()` |

它由 full ACK 携带（[src/control_packet.rs:311-326](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/control_packet.rs#L311-L326) 的 `serialize`，light ACK 走 `info: None` 不带这些字段）。

下游消费（rate_control.rs）：

[src/rate_control.rs:63-77](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/rate_control.rs#L63-L77) — `RateControl::init` 在连接建立时直接拷贝 `flow` 的 `peer_delivery_rate` / `peer_bandwidth` / `rtt` 作为拥塞控制的初值；运行中由 `set_bandwidth` / `set_rcv_rate` / `set_rtt` 持续更新。

> 额外一提：`UdtFlow` 还有一个 `flow_window_size` 字段，它是**流控窗口**（接收方通过 `available_buf_size` 上报、握手时初值取对端 `max_window_size`，见 [src/socket.rs:183](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L183)），与「带宽估计」是两回事——它约束「在途未确认包数」，与 `congestion_window_size` 取 min 作为实际发送窗口（[src/socket.rs:301-304](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L301-L304)）。本讲不展开它，只需知道它住在 `UdtFlow` 里、但语义独立。

#### 4.4.4 代码实践

**目标**：复述 RTT / 带宽 / 包到达速率是如何从「接收方估算」最终「上报给发送方并进入 RateControl」的全链路。

1. 画出下面这条数据流（建议在纸上或笔记里）：

   ```
   接收方 process_data
     ├─ flow.on_pkt_arrival  ─→ arrival_window
     └─ flow.on_probe1/2     ─→ probe_window
                                          │
   接收方 send_ack（full ACK，每 10ms 采样一次速率）
     ├─ rtt        ← flow.rtt
     ├─ rtt_var    ← flow.rtt_var
     ├─ pack_recv_rate ← flow.get_pkt_rcv_speed()  ← arrival_window（中位数±8过滤）
     └─ link_capacity  ← flow.get_bandwidth()      ← probe_window（中位数±8过滤）
                          │  打包成 AckOptionalInfo，随 full ACK 发出
                          ▼
   发送方 process_ctrl 的 Ack 分支
     ├─ flow.update_rtt(extra.rtt)
     ├─ flow.update_rtt_var(extra.rtt_variance)
     ├─ flow.update_peer_delivery_rate(extra.pack_recv_rate) → rate_control.set_rcv_rate
     └─ flow.update_bandwidth(extra.link_capacity)           → rate_control.set_bandwidth
                          │
                          ▼
   RateControl.on_ack / on_loss  用这些值调 pkt_send_period 与 congestion_window_size（u7-l2）
   ```

2. 回答三个问题：
   - 为什么 `rtt` / `rtt_var` **每个** full ACK 都上报，而 `pack_recv_rate` / `link_capacity` 每 10ms 才重算一次？
   - 发送方收到的 `extra.rtt` 是「接收方平滑过的」还是「原始实测」的？
   - 发送方为何还要对 `extra.rtt` **再做一次** `update_rtt`（即二次 EWMA）？
3. **预期结论**：
   - RTT 变化快且开销小（只是读字段），故每 ACK 都带；速率估计要做中位数过滤、开销大，故节流到 10ms 一次。
   - `extra.rtt` 是接收方用 ACK2 实测并 EWMA 平滑后的值（接收方一侧的 `flow.rtt`）。
   - 二次 EWMA 是因为对端汇报值本身有噪声（且受 ACK 发送节奏采样），发送方再平滑一层能让本地拥塞决策更稳——这是「双重平滑」，更稳但反应更慢（u6-l4 已对比过两条路径）。

#### 4.4.5 小练习与答案

**练习 1**：`update_rtt` 的系数是 \(7/8\)，`update_rtt_var` 是 \(3/4\)。如果把 `rtt_var` 也改成 \(7/8\) 会怎样？

**答案**：`rtt_var` 会变得非常迟钝——当链路突然开始剧烈抖动时，`rtt_var` 要很多个样本才能爬上去，而这期间超时界 \(\text{rtt}+4\cdot\text{rtt\_var}\)（[src/socket.rs:922-926](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L922-L926) 的 EXP 定时器用它）会暂时偏小，导致**误判超时、触发不必要的重传**。用 \(3/4\) 让方差对突变更敏感，及时放宽超时界。

**练习 2**：`peer_bandwidth` 初值是 1（[src/flow.rs:33](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/flow.rs#L33)），而不是 0。为什么？

**答案**：若初值为 0，EWMA 公式 \((7\times0+\text{sample})/8\) 在收到第一个真实样本前一直是 0，下游 `RateControl` 用到带宽的地方（如 `on_ack` 里 \(b=\text{bandwidth}-1/\text{period}\)）会出现除零或退化为慢速；用 1 当一个「极小但非零」的占位值，避免冷启动阶段的数值病态，等真实样本陆续到来后被 EWMA 逐步覆盖。

---

## 5. 综合实践

把本讲三条主线串起来：**探针包从发出到最终影响发送方的完整生命周期**。

**任务**：跟踪一个 `seq = 16` 的探针包，回答它在每一站发生了什么、被谁读取、最终影响了什么。

参考调用链（请逐站打开对应源码确认）：

1. **发送方** `next_data_packets`：取到含 `seq=16` 的批次，`16 % 16 == 0` → `probe = true` → 立即返回（[src/socket.rs:323-340](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L323-L340)）。
2. **接收方** `process_data`：`seq=16` → `on_probe1_arrival`（记 `probe_time`）；紧接的 `seq=17` → `on_probe2_arrival`（把 `now - probe_time` 推进 `probe_window`）（[src/socket.rs:685-694](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L685-L694)、[src/flow.rs:48-58](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/flow.rs#L48-L58)）。
3. **接收方** `send_ack`（满 10ms）：`get_bandwidth()` 对 `probe_window` 做中位数 ±8 过滤算出带宽，填进 `AckOptionalInfo.link_capacity`，随 full ACK 发出（[src/socket.rs:854-856](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L854-L856)）。
4. **发送方** `process_ctrl` 的 Ack 分支：`update_bandwidth(extra.link_capacity)` 做 EWMA 平滑 → `rate_control.set_bandwidth(flow.peer_bandwidth)`（[src/socket.rs:571-572](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L571-L572)）。
5. **RateControl**：在 `on_ack` 里用 `bandwidth` 计算增量调节 `pkt_send_period`（[src/rate_control.rs:146-162](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/rate_control.rs#L146-L162)）——具体公式留待 u7-l2。

**交付物**：

- 画出上面 5 站的时序图，标注每一步读/写的是 `UdtFlow` 的哪个字段。
- 标出整条链路里**两次中位数过滤**（arrival 一次、probe 一次）和**两次 EWMA 平滑**（接收方一次、发送方一次）各发生在哪。
- 思考：如果 `seq=16` 这个探针包在网络上丢了，这条链路会怎样？（提示：`on_probe1` 不触发，`on_probe2` 用到陈旧 `probe_time`，该次间隔偏大，会被 4.3 的 ±8 过滤大概率剔除。）

> 待本地验证：跑 `udt_sender`/`udt_receiver`，观察 sender 打印的 `Period`（[src/bin/udt_sender.rs:34-37](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/bin/udt_sender.rs#L34-L37)）是否随时间收敛到一个稳定区间——这是带宽估计经 RateControl 反作用到发送节奏的间接证据。

## 6. 本讲小结

- `UdtFlow` 是 tokio-udt 的「网络状态估算器」，产出 RTT、RTT 方差、链路带宽、包到达速率四类平滑估计，供拥塞控制消费。
- 它维护**两个采样窗口**：`arrival_window`（每包都采，记相邻包到达间隔，16 样本）与 `probe_window`（仅探针包对采，记 packet-pair 间隔，64 样本），分别服务于「到达速率」与「链路带宽」两种估计。
- **探针包对**由收发配合产生：发送方对 `seq % 16 == 0` 立即发送（去 pacing），接收方对 `0` 打戳、对 `1` 算间隔，落进 `probe_window`。
- 速率换算用**中位数 ±8 倍过滤**剔除离群毛刺，再 \(N/T\) 换算成每秒包数；这是鲁棒统计在网络热路径上的典型应用。
- 四个 `update_*` 方法用 **EWMA** 平滑：RTT/带宽/交付速率用 \(7/8\)，RTT 方差用 \(3/4\)（对方差突变更敏感，保超时判定不误判）。
- 平滑值经 full ACK 的 `AckOptionalInfo`（5 个 u32）上报给发送方，发送方再二次 EWMA 后灌进 `RateControl`——形成「接收方估 → ACK 上报 → 发送方平滑 → 拥塞控制」的闭环。

## 7. 下一步学习建议

- **下一讲 u7-l2（RateControl：慢启动与 AIMD）**：本讲反复提到的 `set_bandwidth` / `set_rcv_rate` / `set_rtt` 之后的真正主角。去读 [src/rate_control.rs](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/rate_control.rs) 的 `on_ack` / `on_loss`，看 `peer_bandwidth` 与 `peer_delivery_rate` 如何具体换算成 `pkt_send_period` 与 `congestion_window_size`。
- **u7-l3（定时器）**：本讲 4.4 提到 EXP 定时器用 \(\text{rtt}+4\cdot\text{rtt\_var}\)，那一讲会展开 `check_timers` 如何用本讲的 `rtt`/`rtt_var` 判定超时与 keep-alive。
- **回看 u6-l4**：现在再读 ACK2 路径测 RTT 的代码，应该能把「停表→`update_rtt`/`update_rtt_var`」与本讲的 EWMA 公式一一对应上，彻底打通 RTT 测量与平滑。
- **延伸阅读**：可对照 UDT4 原始论文（Gu & Grossglauser）中关于 packet-pair 带宽估计与 EWMA 平滑的描述，理解 tokio-udt 在哪些地方做了简化（如发送方只标记 `0` 而非严格背靠背发 `0` 和 `1`）。
