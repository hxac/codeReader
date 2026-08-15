# 目录结构与公开 API 地图

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `include/` 下四组公开头文件（`hixl`、`cs`、`adxl`、`llm_datadist`）各自的定位与边界。
2. 拿到一个接口名（例如 `TransferSync` 或 `PushKvCache`），能在 `src/` 源码树中快速定位它的实现目录。
3. 说清楚 HIXL、HIXL_CS、LLM-DataDist、ADXL 四套接口之间的分层与依赖关系。
4. 制作并维护一张「头文件 → 实现文件 → 所属组件」的映射表。

本讲是纯「地图课」：不深入任何一条调用链的细节，只建立「接口在哪里声明、实现在哪里落地」的全局索引。后续所有讲义都会反复用到这张地图。

## 2. 前置知识

阅读本讲前，你需要了解（来自 u1-l1 与 u1-l3）：

- **HIXL 三组件分工**：HIXL Engine（底层单边传输引擎）、LLM-DataDist（KV Cache 语义上层接口）、Python 绑定。
- **单边零拷贝通信**：一端发起 READ/WRITE，直接访问远端注册内存，远端 CPU 不参与。
- **头文件（.h）与实现文件（.cc）**：C/C++ 项目里头文件声明「有什么接口」，实现文件决定「接口怎么工作」。公开头文件就是库给用户的合同。
- **Pimpl 惯用法**（Pointer to implementation）：公开类只持有一个指向实现类的指针，实现类定义在 .cc 文件里。好处是用户不需要看到内部依赖，库也能自由修改内部结构而不破坏二进制兼容。HIXL 和 LLM-DataDist 的公开类都用了这个手法。
- **命名空间与 extern "C"**：C++ 接口放在 `namespace` 里（如 `hixl::`、`llm_datadist::`）；而 HIXL_CS 是纯 C 风格函数接口，用 `extern "C"` 包裹，方便 C 程序直接链接。

## 3. 本讲源码地图

| 文件/目录 | 作用 |
| --- | --- |
| [include/hixl/hixl.h](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/hixl/hixl.h) | HIXL Engine 唯一公开入口类 `hixl::Hixl` |
| [include/hixl/hixl_types.h](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/hixl/hixl_types.h) | HIXL 公开数据结构与状态枚举 |
| [include/cs/hixl_cs.h](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/cs/hixl_cs.h) | HIXL_CS 的 C 风格 Client-Server 接口 |
| [include/adxl/adxl_engine.h](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/adxl/adxl_engine.h) | 已废弃的 ADXL 兼容接口 |
| [include/llm_datadist/llm_datadist.h](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/llm_datadist/llm_datadist.h) | LLM-DataDist 入口类 `llm_datadist::LlmDataDist` 及公开类型 |
| [docs/zh/api/cpp/brief.md](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/docs/zh/api/cpp/brief.md) | C++ API 文档简介（产品形态约束） |
| [docs/zh/api/cpp/header_files_and_library_files.md](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/docs/zh/api/cpp/header_files_and_library_files.md) | 官方「头文件 ↔ 库文件」对照表 |
| `src/hixl/`、`src/llm_datadist/`、`src/ops/`、`src/python/` | 四个实现顶层目录（本讲逐个梳理） |

## 4. 核心概念与源码讲解

### 4.1 include 目录：四组公开头文件边界

#### 4.1.1 概念说明

整个仓库只有 8 个公开头文件，按子目录分成四组，每组对应一种「使用姿势」：

| 子目录 | 头文件 | 语言风格 | 定位 | 对应库 |
| --- | --- | --- | --- | --- |
| `include/hixl/` | `hixl.h`、`hixl_types.h` | C++ 类 | 点对点单边零拷贝传输（10 余个核心调用） | `libcann_hixl.so` |
| `include/cs/` | `hixl_cs.h` | C 函数（`extern "C"`） | Client-Server 模式集成单边传输 | `libcann_hixl.so` |
| `include/adxl/` | `adxl_engine.h`、`adxl_types.h` | C++ 类 | **已废弃**的旧接口，仅作兼容保留 | 历史遗留 |
| `include/llm_datadist/` | `llm_datadist.h`、`llm_engine_types.h`、`llm_error_codes.h` | C++ 类 | 大模型 KV Cache 传输（带 Cache 语义） | `libllm_datadist.so` |

这张表的「定位」与「对应库」两列来自官方文档 [header_files_and_library_files.md](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/docs/zh/api/cpp/header_files_and_library_files.md)。注意官方表里只列了 `llm_datadist`、`hixl`、`cs` 三行——ADXL 头文件没有出现在正式支持列表中，它的接口文档叫 `deprecated_ADXL-interface.md`，这本身就是「ADXL 已废弃」的直接证据。

一个重要的边界事实：`include/` 之外的所有头文件（例如 `src/hixl/engine/engine.h`）都是**内部头文件**，不属于稳定 API，二次开发时不要直接依赖它们。

#### 4.1.2 核心流程

判断「某个接口属于哪一组」的思考流程：

1. 看需求语义：只是「把一段内存传到远端」→ `hixl::Hixl`；是「按 layer/block 组织的 KV Cache」→ `llm_datadist::LlmDataDist`；想用 C 语言或 Client-Server 显式模式 → HIXL_CS。
2. 看链接产物：`hixl.h` 与 `hixl_cs.h` 都来自 `libcann_hixl.so`；`llm_datadist.h` 来自 `libllm_datadist.so`。
3. 看文档边界：`docs/zh/api/cpp/` 下的 `HIXL-*-*.md`、`HIXL_CS-interface.md`、`LLM-DataDist-*-*.md` 与四组头文件一一对应；ADXL 的文档全部带 `deprecated_` 前缀。

#### 4.1.3 源码精读

**（1）`hixl::Hixl`：Engine 的唯一公开类。** 整个 HIXL Engine 对外只暴露这一个类，接口能数得过来：

- 声明类与初始化入口 [include/hixl/hixl.h:26-46](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/hixl/hixl.h#L26-L46)：`Hixl` 类只有一个构造/析构加 `Initialize`，注释写明 `local_engine` 为 `host_ip:host_port` 形式、带端口即为 server 角色（u1-l3 已验证过）。
- 内存与链路接口 [include/hixl/hixl.h:60-114](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/hixl/hixl.h#L60-L114)：`RegisterMem`/`DeregisterMem`、同步与异步的 `Connect`/`Disconnect`、`GetAsyncConnectStatus`。
- 传输与通知接口 [include/hixl/hixl.h:124-178](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/hixl/hixl.h#L124-L178)：`TransferSync`/`TransferAsync`/`GetTransferStatus`、`SendNotify`/`GetNotifies`、静态能力查询 `GetCapability`。
- Pimpl 落点 [include/hixl/hixl.h:180-182](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/hixl/hixl.h#L180-L182)：`class HixlImpl;` 只在前向声明，持有 `std::unique_ptr<HixlImpl> impl_`，所以头文件里看不到任何引擎内部依赖。

配套类型在 `hixl_types.h` 中：[include/hixl/hixl_types.h:57-74](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/hixl/hixl_types.h#L57-L74) 定义了 `MemHandle`（void\* 句柄）、`enum MemType { MEM_DEVICE, MEM_HOST }`、`enum TransferOp { READ, WRITE }`、`MemDesc` 与 `TransferOpDesc`——正是 u1-l3 里用过的「两个正交维度 + 三元组」。

**（2）HIXL_CS：C 风格的 Client-Server 接口。**

- C 接口包裹与句柄类型 [include/cs/hixl_cs.h:19-30](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/cs/hixl_cs.h#L19-L30)：`extern "C"` 内用 `void*` 定义 `HixlServerHandle`/`HixlClientHandle`/`MemHandle`，错误码是裸的 `uint32_t` 常量（`HIXL_SUCCESS = 0`、`HIXL_TIMEOUT = 103901` 等），和 `hixl::Status` 枚举是两套体系。
- Server 侧接口 [include/cs/hixl_cs.h:80-115](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/cs/hixl_cs.h#L80-L115)：`HixlCSServerCreate/RegMem/Listen/UnregMem/Destroy`——server 只管创建、注册内存、监听，不主动传输。
- Client 侧接口 [include/cs/hixl_cs.h:124-225](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/cs/hixl_cs.h#L124-L225)：`HixlCSClientCreate/Connect/GetRemoteMem/RegMem`，再加上同步/异步四件套 `BatchPutSync/BatchGetSync/BatchPutAsync/BatchGetAsync` 与 `QueryCompleteStatus`。注意它比 `hixl::Hixl` 多了一个 `GetRemoteMem`——CS 模式下 client 可以直接向 server 查询「你注册了哪些内存」，而 `hixl::Hixl` 需要用户自己通过带外通道（如 socket）交换地址（u1-l3 正是这么做的）。

**（3）ADXL：废弃接口长什么样。** [include/adxl/adxl_engine.h:26-46](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/adxl/adxl_engine.h#L26-L46) 的 `AdxlEngine` 与新版 `Hixl` 接口签名几乎逐行对应（`Initialize`/`RegisterMem`/`Connect`/`TransferSync`...），同样用 Pimpl（[include/adxl/adxl_engine.h:157-159](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/adxl/adxl_engine.h#L157-L159)）。对比两个头文件是理解「新旧迁移」最直观的方式（u8-l4 会专门展开）。

**（4）LLM-DataDist：KV Cache 语义层。** [include/llm_datadist/llm_datadist.h:159-178](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/llm_datadist/llm_datadist.h#L159-L178) 声明 `LlmDataDist(cluster_id, role)`，接口按功能天然分组：

- 初始化选项常量 [include/llm_datadist/llm_datadist.h:34-41](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/llm_datadist/llm_datadist.h#L34-L41)：如 `OPTION_DEVICE_ID`（`llm.DeviceId`）、`OPTION_TRANSFER_BACKEND`（`llm.TransferBackend`）。
- 独立的错误码体系 [include/llm_datadist/llm_datadist.h:44-61](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/llm_datadist/llm_datadist.h#L44-L61)：`LLM_SUCCESS`/`LLM_TIMEOUT`/`LLM_NOT_YET_LINK` 等，基于 `ge::Status`（来自 CANN 图引擎公共头 `ge_api_error_codes.h`），和 HIXL 的 `Status` 又是两套。
- 角色与 Cache 语义类型 [include/llm_datadist/llm_datadist.h:120-147](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/llm_datadist/llm_datadist.h#L120-L147)：`LlmRole`（kPrompt/kDecoder/kMix）、`CacheIndex`（定位远端 Cache 的三元组）、`CacheDesc`/`Cache`。
- 传输接口 [include/llm_datadist/llm_datadist.h:238-319](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/llm_datadist/llm_datadist.h#L238-L319)：`PullKvCache`/`PushKvCache`、Blocks 版本、`RegisterKvCache`/`UnregisterKvCache`。

**（5）产品形态约束。** [docs/zh/api/cpp/brief.md](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/docs/zh/api/cpp/brief.md) 提醒：不同芯片（A2/A3/950）对协议和内存形态的支持不同，例如 A2 上 HCCS 协议的 LLM-DataDist 接口仅支持 D2D。读接口签名时记得这些约束是分产品的。

#### 4.1.4 代码实践

**实践目标**：不写代码，靠「读头文件 + 查官方文档」确认四组头文件的边界，并回答三个问题。

**操作步骤**：

1. 在仓库根目录执行 `ls include/*/`，确认公开头文件恰好 8 个。
2. 打开 [docs/zh/api/cpp/header_files_and_library_files.md](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/docs/zh/api/cpp/header_files_and_library_files.md)，对照上表核验「头文件 ↔ 库文件」三行内容。
3. 分别统计三个 C++ 头文件的公开成员函数个数：`grep -c "Status \|void \|static Status" include/hixl/hixl.h include/adxl/adxl_engine.h include/llm_datadist/llm_datadist.h`。
4. 用 `grep -n "extern \"C\"" include/cs/hixl_cs.h` 确认 CS 接口的 C 风格包裹位置。

**需要观察的现象**：

- `hixl.h` 与 `adxl_engine.h` 的接口清单几乎一一对应（ADXL 是 HIXL 的历史前身）。
- `llm_datadist.h` 中传输类接口数量明显更多，且都带 Cache/Index/Blocks 字样。
- `hixl_cs.h` 中没有任何 class，全是自由函数。

**预期结果**：`hixl.h` 约 15 个公开成员函数（含静态 `GetCapability`），`adxl_engine.h` 与之相近但多出 `MallocMem`/`FreeMem`，`llm_datadist.h` 约 15 个且全部围绕 Cache 语义。若统计口径不同数字略有出入属正常，重点是比例与分组。待本地验证（grep 结果依赖具体模式）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `hixl_cs.h` 用 `extern "C"` + `void*` 句柄，而 `hixl.h` 用 namespace + class？

**答案**：HIXL_CS 面向 C/C++ 混合集成场景（甚至纯 C 调用方），C 语言没有 namespace、类和重载，所以接口必须是纯 C 函数，对象用 `void*` 句柄表示；`hixl::Hixl` 面向 C++ 用户，用类封装可以支持 RAII、重载（如两个 `GetTransferStatus`/`GetAsyncConnectStatus` 重载）和类型安全。

**练习 2**：用户代码里 `#include "engine/engine.h"`（src 下内部头文件）有什么风险？

**答案**：`src/` 下的头文件不属于公开 API，没有兼容性承诺，版本升级时函数签名、头文件路径都可能变化，直接依赖会导致编译失败或行为变化；正确做法是只 include `include/` 下四组头文件。

**练习 3**：`RegisterMem` 在 `hixl::Hixl`、HIXL_CS、`LlmDataDist` 三套接口中分别叫什么？

**答案**：`hixl::Hixl::RegisterMem(const MemDesc&, MemType, MemHandle&)`；HIXL_CS 中是 `HixlCSServerRegMem`/`HixlCSClientRegMem`（server/client 各一个 C 函数）；LLM-DataDist 中语义对应 `RegisterKvCache(const CacheDesc&, addrs, cfg, cache_id&)`，把「注册内存」升级成了「注册带 Cache 语义的 KV 内存」。

### 4.2 src 目录结构：实现目录地图

#### 4.2.1 概念说明

`src/` 下有四个顶层目录，与公开头文件的对应关系是「多对多，但有主从」：

```
src/
├── hixl/              # HIXL Engine 实现（hixl.h 与 hixl_cs.h 的落地）
│   ├── engine/        #   引擎抽象、HixlImpl、client/server、endpoint 生成匹配
│   ├── cs/            #   CS（Communication Server/Service）模块：hixl_cs.h 的实现
│   ├── fabric_mem/    #   FabricMem 传输模式（超节点场景）
│   ├── common/        #   日志、checker、线程池等公共组件
│   ├── proxy/         #   对昇腾底层系统接口（dcmi/hal 等）的动态加载封装
│   └── profiling/     #   性能数据上报
├── llm_datadist/      # LLM-DataDist 实现（llm_datadist.h 与 adxl_engine.h 的落地）
│   ├── api/           #   LlmDataDistImpl 与 AdxlEngineImpl（公开类的实现层）
│   ├── cache_mgr/     #   Cache 管理
│   ├── data_transfer/ #   传输任务（Job）体系
│   ├── link_mgr/      #   集群建链
│   ├── fsm/           #   发送/接收状态机
│   ├── memory/        #   自研内存子系统
│   ├── transfer_engine/ # 传输后端抽象（含 HIXL 后端适配）
│   ├── comm_adapter/  #   后端桥接
│   ├── adxl/          #   ADXL 旧机制（buffer transfer、channel manager 等）
│   ├── common/ 和 utils/
├── ops/hixl_kernel/   # device 侧算子/内核（配合 fabric_mem 传输）
└── python/            # Python 绑定
    ├── hixl_py/       #   pybind11 绑定 HIXL（对应 hixl.h）
    ├── llm_datadist/  #   LLM-DataDist Python 包
    ├── llm_wrapper/ 与 metadef_wrapper/  # 包装层
```

关键认知：**公开头文件目录和实现目录不是一一对应的**。`include/hixl/hixl.h` 的实现在 `src/hixl/engine/hixl_impl.cc`；`include/cs/hixl_cs.h` 的实现在 `src/hixl/cs/hixl_cs.cc`（同一张 `libcann_hixl.so` 库的两个入口）；而 `include/adxl/adxl_engine.h` 的实现却在 `src/llm_datadist/api/adxl_engine_impl.cc`（历史原因，ADXL 归属 LLM-DataDist 侧）。

#### 4.2.2 核心流程

从「一个公开接口调用」定位实现代码的通用流程：

```
用户调用 Hixl::TransferSync(...)
   │
   ├─ 1. 查公开头文件 include/hixl/hixl.h          → 确认签名与语义（Pimpl: impl_）
   ├─ 2. 查同名实现文件 src/hixl/engine/hixl_impl.cc → Hixl::Xxx 转发到 Hixl::HixlImpl::Xxx
   ├─ 3. HixlImpl 持有内部引擎（engine.h 的 Engine 抽象）
   │       └─ src/hixl/engine/ 下按链路类型分派（direct/UB handler 等）
   └─ 4. 需要跨进程协商时进入 src/hixl/cs/（消息、通道、端点存储）
```

命名规律（源码树内通用的查找启发式）：

- 类 `Xxx` 的实现通常在 `xxx.cc` 或 `xxx_impl.cc`（`Hixl` → `hixl_impl.cc`，`LlmDataDist` → `llm_datadist_impl.cc`）。
- C 函数 `HixlCSServerXxx` 在 `hixl_cs.cc` 入口，转给 `hixl_cs_server.cc`/`hixl_cs_client.cc` 里的 C++ 类。
- 拿不准时 `grep -rn "符号名" src/` 永远是最快的路。

#### 4.2.3 源码精读

**（1）HIXL 公开类 → 实现的转发。** [src/hixl/engine/hixl_impl.cc:35](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_impl.cc#L35) 定义 `class Hixl::HixlImpl`（嵌套在公开类里，外部不可见）；[src/hixl/engine/hixl_impl.cc:241-250](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_impl.cc#L241-L250) 是公开 `Hixl::Initialize` 的实现：构造 `HixlImpl`、调用其 `Initialize`、再 `std::move` 进 `impl_` 成员——教科书式的 Pimpl 转发。其余 `RegisterMem`（L259 起）、`TransferSync` 等接口都是同样的两行式转发。

**（2）内部引擎抽象的入口。** [src/hixl/engine/engine.h:20-53](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/engine.h#L20-L53) 定义纯虚基类 `hixl::Engine`，接口清单与公开 `Hixl` 类几乎镜像（`Initialize/RegisterMem/Connect/TransferSync...`）。`HixlImpl` 持有的就是这个抽象，由 `engine_factory` 按链路/模式创建具体实现（u3-l1 展开）。这说明 `src/hixl/engine/` 是引擎真正的「心脏」。

**（3）HIXL_CS 的 C 入口。** [src/hixl/cs/hixl_cs.cc:11-18](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs.cc#L11-L18) 里 include 了 `cs/hixl_cs.h`（公开头）与 `hixl_cs_server.h`/`hixl_cs_client.h`（内部 C++ 类）；[src/hixl/cs/hixl_cs.cc:23](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs.cc#L23) 的 `HixlCSServerCreate` 先做参数检查（端口范围校验），再转给内部 server 对象。整个 `src/hixl/cs/` 目录（channel、endpoint_store、msg_handler、transfer_pool 等）都服务于这套 C 接口（u4 单元展开）。

**（4）ADXL 实现为何在 llm_datadist 下。** [src/llm_datadist/api/adxl_engine_impl.cc:65-98](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/api/adxl_engine_impl.cc#L65-L98) 定义 `AdxlEngine::AdxlEngineImpl`，其私有成员是 `std::unique_ptr<hixl::Engine> engine_`——废弃的 `AdxlEngine` 内部已经改为直接复用 HIXL 引擎抽象：[src/llm_datadist/api/adxl_engine_impl.cc:107](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/api/adxl_engine_impl.cc#L107) 中 `engine_ = hixl::EngineFactory::CreateEngine(...)`。这证明「ADXL 接口只是 HIXL 引擎的一层兼容壳」。

**（5）LLM-DataDist 实现入口。** [src/llm_datadist/api/llm_datadist_impl.cc:135](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/api/llm_datadist_impl.cc#L135) 定义 `class LlmDataDist::LlmDataDistImpl`；该文件头部（L11-23）include 了 `cache_mgr/cache_manager.h`、`llm_datadist_v2.h`、`common/hixl_utils.h` 等内部头，可见 `api/` 目录是「公开类 → 各子模块」的汇聚点。真正对接 HIXL 引擎的是 `transfer_engine/` 子目录（例如 [src/llm_datadist/transfer_engine/hixl_transfer_engine.cc:77](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/transfer_engine/hixl_transfer_engine.cc#L77) 的 `HixlTransferEngine::Initialize`，u6-l7 展开）。

**（6）Python 绑定与 device 侧。** `src/python/hixl_py/hixl_py.cc` 用 pybind11 把 `hixl::Hixl` 暴露给 Python（u7-l1 精读）；`src/ops/hixl_kernel/` 存放 device 侧内核源码（如 `fabric_mem_aicpu_kernel.cc`、`hixl_batch_transfer.cc`），由 device 工具链编译（u1-l2 提到的 host/device 产物之分就来源于此）。

#### 4.2.4 代码实践

**实践目标**：制作「接口 → 实现」映射表，并用官方文档校对。

**操作步骤**：

1. 对每个公开入口符号执行一次符号定位（源码阅读型实践，无需硬件）：

   ```bash
   # Hixl 公开类的实现转发
   grep -n "Status Hixl::Initialize" src/hixl/engine/hixl_impl.cc
   # HIXL_CS C 入口
   grep -n "HixlCSServerCreate\|HixlCSClientCreate" src/hixl/cs/hixl_cs.cc
   # ADXL 公开类的实现
   grep -n "class AdxlEngine::AdxlEngineImpl" src/llm_datadist/api/adxl_engine_impl.cc
   # LlmDataDist 公开类的实现
   grep -n "class LlmDataDist::LlmDataDistImpl" src/llm_datadist/api/llm_datadist_impl.cc
   ```

2. 把结果整理成下面格式的表（答案模板见「预期结果」）。
3. 打开 [docs/zh/api/cpp/header_files_and_library_files.md](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/docs/zh/api/cpp/header_files_and_library_files.md)，核对你的表中「所属库」一列是否与官方一致。

**需要观察的现象**：四个符号都能一次性命中唯一实现文件，不需要二次猜测；`adxl_engine_impl.cc` 与 `llm_datadist_impl.cc` 同在 `src/llm_datadist/api/` 目录。

**预期结果**（可作为映射表标准答案）：

| 公开头文件 | 入口符号 | 实现文件 | 所属组件/库 |
| --- | --- | --- | --- |
| `include/hixl/hixl.h` | `hixl::Hixl` | `src/hixl/engine/hixl_impl.cc`（`HixlImpl` 在 L35） | HIXL Engine / `libcann_hixl.so` |
| `include/cs/hixl_cs.h` | `HixlCSServerCreate` 等 | `src/hixl/cs/hixl_cs.cc`（入口 L23，转 `hixl_cs_server.cc`/`hixl_cs_client.cc`） | HIXL Engine CS 模块 / `libcann_hixl.so` |
| `include/adxl/adxl_engine.h` | `adxl::AdxlEngine` | `src/llm_datadist/api/adxl_engine_impl.cc`（`AdxlEngineImpl` 在 L65） | LLM-DataDist 侧兼容层（已废弃） |
| `include/llm_datadist/llm_datadist.h` | `llm_datadist::LlmDataDist` | `src/llm_datadist/api/llm_datadist_impl.cc`（`LlmDataDistImpl` 在 L135） | LLM-DataDist / `libllm_datadist.so` |
| `include/hixl/hixl_types.h` | `MemDesc`/`TransferOp` 等 | 仅头文件（内联定义），引擎侧对应 `src/hixl/engine/` | HIXL Engine |
| `include/llm_datadist/llm_engine_types.h`、`llm_error_codes.h` | 辅助类型/错误码 | 仅头文件 | LLM-DataDist |

**待本地验证**：上表行号基于当前 HEAD（a5dd1de），代码演进后需用步骤 1 的 grep 重新定位。

#### 4.2.5 小练习与答案

**练习 1**：`hixl_cs.h` 和 `hixl.h` 对应同一个 `libcann_hixl.so`，为什么仓库要把 CS 的实现单独放在 `src/hixl/cs/` 目录？

**答案**：这是按「模块内聚」划分：CS 模块有自己完整的运行时（消息接收 msg_receiver、消息处理 msg_handler、通道 channel、端点存储 endpoint_store、传输池 transfer_pool），与 `engine/` 目录的「引擎抽象 + 建链匹配」职责不同。同一张 so 只是链接产物，源码组织按模块走。

**练习 2**：只用 `ls` 和目录名，推断 `src/llm_datadist/fsm/`、`src/llm_datadist/memory/` 大概做什么。

**答案**：`fsm` = Finite State Machine，管理传输会话的发送/接收状态迁移（u6-l6）；`memory` 是 LLM-DataDist 自研的内存子系统（分配器 + span 管理，u6-l8），支撑 Cache 的分配与复用。目录命名在这个仓库里高度自解释，这是快速建立地图的可靠手段。

**练习 3**：为什么 `src/ops/hixl_kernel/` 里的代码不跟 `src/hixl/` 放在一起？

**答案**：它们编译目标不同：`src/ops/hixl_kernel` 是 device 侧（AICPU/算子）代码，要用 CANN 的 device 工具链（hcc）交叉编译；`src/hixl` 是 host 侧代码。u1-l2 讲过 build.sh 的 `--host` 参数就是利用这个划分跳过 device 子工程加速编译。

### 4.3 四套接口间的关系：一张分层图

#### 4.3.1 概念说明

四套公开接口不是平行的四个库，而是有明确的上下层关系：

```
应用（vLLM / SGLang / 自研推理引擎）
   │
   ├── LlmDataDist (llm_datadist.h, libllm_datadist.so)   ← KV Cache 语义层
   │        │  内部经 transfer_engine/comm_adapter 适配
   │        ▼
   ├── Hixl (hixl.h, libcann_hixl.so)                     ← 点对点传输层
   │        │  内部经 Engine 抽象 → client handler / CS / FabricMem
   │        ▼
   ├── HixlCS (hixl_cs.h, libcann_hixl.so)                ← CS 模式的同层入口
   │
   └── AdxlEngine (adxl_engine.h, 已废弃)                  ← 历史兼容壳，内部已直连 hixl::Engine
```

三句话总结：

1. **LLM-DataDist 建在传输后端之上**：它把「KV Cache/Block」语义翻译成底层传输后端（可配 `llm.TransferBackend` 选项选择，HIXL 是其中一种后端）。
2. **HIXL 与 HIXL_CS 是同层两种入口**：同一套引擎能力的「类风格」与「C 风格 CS 模式」两种暴露方式。
3. **ADXL 是兼容壳**：接口签名保留给老用户，内部通过 `EngineFactory` 直接创建 HIXL 引擎。

#### 4.3.2 核心流程

用户选型的判断流：

1. 是否使用大模型推理框架、按 layer/block 传 KV Cache？是 → `LlmDataDist`。
2. 是否只需要「把一块内存传过去」的点对点直传？是 → `Hixl`。
3. 是否是 C 语言集成 / 需要 server 集中管理内存并通过 `GetRemoteMem` 发现远端内存？是 → HIXL_CS。
4. 还在维护老 ADXL 工程？→ 继续用 `AdxlEngine`，但新代码应迁移到上述三者（u8-l4 给出迁移清单）。

#### 4.3.3 源码精读

**（1）ADXL → HIXL 的直连证据。** 再看一次 [src/llm_datadist/api/adxl_engine_impl.cc:107](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/api/adxl_engine_impl.cc#L107)：`AdxlEngineImpl::Initialize` 里 `engine_ = hixl::EngineFactory::CreateEngine(local_engine_, options, parsed_options);`——ADXL 没有自己的传输实现，直接借 HIXL 引擎干活。

**（2）LLM-DataDist → 传输后端抽象的证据。** [include/llm_datadist/llm_datadist.h:40](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/llm_datadist/llm_datadist.h#L40) 定义选项 `OPTION_TRANSFER_BACKEND = "llm.TransferBackend"`，说明 KV Cache 传输的后端是可配置的；而 `src/llm_datadist/transfer_engine/hixl_transfer_engine.cc` 中 `HixlTransferEngine::Initialize`（[L77](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/transfer_engine/hixl_transfer_engine.cc#L77)）以及 [L29](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/llm_datadist/transfer_engine/hixl_transfer_engine.cc#L29) 的 `LLMDataDist2HixlOptions`（把 LLM 侧选项翻译成 `hixl::HixlOptions`）就是「HIXL 作为后端被接入」的落点。

**（3）引擎工厂统一入口。** [src/hixl/engine/engine_factory.h:21-23](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/engine_factory.h#L21-L23) 的 `EngineFactory::CreateEngine` 是创建具体引擎实例的唯一工厂——无论入口是 `Hixl`（经 `HixlImpl`）还是 `AdxlEngine`（经 `AdxlEngineImpl`），最终都汇聚到这一个内部工厂。

#### 4.3.4 代码实践

**实践目标**：亲手验证「ADXL 是 HIXL 的壳、LLM-DataDist 把 HIXL 当后端」两条结论。

**操作步骤**：

1. 执行 `grep -rn "hixl::" src/llm_datadist/api/adxl_engine_impl.cc | head`，观察废弃接口的实现对 `hixl::` 命名空间的依赖。
2. 执行 `grep -rln "EngineFactory" src/`，统计哪些文件直接创建引擎。
3. 执行 `grep -rn "HixlOptions" src/llm_datadist/transfer_engine/hixl_transfer_engine.cc | head`，确认选项翻译层的存在。

**需要观察的现象**：`adxl_engine_impl.cc` 中大量出现 `hixl::Engine`、`hixl::HixlOptions`、`hixl::MemType` 等类型转换调用；`EngineFactory` 的使用点集中在 `src/hixl/engine/` 与 `src/llm_datadist/api/`。

**预期结果**：两个结论都得到 grep 证据支持——ADXL 每个接口方法基本是「检查初始化状态 + 转发到 engine_ 对应方法」的两段式；`hixl_transfer_engine.cc` 中存在 LLM 选项 → HIXL 选项的显式翻译函数。待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：既然 `AdxlEngine` 和 `Hixl` 接口几乎相同，为什么不直接让老代码把 `adxl::AdxlEngine` 全局替换成 `hixl::Hixl`？

**答案**：两者错误码、类型头（`adxl_types.h` vs `hixl_types.h`）、少量接口差异（ADXL 多 `MallocMem`/`FreeMem`，HIXL 多异步建链与 `GetTransferStatus` 批量重载）并不完全对齐，直接替换会编译失败或语义漂移；正确做法是按 u8-l4 的迁移清单逐项映射。

**练习 2**：`LlmDataDist` 为什么不直接调用 `hixl::Hixl` 公开类，而是自己实现了一个 `HixlTransferEngine` 适配层？

**答案**：LLM-DataDist 需要支持多种传输后端（`llm.TransferBackend` 可配），必须定义自己的后端抽象（`transfer_engine/`）；HIXL 只是其中一种实现。同时它还需要 cluster 建链、Cache 寻址等 KV 语义逻辑，这些不属于点对点传输层的职责。适配层让「语义」与「传输」解耦。

**练习 3**：一个新项目要做「两进程间搬 2GB 的设备内存，不带任何 Cache 语义」，应该选哪套接口？为什么？

**答案**：选 `hixl::Hixl`（若集成方是纯 C 程序则选 HIXL_CS）。需求里没有 KV Cache/layer/block 概念，LLM-DataDist 的角色模型和 Cache 管理纯属额外负担；`Hixl` 的「注册内存 → 建链 → TransferAsync → 查状态」序列正好覆盖该场景（u1-l3 的 quickstart 就是这个形态）。

## 5. 综合实践

**任务：制作你自己的《HIXL 接口-实现导航手册》。**

把本讲三张表合并成一份可长期维护的导航文档，步骤：

1. **接口层**：遍历 `include/` 四个子目录的 8 个头文件，为每套接口列出：入口类/函数族、错误码类型、选项常量、Pimpl 成员名。
2. **实现层**：用 4.2.4 的 grep 方法定位每个入口符号的实现文件与起始行号，记录成「头文件 → 实现文件 → 关键行号」表。
3. **关系层**：用 4.3 的方法验证三条依赖边（ADXL→hixl::Engine、LLM-DataDist→transfer_engine→HIXL、Hixl/HixlCS→同一 so），把每条边配上一个源码证据（文件+行号）。
4. **校对**：与 [docs/zh/api/cpp/header_files_and_library_files.md](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/docs/zh/api/cpp/header_files_and_library_files.md) 和 [docs/zh/api/cpp/brief.md](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/docs/zh/api/cpp/brief.md) 比对，标注任何不一致之处（例如 ADXL 是否出现在官方支持表中）。

产出物是一份 Markdown 表格文档，后续每学一讲（如 u3 进 Engine、u4 进 CS、u6 进 LLM-DataDist），就在对应行下补充二级实现目录的细节，让这份手册跟着你的学习深度一起生长。

## 6. 本讲小结

- 公开头文件只有四组：`include/hixl`（点对点 C++ 类）、`include/cs`（C 风格 Client-Server）、`include/adxl`（已废弃兼容层）、`include/llm_datadist`（KV Cache 语义层），其余头文件均为内部实现细节。
- `hixl.h` 与 `hixl_cs.h` 同属 `libcann_hixl.so`，`llm_datadist.h` 对应 `libllm_datadist.so`；三套接口各有独立的错误码体系。
- 实现目录地图：`src/hixl/{engine,cs,fabric_mem,common,proxy,profiling}`、`src/llm_datadist/{api,cache_mgr,data_transfer,link_mgr,fsm,memory,transfer_engine,adxl,...}`、`src/ops/hixl_kernel`（device 侧）、`src/python`（绑定）。
- 公开类普遍使用 Pimpl：`Hixl`→`hixl_impl.cc` 的 `HixlImpl`，`LlmDataDist`→`llm_datadist_impl.cc` 的 `LlmDataDistImpl`，`AdxlEngine`→`adxl_engine_impl.cc` 的 `AdxlEngineImpl`。
- 四套接口是上下层关系而非平行关系：ADXL 内部直连 `hixl::EngineFactory::CreateEngine`；LLM-DataDist 经 `HixlTransferEngine` 适配层把 HIXL 当作可配置的传输后端之一。

## 7. 下一步学习建议

- 下一讲 u1-l5 将运行 d2rd/d2rh/multiproc 样例，观察不同内存类型与传输方向在本讲地图中的落点。
- 进入单元 2 后，u2-l1 会沿着本讲的映射（`hixl.h` → `hixl_impl.cc` → `hixl_options.cc`）精读 `Hixl` 类的初始化流程。
- 想先看分层关系下半部分（LLM-DataDist 如何接入 HIXL 后端）的读者，可直接预习 `src/llm_datadist/transfer_engine/hixl_transfer_engine.cc` 与设计文档 `docs/zh/design/llm-datadist_supporting_the_hixl_transfer_backend.md`（对应 u6-l7）。
