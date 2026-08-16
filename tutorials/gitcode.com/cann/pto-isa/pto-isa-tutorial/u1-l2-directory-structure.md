# 源码目录结构导览：从仓库布局看懂项目分层

## 1. 本讲目标

上一讲（u1-l1）我们知道了 PTO 是什么、为什么存在。本讲解决一个更"物理"的问题：**这个仓库里都装了什么，东西放在哪**。学完本讲，你应该能够：

1. 画出 pto-isa 仓库的顶层目录地图，说出 `include`、`kernels`、`demos`、`tests`、`docs`、`scripts`、`cmake` 各自的职责。
2. 理解 `include/pto` 内部 `common / cpu / npu / comm / costmodel` 五个子目录的分工——也就是"同一套指令，五份实现"是怎么组织的。
3. 理解 `include/pto/pto-inst.hpp` 作为唯一统一入口的作用：一份 kernel 代码如何被路由到 CPU 仿真、NPU 真机或 CostModel 后端。

本讲不涉及任何指令的具体语义，只建立"地图感"。有了这张地图，后续单元精读任何一条指令时，你都能立刻知道该去哪个目录找它的 CPU 实现、NPU 实现和文档。

## 2. 前置知识

- **头文件库（header-only library）**：本仓库的核心交付物是一堆 C++ 头文件（`.hpp`），几乎不含 `.so`/`.a` 这样的编译产物。使用者 `#include` 头文件后，指令实现以 C++ 模板的形式在**使用者自己的编译单元里**展开。这解释了为什么仓库主体在 `include/` 下。
- **条件编译（宏路由）**：同一份源码，通过 `#if defined(...)` 这样的预处理指令，在不同构建配置下包含不同的文件，从而编译出不同后端的程序。PTO 用 `__CPU_SIM`、`__CCE_AICORE__`、`__COSTMODEL` 三个宏来选择后端（上一讲已提过，本讲会看到真实代码）。
- **AICORE**：昇腾 NPU 上负责运行算子代码的计算核心（AI Core）。编写 NPU kernel 时编译器会定义 `__CCE_AICORE__`，PTO 据此识别"当前在为真机编译"。
- **前缀约定**：PTO 指令名有固定前缀——`T` 开头表示 tile 级指令（如 `TADD`、`TLOAD`），`M` 开头表示矩阵级指令（如 `MGATHER`），`Set` 开头表示配置类指令（如 `SetQuantScalar`）。看文件名就能猜出指令类别。

## 3. 本讲源码地图

| 文件/目录 | 作用 |
|---|---|
| [README.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/README.md) | 项目主页文档，含 Quick Start 命令与推荐学习路径 |
| [include/README.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/README.md) | 公共头文件说明，含逐指令后端支持状态表 |
| [include/pto/pto-inst.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/pto-inst.hpp) | 统一入口头文件，做后端路由 |
| [include/pto/common/arch_macro.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/arch_macro.hpp) | 把 `__NPU_ARCH__` 数字翻译成 `PTO_NPU_ARCH_*` 架构宏 |
| [include/pto/common/pto_instr_impl.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_instr_impl.hpp) | 按后端/架构分组批量 include 各指令实现头文件 |
| [kernels/README.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/README.md) | kernels 目录的布局说明 |
| [CMakeLists.txt](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/CMakeLists.txt) | 顶层构建入口，主要是打包配置而非编译代码 |

## 4. 核心概念与源码讲解

### 4.1 目录地图：仓库顶层布局

#### 4.1.1 概念说明

一个开源项目最重要的"自解释信息"就是它的目录布局。pto-isa 的顶层目录可以按"交付物类型"分成四组：

| 分组 | 目录 | 装的是什么 |
|---|---|---|
| 核心交付物 | `include/` | PTO 指令的头文件实现（CPU/NPU/CostModel 多后端） |
| 示例与算子 | `kernels/`、`demos/` | 完整算子工程（kernel + host + 脚本）和入门 demo |
| 质量保障 | `tests/` | CPU ST 用例、CostModel 测试、NPU 测试及运行脚本 |
| 文档与工程 | `docs/`、`scripts/`、`cmake/`、`build.sh`、`setup.py` | ISA 文档、安装脚本、CMake 模块、打包配置 |

各目录的关键子结构（均为实际勘察结果）：

- **`include/pto/`**：五个子目录 `common`（跨后端公共定义）、`cpu`（CPU 仿真实现）、`npu`（真机实现，下分 `a2a3`、`a5`、`a6`、`kirin9030` 等架构目录）、`comm`（通信扩展指令，下分 `a2a3`、`a5`、`async`）、`costmodel`（性能模拟实现），外加统一入口 `pto-inst.hpp`。
- **`kernels/`**：分 `manual`（手工调优的 NPU kernel，如 `manual/a2a3/gemm_performance`、`manual/a2a3/topk`、`manual/a5/matmul_mxfp4_performance`、`manual/common/flash_atten`）、`custom`（自定义算子脚手架，如 `fused_add_relu_mul`）、`automode`（Auto Mode 示例）。每个子目录基本是自带 `README.md`、`CMakeLists.txt`、`run.sh` 的迷你工程。
- **`demos/`**：入门演示，分 `baseline`（add、gemm_basic、flash_atten 等基线实现）、`auto_mode`、`cpu`、`torch_jit`。
- **`tests/`**：`cpu/st/testcase/` 下按指令名组织的 ST 用例、`costmodel/`、`npu/`，以及 `run_cpu.py`、`run_st.sh`、`run_costmodel.py`、`validate_op_coverage.py` 等脚本。
- **`docs/`**：`isa/`（逐指令 ISA 文档，如 `TADD.md`）、`coding/`（编程模型与调优文档）、`auto_mode/`、`costmodel` 相关文档、`getting-started.md`、`mkdocs/`（文档站构建）。
- **`scripts/`**：`install_pto.sh`（安装）、`oat_check.sh`（合规检查）、`package/`（打包模板）。
- **`cmake/`**：`fetch_cann_cmake.cmake`、`func.cmake`、`package.cmake`、`a5_vf_mock.cmake` 等 CMake 模块。

#### 4.1.2 核心流程

一个开发者接触本仓库的典型路径：

```text
读 README.md（定位与 Quick Start）
    → 跑 tests/run_cpu.py（CPU 仿真验证环境）
    → 抄 demos/baseline/add（写第一个算子）
    → 查 docs/isa/*.md（指令语义）
    → 去 include/pto/{cpu,npu}/ 找指令实现
    → 参考 kernels/manual/* 学调优
    → 用 tests/ 回归
```

#### 4.1.3 源码精读

README 的 Quick Start 一节给出了所有关键入口命令，这本身就是一张"目录用途速查表"：

[README.md:65-86](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/README.md#L65-L86) —— 这段依次演示了 `tests/run_cpu.py`（CPU 仿真总入口）、`tests/script/run_st.py`（跑单个 ST 用例）、`build.sh --run_all --a3 --sim`（一键构建运行）和 `python3 -m build --wheel`（打 wheel 包）。每个命令都对应本讲地图里的一个目录。

kernels 目录的官方自述直接说明了"迷你工程"约定：

[kernels/README.md:5-6](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/README.md#L5-L6) —— 说明 kernels 下大多数子目录是**自包含迷你工程**（kernel + host + 脚本），各有自己的 README、CMakeLists 和 run.sh。这意味着你学习任何一个算子时，只需进入它的目录即可获得全部上下文，不需要理解全局构建细节。

[kernels/README.md:17-30](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/README.md#L17-L30) —— 目录布局清单：`manual/a2a3/`（gemm_performance、conv2d_forward、topk 等）、`manual/a5/`（flash_atten、matmul_mxfp4/mxfp8_performance）、`manual/common/`（跨平台 flash_atten）、`custom/`（自定义算子扩展脚手架）。**按架构再分类**是 kernels 的组织主键。

顶层 [CMakeLists.txt:11-29](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/CMakeLists.txt#L11-L29) —— 注意这份顶层 CMake 几乎不编译任何 C++ 代码，只做三件事：引入 `cmake/` 下的 CANN 构建模块（`fetch_cann_cmake.cmake`、`package.cmake`、`func.cmake`）、设置包类型（run/rpm/deb）、调用 `pack_built_in()` 打包。这再次印证：**本仓库的产物是头文件，真正的编译发生在使用者的工程里**。

#### 4.1.4 代码实践

1. **实践目标**：建立顶层目录的肌肉记忆，能不查资料说出每个目录的职责。
2. **操作步骤**：
   ```bash
   cd <仓库根目录>
   ls                      # 看顶层
   ls include/pto          # 看核心库的五个子目录
   ls kernels/manual       # 看 manual 下按架构的划分
   ls demos/baseline       # 看入门 demo 列表
   ls docs/isa | head      # 瞄一眼 ISA 文档命名
   ```
3. **需要观察的现象**：`docs/isa` 下的文档文件名几乎都是大写指令名加 `.md`（如 `MGATHER.md`、`TADD.md`），且中英成对（`xxx.md` / `xxx_zh.md`）；`kernels/manual` 下第一层是 `a2a3`、`a5`、`common` 三种架构维度。
4. **预期结果**：你能对着 `ls` 输出，把每个目录归入 4.1.1 表格的四个分组中。
5. 命令本身无副作用，可放心执行；输出细节以本地仓库版本为准（**待本地验证**具体文件列表，仓库在持续新增指令）。

#### 4.1.5 小练习与答案

**练习 1**：你想找 `TADD` 指令的中文语义说明，应该去哪个目录、找什么文件？

答案：去 `docs/isa/` 目录找 `TADD_zh.md`（英文版为 `TADD.md`）。PTO 的 ISA 文档按指令名大写命名，中英文成对出现。

**练习 2**：`demos/baseline/add` 和 `kernels/manual/a2a3/gemm_performance` 都是完整算子工程，它们的定位差别是什么？

答案：`demos/baseline` 是**教学基线**，展示最朴素直白的写法，帮助理解指令如何组织；`kernels/manual` 是**手工调优的参考实现**，包含 double buffer、多核切分等性能手段，面向性能工程师。前者重"读懂"，后者重"调优"。

**练习 3**：为什么顶层 `CMakeLists.txt` 里看不到编译 `include/pto/cpu/TAdd.hpp` 之类的规则？

答案：因为本仓库是 header-only 库，`include/` 下的头文件不单独编译成库；它们在使用者（或测试用例）的编译单元中通过模板展开。顶层 CMake 主要负责 CANN 包的构建配置与打包。

### 4.2 头文件组织：include/pto 的五层分工

#### 4.2.1 概念说明

上一讲说过，PTO 的核心价值是"一套指令 API，多个后端实现"。这个承诺在目录结构上的落地就是 `include/pto` 的五个子目录：

| 子目录 | 职责 | 典型内容 |
|---|---|---|
| `common/` | 跨后端公共定义 | `pto_tile.hpp`（Tile/GlobalTensor 类型）、`event.hpp`（事件）、`type.hpp`（数据类型）、`arch_macro.hpp`（架构宏）、`pto_instr.hpp`（API 声明层）、`cpu_stub.hpp`（CPU 桩） |
| `cpu/` | **CPU 仿真后端**：用普通 C++ 在主机内存里模拟每条指令的行为 | `TAdd.hpp`、`TLoad.hpp`、`NPUMemoryModel.hpp`、`ElementTileOp.h`（通用逐元素骨架） |
| `npu/` | **真机后端**：把指令映射到昇腾硬件 intrinsic | `a2a3/`（约 118 个 `.hpp`，A2/A3 共用）、`a5/`、`a6/`、`kirin9030/` 等架构子目录 |
| `comm/` | **通信扩展指令集**：跨 NPU 的点对点/集合/信号同步 | `a2a3/`（TGet、TPut、TNotify、TWait、TReduce、TBroadCast 等）、`async/`（异步变体）、`comm_types.hpp` |
| `costmodel/` | **性能模拟后端**：不执行计算，只估算指令代价 | `lightweight_costmodel.hpp`、`perf_sim/`（pipe_model、latency、recorder）、`runtime_stub.hpp` |

关键的对应关系是：**同一条指令通常有多个同名文件分布在不同后端目录**。例如 `TADD` 有 `include/pto/cpu/TAdd.hpp`（仿真）与 `include/pto/npu/a2a3/TAdd.hpp`（真机）；`TLOAD` 还会再多一份 `include/pto/npu/a5/TLoad.hpp`。文件路径本身就是"指令 × 后端 × 架构"的三维坐标。

逐指令到底哪个后端支持、哪个还是 TODO，由一张官方状态表维护：

[include/README.md:24-31](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/README.md#L24-L31) —— 这里定义了状态表的六列：CPU（`__CPU_SIM`）、Costmodel（`__COSTMODEL`）、A2/A3（共用 `npu/a2a3/` 实现）、A5（`npu/a5/`）、Kirin（`npu/kirin9030/`）。这是判断"我想用的指令在我的目标硬件上能不能用"的权威入口。

#### 4.2.2 核心流程

从"一条指令"到"它所有实现"的查找流程：

```text
知道指令名（如 TADD）
    → docs/isa/TADD.md           查语义与约束（是什么）
    → include/pto/cpu/TAdd.hpp   查 CPU 仿真实现（怎么模拟）
    → include/pto/npu/a2a3/TAdd.hpp / npu/a5/... 查真机实现（怎么映射硬件）
    → tests/cpu/st/testcase/tadd 查测试用例（怎么验证）
    → include/README.md 状态表  确认各后端支持情况
```

后端选择则发生在编译期，由两个层次的条件编译完成：

```text
pto-inst.hpp
  ├─ __CPU_SIM      → 包含 common/cpu_stub.hpp（CPU 桩）
  ├─ __COSTMODEL    → 包含 costmodel/runtime_stub.hpp（CostModel 桩）
  └─ 两者都含       → common/arch_macro.hpp 把 __NPU_ARCH__ 翻译成 PTO_NPU_ARCH_*
pto_instr_impl.hpp
  ├─ PTO_NPU_ARCH_A2A3 → 批量 include npu/a2a3/*.hpp
  ├─ PTO_NPU_ARCH_A5   → 批量 include npu/a5/*.hpp
  ├─ PTO_NPU_ARCH_A6 / KIRIN* → 对应架构目录
  └─ __CPU_SIM         → 批量 include cpu/*.hpp
```

#### 4.2.3 源码精读

架构宏的翻译逻辑全部集中在一个 46 行的小文件里：

[include/pto/common/arch_macro.hpp:19-38](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/arch_macro.hpp#L19-L38) —— 这段把编译器传入的 `__NPU_ARCH__` 数字映射为语义化架构宏：`2201 → PTO_NPU_ARCH_A2A3`；`3101/3510 → PTO_NPU_ARCH_A5`（其中 3510 额外打开 `PTO_URMA_SUPPORTED`）；`3113/3003/5101/9201` 分别对应 Kirin 系列与 A6，并且这几档都会定义 `PTO_COMM_NOT_SUPPORTED`（不支持通信指令集）。后续所有指令实现文件只认 `PTO_NPU_ARCH_*`，不直接碰数字。

真正"按后端批量拉人头"的是 `pto_instr_impl.hpp`：

[include/pto/common/pto_instr_impl.hpp:18-21](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_instr_impl.hpp#L18-L21) —— `#ifdef PTO_NPU_ARCH_A2A3` 之下开始连续 include `pto/npu/a2a3/TAssign.hpp`、`TAdd.hpp`、`TLoad.hpp`、`TMatmul.hpp` 等真机实现。A5、A6、Kirin 各有对应的 `#ifdef` 块（A6/Kirin 通过各架构目录的 `header.hpp` 聚合引入，见 [pto_instr_impl.hpp:330-345](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_instr_impl.hpp#L330-L345)）。

[include/pto/common/pto_instr_impl.hpp:359-370](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_instr_impl.hpp#L359-L370) —— `#ifdef __CPU_SIM` 之下换成批量 include `pto/cpu/` 下的仿真实现（TSync、TAdd、TLoad、TMatmul、TMrgSort……）。**同一个 `#include` 名字，两条互斥分支**——这就是"一份 kernel 代码、多后端实现"在预处理层面的实现方式。

还有一个值得注意的细节，展示了三种宏如何协同：

[include/pto/common/pto_instr_impl.hpp:350-357](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_instr_impl.hpp#L350-L357) —— `TPrefetchAsync` 的引入条件是 `defined(__CCE_AICORE__) && !(defined(__CPU_SIM) || defined(__COSTMODEL__))`，即"确认真机编译、且不是仿真/造价模型"才引入 NPU 版本；注释还说明 CPU 仿真与 CostModel 会从各自分支拿到自己的变体。可见 `__CPU_SIM` 优先级高于 `__CCE_AICORE__`（CPU 仿真构建里两者可能同时被定义，需要显式排除）。

#### 4.2.4 代码实践

1. **实践目标**：直观感受"同一指令、多份实现"的目录分布。
2. **操作步骤**：
   ```bash
   # 列出 TADD 的所有实现位置
   find include -name 'TAdd.hpp'
   # 列出 TLOAD 的所有实现位置（应跨越 cpu 与多个 npu 架构目录）
   find include -name 'TLoad.hpp'
   # 打开状态表，数一数 A5 列里有多少 TODO
   grep -c 'TODO' include/README.md
   ```
3. **需要观察的现象**：`TAdd.hpp` 至少出现在 `include/pto/cpu/` 与 `include/pto/npu/a2a3/` 两处；`TLoad.hpp` 的分布更广（cpu、a2a3、a5、a6 等）。
4. **预期结果**：你会得到一个"指令 → 后端文件"的多对多映射的直观印象，并且发现部分指令在某些后端缺失（状态表中标 TODO）——这正是 include/README.md 状态表存在的意义。
5. 具体命中数量随仓库版本变化，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`include/pto/common/` 和 `include/pto/cpu/` 都有文件，为什么 `pto_tile.hpp`（Tile 类型定义）必须放在 `common` 而不是 `cpu`？

答案：因为 `pto_tile.hpp` 定义的是 `Tile`、`GlobalTensor` 等类型抽象，CPU、NPU、CostModel 三个后端都要用同一套类型；放在 `common` 才能被所有后端的实现文件无差别引用。`cpu/` 下的文件只服务 CPU 仿真后端。

**练习 2**：`arch_macro.hpp` 中为什么 Kirin 系列和 A6 都定义了 `PTO_COMM_NOT_SUPPORTED`？

答案：这表示这些架构当前不支持 PTO 通信扩展指令集。配合 `pto_instr.hpp`/`pto_instr_impl.hpp` 中的条件编译（如 [pto_instr_impl.hpp:19-21](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_instr_impl.hpp#L19-L21) 处 `#if !defined(__COSTMODEL) && !defined(PTO_COMM_NOT_SUPPORTED)` 才引入 `pto/comm/pto_comm_inst.hpp`），可以保证在不支持通信的架构上编译含通信调用的代码时干净地报错或裁剪。

**练习 3**：如果你要在 A5 上用一条指令，但状态表里 A5 列是 TODO，CPU 列是 Yes，这意味着什么？

答案：意味着你可以先用 CPU 仿真（`__CPU_SIM`）开发和验证使用该指令的 kernel 逻辑，但在 A5 真机上还没有对应实现，暂时无法编译运行真机版本。"先仿真落地、再随硬件落地"正是 PTO 新特性的演进节奏（见上一讲 News 一节）。

### 4.3 统一入口：pto-inst.hpp 的后端路由

#### 4.3.1 概念说明

如果每个后端要 include 不同的头文件集合，"一次编写、多后端运行"就无从谈起。所以 PTO 规定：**上层代码只 include 一个头文件**——

```cpp
#include <pto/pto-inst.hpp>
```

这个 33 行的入口头根据构建宏自动把正确的后端实现拉进来。使用者唯一的"后端选择动作"是在构建时定义 `__CPU_SIM`（CPU 仿真）、`__COSTMODEL`（性能模拟）或什么都不做（由 CANN 工具链为真机编译时自动定义 `__CCE_AICORE__` 与 `__NPU_ARCH__`）。

#### 4.3.2 核心流程

```text
使用者的 kernel 代码：  #include <pto/pto-inst.hpp>
                          │
        构建宏 ──────────┼──────────────┐
        __CPU_SIM        │              │（真机构建）
          ↓              ↓              ↓
   cpu_stub.hpp    costmodel/runtime_stub.hpp   （NPU 路径无需额外桩）
                          │
                          ▼
        三种后端共同包含：arch_macro / arch_capability / pto_tile
                          │
          ┌───────────────┴────────────────┐
       __COSTMODEL                      其余（CPU_SIM 或 AICORE）
   costmodel/pto_instr.hpp         common/pto_instr.hpp
   （造价模型版指令实现）          （内部再按架构宏分发到 cpu/ 或 npu/）
```

#### 4.3.3 源码精读

整个路由逻辑只有十几行，值得逐行读懂：

[include/pto/pto-inst.hpp:16-20](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/pto-inst.hpp#L16-L20) —— 第一层路由：定义了 `__CPU_SIM` 就引入 CPU 桩 `pto/common/cpu_stub.hpp`；定义了 `__COSTMODEL` 就引入 `pto/costmodel/runtime_stub.hpp`。这两个"桩"头为各自后端提供运行时支撑（如内存模拟、指令记录）。

[include/pto/pto-inst.hpp:23-32](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/pto-inst.hpp#L23-L32) —— 第二层路由：只要处于三种后端之一（`__CPU_SIM || __CCE_AICORE__ || __COSTMODEL`），就统一包含 `arch_macro.hpp`、`arch_capability.hpp`、`pto_tile.hpp`（公共类型与架构宏），随后二选一——CostModel 走 `pto/costmodel/pto_instr.hpp`，其余走 `pto/common/pto_instr.hpp`（API 声明层，内部再经由 `pto_instr_impl.hpp` 按架构宏批量引入具体实现，见 4.2.3）。

而 API 声明层的样子，可以看 `common/pto_instr.hpp` 中的两个例子：

[include/pto/common/pto_instr.hpp:27-31](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_instr.hpp#L27-L31) —— `TASSIGN` 的声明：`namespace pto` 下的模板函数，函数体只有一行 `MAP_INSTR_IMPL(TASSIGN, obj, addr)`，即宏拼接成 `TASSIGN_IMPL(obj, addr)`。**API 名与实现名只差一个 `_IMPL` 后缀**，后端实现文件提供 `TASSIGN_IMPL`，API 层负责统一签名。这就是"声明在 common、实现在各后端"的粘合机制。

[include/pto/common/pto_instr.hpp:46-50](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_instr.hpp#L46-L50) —— `TSYNC` 同样是 `TSYNC_IMPL<OpCode>()` 的一行转发。理解这个模式后，你在任何后端目录里找一条指令的实现，就是找"`指令名_IMPL`"这个符号。

#### 4.3.4 代码实践

1. **实践目标**：亲手验证"一个 include、三种编译产物"。
2. **操作步骤**（示例代码，非项目原有文件）：
   ```cpp
   // /tmp/route_probe.cpp（示例代码）
   #include <pto/pto-inst.hpp>
   int main() { return 0; }
   ```
   然后分别预处理：
   ```bash
   g++ -std=c++20 -I include -D__CPU_SIM -E /tmp/route_probe.cpp \
       | grep -o 'pto/cpu/[A-Za-z0-9_]*\.hpp' | sort -u | head
   g++ -std=c++20 -I include -D__CPU_SIM -E /tmp/route_probe.cpp \
       | grep -c 'pto/npu/'
   ```
3. **需要观察的现象**：第一条命令应输出一串 `pto/cpu/TAdd.hpp` 之类的仿真实现路径；第二条命令统计 NPU 头文件的引入次数，在 `__CPU_SIM` 构建下应为 0（或极少）——证明 CPU 构建没有把真机实现拉进来。
4. **预期结果**：不同宏组合下，实际进入编译的头文件集合完全不同，而 `/tmp/route_probe.cpp` 一行都没改。
5. 本实践需要本地有 C++20 编译器且在仓库根目录执行；具体输出**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么不把 `pto-inst.hpp` 直接做成"include 所有后端所有文件"，让条件编译自己选？

答案：一是编译效率——把上百个不相关后端的头全部拉入会显著拖慢编译；二是正确性——不同后端的实现符号同名（都叫 `TADD_IMPL`），同时引入会直接重定义冲突。所以必须由入口头保证任一构建配置下只有一套实现可见。

**练习 2**：你在 CPU 仿真下调用 `pto::TASSIGN(tile, addr)`，请说出从 API 到实现的完整链路。

答案：`pto-inst.hpp`（`__CPU_SIM` 分支引入 cpu_stub）→ `common/pto_instr.hpp` 中 `TASSIGN` 模板（[pto_instr.hpp:27-31](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_instr.hpp#L27-L31)）→ 宏展开为 `TASSIGN_IMPL(obj, addr)` → `pto_instr_impl.hpp` 的 `#ifdef __CPU_SIM` 分支引入 `pto/cpu/TAssign.hpp`，其中定义了 CPU 版 `TASSIGN_IMPL`。

**练习 3**：`include/pto/README.md`（子目录内的那份）和 `include/README.md`（外层的）内容侧重有何不同？

答案：外层 [include/README.md:5-14](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/README.md#L5-L14) 讲"如何开始使用统一入口 + 目录布局"，并维护逐指令后端状态表；`include/pto/README.md` 则深入解释后端选择机制与各子目录细节。前者面向初次接入的使用者，后者面向想理解内部组织的开发者。（细节**待确认**，建议本地对照阅读两份 README。）

## 5. 综合实践

把本讲三个模块串成一个任务：**为后续学习制作一张"我的指令追踪表"**。

1. 在本地执行：
   ```bash
   ls include/pto/npu/a2a3
   ```
   统计其中 `.hpp` 头文件的数量（写作本文时约为 118 个，另含 `README.md`、`README_zh.md` 与 `custom/` 子目录，以本地实际输出为准）。
2. 从列表中挑出 3 个你感兴趣的指令名记下来（建议一个 `T` 开头计算类、一个 `Set` 开头配置类、一个 `M` 开头矩阵类，例如 `TMrgSort`、`SetQuantScalar`、`MGather`）。
3. 对每个指令，填写一张四行小表：
   - ISA 文档路径（提示：`docs/isa/<大写指令名>.md`，用 `ls docs/isa | grep -i <名字>` 确认）；
   - CPU 实现路径（提示：`include/pto/cpu/<指令名>.hpp`，不存在则记"无"）；
   - NPU 实现路径（提示：`include/pto/npu/a2a3/<指令名>.hpp` 与 `npu/a5/` 下是否都有）；
   - 在 `include/README.md` 状态表中的各后端支持情况。
4. 交付物：这张表就是你后续单元的"预习清单"——单元四、单元五会逐个精读这些指令，届时直接在你的表上补充语义笔记。

## 6. 本讲小结

- 仓库顶层按交付物分为四组：核心头文件库 `include/`、算子与演示 `kernels/` + `demos/`、测试 `tests/`、文档与工程设施 `docs/` + `scripts/` + `cmake/` + `build.sh`。
- `include/pto` 内部五层分工：`common`（跨后端公共类型与 API 声明）、`cpu`（仿真实现）、`npu`（按 a2a3/a5/a6/kirin 架构分目录的真机实现）、`comm`（通信扩展指令）、`costmodel`（性能模拟）。
- 同一条指令是"指令 × 后端 × 架构"三维坐标下的多个同名文件；查支持情况看 `include/README.md` 的状态表。
- `arch_macro.hpp` 把 `__NPU_ARCH__` 数字翻译成 `PTO_NPU_ARCH_*` 宏，`pto_instr_impl.hpp` 再按宏批量 include 对应实现。
- `pto/pto-inst.hpp` 是唯一统一入口：`__CPU_SIM` / `__COSTMODEL` / 真机构建三条路径各自动态选择头文件集合，使用者代码一行不改。
- API 层（`common/pto_instr.hpp`）与实现层的粘合约定是 `XXX` → `XXX_IMPL` 宏转发。

## 7. 下一步学习建议

下一讲（u1-l3「环境搭建与 CPU 仿真快速上手」）将把这张地图跑起来：用 `tests/run_cpu.py` 和 `build.sh` 构建 CPU 仿真环境并运行 gemm、flash_attn 演示。在此之前，建议你：

- 通读 [include/README.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/README.md) 的指令支持状态表，感受 90+ 指令的后端覆盖全景。
- 浏览 [include/pto/common/pto_tile.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_tile.hpp) 开头的类型定义，混个眼熟——单元二将正式精读 GlobalTensor 与 Tile。
