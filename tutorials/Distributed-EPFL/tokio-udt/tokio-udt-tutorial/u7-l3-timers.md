# 定时器：EXP、keep-alive 与超时重传

## 1. 本讲目标

UDT 建立在不可靠的 UDP 之上，因此「什么时候该再发一次 ACK」「对端多久没回话算掉线」「迟迟收不到确认的数据要不要重传」这些问题，都不能依赖内核，必须由库自己用定时器来回答。本讲只剖析一个函数——`UdtSocket::check_timers`，它是这些时间相关决策的总入口。

学完本讲，你应当能够：

- 说清 `check_timers` 的三段结构：ACK 定时、light ACK、EXP 定时。
- 解释 ACK 定时器与 light ACK（每 64 包）各自的触发条件。
- 掌握 EXP 定时器如何用 `exp_count` 让等待间隔逐次放大，并据此判定对端无响应。
- 理解超时重传时如何把「未确认区间」整段塞进 `snd_loss_list`，以及何时把连接置为 `Broken`。

本讲是 u7 拥塞控制单元的收尾，承接 u7-l2（RateControl）的 `on_timeout` 与 u6-l2（接收与 ACK）的 `send_ack`。

## 2. 前置知识

在进入源码前，先建立三个直觉。

**直觉一：UDT 没有内核定时器，全靠「轮询」。** `check_timers` 并不是一个被操作系统定期唤醒的回调，而是接收 worker 在每次处理完包之后、以及一个每 100ms 轮转一次的定时器队列里**主动调用**的普通 async 函数。也就是说，时间判断是「到点检查 + 自己决定要不要动作」，而不是「操作系统叫醒我」。这一点决定了它的写法：每次进来都用 `Instant::now()` 和若干「下次该动作的时刻」做比较。

**直觉二：`last_rsp_time` 是 EXP 一切的锚点。** 它记录「最近一次收到对端任何响应（数据包或控制包）的时刻」。EXP（Heartbeat / Expiration）定时器本质就是拿「现在」和这个锚点比，间隔越拉越长——拉到一定程度，要么发心跳探活，要么重传，要么判死。

**直觉三：心跳与超时重传是「同一类事件、两个分支」。** 当 EXP 到点，要看发送缓冲里有没有未确认数据：没有就发一个 keep-alive 探活；有就把整段未确认数据当作丢失塞进重传队列。换句话说，「我没事但想确认你还活着」和「我有事但收不到你的确认」由同一段代码分两种情况处理。

涉及的前序术语（已在 u6/u7 建立）：`send_ack`（light / full，见 u6-l2）、`snd_loss_list`（见 u6-l3）、`on_timeout`（见 u7-l2）、`SocketState` 簿记容器（见 u3-l2）。

## 3. 本讲源码地图

本讲几乎全部围绕 `src/socket.rs` 展开，辅以少量字段定义与调用方：

| 文件 | 作用 |
| --- | --- |
| `src/socket.rs` | `check_timers`、`cc_update`、`send`、`process_ctrl`、`process_data` 的主体，以及 `SYN_INTERVAL` 等常量 |
| `src/state/socket_state.rs` | `SocketState` 中所有定时器相关字段及其初值 |
| `src/rate_control.rs` | `get_ack_pkt_interval` / `get_ack_period` / `on_timeout`，决定 ACK 节拍与超时后的速率重置 |
| `src/queue/rcv_queue.rs` | 唯一调用 `check_timers` 的两处：收包后与定时器轮转队列 |

## 4. 核心概念与源码讲解

### 4.1 check_timers 的总览与触发方式

#### 4.1.1 概念说明

`check_timers` 是一个「被动轮询型」的时间驱动函数。它不是被定时器中断触发的，而是被接收 worker 主动调用。每被调用一次，它就检查三类定时器是否到期，按需发送 ACK / light ACK / keep-alive，或触发超时重传 / Broken 判定。

它一次性承担了两个方向的责任：

- **接收方向**：ACK 定时器与 light ACK，负责「我收到了数据，该回确认了」。
- **发送方向**：EXP 定时器，负责「我发出去的数据迟迟没被确认，对端是不是出问题了」。

之所以能合并成一个函数，是因为每条 `UdtSocket` 既可能收也可能发，而每段逻辑都用「现在时刻 vs. 某个下次动作时刻」来判断，互不冲突。

#### 4.1.2 核心流程

`check_timers` 每次被调用的执行顺序是固定的三段：

1. **同步发送节奏**：调用 `cc_update()`，把 RateControl 当前的 `pkt_send_period` 同步到 `SocketState.interpacket_interval`。
2. **ACK 定时段**：判断是否到了发 full ACK 的时刻（或达到包数阈值）；若没到，再判断是否该发一次 light ACK。
3. **EXP 定时段**：计算下一次 EXP 到期时刻，若已到期，则根据发送缓冲是否为空，分别走 keep-alive 或超时重传分支，并视情况判定 Broken。

这三段都是「先算时刻、再比 now」，互不阻塞，一次调用最多触发一次 full ACK 或一次 light ACK、最多触发一次 EXP 动作。

#### 4.1.3 源码精读

函数入口与第一段（同步节奏）：

[check_timers 入口与 cc_update(src/socket.rs:887-889)](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L887-L889)

`cc_update` 自身只是一个薄同步：

[cc_update：把发送间隔同步进 state(src/socket.rs:882-885)](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L882-L885)

三段涉及的关键常量定义在文件顶部：

[三个时间常量：SYN_INTERVAL / MIN_EXP_INTERVAL / PACKETS_BETWEEN_LIGHT_ACK(src/socket.rs:25-27)](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L25-L27)

它们的含义是：

- `SYN_INTERVAL = 10ms`：ACK 的基础节拍，也参与 EXP 间隔计算。
- `MIN_EXP_INTERVAL = 300ms`：EXP 单次等待间隔的下限，防止 RTT 极小时 EXP 退避过快。
- `PACKETS_BETWEEN_LIGHT_ACK = 64`：每收 64 个包插一次 light ACK。

**调用点**只有两处，都在接收 worker 里。第一处在处理完一个数据/控制包之后立刻调用，让定时器随业务流量被「捎带」推进：

[收包后立即 check_timers(src/queue/rcv_queue.rs:184-186)](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/rcv_queue.rs#L184-L186)

第二处是一个 100ms 粒度的定时器轮转队列，保证即使没有流量进来，每条连接也会被周期性地检查一次（这对 keep-alive / 超时判定至关重要）：

[定时器轮转队列：每 100ms 推进一次 check_timers(src/queue/rcv_queue.rs:200-220)](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/rcv_queue.rs#L200-L220)

其中轮转间隔与收包退避常量是：

[轮转间隔 100ms、空收退避 30µs(src/queue/rcv_queue.rs:18-19)](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/rcv_queue.rs#L18-L19)

每次被检查后，socket 会被 `update` 重新追加到队尾，形成「最近被照顾的排到后面」的公平轮转：

[update：把 socket 重新排到队尾(src/queue/rcv_queue.rs:48-52)](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/rcv_queue.rs#L48-L52)

> 小结：`check_timers` 是「被轮询的纯检查函数」，靠 100ms 轮转队列兜底、靠收包捎带加密检查频率。理解了它的被调用方式，后面三段只是「到点比较」的细节。

#### 4.1.4 代码实践

**实践目标**：确认 `check_timers` 的「被动轮询」本质，找出它的全部调用点与触发节奏。

**操作步骤**：

1. 在 `src/` 下搜索 `check_timers`，确认只有 `src/socket.rs`（定义）与 `src/queue/rcv_queue.rs`（两处调用）出现。
2. 打开 `src/queue/rcv_queue.rs`，对比第 185 行（收包后）与第 216 行（轮转队列）两处调用的上下文差异。
3. 搜索 `TIMERS_CHECK_INTERVAL` 与 `update(`，理解「轮转队列 + 重排队尾」如何保证每条连接大约每 100ms 被检查一次。

**需要观察的现象**：两处调用的共同前置条件都是 `socket.status().is_alive()`；第 185 行多了一个 `socket.peer_addr() == Some(addr)` 的来源校验。

**预期结果**：你应能解释「为什么没有操作系统定时器，EXP 也能按时触发」——因为有 100ms 轮转队列兜底，加上收包时的捎带调用。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `TIMERS_CHECK_INTERVAL` 从 100ms 改成 1s，对 keep-alive 和超时判定会有什么影响？

**答案**：EXP 到期时刻是基于 `last_rsp_time + 间隔` 计算的，与轮转粒度无关；但轮转粒度变粗会让「真正发出 keep-alive / 触发重传」的实际时刻延后最多约 1s，使探活与重传响应变迟钝。

**练习 2**：为什么两处调用都要求 `is_alive()`？

**答案**：`check_timers` 会发包并推进状态，对已经 `Broken / Closing / Closed` 的连接继续跑定时器没有意义，甚至会向已失效的对端发包，故先过滤。

---

### 4.2 ACK 定时器与 light ACK

#### 4.2.1 概念说明

可靠传输要求接收方及时确认收到的数据。但「每收一个包就回一个 ACK」在大流量下会产生过多反向流量；「攒很久才回一个 ACK」又会让发送方误以为丢包而重传。UDT 的折中是**两档 ACK**：

- **full ACK**：携带 ACK 序号与拥塞反馈（RTT、带宽、缓冲余量等，见 u6-l2 的 `AckOptionalInfo`），会推进接收缓冲可读水位。节拍由 ACK 定时器控制，默认约每 10ms（`SYN_INTERVAL`）一次。
- **light ACK**：只通报「我已连续收到到这里」的水位，不带 ACK 序号、不推进缓冲、不算带宽，几乎零成本。在两次 full ACK 之间，每收 64 个包（`PACKETS_BETWEEN_LIGHT_ACK`）插一次。

light ACK 的意义是：在大流量下频繁地向发送方「刷」最新水位，让它尽快释放已确认的发送缓冲、推进拥塞窗口，而不必等到下一次 full ACK。

#### 4.2.2 核心流程

ACK 定时段的判断逻辑：

```
ack_interval = RateControl.get_ack_pkt_interval()   # 默认 0
if now > next_ack_time  OR  (ack_interval > 0 且 ack_interval <= pkt_count):
    发 full ACK（send_ack(false)）
    next_ack_time = now + ack_period     # ack_period = min(SYN_INTERVAL, ack_period字段)
    pkt_count = 0
    light_ack_counter = 0
else:
    若 (light_ack_counter + 1) * 64 <= pkt_count:
        发 light ACK（send_ack(true)）
        light_ack_counter += 1
```

两个触发条件是「或」关系：

- **时间触发**（默认生效）：`now > next_ack_time`。`next_ack_time` 初值是 `now + SYN_INTERVAL`（见下文字段初值），每次发完 full ACK 重置为 `now + ack_period`。
- **包数触发**（默认不生效）：`ack_interval > 0 && ack_interval <= pkt_count`。`ack_interval` 来自 `RateControl.ack_pkt_interval`，默认 0，所以这条默认不触发；只有外部通过 `set_pkt_interval` 设了非零值才启用「每收 N 包发一次 full ACK」的替代节拍。

light ACK 的节拍是「每 64 包一次」，用 `(light_ack_counter + 1) * 64 <= pkt_count` 来判断：`light_ack_counter` 记录「已经补发过几次 light ACK」，所以下一次该补发的水位是 `(已补次数+1) * 64` 包。每次 full ACK 会把 `pkt_count` 和 `light_ack_counter` 都清零，重新计数。

#### 4.2.3 源码精读

ACK 定时与 light ACK 的整段判断：

[ACK 定时器与 light ACK 判断(src/socket.rs:891-918)](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L891-L918)

注意 full ACK 分支末尾的三件套：刷新 `next_ack_time`、清零 `pkt_count`、清零 `light_ack_counter`。

`ack_interval` 与 `ack_period` 的来源：

[get_ack_pkt_interval / get_ack_period(src/rate_control.rs:87-93)](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/rate_control.rs#L87-L93)

`get_ack_period` 用 `min(SYN_INTERVAL, ack_period)` 做了上限保护，确保 ACK 间隔不会超过 10ms，即使 `ack_period` 字段被改大。

相关字段的初值（`next_ack_time = now + SYN_INTERVAL`，`pkt_count = 0`，`light_ack_counter = 0`）：

[SocketState 定时器字段初值(src/state/socket_state.rs:44-72)](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/state/socket_state.rs#L44-L72)

其中 `next_ack_time` 在第 50 行被初始化为 `now + SYN_INTERVAL`，所以连接刚建立后约 10ms 就会发出第一个 full ACK。

`pkt_count` 的累加发生在 `process_data` 里——每收到一个数据包就加 1，同时刷新 `last_rsp_time`：

[process_data：累加 pkt_count 并刷新 last_rsp_time(src/socket.rs:675-681)](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L675-L681)

`send_ack(light)` 的两条路径已在 u6-l2 详述：`light=true` 只发 4 字节水位的轻量 ACK，`light=false` 走 full ACK 并可能附带 `AckOptionalInfo`、推进缓冲、记录 `ack_window`。本段只关心它在何时被调用。

#### 4.2.4 代码实践

**实践目标**：验证「full ACK 按时间、light ACK 按 64 包」的触发分工。

**操作步骤**：

1. 打开 `src/socket.rs:891-918`，在「时间触发」「包数触发」「light ACK 触发」三处分别标注它们落在哪个条件分支。
2. 回到 `src/rate_control.rs:59`，确认 `ack_pkt_interval` 默认是 0，据此推断：默认配置下 `ack_interval > 0` 恒为假，full ACK 只靠 `now > next_ack_time` 触发。
3. 在 `process_data`（src/socket.rs:675-681）确认 `pkt_count` 的唯一累加点，思考：如果对端长时间不发数据，`pkt_count` 不会增长，light ACK 自然不会触发——这是否合理？

**需要观察的现象**：full ACK 分支会同时清零 `pkt_count` 与 `light_ack_counter`，而 light ACK 分支只递增 `light_ack_counter`。

**预期结果**：你能用一句话说清默认配置下 ACK 的节拍——「每约 10ms 一次 full ACK；两次 full ACK 之间，每收 64 个包补一次 light ACK」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 light ACK 不带 ACK 序号、不推进缓冲？

**答案**：light ACK 的唯一目的是高频刷新水位、让发送方尽快释放缓冲与推进窗口；推进接收缓冲可读水位与测量带宽都开销较大，留给约 10ms 一次的 full ACK 即可，避免在大流量下做无用功。

**练习 2**：假设 `ack_pkt_interval` 被外部设为 100，描述 full ACK 的新触发条件。

**答案**：此时包数触发条件 `ack_interval > 0 && 100 <= pkt_count` 生效，即每收 100 个包也会触发一次 full ACK，与「10ms 时间到」二者满足其一即发。

---

### 4.3 EXP 定时器：指数退避与心跳

#### 4.3.1 概念说明

EXP（Expiration）定时器负责「对端失联检测」。它的核心变量是 `exp_count`（退避计数器）和 `last_rsp_time`（最近一次收到对端响应的时刻）。

EXP 的设计哲学是「越等越久」：每触发一次 EXP 而对端仍无响应，下一次等待的间隔就更长，避免在链路真的断了之后还高频发包轰炸网络。这被称为指数退避（exponential backoff）——严格说，本实现里单次等待间隔随 `exp_count` **线性增长**（\(T \propto \text{exp\_count}\)），但累计等待时间随 `exp_count` 平方量级增长，效果上就是越往后越慢，符合退避的直觉。

每当收到对端的任何响应（控制包或数据包），`exp_count` 立即重置为 1，`last_rsp_time` 刷新为当前时刻——链路一恢复，退避立刻归零。

#### 4.3.2 核心流程

EXP 到期时刻的计算：

\[ T_{exp} = \max\bigl(N_{exp}\cdot(RTT + 4\cdot RTT_{var}) + T_{syn},\; N_{exp}\cdot T_{min}\bigr) \]

\[ t_{next} = last\_rsp\_time + T_{exp} \]

其中 \(N_{exp} = \text{exp\_count}\)，\(T_{syn} = \text{SYN\_INTERVAL} = 10\text{ms}\)，\(T_{min} = \text{MIN\_EXP\_INTERVAL} = 300\text{ms}\)。两项取 `max` 保证：RTT 极小时退避不会过快（由 300ms 下限兜底），RTT 较大时退避与 RTT 成比例（由第一项主导）。

当 `now > t_next` 时 EXP 触发，进入三分支决策（详见 4.4 节）：

```
若 exp_count > 16 且 last_rsp_time.elapsed() > 5s:   # 判死
    置 Broken，重排发送队列，返回
否则:
    若发送缓冲为空:     # 没有未确认数据
        发 keep-alive 探活
    否则:               # 有未确认数据
        把未确认区间塞进 snd_loss_list（见 4.4）
        on_timeout()    # 退出慢启动、重置发送间隔
        cc_update()     # 同步间隔
        update_snd_queue(true)   # 立即催发重传
    exp_count += 1
    last_rsp_time = now    # 既然刚发了心跳/重传，重置锚点
```

注意最后两行：触发动作后 `exp_count` 自增、`last_rsp_time` 重置为 now。这意味着下一次 EXP 的等待间隔用「更大的 exp_count」重新计算——这就是退避的来源。

#### 4.3.3 源码精读

EXP 到期时刻的计算（取两项最大值）：

[计算 next_exp_time：RTT 项与下限项取 max(src/socket.rs:920-929)](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L920-L929)

`exp_count` 与 `last_rsp_time` 的初值（`exp_count = 1`，`last_rsp_time = now`）：

[exp_count 初值为 1(src/state/socket_state.rs:56)](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/state/socket_state.rs#L56)

「收到任何响应就重置退避」的机制分两处。控制包路径（任意控制包——ACK/NAK/KeepAlive/Handshake/Shutdown——进来都重置）：

[process_ctrl：任意控制包重置 exp_count=1 与 last_rsp_time(src/socket.rs:442-447)](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L442-L447)

数据包路径（见 4.2.3 已引用的 src/socket.rs:675-681，同样刷新 `last_rsp_time`，但注意它**不**重置 `exp_count`，只刷新时间锚点）。

keep-alive 分支——发送缓冲为空时，发一个 keep-alive 控制包探活：

[EXP 触发且缓冲为空：发 keep-alive(src/socket.rs:941-951)](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L941-L951)

触发动作后的退避推进——`exp_count` 自增、`last_rsp_time` 重置：

[EXP 触发后：exp_count 自增、重置锚点(src/socket.rs:971-974)](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L971-L974)

这里有个容易踩坑的细节：`last_rsp_time` 在每次 EXP 触发后被重置为 now（注释 `Reset last response time since we just sent a heart-beat`）。所以 4.4 节判死条件里的 `last_rsp_time.elapsed() > 5s` 衡量的不是「距离对端最后一次响应过了多久」的累计时间，而是「最近这一次 EXP 等待间隔有多长」。配合 `exp_count` 线性放大的间隔，当间隔本身放大到超过 5s（约 `exp_count >= 17`，因为 \(17 \times 300\text{ms} = 5.1\text{s}\)）时，判死条件才可能成立。

> 还有一处对 `last_rsp_time` 的「主动」重置在 `send` 里：当应用层刚把新数据放入「原本为空」的发送缓冲时，会把 `last_rsp_time` 刷新为 now，注释是「delay the EXP timer to avoid mis-fired timeout」——目的是给正常的 ACK 机制留出时间，避免一塞入新数据就立刻被 EXP 误判为超时。

[send：缓冲从空到非空时推迟 EXP(src/socket.rs:1002-1005)](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L1002-L1005)

#### 4.3.4 代码实践

**实践目标**：量化 EXP 的退避节拍，验证「越等越久」。

**操作步骤**：

1. 假设 RTT 与 RTT 方差都很小，使得 `MIN_EXP_INTERVAL = 300ms` 主导 `T_exp`。手算 `exp_count = 1, 2, 3, ..., 6` 时的单次等待间隔（应为 300ms、600ms、900ms、…、1.8s）。
2. 累加前 6 次的等待时间，得到从最后一次真实响应算起、约 6.3s 内 EXP 触发的次数与节奏。
3. 在 `src/socket.rs:971-974` 确认每次触发后 `exp_count += 1` 且 `last_rsp_time = now`，据此解释「为什么下一次等待会更久」。

**需要观察的现象**：单次等待间隔随 `exp_count` 线性增长；累计等待时间按平方量级增长。

**预期结果**：你能用表格列出 `exp_count` 与对应等待间隔，并解释这是「退避」而非固定间隔。运行环境若需验证，可用 u1-l2 的 sender/receiver，在传输过程中用防火墙规则丢弃反向 ACK，观察 sender 日志（需自行加打印，属可选步骤）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `T_exp` 要取 `RTT 项` 与 `下限项` 的最大值，而不是直接用其中一项？

**答案**：RTT 项让退避与实际链路时延成正比（高速长肥管道下等待更久才合理）；下限项（300ms）在 RTT 极小（如回环）时兜底，避免退避过快导致频繁发包探活。取 max 兼顾两者。

**练习 2**：收到一个数据包会重置 `exp_count` 吗？会刷新 `last_rsp_time` 吗？

**答案**：数据包路径（`process_data`）只刷新 `last_rsp_time`，**不**重置 `exp_count`；只有控制包路径（`process_ctrl`）会把 `exp_count` 重置为 1。即「对端只要还在发包就刷新锚点，但退避计数只在收到控制信令时彻底归零」。

---

### 4.4 超时重传与 Broken 判定

#### 4.4.1 概念说明

EXP 触发后，如果发送缓冲**非空**（有未确认数据），就进入超时重传分支。它的逻辑是：既然正常的 ACK / NAK 反馈迟迟没到（可能 ACK 自己丢了，也可能数据包丢了但 NAK 也丢了），那就「假定整段未确认的数据都丢了」，把它们整段塞进 `snd_loss_list`，让发送主流程（u6-l1 的 `next_data_packets`）在下一次调度时优先重传。

这与 NAK 驱动的选择性重传（u6-l3）互补：NAK 是接收方明确报告「这几个包丢了」，而超时重传是发送方在「啥反馈都没收到」时的兜底——把 `last_ack_received` 到 `curr_snd_seq_number` 这整段未确认区间一次性标记为丢失。

注意一个重要区别（承接 u7-l2）：超时重传调用的 `on_timeout` **只退出慢启动、重置发送间隔，不做乘性回退**；而 NAK 路径的 `on_loss` 才做 `×1.125` 的乘性回退。也就是说，UDT 把「超时」当作「反馈信道暂时不可用」而非「确凿拥塞」，处理相对温和。

判死（Broken）则是失联的最终结论：当退避已经拉到很长（`exp_count > 16`）且最近一次 EXP 间隔超过 5 秒，认定对端彻底失联，置 `Broken`，由全局 GC（u3-l1）后续清理。

#### 4.4.2 核心流程

EXP 触发后的三分支决策伪代码：

```
if exp_count > 16 AND last_rsp_time.elapsed() > 5s:
    status = Broken        # 判死
    update_snd_queue(true) # 唤醒等待者
    return                 # 直接返回，不再做心跳/重传

if snd_buffer.is_empty():
    发 keep-alive          # 探活分支（4.3 已讲）
else:
    if last_ack_received != curr_snd_seq_number + 1 AND snd_loss_list.is_empty():
        snd_loss_list.insert(last_ack_received, curr_snd_seq_number)  # 整段塞入
    on_timeout()           # 退出慢启动、重置间隔（不回退）
    cc_update()            # 同步新间隔
    update_snd_queue(true) # 催发：重排发送队列立即重传

exp_count += 1
last_rsp_time = now
```

「整段塞入」的两个守卫条件缺一不可：

- `last_ack_received != curr_snd_seq_number + 1`：意思是「确实有未确认数据」。如果全部已确认，二者满足 `last_ack_received == curr_snd_seq_number + 1`，没必要重传。
- `snd_loss_list.is_empty()`：避免与 NAK 机制重复。如果丢包链表里已经有条目（说明 NAK 机制已经在处理），就不要再整段覆盖，交给既有的选择性重传即可。

#### 4.4.3 源码精读

判死分支——同时满足「退避足够久」与「最近间隔超过 5s」两个条件才置 `Broken`：

[EXP 判死：exp_count > 16 且 last_rsp_time.elapsed() > 5s(src/socket.rs:931-939)](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L931-L939)

超时重传分支——整段塞入 + `on_timeout` + 催发：

[EXP 超时重传：整段塞入 snd_loss_list 并催发(src/socket.rs:952-969)](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L952-L969)

注意第 966-968 行的三连：`on_timeout()` 改速率、`cc_update()` 同步间隔、`update_snd_queue(true)` 把 socket 重新塞回发送调度堆并要求立即重排（`true` 表示强制 reschedule）。`update_snd_queue(true)` 之后，`next_data_packets`（u6-l1）会优先从 `snd_loss_list` 取号重传。

`on_timeout` 的实现——与 `on_loss` 的关键差异：只退出慢启动、按 BDP 重置发送间隔，**没有**乘性回退：

[on_timeout：仅退出慢启动、重置间隔，不回退(src/rate_control.rs:208-218)](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/rate_control.rs#L208-L218)

可对照 u7-l2 讲过的 `on_loss`（src/rate_control.rs:166-202）——后者有 `pkt_send_period.mul_f64(1.125)` 的乘性回退，`on_timeout` 没有。

`Broken` 是 `UdtStatus` 的一态，被 `is_alive()` 排除（详见 u3-l2）。置 `Broken` 后，等接收数据的 `recv` 会返回 `BrokenPipe`，等发送缓冲腾位的 `poll_write` 会被唤醒并报错，最终由全局 GC（u3-l1 的 `garbage_collect_sockets`）异步补做 `close`。

#### 4.4.4 代码实践

**实践目标**：把「整段塞入 snd_loss_list」与「on_timeout 不回退」两个关键设计在源码里定位清楚。

**操作步骤**：

1. 打开 `src/socket.rs:952-969`，标出两个守卫条件（`last_ack_received != curr_snd_seq_number + 1` 与 `snd_loss_list.is_empty()`），并解释为什么需要 `is_empty()` 这个守卫。
2. 跟踪 `update_snd_queue(true)` → `mux.snd_queue.update(socket_id, true)`（src/socket.rs:978-982），回忆 u6-l1：`next_data_packets` 里 `snd_loss_list.pop_after` 会优先取号重传。据此说清「整段塞入后如何被重传」。
3. 对比 `src/rate_control.rs:208-218`（`on_timeout`）与 `src/rate_control.rs:166-202`（`on_loss`），确认前者没有 `mul_f64(1.125)`，体会「超时温和、丢包严厉」的策略差异。
4. 阅读 `src/socket.rs:931-939`，回答判死为何要同时要求 `exp_count > 16` 与 `last_rsp_time.elapsed() > 5s` 两个条件（提示：单靠 `exp_count > 16` 在 RTT 极大时可能误判；单靠 5s 在 RTT 极小时又过松）。

**需要观察的现象**：超时重传分支既不改 `congestion_window_size` 也不乘性放大 `pkt_send_period`，只通过 `on_timeout` 退出慢启动。

**预期结果**：你应能复述「EXP 触发 → 缓冲非空 → 整段塞入 snd_loss_list → on_timeout 温和重置 → update_snd_queue(true) 催发 → next_data_packets 优先重传」这条完整链路，并指出它与 NAK 重传（u6-l3）的分工。

#### 4.4.5 小练习与答案

**练习 1**：为什么超时重传前要检查 `snd_loss_list.is_empty()`？

**答案**：若丢包链表已有条目，说明 NAK 机制正在处理选择性重传；此时再整段插入会与既有区间重复或冲突。守卫 `is_empty()` 确保只在「完全没有反馈」时才用「整段兜底」，把精细的重传交给 NAK 路径。

**练习 2**：`on_timeout` 与 `on_loss` 对发送速率的处理有何本质区别？为什么？

**答案**：`on_loss` 把 `pkt_send_period` 乘 1.125（乘性回退），因为 NAK 是「确凿丢包」的拥塞信号；`on_timeout` 只退出慢启动并按 BDP 重置间隔、不回退，因为超时更可能是「反馈信道暂时不可用」（ACK/NAK 自己丢了）而非数据面真拥塞，处理更温和，避免在反馈丢失时过度降速。

**练习 3**：连接在什么情况下会被置为 `Broken`？置 `Broken` 后谁来收尾？

**答案**：当 `exp_count > 16` 且最近一次 EXP 等待间隔超过 5s（即退避已放大到秒级仍无任何响应），置 `Broken`。之后由全局 `Udt` 的 `cleanup_worker` 每秒跑 `garbage_collect_sockets`，对 `Broken` 连接异步调用 `close()` 完成最终清理（见 u3-l1、u8-l2）。

---

## 5. 综合实践

把本讲三段串起来，做一个「定时器全景追踪」任务。

**任务**：选取一条真实的 socket 生命周期，用一张时序图把三类定时器事件标注出来。

**步骤**：

1. 选定角色：以 u1-l2 的 `udt_sender`（发送方）为观察对象，因为它既会发数据（触发对端的 ACK 定时器），又会等 ACK（触发自己的 EXP 定时器）。
2. 在 `src/socket.rs` 的以下四个位置临时加 `eprintln!`（**示例代码**，仅供观察，勿提交）：
   - `check_timers` 入口（src/socket.rs:887），打印 `socket_id` 与 `now`。
   - full ACK 分支（src/socket.rs:895 附近）与 light ACK 分支（src/socket.rs:911 附近），各打印一次。
   - keep-alive 分支（src/socket.rs:943 附近）与超时重传分支（src/socket.rs:960 附近），各打印 `exp_count` 与是否 `snd_buffer.is_empty()`。
3. 在 `process_ctrl` 的重置点（src/socket.rs:445-446）打印 `exp_count 重置为 1`，用于观察「收到响应即归零退避」。
4. 跑通一对 sender/receiver，分三种场景观察日志：
   - **正常传输**：应看到密集的 full ACK / light ACK，几乎看不到 EXP 动作（因为 `last_rsp_time` 不断被刷新）。
   - **暂停接收**：杀掉 receiver，观察 sender 上 EXP 逐次触发、`exp_count` 线性增长，先是 keep-alive（缓冲已排空时），最终判死置 `Broken`。
   - **重启 receiver**：在判死前重启 receiver，观察 `exp_count` 被 `process_ctrl` 重置为 1，退避归零。

**预期产出**：一张时序图，横轴为时间，标注出「full ACK 节拍（~10ms）」「light ACK（每 64 包）」「EXP 退避逐次放大」「响应到达后 exp_count 归零」「判死时刻」五类事件。若本地无法运行，则改为纯源码阅读：在上述行号处手工模拟一次「receiver 突然失联」的事件序列，写出每一步 `exp_count`、`last_rsp_time`、`next_exp_time` 的变化（待本地验证运行部分）。

> 本任务需要修改源码加日志，属观察型实践；完成后请用 `git checkout -- src/socket.rs` 还原，切勿提交。

## 6. 本讲小结

- `check_timers` 是「被动轮询型」时间驱动函数，由接收 worker 在收包后与 100ms 轮转队列里两处主动调用，没有内核定时器。
- **ACK 定时段**：默认按时间触发（`now > next_ack_time`，约每 10ms 一次 full ACK）；两次 full ACK 之间，每收 64 个包补一次轻量的 light ACK。`ack_pkt_interval` 默认 0，故包数触发默认不生效。
- **EXP 定时段**：单次等待间隔 \(T_{exp} = \max(N_{exp}(RTT+4RTT_{var})+10\text{ms},\; N_{exp}\cdot300\text{ms})\)，随 `exp_count` 线性放大形成退避；收到任何响应即重置。
- EXP 触发后**按发送缓冲是否为空分两路**：空则发 keep-alive 探活；非空则把 `(last_ack_received, curr_snd_seq_number)` 整段塞入 `snd_loss_list`，调用 `on_timeout`（仅退出慢启动、不乘性回退）并催发重传。
- **判死**：当 `exp_count > 16` 且最近一次 EXP 间隔超过 5s 时置 `Broken`，交由全局 GC 收尾。
- 超时重传（`on_timeout`，温和）与 NAK 重传（`on_loss`，乘性回退）分工互补：前者是「无反馈」的兜底，后者是「确凿丢包」的精确定位。

## 7. 下一步学习建议

本讲讲完了「时间驱动」这一侧。建议接下来：

- **u8-l1（握手与 SYN cookie）**：`check_timers` 里频繁出现的 `SYN_INTERVAL` 同样是握手重传的节拍，去握手流程里看它如何被复用。
- **u8-l2（关闭、linger 与垃圾回收）**：本讲判死置 `Broken` 后的收尾就在这里——`close` 的 linger 等待与 `garbage_collect_sockets` 的两阶段回收。
- **重读 u6-l3（LossList）与 u6-l1（next_data_packets）**：带着本讲「整段塞入 snd_loss_list」的认知，回去看 `pop_after` 如何把这些区间取号重传，闭环可靠性与超时两条重传路径。
