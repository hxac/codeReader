# 传输层：UDP zero-copy / USB / DPDK / NIRIO

## 1. 本讲目标

在前序讲义中，我们看过的 `rx_streamer::recv` / `tx_streamer::send` 都是「接口契约」（u2-l5、u2-l6、u2-l7）。本讲要钻进这些接口的脚下——**传输层（transport layer）**，回答一个一直被悬置的问题：

> 主机内存里那一块块样本缓冲，到底是怎么被搬进网络数据包、搬进 USB 批量传输、搬进 PCIe DMA 的？

学完本讲你应当能够：

1. 说清 `zero_copy_if` 抽象与「帧缓冲池 + 引用计数」模型，并理解它为何被命名为 zero-copy。
2. 读懂 `udp_zero_copy` 的 boost::asio 可移植实现，以及它「其实仍有一次拷贝」的真相。
3. 读懂 `usb_zero_copy` 的 libusb 异步批量传输实现，理解它与 UDP 版的关键差异。
4. 说出 `udp_simple`、DPDK、NIRIO、以及现代 `link_if` 抽象各自的角色，并解释**设备层如何用一个工厂函数指针在它们之间做选型**。

本讲是 u4-l1（convert 子系统）的下游：convert 负责「CPU 格式 ↔ 线上格式」的数值变换，而 transport 负责「把变换好的字节搬过物理链路」。两者共同构成收发流器的真实地基。

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

**(a) 为什么需要「帧（frame）」这种抽象？**

一次以太网包最大约 1500 字节（普通帧）或 9600 字节（jumbo 帧），而一秒 1 GbE 的样本流约有 125 MB。显然不可能「一个包一个 `recv()` 系统调用」——系统调用开销会瞬间吃满 CPU。UHD 的做法是：在用户空间**预先分配一池固定大小的缓冲（帧）**，让上层一个帧一个帧地取用、归还，把系统调用频率与帧频率对齐，而不是与样本频率对齐。这就是 `buffer_pool` 与 `*_frame_size` / `num_*_frames` 这一族参数存在的理由。

**(b) 为什么叫 zero-copy？它真的零拷贝吗？**

理想中的 zero-copy 是：网卡把数据 DMA 直接写进用户态内存，应用读这块内存，全程没有 `memcpy`。UHD 的接口名叫 `zero_copy_if`，意图正是「把传输层管理的内存**直接交给**上层指针用，用完再归还」。但要注意一个关键的诚实声明：UDP 的可移植实现（boost::asio 版）**并不是真正的零拷贝**——每次收发仍有一次内核↔用户态的拷贝。这一点源码注释里写得很明白，本讲会引用并解释。真正的零拷贝收益来自「一帧可被多次复用、且与上层流器共享同一块内存」这层设计，而非字面上消灭了那次 `recv` 拷贝。

**(c) 一次收发的生命周期靠什么串起来？**

靠 `boost::intrusive_ptr`（侵入式引用计数智能指针）。传输层把一块帧内存包进 `managed_buffer`，返回它的智能指针；上层用完后指针析构、引用计数归零，自动回调 `release()` 把帧归还给池。发送侧甚至把「真正发包」这个动作推迟到 `release()` 里执行。这个「取帧 → 用 → 自动归还」的闭环是贯穿本讲的主旋律。

> 名词速查：**frame**（一帧，固定大小缓冲）、**buffer pool**（帧池）、**managed buffer**（传输层托管的帧缓冲）、**MTU**（最大传输单元，包大小上限）、**jumbo frame**（巨型帧，~9600 B）、**DMA**（直接内存访问）、**bulk transfer**（USB 批量传输）。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| `host/include/uhd/transport/zero_copy.hpp` | 传输层公共抽象：`managed_buffer`、`managed_recv_buffer`、`managed_send_buffer`、`zero_copy_if`、`zero_copy_xport_params` |
| `host/include/uhd/transport/udp_zero_copy.hpp` | UDP 版 zero-copy 接口声明与 `make` 工厂 |
| `host/lib/transport/udp_zero_copy.cpp` | UDP 版的可移植（boost::asio）实现，含收/发帧、帧池、socket 缓冲调整 |
| `host/include/uhd/transport/udp_simple.hpp` | 极简 UDP 接口（`send`/`recv` 单缓冲），用于控制与发现 |
| `host/lib/transport/udp_simple.cpp` | `udp_simple` 实现，含 connected / broadcast / UART-over-UDP 三种用法 |
| `host/include/uhd/transport/usb_zero_copy.hpp` | USB 版 zero-copy 接口声明 |
| `host/lib/transport/libusb1_zero_copy.cpp` | 基于 libusb 异步批量传输的 USB 实现 |
| `host/lib/transport/usb_dummy_impl.cpp` | 无 libusb 时的占位实现（抛 `not_implemented_error`） |
| `host/lib/include/uhdlib/transport/udp_common.hpp` | UDP 默认常量与收发包/调缓冲辅助函数（内部头） |
| `host/lib/usrp/x300/x300_eth_mgr.cpp` | 设备层选型示例：用工厂函数指针在 boost::asio 与 DPDK 之间切换 |
| `host/lib/usrp/usrp2/usrp2_impl.cpp` | 老 `udp_zero_copy` 数据通路的真实调用点 |

## 4. 核心概念与源码讲解

### 4.1 zero_copy 抽象与帧缓冲模型

#### 4.1.1 概念说明

`zero_copy_if` 是整个传输层的「统一外形」。无论底下走的是 UDP、USB 还是 PCIe，对上层都暴露同一套方法：给我一个收帧缓冲（`get_recv_buff`）、给我一个发帧缓冲（`get_send_buff`），并告诉我帧有多大、一共有多少帧。

这套抽象的核心是一个**双向的帧租约模型**：

- **取帧**：上层调用 `get_send_buff()` / `get_recv_buff()`，传输层从帧池里拿出一块内存，包成智能指针返回。
- **用帧**：上层直接在这块内存上读（收）或写（发），**不需要再拷贝到别处**——这是「zero-copy」名字的真正含义。
- **还帧**：智能指针析构时引用计数归零，自动触发 `release()`，帧回到池里可被下一次复用。

发帧侧多一个 `commit(num_bytes)`：上层往帧里写完数据后，用 `commit` 告诉传输层「我实际写了多少字节」，因为帧的容量（`frame_size`）和实际有效长度（`length`）通常不相等。

#### 4.1.2 核心流程

整个生命周期由侵入式引用计数驱动。下面这段伪代码描述了「取帧—用—还」的闭环：

```
# 发送侧
msb = transport->get_send_buff(timeout)      # 取一块发送帧，引用计数=1
memcpy(msb.cast<void*>(), my_samples, n)     # 直接写进传输层内存（零拷贝精神）
msb.commit(n)                                # 声明有效长度
# msb 离开作用域 → intrusive_ptr 析构 → release()
#   └─ send 实际发生在这里（UDP 版），然后帧归还池

# 接收侧
mrb = transport->get_recv_buff(timeout)      # 取一块接收帧（内部已 recv 填好），引用计数=1
process(mrb.cast<void*>(), mrb.size())       # 直接读传输层内存
# mrb 离开作用域 → release() → 帧归还池，可复用
```

引用计数的两个自由函数是关键开关：`intrusive_ptr_add_ref` 把计数 `+1`；`intrusive_ptr_release` 把计数 `-1`，**当计数归零时调用 `p->release()`**。注意 `release()` 是纯虚的——具体「还帧」逻辑由各传输实现自己定义（UDP 版归还 claimer，USB 版归还异步传输槽）。

#### 4.1.3 源码精读

先看抽象基类 `managed_buffer`，它承载帧内存指针与有效长度，并提供 `commit`、`cast`、`size` 三个内联工具：

[host/include/uhd/transport/zero_copy.hpp:20-84](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/transport/zero_copy.hpp#L20-L84) —— `managed_buffer` 持有 `_buffer`（裸指针）与 `_length`，`release()` 是纯虚，`commit()` 仅改写 `_length`。

引用计数的开关在这两个自由函数里，注意归零时回调 `release()`：

[host/include/uhd/transport/zero_copy.hpp:86-95](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/transport/zero_copy.hpp#L86-L95) —— `intrusive_ptr_release` 在 `--_ref_count == 0` 时调用 `p->release()`，这是「还帧」的唯一触发点。

`zero_copy_xport_params` 是创建传输时的参数包，六个字段分成「帧级」与「缓冲级」两组：

[host/include/uhd/transport/zero_copy.hpp:122-139](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/transport/zero_copy.hpp#L122-L139) —— `recv_frame_size`/`send_frame_size` 是单帧字节数，`num_*_frames` 是帧池深度，`recv_buff_size`/`send_buff_size` 是底层 socket/驱动的内核缓冲字节数。

最后是统一外形 `zero_copy_if`，所有具体传输都继承它：

[host/include/uhd/transport/zero_copy.hpp:146-197](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/transport/zero_copy.hpp#L146-L197) —— `get_recv_buff`/`get_send_buff` 返回智能指针，超时返回 null；`get_num_*_frames`/`get_*_frame_size` 让上层在开流前查询能力。

> 这套抽象还有个常被忽略的细节：`managed_recv_buffer` 与 `managed_send_buffer` 本身几乎是空类（只各自带一个 `sptr` 类型别名），它们的存在纯粹是为了**类型安全**——把「可写收帧」和「只读发帧」在类型层面区分开，避免把收帧当发帧用反。

#### 4.1.4 代码实践：跟着引用计数走一遍还帧

**实践目标**：在真实调用点确认「取帧 → 写 → commit → 自动还帧」的闭环，验证 `release()` 确实在指针析构时触发。

**操作步骤**：

1. 打开 [host/lib/usrp/usrp2/usrp2_impl.cpp:288-301](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/usrp2/usrp2_impl.cpp#L288-L301)，这是一段真实使用 `udp_zero_copy` 的代码。
2. 关注三行：`xport->get_send_buff()` 取帧 → `std::memcpy(..., send_buff->cast<void*>(), ...)` 直接写帧内存 → `send_buff->commit(sizeof(data))` 声明长度。
3. 注意 `send_buff` 是个局部 `managed_send_buffer::sptr`，函数返回时它析构。

**需要观察的现象 / 预期结果**：

- `memcpy` 的目标正是 `cast<void*>()` 返回的传输层内存指针，**没有第二块用户缓冲**——这就是 zero-copy 精神的体现。
- `commit(sizeof(data))` 把 `_length` 设成 8 字节，即使帧容量是 1472 字节。
- 指针析构 → 引用计数归零 → `release()` → 真正发包（详见 4.2）。

实际发送字节数与丢包率依赖硬件：**待本地验证**。源码阅读型实践到此即可成立。

#### 4.1.5 小练习与答案

**Q1**：为什么 `release()` 是纯虚函数，而不是在基类里直接把帧归还池？
**A**：因为「还帧」的语义随传输而异。UDP 版只需归还一个 `simple_claimer`；USB 版要把对应的异步传输槽重新提交给 libusb。基类无法预知具体动作，所以只规定接口（「该还帧了」），把动作交给子类。

**Q2**：`commit()` 与 `release()` 谁先发生？
**A**：`commit()` 先发生——用户写完帧后主动调用 `commit` 告知有效长度；`release()` 后发生——在智能指针析构、引用计数归零时由框架自动调用。两者职责不同：前者定长度，后者触发实际收发与归还。

---

### 4.2 UDP zero-copy 传输（udp_zero_copy）

#### 4.2.1 概念说明

`udp_zero_copy` 是 `zero_copy_if` 在以太网上的落地。它的接口头这样描述自己：

> 「zero copy udp transport 通过避免 `recv()`/`send()` 时的额外拷贝来高效处理数据……它把内存引用直接交给调用者。」

但紧接着实现文件里有一段诚实的注释，必须读出来：

> 「This is the portable zero copy implementation … However, it is **not a true zero copy** implementation as each send and recv requires a copy operation to/from userspace.」

换句话说，boost::asio 这版「zero-copy」省掉的不是「内核↔用户态拷贝」，而是「用户态到第二块缓冲的拷贝」——上层直接在传输层的帧内存上读写，帧可被反复复用。它的核心组件有三个：

1. **`buffer_pool`**：一块连续大内存，被切成 `num_*_frames` 个等长帧，每帧 16 字节对齐。
2. **收/发帧包装类**（`udp_zero_copy_asio_mrb` / `_msb`）：每帧配一个 `simple_claimer`（信号量式闸门），保证同一帧槽同一时刻只有一个持有者。
3. **socket 缓冲调整**：构造时把内核 socket 收/发缓冲调大到目标值，调不到就告警。

#### 4.2.2 核心流程

`udp_zero_copy::make` 的执行顺序是理解整条链路的钥匙：

1. **解析 hints**：从 `device_addr_t` 读 `recv_frame_size`/`num_recv_frames`/`recv_buff_size` 等键，缺省回退到默认值。
2. **填默认值**：帧数缺省 `UDP_DEFAULT_NUM_FRAMES`，帧大小缺省 `UDP_DEFAULT_FRAME_SIZE`（1472 = 1500 − 20 − 8），缓冲大小缺省 `max(UDP_DEFAULT_BUFF_SIZE, num_frames × MAX_ETHERNET_MTU)`。
3. **构造 impl**：开 UDP socket（`open_udp_socket`），分配收/发 `buffer_pool`，并为每帧造一个 `mrb`/`msb` 包装对象，组成两个池。
4. **调内核缓冲**：`resize_udp_socket_buffer_with_warning` 尝试把 socket 缓冲调到目标，调不到就打印告警（Linux 上给出 `sysctl net.core.rmem_max/wmem_max` 提示）。

收/发的运行期路径则藏在两个包装类里：

- **接收**：`get_recv_buff` → 轮转帧索引 → `mrb.get_new(timeout)` → `claimer.claim_with_wait`（拿不到返回 null 即超时）→ `recv_udp_packet`（先试 `MSG_DONTWAIT`，否则 `poll` 等待后阻塞 `recv`）→ 用实际收到的长度 `make(...)` 成智能指针返回。
- **发送**：`get_send_buff` → `msb.get_new` 拿到帧（`commit` 由用户稍后调用）→ 用户写数据并 `commit` → 指针析构 → `release()` 里调 `send_udp_packet`（带 `ENOBUFS` 重试）真正发包，然后 `claimer.release()`。

**关键不对称**：接收是「`get` 时就收」（`get_new` 里同步 `recv`）；发送是「`release` 时才发」。这是因为发送要等用户写完、`commit` 出长度才能发，而 `commit` 发生在 `get` 之后。

缓冲默认值有一处漂亮的工程经验公式。`UDP_DEFAULT_BUFF_SIZE = 2500000` 字节，注释写明它是「1 GbE 链路上 20 ms 的数据量」：

\[
\text{buff\_size} \approx \frac{1\,\text{Gb/s}}{8} \times 20\,\text{ms} = 125\,\text{MB/s} \times 0.02\,\text{s} = 2.5\,\text{MB} \approx 2{,}500{,}000\,\text{B}
\]

20 ms 这个量级是有意为之：它要大于一次内核调度抖动，又不能大到吃光主机内存——这是「在丢包风险与内存占用之间取平衡」的典型工程取舍。

#### 4.2.3 源码精读

接口与「not a true zero copy」的诚实声明：

[host/include/uhd/transport/udp_zero_copy.hpp:17-27](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/transport/udp_zero_copy.hpp#L17-L27) —— 头注释承诺零拷贝，并指出「若无平台特定方案，则回退到 boost::asio 包装普通 send/recv」。

`make` 工厂签名（注意它把实际缓冲大小通过出参 `buff_params_out` 回传给调用者）：

[host/include/uhd/transport/udp_zero_copy.hpp:54-58](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/transport/udp_zero_copy.hpp#L54-L58)

接收帧包装类 `udp_zero_copy_asio_mrb`——`get_new` 里同步收包：

[host/lib/transport/udp_zero_copy.cpp:60-96](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/transport/udp_zero_copy.cpp#L60-L96) —— 先 `claim_with_wait` 抢帧槽，再 `recv_udp_packet` 收包，成功则 `make` 成指针；失败（超时）则撤销抢占返回 null。

发送帧包装类 `udp_zero_copy_asio_msb`——注意 `release()` 里才真正发包：

[host/lib/transport/udp_zero_copy.cpp:102-129](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/transport/udp_zero_copy.cpp#L102-L129) —— `release()` 调 `send_udp_packet(_sock_fd, _mem, size())`，这里的 `size()` 就是用户 `commit` 写入的长度。

主实现类 `udp_zero_copy_asio_impl` 的构造——开 socket、分配帧池、为每帧造包装对象：

[host/lib/transport/udp_zero_copy.cpp:143-181](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/transport/udp_zero_copy.cpp#L143-L181) —— `buffer_pool::make(num_frames, frame_size)` 分配连续内存，`_mrb_pool`/`_msb_pool` 把每帧包成包装对象。

`get_recv_buff` 的轮转逻辑：

[host/lib/transport/udp_zero_copy.cpp:199-204](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/transport/udp_zero_copy.cpp#L199-L204) —— 索引走到末尾归零（环形），然后调当前帧的 `get_new`。

`make` 函数体——解析 hints、填默认、调缓冲、告警：

[host/lib/transport/udp_zero_copy.cpp:263-368](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/transport/udp_zero_copy.cpp#L263-L368) —— 其中 [L285-304](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/transport/udp_zero_copy.cpp#L285-L304) 填帧数/帧大小默认值，[L309-320](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/transport/udp_zero_copy.cpp#L309-L320) 算缓冲默认值，[L339-365](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/transport/udp_zero_copy.cpp#L339-L365) 调 socket 缓冲并在不足 `num_frames × MAX_ETHERNET_MTU` 时告警。

收发包的真正系统调用封装在内部头里，建议一并精读：

[host/lib/include/uhdlib/transport/udp_common.hpp:101-148](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/include/uhdlib/transport/udp_common.hpp#L101-L148) —— `recv_udp_packet` 先试 `MSG_DONTWAIT` 非阻塞收，否则 `poll` 等待；`send_udp_packet` 在 `ENOBUFS` 时退避 1 µs 重试。

#### 4.2.4 代码实践：对比 udp_zero_copy 与 udp_simple（本讲主实践）

**实践目标**：对照源码说清 `udp_zero_copy` 与 `udp_simple` 的差异，并解释为何前者更适合高吞吐数据通路、后者更适合控制与发现。

**操作步骤**：

1. 读 `udp_simple` 接口与实现：[host/include/uhd/transport/udp_simple.hpp:20-95](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/transport/udp_simple.hpp#L20-L95) 与 [host/lib/transport/udp_simple.cpp:18-80](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/transport/udp_simple.cpp#L18-L80)。
2. 读 `udp_zero_copy` 实现的收/发包装类（4.2.3 已给链接）。
3. 用下表逐项填空（答案见后）。

| 维度 | `udp_simple` | `udp_zero_copy` |
| --- | --- | --- |
| 接口外形 | 单缓冲 `send`/`recv`，调用方自带内存 | `get_*_buff` 返回传输层托管的帧 |
| 内存归属 | 调用方的栈/堆缓冲 | 传输层的 `buffer_pool`，帧可复用 |
| 收发时机 | `recv()`/`send()` 调用即发生 | 收：`get_recv_buff` 内同步；发：指针析构 `release()` 时 |
| 帧数/帧大小 | 无概念，固定 `mtu=1472` | 可配 `num_*_frames` / `*_frame_size` |
| 内核缓冲调整 | 无 | 构造时主动 `set_option` 调大并告警 |
| 典型用途 | 控制事务、设备发现（broadcast） | 高吞吐样本数据通路 |

**需要观察的现象 / 预期结果**：

- `udp_simple::recv` 里只有一次 `wait_for_recv_ready` + `receive_from`（[udp_simple.cpp:55-62](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/transport/udp_simple.cpp#L55-L62)），调用方必须自己准备缓冲并承担「缓冲↔应用数据」的搬运。
- `udp_zero_copy` 把缓冲与帧池托管起来，上层流器直接在帧内存上做 convert（u4-l1），省掉一次额外拷贝；并且通过调大内核 socket 缓冲（[udp_zero_copy.cpp:339-346](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/transport/udp_zero_copy.cpp#L339-L346)）降低瞬时突发丢包。

**关于高吞吐优势的结论**：在每秒数万包的样本流下，`udp_simple`「每包一次系统调用 + 一次用户侧拷贝」会成为瓶颈；`udp_zero_copy` 用帧池复用、与流器共享内存、调大内核缓冲，把系统调用频率与帧频率对齐，从而支撑 1 GbE/10 GbE 线速。具体吞吐数值**待本地验证**（依赖网卡、CPU、`net.core.rmem_max` 等）。

#### 4.2.5 小练习与答案

**Q1**：`udp_zero_copy` 头注释声称能用「kernel packet ring」做真正的零拷贝，当前代码实现了吗？
**A**：没有。当前默认编译的 `udp_zero_copy.cpp` 是 boost::asio 可移植实现，注释自己也承认「not a true zero copy … requires a copy operation to/from userspace」。Windows 上另有 `udp_wsa_zero_copy.cpp` 变体。头注释里 kernel packet ring 的说法是历史/规划性表述，不代表当前主线路径。

**Q2**：为什么发送要在 `release()` 里才发包，而不是在 `get_send_buff()` 时？
**A**：`get_send_buff()` 时帧还是空的，没有数据可发。上层必须先写帧、`commit` 出有效长度，框架才知道发多少。由于 `commit` 发生在 `get` 之后、析构之前，把真正发包放到 `release()`（指针析构时）是最自然的时机——它保证「用户已写完且不再持有该帧」。

**Q3**：把 `recv_buff_size` 调小到小于 `num_recv_frames × MAX_ETHERNET_MTU` 会怎样？
**A**：构造期 `make` 检测到这一情况会打印 `UHD_LOG_WARNING`，提示「可能因 NIC 不足而丢包」（见 [udp_zero_copy.cpp:348-356](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/transport/udp_zero_copy.cpp#L348-L356)）。这不会阻止构造，但运行期更易出现 u2-l6 讲过的 `ERROR_CODE_OVERFLOW`。

---

### 4.3 USB zero-copy 传输（usb_zero_copy / libusb1）

#### 4.3.1 概念说明

USB 设备（如 USRP B200、B100、USRP1）走的是另一条物理链路：USB 批量传输（bulk transfer）。`usb_zero_copy` 同样实现 `zero_copy_if`，但底层从「socket + `recv`/`send`」换成了「libusb 异步批量传输」。

UDP 与 USB 两种实现的关键差异在于「谁驱动收发」：

- **UDP**：每次 `get_recv_buff` 同步调一次 `recv()` 系统调用，**应用线程主动拉**。
- **USB**：构造期就把多个异步传输（`libusb_transfer`）**提交给 libusb**，由 USB 驱动在后台完成；完成后通过回调把结果存进一个结果结构，应用线程在 `get_buff` 里**阻塞等待条件变量**即可。这是一种「推」模型。

正因如此，USB 版用两个新组件：`lut_result_t`（存放回调里看到的状态与实际长度，因为 libusb 规定这些只能在回调里读）与一个释放队列（`bounded_buffer`），用来把「用完的帧」异步归还。

#### 4.3.2 核心流程

`usb_zero_copy::make` 的执行顺序：

1. 从 hint 解析 `num_recv_frames`/`recv_frame_size`（缺省 `DEFAULT_NUM_XFERS=16`、`DEFAULT_XFER_SIZE=32×512=16384`），发送侧同理。
2. 为接收、发送各造一个 `libusb_zero_copy_single`，各管理自己的帧池与一组 `libusb_transfer`。
3. 构造期把所有异步传输 `submit` 给 libusb，启动后台收发。

运行期路径：

- **接收**：`get_recv_buff` → 加锁 → `recv_impl->get_buff<managed_recv_buffer>(timeout)` → 在「完成队列」上等条件变量 → 取到一个已完成的帧 → 包装成指针返回；指针析构时把帧对应的传输重新 submit，形成流水线。
- **发送**：`get_send_buff` 拿帧 → 用户写 + `commit` → 析构时把该传输 submit 出去。

这套「预提交一批、用完即重提交」的设计，让 USB 管线始终有多个传输在途，从而吃满 USB 带宽——这是高吞吐的关键，类比 UDP 侧的「调大内核缓冲 + 多帧」。

一个重要的可移植性细节：当 UHD 没有链接 libusb 时，编译的是占位实现 `usb_dummy_impl.cpp`，它的 `make` 直接抛 `not_implemented_error("no usb support")`。这保证了 `usb_zero_copy::make` 这个符号在所有平台上都存在，但只有装了 libusb 的构建才真正能用。

#### 4.3.3 源码精读

`usb_zero_copy` 接口与 `make` 签名（注意它要的是 USB 设备句柄与收/发接口号、端点号）：

[host/include/uhd/transport/usb_zero_copy.hpp:26-54](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/transport/usb_zero_copy.hpp#L26-L54)

默认传输数与传输大小常量：

[host/lib/transport/libusb1_zero_copy.cpp:27-28](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/transport/libusb1_zero_copy.cpp#L27-L28) —— `DEFAULT_NUM_XFERS = 16`、`DEFAULT_XFER_SIZE = 32*512`。

回调结果结构 `lut_result_t`（注意 libusb 的限制：状态与长度只能在回调里读）：

[host/lib/transport/libusb1_zero_copy.cpp:38-62](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/transport/libusb1_zero_copy.cpp#L38-L62) —— `status`、`actual_length` 配一个 `condition_variable`，供应用线程等待完成。

主实现 `libusb_zero_copy_impl`——`get_recv_buff`/`get_send_buff` 各加一把锁后委托给 single 子对象：

[host/lib/transport/libusb1_zero_copy.cpp:393-448](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/transport/libusb1_zero_copy.cpp#L393-L448) —— 构造里为收/发各建一个 `libusb_zero_copy_single`，帧数与帧大小从 hints 读取（[L402-411](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/transport/libusb1_zero_copy.cpp#L402-L411)）。

`make` 工厂把通用句柄转成 libusb 句柄后再造 impl：

[host/lib/transport/libusb1_zero_copy.cpp:466-474](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/transport/libusb1_zero_copy.cpp#L466-L474)

无 libusb 时的占位实现——直接抛异常，保证符号存在但不可用：

[host/lib/transport/usb_dummy_impl.cpp:43-50](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/transport/usb_dummy_impl.cpp#L43-L50)

#### 4.3.4 代码实践：跟踪 USB 设备如何选用 usb_zero_copy

**实践目标**：确认哪些设备用 `usb_zero_copy`，以及控制通路与数据通路是否复用同一传输。

**操作步骤**：

1. 用 `Grep` 搜索 `usb_zero_copy::make` 的调用点。
2. 阅读三个典型设备：USRP1（[host/lib/usrp/usrp1/usrp1_impl.cpp:180](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/usrp1/usrp1_impl.cpp#L180)）、B100（`host/lib/usrp/b100/b100_impl.cpp:197`）、B200（`host/lib/usrp/b200/b200_impl.cpp:487` 与 `:606`）。

**需要观察的现象 / 预期结果**：

- B200 同时用两个 `usb_zero_copy`：一个做**控制**（`_ctrl_transport`，L487），一个做**数据**（`_data_transport`，L606）。这说明 `zero_copy_if` 既可承载数据流，也可承载控制寄存器读写。
- 控制与数据各走不同的 USB 端点（`recv_interface`/`recv_endpoint`/`send_*` 参数），物理上隔离，避免互相阻塞。

具体端点号与带宽：**待本地验证**（依赖设备型号）。

#### 4.3.5 小练习与答案

**Q1**：UDP 版「`get` 时同步收」，USB 版为何改成「预先 submit + 回调 + 等条件变量」？
**A**：USB 批量传输的延迟与调度由 USB 控制器决定，应用线程若每次都同步发起一次传输再等完成，管线深度只有 1，根本吃不满带宽。预先提交一批（默认 16 个）传输、用完立即重提交，让管线始终满载，才能逼近 USB 极限速率。

**Q2**：没有 libusb 的构建里调用 `usb_zero_copy::make` 会发生什么？
**A**：链接到的是 `usb_dummy_impl.cpp` 的占位实现，`make` 抛 `uhd::not_implemented_error("no usb support -> ...")`。符号存在、调用合法，但运行期立刻失败——这是一种常见的「接口恒在、能力可选」的移植手法。

---

### 4.4 传输家族与设备层选型（udp_simple / DPDK / NIRIO / link_if）

#### 4.4.1 概念说明

把视野拉高，UHD 的传输其实是一个**家族**，`zero_copy_if` 只是其中面向「老数据通路」的一支。完整家族可以这样分：

| 传输 | 继承自 | 典型用途 | 典型设备 |
| --- | --- | --- | --- |
| `udp_simple` | 独立（单缓冲） | 控制事务、设备发现（broadcast） | 所有以太网设备、OctoClock |
| `udp_zero_copy` | `zero_copy_if` | 老以太网数据通路 | USRP2 / N2x0 系列 |
| `usb_zero_copy` | `zero_copy_if` | USB 数据与控制 | B100 / B200 / USRP1 |
| `nirio_zero_copy` / `nirio_link` | `zero_copy_if` / `link_if` | PCIe（NI-RIO） | X300 PCIe 版 |
| `udp_boost_asio_link` | `link_if`（现代） | 现代以太网数据通路 | X300 / N3x0（MPMD） |
| `udp_dpdk_link` / `dpdk_simple` | `link_if` / 独立 | 高吞吐（DPDK 旁路内核） | X300 / N3x0，需 `use_dpdk` |

这里出现了一个本讲必须澄清的演进：现代 RFNoC 设备（X300、N3x0 系列）的数据通路**不再用 `udp_zero_copy`**，而是用更晚引入的 `link_if` 抽象（`send_link_if`/`recv_link_if`），其以太网落地是 `udp_boost_asio_link`（默认）或 `udp_dpdk_link`（DPDK）。`link_if` 与 `zero_copy_if` 是两套并行抽象，最终都喂给收发流器。`udp_zero_copy` 则仍服务于 N2x0 这类老设备的数据通路。

#### 4.4.2 核心流程：设备层如何选型

设备层选型的精髓是**工厂函数指针**。以 X300 为例：构造传输时并不写死 `udp_simple::make_connected`，而是先通过 `x300_get_udp_factory(use_dpdk)` 决定「用哪个工厂」，再调用它。

```
use_dpdk = hint.has_key("use_dpdk")
factory = use_dpdk ? dpdk_simple::make_connected   # 仅当编译含 HAVE_DPDK
                   : udp_simple::make_connected      # 默认 boost::asio
xport = factory(addr, port)                          # 统一调用
```

- **控制/发现通路**：统一用 `udp_simple`（或 DPDK 版 `dpdk_simple`）的 connected/broadcast。`make_broadcast` 用于发现（发广播、收多源回包），`make_connected` 用于点对点控制事务。
- **数据通路（现代）**：默认 `udp_boost_asio_link::make`；若 `use_dpdk` 且编译含 `HAVE_DPDK`，则 `udp_dpdk_link::make`，绕过内核协议栈直接在用户态轮询网卡，用于 10 GbE 线速。
- **数据通路（PCIe）**：`nirio_link::make`，基于 NI-RIO 的 PCIe DMA。

`use_dpdk` 是用户在 device args 里传的提示键，但**只有当 UHD 编译时开启了 DPDK（`HAVE_DPDK`）才真正生效**；否则只打印一条 warning，回退到 boost::asio。这种「软开关 + 编译期硬约束」的组合是处理可选加速器的常见模式。

#### 4.4.3 源码精读

`udp_simple` 的两种工厂（connected 与 broadcast）与 UART 用法：

[host/include/uhd/transport/udp_simple.hpp:31-65](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/transport/udp_simple.hpp#L31-L65) —— 注释明确：connected 用于「控制事务，简单可移植但不快」，broadcast 用于「发现设备」。

`udp_simple` 的 `send`/`recv` 实现（注意 broadcast 用 `send_to`、connected 用 `send`）：

[host/lib/transport/udp_simple.cpp:48-62](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/transport/udp_simple.cpp#L48-L62)

X300 的选型核心——工厂函数指针在 boost::asio 与 DPDK 间切换：

[host/lib/usrp/x300/x300_eth_mgr.cpp:81-95](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/x300/x300_eth_mgr.cpp#L81-L95) —— `x300_get_udp_factory(use_dpdk)`：默认返回 `udp_simple::make_connected`；`use_dpdk` 且 `HAVE_DPDK` 时换成 `dpdk_simple::make_connected` 的 lambda，否则 warning。

发现阶段用工厂函数发起广播：

[host/lib/usrp/x300/x300_eth_mgr.cpp:102-114](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/x300/x300_eth_mgr.cpp#L102-L114) —— `udp_make_broadcast(first_addr, X300_FW_COMMS_UDP_PORT)` 发出请求、循环收回复包。

现代数据通路两条分支（grep 结果已定位）：`udp_dpdk_link::make`（[x300_eth_mgr.cpp:276](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/x300/x300_eth_mgr.cpp#L276)）与 `udp_boost_asio_link::make`（[x300_eth_mgr.cpp:289](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/x300/x300_eth_mgr.cpp#L289)）；PCIe 版用 `nirio_link::make`（[x300_pcie_mgr.cpp:365](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/x300/x300_pcie_mgr.cpp#L365)）。MPMD 设备同样二选一：`udp_dpdk_link::make`（[mpmd_link_if_ctrl_udp.cpp:446](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/mpmd/mpmd_link_if_ctrl_udp.cpp#L446)）或 `udp_boost_asio_link::make`（[mpmd_link_if_ctrl_udp.cpp:458](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/mpmd/mpmd_link_if_ctrl_udp.cpp#L458)）。

> 对照 u2-l1/u2-l2 讲过的 `device_addr_t`：`use_dpdk`、`recv_frame_size`、`recv_buff_size` 这些键正是以 hint 形式流入这里的工厂，体现了「同一 `device_addr_t` 在不同阶段扮演 hint / 档案 / 传输参数三种语义」的设计。

#### 4.4.4 代码实践：画出「设备 → 选型 → 传输」决策树

**实践目标**：把本讲的传输家族与设备层选型串成一张可复用的决策图。

**操作步骤**：

1. 列出三类设备的传输选型：
   - USB 设备（B200/USRP1）：控制与数据均 `usb_zero_copy`。
   - 老以太网设备（N2x0）：控制 `udp_simple`、数据 `udp_zero_copy`。
   - 现代以太网设备（X300/N3x0）：控制 `udp_simple`/`dpdk_simple`、数据 `udp_boost_asio_link` 或 `udp_dpdk_link`（视 `use_dpdk`）。
   - PCIe 设备（X300 PCIe）：数据 `nirio_link`。
2. 画出决策树根节点「物理链路类型？」分支到上述四类。
3. 在每个叶节点标注它实现的抽象（`zero_copy_if` 还是 `link_if`）。

**需要观察的现象 / 预期结果**：你应当得到一张以「物理链路」为根、以「具体传输类 + 抽象基类」为叶的树。关键是意识到：上层收发流器只认 `zero_copy_if` 或 `link_if` 这两个抽象，**底下换成哪种传输对上层透明**——这正是 u2-l5 讲过的「接口契约 vs 设备分派」在传输层的重演。

#### 4.4.5 小练习与答案

**Q1**：为什么控制通路几乎都选 `udp_simple` 而不是 `udp_zero_copy`？
**A**：控制事务是少量、低频、请求-应答式的（读个寄存器、下发一条命令），对吞吐毫无要求，反而需要「简单可移植、调用即收发」的语义。`udp_simple` 正是为此设计（注释原话 "simple and portable (not fast)"），帧池与缓冲调整对它都是多余开销。

**Q2**：用户传了 `use_dpdk`，但 UHD 没编译 DPDK 支持，会发生什么？
**A**：`x300_get_udp_factory` 检测到 `use_dpdk` 但没有 `HAVE_DPDK`，打印 `UHD_LOG_WARNING("Detected use_dpdk argument, but DPDK support not built in.")`，并**回退到** `udp_simple::make_connected` / `udp_boost_asio_link`。功能不中断，只是拿不到 DPDK 的内核旁路加速。

**Q3**：`zero_copy_if` 与 `link_if` 是什么关系？
**A**：两者都是传输抽象，都最终喂给收发流器，但属于不同代际。`zero_copy_if`（含 `udp_zero_copy`/`usb_zero_copy`/`nirio_zero_copy`）较老，服务于 N2x0、USB 设备等；`link_if`（含 `udp_boost_asio_link`/`udp_dpdk_link`/`nirio_link`）较新，服务于现代 RFNoC 设备（X300/N3x0）。它们并存而非替代，是 UHD 演进过程中「新抽象与旧抽象共存」的典型现象。

## 5. 综合实践

**任务：追踪一个样本从「线缆」到「应用缓冲」的完整搬运路径，并定位每一处可能的拷贝。**

请结合本讲与 u4-l1（convert）、u2-l5/u2-l6（流器）完成：

1. 选定一种设备链路（建议现代以太网 X300，默认非 DPDK）。
2. 从下往上画出五层搬运路径，标注每层的关键源码位置：
   - **物理层**：网卡/USB/PCIe DMA 把字节搬进内核或用户态缓冲。
   - **传输层**：`udp_boost_asio_link`（或 `udp_zero_copy`）把字节装进帧（本讲）。
   - **流器层**：`rx_streamer` 经 `super_recv_packet_handler`（u4-l3 将讲）聚合帧。
   - **转换层**：convert 把 `otw_format`（如 sc16）转成 `cpu_format`（如 fc32）（u4-l1）。
   - **应用层**：用户缓冲（u2-l6 的 `recv` 循环）。
3. 在路径上标出每一处 `memcpy` / 系统调用：
   - boost::asio UDP 版有几次用户态↔内核态拷贝？
   - convert 是 in-place 还是另开缓冲？
   - 应用层是否直接拿到了传输层/convert 的内存（真正的零拷贝）还是又拷了一次？
4. 写出结论：在哪一层消除拷贝收益最大？为什么 DPDK（旁路内核）对 10 GbE 如此关键？

**预期产出**：一张五层路径图 + 一份「拷贝点清单」。具体延迟与吞吐数据**待本地验证**；本实践的核心收益是建立「数据在 UHD 内部如何流动」的清晰心智模型，这是后续 u4-l3（VRT 包）与调试丢包问题的前提。

## 6. 本讲小结

- `zero_copy_if` 是传输层统一外形，核心是「帧池 + 侵入式引用计数」：`get_*_buff` 取帧、用完指针析构自动 `release()` 还帧；发送侧真正发包推迟到 `release()`。
- `udp_zero_copy` 是 boost::asio 可移植实现，**并非真正的零拷贝**——每次收发仍有一次内核↔用户态拷贝；它省的是「用户侧第二次拷贝」，并用帧池复用 + 调大内核缓冲（默认 20 ms / 2.5 MB）支撑高吞吐。
- `usb_zero_copy` 基于 libusb 异步批量传输，是「预先 submit 一批 + 回调 + 条件变量等待」的推模型，默认 16 个在途传输吃满 USB 管线；无 libusb 时回退到抛异常的占位实现。
- 传输是个家族：`udp_simple`（控制/发现）、`udp_zero_copy`（老以太网数据）、`usb_zero_copy`（USB）、`nirio_*`（PCIe）、现代 `link_if`（`udp_boost_asio_link`/`udp_dpdk_link`）。
- 设备层用**工厂函数指针**做选型，`use_dpdk` 软开关 + `HAVE_DPDK` 编译期硬约束决定是否启用 DPDK 内核旁路。
- `zero_copy_if`（老）与 `link_if`（新）并存，都喂给收发流器；上层对底层换成哪种传输无感知。

## 7. 下一步学习建议

本讲把「字节如何过物理链路」讲到了帧与系统调用这一层。接下来：

- **u4-l3 VRT 包协议与 super packet handler**：紧接本讲，讲清「一帧里装的到底是什么包」——VITA Radio Transport 包头如何解析、`super_recv_packet_handler` 如何把多个传输帧聚合成一次流器 `recv`。
- 若你想理解现代 `link_if` 如何与 `io_service`、流器接线，可顺读 `host/lib/include/uhdlib/transport/link_if.hpp`、`rx_streamer_zero_copy.hpp`。
- 若你对 DPDK 旁路内核的实现细节感兴趣，可读 `host/lib/transport/udp_dpdk_link.cpp` 与 `host/lib/include/uhdlib/transport/dpdk_io_service.hpp`（需编译含 `HAVE_DPDK`）。
- 调试真实丢包时，回到本讲的「调内核缓冲」一节（[udp_zero_copy.cpp:339-365](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/transport/udp_zero_copy.cpp#L339-L365)），对照 `sysctl net.core.rmem_max` 与 u2-l6 的 `OVERFLOW` 标志做联合分析。
