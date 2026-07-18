# Xilinx IP 核与工程生成脚本

## 1. 本讲目标

学完本讲后，读者应该能够：

- 说清楚 Xilinx 「IP 核」是什么，以及 `.xci`、`.coe` 两类文件在 IP 重生成中各自扮演的角色。
- 拿着 `PCIeSquirrel/ip/` 目录里的任何一个文件，仅凭文件名就能推断它的种类（FIFO / BRAM / ROM / PCIe）、数据宽度、是否跨时钟域、大致用在工程的哪条通路上。
- 读懂 `vivado_generate_project.tcl` 这份近 900 行脚本的关键骨架：它是怎样把 `src/` 的源码、`ip/` 的 IP、`src/` 的约束「拼装」成一个可综合的 Vivado 工程的。
- 理解为什么 `readme.md` 反复强调「发布版不含 Xilinx 专有 IP、用户必须自行重生成」，以及这背后的授权与可复现性考量。

本讲是专家层「时序、约束与 Xilinx IP」单元的第三篇，承接 u1-l3（构建流程）与 u5-l2（约束文件），把视角从「怎么用脚本」下沉到「脚本和 IP 文件里到底写了什么」。

## 2. 前置知识

在进入源码前，先用通俗语言澄清几个 Xilinx / Vivado 的基础概念。

- **FPGA**：一片可以用硬件描述语言（HDL）重新编程的芯片。pcileech-fpga 用的是 Xilinx Artix-7 系列（PCIeSquirrel 板卡为 `xc7a35tfgg484-2`）。
- **IP 核（IP Core）**：一段预先设计好、参数化、可重复使用的硬件模块。可以类比为软件里的「库函数」。Xilinx 提供大量现成 IP（FIFO、BRAM、PCIe 等），用户在 Vivado 里填几张表单就能生成一个定制版本。
- **Vivado**：Xilinx 官方的 FPGA 开发套件。WebPACK 是其免费版本，已足够本工程使用（`readme.md` 要求 2023.2 或更新）。
- **Tcl**：Vivado 的脚本语言。本工程的两个 `.tcl` 脚本就是一连串 Tcl 命令，用来代替在 GUI 里点点点。
- **综合（Synthesis）**：把 SystemVerilog 翻译成逻辑网表（与、或、非、寄存器）。
- **实现（Implementation）**：在真实芯片上为网表布局布线，最终产出可烧录的比特流（bitstream）。
- **`.xci` 文件**：一个 IP 实例的「配置档案」，本质是 JSON。它只描述「我要一个什么样的 IP」，不含 IP 的具体 HDL 实现——后者要由 Vivado 在「重生成（regenerate）」时根据 `.xci` 生成。
- **`.coe` 文件**：内存初始化数据（Coefficient File）。给 BRAM / ROM 这类带存储的 IP 一份上电初始值表。
- **XPM（Xilinx Parameterized Macros）**：Xilinx 提供的参数化宏库，如 `XPM_CDC`（跨时钟域）、`XPM_MEMORY`（内存），在源码里可直接例化。

> 名词速记：**IP = 可定制的硬件积木；.xci = 积木的订单；.coe = 积木里预装的数据；Vivado 重生成 = 按订单把积木造出来。**

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `PCIeSquirrel/vivado_generate_project.tcl` | 工程生成脚本。从零创建 Vivado 工程，导入源码、IP、约束，并建立综合/实现两个 run。 |
| `PCIeSquirrel/readme.md` | 设备说明与构建步骤；解释了专有 IP 的授权与重生成要求。 |
| `PCIeSquirrel/ip/pcileech_cfgspace.coe` | 配置空间影子的 BRAM 初始化数据（4KB）。 |
| `PCIeSquirrel/ip/*.xci`（20 个） | 各类 IP 的配置档案：FIFO、BRAM、分布式 ROM、PCIe 7x。 |
| `PCIeSquirrel/ip/pcileech_bar_zero4k.coe`、`pcileech_cfgspace_writemask.coe` | 另外两份 `.coe`：BAR 零初始化、配置空间写掩码。 |
| `PCIeSquirrel/build.md` | 进阶构建说明，提到通过 PCIe 核 GUI 与 `.coe` 修改设备特征。 |

## 4. 核心概念与源码讲解

本讲拆成五个最小模块：先讲 IP 与 `.xci` 是什么，再纵览 `ip/` 目录的命名规律，接着讲 `.coe`，然后精读 `vivado_generate_project.tcl` 的骨架，最后把「工程生成」与「IP 重生成」的关系讲透。

### 4.1 Xilinx IP 核与 .xci 配置档案

#### 4.1.1 概念说明

在 Vivado 里「创建一个 IP」，本质上是在 GUI 里填表单：选 IP 种类、设数据宽度、选时钟模式、勾选各种可选标志位。填完后，Vivado 把你选的所有选项序列化成一个 `.xci` 文件存到磁盘。

关键在于：`.xci` **不是 IP 的实现**，而是 IP 的「参数化订单」。真正可综合的 HDL 代码、综合后的网表（`.dcp`）、仿真模型等，都要等 Vivado 执行「生成输出产物（generate output products）」时，根据 `.xci` 里的参数现场产生。这也是为什么仓库里只放 `.xci` 而不放生成产物——产物体积大、与 Vivado 版本强绑定、且涉及 Xilinx 专有授权。

`.xci` 是 JSON 格式，顶层结构有几块：

- `component_reference`：指向 IP 目录里的某一项，形如 `xilinx.com:ip:fifo_generator:13.2`（供应商:类别:版本）。
- `component_parameters`：用户填的参数（数据宽度、深度、时钟模式……）。
- `model_parameters`：Vivado 据参数推导出的模型参数（地址宽度、家族等）。
- `project_parameters`：目标器件（`DEVICE`、`PACKAGE`、`SPEEDGRADE`）。
- `boundary`：对外端口的集合与方向。

#### 4.1.2 核心流程

```
用户在 GUI 填表  ──►  Vivado 写出 .xci（参数订单）
                              │
                              ▼
           generate output products（按订单造 IP）
                              │
              ┌───────────────┼────────────────┐
              ▼               ▼                ▼
         可综合 HDL      综合网表 .dcp     仿真模型
```

> 三句话总结：`.xci` 是订单；生成产物是按订单造出来的货；仓库只存订单，货由用户本机的 Vivado 现造。

#### 4.1.3 源码精读

以分布式 ROM `drom_pcie_cfgspace_writemask.xci` 为例（它是 IP 目录里最小的 `.xci`，结构最干净）。它的 `component_reference` 指向 Xilinx 分布式内存生成器：

[PCIeSquirrel/ip/drom_pcie_cfgspace_writemask.xci:4-7](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/ip/drom_pcie_cfgspace_writemask.xci#L4-L7) —— 声明这是一个 `dist_mem_gen:8.0`（分布式内存/ROM）IP 实例，名为 `drom_pcie_cfgspace_writemask`。

紧接着是用户参数：深度 1024、位宽 32、`memory_type` 为 `rom`，并用 `coefficient_file` 指向 `pcileech_cfgspace_writemask.coe` 作为初始值来源：

[PCIeSquirrel/ip/drom_pcie_cfgspace_writemask.xci:10-14](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/ip/drom_pcie_cfgspace_writemask.xci#L10-L14) —— 设定 ROM 的几何形状与类型。

[PCIeSquirrel/ip/drom_pcie_cfgspace_writemask.xci:26-28](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/ip/drom_pcie_cfgspace_writemask.xci#L26-L28) —— 关键一行：把 `.coe` 绑给这个 IP 作为初始化数据，并设默认填充值 `0xffffffff`。

最末的 `boundary` 段给出对外端口：一个 10 位输入地址 `a[9:0]`、一个 32 位输出 `spo[31:0]`，典型的组合地址→数据 ROM：

[PCIeSquirrel/ip/drom_pcie_cfgspace_writemask.xci:95-99](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/ip/drom_pcie_cfgspace_writemask.xci#L95-L99) —— IP 对外的端口边界。

这段 ROM 的用途很明确：它就是配置空间影子的「写掩码表」——每个 DWORD 对应一个 32 位掩码，决定主机经 USB 改写配置空间时哪些位允许写、哪些位锁死只读（u4-l1 详述）。

#### 4.1.4 代码实践

1. **实践目标**：亲手打开一个 `.xci`，确认它是 JSON，并从中读出「这是什么 IP、多宽、多深、用了哪个 `.coe`」。
2. **操作步骤**：
   - 用文本编辑器（不要用 Vivado）打开 `PCIeSquirrel/ip/fifo_134_134_clk2.xci`。
   - 搜索字符串 `component_reference`，记下它的值（应为 `xilinx.com:ip:fifo_generator:13.2`）。
   - 搜索 `Input_Data_Width`、`Input_Depth`、`Fifo_Implementation` 三个键，记下它们的值。
3. **需要观察的现象**：你会看到 `Fifo_Implementation` 的值是 `Independent_Clocks_Block_RAM`——这正说明 `clk2` 后缀的 FIFO 用的是「双时钟块 RAM」实现，对应 u5-l1 讲过的跨时钟域 FIFO。
4. **预期结果**：宽度 134、深度 2048、双时钟块 RAM。这与 u3-l4 讲过的「134 位 TLP 桥接打包宽度」完全对上（128 位 tdata + 1 first + 1 last + 4 tkeepdw = 134）。

#### 4.1.5 小练习与答案

**练习 1**：`.xci` 文件里既有 `component_parameters` 又有 `model_parameters`，二者区别是什么？
**答案**：`component_parameters` 是用户主动选择的输入参数（如宽度、深度）；`model_parameters` 是 Vivado 根据输入参数推导出来的派生量（如地址宽度 = log2(深度)），用户一般不直接改。

**练习 2**：为什么仓库里看不到 IP 的 `.v` / `.vhd` 实现文件？
**答案**：因为实现是 Xilinx 根据 `.xci` 参数现场生成的「输出产物」，体积大、与版本绑定、含专有内容，故仓库只存 `.xci` 订单，产物由用户本机 Vivado 重生成。

---

### 4.2 ip 目录全景：四大类 IP 与命名约定

#### 4.2.1 概念说明

`PCIeSquirrel/ip/` 目录里共 20 个 `.xci`（外加 3 个 `.coe`）。它们分成四类，对应四种 Xilinx IP：

1. **FIFO Generator**（`fifo_generator:13.2`）——16 个，占绝大多数。工程里所有跨时钟域桥接、位宽转换、缓冲排队都靠它们（详见 u5-l1、u3-l4）。
2. **Block Memory（BRAM）Generator**（`blk_mem_gen:8.4`）——2 个，提供 4KB 存储块：一个装配置空间影子、一个装 BAR 零页。
3. **Distributed Memory Generator**（`dist_mem_gen:8.0`）——1 个，用查找表（LUT）实现的小 ROM，装配置空间写掩码。
4. **7 系列 PCIe**（`pcie_7x:3.3`）——1 个，整个工程的硬核 `pcie_7x_0`（u3-l1 详述）。

文件命名遵循强约定，**看到名字就能猜出功能**。FIFO 类命名规律为：

```
fifo_<写位宽>_<读位宽>[_clk1|_clk2][_用途后缀]
```

- `_clk2` = 双时钟（跨时钟域），读写各用一个时钟，对应 u5-l1 的异步 FIFO。
- `_clk1` = 单时钟（同一时钟域），只是缓冲/排队。
- 不带时钟后缀（如 `fifo_64_64`、`fifo_34_34`）= 默认单时钟。
- 写读位宽不同（如 `fifo_256_32`）= 同时做位宽转换。

#### 4.2.2 核心流程

下表把 16 个 FIFO 按时钟模式归类（这是本讲综合实践任务的直接依据）：

| 时钟模式 | 数量 | 代表文件 | 含义 |
| --- | --- | --- | --- |
| `_clk2`（双时钟/跨域） | 8 | `fifo_134_134_clk2`、`fifo_64_64_clk2_comrx`、`fifo_256_32_clk2_comtx`、`fifo_32_32_clk2`、`fifo_43_43_clk2`、`fifo_49_49_clk2`、`fifo_1_1_clk2`、`fifo_134_134_clk2_rxfifo` | 跨 `clk`↔`clk_com` 或 `clk`↔`clk_pcie` 域 |
| `_clk1`（单时钟） | 6 | `fifo_129_129_clk1`、`fifo_134_134_clk1_bar_rdrsp`、`fifo_141_141_clk1_bar_wr`、`fifo_32_32_clk1_comtx`、`fifo_64_64_clk1_fifocmd`、`fifo_74_74_clk1_bar_rd1` | 同域缓冲，多见于 BAR 读/写引擎（u4-l2） |
| 无后缀（默认单时钟） | 2 | `fifo_64_64`、`fifo_34_34` | 通用同域缓冲 |

而非 FIFO 的 4 个 IP：

| 文件 | IP 类 | 用途 |
| --- | --- | --- |
| `bram_pcie_cfgspace.xci` | BRAM | 配置空间影子 4KB（u4-l1） |
| `bram_bar_zero4k.xci` | BRAM | BAR 零页 4KB（u4-l3 zerowrite4k） |
| `drom_pcie_cfgspace_writemask.xci` | 分布式 ROM | 配置空间写掩码 |
| `pcie_7x_0.xci` | PCIe 7x | PCIe 硬核（u3-l1） |

#### 4.2.3 源码精读

四类 IP 的 `component_reference` 各不相同，是判断种类的最可靠线索。FIFO 类指向 FIFO 生成器：

[PCIeSquirrel/ip/fifo_134_134_clk2.xci:4-6](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/ip/fifo_134_134_clk2.xci#L4-L6) —— `component_reference` 为 `xilinx.com:ip:fifo_generator:13.2`，`Fifo_Implementation` 为 `Independent_Clocks_Block_RAM`（独立时钟 = 双时钟）。

PCIe 硬核则指向 7 系列 PCIe IP，并带大量链路参数（链路宽度 X1、速率 5.0 GT/s、64 位接口、BAR0 为 4KB 内存）：

[PCIeSquirrel/ip/pcie_7x_0.xci:4-25](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/ip/pcie_7x_0.xci#L4-L25) —— 这是整个工程里最大、最复杂的 IP（76 KB），也是 u4-l4「设备身份定制」里通过 GUI 改 VID/PID/Class 的对象。

#### 4.2.4 代码实践

1. **实践目标**：不打开 Vivado，仅用命令行统计 `ip/` 目录并分类。
2. **操作步骤**：在仓库根目录执行（把 `.xci` 文件名按前缀分组计数）：

   ```bash
   ls PCIeSquirrel/ip/*.xci | sed 's#.*/##' \
     | awk '{ if (/^fifo_/) f++; else if (/^bram_/) b++; else if (/^drom_/) d++; else p++ } \
            END { print "FIFO:",f; print "BRAM:",b; print "DROM:",d; print "PCIe:",p }'
   ```

   再按 `_clk2 / _clk1 / 无后缀` 分类 FIFO：

   ```bash
   ls PCIeSquirrel/ip/fifo_*.xci | sed 's#.*/##; s/\.xci//' \
     | awk '{ if (/clk2/) k2++; else if (/clk1/) k1++; else none++ } \
            END { print "clk2(双时钟):",k2; print "clk1(单时钟):",k1; print "无后缀:",none }'
   ```

3. **需要观察的现象**：第一条应输出 `FIFO:16  BRAM:2  DROM:1  PCIe:1`；第二条应输出 `clk2:8  clk1:6  无后缀:2`。
4. **预期结果**：与 4.2.2 的表格一致。若数字对不上，说明 `ip/` 目录可能与本讲所基于的 HEAD（`c538c41`）有出入，需重新核对。

#### 4.2.5 小练习与答案

**练习 1**：`fifo_256_32_clk2_comtx` 这个名字透露了哪些信息？
**答案**：FIFO 类；写口 256 位、读口 32 位（同时做 256→32 位宽转换）；`clk2` 双时钟跨域；`comtx` 后缀表明用在 com 模块上行发送方向（把 fifo 的 256 位大包拆成 32 位送给 FT601，对应 u2-l4）。

**练习 2**：为什么 BAR 相关的 FIFO（`fifo_*_bar_*`）几乎都是 `_clk1` 单时钟？
**答案**：BAR 读/写引擎（u4-l2）整体工作在 `clk_pcie` 域内部，这些 FIFO 只做同域的缓冲与排队（拆包/拼包），不跨时钟域，所以用单时钟 `_clk1` 即可，面积与延迟更优。

---

### 4.3 .coe 内存初始化文件

#### 4.3.1 概念说明

`.coe`（Coefficient File）是 Xilinx 给 BRAM / ROM 类 IP 提供上电初始值的文本文件。格式非常简单，两行头部 + 一长串数据：

```
memory_initialization_radix=16;      ; 数据用几进制（16=十六进制）
memory_initialization_vector=        ; 数据向量开始
fffff000,fffff004,fffff008,fffff00c, ; 逗号分隔，每个值代表一个存储单元
...
fffffffc;                             ; 分号结束
```

每个值就是一个存储字（宽度由 IP 的 `data_width` 决定，本工程多为 32 位）。`.coe` 只对「带初始化」的存储类 IP 有意义——纯 FIFO（不关心初始内容）不需要 `.coe`。

本工程有 3 个 `.coe`，分别服务 3 个不同的存储 IP：

| `.coe` 文件 | 内容特征 | 服务的 IP | 含义 |
| --- | --- | --- | --- |
| `pcileech_cfgspace.coe` | 全 `0xfffff...`（每字末位递增） | `bram_pcie_cfgspace` | 配置空间影子初值（默认读回 0，见 u4-l1 的 `cfgtlp_zero`） |
| `pcileech_bar_zero4k.coe` | 全 `0x00000000` | `bram_bar_zero4k` | BAR 零页初值（zerowrite4k，可被主机改写） |
| `pcileech_cfgspace_writemask.coe` | 全 `0xffffffff` | `drom_pcie_cfgspace_writemask` | 写掩码初值（全 1 = 默认全部可写） |

#### 4.3.2 核心流程

`.coe` 与 IP 的绑定发生在 `.xci` 的 `coefficient_file` 字段；真正把数据烧进 BRAM 发生在 Vivado 生成 IP 输出产物时：

```
.coe（数据表）──► .xci 的 coefficient_file 字段引用
                         │
                         ▼
        Vivado 生成 IP 输出产物时，把 .coe 内容编译进 BRAM 初始化
                         │
                         ▼
        比特流烧录后，FPGA 上电即拥有这份初值
```

> 换言之：改 `.coe` 文本 → 必须重新生成对应 IP → 重新综合实现 → 重新烧录，设备才体现新值。

#### 4.3.3 源码精读

`pcileech_cfgspace.coe` 头部声明十六进制、紧跟数据向量，首字为 `fffff000`：

[PCIeSquirrel/ip/pcileech_cfgspace.coe:1-4](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/ip/pcileech_cfgspace.coe#L1-L4) —— 配置空间影子 4KB 的初始值表（1024 个 32 位字）。

注意每个字都是 `0xfffffNNN` 形式——高 20 位全 1、低 12 位是地址递增。结合 u4-l1 可知：在默认 `cfgtlp_zero`(`rw[203]`)=1 时，配置空间读回全零，这份 `.coe` 的真实内容并不显现；只有把 `rw[203]` 改 0 后，主机才能经 `lspci -xxxx` 看到这份 BRAM 初值。

`pcileech_cfgspace_writemask.coe` 则是另一副面孔——全 `0xffffffff`：

[PCIeSquirrel/ip/pcileech_cfgspace_writemask.coe:1-7](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/ip/pcileech_cfgspace_writemask.coe#L1-L7) —— 写掩码初值全 1，表示默认所有配置位都可被（USB 侧）改写。

若想让某个配置 DWORD 变成只读，只需把该地址对应的掩码字改成 `0x00000000` 即可——这是「锁死关键字段」的廉价手段（u4-l1 详述）。

#### 4.3.4 代码实践

1. **实践目标**：验证三个 `.coe` 的「全量填充」特征，并算出它们各初始化多大的存储。
2. **操作步骤**：

   ```bash
   for f in pcileech_cfgspace pcileech_bar_zero4k pcileech_cfgspace_writemask; do
     n=$(grep -oE '[0-9a-fA-F]{8}' PCIeSquirrel/ip/$f.coe | wc -l)
     echo "$f: $n 个 32 位字 = $((n*4)) 字节"
   done
   ```

3. **需要观察的现象**：三个文件都应报告约 1024 个字 = 4096 字节（4 KB）。
4. **预期结果**：`bram_pcie_cfgspace` 与 `bram_bar_zero4k` 各是 4 KB，与 `pcie_7x_0.xci` 里 `Bar0_Size=4`(KB) 对应；`drom_pcie_cfgspace_writemask` 也是 1024 项 ×32 位，正好覆盖整个配置空间每个 DWORD 一个掩码位。

#### 4.3.5 小练习与答案

**练习 1**：改了 `.coe` 之后，直接重新综合工程就够了吗？
**答案**：不够。`.coe` 是 IP 的输入数据，必须先重新生成对应 IP 的输出产物（让 Vivado 把新数据编进 BRAM 初始化），再重新综合实现并烧录，设备才体现新值。

**练习 2**：为什么写掩码用「分布式 ROM（drom）」而不是用 BRAM？
**答案**：写掩码是只读的、且只需按地址查一个 32 位值，分布式 ROM 用 LUT 实现更省资源；而配置空间影子需要被主机改写（读写双口），所以用 BRAM。

---

### 4.4 vivado_generate_project.tcl：把散件组装成工程

#### 4.4.1 概念说明

仓库里**没有**现成的 `.xpr`（Vivado 工程文件），只有一份脚本 `vivado_generate_project.tcl`。运行它，Vivado 就会按脚本里的命令，从零「拼」出一个完整工程。这样设计的好处是：工程定义纯文本、可 diff、可版本管理，不依赖任何二进制工程文件。

脚本的职责可以归纳为五件事：

1. **创建工程**：指定工程名、目标器件（`xc7a35tfgg484-2`）、各类工程属性。
2. **导入源码**：把 `src/` 下的 11 个 `.sv/.svh` 加入 `sources_1` 文件集，并设文件类型。
3. **导入 IP**：把 `ip/` 下的 20 个 `.xci`（和 3 个 `.coe`）逐个加入工程，注册到 IP 管理器。
4. **导入约束**：把 `src/pcileech_squirrel.xdc` 加入 `constrs_1` 文件集。
5. **建立 run**：创建 `synth_1`（综合）和 `impl_1`（实现）两个 run，设好策略与 `bin_file=1`，并 `upgrade_ip` 升级旧版 IP。

理解了这五件事，整份近 900 行脚本就不再可怕——中间大量重复段落只是在给每个 IP 重复设置同样的属性。

#### 4.4.2 核心流程

```
set origin_dir / 工程名 pcileech_squirrel
        │
        ▼
create_project  -part xc7a35tfgg484-2     ◄── 目标器件
        │
        ▼
设工程属性（default_lib、xpm_libraries=XPM_CDC XPM_MEMORY、simulator=Mixed）
        │
        ▼
sources_1 文件集：
   ├─ import 11 个 .sv/.svh，设 file_type=SystemVerilog（header 设 Verilog Header）
   ├─ import 3 个 .coe
   ├─ import 20 个 .xci（每个设 registered_with_manager=1、synth_checkpoint_mode=Singular）
   └─ set top = pcileech_squirrel_top
        │
        ▼
constrs_1 文件集：import pcileech_squirrel.xdc（file_type=XDC）
sim_1 文件集：设 top
        │
        ▼
upgrade_ip [get_ips *]                    ◄── 升级旧版 IP 到当前 Vivado
        │
        ▼
create_run synth_1  -flow Vivado Synthesis 2022
create_run impl_1   -flow Vivado Implementation 2022  -parent synth_1
set write_bitstream.args.bin_file = 1     ◄── 产出 .bin
```

#### 4.4.3 源码精读

**第一件事——创建工程并锁定器件**。脚本先处理 `origin_dir`（可用 `--origin_dir` 覆盖）与工程名（默认 `pcileech_squirrel`），然后用 `create_project` 指定目标器件：

[PCIeSquirrel/vivado_generate_project.tcl:74](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/vivado_generate_project.tcl#L74) —— `create_project ... -part xc7a35tfgg484-2`，锁定 Squirrel 板卡的 Artix-7 35T、484 球 BGA、速度等级 -2。

紧接着一批工程属性里，有一项对跨时钟域至关重要——启用 XPM 库：

[PCIeSquirrel/vivado_generate_project.tcl:98](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/vivado_generate_project.tcl#L98) —— `xpm_libraries` 设为 `XPM_CDC XPM_MEMORY`，允许源码里直接例用 Xilinx 的跨时钟域与内存宏（与 u5-l1 的双时钟 FIFO 主题呼应）。

**第二件事——导入 11 个源文件**。一个 Tcl `list` 把所有 `.sv/.svh` 路径列出，再一次性 `import_files`：

[PCIeSquirrel/vivado_generate_project.tcl:108-121](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/vivado_generate_project.tcl#L108-L121) —— 这份清单就是工程的全部用户 HDL：`pcileech_header.svh`（接口定义，u2-l1）+ com/fifo/ft601/mux/pcie_a7/pcie_cfg_a7/pcie_tlp_a7/tlps128_bar_controller/tlps128_cfgspace_shadow + `pcileech_squirrel_top.sv`（顶层，u1-l4）。

随后逐个设文件类型（`pcileech_header.svh` 设为 `Verilog Header`，其余 `.sv` 设为 `SystemVerilog`），并指定顶层模块：

[PCIeSquirrel/vivado_generate_project.tcl:127-129](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/vivado_generate_project.tcl#L127-L129) —— 把头文件标记为 `Verilog Header`（被其他 `.sv` `` `include ``）。

[PCIeSquirrel/vivado_generate_project.tcl:174](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/vivado_generate_project.tcl#L174) —— 设顶层为 `pcileech_squirrel_top`，`top_auto_set=0` 表示不自动推断、强制指定。

**第三件事——导入 IP**。`.coe` 与第一个 BRAM IP 一起导入，并对 `.xci` 设置三项关键属性：

[PCIeSquirrel/vivado_generate_project.tcl:181-197](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/vivado_generate_project.tcl#L181-L197) —— 导入 3 个 `.coe` 与 `bram_pcie_cfgspace.xci`；设 `generate_files_for_reference=0`（不立即生成产物）、`registered_with_manager=1`（交由 IP 管理器托管）、`synth_checkpoint_mode=Singular`（IP 单独综合成独立网表，便于复用缓存）。

之后的脚本就是这一段的「复制粘贴」——每个 `.xci` 都重复同样的 5~6 行属性设置，只是文件名换一下。直到导入最关键的 PCIe 硬核：

[PCIeSquirrel/vivado_generate_project.tcl:431-444](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/vivado_generate_project.tcl#L431-L444) —— 导入 `pcie_7x_0.xci`，属性设置与其他 IP 完全一致。

**第四件事——导入约束**：

[PCIeSquirrel/vivado_generate_project.tcl:568-572](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/vivado_generate_project.tcl#L568-L572) —— 把 `pcileech_squirrel.xdc`（u5-l2）加入约束集，类型设为 `XDC`。

**第五件事——升级 IP、建 run**。先用当前 Vivado 版本升级所有旧版 IP（本工程 `.xci` 多由 2023.2 生成，跨版本时需要这一步）：

[PCIeSquirrel/vivado_generate_project.tcl:593](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/vivado_generate_project.tcl#L593) —— `upgrade_ip [get_ips *]` 把全部 IP 升级到当前 Vivado 版本。

然后创建综合与实现两个 run，`impl_1` 以 `synth_1` 为父 run（实现依赖综合结果）：

[PCIeSquirrel/vivado_generate_project.tcl:603-604](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/vivado_generate_project.tcl#L603-L604) —— 创建 `synth_1`，使用 `Vivado Synthesis 2022` 流程与默认策略。

[PCIeSquirrel/vivado_generate_project.tcl:629-630](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/vivado_generate_project.tcl#L629-L630) —— 创建 `impl_1`，父 run 为 `synth_1`；这就是 u1-l3 讲过的「父子 run」关系。

最后，把 `write_bitstream` 的 `bin_file` 置 1，确保实现结束后产出可烧录的 `.bin`（而不是只产 `.bit`）：

[PCIeSquirrel/vivado_generate_project.tcl:839](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/vivado_generate_project.tcl#L839) —— `steps.write_bitstream.args.bin_file` 设为 `1`，对应最终产物 `pcileech_squirrel.bin`。

脚本尾部（846 行往后）创建的是 Vivado 仪表盘（dashboard gadgets），用于在 GUI 里展示 DRC、时序、功耗、资源利用率等报告，对命令行构建无影响。

#### 4.4.4 代码实践

1. **实践目标**：通读脚本，统计它到底导入了多少个 `.xci`，并与 `ip/` 目录的实际文件数比对。
2. **操作步骤**：

   ```bash
   # 脚本里出现的 .xci 次数（导入清单）
   grep -c '\.xci' PCIeSquirrel/vivado_generate_project.tcl
   # ip 目录里实际的 .xci 数
   ls PCIeSquirrel/ip/*.xci | wc -l
   ```

   再单独看脚本导入了哪些 `.sv`：

   ```bash
   grep -oE '\$\{origin_dir\}/src/[a-z0-9_]+\.sv[a-z]?' PCIeSquirrel/vivado_generate_project.tcl
   ```

3. **需要观察的现象**：`.xci` 的统计值会大于 20（因为每个 IP 在脚本里被引用两次：一次在 `import` 清单，一次在设属性的 `set file` 行），但去重后应为 20，与目录一致；`.sv/.svh` 去重后应为 11 个。
4. **预期结果**：脚本与目录一一对应，没有「孤儿 IP」（目录里有但脚本没导入）也没有「幽灵 IP」（脚本导入但目录里没有）。这正是工程可复现的关键。

#### 4.4.5 小练习与答案

**练习 1**：脚本里 `registered_with_manager=1` 和 `synth_checkpoint_mode=Singular` 分别有什么用？
**答案**：`registered_with_manager=1` 表示该 IP 交给 Vivado 的 IP 管理器统一托管（跟踪版本、依赖、生成状态）；`synth_checkpoint_mode=Singular` 表示 IP 单独综合成独立网表（out-of-context），这样 IP 一旦综合完成可被缓存复用，改其他源码时不必重新综合 IP，加快迭代。

**练习 2**：为什么 `impl_1` 要以 `synth_1` 为 `parent_run`？
**答案**：实现（布局布线）的输入是综合产出的网表，二者是上下游关系；把 `synth_1` 设为父 run 后，Vivado 知道实现依赖综合结果，运行 `impl_1` 前会自动确保 `synth_1` 已完成（u1-l3 讲过二者成对启动）。

---

### 4.5 工程生成与 IP 重生成：专有 IP 的授权逻辑

#### 4.5.1 概念说明

回看 u1-l3 的构建流程：先跑 `vivado_generate_project.tcl`（生工程），再跑 `vivado_build.tcl`（生 IP 产物 + 综合实现 + 出比特流）。为什么非要拆成两步、而且「生成 Xilinx 专有 IP」被单独归到第二步？

核心原因是**授权与可分发性**。Xilinx 的 IP 分两类：

- **免费 / 开源 IP**：如 FIFO Generator、BRAM Generator、分布式内存，WebPACK 用户可自由生成。
- **专有 IP**：如 7 系列 PCIe 硬核 `pcie_7x_0`，受《Xilinx CORE LICENSE AGREEMENT》约束，其生成产物（综合后的网表、加密的 HDL）**不允许随开源项目再分发**。

因此 ufrisk 在仓库里只放了「订单」（`.xci`）和「数据」（`.coe`），**坚决不放**任何专有 IP 的生成产物。用户在自己机器上用免费下载的 Vivado WebPACK 跑一次构建，就合法地「重生成」出全部专有产物——既遵守了授权，又保证了工程完全可复现。

`readme.md` 把这一点写得很明确：

[PCIeSquirrel/readme.md:49-51](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/readme.md#L49-L51) —— 完整方案含 Xilinx 专有 IP；GitHub 上的发布版不含任何专有 IP；下载了免费 WebPACK 的终端用户有权自行重生成。

而构建步骤本身也呼应了这种拆分：

[PCIeSquirrel/readme.md:36-42](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/readme.md#L36-L42) —— 第 4 步 `vivado_generate_project.tcl` 只生工程文件；第 5 步 `vivado_build.tcl` 才「生成 Xilinx 专有 IP 并构建比特流」。

#### 4.5.2 核心流程

```
仓库（GitHub）                          用户本机
─────────────                          ─────────
 .sv / .svh  ─┐
 .xdc        ─┤   vivado_generate_project.tcl  ──►  工程 .xpr（含 run，无产物）
 .xci（订单）─┤                                  ──►  upgrade_ip（升级版本）
 .coe（数据）─┘
              │
              │   vivado_build.tcl
              ├─►  generate IP 输出产物（专有 IP 在此合法生成）
              ├─►  launch_runs synth_1  ──► 网表
              └─►  launch_runs impl_1   ──► pcileech_squirrel.bin（可烧录）
```

> 一句话：**订单可开源，货物（专有产物）本机造。** 这让仓库保持轻量、合法、可 diff，又丝毫不损失可复现性。

#### 4.5.3 源码精读

`build.md` 在「进阶定制」一节里再次点到 IP 与 `.coe` 的协作关系——配置空间既可由 PCIe 核 GUI 改、也可由 `.coe` 改：

[PCIeSquirrel/build.md:44](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/build.md#L44) —— 说明配置空间通过编辑 `ip/pcileech_cfgspace.coe` 修改，并警告 Xilinx PCIe 核会「部分覆盖」（in-part override）用户配置值。

这恰好印证了 4.3 讲的：改 `.coe` 是改配置空间影子的廉价途径，但硬核本身的某些字段（如 VID/PID）会被 PCIe IP 覆盖，必须从 `pcie_7x_0.xci` 的 GUI 入手（u4-l4）。

而「打开 PCIe 核 GUI」的方式，`build.md` 也写明了：

[PCIeSquirrel/build.md:27](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/build.md#L27) —— 在展开的层次里双击 `i_pcie_7x_0` 即可打开 PCIe 核设计器 GUI——这正是 u4-l4 改 VID/PID/Class 的入口。

#### 4.5.4 代码实践

1. **实践目标**：从 `readme.md` 与脚本里找出「专有 IP 为何需用户重生成」的三条证据。
2. **操作步骤**：
   - 在 `PCIeSquirrel/readme.md` 中搜索 `proprietary`，阅读含该词的整段。
   - 在 `vivado_generate_project.tcl` 中确认：脚本只 `import` 了 `.xci`，**没有任何**「生成输出产物」的命令（`generate_target` / `synth_ip` 都没有）——生成被推迟到 `vivado_build.tcl`。
   - 在 `ip/` 目录确认：除了 `.xci` 和 `.coe`，没有任何 `.dcp`（设计检查点）、`.v`（生成 HDL）、`.mif` 等产物文件。
3. **需要观察的现象**：三处证据相互印证——README 声明不含专有 IP；脚本只导订单不造货；目录里确实没有产物。
4. **预期结果**：理解 pcileech-fpga 的「订单/货物分离」设计：仓库可任意 fork 与 diff，而每个使用者都能用免费工具在自己机器上完整重生成出可烧录的比特流。

#### 4.5.5 小练习与答案

**练习 1**：如果有人把 `pcie_7x_0` 的综合产物（`.dcp`）一起提交到 GitHub，会怎样？
**答案**：违反 Xilinx CORE LICENSE AGREEMENT（专有 IP 产物不可随开源项目再分发）；也使仓库膨胀、失去跨版本可移植性（产物与特定 Vivado 版本强绑定）。正因如此，项目坚持只存 `.xci` 订单。

**练习 2**：为什么 `vivado_generate_project.tcl` 里有 `upgrade_ip` 却没有 `generate_target`？
**答案**：`upgrade_ip` 只是让旧版 `.xci` 适配当前 Vivado（属于「工程生成」阶段，轻量、不造货）；`generate_target` 才是真正「按订单造出可综合产物」（属于「构建」阶段，耗时且涉及专有 IP），故被放到 `vivado_build.tcl`。这种拆分让「生工程」很快、「真构建」另起一步。

---

## 5. 综合实践

**任务**：扮演一次「工程审计员」，用本讲学到的命名规律与脚本结构，核对 PCIeSquirrel 工程的自洽性，并用一句话回答「为何这个仓库能既开源又合法」。

请按顺序完成：

1. **IP 清点**：列出 `PCIeSquirrel/ip/` 下全部 20 个 `.xci`，按下表分类填空（可照搬 4.2.2 的格式）：

   | 类别 | 数量 | 成员（简写） |
   | --- | --- | --- |
   | FIFO / 双时钟 `clk2` | 8 | |
   | FIFO / 单时钟 `clk1` | 6 | |
   | FIFO / 无后缀 | 2 | |
   | BRAM | 2 | |
   | 分布式 ROM | 1 | |
   | PCIe 7x | 1 | |

2. **命名解码**：对下列 4 个 IP，仅凭名字写出「类别 + 写读位宽 + 时钟模式 + 推测用途」，再打开 `.xci` 核对 `component_reference` 与 `Input_Data_Width` 是否与你推断一致：
   - `fifo_256_32_clk2_comtx`
   - `fifo_1_1_clk2`
   - `bram_pcie_cfgspace`
   - `pcie_7x_0`

3. **脚本核对**：在 `vivado_generate_project.tcl` 中确认下列 5 个关键动作各在哪一行（给出行号区间）：
   - `create_project` 指定 `xc7a35tfgg484-2`
   - 导入 11 个 `.sv/.svh` 的 `list`
   - 设顶层 `pcileech_squirrel_top`
   - `upgrade_ip [get_ips *]`
   - `bin_file = 1`

4. **授权论述**：综合 `readme.md` 的专有 IP 段、脚本「只导订单不造货」、目录「无产物」三方面证据，用 100 字以内说明「为什么这个开源仓库不包含 Xilinx 专有 IP，却仍能让任何用户重生成出完整可烧录比特流」。

**验收标准**：第 1 步分类总数 = 20；第 2 步推断与 `.xci` 实际值一致；第 3 步行号能在脚本里对上（分别为 L74、L108-121、L174、L593、L839 附近）；第 4 步能点出「订单（.xci）开源 + 货物（专有产物）本机用免费 WebPACK 重生成」这一核心机制。

## 6. 本讲小结

- **IP 核**是参数化的硬件积木；`.xci` 是它的「订单」（JSON），`.coe` 是给存储类 IP 的「预装数据」；真正的可综合产物由 Vivado 在重生成时按订单现场造出。
- `PCIeSquirrel/ip/` 共 20 个 `.xci`：16 个 FIFO（按 `clk2/clk1/无后缀` 分双时钟 8 个、单时钟 6 个、默认 2 个）、2 个 BRAM、1 个分布式 ROM、1 个 PCIe 7x 硬核；命名规律 `fifo_<写>_<读>_<时钟>_<用途>` 让你「见名知义」。
- 3 个 `.coe` 分别初始化配置空间影子（默认全 0 不可见）、BAR 零页、配置空间写掩码（全 1 = 全可写）；改 `.coe` 必须重新生成 IP 才生效。
- `vivado_generate_project.tcl` 做五件事：`create_project` 锁器件（`xc7a35tfgg484-2`）→ 导入 11 个源文件 → 导入 20 个 `.xci` + 3 个 `.coe` → 导入 `.xdc` 约束 → `upgrade_ip` 并建 `synth_1`/`impl_1` 两个 run（`bin_file=1`）。
- 仓库**只存订单与数据、不存专有产物**：这是兼顾「开源合法」与「完全可复现」的关键——任何用户用免费 Vivado WebPACK 跑一次 `vivado_build.tcl` 即可合法重生成全部 Xilinx 专有 IP 并产出比特流。
- 脚本里 `xpm_libraries=XPM_CDC XPM_MEMORY`、`upgrade_ip`、`registered_with_manager`/`synth_checkpoint_mode=Singular` 等设置，分别支撑了跨时钟域设计（u5-l1）、跨版本兼容与 IP 缓存复用。

## 7. 下一步学习建议

- **横向对比设备变种**：进入 `CaptainDMA/` 或 `ZDMA/` 任一子工程的 `ip/` 目录，对比其 `.xci` 清单与 PCIeSquirrel 的差异——你会直观看到 x4 工程（u6-l1）多出哪些 FIFO、PCIe IP 换成了什么版本，从而巩固本讲的命名规律。
- **深入单个 IP**：挑 `pcie_7x_0.xci` 通读，结合 u4-l4（设备身份定制）理解 `Bar0`/`Link_Speed`/`Device_Port_Type` 等参数如何决定设备在主机端的呈现。
- **接续单元**：本讲是 u5（时序、约束与 IP）单元的收尾。下一单元 u6（设备变种与二次开发）会把本讲识别的 IP 清单放进真实移植场景——建议先做 u6-l2（移植到新板卡），届时你会反复回到 `vivado_generate_project.tcl` 修改目标器件与源文件清单。
- **延伸阅读**：若想理解 `vivado_build.tcl` 里 `generate_target`/`launch_runs`/`wait_on_run` 如何真正「造货」，可结合 u1-l3 的构建流程讲义对照阅读 `PCIeSquirrel/vivado_build.tcl` 本身。
