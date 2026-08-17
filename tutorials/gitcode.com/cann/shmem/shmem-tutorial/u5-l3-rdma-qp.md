# RDMA 传输与 QP 管理

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 RDMA 传输层「V1 管理器（RdmaTransportManager）」与「V2 管理器（RdmaTransportManagerV2）」的分工与编译期分派条件。
2. 描述 QP（Queue Pair）管理器的三层抽象：基类 `DeviceQpManager` → `FixedRanksQpManager`（固定 rank）/ `DynamicRanksQpManager`（动态 rank），以及建链端口的自动分配规则。
3. 数出「N 个 rank 全互联、每对 peer 建 Q 条 QP」时连接与 QP 的总量，并推导 device 侧 `AiQpRMAQueueInfo` 的内存布局。
4. 讲解本轮（提交 8d1d777，Ascend950-RDMA 使能多 QP）新增的能力：`TransportOptions::rdmaQpConfig` 的传递链路、V2 管理器按 `qpNum` 为每个 peer 创建多条 QP、以及 `CheckQpNumConsistency` 对「各 PE QP 数不一致」的失败行为。

本讲承接 u5-l1 的结论：传输层由建堆阶段的内存实体拉起，`TransportManager` 用 12 个纯虚函数规定引擎义务，`TransportOptions` 是控制面配置进入传输层的唯一载体。

## 2. 前置知识

### 2.1 QP：RDMA 通信的基本单元

QP（Queue Pair，队列对）是一对队列——发送队列（SQ）与接收队列（RQ）——加上配套的完成队列（CQ）。一次 RDMA 写/读操作被软件组装成一个 WQE（Work Queue Element）挂到 SQ 上，网卡取走执行；执行结果生成 CQE 进 CQ。硬件通过「doorbell 寄存器」（代码里的 `dbAddr`/`dbMode`）被告知有新 WQE。SHMEM 的 kernel 直驱接口（u5-l6）就是直接写这些队列，所以 Host 侧必须把每个 QP 的队列地址、深度、head/tail 指针、doorbell 地址收集起来下发到 device 全局内存，这份元数据就是 `AiQpRMAQueueInfo`。

本讲涉及的队列类型都要求 RC（Reliable Connection，可靠连接）语义：一条 QP 绑定「本端↔某个对端」，按序可靠传输——这正是「按 peer 建 QP」的原因。

### 2.2 MR 与 lkey / rkey

MR（Memory Region）是注册给网卡的内存区域。注册后得到两个 key：`lkey`（本地访问 key，本端引擎发起操作时用）与 `rkey`（远程访问 key，对端访问这块内存时用）。Host 侧 `TransportManager::RegisterMemoryRegion` 做的就是把对称堆注册成 MR，最终这些 key 会进入 `AiQpRMAQueueInfo` 的 `mr[]` 数组。

### 2.3 两组底层驱动接口：HCCP RA 与 HCOMM

本讲源码会同时出现两族动态加载的 CANN 接口（经 u8-l4 的 under_api 适配层）：

- **HCCP RA**（`DlHccpApi::Ra*`）：socket 化的 RDMA 旧通路。V1 管理器用 `RaSocketInit/RaSocketListenStart/RaSocketBatchConnect/RaQpAiCreate` 等接口手工建 socket、建 QP。
- **HCOMM**（`DlHcommApi::Hcomm*`）：endpoint/channel 模型的新通路。V2 管理器只描述「我要连谁、几条 channel」，建链细节交给 HCOMM。

### 2.4 通信角色 hybm_role_type

[hybm_def.h:L63](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/mem/heap/hybm_def.h#L63) 定义三种角色：`HYBM_ROLE_PEER`（对等成员，标准 SHMEM 场景）、`HYBM_ROLE_SENDER`（发送方）、`HYBM_ROLE_RECEIVER`（接收方）。角色决定了 V1 管理器选哪种 QP 管理器（见 4.3）。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/host/transport/device_rdma/device_rdma_transport_manager.cpp](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/device_rdma/device_rdma_transport_manager.cpp) | V1 RDMA 管理器：HCCP RA 通路、RA 资源引用计数、委托 QP 管理器建链 |
| [src/host/transport/device_rdma/device_qp_manager.cpp](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/device_rdma/device_qp_manager.cpp) / [.h](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/device_rdma/device_qp_manager.h) | QP 管理器抽象基类：定义建链插件接口与 server socket 端口自动分配 |
| [src/host/transport/device_rdma/fixed_ranks_qp_manager.cpp](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/device_rdma/fixed_ranks_qp_manager.cpp) / [.h](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/device_rdma/fixed_ranks_qp_manager.h) | 固定 rank QP 管理器：全互联、一次建齐、rank 集合不可变 |
| [src/host/transport/device_rdma/dynamic_ranks_qp_manager.cpp](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/device_rdma/dynamic_ranks_qp_manager.cpp) / [.h](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/device_rdma/dynamic_ranks_qp_manager.h) | 动态 rank QP 管理器：后台线程任务驱动、支持运行中增量加 rank / 加 MR |
| [src/host/transport/device_rdma/device_rdma_transport_manager_v2.cpp](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/device_rdma/device_rdma_transport_manager_v2.cpp) / [.h](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/device_rdma/device_rdma_transport_manager_v2.h) | V2 RDMA 管理器：HCOMM 通路，本轮新增按 qpNum 建 多 QP 与一致性校验 |
| [src/host/transport/transport_def.h](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/transport_def.h) | `TransportOptions`，本轮新增 `rdmaQpConfig` 字段 |
| [src/host/transport/transport_manager.cpp](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/transport_manager.cpp) | 传输器工厂：`TT_HCCP` 的 V1/V2 编译期分派 |
| [src/host/transport/device_rdma/device_rdma_common.h](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/device_rdma/device_rdma_common.h) | `ConnectRankInfo`、`AiQpInfoToString` 等公共结构 |

## 4. 核心概念与源码讲解

### 4.1 RDMA 传输管理器骨架：V1 与 V2 的分派

#### 4.1.1 概念说明

SHMEM 的 RDMA 传输层有两个并存的实现，它们都继承自 u5-l1 讲过的 `TransportManager` 抽象基类：

- **V1（`RdmaTransportManager`）**：面向非 950 平台（A2/A3 等）。自己管理 HCCP RA 资源的生命周期，把「建 QP」整体委托给 QP 管理器（4.3）。
- **V2（`RdmaTransportManagerV2`）**：面向 Ascend950。改用 HCOMM endpoint/channel 模型，建链大批量、接口更省心，并且是本轮多 QP 能力的落点。

选谁不是运行时决定的，而是**编译期**由 `ACLSHMEM_RDMA_V2_SUPPORT` 宏决定——只有「开启 RDMA 支持 且 SOC 是 Ascend950」时才定义该宏。

#### 4.1.2 核心流程

无论 V1/V2，RDMA 管理器都按 u5-l1 的模板方法节奏走：

```
OpenDevice(options)          ← 解析设备号、记录 rankId/rankCount/qpNum、准备引擎资源
RegisterMemoryRegion(mr)     ← 把对称堆注册为 MR，得到 lkey/rkey
ConnectWithOptions(options)  ← 首次调用: Prepare(解析各 rank 的 nic) + Connect(建链)
GetDeviceInfo()              ← 把 GetQpInfo() 的地址塞进 TransportDeviceInfo.rdmaInfoAddress
CloseDevice()                ← 拆链、反注册 MR
```

#### 4.1.3 源码精读

**工厂分派**：[src/host/transport/transport_manager.cpp:L29-L35](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/transport_manager.cpp#L29-L35) 中 `TT_HCCP`（即 RoCE 引擎）按宏选择实现：

```cpp
case TT_HCCP:
#if defined(ACLSHMEM_RDMA_V2_SUPPORT)
    return std::make_shared<device::RdmaTransportManagerV2>();
#else
    return std::make_shared<device::RdmaTransportManager>();
#endif
```

宏的来源在 [CMakeLists.txt:L266-L268](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/CMakeLists.txt#L266-L268)：`ACLSHMEM_RDMA_SUPPORT` 且 `SOC_TYPE=Ascend950` 才定义。同文件 [CMakeLists.txt:L214-L229](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/CMakeLists.txt#L214-L229) 还要求 950 必须显式选 RDMA 后端（`XSCALE` 或 `HNS_1825`），分别定义 `ACLSHMEMI_RDMA_K_BACKEND_XSCALE` / `..._HNS_1825`——这个宏在 4.4 会再次出现（多 QP 仅 XSCALE 支持）。

**V1 OpenDevice 的设备号三级换算**：[device_rdma_transport_manager.cpp:L53-L102](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/device_rdma/device_rdma_transport_manager.cpp#L53-L102) 把 `userId`（用户设备号）→ `logicId`（逻辑设备号）→ `phyId`（全局物理设备号）逐级换算，HCCP/topo 使用全局 phyId。随后按角色创建 QP 管理器：

```cpp
if (role_ == hybm_role_type::HYBM_ROLE_PEER) {
    qpManager_ = std::make_shared<FixedRanksQpManager>(userId, phyId_, rankId_, rankCount_, deviceAddr);
} else {
    qpManager_ = std::make_shared<DynamicRanksQpManager>(
        userId, phyId_, rankId_, rankCount_, deviceAddr, role_ == hybm_role_type::HYBM_ROLE_RECEIVER);
}
```

（[device_rdma_transport_manager.cpp:L106-L111](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/device_rdma/device_rdma_transport_manager.cpp#L106-L111)）

**V1 对 QP 管理器的三段委托**：`Prepare` 解析每个 rank 的 nic 字符串构造 `ConnectRankInfo`，先 `SetRemoteRankInfo` 再 `Startup`（[L210-L244](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/device_rdma/device_rdma_transport_manager.cpp#L210-L244)）；`Connect` 退化为 `WaitForConnected`（join 后台建链线程，[L246-L279](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/device_rdma/device_rdma_transport_manager.cpp#L246-L279)）；`GetQpInfo` 直接取 QP 管理器的 `GetQpInfoAddress()`（[L313-L320](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/device_rdma/device_rdma_transport_manager.cpp#L313-L320)）。

**RA 资源引用计数**：V1 用进程级静态表 `raInstances_` 管理 HCCP RA 资源（[L365-L381](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/device_rdma/device_rdma_transport_manager.cpp#L365-L381)）。同一 phyId 的多个传输器共享一份 RA，`RaInit` 计数加一（[L402-L433](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/device_rdma/device_rdma_transport_manager.cpp#L402-L433)），析构时计数减到零才真正 `RaDeinit`。注意 [L421-L426](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/device_rdma/device_rdma_transport_manager.cpp#L421-L426) 的容错：`RaInit` 失败时假设 HCCL 已初始化过 RA，等 3 秒后照样置为「已引用但不自持」——避免与同进程的 HCCL 重复初始化冲突。

**QP 信息如何被消费**：基类方法 [transport_manager.cpp:L76-L81](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/transport_manager.cpp#L76-L81) 把 `GetQpInfo()` 返回的 device 地址填进 `TransportDeviceInfo.rdmaInfoAddress`，由内存实体写入 device 元数据区，kernel 侧引擎直驱接口（u5-l6）据此找到每个 QP 的队列与 doorbell。

#### 4.1.4 代码实践

1. **实践目标**：确认本机编译产物走 V1 还是 V2，并观察 `TransportOptions` 的完整打印。
2. **操作步骤**：
   - 查看 [CMakeLists.txt:L266-L268](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/CMakeLists.txt#L266-L268)，对照自己编译时传入的 `SOC_TYPE` 与 `-enable_rdma`、`-rdma_backend` 参数判断宏是否定义；
   - 按 docs/debug 的日志开关把日志等级调到 DEBUG，运行任一 RDMA 示例（如 `examples/rdma_demo`），在输出中找 `begin to open device with TransportOptions(...)` 一行。
3. **需要观察的现象**：`TransportOptions` 的序列化输出中应出现 `rdmaQpNum=` 与 `udmaQpNum=` 两个字段（来自 [transport_def.h:L62-L69](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/transport_def.h#L62-L69)），默认都是 1。
4. **预期结果**：950 平台走 `RdmaTransportManagerV2`、日志出现 `rank[x] begin to open device with ...`；非 950 平台走 V1。
5. 本实践需要 NPU 环境与已编译的 RDMA 版本，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 V1/V2 的选择放在编译期而不是运行时？
**答案**：两者依赖的底层接口族不同（HCCP RA vs HCOMM），HCOMM 相关符号仅在新版 CANN/950 平台存在；用宏分派可以让非 950 平台的二进制完全不引用 HCOMM 符号，避免加载失败，也与 u1-l2 讲过的「引擎可用性在编译期由 CANN 版本与芯片型号锁定」一致。

**练习 2**：V1 的 `Connect()` 里 `AsyncConnect()` 是空实现，为什么接口还保留？
**答案**：`AsyncConnect`/`WaitForConnected` 是基类模板方法 `ConnectWithOptions`（u5-l1）规定的两个阶段。V1 的建链已经在 `Prepare→Startup` 里由 QP 管理器的后台线程异步发起，`Connect` 只需 `WaitForConnected` join 线程，因此 `AsyncConnect` 留空但阶段语义不变。

### 4.2 QP 管理器抽象与建链端口自动分配

#### 4.2.1 概念说明

`DeviceQpManager` 是「建 QP」这件事的抽象基类：它知道本端设备号、rank、总 rank 数、本端网卡地址（`mf_sockaddr`），并用 4 个纯虚函数规定子类义务——`SetRemoteRankInfo`（喂入全组 rank 信息）、`SetLocalMemories`（喂入本地 MR 表）、`Startup`（建链）、`Shutdown`。两个「高级」虚函数给出默认实现：`WaitingConnectionReady` 直接返回成功、`GetQpInfoAddress` 返回空——子类按需覆盖。基类自己负责一件具体的事：**创建 server socket 并自动分配监听端口**。

#### 4.2.2 核心流程

```
CreateLocalSocket()        RaSocketInit(离线网络模式, 本端 IP) → socketHandle
CreateServerSocket()
    ├─ 从 deviceAddress_ 里携带的初始端口开始
    ├─ 循环: RaSocketListenStart(port) 成功 → 回写实际端口, 跳出
    │        失败 → port+1 重试, 直到 65535
    └─ 全部失败 → 销毁 socket, 返回失败
DestroyServerSocket()      ListenStop + RaSocketDeinit
```

端口自动分配的意义：多个 PE 可能共享一台机器同一张网卡，初始端口被占用时自动 +1 递增寻找空闲端口，成功后把**实际**端口写回 `deviceAddress_`，随后该地址经控制面交换给其他 PE，对端才知道连哪个端口。

#### 4.2.3 源码精读

抽象接口定义在 [device_qp_manager.h:L25-L37](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/device_rdma/device_qp_manager.h#L25-L37)：

```cpp
class DeviceQpManager {
public:
    ...
    virtual int SetRemoteRankInfo(const std::unordered_map<uint32_t, ConnectRankInfo> &ranks) noexcept = 0;
    virtual int SetLocalMemories(const MemoryRegionMap &mrs) noexcept = 0;
    virtual int Startup(void *rdma) noexcept = 0;
    virtual void Shutdown() noexcept = 0;
    virtual int WaitingConnectionReady() noexcept;        // 默认: 直接成功
    virtual const void *GetQpInfoAddress() const noexcept; // 默认: 返回 nullptr
    virtual void *GetQpHandleWithRankId(uint32_t rankId) const noexcept = 0;
```

`CreateLocalSocket` 用 `NETWORK_OFFLINE` 模式初始化 socket 并按 IPv4/IPv6 填本端 IP（[device_qp_manager.cpp:L37-L55](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/device_rdma/device_qp_manager.cpp#L37-L55)）。

端口递增循环在 [device_qp_manager.cpp:L73-L90](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/device_rdma/device_qp_manager.cpp#L73-L90)：

```cpp
bool successListen = false;
uint16_t maxPort = std::numeric_limits<uint16_t>::max();
while (listenInfo.port <= maxPort) {
    auto ret = DlHccpApi::RaSocketListenStart(&listenInfo, 1);
    if (ret == 0) {
        ...  // 把 listenInfo.port 回写到 deviceAddress_（v4/v6 分支）
        successListen = true;
        break;
    }
    if (listenInfo.port == maxPort) break;
    listenInfo.port++;
}
```

成功后打印 `start to listen on port: N success.`（[L97-L99](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/device_rdma/device_qp_manager.cpp#L97-L99)）。

#### 4.2.4 代码实践

1. **实践目标**：理解「初始端口从哪来、实际端口到哪去」。
2. **操作步骤**：
   - 阅读 [device_qp_manager.cpp:L69-L72](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/device_rdma/device_qp_manager.cpp#L69-L72)，确认初始端口取自 `deviceAddress_` 携带的端口；
   - 回溯 `deviceAddress_` 的来源：V1 的 `OpenDevice` 里由 `InitializeDeviceAddress` 用 `ParseDeviceNic(options.nic, devicePort_)` 解析出的端口填充（[device_rdma_transport_manager.cpp:L38-L51](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/device_rdma/device_rdma_transport_manager.cpp#L38-L51)、[L86-L90](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/device_rdma/device_rdma_transport_manager.cpp#L86-L90)）；
   - 写 3~5 句话回答：同一台机器上第二个 PE 复用该基类时会发生什么？
3. **需要观察的现象 / 预期结果**：两个 PE 初始端口相同 → 第一个监听成功、第二个 `RaSocketListenStart` 失败后端口 +1，各自的实际端口不同，随后经 nic 字符串交换给对端。这是纯源码阅读实践，结论可从代码直接推出；真实运行行为**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：端口递增的上限是多少？到达上限后行为是什么？
**答案**：上限 65535（`uint16_t` 最大值）。到达上限仍未监听成功则跳出循环，`successListen` 保持 false，销毁 socket 并返回 `ACLSHMEM_DL_FUNC_FAILED`（[device_qp_manager.cpp:L86-L95](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/device_rdma/device_qp_manager.cpp#L86-L95)）。

**练习 2**：`GetQpHandleWithRankId` 是纯虚函数，但当前仓库 src 内除定义与声明外没有任何调用点。这说明什么？
**答案**：它是为「Host 侧直接持有 QP 句柄发起操作」预留的查询接口；现行数据面走 device 直驱（kernel 用 `AiQpRMAQueueInfo` 而非 Host 句柄），因此暂无调用方。读源码时应把「接口存在」与「接口被使用」区分开。

### 4.3 固定 rank 与动态 rank 两种 QP 管理器

#### 4.3.1 概念说明

两个子类对应两种通信形态：

| 维度 | FixedRanksQpManager | DynamicRanksQpManager |
|------|--------------------|-----------------------|
| 角色（构造时固定） | `HYBM_ROLE_PEER`，全组对等 | `HYBM_ROLE_RECEIVER`（server）或 `HYBM_ROLE_SENDER`（client），见 [dynamic_ranks_qp_manager.cpp:L22-L31](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/device_rdma/dynamic_ranks_qp_manager.cpp#L22-L31) |
| rank 集合 | 启动时一次性全量建链，之后**不可变**（`SetRemoteRankInfo` 在 `started_` 后直接报错，[fixed_ranks_qp_manager.cpp:L42-L51](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/device_rdma/fixed_ranks_qp_manager.cpp#L42-L51)） | 支持运行中**增量**加 rank：`SetRemoteRankInfo` 做差分生成任务（[dynamic_ranks_qp_manager.cpp:L42-L75](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/device_rdma/dynamic_ranks_qp_manager.cpp#L42-L75)） |
| MR 更新 | 启动后不支持 | 支持增量注册/更新（`updateMrTask`） |
| 执行模型 | 两条一次性线程（server/client 各一），join 即完成 | 常驻后台线程轮询六类任务 |
| 适用场景 | 标准 SHMEM：进程组固定、任意 PE 互访 | 动态扩缩容、收发角色不对等的场景 |

#### 4.3.2 核心流程

**FixedRanksQpManager 的建链时序**（以 rank i、全组 n 个 rank 为例）：

```
Startup(rdma)
 ├─ ReserveQpInfoSpace()          # device 侧 AiQpRMAQueueInfo 布局预留
 ├─ StartServerSide()             # 若 i == n-1（最大 rank）跳过
 │   ├─ CreateServerSocket()      # 基类: 端口自动分配
 │   ├─ GenerateWhiteList()       # 为所有 rank > i 的对端加白名单（小 id 当 server）
 │   └─ server 线程: WaitConnectionsReady → CreateQpWaitingReady(SERVER)
 ├─ StartClientSide()             # 若 i == 0 跳过
 │   ├─ 为所有 rank < i 创建本地 socket, RaSocketBatchConnect（分批, 每批 RA_MAX_BATCH_NUM）
 │   └─ client 线程: WaitConnectionsReady → CreateQpWaitingReady(CLIENT)
 └─ started_ = true

CreateQpWaitingReady(每条连接):
   CreateOneQp(RaQpAiCreate, RC, SEND_CQ_DEPTH=8192/RECV_CQ_DEPTH=128)
   → 逐个 MR RaMrReg → RaQpConnectAsync(socketFd)
   → 轮询 RaGetQpStatus 直到全部 status==1（1 分钟超时）
   → FillQpInfo: 把 sq/rq/scq/rcq/mr 组装进 AiQpRMAQueueInfo 拷入 device
```

**DynamicRanksQpManager 的任务驱动**：`Startup` 里 RECEIVER 把初始 rank 集合装进 `whitelistTask`、SENDER 装进 `clientConnectTask`，然后启动常驻线程 `BackgroundProcess`（[dynamic_ranks_qp_manager.cpp:L96-L147](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/device_rdma/dynamic_ranks_qp_manager.cpp#L96-L147)）；后台线程每轮依次处理白名单、客户端连接、连接状态查询、QP 连接、QP 状态查询、本地/远端 MR 更新六类任务，无事则条件变量休眠最多 1 分钟（[L167-L188](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/device_rdma/dynamic_ranks_qp_manager.cpp#L167-L188)）。任务结构（带 `Failed/Success` 重试记账）定义在 [dynamic_ranks_qp_def.h](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/device_rdma/dynamic_ranks_qp_def.h)。

#### 4.3.3 源码精读

**QP 创建参数**：[fixed_ranks_qp_manager.cpp:L562-L592](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/device_rdma/fixed_ranks_qp_manager.cpp#L562-L592) 的 `CreateOneQp` 对 AI Core QP 用 `IBV_QPT_RC` 类型、`max_send_wr=8192`/`max_recv_wr=128`，并设置 TC/SL 服务质量属性（可用环境变量 `HCCL_RDMA_TC`/`HCCL_RDMA_SL` 覆盖，[L33-L34](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/device_rdma/fixed_ranks_qp_manager.cpp#L33-L34)）。

**谁连谁**：白名单生成处的注释一锤定音——`small id as server, large id as client`（[fixed_ranks_qp_manager.cpp:L306-L311](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/device_rdma/fixed_ranks_qp_manager.cpp#L306-L311)）：rank i 为所有比它大的 rank 当 server，向所有比它小的 rank 当 client。这样每对 rank 恰好一条连接，无竞态。

**QP 状态机**：`RaGetQpStatus` 返回的状态注释在 [L546](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/device_rdma/fixed_ranks_qp_manager.cpp#L546)：`0-disconnect, 1-connected, 2-timeout, 3-connecting, 4-fd_close, 5-pause`——只有全部到达 1 才算就绪。

**device 侧 QP 信息布局（单 QP 版）**：[fixed_ranks_qp_manager.cpp:L594-L602](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/device_rdma/fixed_ranks_qp_manager.cpp#L594-L602) 把 `count` 写死为 1，五个数组按 rank 数排布：

```
AiQpRMAQueueInfo | sq[n] | rq[n] | scq[n] | rcq[n] | mr[n]
```

预留空间大小 `oneQpSize = 2*(sizeof(AiQpRMAWQ)+sizeof(AiQpRMACQ)) + sizeof(RdmaMemRegionInfo)`，总量乘 `rankCount_`（[L158-L175](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/device_rdma/fixed_ranks_qp_manager.cpp#L158-L175)）——记住这个式子，4.4 的多 QP 版只是把 `rankCount_` 换成 `rankCount_*qpNum`。

**动态管理器的差分**：[dynamic_ranks_qp_manager.cpp:L58-L74](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/device_rdma/dynamic_ranks_qp_manager.cpp#L58-L74) 每次调用 `SetRemoteRankInfo` 时先过滤掉与自身同角色的 rank（SENDER 不连 SENDER），再与上次快照做差分得到「新增 rank」和「需补 MR 的 rank」，据此生成后台任务——这是「动态」二字的全部含义。

#### 4.3.4 代码实践

1. **实践目标**：数清全互联的连接数与 QP 数。
2. **操作步骤**：设全组 n 个 rank（两节点各 8 PE 即 n=16），基于「小 id 当 server、大 id 当 client、每对 rank 一条 socket 连接、每侧各建 1 个 AI QP」的规则手工计算：
   - 连接对数：\[ \binom{n}{2} = \frac{n(n-1)}{2} = \frac{16\times15}{2} = 120 \]
   - AI QP 总数（两侧各一）：\( 2\times120 = 240 \)；每个 rank 恰好 15 个。
   - 验证方式：对 rank i，server 侧连接数 \( 15-i \)（白名单只加比 i 大的），client 侧连接数 \( i \)（只连比 i 小的），两者之和恒为 15。把 i=0..15 的分布画成表。
3. **需要观察的现象 / 预期结果**：rank 0 全是 server 连接、rank 15 全是 client 连接、中间 rank 混合。若真机运行 `examples/rdma_demo`（16 PE），日志中 `connect to (rank) ready` 的条数应符合上表（**待本地验证**）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 rank 0 不执行 `StartClientSide`、rank n-1 不执行 `StartServerSide`？
**答案**：rank 0 没有比它更小的 rank 可连（client 只连小 id），rank n-1 没有比它更大的 rank 需要服务（server 只服务大 id），两个分支各自短路返回成功（[fixed_ranks_qp_manager.cpp:L179-L182](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/device_rdma/fixed_ranks_qp_manager.cpp#L179-L182)、[L243-L247](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/device_rdma/fixed_ranks_qp_manager.cpp#L243-L247)）。

**练习 2**：`WaitConnectionsReady` 的超时是「固定 2 分钟」吗？
**答案**：不是。只要期间有连接进展（`progress` 为真）就把 2 分钟窗口重新起算（[fixed_ranks_qp_manager.cpp:L463-L466](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/device_rdma/fixed_ranks_qp_manager.cpp#L463-L466)），即「连续 2 分钟无进展」才报 `ACLSHMEM_TIMEOUT_ERROR`，避免大集群慢启动被误杀。

**练习 3**：如果把 `FillQpPreSettingCopyInfo` 里的 `copyInfo->count = 1` 改成 2 而不改动其他代码，会发生什么？
**答案**：device 侧消费方按 `rank * count + qpIdx` 索引队列数组（见 4.4 与 u5-l6），count=2 会越界读到 rq/scq 区域，产生错误队列描述——这正是「QP 数全组必须一致」的根因之一，也是本轮把多 QP 支持整体实现在 V2 而非魔改 V1 的原因。

### 4.4 V2 管理器：按 qpNum 建多条 QP 与一致性校验（本轮更新）

#### 4.4.1 概念说明

V2 管理器把建链交给 HCOMM：本端只创建一个 endpoint（`HcommEndpointCreate`），监听端口由 HCOMM 分配后回查（`HcommEndpointGetListenPort`）；每个「本 rank × 远端 rank × QP 序号」组合创建一条 channel，engine 标志固定为 `COMM_ENGINE_AIV`（AIV/AICore 直接驱动的通道）。

**为什么要多 QP**：一条 RC QP 上的 WQE 必须按序提交、按序完成，多核并发写同一 QP 会互相排队；为同一对 peer 开 Q 条 QP 后，kernel 侧可用 `qp_idx` 把流量散到多条队列并行下发，提升小消息场景的带宽利用率。提交 8d1d777 的测试记录显示：单向单网卡 1KB 消息下 1/2/4 QP 实测带宽约 8.95 / 16.55 / 55.95 Gb/s，4 QP 提升近 6 倍；大消息（≥64KB）下网卡趋于饱和、各 QP 数带宽接近。多 QP 的配置入口是 u2-l1/u2-l2 已讲的 `aclshmemx_set_qp_num`：取值 1~32（[shmem_host_def.h:L33-L34](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/include/host/shmem_host_def.h#L33-L34) 的 `ACLSHMEM_MAX_QP_NUM`），init 前设置、实例存活期冻结、最后一个实例 finalize 后复位（[shmem_init.cpp:L615-L637](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/shmem_init.cpp#L615-L637)、[L1039](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/shmem_init.cpp#L1039)、[L1154-L1156](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/shmem_init.cpp#L1154-L1156)）。

**传递链路**（承接 u5-l1 的结论，本轮扩展到 ROCE）：

```
aclshmemx_set_qp_num(ACLSHMEM_DATA_OP_ROCE, q)   [shmem_init.cpp:627-628]
  → g_rdma_qp_config.qpNum = q                    （进程级全局, 冻结于 init）
  → bind_aclshmem_entity(..., g_rdma_qp_config.qpNum)   （快照进 init backend 的 entity_member）
  → transport_options.rdmaQpConfig.qpNum = elem->rdma_qp_num  [shmem_init_backend.cpp:255-261]
  → RdmaTransportManagerV2::OpenDevice: qpNum_ = options.rdmaQpConfig.qpNum  [v2:268]
```

其中 `RdmaQpConfig` 是本轮新增的内嵌结构（[transport_def.h:L50-L60](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/transport_def.h#L50-L60)），与已有的 `udmaQpConfig` 同构。

#### 4.4.2 核心流程

V2 `Connect`（[device_rdma_transport_manager_v2.cpp:L543-L726](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/device_rdma/device_rdma_transport_manager_v2.cpp#L543-L726)）：

```
Connect()
 ├─ CheckQpNumConsistency()              # bootstrap allgather 全组核对 qpNum
 ├─ channelNum = (rankCount-1) * qpNum   # 溢出检查后确定 channel 总数
 ├─ RegisterAtomicMemory()               # 注册 AMO 专用内存 (4K 对齐)
 ├─ 组装 channelDescs[channelNum]:
 │     for remoteRank (跳过自己):
 │       for qpIdx in [0, qpNum):
 │         desc.role   = (rankId < remoteRank) ? SERVER : CLIENT   # 仍是小 id 当 server
 │         desc.port   = isServer ? 本端监听端口 : 对端端口
 │         desc.roceAttr.queueNum = 1     # 每 channel 恰 1 条队列——多 QP 靠多 channel 实现
 │         desc.channelName = "aclshmem-r{小id}-r{大id}-q{qpIdx}"
 ├─ HcommChannelCreate(endpoint, COMM_ENGINE_AIV, descs, channelNum) → channelPtrs
 ├─ 轮询 HcommChannelGetStatus 直到全部 READY（间隔/超时按 channel 数缩放, 单通道超时 1min）
 └─ FillRdmaInfo(): 从各 channel entity 提取 SQ/CQ/MR → 组装 AiQpRMAQueueInfo → 拷入 device
```

#### 4.4.3 源码精读

**OpenDevice 的三重校验**：[device_rdma_transport_manager_v2.cpp:L265-L278](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/device_rdma/device_rdma_transport_manager_v2.cpp#L265-L278)：

```cpp
qpNum_ = options.rdmaQpConfig.qpNum;
if (qpNum_ == 0 || qpNum_ > ACLSHMEM_MAX_QP_NUM) {          // ① 1~32
    return ACLSHMEM_INVALID_PARAM;
}
#if !defined(ACLSHMEMI_RDMA_K_BACKEND_XSCALE)
if (qpNum_ != 1U) {                                          // ② 非 XSCALE 后端只许 1
    return ACLSHMEM_NOT_SUPPORTED;
}
#endif
```

② 说明多 QP 目前仅 XSCALE 后端（云脉网卡）使能；HNS_1825 后端上 `qpNum>1` 会在打开设备阶段即返回 `ACLSHMEM_NOT_SUPPORTED`。随后的 `InitActualListenPort` 用 `HcommEndpointGetListenPort` 取 HCOMM 分配的实际端口（[L316-L337](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/device_rdma/device_rdma_transport_manager_v2.cpp#L316-L337)）——与 4.2 的「端口递增」不同，这里端口分配完全托管给 HCOMM。

**QP 数一致性校验**：[device_rdma_transport_manager_v2.cpp:L728-L753](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/device_rdma/device_rdma_transport_manager_v2.cpp#L728-L753)：

```cpp
if (rankCount_ <= 1) return ACLSHMEM_SUCCESS;          // 单 rank 无需校验
std::vector<uint32_t> allQpNums(rankCount_, 0);
auto ret = g_boot_handle.allgather(&qpNum_, allQpNums.data(), sizeof(qpNum_), &g_boot_handle);
...
for (uint32_t rank = 0; rank < rankCount_; ++rank) {
    if (allQpNums[rank] != qpNum_) {
        SHM_LOG_ERROR("... inconsistent rdma qp num: local=" << qpNum_
                      << ", rank[" << rank << "]=" << allQpNums[rank]);
        return ACLSHMEM_INVALID_PARAM;                  // 任一不一致 → Connect 失败
    }
}
```

它复用了 u2-l3 讲过的 bootstrap 控制面 `allgather` 原语做全组核对——这是「QP 数全组必须一致」的**强制执行点**：不一致时 `Connect` 返回 `ACLSHMEM_INVALID_PARAM`，初始化随之失败，错误日志会同时打印本地值与不一致 rank 的值，便于定位是哪个 PE 配错。

**channel 数量与命名**：`channelNum = (rankCount-1) * qpNum`（[L557-L564](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/device_rdma/device_rdma_transport_manager_v2.cpp#L557-L564)，含 uint32 溢出防护）。channel 名由 [L41-L46](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/device_rdma/device_rdma_transport_manager_v2.cpp#L41-L46) 的 `BuildStableChannelName` 生成：两端各自计算得到**相同**的名字（server/client 由 min/max 归一），保证一条逻辑连接在两侧可对账。`desc.roceAttr.queueNum = 1`（[L625](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/device_rdma/device_rdma_transport_manager_v2.cpp#L625)）——多 QP 不是「一条 channel 里塞多条队列」，而是「多建 channel」。

**device 侧多 QP 布局**：`FillQpPreSettingCopyInfo`（[L836-L844](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/device_rdma/device_rdma_transport_manager_v2.cpp#L836-L844)）把 V1 的 `count=1` 换成 `count=qpNum_`，每个数组的长度从 `rankCount` 变为 `rankCount*qpNum`：

```
AiQpRMAQueueInfo{count=q} | sq[n*q] | rq[n*q] | scq[n*q] | rcq[n*q] | mr[n]
```

队列条目索引为 `remoteRank * qpNum + qpIdx`（如 SQ 填充处 [L1035](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/device_rdma/device_rdma_transport_manager_v2.cpp#L1035)），而 Host 侧 channel 向量的索引是「排在 remoteRank 之前且非自身的 rank 数 × qpNum + qpIdx」（`remoteOrdinal`，[L964-L967](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/device_rdma/device_rdma_transport_manager_v2.cpp#L964-L967)）——两套索引并存是阅读该函数的关键。空间预留 `qpInfoSize_ = sizeof(AiQpRMAQueueInfo) + oneQpSize * rankCount * qpNum`（[L1094-L1104](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/device_rdma/device_rdma_transport_manager_v2.cpp#L1094-L1104)），最终 `FillRdmaInfo` 整体 `AclrtMemcpy` 到 device（[L865-L889](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/device_rdma/device_rdma_transport_manager_v2.cpp#L865-L889)）。

**AMO 内存按 QP 复制**：每个 rank 预留 `128 * 8B` 的 atomic 槽区，所有 QP 条目的 `atomicAddr` 指向同一 rank 槽区、共享同一个 `atomicLkey`（[L1064-L1073](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/device_rdma/device_rdma_transport_manager_v2.cpp#L1064-L1073)）——原子操作的目标内存与 QP 数无关，变的只是访问路径。

#### 4.4.4 代码实践

1. **实践目标**：对比单 QP 与多 QP 的建链数量、并实测带宽差异。
2. **操作步骤**：
   - **纸上推导（无环境也可做）**：填写下表（n=16 个 rank）：

     | qpNum=q | 每 rank channel 数 (n-1)*q | 全组 channel 数 2·C(n,2)·q | device 侧 sq 条目 n*q |
     |---------|---------------------------|------------------------------|----------------------|
     | 1 | 15 | 120 | 16 |
     | 4 | 60 | 480 | 64 |
     | 32 | 480 | 3840 | 512 |

   - **真机运行（需 Ascend950 + XSCALE 后端）**：进入 `examples/shmem_perftest/rdma_perftest/`，用 `bash run.sh -t put -e 10 -q 4` 与 `-q 1` 分别跑 1KB 消息的带宽，再跑 `-e 16`（64KB）对比。`-q/--qp-count` 取值 1~32（[run.sh:L262-L263](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/examples/shmem_perftest/rdma_perftest/run.sh#L262-L263) 有校验）；示例 Host 侧在 init 前调用 `aclshmemx_set_qp_num(ACLSHMEM_DATA_OP_ROCE, qp_num)`（[main.cpp:L170](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/examples/shmem_perftest/rdma_perftest/main.cpp#L170)），kernel 侧按 `op_idx % qp_num` 轮询分配 QP 并用 `aclshmemx_roce_qp_quiet` 逐 QP 等待完成（[rdma_perftest_kernel.cpp:L82-L84](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/examples/shmem_perftest/rdma_perftest/rdma_perftest_kernel.cpp#L82-L84)）。
3. **需要观察的现象**：初始化日志出现 `rdma channel to rank[x] qp=k role=..., name=aclshmem-r..-r..-q..`（[v2:L636-L639](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/device_rdma/device_rdma_transport_manager_v2.cpp#L636-L639)），`-q 4` 时每个对端 4 行。
4. **预期结果**：小消息（1KB 量级）多 QP 带宽显著高于单 QP（提交信息记录约 6 倍），大消息各 QP 数趋同。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：若 PE0 设 `qp_num=4`、PE1 设 `qp_num=1`，初始化会发生什么？
**答案**：两进程都会在 V2 `Connect` 进入 `CheckQpNumConsistency`：allgather 收集到 `[4,1]`，本地值与对方不等，双方各自打印 `inconsistent rdma qp num: local=..., rank[...]=...` 并返回 `ACLSHMEM_INVALID_PARAM`，`Connect` 失败、初始化中止。（另注意 `aclshmemx_set_qp_num` 本身不校验全组一致——一致性是在建链时强制执行的。）

**练习 2**：多 QP 为何不把 `desc.roceAttr.queueNum` 设成 q？
**答案**：HCOMM channel 与 QP 是一对一封装，`queueNum=1` 表示每条 channel 对应一条队列；「每 peer q 条 QP」通过创建 `(rankCount-1)*q` 条 channel 实现。这样 channel 名、状态轮询、错误隔离都按单 QP 粒度管理（一条 channel 失败只影响自身），也使 device 侧 `sq[remoteRank*q + qpIdx]` 的映射保持线性。

**练习 3**：`CheckQpNumConsistency` 为什么直接用 `g_boot_handle.allgather` 而不是重新设计一套交换？
**答案**：bootstrap 控制面（u2-l3）在 init 阶段已就绪，allgather 是现成的全组收集原语；QP 一致性检查只传 4 字节整数，复用控制面成本最低，也符合「控制面做元数据交换、数据面走网卡」的分层原则。

## 5. 综合实践

**任务：画出两节点 × 8 PE 的 RDMA 建链全景图，并解释三种 QP 管理形态的差异。**

1. **画图**（纸或绘图工具均可）：
   - 设全组 16 个 rank。横轴画 rank 0..15，按节点分两组（0-7 / 8-15）。
   - **V1 fixed 单 QP**：每对 rank 画一条连线（共 120 条），在线两端各标 1 个 AI QP；给 rank 3 标注「server 连接 12 条（rank 4-15）+ client 连接 3 条（rank 0-2）」。
   - **V2 多 QP（q=4）**：把每条连线加粗为 4 股（channel），全组 480 条 channel；每个 rank 旁标 `AiQpRMAQueueInfo: count=4, sq/rq/scq/rcq 各 16×4 项, mr 16 项`。
   - 在图上用箭头标出「小 id 当 server、大 id 当 client」的分界规则。
2. **说明 fixed 与 dynamic 的适用模式**（对照 4.3.1 的表格写 5~8 句）：fixed 用于 rank 集合固定的对等全互联（标准 SHMEM 训练/推理作业）；dynamic 用于收发角色固定且对端集合会增长的场景（如一台汇聚节点持续接收多台发送节点的数据），其增量建链由后台线程任务驱动。
3. **解释 CheckQpNumConsistency 行为**（对照 4.4.3 源码）：单 rank 直接通过；多 rank 用 bootstrap allgather 收集全组 qpNum，任一 rank 与本地不等 → 打印双方值并返回 `ACLSHMEM_INVALID_PARAM`，`Connect` 失败。把 4.4.5 练习 1 的「PE0=4、PE1=1」案例作为图注写进你的图。
4. **验证**（可选，需 Ascend950 RDMA 环境）：`cd examples/shmem_perftest/rdma_perftest && bash run.sh -t put -q 4`，对照日志中的 channel 行数与你图中的股数是否一致。**待本地验证**。

## 6. 本讲小结

- RDMA 传输层有 V1/V2 两个实现，`TT_HCCP` 由编译宏 `ACLSHMEM_RDMA_V2_SUPPORT`（= RDMA 支持 + Ascend950）分派；V1 走 HCCP RA 手工建链并委托 QP 管理器，V2 走 HCOMM endpoint/channel 模型。
- `DeviceQpManager` 是建 QP 的抽象基类，自带 server socket 创建与**端口递增自动分配**（成功后把实际端口回写、经控制面交换）。
- `FixedRanksQpManager` 面向对等全互联：小 id 当 server、大 id 当 client，n 个 rank 共 \( n(n-1) \) 个 QP，启动后 rank 集合不可变；`DynamicRanksQpManager` 面向 SENDER/RECEIVER 角色，后台线程按六类任务增量建链、增量注册 MR。
- device 侧 `AiQpRMAQueueInfo` 是 kernel 直驱的元数据：单 QP 时五个数组按 `rankCount` 排布，多 QP 时按 `rankCount×qpNum` 排布，队列索引 `remoteRank*qpNum + qpIdx`。
- 本轮（8d1d777）使能 Ascend950 RDMA 多 QP：`TransportOptions` 新增 `rdmaQpConfig`，经 `aclshmemx_set_qp_num` → init backend 快照 → V2 `OpenDevice` 传入；`Connect` 按 `(rankCount-1)×qpNum` 条 channel 建链（仅 XSCALE 后端允许 qpNum>1），并用 `CheckQpNumConsistency` 在 bootstrap 控制面上强制全组 QP 数一致，不一致即初始化失败。

## 7. 下一步学习建议

- **u5-l7（Ascend950 RDMA 多 QP 机制）**：本讲只讲了 Host 侧「把 QP 建出来、元数据发下去」；kernel 侧如何用 `aclshmemx_roce_qp_put_nbi/get_nbi/quiet` 按 `qp_idx` 直驱这些队列，请继续学 u5-l7 与 u5-l6。
- **u8-l8（性能测试与调优）**：用 `rdma_perftest` 的 `-q`/`-i`/`--metric lat` 系统采集多 QP 带宽/延迟曲线，验证本讲引用的性能趋势。
- **源码延伸阅读**：`device_rdma_helper.cpp`（nic 字符串解析与 `GenerateDeviceNic`）、`device_chip_info.cpp`（芯片能力探测），以及 [src/host/transport/transport_manager.cpp](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/transport_manager.cpp) 中 `ConnectWithOptions` 模板方法如何串联 Prepare/Connect/UpdateRankOptions。
