# 目录结构与源码组织

## 1. 本讲目标

学完本讲后，你应当能够：

- 说出 Ventus（乘影）仓库**顶层有哪些目录**，以及每个目录扮演什么角色（源码、仿真、依赖、文档、构建）。
- 在 `ventus/src/` 下**按功能域定位代码**：顶层、流水线、CTA 调度、L1/L2 缓存、MMU、AXI、SRAM 模板、配置分别放在哪里。
- 理解 `dependencies/` 里的 **git 子模块**分别来自哪个上游项目、提供什么能力。
- 看懂 `GPGPU_top.scala` 如何把分散在不同目录里的模块 `import` 进来、`Module(new ...)` 例化成完整硬件，从而把「目录结构」与「数据通路」对应起来。

本讲不深入任何一个模块的内部实现，只建立一张「**代码住在哪、彼此怎么找**」的地图。后续每一篇讲义都会落到这张地图的某一块上。

## 2. 前置知识

在开始前，先回忆几个关键概念（详见 u1-l1）：

- **Chisel / Scala**：Ventus 的硬件用 Chisel 描述，最终经 FIRRTL→firtool 生成 Verilog。一个 `.scala` 文件里通常会定义若干 `class Xxx extends Module`（硬件模块）和 `object Xxx`（参数 / 工具对象）。
- **package（包）**：Scala 用 `package` 组织命名空间。Chisel 里 `package` 名通常和**目录名一致**（如 `ventus/src/pipeline/` 下的文件几乎都是 `package pipeline`），但这只是约定，并非强制——本讲会指出几个例外。
- **git submodule（子模块）**：一个大项目可以把第三方代码作为子模块引用，源码放在 `dependencies/` 下，由 `make init` 拉取（详见 u1-l2）。
- **顶层模块 GPGPU_top**：整个 GPU 的 RTL 顶层，它的端口 `host_req/host_rsp` 接 Host、`out_a/out_d` 接外部内存。

> 如果你还没读过 u1-l1（项目定位）和 u1-l2（构建系统），建议先读，本讲会直接复用其中的术语。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 / 目录 | 作用 |
| --- | --- |
| `README.md` | 项目说明、快速开始、子模块来源致谢表 |
| `.gitmodules` | 声明 5 个 git 子模块的路径与上游 URL |
| `ventus/src/top/GPGPU_top.scala` | 硬件顶层，例化 CTA 接口、SM 集群、L2、集群互联，是「目录如何串成硬件」的最佳切入点 |
| `ventus/src/top/parameters.scala` | 全局参数对象，决定 `num_sm`/`num_warp`/`num_thread` 等规模 |
| `ventus/src/top/ExtMem_gen.scala` | 包含 `object GPGPU_gen`，是 `make verilog` 的 elaboration 入口 |
| `dependencies/` | 5 个子模块：rocket-chip、inclusive-cache、hardfloat、fpuv2、Membox2.Scala |

## 4. 核心概念与源码讲解

### 4.1 仓库顶层布局

#### 4.1.1 概念说明

一个开源硬件仓库通常由四类东西组成：**源码、构建脚本、仿真框架、外部依赖**，再加上文档与 CI 配置。先认清顶层每个目录属于哪一类，后面读代码就不会迷路。

#### 4.1.2 顶层目录一览

用 `git ls-files` 取出仓库跟踪的所有顶层条目（已去除杂项），可归为下表：

| 顶层条目 | 类别 | 作用 |
| --- | --- | --- |
| `ventus/` | **源码** | 主体 RTL：`ventus/src`（Chisel）+ `ventus/tests`（测试）+ `ventus/txt`（旧测试用例）+ `ventus/fpga_test`（FPGA 工程） |
| `sim-verilator/` | **仿真** | 基于 Verilator 的定制仿真框架（C++ driver + testcase），是当前**正式**的 RTL 仿真入口 |
| `sim-verilator-nocache/` | **仿真** | `sim-verilator` 的无缓存变体/参考实现 |
| `dependencies/` | **依赖** | 5 个 git 子模块，提供浮点、配置、缓存等底层 IP |
| `scripts/` | **脚本** | FPGA 流程辅助脚本，如 `gen_sep_mem.sh`、`vlsi_mem_gen` |
| `docs/` | **文档** | 架构白皮书、ICCD/MICRO 演讲、CTA scheduler 设计文档 |
| `build.sc` / `common.sc` / `mill` / `.config/mill-version` | **构建** | Mill 构建定义与版本（详见 u1-l2） |
| `Makefile` | **构建** | 暴露 `make init / verilog / fpga-verilog` 等目标 |
| `.github/` / `shell.nix` | **CI / 环境** | CI 配置与 Nix 可复现环境 |

> 注意：`make test`（chiseltest）已**废弃**，旧测试产物在 `ventus/txt/` 与 `ventus/tests/`，正式仿真请用 `sim-verilator/`（README 第 105 行）。

#### 4.1.3 源码精读

`README.md` 的「Acknowledgement」表直接说明了哪些设计借鉴自外部项目，这能帮你理解 `dependencies/` 的来历：

[README.md:257-262](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/README.md#L257-L262) — 致谢表，点明 CTA scheduler 源自 MIAOW、L2Cache 借鉴 Sifive block-inclusivecache、乘法器/FPU 借鉴香山、配置等源自 rocket-chip。

同时 README 也划清了**工具链边界**——本仓库只做硬件 RTL，编译器在 `ventus-llvm`，ISA 模拟器/pocl/driver 是姊妹仓库，`ventus-env` 统一打包：

[README.md:62-64](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/README.md#L62-L64) — 说明 OpenCL 编译器由兆松科技基于 LLVM 开发，isa-simulator / pocl / driver 在配套仓库。

#### 4.1.4 代码实践

1. **目标**：把顶层目录分成「源码 / 仿真 / 依赖 / 构建 / 文档」五类。
2. **步骤**：执行 `git ls-files | cut -d/ -f1 | sort -u` 列出所有顶层条目。
3. **观察**：你会看到 `ventus`、`sim-verilator`、`sim-verilator-nocache`、`dependencies`、`scripts`、`docs`、`build.sc`、`Makefile` 等。
4. **预期结果**：能逐条归入上面表格的五类。
5. 待本地验证：不同发行版 `cut/sort` 行为一致，结果应与上表吻合。

#### 4.1.5 小练习与答案

**练习 1**：`sim-verilator` 和 `ventus/tests` 都是「测试」，它们有什么区别？
**答案**：`ventus/tests` 是已废弃的 chiseltest（`make test`）相关 Scala 测试与工具；`sim-verilator` 是当前正式使用的、基于 Verilator 的 C++ 仿真框架，含 driver 与 `.metadata/.data` 测试用例。

**练习 2**：为什么仓库里能直接出现 `mill`、`build.sc` 这类构建文件，而源码却只在 `ventus/` 下？
**答案**：Mill 构建定义必须放在仓库根，`build.sc` 里的 `ventus` 模块才把 `ventus/src` 作为源码目录纳入编译；硬件源码集中在 `ventus/` 是为了让顶层保持干净、便于打包发布。

---

### 4.2 ventus/src 核心源码分区

#### 4.2.1 概念说明

`ventus/src/` 是全部自研 RTL 的家。它按**功能域**切分成若干子目录，几乎每个子目录对应一个 `package`，也对应一类硬件职责。看懂这一层，你就拿到了后续所有讲义的「坐标系」。

#### 4.2.2 各子目录职责

下表是 `ventus/src/` 下各子目录的 Scala 文件数量与职责（数量来自 4.2.4 的实践命令，可作为参考）：

| 子目录 | 文件数 | `package` | 功能域 |
| --- | --- | --- | --- |
| `pipeline/` | 25 | `pipeline` | **SM 流水线**主体：取指、译码、ibuffer、记分板、发射、寄存器堆、操作数收集、各类执行单元（ALU/FPU/LSU/SFU/MUL/SIMT）、写回、CSR |
| `L1Cache/` | 17 | `L1Cache` 及子包 | **L1 缓存层次**：根目录放 ICache/DCache 公共件（TagAccess、MSHR、接口、仲裁器）；`ICache/`、`DCache/`、`ShareMem/`、`AtomicUnit/` 是细分 |
| `L2cache/` | 14 | `L2cache` | **L2 缓存**：参考 Sifive block-inclusivecache 的 Scheduler/SourceA/D/SinkA/D/BankedStore/Directory/MSHR |
| `top/` | 11 | `top` | **顶层与参数**：`GPGPU_top`、`parameters`、各种 SimWrapper/ExtMemWrapper、外部内存模型、elaboration 入口 |
| `cta/` | 6 | `cta`（`utils.scala` 为 `cta.utils`） | **CTA 调度器**：`cta_scheduler`、`wg_buffer`、`resource_table`、`allocator`、`cu_interface` |
| `mmu/` | 4 | `mmu` | **可选 MMU**：`L1TLB`、`L2TLB`、`PTW`、`AsidLookup`（由 `MMU_ENABLED` 开关控制，默认关） |
| `axi/` | 3 | `axi` | **AXI 接口**：`AXI4Lite2CTA`（Host 派发）、`AXI4Adapter`（L2↔AXI4 外存桥）、`AXI4Lite`（通道定义） |
| `SRAMTemplate/` | 3 | `SRAMTemplate` | **SRAM 模板**：可综合的 SRAM 包装（`SRAMTemplate`）、LFSR、Hold 工具，供缓存层使用 |
| `config/` | 1 | `config` | **CDE 配置系统**：源自 rocket-chip 的 `Field/View/Parameters` 链式查询 |
| `GvmDutApi.scala`（根级） | 1 | `gvm` | **协同仿真钩子**：GVM 对拍用的 DUT 状态提取接口（`GvmDutCta2Warp` 等，由 `GVM_ENABLED` 控制） |

> 两个「例外」值得注意：① `GvmDutApi.scala` 直接放在 `ventus/src/` 根级，但其 `package` 是 `gvm`（不是目录名）；② `cta/utils.scala` 的包是子包 `cta.utils`。这说明 **包名与目录名只是约定**，定位代码时要以 `package` 声明为准。

#### 4.2.3 核心流程：目录 → 模块 → 数据通路

把目录按 GPU 的数据通路串起来，就是这样的对应：

```
Host ── axi/AXI4Lite2CTA ──► cta/(调度器)
                                   │ CTA2warp (pipeline/CTA2warp.scala)
                                   ▼
                           pipeline/(SM 流水线: 取指→译码→执行→写回)
                                   │
              ┌────────────────────┼────────────────────┐
              ▼                    ▼                    ▼
      L1Cache/ICache        L1Cache/DCache        L1Cache/ShareMem
              │                    │
              └─────► L1Cache/L1Cache2L2Arbiter ◄─────┘
                                   │
                                   ▼
            top/ 里的集群互联 (SM2clusterArbiter/l2Distribute/cluster2L2Arbiter)
                                   │
                                   ▼
                         L2cache/Scheduler ──► axi/AXI4Adapter ──► DDR
```

也就是说：横向（左→右）对应「**目录分区**」，纵向（顶→底）对应「**目录间的例化与连线**」。这两条线最终都汇聚在 `top/GPGPU_top.scala` 里。

#### 4.2.4 源码精读

**① GPGPU_top 的 import 揭示了它依赖哪些目录。** 顶层模块开头一次性 `import` 了几乎所有功能域的包，等于把目录结构「翻译」成了 Scala 的依赖声明：

[ventus/src/top/GPGPU_top.scala:17-30](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L17-L30) — 依次引入 `L1Cache.ICache`、`L1Cache`、`L1Cache.DCache`、`L1Cache.ShareMem`、`config.config`、`pipeline`、`L2cache`、`cta.cta_scheduler_top`、`axi`、`mmu`、`gvm`，正好对应 4.2.2 表里的目录。

**② GPGPU_top 的四件套例化，对应四个目录。** `GPGPU_top` 类内部把 CTA、SM 集群、L2、集群互联一次性建好：

[ventus/src/top/GPGPU_top.scala:164-169](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L164-L169) — 例化 `CTAinterface`（来自 `cta/`）、`SM_wrapper` 数组（`pipeline/`+`L1Cache/`）、`Scheduler`（`L2cache/`）以及 `SM2clusterArbiter`/`l2Distribute`/`cluster2L2Arbiter`（本文件内定义的集群互联）。

**③ SM_wrapper 把一个 SM 内部的目录模块全部装进去。** 进入单个 SM，可以看到 `cta/`（CTA2warp 在 `pipeline/`）、`pipeline/`、`L1Cache/` 三大目录如何在一个 SM 内合体：

[ventus/src/top/GPGPU_top.scala:351-365](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L351-L365) — 例化 `CTA2warp`（`pipeline/CTA2warp.scala`）、`pipe`（`pipeline/pipe.scala`，SM 主流水线）、`L1Cache2L2Arbiter`（`L1Cache/`）。

紧接着例化两块 L1 缓存与共享内存：

[ventus/src/top/GPGPU_top.scala:369-414](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L369-L414) — `InstructionCache`（`L1Cache/ICache/`）与 `DataCache`（`L1Cache/DCache/`），二者经 `L1Cache2L2Arbiter` 汇聚。

[ventus/src/top/GPGPU_top.scala:484-489](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L484-L489) — `SharedMemory`（`L1Cache/ShareMem/`）直接接流水线的 `shared_req/shared_rsp`。

**④ 规模由 `top/parameters.scala` 一处决定。** 这个文件是所有目录里模块共用的「配置中心」：

[ventus/src/top/parameters.scala:6-9](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/parameters.scala#L6-L9) — `object parameters` 定义 `num_sm=2`、`num_warp=8`、`num_thread=32`，默认规模即由此而来。

**⑤ elaboration 入口在 `top/ExtMem_gen.scala`。** `make verilog` 命令里的 `top.GPGPU_gen` 就指它：

[ventus/src/top/ExtMem_gen.scala:22-22](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/ExtMem_gen.scala#L22-L22) — `object GPGPU_gen extends App`，是生成 Verilog 的 Scala 入口。

#### 4.2.5 代码实践

1. **目标**：用一条命令统计 `ventus/src/` 各子目录的 Scala 文件数量，验证 4.2.2 的表格。
2. **步骤**：在仓库根执行
   ```bash
   git ls-files 'ventus/src/**/*.scala' | cut -d/ -f1-3 | sort | uniq -c | sort -rn
   ```
3. **观察（参考输出）**：
   ```
     25 ventus/src/pipeline
     17 ventus/src/L1Cache
     14 ventus/src/L2cache
     11 ventus/src/top
      6 ventus/src/cta
      4 ventus/src/mmu
      3 ventus/src/axi
      3 ventus/src/SRAMTemplate
      1 ventus/src/config
   ```
4. **预期结果**：合计 84 个文件落在命名子目录里。
5. **注意**：上面的 glob `ventus/src/**/*.scala` **不会**匹配直接放在 `ventus/src/` 根下的 `GvmDutApi.scala`（因为 `**/` 至少要求一层子目录）。要把它也算进来，改用：
   ```bash
   git ls-files 'ventus/src/' | grep -c '\.scala$'   # 应为 85
   ```
   这正好印证了 4.2.2 提到的「`GvmDutApi.scala` 是根级例外」。

#### 4.2.6 小练习与答案

**练习 1**：你想修改「每个 SM 里 warp 数量」的默认值，应该去哪个目录的哪个文件？
**答案**：去 `top/` 目录的 `parameters.scala`，修改 `object parameters` 里的 `num_warp`（第 8 行）。

**练习 2**：`pipeline/` 目录有 25 个文件，明显比 `cta/`（6 个）多得多，这说明什么？
**答案**：说明 SM 流水线是 Ventus 中**最复杂、最细分**的功能域（取指/译码/发射/多类执行单元/写回/CSR/SIMT 分支等都要单独成文件），而 CTA 调度器职责相对集中，故文件较少。文件数是衡量模块复杂度的一个粗略指标。

**练习 3**：`L1Cache/` 下为什么还要再分 `ICache/`、`DCache/`、`ShareMem/`、`AtomicUnit/` 四个子目录？
**答案**：因为 L1 层内部有四种用途不同的存储结构（指令缓存、数据缓存、片上共享内存、原子操作单元），各自有独立的参数、MSHR 和接口，分目录便于隔离设计与复用，`package` 也随之细分为 `L1Cache.ICache` 等。

---

### 4.3 子模块与外部依赖（dependencies）

#### 4.3.1 概念说明

Ventus 不从零造所有轮子。浮点运算、配置系统、L2 缓存框架等成熟 IP 通过 **git 子模块**引入，源码统一放在 `dependencies/`，由 `.gitmodules` 声明、`make init` 拉取（见 u1-l2）。理解每个子模块的来源，能帮你在遇到 `freechips.rocketchip`、`hardfloat`、`fpuv2` 等陌生包名时知道去哪查。

#### 4.3.2 核心流程：子模块 → 提供的能力

`.gitmodules` 声明了 5 个子模块：

[.gitmodules:1-17](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/.gitmodules#L1-L17) — 声明 `dependencies/` 下的 rocket-chip、rocket-chip-inclusive-cache、berkeley-hardfloat、fpuv2、Membox2.Scala。

它们的对应关系如下：

| 子模块路径 | 上游 | 提供的能力（被 Ventus 哪里用到） |
| --- | --- | --- |
| `dependencies/rocket-chip` | chipsalliance/rocket-chip | CDE 配置系统（`config/config.scala` 即源自此）、`freechips.rocketchip.amba.axi4` 等 AMBA/ diplomacy 基础设施、部分 ALU 设计 |
| `dependencies/rocket-chip-inclusive-cache` | chipsalliance/rocket-chip-inclusive-cache | `L2cache/` 目录所参考的 block-inclusive cache 框架基础 |
| `dependencies/berkeley-hardfloat` | ucb-bar/berkeley-hardfloat | 浮点数转换/分类等硬浮点辅助运算（`pipeline/Classify.scala` 等） |
| `dependencies/fpuv2` | liuxd17thu/fpuv2（`chisel6` 分支） | 向量/标量浮点运算单元，对接 `pipeline/fpu_utils.scala` 的 `FPUexe` |
| `dependencies/Membox2.Scala` | liuxd17thu/Membox2.Scala | 仿真用的内存盒子模型（`top/MemBox.scala` 等外部内存模型相关） |

> 这些子模块**不属于 `ventus/src`**，因此 4.2 的统计不含它们；它们在 `build.sc` 里被声明为 ventus 模块的依赖（详见 u1-l2）。

#### 4.3.3 代码实践

1. **目标**：确认 5 个子模块确实只存在于 `dependencies/`，且仓库自身代码不重复它们。
2. **步骤**：执行 `git ls-files 'dependencies/'`，你会看到每个子模块只以「gitlink」（形如 `dependencies/rocket-chip` 单行）出现，而非成千上万个文件。
3. **观察**：输出仅 5 行，分别对应 5 个子模块。
4. **预期结果**：证明这些是 git 子模块引用，真实文件需 `make init` 才会出现在工作区。
5. 待本地验证：若未执行 `make init`，进入 `dependencies/rocket-chip` 目录应为空。

#### 4.3.4 小练习与答案

**练习**：为什么 `fpuv2` 在 `.gitmodules` 里指定了 `branch = chisel6`，而其它子模块没有？
**答案**：因为 Ventus 当前使用 Chisel 6.4.0（见 u1-l2），`fpuv2` 需要切换到其 `chisel6` 分支才能与该 Chisel 版本兼容；其它子模块的默认分支已满足要求，故不必显式指定。

---

### 4.4 从 GPGPU_top 看目录如何串成硬件

#### 4.4.1 概念说明

前面三节是「静态地图」。本节用一个动态视角收尾：跟着 `GPGPU_top.scala` 走一遍，看它怎样把 `cta/`、`pipeline/`、`L1Cache/`、`L2cache/`、`axi/`、`mmu/` 这些**不同目录里的模块**例化、连线，组装成一颗完整 GPU。掌握这一节，你就能在阅读任何一篇后续讲义时，立刻知道「这个模块在整体里处于什么位置」。

#### 4.4.2 核心流程：顶层组装顺序

`GPGPU_top` 的组装可以分为三步：

1. **建四件套**：`CTAinterface` + `SM_wrapper` 数组 + `Scheduler`(L2) 数组 + 三个集群互联模块（`SM2clusterArbiter`/`l2Distribute`/`cluster2L2Arbiter`，均定义在 `GPGPU_top.scala` 本文件内）。
2. **连 SM↔CTA、SM↔L2**：双重循环里把每个 SM 的 `CTAreq/CTArsp` 接到 CTA，把 `memReq/memRsp` 经集群仲裁接到 L2。
3. **按 `MMU_ENABLED` 分支**：开启 MMU 时额外例化 `mmu/` 目录的 `L2TLB`/`AsidLookup`/xbar，并把 TLB 请求与 L2 请求一起仲裁；关闭时直接 SM→L2。

#### 4.4.3 源码精读

**① 四件套的例化与默认规模。** 注意 `NSms`、`NL2Cache`、`NCluster` 这些规模最终都源自 `parameters.scala`：

[ventus/src/top/GPGPU_top.scala:150-169](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L150-L169) — `class GPGPU_top` 声明端口（`host_req/host_rsp/out_a/out_d`），并例化 CTA、SM_wrapper 集群、L2 Scheduler、集群互联。

**② SM↔L2 经三层互联。** 这段循环把「SM（cluster 内）→ SM2clusterArbiter → l2Distribute → cluster2L2Arbiter → L2」串起来，是目录间最密集的连线区：

[ventus/src/top/GPGPU_top.scala:172-198](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L172-L198) — 外层遍历 cluster、内层遍历 cluster 内的 SM，逐拍连接 `memReq/memRsp`。

**③ MMU 开关决定是否引入 `mmu/` 目录。** `MMU_ENABLED` 默认 `false`（`parameters.scala` 第 15 行），所以默认编译里 `mmu/` 目录的模块**不会**出现在数据通路上：

[ventus/src/top/GPGPU_top.scala:200-223](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L200-L223) — `MMU_ENABLED match` 的 `false` 分支直接把 `cluster2l2Arb` 接到 L2，不例化 TLB；`true` 分支才会引入 `mmu/` 的 `L2TLB` 等（见第 224 行起）。

> 这正解释了一个常见困惑：「为什么 `mmu/` 有 4 个文件，但默认仿真波形里看不到 TLB？」——因为它们被编译期开关关掉了。

#### 4.4.4 代码实践

1. **目标**：在 `GPGPU_top.scala` 中追踪「一个 SM 发出的 memReq 如何到达 L2」，并把途经的目录记下来。
2. **步骤**：
   - 打开 `ventus/src/top/GPGPU_top.scala`，定位第 178 行 `sm2clusterArb(i).memReqVecIn(j)`，确认它接的是 `sm_wrapper(...).memReq`（来自 `pipeline/`+`L1Cache/`）。
   - 顺第 186 行 `l2distribute(i).memReqIn` 接 `sm2clusterArb(i).memReqOut`。
   - 再看第 207 行 `cluster2l2Arb(i).memReqVecIn(j)` 接 `l2distribute(j).memReqVecOut(i)`。
   - 最后第 211 行 `io.out_a(i) <> l2cache(i).out_a` 出顶层。
3. **观察**：每经过一个 `<>` 或 `:=` 连线，模块的来源目录依次是 `L1Cache/`（arbiter）→ 本文件（集群互联）→ `L2cache/`（Scheduler）。
4. **预期结果**：画出一条 `SM_wrapper.memReq → SM2clusterArbiter → l2Distribute → cluster2L2Arbiter → Scheduler → out_a` 的链路，并标注每段对应的源码目录。
5. 待本地验证：可与 `docs/cta_scheduler/Top.md` 等文档里的拓扑图交叉核对。

#### 4.4.5 小练习与答案

**练习 1**：`SM2clusterArbiter`、`l2Distribute`、`cluster2L2Arbiter` 这三个集群互联模块定义在哪个目录？
**答案**：它们都定义在 `ventus/src/top/GPGPU_top.scala` 本文件内（见第 525、587、606 行的 `class` 声明），属于 `package top`，并不单独成目录——这是「顶层胶水逻辑就近放在顶层文件」的常见做法。

**练习 2**：如果我想让默认 RTL 支持虚拟内存（页表），最小改动是什么？
**答案**：把 `ventus/src/top/parameters.scala` 第 15 行的 `MMU_ENABLED` 从 `false` 改成 `true`。这会让 `GPGPU_top` 走 `MMU_ENABLED match` 的 `true` 分支，从而把 `mmu/` 目录的 `L2TLB`/`AsidLookup` 等模块纳入数据通路（第 224 行起）。

---

## 5. 综合实践

**任务：制作一份「Ventus 目录—职责—硬件位置」三列对照表。**

把本讲学到的内容串起来，完成下面这张表（示例已填两行，请补全）：

| 目录 | 职责（一句话） | 在 GPGPU_top 数据通路中的位置 |
| --- | --- | --- |
| `cta/` | 把 host 发来的 workgroup 调度到 SM | 最前端，`CTAinterface` 例化于第 164 行 |
| `pipeline/` | ？ | ？ |
| `L1Cache/ICache/` | ？ | ？ |
| `L1Cache/DCache/` | ？ | ？ |
| `L1Cache/ShareMem/` | ？ | ？ |
| `L2cache/` | ？ | ？ |
| `axi/` | ？ | ？ |
| `mmu/` | ？ | ？ |

操作建议：

1. 用 4.2.5 的命令得到各目录文件数，作为复杂度参考。
2. 打开 `ventus/src/top/GPGPU_top.scala`，用 4.4.4 的方法定位每个目录模块被例化的行号，填入第三列。
3. 对 `mmu/` 这一行，特别注明「默认 `MMU_ENABLED=false`，故默认通路中不出现」。

完成后，你应得到一张可以直接贴在显示器旁的「速查地图」——这正是后续阅读任何一篇源码讲义前的最佳准备。

## 6. 本讲小结

- Ventus 仓库顶层分为**源码（`ventus/`）、仿真（`sim-verilator/`）、依赖（`dependencies/`）、构建（`Makefile`/`build.sc`）、文档（`docs/`）** 五类，`make test` 已废弃，正式仿真走 `sim-verilator/`。
- 自研 RTL 全部位于 `ventus/src/`，按功能域分为 `top`、`pipeline`、`cta`、`L1Cache`、`L2cache`、`mmu`、`axi`、`SRAMTemplate`、`config` 九个子目录加一个根级 `GvmDutApi.scala`；其中 `pipeline/`（25 文件）最大、`config/`（1 文件）最小。
- **包名通常等于目录名，但有例外**：`GvmDutApi.scala` 是 `package gvm`、`cta/utils.scala` 是 `package cta.utils`，定位代码时以 `package` 声明为准。
- `dependencies/` 下 5 个 git 子模块（rocket-chip、inclusive-cache、hardfloat、fpuv2、Membox2.Scala）提供配置/浮点/缓存框架等基础能力，源自 rocket-chip、Sifive、Berkeley、香山等上游。
- `top/GPGPU_top.scala` 是「目录如何串成硬件」的枢纽：它的 `import` 列出了所有依赖目录，`Module(new ...)` 把 CTA、SM 集群、L2、集群互联例化并连线，且通过 `MMU_ENABLED`/`GVM_ENABLED` 等开关决定是否纳入 `mmu/`、`gvm` 目录的模块。
- 规模由 `top/parameters.scala` 的 `object parameters`（`num_sm=2`/`num_warp=8`/`num_thread=32`）集中决定，elaboration 入口是 `top/ExtMem_gen.scala` 的 `object GPGPU_gen`。

## 7. 下一步学习建议

有了这张目录地图，接下来可以：

- **进入 u2 单元**：先读 u2-l1（GPU 编程模型与 CTA/Warp/Thread 概念），建立软件视角；再读 u2-l2，深入 `top/GPGPU_top.scala` 的顶层互联细节——本讲只点到为止的 `SM2clusterArbiter`/`l2Distribute`/`cluster2L2Arbiter`，在那里会完整展开。
- **想先跑通仿真**：跳到 u1-l4（Verilator 仿真与测试用例），亲手在 `sim-verilator/` 跑一个 vecadd，再回头看目录会更有体感。
- **按目录逐块深读**：本讲提到的每个子目录，在后续都有对应讲义，例如 `cta/`→u3、`pipeline/`→u4/u5、`L1Cache/`+`L2cache/`→u6、`mmu/`+`axi/`→u7。建议顺着依赖链（`depends_on`）阅读，而非按目录顺序硬啃。
