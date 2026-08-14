# 算法选择器 Selector

## 1. 本讲目标

本讲深入 op_common 的第一个组件——**selector（算法选择器）**。在 u3-l1 里你已经知道 selector 的职责是「根据拓扑和入参，产出一个 `algName` 字符串」，但还没展开它内部到底是怎么「选」的。本讲就把这层打开。

学完后你应该能够：

1. 说清 **`SelectorRegistry` 注册表** 与 **`REGISTER_SELECTOR_BY_OPTYPE` 注册宏** 的工作机制：谁在什么时候、用什么键、把哪个 selector 登记进表里。
2. 读懂 **`ExecuteSelector::Run` 的优先级遍历** 与 **`MATCH` / `NOT_MATCH` 语义**：注册表如何被遍历、命中即返回、一个都命中不了怎么办。
3. 理解 **`AutoSelectorBase::Select` 的引擎分发**：一个 selector 内部如何按「引擎」分发到 `SelectCcuMsAlgo` / `SelectCcuScheduleAlgo` / `SelectAivAlgo` / `SelectAicpuAlgo`，以及 AllReduce 是如何据此产出形如 `AicpuAllReduceSoleNHR` / `CcuMSAllReduceSoleMesh` 的 algName。

本讲只讲 selector 本身。它产出的 `algName` 字符串如何被 `HcclExecOp` 拿去查 executor 注册表，是 u3-l4 的内容；topo 信息 `TopoInfoWithNetLayerDetails` 的字段含义是 u3-l3 的内容——本讲把这两者当作「已就绪的输入」来用。

## 2. 前置知识

本讲承接 **u3-l1（op_common 架构与三大注册表总览）**，并用到 **u2-l4（通信引擎选择与快速路径）** 的结论。请先确认以下几点：

- **三大注册表** 之一是 `SelectorRegistry`，注册宏是 `REGISTER_SELECTOR_BY_OPTYPE`。本讲展开它的实现。
- **引擎（engine）与算法（alg）正交**：u2-l4 讲过，进入 op_common 之前 `OpParam.engine`（更准确地说是 `opExecuteConfig`）就已经由 `HcclGetOpExpansionMode` 定好了。selector 的工作是「**为这个引擎挑一个合适的算法**」，而不是「挑引擎」。
- **algName 是一条脆弱的字符串契约**：selector 在一端产出它，executor 注册表在另一端用它做键查 executor。新增一个 algName，必须两端同步（u3-l1 已强调）。
- 几个 C++ 概念会反复用到：
  - **静态初始化（static initialization）**：全局/静态对象在 `main` 之前构造。注册宏正是靠这一点，在程序启动时就把 selector 登记。
  - **虚函数（virtual）**：基类声明 `virtual` 方法，子类 `override` 提供各自实现，运行时按对象的实际类型调用。本讲里 `AutoSelectorBase` 是基类，每个算子的 `XxxAutoSelector` 是子类。
  - **模板方法模式（Template Method）**：基类用一个**非虚**的 `Select()` 固定住「调度骨架」，把可变的步骤声明为 `virtual`，交由子类填。这是 selector 设计的核心。

## 3. 本讲源码地图

本讲涉及的关键文件及其作用：

| 文件 | 作用 |
| --- | --- |
| [src/ops/op_common/selector/selector_registry.h](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/selector/selector_registry.h) | `SelectorRegistry` 单例类与 `REGISTER_SELECTOR_BY_OPTYPE` 注册宏 |
| [src/ops/op_common/selector/selector_registry.cc](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/selector/selector_registry.cc) | 注册表的 `RegisterByOpType` / `GetSelectorsByOpType` 实现 |
| [src/ops/op_common/selector/execute_selector.h](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/selector/execute_selector.h) / [.cc](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/selector/execute_selector.cc) | `ExecuteSelector::Run()`：按优先级遍历 selector，直到 `MATCH` |
| [src/ops/op_common/selector/auto_selector_base.h](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/selector/auto_selector_base.h) / [.cc](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/selector/auto_selector_base.cc) | selector 抽象基类 `AutoSelectorBase`：`Select()` 调度骨架 + 各引擎 `Select*Algo` 虚函数 + algName 默认映射表 |
| [src/ops/all_reduce/selector/all_reduce_auto_selector.h](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/selector/all_reduce_auto_selector.h) / [.cc](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/selector/all_reduce_auto_selector.cc) | AllReduce 的具体 selector：override 各引擎算法选择，文件末尾注册 |
| [src/ops/op_common/op_common.cc](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/op_common.cc) | `Selector()` 入口：算拓扑 → 调 `ExecuteSelector::Run` → `SetCommEngine` |
| [src/ops/op_common/inc/alg_param.h](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/inc/alg_param.h) | `OpParam`（含 `opType` / `opExecuteConfig` / `engine`）、`OpExecuteConfig` 枚举 |

## 4. 核心概念与源码讲解

### 4.1 SelectorRegistry 与 REGISTER_SELECTOR_BY_OPTYPE 注册机制

#### 4.1.1 概念说明

selector 子系统要解决的问题是：**项目里有十几个算子，每个算子都有自己的「算法选择逻辑」，怎么把它们组织起来，让运行时能按算子类型找到对应的选择器？**

答案是一张注册表 `SelectorRegistry`。它的思路和 u3-l1 介绍的整体一致——**单例 + 静态初始化注册**：

- **单例**：全局只有一个注册表实例，通过 `SelectorRegistry::Global()` 拿到。
- **注册**：每个具体 selector（如 `AllReduceAutoSelector`）在它自己的 `.cc` 文件末尾，用宏 `REGISTER_SELECTOR_BY_OPTYPE` 把自己「**算子类型 + 优先级 → 选择器实例**」登记进表。
- **查表**：运行时按算子类型取出该算子的全部 selector，按优先级排序后逐个尝试。

理解这张表，关键是看懂它的**键**和**注册时机**。

#### 4.1.2 核心流程

注册表内部其实维护了 **两张 map**：

```text
impls_         :  priority(u32)            -> AutoSelectorBase*     // 不区分算子类型（MC2 特殊路径用）
opTypeImpls_   :  HcclCMDType -> (priority -> AutoSelectorBase*)    // 主路径：按算子类型分组
```

主路径用的是 `opTypeImpls_`，它是一个**二级 map**：先按算子类型 `HcclCMDType`（如 `HCCL_CMD_ALLREDUCE`）分桶，每个桶内部再用 `priority` 排序。

一次注册的流程：

```text
REGISTER_SELECTOR_BY_OPTYPE(HCCL_CMD_ALLREDUCE, 18, AllReduceAutoSelector)
   │  （发生在该 .cc 文件被加载时的静态初始化阶段，早于 main）
   ▼
SelectorRegistry::Global()->RegisterByOpType(HCCL_CMD_ALLREDUCE, 18, new AllReduceAutoSelector())
   │
   ▼
opTypeImpls_[HCCL_CMD_ALLREDUCE][18] = new AllReduceAutoSelector()
```

查表流程（见 4.2）则是反过来：`GetSelectorsByOpType(opType)` 取出该桶，得到一个 `std::map<u32, AutoSelectorBase*>`。

#### 4.1.3 源码精读

先看注册表类的声明（注意它有两张 map 和一把互斥锁，保证多线程注册安全）：

> [selector_registry.h:21-33](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/selector/selector_registry.h#L21-L33) —— `SelectorRegistry` 类声明，`opTypeImpls_` 是「算子类型 →（优先级 → 选择器）」的二级 map。

再看注册宏本身。这是整段机制最巧妙的地方，**它利用静态变量初始化来完成注册**：

> [selector_registry.h:45-53](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/selector/selector_registry.h#L45-L53) —— `REGISTER_SELECTOR_BY_OPTYPE` 宏。展开后是一个 `static HcclResult g_func_##... = SelectorRegistry::Global()->RegisterByOpType(optype, priority, new selector())`，即一个静态变量；该变量在程序启动、动态库加载时被初始化，从而「顺带」完成注册。

宏里两个细节值得注意：

- **`__COUNTER__`**：每次展开自增的计数器，拼进变量名 `g_func_##priority##_##name##_##ctr`，保证同一文件里多次注册、或不同文件注册时变量名不冲突。
- **`new selector()`**：注册时直接 `new` 出一个选择器实例常驻注册表。selector 是无状态的（选择逻辑只依赖入参 `opParam`/`topoInfo`），所以一个实例可以反复服务所有调用。

注册函数的实现做了**重复注册检查**（同算子类型、同优先级重复注册会报错）：

> [selector_registry.cc:38-47](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/selector/selector_registry.cc#L38-L47) —— `RegisterByOpType`：加锁后检查 `opTypeImpls_[opType]` 里是否已有该 `priority`，有则报 `HCCL_E_PARA`，否则插入。

查表函数在「该算子类型一个 selector 都没注册」时会打 WARNING 并返回空 map：

> [selector_registry.cc:49-56](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/selector/selector_registry.cc#L49-L56) —— `GetSelectorsByOpType`。

最后，所有算子的注册都长一个样——**清一色 `priority=18`**。下面是仓库里全部 16 处注册中的几条，可以看到无论哪个算子，第二个参数都是 `18`：

> [all_reduce_auto_selector.cc:724](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/selector/all_reduce_auto_selector.cc#L724) —— AllReduce 注册。
> [all_gather_auto_selector.cc:493](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_gather/selector/all_gather_auto_selector.cc#L493) —— AllGather 注册（同为 18）。

这意味着**当前每个算子类型恰好只注册了一个 selector**。那 priority 还有什么用？它是为**可扩展性**预留的：如果将来某个算子类型需要挂多个 selector（例如实验性新算法想插在默认 selector 前面先试），只要给一个不同的优先级即可，`std::map` 会自动按 key 升序排序，小数字优先（见 4.2.3）。

#### 4.1.4 代码实践

**实践目标**：亲手「数一遍」注册表，建立「算子类型 → selector」的全局直觉。

**操作步骤**：

1. 在仓库根目录执行下面的搜索，列出全部 `REGISTER_SELECTOR_BY_OPTYPE` 调用（可用 Grep 工具，或命令行）：

   ```bash
   grep -rn "REGISTER_SELECTOR_BY_OPTYPE(" src/ops | grep -v "selector_registry.h"
   ```

2. 观察每条结果的第二个参数（priority），确认是否**全是 18**。

3. 挑一个不熟悉的算子（如 `barrier`、`scatter`），打开对应 `xxx_auto_selector.cc` 的**文件末尾**，确认它确实以一行 `REGISTER_SELECTOR_BY_OPTYPE(...)` 收尾。

**需要观察的现象**：每个算子的 selector 文件都以注册宏结尾；priority 全部相同；注册宏写在 `namespace ops_hccl {}` 内部、所有函数定义之后。

**预期结果**：你会得到一份「16 个算子 ↔ 16 个 selector ↔ 全部 priority 18」的清单，从而理解：**当前 selector 注册表对每个算子类型是 1:1 的，priority 字段暂未用于同算子多选择器排序，但机制已就位**。

> 说明：以上命令是「源码阅读型实践」，只读不改；若你的环境无法执行 `grep`，可直接用编辑器全局搜索 `REGISTER_SELECTOR_BY_OPTYPE(`。

#### 4.1.5 小练习与答案

**练习 1**：为什么注册宏要用 `__COUNTER__` 拼变量名，而不是直接写一个固定名字的 `static` 变量？

**参考答案**：因为同一个翻译单元（`.cc` 文件）或不同文件里可能有多处注册，若变量名固定会在链接期或编译期冲突（重复定义）。`__COUNTER__` 每次展开递增，保证每个静态变量名唯一，注册互不干扰。

**练习 2**：如果有人误把同一个算子用相同 priority 注册了两次，会发生什么？

**参考答案**：`RegisterByOpType` 检测到 `opTypeImpls_[opType]` 中已存在该 `priority`，打 `HCCL_ERROR` 并返回 `HCCL_E_PARA`，不会覆盖已有项。注册失败通常在静态初始化阶段就被发现。

---

### 4.2 ExecuteSelector::Run 优先级遍历与 MATCH 语义

#### 4.2.1 概念说明

注册表只是「登记」，真正「挑选」发生在 `ExecuteSelector::Run`。它的职责很纯粹：**给定一个算子（`opParam.opType`）和它的拓扑（`topoInfo`），在注册表里找出一个能处理的 selector，让它吐出一个 algName**。

挑选规则是一套**短路（short-circuit）遍历**：

- 把该算子类型的全部 selector 按 **priority 升序**排好。
- 从小到大**依次**调用每个 selector 的 `Select(...)`。
- 一旦某个 selector 返回 `SelectorStatus::MATCH`（且已把 algName 写好），**立即返回成功，不再试后面的**。
- 全都返回 `NOT_MATCH`，则整个选择失败，返回 `HCCL_E_NOT_SUPPORT`。

这里的 **`MATCH` / `NOT_MATCH`** 语义是 selector 子系统的通用语言：`MATCH` 表示「我能处理、我已经选好了算法」，`NOT_MATCH` 表示「我处理不了（拓扑不支持、数据类型不支持、数据量越界等）」。

#### 4.2.2 核心流程

`ExecuteSelector::Run` 的主干（去掉 MC2 特殊路径后）：

```text
selectors = SelectorRegistry::Global()->GetSelectorsByOpType(opParam.opType)   // 取该算子的桶
for (iter in selectors 按 priority 升序):
    status = iter.Select(opParam, topoInfo, selectAlgName)   // 调用基类 AutoSelectorBase::Select
    if status == MATCH:
        日志: "selector[priority=P] matched, algo = <algName>"
        return HCCL_SUCCESS                                   // 命中即返回
return HCCL_E_NOT_SUPPORT                                     // 一个都没命中
```

它在整条链路里的位置如下（`Selector()` 是 op_common 的入口，u3-l1 已介绍）：

```text
Selector(comm, param, topoInfo, algName)            [op_common.cc]
  ├── HcclCalcTopoInfo(...)        // 1. 计算拓扑，填充 topoInfo
  ├── ExecuteSelector::Run(...)    // 2. 算法选择 ← 本模块主角
  │     └── (内部按优先级遍历 selector，命中即止)
  ├── 校验 algName 非空
  ├── SetCommEngine(param)         // 3. 把 opExecuteConfig 映射成 CommEngine
  └── 引擎相关初始化（LoadAICPUKernel / RegisterKernel）
```

#### 4.2.3 源码精读

先看 `ExecuteSelector` 类只有一个核心方法：

> [execute_selector.h:19-24](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/selector/execute_selector.h#L19-L24) —— `ExecuteSelector` 类，对外只有 `Run(OpParam&, TopoInfoWithNetLayerDetails*, std::string& selectAlgName)`。

`Run` 的主体。先看正常路径——按算子类型取桶、升序遍历、命中即返回：

> [execute_selector.cc:41-54](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/selector/execute_selector.cc#L41-L54) —— 正常路径：`GetSelectorsByOpType` 返回的 `std::map<u32, ...>` 天然按 key（priority）升序排列，`for` 循环从小到大调用 `Select`，返回 `MATCH` 即 `HCCL_SUCCESS`；循环走完仍无命中则 `HCCL_E_NOT_SUPPORT`。

> 关键点：`std::map` 的迭代顺序是**按 key 升序**，所以「priority 数字越小越先被试」。这就是 priority 的作用——**优先级数字小 = 优先**。

文件开头还有一段 **MC2 自定义算子** 的特殊分支（自定义算子不走「按算子类型」的常规桶，而是直接去全局表里找 priority 18 的那个 selector 试一次）：

> [execute_selector.cc:25-39](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/selector/execute_selector.cc#L25-L39) —— `opParam.isMc2` 为真时走 `GetAllSelectors()`（即 `impls_`），直接定位 priority 18 的 selector 试一次，命中或失败都立即返回。常规算子不进入此分支。

`Run` 的调用方是 op_common 的 `Selector()` 入口：

> [op_common.cc:101-106](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/op_common.cc#L101-L106) —— `Selector()` 里构造一个 `ExecuteSelector` 并调 `Run`；返回后立即检查 `algName` 是否为空串，空串则视为选择失败。

`Selector()` 在 `Run` 之后紧接着调用 `SetCommEngine`，把 selector 期间可能改写的 `opExecuteConfig` 落定为最终的 `CommEngine`（详见 4.3）：

> [op_common.cc:107-115](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/op_common.cc#L107-L115) —— `SetCommEngine(param)` 后还做了 `AIV_ONLY` 严校验：若用户强制 AIV_ONLY 却没选到 AIV，直接返回不支持。

补充：op_common 还有一个 **`ReSelector()`**（资源回退重选）入口，它**强制把 `opExecuteConfig` 改成 `AICPU_TS`** 再调一次 `Run`，用于「按原算法算资源失败 → 回退到 AICPU 重选」的场景：

> [op_common.cc:572-588](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/op_common.cc#L572-L588) —— `ReSelector()`：`param.opExecuteConfig = AICPU_TS;` 后复用同一个 `ExecuteSelector::Run`。这说明 AICPU 是「兜底引擎」，所有算子最终都能在 AICPU 下选出一个算法。

#### 4.2.4 代码实践

**实践目标**：跟踪一次 `Run`，看清「取桶 → 遍历 → 命中」三步。

**操作步骤**：

1. 打开 [execute_selector.cc](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/selector/execute_selector.cc)，定位 `Run`。
2. 在 `for (auto iter : selectors)` 循环内（[L43-L51](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/selector/execute_selector.cc#L43-L51)）逐行加注释，标注三件事：① `iter.first` 是 priority；② `iter.second->Select(...)` 是真正选择；③ `MATCH` 分支会 `return`。
3. 回答：若 `GetSelectorsByOpType(opType)` 返回**空 map**（该算子没注册任何 selector），循环会发生什么？最终返回什么？

**需要观察的现象 / 预期结果**：

- 空 map 时 `for` 循环一次都不执行，直接走到循环后的 `HCCL_ERROR("No selector is matched.")` 并返回 `HCCL_E_NOT_SUPPORT`。
- 这正是 4.1 里 `GetSelectorsByOpType` 对未注册算子「打 WARNING + 返回空」的后续效果：**没注册的算子，必然选不到算法**。

> 这是「源码阅读型实践」，不运行、只追踪分支。结果可由阅读代码直接得出。

#### 4.2.5 小练习与答案

**练习 1**：假设未来给 AllReduce 增加了一个实验性 selector，注册为 `priority=10`，而原 `AllReduceAutoSelector` 仍是 `priority=18`。运行时谁先被试？什么情况下会用到第二个？

**参考答案**：`std::map` 按 key 升序，`priority=10` 先被试。若它返回 `MATCH`，则 `priority=18` 永远不会被调用；只有当实验性 selector 返回 `NOT_MATCH` 时，才会继续试 `priority=18`。这就是「优先级 + 短路」的组合：高优先级（数字小）的选择器有机会「拦截」默认选择器，失败再回退。

**练习 2**：`SelectorStatus` 有哪两个取值？分别表示什么？

**参考答案**：见 [auto_selector_base.h:35](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/selector/auto_selector_base.h#L35)：`MATCH`（能处理且已选定 algName）、`NOT_MATCH`（当前 selector 无法处理该场景）。

---

### 4.3 AutoSelectorBase::Select 引擎分发与 algName 命名

#### 4.3.1 概念说明

`ExecuteSelector::Run` 调用的 `iter.second->Select(...)`，实际执行的是基类 **`AutoSelectorBase::Select`**。这是一个**模板方法（Template Method）**：

- 基类用一个**非虚**的 `Select()` 固定住「**按引擎分发**」的调度骨架。
- 每个 `Select*Algo` 步骤是 `virtual` 的，由各算子子类（如 `AllReduceAutoSelector`）override 出自己的算法选择逻辑。
- 因此**具体算子不需要、也不能重写 `Select()` 本身**；它只重写「为某个引擎选哪个算法」的钩子。

为什么要按「引擎」分发？因为 u2-l4 已经定好了这次调用要走哪个引擎（存在 `opParam.opExecuteConfig` 里）。selector 要做的就是「**为这个引擎，结合拓扑和数据量，挑一个具体算法**」。同一个 AllReduce：

- 引擎是 AICPU 时，可能选 `AicpuAllReduceSoleNHR`；
- 引擎是 CCU（MS 模式）时，可能选 `CcuMSAllReduceSoleMesh`；
- 引擎是 AIV 时，可能选 `AivAllReduceSoleMeshOneShot`。

algName 的命名也遵循这个「引擎前缀」约定，让你一眼看出它走哪个引擎（见 4.3.3 的映射表）。

#### 4.3.2 核心流程

`AutoSelectorBase::Select` 的分发与**引擎回退链**（这是理解 selector 的关键）：

```text
Select(opParam, topoInfo, algName):                      [auto_selector_base.cc:17]
  configAlgMap = GetExternalInputHcclAlgoConfigAllType() // 读取用户 HCCL_ALGO 等环境变量配置

  ① 若 hostDPUOnly:                                     // 主机 DPU 场景
        opExecuteConfig = HOSTCPU; engine = COMM_ENGINE_CPU
        return SelectDPUAlgo(...)

  ② 若 opExecuteConfig == CCU_MS:                         // 优先试 CCU 的 MS 模式
        ret = SelectCcuMsAlgo(...)
        NOT_MATCH → 把 opExecuteConfig 降级为 CCU_SCHED   // MS 不行就退到 SCHED
        MATCH      → return

  ③ 若 opExecuteConfig == CCU_SCHED:                      // 再试 CCU 的 Sched 模式
        ret = SelectCcuScheduleAlgo(...)
        NOT_MATCH → 把 opExecuteConfig 降级为 CCU_FAIL   // Sched 也不行就标记 CCU 失败
        MATCH      → return

  ④ ProcessAivConfig:                                    // AIV / AIV_ONLY
        若配置是 AIV/AIV_ONLY → SelectAivAlgo(...)
        AIV_ONLY 未命中 → 直接失败;  AIV 未命中 → 降级为 CCU_FAIL

  ⑤ IsStarsState(AICPU_TS / HOSTCPU_TS / CCU_FAIL):       // 最终兜底
        特殊情况下先尝试回退 AIV(IsRollBackAiv)
        否则 SelectAicpuAlgo(...)  → 命中则 opExecuteConfig = AICPU_TS
```

这条链最重要的设计哲学是：**AICPU 是万能兜底**。从 CCU 到 AICPU、从 AIV 到 AICPU，selector 会在当前引擎选不出算法时**自动降级到 AICPU_TS**。这也是 4.2 里 `ReSelector()` 敢于强制 `AICPU_TS` 的底气——任何算子都能在 AICPU 下选出算法。

> 注意 `opExecuteConfig` 与 `engine` 的区别：**selector 在分发过程中可能改写 `opExecuteConfig`**（如 `CCU_MS → CCU_SCHED → CCU_FAIL → AICPU_TS`）；而最终的 `CommEngine engine` 是 `Selector()` 在 `Run` 之后用 `SetCommEngine` 根据「最终」的 `opExecuteConfig` 统一设定的。所以本模块讲的是 `opExecuteConfig` 的分发，不是直接操作 `engine`。

`OpExecuteConfig` 的取值见：

> [alg_param.h:125-136](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/inc/alg_param.h#L125-L136) —— 枚举值 `DEFAULT/HOSTCPU_TS/AICPU_TS/AIV/AIV_ONLY/CCU_MS/CCU_SCHED/.../CCU_FAIL`。

#### 4.3.3 源码精读

先看模板方法 `Select` 本体——它就是 4.3.2 那条链的源码：

> [auto_selector_base.cc:17-68](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/selector/auto_selector_base.cc#L17-L68) —— `AutoSelectorBase::Select`：按 `opExecuteConfig` 依次调用 `SelectDPUAlgo` / `SelectCcuMsAlgo` / `SelectCcuScheduleAlgo` / `SelectAivAlgo` / `SelectAicpuAlgo`，并在每步 `NOT_MATCH` 时降级 `opExecuteConfig`，最终由 `SelectAicpuAlgo` 兜底。

注意它是 `const`、**非虚**（`SelectorStatus Select(...) const;`），子类无法改写分发顺序，只能填各 `Select*Algo` 钩子。基类为这些钩子提供了**默认实现：全部返回 `NOT_MATCH`**，相当于「基类什么都不选」：

> [auto_selector_base.cc:130-178](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/selector/auto_selector_base.cc#L130-L178) —— 基类的 `SelectCcuMsAlgo` / `SelectCcuScheduleAlgo` / `SelectAicpuAlgo` / `SelectAivAlgo` / `SelectDPUAlgo` 默认实现都是 `return SelectorStatus::NOT_MATCH;`。若某算子没 override 某个引擎钩子，该引擎就直接「不匹配」、走降级。

基类还定义了**两张「算子类型 → 默认 algName」的映射表**，是理解 algName 命名的钥匙：

> [auto_selector_base.h:37-62](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/selector/auto_selector_base.h#L37-L62) —— `OP_TYPE_TO_AICPU_SOLE_ALG_MAP`（如 `HCCL_CMD_ALLREDUCE → "AicpuAllReduceSoleNHR"`）与 `OP_TYPE_TO_CCU_1D_ALG_MAP`（如 `HCCL_CMD_ALLREDUCE → "CcuMSAllReduceSoleMesh"`）。

algName 命名约定（拆解后非常规律）：

| algName 片段 | 含义 |
| --- | --- |
| `Aicpu` / `Aiv` / `CcuMS` / `CcuSched` / `Dpu` | **引擎前缀**（AICPU_TS / AIV / CCU 的 MS 模式 / CCU 的 Sched 模式 / DPU） |
| `AllReduce` / `ReduceScatter` / `AllGather` | **算子** |
| `Sole` / `Sequence` / `Parallel` / `PipeLine` / `Concur` | **编排方式**（单发 / 顺序 / 并行 / 流水线 / 并发） |
| `Mesh` / `NHR` / `Ring` | **拓扑算法**（Mesh 全互联 / NHR 非均匀层次环 / Ring 环） |
| `OneShot` / `TwoShot` / `Chunk` | **数据搬运轮数**（一发 / 两发 / 分块） |

例如 `AicpuAllReduceSoleNHR` = AICPU 引擎 + AllReduce 算子 + Sole 单发 + NHR 算法；`CcuMSAllReduceSoleMesh` = CCU 引擎 MS 模式 + AllReduce + Sole + Mesh 算法。

下面看具体算子 `AllReduceAutoSelector` 是怎么 override 这些钩子的。它**没有**重写 `Select`，只重写各 `Select*Algo`：

> [all_reduce_auto_selector.h:18-56](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/selector/all_reduce_auto_selector.h#L18-L56) —— `AllReduceAutoSelector` 声明：override 了 `SelectCcuMsAlgo` / `SelectCcuScheduleAlgo` / `SelectAicpuAlgo` / `SelectAivAlgo` / `SelectDPUAlgo`，并新增了若干私有辅助方法（如 `SelectMeshAlgo`、`SelectMeshAlgoAicpu`）。

**AICPU 钩子**产出 `AicpuAllReduceSoleNHR` 等典型 algName。注意它是按拓扑层级（`topoLevelNums`）和数据量分支的：

> [all_reduce_auto_selector.cc:401-471](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/selector/all_reduce_auto_selector.cc#L401-L471) —— `SelectAicpuAlgo`：多级组网下大量分支会落到 `selectAlgName = "AicpuAllReduceSoleNHR";`（见 L432/L437/L443/L446/L455/L458），单级组网则转给 `SelectMeshAlgoAicpu`。

**CCU MS 钩子**经 `SelectMeshAlgo` 产出 `CcuMSAllReduceSoleMesh` 等：

> [all_reduce_auto_selector.cc:38-75](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/selector/all_reduce_auto_selector.cc#L38-L75) —— `SelectCcuMsAlgo`：先排除保序模式 / int8 / PROD / 64 位数据类型等不支持项，再调 `SelectMeshAlgo`。

> [all_reduce_auto_selector.cc:130-164](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/selector/all_reduce_auto_selector.cc#L130-L164) —— `SelectMeshAlgo` 的 `MESH_1D` 分支：小数据量选 `CcuMSAllReduceSoleMeshOneShot`，大数据量在 960 + 两级网络下选 `CcuAllReduceSoleMeshMsConcur`，其余选 `CcuMSAllReduceSoleMesh`（L149）。

`Select` 里还有一处细节：分发前会读 `configAlgMap = GetExternalInputHcclAlgoConfigAllType()`，即用户通过 `HCCL_ALGO` 等环境变量强制的算法配置（u4-l3 会详讲），作为参数传给各 `Select*Algo`。也就是说**用户的环境变量配置会参与算法选择**，selector 在分支里会参考它。

最后再强调一次引擎回退链在源码里的体现——`CCU_MS` 未命中时直接改写 `opExecuteConfig`：

> [auto_selector_base.cc:29-44](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/selector/auto_selector_base.cc#L29-L44) —— `CCU_MS` 的 `NOT_MATCH` 把 `opExecuteConfig` 降为 `CCU_SCHED`；`CCU_SCHED` 的 `NOT_MATCH` 再降为 `CCU_FAIL`。`CCU_FAIL` 会在第 ⑤ 步被 `IsStarsState` 当作「最终兜底入口」，流向 `SelectAicpuAlgo`。

而 `opExecuteConfig → engine` 的最终落定发生在 `Selector()` 里调用的 `SetCommEngine`：

> [op_common.cc:2984-2996](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/op_common.cc#L2984-L2996) —— `SetCommEngine`：静态映射表把 `AICPU_TS → COMM_ENGINE_AICPU_TS`、`AIV/AIV_ONLY → COMM_ENGINE_AIV`、`CCU_MS/CCU_SCHED → COMM_ENGINE_CCU` 等。selector 期间对 `opExecuteConfig` 的一切降级，最终都通过这张表反映到 `param.engine`。

#### 4.3.4 代码实践

**实践目标**：定位 AllReduceAutoSelector 的注册宏与各引擎钩子，亲手验证「引擎 → algName」的映射。

**操作步骤**：

1. 打开 [all_reduce_auto_selector.cc](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/selector/all_reduce_auto_selector.cc)，跳到**文件末尾** [L724](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/selector/all_reduce_auto_selector.cc#L724)，确认注册语句：`REGISTER_SELECTOR_BY_OPTYPE(HcclCMDType::HCCL_CMD_ALLREDUCE, 18, AllReduceAutoSelector);`。
2. **注意一个反直觉点**：在 `AllReduceAutoSelector` 里**找不到** `Select` 方法的定义——它继承自基类、不可重写。搜索 `::Select(` 只会命中基类 [auto_selector_base.cc:17](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/selector/auto_selector_base.cc#L17)。
3. 找 AICPU 路径：在 `SelectAicpuAlgo`（[L401](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/selector/all_reduce_auto_selector.cc#L401)）里搜索字符串 `"AicpuAllReduceSoleNHR"`，记录所有赋值行号；说明在「多级组网 + Level1Nhr」等条件下它返回这个 algName。
4. 找 CCU MS 路径：从 `SelectCcuMsAlgo`（[L38](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/selector/all_reduce_auto_selector.cc#L38)）→ `SelectMeshAlgo`（[L117](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/selector/all_reduce_auto_selector.cc#L117)），定位 [L149](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/selector/all_reduce_auto_selector.cc#L149) 的 `selectAlgName = "CcuMSAllReduceSoleMesh";`，说明 CCU MS 引擎 + MESH_1D 拓扑 + 中等数据量会返回它。
5. 找 AIV 路径：在 `SelectAivAlgo`（[L584](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/selector/all_reduce_auto_selector.cc#L584)）里定位 `"AivAllReduceSoleMeshOneShot"` 与 `"AivAllReduceSoleMeshTwoShot"`，说明 AIV 引擎按数据量拐点选一发或两发。

**需要观察的现象 / 预期结果**：你能填出下面这张「引擎 → 典型 algName」表，且每条都能在源码里指出对应分支：

| 引擎（opExecuteConfig） | 钩子方法 | 典型 algName | 源码行 |
| --- | --- | --- | --- |
| AICPU_TS | `SelectAicpuAlgo` | `AicpuAllReduceSoleNHR` | L432/L443/L455 等 |
| CCU_MS | `SelectCcuMsAlgo` → `SelectMeshAlgo` | `CcuMSAllReduceSoleMesh` | L149 |
| CCU_SCHED | `SelectCcuScheduleAlgo` | `CcuSchedAllReduceSoleNHR` | L216/L220 |
| AIV / AIV_ONLY | `SelectAivAlgo` | `AivAllReduceSoleMeshOneShot` / `...TwoShot` | L662/L664 |

> 说明：本实践为「源码阅读型」，不修改源码、不运行命令；结果是阅读断言，可在本地用编辑器跳转逐一核对。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `AllReduceAutoSelector` 不重写 `Select()`，而是只重写 `Select*Algo`？

**参考答案**：`Select()` 是模板方法，固化了「按引擎分发 + 失败降级到 AICPU」的调度骨架。这套骨架对所有算子都适用，不应被单个算子改写。各算子真正千差万别的是「某个引擎下该选哪个具体算法」，所以把它抽成 `virtual` 钩子交给子类。这就是模板方法模式：**不变的流程放基类，变化的步骤放子类**。

**练习 2**：一次 AllReduce 调用，`opExecuteConfig` 一开始是 `CCU_MS`，但当前拓扑 / 数据类型 CCU 都不支持。最终 `param.engine` 会是什么？经过哪些中间状态？

**参考答案**：依次 `CCU_MS`（`SelectCcuMsAlgo` 返回 NOT_MATCH）→ 降级为 `CCU_SCHED`（`SelectCcuScheduleAlgo` 返回 NOT_MATCH）→ 降级为 `CCU_FAIL` → 进入第 ⑤ 步被 `IsStarsState` 接住 → `SelectAicpuAlgo` 命中，`opExecuteConfig` 被改写为 `AICPU_TS`。随后 `SetCommEngine` 把 `AICPU_TS` 映射为 `COMM_ENGINE_AICPU_TS`，所以最终 `param.engine == COMM_ENGINE_AICPU_TS`。

**练习 3**：algName `CcuSchedAllReduceSequenceMeshMesh` 里，`CcuSched` 和后两个 `Mesh` 分别传达什么信息？

**参考答案**：`CcuSched` = CCU 引擎的 Scheduler（调度）模式；`Sequence` = 顺序编排；`Mesh`×2 表示这是一个**两级**（layer0 + layer1）都用 Mesh 拓扑的分级算法（节点内 Mesh + 节点间 Mesh）。algName 用字符串编码了「引擎 + 算子 + 编排 + 拓扑层级」的全部信息，这正是它后续能被 executor 注册表直接当键用的原因。

## 5. 综合实践

把本讲三个模块串起来，完成一次**端到端的 selector 追踪**。

**任务**：模拟一次「8 卡单机、FP32、count=1M、SUM」的 AllReduce，推断它会选中哪个 algName，并用源码验证。

**步骤**：

1. **确定入口引擎**。回顾 u2-l4：默认 `OpExpansionMode` 下 `opExecuteConfig` 大概率落到 `CCU_MS` 或 `CCU_SCHED`（取决于机型/版本）。本练习先假设为 `CCU_MS`。
2. **读拓扑**。8 卡单机对应 `topoLevelNums == 1`、`level0Topo == MESH_1D`（节点内 Mesh）。
3. **算数据量**。FP32 = 4 字节，`dataSize = 1M × 4 = 4MB`。对照 [all_reduce_auto_selector.cc:18-37](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/selector/all_reduce_auto_selector.cc#L18-L37) 的阈值常量：`4MB > SMALL_COUNT_512KB`（非小数据），`4MB < AR_AICPU_1D_MAX_DATA_SIZE(32MB)`。
4. **走 CCU MS 分支**。`SelectCcuMsAlgo`（[L38](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/selector/all_reduce_auto_selector.cc#L38)）：FP32 非 int8、非 PROD、非 64 位，通过门禁 → 进 `SelectMeshAlgo`（[L117](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/selector/all_reduce_auto_selector.cc#L117)）。
5. **在 `SelectMeshAlgo` 的 `MESH_1D` 分支里定位**：`dataSize(4MB)` 非小数据、非 960 特例 → 命中 [L149](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/selector/all_reduce_auto_selector.cc#L149) `selectAlgName = "CcuMSAllReduceSoleMesh";`。
6. **回退演练**。把条件改成「`opExecuteConfig = AIV_ONLY` 且数据量 100MB」（超过 AIV 单卡上限 `AIV_MAX_PER_RANK_DATA_SIZE × rankSize`），走 `SelectAivAlgo`（[L584](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/selector/all_reduce_auto_selector.cc#L584)）应返回 NOT_MATCH；因为 `AIV_ONLY` 不允许回退（见 [auto_selector_base.cc:360](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/selector/auto_selector_base.cc#L360) + [op_common.cc:109](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/op_common.cc#L109)），最终这次调用应返回 `HCCL_E_NOT_SUPPORT`。

**预期结果**：

- 8 卡 / FP32 / 4MB / CCU_MS 场景：选中 `CcuMSAllReduceSoleMesh`，最终 `param.engine == COMM_ENGINE_CCU`。
- AIV_ONLY + 超大数据量：选择失败，返回不支持。

> 待本地验证：以上是依据源码分支与阈值的静态推断；真实运行还需考虑机型（910B/950/960）、CANN 版本、`HCCL_ALGO` 等环境变量对 `opExecuteConfig` 初值的影响。建议在有 NPU 的环境用 `HCCL_DEBUG` 级别日志观察 `[Algo][Selector]` 与 `[AllReduceAutoSelector]` 输出，核对推断的 algName。

## 6. 本讲小结

- **注册表是「算子类型 →（优先级 → selector）」的二级 map**。每个算子在自身 `.cc` 末尾用 `REGISTER_SELECTOR_BY_OPTYPE(opType, priority, Selector)` 注册，靠**静态初始化**在程序启动时登记；当前所有算子都用 priority 18，每个算子恰好一个 selector，priority 机制为多选择器扩展预留。
- **`ExecuteSelector::Run` 是短路遍历**：按 priority 升序依次调 `Select`，谁返回 `MATCH`（且填好 algName）就用谁、立即返回；全 `NOT_MATCH` 则整体失败 `HCCL_E_NOT_SUPPORT`。**priority 数字小 = 优先**。
- **`AutoSelectorBase::Select` 是模板方法**：固化「按 `opExecuteConfig` 引擎分发 + 失败降级」的骨架，把 `SelectCcuMsAlgo`/`SelectCcuScheduleAlgo`/`SelectAivAlgo`/`SelectAicpuAlgo`/`SelectDPUAlgo` 留作 `virtual` 钩子；子类只重写钩子，不重写 `Select`。
- **AICPU 是万能兜底引擎**：CCU_MS → CCU_SCHED → CCU_FAIL → AICPU_TS 的降级链保证任何能在 AICPU 下表达的算子最终都能选出算法；`ReSelector()` 也据此敢强制 `AICPU_TS`。
- **algName 命名有规律**：`<引擎前缀><算子><编排方式><拓扑算法><搬运轮数>`，如 `AicpuAllReduceSoleNHR`、`CcuMSAllReduceSoleMesh`、`AivAllReduceSoleMeshOneShot`。它编码了选择结果，也直接作为 executor 注册表的键（u3-l1 的字符串契约）。
- **`opExecuteConfig` 与 `engine` 的关系**：selector 分发与降级改写的是 `opExecuteConfig`；最终的 `CommEngine engine` 由 `Selector()` 在 `Run` 之后调 `SetCommEngine` 统一映射落定。

## 7. 下一步学习建议

selector 已吐出 algName，下一步自然有两个方向：

1. **横向（u3-l3 拓扑适配 Topo）**：本讲反复用到的 `TopoInfoWithNetLayerDetails`（`topoLevelNums`、`level0Topo`、`Level1Nhr`、`netLayerDetails` 等）到底怎么来的、各字段什么含义？`HcclCalcTopoInfo` 如何把 rankGraph 匹配成多级子通信域？去 u3-l3 看 topo 子系统。
2. **纵向（u3-l4 算法执行器 Executor）**：selector 产出的 algName 字符串，如何被 `HcclExecOp` 拿去 `CollAlgExecRegistryV2` 查到对应的 executor，executor 又如何在 `Orchestrate` 里编排 template？这是 algName 契约的「消费端」，去 u3-l4。

建议阅读顺序：先 u3-l3（补齐 selector 的输入侧），再 u3-l4（接上 selector 的输出侧），这样 op_common 的「selector → executor」中段就完整了。若你对 algName 命名里的 `NHR` / `Ring` / `Mesh` 等算法本身的原理感兴趣，可回看 u1-l2 的算法简介。
