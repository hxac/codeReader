# FPGA 构建与比特流生成

## 1. 本讲目标

前面三讲（u11-l1～u11-l3）讲的都是**仿真**：用 Verilator / VCS / cocotb 在 PC 上验证 RTL 是否正确。但 CoralNPU 终究是要烧进**真实 FPGA** 的——只有生成比特流（bitstream）、下载到芯片，才能在真实时序与真实外设上跑。

本讲聚焦 `fpga/` 目录，目标是让读者学完后能够：

1. 看懂 **fusesoc 的 `.core`（CAPI2）文件**如何用「依赖图 + 目标（target）+ 工具（tool）」描述一块硬件 IP，并理解 `coralnpu_soc.core` / `chip_verilator.core` / `coralnpu_soc_pkg.core` 各自的角色。
2. 厘清 CoralNPU 的 **FPGA 综合流程**：Bazel → fusesoc → Vivado（综合 → 优化 → 布局布线 → 生成比特流），以及它与 Verilator 仿真流程的分叉点。
3. 读懂 `get_bitstream.sh` 这个面向用户的脚本**到底做了什么**（一个重要的澄清：它并不是「跑综合」，而是「下载已构建好的比特流」）。
4. 理解 `ddr4_stub`、`coralnpu_tlul` 等 **FPGA 专用 IP / stub** 为什么存在、解决什么问题。

> 本讲是第 11 单元（验证流程与 FPGA 构建）的收口，承接 u11-l1 讲过的「Bazel + fusesoc 如何搭建 Verilator 仿真」，把同一套机制延伸到 Vivado 综合与比特流生成。

## 2. 前置知识

在进入源码前，先用通俗语言建立几个概念。本讲默认读者已学过 **u11-l1（Verilator 仿真流程）**，那里讲过的 Bazel、fusesoc、`template_rule` 在本讲会再次出现。

### 2.1 为什么需要 FPGA？仿真还不够吗？

仿真（Verilator/VCS）逐拍模拟 RTL 行为，能抓逻辑 bug，但有三个短板：

- **没有真实时序**：仿真不跑真实时钟树、不估频率、不报时序违例（setup/hold violation）。
- **没有真实外设**：DDR、SPI Flash、摄像头（ISP）这些在仿真里都是 DPI 模型，不是真实器件。
- **没有真实速度**：仿真速度是 KHz～MHz 级，跑不完一个真实 ML 推理。

FPGA 用真实逻辑单元（LUT/BRAM/DSP）把 RTL「实现」出来，能跑在真实几十 MHz 上、接真实外设，是从「RTL 正确」走向「能上芯片」的必经一步。

### 2.2 什么是比特流（bitstream）？

FPGA 不是 CPU——它没有固定指令集，内部是一张可编程的逻辑网格。告诉这片网格「每个节点怎么连、每个 LUT 算什么」的配置文件，就是**比特流**（一个 `.bit` / `.bin` 文件）。把比特流烧进 FPGA 的过程叫**配置（configure）**。

从 RTL 到比特流，标准 EDA 流程分三大步（Vivado 里就是三个 `*_design` 命令）：

| 阶段 | Vivado 命令 | 做什么 |
|------|------------|--------|
| 综合（Synthesis） | `synth_design` | 把 SystemVerilog 翻译成门级网表（LUT/FF/DSP/BRAM） |
| 实现（Implementation） | `opt_design` → `place_design` → `route_design` | 优化、布局（摆到芯片物理位置）、布线（连起来） |
| 生成比特流 | `write_bitstream` | 根据布局布线结果生成 `.bit` |

### 2.3 fusesoc 与 CAPI2：硬件世界的「包管理器」

写过软件的人都知道 `package.json` / `Cargo.toml` / `pyproject.toml`——声明「我叫什么、我依赖谁、我的源文件在哪、怎么构建」。**fusesoc** 就是硬件描述语言世界的包管理器，它的清单文件叫 **`.core` 文件**，格式规范叫 **CAPI2**（Core API version 2）。

一个 `.core` 文件回答四个问题：

1. **我是谁**：`name`（`厂商:库:名字:版本`，如 `com.google.coralnpu:fpga:coralnpu_soc:0.2`）。
2. **我依赖谁**：`filesets.rtl.depend` 列出其它 `.core` 的名字，fusesoc 会递归拉取、去重。
3. **我的源文件**：`filesets.rtl.files` 列出本 core 提供的 `.sv`/`.v`/`.xdc`/`.tcl`。
4. **怎么构建**：`targets` 段定义若干构建目标（如 `sim`、`synth`），每个目标指定工具（verilator / vivado）、参数、约束。

fusesoc 拿到一个顶层 core，就自动把整棵依赖图解析成「一组源文件 + 一组约束 + 一组参数」，再调用对应工具（Verilator 或 Vivado）去构建。OpenTitan、PULP 等开源芯片项目都用这套。

### 2.4 关键术语速查

- **CAPI2**：`.core` 文件的格式规范。
- **virtual core（虚 core）**：`.core` 里用 `virtual:` 声明的「占位依赖名」，可以被 `mapping:` 或更高层 core 重定向到真实实现。本讲的 `ddr4_stub` 就是这套机制的典型用法。
- **XDC**：Xilinx Design Constraints，Xilinx 的约束文件，主要用来做**引脚分配**（把某个信号绑到芯片某个物理脚）和时序约束。
- **TCL 钩子（hook）**：在 Vivado 流程的某个步骤前后插入的 TCL 脚本（如 `write_bitstream` 之后），用来做定制化处理（拼 memory map、改比特流格式等）。
- **MIG**：Xilinx 的 DDR4 Memory Interface Generator IP，负责和物理 DDR 颗粒打交道，是 Xilinx 专有、需 Vivado 生成的「黑盒」IP。

## 3. 本讲源码地图

| 文件 | 角色 |
|------|------|
| [fpga/coralnpu_soc.core](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/fpga/coralnpu_soc.core) | **SoC 包装层** core：把 CoralNPU 核（Chisel 子系统）+ 外设 + 总线胶水组装成 `coralnpu_soc`，同时声明综合参数与 Vivado synth 目标。 |
| [fpga/coralnpu_soc_pkg.core](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/fpga/coralnpu_soc_pkg.core) | **顶层常量包** core：提供 `top_pkg.sv`（SoC 范围内的参数常量），并以 virtual core 形式映射 `top_pkg`。 |
| [fpga/chip_verilator.core](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/fpga/chip_verilator.core) | **Verilator 仿真顶层** core：在 `coralnpu_soc` 外再套一层 `chip_verilator`，挂上 DPI 外设模型，服务于仿真（**不是** FPGA 综合）。 |
| [fpga/chip_nexus.core](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/fpga/chip_nexus.core) | **真正的 FPGA 综合顶层** core：`chip_nexus` 模块 + 引脚约束（XDC）+ Vivado 钩子（TCL），目标是 Vivado、生成比特流。 |
| [fpga/get_bitstream.sh](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/fpga/get_bitstream.sh) | **比特流下载脚本**：按 git 提交从 GCP Artifact Registry 拉取**预构建好**的比特流（注意：它本身不跑综合）。 |
| [fpga/BUILD](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/fpga/BUILD) | **Bazel 装配**：用 `fusesoc_build` 规则把 `.core` 文件变成 `bazel build //fpga:build_chip_nexus_bitstream` 等目标，是综合流程的真正入口。 |
| [fpga/ip/ddr4_stub/ddr4_stub.core](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/fpga/ip/ddr4_stub/ddr4_stub.core) | **DDR4 stub** core：用一段空壳 RTL 顶替 Xilinx MIG DDR4 IP，使公开版仓库也能综合。 |
| [fpga/ip/coralnpu_tlul/coralnpu_tlul.core](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/fpga/ip/coralnpu_tlul/coralnpu_tlul.core) | **CoralNPU TL-UL 参数包** core：提供 128 位 / 32 位两套 TileLink-UL 参数包。 |

> 说明：本讲规格里点名了 `coralnpu_soc.core`、`chip_verilator.core`、`coralnpu_soc_pkg.core`、`get_bitstream.sh`。但讲清楚「综合 + 引脚 + 比特流」必须额外引入 `chip_nexus.core`（真正的综合顶层）和 `fpga/BUILD`（综合流程入口），否则会与事实不符。下面会逐层展开。

## 4. 核心概念与源码讲解

### 4.1 先纠正一个误解：`get_bitstream.sh` 不跑综合

#### 4.1.1 概念说明

本讲的实践任务里有一句「阅读 `get_bitstream.sh` 梳理 FPGA 构建的综合/实现/生成比特流步骤」。如果直接打开这个脚本期待看到 `synth_design` / `vivado` 之类的命令，你会扑个空——**`get_bitstream.sh` 根本不做综合**。

它的真实职责是：在 Google Cloud 的 **Artifact Registry**（一个云制品仓库）里，按 git 提交号（SHA）查找并下载**已经由 CI 构建好**的比特流二进制，存到本地 `fpga/bitstreams/`。换言之，CoralNPU 的策略是「综合很贵（要 Vivado 许可证、要几十分钟），所以 CI 集中构建、产物入库，开发者用脚本拉取」——这和很多公司「不让人人在本地跑 FPGA 综合」的做法一致。

理解这一点非常重要：**真正的综合步骤不在 `get_bitstream.sh` 里，而在 `fpga/BUILD` + `chip_nexus.core` 里**。本讲会两条线都讲清楚。

#### 4.1.2 核心流程

`get_bitstream.sh` 的执行过程可以概括为：

```text
1. 解析参数（--latest / --ref REF / --limit N / --target NAME）
2. cd 到 git 仓库根目录
3. git log 找出最近 N 个「改动过 fpga/ 或 hdl/chisel/src/soc/」的提交 SHA
4. 对每个 SHA，按命名规则拼出 Artifact Registry 的下载 URL
5. curl 尝试下载；命中即存盘退出
6. 全部 miss 则报错，提示加大 --limit
```

它的关键设计是「**按代码改动定位制品**」：只有改动了 `fpga/`（约束/顶层）或 `hdl/chisel/src/soc/`（SoC 装配）的提交，才可能产生新比特流，所以只在这些提交里找。

#### 4.1.3 源码精读

脚本头部先设了严格模式与默认配置：

[fpga/get_bitstream.sh:16](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/fpga/get_bitstream.sh#L16) 开启 `set -ue -o pipefail`，任一命令失败或引用未定义变量即退出。

[fpga/get_bitstream.sh:18-22](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/fpga/get_bitstream.sh#L18-L22) 定义云仓库的四个坐标：项目 `cerebra-shodan-ci-public`、地区 `us`、仓库 `coralnpu-artifacts`、包 `coralnpu-bitstreams`，默认目标文件名 `chip_nexus.bin`。这些就是 CI 上传比特流时用的同一套命名。

[fpga/get_bitstream.sh:78](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/fpga/get_bitstream.sh#L78) 是「按改动定位」的核心——只列改动过 `fpga/` 或 `hdl/chisel/src/soc/` 的提交：

```bash
SHAS=$(git log "${START_REF}" -n "${LIMIT}" --format="%H" -- fpga/ hdl/chisel/src/soc/)
```

[fpga/get_bitstream.sh:90-94](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/fpga/get_bitstream.sh#L90-L94) 拼出 Artifact Registry 的下载 URL，注意它用冒号 `:` 作分隔符、并在 HTTP 里 URL-encode 成 `%3A`，文件名格式是 `包名:SHA:目标文件`：

```bash
URL+="/files/${AR_PACKAGE}%3A${SHA}%3A${TARGET_FILE}:download?alt=media"
```

[fpga/get_bitstream.sh:98](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/fpga/get_bitstream.sh#L98) 用 `curl --fail` 逐个尝试，命中即存到 `fpga/bitstreams/`（[L75](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/fpga/get_bitstream.sh#L75) 先 `mkdir -p`）并退出。

> 旁证：同目录还有 `nexus/load_bitstream.sh`，它才是「把比特流烧到板子」的脚本（用 `scp` + SSH 调板上的 `zturn` 工具 + MCU UART），但它同样不跑综合。所以 CoralNPU 仓库里**没有任何脚本在本地跑 Vivado 综合**——综合完全交给 Bazel 目标（下文 4.4）和 CI。

#### 4.1.4 代码实践

**实践目标**：亲手验证「`get_bitstream.sh` 是下载器，不是综合器」。

**操作步骤**：
1. 打开 [fpga/get_bitstream.sh](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/fpga/get_bitstream.sh)，用搜索功能查找 `vivado`、`synth`、`fusesoc` 三个关键字。
2. 再打开 [fpga/BUILD](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/fpga/BUILD)，查找 `fusesoc_build`、`vivado`、`chip_nexus`。

**需要观察的现象**：
- `get_bitstream.sh` 里**搜不到** `vivado`/`synth`/`fusesoc`，只能搜到 `curl`、`git log`、`Artifact Registry`。
- `fpga/BUILD` 里**能搜到** `fusesoc_build`、`target = "synth"`、`chip_nexus`。

**预期结果**：两个文件的职责泾渭分明——一个是「取比特流」，一个是「造比特流」。如果有人告诉你「跑 `get_bitstream.sh` 就能综合出比特流」，你现在能用源码反驳他。

**待本地验证**：若你本地装了 `gcloud` 且有访问权限，可执行 `bash fpga/get_bitstream.sh --latest` 观察它访问的网络地址；若没有权限，只做上面的静态阅读即可，不要假装已运行。

#### 4.1.5 小练习与答案

**练习 1**：`get_bitstream.sh` 默认要找的目标文件叫什么？为什么脚本只在「改动过 `fpga/` 或 `hdl/chisel/src/soc/`」的提交里找？

**参考答案**：默认找 `chip_nexus.bin`（[L22](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/fpga/get_bitstream.sh#L22)）。因为只有这两类改动才可能改变 FPGA 顶层或 SoC 装配，进而需要新比特流；纯软件（`sw/`）或测试改动不会影响比特流，CI 也不会为它们产新制品，所以在这些提交里找是徒劳。

**练习 2**：如果连续 10 个提交都没找到制品，脚本会建议你怎么做？

**参考答案**：加大 `--limit` 往更早的提交找，或用 `--ref <最后那个SHA>^ --target <文件名>` 从更早的位置继续搜（见 [L108-109](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/fpga/get_bitstream.sh#L108-L109)）。

---

### 4.2 `coralnpu_soc_pkg.core`：顶层常量包与「虚 core」机制

#### 4.2.1 概念说明

在看复杂的 `coralnpu_soc.core` 之前，先用最小的 `coralnpu_soc_pkg.core` 理解 `.core` 文件的基本骨架，以及一个重要机制——**virtual core（虚 core）**。

SoC 范围内有大量「全芯片共享」的常量（地址宽度、总线参数、sideband 信号类型等），CoralNPU 把它们集中放在 `rtl/top_pkg.sv` 这个 SystemVerilog package 里。`coralnpu_soc_pkg.core` 就是把这个 package 包装成一个可被依赖的 fusesoc core。

但 OpenTitan 的很多 IP 写法是 `import top_pkg::...`，它们依赖一个名为 `lowrisc:virtual_constants:top_pkg` 的「虚 core」。CoralNPU 用 `virtual:` + `mapping:` 把这个名字**重定向**到自己的 `coralnpu_soc_pkg`，这样 OpenTitan IP 无需修改就能直接用上 CoralNPU 自己的 `top_pkg`。

#### 4.2.2 核心流程

```text
coralnpu_soc_pkg.core
  ├─ name: com.google.coralnpu:fpga:coralnpu_soc_pkg:0.1   ← 我是谁
  ├─ virtual: lowrisc:virtual_constants:top_pkg            ← 我顶替了哪个虚依赖
  ├─ filesets.files: rtl/top_pkg.sv                        ← 我提供的源文件
  └─ targets.default: filesets=[rtl]                       ← 默认怎么构建
```

#### 4.2.3 源码精读

[fpga/coralnpu_soc_pkg.core:16-19](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/fpga/coralnpu_soc_pkg.core#L16-L19) 声明身份，并用 `virtual:` 把自己注册为 `top_pkg` 虚 core 的一个实现：

```yaml
name: "com.google.coralnpu:fpga:coralnpu_soc_pkg:0.1"
description: "Toplevel-wide constants for the CoralNPU SoC"
virtual:
  - lowrisc:virtual_constants:top_pkg
```

[fpga/coralnpu_soc_pkg.core:21-30](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/fpga/coralnpu_soc_pkg.core#L21-L30) 提供唯一的源文件 `rtl/top_pkg.sv`，并定义 `default` target 只含 `rtl` 文件集。这是最朴素的一种 core：纯提供源码、无工具配置。

> 这个 `virtual:` 机制是理解 4.5 节 `ddr4_stub` 的钥匙：stub 之所以能「无缝顶替」专有 IP，靠的正是同一套 virtual core 重定向。

#### 4.2.4 代码实践

**实践目标**：体会 virtual core 的「顶替」效果。

**操作步骤**：在仓库内用搜索工具查找字符串 `lowrisc:virtual_constants:top_pkg`（可用 Grep 在 `.core`/`.f` 文件里搜）。

**需要观察的现象**：会看到 `coralnpu_soc.core` 里也有一条 `mapping:`（[L34](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/fpga/coralnpu_soc.core#L34)）把 `top_racl_pkg` 重定向到 `racl_pkg`，与这里的 `top_pkg` 是同一套手法。

**预期结果**：CoralNPU 通过 `virtual:`/`mapping:` 让自己定义的 package 顶替掉 OpenTitan IP 默认引用的虚 package，从而把第三方 IP「无缝」接入自己的常量体系。

**待本地验证**：搜索结果取决于具体 IP 版本，若搜不到 `top_racl_pkg` 也属正常（版本差异）。

#### 4.2.5 小练习与答案

**练习 1**：`coralnpu_soc_pkg.core` 里 `virtual:` 段的作用是什么？删掉它会怎样？

**参考答案**：它声明本 core 是虚依赖 `lowrisc:virtual_constants:top_pkg` 的一个实现。删掉后，依赖该虚 core 的 OpenTitan IP 就找不到 `top_pkg` 的具体实现，fusesoc 解析依赖时会报「unresolved virtual core」错误。

---

### 4.3 `coralnpu_soc.core`：SoC 综合装配

#### 4.3.1 概念说明

`coralnpu_soc.core` 是 FPGA 流程里承上启下的核心。承上：它把 CoralNPU 的「灵魂」——Chisel 生成的 SoC 子系统（标量核 + 总线 + 外设，见 u3-l1 讲的 `CoralNPUChiselSubsystem`）——作为依赖拉进来；启下：它把这些组件用一段 SystemVerilog 胶水（`coralnpu_soc.sv`）组装成可综合的 `coralnpu_soc` 模块，并声明综合参数和 Vivado synth 目标。

它最重要的信息是 **`depend` 列表**——一张「CoralNPU SoC 到底由哪些 IP 拼成」的清单。这正是实践任务要求「标注用到了哪些外部 IP」的落点。

#### 4.3.2 核心流程

`coralnpu_soc.core` 把 SoC 的依赖组织成两个文件集（fileset）：

```text
filesets.rtl  （可综合硬件）
  ├─ depend: CoralNPU Chisel 子系统（虚 core）
  │          + coralnpu_soc_pkg / racl_pkg（常量）
  │          + lowrisc uart / rom / adapter_sram（OpenTitan 外设/原语）
  │          + coralnpu_tlul（CoralNPU TL-UL 参数包）
  │          + i2c_master / ispyocto（I2C / 摄像头 ISP）
  │          + tlul2ahblite（TL-UL→AHB-Lite 桥，给 ISP 用）
  └─ files:  coralnpu_soc.sv / clk_table.sv / autoboot.sv

filesets.sim_src （仅仿真）
  └─ files:  main.cc  + verilator memutil 依赖

targets:
  default → 仅 rtl
  sim     → rtl + sim_src，工具=verilator
  synth   → rtl，工具=vivado，part=xcvu13p-fhga2104-2-e
```

#### 4.3.3 源码精读

[fpga/coralnpu_soc.core:1-3](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/fpga/coralnpu_soc.core#L1-L3) 声明 core 身份：

```yaml
CAPI=2:
name: "com.google.coralnpu:fpga:coralnpu_soc:0.2"
description: "The CoralNPU SoC for FPGA."
```

[fpga/coralnpu_soc.core:5-23](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/fpga/coralnpu_soc.core#L5-L23) 是 `rtl` 文件集，`depend` 列出整棵 SoC 依赖，`files` 列出本 core 自己写的三段胶水。这张依赖表值得逐行标注：

| 依赖 | 提供什么 |
|------|---------|
| `coralnpuv2:virtual:coralnpu_chisel_subsystem`（[L8](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/fpga/coralnpu_soc.core#L8)） | **CoralNPU 核心**：Chisel 生成的 SoC 子系统（标量核 + RVV 向量后端 + 总线 fabric），是个虚 core，由 `coralnpu_chisel_subsystem_default/highmem` 顶替（见 4.5） |
| `coralnpu_soc_pkg`（[L9](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/fpga/coralnpu_soc.core#L9)） | 顶层常量包 `top_pkg` |
| `racl_pkg`（[L10](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/fpga/coralnpu_soc.core#L10)） | RACL（寄存器访问控制）参数包 |
| `lowrisc:ip:uart`（[L11](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/fpga/coralnpu_soc.core#L11)） | OpenTitan UART IP |
| `lowrisc:prim:rom_adv` / `prim_generic:rom`（[L12-13](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/fpga/coralnpu_soc.core#L12-L13)） | ROM 原语（放启动镜像） |
| `lowrisc:tlul:adapter_sram`（[L14](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/fpga/coralnpu_soc.core#L14)） | TL-UL 转 SRAM 适配器 |
| `coralnpuv2:ip:coralnpu_tlul`（[L15](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/fpga/coralnpu_soc.core#L15)） | **CoralNPU 自有 TL-UL 参数包**（128 位 / 32 位，见 4.5） |
| `coralnpuv2:ip:i2c_master`（[L16](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/fpga/coralnpu_soc.core#L16)） | I2C 主机控制器（配摄像头寄存器） |
| `vsi:ip:ispyocto`（[L17](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/fpga/coralnpu_soc.core#L17)） | 摄像头图像信号处理器（ISP）IP |
| `google:ip:tlul2ahblite`（[L18](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/fpga/coralnpu_soc.core#L18)） | TL-UL → AHB-Lite 桥（ISP 走 AHB 总线） |

[fpga/coralnpu_soc.core:19-23](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/fpga/coralnpu_soc.core#L19-L23) 是本 core 自带的胶水：`coralnpu_soc.sv`（实例化并连接上述所有 IP，对应模块 `coralnpu_soc`）、`clk_table.sv`（时钟频率表）、`autoboot.sv`（自主启动释放逻辑）。

[fpga/coralnpu_soc.core:36-86](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/fpga/coralnpu_soc.core#L36-L86) 声明综合可调参数。其中数值参数（`paramtype: vlogparam`）会变成 Verilog `parameter`，如 `ClockFrequencyMhz`（[L37-41](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/fpga/coralnpu_soc.core#L37-L41)，默认 50MHz）、`IspClockFrequencyMhz`（ISP 10MHz）、`SpimClockFrequencyMhz`（SPI 主机 100MHz）、`ItcmSizeKBytes`/`DtcmSizeKBytes`（TCM 大小）；而布尔参数（`paramtype: vlogdefine`）会变成 Verilog 宏定义，如 `FPGA_XILINX`（[L67-71](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/fpga/coralnpu_soc.core#L67-L71)，启用 Xilinx 专用原语）、`VLEN_128`/`ZVE32F_ON`（[L77-86](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/fpga/coralnpu_soc.core#L77-L86)，RVV 向量位宽 128、启用浮点向量 profile——这与 u7 系列讲过的「实际 RTL 按 VLEN_128 构建」一致）。

[fpga/coralnpu_soc.core:116-136](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/fpga/coralnpu_soc.core#L116-L136) 是 `synth` 目标——**这是本 core 自己声明的一个 Vivado 综合入口**：工具选 `vivado`、芯片型号 `xcvu13p-fhga2104-2-e`（Xilinx Virtex UltraScale+ VU13P），并在 `synth_design` 前挂一个 `vivado_pre_synth.tcl` 钩子（[L134-135](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/fpga/coralnpu_soc.core#L134-L135)）。注意：这个 `synth` 目标综合出来的是**孤立的 SoC**（没有 DDR、没有引脚约束），主要用于 SoC 级的可综合性检查；真正能上板的比特流由 `chip_nexus.core` 在更外层装配（见 4.4）。

> 待确认：`vivado_pre_synth.tcl` 在公开仓库内并不存在对应文件（只有 `vivado_pre_opt_hooks.tcl`），说明这个 SoC 级 `synth` 目标在生产中并非主路径，真正用到的钩子在 `chip_nexus.core`。

#### 4.3.4 代码实践

**实践目标**：把 `coralnpu_soc.core` 的依赖表读成一张「SoC 拼装图」。

**操作步骤**：
1. 打开 [fpga/coralnpu_soc.core:5-23](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/fpga/coralnpu_soc.core#L5-L23)。
2. 按「**CoralNPU 自有 IP** / **OpenTitan 第三方 IP** / **外设 IP**」三类给 `depend` 列表分组。
3. 对照 [fpga/rtl/coralnpu_soc.sv](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/fpga/rtl/coralnpu_soc.sv) 的端口（`spi_*`、`gpio_*`、`ISP_DVP_*`、`scl/sda` 等），把每个外部物理接口对应到上表里某个 IP。

**需要观察的现象**：`coralnpu_soc.sv` 的端口与 `depend` 列表一一对应——例如有 `ISP_DVP_*` 端口，正是因为依赖了 `ispyocto`；有 `scl/sda` 端口，正是因为依赖了 `i2c_master`。

**预期结果**：你能用一句话回答「CoralNPU SoC 用到了哪些外部 IP」——OpenTitan 的 uart/rom/adapter_sram、CoralNPU 自有的 coralnpu_tlul、外设类的 i2c_master/ispyocto，以及把它们粘起来的 tlul2ahblite 桥。

**待本地验证**：端口与 IP 的逐一对应用 `grep` 在 `coralnpu_soc.sv` 中确认，本讲不假定你已运行。

#### 4.3.5 小练习与答案

**练习 1**：`coralnpu_soc.core` 里 `ClockFrequencyMhz` 和 `FPGA_XILINX` 都是参数，但前者 `paramtype: vlogparam`、后者 `paramtype: vlogdefine`，这两者在综合时表现成什么？

**参考答案**：`vlogparam` 变成模块的 Verilog `parameter`（可用 `--ClockFrequencyMhz=100` 在命令行覆盖，影响 `coralnpu_soc.sv` 里时钟分频逻辑）；`vlogdefine` 变成 `` `define FPGA_XILINX `` 宏定义（RTL 里用 `` `ifdef FPGA_XILINX `` 选择 Xilinx 专用原语还是 generic 行为模型）。

**练习 2**：为什么 `coralnpu_soc.core` 的 `synth` 目标不能直接产出可上板的比特流？

**参考答案**：因为它只综合了 SoC 本体，**缺少**引脚约束（XDC）、DDR 控制器、时钟复位顶层、烧写相关的 TCL 钩子；这些都在更外层的 `chip_nexus.core` 里补齐。`coralnpu_soc.core` 的 `synth` 目标更适合做 SoC 级可综合性 / 资源估算。

---

### 4.4 `chip_nexus.core` 与 `chip_verilator.core`：两条顶层目标

#### 4.4.1 概念说明

CoralNPU 有**两个顶层 core**，分别服务两条截然不同的流程：

- **`chip_nexus.core` → FPGA 综合流程**：顶层模块 `chip_nexus`，工具 Vivado，带引脚约束和比特流钩子，**产出比特流**。「nexus」是目标 FPGA 板卡代号（Xilinx VU13P，对应 `get_bitstream.sh` 默认找的 `chip_nexus.bin`）。
- **`chip_verilator.core` → Verilator 仿真流程**：顶层模块 `chip_verilator`，工具 Verilator，挂的是 DPI 外设模型（uartdpi、spi_dpi_master、display_dpi 等），**产出仿真可执行文件**（u11-l1 详讲）。

二者都把 `coralnpu_soc` 作为内核依赖，差别只在外围：仿真版用 C++ DPI 模型模拟外设，FPGA 版用真实引脚 + DDR + 约束。这正是「同一份 SoC RTL，仿真/上板两套外壳」的标准做法。

#### 4.4.2 核心流程

FPGA 综合流程（真实步骤藏在这里，而非 `get_bitstream.sh`）：

```text
bazel build //fpga:build_chip_nexus_bitstream
   │
   ▼  fusesoc_build 规则（fpga/BUILD）调起 fusesoc
fusesoc run --target=synth com.google.coralnpu:fpga:chip_nexus:0.1
   │
   ├─ 解析 .core 依赖图：chip_nexus → coralnpu_soc → chisel 子系统 + 外设 + coralnpu_tlul ...
   ├─ 注入约束：pins_nexus.xdc（引脚分配）
   ├─ 注入 TCL 钩子：vivado_setup_hooks / vivado_pre_opt_hooks / check_pin_assignments / pblock_u_isp / write_bitstream_post
   ├─ DDR：用 ddr4_stub（公开版）或 internal 真实 DDR4 IP（内部版）
   ▼
Vivado:  synth_design → opt_design → place_design → route_design → write_bitstream
   │
   ▼  产物：com.google.coralnpu_fpga_chip_nexus_0.1/.../*.bit
CI 上传 chip_nexus.bin 到 Artifact Registry（按 SHA 命名）
   │
   ▼  开发者用 get_bitstream.sh 拉取（回到 4.1）
```

#### 4.4.3 源码精读

[fpga/chip_nexus.core:15-16](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/fpga/chip_nexus.core#L15-L16) 声明这是「Nexus 专用顶层」。

[fpga/chip_nexus.core:20-33](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/fpga/chip_nexus.core#L20-L33) 是三个文件集，体现 FPGA 顶层的三类素材：

- `files_rtl`（[L19-28](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/fpga/chip_nexus.core#L19-L28)）：依赖 `coralnpu_soc`、`pulp-platform:riscv-dbg`（RISC-V 调试模块，对应 u9-l1）和 `xilinx:virtual:ddr4_0`（**DDR4 虚 core**——这就是 ddr4_stub 顶替的对象），并提供 `chip_nexus.sv` + 时钟生成器 `clkgen_*.sv`。
- `files_constraints`（[L30-33](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/fpga/chip_nexus.core#L30-L33)）：`pins_nexus.xdc`——**引脚约束文件**，把 `chip_nexus` 的每个端口（clk、uart、i2c、gpio、DDR 等）绑到 VU13P 的物理引脚。这是「引脚分配」的真正所在。
- `files_tcl`（[L35-42](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/fpga/chip_nexus.core#L35-L42)）：五个 Vivado 钩子脚本，`copyto` 指明会被拷到 Vivado 工程目录里执行。其中 `vivado_hook_write_bitstream_post.tcl` 在 `write_bitstream` 之后跑（常用来拼 memory map / 生成 `.bin`/`.mmi`），`check_pin_assignments.tcl` 校验引脚分配合法，`pblock_u_isp.tcl` 给 ISP 划定物理布局区域（P-block）。

[fpga/chip_nexus.core:116-136](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/fpga/chip_nexus.core#L116-L136) 是 `synth` 目标：`toplevel: chip_nexus`、`default_tool: vivado`、`part: xcvu13p-fhga2104-2-e`，并打开全套 `USE_GENERIC/FPGA_XILINX/TB_SUPPORT/VLEN_128/ZVE32F_ON` 宏。**这才是真正生成比特流的综合目标。**

对照之下，[fpga/chip_verilator.core:15-16](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/fpga/chip_verilator.core#L15-L16) 的 `chip_verilator` 是仿真顶层：

[fpga/chip_verilator.core:20-42](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/fpga/chip_verilator.core#L20-L42) 显示它依赖 `coralnpu_soc` 之后，挂的全是 **DPI 模型**——`uartdpi`、`jtagdpi`、`spi_dpi_master`、`display_dpi`、`gpio_dpi`、`s25fl512s_dpi`（SPI Flash 模型）、`hm01b0_model`（摄像头模型）、`sram_backdoor`（u2/u11 讲过的 DPI 后门加载）。这些在 FPGA 上根本不存在——FPGA 用真实器件，仿真用 C++ DPI 模型。

[fpga/chip_verilator.core:90-115](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/fpga/chip_verilator.core#L90-L115) 的 `sim` 目标用 `verilator`，没有 XDC、没有 Vivado、没有 part——因为它不出比特流，只出 `Vchip_verilator` 仿真可执行文件。

> 把两个 core 并排看，就能理解 CoralNPU「仿真优先」的开发哲学：同一个 `coralnpu_soc` 内核，平时在 `chip_verilator` 上高速验证；验证通过后，CI 才在 `chip_nexus` 上跑昂贵的 Vivado 综合产比特流。

#### 4.4.4 代码实践

**实践目标**：把「综合/实现/生成比特流」三个步骤在源码里逐一找出来。

**操作步骤**：
1. 在 [fpga/chip_nexus.core](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/fpga/chip_nexus.core) 中定位：综合目标（`synth`）、引脚约束文件（`pins_nexus.xdc`）、比特流后处理钩子（`vivado_hook_write_bitstream_post.tcl`）、芯片型号（`part`）。
2. 在 [fpga/BUILD:587-596](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/fpga/BUILD#L587-L596) 中找到把 `chip_nexus.core` 接进 Bazel 的 `template_rule(fusesoc_build, ...)`，注意 `target = "synth"`、`tags = ["manual"]`。

**需要观察的现象**：
- 综合目标里 `default_tool: vivado`、`part: xcvu13p-fhga2104-2-e`。
- `files_tcl` 里有专门处理「写比特流之后」的钩子（`vivado_hook_write_bitstream_post.tcl`）。
- Bazel 目标带 `tags = ["manual"]`，意味着**普通 `bazel build //fpga/...` 不会触发它**——需要显式 `bazel build //fpga:build_chip_nexus_bitstream`，这也呼应了「综合很贵、不轻易跑」。

**预期结果**：你能用源码画出一张从 `bazel build //fpga:build_chip_nexus_bitstream` 到 `.bit` 文件的完整链路，并指出每一步在哪个文件里定义。

**待本地验证**：`bazel build //fpga:build_chip_nexus_bitstream` 需要 Vivado 许可证与 fusesoc 环境，公开 CI 之外通常无法本地跑通，故仅做静态阅读。

#### 4.4.5 小练习与答案

**练习 1**：`chip_nexus.core` 和 `chip_verilator.core` 都依赖 `coralnpu_soc`，为什么后者不需要 XDC、前者必须要有？

**参考答案**：`chip_verilator` 是仿真，信号不对应物理引脚（外设都是 C++ DPI 模型），所以无需引脚约束；`chip_nexus` 要烧进真实 FPGA，每个端口必须绑定到芯片真实物理引脚，否则 Vivado 布局布线无法进行，因此必须有 `pins_nexus.xdc`。

**练习 2**：在 `fpga/BUILD` 里 `build_chip_nexus_*` 目标带 `tags = ["manual"]`（[L594](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/fpga/BUILD#L594)），这样设计的好处是什么？

**参考答案**：`manual` 标签使该目标不会被通配符或 `bazel build //...` 默认选中，避免开发者在没有 Vivado 许可证的环境下意外触发耗时几十分钟的综合；只有显式点名才会跑，通常由 CI 在专用机器上调用。

---

### 4.5 FPGA 专用 IP 与 stub：`ddr4_stub` 与 `coralnpu_tlul`

#### 4.5.1 概念说明

实践任务问：「为什么 FPGA 流程需要 stub？」这是本讲最有价值的一个设计点。答案分两层：

1. **专有 IP 不可开源**：DDR4 控制器（Xilinx MIG）是 Xilinx 专有 IP，需要 Vivado 在线生成、绑定许可证，不能直接放进开源仓库。但 RTL 顶层又必须**引用**它（`chip_nexus` 要接 DDR）。怎么办？用一个**接口完全相同、内部全空**的 stub 顶替——这就是 `ddr4_stub`。
2. **内部版 vs 公开版**：CoralNPU 团队内部有一份真实 DDR4 IP（在私有 `internal/` 目录），公开仓库拿不到。于是 `fpga/BUILD` 用一个开关 `internal_exists` 在两者间切换：有 `internal/` 就用真实 DDR4，没有就用 `ddr4_stub`。stub 保证**公开版仓库也能综合通过**（哪怕 DDR 实际不工作）。

至于 `coralnpu_tlul`，它不是 stub，而是 CoralNPU **自有**的 TL-UL（TileLink-UL）参数包，提供 128 位和 32 位两套总线参数定义（对应 u3 系列讲过的 SoC 内部 128 位、外设 32 位 TL-UL 通道）。

#### 4.5.2 核心流程

DDR4 stub 的「顶替」靠 virtual core：

```text
chip_nexus.core  ──depend──▶  xilinx:virtual:ddr4_0   （一个虚依赖名）
                                       ▲
                                       │ virtual: 实现
                              ddr4_stub.core  （公开版，空壳 RTL）
                                       ▲
                                       │ 或
                              internal 真实 ddr4.core （内部版，Xilinx MIG）

fpga/BUILD 用 internal_exists 二选一：
  internal_exists = True  → DDR_CORES = 真实 ddr4 + ddr4_phy
  internal_exists = False → DDR_CORES = ddr4_stub
```

#### 4.5.3 源码精读

[fpga/ip/ddr4_stub/ddr4_stub.core:16-19](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/fpga/ip/ddr4_stub/ddr4_stub.core#L16-L19) 声明 stub 身份，并用 `virtual:` 注册为 `xilinx:virtual:ddr4_0` 的实现——这正是 `chip_nexus.core` 依赖的那个名字：

```yaml
name: "coralnpuv2:ip:ddr4_stub:0.1"
description: "DDR4 Subsystem stub"
virtual:
  - xilinx:virtual:ddr4_0
```

[fpga/ip/ddr4_stub/rtl/ddr4_stub.sv:1](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/fpga/ip/ddr4_stub/rtl/ddr4_stub.sv#L1) 的模块名是 `ddr_system_bd_ddr4_0_0`——这是 Xilinx MIG 自动生成的 DDR4 模块的**标准名字**。stub 故意用同名同端口，才能「以假乱真」地被顶层实例化。

[fpga/ip/ddr4_stub/rtl/ddr4_stub.sv:98-108](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/fpga/ip/ddr4_stub/rtl/ddr4_stub.sv#L98-L108) 是 stub 的全部「实现」——把所有 AXI 应答信号恒置 `1'b0`，`c0_init_calib_complete = sys_rst`：

```verilog
assign c0_init_calib_complete = sys_rst;
assign c0_ddr4_s_axi_awready = 1'b0;
assign c0_ddr4_s_axi_wready  = 1'b0;
assign c0_ddr4_s_axi_bvalid  = 1'b0;
assign c0_ddr4_s_axi_rvalid  = 1'b0;
// ... 其余 ready/valid 全部拉 0
```

效果是：DDR4 AXI 接口永远不握手（ready 恒 0），任何对 DDR 的访问都会挂起。这显然不能让 DDR 真正工作，但足以让设计**综合通过**、让顶层引脚（DDR 物理脚）在 FPGA 上有归属。这就是 stub 的全部意义——**保综合、不保功能**。

[fpga/BUILD:531-544](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/fpga/BUILD#L531-L544) 是 `internal_exists` 二选一的代码，清楚地展示了「真实 IP 在内部、公开版用 stub」的策略：

```python
DDR_CORES = [
    "//internal/fpga/ip/ddr4:ddr4.core",
    "//internal/fpga/ip/ddr4_phy:ddr4_phy.core",
] if internal_exists else [
    "//fpga/ip/ddr4_stub:ddr4_stub.core",
]
```

`internal_exists` 来自 Bazel 的 `@internal_check//:status.bzl`（见 [fpga/BUILD:18](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/fpga/BUILD#L18)），它在 workspace 拉取阶段探测 `internal/` 目录是否存在。

而 `coralnpu_tlul` 是另一回事——它是真实功能代码。 [fpga/ip/coralnpu_tlul/coralnpu_tlul.core:2-12](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/fpga/ip/coralnpu_tlul/coralnpu_tlul.core#L2-L12) 提供两个 SystemVerilog package：`coralnpu_tlul_pkg_128.sv`（128 位 TL-UL 参数）和 `coralnpu_tlul_pkg_32.sv`（32 位 TL-UL 参数），供 SoC 内部宽通道（128 位）与外设窄通道（32 位）复用同一套 TL-UL 定义（对应 u3-3、u3-4 讲过的 TL-UL 总线）。

> 还有一类「虚 core」是 Chisel 子系统本身：[fpga/ip/coralnpu_chisel_subsystem_default/coralnpu_chisel_subsystem_default.core.tpl:16-29](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/fpga/ip/coralnpu_chisel_subsystem_default/coralnpu_chisel_subsystem_default.core.tpl#L16-L29) 用 `virtual: coralnpuv2:virtual:coralnpu_chisel_subsystem` 顶替 `coralnpu_soc.core` 引用的核心，文件里的 `__VERILOG_FILE__` 占位符在 Bazel 构建时被替换成 Chisel→firtool 生成的真实 SystemVerilog。它有 `default`（8KB ITCM/32KB DTCM）和 `highmem`（各 1MB）两个变体（见 [fpga/BUILD:404-427](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/fpga/BUILD#L404-L427) 的 `_MEM_CONFIGS`）。

#### 4.5.4 代码实践

**实践目标**：亲手验证「stub 顶替专有 IP」的完整链路，并解释为何需要 stub。

**操作步骤**：
1. 在 [fpga/chip_nexus.core:20-28](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/fpga/chip_nexus.core#L20-L28) 找到对 `xilinx:virtual:ddr4_0` 的依赖。
2. 在 [fpga/ip/ddr4_stub/ddr4_stub.core:16-19](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/fpga/ip/ddr4_stub/ddr4_stub.core#L16-L19) 确认 stub 用 `virtual:` 注册为它的实现。
3. 在 [fpga/BUILD:531-536](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/fpga/BUILD#L531-L536) 看清 `internal_exists` 如何在真实 DDR4 与 stub 间切换。
4. 在 [fpga/ip/ddr4_stub/rtl/ddr4_stub.sv:98-108](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/fpga/ip/ddr4_stub/rtl/ddr4_stub.sv#L98-L108) 体会 stub「接口齐全、实现全空」的特点。

**需要观察的现象**：依赖名 `xilinx:virtual:ddr4_0` 在 `chip_nexus.core`（消费者）和 `ddr4_stub.core`（提供者）两侧都出现，靠 virtual 机制对接；stub 的模块名与 Xilinx MIG 一致、端口齐全，但内部只把信号拉 0。

**预期结果**：你能回答「为何 FPGA 流程需要 stub」——因为专有 DDR4 IP（Xilinx MIG）不开源、需许可证，公开仓库用同名同端口的空壳 stub 顶替，保证综合能跑通；内部版用真实 IP 才能真正访问 DDR。

**待本地验证**：可在公开仓库执行 `git ls-files internal/` 确认 `internal/` 不存在（故 `internal_exists=False`，走 stub 路径）。

#### 4.5.5 小练习与答案

**练习 1**：`ddr4_stub` 的模块名为什么故意叫 `ddr_system_bd_ddr4_0_0`、端口为什么和真实 Xilinx DDR4 IP 一模一样？

**参考答案**：因为顶层 `chip_nexus` 是按真实 Xilinx MIG 的模块名和端口去实例化的；stub 必须同名同端口才能在不改顶层代码的前提下「以假乱真」地顶替，这正是 virtual core + stub 模式的精髓——替换发生在依赖解析层，RTL 实例化代码完全不变。

**练习 2**：用 `ddr4_stub` 综合出的比特流，DDR 能正常工作吗？为什么仍然要保留它？

**参考答案**：不能——stub 把所有 AXI ready 拉 0，对 DDR 的访问会永久挂起。保留它是为了让**公开版仓库在没有专有 DDR4 IP / 许可证的情况下也能综合通过**，用于流程验证、资源评估、引脚检查；真实 DDR 功能只在用 `internal/` 真实 IP 的内部版构建里才具备。

**练习 3**：`coralnpu_tlul` 提供的两个 package 文件名（`_128` / `_32`）暗示了什么？

**参考答案**：CoralNPU 的 TL-UL 总线分两个宽度——128 位用于 SoC 内部宽通道（核与 SRAM/DDR 之间）、32 位用于外设窄通道，两套参数各自一个 package，由 `coralnpu_tlul` 这个 core 统一提供。这与 u3-3/u3-4 讲的「内 128 位、外设窄」一致。

---

## 5. 综合实践

**任务**：画出 CoralNPU 从「一行 Bazel 命令」到「开发者拿到比特流」的完整端到端流程图，并标注每个环节发生在哪个文件、用到了哪些 stub/IP。

**建议步骤**：

1. **综合入口**：从 [fpga/BUILD:566-596](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/fpga/BUILD#L566-L596) 的 `_NEXUS_NAME_MAP` 与 `template_rule(fusesoc_build, ...)` 出发，写明 `bazel build //fpga:build_chip_nexus_bitstream` 会用哪个 `system`、哪个 `target`、哪组 `flags`（含内存变体 default/highmem、启动模式 itcm/rom，参考 [fpga/BUILD:404-441](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/fpga/BUILD#L404-L441)）。
2. **依赖解析**：从 [chip_nexus.core](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/fpga/chip_nexus.core) → [coralnpu_soc.core](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/fpga/coralnpu_soc.core) → Chisel 子系统虚 core，画出 fusesoc 解析的依赖树，标出每个节点是「CoralNPU 自有 IP / OpenTitan IP / 外设 IP / stub」。
3. **stub 切换**：在图上标出 [fpga/BUILD:531-544](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/fpga/BUILD#L531-L544) 的 `internal_exists` 分支——公开版走 `ddr4_stub`，内部版走真实 DDR4。
4. **Vivado 三步**：在 [chip_nexus.core:116-136](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/fpga/chip_nexus.core#L116-L136) 标出综合（synth_design，含 `pins_nexus.xdc` 引脚约束与各 TCL 钩子）、实现（opt/place/route）、生成比特流（write_bitstream + `vivado_hook_write_bitstream_post.tcl`）。
5. **产物入库与分发**：把 [get_bitstream.sh](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/fpga/get_bitstream.sh) 接在流程末尾——CI 把 `chip_nexus.bin` 按 SHA 上传到 Artifact Registry，开发者用脚本拉取，最后用 `nexus/load_bitstream.sh` 烧板。

**验收标准**：图上至少出现这些节点：`bazel`、`fusesoc`、`chip_nexus.core`、`coralnpu_soc.core`、`coralnpu_chisel_subsystem`（虚 core）、`coralnpu_tlul`、`ddr4_stub`（或真实 DDR4）、`pins_nexus.xdc`、`vivado`、`synth_design`、`write_bitstream`、`Artifact Registry`、`get_bitstream.sh`，并能在每个节点旁写一句话说明它的作用。

## 6. 本讲小结

- **`get_bitstream.sh` 是下载器，不是综合器**：它按 git SHA 从 GCP Artifact Registry 拉取 CI 预构建的 `chip_nexus.bin`，本身不含任何 Vivado/fusesoc 命令。
- **真正的综合流程在 Bazel 里**：`fpga/BUILD` 用 `fusesoc_build` 规则把 `.core` 文件接成 `bazel build //fpga:build_chip_nexus_bitstream`，由 fusesoc 解析依赖、调 Vivado 完成「综合 → 实现 → 生成比特流」。
- **`.core`（CAPI2）是硬件包清单**：`coralnpu_soc.core` 用 `depend` 列表声明 SoC 拼装图（Chisel 子系统 + OpenTitan uart/rom + coralnpu_tlul + i2c_master + ispyocto + tlul2ahblite），用 `targets.synth` 声明 Vivado 综合入口。
- **两个顶层、两条流程**：`chip_nexus.core` 走 Vivado 出比特流（带 XDC 引脚约束 + TCL 钩子），`chip_verilator.core` 走 Verilator 出仿真可执行文件（挂 DPI 外设模型）；二者共享同一个 `coralnpu_soc` 内核。
- **stub 解决「专有 IP 不开源」**：`ddr4_stub` 用同名同端口的空壳 RTL（ready 全拉 0）顶替 Xilinx MIG DDR4，靠 virtual core 机制无缝替换；公开版用 stub、内部版用真实 IP，由 `internal_exists` 开关切换。
- **设计哲学**：CoralNPU「仿真优先、综合集中」——平时在 `chip_verilator` 高速验证，验证通过后由 CI 在 `chip_nexus` 上跑昂贵综合，开发者只下载产物，最大化节省许可证与算力。

## 7. 下一步学习建议

本讲把 CoralNPU 的「上板」流程讲完了。建议接下来：

1. **往硬件深处**：阅读 `fpga/rtl/chip_nexus.sv` 与 `fpga/rtl/coralnpu_soc.sv` 全文，看顶层如何实例化时钟生成器（`clkgen_xilultrascaleplus.sv`）、如何把 `coralnpu_soc` 的端口接到 FPGA 物理引脚，对照 `pins_nexus.xdc` 理解引脚分配的写法。
2. **往工具链深处**：研究 `fpga/BUILD` 里 `template_rule(fusesoc_build, ...)` 如何用一份模板批量生成「内存变体 × 启动模式 × 综合类型」的笛卡尔积目标（这是 u11-l1 讲过的 `template_rule` 在 FPGA 场景的应用）。
3. **往验证闭环**：回到 u11-l2/u11-l3，理解同一份 RTL 如何在 Verilator/VCS/cocotb 上做仿真回归，再在本讲的 FPGA 流程上做真实综合——两者共同构成「RTL → 仿真 → 上板」的完整验证闭环。
4. **往软件深处**：阅读 `fpga/sw/` 下的 `flash_tool`、`rom_boot` 等程序，理解比特流烧进 FPGA 后，CoralNPU 如何从 SPI Flash 启动（`BootAddr=0x10000000` 的 ROM 启动路径，见 `chip_verilator.core` 的 `BootAddr` 参数注释）。
