# 流式 API：stream_args_t 与收发流器

## 1. 本讲目标

本讲聚焦 UHD 收发数据的「最后一公里」——流式（streaming）API。读完本讲，你应当能够：

- 说清 `stream_args_t` 四个字段（`cpu_format`、`otw_format`、`args`、`channels`）各自的含义与作用。
- 解释 `cpu_format` 与 `otw_format` 的命名约定，并能从字符串推断出 C++ 类型。
- 看懂 `rx_streamer` / `tx_streamer` 这两个抽象类暴露的核心方法。
- 弄明白「通道数」如何决定 `recv` / `send` 调用时要传入多少个缓冲区。
- 把本讲与前序的 `multi_usrp`（u2-l3）、属性树（u2-l4）串起来，理解 `get_rx_stream` / `get_tx_stream` 在 `device` 上的落点。

本讲只讲**接口契约**（头文件里写了什么、约定是什么），不展开具体设备的 `recv` 实现细节——那是 u2-l6/u2-l7 与 u4 系列的任务。

## 2. 前置知识

在进入本讲前，请确认你已经理解以下概念（它们在前序讲义中已建立）：

- **device 与 multi_usrp 的分层**：`uhd::device` 是抽象基类，`multi_usrp` 是它之上的易用封装（facade），持有 `device` 与 `property_tree`（见 u2-l3）。
- **属性树即「文件系统」**：设备的大部分配置都映射成树节点，`multi_usrp` 的 set/get 方法是属性树的翻译层（见 u2-l4）。
- **采样率要回读**：硬件会把请求的采样率四舍五入到最接近的支持值，所以设置后必须回读实际值（见 u1-l6）。
- **公共头文件的 UHD_API 宏**：决定符号是导出还是导入（见 u1-l4）。

本讲新增一个关键直觉：

> **流式 API 是类型擦除（type-erased）的。** 你用字符串 `"fc32"` 告诉 UHD「我的缓冲区是 `complex<float>`」，而不是用 C++ 模板参数。因此 UHD 能在运行期决定如何在线缆格式（`otw`）与主机格式（`cpu`）之间做转换，而无需为每种组合编译一份模板代码。

理解了「字符串描述格式」这一点，本讲后面的所有设计都会顺理成章。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [host/include/uhd/stream.hpp](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/stream.hpp) | **本讲主角**。声明 `stream_args_t`、`rx_streamer`、`tx_streamer` 三个公共类型，定义流式 API 的全部接口契约。 |
| [host/lib/stream.cpp](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/stream.cpp) | 极薄实现，仅提供两个空析构函数；真正的 `recv`/`send` 实现分散在各设备驱动里。 |
| [host/include/uhd/device.hpp](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/device.hpp) | 在 `device` 抽象基类上声明 `get_rx_stream` / `get_tx_stream` 两个纯虚工厂方法，是构造流器的入口。 |
| [host/include/uhd/types/ref_vector.hpp](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/types/ref_vector.hpp) | `ref_vector<T>`，`recv`/`send` 的缓冲参数类型，理解它就理解了「为什么传一个指针也能、传一个 vector 也能」。 |
| [host/examples/rx_samples_to_file.cpp](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/rx_samples_to_file.cpp) | 把本讲全部概念串起来的真实示例（u1-l6 已建立四阶段阅读模板）。 |

## 4. 核心概念与源码讲解

### 4.1 stream.hpp：流式 API 的总入口

#### 4.1.1 概念说明

`stream.hpp` 是一个**纯接口头文件**：它只声明「流式数据通路长什么样」，不包含任何具体设备的实现。它一次性定义了三样东西：

1. `stream_args_t` —— 构造流器的**参数包**（你要什么格式、哪些通道）。
2. `rx_streamer` —— **接收流器**抽象类（主机从设备读样本的接口）。
3. `tx_streamer` —— **发送流器**抽象类（主机向设备写样本的接口）。

这三个类型之间的关系是：用 `stream_args_t` 配好参数 → 交给 `device::get_rx_stream` / `get_tx_stream` → 拿回一个 `rx_streamer::sptr` / `tx_streamer::sptr` → 对着它反复调用 `recv` / `send`。

注意头文件顶部对三类元数据结构只做了**前向声明**（forward declaration），没有 `#include` 它们的完整定义：

```cpp
namespace uhd {
struct async_metadata_t;
struct rx_metadata_t;
struct tx_metadata_t;
```

这是降低编译依赖的常见手法：`recv`/`send` 的签名里只用到了这些类型的引用（`rx_metadata_t&`），前向声明就足够，不必把整个 `metadata.hpp` 拉进每一个 include `stream.hpp` 的文件。

#### 4.1.2 核心流程

一个典型接收程序在「构造设备」之后，进入流式阶段的步骤是：

```text
(1) 组装 stream_args_t（cpu_format / otw_format / channels / args）
        │
        ▼
(2) usrp->get_rx_stream(stream_args)  ──► 返回 rx_streamer::sptr
        │
        ▼
(3) rx_stream->issue_stream_cmd(START)  ──► 命令设备开始往主机推样本
        │
        ▼
(4) 循环 rx_stream->recv(buffs, nsamps, md, timeout)
        │            └─► 阻塞直到填满缓冲 / 超时 / 收到结束包 / 出错
        ▼
(5) 检查 md.error_code，处理溢出/超时/对齐错误
```

发送侧（`tx_streamer`）类似，只是没有 `issue_stream_cmd`，而是直接 `send`，并可选地用 `recv_async_msg` 收取「欠流（underflow）」等异步回报。

#### 4.1.3 源码精读

`stream.hpp` 的整体骨架在文件开头：包含基础头、前向声明元数据类型，随后定义 `stream_args_t`、`rx_streamer`、`tx_streamer`。

- [host/include/uhd/stream.hpp:21-24](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/stream.hpp#L21-L24)：`namespace uhd` 内前向声明三个元数据结构，说明本头文件刻意不引入它们完整定义。

对应的 `stream.cpp` 只有空析构函数，这是关键证据，说明**接口与实现是分离的**：

- [host/lib/stream.cpp:12-20](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/stream.cpp#L12-L20)：`rx_streamer` 与 `tx_streamer` 的析构函数体均为空。真正的 `recv` / `send` 实现不在 `stream.cpp`，而在各设备驱动（如 RFNoC 的 `rx_streamer_impl`、老设备的 `recv_packet_handler`）中。

而 `get_rx_stream` / `get_tx_stream` 这两个工厂方法声明在 `device` 抽象基类上：

- [host/include/uhd/device.hpp:76-96](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/device.hpp#L76-L96)：`device` 上声明 `get_rx_stream` 与 `get_tx_stream` 为**纯虚函数**（`= 0`），由具体设备实现。注意注释里两条重要约束——RFNoC 设备每个通道同一时刻只能挂一个流器；非 RFNoC 设备同一时刻只能有一个 RX 流器。`multi_usrp` 的 `get_rx_stream` / `get_tx_stream` 只是透传到这里。

#### 4.1.4 代码实践

**实践目标**：确认「`stream.hpp` 只定义接口、`stream.cpp` 几乎空」这一事实，建立对接口/实现分离的直观感受。

**操作步骤**：

1. 打开 [host/lib/stream.cpp](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/stream.cpp)，统计它有多少行、定义了哪些函数。
2. 打开 [host/include/uhd/stream.hpp](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/stream.hpp)，用编辑器搜索 `virtual ... = 0`，数出 `rx_streamer` 和 `tx_streamer` 各有几个纯虚方法。

**需要观察的现象**：`stream.cpp` 只有约 20 行、两个空析构函数；而 `stream.hpp` 里 `recv`、`send` 等都是 `= 0` 的纯虚声明。

**预期结果**：你会看到 `rx_streamer` 有 5 个纯虚方法（`get_num_channels`、`get_max_num_samps`、`recv`、`issue_stream_cmd`、`post_input_action`），`tx_streamer` 有 5 个（`get_num_channels`、`get_max_num_samps`、`send`、`recv_async_msg`、`post_output_action`）。这印证了「头文件立约、实现在别处」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `stream.hpp` 顶部只对 `rx_metadata_t` 等做前向声明，而不直接 `#include <uhd/types/metadata.hpp>`？

**参考答案**：因为 `recv`/`send` 的函数签名里只用到了 `rx_metadata_t&` / `const tx_metadata_t&` 这类引用，C++ 对引用类型只需前向声明即可。不 include 完整定义可以显著降低编译依赖——所有间接 include `stream.hpp` 的文件都不必再编译 `metadata.hpp` 的全部内容，加快编译、减少耦合。

**练习 2**：`stream.cpp` 里没有 `recv` 的实现，那运行时调用的 `rx_stream->recv(...)` 究竟执行的是哪段代码？

**参考答案**：执行的是具体设备子类提供的覆盖（override）。例如 RFNoC 设备走 `rx_streamer_impl`，老设备走各自的接收包处理器。`rx_streamer::sptr` 持有的是子类对象，虚函数分派在运行期把调用导向真实实现——这正是「接口在公共头、实现在驱动内」的设计。

---

### 4.2 stream_args_t：构造流器的参数包

#### 4.2.1 概念说明

`stream_args_t` 是一个普通结构体（`struct`），用来把「我想要什么样的流」一次性描述清楚，再交给 `get_rx_stream` / `get_tx_stream`。它有四个字段：

| 字段 | 类型 | 作用 |
| --- | --- | --- |
| `cpu_format` | `std::string` | **主机内存**里样本的格式（你的缓冲区是什么 C++ 类型）。 |
| `otw_format` | `std::string` | **线缆上（over-the-wire）**样本的格式（设备与主机之间实际传输什么）。 |
| `args` | `device_addr_t` | 额外的键值对（如 `spp`、`fullscale`、`peak` 等高级选项）。 |
| `channels` | `std::vector<size_t>` | 选择哪些通道、按什么顺序映射到流器通道。 |

它的设计哲学正是本讲核心直觉：**用字符串描述格式**。你写 `"fc32"`，UHD 就知道你的缓冲区是 `std::complex<float>`；写 `"sc16"`，就知道是 `std::complex<int16_t>`。这样 UHD 能在运行期组合「任意 cpu 格式 × 任意 otw 格式」并自动挑选用哪个转换器（convert，详见 u4-l1）。

#### 4.2.2 核心流程

格式字符串遵循一套统一的**命名约定**，看懂约定就能举一反三：

- 第一个字母表示**实数/复数**：`f`/`s` 开头是实数，`c` 表示复数（complex，实部+虚部）。
- 第二个字母表示**数值类型**：`f` = 浮点（float/double），`s` = 有符号整数（signed int）。
- 末尾数字表示**每个分量多少 bit**。

由此可推导：

```text
fc32 → f(浮点) + c(复数) + 32bit → complex<float>        （实虚部各 32 位浮点）
fc64 → complex<double>
sc16 → s(整数) + c(复数) + 16bit → complex<int16_t>
sc8  → complex<int8_t>
f32  → float       （实数，列出但未实现，用于说明命名）
```

`cpu_format`（主机端）已实现的选项是 `fc64 / fc32 / sc16 / sc8`；`otw_format`（线缆端）已实现的是 `sc16 / sc8 / sc12`（`sc12` 仅部分设备支持）。二者不必相同——这正是 convert 子系统存在的意义：例如主机用 `fc32` 做浮点运算，线缆用 `sc16` 传 16 位定点，UHD 自动在二者间转换。

`otw_format` 的选择有权衡：位宽越小（`sc16` → `sc8`）动态范围越小、量化噪声越大，但链路负载更轻、可用带宽更高。源码注释里给出经典例子——USRP N210 在 16 位复采样下支持 25 MHz 带宽，8 位复采样下可达 50 MHz。

通道映射的关键规则：**`channels` 里的元素个数 = 流器的通道数 = 你之后每次 `recv`/`send` 要提供的缓冲区个数**。例如 `channels = {0, 1}` 表示「我要 2 个通道，第 0 路映射到通道 0、第 1 路映射到通道 1」；写成 `{1, 0}` 则把两路顺序颠倒，应用程序里的「第 0 通道」实际对应设备的物理通道 1。

> ⚠️ RFNoC 设备例外：`channels` 字段**不被使用**。要创建多通道流器，需改用 `rfnoc_graph::create_rx_streamer(num_ports, ...)` 的 `num_ports` 参数。详见 u3-l1 / u3-l3。

#### 4.2.3 源码精读

`stream_args_t` 的完整定义：

- [host/include/uhd/stream.hpp:50-162](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/stream.hpp#L50-L162)：整个结构体。带 `UHD_API` 标记，属于公共导出符号。

便利构造函数只做两件事——把两个格式字符串赋给字段：

- [host/include/uhd/stream.hpp:53-57](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/stream.hpp#L53-L57)：`stream_args_t(cpu, otw)`，所以你可以一行写完 `uhd::stream_args_t stream_args("fc32", "sc16");`。

`cpu_format` 的文档与字段，明确列出了已实现格式与命名约定：

- [host/include/uhd/stream.hpp:59-75](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/stream.hpp#L59-L75)：注意它同时列出了 `f32/f64/s16/s8`，并标注「未实现，仅用于演示命名约定」。

`otw_format` 的文档，给出带宽/动态范围的权衡：

- [host/include/uhd/stream.hpp:77-95](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/stream.hpp#L77-L95)：N210 的 25 MHz / 50 MHz 对比就出自这里。

`args` 字段承载高级键值对：

- [host/include/uhd/stream.hpp:97-132](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/stream.hpp#L97-L132)：列出了 `fullscale`、`peak`、`underflow_policy`（`next_burst` / `next_packet`）、`spp`（每包样本数）、`noclear` 等键。`spp` 用于请求比默认更小的包以降低延迟。

`channels` 字段，并显式声明 RFNoC 设备不用它：

- [host/include/uhd/stream.hpp:134-161](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/stream.hpp#L134-L161)：注释里用 B210（subdev spec `A:A A:B`）举例说明 `{0,1}` 与 `{1,0}` 的差别，以及留空时默认为通道 0。

真实示例中的用法（与头文件文档示例一致）：

- [host/examples/rx_samples_to_file.cpp:163-165](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/rx_samples_to_file.cpp#L163-L165)：示例把命令行传入的 `cpu_format`、`wire_format`、`channel_nums` 填进 `stream_args`，再 `get_rx_stream`。

#### 4.2.4 代码实践

**实践目标**（本讲指定的实践任务）：编写一个 `stream_args_t`，设置 `cpu_format=fc32`、`otw_format=sc16`、`channels={0,1}`，并说明它将产生几个接收缓冲。

**示例代码**（非项目原码，仅作演示）：

```cpp
#include <uhd/stream.hpp>
#include <uhd/usrp/multi_usrp.hpp>

// 假设 usrp 已是一个有效的 multi_usrp
uhd::usrp::multi_usrp::sptr usrp = /* ... */;

// 1) 主机内存用 complex<float>，线缆用 16 位定点复数
uhd::stream_args_t stream_args("fc32", "sc16");

// 2) 选择通道 0 和 1（双通道 / MIMO）
stream_args.channels = {0, 1};

// 3) （可选）请求每包 200 个样本，降低延迟
stream_args.args["spp"] = "200";

// 4) 构造接收流器
uhd::rx_streamer::sptr rx_stream = usrp->get_rx_stream(stream_args);

// 关键问题：它会产生几个接收缓冲？
// 答：channels 有 2 个元素，所以 rx_stream->get_num_channels() == 2，
//     每次 recv 必须传入「2 个缓冲区」（每通道一个）。
```

**需要观察的现象 / 预期结果**：调用 `rx_stream->get_num_channels()` 会返回 `2`。这与头文件文档示例完全吻合——文档对 `channels={0,1,2}` 明确写道「any calls to rx_stream must provide a vector of 3 buffers, one per channel」（见 [stream.hpp:43-45](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/stream.hpp#L43-L45)）。因此本题答案是 **2 个接收缓冲**。若没有真实硬件，可对照 `rx_samples_to_file.cpp` 的缓冲分配代码佐证（见 4.3.4）。

#### 4.2.5 小练习与答案

**练习 1**：若希望主机端用浮点复数做 DSP，但链路负载尽量小，`cpu_format` 与 `otw_format` 应如何组合？这种组合的代价是什么？

**参考答案**：`cpu_format="fc32"`（主机 `complex<float>`，便于 DSP）、`otw_format="sc8"`（线缆传 8 位定点，负载最小）。代价是动态范围变小、量化噪声增大。UHD 会自动在 fc32 与 sc8 之间转换。

**练习 2**：把 `stream_args.channels = {0, 1}` 改成 `{1, 0}`，对应用程序意味着什么？

**参考答案**：通道总数仍是 2，但顺序颠倒了——应用程序里 recv 到的「第 0 个缓冲」对应设备的物理通道 1，「第 1 个缓冲」对应物理通道 0。这是无需改动 subdev spec 即可重排通道顺序的便利手段。

**练习 3**：`stream_args.args["spp"] = "200"` 的作用是什么？省略它时行为如何？

**参考答案**：`spp`（samples per packet）请求每包 200 个样本，通常用于请求比默认更小的包以降低包延迟。省略时，UHD 使用最大帧尺寸的包（吞吐更高、延迟更大）。

---

### 4.3 rx_streamer：接收流器

#### 4.3.1 概念说明

`rx_streamer` 是「主机从设备读取样本」的抽象接口。它继承自 `noncopyable`（不可拷贝），通过 `shared_ptr`（`sptr`）持有。它的职责是把「设备内部接收 DSP 里的样本」搬运到「主机用户提供的缓冲区」，并在搬运过程中处理格式转换、分片（fragmentation）、超时、溢出、序列错误等状况。

它有五个核心方法（全部纯虚）：

| 方法 | 作用 |
| --- | --- |
| `get_num_channels()` | 该流器的通道数（等于构造时 `channels` 的大小）。 |
| `get_max_num_samps()` | 每个缓冲/每包最多多少个样本（决定你缓冲该开多大）。 |
| `recv(buffs, nsamps, md, timeout, one_packet)` | **核心**：阻塞接收样本到 `buffs`。 |
| `issue_stream_cmd(cmd)` | 向设备下达流命令（开始/停止/发 N 个样本）。 |
| `post_input_action(action, port)` | RFNoC 专用：向流器输入边投递 action（高级用法）。 |

#### 4.3.2 核心流程

`recv` 的返回值与终止条件是理解接收的关键。`recv` 会**阻塞**直到下列任一条件成立：

1. 缓冲被填满（返回值 == `nsamps_per_buff`）。
2. 收到 end-of-burst 包（立即返回，并置 `md.end_of_burst`）。
3. 超时（`md.error_code = ERROR_CODE_TIMEOUT`）。
4. `one_packet=true` 时，处理完一个包就返回。
5. 检测到序列错误（丢包，`md.error_code` 置位）。
6. 收到无效包（`ERROR_CODE_BAD_PACKET`）。
7. 多通道无法对齐（`ERROR_CODE_ALIGNMENT`）。
8. 检测到溢出（overrun / overflow：设备产数据比主机读得快）。

返回值是**本次实际收到的样本数**，它可能小于请求值。`recv` 还内置**分片机制**：当一个线上包的样本数超过缓冲剩余空间时，缓冲会被填满，剩余样本由实现内部保存，下次 `recv` 继续从断点读，并用元数据的 fragment 标志标注。

`recv` 的一个反直觉点（务必记住）：

> `timeout` 是「**每一次内部收包调用**的超时」，**不是总超时**。因此 `recv` 的实际阻塞时间可能远大于传入的 `timeout`。另外，`recv` **不是线程安全**的——同一个流器不能被两个线程同时 `recv`，但不同流器可以分别在各自线程里 `recv`。

#### 4.3.3 源码精读

`rx_streamer` 类定义与 `sptr` 别名：

- [host/include/uhd/stream.hpp:169-183](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/stream.hpp#L169-L183)：`typedef std::shared_ptr<rx_streamer> sptr;`，`get_num_channels`、`get_max_num_samps` 为纯虚。

缓冲参数类型 `buffs_type`：

- [host/include/uhd/stream.hpp:183](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/stream.hpp#L183)：`typedef ref_vector<void*> buffs_type;`——注意是 `void*`，这正是「类型擦除」的体现：UHD 不在编译期固定样本类型，而在运行期据 `cpu_format` 解释。

`recv` 的声明与超时默认值：

- [host/include/uhd/stream.hpp:327-331](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/stream.hpp#L327-L331)：`recv(buffs, nsamps_per_buff, metadata, timeout=0.1, one_packet=false)`。其上方 [stream.hpp:185-326](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/stream.hpp#L185-L326) 的注释是 UHD 最详尽的 API 文档之一，覆盖了终止条件、分片、线程安全、错误处理、超时语义、`nsamps=0` 仅取元数据等所有细节。

`issue_stream_cmd`：

- [host/include/uhd/stream.hpp:344](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/stream.hpp#L344)：向设备下达流命令（如 `STREAM_MODE_START_CONTINUOUS` 开启连续接收）。这是接收前「点火」的一步。

`post_input_action`：

- [host/include/uhd/stream.hpp:351-352](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/stream.hpp#L351-L352)：RFNoC 流器用来投递 `action_info`，例如下发频率/时间标签，属于高级用法。

理解 `buffs_type` 必须看 `ref_vector`：

- [host/include/uhd/types/ref_vector.hpp:20-75](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/types/ref_vector.hpp#L20-L75)：`ref_vector` 是「静态大小、不管理内存」的数组视图。它有三种构造方式：
  - [L29-33](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/types/ref_vector.hpp#L29-L33)：由**单个指针**构造，`size()==1`——所以单通道时你可以直接 `rx_stream->recv(&buffer, nsamps, ...)`。
  - [L40-45](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/types/ref_vector.hpp#L40-L45)：由 `std::vector` 构造，`size()` 等于 vector 大小——多通道时传一个「每元素是一个缓冲指针」的 vector。
  - [L53-56](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/types/ref_vector.hpp#L53-L56)：由「指针数组 + 长度」构造。

#### 4.3.4 代码实践

**实践目标**：在真实示例中验证「通道数 == 缓冲数」，并理解缓冲如何分配。

**操作步骤**：

1. 打开 [host/examples/rx_samples_to_file.cpp:172](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/rx_samples_to_file.cpp#L172)，看缓冲容器如何按通道数开空间：
   ```cpp
   std::vector<samp_type*> buffs(rx_stream->get_num_channels());
   ```
2. 看 [L174-176](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/rx_samples_to_file.cpp#L174-L176)：为每个通道单独 `new samp_type[samps_per_buff]`，把指针写进 `buffs[ch]`。
3. 注意 [L169-171](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/rx_samples_to_file.cpp#L169-L171) 的注释：**不能用 `std::vector` 做第二维**，因为 `recv` 会对每个子数组做 `reinterpret_cast<char*>`，这与 vector 的内存布局不兼容，所以示例手动管理裸数组。

**需要观察的现象**：`buffs` 的大小由 `rx_stream->get_num_channels()` 决定，而这个值又来自构造时 `stream_args.channels` 的大小——三者严格一致。

**预期结果**：若 `stream_args.channels = {0, 1}`，则 `get_num_channels()==2`，`buffs` 含 2 个指针，每个指向一块 `samps_per_buff` 大小的样本数组。这正面回答了 4.2.4 的「几个接收缓冲」= 2。

> 待本地验证：若你有 B210 等双通道硬件，可编译运行 `rx_samples_to_file --channels "0,1"`，观察它会写出 2 个文件（`file_ch0.*`、`file_ch1.*`），即每通道一个输出，对应每通道一个缓冲。

#### 4.3.5 小练习与答案

**练习 1**：`recv` 返回 0，但 `md.error_code == ERROR_CODE_NONE`，可能是什么情况？

**参考答案**：最可能是发生了溢出（overrun）。设备产数据比主机读得快，缓冲被填满后设备无法再写。按文档，溢出码不会立刻上报，而是等所有「溢出前已产生的有效样本」都被 `recv` 取走后，才在后续某次调用里把 `error_code` 置为 `ERROR_CODE_OVERFLOW`。因此连续 `recv` 并检查 `error_code` 才能看到溢出标志。

**练习 2**：为什么 `recv` 不能在两个线程里对同一个流器并发调用？

**参考答案**：`stream.hpp` 的线程安全注释（[L240-247](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/stream.hpp#L240-L247)）说明：为了避免加锁开销，`recv` 设计为**非线程安全**。应用必须自行保证同一流器同一时刻只有一个线程在 `recv`；不同流器（接收不同源）才可分别在不同线程并发。

**练习 3**：`get_max_num_samps()` 返回的值应该用来做什么决定？

**参考答案**：用来决定「每个缓冲至少开多大」。每个缓冲应能容纳 `get_max_num_samps()` 个样本，这样一次 `recv` 才不会被自己开的缓冲过小而频繁提前返回（虽然分片机制能处理，但会损失吞吐、增加开销）。

---

### 4.4 tx_streamer：发送流器

#### 4.4.1 概念说明

`tx_streamer` 是「主机向设备写入样本」的抽象接口，与 `rx_streamer` 对称，同样继承 `noncopyable`、用 `sptr` 持有。它的五个核心方法：

| 方法 | 作用 |
| --- | --- |
| `get_num_channels()` | 通道数。 |
| `get_max_num_samps()` | 每包最大样本数。 |
| `send(buffs, nsamps, md, timeout)` | **核心**：阻塞发送样本。 |
| `recv_async_msg(async_md, timeout)` | 收取异步回报（如欠流 underflow）。 |
| `post_output_action(action, port)` | RFNoC 专用：向输出边投递 action。 |

注意接收侧与发送侧的两个对称差异：

1. **缓冲的 const 性**：`rx_streamer::buffs_type = ref_vector<void*>`（可写），`tx_streamer::buffs_type = ref_vector<const void*>`（只读）——发送是「读出主机样本」，接收是「写入主机样本」。
2. **启动方式**：接收靠 `issue_stream_cmd` 点火；发送没有对应方法，直接 `send` 即可。取而代之的是 `recv_async_msg`，用来读取发送侧的异步事件。

#### 4.4.2 核心流程

`send` 会**阻塞**到指定数量的样本被读出各缓冲为止。它内置**分片**：若缓冲样本数超过每包上限，`send` 会自动跨多个包发送，并保证 burst 标志（start_of_burst 只能在第一个分片、end_of_burst 只能在最后一个分片）正确。超时情况下，返回值可能小于请求值（实际发出的样本数）。

发送侧的典型事件是**欠流（underflow）**：主机供数据不够快，设备发送 FIFO 空了。欠流不会通过 `send` 的返回值或同步元数据上报，而是作为**异步消息**经由 `recv_async_msg` 读取。因此一个稳健的发送程序通常会用一个独立线程循环 `recv_async_msg` 来排空这些事件、统计欠流次数。

发送时序通过 `tx_metadata_t` 控制：`has_time_spec` + `time_spec` 可让样本在指定的硬件时刻发出（定时发送）；`start_of_burst` / `end_of_burst` 标记一个突发的起止（突发发送）。这些元数据细节将在 u2-l7 结合 `tx_waveforms` 详讲。

#### 4.4.3 源码精读

`tx_streamer` 类定义与 `sptr`：

- [host/include/uhd/stream.hpp:360-374](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/stream.hpp#L360-L374)：`typedef std::shared_ptr<tx_streamer> sptr;`，以及 `get_num_channels`、`get_max_num_samps`、`buffs_type = ref_vector<const void*>`（注意 `const`）。

`send` 的声明与分片/线程安全文档：

- [host/include/uhd/stream.hpp:376-407](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/stream.hpp#L376-L407)：注释明确——分片会尊重 burst 标志；阻塞调用；同样**非线程安全**（同一流器不可并发 `send`）。

`recv_async_msg`：

- [host/include/uhd/stream.hpp:409-416](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/stream.hpp#L409-L416)：返回 `true` 表示 `async_metadata` 有效（收到一条异步消息），`false` 表示超时。欠流等事件即由此读取。

`post_output_action`：

- [host/include/uhd/stream.hpp:418-424](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/stream.hpp#L418-L424)：RFNoC 发送流器向输出边投递 action。

注意 `device::recv_async_msg` 已被标记**废弃**：

- [host/include/uhd/device.hpp:98-109](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/device.hpp#L98-L109)：`device` 上还有一个 `recv_async_msg`，但注释标明 DEPRECATED，因为它无法确定该回报属于哪个 TX 流器。应改用对应 `tx_streamer` 的 `recv_async_msg`。

#### 4.4.4 代码实践

**实践目标**：对比 `send` 与 `recv` 的签名差异，理解收发两侧的对称设计。

**操作步骤**：

1. 对照 [stream.hpp:327-331](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/stream.hpp#L327-L331)（`recv`）与 [stream.hpp:404-407](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/stream.hpp#L404-L407)（`send`），逐个参数比较异同。
2. 用下表记录差异：

   | 维度 | `recv` | `send` |
   | --- | --- | --- |
   | buffs 类型 | `ref_vector<void*>`（可写出参） | `ref_vector<const void*>`（只读入参） |
   | metadata | `rx_metadata_t&`（出参，填错误码/时间戳） | `const tx_metadata_t&`（入参，带时间/爆发标志） |
   | 额外参数 | `one_packet` | 无 |
   | 异步事件 | 无（错误在 md.error_code） | 有（`recv_async_msg` 单独读欠流等） |

**需要观察的现象 / 预期结果**：你会清楚地看到「接收把元数据当出参填、发送把元数据当入参传」这一对称反转，这正是收发流器最核心的设计差异。

> 待本地验证：阅读 [host/examples/tx_waveforms.cpp](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/tx_waveforms.cpp) 中 `recv_async_msg` 的用法，确认它被放在一个独立线程里循环排空欠流事件（u2-l7 会精讲）。

#### 4.4.5 小练习与答案

**练习 1**：发送侧出现 underflow 时，`send` 的返回值或同步元数据能直接反映吗？

**参考答案**：不能。underflow 是**异步事件**，不会体现在 `send` 的返回值或 `tx_metadata_t` 里。必须调用 `tx_streamer->recv_async_msg(...)` 读取异步消息，其 `event_code` 才会指示欠流。这也是为什么发送程序常用一个独立线程专门排空 `recv_async_msg`。

**练习 2**：`rx_streamer::buffs_type` 与 `tx_streamer::buffs_type` 的唯一区别是什么？为什么？

**参考答案**：前者是 `ref_vector<void*>`，后者是 `ref_vector<const void*>`。区别在 `const`：接收要把样本**写进**主机缓冲（缓冲可写），发送要从主机缓冲**读出**样本（缓冲只读）。`const` 在类型层面表达了这一语义约束。

**练习 3**：为什么 `device::recv_async_msg` 被标为 DEPRECATED？

**参考答案**：因为它作用在 `device` 层级，无法确定一条异步回报究竟属于哪一个 TX 流器（多流器场景下会混淆）。正确做法是在具体的 `tx_streamer` 上调用 `recv_async_msg`，这样回报与流器一一对应（见 [device.hpp:98-103](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/device.hpp#L98-L103) 的废弃说明）。

---

## 5. 综合实践

**综合任务**：把本讲四个最小模块串起来，写一段「双通道接收」的最小骨架（伪代码 + 关键 API），并回答缓冲规模问题。

**示例代码**（非项目原码，综合演示用）：

```cpp
#include <uhd/stream.hpp>
#include <uhd/usrp/multi_usrp.hpp>
#include <uhd/types/stream_cmd.hpp>
#include <vector>
#include <complex>

// 假设 usrp 已构造并配好采样率/频率/增益（见 u2-l3、u1-l6）
uhd::usrp::multi_usrp::sptr usrp = /* ... */;

// ---- 模块 stream_args_t ----
uhd::stream_args_t stream_args("fc32", "sc16"); // 主机 complex<float>，线缆 16 位定点
stream_args.channels = {0, 1};                  // 双通道 → 流器将有 2 个通道

// ---- 模块 rx_streamer：构造 ----
uhd::rx_streamer::sptr rx_stream = usrp->get_rx_stream(stream_args);

// ---- 模块 rx_streamer：按通道数与每包上限分配缓冲 ----
const size_t nch    = rx_stream->get_num_channels();      // == 2
const size_t samps  = rx_stream->get_max_num_samps();     // 每缓冲上限
std::vector<std::vector<std::complex<float>>> mem(
    nch, std::vector<std::complex<float>>(samps));
std::vector<void*> buffs;
for (size_t ch = 0; ch < nch; ++ch) {
    buffs.push_back(mem[ch].data());                       // 每通道一个缓冲指针
}
// 此时 buffs.size() == 2，即「2 个接收缓冲」

// ---- 模块 rx_streamer：点火 + 接收循环 ----
uhd::stream_cmd_t cmd(uhd::stream_cmd_t::STREAM_MODE_START_CONTINUOUS);
cmd.stream_now = true;
rx_stream->issue_stream_cmd(cmd);

uhd::rx_metadata_t md;
for (int i = 0; i < 100; ++i) {
    size_t got = rx_stream->recv(buffs, samps, md, 3.0);  // buffs 含 2 个缓冲
    // 检查 md.error_code：TIMEOUT/OVERFLOW/ALIGNMENT/BAD_PACKET...
    // mem[0]、mem[1] 分别是通道 0、通道 1 的样本
}
```

**操作步骤**：

1. 对照 [rx_samples_to_file.cpp:163-176](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/rx_samples_to_file.cpp#L163-L176)，确认上面的 `stream_args` → `get_rx_stream` → `get_num_channels` → 分配缓冲的顺序与真实示例一致。
2. 在纸上回答三个问题：
   - 流器有几个通道？（答：2，因为 `channels` 有 2 个元素）
   - `recv` 要传几个缓冲？（答：2，每通道一个）
   - 每个缓冲应开多大？（答：至少 `get_max_num_samps()` 个样本）
3. **进阶**：把 `channels` 改成 `{1, 0}`，说明 `mem[0]` 现在对应哪个物理通道（答：物理通道 1）。

**需要观察的现象 / 预期结果**：通道数、缓冲数、`channels` 大小三者严格相等；这正是贯穿本讲的核心不变量。若在真实硬件上运行，双通道会产出两路样本（示例里对应两个输出文件）。

> 待本地验证：上述骨架未在硬件上运行；若需实跑，建议直接复用 `rx_samples_to_file`（它已正确处理多通道文件名、溢出、信号中断等），用 `--channels "0,1" --wire sc16` 观察行为。

## 6. 本讲小结

- `stream.hpp` 是**纯接口头**：声明 `stream_args_t`、`rx_streamer`、`tx_streamer`；`stream.cpp` 只有空析构函数，真正的 `recv`/`send` 实现在各设备驱动里。
- `stream_args_t` 用**字符串描述格式**（类型擦除）：`cpu_format` 描述主机内存类型，`otw_format` 描述线缆类型，命名约定为「复数/实数 + 浮点/整数 + bit 数」（如 `fc32`=complex<float>、`sc16`=complex<int16_t>）。
- `channels` 字段决定流器通道数，进而决定每次 `recv`/`send` 要传**几个缓冲**——`channels={0,1}` ⇒ 2 个缓冲（核心不变量）。RFNoC 设备例外，改用 `create_rx_streamer(num_ports)`。
- `rx_streamer` 的 `recv` 阻塞接收，返回实际样本数；`timeout` 是「每次内部收包」的超时而非总超时；`recv` 非线程安全。接收前要 `issue_stream_cmd` 点火。
- `tx_streamer` 的 `send` 阻塞发送并自带分片与 burst 标志；欠流等异步事件须经 `recv_async_msg` 读取，`device::recv_async_msg` 已废弃。
- `buffs_type` 是 `ref_vector<void*>`（rx，可写）与 `ref_vector<const void*>`（tx，只读），`ref_vector` 是不管理内存的数组视图，支持「单指针（单通道）」或「vector（多通道）」两种传参方式。

## 7. 下一步学习建议

本讲只讲了**接口契约**，下一步应该进入**真实收发循环**与**元数据细节**：

- **u2-l6 接收流与元数据**：结合 `rx_samples_to_file` 的 `recv` 循环，精讲 `rx_metadata_t`（时间戳、`error_code` 各标志、fragment 字段）与溢出/超时的实战处理。这是本讲 `recv` 的自然延伸。
- **u2-l7 发送流与波形生成**：以 `tx_waveforms` 精讲 `send`、`tx_metadata_t`（`has_time_spec`、`start_of_burst`/`end_of_burst`）、`wavetable` 与欠流线程，对应本讲 `tx_streamer` 的深入。
- **u4-l1 convert 子系统**：本讲反复提到「fc32 ↔ sc16 自动转换」，那里才是转换器注册与 SSE2/AVX2/NEON 优化的源头，读完能彻底理解 `cpu_format`/`otw_format` 组合背后的实现。
- 若你关心 RFNoC：直接跳到 **u3-l1**，看 `rfnoc_graph` 如何用 `create_rx_streamer(num_ports)` 取代 `stream_args.channels`。
