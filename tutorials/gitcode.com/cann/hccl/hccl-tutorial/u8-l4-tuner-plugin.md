# Tuner 插件框架与实践

## 1. 本讲目标

本讲是 Unit 8（代价模型选择器）的最后一讲。学完后你应该能够：

1. 说清 HCCL Tuner 插件的 C ABI 约定：插件 `.so` 导出符号 `hcclTunerPlugin_v1`，提供 `init` 与 `getCollInfo` 两个回调。
2. 理解 HCCL 核心侧 `src/common/tuner/tuner_setup.cc` 的完整生命周期：`HcclTunerInit`（dlopen + dlsym + 引用计数）→ `HcclTunerCallGetCollInfo`（每次算子调用）→ `HcclTunerDestroy`，以及「100ms × 3 次慢调用自动禁用」的保护机制。
3. 理解插件修改 cost 后，`SelectorEngine::Run` 如何经 `SelectMinCost` 最终改变选出的 `algName`。
4. 学会通过 `HCCL_TUNER_PLUGIN` + JSON 配置文件干预算法选择，并编译运行 `examples/06_tuner_plugin` 参考实现与其单元测试 `test/test_plugin`。

## 2. 前置知识

阅读本讲前，你需要先掌握前两讲建立的认知（本讲不重复展开）：

- **u8-l1 新选择器 SelectorEngine**：`HCCL_USE_NEW_SELECTOR=1` 且算子在白名单（AllReduce/ReduceScatter/AllGather）时，`Selector()` 走 `SelectorEngine::Run`，其四步流程为「Tuner 初始化 → CostModel 取/建 → CostTableGen → SelectMinCost」。
- **u8-l2 CostModel 与 CostTable**：CostModel 按「通信域 × 引擎」缓存，CostTable 是本次调用的快照，`hcclTunerAlgoEntry_t.cost` 中 `cost < 0` 表示被过滤、`SelectMinCost` 取最小 cost 的条目输出其 `algName`。
- **u8-l3 三维命名**：`AlgoNameMapper::Enrich` 能把内部 algName（如 `AicpuAllReduceSoleMeshOneShot`）拆成用户可读的三维名（`aicpu`/`sole`/`meshoneshot`）。Tuner 插件看到的就是这三个名字。

再补充两个本讲用到的系统级概念：

- **dlopen/dlsym**：Linux 动态加载接口。`dlopen(path, RTLD_NOW)` 把一个 `.so` 装进当前进程并返回句柄；`dlsym(handle, "符号名")` 查找其中的全局符号（函数或变量）。u6-l1 已讲过 HCCL 用它加载 HCOMM；本讲 HCCL 用同一套机制加载 Tuner 插件，只是方向相反——这次 HCCL 是「宿主」，插件是「被加载方」。
- **C ABI（应用二进制接口）**：插件与宿主用 C 结构体 + 函数指针通信，不用 C++ 虚函数或 STL 类型，这样插件可以用任何语言、任何编译器版本编译，只要结构体布局一致。结构体里的 `structSize` 字段是版本兼容的关键：双方各自知道自己编译时的结构体大小，据此判断对方是否带有自己不认识的新字段。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/ops/op_common/selector/inc/hccl_tuner_plugin.h](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/inc/hccl_tuner_plugin.h) | 插件 C ABI 头文件：数据结构、回调签名、入口符号 `hcclTunerPlugin_v1`。宿主与插件共同 include |
| [src/common/tuner/tuner_setup.h](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/tuner/tuner_setup.h) / [tuner_setup.cc](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/tuner/tuner_setup.cc) | 核心侧：插件加载、引用计数、慢调用保护、`HcclTunerInit/CallGetCollInfo/Destroy` 三个对外接口 |
| [src/common/tuner/tuner_host_funcs.cc](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/tuner/tuner_host_funcs.cc) | 提供给插件的 host 函数（ctx 三件套 + 日志）的实现 |
| [src/ops/op_common/selector/selector_engine.cc](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/selector_engine.cc) | 集成点：`Run` 中初始化 Tuner、Enrich 三维名、调用 `getCollInfo` 改 cost |
| [src/ops/op_common/selector/cost_table.h](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/cost_table.h) | `CostTable` 定义——其元素类型 `AlgoCost` **就是** `hcclTunerAlgoEntry_t` |
| [examples/06_tuner_plugin/plugin.cpp](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/examples/06_tuner_plugin/plugin.cpp) | 参考实现：JSON 规则引擎，导出 `hcclTunerPlugin_v1` |
| [examples/06_tuner_plugin/hccl_tuner_config.json](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/examples/06_tuner_plugin/hccl_tuner_config.json) | JSON 配置示例 |
| [examples/06_tuner_plugin/README.md](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/examples/06_tuner_plugin/README.md) / [Makefile](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/examples/06_tuner_plugin/Makefile) | 编译与使用说明 |
| [examples/06_tuner_plugin/test/test_plugin.cpp](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/examples/06_tuner_plugin/test/test_plugin.cpp) | 插件单元测试：`#include "../plugin.cpp"` 直测内部逻辑 |

## 4. 核心概念与源码讲解

### 4.1 插件 ABI：hccl_tuner_plugin.h 的结构体与函数表

#### 4.1.1 概念说明

Tuner 插件解决的问题是：**CostModel 是离线标定的通用模型，而真实集群的组网、负载各不相同**。HCCL 允许用户（通常是集群运维或调优团队）提供一个外部 `.so`，在每次集合通信调用时「看一眼」cost table 并修改 cost，从而把特定场景下的经验规则注入算法选择——这与 NCCL 的 tuner 机制思路一致（源码注释也明确「对标 NCCL tuner」）。

整个交互契约只有一个头文件 `hccl_tuner_plugin.h`，核心是**一份双向的数据交换**：

- HCCL → 插件：通信域信息（`init` 时一次性）+ 每次调用的算子信息 + 候选算法条目数组（可改 cost）。
- 插件 → HCCL：`hcclTunerFuncs_v1_t` 函数表（通过导出的全局变量 `hcclTunerPlugin_v1`）。

#### 4.1.2 核心流程

插件生命周期：

```text
comm 创建（首次 SelectorEngine::Run）
  └─ HCCL: dlopen(插件.so) + dlsym("hcclTunerPlugin_v1")
  └─ HCCL: 调 init(comm, &commInfo, &hostFuncs)     ← 插件此时读配置、建规则表
每次集合通信算子（白名单内）
  └─ HCCL: Enrich 填好三维名 → 调 getCollInfo(comm, &collInfo, entries, count, &matched)
           插件匹配规则 → 命中则改 entries[i].cost，置 *matched=1
  └─ HCCL: SelectMinCost 按修改后的 cost 选 algName
comm 销毁
  └─ HCCL: 引用计数--（不 dlclose，随进程退出回收）
```

#### 4.1.3 源码精读

**① 通信域信息**（per-comm，`init` 时传入）——[src/ops/op_common/selector/inc/hccl_tuner_plugin.h:L25-L34](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/inc/hccl_tuner_plugin.h#L25-L34)：描述 rank 数、每服务器 NPU 数、服务器/Pod/超节点数、通信域名与 buffer 大小，供插件做拓扑维度的规则匹配。末尾的 `structSize` 是 ABI 版本兼容字段。

**② 算子调用信息**（per-op，`getCollInfo` 时传入）——[hccl_tuner_plugin.h:L38-L43](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/inc/hccl_tuner_plugin.h#L38-L43)：只有三个业务字段——算子类型 `collType`、数据量 `nBytes`（字节）、数据类型 `dataType`。这是规则匹配的「运行期变量」。

**③ 算法条目**（cost table 的一行）——[hccl_tuner_plugin.h:L47-L54](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/inc/hccl_tuner_plugin.h#L47-L54)：

```c
typedef struct {
    const char* algName;      /* "AicpuAllReduceSoleMeshOneShot" */
    const char* engineName;   /* "aicpu" — Enrich 填充，插件只读 */
    const char* executorName; /* "sole"  — Enrich 填充，插件只读 */
    const char* templateName; /* "meshoneshot" — Enrich 填充，插件只读 */
    float cost;               /* 可修改: <0=禁用, 0=偏好, >0=覆盖, 不改=用CostModel值 */
    uint32_t structSize;
} hcclTunerAlgoEntry_t;
```

插件**唯一可写的字段是 `cost`**。三维名是 HCCL 在调用前用 `AlgoNameMapper::Enrich` 填好的（见 4.3.2），插件拿它做字符串匹配定位目标条目。

这里有一个体现设计巧思的事实：CostTable 的元素类型**直接就是这个 ABI 结构体**——[cost_table.h:L27-L32](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/cost_table.h#L27-L32)：

```cpp
typedef hcclTunerAlgoEntry_t AlgoCost;

typedef struct {
    AlgoCost* costs;
    int count;
} CostTable;
```

也就是说「cost table」从定义上就是 Tuner 视角的命名条目数组，不需要任何序列化/转换层，`SelectorEngine` 把 `ct.costs` 原样传给插件。

**④ host 函数表**——[hccl_tuner_plugin.h:L60-L66](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/inc/hccl_tuner_plugin.h#L60-L66)：HCCL 借给插件的三件套——`ctxCreate/ctxGet/ctxDestroy`（在通信域 host 内存中存取插件私有上下文）和 `logFunction`（复用 HCCL 日志通道）。插件不自带持久化存储，规则表就存在这个 ctx 里。

**⑤ 函数表与入口符号**——[hccl_tuner_plugin.h:L70-L87](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/inc/hccl_tuner_plugin.h#L70-L87)：定义 `hcclTunerInit_t`/`hcclTunerGetCollInfo_t` 两个回调签名和 `hcclTunerFuncs_v1_t` 函数表，并声明入口符号 `extern hcclTunerFuncs_v1_t hcclTunerPlugin_v1;`。注意：**入口是一个导出的全局变量而非函数**，HCCL 用 `dlsym` 拿到这个变量的地址，从里面读出函数指针——未来升级 v2 时只需新增符号 `hcclTunerPlugin_v2`，新旧共存。

`getCollInfo` 的 `matched` 出参语义见 [hccl_tuner_plugin.h:L73-L75](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/inc/hccl_tuner_plugin.h#L73-L75)：插件命中规则并修改 cost 时设 `*matched=1`；HCCL 核心调用前已将其初始化为 0。

#### 4.1.4 代码实践

**实践目标**：验证「cost table 条目类型即 ABI 结构体」这一契约。

1. 打开 [cost_table.h:L27-L32](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/cost_table.h#L27-L32) 与 [hccl_tuner_plugin.h:L47-L54](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/inc/hccl_tuner_plugin.h#L47-L54)，对照两个结构体的字段。
2. 用 `grep -rn "hcclTunerAlgoEntry_t" src/ examples/` 观察该类型在核心（selector_engine.cc、cost_table.h）与插件（plugin.cpp）两侧被哪些函数消费。

**需要观察的现象**：核心侧没有任何「把内部 cost 结构转成插件格式」的转换函数；两侧用的是同一个类型定义。
**预期结果**：确认 Tuner 干预路径上零拷贝、零转换——`CostTableGen` 产出的数组直接就是插件的输入。
（待本地验证：grep 命令本身可直接执行。）

#### 4.1.5 小练习与答案

**练习 1**：为什么 `hcclTunerAlgoEntry_t` 的三维名字段注释写着「Enrich 填充，插件只读」，而不是让插件自己去解析 `algName` 字符串？
**答案**：algName 的拼接/拆分规则（引擎 Pascal 前缀 + 算子 Pascal 名锚点，见 u8-l3）是 HCCL 内部约定，随版本演进可能变化；由 `AlgoNameMapper` 统一拆名填充，插件只面对稳定的用户词表（`aicpu`/`sole`/`meshoneshot`），解耦了两边的演进节奏。

**练习 2**：函数表末尾的 `structSize` 字段有什么用？
**答案**：[hccl_tuner_plugin.h:L82](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/inc/hccl_tuner_plugin.h#L82) 注释说明：HCCL 侧设值（见 [tuner_setup.cc:L91](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/tuner/tuner_setup.cc#L91) `g_funcs.structSize = sizeof(hcclTunerFuncs_v1_t)`），插件据此判断宿主传来的缓冲区大小，实现 ABI 前向兼容——未来 v1 表新增字段时，旧插件靠比较 structSize 就能知道自己可安全访问多长。

**练习 3**：`cost` 字段的四种取值语义（`<0`、`=0`、`>0`、不改）分别对应什么干预意图？
**答案**：见 [hccl_tuner_plugin.h:L52](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/inc/hccl_tuner_plugin.h#L52)——`<0` = 禁用该算法（`SelectMinCost` 会跳过）；`=0` = 强制偏好（必然成为最小值）；`>0` = 用经验值覆盖 CostModel 估算；不改 = 信任 CostModel 的值。

### 4.2 核心侧加载与生命周期：tuner_setup.cc

#### 4.2.1 概念说明

`tuner_setup.cc` 是 HCCL 核心里唯一的插件管理模块，解决三件事：

1. **按需加载**：只有设置了环境变量 `HCCL_TUNER_PLUGIN` 才加载；没配置、加载失败、init 失败统统静默降级为 no-op，通信完全不受影响——Tuner 是纯可选增强。
2. **进程级单例 + 引用计数**：`.so` 全进程只 dlopen 一次，`g_refCount` 记录存活 comm 数；销毁时故意不 `dlclose`（避免与在途 `getCollInfo` 竞争），随进程退出由 OS 回收。
3. **慢调用保护**：`getCollInfo` 跑在每次算子调用的关键路径上，若插件实现低效会拖慢所有通信。保护规则：单次超过 100ms 记一次慢调用，**连续 3 次**超过即把插件整体禁用，回退纯 CostModel 选择。

还有一个值得注意的**信任边界**声明（[tuner_setup.h:L42-L44](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/tuner/tuner_setup.h#L42-L44)）：插件 `.so` 在本进程内执行，拥有与 HCCL 同等权限，**不做路径校验（by-design）**，管理员须确保 `HCCL_TUNER_PLUGIN` 指向可信 `.so`。这与 u6-l1 讲的 HCOMM dlsym 是同一信任模型。

#### 4.2.2 核心流程

```text
HcclTunerInit(comm, topoInfo)                     [每通信域一次]
  ├─ 持锁 LoadPluginLocked()
  │    ├─ 读 HCCL_TUNER_PLUGIN（空或 "none" → FAILED，no-op）
  │    ├─ dlopen(path, RTLD_NOW | RTLD_LOCAL)
  │    ├─ dlsym(handle, "hcclTunerPlugin_v1")，校验 init/getCollInfo 非空
  │    └─ 成功: g_funcs = *tuner; g_refCount=1; 失败: 置 LOAD_FAILED（此后不再重试）
  ├─ BuildCommInfo(): 从 TopoInfoWithNetLayerDetails 填 nRanks/nServers/nPods/...
  ├─ BuildHostFuncs(): 构造 ctx 三件套 + logFunction
  └─ 调 funcs.init()（超 5s 仅告警；失败仅告警，回退 CostModel）

HcclTunerCallGetCollInfo(comm, cmdType, nBytes, dataType, entries, count, modified)
  ├─ 插件未加载 / cmdType==HCCL_CMD_INVALID → 直接返回
  ├─ 锁内拷贝 g_funcs（防 TOCTOU：并发 Destroy 不会重置正在用的函数指针）
  ├─ 调 funcs.getCollInfo(...)，计时
  ├─ 计时 > 100ms → g_slowCallCount++；连续 ≥3 次 → 持锁置 LOAD_FAILED（禁用插件）
  │  计时 ≤ 100ms → g_slowCallCount 清零（"连续"的语义）
  └─ *modified = (matched == 1)；任何失败只告警、返回 SUCCESS

HcclTunerDestroy(comm)
  └─ 持锁 g_refCount--（不 dlclose）
```

#### 4.2.3 源码精读

**① 单例状态与保护阈值常量**——[tuner_setup.cc:L28-L45](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/tuner/tuner_setup.cc#L28-L45)：`g_loadStatus` 三态（READY/SUCCESS/FAILED）、`g_refCount`、`g_libHandle`、`g_funcs` 由 `g_tunerMutex` 保护；`TUNER_SLOW_CALL_THRESHOLD_MS = 100` 与 `TUNER_SLOW_CALL_LIMIT = 3` 就是「100ms × 3」的来源，另有 init 一次性阈值 5000ms（只告警不禁用）。

**② 首次加载**——[tuner_setup.cc:L55-L96](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/tuner/tuner_setup.cc#L55-L96)：

- [L66-L70](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/tuner/tuner_setup.cc#L66-L70)：读 `HCCL_TUNER_PLUGIN`，空串或 `"none"` 视为未配置，置 `LOAD_FAILED`（注意：FAILED 同时表示「没配」和「配了但失败」，对外都是 no-op，且**不再重试**）。
- [L74-L79](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/tuner/tuner_setup.cc#L74-L79)：`dlopen(pluginPath, RTLD_NOW | RTLD_LOCAL)`——`RTLD_NOW` 立即解析全部符号（缺符号即刻失败而非拖到调用时）；`RTLD_LOCAL` 不污染全局符号表。
- [L81-L87](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/tuner/tuner_setup.cc#L81-L87)：`dlsym(handle, "hcclTunerPlugin_v1")` 取函数表，并校验 `init`/`getCollInfo` 两个指针非空，任一为空则 dlclose 收尾、置 FAILED。

**③ commInfo 装配**——[tuner_setup.cc:L99-L138](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/tuner/tuner_setup.cc#L99-L138)：从 `TopoInfoWithNetLayerDetails`（u3-l3 讲过的拓扑快照）翻译出 `nRanks`/`nServers`，再从 `netLayerDetails` 取 `localNetInsSizeOfLayer[0]` 作 `nNpusPerServer`、第 1/2 层实例数作 `nPods`/`nSuperPods`；另查通信域名与 cclBuffer 大小。所有查询失败都只告警不阻断。

**④ init 的容错哲学**——[tuner_setup.cc:L158-L201](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/tuner/tuner_setup.cc#L158-L201)：`HcclTunerInit` 无论哪一步失败都 `return HCCL_SUCCESS`（[L196-L198](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/tuner/tuner_setup.cc#L196-L198) "fall back to CostModel"）——插件问题永远不阻塞建域。锁内先拷贝 `funcs = g_funcs`（[L167](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/tuner/tuner_setup.cc#L167)）再在锁外调用，是典型的「锁内取快照、锁外用快照」防 TOCTOU 手法。

**⑤ 慢调用保护**——[tuner_setup.cc:L237-L254](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/tuner/tuner_setup.cc#L237-L254)：用 `std::chrono::steady_clock` 计时 `getCollInfo`；超阈值时原子递增 `g_slowCallCount` 并告警打印 `count/3`，达到 3 次持锁置 `g_loadStatus = LOAD_FAILED`——此后所有 `HcclTunerCallGetCollInfo` 在入口检查处直接 no-op；未超阈值则 [L253](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/tuner/tuner_setup.cc#L253) 把计数清零，保证「连续」语义。

**⑥ 不 dlclose 的销毁**——[tuner_setup.cc:L268-L281](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/tuner/tuner_setup.cc#L268-L281)：注释明确这是有意为之（C3 约束）：另一线程可能正拿着 `g_funcs` 副本在执行插件代码，dlclose 卸载代码段会导致跳转到已释放内存；泄漏的 `.so` 由进程退出回收，代价为零。

**⑦ host 函数实现**——[tuner_host_funcs.cc:L33-L58](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/tuner/tuner_host_funcs.cc#L33-L58)：`TunerCtxCreate/Get/Destroy` 包装 `HcclEngineCtxCreate/Get/Destroy`，并给插件传的 ctxTag 统一加 `"__tuner_"` 前缀（[L27-L30](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/tuner/tuner_host_funcs.cc#L27-L30)），避免与 HCCL 内部 context tag 冲突——插件的 `"main"` 实际存为 `"__tuner_main"`。[L60-L88](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/tuner/tuner_host_funcs.cc#L60-L88) 的 `TunerLogFunction` 把插件日志按级别转发到 `HCCL_ERROR/WARNING/INFO/DEBUG`，统一带 `[TunerPlugin][文件:行号]` 前缀。

#### 4.2.4 代码实践

**实践目标**：通过日志观察插件加载与慢调用保护的触发路径。

1. 设置 `export HCCL_TUNER_PLUGIN=/nonexistent/plugin.so` 后运行任意 HCCL 程序（或 UT），开启 HCCL 日志，观察 [L76](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/tuner/tuner_setup.cc#L76) 的 `"[Tuner] dlopen failed"` 告警。
2. 再设置 `export HCCL_TUNER_PLUGIN=none`，对比**没有任何告警**——因为走的是 [L67-L70](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/tuner/tuner_setup.cc#L67-L70) 的「未配置」分支，静默 FAILED。
3. 阅读源码回答：如果第一次 dlopen 失败后修正了路径，同一进程内还有机会加载成功吗？

**需要观察的现象**：情形 1 有一次 WARNING，之后所有算子调用不再出现 `[Tuner]` 日志。
**预期结果**：确认 `LOAD_FAILED` 是终态、不重试（`LoadPluginLocked` 开头的 FAILED 分支直接返回 false）。
（待本地验证：需要可运行的 HCCL 环境与日志开关。）

#### 4.2.5 小练习与答案

**练习 1**：为什么 `g_slowCallCount` 用 `std::atomic` 而其余状态用 mutex？
**答案**：慢调用计数在每次 `getCollInfo` 调用后都要读写，若走 mutex 会给通信热路径加锁开销；而它只做「计数 + 与 3 比较」，原子自增足够。达到阈值后才持锁改 `g_loadStatus`——锁只用于保护加载状态这一低频路径（见 [tuner_setup.cc:L242-L253](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/tuner/tuner_setup.cc#L242-L253)）。

**练习 2**：`HcclTunerCallGetCollInfo` 里 `funcs = g_funcs` 这步锁内拷贝防的是什么竞态？
**答案**：防止另一个线程并发执行 `HcclTunerDestroy`（或慢调用禁用）重置 `g_funcs`/`g_libHandle` 时，本线程正拿着旧指针调用——拷贝到栈上后即使全局状态变化，本线程使用的函数表副本依然有效（[tuner_setup.cc:L214-L238](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/tuner/tuner_setup.cc#L214-L238) 注释「避免并发 HcclTunerDestroy 重置 g_funcs」）。这也是不 dlclose 的配套设计：只要代码段不被卸载，栈上函数指针就始终可安全调用。

**练习 3**：`HcclTunerDidModifyCost()`（[tuner_setup.cc:L283](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/tuner/tuner_setup.cc#L283)）为什么注释说「仅供 ST 测试查询」？
**答案**：它读的是全局 `g_tunerModifiedCost`，多线程并发调用算子时会互相覆盖，只反映「最近一次」结果；生产路径应使用 `HcclTunerCallGetCollInfo` 的 `modified` 出参（per-call、线程各自持栈变量），见 [tuner_setup.h:L61-L63](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/tuner/tuner_setup.h#L61-L63)。

### 4.3 SelectorEngine 集成点：改 cost 如何改变 algName

#### 4.3.1 概念说明

前面两个模块讲了插件本体与加载器，本模块把它们接回 u8-l1 学过的 `SelectorEngine::Run` 四步流程，回答本讲的核心问题：**getCollInfo 修改 cost 之后，Selector 是如何最终改变 algName 的？**

答案链条很短：`CostTableGen` 产出本次调用的 cost table → `Enrich` 给每个条目填三维名 → `getCollInfo` 按规则改写目标条目的 cost → `SelectMinCost` 遍历取最小 cost 条目、输出其 `algName`。也就是说 **Tuner 不直接「指定」算法，而是操纵代价**——把想选的算法 cost 改小（通常 0.0），其他不动，最小值选择自然落到目标上。这保持了「一切选择都有代价语义」的架构一致性。

#### 4.3.2 核心流程

`SelectorEngine::Run` 中与 Tuner 相关的三步（承接 u8-l1 的四步图）：

```text
step 0  Tuner 初始化
        HcclEngineCtxGet(TUNER_INIT_TAG) 失败 →
            HcclTunerInit(comm, topoInfo)           ← 每通信域仅一次
            HcclEngineCtxCreate(TUNER_INIT_TAG)     ← 打标记，下次不再 init
step 2.1 CostTableGen(cm, ct, topoInfo, param)      ← 本次调用快照
step 2.2 if (HcclTunerIsLoaded()) {
            Enrich(ct.costs, ct.count)              ← 填 engine/executor/template 三维名
            HcclTunerCallGetCollInfo(comm, opType,
                inputSize, dataType, ct.costs, ct.count, &modified)
        }                                            ← 插件可改 cost
step 3   SelectMinCost(ct, param, algName)           ← min(cost)，跳过 cost<0
```

#### 4.3.3 源码精读

**① Tuner 惰性初始化**——[selector_engine.cc:L187-L194](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/selector_engine.cc#L187-L194)：用通信域 ctx 上的 `TUNER_INIT_TAG` 标记保证 `HcclTunerInit` 每通信域只执行一次（首次算子调用时才触发，而非建域时）。

**② Enrich + 调用插件**——[selector_engine.cc:L208-L225](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/selector_engine.cc#L208-L225)：`CostTableGen` 之后，先判断 `HcclTunerIsLoaded()`——未加载插件时连 `Enrich` 都跳过（三维名只有插件消费，省掉查表开销）；加载了才 `Enrich` 并调 `HcclTunerCallGetCollInfo`，把 `param.inputSize`（字节）与 `param.DataDes.dataType` 作为 collInfo 传入。命中与否只影响日志（"tuner modified cost table" / "using CostModel selection"），不改流程。

`Enrich` 本体在 [algo_name_mapper.cc:L104-L119](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/algo_name_mapper.cc#L104-L119)：纯查表——用 algName 查启动时预计算的缓存，命中则填 `engineName/executorName/templateName` 三个用户词表名，未命中置空串。

**③ SelectMinCost 的裁决**——[selector_engine.cc:L245-L293](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/selector_engine.cc#L245-L293)：逐条打印 cost table 明细（`| idx | algName | engine | cost | status |`），`name == nullptr || cost < 0.0f` 的条目标记 `filtered` 并跳过（[L274-L276](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/selector_engine.cc#L274-L276)）——这正是 ABI 中「cost<0=禁用」的落点；其余取最小者为 `minIdx`，并列最小值收集进 `tiedAlgos` 处理平票。因此插件把某算法 cost 置 `0.0` 后，除非 CostModel 恰好也给别的算法算出 0，否则该算法必然胜出。

#### 4.3.4 代码实践

**实践目标**：从源码层面走通「改 cost → 变 algName」的因果链。

1. 在 [selector_engine.cc:L228](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/selector_engine.cc#L228) 的 `SelectMinCost` 调用处设断点（或在支持的环境上开 INFO 日志），观察一次 AllReduce 调用打印的 cost table。
2. 对照 [L270-L272](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/selector_engine.cc#L270-L272) 的表格日志，找出 `AicpuAllReduceSoleNHR` 一行，记录其 CostModel 估算 cost。
3. 思考：若插件把它改为 `0.0`，而表中已有另一算法 cost 恰为 `0.0`，会发生什么？

**需要观察的现象**：cost table 明细日志中每个候选算法一行，被过滤的条目 status 为 `filtered`。
**预期结果**：第 3 步答案见 [L295-L299](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/selector_engine.cc#L295-L299)——进入 `tiedAlgos` 平票处理，说明「cost=0 是偏好而非强制指定」。
（待本地验证：需要可上板运行的环境；纯阅读亦能完成第 3 步推理。）

#### 4.3.5 小练习与答案

**练习 1**：为什么不把 `Enrich` 放在 `CostTableGen` 内部，而是在 `HcclTunerIsLoaded()` 为真时才调用？
**答案**：三维名唯一的消费者是 Tuner 插件（核心自身的 `SelectMinCost` 只用 `algName` 和 `cost`）。未加载插件时 Enrich 是纯开销，所以 [L214-L216](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/selector_engine.cc#L214-L216) 把它和插件调用一起收在 `else if (HcclTunerIsLoaded())` 分支里。

**练习 2**：Tuner 把一个不满足当前拓扑条件的算法 cost 置 0，会发生什么？
**答案**：不会被选中。CostTableGen 阶段（u8-l2 的 `FilterByRules`）已经把拓扑不兼容的算法条目置为负 cost/无效，`SelectMinCost` 对 `cost < 0` 的条目直接跳过——插件在 `ApplyRule` 里也会跳过 `cost < 0` 的条目（见 [plugin.cpp:L613-L620](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/examples/06_tuner_plugin/plugin.cpp#L613-L620)），规则「改不动」已被过滤的算法。

### 4.4 参考实现：examples/06_tuner_plugin 的 JSON 规则引擎与 test_plugin

#### 4.4.1 概念说明

`examples/06_tuner_plugin` 是官方参考实现：一个约 850 行的插件 `.so`，把「干预算法选择」进一步简化为**编辑一个 JSON 文件**——用户不必写代码，只需写规则「当通信域/数据量/类型满足某条件时，把 engine/executor/template 定位到的条目 cost 设为某值」。运维团队可以在不改任何框架代码的情况下，把现场调优结论固化成配置。

它的设计要点：

- **init 时一次性解析 JSON**：读文件 → 两遍解析（先数规则数、再填充）→ Schema 校验（未知字段告警、必填字段缺失报错）→ 存入 hostFuncs ctx。
- **per-comm 上下文**：规则表经 `ctxCreate` 存到通信域 host 内存，`getCollInfo` 时 `ctxGet` 取回，commName 拷贝进持久缓冲（init 返回后入参指针失效）。
- **first-match-wins**：规则按数组顺序匹配，命中第一条即应用并返回。
- **附带单元测试** `test/test_plugin`：用 `#include "../plugin.cpp"` 把插件源码直接编进测试二进制，mock 掉 hostFuncs 与 securec，不依赖 NPU 即可跑。

#### 4.4.2 核心流程

```text
init (MyInit)
  读配置（$HCCL_TUNER_CONFIG_FILE → ./hccl_tuner_config.json → /etc/hccl/hccl_tuner_config.json）
  → nlohmann/json 解析 → CountRules 第一遍计数
  → ctxCreate("main", sizeof(StoredHeader) + n * sizeof(Rule))
  → ParseConfig 第二遍填充 + Schema 校验（errors>0 → configValid=0，此后不干预）
  → 拷贝 commInfo/commName 到持久缓冲

getCollInfo (MyGetCollInfo)
  ctxGet("main") → 校验 ctxSize ≥ sizeof(StoredHeader)（C17 防越界）
  → configValid? → 找 collType 匹配的 opSet
  → 逐条 MatchRule（rank 范围/字节范围/dataType/commName/拓扑维度，全 AND）
  → 命中: ApplyRule 按 engine/executor/template 定位条目改 cost，*matched=1，return
  → 全不命中: 打 "no rule matched" 日志
```

#### 4.4.3 源码精读

**① 规则数据结构**——[plugin.cpp:L134-L158](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/examples/06_tuner_plugin/plugin.cpp#L134-L158)：`Rule` = `MatchCond`（各维度上下界 + has 标志位）+ engine/executor/templateName + cost；`StoredHeader` 是 ctx 头部（OpSetDesc 数组 + 通信域信息 + `configValid`），变长 `Rule[]` 紧随其后（[L161-L164](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/examples/06_tuner_plugin/plugin.cpp#L161-L164) 的 `GetRules` 用指针算术访问）。

**② 维度词表与核心一致**——[plugin.cpp:L39-L44](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/examples/06_tuner_plugin/plugin.cpp#L39-L44)：插件自带 `g_validEngines/g_validExecutors/g_validTemplates` 三个词表，注释说明与 `alg_parse.cc` 的维度定义保持一致（engine 5 种、executor 5 种、template 10 种，即 u8-l3 的 `hccl_algo_dims.h` 枚举）。JSON 里写了词表外的值会被 Schema 校验拦截（[L383-L413](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/examples/06_tuner_plugin/plugin.cpp#L383-L413)）。

**③ 规则匹配**——[plugin.cpp:L548-L600](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/examples/06_tuner_plugin/plugin.cpp#L548-L600)：`MatchRule` 对每个「已设置」的条件做 AND 判断（未设置的条件不参与），覆盖 ranks/bytes 范围、dataType 枚举比较、commName 子串匹配（`strstr`）、NPU/服务器/Pod/超节点范围与 bufferSize 精确匹配。

**④ 应用规则**——[plugin.cpp:L602-L640](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/examples/06_tuner_plugin/plugin.cpp#L602-L640)：`ApplyRule` 遍历全部条目，三维名逐维 `strcmp`（空串维度当通配符）；命中且原 cost 非负则改写为规则 cost，并经 `logFunction` 打出 `[TunerDFX] modify: ... cost 旧 -> 新`——这是现场验证规则是否生效的关键日志。

**⑤ 示例配置**——[hccl_tuner_config.json:L4-L31](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/examples/06_tuner_plugin/hccl_tuner_config.json#L4-L31)：第一条规则「8~16 卡、0~64KB、fp16 的 AllReduce → aicpu/sole/nhr，cost 0.0」；第二条「8 卡、64KB~4GB → aicpu/sole/meshoneshot」。字段语义与全部可选项见 [README.md:L31-L99](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/examples/06_tuner_plugin/README.md#L31-L99)（其中 `match` 四个范围字段、engine/executor/template、cost 为必填，且规则间 first-match-wins）。

**⑥ 导出入口符号**——[plugin.cpp:L830-L831](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/examples/06_tuner_plugin/plugin.cpp#L830-L831)：

```cpp
hcclTunerFuncs_v1_t hcclTunerPlugin_v1 = {MyInit, MyGetCollInfo, sizeof(hcclTunerFuncs_v1_t)};
```

一个全局变量完成「实现 + 导出」，与 4.1 讲的 dlsym 约定严丝合缝。编译产物由 [Makefile:L42-L43](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/examples/06_tuner_plugin/Makefile#L42-L43) 产出（`-fPIC -shared`，首次构建自动下载 nlohmann/json 到 `third_party/`）。

**⑦ 单元测试手法**——[test_plugin.cpp:L11-L53](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/examples/06_tuner_plugin/test/test_plugin.cpp#L11-L53)：先手写 `memset_s/snprintf_s` 等 securec 函数的 stub（测试不链接 securec 库），再 `#define HCCL_TUNER_TESTING` + `#include "../plugin.cpp"` 把插件源码整体纳入测试二进制；[L59-L104](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/examples/06_tuner_plugin/test/test_plugin.cpp#L59-L104) 提供 mock 版 `ctxCreate/Get/Destroy`（就是一块 calloc 内存）与 mock 日志。这与 u7-l4 讲过的「stub 桩遮蔽重依赖」是同一手法。

#### 4.4.4 代码实践

**实践目标**：编译参考插件与单测，编写一条「8 卡小字节 AllReduce 偏好 aicpu/sole/nhr」的规则并验证命中。本任务不需要 NPU。

1. **编译插件**：

   ```bash
   source /usr/local/Ascend/cann/set_env.sh
   cd examples/06_tuner_plugin
   make          # 产物 hccl_tuner_example.so（首次会自动下载 nlohmann/json）
   ```

2. **改写规则**：把仓库自带的 [hccl_tuner_config.json](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/examples/06_tuner_plugin/hccl_tuner_config.json) 复制一份，将其中的 allreduce 第一条规则改为精确 8 卡、小字节（可去掉 `data_type` 让匹配更宽松）：

   ```json
   {
     "version": 1,
     "op_types": {
       "allreduce": {
         "rules": [
           {
             "match": { "min_ranks": 8, "max_ranks": 8,
                        "min_bytes": 0, "max_bytes": 65536 },
             "engine": "aicpu",
             "executor": "sole",
             "template": "nhr",
             "cost": 0.0
           }
         ]
       }
     }
   }
   ```

3. **编译并运行单测**：

   ```bash
   cd test
   make
   ./test_plugin
   ```

   test_plugin 内部会以 mock hostFuncs + mock 算法条目驱动 `MyInit/MyGetCollInfo`，观察输出中的 `[TunerDFX] rule hit` 与 `[TunerDFX] modify: ... cost 旧 -> 0.000000` 日志（来自 [plugin.cpp:L807-L817](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/examples/06_tuner_plugin/plugin.cpp#L807-L817)）。

4. **（可选，上板验证）**在真实 8 卡环境：`export HCCL_TUNER_PLUGIN=$PWD/hccl_tuner_example.so`、`export HCCL_TUNER_CONFIG_FILE=$PWD/hccl_tuner_config.json`、`export HCCL_USE_NEW_SELECTOR=1`，运行任一 AllReduce 示例并开 INFO 日志，在 `SelectMinCost` 打印的 cost table 中确认 `AicpuAllReduceSoleNHR` 的 cost 变为 0 且被选为 algName。

**需要观察的现象**：步骤 3 中规则命中日志显示 `engine=aicpu executor=sole template=nhr`，且被修改条目的 cost 变为 0.0；把 `min_ranks` 改成 9 后同样的输入应打出 `no rule matched`。
**预期结果**：插件按 first-match-wins 命中并只修改三维名匹配的条目；上板场景下最终 `SelectorEngine` 日志输出选中 `AicpuAllReduceSoleNHR`。
（待本地验证：make/test_plugin 可在 x86 主机直接执行；步骤 4 需要 8 卡 NPU 环境。）

#### 4.4.5 小练习与答案

**练习 1**：`MyInit` 里为什么要「两遍」解析 JSON（先 `CountRules` 再 `ParseConfig`）？
**答案**：规则表要存进 `ctxCreate` 分配的一块连续内存（`StoredHeader` + 变长 `Rule[]`），分配前必须知道精确大小（[plugin.cpp:L690-L710](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/examples/06_tuner_plugin/plugin.cpp#L690-L710)）。第一遍遍历 JSON 树统计 `totalRules`，按 `sizeof(StoredHeader) + n * sizeof(Rule)` 精确分配，第二遍才填充——避免 vector 扩容或二次分配。

**练习 2**：Schema 校验发现错误时（如规则缺 `cost` 字段），插件的行为是什么？为什么不是「跳过坏规则、用好规则」？
**答案**：`schema.errors > 0` 时置 `ctx->configValid = 0`（[plugin.cpp:L738-L747](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/examples/06_tuner_plugin/plugin.cpp#L738-L747)），此后 `MyGetCollInfo` 一律不干预（[L793-L795](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/examples/06_tuner_plugin/plugin.cpp#L793-L795)）。选择整体失效而非局部容错，是因为配置错误往往意味着用户意图与实际生效规则不一致，静默跳过坏规则可能让用户误以为某条调优规则已生效，宁可全部回退 CostModel 并打告警。

**练习 3**：`MyGetCollInfo` 开头为什么要检查 `ctxSize < sizeof(StoredHeader)`（C17 注释）？
**答案**：防御 ctx 内容与插件预期结构不一致（例如宿主 engine 实现变更、或未来 ABI 演进导致布局错位）时的越界读——ctxSize 是 `ctxGet` 返回的实际分配大小，小于 `StoredHeader` 就说明后面变长 `Rule[]` 的读法不可信，直接放弃干预（[plugin.cpp:L782-L790](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/examples/06_tuner_plugin/plugin.cpp#L782-L790)）。

## 5. 综合实践

**任务：为你的集群写一条调优规则并全程验证生效链路。**

场景设定：假设你的 8 卡训练集群实测发现小字节 fp16 AllReduce 用 `aicpu/sole/nhr` 最快，但 CostModel 有时选了别的算法。请完成：

1. **编译**：`make` 产出 `hccl_tuner_example.so`（4.4.4 步骤 1）。
2. **写规则**：编辑 `hccl_tuner_config.json`，加一条 `min_ranks=8, max_ranks=8, min_bytes=0, max_bytes=32768, data_type=fp16` → `aicpu/sole/nhr, cost=0.0` 的规则，再故意加**第二条**把 `template` 写成非法值（如 `"ring"`），观察 Schema 校验告警与「插件不干预」的整体失效行为（对应练习 4.4.5-2），然后删掉坏规则。
3. **单测验证**：`test/make && ./test/test_plugin`，从 `[TunerDFX]` 日志确认命中与 cost 改写。
4. **因果链复盘**：对照源码写出完整链路——`HcclTunerInit`（[selector_engine.cc:L190-L194](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/selector_engine.cc#L190-L194)）→ `CostTableGen` → `Enrich` 填三维名 → `HcclTunerCallGetCollInfo` 改 `AicpuAllReduceSoleNHR` 条目 cost 为 0 → `SelectMinCost` 取最小输出该 algName → `HcclExecOp` 查注册表执行（u3-l4）。每一步标注对应源码文件与行号。
5. **思考题**（写下你的答案）：如果插件 `getCollInfo` 每次耗时 150ms，第几次调用后 Tuner 被禁用？禁用后已建通信域的算法选择由谁接管？

**参考答案（第 5 步）**：第 3 次调用后禁用（连续 3 次超 100ms 阈值，[tuner_setup.cc:L242-L251](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/tuner/tuner_setup.cc#L242-L251)）；此后 `HcclTunerCallGetCollInfo` 入口直接 no-op，选择完全由 CostModel 估算值接管——注意 CostModel 本身仍有效（它按通信域缓存在 ctx 中，不受插件禁用影响）。

## 6. 本讲小结

- **ABI 契约**：插件 `.so` 导出全局变量 `hcclTunerPlugin_v1`（函数表 v1：`init` + `getCollInfo`）；核心把 cost table 原样传给插件（`CostTable` 的元素类型就是 `hcclTunerAlgoEntry_t`），插件**只改 cost**，三维名由 `AlgoNameMapper::Enrich` 预先填好。
- **生命周期与容错**：`HcclTunerInit` 每 comm 一次（dlopen + dlsym + 引用计数），任何失败一律 no-op 回退 CostModel；`HcclTunerDestroy` 只减引用计数、故意不 dlclose，防在途调用竞争。
- **慢调用保护**：`getCollInfo` 连续 3 次超 100ms 自动禁用插件（原子计数、达到阈值才持锁改状态），init 慢（>5s）只告警不禁用。
- **干预语义**：Tuner 不指定算法，只操纵代价——`SelectMinCost` 取最小 cost 输出 algName，`cost<0` 即被过滤；因此 `cost=0.0` 是「偏好」，可能与其他 0 值算法平票。
- **参考实现**：`examples/06_tuner_plugin` 把干预简化为 JSON 规则（match 条件全 AND、first-match-wins、Schema 校验失败则整体不干预），其 `test/test_plugin` 用「stub securec + #include 源码 + mock hostFuncs」实现无 NPU 单测。

## 7. 下一步学习建议

本讲是 Unit 8 也是整条学习路线代价模型部分的收尾。建议：

1. **横向对照 u6-l1**：HCCL 加载 HCOMM（dlopen libhcomm.so + 弱符号封装）与加载 Tuner 插件（dlopen + 函数表变量）是同一机制的两种用法，对比阅读 `hccl_dl.cc` 与 `tuner_setup.cc` 能加深对「宿主-插件」架构的理解。
2. **动手扩展**：把 `plugin.cpp` 的 JSON 引擎换成基于通信域内实际耗时反馈的在线调优（利用 `hostFuncs->ctxCreate` 存历史观测），这是 Tuner 框架留给二次开发的最大空间——注意慢调用保护的存在意味着重计算必须放在 init 阶段而非 getCollInfo。
3. **回归主线**：至此「Selector → Executor → Template」与新选择器全链路已讲完，可回到 [src/ops/op_common/op_common.cc](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/op_common.cc) 从 `Selector()`/`HcclExecOp()` 门面出发做一次端到端的源码通读，检验整本手册的知识是否串成一线。
