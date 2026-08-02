# FPGA 验证与综合流程

## 1. 本讲目标

本讲把 Ventus GPGPU 从「仿真」推向「真实硬件」。学完后你应该能够：

- 说清 FPGA 验证平台（`FPGA_test`）的整体结构：谁当主机、谁当内存、GPGPU 以什么接口接入。
- 看懂 Vivado 块设计脚本 `ventus_fpga.tcl` 建立了哪些 IP、综合顶层是谁。
- 读懂 Microblaze 上运行的 C 驱动（`naive_driver.c`）：它如何把 kernel 的指令/数据写入 DDR，又如何经 AXI4-Lite 寄存器触发一次 workgroup 派发。
- 区分两种「落地」目标：FPGA 实现（Vivado，FPGA 厂商工艺）与 ASIC DC 综合（tsmc 28nm），并能说出 README 中 620MHz / 3.908mm² 是哪一种、在什么配置下得到的。
- 理解 `T28_MEM` 宏的作用：开启后行为级 SRAM 被替换为 28nm 工艺编译器宏，从而打通 ASIC 综合流；关闭则用于仿真与 FPGA。

## 2. 前置知识

- **仿真 vs. 实现 vs. 综合**：u1-l4 讲的是用 VCS 跑仿真（`simv`、`test.fsdb`），那是在「软件里模拟硬件」。本讲关心两件更现实的事：把 RTL 烧进 FPGA（**实现 implementation**），以及用工艺库估算 ASIC 的频率/面积（**综合 synthesis**）。
- **AXI4 / AXI4-Lite**：u7-l4 已建立——GPGPU 对外有两个 AXI 口：一个 AXI4-Lite **从**口供主机配置派发，一个 AXI4 **主**口供 GPGPU 访问外部 DRAM。本讲你会看到真实的主机（Microblaze）和真实的 DRAM（DDR4）如何接上去。
- **CTA 派发的寄存器视图**：u7-l4 讲过 `axi4lite_2_cta` 用一组 32 位寄存器（写 `reg[0]=1` 触发派发、回读 `reg[17]` 等完成）。本讲的 C 驱动就是这些寄存器的「主机的另一侧」。
- **SRAM 的两种来源**：寄存器堆、cache 的存储体在 RTL 里写成「行为级 RAM」（可综合成触发器或 FPGA 的 BRAM）；做 ASIC 时，这些存储体要换成晶圆厂提供的 **SRAM 编译器宏**（Compiler Macro），面积/功耗才真实。`T28_MEM` 就是这个切换开关。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [FPGA_test/README.md](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/FPGA_test/README.md) | FPGA 验证框架说明：块设计各 IP 角色、地址映射、建工程步骤 |
| [FPGA_test/ventus_fpga.tcl](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/FPGA_test/ventus_fpga.tcl) | Vivado 导出的 Tcl 脚本：创建工程、加入 RTL、搭建 `config_mb` 块设计 |
| [FPGA_test/driver/naive_driver.c](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/FPGA_test/driver/naive_driver.c) | 运行在 Microblaze 上的主机驱动 main 函数 |
| [FPGA_test/driver/naive_driver.h](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/FPGA_test/driver/naive_driver.h) | 寄存器偏移定义、`GpuSendTask`/`init_mem`/`process_data` 等驱动实现 |
| [README.md](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/README.md) | 顶层说明；其中「综合」章节给出 ASIC DC 指标 |
| [src/define/define.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v) | 配置总开关；`T28_MEM` 宏与规模参数都在此 |

辅助引用（用于说明宏替换与 GPGPU 接入点）：

| 文件 | 作用 |
|------|------|
| [src/gpgpu_top/sm/pipeline/operand_collector/scalar_regfile_bank.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/operand_collector/scalar_regfile_bank.v) | 标量寄存器堆 bank，`T28_MEM` 切换 SRAM 宏 |
| [src/gpgpu_top/sm/pipeline/operand_collector/vector_regfile_bank.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/operand_collector/vector_regfile_bank.v) | 向量寄存器堆 bank，`T28_MEM` 切换 SRAM 宏 |
| [src/gpgpu_top/sm/l1cache/dcache/l1_dcache.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/dcache/l1_dcache.v) | D-cache，`T28_MEM` 切换响应 FIFO 的 SRAM 宏 |
| [src/gpgpu_top/gpgpu_axi_adpater.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/gpgpu_axi_adpater.v) | FPGA 块设计中引用的 GPGPU 顶层 `gpgpu_axi_adapter_top` |

> 小提示：`gpgpu_axi_adpater.v` 的文件名拼写是 `adpater`（应为 adapter），但模块名是正确的 `gpgpu_axi_adapter_top`。读源码时以模块名为准。

## 4. 核心概念与源码讲解

本讲围绕一条主线：**同一份 RTL，三种落地形态**——

```
                        ┌── 仿真 (VCS, u1-l4)
RTL (src/) ─────────────┼── FPGA 实现 (Vivado + FPGA_test)   ← 行为级 SRAM → BRAM/FF
                        └── ASIC 综合 (DC + tsmc 28nm)        ← T28_MEM → SRAM 编译器宏
```

四个最小模块分别对应：搭建 FPGA 系统（4.1）、写主机驱动（4.2）、ASIC 综合指标（4.3）、SRAM 宏切换（4.4）。

---

### 4.1 FPGA 验证系统的搭建：ventus_fpga.tcl 与 Vivado 块设计

#### 4.1.1 概念说明

仿真（u1-l4）里主机和内存都是 testbench「假装」的（`host_inter`、`axi_ram`）。FPGA 验证要把它们换成真实的硬件：用一个软核处理器当主机、用真 DDR4 当内存、用 GPIO 点灯看结果。这套「主机 + 互连 + 内存 + GPGPU」的连线，在 Xilinx Vivado 里用 **块设计（Block Design, BD）** 描述，再用 Tcl 脚本一键重建——这就是 [ventus_fpga.tcl](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/FPGA_test/ventus_fpga.tcl)。

`ventus_fpga.tcl` 是 Vivado **导出（write_project_tcl）** 产生的脚本，作用是「把别人电脑上搭好的工程，在你电脑上原样重建」。

#### 4.1.2 核心流程

建一个 FPGA 工程的标准三步：

1. **建工程选器件**：`create_project ... -part <型号>`，绑定目标 FPGA 芯片。
2. **加源码**：`add_files` 把全部 RTL（`define.v`、`GPGPU_top.v`、L2、SM 流水线……）拉进 `sources_1` 文件集。
3. **建块设计 + 设顶层**：`create_bd_design config_mb` 搭出系统，`make_wrapper` 生成 `config_mb_wrapper`，并设为综合顶层。

块设计 `config_mb` 里挂的部件（与 README 框图一一对应）：

| BD 部件 | 角色（对应 README 术语） |
|---------|--------------------------|
| `microblaze_0` | 软核处理器，系统控制单元（**Microblaze**） |
| `mdm_1` | Microblaze Debug Module，经 JTAG 下载/调试 `.elf`（**MDM**） |
| `microblaze_0_local_memory`（层级） | 软核本地内存，存 `.elf` 指令/数据（**Microblaze Local Memory**） |
| `ddr4_0` | DDR4 控制器（MIG），接外部 DRAM（**DDR4 SDRAM**） |
| `axi_cdma_0` | 中央 DMA，在 AXI 上高效搬内存（**CDMA**） |
| `axi_smc` | SmartConnect 高性能互连（**AXI smc**） |
| `axi_gpio_0` | 通用 IO，驱动 LED 显示结果（**GPIO**） |
| `axi_uartlite_0` | 串口打印调试信息（**Uartlite**） |
| `clk_wiz_0` | 时钟向导，产生各部件所需时钟 |
| `gpgpu_axi_adapter_top_0` | GPGPU 本体（**GPGPU**，模块引用） |

#### 4.1.3 源码精读

**① 选定目标 FPGA 器件** —— [ventus_fpga.tcl:290](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/FPGA_test/ventus_fpga.tcl#L290) 指定芯片型号为 Xilinx Virtex UltraScale+ `xcvu37p`：

```tcl
create_project ${_xil_proj_name_} "E:/FPGA/ventus/..." -part xcvu37p-fsvh2892-2L-e
```

紧随其后 [ventus_fpga.tcl:297](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/FPGA_test/ventus_fpga.tcl#L297) 绑定开发板 `vcu128`。这一行决定了整个工程的物理目标——后续综合实现的时序、资源都以 VU37P 的资源（LUT/FF/BRAM/UltraRAM）为准。

**② 把 RTL 全部加入工程** —— [ventus_fpga.tcl:337](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/FPGA_test/ventus_fpga.tcl#L337) 起的 `set files [list ...]` 列出了所有源文件路径（从 `define.v`、`GPGPU_top.v` 到 L2、SM 各执行单元），并在 [ventus_fpga.tcl:527](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/FPGA_test/ventus_fpga.tcl#L527) 用 `add_files -norecurse` 一次性并入。注意脚本里的路径是 `.../ventus-gpgpu-verilog-main/src/...`，即它假设 `src` 与 `FPGA_test` 是平级目录，使用前需保证目录布局。

**③ 把块设计 wrapper 设为综合顶层** —— [ventus_fpga.tcl:649](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/FPGA_test/ventus_fpga.tcl#L649)：

```tcl
set_property -name "top" -value "config_mb_wrapper" -objects $obj
```

意思是：综合的不是 `GPGPU_top`，而是 `config_mb_wrapper`——它把 Microblaze、DDR4、CDMA、GPGPU 等全部连好的整个 SoC 当作顶层。

**④ GPGPU 在块设计中作为「模块引用」挂入** —— [ventus_fpga.tcl:1525](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/FPGA_test/ventus_fpga.tcl#L1525)：

```tcl
set block_name gpgpu_axi_adapter_top
set gpgpu_axi_adapter_top_0 [create_bd_cell -type module -reference $block_name ...]
```

`-type module -reference` 表示这个 BD 单元直接引用 RTL 里已加入的模块 `gpgpu_axi_adapter_top`（定义在 [gpgpu_axi_adpater.v:3](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/gpgpu_axi_adpater.v#L3)）。它对外暴露一个 AXI4-Lite 从口（`s_axilite_*`，接 Microblaze）和一个 AXI4 主口（`m_axi_*`，接 DDR4），内部 [gpgpu_axi_adpater.v:82](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/gpgpu_axi_adpater.v#L82) 例化 `gpgpu_axi_top`——也就是 u7-l4 讲过的那个把 TileLink 转 AXI 的壳。其余 IP（[microblaze_0:1539](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/FPGA_test/ventus_fpga.tcl#L1539)、[ddr4_0:1505](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/FPGA_test/ventus_fpga.tcl#L1505)、[axi_cdma_0:1446](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/FPGA_test/ventus_fpga.tcl#L1446)、[axi_smc:1459](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/FPGA_test/ventus_fpga.tcl#L1459)、[axi_gpio_0:1453](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/FPGA_test/ventus_fpga.tcl#L1453)）都是 Xilinx 官方 IP。

#### 4.1.4 代码实践

**实践目标**：在不出板的情况下，用脚本结构理解「FPGA 工程由什么构成」。

**操作步骤**（源码阅读型）：

1. 打开 [FPGA_test/README.md:5](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/FPGA_test/README.md#L5)，对照其中的 MDM/Microblaze/CDMA/GPIO/AXI smc/DDR4 文字说明。
2. 在 `ventus_fpga.tcl` 中搜索 `create_bd_cell`，把每个 IP 与 README 术语一一对应。
3. 找到 [ventus_fpga.tcl:649](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/FPGA_test/ventus_fpga.tcl#L649) 的 `top` 设置，确认综合顶层不是 GPU 而是 SoC。

**需要观察的现象**：你会发现 BD 里同时存在 `microblaze_0_axi_periph`（外设互连）和 `axi_smc`（高性能互连）两套互连——Microblaze 走外设互连配置 GPGPU 的 AXI4-Lite 寄存器，GPGPU/CDMA 走 SmartConnect 访问 DDR4。

**预期结果**：能画出「Microblaze → AXI 互连 → GPGPU(s_axilite) 派发；GPGPU(m_axi)/CDMA → SmartConnect → DDR4」的拓扑。若手头有 Vivado + VCU128 开发板，按 [FPGA_test/README.md:22](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/FPGA_test/README.md#L22) 执行 `source ventus_fpga.tcl` 可生成比特流；否则标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么综合顶层是 `config_mb_wrapper` 而不是 `GPGPU_top`？
**答**：FPGA 验证需要整套 SoC（主机 + 内存 + GPU）才能独立运行。`config_mb_wrapper` 把 Microblaze、DDR4、互连和 GPGPU 全连好，是可下板的完整系统；单独的 `GPGPU_top` 没有主机驱动它、也没有内存可访。

**练习 2**：`create_bd_cell -type module -reference` 与 `-type ip -vlnv` 有何不同？
**答**：前者引用工程内已有的 RTL 模块（如 GPGPU，源码可见可改）；后者引用 Xilinx 官方加密 IP（如 Microblaze、DDR4），按版本号（vlnv）实例化，不可看源码。

---

### 4.2 主机驱动：Microblaze 与 naive_driver

#### 4.2.1 概念说明

u1-l4 的 `host_inter` 是 SystemVerilog 写的「假装主机」，用 task 直接读写 testbench 信号。上板后，主机换成了真实的 32 位软核 **Microblaze**，它跑的是 C 程序——即 [FPGA_test/driver/](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/FPGA_test/driver) 下的 `naive_driver.c`。SDK 把它编译成 `.elf`，经 JTAG/MDM 灌进 Microblaze 本地内存后开始执行。

驱动的职责与 `host_inter` 完全对称：(1) 把 kernel 指令/数据写进 DDR；(2) 经 AXI4-Lite 写 GPGPU 的寄存器触发派发；(3) 读结果并验证。

#### 4.2.2 核心流程

`main` 的主干（[naive_driver.c:12](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/FPGA_test/driver/naive_driver.c#L12)）：

```
GpuInit(&TestGpu, GPU_BASEADDR)            // 记下 GPU 的 AXI4-Lite 基地址 0x20000000
GpuTaskMemoryInit(&TestMem, 128, 1024)     // 分配指令/数据缓冲
assign_metadata_values(metadata)           // 从 metadata[] 解析 wf_size/wg_size/各 base
init_mem(metadata, data, 128, 1024)        // 把 kernel 数据按 burst 写入 DDR
GpuSendTask(&TestGpu, &TestTask)           // 逐个写 AXI4-Lite 寄存器，最后写 valid=1
读 0x90002000 共 32 字 → process_data       // 读结果，比对 0x42000000(=32.0f)
写 LED_BASE_ADDRESS                        // 点灯示意
```

地址映射（来自 [naive_driver.h:7](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/FPGA_test/driver/naive_driver.h#L7) 与 [FPGA_test/README.md:15](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/FPGA_test/README.md#L15)）：

| 地址 | 含义 |
|------|------|
| `0x20000000` | GPU AXI4-Lite 控制口（`GPU_BASEADDR`） |
| `0x40000000` | GPIO / LED |
| `0x70000000` | GPU local memory（DDR 中划出的 2MB 段） |
| `0x80000000` | DDR 基地址（`DDR_BASE_ADDRESS`） |
| `0x90002000` | 结果缓冲基地址（kernel 写回处） |

#### 4.2.3 源码精读

**① GPGPU 的 AXI4-Lite 寄存器映射** —— [naive_driver.h:12](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/FPGA_test/driver/naive_driver.h#L12) 起定义了一组偏移量，这就是 u7-l4 `axi4lite_2_cta` 那 18 个寄存器的「主机侧视图」：

```c
#define GPU_VALID_OFFSET        0x00   // 写 1 触发 host_req（对应 reg[0]）
#define GPU_WG_ID_OFFSET        0x04
#define GPU_NUM_WF_OFFSET       0x08   // workgroup 的 warp 数
#define GPU_WF_SIZE_OFFSET      0x0c   // 每个 warp 的线程数
#define GPU_START_PC_OFFSET     0x10
#define GPU_VGPR_SIZE_T_OFFSET  0x14
...
#define GPU_WG_ID_DONE_OFFSET   0x38   // 完成回读（对应 reg[17]@0x44? 见说明）
```

> 对应关系提示：这里偏移以 0x04 递增、`GPU_VALID_OFFSET=0x00` 对应 u7-l4 的 `reg[0]`（写 1 触发派发）。注意 `naive_driver.h` 用的是字节偏移，而 `axi4lite_2_cta` 用的是寄存器序号，两者换算时 1 个寄存器 = 4 字节。

**② `GpuSendTask`：组装一次派发** —— [naive_driver.h:91](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/FPGA_test/driver/naive_driver.h#L91) 逐个写寄存器，**最后一拍写 `GPU_VALID_OFFSET=1`** 才真正拉起 `host_req_valid`：

```c
Gpu_WriteReg(BaseAddr, GPU_NUM_WF_OFFSET,   TaskCfg->NumWf);
Gpu_WriteReg(BaseAddr, GPU_WF_SIZE_OFFSET,  TaskCfg->WfSize);
Gpu_WriteReg(BaseAddr, GPU_START_PC_OFFSET, TaskCfg->StartPC);
... // 各 VGPR/SGPR/LDS/base 参数
Gpu_WriteReg(BaseAddr, GPU_VALID_OFFSET, 0x1);   // 触发派发
```

这与 u2-l1/u2-l2 的派发字段（wg_id、wf_count、start_pc、vgpr/sgpr/lds base、wf_tag）一一吻合：主机写寄存器 → `axi4lite_2_cta` 翻译成 `host_req` → CTA 调度器选 SM 派发。

**③ `init_mem`：把 kernel 数据灌进 DDR** —— [naive_driver.h:112](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/FPGA_test/driver/naive_driver.h#L112)。`buf_num_soft`（buffer 数，来自 `metadata[27:26]`）个缓冲，每个按 16-beat INCR burst 用 `Xil_Out32` 写入 DDR：

```c
buf_num_soft = (metadata[27] << 32) | metadata[26];
for (int j=0; j<buf_num_soft; j++){
    uint64_t addr = buf_ba_w[j];
    for (int k=0; k<burst_times[j]; k++){
        uint64_t burst_data = (burst_len_mod[j]==0) ? 16 : ...;  // 每拍最多 16 字
        for (int l=0; l<burst_data; l++){ Xil_Out32(addr, data[m]); addr+=4; m++; }
        addr += 16*4 - burst_data*4;   // 跨到下一 burst 起点
    }
}
```

这与 u8-l1 testbench 里 `tc.v::init_mem` 用 `force` 灌 `axi_ram` 是同一思路的两种实现：仿真用 `force` 直驱从端口，上板用 Microblaze 经 AXI 真写 DDR——后者同时验证了写通路。

**④ metadata 决定派发规模** —— [naive_driver.c:59](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/FPGA_test/driver/naive_driver.c#L59) 调用 `assign_metadata_values(metadata)`。对照 [metadata.h:13](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/FPGA_test/driver/metadata.h#L13)（`metadata[10]=0x8` → `wf_size=8`）与 [metadata.h:15](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/FPGA_test/driver/metadata.h#L15)（`metadata[12]=0x4` → `wg_size=4`），本例派发 **4 个 warp、每 warp 8 线程**——所以 [define.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v) 里的 `NUM_THREAD` 必须与之一致（见 u1-l4「CASE 宏三合一」）。

#### 4.2.4 代码实践

**实践目标**：把驱动流程与 u7-l4/u8-l1 的知识串起来。

**操作步骤**（源码阅读型）：

1. 读 [naive_driver.c:75](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/FPGA_test/driver/naive_driver.c#L75) `init_mem` 调用，回答：kernel 的数据从哪里来（`data[]` 数组）、写到哪个地址段。
2. 读 [naive_driver.h:91](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/FPGA_test/driver/naive_driver.h#L91) `GpuSendTask`，数一数它写了几个寄存器、哪个是「触发位」。
3. 读 [naive_driver.c:94](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/FPGA_test/driver/naive_driver.c#L94) 的结果读取：从 `0x90002000` 连读 32 字，`process_data` 比对 `0x42000000`（IEEE-754 单精度即 32.0）判 pass/fail。

**需要观察的现象**：注意 [naive_driver.c:79](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/FPGA_test/driver/naive_driver.c#L79) 的 `while(retry)` 重试与 [naive_driver.c:95](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/FPGA_test/driver/naive_driver.c#L95) 直接读固定地址——当前驱动**并未轮询完成寄存器**，而是假定 GPU 已执行完。

**预期结果**：能画出 `Microblaze → AXI4-Lite(0x2000_xxxx) → axi4lite_2_cta → host_req → CTA` 的控制链，以及 `data[] → init_mem → DDR(0x8000_xxxx) → GPGPU m_axi 取指/取数` 的数据链。是否真正点灯需上板验证，标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`GpuSendTask` 为何要把 `GPU_VALID_OFFSET` 放在最后写？
**答**：前面写的参数（wg_id、wf_size、start_pc 等）要先就位，最后写 valid=1 才表示「参数已齐，可以派发」，对应 `axi4lite_2_cta` 的 `reg[0]=1` 触发 `host_req_valid`。若先写 valid 再写参数，派发会用到旧/残缺参数。

**练习 2**：`init_mem` 里的 burst 长度为何是 16？
**答**：AXI4 burst 的 `len` 字段为 8 位、最大 256，但常见 DDR 友好的 INCR burst 取 16（拍）。16 字 × 4 字节 = 64 字节，对齐 DDR4 的 burst 与 cache line，能提升写效率并简化地址跨越计算。

---

### 4.3 DC 综合与性能/面积指标

#### 4.3.1 概念说明

FPGA 实现回答「能不能在 Xilinx 芯片上跑」；ASIC **综合** 回答「如果流片，频率和面积大概多少」。Ventus 用 Synopsys **Design Compiler (DC)** 在 **tsmc 28nm** 工艺下做了综合评估，结果写在 [README.md](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/README.md) 的「综合」章节。注意：这是 **ASIC 指标，不是 FPGA 指标**。

#### 4.3.2 核心流程

DC 综合的关键输入是「工艺库 + RTL + 约束」：

1. 读入 RTL（同一份 `src/`），并启用 `T28_MEM`（见 4.4）把 SRAM 换成 28nm 编译器宏。
2. 设定工艺角（tsmc 28nm，只选 HVT/SVT 标准单元）与时钟约束。
3. DC 做逻辑综合、映射到标准单元 + SRAM 宏，报告面积与最高频率。

README 给出的配置与结果：

| 项 | 值 |
|----|----|
| 工艺 | tsmc 28nm |
| 标准单元 | 仅 HVT（高阈值）+ SVT（标准阈值）|
| `NUM_THREAD` | 32 |
| `NUM_SM` | 2 |
| `NUM_WARP` | 8 |
| `DCACHE_BLOCKWORDS` | 2 |
| 频率 | **620 MHz** |
| 总面积 | **3.908 mm²** |

`NUM_THREAD=32` 是综合规模（注意 [define.v:11](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L11) 默认是 `NUM_THREAD 4`，仅用于快速仿真）。频率与面积的近似关系可写作面积-时序权衡：

\[
\text{吞吐} \;\propto\; \frac{\text{NUM\_SM} \times \text{NUM\_WARP} \times \text{NUM\_THREAD}}{\text{周期/cycle}} \times f_{\max}
\]

提高 `NUM_THREAD` 增大并行度但也增大寄存器堆/流水线面积；只选 HVT/SVT（不用 LVT）牺牲少许速度换漏功耗，是典型的低功耗综合策略。

> 注意 README 的测试表注脚（[README.md:200](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/README.md#L200)）：`DCACHE_BLOCKWORDS` 较小时执行周期偏长；增大它能显著改善仿真周期数，但也会增大 cache 块、影响面积。

#### 4.3.3 源码精读

**① 综合配置与结果声明** —— [README.md:27](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/README.md#L27)：

> 我们针对GPGPU进行了DC综合（采用tsmc 28nm工艺）,以下是几个重要的配置参数：NUM_THREAD = 32 / NUM_SM = 2 / NUM_WARP = 8 / DCACHE_BLOCKWORDS = 2。在只采用HVT和SVT cell的条件下，GPGPU频率为 **620MHz**，总面积为 **3.908mm²**。

**② 默认规模参数** —— [define.v:5](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L5) `NUM_SM 2`、[define.v:9](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L9) `NUM_WARP 4'b1000`（=8）、[define.v:11](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L11) `NUM_THREAD 4`。综合时需把 `NUM_THREAD` 从默认 4 改成 32，其余已与综合配置基本一致。

#### 4.3.4 代码实践

**实践目标**：理解「同一份 RTL、不同配置 → 不同面积/频率」。

**操作步骤**（源码阅读型）：

1. 对照 [README.md:27-33](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/README.md#L27) 与 [define.v:5-13](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L5)，列出综合配置与默认配置的差异（关键就是 `NUM_THREAD 4 → 32`）。
2. 回顾 u1-l3 的派生关系：`NUM_LANE=NUM_THREAD`、`NUM_SFU=NUM_THREAD/4`，说明把 thread 改成 32 会让 vALU lane 数、SFU 物理核数都翻 8 倍——这就是面积的主要来源之一。
3. 思考：为何综合只取 HVT/SVT？

**需要观察的现象**：`NUM_THREAD` 牵动的不止是 ALU——寄存器堆每项宽度 = `32 × NUM_THREAD` 位（向量 bank），thread 翻倍使存储体面积线性增长。

**预期结果**：能解释「620MHz / 3.908mm²」是在 NUM_THREAD=32、纯 HVT/SVT 单元、含 SRAM 宏（T28_MEM）条件下的 ASIC 估算值。DC 综合需 license 与工艺库，标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：README 里的 620MHz 是 FPGA 的 fmax 吗？
**答**：不是。它是 tsmc 28nm ASIC 工艺下 DC 综合的估算频率。FPGA（VU37P）上的实际 fmax 取决于 Vivado 实现结果，通常远低于同工艺 ASIC 的频率，两者不可混用。

**练习 2**：为什么综合时把 `NUM_THREAD` 从 4 调到 32？
**答**：4 只是「快速跑仿真」的最小规模；真实产品级配置是 32。综合要在产品配置下评估才有意义，否则面积/频率会严重失真。

---

### 4.4 T28_MEM 宏：行为级 SRAM 与工艺编译器宏的切换

#### 4.4.1 概念说明

寄存器堆和 cache 的存储体在 RTL 里写成「行为级 RAM」（如 `dualportSRAM`）。这种写法在仿真里是普通数组，在 FPGA 综合时会被推断成 BRAM/触发器；但做 **ASIC 综合时**，晶圆厂要求存储体用指定的 **SRAM 编译器宏**（带具体型号、引脚名、时序的硬核），否则面积/功耗不可信、也无法流片。

`T28_MEM` 就是这个总开关：开启后，多处存储体从 `dualportSRAM` 切换为 28nm 工艺的 SRAM 宏。它与 4.3 直接相关——README 的 DC 综合正是「启用 T28_MEM」的 ASIC 流程。

#### 4.4.2 核心流程

宏开关在 [define.v:1](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L1)：

```verilog
//`define T28_MEM        // 默认注释掉 = 仿真/FPGA 模式
```

三种状态下它该怎么取值：

| 场景 | T28_MEM | 存储体实现 | 结果 |
|------|---------|-----------|------|
| VCS 仿真 | 关 | `dualportSRAM`（行为级数组） | 综合成 FF，行为正确 |
| FPGA 实现（Vivado） | 关 | `dualportSRAM` | 推断为 BRAM/UltraRAM |
| ASIC 综合（DC, 28nm） | **开** | `GPGPU_RF_2P_*` 等编译器宏 | 真实 SRAM，面积/频率可信 |

宏的两处启用方式：(1) 直接在 `define.v` 取消注释；(2) 仿真时在 [run.f](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/common/run.f#L13) 用 `+define+T28_MEM`（当前注释掉）。但注意：开启后需要配套的 SRAM 宏行为模型/库，否则仿真报「找不到模块」。

#### 4.4.3 源码精读

**① 标量寄存器堆 bank** —— [scalar_regfile_bank.v:140](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/operand_collector/scalar_regfile_bank.v#L140)：

```verilog
`ifdef T28_MEM  //256x32
  GPGPU_RF_2P_256X32M U_GPGPU_RF_2P_256X32M_0 ( .AA(rdidx_i), .D(rd_i), ... );  // 工艺宏
`else
  dualportSRAM #(.BITWIDTH(`XLEN), .DEPTH(`DEPTH_REGBANK)) U_dualportSRAM(...);  // 行为级
`endif
```

`GPGPU_RF_2P_256X32M` 是 256 深度 × 32 位、双端口（2P）的 SRAM 编译器宏，引脚名为 `AA/AB`（地址）、`D`（写数据）、`Q`（读数据）、`WEB/REB`（写/读使能，低有效）、`BWEB`（字节写掩码）。注释 `//256x32` 标明其容量。

**② 向量寄存器堆 bank** —— [vector_regfile_bank.v:324](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/operand_collector/vector_regfile_bank.v#L324)：

```verilog
`ifdef T28_MEM  //256X1024
  GPGPU_RF_2P_256X128M U_GPGPU_RF_2P_256X128M_0 ( .D(ram_mask[127:0]), ... .Q(rs_o[127:0]) );
`else
  ...
```

向量 bank 每项是 `32 × NUM_THREAD` 位。这里宏为 `GPGPU_RF_2P_256X128M`（256×128），对应 `NUM_THREAD=4` 时一项 128 位；当 `NUM_THREAD=32` 时向量存储体会用更宽/多片的宏组合（这是综合时需配套调整的部分）。

**③ D-cache 响应 FIFO** —— [l1_dcache.v:467](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/dcache/l1_dcache.v#L467)：

```verilog
`ifdef T28_MEM
  stream_fifo_dpsram_16X1060 #(.DATA_WIDTH(...), .FIFO_DEPTH(16)) ...
`else
  // 普通 stream_fifo（触发器实现）
```

cache 的 `core_rsp` 响应队列在 ASIC 模式下换成深度 16、宽 1060 位的双口 SRAM FIFO 宏（`stream_fifo_dpsram_16X1060`），以省面积。

**④ 仿真侧的配套：上 bank 路径与初始化** —— [test_gpu_top_hgx.sv:4](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_top/testbench/test_gpu_top_hgx.sv#L4) 在 `T28_MEM` 下定义了到 SRAM 宏内部 `mem` 的层次路径（如 `...U_vector_regfile_bank.U_GPGPU_RF_2P_256X128M_0.MX.mem`），用于用 `$readmemh`/`force` 直接初始化 SRAM 内容——这是带宏仿真时的特有需求。

#### 4.4.4 代码实践

**实践目标**：亲手确认宏开关带来的存储体替换。

**操作步骤**（源码阅读型）：

1. 在 `src/` 下搜索 `ifdef T28_MEM`，统计替换点（应至少含标量 bank、向量 bank、dcache FIFO）。
2. 对照 [scalar_regfile_bank.v:140-168](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/operand_collector/scalar_regfile_bank.v#L140) 的 `ifdef/else/endif` 两个分支，记录宏名、容量、关键引脚。
3. 思考：若在 FPGA 流程里误开 `T28_MEM` 会怎样？

**需要观察的现象**：开启 `T28_MEM` 后，工具链必须能找到 `GPGPU_RF_2P_256X32M` 等模块定义；这些宏由 28nm 工艺库提供，FPGA Vivado 不认识，故 FPGA 流程必须**关闭**该宏。

**预期结果**：能说清「关 = 仿真/FPGA（行为级 SRAM）；开 = ASIC 28nm（编译器宏）」，并理解这是 4.3 中 620MHz/3.908mm² 得以成立的前提。若尝试在 VCS 中加 `+define+T28_MEM` 而无配套宏模型，仿真会报 unresolved module，标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：为什么 FPGA 流程不能开 `T28_MEM`？
**答**：`T28_MEM` 把存储体换成 tsmc 28nm 的 SRAM 编译器宏（`GPGPU_RF_2P_*`），这些宏依赖 ASIC 工艺库，Xilinx Vivado 无法识别。FPGA 流程应关闭该宏，让 `dualportSRAM` 被推断成 FPGA 的 BRAM。

**练习 2**：宏名 `GPGPU_RF_2P_256X32M` 各段含义？
**答**：`GPGPU_RF` = 项目自定义前缀（寄存器堆）；`2P` = 双端口（一读一写或两读）；`256` = 深度 256；`X32` = 每字 32 位；`M` = memory（编译器宏）。命名直接暴露了 SRAM 的几何参数。

---

## 5. 综合实践

**任务**：把本讲四块内容串成「一次 kernel 在 FPGA 上从加载到执行再到结果回读」的完整叙事，并说清 `T28_MEM` 在其中的角色。

请完成以下梳理（纯源码阅读 + 画图，无需上板）：

1. **建工程**：阅读 [ventus_fpga.tcl:290](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/FPGA_test/ventus_fpga.tcl#L290)、[649](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/FPGA_test/ventus_fpga.tcl#L649)、[1525](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/FPGA_test/ventus_fpga.tcl#L1525)，画一张块设计拓扑：`Microblaze ↔ 外设互连 ↔ GPGPU(s_axilite)`、`GPGPU(m_axi)/CDMA ↔ SmartConnect ↔ DDR4`，标出 GPGPU 的模块名 `gpgpu_axi_adapter_top`。
2. **主机驱动**：沿 [naive_driver.c:12](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/FPGA_test/driver/naive_driver.c#L12) 的 main，写出三件事的代码位置——(a) 把 kernel 数据写进 DDR（`init_mem`）、(b) 写寄存器触发派发（`GpuSendTask` 末尾写 `GPU_VALID_OFFSET=1`）、(c) 读结果（`Xil_In32(0x90002000)` + `process_data`）。
3. **规模一致性**：对照 [metadata.h:13/15](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/FPGA_test/driver/metadata.h#L13)（wf_size=8、wg_size=4）与 [define.v:11](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L11)，说明上板前为何要把 `NUM_THREAD` 设成与 metadata 一致。
4. **T28_MEM 取舍**：回答——本 FPGA 流程应开还是关 `T28_MEM`？为什么？若改做 [README.md:27](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/README.md#L27) 的 28nm DC 综合，又该怎样设？分别在 [scalar_regfile_bank.v:140](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/operand_collector/scalar_regfile_bank.v#L140) 与 [l1_dcache.v:467](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/dcache/l1_dcache.v#L467) 处指出宏替换的具体模块名。

**交付物**：一张 SoC 拓扑图 + 一张「同一份 RTL 在仿真/FPGA/ASIC 三态下 T28_MEM 与存储体实现」的对照表。

## 6. 本讲小结

- Ventus GPGPU 的「落地」有三态：VCS 仿真（u1-l4）、FPGA 实现（`FPGA_test` + Vivado）、ASIC 综合评估（DC + tsmc 28nm）——同一份 `src/`，靠配置与宏切换。
- FPGA 系统靠 [ventus_fpga.tcl](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/FPGA_test/ventus_fpga.tcl) 重建：综合顶层是块设计 wrapper `config_mb_wrapper`，内含 Microblaze（主机）、DDR4（内存）、CDMA/SmartConnect（互连）、GPIO/UART（外设）和作为模块引用的 GPGPU `gpgpu_axi_adapter_top`。
- 上板后主机是真实软核，跑 [naive_driver.c](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/FPGA_test/driver/naive_driver.c)：`init_mem` 把 kernel 数据 burst 写入 DDR，`GpuSendTask` 写 AXI4-Lite 寄存器并在末拍写 valid=1 触发派发，与 u7-l4 的 `axi4lite_2_cta` 寄存器视图完全吻合。
- README 的 **620MHz / 3.908mm²** 是 tsmc 28nm、`NUM_THREAD=32`、仅 HVT/SVT 单元下的 **ASIC** 指标，不是 FPGA 频率。
- `T28_MEM` 宏控制存储体实现：关 = 行为级 `dualportSRAM`（仿真/FPGA→BRAM）；开 = 28nm SRAM 编译器宏（如标量 `GPGPU_RF_2P_256X32M`、向量 `GPGPU_RF_2P_256X128M`、dcache FIFO `stream_fifo_dpsram_16X1060`），是 ASIC 综合可信的前提。

## 7. 下一步学习建议

- **回看 u8-l1（testbench）**：把本讲的 `naive_driver.c` 与 u8-l1 的 `host_inter`/`tc.v::init_mem` 对照阅读，你会看到「同一套派发语义」在仿真 testbench 与上板 C 驱动里的两种实现，对理解 host→CTA 控制流大有裨益。
- **延伸到 u8-l4（指令集扩展）**：学会改 RTL 后，可用本讲的仿真/FPGA 流程把新指令跑起来、甚至上板验证。
- **进一步实践**：若手头有 VCU128 开发板与 Vivado，按 [FPGA_test/README.md:20-27](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/FPGA_test/README.md#L20) 实际生成比特流并在 SDK 中编译 `driver/` 工程，观察 LED 点灯；若关注 ASIC，可学习 DC 综合脚本（`.synopsys_dc.setup`、时钟约束 sdc），在启用 `T28_MEM` + 28nm 工艺库下复现频率/面积评估。
