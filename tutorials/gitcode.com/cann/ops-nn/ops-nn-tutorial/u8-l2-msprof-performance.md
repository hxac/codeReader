# 性能采集与调优：msprof op 与流水分析

## 1. 本讲目标

学完本讲，你应该能够：

1. 使用 `msprof op` 命令对算子样例可执行文件采集算子级性能数据。
2. 读懂采集输出中的关键指标：`Task Duration`、`Block Dim`、`Op Type`、`Current Freq` 等，以及 `OPPROF_*` 目录中 `ArithmeticUtilization` 等流水指标文件的作用。
3. 把指标与源码对应起来：`Block Dim` 由谁决定（tiling 的 `SetBlockDim`）、`Task Duration` 度量的是哪一段时间（kernel 执行，不含 Host 侧 launch 开销）。
4. 根据「流水占比、带宽利用率」的常见形态，判断一个 kernel 的优化方向是搬运受限还是计算受限。

本讲承接 u8-l1（printf / DumpTensor / Host 日志）——那是为了解决「算错了」；本讲解决「算对了但太慢」。

## 2. 前置知识

- **功能正确优先于性能**。性能采集的前提是算子已通过功能验证（u1-l4 / u8-l1 的闭环），否则采集到的耗时没有意义。
- **调试打印必须删干净**。u8-l1 讲过：`AscendC::PRINTF` 和 `DumpTensor` 是向 kernel 注入的额外工作，会显著改变耗时。性能采集前必须移除所有打印、重新 `--pkg` 编译并安装 run 包。
- **Task Duration 与端到端耗时的区别**。u5-l4 讲过端到端耗时 = Host 侧启动开销 + kernel 执行时间。`msprof op` 采集的 `Task Duration` 只对应 kernel 在设备上的执行时间，不含 aclnn 两段式调用、executor 登记等 Host 开销。小算子端到端慢但 Task Duration 很短，说明瓶颈在 launch 路径，应去看 u5-l4 的 fast_kernel_launch，而不是改 kernel。
- **流水（pipeline）的直觉**。矢量算子的 kernel 内部是「搬运 → 计算 → 搬回」三类硬件单元并行工作（u5-l1 的双缓冲就是让搬运与计算重叠）。哪一类占的时间比例高，就说明 kernel 被哪类资源卡住——这就是「流水占比」分析的对象。
- **频率**。NPU 的 AI Core 有运行频率与额定频率（`Current Freq` / `Rated Freq`，单位 MHz）。对比两次采集的耗时，必须在同频条件下进行，否则数据不可比。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [docs/zh/debug/op_debug_prof.md](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/debug/op_debug_prof.md) | 官方「算子调试调优」文档：调试手段 + 性能调优（上板采集与仿真两种方式）的权威出处 |
| [docs/QUICKSTART.md](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/QUICKSTART.md) | 快速入门：第三节「算子调试」给出以 AddExample 为对象的完整采集步骤 |
| [build.sh](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh) | `--run_example` 生成样例可执行文件的入口，`--simulator` 与无卡仿真的构建开关 |
| [examples/add_example/op_host/add_example_tiling.cpp](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/add_example_tiling.cpp) | AddExample 的 tiling 实现——`Block Dim` 指标在源码中的源头 |
| [examples/add_example/op_kernel/add_example.h](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_kernel/add_example.h) | AddExample 的 kernel 实现——Add/Mul 实践的修改点（u1-l4 已精读） |

## 4. 核心概念与源码讲解

### 4.1 性能调优在算子开发流程中的位置

#### 4.1.1 概念说明

ops-nn 把「算子调优」官方地定义为两种互补方式，见 [docs/zh/debug/op_debug_prof.md:L111-L121](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/debug/op_debug_prof.md#L111-L121)：该段说明当算子出现性能问题时，用 msProf 工具分析各运行阶段指标（吞吐率、内存占用、耗时等），并区分了两种采集方式的适用场景。

| 方式 | 适用场景 | 得到什么 |
| --- | --- | --- |
| 上板性能采集（本讲主线） | 手上有真实 NPU，想快速判断算子有没有性能问题 | 算子整体指标：Kernel 耗时、Block 数、流水占比 |
| 流水图仿真（u8-l3 承接） | 无 NPU，或需要指令级流水细节 | 指令级流水图，可看指令排布 |

一句话选型：**先上板粗看，仿真深挖**。

#### 4.1.2 核心流程

```text
功能验证通过（u1-l4 闭环）
    ↓
删除所有调试打印（PRINTF / DumpTensor）
    ↓
重新 --pkg 编译 + 安装 run 包
    ↓
--run_example 生成样例可执行文件（test_aclnn_add_example）
    ↓
msprof op ./test_aclnn_add_example
    ↓
读终端摘要（Task Duration / Block Dim ...） + OPPROF_* 目录明细
    ↓
对照 tiling / kernel 源码定位瓶颈 → 修改 → 重新采集对比
```

#### 4.1.3 源码精读

QUICKSTART 把这一步放进「算子调试」阶段，见 [docs/QUICKSTART.md:L207-L229](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/QUICKSTART.md#L207-L229)：这段文档明确「当算子功能验证正确后，可通过 `msprof op` 命令采集算子级性能数据」，即性能采集是开发闭环（编译运行 → 开发 → 调试 → 验证）中调试阶段的最后一步。

其中生成可执行文件的入口就是 u1-l2 讲过的 `--run_example`，其帮助信息见 [build.sh:L334-L346](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh#L334-L346)——该段列出 `--run_example` 的用法示例，包括 eager/graph 模式、`--vendor_name`、以及与本单元 u8-l3 相关的 `--simulator` 后缀；参数解析落在 [build.sh:L924-L933](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh#L924-L933)，其中 `run_example` 置位 `ENABLE_RUN_EXAMPLE`、`simulator` 置位 `ENABLE_SIMULATOR`，两者是独立开关。

#### 4.1.4 代码实践

1. **实践目标**：确认本机环境已具备采集前提（CANN 包内含 msprof 工具、AddExample 已按 u1-l4 编译安装并通过功能验证）。
2. **操作步骤**：
   - `source /usr/local/Ascend/cann/set_env.sh`，确认 `which msprof` 能找到工具；
   - 检查 `examples/add_example/op_kernel/add_example.h` 中没有残留 u8-l1 添加的 `AscendC::PRINTF` / `DumpTensor`；
   - 若有残留：回到项目根目录执行 `bash build.sh --pkg --soc=${soc_version} --ops=add_example -j16` 并重新安装 `./build_out/cann-ops-nn-*linux*.run`。
3. **需要观察的现象**：msprof 命令可被 shell 找到；kernel 源码干净无打印。
4. **预期结果**：环境就绪。若无真实 NPU，本讲上板部分标记「待本地验证」，可直接跳到 u8-l3 的仿真路线。

#### 4.1.5 小练习与答案

**练习 1**：为什么「删打印」必须放在性能采集之前，而不是之后？

**答案**：`AscendC::PRINTF` 和 `DumpTensor` 会在 kernel 执行路径上注入额外的同步与数据搬运工作，改变真实的流水排布，采集到的 Task Duration 与流水占比都会失真。所以顺序是：带打印调试（u8-l1）→ 删打印重编 → 性能采集（本讲）。

**练习 2**：算子端到端调用耗时 500us，`msprof op` 显示 Task Duration 只有 30us，这说明什么？下一步看哪里？

**答案**：说明 kernel 本身不慢，时间花在 Host 侧 aclnn 两段式调用与 launch 路径上（u5-l4 的结论）。优化方向不在 kernel/tiling，而在减少 launch 开销（如 fast_kernel_launch 思路）或减少调用次数（融合）。

### 4.2 上板性能采集：msprof op 命令与输出解读

#### 4.2.1 概念说明

`msprof op` 是 msProf 工具的算子级采集模式：它「包裹」运行一个调用算子的可执行文件，抓取该进程中每个被调起算子的设备侧执行数据。对 ops-nn 开发者，这个可执行文件就是 `--run_example` 编译出来的 `test_aclnn_add_example`。

采集产出两层数据：

1. **终端摘要**：算子名、算子类型、Task Duration、Block Dim、频率等基础信息，以及性能瓶颈提示。
2. **`OPPROF_*` 目录**：自动解析导出的性能数据文件，其中 `ArithmeticUtilization` 文件包含各项流水的耗时与占比。

#### 4.2.2 核心流程

```text
cd <可执行文件所在目录>
msprof op ./test_aclnn_add_example        # 文档给出的等价形式：msprof op --application="./test_aclnn_add_example"
    ↓ 终端打印摘要
Op Name / Op Type / Task Duration(us) / Block Dim / Mix Block Dim /
Device Id / Pid / Current Freq / Rated Freq
    ↓ 落盘
./OPPROF_*/ArithmeticUtilization  → 各类指令的耗时与占比
```

关键指标含义（出自官方文档对打印样例的说明）：

| 指标 | 含义 | 与源码的对应 |
| --- | --- | --- |
| Op Type | 算子执行单元类型，如 `vector` | def 文件中该算子走 AI Core 矢量单元 |
| Task Duration(us) | 当前算子 Kernel 耗时（微秒） | kernel 三段式流水的总执行时间 |
| Block Dim | 当前算子执行核数 | tiling 中 `context->SetBlockDim(usedCoreNum)` 的值 |
| Current Freq / Rated Freq | 实际频率 / 额定频率（MHz） | 对比两次采集是否同频的前提 |

#### 4.2.3 源码精读

- 命令与输出样例的权威定义见 [docs/zh/debug/op_debug_prof.md:L130-L150](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/debug/op_debug_prof.md#L130-L150)：这段给出 `msprof op ./test_aclnn_add_example` 命令、`OPPROF_*` 结果目录，以及一段完整打印样例（`Task Duration(us): 97.861954`、`Block Dim: 8` 等）。
- 指标解释见 [docs/zh/debug/op_debug_prof.md:L150-L152](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/debug/op_debug_prof.md#L150-L152)：文档明确「Task Duration 是当前算子 Kernel 耗时，Block Dim 是当前算子执行核数」，并指出流水明细看 `OPPROF_*` 下的 `ArithmeticUtilization` 文件（cube 及 vector 类型指令耗时和占比），详细字段以 msProf 官方指南为准。
- QUICKSTART 视角的同一流程见 [docs/QUICKSTART.md:L211-L229](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/QUICKSTART.md#L211-L229)：该段用 `--run_example` 生成可执行文件（位于项目 `ops-nn/build` 目录），然后执行 `msprof op --application="./test_aclnn_add_example"`，并说明「执行后会直接打印算子基础信息……和性能瓶颈提示」。

> **注意一个文档间差异**：`op_debug_prof.md` 说可执行文件在 `examples/add_example/examples/build/bin/`，`QUICKSTART.md` 说在项目 `ops-nn/build/`。两份文档写作时点不同，实际位置以你本地为准——可用 `find . -name test_aclnn_add_example` 在项目根目录确认，**待本地验证**。

#### 4.2.4 代码实践

1. **实践目标**：独立完成一次 AddExample 的性能采集，拿到终端摘要与 `OPPROF_*` 目录。
2. **操作步骤**：
   - 项目根目录执行 `bash build.sh --run_example add_example eager cust --vendor_name=custom`，生成并运行 `test_aclnn_add_example`；
   - `find . -name test_aclnn_add_example` 定位可执行文件目录并 `cd` 过去；
   - 执行 `msprof op --application="./test_aclnn_add_example"`；
   - 记录终端打印的 `Op Name / Op Type / Task Duration(us) / Block Dim / Current Freq`；
   - `ls OPPROF_*/` 查看导出文件，找到 `ArithmeticUtilization` 并打开浏览其结构。
3. **需要观察的现象**：终端出现与文档样例同构的摘要块；`OPPROF_*` 目录生成且包含 `ArithmeticUtilization` 文件。
4. **预期结果**：Op Type 为 `vector`；Block Dim 与输入 shape 相关（默认样例 shape 为 `{32,4,4,4}` 共 2048 个元素，元素较少时核数远小于芯片总 AIV 核数）。具体数值**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`msprof op` 采集的 Task Duration 包不包括 `aclnnAddExampleGetWorkspaceSize` 的执行时间？为什么？

**答案**：不包括。Task Duration 是 kernel 在设备上的执行耗时；GetWorkspaceSize 运行在 Host 侧，属于 launch 路径开销（u2-l1、u5-l4）。

**练习 2**：两次采集中 Current Freq 一次 1800、一次 1200，Task Duration 能直接对比吗？

**答案**：不能。频率不同意味着单位时间完成的指令数不同，耗时天然不可比。应在同频（最好等于额定频率）条件下采集，或换算后再比。

### 4.3 从指标回到源码：Block Dim 的源头与常见调优方向

#### 4.3.1 概念说明

采集不是目的，**指标 → 源码 → 修改 → 复测** 才是。本模块把两个核心指标钉到源码上：

- **Block Dim** 由 Host 侧 tiling 决定：AddExample 的 tiling 做「核切分」时计算 `usedCoreNum` 并 `SetBlockDim`。核数少意味着并行度低，Task Duration 会被拉长。
- **流水占比** 对应 kernel 内搬运与计算的配比：`ArithmeticUtilization` 反映 vector/cube 类指令耗时占比；占比低说明大量时间花在等数据（搬运受限），占比高且总时长长说明计算本身是瓶颈（计算受限）。具体文件字段定义以 msProf 官方指南「ArithmeticUtilization」章节为准。

#### 4.3.2 核心流程

常见形态与对策（方法论）：

```text
读取指标组合 → 判断受限类型
  Block Dim 远小于硬件核数 + Task Duration 长
      → 核切分不足：输入规模小（totalIdx/coreNum 向上取整后反推核数少），
        或 tiling 的 blockFactor 划分不合理 → 检查 tiling 的核切分逻辑
  搬运类流水占比高（计算单元等数据）
      → 访存受限（elementwise 算子典型形态）：增大单次搬运长度（ubFactor）、
        确认双缓冲生效（u5-l1 的 BUFFER_NUM）、保证 32 字节对齐（u5-l2）
  计算类流水占比高且接近指令吞吐上限
      → 计算受限：减少指令条数（如 u5-l3 gelu 用 Cast 提精度 + MicroAPI 精简指令）、
        或接受现状（已到硬件极限）
```

对 add_example 这类纯 elementwise 算子，理论耗时下界由「搬运总量 ÷ 带宽」决定，属访存受限形态——流水占比上通常表现为搬运主导。

#### 4.3.3 源码精读

Block Dim 的源头在 tiling 的核切分，见 [examples/add_example/op_host/add_example_tiling.cpp:L212-L225](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/add_example_tiling.cpp#L212-L225)：这段先 `CeilDiv(totalIdx, coreNum)` 算每核元素量 `blockFactor`，再 `CeilDiv(totalIdx, blockFactor)` 反推实际使用的核数 `usedCoreNum`（避免起空核，u4-l1 已讲），最后 `context->SetBlockDim(usedCoreNum)`——**msprof 打印的 Block Dim 就是这一行的值**。当 `totalIdx`（输入总元素数）小于核数时，`usedCoreNum` 等于元素数，并行度天然受限，这与 4.2.4 实践中默认 2048 元素小输入的观察互相印证。

UB 切分决定单次搬运长度，见 [examples/add_example/op_host/add_example_tiling.cpp:L218-L222](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/add_example_tiling.cpp#L218-L222)：`ubFactor` 由 UB 总量、元素宽度（`TYPE_SIZE`）、缓冲块数（`BUFFER_NUM = 6`，即 2 输入 + 1 输出 × 双缓冲，u5-l1 讲过）与 32 字节向下对齐共同决定——若流水分析显示搬运次数过多、单次过短，可从这里入手增大 `ubFactor` 或调整 `BUFFER_NUM`（kernel 侧 `TQue` 深度需同步改，u5-l1）。

#### 4.3.4 代码实践

1. **实践目标**：验证「Block Dim 跟着输入规模走」——不改算子，只改样例 shape，观察核数变化。
2. **操作步骤**：
   - 编辑 `examples/add_example/examples/test_aclnn_add_example.cpp`，把输入 shape 从默认 `{32,4,4,4}` 改为 `{8,8,8,8}`（4096 元素），同步把 `selfX`、`selfY`、`out` 三个 host 侧 vector 长度改为 4096（u1-l4 讲过三者必须一致）；
   - 重新 `bash build.sh --run_example add_example eager cust --vendor_name=custom`；
   - 再次 `msprof op` 采集，记录 Block Dim 与 Task Duration，与 4.2.4 的结果并排对比。
3. **需要观察的现象**：shape 变大后 Block Dim 相同或增大（取决于是否仍远小于核数）、Task Duration 相应变化。
4. **预期结果**：小规模输入下两次 Block Dim 都很小（如个位数），说明该规模下算子未吃满硬件并行度——**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：某次采集中 Block Dim = 8，芯片 AIV 核数为 50，Task Duration 偏长。给出一个最直接的改善思路。

**答案**：先看输入规模：`usedCoreNum = CeilDiv(totalIdx, blockFactor)`，元素太少时核数天然上不去（tiling 无错）。若规模足够大却仍只用 8 核，则检查 tiling 核切分逻辑（`coreNum` 获取是否正确、`blockFactor` 是否被异常放大）。

**练习 2**：为什么 elementwise 算子（add/mul/gelu）的调优重点通常在搬运而不是计算？

**答案**：每个元素只做一两条矢量指令，却要经历 GM→UB→GM 两次搬运，计算量与访存量之比极低，耗时下界由带宽决定。故优化方向是：足够大的 `ubFactor`、双缓冲让搬运与计算重叠、地址 32 字节对齐（u5-l1/u5-l2 的三件套）。

### 4.4 无卡与指令级场景：仿真流水图采集（预告）

#### 4.4.1 概念说明

上板采集依赖真实硬件且只到「流水占比」粒度。当需要指令级排布分析，或手边没有 NPU 时，走仿真路线。op_debug_prof.md 给出两条仿真路径，详见 [docs/zh/debug/op_debug_prof.md:L154-L193](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/debug/op_debug_prof.md#L154-L193)：该段说明 Ascend 950PR 可用 NPU Simulator 执行 `npusim record ./test_aclnn_add_example -s Ascend950 --gen-report` 生成 `trace_core0.json` 指令流水图（用 `chrome://tracing` 打开），而 Atlas A2/A3 系列可用 `msprof op simulator --output=$PWD/pipeline_auto --kernel-name "AddExample" ./test_aclnn_add_example` 生成 `visualize_data.bin` 流水数据（用 MindStudio Insight 查看）。

build.sh 侧对应的构建开关是 `--simulator`（见 [build.sh:L346](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh#L346) 的用法示例 `--run_example mat_mul_v3 eager cust --vendor_name=custom --simulator`），其实现会链接仿真用的 runtime 库并注入 `LD_LIBRARY_PATH`，见 [build.sh:L1511-L1539](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh#L1511-L1539)。这条路线由下一讲 u8-l3 展开，本讲不深入。

#### 4.4.2 核心流程

```text
有卡：msprof op（本讲）→ 指标级判断 → 疑难再仿真深挖
无卡：msprof op simulator / npusim record（u8-l3）→ 指令级流水图
```

#### 4.4.3 源码精读

（本模块源码引用已并入 4.4.1 的两处链接：[docs/zh/debug/op_debug_prof.md:L154-L193](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/debug/op_debug_prof.md#L154-L193) 与 [build.sh:L1511-L1539](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh#L1511-L1539)。）

#### 4.4.4 代码实践

1. **实践目标**：确认仿真命令形态与本机适用的那条路径。
2. **操作步骤**：按 [docs/zh/debug/op_debug_prof.md:L160-L193](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/debug/op_debug_prof.md#L160-L193)，根据芯片型号（950PR → npusim；A2/A3 → msprof op simulator）选择命令试跑一次。
3. **需要观察的现象**：生成 `npusim_*/report/.../trace_core0.json` 或 `pipeline_auto/OPPROF_*/simulator/visualize_data.bin`。
4. **预期结果**：产物文件存在；具体解读留给 u8-l3。**待本地验证**。

#### 4.4.5 小练习与答案

**练习**：上板采集和仿真采集各解决什么问题？顺序上怎么安排？

**答案**：上板快速给出算子级整体指标（耗时、核数、流水占比），用于判断「有没有问题」；仿真给出指令级流水图，用于「问题出在哪条流水、哪段排布」。常规顺序：先上板粗定位，仿真再深挖；无卡环境直接仿真。

## 5. 综合实践：Add 与 Mul 的性能对账

本任务把 u1-l4 的代码修改能力与本讲的采集能力串起来，完成一次标准的「修改 → 复测 → 分析」性能对账。

**任务**：用 `msprof op` 对 AddExample 的 Add 版本与 Mul 版本各采集一次，对比 Task Duration 与 Block Dim，并解释差异。

**步骤**：

1. **基线采集（Add 版）**：
   - 确认 `examples/add_example/op_kernel/add_example.h` 的 `Compute` 中激活的是 `AscendC::Add(zLocal, xLocal, yLocal, currentNum);`（修改方法见 [docs/QUICKSTART.md:L118-L135](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/QUICKSTART.md#L118-L135)，该段展示了 Add/Mul 互换的完整代码）；
   - `bash build.sh --pkg --soc=${soc_version} --ops=add_example -j16` → 安装 run 包 → `bash build.sh --run_example add_example eager cust --vendor_name=custom`；
   - 定位可执行文件并 `msprof op --application="./test_aclnn_add_example"`，记录 Task Duration、Block Dim、Current Freq、Op Name。
2. **切换 Mul 版**：按同一文档把 `Add` 换成 `Mul`，重复编译、安装、采集，记录同样三项指标。
3. **对照分析**，回答三个问题：
   - **Block Dim 变了吗？** 预期不变——Add 与 Mul 的 tiling 完全相同（[add_example_tiling.cpp:L212-L225](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/add_example_tiling.cpp#L212-L225) 的核切分只看 totalIdx 与 coreNum，与 kernel 里做加法还是乘法无关）；
   - **Task Duration 变了吗？** 预期几乎不变或差异极小——二者同为单条矢量指令（u1-l4 讲过矢量指令「输出在前、输入在后、元素数最后」的统一形态），且该算子访存受限，计算指令替换不改变搬运总量；
   - **Op Name 尾缀差异**：打印样例中 Op Name 形如 `AddExample_..._high_performance_1`（见 [docs/zh/debug/op_debug_prof.md:L138-L148](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/debug/op_debug_prof.md#L138-L148)），观察两版的命名与哈希是否变化，理解算子名带二进制指纹的机制。
4. **延伸一步（可选）**：把样例 shape 改为 `{8,8,8,8}` 重复两版采集，验证「shape 影响 Block Dim、指令类型不影响」这一结论在另一规模下仍成立。

**预期结果表**（数值待本地验证，结构应如下）：

| 版本 | Block Dim | Task Duration(us) | 结论 |
| --- | --- | --- | --- |
| Add | N | t1 | 同 tiling → 同核数 |
| Mul | N（与 Add 相同） | t2 ≈ t1 | 访存受限，指令替换不影响耗时 |

**纪律提醒**：整个对账过程中不得残留任何 printf/DumpTensor；两版采集的 Current Freq 必须一致；改 kernel 后必须重新 `--pkg` 编译安装，只跑 `--run_example` 是不生效的（u1-l4 的结论）。

## 6. 本讲小结

- 性能采集排在功能验证之后、且必须先删净调试打印重编：`AscendC::PRINTF`/`DumpTensor` 会污染耗时数据。
- `msprof op --application="./test_aclnn_add_example"` 包裹运行样例可执行文件，产出终端摘要（Op Name/Op Type/Task Duration/Block Dim/频率）与 `OPPROF_*` 目录明细。
- `Task Duration` 只测 kernel 设备侧执行时间，不含 aclnn 两段式的 Host launch 开销——端到端慢但 Task Duration 短时，瓶颈在 launch 路径（呼应 u5-l4）。
- `Block Dim` 的源码源头是 tiling 的 `context->SetBlockDim(usedCoreNum)`，由输入规模与核切分算法决定，与 kernel 内具体指令无关。
- 流水明细看 `OPPROF_*/ArithmeticUtilization`：搬运占比高 → 访存受限（elementwise 典型，抓 ubFactor/双缓冲/对齐三件套）；计算占比高 → 计算受限（抓指令精简）。
- 无卡或需指令级细节时走仿真流水图（npusim / msprof op simulator），由 u8-l3 展开。

## 7. 下一步学习建议

- 下一讲 **u8-l3 无卡调试：NPU Simulator 仿真开发调试**：本讲 4.4 预告的两条仿真路径的完整展开，包括 `--simulator` 构建、`trace_core0.json` 流水图解读。
- 建议继续阅读的源码/文档：
  - [docs/zh/debug/npu_sim.md](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/debug/npu_sim.md)：仿真结果解析说明；
  - msProf 官方指南「性能数据文件 > msprof op > ArithmeticUtilization」章节（文档内多处外链指向），把本讲的指标字段表补全；
  - 对照 [examples/add_example/op_host/add_example_tiling.cpp](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/examples/add_example/op_host/add_example_tiling.cpp) 重温 u4 单元，把「指标异常 → tiling 参数」的映射练成条件反射。
- 采集技能就绪后，可回到 u5 单元的生产算子（gelu）做一次真实的流水分析练习，为 u9 单元的二次开发打基础。
