# 仓库目录结构详解

## 1. 本讲目标

本讲承接 [u1-l1](u1-l1-project-overview.md) 建立的宏观认知，带你走进 NVDLA 仓库内部，建立一张清晰的「源码地图」。

学完后你应当能够：

- 说出顶层目录 `vmod/`、`spec/`、`tools/`、`verif/`、`cmod/`、`syn/`、`perf/` 各自的职责。
- 认识 `vmod/nvdla/` 下的 16 个功能子模块，并能按「配置总线 / 卷积核心 / 存储接口 / 后处理 / 时钟复位 / 顶层」分类。
- 区分 `vmod/` 下三类支撑目录：`vlibs/`（库单元）、`rams/`（存储模型）、`include/`（头文件）。
- 看懂 `tools/etc/build.config` 如何用一张依赖图把目录组织成可编译的 sandbox。

本讲只看目录与构建拓扑，不深入任何模块的内部实现——那是后续讲义的任务。

## 2. 前置知识

阅读本讲前，你需要了解以下概念（u1-l1 已引入，这里补充与本讲相关的部分）：

- **RTL（Register Transfer Level）**：用硬件描述语言（这里是 Verilog/SystemVerilog）写出的电路设计文本，是芯片功能的「源码」。
- **Testbench（测试平台）**：围绕 RTL 搭建的激励与检查环境，用于在仿真中验证 RTL 是否正确。
- **C-model（参考模型）**：用 C++/SystemC 写的、与 RTL 功能对应的软件模型，作为「黄金参考」与 RTL 仿真结果比对。
- **Sandbox（构建沙箱）**：NVDLA 构建系统里的一个编译单元，每个 sandbox 对应一个目录，有自己的 Makefile 和依赖列表。
- **SoC / IP**：NVDLA 是一个可集成的 IP 块，最终嵌入 SoC；本仓库提供的是这个 IP 的全部设计资产。

一个核心直觉：**目录划分 ≈ 模块划分 ≈ 构建单元划分**。NVDLA 把功能、构建、目录三者对齐——看懂目录树，就大致看懂了硬件的功能分解。

## 3. 本讲源码地图

下表列出本讲涉及的关键文件及其作用：

| 文件 / 目录 | 作用 |
| --- | --- |
| [README.md](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/README.md#L33-L47) | 项目说明，包含顶层目录结构定义 |
| [Makefile](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/Makefile#L1-L22) | 顶层 Makefile，交互式生成 `tree.make` 环境配置 |
| [vmod/README.md](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/README.md#L1-L1) | RTL 目录的简短说明 |
| [spec/manual/README.md](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/spec/manual/README.md#L1-L21) | 寄存器规格（SystemRDL）生成流程说明 |
| [verif/traces/README.md](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/traces/README.md#L1-L9) | 仿真 trace 运行方法与结果目录 |
| [tools/etc/build.config](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/tools/etc/build.config#L1-L165) | YAML 形式的 sandbox 依赖图，是理解目录组织的「总钥匙」 |

先给一张顶层目录的全景图（示意图，非项目源码）：

```
nvdla-hw/
├── README.md            # 项目说明
├── Makefile             # 顶层入口，生成 tree.make
├── LICENSE              # NVDLA Open Hardware License
├── VERSION              # NVDLA_OS_INITIAL
├── vmod/                # RTL 实现（本讲重点 4.1）
│   ├── nvdla/           # NVDLA 全部功能 Verilog（16 个功能子模块 + retiming）
│   ├── vlibs/           # 库单元（同步器、FPU、MUX 等）
│   ├── rams/            # RAM 模型（model 仿真 / synth 综合）
│   ├── include/         # 共享头文件 .vh
│   └── plugins/         # eperl 预处理插件
├── verif/               # 验证与仿真（本讲重点 4.2）
│   ├── sim/             # VCS 仿真入口 Makefile
│   ├── synth_tb/        # trace-player 测试平台
│   ├── traces/          # 样例 trace（sanity0-3 等）
│   ├── verilator/       # 开源 Verilator 仿真路径
│   ├── sim_vivado/      # Vivado 仿真路径
│   └── dut/             # DUT 文件列表
├── spec/                # 配置规格（本讲重点 4.3）
│   ├── defs/            # 特性宏 .spec（nv_full.spec 等）
│   └── manual/          # 寄存器 SystemRDL 与 Ordt 生成
├── tools/               # 构建工具链（本讲重点 4.4）
│   ├── bin/             # tmake / defgen / eperl / run_sanity 等脚本
│   ├── etc/             # build.config 依赖图
│   └── make/            # 通用 make 片段
├── cmod/                # C++/SystemC 参考模型
├── syn/                 # 综合脚本（SDC / DC Tcl / config 模板）
└── perf/                # 性能评估电子表格
```

下面四个最小模块分别深入 `vmod/`、`verif/`、`spec/`、`tools/`，并在最后用综合实践把整张地图串起来。

## 4. 核心概念与源码讲解

### 4.1 vmod 目录：RTL 实现的主体

#### 4.1.1 概念说明

`vmod/` 是整个仓库的核心——NVDLA 的硬件电路全部用 Verilog 写在这里。README 明确把它列为 RTL 模型目录，并进一步细分为三个子目录（见 [README.md:38-41](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/README.md#L38-L41)）：

- `vmod/nvdla/` —— NVDLA 的 Verilog 实现；
- `vmod/vlibs/` —— 库与单元模型（library and cell models）；
- `vmod/rams/` —— NVDLA 使用的 RAM 行为模型。

`vmod/nvdla/` 是最关键的部分，下面挂着 17 个子目录：16 个功能子模块 + 1 个 `retiming`（重定时/流水寄存器，属横切基础设施）。这 16 个功能子模块按职责可分为五类：

| 类别 | 子模块 | 中文名 |
| --- | --- | --- |
| 配置总线 / 全局 | `apb2csb`、`csb_master`、`glb` | APB 转 CSB 桥、CSB 中央路由器、全局配置与中断 |
| 卷积核心 | `cdma`、`cbuf`、`csc`、`cmac`、`cacc` | 卷积 DMA、卷积缓冲、卷积时隙控制器、乘加阵列、累加器 |
| 存储接口 | `nocif`、`bdma` | 存储接口（含 MCIF/CVIF）、桥 DMA |
| 后处理 | `sdp`、`pdp`、`cdp`、`rubik` | 单点处理器、平面处理器（池化）、通道处理器（LRN）、数据重排 |
| 时钟复位 / 顶层 | `car`、`top` | 时钟与复位、顶层 |

`vlibs/`、`rams/`、`include/` 三类是支撑目录，不是功能引擎，而是被各引擎复用的基础资源：

- **`vlibs/`（库单元）**：64 个文件，提供同步器（`sync3d.v`）、浮点单元（`HLS_fp17_add.v` 等）、MUX、缓冲单元（`NV_BLKBOX_BUFFER.v`）等可复用原语，保证全芯片风格一致。
- **`rams/`（存储模型）**：分 `model/`（35 项，仿真用行为级 RAM，如 `RAMDP_256X8_GL_M2_E2.v`）与 `synth/`（57 项，综合用 RAM wrapper，如 `nv_ram_rws_256x512.v`）两套，同一块缓冲在仿真与综合下替换为不同模型。
- **`include/`（头文件）**：共享的 `.vh` 定义，如 `NV_HWACC_NVDLA_tick_defines.vh`。

#### 4.1.2 核心流程

`vmod/` 的组织遵循「目录即模块、模块即构建单元」的原则。其内部依赖关系可概括为：

1. `vlibs/`、`rams/`、`include/` 是底层共享资源，被所有功能模块依赖。
2. 16 个功能子模块各自独立，只依赖底层共享资源，彼此之间通过顶层 `top/` 互联。
3. `top/`（即 `NV_nvdla.v` 所在目录）是集成点，它把所有子模块实例化并连线，形成完整的 NVDLA。

用伪代码描述这种分层：

```
顶层 NV_nvdla (top/)
 ├── 实例化 apb2csb, csb_master, glb     // 配置通路
 ├── 实例化 cdma→cbuf→csc→cmac→cacc      // 卷积主流水线
 ├── 实例化 nocif, bdma                   // 存储接口
 ├── 实例化 sdp, pdp, cdp, rubik          // 后处理
 └── 实例化 car                           // 时钟复位
其中每个实例都引用 vlibs/ 的原语与 rams/ 的存储模型
```

#### 4.1.3 源码精读

README 的目录结构定义直接给出了 `vmod/` 的三层划分（[README.md:38-41](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/README.md#L38-L41)）：

```
  * vmod/ -- RTL model, including:
    * vmod/nvdla/ -- Verilog implementation of NVDLA
    * vmod/vlibs/ -- library and cell models
    * vmod/rams/ -- behavioral models of RAMs used by NVDLA
```

`vmod/README.md` 只有一行说明（[vmod/README.md:1](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/README.md#L1-L1)）：`# Verilog Source Code`，表明该目录即 Verilog 源码根。

最有说服力的证据来自构建依赖图。`tools/etc/build.config` 把每个 `vmod/nvdla/*` 子目录注册为一个独立 sandbox，且底层三类支撑目录也被单独注册（[build.config:17-34](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/tools/etc/build.config#L17-L34)）：

```
vmod_vlibs:
  sandbox:
    - vmod/vlibs
  dependencies:
    - defs

vmod_rams:
  sandbox:
    - vmod/rams/model
    - vmod/rams/synth
  dependencies:
    - defs

vmod_include:
  sandbox:
    - vmod/include
  dependencies:
    - defs
```

而 `vmod_nvdla_top` 作为顶层集成 sandbox，显式依赖全部 16 个功能子模块加 `retiming`（[build.config:140-159](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/tools/etc/build.config#L140-L159)）：

```
vmod_nvdla_top:
  sandbox:
    - vmod/nvdla/top
  dependencies:
    - vmod_nvdla_apb2csb
    - vmod_nvdla_cdma
    - vmod_nvdla_cbuf
    - vmod_nvdla_csc
    - vmod_nvdla_cmac
    - vmod_nvdla_cacc
    - vmod_nvdla_sdp
    - vmod_nvdla_pdp
    - vmod_nvdla_cdp
    - vmod_nvdla_bdma
    - vmod_nvdla_rubik
    - vmod_nvdla_glb
    - vmod_nvdla_csb_master
    - vmod_nvdla_nocif
    - vmod_nvdla_retiming
    - vmod_nvdla_car
```

这段依赖列表同时印证了两件事：一是 `vmod/nvdla/` 下确有 16 个功能子模块（外加 `retiming`）；二是卷积核心（`cdma`/`cbuf`/`csc`/`cmac`/`cacc`）与后处理（`sdp`/`pdp`/`cdp`）都被 `top` 依赖，必须先于 `top` 编译。

#### 4.1.4 代码实践

**实践目标**：用 `ls` 亲自核对 `vmod/nvdla/` 的子目录，验证讲义中的 17 项划分。

**操作步骤**：

1. 在仓库根目录执行 `ls vmod/nvdla/`。
2. 数一数输出有多少行，并与本讲 4.1.1 的表格对照。
3. 再执行 `ls vmod/vlibs/ | wc -l`、`ls vmod/rams/model | wc -l`、`ls vmod/rams/synth | wc -l`，核对三类支撑目录的文件数。

**需要观察的现象**：

- `vmod/nvdla/` 输出 17 个目录名，其中 16 个是功能子模块，`retiming` 是基础设施。
- `vlibs/`、`rams/model`、`rams/synth` 各有数十个文件。

**预期结果**：输出与本讲给出的清单完全一致（`apb2csb bdma cacc car cbuf cdma cdp cmac csb_master csc glb nocif pdp retiming rubik sdp top`）。

**待本地验证**：文件数量可能随版本微调，以本地 `ls` 实际输出为准。

#### 4.1.5 小练习与答案

**练习 1**：`vmod/rams/` 为什么要有 `model/` 和 `synth/` 两套？

**参考答案**：`model/` 是仿真用的行为级 RAM，方便快速跑波形；`synth/` 是综合用的 RAM wrapper，可替换为真实工艺库。同一块缓冲（如 CBUF）在仿真与综合下挂不同的 RAM 模型，互不干扰。

**练习 2**：为什么 `vmod_nvdla_top` 要依赖全部 16 个子模块，而子模块之间互不直接依赖？

**参考答案**：子模块只通过顶层 `top/` 互联，彼此解耦便于独立编译与复用；`top/` 负责实例化并连线，因此必须等所有子模块就绪后才能编译，故 `top` 依赖全部子模块。

---

### 4.2 verif 目录：验证与仿真资产

#### 4.2.1 概念说明

`verif/` 存放验证 RTL 正确性所需的全部资产。README 把它描述为「trace-player testbench for basic sanity validation」（[README.md:44-45](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/README.md#L44-L45)）。它包含六个子目录：

| 子目录 | 作用 |
| --- | --- |
| `sim/` | VCS 仿真入口，含 `Makefile` 与结果校验脚本 `checktest.pl` |
| `synth_tb/` | trace-player 测试平台源码（`tb_top.v`、`memory.v`、`axi_slave.v` 等） |
| `traces/` | 样例 trace，含 `sanity0-3`、`sdp_relu_int16`、`pdp_max_pooling_int16` 等 12 项 |
| `verilator/` | 开源 Verilator 仿真路径（不依赖 VCS） |
| `sim_vivado/` | Vivado 仿真路径 |
| `dut/` | DUT 文件列表（`dut.f`、`dut.vivado.f`） |

核心概念是 **trace（激励序列）**：一条 trace 就是一串寄存器写事务，用来「编程」并启动 NVDLA 各引擎。trace-player 把 trace 喂给 DUT，跑完后比对结果。

#### 4.2.2 核心流程

一次典型的 sanity 仿真流程：

1. 在 `verif/sim/` 下用 `make` 编译 DUT 与 testbench（依赖 VCS 或 Verilator）。
2. 用 `make run TESTDIR=<path/to/test>` 指定一条 trace 运行。
3. trace-player（`csb_master_seq.v`）把 trace 解码成一串 CSB 写，驱动 DUT。
4. 仿真结束后，`checktest.pl` 校验结果，输出 PASSED/FAILED。
5. 结果落在 `verif/sim/_**test**_/` 目录下。

#### 4.2.3 源码精读

`verif/traces/README.md` 给出了运行方法与结果位置（[verif/traces/README.md:4-9](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/traces/README.md#L4-L9)）：

```
cd verif/sim 
make run TESTDIR=<path/to/test>
```
结果目录：`verif/sim/_**test**_`。

trace 样例可在 `verif/traces/traceplayer/` 下找到，包含 `sanity0`、`sanity1`、`sanity2`、`sanity3`（及对应的 `_cvsram` 变体）、`sdp_relu_int16`、`pdp_max_pooling_int16`、`googlenet_conv2_3x3_int16`、`cc_alexnet_conv5_relu5_int16_dtest_cvsram`、`conv_8x8_fc_int16` 等 12 项。从命名就能看出每条 trace 针对哪类操作（卷积、ReLU、池化、特定网络层）。

testbench 的核心源码在 `verif/synth_tb/`：`tb_top.v` 是顶层、`memory.v` 与 `axi_slave.v` 是存储模型、`csb_master.v`/`csb_master_seq.v` 把 trace 转成 CSB 激励、`zemi3_tb.sv` 是监控/检查器。

#### 4.2.4 代码实践

**实践目标**：浏览 trace 样例，建立「trace 名 → 验证目标」的直觉。

**操作步骤**：

1. 执行 `ls verif/traces/traceplayer/`。
2. 对每个 trace 名，猜它验证的是哪个引擎（例如 `sdp_relu_int16` → SDP 的 ReLU，`pdp_max_pooling_int16` → PDP 的 max pooling）。
3. 打开 `verif/sim/Makefile`，找到 `run` 目标与 `TESTDIR` 的用法。

**需要观察的现象**：trace 名包含引擎缩写（sdp/pdp/conv）与精度（int16），还可能出现网络名（alexnet/googlenet）。

**预期结果**：12 条 trace 覆盖卷积、后处理与真实网络层。

**待本地验证**：能否实际运行取决于本地是否装有 VCS/Verilator，无工具时本实践退化为源码阅读型。

#### 4.2.5 小练习与答案

**练习 1**：`sanity1` 与 `sanity1_cvsram` 有何区别？

**参考答案**：`sanity1` 用主存接口（MCIF→DBB），`sanity1_cvsram` 用片上 CVSRAM 二级接口，验证 CVIF 通路。两者激励相似但存储路径不同。

**练习 2**：`verif/verilator/` 与 `verif/sim/` 为何要分两套？

**参考答案**：`sim/` 走商用 VCS，`verilator/` 走开源 Verilator，为没有 VCS license 的用户提供可跑通的替代路径。

---

### 4.3 spec 目录：配置与寄存器规格

#### 4.3.1 概念说明

`spec/` 存放 NVDLA 的「配置规格」，分两个子目录：

- `spec/defs/`：特性宏定义，用 `.spec` 文件描述 NVDLA 的固定特性集（如 MAC 阵列规模）。
- `spec/manual/`：寄存器规格，用 SystemRDL（`test.rdl`）作为单一可信源，经 `Ordt.jar` 自动生成 RTL/RAL/cmod 寄存器模型。

README 把 `spec` 描述为「RTL configuration option settings」（[README.md:47](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/README.md#L47-L47)）。注意本仓库是 `nvdlav1` 固定全精度分支，spec 里的特性集是锁死的（[README.md:15-19](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/README.md#L15-L19)）：2048 个 8-bit MAC 或 1024 个 16-bit MAC。

#### 4.3.2 核心流程

`spec/` 的两个子目录分别驱动两类生成：

1. **defs 流程**：`nv_full.spec`（特性宏）经 `tools/bin/defgen` 展开为 RTL 使用的 `define`，控制条件编译。
2. **manual 流程**：`test.rdl`（SystemRDL）经 `Ordt.jar` 生成多种后端——Verilog 寄存器模型、RAL 类、cmod 寄存器、SystemVerilog 模型。

两者都是「单一源 → 多后端」的代码生成模式，避免手写寄存器带来的不一致。

#### 4.3.3 源码精读

`spec/manual/README.md` 说明了生成流程（[spec/manual/README.md:12-20](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/spec/manual/README.md#L12-L20)）：先确保装了 Java 1.7+，更新 `make/tools.mk` 里的 `JAVA` 变量，然后 `make` 即可生成：

```
make
```

生成的后端有四类：

- **regs_v.v**：Verilog 模型（即 RTL 里的 `_CSB_reg.v` 寄存器文件）；
- **regs_ral.sv**：RAL（Register Abstraction Layer）类，供验证用；
- **cmod**：C++ 模型寄存器；
- **sv**：SystemVerilog 模型。

`spec/defs/` 下有 `nv_full.spec`（默认特性集，对应 Makefile 里的 `DEFAULT_PROJ := nv_full`，见 [Makefile:19](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/Makefile#L19-L19)）、`projects.spec`（用户宏到内部宏的映射与校验）与 `Makefile`。

#### 4.3.4 代码实践

**实践目标**：核对 `spec` 两个子目录的文件，理解「源 → 生成物」关系。

**操作步骤**：

1. 执行 `ls spec/defs/` 与 `ls spec/manual/`。
2. 确认 `spec/manual/` 下有 `test.rdl`（源）、`Ordt.jar`（生成器）、`Makefile`（流程）、`test.parms`（参数）。
3. 阅读 `spec/manual/README.md`，确认四个生成后端的名字。

**需要观察的现象**：`spec/manual/` 同时存在源（`.rdl`）与生成器（`.jar`），但**生成产物不在仓库里**（在 `.gitignore` 忽略的输出目录中），需要本地 `make` 才会产生。

**预期结果**：看到 `test.rdl`、`Ordt.jar`、`Makefile`、`README.md`、`test.parms` 五项。

**待本地验证**：生成产物需本地执行 `make` 后才会出现。

#### 4.3.5 小练习与答案

**练习 1**：为什么寄存器要先用 SystemRDL 写，再生成多种后端，而不是直接手写 Verilog 寄存器？

**参考答案**：寄存器空间需要同时存在于 RTL、验证 RAL、C-model 三处。手写三份容易不一致；用 SystemRDL 作为单一可信源自动生成，保证三处完全对齐。

**练习 2**：`nv_full.spec` 与 Makefile 的 `DEFAULT_PROJ := nv_full` 是什么关系？

**参考答案**：`DEFAULT_PROJ` 指定默认项目名为 `nv_full`，构建系统据此找到 `spec/defs/nv_full.spec`，展开其中的特性宏驱动条件编译。

---

### 4.4 tools 目录：构建工具链

#### 4.4.1 概念说明

`tools/` 存放构建 RTL、跑仿真、跑综合所需的全部脚本与 make 片段。README 把它描述为「tools used for building the RTL and running simulation/synthesis/etc.」（[README.md:46](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/README.md#L46-L46)）。它分三个子目录：

| 子目录 | 内容 |
| --- | --- |
| `bin/` | 可执行脚本：`tmake`、`defgen`、`eperl`、`run_sanity`、`vcp`、`depth` |
| `etc/` | 配置：`build.config`（sandbox 依赖图） |
| `make/` | 通用 make 片段：`common.make`、`vmod_common.make`、`tools.mk`、`tree.make.vm` |

最关键的概念是 **tmake + build.config**：`tmake` 读取 `build.config` 里的 YAML 依赖拓扑，按依赖顺序驱动各 sandbox 的 make。README 给出的基本构建命令就是 `bin/tmake`（[README.md:55](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/README.md#L55-L55)）。

#### 4.4.2 核心流程

构建的完整链条：

1. 用户在仓库根目录执行 `make`（顶层 [Makefile:10](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/Makefile#L10-L10) 的 `default` 目标），交互式生成 `tree.make`（[Makefile:4](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/Makefile#L4-L4)），里面记录项目名与各工具路径（cpp/gcc/perl/java/systemc/verilator/clang）。
2. 用户执行 `bin/tmake`，它读 `tools/etc/build.config` 的依赖图。
3. `tmake` 按拓扑顺序（先 `defs`/`manual`/`vlibs`/`rams`/`include`，再各功能子模块，最后 `top`）逐个 sandbox 调用 make。
4. 每个 sandbox 内部用 `tools/make/vmod_common.make` 与 `common.make` 完成 eperl 预处理、defgen 展开、编译。

#### 4.4.3 源码精读

顶层 Makefile 的核心是生成 `tree.make`（[Makefile:21-22](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/Makefile#L21-L22)）：

```
$(TREE_MAKE): Makefile
	@echo "Creating tree.make to setup your working environment and projects"
```

它会交互式询问项目名（默认 `nv_full`）与各工具路径，写入 `tree.make`。注意 `tree.make` 被 `.gitignore` 忽略，是本地环境文件。

`tools/etc/build.config` 是理解整个目录组织的总钥匙。它的结构是「sandbox 名 → sandbox 路径 + 依赖列表」，例如（[build.config:10-15](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/tools/etc/build.config#L10-L15)）：

```
cmod_top:
  sandbox:
    - cmod
  dependencies:
    - defs
    - manual
```

这表示 `cmod` 这个 sandbox 依赖 `defs` 与 `manual`——正好对应 4.3 讲的 spec 生成物是 C-model 的输入。同样，`verilator` sandbox 依赖 `vmod_nvdla_top`（[build.config:161-165](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/tools/etc/build.config#L161-L165)），说明 Verilator 仿真必须等 RTL 全部编完。整张图把 `vmod/`、`spec/`、`cmod/`、`verif/` 四大资产串成一条依赖链。

#### 4.4.4 代码实践

**实践目标**：在 `build.config` 里追踪一条从 spec 到 verif 的完整依赖链。

**操作步骤**：

1. 打开 `tools/etc/build.config`。
2. 找到 `defs`（[build.config:2-4](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/tools/etc/build.config#L2-L4)）与 `manual`（[build.config:6-8](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/tools/etc/build.config#L6-L8)），确认它们是叶子节点（只产出，不依赖别的 sandbox）。
3. 找到 `vmod_nvdla_deps`（[build.config:36-41](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/tools/etc/build.config#L36-L41)），看它依赖 `manual`、`vmod_vlibs`、`vmod_include`、`vmod_rams`。
4. 找到 `vmod_nvdla_top`（[build.config:140-159](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/tools/etc/build.config#L140-L159)）与 `verilator`（[build.config:161-165](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/tools/etc/build.config#L161-L165)）。

**需要观察的现象**：依赖是分层的——`defs/manual` 在最底层，`vmod_nvdla_*` 在中层，`vmod_nvdla_top` 与 `verilator` 在上层。

**预期结果**：能画出 `defs → vmod_nvdla_deps → 各功能子模块 → vmod_nvdla_top → verilator` 的链路。

**待本地验证**：是否真正执行 `tmake` 取决于本地工具链是否齐全。

#### 4.4.5 小练习与答案

**练习 1**：`tmake` 与顶层 `make` 各做什么？

**参考答案**：顶层 `make` 只负责交互式生成 `tree.make`（环境配置）；`tmake` 才是真正的构建驱动，读 `build.config` 按依赖拓扑编译各 sandbox。

**练习 2**：为什么 `tree.make` 要被 `.gitignore` 忽略？

**参考答案**：`tree.make` 记录的是本地工具路径（cpp/gcc/perl/java 等），每台机器不同，不应提交到仓库共享。

---

## 5. 综合实践

把本讲四个模块串成一张完整的源码地图。请完成以下任务：

**任务**：对照本讲目录树，画出 `vmod/nvdla/` 的子模块树，为每个子模块标注中文名，并按颜色/分组标出「卷积核心」与「后处理」两类。

**操作步骤**：

1. 在一张纸或文本文件里画出 `vmod/nvdla/` 下 17 个子目录的树状图。
2. 为每个目录写中文名（参考 4.1.1 表格）。
3. 用记号标出卷积核心五件套：`cdma`（卷积 DMA）、`cbuf`（卷积缓冲）、`csc`（卷积时隙控制器）、`cmac`（乘加阵列）、`cacc`（累加器）。
4. 用另一种记号标出后处理四件套：`sdp`（单点处理器）、`pdp`（平面处理器/池化）、`cdp`（通道处理器/LRN）、`rubik`（数据重排）。
5. 打开 `tools/etc/build.config` 的 `vmod_nvdla_top` 段（[build.config:140-159](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/tools/etc/build.config#L140-L159)），核对你画的子模块清单与依赖列表是否一致。

**需要观察的现象**：

- 卷积核心五件套在依赖列表里连续出现（`cdma`/`cbuf`/`csc`/`cmac`/`cacc`），后处理三件套也连续（`sdp`/`pdp`/`cdp`）。
- `rubik` 单独列在后处理之后，`glb`/`csb_master`/`nocif`/`retiming`/`car` 是支撑性模块。

**预期结果**：得到一张标注完整的子模块树，能脱口而出「卷积核心 = cdma→cbuf→csc→cmac→cacc，后处理 = sdp/pdp/cdp/rubik」。

**待本地验证**：本实践为源码阅读型，无需运行即可完成；若想进一步验证目录数，可执行 `ls vmod/nvdla/ | wc -l`，预期为 17。

## 6. 本讲小结

- 顶层目录按资产类型划分：`vmod/`（RTL）、`verif/`（验证）、`spec/`（配置规格）、`tools/`（构建工具链）、`cmod/`（C 参考模型）、`syn/`（综合）、`perf/`（性能评估）。
- `vmod/nvdla/` 下有 16 个功能子模块 + `retiming`，按「配置总线 / 卷积核心 / 存储接口 / 后处理 / 时钟复位 / 顶层」五类组织。
- 卷积核心是 `cdma→cbuf→csc→cmac→cacc` 五件套；后处理是 `sdp`/`pdp`/`cdp`/`rubik` 四件套。
- `vmod/` 还有三类支撑目录：`vlibs/`（库原语）、`rams/`（分 model/synth 两套存储模型）、`include/`（共享头文件）。
- `verif/` 用 trace-player 把 trace 喂给 DUT，`sanity0-3` 等样例覆盖卷积与后处理。
- `tools/etc/build.config` 是理解目录组织的总钥匙：它把每个目录注册为 sandbox，并用依赖图把它们串成一条从 `defs/manual` 到 `vmod_nvdla_top` 再到 `verilator` 的构建链。

## 7. 下一步学习建议

掌握了目录地图后，建议按以下顺序继续：

- **u1-l3 构建系统与工具链**：深入 `tmake`、`build.config`、`defgen`、`eperl` 的协作，理解本讲 4.4 提到的构建链如何真正运转。
- **u1-l4 运行第一次仿真**：动手跑通一条 sanity trace，把 4.2 的 trace-player 流程跑出真实结果。
- **u1-l5 顶层 RTL**：进入 `vmod/nvdla/top/NV_nvdla.v`，看 16 个子模块如何在顶层被实例化与连线。

阅读源码时，随时回到 `tools/etc/build.config` 对照依赖关系——它是贯穿整个学习手册的导航图。
