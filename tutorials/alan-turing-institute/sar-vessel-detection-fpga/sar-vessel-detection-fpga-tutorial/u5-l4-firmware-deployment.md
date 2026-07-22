# 固件制作与板载部署

## 1. 本讲目标

本讲是「FPGA 硬件平台构建」单元的收尾篇，承接 u5-l3（PetaLinux 镜像构建）。学完本讲后，读者应该能够：

- 说清 KV260「加速器固件三件套」（`.bit.bin`、`.dtbo`、`shell.json`）各自的含义与作用，以及它们与 SD 卡启动镜像（`BOOT.BIN`/`image.ub`/rootfs）的区别。
- 复述设备树 overlay（`.dtbo`）从 `.xsa` 到二进制 `.dtbo` 的生成流程（XSCT `createdts` → `dtc`），并能看懂其中 DPU 节点的关键字段。
- 写出在 KV260 上部署固件的完整命令序列（`scp` → 建目录 → `mv` → `xmutil unloadapp/loadapp`）。
- 解读 `xdputil query` 输出，验证 DPU 的架构名（`DPUCZDX8G_ISA1_B4096`）、频率（325 MHz）与 `is_vivado_flow` 等字段，把 u5-l1 中「TCL 配置 → 资源表 → `xdputil query`」的三处自洽闭环补完。

本讲产出的板载 DPU 运行环境，正是 u6（Vitis AI 推理框架）与 u7（板载推理应用）运行的前置条件。

## 2. 前置知识

本讲假设你已学完 u5-l1～u5-l3，熟悉以下概念（不熟悉的可先回看）：

- **PS / PL 协同**：KV260 的 Zynq UltraScale+ MPSoC 分 PS（ARM Cortex-A53，跑 Linux）与 PL（FPGA，承载 DPU）。本讲做的事，就是告诉 Linux「PL 里现在多了哪些设备」。
- **DPU**：PL 侧的 int8 神经网络加速器 IP，型号 `DPUCZDX8G`，u5-l1 已确认板载为 `DPUCZDX8G_ISA1_B4096`、325 MHz、开启 softmax。
- **`.xsa` 与 SD 卡镜像**：u5-l2 导出的 `.xsa`（硬件平台）被 u5-l3 的 PetaLinux 消费，产出 `BOOT.BIN`/`image.ub`/rootfs，刷进 SD 卡让板子能启动 Linux。**但仅刷 SD 卡还跑不了 DPU**——这正是本讲要解决的「最后一公里」。
- **设备树（Device Tree）**：Linux 描述硬件拓扑的数据结构。KV260 的 PL 侧设备是运行时可变的，所以用「overlay」（叠加片）而非写死在基础设备树里。

一个直觉比喻：SD 卡镜像像是给板子装了「操作系统底座」（知道有 CPU、内存、网口），而加速器固件三件套像是「运行时插上的一张加速卡驱动包」——它告诉操作系统「现在 PL 里烧进了什么电路、有哪些寄存器、有哪些中断」。`xmutil loadapp` 就是「热插拔」这张卡的过程。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [platform/kv260/README.md](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/kv260/README.md) | 全流程主文档，第 3.2 节讲固件制作、第 4 节讲部署与验证，是本讲的主线 |
| [platform/kv260/sw/shell.json](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/kv260/sw/shell.json) | shell 元数据文件，一行 JSON，声明 `XRT_FLAT` 与单 slot |
| [platform/kv260/sw/kv260.dtbo](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/kv260/sw/kv260.dtbo) | 编译好的设备树 overlay 二进制（2430 字节），内含 DPU 节点定义 |
| platform/kv260/sw/project_1.bit.bin | 比特流（约 6.6 MB），即 PL 侧电路的「配置数据」，由 Vivado 生成并改名 |
| platform/kv260/hw/project_1.xsa | u5-l2 导出的硬件平台文件，是 `createdts` 生成 `.dtsi` 的输入 |
| [platform/kv260/sw/helper_build_bsp.sh](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/kv260/sw/helper_build_bsp.sh) | u5-l3 的 PetaLinux 构建脚本，其中禁用 `xrt`/`zocl` 的「vivado flow」是理解 `is_vivado_flow:true` 的线索 |

## 4. 核心概念与源码讲解

### 4.1 固件三件套：KV260 加速器固件的组成

#### 4.1.1 概念说明

KV260 的硬件能力是「**运行时可重配置**（runtime reconfiguration）」：FPGA（PL）里烧什么电路，取决于你加载什么比特流。这与 GPU/CPU 的固定硅片完全不同——同一块 KV260，这一刻可以是「DPU 推理卡」，下一刻（换个 app）可以变成「视频编解码卡」。

AMD/Xilinx 用一套统一的「**accelerator application（加速应用）**」机制来管理这种可重配置性。一个加速应用由**三个文件**组成，README 把它们概括为一句话：

> The accelerator requires three files: the bitstream, a device tree overlay, and a shell metadata file.

这三个文件的分工：

| 文件 | 物理含义 | 让 Linux 知道什么 |
| --- | --- | --- |
| `*.bit.bin` | 比特流（bitstream），PL 侧 LUT/BRAM/布线的配置数据 | 「PL 里要烧成这套电路」 |
| `*.dtbo` | 设备树 overlay（Device Tree Blob Overlay） | 「烧完后 PL 里出现了哪些设备、地址、时钟、中断」（尤其 DPU） |
| `shell.json` | shell 元数据 | 「这个 app 的拓扑类型（扁平/可分区）与 slot 数」 |

**关键区分（容易混淆点）**：本讲的「加速器固件三件套」与 u5-l3 的 SD 卡启动镜像是**两套独立的东西**：

- SD 卡镜像（`BOOT.BIN` + `image.ub` + rootfs）：让板子能**启动到 Linux**，是「底座」。
- 加速器固件三件套：让 Linux **认识并能驱动 DPU**，是「运行时叠加的加速器」。

底座里**不包含** DPU 的电路与设备节点——DPU 是在启动后由 `xmutil loadapp` 动态「插」进来的。这就是为什么即使 SD 卡刷好了，不加载固件时 `xdputil query` 也会找不到 DPU。

#### 4.1.2 核心流程

三件套的生成与衔接关系（伪代码）：

```
# 输入：u5-l2 的产物 .xsa（已含比特流 project_1.bit 与 PS/PL 地址映射）
.xsa
 ├─(1) XSCT createdts  ──→  pl.dtsi          （文本设备树源）
 │                          │
 │      dtc -@ -O dtb       ▼
 │                       kv260.dtbo           （二进制设备树 overlay）
 │
 └─(2) 从 .xsa 解出 project_1.bit
            │  cp + 改名
            ▼
        project_1.bit.bin                     （比特流，名字被 dtbo 引用）

# (3) shell.json 手写一行
echo '{ "shell_type":"XRT_FLAT","num_slots":"1" }' > shell.json

# 最终：三件套放在同一个目录（= app 名），scp 上板后用 xmutil loadapp 加载
```

注意一个**强耦合**：`.dtbo` 里以**文件名字符串**引用了比特流（`firmware-name = "project_1.bit.bin"`，见 4.2.3）。所以比特流必须**精确**改名为 `project_1.bit.bin`，否则 Linux 的 FPGA manager 找不到要烧的文件。这解释了 README 第 191 行那句「This specific naming is expected by the firmware loading utility」。

#### 4.1.3 源码精读

README 第 3.2 节列出三件套的制作步骤：

[platform/kv260/README.md:169-171](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/kv260/README.md#L169-L171) 点明加速器需要三个文件（比特流、设备树 overlay、shell 元数据）。

[platform/kv260/README.md:188-191](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/kv260/README.md#L188-L191) 说明比特流改名为 `project_1.bit.bin`，并强调该命名是固件加载工具的约定。

[platform/kv260/README.md:193-198](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/kv260/README.md#L193-L198) 给出 `shell.json` 的内容与生成命令：

```bash
echo '{ "shell_type" : "XRT_FLAT", "num_slots": "1" }' > shell.json
```

仓库里已经预制好了这三个文件，位于 `platform/kv260/sw/`：

```
platform/kv260/sw/
├── helper_build_bsp.sh      # u5-l3 的 PetaLinux 构建脚本
├── kv260.dtbo               # 设备树 overlay（2430 字节）
├── project_1.bit.bin        # 比特流（6 883 499 字节 ≈ 6.6 MB）
└── shell.json               # shell 元数据（48 字节）
```

[platform/kv260/sw/shell.json:1](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/kv260/sw/shell.json#L1) 就是那一行：

```json
{ "shell_type" : "XRT_FLAT", "num_slots": "1" }
```

两个字段的含义：

- `shell_type: XRT_FLAT`：声明这是一个「**扁平（flat）**」shell——即整个 PL 一次性整体重配置，**不是** DFX（Dynamic Function eXchange，部分重配置）那种把 PL 划成多个可独立切换的区域。扁平 shell 简单、适合单一固定加速器（本项目的 DPU）。
- `num_slots: 1`：运行时只有 1 个加速槽（slot），即同一时刻 PL 上只跑这一个 DPU。

#### 4.1.4 代码实践

**实践目标**：在仓库内亲手确认三件套的存在、大小与文件类型，建立「这三个文件确实在仓库里、且各自有不同性质」的直观印象。

**操作步骤**：

1. 进入仓库根目录，列出 `platform/kv260/sw/` 目录。
2. 用 `file` 命令查看每个文件的类型（比特流是裸二进制、dtbo 是 Device Tree Blob、shell.json 是文本）。

```bash
# 在仓库根目录执行
ls -la platform/kv260/sw/
file platform/kv260/sw/kv260.dtbo platform/kv260/sw/project_1.bit.bin platform/kv260/sw/shell.json
```

**需要观察的现象**：

- `kv260.dtbo` 应被识别为 `Device Tree Blob version 17`（约 2430 字节）。
- `project_1.bit.bin` 是 `data`（裸二进制，约 6.6 MB，体积最大）。
- `shell.json` 是 `ASCII text`（仅 48 字节）。

**预期结果**：三类文件性质迥异——一个是硬件电路配置（最大）、一个是给内核的设备描述（很小）、一个是元数据（最小）。这种「按职责拆分」正是 KV260 可重配置架构的体现。

> 说明：上述命令在仓库根目录即可运行，无需 KV260 硬件。

#### 4.1.5 小练习与答案

**练习 1**：如果把比特流改名为 `my_dpu.bit.bin` 而不是 `project_1.bit.bin`，加载固件时会出什么问题？

**参考答案**：`.dtbo` 里以 `firmware-name = "project_1.bit.bin"` 硬编码引用了比特流文件名（见 4.2.3）。改名后，Linux 的 FPGA manager 在 app 目录下找不到 `project_1.bit.bin`，比特流无法烧入 PL，DPU 设备节点也就不会出现，`xmutil loadapp` 报错或 `xdputil query` 找不到 DPU。要改名必须同时重新生成 `.dtbo` 里对应的 `firmware-name` 字段。

**练习 2**：`shell_type` 选 `XRT_FLAT` 而非 DFX（部分重配置）shell，对本项目意味着什么限制？

**参考答案**：扁平 shell 意味着 PL 作为一个整体被重配置，同一时刻只能加载一个加速应用、不能把 PL 切成多个独立区域同时运行多个加速器。对本项目足够——板上只跑一个 DPU（外加 u8 的 HLS 后处理核），不需要多应用并发；换来的是配置更简单、不需要 DFX 的复杂分区与解耦布线约束。

---

### 4.2 设备树 overlay（dtbo）的生成与结构

#### 4.2.1 概念说明

**设备树（Device Tree）** 是 ARM Linux 描述硬件拓扑的标准方式：一张树形数据结构，告诉内核「系统里有哪些设备、它们的寄存器基址、挂在哪条总线、用什么时钟、触发哪个中断」。内核启动时解析它来注册驱动。

**问题**：KV260 的 PL 侧硬件是**运行时可变**的——你这次烧 DPU，下次可能烧别的。如果把 PL 设备写死在基础设备树里，换电路就得重启换设备树，丧失灵活性。

**解法：设备树 overlay（Dtbo = Device Tree Blob Overlay）**。它是一小段「叠加片」设备树，可在系统运行后动态合并到基础设备树上，向内核声明「现在 PL 里新增了这些设备」。加载加速器固件时，`xmutil` 会把 `.dtbo` 应用到内核，于是内核看到 DPU 并加载 `dpuczdx8g` 驱动。

本项目中，`.dtbo` 最核心的职责就是向内核注册 **DPU 节点**（`dpuczdx8g@80000000`），让 DPU 驱动接管它。

#### 4.2.2 核心流程

`.dtbo` 的生成是两步走（README 第 3.2 节第 1 步）：

```
(1) createdts（XSCT 命令，随 Vitis/Vivado/PetaLinux 提供）
        输入：.xsa（u5-l2 的硬件平台，含 PL 地址映射与 IP 信息）
        输出：pl.dtsi（人类可读的设备树源文本，描述 PL 侧 IP）

(2) dtc（Device Tree Compiler，apt 可装）
        输入：pl.dtsi
        输出：kv260.dtbo（二进制 overlay blob，内核可加载）
```

为什么要分两步？因为 `.xsa` 是 Xilinx 私有格式，只有 XSCT 能读懂并翻译成 Linux 设备树源；而把源文本编译成内核能吃的二进制 blob 则是标准开源工具 `dtc` 的事。两者职责分离。

生成的 `.dtbo` 内部是「fragment（片段）」结构，每个 fragment 叠加到基础设备树的某个锚点上。本项目 `.dtbo` 含两个 fragment：

```
fragment@0  ──→  叠加到 FPGA manager 节点
                  └─ firmware-name = "project_1.bit.bin"   # 声明要烧的比特流

fragment@1  ──→  叠加到 PL 总线节点
                  ├─ afi0          (xlnx,afi-fpga)         # PS-PL 接口配置
                  ├─ clocking0     (xlnx,fclk)             # PL fabric 时钟
                  ├─ dpuczdx8g@80000000 (xlnx,dpuczdx8g-4.1)  # ★ DPU 节点
                  │     ├─ clocks:    dpu_2x_clk / m_axi_dpu_aclk / s_axi_aclk
                  │     └─ interrupts: dpu0_interrupt / sfm_interrupt
                  ├─ misc_clk_0/1   (fixed-clock)
                  └─ zyxclmm_drm    (xlnx,zocl)            # 保留的 drm 节点
```

其中 `dpuczdx8g@80000000` 是关键：`@80000000` 是 DPU 在 AXI 总线上的物理基址，`xlnx,dpuczdx8g-4.1` 是驱动用来匹配的 compatible 字符串，三个 `*_clk` 是 DPU 的工作时钟，`dpu0_interrupt` 是主计算核中断、`sfm_interrupt` 是 softmax 引擎中断（呼应 u5-l1 的 SFM_ENA=1）。

#### 4.2.3 源码精读

[platform/kv260/README.md:173-186](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/kv260/README.md#L173-L186) 给出两步命令：

```bash
# 第一步：XSCT 终端内，从 .xsa 生成 pl.dtsi
createdts -hw /path/to/your/project.xsa -zocl -platform-name KV260 \
  -git-branch xlnx_rel_v2023.2 -overlay -compile \
  -out /path/to/output_dtsi_dir/

# 第二步：用 dtc 把 pl.dtsi 编译成 .dtbo
dtc -@ -O dtb -o ./kv260.dtbo \
  /path/to/output_dtsi_dir/KV260/psu_cortexa53_0/device_tree_domain/bsp/pl.dtsi
```

几个关键开关：

- `-hw ...project.xsa`：消费 u5-l2 的硬件平台文件作为输入。
- `-overlay`：生成 overlay（叠加片）而非完整设备树。
- `-zocl`：在生成的设备树里加入 `xlnx,zocl`（zyxclmm_drm）节点——即使本项目走 vivado flow，这个节点仍被保留（见 4.2.3 末尾的说明）。
- `dtc -@`：`-@` 让 `dtc` 生成带符号表（`__symbols__`）的 blob，overlay 机制靠它定位叠加锚点。

[platform/kv260/sw/kv260.dtbo](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/kv260/sw/kv260.dtbo) 是仓库已编译好的二进制。由于它是二进制，直接 `cat` 看不出结构，但提取其中的可读字符串即可印证上面的 fragment 结构（用 `strings platform/kv260/sw/kv260.dtbo`）。你会依次看到 `fragment@0`、`project_1.bit.bin`、`fragment@1`、`dpuczdx8g@80000000`、`xlnx,dpuczdx8g-4.1`、`dpu0_interrupt`、`sfm_interrupt`、`xlnx,zocl` 等节点名与 compatible 串——它们正是 4.2.2 那棵树的真实来源。

**关于 `zocl` 与 vivado flow 的一个细节**：u5-l3 的 [platform/kv260/sw/helper_build_bsp.sh:42-46](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/kv260/sw/helper_build_bsp.sh#L42-L46) 在 rootfs 里**禁用**了 `xrt`/`zocl`，注释写明是「for vivado flow」（DPU 走独立字符设备驱动而非 XRT/zocl 栈）。但 `createdts -zocl` 仍在设备树里保留了 zocl 节点——这两者并不矛盾：设备树声明节点只是「告知硬件存在」，而真正的运行时栈由 `xdputil query` 里 `is_vivado_flow:true` 标志决定（DPU 用 vivado flow 的字符设备驱动，不依赖 XRT runtime）。这条线索把 u5-l3 的构建脚本与本讲的 `xdputil query` 输出串了起来。

#### 4.2.4 代码实践

**实践目标**：不依赖 KV260，在主机上直接「解剖」仓库里的 `kv260.dtbo`，读出其中的 DPU 节点与 fragment 结构，验证它确实描述了 DPU 硬件。

**操作步骤**：

```bash
# 方式 A：用 dtc 反编译成可读的设备树源（推荐，若主机装了 dtc）
dtc -I dtb -O dts platform/kv260/sw/kv260.dtbo | less

# 方式 B：仅用 strings 粗看节点名与 compatible（任何主机都能跑）
strings platform/kv260/sw/kv260.dtbo
```

**需要观察的现象**：

- 应能看到 `fragment@0` 与 `fragment@1` 两个叠加片段。
- `fragment@0` 内引用 `project_1.bit.bin`（即比特流文件名）。
- `fragment@1` 内含 `dpuczdx8g@80000000`、compatible `xlnx,dpuczdx8g-4.1`、时钟 `dpu_2x_clk`/`m_axi_dpu_aclk`/`s_axi_aclk`、中断 `dpu0_interrupt`/`sfm_interrupt`。

**预期结果**：确认 `.dtbo` 的核心就是「声明 DPU 这个 PL 设备 + 指定要烧的比特流」。若主机未装 `dtc`，方式 B 的 `strings` 已足够验证关键节点存在（其余可视化效果待本地用 `dtc` 验证）。

> 说明：方式 B 在仓库根目录可直接运行；方式 A 需 `sudo apt install device-tree-compiler`。

#### 4.2.5 小练习与答案

**练习 1**：为什么用 overlay（`.dtbo`）而不是把 DPU 节点直接写进基础设备树？

**参考答案**：KV260 的 PL 是运行时可重配置的，DPU 并非板上固定硅片。基础设备树描述的是启动时即存在的固定硬件；DPU 是启动后由 `xmutil loadapp` 动态「插入」的，所以用 overlay 在运行时叠加声明。这样换加速应用时只需换 overlay，无需改基础设备树或重启换镜像。

**练习 2**：`dpuczdx8g@80000000` 里的 `@80000000` 与 `xlnx,dpuczdx8g-4.1` 分别对内核驱动起什么作用？

**参考答案**：`@80000000` 是 DPU IP 在 AXI 总线上的物理基址，驱动据此 `ioremap` 映射寄存器空间；`xlnx,dpuczdx8g-4.1` 是 compatible 字符串，内核用它做驱动匹配（platform driver 的 `of_match_table`），找到后才会 probe 并初始化 DPU。两者缺一：没有地址则驱动无法访问寄存器，没有 compatible 则内核根本不会尝试加载驱动。

---

### 4.3 xmutil 板载加载与 xdputil 验证

#### 4.3.1 概念说明

**xmutil** 是 Kria SOM 上管理加速应用的命令行工具，扮演「加速应用调度器」角色。KV260 同时只能激活一个加速应用（slot 数由 `shell.json` 的 `num_slots` 决定，本项目为 1），xmutil 负责：

- `xmutil listapps`：列出系统已知的 app 与当前激活的 app。
- `xmutil loadapp <name>`：加载某个 app（应用 overlay + 烧比特流 + 注册设备）。
- `xmutil unloadapp`：卸载当前 app（释放 PL）。

xmutil 约定：每个 app 是 `/lib/firmware/xilinx/<app-name>/` 目录下的**一组文件**，**目录名就是 app 名**。本项目把 app 命名为 `kv260-dpu-trd`（见 README 命令），于是三件套要放进 `/lib/firmware/xilinx/kv260-dpu-trd/`，加载命令就是 `xmutil loadapp kv260-dpu-trd`。

**xdputil** 是 Vitis AI 提供的 DPU 诊断工具。`xdputil query` 读取已加载 DPU 的运行时信息（IP 版本、架构、频率、Vitis AI 库版本等），是验证「DPU 真的被加载成功且参数正确」的权威手段。它把 u5-l1 里 TCL 配置的 DPU 参数（`DPUCZDX8G_ISA1_B4096`、325 MHz、softmax 使能）与运行时实测**对账**——三者一致（TCL 配置 → 资源表 → `xdputil query`）才算部署成功。

#### 4.3.2 核心流程

板载部署的完整时序（README 第 4 节）：

```
[主机侧]
  scp 三件套 ──────────────────────→  /home/root/   (板上临时目录)

[板侧，ssh 登录后]
  mkdir -p /lib/firmware/xilinx/kv260-dpu-trd          # 建 app 目录（目录名=app名）
  mv /home/root/{kv260.dtbo,project_1.bit.bin,shell.json} \
        /lib/firmware/xilinx/kv260-dpu-trd/            # 三件套就位

  xmutil unloadapp                                     # 先卸载当前 app（若有）
  xmutil loadapp kv260-dpu-trd                         # 加载 → "Accelerator loaded to slot 0"
        │
        │  内部动作：
        │   1. 读 shell.json（XRT_FLAT, 1 slot）
        │   2. 应用 kv260.dtbo → 内核注册 dpuczdx8g 节点
        │   3. FPGA manager 烧 project_1.bit.bin 到 PL
        │   4. DPU 驱动 probe，/dev/dpu* 出现
        ▼
  xdputil query                                        # 验证 DPU Arch / 频率 / is_vivado_flow
```

成功标志：`xmutil loadapp` 输出 `Accelerator loaded to slot 0`，随后 `xdputil query` 返回 DPU 信息 JSON。

#### 4.3.3 源码精读

**部署命令序列**（README 第 4 节）：

[platform/kv260/README.md:208-212](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/kv260/README.md#L208-L212) 用 `scp` 把三件套拷到板上的 `/home/root/`：

```bash
scp kv260.dtbo project_1.bit.bin shell.json root@<ip_address_of_kv260>:/home/root/
```

[platform/kv260/README.md:214-225](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/kv260/README.md#L214-L225) 在板上建 app 目录并就位三件套：

```bash
ssh root@<ip_address_of_kv260>
mkdir -p /lib/firmware/xilinx/kv260-dpu-trd
mv /home/root/{kv260.dtbo,project_1.bit.bin,shell.json} /lib/firmware/xilinx/kv260-dpu-trd/
```

[platform/kv260/README.md:226-234](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/kv260/README.md#L226-L234) 卸载旧 app、加载新 app：

```bash
xmutil unloadapp
xmutil loadapp kv260-dpu-trd
# 期望输出: Accelerator loaded to slot 0
```

**验证输出**（README 第 4 节第 4 步），[platform/kv260/README.md:236-265](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/kv260/README.md#L236-L265) 给出 `xdputil query` 的 JSON 输出。摘其关键字段解读：

| 字段 | 值 | 含义与对账 |
| --- | --- | --- |
| `IP version` | `v4.1.0` | DPU IP 版本，对应 u5-l1 的 DPU 4.1 |
| `enable softmax` | `True` | softmax 引擎使能（SFM_ENA=1），对应 dtbo 里的 `sfm_interrupt` |
| `DPU Arch` | `DPUCZDX8G_ISA1_B4096` | 板载架构名，与 u5-l1 完全一致 |
| `DPU Frequency (MHz)` | `325` | 工作频率 325 MHz，与 u5-l1 一致 |
| `XRT Frequency (MHz)` | `100` | XRT/管理时钟 100 MHz |
| `cu_idx` | `0` | compute unit 索引（只有一个 DPU 核） |
| `is_vivado_flow` | `true` | 走 vivado flow（字符设备驱动），呼应 helper_build_bsp.sh 禁用 xrt/zocl |
| `fingerprint` | `0x101000056010407` | DPU 唯一指纹，编译模型时须匹配 |

[platform/kv260/README.md:240-265](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/kv260/README.md#L240-L265) 的 `kernels` 数组里只有一项 `name: "DPU Core 0"`，正对应 `DPU Core Count: 1`——板上只有一个 DPU 核。

至此，u5-l1 提出的「TCL 配置 → 资源利用率表 → `xdputil query`」三处自洽闭环在本讲**验证完成**：设计时配的 `DPUCZDX8G_ISA1_B4096`/325MHz/softmax-on，运行时 `xdputil query` 全部如实报告。

#### 4.3.4 代码实践

**实践目标**：写出 KV260 板载部署的完整命令序列（本讲主实践），并学会逐字段解读 `xdputil query`。由于多数读者手边没有 KV260，命令以「待上板验证」方式给出，但每一步都可对照 README 逐一核对。

**操作步骤**：

1. 主机侧：把三件套从仓库 `platform/kv260/sw/` 拷上板（假设板 IP 为 `192.168.1.99`，默认 `root:root`）。

```bash
cd platform/kv260/sw
scp kv260.dtbo project_1.bit.bin shell.json root@192.168.1.99:/home/root/
```

2. 板侧（`ssh root@192.168.1.99` 登录后）：建 app 目录、就位三件套。

```bash
mkdir -p /lib/firmware/xilinx/kv260-dpu-trd
mv /home/root/{kv260.dtbo,project_1.bit.bin,shell.json} /lib/firmware/xilinx/kv260-dpu-trd/
ls /lib/firmware/xilinx/kv260-dpu-trd/   # 确认三件套都在
```

3. 卸载当前 app，加载本项目 app。

```bash
xmutil unloadapp                          # 若提示无 app 可卸，忽略即可
xmutil loadapp kv260-dpu-trd              # 期望: Accelerator loaded to slot 0
```

4. 验证 DPU。

```bash
xdputil query
```

**需要观察的现象**：

- 第 3 步看到 `Accelerator loaded to slot 0`。
- 第 4 步输出的 JSON 中 `DPU Arch` 应为 `DPUCZDX8G_ISA1_B4096`，`DPU Frequency (MHz)` 应为 `325`，`is_vivado_flow` 为 `true`，`enable softmax` 为 `True`。

**预期结果 / 字段解读（对照 4.3.3 的表）**：

- `DPU Arch: DPUCZDX8G_ISA1_B4096`：确认烧入的是本项目配置的 DPU（ISA1 = 第一代指令集，B4096 = 4096 MAC 批次），与 u4 编译模型时 `arch.json` 选的架构必须一致，否则模型跑不了。
- `DPU Frequency (MHz): 325`：DPU 实际工作频率，决定理论算力（MAC/s ∝ 频率）。
- `is_vivado_flow: true`：确认运行时栈是 vivado flow 的字符设备驱动，与 u5-l3 在 rootfs 禁用 `xrt`/`zocl` 的决策自洽。
- 若 `DPU Core Count: 1` 且 `kernels` 数组只有一项，说明只有一个 DPU 核被注册，符合设计。

> 说明：上述命令需在真实 KV260 上运行（待本地/上板验证）。无硬件时，可把本序列作为部署 checklist 使用，并重点练习 `xdputil query` JSON 的字段解读。

#### 4.3.5 小练习与答案

**练习 1**：执行 `xmutil loadapp kv260-dpu-trd` 前为什么要先 `xmutil unloadapp`？

**参考答案**：KV260 同时只能激活一个加速应用（`shell.json` 的 `num_slots:1`，PL 作为一个整体被重配置）。如果已有 app（如出厂的视觉应用）占用 PL，直接 loadapp 会失败；必须先 unloadapp 释放 PL 与相关设备节点，再加载新 app。若板上当前无 app，unloadapp 会提示无可卸载项，可安全忽略。

**练习 2**：若 `xdputil query` 报告的 `DPU Arch` 与 u4 阶段 `arch.json` 指定的架构不一致，会发生什么？

**参考答案**：u4（u4-l4）用 `vai_c_xir -a arch.json` 把量化模型编译成**针对特定 DPU 架构**的指令包，编译结果与架构强绑定（甚至含 `fingerprint` 校验）。若运行时 DPU 架构与编译时不一致，DPU 无法正确执行模型指令，推理会失败或给出错误结果。这正是 u4-l4 强调「`arch.json` 是阶段④⑤唯一硬件耦合点、换板必换重编译」的原因——本讲的 `xdputil query` 就是发现这种不匹配的诊断手段。

---

## 5. 综合实践

**任务：制作一份「从 .xsa 到 DPU 可查询」的端到端部署清单。**

把本讲（及 u5-l2/u5-l3）串起来，写一份可照着做的部署文档，要求：

1. **产物清点**：列出从 u5-l2（`.xsa`、`project_1.bit`）到本讲三件套（`kv260.dtbo`、`project_1.bit.bin`、`shell.json`）的来源——每个文件来自哪条命令。
2. **固件制作三步**：写出 `createdts` → `dtc` → 比特流改名 → 写 `shell.json` 的完整命令（参数用占位路径，标注每个参数含义）。
3. **板载部署四步**：写出 `scp` → `mkdir/mv` → `unloadapp/loadapp` → `xdputil query` 的命令序列。
4. **验收判据**：明确指出 `xdputil query` 输出中哪些字段必须为何值才算部署成功（`DPU Arch`、`DPU Frequency`、`is_vivado_flow`、`enable softmax`），并说明每个字段分别与 u5-l1（硬件配置）、u5-l3（rootfs 裁剪）、u4-l4（arch.json）的哪一步对账。

**进阶思考**：如果你要在同一块 KV260 上额外加载 u8 的 HLS 后处理解码核（需要新比特流与新 dtbo），现有 `shell.json`（`XRT_FLAT`/`num_slots:1`）是否还够用？需要怎么改？（提示：考虑 DPU 与后处理核是放同一个比特流还是分两个 app，以及扁平 shell 与 slot 数的限制。）

> 说明：本任务以文档撰写为主，无需硬件即可完成；命令的可运行性待上板验证。

## 6. 本讲小结

- KV260 的加速器能力以「**三件套固件**」形式提供：`project_1.bit.bin`（比特流，PL 电路配置）、`kv260.dtbo`（设备树 overlay，向内核声明 PL 设备尤其 DPU）、`shell.json`（`XRT_FLAT`/`num_slots:1` 的 shell 元数据）。
- 三件套与 SD 卡启动镜像是**两套独立**的东西：SD 卡镜像（u5-l3 的 `BOOT.BIN`/`image.ub`/rootfs）只让板子启动到 Linux；DPU 是启动后由 `xmutil loadapp` **动态加载**的，二者不可混淆。
- `.dtbo` 由 `createdts`（从 `.xsa` 生成 `pl.dtsi`）+ `dtc`（编译成 blob）两步生成；其核心是 `dpuczdx8g@80000000` 节点，含 DPU 的地址、compatible、时钟与中断（含 softmax 的 `sfm_interrupt`）。比特流文件名被 dtbo 以 `firmware-name` 硬引用，故必须精确改名 `project_1.bit.bin`。
- 板载部署序列：`scp` 三件套上板 → 建 `/lib/firmware/xilinx/kv260-dpu-trd/` 目录并就位 → `xmutil unloadapp` → `xmutil loadapp kv260-dpu-trd` → `xdputil query` 验证。
- `xdputil query` 是验收权威：`DPU Arch=DPUCZDX8G_ISA1_B4096`、`DPU Frequency=325`、`enable softmax=True`、`is_vivado_flow=true` 与 u5-l1 的 TCL 配置、u5-l3 禁用 xrt/zocl 的决策**三处自洽**，补完了 u5-l1 的闭环。
- 本讲产出的「板上可查询的 DPU」是 u6（Vitis AI 推理框架补丁）与 u7（板载 C++ 推理应用）运行的前置底座；u4-l4 编译模型时的 `arch.json`/`fingerprint` 必须与本讲的 `xdputil query` 对账一致。

## 7. 下一步学习建议

- **横向承接（推理框架）**：下一步进入 **u6-l1（框架补丁总览与图像加载/归一化）**——DPU 已就绪后，要在其上跑 YOLOv8，需先给 Vitis AI 框架打补丁（TIFF 加载、signed int8 归一化），本讲的 `xdputil query` 正是 u6/u7 跑起来前的自检手段。
- **纵向深挖（硬件后处理）**：若对 PL 侧加速器意犹未尽，可跳读 **u8（HLS 后处理解码内核）**，看一个全新的 PL 加速核（解码核）如何从 HLS 源码综合成 `.xo`、再与本讲的 DPU 一起打包进同一个 shell——届时可回看本讲的 `shell.json`/dtbo 机制如何容纳多核。
- **补强阅读**：想更懂设备树 overlay，可参考内核文档 `Documentation/devicetree/overlay-notes.txt`；想更懂 KV260 app 机制，可读 AMD 官方《Kria SOM Carrier Card Design》（UG1091）与 `xmutil` 手册。
