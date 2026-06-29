# 发送流与波形生成

## 1. 本讲目标

上一讲（u2-l5）我们读了 `stream.hpp` 的接口契约：`stream_args_t`、`rx_streamer`/`tx_streamer` 两个抽象流器，以及「`channels` 元素数 == 流器通道数 == 每次收发要传的缓冲数」这条不变量。本讲从**接收侧**翻到**发送侧**，学完后你应当能够：

- 说清 `tx_streamer::send` 的阻塞行为、分片（fragmentation）规则与返回值含义；
- 写出一个正确的 `tx_metadata_t`，理解 `has_time_spec`、`start_of_burst`、`end_of_burst` 三个标志如何驱动「立即发 / 定时发 / 突发起止」；
- 看懂 `tx_waveforms` 里那张预先生成的波形表（wavetable），理解它如何用一个长度为 8192 的复数表「子采样」出任意基带频率；
- 认识发送侧的「欠流（underflow）」异步事件，知道它和接收侧「溢出（overflow）」是对称的两个问题，以及如何用 `recv_async_msg` 把它取出来。

本讲承接 u2-l5 的接口层，往下走一层到「示例程序怎么用这些接口」；之后再往下的 u2-l8 会把发送侧的定时能力用到多设备同步上。

---

## 2. 前置知识

在进入源码前，先建立三组直觉。

### 2.1 发送 = 主机把样本「喂」进设备

接收是「设备产生样本，主机来取」，发送刚好相反：**主机把样本推进设备的发送 FIFO，设备按采样率节拍消费它们**。这就引出一条贯穿全讲的对称性：

| | 接收侧（u2-l6） | 发送侧（本讲） |
|---|---|---|
| 数据方向 | 设备 → 主机 | 主机 → 设备 |
| 缓冲可写性 | `ref_vector<void*>`（可写） | `ref_vector<const void*>`（只读） |
| 主机太慢的后果 | **溢出 overflow**：FIFO 被设备写满，丢样本 | **欠流 underflow**：FIFO 被设备读空，发送中断 |
| 错误上报通道 | `recv` 的 `rx_metadata_t.error_code` | 异步消息 `async_metadata_t.event_code` |

记住这张表，本讲大半内容都是它的展开。

### 2.2 什么是「突发（burst）」

UHD 把一次发送看作一串**样本包**。一串包可以被打上两个边界标志：

- `start_of_burst`（SOB）：这串的第一个包；
- `end_of_burst`（EOB）：这串的最后一个包。

连续发送（如 `tx_waveforms` 一直发）只有一个 SOB 和一个 EOB，中间无数个包都没有标志。脉冲发送（如 `tx_bursts` 发一串、停一会、再发一串）则有多个 SOB/EOB 对。这个概念后面会反复出现。

### 2.3 时间戳定时的直觉

UHD 设备内部维护一个以采样时钟计数的硬件时间戳。给发送包打上 `has_time_spec=true` 加一个未来时刻，设备就会**等到那个时刻**才把这一包的第一个样本送上天线。这就是「定时发送」。它的用途是让多个通道/多台设备在同一时刻开始发射（MIMO）。注意：通常只有一串包的**第一个**包需要定时，后续包连续跟上即可。

---

## 3. 本讲源码地图

| 文件 | 角色 |
|---|---|
| `host/include/uhd/stream.hpp` | 发送侧的「接口契约」：`tx_streamer` 抽象类、`send()` 纯虚函数签名、`recv_async_msg()`。本讲的 API 真相都在这里。 |
| `host/include/uhd/types/metadata.hpp` | `tx_metadata_t`（发送元数据）与 `async_metadata_t`（异步事件，含欠流/ACK 事件码）的结构体定义。 |
| `host/examples/wavetable.hpp` | 波形表 `wave_table_class`：在内存里预生成一段复数波形，供发送循环反复读取。 |
| `host/examples/tx_waveforms.cpp` | **本讲主角**：一个完整的连续发送示例，串起了波形表 → `get_tx_stream` → `send` 循环 → 收尾 EOB。 |
| `host/examples/tx_bursts.cpp` | 对照样本：脉冲式发送，演示 SOB/EOB 分片与 `recv_async_msg` 等待突发 ACK（含欠流消息处理）。 |

> 提示：`stream.cpp`（u2-l5 已读过）里只有空的虚析构函数，`send()` 的真正实现由各设备驱动以虚函数分派提供。本讲只讲「怎么调用」，不讲各驱动的内部实现。

---

## 4. 核心概念与源码讲解

### 4.1 波形表 wavetable：发送的数据从哪里来

#### 4.1.1 概念说明

发送总得有数据可发。真实应用里样本可能来自文件、网络或 DSP 计算；而 `tx_waveforms` 是个自检/演示程序，它选择**在主机内存里预先生成一张固定波形表**，然后让发送循环不断重复读这张表。这样做有两个好处：

1. 不依赖外部数据源，开箱即用；
2. 发送循环只做「读表 + 喂缓冲」，CPU 开销极小，便于压满吞吐、暴露欠流问题。

`wave_table_class` 支持 4 种波形：`CONST`（直流）、`SQUARE`（方波）、`RAMP`（锯齿）、`SINE`（复正弦）。一个关键设计是：**表里只存「一个周期」的样本，靠改变读取步长来产生不同频率**——而不是为每个频率重新生成整张表。

#### 4.1.2 核心流程

波形表的生成与使用分两步：

1. **建表**：构造 `wave_table_class(type, ampl)`，生成一个长度为 `N = 8192` 的 `std::complex<float>` 数组。对 `SINE`，数组第 `i` 个元素是
   \[ a\,e^{\,j\,2\pi i/N},\quad i=0,\dots,N-1 \]
   即复平面上整整转一圈。`CONST/SQUARE/RAMP` 只填实部 I，虚部 Q 留 0（它们是幅度调制信号，不是相位调制）。

2. **子采样取频率**：发送循环每发一个样本，就把表内索引前进 `step`，并按 `N` 取模。这样产生的数字频率为
   \[ f_{\text{out}} = \frac{\text{step}}{N}\cdot f_s \]
   其中 \(f_s\) 是采样率。反过来，想产生目标频率 \(f_{\text{wave}}\)，就取
   \[ \text{step} = \mathrm{round}\!\left(\frac{f_{\text{wave}}}{f_s}\cdot N\right) \]

   `step` 越大，每拍在表里跳得越远，输出频率越高；`step=0` 则输出直流。`CONST` 因为整张表都是同一个值，`step` 取多少都不影响输出。

这两条公式直接对应 `tx_waveforms.cpp` 里的两行代码，下面精读时会指出来。

#### 4.1.3 源码精读

表的长度是编译期常量：

[host/examples/wavetable.hpp:16](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/wavetable.hpp#L16) —— `wave_table_len = 8192`，即上面公式里的 \(N\)。

构造函数按类型填表。以 `SINE` 为例：

[host/examples/wavetable.hpp:52-63](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/wavetable.hpp#L52-L63) —— 用 `ampl * exp(J * tau * i / N)` 直接生成复正弦的一整圈（`J` 是虚数单位，`tau = 2π`）。注意 `CONST/SQUARE/RAMP` 的分支只动实部 I，Q 保持 0。

取样本靠函数调用运算符，带取模：

[host/examples/wavetable.hpp:69-72](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/wavetable.hpp#L69-L72) —— `operator()(index)` 返回 `_wave_table[index % N]`。`% N` 保证索引永远合法，天然实现「表尾接表头」的循环。

`step` 的计算在 `tx_waveforms.cpp` 主函数里：

[host/examples/tx_waveforms.cpp:231-234](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/tx_waveforms.cpp#L231-L234) —— 构造表 `wave_table(wave_type, ampl)`，再用 `std::lround(wave_freq / get_tx_rate() * wave_table_len)` 算出 `step`，初值 `index = 0`。这正是上面第二条公式。

`tx_waveforms` 还做了两道与公式直接相关的合法性检查：

[host/examples/tx_waveforms.cpp:282-287](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/tx_waveforms.cpp#L282-L287) —— 第一条 `abs(wave_freq) > rate/2` 是奈奎斯特检查（频率不能超过半采样率）；第二条 `rate/abs(wave_freq) > N/2` 是「表太短」检查（每个波形周期的样本数超过 N/2 时，`step` 会小到 0 或 1，子采样太粗）。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：用纸笔验证 `step` 公式真的能产生想要的频率。
2. **步骤**：
   - 取一组典型参数：\(f_s = 10\,\text{MSps}\)，\(f_{\text{wave}} = 1\,\text{MHz}\)，\(N = 8192\)。
   - 手算 `step = round(1e6 / 10e6 × 8192) = round(819.2) = 819`。
   - 代回输出频率公式：\(f_{\text{out}} = 819/8192 × 10\,\text{MHz} \approx 0.99975\,\text{MHz}\)。
3. **需要观察的现象**：由于 `step` 必须是整数，输出频率与目标频率之间有一个很小的量化误差；这正是上面「奈奎斯特/表太短」之外的第三种失配来源。
4. **预期结果**：误差约 \(-250\,\text{Hz}\)，相对 \(1\,\text{MHz}\) 可忽略；但若把 \(f_{\text{wave}}\) 设得非常小（接近第二条检查的边界），量化误差会急剧放大。**待本地验证**：你可以在有硬件时用频谱仪看实际谱线位置。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `CONST/SQUARE/RAMP` 只填实部 I、虚部 Q 留 0，而 `SINE` 是复数（I、Q 都有）？

**参考答案**：前三者是「幅度调制」信号——用一个实数值描述瞬时幅度，对应到基带就是只有 I 分量、Q 为 0。`SINE` 是「相位调制」信号，需要复正弦 \(e^{j2\pi f t}\) 才能在基带表示一个正频率的纯正弦（实正弦会同时产生 ±f 两条谱线）。源码注释在 [wavetable.hpp:24-25](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/wavetable.hpp#L24-L25) 明确说了这一点。

**练习 2**：若把 `wave_table_len` 改成 1024，对生成的正弦波有什么影响？

**参考答案**：表变短意味着频率分辨率变粗（`step` 的量化步长变大），同样的 `wave_freq` 量化误差变大；同时「表太短」检查会更容易触发，能表达的最小频率提高。表长不影响 CONST（所有元素相同）。

---

### 4.2 tx_metadata_t：时间戳与突发标志

#### 4.2.1 概念说明

`send()` 每次调用除了带走一段样本缓冲，还带走一个 `tx_metadata_t md`——它**不**描述这次数据本身（样本格式由 `stream_args` 决定），而是描述「这批样本该怎么发」：立刻发还是定时发？是不是一串突发的开头/结尾？设备会把这些标志翻译成线上包头部的对应比特位。

注意它和接收侧 `rx_metadata_t` 的一个根本区别：`rx_metadata_t` 里有一大堆 `error_code`（TIMEOUT/OVERFLOW…），因为接收是「设备主动、主机被动」，出错由设备上报；而 `tx_metadata_t` **没有任何错误码字段**——发送是「主机主动」，主机的错误（欠流）走另一条「异步消息」通道，我们放到 4.4 讲。

#### 4.2.2 核心流程

`tx_metadata_t` 只有 4 个常用字段，用法是一个小型状态机：

```
准备一串突发发送：
  第一包:  has_time_spec = true,  time_spec = 未来某时刻,  SOB = true,  EOB = false
  中间包:  has_time_spec = false,                          SOB = false, EOB = false
  最后一包:has_time_spec = false,                          SOB = false, EOB = true
  (可选)空尾包: nsamps=0, EOB=true  → 只发一个结尾标志，不带数据
```

关键规则：
- `has_time_spec=false` → 立即发送，样本被尽快推进 FIFO；
- `has_time_spec=true` → 定时发送，设备等到 `time_spec` 才发；通常**只给一串包的第一个包**定时，后续包 `has_time_spec=false` 紧跟；
- SOB/EOB 是给设备 DSP 的「突发边界」提示，配合分片规则使用（见 4.3）。

#### 4.2.3 源码精读

结构体定义很紧凑：

[host/include/uhd/types/metadata.hpp:162-197](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/types/metadata.hpp#L162-L197) —— `tx_metadata_t` 只有 `has_time_spec`、`time_spec`、`start_of_burst`、`end_of_burst` 四个核心字段（外加高级用法 `eov_positions`）。注意它**没有** `error_code`，印证了 4.2.1 的说法。

`tx_waveforms` 里构造元数据的真实写法：

[host/examples/tx_waveforms.cpp:411-415](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/tx_waveforms.cpp#L411-L415) —— 第一包：`start_of_burst=true`、`has_time_spec=true`、`time_spec = get_time_now() + 0.1s`。也就是说「从现在起 100ms 后开始发射」，给 MIMO 多通道留出对齐裕量。

`tx_bursts` 给出了脉冲发送里「SOB→连续→EOB」的完整状态翻转会更清楚，我们放到 4.4 一起看。

#### 4.2.4 代码实践（源码阅读型）

1. **目标**：对比「连续发送」与「脉冲发送」两种模式下元数据标志的差异。
2. **步骤**：打开 `tx_bursts.cpp`，阅读它每次突发的元数据设置。
3. **观察重点**：注意它在「最后一包」把 `end_of_burst` 置 true 的时机，以及为什么后续包要把 `has_time_spec` 和 `start_of_burst` 都清零。
4. **预期结果**：你能用一句话描述「定时只标第一包、SOB 只标第一包、EOB 只标最后一包」这条规则。具体代码见 [tx_bursts.cpp:230-253](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/tx_bursts.cpp#L230-L253)。

#### 4.2.5 小练习与答案

**练习 1**：如果把一串连续发送里**每个**包都设 `has_time_spec=true` 并递增 `time_spec`，会发生什么？这是个好习惯吗？

**参考答案**：功能上能发出去（每个包都定时），但这通常**不是**好习惯。原因：① 主机算 `time_spec` 有抖动，逐包定时反而引入相位不连续；② 设备已经能按采样率自动连续播放，只需给首包定个起点即可。正确做法是首包定时、后续包 `has_time_spec=false` 让设备自由衔接，正是 `tx_waveforms`/`tx_bursts` 的写法。

**练习 2**：`tx_metadata_t` 为什么没有 `error_code` 字段，而 `rx_metadata_t` 有？

**参考答案**：接收是设备驱动的，设备随时可能报告 TIMEOUT/OVERFLOW/序列错误等，这些必须随数据包返回，所以放进 `rx_metadata_t`。发送是主机驱动的，主机推样本时不会立刻知道设备端是否欠流——欠流是设备事后通过异步消息回告的，所以走单独的 `async_metadata_t` 通道（见 4.4），而不是塞进每次 `send` 的返回值里。

---

### 4.3 tx_streamer::send 与发送主循环

#### 4.3.1 概念说明

`tx_streamer::send` 是发送侧的主入口，和接收侧的 `recv` 对称。它是一个**纯虚函数**（接口），签名固定在 `stream.hpp`；具体怎么把样本打成 VRT 包、走哪条传输链路发出去，由各设备驱动的派生类实现（参见 u4-l2 传输层、u4-l3 VRT 包）。

调用方只需要记住它的**契约**：阻塞、自动分片、返回实际发送数。

#### 4.3.2 核心流程

`send` 一次调用的内部行为：

```
send(buffs, nsamps_per_buff, md, timeout):
    if nsamps_per_buff > 单包最大样本数:
        自动拆成多个包发送（fragmentation）
        拆包时遵守 burst 标志：SOB 只能落在第一个分片，EOB 只能落在最后一个分片
    阻塞，直到全部样本被读出各缓冲，或超时
    return 实际发出的样本数（超时可能 < nsamps_per_buff）
```

缓冲 `buffs` 的类型是 `ref_vector<const void*>`——一个「只读指针的引用数组」，元素数等于流器通道数（u2-l5 的不变量）。一个常用技巧：**让多个通道的指针都指向同一块缓冲**，就能让所有通道发射相同波形。

整个 `tx_waveforms` 的发送主线可以画成这样一个状态流转：

```
[构造设备 multi_usrp::make]
        │
        ▼
[选子设备 → 配置射频(速率/频率/增益/带宽/天线)]
        │
        ▼
[建波形表 + 算 step]  ←  4.1
        │
        ▼
[get_tx_stream(stream_args)]  ←  u2-l5
        │
        ▼
[分配缓冲 buff，预填波形，多通道指针都指向 buff]
        │
        ▼
[设 md：SOB=true, has_time_spec=true, time=now+0.1]  ←  4.2
        │
        ▼
┌─────►[循环顶部：检查 stop / 累计样本达标？]──── 跳出 ──┐
│      │ 否                                            │
│      ▼                                               │
│   [send(buffs, spb, md)]  ←  本节                    │
│      │                                               │
│      ▼                                               │
│   [用 wave_table(index+=step) 重填 buff]             │
│      │                                               │
│      ▼                                               │
│   [md.SOB=false, md.has_time_spec=false]  ───────────┘  后续包不再定时
│
▼ (跳出循环)
[发空尾包 send("",0,md) 且 md.EOB=true]
        │
        ▼
[Done]
```

注意循环里第一次 `send` 之后立刻把 `SOB` 和 `has_time_spec` 都清零——这正对应 4.2 的「只首包定时/首包 SOB」规则。

#### 4.3.3 源码精读

`send` 的接口契约（重点读它的文档注释）：

[host/include/uhd/stream.hpp:376-407](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/stream.hpp#L376-L407) —— 注意三件事：① 自动分片且「SOB 只落首个分片、EOB 只落末个分片」；② 阻塞调用，超时时返回值可能小于请求量；③ **非线程安全**（同一流器不可多线程并发 send，但不同流器可以）。返回值是「实际发出样本数」。

发送缓冲类型，只读指针：

[host/include/uhd/stream.hpp:374](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/stream.hpp#L374) —— `typedef ref_vector<const void*> buffs_type;`，对比接收侧的 `ref_vector<void*>`，`const` 体现了「发送缓冲只读、接收缓冲可写」。

构造流器与缓冲（`tx_waveforms` 主线）：

[host/examples/tx_waveforms.cpp:289-300](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/tx_waveforms.cpp#L289-L300) —— `stream_args("fc32", otw)` 设定 CPU 格式 fc32（`complex<float>`）与线上 otw 格式；`get_tx_stream` 拿到流器；若用户没指定 `--spb`，则取 `get_max_num_samps()*10` 作为缓冲大小（10 倍单包，减少 send 次数、提升吞吐）。`buffs` 是「每通道一个指针、全部指向同一块 `buff`」的多通道同波形技巧。

发送主循环：

[host/examples/tx_waveforms.cpp:419-440](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/tx_waveforms.cpp#L419-L440) —— 每轮：检查停止/达标 → `send(buffs, buff.size(), md)` → 用波形表重填缓冲 → 把 `SOB` 和 `has_time_spec` 清零（仅首包之后生效）。

循环结束后的「空尾包」：

[host/examples/tx_waveforms.cpp:443-444](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/tx_waveforms.cpp#L443-L444) —— `md.end_of_burst=true; send("", 0, md);` 发一个**不带数据、只带 EOB 标志**的包，明确告诉设备「这串发送到此结束」。对于需要等突发 ACK 的设备，这一步是触发 ACK 收尾的必要动作。

#### 4.3.4 代码实践（本讲主实践）

这是本讲指定的实践任务：**基于 `tx_waveforms` 修改波形类型与幅度，观察发送行为；若无硬件，绘制发送主循环的状态流转图。**

**有硬件版本**：

1. **目标**：直观感受不同波形与幅度对发射谱的影响。
2. **步骤**：
   - 在构建目录编译示例（`tx_waveforms` 由 `host/examples/CMakeLists.txt` 的示例循环统一编译，参见 [CMakeLists.txt:21-45](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/CMakeLists.txt#L21-L45)）。
   - 连续发 Const：`tx_waveforms --args="addr=<你的设备>" -f 2.4e9 -r 10e6 --wave-type CONST --wave-ampl 0.3`
   - 改成正弦：加 `--wave-type SINE --wave-freq 1e6`
   - 把幅度推到上限：`--wave-ampl 0.7`（程序默认上限 0.7，见 [tx_waveforms.cpp:143-144](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/tx_waveforms.cpp#L143-L144)）。
3. **需要观察的现象**：CONST 应在载频 2.4 GHz 处看到一根单谱线；SINE 在 ±1 MHz 处出现对称谱线（复基带上变频后）；幅度增大时谱线抬高，逼近 0.7 时可能开始出现非线性削顶失真。
4. **预期结果**：谱线位置与 4.1 算出的 `f_out` 吻合。**待本地验证**（依赖硬件与频谱仪/另一台 USRP 接收）。

**无硬件版本（源码阅读型）**：

1. **目标**：不看上面我画的状态图，自己照着源码把发送主循环的状态流转画出来。
2. **步骤**：只读 [tx_waveforms.cpp:411-444](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/tx_waveforms.cpp#L411-L444)，列出：① 首包 md 的三个标志初值；② 循环体内 md 哪两个字段在首包后被清零；③ 退出循环后发了什么「特殊包」。
3. **预期结果**：你画出的图应与 4.3.2 的状态流转一致，并能解释「为什么首包之后要清掉 `has_time_spec`」。

#### 4.3.5 小练习与答案

**练习 1**：`tx_waveforms` 里 `buffs` 把所有通道指针都设成 `&buff.front()`。如果想让两个通道发**不同**波形，要怎么改？

**参考答案**：为每个通道各开一块缓冲（如 `std::vector<std::complex<float>> buff0, buff1;`），让 `buffs = {&buff0.front(), &buff1.front()}`，分别用不同的 `wave_table` 或不同 `step` 填充。注意 `send` 第二参 `nsamps_per_buff` 是「每缓冲」样本数，两块缓冲长度必须相同。

**练习 2**：为什么 `send` 是阻塞的，而且 `tx_waveforms` 还要把 `spb` 设成 `get_max_num_samps()*10` 这么大？

**参考答案**：`send` 阻塞意味着主机推样本的速度受限于设备消费速度（即采样率），这天然起到节流作用、防止主机把 FIFO 灌爆。`spb` 取 10 倍单包，是为了让每次 `send` 多带些样本、减少系统调用与上下文切换次数，在高速率下降低 CPU 占用、减少欠流风险。代价是延迟略增。

---

### 4.4 欠流与异步消息：underflow 的去向

#### 4.4.1 概念说明

发送侧最典型的错误是**欠流（underflow）**：设备按采样率节拍消费 FIFO，但主机供样本供得不够快，FIFO 被读空了。结果是发送出现「断流」，对应 `async_metadata_t::EVENT_CODE_UNDERFLOW`。

回顾 2.1 的对称表：欠流之于发送，正如溢出之于接收。但二者上报方式不同：

- 接收溢出：随数据包返回，塞进 `rx_metadata_t.error_code`，`recv` 调用当场就能看到（u2-l6）；
- **发送欠流：事后异步回告**，进 `tx_streamer` 的异步消息队列，必须**主动调用 `recv_async_msg`** 才能取出来。

这就引出一个重要事实：**`tx_waveforms` 从头到尾没有调用 `recv_async_msg`**——它对欠流「视而不见」，欠流消息会一直堆在队列里不被消费。对纯连续发送的自检程序这无所谓；但生产代码通常需要单独开一个线程排空异步队列、统计欠流次数。`tx_bursts` 就演示了如何处理异步消息。

#### 4.4.2 核心流程

异步消息的处理模式（以 `tx_bursts` 等待突发 ACK 为例）：

```
发完一串突发（最后一个包带 EOB）
        │
        ▼
设备消费完这串样本后，回告一个 EVENT_CODE_BURST_ACK（突发完成确认）
        （若中途 FIFO 读空，则先回告 EVENT_CODE_UNDERFLOW）
        │
        ▼
主机循环调用 tx_stream->recv_async_msg(async_md, timeout):
   - 返回 true  → 取到一条异步消息，查 async_md.event_code
   - 返回 false → 超时，队列空
        │
        ▼
按 event_code 计数/告警：
   EVENT_CODE_BURST_ACK            突发成功完成
   EVENT_CODE_UNDERFLOW            FIFO 读空（断流）
   EVENT_CODE_UNDERFLOW_IN_PACKET  包内欠流
   EVENT_CODE_SEQ_ERROR            主机到设备丢包
   EVENT_CODE_TIME_ERROR           定时包迟到
```

`tx_bursts` 的逻辑是：发完突发后，循环读异步消息，**只数 ACK**，遇到欠流等其它消息跳过（因为欠流消息可能排在 ACK 前面，必须把队列排空才能拿到 ACK）。

#### 4.4.3 源码精读

异步事件码定义：

[host/include/uhd/types/metadata.hpp:216-233](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/types/metadata.hpp#L216-L233) —— 重点看 `EVENT_CODE_UNDERFLOW = 0x2`（FIFO 读空）、`EVENT_CODE_BURST_ACK = 0x1`（突发完成确认）、`EVENT_CODE_UNDERFLOW_IN_PACKET = 0x10`（包内欠流）、`EVENT_CODE_TIME_ERROR = 0x8`（定时包迟到）。

取异步消息的接口：

[host/include/uhd/stream.hpp:415-416](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/stream.hpp#L415-L416) —— `recv_async_msg(async_metadata, timeout)`，返回 `true` 表示取到一条有效消息，`false` 表示超时队列空。

`tx_bursts` 处理异步消息的真实代码：

[host/examples/tx_bursts.cpp:272-283](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/tx_bursts.cpp#L272-L283) —— 注释明说「队列里可能混有欠流消息」，所以它用 `while` 一直读直到数够 `channel_nums.size()` 个 ACK。如果只发不读，欠流消息会无限堆积。

`tx_bursts` 里 SOB/EOB 翻转的完整片段（补全 4.2 的承诺）：

[host/examples/tx_bursts.cpp:229-253](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/tx_bursts.cpp#L229-L253) —— 首包 SOB=true + 定时；后续包 `has_time_spec=false`、`SOB=false`；只有当 `samps_to_send <= spb`（最后一拍）时才把 `EOB=true`。这是「定时/SOB 只标首包、EOB 只标末包」规则的标准实现。

#### 4.4.4 代码实践（源码阅读型）

1. **目标**：理解欠流为什么是「异步」的，以及 `tx_waveforms` 与 `tx_bursts` 在欠流处理上的根本差别。
2. **步骤**：
   - 在 `tx_waveforms.cpp` 全文搜索 `recv_async_msg`（用 `Grep`），确认它**不存在**。
   - 在 `tx_bursts.cpp` 找到 `recv_async_msg` 调用，阅读它如何区分 ACK 与欠流。
3. **需要观察的现象**：思考——如果用 `tx_waveforms` 长时间连续发送且主机供样本过慢，欠流消息会去哪？
4. **预期结果**：欠流消息会堆在流器的异步队列里无人读取；队列有上限，满了之后新的欠流事件会被丢弃（计数的欠流数会偏低）。这就是为什么生产代码应当起一个后台线程持续 `recv_async_msg` 排空队列。

#### 4.4.5 小练习与答案

**练习 1**：欠流（underflow）和接收侧的溢出（overflow）是「同一个问题的两面」。请说明它们各自的物理成因。

**参考答案**：溢出（接收）——设备产生样本比主机 `recv` 快，设备/传输 FIFO 被写满，丢样本；欠流（发送）——设备消费样本比主机 `send` 快，发送 FIFO 被读空，发射中断。两者都是「主机处理速度跟不上采样率节拍」，方向相反。

**练习 2**：为什么 `tx_waveforms` 不读异步消息也能正常工作，而 `tx_bursts` 必须读？

**参考答案**：`tx_waveforms` 是连续发送，欠流只是「短暂断流后会自然续上」，程序不关心是否欠流，所以可忽略异步队列。`tx_bursts` 是脉冲发送，它需要知道「这一串突发到底有没有完整发完」才能决定下一步（比如是否重发、是否推进时间），而「突发完成」这个信号只通过 `EVENT_CODE_BURST_ACK` 异步回告，所以必须 `recv_async_msg`。

---

## 5. 综合实践

把本讲四个模块串起来，完成下面这个「最小可用的脉冲发送器」设计任务（源码阅读 + 伪代码，无需硬件）：

**任务**：参考 `tx_bursts`，写一段伪代码，实现「每 0.5 秒发一串 10000 样本的 SINE 突发，并统计每串的欠流次数」。要求你的伪代码：

1. 用 `wave_table_class("SINE", ampl)` 建表并算出 `step`（用到 4.1）；
2. 正确设置首包 `md`（SOB + 定时）与末包 EOB（用到 4.2）；
3. 在发送循环里调用 `send` 并处理「实际发送数 < 请求」的情况（用到 4.3）；
4. 发完后用 `recv_async_msg` 排空队列，区分并计数 `EVENT_CODE_UNDERFLOW` 与 `EVENT_CODE_BURST_ACK`（用到 4.4）。

**参考伪代码**（示例代码，非项目原有）：

```cpp
// 示例代码：综合实践的参考答案骨架
const wave_table_class wave("SINE", 0.3f);
const size_t step = std::lround(wave_freq / rate * wave_table_len);

uhd::stream_args_t args("fc32", "sc16");
args.channels = channel_nums;
auto tx = usrp->get_tx_stream(args);
const size_t spb = tx->get_max_num_samps();
std::vector<std::complex<float>> buff(spb);
std::vector<std::complex<float>*> buffs(channel_nums.size(), &buff.front());

double t_send = 1.5; // 首串定时
do {
    uhd::tx_metadata_t md;
    md.start_of_burst = true;  md.has_time_spec = true;
    md.time_spec = uhd::time_spec_t(t_send); // 4.2 首包定时

    size_t acc = 0;
    while (acc < total_num_samps) {
        size_t n = std::min(spb, total_num_samps - acc);
        if (acc + n == total_num_samps) md.end_of_burst = true; // 4.2 末包 EOB
        for (size_t i = 0; i < buff.size(); ++i) buff[i] = wave(index += step); // 4.1 填表
        size_t sent = tx->send(buffs, n, md, 1.0);                // 4.3 send
        if (sent < n) { /* 发送超时/欠流风险，记录 */ }
        md.has_time_spec = false; md.start_of_burst = false;      // 后续包清零
        acc += sent;
    }
    t_send += 0.5;

    // 4.4 排空异步队列，统计欠流
    uhd::async_metadata_t amd;
    size_t underflows = 0, acks = 0;
    while (acks < channel_nums.size() && tx->recv_async_msg(amd, 1.0)) {
        if (amd.event_code == uhd::async_metadata_t::EVENT_CODE_BURST_ACK)   ++acks;
        if (amd.event_code == uhd::async_metadata_t::EVENT_CODE_UNDERFLOW)   ++underflows;
    }
    std::cout << "underflows this burst: " << underflows << std::endl;
} while (!stop_signal_called && repeat);
```

**自检要点**：把这段骨架与真实 `tx_bursts.cpp`（[tx_bursts.cpp:228-284](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/tx_bursts.cpp#L228-L284)）逐行对照，确认你的版本在每个标志的翻转时机上与官方示例一致。

---

## 6. 本讲小结

- 发送是接收的镜像：主机主动把样本推进设备 FIFO，缓冲只读（`ref_vector<const void*>`），主机供不上时的「欠流 underflow」对应接收侧的「溢出 overflow」。
- `wave_table_class` 用一张 8192 点的表存一个周期，靠整数步长 `step = round(f_wave/f_s × N)` 子采样出任意基带频率；CONST/SQUARE/RAMP 只填实部，SINE 是复正弦。
- `tx_metadata_t` 只有 `has_time_spec`/`time_spec`/`start_of_burst`/`end_of_burst` 四个常用字段，**没有错误码**；定时与 SOB 只标首包，EOB 只标末包。
- `tx_streamer::send` 阻塞、自动分片（SOB/EOB 自动落在首/末分片）、返回实际发送数，非线程安全；`tx_waveforms` 用「多通道指针指向同一缓冲」让所有通道发同波形。
- 发送循环退出后要发一个 `send("",0,md)` 的空尾包带 EOB，作为突发的干净收尾。
- 欠流等错误**异步**回告，必须主动 `recv_async_msg` 读取；`tx_waveforms` 不读（连续发送可忽略），`tx_bursts` 读（脉冲发送需等 BURST_ACK，且欠流消息会混在队列里需排空）。

---

## 7. 下一步学习建议

- **横向**：把本讲的发送侧与 u2-l6 的接收侧对照重读一遍，把那张「溢出/欠流对称表」彻底内化，这是理解所有 SDR 收发程序的关键。
- **纵向（同步）**：本讲的「首包定时」能力（`has_time_spec` + `time_spec`）是多设备同步的基石。下一讲 **u2-l8 多设备同步与时钟/PPS** 会把它和 `set_time_unknown_pps`、10 MHz 参考、GPSDO 传感器组合起来，实现多台 USRP 的相位/时间对齐。
- **深入底层**：若想知道 `send` 内部如何把 `complex<float>` 打包成线上 VRT 包并发出去，可跳到 **u4-l1 样本格式转换 convert**（cpu↔otw 转换）和 **u4-l3 VRT 包协议**（包头格式与批量收发）。
