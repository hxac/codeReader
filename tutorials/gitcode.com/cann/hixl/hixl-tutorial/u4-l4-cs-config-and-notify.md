# CS 全局配置、Notify 解析与内核加载

## 1. 本讲目标

本讲是单元四（CS 通信服务模块）的收尾篇。前三讲我们已经看清了 CS 层的控制面（消息处理器）、数据面（Channel、TransferPool、EndpointStore）。本讲下沉到 CS 层的三块「基础设施」：

1. **GlobalConfig**：server 与 client 各自如何解析同一份 JSON 配置，配置项有哪些、默认值是什么。
2. **NotifyAddrResolver**：device 上的 notify（通知信号）如何被翻译成可被远端单边读写的设备地址。
3. **HostRegisterProxy 与 load_kernel**：host 内存如何被映射为 device 地址（host register），以及 `HixlBatchGet/HixlBatchPut` 这些数据面内核是怎么从 CANN 安装目录加载进来的。

学完本讲，你应当能够：

- 说出 `comm_resource_config` 三个配置项（`listen_port`、`qos`、`max_active_channels`）的取值范围、默认值与生效位置。
- 解释为什么同一个 `GlobalConfig::Parse` 在 server 侧和 client 侧解析的字段集合不同。
- 跟踪一次 notify 地址解析的双路径分派（HCCP 路径 vs Runtime 路径）。
- 理解 host register 的引用计数语义与内核二进制的加载流程。

## 2. 前置知识

本讲默认你已完成 u4-l1 ~ u4-l3，以下概念直接使用不再展开：

- **CS 层 server/client 分工**：server 被动监听 TCP，client 主动建链（u4-l1）。
- **TransferPool 槽位池**：per-device 单例，每个槽预置 ctx/stream/notify/err_flag（u4-l3）。
- **trans_flag 与 notify**：传输完成感知靠「读/写一个标志位」实现，本质是一次单边访存。
- **register_dev_addr**：host 内存注册后得到的设备侧映射地址（u2-l3 引入）。

本讲新增的背景概念：

- **notify（`aclrtNotify`）**：CANN runtime 提供的轻量设备侧信号量。host 侧拿到的是一个不透明句柄，但数据面内核（AICPU）在 device 上执行时需要的是「设备物理/虚拟地址 + 长度」，所以存在一次「句柄 → 地址」的翻译。
- **qos（Quality of Service）**：建链时的服务质量等级，取值 0~7，会被透传给底层 Hcomm 建链接口。
- **AICPU 内核二进制**：HIXL 的批量传输（BatchGet/BatchPut）不是 host 代码，而是随 CANN 包发布的 AICPU 内核，运行时按需从 JSON 描述文件加载。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/hixl/cs/global_config.h/.cc` | CS 层全局配置的解析与访问（listen_port / qos / max_active_channels） |
| `src/hixl/common/hixl_inner_types.h` | qos 的键名与取值范围常量（kQosName/kQosMin/kQosMax/kQosDefault） |
| `src/hixl/cs/notify_addr_resolver.h/.cc` | notify 句柄到设备地址的解析（HCCP / Runtime 双路径） |
| `src/hixl/cs/host_register_proxy.h/.cc` | host 内存注册代理：per-device 实例表 + 引用计数 |
| `src/hixl/cs/load_kernel.h/.cc` | AICPU 传输内核二进制的定位与函数句柄解析 |
| `src/hixl/cs/hixl_cs_server.cc` / `hixl_cs_client.cc` | GlobalConfig 的两个消费端（ParseTarget 分流的体现） |
| `src/hixl/cs/transfer_pool.cc` | NotifyAddrResolver 与 load_kernel 的主要调用方 |
| `src/hixl/engine/hixl_server.cc` / `client_handler_config_helper.h` | 上层（engine 层）把选项拼装成 JSON 传给 CS 层的桥梁 |

## 4. 核心概念与源码讲解

### 4.1 GlobalConfig：CS 层配置体系

#### 4.1.1 概念说明

`GlobalConfig` 是 CS 层的「全局资源配置」解析器。它的输入是一个 JSON 字符串（`global_resource_config`），来源是用户在 `Hixl::Initialize` 传入的 `comm_resource_config.*` 选项；它的输出是三个 `std::optional` 字段——用 optional 而不是普通值，是为了区分「用户没配置」与「用户配置为 0」这两种语义，默认值由**消费方**决定，而不是解析器决定。

配置项只有三个（数据结构定义）：

[global_config.h:21-25](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/global_config.h#L21-L25) —— `CommResourceConfig` 用三个 optional 字段承载 listen_port、qos、max_active_channels。

| 配置键（JSON 字段名） | 类型 | 取值范围 | 默认值（消费方回填） | 生效侧 |
| --- | --- | --- | --- | --- |
| `comm_resource_config.listen_port` | uint32 | [1, 65535] | 未配置时由 `HcommProxy::EndpointGetListenPort` 向底层查询 | server |
| `comm_resource_config.qos` | uint8 | [0, 7]（kQosMin/kQosMax） | `kQosDefault = 0` | client |
| `comm_resource_config.max_active_channels` | uint32 | [1, 2^32-1] | `kDefaultTransferPoolSize = 128` | server 与 client |

#### 4.1.2 核心流程

解析流程：

```text
用户 options（Hixl::Initialize）
  └─ HixlOptions::Parse → ParseCommResourceConfig（引擎层校验，含 protocol_desc）
        └─ HixlServer / ClientHandler 把命中的字段重新 dump 成 JSON 串
              └─ HixlCSServerCreate / HixlCSClient::Create
                    └─ GlobalConfig::Parse(config_str, target)
                          ├─ target 含 kServer → ParseListenPort
                          ├─ target 含 kClient → ParseQos
                          └─ 总是 → ParseMaxActiveChannels
```

`ParseTarget` 是本模块最有设计感的部分：同一份 JSON，server 只认 `listen_port`，client 只认 `qos`，两者都认 `max_active_channels`。这避免了一端写错另一端的配置却静默生效。

#### 4.1.3 源码精读

**解析入口**：空指针或空串直接返回成功（视为「无配置」）；JSON 解析异常统一翻译为 `PARAM_INVALID`，且要求顶层必须是 object。

[global_config.cc:108-130](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/global_config.cc#L108-L130) —— `GlobalConfig::Parse` 的双层重载：单参数版本固定 `ParseTarget::kAll`，双参数版本按 target 分流。

**按 target 分流**：

[global_config.cc:82-101](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/global_config.cc#L82-L101) —— `ParseCommResourceConfig`：`kAll/kServer` 才解析 listen_port，`kAll/kClient` 才解析 qos，max_active_channels 无条件解析。

**逐项校验**（三个 Parse 函数结构完全一致：找不到键 → SUCCESS；越界 → PARAM_INVALID）：

[global_config.cc:29-45](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/global_config.cc#L29-L45) —— `ParseListenPort`：值域 [1, 65535]，越界打日志并返回 PARAM_INVALID。

[global_config.cc:47-62](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/global_config.cc#L47-L62) —— `ParseQos`：键名与范围来自公共常量。

[global_config.cc:64-80](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/global_config.cc#L64-L80) —— `ParseMaxActiveChannels`：下限 1，上限 uint32 最大值。

**qos 常量的唯一定义处**（避免引擎层与 CS 层各自维护一份范围）：

[hixl_inner_types.h:73-76](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/common/hixl_inner_types.h#L73-L76) —— `kQosName = "comm_resource_config.qos"`、`kQosDefault = 0`、`kQosMin = 0`、`kQosMax = 7`。

**server 侧调用点**（target = kServer，因此 qos 在这里被忽略）：

[hixl_cs.cc:33-36](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs.cc#L33-L36) —— `HixlCSServerCreate` 在创建 server 前解析配置，失败则整体创建失败。

**client 侧调用点**（target = kClient，listen_port 被忽略）：

[hixl_cs_client.cc:345-347](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs_client.cc#L345-L347) —— `HixlCSClient::Create` 同样在入口处解析。

**三个配置项的消费位置**：

1. listen_port——server 处理 MatchEndpoint 消息时决定数据面监听端口：

[hixl_cs_server.cc:417-431](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs_server.cc#L417-L431) —— 配置了 listen_port 就直接 `ep->SetPort()`；没配置则调 `HcommProxy::EndpointGetListenPort` 向底层查询（不支持时仅告警）。

2. qos——client 建 channel 时透传给 Hcomm 接口：

[hixl_cs_client.cc:1221-1233](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs_client.cc#L1221-L1233) —— `global_config_.Qos().value_or(kQosDefault)`：未配置时回填默认值 0。同时 server 侧在处理 CreateChannel 消息时会再校验一次 qos 范围（[hixl_cs_server.cc:453-454](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs_server.cc#L453-L454)）。

3. max_active_channels——决定 TransferPool 槽位数量：

[hixl_cs_client.cc:310](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs_client.cc#L310) 与 [hixl_cs_server.cc:177](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs_server.cc#L177) —— 两侧都用 `value_or(kDefaultTransferPoolSize)`，默认值 128 定义于 [hixl_cs_client.cc:37](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs_client.cc#L37)（server 侧同值，注释注明两端必须一致）。

**配置如何从引擎层流到 CS 层**（两个 JSON 重打包的例子）：

[hixl_server.cc:76-88](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_server.cc#L76-L88) —— server 侧：只把 listen_port / max_active_channels 两个命中的字段拼进 JSON（qos 不进 server 配置）。

[client_handler_config_helper.h:24-37](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/client_handler_config_helper.h#L24-L37) —— client 侧：`BuildGlobalResourceConfig` 拼 qos / max_active_channels；两个都未配置时显式返回空串，避免 `json.dump()` 产出 `"null"` 这种非法配置。

#### 4.1.4 代码实践

1. **实践目标**：制作一份 CS 配置调优速查表（即本讲指定的主实践任务）。
2. **操作步骤**：
   - 通读 [global_config.h](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/global_config.h) 与 [global_config.cc](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/global_config.cc)，确认三个配置键的字面拼写。
   - 用 grep 找出每个键的 `value_or` 回填点（本讲 4.1.3 已给出全部位置）。
   - 整理成如下表格并补充「调优建议」列：

     | 配置键 | 范围 | 默认 | 生效侧 | 消费点 | 调优建议 |
     | --- | --- | --- | --- | --- | --- |
     | comm_resource_config.listen_port | [1,65535] | Hcomm 查询 | server | MatchEndpoint 消息处理 | 多 server 共机时显式指定避免端口冲突 |
     | comm_resource_config.qos | [0,7] | 0 | client | CreateChannel 请求 | RDMA 网络拥塞场景按网络规划调整 |
     | comm_resource_config.max_active_channels | [1,2^32-1] | 128 | 双侧 | TransferPool::Initialize | 高并发传输可增大；注意每槽都持有 stream/notify 资源 |

3. **需要观察的现象**：速查表中每一行都能指向一个具体的源码消费点（文件+行号）。
4. **预期结果**：三个配置项、三个默认值、四个消费点全部可追溯，无凭印象填写的条目。

#### 4.1.5 小练习与答案

**练习 1**：如果 client 的配置串里误写了 `comm_resource_config.listen_port`，会发生什么？

**答案**：不会有任何效果也不报错。client 侧以 `ParseTarget::kClient` 调用解析，[global_config.cc:92-98](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/global_config.cc#L92-L98) 只在 target 含 kClient 时解析 qos，listen_port 分支被跳过；且引擎层的 `BuildGlobalResourceConfig` 根本不会把 listen_port 拼进 client 配置串。

**练习 2**：为什么 `CommResourceConfig` 用 `std::optional` 而不是直接给默认值？

**答案**：因为三个配置项的默认值语义各不相同且不在解析层决定：listen_port 未配置要走 Hcomm 查询路径（是一个运行时动作而非常量），qos 与 max_active_channels 的默认常量定义在消费方所在文件（kQosDefault 在 hixl_inner_types.h，kDefaultTransferPoolSize 在 hixl_cs_client.cc/hixl_cs_server.cc）。optional 让「未配置」这个信息原样传到消费点，由消费点用 `value_or` 或 `has_value` 分支决定行为。

**练习 3**：把 `max_active_channels` 配成 0 会怎样？

**答案**：`Initialize` 阶段直接失败。ParseMaxActiveChannels 要求值 ≥ 1（[global_config.cc:71-75](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/global_config.cc#L71-L75)），返回 PARAM_INVALID 后 `HixlCSServerCreate` / `HixlCSClient::Create` 整体失败并回滚。

### 4.2 NotifyAddrResolver：notify 设备地址解析

#### 4.2.1 概念说明

回顾 u4-l3：TransferPool 每个槽位持有一个 `aclrtNotify`，批量传输内核每处理一批 desc 就要在 notify 上「踩一脚」，host 侧据此感知完成。但 host 拿到的 notify 是 runtime 的不透明句柄，而**数据面内核运行在 device 上，它需要的是 notify 背后的设备侧地址（addr + len）**，才能在内核里直接对这个地址发信号。`NotifyAddrResolver` 就是这次「句柄 → 设备地址」翻译的唯一入口。

它是一个纯静态工具类（构造/析构均 delete），只有 `Resolve` 一个方法。

#### 4.2.2 核心流程

```text
TransferPool 初始化/重建槽位
  └─ ResolveNotifyAddressLocked(slot)
        └─ NotifyAddrResolver::Resolve(device_id, slot.notify, addr, len)
              ├─ aclrtGetSocName() 取当前芯片型号
              ├─ 型号 ∈ {A2 列表 ∪ A3 列表}？
              │     ├─ 是 → HccpProxy::RaGetNotifyAddrLen（HCCP 路径，走 proxy 层）
              │     └─ 否 → ResolveNotifyDeviceAddressByRuntime（Runtime 路径）
              │              ├─ aclrtGetNotifyId(notify) → notify_id
              │              └─ rtGetDevResAddress(res_info{procType=RT_PROCESS_HCCP,
              │                   resType=RT_RES_TYPE_STARS_NOTIFY_RECORD, resId=notify_id})
              └─ 输出 notify_addr / notify_len 写回 slot
```

#### 4.2.3 源码精读

**类定义**：删除构造析构，纯静态命名空间式的工具类。

[notify_addr_resolver.h:20-26](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/notify_addr_resolver.h#L20-L26) —— 唯一接口 `Resolve`，出参为设备地址与长度。

**Runtime 路径**：把 notify 句柄先换成数字 id，再用「设备资源表」反查地址。

[notify_addr_resolver.cc:25-40](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/notify_addr_resolver.cc#L25-L40) —— 填充 `rtDevResInfo`（procType 固定 `RT_PROCESS_HCCP`，resType 固定 `RT_RES_TYPE_STARS_NOTIFY_RECORD`，resId 填 notify_id），调 `rtGetDevResAddress` 取 addr/len。

**SoC 白名单**：A2（910B 系列 6 型号）与 A3（910_93xx/92xx 系列 6 型号）走 HCCP，其余走 Runtime。

[notify_addr_resolver.cc:42-52](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/notify_addr_resolver.cc#L42-L52) —— 两个 `std::set` 硬编码型号名单；`aclrtGetSocName` 返回空指针时视为不在名单内。

**分派主体**：

[notify_addr_resolver.cc:57-69](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/notify_addr_resolver.cc#L57-L69) —— 入参非空检查 → 取 soc_name → 白名单命中走 `HccpProxy::RaGetNotifyAddrLen`（u3-l5 讲过的 proxy 层，弱符号封装），否则走 Runtime。两条路径都打 LOGI 记录选择了哪条与 device_id/soc。

**调用方**：TransferPool 解析结果直接写进槽位字段。

[transfer_pool.cc:292-301](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/transfer_pool.cc#L292-L301) —— `ResolveNotifyAddressLocked`：清零出参 → 判空 → 调 Resolve → LOGD 打印 notify_id/addr/len。

注意：`device_id` 参数只在 HCCP 路径被使用；Runtime 路径靠当前线程的 ACL context 隐式绑定设备（这也是 TransferPool 各 Ensure*Locked 函数都要先恢复 context 的原因，参见 u4-l3）。

#### 4.2.4 代码实践

1. **实践目标**：搞清「一次传输的完成信号」在源码中完整走过了哪些形态。
2. **操作步骤**（源码阅读型实践）：
   - 从 [transfer_pool.cc:283-290](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/transfer_pool.cc#L283-L290) 的 `InitOneSlotLocked` 出发，找到 `EnsureNotifyLocked`（notify 的创建处），再跟到 `ResolveNotifyAddressLocked`。
   - 在 `transfer_pool.cc` 内搜索 `notify_addr`，观察它在下发内核时如何被使用。
3. **需要观察的现象**：notify 至少经历三个形态——`aclrtNotify` 句柄（host 创建）→ `notify_id`（数字）→ `notify_addr/notify_len`（设备地址，内核可见）。
4. **预期结果**：能画出「notify 句柄 → id → 设备地址 → 内核写信号 → host 感知完成」的形态变迁链。若手头无昇腾环境，标注「待本地验证」即可，因为整条链可纯静态读完。

#### 4.2.5 小练习与答案

**练习 1**：为什么 A2/A3 要单独走 HCCP 路径而不是统一走 Runtime？

**答案**：源码只体现了「按 SoC 型号分派」这一事实（白名单 + 两条路径）；具体动机源码未写注释，属于「待确认」的领域知识。可合理推断：这两代芯片的 notify 地址信息由 HCCP（Host Communication Control Process，即 u3-l5 的 Hcomm/HCCP 接口族）管理，Runtime 的 `rtGetDevResAddress` 在这些芯片上不可用或语义不同。下结论前应以 CANN 官方文档或 `hccp_proxy.h` 的接口注释为准。

**练习 2**：`Resolve` 开头为什么先 `notify_addr = 0U; notify_len = 0U;`？

**答案**：防御式编程：出参先清零，保证任何失败返回路径下调用方拿到的都是确定的 0 而不是栈上脏值。配合 `HIXL_CHECK_NOTNULL(notify)` 的前置检查，失败时槽位的 notify 地址字段不会残留半更新的数据。

**练习 3**：一块未列入白名单的新芯片（假设 `aclrtGetSocName` 返回其型号）会走哪条路径？

**答案**：Runtime 路径。`IsA2OrA3Soc` 两个集合都查不到该型号返回 false，于是落到 [notify_addr_resolver.cc:67-68](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/notify_addr_resolver.cc#L67-L68)。这意味着适配新芯片时如果其 notify 需要走 HCCP，需要显式改白名单——这是一个典型的「换代适配点」。

### 4.3 HostRegisterProxy：host 内存注册代理

#### 4.3.1 概念说明

u2-l3 讲过：host 内存注册后引擎会得到一个 `register_dev_addr`（设备侧映射地址），传输前把主机虚拟地址替换为它。本模块就是这个机制在 CS 层的实现。要点有三个：

1. **为什么要注册**：HCCS/UB 等链路上 device 侧发起的 DMA 不能直接用可分页的主机虚拟地址，必须先 `aclrtHostRegister(..., ACL_HOST_REGISTER_MAPPED, &dev_ptr)` 把锁页主机内存映射成 device 可见地址。
2. **为什么需要「代理」**：同一个进程里可能有多个 endpoint（甚至多个远端）注册同一块 host 内存。裸调 ACL 会重复注册/提前解注册。HostRegisterProxy 用 per-device 实例 + 引用计数（ref_cnt）把这件事管起来。
3. **为什么按 device 分实例**：映射是 device 相关的（不同 devPhyId 得到不同 dev_addr），全局实例表以 dev_phy_id 为 key。

#### 4.3.2 核心流程

```text
Register(host_addr, size) 流程：
  查 registered_mems_ 表
    ├─ 已存在且 size 相同 → ref_cnt++，返回缓存 device_addr（幂等）
    ├─ 已存在但 size 不同 → PARAM_INVALID（同地址不同长度的注册冲突）
    └─ 不存在 → aclrtHostRegister(ACL_HOST_REGISTER_MAPPED) → 记表，ref_cnt=1

Unregister(host_addr) 流程：
  查表
    ├─ 不存在 → SUCCESS（幂等，仅打日志）
    ├─ ref_cnt-- 后仍 > 0 → SUCCESS（还有别人在用，不真正解注册）
    └─ ref_cnt 归 0 → aclrtHostUnregister + 删表项
```

ref_cnt 语义保证：**任意多次注册最终只需一次解注册配对到真正的 ACL 调用**。

#### 4.3.3 源码精读

**全局实例表与获取**：

[host_register_proxy.cc:19-20](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/host_register_proxy.cc#L19-L20) —— 进程级 `map<int32_t, shared_ptr<HostRegisterProxy>>` + 全局互斥。

[host_register_proxy.cc:23-40](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/host_register_proxy.cc#L23-L40) —— `GetOrCreateInstance`：先无锁快查，miss 后加锁再查一次（双重检查），仍 miss 才 new。dev_phy_id 超过 INT32_MAX 直接拒绝。

**注册主体（含两个关键分支）**：

[host_register_proxy.cc:100-127](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/host_register_proxy.cc#L100-L127) —— 重复注册：size 一致则 ref_cnt++ 并返回缓存地址；size 不一致返回 PARAM_INVALID。首次注册：`aclrtHostRegister(host_addr, size, ACL_HOST_REGISTER_MAPPED, &dev_ptr)` 后记入 `registered_mems_`。

**解注册主体（引用计数递减）**：

[host_register_proxy.cc:129-150](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/host_register_proxy.cc#L129-L150) —— 未注册直接成功（幂等）；ref_cnt-- 后大于 0 直接返回；归零才调 `aclrtHostUnregister` 并删表。

**析构兜底**：万一用户漏了解注册，析构时统一清理，防止锁页内存泄漏。

[host_register_proxy.cc:59-70](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/host_register_proxy.cc#L59-L70) —— 遍历残留表项逐个 `aclrtHostUnregister`，失败仅打错误日志。

**上层使用点一：Endpoint::RegisterMem 的 host VA 映射分支**。只有 `NeedHostVaMapping()` 为真的 endpoint（UB 系链路）才需要这一步：

[endpoint.cc:160-172](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/endpoint.cc#L160-L172) —— host 内存先经 `HostRegisterProxy::RegisterByDev` 拿到 registered_dev_mem，再把它**伪装成 COMM_MEM_TYPE_DEVICE 内存**交给 `HcommProxy::MemReg`。回滚 guard 保证 MemReg 失败时撤销注册。

**上层使用点二：client 记账时反查映射地址**：

[hixl_cs_client.cc:404-409](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs_client.cc#L404-L409) —— `RegMemLocked` 在需要 host VA 映射时，用 `GetRegisteredDeviceAddrByDev` 反查并记入 mem_store_（这就是 u2-l3 说的「传输前把主机地址替换为 register_dev_addr」的数据来源）。注意头文件注释（[host_register_proxy.h:41-49](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/host_register_proxy.h#L41-L49)）明确：偏移后的地址与远端地址不支持反查，只支持直接注册的地址。

**上层使用点三：解注册对称清理**：

[endpoint.cc:201-205](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/endpoint.cc#L201-L205) —— `DeregisterMem` 中若该内存存在 registered_dev_mem，则配对调用 `UnregisterByDev`（ref_cnt 递减，可能并不真正解注册）。

#### 4.3.4 代码实践

1. **实践目标**：验证引用计数语义——同一块 host 内存被两个 endpoint 注册时，第一个解注册不应触发真正的 `aclrtHostUnregister`。
2. **操作步骤**（源码阅读型实践，无硬件也可完成推理部分）：
   - 通读 [host_register_proxy.cc:100-150](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/host_register_proxy.cc#L100-L150)。
   - 推演场景：endpoint A 与 endpoint B（同一 devPhyId）先后 Register 同一 `(host_addr, size)`，随后 A Unregister、B Unregister。写出每一步后 ref_cnt 的值与是否发生真实 ACL 调用。
3. **需要观察的现象**：只有 B 的 Unregister 才触发 `aclrtHostUnregister`。
4. **预期结果**：时间线为——A 注册（ref_cnt=1，真实 ACL 注册）、B 注册（ref_cnt=2，无 ACL 调用，返回同一 device_addr）、A 解注册（ref_cnt=1，无 ACL 调用）、B 解注册（ref_cnt=0，真实 ACL 解注册、删表项）。有硬件时可在 host_register_proxy.cc 的两条 LOGI（已注册复用、成功解注册）处观察日志验证；日志验证「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `GetOrCreateInstance` 里第一次 `GetInstance` 不加锁，第二次加锁？

**答案**：先走无锁快路径——绝大多数调用命中已存在的实例，避免每次都抢全局锁；miss 后才进入加锁的慢路径，且加锁后**再查一次**（双重检查锁定），防止两个线程同时通过第一次检查后重复 new。这是共享只读缓存表的经典写法。

**练习 2**：同一 host_addr 先以 size=1MB 注册，再以 size=2MB 注册，结果是什么？

**答案**：第二次返回 PARAM_INVALID。[host_register_proxy.cc:106-111](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/host_register_proxy.cc#L106-L111) 检测到缓存 size 与请求 size 不一致即拒绝，因为 device 映射是按注册时的长度建立的，长度变了映射就不再有效，静默复用会导致越界 DMA。

**练习 3**：`Endpoint::RegisterMem` 中为什么把 host 内存「伪装」成 `COMM_MEM_TYPE_DEVICE` 再交给 `HcommProxy::MemReg`？

**答案**：因为对底层 Hcomm 链路而言，数据面真正被 DMA 访问的地址是 registered_dev_mem（设备侧映射地址），底层只需要认识 device 内存。host 身份的转换（VA 映射、记账、传输前地址替换）已由 HostRegisterProxy 与 mem_store_ 在上层完成。这是「分层各管一段」的典型体现。

### 4.4 load_kernel：AICPU 传输内核加载

#### 4.4.1 概念说明

u4-l1 讲过：device 侧传输走 `HixlBatchGet/HixlBatchPut` 内核。这些内核不是编译进 libhixl 的，而是 CANN 安装包里一个独立的 AICPU 二进制（由 JSON 文件描述），进程首次用到时动态加载。`load_kernel.cc` 负责三件事：

1. **定位**：从 `ASCEND_HOME_PATH` 环境变量（缺省 `/usr/local/Ascend/cann`）拼出内核描述 JSON 的绝对路径。
2. **加载**：realpath 规范化 + access 存在性检查后，`aclrtBinaryLoadFromFile` 加载二进制，得到 `aclrtBinHandle`。
3. **取函数**：`aclrtBinaryGetFunction` 按符号名取 `aclrtFuncHandle`，供后续 `aclrtLaunchDataSink` 类接口下发。

#### 4.4.2 核心流程

```text
TransferPool::EnsureDeviceKernelsLocked（首次或 Abort 重建后）
  └─ LoadDeviceKernelAndGetHandles("HixlBatchGet", "HixlBatchPut", bin_handle, handles,
                                    "HixlSyncTransferContext")
        ├─ GetKernelFilePath：ASCEND_HOME_PATH + "/opp/built-in/op_impl/aicpu/config/libann_hixl_kernel.json"
        ├─ bin_handle 已存在？（幂等：只在 nullptr 时加载）
        │     └─ LoadBinaryFromJson：realpath → access → aclrtBinaryLoadFromFile(CPU_KERNEL_MODE)
        └─ 逐个 GetFuncHandle：aclrtBinaryGetFunction → func_handle
```

#### 4.4.3 源码精读

**路径常量**：

[load_kernel.cc:30-32](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/load_kernel.cc#L30-L32) —— CPU 内核模式常量 0、内核 JSON 后缀、默认 CANN 安装路径。

**定位**：

[load_kernel.cc:33-46](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/load_kernel.cc#L33-L46) —— 读 `ASCEND_HOME_PATH`，未设置时打告警并退回 `/usr/local/Ascend/cann`。这是「找不到内核」问题的第一排查点。

**加载（防御式三连）**：

[load_kernel.cc:48-71](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/load_kernel.cc#L48-L71) —— realpath 失败 → PARAM_INVALID（路径不存在或无权限）；access(F_OK) 失败 → FAILED；然后以 `ACL_RT_BINARY_LOAD_OPT_CPU_KERNEL_MODE` 选项调 `aclrtBinaryLoadFromFile`，成功打 LOGI 记录 handle。

**函数句柄解析**：

[load_kernel.cc:73-80](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/load_kernel.cc#L73-L80) —— `aclrtBinaryGetFunction(bin_handle, func_name, &func_handle)`。

**两个入口函数**：

[load_kernel.cc:104-124](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/load_kernel.cc#L104-L124) —— `LoadDeviceKernelAndGetHandles`：面向 CS TransferPool，固定解析 get/put 两个函数，`func_sync_context` 可选（传 nullptr 跳过），三个出参先清零保证失败路径干净。

[load_kernel.cc:84-102](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/load_kernel.cc#L84-L102) —— `LoadDeviceKernelFunctions`：面向任意函数名列表的通用版本，FabricMem 的 AICPU dispatcher 用它。

**主调用方（CS 侧）**：三个内核符号名与加载时机。

[transfer_pool.cc:37-39](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/transfer_pool.cc#L37-L39) —— `"HixlBatchGet"` / `"HixlBatchPut"` / `"HixlSyncTransferContext"` 三个符号名。

[transfer_pool.cc:663-677](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/transfer_pool.cc#L663-L677) —— `EnsureDeviceKernelsLocked`：三个句柄都已非空则直接返回（幂等）；否则先用 `TemporaryRtContext` 恢复 rts_context_（加载内核也是设备相关操作），加载后逐个判空。

**另一个复用方（FabricMem，体现通用入口的用途）**：

[fabric_mem_aicpu_dispatcher.cc:131-144](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/fabric_mem/fabric_mem_aicpu_dispatcher.cc#L131-L144) —— FabricMem 的 AICPU dispatcher 用通用入口加载 BatchRead/BatchWrite/SyncTransferContext 三个函数；加载失败不视为致命错误，而是 `aclrtBinaryUnLoad` 回收后返回 UNSUPPORTED（内核或 RTSQ 运行时未安装时优雅降级）。

#### 4.4.4 代码实践

1. **实践目标**：在不运行程序的前提下，确认当前环境的 HIXL 内核文件是否存在、路径是否正确。
2. **操作步骤**：
   - 执行 `echo $ASCEND_HOME_PATH`；为空则按默认路径 `/usr/local/Ascend/cann` 继续。
   - 检查 `<该路径>/opp/built-in/op_impl/aicpu/config/libcann_hixl_kernel.json` 是否存在（`ls -l`）。
   - 对照 [load_kernel.cc:33-46](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/load_kernel.cc#L33-L46) 理解：若文件缺失，运行时会在 realpath/access 检查处失败，错误日志中会带完整路径。
3. **需要观察的现象**：文件存在则路径链路无问题；不存在则说明 CANN 包版本不含 HIXL 内核（或安装不完整），任何 device 侧传输都会在初始化 TransferPool 时失败。
4. **预期结果**：能写出一条排查命令：`ls ${ASCEND_HOME_PATH:-/usr/local/Ascend/cann}/opp/built-in/op_impl/aicpu/config/libcann_hixl_kernel.json`。本讲义生成环境无昇腾机器，具体结果「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：`LoadDeviceKernelAndGetHandles` 为什么把 `bin_handle` 设计成「入参引用 + 空判跳过加载」？

**答案**：为了幂等与复用。调用方（TransferPool）把 `kernel_bin_handle_` 作为成员反复传入；首次为 nullptr 时触发加载并回写，之后（包括 Abort 重建槽位后）直接复用已加载的二进制，避免重复 `aclrtBinaryLoadFromFile`。FabricMem dispatcher 同样把 `binary_handle_` 成员传入复用。

**练习 2**：内核加载失败与 FabricMem 的 AICPU 降级失败，错误处理策略有何不同？为什么？

**答案**：CS 侧 `EnsureDeviceKernelsLocked` 失败直接向上返回错误（HIXL_CHK_STATUS_RET），因为 CS 路径没有替代实现，device 传输完全依赖这三个内核；FabricMem 侧加载失败返回 UNSUPPORTED 并回收句柄（[fabric_mem_aicpu_dispatcher.cc:134-143](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/fabric_mem/fabric_mem_aicpu_dispatcher.cc#L134-L143)），因为 AICPU 只是 FabricMem 的可选加速路径（还有 host 路径，见 u5-l3），缺内核时可以降级。同一个加载函数，两种失败策略，差异来自上层是否有 fallback。

**练习 3**：为什么加载前要先 `realpath` 再 `access`，而不是直接调 `aclrtBinaryLoadFromFile`？

**答案**：为了在 ACL 之前就拿到确定、可读的错误信息：realpath 失败能区分「路径不存在/符号链接断裂」并报 PARAM_INVALID，access 失败报 FAILED，两者都会把 errno 和 strerror 打进日志；直接交给 ACL 只会得到一个笼统的错误码。同时 realpath 把相对路径、软链接规范化成绝对路径，避免后续按路径比对时出现同文件异名。

## 5. 综合实践

**任务：写一份《CS 层初始化依赖清单》**。把本讲四个模块串成一次 `HixlCSClient::Create` / `HixlCSServerCreate` 的初始化全景图：

1. 画出从 `Hixl::Initialize` 的 options 出发，配置 JSON 经过 `HixlOptions::Parse`（引擎层，[hixl_options.cc:173-189](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_options.cc#L173-L189)）→ `HixlServer`/`ClientHandlerConfigHelper` 重打包 → `GlobalConfig::Parse`（CS 层）→ 三个消费点的完整数据流图，标注每一跳的字段集合变化（哪些字段被丢弃、哪些被回填默认值）。
2. 在图中补上三个「隐性初始化依赖」：
   - TransferPool 初始化依赖 `MaxActiveChannels` 决定槽数；
   - 每个槽的 notify 依赖 `NotifyAddrResolver::Resolve` 拿到设备地址；
   - device 传输依赖 `EnsureDeviceKernelsLocked` 加载的三个 AICPU 内核。
3. 为 host 内存场景补一条支线：`Endpoint::RegisterMem` → `HostRegisterProxy::RegisterByDev` → `mem_store_` 记录 register_dev_addr。
4. 最后在图上标注三个「故障排查点」：ASCEND_HOME_PATH/内核 JSON 缺失（load_kernel）、listen_port 冲突（GlobalConfig）、host 内存重复注册 size 不一致（HostRegisterProxy）。

预期产出：一张静态可画（纸/mermaid 均可）的依赖图 + 一页排查点说明。所有节点都应能指向本讲义引用过的具体源码行号；运行验证部分若无昇腾环境，标注「待本地验证」。

## 6. 本讲小结

- **GlobalConfig** 只解析三个键（listen_port [1,65535] / qos [0,7] / max_active_channels ≥1），用 `ParseTarget` 实现 server 只认 listen_port、client 只认 qos 的字段隔离；默认值不在解析层，由消费方 `value_or` 回填（qos→0，max_active_channels→128，listen_port→Hcomm 查询）。
- **NotifyAddrResolver** 把 notify 句柄翻译成设备地址，按 SoC 白名单（A2/A3 共 12 个型号）在 HCCP 路径与 Runtime 路径（`aclrtGetNotifyId` + `rtGetDevResAddress`）之间二选一，是换芯片代际时的显式适配点。
- **HostRegisterProxy** 用 per-device 实例表 + ref_cnt 引用计数管理 `aclrtHostRegister(ACL_HOST_REGISTER_MAPPED)`，保证同一 host 内存多 endpoint 共享时注册/解注册正确配对，析构兜底清理漏解注册的内存。
- **load_kernel** 从 `ASCEND_HOME_PATH/opp/built-in/op_impl/aicpu/config/libcann_hixl_kernel.json` 加载 AICPU 二进制并解析 `HixlBatchGet/HixlBatchPut/HixlSyncTransferContext` 三个函数句柄；bin_handle 复用实现幂等，FabricMem 侧复用同一机制但失败时优雅降级为 UNSUPPORTED。
- 三个模块共同构成 CS 层「可配置、可感知完成、可被 device 直接访存」的地基：配置决定资源规模，notify 提供完成信号，host register 与内核加载打通数据面的最后一段。

## 7. 下一步学习建议

- 单元四至此完结。建议先回头把 u4-l1 画的 CS 交互图按本讲内容增补（listen_port 从哪来、notify 地址何时解析、内核何时加载），形成一张完整的 CS 层终版架构图。
- 下一单元（u5）进入 FabricMem 模式：本讲的 `LoadDeviceKernelFunctions` 通用入口在 [fabric_mem_aicpu_dispatcher.cc](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/fabric_mem/fabric_mem_aicpu_dispatcher.cc) 中的降级用法会再次出现，建议从 u5-l1（FabricMem 概念与设计）开始。
- 若你更关心上层 KV Cache 传输，可直接跳到 u6（LLM-DataDist），其中 `HixlTransferEngine` 适配层会消费本讲所属的整个 HIXL Engine 栈。
- 延伸阅读源码：`src/hixl/proxy/hccp_proxy.h`（notify HCCP 路径的底层封装）与 `src/hixl/engine/hixl_options.cc` 的 `ParseGlobalResourceConfig`（引擎层与 CS 层两段解析的差异对照）。
