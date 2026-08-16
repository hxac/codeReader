# CostModel 性能模拟：从功能正确到性能可预估

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 CostModel 后端与 CPU 功能仿真后端（`__CPU_SIM`）的本质区别：一个回答「算得对不对」，一个回答「大约要多少周期」。
2. 掌握轻量 CostModel 的三级分发逻辑：逐元素公式、搬运带宽、矩阵乘解析式，以及它们各自的输入参数从哪里来。
3. 掌握 perf_sim 流水线仿真的四步流水：记录（recorder）→ 合并（merge）→ 仿真（pipe_model）→ 报告（reporter），并能读懂 `Pipeline` 表中各流水段的 busy cycles 占比。
4. 会用 `tests/run_costmodel.py`（或直接 cmake）对 `gemm_perf_sim` 用例跑一次性能模拟，并指出瓶颈流水段。
5. 理解 A5 VF 造价模型的「曲线键查表 + 公式求值」机制。

## 2. 前置知识

**CostModel 是什么？** 在 [u2-l4 统一入口 pto-inst.hpp 与多后端架构切换](u2-l4-pto-inst-entry.md) 中我们讲过，同一份 kernel 源码按编译宏路由到三个后端：`__CPU_SIM`（CPU 功能仿真）、`__CCE_AICORE__`（NPU 真机）、`__COSTMODEL`（性能模拟）。`__COSTMODEL` 后端同样在普通 C++ 进程里「真的执行」一遍 kernel（所以数据是对的），但每条 PTO 指令在执行的同时被**记录**下来，随后交给一个**流水线级仿真器**推演它在硬件上大约占多少周期。

**为什么需要它？** u10-l2 结尾的结论是「CPU 仿真只保证功能正确，同步与时序问题须真机验证」。真机验证成本高（要硬件、要刷包、要 profiling），而 CostModel 在笔记本上就能给出：

- kernel 总周期数（`Total cycles`）；
- 各流水段（Scalar / MTE2 / MTE1 / CUBE / FIXP / VEC / MTE3）的忙周期分布；
- Chrome Trace 格式的 JSON 时间线，可以直接判断流水线有没有重叠、有没有气泡。

它回答的是 **u6-l3 性能分析与优化方法论** 里「判定 Bound」这一步——在没有硬件时先做趋势分析和参数扫描。它是建模工具，**不替代**真机 profiling（文档原话：It does not replace hardware profiling）。

**两个关键术语：**

- **cycles（周期）**：硬件主频下的时钟拍数。本模型的主频常数是 1.85 GHz（见 [arch_config.hpp:24](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/costmodel/arch_config.hpp#L24)），周期与微秒的换算是 \[ \text{latency}_{\mu s} = \frac{\text{cycles}}{f_{\text{MHz}}} \]。
- **stub 与 fit 两个子后端**：仓库把 CostModel 分成 `st`（stub，指令级基线行为）与 `st_fit`（fit，公式化的延迟预测）两个测试套件，对应关系见 [docs/costmodel-backends.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/costmodel-backends.md)。本讲主线是 fit（轻量公式）+ perf_sim（流水线仿真）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [include/pto/costmodel/lightweight_costmodel.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/costmodel/lightweight_costmodel.hpp) | 轻量 CostModel 入口：`EstimateCycles` 按指令类别分发到公式/带宽/解析式 |
| [include/pto/costmodel/arch_config.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/costmodel/arch_config.hpp) | 架构参数：主频、`PipeKey` 枚举、A2/A3 带宽表、Hill 带宽模型 |
| [include/pto/costmodel/a2a3/formula_costmodel/formula_backend_compute.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/costmodel/a2a3/formula_costmodel/formula_backend_compute.hpp) | A2/A3 计算指令周期：逐元素线性公式 + TMATMUL 解析式 |
| [include/pto/costmodel/a2a3/formula_costmodel/formula_backend_transfer.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/costmodel/a2a3/formula_costmodel/formula_backend_transfer.hpp) | 搬运指令周期：指令 × tile 类型 → 数据通路 → 带宽查表 |
| [include/pto/costmodel/a2a3/formula_costmodel/formula_params.csv](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/costmodel/a2a3/formula_costmodel/formula_params.csv) | 真机 profiling 拟合出的 `(op, dtype, cols) → (slope, bias)` 参数表 |
| [include/pto/costmodel/perf_sim/launch.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/costmodel/perf_sim/launch.hpp) | `LAUNCH_KERNEL` 宏：`__COSTMODEL` 下替代 `<<<>>>` 启动语法 |
| [include/pto/costmodel/pto_instr.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/costmodel/pto_instr.hpp) | CostModel 版指令 API：`MAP_INSTR_IMPL` 展开为「执行 + 记录」 |
| [include/pto/costmodel/perf_sim/recorder.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/costmodel/perf_sim/recorder.hpp) | `PipeStage` 枚举、`InstrRecord`/`SyncRecord`、`PtoRecorder`/`SyncRecorder` |
| [include/pto/costmodel/perf_sim/latency.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/costmodel/perf_sim/latency.hpp) | 指令 → 流水段的静态/动态解析 |
| [include/pto/costmodel/perf_sim/pipe_model.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/costmodel/perf_sim/pipe_model.hpp) | `PipeEntry`/`PipeQueue`/`EventChannel` 数据结构 |
| [include/pto/costmodel/perf_sim/pipe_model_sim_impl.inl](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/costmodel/perf_sim/pipe_model_sim_impl.inl) | 事件驱动的单核/多核步进仿真核心 |
| [include/pto/costmodel/perf_sim/reporter_core_impl.inl](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/costmodel/perf_sim/reporter_core_impl.inl) | 报告生成：文本表、CSV、Chrome Trace JSON |
| [include/pto/costmodel/perf_sim/config.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/costmodel/perf_sim/config.hpp) | 仿真配置：`VEC_CORES_PER_AIC`、缓存参数、输出目录 |
| [include/pto/costmodel/a5/vf_costmodel.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/costmodel/a5/vf_costmodel.hpp) | A5 VF 造价模型：构造曲线键 → 查表 → 求值 |
| [include/pto/costmodel/a5/formula_costmodel/formula_backend_vf.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/costmodel/a5/formula_costmodel/formula_backend_vf.hpp) | VF 曲线键结构与四种拟合公式 |
| [tests/run_costmodel.py](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/run_costmodel.py) | CostModel 测试的构建/运行入口脚本 |
| [tests/costmodel/perf_sim_st/testcase/gemm_perf_sim/main.cpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/costmodel/perf_sim_st/testcase/gemm_perf_sim/main.cpp) | gemm 性能模拟用例（复用 gemm_performance kernel） |
| [docs/costmodel/perf-sim-user-guide.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/costmodel/perf-sim-user-guide.md) | 官方用户指南：新增用例的步骤与输出解读 |

## 4. 核心概念与源码讲解

本讲的三个最小模块：**轻量 CostModel**（单条指令周期怎么估）、**流水线建模**（整个 kernel 的周期怎么仿真）、**A5 VF 造价模型**（新一代架构换了一套估算方法）。

### 4.1 轻量 CostModel：一条指令的周期从哪里来

#### 4.1.1 概念说明

轻量 CostModel（`pto::mocker::lightweight` 命名空间）回答一个原子问题：**给定一条 PTO 指令 + 形状 + dtype，它在 A2/A3 上大约占多少 cycles？**

它不执行 kernel、不建模流水线重叠，只做单条指令的静态估算。核心思想是「分而治之」——指令天然分三类，每类一个数学模型：

| 指令类别 | 例子 | 模型 |
| --- | --- | --- |
| 逐元素/规约计算 | TADD、TEXP、TROWSUM… | 线性拟合公式 \( \text{cycles} = \text{slope} \cdot R \cdot C + \text{bias} \) |
| 数据搬运 | TLOAD、TSTORE、TMOV | 带宽模型 \( \text{latency}_{\mu s} = \frac{\text{bytes}}{\text{bw}} \)，再乘主频得 cycles |
| 矩阵乘 | TMATMUL | 硬件 repeat 计数的解析式 |

逐元素公式的 `(slope, bias)` 不是拍脑袋写的，而是从真机 profiling 数据拟合而来，存放在 CSV 里：[formula_params.csv:1-7](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/costmodel/a2a3/formula_costmodel/formula_params.csv#L1-L7)。例如 `TADDS,fp32,32,0.0156,25` 表示：fp32、列宽 32 时，每个元素 0.0156 cycle，固定开销 25 cycles。构建前由 [gen_formula_params_header.py](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/costmodel/a2a3/formula_costmodel/gen_formula_params_header.py) 把 CSV 生成 `formula_params_generated.hpp` 头文件（`st_fit`/`st_a5_fit` 套件需要此步骤，见 [run_costmodel.py:543-567](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/run_costmodel.py#L543-L567)）。

#### 4.1.2 核心流程

```
EstimateCycles(input)                      # 总分发
  ├─ arch == A5        → TryEstimateA5VfCycles     # 4.3 节，A5 全走 VF 曲线
  ├─ op ∈ {TLOAD,TSTORE,TMOV}
  │                    → TryEstimateTransferCycles # 字节 → 带宽 → 延迟 → 周期
  ├─ op == TMATMUL     → TryEstimateMatmulCycles   # 解析式
  └─ 其他              → TryEstimateElementwiseCycles # slope·R·C + bias
任何一步失败 → WarnAndFallbackToZero（打 WARN 日志，返回 0 cycles）
```

三个模型的数学形式：

逐元素（[formula_backend_compute.hpp:109-123](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/costmodel/a2a3/formula_costmodel/formula_backend_compute.hpp#L109-L123)）：

\[ \text{cycles} = \text{slope}(op, dtype, cols) \times R \times C + \text{bias}(op, dtype, cols) \]

矩阵乘（[formula_backend_compute.hpp:60-90](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/costmodel/a2a3/formula_costmodel/formula_backend_compute.hpp#L60-L90)）。Cube 单元按 16×16 的 M/N 小块、32 字节 K 分形推进，fp16 每个 repeat 1 cycle、fp32 2 cycles：

\[ \text{cycles} = 6 + c_{r} \times \left\lceil \frac{M}{16} \right\rceil \times \left\lceil \frac{K}{K_{\text{tile}}} \right\rceil \times \left\lceil \frac{N}{16} \right\rceil, \quad K_{\text{tile}} = \frac{32}{\text{sizeof}(A)} \]

搬运（[lightweight_costmodel.hpp:328-364](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/costmodel/lightweight_costmodel.hpp#L328-L364)）：

\[ \text{cycles} = \frac{\text{bytes}}{\text{bandwidth}} \times f_{\text{MHz}} \]

#### 4.1.3 源码精读

**① 输入输出结构。** 一切估算从 `CostModelInput` 开始——它把「哪条指令、什么形状、什么 dtype、什么 tile 类型」打包成纯值对象；`CostModelResult` 只有两个字段：周期和微秒。见 [lightweight_costmodel.hpp:102-145](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/costmodel/lightweight_costmodel.hpp#L102-L145)。这段代码定义了估算的完整输入空间：`op/dtype/rows/cols/k/tile_type/data_size/valid_rows/valid_cols/vf_impl_kind/a5_op_params`——后面 perf_sim 记录指令时会填的就是这些字段。

**② 总分发函数。** [lightweight_costmodel.hpp:429-442](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/costmodel/lightweight_costmodel.hpp#L429-L442) 是整个轻量模型的调度中心：A5 架构优先走 VF 曲线；否则按「搬运 → 矩阵乘 → 逐元素」的顺序分发。这段代码就是 4.1.2 流程图的直接翻译。

**③ 失败不撒谎。** [lightweight_costmodel.hpp:227-237](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/costmodel/lightweight_costmodel.hpp#L227-L237) 的 `WarnAndFallbackToZero` 在遇到不支持的组合（如 int8 的 TEXP）时打印 WARN 并返回 0，而不是给一个假数字。这个 0 会在 4.2 节被 `FallbackCycles` 兜底。

**④ 搬运通路的二维表。** [formula_backend_transfer.hpp:37-62](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/costmodel/a2a3/formula_costmodel/formula_backend_transfer.hpp#L37-L62) 用一张编译期二维表把「指令 × tile 类型」映射到 `PipeKey` 数据通路：例如 `TLoad × MatTile → GM_TO_L1`、`TMov × AccTile → L0C_TO_L1`、`TMov × LeftTile → L1_TO_L0A`。这与 u3-l1 讲的 TLOAD 落点语义（Vec→UB、Mat→L1）一一对应——**tile 类型决定搬运走哪条物理通路，通路决定用哪个带宽**。

**⑤ 带宽表与架构配置。** [arch_config.hpp:27-43](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/costmodel/arch_config.hpp#L27-L43) 定义了 14 个 `PipeKey`（VECTOR/CUBE 两个计算通路 + 12 条搬运通路），[arch_config.hpp:101-118](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/costmodel/arch_config.hpp#L101-L118) 给出 A2/A3 默认带宽表。注意：同一张表的数值在代码里有两种用法——平铺路径（`TransferBytesToUs` 直接做除法，量纲 bytes/us）与 Hill 饱和模型（[arch_config.hpp:194-237](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/costmodel/arch_config.hpp#L194-L237)，注释量纲 GiB/s，公式 \( \text{bw}(B) = \text{peak} \cdot \frac{B}{K+B} \)，小传输未达峰值带宽，可用环境变量 `PTO_BW_MODE=fitted` 启用）。两种量纲解释并存是历史遗留，具体标定以 st_fit 用例为准——**待确认**，读者引用数值前务必跑用例核对。

**⑥ 兜底公式。** 当轻量模型返回 0 时，perf_sim 侧还有一层按流水段分类的粗粒度兜底，见 [costmodel_provider.hpp:125-138](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/costmodel/perf_sim/costmodel_provider.hpp#L125-L138)：Matrix 段 `4 + elems/16`，GM 通路 `3 + 2·elems/64`，MTE1 `1 + elems/64`，其余 `2 + elems/32`。这保证任何指令都有周期可用，仿真不会因为缺参数而中断。

#### 4.1.4 代码实践

**实践目标**：亲手调用轻量 CostModel，验证三类模型的数值量级。

**操作步骤**（示例代码，非项目原有文件，可临时建在 `/tmp` 下编译）：

1. 写一个 20 行左右的 main，分别构造三个 `CostModelInput`：
   - 逐元素：`{op=TADD, dtype=Half, rows=128, cols=128}`；
   - 矩阵乘：`{op=TMATMUL, dtype=Half, rows=128, k=256, cols=128}`；
   - 搬运：`{op=TLOAD, dtype=Half, data_size=128*128, tile_type=MatTile}`。
2. 逐个调用 `pto::mocker::lightweight::EstimateCycles(input)`，打印 `cycles` 与 `latency_us`。
3. 编译命令（需先生成公式参数头，见步骤 4）：`g++ -std=c++20 -I include -I include/pto/costmodel/a2a3/formula_costmodel main.cpp`。

**需要观察的现象**：

- TADD(128×128, fp16) 的 cycles 应接近 `slope × 16384 + bias`，量级在数千 cycles；
- TMATMUL(128×256×128, fp16) 用解析式手算一遍：\(6 + 1 \times 8 \times 16 \times 8 = 1030\) cycles，与程序输出对照；
- TLOAD 的 latency_us = bytes / GM_TO_L1 带宽。

**预期结果**：三组数值都能打印出来，且矩阵乘与手算解析式一致（fp16 路径 `kCyclePerRepeat=1`）。若输出 0 并伴随 `[WARN] lightweight::EstimateCycles fallback`，说明撞上了不支持的组合——检查 dtype 是否在支持列表（fp32/fp16，见 [lightweight_costmodel.hpp:410-419](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/costmodel/lightweight_costmodel.hpp#L410-L419)）。具体数值**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么逐元素公式要按 `(op, dtype, cols)` 三元组存参数，而不是一条指令一组参数？

**答案**：因为不同 dtype 的向量通路吞吐不同（fp32 每 repeat 处理 64 元素、fp16 128 元素），不同列宽触发的 repeat 数和尾块处理也不同。CSV 按 cols 分行存 slope/bias，能让拟合误差在不同形状区间内各自最小；查表逻辑见 [formula_backend_compute.hpp:92-107](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/costmodel/a2a3/formula_costmodel/formula_backend_compute.hpp#L92-L107)，`TryEstimateFormulaCyclesAnyDType` 还带逐级降级（精确 cols → 通配 cols → 通配 dtype）。

**练习 2**：`TryEstimateMatmulCycles` 里为什么 \(K_{\text{tile}} = 32/\text{sizeof}(A)\)？

**答案**：Cube 单元的 K 方向按 32 字节分形（fractal）为单位喂数据（u2-l2 讲过的 32B 分形约束）。fp16 占 2 字节，则每分形 16 个 K 元素；fp32 占 4 字节，每分形 8 个。所以 fp32 的 repeat 数是 fp16 的两倍以上（还要乘 `kCyclePerRepeat=2`），总周期约为 4 倍——这就是混合精度用 fp16 存 A/B 的收益来源之一。

**练习 3**：`WarnAndFallbackToZero` 为什么选择打印 WARN 而不是抛异常？

**答案**：CostModel 是估算工具，单条指令缺参数不应让整个 kernel 的仿真崩溃。返回 0 后由 [costmodel_provider.hpp:142-149](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/costmodel/perf_sim/costmodel_provider.hpp#L142-L149) 的 `EstimateInstrCycles` 用 `FallbackCycles` 兜底，保证流水线仿真可以继续；WARN 日志提示开发者该指令的精度可能不足。

### 4.2 流水线建模：perf_sim 的记录—合并—仿真—报告

#### 4.2.1 概念说明

单条指令的周期知道了，但 kernel 的总周期不等于把所有指令周期加起来——u2-l3 讲过 MTE2/V/MTE3/Cube 多条流水线**并行执行**，总时长取决于关键路径与依赖关系。perf_sim 就是把这些并行结构仿出来：

1. **记录（recorder）**：kernel 在 `__COSTMODEL` 编译下真实执行一遍，每条指令、每次 set/wait 事件、每个 TPipe token 都被追加分条记录；
2. **合并（merge）**：把指令流与同步流按全局序号归并成 `MergedEntry`，把显式同步内联成队列里的 SIGNAL/WAIT 条目；
3. **仿真（pipe_model）**：8 条流水线队列 + 计数器事件通道，事件驱动地步进推演每条指令的开始/结束周期；
4. **报告（reporter）**：汇总各流水段 busy cycles，输出文本表 + CSV + Chrome Trace JSON。

三个关键设计决策值得注意：

- **measured 与 estimated 双轨周期**：记录时既存「stub 执行实测周期」（trace.hpp 里逐条 CCE 调用累加），又存「公式估算周期」，实测为 0 或特定指令（TROWEXPAND、int16/int32 的 TDIVS）时用估算替代；
- **事件通道用计数器不用布尔**：同一 `(dst_pipe, event_id)` 对在第 N 轮循环的 signal 可能要跨迭代累积，布尔会丢信号；
- **物理核与逻辑核分离**：一个物理 AIC 带 2 个 AIV（`VEC_CORES_PER_AIC = 2`），记录按逻辑核存，仿真按物理核合并。

#### 4.2.2 核心流程

`LAUNCH_KERNEL` 宏是整个 perf_sim 的发动机（伪代码）：

```
LAUNCH_KERNEL(func, targs, (block_dim, l2_ptr, stream)):
    清空 PtoRecorder / SyncRecorder / CvSyncRecorder / TileDepTracker
    SetLaunchConfig(block_dim, l2_ptr, stream)      # l2_ptr != nullptr 时启用 L2 模型
    for core in 0..block_dim:                        # 物理核
        for sub in 0..VEC_CORES_PER_AIC:             # 逻辑核（AIV 子块）
            current_subblock_id = sub
            PtoRecorder::SetActiveCore(core*2 + sub)
            ScopedExecutionContext(core, sub)         # CPU 仿真上下文扮演该核
            func targs(...)                           # ★ 真实执行 kernel，逐指令记录
    report = PerfSimReporter().Run(#func)            # 合并 → 仿真
    PrintText(report)                                 # 终端报告
    WriteSwimlaneJson / WritePipelineSummaryCSV       # perf_sim_output/ 下两个产物
```

每条指令进入 `MAP_INSTR_IMPL` 宏（[pto_instr.hpp:209-216](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/costmodel/pto_instr.hpp#L209-L216)）时做四件事：`PtoInstrScope` 开始计时 → `API##_IMPL` 执行（CPU 语义）→ `InjectTileCycles` 把累计周期写回 tile → `RecordInstr` 落记录。`RecordInstr`（[pto_instr.hpp:108-148](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/costmodel/pto_instr.hpp#L108-L148)）内部依次：解析流水段 → `TileDepTracker::TrackByAddr` 按地址追查数据依赖生成 wait 事件 → 提取形状 dtype → 双轨取周期。

仿真主循环 `StepSimulate`（事件驱动，不用固定时间片）：

```
每一步:
  1. 完成检查：所有队列里 end_cycle ≤ now 的条目出队，向事件通道发 signal
  2. 弹出已完成条目
  3. 派发尝试：每条队列队首若 IDLE 且 wait 事件全部 signaled → 启动
     （MTE 条目启动前先过 L2CacheModel::Access 调整 duration）
  4. 无任何活动 → 结束；有活动但未派发 → now 跳到最近的 end_cycle
死锁/漏等待：剩余条目标记 stuck=true 进入报告（CollectStuckEntries）
```

#### 4.2.3 源码精读

**① 八条流水段的定义。** [recorder.hpp:37-47](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/costmodel/perf_sim/recorder.hpp#L37-L47) 的 `PipeStage` 枚举把 NPU 流水线拆成 8 条队列：`Scalar / MTE2_AIV / MTE2_AIC / MTE3 / MTE1 / Vector / Fixpipe / Matrix`。相比 u2-l3 的三流水线心智模型（MTE2/V/MTE3），这里多了 `MTE1`（片上 L1↔L0 搬运）、`Fixpipe`（L0C 写出）、`Matrix`（Cube），并把 MTE2 拆成 AIC 侧（搬向 L1）与 AIV 侧（搬向 UB）两个队列——正对应 [recorder.hpp:60-68](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/costmodel/perf_sim/recorder.hpp#L60-L68) 的 `IsAICStage/IsAIVStage` 归属划分。

**② 指令到流水段的路由。** [latency.hpp:78-112](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/costmodel/perf_sim/latency.hpp#L78-L112) 的 `ResolvePipeStage` 是动态路由：TLOAD 看 **dst tile**（Vec→MTE2_AIV，否则 MTE2_AIC）；TSTORE 看 **src tile**（Acc→Fixpipe，否则 MTE3）；TMOV/TEXTRACT 看 **src+dst 组合**（如 L0C→UB 走 Fixpipe、L1→L0A 走 MTE1）。静态表在 [latency.hpp:43-63](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/costmodel/perf_sim/latency.hpp#L43-L63)：TSYNC/TRESHAPE/TASSIGN 等零开销指令归 Scalar，TMATMUL 族归 Matrix，TIMG2COL/TTRANS 归 MTE1，**其余默认 Vector**。这解释了为什么同一条 TMOV 会出现在报告的不同行——它落在哪条队列取决于操作数的 TileType。

**③ 事件通道与防串扰布局。** [pipe_model.hpp:82-107](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/costmodel/perf_sim/pipe_model.hpp#L82-L107) 把通道号空间切成两段：低段给 `TileDepTracker` 的自动依赖（按地址追踪），`SyncChannelBase()` 以上给显式 set/wait 同步，且用**硬件 pipe 号**（0-7）而不是 PipeStage 编码——因为 PIPE_MTE2 对应两个 PipeStage 但同步上是一条硬件流水线。`EventChannel`（[pipe_model.hpp:68-72](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/costmodel/perf_sim/pipe_model.hpp#L68-L72)）用 `count` 计数器：signal 使 count++，wait 消费时 count--，只要 count>0 就算已通知——多轮循环的信号不会互相覆盖。

**④ 步进仿真核心。** [pipe_model_sim_impl.inl:243-274](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/costmodel/perf_sim/pipe_model_sim_impl.inl#L243-L274) 的 `StepSimulate` 实现了 4.2.2 的循环，`max_steps=500000` 是防死循环保险丝。启动条目前，MTE 类搬运先过 [pipe_model_sim_impl.inl:90-103](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/costmodel/perf_sim/pipe_model_sim_impl.inl#L90-L103) 的 `StartEntry`，其中 `cache.Access(addr, size, duration)` 模拟 L2 命中缩短搬运时长（缓存参数默认值见 [config.hpp:43-50](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/costmodel/perf_sim/config.hpp#L43-L50)：命中省 50 cycles、未命中多花 150 cycles）。仿真结束后 `CollectStuckEntries` 把永远等不到信号的条目也收进时间线并标 `stuck`——**stuck 条目就是在真机上会死锁的写法**，这是 perf_sim 独有的诊断能力。

**⑤ 多核与跨核通道。** [pipe_model_sim_impl.inl:425-468](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/costmodel/perf_sim/pipe_model_sim_impl.inl#L425-L468) 的 `StepSimulateMultiCore` 给每个物理核一套独立队列，事件通道按 `core_id * EVENTS_PER_CORE + event_id` 寻址，跨核信号放到 `CROSS_CHANNEL_OFFSET`（262144）以上的独立区间（[pipe_model_sim_impl.inl:135-144](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/costmodel/perf_sim/pipe_model_sim_impl.inl#L135-L144)）。`block_dim <= 1` 走单核路径，否则走多核路径（[reporter_core_impl.inl:216-227](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/costmodel/perf_sim/reporter_core_impl.inl#L216-L227)）。

**⑥ 报告生成。** [reporter_core_impl.inl:278-294](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/costmodel/perf_sim/reporter_core_impl.inl#L278-L294) 的 `PrintText` 打印五项摘要（核数、指令数、同步事件数、总周期、L2 命中率）后调用 `PrintPipelineTable`（[reporter_core_impl.inl:230-276](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/costmodel/perf_sim/reporter_core_impl.inl#L230-L276)）：行是 8 条流水段、列是物理核、单元格是该段 busy cycles。CSV 产物按 `AIC / AIV0 / AIV1` 三行拆分（[reporter_core_impl.inl:119-135](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/costmodel/perf_sim/reporter_core_impl.inl#L119-L135)），保留 1C2V 结构。

**⑦ gemm 性能模拟用例。** [tests/costmodel/perf_sim_st/testcase/gemm_perf_sim/main.cpp:54-67](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/costmodel/perf_sim_st/testcase/gemm_perf_sim/main.cpp#L54-L67) 直接 `#include "gemm_performance_kernel.cpp"`（即 u5-l3 精读的高性能 GEMM），用 `RunGemmE2E` 模板实例化 1536×256×1024 的单核形状，再经 `LAUNCH_KERNEL` 进入仿真；断言部分（[main.cpp:69-84](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/costmodel/perf_sim_st/testcase/gemm_perf_sim/main.cpp#L69-L84)）检查记录里确实出现了 Matrix/MTE2_AIC/Fixpipe 三种流水段——这正是 GEMM 四级流水（TLOAD→TEXTRACT→TMATMUL→TSTORE）应有的足迹。CMake 一行注册见 [gemm_perf_sim/CMakeLists.txt:1-5](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/costmodel/perf_sim_st/testcase/gemm_perf_sim/CMakeLists.txt#L1-L5)，工程级宏定义 `__COSTMODEL / __NPU_ARCH__=2201 / PTO_COMM_NOT_SUPPORTED` 见 [tests/costmodel/st/CMakeLists.txt:22](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/costmodel/st/CMakeLists.txt#L22)。

#### 4.2.4 代码实践

**实践目标**：对 gemm kernel（gemm_performance 实现）跑一次性能模拟，解读报告并指出瓶颈段。

**操作步骤**：

1. 入口 A（脚本，推荐）——`st` 套件里同样有 gemm_perf_sim 用例（单 case `GemmPerfSim.RunGemm_1536x256x1024`，见 [tests/costmodel/st/testcase/gemm_perf_sim/main.cpp:32-45](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/costmodel/st/testcase/gemm_perf_sim/main.cpp#L32-L45)）：

   ```bash
   python3 tests/run_costmodel.py --suite st --testcase gemm_perf_sim --clean --verbose
   ```

2. 入口 B（手动 cmake，perf_sim_st 套件含 Small/Medium/LargeK 三档，官方指南给出的方式）：

   ```bash
   cmake -S tests/costmodel/perf_sim_st -B tests/costmodel/perf_sim_st/build -DCMAKE_BUILD_TYPE=Release
   cmake --build tests/costmodel/perf_sim_st/build --target gemm_perf_sim --parallel 4
   cd tests/costmodel/perf_sim_st/build && ./bin/gemm_perf_sim
   ```

   注意：`run_costmodel.py` 的 `--suite` 选项只有 `st / st_fit / st_a5_fit` 三档（[run_costmodel.py:479-484](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/run_costmodel.py#L479-L484)），`perf_sim_st` 需手动 cmake。另 `--demo gemm` 会去找 `demos/costmodel/gemm_demo`，该目录在当前仓库不存在，会报 `demo dir not found`——不要用。

3. 从 gtest 输出中找到 `===== Perf-Sim Report: runGemm_... =====` 段，抄下 `Total cycles` 与 Pipeline 表 8 行数值。
4. 打开运行目录下 `perf_sim_output/runGemm_1536x256x1024_pipeline_summary.csv`，核对 AIC/AIV0/AIV1 三行的 busy 细分。
5. 计算各段占比：\( \text{ratio}_p = \frac{\text{busy}_p}{\text{Total cycles}} \)，排出前两名。

**需要观察的现象**：

- Pipeline 表中 GEMM 的活跃段应集中在 `MTE2(AIC)`（TLOAD 搬 A/B 面板进 L1）、`MTE1`（TEXTRACT 从 L1 切片到 L0）、`CUBE`（TMATMUL）、`FIXP`（Acc 写回 GM），而 `VEC`、`MTE2(AIV)`、`MTE3` 应接近 0——因为该 kernel 是纯 Cube 侧流水；
- 各段 busy cycles 之和通常**大于** Total cycles（多条流水并行累计，官方 FAQ 明确此点）；
- busy 最大且接近 Total cycles 的段即瓶颈段。

**预期结果**：按 u5-l3 的真机结论（TMATMUL Ratio 高、TLOAD 接近满载），瓶颈段预期在 `CUBE` 与 `MTE2(AIC)` 之间——若 `MTE2(AIC)` 占比最高说明 memory-feed limited（该 kernel 文档中 TLOAD 长期接近 100%）；`MTE1`（TEXTRACT）占比也不可忽视。**具体数值待本地验证**，以实际输出为准。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `EventChannel` 用 `count` 计数器而不是 `bool signaled`？

**答案**：u2-l3 讲过事件编号按模 8 轮转，第 i 轮与第 i+8 轮复用同一 event_id。若用布尔，前一轮未被消费的信号会被后一轮覆盖或误配；计数器语义下每个 signal 使 count++、wait 消费 count--，多轮信号正确累积（注释原文：so multiple SIGNALs for the same pair accumulate correctly across iterations），见 [pipe_model.hpp:64-72](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/costmodel/perf_sim/pipe_model.hpp#L64-L72)。

**练习 2**：报告里 `stuck` 条目说明什么？它与 CPU 仿真的关系是什么？

**答案**：stuck 表示该条目等待的事件在仿真结束前从未被 signal——通常意味着事件配对泄漏或顺序颠倒，真机上大概率死锁。CPU 仿真后端把 set/wait 做成空桩、单线程顺序执行，**永远发现不了**这类错误（u2-l3 结论）；perf_sim 用事件通道真实模拟了等待语义，`CollectStuckEntries`（[pipe_model_sim_impl.inl:127-133](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/costmodel/perf_sim/pipe_model_sim_impl.inl#L127-L133)）把它们显式暴露出来。这就是「同步正确性上真机之前先过 CostModel」的价值。

**练习 3**：把 `LAUNCH_KERNEL` 的第三个参数从 `(1, nullptr, nullptr)` 改成 `(4, nullptr, nullptr)`，报告会怎么变？

**答案**：`block_dim=4` 使 `Run` 走多核路径（[reporter_core_impl.inl:216-227](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/costmodel/perf_sim/reporter_core_impl.inl#L216-L227)），报告出现 AIC-0..AIC-3 四列；但注意：gemm 用例里 kernel 体是按 blockDim=1 的模板参数写的（`RunGemmE2E<..., 1, ...>`），四列内容大概率相同（同一份记录复制四份），不代表真实多核切分——要仿真多核必须让 kernel 内部按 `block_idx` 真切分。这是初学者最常见的误读，**结论以本地运行为准（待本地验证）**。

### 4.3 A5 VF 造价模型：曲线键查表与公式求值

#### 4.3.1 概念说明

A5 的向量单元（VF）微架构与 A2/A3 差异很大——指令实现方式（1D/2D 形状路径、是否有 post-update 修地址）、尾块处理都影响周期，A2/A3 那张 `(op, dtype, cols)` 三元组表不够用。于是 A5 换成**七元组曲线键查表**：

```
VfCurveKey = (op, src_dtype, dst_dtype, shape_path, vf_impl_kind, tail_kind, op_params)
```

查到一组拟合参数后，按公式种类求值。这本质上是把「一条指令在 A5 向量流水线上的行为」细分成了更贴硬件的等价类，每个等价类一条拟合曲线。`EstimateCycles` 分发里只要 `arch == A5`，所有指令都改走这条路（[lightweight_costmodel.hpp:432-434](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/costmodel/lightweight_costmodel.hpp#L432-L434)）。

#### 4.3.2 核心流程

```
TryEstimateA5VfCycles(input):
  1. 初始化上下文：valid_rows/valid_cols（无掩码时回退 rows/cols）、op_key、src_dtype
  2. 解析 shape_path：
       标量指令（TADDS 等）: valid_cols == cols ? 1D : 2D
       其他: rows == 1 或 valid_cols == cols ? 1D : 2D
  3. 解析 elements_per_repeat（按 dtype 宽度归到 256 字节 repeat 桶）
  4. 计算循环计数：
       1D: inner = ⌈rows·cols / epr⌉, outer = 1
       2D: inner = ⌈cols / epr⌉,     outer = rows
       tail = (有效元素数) mod epr；tail==0 → TailKind::Full，否则 Tail
  5. 组装七元组 VfCurveKey，查 kVfFormulaParamTable
  6. 按公式种类求值（Linear / LinearRows / LinearTail / NestedLoop）
```

四种拟合公式（[formula_backend_vf.hpp:53-77](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/costmodel/a5/formula_costmodel/formula_backend_vf.hpp#L53-L77)）：

\[ \begin{aligned} \text{Linear} &= p_0 \cdot \text{loop} + p_1 \\ \text{LinearRows} &= p_0 \cdot \text{loop} + p_1 \cdot \text{outer} + p_2 \\ \text{LinearTail} &= p_0 \cdot \text{loop} + p_1 \cdot \text{tail} + p_2 \\ \text{NestedLoop} &= (p_0 \cdot \text{inner} + p_1) \cdot \text{outer} + p_2 \end{aligned} \]

`NestedLoop` 形式最能体现 2D 路径的硬件行为：内层每行按 repeat 数线性，外层按行数放大，且行间有固定开销（修地址、判断循环边界）。

#### 4.3.3 源码精读

**① 每 repeat 元素数分档。** [vf_costmodel.hpp:97-126](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/costmodel/a5/vf_costmodel.hpp#L97-L126) 按 dtype 归档：fp32/int32/fp8 → 每 repeat 64 元素、fp16/bf16/int16 → 128、int8 → 256、fp4 → 256。这与 u4-l1 讲的「repeat 固定 256 字节」一致（64×4B = 128×2B = 256×1B = 256B）。**repeat 桶是循环计数的分母**，选错档周期就差数倍。

**② 1D/2D 路径判定。** [vf_costmodel.hpp:202-211](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/costmodel/a5/vf_costmodel.hpp#L202-L211) 的 `ResolveShapePath`：整行有效（无列掩码）或单行时走 1D（数据连续，一条循环吃满 repeat）；否则 2D（逐行推进，行间有开销）。尾块有无（`tail_count == 0`）进一步区分 `TailKind`——尾块要额外的掩码处理，曲线不同。

**③ 曲线键组装与查表。** [vf_costmodel.hpp:325-351](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/costmodel/a5/vf_costmodel.hpp#L325-L351) 的 `BuildVfCurveKeyAndLoopCount` 把上述要素拼成键；[formula_backend_vf.hpp:40-51](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/costmodel/a5/formula_costmodel/formula_backend_vf.hpp#L40-L51) 的 `TryLookupVfFormulaParam` 线性扫描参数表精确匹配七个字段——**任何一个字段对不上就整体失败**（返回 false 后 `TryEstimateA5Cycles` 走 `WarnAndFallbackToZero`）。特殊指令还有额外键位：TSEL 奇偶迭代分别有 `TSel_b32_even/odd` 曲线（[vf_costmodel.hpp:148-160](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/costmodel/a5/vf_costmodel.hpp#L148-L160)），TCVT 按 RoundMode×SaturationMode 组合出 `CAST_RINT_SAT_ON_normal` 等键（[vf_costmodel.hpp:172-184](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/costmodel/a5/vf_costmodel.hpp#L172-L184)）——即 u4-l4 讲过的舍入/饱和两个旋钮在 A5 上各有独立造价曲线。

**④ 入口。** [vf_costmodel.hpp:353-371](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/costmodel/a5/vf_costmodel.hpp#L353-L371) 的 `TryEstimateA5VfCycles` 串起全流程：构造键 → 查表 → `EvalVfFormula` 求值。A5 的参数表同样由 CSV 生成（[include/pto/costmodel/a5/formula_costmodel/](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/costmodel/a5/formula_costmodel/)，对应 `st_a5_fit` 套件验证）。

#### 4.3.4 代码实践

**实践目标**：用只读方式验证「同一指令、不同键位 → 不同周期」，并理解 `st_a5_fit` 套件如何守护参数表。

**操作步骤**：

1. 打开 [include/pto/costmodel/a5/formula_costmodel/formula_params.csv](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/costmodel/a5/formula_costmodel/formula_params.csv)，搜索 `TEXP`，对比 1D 与 2D 路径（若有）、Full 与 Tail 的参数差异。
2. 运行 A5 fit 套件：

   ```bash
   python3 tests/run_costmodel.py --suite st_a5_fit --build-type Release
   ```

3. 挑一个用例（如 tadd_fit），打开其 `main.cpp`/`gen_data`，观察断言里的期望周期是怎么算出来的。

**需要观察的现象**：

- 同一 `(op, dtype)` 下，`Path2D` 或 `Tail` 行的参数与 `Path1D`/`Full` 不同——这就是「有效区不整除 repeat」在 A5 上的代价体现；
- `st_a5_fit` 用例能全部 PASS，说明键位解析逻辑与参数表一一对应。

**预期结果**：套件 PASS；CSV 中能找到多条同 op 不同键的曲线行。若某条曲线缺失，对应指令在 A5 估算时会打 WARN 并回退 0。具体参数值**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：A5 模型为什么必须区分 1D 与 2D 路径，而 A2/A3 模型不需要？

**答案**：A2/A3 的逐元素公式只按 `(op, dtype, cols)` 查表，行数以 `slope × rows × cols` 的形式整体进入，隐含「每行开销均匀」假设。A5 向量单元的 2D 路径（有效列不满 repeat、跨行不连续）每行都要付地址修改与边界处理的固定成本，`NestedLoop` 公式中 `(p0·inner + p1)·outer` 的 p1 项就是行间开销，1D 路径则没有——两类行为曲线形状不同，必须分开拟合。

**练习 2**：一个 `[37, 128]` 的 fp16 tile 做 TADD（A5），`tail_count` 和 `tail_kind` 是多少？

**答案**：fp16 每 repeat 128 元素。走 2D 路径（37 行 > 1 且假设整行有效）：inner = ⌈128/128⌉ = 1，outer = 37，tail = 128 mod 128 = 0 → `tail_kind = Full`。若改成 `[37, 100]`：tail = 100 mod 128 = 100 ≠ 0 → `Tail`。判定逻辑见 [vf_costmodel.hpp:295-312](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/costmodel/a5/vf_costmodel.hpp#L295-L312)。

**练习 3**：为什么 `EstimateCycles` 对 A5 不再区分 TLOAD/TMATMUL 等类别，全部走 VF 曲线？

**答案**：`TryEstimateA5Cycles` 是 A5 的统一入口（[lightweight_costmodel.hpp:366-376](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/costmodel/lightweight_costmodel.hpp#L366-L376)），而 `PtoOpcodeToKey` 只映射了 VF 类指令（TADD..TSEL，见 [vf_costmodel.hpp:30-59](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/costmodel/a5/vf_costmodel.hpp#L30-L59)）。搬运/矩阵乘指令的 op_key 为空串 → 查表失败 → WARN 回退 0。也就是说当前 A5 轻量模型的覆盖面以向量指令为主（配套 VfSim 乱序仿真器另见 `include/pto/costmodel/a5/VfSim/`），完整的 A5 矩阵乘建模走 u5-l5 讲过的 MX 算子真机数据——这体现了「模型覆盖随架构演进逐步补齐」的现状，选用前先确认指令在键表内。

## 5. 综合实践

**任务**：给 gemm 性能模拟做一次「形状扫描 + 瓶颈判定」，产出一份迷你调优报告。

1. **跑基线**：按 4.2.4 的入口 B 构建并运行 `gemm_perf_sim`，记录三个 case（Small 128×64×256 / Medium 1536×256×1024 / LargeK 256×4096×256）的 `Total cycles` 与 Pipeline 表。
2. **算占比**：对 Medium case 造一张表：`段名 | busy cycles | busy/Total`，按占比降序。
3. **判 Bound**：用 u6-l3 的方法解读——若 `MTE2(AIC)` 占比最高且接近 100%，判 MTE Bound（memory-feed limited），对应真机文档里 TLOAD 长期满载的结论；若 `CUBE` 最高，判 CUBE Bound。Small case（单 tile、无 K 分块）与 LargeK case（K=4096、stepK=4）的占比应该有明显差异。
4. **看时间线**：把 `perf_sim_output/*.json` 拖进 `chrome://tracing`，观察 MTE2/MTE1/CUBE/FIXP 四条泳道是否形成稳定流水、CUBE 泳道有没有等数据的空隙（气泡）。
5. **改参数再跑**：仿照 [main.cpp:21-30](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/costmodel/perf_sim_st/testcase/gemm_perf_sim/main.cpp#L21-L30) 新增一个 case（例如把 baseK 从 64 改成 128，注意 L0 容量约束），对比 Total cycles 变化，验证 u5-l3 的「stepK/baseK 摊薄 MTE1 开销」结论。

**验收标准**：能说出 (a) 三个 case 各自的瓶颈段；(b) Small 与 LargeK 瓶颈不同的原因（K 迭代次数与复用率）；(c) 至少一个「改参数 → Total cycles 变化」的实测对比。所有数值以本地输出为准。

## 6. 本讲小结

- CostModel（`__COSTMODEL`）后端在普通 C++ 进程里真实执行 kernel 并逐指令记录，再用流水线仿真推演周期——回答「多快」，与 CPU 仿真回答的「对不对」正交。
- 轻量 CostModel 按「A5 VF 曲线 / 搬运带宽 / TMATMUL 解析式 / 逐元素线性公式」四级分发，参数来自真机 profiling 拟合的 CSV；不支持的组合打 WARN 回退 0，再由按流水段分类的 `FallbackCycles` 兜底。
- perf_sim 四步流水：recorder 记录（指令 + 同步 + TPipe token）→ merge 归并 → pipe_model 事件驱动步进仿真（8 条流水队列、计数器事件通道、L2 缓存模型、stuck 死锁检测）→ reporter 输出文本表 / CSV / Chrome Trace JSON。
- 报告解读三板斧：`Total cycles` 看总量、Pipeline 表各段 busy/Total 看瓶颈段、JSON 时间线看流水重叠与气泡；busy 之和可超过 Total（并行累计）。
- `LAUNCH_KERNEL` 按 `block_dim × VEC_CORES_PER_AIC(2)` 扮演多核执行，单核与多核走不同仿真路径；kernel 体必须内部按核切分，否则多列报告只是复制。
- A5 换用七元组曲线键（op/src/dst/shape_path/vf_impl_kind/tail_kind/op_params）+ 四种公式求值，本质是把指令行为按微架构等价类细分拟合；当前覆盖以 VF 向量指令为主。
- 运行入口：`python3 tests/run_costmodel.py --suite st --testcase gemm_perf_sim`（脚本三套件 st/st_fit/st_a5_fit）；perf_sim_st 套件需手动 cmake；`--demo` 在当前仓库因 `demos/costmodel` 缺失不可用。

## 7. 下一步学习建议

本讲是单元十「测试体系与仿真器内幕」的收官。你已经集齐了三层验证武器：CPU 仿真（功能）、CostModel（周期趋势）、真机 ST（最终裁判）。建议下一步：

1. **实践闭环**：回到 [u6-l3 性能分析与优化方法论](u6-l3-performance-optimization.md) 的方法论，用本讲的 `gemm_perf_sim` 重做一遍「判定 → 调优 → 验证」，体会无硬件环境下的调优工作流。
2. **阅读进阶源码**：`include/pto/costmodel/a5/VfSim/`（乱序向量仿真器 OOO.cpp、IFU/IDU 取指译码模型）是 A5 性能建模的深水区，配合其 `SOURCE_PROVENANCE.md` 了解参数来源。
3. **展望单元十一**：[u11-l1 为 PTO 新增一条指令](u11-l1-add-new-instruction.md) 会讲到新增指令时四件套（CPU/NPU/文档/ST）之外还有第五处——如果你希望新指令可被性能模拟，别忘了在 `PtoOpcode` 枚举与公式 CSV 里登记。
