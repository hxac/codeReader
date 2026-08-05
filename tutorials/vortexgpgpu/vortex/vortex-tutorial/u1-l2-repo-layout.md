# 仓库目录结构地图

## 1. 本讲目标

Vortex 是一个**全栈**开源 GPGPU——从主机 C 程序、设备内核，到 C++ 仿真器、RTL、FPGA 比特流，全都挤在同一个 Git 仓库里。这意味着仓库目录又大又杂，如果不先建立一张「地图」，很容易在第一次 `ls` 时就迷路。

本讲的目标是：

- 记住仓库的**一级目录**（`hw`、`sw`、`sim`、`tests`、`ci`、`docs`、`perf` 等）各自负责什么。
- 能够在 `hw/rtl` 下区分 `core`、`cache`、`mem`、`cp`、`tcu`、`dxa`、`rtu`、`gfx` 等子系统，知道它们对应 GPU 的哪一部分。
- 理解 `tests` 下不同测试套件（`regression`、`riscv`、`opencl`、`graphics`……）的定位差异。
- 学会以 `docs/codebase.md` 为总索引、`docs/index.md` 为文档入口，在后续学习里随时回到这张地图。

学完后，你应该能对着任何一个 Vortex 路径（例如 `sw/runtime/simx/vortex.cpp`）说出它属于哪一层、大概做什么。

## 2. 前置知识

阅读本讲前，你应该已经学过 **u1-l1《Vortex 是什么》**，从而了解：

- Vortex 的全栈分层：主机运行时 → 驱动后端 → 仿真器 / RTL / FPGA。
- SIMT 执行模型与 thread / warp / socket / cluster 的层次。
- Vortex 是一条 6 级流水线：Schedule → Fetch → Decode → Issue → Execute → Commit。

这些概念在本讲里会反复出现——**目录结构其实就是这些概念在文件系统里的投影**。例如 `core`（核心流水线）、`cache`（缓存）、`tcu`（张量核）这些目录名，正是上一讲提到的硬件模块的名字。

如果你对下面几个名词还不熟，这里先给一句话解释：

- **RTL（Register Transfer Level）**：用硬件描述语言（这里是 SystemVerilog，`.sv` 文件）写出的、可综合成真实电路的硬件设计。
- **仿真器（Simulator）**：用软件（C++ / SystemVerilog）模拟硬件行为，让你在不烧 FPGA 的前提下跑程序、看时序。
- **内核（kernel）**：运行在 GPU 设备上的程序，区别于运行在主机（host）上的程序。
- **驱动（driver）**：主机侧负责打开设备、搬运数据、启动内核的库。

## 3. 本讲源码地图

本讲主要阅读两份「文档型源码」，它们就是仓库的官方地图：

| 文件 | 作用 |
| --- | --- |
| [docs/codebase.md](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/codebase.md) | 仓库目录树的总说明，逐条列出每个目录的职责。 |
| [docs/index.md](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/index.md) | 全部文档的索引页，按「入门 / 架构 / 开发 / 设计文档」分类。 |

此外，我们会用本地的 `ls` / `git ls-files` 去**对照**这些说明，验证文档描述与磁盘上的真实文件一致——这是阅读任何大型项目源码的基本功。

> 说明：本讲引用的目录都已在当前 HEAD（`d76b7f24e`）下实际确认存在；行号基于上述两份文档的当前内容。

## 4. 核心概念与源码讲解

### 4.1 一级目录总览：用 codebase.md 当地图

#### 4.1.1 概念说明

`docs/codebase.md` 是 Vortex 自己写的「目录说明书」。它的开宗明义第一句就是：

> The directory/file layout of the Vortex codebase is as follows:

随后用一棵缩进列表逐条解释目录。这种「文档先于代码、文档即地图」的做法，是阅读 Vortex 时最该养成的习惯：**先读 `codebase.md` 再 `ls`**，而不是反过来。

#### 4.1.2 核心流程

阅读 Vortex 目录的标准动线是：

1. 打开 `docs/codebase.md`，扫一眼顶层条目，建立全局印象。
2. 用 `ls` 在本地验证每个一级目录确实存在。
3. 把每个一级目录对应到上一讲学过的「全栈分层」上：
   - `hw` = 硬件层（RTL + 综合 + FPGA 外壳）
   - `sw` = 软件层（设备内核 + 主机运行时 / 驱动）
   - `sim` = 仿真器层（SimX、Verilator rtlsim 等）
   - `tests` = 各类测试套件
   - `ci` = 持续集成脚本与测试目录
   - `docs` = 文档
   - `perf` = 性能分析资源
   - `third_party` = 外部依赖子模块
4. 想深入某子系统时，再去 `docs/index.md` 找对应的设计文档。

#### 4.1.3 源码精读

仓库最顶层的两个配置文件是整套构建的「唯一真相来源」（详见下一讲 U2 硬件配置系统）：

[docs/codebase.md:L5-L7](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/codebase.md#L5-L7) —— 说明 `VX_config.toml` / `VX_types.toml` 是硬件配置系统的单一真相来源。

接下来是各个一级目录的说明：

[docs/codebase.md:L8-L12](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/codebase.md#L8-L12) —— `ci` 目录：持续集成脚本，包括声明式测试目录 `testcases`、性能基线 `perf/baselines`、统一启动器 `blackbox.sh`、本地回归入口 `regression.sh`。

[docs/codebase.md:L54-L60](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/codebase.md#L54-L60) —— `sim` 目录：四套仿真器（`simx`、`rtlsim`、`opaesim`、`xrtsim`）加上共享基础设施 `common`（命令处理器模型、DRAM 模型、ELF 加载器、虚拟内存）。

[docs/codebase.md:L61-L72](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/codebase.md#L61-L72) —— `tests` 目录：十一类测试套件，覆盖 RISC-V 一致性、设备内核、回归、OpenCL/Vulkan/HIP、图形、光追等。

[docs/codebase.md:L73-L76](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/codebase.md#L73-L76) —— 收尾三个目录：`third_party`（cvfpu、softfloat、ramulator 等外部子模块）、`perf`（性能分析）、`miscs`（杂项）。

#### 4.1.4 代码实践

**实践目标**：验证 `codebase.md` 的描述与磁盘真实文件一致，建立「文档可信赖」的信心。

**操作步骤**：

1. 在仓库根目录执行 `ls -1F`，列出所有一级条目。
2. 对照上面引用的 `codebase.md` 几个片段，逐个确认 `hw`、`sw`、`sim`、`tests`、`ci`、`docs`、`perf`、`third_party`、`miscs` 都存在。
3. 用 `git ls-files hw/rtl | head -20` 看一眼 RTL 下被 git 跟踪的真实文件名。

**需要观察的现象**：`ls` 输出里应当出现 `codebase.md` 提到的每一个一级目录；`git ls-files` 里会看到形如 `hw/rtl/Vortex.sv`、`hw/rtl/core/VX_core.sv` 的路径，证明目录描述不是凭空写的。

**预期结果**：文档描述与磁盘文件一一对应。如果发现某个目录文档里有、磁盘上却没有（或反过来），那通常意味着你切到了不同的 git 分支或 HEAD，这是排查「为什么对不上」的第一个线索。

#### 4.1.5 小练习与答案

**练习 1**：仓库根目录还有 `VX_config.toml` 和 `vortex.cfg` 两个文件，它们是不是同一个东西？
**参考答案**：不是。`VX_config.toml` 是硬件构建配置的「唯一真相来源」（`codebase.md` 第 5–7 行）；`vortex.cfg` 则是另一份配置（本讲不展开）。区分两者的关键看后缀与命名空间：`VX_*` 前缀才是 Vortex 官方约定。

**练习 2**：`third_party` 里的库是 Vortex 自己写的吗？为什么它们以子模块形式存在？
**参考答案**：不是自己写的。`codebase.md` 第 73–74 行明确说是 external library submodules（cvfpu、softfloat、hardfloat、ramulator、cocogfx）。Vortex 复用这些成熟实现（如浮点库、DRAM 时序模型），用 git submodule 引入而非拷贝进仓库，便于跟进上游更新。

---

### 4.2 hw/rtl 与 sw/sim 子系统分解

#### 4.2.1 概念说明

一级目录知道了，但真正「藏龙卧虎」的是它们内部。Vortex 的两条主线——硬件 `hw/rtl` 与软件 `sw`——都是按 GPU 的功能模块拆成子目录的。上一讲那条 6 级流水线和各种加速器，在这里都有同名的目录。

#### 4.2.2 核心流程

`hw/rtl` 子系统的对应关系（这是本讲最重要的「翻译表」）：

```
hw/rtl/
├── core/      → 核心 6 级流水线（fetch/decode/issue/execute/lsu/commit）
├── cache/     → 缓存子系统（banks、MSHR、AMO 引擎、flush）
├── mem/       → 内存子系统（仲裁器、适配器、本地内存）
├── fpu/       → 浮点单元
├── cp/        → 命令处理器（Command Processor，接收主机 launch）
├── tcu/       → 张量核（WGMMA、结构化稀疏）
├── dxa/       → 异步数据搬运加速器（DMA / 多播）
├── rtu/       → 光线追踪单元（BVH 遍历、相交测试）
├── raster/ tex/ om/ gfx/  → 图形固定功能流水线
├── afu/       → FPGA 加速器外壳（OPAE、XRT）
├── interfaces/ → SystemVerilog 接口
└── libs/      → 通用 RTL 模块（队列、仲裁器、交叉开关）
```

软件侧 `sw` 则按「运行在哪」分层：

```
sw/
├── kernel/   → 设备侧内核 API（运行在 GPU 上）
├── runtime/  → 主机侧运行时与驱动（运行在 CPU 上）
├── common/   → Vortex 内部共享层（在线 ABI 结构、主机侧硬件模型）
└── gfx/      → 图形固定功能的软件发射器
```

其中 `runtime` 又按**后端**拆分，对应上一讲讲的「`$VORTEX_DRIVER` 切换后端」机制：`stub`（分发桩）、`simx`、`rtlsim`、`opae`、`xrt`、`gem5`。注意 `sim/` 与 `sw/runtime/simx` 是两件事：`sim/simx` 是仿真器本体（被调用方），`sw/runtime/simx` 是让主机程序去调用它的驱动胶水（调用方）。

#### 4.2.3 源码精读

`hw/rtl` 各子系统的官方解释：

[docs/codebase.md:L15-L29](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/codebase.md#L15-L29) —— `hw` 顶层与 `hw/rtl` 全部子系统的职责清单，是上面那张翻译表的权威出处。

`hw` 下除 RTL 外还有综合、DPI、单元测试：

[docs/codebase.md:L30-L37](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/codebase.md#L30-L37) —— `dpi`（仿真器共享的 DPI 模型）、`syn`（四家厂商综合脚本：altera / xilinx / synopsys / yosys）、`unittest`（Verilator 单元测试）、`scripts`（RTL 构建预处理工具）。

软件栈 `sw` 的分层：

[docs/codebase.md:L38-L53](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/codebase.md#L38-L53) —— `sw/kernel`（设备侧，含 `include`/`src`/`linker`）、`sw/runtime`（主机侧，含六个后端）、`sw/common`（**永不安装**的内部共享层）、`sw/gfx`（图形软件发射器）。

> 小提示：`sw/common` 标注「never installed」很重要——它是 Vortex 内部 ABI 与主机侧硬件模型的共享层，属于实现细节，不会进公共头文件。这关系到 U2 会讲的「硬件/软件边界隔离」。

#### 4.2.4 代码实践

**实践目标**：亲手在 RTL 里找到流水线的某个真实模块，把抽象的「翻译表」落到具体文件。

**操作步骤**：

1. 执行 `ls hw/rtl/core/`，你会看到 `VX_core.sv`、`VX_fetch.sv`、`VX_decode.sv`、`VX_issue.sv`、`VX_execute.sv`、`VX_commit.sv` 等文件。
2. 把它们与上一讲的 6 级流水线对号入座：`VX_fetch.sv` ↔ Fetch，`VX_decode.sv` ↔ Decode，以此类推。
3. 再执行 `ls hw/rtl/`，确认 `tcu`、`dxa`、`rtu`、`gfx` 等加速器目录确实存在。

**需要观察的现象**：`core` 目录里的 `.sv` 文件名几乎和流水线级一一对应；顶层 `hw/rtl` 里既有 `Vortex.sv`（顶层模块）也有各加速器的子目录。

**预期结果**：你能不查文档地指出「张量核在 `hw/rtl/tcu`，光追在 `hw/rtl/rtu`，DMA 在 `hw/rtl/dxa`」。这正是后续 U7（核心流水线 RTL）、U9（张量核 / DXA）、U10（图形 / 光追）要逐个深入的入口。

#### 4.2.5 小练习与答案

**练习 1**：`sw/runtime/simx` 和 `sim/simx` 有什么区别？谁是调用方、谁是被调用方？
**参考答案**：`sim/simx` 是 SimX 仿真器**本体**（C++ 写的、模拟 GPU 的那个程序，被调用方）；`sw/runtime/simx` 是让**主机程序**通过 `libvortex` 去驱动 SimX 的后端胶水（调用方）。两者一上一下，中间靠 `sw/runtime/stub` 动态分发。

**练习 2**：为什么 `hw/rtl` 下既有 `libs`（通用模块）又有各功能子系统目录？
**参考答案**：`libs` 存放可复用的基础设施（队列、仲裁器、交叉开关、编码器，见 `codebase.md` 第 29 行），被多个子系统共享；而 `core`/`cache`/`tcu` 等是 GPU 特有的功能模块。这种「基础设施 vs 功能模块」的拆分能避免重复造轮子，也让通用组件可以独立测试。

---

### 4.3 tests 测试套件与 docs 文档索引

#### 4.3.1 概念说明

知道代码在哪之后，还要知道「怎么验证它跑得对」和「想深入去哪查」。前者靠 `tests`，后者靠 `docs`。Vortex 的测试组织得非常细，按**用什么上层 API** 和**测什么子系统**分门别类；文档则有一张清晰的索引页 `docs/index.md`。

#### 4.3.2 核心流程

`tests` 套件可以这样分类记忆：

- **底层一致性**：`riscv`（RISC-V 指令一致性）、`kernel`（设备内核）。
- **主回归**：`regression`（主机 + 内核回归，最常用的入口，如 `demo`）。
- **单元 / 驱动**：`unittest`（主机侧单元测试）、`runtime`（驱动 API 测试）、`mpi`（多进程）。
- **上层 API**：`opencl`（经 PoCL）、`vulkan`（经 mesa-vortex）、`hip`（经 chipStar）。
- **专用流水线**：`graphics`（图形流水线）、`raytracing`（光追单元）。

文档侧，`docs/index.md` 把全部文档分成四大类：Getting Started（入门）、Architecture（架构）、Development（开发）、Design Documents（设计文档）。其中 `designs/` 子目录里有约 28 篇子系统设计文档，是进阶学习的金矿。

#### 4.3.3 源码精读

`tests` 各套件的官方定位：

[docs/codebase.md:L61-L72](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/codebase.md#L61-L72) —— 十一类测试套件逐条说明，是上面分类表的依据。

文档索引页对「Codebase Layout」与「Microarchitecture」的指引：

[docs/index.md:L13-L14](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/index.md#L13-L14) —— 架构类入口：`codebase.md`（本讲地图）与 `microarchitecture.md`（微架构细节，自然的第一篇深读）。

设计文档总入口：

[docs/index.md:L33-L36](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/index.md#L33-L36) —— 指向 `designs/` 目录，里面按「核心流水线 / 内存系统 / 加速器 / 系统与主机接口 / 软件栈 / 仿真与 CI」分组列出了所有子系统设计文档。

> 这意味着：当你想了解某个子系统（比如张量核）时，路径是 `index.md` → 找到「Accelerators / Tensor Core (WGMMA)」→ 打开 `designs/tensor_core_wgmma_engine.md`。索引页就是你后续几十讲的「目录页」。

#### 4.3.4 代码实践

**实践目标**：用文档索引定位一个具体子系统的设计文档，并找到对应的回归测试。

**操作步骤**：

1. 打开 `docs/index.md`，在「Design Documents」里找到「Memory system → Cache Subsystem」对应的设计文档路径。
2. 确认该文件存在：`ls docs/designs/cache_subsystem.md`。
3. 执行 `ls tests/regression/ | head`，看回归测试里有哪些可运行的小程序（如 `demo`、`vecadd`、`sgemm`）。

**需要观察的现象**：索引页给出的链接都能在本地 `ls` 到真实文件；`tests/regression` 下是一堆可直接运行的小测试程序。

**预期结果**：你建立起「文档 → 设计文档 → 回归测试」三者的对应关系。比如想学缓存，就读 `docs/designs/cache_subsystem.md`，再用 `tests/regression` 里的访存密集型程序（如 sgemm）做实验验证。这也是 u1-l4「首次运行」要用的回归目录。

#### 4.3.5 小练习与答案

**练习 1**：`tests/opencl`、`tests/vulkan`、`tests/hip` 三个套件的共同点是什么？为什么 Vortex 要分别维护它们？
**参考答案**：共同点是它们都测「上层 API」而非裸 Vortex 运行时。分别维护是因为这三套 API 各走不同的桥接栈：OpenCL 经 PoCL、Vulkan 经 mesa-vortex、HIP 经 chipStar（见 `codebase.md` 第 66–68 行）。它们验证 Vortex 能向上支撑主流 GPU 编程模型（对应 U12）。

**练习 2**：如果你完全不懂某个子系统，应该先读 `docs/designs/` 下的设计文档，还是先读源码？
**参考答案**：先读设计文档。`docs/index.md` 把 `microarchitecture.md` 标注为「natural first read before the subsystem design documents」——Vortex 的惯例是文档先行、用文档为源码建立心智模型，再带着地图读代码。

---

## 5. 综合实践

**任务**：为仓库的每个一级目录写一张「职责名片」，并锁定你最感兴趣的一个子系统作为后续学习的主线。

**操作步骤**：

1. 对照 `docs/codebase.md`，在本地用 `ls` 找到 `hw`、`sw`、`sim`、`tests`、`ci`、`docs`、`perf`、`third_party`、`miscs` 这几个一级目录。
2. 建一张表，为每个目录写**一句话**职责说明（不要照抄，用自己的话）。例如：

   | 目录 | 一句话职责 |
   | --- | --- |
   | `hw` | 硬件层：RTL 源码、综合脚本、FPGA 外壳 |
   | `sw` | …（你来填） |
   | `sim` | … |

3. 在 `hw/rtl` 的子系统里，挑一个你最感兴趣的（比如张量核 `tcu`、光追 `rtu`、或核心流水线 `core`）。
4. 用 `docs/index.md` 找到该子系统对应的设计文档路径，记下来——这将是你在后续讲义里反复回到的「主入口」。

**需要观察的现象 / 预期结果**：你能凭这张表，对任意一个 Vortex 路径（如 `sim/simx/scheduler.cpp`）说出「它属于仿真器层、负责 warp 调度」。完成此表后，本讲目标即达成。本任务为源码阅读型实践，**无需运行任何命令即可完成**；若想顺带跑通一个程序，可结合下一讲 u1-l3 的构建步骤。

## 6. 本讲小结

- Vortex 是全栈仓库，先读 `docs/codebase.md` 再 `ls`，用文档为代码建立地图。
- 一级目录对应全栈分层：`hw`（硬件）、`sw`（软件）、`sim`（仿真器）、`tests`（测试）、`ci`（集成）、`docs`（文档）、`perf`（性能）、`third_party`（依赖）。
- `hw/rtl` 按功能子系统拆分：`core` 是 6 级流水线，`cache`/`mem` 是存储，`tcu`/`dxa`/`rtu`/`gfx` 是各类加速器。
- `sw` 按「运行在哪」分层：`kernel`（设备侧）、`runtime`（主机侧，含六个驱动后端）、`common`（内部共享层，永不安装）。
- `tests` 按底层一致性 / 主回归 / 上层 API / 专用流水线分类；`docs/index.md` 是通往约 28 篇设计文档的总索引。
- 区分 `sim/simx`（仿真器本体，被调用方）与 `sw/runtime/simx`（驱动胶水，调用方）这对易混概念。

## 7. 下一步学习建议

- 下一讲 **u1-l3《构建系统、configure 与工具链》** 会解释这些目录是怎么被 `configure` 脚本「组装」成一棵可运行的 Vortex 树的，并讲清「改了 toml / Makefile 必须重新 configure」这条核心规则。
- 想立刻跑通一个程序，可跳到 **u1-l4《首次运行：用 blackbox.sh 跑通 demo》**，但它依赖 u1-l3 的构建基础。
- 进入任一子系统深读前，先回到 `docs/index.md` 找对应设计文档；微架构总览 `docs/designs/microarchitecture.md` 是绝大多数子系统文档的前置读物。
