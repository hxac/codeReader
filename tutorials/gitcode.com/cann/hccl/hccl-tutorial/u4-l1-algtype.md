# 算法类型 AlgType 与分级选择

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 HCCL 里「算法（algorithm）」这个词在源码中有哪两套并行的枚举表示：对外的 `HcclAlgoType` 与对内的 `AlgTypeLevel0/1/2`。
- 解释 `TagAlgType` 这个三级组合结构如何同时描述「节点内 / 节点间 / 超节点间」三个网络层级的算法选择，并理解为什么这三层可以各选各的算法。
- 读懂 `AlgTypeToStr` / `TransferAlgType` / `TransferAlgTypeStr` 三个字符串转换函数的差异与各自用途。
- 把本讲的 `algType` 与上一讲（u3-l2）Selector 产出的 `algName` 字符串区分开，理解「结构化的算法类型」与「调度用的算法名字」是两件事。

本讲是 Unit 4 的第一篇，属于 **intermediate** 层级，承接 u3-l2（算法选择器 Selector），为后续 u4-l3（环境变量与算法配置）打基础。

## 2. 前置知识

阅读本讲前，你需要先建立以下认知（在 u1-l2、u3-l2 中已讲过，这里只做回顾）：

- **集合通信与分级通信**：一次 AllReduce 在物理上往往被拆成「节点内 ReduceScatter → 节点间 AllReduce → 节点 AllGather」。之所以要分级，是因为网络是分层的——**Server 内（节点内）链路又快又宽，Server 间链路相对慢且窄**。把大块数据搬移放在低层级的高速链路上，整体更省时。
- **网络层级（netLayer）**：HCCL 用 Layer0/Layer1/Layer2 描述三层物理网络，大致对应：
  - **Level0 = Server 内 / 节点内**（intra-server，常走 HCCS/PCIE）。
  - **Level1 = Server 间**（inter-server，常走 RoCE 网卡）。
  - **Level2 = 超节点间**（inter-superpod，规模更大的跨机柜层级）。
- **Selector 产出 algName**：u3-l2 讲过，算法选择器最终输出一个 `algName` 字符串（如 `AicpuAllReduceSoleNHR`），它是后续 executor/template 注册表的查表键。

> 一个关键提醒：本讲的 `algType`（结构化的三级算法类型）和 u3-l2 的 `algName`（调度字符串）**是两套并行的「算法身份」**。`algName` 负责「查到用哪个 executor/template」，`algType` 负责「结构化地记录每一层用什么算法族」，二者不要混淆。本讲末尾会再点一次这个区别。

如果你对上面的层级概念还不熟，建议先回去看 u1-l2 的「RankGraph 拓扑模型」和「分级通信」两节。

## 3. 本讲源码地图

本讲涉及的关键文件，全部位于 `src/common/` 与 `src/ops/op_common/`：

| 文件 | 作用 |
| --- | --- |
| `src/common/alg_type.h` | 定义全部算法类型枚举、`TagAlgType` 结构、三张名字映射表、转换函数声明。**本讲的核心文件。** |
| `src/common/alg_type.cc` | 实现三个字符串转换函数与查表辅助函数。 |
| `src/ops/op_common/inc/alg_param.h` | `OpParam` 中央参数容器，其中持有 `AlgType algType` 字段——本讲结构在实际数据流中的载体。 |
| `src/ops/op_common/executor/executor_base.cc` | `RefreshAlgType`：按 executor 支持的算法清单校验并重置 `algType`。 |
| `src/ops/op_common/executor/channel/channel.cc` | 按 `algoLevel0/1/2` 计算通信通道（channel）——本讲结构最典型的消费点。 |
| `src/common/alg_env_config.h` / `.cc` | `HcclAlgoType` 的字符串映射与 `HCCL_ALGO` 环境变量解析，是「对外枚举」的入口。 |
| `src/common/hccl_common.h` | `HCCL_ALGO_LEVEL_0..3` / `HCCL_ALGO_LEVEL_NUM` 等层级常量。 |

> 阅读建议：先看 `alg_type.h` 把三套枚举和 `TagAlgType` 结构看清，再看 `alg_type.cc` 的三个转换函数，最后用 `channel.cc` 与 `executor_base.cc` 两个消费点验证理解。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **两套算法枚举**：对外的 `HcclAlgoType` 与对内的 `AlgTypeLevel0/1/2`。
2. **`TagAlgType` 三级组合结构**。
3. **三个字符串转换函数**。

---

### 4.1 两套算法枚举：HcclAlgoType 与 AlgTypeLevel0/1/2

#### 4.1.1 概念说明

HCCL 源码里同时存在**两套**描述「算法」的枚举，初学者很容易把它们混成一团。先用一句话区分：

- **`HcclAlgoType`——「对外 / 配置层」**：用户能通过 `HCCL_ALGO` 环境变量配置的算法**族**（family），粒度较粗，比如 RING、PIPELINE、NHR、FULLMESH。它回答「这一层想用哪一类算法」。
- **`AlgTypeLevel0/1/2`——「对内 / 运行层」**：运行期内部使用的、**按网络层级分**的细粒度算法枚举。它不仅区分算法族，还区分拓扑变体，比如同是 Ring，Level0 还分 `NP_SINGLE_RING`（单环）与 `NP_DOUBLE_RING`（双环）。

为什么需要两套？因为「用户想表达的意思」和「引擎实际要执行的细节」颗粒度不同。用户只需说「Server 间用 Ring」，但运行期还要知道是节点内的几卡环、单环还是双环，这些细节对外暴露反而增加心智负担。

#### 4.1.2 核心流程

两套枚举的关系可以这样理解（概念图，非函数调用）：

```text
用户配置 HCCL_ALGO="level1:ring"
        │  (字符串解析，见 alg_env_config.cc)
        ▼
HcclAlgoType::HCCL_ALGO_TYPE_RING        ← 对外枚举（配置层，按 level0..3 共 4 级存放）
        │  (内部转换为结构化的逐层算法类型)
        ▼
TagAlgType { algoLevel0, algoLevel1, algoLevel2 }   ← 对内枚举组合（运行层，3 级）
        │  (executor 校验 / channel 计算 / 日志打印时消费)
        ▼
具体执行：选 channel、算资源、下发 kernel
```

注意层级数量的一个小坑：对外配置侧用 **4 级**（`HCCL_ALGO_LEVEL_0..3`，常量 `HCCL_ALGO_LEVEL_NUM = 4`），而对内的 `TagAlgType` 只组合 **3 级**（`algoLevel0/1/2`）。两者层级含义大体对应但不完全一一映射，阅读时不要假设「外层第 4 级就等于内层某一级」。

#### 4.1.3 源码精读

先看**对外枚举** `HcclAlgoType`，它就是一组算法族的列举：

[HcclAlgoType 枚举（对外配置层）](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/alg_type.h#L20-L34) —— 注意 `HCCL_ALGO_TYPE_DEFAULT = 0`，配置为 DEFAULT 时表示「交给 HCCL 自适应选择」，这正是 `HCCL_ALGO` 文档里「默认自适应、一般无需手工指定」的源码出处。

再看**对内枚举**，按层级分成三个 `enum class`。Level0（节点内）的取值最丰富，因为它要区分节点内不同卡数 / 拓扑形状：

[AlgTypeLevel0 枚举（节点内 / Level0）](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/alg_type.h#L37-L51) —— 例如 `ALG_LEVEL0_NP_SINGLE_RING`（N 卡单环）与 `ALG_LEVEL0_NP_DOUBLE_RING`（N 卡双环）是两种 Ring 变体；`ALG_LEVEL0_NP_MESH` 表示「服务器内 3~8 卡组成 MESH」。

Level1（Server 间）与 Level2（超节点间）的取值相对少：

[AlgTypeLevel1 / AlgTypeLevel2 枚举](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/alg_type.h#L53-L74) —— 注意每个枚举的第一个值都是 `*_WHOLE_RING = 0`，注释说明：「单层拓扑，所有 level 均为 Whole ring 时，组成一个大环」。这是一个特殊语义：当三层都取 `WHOLE_RING` 时，表示**不做分级、把所有 rank 拉成一个大环**。

对外枚举 `HcclAlgoType` 的字符串映射（用于环境变量解析）定义在 env config 里，键是用户可写的字符串：

[HcclAlgoTypeMap：对外枚举 ↔ 配置字符串](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/alg_env_config.h#L83-L97) —— 这张表正是 `HCCL_ALGO="level1:ring"` 里 `ring`、`NHR`、`H-D_R`、`pipeline` 等字符串的归宿。注意这里的 `H-D_R` 对应 `HCCL_ALGO_TYPE_HDR`（即 RHD，递归二分倍增算法）。

而 env config 把 `HCCL_ALGO` 字符串按 `level0/level1/level2/level3` 拆级时，用的是另一张**反向**映射：

[ParserHcclAlgoLevel 中的 level 字符串 → HcclAlgoType](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/alg_env_config.cc#L481-L493) —— 它先把 `level0`/`level1`/`level2`/`level3` 这种**层级关键字**解析成层级下标（`HCCL_ALGO_LEVEL_0..3`），再把该层的算法字符串解析成 `HcclAlgoType`。层级常量定义在：

[层级下标常量 HCCL_ALGO_LEVEL_0..3 / NUM](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/hccl_common.h#L150-L154) —— `HCCL_ALGO_LEVEL_NUM = 4`，所以 env config 里每个算子都存一个长度为 4 的 `HcclAlgoType` 向量。

#### 4.1.4 代码实践

**实践目标**：亲手核对「对外枚举」与「对内枚举」的颗粒度差异。

**操作步骤**：

1. 打开 `src/common/alg_type.h`，分别数一下 `HcclAlgoType`、`AlgTypeLevel0`、`AlgTypeLevel1`、`AlgTypeLevel2` 各有几个枚举值。
2. 在 `AlgTypeLevel0` 中找到所有「带 RING」的枚举（如 `ALG_LEVEL0_NP_SINGLE_RING`、`ALG_LEVEL0_NP_DOUBLE_RING`、`ALG_LEVEL0_8P_RING`、`ALG_LEVEL0_4P_RING` 等），数一数有几种 Ring 变体。
3. 对照 `HcclAlgoType`，确认对外只有**一个** `HCCL_ALGO_TYPE_RING`。

**需要观察的现象**：对外只有一个粗粒度的 `RING`，对内 Level0 却有多个细粒度的 Ring 变体。

**预期结果**：这印证了「对外是算法族、对内是带拓扑细节的算法枚举」。对外 `RING` 在运行期会被细化为具体的节点内 Ring 变体。

> 说明：本实践为源码阅读型，无需运行；若要运行验证算法效果，需配合 `HCCL_ALGO` 环境变量在真实多卡环境上观察（见 u4-l3）。

#### 4.1.5 小练习与答案

**练习 1**：`HCCL_ALGO_TYPE_DEFAULT` 的值是多少？它在配置语义上代表什么？

**参考答案**：值为 `0`。它代表「交给 HCCL 自适应算法选择逻辑」，即用户不指定时的默认行为，此时 Selector 会根据产品形态、数据量、Server 个数自动选算法。

**练习 2**：为什么 `AlgTypeLevel0` 比 `HcclAlgoType` 多出那么多「带数字 P」的变体（`8P_RING`、`4P_MESH`、`2P_MESH` 等）？

**参考答案**：因为 Level0 描述的是节点内（Server 内）的具体拓扑形状，节点内卡数（P 即卡数）和连接方式（Mesh/Ring）直接决定能用的算法变体；而 `HcclAlgoType` 是面向用户的粗粒度算法族，不需要暴露这些节点内拓扑细节。

---

### 4.2 TagAlgType 三级组合结构

#### 4.2.1 概念说明

`TagAlgType` 是把上面三个对内枚举「打包」起来的结构体，一次性描述**三个层级各自用什么算法**。它是本讲标题里的 `AlgType`（注意 `alg_type.h` 里有一行 `using AlgType = struct TagAlgType {...}`，所以代码里 `AlgType` 就是 `TagAlgType` 的别名）。

它的核心是三个字段：

```text
TagAlgType {
    AlgTypeLevel0 algoLevel0;   // 节点内（Level0）算法
    AlgTypeLevel1 algoLevel1;   // Server 间（Level1）算法
    AlgTypeLevel2 algoLevel2;   // 超节点间（Level2）算法
}
```

这个结构最直观的价值是：**三层可以各选各的算法**。比如一个常见组合是「节点内用 Mesh（又快又宽）+ Server 间用 NHR（节点多时步数少）」。这正是分级通信在数据结构层面的体现——把三个物理上差异巨大的网络层级，用三个独立字段分别描述。

#### 4.2.2 核心流程

`TagAlgType` 的生命周期大致是：

1. **构造**：默认构造时三层都置为 `WHOLE_RING`（即「不分级的单一大环」）；也提供多个便捷构造函数，可以只指定一层或两层、未指定的层回退到 `WHOLE_RING`。
2. **承载**：作为 `OpParam.algType` 字段贯穿执行链路（栈上、随 `OpParam` 生命周期）。
3. **校验 / 重置**：executor 拿到 `algType` 后，会按自己支持的算法清单逐层校验；不支持的层会被重置为该 executor 支持的第一个算法。
4. **消费**：channel 计算按 `algoLevel0/1/2` 分支选不同的链路连接方式；日志按 `AlgTypeToStr` 打印。

`WHOLE_RING` 的特殊语义值得单独强调：当且仅当三层**都**是 `WHOLE_RING` 时，表示「这一层不参与分级切分，所有 rank 组成一个大环」。所以默认构造的 `TagAlgType` 含义是「完全不分级的单环」。

#### 4.2.3 源码精读

先看 `AlgType` 别名与结构体定义，重点看默认构造和字段：

[TagAlgType 结构体定义与默认构造](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/alg_type.h#L76-L107) —— 默认构造（L80-84）把三层都设成 `WHOLE_RING`；这里还提供了多个重载构造函数，比如只传 `algoLevel0`、或传 `level0+level1`、或 `level0+level2` 等，**未传的层一律补 `WHOLE_RING`**。这是一个对调用方很友好的设计：只想配置某一层时不必把三层都写全。

还有一个工厂方法，用于显式构造一个「全保留」的占位值：

[TagAlgType::Reserved() 静态工厂](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/alg_type.h#L126-L130) —— 三层都设成 `*_RESERVED`，常用于「尚未确定算法」的初始占位。

`TagAlgType` 在执行链路中的载体是 `OpParam`：

[OpParam 中的 algType 字段](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/inc/alg_param.h#L433) —— 注释写明「环境变量设置的算法类型」。`OpParam` 是贯穿入口→Selector→Executor→Template 的中央参数容器（u2-l3 已讲），`algType` 就搭在这趟车上流转。它还被序列化/反序列化（同文件 L485 `binaryStream << algType`、L520 `binaryStream >> algType`），用于跨进程资源计算场景的传递。

**消费点一：executor 的逐层校验与重置。** 这是理解「三层独立」最关键的代码：

[RefreshAlgType：按 executor 支持清单逐层校验](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/executor/executor_base.cc#L49-L68) —— 对 Level0/1/2 **分别**做 `std::find`：如果当前层的算法不在该 executor 的 `level{0,1,2}SupportedAlgos` 清单里，就打一条 WARNING 并**重置为清单里的第一个算法**。注意三层是独立判定的——这正是「三层可以各选各的、且各层有各自的合法算法集」的源码体现。注释里也说明了 `desc_` 即 executor 描述符里各层支持的算法列表为空时表示「不校验」。

**消费点二：channel 计算按层分支。** `channel.cc` 是 `algType` 最典型的实际消费者，它按每层的算法选不同的链路连接计算函数：

[CalcLevel0ChannelRequest：按 algoLevel0 分支选 Ring / Mesh 连接](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/executor/channel/channel.cc#L37-L48) —— `switch (algType.algoLevel0)`：若是 `NP_SINGLE_RING` / `NP_DOUBLE_RING` 调 `CalcRingChannelConnect`（算环形邻居），若是 `NP_MESH`（default 分支）调 `CalcMeshChannelConnect`（算全网连接）。同文件对 Level1、Level2 也有同样模式的 switch（分别在 [channel.cc:147](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/executor/channel/channel.cc#L147) 与 [channel.cc:196](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/executor/channel/channel.cc#L196)）。这说明 `algType` 直接决定了每一层要建多少条 channel、连哪些 rank。

#### 4.2.4 代码实践

**实践目标**：验证「三层独立校验、独立重置」这一行为。

**操作步骤**：

1. 阅读 [executor_base.cc 的 RefreshAlgType](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/executor/executor_base.cc#L49-L68)，确认 Level0/1/3 是三个**互不影响**的 `if` 块。
2. 假设某个 executor 的 `desc_.level0SupportedAlgos = {NP_MESH}` 且 `desc_.level1SupportedAlgos = {RING, NHR}`，而传入的 `algType = { NP_SINGLE_RING, PIPELINE, WHOLE_RING }`。
3. 手动推断：调用 `RefreshAlgType` 后，三层的值分别变成什么？

**需要观察的现象 / 预期结果**：

- Level0：`NP_SINGLE_RING` 不在 `{NP_MESH}` 中 → 重置为 `NP_MESH`。
- Level1：`PIPELINE` 不在 `{RING, NHR}` 中 → 重置为 `RING`（清单第一个）。
- Level2：若 `level2SupportedAlgos` 为空（不校验）→ 保持 `WHOLE_RING` 不变。

> 说明：这是源码阅读 + 推理型实践，结论可由 `RefreshAlgType` 的三条 `if` 直接推出，无需运行。

#### 4.2.5 小练习与答案

**练习 1**：默认构造的 `TagAlgType{}` 三层分别是什么值？合起来代表什么含义？

**参考答案**：三层都是 `WHOLE_RING`。合起来代表「不分级的单一大环」——所有 rank 不按层级切分，直接组成一个环。

**练习 2**：`RefreshAlgType` 为什么对三层用三个独立的 `if`，而不是一个统一的循环或单个判断？

**参考答案**：因为三层是**三种不同枚举类型**（`AlgTypeLevel0/1/2`），且每个 executor 对三层有**各自独立的支持清单**（`level0/1/2SupportedAlgos`）。三层互不影响——某一层不合法只重置那一层，不波及其他层。这正反映了三个网络层级可以独立选择算法的设计。

---

### 4.3 三个字符串转换函数

#### 4.3.1 概念说明

`alg_type.cc` 提供了三个把 `TagAlgType` 转成字符串的函数，用途各不相同：

| 函数 | 输出格式 | 典型用途 |
| --- | --- | --- |
| `AlgTypeToStr` | `level0:ring,level1:pipeline,level2:ring` | **人类可读**日志，带 `level0:/level1:/level2:` 前缀 |
| `TransferAlgType` | `ring-pipeline-ring` | 用算法**名字**用 `-` 拼接，紧凑 |
| `TransferAlgTypeStr` | `7-3-1` | 用枚举的**整数值**用 `-` 拼接，机读友好 |

它们都依赖三张「枚举值 → 名字」的映射表（`HCCL_ALGO_LEVEL0/1/2_NAME_MAP`）。注意这些名字是**对内枚举的展示名**，和 `HcclAlgoType` 的配置字符串（如 `H-D_R`）是两套命名空间，部分重叠（都有 `ring`/`NHR`/`NB`）但不完全相同。

#### 4.3.2 核心流程

三个函数的共同骨架是「查三张表 → 拼字符串」，区别只在**拼接格式**与**查不到时的兜底**：

```text
AlgType(algoLevel0, algoLevel1, algoLevel2)
        │
        ├── AlgTypeToStr      → "level0:" + name0 + ",level1:" + name1 + ",level2:" + name2
        ├── TransferAlgType   → name0 + "-" + name1 + "-" + name2       (查不到任一项 → "not found")
        └── TransferAlgTypeStr→ int(level0) + "-" + int(level1) + "-" + int(level2)  (同上)
```

其中「查表」由模板辅助函数 `GetAlgoString` 完成：在对应映射表里 `find`，找不到就返回 `"invalid algo type"`。

#### 4.3.3 源码精读

先看三张名字映射表，它们定义了「对内枚举 → 展示名」：

[HCCL_ALGO_LEVEL0_NAME_MAP（节点内枚举 → 名字）](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/alg_type.h#L149-L156) —— 注意多个细粒度枚举会**映射到同一个名字**：例如 `NP_DOUBLE_RING`、`WHOLE_RING`、`8P_RING`、`4P_RING`、`NP_SINGLE_RING` 都映射成 `"ring"`；`4P_MESH`、`2P_MESH`、`1P_MESH`、`NP_MESH` 都映射成 `"fullmesh"`；`RESERVED` 映射成 `"null"`。这说明字符串表示是**有损**的——从字符串反推不回精确枚举，只能反推到算法族。

[HCCL_ALGO_LEVEL1 / LEVEL2 NAME_MAP](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/alg_type.h#L158-L170) —— Level1 多了 `pipeline`、`AHC`、`AHC_BROKE`、`H-D` 等；Level2 较精简。注意 Level1 的 `ALG_LEVEL1_PIPELINE` 映射为 `"pipeline"`（**不是** `"pipe"`）。

再看查表辅助模板：

[GetAlgoString 模板：查表 + 兜底](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/alg_type.cc#L15-L24) —— 找不到键就返回 `"invalid algo type"`。这是 `AlgTypeToStr` 里每一层的兜底逻辑。

然后是三个转换函数本身。先看最常用、最易读的 `AlgTypeToStr`：

[AlgTypeToStr：拼出带 level 前缀的可读串](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/alg_type.cc#L26-L42) —— 分别查三张表得到 `algStrLevel0/1/2`，再拼成 `"level0:X,level1:Y,level2:Z"`。它被广泛用于 `HCCL_INFO` 日志，例如 `scatter_op.cc` 在选定算法后用它打印 `algType`，方便定位「这一层到底用了什么算法」。

再看两个「紧凑版」。`TransferAlgTypeStr` 输出整数三元组：

[TransferAlgTypeStr：枚举整数值用 - 拼接](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/alg_type.cc#L44-L62) —— 注意它的查表只是为了**校验合法性**（三项都在表里才拼接），实际拼接用的是 `static_cast<int>(枚举值)`，所以输出如 `7-3-1`。任一项不在表里则返回 `"not found"`。

`TransferAlgType` 输出名字三元组：

[TransferAlgType：算法名字用 - 拼接](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/alg_type.cc#L64-L82) —— 与上一个同构，但拼接的是表里的名字字符串，输出如 `ring-pipeline-ring`。

> 这三个函数的「查不到」语义不一致，是个容易踩的细节：`AlgTypeToStr` 经 `GetAlgoString` 对**单层**找不到返回 `"invalid algo type"`（仍会拼出完整串），而 `TransferAlgType` / `TransferAlgTypeStr` 只要**任一层**找不到就整体返回 `"not found"`。阅读时留意这个差别。

#### 4.3.4 代码实践（对应大纲的练习任务）

**实践目标**：给定 `AlgTypeToStr` 的输出，反推三级算法选择，并解释 Level0 与 Level1 为何可以不同。

**操作步骤与问题**：假设某次调用 `AlgTypeToStr` 打印出（大纲原文写作 `"level0:ring,level1:pipe,level2:ring"`，其中 `pipe` 是简写，**实际 `ALG_LEVEL1_PIPELINE` 在 [LEVEL1_NAME_MAP](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/alg_type.h#L158-L164) 中映射为 `"pipeline"`**，故真实输出为 `level0:ring,level1:pipeline,level2:ring`）：

1. 对照三张 NAME_MAP，反查每一层对应的算法族。
2. 解释这三层分别作用在哪个物理网络上。
3. 解释 Level0（节点内）与 Level1（Server 间）为什么可以选不同的算法。

**预期结果（参考答案）**：

- `level0:ring` —— 节点内（Server 内）用 Ring 类算法。由 [LEVEL0_NAME_MAP](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/alg_type.h#L149-L156) 可知 `ring` 可能对应 `WHOLE_RING / NP_SINGLE_RING / NP_DOUBLE_RING / 8P_RING / 4P_RING` 中的某一个（字符串有损，无法精确反推）。
- `level1:pipeline` —— Server 间用 Pipeline（流水线并行）算法，对应 `ALG_LEVEL1_PIPELINE`。
- `level2:ring` —— 超节点间用 Ring 类算法。

**Level0 与 Level1 为何可以不同**：因为它们作用在**物理特性截然不同的两层网络**上。Level0 是 Server 内，链路带宽高、时延低（如 HCCS/PCIE），通常卡数少，适合关系简单、低时延的 Mesh/Ring；Level1 是 Server 间，链路带宽相对低、时延高，且 Server 数可能很多，因此更适合步数少（对数复杂度）的 Pipeline/NHR 等算法以摊薄时延。三级字段相互独立（见 4.2 的 `RefreshAlgType`），正是为了让每一层都挑最契合自己物理特性的算法。

> 说明：本实践为源码阅读 + 推理型，结论可直接由三张 NAME_MAP 与 `AlgTypeToStr` 的拼接逻辑推出。

#### 4.3.5 小练习与答案

**练习 1**：同样一个 `TagAlgType`，`AlgTypeToStr` 与 `TransferAlgTypeStr` 的输出分别长什么样？举一个具体例子。

**参考答案**：假设 `algType = {ALG_LEVEL0_NP_SINGLE_RING, ALG_LEVEL1_PIPELINE, ALG_LEVEL2_RING}`。则 `AlgTypeToStr` 输出 `level0:ring,level1:pipeline,level2:ring`；`TransferAlgTypeStr` 输出 `ALG_LEVEL0_NP_SINGLE_RING`、`ALG_LEVEL1_PIPELINE`、`ALG_LEVEL2_RING` 三个枚举的整数值用 `-` 拼接（具体数字取决于枚举声明顺序，例如形如 `6-3-2`）。

**练习 2**：为什么说从 `AlgTypeToStr` 的输出**不能**精确反推出原始的 `AlgTypeLevel0` 枚举？

**参考答案**：因为 [LEVEL0_NAME_MAP](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/alg_type.h#L149-L156) 是**多对一**映射——`NP_DOUBLE_RING`、`WHOLE_RING`、`8P_RING`、`4P_RING`、`NP_SINGLE_RING` 都映射成 `"ring"`。字符串表示只保留到「算法族」粒度，丢失了具体拓扑变体，所以无法精确反推。

**练习 3**：`TransferAlgType` 与 `TransferAlgTypeStr` 在「某层枚举不在映射表里」时返回什么？这与 `AlgTypeToStr` 的行为有何不同？

**参考答案**：前两者只要**任一层**查不到就整体返回 `"not found"`；而 `AlgTypeToStr` 经 `GetAlgoString` 对**单层**找不到返回 `"invalid algo type"`，但仍会拼出完整的 `level0:..,level1:..,level2:..` 串，不会整体失败。

---

## 5. 综合实践

把本讲三个模块串起来，做一个完整的「算法类型追踪」小任务。

**任务背景**：你拿到一条来自线上日志的算法类型串：

```text
level0:fullmesh,level1:NHR,level2:ring
```

**要求**：

1. **反推结构**（用 4.3 的 NAME_MAP）：写出它对应的 `TagAlgType` 三层**算法族**；并指出 Level0 的 `fullmesh` 可能对应哪几个 `AlgTypeLevel0` 枚举（说明为何不能精确到唯一一个）。
2. **定位载体**（用 4.2）：说明这个 `algType` 在代码里挂在哪个数据结构的哪个字段上（给出文件与行号的永久链接），它会随哪个对象一起流转。
3. **追踪消费**（用 4.2）：分别说明 `executor_base.cc` 的 `RefreshAlgType` 与 `channel.cc` 的 `CalcLevel0ChannelRequest` 会如何处理这三层——哪些层可能被重置、Level0 的 `fullmesh`（即 Mesh 类）会走哪个 `Calc*ChannelConnect` 分支。
4. **对比 algName**（承接 u3-l2）：用一句话说明，为什么光有这个 `algType` 还不足以让 executor 真正执行，还必须配合 Selector 产出的 `algName` 字符串。

**参考要点**：

1. Level0=`fullmesh`（Mesh 族，可能对应 `4P_MESH/2P_MESH/1P_MESH/NP_MESH` 之一，多对一故无法精确反推）、Level1=`NHR`、Level2=`ring`。
2. 挂在 [`OpParam.algType`](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/inc/alg_param.h#L433)，随 `OpParam` 贯穿执行链路。
3. [`RefreshAlgType`](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/executor/executor_base.cc#L49-L68) 逐层校验，不支持的层重置为清单第一个；[`CalcLevel0ChannelRequest`](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/executor/channel/channel.cc#L37-L48) 中 Mesh 族会落入 `default` 分支调 `CalcMeshChannelConnect`（Ring 族才调 `CalcRingChannelConnect`）。
4. `algType` 只描述「每层用什么算法族」的结构化信息，用于 channel 计算/校验/日志；而 executor/template 的实例化是按 `algName` 字符串查注册表的（u3-l1/u3-l4），二者职责不同，缺一不可。

> 说明：本综合实践为源码阅读与推理型，全部结论可由本讲引用的源码直接推出，无需上板运行。若要在真实环境观察 `AlgTypeToStr` 的日志输出，可在开启 `HCCL_INFO` 日志后运行任意集合通信样例（参考 u1-l5）并 grep `level0:`。

## 6. 本讲小结

- HCCL 有**两套**算法枚举：对外的 `HcclAlgoType`（配置层、算法族、来自 `HCCL_ALGO`）与对内的 `AlgTypeLevel0/1/2`（运行层、按网络层级、带拓扑变体）。
- `TagAlgType`（即 `AlgType`）把三层算法打包成一个结构，分别描述**节点内 / Server 间 / 超节点间**，三层字段相互独立、可各选各的算法。
- 默认构造三层都为 `WHOLE_RING`，含义是「不分级的单一大环」；`Reserved()` 三层都为 `*_RESERVED`，用于占位。
- `algType` 挂在 `OpParam.algType` 上贯穿链路，由 executor 的 `RefreshAlgType` 逐层校验/重置，由 `channel.cc` 按层 switch 决定链路连接方式。
- `AlgTypeToStr`（带 level 前缀，人读）/ `TransferAlgType`（名字拼）/ `TransferAlgTypeStr`（整数拼）三个函数都基于三张多对一的 NAME_MAP，字符串表示是**有损**的，无法精确反推枚举。
- **关键区分**：`algType` 是结构化的算法类型（用于 channel/校验/日志），`algName` 是 Selector 产出的调度字符串（用于查 executor/template 注册表），两者并行、不可互相替代。

## 7. 下一步学习建议

- 想看「用户配置如何变成 `HcclAlgoType` 再影响选择」，接着学 **u4-l3 环境变量与算法配置系统**，它会完整讲 `AlgEnvConfig`、`InitEnvConfig` 的 Parse 流程与 `HCCL_ALGO` 的解析。
- 想看「设备差异如何影响可用算法/特性」，学 **u4-l2 设备类型与能力识别**（`HcclDevType`、950/960 等）。
- 想回到调度主链路看 `algName` 如何被消费，复习 **u3-l2 Selector** 与 **u3-l4 Executor**，并把本讲的 `algType` 与之对照。
- 建议顺带阅读源码：[`src/common/alg_type.h`](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/common/alg_type.h) 全文（很短）与 [`docs/zh/user_guide/hccl_env/HCCL_ALGO.md`](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/docs/zh/user_guide/hccl_env/HCCL_ALGO.md)，把「源码枚举」与「用户文档」两边对齐看。
