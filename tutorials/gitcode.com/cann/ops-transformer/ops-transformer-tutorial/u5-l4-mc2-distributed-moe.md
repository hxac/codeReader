# u5-l4 mc2 分布式 MoE：dispatch/combine 与同步原语

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚专家并行（EP）场景下一次 MoE 前向需要哪些通信算子协作：`moe_distribute_dispatch`（派发）、专家计算、`moe_distribute_combine`（汇聚），以及 `distribute_barrier`（全卡同步）。
2. 理解 dispatch/combine 的 v1 → v2 → v3 版本演进逻辑，并能用 `build.sh` 中「编译 v2 时自动补编 v3」的源码证据解释 v2/v3 为什么必须并存。
3. 理解 Ascend 950 上把一个通信算子拆成 setup / run / teardown 三段的生命周期设计，以及 setup/teardown 接口「只发不收 / 只收不处理完不算完」的语义。
4. 能独立拼出一条两卡 EP MoE 前向的完整算子调用序列（含通信域初始化、setup/teardown、barrier 与资源释放）。

本讲承接 u5-l3 建立的 mc2 通信域（`groupEp`）、`HcclGetCommName`、多 rank 协作等概念，是把「通信-计算融合」推广到「多卡分布式 MoE」的收官一讲。

## 2. 前置知识

- **EP（Expert Parallelism，专家并行）**：把 MoE 层的不同专家切到不同 NPU 卡上，每张卡只存放和计算一部分专家。于是一个 batch 的 token 必须「按专家归属发到对应卡」（派发），专家算完再「原路收回来加权合并」（汇聚）。
- **AllToAllV**：全互联通信原语。第 \(i\) 张卡发给第 \(j\) 张卡的数据量可以不同（V 表示 variable），正好匹配「每个专家收到的 token 数不均」的 MoE 负载。dispatch 和 combine 本质上都是一次 AllToAllV。
- **topK 路由的膨胀与收缩**：本卡输入 `BS` 个 token，每个 token 选 `K` 个专家，派发前 token 数膨胀为 `BS × K`（每个 token 复制 K 份发往 K 张卡）；combine 阶段再按专家权重收缩回 `BS`。
- **通信域（communication group）**：一组卡的集合，用字符串名字（如 `hccl_comm_test`）标识，由 HCCL 通过 rank table 初始化。u5-l3 讲过：算子拿到的不是 `HcclComm` 句柄，而是经 `HcclGetCommName` 转换出的域名字符串。
- **barrier（同步屏障）**：通信域内所有卡都到达同一位置才能继续。用于屏蔽快慢卡性能波动、辅助性能分析。
- **两阶段 aclnn API**：第一段 `GetWorkspaceSize` 做校验/tiling 并产出 executor，第二段 `aclnnXxx` 下发任务到 stream。本讲的 setup/teardown 接口同样遵循这一骨架。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [mc2/moe_distribute_dispatch/README.md](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/mc2/moe_distribute_dispatch/README.md) | dispatch v1 算子的功能公式、参数表、跨代 SoC 约束（本讲主文档） |
| [mc2/moe_distribute_combine/README.md](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/mc2/moe_distribute_combine/README.md) | combine v1 算子文档：AllToAllV 反向 + 加权求和 |
| [mc2/moe_distribute_dispatch_v2/README.md](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/mc2/moe_distribute_dispatch_v2/README.md) | dispatch v2 文档：相对 v1 的接口变更（commAlg、assistInfoForCombineOut 等） |
| [mc2/moe_distribute_dispatch_v3/README.md](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/mc2/moe_distribute_dispatch_v3/README.md) | dispatch v3 文档：context/ccl_buffer_size 新参数（仅 A3/950） |
| [mc2/moe_distribute_dispatch_setup/docs/aclnnMoeDistributeDispatchSetup.md](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/mc2/moe_distribute_dispatch_setup/docs/aclnnMoeDistributeDispatchSetup.md) | setup 接口文档：三段式原型、约束与完整两卡调用示例 |
| [mc2/moe_distribute_dispatch_setup/op_api/aclnn_moe_distribute_dispatch_setup.cpp](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/mc2/moe_distribute_dispatch_setup/op_api/aclnn_moe_distribute_dispatch_setup.cpp) | setup 的 aclnn 实现：校验 + CalcOutputSize + 第二段下发 |
| [mc2/moe_distribute_dispatch_setup/examples/test_aclnn_moe_distribute_dispatch_setup.cpp](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/mc2/moe_distribute_dispatch_setup/examples/test_aclnn_moe_distribute_dispatch_setup.cpp) | 官方两卡 setup/teardown 可运行示例（fork 双进程） |
| [mc2/moe_distribute_dispatch_v2/op_api/moe_distribute_dispatch_v2_base.cpp](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/mc2/moe_distribute_dispatch_v2/op_api/moe_distribute_dispatch_v2_base.cpp) | v2/v3 共用的 base 层：按 SoC 在 v2/v3 inner 实现间路由 |
| [mc2/moe_distribute_dispatch/op_host/op_tiling/moe_distribute_dispatch_tiling_base.h](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/mc2/moe_distribute_dispatch/op_host/op_tiling/moe_distribute_dispatch_tiling_base.h) | v1 tiling 复用 v2 tiling helper 的证据 |
| [mc2/distribute_barrier/README.md](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/mc2/distribute_barrier/README.md) | barrier 算子文档：参数与使用场景 |
| [mc2/distribute_barrier/op_api/aclnn_distribute_barrier.cpp](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/mc2/distribute_barrier/op_api/aclnn_distribute_barrier.cpp) | barrier 的 aclnn 薄入口 |
| [build.sh](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh) | 构建入口：v2 自动补编 v3、barrier 自动补编 extend 的逻辑 |

## 4. 核心概念与源码讲解

### 4.1 分布式 MoE：dispatch 与 combine 的配对链路

#### 4.1.1 概念说明

单机 MoE（u5-l1 的 `moe_init_routing` / `moe_token_permute`）里，「把 token 送到专家那里」只是同卡内存搬运。到了 EP 多卡场景，这件事变成了跨卡通信，于是 mc2 域提供了两个必须配套的通信算子：

- **`MoeDistributeDispatch`（派发）**：读入本卡 token `x` 和每个 token 的 topK 专家索引 `expertIds`，可选地做量化，然后在 EP 域做一次 AllToAllV，把每个 token 送到其专家所在的卡；同时输出一堆「路由元数据」（`expandIdx`、`epRecvCounts`、`expertTokenNums` 等）。
- **`MoeDistributeCombine`（汇聚）**：专家计算完成后，把各卡结果再经一次 AllToAllV 原路收回，按专家权重 `expertScales` 加权求和，恢复出与本卡原始输入同 shape 的输出。

两算子之间夹着的正是「本卡专家的 FFN 计算」——这就是 EP MoE 一层的完整骨架。

#### 4.1.2 核心流程

```text
每张卡并行执行：
  x(BS, H), expertIds(BS, K), expertScales(BS, K)
        │
        ▼
  [MoeDistributeDispatch]  量化(可选) → 按 expertIds 重排 → EP 域 AllToAllV
        │                   输出: expandX(A, H) + 路由元数据(expandIdx / epRecvCounts / ...)
        ▼
  [本卡专家 FFN 计算]        groupEp 域内各卡各自计算自己负责的 localExpertNum 个专家
        │
        ▼
  [MoeDistributeCombine]   AllToAllV 反向收回 → xOut = Σ(expertScales · ataOut)
        │
        ▼
  xOut(BS, H)              与输入 x 同 shape，token 顺序不变
```

非量化场景下 dispatch 的计算公式非常直白：

\[ \mathrm{expandXOut} = \mathrm{AllToAllV}(X) \]

combine 则是：

\[ \mathrm{ataOut} = \mathrm{AllToAllV}(\mathrm{expandX}), \quad x_{out} = \sum_k \big( s_k \cdot \mathrm{ataOut}_k \big) \]

其中 \(s_k\) 是该 token 第 \(k\) 个选中专家的权重（`expertScales`）。

「原路返还」是这套设计的核心约束：dispatch 输出的 `expandIdx` / `epRecvCounts` 等元数据张量，不要在业务代码里解读，**原样传给 combine 的对应入参**即可——这些值在不同通信算法、不同版本下可能不同，属于 dispatch 与 combine 之间的「内部合同」。

#### 4.1.3 源码精读

dispatch 的功能定义与公式（含 pertoken 动态量化分支）在算子 README 中：

- [mc2/moe_distribute_dispatch/README.md:L14-L37](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/mc2/moe_distribute_dispatch/README.md#L14-L37)：说明算子做「可选量化 + EP 域 AllToAllV」，给出 quantMode=0（非量化）与 quantMode=2（pertoken 动态量化，先算 dynamicScales、量化成 INT8 再通信、scale 走单独一次 AllToAllV）两种公式，并明确「必须与 `MoeDistributeCombine` 配套使用」。

combine 的功能定义：

- [mc2/moe_distribute_combine/README.md:L14-L25](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/mc2/moe_distribute_combine/README.md#L14-L25)：combine 做 AllToAllV 后加权求和，README 原文点明它「相当于按 MoeDistributeDispatch 算子收集数据的路径原路返还」。

「内部合同」约束（元数据张量不可被业务依赖）：

- [mc2/moe_distribute_dispatch/README.md:L252-L260](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/mc2/moe_distribute_dispatch/README.md#L252-L260)：约束说明第一条是「与 Combine 必须配套使用」；L258 明确 `expandIdx`、`epRecvCounts`、`tpRecvCounts`、`expandScales` 的元素值可能随产品/算法/版本变化，「使用时直接将上述 Tensor 传给 MoeDistributeCombine 对应参数即可，模型其他业务逻辑不应对其存在依赖」。

通信域独占约束（配对链路的硬边界）：

- [mc2/moe_distribute_dispatch/README.md:L272-L274](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/mc2/moe_distribute_dispatch/README.md#L272-L274)：一个模型中的 Combine 和 Dispatch 仅支持**相同** EP 通信域，且该通信域中**不允许有其他算子**——也就是说不能拿 dispatch 用的通信域再去跑 AllReduce。

容量上限 `A` 的计算（理解 token 膨胀的关键）：

- [mc2/moe_distribute_dispatch/README.md:L262-L270](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/mc2/moe_distribute_dispatch/README.md#L262-L270)：`A` 是本卡可能接收的最大 token 数。对 MoE 专家卡，`A ≥ BS × epWorldSize × min(localExpertNum, K)`，`localExpertNum = moeExpertNum / (epWorldSize - sharedExpertRankNum)`。直觉：全域 `BS × epWorldSize` 个 token 副本里，落到本卡的是「每个 token 选中了我这 localExpertNum 个专家中的至少一个」的部分，上限是每 token 贡献 min(localExpertNum, K) 份。

#### 4.1.4 代码实践

**实践目标**：亲手拼出两卡（`epWorldSize=2`、每卡 16 个专家，`moeExpertNum=32`）非量化 EP MoE 前向的算子序列。

**操作步骤**：

1. 通读 [mc2/moe_distribute_dispatch/README.md](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/mc2/moe_distribute_dispatch/README.md) 与 [mc2/moe_distribute_combine/README.md](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/mc2/moe_distribute_combine/README.md) 的参数表，为两卡场景填一张「参数取值表」：`groupEp`、`epWorldSize=2`、`epRankId=0/1`、`moeExpertNum=32`、`quantMode=0`、`globalBS=BS×2`、`localExpertNum=16`、`A ≥ BS×2×min(16, K)`。
2. 列出 dispatch 与 combine 之间必须「原样传递」的张量清单（对照 dispatch 的输出表与 combine 的输入表：`expandX`、`expandIdx`、`epRecvCounts`、`expandScales`）。
3. 在两算子之间补上「本卡专家 FFN」占位（伪代码即可，输入 `expandX(A, H)` + `expertTokenNums`，输出同 shape 张量）。

**需要观察的现象 / 预期结果**：你得到的调用序列应为「每卡：初始化通信域 → gating 得到 expertIds → Dispatch → 专家 FFN → Combine → 得到 xOut」，且 dispatch/combine 各参数两卡取值一致（README L260 约束：属性取值所有卡、所有层必须一致）。本实践为源码阅读型，无需 NPU；若要真机运行 dispatch 示例，可参考 `build.sh --run_example`（u2-l4 讲过），需配置 rank table 并起两个进程。运行结果**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 dispatch 输出的 `expandIdx` / `epRecvCounts` 不能在业务侧解析使用？

**答案**：README 约束（L258）说明这些张量中的元素值在不同产品型号、通信算法或版本中可能不同，它们只是 dispatch 与 combine 之间的内部对账信息；业务侧只需原样透传给 combine 对应入参，任何对其内容的依赖都会在换算法/升版本后悄悄出错。

**练习 2**：`epWorldSize=4`、`moeExpertNum=64`、无共享专家卡，某卡 `localExpertNum` 是多少？`BS=8`、`K=4` 时 `A` 至少多大？

**答案**：`localExpertNum = 64 / (4 - 0) = 16`；`A ≥ BS × epWorldSize × min(localExpertNum, K) = 8 × 4 × min(16, 4) = 128`。

**练习 3**：TP（张量并行）相关参数（`groupTp`/`tpWorldSize`/`tpRankId`）在文档里大量出现，为什么实践中都传默认值？

**答案**：README 功能说明（L16）写明「TP 域通信当前版本不再支持，TP 相关参数为预留参数」，各代 SoC 约束里也都注明「当前不支持 TP 域通信」；保留参数是为了接口向前兼容，避免后续版本恢复 TP 时破坏 ABI。

### 4.2 EP 通信原语与版本演进：v1 → v2 → v3 为什么并存

#### 4.2.1 概念说明

dispatch/combine 不是一对算子，而是**三代并存的算子家族**：

| 版本 | 关键变化 | 支持范围 |
| --- | --- | --- |
| v1（`moe_distribute_dispatch/combine`） | 基础 AllToAllV 形态，靠 `HCCL_INTRA_PCIE_ENABLE` 等环境变量控制通信算法 | A2 / A3 / 950DT |
| v2 | `expandIdx` 换成更丰富的 `assistInfoForCombineOut`；新增 `commAlg` 属性（fullmesh/hierarchy 等）替代环境变量；支持 FP8/FP4 等低精度 | A2 / A3 / 950DT |
| v3 | 新增 `context`（通信域上下文张量）与 `ccl_buffer_size` 入参，**去掉 `group_ep` 域名字符串** | 仅 A3 / 950DT（A2 不支持） |

v3 的动机：域名字符串每次调用都要 HCCL 查表换取通信资源；改用 `Mc2Context` 张量把通信域信息缓存成 device 侧句柄后，多层 MoE 反复调用时可以复用上下文、减少 host 侧开销。但 A2 硬件不走这条路径，所以 v2 必须保留。这就是「同一功能多版本并存」的根本原因——**版本差异本质是 SoC 代际差异**。

#### 4.2.2 核心流程

用户调用的始终是 v2 的 aclnn 入口，内部按 SoC 分流：

```text
aclnnMoeDistributeDispatchV2（用户入口）
        │
        ▼
aclnnMoeDistributeDispatchGetWorkspaceSizeBase（base 层路由）
        ├── 非 950（含 A2/A3 部分 commAlg）──► aclnnInnerMoeDistributeDispatchV2...
        └── 950（DAV_3510，非 ccu 模式）
                ├── GetMc2ContextTensor(groupEp, ...) 构造 context 张量
                └──► aclnnInnerMoeDistributeDispatchV3...（带 context/ccl_buffer_size）
```

因此编译 v2 时若不带上 v3，在 950 上运行期会找不到 `aclnnInnerMoeDistributeDispatchV3` 符号——构建系统必须在编译期把这对「隐形依赖」补齐。

#### 4.2.3 源码精读

v2 相对 v1 的接口变更（README 官方说明）：

- [mc2/moe_distribute_dispatch_v2/README.md:L78-L83](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/mc2/moe_distribute_dispatch_v2/README.md#L78-L83)：v2 输出更详细的 token 信息（`expandIdx` 替换为 shape `(A×128,)` 的 `assistInfoForCombineOut`）辅助 CombineV2 系列高效全卡同步；新增 `commAlg` 入参代替两个 HCCL 环境变量。

v3 相对 v2 的接口变更：

- [mc2/moe_distribute_dispatch_v3/README.md:L39-L47](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/mc2/moe_distribute_dispatch_v3/README.md#L39-L47)：v3 必须与 `MoeDistributeCombineV3` 一起使用；新增 `context` 入参存通信域信息、新增 `ccl_buffer_size` 指定通信域大小、减少 `group_ep`/`group_tp` 域名入参。

base 层的 SoC 路由（v2/v3 并存的第一手代码证据）：

- [mc2/moe_distribute_dispatch_v2/op_api/moe_distribute_dispatch_v2_base.cpp:L196-L216](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/mc2/moe_distribute_dispatch_v2/op_api/moe_distribute_dispatch_v2_base.cpp#L196-L216)：非 950 或 ccu 模式走 `aclnnInnerMoeDistributeDispatchV2GetWorkspaceSize`；950（`NpuArch::DAV_3510`）先用 `Mc2Aclnn::Mc2Context::GetMc2ContextTensor(groupEp, opName, hcclBuffSize, mc2Context)` 把域名换成 context 张量，再调 `aclnnInnerMoeDistributeDispatchV3GetWorkspaceSize`。这就是 v2 入口「内嵌」v3 实现的位置。
- [mc2/moe_distribute_dispatch_v2/op_api/moe_distribute_dispatch_v2_base.cpp:L221-L237](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/mc2/moe_distribute_dispatch_v2/op_api/moe_distribute_dispatch_v2_base.cpp#L221-L237)：第二段同样分流——950 上从 executor 的 user handle 取出 `CommType`，为 `AIV`（mte 新模板）时转投 `aclnnInnerMoeDistributeDispatchV3`，否则走 v2 inner。

构建侧自动补编（v2 依赖 v3 的编译期保障）：

- [build.sh:L1343-L1352](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L1343-L1352)：`assemble_cmake_args` 中，若用户 `--ops` 里出现 `moe_distribute_combine_v2` 而没有 v3，自动追加 `;moe_distribute_combine_v3`；dispatch 同理；`distribute_barrier` 也会自动追加 `distribute_barrier_extend`。因为 base 层在 950 上会引用 v3 的 inner 符号，漏编 v3 会导致运行期链接失败。

反向的复用也存在——v1 在 tiling 层复用 v2 的代码：

- [mc2/moe_distribute_dispatch/op_host/op_tiling/moe_distribute_dispatch_tiling_base.h:L19-L30](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/mc2/moe_distribute_dispatch/op_host/op_tiling/moe_distribute_dispatch_tiling_base.h#L19-L30)：v1 的 tiling base 头文件直接 `#include "../../../moe_distribute_dispatch_v2/op_host/op_tiling/moe_distribute_dispatch_tiling_helper.h"` 和 v2 的 kernel 侧 tiling 结构头。三代算子不是三份拷贝，而是**共用底座的三个入口**。

#### 4.2.4 代码实践

**实践目标**：用构建系统行为佐证 v2/v3 依赖关系。

**操作步骤**：

1. 执行 `bash build.sh --pkg --ophost --opapi --ops=moe_distribute_dispatch_v2 --soc=ascend950`（无 NPU 也可编译 host 库，参考 u1-l4）。
2. 观察编译日志中 `Info: cmake config` 行里 `ASCEND_OP_NAME` 的实际取值——预期是 `moe_distribute_dispatch_v2;moe_distribute_dispatch_v3`，即 build.sh 自动补编了 v3。
3. 对照 [build.sh:L1347-L1349](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L1347-L1349) 确认追加来源。
4. 再试试 `--ops=moe_distribute_dispatch`（v1），观察是否触发补编（预期**不会**追加 v3——v1 对 v2 的依赖是头文件级的，v2 目录需单独存在但不由这条规则触发；v1 tiling 头文件引用 v2 路径这一点可作为源码证据）。

**需要观察的现象**：cmake 参数里被注入了未手动指定的算子名。**预期结果**：`ASCEND_OP_NAME=moe_distribute_dispatch_v2;moe_distribute_dispatch_v3`。本实践只需编译态环境；若无法编译，可直接走读 build.sh 上述行得到同样结论（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：为什么不干脆把 v2 入口删掉，让所有卡都直接用 v3？

**答案**：v3 依赖 `context` 张量机制，而该机制只在 A3/950 上可用（v3 README 产品表里 A2 不支持）；A2 上 v2 入口仍需走域名 + inner v2 路径。多版本并存是用「入口稳定 + base 内部按 SoC 路由」换来的全代际覆盖。

**练习 2**：如果用户只编译了 v2 没编 v3，故障会在什么时候、以什么形式暴露？

**答案**：编译期能过（base 对 v3 inner 的引用是链接期符号），安装后在 950 上运行到 dispatch 时，base 层调用 `aclnnInnerMoeDistributeDispatchV3GetWorkspaceSize`/`aclnnInnerMoeDistributeDispatchV3` 找不到符号而失败。build.sh 的自动补编正是为堵住这个坑。

**练习 3**：`Mc2Context::GetMc2ContextTensor(groupEp, opName, hcclBuffSize, mc2Context)` 这一步解决什么问题？

**答案**：把「按域名字符串查通信资源」变成「首次查询后缓存为可复用的 context 张量」，后续多层 MoE 调用复用同一 context，并把 `hcclBuffSize` 一并传入（对应 v3 新增的 `ccl_buffer_size` 入参），省去每次调用重复的 host 侧查表开销。

### 4.3 setup / run / teardown：把一个通信算子拆成三段生命周期

#### 4.3.1 概念说明

在 Ascend 950DT 上，dispatch/combine 各自又被拆成两个 aclnn 接口，形成四件套：

- **`aclnnMoeDistributeDispatchSetup`（派发-发起）**：完成可选量化、按 topK 重排数据、发出通信指令——**指令发出后立即返回，不等通信完成**。输出是排好版的待发送数据 `yOut` 和通信命令信息 `commCmdInfoOut`。
- **`aclnnMoeDistributeDispatchTeardown`（派发-收尾）**：等待并完成数据接收与后处理，产出 v2 语义的 `expandX`、`assistInfoForCombineOut` 等最终输出。
- **`aclnnMoeDistributeCombineSetup` / `Teardown`**：combine 方向的同样拆分。

拆分的动机是**通信与计算重叠**：Setup 发出指令就退出，把 stream 让给后续计算任务；通信在后台推进；到真正需要数据的节点再调 Teardown 收结果。这相当于把「一次同步的 AllToAllV」改造成「异步 launch + 显式 wait」两段。此外还提供一个 `...TeardownCalcOutputSize` 辅助接口，在 host 侧预先算出各输出 buffer 的尺寸（如 `tokenMsgSize`），让用户能提前分配 device 内存。

注意：这四个接口当前仅 950DT 支持（setup 文档产品表中 A3/A2 均为「不支持」），且必须四件套配套使用。

#### 4.3.2 核心流程

两卡各一个进程，每个进程内的调用骨架：

```text
main: fork() 出 2 个子进程（模拟两卡两 rank）
  └─ RunInProcess(rank):
       aclInit → aclrtSetDevice → aclrtCreateContext
       → 创建 setup stream / teardown stream（两条流！）
       → HcclCommInitClusterInfoConfig(rank_table, rank_id, &config, &commsEp)   # 初始化 EP 通信域
       → HcclGetCommName(commsEp, hcomEpName)                                     # 句柄 → 域名字符串
       └─ LaunchOneProcess:
            构造全部输入/输出 aclTensor
            ① ProcessDispatchSetup:
                 aclnnMoeDistributeDispatchSetupGetWorkspaceSize(...)   # 第一段：校验+tiling
                 aclrtMalloc(workspace)                                  # 用户申请 workspace
                 aclnnMoeDistributeDispatchSetup(ws, size, executor, setupStream)
                 aclrtSynchronizeStreamWithTimeout(setupStream, timeout) # 等指令发出即返回
            ② ProcessDispatchTeardown:
                 aclnnMoeDistributeDispatchTeardownGetWorkspaceSize(x, yOut, commCmdInfoOut, ...)
                 aclnnMoeDistributeDispatchTeardown(..., teardownStream)
                 aclrtSynchronizeStreamWithTimeout(teardownStream, timeout)
            释放 tensor / workspace / stream / context / HcclComm
```

#### 4.3.3 源码精读

Setup 接口的语义定义：

- [mc2/moe_distribute_dispatch_setup/docs/aclnnMoeDistributeDispatchSetup.md:L26-L36](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/mc2/moe_distribute_dispatch_setup/docs/aclnnMoeDistributeDispatchSetup.md#L26-L36)：功能说明写明「只进行数据发送和通信状态发送，通信指令发出后算子即刻退出，无需等待通信完成。数据的接收和后处理由 aclnnMoeDistributeDispatchTeardown 接口完成」，且必须与 Teardown、CombineSetup、CombineTeardown 配套；同时提供了第三个辅助接口 `aclnnMoeDistributeDispatchSetupTeardownCalcOutputSize` 用于预计算输出尺寸。

setup 的 aclnn 第一段（校验后转 inner）：

- [mc2/moe_distribute_dispatch_setup/op_api/aclnn_moe_distribute_dispatch_setup.cpp:L84-L102](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/mc2/moe_distribute_dispatch_setup/op_api/aclnn_moe_distribute_dispatch_setup.cpp#L84-L102)：`aclnnMoeDistributeDispatchSetupGetWorkspaceSize` 先做 `CheckParams`（空指针 + groupEp 长度校验，见 [L52-L82](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/mc2/moe_distribute_dispatch_setup/op_api/aclnn_moe_distribute_dispatch_setup.cpp#L52-L82)），再透传给 `aclnnInnerMoeDistributeDispatchSetupGetWorkspaceSize`。这与 u5-l3 讲过的「op_api 层是前台，翻译参数、打包执行计划」完全一致。

输出尺寸的 host 侧计算（`CalcOutputSize` 的核心逻辑）：

- [mc2/moe_distribute_dispatch_setup/op_api/aclnn_moe_distribute_dispatch_setup.cpp:L104-L160](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/mc2/moe_distribute_dispatch_setup/op_api/aclnn_moe_distribute_dispatch_setup.cpp#L104-L160)：非量化时 `tokenMsgSize = Align(H, 256)`，量化时 `tokenMsgSize = Align(Align(H, 32) + 4, 512)`（L129-L133）——这就是 `yOut` 第二维的来源；随后按 `epRankId < sharedExpertRankNum` 区分共享专家卡/MoE 专家卡计算 `localExpertNum` 与容量 `A`（L148-L154），导出 `assistInfoForCombineOutSize = A × 128`、`commCmdInfoOutSize = (BS×(K+sharedExpertNum) + epWorldSize×localExpertNum) × 16`（L156-L158）。纯 host 整数运算，不碰 NPU。

第二段设定通信服务类型：

- [mc2/moe_distribute_dispatch_setup/op_api/aclnn_moe_distribute_dispatch_setup.cpp:L162-L171](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/mc2/moe_distribute_dispatch_setup/op_api/aclnn_moe_distribute_dispatch_setup.cpp#L162-L171)：`aclnnMoeDistributeDispatchSetup` 在真正下发前用弱符号 `NnopbaseSetHcclServerType(executor, NNOPBASE_HCCL_SERVER_TYPE_MTE)` 给 aclnn 框架指定通信方式（MTE，对应文档约束「950DT 仅支持 URMA 通信」的服务侧配置），再调 inner 执行。

官方两卡示例的调用顺序（最佳实践模板）：

- [mc2/moe_distribute_dispatch_setup/examples/test_aclnn_moe_distribute_dispatch_setup.cpp:L558-L589](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/mc2/moe_distribute_dispatch_setup/examples/test_aclnn_moe_distribute_dispatch_setup.cpp#L558-L589)：`ProcessDispatchSetup` 内完整的「第一段 → 按 workspaceSize 申请内存 → 第二段 → 带超时同步」四步。
- [mc2/moe_distribute_dispatch_setup/examples/test_aclnn_moe_distribute_dispatch_setup.cpp:L590-L620](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/mc2/moe_distribute_dispatch_setup/examples/test_aclnn_moe_distribute_dispatch_setup.cpp#L590-L620)：`ProcessDispatchTeardown` 注意其入参——它消费 setup 的输出 `yOut` 和 `commCmdInfoOut`，产出最终的 `expandX`/`assistInfoForCombineOut` 等，setup 与 teardown 之间也是靠张量传递「合同」。
- [mc2/moe_distribute_dispatch_setup/examples/test_aclnn_moe_distribute_dispatch_setup.cpp:L807-L810](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/mc2/moe_distribute_dispatch_setup/examples/test_aclnn_moe_distribute_dispatch_setup.cpp#L807-L810)：`HcclGetCommName(args.hcclEpComm, hcomEpName)` 把 HCCL 句柄换成域名字符串（u5-l3 概念在此落地），随后按 `ProcessDispatchSetup → ProcessDispatchTeardown` 顺序调用。
- [mc2/moe_distribute_dispatch_setup/examples/test_aclnn_moe_distribute_dispatch_setup.cpp:L890-L893](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/mc2/moe_distribute_dispatch_setup/examples/test_aclnn_moe_distribute_dispatch_setup.cpp#L890-L893) 与 [L928-L935](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/mc2/moe_distribute_dispatch_setup/examples/test_aclnn_moe_distribute_dispatch_setup.cpp#L928-L935)：先用 `HcclCommInitClusterInfoConfig` 依据 rank table 建立 EP 通信域（config 里还设置了 `hcclDeterministic=1` 与 `hcclBufferSize`），`main` 里 `fork()` 出两个子进程分别扮演 rank 0/1。

配套与通信域约束：

- [mc2/moe_distribute_dispatch_setup/docs/aclnnMoeDistributeDispatchSetup.md:L660-L698](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/mc2/moe_distribute_dispatch_setup/docs/aclnnMoeDistributeDispatchSetup.md#L660-L698)：四接口必须配套、属性取值全卡全层一致、四接口仅支持同一个 EP 通信域且域内不允许混入其他算子、950DT 仅支持 URMA 通信，以及 `HCCL_BUFFSIZE` 的估算公式。

#### 4.3.4 代码实践

**实践目标**：走读官方两卡示例，画出 setup/teardown 版 EP MoE 前向的完整时序。

**操作步骤**：

1. 打开 [test_aclnn_moe_distribute_dispatch_setup.cpp](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/mc2/moe_distribute_dispatch_setup/examples/test_aclnn_moe_distribute_dispatch_setup.cpp)，从 `main`（L913 起）向下跟踪：fork 双进程 → `RunInProcess`（L862 起）→ `LaunchOneProcess`（L798 起）→ `ProcessDispatchSetup` / `ProcessDispatchTeardown`。
2. 记录三条关键依赖链：① `HcclCommInitClusterInfoConfig` → `HcclGetCommName` → 作为 `groupEp` 传入第一段；② setup 的输出 `yOut`/`commCmdInfoOut` → teardown 的输入；③ setup 与 teardown 使用**不同的 stream**（`dispatchsetupstream` / `dispatchteardownstream`）。
3. 按 [aclnnMoeDistributeDispatchSetup.md:L701-L748](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/mc2/moe_distribute_dispatch_setup/docs/aclnnMoeDistributeDispatchSetup.md#L701-L748) 整理运行前置条件：rank table 文件的配置方法、`RANK_TABLE_FILE` 环境变量、`device ip` 查询命令（`hccn_tool -i <id> -ip -g`）。
4. 若有 950 环境：`bash build.sh --pkg --opapi --ops=moe_distribute_dispatch_setup,moe_distribute_dispatch_teardown,moe_distribute_combine_setup,moe_distribute_combine_teardown` 后按文档步骤运行示例；无环境则止步于时序图（源码阅读型实践）。

**需要观察的现象**：setup 同步返回极快（只等指令发出），teardown 同步才等待真正的数据到达；两条 stream 上的任务互不阻塞。**预期结果**：得到一张含「初始化 → setup → (可插入重叠计算) → teardown → 释放」的时序图。运行行为**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：setup 与 teardown 之间为什么用两条独立 stream？

**答案**：setup 的语义是「发出通信指令即退出」，其 stream 同步只代表指令下发完成；数据接收由 teardown 在另一条 stream 上等待。分成两条 stream 后，两条 stream 之间可以插入其他计算任务，实现通信与计算重叠——这正是拆分 setup/teardown 的目的。

**练习 2**：`aclnnMoeDistributeDispatchSetupTeardownCalcOutputSize` 与两阶段 API 的第一段 `GetWorkspaceSize` 有何不同？

**答案**：第一段产出的是 workspace 大小和 executor（执行计划）；`CalcOutputSize` 产出的是**用户侧输出张量**（`yOut` 的 `tokenMsgSize`、`assistInfoForCombineOut`、`commCmdInfoOut` 等）的尺寸，让用户在调用第一段之前就能正确 `aclrtMalloc` 输出内存。它是纯 host 计算（源码 L104-L160 只做整数对齐运算）。

**练习 3**：为什么 `yOut` 的第二维是 `Align256(H)` 而不是 `H`？

**答案**：通信搬运按固定块对齐效率最高，源码 L129-L133 对非量化场景做 256 对齐、量化场景还要附加 4 字节 scale 头并对齐到 512；`tokenMsgSize` 就是「每个 token 在通信报文中的实际占用宽度」。

### 4.4 distribute_barrier：多卡同步原语

#### 4.4.1 概念说明

EP MoE 是全卡协同的计算：任何一张卡慢了，其他卡都会在 AllToAllV 里等它。`distribute_barrier` 提供通信域内的**全卡同步**——所有卡都调用了它，才一起放行。两个设计要点：

- 它有一个 `xRef` 输入，但**没有任何业务语义**，仅用于构建张量依赖（让图模式/流调度知道 barrier 排在哪些算子之后），接口内部不对它做任何操作。
- 它需要一个**独占的通信域**：模型中 barrier 用的通信域里不允许混入其他算子（所以不能复用 dispatch/combine 的 EP 域）。

使用场景：在需要全卡同步的网络中调用，可屏蔽快慢卡引入的性能波动，协助性能分析（把「木桶效应」隔离到 barrier 处，让每段耗时可归因）。它支持连续多次调用；开启弹性缩容（`elasticInfoOptional`）时要保证 Dispatch/Combine 也传入一致的 `elasticInfo`。

#### 4.4.2 核心流程

```text
rank0: ...前置算子... → xRef(上一个算子的输入/相关张量) ─┐
rank1: ...前置算子... → xRef                            ├─ aclnnDistributeBarrier(xRef, group, worldSize)
rankN: ...前置算子... → xRef                            ┘
                 （所有 rank 都到达后一起返回）
                        │
                        ▼
              各 rank 继续后续任务
```

#### 4.4.3 源码精读

功能与参数定义：

- [mc2/distribute_barrier/README.md:L14-L16](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/mc2/distribute_barrier/README.md#L14-L16)：算子功能一句话——「完成通信域内的全卡同步，xRef 仅用于构建 Tensor 依赖，接口内不对 xRef 做任何操作」。
- [mc2/distribute_barrier/README.md:L36-L71](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/mc2/distribute_barrier/README.md#L36-L71)：参数表——`xRef` 支持几乎所有 dtype（正因为它无业务语义）、`timeOutOptional` 超时设置（950DT 单位 us，建议 5000000us）、`elasticInfoOptional` 动态缩容信息（A3 上为 shape `4 + 2×epWorldSize` 的 INT32 一维张量：前 4 位缩容配置 + rank 映射表）、`group` 域名与 `worldSize`。
- [mc2/distribute_barrier/README.md:L76-L88](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/mc2/distribute_barrier/README.md#L76-L88)：使用场景与约束——「屏蔽快慢卡引入的性能波动问题，协助分析性能」；需独占通信域；可连续调用；elasticInfo 与 Dispatch/Combine 保持一致。

aclnn 薄入口（典型的 base 转发风格）：

- [mc2/distribute_barrier/op_api/aclnn_distribute_barrier.cpp:L24-L36](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/mc2/distribute_barrier/op_api/aclnn_distribute_barrier.cpp#L24-L36)：两段接口各自只打一条日志后转发到 `aclnnDistributeBarrierGetWorkspaceSizeBase` / `aclnnDistributeBarrierBase`（base 实现在同目录 `distribute_barrier_base.cpp`，v2 接口与 extend 变体也挂在这套 base 上）。这与 u5-l3 讲的「多入口共用 base」组织方式一致。

构建侧同样有隐形依赖：

- [build.sh:L1350-L1352](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L1350-L1352)：编译 `distribute_barrier` 时自动追加 `distribute_barrier_extend`，机制与 dispatch/combine 的 v2→v3 补编完全相同。

#### 4.4.4 代码实践

**实践目标**：设计 barrier 在 EP MoE 训练循环中的插入点。

**操作步骤**：

1. 基于 4.1 实践得到的两卡 EP MoE 前向序列，在每个「iteration 边界」（combine 返回之后、下一轮 gating 之前）插入 `aclnnDistributeBarrier(xRef, groupBarrier, 2)`；注意 `groupBarrier` 必须是**新建的独立通信域**，不能复用 `groupEp`。
2. 为 `xRef` 选择依赖张量：按 README「入图时需将上个算子的输入、下个算子的输出作为入参传入」的原则，取 combine 的输出 `xOut`。
3. 若无 NPU，写出两 rank 的伪代码时序图并标注 barrier 的等待点。

**需要观察的现象 / 预期结果**（真机场景）：插入 barrier 后，各 rank 每轮前向耗时分布更平稳——快卡的耗时被「对齐」到慢卡水平，但单轮总耗时可能略增；这就是「用同步换可归因性」的取舍。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`xRef` 为什么要存在却又不被处理？

**答案**：调度系统（stream/图）靠数据依赖排列算子顺序。barrier 本身无输入输出语义，若不引入 `xRef`，框架无法知道它应排在哪些算子之后；给它挂一个张量依赖即可控制同步点位置，而算子内部无须（也不应）读写它。

**练习 2**：barrier 的通信域为什么必须独占？

**答案**：README 约束明确 barrier 需要单独通信域且域内不允许其他算子；dispatch/combine 的 EP 域也声明了同样的独占要求。混用会在通信资源与状态机上互相干扰（AllToAllV 的收发配对与 barrier 的全卡到达语义无法共享同一通信状态）。

**练习 3**：`elasticInfoOptional` 解决什么问题？与 Dispatch/Combine 的关系是什么？

**答案**：集群中有卡故障被剔除时，它携带实际参与通信的卡数与 rank 映射表（shape `4 + 2×epWorldSize`），让存活卡在缩容后的域内继续正确同步；约束要求开启时 Dispatch/Combine 必须传入同一份 elasticInfo，保证「派发-汇聚-同步」三方对当前有效 rank 集合的认知一致。

## 5. 综合实践

**任务：产出一份《两卡 EP MoE 前向算子调用序列》文档**，把本讲四个模块串起来。要求包含：

1. **环境与通信域准备**（参考 [aclnnMoeDistributeDispatchSetup.md:L701-L748](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/mc2/moe_distribute_dispatch_setup/docs/aclnnMoeDistributeDispatchSetup.md#L701-L748)）：rank table 配置、`RANK_TABLE_FILE`、每进程 `aclInit → SetDevice → CreateContext`、`HcclCommInitClusterInfoConfig` 建 EP 域、`HcclGetCommName` 换域名；为 barrier 额外建一个独立域。
2. **主链路**（v2 接口，A3/950 通用）：gating 得到 `expertIds/expertScales` → `aclnnMoeDistributeDispatchV2`（第一段+第二段）→ 本卡专家 FFN → `aclnnMoeDistributeCombineV2`，标出所有需要「原样透传」的元数据张量。
3. **950 三段式变体**：把主链路中的 dispatch/combine 替换成 setup/teardown 四件套，标注 setup/teardown 各自的 stream 与可重叠区间。
4. **同步点**：在迭代边界插入 `aclnnDistributeBarrier`。
5. **收尾**：释放 tensor/workspace/stream/context/HcclComm 的顺序（参照示例的 `destroyFunc`）。
6. **v2/v3 并存说明**：引用 [build.sh:L1343-L1352](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L1343-L1352) 与 [moe_distribute_dispatch_v2_base.cpp:L196-L216](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/mc2/moe_distribute_dispatch_v2/op_api/moe_distribute_dispatch_v2_base.cpp#L196-L216) 说明：v2 是面向用户的稳定入口，950 上 base 层会把请求路由到带 `Mc2Context` 的 v3 inner 实现，因此 v3 必须随 v2 一同编译——版本并存的根因是 SoC 代际差异，而非简单的功能迭代。

**验收标准**：序列图中每个算子都标出关键输入输出来源；能回答「如果把 combine 的 `epSendCounts` 换成自己统计的计数会怎样」（答案：违反内部合同约束，换通信算法/版本后即出错）。

## 6. 本讲小结

- EP 场景的 MoE 前向 = **Dispatch（AllToAllV 派发）→ 本卡专家 FFN → Combine（AllToAllV 反向 + 加权求和）**，两算子必须配套，且 `expandIdx`/`epRecvCounts` 等元数据张量只可在两算子间原样透传。
- dispatch/combine 是 **v1/v2/v3 三代并存**的家族：v2 是稳定入口，950（DAV_3510）上 base 层把请求路由到使用 `Mc2Context` 张量与 `ccl_buffer_size` 的 v3 inner 实现；`build.sh --ops` 里出现 v2 时会自动补编 v3（barrier 亦自动补编 extend），否则运行期符号缺失。
- 950DT 上单个通信算子可拆成 **setup（发指令即退）+ teardown（收数据做后处理）** 四件套，用两条 stream 换取通信与计算重叠；`TeardownCalcOutputSize` 以纯 host 整数运算（对齐公式 + localExpertNum/A 推导）预先给出输出尺寸。
- 通信算子的组织手法与 u5-l3 一脉相承：**薄 aclnn 入口 + 共用 base 层按 SoC 路由**，且版本间在 tiling 头文件级别互相复用（v1 include v2 的 tiling helper）。
- `distribute_barrier` 提供**独占通信域内的全卡同步**，`xRef` 仅承载调度依赖无业务语义，用于屏蔽快慢卡波动、辅助性能分析；弹性缩容信息需与 Dispatch/Combine 保持一致。
- 通信域纪律是所有约束的公共底线：属性取值全卡全层一致、dispatch/combine 共用一个 EP 域且域内不许混入其他算子、barrier 另用独占域。

## 7. 下一步学习建议

本讲结课后，mc2 与 MoE 两大域的主线已经完成。建议下一步：

1. **u7-l1 单元测试体系**：分布式通信算子无法单卡 UT，正好观察仓库如何为 mc2 算子组织 `tests` 与 `test_config.yaml` 裁剪，补齐你的工程化视角。
2. 自学延伸：`mc2/mega_moe`（把 dispatch/combine/专家计算整体融合的单算子）、`mc2/moe_ep_dispatch` / `moe_ep_combine`（另一代 EP 通信接口）、`moe/moe_update_expert`，对照本讲的「版本并存 + base 路由」模式阅读，验证这套分析框架的普适性。
3. 若关注 950 通信栈，可深入 `mc2/common`（u5-l3 已导览）中 `Mc2Context` 相关实现与 `distribute_barrier_extend`，理解 context 张量机制的全貌。
