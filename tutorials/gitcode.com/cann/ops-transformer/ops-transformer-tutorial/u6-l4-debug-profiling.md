# 调试与调优：prof 与 NPU Simulator

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚「调试（定位对不对）」和「调优（定位快不快）」两类问题的不同手段与适用边界。
2. 在真实 NPU 上用 `msprof op` 采集算子耗时、Block Dim 和流水占比，读懂 `OPPROF_*` 产物。
3. 在没有 NPU 的机器上，用 NPU Simulator（`npusim`）跑通 add_example 的精度仿真与流水仿真，并用 `chrome://tracing` 打开指令流水图。
4. 理解 `--dump_cce`、`--oom`、`--mssanitizer`、`--op_debug_config` 四个构建级调试开关从 build.sh 到 opc 编译器旗标的完整传递链路，以及它们之间的互斥规则。

本讲是专家层的调试专题：u6-l1 已经带你从零开发了一个 AICore 算子，本讲回答的是「算子写完之后，跑错了怎么查、跑慢了怎么优化」。

## 2. 前置知识

### 2.1 调试与调优是两类问题

- **正确性调试**：算子结果不对、执行报错、进程卡死。手段包括看 host 日志、kernel 内 `printf`/`DumpTensor`、msDebug 单步、msSanitizer 内存检测。
- **性能调优**：算子结果对但太慢。手段包括 `msprof op` 上板采集（拿到 Kernel 耗时、核数、流水占比）和仿真流水图（拿到指令级排布）。

一个粗略但有用的量化直觉：算子 Kernel 的有效流水占比可以写成

\[ \eta_{pipe} = \frac{T_{busy}}{T_{total}} \]

其中 \( T_{busy} \) 是计算/搬运单元真正在干活的时间，\( T_{total} \) 是 Kernel 端到端时间。\( \eta_{pipe} \) 偏低通常意味着「搬运等计算」或「计算等搬运」，需要调整 tiling 里的切分与双缓冲；而 \( T_{total} \) 本身偏大则可能是切得太碎（launch 次数多）或没吃满核（BlockDim 太小）。本讲的两个采集手段，本质上都是在帮你估计这两个量。

### 2.2 上板 vs 仿真

- **上板（真实 NPU）**：结果最真实，快速拿到整体指标；需要机器和驱动。
- **仿真（NPU Simulator）**：无需 NPU 硬件（只要 CANN toolkit，不要驱动固件），能给出比上板更细的指令级流水；代价是速度慢（CPU 模拟 SoC），且目前只支持部分芯片。

### 2.3 承接前面几讲

- u2-l3 讲过 kernel 的 `CopyIn → Compute → CopyOut` 三段循环和 `TQue` 双缓冲——流水图里看到的正是这三段是否重叠。
- u2-l4 讲过 `build.sh --run_example` 会现场编译 `test_aclnn_*.cpp` 并执行——本讲的仿真实践直接复用这个入口。
- u3-l1 讲过 aclnn 返回码与 `aclGetRecentErrMsg`——那是 host 侧第一道排错手段，本讲把它接上 kernel 侧手段。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [docs/zh/debug/op_debug_prof.md](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/debug/op_debug_prof.md) | 算子调试调优总纲：host 日志、kernel printf/DumpTensor、msDebug、msSanitizer、msprof 上板采集与仿真流水采集 |
| [docs/zh/debug/npu_sim.md](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/debug/npu_sim.md) | NPU Simulator 工具手册：约束、record/report 命令、流水图字段解析 |
| [build.sh](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh) | 构建入口：`--simulator`、`--dump_cce`、`--oom`、`--mssanitizer`、`--op_debug_config` 的解析与 cmake 翻译 |
| [CMakeLists.txt](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/CMakeLists.txt) | 声明 `ENABLE_OOM/ENABLE_MSSANITIZER/ENABLE_DUMP_CCE` 三个缓存 option，并把 `OP_DEBUG_CONFIG` 下发给全局 opc 配置 |
| [cmake/func.cmake](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/cmake/func.cmake) | `add_opc_config` 把调试配置翻译成 opc 编译器旗标（`--save-temp-files`、`--oom`、`--cce-enable-sanitizer` 等） |
| [docs/zh/install/build.md](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/install/build.md) | build.sh 参数官方说明表，含调试开关的互斥关系 |

## 4. 核心概念与源码讲解

### 4.1 性能调优：msprof 上板采集与调试手段总览

#### 4.1.1 概念说明

调优之前通常先要做一轮「正确性排雷」。ops-transformer 把排雷手段按位置分成三层：

1. **Host 侧**：plog 日志 + `aclGetRecentErrMsg()`。aclnn 第一段 `GetWorkspaceSize` 的参数校验失败信息走这条路（u3-l1 已讲过返回码体系）。
2. **Kernel 侧打印**：`AscendC::PRINTF` 打标量、`AscendC::DumpTensor` 把 UB 里的中间张量落到文件，用于核对中间结果。
3. **工具级**：msDebug 单步（适合卡死、越界）、msSanitizer 内存检测（GM/UB 越界、内存泄漏、并发竞争）。

排雷通过后进入性能采集。文档明确区分了两种采集方式的适用场景：

> 上板性能采集适用于在真实 NPU 硬件上运行算子，快速获取算子整体性能指标（如 Kernel 耗时、Block 数、流水占比等）；流水图仿真适用于无 NPU 硬件开发者，或需要深入分析算子内部指令级流水瓶颈、优化指令排布的场景。

#### 4.1.2 核心流程

上板采集的标准流程：

```text
编译安装算子包（bash build.sh --pkg --ops=xxx --soc=...）
    ↓
编译示例可执行文件（build.sh --run_example xxx eager，或手工 g++）
    ↓
进入可执行文件所在目录，执行 msprof op ./test_aclnn_xxx
    ↓
读取 OPPROF_* 目录：Task Duration（Kernel 耗时）、Block Dim（核数）
    ↓
读 ArithmeticUtilization 文件：cube/vector 指令耗时和占比 → 判断流水瓶颈
```

定位到「流水占比低」之后，回到 u2-l3 的知识体系改 tiling（加大 tile、调整 `BUFFER_NUM` 双缓冲、提高 BlockDim），再采一遍对比——这是算子优化的标准迭代闭环。

#### 4.1.3 源码精读

**（1）host 侧日志与错误信息入口**

[docs/zh/debug/op_debug_prof.md:L8-L24](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/debug/op_debug_prof.md#L8-L24) 说明 host 日志默认落在 `$HOME/ascend/log/debug/plog/plog-pid_*.log`，并可用 `export ASCEND_SLOG_PRINT_TO_STDOUT=1` 直接打屏；[docs/zh/debug/op_debug_prof.md:L26-L38](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/debug/op_debug_prof.md#L26-L38) 给出 `aclGetRecentErrMsg()` 的用法和样例输出（形如 `AclNN_Parameter_Error(EZ1001): ... got null for argument ...`）。这是每次算子报错后应做的第一个动作。

**（2）kernel 侧 printf / DumpTensor**

[docs/zh/debug/op_debug_prof.md:L44-L65](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/debug/op_debug_prof.md#L44-L65) 给出两个 kernel 内调试接口的最小示例：

```cpp
// 打印 host 侧算出的分块长度（标量）
AscendC::PRINTF("Tiling blockLength is %llu\n", blockLength_);
// 把 UB 中的中间张量前 128 个元素 dump 出来
DumpTensor(zLocal, 0, 128);
```

注意 `PRINTF` 只支持标量类型；`DumpTensor` 的后两个参数是起始位置与长度，还能附带行号等自定义信息。

**（3）msprof op 上板采集**

[docs/zh/debug/op_debug_prof.md:L164-L194](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/debug/op_debug_prof.md#L164-L194) 是上板采集的核心段落：在可执行文件目录执行 `msprof op ./test_aclnn_add_example`，采完后打印的关键指标包括：

```text
Task Duration(us): 97.861954    ← Kernel 耗时
Block Dim: 8                      ← 实际使用的核数
```

产出目录 `OPPROF_*` 下的 `ArithmeticUtilization` 文件包含各流水占比，是判断「cube/vector/MTE 谁在拖后腿」的直接依据。

**（4）工具级手段：msDebug 与 msSanitizer**

- [docs/zh/debug/op_debug_prof.md:L69-L104](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/debug/op_debug_prof.md#L69-L104)：msDebug 单步调试前需用 `--op_debug_config "ccec_O0,ccec_g"` 编出「无优化 + 带调试信息」的 kernel（大算子还需 `--tiling_key` 收窄到目标变体，tilingKey 从 msdebug 启动日志的 `[Launch of Kernel AddExample_c9e4...306a_7]` 里读）。
- [docs/zh/debug/op_debug_prof.md:L106-L151](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/debug/op_debug_prof.md#L106-L151)：msSanitizer 用 `--op_debug_config "sanitizer"` 编译，再用 `mssanitizer --tool=memcheck --tool=racecheck --kernel-name=AddExample -- ./test_aclnn_add_example` 同时做内存检测与竞争检测。

这里出现的 `--op_debug_config`、`--tiling_key` 是 build.sh 的正式参数，它们如何生效见 4.3 节。

#### 4.1.4 代码实践

**实践目标**：在真实 NPU 上为 add_example 采一次性能基线，并读懂两个核心指标。

**操作步骤**：

1. 按 u2-l4 的方式编译并安装算子包，然后跑通示例：
   ```bash
   bash build.sh --run_example add_example eager --soc=ascend910b
   ```
2. 进入可执行文件所在目录（走 build.sh 时为仓库 `build/` 目录）：
   ```bash
   cd build
   msprof op ./test_aclnn_add_example
   ```
3. 打开采集到的 `OPPROF_*` 目录，记录 `Task Duration` 与 `Block Dim`，并查看 `ArithmeticUtilization` 文件里 vector 类指令的占比。

**需要观察的现象**：终端打印的 Op Name 形如 `AddExample_<hash>_high_performance_1`；Task Duration 为微秒级小数；Block Dim 应等于 tiling 里设置的 8（呼应 u2-l2 讲过的教学算子 tiling 参数写死 blockDim=8）。

**预期结果**：得到一组基线数据。之后你可以回到 `examples/add_example/op_host/add_example_tiling.cpp` 把 `blockDim` 改成别的值重编译再采，观察 Task Duration 变化，直观感受「核数翻倍 ≠ 耗时减半」（数据量太小的时候搬运和启动开销主导）。无 NPU 环境时此实践标记为**待本地验证**，可先完成 4.2 节的仿真实践作为替代。

#### 4.1.5 小练习与答案

**练习 1**：`msprof op` 输出中 `Task Duration` 和 `Block Dim` 分别对应 kernel 的什么信息？为什么优化时要两个一起看？

**参考答案**：`Task Duration` 是该次 Kernel 端到端执行耗时（微秒），`Block Dim` 是本次下发的核数。只看耗时无法区分「单核太慢」和「没用够核」：Block Dim 远小于芯片核数说明并行度没吃满，应优先改 tiling 的切分；Block Dim 已满而耗时仍高，才是单核流水问题，需要看 `ArithmeticUtilization` 里的指令占比。

**练习 2**：想在 kernel 里核对 tiling 计算是否正确下发了 `blockLength_`，应该用 `PRINTF` 还是 `DumpTensor`？

**参考答案**：`blockLength_` 是标量整数，用 `AscendC::PRINTF` 打印即可；`DumpTensor` 用于把 `LocalTensor`（UB 中的向量数据）落到文件查看，适合核对中间计算结果。

### 4.2 仿真调试：NPU Simulator（npusim）

#### 4.2.1 概念说明

NPU Simulator 是一款 **SoC 级芯片仿真工具**：用 CPU 软件模拟一颗 Ascend 芯片来跑你的程序。它的两个关键性质：

1. **二进制兼容**：与板上运行保持二进制兼容，同一个 kernel 二进制既能上真机也能进仿真器——所以你在 u2/u6 里编的 add_example 无需任何改动就能仿真。
2. **双用途**：
   - **精度仿真**：输出 bit 级精度结果，可在无 NPU 时做算子精度验证；
   - **性能仿真**：输出指令流水图，定位算子性能瓶颈。

**重要约束**（[docs/zh/debug/npu_sim.md:L14-L30](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/debug/npu_sim.md#L14-L30)）：

- 仅支持 **Ascend950PR / Ascend950DT**，仅支持 **AI Core 计算类算子**（不支持 MC2/HCCL 通信类算子，所以 u5-l3 的 matmul_all_reduce 不能仿真）；
- 仅**单卡**场景，代码里只能用 0 卡；
- 依赖 CANN toolkit 包但**无需安装驱动固件**——这正是「无 NPU 也能开发」的关键；
- 建议 16 核 CPU、32GB 以上内存，不支持 arm 环境；
- 名称变更：自 2026-07-30 版本起 `cannsim` 正式更名为 `npusim`，旧命令作为别名保留一段时间。

#### 4.2.2 核心流程

仿真的完整生命周期是「record 执行 → 看日志对精度 → report 出流水图」：

```text
npusim record ./test_aclnn_add_example -s Ascend950 --gen-report
    ↓ 生成 npusim_{timestamp}_{user_app}/ 目录
npusim.log                      ← 程序 stdout + 精度比对结果（wrong_num/total_num）
    ↓（--gen-report 已自动解析；也可手动）
npusim report -e <npusim_目录> [-n '0-1,11-12']
    ↓
report/results/kernel_*/core_*/trace_core0.json
    ↓ 拖入 Chrome 的 chrome://tracing
指令流水图：VECTOR / SCALAR / Cube / MTE1-3 / FIXP 各行的时空条带
```

流水图各行含义（[docs/zh/debug/npu_sim.md:L185-L197](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/debug/npu_sim.md#L185-L197)）：

| 字段 | 含义 |
| --- | --- |
| VECTOR | 向量运算单元 |
| SCALAR | 标量运算单元 |
| Cube | 矩阵乘运算单元 |
| MTE1 | 搬运流水：L1 → {L0A/L0B, UBUF} |
| MTE2 | 搬运流水：{DDR/GM, L2} → {L1, L0A/B, UBUF} |
| MTE3 | 搬运流水：UBUF → {DDR/GM, L2, L1}、L1 → {DDR/L2} |
| FIXP | 搬运流水：FIXPIPE L0C → OUT/L1 |
| FLOWCTRL | 控制流指令 |

读图方法：把 MTE 行和 VECTOR/Cube 行在时间轴上对齐看——如果 MTE2（搬入）与 VECTOR（计算）完全串行，说明双缓冲没起作用；理想状态是二者交错重叠。

#### 4.2.3 源码精读

**（1）工具能力定义**

[docs/zh/debug/npu_sim.md:L3-L10](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/debug/npu_sim.md#L3-L10) 定义了仿真器的定位：「在无法获取或芯片资源紧缺的情况下，也能获得与真实芯片几乎一致的验证效果和性能反馈」，并列出精度仿真、性能仿真两大用途。

**（2）record 命令参数**

[docs/zh/debug/npu_sim.md:L85-L104](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/debug/npu_sim.md#L85-L104) 是 `npusim record` 参数表，必选参数只有两个：

- `-s / --soc-version`：目标芯片版本（如 `Ascend950`）；
- `user_app`：待运行程序（可执行文件、`python train.py`、`bash run.sh` 都行）。

可选参数里 `-g/--gen-report` 控制仿真结束后是否自动解析生成报告，`-n/--core-id` 指定哪些核开启日志（配合 `-g` 且未指定时回退到 core 0）。

**（3）精度结果与流水产物**

[docs/zh/debug/npu_sim.md:L53-L79](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/debug/npu_sim.md#L53-L79) 展示了 add_example 仿真的实际 stdout（`result[0] is: 2.000000` 等）以及流水文件路径 `npusim_*/report/results/kernel_*/core_*/trace_core0.json`；[docs/zh/debug/npu_sim.md:L126-L137](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/debug/npu_sim.md#L126-L137) 给出精度比对结果的日志格式（`wrong_num / total_num / result` 列）。

**（4）build.sh 的仿真入口**

`--simulator` 是 `--run_example` 的伴生选项（帮助文本注明 requires --soc parameter），解析处只做一件事——把值存进变量：

[build.sh:L1689-L1693](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L1689-L1693) 把 `--simulator=camodel` 的值赋给 `SIMULATOR` 变量。真正消费它的是示例执行分支：

[build.sh:L687-L691](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L687-L691) 在 eager 示例编译完成后，判断「`SIMULATOR == camodel` 且 `ASCEND_SOC_UNITS == ascend950`」两个条件同时成立，才用 `cannsim record -s Ascend950 ./test_aclnn_xxx --gen-report` 包住示例执行；否则直接 `./test_aclnn_xxx` 上板跑。

```bash
if [[ "${SIMULATOR}" == "camodel" && "${ASCEND_SOC_UNITS}" == "ascend950" ]]; then
    cannsim record -s Ascend950 ./test_aclnn_${EXAMPLE_NAME} --gen-report
else
    ./test_aclnn_${EXAMPLE_NAME}
fi
```

两点值得注意：

- **芯片硬约束写死在脚本里**：非 ascend950 的 SoC 即使传了 `--simulator=camodel` 也会静默走真机执行分支，这与 npu_sim.md「仅支持 950PR/DT」的约束一致。
- **脚本用的是旧名 `cannsim`**：源码尚未跟进 npu_sim.md 记载的 2026-07-30 更名（`cannsim` → `npusim`）。由于旧命令作为别名保留，当前仍可工作；若你的 CANN 版本已移除别名，需手动执行等价的 `npusim record -s Ascend950 ./test_aclnn_xxx --gen-report`。这是文档与源码存在时间差的一个真实例子。

#### 4.2.4 代码实践

**实践目标**：在无 NPU 的环境下，用 simulator 模式跑通 add_example 的完整仿真闭环，拿到流水图文件。

**操作步骤**（有 ascend950 对应 CANN toolkit 的环境）：

1. source CANN 环境变量（u1-l3 讲过 `source set_env.sh`，仿真不需要驱动）。
2. 编译并安装自定义算子包（参照 [docs/zh/debug/npu_sim.md:L40-L51](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/debug/npu_sim.md#L40-L51)）：
   ```bash
   bash build.sh --pkg --soc=ascend950 --vendor_name=custom --ops=add_example
   ./build_out/cann-ops-transformer-custom_linux-<arch>.run
   ```
3. 以仿真模式运行 eager 示例：
   ```bash
   bash build.sh --run_example add_example eager --simulator=camodel --soc=ascend950
   ```
4. 找到产物：走 build.sh 时可执行文件与 `npusim_*`（或 `cannsim_*`）目录生成在仓库 `build/` 下；文档示例路径为 `examples/add_example/examples/build/bin/npusim_*`（手动进示例目录编译时）。查看 `npusim.log` 里的 `result[i] is: 2.000000`。
5. 打开 Chrome 访问 `chrome://tracing`，把 `npusim_*/report/results/kernel_*/core_*/trace_core0.json` 拖入页面，快捷键 W/S 放大缩小、A/D 左右移动。

**需要观察的现象**：log 中 add_example 的逐元素输出为两输入之和；流水图中 MTE2（GM→UBUF 搬入）、VECTOR（Add 计算）、MTE3（UBUF→GM 搬出）三类条带交替出现，且因 `BUFFER_NUM=2` 双缓冲，相邻 tile 的搬送与计算应有交叠。

**预期结果**：精度全部正确、得到可用浏览器打开的 `trace_core0.json`。若你的环境既无 NPU 也无配套 toolkit，本实践标记为**待本地验证**——此时请走读上述命令与 4.2.3 的 build.sh 分支，把「`--simulator=camodel` + `--soc=ascend950` 双条件才生效」这条链路复述出来。

#### 4.2.5 小练习与答案

**练习 1**：为什么 MC2 模块的 `matmul_all_reduce` 无法用 NPU Simulator 仿真？

**参考答案**：npusim 明确约束「仿真环境仅支持 AI Core 计算类算子（不支持 MC2 和 HCCL 类型的算子）」，且仅支持单卡。matmul_all_reduce 依赖 HCCS 卡间集合通信，属于被排除的类型。

**练习 2**：`npusim record` 与 `npusim report` 的分工是什么？`--gen-report` 的作用是什么？

**参考答案**：`record` 负责在仿真环境中执行应用程序并落盘执行数据（`npusim_{timestamp}_{app}/npusim.log` 等）；`report` 负责把 record 的结果解析成可视化的指令流水图（`trace_coreN.json`）。`--gen-report` 让 record 结束后自动执行解析步骤，省去手动调用 report（默认不自动解析）。

**练习 3**：流水图里看到 VECTOR 行有大段空闲、而 MTE2 行一直在忙，最可能的优化方向是什么？

**参考答案**：计算单元在等数据——访存受限。方向包括：增大 tile 尺寸减少搬运次数、确认双缓冲（`BUFFER_NUM`）是否生效让搬运与计算重叠、检查数据对齐与搬运效率，必要时调整 tiling 让可用 UB 更充分利用。

### 4.3 构建级调试开关：--dump_cce / --oom / --mssanitizer / --op_debug_config

#### 4.3.1 概念说明

4.1/4.2 的工具大多要求「先有一个带调试信息的 kernel 二进制」。ops-transformer 把这类需求做成了 build.sh 的**构建级开关**：它们不改变算子逻辑，而是在 opc（离线预编译）阶段给毕昇编译器注入额外旗标。四个开关一句话画像：

| 开关 | 注入的编译旗标 | 用途 |
| --- | --- | --- |
| `--dump_cce` | `--save-temp-files` | 保留 CCE 等中间编译产物，用于分析「Ascend C 源码如何被编译成芯片指令」 |
| `--oom` | `--oom -ffunction-sections -fdata-sections` | kernel 侧内存越界/耗尽检测 |
| `--mssanitizer` | `-g --cce-enable-sanitizer` | 给 msSanitizer 工具准备带检测的 kernel |
| `--op_debug_config "ccec_O0,ccec_g"` 等 | `-O0` / `-g` / `-sanitizer` / `--save-temp-files` 的组合 | 统一的配置入口，msDebug 单步需要 `ccec_O0,ccec_g` |

其中 `--op_debug_config` 是「配置字符串」风格的总入口，`--dump_cce/--oom/--mssanitizer` 是三个「一键」快捷方式，底层汇合到同一个翻译函数。

#### 4.3.2 核心流程

开关从命令行到编译器的传递链（这正是 u1-l4 讲过的「build.sh = 选项翻译器」模式在调试域的实例）：

```text
bash build.sh --pkg --ops=add_example --soc=ascend910b --dump_cce
    ↓ case 解析：ENABLE_DUMP_CCE=TRUE            (build.sh 参数层)
    ↓ check_param：与 --mssanitizer/--oom/--bisheng_flags/--build-type=Debug 互斥校验
    ↓ assemble_cmake_args：-DENABLE_DUMP_CCE=TRUE (翻译成 cmake 缓存变量)
    ↓ CMakeLists.txt option(ENABLE_DUMP_CCE ...)  (cmake 配置层)
    ↓ add_opc_config()：追加 --save-temp-files    (翻译成 opc 编译器旗标)
    ↓ opc 离线预编译 kernel 时保留 CCE 中间文件   (编译器层)
```

`--op_debug_config` 走的是平行通道：值原样传成 `-DOP_DEBUG_CONFIG=<值>`，再由 `add_opc_config` 逐项查表翻译（`ccec_g`→`-g`、`ccec_O0`→`-O0`、`sanitizer`→`-sanitizer`、`dump_cce`→`--save-temp-files`）。

互斥规则一览（`--dump_cce`、`--oom`、`--mssanitizer` 三者两两互斥，且都不可与 `--bisheng_flags=`、`--build-type=Debug` 同用）：这些开关都在改同一批编译旗标，同时开会产生冲突，所以 build.sh 在参数校验阶段直接拒绝。

#### 4.3.3 源码精读

**（1）参数解析：三个一键开关**

[build.sh:L1966-L1981](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L1966-L1981) 是三个开关的 case 分支，各自只置一个标志变量：

```bash
--mssanitizer)
    ENABLE_MSSANITIZER=TRUE
    shift
    ;;
--dump_cce)
    ENABLE_DUMP_CCE=TRUE
    shift
    ;;
...
--oom)
    OOM="true"
    shift
    ;;
```

[build.sh:L1828-L1831](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L1828-L1831) 则把 `--op_debug_config` 的参数值（如 `"ccec_O0,ccec_g"`）存入 `OP_DEBUG_CONFIG`。

**（2）互斥校验**

[build.sh:L1283-L1305](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L1283-L1305) 的 `check_param` 函数集中拦截非法组合，例如：

```bash
if [[ "$ENABLE_MSSANITIZER" == "TRUE" && "$OOM" == "true" ]]; then
    echo "[ERROR] --mssanitizer cannot be used with --oom"
    exit 1
fi
if [[ "$ENABLE_MSSANITIZER" == "TRUE" && "$ENABLE_DUMP_CCE" == "TRUE" ]]; then
    echo "[ERROR] --mssanitizer cannot be used with --dump_cce"
    exit 1
fi
```

同段还拦截 `--bisheng_flags=` 与三开关的组合、`--build-type=Debug` 与三开关的组合。这些约束与 [docs/zh/install/build.md:L73-L76](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/install/build.md#L73-L76) 参数表的描述一一对应。

**（3）翻译成 cmake 变量**

[build.sh:L1472-L1482](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L1472-L1482) 在 `assemble_cmake_args` 中完成标志位到 `-D` 参数的翻译：

```bash
if [ "${OOM}" == "true" ];then
    CUSTOM_OPTION="${CUSTOM_OPTION} -DENABLE_OOM=ON"
fi
if [[ "$ENABLE_MSSANITIZER" == "TRUE" ]]; then
    CUSTOM_OPTION="${CUSTOM_OPTION} -DENABLE_MSSANITIZER=TRUE"
fi
if [[ "$ENABLE_DUMP_CCE" == "TRUE" ]]; then
    CUSTOM_OPTION="${CUSTOM_OPTION} -DENABLE_DUMP_CCE=TRUE"
fi
```

`--op_debug_config` 的翻译在 [build.sh:L1497-L1499](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L1497-L1499)（`-DOP_DEBUG_CONFIG=${OP_DEBUG_CONFIG}`）。

**（4）CMake 层的声明与消费**

[CMakeLists.txt:L46-L48](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/CMakeLists.txt#L46-L48) 声明三个缓存 option（默认全 OFF）。随后 [CMakeLists.txt:L315-L318](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/CMakeLists.txt#L315-L318) 把 `OP_DEBUG_CONFIG` 作为全局配置下发给 `add_opc_config`（`OP_NAME "ALL"`，自定义包路径在 [cmake/custom_build.cmake:L227-L230](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/cmake/custom_build.cmake#L227-L230) 有一份对等调用）。

**（5）最终翻译：add_opc_config**

[cmake/func.cmake:L395-L444](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/cmake/func.cmake#L395-L444) 是整条链路的终点，也是信息量最大的一段。它先逐项翻译 `OP_DEBUG_CONFIG` 字符串：

```cmake
foreach(_option ${OP_COMPILE_CONFIG_LIST})
    if("${_option}" STREQUAL "ccec_g")
        list(APPEND _OPC_CONFIG "-g")
    elseif("${_option}" STREQUAL "ccec_O0")
        list(APPEND _OPC_CONFIG "-O0")
    elseif("${_option}" STREQUAL "sanitizer")
        list(APPEND _OPC_CONFIG "-sanitizer")
    elseif("${_option}" STREQUAL "dump_cce")
        list(APPEND _OPC_CONFIG "--save-temp-files")
    endif()
endforeach()
```

再叠加三个一键开关：

```cmake
if(ENABLE_OOM)
    list(APPEND _OPC_CONFIG "--oom")
    list(APPEND _OPC_CONFIG "-ffunction-sections")
    list(APPEND _OPC_CONFIG "-fdata-sections")
endif()
if(ENABLE_MSSANITIZER)
    list(APPEND _OPC_CONFIG "-g")
    list(APPEND _OPC_CONFIG "--cce-enable-sanitizer")
endif()
if(ENABLE_DUMP_CCE)
    list(APPEND _OPC_CONFIG "--save-temp-files")
endif()
```

可以看到：`--dump_cce` 的本质是给 opc 编译器加 `--save-temp-files`（保留 Ascend C → CCE → 汇编/二进制的中间临时文件）；`--mssanitizer` 的本质是 `-g --cce-enable-sanitizer`，与帮助文本 [build.sh:L125](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L125) 描述的选项完全一致，因此 `--mssanitizer` 与 `--op_debug_config "sanitizer"` 最终效果等价（前者还多带 `-g`）。

另一条消费 `OP_DEBUG_CONFIG` 的路径在 [cmake/func.cmake:L260-L268](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/cmake/func.cmake#L260-L268)：`add_compile_cmd_target` 在生成 kernel 编译命令时把它作为 `--op-debug-config` 厽令行参数传给 CANN 包的 `ascendc_bin_param_build.py`，用于生成编译命令文件——即同一份调试配置既影响离线预编译、也影响生成的编译命令。

#### 4.3.4 代码实践

**实践目标**：用 `--dump_cce` 重编译 add_example，找到 dump 出的 CCE 中间文件，并说明它的分析价值。

**操作步骤**：

1. 确认环境装有 CANN toolkit（u1-l3），进入仓库根目录。
2. 带 `--dump_cce` 编译二进制 kernel（注意不可同时加 `--mssanitizer`、`--oom`、`--bisheng_flags=`、`--build-type=Debug`）：
   ```bash
   bash build.sh --pkg --ops=add_example --soc=ascend910b --dump_cce
   ```
3. 等价写法验证：不带 `--dump_cce`，改用配置字符串 `--op_debug_config "dump_cce"` 编译，对比两次编译日志。
4. 编译过程中观察日志中「Info: cmake config」一行（u1-l4 讲过这是观察实际生效 cmake 参数的入口），确认出现 `-DENABLE_DUMP_CCE=TRUE`。
5. 在 build 目录树下搜索中间产物（典型如 `build/` 下 kernel 编译工作目录中的 `*.cce` 及其它临时文件）：
   ```bash
   find build -name "*.cce" -o -name "*add_example*" | grep -i -E "cce|temp" | head
   ```

**需要观察的现象**：带 `--dump_cce` 的那次编译在 build 目录留下 CCE 中间文件；不带开关的默认编译则只有最终二进制 kernel（临时文件被清理）。中间文件的具体落盘路径由 CANN 包 opc 工具决定，**待本地确认**（不同 CANN 版本路径可能不同，以 `find` 结果为准）。

**预期结果**与**它能帮什么忙**：CCE 文件是 Ascend C 源码经过前端编译后的中间表示，继续向下才生成芯片汇编与二进制。它的分析价值在于：

- **看编译器做了什么**：你写的 Vector/MTE 调用在 CCE 层如何被展开、合并或重排，帮助理解「源码 → 指令」的映射；
- **对齐问题排查**：对照 CCE 与最终汇编，可确认循环展开、双缓冲是否按预期生成，为 4.1/4.2 看到的流水空洞提供编译侧证据；
- **版本差异取证**：同一算子在不同 CANN 版本性能不同时，对比两版 CCE 可以定位是编译器优化策略变了还是源码变了。

无 toolkit 环境时，本实践的编译部分标记为**待本地验证**；可以先做源码阅读型实践：沿 4.3.3 的五个代码点，把 `ENABLE_DUMP_CCE` 从 shell 变量到 `--save-temp-files` 旗标的传递链画成自己的流程图。

#### 4.3.5 小练习与答案

**练习 1**：`bash build.sh --pkg --ops=add_example --soc=ascend910b --dump_cce --mssanitizer` 会发生什么？为什么这样设计？

**参考答案**：直接报错退出，`check_param` 检测到 `--mssanitizer` 与 `--dump_cce` 组合并打印 `[ERROR] --mssanitizer cannot be used with --dump_cce`。因为两者都在修改同一批 opc 编译旗标（sanitizer 要注入检测代码，dump_cce 要保留临时文件，产物的编译配置互相干扰），与其让编译结果不可预期，不如在入口拒绝。

**练习 2**：想用 msDebug 单步调试一个 tilingKey 很多的大算子，应该用哪些选项组合？

**参考答案**：先用 msdebug 跑一次算子，从 `[Launch of Kernel <名>_<tilingKey>_N]` 日志里确定目标 tilingKey；然后 `bash build.sh --pkg --ops=<算子> --soc=<soc> --op_debug_config "ccec_O0,ccec_g" --tiling_key=<目标tilingKey>` 编出无优化带调试信息、且只针对目标变体的 kernel，安装后 `msdebug ./test_aclnn_<算子>` 启动调试。

**练习 3**：`--dump_cce` 与 `--op_debug_config "dump_cce"` 是什么关系？

**参考答案**：等价的两层封装。前者置位 `ENABLE_DUMP_CCE`，后者把字符串 `dump_cce` 传入 `OP_DEBUG_CONFIG`；两者在 `add_opc_config` 中都被翻译为同一个 opc 旗标 `--save-temp-files`。前者是快捷方式，后者是可组合的统一配置入口（还能写 `ccec_O0,ccec_g` 这类组合）。

## 5. 综合实践

**任务：给 u6-l1 开发的自定义算子（或 add_example）做一次完整的「排错 → 仿真 → 调优」演练。**

场景设定：假设你的算子在大 shape 下偶发结果错误且比预期慢。请按下面的顺序综合运用本讲手段，每一步写下你观察到的证据：

1. **host 侧取证**：`export ASCEND_SLOG_PRINT_TO_STDOUT=1` 后重跑示例，记录 plog 与 `aclGetRecentErrMsg()` 输出；若有 `EZ1001` 类参数错误，先回到调用侧修参数（呼应 u3-l1 的校验漏斗）。
2. **kernel 侧取证**：在 kernel 的 Compute 段加一行 `AscendC::PRINTF` 打印 tile 边界，用 `DumpTensor` 落一份输出张量前 128 个元素，与 gen_data.py 的期望对比。
3. **内存排雷**：`bash build.sh --pkg --ops=<你的算子> --soc=ascend910b --mssanitizer` 编译安装后，用 `mssanitizer --tool=memcheck --tool=racecheck --kernel-name=<Kernel类名> -- ./test_aclnn_<算子>` 检查 GM/UB 越界与竞争。
4. **无 NPU 时的替代验证**：若手头没有 950 机器之外的真机，用 `npusim record ./test_aclnn_<算子> -s Ascend950 --gen-report` 在仿真器上核对精度（`npusim.log` 的 wrong_num/total_num）。
5. **性能基线与瓶颈定位**：正确性排除后，`msprof op ./test_aclnn_<算子>` 记录 Task Duration/Block Dim；有 950 环境再用仿真流水图看 MTE 与 VECTOR 的重叠情况。
6. **编译侧取证**：`--dump_cce` 重编译一次，保留 CCE 中间文件，对照流水图的空洞猜测验证编译器是否按预期展开了双缓冲。
7. **迭代**：根据瓶颈修改 tiling（tile 大小 / BLOCK 数 / BUFFER_NUM），重复第 5 步，形成「改前 vs 改后」两组数据，写一段 200 字的结论。

没有硬件的读者至少完成 1（走读）、2（改代码并说明预期输出）、6（走读 4.3.3 传递链）三步，并把全流程整理成一张「症状 → 工具 → 证据」对照表。

## 6. 本讲小结

- 算子问题分两类：**正确性调试**（host 日志、`aclGetRecentErrMsg`、kernel `PRINTF`/`DumpTensor`、msDebug、msSanitizer）与**性能调优**（`msprof op` 上板采集、指令流水仿真），先用前者排雷再用后者优化。
- **上板采集**给出整体指标（Task Duration、Block Dim、`ArithmeticUtilization` 流水占比），**仿真流水图**给出指令级细节（MTE/VECTOR/Cube 时空条带），二者互补。
- **NPU Simulator（npusim，旧名 cannsim）**与真机二进制兼容，无需驱动固件，仅支持 950PR/DT、单卡、AI Core 算子；`build.sh --run_example <op> eager --simulator=camodel --soc=ascend950` 会在示例执行处自动改走 `cannsim record` 分支（双条件硬编码）。
- 构建级调试开关走「build.sh case → check_param 互斥校验 → `-DENABLE_*` → `add_opc_config` 翻译成 opc 旗标」的固定链路：`--dump_cce`→`--save-temp-files`、`--oom`→`--oom -ffunction-sections -fdata-sections`、`--mssanitizer`→`-g --cce-enable-sanitizer`。
- `--op_debug_config` 是统一配置入口，`ccec_O0,ccec_g` 服务 msDebug、`sanitizer` 服务 msSanitizer；三快捷开关两两互斥且不可与 `--bisheng_flags=`、`--build-type=Debug` 同用。
- 文档与源码存在时间差时要交叉验证：npusim.md 已记载更名，而 build.sh 仍调用旧命令 `cannsim`（靠别名兼容）——源码才是事实。

## 7. 下一步学习建议

- **u6-l5（experimental 目录与算子工程模板）**：本讲的调试手段都可以直接用于 experimental 模板里开发的算子，把「调试 + torch 侧验证」串起来。
- **u7-l1（单元测试体系）**：msprof/npusim 是人工运行的分析工具，CI 里看护正确性靠 UT；两套体系配合才是完整的质量防线。
- **深入阅读**：把 [cmake/func.cmake](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/cmake/func.cmake) 中 `add_compile_cmd_target` 与 `ascendc_bin_param_build.py` 的调用关系读完，理解 opc 二进制编译的全貌（承接 u2-l1 的 opc 概念）。
- **外部工具文档**：msProf、msDebug、msSanitizer、NPU Simulator 都是 CANN 工具链成员，本讲只覆盖了与 ops-transformer 仓库相交的部分，深入调优建议到昇腾社区文档读对应工具手册。
