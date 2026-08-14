# Tiling 代码生成

## 1. 本讲目标

本讲是 Autofuse 数据流中 **att（Auto Tiling）阶段的收尾篇**。在 [u7-l1](#) 里我们把候选 scheduled graph 建成了一份可求解的性能模型 `ModelInfo`，在 [u7-l2](#) 里把 tiling 选择形式化成了「目标 + 约束 + 决策变量」的优化问题，讲解了 `ArgListReorder` 轴重排如何把 Reduce R 轴与尾轴的切分优先级分成四档、其中 kEqual 等序档交由运行期求解器权衡。这两讲处理的全是**符号表达式**，还没有一行真正可执行的设备/主机代码。

本讲要回答最后一个问题：**这些符号模型如何变成最终编译进 kernel 的 C++ 源码？尤其是 u7-l2 留下的「等序求解」悬念，它的运行期代码到底从哪里来？**

学完后你应当掌握：

1. ATT `generator/` 模块的三层结构（extern C 入口 → `TilingCodeGenerator` 编排 → `TilingCodeGenImpl` 实现），以及 `GenTilingHead / GenTiling / GenTilingTail` 三段式流水线。
2. ATT 最终生成哪些产物（若干 `.h`/`.cpp` 源码片段），它们如何被下游 `codegen` 消费、拼进最终 kernel。
3. 两级 tiling 缓存（operator 级 / group 级）与 reuse group 的代码生成机制。
4. extra info（额外 tiling 字段）与 `TilingData` 结构的生成方式。
5. axes reorder 求解代码的三个层次（原始代码库 → 薄封装 → 按图定制），以及 `enable_equal_order_tiling` 开关「跨 model 聚合」的语义。

## 2. 前置知识

阅读本讲前，请确保理解以下概念（前序讲义已建立）：

- **Tiling（切分）**：把一个大数据块切成小块，决定每次搬多少、算多少、用多少核。参见 [u3-l1](#)。
- **ModelInfo**：ATT 的「总账本」，用符号表达式记录目标函数、硬件约束、决策变量。参见 [u7-l1](#)。
- **求解器在运行期执行**：Autofuse 采用「编译期生成求解器代码、运行期执行搜索」的架构——编译期只生成 C++ 源码，真正的 tiling 数值是在 kernel 运行时、拿到真实 shape 后才求解出来的。参见 [u7-l2](#)。
- **ArgListReorder 四档策略与等序（equal order）**：u7-l2 讲过轴重排把 Reduce R 轴与尾轴的切分优先级分为 kPreferTail/kKeepDefault/kEqual/kFallback；kEqual 档通过 `SetAxesSameOrder` 让多条轴共享同一个 `order` 值，把取舍交给运行期求解器。本讲会看到这份「交接」的接收端。
- **ScheduleGroup / FusedScheduledResult**：optimize 阶段把融合图切成若干调度组，每组带一份 `ModelInfo`。参见 [u6-l3](#)、[u6-l4](#)。
- **CodePrinter**：一个向字符串缓冲逐行追加代码的工具，是 codegen 与 ATT 共用的「代码打印机」。参见 [u5-l3](#)。

一个关键的认知转换：本讲讲的「代码生成」是**用代码生成代码**——ATT 读入符号化的 `ModelInfo`，输出的是一段段 C++ 字符串（存在 `std::map<std::string, std::string>` 里）。这段 C++ 被编进 kernel 后，才在运行时真正执行 tiling 搜索。

## 3. 本讲源码地图

本讲涉及的关键文件都位于 `autofuse/att/` 下：

| 文件 | 作用 |
|------|------|
| `gen_tiling_impl.h` / `gen_tiling_impl.cpp` | ATT 对外的 `extern "C"` 入口（`GenTilingImpl`、`GenTilingImplAutoFuseV3`） |
| `generator/tiling_code_generator.h` / `.cpp` | 编排层 `TilingCodeGenerator`：三个 `GenTilingCode` 重载、Head/Body/Tail 调度 |
| `generator/tiling_code_gen_impl.h` / `.cpp` | 实现层 `TilingCodeGenImpl`：真正逐行打印 C++ 代码的巨型类（约 5000 行） |
| `generator/generator_config.h` | `TilingImplType` 枚举与 `TilingCodeGenConfig` 配置结构 |
| `generator/high_perf_tiling_code_gen_impl.h` | 高性能策略实现（`HIGH_PERF`） |
| `generator/axes_reorder_tiling_code_gen_impl.h` / `.cpp` | 轴重排策略实现（`AXES_REORDER`，Autofuse 默认），持有等序开关判定与 `SolverPassManager` 注入逻辑 |
| `generator/solver_pass/axes_reorder_solver_code.h` + `axes_reorder_solver_code/` 子目录 | 轴重排求解器的**原始代码库**：几十个 `Gen*` 函数各拼接一段 C++ 字符串，组装出完整的 `AxesReorderSolver` 基类 |
| `generator/solver_pass/solver.h` / `solver.cpp` | 求解器代码的薄封装（`GetAxesReorderSolverHead/Func` 转发） |
| `generator/solver_pass_gen/solver_pass_manager.h` / `.cpp` | `SolverPassManager`：基于 `ModelInfo` 按图定制求解器子类与入口函数 |
| `generator/cache/` | 两级缓存代码生成（`tiling_cache_code_gen`、`operator_level_cache_gen`、`group_level_cache_gen`） |
| `generator/extra_info_gen/` | 额外 tiling 字段生成（`extra_info_generator`、`extra_info_config`） |
| `generator/tiling_data_gen/tiling_data_generator.h` | `TilingData` 字段表达式生成（Axes/BlockDim/Memory 三类） |
| `base/att_const_values.h` | 产物 key 与文件名常量（`kTilingHeadIdentify` 等） |
| `codegen/codegen_tiling.cpp` | 下游 codegen 侧消费 ATT 产物的 `TilingLib` |

> 导航提示：`generator/` 子目录的组织原则是「按产物分目录」——`cache/` 生成缓存相关代码，`extra_info_gen/` 生成额外字段，`tiling_data_gen/` 生成 TilingData 字段表达式；而 `solver_pass/` 与 `solver_pass_gen/` 这对目录的分工是：前者提供**与具体图无关**的求解器源码模板，后者针对**每个具体 model_info** 做定制生成。

---

## 4. 核心概念与源码讲解

### 4.1 Tiling 代码生成总流程与产物

#### 4.1.1 概念说明

ATT 的 `generator/` 模块解决一个问题：**把符号化的 `ModelInfo` 翻译成一段可在运行期执行的 C++ tiling 代码**。

承接 [u7-l2](#) 的核心结论——Autofuse「编译期生成求解器、运行期执行搜索」。本讲就是「编译期生成」这一半的落地：generator 不求解任何 tiling 数值，它只负责**打印代码**。打印出来的代码被编进 kernel，等运行时拿到真实输入 shape，才实例化求解器、遍历 tiling case、选出最优解、填好 `TilingData` 结构。

因此 generator 的输出不是数据，而是**源码文本**，具体形态是一个 `std::map<std::string, std::string>`：key 是产物标识（如 `"TilingHead"`），value 是该产物的 C++ 源码字符串。我们把它叫作 `tiling_res`。

generator 内部是清晰的三层：

1. **入口层**（`gen_tiling_impl.cpp`）：`extern "C"` 函数，供 codegen 反向调用（[u3-l2](#) 已确立这个反向调用关系）。
2. **编排层**（`TilingCodeGenerator`）：门面，决定走「单 group」还是「多 group」路径，按 Head/Body/Tail 顺序调度。
3. **实现层**（`TilingCodeGenImpl`）：真正用 `CodePrinter` 逐行打印代码的工作类，所有细节都在这里。

#### 4.1.2 核心流程

整个生成流程可以概括为下图（文字版）：

```
extern "C" GenTilingImplAutoFuseV3(op_name, fused_schedule_result, options, tiling_func)
        │  ① 构造 TilingCodeGenConfig（is_autofuse=true）
        │  ② GetModelInfoMap → FusedParsedScheduleResult
        ▼
TilingCodeGenerator::GenTilingCode(autofuse 重载)
        │  ③ CollectModelInfosAndMetadata：摊平四级结构，收集 score_func/var_relations/...
        │     ├── 单 group → 委派给「model_infos 重载」
        │     └── 多 group → Head → 各 group Body → Tail → FinishGeneratedHeaders
        ▼
TilingCodeGenImpl（由 CreateTilingCodeGenImpl 按 type 选 HIGH_PERF / AXES_REORDER）
        ├── GenTilingHead  ：总 TilingData 框架 + 公共 tiling func 骨架
        ├── GenTiling      ：每个 schedule group 的求解器子类 + 搜索入口（DoTiling）
        └── GenTilingTail  ：schedule group 尾部 + 二次 tiling 全局变量
        ▼
tiling_res（map<产物key, C++源码>）→ 返回给 codegen
```

三个关键设计点：

- **单 group 与多 group 两条路径**：Autofuse 一个融合算子通常只切成一个 schedule group（`is_uniq_group_` 默认 `true`），此时走简化的 `model_infos` 重载；当存在多个 group 时，才走 Head/Body/Tail 全套流程，每个 group 各自生成一份 body，最后合并。
- **两种求解策略**：`TilingImplType` 区分 `HIGH_PERF` 与 `AXES_REORDER`，Autofuse 默认用 `AXES_REORDER`（见 `gen_tiling_impl.cpp` 的 `GetTilingAlgorithm`，找不到时回退到 `AXES_REORDER`）。两者差异通过虚函数（`GenSolverTiling`、`GenDoTiling`、`GenSolverBaseClass` 等）注入同一套框架。
- **产物的「分头文件」设计**：生成的代码不是一整块，而是拆成 5 个「原子头文件」（State/Log/Pgo/Solver/Api）+ 1 个公共头 + tiling func，便于 codegen 按需引用、按需 fallback。

#### 4.1.3 源码精读

**入口层：extern "C" 接口**

ATT 暴露两个 C 接口。Autofuse 场景用的是 `GenTilingImplAutoFuseV3`：

[gen_tiling_impl.h:47-49](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/gen_tiling_impl.h#L47-L49) 声明了 Autofuse 专用的 tiling 生成入口，入参是算子名、`FusedScheduledResult`、选项 map，出参是 `tiling_func`（即 `tiling_res`）。

其实现里最关键的是配置构造与委派：

[gen_tiling_impl.cpp:190-204](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/gen_tiling_impl.cpp#L190-L204)——注意三个要点：`gen_tiling_data = false`（Autofuse 场景下 TilingData 结构由 codegen 另外生成，不由 ATT 生成）、`is_autofuse = true`、`is_inductor_scene` 透传，最后调用 `generator.GenTilingCode(...)` 并校验 `tiling_func` 里一定含 `kTilingHeadIdentify`。

> 为什么 Autofuse 把 `gen_tiling_data` 设为 false？因为 Autofuse 的 TilingData 是跨多个 schedule group 的统一结构 `AutofuseTilingData`，由 codegen 侧的 `TilingData("Autofuse")` 统一拼装；ATT 只负责生成「填充这个结构的 tiling 函数」，不负责结构定义本身。

**编排层：TilingCodeGenerator**

[tiling_code_generator.h:35-76](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/tiling_code_generator.h#L35-L76) 定义了门面类。它对外只暴露动词 `GenTilingCode`，但有三个重载：返回 `model_infos`（落盘）、返回 `model_infos + tiling_res`（不落盘）、Autofuse 专用（`FusedParsedScheduleResult + tiling_res`）。

工厂方法决定走哪种策略：

[tiling_code_generator.cpp:106-120](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/tiling_code_generator.cpp#L106-L120) `CreateTilingCodeGenImpl` 根据 `config.type` 创建 `HighPerfTilingCodeGenImpl` 或 `AxesReorderTilingCodeGenImpl`，这就是策略模式的落点。

Autofuse 重载的主干：

[tiling_code_generator.cpp:181-221](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/tiling_code_generator.cpp#L181-L221) 先用 `CollectModelInfosAndMetadata` 把「asc_graph → schedule_result → schedule_group → impl_graph」四级结构摊平成一份 `all_model_infos`，并顺带收集 `score_funcs`、`var_relations`、`enable_group_parallels`、`workspace_tensor_id_set` 四类元数据；若只有一个 group，直接委派给 `model_infos` 重载；否则走完整的 Head → `GenScheduleGroupTilingBodies` → Tail → `FinishGeneratedHeaders`。

[tiling_code_generator.cpp:301-336](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/tiling_code_generator.cpp#L301-L336) `GenScheduleGroupTilingBodies` 逐 group 创建 impl、`SetScheduleResultGroupNums`、调 `GenTiling`，最后把各 group 的 tiling data 拼到总 `tiling_data_type_name` 上——这就是多 group 场景下「先分后合」的合并点。

**实现层：TilingCodeGenImpl 的三段式**

`TilingCodeGenImpl` 是真正逐行打印代码的类。它持有几个核心输出缓冲（[tiling_code_gen_impl.h:341-345](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/tiling_code_gen_impl.h#L341-L345)）：

- `tiling_data_`：TilingData 结构定义；
- `tiling_func_`：tiling 函数体（.cpp 逻辑）；
- `tiling_head_`：公共 tiling 头；
- `atomic_headers_`：5 个原子头文件的源码（按 `GeneratedHeaderId` 索引）。

三段式的职责划分：

[tiling_code_gen_impl.cpp:3110-3161](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/tiling_code_gen_impl.cpp#L3110-L3161) `GenTilingHead` 负责「总框架」：初始化 `tiling_data_manager_`、生成 TilingData 头（若 `gen_tiling_data`）、生成宏与 include、写出 `namespace optiling` 骨架与公共框架代码（`GenCommonFrameWork`），并把结果刷进 `tiling_res[kTilingHeadIdentify]` 与 solver 头。

[tiling_code_gen_impl.cpp:4956-4989](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/tiling_code_gen_impl.cpp#L4956-L4989) `GenTiling` 负责「单个 group 的主体」：先 `InitTilingGeneration`（重置缓冲、初始化 tiling data manager、生成 group 头），再调 `GenTilingKeyFunc`；若当前 group 是 reuse group（[tiling_code_gen_impl.cpp:4976](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/tiling_code_gen_impl.cpp#L4976) 处判断）则改走 `GenReuseGroupTilingWrapper`（见 4.2）。`GenTilingKeyFunc`（[tiling_code_gen_impl.cpp:4851-4867](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/tiling_code_gen_impl.cpp#L4851-L4867)）是核心：它对每个 `model_info` 调 `GenSolverTiling`（生成求解器子类，4.4 详解）和 `GenTilingCaseImpl`（生成 tiling case 实现），再生成 `GenImplPtr`（tiling_key → impl 指针）、`GenGetTilingKey`、`GenTilingFuncCallEntrance`（搜索入口）。注意这些都是**虚函数**，由 `AxesReorder`/`HighPerf` 子类提供不同实现。

[tiling_code_gen_impl.cpp:4775-4817](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/tiling_code_gen_impl.cpp#L4775-L4817) `GenTilingTail` 负责「收尾」：定义支持二次 tiling 的全局变量 `g_secondary_tiling_ratio`（运行期可调整核数比例）、生成 schedule group 尾部代码，刷进 `tiling_res`。

**多 group 间的转发：GenFillOtherGroupsGetTiling**

多 group 场景下，当前 group 求解成功后还要「顺带」触发其他 group 的 `GetTiling`。[tiling_code_gen_impl.cpp:3932-3968](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/tiling_code_gen_impl.cpp#L3932-L3968) 的 `GenFillOtherGroupsGetTiling` 负责打印这段转发代码：对每个后续 group 生成「设置硬件信息 → 更新 var_relations → 调 `<group>::GetTiling` → 失败则把候选标记为无效并 continue」的语句块。

> 本次更新注意：该函数原先对 `is_inductor_scene` 有一条特殊分支（Inductor 场景遍历「除当前 group 外的所有 group」），现已删除，统一为 `std::next(current_group_iter)` 起的「只向排在当前 group 之后的 group 转发」（[tiling_code_gen_impl.cpp:3962-3964](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/tiling_code_gen_impl.cpp#L3962-L3964)）。语义从「无序全转发」收敛为「按 group 登记顺序的链式转发」，代码更短、行为更确定。

**两种策略：虚函数注入差异**

[axes_reorder_tiling_code_gen_impl.h:17-40](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/axes_reorder_tiling_code_gen_impl.h#L17-L40) 是 Autofuse 默认策略，override 了 `GenSolverBaseClass / GenSolverTiling / GenDoTiling / GenHardwareCons / GenPipeTypeObj / GenGetObj` 等，并持有 `SolverPassManager`（详见 4.4）。

[high_perf_tiling_code_gen_impl.h:18-33](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/high_perf_tiling_code_gen_impl.h#L18-L33) 是另一种策略，override 集合不同。两者共享 `TilingCodeGenImpl` 的全部框架代码，只在「如何生成求解器与目标函数」上分化。

**配置：TilingCodeGenConfig**

[generator_config.h:19-70](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/generator_config.h#L19-L70) 是控制生成行为的总开关：`type`（策略）、`gen_tiling_data` / `gen_extra_infos`（是否生成结构/额外信息）、`enable_autofuse_pgo`（PGO 场景）、`cache_enabled_at_compile_time`（编译态缓存开关，默认关）、`ub_threshold` / `corenum_threshold`（多核 UB 权衡阈值）、`force_tiling_case` / `force_schedule_result`（调试用的强制模板）。`Debug()` 方法把所有字段拼成一行日志，便于 DFX 排查。

**产物 key 与文件名**

ATT 生成的 `tiling_res` 的 key 在 [att_const_values.h:111-127](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/base/att_const_values.h#L111-L127) 集中定义。5 个原子头文件由 `FinishGeneratedHeaders`（[tiling_code_gen_impl.cpp:559-578](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/tiling_code_gen_impl.cpp#L559-L578)）按固定顺序（State → Log → Pgo → Solver → Api）渲染、套上 include guard、写入 `tiling_res`。key 到文件名的映射见下表：

| `tiling_res` 的 key | 落盘文件名 |
|---|---|
| `TilingHead` | `autofuse_tiling_func_common.h` |
| `TilingStateHeader` | `autofuse_tiling_func_state.h` |
| `TilingLogHeader` | `autofuse_tiling_func_log.h` |
| `TilingPgoHeader` | `autofuse_tiling_func_pgo.h` |
| `TilingSolverHeader` | `autofuse_tiling_func_solver.h` |
| `TilingApiHeader` | `autofuse_tiling_func_api.h` |
| `AutofuseTilingData`（结构，非 Autofuse 场景） | `tiling_data.h` |

key 由 [tiling_code_gen_impl.cpp:411-426](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/tiling_code_gen_impl.cpp#L411-L426) `GetAtomicHeaderKey` 从内部枚举 `GeneratedHeaderId` 翻译得到。

**下游消费：codegen 侧的 TilingLib**

[u3-l2](#) 已指出 codegen 通过 `att::GenTilingImplAutoFuseV3` 反向调用 ATT。具体的衔接点在：

[codegen_tiling.cpp:425-460](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/codegen/codegen_tiling.cpp#L425-L460) `TilingLib` 构造函数：当没有外部 tiling 库（`lib_path` 为空）时，把 `this->codegen_func_` 指向 `att::GenTilingImplAutoFuseV3`（[codegen_tiling.cpp:438-439](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/codegen/codegen_tiling.cpp#L438-L439)）；否则用 `dlopen`/`dlsym` 加载外部符号。这是「反向调用」的物理落点——codegen 持有一个函数指针，默认指向 ATT。

[codegen_tiling.cpp:750-804](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/codegen/codegen_tiling.cpp#L750-L804) `GetTilingHeaders` 是真正的消费点：它先写好 include 头与 `#endif`，把前缀塞进 `tiling_file_name_to_content[kTilingHeadIdentify]`，然后调用 `codegen_func_(...)`（即 ATT 的 `GenTilingImplAutoFuseV3`）让 ATT 把 5 个原子头 + 公共头全部填进同一个 map（[codegen_tiling.cpp:784-789](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/codegen/codegen_tiling.cpp#L784-L789)），最后 codegen 再补上 include guard 尾巴、调用 `PopulateFallbackAtomicHeaders` 铺底 fallback 头（[codegen_tiling.cpp:798-799](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/codegen/codegen_tiling.cpp#L798-L799)）；本次更新后，cv（cube-vector）融合场景还会额外调用 `AddCvDeclarationsToApiHeader` 向 Api 头补充声明（[codegen_tiling.cpp:800-802](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/codegen/codegen_tiling.cpp#L800-L802)），cv tiling wrapper 的复用编译细节在 [u8-l2](#) 展开。

> 衔接的本质：ATT 与 codegen 之间的「接口」就是这个 `std::map<std::string, std::string>`（codegen 侧叫 `tiling_file_name_to_content`，ATT 侧叫 `tiling_res` / `tiling_func`）。ATT 填它，codegen 读它并拼装成最终编译单元。

#### 4.1.4 代码实践

**实践目标**：跟踪 `tiling_res` 这个 map 从 ATT 生成到 codegen 消费的全过程，亲手确认「ATT 输出什么、codegen 如何接住」。

**操作步骤**：

1. 打开 [tiling_code_generator.h:35-76](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/tiling_code_generator.h#L35-L76)，找到 Autofuse 专用的 `GenTilingCode` 重载，确认它的最后一个出参类型是 `std::map<std::string, std::string> &tiling_res`。
2. 跟进 [tiling_code_generator.cpp:181-221](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/tiling_code_generator.cpp#L181-L221)，在 `GenTilingHead`、`GenScheduleGroupTilingBodies`、`GenTilingTail`、`FinishGeneratedHeaders` 四处分别找到向 `tiling_res` 写入 key 的语句（提示：搜索 `tiling_res[` 和 `tiling_res[GetAtomicHeaderKey`）。
3. 跳到 [att_const_values.h:111-127](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/base/att_const_values.h#L111-L127)，把每个 key 对应的文件名抄成一张表。
4. 切换到消费侧 [codegen_tiling.cpp:750-804](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/codegen/codegen_tiling.cpp#L750-L804)，确认 codegen 用的 map 变量名是 `tiling_file_name_to_content`，且第 785 行把同一个 map 作为 `codegen_func_` 的第 4 个参数传入。

**需要观察的现象**：ATT 侧写入 map 用的 key 字符串（如 `"TilingHead"`、`"TilingApiHeader"`）与 codegen 侧读取时用的常量是否完全一致——这决定了两端能否对上。

**预期结果**：你会得到一张「ATT 写入点 → key → 文件名 → codegen 读取点」的对应表，证明 `tiling_res` 就是 ATT 与 codegen 之间的契约。运行结果待本地验证（本实践为源码阅读型，无需编译运行）。

#### 4.1.5 小练习与答案

**练习 1**：Autofuse 场景下，ATT 生成的 `tiling_res` 里**不会**包含哪个产物？为什么？

> **答案**：不会包含 `AutofuseTilingData` 的结构定义（即 `tiling_data.h` 内容）。因为 [gen_tiling_impl.cpp:193](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/gen_tiling_impl.cpp#L193) 把 `gen_tiling_data` 设为 `false`，而 `GenTilingHead`/`GenTilingTail`/`FinishGroupTiling` 里所有写 TilingData 结构的语句都被 `if (config_.gen_tiling_data)` 守卫。结构定义由 codegen 侧的 `TilingData("Autofuse")` 另行生成。

**练习 2**：多 group 场景下，`GenFillOtherGroupsGetTiling` 现在向哪些 group 转发 `GetTiling`？本次更新前后有何差异？

> **答案**：现在只向**排在当前 group 之后**的 group 转发（`std::next(current_group_iter)` 起顺序遍历，[tiling_code_gen_impl.cpp:3962-3964](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/tiling_code_gen_impl.cpp#L3962-L3964)）。更新前 Inductor 场景有一条特殊分支，会遍历「除当前 group 外的所有 group」（包括排在前面的）；更新后该分支删除，两种场景统一为按登记顺序的链式转发。

**练习 3**：如果想换一种 tiling 求解策略，需要改哪一处配置？策略之间的差异在代码里以什么机制隔离？

> **答案**：改 `TilingCodeGenConfig.type`（通过 options 的 `solver_type` 或 ini 配置的 `tiling_algorithm`），可选 `AxesReorder` / `HighPerf`。差异通过 `TilingCodeGenImpl` 的虚函数（`GenSolverTiling`、`GenDoTiling`、`GenSolverBaseClass` 等）隔离，两个子类 `AxesReorderTilingCodeGenImpl` / `HighPerfTilingCodeGenImpl` 各自 override，框架代码完全共享。

---

### 4.2 缓存处理（cache）

#### 4.2.1 概念说明

承接 4.1：ATT 生成的 tiling 代码会在**运行期**对每个输入 shape 执行一次求解搜索。如果一个融合算子被反复调用、且输入 shape 重复出现（训练场景的典型情况），每次都重新求解就太浪费了。

`generator/cache/` 模块生成的代码就是为了解决这个问题：它在生成的 tiling 函数里**织入一层运行期缓存**——用输入 shape 作为 key，命中就直接返回已算好的 `TilingData`，未命中才走求解器，算完再存回去。

Autofuse 设计了**两级缓存**：

- **operator 级缓存**（`OperatorLevelCacheGen`）：面向整个融合算子，用所有输入 shape 拼成 key，`thread_local` 的固定大小哈希表，带 LRU 老化。由 `config.cache_enabled_at_compile_time` 开关控制（默认关闭）。
- **group 级缓存**（`GroupLevelCacheGen`）：面向 schedule group。当多个 group 的 tiling 逻辑同构时，一个 group 可以**复用**另一个 group 已算好的结果，不必各自求解——这就是「reuse group」机制。

> 注意：这两级缓存生成的都是**运行期数据结构**（哈希表、context 类），由 ATT 在编译期把它们的定义和查询/保存调用打印进 tiling 代码。

#### 4.2.2 核心流程

缓存代码的生成流程：

```
① 收集 reuse 关系：GetCacheReuseInfo 遍历 schedule_result
       找出「当前 group 复用某个源 group」的映射 → cache_reuse_info[cur_prefix] = reuse_prefix
② 确定缓存容量：cache_capacity = all_model_infos.size() * 2
③ InitTilingGeneration 记录 cache_reuse_info / cache_capacity / with_reuse_info
④ 生成阶段：
   ├── OperatorLevelCacheGen：FixedSizeHashMap 模板类 + TilingCacheContext + Find/Save（带 LRU）
   ├── GroupLevelCacheGen：GroupLevelCache 类型 + group 间查询函数
   └── GenCacheInit：在每个 reuse group 入口声明 GroupLevelCache 实例
⑤ 运行期：生成的 GetTiling 先查缓存，未命中才 DoTiling，算完 SaveCache
```

reuse group 的关键在于：一个标记为 reuse 的 group 不生成自己的求解器，而是生成一段「把自己的 `TilingData` cast 成源 group 的 `TilingData`、再调用源 group 的 `GetTiling`」的转发代码（见 [tiling_code_gen_impl.cpp:4991-5023](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/tiling_code_gen_impl.cpp#L4991-L5023) 的 `GenReuseGroupTilingWrapperGetTiling`）。这把「N 个同构 group 各算一次」压缩成「算 1 次、转发 N-1 次」。

#### 4.2.3 源码精读

**缓存代码生成器基类**

[tiling_cache_code_gen.h:27-106](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/cache/tiling_cache_code_gen.h#L27-L106) 定义了基类 `TilingCacheCodeGen`，核心职责是生成一个 `FixedSizeHashMap` 模板类——它把一组静态方法（`GenHashMapTemplate`、`GenFindMethod`、`GenInsertMethod`、`GenEraseMethod`、`GenHashFunction` 等）拼装成一个固定容量、开放寻址的哈希表，提供 `Find/Insert/Erase/Clear/Size`。这是两级缓存共用的底层数据结构。

**operator 级缓存**

[operator_level_cache_gen.h:25-119](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/cache/operator_level_cache_gen.h#L25-L119) 派生类负责生成 `TilingCacheContext` 类、`OperatorLevelCache` 类型、以及 `FindOperatorCache`/`SaveOperatorCache` 两个运行期函数。其中 `GenInitAndQueryCacheCode` 与 `GenSaveCacheCalls` 负责在生成的 tiling 函数体里插入「查询」与「保存」调用。

具体生成的运行期代码长这样（节选）：

[operator_level_cache_gen.cpp:239-254](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/cache/operator_level_cache_gen.cpp#L239-L254) `GenContextClassStructure` 生成 context 类的私有成员：一个 `thread_local` 的 `OperatorLevelCache<TilingData>` 指针、访问计数数组 `access_counts_`（用于 LRU 老化）。

[operator_level_cache_gen.cpp:290-308](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/cache/operator_level_cache_gen.cpp#L290-L308) `GenFindOperatorCacheImpl` 生成查询函数：用 shape_key 在哈希表里 `Find`，命中则累加访问计数。

[operator_level_cache_gen.cpp:311-341](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/cache/operator_level_cache_gen.cpp#L311-L341) `GenSaveOperatorCacheImpl` 生成保存函数：先尝试 `Insert`，缓存满到阈值（`kOperatorCacheCapacity * kLoadFactorThreshold`）就执行 LRU 老化——扫描 `access_counts_` 找最小值、`Clear` 后重新插入。哈希函数用黄金比例常数 `0x9e3779b9` 做混合（[operator_level_cache_gen.cpp:370-374](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/cache/operator_level_cache_gen.cpp#L370-L374)）。

**group 级缓存**

[group_level_cache_gen.h:24-52](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/cache/group_level_cache_gen.h#L24-L52) 负责生成 `GroupLevelCache` 类型与 group 间缓存函数，复用基类的 `FixedSizeHashMap`，但 key 维度是单个 group 内的 shape。

**reuse 关系收集与入口声明**

[tiling_code_generator.cpp:122-144](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/tiling_code_generator.cpp#L122-L144) `GetCacheReuseInfo` 遍历 `FusedParsedScheduleResult`，凡是被标记为 reuse 的 group，就记下 `cache_reuse_info[cur_prefix] = reuse_prefix`。缓存容量在 [tiling_code_generator.cpp:208](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/tiling_code_generator.cpp#L208) 定为 `all_model_infos.size() * 2`。

[tiling_code_gen_impl.cpp:3227-3237](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/tiling_code_gen_impl.cpp#L3227-L3237) `GenCacheInit` 在生成的函数体里为每个 reuse 源 group 声明一个 `GroupLevelCache` 实例（用 `declared_cache_types_` 去重，避免重复声明）。

**织入点：GenCacheHashMapDef**

[tiling_code_gen_impl.cpp:961-1000](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/tiling_code_gen_impl.cpp#L961-L1000) `GenCacheHashMapDef` 是缓存代码的总织入点：若 operator 缓存与 group reuse 都未启用则直接返回；否则向 State 原子头 require `<array>` 等系统头、生成共享的常量定义与 `FixedSizeHashMap` 模板，且只在 `cache_enabled_at_compile_time` 为真时才生成 `TilingCacheContext` 等 operator 级类型。`AxesReorderTilingCodeGenImpl::GenToolFuncs` 也会调用它（[axes_reorder_tiling_code_gen_impl.cpp:148](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/axes_reorder_tiling_code_gen_impl.cpp#L148)），保证默认策略下缓存定义可用。

#### 4.2.4 代码实践

**实践目标**：理解 reuse group 如何把「多次求解」变成「一次求解 + 多次转发」。

**操作步骤**：

1. 阅读 [tiling_code_generator.cpp:122-144](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/tiling_code_generator.cpp#L122-L144)，确认 `cache_reuse_info` 的 key 和 value 各是什么（提示：都是 group prefix 字符串）。
2. 打开 [tiling_code_gen_impl.cpp:4956-4989](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/tiling_code_gen_impl.cpp#L4956-L4989)，看 `GenTiling` 如何判断当前 group 是 reuse group（`IsReuseGroup`），若是则走 `GenReuseGroupTilingWrapper` 而非正常求解路径（分支点在 [tiling_code_gen_impl.cpp:4976](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/tiling_code_gen_impl.cpp#L4976)）。
3. 跟进 [tiling_code_gen_impl.cpp:4991-5023](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/tiling_code_gen_impl.cpp#L4991-L5023)，找到转发到源 group 的那句 `auto ret = <reuse_prefix>::GetTiling(...)`。

**需要观察的现象**：reuse group 生成的 `GetTiling` 函数体里，是否完全不含求解器调用，只剩 `RefToRef` 类型转换 + 调用源 group。

**预期结果**：reuse group 的 `GetTiling` 只做 `TilingData` 的引用转换并委托给源 group，自身不生成 solver。这解释了为什么同构 group 越多，reuse 带来的编译产物体积与运行期求解节省越大。运行结果待本地验证（源码阅读型实践）。

#### 4.2.5 小练习与答案

**练习 1**：operator 级缓存默认是开还是关？由哪个字段控制？

> **答案**：默认关。由 `TilingCodeGenConfig.cache_enabled_at_compile_time` 控制（[generator_config.h:47](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/generator_config.h#L47)，默认 `false`）。开启后才会在生成的代码里织入 `TilingCacheContext` 与 `Find/Save` 调用（判定与织入逻辑见 [tiling_code_gen_impl.cpp:961-1000](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/tiling_code_gen_impl.cpp#L961-L1000) 的 `GenCacheHashMapDef`）。

**练习 2**：缓存满了之后，生成的代码用什么策略淘汰旧条目？

> **答案**：LRU 老化。当 `cache.Size() >= kOperatorCacheCapacity * kLoadFactorThreshold` 时，扫描 `access_counts_` 找最小访问计数，`Clear` 整个缓存后重新插入新条目（[operator_level_cache_gen.cpp:326-341](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/cache/operator_level_cache_gen.cpp#L326-L341)）。

---

### 4.3 额外信息与 TilingData 生成（extra info）

#### 4.3.1 概念说明

`TilingData` 是运行期 tiling 函数的**输出结构**：求解器把选定的切分参数（每条轴切多大、用多少核、各 buffer 多大）写进它，kernel 再读它来执行。

`TilingData` 里的字段分两类：

- **基本 tiling 参数**：由求解器直接求出的决策变量（tile 大小等）。
- **额外信息（extra info）**：由基本参数**派生**出来的辅助字段，例如每条轴的循环次数 `loop_num`、尾块大小 `tail_size`、对齐后的轴大小、高阶 api 的 tiling、外轴/尾轴逻辑等。这些字段不必进求解器，只需在基本参数定下来后用固定公式算一遍。

`generator/extra_info_gen/` 与 `generator/tiling_data_gen/` 两个子目录共同负责生成这些字段的**定义与赋值表达式**。注意：在 Autofuse 场景下（`gen_tiling_data=false`），ATT 只生成「字段表达式」，结构定义由 codegen 拼；但表达式计算的来源仍是这里。

#### 4.3.2 核心流程

extra info 的生成围绕一个中心对象 `TilingDataGenerator`（即 `TilingCodeGenImpl` 的成员 `tiling_data_manager_`）：

```
TilingDataGenerator（按 tiling_key 缓存多组生成器）
   ├── AxesTilingDataGen     ：轴相关（loop_num = Ceil(轴大小 / tile)，tail_size = 轴大小 % tile，对齐大小）
   ├── BlockDimTilingDataGen ：核数相关（used_core_num）
   └── MemoryTilingDataGen   ：内存相关（buffer 大小）
        ▼
ExtraInfoGenerator 汇总
   ├── GetExtraTilingDataDef ：拼出 CoreParams 等结构定义
   └── GetExtraTilingVars    ：列出某 tiling_key 用到的字段名
```

字段值全部用符号表达式表示（承接 [u7-l1](#) 的 `af::Expr`），例如轴的循环次数：

\[ \text{loop\_num} = \lceil \text{axis\_size} / \text{tile\_size} \rceil,\quad \text{tail\_size} = \text{axis\_size} \bmod \text{tile\_size} \]

这些表达式最终被打印成运行期 `TilingData` 的 `set_xxx(...)` 调用。

`ExtraInfoConfig`（[extra_info_config.h:15-19](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/extra_info_gen/extra_info_config.h#L15-L19)）用两个开关控制：`do_api_tiling`（是否生成高阶 api tiling）、`do_axes_calc`（是否生成外轴/尾轴逻辑）。

#### 4.3.3 源码精读

**TilingDataGenerator：三类字段生成器**

[tiling_data_generator.h:21-27](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/tiling_data_gen/tiling_data_generator.h#L21-L27) 定义字段类别枚举 `TilingDataGenType`：`AXES_TILING_DATA_GEN`（轴）、`GENERAL_TILING_DATA_GEN`（核数）、`MEMORY_TILING_DATA_GEN`（内存）、`ALL_TILING_DATA_GEN`（全量）。

三个具体生成器（[tiling_data_generator.h:56-126](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/tiling_data_gen/tiling_data_generator.h#L56-L126)）：

- `AxesTilingDataGen`：核心方法 `AddAxesAlignedSize`（轴对齐）、`AddAxesTailSizeAndLoopNum`（尾块与循环次数）、`AddSplitOuterAxisTailArgs`（外轴尾块），内部用 `axes_tiling_data_map_` 按 axis_name 存「loop_num / tail_size」对（见文件内注释 `key: axis_name, value: [AxisTilingData(...)]`）。
- `BlockDimTilingDataGen`：依赖 `AxesTilingDataGen`，`AddUsedCoreNum` 生成实际使用核数。
- `MemoryTilingDataGen`：把 `var_name → Expr` 的内存参数翻成函数实现与调用。

管理类 `TilingDataGenerator`（[tiling_data_generator.h:129-156](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/tiling_data_gen/tiling_data_generator.h#L129-L156)）按 `tiling_key` 缓存每组生成器，对外提供 `GetTilingDataWithAnnotation`（取字段定义）和 `GetTilingFuncImpl`/`GetTilingFuncInvoke`（取赋值函数实现与调用）。

**ExtraInfoGenerator：汇总输出**

[extra_info_generator.h:22-49](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/extra_info_gen/extra_info_generator.h#L22-L49) 持有 `ExtraInfoConfig`、`model_info_list_` 和一个 `TilingDataGenerator` 引用，对外暴露 `GetExtraTilingDataDef`（拼结构定义）和 `GetExtraTilingVars`（列字段名）。

[extra_info_generator.cpp:23-51](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/extra_info_gen/extra_info_generator.cpp#L23-L51) `WriteCoreParamData` 是核心：对每个 `model_info`，用 `tiling_data_generator_.GetTilingDataWithAnnotation(tiling_case_id, AXES_TILING_DATA_GEN)` 取出轴字段，借助 `TilingDataGenUtils::NeedWrittenTilingData` 去重后逐行打印；`GetExtraTilingDataDef` 把结果累加到 `type_name_to_definition["CoreParams"]`。

> 关键协作：`ExtraInfoGenerator` 不自己算字段，它只是「问 `TilingDataGenerator` 要字段表达式、再排版成结构定义」。真正的字段表达式来源是 `AxesTilingDataGen` 等三类生成器，而它们的输入是 `ModelInfo`（承接 [u7-l1](#)）。

#### 4.3.4 代码实践

**实践目标**：确认 extra info 的字段值是「派生公式」而非「求解器决策变量」。

**操作步骤**：

1. 打开 [tiling_data_generator.h:56-94](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/tiling_data_gen/tiling_data_generator.h#L56-L94)，定位 `AxesTilingDataGen` 的私有方法 `AddAxesTailSizeAndLoopNum`。
2. 阅读文件头部注释（第 89-90 行附近）里的示例：`AxisTilingData(AXIS_LOOP_NUM, s0t_loop_num, Ceil(s0T / s0t), ...)`，理解 `loop_num` 是怎么由轴大小 `s0T` 和 tile 大小 `s0t` 算出来的。
3. 对比 [extra_info_generator.cpp:40-51](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/extra_info_gen/extra_info_generator.cpp#L40-L51)，确认 `GetExtraTilingDataDef` 只调用了 `AXES_TILING_DATA_GEN` 这一类生成器来拼 `CoreParams`。

**需要观察的现象**：`loop_num`、`tail_size` 这类字段的表达式里是否含有求解器决策变量（如 `s0t`），以及它们是否由 `Ceil`、`%` 等确定性公式给出。

**预期结果**：extra info 字段都是基本 tiling 参数（如 `s0t`）经 `Ceil`/取模/对齐等确定性公式派生而来，不参与求解搜索。这解释了为何它们可以放在求解之外单独生成。运行结果待本地验证（源码阅读型实践）。

#### 4.3.5 小练习与答案

**练习 1**：`ExtraInfoGenerator` 与 `TilingDataGenerator` 是什么关系？谁真正产生字段表达式？

> **答案**：`ExtraInfoGenerator` 是「排版者」，`TilingDataGenerator` 是「字段来源」。`ExtraInfoGenerator` 持有 `TilingDataGenerator` 的引用（[extra_info_generator.h:24-26](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/extra_info_gen/extra_info_generator.h#L24-L26)），通过它取字段表达式再排版成 `CoreParams` 等结构定义；真正的表达式由 `TilingDataGenerator` 内部的 `AxesTilingDataGen` 等生成器依据 `ModelInfo` 计算。

**练习 2**：为什么 `loop_num`、`tail_size` 不放进求解器，而是作为 extra info 单独生成？

> **答案**：因为它们是基本 tile 参数的**确定性派生量**（`loop_num = Ceil(轴/tile)`、`tail_size = 轴 % tile`），没有自由度，求解器选出 tile 后它们就唯一确定了。放进求解器只会无谓增加决策变量维度，所以放在求解之后用公式一次性算出。

---

### 4.4 axes reorder 求解代码（solver_pass 与 solver_pass_gen）

#### 4.4.1 概念说明

[u7-l2](#) 留了一个悬念：`ArgListReorder` 把 Reduce R 轴与尾轴的切分优先级分档后，**kEqual 等序档**通过 `SetAxesSameOrder` 让多条轴共享同一个 `order` 值，把「先切谁」的取舍交给**运行期求解器**权衡。本模块就讲这个运行期求解器的代码从哪里来——它不是手写的库，而是 ATT 在编译期**打印出来的一整套 C++ 源码**。

这套求解代码分三个层次：

1. **原始代码库**（`solver_pass/axes_reorder_solver_code.h` + `axes_reorder_solver_code/` 子目录）：几十个 `Gen*` 函数，每个函数拼接一段 C++ 字符串（求解器类定义、约束结构、二分搜索、三阶段算法框架、等序求解、多核编排……），最终由 `GetAxesSolverSolverHead` / `GetAxesSolverSolverFunc` 组装成完整的 `AxesReorderSolver` 基类。这一层**与具体图无关**——不管什么样的融合子图，基类代码都一样。
2. **薄封装**（`solver_pass/solver.h` / `solver.cpp`）：`GetAxesReorderSolverHead/Func` 直接转发到上一层，为上层调用提供统一命名。
3. **按图定制**（`solver_pass_gen/solver_pass_manager.h` / `.cpp` 的 `SolverPassManager`）：基于 `ArgsManager` + `ModelInfo`，为**每个具体 model_info** 生成求解器子类（`GenAxesReorderClass`，产出形如 `AxesReorderSolvercase<tag><tiling_case_id>` 的类）与求解入口（`GenAxesReorderFunc`）。

注入点是 4.1 提到的 `AxesReorderTilingCodeGenImpl` 三个虚函数：`GenSolverBaseClass`（每图一次，铺基类）、`GenSolverTiling`（每 model_info 一次，生成子类）、`GenDoTiling`（每 model_info 一次，生成入口）。

#### 4.4.2 核心流程

```
GenSolverBaseClass（每图一次）
    │  is_enable_equal_order = IsAnyModelEnableEqualOrderTiling(tiling_model_info_)   ← 跨 model 聚合开关
    │  SolverPassManager::GenAxesReorderBaseClassesHead/Func(is_enable_equal_order)
    │      → 写入 kSolver 原子头 + tiling_func_
    └─ 若 enable_autofuse_pgo || is_inductor_scene
         → 追加 GenAxesReorderPgoClassesHead/Func（AxesReorderPgoSolver 子类）

GenSolverTiling（每 model_info 一次）
    │  构造 SolverPassManager(args_manager, {tiling_case_id, sub_case_tag}, tiling_data_type_name)
    │  ConfigureSolverPassManagerCommon：SetUBThreshold / SetCoreNumThreshold / SetEnableMulticoreUBTradeoff /
    │      SetEnableAutofusePGO / SetVariableReplace / SetHighPerfTiling ...
    │  GetGroupNumAndSetToSolver：把同一 ScheduleResult 的 group 数喂给求解器
    └─ GenAxesReorderClass() → 生成 AxesReorderSolvercase<tag><id> 子类源码

GenDoTiling（每 model_info 一次）
    │  SetEnableEqualOrder(IsEnableEqualOrderTiling(model_info))   ← 单 model 开关
    │  SetIsInductorScene / SetIsUniGroup / SetTilingDataSubGroupItemName
    └─ GenAxesReorderFunc(arrange_code_) → 生成 DoTiling 求解入口
```

**等序开关（Reduce balance）的判定**：`IsEnableEqualOrderTiling` 扫描单个 `ModelInfo` 的 `arg_list`，把可切分轴按 `order` 值分桶；若同一个 `order` 值下挂了 ≥2 条可切分轴（`kMaxEqualOrderAxesCount = 2`），说明 u7-l2 的 `ArgListReorder` 判出了 kEqual 档——这两条轴的切分顺序需要运行期权衡，于是打开等序求解。

**「跨 model 聚合」的语义（本次更新重点）**：基类代码是整张图**共享一份**的，只要任何一个 model 启用了等序，基类就必须带上等序求解逻辑，所以 `GenSolverBaseClass` 用的是 `IsAnyModelEnableEqualOrderTiling`（任一 model 为真即为真）；而每个求解入口（`GenDoTiling`）再用单 model 粒度的 `IsEnableEqualOrderTiling` 精确控制自己是否走等序路径。本次更新在 [axes_reorder_solver_code.h:196](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/solver_pass/axes_reorder_solver_code.h#L196) 新增的注释 `// The Reduce balance switch is aggregated across all models that share this solver definition.` 正是把这条隐式契约写进了代码——`enable_equal_order_tiling` 形参不是「某个模型的开关」，而是「共享这份求解器定义的所有模型的聚合开关」。

此外，该头文件声明的三个全局常量 `AXES_SOLVER_CODE_HEAD / AXES_SOLVER_CODE_FUNC / AXES_SOLVER_PGO_CODE_FUNC` 在启动期就以 `enable_equal_order_tiling=true` 预生成好完整代码串（[axes_reorder_solver_main.cpp:313-315](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/solver_pass/axes_reorder_solver_code/axes_reorder_solver_main.cpp#L313-L315)），供需要「最全版本」求解代码的调用方直接取用。

#### 4.4.3 源码精读

**第一层：原始代码库的组装**

[axes_reorder_solver_code.h:186-205](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/solver_pass/axes_reorder_solver_code.h#L186-L205) 声明了主入口（`GenAxesReorderRun`、`GetAxesSolverSolverHead/Func`、PGO 变体）与全局常量。头文件按 Section 组织（Section 8.2 二分搜索、Section 8.3 三阶段算法框架、Section 8.4 NaiveTiling……），每个 `Gen*` 对应一段可独立拼接的 C++ 代码。第 196 行是本次更新新增的聚合语义注释。

[axes_reorder_solver_main.cpp:180-190](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/solver_pass/axes_reorder_solver_code/axes_reorder_solver_main.cpp#L180-L190) `GetAxesSolverSolverHead` 展示了基类头部的组装方式：依次拼接约束类型、结构定义、变量、约束、tiling 变量、求解器输入结构、`AxesReorderSolver` 类（等序开关作为形参传入，控制类内是否生成等序相关的成员与方法）。

[axes_reorder_solver_main.cpp:305-315](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/solver_pass/axes_reorder_solver_code/axes_reorder_solver_main.cpp#L305-L315) 函数体侧的组装与静态常量初始化：`GenCoreSolverFunctions` 把初始化变量、满足约束、二分搜索、等序求解、局部 buffer tiling、主求解函数按序拼接；`AXES_SOLVER_CODE_HEAD/FUNC` 等常量在静态初始化期以「全开」参数生成。

**第二层：薄封装**

[solver.h:17-22](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/solver_pass/solver.h#L17-L22) 与 [solver.cpp:26-35](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/solver_pass/solver.cpp#L26-L35) 提供统一命名的一组转发函数（`GetSolverHead/GetSolverFunc` 按 `SolverType` 分发，AxesReorder 系列一对一转发到 `GetAxesSolverSolverHead/Func`）。

**第三层：SolverPassManager 按图定制**

[solver_pass_manager.h:47-52](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/solver_pass_gen/solver_pass_manager.h#L47-L52) 暴露的四个关键方法：静态的 `GenAxesReorderBaseClassesHead/Func`（铺与图无关的基类）、实例的 `GenAxesReorderClass`（按 model_info 生成子类）与 `GenAxesReorderFunc`（生成求解入口）。实例方法依赖构造时传入的 `ArgsManager`（承载 arg_list 与约束）与 `{tiling_case_id, sub_case_tag}` 标识。

**注入点：AxesReorderTilingCodeGenImpl 的三个虚函数**

[axes_reorder_tiling_code_gen_impl.cpp:23-55](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/axes_reorder_tiling_code_gen_impl.cpp#L23-L55) 是等序判定的全部逻辑：`IsEnableEqualOrderTiling` 按 `order` 值给可切分轴分桶（桶内超过 `kMaxEqualOrderAxesCount=2` 条会告警），≥2 条即启用；`IsAnyModelEnableEqualOrderTiling` 遍历所有 model 做 OR 聚合。

[axes_reorder_tiling_code_gen_impl.cpp:82-103](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/axes_reorder_tiling_code_gen_impl.cpp#L82-L103) `GenSolverBaseClass`：先为 kSolver 原子头 require 系统头，等序开启时额外 require `<limits>`、`<map>`；用聚合开关生成基类头（进原子头）与基类函数体（进 `tiling_func_`）；PGO 或 Inductor 场景再追加 `AxesReorderPgoSolver` 子类。

[axes_reorder_tiling_code_gen_impl.cpp:105-120](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/axes_reorder_tiling_code_gen_impl.cpp#L105-L120) `GenSolverTiling`：构造 `ArgsManager` 与 `SolverPassManager`，`ConfigureSolverPassManagerCommon` 把 `TilingCodeGenConfig` 里的阈值类配置（UB 阈值、核数阈值、多核 UB 权衡、PGO、变量替换、高精度）灌进去，`GetGroupNumAndSetToSolver` 喂入 group 数，最后 `GenAxesReorderClass` 的产物追加进 `tiling_func_`。

[axes_reorder_tiling_code_gen_impl.cpp:122-138](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/axes_reorder_tiling_code_gen_impl.cpp#L122-L138) `GenDoTiling`：与 `GenSolverTiling` 同构地构造 manager，但改用**单 model 粒度**的 `SetEnableEqualOrder(IsEnableEqualOrderTiling(model_info))`，并 `SetIsInductorScene`、`SetIsUniGroup`，最后以 `GenAxesReorderFunc(arrange_code_)` 生成入口、走 `GenDoTilingCommon` 收尾。

> 与 u7-l2 的闭环：`ArgListReorder`（编译期）判出 kEqual → 给共享 order 的轴写入相同 `order` 值 → `ModelInfo.arg_list` 里出现「同 order 多轴」→ 本模块 `IsEnableEqualOrderTiling`（编译期）识别 → 生成的 `AxesReorderSolver` 带上等序求解逻辑 → 运行期在两条轴的切分组合里搜索最优。Reduce R 轴与尾轴的 tiling 平衡，就是这样「编译期识别 + 运行期权衡」接力完成的。

#### 4.4.4 代码实践

**实践目标**：完整跟踪 `enable_equal_order_tiling` 开关的三跳——从 `arg_list` 的 order 值，到基类生成的聚合开关，再到单 model 求解入口的独立开关。

**操作步骤**：

1. 从判定端开始：阅读 [axes_reorder_tiling_code_gen_impl.cpp:23-55](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/axes_reorder_tiling_code_gen_impl.cpp#L23-L55)，写出 `IsEnableEqualOrderTiling` 与 `IsAnyModelEnableEqualOrderTiling` 的返回条件各是什么（提示：关注 `AttUtils::IsTileSplitAxis(arg)`、`arg->order` 分桶、`kMaxEqualOrderAxesCount`）。
2. 跳到聚合点：在 [axes_reorder_tiling_code_gen_impl.cpp:87-95](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/axes_reorder_tiling_code_gen_impl.cpp#L87-L95) 确认基类用的是哪个函数、结果传给了 `GenAxesReorderBaseClassesHead/Func` 的哪个形参。
3. 再看单 model 点：在 [axes_reorder_tiling_code_gen_impl.cpp:131](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/axes_reorder_tiling_code_gen_impl.cpp#L131) 确认 `SetEnableEqualOrder` 用的是哪个函数，与步骤 2 的差异在哪。
4. 最后到代码库层：打开 [axes_reorder_solver_code.h:186-205](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/solver_pass/axes_reorder_solver_code.h#L186-L205)，读第 196 行新增的注释，再到 [axes_reorder_solver_main.cpp:180-190](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/solver_pass/axes_reorder_solver_code/axes_reorder_solver_main.cpp#L180-L190) 看 `enable_equal_order_tiling` 如何改变 `GenAxesReorderSolver` 生成的类内容。

**需要观察的现象**：同一个开关在三个层次出现了三种形态——`IsEnableEqualOrderTiling(model)`（单 model 布尔）、`IsAnyModelEnableEqualOrderTiling(models)`（聚合布尔）、`enable_equal_order_tiling` 形参（穿透到原始代码库的生成参数）。

**预期结果**：你能画出「kEqual 判定（u7-l2）→ arg_list 同 order 多轴 → IsEnableEqualOrderTiling → IsAnyModelEnableEqualOrderTiling → GenAxesReorderBaseClasses(聚合开关) + SetEnableEqualOrder(单 model 开关)」的完整链路，并解释为什么基类必须用聚合开关（基类全图共享一份，缺了等序逻辑会导致启用等序的 model 在运行期找不到求解方法）。运行结果待本地验证（源码阅读型实践）。

#### 4.4.5 小练习与答案

**练习 1**：`solver_pass/` 与 `solver_pass_gen/` 这对目录的分工是什么？为什么要把它们分开？

> **答案**：`solver_pass/`（含 `axes_reorder_solver_code/` 子目录）提供**与具体图无关**的求解器源码模板——`AxesReorderSolver` 基类、二分搜索、三阶段算法等，任何图生成的这部分代码都相同；`solver_pass_gen/` 的 `SolverPassManager` 负责把 `ModelInfo`/`ArgsManager` 里每张图特有的决策变量、约束、tiling_case 标识**定制**成求解器子类与入口。分开后，前者可以稳定复用（甚至预生成为 `AXES_SOLVER_CODE_HEAD` 静态常量），后者专注逐图定制，互不干扰。

**练习 2**：为什么 `GenSolverBaseClass` 用 `IsAnyModelEnableEqualOrderTiling`（任一 model 为真即开），而 `GenDoTiling` 用 `IsEnableEqualOrderTiling(model_info)`（单 model 判定）？

> **答案**：基类代码是整张图共享一份的（[axes_reorder_tiling_code_gen_impl.cpp:87-95](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/axes_reorder_tiling_code_gen_impl.cpp#L87-L95)），只要有一个 model 需要等序求解，基类就必须包含等序逻辑，所以取「并」；而每个求解入口只服务一个 model，用单 model 开关（[axes_reorder_tiling_code_gen_impl.cpp:131](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/axes_reorder_tiling_code_gen_impl.cpp#L131)）避免未启用等序的 model 白白走等序搜索。这正是 [axes_reorder_solver_code.h:196](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/solver_pass/axes_reorder_solver_code.h#L196) 新注释所说的「Reduce balance 开关在共享同一求解器定义的所有 model 间聚合」。

**练习 3**：什么条件下生成的 tiling 代码里会额外出现 `AxesReorderPgoSolver` 子类？

> **答案**：当 `config_.enable_autofuse_pgo || config_.is_inductor_scene` 为真时，`GenSolverBaseClass` 会追加 `GenAxesReorderPgoClassesHead(config_.pgo_step_max)` 与 `GenAxesReorderPgoClassesFunc()`（[axes_reorder_tiling_code_gen_impl.cpp:96-101](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/axes_reorder_tiling_code_gen_impl.cpp#L96-L101)）。PGO 子类继承 `AxesReorderSolver`，提供枚举多份候选 tiling 数据的能力，服务于 Inductor 多阶段 PGO 的 top-N 候选机制（详见 u8-l2）。

---

## 5. 综合实践

**任务**：画出 ATT `generator/` 从输入到 codegen 消费的完整数据流，并标注每个产物的来源与去向。

请完成以下子任务：

1. **入口与配置**：在 [gen_tiling_impl.cpp:165-206](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/gen_tiling_impl.cpp#L165-L206) 中，标出 `GenTilingImplAutoFuseV3` 的输入（`fused_schedule_result`）、关键配置项（`is_autofuse`、`gen_tiling_data`、`is_inductor_scene`）和输出（`tiling_func`）。

2. **三段式生成**：用一张表把 `TilingCodeGenImpl` 的 `GenTilingHead`、`GenTiling`、`GenTilingTail` 三段各自「读什么、生成什么、写入 `tiling_res` 的哪个 key」填出来（提示：综合 [tiling_code_gen_impl.cpp:3110-3161](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/tiling_code_gen_impl.cpp#L3110-L3161)、[4956-4989](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/tiling_code_gen_impl.cpp#L4956-L4989)、[4775-4817](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/tiling_code_gen_impl.cpp#L4775-L4817)）。

3. **产物落点**：对照 [att_const_values.h:111-127](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/base/att_const_values.h#L111-L127)，列出全部产物 key → 文件名，并标注哪些由 `FinishGeneratedHeaders` 统一渲染。

4. **codegen 衔接**：在 [codegen_tiling.cpp:750-804](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/codegen/codegen_tiling.cpp#L750-L804) 中，画出 `tiling_file_name_to_content` 这个 map 如何被 ATT 填充、又被 codegen 后处理（补 include guard、`PopulateFallbackAtomicHeaders` 铺底、cv 场景 `AddCvDeclarationsToApiHeader`、合并进 entry 编译单元）。

5. **缓存与 extra info 的位置**：在上述数据流图上标出 cache（[cache/](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/cache/operator_level_cache_gen.h)）与 extra info（[extra_info_generator.h](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/extra_info_gen/extra_info_generator.h)）织入生成流程的位置——它们都是被 `TilingCodeGenImpl` 在 Head/Body 阶段调用来「往生成的代码里再插入片段」。

6. **axes reorder 求解代码的注入路径**：在图上补一条支线——`GenTilingKeyFunc → GenSolverTiling/GenDoTiling → AxesReorderTilingCodeGenImpl 的 override → SolverPassManager → axes_reorder_solver_code 原始代码库`，并标注等序开关在哪两处分别以「聚合」与「单 model」形态出现（综合 [tiling_code_gen_impl.cpp:4851-4867](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/tiling_code_gen_impl.cpp#L4851-L4867) 与 [axes_reorder_tiling_code_gen_impl.cpp:82-138](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/axes_reorder_tiling_code_gen_impl.cpp#L82-L138)）。

**验收标准**：你的图能回答——「一个融合算子的 `FusedScheduledResult` 进来，ATT 生成了哪几个 `.h`/`.cpp` 片段、分别由哪个方法负责、最终怎么进了 codegen 的编译单元；其中求解器部分哪段是与图无关的模板、哪段是逐 model 定制的」。如果某处画不出箭头，回到对应源码精读段落确认。

## 6. 本讲小结

- ATT `generator/` 的使命是**把符号化 `ModelInfo` 翻译成运行期可执行的 C++ tiling 代码**，输出形态是 `std::map<std::string, std::string>`（ATT 侧叫 `tiling_res`，codegen 侧叫 `tiling_file_name_to_content`），这就是 ATT 与 codegen 的契约。
- 生成过程是三层结构：`extern "C"` 入口 → `TilingCodeGenerator` 编排（单 group / 多 group 两条路径）→ `TilingCodeGenImpl` 三段式 `GenTilingHead / GenTiling / GenTilingTail`；策略差异（`AXES_REORDER` / `HIGH_PERF`）通过虚函数注入。本次更新后，多 group 间的 `GetTiling` 转发统一为「只向登记序在后的 group 链式转发」，删除了 Inductor 特殊分支。
- 产物是 1 个公共头 + 5 个原子头（State/Log/Pgo/Solver/Api）+ tiling func，由 `FinishGeneratedHeaders` 统一套 include guard 渲染；Autofuse 场景下 TilingData 结构定义不归 ATT 生成（`gen_tiling_data=false`）。
- 下游 codegen 的 `TilingLib` 持有指向 `GenTilingImplAutoFuseV3` 的函数指针，`GetTilingHeaders` 调用它填充同一个 map，再补 include guard、fallback 头（cv 场景还会向 Api 头补 cv 声明），最终合并进 entry 编译单元。
- 两级缓存（operator 级 `FixedSizeHashMap` + LRU、group 级 `GroupLevelCache`）与 reuse group 机制把「运行期反复求解」压缩为「算一次、转发/查表 N 次」；operator 级缓存默认关闭。
- extra info（`loop_num`、`tail_size`、对齐大小等）是基本 tiling 参数的确定性派生量，由 `TilingDataGenerator` 的三类生成器产出表达式、`ExtraInfoGenerator` 排版，不参与求解搜索。
- axes reorder 求解代码分三层：`solver_pass/axes_reorder_solver_code` 原始模板（与图无关）→ `solver.h` 薄封装 → `solver_pass_gen/SolverPassManager` 逐 model 定制，经 `AxesReorderTilingCodeGenImpl` 的 `GenSolverBaseClass/GenSolverTiling/GenDoTiling` 注入；等序开关（Reduce balance）在基类处「跨 model 聚合」、在求解入口处「单 model 判定」，闭环了 u7-l2 `ArgListReorder` kEqual 档的运行期权衡。

## 7. 下一步学习建议

本讲完成了 att 阶段（u7 全单元）的源码精读，至此你已经走完了 Autofuse 主线的「图 IR → 注册 → optimize → att」半程。建议接下来：

1. **进入 codegen（u8 单元）**：本讲反复出现的下游消费者 `TilingLib`、`GetTilingHeaders`、entry 编译单元都属于 codegen。建议从 [u8-l1 Codegen 主类与生成流程](#) 开始，看 ATT 产出的 tiling func 如何与 codegen 生成的 kernel 主体拼装到一起，重点关注 `Codegen::Generate` 的三大分支与 `CodegenResult`。
2. **顺着衔接点深入**：本讲指出的 `tiling_res`/`tiling_file_name_to_content` 契约是理解 ATT↔codegen 的钥匙。在 u8 里可以反向验证——codegen 读取这些 key 时是否与 [att_const_values.h:111-127](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/base/att_const_values.h#L111-L127) 完全一致；u8-l2 还会展开本讲提到的 cv 场景 `AddCvDeclarationsToApiHeader` 与 cv tiling wrapper 复用编译。
3. **若对 PGO 细节感兴趣**：本讲 4.4 提到 `AxesReorderPgoSolver` 能枚举多份候选 tiling 数据，配合 `codegen_tiling_inductor_topn.cpp` 的 top-N 选择构成 Inductor 多阶段 PGO，这条链路在 [u8-l2](#) 有完整讲解。
4. **二次开发提示**：若要为 v35 平台新增 cube 类算子的 tiling 生成，关注 [u11-l1 v35 平台扩展](#)——v35 子目录会提供专属的 att 扩展，与本讲的 `TilingCodeGenConfig.is_cube` 开关呼应。
