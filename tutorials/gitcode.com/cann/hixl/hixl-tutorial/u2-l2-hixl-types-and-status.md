# u2-l2 核心数据结构与错误码

## 1. 本讲目标

学完本讲，你应该能够：

1. 逐字段说出 `MemType`、`MemDesc`、`TransferOp`、`TransferOpDesc`、`TransferStatus`、`AsyncConnectStatus` 等数据结构的含义与用途。
2. 理解 `Status`（`uint32_t`）错误码的取值规则、可恢复性，以及「成功:SUCCESS，失败:其它」的判断方式。
3. 理解 `AscendString`、`MemHandle`、`TransferReq` 三个基础类型的来源与用法。
4. 会用静态接口 `Hixl::GetCapability` 查询 `FeatureType` 能力，并能写出一个只包含 `hixl.h` 头文件的最小程序来观察返回值。

本讲是单元二的「字典课」：u2-l1 讲了 `Hixl` 类的骨架和生命周期，本讲把骨架上流转的「名词」——所有公开数据结构与错误码——一次性讲清楚，后续 u2-l3（内存注册）、u2-l4（建链）、u2-l5（传输）都会反复引用本讲的概念。

## 2. 前置知识

- **`uintptr_t` 与裸地址**：`hixl_types.h` 里大量使用 `uintptr_t`（一个足以存放指针地址的无符号整数）来传递设备/主机内存地址。HIXL 是零拷贝库，传输时直接搬运地址指向的数据，所以 API 层看到的都是「地址 + 长度」，而不是 buffer 对象。
- **句柄（handle）模式**：`MemHandle` 和 `TransferReq` 都是 `void *`。库内部把它们指向真实对象，用户只负责持有和回传，不解引用。这是 C++ 库隔离内部实现的常用手段（和 u2-l1 讲的 Pimpl 是同一个思想）。
- **枚举类的两种写法**：`enum MemType`（无 class，可隐式转 int，值默认从 0 开始）与 `enum class TransferStatus`（强类型枚举，必须写 `TransferStatus::WAITING`）。hixl_types.h 中两种风格并存，新类型多用 `enum class`。
- **`constexpr` 常量**：错误码（如 `SUCCESS`、`PARAM_INVALID`）是 `constexpr Status` 常量而非宏，编译期即可确定，类型安全。
- **ABI 预留字段**：本讲多个结构体带 `uint8_t reserved[N]` 字段。这些是给未来扩展预留的空间，使结构体总大小固定（如 `TransferResult` 凑成 128 字节量级），新增字段时不破坏二进制兼容。用户代码**不应**读写 reserved。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [include/hixl/hixl_types.h](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/hixl/hixl_types.h) | 本讲主角：HIXL 全部公开数据结构、错误码、选项键、能力枚举的定义，仅 110 行 |
| [include/hixl/hixl.h](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/hixl/hixl.h) | `Hixl` 类接口，所有接口的返回值/出入参都用 hixl_types.h 中的类型 |
| [src/hixl/engine/hixl_impl.cc](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_impl.cc) | `Hixl::GetCapability` 的实现（本讲实践任务的分析对象） |
| [src/hixl/common/hixl_checker.h](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/common/hixl_checker.h) | 内部统一的错误检查宏，展示 Status 在源码内部如何被判断 |
| [docs/zh/api/cpp/HIXL-data-structure.md](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/docs/zh/api/cpp/HIXL-data-structure.md) | 官方数据结构文档，与本讲内容互为对照 |
| [docs/zh/api/cpp/HIXL-error-code.md](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/docs/zh/api/cpp/HIXL-error-code.md) | 官方错误码文档，含每个错误码的可恢复性与处理建议 |

另外注意：`AscendString` 并非本仓库定义，而是 `using AscendString = ge::AscendString;`，来自 CANN 的 ge_common 头文件（[include/hixl/hixl_types.h:L15](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/hixl/hixl_types.h#L15)），编译时需要 CANN 环境提供该头文件。

## 4. 核心概念与源码讲解

本讲拆为四个最小模块：

1. **基础类型层**：`Status`、`AscendString`、句柄类型与选项键常量。
2. **内存与传输描述**：`MemType`/`MemDesc` 与 `TransferOp`/`TransferOpDesc` 及传输状态族。
3. **错误码体系**：9 个错误码的含义、可恢复性与判断方式。
4. **能力查询**：`FeatureType` 与 `GetCapability`。

### 4.1 基础类型层：Status、AscendString 与句柄

#### 4.1.1 概念说明

任何 `Hixl` 接口被调用时，信息在两个方向流动：入参（引擎标识、地址、选项）和出参（结果状态、句柄、查询结果）。hixl_types.h 开头的几行 `using` 定义了这些流动的「载体」。理解它们的关键是：HIXL 刻意让公开层只依赖极少的类型——一个整型状态码、一个字符串类、两个 `void *` 句柄——从而保证头文件的依赖面最小。

#### 4.1.2 核心流程

用户视角下的类型流：

```text
字符串参数 ──► AscendString（local_engine、remote_engine、选项键值）
返回结果   ──► Status（uint32_t，0 为成功）
注册内存   ──► MemDesc 进，MemHandle 出
异步传输   ──► TransferArgs 进，TransferReq 出（再用于状态查询）
```

#### 4.1.3 源码精读

基础别名定义在文件最顶部：

- [include/hixl/hixl_types.h:L24-L26](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/hixl/hixl_types.h#L24-L26)：`Status` 是 `uint32_t` 别名，`AscendString` 复用 ge_common 的字符串类，`TransferReq` 是 `void *`。错误码因此是普通整数，可以直接比较、打印。

紧随其后的是选项键常量：

- [include/hixl/hixl_types.h:L28-L35](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/hixl/hixl_types.h#L28-L35)：`OPTION_ENABLE_USE_FABRIC_MEM`、`OPTION_RDMA_TRAFFIC_CLASS`、`OPTION_RDMA_SERVICE_LEVEL`、`OPTION_BUFFER_POOL`、`OPTION_GLOBAL_RESOURCE_CONFIG`、`OPTION_AUTO_CONNECT`、`OPTION_LOCAL_COMM_RES`。它们是 `Initialize` 第二个参数 `std::map<AscendString, AscendString>` 的合法键名（u1-l5 讲过的 `AutoConnect`、`LocalCommRes` 等就定义在这里）。

`MemHandle` 单独定义在结构体区：

- [include/hixl/hixl_types.h:L57](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/hixl/hixl_types.h#L57)：`using MemHandle = void *;`，由 `RegisterMem` 输出、`DeregisterMem` 消费。

#### 4.1.4 代码实践

1. **实践目标**：确认「基础类型 = 极简载体」这一设计事实。
2. **操作步骤**：打开 hixl_types.h，把 L24-L26、L57 的四个别名抄成一张表，标注每个类型「由哪个接口产生、被哪个接口消费」。可对照 [include/hixl/hixl.h:L54-L67](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/hixl/hixl.h#L54-L67)（RegisterMem/DeregisterMem）与 [include/hixl/hixl.h:L136-L146](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/hixl/hixl.h#L136-L146)（TransferAsync/GetTransferStatus）核对。
3. **需要观察的现象**：所有句柄都是 `void *`，公开头文件中找不到任何内部类的前置声明（除 `HixlImpl` 外）。
4. **预期结果**：得到一张 4 行的类型流转表，验证「用户只持有、不解引用」的句柄规则。
5. 本实践为纯阅读型，无需运行。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Status` 用 `constexpr` 常量而不用 `#define` 宏或 `enum`？

**答案**：`constexpr` 常量有真实类型（`uint32_t`）、参与编译期类型检查、可放入命名空间（这里在 `namespace hixl` 内，不会污染全局），且能被调试器直接显示符号名；宏没有类型且全局生效，普通 `enum` 会隐式转换为 int、不利于表达 50 万级的错误码值。

**练习 2**：`AscendString` 为什么不直接用 `std::string`？

**答案**：`AscendString` 是 CANN 生态（ge_common）统一的字符串类型，HIXL 通过 `using` 复用它以与 CANN 其它组件（如 LLM-DataDist 所基于的 ge 接口风格）保持 ABI 与依赖一致；同时它面向 C++ 接口导出场景设计，避免把 libstdc++ 特定实现细节写进公开 ABI。

### 4.2 内存与传输描述：从 MemDesc 到 TransferResult

#### 4.2.1 概念说明

这是本讲的核心模块。HIXL 的两类「描述子」分别回答两个问题：

- `MemDesc` + `MemType`：**哪块内存**要被注册（地址、长度、位于设备还是主机）。
- `TransferOpDesc` + `TransferOp`：**怎么搬**（本地地址、远端地址、长度；方向是 READ 还是 WRITE）。

围绕异步传输还有一族辅助结构：`TransferArgs`（下发时的可选参数，携带用户自定义指针）、`GetTransferStatusArgs`（批量查询的过滤参数）、`TransferResult`（每个请求的查询结果）。它们共同构成 u1-l3 见过的调用序列中「注册 → 传输 → 查询」三步的数据载体。

#### 4.2.2 核心流程

一次完整传输中各结构的生命周期：

```text
MemDesc{addr,len} + MemType ──RegisterMem──► MemHandle
                                                    │（地址需通过控制面告诉对端）
                                                    ▼
TransferOpDesc{local_addr, remote_addr, len} ──┐
TransferOp (READ/WRITE)                        ├─TransferAsync──► TransferReq
TransferArgs{user_data}                        ┘                     │
                                                                     ▼
GetTransferStatusArgs{max_query_count, skip_waiting} ──► TransferResult{req, user_data, status}
```

两个正交维度（承接 u1-l3 的结论）：

- **内存类型维度**：`MemType` 描述内存形态（`MEM_DEVICE`=昇腾设备内存，`MEM_HOST`=主机锁页内存），组合出 D2D/D2H/H2D/D2rH 等路径。
- **方向维度**：`TransferOp` 按发起方视角定义——`READ` 把远端内存读回本地，`WRITE` 把本地内存写到远端。

#### 4.2.3 源码精读

内存侧：

- [include/hixl/hixl_types.h:L59](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/hixl/hixl_types.h#L59)：`enum MemType { MEM_DEVICE, MEM_HOST };`，普通枚举，值从 0 开始，可作为数组下标使用。
- [include/hixl/hixl_types.h:L63-L67](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/hixl/hixl_types.h#L63-L67)：`MemDesc` 仅 `addr` + `len` 两个有效字段加 128 字节 reserved。注册内存时由用户填充，`RegisterMem` 据此把内存登记进引擎（内部流程见 u2-l3）。

传输侧：

- [include/hixl/hixl_types.h:L61](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/hixl/hixl_types.h#L61)：`enum TransferOp { READ, WRITE };`。
- [include/hixl/hixl_types.h:L69-L73](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/hixl/hixl_types.h#L69-L73)：`TransferOpDesc` 是最纯粹的三元组：`local_addr`、`remote_addr`、`len`。一次 `TransferSync`/`TransferAsync` 接受 `std::vector<TransferOpDesc>`，即批量下发多个不连续片段（u1-l5 的 d2rd 样例一次下发了 512 条）。
- [include/hixl/hixl_types.h:L74](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/hixl/hixl_types.h#L74)：`TransferStatus` 四态：`WAITING`（已下发未完成）、`COMPLETED`、`TIMEOUT`、`FAILED`。注意官方文档标注 TIMEOUT **暂不支持**（[docs/zh/api/cpp/HIXL-data-structure.md:L97-L103](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/docs/zh/api/cpp/HIXL-data-structure.md#L97-L103)），当前实际只会观察到 WAITING/COMPLETED/FAILED。
- [include/hixl/hixl_types.h:L76-L79](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/hixl/hixl_types.h#L76-L79)：`TransferArgs` 唯一有效字段是 `user_data`，是用户挂在请求上的任意指针，查询时会原样带回 `TransferResult`，用于把「完成事件」映射回业务对象。
- [include/hixl/hixl_types.h:L81-L92](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/hixl/hixl_types.h#L81-L92)：`GetTransferStatusArgs` 支持两个过滤——`max_query_count` 限制一次最多取回多少条结果（默认 `UINT32_MAX` 即全部），`skip_waiting` 跳过仍在等待的请求；`TransferResult` 回带 `req`、`user_data`、`status` 三元组。

通知与链路状态（同属传输族结构）：

- [include/hixl/hixl_types.h:L94-L97](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/hixl/hixl_types.h#L94-L97)：`NotifyDesc` 由 `name` 与 `notify_msg` 两个 `AscendString` 组成，供 `SendNotify`/`GetNotifies` 使用。
- [include/hixl/hixl_types.h:L99-L107](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/hixl/hixl_types.h#L99-L107)：`AsyncConnectStatus` 七态，覆盖异步建链（NOT_CONNECT → CONNECT_PENDING → CONNECTING → CONNECTED/CONNECT_FAILED）与异步断链（DISCONNECT_PENDING → DISCONNECTING）两条轨迹，是 u2-l4 将精读的状态机。

#### 4.2.4 代码实践

1. **实践目标**：用 sizeof 与字段填充验证对结构体的理解，不依赖任何硬件。
2. **操作步骤**：写一个只 `#include "hixl/hixl.h"` 的小程序（完整示例见本讲第 5 节综合实践的前半部分），打印 `sizeof(MemDesc)`、`sizeof(TransferOpDesc)`、`sizeof(TransferResult)`，并构造 3 个不同 `len` 的 `TransferOpDesc` 放进 vector。
3. **需要观察的现象**：`MemDesc` 因 reserved 字段远大于 `TransferOpDesc`（后者无 reserved，仅 3 个字段）。
4. **预期结果**：`sizeof(TransferOpDesc)` = 24（64 位平台：8+8+8）；`MemDesc` 与 `TransferResult` 为 144/128 字节量级。具体数值待本地验证（依赖编译器与平台）。
5. 若无 CANN 头文件环境，可临时手工抄录结构体定义验证 sizeof，但正式编译需 CANN 环境（见第 5 节说明）。

#### 4.2.5 小练习与答案

**练习 1**：`TransferOpDesc` 中 `local_addr` 和 `remote_addr` 在 READ 和 WRITE 两种操作下语义有何变化？

**答案**：字段本身不变，变的是数据流向。`WRITE`：数据从 `local_addr` 流向 `remote_addr`（本地为源）；`READ`：数据从 `remote_addr` 流向 `local_addr`（本地为目的）。两个地址都必须是**已注册**内存的有效地址，且 `len` 不得超过注册长度。

**练习 2**：为什么 `TransferArgs` 要设计 `user_data` 挂钩？

**答案**：异步传输下发后，用户拿到的是不透明的 `TransferReq`。批量查询（`GetTransferStatus` 的 vector 版本）返回 `TransferResult` 时，靠 `user_data` 把请求关联回业务上下文（如指向某个 batch 的元数据），无需用户自己维护 req→业务的映射表。

**练习 3**：`MemType` 用普通 `enum` 而 `TransferStatus` 用 `enum class`，混用两种风格有什么风险？

**答案**：普通 `enum` 可隐式转 int，若两个枚举都有 0 值成员（如 `MEM_DEVICE` 和 `READ` 都是 0），传错参数编译器不会报错；`enum class` 强类型则会在编译期拦截。读写旧代码时应格外注意普通枚举的隐式转换陷阱。

### 4.3 错误码体系：9 个值与两类可恢复性

#### 4.3.1 概念说明

HIXL 所有接口的返回值都是 `Status`（`uint32_t`），约定为「成功:SUCCESS，失败:其它」。判断成功只有一种正确写法：

```cpp
if (status == hixl::SUCCESS) { ... }        // 或
if (status != hixl::SUCCESS) { ... }
```

**不要**写 `if (!status)` 或假设错误码区间连续。错误码的关键设计是「可恢复性」：有的错误（参数错误）改掉参数重试即可；有的错误（TIMEOUT、FAILED）意味着现场已经不可信，应当保留日志并停止使用该实例。

#### 4.3.2 核心流程

按官方文档（[docs/zh/api/cpp/HIXL-error-code.md:L20-L30](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/docs/zh/api/cpp/HIXL-error-code.md#L20-L30)）整理的决策表：

| 错误码 | 值 | 含义 | 可恢复 | 建议处理 |
| --- | --- | --- | --- | --- |
| SUCCESS | 0 | 成功 | — | — |
| PARAM_INVALID | 103900 | 参数错误 | 是 | 根据日志定位非法参数 |
| TIMEOUT | 103901 | 处理超时 | 否 | 保留现场，收集 Host/Device 日志 |
| NOT_CONNECTED | 103902 | 尚未建链 | 是 | 排查建链时序（先 Connect 再 Transfer） |
| ALREADY_CONNECTED | 103903 | 已建链 | 是 | 排查是否重复 Connect |
| NOTIFY_FAILED | 103904 | 通知失败 | 否 | 预留错误码，当前不会返回 |
| UNSUPPORTED | 103905 | 不支持的参数或接口 | 是 | 换用受支持的参数/接口 |
| FAILED | 503900 | 通用失败 | 否 | 保留现场，收集日志 |
| RESOURCE_EXHAUSTED | 203900 | 资源耗尽（当前仅 stream 资源） | 是 | 等资源释放后重试 |

编码规律（观察值可得）：`1039xx` 段是「可定位、多可恢复」的 HIXL 业务错误；`203900` 属资源类；`503900` 是兜底的通用失败。对照 u1-l4 讲过的 LLM-DataDist 错误码（基于 `ge::Status`、前缀 `LLM_DATA_DIST`），两套体系各自独立，由适配层转换。

#### 4.3.3 源码精读

- [include/hixl/hixl_types.h:L37-L46](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/hixl/hixl_types.h#L37-L46)：9 个 `constexpr Status` 错误码常量的定义处，每个接口的注释都写着「成功:SUCCESS, 失败:其它」（如 [include/hixl/hixl.h:L44](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/hixl/hixl.h#L44)）。
- [src/hixl/common/hixl_checker.h:L44-L47](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/common/hixl_checker.h#L44-L47)：`HIXL_CHK_STATUS_RET` 宏——内部实现统一用 `!= hixl::SUCCESS` 判断失败并记录日志后返回。这说明「与 SUCCESS 严格比较」是仓库内外的共同约定（u8-l5 将系统展开 checker 体系）。

一个真实例子：u2-l1 讲过的 `GetNotifies` 外壳在 `impl_` 为空时返回 `FAILED`：

- [src/hixl/engine/hixl_impl.cc:L411-L414](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_impl.cc#L411-L414)：门卫检查失败即返回 `FAILED` 并打日志——这就是「不可恢复错误伴随日志」的典型落地。

#### 4.3.4 代码实践

1. **实践目标**：建立错误码到处置动作的肌肉记忆。
2. **操作步骤**：在 u1-l3 跑通的 quickstart 样例上做三个破坏性实验：(a) 把 `Connect` 注释掉直接 `TransferSync`；(b) `Connect` 成功后再 `Connect` 一次同一远端；(c) 把 `Initialize` 的 options 里塞入非法键值。
3. **需要观察的现象**：分别观察到 `NOT_CONNECTED`（103902）、`ALREADY_CONNECTED`（103903）、`PARAM_INVALID`（103900，来自 u2-l1 讲过的选项解析失败路径）。
4. **预期结果**：打印出的错误码与本讲表格一一对应；同时日志中能找到对应的中文/英文错误描述。
5. 需要两台可互通 device 的环境；无硬件时本实践**待本地验证**，可退化为阅读 `hixl_impl.cc` 中 `PARAM_INVALID` 的返回点做静态对照。

#### 4.3.5 小练习与答案

**练习 1**：调用方写 `if (status)` 判断成功，哪里会出错？

**答案**：`SUCCESS` 恰好是 0，所以 `if (status)` 在成功时为 false——碰巧能工作。但这依赖 0 值假设且语义颠倒，一旦错误码定义调整或代码被复制到 SUCCESS 非 0 的子系统（如某些 ge 错误码体系）就会出错。统一写 `== hixl::SUCCESS` 才符合接口契约。

**练习 2**：收到 `RESOURCE_EXHAUSTED` 后正确的重试策略是什么？

**答案**：该错误当前仅表示 stream 资源耗尽，属可恢复错误。应暂停下发新传输、等待在途请求完成释放资源后重试，而不是销毁重建 Hixl 实例。

**练习 3**：`NOTIFY_FAILED` 为什么文档说「暂不会返回」却仍保留？

**答案**：它是预留错误码，为将来通知机制的失败路径占位；提前定义可避免后续新增错误码时打乱既有取值约定，也让用户代码可以先行处理。

### 4.4 能力查询：FeatureType 与 GetCapability

#### 4.4.1 概念说明

不同芯片代际、不同传输链路对特性的支持不同（u1-l1 讲过 A2/A3 差异）。HIXL 用「能力查询」模式把这种差异显式化：程序在运行时调用静态接口 `Hixl::GetCapability(FeatureType, int32_t &value)`，按返回值决定走哪条代码路径，而不是硬编码假设。注意两点：它是**静态**成员，无需构造 Hixl、无需 Initialize 即可调用；未知特性不报错，而是返回 `FEATURE_NOT_SUPPORTED`（0），保证向前兼容——将来库新增特性时，旧程序查询新值得到 0 而非崩溃。

#### 4.4.2 核心流程

```text
用户程序: value 初始化为 FEATURE_NOT_SUPPORTED
          │
          ▼
Hixl::GetCapability(feature_type, value)
          │
          ├─ feature_type < 0        → 返回 PARAM_INVALID（value 不变）
          ├─ AUTO_CONNECT / CLIENT_SERVER_COMM → value = 1 (FEATURE_SUPPORTED)，返回 SUCCESS
          └─ 其它（含未来新增未识别值）→ value = 0 (FEATURE_NOT_SUPPORTED)，返回 SUCCESS
```

#### 4.4.3 源码精读

- [include/hixl/hixl_types.h:L48-L55](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/hixl/hixl_types.h#L48-L55)：`FeatureType` 枚举带一条重要注释——**值必须显式赋值、新能力只允许追加在末尾**。因为 value 的语义是「按数值分派」，中间插值或复用旧值都会破坏已发布程序的行为。`AUTO_CONNECT = 0`、`CLIENT_SERVER_COMM = 1`，分别对应 Initialize 的 `AutoConnect` 选项与「Server 监听/Client 发起」的建链能力（见 [docs/zh/api/cpp/HIXL-data-structure.md:L152-L155](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/docs/zh/api/cpp/HIXL-data-structure.md#L152-L155)）。
- [include/hixl/hixl.h:L173-L178](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/hixl/hixl.h#L173-L178)：`GetCapability` 的公开声明，`static` 修饰，注释明确「1=支持，0=不支持（含未知特性）」「参数非法:PARAM_INVALID」。
- [src/hixl/engine/hixl_impl.cc:L417-L430](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_impl.cc#L417-L430)：实现本体。负值走 `PARAM_INVALID`；两个已定义特性命中 `FEATURE_SUPPORTED`；`default` 分支落到 `FEATURE_NOT_SUPPORTED`——这就是「未知特性安全返回 0」的代码依据。与 u2-l1 讲的其它接口不同，它不经过 `impl_` 门卫，因为它不触碰任何引擎状态。

顺带一提，Python 绑定也导出了该能力（[src/python/hixl_py/hixl_py.cc:L214-L218](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/python/hixl_py/hixl_py.cc#L214-L218)），返回 `(status, value)` 二元组，行为与 C++ 一致。

#### 4.4.4 代码实践：头文件最小程序 + GetCapability 观察

这是本讲的主实践，完整任务如下。

1. **实践目标**：写一个只包含 `hixl/hixl.h` 的程序，构造多种 `TransferOpDesc`，并逐一查询 `FeatureType`，验证本讲 4.4.2 的分派逻辑。

2. **操作步骤**：

   (a) 新建 `capability_probe.cpp`（**示例代码**，非仓库原有文件）：

   ```cpp
   // 示例代码：仅依赖公开头文件，无需 Initialize，无硬件也可编译运行
   #include <cstdio>
   #include <vector>
   #include "hixl/hixl.h"

   int main() {
     // 1. 构造各种 TransferOpDesc（合法与刻意越界的观察点）
     std::vector<hixl::TransferOpDesc> ops = {
       {0x1000, 0x2000, 4096},   // 常规 4KB 片段
       {0x1000, 0x2000, 0},      // len 为 0
       {0, 0, 1},                // 两端空地址
     };
     for (size_t i = 0; i < ops.size(); i++) {
       printf("op[%zu] local=0x%lx remote=0x%lx len=%zu\n",
              i, ops[i].local_addr, ops[i].remote_addr, ops[i].len);
     }

     // 2. 查询全部已知特性 + 一个越界值（模拟未知/非法特性）
     const struct { int32_t ft; const char *name; } cases[] = {
       {hixl::AUTO_CONNECT, "AUTO_CONNECT"},
       {hixl::CLIENT_SERVER_COMM, "CLIENT_SERVER_COMM"},
       {2, "FUTURE/UNKNOWN(2)"},
       {-1, "NEGATIVE(-1)"},
     };
     for (const auto &c : cases) {
       int32_t value = hixl::FEATURE_NOT_SUPPORTED;
       hixl::Status s = hixl::Hixl::GetCapability(
           static_cast<hixl::FeatureType>(c.ft), value);
       printf("%-24s status=%u value=%d\n", c.name, s, value);
     }
     return 0;
   }
   ```

   (b) 在已加载 CANN 环境的机器上编译链接（hixl 头文件需指向安装目录或仓库 `include/`，链接 libhixl；具体路径随安装方式不同）：

   ```bash
   # 编译：需 CANN 环境提供 external/ge_common 头文件
   g++ -std=c++17 capability_probe.cpp -I<安装目录>/include -L<安装目录>/lib -lhixl -o capability_probe
   ./capability_probe
   ```

3. **需要观察的现象**：
   - `AUTO_CONNECT` 与 `CLIENT_SERVER_COMM` 行输出 `status=0 value=1`；
   - `FUTURE/UNKNOWN(2)` 行输出 `status=0 value=0`（未知特性安全降级）；
   - `NEGATIVE(-1)` 行输出 `status=103900 value=0`（PARAM_INVALID）。

4. **预期结果**：三行输出与 4.4.2 流程图逐条吻合；`TransferOpDesc` 的三个片段只是构造与打印，不触发任何传输（构造描述子本身不做任何校验，校验发生在 Transfer 接口内部）。

5. 本机无 CANN/昇腾环境时编译链接步骤**待本地验证**；`GetCapability` 本身不依赖 device，理论上 x86 主机装好头文件与库即可运行。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `FeatureType` 规定「新能力只能追加在末尾」？

**答案**：能力值一旦发布，用户程序就会把 `0`、`1` 等数值写进代码或配置。中间插值会改变既有值的语义（旧程序查询到错误的能力），删除或复用旧值同理。追加在末尾则保证旧值语义永不变，配合 default 分支返回 0，实现二进制层面的向前兼容。

**练习 2**：`GetCapability` 为什么可以不初始化就调用，而 `TransferSync` 不行？

**答案**：`GetCapability` 是静态接口，查询的是**编译进库的固有能力表**（hixl_impl.cc 中的 switch 常量），不读写任何运行时状态；`TransferSync` 依赖引擎实例的链路、内存注册表等状态，必须先 `Initialize` 创建 `HixlImpl`（对照 u2-l1 的两道门卫）。

**练习 3**：你的程序想用 AutoConnect 特性但不确定目标机器支持，正确的调用序列是什么？

**答案**：先 `int32_t v = hixl::FEATURE_NOT_SUPPORTED; Hixl::GetCapability(hixl::AUTO_CONNECT, v);`，若 `v == hixl::FEATURE_SUPPORTED` 再在 `Initialize` 的 options 里设置 `hixl::OPTION_AUTO_CONNECT`（值为 `"true"` 等字符串，见 u2-l1 选项解析）；否则走手动 `Connect` 路径。查询动作本身总是安全的。

## 5. 综合实践

把本讲四条线串成一个「类型探针」程序（在 4.4.4 示例代码基础上扩展，**示例代码**）：

1. **第一段——内存与传输描述**：`malloc` 一块主机内存，填充 `MemDesc{addr, len}` 并配 `MEM_HOST`；再构造 N 个 `TransferOpDesc`（可按 4KB 切片一段虚拟 buffer）。只构造、不调用 RegisterMem/Transfer，打印每个字段的值，体会「描述子只是数据，行为由接口赋予」。
2. **第二段——状态与错误码**：打印 `SUCCESS/PARAM_INVALID/FAILED` 等常量的数值，验证与官方错误码表格一致；再用 `static_assert(hixl::SUCCESS == 0U, "contract")` 把「成功必须为 0」的契约固化进代码。
3. **第三段——能力查询**：按 4.4.4 逐项查询并输出。
4. **观察与记录**：把三段输出整理成一张「类型-值-语义」速查表，存入个人笔记；这张表就是你后续阅读 u2-l3～u2-l5 传输链路时的随身字典。
5. 编译运行依赖 CANN 头文件与 libhixl，无环境时记录阻塞原因，各打印值**待本地验证**。

## 6. 本讲小结

- hixl_types.h 用约 110 行定义了 HIXL 全部公开数据类型：基础别名（`Status`/`AscendString`/`MemHandle`/`TransferReq`）、内存描述（`MemType`/`MemDesc`）、传输描述（`TransferOp`/`TransferOpDesc` 及异步查询族）、链路状态（`AsyncConnectStatus`）与通知（`NotifyDesc`）。
- `TransferOpDesc` 是纯三元组（local_addr/remote_addr/len），批量传输 = vector of 三元组；`MemType`（内存形态）与 `TransferOp`（方向）是两个正交维度。
- 错误码共 9 个 `constexpr uint32_t`，唯一正确判断是 `== hixl::SUCCESS`；`1039xx` 多为可恢复业务错误，`TIMEOUT/FAILED` 不可恢复需保留现场，`RESOURCE_EXHAUSTED` 等待重试。
- 结构体中的 `reserved` 字段是 ABI 预留空间，用户代码不应读写。
- `FeatureType` 值显式赋值且只增不改，`GetCapability` 是无需初始化的静态接口，未知特性安全返回 `FEATURE_NOT_SUPPORTED`。
- 本讲所有概念以官方文档 [HIXL-data-structure.md](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/docs/zh/api/cpp/HIXL-data-structure.md) 与 [HIXL-error-code.md](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/docs/zh/api/cpp/HIXL-error-code.md) 为权威对照。

## 7. 下一步学习建议

下一讲 **u2-l3 内存注册与解注册** 将深入 `MemDesc`/`MemHandle` 背后的引擎实现：阅读 `src/hixl/engine/hixl_impl.cc` 中 `RegisterMem` 的实现、`src/hixl/common/segment.h` 的 segment 抽象与 `src/hixl/cs/hixl_mem_store.cc` 的登记流程，回答「为什么零拷贝必须先注册内存」。建议提前带着一个问题去读：注册动作到底把内存信息告诉了谁——本端引擎、远端引擎，还是两者？
