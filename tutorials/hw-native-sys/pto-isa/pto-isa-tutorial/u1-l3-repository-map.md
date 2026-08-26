# 仓库地图：目录结构与模块职责

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `include/`、`kernels/`、`docs/`、`demos/`、`tests/` 五大目录各自的职责，以及 `scripts/`、`cmake/`、`build.sh`、`pkg_inc/` 等辅助设施的作用。
2. 在 `include/pto/` 下快速定位 `common/`、`cpu/`、`npu/`、`comm/`、`costmodel/` 五个模块，并知道每个模块放什么文件。
3. 掌握一条「指令定位法」：对任意一条 PTO 指令（以 TADD 为例），能找到它的公共 API 声明、CPU 实现、A2/A3 NPU 实现、A5 NPU 实现、ST 测试目录和 ISA 文档，共六个落点。
4. 解释 `pkg_inc/` 目录为什么存在，以及通信 notify 头文件为什么从那里解析——这是本版本（`0dbecbe` → `be5ccb7`）仓库结构上最重要的变化。

本讲不深入任何实现细节——那是后续讲义的任务。本讲只建立「地图感」：拿到一个问题时，你知道去哪个目录找答案。

## 2. 前置知识

学习本讲前，你需要理解三个概念（u1-l2 已铺垫，这里复习并补充）：

- **header-only 模板库**：PTO-ISA 的主体不是 `.so`/`.a` 这样的独立库，而是一堆 C++ 头文件。你的测试或算子代码 `#include <pto/pto-inst.hpp>` 之后，指令实现会随你的代码一起被编译。所以「找库源码」就是「找头文件」。
- **多后端**：同一份 Tile 抽象有多套实现。编译时通过宏选择后端——`__CPU_SIM`（CPU 模拟器）、`__CCE_AICORE__`（NPU 真机/仿真）、`__COSTMODEL`（性能模型）。目录结构与这套后端划分一一对应，这是本讲最重要的主线。
- **SoC 代际**：NPU 实现再按芯片代际细分。A2（Ascend 910B）与 A3（Ascend 910C）共用一套实现（`npu/a2a3/`），A5（Ascend 950）独立一套（`npu/a5/`），A6 与 Kirin 系列各有自己的目录（`npu/a6/`、`npu/kirin9030/` 等）。

另外建议你准备两个趁手的命令行工具：

- `ls` / `find`：浏览目录。
- `grep -rn "关键字" 目录`：全仓库搜索某个指令名或函数名。本讲的实践大量依赖它们。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/README.md) | 项目主 README，其中的 Directory Structure 一节是一级目录的权威说明 |
| [include/pto/README.md](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/README.md) | 头文件布局说明：common/cpu/npu/comm 五个模块的划分 |
| [include/README.md](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/README.md) | **指令实现状态表**：每条指令在 CPU/Costmodel/A2/A3/A5/Kirin 六个后端的可用性 |
| [tests/README.md](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/README.md) | 测试布局说明与各测试入口脚本的用法 |
| [kernels/README.md](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/README.md) | 算子示例目录的组织方式 |
| [pkg_inc/README.md](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/README.md) | 内部头文件目录的定位说明（CANN 打包约定，本版本的关键新增点） |
| [include/pto/comm/pto_comm_instr_impl.hpp](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/pto_comm_instr_impl.hpp) | 通信指令的平台分发层，本讲用它演示 include/ 与 pkg_inc/ 的协作 |

本讲还会路过（只看位置、不深入）：

- `include/pto/pto-inst.hpp`：统一入口头（u1-l5 专门剖析）。
- `include/pto/common/pto_instr.hpp`、`include/pto/cpu/TAdd.hpp`、`include/pto/npu/a2a3/TAdd.hpp`、`include/pto/npu/a5/TAdd.hpp`：TADD 的四个落点，用于演示指令定位法。
- `tests/cpu/st/testcase/tadd/`：TADD 的 ST 测试目录。
- `docs/isa/TADD.md`：TADD 的 ISA 文档。

## 4. 核心概念与源码讲解

### 4.1 一级目录总览：这张地图上有哪些大陆

#### 4.1.1 概念说明

一个开源项目的根目录通常分成几类东西：**对外交付的代码**（头文件/库）、**示例**（demos/kernels）、**质量保障**（tests）、**文档**（docs）和**工程设施**（scripts/cmake/build 脚本）。PTO-ISA 是 header-only 库，所以「对外交付的代码」就是 `include/`；又因为它同时服务多代芯片和多种运行模式，测试和示例也按同样维度组织。先认清这一点，目录结构就成了「自然而然」的而不是死记硬背的。

#### 4.1.2 核心流程

拿到仓库后建立地图的推荐顺序：

1. 读根 README 的 Directory Structure 一节，得到官方目录树。
2. 对照本地 `ls` 确认每个一级目录真实存在（README 可能滞后于代码）。
3. 给每个目录贴一张「职责标签」：放什么、不放什么。
4. 记住每个目录的「入口文件」（README 或主脚本），之后从入口向内深入。

#### 4.1.3 源码精读

官方目录树（节选自根 README）：

> [README.md:203-224](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/README.md#L203-L224) —— 根 README 用一棵注释树概括了全部关键目录：`include/` 是公共头文件与接口，`kernels/` 下分 `manual/`（手工调优实现）与 `custom/`（自定义算子），`docs/` 下分 `isa/`、`coding/`、`assembly/`、`mkdocs/`，`demos/` 放 Auto Mode、baseline 与 torch_jit 示例，`tests/` 下分 `cpu/`、`npu/`、`script/`，最后 `scripts/`、`cmake/`、`build.sh`、`CMakeLists.txt` 构成工程设施。

把官方树和本地实际情况对齐后，整理成下表（职责一栏为本讲总结，文件数为当前 HEAD 的近似值）：

| 一级条目 | 职责 | 关键入口 |
| --- | --- | --- |
| `include/` | **对外交付的全部内容**：PTO 公共 API 与各后端实现（纯头文件） | `include/pto/pto-inst.hpp`（统一入口）、`include/README.md`（状态表） |
| `pkg_inc/` | **内部（不对外暴露）头文件**：通信 notify 头与 RDMA 传输后端实现，随包一起安装但不属于公共 API | `pkg_inc/README.md` |
| `kernels/` | 手工调优算子、Auto Mode 算子、自定义算子脚手架、Python 驱动算子 | `kernels/README.md` |
| `demos/` | 端到端小示例：baseline（add/gemm/flash_attn/allgather）、CPU demo、torch_jit | `demos/README.md` |
| `tests/` | CPU 模拟器 ST、NPU 各 SoC ST、通信 ST、CostModel 测试及全部驱动脚本 | `tests/README.md`、`tests/run_cpu.py` |
| `docs/` | ISA 逐指令文档、编程模型/调优文档、Auto Mode、CostModel、文档站源码 | `docs/README.md`、`docs/isa/README.md` |
| `scripts/` | 安装、打包、检查、同步等工程脚本 | `scripts/README.md` |
| `cmake/` | 共享 CMake 配置与打包逻辑（含把 `pkg_inc/` 装进包的 `package.cmake`） | `cmake/README.md` |
| `build.sh` + `CMakeLists.txt` | 一键构建/运行入口与顶层 CMake 配置 | 根目录 |
| 其他 | `agents/`（agent/skills 设施）、`CONTRIBUTING.md`（贡献规范） | — |

两个容易混淆的点：

- `kernels/` 与 `demos/` 都含算子代码。区别在于 `kernels/` 偏「性能参考实现」（NPU 为主），`demos/` 偏「端到端跑通的最小示例」（含纯 CPU 路径）。`python3 tests/run_cpu.py --demo gemm` 跑的正是 `demos/cpu/gemm_demo/`。
- 通信相关内容分散在**四处**：公共实现头在 `include/pto/comm/`，内部实现头（notify、RDMA）在 `pkg_inc/pto/comm/`，ISA 文档在 `docs/isa/comm/`，测试在 `tests/*/comm/`。这是「同一条指令、多个落点」规律的第一次预演，4.3 会专门讲 `include/` 与 `pkg_inc/` 的分工。

#### 4.1.4 代码实践

**实践：五分钟目录巡检（源码阅读型，无需编译）**

1. 实践目标：确认五大目录与 `pkg_inc/` 在你本地真实存在，并为每个目录找到入口 README。
2. 操作步骤：
   ```bash
   cd <仓库根目录>
   ls -d include kernels docs demos tests scripts cmake pkg_inc
   ls include/pto/          # 应看到 common cpu npu comm costmodel 五个模块
   ls pkg_inc/pto/          # 应看到 comm common costmodel cpu npu 五个占位/实现目录
   ls demos/ kernels/       # 应看到本讲表格中列出的子目录
   ```
3. 需要观察的现象：每个命令都不报 `No such file or directory`；`include/pto/` 下恰好有 `common/ cpu/ npu/ comm/ costmodel/` 五个目录（外加 README 与 `pto-inst.hpp`）；`pkg_inc/pto/` 下当前只有 `comm/` 有实际头文件，其余四个模块目录用 `.gitkeep` 占位。
4. 预期结果：与 4.1.3 表格一致。若某个目录缺失，说明你检出的版本与讲义 HEAD（`be5ccb76`）不同，请先 `git log -1` 核对。

#### 4.1.5 小练习与答案

**练习 1**：仓库里哪个文件是「一条指令在六个后端是否可用」的权威查询位置？

答案：`include/README.md` 中的状态表（见 [include/README.md:34-41](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/README.md#L34-L41)）。表头为 Instruction / CPU / Costmodel / A2 / A3 / A5 / Kirin，取值 Yes / TODO 等。

**练习 2**：你想给 PTO 贡献一个新算子工程，应该放进 `demos/` 还是 `kernels/`？依据是什么？

答案：按 [kernels/README.md:28-31](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/README.md#L28-L31) 的约定，性能/算子实现放 `kernels/`（且建议附带 README 与 `run.sh`）；`demos/` 面向端到端最小示例。

**练习 3**：一个头文件「应该放 `include/` 还是 `pkg_inc/`」，判断标准是什么？

答案：看它是否属于对外公共 API。[pkg_inc/README.md:3-8](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/README.md#L3-L8) 写明：公共 API 头放 `include/`，仅供包内模块使用的内部头放 `pkg_inc/`。新写的内部头应直接放进 `pkg_inc/` 对应子目录。

### 4.2 头文件布局：include/pto 的五个模块

#### 4.2.1 概念说明

`include/pto/` 是整个项目的「发动机」。它按「**与平台无关 → 按后端分**」两刀切开：第一刀分出 `common/`（平台无关的 Tile 类型系统与指令 API 声明），第二刀把后端实现分给 `cpu/`、`npu/`、`comm/`、`costmodel/`。理解这条切法后，「一条指令的代码写在哪」就有了确定性答案：**声明永远在 common，实现按后端各归其位**。

#### 4.2.2 核心流程

一次典型的 include 解析路径（细节留给 u1-l5）：

```text
用户代码
  └─ #include <pto/pto-inst.hpp>          统一入口
       ├─ 定义 __CPU_SIM?  → common/cpu_stub.hpp      （拉入 CPU 侧桩）
       ├─ 定义 __COSTMODEL? → costmodel/runtime_stub.hpp
       ├─ common/pto_tile.hpp             （Tile 类型系统，所有后端共用）
       └─ 定义 __COSTMODEL?
            ├─ 是 → costmodel/pto_instr.hpp   （CostModel 指令集）
            └─ 否 → common/pto_instr.hpp      （公共指令 API 声明）
```

也就是说：无论最终落到哪个后端，用户面对的 API 都来自 `common/`；`cpu/`、`npu/`、`costmodel/` 里的文件一般不直接被用户 include，而是被公共层按宏「组装」进来。

#### 4.2.3 源码精读

**（1）官方布局说明**

> [include/pto/README.md:16-29](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/README.md#L16-L29) —— 官方对五个模块的划分：`common/` 是平台无关的 Tile 与指令基础设施；`cpu/` 是 CPU 模拟/调试支持；`npu/` 按 SoC 版本拆分（`npu/a2a3/`、`npu/a5/`）；`comm/` 是通信指令库（统一入口 `pto_comm_inst.hpp`、类型定义 `comm_types.hpp`、平台分发层 `pto_comm_instr_impl.hpp`）。

**（2）统一入口头**

> [include/pto/pto-inst.hpp:16-33](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/pto-inst.hpp#L16-L33) —— 统一入口按宏决定包含谁：`__CPU_SIM` 时引入 `cpu_stub.hpp`，`__COSTMODEL` 时引入 `costmodel/runtime_stub.hpp`，随后统一引入 `pto_tile.hpp`，并在 `__COSTMODEL` 与其余情况之间二选一地引入 costmodel 或公共的指令声明头。这 18 行就是「多后端」在源码里的物证。

五个模块的职责速查（文件计数为当前 HEAD 近似值）：

| 模块 | 放什么 | 代表文件 |
| --- | --- | --- |
| `common/`（约 20 个条目 + `arch/` 子目录） | Tile 类型系统、指令公共声明与共享实现、事件、内存抽象、常量、CPU 桩 | `pto_tile.hpp`、`pto_instr.hpp`、`pto_instr_impl.hpp`、`memory.hpp`、`event.hpp`、`constants.hpp`、`type.hpp`、`cpu_stub.hpp`、`buffer_limits.hpp`、`syncall_soft.hpp` |
| `cpu/`（98 个条目） | 每条指令一个头文件的 CPU 模拟器实现，外加并行模拟、偏移计算、内存模型等基础设施 | `TAdd.hpp`、`parallel.hpp`、`tile_offsets.hpp`、`NPUMemoryModel.hpp` |
| `npu/` | 按 SoC 分目录的真机实现 | `a2a3/`（约 120 个条目，A2/A3 共用）、`a5/`（约 127 个条目，Ascend 950）、`a6/`、`kirin9030/`、`kirinDev0000/`、`kirinX90/`、`kernels/`（跨架构共享头 `Pto_prefetch.hpp`） |
| `comm/` | 跨 NPU 通信指令：类型定义、统一入口、平台分发，以及 `a2a3/`、`a5/`、`async_common/` 与 `async/` 下的 `sdma/`、`urma/`、`ccu/`、`rdma/` 等传输后端 | `pto_comm_inst.hpp`、`comm_types.hpp`、`pto_comm_instr_impl.hpp` |
| `costmodel/` | `__COSTMODEL` 后端：轻量代价模型与 `perf_sim/` 流水线模拟 | `lightweight_costmodel.hpp`、`arch_config.hpp`、`perf_sim/pipe_model.hpp`、`perf_sim/latency.hpp` |

**（3）多代 NPU 后端目录：npu/ 下的族谱**

`include/pto/npu/` 下的子目录是「跨代际」这条主线最直观的物证：

| 子目录 | 服务对象 | 现状 |
| --- | --- | --- |
| `a2a3/` | A2（Ascend 910B）/ A3（Ascend 910C），两者共用 | 最完整的 NPU 实现之一 |
| `a5/` | A5（Ascend 950），寄存器模型与前代差异较大（见 4.6.3） | 本版本新增 int64/uint64 位运算等一批头文件 |
| `a6/` | 下一代架构 | 本版本恢复了指令头接入：`header.hpp` 汇总 `TLoad.hpp`、`TMatmul.hpp`、`TExtract.hpp`、`TQuant.hpp`、`TReshape.hpp`、`SyncAll.hpp` 等 11 个文件 |
| `kirin9030/` | Kirin 平台 | 状态表的 Kirin 列对应此目录 |
| `kirinDev0000/`、`kirinX90/` | Kirin 系列早期/开发中适配 | 文件较少，状态表暂未跟踪 |
| `kernels/` | 跨架构共享的算子级头 | 目前只有 `Pto_prefetch.hpp` |

a6 的接入方式值得记一眼——它和其他代际一样走「架构宏 + 汇总头」的模式：

> [include/pto/common/pto_instr_impl.hpp:317-319](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/pto_instr_impl.hpp#L317-L319) —— 公共实现层在 `PTO_NPU_ARCH_A6` 宏下包含 `pto/npu/a6/header.hpp`。也就是说，NPU 编译时除了 `__CCE_AICORE__`，还要靠 `PTO_NPU_ARCH_*` 系列宏进一步选出具体代际（A2A3/A5/A6/KIRIN9030…），这套两级宏选择在 u1-l5 展开成完整依赖图。

**（4）指令命名到文件名的映射规律**

看两组对照就能总结出规律：

- 计算类：`TADD` → `cpu/TAdd.hpp`、`npu/a2a3/TAdd.hpp`、`npu/a5/TAdd.hpp`（注意是驼峰 `TAdd.hpp`，不是全大写）。
- 文档类：`TADD` → `docs/isa/TADD.md` 与 `docs/isa/TADD_zh.md`（全大写 + 中英双份）。

因此，知道指令名就能直接猜出文件路径；猜错了再用 `find include -iname "*tadd*"` 兜底。

#### 4.2.4 代码实践

**实践：验证「一条指令、多个同名文件」的布局规律（源码阅读型）**

1. 实践目标：确认 TADD 在 `include/` 下有多个同名实现文件，并观察 CPU 版与 NPU 版的体量差异。
2. 操作步骤：
   ```bash
   find include -iname "*tadd*" | sort
   wc -l include/pto/cpu/TAdd.hpp include/pto/npu/a2a3/TAdd.hpp include/pto/npu/a5/TAdd.hpp
   ```
3. 需要观察的现象：find 应返回 8 个文件——`cpu/TAdd.hpp`、`cpu/TPartAdd.hpp`，以及 `npu/a2a3/`、`npu/a5/` 下各自的 `TAdd.hpp / TAddS.hpp / TPartAdd.hpp`。
4. 预期结果：三个 `TAdd.hpp` 都在百行以内（当前 HEAD 分别为 77 / 96 / 93 行）。文件小是因为「一条指令一个头」，复杂度被拆散到公共框架（如 `TBinOp.hpp`）里。

#### 4.2.5 小练习与答案

**练习 1**：为什么 A2 和 A3 在状态表里永远同值？

答案：见 [include/README.md:30](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/README.md#L30) —— A2（Ascend 910B）与 A3（Ascend 910C）共用 `include/pto/npu/a2a3/` 的同一套实现，因此两列状态恒同。

**练习 2**：`include/pto/common/event.hpp` 属于哪个后端？

答案：不属于任何后端。`common/` 是平台无关层，事件与同步的类型定义被所有后端共享（具体机制在 u3-l1 精读）。

**练习 3**：如果要在 `include/pto/` 下新增一条只在 A5 上支持的指令，至少要动哪几个模块？

答案：`common/`（公共 API 声明，让所有后端都能看到统一接口）、`npu/a5/`（A5 实现），并更新 `include/README.md` 状态表把 A5 列标记为 Yes、其余后端按实际情况标记（完整清单在 u8-l2 展开）。

**练习 4**：`npu/kernels/` 这个子目录和根目录的 `kernels/` 是一回事吗？

答案：不是。根目录 `kernels/` 是可运行的算子示例工程（README + CMakeLists + run.sh）；`include/pto/npu/kernels/` 是 NPU 实现内部共享的头文件目录，目前只有 `Pto_prefetch.hpp` 一个文件，属于库源码而非示例。

### 4.3 pkg_inc 内部头文件目录：通信打包头与 RDMA 后端

#### 4.3.1 概念说明

本版本仓库结构上最重要的事件，是 `pkg_inc/` 从「几乎为空的占位目录」变成了通信内部实现的正式落脚点。理解它需要先了解 CANN 的打包约定：一个 CANN 组件安装后，**上层消费者直接 include 的公共 API** 与**只在组件内部使用、不对外承诺稳定性**的头文件是分开管理的——前者放 `include/`，后者放 `pkg_inc/`。两者都会被安装进包里，但只有 `include/` 下的内容构成对外接口契约。

当前住在 `pkg_inc/` 里的有两类通信头文件：

- **TPutAsyncNotify 的各代实现**：`pkg_inc/pto/comm/a2a3/async/TPutAsyncNotify.hpp` 与 `pkg_inc/pto/comm/a5/async/TPutAsyncNotify.hpp`。它们本版本从 `include/pto/comm/*/async/` 迁出（`include/` 侧的 `async/` 目录现在只剩 `TPutAsync.hpp` 与 `TGetAsync.hpp`）。
- **RDMA 传输后端**：`pkg_inc/pto/comm/async/rdma/` 下的 `rdma_async_intrin.hpp`（设备分发入口）、`rdma_types.hpp` 等类型头，以及 `backends/hns_1825/` 里的 7 个 HNS1825 网卡后端头（backend、workspace manager 系列）。

#### 4.3.2 核心流程

`pkg_inc/` 与 `include/` 的协作链路：

```text
cmake/package.cmake 安装阶段
  ├─ include/  → <install_path>/<arch>-linux/include   （公共 API）
  └─ pkg_inc/  → <install_path>/<arch>-linux/pkg_inc   （内部头，随包一起装）
       └─ CANN 安装器建好顶层 pkg_inc → <arch>-linux/pkg_inc 符号链接

编译阶段（以通信为例）
  include/pto/comm/pto_comm_instr_impl.hpp
    ├─ #include "pto/comm/a5/async/TPutAsync.hpp"          ← 公共头，从 include/ 解析
    └─ #include "../../../pkg_inc/pto/comm/a5/async/TPutAsyncNotify.hpp"
                                                            ← 内部头，相对路径回到仓库根再进 pkg_inc/
                                                              （源码树与安装树的目录层级一致，故同一相对路径两处通用）
```

注意 `pkg_inc/` 里的头并非与世隔绝——例如 `pkg_inc/pto/comm/a5/async/TPutAsyncNotify.hpp` 内部又 `#include "pto/comm/a5/TNotify.hpp"` 与 `"pto/comm/a5/async/TPutAsync.hpp"`，即**内部头可以引用公共头，反向的公共 API 面则不暴露内部头**。

#### 4.3.3 源码精读

**（1）pkg_inc 的定位声明**

> [pkg_inc/README.md:1-15](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/README.md#L1-L15) —— 官方定义：此目录存放**不暴露给上层消费者**的内部头，遵循 CANN 打包约定；`cmake/package.cmake` 会把它随 `include/` 一起安装到 `<arch>-linux/pkg_inc`，CANN 安装器已建好顶层符号链接，因此新增内部头立即可通过 `<install_path>/pkg_inc/pto/...` 访问。

> [pkg_inc/README.md:20-29](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/README.md#L20-L29) —— 内部头的布局规则：**与 `include/pto/` 完全同构**——`pto/comm/`、`pto/common/`、`pto/costmodel/`、`pto/cpu/`、`pto/npu/`（按 SoC 分）。目前除 `comm/` 外都是 `.gitkeep` 占位，为将来其他模块的内部头预留了位置。

**（2）通信平台分发层如何引用 pkg_inc**

> [include/pto/comm/pto_comm_instr_impl.hpp:16-35](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/pto_comm_instr_impl.hpp#L16-L35) —— 通信实现的分发骨架：先由 `#if defined(__CCE_AICORE__) && !(defined(__CPU_SIM) || defined(__COSTMODEL))` 确认是 NPU 真机编译，再在 `PTO_NPU_ARCH_A2A3` 块里逐条包含 a2a3 的通信头。注意第 24 行：`TPutAsyncNotify` 用的是相对路径 `"../../../pkg_inc/pto/comm/a2a3/async/TPutAsyncNotify.hpp"`，而它上面一行的 `TPutAsync` 用的是常规的 `"pto/comm/a2a3/async/TPutAsync.hpp"`——**同一批指令、两种解析来源**，这正是 include/ 与 pkg_inc/ 分工的物证。

> [include/pto/comm/pto_comm_instr_impl.hpp:37-43](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/comm/pto_comm_instr_impl.hpp#L37-L43) —— A5 块结构完全平行：`PTO_NPU_ARCH_A5` 下依次包含同步 TPUT/TGET、异步 TPUT_ASYNC，然后第 43 行同样以 `../../../pkg_inc/...` 路径包含 A5 版 notify 头。这个文件只有 75 行，是观察「平台分发 + 内外头分工」最小而完整的样本。

**（3）相对路径为什么能工作**

`pto_comm_instr_impl.hpp` 位于 `include/pto/comm/`。带引号的 `#include` 按相对**当前文件**的路径解析：`../../../` 从 `include/pto/comm/` 向上三级恰好回到仓库根（或安装根），再进入 `pkg_inc/pto/...`。由于 `package.cmake` 安装时保持了 `include/` 与 `pkg_inc/` 的顶层并列关系，这条相对路径在源码树和安装树中都成立——这就是为什么不写成 `"pto/comm/a5/async/TPutAsyncNotify.hpp"`（那会命中 include 搜索路径，而该文件已不在 include/ 下）。

**（4）RDMA 后端在 pkg_inc 中的位置**

`pkg_inc/pto/comm/async/rdma/` 当前包含：`rdma_async_intrin.hpp`（RDMA 异步指令的设备侧入口）、`rdma_types.hpp`、`rdma_device_common.hpp`、`rdma_workspace_types.hpp`，以及 `backends/hns_1825/` 下的 `hns_1825_backend.hpp` 与 6 个 workspace/类型头。与之对照，`include/pto/comm/async/rdma/` 下只保留了 `rdma_workspace_manager.hpp`。也就是说 **SDMA/URMA/CCU 的 intrin 实现仍在 `include/pto/comm/async/` 下，而 RDMA 的 intrin 与 backend 全套放进了 `pkg_inc/`**——传输层实现属于内部细节，不进公共 API。它们的逐头精读留给 u6-l5。

#### 4.3.4 代码实践

**实践：亲手验证 notify 头的迁移（源码阅读型，10 分钟）**

1. 实践目标：确认 `TPutAsyncNotify` 的实现已从 `include/` 迁到 `pkg_inc/`，并能解释 `pto_comm_instr_impl.hpp` 中 include 路径的含义。
2. 操作步骤：
   ```bash
   grep -n "TPutAsyncNotify" include/pto/comm/pto_comm_instr_impl.hpp
   find include pkg_inc -name "TPutAsyncNotify.hpp"
   grep -n '#include' pkg_inc/pto/comm/a5/async/TPutAsyncNotify.hpp | head -5
   git log --oneline -3 -- pkg_inc/README.md
   ```
3. 需要观察的现象：第一个命令显示两处 `../../../pkg_inc/...` 路径（a2a3 与 a5 各一处）；第二个命令**只在 `pkg_inc/` 下**找到两个 `TPutAsyncNotify.hpp`，`include/` 下没有；第三个命令显示 pkg_inc 版 notify 反过来引用了 include/ 下的 `pto/comm/a5/TNotify.hpp` 等公共头。
4. 预期结果：你能回答规格中的问题——通信 notify 头从 `pkg_inc/` 解析，是因为它属于「不暴露给上层消费者」的内部实现（`pkg_inc/README.md` 的约定），`include/` 只保留公共 API；`../../../` 三级回退正好从 `include/pto/comm/` 回到仓库根再进 `pkg_inc/`。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `TPUT_ASYNC_NOTIFY` 的 ISA 文档（`docs/isa/comm/TPUT_ASYNC_NOTIFY.md`）存在，但它的实现头却在内部目录 `pkg_inc/`？这两者矛盾吗？

答案：不矛盾。「有 ISA 文档」说明这条指令是**指令集的正式成员**，语义对用户可见；「实现在 pkg_inc」说明它的**代码文件**属于内部实现细节，用户通过公共入口（`pto_comm_inst.hpp` / `pto_comm_instr_impl.hpp` 的分发）间接使用它，而不应直接 `#include` 它的头。API 契约与代码摆放是两个维度。

**练习 2**：如果未来某条 CostModel 内部辅助头需要「随包安装但不对外」，应该放哪？

答案：放 `pkg_inc/pto/costmodel/`——该目录已经用 `.gitkeep` 预置好了，遵循 [pkg_inc/README.md:20-29](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/pkg_inc/README.md#L20-L29) 声明的「与 include/pto/ 同构」规则。

**练习 3**：`include/pto/comm/async/` 下现在有哪些传输后端目录？RDMA 有什么不同？

答案：`sdma/`、`urma/`、`ccu/`、`rdma/` 四个。前三个的 intrin 实现头就在 `include/` 下；`rdma/` 在 include 侧只有 `rdma_workspace_manager.hpp`，intrin 入口与 HNS1825 backend 全套在 `pkg_inc/pto/comm/async/rdma/` 下。

### 4.4 测试布局：tests 的三条纵队和一个脚本层

#### 4.4.1 概念说明

PTO-ISA 的测试按「**在哪运行**」分三条纵队：CPU 模拟器测试（`tests/cpu/`，无昇腾环境即可跑）、NPU 测试（`tests/npu/`，按 SoC 再分目录）、CostModel 测试（`tests/costmodel/`）。纵队之上有一个脚本层（`tests/script/` 与 `tests/` 根下的若干 `run_*.py`/`*.sh`），负责构建、数据生成、运行与汇总。这种「内容按平台分、驱动统一收口」的结构，让同一套用例思想可以在不同后端复用。

#### 4.4.2 核心流程

一个 CPU ST（Single-instruction Test，单指令测试）用例的生命周期（承接 u1-l2 学过的 `run_cpu.py` 流程）：

```text
tests/script 或 run_cpu.py 驱动
  → CMake 构建 tests/cpu/st/testcase/<指令>/ 下的工程
  → 运行 gen_data.py 生成 input/golden 二进制
  → gtest 运行 main.cpp 中的用例（调用 *_kernel.cpp 里的内核）
  → 内核输出与 golden 比对，输出 PASS/FAIL
```

#### 4.4.3 源码精读

**（1）官方布局说明**

> [tests/README.md:19-48](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/README.md#L19-L48) —— 官方列出的测试布局：`script/` 是推荐入口脚本（`run_st.py`、`build_st.py`、`all_cpu_tests.py` 等）；`cpu/st/` 是 CPU 计算类 ST；`cpu/comm/st/` 是 CPU 通信 ST；`npu/` 下按 `a2a3/src/st/`、`a5/src/st/`、`kirin9030/src/st/` 等 SoC 分目录；`costmodel/` 含 `st/` 与 `st_fit/`；根下还有 `run_cpu.py`、`run_st.sh`、`run_comm_test.sh`、`run_costmodel.py` 等入口。

**（2）ST 用例的「标准四件套」**

以 TADD 的 CPU ST 为例，一个用例目录恰好四个文件：

| 文件 | 行数（当前 HEAD） | 职责 |
| --- | --- | --- |
| `tadd_kernel.cpp` | 62 | 被测内核：定义 Tile、TASSIGN 绑地址、TLOAD/TADD/TSTORE |
| `main.cpp` | 101 | gtest 用例骨架：aclrt 初始化、读 golden、比对 |
| `gen_data.py` | 92 | 用 numpy 生成输入与期望输出二进制 |
| `CMakeLists.txt` | 10 | 一行 `pto_cpu_sim_st(tadd)` 把用例接入构建 |

其中 CMakeLists 的全部「有效内容」只有一行：

> [tests/cpu/st/testcase/tadd/CMakeLists.txt:10](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/cpu/st/testcase/tadd/CMakeLists.txt#L10) —— `pto_cpu_sim_st(tadd)`：一个 CMake 函数调用就完成注册，目录名即用例名。这是「用例目录高度模板化」的物证，也是 u5-l1 你将仿照它新增用例的基础。

内核文件的开头体现了「用例 → 统一入口头」的依赖方向：

> [tests/cpu/st/testcase/tadd/tadd_kernel.cpp:10-27](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/cpu/st/testcase/tadd/tadd_kernel.cpp#L10-L27) —— 内核只 include 统一入口 `<pto/pto-inst.hpp>` 与常量头，然后定义五维 Shape/Stride、GlobalTensor、Tile，并用三条 TASSIGN 把三个 tile 绑到 UB 的 0x0/0x4000/0x8000 地址上。逐行精读留给 u1-l4。

**（3）规模感**

`tests/cpu/st/testcase/` 下当前约 135 个用例目录（`tadd`、`tmatmul`、`tflashattn`、`tisa_coverage` 等），`tests/npu/a2a3/src/st/testcase/` 约 134 个；**A5 侧重是本版本扩容的主战场**——`tests/npu/a5/src/st/testcase/` 已达约 159 个用例目录，新增了 `tsel`、`tsels`、`tpartmax`、`tpartadd`、`tshl`、`tshls`、`trem`、`trems`、`txor`、`txors`、`tors`、`tpushpop_subblock_dispatch` 等一大批（配合 A5 新增的 int64/uint64 支持，详见 u4-l7 与 u5-l1）。也就是说，**同一条指令的 ST 往往同时存在 CPU 版和 NPU 版**，目录结构平行，便于对照迁移。

#### 4.4.4 代码实践

**实践：数一数测试纵队（源码阅读型）**

1. 实践目标：建立对三条测试纵队规模的直观感受，并确认 tadd 用例在 CPU 与 NPU 两侧都存在。
2. 操作步骤：
   ```bash
   ls tests/cpu/st/testcase | grep -v CMakeLists | wc -l
   ls tests/npu/a2a3/src/st/testcase | head
   ls tests/npu/a5/src/st/testcase | wc -l
   ls tests/npu/a5/src/st/testcase/tadd
   ls tests/script
   ```
3. 需要观察的现象：CPU 用例约 135 个；NPU a2a3 用例目录列表里能找到 `tadd`；A5 用例目录约 159 个；`tests/npu/a5/src/st/testcase/tadd/` 下同样是 `CMakeLists.txt / gen_data.py / main.cpp / tadd_kernel.cpp` 四件套。
4. 预期结果：与 4.4.3 的描述一致。可再运行 `python3 tests/script/run_st.py -r sim -v a3 -t tadd`（需按 u1-l2 准备环境）验证用例可被驱动脚本找到。

#### 4.4.5 小练习与答案

**练习 1**：`run_st.py`、`run_cpu.py`、`run_comm_test.sh` 各自的适用场景？

答案：`run_cpu.py` 驱动 CPU 模拟器全量 ST 与 demo（见 [tests/README.md:9-13](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/README.md#L9-L13)）；`run_st.py` 构建/运行单个 NPU ST，支持 `-r sim|npu`、`-v a3|a5`、`-t 用例名` 过滤；`run_comm_test.sh` 专跑基于 MPI+HCCL 的多卡通信 ST（[tests/README.md:50-56](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/README.md#L50-L56)）。

**练习 2**：`tests/npu/a2a3/src/common/` 与 `tests/npu/a2a3/src/st/` 有什么区别？

答案：`st/` 存放具体用例工程；`src/common/` 存放该 SoC 下多个用例共享的测试资源（见 [tests/README.md:30-36](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/README.md#L30-L36) 的布局说明）。

**练习 3**：本版本 A5 ST 用例大批扩容，最直接的受益者是谁？

答案：A5 后端指令（尤其本版本新增的 int64/uint64 位运算与 TPUSH/TPOP 派发修复）的回归保障。每条新支持的指令都有对应 ST 用例做 golden 比对，`tests/run_st.sh` 冒烟清单圈定了最小用例集（u5-l1 详述）。

### 4.5 算子示例布局：kernels 与 demos 的分工

#### 4.5.1 概念说明

`kernels/` 和 `demos/` 都是「可运行的完整算子工程」，但定位不同：`kernels/` 展示**怎么写好**（手工调优、Auto Mode、自定义算子脚手架、Python 驱动），`demos/` 展示**怎么跑起来**（最小端到端示例，含纯 CPU 路径与框架集成路径）。它们共同的特点是「自包含小工程」：每个子目录自带 README、CMakeLists 与运行脚本，可以独立阅读、独立构建。

#### 4.5.2 核心流程

按学习目的选择示例的决策流：

```text
想看手工调优的极致实现？      → kernels/manual/{a2a3,a5,common}/
想看自动模式的简化写法？      → kernels/automode/{a2a3,a5}/ 或 demos/auto_mode/
想从零搭自定义算子工程？      → kernels/custom/（fused_add_relu_mul 脚手架）
想不依赖昇腾环境跑通算法？    → demos/cpu/（gemm/flash_attention/mla 三个 demo）
想了解 PyTorch/torch_jit 集成？→ demos/torch_jit/ 与 kernels/python/
```

#### 4.5.3 源码精读

**（1）kernels 布局**

> [kernels/README.md:15-27](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/README.md#L15-L27) —— 官方布局：`manual/` 下按平台分 `a2a3/`（gemm_performance、conv2d_forward、topk 等）、`a5/`（flash_atten、matmul_mxfp4/mxfp8_performance 等）、`common/`（跨平台 flash_atten）；`custom/` 是自定义算子扩展脚手架。

> [kernels/README.md:5](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/README.md#L5) —— 关键一句：大多数 kernel 子目录是**自包含的小工程**（kernel + host + scripts），各自带 README、CMakeLists 与 run.sh。这决定了阅读策略：挑一个目录，从它的 README 进入即可，不必按依赖顺序通读。

本地实际结构比 README 略丰富，还包括：`automode/`（a2a3、a5 下的 Auto Mode 版算子）、`python/`（gemm、flash_atten 的 Python 驱动工程）、`include/`（kernels 共享头，如 flash_atten 的 `pto_macro_*` 宏封装）与 `run_kernels.sh`。

**（2）demos 布局**

`demos/` 下四个子目录：

| 子目录 | 内容 | 用途 |
| --- | --- | --- |
| `baseline/` | `add`、`gemm_basic`、`flash_atten`、`allgather_async` | 各类算子的基线实现（host 侧封装齐全，含 setup.py 打包示范） |
| `cpu/` | `gemm_demo`、`flash_attention_demo`、`mla_attention_demo` | `run_cpu.py --demo` 的实际载入对象，纯 CPU 可跑 |
| `auto_mode/` | `baseline`、`torch_jit` | Auto Mode 示例（根 README 推荐的第一个入门示例 `demos/auto_mode/baseline/add/`） |
| `torch_jit/` | `add`、`gemm`、`flash_atten` | PyTorch 即时编译运行 PTO 内核的工作流 |

**（3）docs 布局速览（顺带建立文档地图）**

- `docs/isa/`：每条指令一个 `指令名.md` + 一个 `指令名_zh.md`，顶层约 280 个 md；通信指令集中在子目录 `docs/isa/comm/`（TPUT/TGET/TREDUCE/TNOTIFY 等，本版本新增 `TPUT_ASYNC_NOTIFY.md` 独立文档）。
- `docs/coding/`：编程模型与开发文档（`Tile.md`、`Event.md`、`GlobalTensor.md`、`cpu_sim.md`、`opt.md`、`multi-core-programming.md`、`tutorials/` 等）。
- 其他：`docs/auto_mode/`、`docs/costmodel/`、`docs/mkdocs/`（文档站）、`docs/figures/`（插图）、`docs/getting-started.md`。

#### 4.5.4 代码实践

**实践：为一个算子示例建档案（源码阅读型）**

1. 实践目标：以 `kernels/manual/a2a3/gemm_performance/` 为对象，验证「自包含小工程」的说法。
2. 操作步骤：
   ```bash
   ls kernels/manual/a2a3/gemm_performance/
   head -40 kernels/manual/a2a3/gemm_performance/README.md
   ls demos/cpu/gemm_demo/
   ```
3. 需要观察的现象：gemm_performance 目录下同时存在 `README.md`、`CMakeLists.txt`、`gemm_performance_kernel.cpp`、`main.cpp` 与运行脚本；README 里有构建/运行说明与性能数据。
4. 预期结果：确认它不依赖 kernels 下其他工程即可独立构建（NPU 环境）；`demos/cpu/gemm_demo/` 则是它的纯 CPU 简化版，两者可在 u5-l3 对照阅读。

#### 4.5.5 小练习与答案

**练习 1**：想找 Flash Attention 在 A2/A3 与 A5 上各自的参考实现，路径分别是什么？

答案：A2/A3 用跨平台实现 `kernels/manual/common/flash_atten/`，A5 用 `kernels/manual/a5/flash_atten/`（见 [README.md:136-138](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/README.md#L136-L138) 的推荐索引）。

**练习 2**：`docs/isa/TADD.md` 和 `docs/coding/Tile.md` 的区别是什么？

答案：前者是**逐指令参考**（语义、数学定义、汇编语法、约束），按指令名组织在 `docs/isa/`；后者是**编程模型文档**（Tile 抽象本身怎么用），按主题组织在 `docs/coding/`。查「TADD 能不能算 int8」去前者，查「Tile 的 valid mask 怎么理解」去后者。

### 4.6 指令定位法：以 TADD 为例的六个落点

#### 4.6.1 概念说明

前面几个模块建立了静态地图。本模块把它变成**可复用的方法**：给定任意一条指令名（如 TADD），如何在 1 分钟内找齐它的全部相关代码。这套方法的基础正是 4.2 的「声明在 common、实现按后端分」+ 4.4 的「用例目录即指令名」+ 4.5 的「每指令一份 ISA 文档」三条布局规律。

#### 4.6.2 核心流程

指令定位的固定套路（对绝大多数计算类指令成立）：

```text
第 1 落点  公共 API 声明    grep -n "TADD(" include/pto/common/pto_instr.hpp
第 2 落点  CPU 实现        include/pto/cpu/TAdd.hpp           （驼峰命名）
第 3 落点  A2/A3 NPU 实现  include/pto/npu/a2a3/TAdd.hpp
第 4 落点  A5 NPU 实现     include/pto/npu/a5/TAdd.hpp
第 5 落点  ST 测试         tests/cpu/st/testcase/tadd/ 与 tests/npu/*/src/st/testcase/tadd/
第 6 落点  ISA 文档 + 状态表  docs/isa/TADD.md 与 include/README.md 状态表
```

（通信指令多一个维度：按 4.3 的分工，实现还可能落在 `pkg_inc/` 下。）

#### 4.6.3 源码精读

**（1）第 1 落点：公共 API 声明**

> [include/pto/common/pto_instr.hpp:175-181](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/pto_instr.hpp#L175-L181) —— TADD 的公共签名：`PTO_INST RecordEvent TADD(dst, src0, src1, WaitEvents&... events)`。先 `detail::PtoWaitEvents(events...)` 等待事件，再 `MAP_INSTR_IMPL(TADD, ...)` 把调用映射到当前后端的 `TADD_IMPL`，最后返回一个 RecordEvent。**所有指令都长这个样子**——变参 WaitEvents 表达「执行前要等谁」，返回值表达「我完成后可以通知谁」。

**（2）第 2 落点：CPU 实现**

> [include/pto/cpu/TAdd.hpp:63-75](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/cpu/TAdd.hpp#L63-L75) —— CPU 后端的 `TADD_IMPL`：先取 dst 的有效行列，用两条 `PTO_ASSERT` 校验 src0/src1 的有效形状与 dst 一致，然后调用 [TAdd_Impl（L19-L61）](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/cpu/TAdd.hpp#L19-L61)——按行主/列主、是否分形布局分四个分支，用 `cpu::parallel_for_rows` 多线程模拟向量指令，逐元素 `dst[idx] = src0[idx] + src1[idx]`。

**（3）第 3 落点：A2/A3 NPU 实现**

> [include/pto/npu/a2a3/TAdd.hpp:20-32](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a2a3/TAdd.hpp#L20-L32) —— `AddOp<T>` 结构体把「加法」封装成对 CCE 内置指令 `vadd(dst, src0, src1, repeats, 1, 1, 1, 8, 8, 8)` 的调用；[L38-L54](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a2a3/TAdd.hpp#L38-L54) 的 `__tf__` 层函数从 tile 取出 `__ubuf__` 指针并交给通用的 `BinaryInstr` 框架（在 `TBinOp.hpp`）；[L57-L78](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a2a3/TAdd.hpp#L57-L78) 的 `TAddCheck` 用 `static_assert` 在编译期拦截类型不一致、不支持的 dtype、非行主布局三类错误；[L81-L94](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a2a3/TAdd.hpp#L81-L94) 的 `TADD_IMPL` 把这些串起来。

**（4）第 4 落点：A5 NPU 实现**

> [include/pto/npu/a5/TAdd.hpp:24-30](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a5/TAdd.hpp#L24-L30) —— A5 的 `AddOp` 同样封装 `vadd`，但操作数是 `RegTensor<T>&` 加 `MaskReg& preg` 的寄存器模型（`vadd(reg_dst, reg_src0, reg_src1, preg, MODE_ZEROING)`），这与 A2/A3 的「指针 + repeats + stride」模型不同；[L35-L53](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a5/TAdd.hpp#L35-L53) 的 `TAdd` 还带 `OP_NAME(TADD) OP_TYPE(element_wise)` 标注与 `VFImplKind` 版本参数，并对 int64/uint64 走 [L45-L47](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a5/TAdd.hpp#L45-L47) 的 `Int64Binary<Int64Op::Add, ...>` 专门分支（本版本 A5 新增的 64 位仿真路径，u4-l7 精读）；[L80-L93](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a5/TAdd.hpp#L80-L93) 的 `TADD_IMPL` 把检查与调用串起来。同一语义、两套微架构映射，这正是「跨代抽象」的具象体现。

**（5）第 5、6 落点：测试与文档**

> [tests/cpu/st/testcase/tadd/tadd_kernel.cpp:10-16](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/cpu/st/testcase/tadd/tadd_kernel.cpp#L10-L16) —— CPU ST 的被测内核入口 `runTAdd`，通过统一入口头拿到 TADD 等指令。

> [docs/isa/TADD.md:1-15](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/docs/isa/TADD.md#L1-L15) —— ISA 文档给出语义（「Elementwise add of two tiles」）、数学解释 \(\mathrm{dst}_{i,j} = \mathrm{src0}_{i,j} + \mathrm{src1}_{i,j}\) 与汇编语法（`%dst = tadd %src0, %src1 : !pto.tile<...>`）。

> [include/README.md:39](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/README.md#L39) —— 状态表中 TADD 一行六个 Yes：CPU/Costmodel/A2/A3/A5/Kirin 全部支持，属于覆盖最完整的指令之一。

#### 4.6.4 代码实践

**实践：用定位法追一条新指令（源码阅读型，10 分钟）**

1. 实践目标：把 TADD 的定位套路复用到 TMUL（或任选一条你感兴趣的指令），检验方法的通用性。
2. 操作步骤：
   ```bash
   grep -n "TMUL(" include/pto/common/pto_instr.hpp | head -3
   ls include/pto/cpu/ | grep -i tmul
   ls include/pto/npu/a2a3/ include/pto/npu/a5/ | grep -i tmul
   ls tests/cpu/st/testcase/ | grep -i tmul
   ls docs/isa/ | grep -i "^TMUL"
   grep -n "| \`TMUL\`" include/README.md
   ```
3. 需要观察的现象：六个落点是否都能命中？特别注意：有些指令可能在某个后端是 `TODO`（状态表可查），此时对应实现文件可能缺失或仅有占位——这本身就是有价值的信息。
4. 预期结果：能整理出一张与 4.6.3 同样结构的六行对照表。若某落点找不到，请在表中如实写「缺失」，并对照状态表解释原因（待本地验证具体指令的差异）。

#### 4.6.5 小练习与答案

**练习 1**：公共声明里 `MAP_INSTR_IMPL(TADD, dst, src0, src1)` 的作用是什么？

答案：它是一个宏，把公共 API 调用转发到当前被编译后端中名为 `TADD_IMPL` 的实现函数（CPU 版在 `cpu/TAdd.hpp`，a2a3 版在 `npu/a2a3/TAdd.hpp:81`，a5 版在 `npu/a5/TAdd.hpp:82`）。后端选择由 include 了哪些头决定，而非运行时判断。

**练习 2**：为什么 `include/pto/npu/a2a3/TAdd.hpp` 里要有 `TAddCheck` 这样一层 `static_assert`？

答案：把「类型一致、dtype 受支持、行主布局」等约束放在编译期拦截，错误直接出现在用户内核的编译日志里（提示以 "Fix:" 开头），而不是等到上板运行才失败。CPU 版对应的是运行期 `PTO_ASSERT`（[cpu/TAdd.hpp:68-73](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/cpu/TAdd.hpp#L68-L73)）。

**练习 3**：如果不记得文件名是 `TAdd.hpp` 还是 `TADD.hpp`，最稳的查找命令是什么？

答案：大小写不敏感的 find：`find include -iname "*tadd*"`；或 `grep -rn "TADD_IMPL" include/` 直接按实现函数名搜。通信指令还要多搜一处：`find pkg_inc -iname "*<指令>*"`。

## 5. 综合实践

**任务：绘制你的仓库地图 + TADD 全链路对照表 + pkg_inc 分工分析**

这是本讲的核心交付物，建议完成后保存下来，作为后续所有讲义的速查页。

**第一步：绘制带职责标注的目录树。**

1. 实践目标：产出一棵两级目录树，每个一级目录标注职责、每个 `include/pto/` 二级模块标注「放什么文件」。
2. 操作步骤：
   ```bash
   cd <仓库根目录>
   ls -d */ | sort                      # 一级目录
   ls include/pto/                      # 五大模块
   ls pkg_inc/pto/                      # 内部头目录（与 include/pto 同构）
   ls kernels/ demos/ tests/ docs/      # 主要二级结构
   ```
   然后手绘（或用文本编辑器）整理成树，格式参考 4.1.3 的表格。
3. 需要观察的现象：你画出的树与 [README.md:203-224](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/README.md#L203-L224) 的官方树有哪些出入（例如官方树未列出 `kernels/automode/`、`kernels/python/`、`pkg_inc/`、`tests/costmodel/`）。
4. 预期结果：一张你自己验证过、与本地 HEAD 一致的地图，而不是照抄 README。

**第二步：制作 TADD 全链路对照表。**

按下表格式填满每一格（路径必须真实存在，行号以当前 HEAD 为准）：

| 落点 | 路径 | 关键符号 | 你的一句话说明 |
| --- | --- | --- | --- |
| 公共 API 声明 | `include/pto/common/pto_instr.hpp` L175-L181 | `TADD(...)` | （自行填写） |
| CPU 实现 | `include/pto/cpu/TAdd.hpp` L63-L75 | `TADD_IMPL` | |
| A2/A3 实现 | `include/pto/npu/a2a3/TAdd.hpp` L20-L94 | `AddOp`/`TAdd`/`TAddCheck`/`TADD_IMPL` | |
| A5 实现 | `include/pto/npu/a5/TAdd.hpp` L24-L93 | `AddOp`/`TAdd`/`TAddCheck`/`TADD_IMPL`/`Int64Binary` | |
| CPU ST 测试 | `tests/cpu/st/testcase/tadd/` | `runTAdd` | |
| NPU ST 测试 | `tests/npu/a2a3/src/st/testcase/tadd/`、`tests/npu/a5/src/st/testcase/tadd/` | — | |
| ISA 文档 | `docs/isa/TADD.md`、`docs/isa/TADD_zh.md` | — | |
| 状态表 | `include/README.md` L39 | TADD 行 | |

**第三步：解释通信 notify 头的解析位置。**

1. 实践目标：用源码回答「TPutAsyncNotify 头文件为何从 `pkg_inc/` 目录解析」。
2. 操作步骤：
   ```bash
   grep -n "TPutAsyncNotify" include/pto/comm/pto_comm_instr_impl.hpp
   find include pkg_inc -name "TPutAsyncNotify.hpp"
   sed -n '1,15p' pkg_inc/README.md
   ls pkg_inc/pto/comm/async/rdma/
   ```
3. 需要观察的现象：include 路径是 `../../../pkg_inc/pto/comm/<arch>/async/TPutAsyncNotify.hpp`；`include/` 下已搜不到该文件；`pkg_inc/README.md` 说明了「不对外暴露的内部头」约定；`pkg_inc` 下还有整套 RDMA 后端头。
4. 预期结果：写出三句话的结论——(a) notify 头属于内部实现而非公共 API，按 CANN 打包约定放 `pkg_inc/`；(b) `cmake/package.cmake` 把它随 `include/` 一起安装，目录层级保持并列，因此三级相对路径 `../../../` 在源码树与安装树中都成立；(c) RDMA 传输后端同理放在 `pkg_inc/`，公共层只暴露 `pto_comm_inst.hpp` 入口。

**第四步（可选验证）：** 若已按 u1-l2 配好环境，运行 `python3 tests/script/run_st.py -r sim -v a3 -t tadd -g TADDTest.case_float_64x64_64x64`，确认第 5 落点的用例真的能被驱动脚本找到并执行（待本地验证）。

## 6. 本讲小结

- 仓库由五大内容目录组成：`include/`（交付的头文件）、`kernels/`（性能算子）、`demos/`（端到端示例）、`tests/`（三纵队测试 + 脚本层）、`docs/`（ISA 与开发文档），外加 `pkg_inc/`（内部头）、`scripts/`、`cmake/`、`build.sh` 等工程设施。
- `include/pto/` 按「common 平台无关层 + cpu/npu/comm/costmodel 四个后端」组织；声明永远在 `common/pto_instr.hpp`，实现按后端各归其位，由 `pto-inst.hpp` 按宏组装。
- `pkg_inc/` 存放不对外暴露的内部头，布局与 `include/pto/` 同构：本版本起通信 `TPutAsyncNotify` 各代实现与整套 RDMA 传输后端（含 HNS1825）迁入此处，公共层通过 `../../../pkg_inc/...` 相对路径引用。
- `npu/` 下按代际分目录：`a2a3/`（A2/A3 共用）、`a5/`、`a6/`（本版本恢复接入 `header.hpp`）、`kirin9030/` 及 Kirin 早期目录；`include/README.md` 状态表是「一条指令六个后端是否可用」的权威查询位置，A2/A3 两列恒同。
- ST 用例是「四件套」模板：`*_kernel.cpp` + `main.cpp` + `gen_data.py` + `CMakeLists.txt`，目录名即指令名，CPU 与 NPU 两侧结构平行；A5 用例本版本扩容至约 159 个。
- 指令定位法六个落点：公共声明 → CPU 实现 → a2a3 实现 → a5 实现 → ST 测试 → ISA 文档/状态表；计算类指令普遍适用，通信指令需额外考虑 `pkg_inc/` 落点。
- kernels 子目录是自包含小工程（README + CMakeLists + run.sh），阅读策略是「挑一个目录从 README 进入」，不必通读。

## 7. 下一步学习建议

下一讲（u1-l4）将拿着本讲的地图深入第 5 落点：逐行精读 `tests/cpu/st/testcase/tadd/` 的内核与测试骨架，理解 Tile 定义、TASSIGN 地址绑定和 set_flag/wait_flag 流水线同步——这是你读懂任何 PTO 内核的第一块拼图。之后 u1-l5 会剖析 `pto-inst.hpp` 的宏分发机制，把本讲 4.2.2 的「组装路径」展开成完整依赖图（包括本讲只点了一句的 `PTO_NPU_ARCH_A6` 两级宏选择）。

继续阅读建议：

- 想先鸟瞰全部指令：`docs/isa/README.md`（按类别索引）与 `include/README.md`（状态表）对照着看。
- 想深入内部头目录的来历与安装细节：`pkg_inc/README.md` 与 `cmake/package.cmake`。
- 想知道目录背后的工程约定：`CONTRIBUTING.md` 与 `kernels/README.md` 的 Notes 一节。
- 遇到目录相关问题（如「这个用例怎么跑」）：先查 `tests/README.md`，再查根 `FAQ.md`。
