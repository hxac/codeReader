# u5-l4 SDMA 与 UDMA 传输管理器

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 SDMA 与 UDMA 两个传输管理器在「初始化依赖」上的根本差异：SDMA 是**本地资源准备型**引擎（不建链、不注册 MR），UDMA 是**远端建链型**引擎（解析拓扑、经 HCOMM 建 endpoint/channel）。
2. 理解两个引擎的平台约束从哪里来：SDMA 仅 A3（`__NPU_ARCH__ == 2201`），UDMA 仅 Ascend950（`ACLSHMEM_SOC_950`），并知道约束分别在编译期和运行期如何生效。
3. 掌握 UDMA 对 CANN HCOMM 资源接口（`HcommEndpointCreate` / `HcommMemReg` / `HcommChannelCreate` / `HcommChannelGetStatus`）的依赖方式。
4. 理解本轮（HEAD `9afc391`）明确的 UDMA 能力约束：**UDMA 不支持自发送（self-send）**。对本 rank 的高阶 put/get 一律回落 MTE；entity 层 `CanReachDataOperators` 也不再对本 rank 通告 UDMA 位——这是与 device 侧分派宏构成的双层防御。
5. 能在有 Ascend950 环境时运行 `examples/udma_demo` 并解释它用了哪个引擎、为什么不会用 UDMA 访问自己。

## 2. 前置知识

本讲承接 u5-l1（传输层架构）与 u5-l3（RDMA 与 QP 管理）。先用通俗语言补几个新概念：

- **HCOMM**：CANN 9.1.0 起提供的「通信资源管理」动态加载接口族，位于 `src/host/utils/under_api/dl_hcomm_api.cpp`。UDMA 引擎自己不发明建链协议，而是把 endpoint、内存注册、channel（通道）的创建全部委托给 HCOMM。可以把它理解为「CANN 帮你管网卡和队列，SHMEM 只负责填表」。
- **EID（Endpoint ID）**：统一远程内存访问（URMA）体系里的端点标识，粗略类比「网卡的端口号」。Ascend950 的 Clos 网络中，一块 NPU 有多个 EID，去往不同 peer 要从不同 EID 出发。`TopoQuerier::GetEidRoutes` 负责为每个 peer 解析「本端从哪个 EID 出、对端从哪个 EID 入」。
- **STARS**：SDMA 路径依赖的片上搬运服务，由一个 AICPU 内置算子（`aclnnShmemSdmaStarsQuery`）在初始化时激活。SDMA 管理器的核心工作就是把 stream/队列信息打包交给这个算子。
- **self-send（自发送）**：指 `pe == my_pe` 的 RMA/AMO 调用，即 kernel 把数据搬给「同一 PE 自己」。MTE 天然支持（本质是本地拷贝），而 UDMA 的通道与路由只为**真实远端 peer** 建立，自发送没有可用的硬件路径——这就是本讲 4.4 节的主角。
- **topo_list**：device 侧全局状态里的引擎位图数组，`topo_list[pe]` 记录「到 pe 可用哪些引擎」。u5-l1 已讲过它由 Host 侧 `CanReachDataOperators` 逐 rank 计算后镜像到 device；本讲 4.4 会看到它的生成规则在本轮被收紧。

若你对「传输管理器在建堆阶段由 `MemEntityDefault::InitTransManager` 拉起」不熟悉，请先回看 u5-l1 的 4.1 节。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/host/transport/device_sdma/device_sdma_transport_manager.cpp](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/host/transport/device_sdma/device_sdma_transport_manager.cpp) | SDMA 传输管理器实现：查核数、建 STARS 流、调 AICPU 算子，全部本地动作 |
| [src/host/transport/device_sdma/device_sdma_transport_manager.h](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/host/transport/device_sdma/device_sdma_transport_manager.h) | SDMA 管理器类定义：`host_stream_info_t`/`sdma_op_res_info_t` 与一堆 no-op 虚函数 |
| [src/host/transport/device_udma/device_udma_transport_manager.cpp](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/host/transport/device_udma/device_udma_transport_manager.cpp) | UDMA 传输管理器实现：拓扑解析、HCOMM endpoint/channel、udmaInfo 表构建（本讲主战场） |
| [src/host/transport/device_udma/device_udma_def.h](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/host/transport/device_udma/device_udma_def.h) | 控制面侧的 SQ/CQ/Mem 结构体定义，与 device 数据面布局逐字节对齐 |
| [src/host/entity/mem_entity_default.cpp](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/host/entity/mem_entity_default.cpp) | 传输层的调用方：`InitTransManager` 建管理器；`CanReachDataOperators` 按 rank 通告可达引擎（本轮加入本 rank 守卫） |
| [src/device/gm2gm/shmem_device_rma.hpp](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/device/gm2gm/shmem_device_rma.hpp) | device 侧高阶 RMA 的引擎分派宏（本轮加入 `pe != mype` 守卫，见 4.4） |
| [src/device/gm2gm/engine/shmem_device_sdma.hpp](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/device/gm2gm/engine/shmem_device_sdma.hpp) | SDMA 编译期平台开关 `ACLSHMEM_TRANSPORT_SDMA_SUPPORTED`（`__NPU_ARCH__ == 2201` 即 A3） |
| [src/host/transport/transport_manager.cpp](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/host/transport/transport_manager.cpp) | 传输工厂：`TT_SDMA`/`TT_UDMA` 分支及引擎优先级 |
| [examples/udma_demo/main.cpp](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/examples/udma_demo/main.cpp) | UDMA 演示示例：以 `ACLSHMEM_DATA_OP_UDMA` 引擎位初始化后下发 kernel |
| [tests/unittest/host/mem/udma_mem/udma_mem_host_test.cpp](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/tests/unittest/host/mem/udma_mem/udma_mem_host_test.cpp) | 本轮新增的平台门控用例 `TestShmemUDMAHighLevelLocalRma` |
| [tests/unittest/device/mem/udma_mem/udma_mem_kernel.cpp](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/tests/unittest/device/mem/udma_mem/udma_mem_kernel.cpp) | 配套 kernel：强制置位本 PE 的 UDMA topo 位后仍验证高阶接口走 MTE |

## 4. 核心概念与源码讲解

本讲的最小模块有四个：SDMA 管理器（4.1）、UDMA 的 OpenDevice 与拓扑/EID 解析（4.2）、UDMA 建链与 udmaInfo 下发（4.3）、UDMA 不支持自发送的约束（4.4）。

### 4.1 SDMA 传输管理器：本地资源准备型引擎

#### 4.1.1 概念说明

SDMA（System DMA）是 A3 平台上由片上搬运服务（STARS）承载的 DMA 引擎。它和 u5-l3 讲的 RDMA 有一个本质区别：**RDMA 需要跨 rank 建链，而 SDMA 的初始化完全是本地的**——它不连接任何远端，不注册任何 MR，不交换任何描述符。`TransportManager` 基类里那些为建链服务的虚函数（`Connect`、`WaitForConnected`、`RegisterMemoryRegion` 等）在 SDMA 这里全部是直接返回成功的空实现。

为什么可以这样「偷懒」？因为 SDMA 的数据面由 AICore 直接向片上队列提交 WQE（见 u5-l6 的低阶直驱接口），控制面唯一要做的，是把「每个 AIV 核对应一条 stream/队列」的资源表准备好，并唤醒常驻的 AICPU 搬运算子。

平台约束方面，SDMA 仅支持 A3：device 侧的编译开关写作 `__NPU_ARCH__ == 2201`（2201 即 A3 的 NPU 架构号），非 A3 平台整个 SDMA 数据面退化为空。官方支持矩阵（[docs/quickstart.md](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/docs/quickstart.md)）也明确标注「SDMA（仅支持 A3）」。

#### 4.1.2 核心流程

`OpenDevice` 的六步（严格按源码顺序）：

```text
OpenDevice(options)
 ├─ 1. GetVectorCoreNum(channel_num)     # 查询 AIV 向量核数，作为通道数；查询失败回落 72
 ├─ 2. CreateStarsStreams(channel_num)   # 每核建一条 DEVICE_USE_ONLY 流，记录 stream_id/sq_id/cq_id/die_id
 ├─ 3. MallocSdmaWorkspace(28 KB)        # AICPU 与 AIV 的共享工作区
 ├─ 4. CreateNotifyIds()                 # 每核一个 notify，id 写入共享工作区固定偏移
 ├─ 5. CopyHostOpResToDevice()           # 把 stream 表 + 工作区地址打成 op_res_info 拷到 device
 └─ 6. LaunchSdmaAicpuKernel(...)        # aclnn 二段式调用常驻 AICPU 算子，激活 STARS 服务
```

之后 `Connect()` 是 no-op：SDMA 没有「远端」要连。`CloseDevice()` 按相反顺序释放流、notify 与两块 device 内存。

#### 4.1.3 源码精读

SDMA 的 `OpenDevice` 一眼可见其「纯本地」特性——全程只有 ACL 运行时调用，没有任何 bootstrap 交换：[src/host/transport/device_sdma/device_sdma_transport_manager.cpp:L39-L68](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/host/transport/device_sdma/device_sdma_transport_manager.cpp#L39-L68)。六步与上面流程图一一对应，成功后置 `inited_ = true`。

核数查询的容错值得注意：[L70-L89](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/host/transport/device_sdma/device_sdma_transport_manager.cpp#L70-L89) 先试 `aclrtGetDeviceInfo(ACL_DEV_ATTR_VECTOR_CORE_NUM)`，失败再试 `aclGetDeviceCapability`，仍失败则**告警并回落默认值 72**（`MAX_AIV_CORE_NUM`，[L32](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/host/transport/device_sdma/device_sdma_transport_manager.cpp#L32)），而不是报错退出——通道数只影响并发度，不该让初始化失败。

`CreateStarsStreams` 是资源准备的核心：[L110-L169](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/host/transport/device_sdma/device_sdma_transport_manager.cpp#L110-L169) 用 `ACL_STREAM_DEVICE_USE_ONLY` 标志建流（这种流不进通用调度，专供 device 侧直接驱动），再逐条取出 `stream_id / sq_id / cq_id / logic_cq_id` 与 `die_id` 填进 `host_stream_info_t`——这张表就是数据面提交 WQE 时要引用的「队列坐标」。结构体定义见 [device_sdma_transport_manager.h:L26-L43](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/host/transport/device_sdma/device_sdma_transport_manager.h#L26-L43)，注释强调它与 AICPU 算子侧的定义相同、按 64 字节对齐。

最后一步调 AICPU 内置算子：[L236-L279](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/host/transport/device_sdma/device_sdma_transport_manager.cpp#L236-L279) 是标准的 aclnn 二段式调用——先用 `AclnnShmemSdmaStarsQueryGetWorkspaceSize` 拿 executor，再 `AclnnShmemSdmaStarsQuery` 在专用流上启动并同步。输入张量就是第 5 步下发的 stream 表地址和工作区地址。

而头文件里的一排空实现最能说明 SDMA 的定位：[device_sdma_transport_manager.h:L53-L64](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/host/transport/device_sdma_transport_manager.h#L53-L64) 中 `RegisterMemoryRegion`、`Connect`、`WaitForConnected`、`UpdateRankOptions` 等全部直接 `return ACLSHMEM_SUCCESS`。对照 u5-l1 的 12 个虚函数清单，SDMA 真正干活的只有 `OpenDevice`/`CloseDevice`。

#### 4.1.4 代码实践

1. **实践目标**：确认 SDMA 的平台开关与「no-op 管理器」两个结论。
2. **操作步骤**：
   - 打开 [src/device/gm2gm/engine/shmem_device_sdma.hpp:L19-L23](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/device/gm2gm/engine/shmem_device_sdma.hpp#L19-L23)，阅读 `ACLSHMEM_TRANSPORT_SDMA_SUPPORTED` 的条件编译；
   - 在仓库根目录执行 `grep -n "TT_SDMA" src/host/transport/transport_manager.cpp`，找到工厂分支；
   - 数一数 `device_sdma_transport_manager.h` 中有多少个虚函数是空实现。
3. **需要观察的现象**：SDMA 支持宏只在 `__NPU_ARCH__ == 2201` 时为 1；工厂分支无平台 `#if` 包裹（对比 TT_UDMA 分支的 `ACLSHMEM_SOC_950`）。
4. **预期结果**：非 A3 平台上高阶 RMA 分派宏 `ACLSHMEM_SDMA_TRANSPORT_ENABLED` 恒为假，即使 `topo_list` 里误设了 SDMA 位也不会走 SDMA——编译期开关是第一道闸门。（待本地验证：在有 A3 的环境上编译后运行任一示例并打开 debug 日志，观察 `init sdma success` 与 `create N stars streams success` 两行。）

#### 4.1.5 小练习与答案

**练习 1**：SDMA 管理器没有实现 `RegisterMemoryRegion` 的实际逻辑，那 SDMA 数据面访问远端内存靠什么授权？
**答案**：SDMA 走片内/节点内路径，可达性由内存实体层的 `SdmaReaches`（底层是 `CheckSdmaReaches` → `IsSdmaAccessible`，按 superPod/server/逻辑设备判断物理可达，见 [hybm_drv_device_mem_segment.cpp:L449-L457](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/host/mem/heap/hybm_drv_device_mem_segment.cpp#L449-L457)）决定；不需要 RDMA 那种 lkey/rkey 授权机制，所以注册接口为空。

**练习 2**：为什么 `GetVectorCoreNum` 失败时选择「回落 72」而不是返回错误？
**答案**：核数只决定建多少条 stream（并发通道数），取少了影响吞吐、取多了浪费资源，但都不影响正确性。库的设计取向是：非关键探测失败给默认值并打 WARN，让初始化继续；这与 `OpenDevice` 中其余步骤「失败即中止」（`ACLSHMEM_CHECK_RET`）形成对比。

### 4.2 UDMA 的 OpenDevice：拓扑解析与 HCOMM endpoint

#### 4.2.1 概念说明

UDMA 是 Ascend950 平台上由 AIV（向量核）直驱的 DMA 引擎。与 SDMA 相反，它是**重控制面**的引擎：数据面（kernel 里一句 `aclshmemx_udma_put_nbi`）非常轻，但控制面要先解决三个问题——

1. 我这块 NPU 到每个 peer 应该从哪个 EID 出发？（拓扑路由）
2. 每对 (本 rank, peer) 的通信通道（channel，语义上等价于 QP）如何建立？（HCOMM）
3. 建好的队列上下文如何交给 device 数据面？（udmaInfo 表，见 4.3）

平台约束同样是编译期的：[CMakeLists.txt:L240-L243](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/CMakeLists.txt#L240-L243) 在 `SOC_TYPE STREQUAL "Ascend950"` 时定义 `ACLSHMEM_SOC_950`，传输工厂里 `TT_UDMA` 分支被这个宏包裹（[transport_manager.cpp:L38-L41](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/host/transport/transport_manager.cpp#L38-L41)）。此外 UDMA 运行期强依赖 CANN ≥ 9.1.0 提供的 HCOMM 接口，符号经 `dl_hcomm_api` 动态加载、缺失即初始化失败（示例 README 有明确说明）。

#### 4.2.2 核心流程

`OpenDevice` → `PrepareOpenDevice` 的主干：

```text
OpenDevice(options)
 ├─ 校验 qp_num ∈ [1, 32]；relay 模式强制 qp_num == 1
 ├─ user_dev_id → logic_dev_id → phy_dev_id 换算
 └─ PrepareOpenDevice(device_id, rank_count)
     ├─ TopoReader::ParseRootInfo / ParseTopoInfo   # 读 rootinfo 与系统拓扑 XML
     ├─ GetLocalId / GetEidCount                    # 本机 NPU 编号与 EID 数量
     ├─ bootstrap allgather：各 rank 的 eid_count、local_id、同步 endpoint
     ├─ 对每个 peer（跳过本 rank！）：
     │    ├─ TopoQuerier::GetEidRoutes(peer, qp_num 或 1)   # 解析 (local_eid, remote_eid)
     │    └─ CreateEndpoint(local_eid_index, eid_raw)        # HcommEndpointCreate + 监听端口
     └─ relay 模式追加：allgather 出 N×N 全路由矩阵
```

注意流程图里那句「跳过本 rank」——它不是 incidental 的优化，而是 UDMA 能力边界的直接体现，4.4 节展开。

#### 4.2.3 源码精读

`OpenDevice` 入口先做参数闸门：[src/host/transport/device_udma/device_udma_transport_manager.cpp:L283-L295](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/host/transport/device_udma/device_udma_transport_manager.cpp#L283-L295) 校验 `qp_num` 非零且不超过 `ACLSHMEM_MAX_QP_NUM`，并在 relay 编译模式下强制 `qp_num == 1`（relay 把多 QP 的 slot 空间让给了绕路矩阵，两者互斥）。随后 [L297-L324](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/host/transport/device_udma/device_udma_transport_manager.cpp#L297-L324) 完成 user/logic 设备号换算并调用 `PrepareOpenDevice`。

拓扑解析是 UDMA 独有的重头戏：[L1392-L1450](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/host/transport/device_udma/device_udma_transport_manager.cpp#L1392-L1450) 依次解析 rootinfo 文件与拓扑 XML，取出 `local_id`、`eid_count`，再用 **bootstrap 控制面的 allgather**（u2-l3 讲过的 KV 原语）把各 rank 的 EID 数量、本机编号、同步 endpoint 收齐——UDMA 的路由计算需要知道「全局每块卡在哪」。

逐 peer 的路由与 endpoint 建立在 [L1453-L1491](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/host/transport/device_udma/device_udma_transport_manager.cpp#L1453-L1491)：循环第一行就是 `if (peer == rank_id_) { continue; }`——**本 rank 不解析路由、不建 endpoint**。对每个真实 peer，`GetEidRoutes` 按 `qp_num`（direct 模式）或 1（relay 模式）条路由解析，每条路由调用一次 `CreateEndpoint`。注释说明 Clos 感知路由器同时给出本端出 EID 与对端入 EID，无需反向查表。

`CreateEndpoint` 展示了 HCOMM 的第一种用法：[L1506-L1569](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/host/transport/device_udma/device_udma_transport_manager.cpp#L1506-L1569) 初始化 `EndpointDesc`（协议固定 `COMM_PROTOCOL_UBC_CTP`、地址类型 `COMM_ADDR_TYPE_EID`、位置填本芯片物理坐标），`HcommEndpointCreate` 创建句柄，`HcommEndpointGetListenPort` 取监听端口（port 为 0 视为失败并回滚销毁）。

#### 4.2.4 代码实践

1. **实践目标**：在源码上确认「UDMA 的平台闸门有两道：编译期 SOC 宏 + 运行期 HCOMM 符号」。
2. **操作步骤**：
   - 阅读 [CMakeLists.txt:L240-L243](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/CMakeLists.txt#L240-L243) 与 [transport_manager.cpp:L36-L41](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/host/transport/transport_manager.cpp#L36-L41)，注意 `TT_UDMA` 分支被 `#if defined(ACLSHMEM_SOC_950)` 包裹而 `TT_SDMA` 没有；
   - 执行 `grep -n "Hcomm" src/host/utils/under_api/dl_hcomm_api.cpp | head`，列出 UDMA 用到的 HCOMM 函数族；
   - 对照 [examples/udma_demo/README.md](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/examples/udma_demo/README.md) 中「CANN 9.1.0 已提供 HcommEndpointCreate、HcommMemReg、HcommChannelCreate、HcommChannelGetStatus」的说明。
3. **需要观察的现象**：非 950 平台编译时 `UdmaTransportManager` 根本不会被工厂创建；950 平台但 CANN 过旧时，初始化在 dlopen/dlsym 检查处失败并打出含 HCOMM 字样的错误日志。
4. **预期结果**：能口头复述两道闸门各拦截哪类环境错误——SOC 宏拦「芯片不对」，HCOMM 符号拦「CANN 版本不够」。（待本地验证。）

#### 4.2.5 小练习与答案

**练习 1**：UDMA 的 `PrepareOpenDevice` 为什么需要 bootstrap allgather，而 SDMA 完全不需要？
**答案**：UDMA 建 channel 需要对端的 endpoint 描述符（EID、监听端口）和全局路由信息（谁在哪块卡、有几个 EID），这些信息分散在各 rank，必须经控制面交换；SDMA 是本地资源准备，队列由本芯片 ACL 运行时直接创建，不涉及任何跨 rank 信息。

**练习 2**：direct 多 QP 模式与 relay 模式在「每 peer 解析几条路由」上有何不同？
**答案**：见 [L1458](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/host/transport/device_udma/device_udma_transport_manager.cpp#L1458)：`route_count = ACLSHMEM_UDMA_RELAY_ENABLED ? 1U : qp_num_`。direct 模式每 QP 可走不同 CLOS endpoint（`qp_routes` 数组长为 qp_num），relay 模式每 peer 只要一条默认路由，但额外 allgather 出 N×N 矩阵供绕路查表。

### 4.3 UDMA 建链与 udmaInfo 表下发

#### 4.3.1 概念说明

endpoint 只是「网卡口」，真正承载数据的是 **channel**——UDMA 语境下的 channel 与 RDMA 的 QP 语义等价：一条 channel 对应一对发送/完成队列。channel 建好后，控制面还要把队列上下文（SQ/CQ 地址、深度、doorbell 地址、远端内存 token 等）打包成一张 `udmaInfo` 表拷到 device，AIV kernel 的低阶接口（`aclshmemx_udma_put_nbi` 等）就是直接读写这张表来发 WQE 的。

这张表的布局由 [device_udma_def.h](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/host/transport/device_udma/device_udma_def.h) 定义，头部注释（[L20-L23](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/host/transport/device_udma/device_udma_def.h#L20-L23)）强调：这些结构体必须与 device 数据面定义**逐字节一致**，控制面填充后整体 H2D 拷贝。slot 索引方式两种模式不同（[L73-L83](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/host/transport/device_udma/device_udma_def.h#L73-L83)）：direct 表 `slot == pe`，relay 表 `slot == pe * rank_count + relay_pe`。

#### 4.3.2 核心流程

`AsyncConnect` 的四步（[L1292-L1333](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/host/transport/device_udma/device_udma_transport_manager.cpp#L1292-L1333)）：

```text
AsyncConnect
 ├─ 1. ExchangeEndpointDescriptors   # bootstrap allgather 各 rank 的 endpoint 描述符（EID + 监听端口）
 ├─ 2. BuildChannels                 # direct：每 peer × 每 qp 一条 channel（slot==peer）
 │                                   # relay：每 (actual, relay) 有意义组合一条（跳过对角线）
 ├─ 3. WaitHcommChannelReady         # 轮询 HcommChannelGetStatus，10ms 间隔，120s 超时
 └─ 4. BuildUdmaInfo                 # 从 device 读回 channel 上下文 → 填 SQ/CQ/Mem 表 → H2D
```

其中第 2 步建链时还有一条 RDMA 没有的规则：**小 rank 当 server、大 rank 当 client**，双方以对称角色配对，避免同时监听冲突。

#### 4.3.3 源码精读

endpoint 描述符交换用两次 allgather 完成：[L980-L1015](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/host/transport/device_udma/device_udma_transport_manager.cpp#L980-L1015) 先交换各 rank 的 endpoint 数量，再按最大数量对齐交换描述符（未填满的槽 `valid = 0`）。

direct 模式的建链循环：[BuildDirectChannels L1236-L1262](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/host/transport/device_udma/device_udma_transport_manager.cpp#L1236-L1262)。循环开头同样 `if (peer == rank_id_) continue;`，随后要求该 peer 的 `qp_routes.size() == qp_num_`（多 QP 路由必须齐配），逐 QP 调 `CreateChannelsForSlot`，slot 就等于 peer 编号。relay 版本 [L1204-L1234](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/host/transport/device_udma/device_udma_transport_manager.cpp#L1204-L1234) 则遍历所有 `(actual_pe, relay_pe)` 组合，跳过 actual == relay 的对角线槽（「peer 给自己绕路」无物理意义）。

单个 slot 的 channel 创建：[CreateChannelsForSlot L1030-L1202](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/host/transport/device_udma/device_udma_transport_manager.cpp#L1030-L1202)。几个关键点：角色选择 `is_server = rank_id_ < dst_pe`（[L1091-L1092](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/host/transport/device_udma/device_udma_transport_manager.cpp#L1091-L1092)）；server 用本端监听端口、client 用交换来的对端端口（[L1093-L1107](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/host/transport/device_udma/device_udma_transport_manager.cpp#L1093-L1107)）；最终 `HcommChannelCreate` 以 `COMM_ENGINE_AIV` 引擎批量创建（[L1148-L1150](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/host/transport/device_udma/device_udma_transport_manager.cpp#L1148-L1150)）——`COMM_ENGINE_AIV` 正是「UDMA 由 AIV 直驱」在控制面的印证。

`HcommChannelCreate` 是非阻塞的，只发起异步连接，所以需要轮询：[WaitHcommChannelReady L75-L127](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/host/transport/device_udma/device_udma_transport_manager.cpp#L75-L127) 每 10ms 查一次 `HcommChannelGetStatus`，任何一条 channel 返回 FAILED/TIMEOUT 立即报错，全 READY 才放行；总超时 120 秒（与 u1-l4 讲过的集体操作默认超时一致）。

最后是 udmaInfo 表的组装。[L263-L275](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/host/transport/device_udma/device_udma_transport_manager.cpp#L263-L275) 的 `SetUdmaInfoSectionPtrs` 把一块连续 device 内存切成的布局为：`[info 头][WQCtx×n][WQCtx(rq)×n][CQCtx(scq)×n][CQCtx(rcq)×n][UBmemInfo×n]`，其中 `n = slot_count × qp_num`（[InitHostUdmaInfo 的注释 L645-L647](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/host/transport/device_udma/device_udma_transport_manager.cpp#L645-L647)）。`FillHostUdmaInfo`（[L680-L750](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/host/transport/device_udma/device_udma_transport_manager.cpp#L680-L750)）逐 slot 填表，`FillWqCtx`/`FillCqCtx`/`FillMemInfo` 把 HCOMM 上下文翻译成 legacy 布局（WQE 环地址、深度、SW doorbell、远端 token 等），EID 原始字节单独入表（[L733-L746](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/host/transport/device_udma/device_udma_transport_manager.cpp#L733-L746)）。

内存注册走 `HcommMemReg`：[RegisterMemoryRegion L333-L409](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/host/transport/device_udma/device_udma_transport_manager.cpp#L333-L409) 按 HBM/DRAM 标志映射 `CommMemType`，对**每一个已建 endpoint** 注册同一块堆内存，任何一步失败都回滚已注册句柄。注意 [L351-L354](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/host/transport/device_udma/device_udma_transport_manager.cpp#L351-L354)：建链之后不允许再注册——UDMA 只接受「一整块连续对称堆」的模型（`ReadChannelContexts` 里也断言注册区数恰为 1，[L576-L580](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/host/transport/device_udma/device_udma_transport_manager.cpp#L576-L580)）。

#### 4.3.4 代码实践

1. **实践目标**：算清一个具体规模下 UDMA 要建多少条 channel、udmaInfo 有多大。
2. **操作步骤**：
   - 设 `rank_count = 8`、`qp_num = 2`、direct 模式。阅读 [SlotCount L473-L484](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/host/transport/device_udma/device_udma_transport_manager.cpp#L473-L484) 与 [BuildChannels L1264-L1290](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/host/transport/device_udma/device_udma_transport_manager.cpp#L1264-L1290)，按公式 `channel_count = (rank_count - 1) × qp_num` 计算；
   - 再按 relay 模式（`channel_multiplier = peer_count`）重算一遍；
   - 用 `sizeof(aclshmemi_udma_wq_ctx_t)` 等结构体推一下 udmaInfo 的字节规模（布局公式见 [L645-L647](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/host/transport/device_udma/device_udma_transport_manager.cpp#L645-L647)）。
3. **需要观察的现象**：direct 8 rank × 2 QP = 14 条 channel/每 rank；relay 8 rank = 14 条 channel（每 (actual, relay) 非对角组合一条）。slot 数 direct 为 8、relay 为 64。
4. **预期结果**：两条数量公式与源码一致；relay 的 slot 表远大于 direct（N² vs N），这正是 relay 模式禁用多 QP 的原因之一（参见 4.2 的 `qp_num != 1` 校验）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `HcommChannelCreate` 之后还要 `WaitHcommChannelReady`，而 u5-l3 的 RDMA FixedRanksQpManager 是「小 id 当 server 阻塞 accept」？
**答案**：HCOMM 的 channel 创建是非阻塞的，只发起异步连接，状态要靠 `HcommChannelGetStatus` 轮询；注释（[L1206-L1209](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/host/transport/device_udma/device_udma_transport_manager.cpp#L1206-L1209)）还点出这样可以让所有 channel 一把建完而不必分波同步，避免老式阻塞建链的跨卡死锁。

**练习 2**：`aclshmemi_udma_qp_table_t` 里 `sq_ptr/rq_ptr/scq_ptr/rcq_ptr/mem_ptr` 五个指针指向哪里？
**答案**：都指向同一块 device 内存 blob 的不同区段（由 `SetUdmaInfoSectionPtrs` 计算，[L263-L275](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/host/transport/device_udma/device_udma_transport_manager.cpp#L263-L275)），各自是长度为 `slot_count × qp_num` 的数组首地址；填表时先指向 host 侧缓冲，拷贝前再重定基到 device 地址。

### 4.4 UDMA 能力边界：不支持自发送（本轮更新重点）

#### 4.4.1 概念说明

本轮提交 `1e7fffb fix(udma): route local RMA through MTE` 把一条隐含约定变成了显式约束：**UDMA 不支持自发送（self-send）**。也就是说，`aclshmem_putmem(dst, src, size, my_pe)` 这种「搬给自己」的调用，永远不应该走 UDMA。

为什么？从 4.2/4.3 的源码已经能看出结构性原因：

- 路由表只为真实 peer 建立（`peer == rank_id_` 直接 continue），本 rank 在 `peer_routes_` 里没有表项；
- channel 只对真实 peer 创建，本 rank 的 slot 在 udmaInfo 里**保持全零**；
- AMO scratch buffer 逐 peer 分配，唯独本 rank 那格是 `nullptr`。

硬件路径不存在，软硬两层就必须保证「永远不会有人试图走这条路径」。修复采用**双层防御**（u4-l2 从 kernel 侧看过一次，本讲从传输层/实体层补全全景）：

1. **Host 侧（通告层）**：`CanReachDataOperators` 不再对本 rank 通告 UDMA 位 → `topo_list[my_pe]` 天然没有 UDMA 位，device 侧分派自然落到 MTE；
2. **device 侧（分派层）**：`ACLSHMEM_UDMA_TRANSPORT_ENABLED` 宏追加 `(PE) != mype` 条件 → 即使 topo 位被异常置上（例如越权篡改、旧状态残留），对本 PE 的 put/get 仍强制选 MTE。

#### 4.4.2 核心流程

自发送请求在两层防御下的路径：

```text
kernel: aclshmem_putmem(dst, src, n, pe = my_pe)
        │
        ├─ Host 层已保证 topo_list[my_pe] 无 UDMA 位（CanReachDataOperators 守卫）
        │
        └─ 分派宏再兜底：
           ACLSHMEM_UDMA_TRANSPORT_ENABLED(state, pe)
             = UDMA 编译支持 ∧ pe != mype ∧ topo_list[pe] 含 UDMA 位
                                          ↑ 本 PE 恒为假 → 落到 MTE 分支
```

而 MTE 之所以能兜底，是因为 entity 层对本 rank 始终通告 MTE 位：`sdmaReach`（本 rank 必然可达）→ `HYBM_DOP_TYPE_MTE`（见下面源码）。

#### 4.4.3 源码精读

**第一层（Host 通告）**：[src/host/entity/mem_entity_default.cpp:L926-L945](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/host/entity/mem_entity_default.cpp#L926-L945) 的 `CanReachDataOperators`。本轮改动集中在 UDMA 分支：[L939-L942](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/host/entity/mem_entity_default.cpp#L939-L942) 新增 `remoteRank != options_.rankId` 守卫，注释直书动机："UDMA does not support self-send; keep the local rank reachable through MTE only."。对比上面的 SDMA 分支（L933-935）没有这个守卫——SDMA 的可达判断 `sdmaReach` 对本 rank 本来就为真且无害。另一个要点在 L929：`SDMA reaches mean MTE reaches too`，即本 rank 至少拿到 MTE 位，这就是「回落 MTE」的通告基础。

**第二层（device 分派）**：[src/device/gm2gm/shmem_device_rma.hpp:L29-L30](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/device/gm2gm/shmem_device_rma.hpp#L29-L30) 的分派宏，diff 只加了 `((PE) != (STATE)->mype) &&` 一个条件。与之并列的 SDMA 宏（[L26-L27](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/device/gm2gm/shmem_device_rma.hpp#L26-L27)）没有此守卫，因为 SDMA 不存在自发送问题。

**传输层的结构性证据**（为什么约束成立）：

- AMO scratch 只给真实 peer：[src/host/transport/device_udma/device_udma_transport_manager.cpp:L782-L795](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/host/transport/device_udma/device_udma_transport_manager.cpp#L782-L795)，`if (peer == rank_id_) { continue; }` 使 `amo_dev_list_[本rank]` 保持 `nullptr`；
- 本 rank slot 在 udmaInfo 里保持全零：[FillHostUdmaInfo 的注释 L701-L704](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/host/transport/device_udma/device_udma_transport_manager.cpp#L701-L704) 写明 "Self entry, unconnected peer, and skipped relay-diagonal slots stay zero-initialized; the data plane never issues a self-send and asserts against self pe."，且 [L713](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/host/transport/device_udma/device_udma_transport_manager.cpp#L713) 对 `dst_pe == rank_id_` 的 slot 直接判错；
- 通道上下文读取时拒绝自目标：[ReadChannelContexts L587-L593](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/host/transport/device_udma/device_udma_transport_manager.cpp#L587-L593) 把 `dst_pe == rank_id_` 列为非法输入。

**回归测试**（本轮新增，平台门控的范例写法）：Host 侧 [tests/unittest/host/mem/udma_mem/udma_mem_host_test.cpp:L443-L452](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/tests/unittest/host/mem/udma_mem/udma_mem_host_test.cpp#L443-L452) 用 `aclrtGetSocName()` 检测非 Ascend950 即 `GTEST_SKIP`；测试体 [L407-L409](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/tests/unittest/host/mem/udma_mem/udma_mem_host_test.cpp#L407-L409) 先断言初始状态合法：本 rank 的 `topo_list` 有 MTE 位、**无 UDMA 位**（验证第一层防御）。kernel 侧 [tests/unittest/device/mem/udma_mem/udma_mem_kernel.cpp:L145-L181](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/tests/unittest/device/mem/udma_mem/udma_mem_kernel.cpp#L145-L181) 则故意「破坏」第一层：[L153-L155](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/tests/unittest/device/mem/udma_mem/udma_mem_kernel.cpp#L153-L155) 强行把本 PE 的 topo 字节或上 UDMA 位并刷 cacheline，随后对 `my_pe` 依次调 `aclshmem_putmem/getmem/putmem_nbi/getmem_nbi`（[L165-L172](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/tests/unittest/device/mem/udma_mem/udma_mem_kernel.cpp#L165-L172)），收尾全部用 **`aclshmemx_mte_quiet`**——用「等的是 MTE 的 quiet」反证「走的确实是 MTE 分支」，数据校验回 Host 侧做（[L427-L435](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/tests/unittest/host/mem/udma_mem/udma_mem_host_test.cpp#L427-L435)）。

#### 4.4.4 代码实践

1. **实践目标**：亲手验证双层防御中「第二层」的兜底价值。
2. **操作步骤**：
   - 精读 [udma_mem_kernel.cpp:L145-L181](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/tests/unittest/device/mem/udma_mem/udma_mem_kernel.cpp#L145-L181)，标出「篡改 topo → 调高阶接口 → 恢复 topo」三段；
   - 思考实验：假设分派宏没有 `(PE) != mype` 守卫，这个 kernel 里被置位的 `topo_list[my_pe]` 会让 `aclshmem_putmem(..., my_pe)` 走向哪个分支？那一分支会读到 4.3 里「保持全零」的 slot 表，发生什么？
   - 有 950 环境时按 [examples/udma_demo/README.md](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/examples/udma_demo/README.md) 编译并运行该 UT：`bash scripts/build.sh -tests -soc_type Ascend950` 后运行 `TestMemApi.TestShmemUDMAHighLevelLocalRma`。
3. **需要观察的现象**：UT 通过——即使 topo 位被人为置上，4 个高阶 RMA 调用全部经 MTE 完成，`aclshmemx_mte_quiet` 足以等到数据落位。
4. **预期结果**：若防御缺失，全零 slot 的 `buf_addr/depth` 会让 UDMA 数据面写 WQE 到空指针/零深度环，表现为 kernel 挂死或非法地址错误；守卫存在时数据经 MTE 正确搬运、校验逐字节相等。（待本地验证：无 Ascend950 环境时该用例显示 SKIPPED，属预期行为。）

#### 4.4.5 小练习与答案

**练习 1**：既然 Host 层已经不通告 UDMA 位，为什么 device 侧宏还要再加一道守卫？一道不够吗？
**答案**：`topo_list` 驻留 device 全局内存，kernel 侧可写（本轮 UT 正是利用这一点做测试）。若上层业务 kernel 意外覆写了 state、或未来某路径遗漏了 entity 层守卫，单层防御就会退化为「按约定安全」。宏级守卫让分派决策在**使用点**自洽，不依赖上游状态正确性——这正是「双层防御」的意义。

**练习 2**：把本 PE 的自发送交给 MTE 后，性能上吃亏吗？
**答案**：几乎不吃亏。自发送本质是同一 PE 内的 GM 到 GM 拷贝，MTE 本就是为片上搬运设计的引擎（u4-l6 的 ub2gm 通路即 MTE）；UDMA 的优势在跨卡跨节点，用在自拷贝上反而要绕一整条网络路径。所以该约束既是硬件事实，也是合理选路。

**练习 3**：`CanReachDataOperators` 对本 rank 返回的引擎集合里有哪些位？
**答案**：本 rank `sdmaReach` 为真 → 至少含 `HYBM_DOP_TYPE_MTE`；若用户引擎掩码含 SDMA 则加 SDMA 位（SDMA 分支无自发送守卫）；若含 RDMA 则加 RDMA 位（RDMA 分支本就无 `sdmaReach` 条件）；唯独 UDMA 位被 `remoteRank != options_.rankId` 排除。即本 rank：MTE（必有）+ SDMA/RDMA（视配置），无 UDMA。

## 5. 综合实践

把本讲知识串成一张「平台-引擎-依赖条件」对照表，并（有条件时）跑通 udma_demo。

**任务 A：三引擎初始化依赖对照表**。通读本讲三个管理器（RDMA 用 u5-l3 的结论），填写并核对下表（答案已给出，请逐格回到源码验证）：

| 维度 | RDMA（TT_HCCP） | SDMA（TT_SDMA） | UDMA（TT_UDMA） |
| --- | --- | --- | --- |
| 平台约束 | 全平台；950 走 V2（HCOMM） | 仅 A3（`__NPU_ARCH__ == 2201`） | 仅 Ascend950（`ACLSHMEM_SOC_950`） |
| 外部依赖 | HCCP RA（V1）/ HCOMM endpoint（V2）、RoCE 网卡 | ACL 运行时 + STARS AICPU 算子（需匹配的 ops 包） | CANN ≥ 9.1.0 的 HCOMM 接口 + rootinfo/拓扑 XML |
| 是否跨 rank 建链 | 是（QP，socket/endpoint 模型） | 否（纯本地） | 是（HCOMM channel，小 id 当 server） |
| 内存注册 | MR + lkey/rkey 下发 | 空实现（物理可达即授权） | `HcommMemReg`，恰一块连续堆，建链后不可再注册 |
| QP/通道数 | 每 peer `rdmaQpConfig.qpNum` 条 | 无概念（每 AIV 核一条 stream） | 每 peer `udmaQpConfig.qpNum` 条（relay 模式锁 1） |
| 自发送 | 走 RDMA 本地路径 | 天然支持 | **不支持：本 rank 走 MTE，entity 层不对本 rank 通告 UDMA** |

**任务 B：运行 udma_demo**（需 Ascend950 + CANN ≥ 9.1.0）：

```bash
# 编译（仓库根目录）
bash scripts/build.sh -examples -soc_type Ascend950
# 运行 all-gather 测试（默认单机 8 卡）
bash examples/udma_demo/run.sh 0
# 或 put signal 测试
bash examples/udma_demo/run.sh 1
```

- 引擎选择的证据链：[examples/udma_demo/main.cpp:L60-L64](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/examples/udma_demo/main.cpp#L60-L64) 把 `data_op_engine_type` 设为 `ACLSHMEM_DATA_OP_UDMA`，经 [shmem_init_backend.cpp:L244-L251](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/host/init/backends/shmem_init_backend.cpp#L244-L251) 翻成 `HYBM_DOP_TYPE_DEVICE_UDMA` 位，最终由 4.2/4.3 的流程建出 channel 与 udmaInfo。
- kernel 侧每 PE 对其他 PE 调 `aclshmemx_udma_put_nbi` 后逐 PE `aclshmemx_udma_quiet(i)`（[udma_demo_kernel.cpp:L54-L56](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/examples/udma_demo/udma_demo_kernel.cpp#L54-L56)）——注意循环若包含 `i == my_pe` 会发生什么：低阶直驱接口**没有**自发送守卫（u4-l2 已说明这是调用者约定），demo 的循环写法正确地只覆盖远端 PE。
- 观察每个 PE 打印的 `check transport result success` / `[SUCCESS]`；多机时按 README 的双节点示例传参。
- 无 NPU 环境时，本任务退化为「源码阅读型实践」：把 run.sh 的参数解析与 [main.cpp:L258-L288](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/examples/udma_demo/main.cpp#L258-L288) 的位置参数对应关系写成一张表即可。

## 6. 本讲小结

- SDMA 是**本地资源准备型**引擎：`OpenDevice` 六步全是本机动作（核数 → STARS 流 → 工作区 → notify → H2D → AICPU 算子），建链/注册类虚函数全部 no-op；仅 A3（`__NPU_ARCH__ == 2201`）编译支持。
- UDMA 是**远端建链型**引擎：OpenDevice 解析 rootinfo/拓扑 XML、bootstrap allgather 交换 EID 信息、逐 peer 解析 (local_eid, remote_eid) 路由并经 HCOMM 建 endpoint；建链依赖 CANN ≥ 9.1.0 的 HCOMM 接口，仅 Ascend950 支持。
- UDMA 建链四步：交换 endpoint 描述符 → 建channel（direct: slot==peer、每 QP 一条；relay: 每 (actual, relay) 一条、跳对角）→ 轮询就绪（10ms/120s）→ 组装 udmaInfo 表（SQ/CQ/Mem 按 `slot_count × qp_num` 展开）H2D 下发。
- **UDMA 不支持自发送**是硬约束：路由、channel、AMO scratch 都只为真实 peer 建立，本 rank slot 全零。本轮以双层防御固化：entity 层 `CanReachDataOperators` 不对本 rank 通告 UDMA（本 rank 仅 MTE 可达兜底），device 层分派宏追加 `(PE) != mype` 守卫。
- 回归测试 `TestShmemUDMAHighLevelLocalRma` 展示了平台门控（`aclrtGetSocName` + `GTEST_SKIP`）与「kernel 内篡改 topo 位再用 `aclshmemx_mte_quiet` 反证走 MTE」的测试手法。

## 7. 下一步学习建议

- 下一讲 u5-l5（拓扑发现与 rootinfo）会展开本讲反复出现的 `TopoReader`/`TopoQuerier`：rootinfo 怎么生成、Clos 网络的 EID 路由如何计算，建议先做本讲 4.2 的练习再读。
- u5-l6（AICore 直驱引擎低阶接口）承接 4.3 的 udmaInfo 表：kernel 侧如何用表里的 WQCtx/CQCtx 直接发 WQE，以及低阶接口没有自发送守卫的调用者约定。
- 想深入 HCOMM 适配层的 dlopen/dlsym 机制与版本兼容策略，直接读 `src/host/utils/under_api/dl_hcomm_api.cpp`（u8-l4 会系统讲解）。
- 若你负责引擎选型，建议把第 5 节的对照表扩展上 MTE 一列（结合 u4-l6），并补充各引擎在不同消息规模下的带宽特征（u8-l8 的 perftest 实践）。
