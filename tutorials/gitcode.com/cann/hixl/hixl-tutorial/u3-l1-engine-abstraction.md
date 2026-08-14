# u3-l1 Engine 抽象体系与工厂

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `Engine` 抽象基类定义了哪几组纯虚接口，以及它为什么是整个 HIXL Engine 的「接口合同」。
2. 解释 `CommEngine` 的适配器角色：它如何把 HIXL 新接口逐字段翻译成旧 ADXL 内部引擎调用。
3. 掌握 `EngineFactory::CreateEngine` 的四个选择分支（FabricMem / LocalCommRes 版本 / protocol_desc / SoC 类型），理解工厂模式如何把「选哪个引擎」从用户代码中剥离。
4. 理解 `HixlEngine` 与 `HixlServer` 的角色划分：一个 `HixlEngine` 实例内部同时持有 `HixlServer`（被动方）与 `ClientManager`（主动方）。
5. 画出从 `Hixl::Initialize` 到具体 Engine 对象创建的完整调用时序图。

## 2. 前置知识

本讲建立在前几讲的基础上，先回顾两个关键认知，再补充两个新概念。

**回顾一：Pimpl 与两层外壳（u2-l1）。** 用户拿到的是 `hixl::Hixl` 类，它只做日志、门卫检查和转发；真实状态放在 `HixlImpl` 里，其中最重要的成员就是一个 `std::unique_ptr<Engine> engine_`。本讲要回答的问题正是：这个 `engine_` 指针背后到底是什么对象、由谁决定。

**回顾二：local_engine 标识（u2-l1）。** `Initialize` 的第一个参数是形如 `host_ip:port` 的字符串，`port > 0` 表示本实例要作为 server 监听。本讲会看到这个字符串如何一路传递到 `HixlServer`。

**新概念一：抽象基类与纯虚接口。** C++ 中把所有成员函数声明为 `= 0` 的类叫抽象基类，它只规定「必须提供哪些函数」，不提供实现。任何子类必须实现全部纯虚函数才能实例化。这相当于一份强制合同：上层代码只依赖基类指针，不关心底层是哪个引擎。

**新概念二：工厂模式（Factory）。** 「根据运行期条件决定创建哪个子类对象」的逻辑集中到一个静态方法里，这个类就叫工厂。好处是：调用方（`HixlImpl`）完全不知道有几种引擎、按什么规则选，新增引擎类型时上层代码一行不改。

**新概念三：适配器（Adapter）。** 当新接口和旧实现的签名不完全一致时，写一个中间类做「逐参数翻译」。本讲的 `CommEngine` 就是把 HIXL 类型翻译成 ADXL 类型的适配器。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/hixl/engine/engine.h](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/engine.h) | `Engine` 抽象基类，定义全部纯虚接口与 `CallbackProcessor` 类型 |
| [src/hixl/engine/comm_engine.h](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/comm_engine.h) / [comm_engine.cc](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/comm_engine.cc) | `CommEngine`：适配旧 ADXL 内部引擎的 Engine 子类 |
| [src/hixl/engine/engine_factory.h](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/engine_factory.h) / [engine_factory.cc](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/engine_factory.cc) | `EngineFactory`：按选项/芯片类型决定创建哪个引擎 |
| [src/hixl/engine/hixl_engine.h](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_engine.h) / [hixl_engine.cc](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_engine.cc) | `HixlEngine`：基于 HIXL CS 的主力引擎实现 |
| [src/hixl/engine/hixl_server.h](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_server.h) / [hixl_server.cc](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_server.cc) | `HixlServer`：HixlEngine 内部的服务端角色，封装 CS C 接口 |
| [src/hixl/engine/hixl_impl.cc](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_impl.cc) | `Hixl::HixlImpl`：持有 `engine_` 并调用工厂（回顾 u2-l1） |
| [src/hixl/engine/fabric_mem_engine.h](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/fabric_mem_engine.h) | `FabricMemEngine`：第三条引擎分支，单元五详讲 |

## 4. 核心概念与源码讲解

### 4.1 Engine：所有引擎的接口合同

#### 4.1.1 概念说明

`Engine` 是 HIXL Engine 内部的抽象基类（注意：它在 `src/` 下，不是公开 API，用户永远接触不到它）。它存在的意义是：把「上层外壳 `HixlImpl` 需要什么能力」和「底层到底用哪套通信实现」解耦。目前仓库里有三个子类：

- `HixlEngine`：走 HIXL CS 通信服务的主力实现（单元四的主角）；
- `CommEngine`：适配旧 ADXL 内部引擎的兼容实现；
- `FabricMemEngine`：超节点 FabricMem 传输模式（单元五的主角）。

`HixlImpl` 只持有 `Engine*`，对这三个子类一视同仁。

#### 4.1.2 核心流程

`Engine` 的接口清单与 u2-l1 讲过的公开 API 五分组一一对应：

```
生命周期组：  Initialize / Finalize / IsInitialized
内存组：      RegisterMem / DeregisterMem
链路组：      Connect / Disconnect（单个远端、全部远端两个重载）
传输组：      TransferSync / TransferAsync / GetTransferStatus（单个、批量两个重载）
通知组：      SendNotify / GetNotifies
扩展组：      RegisterCallbackProcessor（注册控制面消息回调）
```

也就是说，`Engine` 接口面 ≈ `Hixl` 公开接口面减去静态的 `GetCapability` 和异步建链组（异步建链由 `HixlImpl` 层的 `ConnectPoolExecutor` 实现，不在引擎内）。

#### 4.1.3 源码精读

基类定义非常紧凑，全部是纯虚函数：

- [engine.h:18](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/engine.h#L18) 定义 `CallbackProcessor`：控制面消息回调的统一签名，`(fd, msg, msg_len, keep_fd) -> Status`，`keep_fd` 让回调决定是否保留这条连接。
- [engine.h:20-24](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/engine.h#L20-L24) `Engine` 类声明：构造函数只做一件事——把 `local_engine` 字符串存进 `local_engine_` 成员；虚析构函数 `= default` 保证通过基类指针 `delete` 子类对象时行为正确。
- [engine.h:26-58](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/engine.h#L26-L58) 全部纯虚接口。注意 [engine.h:36-38](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/engine.h#L36-L38) 中 `Connect`/`Disconnect` 都带 `timeout_in_millis`，超时语义由各子类自行实现。
- [engine.h:60-61](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/engine.h#L60-L61) `protected` 成员 `local_engine_`：所有子类共享的本实例标识。

#### 4.1.4 代码实践

**实践目标**：验证「Engine 接口面 ≈ Hixl 公开接口面」这一论断，并找出两处差异。

**操作步骤**：

1. 打开 [include/hixl/hixl.h](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/hixl/hixl.h) 和 [src/hixl/engine/engine.h](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/engine.h) 并排对照。
2. 把 `Hixl` 类的每个非静态成员函数归入上面六组，检查 `Engine` 是否都有对应纯虚函数。
3. 特别留意 `Hixl` 有而 `Engine` 没有的接口（提示：`ConnectAsync`/`DisconnectAsync`/`GetAsyncConnectStatus`/`GetCapability`）。

**需要观察的现象**：异步建链三个接口在 `Engine` 中不存在。

**预期结果**：异步建链确实不在引擎抽象里——它由 `HixlImpl` 持有的 `ConnectPoolExecutor` 用线程池包装同步 `Connect` 实现（u2-l4 已讲），`GetCapability` 则是纯静态查询、根本不需要引擎实例。待本地验证（纯源码阅读，无需硬件）。

#### 4.1.5 小练习与答案

**练习 1**：`Engine` 的构造函数为什么是 `explicit` 且只有一行？

**答案**：`explicit` 防止 `AscendString` 被隐式转换成 `Engine`；构造只保存 `local_engine_` 是因为抽象基类不能被实例化，真正的初始化工作（建线程、开端口）必须推迟到子类的 `Initialize(options)` 中按选项进行——这也解释了为什么接口合同里 `Initialize` 与构造函数分离。

**练习 2**：如果把 `Engine` 的析构函数改成非 virtual 会发生什么？

**答案**：`HixlImpl` 里的 `std::unique_ptr<Engine>` 在析构时通过基类指针销毁子类对象；非 virtual 析构会导致未定义行为（通常只析构基类部分，子类成员如 `HixlServer`、`ClientManager` 泄漏）。[engine.h:24](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/engine.h#L24) 的 `virtual ~Engine() = default` 正是为此。

### 4.2 CommEngine：旧引擎的适配器外衣

#### 4.2.1 概念说明

`CommEngine` 是 `Engine` 的子类，但它**自己不实现任何传输逻辑**——它内部持有一个 `adxl::AdxlInnerEngine`（旧 ADXL 内部引擎，u8-l4 会详细拆解），所有接口都转发过去。它存在的意义是：HIXL 新接口面（`hixl::MemType`、`hixl::TransferOpDesc` 等）与 ADXL 旧类型（`adxl::MemType`、`adxl::TransferOpDesc`）是两套平行定义，需要一个适配层做逐字段翻译。

什么时候会用到它？下一节的工厂逻辑会告诉我们：当没有命中任何「HIXL CS 选择器」时（例如老芯片、旧版 `LocalCommRes` 配置），就退回 `CommEngine` 走旧路径。

#### 4.2.2 核心流程

`CommEngine` 每个接口的处理模式都相同：

```
收到 hixl:: 参数
  → 校验（如 op_descs 非空）
  → 逐字段拷贝/强转成 adxl:: 类型
  → 调用 adxl_inner_engine_ 的同名接口
  → 把 adxl 的返回值/出参翻译回 hixl 类型
```

类型翻译之所以能直接 `static_cast`，是因为 `hixl::MemType`/`TransferOp`/`TransferStatus` 与 `adxl::` 侧的枚举值是一一对应的（u2-l2 讲过这些枚举的定义）。

#### 4.2.3 源码精读

- [comm_engine.h:21-23](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/comm_engine.h#L21-L23) `CommEngine` 继承 `Engine`，构造时同时构造内嵌的 `adxl_inner_engine_`。
- [comm_engine.h:61-62](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/comm_engine.h#L61-L62) 唯一的成员：按值持有的 `adxl::AdxlInnerEngine`。这说明 `CommEngine` 与旧引擎生命周期完全绑定。
- [comm_engine.cc:15-17](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/comm_engine.cc#L15-L17) `Initialize` 只把原始选项透传给旧引擎。
- [comm_engine.cc:49-58](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/comm_engine.cc#L49-L58) `TransferSync` 的典型适配流程：先校验 `op_descs` 非空（返回 `PARAM_INVALID`），再把每个 `hixl::TransferOpDesc` 逐个 `emplace_back` 成 `adxl::TransferOpDesc`，最后转发。
- [comm_engine.cc:81-85](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/comm_engine.cc#L81-L85) 一个重要例外：**批量状态查询 `GetTransferStatus(GetTransferStatusArgs, ...)` 直接返回 `UNSUPPORTED`**——旧引擎没有这个能力，适配器不做模拟。这印证了 u2-l5 讲过的「批量查询是 HixlEngine 的新能力」。
- [comm_engine.cc:64](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/comm_engine.cc#L64) `TransferAsync` 中 `(void)optional_args;`：`TransferArgs`（含 `user_data` 回调上下文）在旧路径上被显式丢弃。

#### 4.2.4 代码实践

**实践目标**：统计 `CommEngine` 的「能力缺口」——哪些 Engine 接口只是转发，哪些被降级或丢弃。

**操作步骤**：

1. 通读 [comm_engine.cc](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/comm_engine.cc) 全文（仅 105 行）。
2. 建一张三列表格：接口 | 处理方式（纯转发 / 带校验转发 / 降级）| 被丢弃或缺失的信息。
3. 重点检查三个接口：`TransferAsync`（`optional_args` 去哪了）、批量 `GetTransferStatus`（返回什么）、`SendNotify`（`NotifyDesc` 的哪些字段被翻译）。

**需要观察的现象**：大部分接口是 1-3 行的纯转发；只有少数接口有校验或降级逻辑。

**预期结果**：能力缺口表大致为——`TransferAsync` 丢弃 `user_data`；批量 `GetTransferStatus` 返回 `UNSUPPORTED`；其余接口语义完整保留。待本地验证（源码阅读型实践）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `CommEngine` 不把 `hixl::TransferOpDesc` 直接 `reinterpret_cast` 成 `adxl::TransferOpDesc`，而要逐个构造？

**答案**：两套 `TransferOpDesc` 是不同头文件里的独立类型，字段碰巧相同（`local_addr/remote_addr/len`），但 C++ 标准不保证布局不同的类能安全互转；逐字段构造是唯一可移植的写法，也让字段映射关系显式可见、便于审计。

**练习 2**：如果用户在走 `CommEngine` 的机器上调用批量 `GetTransferStatus`，会得到什么？

**答案**：接口本身返回 `hixl::UNSUPPORTED`（不是 `SUCCESS`），出参 `results` 不被填写。用户程序应当同时判断返回码，而不是只看 `status == SUCCESS`（u2-l2 的错误码判断规则）。

### 4.3 EngineFactory：引擎选择的唯一决策点

#### 4.3.1 概念说明

`EngineFactory` 只有一个静态方法 `CreateEngine`，它是「用户传入的选项字符串」到「具体 Engine 对象」的唯一映射点。这个设计把三条决策链集中在一处：

1. **显式选项优先**：用户设置了 `EnableFabricMem` 就直接走 `FabricMemEngine`。
2. **通信资源配置**：`LocalCommRes` 的 JSON 版本号决定新旧路径；配置了 `protocol_desc` 走 HIXL CS。
3. **芯片代际探测**：以上都没命中时，探测 SoC 类型，`kV5` 走 `HixlEngine`，其余退回 `CommEngine`。

每次选择都会通过 `LogSelectedEngine` 打一条事件日志（`[EngineFactory] selected engine:xxx, reason:xxx`），这是排查「为什么我的程序走了旧引擎」的第一入口。

#### 4.3.2 核心流程

`CreateEngine` 的决策流程可以画成一棵按序判断的决策树（自上而下短路）：

```
CreateEngine(local_engine, options, parsed_options)
│
├─ ① HixlOptions::Parse 解析全部选项 ──── 失败 → 返回 nullptr
│
├─ ② EnableFabricMem == true?
│      是 → FabricMemEngine
│
├─ ③ LocalCommRes 配置存在?
│      是 → 解析 JSON：
│            version == "1.3" → HixlEngine
│            其他 version     → CommEngine
│            JSON 非法        → 返回 nullptr
│
├─ ④ GlobalResourceCfg.protocol_desc 非空?
│      是 → HixlEngine
│
├─ ⑤ GetSocType() 成功且 == kV5?
│      是 → HixlEngine
│
└─ ⑥ 兜底 → CommEngine
```

注意分支②的优先级最高：FabricMem 是显式开关，一旦打开即使其他条件都满足 HIXL CS 也不会走。分支③中 `LocalCommRes` 的 `version` 字段是通信资源配置的版本号，`1.3` 代表新一代 HIXL CS 资源模型。

#### 4.3.3 源码精读

- [engine_factory.h:21-26](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/engine_factory.h#L21-L26) 工厂本体：静态方法、返回 `unique_ptr<Engine>`、出参 `HixlOptions &parsed_options` 把解析结果带回给调用方（`HixlImpl` 随后用它初始化连接池）。
- [engine_factory.cc:38-45](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/engine_factory.cc#L38-L45) 入口：先 `HixlOptions::Parse`（u2-l1 讲过的五步解析），失败即返回空指针，此时 `HixlImpl` 会报「Created engine is null」。
- [engine_factory.cc:47-50](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/engine_factory.cc#L47-L50) 分支②：FabricMem 显式开关，优先级最高。
- [engine_factory.cc:51-65](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/engine_factory.cc#L51-L65) 分支③：`LocalCommRes` JSON 解析，`version == "1.3"` 走 `HixlEngine`，否则走 `CommEngine`；JSON 异常被捕获并返回 nullptr（不会抛出 C++ 异常穿透到用户代码）。
- [engine_factory.cc:24-31](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/engine_factory.cc#L24-L31) 辅助函数 `UseProtocolDesc`：检查 `GlobalResourceCfg` 里是否配置了非空 `protocol_desc`（u1-l5 讲过的 `--protocol` 参数最终落到这里）。
- [engine_factory.cc:66-69](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/engine_factory.cc#L66-L69) 分支④：配置了协议描述符即选择 HIXL CS。
- [engine_factory.cc:70-76](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/engine_factory.cc#L70-L76) 分支⑤⑥：`GetSocType` 探测芯片（来自 `common/hixl_utils.h`），`kV5` 命中 `HixlEngine`；兜底 `CommEngine`。
- [engine_factory.cc:33-36](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/engine_factory.cc#L33-L36) `LogSelectedEngine`：每个分支返回前都会打 `HIXL_EVENT` 日志，附选择原因和 IntraRoce 状态。

#### 4.3.4 代码实践

**实践目标**：在不运行程序的前提下，推演四种不同配置分别会命中哪个分支，并给出验证方法。

**操作步骤**：

1. 准备四个假设的 `Initialize` 选项组合：
   - A：`{hixl.fabric_mem: "true"}` + 芯片是 kV5；
   - B：`local_comm_res` JSON 的 `version` 为 `"1.0"`；
   - C：`global_resource_config` 中 `protocol_desc` 为 `["hccs"]`；
   - D：不配置任何上述选项，芯片探测返回 kV5。
2. 对照 4.3.2 的决策树逐一写出命中的分支编号与最终引擎类型。
3. 验证方法：在真实环境运行任意 HIXL 样例（如 quickstart），从日志中过滤 `[EngineFactory] selected engine` 关键字，比对 `reason` 字段是否与你的推演一致。

**需要观察的现象**：日志形如 `selected engine:hixl_cs, reason:SoC type matched hixl_cs, local_engine:...`。

**预期结果**：A→FabricMemEngine（分支②，FabricMem 优先级高于 SoC 判断）；B→CommEngine（version 非 1.3）；C→HixlEngine（protocol_desc）；D→HixlEngine（kV5 命中）。日志验证需要昇腾环境，待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `CreateEngine` 把解析好的 `HixlOptions` 作为出参传回，而不是让 `HixlImpl` 自己再调一次 `HixlOptions::Parse`？

**答案**：选项解析只应发生一次（解析本身可能失败，重复解析既浪费又可能产生两次失败日志）；工厂在创建对象前必须先知道选项内容才能选择分支，因此解析结果天然在工厂手里，通过出参带回是最简单的复用方式。

**练习 2**：分支③中 JSON 解析失败为什么返回 nullptr 而不是降级到 `CommEngine`？

**答案**：`LocalCommRes` 是用户显式提供的通信资源配置，内容非法说明配置本身有错误；静默降级会让程序以用户未预期的引擎运行，违背最小惊讶原则。返回 nullptr 后 `HixlImpl::Initialize` 会失败并提示检查参数，把问题暴露在初始化阶段。

### 4.4 HixlEngine：主力引擎与它的双角色

#### 4.4.1 概念说明

`HixlEngine` 是基于 HIXL CS 通信服务的 `Engine` 实现，也是当前新部署形态下的默认主力。它最重要的设计是**双角色合一**：

- 对外提供主动方能力：持有 `ClientManager`（u2-l4 讲过），管理到所有远端的 `HixlClient`；
- 同时承担被动方职责：持有 `HixlServer`，当 `local_engine` 带端口时监听并服务远端的建链请求。

也就是说，「server / client」不是两种进程模式，而是同一个 `HixlEngine` 实例内的两个组成部分——是否真的监听端口，由 `local_engine` 字符串里有没有端口决定。这解释了 quickstart 样例里双方调用同一套 API 的现象（u1-l3）。

#### 4.4.2 核心流程

`HixlEngine::Initialize` 的执行顺序（任一步失败即回滚已完成的步骤）：

```
加锁
① CheckSupportedOptions     校验选项都在支持列表内
② BuildEndpointList         由选项生成 endpoint 列表（u3-l3 详讲）
③ 读取 GlobalResourceCfg    listen_port / qos / max_active_channels
④ CreateContext             创建可选的 aclrt 上下文（失败有 guard 自动回滚）
⑤ InitServer                解析 local_engine → 启动 HixlServer（端口>0 才注册处理器并监听）
⑥ 初始化 RDMA QoS 参数、auto_connect 开关
⑦ ClientManager::Initialize
⑧ is_initialized_ = true
```

其中 `InitServer` 内部先调 `ParseListenInfo` 把 `local_engine_` 拆成 ip 和 port（格式不对直接报错并提示正确格式），再交给 `HixlServer::Initialize`。

#### 4.4.3 源码精读

- [hixl_engine.h:28-36](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_engine.h#L28-L36) 类声明与构造函数注释：注释明确了 `local_engine` 的 ipv4/ipv6 两种格式，以及「设置 port 且 > 0 即为 server」的语义——这是 u2-l1 结论的源头。
- [hixl_engine.h:167-180](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_engine.h#L167-L180) 成员一览：`client_manager_`（第 170 行）与 `server_`（第 171 行）并列，直观体现双角色；`mem_map_`（第 172 行）是本端注册内存台账（u2-l3 讲过）；`aclrt_context_`（第 180 行）是可选的 ACL 运行时上下文。
- [hixl_engine.cc:42-54](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_engine.cc#L42-L54) `InitServer`：`ParseListenInfo` 失败时的错误消息把合法格式完整打印出来，是初学者最常遇到的报错之一。
- [hixl_engine.cc:56-99](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_engine.cc#L56-L99) `Initialize` 全流程：第 59 行选项白名单校验；第 64-67 行对 `OPTION_BUFFER_POOL` 的特殊约束（HixlEngine 只支持 `0:0`）；第 69 行构建 endpoint 列表；第 82-89 行在 aclrt 上下文保护下调用 `InitServer`；第 93 行初始化 ClientManager；第 95 行置位原子标志 `is_initialized_`。
- [hixl_engine.cc:38-40](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_engine.cc#L38-L40) `IsInitialized` 用 relaxed 原子读，供 `HixlImpl` 的门卫检查高频调用。

#### 4.4.4 代码实践

**实践目标**：通过一次故意输错的 `local_engine`，验证 `ParseListenInfo` 的报错路径与提示格式。

**操作步骤**：

1. 取 quickstart 样例（u1-l3），把 server 侧 `Initialize` 的第一个参数从 `127.0.0.1:26000` 改成非法格式（如 `127.0.0.1:port` 或 `[1.2.3.4]` 少了内容的形式）。
2. 重新编译运行，观察 stderr 日志。
3. 对照 [hixl_engine.cc:45-50](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_engine.cc#L45-L50) 的错误消息文本。

**需要观察的现象**：初始化立即失败，日志中包含 `Failed to parse ip and port` 与两种合法格式的说明。

**预期结果**：错误消息与本讲引用的源码字符串一致，`Initialize` 返回 `PARAM_INVALID`，程序不会进入半初始化状态。需要昇腾环境运行，待本地验证；无环境时可仅做源码走读。

#### 4.4.5 小练习与答案

**练习 1**：`HixlEngine` 里 `server_` 是按值成员而不是指针，这带来什么顺序保证？

**答案**：C++ 成员按声明逆序析构：`aclrt_context_`（最后声明）最先销毁，随后是 `mem_map_`、`server_`、`client_manager_`，而最先声明的 `mutex_` 最后销毁。这带来两点：其一，`mutex_` 的生命周期覆盖所有其他成员，析构期间加锁不会碰到已销毁的锁；其二，主动方 `client_manager_` 在被动方 `server_` 之后销毁，与正常退出时「先断链路、再关服务」的清理方向一致。真正的有序清理主要靠显式调用 `Finalize`（或 `HixlImpl` 失败回滚）完成，而不依赖默认析构。

**练习 2**：为什么第 82 行要专门 `CreateContext` 并用 guard 包住 `InitServer`？

**答案**：HIXL CS 初始化过程中会调用 ACL/驱动接口，这些接口往往依赖当前线程的 aclrt 上下文；`GetContextGuard()` 保证 `InitServer` 执行期间上下文就绪。guard（`HIXL_DISMISSABLE_GUARD`）的作用是：若后续步骤失败，析构时自动 `DestroyContext` 回滚，不留半初始化资源——与 `HixlImpl::Initialize` 失败回滚（u2-l1）同一思想。

### 4.5 HixlServer：被动方的全部实现

#### 4.5.1 概念说明

`HixlServer` 不是 `Engine` 的子类，而是 `HixlEngine` 的一个组成部分，代表「被连接、被读取」的被动方角色。它做四件事：

1. 封装 CS 层的 C 接口（`HixlCSServerCreate/RegMem/UnregMem/Destroy/RegProc/Listen`，定义在 `include/cs/hixl_cs.h`）；
2. 维护本端注册内存台账 `handle_to_addr_`，支持重复注册幂等与区间重叠检查；
3. 注册控制面消息处理器（endpoint 信息、内存信息、心跳、notify）；
4. 缓存收到的 notify 消息，等待用户 `GetNotifies` 取走。

u2-l3 讲过的「server 侧区间来自建链导入、幂等」，其实现细节就在这个类里。

#### 4.5.2 核心流程

`HixlServer::Initialize` 的流程：

```
① 把 EndpointConfig 列表转换成 EndpointDesc（供 CS 层使用）
② port < 0 时归零（0 表示由系统自动分配）
③ 若指定了 listen_port / max_active_channels，组装成 JSON 的
   global_resource_config 传给 CS 层
④ HixlCSServerCreate 创建底层 server 句柄
⑤ 仅当 port > 0：RegisterProcessors() 注册四类消息处理器并 Listen
```

`RegisterProcessors` 注册的四类处理器：

| CtrlMsgType | 处理器行为 |
| --- | --- |
| `kGetEndpointInfoReq` | 序列化本端 endpoint 配置列表回给请求方（client 建链时先问这个） |
| `kGetMemInfoReq` | 序列化本端已注册内存列表回给请求方 |
| `kHeartBeat` | 空实现直接返回 SUCCESS（u2-l4 讲过的探活心跳） |
| `kNotify` | 解析消息、入队（上限检查）、回 Ack |

#### 4.5.3 源码精读

- [hixl_server.h:24-37](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_server.h#L24-L37) 类声明与 `Initialize` 签名：ip、port、endpoint 配置列表，可选的 listen_port 与 max_active_channels。
- [hixl_server.h:86-91](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_server.h#L86-L91) 成员：`server_handle_`（CS 层不透明句柄）、`handle_to_addr_`（内存台账）、`notify_messages_`（notify 队列）。
- [hixl_server.cc:61-100](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_server.cc#L61-L100) `Initialize` 全流程；第 94 行创建 server，第 96-98 行仅在 `port > 0` 时注册处理器——纯 client 角色（`local_engine` 不带端口）也会创建 server 对象但不监听。
- [hixl_server.cc:102-130](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_server.cc#L102-L130) `RegisterMem`：地址溢出检查（`AddOverflow`）→ 重叠/重复检查 → 重复注册直接返回已有 handle（幂等）→ `HixlCSServerRegMem` 真正注册 → 记入台账。
- [hixl_server.cc:148-164](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_server.cc#L148-L164) `Finalize`：先注销所有注册内存（失败仅记日志不中断），再销毁 server 句柄——「先内存后连接」的清理顺序。
- [hixl_server.cc:255-303](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_server.cc#L255-L303) `RegisterProcessors`：注册上表四类处理器，最后 `HixlCSServerListen` 开始监听，backlog 常量 `kDefaultBackLog = 1024`（[hixl_server.cc:27](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_server.cc#L27)）。
- [hixl_server.cc:193-239](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_server.cc#L193-L239) `ProcessNotifyMsg`：u2-l5 讲过的「队列上限 4096 / 字段上限 1024」检查就实现于此（`kMaxNotifyQueueSize`/`kMaxNotifyNameLen`/`kMaxNotifyMsgLen` 来自 `common/hixl_inner_types.h`），检查完无论成败都同步回 `kNotifyAck`。

#### 4.5.4 代码实践

**实践目标**：把「server 端收到 client 建链请求后的控制面交互」整理成消息流转表。

**操作步骤**：

1. 通读 [hixl_server.cc](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_server.cc) 的 `RegisterProcessors` 与 `ProcessNotifyMsg`。
2. 结合 [common/ctrl_msg.h](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/common/ctrl_msg.h) 中的 `CtrlMsgType` 枚举，列出每种消息的方向（client→server 还是 server→client）、触发时机、应答类型。
3. 注意所有应答的共同格式：`CtrlMsgHeader`（含 magic 与 body_size）+ `CtrlMsgType` + JSON body，均通过 `CtrlMsgPlugin::Send` 分三次发送。

**需要观察的现象**：控制面所有消息都是「请求-应答」成对出现的，且 heartbeat 是唯一无应答的消息。

**预期结果**：得到一张约 5 行的消息流转表（kGetEndpointInfoReq/Resp、kGetMemInfoReq/Resp、kHeartbeat、kNotify/kNotifyAck），与 u2-l4「控制面 TCP socket」的结论互相印证。待本地验证（源码阅读型实践）。

#### 4.5.5 小练习与答案

**练习 1**：`local_engine` 不带端口的实例也会执行 `HixlCSServerCreate`，这是浪费吗？

**答案**：不是。CS server 句柄还承担本端内存注册（`HixlCSServerRegMem`）等职责，纯 client 角色同样需要注册本地内存供对端读取；只是不注册消息处理器、不监听端口，即不承担被动连接职责。

**练习 2**：`HixlServer::Finalize` 里注销内存失败时为什么只记日志、继续清理，而不是直接返回错误？

**答案**：Finalize 的目标是尽可能释放资源（best-effort）。若第一条注销失败就返回，后面的内存和 server 句柄都不会被释放，造成泄漏扩大；把失败记录下来供排查、继续走完清理链路，是析构类函数的通用写法。

## 5. 综合实践

**任务：绘制 `Hixl::Initialize` 到具体 Engine 对象的完整时序图，并标注工厂选择分支。**

这是本讲规格中指定的实践任务，把五个最小模块串成一条线。步骤：

1. **梳理参与者**。从上到下五层：用户代码 → `Hixl`（公开类，[hixl_impl.cc:241-245](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_impl.cc#L241-L245)）→ `Hixl::HixlImpl`（[hixl_impl.cc:82-100](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_impl.cc#L82-L100)）→ `EngineFactory` → 具体引擎（`HixlEngine`，内部再调 `HixlServer`；或 `CommEngine`/`FabricMemEngine`）。
2. **画出主干时序**（以 `HixlEngine` 分支为例，可用 mermaid 或手绘）：

```
用户          Hixl        HixlImpl      EngineFactory     HixlEngine      HixlServer
 │ Initialize  │             │               │               │              │
 │────────────>│  创建impl    │               │               │              │
 │             │────────────>│ CreateEngine  │               │              │
 │             │             │──────────────>│ Parse 选项     │              │
 │             │             │               │ 分支②③④⑤判定  │              │
 │             │             │   unique_ptr<Engine>          │              │
 │             │             │<──────────────│ (HixlEngine)  │              │
 │             │             │ engine_->Initialize           │              │
 │             │             │──────────────────────────────>│ InitServer    │
 │             │             │                               │─────────────>│ CSServerCreate
 │             │             │                               │              │ RegProc×4 + Listen
 │             │             │ connect_pool_executor_.Initialize             │
 │<────────────│  SUCCESS    │               │               │              │
```

3. **标注选择分支**。在 `CreateEngine` 的返回边上标注命中的分支与原因（FabricMem 开关 / LocalCommRes version=1.3 / protocol_desc / kV5 / 兜底 CommEngine）。
4. **标注失败回滚边**：`engine_->Initialize` 失败或连接池初始化失败时，`HixlImpl` 会 `engine_->Finalize()` 并 `engine_.reset()`（[hixl_impl.cc:92-98](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_impl.cc#L92-L98)）。
5. **（可选，需环境）实测验证**：跑通 quickstart 后过滤日志 `grep "selected engine" `，把你观察到的 reason 写进时序图的分支注释里；无硬件环境则整图基于源码完成即可，并注明「日志部分待本地验证」。

**产出物**：一张时序图 + 一段不超过 10 行的文字说明，解释为什么「选择引擎」这一步必须发生在 `HixlImpl` 持锁期间（提示：防止两个线程并发 Initialize 创建出两个引擎）。

## 6. 本讲小结

- `Engine` 是 `src/` 内部的抽象基类，接口面与公开 API 五分组一一对应；异步建链与 `GetCapability` 不在其中，分别由 `ConnectPoolExecutor` 和静态查询承担。
- `CommEngine` 是旧 ADXL 内部引擎的适配器，逐字段翻译类型；批量 `GetTransferStatus` 返回 `UNSUPPORTED`、`TransferArgs` 被丢弃，是其能力缺口。
- `EngineFactory::CreateEngine` 是引擎选择的唯一决策点，分支顺序为 FabricMem 开关 → LocalCommRes version==1.3 → protocol_desc → SoC kV5 → 兜底 CommEngine，每次选择都打 `selected engine` 事件日志。
- `HixlEngine` 双角色合一：按值持有 `HixlServer`（被动方）与 `ClientManager`（主动方），是否监听由 `local_engine` 是否带端口决定。
- `HixlServer` 封装 CS 层 C 接口，负责内存台账（幂等 + 重叠检查）、四类控制面消息处理器与 notify 队列。
- 工厂 + 抽象基类的组合让 `HixlImpl` 对引擎种类零感知，新增引擎（如 `FabricMemEngine`）不影响上层一行代码。

## 7. 下一步学习建议

下一讲 **u3-l2 ClientHandler：不同链路的传输处理策略** 将深入主动方：`ClientManager` 管理的每条 `HixlClient` 如何把传输请求分派给 `DirectClientHandler` 或 `UBClientHandler`。建议先自行浏览 [src/hixl/engine/client_handler.h](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/client_handler.h) 建立印象。若对被动方的 CS 层更感兴趣，也可以先跳到单元四（u4-l1 CS 通信服务总体架构），看 `HixlCSServerCreate` 背后的完整实现；对 FabricMem 分支好奇的读者可预习单元五。
