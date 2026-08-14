# Tiling 代码生成

## 1. 本讲目标

本讲是 Autofuse 数据流中 **att（Auto Tiling）阶段的收尾篇**。在 [u7-l1](#) 里我们把候选 scheduled graph 建成了一份可求解的性能模型 `ModelInfo`，在 [u7-l2](#) 里把 tiling 选择形式化成了「目标 + 约束 + 决策变量」的优化问题并设计了求解器。这两讲处理的全是**符号表达式**，还没有一行真正可执行的设备/主机代码。

本讲要回答最后一个问题：**这些符号模型如何变成最终编译进 kernel 的 C++ 源码？**

学完后你应当掌握：

1. ATT `generator/` 模块的三层结构（extern C 入口 → `TilingCodeGenerator` 编排 → `TilingCodeGenImpl` 实现），以及 `GenTilingHead / GenTiling / GenTilingTail` 三段式流水线。
2. ATT 最终生成哪些产物（若干 `.h`/`.cpp` 源码片段），它们如何被下游 `codegen` 消费、拼进最终 kernel。
3. 两级 tiling 缓存（operator 级 / group 级）与 reuse group 的代码生成机制。
4. extra info（额外 tiling 字段）与 `TilingData` 结构的生成方式。

## 2. 前置知识

阅读本讲前，请确保理解以下概念（前序讲义已建立）：

- **Tiling（切分）**：把一个大数据块切成小块，决定每次搬多少、算多少、用多少核。参见 [u3-l1](#)。
- **ModelInfo**：ATT 的「总账本」，用符号表达式记录目标函数、硬件约束、决策变量。参见 [u7-l1](#)。
- **求解器在运行期执行**：Autofuse 采用「编译期生成求解器代码、运行期执行搜索」的架构——编译期只生成 C++ 源码，真正的 tiling 数值是在 kernel 运行时、拿到真实 shape 后才求解出来的。参见 [u7-l2](#)。
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
| `generator/axes_reorder_tiling_code_gen_impl.h` | 轴重排策略实现（`AXES_REORDER`，Autofuse 默认） |
| `generator/cache/` | 两级缓存代码生成（`tiling_cache_code_gen`、`operator_level_cache_gen`、`group_level_cache_gen`） |
| `generator/extra_info_gen/` | 额外 tiling 字段生成（`extra_info_generator`、`extra_info_config`） |
| `generator/tiling_data_gen/tiling_data_generator.h` | `TilingData` 字段表达式生成（Axes/BlockDim/Memory 三类） |
| `base/att_const_values.h` | 产物 key 与文件名常量（`kTilingHeadIdentify` 等） |
| `codegen/codegen_tiling.cpp` | 下游 codegen 侧消费 ATT 产物的 `TilingLib` |

> 导航提示：`generator/` 子目录的组织原则是「按产物分目录」——`cache/` 生成缓存相关代码，`extra_info_gen/` 生成额外字段，`tiling_data_gen/` 生成 TilingData 字段表达式，`solver_pass_gen/` 生成求解器子类。

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

[gen_tiling_impl.h:47-49](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/gen_tiling_impl.h#L47-L49) 声明了 Autofuse 专用的 tiling 生成入口，入参是算子名、`FusedScheduledResult`、选项 map，出参是 `tiling_func`（即 `tiling_res`）。

其实现里最关键的是配置构造与委派：

[gen_tiling_impl.cpp:190-204](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/gen_tiling_impl.cpp#L190-L204)——注意三个要点：`gen_tiling_data = false`（Autofuse 场景下 TilingData 结构由 codegen 另外生成，不由 ATT 生成）、`is_autofuse = true`、`is_inductor_scene` 透传，最后调用 `generator.GenTilingCode(...)` 并校验 `tiling_func` 里一定含 `kTilingHeadIdentify`。

> 为什么 Autofuse 把 `gen_tiling_data` 设为 false？因为 Autofuse 的 TilingData 是跨多个 schedule group 的统一结构 `AutofuseTilingData`，由 codegen 侧的 `TilingData("Autofuse")` 统一拼装；ATT 只负责生成「填充这个结构的 tiling 函数」，不负责结构定义本身。

**编排层：TilingCodeGenerator**

[tiling_code_generator.h:35-76](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/generator/tiling_code_generator.h#L35-L76) 定义了门面类。它对外只暴露动词 `GenTilingCode`，但有三个重载：返回 `model_infos`（落盘）、返回 `model_infos + tiling_res`（不落盘）、Autofuse 专用（`FusedParsedScheduleResult + tiling_res`）。

工厂方法决定走哪种策略：

[tiling_code_generator.cpp:106-120](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/generator/tiling_code_generator.cpp#L106-L120) `CreateTilingCodeGenImpl` 根据 `config.type` 创建 `HighPerfTilingCodeGenImpl` 或 `AxesReorderTilingCodeGenImpl`，这就是策略模式的落点。

Autofuse 重载的主干：

[tiling_code_generator.cpp:181-221](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/generator/tiling_code_generator.cpp#L181-L221) 先用 `CollectModelInfosAndMetadata` 把「asc_graph → schedule_result → schedule_group → impl_graph」四级结构摊平成一份 `all_model_infos`，并顺带收集 `score_funcs`、`var_relations`、`enable_group_parallels`、`workspace_tensor_id_set` 四类元数据；若只有一个 group，直接委派给 `model_infos` 重载；否则走完整的 Head → `GenScheduleGroupTilingBodies` → Tail → `FinishGeneratedHeaders`。

[tiling_code_generator.cpp:301-336](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/generator/tiling_code_generator.cpp#L301-L336) `GenScheduleGroupTilingBodies` 逐 group 创建 impl、`SetScheduleResultGroupNums`、调 `GenTiling`，最后把各 group 的 tiling data 拼到总 `tiling_data_type_name` 上——这就是多 group 场景下「先分后合」的合并点。

**实现层：TilingCodeGenImpl 的三段式**

`TilingCodeGenImpl` 是真正逐行打印代码的类。它持有几个核心输出缓冲（[tiling_code_gen_impl.h:341-345](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/generator/tiling_code_gen_impl.h#L341-L345)）：

- `tiling_data_`：TilingData 结构定义；
- `tiling_func_`：tiling 函数体（.cpp 逻辑）；
- `tiling_head_`：公共 tiling 头；
- `atomic_headers_`：5 个原子头文件的源码（按 `GeneratedHeaderId` 索引）。

三段式的职责划分：

[tiling_code_gen_impl.cpp:3110-3161](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/generator/tiling_code_gen_impl.cpp#L3110-L3161) `GenTilingHead` 负责「总框架」：初始化 `tiling_data_manager_`、生成 TilingData 头（若 `gen_tiling_data`）、生成宏与 include、写出 `namespace optiling` 骨架与公共框架代码（`GenCommonFrameWork`），并把结果刷进 `tiling_res[kTilingHeadIdentify]` 与 solver 头。

[tiling_code_gen_impl.cpp:4964-4997](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/generator/tiling_code_gen_impl.cpp#L4964-L4997) `GenTiling` 负责「单个 group 的主体」：先 `InitTilingGeneration`（重置缓冲、初始化 tiling data manager、生成 group 头），再调 `GenTilingKeyFunc`。`GenTilingKeyFunc`（[tiling_code_gen_impl.cpp:4859-4875](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/generator/tiling_code_gen_impl.cpp#L4859-L4875)）是核心：它对每个 `model_info` 调 `GenSolverTiling`（生成求解器子类）和 `GenTilingCaseImpl`（生成 tiling case 实现），再生成 `GenImplPtr`（tiling_key → impl 指针）、`GenGetTilingKey`、`GenTilingFuncCallEntrance`（搜索入口）。注意这些都是**虚函数**，由 `AxesReorder`/`HighPerf` 子类提供不同实现。

[tiling_code_gen_impl.cpp:4783-4825](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/generator/tiling_code_gen_impl.cpp#L4783-L4825) `GenTilingTail` 负责「收尾」：定义支持二次 tiling 的全局变量 `g_secondary_tiling_ratio`（运行期可调整核数比例）、生成 schedule group 尾部代码，刷进 `tiling_res`。

**两种策略：虚函数注入差异**

[axes_reorder_tiling_code_gen_impl.h:17-40](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/generator/axes_reorder_tiling_code_gen_impl.h#L17-L40) 是 Autofuse 默认策略，override 了 `GenSolverBaseClass / GenSolverTiling / GenDoTiling / GenHardwareCons / GenPipeTypeObj / GenGetObj` 等，并持有 `SolverPassManager`（承接 [u7-l2](#) 的 solver_pass）。

[high_perf_tiling_code_gen_impl.h:18-33](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/generator/high_perf_tiling_code_gen_impl.h#L18-L33) 是另一种策略，override 集合不同。两者共享 `TilingCodeGenImpl` 的全部框架代码，只在「如何生成求解器与目标函数」上分化。

**配置：TilingCodeGenConfig**

[generator_config.h:19-70](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/generator/generator_config.h#L19-L70) 是控制生成行为的总开关：`type`（策略）、`gen_tiling_data` / `gen_extra_infos`（是否生成结构/额外信息）、`enable_autofuse_pgo`（PGO 场景）、`cache_enabled_at_compile_time`（编译态缓存开关，默认关）、`ub_threshold` / `corenum_threshold`（多核 UB 权衡阈值）、`force_tiling_case` / `force_schedule_result`（调试用的强制模板）。`Debug()` 方法把所有字段拼成一行日志，便于 DFX 排查。

**产物 key 与文件名**

ATT 生成的 `tiling_res` 的 key 在 [att_const_values.h:111-127](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/base/att_const_values.h#L111-L127) 集中定义。5 个原子头文件由 `FinishGeneratedHeaders`（[tiling_code_gen_impl.cpp:559-578](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/generator/tiling_code_gen_impl.cpp#L559-L578)）按固定顺序（State → Log → Pgo → Solver → Api）渲染、套上 include guard、写入 `tiling_res`。key 到文件名的映射见下表：

| `tiling_res` 的 key | 落盘文件名 |
|---|---|
| `TilingHead` | `autofuse_tiling_func_common.h` |
| `TilingStateHeader` | `autofuse_tiling_func_state.h` |
| `TilingLogHeader` | `autofuse_tiling_func_log.h` |
| `TilingPgoHeader` | `autofuse_tiling_func_pgo.h` |
| `TilingSolverHeader` | `autofuse_tiling_func_solver.h` |
| `TilingApiHeader` | `autofuse_tiling_func_api.h` |
| `AutofuseTilingData`（结构，非 Autofuse 场景） | `tiling_data.h` |

key 由 [tiling_code_gen_impl.cpp:411-426](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/generator/tiling_code_gen_impl.cpp#L411-L426) `GetAtomicHeaderKey` 从内部枚举 `GeneratedHeaderId` 翻译得到。

**下游消费：codegen 侧的 TilingLib**

[u3-l2](#) 已指出 codegen 通过 `att::GenTilingImplAutoFuseV3` 反向调用 ATT。具体的衔接点在：

[codegen_tiling.cpp:425-460](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/codegen/codegen_tiling.cpp#L425-L460) `TilingLib` 构造函数：当没有外部 tiling 库（`lib_path` 为空）时，把 `this->codegen_func_` 指向 `att::GenTilingImplAutoFuseV3`（[codegen_tiling.cpp:438-439](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/codegen/codegen_tiling.cpp#L438-L439)）；否则用 `dlopen`/`dlsym` 加载外部符号。这是「反向调用」的物理落点——codegen 持有一个函数指针，默认指向 ATT。

[codegen_tiling.cpp:750-804](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/codegen/codegen_tiling.cpp#L750-L804) `GetTilingHeaders` 是真正的消费点：它先写好 include 头与 `#endif`，把前缀塞进 `tiling_file_name_to_content[kTilingHeadIdentify]`，然后调用 `codegen_func_(...)`（即 ATT 的 `GenTilingImplAutoFuseV3`）让 ATT 把 5 个原子头 + 公共头全部填进同一个 map（[codegen_tiling.cpp:784-789](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/codegen/codegen_tiling.cpp#L784-L789)），最后 codegen 再补上 include guard 尾巴与 fallback 头。

> 衔接的本质：ATT 与 codegen 之间的「接口」就是这个 `std::map<std::string, std::string>`（codegen 侧叫 `tiling_file_name_to_content`，ATT 侧叫 `tiling_res` / `tiling_func`）。ATT 填它，codegen 读它并拼装成最终编译单元。

#### 4.1.4 代码实践

**实践目标**：跟踪 `tiling_res` 这个 map 从 ATT 生成到 codegen 消费的全过程，亲手确认「ATT 输出什么、codegen 如何接住」。

**操作步骤**：

1. 打开 [tiling_code_generator.h:35-76](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/generator/tiling_code_generator.h#L35-L76)，找到 Autofuse 专用的 `GenTilingCode` 重载（第 42-43 行），确认它的最后一个出参类型是 `std::map<std::string, std::string> &tiling_res`。
2. 跟进 [tiling_code_generator.cpp:181-221](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/generator/tiling_code_generator.cpp#L181-L221)，在 `GenTilingHead`、`GenScheduleGroupTilingBodies`、`GenTilingTail`、`FinishGeneratedHeaders` 四处分别找到向 `tiling_res` 写入 key 的语句（提示：搜索 `tiling_res[` 和 `tiling_res[GetAtomicHeaderKey`）。
3. 跳到 [att_const_values.h:111-127](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/base/att_const_values.h#L111-L127)，把每个 key 对应的文件名抄成一张表。
4. 切换到消费侧 [codegen_tiling.cpp:750-804](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/codegen/codegen_tiling.cpp#L750-L804)，确认 codegen 用的 map 变量名是 `tiling_file_name_to_content`，且第 785 行把同一个 map 作为 `codegen_func_` 的第 4 个参数传入。

**需要观察的现象**：ATT 侧写入 map 用的 key 字符串（如 `"TilingHead"`、`"TilingApiHeader"`）与 codegen 侧读取时用的常量是否完全一致——这决定了两端能否对上。

**预期结果**：你会得到一张「ATT 写入点 → key → 文件名 → codegen 读取点」的对应表，证明 `tiling_res` 就是 ATT 与 codegen 之间的契约。运行结果待本地验证（本实践为源码阅读型，无需编译运行）。

#### 4.1.5 小练习与答案

**练习 1**：Autofuse 场景下，ATT 生成的 `tiling_res` 里**不会**包含哪个产物？为什么？

> **答案**：不会包含 `AutofuseTilingData` 的结构定义（即 `tiling_data.h` 内容）。因为 [gen_tiling_impl.cpp:193](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/gen_tiling_impl.cpp#L193) 把 `gen_tiling_data` 设为 `false`，而 `GenTilingHead`/`GenTilingTail`/`FinishGroupTiling` 里所有写 TilingData 结构的语句都被 `if (config_.gen_tiling_data)` 守卫。结构定义由 codegen 侧的 `TilingData("Autofuse")` 另行生成。

**练习 2**：如果想换一种 tiling 求解策略，需要改哪一处配置？策略之间的差异在代码里以什么机制隔离？

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

reuse group 的关键在于：一个标记为 reuse 的 group 不生成自己的求解器，而是生成一段「把自己的 `TilingData` cast 成源 group 的 `TilingData`、再调用源 group 的 `GetTiling`」的转发代码（见 [tiling_code_gen_impl.cpp:4999-5031](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/generator/tiling_code_gen_impl.cpp#L4999-L5031) 的 `GenReuseGroupTilingWrapperGetTiling`）。这把「N 个同构 group 各算一次」压缩成「算 1 次、转发 N-1 次」。

#### 4.2.3 源码精读

**缓存代码生成器基类**

[tiling_cache_code_gen.h:27-106](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/generator/cache/tiling_cache_code_gen.h#L27-L106) 定义了基类 `TilingCacheCodeGen`，核心职责是生成一个 `FixedSizeHashMap` 模板类——它把一组静态方法（`GenHashMapTemplate`、`GenFindMethod`、`GenInsertMethod`、`GenEraseMethod`、`GenHashFunction` 等）拼装成一个固定容量、开放寻址的哈希表，提供 `Find/Insert/Erase/Clear/Size`。这是两级缓存共用的底层数据结构。

**operator 级缓存**

[operator_level_cache_gen.h:25-119](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/generator/cache/operator_level_cache_gen.h#L25-L119) 派生类负责生成 `TilingCacheContext` 类、`OperatorLevelCache` 类型、以及 `FindOperatorCache`/`SaveOperatorCache` 两个运行期函数。其中 `GenInitAndQueryCacheCode` 与 `GenSaveCacheCalls` 负责在生成的 tiling 函数体里插入「查询」与「保存」调用。

具体生成的运行期代码长这样（节选）：

[operator_level_cache_gen.cpp:239-254](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/generator/cache/operator_level_cache_gen.cpp#L239-L254) `GenContextClassStructure` 生成 context 类的私有成员：一个 `thread_local` 的 `OperatorLevelCache<TilingData>` 指针、访问计数数组 `access_counts_`（用于 LRU 老化）。

[operator_level_cache_gen.cpp:290-308](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/generator/cache/operator_level_cache_gen.cpp#L290-L308) `GenFindOperatorCacheImpl` 生成查询函数：用 shape_key 在哈希表里 `Find`，命中则累加访问计数。

[operator_level_cache_gen.cpp:311-341](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/generator/cache/operator_level_cache_gen.cpp#L311-L341) `GenSaveOperatorCacheImpl` 生成保存函数：先尝试 `Insert`，缓存满到阈值（`kOperatorCacheCapacity * kLoadFactorThreshold`）就执行 LRU 老化——扫描 `access_counts_` 找最小值、`Clear` 后重新插入。哈希函数用黄金比例常数 `0x9e3779b9` 做混合（[operator_level_cache_gen.cpp:370-374](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/generator/cache/operator_level_cache_gen.cpp#L370-L374)）。

**group 级缓存**

[group_level_cache_gen.h:24-52](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/generator/cache/group_level_cache_gen.h#L24-L52) 负责生成 `GroupLevelCache` 类型与 group 间缓存函数，复用基类的 `FixedSizeHashMap`，但 key 维度是单个 group 内的 shape。

**reuse 关系收集与入口声明**

[tiling_code_generator.cpp:122-144](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/generator/tiling_code_generator.cpp#L122-L144) `GetCacheReuseInfo` 遍历 `FusedParsedScheduleResult`，凡是被标记为 reuse 的 group，就记下 `cache_reuse_info[cur_prefix] = reuse_prefix`。缓存容量在 [tiling_code_generator.cpp:208](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/generator/tiling_code_generator.cpp#L208) 定为 `all_model_infos.size() * 2`。

[tiling_code_gen_impl.cpp:3227-3237](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/generator/tiling_code_gen_impl.cpp#L3227-L3237) `GenCacheInit` 在生成的函数体里为每个 reuse 源 group 声明一个 `GroupLevelCache` 实例（用 `declared_cache_types_` 去重，避免重复声明）。

#### 4.2.4 代码实践

**实践目标**：理解 reuse group 如何把「多次求解」变成「一次求解 + 多次转发」。

**操作步骤**：

1. 阅读 [tiling_code_generator.cpp:122-144](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/generator/tiling_code_generator.cpp#L122-L144)，确认 `cache_reuse_info` 的 key 和 value 各是什么（提示：都是 group prefix 字符串）。
2. 打开 [tiling_code_gen_impl.cpp:4976-4985](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/generator/tiling_code_gen_impl.cpp#L4976-L4985)，看 `GenTiling` 如何判断当前 group 是 reuse group（`IsReuseGroup`），若是则走 `GenReuseGroupTilingWrapper` 而非正常求解路径。
3. 跟进 [tiling_code_gen_impl.cpp:4999-5031](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/generator/tiling_code_gen_impl.cpp#L4999-L5031)，找到转发到源 group 的那句 `auto ret = <reuse_prefix>::GetTiling(...)`。

**需要观察的现象**：reuse group 生成的 `GetTiling` 函数体里，是否完全不含求解器调用，只剩 `RefToRef` 类型转换 + 调用源 group。

**预期结果**：reuse group 的 `GetTiling` 只做 `TilingData` 的引用转换并委托给源 group，自身不生成 solver。这解释了为什么同构 group 越多，reuse 带来的编译产物体积与运行期求解节省越大。运行结果待本地验证（源码阅读型实践）。

#### 4.2.5 小练习与答案

**练习 1**：operator 级缓存默认是开还是关？由哪个字段控制？

> **答案**：默认关。由 `TilingCodeGenConfig.cache_enabled_at_compile_time` 控制（[generator_config.h:47](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/generator/generator_config.h#L47)，默认 `false`）。开启后才会在生成的代码里织入 `TilingCacheContext` 与 `Find/Save` 调用，并额外 require `<array>`、`<cstring>` 头（见 [tiling_code_gen_impl.cpp:4933-4938](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/generator/tiling_code_gen_impl.cpp#L4933-L4938)）。

**练习 2**：缓存满了之后，生成的代码用什么策略淘汰旧条目？

> **答案**：LRU 老化。当 `cache.Size() >= kOperatorCacheCapacity * kLoadFactorThreshold` 时，扫描 `access_counts_` 找最小访问计数，`Clear` 整个缓存后重新插入新条目（[operator_level_cache_gen.cpp:326-341](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/generator/cache/operator_level_cache_gen.cpp#L326-L341)）。

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

`ExtraInfoConfig`（[extra_info_config.h:15-19](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/generator/extra_info_gen/extra_info_config.h#L15-L19)）用两个开关控制：`do_api_tiling`（是否生成高阶 api tiling）、`do_axes_calc`（是否生成外轴/尾轴逻辑）。

#### 4.3.3 源码精读

**TilingDataGenerator：三类字段生成器**

[tiling_data_generator.h:21-27](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/generator/tiling_data_gen/tiling_data_generator.h#L21-L27) 定义字段类别枚举 `TilingDataGenType`：`AXES_TILING_DATA_GEN`（轴）、`GENERAL_TILING_DATA_GEN`（核数）、`MEMORY_TILING_DATA_GEN`（内存）、`ALL_TILING_DATA_GEN`（全量）。

三个具体生成器（[tiling_data_generator.h:56-126](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/generator/tiling_data_gen/tiling_data_generator.h#L56-L126)）：

- `AxesTilingDataGen`：核心方法 `AddAxesAlignedSize`（轴对齐）、`AddAxesTailSizeAndLoopNum`（尾块与循环次数）、`AddSplitOuterAxisTailArgs`（外轴尾块），内部用 `axes_tiling_data_map_` 按 axis_name 存「loop_num / tail_size」对（见文件内注释 `key: axis_name, value: [AxisTilingData(...)]`）。
- `BlockDimTilingDataGen`：依赖 `AxesTilingDataGen`，`AddUsedCoreNum` 生成实际使用核数。
- `MemoryTilingDataGen`：把 `var_name → Expr` 的内存参数翻成函数实现与调用。

管理类 `TilingDataGenerator`（[tiling_data_generator.h:129-156](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/generator/tiling_data_gen/tiling_data_generator.h#L129-L156)）按 `tiling_key` 缓存每组生成器，对外提供 `GetTilingDataWithAnnotation`（取字段定义）和 `GetTilingFuncImpl`/`GetTilingFuncInvoke`（取赋值函数实现与调用）。

**ExtraInfoGenerator：汇总输出**

[extra_info_generator.h:22-49](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/generator/extra_info_gen/extra_info_generator.h#L22-L49) 持有 `ExtraInfoConfig`、`model_info_list_` 和一个 `TilingDataGenerator` 引用，对外暴露 `GetExtraTilingDataDef`（拼结构定义）和 `GetExtraTilingVars`（列字段名）。

[extra_info_generator.cpp:23-51](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/generator/extra_info_gen/extra_info_generator.cpp#L23-L51) `WriteCoreParamData` 是核心：对每个 `model_info`，用 `tiling_data_generator_.GetTilingDataWithAnnotation(tiling_case_id, AXES_TILING_DATA_GEN)` 取出轴字段，借助 `TilingDataGenUtils::NeedWrittenTilingData` 去重后逐行打印；`GetExtraTilingDataDef` 把结果累加到 `type_name_to_definition["CoreParams"]`。

> 关键协作：`ExtraInfoGenerator` 不自己算字段，它只是「问 `TilingDataGenerator` 要字段表达式、再排版成结构定义」。真正的字段表达式来源是 `AxesTilingDataGen` 等三类生成器，而它们的输入是 `ModelInfo`（承接 [u7-l1](#)）。

#### 4.3.4 代码实践

**实践目标**：确认 extra info 的字段值是「派生公式」而非「求解器决策变量」。

**操作步骤**：

1. 打开 [tiling_data_generator.h:56-94](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/generator/tiling_data_gen/tiling_data_generator.h#L56-L94)，定位 `AxesTilingDataGen` 的私有方法 `AddAxesTailSizeAndLoopNum`。
2. 阅读文件头部注释（第 89-90 行附近）里的示例：`AxisTilingData(AXIS_LOOP_NUM, s0t_loop_num, Ceil(s0T / s0t), ...)`，理解 `loop_num` 是怎么由轴大小 `s0T` 和 tile 大小 `s0t` 算出来的。
3. 对比 [extra_info_generator.cpp:40-51](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/generator/extra_info_gen/extra_info_generator.cpp#L40-L51)，确认 `GetExtraTilingDataDef` 只调用了 `AXES_TILING_DATA_GEN` 这一类生成器来拼 `CoreParams`。

**需要观察的现象**：`loop_num`、`tail_size` 这类字段的表达式里是否含有求解器决策变量（如 `s0t`），以及它们是否由 `Ceil`、`%` 等确定性公式给出。

**预期结果**：extra info 字段都是基本 tiling 参数（如 `s0t`）经 `Ceil`/取模/对齐等确定性公式派生而来，不参与求解搜索。这解释了为何它们可以放在求解之外单独生成。运行结果待本地验证（源码阅读型实践）。

#### 4.3.5 小练习与答案

**练习 1**：`ExtraInfoGenerator` 与 `TilingDataGenerator` 是什么关系？谁真正产生字段表达式？

> **答案**：`ExtraInfoGenerator` 是「排版者」，`TilingDataGenerator` 是「字段来源」。`ExtraInfoGenerator` 持有 `TilingDataGenerator` 的引用（[extra_info_generator.h:24-26](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/generator/extra_info_gen/extra_info_generator.h#L24-L26)），通过它取字段表达式再排版成 `CoreParams` 等结构定义；真正的表达式由 `TilingDataGenerator` 内部的 `AxesTilingDataGen` 等生成器依据 `ModelInfo` 计算。

**练习 2**：为什么 `loop_num`、`tail_size` 不放进求解器，而是作为 extra info 单独生成？

> **答案**：因为它们是基本 tile 参数的**确定性派生量**（`loop_num = Ceil(轴/tile)`、`tail_size = 轴 % tile`），没有自由度，求解器选出 tile 后它们就唯一确定了。放进求解器只会无谓增加决策变量维度，所以放在求解之后用公式一次性算出。

---

## 5. 综合实践

**任务**：画出 ATT `generator/` 从输入到 codegen 消费的完整数据流，并标注每个产物的来源与去向。

请完成以下子任务：

1. **入口与配置**：在 [gen_tiling_impl.cpp:165-206](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/gen_tiling_impl.cpp#L165-L206) 中，标出 `GenTilingImplAutoFuseV3` 的输入（`fused_schedule_result`）、关键配置项（`is_autofuse`、`gen_tiling_data`、`is_inductor_scene`）和输出（`tiling_func`）。

2. **三段式生成**：用一张表把 `TilingCodeGenImpl` 的 `GenTilingHead`、`GenTiling`、`GenTilingTail` 三段各自「读什么、生成什么、写入 `tiling_res` 的哪个 key」填出来（提示：综合 [tiling_code_gen_impl.cpp:3110-3161](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/generator/tiling_code_gen_impl.cpp#L3110-L3161)、[4964-4997](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/generator/tiling_code_gen_impl.cpp#L4964-L4997)、[4783-4825](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/generator/tiling_code_gen_impl.cpp#L4783-L4825)）。

3. **产物落点**：对照 [att_const_values.h:111-127](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/base/att_const_values.h#L111-L127)，列出全部产物 key → 文件名，并标注哪些由 `FinishGeneratedHeaders` 统一渲染。

4. **codegen 衔接**：在 [codegen_tiling.cpp:750-804](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/codegen/codegen_tiling.cpp#L750-L804) 中，画出 `tiling_file_name_to_content` 这个 map 如何被 ATT 填充、又被 codegen 后处理（补 include guard、fallback 头、合并进 entry 编译单元）。

5. **缓存与 extra info 的位置**：在上述数据流图上标出 cache（[cache/](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/generator/cache/operator_level_cache_gen.h)）与 extra info（[extra_info_generator.h](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/generator/extra_info_gen/extra_info_generator.h)）织入生成流程的位置——它们都是被 `TilingCodeGenImpl` 在 Head/Body 阶段调用来「往生成的代码里再插入片段」。

**验收标准**：你的图能回答——「一个融合算子的 `FusedScheduledResult` 进来，ATT 生成了哪几个 `.h`/`.cpp` 片段、分别由哪个方法负责、最终怎么进了 codegen 的编译单元」。如果某处画不出箭头，回到对应源码精读段落确认。

## 6. 本讲小结

- ATT `generator/` 的使命是**把符号化 `ModelInfo` 翻译成运行期可执行的 C++ tiling 代码**，输出形态是 `std::map<std::string, std::string>`（ATT 侧叫 `tiling_res`，codegen 侧叫 `tiling_file_name_to_content`），这就是 ATT 与 codegen 的契约。
- 生成过程是三层结构：`extern "C"` 入口 → `TilingCodeGenerator` 编排（单 group / 多 group 两条路径）→ `TilingCodeGenImpl` 三段式 `GenTilingHead / GenTiling / GenTilingTail`；策略差异（`AXES_REORDER` / `HIGH_PERF`）通过虚函数注入。
- 产物是 1 个公共头 + 5 个原子头（State/Log/Pgo/Solver/Api）+ tiling func，由 `FinishGeneratedHeaders` 统一套 include guard 渲染；Autofuse 场景下 TilingData 结构定义不归 ATT 生成（`gen_tiling_data=false`）。
- 下游 codegen 的 `TilingLib` 持有指向 `GenTilingImplAutoFuseV3` 的函数指针，`GetTilingHeaders` 调用它填充同一个 map，再补 include guard 与 fallback 头，最终合并进 entry 编译单元。
- 两级缓存（operator 级 `FixedSizeHashMap` + LRU、group 级 `GroupLevelCache`）与 reuse group 机制把「运行期反复求解」压缩为「算一次、转发/查表 N 次」；operator 级缓存默认关闭。
- extra info（`loop_num`、`tail_size`、对齐大小等）是基本 tiling 参数的确定性派生量，由 `TilingDataGenerator` 的三类生成器产出表达式、`ExtraInfoGenerator` 排版，不参与求解搜索。

## 7. 下一步学习建议

本讲完成了 att 阶段（u7 全单元）的源码精读，至此你已经走完了 Autofuse 主线的「图 IR → 注册 → optimize → att」半程。建议接下来：

1. **进入 codegen（u8 单元）**：本讲反复出现的下游消费者 `TilingLib`、`GetTilingHeaders`、entry 编译单元都属于 codegen。建议从 [u8-l1 Codegen 主类与生成流程](#) 开始，看 ATT 产出的 tiling func 如何与 codegen 生成的 kernel 主体拼装到一起，重点关注 `Codegen::Generate` 的三大分支与 `CodegenResult`。
2. **顺着衔接点深入**：本讲指出的 `tiling_res`/`tiling_file_name_to_content` 契约是理解 ATT↔codegen 的钥匙。在 u8 里可以反向验证——codegen 读取这些 key 时是否与 [att_const_values.h:111-127](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/base/att_const_values.h#L111-L127) 完全一致。
3. **若对求解细节感兴趣**：可回头精读 `solver_pass_gen/`（本讲只提到 `AxesReorderTilingCodeGenImpl` 持有 `SolverPassManager`），看 u7-l2 的 `GeneralSolver` 的「定域 + 微调」两阶段搜索如何被生成为具体 C++ 子类。
4. **二次开发提示**：若要为 v35 平台新增 cube 类算子的 tiling 生成，关注 [u11-l1 v35 平台扩展](#)——v35 子目录会提供专属的 att 扩展，与本讲的 `TilingCodeGenConfig.is_cube` 开关呼应。
