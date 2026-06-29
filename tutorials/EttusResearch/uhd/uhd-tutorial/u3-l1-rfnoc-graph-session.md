# RFNoC 架构与 rfnoc_graph 会话

## 1. 本讲目标

学完本讲，读者应当能够：

1. 用一句话说清 **RFNoC（RF Network on Chip，片上射频网络）** 解决了什么问题，以及「块（block）」「流端点（SEP）」「CHDR 通路」这些名词的含义。
2. 理解 `rfnoc_graph` 为什么是 `uhd::device` 的「超集会话」，以及它相对 `device::make` 多出来了哪些能力。
3. 掌握 `rfnoc_graph::make(...)` 的工厂入口：它内部如何调用 `device::make`、如何把设备升级成 RFNoC 会话、又如何保证「一台设备只对应一张图」。
4. 看懂一个 RFNoC 程序的骨架：建图 → 取块控制器 → 连接 → 提交（commit）→ 收发。

本讲是 RFNoC 单元（u3）的第一讲，只讲「会话入口」这一层，**不深入单个块的实现细节**——那是 u3-l2 之后的内容。

## 2. 前置知识

本讲承接 u2 单元已建立的概念，重点承接以下几条：

- **设备工厂（u2-l1）**：`uhd::device::find` 发现设备、`uhd::device::make` 制造并打开设备，背后是 `register_device` 的自注册机制。本讲的 `rfnoc_graph::make` 就建立在 `device::make` 之上。
- **device_addr_t（u2-l2）**：键值对地址容器，`make` 的入参就是它。
- **multi_usrp（u2-l3）**：高层封装层。对 RFNoC 设备，`multi_usrp::make` 内部用的正是本讲要讲的 `rfnoc_graph`。本讲会补上「device → rfnoc_graph → multi_usrp」这条链的中间一环。
- **property_tree（u2-l4）**：树状配置中枢，`rfnoc_graph` 同样提供 `get_tree()` 访问它。
- **流式 API（u2-l5）**：`stream_args_t`、`rx_streamer`/`tx_streamer`、`cpu_format`/`otw_format`。在 RFNoC 里，流器不是用 `get_rx_stream` 取的，而是用 `rfnoc_graph::create_rx_streamer` 创建，再**手动连进图里**——这是本讲的一个关键差异。

如果你对 RFNoC 这个词完全陌生，不用担心，下一节会从零讲起。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [host/include/uhd/rfnoc_graph.hpp](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/rfnoc_graph.hpp) | `rfnoc_graph` 抽象类的公共接口：工厂、块发现、连接、流器创建、硬件控制。本讲的主轴。 |
| [host/lib/rfnoc/rfnoc_graph.cpp](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/rfnoc_graph.cpp) | `rfnoc_graph_impl` 实现 + `make`/`make_rfnoc_graph` 工厂。包含构造期的完整初始化链。 |
| [host/lib/include/uhdlib/rfnoc/rfnoc_device.hpp](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/include/uhdlib/rfnoc/rfnoc_device.hpp) | `detail::rfnoc_device`：继承自 `uhd::device`、增加了 RFNoC 能力的内部抽象，是 `make` 转型的目标类型。 |
| [host/include/uhd/rfnoc/graph_edge.hpp](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/rfnoc/graph_edge.hpp) | `graph_edge_t`：描述图中的「边」及其四种子类型（STATIC / DYNAMIC / RX_STREAM / TX_STREAM）。 |
| [host/include/uhd/rfnoc/rfnoc_types.hpp](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/rfnoc/rfnoc_types.hpp) | `chdr_w_t`（CHDR 位宽枚举）、`sep_addr_t` 等 RFNoC 基础类型。 |
| [host/examples/rfnoc_rx_to_file.cpp](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/rfnoc_rx_to_file.cpp) | 一个完整可编译的 RFNoC 接收示例，本讲用它串起整条会话流程。 |

> 说明：`rfnoc_device.hpp` 与 `rfnoc_graph.cpp` 中引用的 `uhdlib/...` 头文件属于**内部头**（安装时不对外发布），但它们真实存在于源码树，阅读源码时可以直接看。

## 4. 核心概念与源码讲解

### 4.1 RFNoC 理念：片上射频网络与块图模型

#### 4.1.1 概念说明

**RFNoC** 的全称是 **RF Network on Chip（片上射频网络）**。要理解它，先看它取代了什么。

早期 USRP 的 FPGA 内部是一条**固定**的信号流水线：射频前端 → 数字下变频（DDC）/上变频（DUC）→ 与主机收发的数据口。如果你想在中途插入一个 FFT 或 FIR 滤波，就只能**重新综合整个 FPGA 镜像**，门槛极高。

RFNoC 把 FPGA 内部改造成一个**可路由的块网络**：

- **块（block）**：FPGA 里的一个功能单元，例如 Radio（射频前端）、DDC、DUC、FFT、FIR、Replay、Null 等。每个块在硬件里有一个编号叫 **NoC ID（`noc_id`）** 标识其类型；在软件里用一个字符串 **block_id** 寻址，格式为「主板号/块名#实例号」，例如 `0/Radio#0`、`0/DDC#1`、`0/FFT#0`。
- **流端点（SEP，Stream EndPoint）**：块网络的「网口」。块的输入/输出端口接到 SEP 上，SEP 之间通过**交叉开关（crossbar）**路由数据。当一个块的输出要送到主机，也先经过一个 SEP。
- **CHDR**：在片上网络里搬运样本的包协议（可理解为「带元数据的样本快递包」）。它的数据位宽 `chdr_w_t` 有 64/128/256/512 比特几档（见 `rfnoc_types.hpp`）。

于是，用户可以在**主机软件里**描述一张数据流图（例如 `Radio → DDC → FFT → 主机`），再让 UHD 把这张图「提交」到 FPGA，由框架配置交叉开关路由、并把采样率等属性沿图传播。**只要这些块已经存在于 FPGA 镜像中，就不必重新综合 FPGA。** 这就是 RFNoC 的核心价值：把 FPGA 信号链从「硬件固定」变成「软件可重构」。

> 注意区分两个层次：块**本身**（Radio、FFT 的 HDL 实现）仍要在 FPGA 镜像里；RFNoC 改变的是它们之间的**连接关系**可以在软件里动态描述。

#### 4.1.2 核心流程

一个 RFNoC 数据通路的抽象流程：

```
天线 ──> Radio 块 ──(静态边)==> DDC 块 ──(动态边/SEP)--> [crossbar] --> SEP ──(RX_STREAM)--> 主机 rx_streamer
```

从软件视角，使用 RFNoC 的典型步骤是：

1. **建图**：`rfnoc_graph::make(args)` 打开设备并构造会话。
2. **取块**：用 `get_block<块类型>(block_id)` 拿到某个块的控制句柄（C++ 对象）。
3. **连接**：用 `connect(...)` 把块与块、块与流器连成图。
4. **提交**：`commit()` 让框架校验图、配置物理路由、传播属性。
5. **配置/收发**：在块上设置频率/采样率，用流器 `recv`/`send`。

这里出现三种「边」，后续会反复用到：

- **STATIC**：FPGA 里**综合时已物理焊死**的连接（如 Radio→DDC）。
- **DYNAMIC**：运行时**经 crossbar 动态路由**的块间连接。
- **RX_STREAM / TX_STREAM**：块与主机软件流器之间的连接。

#### 4.1.3 源码精读

边的四种类型定义在 `graph_edge.hpp`：

[host/include/uhd/rfnoc/graph_edge.hpp:25-30](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/rfnoc/graph_edge.hpp#L25-L30) —— 定义 `edge_t` 枚举，区分四种边，注释说明了各自含义。

边的字符串表示里，**静态边用 `==>`，其余用 `-->`**，这正是示例程序输出里会看到的样子：

[host/include/uhd/rfnoc/graph_edge.hpp:83-88](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/rfnoc/graph_edge.hpp#L83-L88) —— `to_string()` 用 `==>` 表示 STATIC、`-->` 表示其它，所以 `0/Radio#0:0==>0/DDC#0:0` 表示静态连接，`0/DDC#0:0-->RxStreamer#0:0` 表示到流器的连接。

CHDR 位宽枚举（影响包格式与吞吐）：

[host/include/uhd/rfnoc/rfnoc_types.hpp:19-22](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/rfnoc/rfnoc_types.hpp#L19-L22) —— `chdr_w_t` 枚举 64/128/256/512 比特四档，并提供 `chdr_w_to_bits()` 转成位数。`rfnoc_graph` 要求一张图里所有主板的 CHDR 位宽必须一致（否则报「Non-homogenous devices」）。

#### 4.1.4 代码实践

**实践目标**：通过阅读示例程序的文档字符串，直观感受 RFNoC 图的输出形态，建立「边类型」的感性认识。

**操作步骤**：

1. 打开 [host/examples/rfnoc_rx_to_file.cpp](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/rfnoc_rx_to_file.cpp)，定位到程序文档里给出的三个「Active connections」示例（约 L276-L300）。
2. 阅读这三段输出，对照 `graph_edge.hpp` 的 `to_string()` 规则。

**需要观察的现象**：

- 第一段示例里出现 `0/Radio#0:0==>0/DDC#0:0`（`==>`）和 `0/DDC#0:0-->RxStreamer#0:0`（`-->`）两种箭头。
- 第三段示例插入 FFT 后变成 `0/DDC#0:0-->0/FFT#0:0-->RxStreamer#0:0`。

**预期结果**：你能解释——Radio 到 DDC 是 FPGA 里**静态焊死**的（`==>`），所以永远连在一起；而 DDC 到流器（以及插入的 FFT）是**运行时动态连接**的（`-->`），由软件决定走哪条路。

**待本地验证**：若有 RFNoC 设备，运行 `rfnoc_rx_to_file --args "addr=..." --freq 2.4e09 --rate 10e06 --duration 0.01`，确认实际打印的连接与文档示例一致。

#### 4.1.5 小练习与答案

**练习 1**：为什么说 RFNoC 让「插入一个 FFT」不再需要重新综合 FPGA？

> **参考答案**：因为 FFT 块的 HDL 实现已经预先综合进 FPGA 镜像；RFNoC 把块间连接做成可路由的片上网络，软件只需 `connect` 出新路径并 `commit`，由框架配置 crossbar 路由，不必改动 HDL。前提是目标块已经在镜像里。

**练习 2**：`0/Radio#0:0==>0/DDC#0:0` 和 `0/DDC#0:0-->RxStreamer#0:0` 各是哪种边类型？

> **参考答案**：前者是 `STATIC`（`==>`，FPGA 综合期固定）；后者是 `RX_STREAM`（`-->`，块到主机 RX 流器）。

---

### 4.2 rfnoc_graph：device 的会话超集

#### 4.2.1 概念说明

`rfnoc_graph` 是「一次 RFNoC 会话」在 C++ 层的核心对象。类的文档注释直白地说明了它的定位——**它是 `uhd::device` 的超集**：

[host/include/uhd/rfnoc_graph.hpp:27-32](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/rfnoc_graph.hpp#L27-L32) —— 注释说明：`rfnoc_graph` 不仅仅持有一次设备会话，还管理这些设备上的 RFNoC 块；只有兼容现代 RFNoC 版本的设备才能被这个类寻址。

为什么说是「超集」？因为 `device` 只给你「一台打开的设备」，而 `rfnoc_graph` 在此之上额外提供：

- **块管理**：枚举、查找、按类型取回 FPGA 里的块控制器。
- **图连接**：把块与块、块与流器连成数据流图，并提交。
- **流器创建**：`create_rx_streamer`/`create_tx_streamer`（替代老 API 的 `get_rx_stream`）。
- **主板控制**：`get_mb_controller` 统一管理时间/时钟/参考。
- **底层访问**：`get_tree()`（属性树）、`get_chdr_width()` 等。

它的继承设计有两点值得注意：

[host/include/uhd/rfnoc_graph.hpp:33-35](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/rfnoc_graph.hpp#L33-L35) —— `rfnoc_graph` 继承 `noncopyable`（不可拷贝，会话是唯一资源）与 `enable_shared_from_this`（实现内部能安全地把 `shared_ptr<自己>` 交给流器等子对象，保证生命周期）。

#### 4.2.2 核心流程

把 `rfnoc_graph` 的公共方法按职责分成四大类，就构成了它的全部能力面：

| 类别 | 代表方法 | 作用 |
| --- | --- | --- |
| **块发现/取回** | `find_blocks`、`has_block`、`get_block` | 按 block_id 提示或类型查找/取回块控制器 |
| **连接管理** | `connect`（3 种重载）、`disconnect`、`commit`、`release`、`enumerate_active_connections`、`to_dot` | 描述并提交数据流图 |
| **流式** | `create_rx_streamer`、`create_tx_streamer` | 创建尚未连接的收/发流器 |
| **硬件控制** | `get_num_mboards`、`get_mb_controller`、`synchronize_devices`、`get_tree`、`get_chdr_width` | 主板级控制与底层访问 |

这里有几个**与 `device` 截然不同、`rfnoc_graph` 独有**的设计要点：

1. **块控制器是类型化对象**：`get_block` 有模板版本，能把通用基类 `noc_block_base` 安全地 `dynamic_pointer_cast` 成具体子类（如 `radio_control`），从而调用该块特有的方法。
2. **流器要手动连进图**：`create_rx_streamer` 返回的流器「什么都没连」，必须再调一次 `connect(src_blk, src_port, rx_streamer, strm_port)` 才能收数据。这与 u2-l5 讲过的「`get_rx_stream` 直接可用」形成对比。
3. **图必须 `commit`**：连接完成后调用 `commit()`，框架才会校验图、配置物理路由并做属性传播。在此之前块上的某些设置可能尚未生效。

#### 4.2.3 源码精读

块发现/取回这一组接口（注意模板版在头文件内联实现，非模板版是纯虚函数）：

[host/include/uhd/rfnoc_graph.hpp:59-153](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/rfnoc_graph.hpp#L59-L153) —— 这一段定义了 `find_blocks`/`has_block`/`get_block` 的非模板纯虚版本与带类型检查的模板版本。模板版 `get_block<T>` 在转型失败时抛 `uhd::lookup_error`，错误信息里用 `boost::units::detail::demangle` 打印期望类型的可读名字。

连接管理这一组（含三类 `connect` 重载和 `commit`/`release`）：

[host/include/uhd/rfnoc_graph.hpp:155-337](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/rfnoc_graph.hpp#L155-L337) —— 含 `is_connectable`、块到块的 `connect`、TX 流器到块、块到 RX 流器的 `connect`、若干 `disconnect` 重载、`enumerate_*_connections`、`commit`/`release`、`to_dot`。注意 `commit()` 的注释：它「运行图检查并触发一次属性传播」，属性解析失败会抛 `uhd::resolve_error`。

流器创建与硬件控制：

[host/include/uhd/rfnoc_graph.hpp:339-453](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/rfnoc_graph.hpp#L339-L453) —— `create_rx_streamer`/`create_tx_streamer` 的注释都强调「创建出的流器尚未连接，需要再调 `graph::connect`」；硬件控制组提供主板数量、主板控制器、设备同步、属性树、CHDR 位宽等。

#### 4.2.4 代码实践

**实践目标**：动手把 `rfnoc_graph` 的全部公共方法归类，建立「它比 `device` 多了什么」的清单（这也是本讲总实践任务的一部分）。

**操作步骤**：

1. 打开 `host/include/uhd/rfnoc_graph.hpp`，从 L36 扫到 L453。
2. 找出所有 `virtual ... = 0`（纯虚方法）和带 `template` 的内联方法。
3. 把它们填进 4.2.2 的四类表格里。

**需要观察的现象**：

- 纯虚方法集中在「块发现」「连接」「流器创建」「硬件控制」四块，工厂 `make` 是唯一的非虚静态方法。
- 模板方法（`find_blocks<T>`、`has_block<T>`、`get_block<T>`）都建立在对应非虚方法之上，只多做了一层类型筛选/转型。

**预期结果**：你能列出 `rfnoc_graph` 独有、`device` 没有的方法，例如 `find_blocks`、`has_block`、`get_block`、`connect`、`disconnect`、`commit`、`release`、`create_rx_streamer`、`create_tx_streamer`、`get_mb_controller`、`synchronize_devices`、`get_chdr_width`、`to_dot` 等。

#### 4.2.5 小练习与答案

**练习 1**：`rfnoc_graph` 继承 `noncopyable` 和 `enable_shared_from_this`，分别是为了解决什么问题？

> **参考答案**：`noncopyable` 禁止拷贝，因为一次会话对应独占的设备/图资源，拷贝会导致双重管理；`enable_shared_from_this` 让实现内部能安全地把「指向自己的 `shared_ptr`」交给流器等子对象（如流器析构时回调图的 `disconnect`），避免使用裸 `this` 造成悬空。

**练习 2**：为什么 RFNoC 用 `create_rx_streamer` 而不是 u2-l5 讲的 `get_rx_stream`？

> **参考答案**：RFNoC 的流器必须先创建、再显式 `connect` 进数据流图、最后 `commit`，才能确定它从哪个块的哪个端口取数。`create_rx_streamer` 只负责「造一个空流器」，把「连到哪」交给图的 `connect`，更符合 RFNoC 的「先描述图再提交」模型。

---

### 4.3 rfnoc_graph::make：会话工厂与初始化链

#### 4.3.1 概念说明

`rfnoc_graph::make(const device_addr_t&)` 是创建会话的唯一公开入口。它的核心思想是：

> **先借助 `device::make` 打开设备，再把设备「升级」成 RFNoC 会话。**

关键在于「升级」这一步是一次**类型检查**：`device::make` 返回的是通用 `device::sptr`，`rfnoc_graph::make` 用 `dynamic_pointer_cast` 把它转成 `detail::rfnoc_device`。只有**真正具备 RFNoC 能力的现代设备**才能通过这次转型；老设备（转型失败）会被直接拒绝并抛 `uhd::key_error`。

`detail::rfnoc_device` 是一个**内部抽象**，它继承自 `uhd::device`，并在其上增加了 RFNoC 专用的访问接口：

[host/lib/include/uhdlib/rfnoc/rfnoc_device.hpp:20-21](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/include/uhdlib/rfnoc/rfnoc_device.hpp#L20-L21) —— `class rfnoc_device : public uhd::device`，声明它是 `device` 的派生类，并新增 `get_mb_iface()`、`get_mb_controller()` 等 RFNoC 专用接口。

另一个重要设计是**「一台设备只对应一张图」的缓存**：内部用一个以 `weak_ptr<rfnoc_device>` 为键、`weak_ptr<rfnoc_graph>` 为值的静态映射，保证对同一设备反复 `make` 拿到的是同一个 `rfnoc_graph` 对象。这与 u2-l1 讲过的「`make` 用 `weak_ptr` 缓存实现设备复用」是同一种思路，只是这里缓存的是「图」而非「设备」。

#### 4.3.2 核心流程

`rfnoc_graph::make` 的执行链路：

```
rfnoc_graph::make(device_addr)
   │
   ├─ uhd::device::make(device_addr)          // u2-l1 的设备工厂，打开设备
   │       返回 device::sptr
   ├─ dynamic_pointer_cast<rfnoc_device>(...)  // 类型检查：是否 RFNoC 设备？
   │       失败 ─────────────────────────────►  抛 uhd::key_error
   └─ detail::make_rfnoc_graph(dev, device_addr)
           │
           ├─ 查 dev_to_graph 缓存             // 同一设备已有图？直接返回
           └─ 否则 new rfnoc_graph_impl(dev, device_addr)  // 触发完整初始化链
```

`rfnoc_graph_impl` 构造函数里的初始化链（这是整张图「从无到有」的过程）：

1. 保存底层 `_device`、属性树 `_tree`，数主板数 `_num_mboards`。
2. `_init_io_srv_mgr`：建立全局 I/O 服务管理器（管理主机与设备的传输）。
3. `_init_mb_controllers`：为每块主板创建主板控制器。
4. `_init_gsm`：建立**图流管理器（GSM）**，并校验所有主板的 CHDR 位宽与字节序一致（异构则报错）。
5. 逐主板 `_init_blocks`：通过 **Client Zero**（设备上的控制端点）枚举 FPGA 里所有块，为每个块造出对应的 C++ 块控制器并注册。
6. `_block_registry->init_props()`：初始化块属性。
7. `_init_sep_map`：登记所有流端点（SEP）的地址。
8. `_init_static_connections`：从设备读回 FPGA 里的**静态连接表**（adjacency list）。
9. `_init_mbc`：初始化主板控制器。
10. `synchronize_devices(0.0, quiet=true)`：以零时刻做一次初始同步（允许失败，静默）。

若中途任何一步抛异常，会先把已注册的块 `shutdown` 再重新抛出，最后外层 catch 把异常统一包装成 `runtime_error("Failure to create rfnoc_graph.")`。

#### 4.3.3 源码精读

公开工厂 `make`——注意它如何复用 `device::make` 并做类型检查：

[host/lib/rfnoc/rfnoc_graph.cpp:1129-1138](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/rfnoc_graph.cpp#L1129-L1138) —— `rfnoc_graph::make` 先 `uhd::device::make(device_addr)` 打开设备，再 `dynamic_pointer_cast<detail::rfnoc_device>` 转型；转型结果为空就抛 `key_error("No RFNoC devices found ...")`，否则交给 `detail::make_rfnoc_graph`。

带缓存的内部工厂 `make_rfnoc_graph`——「一设备一图」的实现：

[host/lib/rfnoc/rfnoc_graph.cpp:1101-1123](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/rfnoc_graph.cpp#L1101-L1123) —— 静态映射 `dev_to_graph`（`weak_ptr<rfnoc_device>` → `weak_ptr<rfnoc_graph>`），在互斥锁保护下：若该设备已有存活图就返回它，否则 `make_shared<rfnoc_graph_impl>` 新建并登记。

对比 `device::make` 的签名（u2-l1 讲过）：

[host/include/uhd/device.hpp:73-74](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/device.hpp#L73-L74) —— `device::make(hint, filter=ANY, which=0)` 返回通用 `device::sptr`，不涉及任何 RFNoC 概念。这正是 `rfnoc_graph::make` 要在其上「加料」的基础。

构造函数与初始化链（精简看关键调用顺序）：

[host/lib/rfnoc/rfnoc_graph.cpp:86-120](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/rfnoc_graph.cpp#L86-L120) —— 构造函数初始化各成员后，依次 `_init_io_srv_mgr` → `_init_mb_controllers` → `_init_gsm`，再在 try 块里逐主板 `_init_blocks`、`init_props`、`_init_sep_map`、`_init_static_connections`、`_init_mbc`、`synchronize_devices(0.0, true)`；catch 块先 `_block_registry->shutdown()` 再抛，最外层把任何异常包成 `runtime_error("Failure to create rfnoc_graph.")`。

「逐主板枚举块」是初始化链里信息量最大的一步，节选其骨架：

[host/lib/rfnoc/rfnoc_graph.cpp:696-790](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/rfnoc_graph.cpp#L696-L790) —— `_init_blocks` 通过 Client Zero 拿到每块主板的块数量与每个块的 `noc_id`，用 `factory::get_block_factory(noc_id, device_type)` 查到对应的 C++ 工厂函数，组装 `make_args`（含 block_id、端口数、MTU、寄存器接口、时钟接口、属性树子树等），造出块控制器并注册到 `_block_registry`。这解释了「FPGA 里有什么块，软件就有对应的控制器」。

最后看 `make` 与 `multi_usrp` 的关系——补上 u2-l3 留下的中间一环：

[host/lib/usrp/multi_usrp_rfnoc.cpp:2887-2914](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/multi_usrp_rfnoc.cpp#L2887-L2914) —— `make_rfnoc_device` 先 `make_rfnoc_graph(...)` 得到图，再用「图 → multi_usrp」的静态缓存保证「一图一 multi_usrp」，最后 `make_shared<multi_usrp_rfnoc>(graph, dev_addr)`。所以完整链是：`rfnoc_device`（设备）→ `rfnoc_graph`（图/会话）→ `multi_usrp_rfnoc`（高层封装），三层各自有 weak_ptr 缓存保证唯一性。

#### 4.3.4 代码实践

**实践目标**（本讲的核心实践任务）：对比 `device::make` 与 `rfnoc_graph::make`，弄清后者的「升级」过程，并列出 `rfnoc_graph` 独有的方法。

**操作步骤**：

1. 并排打开 [host/include/uhd/device.hpp](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/device.hpp) 与 [host/include/uhd/rfnoc_graph.hpp](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/rfnoc_graph.hpp)。
2. 阅读 [rfnoc_graph.cpp:1129-1138](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/rfnoc_graph.cpp#L1129-L1138)，确认 `make` 内部确实调用了 `device::make` 并做了 `dynamic_pointer_cast<rfnoc_device>`。
3. 在 `rfnoc_graph.hpp` 里挑出 `device` 完全没有的方法，分类填表。

**参考答案（差异表）**：

| 维度 | `device::make` | `rfnoc_graph::make` |
| --- | --- | --- |
| 返回类型 | `device::sptr`（通用设备） | `rfnoc_graph::sptr`（RFNoC 会话） |
| 入参 | `hint, filter=ANY, which=0` | `device_addr_t`（仅一个） |
| 对非 RFNoC 设备 | 正常返回通用 device | `dynamic_pointer_cast` 失败，抛 `uhd::key_error` |
| 缓存粒度 | 设备级 weak_ptr 缓存 | 图级 weak_ptr 缓存（`dev_to_graph`） |
| 额外能力 | 无 | 块发现/连接/流器创建/主板控制（见 4.2） |

`rfnoc_graph` 独有（`device` 没有）的代表性方法：`find_blocks`、`has_block`、`get_block`、`connect`、`disconnect`、`commit`、`release`、`enumerate_active_connections`、`create_rx_streamer`、`create_tx_streamer`、`get_mb_controller`、`synchronize_devices`、`get_chdr_width`、`to_dot`。

**预期结果**：你能用自己的话说明——`rfnoc_graph::make` 不是另一套独立的设备发现机制，而是**复用 `device::make` 打开设备，再叠加一层 RFNoC 图管理**。

**待本地验证**：若手头有非 RFNoC 的老设备，可用示例代码尝试 `rfnoc_graph::make(args)`，预期会收到 `key_error("No RFNoC devices found ...")`。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `rfnoc_graph::make` 要用 `dynamic_pointer_cast<rfnoc_device>` 而不是 `static_pointer_cast`？

> **参考答案**：因为 `device::make` 可能返回**非** RFNoC 设备（转型目标不是 `rfnoc_device` 的派生类）。`dynamic_pointer_cast` 在类型不符时安全地返回空指针，让 `make` 能据此抛出清晰的 `key_error`；`static_pointer_cast` 不做运行期检查，会对非 RFNoC 设备给出错误且危险的指针。

**练习 2**：构造期 `_init_gsm` 为什么要校验各主板 CHDR 位宽一致？

> **参考答案**：一张 `rfnoc_graph` 里的所有块共享同一套包格式与路由逻辑，CHDR 位宽（64/128/256/512）决定了包的物理形态。若主板位宽不一，跨板路由的包无法正确解析，因此 `_init_gsm` 发现异构就直接抛 `runtime_error("Non-homogenous devices ...")`。

---

### 4.4 图的连接与提交：connect 与 commit

#### 4.4.1 概念说明

`connect` 与 `commit` 是 RFNoC 编程里使用频率最高的两个动作。

`connect` 有**三类重载**，对应图里的三种「边」：

1. **块到块**：`connect(src_blk, src_port, dst_blk, dst_port, is_back_edge=false)` —— 连接两个 RFNoC 块。
2. **TX 流器到块**：`connect(tx_streamer, strm_port, dst_blk, dst_port, adapter_id=...)` —— 把主机发送流器接到某块的输入。
3. **块到 RX 流器**：`connect(src_blk, src_port, rx_streamer, strm_port, adapter_id=...)` —— 把某块的输出接到主机接收流器。

`commit()` 则是「让图真正生效」的开关：它运行图校验、配置物理路由，并触发一次**属性传播**（例如把采样率从主机一路推到 Radio/DDC，并算出各级 DSP 缩放系数）。属性解析失败会抛 `uhd::resolve_error`。`release()` 是 `commit()` 的反面——临时关闭属性传播，用于改图后重新 `commit`。

#### 4.4.2 核心流程

一次「块到块」的 `connect` 内部分两步走：

```
connect(src_blk, src_port, dst_blk, dst_port)
   │
   ├─ has_block(src_blk) / has_block(dst_blk)        // 两个块必须都存在
   ├─ _physical_connect(...)                          // 物理层：配置 crossbar 路由
   │      └─ _get_route_info(...) 判定边的子类型：
   │           · src 静态直连 dst              ─► STATIC
   │           · src 接 SEP 且 dst 接 SEP       ─► DYNAMIC（经 crossbar 路由）
   │           · 否则                           ─► 抛 routing_error
   └─ _connect(...)                                   // 逻辑层：把边加入 BGL 图，供属性传播使用
```

要点：

- **静态边**（STATIC）是 FPGA 综合期就焊死的，`_physical_connect` 对它什么都不做，只登记到逻辑图。
- **动态边**（DYNAMIC）需要 `_gsm->create_device_to_device_data_stream(...)` 在 crossbar 上**建立一条真实路由**。
- 连**流器**时，块那一端必须接在一个 SEP 上（否则报 `routing_error("... is not connected to an SEP!")`），然后 `_gsm` 会建立主机↔设备的数据流传输。

`commit` 之后，调用 `enumerate_active_connections()` 可以回读当前图里所有「活跃边」（即所有 `connect` 产生的边），这正是示例程序最后打印的那段输出。

#### 4.4.3 源码精读

块到块 `connect` 的实现——先校验存在性，再「物理连 + 逻辑连」：

[host/lib/rfnoc/rfnoc_graph.cpp:225-248](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/rfnoc_graph.cpp#L225-L248) —— 先 `has_block` 检查源/目的块是否存在（不存在抛 `lookup_error`），再调 `_physical_connect` 拿到边类型，最后调 `_connect` 把边加入逻辑图。注意源/目的块的端口若已**静态连到别的块**，会抛 `uhd::routing_error`。

路由判定逻辑（STATIC/DYNAMIC/SEP 的分叉）：

[host/lib/rfnoc/rfnoc_graph.cpp:870-927](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/rfnoc_graph.cpp#L870-L927) —— `_get_route_info` 查源端口与目的端口的静态边：若源静态边的目的正是 `dst_blk:dst_port`，则是 STATIC；否则要求源接 SEP、目的接 SEP，才是可路由的 DYNAMIC；任一端没接 SEP 就抛 `routing_error`。

物理层建立动态路由：

[host/lib/rfnoc/rfnoc_graph.cpp:936-961](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/rfnoc_graph.cpp#L936-L961) —— `_physical_connect` 仅当边为 DYNAMIC 时，用源/目的 SEP 地址调 `_gsm->create_device_to_device_data_stream(...)` 在 crossbar 上建立路由；STATIC 则直接返回，不动物理层。

提交与释放：

[host/lib/rfnoc/rfnoc_graph.cpp:584-598](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/rfnoc_graph.cpp#L584-L598) —— `commit()` 转调 `_graph->commit()` 并打印调试用 dot 图；`release()` 转调 `_graph->release()`；`to_dot()` 返回整张图的 Graphviz 表示，便于排查连接问题。

#### 4.4.4 代码实践

**实践目标**：在真实示例里跟踪「建流器 → 连接 → 提交 → 打印活跃连接」这一段，把本模块的流程落到具体代码行。

**操作步骤**：

1. 打开 [host/examples/rfnoc_rx_to_file.cpp](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/rfnoc_rx_to_file.cpp)。
2. 定位到下列关键行（约 L532-L547）。

关键片段（项目原有代码）：

```cpp
// create a receive streamer
uhd::stream_args_t stream_args(format, "sc16");
stream_args.args = streamer_args;
auto rx_stream = graph->create_rx_streamer(1, stream_args);          // L539：创建空流器

// Connect streamer to last block and commit the graph
graph->connect(last_block_in_chain, last_port_in_chain, rx_stream, 0); // L542：块 → RX 流器
graph->commit();                                                       // L543：提交
std::cout << "Active connections:" << std::endl;
for (auto& edge : graph->enumerate_active_connections()) {            // L545：回读活跃边
    std::cout << "* " << edge.to_string() << std::endl;
}
```

**需要观察的现象**：

- `create_rx_streamer(1, ...)` 第一个参数是端口数 `1`，对应单通道接收。
- `connect(块, 块端口, rx_stream, 0)` 用的是「块到 RX 流器」重载，流器端口为 `0`。
- `commit()` 之后才 `enumerate_active_connections()`——活跃边在提交后才能完整回读。

**预期结果**：按程序文档示例，打印形如：

```
Active connections:
* 0/Radio#0:0==>0/DDC#0:0
* 0/DDC#0:0-->RxStreamer#0:0
```

第一行是静态边（Radio→DDC，`==>`），第二行是 RX_STREAM 边（DDC→流器，`-->`）。

**待本地验证**：在有 RFNoC 设备的机器上运行该示例，对照实际打印的连接与上面的预测。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `connect` 要分成「物理连」和「逻辑连」两步？

> **参考答案**：物理连（`_physical_connect`）负责在 FPGA crossbar 上建立/确认真实数据路由（动态边要建路，静态边无需动）；逻辑连（`_connect`）把这条边加入软件维护的 BGL 图，供 `commit` 时的属性传播与拓扑排序使用。两者职责不同，缺一不可：只物理连则属性传播看不到这条边，只逻辑连则数据实际流不过去。

**练习 2**：把块连到 RX 流器时，如果该块端口没有接 SEP，会发生什么？

> **参考答案**：`connect` 会进入检查分支，发现源端口静态边的对端不是 SEP，于是抛 `uhd::routing_error("... is not connected to an SEP! Routing impossible.")`。因为只有经 SEP 才能把数据送出 crossbar 到主机。

---

## 5. 综合实践

把本讲四个模块串起来，完成一次「RFNoC 会话生命周期」的源码追踪。

**任务**：以 [host/examples/rfnoc_rx_to_file.cpp](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/rfnoc_rx_to_file.cpp) 为对象，回答下面的问题清单，并画出它的 RFNoC 会话时序。

1. **会话创建**：定位 `uhd::rfnoc::rfnoc_graph::make(args)`（约 L424）。说明这一行内部依次发生了哪三件事（参考 4.3.2）。
2. **取块控制器**：定位 `graph->get_block<uhd::rfnoc::radio_control>(radio_ctrl_id)`（约 L429）。说明为什么这里必须用**模板版** `get_block` 而不是非模板版。
3. **建流器**：定位 `graph->create_rx_streamer(1, stream_args)`（约 L539）。此时流器连上了吗？为什么还要单独 `connect`？
4. **连接与提交**：定位 `connect(...)` 与 `commit()`（约 L542-L543）。说明 `commit()` 触发了什么，为什么后续的采样率/频率设置（L555-L593）要放在 `commit()` 之后。
5. **回读验证**：定位 `enumerate_active_connections()`（约 L545）。预测当用户用 `--block-id "0/FFT#0"` 插入 FFT 后，输出会比默认多出哪两行（提示：参考程序文档第三段示例）。

**预期产出**：

- 一张时序图，横轴为时间，依次标出：`make` → `get_block` → `create_rx_streamer` → `connect` → `commit` → `enumerate_active_connections` → 配置射频 → `recv`。
- 对第 5 问，预测多出 `0/DDC#0:0-->0/FFT#0:0` 与 `0/FFT#0:0-->RxStreamer#0:0` 两行（即 DDC 不再直连流器，而是经 FFT）。

**参考要点**：

- 第 2 问：必须用模板版才能拿到 `radio_control` 特有的方法（如 `set_rx_gain`、`set_rx_frequency`）；非模板版只返回通用 `noc_block_base`，调不到射频专用接口。
- 第 4 问：`commit()` 触发属性传播，使图里各块的属性（如采样率、缩放）相互协调；示例故意把频率/速率设置放在 `commit()` 之后，正是为了**借助属性传播**把主机设的速率一路推到 DDC/Radio（见 L585-L591 在 DDC 上设输出速率）。

## 6. 本讲小结

- **RFNoC** 把 FPGA 内部改造成可路由的块网络：块（block）经流端点（SEP）由 crossbar 互连，样本以 CHDR 包搬运；软件可动态描述块间连接，免重新综合 FPGA。
- **`rfnoc_graph` 是 `uhd::device` 的超集会话**：它持有设备会话，并额外提供块发现、图连接、流器创建、主板控制等能力；它不可拷贝，但支持 `shared_from_this`。
- **`rfnoc_graph::make` 复用 `device::make`**：先打开设备，再 `dynamic_pointer_cast<rfnoc_device>` 做 RFNoC 资格检查（失败抛 `key_error`），最后用 `dev_to_graph` 缓存保证「一设备一图」。
- 构造期执行一条**完整的初始化链**：I/O 服务管理器 → 主板控制器 → 图流管理器（GSM，校验同构）→ 逐主板枚举块并造控制器 → 属性初始化 → SEP 表 → 静态连接表 → 主板控制器初始化 → 初始同步。
- **`connect` 有三类重载**（块-块、TX 流器-块、块-RX 流器），内部分「物理连 + 逻辑连」两步，边分子类型 STATIC/DYNAMIC/RX_STREAM/TX_STREAM；**`commit`** 触发校验与属性传播，是图生效的开关。
- 设备层的「device → rfnoc_graph → multi_usrp_rfnoc」三层各有 weak_ptr 缓存保证唯一性；`rfnoc_graph` 是这条链的中间枢纽。

## 7. 下一步学习建议

本讲只讲了「会话入口」。建议按以下顺序继续：

1. **u3-l2 Block 控制器与注册表**：深入 `noc_block_base`、`block_id_t`、`blockdef` 与 `registry`，弄清 4.3.3 里 `factory::get_block_factory(noc_id, ...)` 是怎么把硬件 `noc_id` 映射到 C++ 块控制器的。
2. **u3-l3 流图连接与 commit**：把本讲 4.4 的 `connect`/`commit` 讲透，含 `block_container`、属性传播细节与 `release`/重配。
3. **u3-l4 mb_controller 主板控制器**：展开本讲提到的 `get_mb_controller()`、`synchronize_devices()` 背后的主板时间/时钟/参考管理。
4. 配合阅读：[host/examples/rfnoc_radio_loopback.cpp](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/rfnoc_radio_loopback.cpp) 与 [host/examples/rfnoc_replay_samples_from_file.cpp](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/rfnoc_replay_samples_from_file.cpp)，看更多 `connect`/`commit` 的真实用法。
