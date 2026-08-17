# u5-l1 传输层架构：TransportManager 抽象

> 本讲为 update 版本：已纳入提交 `8d1d777`（Ascend950 RDMA 使能多 QP）引入的变化——`TransportOptions` 新增 `rdmaQpConfig` 配置，`aclshmemx_set_qp_num` 从仅支持 UDMA 扩展到同时支持 ROCE。相关细节会单独标注「本轮新增」。

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 `src/host/transport` 目录在整个 SHMEM 初始化流程中的位置：它是在「建堆阶段」被内存实体（mem entity）拉起来的，而不是一个独立启动的模块。
2. 读懂 `TransportManager` 抽象基类定义的统一生命周期契约（打开设备 → 注册内存 → 准备 → 建链），并数出哪些是纯虚函数、哪些是带默认实现的虚函数。
3. 理解「引擎标识」的三层换算：用户可见的 `data_op_engine_type_t` 位掩码 → HYBM 内部位掩码 `hybm_data_op_type` → 传输层枚举 `TransportType`，以及工厂 `CreateForDataOpType` 如何据此决定创建哪个（或哪些）传输管理器。
4. 掌握 `TransportOptions` 携带的引擎配置（`udmaQpConfig`、本轮新增的 `rdmaQpConfig`）从用户 API `aclshmemx_set_qp_num` 一路传到具体引擎管理器的完整链路。
5. 能在源码中定位任意一种引擎（RDMA/SDMA/UDMA）的初始化入口 `OpenDevice`。

## 2. 前置知识

本讲建立在你已学过 u2-l2（初始化全流程源码走读）的基础上。先把几个会用到的概念用通俗语言复习/补充一遍：

- **传输层（transport）是什么**：SHMEM 的数据面引擎（RoCE/SDMA/UDMA）在使用之前需要做三件「控制面」的事——初始化网卡/通信资源、把对称堆内存注册给硬件（让远端可以直接读写）、与其他 PE 建立连接（交换内存钥匙、队列对信息）。`src/host/transport` 就是把这三件事按「同一套接口、不同引擎各自实现」的方式组织起来的层。
- **QP（Queue Pair，队列对）**：RDMA/UDMA 通信的基本单位。一对收发队列构成一条 QP，通信双方各持一端。一个 peer 连接可以配置多条 QP（多 QP 并行可提升带宽、分散链路压力），QP 数就是本讲反复出现的 `qpNum`。
- **模板方法模式（Template Method）**：基类提供一个公开方法把「流程骨架」固定下来（先 Prepare 再 Connect），把每一步的具体实现留给派生类。本讲中 `ConnectWithOptions` 就是模板方法。
- **工厂模式（Factory）**：调用方不直接 `new` 某个具体引擎类，而是把引擎标识交给静态工厂函数，由工厂决定实例化谁。好处是上层代码（mem entity）完全不知道引擎差异。
- **位掩码（bitmask）**：用一个整数的每个二进制位表示一个开关。例如 `0x01 | 0x04 = 0x05` 同时打开 MTE 与 ROCE。本讲的引擎选择全靠位掩码的按位与/或。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/host/transport/transport_manager.h](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/transport_manager.h) | `TransportManager` 抽象基类：定义全部虚接口与两个静态工厂入口 |
| [src/host/transport/transport_manager.cpp](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/transport_manager.cpp) | 工厂实现（`Create` / `CreateForDataOpType`）与 `ConnectWithOptions` 模板方法 |
| [src/host/transport/transport_def.h](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/transport_def.h) | 传输层公共数据结构：`TransportType`、`TransportOptions`（含 QP 配置）、内存注册/建链交换用的结构体 |
| src/host/transport/device_rdma/ | RoCE 引擎实现：`RdmaTransportManager` 与 `RdmaTransportManagerV2`（本轮多 QP 改动的主战场，详见 u5-l3/u5-l7） |
| src/host/transport/device_sdma/ | SDMA 引擎实现（仅 A3 平台） |
| src/host/transport/device_udma/ | UDMA 引擎实现（仅 Ascend950） |
| src/host/transport/composite_transport_manager.* | 组合管理器：多引擎并存时按优先级委托（详见 u5-l2） |
| [src/host/entity/mem_entity_default.cpp](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/entity/mem_entity_default.cpp) | 传输层的调用方：`InitTransManager()` 创建管理器并调 `OpenDevice` |
| [src/host/init/backends/shmem_init_backend.cpp](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/backends/shmem_init_backend.cpp) | 把初始化属性（引擎位掩码、QP 数）翻译成 `hybm_options` 与 `TransportOptions` 的中转站 |
| [src/host/init/shmem_init.cpp](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/shmem_init.cpp) | QP 数配置的源头：`aclshmemx_set_qp_num` 与全局配置变量 |

## 4. 核心概念与源码讲解

### 4.1 传输层在初始化流程中的位置与三层引擎标识

#### 4.1.1 概念说明

回顾 u2-l2：`aclshmemx_init_attr` 的初始化分三阶段——bootstrap 建链、HYBM 建堆、子模块就绪。传输层不属于这三个阶段中的任何一个「独立阶段」，它是在**建堆阶段内部**、由内存实体在初始化自身时顺手拉起来的：`mem entity` 创建 → `InitTransManager()` → 工厂创建传输管理器 → `OpenDevice`。也就是说，**传输层的生命周期与堆绑定**：堆建好了，远端访问的「路」也修好了。

另一个容易混淆的点是：同一个「引擎」在不同代码层有三个不同的名字。用户在初始化属性里写的是 `ACLSHMEM_DATA_OP_ROCE`，到了 HYBM 内部变成 `HYBM_DOP_TYPE_DEVICE_RDMA`，进了传输层又变成 `TT_HCCP`。三者的取值还互不相同，必须搞清换算关系，否则读代码会一头雾水。

#### 4.1.2 核心流程

从用户到传输管理器的完整链路：

```text
用户设置 option_attr.data_op_engine_type（位掩码，如 MTE|ROCE）
        │
        ▼  shmem_init_backend.cpp（bind_aclshmem_entity 内）
按位翻译成 hybm_data_op_type：ROCE位→DEVICE_RDMA、SDMA位→DEVICE_SDMA、UDMA位→DEVICE_UDMA
（MTE 位是默认项，始终存在于 bmDataOpType 中）
        │
        ▼  hybm_create_entity_with_transport_options(...)
mem entity 保存 options，稍后在 InitTransManager() 里：
        │
        ▼  TransportManager::CreateForDataOpType(options_.bmDataOpType)
按 HYBM 位掩码挑出候选引擎 → 1 个引擎直接创建；多个引擎创建 CompositeTransportManager
        │
        ▼  transportManager_->OpenDevice(transportOptions_)
具体引擎初始化网卡/设备资源，QP 数等配置在此生效
```

三层标识的对照表（注意三组取值互不相同）：

| 层 | 定义位置 | MTE | RoCE/RDMA | SDMA | UDMA |
| --- | --- | --- | --- | --- | --- |
| 用户 API 位掩码 `data_op_engine_type_t` | shmem_common_types.h | 0x01 | 0x04 | 0x02 | 0x08 |
| HYBM 内部位掩码 `hybm_data_op_type` | hybm_def.h | 1<<0 | 1<<1 | 1<<2 | 1<<3 |
| 传输层枚举 `TransportType` | transport_def.h | —（不建传输器） | TT_HCCP=0 | TT_SDMA=1 | TT_UDMA=2 |

两个值得注意的细节：

- **MTE 不进传输层工厂**。MTE（Memory Transfer Engine）是 AICore 片内搬运引擎，不需要跨节点建链与内存注册，因此 `CreateForDataOpType` 只看 RDMA/SDMA/UDMA 三个位。
- **`TT_HCCP` 对应的是 RoCE 引擎**。枚举名沿用了 CANN 通信组件 HCCP 的叫法（RoCE 网卡与拓扑信息与该通信体系相关），映射到 `RdmaTransportManager(V2)`，初读源码时不要被名字骗了。

#### 4.1.3 源码精读

用户 API 的引擎位掩码定义在公共类型头中，四个引擎各占一位：

[include/host_device/shmem_common_types.h:L78-L84](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/include/host_device/shmem_common_types.h#L78-L84) 定义 `data_op_engine_type_t`：`ACLSHMEM_DATA_OP_MTE = 0x01`、`ACLSHMEM_DATA_OP_SDMA = 0x02`、`ACLSHMEM_DATA_OP_ROCE = 0x04`、`ACLSHMEM_DATA_OP_UDMA = 0x08`——这是初始化属性 `option_attr.data_op_engine_type` 的类型，也是用户唯一需要接触的引擎开关。

HYBM 内部的对应位掩码：

[src/host/mem/heap/hybm_def.h:L37-L42](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/mem/heap/hybm_def.h#L37-L42) 定义 `hybm_data_op_type`：`HYBM_DOP_TYPE_MTE = 1U << 0`、`HYBM_DOP_TYPE_DEVICE_RDMA = 1U << 1`、`HYBM_DOP_TYPE_DEVICE_SDMA = 1U << 2`、`HYBM_DOP_TYPE_DEVICE_UDMA = 1U << 3`——注意 SDMA 与 ROCE 的位序相对用户掩码调换了，这是纯粹的历史命名结果，换算必须逐个对号，不能想当然按位平移。

把用户掩码翻译成 HYBM 掩码的中转代码：

[src/host/init/backends/shmem_init_backend.cpp:L237-L252](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/backends/shmem_init_backend.cpp#L237-L252) 在创建 entity 时先把 `options.bmDataOpType` 置为 `HYBM_DOP_TYPE_MTE`，然后逐个判断 `attributes->option_attr.data_op_engine_type` 是否含 ROCE/SDMA/UDMA 位，有就把对应的 `HYBM_DOP_TYPE_DEVICE_*` 位或进去，同时设置 `bmScope = HYBM_SCOPE_CROSS_NODE` 与 rank 信息。这段就是「三层标识换算」中第一、二层的发生地。

传输层的枚举：

[src/host/transport/transport_def.h:L39-L44](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/transport_def.h#L39-L44) 定义 `TransportType`：只有 `TT_HCCP`、`TT_SDMA`、`TT_UDMA` 三个值——再次印证 MTE 不经过传输管理器。

#### 4.1.4 代码实践

1. **实践目标**：亲手完成一次三层标识换算，验证对位掩码流向的理解。
2. **操作步骤**：
   - 假设用户初始化时设置 `option_attr.data_op_engine_type = ACLSHMEM_DATA_OP_MTE | ACLSHMEM_DATA_OP_ROCE | ACLSHMEM_DATA_OP_UDMA`。
   - 对照上面两个枚举定义，手工推算 `options.bmDataOpType` 的最终值。
   - 再推算 `CreateForDataOpType` 收到该值后，`order` 向量里会依次压入哪些 `TransportType`，最终创建的是什么对象。
3. **需要观察的现象**：纯纸面推演，观察自己是否会把 0x04（ROCE）误当成 1<<2（SDMA 的 HYBM 位）。
4. **预期结果**：用户掩码 = 0x01|0x04|0x08 = 0x0D。`bmDataOpType` 初始为 `HYBM_DOP_TYPE_MTE`(=1)，或上 `DEVICE_RDMA`(=2)、`DEVICE_UDMA`(=8)，最终 = 11。`order` 依次压入 `TT_HCCP`、`TT_UDMA`，共两个 → 创建 `CompositeTransportManager`。
5. 本实践无需 NPU 环境，属于源码阅读型实践，结论可对照 4.4.3 的工厂代码逐行验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `CreateForDataOpType` 里找不到 MTE 的分支？MTE 的数据通路（ub2gm）不需要建链吗？

**答案**：MTE 是 AICore 片内/片间的内存搬运引擎，kernel 直接通过 MTE 指令驱动（见 u4-l6），不涉及跨节点网卡连接、内存注册钥匙交换这类控制面工作，因此不需要传输管理器参与；`CreateForDataOpType` 只处理 RDMA/SDMA/UDMA 三个跨节点引擎位。

**练习 2**：用户掩码 `ACLSHMEM_DATA_OP_ROCE`（0x04）与 HYBM 掩码 `HYBM_DOP_TYPE_DEVICE_RDMA`（1<<1 = 2）数值不同，这会不会导致 bug？

**答案**：不会。两个掩码从不直接比较，中间隔着 `shmem_init_backend.cpp:L240-L243` 的显式翻译：判断用户掩码的 ROCE 位，再或上 HYBM 的 DEVICE_RDMA 位。数值不同只是两套枚举独立编号的结果；真正要防的是读代码的人把两套掩码混用。

**练习 3**：`TT_HCCP` 这个名字为什么不出现在用户文档里？

**答案**：它是传输层内部枚举，用户面向的是 `ACLSHMEM_DATA_OP_ROCE`；`TT_HCCP` 只是 RDMA 引擎在传输层的内部代号（名字源自 CANN 通信组件 HCCP 的历史叫法），在工厂 `Create` 里被映射为 `RdmaTransportManager(V2)`。

### 4.2 TransportOptions：引擎配置的载体

#### 4.2.1 概念说明

工厂创建出传输管理器后，调用方要告诉它「你是谁、全网多少人、用哪张网卡、建几条 QP」。这些参数被打包在一个普通结构体 `TransportOptions` 里，经 `OpenDevice(options)` 一次性传入。它是**控制面配置从 init 模块流向传输层的唯一入口**，理解它就理解了引擎可调参数的全集。

本轮新增点：结构体里原来只有 `udmaQpConfig`（UDMA 的 QP 数配置），提交 `8d1d777` 为支持 Ascend950 RDMA 多 QP 新增了嵌套结构 `RdmaQpConfig rdmaQpConfig`。两者都是「每个 peer 连接的 QP 条数」，默认 1。

#### 4.2.2 核心流程

`TransportOptions` 各字段的来源与去向：

| 字段 | 含义 | 来源 | 消费者 |
| --- | --- | --- | --- |
| `rankId` / `rankCount` | 本 PE 编号 / 全网 PE 数 | `attributes->my_pe` / `n_pes` | 各引擎保存为成员，建链时确定对端集合 |
| `protocol` | 引擎位掩码（即 `bmDataOpType`） | init backend 翻译结果 | 部分引擎用于日志/判断 |
| `rdmaQpConfig.qpNum` | RDMA 每 peer QP 数（本轮新增） | `aclshmemx_set_qp_num(ROCE, n)` | `RdmaTransportManagerV2::OpenDevice` |
| `udmaQpConfig.qpNum` | UDMA 每 peer QP 数 | `aclshmemx_set_qp_num(UDMA, n)` | `UdmaTransportManager::OpenDevice` |
| `role` | 本端角色（HYBM_ROLE_PEER 等） | 固定填 PEER | 建链握手 |
| `nic` / `type` | 网卡标识 / IP 类型（IpV4/IpV6） | init backend 填默认值 | RDMA 引擎解析网卡地址 |

结构体还重载了 `operator<<`，日志里会打印完整的配置快照（含本轮加入的 `rdmaQpNum` 字段），排查配置问题时直接看日志即可。

#### 4.2.3 源码精读

QP 配置结构体与 `TransportOptions` 本体：

[src/host/transport/transport_def.h:L46-L60](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/transport_def.h#L46-L60) 定义 `UdmaQpConfig`（单个字段 `qpNum`，默认 1）与 `TransportOptions`。其中 L54-L56 的嵌套结构 `RdmaQpConfig` 与成员 `rdmaQpConfig{}` 是本轮新增——与 `UdmaQpConfig` 字段相同，但作为内嵌定义直接写在 `TransportOptions` 里，因此外部引用它的类型名是 `TransportOptions::RdmaQpConfig`（init 模块的全局变量就是这么声明的）。

日志打印重载：

[src/host/transport/transport_def.h:L62-L69](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/transport_def.h#L62-L69) 的 `operator<<` 把 rankId、rankCount、protocol、`rdmaQpNum`、role、nic、iptype、`udmaQpNum` 全部输出。本轮提交同时在该输出中插入了 `rdmaQpNum=` 字段，使多 QP 配置在日志里可见。

`TransportOptions` 在哪里被填：

[src/host/init/backends/shmem_init_backend.cpp:L255-L261](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/backends/shmem_init_backend.cpp#L255-L261) 逐字段填充 `transport_options`：rankId/rankCount 取自初始化属性，`protocol` 直接复用翻译好的引擎位掩码，最后两行 L260-L261 分别把 `elem->udma_qp_num` 与 `elem->rdma_qp_num` 写入 `udmaQpConfig.qpNum` 与 `rdmaQpConfig.qpNum`。这两个 `elem` 字段来自 bind 阶段的保存（见 4.5.3）。

QP 数上限：

[include/host/shmem_host_def.h:L34](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/include/host/shmem_host_def.h#L34) 定义 `ACLSHMEM_MAX_QP_NUM = 32`——RDMA/UDMA 每 peer QP 数的统一上限，`aclshmemx_set_qp_num` 与两个引擎的 `OpenDevice` 都按「1 ≤ qpNum ≤ 32」校验。

#### 4.2.4 代码实践

1. **实践目标**：通过日志观察一份真实的 `TransportOptions` 输出。
2. **操作步骤**：
   - 按 u8-l5（日志调试）的方法把 SHMEM 日志等级调到 Debug（例如设置环境变量打开 DEBUG 日志）。
   - 在有 CANN 环境的机器上运行任一多 PE 示例（如 `examples/init`，pesize=2），在初始化阶段翻找包含 `TransportOptions(` 的日志行。
   - 无 NPU 环境时改为源码阅读：在仓库里全局搜索 `options` 在 `SdmaTransportManager::OpenDevice`（[src/host/transport/device_sdma/device_sdma_transport_manager.cpp:L44](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/device_sdma/device_sdma_transport_manager.cpp#L44)）中被 `SHM_LOG_DEBUG` 打印的位置，确认 `operator<<` 会被调用。
3. **需要观察的现象**：日志行应形如 `TransportOptions(rankId=0, count=2, protocol=3, rdmaQpNum=1, role=..., nid=10002, iptype=..., udmaQpNum=1)`。
4. **预期结果**：默认情况下 `rdmaQpNum` 与 `udmaQpNum` 均为 1；`protocol` 的值等于 HYBM 引擎掩码（如 MTE+RDMA = 3）。运行结果**待本地验证**（依赖 NPU 环境与日志配置）。

#### 4.2.5 小练习与答案

**练习 1**：`rdmaQpConfig` 为什么做成嵌套结构体而不是像 `rankId` 那样的平铺 uint32_t 字段？

**答案**：与既有的 `UdmaQpConfig` 保持同构——QP 配置是一组语义相关的参数（当前只有 qpNum，未来可能扩展），用结构体包起来后新增字段不需要改 `TransportOptions` 的字段列表，两个引擎的 QP 配置也各自独立、互不干扰。

**练习 2**：如果把 `protocol` 字段误当成「网络协议类型」（TCP/IB 之类）来理解，会造成什么误读？

**答案**：`protocol` 实际是 `options.bmDataOpType` 的原样拷贝，即 HYBM 引擎位掩码（MTE|RDMA|SDMA|UDMA 的按位组合），描述「启用了哪些数据面引擎」，与任何网络协议无关；它在 [src/host/init/backends/shmem_init_backend.cpp:L258](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/backends/shmem_init_backend.cpp#L258) 被赋值。

**练习 3**：`TransportOptions` 里 `nic` 的默认值是什么？从哪行代码可见？

**答案**：默认字符串 `"10002"`，见 [src/host/init/backends/shmem_init_backend.cpp:L278-L280](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/backends/shmem_init_backend.cpp#L278-L280)：`std::string defaultNic = "10002";` 随后拷入 options 与 transport_options。

### 4.3 TransportManager 抽象基类：统一的生命周期契约

#### 4.3.1 概念说明

`TransportManager` 是一个纯抽象基类（含纯虚函数的抽象类），它不实现任何引擎逻辑，只规定「任何传输引擎都必须会做的事」。可以把它的接口按生命周期分成四组：

1. **设备生命周期**：`OpenDevice` / `CloseDevice`——初始化与释放引擎所需的设备/网卡资源。
2. **内存注册**：`RegisterMemoryRegion` / `UnregisterMemoryRegion` / `QueryMemoryKey` / `ParseMemoryKey`——把一段地址注册给硬件，拿到可供远端使用的钥匙（memory key），建链时交换。
3. **建链**：`Prepare` / `Connect` / `AsyncConnect` / `WaitForConnected` / `UpdateRankOptions`——先准备本端信息（网卡地址、内存钥匙），再与所有对端交换并建立连接；也支持异步建链与运行期增减 rank。
4. **查询**：`GetNic` / `GetQpInfo` / `GetDeviceInfo`——把引擎相关的信息（如 QP 上下文地址）暴露给上层，供 device 侧 kernel 直驱使用。

接口分为「纯虚」（必须实现）与「带默认实现的虚函数」（可选实现）两档，这本身就传递了设计意图：建链与内存注册是所有引擎的硬性义务；`ConnectWithOptions` 这类编排逻辑则由基类统一提供。

#### 4.3.2 核心流程

一次典型的传输层生命周期（以 mem entity 视角）：

```text
InitTransManager()
  ├─ TransportManager::CreateForDataOpType(引擎位掩码)   → 得到 manager
  ├─ manager->OpenDevice(options)                        → 引擎就绪（QP 数在此校验生效）
  ├─ manager->RegisterMemoryRegion(堆的每个分段)          → 拿到 memory key
  ├─ manager->ConnectWithOptions(prepareInfo)             → 模板方法：内部先 Prepare 后 Connect
  │      （后续再次调用同一 manager 的 ConnectWithOptions 时走 UpdateRankOptions 分支）
  └─ （销毁期）manager->CloseDevice()
```

基类用 `connected_` 标志位记住「是否已经建过链」，从而把「首次建链」与「后续更新 rank 集合」两个场景统一到一个入口 `ConnectWithOptions` 里——这是典型的模板方法模式。

#### 4.3.3 源码精读

类声明与两个工厂入口：

[src/host/transport/transport_manager.h:L26-L29](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/transport_manager.h#L26-L29) 声明抽象类 `TransportManager` 及两个静态工厂：`Create(TransportType)` 按 transport 枚举创建单个引擎，`CreateForDataOpType(uint32_t)` 按 HYBM 位掩码创建（可能返回组合管理器）。

设备生命周期纯虚接口：

[src/host/transport/transport_manager.h:L40-L42](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/transport_manager.h#L40-L42) 定义 `OpenDevice(const TransportOptions&) = 0` 与 `CloseDevice() = 0`。注释 `/* 1、本地IP（NIC、Device）*/` 表明 OpenDevice 阶段处理的是**本端**资源（网卡、设备），尚不涉及对端。

内存注册与钥匙查询纯虚接口：

[src/host/transport/transport_manager.h:L50-L56](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/transport_manager.h#L50-L56) 定义 `RegisterMemoryRegion`（注释「2、注册内存」）、`UnregisterMemoryRegion`、`QueryMemoryKey`、`ParseMemoryKey` 四个纯虚函数——这就是 u2-l5 中「slice 描述符交换」之前，本地堆地址换取远端可访问钥匙的接口。

建链与 rank 更新纯虚接口：

[src/host/transport/transport_manager.h:L62-L85](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/transport_manager.h#L62-L85) 定义 `Prepare`（注释「3、建链前的准备工作」，生成本端 nic/钥匙集合）、`Connect` / `AsyncConnect` / `WaitForConnected`（注释「4、建链」，同步与异步两种形态）以及 `UpdateRankOptions`（注释「建链完成后，更新rank配置信息，可以新增rank或减少rank」）。

带默认实现的虚函数与状态位：

[src/host/transport/transport_manager.h:L90-L96](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/transport_manager.h#L90-L96) 定义 `GetNic()`（标记 `// X` 表示仅部分引擎支持）、`GetQpInfo()`、`GetDeviceInfo()` 三个**非纯虚**接口，以及 protected 成员 `connected_{false}`——派生类可选择性覆盖查询接口，`connected_` 则由基类的 `ConnectWithOptions` 维护。

上层调用点（传输层的「用户」）：

[src/host/entity/mem_entity_default.cpp:L886-L911](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/entity/mem_entity_default.cpp#L886-L911) 是 `MemEntityDefault::InitTransManager()` 的完整实现：单 rank（`rankCount <= 1`）直接跳过——没有远端就没有传输层；引擎掩码不含 RDMA/SDMA/UDMA 也跳过；否则 L899 调 `CreateForDataOpType` 创建管理器，L905 调 `OpenDevice(transportOptions_)`。这两行就是「传输层初始化入口」的精确定位。

#### 4.3.4 代码实践

1. **实践目标**：整理出 `TransportManager` 的完整虚函数清单，并区分「必须实现」与「可选实现」。
2. **操作步骤**：
   - 打开 [src/host/transport/transport_manager.h](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/transport_manager.h)，把每个虚函数抄进表格，标注是否纯虚、属于哪一组（设备/内存/建链/查询）。
   - 用 `grep -n "override" src/host/transport/device_rdma/device_rdma_transport_manager_v2.h` 查看一个具体引擎覆盖了哪些接口。
   - 把结果与本节 4.3.1 的四组分类对照。
3. **需要观察的现象**：清单里应有 12 个纯虚函数（OpenDevice、CloseDevice、RegisterMemoryRegion、UnregisterMemoryRegion、QueryMemoryKey、ParseMemoryKey、Prepare、Connect、AsyncConnect、WaitForConnected、UpdateRankOptions、GetNic，均以 `= 0` 结尾）与 3 个带默认实现的虚函数（ConnectWithOptions、GetQpInfo、GetDeviceInfo），共 15 个虚函数。
4. **预期结果**：表格成型，且发现 `GetNic` 虽是纯虚却标注了 `// X`（并非所有引擎都能给出有意义的 nic）；`ConnectWithOptions` 的默认实现在 .cpp 中（见 4.4.3），派生类通常不覆盖它。
5. 本实践为源码阅读型，无需运行环境。

#### 4.3.5 小练习与答案

**练习 1**：`OpenDevice` 与 `Prepare` 都发生在建链之前，它们的分工边界是什么？

**答案**：`OpenDevice` 只处理**本端静态资源**——解析设备号、网卡地址、按 QP 数创建 endpoint/队列资源，输入仅 `TransportOptions`，不需要任何对端信息；`Prepare` 处理的是**为交换而生的本端信息**——把 nic、各内存段的 memory key 打包进 `HybmTransPrepareOptions`，其产物将在 `Connect` 阶段与对端互换。

**练习 2**：为什么 `ConnectWithOptions` 不设计成纯虚、而由基类给出默认实现？

**答案**：它的流程对所有引擎都一样——首次调用走「Prepare → Connect → 置 connected_」，再次调用走「UpdateRankOptions」。这种编排与引擎无关，放在基类可以避免各派生类重复实现同一状态机，这正是模板方法模式的价值。

**练习 3**：`InitTransManager` 在 `rankCount <= 1` 时直接返回成功，这意味着单卡进程里传输层存在吗？

**答案**：不存在。单 rank 没有远端 PE，不需要任何网络建链与内存注册，`transportManager_` 保持为空；这也解释了为什么单进程用 SHMEM 做对称内存管理时引擎配置完全不生效。

### 4.4 工厂创建：Create 与 CreateForDataOpType

#### 4.4.1 概念说明

两个静态工厂是上层接触传输层的唯一「入口」。二者的分工：

- `Create(TransportType)`：已知单个引擎类型时创建对应管理器。内部还要处理**编译期条件**——RDMA 有 v1/v2 两代实现（Ascend950 且开启 RDMA 支持时编译 `RdmaTransportManagerV2`，否则退回 `RdmaTransportManager`）；UDMA 仅在 Ascend950（`ACLSHMEM_SOC_950`）平台编译。
- `CreateForDataOpType(uint32_t dataOpType)`：面向上层的便捷入口，把 HYBM 位掩码翻译成引擎候选序列；**多引擎时返回组合管理器** `CompositeTransportManager`（其内部按序委托，详见 u5-l2）。

#### 4.4.2 核心流程

`CreateForDataOpType` 的决策过程（伪代码）：

```text
order = []
if dataOpType 含 DEVICE_RDMA 位: order.push(TT_HCCP)   # 注意检查顺序：RDMA → SDMA → UDMA
if dataOpType 含 DEVICE_SDMA 位: order.push(TT_SDMA)
if dataOpType 含 DEVICE_UDMA 位: order.push(TT_UDMA)

if order 为空:   return nullptr              # 没有任何跨节点引擎
if order 仅 1 个: return Create(order[0])    # 单引擎：直接创建
else:            return CompositeTransportManager(order)   # 多引擎：组合管理器
```

`Create` 内部的编译期分派：

```text
switch (type):
  TT_HCCP: 有 ACLSHMEM_RDMA_V2_SUPPORT → RdmaTransportManagerV2
           否则                        → RdmaTransportManager
  TT_SDMA: → SdmaTransportManager
  TT_UDMA: 仅 ACLSHMEM_SOC_950 → UdmaTransportManager
  default: 打错误日志，返回 nullptr
```

#### 4.4.3 源码精读

单引擎工厂与编译开关：

[src/host/transport/transport_manager.cpp:L27-L46](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/transport_manager.cpp#L27-L46) 是 `Create` 的全部实现：`case TT_HCCP` 分支被 `#if defined(ACLSHMEM_RDMA_V2_SUPPORT)` 一分为二，分别 `make_shared<RdmaTransportManagerV2>()` 与 `make_shared<RdmaTransportManager>()`；`case TT_UDMA` 整个被 `#if defined(ACLSHMEM_SOC_950)` 包裹——非 950 平台该 case 直接不存在，落入 `default` 打印 `Invalid trans type` 并返回 nullptr。文件头部的条件 include（[src/host/transport/transport_manager.cpp:L16-L22](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/transport_manager.cpp#L16-L22)）与之一一对应。

多引擎分派工厂：

[src/host/transport/transport_manager.cpp:L48-L68](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/transport_manager.cpp#L48-L68) 是 `CreateForDataOpType`：先按 RDMA→SDMA→UDMA 的固定顺序把命中的 `TransportType` 压入 `order`（L51-L59），空则返回 nullptr（L61-L63），单个则转 `Create`（L64-L66），多个则构造 `CompositeTransportManager(std::move(order))`（L67）。压入顺序就是组合管理器内部的**优先级顺序**。

模板方法与查询默认实现：

[src/host/transport/transport_manager.cpp:L83-L104](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/transport_manager.cpp#L83-L104) 是 `ConnectWithOptions`：`connected_` 为假时依次调 `Prepare` 与 `Connect`，任一失败即返回错误码，全部成功后置 `connected_ = true`；已连接时改为调 `UpdateRankOptions`。另外 L70-L81 给出 `GetQpInfo`（默认返回 nullptr 并打 DEBUG 日志）与 `GetDeviceInfo`（把 `GetQpInfo()` 的返回包成 `rdmaInfoAddress`）的基类默认实现——不支持 QP 信息暴露的引擎无需覆盖它们。

两个编译宏在哪里定义：

[CMakeLists.txt:L267](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/CMakeLists.txt#L267) 在 `ACLSHMEM_RDMA_SUPPORT` 且 `SOC_TYPE = Ascend950` 时定义 `ACLSHMEM_RDMA_V2_SUPPORT`；[CMakeLists.txt:L236](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/CMakeLists.txt#L236) 在 950 平台定义 `ACLSHMEM_SOC_950`。结合 u1-l2 的结论「引擎可用性在编译期由 CANN 版本与芯片型号锁定」，工厂里的条件编译就是这一锁定在传输层的具体表现。

#### 4.4.4 代码实践

1. **实践目标**：验证「同一份代码，不同平台编译出不同的传输器集合」。
2. **操作步骤**：
   - 阅读 [CMakeLists.txt:L214-L236](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/CMakeLists.txt#L214-L236)：Ascend950 必须显式指定 `-rdma_backend` 取 `XSCALE` 或 `HNS_1825`，并由此定义 `ACLSHMEMI_RDMA_K_BACKEND_XSCALE`（或 HNS_1825）。
   - 推演三种配置下 `Create(TT_HCCP)` 的返回类型：① 非 950 平台；② 950 + 开启 RDMA 支持；③ 950 + 未开启 RDMA 支持。
   - 再推演 `Create(TT_UDMA)` 在非 950 平台的行为。
3. **需要观察的现象**：能否准确说出每种组合落到 switch 的哪个分支（或 case 是否存在）。
4. **预期结果**：① 返回 `RdmaTransportManager`（无 V2 宏）；② 返回 `RdmaTransportManagerV2`；③ 同 ①（V2 宏要求 RDMA 支持与 950 同时成立）。非 950 平台 `case TT_UDMA` 整个不存在，落入 default 返回 nullptr 并打 `Invalid trans type` 错误日志。纯源码推演，可直接对照 L27-L46 验证。

#### 4.4.5 小练习与答案

**练习 1**：`CreateForDataOpType` 为什么把 RDMA 放在候选序列第一位？这对运行时行为意味着什么？

**答案**：RDMA→SDMA→UDMA 的压入顺序决定了 `CompositeTransportManager` 内部的委托优先级（u5-l2 详解）：同样是「跨节点引擎可用」，组合管理器会优先尝试 RDMA 通路，失败/不满足条件再降级到 SDMA、UDMA。

**练习 2**：在非 Ascend950 平台上，用户错误地在 `data_op_engine_type` 里设置了 `ACLSHMEM_DATA_OP_UDMA`，会在哪一步以什么方式失败？

**答案**：不是编译期失败，而是运行到 `Create(order.front())` 时，`case TT_UDMA` 因 `ACLSHMEM_SOC_950` 未定义而不存在，落入 default 分支，`SHM_LOG_ERROR("Invalid trans type")` 并返回 nullptr；随后 [mem_entity_default.cpp:L900-L903](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/entity/mem_entity_default.cpp#L900-L903) 判空并以 `ACLSHMEM_NOT_SUPPORTED` 报错。（另见 u1-l2：非 950 平台在 CMake 构建层通常已排除 UDMA 支持。）

**练习 3**：`Create` 返回 `std::shared_ptr<TransportManager>` 而不是裸指针或 `unique_ptr`，有什么考虑？

**答案**：`shared_ptr` 允许 mem entity 与组合管理器等多个持有者共享同一管理器对象的生命周期（组合管理器持有子管理器，上层持有组合管理器），析构时机由引用计数统一管理，避免手工 CloseDevice/释放顺序问题。

### 4.5 QP 配置的传递链路：从 set_qp_num 到引擎（本轮新增重点）

#### 4.5.1 概念说明

`TransportOptions` 里的 `rdmaQpConfig` / `udmaQpConfig` 不是用户在初始化属性里直接填的，而是走了一条「进程级全局配置 → init 阶段快照 → backend 暂存 → TransportOptions → 引擎成员变量」的长链路。本轮提交 `8d1d777` 把这条链路从「仅 UDMA」扩展到「RDMA + UDMA 双引擎」，理解这条链路是本讲实践任务的核心，也是 u5-l7（RDMA 多 QP 机制）的前置。

链路上的关键规则（承接 u2-l2 已建立的认知）：

- QP 数是**进程级配置**：在 `aclshmemx_init_attr` 之前调用 `aclshmemx_set_qp_num(engine, qp_num)` 设置。
- 配置在**实例存活期间冻结**：任何 SHMEM 实例初始化成功后再改配置会返回 `ACLSHMEM_NOT_SUPPORTED`。
- **最后一个实例 finalize 后复位解冻**：全局配置恢复默认值 1。
- 取值范围 1~32（`ACLSHMEM_MAX_QP_NUM`），且全组各 PE 必须一致（由 v2 管理器建链时校验，见 u5-l3/u5-l7）。

#### 4.5.2 核心流程

```text
用户: aclshmemx_set_qp_num(ACLSHMEM_DATA_OP_ROCE, 8)
  │  (shmem_init.cpp: 写全局 g_rdma_qp_config.qpNum = 8)
  ▼
aclshmemx_init_attr(...)
  │  shmem_init.cpp L1004: bind_aclshmem_entity(..., g_udma_qp_config, g_rdma_qp_config.qpNum)
  ▼
aclshmemi_init_backend::bind_aclshmem_entity
  │  (shmem_init_backend.cpp L97-L98: 存入 elem->udma_qp_num / elem->rdma_qp_num)
  ▼
创建 entity 时 (L255-L261)
  │  transport_options.rdmaQpConfig.qpNum = elem->rdma_qp_num
  │  hybm_create_entity_with_transport_options(entity_id, &options, &transport_options, 0)
  ▼
MemEntityDefault::InitTransManager (mem_entity_default.cpp L899-L905)
  │  CreateForDataOpType(...) → manager; manager->OpenDevice(transportOptions_)
  ▼
引擎 OpenDevice
  ├─ RdmaTransportManagerV2::OpenDevice: qpNum_ = options.rdmaQpConfig.qpNum   (v2 L268)
  └─ UdmaTransportManager::OpenDevice:   qp_num = options.udmaQpConfig.qpNum   (udma L285)
  ▼
最后一个实例 finalize: g_rdma_qp_config / g_udma_qp_config 复位为默认，解冻
```

#### 4.5.3 源码精读

配置源头——用户 API 与全局变量：

[src/host/init/shmem_init.cpp:L103-L104](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/shmem_init.cpp#L103-L104) 定义两个进程级静态配置 `g_udma_qp_config`（类型 `UdmaQpConfig`）与 `g_rdma_qp_config`（类型 `TransportOptions::RdmaQpConfig`，本轮新增）。

[src/host/init/shmem_init.cpp:L615-L637](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/shmem_init.cpp#L615-L637) 是 `aclshmemx_set_qp_num` 全文：加锁后先做范围校验（[L176](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/shmem_init.cpp#L176) 的 `is_valid_rdma_qp_num` 要求 1 ≤ qp_num ≤ 32），再检查冻结标志 `g_qp_config_frozen`，然后按引擎分流——`ACLSHMEM_DATA_OP_ROCE` 写 `g_rdma_qp_config`（L627-L628，本轮新增分支）、`ACLSHMEM_DATA_OP_UDMA` 写 `g_udma_qp_config`（L629-L630），其他引擎打 WARN 并返回 `ACLSHMEM_NOT_SUPPORTED`。

快照点——init 时把全局配置交给 backend：

[src/host/init/shmem_init.cpp:L1003-L1005](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/shmem_init.cpp#L1003-L1005) 在初始化主流程中调用 `init_manager->bind_aclshmem_entity(attributes, &g_state, &g_boot_handle, ..., g_udma_qp_config, g_rdma_qp_config.qpNum)`——全局配置在这里被「快照」进实例上下文，此后冻结期内全局变量的修改不再影响已初始化的实例。

暂存与转填——backend 两级中转：

[src/host/init/backends/shmem_init_backend.cpp:L81-L98](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/backends/shmem_init_backend.cpp#L81-L98) `bind_aclshmem_entity` 把两个 QP 数存进该实例的 `entity_member`（L97-L98：`elem->udma_qp_num = udma_qp_config.qpNum; elem->rdma_qp_num = rdma_qp_num;`；字段声明见 [src/host/init/backends/shmemi_init_backend.h:L51-L52](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/backends/shmemi_init_backend.h#L51-L52)，默认值均为 1）。

[src/host/init/backends/shmem_init_backend.cpp:L260-L261](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/backends/shmem_init_backend.cpp#L260-L261) 在创建 entity 时把它们填入 `transport_options`，随后 L283/L291 经 `hybm_create_entity_with_transport_options` 一起传给内存实体。

消费点——两个引擎的 OpenDevice：

[src/host/transport/device_rdma/device_rdma_transport_manager_v2.cpp:L265-L278](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/device_rdma/device_rdma_transport_manager_v2.cpp#L265-L278) `RdmaTransportManagerV2::OpenDevice` 在 L268 取出 `options.rdmaQpConfig.qpNum` 存入成员 `qpNum_`，随后双重校验：范围必须 1~32（L269-L272）；非 XSCALE 后端时多 QP 直接 `ACLSHMEM_NOT_SUPPORTED`（L273-L278，受编译宏 `ACLSHMEMI_RDMA_K_BACKEND_XSCALE` 控制，该宏由 [CMakeLists.txt:L222](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/CMakeLists.txt#L222) 的 `-rdma_backend=XSCALE` 定义）。

[src/host/transport/device_udma/device_udma_transport_manager.cpp:L283-L299](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/device_udma/device_udma_transport_manager.cpp#L283-L299) `UdmaTransportManager::OpenDevice` 同样在入口取出 `options.udmaQpConfig.qpNum` 并校验范围；额外的约束是 relay 模式（`ACLSHMEM_UDMA_RELAY_ENABLED`）下 QP 数必须为 1。

复位点——最后一个实例 finalize：

[src/host/init/shmem_init.cpp:L1152-L1156](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/shmem_init.cpp#L1152-L1156) 在 `is_last_instance` 为真时把 `g_rdma_qp_config`、`g_udma_qp_config` 重新默认构造（qpNum 回到 1）并清除 `g_qp_config_frozen`——配置生命周期与「是否存在任何存活实例」严格对齐。

#### 4.5.4 代码实践

1. **实践目标**：独立梳理 `rdmaQpConfig` 的赋值点与消费点，画出传递链路图。
2. **操作步骤**：
   - 在仓库根目录执行 `grep -rn "rdmaQpConfig" src/ include/`，记录每一处出现的文件与行号。
   - 按出现顺序把它们归类为：定义（transport_def.h）、写入（set_qp_num / backend 转填）、传递（bind、hybm_create_entity_with_transport_options、OpenDevice 入参）、读取（v2 管理器 L268）。
   - 用箭头图把上述节点连起来，与 4.5.2 的流程图对照。
   - 进阶：再对 `udmaQpConfig` 做一遍同样的梳理，标出两条链路的对称性与不对称处（提示：bind 接口对二者传参形式不同——一个传结构体引用、一个传 uint32_t）。
3. **需要观察的现象**：`rdmaQpConfig` 的出现点应集中在 transport_def.h、shmem_init.cpp、shmem_init_backend.cpp、device_rdma_transport_manager_v2.cpp 四个文件，且**没有**出现在 device_udma 目录。
4. **预期结果**：得到一条「`aclshmemx_set_qp_num` → `g_rdma_qp_config` → `bind_aclshmem_entity` → `elem->rdma_qp_num` → `transport_options.rdmaQpConfig.qpNum` → `hybm_create_entity_with_transport_options` → `InitTransManager`/`OpenDevice` → `RdmaTransportManagerV2::qpNum_`」的完整链路；不对称处见 [src/host/init/backends/shmemi_init_backend.h:L75](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/backends/shmemi_init_backend.h#L75)——bind 接口对 UDMA 收 `const UdmaQpConfig&`、对 RDMA 只收 `uint32_t rdma_qp_num`。
5. 本实践为源码阅读型，无需 NPU 环境。

#### 4.5.5 小练习与答案

**练习 1**：在实例 A 存活期间调用 `aclshmemx_set_qp_num(ROCE, 16)` 会发生什么？返回码是什么？

**答案**：返回 `ACLSHMEM_NOT_SUPPORTED`。`aclshmemx_set_qp_num` 在 [shmem_init.cpp:L622-L625](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/shmem_init.cpp#L622-L625) 检查 `g_qp_config_frozen`，实例初始化成功后该标志为真，直接拒绝修改。

**练习 2**：为什么 QP 数校验出现在两处（`aclshmemx_set_qp_num` 与各引擎 `OpenDevice`）？删掉引擎侧的校验可以吗？

**答案**：不可以。`set_qp_num` 只能保证「用户经该 API 设置的值合法」，但 `TransportOptions` 的默认值、多实例场景下更早设置的值都可能绕过该 API；引擎侧 `OpenDevice` 是配置真正生效前的最后一道防线（v2 管理器还额外校验后端类型——非 XSCALE 时 qpNum 必须 为 1）。防御性校验放在消费端是分布式配置传递的惯例。

**练习 3**：`g_rdma_qp_config` 的复位为什么放在 `is_last_instance` 分支里，而不是每次 finalize 都复位？

**答案**：SHMEM 支持单进程多实例（u8-l1）：实例 A finalize 后实例 B 仍可能存活并已快照了配置；若此时复位并解冻，用户可在 B 存活期间修改全局配置，再初始化实例 C 时同进程内出现两套 QP 配置，破坏一致性。只有最后一个实例退出后，进程回到「无实例」状态，复位才是安全的。

## 5. 综合实践

**任务：手工绘制传输层一层类图 + 虚函数职责表 + rdmaQpConfig 链路图（本讲官方实践任务）。**

具体步骤：

1. **虚函数接口列表**：通读 [transport_manager.h](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/transport_manager.h)，整理 15 个虚函数的表格（12 纯虚 + 3 默认实现），按「设备 / 内存注册 / 建链 / 查询」四组归类，标注哪些被 `RdmaTransportManagerV2`、`SdmaTransportManager`、`UdmaTransportManager`、`CompositeTransportManager` 覆盖（用 `grep -n "override" src/host/transport/*/**.h` 逐一核对）。

2. **一层类图**（参考答案，读者应自己画一遍再对照）：

   ```text
                    ┌──────────────────────────────┐
                    │  TransportManager (抽象)      │
                    │  + OpenDevice()/CloseDevice()│
                    │  + Register/UnregisterMR()   │
                    │  + Query/ParseMemoryKey()    │
                    │  + Prepare()/Connect()       │
                    │  + AsyncConnect()/Wait...()  │
                    │  + UpdateRankOptions()       │
                    │  + GetNic()                  │
                    │  # connected_                │
                    │  + ConnectWithOptions() ←默认实现（模板方法）
                    │  + GetQpInfo()/GetDeviceInfo() ←默认实现
                    └──────────────┬───────────────┘
        ┌──────────────┬───────────┼────────────────┬──────────────────┐
        ▼              ▼           ▼                ▼                  ▼
   RdmaTransport  RdmaTransport  SdmaTrans      UdmaTrans       CompositeTransport
   Manager        ManagerV2      Manager        Manager         Manager（持有多个子管理器，
   （v1,非950或    （950+RDMA     （仅 A3）      （仅 950）        按 RDMA→SDMA→UDMA 优先级
   未开V2）        支持）                                         委托，见 u5-l2）
   ```

   各引擎 `OpenDevice`/`CloseDevice` 职责速查（均已在源码确认）：

   | 引擎 | OpenDevice 主要职责 |
   | --- | --- |
   | RdmaTransportManagerV2 | 取设备物理 ID；保存 rankId/rankCount/role；读取 `rdmaQpConfig.qpNum` 并校验（1~32、非 XSCALE 须为 1）；从拓扑解析网卡 IP；创建 endpoint、初始化监听端口、生成本端 nicInfo（[device_rdma_transport_manager_v2.cpp:L255-L304](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/device_rdma/device_rdma_transport_manager_v2.cpp#L255-L304)） |
   | RdmaTransportManager | v1 版本，无多 QP 能力（本讲不展开，见 u5-l3） |
   | SdmaTransportManager | 保存 rankId/rankCount；查询 vector core 数并创建通信流（[device_sdma_transport_manager.cpp:L39-L51](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/device_sdma/device_sdma_transport_manager.cpp#L39-L51)） |
   | UdmaTransportManager | 校验 `udmaQpConfig.qpNum`（1~32；relay 模式须为 1）；获取设备 ID 后创建 HCOMM 通道资源（[device_udma_transport_manager.cpp:L283-L299](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/device_udma/device_udma_transport_manager.cpp#L283-L299)） |
   | CompositeTransportManager | 逐个转发 OpenDevice/CloseDevice 给子管理器（u5-l2 展开） |

   `CloseDevice` 的统一语义是释放 OpenDevice 建立的本端资源（endpoint、队列、通道、流），使进程可以干净退出或重新初始化。

3. **rdmaQpConfig 链路图**：按 4.5.4 的实践独立完成，并与 4.5.2 的参考流程图对照；重点确认「赋值点只有两处」——用户侧 `aclshmemx_set_qp_num` 写全局变量、backend 转填 `transport_options`；消费点唯一——`RdmaTransportManagerV2::OpenDevice` L268。

4. **验证方式**：全部为源码阅读型工作，无需 NPU；若在有 CANN 环境的机器上，可额外运行 4.2.4 的日志实践，用真实日志行验证 `rdmaQpNum=` 字段输出。

## 6. 本讲小结

- 传输层在建堆阶段由 `MemEntityDefault::InitTransManager()` 拉起：`CreateForDataOpType` 按引擎位掩码创建管理器，`OpenDevice` 完成引擎初始化；单 rank 或无跨节点引擎时传输层不创建。
- 引擎标识有三层且取值互不相同：用户掩码 `data_op_engine_type_t`（MTE=0x01/SDMA=0x02/ROCE=0x04/UDMA=0x08）→ HYBM 掩码 `hybm_data_op_type` → 传输枚举 `TransportType`（TT_HCCP 对应 RoCE）；翻译发生在 `shmem_init_backend.cpp`，MTE 不进传输层。
- `TransportManager` 用 12 个纯虚函数规定「打开设备 → 注册内存 → 准备 → 建链 → 增删 rank」的引擎义务，用 3 个带默认实现的虚函数（`ConnectWithOptions` 模板方法、`GetQpInfo`/`GetDeviceInfo`）提供统一编排与兜底查询。
- 工厂带编译期分派：`ACLSHMEM_RDMA_V2_SUPPORT` 决定 RDMA 用 v2 还是 v1 管理器，`ACLSHMEM_SOC_950` 决定 UDMA 是否存在；多引擎时返回 `CompositeTransportManager`，内部优先级为 RDMA→SDMA→UDMA。
- `TransportOptions` 是控制面配置进入传输层的唯一载体；本轮新增的 `rdmaQpConfig`（与既有 `udmaQpConfig` 同构，默认 1、上限 32）走「set_qp_num → 全局变量 → bind 快照 → backend 暂存 → TransportOptions → 引擎 OpenDevice」链路，实例存活期间冻结、最后一个实例 finalize 后复位。

## 7. 下一步学习建议

- **u5-l2（组合传输管理器）**：弄清 `CompositeTransportManager` 如何按本讲看到的优先级顺序把 12 个纯虚接口逐个委托给子管理器，以及组合掩码创建与回退行为。
- **u5-l3（RDMA 传输与 QP 管理）**：顺着本讲的 `OpenDevice` 继续往下读 `Prepare`/`Connect`——v2 管理器如何按 `qpNum_` 为每个 peer 创建多条 QP、`CheckQpNumConsistency` 如何用 bootstrap allgather 校验全组 QP 数一致。
- **u5-l6（AICore 直驱引擎低阶接口）**：看本讲 `GetQpInfo` 暴露出去的 QP 上下文信息，最终如何被 kernel 侧 `aclshmemx_roce_qp_put_nbi` 等接口按 `qp_idx` 直驱使用；多 QP 全链路总结见 u5-l7。
- 若想回补背景：堆与 slice 交换（u2-l5）解释了 `RegisterMemoryRegion` 注册的内存从何而来；bootstrap（u2-l3）解释了 `Connect` 阶段交换 nic/钥匙所用的控制面。
