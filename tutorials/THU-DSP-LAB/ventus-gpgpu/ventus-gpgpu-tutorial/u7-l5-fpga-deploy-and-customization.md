# FPGA 部署与参数定制

## 1. 本讲目标

本讲是整个学习手册的最后一篇，目标是把 Ventus（乘影）从「能仿真」推进到「能上 FPGA 板」。

学完后你应该能够：

1. 说清 `make fpga-verilog` 这条命令背后做的三件事，以及它和 `make verilog`（仿真用单文件）的根本区别。
2. 理解 `firtool --split-verilog --repl-seq-mem` 为什么要把存储器从 RTL 里「剥离」出来，写进 `mem.conf`，再由 `gen_sep_mem.sh` + `vlsi_mem_gen` 重新生成可替换的 SRAM 模型。
3. 读懂 `ventus/fpga_test/` 里的 Vivado 工程脚本：它如何用 MicroBlaze 当 host、把 `GPGPU_axi_adapter_top` 挂到 AXI 总线、用 DDR4 当显存，最终在 VU37P（VCU128）开发板上跑通一个 kernel。
4. 学会通过修改 `ventus/src/top/parameters.scala` 里的 `num_sm`/`num_warp`/缓存规模等旋钮来定制 GPU 规模，并定性预测资源（尤其是 BRAM）的变化趋势。

本讲承接 **u1-l2（构建系统与 Verilog 生成）** 和 **u2-l3（参数系统）**，并用到 **u7-l2（AXI 接口与 host 驱动）** 中关于 `AXI4Lite2CTA` 寄存器映射的知识。

---

## 2. 前置知识

- **仿真（Simulation）vs 综合（Synthesis）**：仿真是用软件（Verilator/VCS）逐拍推演电路行为，关心「对不对」；综合是用工具（Vivado）把 RTL 映射到 FPGA 上的真实单元（LUT/FF/BRAM/DSP），关心「能不能放下、跑多快」。仿真用的 Verilog 可以是一个几万行的巨型单文件；综合则更希望模块化、存储器独立，以便工具识别和替换。
- **BRAM 与 SRAM 宏单元**：FPGA 片内有固定的块状 RAM 资源（Xilinx 称 BRAM）。但 RTL 里写的 `reg [31:0] mem [0:1023]` 不一定都能被工具识别成 BRAM；为保证确定性，业界常把存储器显式「黑盒化」成一个个独立的 SRAM 模块，再由综合工具把每个黑盒映射到 BRAM（FPGA）或厂商 SRAM 编译器（ASIC）。
- **Vivado Block Design（BD）与 Tcl**：BD 是图形化拼接 IP 的方式；Vivado 可以把整个 BD 导出成一条 Tcl 脚本，`source` 这条脚本就能在命令行里完整重建工程，非常适合版本管理。本讲的 `project_gpgpu.tcl` 就是这样的脚本。
- **AXI4 / AXI4-Lite**：Xilinx 生态的标准总线。AXI4-Lite 是轻量控制面（寄存器读写），AXI4 是宽数据面（突发传输）。详见 u7-l2。
- **Chisel → FIRRTL → Verilog 链路**：回顾 u1-l2，Ventus 的 Scala 源码先经 Chisel 生成 FIRRTL 中间表示（`.fir`/CHIRRTL），再由 CIRCT 的 `firtool` lowering 成 Verilog。本讲的存储器分离就发生在 `firtool` 这一步。

> 本讲用到的关键术语：**综合（synthesis）/ 实现（implementation）/ 比特流（bitstream）**、**SRAM 宏 / BRAM**、**`mem.conf`**、**`--repl-seq-mem`**、**Block Design / MicroBlaze**、**VU37P / VCU128**。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [`Makefile`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/Makefile) | `fpga-verilog` 目标的定义，串起三步命令 |
| [`scripts/gen_sep_mem.sh`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/scripts/gen_sep_mem.sh) | 逐行读 `mem.conf`，为每个存储器调用生成器 |
| [`scripts/vlsi_mem_gen`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/scripts/vlsi_mem_gen) | Python 脚本（源自 SiFive/rocket-chip），按 conf 生成 SRAM Verilog |
| [`ventus/src/top/GPGPU_top.scala`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala) | 定义 `GPGPU_axi_top` 与 FPGA 综合顶层 `GPGPU_axi_adapter_top` |
| [`ventus/src/top/parameters.scala`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/parameters.scala) | GPU 规模与各种位宽/容量的单一事实来源 |
| [`ventus/fpga_test/project_gpgpu.tcl`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/fpga_test/project_gpgpu.tcl) | Vivado 工程重建脚本（器件、BD、综合/实现 run） |
| [`ventus/fpga_test/scrs/bd/config_mb_wrapper.v`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/fpga_test/scrs/bd/config_mb_wrapper.v) | BD 顶层 wrapper，对外暴露 DDR4/UART/时钟/复位等物理端口 |
| [`ventus/fpga_test/scrs/driver/naive_driver.c`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/fpga_test/scrs/driver/naive_driver.c) | 跑在 MicroBlaze 上的最小 host 驱动示例 |
| [`ventus/fpga_test/scrs/driver/naive_driver.h`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/fpga_test/scrs/driver/naive_driver.h) | 驱动的寄存器偏移定义（注意：已部分过时，见后文） |
| [`ventus/fpga_test/readme.md`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/fpga_test/readme.md) | 工程目录说明与建工程步骤 |

---

## 4. 核心概念与源码讲解

### 4.1 GPGPU_axi_adapter_top：FPGA 综合顶层

#### 4.1.1 概念说明

在 u1-l2 里我们见过两个 Verilog 生成入口：

- `make verilog` → 顶层 `top.GPGPU_gen` → elaborates `GPGPU_top`，产出仿真用单文件 `GPGPU_top.v`。它只有 `host_req/host_rsp` 与 `out_a/out_d` 这种 TileLink 风格端口，**不含 AXI**，外挂内存模型由 `GPGPU_SimTop` 提供。
- `make fpga-verilog` → 顶层 `top.GPGPU_axi_adapter_top`，是 **FPGA 综合专用** 的顶层。

为什么 FPGA 要单独一个顶层？因为真实板卡上 GPU 必须用标准总线（AXI）和外部世界打交道：一条 AXI4-Lite 从口接收 host 写来的 kernel 参数（控制面），一条 AXI4 主口访问 DDR4 显存（数据面）。回顾 u7-l2，`GPGPU_axi_top` 已经把 `GPGPU_top` + `AXI4Lite2CTA` + `AXI4Adapter` 拼成了这样一个带 AXI 的 IP。

那 `GPGPU_axi_adapter_top` 又是什么？它是对 `GPGPU_axi_top` 的一层**极薄的包装**，唯一目的是换一组更省资源的 AXI id 位宽参数，供 FPGA 综合使用。

#### 4.1.2 核心流程

两者的包装关系是嵌套的：

```text
GPGPU_axi_adapter_top          ← make fpga-verilog 的综合顶层（idBits 更省）
        └── GPGPU_axi_top      ← 已经把 GPGPU + 两个 AXI 适配器拼好
                ├── GPGPU_top                  （GPU 内核：CTA 调度 + SM 集群 + L2）
                ├── AXI4Lite2CTA               （AXI4-Lite 从口 → host2CTA 寄存器）
                └── AXI4Adapter                （L2 TileLink ↔ AXI4 主口 → DDR4）
```

端口上，`GPGPU_axi_adapter_top` 对外只有两路 AXI：

- `s`：`Flipped(new AXI4Lite(32, 32))` —— AXI4-Lite **从口**（32 位地址/数据），host 经它派发 kernel。
- `m`：`new AXI4Bundle(...)` —— AXI4 **主口**（32 位地址、64 位数据），访问外存。

#### 4.1.3 源码精读

先看内层的 `GPGPU_axi_top`，它用「满」id 位宽：

[ventus/src/top/GPGPU_top.scala:L116-L137](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L116-L137) —— `GPGPU_axi_top` 直接例化 `GPGPU_top` 与两个 AXI 适配器并连线；其中 `l2cache_axi_params` 的 id 位宽取 `l2cache_params.source_bits`（=16，见 u6-l5）。

再看外层的 `GPGPU_axi_adapter_top`，它把 id 位宽压窄：

[ventus/src/top/GPGPU_top.scala:L138-L148](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L138-L148) —— 综合顶层重新定义 `l2cache_axi_params`，id 位宽改成 `log2Up(num_sm)+log2Up(num_warp)+1`，然后例化 `GPGPU_axi_top` 并直连 `io.s`/`io.m`。

关键差异在 id 位宽：

- `GPGPU_axi_top`：`AXI4BundleParameters(32, 64, l2cache_params.source_bits)` —— idBits = `source_bits` = **16**。
- `GPGPU_axi_adapter_top`：`AXI4BundleParameters(32, 64, log2Up(num_sm)+log2Up(num_warp)+1)` —— 默认 `num_sm=2, num_warp=8`，故 idBits = 1 + 3 + 1 = **5**。

> AXI 的 id 位宽直接决定 interconnect 里 FIFO/比较器的宽度。从 16 位压到 5 位，对 FPGA 资源和时序都有好处。这正是 u7-l2 里「`GPGPU_axi_adapter_top` 是 FPGA 综合用的薄包装（idBits 更省资源）」的代码出处。功能上两者完全等价，只是 id 字段更窄——而 `AXI4Adapter` 用 TL `source` 当 AXI `id` 做回程配对（见 u7-l2），5 位足以覆盖默认规模下的在途请求数。

#### 4.1.4 代码实践

**实践目标**：直观对比两个顶层在 AXI id 位宽上的差异。

**操作步骤**：

1. 打开 [GPGPU_top.scala:L116-L148](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L116-L148)。
2. 手算默认配置下两者的 idBits：`source_bits` 来自 [parameters.scala:L111](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/parameters.scala#L111) 的 `3 + log2Up(dcache_MshrEntry) + log2Up(dcache_NSets)` = 3 + 2 + 8 = **13**（注意：`source_bits` 在 L2 参数里还会被向上取整/补齐，最终为 16，待本地确认具体推导）。
3. 对比 `GPGPU_axi_adapter_top` 的 `log2Up(2)+log2Up(8)+1 = 5`。

**需要观察的现象**：两个类同名变量 `l2cache_axi_params` 取了不同的第三个参数（idBits）。

**预期结果**：`GPGPU_axi_adapter_top` 的 AXI id 比 `GPGPU_axi_top` 窄得多，因此被选作 FPGA 综合顶层。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `num_warp` 从 8 改成 16，`GPGPU_axi_adapter_top` 的 idBits 会变成多少？

**参考答案**：`log2Up(2) + log2Up(16) + 1 = 1 + 4 + 1 = 6`。

**练习 2**：为什么不在仿真（`GPGPU_SimTop`）路径里也用窄 idBits？

**参考答案**：仿真路径用的是 L2 cache 的完整 `source_bits` 来做 MSHR/请求配对（见 u6-l5 的 source 路由），且仿真不关心资源占用；窄 id 是为了综合省资源而做的折中，只在 FPGA 顶层才需要。

---

### 4.2 make fpga-verilog 三步流水与 mem.conf

#### 4.2.1 概念说明

这是本讲最核心的一节。问题先放着：一个 GPGPU 里有成百上千个存储器（寄存器堆、ICache/DCache 的 tag/data 阵列、SharedMemory 的 bank、MSHR 表项、ibuffer……）。在仿真里它们都是 `reg` 数组，揉在一个大 `.v` 文件里无所谓；但在 FPGA 综合时，我们希望：

1. 每个存储器是一个**独立的模块**，便于 Vivado 把它识别/替换成 BRAM；
2. 列一张**清单**（`mem.conf`），记录每个存储器的名字、位宽、深度、端口类型，便于后续（ASIC 流程时）替换成厂商 SRAM 编译器生成的宏。

`firtool` 的 `--repl-seq-mem`（replace sequential memory）选项就是干这件事的：它把 FIRRTL 里所有的 `mem`（ sequential memory，对应 Chisel 里的 `SyncReadMem` / Ventus 的 `SRAMTemplate`）抽出来变成黑盒模块，并把它们的「规格」写到 `--repl-seq-mem-file` 指定的 `mem.conf` 里。

> Ventus 的片上存储器几乎全部用 `ventus/src/SRAMTemplate/` 下的 `SRAMTemplate`（如 `SRAM1R1W`/`SRAM1RW` 等）封装，底层是 Chisel `SyncReadMem`。它们 lowering 后就是 FIRRTL `mem`，正是 `--repl-seq-mem` 抽取的对象。参见 [SRAMTemplate.scala](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/SRAMTemplate/SRAMTemplate.scala)。

#### 4.2.2 核心流程

`make fpga-verilog` 实际上是**三条命令的串联**，完整流程如下：

```text
① Chisel → CHIRRTL
   ./mill ventus[6.4.0].runMain circt.stage.ChiselMain \
       --module top.GPGPU_axi_adapter_top --target chirrtl \
       --target-dir gen_fpga_verilog/
   产出: gen_fpga_verilog/GPGPU_axi_adapter_top.fir   (CHIRRTL 中间表示)

② firtool lowering + 分拆 + 抽存储器
   cd gen_fpga_verilog/
   firtool --split-verilog --repl-seq-mem \
           --repl-seq-mem-file=mem.conf -o . GPGPU_axi_adapter_top.fir
   产出: 一堆 .v (按模块分文件)  +  mem.conf (所有存储器规格清单)  +  若干 <mem>.v 黑盒

③ 为每个存储器生成行为级 SRAM 模型
   ./scripts/gen_sep_mem.sh ./scripts/vlsi_mem_gen \
       gen_fpga_verilog/mem.conf gen_fpga_verilog/
   产出: gen_fpga_verilog/<每个存储器名>.v  (可综合的 reg 阵列实现)
```

`mem.conf` 的每一行描述一个存储器，采用「键 值」成对的格式，由 [`vlsi_mem_gen` 的 `parse_line`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/scripts/vlsi_mem_gen#L145-L168) 解析，可识别的键有 `name`、`width`、`depth`、`ports`、`mask_gran`。典型一行长这样（**示例**，具体内容由实际 elaborate 决定）：

```text
name icache_data_0 width 32 depth 1024 ports read,write mask_gran 8
```

- `ports` 是逗号分隔的端口类型：`read`（读口）、`write`（写口）、`rw`（读写口）；前缀加 `m` 表示带字节掩码（`mwrite`/`mrw`）。
- `mask_gran` 是字节掩码粒度，`width // mask_gran` 得到掩码段数（mask_seg）。

`gen_sep_mem.sh` 的逻辑非常朴素：逐行读 `mem.conf`，用正则抠出 `name` 后面的模块名，把这一行单独写进 `.tmp`，再调用 `vlsi_mem_gen` 生成同名 `.v`。

#### 4.2.3 源码精读

**Makefile 的 `fpga-verilog` 目标**，三条命令清清楚楚：

[Makefile:L28-L31](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/Makefile#L28-L31) —— 注意第一行的入口模块是 `top.GPGPU_axi_adapter_top`（不是 `GPGPU_gen`），这正是上一节讲的省 idBits 的综合顶层。

**gen_sep_mem.sh 全文**（只有十几行）：

[scripts/gen_sep_mem.sh:L1-L14](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/scripts/gen_sep_mem.sh#L1-L14) —— 关键是第 9 行用 `grep -oP '(?<=name )[^ ]*(?= .*)'` 从一行 conf 里提取存储器模块名；第 11 行对每个存储器单独调用 `vlsi_mem_gen`，输出 `${output_dir}/${file}.v`。

**vlsi_mem_gen 的入口**：

[scripts/vlsi_mem_gen:L424-L436](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/scripts/vlsi_mem_gen#L424-L436) —— `main()` 逐行读 conf，每行 new 一个 `SRAM(line)` 对象并 `generate()` 出 Verilog；命令行参数见 [L439-L452](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/scripts/vlsi_mem_gen#L439-L452)，支持 `--tsmc28`（插 ASIC SRAM 库）和 `--blackbox`/`-b`（只出空壳黑盒）。

**conf 解析**：

[scripts/vlsi_mem_gen:L145-L168](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/scripts/vlsi_mem_gen#L145-L168) —— 把一行解析成 `(name, width, depth, mask_gran, mask_seg, ports)` 六元组；端口类型在 [L182-L218](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/scripts/vlsi_mem_gen#L182-L218) 决定每个端口的信号（`R0_addr/R0_data`、`W0_addr/W0_data/W0_mask`、`RW0_wmode/RW0_rdata` 等）。

> 一个重要含义：第 ③ 步默认生成的是**行为级 `reg` 阵列**（不是黑盒），所以这套流程产出的 RTL 自己就能被 Vivado 综合成 BRAM，无需额外 SRAM 库。若走 ASIC 流程，则用 `--tsmc28` 或 `--blackbox` 生成空壳，再替换成代工厂的 SRAM 编译器输出。本讲聚焦 FPGA，所以用默认行为级即可。

#### 4.2.4 代码实践

**实践目标**：亲眼看到 `mem.conf` 的格式和被分离出来的 SRAM 文件。

**操作步骤**：

1. 确保环境就绪：需要 `./mill`、`firtool`（CIRCT）在 PATH 上，且已 `make init` 拉取子模块（见 u1-l2）。`firtool` 版本需与 Chisel 6.4.0 匹配，缺失会报 `command not found`。
2. 在仓库根目录执行：
   ```bash
   make fpga-verilog
   ```
3. 进入 `gen_fpga_verilog/` 目录，查看：
   ```bash
   ls gen_fpga_verilog/ | head
   cat gen_fpga_verilog/mem.conf | head -5
   wc -l gen_fpga_verilog/mem.conf          # 存储器总数（行数）
   ```
4. 任意挑一个生成的 SRAM 文件，例如某个 `*_data*.v`，打开看它的端口。

**需要观察的现象**：

- `gen_fpga_verilog/` 下应有一堆按模块拆分的 `.v`，其中顶层叫 `GPGPU_axi_adapter_top.v`。
- `mem.conf` 每行一个存储器，字段是 `name ... width ... depth ... ports ...`。
- 每个 conf 行对应一个独立生成的 `.v`，端口名符合 `R0_*/W0_*/RW0_*` 规约。

**预期结果**：

- 工程能完整 elaborate 出 GPGPU_axi_adapter_top 的 RTL，`mem.conf` 行数即被分离的存储器实例数。
- 若 `firtool` 未安装或版本不符，第一步或第二步会失败——这是最常见的坑。

> **待本地验证**：`mem.conf` 的确切行数与每个存储器的 width/depth 取决于默认参数下的 elaborate 结果，本讲不臆造具体数字。请在实际机器上跑一次后记录。本机环境若无 `firtool`，则整条流程无法运行，可改为纯阅读型实践（见 4.2.5）。

#### 4.2.5 小练习与答案

**练习 1**：如果一条 conf 行是 `name foo width 64 depth 256 ports mrw mask_gran 8`，它描述的是什么样的存储器？

**参考答案**：名为 `foo`，数据 64 位、256 深度的 1 读写口（`rw`）存储器，带字节掩码（`m` 前缀），掩码粒度 8 位，故掩码段数 = 64/8 = 8（即 8 位写掩码 `W0_mask`）。

**练习 2**：为什么 `gen_sep_mem.sh` 要把每一行单独写成 `.tmp` 再调用 `vlsi_mem_gen`，而不是一次性喂整个 conf？

**参考答案**：因为 `vlsi_mem_gen` 的 `main()` 会对 conf 文件里**每一行**都生成一个完整 module 并写到同一个输出；而这里我们希望每个存储器各自落盘成**独立 `.v` 文件**（`-o ${output_dir}/${file}.v`）。所以逐行喂数据、逐行指定不同输出文件名，是拆分文件落盘的需要。

**练习 3（阅读型）**：不运行命令，仅根据 [gen_sep_mem.sh:L9](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/scripts/gen_sep_mem.sh#L9) 的正则 `(?<=name )[^ ]*(?= .*)`，说明它对一行 `name icache_data width 32 ...` 会提取出什么字符串。

**参考答案**：提取 `name ` 之后、下一个空格之前的非空串，即 `icache_data`，作为输出文件名 `icache_data.v`。

---

### 4.3 fpga_test Vivado 工程与上板验证

#### 4.3.1 概念说明

生成 RTL 只是第一步，要上板还需要一个完整的 FPGA 工程。`ventus/fpga_test/` 提供了一个基于 **Vivado Block Design** 的参考设计，思路是：

- 用一个 **MicroBlaze** 软核当 host CPU（代替仿真里的 mini driver），在板载 SDK 里跑 C 程序。
- MicroBlaze 经 AXI4-Lite 写 `GPGPU_axi_adapter_top` 的控制寄存器来派发 kernel（控制面）。
- `GPGPU_axi_adapter_top` 的 AXI4 主口经 SmartConnect 接到 **DDR4 MIG**，把 DDR4 当显存（数据面）。
- 用 `axi_cdma` 把 kernel 的指令/数据从 MicroBlaze 搬到 DDR4；用 `axi_uartlite` 打印调试；用 `system_ila` 抓波形。

这样 GPU 就被包装成了一个标准的 AXI IP，挂在一个小型 SoC 里。

#### 4.3.2 核心流程

完整上板流程：

```text
1. make fpga-verilog                       # 得到 GPGPU_axi_adapter.v 等综合用 RTL
2. 把生成的 Verilog 拷到 fpga_test/scrs/gpgpu_fpga_test/
3. 在 Vivado Tcl 控制台: source project_gpgpu.tcl   # 重建工程 + BD + 综合/实现 run
4. (可选) 跑 synth_1 / impl_1，生成 bitstream
5. Program Device，下载比特流
6. Export Hardware → SDK，新建 C 工程，导入 scrs/driver/ 下的 naive_driver.c/.h
7. 在 SDK 里编译、运行，观察 UART 输出
```

`project_gpgpu.tcl` 是一条 Vivado 导出的脚本，干了几件关键事：

- 选定器件：`xcvu37p-fsvh2892-2L-e`（Xilinx Virtex UltraScale+ VU37P），板卡 `vcu128`。
- 把 RTL 文件加进 `sources_1`，设定顶层为 `config_mb_wrapper`。
- 用 `cr_bd_config_mb` 这段 Tcl 过程**程序化重建**整个 Block Design，里面例化了 MicroBlaze、DDR4、SmartConnect、cdma、gpio、uartlite、ila，以及 `GPGPU_axi_adapter_top_0`。
- 配好地址映射，建好 `synth_1`/`impl_1` 两个 run。

#### 4.3.3 源码精读

**器件选型与工程创建**：

[project_gpgpu.tcl:L105-L112](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/fpga_test/project_gpgpu.tcl#L105-L112) —— 器件 `xcvu37p-fsvh2892-2L-e`，板卡 `xilinx.com:vcu128:part0:1.0`。顶层在 [L253](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/fpga_test/project_gpgpu.tcl#L253) 设为 `config_mb_wrapper`。

**BD 里对 GPGPU 模块的检查**：

[project_gpgpu.tcl:L434-L435](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/fpga_test/project_gpgpu.tcl#L434-L435) —— BD 会校验 `GPGPU_axi_adapter_top` 这个模块能否在 sources 里 resolve，这说明 BD 期望的 RTL 顶层正是 `GPGPU_axi_adapter_top`（与 `make fpga-verilog` 一致）。

**GPGPU 在 BD 中的例化**：

[project_gpgpu.tcl:L590-L599](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/fpga_test/project_gpgpu.tcl#L590-L599) —— 以 `-reference GPGPU_axi_adapter_top` 的方式例化成 `GPGPU_axi_adapter_top_0`。

**两条关键总线连接**：

- 数据面：[project_gpgpu.tcl:L766](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/fpga_test/project_gpgpu.tcl#L766) —— `GPGPU_axi_adapter_top_0/m_axi` → `axi_smc/S04_AXI`（SmartConnect 第 4 个从口），最终到 DDR4。
- 控制面：[project_gpgpu.tcl:L788](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/fpga_test/project_gpgpu.tcl#L788) —— MicroBlaze 经 `microblaze_0_axi_periph/M05_AXI` → `GPGPU_axi_adapter_top_0/s_axi_lite`。

**地址映射**：

[project_gpgpu.tcl:L804-L806](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/fpga_test/project_gpgpu.tcl#L804-L806) —— GPGPU 主口 `m_axi` 映射到 DDR4 `0x80000000` 起 2GB（显存）；MicroBlaze 数据总线上 GPGPU 从口 `s_axi_lite/reg0` 映射在 `0x20000000` 起 512MB（控制寄存器窗口）。

> 这两张地址表正好呼应 4.1 节的两路 AXI：`s_axi_lite` 是控制面、`m_axi` 是数据面。

**BD 顶层对外端口**：

[config_mb_wrapper.v:L12-L52](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/fpga_test/scrs/bd/config_mb_wrapper.v#L12-L52) —— 顶层对外暴露 DDR4 物理引脚（`ddr4_sdram_*`）、差分时钟（`default_100mhz_clk_clk_n/p`）、复位、UART、LED——这些都是 VCU128 板上的真实物理接口。

**MicroBlaze 上的最小 host 驱动**：

[naive_driver.c:L3-L40](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/fpga_test/scrs/driver/naive_driver.c#L3-L40) —— `main()` 的流程是：`GpuInit` 初始化 → `GpuTaskMemoryInit` 把指令/数据载入内存 → `GpuSendTask` 写参数寄存器并触发派发 → `GpuWatchTask` 轮询完成 → `GpuDeleteTask` 收尾。

[naive_driver.c:L96-L125](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/fpga_test/scrs/driver/naive_driver.c#L96-L125) —— `GpuSendTask` 逐个写 `WG_ID/NUM_WF/WF_SIZE/START_PC/...` 寄存器，最后写 `GPU_VALID_OFFSET=1` 触发硬件派发，然后轮询该位被硬件清零（即 u7-l2 讲的「写 1 触发，硬件发完自动清零」）。`GpuWatchTask` 则轮询 `GPU_WG_VALID_OFFSET=1`，读回完成的 `WG_ID`，再写 0 重新使能完成中断/标志。

> ⚠️ **重要提醒**：[naive_driver.h:L9-L24](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/fpga_test/scrs/driver/naive_driver.h#L9-L24) 的寄存器偏移表（如 `GPU_VALID_OFFSET=0x00`、`GPU_WG_ID_OFFSET=0x04` 等）**与当前 [`AXI4Lite2CTA`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/axi/AXI4Lite2CTA.scala) 的实际寄存器布局已不一致**（u7-l2 已指出 `naive_driver.h` 过时）。同样，`fpga_test/readme.md` 提到「拷贝 `GPGPU_axi_top.v`」，但实际综合顶层是 `GPGPU_axi_adapter_top`、对应文件 `GPGPU_axi_adapter.v`。**以源码（AXI4Lite2CTA.scala、project_gpgpu.tcl、Makefile）为准，文档/驱动头存在滞后。**

#### 4.3.4 代码实践

**实践目标**：理清 SoC 数据通路与地址映射（源码阅读型，无需上板）。

**操作步骤**：

1. 在 [project_gpgpu.tcl](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/fpga_test/project_gpgpu.tcl) 里定位 L766（数据面）与 L788（控制面）两条连接，以及 L804–L806 的地址段。
2. 画一张框图：MicroBlaze → axi_periph(M05) → `GPGPU.s_axi_lite`（控制）；`GPGPU.m_axi` → SmartConnect(S04) → DDR4 @0x80000000（数据）；MicroBlaze → axi_cdma → DDR4（搬运指令/数据）。
3. 对照 [naive_driver.c:L96-L125](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/fpga_test/scrs/driver/naive_driver.c#L96-L125) 的 `GpuSendTask`，标注每条 `Gpu_WriteReg` 写进的是 MicroBlaze 视角的 `0x20000000 + offset` 地址。

**需要观察的现象**：GPGPU 在地址空间里既是「从设备」（控制面 0x20000000）又是「主设备」（数据面 0x80000000）。

**预期结果**：你应当能用一句话描述「kernel 参数怎么进 GPU、GPU 怎么访问显存」：MicroBlaze 写 0x20000000 区间的寄存器派发 kernel，GPU 经 0x80000000 的 DDR4 取指令和数据。

**待本地验证**：若有 VCU128 实板且已装 Vivado 2019.1，可 `source project_gpgpu.tcl` 后跑 synth_1/impl_1 验证能否通过；无板则止步于源码阅读。

#### 4.3.5 小练习与答案

**练习 1**：为什么 GPGPU 的 `m_axi` 和 MicroBlaze 的数据口都映射到同一个 DDR4 地址段（0x80000000）？

**参考答案**：因为它们共享同一片物理 DDR4 显存。MicroBlaze（经 cdma）先把 kernel 指令/数据写到 DDR4 的某地址，GPGPU 的 `m_axi` 随后从同一地址取指/取数——双方必须用相同的物理地址才能“看到”同一块数据。

**练习 2**：`config_mb_wrapper` 的顶层端口里为什么有 `ddr4_sdram_dq` 这种 `inout`？

**参考答案**：它们是直接连到 VCU128 板上 DDR4 颗粒的物理引脚，由 DDR4 MIG IP 驱动；`dq` 是双向数据线，所以是 `inout`。这是 FPGA 顶层和真实 PHY 对接的必然形态。

**练习 3**：`naive_driver.c` 里 `GpuSendTask` 最后写 `GPU_VALID_OFFSET=1` 后立刻轮询读它为 0，这对应 u7-l2 里讲的什么机制？

**参考答案**：对应 `AXI4Lite2CTA` 的「写 `regs(0)=1` 触发一次性派发，硬件发完自动清零」机制；host 轮询到 0 即表示这次派发已被硬件接收。

---

### 4.4 参数定制与资源对比

#### 4.4.1 概念说明

上板之前或换板时，常需要根据 FPGA 资源调整 GPU 规模（比如一块小板放不下默认配置，就得缩小）。Ventus 的几乎所有规模都集中在一个全局单例 [`object parameters`](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/parameters.scala) 里（详见 u2-l3），改这几个常量就能改变整个芯片面积，尤其是 **BRAM 用量**（因为寄存器堆、cache 阵列、sharedmem 都是大块 SRAM）。

#### 4.4.2 核心流程

定制流程：

```text
1. 编辑 ventus/src/top/parameters.scala，改目标参数（如 num_warp、num_thread、缓存规模）
2. make fpga-verilog                       # 重新生成 RTL
3. （可选）观察 gen_fpga_verilog/mem.conf 的变化：行数、width、depth
4. 在 Vivado 里综合，对比 Utilization Report 里的 BRAM/LUT/REG/DSP 数量
```

关键直觉——哪些参数最「贵」：

- **`num_warp`（默认 8）**：寄存器堆几乎线性增长。`num_vgpr = 128*num_warp`、`num_sgpr = 256*num_warp`（[parameters.scala:L20-L21](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/parameters.scala#L20-L21)），所以 `num_warp` 翻倍意味着向量/标量寄存器堆容量翻倍，是 BRAM 占用的大头。
- **`num_thread`（默认 32）**：决定 warp 宽度与 lane 数（`num_lane = num_thread`，[L57](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/parameters.scala#L57)）、SFU 数（`num_sfu = num_thread>>2`，[L91](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/parameters.scala#L91)）；改它会显著影响向量数据通路宽度与 DSP 用量。
- **`num_sm`（默认 2）**：SM 数量，整体面积近似线性增长（[L7](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/parameters.scala#L7)）。
- **缓存规模**：`dcache_NSets/NWays/BlockWords`（[L71-L75](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/parameters.scala#L71-L75)）、`l2cache_NSets/NWays`（[L99-L100](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/parameters.scala#L99-L100)）、`sharedmem_depth`（[L93](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/parameters.scala#L93)）直接决定 cache 阵列和 sharedmem bank 的总位数。

粗略的资源估算可写成（仅说明趋势，非精确公式）：

\[
\text{BRAM}_{\text{regfile}} \;\propto\; \text{num\_sm} \times \big(\text{num\_vgpr} + \text{num\_sgpr}\big) \;=\; \text{num\_sm}\times\text{num\_warp}\times(128+256)
\]

\[
\text{BRAM}_{\text{cache}} \;\propto\; \text{num\_sm}\times\big(\text{dcache\_NSets}\times\text{dcache\_NWays} + \text{sharedmem\_depth}\big) + \text{l2cache\_NSets}\times\text{l2cache\_NWays}
\]

#### 4.4.3 源码精读

核心规模旋钮集中在文件开头：

[parameters.scala:L7-L9](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/parameters.scala#L7-L9) —— `num_sm = 2`、`num_warp = 8`（`var`）、`num_thread = 32`（`var`）。

[parameters.scala:L20-L21](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/parameters.scala#L20-L21) —— `num_vgpr = 128*num_warp`、`num_sgpr = 256*num_warp`：寄存器堆随 `num_warp` 线性扩张。

[parameters.scala:L91-L97](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/parameters.scala#L91-L97) —— `num_sfu = num_thread>>2`；`sharedmem_depth = 1024`，`sharemem_size = sharedmem_depth * sharedmem_BlockWords * 4`（默认 128 KiB）。

[parameters.scala:L71-L75](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/parameters.scala#L71-L75) 与 [L99-L100](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/parameters.scala#L99-L100) —— DCache 与 L2 的组数/相联度，决定 cache 阵列大小。

> 还有两点工程注意（承接 u2-l3）：
> 1. `num_warp`/`num_thread` 被声明为 `var`，主要是为旧 chiseltest 路径按 metadata 运行时改写；但 `make fpga-verilog` 走的是 elaborate 静态路径，直接改源码常量即可。
> 2. 改完参数后，可用 [ParamPrintApp](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/parameters.scala#L336-L339)（`./mill ventus[6.4.0].runMain top.ParamPrintApp`）把参数导出成 `parameters.json` 做前后对比，避免漏改派生量。

#### 4.4.4 代码实践

**实践目标**：观察 `num_warp` 变化对存储器清单的影响。

**操作步骤**：

1. 先用默认参数跑一次 `make fpga-verilog`，保存基线：
   ```bash
   cp gen_fpga_verilog/mem.conf /tmp/mem_default.conf
   wc -l /tmp/mem_default.conf
   ```
2. 编辑 [parameters.scala:L8](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/parameters.scala#L8)，把 `var num_warp = 8` 改成 `var num_warp = 4`。
3. 重新 `make fpga-verilog`，再统计：
   ```bash
   wc -l gen_fpga_verilog/mem.conf
   diff /tmp/mem_default.conf gen_fpga_verilog/mem.conf | head -40
   ```
4. 重点找名字里含 `gpr`/`regfile`/`RegFile` 之类的存储器行，对比它们的 `depth` 变化。
5. （可选）把 `num_warp` 改回 8，以免影响后续实验。

**需要观察的现象**：

- 寄存器堆类存储器的 `depth` 应随 `num_warp` 减半而下降（因为 `num_vgpr`/`num_sgpr` 减半）。
- 总存储器实例数（`mem.conf` 行数）可能也会减少（每 warp 一份的表项变少）。
- cache 阵列行（不依赖 `num_warp` 的 set/way）应基本不变。

**预期结果**：`num_warp` 4 对比 8，寄存器堆容量减半 → 预计 BRAM 占用明显下降，LUT/REG 也随之减少。

**待本地验证**：具体的 `mem.conf` 行数、每个存储器的 depth、以及 Vivado 综合后的 BRAM 块数，需在本地实跑后记录。本环境无法运行 `firtool`/Vivado，故只给定性预测与操作步骤。

#### 4.4.5 小练习与答案

**练习 1**：把 `num_sm` 从 2 改成 4，整体 BRAM 大致如何变化？

**参考答案**：SM 数翻倍，每个 SM 自带的寄存器堆、ICache/DCache、SharedMemory 都翻倍，BRAM 近似翻倍；L2 部分不变（`num_l2cache=1`），故总体 BRAM 约为原来的不到 2 倍（L2 那部分被摊薄）。

**练习 2**：想把 sharedmem 从 128 KiB 缩到 64 KiB，应改哪个参数、改成多少？

**参考答案**：`sharemem_size = sharedmem_depth * sharedmem_BlockWords * 4`，默认 `sharedmem_depth=1024`、`sharedmem_BlockWords=32` → 128 KiB。要变 64 KiB，把 [parameters.scala:L93](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/parameters.scala#L93) 的 `sharedmem_depth` 改成 `512` 即可（注意 `sharedmem_BlockWords` 复用了 `dcache_BlockWords`）。

**练习 3**：为什么改完参数最好跑一遍 `ParamPrintApp`？

**参考答案**：因为 `parameters` 里有大量 `log2Ceil(...)` 派生位宽（如 `depth_warp`、`WF_COUNT_WIDTH`、各种 `_ID_WIDTH`），手算容易漏；`ParamPrintApp` 用反射把全部字段导出成 JSON，便于核对改动是否如期、派生量是否自洽。

---

## 5. 综合实践

**任务**：把本讲四个模块串起来，做一次「缩小规模以适配小 FPGA」的端到端实验。

**要求**：

1. 在 [parameters.scala](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/parameters.scala) 里把规模调小，例如 `num_sm=1, num_warp=4`，并把 `dcache_NSets` 与 `l2cache_NSets` 各减半（注意保持 2 的幂）。
2. 用 `./mill ventus[6.4.0].runMain top.ParamPrintApp` 导出 `parameters.json`，与默认配置的 JSON 做对比，列出所有变化的字段。
3. 执行 `make fpga-verilog`，统计新的 `gen_fpga_verilog/mem.conf`：
   - 存储器实例总数（行数）；
   - 找出 depth 变化的寄存器堆/cache 行，记录 before/after。
4. 写一份简短报告：定性预测 BRAM/LUT/DSP 的变化方向，并说明哪些存储器贡献了主要变化。
5. 若有 Vivado 环境，把生成的 Verilog 加入一个空工程跑 `synth_design`，用 `report_utilization` 验证预测；若无则明确标注「待本地验证」并止步于 RTL/mem.conf 分析。

**验收标准**：

- 能画出从「改参数 → Chisel → firtool → mem.conf → 独立 SRAM → Vivado 综合」的完整链路；
- 能指出至少两类存储器（寄存器堆、cache 阵列）随哪个参数变化；
- 能说清 `GPGPU_axi_adapter_top` 在 SoC 里的控制面/数据面双重身份。

> 提示：这个综合实践本质上复刻了一次真实工程里「换板/缩规模」的日常工作流，只是把每次改动的依据都落到了本讲讲的源码上。

---

## 6. 本讲小结

- `make fpga-verilog` 是三步串联：Chisel 生成 CHIRRTL → `firtool --split-verilog --repl-seq-mem` 把存储器剥离并写 `mem.conf` → `gen_sep_mem.sh` 配合 `vlsi_mem_gen` 为每个存储器生成独立的可综合 SRAM 模型。
- FPGA 综合顶层是 `GPGPU_axi_adapter_top`（[GPGPU_top.scala:L138-L148](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L138-L148)），它是对 `GPGPU_axi_top` 的薄包装，核心收益是把 AXI id 位宽从 16 压到约 5，省资源。
- `mem.conf` 每行一个存储器，格式为 `name ... width ... depth ... ports ... mask_gran ...`，`ports` 用 `read/write/rw` 及带 `m` 前缀的掩码变体描述端口。
- `fpga_test/` 是一个基于 Vivado BD 的 SoC：MicroBlaze 当 host，经 AXI4-Lite（0x20000000）派发 kernel，GPGPU 经 AXI4 主口（DDR4 @0x80000000）取显存，器件为 VU37P / VCU128，顶层 `config_mb_wrapper`。
- GPU 规模几乎全部由 `object parameters` 决定，`num_warp`/`num_thread`/`num_sm` 与缓存规模是最主要的资源旋钮；寄存器堆（`num_vgpr=128*num_warp`、`num_sgpr=256*num_warp`）是 BRAM 大头。
- ⚠️ `fpga_test/readme.md` 与 `naive_driver.h` 存在滞后（顶层名、寄存器偏移），一切以 `Makefile`/`AXI4Lite2CTA.scala`/`project_gpgpu.tcl` 源码为准。

---

## 7. 下一步学习建议

走到这里，你已经读完了 Ventus 的全部主要模块。后续可以朝这些方向深入：

1. **真实上板**：如果你有 VCU128 或类似 Xilinx UltraScale+ 板卡，按本讲流程 `source project_gpgpu.tcl` 跑通综合/实现，下载比特流，在 SDK 里用（修正过寄存器偏移的）driver 跑一个 vecadd，观察 UART 输出与 ILA 波形。
2. **移植到其他 FPGA**：改 `project_gpgpu.tcl` 里的 `create_project -part` 与板卡约束，把工程搬到 Zynq UltraScale+ 或国产 FPGA 上；相应地调整 `parameters` 规模以适配目标资源。
3. **ASIC 视角**：把 `vlsi_mem_gen` 换成 `--blackbox` 或 `--tsmc28`，研究存储器替换成厂商 SRAM 编译器宏的流程，体会 FPGA BRAM 与 ASIC SRAM 宏的对接差异。
4. **回归软件栈**：硬件跑通后，结合 ventus-llvm 编译器、pocl 运行时与 SPIKE 模拟器（姊妹仓库），理解一条 OpenCL C 代码是如何最终变成本讲驱动里那串 `ProgramInstr`/`ProgramData` 的。
5. **重读架构**：带着上板/综合的体感回看 u2-l2 的 `GPGPU_top` 与 u6 的缓存层次，你会对「为什么这么切模块、为什么存储器要独立」有更扎实的理解。

> 推荐顺带重读：u1-l2（构建链路）、u2-l3（参数系统）、u6-l5（L2 与 source 路由）、u7-l2（AXI/host 驱动）。本讲涉及的 `GPGPU_axi_adapter_top`、`mem.conf`、`project_gpgpu.tcl` 正是这些讲义知识在「真实部署」场景下的汇流点。
