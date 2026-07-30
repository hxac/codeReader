# costmodel：编译期代价模型与 AscendModel 流水线

## 1. 本讲目标

学完本讲后，你应该能够：

1. 说清楚 **costmodel 是什么**：它在「不跑设备」的前提下，仅凭 TTIR 文本预测一段 kernel 的执行延迟（微秒），用来在自动调优里**预先筛掉慢配置**。
2. 理解 `run_costmodel` → `costmodel_bench` 这条 Python 入口的**进程内调用与缓存机制**。
3. 画出 **AscendModel MLIR 流水线**在 C++ 侧的真实 pass 顺序，以及「周期数 → 微秒」的换算口径。
4. 掌握 `HardwareConfig` 与 `ascend_910b.json` 硬件 schema 如何驱动代价估算，尤其是本轮「精度优化」（commit `9b3717c0f`）新引入的 tilesim 微架构表、标量开销与互斥单元模型。
5. 认识 `enable_costmodel_backend` 如何把代价模型接到 **compile-only autotune 路由**上。

> 本讲承接 [u9-l1](u9-l1-autotuner-and-auto-tiling.md)（autotuner 与自动 tiling）。u9-l1 讲的是「怎么生成候选配置并在真机上实测」，本讲讲的是「怎么不实测、用代价模型预测」，两者互补。

## 2. 前置知识

在进入本讲前，建议你已经了解以下概念（不必精通）：

- **TTIR**：Triton 的目标无关中间表示。本讲的代价模型**直接吃 TTIR 文本**，不经过 Linalg/BiSheng。若不清楚，先看 [u3-l1](u3-l1-jit-and-compile-entry.md) 与 [u3-l3](u3-l3-ttir-metadata-and-cache.md)。
- **Roofline 模型**：一种性能建模方法——把一次计算的耗时估计为「计算时间」与「访存时间」两者中较大的那个（二者可重叠时取 max，不可重叠时取 sum）。本讲会反复用到。
- **Cube / Vector 双计算单元**：昇腾 AI 核里 Cube（矩阵，做 `tl.dot`）与 Vector（向量）是两条物理流水线，各自有 load/compute/store 子单元。若不清楚，先看 [u8-l1](u8-l1-cube-vector-model-and-cv-fusion.md)。
- **`@triton.autotune`**：自动调优装饰器，会在多组配置里挑出最快的。先看 [u9-l1](u9-l1-autotuner-and-auto-tiling.md)。

> 关键直觉：autotune 的痛点是「每试一个配置都要编译 + 上板跑一次」，开销大。costmodel 把这一步**换成一个纯编译期的快速估算**，在配置很多时先做一轮预筛，再把少数候选送上板精测。

## 3. 本讲源码地图

本讲涉及的关键文件及其作用：

| 文件 | 作用 |
| --- | --- |
| `third_party/ascend/backend/runtime/costmodel_runtime.py` | Python 侧入口：`run_costmodel`、`costmodel_bench`、缓存键、并行求值、延迟解析。 |
| `third_party/ascend/triton_ascend.cc` | C++ pybind 桥：`run_costmodel_inproc` 绑定 + 真正的 MLIR pass 流水线装配 + 「周期→微秒」换算。 |
| `third_party/ascend/costmodel/lib/AscendModel/Transforms/EstimateCycles.cpp` | 逐算子周期估算 pass，roofline 统计的源头。 |
| `third_party/ascend/costmodel/lib/AscendModel/Transforms/PipelineAnalysisPass.cpp` | 路径级 roofline 汇总，引入互斥单元模型，产出最终 `scheduled_cycles`。 |
| `third_party/ascend/costmodel/include/AscendModel/Analysis/HardwareConfig.h` | 硬件参数类：加载/查询 JSON 硬件 schema，含本轮新增的 tilesim 微架构表。 |
| `third_party/ascend/costmodel/configs/ascend_910b.json` | Ascend 910B 硬件 schema 配置文件（本轮精度优化大幅扩充）。 |
| `third_party/ascend/backend/compiler.py` | `enable_costmodel_backend` 选项定义与 autotune 路由处理。 |
| `docs/en/examples/09_costmodel_example.md` | 端到端 costmodel 调用示例（本讲代码实践的依据）。 |
| `third_party/ascend/unittest/costmodel_ut/test_compiler_costmodel_contract.py` | 验证 `enable_costmodel_backend` 与 `use_bytecode` 耦合关系的契约测试。 |

---

## 4. 核心概念与源码讲解

### 4.1 costmodel 是什么：编译期延迟预测器（run_costmodel + costmodel_bench）

#### 4.1.1 概念说明

costmodel（代价模型）是一个**编译期延迟预测器**：输入一段 TTIR 文本，输出一个「预估执行时间（微秒）」数字，全程不触碰 NPU 硬件。它的价值在于**自动调优的预筛**——当候选配置很多（比如 `configs=[]` 自动生成几十上百个 tiling）时，逐个上板实测代价过高，先用 costmodel 快速排个序、砍掉明显慢的，再对少数头部候选做精测，能大幅节省调优时间。

Python 侧有两层 API：

- **`run_costmodel(ttir_or_path, extra_args)`**：底层单次调用，吃一段 TTIR，返回 C++ 进程内评估出的原始输出字符串。
- **`costmodel_bench(config_ttir_items)`**：面向 autotune 的高层批量 API，吃「多组候选配置 + 各自 TTIR」的列表，返回 `{config: 延迟}` 字典，并内置缓存与并行。

#### 4.1.2 核心流程

`costmodel_bench` 的处理流程：

```
config_ttir_items (多组: {config, ttir, arg_bindings, hardware_config})
        │
        ▼
_normalize_costmodel_items: 过滤无效项, 拆出 pending_items
        │
        ▼
_evaluate_pending_items: 按 jobs 串行 or 线程池并行
        │   每个 item:
        │     extra_args = _build_costmodel_extra_args(arg_bindings, hardware_config)
        │     cache_key  = make_costmodel_cache_key(ttir, extra_args)
        │     若缓存命中 -> 直接返回延迟
        │     否则 run_costmodel(ttir, extra_args) -> parse_latency
        │     store_costmodel_latency(cache_key, latency)
        ▼
{config: latency_us}  (失败/缺 TTIR 的项记为 inf)
```

其中 `run_costmodel` 把 TTIR 文本连同命令行参数交给 C++ 的进程内桥 `run_costmodel_inproc`。

#### 4.1.3 源码精读

`run_costmodel` 读入 TTIR（支持文件路径或内联文本），调用 C++ 进程内函数，失败时返回 `None`：

[run_costmodel（costmodel_runtime.py:33-52）](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/backend/runtime/costmodel_runtime.py#L33-L52) —— 这里 `from triton._C.libtriton import ascend as ascend_capi` 拿到 C++ 桥，再 `ascend_capi.run_costmodel_inproc(mlir_text, args)` 真正执行估算。

高层 API `costmodel_bench` 对每个候选做「先查缓存、未命中再算、算完存缓存」的闭环，并把失败项兜底为 `float("inf")`（保证最差配置也能进字典，不会被静默丢弃）：

[costmodel_bench（costmodel_runtime.py:200-240）](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/backend/runtime/costmodel_runtime.py#L200-L240)

单个候选的求值逻辑在 `_eval_one_costmodel_item`：它组装命令行参数、算缓存键、查缓存，未命中则调用 `run_costmodel` 并用 `parse_latency` 解析延迟：

[_eval_one_costmodel_item（costmodel_runtime.py:165-176）](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/backend/runtime/costmodel_runtime.py#L165-L176)

延迟解析依赖 C++ 输出里的固定字符串 `Estimated Time: ... us`：

[parse_latency（costmodel_runtime.py:108-112）](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/backend/runtime/costmodel_runtime.py#L108-L112) —— 用正则 `Estimated Time:\s+([0-9.]+)\s*us` 抠出数字；匹配不到就返回 `inf`。

命令行参数由 `_build_costmodel_extra_args` 拼装，基础开关固定是 `-ascend-perf-model`，再追加运行期绑定或硬件 schema 路径：

[_build_costmodel_extra_args（costmodel_runtime.py:132-141）](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/backend/runtime/costmodel_runtime.py#L132-L141) —— 注意当前进程内解析器在 `-ascend-perf-model` 后只消费一个 payload token，所以「arg 绑定」与「hardware 配置」二选一转发；默认硬件配置由 `_resolve_default_hardware_config` 定位到 `ascend_910b.json`（[costmodel_runtime.py:119-129](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/backend/runtime/costmodel_runtime.py#L119-L129)）。

> 术语：**进程内（in-process）桥**——指不 fork 子进程、不在同一 Python 进程内通过 pybind 调 C++ 的方式；**arg 绑定（arg-bindings）**——把运行期具体数值（如 `n_elements`、`program_id`）静态喂给代价模型，让 `scf.for` 的循环次数能被求值。

#### 4.1.4 代码实践（源码阅读型）

> 本实践为「源码阅读型」，无需设备，可在任意环境完成。

1. **实践目标**：把 costmodel 的 Python 调用链走通到 C++ 桥。
2. **操作步骤**：
   - 打开 `costmodel_runtime.py`，从 `costmodel_bench`（L200）出发，沿 `_normalize_costmodel_items` → `_evaluate_pending_items` → `_eval_one_costmodel_item` → `run_costmodel` → `ascend_capi.run_costmodel_inproc` 画一条调用链。
   - 在 `triton_ascend.cc` 里找到 `run_costmodel_inproc` 的 pybind 绑定（L408），确认它释放 GIL（`py::gil_scoped_release`）后调用 `runAscendCostModelInProcess`。
3. **需要观察的现象**：解释为什么 `run_costmodel_inproc` 要释放 GIL（提示：多线程并行求值时，`_evaluate_pending_items` 用了线程池）。
4. **预期结果**：GIL 释放让多个候选的 C++ 估算能真正并行跑在多核上，而不是被 Python 全局锁串行化（见 `get_costmodel_jobs` 与 `_evaluate_pending_items`，[costmodel_runtime.py:55-67](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/backend/runtime/costmodel_runtime.py#L55-L67)、[179-197](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/backend/runtime/costmodel_runtime.py#L179-L197)）。

#### 4.1.5 小练习与答案

**练习 1**：`costmodel_bench` 的某个候选缺少 `ttir` 字段时，返回字典里它的延迟是多少？为什么这样设计？

> **答案**：`float("inf")`。设计上让「无法评估的配置」排到最后，而不是被静默丢弃，保证调用方拿到的字典覆盖全部 config 键（见 `_normalize_costmodel_items` 与 `costmodel_bench` 末尾的 `setdefault(..., float("inf"))`）。

**练习 2**：`parse_latency` 匹配不到 `Estimated Time` 时返回什么？这会影响排序结果吗？

> **答案**：返回 `float("inf")`。会——失败配置会排到末尾，不会被误判为最快。

---

### 4.2 AscendModel MLIR 流水线：从 TTIR 到 Estimated Time

#### 4.2.1 概念说明

C++ 侧的 `runAscendCostModelInProcess` 装配了一条**轻量 MLIR pass 流水线**，把 TTIR 逐步变换成带「周期数标注」的模块，再把模块级总周期换算成微秒。这条流水线**不经过 Linalg、不经过 BiSheng**，纯靠 AscendModel 方言里的算子周期估算，因此又快又稳，适合在 autotune 内循环里反复调用。

> 重要提示：本讲规格里给出的 pass 顺序是规划期的大致描述。**实际代码**（`triton_ascend.cc` 的 inproc 桥）顺序见下文源码精读，请以源码为准，不要照搬大纲字面。

#### 4.2.2 核心流程

实际 inproc 流水线（按 `pm.addPass` 顺序）：

```
TTIR 文本
  │
  ├─① createInlinerPass()       # 内联 tt.call 辅助函数 (如 softmax 的 reduce helper)
  ├─② createSymbolDCEPass()      # 删除内联后变死的私有 helper (防重复计数)
  ├─③ ConvertTritonToAscend      # tt.* -> AscendModel 方言算子
  ├─④ InsertDataTransfers        # 插入搬运算子 (CubeMTE2/FixPipe/VecMTE2/MTE3)
  ├─⑤ AssignOpIDs                # 给算子编号
  ├─⑥ EstimateCycles             # 逐算子估周期 + roofline 统计 (写 roofline/simple_sum)
  └─⑦ PipelineAnalysis           # 路径级 roofline + 互斥单元模型 (写 scheduled_cycles)
        │
        ▼
extractEstimatedTimeUs: scheduled_cycles / 1850  cycles/us  ->  "Estimated Time: X.XXX us"
```

**周期 → 微秒**换算用 910B 的时钟频率 1.85 GHz，即 1850 cycles/µs：

\[
\text{time\_us} = \frac{\text{scheduled\_cycles}}{1850}
\]

`extractEstimatedTimeUs` 按优先级取模块属性：先 `ascend.scheduled_cycles`，其次 `ascend.roofline_cycles`，再次 `ascend.simple_sum_cycles`，最后兜底遍历算子的 `estimated_cycles × loop_multiplier`。

#### 4.2.3 源码精读

inproc 桥的真实 pass 装配（这是本讲最重要的代码点，**请以这里为准**）：

[runAscendCostModelInProcess 的 pass 流水线（triton_ascend.cc:357-373）](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/triton_ascend.cc#L357-L373) —— 注意前两步 `createInlinerPass` + `createSymbolDCEPass` 是本轮精度优化新增的（见注释：不内联的话，循环内 helper 的算子会被当成「循环外」、拿到 `loopMultiplier=1`，导致每次迭代的归约算子严重低估；内联后 `SymbolDCE` 再删掉死掉的 helper 副本，避免与内联副本双重计数）。`EstimateCyclesPass` 与 `PipelineAnalysisPass` 都接收 `argBindingsStr` 与 `hardwareConfigPath` 两个选项。

周期换算逻辑：

[extractEstimatedTimeUs（triton_ascend.cc:283-314）](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/triton_ascend.cc#L283-L314) —— 优先 `scheduled_cycles`（PipelineAnalysis 产出的「含循环倍数」roofline），兜底链保证旧调用方兼容。

pybind 绑定入口（释放 GIL）：

[run_costmodel_inproc 绑定（triton_ascend.cc:408-415）](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/triton_ascend.cc#L408-L415)

`EstimateCyclesPass` 是逐算子估算的核心。它先给每个 `scf.for` 求循环次数（支持 override / 静态 / 用 arg 绑定求值），再遍历实现了 `EstimateCyclesOpInterface` 的算子、按硬件单元累加 roofline 统计：

[EstimateCyclesPass 第一遍——求循环次数（EstimateCycles.cpp:137-175）](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/costmodel/lib/AscendModel/Transforms/EstimateCycles.cpp#L137-L175) —— 把循环次数写为 `ascend.trip_count`、来源写为 `ascend.trip_count_source`（`override`/`static`/`evaluated`）。

[EstimateCyclesPass 第二遍——逐算子估周期并累加 roofline（EstimateCycles.cpp:185-256）](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/costmodel/lib/AscendModel/Transforms/EstimateCycles.cpp#L185-L256) —— 关键点：`totalOpCycles = cycles * loopMultiplier`，即循环内的算子要乘以它所在各层循环的次数积；并给算子打 `estimated_cycles`、`hw_unit`、`bytes`、`flops`、`loop_multiplier` 标注，按 `HWUnit`（Cube/CubeMTE2/FixPipe/Vector/VecMTE2/MTE3）分桶累加。

逐算子 roofline 汇总公式（`RooflineStats::calculateRooflineCycles`）：

[RooflineStats（EstimateCycles.cpp:69-85）](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/costmodel/lib/AscendModel/Transforms/EstimateCycles.cpp#L69-L85) —— Cube 路径 \( \max(\text{Cube}, \text{CubeMTE2}, \text{FixPipe}) \)，Vector 路径 \( \max(\text{Vector}, \text{VecMTE2}, \text{MTE3}) \)，两路可重叠时取 \( \max \)。注意这是 EstimateCycles 内的初步统计；最终的路径级 roofline（含互斥单元修正）在 PipelineAnalysisPass 里重算。

PipelineAnalysisPass 的路径级 roofline（**本轮精度优化的关键改动之一**），引入「互斥单元」：910B 上 AIV 的 MTE2（向量 load）与 MTE3（向量 store）共用一条物理流水线，必须串行：

[PipelineAnalysisPass 路径 roofline 与互斥单元（PipelineAnalysisPass.cpp:190-234）](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/costmodel/lib/AscendModel/Transforms/PipelineAnalysisPass.cpp#L190-L234) —— 旧模型 Vector 路径是 \( \max(\text{Vector}, \text{VecMTE2}, \text{MTE3}) \)（假设 load/store 重叠）；新模型在 `areMutexUnits("vec_mte2", "mte3")` 为真时改为 \( \text{vecTransfer} = \text{VecMTE2} + \text{MTE3} \)（串行），Vector 路径 = \( \max(\text{Vector}, \text{vecTransfer}) \)；总周期 = \( \max(\text{cubePath}, \text{vectorPath}) \)。最终把「含循环倍数的 roofline」写为 `ascend.scheduled_cycles`（被 inproc 桥消费，[L229-231](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/costmodel/lib/AscendModel/Transforms/PipelineAnalysisPass.cpp#L229-L231)）。

> 术语：**HWUnit**——硬件单元枚举，Cube/CubeMTE2/FixPipe/Vector/VecMTE2/MTE3，对应 910B 的两条流水线各子段；**互斥单元（mutex units）**——共用物理流水线、不能并行的单元对，在 [4.3](#43-hardwareconfig-与-ascend_910bjson-硬件-schema) 的 `mutex_groups` 里配置。

#### 4.2.4 代码实践（源码阅读型）

1. **实践目标**：理解「为什么需要先内联 helper」这条精度补丁。
2. **操作步骤**：用 `git show 9b3717c0f -- third_party/ascend/triton_ascend.cc` 查看本轮提交对 `runAscendCostModelInProcess` 的改动，重点读新增的两行 `pm.addPass(createInlinerPass())` / `createSymbolDCEPass()` 上的注释。
3. **需要观察的现象**：设想一个 softmax kernel，它通过 `tt.call @max(...)` 调一个归约 helper，而这个调用发生在内层 `scf.for` 里。
4. **预期结果**：不内联时，helper 体内的归约算子处于 helper 函数顶层（循环外），`loopMultiplier=1`，每次迭代的归约开销被严重低估；内联后归约算子进入循环体，拿到正确的 `loopMultiplier`，估算精度提升。

#### 4.2.5 小练习与答案

**练习 1**：`extractEstimatedTimeUs` 为什么把 `ascend.scheduled_cycles` 排在 `ascend.roofline_cycles` 之前？

> **答案**：`scheduled_cycles` 是 PipelineAnalysisPass 产出的「含循环倍数」roofline，是当前最准确的公开汇总；`roofline_cycles` 是 EstimateCyclesPass 的初步统计。优先用更准的，旧的 `roofline_cycles` 作为兼容回退。

**练习 2**：这条 inproc 流水线里有 `HIVMAnalysisPass` 和 `PerfReportPass` 吗？

> **答案**：**没有**。`runAscendCostModelInProcess` 只装配了 Inliner、SymbolDCE、ConvertTritonToAscend、InsertDataTransfers、AssignOpIDs、EstimateCycles、PipelineAnalysis 这 7 个 pass。`PerfReportPass`（生成性能报告）与 `HIVMAnalysisPass` 是 AscendModel 库里独立注册的 pass，并不在进程内桥的实际链路中——这是容易被大纲字面描述误导的地方，**以源码为准**。

---

### 4.3 HardwareConfig 与 ascend_910b.json 硬件 schema

#### 4.3.1 概念说明

周期估算的精度完全取决于「硬件参数有多准」。`HardwareConfig` 是 C++ 侧的硬件参数类，负责加载 JSON schema 并回答两类问题：某个搬运（src→dst）在某个并发核数下的带宽是多少；某个向量指令在某个 dtype 下的 `{compute, head, interval}` 周期三元组是多少。

本轮「精度优化」（commit `9b3717c0f`）的核心动作就是把 **tilesim**（华为内部周期级模拟器）910B1 的实测微架构表迁移进 `ascend_910b.json`，并在 `HardwareConfig.h` 里新增对应的查询接口与 `BandwidthTable`/`VecCycleEntry`/`SmallPacketCoeffs`/`CubeModelConfig` 等数据结构。这使代价模型从「粗粒度 TFLOPS/带宽屋顶线」升级为「指令级 + 小包拟合 + 多核退化」的细粒度模型。

#### 4.3.2 核心流程

数据从 JSON 到估算的链路：

```
ascend_910b.json  ──loadFromFile──▶  HardwareConfig 对象
   (memory_spaces/compute_units/data_movers/                         │
    tilesim/cube_model/bandwidth_tables/vec_cycle_tables/            │
    calibration)                                                      │
                                                                     ▼
              loadHardwareConfigForAnalysis(path) 返回一次性配置
                                                                     │
                   ┌─────────────────────────────────────────────────┤
                   ▼                                                 ▼
   lookupBandwidth(src,dst,coreNum,pktBytes)        lookupVecCycle(intrinsic,elemBits)
   (含小包拟合 + 多核退化)                              ({compute,head,interval})
                   │                                                 │
                   └──────────────► estimateTransferCycles / 向量周期 ◀┘
```

几个本轮引入的关键建模点：

1. **多核带宽退化**：`bandwidth_tables.hbm:ub` / `ub:hbm` 带 `per_core_gbps`，即 1~48 核各自实测带宽——核越多、争用同一 off-chip 端口、单核带宽越低。
2. **小包拟合（small-packet fitting）**：搬运包小于阈值（默认 256B）时，用分桶拟合系数 \( \text{bw} = b / (a \cdot b + \text{pktBytes}/1024^3) \) 修正，避免「小包也按满带宽算」的高估。
3. **向量指令周期三元组**：`cycles = compute × repeats + startup`，其中 \( \text{repeats} = \lceil \text{numElems} \times \text{elemBytes} / 256 \rceil \)。
4. **标量开销与 pipe_barrier**：来自真实 `_attn_fwd` profiling 的标定值（见下文 calibration）。

#### 4.3.3 源码精读

`HardwareConfig` 类的公开查询接口（含本轮新增的 tilesim 迁移查询）：

[HardwareConfig 类与 tilesim 查询（HardwareConfig.h:182-377）](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/costmodel/include/AscendModel/Analysis/HardwareConfig.h#L182-L377) —— 重点接口：`lookupBandwidth`（[L288-289](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/costmodel/include/AscendModel/Analysis/HardwareConfig.h#L288-L289)，对应 tilesim `lookup_bw`，含小包拟合分支，未知 src/dst 退化为聚合 HBM 带宽以免报错）、`lookupVecCycle`（[L295-296](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/costmodel/include/AscendModel/Analysis/HardwareConfig.h#L295-L296)，对应 tilesim `lookup_vec_cycle`）、`estimateTransferCycles`（[L301-302](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/costmodel/include/AscendModel/Analysis/HardwareConfig.h#L301-L302)）、`areMutexUnits`（[L319](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/costmodel/include/AscendModel/Analysis/HardwareConfig.h#L319)，供 [4.2](#42-ascendmodel-mlir-流水线从-ttir-到-estimated-time) 的 PipelineAnalysisPass 判定 vec_mte2/mte3 是否串行）。标定派生量 `getAIVScalarOverheadFactor`（[L255](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/costmodel/include/AscendModel/Analysis/HardwareConfig.h#L255)，返回 3.74）与 `getPipeBarrierCyclesPerIter`（[L265](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/costmodel/include/AscendModel/Analysis/HardwareConfig.h#L265)，返回 7500）。

一次性加载入口（每次分析调用独立加载，避免污染全局）：

[loadHardwareConfigForAnalysis（HardwareConfig.h:395-396）](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/costmodel/include/AscendModel/Analysis/HardwareConfig.h#L395-L396) —— `EstimateCyclesPass` 与 `PipelineAnalysisPass` 都用它，path 为空时返回默认 910B 配置。

JSON schema 里本轮新增/扩充的关键段（**这些就是影响精度的关键字段**）：

- **标量开销标定（scalar_overhead）**：[ascend_910b.json:286-292](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/costmodel/configs/ascend_910b.json#L286-L292) —— `aiv_scalar_overhead_factor: 3.74`，含义是总 AIV 时间 ≈ 纯向量周期 × 4.74（即 \( \text{total} = \text{vec} \times (1 + 3.74) \)）。注释明确：标量开销覆盖循环控制、指针算术、AIC↔AIV 间的 `pipe_barrier` 同步，是「旧模型完全缺失」的部分。

- **pipe_barrier 标定**：[ascend_910b.json:300-303](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/costmodel/configs/ascend_910b.json#L300-L303) —— `cycles_per_inner_iteration: 7500`，由 BM=64 单 wave 的 38.9% 空闲比例反推。

- **互斥单元组（tilesim.mutex_groups）**：[ascend_910b.json:330-338](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/costmodel/configs/ascend_910b.json#L330-L338) —— 910B 只有一组 `["vec_mte2", "mte3"]`，正是 [4.2](#42-ascendmodel-mlir-流水线从-ttir-到-estimated-time) 串行模型的依据。

- **多核退化带宽表（bandwidth_tables.hbm:ub）**：[ascend_910b.json:359-409](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/costmodel/configs/ascend_910b.json#L359-L409) —— 1 核 100.9 GB/s 一路退化到 48 核 33.7 GB/s，迁移自 tilesim `bandwidth_910B1.csv`。

- **向量指令周期三元组（vec_cycle_tables）**：[ascend_910b.json:507-520](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/costmodel/configs/ascend_910b.json#L507-L520) —— 以 `VADD` 为例，fp32 `{compute:2, head:13, interval:18}`；表头注释给出 `cycles = compute * repeats + startup, repeats = ceil(numElems*elemBytes/256)`。

- **Cube 微架构（cube_model）**：[ascend_910b.json:341-355](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/costmodel/configs/ascend_910b.json#L341-L355) —— `throughput:[16,32,16]`、`repeat_cycles`（fp32=2、fp16/bf16/int8=1）、`l0_tile_limit_kb:32`。

> 术语：**tilesim**——华为内部的周期级 NPU 模拟器，910B1 配置（`910B1.yaml` / `bandwidth_910B1.csv` / `vec_cycle_910B1.csv`）是这些表的源头；**roofline 屋顶线**——计算屋顶（TFLOPS）与访存屋顶（带宽）的交点（ridge point）决定算子是计算 bound 还是访存 bound。

#### 4.3.4 代码实践（源码阅读型）

1. **实践目标**：定位「影响精度的三个关键字段」，并解释各自的物理含义。
2. **操作步骤**：打开 `ascend_910b.json`，分别在 `calibration.scalar_overhead`、`tilesim.mutex_groups`、`bandwidth_tables.hbm:ub` 三处停留，记录其数值与注释里的标定来源（`_attn_fwd`、BM、核数）。
3. **需要观察的现象**：思考「如果把 `aiv_scalar_overhead_factor` 改回 0、把 `mutex_groups` 清空」，对纯向量 kernel 与含 load+store 的 kernel 估算分别有何影响。
4. **预期结果**：去掉标量开销 → AIV 时间被低估约 4.7 倍；去掉互斥组 → load/store 从串行变回重叠，含大量 load+store 的 kernel 延迟被低估。
5. 如果无法确定运行结果，明确标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`lookupBandwidth` 遇到未知的 `(src, dst)` 对时返回什么？为什么这样设计？

> **答案**：退化为聚合 HBM 带宽（`hbm:l2_agg`），让估算「优雅降级」而非报错。代价模型用于 autotune 预筛，宁可估得不准也不能让整条流水线失败。

**练习 2**：`vec_cycle_tables` 里 `VDIV` 的 fp32 `compute=4`、`VADD` 的 fp32 `compute=2`，说明什么？

> **答案**：除法比加减乘贵约一倍的 compute 周期。这让含大量除法的 kernel 在排序里被正确地往后推。

---

### 4.4 costmodel 缓存与多任务并行

#### 4.4.1 概念说明

autotune 会在「同一 kernel + 不同配置」上反复调 costmodel，而 TTIR 文本与命令行参数完全决定估算结果（确定性），因此**缓存**是性价比最高的优化。costmodel_runtime 实现了两级缓存：进程内字典（命中零开销）+ Triton 内容寻址文件缓存（跨进程复用）。此外，多候选可并行求值。

本轮精度优化还把缓存键的版本标签从旧值更新为 `inproc_costmodel_v2_loop_weighted`，**主动让旧缓存失效**——因为估算口径变了，旧结果不再可信。

#### 4.4.2 核心流程

```
(ttir 文本, extra_args)
        │
        ▼
make_costmodel_cache_key = sha256( ttir | extra_args | "inproc_costmodel_v2_loop_weighted" )
        │
        ▼
load_costmodel_latency:
   1) _COSTMODEL_MEM_CACHE[key]   # 进程内字典, O(1)
   2) get_cache_manager(namespace).get_file(key.json)  # 文件缓存
        │ 命中 -> 返回 latency
        │ 未命中
        ▼
run_costmodel -> parse_latency -> latency
        │
        ▼
store_costmodel_latency: 同时写进程内字典 + 文件缓存
```

文件缓存的命名空间由 `_costmodel_cache_namespace()` 生成，纳入 `triton_key`（Triton 版本指纹），使 Triton 升级后自动隔离。

并行度由 `get_costmodel_jobs` 决定：环境变量 `TRITON_COSTMODEL_WORKER_NUM` 优先，否则取 `os.cpu_count()`，上限是候选数。

#### 4.4.3 源码精读

缓存键构造（注意末尾的版本标签——本轮改动的 2 行之一就在这里）：

[make_costmodel_cache_key（costmodel_runtime.py:70-78）](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/backend/runtime/costmodel_runtime.py#L70-L78) —— `b"inproc_costmodel_v2_loop_weighted"` 是版本标签；估算口径一变就改这个字符串，令旧 `.json` 缓存键对不上而自动失效。

命名空间（纳入 Triton 版本）：

[_costmodel_cache_namespace（costmodel_runtime.py:18-27）](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/backend/runtime/costmodel_runtime.py#L18-L27)

两级读 / 写：

[load_costmodel_latency（costmodel_runtime.py:81-98）](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/backend/runtime/costmodel_runtime.py#L81-L98) —— 先查进程内字典 `_COSTMODEL_MEM_CACHE`，再查文件缓存，命中后回填进程内字典。

[store_costmodel_latency（costmodel_runtime.py:101-105）](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/backend/runtime/costmodel_runtime.py#L101-L105)

并行求值（候选多时开线程池，配合 [4.1](#41-costmodel-是什么编译期延迟预测器run_costmodel--costmodel_bench) 里 C++ 释放 GIL 才能真并行）：

[_evaluate_pending_items（costmodel_runtime.py:179-197）](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/backend/runtime/costmodel_runtime.py#L179-L197) —— `jobs <= 1` 串行，否则 `ThreadPoolExecutor`；单个 future 抛异常被吞掉（不影响其它候选）。

#### 4.4.4 代码实践（配置观察型）

1. **实践目标**：亲手让旧缓存失效并验证缓存命中。
2. **操作步骤**：
   - 设置 `TRITON_COSTMODEL_WORKER_NUM=1`（串行，便于观察），跑一次本讲 [4.5 综合实践](#5-综合实践) 的示例，记录耗时。
   - 立刻再跑一次，对比耗时（第二次应明显更快，因为进程内字典命中）。
   - 在 `make_costmodel_cache_key` 里把版本标签临时改一个字符，再跑，观察是否重新计算。
3. **需要观察的现象**：版本标签改动后，即便 TTIR 没变也会重新估算——这正是「精度优化后主动失效旧缓存」的机制。
4. **预期结果**：第二次运行命中 `_COSTMODEL_MEM_CACHE`，几乎零耗时；改版本标签后恢复为完整估算耗时。
5. 如果无法运行，标注「待本地验证」，但理解机制即可。

#### 4.4.5 小练习与答案

**练习 1**：为什么缓存键里要同时纳入 `ttir`、`extra_args` 和版本标签，三者缺一行不行？

> **答案**：`ttir` 区分不同 kernel/配置；`extra_args` 区分不同 arg 绑定（同一 TTIR 在不同 `n_elements` 下延迟不同）；版本标签区分估算口径——三者任一变化，结果都可能不同，必须全部进键。

**练习 2**：`_evaluate_pending_items` 里某个 future 抛异常会怎样？

> **答案**：被 `except Exception` 吞掉，不影响其它候选；该候选最终在 `costmodel_bench` 末尾被 `setdefault(..., inf)` 兜底为 `inf`。

---

## 5. 综合实践

本实践把 [4.1](#41-costmodel-是什么编译期延迟预测器run_costmodel--costmodel_bench)～[4.4](#44-costmodel-缓存与多任务并行) 串起来，对应规格里的实践任务：**用 costmodel 给多组配置打分，对比预测与实测，并指出影响精度的字段**。

### 实践目标

用一个 vector-add kernel，对 3 组 `BLOCK_SIZE` 配置：① 用 `costmodel_bench` 预测延迟并排序；② （若有 NPU 环境）上板实测同一批配置的最优解，对比 costmodel 是否选对；③ 在 `ascend_910b.json` 里指出影响精度的关键字段。并理解 `enable_costmodel_backend` 在 compile-only autotune 路由中的作用。

### 操作步骤

1. **直接复用官方示例**。把 `docs/en/examples/09_costmodel_example.md` 里的完整脚本存为 `costmodel_example.py` 并运行（该脚本用 `ASTSource + ast_to_ttir` 只生成 TTIR、不编译不启动，再调 `costmodel_bench`）：

   [端到端示例（09_costmodel_example.md:15-91）](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/docs/en/examples/09_costmodel_example.md#L15-L91) —— 注意它构造 `items` 时带了 `arg_bindings=f"arg3={n_elements},pid_x=0"`，把运行期数值喂给代价模型，让循环次数可求值（呼应 [4.2](#42-ascendmodel-mlir-流水线从-ttir-到-estimated-time) 的 arg 绑定）。

2. **观察预测排序**。脚本会按预测延迟升序打印三组配置（示例输出形如 `block256 < block1024 < block2048`）。

3. **（可选，需 NPU）对比实测**。把同样三组 `BLOCK_SIZE` 写进一个普通 Triton kernel 并用 `torch.npu.synchronize()` 计时实测，看 costmodel 选出的「最快预测」是否也是实测最快。若有偏差，结合 [4.3](#43-hardwareconfig-与-ascend_910bjson-硬件-schema) 的字段分析原因。

4. **理解 autotune 路由**。阅读 `enable_costmodel_backend` 选项如何让 compile-only 路径更轻：

   [enable_costmodel_backend 定义（compiler.py:1106）](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/backend/compiler.py#L1106) 与 [parse_options 里的路由处理（compiler.py:1230-1233）](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/backend/compiler.py#L1230-L1233) —— 开启后会 `use_bytecode=False`，跳过 BC↔MLIR 转换阶段，让「只编译不跑」的 autotune 路由更轻量稳定。

5. **契约测试佐证**。跑 `test_compiler_costmodel_contract.py`，确认 `enable_costmodel_backend=True` 时 `use_bytecode` 被强制为 `False`：

   [契约测试（test_compiler_costmodel_contract.py:124-134）](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/unittest/costmodel_ut/test_compiler_costmodel_contract.py#L124-L134)

### 需要观察的现象

- costmodel 对不同 `BLOCK_SIZE` 给出**单调合理**的延迟排序（数据量固定时，block 越大、单 program 工作越多但 program 数越少，二者权衡）。
- 第二次运行因缓存命中而明显加速。
- `enable_costmodel_backend` 改变了 `use_bytecode`，但**不改 IR**——它只是让编译路径更轻，真正「不跑设备」的预测由 `costmodel_bench` 单独完成。

### 预期结果

- 预测排序应与实测趋势一致；若 vector-add 是访存 bound，应能看到大 block 因启动开销摊薄而更优。
- 影响精度的三个关键字段：`calibration.scalar_overhead.aiv_scalar_overhead_factor`（标量开销，旧模型完全缺失）、`tilesim.mutex_groups`（load/store 串行）、`bandwidth_tables.hbm:ub` 的 `per_core_gbps`（多核退化）。
- 若无 NPU 环境，步骤 3 标注「待本地验证」，其余步骤在纯编译环境即可完成。

> 提示：`costmodel_bench` 是「面向 autotune 的高层 API」，但它**并不被 `autotuner.py` 直接硬编码调用**——它被设计成供外层调优逻辑（如本示例脚本）调用的公共接口，由调用方自行生成各配置的 TTIR、调 `costmodel_bench`、再按返回值排序筛选。`enable_costmodel_backend` 则是配套的「轻编译」开关。

---

## 6. 本讲小结

- **costmodel 是编译期延迟预测器**：吃 TTIR 文本、吐「Estimated Time: X us」，不碰设备，用于 autotune 预筛慢配置。
- **Python 两层 API**：`run_costmodel`（单次、走 C++ 进程内桥 `run_costmodel_inproc`）、`costmodel_bench`（批量、带缓存与并行）。
- **C++ 真实流水线**（以源码为准）：`Inliner → SymbolDCE → ConvertTritonToAscend → InsertDataTransfers → AssignOpIDs → EstimateCycles → PipelineAnalysis`，周期数按 `scheduled_cycles / 1850` 换算成微秒。
- **本轮精度优化的三个关键点**：① 先内联 helper 再估算（修正循环内算子的 `loopMultiplier` 低估）；② 引入互斥单元模型（vec_mte2 + mte3 串行）；③ 迁移 tilesim 实测表（多核退化带宽、向量指令三元组、标量开销、pipe_barrier）。
- **硬件 schema 驱动精度**：`ascend_910b.json` 的 `scalar_overhead`、`mutex_groups`、`bandwidth_tables`、`vec_cycle_tables`、`cube_model` 是影响估算精度的关键字段。
- **缓存与并行**：两级缓存（进程内字典 + 文件缓存），版本标签 `inproc_costmodel_v2_loop_weighted` 在口径变化时主动失效旧缓存；多候选经线程池并行（依赖 C++ 释放 GIL）。`enable_costmodel_backend` 是配套的「轻编译」路由开关（强制 `use_bytecode=False`）。

## 7. 下一步学习建议

- 想看「逐算子周期是怎么算出来的」：精读 `AscendModelOps.cpp` 里各算子对 `EstimateCyclesOpInterface` 的实现（`estimateCycles`/`getFlops`/`getTransferBytes`/`getHWUnit`），以及 `ConvertTritonToAscend.cpp` 如何把 `tt.dot/tt.load` 映射成 MatmulOp/CubeLoadOp。
- 想看「循环次数怎么静态求值」：精读 `AscendModel/Utils` 里的 `getScfForTripCountWithBindings` / `parseBindings`，理解 arg 绑定如何让依赖 `program_id` 的循环可求值。
- 想看代价模型如何接进完整 autotune：回到 [u9-l1](u9-l1-autotuner-and-auto-tiling.md) 与 [u9-l2](u9-l2-cv-autotune.md)，对比「上板实测」与「costmodel 预测」两条选配置路径的取舍。
- 想验证模型精度：参考 [u10-l2](u10-l2-profiling-and-pipeline.md) 用 msProf 板端 profiling，把实测周期与 `scheduled_cycles/1850` 对照，标定你自己的 workload。
- 建议继续阅读源码：`third_party/ascend/costmodel/lib/AscendModel/Analysis/RooflineAnalysis.cpp`、`PipelineAnalysis.cpp`、`HardwareConfig.cpp`。
