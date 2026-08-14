# 算法三维命名与 HCCL_ALGO 解析 alg_parse

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 HCCL 新选择器体系下的「算法三维命名」：一个算法由 **engine（引擎）/ executor（执行器）/ template（模板）** 三个维度唯一确定，并理解 `hccl_algo_dims.h` 中的维度枚举。
2. 理解 `AlgoNameMapper` 如何把内部的驼峰 algName（如 `AicpuAllReduceSoleMeshOneShot`）拆解为用户可读、可配置的三维名（`aicpu` / `sole` / `meshoneshot`），以及它的「2D 预计算表 + 缓存」两段式设计。
3. 掌握 `HcclAlgoParser` 对 `HCCL_ALGO` 新格式（`opType:executor{level0=algo,...}` + `not()` 排除语法）的递归下降解析，以及 `UpdateCostModelWithAlgo` 如何按「反向遍历、OpType 隔离、count=0 排除」的规则刷新 CostModel。

本讲是 Unit 8 第三讲：u8-l1 讲了 SelectorEngine 的双路径分发，u8-l2 讲了 CostModel/CostTable 的代价建模，本讲补上两块「翻译层」——**内部 algName ↔ 用户三维名** 的双向翻译：

- 正向（配置 → 模型）：用户写 `HCCL_ALGO`，`alg_parse` 解析后过滤 CostModel；
- 反向（模型 → 用户）：`AlgoNameMapper` 把 cost table 里的 algName 拆成三维名，供 Tuner 插件（u8-l4）按维度修改 cost。

## 2. 前置知识

- **algName 契约**（u3-l1/u3-l2）：Selector 产出一个驼峰字符串（如 `CcuMSAllReduceSoleMesh`），它是 executor/template 注册表的键。本讲要拆解的就是这个字符串的内部结构。
- **CostModel / CostAlgoParams**（u8-l2）：CostModel 是按「通信域 × 引擎」缓存的算法代价表，每个条目 `costAlgoParams[i]` 含 `algName`、代价参数 `param` 与 `count`；**count=0 是「被过滤/排除」的标记**，`SelectMinCost` 只会在 count≠0 的条目里比价。
- **AllAlgos 算法全集**（u3-l1/u3-l4）：executor 注册宏在登记执行器的同时经 `AddAlgToAllAlgos` 把算法元数据写入全局 `AllAlgos`，使「可枚举的算法」与「可查表的执行器」天然同步——本讲的 `AlgoNameMapper::Init` 正是以 AllAlgos 为输入。
- **递归下降解析**：一种手写解析器技术，每个文法规则对应一个函数，函数间相互调用，形如 `ParseSegment → ParseExecutorExpr → ParseExecutorUnitOrAtom → ParseTemplateList → ...`。不熟悉也没关系，读源码时把它当成「逐字符消费输入、按语法结构递归」即可。
- **驼峰命名（camelCase / PascalCase）**：`meshoneshot` 是小写粘写（用户配置用的 key），`MeshOneShot` 是帕斯卡命名（algName 中拼接用的片段），`mesh_one_shot` 是下划线写法（用户也可写，解析时会转成小写粘写）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/ops/op_common/selector/hccl_algo_dims.h` | 三维维度的 C 枚举（引擎 5 种、执行器 5 种、模板 6 种）与 `HcclOpTypeToPascal` 算子名转换 |
| `src/ops/op_common/selector/algo_name_mapper.h/.cc` | `AlgoNameMapper` 单例：内部 algName → 用户三维名的反向映射（2D 预计算表 + 缓存） |
| `src/common/alg_parse.h/.cc` | `HcclAlgoParser` 递归下降解析器、`FilterCmByHcclAlgo` / `UpdateCostModelWithAlgo` CostModel 刷新、维度名派生数组 |
| `src/ops/op_common/selector/selector_engine.cc` | 调用方：`InitCostModel` 中做 mapper 一次性初始化与 HCCL_ALGO 过滤，`Run` 中做 Enrich |
| `src/ops/op_common/selector/cost_model.h` | `AllAlgos` / `CostModel` / `CostAlgoParams` 数据结构定义 |
| `test/ut/common/alg_parse/alg_parse_test.cc` | 解析器单测：正反用例覆盖全部语法特性 |

一条数据先记住（后文反复用）：内部 algName 的拼接文法是

```
algName := EnginePascal + OpTypePascal + ExecutorPascal + TemplatePascal{1..N}
           例：Aicpu + AllReduce + Sole + MeshOneShot
```

这正是 [alg_parse.cc:449-462](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/alg_parse.cc#L449-L462) 中 `ComposeAlgoName` 的拼法（正向），也是 `AlgoNameMapper::Lookup2D` 拆法的依据（反向）。

## 4. 核心概念与源码讲解

### 4.1 三维命名体系与 hccl_algo_dims.h

#### 4.1.1 概念说明

在旧选择器（u3-l2）里，「算法」对用户是一个黑盒的 algName 字符串。新选择器把它显式拆成三个用户可配置的维度：

- **engine（引擎）**：由哪种通信引擎执行——AICPU、CCU（分 MS/SCHED 两种形态）、AIV、DPU；
- **executor（执行器）**：用什么编排模式——sole（单级）、sequence（串行多级）、parallel、pipeline、concur；
- **template（模板）**：用什么搬运算法——mesh、nhr、mesh_two_shot、mesh_one_shot 等。

这样设计有两个直接受益者：一是 **用户**——`HCCL_ALGO` 可以按维度表达「我只要 aicpu 引擎下的 sole 编排 + nhr 算法」，而不必记忆完整驼峰名；二是 **Tuner 插件**（u8-l4）——插件收到的是带三维名的条目数组，可以按「引擎 × 执行器 × 模板」任意粒度改 cost。

#### 4.1.2 核心流程

`hccl_algo_dims.h` 用三组 C 枚举把维度值暴露给外部（Tuner 插件的 C ABI 也依赖这类头）：

```
hcclEngineType_t   : AICPU | CCU_MS | CCU_SCHED | AIV | DPU          （5 种）
hcclExecutorType_t : SEQUENCE | SOLE | PARALLEL | PIPILINE | CONCUR  （5 种）
hcclTemplateType_t : MESH | NHR | MESH_TWO_SHOT | MESH_ONE_SHOT
                   | MESH_CHUNK | MESH_2DIE                          （6 种）
```

另有一个纯函数 `HcclOpTypeToPascal`，把 `HcclCMDType` 枚举翻译成 algName 中间的算子名片段（`HCCL_CMD_ALLREDUCE → "AllReduce"`），它是「引擎前缀」与「执行器+模板后缀」之间的**切分锚点**——这是反向拆名能成立的关键：algName 里只有算子名是双方都知道边界的。

注意维度枚举与 `alg_parse.cc` 里的字符串表（下文 4.3.1）是**两套平行定义**：枚举给 C ABI 用，字符串表给解析/映射用；新增一个维度值需要两处同步。

#### 4.1.3 源码精读

引擎维度枚举（[hccl_algo_dims.h:17-25](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/hccl_algo_dims.h#L17-L25)）——5 种引擎 + COUNT 哨兵：

```c
typedef enum {
    HCCL_ENGINE_AICPU = 0,
    HCCL_ENGINE_CCU_MS,
    HCCL_ENGINE_CCU_SCHED,
    HCCL_ENGINE_AIV,
    HCCL_ENGINE_DPU,
    HCCL_ENGINE_COUNT
} hcclEngineType_t;
```

模板维度枚举（[hccl_algo_dims.h:37-46](https://github.com/gitcode.com/cann-hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/hccl_algo_dims.h#L37-L46)）——注意这里只有 6 种「代表」，实际模板家族远多于此（见 ALGO_TYPES 13 种），枚举是面向外部的归并视图。

算子名切分锚点 `HcclOpTypeToPascal`（[hccl_algo_dims.h:50-72](https://github.com/gitcode.com/cann-hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/hccl_algo_dims.h#L50-L72)）——把 8 种 `HcclCMDType` 映射为 PascalCase 名，未知算子返回 `nullptr`：

```c
static inline const char* HcclOpTypeToPascal(HcclCMDType opType)
{
    switch (opType) {
        case HCCL_CMD_ALLREDUCE:
            return "AllReduce";
        ...
        default:
            return nullptr;
    }
}
```

#### 4.1.4 代码实践

1. **实践目标**：亲手验证 algName 的「四段拼接」结构，并确认 `HcclOpTypeToPascal` 覆盖的算子集合。
2. **操作步骤**：
   - 在仓库内搜索 `REGISTER_EXECUTOR_BY_FOUR_TEMPS`（u3-l4 讲过），任选 3 个注册点，记下它们的 algName 字符串；
   - 对照 `HcclOpTypeToPascal` 的 switch 分支，把每个 algName 手工切成 `Engine | OpType | Executor | Template` 四段。
3. **需要观察的现象**：algName 永远以引擎 Pascal 前缀开头（`Aicpu`/`CcuMS`/`CcuSched`/`Aiv`/`Dpu`），中间一定能找到算子 Pascal 名作为子串，后半段是执行器+模板的连续拼接（无分隔符）。
4. **预期结果**：例如 `CcuMSAllReduceSoleMesh` → `CcuMS | AllReduce | Sole | Mesh`。若遇到四段切不开的名字（如两级模板 `...MeshNHR`），说明模板段有多个——这正是 4.3 节 `MatchesAlgoPattern` 要处理的情况。

#### 4.1.5 小练习与答案

**练习 1**：为什么三维枚举里引擎有 `HCCL_ENGINE_CCU_MS` 和 `HCCL_ENGINE_CCU_SCHED` 两个值，而 u2-l4 的 `CommEngine` 里 CCU 只是一个值？

**答案**：`CommEngine` 是物理引擎大类（AICPU_TS/AIV/CCU）；而 algName 维度里的 `CcuMS`/`CcuSched` 是 CCU 之下的两种**展开模式**（CCU_MS 与 CCU_SCHED，对应 u2-l4 的 OpExpansionMode）。三维命名面向「用户可配置的算法粒度」，所以拆得更细；两套粒度经 `ENGINE_PREFIX_MAP`/`CandidateEnginesToPrefixes`（u8-l1）桥接。

**练习 2**：`HcclOpTypeToPascal` 返回 `nullptr` 意味着什么？

**答案**：该算子不在三维命名的支持范围内（如 Send/Recv 类点对点算子）。`AlgoNameMapper::Init` 遇到时会打 WARNING 并跳过该算法——即这类算法没有三维名，Tuner 插件看不到它们（`Enrich` 会把三维名填成空串）。

### 4.2 AlgoNameMapper：algName → 三维名的反向映射

#### 4.2.1 概念说明

`AlgoNameMapper` 解决的问题是：cost table 里的条目只有内部 algName，而 Tuner 插件（u8-l4）的回调接口 `getCollInfo` 拿到的每个条目需要 `engineName/executorName/templateName` 三个用户可读字段。把「驼峰长名拆三维」做成一个**启动时一次性预计算、运行时纯查表**的单例，避免每次算子调用都做字符串解析。

它内部其实是「1D + 2D」两张表：

- **1D**：引擎表——algName 前缀（`Aicpu`）→ 引擎用户名（`aicpu`），直接线性查 `GetAlgoEngines()`；
- **2D**：执行器 × 模板的笛卡尔积预计算表——`SoleMeshOneShot` → (`sole`, `meshoneshot`)。注释说 30 条，是按当时 EXECUTOR_TYPES(6) × ALGO_TYPES(13) 的有效组合算出的量级（构建耗时 <0.1ms）。

#### 4.2.2 核心流程

```
Init（进程内 call_once，输入 = AllAlgos 全集）:
  1. BuildMap2D：EXECUTOR_TYPES × ALGO_TYPES 笛卡尔积
       key = ExecutorPascal + TemplatePascal（如 "SoleMeshOneShot"）
       value = (executorKey, templateKey)（如 ("sole", "meshoneshot")）
  2. 遍历 AllAlgos 每个算法：
       a. HcclOpTypeToPascal(opType) 得到切分锚点（nullptr 则跳过并告警）
       b. Lookup2D(algName, opTypePascal):
            - find(opTypePascal) 定位锚点
            - 锚点前的子串 = 引擎 Pascal，查引擎表 → engineUser
            - 锚点后的子串 = execTpl，查 2D 表 → (executorUser, templateUser)
       c. 成功则 cache_[algName] = dims；失败打 WARNING（该算法无三维名）

Enrich（每次算子调用、CostTableGen 之后，仅 Tuner 已加载时）:
  对 cost table 每个条目查 cache_：
    命中 → 填 engineName/executorName/templateName
    未命中 → 三维名置空串（Tuner 无法按维度识别它）
```

调用时机在 [selector_engine.cc:122-126](https://github.com/gitcode.com/cann-hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/selector_engine.cc#L122-L126)（Init，`std::call_once` 全局一次）与 [selector_engine.cc:214-219](https://github.com/gitcode.com/cann-hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/selector_engine.cc#L214-L219)（Enrich，在调用 Tuner 插件改 cost 之前）。

#### 4.2.3 源码精读

查询结果结构 `AlgoDims`（[algo_name_mapper.h:24-29](https://github.com/gitcode.com/cann-hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/algo_name_mapper.h#L24-L29)）——三个指针指向静态字符串表的 key，无需释放：

```cpp
struct AlgoDims {
    const char* engineUser;   /* "aicpu" */
    const char* executorUser; /* "sole" */
    const char* templateUser; /* "mesh_one_shot" */
};
```

构建 2D 表（[algo_name_mapper.cc:27-40](https://github.com/gitcode.com/cann-hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/algo_name_mapper.cc#L27-L40)）——双重循环做笛卡尔积，value 是 `pair(executorKey, templateKey)`：

```cpp
for (int ex = 0; ex < execCount; ex++) {
    for (int t = 0; t < tplCount; t++) {
        std::string key = std::string(execs[ex].pascal) + tpls[t].pascal;
        map2D_[key] = {execs[ex].key, tpls[t].key};
    }
}
```

拆名核心 `Lookup2D`（[algo_name_mapper.cc:43-75](https://github.com/gitcode.com/cann-hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/algo_name_mapper.cc#L43-L75)）——以算子名为锚点切两刀：锚点前查 1D 引擎表，锚点后查 2D 表：

```cpp
size_t pos = algName.find(opTypePascal);   // 1. 定位算子名锚点
...
std::string enginePascal = algName.substr(0, pos);  // 2. 锚点前 = 引擎
for (int i = 0; i < engineCount; i++) {
    if (enginePascal == engines[i].pascal) { dims.engineUser = engines[i].key; break; }
}
...
std::string execTpl = algName.substr(pos + opTypePascal.size());  // 3. 锚点后 = 执行器+模板
auto it = map2D_.find(execTpl);
dims.executorUser = it->second.first;
dims.templateUser = it->second.second;
```

`Enrich`（[algo_name_mapper.cc:104-122](https://github.com/gitcode.com/cann-hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/algo_name_mapper.cc#L104-L122)）——纯查缓存填条目，未命中填空串：

```cpp
auto it = cache_.find(entries[i].algName);
if (it != cache_.end()) {
    entries[i].engineName = it->second.engineUser;
    entries[i].executorName = it->second.executorUser;
    entries[i].templateName = it->second.templateUser;
} else {
    entries[i].engineName = "";
    ...
}
entries[i].structSize = sizeof(hcclTunerAlgoEntry_t);
```

`Init` 以 AllAlgos 为输入（[algo_name_mapper.cc:78-101](https://github.com/gitcode.com/cann-hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/algo_name_mapper.cc#L78-L101)）；AllAlgos 的结构定义在 [cost_model.h:25-39](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/cost_model.h#L25-L39)——`AlgElement` 含 algName、executorName、模板名数组和 opType，由注册宏双写登记（u3-l4）。

#### 4.2.4 代码实践

1. **实践目标**：手工推演 `AicpuAllReduceSoleMeshOneShot` 的三维名，验证 2D 表拆分逻辑。
2. **操作步骤**：
   - 锚点：`HcclOpTypeToPascal(HCCL_CMD_ALLREDUCE)` = `"AllReduce"`，在 algName 中 `find` 到位置 5；
   - 锚点前：`"Aicpu"` → 对照 `ENGINE_TYPES`（[alg_parse.cc:22-23](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/alg_parse.cc#L22-L23)）中 `"aicpu" → "Aicpu"`，得 `engineUser = "aicpu"`；
   - 锚点后：`"SoleMeshOneShot"` → 拆成 `"Sole"` + `"MeshOneShot"`，对照 `EXECUTOR_TYPES`（`"sole"→"Sole"`）与 `ALGO_TYPES`（`"meshoneshot"→"MeshOneShot"`），得 `executorUser = "sole"`、`templateUser = "meshoneshot"`。
3. **需要观察的现象**：无需运行即可在纸上完成；若想验证，可在本机写一段小程序调用 `GetAlgoExecutors/GetAlgoTemplates` 打印全部维度 key（头文件 `alg_parse.h` 均已导出）。
4. **预期结果**：`{engineUser:"aicpu", executorUser:"sole", templateUser:"meshoneshot"}`；该结果会出现在 Tuner 插件收到的 `hcclTunerAlgoEntry_t` 条目里（u8-l4 实践会真实观察到）。

#### 4.2.5 小练习与答案

**练习 1**：`Enrich` 为什么不直接在每次调用时做 `Lookup2D`，而要维护 `cache_`？

**答案**：`Lookup2D` 含字符串 `find`/`substr` 和两次查表，`Enrich` 在**每次算子调用**的 cost table 上执行（条目数 = 候选算法数），而 algName 集合是静态的（AllAlgos 在注册期固定）。`Init` 一次性把全部算法拆好放进 `cache_`，`Enrich` 退化为一次 `unordered_map::find`，把字符串解析成本从热路径挪到启动路径。

**练习 2**：如果一个新注册的算法 algName 拼写不符合「Engine+OpType+Executor+Template」约定，会发生什么？

**答案**：`Lookup2D` 返回 false，`Init` 打 WARNING `[AlgoNameMapper] lookup failed, algName=...` 并不缓存；该算法在 CostModel 比价中不受影响（FilterCmByHcclAlgo 用的是自己的 `MatchesAlgoPattern`），但 Tuner 插件看到的它三维名为空串，无法按维度识别。这也是 algName 契约（u3-l1「字符串契约两端必须同步」）的又一处体现。

### 4.3 HcclAlgoParser 与 UpdateCostModelWithAlgo：HCCL_ALGO 正向解析

#### 4.3.1 概念说明

`HCCL_ALGO` 在新选择器体系下换了一副面孔。旧格式（u4-l3，按 `/`、`;`、`:` 切分的 level0/1/2 算法族）只作用于 910_93（A3）的旧选择器；新格式是面向三维命名的表达式语法，标准形式为：

```
HCCL_ALGO := segment (';' segment)*
segment  := [opType ':'] executor_expr
executor_expr := 'not' '(' executor_unit ')'      ← 整段取非（enable=false）
               | executor_unit
               | template_name                     ← 简写，等价 sole{template_name}
executor_unit  := executorType '{' tpl_list '}'
tpl_list := tpl_item (',' tpl_item)*
tpl_item := ['level'N '='] tpl_expr
tpl_expr := 'not' '(' template_name ')'           ← 单个算法取非
          | template_name
```

四个词表（全部小写粘写为 key、Pascal 为 value）定义在 [alg_parse.cc:22-52](https://github.com/gitcode.com/cann-hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/alg_parse.cc#L22-L52)：

| 词表 | 条目数 | 示例 |
| --- | --- | --- |
| `ENGINE_TYPES` | 5 | `aicpu`、`aiv`、`dpu`、`ccums`、`ccusched` |
| `OP_TYPES` | 11 | `allreduce`、`allgather`、`reducescatter`… |
| `EXECUTOR_TYPES` | 6 | `sole`、`sequence`、`parallel`、`pipeline`、`concur`、`strictordered` |
| `ALGO_TYPES` | 13 | `mesh`、`nhr`、`meshoneshot`、`mesh2die`、`meshchunk`… |

注意三维枚举（4.1）与这里的字符串表是平行定义：枚举面向 C ABI，字符串表面向解析。用户写 `mesh_one_shot`、`meshoneshot` 都可以——`UnderscoreToCamelCase` 先转 `meshOneShot` 再 `ToLowerStr` 成 `meshoneshot` 归一化。

解析产物是 `executorList`：`HcclAlgoExecutor{opType, executorType, algoList[level→HcclAlgo{algoType, enable}]}`，其中 **level 不显式存储，由 algoList 的下标隐式表达**（[alg_parse.h:29-46](https://github.com/gitcode.com/cann-hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/alg_parse.h#L29-L46)）。

#### 4.3.2 核心流程

整条链路是（入口在 [alg_parse.cc:742-794](https://github.com/gitcode.com/cann-hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/alg_parse.cc#L742-L794) 的 `FilterCmByHcclAlgo`，由 `SelectorEngine::InitCostModel` 在 CostModel 建好并做完引擎过滤之后调用）：

```
FilterCmByHcclAlgo(comm, cm, candidateEngineNames):
  1. 取配置：优先通信域级 hcclAlgo（HcclGetHcclAlgo），为空再取环境变量 HCCL_ALGO
     两者都空 → 直接返回，不过滤
  2. HcclAlgoParser::Parser(algoConfig) 递归下降解析
     解析失败且设备不是 910_93 → 退回旧规则 SetHcclAlgoConfig（A3 兼容路径）
  3. UpdateCostModelWithAlgo(algoParser, cm, candidateEngineNames) 刷新 CostModel
  4. HCCL_DEBUG 打印过滤后每个条目的 algName:count
```

`UpdateCostModelWithAlgo`（[alg_parse.cc:674-740](https://github.com/gitcode.com/cann-hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/alg_parse.cc#L674-L740)）的规则（头文件注释 [alg_parse.h:64-77](https://github.com/gitcode.com/cann-hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/alg_parse.h#L64-L77) 也写明了）：

1. 先 `ExcludeAlgosNotInEngines`：把不属于候选引擎前缀的条目 count 置 0（send/recv 类豁免）；
2. **反向遍历** `executorList`（下标大的、即配置里靠后的段优先级高）；
3. **OpType 隔离**：每个 opType 只被第一个（反向遍历中最先遇到的）命中它的段处理，处理过即进 `matchedOpTypes` 不再参与；
4. 所有 opType 都处理完则提前退出；
5. `enable=false` 的语义是**排除**：把匹配到的条目 count 置 0。

对每个段，`CollectMatchedNamesForOpType`（[alg_parse.cc:569-609](https://github.com/gitcode.com/cann-hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/alg_parse.cc#L569-L609)）按三种模式匹配条目：

| 模式 | 触发条件 | 行为 |
| --- | --- | --- |
| **模式 A：含取非算法** | `algoList` 里有 `enable=false` 的项 | 用 `MatchesAlgoPattern` 模式匹配收集「符合」的条目名（正选） |
| **模式 B：仅 executorType** | `algoList` 为空（如 `allreduce:sequence{}` 不会出现，但前缀匹配场景存在） | 按 `ComposeAlgoPrefix` 前缀匹配，正选收集 / 取非直接置 0 |
| **模式 C：全精确名** | `algoList` 全部 `enable=true` | `ComposeAlgoName` 拼出完整 algName 精确查 `keyToIdx` |

匹配后 `ProcessMatchedResults`（[alg_parse.cc:613-635](https://github.com/gitcode.com/cann/hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/alg_parse.cc#L613-L635)）执行「**白名单收口**」：正选匹配成功后，该 opType 下**所有未被匹配到的条目 count 一并置 0**（send/recv 豁免）——也就是说一段正选配置既选入又排除，最终该 opType 只剩匹配条目参与 `SelectMinCost` 比价。

`MatchesAlgoPattern`（[alg_parse.cc:493-523](https://github.com/gitcode.com/cann-hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/alg_parse.cc#L493-L523)）处理多级模板名的逐段匹配：前缀 `Engine+OpType+Executor` 必须精确命中，剩余部分按 `SORTED_ALGO_NAMES`（**长度降序**，避免 `Mesh` 误吞 `Mesh2Die` 的前缀）逐个剥算法段；每个 level 上 `enable=true` 必须「是指定算法」、`enable=false` 必须「不是指定算法」，且剥完必须恰好耗尽（`pos == remaining.size()`），防止拼错名部分命中。

#### 4.3.3 源码精读

解析文法入口 `AlgoParserImpl::Parse`（[alg_parse.cc:93-118](https://github.com/gitcode.com/cann-hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/alg_parse.cc#L93-L118)）——循环解析 segment、分号分隔、结尾不得有残余字符，标准格式写进报错信息：

```cpp
while (!AtEnd()) {
    HcclAlgoExecutor exec;
    CHK_RET(ParseSegment(exec));
    CompactAlgoList(exec.algoList);
    result.push_back(std::move(exec));
    SkipWs();
    if (Eat(';')) { SkipWs(); continue; }
    break;
}
```

整段取非与简写展开 `ParseExecutorExpr`（[alg_parse.cc:229-251](https://github.com/gitcode.com/cann-hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/alg_parse.cc#L229-L251)）——识别 `not(` 则置 `exec.enable = false`；`ParseExecutorUnitOrAtom`（[alg_parse.cc:254-291](https://github.com/gitcode.com/cann-hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/alg_parse.cc#L254-L291)）里，裸模板名走简写路径：

```cpp
// template shorthand: name => sole{name}
std::string algoType = ToLowerStr(UnderscoreToCamelCase(name));
if (ALGO_TYPES.find(algoType) == ALGO_TYPES.end()) { ... return HCCL_E_PARA; }
exec.executorType = "sole";
```

即 `mesh_chunk` 等价于 `sole{meshchunk}`，单测 `ParseShorthand`（[alg_parse_test.cc:194-210](https://github.com/gitcode.com/cann-hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/test/ut/common/alg_parse/alg_parse_test.cc#L194-L210)）逐项验证了这一展开。

反向遍历主循环（[alg_parse.cc:687-738](https://github.com/gitcode.com/cann-hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/alg_parse.cc#L687-L738)）——核心三行：

```cpp
for (int idx = static_cast<int>(algoParser.executorList.size()) - 1; idx >= 0; idx--) {
    ...
    if (exec.opType.empty()) {
        for (const auto& pair : OP_TYPES) {          // 全局段：作用于所有未处理的 opType
            if (matchedOpTypes.find(pair.first) == matchedOpTypes.end()) unprocessedOps.push_back(pair.first);
        }
    } else if (...matchedOpTypes.find(exec.opType) == matchedOpTypes.end()) {
        unprocessedOps.push_back(exec.opType);       // 定向段：只作用于指定且未处理的 opType
    }
    ...
    if (allMatched) { return HCCL_SUCCESS; }         // 所有 opType 处理完提前退出
}
```

白名单收口（[alg_parse.cc:615-631](https://github.com/gitcode.com/cann-hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/alg_parse.cc#L615-L631)）——正选后把该 opType 下未匹配的条目 count 置 0：

```cpp
if (!isMatched && !ContainsSendRecv(key)) {
    ctx.model.costAlgoParams[i].count = 0;
}
```

长名优先排序（[alg_parse.cc:477-487](https://github.com/gitcode.com/cann-hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/alg_parse.cc#L477-L487)）——注释直接给出动机：

```cpp
// algoType 驼峰名列表（按长度降序，避免 "Mesh" 误匹配 "Mesh2Die"）
```

配置来源优先级（[alg_parse.cc:744-758](https://github.com/gitcode.com/cann-hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/alg_parse.cc#L744-L758)）——通信域级 hcclAlgo 优先于环境变量，双双为空则跳过过滤：

```cpp
HcclResult ret = HcclGetHcclAlgo(comm, algoConfig);
...
if (algoConfig.empty()) {
    algoConfig = GetEnv("HCCL_ALGO");
    if (algoConfig == "EmptyString") { ...skip filtering...; }
}
```

#### 4.3.4 代码实践

1. **实践目标**：写一段含 `not()` 排除语法的 `HCCL_ALGO` 配置，用 `UpdateCostModelWithAlgo` 的「反向遍历 + OpType 隔离」规则手推 CostModel 的过滤结果。
2. **操作步骤**（源码阅读 + 本地可验证）：
   - 设配置（一行）：
     ```
     HCCL_ALGO='sole{nhr}; allreduce:not(sequence{level0=mesh2die,level1=nhrmultilink}); allreduce:sole{meshoneshot}'
     ```
   - 解析得到 `executorList`（可用 [alg_parse_test.cc:31-61](https://github.com/gitcode.com/cann-hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/test/ut/common/alg_parse/alg_parse_test.cc#L31-L61) 的断言方式核对）：`[0]=sole{nhr}`（全局）、`[1]=allreduce + not(sequence{mesh2die,nhrmultilink})`、`[2]=allreduce + sole{meshoneshot}`；
   - 按 `UpdateCostModelWithAlgo` 反向遍历：先处理 `[2]`——allreduce 属定向段，`sole{meshoneshot}` 全正选，走模式 C 精确拼名，对每个候选引擎拼出如 `aicpuAllReduceSoleMeshOneShot` 查 `keyToIdx`；命中后 allreduce 进 `matchedOpTypes`，且该 opType 下其它条目（如 `...SoleNHR`、`...SequenceMeshNHR`）count 被收口置 0；
   - 接着 `[1]`——allreduce 已处理，整段被 OpType 隔离跳过（**这就是反向优先级：配置里靠后的段先到先得**）；
   - 最后 `[0]`——全局段作用于剩余未处理的 opType（allgather、reducescatter…），同理正选 `sole{nhr}` 并收口。
   - 如需真实运行验证：按 u1-l4/u7-l4 执行 `bash build.sh -u` 跑 `test/ut/common/alg_parse` 下的单测（`alg_parse_test` 与 `update_cost_model_test`），或在测试中新增一个用例把上述配置喂给 `HcclAlgoParser::Parser` 后断言 `executorList` 内容。运行结果为「待本地验证」（本讲义编写环境未执行编译）。
3. **需要观察的现象**：日志 `[FilterCmByHcclAlgo] use algo config: [...]` 与 `[HcclAlgoParser] parse ok, HcclAlgoExecutorParser{...}`（[alg_parse.cc:412](https://github.com/gitcode.com/cann-hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/alg_parse.cc#L412)），以及 DEBUG 级的 `final costModel` 条目列表。
4. **预期结果**：allreduce 的 cost table 只剩各引擎下 `Sole MeshOneShot` 条目 count≠0；其余算子只剩 `Sole NHR` 族；`[1]` 段的 `not(...)` 因 OpType 已被 `[2]` 占据而完全不生效——印证「反向遍历、靠后优先、一 opType 一次机会」。

#### 4.3.5 小练习与答案

**练习 1**：`sole{nhr}; not(sole{meshoneshot})` 与 `sole{nhr, not(meshoneshot)}` 语义有何不同？

**答案**：前者是**整段取非**（`exec.enable=false`，模式 B/前缀路径），语义是「排除所有引擎下的 sole+meshoneshot 组合」，且因该段命中会把 opType 标记为已处理；后者是**算法级取非**（`HcclAlgo.enable=false`，模式 A 路径），配合 `nhr` 一起进入 `MatchesAlgoPattern`，语义是「level0 是 nhr 之外的任意算法且排除后仍须能拼出合法名」——用于在保留其它算法的同时点名排除一个。整段取非是黑名单，算法级取非是带通配的白名单。

**练习 2**：为什么 `ExcludeAlgosNotInEngines` 与 `ProcessMatchedResults` 都要豁免 `ContainsSendRecv` 的算法名？

**答案**：Send/Recv（点对点）算法不是三维命名体系的成员（`HcclOpTypeToPascal` 不覆盖），不应被面向集合通信的算法配置误伤；若被 count=0 排除，可能在 p2p 场景选不出算法。代码注释（[alg_parse.cc:545-547](https://github.com/gitcode.com/cann-hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/alg_parse.cc#L545-L547)）明确标注「send/recv 算法名不参与 count=0 的排除逻辑」。

**练习 3**：若 `HCCL_ALGO` 写了非法 token（如 `allreduce:bogus{mesh}`），运行期会怎样？

**答案**：`EXECUTOR_TYPES` 查不到 `bogus`，解析返回 `HCCL_E_PARA`（[alg_parse.cc:265-268](https://github.com/gitcode.com/cann-hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/alg_parse.cc#L265-L268)）；`FilterCmByHcclAlgo` 里若设备是 910_93 则直接上抛该错误，否则尝试旧规则 `SetHcclAlgoConfig` 兜底（[alg_parse.cc:764-772](https://github.com/gitcode.com/cann-hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/alg_parse.cc#L764-L772)）。解析失败时 `HcclAlgoParser::Parser` 会先 `executorList.clear()` 保证不留半截结果（[alg_parse.cc:408-411](https://github.com/gitcode.com/cann-hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/alg_parse.cc#L408-L411)）。

## 5. 综合实践

把本讲三块知识串成一条「双向翻译」闭环（纯源码阅读型实践，不依赖 NPU）：

1. **正向**：从 `examples/` 或注册宏中选定一个你熟悉的算法（推荐 `AicpuAllReduceSoleMeshOneShot`，u3-l5/u8-l2 已精读过它的模板）。
2. **拆名**：按 4.2.4 的步骤手工执行 `Lookup2D`，写出它的三维名，并对照 [alg_parse.cc:22-52](https://github.com/gitcode.com/cann-hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/alg_parse.cc#L22-L52) 的三张词表确认每个字段都是合法 key。
3. **构造配置**：写一段 `HCCL_ALGO`，要求：(a) 用 `opType:` 定向段让 allreduce **只能**选到该算法（任选一个引擎）；(b) 用一个 `not(...)` 段把某undesired模板（如 `nhrmultilink`）从其余算子中排除；(c) 至少用一次下划线写法和一次 level 显式写法。
4. **手推刷新**：按 `UpdateCostModelWithAlgo` 的规则逐步推出：哪些条目 count=0、哪个段被 OpType 隔离跳过、最终 `SelectMinCost` 的候选集。
5. **验证**：参照 [alg_parse_test.cc](https://github.com/gitcode.com/cann-hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/test/ut/common/alg_parse/alg_parse_test.cc) 的写法，把你的配置写成一个新 TEST_F 用例（只改 `hccl-tutorial/` 之外不动源码的前提下，可在本地 fork 分支试验；`bash build.sh -u` 运行），断言 `executorList` 各字段与你的手推一致。编译运行结果为「待本地验证」。

完成后再回看 [selector_engine.cc:168-172](https://github.com/gitcode.com/cann-hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/ops/op_common/selector/selector_engine.cc#L168-L172)：`FilterCmByEngine`（候选引擎过滤）与 `FilterCmByHcclAlgo`（本讲内容）一先一后，共同决定了 CostModel 里 count≠0 的候选集——你应当能说出这两道过滤各自依据的是哪两个维度。

## 6. 本讲小结

- 算法三维命名把 algName 拆为 **engine/executor/template** 三个用户可配置维度；algName 的拼接文法是 `EnginePascal + OpTypePascal + ExecutorPascal + TemplatePascal{1..N}`，算子 Pascal 名是反向拆分的锚点。
- `hccl_algo_dims.h` 提供维度 C 枚举（引擎 5/执行器 5/模板 6）与 `HcclOpTypeToPascal`，面向 Tuner C ABI；`alg_parse.cc` 的三张字符串词表（5/11/6/13）面向解析与映射，两者平行维护。
- `AlgoNameMapper` 用「1D 引擎表 + 2D 执行器×模板笛卡尔积表」在启动时（`call_once`）把 AllAlgos 全部拆名缓存，运行期 `Enrich` 一次查表填充 Tuner 条目，解析成本被挪出热路径。
- `HcclAlgoParser` 是递归下降解析器：支持 `[opType:]` 定向、`not()` 整段/算法级取非、裸模板名简写展开为 `sole{...}`、`levelN=` 显式层级、下划线转驼峰归一化。
- `UpdateCostModelWithAlgo` 的刷新规则是：先排除非候选引擎 → **反向遍历** executorList（靠后优先）→ **OpType 隔离**（每个 opType 只被一段处理）→ 正选段做白名单收口（未匹配置 count=0），send/recv 算法全程豁免。
- `FilterCmByHcclAlgo` 是入口：通信域级 hcclAlgo 优先于环境变量 `HCCL_ALGO`，解析失败在非 910_93 设备上回退旧规则。

## 7. 下一步学习建议

下一讲 **u8-l4 Tuner 插件框架与实践**：`hcclTunerAlgoEntry_t` 条目里被 `Enrich` 填入的三个维度名，正是 Tuner 插件 JSON 规则的匹配字段；建议先阅读 `src/ops/op_common/selector/inc/hccl_tuner_plugin.h` 的 C ABI 定义，再对照本讲 4.2/4.3 理解「三维名 → 插件改 cost → SelectMinCost 改变 algName」的完整闭环。若想巩固解析器本身，可通读 [alg_parse.cc](https://github.com/gitcode.com/cann-hccl/blob/b16b2eab48cea3d6d08bfb2e2acc45073d9fa61a/src/common/alg_parse.cc) 的 `AlgoParserImpl` 全部文法函数与 `test/ut/common/alg_parse/update_cost_model_test.cc`。
