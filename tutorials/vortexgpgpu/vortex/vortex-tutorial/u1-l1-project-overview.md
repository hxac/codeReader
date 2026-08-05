# Vortex 是什么：全栈开源 RISC-V GPGPU

## 1. 本讲目标

本讲是整本 Vortex 学习手册的第一篇。读完本讲，你应当能够：

1. 用一句话说清 Vortex 是什么、解决什么问题。
2. 画出 Vortex「主机运行时 → 驱动后端 → 模拟器 / RTL / FPGA」的全栈分层，并把每一层对应到仓库里的真实目录。
3. 说清 SIMT 模型下 thread / warp / socket / cluster 的层次关系。
4. 列出 Vortex 给 RISC-V 加了哪些「类 GPU」的指令扩展（TMC / WSPAWN / SPLIT / JOIN / PRED / BAR）。
5. 看懂 Vortex 的 6 级流水线骨架，为后续深入 SimX 和 RTL 打下基础。

本篇不要求你写过 Verilog，也不要求你装好工具链——我们只读文档和目录，先建立「心智模型」。

## 2. 前置知识

如果你完全没接触过 GPU，下面几个概念只需有个直觉即可，后续会结合源码细化：

- **ISA（指令集架构）**：CPU/GPU 能执行的一套指令的规范。本项目的底座是 RISC-V，常见的是 `RV32IMAFC`（32 位）和 `RV64IMAFDC`（64 位）。
- **SIMT（Single Instruction, Multiple Threads，单指令多线程）**：GPU 的核心执行思想——同一条指令同时驱动很多个线程，每个线程处理不同的数据。它和 SIMD 的区别是：SIMT 让程序员像写「一个线程」一样写程序，硬件再把它扩展成大量并行线程。
- **Warp / 线程束**：被同一指令同时驱动的一组线程。Vortex 里一个 warp 内的所有线程共享同一个 PC（程序计数器）。
- **CTA（Cooperative Thread Array）**：可以理解成 CUDA 里的「block」，是一组能互相同步、共享内存的线程。
- **GPGPU**：通用并行计算（不只是画图）的 GPU 用法。

> 术语对照：Vortex 的很多概念对齐主流的 CUDA / OpenCL / Vulkan，这是项目明确的设计取向——**让 Vortex 的对外接口对主流开发者足够「眼熟」**（见 [AGENTS.md:133](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/AGENTS.md#L133-L133)）。所以如果你会一点 CUDA，很多地方会似曾相识。

## 3. 本讲源码地图

本讲是导论，主要读三份文档型源码（它们虽是 Markdown，但和 `.sv`、`.cpp` 一样是项目的「真相来源」）：

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/README.md) | 项目门面：定位、规格、目录结构、快速运行。 |
| [AGENTS.md](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/AGENTS.md) | 贡献者与 AI 智能体的「规则手册」，记录了所有不可违反的不变量与易踩的坑。 |
| [docs/designs/microarchitecture.md](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/microarchitecture.md) | 微架构定义：SIMT 模型、ISA 扩展、6 级流水线、聚类结构。 |

后续会引用到的仓库真实目录（本讲会标注它们的职责）：

- `sw/`：软件源（kernel 内核、runtime 运行时、drivers 驱动）。
- `sw/runtime/`：主机运行时与各驱动后端（`simx/`、`rtlsim/`、`opae/`、`xrt/`、`gem5/`、`stub/`）。
- `sim/`：模拟器（核心是 `simx` C++ 仿真器）。
- `hw/`：硬件源（RTL，主要是 SystemVerilog）。
- `ci/`：持续集成与构建脚本（`blackbox.sh`、`gen_config.py`、`regression.sh` 等）。

## 4. 核心概念与源码讲解

### 4.1 Vortex 是什么：全栈开源 RISC-V GPGPU

#### 4.1.1 概念说明

Vortex 的开篇第一句话就给它定了性：

> Vortex is a full-stack open-source RISC-V GPGPU.

翻译过来：**Vortex 是一个「全栈」的开源 RISC-V GPGPU。** 关键词有三个：

1. **全栈（full-stack）**：从你写的主机程序，一直到能在真实 FPGA 上跑的硬件比特流，整条链路都在仓库里。
2. **开源 RISC-V**：底座是开源的 RISC-V 指令集，而不是某家厂商的私有 ISA。
3. **GPGPU**：既能做通用并行计算，也支持图形。

「全栈」体现在它有**多个后端驱动（backend drivers）**：同一个 Vortex，可以被 C++ 仿真器（simx）跑、可以被 RTL 仿真器（rtlsim）跑，也可以烧到真实的 Xilinx / Altera FPGA 上跑。**这三种后端由同一个驱动脚本控制**，你只需要选一个 driver。

#### 4.1.2 核心流程：从主机到 FPGA 的分层

我们可以把全栈自上而下分成五层。下面这张「文字流程图」是后续所有讲义都要反复用到的骨架：

```
┌─────────────────────────────────────────────┐
│ 你的主机程序 (host app)                       │  调用 vortex.h 的 API
│   如 vecadd / sgemm 的 host 侧               │
└──────────────────────┬──────────────────────┘
                       │ vx_dev_open / vx_start ...
┌──────────────────────▼──────────────────────┐
│ 主机运行时 libvortex.so (stub 分发)          │  sw/runtime/
│   按 $VORTEX_DRIVER 选后端                   │
└──────────────────────┬──────────────────────┘
                       │ dlopen 对应后端 .so
        ┌──────────────┼──────────────┬──────────────┐
        ▼              ▼              ▼              ▼
   ┌─────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐
   │ simx    │   │ rtlsim   │   │ opae     │   │ xrt      │
   │ C++仿真 │   │ RTL仿真  │   │ Intel FPGA│  │ Xilinx FPGA│
   │ sim/    │   │ sim/     │   │ hw/rtl/afu│  │ hw/rtl/afu│
   └─────────┘   └──────────┘   └──────────┘   └──────────┘
```

读这张图的方式：

- **最上层**是你写的主机程序，它只看得见一套统一的运行时 API（类似 CUDA / OpenCL 的主机接口）。
- **中间层**是 `libvortex.so`，它是一个「stub（桩）」分发器——首次打开设备时，根据环境变量 `$VORTEX_DRIVER` 动态加载（`dlopen`）真正干活的后端库。所以同一份主机代码，换个 driver 就换了后端。
- **最下层**是四类「Vortex 实体」：纯软件仿真（simx，最快、用来原型设计）、RTL 仿真（rtlsim）、真实 Intel FPGA（opae）、真实 Xilinx FPGA（xrt）。项目的设计哲学是：**先在 simx 里把设计原型跑通，再前推到 RTL 实现。**

仓库目录与这五层的对应关系（稍后 4.4 节会在源码地图里再确认一次）：

| 层 | 仓库目录 |
| --- | --- |
| 主机程序 / 运行时 API / 驱动后端 | `sw/runtime/`（API 头在 `sw/runtime/include/`） |
| 内核（device 侧代码） | `sw/kernel/` |
| C++ 仿真器 | `sim/simx/`（公共代码在 `sim/common/`） |
| RTL 仿真器 | `sim/rtlsim/` |
| 硬件 RTL 源 | `hw/rtl/` |
| 构建与 CI 脚本 | `ci/` |

#### 4.1.3 源码精读

- README 开篇定性：[README.md:3-3](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/README.md#L3-L3)——这一句就是「Vortex 是全栈开源 RISC-V GPGPU」的出处，并点明它有多个 backend driver、由单一驱动脚本控制。
- AGENTS.md 把自己定位为「人类贡献者与 AI 智能体共同的入口」：[AGENTS.md:1-5](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/AGENTS.md#L1-L5)，记录的是「规则」（不变量与坑），而非具体步骤。
- 五种驱动后端在仓库里的真实位置：运行时后端目录 `sw/runtime/{simx,rtlsim,opae,xrt,gem5,stub}/`，模拟器侧 `sim/{simx,rtlsim,opaesim,xrtsim}/`。这些目录确实存在，是 stub 分发的落地证据。
- 一个重要纪律（先记下来，后续讲义会反复用到）：当 RTL 调试卡住时，官方推荐的路径是用 simx 当「预言机（oracle）」做 trace diff，见 [AGENTS.md:90-90](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/AGENTS.md#L90-L90)。这说明 simx 与 RTL 在项目里是「成对、需保持一致」的两套模型。

#### 4.1.4 代码实践

> 本讲定位为「源码阅读型实践」——你不需要跑通编译，只需要会读仓库。

1. **实践目标**：亲手确认「全栈五层」在仓库里都有对应目录。
2. **操作步骤**：
   - 在仓库根目录执行 `ls -d sw/ sim/ hw/ ci/ tests/`，确认这些一级目录都存在。
   - 执行 `ls sw/runtime/`，确认里面确实有 `simx`、`rtlsim`、`opae`、`xrt`、`gem5`、`stub` 这几个后端目录。
   - 执行 `ls sim/`，确认模拟器侧有 `simx`、`rtlsim`、`opaesim`、`xrtsim`。
3. **需要观察的现象**：每个后端在 `sw/runtime/` 和 `sim/`（或 `hw/rtl/afu/`）里各有一个对应实现。
4. **预期结果**：你能把 4.1.2 的分层图里每一个方框，指到仓库里的一个具体目录。
5. 如果当前环境无法执行命令：**待本地验证**——可改为在 GitHub 仓库网页上点开对应目录确认。

#### 4.1.5 小练习与答案

- **练习 1**：为什么说 Vortex 是「全栈」？「全栈」体现在哪？
  - **答案**：因为它把「主机运行时 → 驱动分发 → 仿真器 / RTL / FPGA」整条链路都开源在同一个仓库里，并且同一份主机代码可以通过选不同的 driver 跑在软件仿真或真实 FPGA 上。
- **练习 2**：项目建议的开发顺序是「先 simx，再 RTL」，原因是什么？
  - **答案**：simx 是纯 C++ 仿真，迭代快、易调试，适合做设计原型；原型跑通后再前推到 RTL 实现并最终上 FPGA（见 README [README.md:3-3](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/README.md#L3-L3) 中 "prototype their intended design in simx, before ... going forward with an RTL implementation"）。

### 4.2 SIMT 执行模型与硬件层次

#### 4.2.1 概念说明

GPU 之所以快，靠的是「一条指令、成百上千个线程并行」。Vortex 用的是 **SIMT（Single Instruction, Multiple Threads）** 模型，并且「每个周期发射一个 warp（single warp issued per cycle）」。

理解 SIMT，关键是理解三个层次：

1. **Thread（线程）**：最小的计算单位。每个线程有自己的寄存器堆（32 个整型 + 32 个浮点寄存器）。
2. **Warp（线程束）**：一组线程的「逻辑簇」。同一个 warp 内的线程**执行同一条指令**——它们共享同一个 PC，硬件用一个「线程掩码（thread mask / tmask）」标记哪些线程真正参与写回。
3. **Warp 的时间复用**：多个 warp 不是同时跑，而是被调度器**按周期轮流**发射。比如 warp 0 在周期 0 发射，warp 1 在周期 1 发射——这叫「在 log 步长上时间复用」。

在这之上还有聚类（clustering）层次，用来组织缓存共享：

- **Socket**：一组共享 L1 缓存的 core。
- **Cluster**：一组共享 L2 缓存的 socket。

> 这就解释了为什么 Vortex「可扩展」——你可以配置 core、warp、thread 的数量，也可以配置 socket / cluster 的规模，让缓存共享边界落在不同粒度。

#### 4.2.2 核心流程：线程与 warp 的执行

一个 warp 在一个周期内的执行，可以简化为下面的伪代码：

```
每个周期：
  scheduler 选一个「就绪且未阻塞」的 warp
  PC <- 该 warp 当前的 PC
  取指 / 译码 / 取操作数 / 执行
  对于写回阶段：
      for 每个线程 t in warp:
          if tmask[t] == 1:   # 该线程活跃
              regfile[t][rd] <- result   # 只写活跃线程的寄存器
  warp.PC <- next_PC          # 整个 warp 共享 PC 的推进
```

要点：

- **PC 是 warp 级别共享的**，不是每个线程一个 PC。
- **tmask 决定写回**：分支会让一部分线程走、一部分不走，靠掩码把「不走的线程」暂时屏蔽，但它们的 PC 仍然跟着 warp 走。
- **时间复用**：多个 warp 排队轮转，隐藏了访存等长延迟。

#### 4.2.3 源码精读

- SIMT 模型的权威定义：[microarchitecture.md:4-5](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/microarchitecture.md#L4-L5)——「Vortex uses the SIMT execution model with a single warp issued per cycle」。
- Thread 的定义（每个线程有自己的 32 int + 32 fp 寄存器）：[microarchitecture.md:7-11](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/microarchitecture.md#L7-L11)。
- Warp 的定义（共享 PC、靠 thread mask 控制写回、按周期时间复用）：[microarchitecture.md:13-17](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/microarchitecture.md#L13-L17)。
- Socket / Cluster 的缓存共享边界：[microarchitecture.md:78-82](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/microarchitecture.md#L78-L82)。

#### 4.2.4 代码实践

1. **实践目标**：用文档把「thread → warp → socket → cluster」四层关系讲清楚。
2. **操作步骤**：打开 [microarchitecture.md](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/microarchitecture.md)，逐条阅读 **Threads**、**Warps**、**Vortex clustering architecture** 三段。
3. **需要观察的现象**：注意「warp 内线程共享 PC」与「socket/cluster 共享缓存」是两个不同维度的「共享」——前者是计算维度的共享，后者是存储维度的共享。
4. **预期结果**：你能用自己的话回答「socket 共享什么？cluster 共享什么？warp 共享什么？」。
5. 如想进一步验证硬件是否真的这样配置：可查阅 `VX_config.toml`（本讲不展开，留给 U2 讲义），**待本地验证**。

#### 4.2.5 小练习与答案

- **练习 1**：如果一个 warp 内有 8 个线程，其中 3 个因分支被屏蔽，写回阶段会发生什么？
  - **答案**：写回时只对 tmask 为 1 的 5 个线程写寄存器；被屏蔽的 3 个线程不写回，但它们的 PC 仍随 warp 一起推进。
- **练习 2**：socket 和 cluster 各自共享哪一级缓存？
  - **答案**：socket 共享 L1，cluster 共享 L2（见 [microarchitecture.md:78-82](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/microarchitecture.md#L78-L82)）。

### 4.3 RISC-V ISA 扩展：类 GPU 的控制指令

#### 4.3.1 概念说明

Vortex 的精髓在于：**它没有发明一个全新的 GPU 指令集，而是在 RISC-V 上做了「最小化」的扩展。** 这是项目最重要的设计取舍——最小化 ISA 改动，意味着对开源生态（编译器、工具链、内核）的改动也最小，从而可持续。

Vortex 给 RISC-V 加的扩展可以分四组，对应 SIMT 需要的四类控制能力：

| 指令 | 全称 / 含义 | 解决的问题 |
| --- | --- | --- |
| `TMC` *count* | Thread Mask Control | 激活 count 个线程 |
| `WSPAWN` *count, addr* | Warp Spawn | 派生 count 个 warp，跳到 addr |
| `SPLIT` *taken, predicate* | （分支发散）保存当前掩码到 IPDOM 栈 | 处理 warp 内线程走不同分支 |
| `JOIN` | 弹出 IPDOM 栈恢复掩码 | 分支发散后的汇聚 |
| `PRED` *predicate, restore_mask* | 线程谓词指令 | 按谓词激活/屏蔽线程 |
| `BAR` *id, count* | Barrier | 让进入 barrier *id* 的 warp 停住，直到达到 count 个 |

其中 `SPLIT` / `JOIN` 依赖一个叫 **IPDOM 栈（Immediate Post-Dominator 栈）** 的硬件结构——简单理解：当分支让 warp 内线程「分叉」时，把当前掩码压栈；等这些线程最终在「汇聚点（post-dominator）」重新汇合时，再弹栈恢复。

> 概念提示：**IPDOM = Immediate Post-Dominator（直接后必经节点）**。在控制流图里，一个分支的两个分支路径一定会重新汇合的最早那个点，就是 post-dominator。Vortex 用栈来管理这种「分叉 → 汇聚」的嵌套。

#### 4.3.2 核心流程：一次分支发散与汇聚

下面用伪代码描述 warp 内线程遇到 `if/else` 分支时，`SPLIT` / `JOIN` 如何配合：

```
假设 warp 进入 if (cond)，cond 对部分线程为真、部分为假：

  SPLIT taken, predicate
      # taken = 哪些线程走 if 分支
      # 把「当前完整掩码」压入 IPDOM 栈
      # 当前激活掩码 <- 走 if 分支的线程
  ... 执行 if 分支体 ...
  JOIN
      # 弹出 IPDOM 栈，恢复成「走 else 分支的线程」
  ... 执行 else 分支体 ...
  JOIN
      # 再次弹出，恢复成最初「全部活跃」的掩码
```

要点：

- `SPLIT` 负责「分叉」——压栈 + 切换到某一支。
- `JOIN` 负责「汇聚」——弹栈 + 切到另一支或恢复全活跃。
- `BAR` 是 warp 之间的同步：多个 warp 跑到同一个 barrier 时停下来等齐，再一起继续。

#### 4.3.3 源码精读

- ISA 扩展总入口（标注 "Vortex RISC-V ISA Extension"）：[microarchitecture.md:18-19](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/microarchitecture.md#L18-L19)。
- `TMC`：[microarchitecture.md:20-22](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/microarchitecture.md#L20-L22)（注意它的小标题是 "Warp Scheduling"，按文档原文理解即可）。
- `WSPAWN`：[microarchitecture.md:23-25](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/microarchitecture.md#L23-L25)。
- `SPLIT` / `JOIN` / `PRED`（控制流发散与汇聚）：[microarchitecture.md:26-30](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/microarchitecture.md#L26-L30)——这里明确写了 SPLIT 会 "save current state into IPDOM stack"，JOIN 会 "pop IPDOM stack to restore thread mask"。
- `BAR` 屏障：[microarchitecture.md:31-32](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/microarchitecture.md#L31-L32)。

> 提示：这些指令在设备侧代码里以「内联函数」形式暴露，定义在 `sw/kernel/include/vx_intrinsics.h` 与 `sw/kernel/include/vx_spawn.h`，U4 讲义会精讲。本讲只需记住名字和用途。

#### 4.3.4 代码实践

1. **实践目标**：在仓库里找到这些 ISA 扩展的「软件入口」证据。
2. **操作步骤**：
   - 执行 `ls sw/kernel/include/`，确认存在 `vx_intrinsics.h` 和 `vx_spawn.h`。
   - （可选）用 `grep -rn "WSPAWN\|csrrw" sw/kernel/include/vx_intrinsics.h` 看看这些控制指令如何被包装成 C 内联函数。
3. **需要观察的现象**：每条扩展指令（TMC / WSPAWN / SPLIT / JOIN / PRED / BAR）都能在头文件里找到一个对应的内联函数封装。
4. **预期结果**：你能在 `vx_intrinsics.h` 里定位到至少 `vx_tmc`（对应 TMC）和 `vx_wspawn`（对应 WSPAWN）这样的函数名。
5. 若无 grep 环境：**待本地验证**——可改为在 GitHub 网页打开 `sw/kernel/include/vx_intrinsics.h` 搜索。

#### 4.3.5 小练习与答案

- **练习 1**：为什么 Vortex 选择「最小化扩展 RISC-V」而不是设计全新 ISA？
  - **答案**：最小化 ISA 改动 → 对编译器/工具链/内核等开源生态的改动也最小 → 生态更可持续（这也是项目论文的核心论点）。
- **练习 2**：`SPLIT` 和 `JOIN` 各自对 IPDOM 栈做了什么？
  - **答案**：`SPLIT` 把当前线程掩码压入 IPDOM 栈并切换到某一支；`JOIN` 弹出栈恢复掩码，实现分支发散后的汇聚（见 [microarchitecture.md:27-29](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/microarchitecture.md#L27-L29)）。
- **练习 3**：`BAR` 指令的 `count` 参数有什么用？
  - **答案**：进入 barrier *id* 的 warp 会停住，直到累计达到 count 个 warp 到达才一起放行（见 [microarchitecture.md:31-32](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/microarchitecture.md#L31-L32)）。

### 4.4 六级流水线与仓库目录地图

#### 4.4.1 概念说明

Vortex 的核心是一条 **6 级流水线**。这是后续所有 SimX / RTL 讲义的共同骨架，本讲先建立整体印象：

1. **Schedule（调度）**：Warp Scheduler 选下一个 warp 的 PC 送进流水线；IPDOM 栈管理发散/汇聚；Inflight Tracker 跟踪在飞指令。
2. **Fetch（取指）**：从内存取指令，处理 I-cache。
3. **Decode（译码）**：把取来的指令译码；遇到控制指令要通知调度器。
4. **Issue（发射）**：IBuffer 按 warp 存放已译码指令；Scoreboard 跟踪寄存器占用、检查冒险；Operands Collector 取操作数。
5. **Execute（执行）**：分到各功能单元——ALU（算术/分支）、FPU（浮点）、LSU（访存）、SFU（warp 控制 + CSR）、TCU（矩阵乘加）。
6. **Commit（写回）**：把结果写回寄存器堆并更新 Scoreboard。

与此同时，README 给的目录结构是我们逛仓库的「地图」。把流水线各级和目录对应起来，后续读源码就不会迷路。

#### 4.4.2 核心流程：流水线数据通路

一条指令在 Vortex 核心里的生命周期（简化）：

```
Schedule → Fetch → Decode → Issue → Execute → Commit
  选warp     取指    译码     发射      执行       写回
  (PC/tmask)         (通知调度) (IBuffer  (ALU/FPU/  (写寄存器
                                +Score    LSU/SFU/   +更新Score)
                                +取操作数) TCU)
```

对应的仓库目录（为后续讲义预热）：

- SimX 里这条流水线由 `sim/simx/` 下的一系列 `.cpp` 实现（如 `scheduler.cpp`、`decode.cpp`、`scoreboard.cpp`、`alu_unit.cpp` 等）。
- RTL 里同一条流水线由 `hw/rtl/core/` 下的一系列 `VX_*.sv` 实现（如 `VX_fetch.sv`、`VX_decode.sv`、`VX_issue.sv`、`VX_execute.sv`、`VX_commit.sv`）。
- **这两套实现必须保持功能/时序一致**——这就是后面会反复出现的「SimX ↔ RTL model parity」主线。

#### 4.4.3 源码精读

- 6 级流水线总述：[microarchitecture.md:38-39](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/microarchitecture.md#L38-L39)（"Vortex has a 6-stage pipeline"）。
- Schedule 级（含 Warp Scheduler / IPDOM Stack / Inflight Tracker）：[microarchitecture.md:40-47](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/microarchitecture.md#L40-L47)。
- Fetch / Decode：[microarchitecture.md:48-54](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/microarchitecture.md#L48-L54)。
- Issue（IBuffer / Scoreboard / Operands Collector）：[microarchitecture.md:55-62](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/microarchitecture.md#L55-L62)。
- Execute（ALU/FPU/LSU/SFU/TCU）：[microarchitecture.md:63-74](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/microarchitecture.md#L63-L74)。
- Commit：[microarchitecture.md:75-76](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/microarchitecture.md#L75-L76)。
- 仓库目录地图：[README.md:52-61](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/README.md#L52-L61)——这里逐条标注了 `hw` / `sw` / `sim` / `tests` / `ci` 的职责。
- 一条快速跑通 demo 的命令（验证整个栈是否联通的最简单方式）：[README.md:122-125](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/README.md#L122-L125)（`./ci/blackbox.sh --cores=2 --app=vecadd`）。

#### 4.4.4 代码实践

1. **实践目标**：把 6 级流水线分别对应到 SimX 与 RTL 的真实文件。
2. **操作步骤**：
   - 执行 `ls hw/rtl/core/`，找到 `VX_fetch.sv`、`VX_decode.sv`、`VX_issue.sv`、`VX_execute.sv`、`VX_commit.sv`、`VX_core.sv`。
   - 执行 `ls sim/simx/`，找到 `scheduler.cpp`、`decode.cpp`、`scoreboard.cpp`、`alu_unit.cpp`、`lsu_unit.cpp` 等。
3. **需要观察的现象**：同一个流水级，在 SimX（`.cpp`）和 RTL（`.sv`）里各有一个文件——这正是「两套模型成对存在」的证据。
4. **预期结果**：你能填出一张「流水级 → RTL 模块 → SimX 模块」的对照表雏形。
5. 若某文件名与预期不符：**待本地验证**——文件清单可能随版本变化，以你本地仓库为准。

#### 4.4.5 小练习与答案

- **练习 1**：Issue 级里 Scoreboard 的作用是什么？
  - **答案**：跟踪「哪些寄存器正在被使用」，在发射指令前检查数据冒险，避免读到未写回的旧值（见 [microarchitecture.md:57-60](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/microarchitecture.md#L57-L60)）。
- **练习 2**：仓库里哪个目录是「持续集成脚本」？哪个是「硬件源」？
  - **答案**：`ci/` 是持续集成脚本，`hw/` 是硬件源（见 [README.md:56-60](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/README.md#L56-L60)）。

## 5. 综合实践

这是本讲的主实践任务，把前面四个模块串起来。

**任务**：画一张 **「主机 → 驱动后端 → 模拟器 / RTL / FPGA」的全栈分层图**，并标注每一层对应的仓库目录。

**操作步骤**：

1. 读 [README.md:3-3](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/README.md#L3-L3) 与 [README.md:52-61](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/README.md#L52-L61)。
2. 读 [microarchitecture.md:4-5](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/microarchitecture.md#L4-L5)、[microarchitecture.md:38-39](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/microarchitecture.md#L38-L39)、[microarchitecture.md:78-82](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/microarchitecture.md#L78-L82)。
3. 在图上至少标注：主机程序层、`libvortex.so` stub 分发层、四个后端（simx / rtlsim / opae / xrt），以及每个后端对应的仓库目录（`sim/simx`、`sim/rtlsim`、`hw/rtl/afu/opae`、`hw/rtl/afu/xrt`）。
4. 在图旁用一句话写明：为什么 simx 与 rtl 必须保持一致（提示：参考 [AGENTS.md:89-89](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/AGENTS.md#L89-L89) 的 model_parity）。
5. （进阶，可选）在图的最上层画出一条指令在核心里经过的 6 级流水线（Schedule → Fetch → Decode → Issue → Execute → Commit）。

**预期结果**：一张你自己画的、每个方框都能指到真实目录的全栈分层图。这张图会贯穿后续所有讲义，请保存好。

> 说明：本实践是阅读与画图型任务，不要求运行任何编译命令；运行 demo 的实践放在下一讲（U1-L4「首次运行」）。

## 6. 本讲小结

- **Vortex 是全栈开源 RISC-V GPGPU**：从主机运行时到 FPGA 比特流都在一个仓库里，支持 simx / rtlsim / opae / xrt 多后端，由单一驱动脚本控制。
- **SIMT 模型**：thread 是最小单位（各有寄存器堆）；warp 内线程共享 PC、靠 tmask 控制写回、按周期时间复用；warp 之间靠调度器轮转隐藏延迟。
- **硬件聚类层次**：socket 共享 L1，cluster 共享 L2——这是 Vortex 可扩展性的组织方式。
- **最小化 ISA 扩展**：Vortex 只给 RISC-V 加了 TMC / WSPAWN / SPLIT / JOIN / PRED / BAR 几条类 GPU 控制指令，其中 SPLIT/JOIN 靠 IPDOM 栈处理分支发散与汇聚。
- **6 级流水线**：Schedule → Fetch → Decode → Issue → Execute → Commit，是 SimX 与 RTL 两套实现的共同骨架。
- **贯穿主线**：SimX 是 RTL 的时序模型，两者必须保持一致（model_parity）——这条主线会在后续讲义反复出现。

## 7. 下一步学习建议

本讲建立了「全栈 + SIMT + ISA 扩展 + 流水线骨架」的心智模型。接下来建议：

1. **U1-L2「仓库目录结构地图」**：用 `docs/codebase.md` 把每一级目录的职责摸清，让你能在仓库里「不迷路」。
2. **U1-L3「构建系统、configure 与工具链」**：理解 out-of-tree 构建、`configure` 脚本、以及「改了 toml/Makefile 必须重新 configure」这条核心规则。
3. **U1-L4「首次运行：用 blackbox.sh 跑通 demo」**：亲手用 `./ci/blackbox.sh` 在 SimX 上跑通第一个程序，把本讲的「全栈分层」从纸面变成可运行的现实。

如果你已经等不及想看代码，可以先扫一眼 `sw/runtime/include/vortex.h`（主机 API）和 `sim/simx/main.cpp`（仿真器入口）——它们分别是软件栈和仿真器的两个「门」，会在 U3 与 U5 讲义里精讲。
