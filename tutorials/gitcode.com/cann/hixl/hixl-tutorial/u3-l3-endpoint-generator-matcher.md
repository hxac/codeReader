# u3-l3 Endpoint 生成与匹配

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚「endpoint（端点）」在 HIXL 中是什么：它如何用 `protocol + comm_id + placement` 等字段描述一条可用的通信链路资源。
2. 理解 `EndpointGenerator` 如何得到本端端点列表：优先解析用户显式传入的 `local_comm_res` JSON，否则按芯片代际（A5/V2/V3）自动生成。
3. 理解 `local_comm_res_generator_v1` 与 `rootinfo_builder_generator_v1` 两个子生成器如何从 topo 文件、`urma_admin` 输出和 DCMI/DSMI 接口中推导出 D2D/H2D/D2H 等通信「边」。
4. 掌握 `EndpointMatcher` 的静态优先级规则表：同实例走 UB 分组、跨实例走 DIRECT 单协议，匹配失败返回哪个错误码。
5. 能完整描述「从远端 engine 字符串（如 `192.168.1.10:26000`）到一条可用传输链路」的全过程。

## 2. 前置知识

本讲假设你已学完 u3-l1（Engine 抽象体系）和 u2-l4（建链流程）。再补充几个本讲要用到的概念：

- **endpoint（端点）**：一条「可被通信库使用的链路资源」的描述。例如「roce 协议 + 设备侧 IP 192.168.10.1」或「ub_ctp 协议 + 一个 32 字节的 EID」。一台机器通常同时拥有多条不同协议的端点（RoCE 网卡、HCCS 片间总线、UB 总线等）。
- **协议（protocol）**：HIXL 内部识别五种链路协议字符串：`roce`、`hccs`、`ub_ctp`、`uboe`、`ub_rtp`（见 [hixl_inner_types.h:59-63](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/common/hixl_inner_types.h#L59-L63)）。其中 `ub_ctp` 用于超节点内 UB（Unified Bus）多路径分组传输，`uboe`/`ub_rtp` 是 ScaleOut（跨主机扩展）方向的 UB 变体。
- **placement（放置位置）**：端点落在 `device`（NPU 上）还是 `host`（CPU 上），见 [hixl_inner_types.h:66-67](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/common/hixl_inner_types.h#L66-L67)。它决定了传输的内存路径（D2D/H2D/D2H/H2H）。
- **comm_id（通信标识）**：不同协议下含义不同——RoCE/UBoE 下是一个 IP 地址字符串；HCCS 下是设备号十进制字符串；ub_ctp/ub_rtp 下是一个 32 字节 EID 的十六进制字符串（64 个 hex 字符，无冒号）。
- **EID（Endpoint Identifier）**：UB 体系里的「总线地址身份证」，16 字节（`COMM_ADDR_EID_LEN`），引擎通过解析 EID 中的特定字节判断它属于哪个 die、哪个端口、是否是 PG（Port Group，端口聚合）EID。
- **net_instance_id（网络实例标识）**：一台「超节点/主机」级别的身份字符串。两端 net_instance_id 不同即为「跨实例」（cross-instance），相同即「同实例」（same-instance），这直接决定匹配走哪张规则表。
- **topo 文件 / route 数据**：驱动目录下的 JSON 拓扑文件（描述 NPU 之间端口怎么连线）和路由信息（描述 host 侧到 device 侧 EID 怎么配对），是自动生成端点的原始素材。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/hixl/common/hixl_inner_types.h` | 定义 `EndpointConfig` 结构与五种协议、两种 placement 的字符串常量 |
| `src/hixl/engine/endpoint_generator/endpoint_generator.cc/.h` | 端点列表生成的总入口：显式 `local_comm_res` 解析、按 SoC 类型自动生成、端点的序列化/反序列化 |
| `src/hixl/engine/endpoint_generator/local_comm_res_generator_v1.cc` | LocalCommRes 生成工具：解析 topo 文件、执行 `urma_admin`、生成 D2D/H2D/D2H/D2U/H2U 各类「边」 |
| `src/hixl/engine/endpoint_generator/rootinfo_builder_generator_v1.cc` | RootInfo 构建器：从 DCMI 拿 EID 列表，解析 EID 字节得到端口表和 CLOS PG EID |
| `src/hixl/engine/endpoint_matcher.cc/.h` | 端点匹配器：用静态优先级规则表在本地/远端端点列表间选路，决定 ClientHandler 类型 |
| `src/hixl/engine/hixl_engine.cc` | 引擎初始化处调用 `BuildEndpointList` 生成 `endpoint_list_` |
| `src/hixl/engine/hixl_client.cc` | client 侧通过控制面 socket 拉取远端端点列表，再调用 `EndpointMatcher::MatchEndpoints` |

一条数据在本讲中的流向：

```
server 侧:  HixlEngine::Initialize ──> EndpointGenerator::BuildEndpointList ──> endpoint_list_
                                                                        │
client 侧:  HixlClient::Initialize ──(控制面 socket 拉取远端 endpoint_list)
                    │
                    └──> EndpointMatcher::MatchEndpoints(local, remote) ──> matched_pairs + handler_type
                                                                    │
                                                                    └──> ClientHandlerFactory::Create
```

## 4. 核心概念与源码讲解

### 4.1 EndpointConfig：通信资源标识的数据模型

#### 4.1.1 概念说明

要在一台拥有多条链路（RoCE 网卡、HCCS、UB）的机器和另一台同样复杂的机器之间传输数据，第一步是把「我有哪些链路、每条链路的地址是什么」用一个统一的结构描述出来。这个结构就是 `EndpointConfig`。它既是生成器的产物，也是匹配器的输入，还是控制面消息的序列化单元——贯穿本讲所有模块。

#### 4.1.2 核心流程

一个 `EndpointConfig` 的关键字段与含义：

| 字段 | 含义 | 典型值 |
| --- | --- | --- |
| `protocol` | 链路协议 | `"roce"` / `"hccs"` / `"ub_ctp"` / `"uboe"` / `"ub_rtp"` |
| `comm_id` | 通信地址标识 | IP 字符串 / 设备号字符串 / 64 位 hex EID |
| `placement` | 端点在 device 还是 host | `"device"` / `"host"` |
| `plane` | UB 分组所在的平面名 | `"plane_pg_0"` / `"plane_pg_1"` |
| `dst_eid` | （UB 类端点）对端 EID，用于精确配对 | hex EID 字符串 |
| `net_instance_id` | 所属网络实例（超节点/主机身份） | `"superpod_3"` / 主机 IP |
| `server_id` | （host 端点）所属 server 进程标识，用于环回匹配 | 进程标识字符串 |
| `device_info` | 物理设备定位（phy_device_id 等） | 整数 |

#### 4.1.3 源码精读

协议与 placement 的字符串常量统一定义在内部类型头文件中，生成器和匹配器共用同一套词表：

- [hixl_inner_types.h:59-67](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/common/hixl_inner_types.h#L59-L67)：定义 `kProtocolRoce/kProtocolUbCtp/kProtocolHccs/kProtocolUboe/kProtocolUbRtp` 与 `kPlacementDevice/kPlacementHost` 五协议两位置常量。这保证了「生成器写出的字符串」和「匹配器查询的字符串」永远一致。

`EndpointConfig` 结构本体：

- [hixl_inner_types.h:100-119](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/common/hixl_inner_types.h#L100-L119)：`EndpointConfig` 结构，含上表所列的 `plane`、`dst_eid`、`net_instance_id`、`server_id` 等字段及 `ToString()` 调试输出。注意这是 **src 内部头文件**，不对用户公开——用户感知到的只是 `Initialize` 选项里的 `local_comm_res` JSON 文本。

#### 4.1.4 代码实践

1. **实践目标**：建立「字符串词表 → 数据结构」的直觉。
2. **操作步骤**：打开 [hixl_inner_types.h:59-67](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/common/hixl_inner_types.h#L59-L67)，然后对照 u1-l5 讲过的样例参数 `--protocol hccs:device`——你会发现冒号前后两段正是 `kProtocolHccs` 与 `kPlacementDevice` 两个常量的值。
3. **需要观察的现象**：protocol_desc 的 `protocol:placement` 记法在本模块、4.2 的过滤器、4.4 的匹配规则表中反复出现。
4. **预期结果**：能不查资料说出 `ub_rtp:device` 中两段各自的常量名。
5. 本实践为纯阅读型，无需运行（无硬件环境下结论同样成立）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `EndpointConfig` 定义在 `src/hixl/common/` 而不是 `include/hixl/` 下的公开头文件里？

**答案**：它是引擎内部的数据模型，用户不需要（也不应该）直接构造它；用户侧唯一接触的是 `local_comm_res` JSON 字符串选项。放在内部头文件中可以避免内部字段变化破坏公开 ABI（可对照 u1-l4 讲过的「公开头文件边界」结论）。

**练习 2**：`comm_id` 在三种协议下分别是什么形式？

**答案**：RoCE/UBoE 下是 IP 地址字符串；HCCS 下是物理设备号的十进制字符串（长度 ≤ 10 的纯数字，见 `ParseHccsCommId`）；ub_ctp/ub_rtp 下是 32 字节 EID 的 64 个连续十六进制字符（无冒号）。

### 4.2 EndpointGenerator：本地端点列表的生成

#### 4.2.1 概念说明

`EndpointGenerator` 是一个纯静态工具类，回答一个问题：「**本机**有哪些可用的通信端点？」它有三条信息来源，按优先级排列：

1. **显式配置**：用户在 `Initialize` 选项里传了 `local_comm_res` JSON（含 `net_instance_id` 与 `endpoint_list`），直接解析使用。
2. **自动生成**：没传（或传了但没有有效 endpoint_list）时，按本机 SoC 类型自动探测——A5（kV5）走 ScaleOut + UB 生成，V2/V3 走 RoCE + HCCS 默认生成。
3. **约束过滤**：若用户给了 `protocol_desc`（如 `hccs:device`），对生成的列表做过滤，只保留指定协议与位置的端点。

一个重要约束：通用服务器（无数卡设备）**不支持**自动生成，必须显式传 `local_comm_res`。

#### 4.2.2 核心流程

`BuildEndpointList` 的决策流程（伪代码）：

```
BuildEndpointList(options, local_engine):
    endpoint_list = 解析 options.local_comm_res 中的 endpoint_list   # Step 1
    if endpoint_list 非空:
        if 环境变量 HCCL_INTRA_ROCE_ENABLE=1: 只保留 roce 端点
        补充本机 device_info（phy_device_id 等）
        return
    if 本机 NPU device 数 == 0:
        return PARAM_INVALID   # 无卡环境不支持自动生成
    endpoint_list = AutoGenEndpointList(options, local_engine)       # Step 2
        ├── SoC == kV5:  AutoGenA5EndpointList（ScaleOut 端点 + ub_ctp 端点）
        ├── SoC == kV2/kV3: GenerateInfo（roce 端点 + hccs 端点）
        └── 按 protocol_desc 过滤
    if endpoint_list 为空: return PARAM_INVALID
    补充本机 device_info
```

A5 路径的 `AutoGenA5Core` 又分三步：

```
AutoGenA5Core:
    1. GenAutoScaleOutEndpoints   # protocol_desc 空则按 DSMI InterconType 自动选 ub_rtp/uboe；非空则按显式模式生成
    2. AppendUbCtpEndpoints       # 需要时调用 GenerateLocalCommRes（4.3 讲）生成 ub_ctp 边并合并
    3. FilterEndpointsByProtocolDescList + 回填 net_instance_id
```

#### 4.2.3 源码精读

总入口由引擎初始化调用：

- [hixl_engine.cc:68-70](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_engine.cc#L68-L70)：`HixlEngine::Initialize` 中调用 `EndpointGenerator::BuildEndpointList(options, local_engine_, local_comm_res, endpoint_list_)`，把生成的 `endpoint_list_` 交给后续的 `HixlServer` 初始化。也就是说 **server 侧在引擎 Initialize 时就把端点列表准备好了**，等 client 来索取。

`BuildEndpointList` 本体：

- [endpoint_generator.cc:632-662](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/endpoint_generator/endpoint_generator.cc#L632-L662)：Step 1 尝试从 `local_comm_res` 解析（637 行），非空则处理 `HCCL_INTRA_ROCE_ENABLE` 过滤（640-645 行）后返回；否则检查 device 数量（650-654 行，无卡报 `PARAM_INVALID`），再走 Step 2 自动生成（657 行），最终保证列表非空（658-659 行）。

显式 `local_comm_res` 的解析：

- [endpoint_generator.cc:569-597](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/endpoint_generator/endpoint_generator.cc#L569-L597)：`ParseEndpointListFromLocalCommRes` 把选项字符串按 JSON 解析，要求同时具备字符串型 `net_instance_id` 与非空数组 `endpoint_list` 才算有效配置，否则视为「未显式配置」走自动生成。
- [endpoint_generator.cc:850-885](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/endpoint_generator/endpoint_generator.cc#L850-L885)：`ParseLocalCommRes` 逐项校验每个 endpoint 的 `protocol`/`comm_id`/`placement`，并用 `IsSupportedProtocolDesc` 拒绝不支持的组合（865-867 行）；880-881 行有一条硬约束——`endpoint_list` 不允许同时含 `ub_rtp` 和 `uboe` 两种协议。

按 SoC 类型分派的自动生成：

- [endpoint_generator.cc:685-712](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/endpoint_generator/endpoint_generator.cc#L685-L712)：`AutoGenEndpointList` 先 `GetSocType`，kV5 走 `AutoGenA5EndpointList`，kV2/kV3 走 `GenerateInfo`（RoCE + HCCS 双端点），最后统一按 `protocol_desc` 过滤。
- [endpoint_generator.cc:929-949](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/endpoint_generator/endpoint_generator.cc#L929-L949)：`BuildDefaultDeviceEndpointInfoList`——V2/V3 的默认端点就是两条：RoCE 端点（comm_id 为设备 IP，来自 hccn.conf/hccn_tool）+ HCCS 端点（comm_id 为 phy_device_id 的十进制字符串，962-967 行的 `BuildHccsEndpoint`）。这正对应 u1-l5 样例里 `--protocol` 可选的两类值。

端点列表跨进程传输的序列化/反序列化：

- [endpoint_generator.cc:740-767](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/endpoint_generator/endpoint_generator.cc#L740-L767)：`SerializeEndpointConfigList` 把端点列表转成 JSON 数组字符串（server 侧应答 client 时用）。
- [endpoint_generator.cc:769-798](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/endpoint_generator/endpoint_generator.cc#L769-L798)：`DeserializeEndpointConfigList` 的逆过程（client 侧收到后用），逐字段解析并兼容可选的 `server_id` 字段。

#### 4.2.4 代码实践

1. **实践目标**：验证「显式配置优先于自动生成」。
2. **操作步骤**：
   - 阅读 [endpoint_generator.cc:632-662](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/endpoint_generator/endpoint_generator.cc#L632-L662)，注意 639 行的 `if (!endpoint_list.empty())` 提前返回。
   - 在 u1-l3 的 quickstart 样例启动命令基础上，为两端分别增加选项 `local_comm_res`，内容为一个只含 roce 端点的 JSON（格式参照 `ParseLocalCommRes` 要求的字段：`net_instance_id` + `endpoint_list[].{protocol,comm_id,placement}`）。
3. **需要观察的现象**：引擎日志中出现 `[EndpointGenerator] parsed or generated endpoint list` 与 `endpoint list after protocol_desc filter` 两条事件（来自 [endpoint_generator.cc:618-630](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/endpoint_generator/endpoint_generator.cc#L618-L630) 的 `FilterEndpointListByProtocolDesc` 日志）。
4. **预期结果**：显式 JSON 里的端点原样出现在日志中，自动生成路径完全未执行。**待本地验证**（需要真实双卡环境；无硬件时可只做源码推演）。

#### 4.2.5 小练习与答案

**练习 1**：为什么无数卡的通用服务器必须显式传 `local_comm_res`？

**答案**：自动生成的所有路径（A5 的 DSMI/DCMI 探测、V2/V3 的设备 IP/HCCS 查询）都依赖本机存在 NPU 设备；[endpoint_generator.cc:651-654](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/endpoint_generator/endpoint_generator.cc#L651-L654) 在 `device_count == 0` 时直接返回 `PARAM_INVALID`，并提示自动生成不支持。

**练习 2**：`protocol_desc` 同时写 `ub_rtp:device` 和 `uboe:device` 会怎样？

**答案**：返回 `PARAM_INVALID`。见 [endpoint_generator.cc:599-616](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/endpoint_generator/endpoint_generator.cc#L599-L616) 中 `ParseProtocolDescMode` 判定出的 `kConflict` 分支（606-608 行），这两种 ScaleOut 协议互斥。

### 4.3 LocalCommRes 生成器与 RootInfo 构建器

#### 4.3.1 概念说明

A5（kV5）芯片上，ub_ctp 端点不能像 RoCE 那样「查个 IP 就完事」——超节点内部是复杂的 UB 网络：多个 NPU 通过 mesh 端口直连，host 与 device 之间通过配对的 EID 通信，还有 PG（端口组）EID 提供聚合带宽。`local_comm_res_generator_v1` 的职责就是把这些硬件信息变成一张「边表」：每条边是一个 `EndpointConfig`，标注本端 EID（comm_id）、对端 EID（dst_eid）、位置（placement）与平面（plane）。`rootinfo_builder_generator_v1` 则是它的底层侦察兵：从 DCMI 拿到每个 NPU 的 EID 列表，并解码 EID 字节得到「端口 → EID」映射和 CLOS PG EID。

这套能力同时被打包成对外的 `TransLocalCommRes` 接口（生成 JSON 字符串），也就是 u1-l5 提过的 `localcommres` 工具背后的核心。

#### 4.3.2 核心流程

`GenerateLocalCommRes` 主流程：

```
GenerateLocalCommRes(phy_dev_id, topo_path, mode):
    1. GetMainboardId ──> 判断产品形态（PoD 还是 Server），决定 mesh die 位置
    2. ParseTopoAndRouteFiles:
        a. ParseTopoFile: 解析 topo JSON 的 edge_list（NPU 间连线）
        b. GenerateRouteDataViaDsmi: 执行 urma_admin show，建立
           "UB 设备名 → PG EID" 与 "cpu+die → 8 端口 PG EID" 两张映射，
           再为组内每个 NPU 生成 RouteEntry(local_eid, remote_eid)
    3. BuildLocalCommResResult:
        a. BuildNpuRootinfos: 对组内每个 NPU 调 BuildNpuRootInfo（rootinfo_builder）
        b. CollectClosPgEids: 提取 plane_pg_0 / plane_pg_1 两个 PG EID
        c. CollectAllEdges: 生成各类边
             D2D（mesh 端口对端口）+ D2U（device→PG 平面）
             mode=kDeviceAndHost 时再加 H2U（host 8 端口 PG）+ H2D + D2H（路由配对）
        d. net_instance_id = "superpod_<super_pod_id>"，写回每条边
```

EID 解码规则（rootinfo_builder 的核心小算法）：EID 字符串第 6 字节（hex 第 10~11 位）的高 4 位若为 `0x3` 或 `0x7` 则是 PG EID；其 `0x4` 位决定 die_id（0 或 1）；低 4 位是端口号。

#### 4.3.3 源码精读

- [rootinfo_builder_generator_v1.cc:29-57](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/endpoint_generator/rootinfo_builder_generator_v1.cc#L29-L57)：`ParseEidByte6`——按上述规则从 EID 字符串解出 `die_id`、`is_pg_eid`、`port`。这是整个 UB 端点生成的基础解码函数。
- [rootinfo_builder_generator_v1.cc:137-150](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/endpoint_generator/rootinfo_builder_generator_v1.cc#L137-L150)：`GetMeshDieId`——Server 形态 mesh 固定在 die 1；PoD 形态按 `npu_id % 8` 前 4 个在 die 0、后 4 个在 die 1。产品形态影响硬件拓扑认知，这里是「异构集群适配」的一个具体体现。
- [rootinfo_builder_generator_v1.cc:168-189](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/endpoint_generator/rootinfo_builder_generator_v1.cc#L168-L189)：`CollectMeshPorts`——跳过 PG EID，把 mesh die 上端口 0~8 的 EID 记入 `port_to_eid` 映射，键形如 `"0/3"`（die/端口）。
- [rootinfo_builder_generator_v1.cc:248-283](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/endpoint_generator/rootinfo_builder_generator_v1.cc#L248-L283)：`BuildNpuRootInfo` 总装——拿设备列表、确定 mesh die、收集端口表与 CLOS PG，最后 278-280 行校验两类信息都非空才算完整。

生成器侧：

- [local_comm_res_generator_v1.cc:1435-1473](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/endpoint_generator/local_comm_res_generator_v1.cc#L1435-L1473)：`GenerateLocalCommRes` 四个重载的最终实现——取 mainboard_id 判形态、解析 topo 与路由、组装结果，对应上面的伪代码。
- [local_comm_res_generator_v1.cc:956-978](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/endpoint_generator/local_comm_res_generator_v1.cc#L956-L978)：`ParseTopoFile` 解析驱动目录 `/usr/local/Ascend/driver/topo/950/` 下的 topo JSON（文件名由 mainboard_id 决定，见 481-515 行的 `TopoFileFinder`）。
- [local_comm_res_generator_v1.cc:1050-1109](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/endpoint_generator/local_comm_res_generator_v1.cc#L1050-L1109)：`GenerateD2DEdges`——只保留 `net_layer==0 && link_type==PEER2PEER && topo_type==1DMESH` 的链路（1002-1016 行的 `ShouldSkipD2DLink`），对每条链路把本端端口 EID 与对端端口 EID 配成一条边（`comm_id`=本端，`dst_eid`=对端）。
- [local_comm_res_generator_v1.cc:1111-1152](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/endpoint_generator/local_comm_res_generator_v1.cc#L1111-L1152)：`GenerateH2DEdges`（host→device 边，placement=host）与 `GenerateD2HEdges`（device→host 边，按 `entry.device_id == phy_dev_id` 只留自己的），二者把路由表里的 local/remote EID 按方向填进 `comm_id`/`dst_eid`。
- [local_comm_res_generator_v1.cc:1218-1260](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/endpoint_generator/local_comm_res_generator_v1.cc#L1218-L1260)：`GenerateD2UEdges`/`GenerateH2UEdges`——用 `plane_pg_0`/`plane_pg_1` 两个 CLOS PG EID 生成设备/主机到 PG 平面的边，这是 UBClientHandler 多平面传输（u3-l2）的端点来源。
- [local_comm_res_generator_v1.cc:1482-1507](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/endpoint_generator/local_comm_res_generator_v1.cc#L1482-L1507)：`SerializeLocalCommResJson`——把结果序列化为 2 空格缩进 JSON，也就是用户在 `local_comm_res` 选项里看到的格式，实现「自动生成」与「显式配置」两种入口的同构。

#### 4.3.4 代码实践

1. **实践目标**：读懂一条 D2D 边的产生条件。
2. **操作步骤**：
   - 精读 [local_comm_res_generator_v1.cc:1002-1016](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/endpoint_generator/local_comm_res_generator_v1.cc#L1002-L1016) 的 `ShouldSkipD2DLink`，记录三个跳过条件。
   - 再看 [local_comm_res_generator_v1.cc:1069-1083](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/endpoint_generator/local_comm_res_generator_v1.cc#L1069-L1083)——链路的 `local_a`/`local_b` 必须有一端等于自己的 `phy_id`，否则整条链路与我无关。
3. **需要观察的现象**：日志中 `D2D result: matched=N, skip(net_layer)=..., skip(link_type)=..., ...` 这一行（1102-1107 行）把所有跳过原因分类计数。
4. **预期结果**：能回答「一条 net_layer=1 的链路为什么不会生成 D2D 边」。**待本地验证**（需 A5 真实环境；源码推演结论确定）。

#### 4.3.5 小练习与答案

**练习 1**：`BuildNpuRootInfo` 为一个 NPU 构建的两类核心信息是什么？

**答案**：`port_to_eid`（mesh die 上「die/端口 → EID」的映射，供 D2D 配对用）和 `clos_pg_eids`（CLOS 端口组的 PG EID 列表，供 plane_pg_0/plane_pg_1 平面边使用）。

**练习 2**：为什么 `GenerateD2HEdges` 要按 `entry.device_id != phy_dev_id` 过滤，而 `GenerateH2DEdges` 不用？

**答案**：路由表中每条 entry 的 `local_eid` 是 host 侧 EID，对所有 NPU 通用（host 发往任意 device 都可复用），所以 H2D 全量生成；而 D2H 方向的「本端 comm_id」必须是**自己**设备的 remote_eid，别的 NPU 的边对自己无效，因此只保留 `device_id == phy_dev_id` 的条目。

### 4.4 EndpointMatcher：优先级规则与端点匹配

#### 4.4.1 概念说明

现在两端各自都有了端点列表，但「用哪一对端点建链」仍需决策：本机的 ub_ctp 端点应该配远端哪个 EID？同实例和跨实例的策略一样吗？`EndpointMatcher` 用**静态优先级规则表**回答这个问题——按顺序逐条尝试，第一条成功的规则决定 `matched_pairs`（配好的端点对 + CommType）和 `handler_type`（DIRECT 还是 UB，即 u3-l2 讲的两种 ClientHandler）。它是一个纯静态类（构造函数 `= delete`），无状态、可独立测试。

#### 4.4.2 核心流程

匹配的总流程：

```
MatchEndpoints(local, remote):
    cross = (local[0].net_instance_id != remote[0].net_instance_id)
    ──> TryMatchByPriority:
        特例: 同实例 且 两端 ub_ctp 端点全部在 device
              ──> 试 kSameInstanceUbCtpDeviceOnlyRule（仅 D2D 的 ub_ctp 分组 → DIRECT）
        cross ? kCrossInstanceRules : kSameInstanceRules   # 按序尝试
        全部落空 ──> 返回 PARAM_INVALID（这就是匹配失败的错误码）
```

两张规则表（按优先级从高到低）：

**跨实例（kCrossInstanceRules，4 条，全部 SINGLE/DIRECT）**：

| 序 | 协议 | 位置 | CommType | 说明 |
| --- | --- | --- | --- | --- |
| 1 | uboe | device | UBOE | 跨实例优先设备侧 uboe |
| 2 | ub_rtp | device | UBG | 次选 ub_rtp |
| 3 | roce | device | ROCE | 再次选设备侧 roce |
| 4 | roce | host | ROCE | 兜底主机侧 roce |

**同实例（kSameInstanceRules，6 条）**：

| 序 | 类型 | 协议 | 位置 | CommType | 说明 |
| --- | --- | --- | --- | --- | --- |
| 1 | GROUP | （UB 组） | mixed | UB_D2D 等 | 同实例优先 UB 分组 |
| 2 | SINGLE | hccs | device | HCCS | 次选 hccs |
| 3 | SINGLE | uboe | device | UBOE | |
| 4 | SINGLE | ub_rtp | device | UBG | |
| 5 | SINGLE | roce | device | ROCE | |
| 6 | SINGLE | roce | host | ROCE | |

SINGLE 匹配（`TryMatchSingle`）要求同一 `protocol:placement` 在两端列表中**都存在**才算命中；GROUP 匹配（`TryMatchGroup`）则用 UB 的三维键 `MatchKey{dst_eid, plane, placement}` 在远端建 map 逐个配对，按 D2D/H2D/D2H/H2H 四种 CommType 去重，最多产出 4 对（`kMaxUbCsClientNum = 4`）。

#### 4.4.3 源码精读

- [endpoint_matcher.h:23-50](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/endpoint_matcher.h#L23-L50)：`MatchKey` 结构——UB 端点的匹配键。注意 `Matches` 的宽容规则：任一侧 `dst_eid` 为空则不比较 EID（39-41 行），但 `plane` 与 `placement` 必须严格相等。
- [endpoint_matcher.cc:35-59](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/endpoint_matcher.cc#L35-L59)：两张规则表的本体，每条规则带一个 `reason` 字符串（如 "cross-instance prefers device uboe"），命中时打进事件日志，非常利于排查「为什么选了这条链路」。
- [endpoint_matcher.cc:61-63](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/endpoint_matcher.cc#L61-L63)：`kSameInstanceUbCtpDeviceOnlyRule`——u3-l2 提过的「全 device ub_ctp 仅 D2D」特例规则。
- [endpoint_matcher.cc:113-116](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/endpoint_matcher.cc#L113-L116)：`IsCrossInstance`——只比较两端列表**第 0 个**端点的 `net_instance_id`。这隐含一个前提：同一实例生成的端点共享同一个 net_instance_id。
- [endpoint_matcher.cc:128-142](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/endpoint_matcher.cc#L128-L142)：`TryMatchSingle`——lambda 按 `protocol && placement` 在两端各 `find_if`，双双命中则产出一对，否则返回 FAILED 继续下一条规则。
- [endpoint_matcher.cc:144-163](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/endpoint_matcher.cc#L144-L163)：`TryMatchUb`——对每个本地 UB 端点，用 `comm_id` 作为查询 EID、分别尝试 device/host 两个 placement 在远端 map 中找可配对者（154 行 `ParseCommType` 按两侧 placement 算出 D2D/D2H/H2D/H2H），同类型只保留第一对（155-159 行去重）。
- [endpoint_matcher.cc:189-213](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/endpoint_matcher.cc#L189-L213)：`TryMatchGroup`——先把远端 UB 端点建成 map（198 行），再把本地有 `dst_eid` 的端点排前（200-201 行，精确指定对端的优先配对），逐端点尝试环回匹配与 UB 匹配，达 4 对即止（204-210 行）。
- [endpoint_matcher.cc:230-279](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/endpoint_matcher.cc#L230-L279)：`TryMatchByPriority`——先试特例规则（263-267 行），再按 cross/同实例选表逐条尝试（269-276 行）；**277-278 行是本讲实践任务的答案：全部规则落空时 `HIXL_LOGE(PARAM_INVALID, "Failed to find matched endpoints")`，返回 `hixl::PARAM_INVALID`**。
- [endpoint_matcher.cc:291-299](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/endpoint_matcher.cc#L291-L299)：`MatchEndpoints` 入口，打日志输出两端的 net_instance_id 后转 `TryMatchByPriority`。

#### 4.4.4 代码实践

1. **实践目标**：找到匹配失败的错误码及其传播路径。
2. **操作步骤**：
   - 在 [endpoint_matcher.cc:277](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/endpoint_matcher.cc#L277) 确认错误码为 `PARAM_INVALID`。
   - 用 Grep 追它的两个上游调用点：[hixl_client.cc:69-71](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_client.cc#L69-L71) 中 `MatchEndpoints` 失败会使 `HixlClient::Initialize` 失败，进而导致 Connect/首次传输返回 `PARAM_INVALID`（结合 u2-l4：控制面 socket 连接成功但两端端点无交集时就会走到这里）。
3. **需要观察的现象**：人为构造一个「两端无共同协议」的场景（例如一端 `protocol_desc` 限定 `hccs:device`、另一端限定 `roce:device`）后建链。
4. **预期结果**：日志出现 `Failed to find matched endpoints`，Connect 返回值等于 `hixl::PARAM_INVALID`（107 的错误段，可对照 u2-l2 的错误码表）。**待本地验证**。
5. 若无硬件，可阅读 `tests/cpp/hixl` 下与 endpoint matcher 相关的单测（可用 `Grep pattern="MatchEndpoints" tests/` 定位），从测试断言反推各规则的预期行为。

#### 4.4.5 小练习与答案

**练习 1**：同实例与跨实例的第一条规则分别是什么？为什么跨实例没有 GROUP 规则？

**答案**：同实例第一条是 GROUP（UB 分组，产 UB handler）；跨实例第一条是 SINGLE 的 `uboe:device`。UB 分组依赖两端在同一条 UB 总线上（同 net_instance），跨主机不存在这种共享总线，只能走单协议 DIRECT 链路，所以规则表里全是 SINGLE。

**练习 2**：`TryMatchUb` 中为什么要按 `{kPlacementDevice, kPlacementHost}` 两个 placement 各试一次？

**答案**：一个本地 UB 端点（比如 device 侧 EID）理论上可以与远端 device 端点配成 D2D、与远端 host 端点配成 D2H，两种都是合法的通信路径；各试一次可以在一次建链中同时建立多条不同 CommType 的链路（这正是 UbClientHandler `map<CommType, handle>` 多链路结构的来源，见 u3-l2）。

**练习 3**：`MatchKey::Matches` 里 `dst_eid` 为空时不比较 EID，这样设计的意义是什么？

**答案**：自动生成的 plane 边（D2U/H2U）没有指定具体对端 EID（`dst_eid` 为空），只能按 plane+placement 维度模糊匹配；而 LocalCommRes 生成的 D2D 边带精确 `dst_eid`，可以精确配对。宽容匹配让两类端点共用同一套匹配逻辑。

### 4.5 从远端 engine 字符串到可用 endpoint：端到端串联

#### 4.5.1 概念说明

前面三个模块各自解决了「数据怎么建模」「本端列表怎么来」「两端怎么配对」。本模块把它们串成一条完整链路：用户调用 `Connect("192.168.1.10:26000")` 时传入的只是一个 engine 字符串，最终却要变成一条可传数据的链路——中间发生了什么？理解这条链路，就理解了 HIXL 控制面的核心设计。

#### 4.5.2 核心流程

```
用户: Hixl::Connect(remote_engine="ip:port")
  1. ClientManager 按 remote_engine 找/建 HixlClient（u2-l4）
  2. HixlClient::Initialize(local_endpoint_list, ...):
     a. CtrlMsgPlugin::Connect(server_ip, server_port)      # 控制面 TCP 连到远端 engine 端口
     b. SendEndpointInfoReq(kGetEndpointInfoReq)            # 索要远端端点列表
     c. RecvEndpointInfoResp ──> EndpointGenerator::
        DeserializeEndpointConfigList(json)                 # JSON ──> vector<EndpointConfig>
     d. EndpointMatcher::MatchEndpoints(local, remote)      # 规则表选路
     e. ClientHandlerFactory::Create(handler_type, matched_pairs, ...)
        # DIRECT 消费 matched_pairs[0]；UB 按多条 CommType 建链（u3-l2）
  3. 此后 Connect 只负责启动数据面，传输走 handler
```

注意与 u2-l4 结论的呼应：**endpoint 交换与匹配发生在 HixlClient::Initialize（即首次 Connect）阶段**，复用的正是那条控制面 TCP socket——控制面（socket 上的 endpoint/内存/notify 消息）与数据面（匹配出的链路）分离。

#### 4.5.3 源码精读

- [hixl_client.cc:53-66](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_client.cc#L53-L66)：`HixlClient::Initialize` 前半段——连控制面 socket（59 行）、发 `kGetEndpointInfoReq`（62 行）、收远端 `endpoint_list`（64 行），66 行校验远端列表非空。
- [hixl_client.cc:69-88](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_client.cc#L69-L88)：后半段——调用 `EndpointMatcher::MatchEndpoints`（69-71 行）、把 matched_pairs 与 handler_type 连同 qos/is_lazy 等打包进 `HandlerCreateArgs` 交给 `ClientHandlerFactory::Create`（84-88 行）。u3-l2 讲过的分派，其输入正是本讲匹配器的输出。
- [hixl_client.cc:105-139](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_client.cc#L105-L139)：`RecvEndpointInfoResp`——校验控制面消息头 magic 与 body_size 上限（4MB，31 行常量），取出 JSON 字符串后交给 `EndpointGenerator::DeserializeEndpointConfigList`（138 行）。engine 字符串里的端口在这里被用作了 TCP 端口，而真正数据链路的地址来自 JSON 里的端点。

server 侧的应答来源则在 4.2 已看到：`HixlEngine::Initialize` 生成 `endpoint_list_` 交给 `HixlServer`（[hixl_engine.cc:51-52](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_engine.cc#L51-L52)），server 收到请求后用 `SerializeEndpointConfigList` 序列化发回（u4-l2 的 endpoint 消息处理器）。

#### 4.5.4 代码实践（本讲主实践）

1. **实践目标**：找到 endpoint 匹配失败的错误码，并写一段笔记解释「从远端 engine 字符串到可用 endpoint」的完整转换过程。
2. **操作步骤**：
   - **第一步**：Grep `Failed to find matched endpoints`，定位到 [endpoint_matcher.cc:277-278](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/endpoint_matcher.cc#L277-L278)，记下错误码 `hixl::PARAM_INVALID`。
   - **第二步**：沿调用链向上标注五个站点（建议在笔记里画成时序图）：
     1. `Hixl::Connect("ip:port")` → engine 字符串被 `ParseListenInfo` 拆成 ip/port（u2-l1）；
     2. `HixlClient::Initialize` 用该 ip/port 建 TCP 控制面（[hixl_client.cc:59](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_client.cc#L59)）；
     3. server 把初始化时生成好的 `endpoint_list_` 序列化回传（[endpoint_generator.cc:740-767](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/endpoint_generator/endpoint_generator.cc#L740-L767)）；
     4. client 反序列化得到 `remote_endpoint_list`（[hixl_client.cc:138](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_client.cc#L138)）；
     5. `MatchEndpoints` 按规则表配对（[endpoint_matcher.cc:291-299](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/endpoint_matcher.cc#L291-L299)），产出 matched_pairs → ClientHandler。
   - **第三步**：在笔记末尾回答——engine 字符串本身**从不**参与数据传输，它只是控制面的「门牌号」；真正的数据链路地址全部来自两端各自生成的端点列表。
3. **需要观察的现象**：若跳过第二步直接做，容易误以为 engine 字符串里的 IP 就是数据面地址；对照 `MatchEndpoints` 的输入会发现两者完全解耦。
4. **预期结果**：一份包含错误码（`PARAM_INVALID`）、五个站点文件:行号、一张时序草图的笔记。本实践为源码阅读型，无硬件也可完整完成。

#### 4.5.5 小练习与答案

**练习 1**：server 侧的端点列表是在什么时刻生成的？client 呢？

**答案**：server 在 `HixlEngine::Initialize` 时生成（引擎初始化即完成）；client 的**本地**列表同样在引擎初始化时生成，但**远端**列表在首次 `Connect` 触发的 `HixlClient::Initialize` 中通过控制面拉取。

**练习 2**：如果把 `remote_engine` 写成一个 IP 可达但未启动 server 的地址，失败发生在链路哪一步？

**答案**：失败在第 2 步之前——`CtrlMsgPlugin::Connect` 建 TCP 失败（[hixl_client.cc:59-60](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_client.cc#L59-L60)），根本到不了匹配阶段。这提示排查建链问题时先分清「控制面 TCP 不通」还是「匹配失败（PARAM_INVALID + Failed to find matched endpoints 日志）」。

## 5. 综合实践

**任务：模拟一次完整的选路决策。**

假设本机（A5，同实例）自动生成的本地端点列表为：

```
1. {protocol: "ub_ctp", comm_id: <EID_A>, placement: "device", dst_eid: <EID_B>, plane: ""}
2. {protocol: "uboe",   comm_id: "10.1.1.5", placement: "device"}
3. {protocol: "roce",   comm_id: "192.168.10.1", placement: "device"}
```

远端（同一超节点）回传的列表为：

```
1. {protocol: "ub_ctp", comm_id: <EID_B>, placement: "device", dst_eid: <EID_A>, plane: ""}
2. {protocol: "roce",   comm_id: "192.168.10.2", placement: "device"}
```

请完成：

1. 判断 `IsCrossInstance` 的返回值（提示：两端的 `net_instance_id` 都来自同一个 superpod，相同）。
2. 按 [endpoint_matcher.cc:46-59](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/endpoint_matcher.cc#L46-L59) 的同实例规则表逐条推演：第一条 GROUP 规则是否命中？产出的 matched_pairs 是哪一对、CommType 是什么、handler_type 是 UB 还是 DIRECT？
3. 如果远端不支持 UB（列表里只有第 2 条 roce），匹配会落到第几条规则？此时 handler_type 变成什么？结合 u3-l2 说明这对后续 `TransferAsync` 的分派路径有什么影响。
4. 如果两端 net_instance_id 不同（跨实例），重新走一遍跨实例规则表，写出结果差异。

**参考答案**：

1. 相同 → `cross_instance = false`，走同实例表。
2. GROUP 命中：`TryMatchUb` 用本端 EID_A 作为查询键在远端 map（键为远端各端点的 `{dst_eid, plane, placement}`）中找到 dst_eid=EID_A 的第 1 条，placement 双方都是 device → `CommType = COMM_TYPE_UB_D2D`，产出一对，handler_type = UB。注意本端第 2 条 uboe 虽存在，但 GROUP 已先命中，后续规则不再尝试。
3. 落到第 5 条 `roce:device`（hccs、uboe、ub_rtp 都无法在两端同时命中），handler_type = DIRECT。影响：传输走 DirectClientHandler，只消费 matched_pairs[0]，一对一封装 CS 层 Put/Get；而 UB handler 是多 CommType 聚合语义（一个 TransferReq 聚合多个 BatchHandle）。
4. 跨实例表没有 GROUP：第 1 条 `uboe:device` 要求远端也有 uboe——远端没有则失败；第 2 条 ub_rtp 同理；第 3 条 `roce:device` 两端都有 → 命中，DIRECT + ROCE。差异体现了设计意图：跨主机没有共享 UB 总线，规则表全部退化为单协议 DIRECT。

## 6. 本讲小结

- `EndpointConfig` 是贯穿生成、序列化、匹配三个阶段的统一端点模型，核心字段为 `protocol/comm_id/placement`，UB 类端点额外携带 `plane/dst_eid`。
- `EndpointGenerator::BuildEndpointList` 按优先级工作：显式 `local_comm_res` JSON > 按 SoC 类型自动生成（A5 走 ScaleOut+UB、V2/V3 走 RoCE+HCCS）> `protocol_desc` 过滤；无数卡环境必须显式配置。
- A5 的 ub_ctp 端点由 `local_comm_res_generator_v1`（topo 文件 + `urma_admin` + 路由 → D2D/H2D/D2H/D2U/H2U 边）与 `rootinfo_builder_generator_v1`（EID 字节解码 → 端口表 + CLOS PG）协作生成，是异构集群适配的核心。
- `EndpointMatcher` 用静态优先级规则表选路：同实例优先 UB 分组（GROUP/UB handler），跨实例只用单协议 DIRECT；`MatchKey` 的宽容 EID 匹配让精确边与平面边共用一套逻辑。
- **匹配失败返回 `hixl::PARAM_INVALID`**（`endpoint_matcher.cc:277`），经由 `HixlClient::Initialize` 传播到 Connect/首次传输的返回值。
- engine 字符串只是控制面 TCP 的门牌号；数据链路地址完全来自两端独立生成、经控制面交换、由规则表配对的端点列表。

## 7. 下一步学习建议

- 下一讲 u3-l4（连接池执行器与线程模型）将讲解 `ConnectPoolExecutor` 如何并发执行本讲提到的建链前置流程，建议先复习 u2-l4 的异步建链状态机。
- 想继续深挖控制面消息格式，可提前阅读 `src/hixl/common/ctrl_msg.h`，u4-l2（消息处理器与接收器）会精读 server 侧如何应答 `kGetEndpointInfoReq`。
- 对 UB 多链路传输感兴趣的读者，可回看 u3-l2 的 `UbClientHandler::ClassifyTransfers`，体会本讲产出的多 CommType matched_pairs 如何被逐条消费。
