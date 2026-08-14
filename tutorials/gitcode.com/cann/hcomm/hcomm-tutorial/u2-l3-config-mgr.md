# u2-l3 配置管理机制：HcclConfig 与配置解析

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 HCOMM 中三条配置通路分别是什么、各在什么时机生效：通信域初始化时的 `HcclCommConfig`、运行期的 `HcclSetConfig/HcclGetConfig`、通道级的 `HcclChannelConfig`。
2. 读懂 `CommConfig` 类的「版本协商 + 尾部追加」设计：为什么 `SetConfigByVersion` 要按版本号逐段读取配置。
3. 跟踪一个配置项（如确定性计算开关 `HCCL_DETERMINISTIC`）从用户接口一直到实际生效点的完整调用链。
4. 理解新架构（V2 / A5）下 `config_mgr` 模块的 `ApplyHcclCommConfig` 如何把对外配置结构体翻译成内部 `CommConfig`。

本讲承接 u2-l2：你已经知道 `CollComm` 是通信域上下文的聚合点，其成员中就有一个 `CommConfig config_`（[src/coll_communicator_mgr/communicator/coll_comm.h:63](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/communicator/coll_comm.h#L63)、[src/coll_communicator_mgr/communicator/coll_comm.h:153](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/communicator/coll_comm.h#L153)）。本讲就专门回答：这个 `CommConfig` 从哪里来、怎么解析、以及运行期还能怎么改。

## 2. 前置知识

- **配置结构体的 ABI 兼容**（u1-l4 已讲）：`HcclCommConfig` 头部 24 字节是 `size/magic/version` 协商区，新字段只能追加在结构体尾部，未设置的项保持 `0xffffffff` 哨兵值。本讲的 `CommConfig::Load` 正是消费这套协商约定的「读侧」。
- **magic word（魔数）**：结构体头部放一个约定的常量（这里是 `0xf0f0f0f0`），用于判断调用方是否用初始化函数正确地初始化过这块内存，防止传入野指针或未初始化的栈内存被误当配置解析。
- **版本协商（version negotiation）**：新旧进程/库各自认识的结构体版本可能不同。约定「头部声明自己写的版本号，读取方按版本号逐段读取」，就能做到「新库读旧配置不越界、旧库读新配置不误解」。
- **确定性计算（deterministic）**：集合通信算子内部常有异步多线程/多路径归约，浮点数累加顺序不同会导致多次执行结果有微小差异。开启确定性计算后，相同硬件与输入下结果严格一致，但通常牺牲性能。取值：0 关闭、1 开启、2 严格（规约保序，仅部分芯片支持）。
- **不透明句柄（opaque handle）**：对外头文件只暴露一个 `void*` 别名（如 `HcclChannelConfig`），真正的 C++ 结构体藏在库内部。调用方只能通过 Create/Set/Destroy 一组接口操作它，库可以自由改内部布局而不破坏 ABI。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `include/hccl/hccl_types.h` | 对外类型：`HcclConfig` 枚举、`HcclConfigValue` 联合体、`HCCL_COMM_CONFIG_VERSION` 等常量 |
| `include/hccl/hccl_comm.h` | 对外 C 接口：`HcclSetConfig` / `HcclGetConfig` 弱符号声明 |
| `src/legacy/ascend910/framework/inc/comm_config_pub.h` | **核心**：`CommConfig` 类、`CommConfigHandle`（内部配置视图）、版本枚举 |
| `src/legacy/ascend910/framework/communicator/comm_config.cc` | `CommConfig::Load / CheckMagicWord / SetConfigByVersion / SetConfigDeterministic` 等实现 |
| `src/legacy/ascend910/framework/inc/comm_configer.h` | `CommConfiger` 单例：按通信域 identifier 存放 `CommConfig` |
| `src/coll_communicator_mgr/config_mgr/coll_comm_config.h/.cc` | 新架构（V2）配置翻译层：`ApplyHcclCommConfig` |
| `src/coll_communicator_mgr/api_c_adpt/hccl_channel_config.h/.cc` | Channel 级配置对象 `HcclChannelConfig` 的实现 |
| `src/coll_communicator_mgr/api_c_adpt/coll_comm_res_c_adpt.cc` | `HcclChannelAcquireWithConfig` 如何消费 Channel 配置 |
| `src/legacy/ascend910/framework/op_base/src/op_base.cc` | `HcclSetConfig/HcclGetConfig` 强符号实现（V1 主入口） |
| `src/legacy/ascend950/framework/entrance/op_base/op_base_v2.cc` | V2 分支的 `HcclSetConfigV2/HcclGetConfigV2` |

一个容易混淆的点先说清楚：`CommConfig` 类物理上位于 `src/legacy/ascend910/framework/` 下，但它是**新老架构共用的基础设施**——新架构 `CollComm` 的 `config_` 成员就是它，`config_mgr/coll_comm_config.cc` 的 `ApplyHcclCommConfig` 最终写入的也是它。不要因为它在 legacy 目录就以为只有老芯片才用。

## 4. 核心概念与源码讲解

### 4.1 配置体系全景：三条配置通路

#### 4.1.1 概念说明

HCOMM 的「配置」不是一个大杂烩，而是按**生效时机与作用范围**分成三条独立通路：

| 通路 | 接口 | 生效时机 | 作用范围 |
| --- | --- | --- | --- |
| 初始化配置 | `HcclCommInitRootInfoConfig(config)` | 通信域创建时一次性读入 | 整个通信域生命周期（buffer 大小、算法串、TC/SL、QoS 等） |
| 运行期配置 | `HcclSetConfig / HcclGetConfig` | 通信域已建好后随时可调 | 当前仅 `HCCL_DETERMINISTIC` 一个开关，进程级生效 |
| 通道配置 | `HcclChannelConfigCreate + HcclChannelAcquireWithConfig` | 每次创建 channel 时 | 单次 channel 创建（共享 Jetty 队列等高级选项） |

#### 4.1.2 核心流程

```text
用户程序
  │
  ├─ 路径 A：HcclCommInitRootInfoConfig(&config)
  │     └→ CommConfig::Load() 解析 → 存入 CommConfig/CommConfiger
  │           └→ (V2 新架构) 先经 ApplyHcclCommConfig 翻译
  │
  ├─ 路径 B：HcclSetConfig(HCCL_DETERMINISTIC, value)
  │     └→ SetDeterministic(全局开关) → 遍历所有已建通信域 →
  │        hcclComm::SetDeterministicConfig → HcclCommunicator → HcclAlg → TopoMatcher
  │
  └─ 路径 C：HcclChannelConfigCreate → SetInt/SetStr →
        HcclChannelAcquireWithConfig(..., config, ...) → 用完 Destroy
```

#### 4.1.3 源码精读

先看对外接口的样子。`HcclSetConfig` 与 `HcclGetConfig` 在对外头文件中声明为弱符号（u2-l1 讲过弱符号机制）：

- [include/hccl/hccl_comm.h:104-112](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/include/hccl/hccl_comm.h#L104-L112)：声明 `HcclSetConfig/HcclGetConfig`，注释写明用途是 "Set deterministic calculate"（设置确定性计算）。

配置项枚举与取值容器定义在类型头文件中：

- [include/hccl/hccl_types.h:103-110](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/include/hccl/hccl_types.h#L103-L110)：`HcclConfig` 枚举目前只有 `HCCL_DETERMINISTIC = 0` 一项；`HcclConfigValue` 是个只含 `int32_t value` 的联合体。用「枚举 + 联合体」而不是一堆散接口，是为了将来新增配置项时不加新符号。
- [include/hccl/hccl_types.h:126-128](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/include/hccl/hccl_types.h#L126-L128)：`HCCL_COMM_CONFIG_MAGIC_WORD = 0xf0f0f0f0`、`HCCL_COMM_CONFIG_VERSION = 11`——对外承诺的当前配置结构体版本。

#### 4.1.4 代码实践

**实践目标**：建立「接口 → 枚举 → 文档」的对照能力。

1. 打开 [docs/zh/api_ref/comm_mgr_c/HcclSetConfig.md](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/docs/zh/api_ref/comm_mgr_c/HcclSetConfig.md)，阅读「功能说明」与「调用示例」。
2. 对照 `hccl_types.h:103-110`，确认文档示例中的 `configValue.value = 1` 正对应枚举值「1 = deterministic」。
3. 注意文档中「产品支持情况」一节：Ascend 950PR/950DT **不支持**该接口（A5 默认开启确定性，详见 4.4.3）。

**预期结果**：能回答「为什么 `HcclConfigValue` 是联合体但只有一个成员」——为未来扩展预留 ABI 空间。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `HcclConfig` 用枚举 + 联合体，而不是为每个配置项设计一个独立函数（如 `HcclSetDeterministic()`）？

**答案**：接口符号数量固定。新增配置项只改枚举值，不新增动态库导出符号，调用方用同一对 `HcclSetConfig/HcclGetConfig` 即可，避免符号表膨胀，也便于 HCCL 算子层通过 dlsym 按固定名字加载。

**练习 2**：`HcclGetConfig` 只传 `configValue` 指针而不返回值本身，为什么？

**答案**：返回值位置已被 `HcclResult` 占用（C 接口惯例），输出参数是 C 语言中返回多值的通用手法；同时 `HcclGetConfig` 内部会先做空指针检查（`CHK_PTR_NULL(configValue)`，见 op_base.cc:2332）。

---

### 4.2 CommConfig：内部配置视图与版本协商

#### 4.2.1 概念说明

用户传入的 `HcclCommConfig` 是「对外 ABI 结构体」，出于兼容性考虑不能直接在库内到处传。库内使用两个内部结构承接它：

- `CommConfigInfo`：头部 24 字节协商区（size / magic / version / reserved）的内部视图。
- `CommConfigHandle`：对外结构体的**内部完整视图**，把所有合法字段都「翻译」成有名字的成员。

而 `hccl::CommConfig` 类则是真正的配置载体：它把 `CommConfigHandle` 中的原始值做合法性校验后，存为带默认值的 C++ 成员（如 `bufferSize_`、`deterministic_`），并对外提供成对的 `GetConfigXxx/SetConfigXxx` 访问器。默认值来自环境变量（如 `HCCL_DETERMINISTIC`），用户显式配置则覆盖默认。

#### 4.2.2 核心流程

`CommConfig::Load` 的处理流程：

1. 从用户结构体头部读出 `configSize`；
2. 与 `sizeof(CommConfigHandle)` 比较：**大于则截断**（用户是更新的库、结构体更长，只取自己认识的前半段）、**小于则告警**（用户是更老的库，尾部字段按默认值处理）；
3. 按截断后的长度 `memcpy_s` 拷入内部 `CommConfigHandle`；
4. `CheckMagicWord`：校验魔数是否为 `0xf0f0f0f0`，不是则报 `EI0003` 输入参数错误；
5. `SetConfigByVersion`：按 `version` 逐段读取，版本号每升高一档多读一批尾部字段；
6. 打印配置汇总日志。

#### 4.2.3 源码精读

内部视图与版本枚举的定义：

- [src/legacy/ascend910/framework/inc/comm_config_pub.h:23-35](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/legacy/ascend910/framework/inc/comm_config_pub.h#L23-L35)：`CommConfigVersion` 枚举列出 v1~v11。每一档版本对应一批新配置项，是理解「配置演进史」的索引。
- [src/legacy/ascend910/framework/inc/comm_config_pub.h:46-51](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/legacy/ascend910/framework/inc/comm_config_pub.h#L46-L51)：`CommConfigInfo`，即头部 24 字节协商区。
- [src/legacy/ascend910/framework/inc/comm_config_pub.h:53-74](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/legacy/ascend910/framework/inc/comm_config_pub.h#L53-L74)：`CommConfigHandle`，字段从 v1 的 `bufferSize/deterministic`（紧跟头部）一路追加到 v11 的 `sqDepth`（结构体末尾）——**字段顺序就是版本顺序**，这正是尾部追加式 ABI 演进的直观体现。

`CommConfig` 类骨架（访问器非常规整，一个字段一对 Get/Set）：

- [src/legacy/ascend910/framework/inc/comm_config_pub.h:77-117](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/legacy/ascend910/framework/inc/comm_config_pub.h#L77-L117)：`CommConfig` 类公有接口，`Load` 是唯一入口。
- [src/legacy/ascend910/framework/inc/comm_config_pub.h:138-139](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/legacy/ascend910/framework/inc/comm_config_pub.h#L138-L139)：`deterministic_` 成员，注释写明三档语义（0 关闭 / 1 开启 / 2 开启且规约保序）。

加载与校验的实现：

- [src/legacy/ascend910/framework/communicator/comm_config.cc:87-131](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/legacy/ascend910/framework/communicator/comm_config.cc#L87-L131)：`CommConfig::Load`，完整呈现「读 size → 截断/告警 → 拷贝 → 校验魔数 → 按版本解析 → 打日志」六步。
- [src/legacy/ascend910/framework/communicator/comm_config.cc:133-149](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/legacy/ascend910/framework/communicator/comm_config.cc#L133-L149)：`CheckMagicWord`，魔数不对时报 `EI0003` 并提示「请确认已用 HcclCommConfigInit 初始化」。
- [src/legacy/ascend910/framework/communicator/comm_config.cc:151-245](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/legacy/ascend910/framework/communicator/comm_config.cc#L151-L245)：`SetConfigByVersion`，一组 `if (version >= Vx)` 阶梯：v1 读 bufferSize/deterministic，v2 读 commName，v4 读 opExpansionMode，v5 读 TC/SL，v8 读 execTimeOut/hcclAlgo/retry，v10 读 hcclQos/symmetricMemoryStride，v11 读 sqDepth。

确定性配置项的单项校验：

- [src/legacy/ascend910/framework/communicator/comm_config.cc:273-311](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/legacy/ascend910/framework/communicator/comm_config.cc#L273-L311)：`SetConfigDeterministic`。三条分支：未设置（哨兵值 `0xffffffff`）→ 沿用环境变量默认值；取值 > 2 → 报 `HCCL_E_PARA`；取值 = 2（strict）→ 额外查询设备类型，非 `DEV_TYPE_910B` 直接拒绝。这就是「配置校验」在这一层做的典型样例：**取值合法性 + 硬件能力双重检查**。

`CommConfig::Load` 的调用点在入口层（每次带 config 的通信域初始化都会走到）：

- [src/legacy/ascend910/framework/op_base/src/op_base.cc:2207](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/legacy/ascend910/framework/op_base/src/op_base.cc#L2207)：`HcclCommInitRootInfoConfig` 的初始化流程中调用 `commConfig.Load(config)`（同类调用还有 op_base.cc:874、971、1062、1351，对应 cluster info、多设备等不同初始化入口）。

解析完的 `CommConfig` 会被登记到单例 `CommConfiger`（按通信域 identifier 存放，供算子执行期按通信域查询算法/重试等配置）：

- [src/legacy/ascend910/framework/communicator/hccl_comm.cc:142](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/legacy/ascend910/framework/communicator/hccl_comm.cc#L142)、[src/legacy/ascend910/framework/communicator/hccl_comm.cc:199](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/legacy/ascend910/framework/communicator/hccl_comm.cc#L199)：`CommConfiger::GetInstance().SetCommConfig(commConfig, identifier_)`，两条初始化路径都会注册。
- [src/legacy/ascend910/framework/inc/comm_configer.h:24-47](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/legacy/ascend910/framework/inc/comm_configer.h#L24-L47)：`CommConfiger` 单例，内部是 `unordered_map<string, CommConfig>` 加一把互斥锁——配置表是**通信域粒度**的。

#### 4.2.4 代码实践

**实践目标**：通过单元测试反推配置解析行为。

1. 打开 [test/ut/platform/hcom/ut_comm_config.cc](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/test/ut/platform/hcom/ut_comm_config.cc)。
2. 重点阅读其中的 `CommConfigTest` 用例（如 `ApplyHcclCommConfig_InvalidTrafficClass_ReturnParaError`、`ApplyHcclCommConfig_QosVersionBelow10_SkipQos` 等，见该文件 481 行起）。
3. 记录每个用例的「输入构造方式 + 断言结果」，回答：非法 TC（不是 4 的倍数）时返回什么错误码？版本号低于 10 时 hcclQos 是被拒绝还是被跳过？

**需要观察的现象**：测试用例如何手工填充 `HcclCommConfig`——特别是它如何设置头部 version 字段来模拟「新旧库混跑」。

**预期结果**：非法参数返回 `HCCL_E_PARA`；低版本时 QoS 被置为 NOT_SET 并返回 `HCCL_SUCCESS`（即「跳过」而非「报错」）。

**运行说明**：如需实际运行这些用例，参照 u1-l2 的 UT 构建方式（`bash build.sh` 加 UT 选项），无昇腾硬件时本实践为源码阅读型，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：如果用户侧链接的是比库更新的头文件（`configSize` 比 `sizeof(CommConfigHandle)` 大），`Load` 会怎样？

**答案**：截断到 `maxConfigSize` 再拷贝并打告警日志（comm_config.cc:97-100），多出来的尾部字段（新版本才有的配置）被忽略——「新配置被旧库忽略」是兼容性设计的预期行为。

**练习 2**：`SetConfigByVersion` 中为什么 v5 的 TC/SL 直接赋值（`trafficClass_ = config.trafficClass;`），而 v1 的 bufferSize/deterministic 却要经过带校验的 `SetConfigBufferSize/SetConfigDeterministic`？

**答案**：bufferSize 与 deterministic 有取值范围和硬件能力约束（如 buffer 最小 1MB、deterministic≤2 且 strict 仅 A2 支持），需要报错拦截；早期设计中 TC/SL 未在此层做校验（其校验后移到了 `config_mgr` 的 `ApplyTrafficClassAndServiceLevel`，见 4.3），体现了校验职责随架构演进而迁移。

---

### 4.3 新架构通路：config_mgr 的 ApplyHcclCommConfig

#### 4.3.1 概念说明

`config_mgr` 是控制面 `coll_communicator_mgr` 下的子模块，但它只有一个（很重要的）函数：`ApplyHcclCommConfig`。它的职责是**翻译 + 补充校验**：V2 新架构（A5）的通信域初始化不走 legacy 的 `CommConfig::Load` 入口，而是在创建 `CollComm` 后，把 `HcclCommConfig` 中新架构关心的字段（TC/SL、QoS、算法串、SQ depth）经它写入 `CollComm::config_`。

之所以单独成层，是因为新架构的配置字段有「版本门槛」语义：例如 `hcclQos` 自 config version 10 起才存在，`hcclChannelSqDepth` 自 version 11 起才存在——低于门槛时必须按「未配置」处理，不能把旧结构体尾部的不确定字节当配置读。

#### 4.3.2 核心流程

```text
HcclCommInitRootInfoConfig (V2 分支)
  └→ hcclComm::InitCollComm(config)                    [hccl_comm_host.cc:325]
       ├→ 创建 CollComm
       ├→ ApplyHcclCommConfig(config, collComm_->GetCommConfig(), mode)   ← 本模块
       │     ├→ ApplyTrafficClassAndServiceLevel   校验 TC∈[0,255]且4的倍数、SL∈[0,7]
       │     ├→ ApplyHcclQos                        version≥10 才读，值∈[0,7]
       │     ├→ hcclAlgo 非空则整串存入
       │     └→ ApplyHcclSqDepth                     version≥11 才读
       └→ collComm_->Init(...)
```

#### 4.3.3 源码精读

- [src/coll_communicator_mgr/config_mgr/coll_comm_config.h:17-19](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/config_mgr/coll_comm_config.h#L17-L19)：`HCCL_COMM_CONFIG_SQ_DEPTH_VERSION = 11` 常量与 `ApplyHcclCommConfig` 声明——整个 config_mgr 模块的对外面就这一函数一常量。
- [src/coll_communicator_mgr/config_mgr/coll_comm_config.cc:21-30](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/config_mgr/coll_comm_config.cc#L21-L30)：`GetHcclCommConfigVersion`，从用户结构体的 `reserved` 字段把头部 24 字节拷出来读 version——不依赖结构体布局直接强转，而是 `memcpy_s` 出 `CommConfigInfo` 视图，写法上保持了对 ABI 的敬畏。
- [src/coll_communicator_mgr/config_mgr/coll_comm_config.cc:32-59](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/config_mgr/coll_comm_config.cc#L32-L59)：`ApplyHcclQos`。version < 10 时把 QoS 置为 `HCCL_COMM_QOS_CONFIG_NOT_SET` 并直接返回成功（跳过）；否则校验值域 `[0,7]` 或哨兵值，非法报 `HCCL_E_PARA`。
- [src/coll_communicator_mgr/config_mgr/coll_comm_config.cc:61-81](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/config_mgr/coll_comm_config.cc#L61-L81)：`ApplyHcclSqDepth`，同样模式，门槛 version 11。
- [src/coll_communicator_mgr/config_mgr/coll_comm_config.cc:83-109](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/config_mgr/coll_comm_config.cc#L83-L109)：`ApplyTrafficClassAndServiceLevel`。TC 必须是 4 的倍数且 ≤255，SL 必须 ≤7，否则 `HCCL_E_PARA`——这是 RDMA 报文优先级字段（Traffic Class / Service Level）的硬件约束在软件层的体现。
- [src/coll_communicator_mgr/config_mgr/coll_comm_config.cc:111-128](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/config_mgr/coll_comm_config.cc#L111-L128)：`ApplyHcclCommConfig` 主函数：空指针直接成功（V2 允许不带 config 初始化，走默认加速模式）；依次套用 TC/SL、QoS、算法串、SQ depth。
- 调用点：[src/legacy/ascend910/framework/communicator/hccl_comm_host.cc:360-365](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/legacy/ascend910/framework/communicator/hccl_comm_host.cc#L360-L365)：`InitCollComm` 中 `ApplyHcclCommConfig(config, collComm_->GetCommConfig(), configOpExpansionMode)` 后紧接 `collComm_->Init(...)`——配置先于初始化就位。

#### 4.3.4 代码实践

**实践目标**：验证「版本门槛」语义。

1. 阅读 [test/ut/framework/next/coll_comms/communicator/ut_coll_comm_test.cc:187-214](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/test/ut/framework/next/coll_comms/communicator/ut_coll_comm_test.cc#L187-L214) 的 `Ut_ApplyHcclCommConfig_When_SqDepthVaries_Expect_VersionRules` 用例。
2. 该用例循环构造不同 version 的 config，断言 `ApplyHcclCommConfig` 的返回值。画出「version × 期望结果」对照表。
3. 思考：如果把 `HCCL_COMM_CONFIG_SQ_DEPTH_VERSION` 从 11 改成 12，这个用例中哪些断言会翻转？（只需推理，不要改源码。）

**预期结果**：version ≥ 11 时 sqDepth 正常写入；version < 11 时被置 NOT_SET 且返回成功。第 3 问答案：原本「version=11 通过」的用例会变为「跳过」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `ApplyHcclCommConfig` 收到 `nullptr` 配置时返回成功而不是报错？

**答案**：V2 通信域初始化允许不传 config（`InitCollComm` 注释「不校验config，为空时配置默认加速模式」，hccl_comm_host.cc:329）。`HcclCommInitRootInfo`（不带 Config 后缀）这条老入口也会以空 config 走到此处，配置对象保持构造默认值（跟随环境变量）即可。

**练习 2**：`hcclQos` 与 `trafficClass/serviceLevel` 同为网络优先级类字段，为什么 QoS 有版本门槛而 TC/SL 没有？

**答案**：TC/SL 自 config version 5 起就存在于结构体中，而 `ApplyHcclCommConfig` 服务的新架构基线已高于 5，无需再判；`hcclQos` 是 version 10 才追加的尾部字段，旧版本结构体该位置是越界内存或哨兵，必须先判版本再读。

---

### 4.4 运行期配置：HcclSetConfig 与确定性计算全链路

#### 4.4.1 概念说明

初始化配置是「一次性」的，但训练框架往往在通信域建立后才决定是否开启确定性计算。`HcclSetConfig` 填补这个空档，目前它唯一支持的配置项是 `HCCL_DETERMINISTIC`。

它有一条重要的**优先级规则**：环境变量 `HCCL_DETERMINISTIC` 优先于接口设置。如果环境变量已设置，接口调用会打 WARNING 并直接返回成功、不做任何修改——「先到先得」，避免两处配置打架。

它还遵循 u2-l1 讲过的 V1/V2 双架构分派：先经 `HCCLV2_FUNC_RUN` 宏尝试 V2 路径（A5），再做 V1 的通用逻辑。

#### 4.4.2 核心流程

```text
HcclSetConfig(HCCL_DETERMINISTIC, value)
  ├─ [V2 可用时] HcclSetConfigV2 → A5 固定开启确定性，仅打 WARNING 直接返回
  ├─ 读环境变量 HCCL_DETERMINISTIC
  │    ├─ 已设置 → WARNING「已被 Env 设置，不再重设」→ 返回成功（后续逻辑跳过）
  │    └─ 未设置 ↓
  ├─ 校验 value ∈ {0,1,2}，否则 HCCL_E_PARA
  ├─ value=2(strict) 且设备非 910B → HCCL_E_NOT_SUPPORT
  ├─ SetDeterministic(value)               ← 写全局开关（externalinput）
  └─ 遍历 CollCommMgr 中该设备所有已建通信域
       └→ hcclComm::SetDeterministicConfig
            └→ HcclCommunicator::SetDeterministicConfig
                 └→ HcclAlg::SetDeterministicConfig
                      └→ TopoMatcher::SetDeterministicConfig  ← 真正生效点
                           externalEnable_.deterministic = value
                           （后续算法选择读取该值，如确定性 pipeline 执行器）
```

#### 4.4.3 源码精读

主入口：

- [src/legacy/ascend910/framework/op_base/src/op_base.cc:2288-2328](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/legacy/ascend910/framework/op_base/src/op_base.cc#L2288-L2328)：`HcclSetConfig` 全文。可依次看到：`HCCLV2_FUNC_RUN` 分派（L2293）→ 环境变量检查（L2295-2318）→ 取值校验与设备能力校验（L2299-2312）→ `SetDeterministic`（L2313）→ 遍历 `opGroup2CommMap` 逐通信域下发（L2320-2324）。
- [src/legacy/ascend910/framework/op_base/src/op_base.cc:2330-2341](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/legacy/ascend910/framework/op_base/src/op_base.cc#L2330-L2341)：`HcclGetConfig`，读取的是全局外部输入 `GetExternalInputHcclDeterministicV2()`——即环境变量或上一次 Set 的结果，而不是逐通信域查询。

V2 分支（A5 的行为）：

- [src/legacy/ascend950/framework/entrance/op_base/op_base_v2.cc:2980-2994](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/legacy/ascend950/framework/entrance/op_base/op_base_v2.cc#L2980-L2994)：`HcclSetConfigV2` 固定返回成功并告警「DETERMINISTIC_ENABLE 是 950 默认选项，不可设置」；`HcclGetConfigV2` 恒返回 1。这解释了文档中 950「不支持」的原因——不是缺失，而是**永远开启、无需配置**。
- [src/legacy/ascend910/framework/op_base/src/op_base.h:142-144](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/legacy/ascend910/framework/op_base/src/op_base.h#L142-L144)：`HcclSetConfigV2/HcclGetConfigV2` 的弱符号声明，运行时由 `hrtGetHcclV2Support` 探测到的架构决定是否绑定到 op_base_v2.cc 的强符号（与 u2-l1 的 `HcclCommInitRootInfo` 分派机制一致）。

生效链路的下半段（本讲综合实践将完整走一遍）：

- [src/legacy/ascend910/framework/communicator/hccl_comm.cc:1343-1345](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/legacy/ascend910/framework/communicator/hccl_comm.cc#L1343-L1345)：`hcclComm::SetDeterministicConfig` 转发给 communicator。
- [src/legacy/ascend910/framework/communicator/impl/hccl_communicator.cc:1590-1593](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/legacy/ascend910/framework/communicator/impl/hccl_communicator.cc#L1590-L1593)：`HcclCommunicator::SetDeterministicConfig` 再转发给算法对象。
- [src/legacy/ascend910/algorithm/impl/hccl_alg.cc:164-166](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/legacy/ascend910/algorithm/impl/hccl_alg.cc#L164-L166)：`HcclAlg::SetDeterministicConfig` 转发给拓扑匹配器。
- [src/legacy/ascend910/algorithm/impl/topo_matcher.cc:539-548](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/legacy/ascend910/algorithm/impl/topo_matcher.cc#L539-L548)：`TopoMatcher::SetDeterministicConfig`——最终落点，再次校验取值后写入 `externalEnable_.deterministic`。算法层选择确定性实现时读的就是它，例如 [src/legacy/ascend910/algorithm/impl/coll_executor/coll_reduce_scatter/coll_reduce_scatter_deter_pipeline_executor.cc:125](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/legacy/ascend910/algorithm/impl/coll_executor/coll_reduce_scatter/coll_reduce_scatter_deter_pipeline_executor.cc#L125) 中 ReduceScatter 的确定性流水线执行器。

#### 4.4.4 代码实践

**实践目标**：跟踪一个配置项从 `HcclSetConfig` 到实际生效点的完整路径。

1. 从 [op_base.cc:2288](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/legacy/ascend910/framework/op_base/src/op_base.cc#L2288) `HcclSetConfig` 出发，用 Grep 逐层跳转 `SetDeterministicConfig`：op_base.cc → hccl_comm.cc:1343 → hccl_communicator.cc:1590 → hccl_alg.cc:164 → topo_matcher.cc:539。
2. 记录每一层的文件、行号、函数名，以及该层做的额外工作（校验？转发？存储？），整理成调用链表格。
3. 补充两个分支：环境变量优先分支（op_base.cc:2295-2318）和 V2 分支（op_base_v2.cc:2980）。回答：在 A5 机器上调用 `HcclSetConfig(HCCL_DETERMINISTIC, 0)` 会发生什么？

**预期结果**：得到一张六层调用链表；A5 上该调用先走 V2 分支打 WARNING，随后 V1 逻辑仍会执行（若环境变量未设置，全局开关仍被写为 0，但 A5 算法本身默认确定性，此值对 A5 路径无实际影响——**待本地验证**）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `HcclSetConfig` 需要遍历所有已建通信域逐个下发，而不是只改一个全局变量？

**答案**：确定性开关影响的是**算法选择**（TopoMatcher 的 externalEnable），而算法配置是每个通信域一份。只改全局量无法影响已按非确定性算法初始化好的通信域；同时全局 externalinput（`SetDeterministic`）仍会被设置，用于尚未创建的通信域取默认值——两处都需要写。

**练习 2**：`HcclGetConfig` 返回的值与「某个具体通信域当前实际使用的确定性模式」一定一致吗？

**答案**：不一定。它读的是全局 externalinput（op_base.cc:2337），反映环境变量/最近一次 Set 的结果；若某通信域在初始化时通过 `HcclCommConfig.deterministic` 单独指定过不同值（comm_config.cc:273 路径），该通信域的实际行为以自己的 `CommConfig`/TopoMatcher 状态为准。

---

### 4.5 Channel 级配置：HcclChannelConfig 不透明句柄

#### 4.5.1 概念说明

第三条通路服务于 u2-l6 将详细讲的 Team/Channel 机制：`HcclChannelAcquireWithConfig` 创建通信通道时，可附带一个「高级选项」配置对象——目前唯一的能力是**共享 Jetty 队列**（多个算子复用同一组传输队列以节省资源），由 `isSharedQueue` 开关 + `sharedQueueTag` 标识。

它展示了与前两条通路完全不同的封装风格：不透明句柄。对外只声明 `HcclChannelConfig` 类型与 Create/Destroy/SetInt/SetStr 四个操作（声明在 `include/hccl/hccl_res.h` / `hccl_channel.h` 一侧），内部结构 `HcclChannelConfigData` 是一个仅两个成员的简单 struct，藏在新架构适配层 `api_c_adpt` 里。好处是：新增字段不用动 ABI，坏处是：调用方必须成对调用 Create/Destroy，且只能通过类型标签（`HcclChannelConfigType`）逐项设置。

#### 4.5.2 核心流程

```text
HcclChannelConfigCreate(&config)                 // new 一个 HcclChannelConfigData
  └→ HcclChannelConfigSetInt(config, TYPE_IS_SHARED_QUEUE, 1)
  └→ HcclChannelConfigSetStr(config, TYPE_SHARED_QUEUE_TAG, "my_tag")
       └→ HcclChannelAcquireWithConfig(comm, engine, descs, num, config, channels)
            ├→ ParseSharedQueueConfig：解出 isSharedQueue / sharedQueueTag
            ├→ 非共享 → 直接走普通 HcclChannelAcquire
            └→ 共享 → PrepareV2ChannelAcquire → 校验/建链/按 tag 复用共享队列
  └→ HcclChannelConfigDestroy(config)            // 用完即可销毁，不影响已建 channel
```

#### 4.5.3 源码精读

- [src/coll_communicator_mgr/api_c_adpt/hccl_channel_config.h:24-27](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/api_c_adpt/hccl_channel_config.h#L24-L27)：`HcclChannelConfigData` 定义，注释明确其职责是「记录是否共享 Jetty 及其 tag，用于通信域层 channel 复用管理」。
- [src/coll_communicator_mgr/api_c_adpt/hccl_channel_config.cc:14-29](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/api_c_adpt/hccl_channel_config.cc#L14-L29)：`HcclChannelConfigCreate` 用 `NEW_NOTHROW` 分配并向上转型为不透明句柄；`HcclChannelConfigDestroy` 负责时直接 `delete`，空指针宽容返回成功。
- [src/coll_communicator_mgr/api_c_adpt/hccl_channel_config.cc:31-62](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/api_c_adpt/hccl_channel_config.cc#L31-L62)：`SetInt/SetStr` 按 `HcclChannelConfigType` 标签 switch 分发，未知标签报 `HCCL_E_PARA`——「标签 + 分发」替代直接成员访问，是句柄模式的代价与安全保障。
- 消费点：[src/coll_communicator_mgr/api_c_adpt/coll_comm_res_c_adpt.cc:1248-1270](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/api_c_adpt/coll_comm_res_c_adpt.cc#L1248-L1270)：`HcclChannelAcquireWithConfig` 先 `ParseSharedQueueConfig` 解出共享配置，非共享则完全退化为普通 `HcclChannelAcquire`；共享路径复用同一套前置校验（`PrepareV2ChannelAcquire`，[coll_comm_res_c_adpt.cc:581-626](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/api_c_adpt/coll_comm_res_c_adpt.cc#L581-L626)，注释写明「非共享路径与共享路径共用」）。
- 用法文档：[docs/zh/api_ref/comm_opdev/control_plane_api/comms_domain_resource_mgmt/HcclChannelConfigCreate.md](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/docs/zh/api_ref/comm_opdev/control_plane_api/comms_domain_resource_mgmt/HcclChannelConfigCreate.md)：说明配置对象在 `HcclChannelAcquireWithConfig` 调用完成后即可销毁，不影响已创建的 channel——即配置对象只是「传参的信封」，不是长生命周期资源。

#### 4.5.4 代码实践

**实践目标**：掌握不透明句柄类接口的使用范式，为 u2-l6 的 Team 实践做铺垫。

1. 依次阅读四个接口文档：`HcclChannelConfigCreate.md`、`HcclChannelConfigSetInt.md`、`HcclChannelConfigSetStr.md`、`HcclChannelConfigDestroy.md`（同目录下），以及 `HcclChannelAcquireWithConfig.md` 中的调用示例。
2. 对照源码确认：文档中「配置对象用完即可销毁」的依据是 `HcclChannelAcquireWithConfig` 在入口处就把句柄解构成局部变量 `isSharedQueue/sharedQueueTag`（coll_comm_res_c_adpt.cc:1265-1268），此后不再引用句柄。
3. 写出一段示例伪代码（明确标注「示例代码」）：Create → SetInt(IS_SHARED_QUEUE,1) → SetStr(SHARED_QUEUE_TAG,"tag") → AcquireWithConfig → Destroy，并注明每步对应的错误码检查。

**预期结果**：能独立写出完整的 Channel 配置调用骨架；运行验证需昇腾环境与已初始化的通信域，**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：与 `HcclCommConfig`（扁平 ABI 结构体）相比，`HcclChannelConfig`（不透明句柄）各有什么取舍？

**答案**：扁平结构体一次赋值即可、可局部变量分配、零额外系统调用，但字段追加依赖尾部扩展 + 版本号，且调用方可见布局；不透明句柄布局完全隐藏、扩展自由（可放 `std::string` 等 C++ 类型），但必须堆分配、成对 Create/Destroy、逐项 Set。前者适合一次性大批量配置，后者适合少量可选高级配置——HCOMM 两条通路的选择正好对应这两种场景。

**练习 2**：如果调用方忘记 `HcclChannelConfigDestroy`，后果是什么？

**答案**：`HcclChannelConfigData` 是裸 `new` 出的堆对象（hccl_channel_config.cc:17），泄漏一个含 `std::string` 的小对象；不会影响已创建的 channel 功能，但长期反复创建会累积内存泄漏。

---

## 5. 综合实践

**任务：为「确定性计算」写一份完整的配置链路档案。**

把本讲三条通路中与确定性/配置机制相关的知识串起来，产出一份 markdown 档案，包含以下四部分：

1. **初始化路径**：从 `HcclCommInitRootInfoConfig` 出发，画出两条分支——V1 的 `CommConfig::Load → SetConfigByVersion → SetConfigDeterministic`（comm_config.cc:87/151/273）与 V2 的 `InitCollComm → ApplyHcclCommConfig`（hccl_comm_host.cc:364）。标注 V2 路径中确定性开关由谁承载（提示：A5 默认开启，不走 `SetConfigDeterministic`）。
2. **运行期路径**：完整抄录 4.4.2 的六层调用链，每层附文件:行号，并回答：链路中哪一层做了取值校验、哪一层做了设备能力校验、哪一层只是纯转发？
3. **优先级矩阵**：整理三种设置方式的优先级关系——环境变量 `HCCL_DETERMINISTIC`、初始化 config 的 `deterministic` 字段、运行期 `HcclSetConfig`——两两之间谁覆盖谁？依据分别是 op_base.cc:2295-2318 与 comm_config.cc:275-279。
4. **验证代码**（可选，有昇腾环境时）：写一个最小程序（示例代码）：初始化通信域后先 `HcclGetConfig` 读当前值，再 `HcclSetConfig` 置 1，再 `GetConfig` 复读；分别在不设置和设置 `HCCL_DETERMINISTIC=0` 环境变量两种条件下运行，对比两次读数是否符合你在第 3 部分总结的优先级。**待本地验证**。

## 6. 本讲小结

- HCOMM 的配置分三条通路：初始化期的 `HcclCommConfig`（一次性、通信域粒度）、运行期的 `HcclSetConfig/HcclGetConfig`（目前仅确定性开关、环境变量优先）、通道级的 `HcclChannelConfig`（不透明句柄、共享 Jetty 队列）。
- 库内配置载体是 `hccl::CommConfig`：`Load` 按「size 截断 + 魔数校验 + 版本阶梯」解析对外结构体，`CommConfiger` 单例按通信域 identifier 存放配置表。
- 尾部追加 + 版本号是贯穿配置体系的 ABI 兼容手法：`CommConfigHandle` 的字段顺序即版本顺序，`SetConfigByVersion` 用 `version >= Vx` 阶梯逐段读取。
- 新架构（V2/A5）的配置翻译层是 `config_mgr` 的 `ApplyHcclCommConfig`：为 QoS（v10）、SQ depth（v11）等新字段做版本门槛检查与值域校验后写入 `CollComm::config_`。
- 确定性开关的生效链路是 `HcclSetConfig → SetDeterministic（全局）+ 遍历通信域 → hcclComm → HcclCommunicator → HcclAlg → TopoMatcher`，最终落在算法选择的 `externalEnable_.deterministic`；A5 上确定性恒开、接口仅告警返回。

## 7. 下一步学习建议

- 下一讲 u2-l4 将进入**拓扑管理**：`rank_graph` 模块与 `HcclRankGraphGetRanksByLayer` 等分层拓扑查询接口。配置中的 TC/SL、QoS 正是作用于这些网络链路的，先懂拓扑再回头看配置会有新的理解。
- 若你对 `opExpansionMode`（本讲在 `CommConfigHandle` 与 `ApplyHcclCommConfig` 中反复遇到）感兴趣，可提前阅读 [docs/zh/comm_op_dev_guide/prog_models_concepts/comm_engine.md](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/docs/zh/comm_op_dev_guide/prog_models_concepts/comm_engine.md)，u5-l1 会展开。
- 想巩固本讲内容，建议通读 [test/ut/platform/hcom/ut_comm_config.cc](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/test/ut/platform/hcom/ut_comm_config.cc) 全部用例——它几乎覆盖了 `ApplyHcclCommConfig` 的每一条校验分支，是最好的「配置行为规格说明书」。
