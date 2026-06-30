# 仓库目录结构总览

## 1. 本讲目标

上一讲（[u1-l1](u1-l1-project-overview.md)）我们从三份文档认识了 CoralNPU 是什么。本讲把视角落到**代码仓库本身**：它把近 1000 个文件按什么逻辑组织？哪些目录是硬件设计、哪些是软件、哪些是验证？

学完本讲，你应当能够：

- 在 30 秒内说出每个**顶层目录**的一行职责。
- 分清 `hdl/chisel`（Scala 写的标量核/SoC）与 `hdl/verilog/rvv`（SystemVerilog 写的向量/矩阵后端）两套硬件实现各管什么。
- 定位 `sw`、`tests`、`examples`、`toolchain`、`doc` 等目录，知道要找的东西去哪里翻。
- 用 `git ls-files` 这种只读命令快速建立任意大型仓库的全景认知。

本讲是后续所有源码讲义的「地图」——读懂这张地图，后面每篇讲义你都能立刻定位它在仓库里的位置。

## 2. 前置知识

阅读本讲前，建议你已经了解：

- **什么是源码仓库**：一个项目把代码、文档、配置组织成一棵目录树，用 Git 追踪。
- **Chisel 与 SystemVerilog 的区别（一句话版）**：Chisel 是嵌在 Scala 里的硬件构造语言，写法像编程、最终生成 Verilog；SystemVerilog 是传统的硬件描述语言。CoralNPU **同时**用了两者，各负责一部分硬件——这是它目录结构最大的特点。
- **Bazel 是什么**：Google 出品的构建工具，用 `BUILD`/`BUILD.bazel` 文件声明「怎么把源码变成可运行的产物」。我们在 [u1-l3](u1-l3-bazel-build-quickstart.md) 会深入它，本讲只需知道它的配置文件长什么样。
- **上一讲建立的认知**：CoralNPU = 标量核 + 向量核(SIMD) + 矩阵 MAC 引擎，三者协同（见 [[u1-l1-summary]]）。本讲的目录划分几乎就是沿这条「三核一体」的主线切出来的。

> 名词速查：**RTL**（Register Transfer Level）= 寄存器传输级硬件代码，本仓库里指 Chisel/SystemVerilog 这类硬件设计文件。**IP**（Intellectual Property）= 可复用的硬件模块，这里指 CoralNPU 这个可被集成进别人 SoC 的「IP 核」。

## 3. 本讲源码地图

本讲的「源码」主要是仓库的**目录结构**和**顶层配置文件**。下表列出本讲涉及的关键文件及其作用：

| 路径 | 类型 | 作用 |
|------|------|------|
| `README.md` | 文档 | 项目门面：定位、特性清单、系统要求、Quick Start |
| `BUILD.bazel` | 配置 | 工作区根的（近乎为空的）Bazel 构建文件 |
| `WORKSPACE` | 配置 | 声明 Bazel 工作区与外部依赖 |
| `.bazelrc` / `.bazelversion` | 配置 | Bazel 默认构建选项 / 钉死的 Bazel 版本 |
| `CONTRIBUTING.md` | 文档 | 如何向项目提交贡献（CLA、代码评审） |
| `git ls-files` 输出 | 命令 | 列出所有被 Git 追踪的文件，是建立目录地图的利器 |

> 说明：本讲引用的永久链接主要指向上面这些**真实存在的顶层文件**。对各目录内部的文件，我们用「目录树 + 文件计数」来描述，这些数据来自在仓库根目录实际执行的 `git ls-files`（见综合实践），不存在编造。

## 4. 核心概念与源码讲解

按「根 → 硬件 → 软件 → 验证/支撑」的顺序，我们把仓库拆成 4 个最小模块。

### 4.1 工作区根：顶层文件与 Bazel 配置

#### 4.1.1 概念说明

任何一个大型项目，读懂它的第一步都是看**仓库根目录**。CoralNPU 的根目录同时躺着两类东西：

- **门面文件**：`README.md`、`LICENSE`、`CONTRIBUTING.md`——告诉访客「这是什么、能不能用、怎么参与」。
- **构建配置**：`BUILD.bazel`、`WORKSPACE`、`.bazelrc`、`.bazelversion`——告诉 Bazel「这是一个 Bazel 工作区，构建规则如下」。

这些根文件本身代码量很小，但它们定义了整个仓库的「身份和入口」。理解它们，就理解了项目怎么被组织起来。

#### 4.1.2 核心流程

建立一个仓库的全局认知，推荐的阅读流程是：

1. 读 `README.md` → 知道项目定位和最小运行命令（Quick Start）。
2. 看 `BUILD.bazel` / `WORKSPACE` → 确认构建系统是 Bazel。
3. 看 `.bazelversion` → 知道该用哪个版本的 Bazel。
4. 跑 `git ls-files | cut -d/ -f1 | sort | uniq -c` → 按一级目录统计文件数，得到一张「哪个目录最重」的全景图。
5. 对感兴趣的大目录，继续向下钻一层。

#### 4.1.3 源码精读

**README 的核心定位**——开头三句话把项目是什么说清楚了：

[README.md:1-5](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/README.md#L1-L5)：定义 Coral NPU 是面向超低功耗可穿戴 SoC 的开源 ML 加速器 IP，基于 32 位 RISC-V。

[README.md:7](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/README.md#L7)：点明「三个处理器组件协同工作：matrix、vector(SIMD)、scalar」。这一句是后续 `hdl/` 目录划分为标量/向量两套实现的根因。

README 的**特性清单**浓缩了硬件参数，目录结构里的很多目录就是为了支撑这些特性：

[README.md:15-23](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/README.md#L15-L23)：列出 `rv32imf_zve32x` 指令集、四发射、8KB ITCM、32KB DTCM、AXI4 双向接口等特性。

README 的**系统要求**——注意这里写的是 Bazel 7.4.1：

[README.md:25-29](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/README.md#L25-L29)：声明系统要求（Bazel 7.4.1、Python 3.9–3.12、SRecord）。

> ⚠️ 一个值得留意的细节：README 这里写「Bazel 7.4.1」，但仓库根的 `.bazelversion` 实际钉死的是 `8.6.0`（见本模块小练习）。两者不完全一致——以 `.bazelversion` 为准是更稳妥的做法。这类「文档与配置漂移」在真实项目里很常见，正好是练习「交叉验证」的好素材。

README 的 **Quick Start**——这是整个项目「跑起来」的最短路径：

[README.md:31-45](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/README.md#L31-L45)：四步 Quick Start，分别跑测试、编译二进制、编译仿真器、在仿真器上运行二进制。

**根目录的 Bazel 配置**——`BUILD.bazel` 只有一行注释，说明根目录本身不直接产出构建目标：

[BUILD.bazel:1](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/BUILD.bazel#L1)：`# Empty BUILD file for the workspace root.`——工作区根的空构建文件。

**贡献指南**——`CONTRIBUTING.md` 说明本项目用 Gerrit 做代码评审，而不是常见的 GitHub PR：

[CONTRIBUTING.md:18-23](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/CONTRIBUTING.md#L18-L23)：所有提交都要经过 Gerrit 评审。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**（不编译、只观察），目标是验证你对「文档 vs 配置」一致性的判断力。

1. **实践目标**：核对 README 声明的 Bazel 版本与实际钉死的版本是否一致。
2. **操作步骤**：
   - 打开 [README.md:27](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/README.md#L27)，读出它写的 Bazel 版本。
   - 打开仓库根的 `.bazelversion` 文件，读出实际钉死的版本。
3. **需要观察的现象**：两个数字是否相同。
4. **预期结果**：README 写 `7.4.1`，`.bazelversion` 写 `8.6.0`——两者不同。
5. **结论**：当本地实际构建时，Bazel 会按 `.bazelversion` 自动切到 `8.6.0`（若装了 Bazelisk/版本管理器）；README 的 `7.4.1` 可能是历史遗留。以 `.bazelversion` 为准。这一步如果无法本地复现，记为「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：仓库根目录除了 `README.md` 和 `BUILD.bazel`，还有哪些**文件**（非目录）？至少说出 4 个。
**答案**：`WORKSPACE`、`.bazelrc`、`.bazelversion`、`CONTRIBUTING.md`、`LICENSE`、`PREUPLOAD.cfg`、`jtag-sim.cfg`、`run_isp_cam_sim.sh`、`.gitignore`（任选 4 个即可）。可用 `git ls-files | grep -v /` 一次列全。

**练习 2**：为什么根目录的 `BUILD.bazel` 几乎是空的（只有一行注释）？
**答案**：因为根目录本身不直接产出构建产物。Bazel 工作区根需要一个 `WORKSPACE` 文件来声明工作区身份，根目录的 `BUILD.bazel` 只是占位；真正的构建目标分散在 `hdl/`、`tests/`、`examples/` 等子目录各自的 `BUILD`/`BUILD.bazel` 里。

**练习 3**：README 的 Quick Start 里出现了 `//tests/...`、`//examples/...`、`//tests/verilator_sim:...` 这样的路径。请据此推断：本仓库哪些目录是 Bazel 的「包（package）」会包含构建目标？
**答案**：至少 `tests/cocotb`、`tests/verilator_sim`、`examples` 是包，它们各自有 `BUILD`/`BUILD.bazel` 文件。`//` 开头是 Bazel 的「从工作区根算起的目标标签」语法，斜杠分隔的就是目录路径。

---

### 4.2 硬件代码双轨制：`hdl` 目录

#### 4.2.1 概念说明

`hdl/`（Hardware Description Language，硬件描述语言）是整个仓库最重的目录，约 313 个文件。它最特别的一点是：**硬件设计被拆成两套语言、两条流水线**。

- `hdl/chisel/`：用 **Chisel（Scala）** 写的部分——标量核、SoC、总线、外设、Cache、TCM、参数系统等。这部分是「自研主体」，生成 Verilog 后再综合。
- `hdl/verilog/`：用 **SystemVerilog** 写的部分——主要是 `rvv/` 下的 **RVV 向量/矩阵后端**（包括 MAC 外积引擎），以及少量公共模块（`Sram.v`、`ClockGate.sv`、`RstSync.sv`）。

为什么要双轨？上一讲提到 CoralNPU 的「三核一体」：标量核是通用 RISC-V，适合用 Chisel 这类高层硬件语言快速迭代；而向量/矩阵 MAC 引擎追求极致的算力密度（256 MACs/周期），用 SystemVerilog 手写能更精细地控制时序和面积。所以这两套实现是**按处理器组件分工**的，不是冗余。

#### 4.2.2 核心流程

理解 `hdl/` 目录的递进路线：

```
hdl/
├── chisel/                 # Scala/Chisel 写的硬件（生成 Verilog）
│   └── src/
│       ├── coralnpu/       # 59 个文件：标量核前端/执行/存储 + rvv 桥接
│       ├── common/         # 34 个文件：共享模块（寄存器堆、FMA、除法器等）
│       ├── bus/            # 28 个文件：AXI / TileLink-UL / DMA / 外设
│       ├── soc/            # 7 个文件：SoC 顶层装配、Xbar、配置
│       └── peripherals/    # 3 个文件：外设接口抽象
└── verilog/                # SystemVerilog 写的硬件（手写）
    ├── rvv/                # 170 个文件：RVV 向量/矩阵后端
    │   ├── design/         # 58 个：译码/派发/VRF/ROB/MAC/ALU 等执行单元
    │   ├── sve/            # 79 个：SystemVerilog 验证环境（testbench/UVM）
    │   ├── common/         # 19 个：公共定义
    │   ├── inc/            # 10 个：头文件（opcode/define）
    │   └── com/            # 2 个：公共组件
    ├── Sram.v              # SRAM 行为模型（仿真用）
    ├── ClockGate.sv        # 时钟门控
    ├── RstSync.sv          # 复位同步
    └── sram_backdoor.*     # 仿真后门访问 SRAM 的 DPI/C++ 支持
```

注意一个关键区分：`hdl/verilog/rvv/` 下既有 `design/`（设计本身）又有 `sve/`（SystemVerilog **验证环境**）——验证代码和设计代码住在同一个硬件目录里，因为它们紧密耦合。

#### 4.2.3 源码精读

`hdl/chisel/` 用 `.scala` 后缀，`hdl/verilog/rvv/` 用 `.sv`/`.svh` 后缀。这是区分两套实现最快的方法：

- 标量核的取指单元在 `hdl/chisel/src/coralnpu/scalar/Fetch.scala`（Chisel，后续 [u4-l2](u4-l2-fetch-instruction-buffer.md) 精读）。
- 向量后端的 MAC 引擎在 `hdl/verilog/rvv/design/rvv_backend_mulmac.sv`（SystemVerilog，后续 [u7-l4](u7-l4-mac-outer-product.md) 精读）。

根目录的 README 没有逐字列出 `hdl/` 的子目录，但它的特性清单（[README.md:15-23](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/README.md#L15-L23)）里「四发射标量 / 双发射向量 / 128-bit SIMD / 256-bit 流水线」正好对应了「标量走 Chisel、向量走 SystemVerilog」这条主线。

> 本模块的「源码精读」主要是**目录结构**而非单一代码片段——对一个目录总览讲义而言，「能准确说出哪个目录放哪类硬件」就是最核心的精读成果。各目录内部的关键文件，我们留给后续专门的源码讲义展开。

#### 4.2.4 代码实践

这是一个**源码阅读型实践**，目标是亲手验证「双轨制」的边界。

1. **实践目标**：用文件后缀区分 `hdl/` 下的两种硬件语言。
2. **操作步骤**：
   - 在仓库根执行 `git ls-files hdl/chisel | grep -c '\.scala$'`，统计 Chisel 文件数。
   - 执行 `git ls-files hdl/verilog/rvv | grep -cE '\.sv$|\.svh$'`，统计 SystemVerilog 文件数。
   - 执行 `git ls-files hdl/chisel/src/coralnpu/scalar | head`，看标量核前端有哪些文件。
   - 执行 `git ls-files hdl/verilog/rvv/design | head`，看向量后端执行单元有哪些文件。
3. **需要观察的现象**：`.scala` 文件几乎全在 `hdl/chisel/`；`.sv`/`.svh` 文件几乎全在 `hdl/verilog/`（及少量公共 `.sv`）。
4. **预期结果**：Chisel 文件约 130+ 个集中在 `hdl/chisel/`；SystemVerilog 文件约 170+ 个集中在 `hdl/verilog/rvv/`。两类几乎不交叉。
5. 若本地未安装 git 或无法执行，记为「待本地验证」，但可基于上面的目录树理解结论。

#### 4.2.5 小练习与答案

**练习 1**：如果我想修改「标量核的 ALU」，应该去 `hdl/chisel/` 还是 `hdl/verilog/rvv/`？为什么？
**答案**：去 `hdl/chisel/src/coralnpu/scalar/Alu.scala`。标量核属于 Chisel 实现的部分；`hdl/verilog/rvv/` 只放向量/矩阵后端。

**练习 2**：`hdl/verilog/rvv/sve/`（约 79 个文件）和 `hdl/verilog/rvv/design/`（约 58 个文件）有什么区别？
**答案**：`design/` 是**设计本身**（真正会被综合成硬件的 RTL，如 MAC 单元、VRF、ROB）；`sve/` 是 **SystemVerilog 验证环境**（testbench、UVM agent/scoreboard/coverage），只在仿真时用，不进最终芯片。

**练习 3**：`hdl/verilog/Sram.v` 和 `hdl/verilog/ClockGate.sv` 为什么不放在 `rvv/` 里？
**答案**：因为它们是**跨模块共享的公共基础设施**（SRAM 行为模型、时钟门控单元），标量核和向量后端都可能用到，所以放在 `hdl/verilog/` 根下而不是某个具体后端的子目录里。

---

### 4.3 软件、工具链与示例：`sw` / `toolchain` / `examples`

#### 4.3.1 概念说明

CoralNPU 是硬件 IP，但要让它在上面**跑程序**，就需要一整套软件：编译器工具链、C 运行时、链接脚本、机器学习算子库，以及示例程序。这三个目录共同构成「在 CoralNPU 上写代码、编译、运行」的软件侧：

- `toolchain/`：**RISC-V 工具链相关**——编译器包装、C 运行时（CRT）、把代码/数据映射到 ITCM/DTCM 的链接脚本。
- `sw/`：**运行在 CoralNPU 上的软件**——ML 算子库（litert-micro）、仿真封装、工具。
- `examples/`：**可编译运行的示例程序**，是初学者上手的最短路径。

#### 4.3.2 核心流程

三个目录的协作链路是：

```
你写的 C/C++ 程序
      │  （放在 examples/，如 hello_world_add_floats.cc）
      ▼
toolchain/  ──提供──►  RISC-V 编译器 + CRT + 链接脚本(corallnpu_tcm.ld.tpl)
      │                 （把 .text 放 ITCM、.data 放 DTCM）
      ▼
编译出 .elf
      │
      ▼
sw/opt/  ──提供──►  ML 算子库（litert-micro）、RVV 优化头(rvv_opt.h)
      │             （写 ML 应用时调用）
      ▼
运行（仿真器或真实硬件）
```

`toolchain/` 的内部结构：

```
toolchain/
├── coralnpu_tcm.ld.tpl   # 链接脚本模板：划分 ITCM/DTCM section
├── crt/                  # C 运行时：coralnpu_start.S / crt.S / coralnpu_gloss.cc
├── wrappers/             # 编译器/链接器包装脚本（14 个）
├── host_clang/           # 宿主 clang 相关（10 个）
├── build_scripts/        # 构建脚本（3 个）
├── cc_toolchain_config.bzl  # Bazel C++ 工具链配置
└── BUILD.bazel
```

`sw/` 的内部结构：

```
sw/
├── opt/                  # 优化库
│   ├── litert-micro/     # 18 个文件：卷积/全连接/池化等 ML 算子
│   └── rvv_opt.h         # RVV 向量优化头
├── coralnpu_sim/         # 4 个：CoralNPU 仿真封装（Python/C++）
└── utils/                # 7 个：软件工具
```

`examples/` 内容很少但很关键，只有 3 个文件：

```
examples/
├── BUILD.bazel
├── hello_world_add_floats.cc   # 最简单的入门示例：浮点加法
└── rvv_add_intrinsic.cc        # RVV 向量 intrinsics 示例
```

#### 4.3.3 源码精读

`examples/` 是初学者最先该读的代码。README Quick Start 里编译的就是第一个示例：

[README.md:37-38](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/README.md#L37-L38)：`bazel build //examples:coralnpu_v2_hello_world_add_floats`——构建入门示例。

这两个示例的深度阅读和动手编写，分别在 [u2-l2](u2-l2-write-compile-program.md)（写并编译一个 C++ 程序）和 [u10-l1](u10-l1-rvv-intrinsics.md)（RVV 向量编程）展开，本讲只需记住**入口在哪**。

`toolchain/` 把代码放进 TCM 的机制（链接脚本 + CRT）是 [u2-l1](u2-l1-toolchain-linker-tcm.md) 的主题；这里先建立「链接脚本在 `toolchain/coralnpu_tcm.ld.tpl`、C 运行时在 `toolchain/crt/`」的位置感。

#### 4.3.4 代码实践

这是一个**定位型实践**，目标是熟悉软件侧三个目录的入口。

1. **实践目标**：找到「写程序、编译程序、调算子」各自对应的文件入口。
2. **操作步骤**：
   - 执行 `git ls-files examples`，确认只有两个示例源文件。
   - 执行 `git ls-files toolchain/crt`，列出 C 运行时的汇编/C++ 文件。
   - 执行 `git ls-files toolchain | grep '\.ld'`，找到链接脚本。
   - 执行 `git ls-files sw/opt/litert-micro | head`，看 ML 算子库有哪些算子文件（如 `conv.cc`、`fully_connected.cc`）。
3. **需要观察的现象**：`examples/` 极简（两个 `.cc`）；`toolchain/crt/` 有汇编启动文件；`sw/opt/litert-micro/` 有按算子命名的 `.cc`。
4. **预期结果**：能准确说出「入门示例 = `examples/hello_world_add_floats.cc`」「链接脚本 = `toolchain/coralnpu_tcm.ld.tpl`」「卷积算子 = `sw/opt/litert-micro/conv.cc`」。
5. 若无法执行命令，记为「待本地验证」，但可凭上面的目录树作答。

#### 4.3.5 小练习与答案

**练习 1**：`toolchain/crt/` 里的 `.S` 文件（如 `coralnpu_start.S`）是做什么的？
**答案**：是 C 运行时（CRT, C Runtime）的汇编启动代码，负责从复位入口初始化栈、清 `.bss`、然后跳转到 `main`。它和 `crt.S` 一起构成「上电到进入 main」的桥梁（详见 [u2-l1](u2-l1-toolchain-linker-tcm.md)）。

**练习 2**：为什么 `examples/` 只有区区几个文件，而 `sw/opt/litert-micro/` 有 18 个？
**答案**：`examples/` 的定位是「最小可运行示例」，只演示怎么写程序，所以少而精；`sw/opt/litert-micro/` 是完整的 ML 算子库（卷积、深度卷积、全连接、池化……每种算子一个或多个文件），服务于端到端 ML 推理，所以文件多。

**练习 3**：README Quick Start 用 `bazel build //examples:coralnpu_v2_hello_world_add_floats`，这里 `examples` 和 `coralnpu_v2_hello_world_add_floats` 分别指什么？
**答案**：`examples` 是 Bazel 包（即 `examples/BUILD.bazel` 所在目录）；`coralnpu_v2_hello_world_add_floats` 是该包里定义的一个构建目标（target）名，对应 `examples/BUILD.bazel` 中的某条规则。

---

### 4.4 验证、仿真与支撑设施

#### 4.4.1 概念说明

硬件项目有一句行话：「验证代码的量往往是设计代码的好几倍。」CoralNPU 也不例外——`tests/` 目录约 296 个文件，是仓库里第二重的目录。围绕「怎么验证硬件是对的」，CoralNPU 准备了一整套设施，分散在多个目录：

- `tests/`：各类**测试与回归**——cocotb（Python 测试台）、verilator_sim（C++ 仿真）、uvm（UVM 验证）、vcs_sim、systemc、npusim_examples。
- `hw_sim/`：Verilator 仿真器的 **C++ 封装**（simulator/wrapper/primitives/mailbox）。
- `coralnpu_test_utils/`：测试用的 **Python 辅助库**（接口驱动、axi 从机、ECC 黄金值等）。
- `doc/`：**文档**（overview、microarch、integration_guide、tutorials、peripherals）。
- `rules/`：Bazel **自定义构建规则**（chisel.bzl、verilog.bzl、vcs.bzl、coco_tb.bzl 等）。
- `third_party/`：**第三方依赖**（rules_hdl、cvfpu、spike、riscv-tests、freertos、tflite-micro 等）。
- `fpga/`：**FPGA 原型**构建（IP、rtl、综合脚本、比特流生成）。
- `utils/`、`platforms/`、`.github/`：杂项工具、平台定义、CI 工作流。

#### 4.4.2 核心流程

`tests/` 内部按验证方法分类，是最该理清的目录：

```
tests/
├── cocotb/           # 222 个：Python(cocotb) 测试台，最大回归集
│   ├── rvv/          # 106 个：向量后端测试
│   ├── tutorial/     # 27 个：教程配套测试
│   ├── tlul/         # 16 个：TileLink-UL 总线测试
│   ├── exceptions/   # 10 个：异常处理测试
│   ├── riscv-dv/     # 8 个：随机指令生成(riscv-dv)
│   ├── csr_test/     # CSR 测试
│   ├── coralnpu_isa/ # ISA 一致性测试
│   └── freertos_app/ # FreeRTOS 应用测试
├── uvm/              # 41 个：UVM 验证平台
├── verilator_sim/    # 23 个：Verilator 仿真入口与测试
├── systemc/          # 4 个：SystemC 仿真
├── vcs_sim/          # 3 个：VCS 仿真
└── npusim_examples/  # 3 个：npusim（MobileNet 端到端）示例
```

其余支撑目录的职责：

```
hw_sim/               # 10 个：Verilator 仿真器的 C++ 封装
  ├── core_mini_axi_simulator.cc   # 仿真器主体
  ├── core_mini_axi_wrapper.h      # 仿真器封装
  ├── coralnpu_simulator.h         # CoralNPU 专用仿真器
  ├── hw_primitives.cc/h           # 仿真原语（如 backdoor 访问）
  └── mailbox.h                    # 邮箱（仿真观测内核状态）

coralnpu_test_utils/  # 17 个：Python 测试辅助库
  ├── core_mini_axi_interface.py   # 测试台接口(reset/load_elf/run/halt)
  ├── axi_slave.py                 # 模拟外部 AXI 主机
  ├── secded_golden.py             # ECC 编解码黄金参考
  └── rvv_type_util.py             # RVV 向量类型辅助

doc/                  # 26 个：文档
  ├── overview.md / integration_guide.md / simulation.md
  ├── microarch/      # 微架构：microarch / dispatch / lsu / mlu / debug
  ├── tutorials/      # writing_coralnpu_programs / npusim_mobilenet_tutorial
  └── peripherals/ sw/ images/

rules/                # 23 个：Bazel 自定义规则
  ├── chisel.bzl / verilog.bzl / vcs.bzl   # 各语言的构建规则
  ├── coco_tb.bzl / coralnpu_v2.bzl        # cocotb 测试 / 二进制规则
  └── *.tpl                                # 模板（如 default.vlt.tpl）

third_party/          # 76 个：第三方依赖（rules_hdl, cvfpu, spike, freertos, tflite-micro, ...）
fpga/                 # 131 个：FPGA 原型（ip / sw / rtl + 综合脚本）
utils/                # 15 个：脚本工具（回归脚本、dockerfile、loader）
platforms/ .github/   # Bazel 平台定义 / CI 工作流
```

#### 4.4.3 源码精读

`tests/cocotb/` 是 README Quick Start 第一步跑的测试所在：

[README.md:34-35](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/README.md#L34-L35)：`bazel run //tests/cocotb:core_mini_axi_sim_cocotb`——运行 cocotb 测试套件，确认环境可用。

[README.md:40-44](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/README.md#L40-L44)：构建 Verilator 仿真器并在其上运行二进制——这一步同时涉及 `tests/verilator_sim/` 和 `hw_sim/` 两个目录。

文档目录是后续几乎每篇讲义都会引用的「知识源」：`doc/overview.md`（总览，[u1-l1](u1-l1-project-overview.md) 已读）、`doc/integration_guide.md`（集成指南，[u3-l2](u3-l2-axi-integration.md)）、`doc/microarch/`（微架构详解，[u4](#)/[u6](#) 多篇引用）。本讲只建立「文档在哪」的位置感。

#### 4.4.4 代码实践

这是一个**分类统计型实践**，目标是量化「验证设施有多重」。

1. **实践目标**：统计并对比验证相关目录的文件数量，体会「验证 > 设计」的现象。
2. **操作步骤**：
   - 执行 `git ls-files tests | wc -l`，得到测试文件总数。
   - 执行 `git ls-files hdl | wc -l`，得到硬件设计文件总数。
   - 执行 `git ls-files tests/cocotb | cut -d/ -f3 | sort | uniq -c`，看 cocotb 下的测试分类。
   - 执行 `git ls-files rules | grep '\.bzl$' | head`，看有哪些自定义 Bazel 规则。
3. **需要观察的现象**：`tests/` 与 `hdl/` 文件数孰多孰少；cocotb 下哪个子类测试最多。
4. **预期结果**：`tests`（约 296）与 `hdl`（约 313）量级相当，验证规模确实不亚于设计；cocotb 下 `rvv/`（约 106）最多，印证「向量后端是验证重点」。
5. 若本地无法统计，记为「待本地验证」，但可凭上面的目录树得出定性结论。

#### 4.4.5 小练习与答案

**练习 1**：`tests/cocotb/` 和 `hw_sim/` 都跟仿真有关，它们分工有何不同？
**答案**：`hw_sim/` 是**仿真器本身的 C++ 实现/封装**（Verilator 编译出来的可执行程序如何加载 ELF、提供 backdoor）；`tests/cocotb/` 是**跑在仿真器上的 Python 测试用例与回归集**。前者是「舞台」，后者是「剧本」。

**练习 2**：`coralnpu_test_utils/` 里的 `core_mini_axi_interface.py` 和 `axi_slave.py` 大致各管什么？
**答案**：`core_mini_axi_interface.py` 封装测试台对内核的标准生命周期接口（reset / load_elf / execute_from / wait_for_halted / read）；`axi_slave.py` 模拟一个外部 AXI 主机，用来从外部向 CoralNPU 的 DTCM 注入数据并读回结果。二者是 cocotb 测试最常用的两个工具（详见 [u2-l4](u2-l4-cocotb-testbench-intro.md)）。

**练习 3**：`rules/` 目录下的 `chisel.bzl`、`verilog.bzl`、`vcs.bzl`、`coco_tb.bzl` 各对应什么？
**答案**：它们是 Bazel 的**自定义构建规则**（Starlark 写的 `.bzl` 文件）：`chisel.bzl` 告诉 Bazel 怎么编译 Chisel、`verilog.bzl` 怎么跑 Verilator 仿真、`vcs.bzl` 怎么用 VCS、`coco_tb.bzl` 怎么跑 cocotb 测试。没有它们，Bazel 默认不认识这些硬件/验证流程。详见 [u1-l3](u1-l3-bazel-build-quickstart.md) 与 [u11](u11-l1-verilator-flow.md) 系列。

---

## 5. 综合实践

本讲的核心实践任务是：**亲手产出一张「目录-职责」对照表**，把全篇知识串起来。

**任务**：在仓库根目录执行下面的命令，列出所有顶层目录及其文件数，然后为每个目录写一行中文职责，并标注它属于哪一类（Chisel 硬件 / SystemVerilog 硬件 / 软件 / 验证 / 构建 / 文档 / 平台）。

```bash
# 第 1 步：列出所有顶层目录及文件数
git ls-files | cut -d/ -f1 | sort | uniq -c | sort -rn
```

**第 2 步**：把输出整理成一张表，参考答案如下（你可以补充自己读到的细节）：

| 目录 | 文件数(约) | 一行职责 | 类别 |
|------|-----------|----------|------|
| `hdl/` | 313 | 硬件设计（chisel 标量核/SoC + verilog RVV 向量/矩阵后端） | Chisel + SystemVerilog 硬件 |
| `tests/` | 296 | 各类测试与回归（cocotb/uvm/verilator/vcs 等） | 验证 |
| `fpga/` | 131 | FPGA 原型构建（IP/rtl/综合脚本/比特流） | 平台/验证 |
| `third_party/` | 76 | 第三方依赖（rules_hdl/cvfpu/spike/freertos/tflite-micro 等） | 构建/依赖 |
| `toolchain/` | 37 | RISC-V 工具链、CRT、TCM 链接脚本 | 软件/构建 |
| `sw/` | 31 | 运行在 NPU 上的软件（litert-micro 算子库、仿真封装） | 软件 |
| `doc/` | 26 | 文档（overview/integration_guide/microarch/tutorials） | 文档 |
| `rules/` | 23 | Bazel 自定义构建规则（.bzl + 模板） | 构建 |
| `coralnpu_test_utils/` | 17 | Python 测试辅助库（接口/axi 从机/ECC 黄金值） | 验证 |
| `utils/` | 15 | 脚本工具（回归脚本/dockerfile/loader） | 构建/工具 |
| `hw_sim/` | 10 | Verilator 仿真器的 C++ 封装 | 验证 |
| `examples/` | 3 | 入门示例程序 | 软件 |
| `platforms/` | 3 | Bazel 平台定义（cpu/os） | 构建 |
| `.github/` | 2 | CI 工作流 | 构建/CI |

**第 3 步（进阶，可选）**：任选一个目录向下钻一层，用 `git ls-files <目录> | cut -d/ -f1-3 | sort | uniq -c` 看它的二级结构，验证你对其职责的判断。比如钻进 `hdl/verilog/rvv`，你会看到 `design`（设计）/`sve`（验证）/`inc`（头文件）的清晰分工，从而对「向量后端是手写 SystemVerilog」有更具体的体感。

> 完成这张表后，你就拥有了 CoralNPU 的「代码地图」。后续每读到一篇讲义提到某个文件，你都能立刻在脑中定位它属于哪个目录、负责什么——这正是本讲想建立的能力。

## 6. 本讲小结

- CoralNPU 仓库约 1000 个文件，按「硬件设计 / 软件 / 验证 / 构建 / 文档」清晰分层，根目录的门面文件（README、WORKSPACE、.bazelrc）定义了项目身份和 Bazel 工作区。
- **硬件是双轨制**：`hdl/chisel/`（Scala）写标量核/SoC/总线/外设，`hdl/verilog/rvv/`（SystemVerilog）写向量/矩阵 MAC 后端，按处理器组件分工、各管一段。
- **软件侧三件套**：`toolchain/` 提供工具链与 TCM 链接脚本，`sw/` 提供 ML 算子库，`examples/` 提供最短上手路径。
- **验证规模庞大**：`tests/`（约 296）与 `hdl/`（约 313）量级相当，cocotb 是主力回归集；`hw_sim/` + `coralnpu_test_utils/` 提供仿真器封装与 Python 测试工具。
- **支撑设施**：`doc/`（知识源）、`rules/`（Bazel 自定义规则）、`third_party/`（依赖）、`fpga/`（FPGA 原型）各司其职。
- `git ls-files | cut | sort | uniq -c` 是建立任意大型仓库目录地图的通用利器。
- 实操中发现一处文档与配置漂移：README 写 Bazel 7.4.1，而 `.bazelversion` 钉死 8.6.0——以配置文件为准。

## 7. 下一步学习建议

本讲建立的是「地图」。下一步建议：

1. **紧接着学 [u1-l3](u1-l3-bazel-build-quickstart.md)「Bazel 构建系统与快速上手」**：把本讲提到的 Quick Start 四步命令真正跑起来，理解 `rules/` 里的自定义规则如何驱动 `hdl/` 和 `tests/` 的构建。这是从「看懂地图」到「用地图导航」的关键一步。
2. **跑通 Quick Start 后进入单元 2**：用 [u2-l1](u2-l1-toolchain-linker-tcm.md) 理解 `toolchain/` 的链接脚本与 CRT，再在 [u2-l2](u2-l2-write-compile-program.md) 动手改编译 `examples/` 里的示例程序。
3. **对硬件感兴趣可直接跳到单元 3**：[u3-l1](u3-l1-soc-subsystem.md) 会带你读 `hdl/chisel/src/soc/` 的 SoC 顶层装配，把本讲的 Chisel 目录结构具体化为真实模块。
4. **建议持续维护你自己的「目录-职责」对照表**：随着后续讲义展开，把每个目录对应的关键文件填进去，最终形成你专属的 CoralNPU 代码索引。
