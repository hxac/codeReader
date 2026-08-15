# FabricMem 实战：d2d 样例端到端

## 1. 本讲目标

学完本讲，你应该能够：

1. 独立使能 FabricMem 模式：知道 `OPTION_ENABLE_USE_FABRIC_MEM` 选项怎么配、`FabricMemConfig` 里有哪些可调参数、以及硬件与 HDK 版本约束。
2. 逐行读懂 `examples/cpp/fabric_mem_d2d.cpp`：包括样例为什么自己用 ACL VMM 接口分配内存、两个进程如何用文件完成握手、双向 WRITE 如何校验。
3. 说清 `FabricMemStatistic` 的工作方式：统计在何处埋点、通道台账如何组织、带宽如何计算、汇总日志何时输出、单次传输耗时日志怎么看。
4. 在真实 A3 环境跑通样例并记录一次传输的带宽数据，与普通路径（如 `hixl_example_d2rd_multiproc`）做对比。

本讲是单元五的收官篇：u5-l1 建立了 FabricMem 的设计观，u5-l2 讲了内存底座，u5-l3 讲了 host/AICPU 两条传输路径，本讲把配置 → 注册 → 传输 → 统计串成完整闭环。

## 2. 前置知识

阅读本讲前，你应当具备以下概念（前几讲均已建立，这里只做一页速查）：

- **VMM 三步机制**：`aclrtMallocPhysical`（申请物理内存）→ `aclrtReserveMemAddress`（预留虚拟地址）→ `aclrtMapMem`（建立映射）。FabricMem 的「统一编址」建立在它之上（见 u5-l1、u5-l2）。
- **单边 WRITE**：发起方同时掌握本地地址与远端地址，直接把本地内存写入远端内存；远端进程无需参与（见 u1-l3、u2-l5）。
- **TransferOpDesc 三元组**：`local_addr + remote_addr + len`，支持 vector 批量下发（见 u2-l2）。
- **engine 标识**：`ip:port` 形式字符串，带端口即为 server 监听地址（见 u2-l1）。
- **FabricMem 传输路径**：`enable_aicpu_unfold` 为 true 走 AICPU 内核下发 SDMA，false 走 host 侧 `aclrtMemcpyAsync`（见 u5-l3）。

本讲新增两个背景概念：

- **dlog 日志体系**：HIXL 内部日志走 CANN 的 dlog（`dlog_info` 等），默认落入 slog 日志文件；把环境变量 `ASCEND_SLOG_PRINT_TO_STDOUT` 设为 `1` 可同时打印到标准输出。统计汇总用的 `HIXL_EVENT` 宏就是基于 `dlog_info` 的。
- **HDK 版本差异**：HDK 25.5 不支持 `aclrtMemRetainAllocationHandle`，FabricMem 场景的 Host 内存必须走 ADXL 的 `MallocMem`/`FreeMem`；HDK 26.0 起可直接用 ACL 接口。本讲样例依赖后者。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `examples/cpp/fabric_mem_d2d.cpp` | FabricMem 模式 d2d 场景样例：双进程成对运行，各自 WRITE 到对端并校验 |
| `examples/cpp/README.md` | 样例运行说明，含 fabric_mem_d2d 的参数与 HDK 约束（209-226 行） |
| `docs/zh/FabricMem.md` | FabricMem 模式官方文档：背景、使能方式、依赖版本、硬件范围 |
| `include/hixl/hixl_types.h` | 公开类型定义，含选项键 `OPTION_ENABLE_USE_FABRIC_MEM`（29 行） |
| `src/hixl/fabric_mem/fabric_mem_config.h` | FabricMem 内部配置结构体 `FabricMemConfig` |
| `src/hixl/engine/fabric_mem_engine.cc` | FabricMemEngine 装配流程，含统计周期 Dump 的启动与停止 |
| `src/hixl/fabric_mem/fabric_mem_statistic.h/.cc` | 统计组件：通道台账、计数更新、带宽汇总 Dump |
| `src/hixl/common/statistic_utils.h` | 统计公共工具：带宽公式、常量、`TransferSummary` 聚合器 |
| `src/hixl/fabric_mem/fabric_mem_channel_manager.cc` | 建链/断链时统计通道的注册与摘除 |
| `src/hixl/fabric_mem/fabric_mem_host_transfer_service.cc` | host 路径传输实现，含每次传输的统计埋点与耗时日志 |

## 4. 核心概念与源码讲解

### 4.1 使能 FabricMem：从初始化选项到引擎装配

#### 4.1.1 概念说明

FabricMem 不是独立进程或独立库，而是 HIXL Engine 的第三种引擎实现（`FabricMemEngine`，见 u3-l1 的工厂分支：FabricMem 开关是第一优先级）。用户侧唯一的使能动作，就是在 `Initialize` 的 options 里加一个键值对。因此「使能方式」的本质是：一个选项键 → `EngineFactory` 选择 `FabricMemEngine` → 引擎读取自己的 `FabricMemConfig` 完成装配。

#### 4.1.2 核心流程

```text
用户调用 Hixl::Initialize(local_engine, options)
  └─ options 含 OPTION_ENABLE_USE_FABRIC_MEM = "1"
       └─ HixlOptions::Parse 解析出 EnableUseFabricMem
            └─ EngineFactory::CreateEngine：FabricMem 分支命中 → FabricMemEngine
                 └─ FabricMemEngine::InitializeLocked
                      ├─ 校验 EnableUseFabricMem 必须为 1（否则 PARAM_INVALID）
                      ├─ aclrtGetDevice / aclrtCreateContext 绑定设备与上下文
                      └─ InitFabricMem：
                           ├─ ApplyVirtualMemoryConfig（应用 FabricMemConfig）
                           ├─ VirtualMemoryManager::GetInstance().Initialize()
                           ├─ fabric_mem_statistic_.StartPeriodicDump()   ← 统计周期 Dump 启动
                           ├─ StartControlServer()                       ← 句柄交换控制面
                           └─ InitTransferService()                      ← host/AICPU 二选一
```

#### 4.1.3 源码精读

选项键定义在公开头文件里（u2-l2 的「字典课」提过选项键常量族）：

- [include/hixl/hixl_types.h:29](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/hixl/hixl_types.h#L29)：定义 `OPTION_ENABLE_USE_FABRIC_MEM = "EnableUseFabricMem"`。注意 options 的 key 用的是这个 ASCII 键名，而不是宏名。

官方文档对使能方式与版本约束的说明：

- [docs/zh/FabricMem.md:63-73](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/docs/zh/FabricMem.md#L63-L73)：列出依赖（HDK ≥ 25.5、灵衢计算网络 ≥ 1.5.0、CANN ≥ 9.0），说明启用方式是 options 里配 `OPTION_ENABLE_USE_FABRIC_MEM`（"1" 开 / "0" 关），并强调硬件范围仅支持 Atlas A3 训练/推理系列；还可通过 `OPTION_GLOBAL_RESOURCE_CONFIG` 的 `fabric_memory.*` 字段调虚存池。

内部配置结构体：

- [src/hixl/fabric_mem/fabric_mem_config.h:22-32](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/fabric_mem/fabric_mem_config.h#L22-L32)：`FabricMemConfig` 的全部字段——`enabled`（开关解析结果）、`auto_connect`、`capacity_tb`/`has_capacity_tb` 与 `start_address_tb`/`has_start_address_tb`（虚存池容量与起始地址，可选，默认不设即用 `VirtualMemoryManager` 默认 32TB，见 u5-l2）、`task_stream_num`（默认 1）与 `max_stream_num`（默认 512）、`enable_aicpu_unfold`（默认 true，即默认走 AICPU 路径）。`has_xxx` 布尔与值分离的写法，是为了区分「用户没配」与「配成 0」。

引擎装配时的两处关键代码：

- [src/hixl/engine/fabric_mem_engine.cc:112-113](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/fabric_mem_engine.cc#L112-L113)：引擎内再校验一次 `EnableUseFabricMem` 必须为 1，否则 `PARAM_INVALID`——选项解析与引擎校验双保险。
- [src/hixl/engine/fabric_mem_engine.cc:93-104](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/fabric_mem_engine.cc#L93-L104)：`InitFabricMem` 的装配顺序——应用虚存配置 → 初始化 `VirtualMemoryManager` → **启动统计周期 Dump** → 启动控制面 server → 初始化传输服务。注意统计组件在控制面与传输服务之前就位，保证后续任何一次传输都能被计数。装配用 `HIXL_DISMISSABLE_GUARD` 失败回滚（u5-l2 讲过 scope guard 风格）。

#### 4.1.4 代码实践

1. **实践目标**：确认「一个选项键如何改变引擎选择」，并整理 FabricMem 的全部可调参数。
2. **操作步骤**：
   - 通读 `fabric_mem_config.h`（全文仅 36 行），把 7 个字段抄成一张速查表。
   - 用 `grep -rn "OPTION_ENABLE_USE_FABRIC_MEM" src/ include/ examples/` 找出该键从解析到引擎校验的全部出现点。
3. **需要观察的现象**：grep 结果应覆盖 `hixl_types.h`（定义）、`hixl_options.cc`（解析）、`fabric_mem_engine.cc`（校验）、`fabric_mem_d2d.cpp`（使用）四处。
4. **预期结果**：你能画出「选项字符串 → HixlOptions → EngineFactory → FabricMemConfig → VirtualMemoryManager」这条链，并说出 `task_stream_num`/`max_stream_num`/`enable_aicpu_unfold` 各自影响哪一层（前两个影响传输服务 stream 槽位，最后一个决定 host/AICPU 路径分叉）。
5. 本实践为纯源码阅读，无硬件也可完成。

#### 4.1.5 小练习与答案

**练习 1**：如果用户在 options 里写了 `EnableUseFabricMem = "0"`，会发生什么？
**答案**：`HixlOptions::Parse` 解析出 `EnableUseFabricMem=false`，`EngineFactory` 的 FabricMem 分支不命中，引擎按后续分支（LocalCommRes version、protocol_desc 等）选择其他引擎；即使某个路径强行进入 `FabricMemEngine::InitializeLocked`，[fabric_mem_engine.cc:112-113](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/fabric_mem_engine.cc#L112-L113) 也会以 `PARAM_INVALID` 拒绝。

**练习 2**：`FabricMemConfig` 为什么同时需要 `capacity_tb` 和 `has_capacity_tb` 两个字段？
**答案**：用 `std::optional` 语义（此处以 bool+值手动实现）区分「用户没有配置该项」（应使用 `VirtualMemoryManager` 的默认容量 32TB，见 u5-l2）与「用户显式配置为 0」（一个应当被拒绝或特殊处理的非法值）。若只有一个 `size_t`，两者无法区分。

### 4.2 fabric_mem_d2d 样例精读：VMM 内存、文件握手与双向校验

#### 4.2.1 概念说明

这个样例演示 FabricMem 模式下最简单的 d2d 场景：**两个进程各自持有 2MB 的 device 内存，各自把本地前 1MB 写到对端后 1MB，然后各自校验收到的数据**。它有三个与 quickstart（u1-l3）显著不同的写法：

1. **内存由用户自己用 ACL VMM 接口分配**，而不是 `aclrtMalloc`——因为 FabricMem 的注册路径要求内存在 VMM 管理之下（物理内存 + 预留虚拟地址 + 映射）。
2. **两个进程是对称的**：双方都 Initialize（都带端口，都是 server）、都 Connect、都发起 WRITE。没有 quickstart 里「server 被动、client 主动」的角色分工。
3. **握手靠文件系统**：双方把「本端 VA + device_id」写进以对端 engine 字符串命名的文件里，用 `.init_done`/`.done` 后缀文件做阶段信号，60 秒超时等待。

为什么地址可以「写文件交换」？因为 FabricMem 模式下两端地址要经 `VirtualMemoryManager` 翻译（u5-l2 的 ShareHandle 导入）——传输前引擎会自动把旧 VA 翻译成 fabric 视角新 VA，所以样例只需交换用户视角的原始 VA。

#### 4.2.2 核心流程

单个进程的主流程（`main → Run`）：

```text
main: 解析 3 个参数(device_id, local_engine, remote_engine)
  aclInit → aclrtSetDevice → Run:
    1. Initialize：options[EnableUseFabricMem]="1" → Hixl::Initialize
    2. AllocateBuffer：VMM 三步分配 2MB device 内存，并用 device_id 字节填充
    3. RegisterMem(desc, MEM_DEVICE) → 得到 handle
    4. 写文件 <local_engine>（内容：va + device_id），再写 <local_engine>.init_done
    5. 等待 <remote_engine>.init_done 出现（对端也完成注册）
    6. Connect(remote_engine)
    7. Transfer：
         等待 <remote_engine> 文件出现，读出 remote_addr / remote_dev_id
         TransferSync(remote_engine, WRITE, {local va → remote_addr + 1MB, 1MB})
         写 <local_engine>.done
    8. 等待 <remote_engine>.done（对端也写完我）
    9. VerifyBuffer：校验本地 buffer 后 1MB 每个字节 == remote_dev_id（低 8 位）
   10. Finalize：Disconnect → DeregisterMem → Finalize
```

数据流（双向对称）：

```text
进程 A 本地 [va, va+1MB)   ──WRITE──▶  进程 B 远端 [remote_va+1MB, remote_va+2MB)
进程 B 本地 [va, va+1MB)   ──WRITE──▶  进程 A 远端 [remote_va+1MB, remote_va+2MB)
校验：各自检查本地后半段是否等于对端 device_id 的字节填充
```

#### 4.2.3 源码精读

**初始化——一行选项使能 FabricMem**：

- [examples/cpp/fabric_mem_d2d.cpp:50-60](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/fabric_mem_d2d.cpp#L50-L60)：样例的 `Initialize` 函数，options 只有一项 `OPTION_ENABLE_USE_FABRIC_MEM = "1"`。与 quickstart 相比，这就是 FabricMem 场景的全部额外配置。

**VMM 三步分配——样例自己管理内存**：

- [examples/cpp/fabric_mem_d2d.cpp:132-149](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/fabric_mem_d2d.cpp#L132-L149)：`AllocateBuffer` 依次调用 `aclrtReserveMemAddress`（预留 VA）、`aclrtMallocPhysical`（按 `ACL_HBM_MEM_HUGE` + device 位置申请物理内存）、`aclrtMapMem`（映射）；随后用一块临时 host 内存把整个 buffer 填充为 `device_id` 字节。注意：这里的 `aclrtMallocHost`/`aclrtMemcpy` 只是初始化手段，**不是** FabricMem 的 host 内存注册。

**注册与文件握手**：

- [examples/cpp/fabric_mem_d2d.cpp:217-233](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/fabric_mem_d2d.cpp#L217-L233)：以 `MemDesc{addr, len}` + `MEM_DEVICE` 注册 2MB buffer；随后把 `va` 和 `device_id` 写进以 `local_engine` 命名的文件——engine 字符串在此被复用为文件名，这是样例的私有约定。
- [examples/cpp/fabric_mem_d2d.cpp:234-250](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/fabric_mem_d2d.cpp#L234-L250)：写 `<local_engine>.init_done` 信号文件，等待 `<remote_engine>.init_done` 出现（`WaitFile` 每 1 秒轮询一次、60 秒超时，见 [76-88 行](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/fabric_mem_d2d.cpp#L76-L88)），双方都注册完成后才 `Connect`。这与 u2-l3 的顺序合同一致：先注册 → 交换地址 → 建链。

**传输——本地前 1MB 写到对端后 1MB**：

- [examples/cpp/fabric_mem_d2d.cpp:90-110](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/fabric_mem_d2d.cpp#L90-L110)：`Transfer` 等待并读取对端地址文件，构造 `TransferOpDesc{local va, remote_addr + kWriteSize, kWriteSize}`——注意目标地址加了 1MB 偏移，即写到对端 buffer 的后半段；然后 `TransferSync(remote_engine, WRITE, {desc})` 单条同步下发。一个小细节：第 99 行注释写的是「512K」，但常量 `kWriteSize` 是 1MB（[29 行](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/fabric_mem_d2d.cpp#L29)），注释滞后于代码，读源码时以常量为准。

**校验与收尾**：

- [examples/cpp/fabric_mem_d2d.cpp:172-202](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/fabric_mem_d2d.cpp#L172-L202)：`TransferAndVerify` 写 `<local_engine>.done`、等 `<remote_engine>.done`，再 `VerifyBuffer` 校验本地后半段每个字节都等于对端 `device_id` 的低 8 位（对端初始化时以自己的 device_id 字节填充 buffer，见 [145 行](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/fabric_mem_d2d.cpp#L145) 的 `memset_s`）。双向都等待 `.done` 是因为每个进程既写别人也被别人写。
- [examples/cpp/fabric_mem_d2d.cpp:112-130](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/fabric_mem_d2d.cpp#L112-L130)：`Finalize` 的清理顺序——`Disconnect` → 逐个 `DeregisterMem` → `Finalize`，严格遵循 u2-l3 的约束「解注册前必须断开全部链路」。

**运行方式与版本约束**：

- [examples/cpp/README.md:211-226](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/README.md#L211-L226)：说明样例需成对运行、FabricMem 仅支持 A3 系列；并特别指出样例在 `AllocateBuffer` 直接使用 ACL VMM 接口分配内存再以 `MEM_DEVICE` 注册，该路径依赖 `aclrtMemRetainAllocationHandle`，**因此要求 HDK 26.0 及以上，不兼容 HDK 25.5**。两个终端分别执行 `./fabric_mem_d2d 0 127.0.0.1:16000 127.0.0.1:16001` 与 `./fabric_mem_d2d 1 127.0.0.1:16001 127.0.0.1:16000`。

#### 4.2.4 代码实践

1. **实践目标**：跑通 fabric_mem_d2d，并理解「参数即地址、文件即握手」的样例组织方式。
2. **操作步骤**：
   - 构建：`bash build.sh --examples`（u1-l2），产物在 `build_out` 下的样例目录。
   - 准备环境：`source ${HOME}/Ascend/cann/set_env.sh`；如需看引擎日志，另设 `export ASCEND_SLOG_PRINT_TO_STDOUT=1`。
   - 清理旧握手文件：`rm -f 127.0.0.1:16000* 127.0.0.1:16001*`（上次运行的残留文件会让 `WaitFile` 立即误判成功，这是本样例最常见的「假通过」来源）。
   - 两个终端分别执行：
     ```bash
     ./fabric_mem_d2d 0 127.0.0.1:16000 127.0.0.1:16001
     ./fabric_mem_d2d 1 127.0.0.1:16001 127.0.0.1:16000
     ```
3. **需要观察的现象**：两边依次输出 `Initialize success` → `RegisterMem success` → `Connect success` → `TransferSync write success` → `Verify success, value:1`（或对端 device_id）→ `run Sample end`；同时目录里出现 4 类文件（`127.0.0.1:1600x`、`.init_done`、`.done`）。
4. **预期结果**：两个进程均以 0 退出，校验通过。若 `Initialize` 失败先查 HDK/CANN 版本与灵衢网络依赖（[docs/zh/FabricMem.md:63-69](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/docs/zh/FabricMem.md#L63-L69)）；若卡在 `Wait file ... timeout`，多为残留文件或对端未启动。
5. 本实践需要 A3 环境与 HDK ≥ 26.0；无硬件时标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：样例为什么在 `Connect` 之前先等待对端的 `.init_done` 文件，而不是像 quickstart 那样只等地址文件？
**答案**：quickstart 中 server 是纯被动方，只要把注册地址发出去即可；而本样例双方对称、都要主动 `Connect` 并立即传输。若在对端 `RegisterMem` 完成前建链并下发 WRITE，对端地址尚未进入可导入状态，传输会失败。`.init_done` 保证「双方都完成注册」这个前置条件。

**练习 2**：把 `Transfer` 中的目标地址偏移 `kWriteSize` 去掉（写到对端前 1MB），校验还能通过吗？
**答案**：不能。`VerifyBuffer` 校验的是本地 buffer 的**后半段**（[155 行](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/fabric_mem_d2d.cpp#L155) `va + kWriteSize` 处取数据）。若去掉偏移，对端数据落在本地前半段，而后半段仍是本进程初始化时填充的本端 device_id 字节，与期望的对端 device_id 不相等，校验将报 `Verify failed`。

**练习 3**：样例的地址交换为什么可以用普通文件，而不需要像 `hixl_example_d2rd_multiproc` 那样用 TCP socket？
**答案**：两种都只是控制面握手手段，与数据面无关。文件方式实现最简单，但要求两个进程能访问同一文件系统（同机或共享存储）；socket 方式（u1-l5）适用于跨主机无共享盘的场景。另外 FabricMem 场景本身就是超节点内统一编址，文件交换的原始 VA 会在传输前由引擎经 `VirtualMemoryManager` 翻译成 fabric 视角地址（u5-l2），样例无需关心。

### 4.3 FabricMemStatistic：统计采集、通道台账与带宽 Dump

#### 4.3.1 概念说明

`FabricMemStatistic` 是 FabricMem 引擎内建的传输统计组件，**始终开启**（没有开关选项），随 `FabricMemEngine` 构造（成员变量）并由引擎启动周期 Dump。它回答的问题是：**每条通道（channel）传了多少次、多少字节、耗时多少、带宽多少**。

三个层次要分清：

1. **埋点**：传输服务在每次同步/异步传输完成后调用 `UpdateStats`，上报 `transfer_cost`（整次接口耗时）、`real_copy_cost`（纯拷贝耗时）、字节数、op_desc 条数。
2. **台账**：`unordered_map<channel_id, shared_ptr<FabricMemTransferStatisticInfo>>`，channel_id 带 `client:`/`server:` 前缀区分角色；全部计数用原子变量，无锁更新。
3. **输出**：一条 `HIXL_EVENT` 汇总日志，含平均/最大/最小带宽；由 `PeriodicTask` 周期触发，周期默认 **30 分钟**。

带宽计算公式（cost 单位为微秒）：

\[ \text{bandwidth (GiB/s)} = \frac{\text{total\_bytes} \times 10^6}{\text{total\_cost}\,\mu s \times 2^{30}} \]

#### 4.3.2 核心流程

```text
建链时   ChannelManager::CreateChannel → statistic_->RegisterChannel("client:<remote>")
传输时   HostTransferService::TransferSync 完成
           ├─ transfer_cost  = start → 返回时刻
           ├─ real_copy_cost = real_copy_start → 返回时刻
           └─ UpdateStats(...) → UpdateCostsDirect（原子累加 + max CAS 更新）
                 └─ 累计次数超过 kResetTimes(100000) → 整表 Reset（防长期均值失真）
周期性   PeriodicTask 每 30 分钟 → Dump()
           └─ 遍历台账 → TransferSummary::Accumulate（逐通道算带宽、取 max/min/avg）
                 └─ HIXL_EVENT 输出汇总（无活跃通道则静默跳过）
断链时   RemoveStatisticChannel("client:<remote>")
引擎退出 StopPeriodicDump（只停任务，不额外 Dump 一次）
```

#### 4.3.3 源码精读

**统计组件的启动与停止**：

- [src/hixl/engine/fabric_mem_engine.cc:98-99](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/fabric_mem_engine.cc#L98-L99)：`InitFabricMem` 中调用 `fabric_mem_statistic_.StartPeriodicDump()`，统计随引擎初始化自动生效。
- [src/hixl/engine/fabric_mem_engine.cc:344](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/fabric_mem_engine.cc#L344)：引擎清理时 `StopPeriodicDump()`；析构函数也会兜底停止（[fabric_mem_statistic.cc:32-34](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/fabric_mem/fabric_mem_statistic.cc#L32-L34)）。

**传输埋点——每次传输都会留下两个耗时**：

- [src/hixl/fabric_mem/fabric_mem_host_transfer_service.cc:92-100](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/fabric_mem/fabric_mem_host_transfer_service.cc#L92-L100)：host 路径 `TransferSync` 收尾处：`transfer_cost` 覆盖从进入接口到返回的全程（含取槽位、等流），`real_copy_cost` 只覆盖实际下发拷贝之后的时间；二者都交给 `UpdateStats`，同时打出一条 `HIXL_LOGI` **单次传输耗时日志**——这是短跑场景里最实用的观测点。AICPU 路径有同样的埋点（`fabric_mem_aicpu_transfer_service.cc:176、358`）。
- [src/hixl/fabric_mem/fabric_mem_transfer_service.cc:288-301](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/fabric_mem/fabric_mem_transfer_service.cc#L288-L301)：`UpdateStats` 的分派逻辑——若 `TransferContext` 里带了预取的 `stat_info`（建链时通过 `BuildTransferContext` 拿到的 shared_ptr，见 [fabric_mem_channel_manager.cc:364-368](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/fabric_mem/fabric_mem_channel_manager.cc#L364-L368)）则走免查表的 `UpdateCostsDirect`，否则按 channel_id 查表。这是一处典型的小优化：传输热路径避免每次都锁 map 查找。

**通道台账——前缀区分角色，建链注册、断链摘除**：

- [src/hixl/fabric_mem/fabric_mem_channel_manager.cc:94](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/fabric_mem/fabric_mem_channel_manager.cc#L94)：建链成功即 `RegisterChannel(GetClientStatisticChannelId(remote))`；[280 行](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/fabric_mem/fabric_mem_channel_manager.cc#L280) 断链时摘除。
- [src/hixl/fabric_mem/fabric_mem_statistic.cc:36-46](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/fabric_mem/fabric_mem_statistic.cc#L36-L46)：channel_id 的生成规则——`"client:"` 或 `"server:"` 前缀 + 原始 channel_id（常量定义在 [statistic_utils.h:24-25](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/common/statistic_utils.h#L24-L25)）。

**原子更新与自动复位**：

- [src/hixl/fabric_mem/fabric_mem_statistic.cc:57-64](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/fabric_mem/fabric_mem_statistic.cc#L57-L64)：`UpdateCost` 用 `fetch_add` 累加次数与总耗时，用 `compare_exchange_weak` 循环更新最大值——无锁热路径的标准写法。
- [src/hixl/fabric_mem/fabric_mem_statistic.cc:102-111](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/fabric_mem/fabric_mem_statistic.cc#L102-L111)：`UpdateCostsDirect` 累计全部指标，并在次数超过 `kResetTimes`（100000，[statistic_utils.h:19](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/common/statistic_utils.h#L19)）后整表复位——防止长期运行时早期慢传输永久拖低均值（滑动窗口思想的简化版）。

**带宽计算与汇总输出**：

- [src/hixl/common/statistic_utils.h:27-32](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/common/statistic_utils.h#L27-L32)：`GetBandwidthGbps`，即本节开头的公式；分母为 0 时返回 0 防除零。
- [src/hixl/common/statistic_utils.h:49-82](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/common/statistic_utils.h#L49-L82)：`TransferSummary::Accumulate` 逐通道累计次数/字节/描述符数，并维护最大/最小带宽（最小带宽还记录 channel_id，方便定位慢通道）。
- [src/hixl/fabric_mem/fabric_mem_statistic.cc:126-147](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/fabric_mem/fabric_mem_statistic.cc#L126-L147)：`Dump()` 在共享锁下遍历台账聚合，活跃通道数为 0 时直接返回（不刷无意义日志），否则用 `HIXL_EVENT` 输出一条汇总：传输次数、平均单条大小（KB）、最大/最小/平均带宽（GiB/s）、最小带宽通道。
- [src/hixl/fabric_mem/fabric_mem_statistic.cc:149-155](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/fabric_mem/fabric_mem_statistic.cc#L149-L155)：`StartPeriodicDump` 用 `PeriodicTask`（u3-l4）每 `kStatisticTimerPeriodMs` 触发一次 `Dump`——该常量为 30×60×1000 毫秒即 **30 分钟**（[statistic_utils.h:20](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/common/statistic_utils.h#L20)，正确链接见 [statistic_utils.h:20](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/common/statistic_utils.h#L20)）。

**日志出口**：

- [src/hixl/common/hixl_log.h:65-72](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/common/hixl_log.h#L65-L72)：`HIXL_EVENT` 基于 `dlog_info`，同时写事件日志与模块日志；[36-42 行](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/common/hixl_log.h#L36-L42) 的 `HixlLogPrintStdout` 表明：设 `ASCEND_SLOG_PRINT_TO_STDOUT=1` 可把日志打到终端，便于样例场景直接观察。

一个重要的工程事实：**周期 Dump 默认 30 分钟一次，而 d2d 样例几秒就结束了**——所以短跑时几乎看不到汇总日志，实际观测靠的是每次传输的 `HIXL_LOGI` 单条耗时日志（4.3.3 第一条引用），带宽用公式手算即可。

#### 4.3.4 代码实践

1. **实践目标**：在不开统计开关（因为也没有开关）的前提下，拿到一次传输的带宽数据。
2. **操作步骤**：
   - 设 `export ASCEND_SLOG_PRINT_TO_STDOUT=1` 后重跑 4.2.4 的样例；
   - 在终端输出中查找形如 `Fabric mem transfer cost:%lu us, real copy:%lu us, channel:%s` 的行（由 [fabric_mem_host_transfer_service.cc:99](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/fabric_mem/fabric_mem_host_transfer_service.cc#L99) 打出；若走默认 AICPU 路径，找 AICPU 服务中对应的耗时日志）；
   - 手算带宽：本次传输 1MB（`kWriteSize`），带宽 = \( 1048576 \times 10^6 / (\text{transfer\_cost} \times 2^{30}) \) GiB/s；
   - 进阶：把样例 `Transfer` 改成循环下发同一条 desc 若干次（示例代码，非项目原有）：
     ```cpp
     // 示例代码：循环下发，观察耗时稳定性
     for (int i = 0; i < 100; ++i) {
       auto ret = hixl_engine.TransferSync(remote_engine, WRITE, {desc});
       if (ret != SUCCESS) { break; }
     }
     ```
     累计 100 次的 cost 取平均，可显著降低单次调度抖动的影响。
3. **需要观察的现象**：每次（或每轮）传输后出现一条耗时日志；`transfer_cost ≥ real_copy_cost`（前者含取槽位、上下文恢复等额外开销）。
4. **预期结果**：记录一组 `(transfer_cost, real_copy_cost, 带宽)` 三元组。若想让周期汇总日志真正出现，可把循环次数加大并保持进程存活 30 分钟以上（或将 [statistic_utils.h:20](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/common/statistic_utils.h#L20) 的周期改小后重新编译——仅用于本地实验，勿提交）。
5. 需要 A3 环境；无硬件时标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `transfer_cost` 和 `real_copy_cost` 要分开统计？
**答案**：`transfer_cost` 是接口全程耗时，`real_copy_cost` 只计数据搬运段。二者之差暴露了非搬运开销（获取 AsyncSlot、恢复 RT 上下文、等流完成等）。若差值占比大，说明瓶颈在调度而非带宽——这是用 FabricMem 做性能调优时最直接的判据。

**练习 2**：统计累计到 `kResetTimes`（100000 次）就整体复位的目的是什么？
**答案**：长期运行的服务中，早期因建链、预热造成的慢传输会把总耗时垫高，导致平均带宽长期失真。周期性复位相当于定期清零重新统计，让汇总反映「近期」行为。代价是复位瞬间丢弃历史累计，属于简单但有效的折中。

**练习 3**：`Dump()` 输出里为什么专门记录「最小带宽通道」的 channel_id，而不单独记录最大带宽通道？
**答案**：性能分析中最大带宽通道通常没有行动价值（已经够快），而最小带宽通道是木桶短板，指明下一步排查对象（该远端的链路质量、内存 NUMA 归属等）。channel_id 带 `client:`/`server:` 前缀，还能直接区分是本端发起还是对端发起的方向。

## 5. 综合实践

**任务：FabricMem 与普通路径的 1MB 传输对比报告。**

在 A3 环境（HDK ≥ 26.0）完成以下步骤，产出一份对比报告：

1. **跑 FabricMem 路径**：按 4.2.4 跑通 `fabric_mem_d2d`，按 4.3.4 记录 1MB WRITE 的 `transfer_cost`/`real_copy_cost`，并用公式算出带宽（建议循环 100 次取平均）。
2. **跑普通路径**：以相同数据量运行 `hixl_example_d2rd_multiproc`（u1-l5 讲过的多进程样例，`--protocol=hccs:device` 或环境支持的协议），同样记录耗时并算带宽。若两者数据量不便对齐，可各自按「字节数 ÷ 耗时」归一成带宽再比。
3. **对照参考数据**：与 [docs/zh/FabricMem.md:75-77](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/docs/zh/FabricMem.md#L75-L77) 指向的 `benchmarks/performance.md` 中的官方数据对照，分析你的测量与基准的差距来源（单次 1MB 太小、未预热、协议不同等）。
4. **写结论**：报告中至少回答——FabricMem 模式相比普通路径在你的环境下带宽提升多少？`transfer_cost - real_copy_cost` 的差值在两条路径上各占多少？结合 u5-l1 的设计文档说明为什么 FabricMem 在 D2RH 类场景收益最大（注意：d2d 场景本身不是 FabricMem 的最大价值点，D2RH/RH2D 才是）。

无硬件环境时，退化为源码阅读版报告：整理「一次 `TransferSync` 从样例到统计埋点的完整调用链」（样例 `Transfer` → `Hixl::TransferSync` → `FabricMemEngine` → 传输服务 → `UpdateStats`），并标注每层所在文件与行号，全部标注「待本地验证」。

## 6. 本讲小结

- 使能 FabricMem 只需一个选项：options 中 `EnableUseFabricMem="1"`，引擎即被 `EngineFactory` 第一优先级分支选中；`FabricMemConfig` 提供 `capacity_tb`/`start_address_tb`/`task_stream_num`/`max_stream_num`/`enable_aicpu_unfold` 五类可调参数，默认走 AICPU 路径。
- `fabric_mem_d2d` 是对称双进程样例：用户自己用 ACL VMM 三步接口分配 device 内存再以 `MEM_DEVICE` 注册（因此要求 HDK ≥ 26.0），用 engine 字符串命名的文件交换地址并以 `.init_done`/`.done` 做阶段握手，双方各把本地前 1MB 单边 WRITE 到对端后 1MB 并以字节填充值校验。
- `FabricMemStatistic` 始终开启、无用户开关：建链注册通道、每次传输原子累加 `transfer_cost`/`real_copy_cost`/字节/op 数、超 10 万次自动复位，`PeriodicTask` 每 30 分钟输出一条含最大/最小/平均带宽的 `HIXL_EVENT` 汇总。
- 短跑场景的实际观测手段是每次传输的 `HIXL_LOGI` 耗时日志（配 `ASCEND_SLOG_PRINT_TO_STDOUT=1` 直接看终端），带宽按 \( \text{bytes} \times 10^6 / (\text{cost}\,\mu s \times 2^{30}) \) GiB/s 手算。
- 样例的清理顺序（Disconnect → DeregisterMem → Finalize）与全手册强调的顺序合同完全一致；残留握手文件是样例「假通过」的主要来源。

## 7. 下一步学习建议

- **单元七（u7）**：Python 绑定与端到端样例——如果你想在 Python 侧复用本讲的测量方法，先读 `src/python/hixl_py/hixl_py.cc`。
- **性能专题（u8-l1）**：本讲的 1MB 手测只是入门，系统化的带宽/时延测量看 `benchmarks/run_all_bench.sh` 与 `benchmarks/performance.md`，把本讲的综合实践升级为标准基准数据。
- **源码延伸阅读**：`fabric_mem_aicpu_transfer_service.cc` 中 `UpdateStats` 的异步埋点（275、358 行附近）与本讲 host 路径对照，体会两条路径统计语义的一致性；以及 `FabricMemEngine::Finalize`（[fabric_mem_engine.cc:344](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/fabric_mem_engine.cc#L344) 附近）的完整清理顺序。
