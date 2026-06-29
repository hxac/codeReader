# MPMD 设备实现

## 1. 本讲目标

本讲深入 UHD「现代设备」的驱动实现——MPMD（Module Peripheral Manager Device）。学完本讲你应当能够：

- 说清楚 MPMD 是什么、为什么 N3xx/E3xx/X4xx 这些设备只写一个驱动就够。
- 读懂 `mpmd_find` 的设备发现流程：广播发现 → 解析应答 → 可达性校验。
- 读懂 `mpmd_impl` 的构造链路：声明设备（claim）→ 初始化 → 建属性树，并解释「声明循环」的作用。
- 理解 `mpmd_link_if_mgr` 如何把抽象的 CHDR 链路落地为具体的 UDP 链路控制器，并把 RPC 调用挂到属性树上。

本讲承接 [u2-l1 设备发现与工厂模式](u2-l1-device-factory.md)（`device::register_device`/`find`/`make` 三件套）和 [u3-l4 mb_controller 主板控制器](u3-l4-mb-controller.md)（主板时间/参考管理）。理解本讲后，你就能回答一个关键问题：**当我 `device::make("type=x4xx")` 时，主机到底通过网络和设备端说了哪些话、建了哪些对象。**

## 2. 前置知识

在进入源码前，先用通俗语言建立几个心智模型。

### 2.1 老设备 vs 现代设备

老一代 USRP（如 B100、USRP2）的驱动把「控制逻辑」几乎全写在主机端：主机直接读写 FPGA 寄存器、直接管理子板。每加一个新型号，主机端就要写一个专门的派生类。

新一代 USRP（N3xx、E3xx、X4xx）运行一个 **完整的 Linux**，设备端常驻一个 Python 进程——**MPM（Module Peripheral Manager）**。它把射频、时钟、网络这些「外设」管起来，对主机只暴露一组 **RPC（远程过程调用）接口**。于是主机端不再需要为每个型号写专门的驱动：所有「会跑 MPM」的设备共用一个驱动——**MPMD**。

> 类比：MPMD 像是一个「统一遥控器」。不管客厅里摆的是哪款电视，只要它支持同一种遥控协议，一个遥控器就能控制。

### 2.2 三条通信通道

主机与 MPM 设备之间其实有三条并行的通信通道，务必分清：

| 通道 | 端口/方式 | 用途 | 本讲对应源码 |
|------|-----------|------|--------------|
| 发现（discovery） | UDP 广播 49600 | 设备上线广播、ping 测可达 | `mpmd_find.cpp` |
| RPC 控制 | TCP 49601 | 配置射频、读传感器、声明设备 | `rpc.hpp` + 各 `*_with_token` 调用 |
| CHDR 数据流 | UDP（链路控制器协商） | 样本高速收发、RFNoC 控制 | `mpmd_link_if_mgr` + `mpmd_mb_iface` |

发现通道用来「找到设备」，RPC 通道用来「配置和问询设备」，CHDR 通道用来「搬样本」。三者职责不重叠。

### 2.3 claim（声明）机制

MPM 设备是共享资源：网络上多台主机都能看到同一台 USRP。为防止两台主机同时控制一台设备，MPM 引入 **claim（声明）** 机制：主机拿到一个 **token（令牌）**，只有持令牌的主机才能操作设备。令牌会过期，所以主机要周期性地 **reclaim（续声明）**，否则 MPM 会认为主机掉线而释放设备。

理解了上面三点，下面读源码会顺畅很多。

## 3. 本讲源码地图

本讲涉及的关键文件及其职责：

| 文件 | 作用 |
|------|------|
| [`host/lib/usrp/mpmd/mpmd_impl.hpp`](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/mpmd/mpmd_impl.hpp) | MPMD 驱动的头文件。声明 `mpmd_impl`（整个设备）与 `mpmd_mboard_impl`（单块主板）两个核心类、RPC 超时常量、`mpmd_find` 原型。 |
| [`host/lib/usrp/mpmd/mpmd_impl.cpp`](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/mpmd/mpmd_impl.cpp) | `mpmd_impl` 的构造、自注册（`register_device`）、主板声明/初始化/属性树入口。 |
| [`host/lib/usrp/mpmd/mpmd_find.cpp`](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/mpmd/mpmd_find.cpp) | 设备发现逻辑：广播发现、地址发现、可达性校验。 |
| [`host/lib/usrp/mpmd/mpmd_link_if_mgr.cpp`](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/mpmd/mpmd_link_if_mgr.cpp) / [`.hpp`](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/mpmd/mpmd_link_if_mgr.hpp) | 链路工厂：把抽象「链路类型」落地为具体的链路控制器（当前仅 UDP）。 |
| [`host/lib/usrp/mpmd/mpmd_mboard_impl.cpp`](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/mpmd/mpmd_mboard_impl.cpp) | 单块主板的生命周期：构造即声明、构造期拉取设备/子板信息、`init()`、声明循环。 |
| [`host/lib/usrp/mpmd/mpmd_mb_iface.cpp`](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/mpmd/mpmd_mb_iface.cpp) | 主板接口：通过 RPC 分配 device id、拉取时钟与 CHDR 链路类型、驱动 `link_if_mgr` 建链。 |
| [`host/lib/usrp/mpmd/mpmd_prop_tree.cpp`](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/mpmd/mpmd_prop_tree.cpp) | 把 RPC 调用「挂」到属性树的叶子节点上（coercer/subscriber/publisher）。 |
| [`host/lib/include/uhdlib/utils/rpc.hpp`](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/include/uhdlib/utils/rpc.hpp) | `rpc_client`：封装底层 rpclib，提供 `request`/`notify`/`*_with_token`。 |
| [`host/lib/usrp/mpmd/mpmd_devices.hpp`](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/mpmd/mpmd_devices.hpp) | MPM 设备类型清单（`mpm`/`n3xx`/`e3xx`/`x4xx`），仅用于过滤。 |

> 阅读建议：先看 `mpmd_impl.cpp` 末尾的 `UHD_STATIC_BLOCK`（注册）和构造函数（主流程），再看 `mpmd_find.cpp`（发现），最后看 `mpmd_link_if_mgr` + `mpmd_prop_tree`（建链与建树）。

## 4. 核心概念与源码讲解

### 4.1 MPMD 的定位与自注册

#### 4.1.1 概念说明

回顾 [u2-l1](u2-l1-device-factory.md)：UHD 用「工厂 + 自注册」机制管理设备。每种设备实现文件用 `UHD_STATIC_BLOCK` 在 `main` 之前调用 `device::register_device(find, make, filter)`，把发现函数、制造函数、设备过滤器三元组登记进全局注册表。

MPMD 的关键设计是：**它注册的不是「某一款设备」，而是「所有跑 MPM 的设备」这一个统一驱动。** 无论 N3xx、E3xx 还是 X4xx，只要设备端在跑 MPM、能用同一种 RPC 协议对话，主机端就共用同一个 `mpmd_impl` 类。设备之间的差异（不同的射频子板、不同的时钟树）全部由设备端的 MPM 进程吸收，对主机透明。

这点直接写在头文件的类注释里——`mpmd_impl` 是「所有跑 MPM 的 USRP 的父类」，因为大部分硬件控制都由 MPM 自己处理，所以不必为每个型号单独写派生类：

> "An MPM device is a USRP running MPM. Because most of the hardware controls are taken care of by MPM itself, it is not necessary to write a specific derived class for every single type of MPM device."

#### 4.1.2 核心流程

MPMD 的「自注册」只有三行：

1. 在 `mpmd_impl.cpp` 末尾定义工厂函数 `mpmd_make`，它 `new` 一个 `mpmd_impl` 并包成 `device::sptr`。
2. 用 `UHD_STATIC_BLOCK(register_mpmd_device)` 注册 `(mpmd_find, mpmd_make, device::USRP)`。
3. 至此 MPMD 进入全局注册表，`device::find`/`device::make` 调用时就会被遍历到。

继承关系是本讲的关键骨架：`mpmd_impl` 继承自 `uhd::rfnoc::detail::rfnoc_device`，而 `rfnoc_device` 又继承自 `uhd::device`。这条继承链解释了 [u3-l1](u3-l1-rfnoc-graph-session.md) 里提到的「`rfnoc_graph::make` 用 `dynamic_pointer_cast<rfnoc_device>` 做 RFNoC 资格检查」——MPMD 设备天然是 RFNoC 设备。

```
uhd::device                         ← u2-l1 的抽象基类（find/make/register_device）
  └─ rfnoc::detail::rfnoc_device    ← 持有 property_tree + mb_controller 注册表
       └─ mpmd::mpmd_impl           ← 本讲主角：现代设备统一驱动
```

`rfnoc_device` 基类在构造时做了两件铺垫工作：把 `_type` 设为 `USRP`，并 `new` 出一棵空的 `property_tree`。后续 MPMD 的全部配置最终都写进这棵树。

#### 4.1.3 源码精读

**注册入口**（工厂 + 自注册块）：

[`mpmd_impl.cpp:266-274`](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/mpmd/mpmd_impl.cpp#L266-L274) —— `mpmd_make` 工厂函数与 `UHD_STATIC_BLOCK` 自注册。注意过滤器是 `device::USRP`（与 u2-l1 一致），表示这是一个 USRP 类设备（而非 CLOCK）：

```cpp
static device::sptr mpmd_make(const device_addr_t& device_args)
{
    return device::sptr(std::make_shared<mpmd_impl>(device_args));
}

UHD_STATIC_BLOCK(register_mpmd_device)
{
    device::register_device(&mpmd_find, &mpmd_make, device::USRP);
}
```

**继承与基类**：

[`rfnoc_device.hpp:20-29`](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/include/uhdlib/rfnoc/rfnoc_device.hpp#L20-L29) —— `rfnoc_device` 基类，构造时建空属性树，并把设备类型定为 USRP：

```cpp
class rfnoc_device : public uhd::device
{
public:
    rfnoc_device()
    {
        _type = uhd::device::USRP;
        _tree = uhd::property_tree::make();
    }
    virtual uhd::rfnoc::mb_iface& get_mb_iface(const size_t mb_idx) = 0;
    ...
};
```

[`mpmd_impl.hpp:213-242`](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/mpmd/mpmd_impl.hpp#L213-L242) —— `mpmd_impl` 继承 `rfnoc_device`，持有主板列表 `_mb`，并实现纯虚 `get_mb_iface`。

**RPC 超时常量**（理解后续 RPC 调用超时行为的基础）：

[`mpmd_impl.hpp:26-46`](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/mpmd/mpmd_impl.hpp#L26-L46) —— 一组关键常量：声明续约间隔 1000 ms、`init()` 超时 120 s、常规 RPC 2 s、声明专用 RPC 10 s。这些值会在 4.3 节反复出现。

#### 4.1.4 代码实践

**实践目标**：在源码层面确认「MPMD 是统一驱动」这一论断。

**操作步骤**：

1. 在 `host/lib/usrp/mpmd/` 下用搜索工具查找所有 `register_device` 调用，确认 MPMD 整个目录只注册了一次。
2. 在 `host/lib/usrp/` 下查找其它设备实现（如 `x300_impl.cpp`、`e300_impl.cpp`）是否也各自调用了 `register_device`，对比「每型号一个驱动」与「MPMD 一个驱动覆盖多型号」的差异。
3. 打开 [`mpmd_devices.hpp:18-19`](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/mpmd/mpmd_devices.hpp#L18-L19)，确认 `MPM_DEVICE_TYPES` 列出的所有类型都共用同一个 `mpmd_impl`。

**需要观察的现象**：MPMD 目录里只有一个 `UHD_STATIC_BLOCK`；而老设备目录里每个型号一个。

**预期结果**：你会得出结论——MPMD 用「一个注册项 + 一组 RPC 协议」覆盖了 N3xx/E3xx/X4xx 全系列。

#### 4.1.5 小练习与答案

**练习 1**：`mpmd_make` 注册时用的过滤器是 `device::USRP`。如果用户执行 `device::find(hint, device::CLOCK)`，MPMD 会被遍历到吗？

**答案**：不会。`device::find` 用过滤器与各实现自注册的 filter 匹配（见 u2-l1），`CLOCK` 与 MPMD 的 `USRP` 不匹配，所以 MPMD 的 `mpmd_find` 不会被调用。OctoClock 这类参考分发设备走的是另一个 CLOCK 过滤器的实现。

**练习 2**：为什么 `mpmd_impl` 要继承 `rfnoc_device` 而不是直接继承 `uhd::device`？

**答案**：因为现代 MPM 设备天然是 RFNoC 设备。`rfnoc_device` 在 `device` 之上增加了「属性树 + mb_controller 注册表 + `get_mb_iface` 抽象」这些 RFNoC 会话（`rfnoc_graph`）必需的能力。继承它，使得 `rfnoc_graph::make` 能用 `dynamic_pointer_cast<rfnoc_device>` 成功识别 MPMD 设备为 RFNoC 设备（见 u3-l1）。

---

### 4.2 mpmd_find：设备发现流程

#### 4.2.1 概念说明

`mpmd_find` 是注册到工厂的「发现函数」。它要回答两个问题：**网络上有多少台 MPM 设备？这些设备我能用 CHDR 协议跟它通信吗？**

回顾 [u2-l2](u2-l2-device-addr.md)：`device_addr_t` 既是 `find` 的输入提示（hint），也是返回的设备档案。MPMD 的发现过程就是「发广播 → 收应答 → 把应答解析成 `device_addr_t` → 校验可达性」。

发现用的是 4.2 节提到的「发现通道」：UDP 广播到 49600 端口，发送命令字 `MPM-DISC`，设备应答一串以 `USRP-MPM` 开头、分号分隔的键值对。

#### 4.2.2 核心流程

`mpmd_find` 的总入口区分两种场景（源码注释里写得很清楚）：

- **场景 1：用户给定了地址**（hint 含 `addr` 或 `mgmt_addr`）。直接对每个地址单点发现，不做广播。注释说「我们假设用户知道自己在做什么」，所以不再做可达性校验。
- **场景 2：用户没给地址**。在所有本机网络接口上广播 `MPM-DISC`，收集应答；然后做可达性校验，把无法用 CHDR 通信的设备过滤掉。

场景 2 的广播是 **并发** 的：对每个本机网络接口起一个 `std::async` 任务同时发广播。可达性校验也是 **并发** 的：对每台应答设备起一个任务调 `is_device_reachable`。

整个 `mpmd_find` 的伪代码：

```
mpmd_find(hint):
    1. separate_device_addr + prefs::get_usrp_args   # 拆分多板、合并默认参数
    2. 若 hint 含 type 且不在 MPM_DEVICE_TYPES 中       # 类型过滤，提前返回
       → return {}
    3. 若场景 1（给了地址）
       → return mpmd_find_with_addrs(hints)            # 单点发现，不校验可达
    4. 否则场景 2（没给地址）
       bcast_mpm_devs = mpmd_find_with_bcast(hints[0]) # 并发广播
       if check_reachability:                          # 默认查可达
           并发对每台设备调 is_device_reachable
           只保留可达的（find_all 时不可达的也保留但打 No 标记）
       else:
           仅按 RPC 版本过滤
       return filtered_mpm_devs
```

广播发现 `mpmd_find_with_addr` 的应答解析逻辑：收到字节流 → 按 `;` 切分 → 第一段必须是 `USRP-MPM` 前缀（否则丢弃，防误判）→ 其余段按 `=` 切成键值对塞进 `device_addr_t` → 记下应答来源 IP 作为 `mgmt_addr`。

#### 4.2.3 源码精读

**总入口与双场景分派**：

[`mpmd_find.cpp:198-233`](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/mpmd/mpmd_find.cpp#L198-L233) —— `mpmd_find` 入口。先做 prefs 合并与 type 过滤，再按「是否给地址」分派到场景 1 或场景 2：

```cpp
device_addrs_t mpmd_find(const device_addr_t& hint_)
{
    ...
    // 场景 1): User gave us at least one address
    if (not hints.empty()
        and (hints[0].has_key(xport::FIRST_ADDR_KEY)
             or hints[0].has_key(MGMT_ADDR_KEY))) {
        return mpmd_find_with_addrs(hints);
    }
    // 场景 2): User gave us no address, and we need to broadcast
    ...
    const auto bcast_mpm_devs = mpmd_find_with_bcast(hints[0]);
```

**发现端口与命令字**（这些常量是发现协议的「契约」）：

[`mpmd_impl.cpp:137-144`](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/mpmd/mpmd_impl.cpp#L137-L144) —— 发现端口 49600、RPC 端口 49601、发现命令 `MPM-DISC`、回声命令 `MPM-ECHO`。设备端 MPM 必须监听这些端口、识别这些命令字，发现才能成立。

**广播发现 + 应答解析**：

[`mpmd_find.cpp:56-83`](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/mpmd/mpmd_find.cpp#L56-L83) —— 发送 `MPM-DISC`、收应答、校验 `USRP-MPM` 前缀：

```cpp
transport::udp_simple::sptr comm =
    transport::udp_simple::make_broadcast(mgmt_addr, mpm_discovery_port);
comm->send(boost::asio::buffer(
    mpmd_impl::MPM_DISCOVERY_CMD.c_str(), mpmd_impl::MPM_DISCOVERY_CMD.size()));
while (true) {
    ...
    if (result[0] != MPM_DISC_RESPONSE_PREAMBLE) {  // 必须以 "USRP-MPM" 开头
        continue;
    }
```

[`mpmd_find.cpp:99-119`](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/mpmd/mpmd_find.cpp#L99-L119) —— 把应答键值对填进 `device_addr_t`，并用应答来源 IP 回填 `mgmt_addr`。`type` 先填 `"mpmd"`（注释说「hwd will overwrite this」，即设备端硬件描述会覆盖它）。

**并发广播**：

[`mpmd_find.cpp:162-180`](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/mpmd/mpmd_find.cpp#L162-L180) —— `mpmd_find_with_bcast` 对每个本机网络接口起一个 `std::async` 任务并发广播，避免在多网卡机器上串行等待。

**可达性校验分派**：

[`mpmd_find.cpp:252-294`](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/mpmd/mpmd_find.cpp#L252-L294) —— 是否校验可达由 `mpm_check_reachability` 偏好项决定（默认查）。校验时对每台设备并发调 `is_device_reachable`；`find_all` 模式下不可达的设备也会保留，但打上 `reachable=No` 标记。

**可达性细节**（这一步同时用了发现通道和 RPC 通道）：

[`mpmd_mboard_impl.cpp:177-268`](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/mpmd/mpmd_mboard_impl.cpp#L177-L268) —— `is_device_reachable` 的三段式：① 先 RPC 拉 `get_device_info`；② 若是 `connection=local`（主机就跑在设备上）直接判可达；③ 否则对设备报上来的每个 CHDR 地址（`addr`/`second_addr` 等）先做 MPM ping（发现通道，`MPM-ECHO ping`），ping 通再 RPC `get_device_info` 验序列号一致。

[`mpmd_mboard_impl.cpp:38-63`](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/mpmd/mpmd_mboard_impl.cpp#L38-L63) —— `is_pingable`：发 `MPM-ECHO ping`，比较回声长度与内容是否一致，借此快速判断设备是否在线（注释提到 rpclib 有时会无限超时，所以先用 UDP ping 探活）。

#### 4.2.4 代码实践

**实践目标**：把发现流程「画」出来，并标注每一步用的是哪条通信通道（发现/RPC）。

**操作步骤**：

1. 准备一张白纸或文本文件，画一个时序图：左是主机，右是设备 MPM。
2. 对照 [`mpmd_find.cpp:198-304`](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/mpmd/mpmd_find.cpp#L198-L304)，逐句把以下动作画成箭头并标注通道：
   - 主机 → 设备：`MPM-DISC`（UDP 49600，发现通道）
   - 设备 → 主机：`USRP-MPM;...`（应答，发现通道）
   - 主机 → 设备：`MPM-ECHO ping`（UDP，发现通道）
   - 主机 → 设备：`get_device_info`（TCP 49601，RPC 通道）
3. 在每个箭头旁注明对应的源码行号。

**需要观察的现象**：发现阶段**不持有设备**、**不 claim**、**不建立 CHDR 链路**——它只做「找 + 探活」。

**预期结果**：得到一张清晰的双通道时序图。关键结论：发现通道负责「快速找到」，RPC 通道负责「确认能配置」。

> 若无硬件，本实践为「源码阅读型实践」，无需运行命令，结果待本地结合真机验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么场景 1（用户给了地址）不做可达性校验，而场景 2（广播）要做？

**答案**：场景 1 中用户显式指定了地址，注释明确「we assume she knows what she's doing」——用户既已点名，就直接尝试连接，省去校验开销。场景 2 是广播「广撒网」，可能捞到子网里那些「能被发现但不能被本机用 CHDR 通信」的设备（比如连在不同网段、路由不通的设备），所以必须校验可达，避免 `make` 时才失败。

**练习 2**：`is_device_reachable` 为什么先做 UDP ping，再 RPC `get_device_info`，而不是直接 RPC？

**答案**：注释解释 rpclib（底层 RPC 库）在某些情况下会导致无限超时。先用轻量的 UDP `MPM-ECHO ping` 探活，ping 不通就直接判不可达、跳过昂贵的 RPC 连接，避免在不可达设备上长时间挂起。

---

### 4.3 mpmd_impl 与 mpmd_mboard_impl：构造、声明与初始化

#### 4.3.1 概念说明

发现只回答「有没有」，`make`（即 `mpmd_make` → `new mpmd_impl`）才真正「占有并配置」设备。`mpmd_impl` 把工作拆成两层：

- **`mpmd_impl`（设备级）**：管理一或多块主板（`_mb` 数组）、持有属性树、向 `rfnoc_device` 注册 mb_controller。多板设备（如两台 X4xx 组成多通道系统）在这里被统一管理。
- **`mpmd_mboard_impl`（主板级）**：对应「一块主板」。它持有该主板的 RPC 客户端、负责 claim/续声明、拉取设备/子板信息、建 mb_iface 与 mb_controller。

MPMD 的构造遵循严格的四阶段顺序（这一点与 [u1-l6](u1-l6-first-example-rx-to-file.md) 四阶段模板呼应）：**声明（claim）→ 初始化（init）→ 建属性树 → 同步时间**。每一步都依赖上一步的结果，顺序不可乱。

最特别的是 **声明循环（claim loop）**：构造期拿到 token 后，会启动一个后台任务，每秒向设备续声明一次，证明「我还活着」。这个任务在析构时随 `unclaim` 一起结束。

#### 4.3.2 核心流程

`mpmd_impl` 构造函数（精简伪代码）：

```
mpmd_impl(device_args):
    mb_args = separate_device_addr + prefs        # 拆多板、合并默认参数
    校验 RPC 版本 == "1"                            # 主机/设备协议版本必须一致
    for 每块主板 mb_i:
        _mb.push_back( claim_and_make(mb_args[mb_i]) )   # 阶段1: 声明
    if not skip_init:
        for 每块主板 mb_i:
            setup_mb(_mb[mb_i], mb_i)               # 阶段2: 初始化(compat校验+init+注册mbc)
    for 每块主板 mb_i:
        init_property_tree(_tree, /mboards/mb_i, _mb[mb_i])  # 阶段3: 建属性树
    if not skip_init and has "sync_time":
        reset_time_synchronized(_tree)              # 阶段4: 同步时间
```

主板级声明 `claim_device_and_make_task` 的流程：

```
1. _claim_rpc->request("claim", session_id)  → 返回 token
2. 若 token 为空 → 抛 "claiming failed"
3. set_token(token)  # 两个 rpc 客户端都保存 token
4. (可选) dump_logs 清空旧日志
5. return make_claim_loop_task()  # 启动每秒续声明的后台任务
```

声明循环每 `MPMD_RECLAIM_INTERVAL_MS`（1000 ms）执行一次 `claim()`（即 `reclaim` RPC）。若续声明失败且当前不允许失败，循环任务抛异常退出。

兼容性校验用 **compat number（兼容号）** 机制：主机期望 `{6, 1}`（MPM_COMPAT_NUM），设备回传实际版本。规则是「主版本必须相等，次版本设备端不能低于主机」。这是防止「新主机配老 MPM」导致行为异常的护栏。

#### 4.3.3 源码精读

**设备级构造函数（四阶段）**：

[`mpmd_impl.cpp:149-216`](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/mpmd/mpmd_impl.cpp#L149-L216) —— `mpmd_impl` 构造函数。开头的 RPC 版本校验很关键：每个主板的 `rpc_version` 必须等于 `"1"`，否则直接报错并提示「请把设备端 MPM 版本对齐主机驱动版本」。

[`mpmd_impl.cpp:179-182`](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/mpmd/mpmd_impl.cpp#L179-L182) —— 阶段 1：逐板声明。

[`mpmd_impl.cpp:184-193`](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/mpmd/mpmd_impl.cpp#L184-L193) —— 阶段 2：逐板初始化（`skip_init` 时跳过，且 compat 校验也随之跳过）。

[`mpmd_impl.cpp:201-203`](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/mpmd/mpmd_impl.cpp#L201-L203) —— 阶段 3：建属性树（必须在块初始化之后，因为块可能要访问这些属性）。

[`mpmd_impl.cpp:205-215`](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/mpmd/mpmd_impl.cpp#L205-L215) —— 阶段 4：若带 `sync_time` 则同步时间。

**声明与主板工厂**：

[`mpmd_impl.cpp:235-248`](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/mpmd/mpmd_impl.cpp#L235-L248) —— `claim_and_make`：从设备参数取 `mgmt_addr`（RPC 地址），交给 `mpmd_mboard_impl::make`。

**主板级构造（声明 + 拉信息）**：

[`mpmd_mboard_impl.cpp:273-316`](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/mpmd/mpmd_mboard_impl.cpp#L273-L316) —— `mpmd_mboard_impl` 构造函数。关键动作：建两个 RPC 客户端（`rpc` 常规用、`_claim_rpc` 声明专用，超时更长）；调 `claim_device_and_make_task()` 拿 token 并启动声明循环；RPC 拉 `get_device_info` 和 `get_dboard_info` 存进 `device_info`/`dboard_info`；最后建 `mb_iface` 和 `mpmd_mb_controller`。

```cpp
mpmd_mboard_impl::mpmd_mboard_impl(...)
    : mb_args(mb_args_)
    , rpc(make_mpm_rpc_client(rpc_server_addr, mb_args))
    , _claim_rpc(make_mpm_rpc_client(rpc_server_addr, mb_args, MPMD_CLAIMER_RPC_TIMEOUT))
    , _rpc_server_addr(rpc_server_addr)
{
    ...
    _claimer_task = claim_device_and_make_task();   // 拿 token + 启动声明循环
    ...
    const auto device_info_dict = rpc->request<dev_info>("get_device_info");  // RPC 拉信息
    ...
}
```

**claim → token → 声明循环**：

[`mpmd_mboard_impl.cpp:407-428`](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/mpmd/mpmd_mboard_impl.cpp#L407-L428) —— `claim_device_and_make_task`：发 `claim` RPC 拿 token，空则抛错；给两个 RPC 客户端都 `set_token`；返回声明循环任务。

[`mpmd_mboard_impl.cpp:362-405`](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/mpmd/mpmd_mboard_impl.cpp#L362-L405) —— `claim()`（实际发 `reclaim`）与 `make_claim_loop_task`：每 1000 ms 续声明一次，成功则顺带 `dump_logs()` 把设备端日志拉回主机。

**析构 = 解声明**：

[`mpmd_mboard_impl.cpp:318-326`](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/mpmd/mpmd_mboard_impl.cpp#L318-L326) —— 析构时先停声明任务、拉日志，再发 `unclaim` RPC 释放设备，让别人能声明它。

**初始化与 compat 校验**：

[`mpmd_impl.cpp:250-261`](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/mpmd/mpmd_impl.cpp#L250-L261) —— `setup_mb`：先 RPC `get_mpm_compat_num` 校验兼容号，再 `mb->init()`，最后 `register_mb_controller`（把主板控制器登记进 `rfnoc_device`，呼应 u3-l4）。

[`mpmd_impl.cpp:83-131`](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/mpmd/mpmd_impl.cpp#L83-L131) —— `assert_compat_number_throw`：主版本不等→抛错；次版本设备低于主机→抛错；设备高于主机→仅告警（向前兼容）。

[`mpmd_impl.cpp:31-33`](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/mpmd/mpmd_impl.cpp#L31-L33) —— `MPM_COMPAT_NUM = {6, 1}`，当前主机期望的 MPM 兼容号。

**`init()` 的特殊情况：MPM 自重启**：

[`mpmd_mboard_impl.cpp:331-349`](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/mpmd/mpmd_mboard_impl.cpp#L331-L349) —— `init()` 先 `init_device`（RPC `get_init_status` + `init`）；若设备报告需要 `mpm_reboot`，则 `allow_claim_failure(true)` 容忍续声明失败、发 `reset_timer_and_mgr` 让 MPM 重启、重建声明循环、重新 init。这是「更新 FPGA 镜像后让 MPM 重启」的代码路径。

**RPC 客户端的 token 机制**（理解 `*_with_token` 系列）：

[`rpc.hpp:226-272`](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/include/uhdlib/utils/rpc.hpp#L226-L272) —— `request_with_token`/`notify_with_token` 把保存的 `_token` 作为第一个参数自动带上；`set_token` 设置令牌。绝大多数配置类 RPC 都要求带 token（设备端据此判定调用方已声明设备），而 `get_device_info`、`get_mpm_compat_num` 这类只读查询则不需要 token。

#### 4.3.4 代码实践

**实践目标**：跟踪一次 `device::make` 在主机与 MPM 之间的完整 RPC 对话，理解声明循环。

**操作步骤**：

1. 假设你已构建好 UHD 并有一台 MPM 设备（如 X4xx）。开启主机端详细日志：
   ```bash
   export UHD_LOG_LEVEL=trace
   export UHD_LOG_FILTER="MPMD,RPC"
   ```
2. 运行 `uhd_usrp_probe --args "type=x4xx"`（若无硬件，跳到步骤 3 做源码阅读）。
3. 对照源码，把日志里出现的 RPC 调用逐个对应到代码行：
   - `claim` → [`mpmd_mboard_impl.cpp:409`](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/mpmd/mpmd_mboard_impl.cpp#L409)
   - `get_device_info` → [`mpmd_mboard_impl.cpp:290`](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/mpmd/mpmd_mboard_impl.cpp#L290)
   - `get_dboard_info` → [`mpmd_mboard_impl.cpp:296`](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/mpmd/mpmd_mboard_impl.cpp#L296)
   - `get_mpm_compat_num` → [`mpmd_impl.cpp:254`](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/mpmd/mpmd_impl.cpp#L254)
   - `init` → [`mpmd_mboard_impl.cpp:84`](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/mpmd/mpmd_mboard_impl.cpp#L84)

**需要观察的现象**：构造完成后，日志里会**持续**每隔约 1 秒出现 `reclaim` 相关记录——这就是声明循环在续约。

**预期结果**：你能把日志里的每一条 RPC 调用精确定位到源码行，并解释为什么 `claim` 之后所有配置调用都自动带上了 token。

> 若无硬件，本实践退化为「源码阅读型实践」：按上面行号顺序，在源码里串出 claim→get_device_info→get_dboard_info→get_mpm_compat_num→init 这条调用链即可。运行结果待本地结合真机验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `mpmd_mboard_impl` 要建**两个** RPC 客户端（`rpc` 和 `_claim_rpc`），而不是共用一个？

**答案**：两者超时不同、用途不同。`_claim_rpc` 专用于声明循环，超时设为 `MPMD_CLAIMER_RPC_TIMEOUT`（10 s），且声明循环在后台任务里独立运行；`rpc` 用于常规配置调用，超时 2 s。分开避免声明循环的长超时影响常规调用，也避免两者互相阻塞（`rpc_client` 内部虽有锁，但语义上分离更清晰、更易维护）。

**练习 2**：`setup_mb` 里注释说 compat 校验「effectively disabled for `skip_init=1`」。如果你用 `skip_init` 跳过初始化，会有什么风险？

**答案**：`skip_init` 跳过整个 `setup_mb`，包括 compat 校验和 `mb->init()`。这意味着即便设备端 MPM 版本与主机不兼容，也不会报错；设备也未真正初始化。这仅供特殊场景（如镜像加载工具 `mpmd_image_loader` 只想声明设备、刷固件而不配置射频）使用。普通应用用它会导致 `mb_iface`/`mb_ctrl` 为空指针（构造函数注释明确警告会空指针解引用）。

**练习 3**：声明循环每秒 `reclaim` 一次。如果某次 `reclaim` 失败但 `allow_claim_failure` 为 true，会发生什么？这个机制为谁服务？

**答案**：`claim()` 捕获异常后，若 `_allow_claim_failure_latch` 为 true，只记 DEBUG 日志并返回 true（循环不退出）；否则记 WARNING 并返回 latch 值。这个机制为「会让 RPC 服务短暂中断」的操作服务——典型就是 `init()` 里更新 FPGA 触发 MPM 重启（[`mpmd_mboard_impl.cpp:337-345`](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/mpmd/mpmd_mboard_impl.cpp#L337-L345)）。重启期间续声明必然失败，但这是「预期的」，不该让声明循环崩溃。

---

### 4.4 mpmd_link_if_mgr：链路管理与属性树构建

#### 4.4.1 概念说明

声明并初始化设备后，还缺最后一块拼图：**建立 CHDR 数据通路**（样本怎么搬），以及**把设备能力挂到属性树**（用户怎么配）。

`mpmd_link_if_mgr` 是一个 **工厂**：它接收抽象的「链路类型」（如 `"udp"`），产出一个具体的 **链路控制器**（`mpmd_link_if_ctrl_udp`）。注释直言——「As of UHD 4.0, there is only one underlying transport medium (UDP)」，之所以保留这个工厂抽象，纯粹是为了「连续性与面向未来」。这正是 [u4-l2](u4-l2-transport-layer.md) 提到的 link_if 抽象在 MPMD 侧的落地。

链路控制器的真正使用者在 `mpmd_mb_iface`：它通过 RPC 问设备「你支持哪些 CHDR 链路类型？每个类型有哪些可用地址？」，再驱动 `link_if_mgr` 建链，最后把建好的 send/recv 链路交给 RFNoC 的 I/O 服务去收发样本。

属性树构建则把「RPC 调用」翻译成「属性树节点」。回顾 [u2-l4](u2-l4-property-tree.md)：属性树叶子有 desired/coerced 双值，以及 coercer/subscriber/publisher 三类回调。MPMD 的做法是——把每次 `set`（coercer/subscriber）映射成一次 RPC `notify`，把每次 `get`（publisher）映射成一次 RPC `request`。于是用户对属性树的读写，最终都变成了对设备端 MPM 的远程调用。

#### 4.4.2 核心流程

**建链流程**（`mpmd_mb_iface::init` 精简）：

```
init():
    clock_ifaces = rpc->request_with_token("get_clocks")          # 拉时钟列表
    for 每个时钟: 建 clock_iface，登记进 _clock_ifaces
    chdr_link_types = rpc->request_with_token("get_chdr_link_types")  # 设备支持的链路类型
    for 每个 type:
        xport_info = rpc->request_with_token("get_chdr_link_options", type)  # 该类型的可用地址
        _link_if_mgr->connect(type, xport_info, chdr_w)          # 尝试建链
    if _link_if_mgr->get_num_links() == 0:
        throw "No CHDR connection available!"                    # 一个链都没建成就报错
    for 每条链: 分配一个本地 device_id，登记进 _local_device_id_map
```

注意 `connect` 的协商本质：主机用户可能指定了 `addr=192.168.10.2`，而设备 MPM 报上来「我有 192.168.10.2 和 192.168.20.2 两个口」。`link_if_mgr` 负责把这两边对上号，决定实际用哪个地址建 UDP 链路。如果用户同时给了 `addr` 和 `second_addr`，就能建两条链（`get_num_links()==2`）。

**属性树构建**（`init_property_tree` 精简）：

```
init_property_tree(tree, mb_path, mb):
    # 设备信息（一次性 set）
    tree[mb_path/name]      = device_info["name"]
    tree[mb_path/serial]    = device_info["serial"]
    tree[mb_path/mpm_version] ... 等
    # 时钟/时间源（set → RPC notify；get → RPC request，即 publisher）
    tree[mb_path/clock_source/value]
        .add_coerced_subscriber([](src){ rpc->notify_with_token("set_clock_source", src) })
        .set_publisher([](){ return rpc->request_with_token("get_clock_source") })
    # 传感器（只读，publisher 实时查 MPM）
    for sensor in rpc->request_with_token("get_mb_sensors"):
        tree[mb_path/sensors/sensor].set_publisher(查 MPM)
    # EEPROM、可更新组件（FPGA 镜像等）
    ...
```

这里能看到 [u2-l4](u2-l4-property-tree.md) 的 publisher 机制最典型的用法：传感器节点不存值，每次 `get` 都实时 RPC 查询设备端，保证读到的永远是最新硬件状态。

#### 4.4.3 源码精读

**链路工厂与唯一的 UDP 实现**：

[`mpmd_link_if_mgr.cpp:91-101`](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/mpmd/mpmd_link_if_mgr.cpp#L91-L101) —— `make_link_if_ctrl`：当前硬编码只支持 `"udp"`，其它类型记告警并返回 nullptr。这就是「UHD 4.0 起只有 UDP 一种介质」的代码体现：

```cpp
mpmd_link_if_ctrl_base::uptr make_link_if_ctrl(...)
{
    if (link_type == "udp") {
        return std::make_unique<mpmd_link_if_ctrl_udp>(_mb_args, xport_info, chdr_w);
    }
    UHD_LOG_WARNING("MPMD", "Cannot instantiate transport medium " << link_type);
    return nullptr;
}
```

[`mpmd_link_if_mgr.cpp:36-58`](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/mpmd/mpmd_link_if_mgr.cpp#L36-L58) —— `connect`：建控制器、检查链路数、把每条链登记进 `_link_link_if_ctrl_map`（链索引 → (控制器索引, 链内索引) 映射）。

[`mpmd_link_if_mgr.hpp:47-56`](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/mpmd/mpmd_link_if_mgr.hpp#L47-L56) —— 类注释明确：传输管理器是「建物理连接的工厂」，实现与介质绑定（UDP 则建 socket）。

**主板接口建链（RPC 驱动 link_if_mgr）**：

[`mpmd_mb_iface.cpp:53-92`](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/mpmd/mpmd_mb_iface.cpp#L53-L92) —— `mpmd_mb_iface::init`：RPC 拉 `get_clocks` 建时钟接口，RPC 拉 `get_chdr_link_types` + `get_chdr_link_options` 驱动 `_link_if_mgr->connect`，链数为 0 则抛错。

[`mpmd_mb_iface.cpp:84-91`](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/mpmd/mpmd_mb_iface.cpp#L84-L91) —— 「No CHDR connection available」报错点，以及为每条链分配本地 device_id。

[`mpmd_mb_iface.cpp:35-48`](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/mpmd/mpmd_mb_iface.cpp#L35-L48) —— 构造函数：分配远端 device_id 并 RPC `set_device_id` 告知设备；同时拉 compat 号判断设备是否支持「远端传输」能力（`REMOTE_XPORT_CAP_MIN{4,3}`）。

**收发数据通路的真正建立**：

[`mpmd_mb_iface.cpp:193-340`](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/mpmd/mpmd_mb_iface.cpp#L193-L340) —— `make_ctrl_transport` / `make_rx_data_transport`：当 RFNoC 流图需要控制通路或收发数据通路时，通过 `_link_if_mgr->get_link(...)` 取出 send/recv 链路，配置流控参数，交给 `chdr_ctrl_xport`/`chdr_rx_data_xport`。这是 link_if_mgr 与 [u4-l2](u4-l2-transport-layer.md) 传输层、[u4-l3](u4-l3-vrt-packets.md) VRT 包协议的衔接点。

**属性树构建（RPC 挂回调）**：

[`mpmd_prop_tree.cpp:100-132`](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/mpmd/mpmd_prop_tree.cpp#L100-L132) —— 设备信息节点（一次性写入 name/serial/connection/mpm_version/fpga_version 等）。

[`mpmd_prop_tree.cpp:135-160`](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/mpmd/mpmd_prop_tree.cpp#L135-L160) —— 时钟/时间源节点。这是「属性树 ↔ RPC」映射的范本：`set` 触发 `set_clock_source` RPC 通知，`get`（publisher）触发 `get_clock_source` RPC 查询：

```cpp
tree->create<std::string>(mb_path / "clock_source/value")
    .add_coerced_subscriber([mb](const std::string& clock_source) {
        mb->rpc->notify_with_token(MPMD_DEFAULT_INIT_TIMEOUT, "set_clock_source", clock_source);
    })
    .set_publisher([mb]() {
        return mb->rpc->request_with_token<std::string>("get_clock_source");
    });
```

[`mpmd_prop_tree.cpp:163-179`](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/mpmd/mpmd_prop_tree.cpp#L163-L179) —— 传感器节点：RPC 拉 `get_mb_sensors` 得到传感器名列表，为每个建一个 publisher 节点（实时 RPC `get_mb_sensor` 查值），并挂 coercer 拒绝写入（只读）。

[`mpmd_prop_tree.cpp:182-225`](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/mpmd/mpmd_prop_tree.cpp#L182-L225) —— EEPROM 与可更新组件（FPGA 镜像等）：`set` 触发 `update_component` RPC（刷镜像），其间会 `allow_claim_failure(true)` 容忍 MPM 重启导致的续声明失败——这与 4.3 节的声明失败容忍机制呼应。

#### 4.4.4 代码实践

**实践目标**：理解「属性树的每次读写都对应一次 RPC」，并通过属性树间接观察 RPC 流量。

**操作步骤**：

1. 阅读 [`mpmd_prop_tree.cpp:135-160`](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/mpmd/mpmd_prop_tree.cpp#L135-L160)，确认 `clock_source/value` 节点的 subscriber 调 `set_clock_source`、publisher 调 `get_clock_source`。
2. 若有硬件，写一段最小 C++ 程序（基于 [u2-l3](u2-l3-multi-usrp-api.md) 的 `multi_usrp`），打开设备后循环执行 `usrp->set_clock_source("internal")` 与 `usrp->get_clock_source()` 各 10 次，开启 `UHD_LOG_LEVEL=debug` 观察日志里 `set_clock_source` / `get_clock_source` 的 RPC 调用次数。
3. 改成读一个传感器（如 `get_mboard_sensor("temp")`），观察每次读取是否都触发一次 `get_mb_sensor` RPC（验证 publisher 是「实时查询」而非缓存）。

**需要观察的现象**：每次 `set`/`get` 几乎都对应一条 RPC 日志；传感器每次读取都触发 RPC（无缓存）。

**预期结果**：得出结论——MPMD 设备的属性树是「RPC 调用的薄封装」，属性树本身不缓存硬件状态（信息节点除外），读写实时穿透到设备端。

> 若无硬件，本实践退化为「源码阅读型实践」：在 `mpmd_prop_tree.cpp` 中统计有多少个节点用了 `set_publisher`（实时查询型）、多少个用 `set`（一次性写入型），分类记录。运行结果待本地结合真机验证。

#### 4.4.5 小练习与答案

**练习 1**：`mpmd_link_if_mgr` 当前只支持 UDP，为什么还要保留这层工厂抽象？

**答案**：注释说明是为「连续性与面向未来」（continuity and future-proofing）。历史上 MPMD 曾支持多种介质，未来也可能再增加（如 PCIe/NIRIO 直连）。保留工厂抽象使得新增介质时只需在 `make_link_if_ctrl` 里加一个 `if` 分支并实现一个新的 `mpmd_link_if_ctrl_*`，上层 `mpmd_mb_iface` 无需改动。这是典型的开闭原则应用。

**练习 2**：传感器节点为什么用 `set_publisher` 而不是构造时 `set` 一次缓存起来？

**答案**：因为传感器值（如温度、锁相状态）会随时间变化。回顾 [u2-l4](u2-l4-property-tree.md)：publisher 每次 `get` 都重新求值。MPMD 把 publisher 绑成 `get_mb_sensor` RPC，保证每次读传感器都拿到设备端最新值。若缓存，就会读到过时数据，对「`ref_locked` 是否锁定」这类关键判据是不可接受的。

**练习 3**：`mpmd_mb_iface::init` 里，如果设备报告支持 `["udp"]` 但用户没给任何地址，会发生什么？

**答案**：`get_chdr_link_options("udp")` 会返回设备端可用的 CHDR 地址列表，`link_if_mgr->connect` 会尝试用这些地址建链（详见 `mpmd_link_if_ctrl_udp` 的协商逻辑）。只要设备端有可用地址、网络可达，就能建链成功。若协商后一条链都没建成，[`mpmd_mb_iface.cpp:84-87`](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/mpmd/mpmd_mb_iface.cpp#L84-L87) 抛出 `"No CHDR connection available!"`。

---

## 5. 综合实践

**任务**：绘制一张完整的「MPMD 设备从 `find` 到 `make` 全链路图」，标注与 MPM 服务通信的每一个关键环节，并解释每个环节用的是哪条通信通道、对应哪个源码函数。

**要求**：

1. **发现阶段**（`mpmd_find`）：
   - 画出广播 `MPM-DISC`（UDP 49600）→ 收 `USRP-MPM` 应答 → 解析成 `device_addr_t`。
   - 画出可达性校验：`MPM-ECHO ping`（UDP）→ `get_device_info`（RPC，验序列号）。
   - 标注场景 1（给地址，不校验）与场景 2（广播，校验）的分叉点。

2. **制造阶段**（`mpmd_impl` 构造，四阶段）：
   - 阶段 1 声明：`claim`（RPC）→ token → 启动声明循环（`reclaim` 每 1 s）。
   - 阶段 2 初始化：`get_mpm_compat_num`（RPC，校验 {6,1}）→ `init`（RPC）→ `get_device_info`/`get_dboard_info`（RPC）→ `set_device_id`（RPC）。
   - 阶段 2.5 建链：`get_clocks`（RPC）→ `get_chdr_link_types`（RPC）→ `get_chdr_link_options`（RPC）→ `link_if_mgr->connect`（建 UDP 链路）。
   - 阶段 3 建属性树：把 `set_clock_source`/`get_clock_source`/`get_mb_sensors` 等挂成节点回调。
   - 阶段 4 同步时间（若带 `sync_time`）。

3. **析构阶段**：停声明循环 → `unclaim`（RPC）释放设备。

**交付物**：一张时序图（主机在左、MPM 在右、RPC 通道与发现通道用不同颜色区分），每个箭头标注：① 通信通道；② 对应的源码函数名与文件:行号；③ 是否需要 token。

**验收标准**：能指着图上任意一个箭头，回答「这一步在做什么、为什么需要它、跳过会怎样」。例如：指到 `claim` 能答出「拿 token 占有设备，防多主机冲突；跳过则后续 `*_with_token` 调用会被设备端拒绝」。

> 若无硬件，本实践全程基于源码阅读完成（「源码阅读型实践」）。所有 RPC 调用名均可在本讲引用的源码行号中找到原文。运行验证待本地结合真机。

## 6. 本讲小结

- **MPMD 是「所有跑 MPM 的现代设备」的统一驱动**：N3xx/E3xx/X4xx 共用一个 `mpmd_impl`，硬件差异由设备端 MPM 吸收；它通过 `UHD_STATIC_BLOCK` 以 `(mpmd_find, mpmd_make, USRP)` 自注册，继承 `rfnoc_device` 从而天然是 RFNoC 设备。
- **三条通信通道职责分明**：UDP 49600 发现通道（`MPM-DISC`/`MPM-ECHO`）、TCP 49601 RPC 控制通道、UDP CHDR 数据通道。
- **`mpmd_find` 分双场景**：给地址则单点发现不校验可达；不给地址则全网卡并发广播 + 并发可达性校验（先 UDP ping 探活，再 RPC 验序列号）。
- **`mpmd_impl` 构造是严格四阶段**：声明（claim 拿 token）→ 初始化（compat 校验 + init）→ 建属性树 → 同步时间；声明循环每秒 `reclaim` 续约，析构时 `unclaim` 释放。
- **`mpmd_link_if_mgr` 是链路工厂**：当前仅 UDP 一种介质，把抽象链路类型落地为 `mpmd_link_if_ctrl_udp`，由 `mpmd_mb_iface` 通过 RPC 拉取设备能力后驱动建链。
- **属性树是「RPC 的薄封装」**：`init_property_tree` 把每个 `set` 映射成 RPC notify、每个 `get`（publisher）映射成 RPC request，传感器等只读节点实时查询不缓存。

## 7. 下一步学习建议

- **横向对比老设备驱动**：阅读 `host/lib/usrp/x300_impl.cpp` 等老设备实现，对比「主机直接读写 FPGA 寄存器」与「MPMD 全程 RPC」的架构差异，体会 MPMD 抽象的价值。
- **深入设备端 MPM**：本讲只讲了主机侧。建议进入 [u5-l3 MPM 嵌入式外设管理](u5-l3-mpm-embedded.md)（待生成）阅读 `mpm/` 目录，看设备端 Python 进程如何实现 `claim`/`get_device_info`/`get_chdr_link_options` 等 RPC 接口，与主机侧一一对应。
- **打通传输层**：本讲的 `link_if_mgr` 产出的 send/recv 链路最终喂给 [u4-l2 传输层](u4-l2-transport-layer.md) 与 [u4-l3 VRT 包协议](u4-l3-vrt-packets.md)。建议回看这两讲，理解一条 CHDR 链路从「建 socket」到「搬 VRT 包」的完整链路。
- **动手验证**：在有 MPM 设备的环境里，用 `UHD_LOG_LEVEL=trace` 跑 `uhd_usrp_probe`，把日志里的 RPC 调用与本讲的源码行号一一对照，是巩固本讲最快的方式。
