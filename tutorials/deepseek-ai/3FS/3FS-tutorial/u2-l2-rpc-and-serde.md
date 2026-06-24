# RPC 与序列化框架：serde 与 FlatBuffers

## 1. 本讲目标

本讲是公共基础设施单元（u2）的第二篇，承接 [u2-l1 服务骨架](u2-l1-service-skeleton.md)。在上一讲里我们看到，meta、storage、mgmtd 三个服务都共用同一套 `TwoPhaseApplication` 启动骨架，而它们彼此之间、以及它们与 client 之间如何“说话”，就要靠本讲的 **RPC + 序列化（serde）框架**。

学完本讲，你应该能够：

1. 读懂 3FS 的 RPC 抽象：一次调用在客户端如何被 `ClientContext` 打包发出，在服务端如何被 `CallContext` 拆开并派发到具体方法。
2. 说出 `MessagePacket` 在线上的结构（serviceId / methodId / flags / payload / timestamp），以及为什么靠 `(serviceId, methodId)` 两个数字就能定位一个远程方法。
3. 看懂 `fbs` 目录里的“服务定义”，理解它如何用 **C 预处理器 X-macro** 技巧，从同一份方法列表同时生成「服务端派发表」「客户端发送器」「同步/异步 Stub 接口」。
4. 自己追踪 `simple_example` 的 `echo` 调用，列出沿途涉及的宏与生成的类。

> 一个贯穿全讲的认知：3FS 目录里叫 `fbs`（FlatBuffers schema 的常见缩写），但它**并不使用 Google FlatBuffers 的 `flatc` 编译器**。`fbs` 层是一组手写的 C++ 头文件 + 宏，线上的二进制表格式**借鉴了** FlatBuffers 的 vtable 思路，而真正的“代码生成”完全由 C 预处理器在编译期完成。这点务必记住，否则会被目录名误导。

## 2. 前置知识

- **RPC（远程过程调用）**：让调用远端机器上的函数看起来像调用本地函数。需要解决三件事——把参数变成字节流（序列化）、把字节流送到对端（网络传输）、对端找到对应函数并执行（派发）。
- **序列化 / 反序列化（serde）**：把内存里的 C++ 结构体转成连续字节（serialize），以及反过来（deserialize）。
- **C 预处理器宏与 X-macro**：`#define` + `#include` 同一个文件多次、每次重定义宏，从而“展开”出不同的代码。本讲的桩代码生成就建立在这个技巧上。
- **folly 协程（`CoTryTask` / `co_await`）**：3FS 的网络栈是协程化的，远程调用返回的是一个可 `co_await` 的 `CoTryTask<Rsp>`。如果你还不熟悉，可先把它当成“返回 `Result<Rsp>` 的异步函数”。
- **`Result<T>`**：3FS 自带的“值或错误”类型，等价于带错误码的 `expected<T>`。

建议先读过 [u2-l1](u2-l1-service-skeleton.md) 了解 `beforeStart` 钩子，因为服务注册就发生在那里。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/common/serde/Service.h` | RPC 服务的“声明式”核心：`SERDE_SERVICE` / `SERDE_SERVICE_METHOD` / `SERDE_SERVICE_CLIENT` 等宏，以及反射用的 `MethodInfo`、`MethodExtractor`。 |
| `src/common/serde/CallContext.h` | **服务端**调用上下文：收包、反序列化、按 methodId 派发到 handler、把返回值打包回发。 |
| `src/common/serde/ClientContext.h` | **客户端**调用上下文：打包请求、发送、等待、反序列化响应；分协程异步 (`call`) 与同步 (`callSync`) 两条路径。 |
| `src/common/serde/MessagePacket.h` | 线上消息的信封：`uuid/serviceId/methodId/flags/payload/timestamp`，以及 `EssentialFlags`。 |
| `src/common/serde/Services.h` | 服务表容器：按 `serviceId` 注册并查找 `CallContext::ServiceWrapper`，区分 TCP/RDMA 两套表。 |
| `src/common/serde/Serde.h` | 序列化引擎：`SERDE_STRUCT_FIELD` 宏、`serialize/deserialize`，以及“借鉴 FlatBuffers 的表格式”二进制编码。 |
| `src/common/net/Processor.h` | 收到字节后的入口：区分请求/响应，构造 `CallContext` 并 `handle()`。 |
| `src/common/net/Server.h` | `addSerdeService`：按服务名把 handler 对象挂到对应 ServiceGroup。 |
| `src/fbs/simple_example/SerdeService.h` | 最小示例服务定义：`echo` 方法。 |
| `src/simple_example/service/Service.h` / `Service.cc` | `echo` 的服务端实现。 |
| `src/fbs/mgmtd/MgmtdServiceDef.h` | mgmtd 的“方法清单”（X-macro 数据源）。 |
| `src/fbs/macros/SerdeDef.h` | 4 参宏 → 6 参宏的默认桥接（`Name##Req` / `Name##Rsp` 命名约定）。 |
| `src/fbs/mgmtd/MgmtdServiceBase.h` / `MgmtdServiceClient.h` | mgmtd 服务端基类（反射元数据）与客户端发送器。 |
| `src/stubs/mgmtd/IMgmtdServiceStub.h` / `MgmtdServiceStub.h` / `MgmtdServiceStub.cc` | mgmtd 的 Stub 接口与实现（复用同一份 `MgmtdServiceDef.h` 生成）。 |
| `src/stubs/common/Stub.h` / `RealStubFactory.h` | Stub 的运行期封装：Real / Mock 两种实现 + 工厂。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**4.1 RPC 抽象**（运行期的调用与派发）、**4.2 serde 序列化与表格式编码**（字节怎么排）、**4.3 fbs 层与 X-macro 代码生成**（宏怎么把一份定义变成多份代码）。

### 4.1 RPC 抽象：Service / CallContext / ClientContext

#### 4.1.1 概念说明

3FS 的 RPC 模型可以浓缩成一句话：**用 `(serviceId, methodId)` 两个 16 位整数给每个远程方法编一个“门牌号”，线上只传门牌号，不传方法名**。

这样做的好处是：

- 服务端拿到包后，能用 `O(1)` 的数组下标（`methodId`）直接查到“该调用哪个成员函数指针”，没有任何字符串比较或哈希。
- 协议紧凑、派发快，适合 3FS 这种“每秒上百万次小 RPC”的存储场景。

围绕这个想法，三个类各司其职：

- `Service`（由 `SERDE_SERVICE` 宏生成）：一个服务的“身份证”——名字 + `serviceId` + 一堆方法的元信息（方法名、Req/Rsp 类型、methodId、成员函数指针）。
- `ClientContext`：客户端拿着它发请求。它知道“怎么连到对端”（`IOWorker` / 同步连接池 / 已有连接三选一）、“对端地址”、“默认请求选项”。调一个方法 = 打包一个 `MessagePacket` 发出去，然后挂起协程等回包。
- `CallContext`：服务端每个请求对应一个临时对象。它持有收到的包、传输句柄、以及该服务的派发表，负责“反序列化 → 调 handler → 序列化响应 → 回发”。

#### 4.1.2 核心流程

一次完整的 RPC（以协程异步路径为例）：

```text
客户端                                   网络                      服务端
------                                   ----                      ------
ClientContext.call<...>(req)
  ├─ 组装 MessagePacket{serviceId, methodId, IsReq, payload}
  ├─ 用 uuid 绑定一个 Waiter::Item（用于匹配回包）
  ├─ IOWorker.sendAsync(destAddr, writeItem)  ──字节流──►
  ├─ Waiter.schedule(uuid, timeout)
  └─ co_await item.baton  (协程挂起)                              Processor.processMsg
                                                                    ├─ deserialize 出 MessagePacket
                                                                    ├─ flags & IsReq ? 请求 : 响应
                                                                    └─ processSerdeRequest:
                                                                         service = getServiceById(serviceId)
                                                                         CallContext ctx(packet, tr, service)
                                                                         co_await ctx.handle()
                                                                            ├─ method = service.getter(methodId)
                                                                            ├─ (this->*method)() → call<F>()
                                                                            │     ├─ deserialize req
                                                                            │     ├─ obj->F::method(ctx, req)   ← 真正的 handler
                                                                            │     └─ makeResponse(result)
                                                                  ◄──字节流──  tr_->send(响应包)
  ├─ baton 唤醒，取出 item.packet
  ├─ deserialize 出 Rsp
  └─ co_return rsp
```

关键点：客户端用 `uuid` 把“请求”和“回包”配对（因为多条 RPC 可能共用一条连接、乱序到达）；服务端用 `(serviceId, methodId)` 两次数组下标定位 handler，全程无字符串查找。

#### 4.1.3 源码精读

**① `MessagePacket`：线上的信封。** [src/common/serde/MessagePacket.h:53-70](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/serde/MessagePacket.h#L53-L70) 定义了每个 RPC 包都带的头部字段：`uuid`（配对用）、`serviceId`/`methodId`（门牌号）、`flags`（是不是请求、是否压缩、是否走 RDMA 控制路径）、`version`（协议版本）、`payload`（真正的 Req 或 Rsp）、可选的 `timestamp`（全程时延埋点）。`EssentialFlags` 三个位见 [src/common/serde/MessagePacket.h:11-15](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/serde/MessagePacket.h#L11-L15)。

**② `ClientContext::call`：客户端发送 + 等待。** [src/common/serde/ClientContext.h:40-131](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/serde/ClientContext.h#L40-L131)。核心几步：

- `MessagePacket packet(req); packet.serviceId = ServiceID; packet.methodId = MethodID; packet.flags = IsReq;` 把门牌号和 Req 写进信封（`L56-L66`）。
- `uint64_t uuid = net::Waiter::instance().bind(item);` 申请配对号（`L48-L49`）。
- `ioWorker->sendAsync(destAddr_, ...)` 把字节发出去，`Waiter::schedule(uuid, options.timeout)` 挂一个超时（`L74-L85`）。
- `co_await item.baton;` 协程挂起，等回包或超时（`L86`）。
- 回包到了之后 `serde::deserialize(rsp, item.packet.payload)` 还原 `Rsp`（`L101-L109`）。

注意它的模板参数里带了 `kServiceName`/`kMethodName`：这是为了在 `L123-L129` 上报监控指标时能拼出 `"<服务名>::<方法名>"` 的 tag，而不需要运行期查表。

**③ `CallContext`：服务端派发。** [src/common/serde/CallContext.h:13-23](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/serde/CallContext.h#L13-L23) 定义了内部结构 `ServiceWrapper`：一个函数指针表 `getter`（“给我 methodId，我还你成员函数指针”）、一个错误处理函数 `onError`、以及 handler 对象指针 `object`。派发逻辑极简，见 [src/common/serde/CallContext.h:35-38](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/serde/CallContext.h#L35-L38)：

```cpp
CoTask<void> handle() {
  auto method = service_.getter(packet_.methodId);   // methodId → 函数指针
  co_await (this->*method)();                          // 调用，最终落到 call<F>()
}
```

`call<F>()` 是模板，`F` 是某个方法的反射元信息。它做“反序列化 → 调 handler → 回包”三件事，见 [src/common/serde/CallContext.h:46-76](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/serde/CallContext.h#L46-L76)，其中 `auto obj = reinterpret_cast<typename F::Object *>(service_.object); auto result = co_await ... (obj->*F::method)(*this, req);` 就是真正调用你写的 handler（`L60-L61`）。响应由 `makeResponse` 回发，见 [src/common/serde/CallContext.h:119-128](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/serde/CallContext.h#L119-L128)：它把 `serviceId/methodId/uuid` 原样回填，保证客户端能配对。

**④ `Services`：服务表。** [src/common/serde/Services.h:17-32](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/serde/Services.h#L17-L32) 的 `addService` 把一个 handler 对象挂到 `services_[rdma?1:0][serviceId]` 上，并把 `getter` 设成 `MethodExtractor<Service, CallContext, &CallContext::invalidId>::get`。容器本身是一个大小 65536 的数组（见 [src/common/serde/Services.h:38](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/serde/Services.h#L38) 的注释 `0 for TCP, 1 for RDMA`），所以 `getServiceById(serviceId, isRDMA)` 是纯数组下标，O(1)。

**⑤ `Processor`：网络层到 RPC 层的桥。** 收到字节后先反序列化出 `MessagePacket`，再用 `flags & IsReq` 区分请求/响应：请求走 `tryToProcessSerdeRequest`，响应用 `Waiter::post` 唤醒等待中的客户端协程。见 [src/common/net/Processor.h:147-155](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/Processor.h#L147-L155)。请求处理见 [src/common/net/Processor.h:158-170](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/Processor.h#L158-L170)：`auto &service = serdeServices_.getServiceById(packet.serviceId, tr->isRDMA()); serde::CallContext ctx(packet, std::move(tr), service); co_await ctx.handle();`。

#### 4.1.4 代码实践

**目标**：用一个会编译失败的最小实验，验证“门牌号 = `(serviceId, methodId)`”这件事，并看懂 `MethodExtractor` 如何把 methodId 映射到函数指针。

**步骤**：

1. 打开 [src/common/serde/Service.h:48-78](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/serde/Service.h#L48-L78)。`MethodExtractor` 是一个 `consteval`（编译期求值）类：构造时遍历所有反射字段，把“methodId → `&C::call<FieldInfo>`”填进一张 `std::array`，运行期 `get(id)` 只是 `table[id]` 一次下标。
2. 回答：为什么 `MethodExtractor` 用 `consteval` 构造？（提示：这张派发表是编译期常量，避免运行期初始化开销与数据竞争。）
3. 进一步追踪 [src/common/serde/Services.h:24](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/serde/Services.h#L24)：`service.getter = &MethodExtractor<Service, CallContext, &CallContext::invalidId>::get;`。注意第三个模板参数 `&CallContext::invalidId` 是 `DEFAULT`——当 `id` 超出已知范围时返回它，对应“方法不存在”。

**需要观察的现象 / 预期结果**：

- 你应该得出结论：服务端派发一次请求 = 1 次数组下标（serviceId）+ 1 次数组下标（methodId）+ 1 次成员函数指针调用，没有任何字符串/哈希。这是 3FS RPC 高吞吐的根基之一。
- 本实践为“源码阅读型”，无需运行；如果要让结论可执行，可在 `CallContext::invalidId`（[src/common/serde/CallContext.h:40-44](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/serde/CallContext.h#L40-L44)）处加一行日志，向一个不存在的 `methodId` 发包，观察服务端打印 `method X:Y not found!`（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `MessagePacket` 里同时有 `serviceId` 和 `methodId`，而不直接用一个 32 位 `methodId` 全局编号？

**参考答案**：分组便于隔离与扩展。不同服务（mgmtd/meta/storage/core）各自独立编号 methodId（很多都从 1 开始），新增方法只影响本服务；服务端也按 `serviceId` 先定位到 handler 对象，再用 `methodId` 在该对象的方法表里查。这比维护一个全局唯一、随服务演进不断膨胀的方法号要清晰。

**练习 2**：`ClientContext` 有三种 `connectionSource_`（`IOWorker*` / `ConnectionPool*` / `Transport*`），分别对应什么调用方式？

**参考答案**：`IOWorker*` 走协程异步路径（`call`，`sendAsync` + `co_await baton`）；`ConnectionPool*` 走同步阻塞路径（`callSync`，`sendSync/recvSync`，见 [src/common/serde/ClientContext.h:133-192](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/serde/ClientContext.h#L133-L192)）；`Transport*` 针对已建立的单条连接直接 `send`。三者在 `L74-L83` 与 `L158-L161` 被区分。

### 4.2 serde 序列化与 FlatBuffers 风格的表格式编码

#### 4.2.1 概念说明

RPC 要把 C++ 结构体变成字节流。3FS 没有引入 protobuf / FlatBuffers 库，而是自己写了一套叫 **serde** 的序列化框架，特点：

- **声明式字段**：结构体里写一行 `SERDE_STRUCT_FIELD(name, default)`，就同时声明了成员变量、默认值，以及反射元信息（字段名 + getter）。
- **双模输出**：同一份 `serialize` 代码，在二进制模式（`isBinaryOut`）下输出紧凑的表格式字节，在可读模式下输出带 key 的 JSON 风格文本（用于日志、`toJsonString`）。
- **表格式（借鉴 FlatBuffers 的 vtable 思路）**：一个结构体 = 一张“表”，先依次写各字段，再记录表的长度信息，便于跳过未知字段、支持前向/后向兼容。

`fbs` 这个目录名里的 “fb” 正是暗示这种 FlatBuffers 风格的表编码；但请再次记住：**没有 `flatc`，没有 `.fbs` 模式文件，全是 C++ 头 + 宏**。

#### 4.2.2 核心流程

对一个满足 `SerdeType` 概念（即用 `SERDE_STRUCT_FIELD` 声明了字段）的结构体 `T`，`serde::serialize(o, out)` 的分支见 [src/common/serde/Serde.h:282-290](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/serde/Serde.h#L282-L290)：

```text
二进制模式:
  start = tableBegin(false)                 // 记录当前输出位置
  for each field: serialize(o.*getter, out) // 依次写字段（递归）
  tableEnd(start)                           // 追加写一个 Varint32 = 本表字节数
可读模式:
  start = tableBegin(false)
  for each field: out.key(name); serialize(o.*getter, out)
  tableEnd(start)
```

二进制 `Serializer` 的 `tableBegin/tableEnd` 实现见 [src/common/serde/Serde.h:424-425](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/serde/Serde.h#L424-L425)：`tableBegin` 返回当前 `out_.size()`，`tableEnd` 在末尾再写一个 `Varint32(out_.size() - start)`（本表长度）。这种“内容 + 长度后缀”的布局让反序列化能按表跳跃，是 FlatBuffers vtable 思想的简化版。

容器、`optional`、`variant`、`map` 等都有对应分支（见 [src/common/serde/Serde.h:295-360](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/serde/Serde.h#L295-L360)），例如 `optional` 在末尾追加 `HasValue/NullOpt` 标志位，`vector` 用 `arrayBegin/arrayEnd(size)` 包裹。

#### 4.2.3 源码精读

**① `SERDE_STRUCT_FIELD` 宏。** [src/common/serde/Serde.h:59-60](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/serde/Serde.h#L59-L60) 把 `SERDE_STRUCT_FIELD(name, default)` 转成带类型的 `SERDE_STRUCT_TYPED_FIELD(decltype(default), name, default)`；后者见 [src/common/serde/Serde.h:42-54](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/serde/Serde.h#L42-L54)，做了三件事：声明成员 `TYPE NAME = DEFAULT;`、把字段注册进反射（`FieldInfo<#NAME, &getter>`）、并把当前累计的反射列表记到一个随方法 id 递增的 `T##NAME` 类型里（这是 `SERDE_SERVICE_METHOD_REFL` 收集方法表用的同一套反射机制）。

**② 一个真实结构体。** 看 [src/fbs/simple_example/SerdeService.h:8-14](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/simple_example/SerdeService.h#L8-L14)：

```cpp
struct SimpleExampleReq { SERDE_STRUCT_FIELD(message, String{}); };
struct SimpleExampleRsp { SERDE_STRUCT_FIELD(message, String{}); };
```

这两行就足够让 `serde::serialize/deserialize` 正确编解码——字段名 `message` 同时用于可读输出与反射。

**③ 时延埋点 `Timestamp`。** [src/common/serde/MessagePacket.h:36-51](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/serde/MessagePacket.h#L36-L51) 定义了 8 个时间戳，把一次 RPC 的耗时切成几段。它们的关系是纯算术：

\[
\text{totalLatency} = \text{clientWaked} - \text{clientCalled}
\]

\[
\text{networkLatency} = (\text{clientReceived} - \text{clientSerialized}) - (\text{serverSerialized} - \text{serverReceived})
\]

\[
\text{queueLatency} = \text{serverWaked} - \text{serverReceived}
\]

当 `options.logLongRunningThreshold != 0` 或 `reportMetrics` 打开时，`ClientContext` 会在请求里带上 `Timestamp`，`CallContext` 在 `call<F>()` 里填 `serverWaked/serverProcessed`（见 [src/common/serde/CallContext.h:49-51](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/serde/CallContext.h#L49-L51) 与 `L71-L73`），客户端据此算出“排队耗时 / 服务端处理耗时 / 网络耗时”，见 [src/common/serde/ClientContext.h:114-129](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/serde/ClientContext.h#L114-L129)。这是 3FS 排查慢请求的核心数据。

#### 4.2.4 代码实践

**目标**：亲手走一遍 `SimpleExampleReq` 的可读序列化输出，理解“双模输出”。

**步骤**：

1. 在 `src/simple_example` 或一个临时 cpp 里，包含 `fbs/simple_example/SerdeService.h` 与 `common/serde/Serde.h`。
2. 构造 `simple_example::SimpleExampleReq req; req.message = "hello";`
3. 调用 `serde::toJsonString(req)`（`ClientContext` 里多处用它打印请求体，见 [src/common/serde/CallContext.h:68](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/serde/CallContext.h#L68) 与 [src/common/serde/ClientContext.h:121](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/serde/ClientContext.h#L121)），打印结果。
4. 再用二进制 `Serializer` 序列化同一对象，观察字节长度。

**需要观察的现象 / 预期结果**：

- 可读输出应形如 `{"message": "hello"}`（带 key），因为它走的是 [src/common/serde/Serde.h:288](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/serde/Serde.h#L288) 的 `out.key(type.name)` 分支。
- 二进制输出不含字段名 `message`，只有字段内容 + 表长度后缀，更紧凑。这解释了为何线上用二进制、日志用可读：同一套 `serialize` 逻辑，两种产物。
- 若本地无构建环境，记为「待本地验证」；可改为纯阅读：在 `Serde.h` 的 `L282-L290` 处对照两个分支说明差异。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `SERDE_STRUCT_FIELD` 要求写默认值（如 `String{}`、`0`、`false`）？

**参考答案**：默认值同时承担两个用途——(1) 声明成员变量时的初值；(2) 序列化框架用它推导字段类型（`std::decay_t<decltype(DEFAULT)>`），省去手写类型。这也是为什么 3FS 的 Req/Rsp 结构体看起来异常简洁。

**练习 2**：`optional` 字段在二进制编码里如何区分“有值”和“无值”？

**参考答案**：见 [src/common/serde/Serde.h:295-301](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/serde/Serde.h#L295-L301)：先（递归）写值本身，再追加一个 `Optional::HasValue` 或 `Optional::NullOpt` 标志字节，反序列化时读这个尾部标志决定要不要还原值。

### 4.3 fbs 层与 X-macro 代码生成

#### 4.3.1 概念说明

写一个 RPC 服务，传统上要分别手写：“服务端 handler 类”“客户端发送函数”“接口（抽象基类）”“监控 tag”……这些代码 90% 是重复的样板。3FS 用一个经典技巧消除重复：**X-macro（重复包含同一份“方法清单”文件，每次重定义宏）**。

核心约定：

- 一个服务只有一个“权威清单”，例如 mgmtd 是 [src/fbs/mgmtd/MgmtdServiceDef.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/mgmtd/MgmtdServiceDef.h)，里面每一行是一个宏调用 `DEFINE_SERDE_SERVICE_METHOD(svc, methodName, MethodName, id)`。
- 清单文件**自己不定义宏**，而是先 `#include "fbs/macros/SerdeDef.h"` 取默认宏定义，再被各个“消费者”文件 `#include`（消费者会先 `#define` 成自己想要的样子）。
- 于是同一份清单，被展开成：服务端基类的反射元数据、客户端发送器、Stub 接口、Stub 实现……各取所需。

命名约定（重要）：方法名有大小写两种形态——`methodName`（小驼峰，C++ 方法名）与 `MethodName`（大驼峰，用于拼接 `MethodName##Req` / `MethodName##Rsp` 类型）。桥接见 [src/fbs/macros/SerdeDef.h:5-8](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/macros/SerdeDef.h#L5-L8)。

> 关于“ISyncStub/SyncStub 宏”：`src/fbs/macros/` 下确实有 `ISyncStub.h`、`SyncStub.h`、`IStub.h`、`Stub.h` 四个文件，用另一套宏名（`DEFINE_FBS_SERVICE` / `FINISH_FBS_SERVICE` / `DEFINE_FBS_SERVICE_METHOD`）描述服务。但当前 mgmtd / core / meta 等服务的 Stub 实际**并不使用**这套宏（全局检索仅命中这几个文件自身），活跃路径用的是本节讲的 `SERDE_SERVICE_METHOD` + `DEFINE_SERDE_SERVICE_METHOD_FULL` X-macro 组合。读源码时以 `SERDE_SERVICE*` 与 `DEFINE_SERDE_SERVICE_METHOD_FULL` 为准；`fbs/macros/` 那套可视为历史/备用词汇。

#### 4.3.2 核心流程

以 mgmtd 为例，一份清单 `MgmtdServiceDef.h` 被“消费”四次：

```text
MgmtdServiceDef.h (权威清单: 24 个 DEFINE_SERDE_SERVICE_METHOD 行)
        │  依赖 SerdeDef.h 把 4 参 → 6 参 (..., Name##Req, Name##Rsp)
        │
        ├─① MgmtdServiceBase.h   重定义 _FULL = SERDE_SERVICE_METHOD_REFL
        │     → 生成「服务端反射元数据」(供 MethodExtractor 建 methodId 派发表)
        │
        ├─② MgmtdServiceClient.h SERDE_SERVICE_CLIENT → 生成 send<#name>(ctx, req)
        │     → 生成「客户端发送器」(基于 Service.h 的 SERDE_SERVICE_METHOD_SENDER)
        │
        ├─③ IMgmtdServiceStub.h  重定义 _FULL = pure virtual CoTryTask<Rsp> name(Req)=0
        │     → 生成「Stub 接口」(抽象基类)
        │
        └─④ MgmtdServiceStub.h/.cc 重定义 _FULL = 具体实现
              → body: co_return co_await MgmtdServiceClient<>::send<#name>(ctx_, req)
              → 生成「Stub 实现」(把接口方法转交给 ClientContext)
```

最终对外暴露的是 `IMgmtdServiceStub`（接口）+ `MgmtdServiceStub<Ctx>`（实现），调用方只面向接口编程，便于用 `Mock` 替换（见 `stubs/common/Stub.h` 的 `SerdeStub`，同时持有 Real 与 Mock 两种实现）。

#### 4.3.3 源码精读

**① 权威清单。** [src/fbs/mgmtd/MgmtdServiceDef.h:3-26](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/mgmtd/MgmtdServiceDef.h#L3-L26) 列出 mgmtd 全部 RPC，例如 `DEFINE_SERDE_SERVICE_METHOD(Mgmtd, heartbeat, Heartbeat, 3)`、`... getRoutingInfo, GetRoutingInfo, 5`、`... updateChain, UpdateChain, 24`。注意编号不必连续（`// 2 is deprecated`），这正是用数字门牌号的好处之一——可以废弃旧号、保留兼容。

**② 4 参 → 6 参桥接。** [src/fbs/macros/SerdeDef.h:5-8](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/macros/SerdeDef.h#L5-L8)：

```cpp
#define DEFINE_SERDE_SERVICE_METHOD(ServiceName, methodName, MethodName, MethodId) \
  DEFINE_SERDE_SERVICE_METHOD_FULL(ServiceName, methodName, MethodName, MethodId, MethodName##Req, MethodName##Rsp)
```

`Heartbeat` 自动拼出 `HeartbeatReq` / `HeartbeatRsp`——这就是 [src/fbs/mgmtd/Rpc.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/mgmtd/Rpc.h) 里那些 `DEFINE_SERDE_HELPER_STRUCT(HeartbeatReq){...}` 的命名来源。

**③ 服务端基类（只取反射）。** [src/fbs/mgmtd/MgmtdServiceBase.h:7-11](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/mgmtd/MgmtdServiceBase.h#L7-L11)：`SERDE_SERVICE_2(MgmtdServiceBase, Mgmtd, 217)` 声明服务 id = 217，然后把 `_FULL` 重定义为 `SERDE_SERVICE_METHOD_REFL(name, id, reqtype, rsptype)`（只收集反射元数据，不生成发送器）。对比 [src/fbs/core/service/CoreServiceBase.h:7-11](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/core/service/CoreServiceBase.h#L7-L11)，core 用的是完整的 `SERDE_SERVICE_METHOD`（`REFL` + `SENDER` 都要），区别在于客户端发送器是放在基类里还是单独用 `SERDE_SERVICE_CLIENT` 生成——mgmtd 走后者（见 [src/fbs/mgmtd/MgmtdServiceClient.h:6](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/mgmtd/MgmtdServiceClient.h#L6)）。

**④ `SERDE_SERVICE_METHOD` 展开成什么。** 看 [src/common/serde/Service.h:86-126](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/serde/Service.h#L86-L126)：它分成 `SERDE_SERVICE_METHOD_SENDER`（[L90-L112](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/serde/Service.h#L90-L112)）与 `SERDE_SERVICE_METHOD_REFL`（[L114-L126](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/serde/Service.h#L114-L126)）。`SENDER` 生成两个静态方法：异步 `name(Context&, req, ...)`（`co_return co_await ctx.call<...>(req, ...)`）和同步 `nameSync(Context&, req, ...)`（`ctx.callSync<...>(...)`）。`REFL` 通过 `CollectField` 把这个方法追加进反射列表——正是 4.1 里 `MethodExtractor` 用来建派发表的数据。

**⑤ 客户端发送器 `SERDE_SERVICE_CLIENT`。** [src/common/serde/Service.h:128-151](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/serde/Service.h#L128-L151) 生成 `MgmtdServiceClient<Ctx>`，提供一个模板 `send<name>(ctx, req, ...)`：它用 `refl::Helper::visit` 遍历基类的反射方法表，按方法名和 Req 类型匹配到对应元信息，再调 `ctx.call<...>(req)`。所以 Stub 实现里一句 `MgmtdServiceClient<>::send<"heartbeat">(ctx_, req)` 就能正确发出 heartbeat 请求。

**⑥ Stub 接口与实现。** [src/stubs/mgmtd/IMgmtdServiceStub.h:13-15](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/stubs/mgmtd/IMgmtdServiceStub.h#L13-L15) 把 `_FULL` 重定义为纯虚 `virtual CoTryTask<rsptype> name(const reqtype&) = 0;` 再包含清单 → 得到抽象接口。实现见 [src/stubs/mgmtd/MgmtdServiceStub.cc:8-12](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/stubs/mgmtd/MgmtdServiceStub.cc#L8-L12)：

```cpp
#define DEFINE_SERDE_SERVICE_METHOD_FULL(svc, name, Name, id, reqtype, rsptype) \
  template <typename Ctx>                                                       \
  CoTryTask<rsptype> svc##ServiceStub<Ctx>::name(const reqtype &req) {          \
    co_return co_await svc##ServiceClient<>::send<#name>(ctx_, req);            \
  }
#include "fbs/mgmtd/MgmtdServiceDef.h"
```

每个方法体都一样：转交给 `MgmtdServiceClient::send<#name>`。.cc 末尾还显式实例化了 `ClientContext` 与 `ClientMockContext` 两个版本（[L15-L16](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/stubs/mgmtd/MgmtdServiceStub.cc#L15-L16)），对应“真网络”与“单测 mock”两种用法。

**⑦ Stub 的运行期封装。** [src/stubs/common/Stub.h:16-44](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/stubs/common/Stub.h#L16-L44) 的 `SerdeStub` 用 `std::variant<RealStub, MockStub>` 同时持有两种实现，`operator->` 返回接口指针；构造靠 [src/stubs/common/RealStubFactory.h:19-21](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/stubs/common/RealStubFactory.h#L19-L21) 的 `create(addr)`：每给一个目标地址，就用 `ClientContextCreator` 造一个 `ClientContext`，再 `make_unique<StubType>(ctx)`。你在 [u2-l1](u2-l1-service-skeleton.md) 见过的 `RealStubFactory<mgmtd::MgmtdServiceStub>` 就是这么把“网络层”接到“Stub 接口”上的。

#### 4.3.4 代码实践

**目标**：验证“新增一个 RPC 方法，只需在清单里加一行，所有客户端/服务端/Stub 代码自动跟上”。

**步骤**（纯源码阅读 + 思想实验，不真正改源码以免影响仓库）：

1. 打开 [src/fbs/mgmtd/MgmtdServiceDef.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/mgmtd/MgmtdServiceDef.h)，假设你要加一个 `dumpStats` 方法（id=25）。
2. 问自己：除了在这一行加 `DEFINE_SERDE_SERVICE_METHOD(Mgmtd, dumpStats, DumpStats, 25)`，还需要改哪些文件，才能让 (a) 服务端能收到这个请求、(b) 客户端能发出这个请求、(c) `IMgmtdServiceStub` 多一个可调用方法？
3. 答案对照：
   - (a) 还要在 `src/fbs/mgmtd/Rpc.h` 加 `DEFINE_SERDE_HELPER_STRUCT(DumpStatsReq){...}` 与 `DumpStatsRsp`（提供 Req/Rsp 定义），以及在真实 mgmtd 服务端实现类里实现 `dumpStats` 方法。
   - (b)(c) **不需要再改任何 Stub/Client 文件**——`MgmtdServiceBase.h`、`MgmtdServiceClient.h`、`IMgmtdServiceStub.h`、`MgmtdServiceStub.h/.cc` 都会因重新包含 `MgmtdServiceDef.h` 而自动获得新方法。
4. 对照 [src/simple_example/service/Server.cc:50-51](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/simple_example/service/Server.cc#L50-L51) 的 `addSerdeService(std::make_unique<SimpleExampleService>(), true)` 与 [src/common/net/Server.h:42-52](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/Server.h#L42-L52)：服务端注册是按 `Service::kServiceName` 匹配 ServiceGroup 的（`Server.h:44`），所以配置里 `set_services({"SimpleExampleSerde"})`（见 [src/simple_example/service/Server.h:46](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/simple_example/service/Server.h#L46)）必须与 `SERDE_SERVICE` 的名字一致。

**需要观察的现象 / 预期结果**：

- 你应能说清“加一行清单”到底自动生成了哪些符号：服务端的 `MethodExtractor` 派发表多一个 id=25 的条目、`MgmtdServiceClient::send<"dumpStats">` 可用、`IMgmtdServiceStub::dumpStats` 虚函数出现、`MgmtdServiceStub::dumpStats` 实现出现。
- 本实践为“源码阅读/思想实验型”，无需编译；若要真验证，可参照 `src/simple_example/README.md` 的步骤复制出一个新服务再加方法（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：为什么清单文件 `MgmtdServiceDef.h` 要在开头 `#include "fbs/macros/SerdeDef.h"`，结尾 `#include "fbs/macros/Undef.h"`？

**参考答案**：清单本身只用 4 参宏 `DEFINE_SERDE_SERVICE_METHOD`，但不同消费者需要 6 参的 `_FULL`。`SerdeDef.h` 提供默认的“4→6”桥接，使消费者只需重定义 `_FULL` 即可；`Undef.h`（见 [src/fbs/macros/Undef.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/macros/Undef.h)）在清单末尾清理宏，避免污染后续翻译单元，保证“每次包含都干净可重定义”。

**练习 2**：`MgmtdServiceStub` 是模板 `template <typename Ctx>`，显式实例化了 `ClientContext` 与 `ClientMockContext` 两种。这样设计的好处是什么？

**参考答案**：同一份 Stub 逻辑既能跑真网络（`ClientContext`，走 `IOWorker`/连接池），也能在单测里跑 mock（`ClientMockContext`，不发真包、可注入预设响应）。调用方面向 `IMgmtdServiceStub` 接口编程，测试时换一个 mock 实现即可，业务代码无感——这正是 3FS 大量服务可单测的基础。

## 5. 综合实践：追踪 `simple_example` 的 `echo` 端到端

把三个模块串起来。`simple_example` 是官方“新服务模板”（见 [src/simple_example/README.md](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/simple_example/README.md)），它的 `echo` 是最小的完整 RPC，最适合做端到端追踪。

**任务**：从“客户端调用 `SimpleExampleSerde::echo(ctx, req)`”一路追到“服务端 `SimpleExampleService::echo` 被执行并回包”，画出时序，并标注沿途每一个**宏**与**生成的类/方法**。

**参考追踪路径**（请逐点打开链接核对）：

1. **服务定义**。[src/fbs/simple_example/SerdeService.h:16](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/simple_example/SerdeService.h#L16)：
   `SERDE_SERVICE(SimpleExampleSerde, 0xF0) { SERDE_SERVICE_METHOD(echo, 1, SimpleExampleReq, SimpleExampleRsp); }`
   - 宏：`SERDE_SERVICE`（→ `SERDE_SERVICE_2`，见 [Service.h:80-84](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/serde/Service.h#L80-L84)）、`SERDE_SERVICE_METHOD`（见 [Service.h:86-126](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/serde/Service.h#L86-L126)）。
   - 生成：服务 id = `0xF0`(240)；静态发送器 `echo(Context&, req)` 与 `echoSync(...)`；反射元信息（methodId=1）。

2. **客户端发起**。调用 `SimpleExampleSerde::echo(ctx, req)` → 进入 `SENDER` 生成的 `co_return co_await ctx.call<kServiceNameWrapper, "echo", Req, Rsp, 240, 1>(req, ...)` → [ClientContext::call](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/serde/ClientContext.h#L40-L131) 打包 `MessagePacket{serviceId=240, methodId=1, IsReq, payload=序列化后的 req}`，`sendAsync` 发出，`co_await baton`。

3. **服务端接收与派发**。[Processor::processMsg](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/Processor.h#L147-L155) 反序列化信封 → `processSerdeRequest`（[L158-L170](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/Processor.h#L158-L170)）→ `getServiceById(240, isRDMA)` 查到 `SimpleExampleService` 注册的 `ServiceWrapper` → `CallContext::handle()`（[CallContext.h:35-38](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/serde/CallContext.h#L35-L38)）→ `getter(1)` 经 `MethodExtractor` 返回 `&CallContext::call<echo 的 FieldInfo>` → `call<F>()`（[CallContext.h:46-76](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/serde/CallContext.h#L46-L76)）。

4. **handler 执行**。`call<F>()` 反序列化出 `SimpleExampleReq`，调用 `obj->echo(*this, req)`，即 [src/simple_example/service/Service.cc:13-17](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/simple_example/service/Service.cc#L13-L17) 的实现：把 `req.message` 原样塞回 `SimpleExampleRsp`，`co_return resp`。
   - 这里的 `SimpleExampleService` 继承 `serde::ServiceWrapper<SimpleExampleService, SimpleExampleSerde>`（[Service.h:37-46](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/serde/Service.h#L37-L46)），它的 `echo` 签名正是 `SERDE_SERVICE_METHOD` 通过 `MethodInfo::method = &T::echo` 记录的那个成员指针所指向的函数。

5. **回包**。`call<F>()` 调 `makeResponse(result)`（[CallContext.h:119-128](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/serde/CallContext.h#L119-L128)）把 `Rsp` 序列化、回填 `serviceId/methodId/uuid`，`tr_->send` 回发。

6. **客户端收尾**。`Processor` 判定为响应 → `Waiter::post` 唤醒 baton → `ClientContext::call` 在 [L101-L131](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/serde/ClientContext.h#L101-L131) 反序列化出 `SimpleExampleRsp`，`co_return rsp`，原始 `echo` 调用拿到结果。

**需要观察的现象 / 预期结果**：

- 你应得到一张时序图与一张“涉及的宏/类清单”：
  - 宏：`SERDE_SERVICE`、`SERDE_SERVICE_METHOD`（= `_SENDER` + `_REFL`）、`SERDE_STRUCT_FIELD`、（mgmtd 那种规模还会用到）`DEFINE_SERDE_SERVICE_METHOD`、`DEFINE_SERDE_SERVICE_METHOD_FULL`、`SERDE_SERVICE_CLIENT`。
  - 生成的类/符号：`SimpleExampleSerde`（服务描述）、`SimpleExampleSerde::echo/echoSync`（发送器）、`SimpleExampleReq/Rsp`（消息）、`MethodExtractor` 派发表条目、`CallContext::call<...>` 特化、`SimpleExampleService`（你的 handler）。
- 完成后，对照 [src/simple_example/README.md](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/simple_example/README.md) 思考：复制 `simple_example` 改名后，哪几个文件里的 `SimpleExample` / `simple_example` 字样必须替换（README 第 4、5 步的 `sed`）。

## 6. 本讲小结

- 3FS 的 RPC 用 **`(serviceId, methodId)` 两个 16 位整数**作为远程方法门牌号；`ClientContext` 负责打包发送+等待，`CallContext` 负责按门牌号 `O(1)` 派发到 handler，二者通过 `MessagePacket` 信封（含 `uuid` 做请求/回包配对）在线上对接。
- 派发的“methodId → 函数指针”映射由 `MethodExtractor` 在**编译期**（`consteval`）建成一张数组表，运行期仅一次下标，这是高吞吐 RPC 的根基。
- 序列化用自研 **serde** 框架：`SERDE_STRUCT_FIELD` 一行同时声明成员+默认值+反射；同一份 `serialize` 产出二进制（表格式，借鉴 FlatBuffers vtable 思路，**但不使用 `flatc`**）与可读 JSON 两种产物。
- “代码生成”靠 **C 预处理器 X-macro**：一份 `*ServiceDef.h` 方法清单被重复包含，每次重定义 `DEFINE_SERDE_SERVICE_METHOD(_FULL)`，从而一次性生成服务端反射元数据、客户端发送器、Stub 接口、Stub 实现。
- `fbs` 目录名暗示 FlatBuffers 风格的表编码，但里面是手写 C++ + 宏，不是 FlatBuffers 模式文件；活跃 Stub 走 `SERDE_SERVICE_METHOD` + `DEFINE_SERDE_SERVICE_METHOD_FULL`，`src/fbs/macros/{ISyncStub,SyncStub,...}.h` 是历史/备用宏词汇，当前服务未使用。
- Stub 用模板 `Stub<Ctx>` + 显式实例化（`ClientContext` / `ClientMockContext`）实现“真网络/单测 mock”双形态，调用方面向 `IServiceStub` 接口编程。

## 7. 下一步学习建议

本讲建立了“一次 RPC 在骨架之上如何流动”的认知，接下来建议：

1. **[u2-l3 协程、线程池与后台任务](u2-l3-coroutine-and-pools.md)**：`CoTryTask` 到底是怎么被调度执行的？`CallContext::handle()` 返回的协程由谁驱动？`Processor` 的 `coroutinesPoolGetter_`（见 [Processor.h:190-191](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/Processor.h#L190-L191)）指向哪里？这些都在下一讲解答。
2. **[u2-l4 网络层：TCP 与 RDMA 传输](u2-l4-network-rdma.md)**：本讲的 `IOWorker.sendAsync`、`Transport::send`、`CallContext::RDMATransmission`（[CallContext.h:94-114](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/serde/CallContext.h#L94-L114)）背后是真实的 TCP/RDMA 栈，下一讲深入。
3. **带着本讲的地图读真实服务**：挑 mgmtd 的 `getRoutingInfo`（[MgmtdServiceDef.h:7](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/mgmtd/MgmtdServiceDef.h#L7)），从 `IMgmtdServiceStub` 一路追到 mgmtd 服务端 handler，作为进入 [u3 集群管理服务 mgmtd](u3-l1-mgmtd-overview.md) 的预习。
