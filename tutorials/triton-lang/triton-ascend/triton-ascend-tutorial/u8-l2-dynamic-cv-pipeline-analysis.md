# DynamicCVPipeline：数据流分析与计算块规划

## 1. 本讲目标

在 u8-l1 中，我们建立了昇腾 NPU「Cube（矩阵）/Vector（向量）双计算单元」的硬件模型，并了解到「CV 融合算子」能让 Cube 与 Vector 在同一个核内协同工作。本讲要回答一个更深层的问题：**当一段 Triton kernel 里同时含 `tl.dot`（Cube）和逐元素/reduce（Vector）两类计算时，编译器如何自动地把它们拆分到两条计算流水线上、并插入正确的跨核数据搬运与同步，从而让 Cube 与 Vector 真正并行（重叠）执行？**

完成本讲后，你应当能够：

- 说清 `DynamicCVPipeline` 这个顶层 pass 的整体目标、10 个子 pass 的编排顺序，以及它的「安全回退」机制。
- 掌握 `PreCheckAvailable` 如何在流水线最前端做可行性预判（黑名单 + 矩阵校验）。
- 掌握 `PlanComputeBlock`（核心是 `OpClassifier`）如何用「图染色 + BFS 传播」给每个 op 标上 `CUBE`/`VECTOR`，并规划出「计算块（compute block）」。
- 掌握 `SplitDataflow`（核心是 `DataDependencyAnalysis`）如何识别 Vector↔Cube 之间的三类跨核数据依赖，并用「最低公共祖先（LCA）」算法定位生产者/消费者块。
- 理解 `AnalyzeDataFlow` 收集名称/作用域/参数/标志信息的职责。
- 看懂新增的 `DynamicCVPipeline_ut` 手写 MLIR 测试套件如何以「编译出 MLIR → 断言关键字」的方式覆盖各种依赖场景。

## 2. 前置知识

阅读本讲前，请确认你已经掌握以下概念（来自前置讲义）：

- **Cube Core 与 Vector Core**（u8-l1）：NPU 的 AI 核内含专做矩阵乘的 Cube 单元和做向量运算的 Vector 单元。`tl.dot` 的归宿是 Cube，绝大多数逐元素/reduce 运算的归宿是 Vector。
- **Linalg IR**（u4-l5）：`triton-to-linalg` pass 把 `ttir` 算子翻译成 `linalg.matmul` / `linalg.fill` / `arith.addf` 等。`DynamicCVPipeline` 正是在 **Linalg IR** 上工作的——它接在 `triton-to-linalg` 之后。
- **MLIR Pass / PassManager**（u4-l1）：一个 pass 接收并改写一个 `ModuleOp`；多个 pass 串成 `PassManager` 依次执行；pass 之间可通过 `getAnalysis<T>()` 传递分析结果。
- **`scope` 方言**（u4-l5）：BiSheng 的 `scope::ScopeOp` 用来把一段计算框进一个执行作用域。`DynamicCVPipeline` 成功时，最终会在 IR 里生成 `scope` 算子，把 Cube、Vector 各自框进独立流水线。

几个本讲会用到的术语先打个预防针：

- **core_type（核类型）**：一个 op 归属 `CUBE` 还是 `VECTOR`，记录在 `ssbuffer.core_type` 属性里。注意这里的 `ssbuffer` 指的是「SeparateSSBUFFER」——这正是本流水线的别名。
- **block_id（计算块编号）**：把连续的、同一核类型的 op 归并为一个「计算块」，编号记在 `ssbuffer.block_id` 属性里。跨核依赖发生在两个不同 block_id 之间。
- **回退（fallback）**：流水线任一阶段若发现「这个 kernel 我处理不了」，会设置一个错误码属性，顶层 pass 把整个模块**恢复到变换前**的备份，继续走「不带动态 CV 流水线」的普通编译。

## 3. 本讲源码地图

本讲涉及的源码集中在 `third_party/ascend/lib/DynamicCVPipeline/`、`include/DynamicCVPipeline/` 与 `unittest/DynamicCVPipeline_ut/`。关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [lib/DynamicCVPipeline/AddDynamicCVPipeline.cpp](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/lib/DynamicCVPipeline/AddDynamicCVPipeline.cpp) | 顶层编排 pass：克隆备份 → 跑 10 个子 pass → 失败则回退恢复 |
| [include/DynamicCVPipeline/Passes.h](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/include/DynamicCVPipeline/Passes.h) | 子 pass 的注册入口（`GEN_PASS_REGISTRATION`） |
| [include/DynamicCVPipeline/Common/Utils.h](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/include/DynamicCVPipeline/Common/Utils.h) | 全部 `ssbuffer.*` / `hivm.*` 属性名常量、`CoreType` 枚举、回退错误码 |
| [lib/DynamicCVPipeline/PreCheckAvailable.cpp](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/lib/DynamicCVPipeline/PreCheckAvailable.cpp) 与 `PreCheckAvailable/PreCheckBlacklist.cpp` | 可行性预判：黑名单（`scope.scope`、`scf.while`）+ matmul 校验 |
| [lib/DynamicCVPipeline/PlanComputeBlock.cpp](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/lib/DynamicCVPipeline/PlanComputeBlock.cpp) | 计算块规划编排：OpClassifier → PlanCubeBlock → PlanVectorBlock → Reorder |
| [lib/DynamicCVPipeline/PlanComputeBlock/OpClassifier.cpp](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/lib/DynamicCVPipeline/PlanComputeBlock/OpClassifier.cpp) | **核心**：给每个 op 染色为 CUBE/VECTOR，处理 CUBE_AND_VECTOR 拆分、SCF yield |
| [lib/DynamicCVPipeline/PlanComputeBlock/PlanCubeBlock.cpp](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/lib/DynamicCVPipeline/PlanComputeBlock/PlanCubeBlock.cpp) | 把 Cube 种子 op 按 BFS 合并成计算块、分配 block_id |
| [lib/DynamicCVPipeline/SplitDataflow.cpp](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/lib/DynamicCVPipeline/SplitDataflow.cpp) | 数据流拆分编排：含 7 个子步骤 |
| [lib/DynamicCVPipeline/SplitDataflow/DataDependencyAnalysis.cpp](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/lib/DynamicCVPipeline/SplitDataflow/DataDependencyAnalysis.cpp) | **核心**：识别 V→C / C→V / 内存跨核依赖，LCA 定位块 |
| [lib/DynamicCVPipeline/AnalyzeDataFlow.cpp](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/lib/DynamicCVPipeline/AnalyzeDataFlow.cpp) | 收集 name/scope/args/flag/cube 控制流输入链信息 |
| [backend/compiler.py](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/backend/compiler.py) | Python 侧接线：`enable_dynamic_cv_pipeline` 选项、`add_dynamic_cv_pipeline` 注册、rc 回读 |
| [unittest/DynamicCVPipeline_ut/test_pcb01_mlir_single_cube_single_vector.py](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/unittest/DynamicCVPipeline_ut/test_pcb01_mlir_single_cube_single_vector.py) | PlanComputeBlock 场景参考样例（独立 CV，回退，无 scope） |
| [unittest/DynamicCVPipeline_ut/test_sdf01_mlir_v2c_unaligned.py](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/unittest/DynamicCVPipeline_ut/test_sdf01_mlir_v2c_unaligned.py) | SplitDataflow 场景参考样例（V→C 依赖，生成 scope） |
| [unittest/DynamicCVPipeline_ut/dynamic-cv-pipeline-ci.sh](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/unittest/DynamicCVPipeline_ut/dynamic-cv-pipeline-ci.sh) | CI：跑测试生成 MLIR，再对比 base/PR 两份 MLIR |

---

## 4.1 DynamicCVPipeline：整体编排与安全回退

### 4.1.1 概念说明

`DynamicCVPipeline`（动态 Cube-Vector 流水线）是一个**编译期 IR 变换流水线**，目标是把一段「混在一起」的 Cube/Vector 计算自动重构为两条可重叠执行的流水线：Cube 一边做矩阵乘，Vector 一边做逐元素/reduce，二者通过片上缓冲（L0C/UB）和跨核同步原语交换数据。所谓「动态」，是指它不依赖固定的模板，而是**根据每个 kernel 的实际数据流**现场规划计算块和同步点。

它之所以能放心地做这种激进的 IR 重构，是因为设计了一套**「先克隆备份、失败即回退」**的安全网：进入时把整个模块克隆一份，任何子 pass 失败或主动要求回退时，就把模块恢复成备份原样，转而走普通编译路径。这意味着该流水线是**纯增益**的——能优化就优化，不能优化就当它不存在，绝不会让原本能编译的 kernel 编不过。

### 4.1.2 核心流程

顶层 pass `AddDynamicCVPipelinePass` 的执行流程（伪代码）：

```
runOnOperation(module):
    清除旧的错误码属性
    if 不是 910_95(950) 平台: 直接返回（该特性仅 950 可用）
    backup = module.clone()              # 关键：先存档

    pm = PassManager()
    pm.add(PreCheckAvailable)            # ① 可行性预判
    pm.add(StandardizeOp)                # ② 算子标准化
    pm.add(PlanComputeBlock)             # ③ 染色 + 计算块规划
    pm.add(ComputeBlockOpt)              # ④ 计算块优化
    pm.add(SplitDataflow)                # ⑤ 数据流拆分（跨核依赖）
    pm.add(AnalyzeDataFlow)              # ⑥ 信息收集
    pm.add(AllocMultiCache)              # ⑦ 多缓冲分配
    pm.add(AddControlFlowCondition)      # ⑧ 控制流条件化
    pm.add(SeparateMemoryFromCompute)    # ⑨ 访存/计算解耦（见 u8-l3）
    pm.add(RemoveSsbufAttr)              # ⑩ 清理 ssbuffer 属性

    if pm.run 失败 或 模块带 fallback 属性:
        读取错误码 rc
        restoreModuleFromBackup(module, backup)   # 恢复原样
        module.setAttr(rc)                        # 留一个 rc 给 Python
        return
    销毁 backup  # 全程成功
```

本讲聚焦于 **①③⑤⑥** 这几个「分析与规划」型子 pass（②④⑧⑩是辅助变换，⑦⑨涉及多缓冲与访存解耦，留待 u8-l3）。

### 4.1.3 源码精读

顶层 pass 的「平台门控 + 克隆备份 + 10 个子 pass 注册」：

这段代码先做平台检查（仅 950 支持），再克隆整个模块作为备份，然后按固定顺序加入 10 个子 pass，是整条流水线的总装配线——[AddDynamicCVPipeline.cpp:L72-L98](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/lib/DynamicCVPipeline/AddDynamicCVPipeline.cpp#L72-L98)。

「失败即回退」的核心：若 `runPipeline` 返回失败，或任一子 pass 在模块上设置了 fallback 属性，就读取错误码、调用 `restoreModuleFromBackup` 把模块恢复成备份、并重新打上错误码属性后返回——[AddDynamicCVPipeline.cpp:L99-L122](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/lib/DynamicCVPipeline/AddDynamicCVPipeline.cpp#L99-L122)。

`restoreModuleFromBackup` 的实现：把备份的位置、属性、properties、以及整个 body region 原样拷回当前模块，等价于「撤销本流水线的所有改写」——[AddDynamicCVPipeline.cpp:L54-L64](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/lib/DynamicCVPipeline/AddDynamicCVPipeline.cpp#L54-L64)。

错误码与属性名的统一定义（贯穿所有子 pass）：`ERRCODE_ATTR = "triton_ascend.dynamic_cv_pipeline.rc"`，`ERRCODE_FAILED=1`（处理失败）、`ERRCODE_IGNORED=2`（主动忽略）；以及全部 `ssbuffer.*` 属性名——[Common/Utils.h:L36-L74](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/include/DynamicCVPipeline/Common/Utils.h#L36-L74)。

Python 侧的接线：在 `ttir_to_linalg`（`ttadapter` 阶段）里，当 `enable_dynamic_cv_pipeline` 为真时，先把若干配套选项设好（`enable_mixed_cv=True`、`set_workspace_multibuffer=0` 等），再注册 `add_dynamic_cv_pipeline`——[compiler.py:L217-L228](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/backend/compiler.py#L217-L228)。

Python 如何感知「C++ 侧回退」：`_adjust_metadata_by_module_result` 从模块读回 `rc` 属性，若 `rc > 0` 就把 `enable_dynamic_cv_pipeline` 等选项改回 False，使后续阶段（如多缓冲）按「未启用 CV 流水线」处理——[compiler.py:L109-L119](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/backend/compiler.py#L109-L119)。

选项默认值：`enable_dynamic_cv_pipeline` 若用户未指定，默认等于 `is_compile_on_910_95()`，即**在 950 上自动开启**——[compiler.py:L1227-L1229](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/backend/compiler.py#L1227-L1229)。

### 4.1.4 代码实践

**实践目标**：在 950（910_95）上确认 DynamicCVPipeline 是否被触发，并观察回退日志。

**操作步骤**：

1. 准备一个会**触发回退**的 kernel：在 kernel body 里显式写一个 `scf.while` 循环（例如用 `tl.while` 形态的控制流，或直接构造一个含 while 的 kernel）。
2. 用 `enable_dynamic_cv_pipeline=True`、`compile_on_910_95=True` 编译它。
3. 设置环境变量 `MLIR_ENABLE_DUMP=1`（或开启 debug）观察编译日志。

**需要观察的现象**：编译仍能成功（因为回退保护），但日志里出现类似 `[AddDynamicCVPipeline] Pass failed, fallback to compilation without dynamic CV pipeline.` 的警告；最终 IR 中不含 `scope` 算子。

**预期结果**：带有 `scf.while` 的 kernel 会命中 `PreCheckBlacklist`（见 4.2）从而回退，编译产物与「未开 CV 流水线」一致。

> 若无 950 实机，可只在源码层面确认：本实践的运行结果**待本地验证**；但可读 `AddDynamicCVPipeline.cpp` 的回退分支确认逻辑。

### 4.1.5 小练习与答案

**练习 1**：为什么顶层 pass 要在一切开始前 `removeAttr(CVPipeline::ERRCODE_ATTR)`（L78）？

**参考答案**：错误码属性是上一次编译可能残留在缓存模块上的。本次编译必须从「无错误码」的干净状态出发，否则后面 `hasFallbackAttr` 的判断会被旧值污染，误触发回退。

**练习 2**：`ERRCODE_FAILED(1)` 与 `ERRCODE_IGNORED(2)` 的语义差别是什么？

**参考答案**：`FAILED` 表示「我想处理但处理过程中出错了」（如找不到公共祖先块）；`IGNORED` 表示「我主动决定不处理这个 kernel」（如命中黑名单、检测到已被其它 Ascend 优化过的 `scope.scope`）。两者都会触发回退，但含义不同，便于后续诊断。

---

## 4.2 PreCheckAvailable：流水线可行性预判

### 4.2.1 概念说明

`PreCheckAvailable` 是整条流水线的**第一道关卡**，在真正染色、拆分之前先快速判断「这个 kernel 适不适合走 CV 流水线」。它的判断很便宜（只是一次遍历），目的是尽早剔除那些「即使跑了也得不到收益、甚至出错」的情况，避免后面九个 pass 白做功再回退。

它内部又分两个子检查：

- **PreCheckBlacklist**（黑名单）：若 IR 里已经存在 `scope.scope` 或 `scf.while`，说明这段计算**要么已经被 Ascend 其它优化处理过、要么是 CV 流水线无法处理的 while 循环**，直接 `IGNORED`。
- **PreCheckMatmul**（矩阵校验）：检查 matmul 的形状等是否满足 CV 流水线的要求（详见源码，本讲不展开）。

### 4.2.2 核心流程

```
PreCheckAvailable.runOnOperation(module):
    if 已有 fallback 属性: return
    pm = PassManager()
    pm.add(PreCheckBlacklist)    # 命中黑名单 -> ERRCODE_IGNORED
    pm.add(PreCheckMatmul)       # 形状不满足 -> 回退
    if pm.run 失败:
        setFallbackAttr(IGNORED)
```

### 4.2.3 源码精读

`PreCheckAvailable` 的编排：先跑黑名单、再跑 matmul 校验，任一失败即设置 `ERRCODE_IGNORED`——[PreCheckAvailable.cpp:L44-L64](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/lib/DynamicCVPipeline/PreCheckAvailable.cpp#L44-L64)。

黑名单定义与命中逻辑：黑名单只有两个 op 名 `scope.scope` 和 `scf.while`；`module.walk` 遍历到任一即中断并设置回退——[PreCheckBlacklist.cpp:L35-L38](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/lib/DynamicCVPipeline/PreCheckAvailable/PreCheckBlacklist.cpp#L35-L38) 与 [PreCheckBlacklist.cpp:L64-L83](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/lib/DynamicCVPipeline/PreCheckAvailable/PreCheckBlacklist.cpp#L64-L83)。

### 4.2.4 代码实践

**实践目标**：确认黑名单的判定依据。

**操作步骤**（源码阅读型）：在 `PreCheckBlacklist.cpp` 中找到 `kBlacklistOpNames`，思考：为何 `scope.scope` 命中就要跳过？

**需要观察的现象 / 预期结果**：因为 `scope.scope` 表示这段 IR **已经被 Ascend 侧（可能是手工或别的 pass）框进了执行作用域**，再让 DynamicCVPipeline 重复处理会破坏既有结构，故选择跳过。这说明 DynamicCVPipeline 只处理「原始的、未经 CV 划分的」Linalg IR。

### 4.2.5 小练习与答案

**练习**：如果一个 kernel 不含 `tl.dot`（纯 Vector），`PreCheckAvailable` 会放行它吗？后续会怎样？

**参考答案**：`PreCheckAvailable` 只看黑名单和 matmul 形状，纯 Vector kernel 通常会通过预判；但到了 `PlanComputeBlock`，因为没有 Cube 种子，规划不出有意义的 CV 划分，最终大概率不会产生 `scope`（参考 PCB01 测试，见 4.5）。

---

## 4.3 PlanComputeBlock：算子染色与计算块规划

### 4.3.1 概念说明

这是整条流水线里**信息量最大**的一步，目标是给模块里每个 op 打上 `ssbuffer.core_type`（`CUBE`/`VECTOR`）和 `ssbuffer.block_id`（计算块编号）。它解决的问题是：「这段混合计算里，哪些该归 Cube 核、哪些该归 Vector 核，相邻的同核 op 该不该合并成一个块？」

核心子 pass 是 **`OpClassifier`（算子分类器）**，它用了一种经典的**图染色 + BFS 传播**思路：

1. **找种子**：`linalg.matmul` 永远是 CUBE；顺着 matmul 的输入（`to_tensor`/`transpose`/`fill`/`empty`）和输出（`hivm.store`/`extract_slice`/`materialize_in_destination`）找出 CUBE 种子 op。
2. **CUBE 向上传播**：从种子出发做 BFS，把上游依赖也染成 CUBE（但跳过 `matmul` 本身、arith 张量算子、linalg 内部块）。
3. **剩余染 VECTOR**：还没被染色的 op 默认是 VECTOR。
4. **VECTOR 向上传播**：再从所有 VECTOR op 出发 BFS 染上游（但绝不把 `matmul` 染成 VECTOR）。
5. **CUBE_AND_VECTOR 拆分**：若一个 op 同时被 CUBE 用户和 VECTOR 用户使用（如共享的 `linalg.fill`），就**克隆**成两份：原 op 给 CUBE，克隆 op 给 VECTOR。
6. **SCF yield 处理**：给 `scf.yield` 及其父 `scf.if`/`scf.for` 打上与其结果相符的核类型。
7. **盖戳**：把每个 op 的 `core_type` 写成 IR 属性。

随后 **`PlanCubeBlock` / `PlanVectorBlock`** 把连续的同核 op 合并成计算块并分配 `block_id`，**`ReorderOpsByBlockId`** 按块重排 op 顺序。

> 直觉：这就像给一张计算图上色——先确定「必红」（matmul）和它的红色朋友圈，剩下的染「蓝」，遇到「红蓝都要」的就复制一份分红蓝两份。染色完成后，连续的同色节点就是一条流水线上的一个工作单元（计算块）。

### 4.3.2 核心流程

```
PlanComputeBlock.runOnOperation(module):
    pm.add(OpClassifier)        # 染色（8 个内部步骤）
    pm.add(PlanCubeBlock)       # Cube op 合并成块、分配 block_id
    pm.add(PlanVectorBlock)     # Vector op 合并成块
    pm.add(ReorderOpsByBlockId) # 按 block_id 重排
```

`OpClassifier` 内部 8 步（精简）：

```
initialize: 全部 op 置 UNDETERMINED
Step1 patternMatchCUBE:   matmul→CUBE，匹配上下游种子
Step2 propagateCubeUpstream:   从种子 BFS 染上游为 CUBE
Step3 penetrateCubeIntoForLoops: 纯数据搬运的 for 循环整块染 CUBE
Step4 markRemainingAsVector:    剩余染 VECTOR
Step5 propagateVectorUpstream:  从 VECTOR BFS 染上游
Step6 handleCubeAndVector:      共享 op 克隆拆分为 CUBE+VECTOR 两份
Step7 handleSCFYield:           给 yield/if/for 打核类型
Step8 stampToIR:                把 core_type 写成 ssbuffer.core_type 属性
```

### 4.3.3 源码精读

`PlanComputeBlock` 的编排：依次跑 OpClassifier、PlanCubeBlock、PlanVectorBlock、Reorder——[PlanComputeBlock.cpp:L44-L75](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/lib/DynamicCVPipeline/PlanComputeBlock.cpp#L44-L75)。

`OpClassifier::runOnOperation` 的 8 步总览——[OpClassifier.cpp:L1556-L1626](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/lib/DynamicCVPipeline/PlanComputeBlock/OpClassifier.cpp#L1556-L1626)。

Step1 找 CUBE 种子：`linalg.matmul` 标 CUBE_ONLY，并对每个操作数匹配 `to_tensor`/`transpose`/`fill`/`empty` 上游模式、对结果匹配 `store`/`extract_slice`/`materialize` 下游模式——[OpClassifier.cpp:L414-L517](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/lib/DynamicCVPipeline/PlanComputeBlock/OpClassifier.cpp#L414-L517)。

Step4 剩余染 VECTOR：所有仍为 `UNDETERMINED` 的 op（除 `scf.yield`）标为 `VECTOR_ONLY`，确立了「非 CUBE 即 VECTOR」的默认——[OpClassifier.cpp:L703-L723](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/lib/DynamicCVPipeline/PlanComputeBlock/OpClassifier.cpp#L703-L723)。

Step6 共享 op 拆分（`CUBE_AND_VECTOR`）：把同时服务 CUBE 与 VECTOR 的 op 克隆一份，原 op 归 CUBE、克隆 op 归 VECTOR，并把 VECTOR 用户改接到克隆结果——[OpClassifier.cpp:L1378-L1471](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/lib/DynamicCVPipeline/PlanComputeBlock/OpClassifier.cpp#L1378-L1471)。

Step8 盖戳：把内存里的核类型写成 `ssbuffer.core_type` 属性（跳过 scf、linalg 内部块、module/func）——[OpClassifier.cpp:L1517-L1553](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/lib/DynamicCVPipeline/PlanComputeBlock/OpClassifier.cpp#L1517-L1553)。

核类型字符串映射与枚举：`CUBE`/`VECTOR`/`CUBE_AND_VECTOR`/`UNDETERMINED`——[OpClassifier.cpp:L81-L92](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/lib/DynamicCVPipeline/PlanComputeBlock/OpClassifier.cpp#L81-L92)。

`PlanCubeBlock` 如何分配 block_id：对每个含 Cube 的 Block，以 matmul 为种子 BFS 扩张成一个组，用 `ComputeBlockIdManager` 给整组盖一个新 `block_id`（`markOpsWithNewId`），并做环检测避免把有环的依赖并进同一块——[PlanCubeBlock.cpp:L585-L615](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/lib/DynamicCVPipeline/PlanComputeBlock/PlanCubeBlock.cpp#L585-L615) 与入口 [PlanCubeBlock.cpp:L617-L642](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/lib/DynamicCVPipeline/PlanComputeBlock/PlanCubeBlock.cpp#L617-L642)。

### 4.3.4 代码实践

**实践目标**：在一个含 `tl.dot` 的 kernel 上，观察 `OpClassifier` 染色后的 `ssbuffer.core_type` 属性分布。

**操作步骤**：

1. 取 `unittest/DynamicCVPipeline_ut/test_pcb04_mlir_multi_cube_single_vector.py` 这类「多 Cube + 单 Vector」测试的 kernel（它含多个 `tl.dot` 与一段逐元素运算）。
2. 模仿 `test_pcb01` 的 `compile_kernel`，以 `enable_dynamic_cv_pipeline=True, compile_on_910_95=True` 编译到 Linalg。
3. 在生成的 MLIR 文本里搜索 `ssbuffer.core_type`。

**需要观察的现象**：`linalg.matmul` 及其紧邻的 `to_tensor`/`fill` 带 `ssbuffer.core_type = "CUBE"`；逐元素 `arith.addf`/`linalg` 等带 `"VECTOR"`；若某个 `fill` 同时喂给 matmul 和 addf，应能看到它被克隆成两份、分别标 CUBE 与 VECTOR。

**预期结果**：CUBE 与 VECTOR 在 IR 上被明确区分，连续同核 op 共享同一个 `ssbuffer.block_id`。运行结果**待本地验证**（需 950 编译环境）。

### 4.3.5 小练习与答案

**练习 1**：为什么 CUBE 的 BFS 传播要「跳过 arith 方言的张量结果算子」？

**参考答案**：arith 张量算子（如 `arith.addf` 作用于 tensor）是典型的逐元素 Vector 计算，把它染成 CUBE 既不符合硬件擅长（Cube 不擅长逐元素），也会把 Cube 块撑得过大、破坏流水线划分，故显式跳过，留给 VECTOR。

**练习 2**：`CUBE_AND_VECTOR` 拆分为什么必须**先递归处理操作数**再克隆当前 op？

**参考答案**：克隆当前 op 给 VECTOR 时，它的操作数若本身也是 CUBE_AND_VECTOR，就必须先被拆出 VECTOR 克隆，才能让新克隆 op 的输入指向「VECTOR 侧」的值；否则克隆 op 仍会引用 CUBE 侧的值，造成两条流水线串扰。这是深度优先的原因（见源码注释 L1391-L1403）。

---

## 4.4 SplitDataflow：跨核数据依赖分析

### 4.4.1 概念说明

染色（4.3）只是把 op 归了类，但 Cube 块和 Vector 块之间往往**有数据往来**——比如 Vector 先算出一个 bias，Cube 的 matmul 再加上它；或 Cube 算出矩阵结果，Vector 再对它做 reduce。`SplitDataflow` 的核心子 pass **`DataDependencyAnalysis`（数据依赖分析）**就是要找出这些**跨核依赖**，为后续插入「跨核数据搬运 + 同步」（`InterCoreTransferAndSync`）、标记主循环（`MarkMainLoop`）、分离 CV 作用域（`SeparateCVScope`）做准备。

它把跨核依赖分成三类，记录在一个 `DataDependencyInfo` 分析结果里：

- **V→C（VectorToCube）**：Cube 块的外部输入来自一个 Vector 块的结果。
- **C→V（CubeToVector）**：Cube 块的结果被一个 Vector 块使用。
- **内存依赖（MemoryDependencies）**：两个不同核类型的 op 通过**同一块内存**（如对同一 memref 的写-读）产生隐式依赖，而非 SSA 直接相连。

每条依赖都要回答两个问题：**谁是生产者块、谁是消费者块？它们在 IR 嵌套树的哪一层「对齐」？** 第二个问题用 **最低公共祖先（LCA）** 算法解决。

### 4.4.2 核心流程（含 LCA 原理）

`SplitDataflow` 编排 7 个子步骤：

```
SplitDataflow.runOnOperation(module):
    Step1 AddBlockIdForControlOps      # 给控制流 op 补 block_id
    Step2 DataDependencyAnalysis       # ★ 分析三类跨核依赖
    Step3 InterCoreTransferAndSync     # 插入跨核搬运与同步
    Step4 MarkMainLoop                 # 标记主计算循环
    Step5 SeparateCVScope              # 把 CV 分离进各自 scope
    Step6 PreserveControlAttrsCanonicalize
    Step7 RefineArgsBlockId            # 细化主循环迭代变量的 block_id
```

`DataDependencyAnalysis` 的核心是「构造块信息表 + 找跨核依赖」：

```
runOnOperation(module):
    info = getAnalysis<DataDependencyInfo>()
    createBlockInfoMap(info)        # 1. 按 block_id 聚合出 BlockInfo（输入/输出）
    processIterArgDependencies()    # 2. 处理 scf.for 迭代变量的跨核携带依赖
    analyzeExternalInputs(info)     # 3. V->C：Cube 块的外部输入若来自 VECTOR 块
    analyzeExternalOutputs(info)    # 4. C->V：Cube 块的结果若被 VECTOR 块使用
    analyzeMemoryEffect(info)       # 5. 内存依赖：用 MemoryDependenceGraph 找跨核写读
    info.setValid(true)
```

**LCA 原理**：生产者块和消费者块可能位于不同的 `scf.for`/`scf.if` 嵌套层。要插入同步，必须在二者**共同的最近祖先作用域**里操作。`findCommonLevelBlockIds` 把 MLIR 的 op 嵌套关系当作一棵树：

- 收集生产者 op 的全部祖先链（`getParentOp()` 一路向上）。
- 沿消费者的祖先链向上走，找到第一个也出现在生产者祖先链里的 op，即 **LCA**。
- 返回 LCA 正下方的「生产者侧子块 id」与「消费者侧子块 id」，这就是要对齐的层级。若找不到公共祖先，返回 `{-1,-1}` 触发回退。

### 4.4.3 源码精读

`SplitDataflow` 的 7 步编排——[SplitDataflow.cpp:L44-L84](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/lib/DynamicCVPipeline/SplitDataflow.cpp#L44-L84)。

`DataDependencyAnalysis::runOnOperation` 的 5 步——[DataDependencyAnalysis.cpp:L830-L865](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/lib/DynamicCVPipeline/SplitDataflow/DataDependencyAnalysis.cpp#L830-L865)。

构造块信息表 `createBlockInfoMap`：遍历模块、按 `block_id` 把连续 op 聚成一个 `BlockInfo`，并计算每个块的「外部输入」（操作数的定义 op 不在本块内）与「外部输出」（结果被本块外的 op 使用）——[DataDependencyAnalysis.cpp:L239-L261](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/lib/DynamicCVPipeline/SplitDataflow/DataDependencyAnalysis.cpp#L239-L261) 与 `collectBlockInfo` 的输入/输出判定 [DataDependencyAnalysis.cpp:L182-L236](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/lib/DynamicCVPipeline/SplitDataflow/DataDependencyAnalysis.cpp#L182-L236)。

V→C 依赖分析 `analyzeExternalInputs`：对每个 Cube 块的外部输入，若其定义 op 是 VECTOR 核类型，则记录一条 `VectorToCube` 依赖——[DataDependencyAnalysis.cpp:L495-L548](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/lib/DynamicCVPipeline/SplitDataflow/DataDependencyAnalysis.cpp#L495-L548)。

C→V 依赖分析 `analyzeExternalOutputs`：对每个 Cube 块的外部输出，遍历其用户；若用户是 VECTOR 核类型，记录一条 `CubeToVector` 依赖（并特判「全部经 transpose 进入 vector」可在 fixpipe 内完成的情况）——[DataDependencyAnalysis.cpp:L551-L628](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/lib/DynamicCVPipeline/SplitDataflow/DataDependencyAnalysis.cpp#L551-L628)。

内存依赖分析 `analyzeMemoryEffect`：借助 `MemoryDependenceGraph`（基于 `AliasAnalysis`）找出「在本 op 之前执行、且与本 op 有真实内存依赖」的前驱 op；若前驱与当前 op 核类型不同，则记录一条内存跨核依赖——[DataDependencyAnalysis.cpp:L653-L748](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/lib/DynamicCVPipeline/SplitDataflow/DataDependencyAnalysis.cpp#L653-L748)。

LCA 实现 `findCommonLevelBlockIds`：收集生产者祖先链、沿消费者祖先向上找公共祖先、返回 LCA 下一层的块 id；找不到返回 `{-1,-1}`——[DataDependencyAnalysis.cpp:L751-L828](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/lib/DynamicCVPipeline/SplitDataflow/DataDependencyAnalysis.cpp#L751-L828)。

迭代变量跨核携带依赖 `processIterArgDependencies`：处理 `scf.for` 的 iter_args 在循环间携带的跨核数据流（本轮重构的重点之一，见下文「更新说明」），若 init 与 yield 核类型不一致且无法处理则回退——[DataDependencyAnalysis.cpp:L421-L492](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/lib/DynamicCVPipeline/SplitDataflow/DataDependencyAnalysis.cpp#L421-L492)。

### 4.4.4 代码实践

**实践目标**：用一个「V→C 依赖且不对齐」的 kernel，确认跨核依赖被识别并最终生成 `scope`。

**操作步骤**：

1. 直接阅读/运行 `unittest/DynamicCVPipeline_ut/test_sdf01_mlir_v2c_unaligned.py`：它的 kernel 让 Vector 先算出一个小张量（N=3，不满足 32B 对齐），再喂给 Cube 的 matmul，构造出一条 V→C 依赖。
2. 关注其断言：`assert "scope" in mlir`。

**需要观察的现象**：编译产出的 MLIR 中**包含** `scope` 关键字，说明 SplitDataflow 成功识别了 V→C 跨核依赖，并由 `SeparateCVScope` 生成了独立作用域。

**预期结果**：SDF01 这类「有跨核依赖」的场景测试断言 `scope` **存在**；与 4.5 中 PCB01「无跨核依赖」断言 `scope` **不存在**形成对照。运行结果**待本地验证**。

### 4.4.5 小练习与答案

**练习 1**：为什么 `analyzeExternalInputs` 里，当输入的定义 op 也是 CUBE 核类型时直接 `continue`（L528-L530）？

**参考答案**：生产者和消费者都是 CUBE，这是 C→C 同核依赖，不属于「跨核」范畴，不需要插入跨核搬运/同步，留给后续普通的块内处理即可。本 pass 只关心核类型**不同**的生产-消费对。

**练习 2**：`findCommonLevelBlockIds` 返回 `{-1,-1}` 时会发生什么？

**参考答案**：调用方（如 `collectDepInfo`、`analyzeMemoryEffect`）检测到 `-1` 后会调用 `setFallbackAttr(module, ERRCODE_FAILED)`，最终触发顶层 pass 的回退，把模块恢复成备份。即「找不到共同层级就无法安全插同步，宁可不做」。

---

## 4.5 AnalyzeDataFlow 与 DynamicCVPipeline_ut 测试套件

### 4.5.1 概念说明

`AnalyzeDataFlow`（注意与 4.4 的 `DataDependencyAnalysis` 不同）位于 SplitDataflow **之后**，负责把后续 `AllocMultiCache`/`AddControlFlowCondition` 等步骤需要的信息**预先采集并标注到 IR 上**。它由 5 个更细的子 pass 组成：

- `AnalyzeName`：为需要跨核传递的中间值生成统一名称/标识。
- `AnalyzeScope`：分析各计算块所属的 `scope` 作用域层级。
- `AnalyzeArgs`：分析跨核传递所需的参数（如搬运源/目的）。
- `AnalyzeFlag`：分析同步标志（flag）的 id 与复用（配合 `FlagIdReuse`）。
- `AnalyzeCubeContolFLowInputChain`：分析 Cube 控制流的输入链路。

本模块同时介绍**新增的 `DynamicCVPipeline_ut` 手写 MLIR 测试套件**——它是理解上述各 pass 行为的最佳「参考样例」。这套测试在本轮迭代中随 commit `72e8c3ba6`（"Add DynamicCVPipeline handwritten MLIR testcases"）大量新增，采用一种**「编译出 MLIR → 断言关键字是否存在」**的验证模式：把每个场景的 Triton kernel 编译到 Linalg MLIR（`enable_dynamic_cv_pipeline=True`），再用字符串断言判定 `scope` 是否出现。

测试按考察的子 pass 分三类，文件名前缀即所属：

| 前缀 | 考察的子 pass | 典型场景 |
| --- | --- | --- |
| `acf*` | AddControlFlow | if/else 条件分支下的 CV 转换 |
| `pcb*` | PlanComputeBlock | 单/多 Cube 与 Vector 的块规划 |
| `sdf*` | SplitDataflow | V→C/C→V/内存依赖、多层嵌套循环 |

### 4.5.2 核心流程

```
AnalyzeDataFlow.runOnOperation(module):
    pm.add(AnalyzeName)
    pm.add(AnalyzeScope)
    pm.add(AnalyzeArgs)
    pm.add(AnalyzeFlag)
    pm.add(AnalyzeCubeContolFLowInputChain)
    runPipeline(pm, module)
```

测试套件的运行模式（CI 视角）：

```
对每个 test_*.py:
    pytest 编译 kernel -> 生成 mlir_output/*.mlir
分别在 PR 包与 base 包上跑一遍，得到两份 MLIR
用 mlir_diff.py 对比两份 MLIR，确保变换结果稳定/符合预期
```

### 4.5.3 源码精读

`AnalyzeDataFlow` 的 5 个子 pass 编排——[AnalyzeDataFlow.cpp:L35-L64](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/lib/DynamicCVPipeline/AnalyzeDataFlow.cpp#L35-L64)。

**PCB01 参考样例**——「单个独立 Cube + 单个独立 Vector，二者无数据依赖」。其 `compile_kernel` 助手展示了「手工驱动编译到 Linalg」的标准写法：构造 `ASTSource` → `ast_to_ttir` → `make_ttir` → `ttir_to_linalg`，全程 `enable_dynamic_cv_pipeline=True`——[test_pcb01.py:L62-L103](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/unittest/DynamicCVPipeline_ut/test_pcb01_mlir_single_cube_single_vector.py#L62-L103)。

PCB01 的 kernel：`for k in range(K)` 内含 `tl.dot`（Cube）与 `c + d`（Vector），但两者**互不依赖**——[test_pcb01.py:L138-L157](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/unittest/DynamicCVPipeline_ut/test_pcb01_mlir_single_cube_single_vector.py#L138-L157)。

PCB01 的关键断言：`assert "scope" not in mlir`——因为 Cube 与 Vector 之间**没有跨核数据依赖**，SplitDataflow 无需（也不会）生成 CV 分离作用域，故 `scope` 不出现。这是「无依赖 → 不生成 scope」的基准样例——[test_pcb01.py:L261-L262](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/unittest/DynamicCVPipeline_ut/test_pcb01_mlir_single_cube_single_vector.py#L261-L262)。

**SDF01 对照样例**——「V→C 依赖、不满足 32B 对齐（N=3）」。它构造了一条真实的 Vector→Cube 跨核依赖，因此断言相反：`assert "scope" in mlir`——[test_sdf01.py:L214-L217](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/unittest/DynamicCVPipeline_ut/test_sdf01_mlir_v2c_unaligned.py#L214-L217)。

CI 脚本如何批量跑测试并对比 MLIR：`run_test_cases` 用 pytest 跑每个 `test_*.py`，`compare_mlir` 调 `mlir_diff.py` 比对 base 与 PR 两份产物——[dynamic-cv-pipeline-ci.sh:L50-L78](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/unittest/DynamicCVPipeline_ut/dynamic-cv-pipeline-ci.sh#L50-L78) 与 [dynamic-cv-pipeline-ci.sh:L80-L109](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/unittest/DynamicCVPipeline_ut/dynamic-cv-pipeline-ci.sh#L80-L109)。

> **本轮更新说明（update 要点）**：本讲的「update」动作主要源于 `DynamicCVPipeline_ut` 测试套件的大量新增（`pcb*`/`sdf*`/`acf*` 共数十个手写 MLIR 用例），以及 `SplitDataflow`、`AddControlFlowCondition` 等子 pass 的重构。重构后 `DataDependencyAnalysis` 的 `processIterArgDependencies` 对 `scf.for` 迭代变量的跨核携带依赖处理更稳健（`insertProducerAndRecordDeps` 在循环体起始插入生产者块并记录 V→C/C→V 依赖），`findCommonLevelBlockIds` 的 LCA 定位仍是定位生产/消费块层级的核心。源码引用已全部刷新到当前 HEAD `0c3b1f6c` 的行号。

### 4.5.4 代码实践

**实践目标**：用测试套件验证「跨核依赖 → 生成 scope」与「无依赖 → 不生成 scope」这一判别规则。

**操作步骤**：

1. 在 950 环境下进入 `third_party/ascend/unittest/DynamicCVPipeline_ut/`。
2. 分别运行两个对照测试：
   - `pytest -sv test_pcb01_mlir_single_cube_single_vector.py`（无依赖）
   - `pytest -sv test_sdf01_mlir_v2c_unaligned.py`（V→C 依赖）
3. 查看各自在 `mlir_output/` 下生成的 `.mlir` 文件，搜索 `scope`。

**需要观察的现象**：PCB01 产物中 `scope` **不出现**；SDF01 产物中 `scope` **出现**。

**预期结果**：两测试均通过（断言成立）。这印证了「`scope` 是否生成」直接反映 SplitDataflow 是否识别到跨核依赖。运行结果**待本地验证**。

### 4.5.5 小练习与答案

**练习 1**：`AnalyzeDataFlow`（4.5）与 `DataDependencyAnalysis`（4.4）名字很像，职责区别是什么？

**参考答案**：`DataDependencyAnalysis` 是 SplitDataflow 内部的子 pass，负责**发现**跨核依赖（V→C/C→V/内存），产出 `DataDependencyInfo`；`AnalyzeDataFlow` 是更靠后的独立子 pass，负责在依赖已识别、块已规划之后，**采集**名称/作用域/参数/标志等元信息并标注到 IR，供后续多缓冲分配与控制流条件化使用。

**练习 2**：为什么测试用「`scope` 是否出现」而不是「逐 op 对比」来断言？

**参考答案**：逐 op 对比太脆弱——任何一个中间 pass 的微小改写（如 op 顺序、属性写法）都会让测试误报。`scope` 是「是否成功生成 CV 流水线」的稳定高层标志，既足以区分「走了 CV 流水线」与「回退」两条路径，又对内部实现细节鲁棒。CI 还会用 `mlir_diff.py` 在 base/PR 之间做更细的 MLIR 对比来捕捉非预期变化。

---

## 5. 综合实践

**任务**：手工构造一个 kernel，让它「从无跨核依赖」变成「有 V→C 跨核依赖」，并对照 `DynamicCVPipeline_ut` 测试观察 `scope` 由无到有的变化。

**步骤**：

1. **版本 A（无依赖）**：参照 PCB01，写一个 kernel，在同一个 `for` 循环里放一个 `tl.dot`（结果写入 out1）和一个独立的 `c + d`（写入 out2），二者不互通。编译后确认 MLIR **不含** `scope`。
2. **版本 B（制造 V→C 依赖）**：修改版本 B，让 Vector 的输出（如某个 `c + d` 的结果）作为 `tl.dot` 的一个输入（例如把它加到 matmul 的 bias/累加上，形成 V→C 数据流）。重新编译。
3. **对照**：确认版本 B 的 MLIR **含** `scope`；用 `MLIR_ENABLE_DUMP=1` 找到 SplitDataflow 阶段的 dump，定位 `DataDependencyAnalysis` 识别出的 V→C 依赖（debug 日志中 `Recorded ... dependency: VECTOR -> CUBE` 字样）。
4. **进阶**：再改成「Cube 结果喂给 Vector 做 reduce」（C→V 依赖），参照 `test_sdf*` 系列确认同样生成 `scope`。

**预期产出**：一份「同一 kernel 的无依赖/有依赖两版 MLIR diff」，标注出 `scope` 出现的位置，并用你自己的话解释 SplitDataflow 在其中识别出的跨核依赖类型与生产/消费者块。

> 说明：本实践需要 950（910_95）编译环境；若仅有源码，可改为「阅读型实践」——对照 `test_pcb01` 与 `test_sdf01` 的 kernel 源码，画出两者的 Cube/Vector 数据流图，标注依赖方向。

## 6. 本讲小结

- `DynamicCVPipeline` 是一条「纯增益」的编译期 IR 变换流水线：**先克隆备份、失败即回退**，仅在 950 平台默认开启，由 Python 侧 `enable_dynamic_cv_pipeline` 与模块回传的 `rc` 错误码协同控制。
- 它由 10 个子 pass 串成：`PreCheckAvailable`（预判）→ `StandardizeOp` → `PlanComputeBlock`（染色规划）→ `ComputeBlockOpt` → `SplitDataflow`（跨核依赖）→ `AnalyzeDataFlow`（信息采集）→ `AllocMultiCache` → `AddControlFlowCondition` → `SeparateMemoryFromCompute` → `RemoveSsbufAttr`。
- **PlanComputeBlock** 用「图染色 + BFS」给每个 op 标 `CUBE`/`VECTOR`：matmul 是 CUBE 种子，向上传播染朋友圈，剩余染 VECTOR，「红蓝都要」的共享 op 被克隆拆分，最后盖戳 `ssbuffer.core_type` 并分配 `ssbuffer.block_id`。
- **SplitDataflow** 的核心 `DataDependencyAnalysis` 识别三类跨核依赖（V→C、C→V、内存依赖），用 **LCA（最低公共祖先）算法** 在 IR 嵌套树上定位生产者/消费者块的对齐层级；找不到共同层级则触发回退。
- **AnalyzeDataFlow**（与依赖分析同名但不同物）在拆分后采集 name/scope/args/flag 等元信息，供多缓冲分配与控制流条件化使用。
- **DynamicCVPipeline_ut** 测试套件以「编译出 MLIR → 断言 `scope` 是否出现」为模式，按 `acf*`/`pcb*`/`sdf*` 覆盖三大子 pass；其中「无跨核依赖（PCB01）→ 无 scope」与「有 V→C 依赖（SDF01）→ 有 scope」是理解整条流水线行为的最重要对照样例。

## 7. 下一步学习建议

- **下一讲 u8-l3** 将承接本讲的「分析与规划」结果，精读 `SeparateMemoryFromCompute`（重写版，含 `MarkGMLoadPass` 访存/计算解耦）、`AllocMultiCache`/`multibuffer` 多缓冲与 `AutoBlockify`，看本讲规划出的计算块如何最终变成可重叠执行的 Cube/Vector 双流水线。
- 若你想看「跨核搬运与同步」具体怎么落到 IR，可继续阅读 `lib/DynamicCVPipeline/SplitDataflow/InterCoreTransferAndSync.cpp` 与 `MarkMainLoop.cpp`。
- 若对「染色」细节感兴趣，可精读 `OpClassifier.cpp` 的 `patternMatchCUBE`（L414）与 `propagateCubeUpstream`（L633），以及 `PlanCubeBlock.cpp` 的 `SeedRegionPlanner` 与环检测 `DependencyCycleDetector`。
- 想验证自己的理解，可仿照 `test_sdf*` 写一个新的多层嵌套循环 V→C 依赖 kernel，预测它是否会生成 `scope`，再编译验证。
