# 速率控制 RateControl：慢启动与 AIMD

## 1. 本讲目标

本讲剖析 tokio-udt 的发送速率控制器 `RateControl`（定义于 `src/rate_control.rs`），它是 UDT 拥塞控制算法的「大脑」：根据接收方经 ACK 上报的网络状态，决定发送方该用多大的**拥塞窗口**（在途包数上限）和多大的**包间隔**（相邻两个数据包的时间间隔）。

读完本讲，你应当能够：

- 复述 `RateControl` 三个工作阶段：**慢启动 → AIMD 加性增长 → 丢包/超时的乘性回退**，以及它们的触发条件。
- 解释 `on_ack` 中 AIMD 增长公式里 `b`、`increase` 的含义，以及 `pkt_send_period` 的更新公式为什么等价于「速率上的加性增长」。
- 解释 `on_loss` 的乘性回退（`× 1.125`）、`avg_nak_num` 滑动平均、以及 `dec_random` 随机化为什么能避免多连接同步回退、提升公平性。
- 把 `RateControl` 的输出（窗口、间隔）与上一讲 `UdtFlow`（u7-l1）的输入、以及 `next_data_packets`（u6-l1）的窗口/节奏限流对接起来。

## 2. 前置知识

本讲默认你已掌握以下内容（前序讲义已建立，这里只做最小承接，不重复）：

- **UDT 的反馈链路（u7-l1）**：接收方用 `UdtFlow` 估算出 RTT、链路带宽、包到达速率，经 full ACK 的 `AckOptionalInfo`（5 个 `u32`）上报发送方。本讲的 `RateControl` 正是这些上报值的**消费者**。
- **发送主流程（u6-l1）**：发送 worker 按调度堆到点调用 `next_data_packets`，它用 `min(flow_window, congestion_window)` 限流、用 `interpacket_interval`（即本讲的 `pkt_send_period`）做包级 pacing。本讲回答的就是「这两个值怎么来的、怎么变」。
- **AIMD 拥塞控制直觉**：经典互联网拥塞控制（如 TCP Reno）的核心思想——**和性增长 / 乘性回退**（Additive Increase / Multiplicative Decrease）。探测可用带宽时缓慢加码（线性增长），一旦发现丢包则果断收缩（乘性回退）。本讲把这一思想在 UDT 中的具体实现讲透。

几个本讲反复用到的术语：

| 术语 | 含义 |
|---|---|
| 拥塞窗口 `congestion_window_size` | 发送方允许的最大「在途未确认」包数 |
| 流量窗口 `flow_window_size` | 接收方在握手中通告的接收能力上限（u3-l2 / u8-l1） |
| 包发送间隔 `pkt_send_period` | 相邻两个数据包之间的时间间隔，其倒数即发送速率 |
| BDP（带宽时延积） | 速率 × RTT，即「把链路塞满」所需的在途数据量 |
| 慢启动 / AIMD | 两个工作阶段，详见 4.2 / 4.3 |

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [src/rate_control.rs](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/rate_control.rs) | **本讲主角**。`RateControl` 结构体与 `init` / `on_ack` / `on_loss` / `on_timeout` 四个核心方法。 |
| [src/socket.rs](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs) | `RateControl` 的所有调用方：握手后 `init`、收 ACK 调 `on_ack`、收 NAK 调 `on_loss`、EXP 超时调 `on_timeout`，以及 `next_data_packets` 消费窗口、`cc_update` 消费间隔。 |
| [src/flow.rs](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/flow.rs) | `UdtFlow`：`on_ack` 读取的 `recv_rate` / `bandwidth` / `rtt` 都来自这里的 EWMA 平滑值（u7-l1）。 |
| [src/bin/udt_sender.rs](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/bin/udt_sender.rs) | 实践用到的 binary：每秒打印 `Period` 与 `Window`，是观察拥塞控制行为的现成仪表盘。 |

---

## 4. 核心概念与源码讲解

### 4.1 RateControl 的角色、字段与三阶段总览

#### 4.1.1 概念说明

`RateControl` 是一个**纯计算对象**：它自己不发包、不收包，只接收三类事件（被确认 `on_ack`、被报告丢包 `on_loss`、超时 `on_timeout`），据此维护两个输出量：

- `congestion_window_size`：拥塞窗口（包数），约束「同时在途多少包」。
- `pkt_send_period`：包发送间隔（`Duration`），约束「相邻包隔多久」。

发送方真正发包时必须**同时**满足这两个约束（见 u6-l1 的 `next_data_packets`：先看窗口有没有额度，再看是否到了 pacing 时刻）。可以把它理解成两条独立的「阀门」：一条管流量上限（窗口），一条管放行节奏（间隔）。

它的整个生命周期可以划分成三个阶段：

1. **慢启动（slow start）**：连接刚建立，对网络一无所知，先激进地把窗口推大，快速探测可用容量。
2. **AIMD 加性增长**：慢启动结束后，进入谨慎的线性增长，缓慢逼近链路真实容量。
3. **乘性回退**：一旦收到丢包信号（NAK）或超时，立刻收缩（乘性减小速率），然后再回到阶段 2 继续试探。

`RateControl` 用一个布尔字段 `slow_start` 标记当前是否处于阶段 1；阶段 2 和阶段 3 共享 `slow_start == false` 的代码路径，靠 `on_ack`（增长）和 `on_loss`（回退）交替驱动。

#### 4.1.2 核心流程

```text
连接建立 (握手完成)
   │
   ▼
init(): slow_start = true, congestion_window = 16, period = 1µs
   │
   ▼
┌─────────── 慢启动阶段 (on_ack 驱动) ───────────┐
│  每收到一个 ACK：congestion_window += 新确认数  │
│  直到 congestion_window > max_window_size       │
│    → slow_start = false, 设定初始 period        │
└─────────────────────────────────────────────────┘
   │
   ▼
┌─────────── AIMD 稳态 (事件驱动) ───────────┐
│  on_ack  → 按「对数增量」缩小 period (加速) │
│  on_loss → period ×1.125 (减速, 乘性回退)   │
│  on_timeout → 退出慢启动 + 重设 period      │
└─────────────────────────────────────────────┘
```

#### 4.1.3 源码精读

`RateControl` 的全部字段集中在结构体定义处，可以按用途分成「输出量」「外部输入」「阶段状态」「回退记账」四组：

[src/rate_control.rs:7-32](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/rate_control.rs#L7-L32) —— 结构体定义。前两个字段 `pkt_send_period` 与 `congestion_window_size` 就是上面说的两个输出「阀门」；`recv_rate` / `bandwidth` / `rtt` 是从 `UdtFlow` 灌入的外部输入；`slow_start` 是阶段标记；`last_dec_seq` / `nak_count` / `dec_random` / `avg_nak_num` / `dec_count` 这一串都是为 `on_loss` 的「按轮次回退」服务的记账字段（4.4 详述）。

构造函数给出关键初值：

[src/rate_control.rs:35-61](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/rate_control.rs#L35-L61) —— `new()`。注意三个初值：`congestion_window_size = 16.0`（初始窗口 16 包）、`pkt_send_period = 1µs`（几乎不加节流，让慢启动期窗口成为唯一瓶颈）、`slow_start = true`（默认进入慢启动）。`last_dec_seq` 初值用 `SeqNumber::zero() - 1`（循环减法，回绕到最大值，见 u4-l4），是为了让第一次 `on_loss` 的「新轮次」判定必然成立。

真正的「按本连接参数初始化」发生在握手完成、即将进入 `Connected` 时调用 `init`：

[src/rate_control.rs:63-77](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/rate_control.rs#L63-L77) —— `init()`。它把 MSS、`flow` 中已知的 `peer_delivery_rate` / `peer_bandwidth` / `rtt` 灌入，并把 `max_window_size` 设为 `flow.flow_window_size`（接收方通告的流量窗口）。这个 `max_window_size` 就是慢启动的退出门槛（见 4.2）。

调用点在 socket 的握手收尾处：

[src/socket.rs:194-200](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L194-L200) —— 握手完成后立即 `rate_control.init(...)`，随后才把状态置为 `Connected`。从此 `RateControl` 进入慢启动。

#### 4.1.4 代码实践

1. **实践目标**：确认 `RateControl` 是「事件驱动的纯计算对象」，且其输出被两处消费。
2. **操作步骤**：
   - 在 `src/socket.rs` 中搜索 `rate_control` 的所有出现（用 IDE 或 `grep`）。
   - 把它们分成三类：**写入端**（`init` / `on_ack` / `on_loss` / `on_timeout` / `set_*`）、**读取端**（`get_pkt_send_period` / `get_congestion_window_size`）、**持有端**（字段声明 `pub rate_control: RwLock<RateControl>`）。
3. **需要观察的现象**：写入端只有 4 个事件方法 + 几个 `set_*`；读取端集中在 `next_data_packets`（消费窗口）和 `cc_update`（消费间隔）。
4. **预期结果**：你会看到 `RateControl` 本身从不直接调用 `send`，证明它是纯决策对象；它的产物通过 `state.interpacket_interval` 和 `next_data_packets` 的窗口比较间接作用于发送。
5. 说明：本实践是源码阅读型，无需运行。

#### 4.1.5 小练习与答案

**练习 1**：`RateControl` 有哪两个「输出阀门」？它们分别约束发送的哪个维度？

**参考答案**：`congestion_window_size` 约束「同时在途未确认包数」（流量上限维度）；`pkt_send_period` 约束「相邻两包的时间间隔」（放行节奏维度）。发送方必须同时满足两者才能发包。

**练习 2**：为什么 `new()` 里要把 `last_dec_seq` 设成 `SeqNumber::zero() - 1`？

**参考答案**：`on_loss` 用 `loss_seq - last_dec_seq > 0` 判定「是否进入新一轮回退」。把 `last_dec_seq` 初始化为「0 的前一个序号」（循环回绕到最大值），能保证连接第一次发生丢包时，该差值一定为正，从而触发一次完整的新轮次乘性回退，而不是被误判为「同轮次」。

---

### 4.2 慢启动：每 ACK 加窗

#### 4.2.1 概念说明

**慢启动**是连接刚建立时的快速探测阶段。名字叫「慢」，其实增长很快——它的目标是尽快把发送速率从「零」推到接近链路容量，避免连接启动后长时间跑不满带宽。

UDT 的慢启动逻辑与 TCP 类似但更简单：**每收到一个确认了新数据的 ACK，就把拥塞窗口加上本次新确认的包数**。由于窗口越大 → 在途包越多 → 一个 RTT 内能被确认的包也越多 → 窗口增长越快，这本质上是一种**指数级增长**（大约每个 RTT 翻一倍）。

慢启动的退出条件是：拥塞窗口超过了接收方通告的流量窗口 `max_window_size`。退出时，控制器把 `slow_start` 置为 `false`，并为 `pkt_send_period` 设定一个初始值（此前它一直是 1µs，几乎不起作用），正式进入 AIMD 阶段。

#### 4.2.2 核心流程

```text
on_ack(ack) 到达，且距上次增长 >= rc_interval (10ms)
   │
   ├─ 若 slow_start == true:
   │     congestion_window += (ack - last_ack)   # 加上本次新确认的包数
   │     last_ack = ack
   │     若 congestion_window > max_window_size:  # 超过接收方通告窗口
   │         slow_start = false
   │         根据 recv_rate 或 BDP 设定初始 pkt_send_period
   │
   ├─ (若仍在慢启动) 直接 return，不做 AIMD 增长
   │
   └─ 否则进入 AIMD 路径 (见 4.3)
```

关键约束：慢启动期 `pkt_send_period` 维持 1µs，**窗口是唯一的瓶颈**；只有退出慢启动、`period` 被赋予真实值后，pacing 节流才开始起作用。

#### 4.2.3 源码精读

慢启动分支在 `on_ack` 的开头：

[src/rate_control.rs:111-131](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/rate_control.rs#L111-L131) —— `on_ack` 入口与慢启动分支。

逐句说明：

- L114-L118：节流门。距上次速率增长不足 `rc_interval`（即 `SYN_INTERVAL = 10ms`，见 [src/socket.rs:25](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L25)）就直接返回，避免每个 ACK 都调整一次（过于频繁）。
- L121：`congestion_window_size += (ack - self.last_ack) as f64`。`(ack - last_ack)` 是循环序列号减法（u4-l4），返回 `i32`，表示「本次 ACK 比上次又新确认了多少包」。窗口加上这个数——这就是「每 ACK 加窗」。
- L123-L130：退出慢启动的判定与初始 `period` 的设定。退出后，若接收方上报了 `recv_rate > 0`，则 `period = 1s / recv_rate`（即按接收方速率等速发送）；否则用 BDP 估计 `period = (rtt + rc_interval) / congestion_window`。

退出后紧跟一个早返回：

[src/rate_control.rs:137-139](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/rate_control.rs#L137-L139) —— 若仍在慢启动则 `return`，不执行后面的 AIMD 增长。这保证了慢启动期**只长窗口、不缩间隔**，两个阶段互不干扰。

#### 4.2.4 代码实践

1. **实践目标**：观察慢启动期 `congestion_window` 从 16 快速增长、`pkt_send_period` 维持 1µs 的现象。
2. **操作步骤**：
   - 在两个终端分别运行 `cargo run --bin udt_receiver` 与 `cargo run --bin udt_sender`。
   - 关注 sender 每秒打印的 `Window`（`get_congestion_window_size`）与 `Period`（`get_pkt_send_period`）。
3. **需要观察的现象**：连接建立后的最初几秒，`Window` 数值明显大于初始的 16 并持续变化；`Period` 在慢启动期应接近 `1µs`（或极小值），退出慢启动后才会跳变到由 `recv_rate` / BDP 决定的真实值。
4. **预期结果**：能直观看到「窗口先快速爬升、随后节奏接管」的两段式行为。
5. **待本地验证**：回环（127.0.0.1）链路极快、几乎不丢包，慢启动可能在第一秒内就结束，需要快速观察首秒输出；若要放大该阶段，可人为限制发送速率或增大 MSS 观察边界。

#### 4.2.5 小练习与答案

**练习 1**：为什么说 UDT 的慢启动窗口增长是「指数级」的？

**参考答案**：因为 `congestion_window += (ack - last_ack)`，而「本次新确认的包数」本身正比于当前窗口大小（窗口越大，一个 RTT 内能确认的包越多）。窗口增长率 ∝ 当前窗口 → 窗口随时间指数增长（约每 RTT 翻倍）。

**练习 2**：慢启动退出后，`pkt_send_period` 有哪两种可能的取值方式？分别依赖什么？

**参考答案**：若接收方上报了 `recv_rate > 0`，则 `period = 1s / recv_rate`（直接采用接收方的送达速率作为发送速率）；否则 `period = (rtt + rc_interval) / congestion_window`（用带宽时延积 BDP 反推：把当前窗口的包均摊到一个 RTT 上发送）。

---

### 4.3 on_ack 增窗：AIMD 的「加性增长」与对数增量公式

#### 4.3.1 概念说明

慢启动结束后，控制器进入 **AIMD 的加性增长（Additive Increase）**阶段。这一阶段的核心问题：已知链路剩余带宽有限，如何**缓慢、安全地**把发送速率推高，去试探还有多少余量？

`on_ack` 在 AIMD 阶段的策略是：**每过一个控制间隔（`rc_interval = 10ms`），就根据当前剩余带宽，把发送速率往上加一点**。它操作的直接对象是 `pkt_send_period`（间隔），但本质上是在做「速率上的加性增长」——后面会用一行代数证明这点。

这里最精巧的是**对数增量公式**：可用带宽越大，增长步长越大，但步长只随带宽的**数量级**（十进制阶）变化，不会无节制地暴涨。这是一种「胆大心细」的设计：带宽充足时敢快速加码，但又用对数把上限锁住，避免一次过冲。

注意 AIMD 阶段对窗口的处理与 TCP 不同：UDP 拥塞窗口在这里**不是**线性增长，而是**每个 ACK 直接根据接收方上报重算**为一个 BDP 估计（见源码精读）。真正的「增长」体现在 `pkt_send_period` 的缩小上。

#### 4.3.2 核心流程

```text
on_ack 进入 AIMD 路径 (slow_start == false)
   │
   1. 重算拥塞窗口 = recv_rate × (rtt + rc_interval) + 16   # BDP 估计
   │
   2. 若 loss 标志为真 (上次增长后发生过丢包):
   │     清掉 loss 标志, return —— 丢包后这一轮不增长
   │
   3. 计算「可用带宽」 b:
   │     b = bandwidth - 当前发送速率(1/period)
   │     若刚回退过且 b 过大: b = bandwidth / 9   # 限幅, 防过冲
   │
   4. 由 b 算「增量」 increase (对数公式):
   │     inc = 10^ceil(log10(b·mss·8)) · 1.5e-6 / mss, 下限 MIN_INC=0.01
   │
   5. 更新 period:
   │     period' = (period · rc_interval) / (period · inc + rc_interval)
   │     # 等价于: 速率 R' = R + inc/rc_interval  (加性增长)
```

#### 4.3.3 源码精读

AIMD 路径开头先把拥塞窗口重设为 BDP 估计：

[src/rate_control.rs:132-135](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/rate_control.rs#L132-L135) —— 拥塞窗口重算：`congestion_window = recv_rate × (rtt + rc_interval) + 16`。`recv_rate`（包/秒）乘以 `(rtt + rc_interval)`（约一个 RTT，秒）得到「在途包数」估计，再加 16 的余量。注意这与慢启动的「累加」完全不同——AIMD 期窗口是**被接收方反馈直接定义**的，不靠自己增长。

丢包后跳过一次增长的守卫：

[src/rate_control.rs:141-144](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/rate_control.rs#L141-L144) —— 若 `self.loss` 为真（自上次增长后发生过丢包，由 `on_loss` 置位），清掉标志并 `return`。含义：刚回退过的这一轮不再增长，给网络喘息时间。

核心的对数增量计算：

[src/rate_control.rs:146-159](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/rate_control.rs#L146-L159) —— `b` 与 `increase` 的计算。逐项拆解：

- L146：`b = bandwidth - 1/period`。`bandwidth` 是链路容量估计（包/秒），`1/period` 是当前发送速率（包/秒），所以 `b` 是**剩余可用带宽**（包/秒）。
- L147-L149：限幅。若 `period > last_dec_period`（当前间隔比上次回退时还大，即当前速率比刚回退后还慢）且 `bandwidth/9 < b`，则把 `b` 钳到 `bandwidth/9`。含义：刚经历过回退、速率还没恢复时，不要把「可用带宽」估得过高，限制单步增长幅度，防止立刻又把链路打满。
- L150-L159：对数增量公式。`b` 为正时：

\[ \text{inc} = \frac{10^{\,\lceil \log_{10}(b \cdot \text{mss} \cdot 8)\rceil} \times 1.5\times10^{-6}}{\text{mss}} \]

  其中 `b · mss · 8` 把「包/秒」换算成「比特/秒」（mss 单位是字节，×8 得比特），`log10(...).ceil()` 取其十进制阶数，`10^阶数` 是按数量级跳变的步长基数，`1.5e-6` 是经验系数，最后 `/mss` 换回「包/秒」单位。下限 `MIN_INC = 0.01`。`b ≤ 0`（没有剩余带宽）时直接用 `MIN_INC`。

最后用经典 UDT 公式更新间隔：

[src/rate_control.rs:160-163](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/rate_control.rs#L160-L163) —— 间隔更新。设 `S = pkt_send_period`、`RC = rc_interval`、`inc = increase`：

\[ S' = \frac{S \cdot RC}{S \cdot \text{inc} + RC} \]

**为什么这是「加性增长」？** 记发送速率 \(R = 1/S\)（包/秒），对上式两边取倒数：

\[ \frac{1}{S'} = \frac{S\cdot\text{inc} + RC}{S\cdot RC} = \frac{\text{inc}}{RC} + \frac{1}{S} \quad\Longrightarrow\quad R' = R + \frac{\text{inc}}{RC} \]

也就是说，**每过一个控制间隔，发送速率就增加 `inc/RC`（包/秒）**——在速率空间里这是纯粹的加性增长。控制器操作的是间隔 `S`，但设计意图是让速率 `R` 线性增长，公式只是把这一意图翻译到了「间隔」坐标系。

> 说明：以上 `inc/RC` 的代数推导属于本讲义对源码公式的分析，并非项目源码中的代码或注释。

#### 4.3.4 代码实践

1. **实践目标**：手工验证对数增量公式与「速率加性增长」的等价关系，并理解 `b` 的含义。
2. **操作步骤**：
   - 假设某时刻 `bandwidth = 10000`（包/秒）、`pkt_send_period = 200µs`（即当前速率 5000 包/秒）、`mss = 1500`。
   - 计算 `b = 10000 - 1/0.0002 = 10000 - 5000 = 5000`（包/秒）。
   - 代入增量公式：`b·mss·8 = 5000 × 1500 × 8 = 6×10⁷`（比特/秒）；`log10(6×10⁷) ≈ 7.778`，`ceil = 8`；`inc = 10⁸ × 1.5e-6 / 1500 ≈ 0.1`（包/秒）。
   - 用 `R' = R + inc/RC` 验证：`RC = 0.01s`，故每 10ms 速率增加 `0.1/0.01 = 10` 包/秒。
3. **需要观察的现象**：可用带宽 `b` 越大，`inc` 的阶数越高，单步速率增量越大；但 `inc` 只随 `b` 的数量级跳变，不会随 `b` 线性暴涨。
4. **预期结果**：手工计算结果与「`b` 充足时增长较快、但被对数锁住上限」的直觉一致。
5. **待本地验证**：上述数值为示例输入，非程序实测；如需对照，可在 `on_ack` 的 L159 后临时加日志打印 `b` 与 `inc`（注意只读探索，勿提交改动）。

#### 4.3.5 小练习与答案

**练习 1**：`b = bandwidth - 1/period` 中的 `1/period` 代表什么？为什么 `b` 可以理解为「剩余可用带宽」？

**参考答案**：`1/period` 是当前发送速率（包/秒）。`bandwidth` 是链路容量估计（包/秒）。两者之差 `b` 就是「链路还能再多吃多少包/秒」，即剩余可用带宽。`b` 越大说明越有余量，增量公式就给出越大的增长步长。

**练习 2**：为什么增量公式要对 `b` 取 `log10` 再 `ceil`，而不是直接用 `b` 本身？

**参考答案**：取 `log10` 并向上取整让步长只随 `b` 的**数量级**变化（10、100、1000……），而不是随 `b` 线性增长。这样既能在带宽充足时给出较大的步长（快速试探），又把单步增长幅度限制在一个对数量级内，避免一次性加码过猛把链路打满、立刻引发丢包。

**练习 3**：为什么说 `on_ack` 的间隔更新公式等价于「速率的加性增长」？

**参考答案**：设间隔 `S`、速率 `R = 1/S`，由更新公式 `S' = S·RC/(S·inc+RC)` 两边取倒数可得 `R' = R + inc/RC`。即每个控制间隔 `RC` 内速率恒定增加 `inc/RC`，这正是 AIMD 中「Additive Increase」的数学表达。

---

### 4.4 on_loss 回退：AIMD 的「乘性递减」、avg_nak_num、dec_random 与 on_timeout

#### 4.4.1 概念说明

加性增长会一直把速率往上推，直到撞上链路真实容量——这时路由器队列溢出，数据包被丢弃，接收方经 NAK 把丢包信息回报发送方（见 u6-l3）。`on_loss` 就是收到 NAK 后的**乘性递减（Multiplicative Decrease）**：果断把发送间隔放大、速率缩小，给网络泄压。

UDT 的 MD 实现有三个关键设计，本模块逐一拆解：

1. **乘性因子 1.125**：每次回退把 `pkt_send_period` 乘以 1.125，即速率缩小到原来的 \(1/1.125 \approx 0.889\)（约 11% 的下降）。相比 TCP 的「窗口减半」，UDT 的回退更温和。
2. **按「轮次」回退 + `avg_nak_num`**：不是每个 NAK 都触发一次回退。控制器把丢包划分成「轮次」，每个轮次只做一次完整回退；并用滑动平均 `avg_nak_num` 估计「一个轮次里通常来几个 NAK」。
3. **`dec_random` 随机化**：在同一轮次的后续 NAK 中，只有每 `dec_random` 个才再次回退，而 `dec_random` 是 `[1, avg_nak_num]` 上的随机数。这一随机化是 UDT 公平性的关键——当多条 UDT 连接共享同一瓶颈链路时，它们会同时看到丢包；若都同步回退，就会一起涨、一起跌、互相抢占。`dec_random` 让各连接以不同概率回退，打破同步，使带宽趋于公平分配。

最后，`on_timeout` 处理的是比 NAK 更严重的信号——**长时间收不到任何反馈**（EXP 超时，见 u7-l3）。它的处置与 `on_loss` 进入时的「退出慢启动 + 重设 period」部分一致，但不做 1.125 回退（回退留给 `on_loss`）。

#### 4.4.2 核心流程

```text
on_loss(loss_seq) 到达
   │
   ├─ 若仍在慢启动: 退出慢启动, 用 recv_rate 或 BDP 重设 period
   │
   ├─ 置 loss = true (标记本增长周期内发生过丢包)
   │
   ├─ 判定「新轮次」: loss_seq - last_dec_seq > 0 ?
   │    │
   │    ├─ 是新轮次 (本次丢包的序号在上一回退点之后):
   │    │     period ×= 1.125                      # 乘性递减
   │    │     avg_nak_num = ⌈0.875·avg + 0.125·nak_count⌉  # EWMA
   │    │     nak_count = 1, dec_count = 1
   │    │     last_dec_seq = curr_snd_seq_number
   │    │     dec_random = rand(1..=avg_nak_num)   # 随机化
   │    │
   │    └─ 是同轮次 (dec_count <= 5 时才计):
   │          dec_count += 1, nak_count += 1
   │          若 nak_count % dec_random == 0:
   │              period ×= 1.125, last_dec_seq = curr_snd_seq_number
   │
   ▼
on_timeout() (EXP 超时, 无任何反馈)
   │
   └─ 退出慢启动 + 用 recv_rate 或 BDP 重设 period (不做 1.125 回退)
```

#### 4.4.3 源码精读

`on_loss` 入口先处理「从慢启动中被拽出来」的情况：

[src/rate_control.rs:166-175](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/rate_control.rs#L166-L175) —— 若当前还在慢启动，立刻 `slow_start = false`，并按 `recv_rate > 0` 与否重设 `pkt_send_period`（与 4.2 慢启动退出时的逻辑一致：优先用接收方速率，否则用 BDP）。也就是说，丢包会强制结束慢启动。

接着置 `loss` 标志（[src/rate_control.rs:177](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/rate_control.rs#L177)），让下一个 `on_ack` 跳过一次增长（见 4.3.3 的 L141-L144）。

**新轮次的完整回退**——本模块核心：

[src/rate_control.rs:178-191](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/rate_control.rs#L178-L191) —— 逐句说明：

- L178：`(loss_seq - last_dec_seq) > 0` 判定新轮次。`loss_seq` 是本次 NAK 报告的丢失序号；若它落在「上一次回退时发送到的序号」`last_dec_seq` 之后，说明这是新一轮拥塞引发的丢包，而非旧一轮的余波。
- L179：`last_dec_period = pkt_send_period`，记下回退前的间隔，供 4.3 的限幅判断（L147）使用——避免回退后立刻又过快涨回。
- L180：`pkt_send_period *= 1.125`。**乘性递减**：间隔变大，速率 \(R \to R/1.125 \approx 0.889R\)。
- L181-L182：`avg_nak_num` 的滑动平均 `⌈0.875·avg + 0.125·nak_count⌉`。系数 7/8 与 1/8（与 u7-l1 的 EWMA 一致），用上一轮的 `nak_count` 更新「每轮平均 NAK 数」的估计。
- L183-L185：重置本轮计数器 `nak_count = dec_count = 1`，并把 `last_dec_seq` 推进到当前已发送序号 `curr_snd_seq_number`（标记新一轮的起点）。
- L187-L191：`dec_random` 的随机化。若 `avg_nak_num == 0` 则取 1；否则在 `[1, avg_nak_num]` 上均匀随机取一个整数。这就是「同轮次内每多少个 NAK 才再回退一次」的随机步长。

**同轮次的稀疏回退**：

[src/rate_control.rs:192-201](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/rate_control.rs#L192-L201) —— 同一轮次（`loss_seq` 落在 `last_dec_seq` 之前）的后续 NAK：`dec_count += 1`，仅在前 5 次（`dec_count <= 5`）内才计数 `nak_count += 1`，且只有 `nak_count % dec_random == 0` 时才再次 `period *= 1.125` 并推进 `last_dec_seq`。这保证同一拥塞事件不会被每个 NAK 反复惩罚，且因 `dec_random` 是随机的，不同连接的回退节奏互不同步。

**超时处理 `on_timeout`**：

[src/rate_control.rs:208-218](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/rate_control.rs#L208-L218) —— 仅退出慢启动 + 按 `recv_rate` / BDP 重设 `period`，**不做** 1.125 回退。它由 `check_timers` 在 EXP 超时（长时间无任何反馈）时调用，调用点见 [src/socket.rs:966](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L966)；判定条件与 EXP 退避详见 u7-l3。注意它和 `on_loss` 进入慢启动分支的唯一差别：`on_loss` 之后还会接着做 1.125 回退，`on_timeout` 不会。

`on_loss` 在 socket 中的调用点（NAK 分支）：

[src/socket.rs:605-612](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L605-L612) —— 收到 NAK 后先 `rate_control.on_loss((nak.loss_info[0] & 0x7fff_ffff).into())`（取第一个丢失条目、剥掉最高位区间标记，见 u6-l3 的 NAK 编码），再 `cc_update()` 把新间隔同步到 `state.interpacket_interval`，最后解析全部 `loss_info` 写入 `snd_loss_list` 触发重传。注意 `on_loss` 只用第一个丢失序号做拥塞控制判定——回退是对「事件」的反应，一次就够。

#### 4.4.4 代码实践

1. **实践目标**：理解 `dec_random` 如何避免多连接同步回退，并用 `udt_sender` 观察丢包时的 `Period` 跳变。
2. **操作步骤**：
   - **源码分析（必做）**：设三条 UDT 连接 A、B、C 共享同一瓶颈，`avg_nak_num` 稳定在 4。每条连接在 `on_loss` 新轮次时各自独立调用 `rand::thread_rng().gen_range(1..=4)`，得到各自的 `dec_random`（比如 A=1、B=2、C=4）。回答：在随后到来的同一批 NAK 中，A、B、C 各自的回退频率分别是多少？（答案：A 每个 NAK 都回退、B 每两个、C 每四个）——三条连接回退节奏错开，避免锁步。
   - **运行观察（待本地验证）**：在两个终端运行 `cargo run --bin udt_receiver` 与 `cargo run --bin udt_sender`，盯着每秒打印的 `Period`。若能人为注入丢包（如在 Linux 上用 `tc netem loss 1%` 作用于回环或网卡），观察注入丢包瞬间 `Period` 是否出现 `×1.125` 量级的跳变（间隔变大）。
3. **需要观察的现象**：丢包发生时 `Period` 立即变大（约 ×1.125），随后在 `on_ack` 的加性增长下缓慢回落——形成经典的「锯齿」波形。
4. **预期结果**：在无丢包的纯回环环境中，`Period` 可能长期保持平稳、看不到回退（因为根本没有 NAK 触发 `on_loss`）；要看到 MD 行为通常需要人为制造丢包。
5. **待本地验证**：本机回环极少丢包，MD 的「锯齿」现象需配合 `tc netem` 等工具注入丢包才能稳定复现。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `on_loss` 用 `(loss_seq - last_dec_seq) > 0` 来判定「新轮次」，而不是简单地对每个 NAK 都回退？

**参考答案**：一次拥塞事件通常会引发一批（多个序号的）丢包，从而产生多个 NAK。若每个 NAK 都回退一次，发送速率会被同一事件反复惩罚、暴跌过头。用「本次丢失序号是否越过上一次回退点 `last_dec_seq`」来判定新轮次，可保证每个拥塞事件只触发一次完整的乘性回退，同轮次的后续 NAK 只做稀疏的、受 `dec_random` 调节的额外回退。

**练习 2**：`dec_random` 的随机化对「多连接公平性」起什么作用？

**参考答案**：当多条 UDT 连接共享同一瓶颈时，它们会同时观察到丢包、同时收到 NAK。若所有连接的回退规则完全相同，就会同步回退、同步增长，导致带宽被「集体抢—集体让」地振荡，某些连接长期占优、另一些饥饿。`dec_random` 让每条连接独立地随机决定「每几个 NAK 才再回退一次」，使各连接的回退节奏彼此错开、去相关，从而让带宽趋于公平分配。

**练习 3**：`on_loss` 和 `on_timeout` 都会「退出慢启动 + 重设 period」，它们的差别在哪？

**参考答案**：`on_loss` 在重设 period 之后，还会（在新轮次里）继续执行 `period *= 1.125` 的乘性回退，并更新 `avg_nak_num` / `dec_random` 等记账字段；`on_timeout` 只做「退出慢启动 + 按 recv_rate/BDP 重设 period」，不做 1.125 回退。也就是说，NAK（显式丢包）触发 MD，而 EXP 超时（长时间无反馈）只把速率退回到一个保守估计，把回退的「惩罚」留给后续可能的 NAK。

---

## 5. 综合实践

把本讲三个阶段串起来，做一次端到端的「拥塞控制行为观察」。

**任务**：用 `udt_sender` / `udt_receiver` 作为仪表盘，记录一次完整传输中 `Window` 与 `Period` 随时间的变化曲线，并在图上标出三个阶段的分界。

**步骤**：

1. 启动两端：`cargo run --bin udt_receiver` 与 `cargo run --bin udt_sender`（分别见 [src/bin/udt_receiver.rs](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/bin/udt_receiver.rs) 与 [src/bin/udt_sender.rs](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/bin/udt_sender.rs)），其中 sender 每秒打印的指标见 [src/bin/udt_sender.rs:34-41](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/bin/udt_sender.rs#L34-L41)。
2. 把每秒输出的 `Window`（`get_congestion_window_size`，来自 [src/rate_control.rs:83-85](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/rate_control.rs#L83-L85)）和 `Period`（`get_pkt_send_period`，来自 [src/rate_control.rs:79-81](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/rate_control.rs#L79-L81)）记录成时间序列，画成折线图。
3. 在图上尝试标注：
   - **慢启动段**：`Window` 从 16 起快速爬升、`Period` 维持极小值（1µs 量级）。
   - **AIMD 段**：`Window` 趋稳（跟随 BDP）、`Period` 缓慢减小（加性增长）。
   - **回退段**（若能制造丢包）：`Period` 出现 ×1.125 的跳升。
4. 结合本讲源码，解释每个拐点分别由 `init` / `on_ack` 慢启动分支 / `on_ack` AIMD 公式 / `on_loss` 中的哪一段产生。

**预期结果**：你能用本讲学到的三个阶段，把 sender 打印的数值曲线「对号入座」地解释清楚。

**待本地验证**：纯回环环境很难看到丢包回退段；若要完整复现三阶段（尤其是 MD 锯齿），建议在 Linux 上用 `tc qdisc add ... netem loss 0.5%` 给链路注入少量随机丢包后再观察。

## 6. 本讲小结

- `RateControl` 是事件驱动的纯计算对象，靠 `on_ack` / `on_loss` / `on_timeout` 三个事件维护两个输出：拥塞窗口 `congestion_window_size` 与包间隔 `pkt_send_period`，分别约束「在途包数」和「放行节奏」。
- **慢启动**：每收到 ACK 就 `congestion_window += 新确认包数`，窗口近似指数增长；当窗口超过接收方通告的 `max_window_size` 时退出慢启动，并为 `period` 设定初值（优先 `1s/recv_rate`，否则 BDP）。
- **AIMD 加性增长**：`on_ack` 在 AIMD 阶段把窗口重算为 BDP 估计，并用对数增量公式缩小 `period`；间隔更新公式 `S' = S·RC/(S·inc+RC)` 在数学上等价于「速率每个控制间隔增加 `inc/RC`」，即加性增长。
- **乘性回退**：`on_loss` 在新轮次里 `period *= 1.125`（速率降到约 0.889 倍），并用 `avg_nak_num` 的 EWMA、`dec_random` 的随机化让同一拥塞事件只回退有限次、且使多连接回退去相关，提升公平性。
- `on_loss` 用 `(loss_seq - last_dec_seq) > 0` 判定新轮次，避免对同一批 NAK 反复惩罚；`on_timeout` 与之类似地退出慢启动 + 重设 period，但不做 1.125 回退。
- `RateControl` 的输入来自 u7-l1 的 `UdtFlow`（`recv_rate` / `bandwidth` / `rtt`），输出被 u6-l1 的 `next_data_packets`（窗口限流）与 `cc_update`（间隔同步）消费，三者构成完整的「估→控→发」闭环。

## 7. 下一步学习建议

- 接着读 **u7-l3（定时器：EXP、keep-alive 与超时重传）**：本讲多次提到 `on_timeout` 由 `check_timers` 在 EXP 超时时调用，下一讲会完整讲清 EXP 指数退避、keep-alive、超时重传以及 Broken 判定，把 `on_timeout` 的触发条件补全。
- 回顾 **u6-l1（socket 发送主流程）** 与 **u6-l4（ACK2 与 RTT 测量）**：前者展示 `RateControl` 的两个输出如何作用于 `next_data_packets` 的窗口与节奏限流；后者讲清 `rtt` / `rtt_var`（本讲 BDP 公式与 EXP 判定的输入）的两条测量路径。
- 进阶阅读：可对照 UDT 原始论文（Gu & Grossglauser, *UDT: UDP-based Data Transfer*）中的速率控制公式，理解本讲 `inc` 与 `S'` 公式的理论来源，体会 tokio-udt 在忠实复刻与 Rust 实现之间的取舍。
