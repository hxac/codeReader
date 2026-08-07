# 构建流程总览：CSR → SW → HW → bitstream

## 1. 本讲目标

本讲是「上手篇」的关键一讲。读完本讲，你应该能够：

- 用一句话说清 wireguard-fpga 的构建分为哪三步，以及它们为什么必须按 `CSR → SW → HW` 的顺序进行。
- 指出每一步的**输入文件**、**调用工具**和**输出产物**分别是什么。
- 解释为什么「软件必须先于硬件构建」——即 `imem.INIT.vh` 这个文件在两步之间扮演的桥梁角色。
- 区分 **Vivado** 与 **openXC7** 两条可选硬件工具链，并知道它们各自的产物与代价。

本讲只看「怎么把源码变成能跑的东西」，不深入每个模块的内部原理（那是后续单元的事）。我们把三个 `Makefile` 当作三条流水线，逐一拆解它们的进料与出料。

## 2. 前置知识

- **Make / Makefile**：一个用依赖关系驱动命令执行的工具。`make -f 某个Makefile` 会按照文件里写的「目标: 依赖」规则，自动按顺序跑命令。本讲把三个 Makefile 当作构建脚本读，不需要你会写。
- **CSR（Control and Status Registers，控制状态寄存器）**：软 CPU 与硬件 RTL 之间通信的唯一窗口。CPU 往某些地址写值就能「控制」硬件，读某些地址就能拿到硬件「状态」。
- **RTL（Register Transfer Level，寄存器传输级）**：用 SystemVerilog/Verilog 写的、可被综合成真实电路的硬件描述代码。
- **HAL（Hardware Abstraction Layer，硬件抽象层）**：给软件用的一组 C 头文件/函数，把「读写某个寄存器」封装成好记的 API，屏蔽底层地址细节。
- **交叉编译（cross-compile）**：在你的 PC 上编译出给**另一种架构**（这里是 RISC-V 32 位）运行的程序。需要专门的工具链前缀，如 `riscv64-unknown-elf-`。
- **ELF / bin / hex**：编译器产出的几种可执行文件格式。`.elf` 带调试信息，`.bin` 是裸字节，`.hex` 是文本化的十六进制。
- **bitstream（比特流）**：FPGA 厂商工具综合+布线后产出的 `.bit` 文件，烧进芯片后决定电路怎么连。
- **综合（synthesis）/ 布局布线（PnR）**：把 RTL 翻译成门电路（综合），再把这些门摆到芯片物理单元上、连好线（PnR），最终生成 bitstream。

如果你对上面某些词还陌生，没关系，本讲会在用到时再点一遍。

## 3. 本讲源码地图

本讲围绕 `3.build/` 目录下的构建脚本展开，但会牵出 `1.hw` 与 `2.sw` 两个目录作为「进料」与「出料」的端点。

| 文件 | 作用 |
|------|------|
| [3.build/README.md](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/README.md) | 构建总说明，定义三步流程、产物清单与两条工具链 |
| [3.build/MakefileCSR](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/MakefileCSR) | 第 1 步：从 `csr.rdl` 生成 CSR 的 RTL 与 HAL |
| [3.build/MakefileSW](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/MakefileSW) | 第 2 步：交叉编译 RISC-V 固件，产出 `imem.INIT.vh` |
| [3.build/MakefileHW](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/MakefileHW) | 第 3 步：调用 Vivado 综合 PnR，产出 bitstream |
| [3.build/imem.INIT.py](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/imem.INIT.py) | 第 2 步内的辅助脚本：把 `main.bin` 转成 Verilog 可包含的内存初始化文件 |
| [3.build/csr_build/csr.rdl](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/csr.rdl) | CSR 的单一真源（SystemRDL 描述），第 1 步的唯一进料 |
| [1.hw/ip.cpu/imem.sv](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.cpu/imem.sv) | 硬件里的指令存储器；它 `\`include` 了第 2 步的产物 |
| [1.hw/top.filelist](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.filelist) | 列出综合需要的所有源文件，含第 1 步生成的 CSR RTL |

> 小提示：GitHub 链接里的 `9887a3b3…` 是当前 HEAD 的 commit 号，点开后能直接定位到本讲对应的代码版本。

## 4. 核心概念与源码讲解

### 4.1 三步构建总览与依赖链

#### 4.1.1 概念说明

wireguard-fpga 是一个**软硬协同的 SoC**：硬件用 SystemVerilog 写（`1.hw`），软件用 C/C++ 写（`2.sw`），二者要能通信、能协同运行。把这样两套异构代码「变成一块能跑的 FPGA 芯片」，需要一条精心编排的构建流水线。

`3.build/README.md` 开宗明义，把构建拆成三步：

1. **CSR 编译**：从寄存器规格（RDL）生成硬件用的 RTL 和软件用的 HAL。
2. **SW 编译**：把 C/C++ 软件交叉编译成 RISC-V 二进制。
3. **HW 编译**：把 SystemVerilog 综合成 bitstream。

#### 4.1.2 核心流程

这三步不是平行可选的，而是**有硬性先后依赖**的单向链：

```
   第1步 CSR            第2步 SW              第3步 HW
 ┌──────────┐       ┌──────────┐        ┌──────────────┐
 │ csr.rdl  │──┬───▶│ 固件源码  │───────▶│ SystemVerilog │
 │(单一真源)│  │HAL │ + HAL头   │        │  + csr.sv     │
 └──────────┘  │    └────┬─────┘        │  + imem.INIT  │──▶ top.bit
     │         │         │ imem.INIT.vh │     .vh       │
     └─────────┘         └─────────────▶└──────────────┘
      csr.sv(给HW RTL)         ^
                              │
                 这就是「SW必须先于HW」的根因
```

要点：

- 第 1 步的产物**同时**喂给第 2 步（HAL 头文件给软件 include）和第 3 步（`csr.sv` 给硬件综合）。
- 第 2 步会产出一个 `imem.INIT.vh` 文件，它是**软件二进制**的 Verilog 外壳——硬件综合时必须把它「焊」进指令存储器里。所以第 3 步必须等第 2 步完成。

#### 4.1.3 源码精读

README 对三步的定义（[3.build/README.md:9-12](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/README.md#L9-L12)）：

> The build process for the WireGuard project consists of three steps:
> - Compilation of … CSR from RDL specification into RTL … and HAL …
> - Compilation of the software application for the RISC-V hardware target
> - Compilation of SystemVerilog designs into bitstream for the hardware target

而 openXC7 章节用粗体强调了关键依赖（[3.build/README.md:152](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/README.md#L152)）：

> **IMPORTANT:** The software must be built first to generate the `imem.INIT.vh` file required by the hardware build.

「必须先编译软件」的根本原因，藏在硬件指令存储器里（[1.hw/ip.cpu/imem.sv:89-91](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.cpu/imem.sv#L89-L91)）：

```systemverilog
  initial begin
    `include "imem.INIT.vh"   // ← 这一行在综合时把软件二进制焊进 BRAM
  end
```

`\`include` 在综合期就被展开，意味着综合那一刻 `imem.INIT.vh` 必须已存在——这把第 2 步与第 3 步死死绑成先后关系。

#### 4.1.4 代码实践

**实践目标**：在脑海里把三步依赖图固定下来。

**操作步骤**：

1. 打开 [3.build/README.md](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/README.md)，找到三个一级标题：`CSR HAL Compilation`、`SW Compilation`、`HW Compilation - Vivado`。
2. 注意它们的文档顺序，恰好就是构建顺序。
3. 在 L152 找到那行 **IMPORTANT** 粗体提示。

**需要观察的现象**：README 的章节顺序 = 构建顺序；而且 README 特意把「缺 `imem.INIT.vh`」列为常见错误（L225-229），并提示解法是先跑 `make -f MakefileSW`。

**预期结果**：你会确认依赖链是 `CSR → SW → HW`，且 SW↔HW 之间的唯一物理耦合点就是 `imem.INIT.vh`。

#### 4.1.5 小练习与答案

**练习**：如果只跑了第 1 步和第 3 步、跳过第 2 步，综合会报什么错？为什么？

> **参考答案**：会报 `imem.INIT.vh not found`（README L227 正是这个错误）。因为 `imem.sv:90` 在综合期 `\`include "imem.INIT.vh"`，而该文件只有第 2 步 `MakefileSW` 通过 `imem.INIT.py` 才会生成。跳过第 2 步，这个文件就不存在，综合便无法展开 include。

---

### 4.2 CSR 构建步骤：单一真源到多产物

#### 4.2.1 概念说明

CSR 是软硬件之间**唯一的桥梁**。如果硬件工程师改了某个寄存器的地址，软件却还在用旧地址，整个系统就错了。为了避免这种「两边对不上」的灾难，本项目用 **SystemRDL**（一种专门描述寄存器的领域语言）写一份规格文件 `csr.rdl`，把它当作**单一真源（single source of truth）**，再用工具自动生成两边各自需要的代码。这样无论改什么，两边都跟着一起变。

#### 4.2.2 核心流程

`MakefileCSR` 的生成流水线（`make -f MakefileCSR`）：

1. **过滤**：用 `sed` 把 `csr.rdl` 里 `systemrdl-compiler` 看不懂的 `buffer_writes`/`wbuffer_trigger` 行删掉，得到 `csr_cosim.rdl`。
2. **生成 c-header 基底**：用 `peakrdl c-header` 从 `csr_cosim.rdl` 产出 `csr.h`。
3. **生成 RTL**：用 `peakrdl regblock` 从原始 `csr.rdl` 产出 `csr.sv` + `csr_pkg.sv`。
4. **包装 HAL**：用 `sysrdl_cosim.py` 脚本把 `csr.h` 包成 `csr_hw.h`（给真硬件 + rv32 ISS）和 `csr_cosim.h`（给 VProc 协同仿真）。
5. **附带文档**：用 `peakrdl html/markdown` 产出人读的寄存器文档。

#### 4.2.3 源码精读

入口变量定义（[3.build/MakefileCSR:5-12](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/MakefileCSR#L5-L12)）——注意有两个顶层：`csr`（RTL 用）和 `wireguard`（文档用）：

```makefile
RDLCSRTOP = csr          # RTL 的 addrmap 顶层
RDLWGTOP  = wireguard    # HTML/MD 文档的 addrmap 顶层
RDLSRC    = $(RDLDIR)/$(RDLPREFIX).rdl   # 即 csr_build/csr.rdl
```

生成 RTL 的核心规则（[3.build/MakefileCSR:34-35](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/MakefileCSR#L34-L35)）：

```makefile
rtl: $(RDLSRC)
	@peakrdl regblock $^ -o $(GENDIR)/ --cpuif passthrough --top $(RDLCSRTOP)
```

这一行调用 `peakrdl regblock`，以 `csr` 这个 addrmap 为顶层，产出 `csr.sv` 与 `csr_pkg.sv`。这两个文件会被第 3 步的硬件综合纳入（见 [1.hw/top.filelist:42-43](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.filelist#L42-L43)）。

两个 HAL 头由同一脚本加不同选项生成（[3.build/MakefileCSR:22-26](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/MakefileCSR#L22-L26)）：

```makefile
$(COSIMHDR): $(COSIMRDL) $(PKRDLHDR)
	@python3 sysrdl_cosim.py -c -r $< -o $@    # -c = 协同仿真版

$(HWHDR): $(COSIMRDL) $(PKRDLHDR)
	@python3 sysrdl_cosim.py -r $< -o $@        # 无 -c = 硬件版
```

软件最终通过一个分发头选择用哪一个 HAL（[3.build/csr_build/generated-files/wireguard_regs.h:15-19](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/generated-files/wireguard_regs.h#L15-L19)）：

```c
# ifdef VPROC
#  include "csr_cosim.h"     // 协同仿真时
# else
#  include "csr_hw.h"        // 真硬件 / ISS 时
# endif
```

这就是「单一真源 → 多个产物（RTL + 硬件 HAL + 仿真 HAL + 文档）」的全貌。最终产物清单见 README（[3.build/README.md:48-56](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/README.md#L48-L56)）：`csr_cosim.rdl`、`csr.sv`、`csr_pkg.sv`、`csr.h`、`csr_hw.h`、`csr_cosim.h`。

#### 4.2.4 代码实践

**实践目标**：确认「同一份 `csr.rdl` 生成多种产物」的事实。

**操作步骤**：

1. 打开 [3.build/csr_build/csr.rdl](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/csr.rdl)，找到 L43 的 `addrmap csr {`（RTL 顶层）和 L930 的 `addrmap wireguard {`（文档顶层）。
2. 打开 [3.build/MakefileCSR](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/MakefileCSR)，对照 L34-41，看 `rtl` 目标用 `--top csr`、而 `html`/`markdown` 目标用 `--top wireguard`。

**需要观察的现象**：同一份 RDL 文件里存在两个 addrmap，分别服务不同产物。

**预期结果**：你会理解为什么 `RDLCSRTOP` 与 `RDLWGTOP` 是两个不同的名字——RTL 只综合 `csr` 这个子集，而文档可以覆盖 `wireguard` 这个更大的顶层。

> 注：本实践的 `make -f MakefileCSR` 需要安装 `peakrdl` 与 `systemrdl-compiler`（见 `0.doc/1.README.Tool-Installs.txt`）。若环境未就绪，则做「源码阅读型实践」即可——对照阅读同样能验证结论。

#### 4.2.5 小练习与答案

**练习**：为什么 `csr_cosim.rdl` 要用 `sed` 删掉 `buffer_writes` 行，而不是直接用原始 `csr.rdl` 生成 c-header？

> **参考答案**：因为 `systemrdl-compiler`（生成 c-header 的底层工具）不认识 `buffer_writes`/`wbuffer_trigger` 这些 PeakRDL 扩展语法，直接喂会报错。但这些语法只用于 RTL 生成，对 c-header 无用，所以过滤掉即可（见 [3.build/MakefileCSR:28-29](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/MakefileCSR#L28-L29) 与 README L22）。

---

### 4.3 SW 构建步骤：RISC-V 交叉编译与 IMEM 生成

#### 4.3.1 概念说明

本项目的控制面（处理 WireGuard 握手、路由、CLI）跑在一个 32 位 RISC-V 软 CPU 上。这些 C/C++ 代码不能在你的 x86 PC 上直接运行，必须用**交叉编译器**翻译成 RISC-V 机器码。但编译出来的二进制还要被「装进」FPGA 的指令存储器里——这就是 `imem.INIT.vh` 的来历：它把裸二进制包装成 Verilog 能识别的内存初始化语句。

#### 4.3.2 核心流程

`MakefileSW`（`make -f MakefileSW`）的内部阶段：

1. **`sw_map`**：用 `riscv64-unknown-elf-cpp` 预处理链接脚本 `link_map.lds`，产出 `main.lds`（定义 IMEM/DMEM/CSR 的地址布局）。
2. **`sw_elf`**：用 `riscv64-unknown-elf-g++` 编译并链接所有 `.c/.cpp/.s` 源文件（含第 1 步的 HAL 头），产出 `main.elf`。关键参数 `-march=rv32i -mabi=ilp32` 把目标定为 32 位 RISC-V。
3. **`sw_out`**：用 `objcopy` 把 `main.elf` 转成 `main.hex` / `main.bin` / `main.dump`，再调 `imem.INIT.py` 生成 `imem.INIT.vh`。

#### 4.3.3 源码精读

工具链与编译开关（[3.build/MakefileSW:27-28](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/MakefileSW#L27-L28)）：

```makefile
CROSS = riscv64-unknown-elf-
CFLAGS = -Os -std=c++11 -DTCM=1 -DHARVARD=1 -DUART_TEST
```

`-DHARVARD=1` 表示采用「哈佛架构」——指令内存（IMEM）与数据内存（DMEM）物理分离。编译时还要带上第 1 步的 HAL 头（[3.build/MakefileSW:54-58](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/MakefileSW#L54-L58)）：

```makefile
$(CSR_BLD)/wireguard_regs.h \
$(CSR_BLD)/csr_hw.h \
$(CSR_BLD)/csr.h \
```

> 这正说明：第 2 步依赖第 1 步的产物。

链接时锁定目标架构（[3.build/MakefileSW:77-78](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/MakefileSW#L77-L78)）：

```makefile
   -march=rv32i \
   -mabi=ilp32 \
```

把 ELF 转成各格式，并生成 IMEM 初始化文件的收尾规则（[3.build/MakefileSW:109-113](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/MakefileSW#L109-L113)）：

```makefile
sw_out: sw_elf
	$(CROSS)objcopy -O verilog $(SW_BLD)/main.elf   $(SW_BLD)/main.hex
	$(CROSS)objcopy -O binary  $(SW_BLD)/main.elf   $(SW_BLD)/main.bin
	$(CROSS)objdump -drwC -S   $(SW_BLD)/main.elf > $(SW_BLD)/main.dump
	python3 imem.INIT.py
```

`imem.INIT.py` 做的事很直观：读 `main.bin`，每 4 字节按**小端序**拼成一个 32 位字，写成一行行的初始化语句（[3.build/imem.INIT.py:31-34](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/imem.INIT.py#L31-L34) 与 [3.build/imem.INIT.py:42-44](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/imem.INIT.py#L42-L44)）：

```python
    hex_values.append(struct.unpack("<I", bin_contents[0:4])[0])  # <I = 小端 32 位
...
        imem_text += f"mem['h{addr:04X}] = 32'h{value:08X};\n"
```

最终 `imem.INIT.vh` 里就是一堆 `mem['h0000] = 32'h...;` 这样的行，正好能被 `imem.sv` 的 `\`include` 吃掉。产出的完整文件清单见 README（[3.build/README.md:70-79](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/README.md#L70-L79)）：`main.lds`、`main.map`、`main.elf`、`main.hex`、`main.bin`、`main.dump`、`imem.INIT.vh`。

> **小贴士**：还有一个 `program` 目标（[3.build/MakefileSW:116-118](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/MakefileSW#L116-L118)）调用 `imem.UART.py`，能通过 UART 把新固件在线烧进已运行的板子——这样**改软件不用重新综合硬件**，是调试效率的关键，详见 u2-l5。

#### 4.3.4 代码实践

**实践目标**：理解 `main.bin` 是怎么变成 `imem.INIT.vh` 的。

**操作步骤**：

1. 打开 [3.build/imem.INIT.py](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/imem.INIT.py)。
2. 假设 `main.bin` 的前 8 字节是 `13 00 00 00 93 00 00 00`（这是 RISC-V 的两条 `nop`/`addi` 指令的字节）。
3. 手工按小端序每 4 字节拼一个 32 位字。

**需要观察的现象**：脚本用 `struct.unpack("<I", ...)`，`<` 表示小端，所以字节 `13 00 00 00` 会被解释成 `0x00000013`，而不是 `0x13000000`。

**预期结果**：前两行 `imem.INIT.vh` 应为：
```
mem['h0000] = 32'h00000013;
mem['h0001] = 32'h00000093;
```

**待本地验证**：若你已装好 RISC-V 工具链并跑了 `make -f MakefileSW`，可打开生成的 `3.build/sw_build/imem.INIT.vh` 对照前几行；同时 README L28 会打印 `Bin file size: ... bytes`，可据此估算 IMEM 词数 = 字节数 / 4。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `objcopy` 之后还要专门写一个 `imem.INIT.py`，而不是直接用 `objcopy -O verilog` 产出的 `main.hex`？

> **参考答案**：`objcopy -O verilog` 产出的是带 `@地址` 的通用 hex 格式，而本项目需要的是 `mem['hXXXX] = 32'hXXXXXXXX;` 这种**特定于 `imem.sv` 数组**的赋值语法，且要求 32 位字、小端。格式不匹配，所以用 Python 脚本定制转换。

**练习 2**：`CFLAGS` 里的 `-DHARVARD=1` 对构建意味着什么？

> **参考答案**：它告诉固件「运行在哈佛架构上」，即 IMEM 与 DMEM 分离。这会影响链接脚本 `link_map.lds` 里代码段与数据段的地址分配（详见 u6-l1 的内存映射）。

---

### 4.4 HW 构建步骤：综合 PnR 到 bitstream

#### 4.4.1 概念说明

硬件构建是最后一步：把所有 SystemVerilog（含第 1 步生成的 `csr.sv`、第 2 步生成的 `imem.INIT.vh`）一起喂给 FPGA 厂商工具，经过**综合**（RTL→门电路）和**布局布线 PnR**（门电路→芯片物理资源），最终生成可烧写的 bitstream。

#### 4.4.2 核心流程

`MakefileHW`（Vivado 路径，`make -f MakefileHW`）非常简洁：

1. 进入 Vivado 工程目录 `hw_build.Vivado`。
2. 调用 `vivado -mode batch -source build_bitstream.tcl` 跑批处理脚本完成综合+PnR。
3. 产出 `top.bit`。
4. `program` 目标再调用 `program_fpga.tcl` 把 bitstream 烧进芯片。

#### 4.4.3 源码精读

`MakefileHW` 全文很短，变量与目标一目了然（[3.build/MakefileHW:5-12](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/MakefileHW#L5-L12)）：

```makefile
VIVADO      := vivado
HW_BLD      := $(BLD_DIR)/hw_build.Vivado
PROJECT     := $(HW_BLD)/wireguard.xpr
BITFILE     := $(HW_BLD)/wireguard.runs/impl_1/top.bit
BUILD_TCL   := $(HW_BLD)/build_bitstream.tcl
PROGRAM_TCL := $(HW_BLD)/program_fpga.tcl
```

综合规则（[3.build/MakefileHW:19-26](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/MakefileHW#L19-L26)）：

```makefile
$(BITFILE): $(PROJECT) $(BUILD_TCL)
	cd $(HW_BLD) && \
	$(VIVADO) -mode batch -nolog -nojournal -notrace \
		-source $(notdir $(BUILD_TCL))
```

注意 `$(BITFILE)` 这个目标只依赖 `$(PROJECT)` 和 `$(BUILD_TCL)`，**并没有显式依赖 `imem.INIT.vh`**——这正是 README 要用粗体反复提醒「先编软件」的原因：Makefile 本身不强制这个依赖，顺序要靠人来保证。Vivado 工程文件 `wireguard.xpr` 里确实把 `../sw_build/imem.INIT.vh` 列为了一个源文件（`3.build/hw_build.Vivado/wireguard.xpr:456`），综合时会通过 `imem.sv` 的 `\`include` 把它纳入。

README 给出的运行命令（[3.build/README.md:86-100](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/README.md#L86-L100)）：

```
make -f MakefileHW           # 综合 + PnR + 生成 bitfile
make -f MakefileHW program   # 烧写 FPGA
```

#### 4.4.4 代码实践

**实践目标**：追踪 `imem.INIT.vh` 是怎么被 Vivado 综合吃进去的。

**操作步骤**：

1. 打开 [1.hw/ip.cpu/imem.sv](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.cpu/imem.sv#L89-L91)，看 L90 的 `\`include "imem.INIT.vh"`。
2. 打开 [1.hw/top.filelist:62](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.filelist#L62)，确认 `imem.sv` 是综合文件清单的一员。

**需要观察的现象**：`imem.sv` 既在 filelist 里（被编译），又 `\`include` 了 `imem.INIT.vh`（其内容由第 2 步提供）。

**预期结果**：你理解了「软件二进制如何物理地嵌入硬件 bitstream」——不是运行时加载，而是综合期焊死。

**待本地验证**：实际综合需要 Vivado 与数分钟时间（README L276 实测约 8 分钟）。若无环境，本「源码阅读型实践」已足以验证结论。

#### 4.4.5 小练习与答案

**练习**：`MakefileHW` 的 `$(BITFILE)` 目标没有把 `imem.INIT.vh` 写进依赖列表，这是 bug 还是设计？

> **参考答案**：可视为**已知的设计取舍**，但容易踩坑。`imem.INIT.vh` 由 `imem.sv` 的 `\`include` 间接引入，而 Makefile 无法跟踪 include 依赖。所以 README 用粗体人工提醒（L152、L225-229）。一个更稳健的做法是把 `$(SW_BLD)/imem.INIT.vh` 显式加进 `$(BITFILE)` 的依赖，让 `make` 自动强制先编软件。

---

### 4.5 工具链选择：Vivado vs openXC7

#### 4.5.1 概念说明

第 3 步硬件构建有**两条可选工具链**：

- **Vivado**：Xilinx 官方商业工具，质量（QoR）最高，但闭源、体积大、慢、需许可证。
- **openXC7**：开源工具链（Yosys + nextpnr-xilinx + Project X-Ray），免费、快，但对某些 Xilinx 原语支持不全，QoR 略逊。

本项目同时支持两者，体现「开源优先」的理念（这也是 u1-l1 提到的项目特色）。

#### 4.5.2 核心流程

两条链的入口与产物对比：

| 维度 | Vivado | openXC7 |
|------|--------|---------|
| 入口 Makefile | `3.build/MakefileHW` | `3.build/hw_build.openXC7/Makefile` |
| 综合工具 | Vivado（官方） | Yosys（开源综合） |
| PnR 工具 | Vivado | nextpnr-xilinx |
| bitstream 工具 | Vivado | Project X-Ray |
| 源码预处理 | 直接吃 SystemVerilog | 先 `sv2v` 转 Verilog，再 `extract_modules.py` 拆分 |
| 产物路径 | `hw_build.Vivado/.../top.bit` | `build_artifacts/top.bit` |
| 实测耗时（README） | ~8 分钟 | ~1 分钟 |

openXC7 多了一步 **sv2v 转换**，因为 Yosys 对 SystemVerilog 支持有限，需要先把 `.sv` 转成纯 Verilog。README 的 openXC7 流程（[3.build/README.md:150-197](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/README.md#L150-L197)）：

1. **先编软件**（同样要先有 `imem.INIT.vh`）。
2. `make convert`：sv2v 批量转换 + 模块拆分，产出 `converted/all_converted.v` 与 `converted/extracted/*.v`。
3. `make all`：Yosys 综合 → nextpnr PnR → 生成 `build_artifacts/top.bit`。

openXC7 的 Makefile 同样引用了软件产物（`3.build/hw_build.openXC7/Makefile:111`：`IMEM_INIT = $(BUILD_DIR_ROOT)/sw_build/imem.INIT.vh`），再次印证 SW→HW 的依赖与具体工具链无关。

#### 4.5.3 源码精读

两条链的耗时实测对比（[3.build/README.md:276](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/README.md#L276) 与 [3.build/README.md:286](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/README.md#L286)）：

> Vivado：Build duration (synthesis + PnR): **8 minutes**
> openXC7：Build duration (synthesis + PnR): **1 minute**

但 openXC7 有代价——README L288-291 指出两个 Xilinx 原语不被支持，需要适配或删除：

- **IBUFGDS**：适配为 `IBUFDS + BUFG`。
- **BUFGMUX**：直接删除，导致时钟选择改用 LUT，引发时序问题。

README L314-317 解释了由此产生的功能差异：因为缺 BUFGMUX，时钟走 LUT 产生延迟，部分 ping 请求收不到正确回复（openXC7 的「Test result 2」出现丢包）。

#### 4.5.4 代码实践

**实践目标**：在两条工具链之间做有依据的选择。

**操作步骤**：

1. 打开 [3.build/README.md](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/README.md) 的 `HW Compilation - openXC7` 一节（L102 起）。
2. 找到 `Step 1/2/3`（L150/155/181）与「Key difference」（L314）。
3. 列一张表：什么场景该用 Vivado、什么场景该用 openXC7。

**需要观察的现象**：openXC7 快 8 倍，但 BUFGMUX 缺失会影响时钟完整性。

**预期结果**：你会得出类似结论——**快速迭代/CI 用 openXC7，最终上板验证用 Vivado**。

**待本地验证**：实际跑两条链需要相应工具链安装（openXC7 安装见 L105-106 链接，sv2v 安装见 L120-145）。无环境时，本「源码阅读型实践」结论依然成立。

#### 4.5.5 小练习与答案

**练习**：为什么 openXC7 需要 sv2v，而 Vivado 不需要？

> **参考答案**：Yosys（openXC7 的综合器）对 SystemVerilog 的高级特性（如 interface、包、部分 typedef）支持不全，必须先用 `sv2v` 把 `.sv` 转成纯 Verilog 才能综合。Vivado 是官方工具，原生支持 SystemVerilog，无需转换。

---

## 5. 综合实践

**任务**：为三个 Makefile 各画一张「输入 → 工具 → 输出」三栏表，并标注它们之间的先后顺序与耦合点。这是本讲指定的主实践任务，能把前面 4 个模块串成一张完整的构建地图。

**操作步骤**：

1. 分别阅读 [3.build/MakefileCSR](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/MakefileCSR)、[3.build/MakefileSW](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/MakefileSW)、[3.build/MakefileHW](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/MakefileHW)，对每个 Makefile 列出：
   - **输入文件**（它读了哪些上游产物）
   - **调用工具**（peakrdl / riscv64-gcc / vivado / yosys 等）
   - **输出文件**（它产出了什么、放在哪个目录）
2. 在三张表之间画箭头，标注耦合文件名（`csr_hw.h`、`csr.h`、`csr.sv`、`imem.INIT.vh`）。

**参考答案（输入/输出清单）**：

| 步骤 | Makefile | 主要输入 | 调用工具 | 主要输出 |
|------|----------|----------|----------|----------|
| ① CSR | MakefileCSR | `csr_build/csr.rdl` | peakrdl regblock/c-header/html/markdown、sysrdl_cosim.py、sed | `csr.sv`、`csr_pkg.sv`、`csr.h`、`csr_hw.h`、`csr_cosim.h`、`csr_cosim.rdl`（均在 `generated-files/`） |
| ② SW | MakefileSW | `2.sw/app/*`、`boot_crt.s`、`link_map.lds`、`csr_hw.h`、`csr.h` | riscv64-unknown-elf-g++/cpp/objcopy、imem.INIT.py | `main.elf/hex/bin/dump/lds/map`、`imem.INIT.vh`（均在 `sw_build/`） |
| ③ HW | MakefileHW | `wireguard.xpr`、`build_bitstream.tcl`、`csr.sv`、`imem.INIT.vh`、所有 `1.hw` 源 | vivado（或 openXC7: sv2v+yosys+nextpnr） | `top.bit` |

**耦合箭头**：

- `csr.rdl` →(MakefileCSR)→ `csr_hw.h`/`csr.h` ──喂给──▶ ②SW
- `csr.rdl` →(MakefileCSR)→ `csr.sv`/`csr_pkg.sv` ──喂给──▶ ③HW
- `main.bin` →(imem.INIT.py)→ `imem.INIT.vh` ──喂给──▶ ③HW（经 `imem.sv` 的 `\`include`）

**先后顺序**：①CSR → ②SW → ③HW（SW 同时依赖 CSR 的头，HW 同时依赖 CSR 的 RTL 和 SW 的 `imem.INIT.vh`）。

## 6. 本讲小结

- wireguard-fpga 的构建是一条 **CSR → SW → HW** 的单向依赖链，由 `3.build/` 下三个 Makefile 编排。
- **第 1 步 CSR**：以 `csr.rdl` 为单一真源，用 `peakrdl` 生成硬件 RTL（`csr.sv`）和软件 HAL（`csr_hw.h`/`csr_cosim.h`），一份规格喂两边。
- **第 2 步 SW**：用 RISC-V 交叉编译器（`riscv64-unknown-elf-`，`-march=rv32i`）把固件编成 `main.elf/bin`，再用 `imem.INIT.py` 包装成 `imem.INIT.vh`。
- **第 3 步 HW**：调用 Vivado 综合 PnR 生成 `top.bit`；也可走开源 openXC7 链（sv2v→Yosys→nextpnr），更快但 BUFGMUX 缺失影响时序。
- **SW 必须先于 HW**：因为 `imem.sv` 在综合期 `\`include "imem.INIT.vh"`，把软件二进制焊死进指令存储器——这是两步之间唯一的物理耦合点。
- CSR 产物同时供给 SW（头文件）和 HW（RTL），是整个软硬件协同的根基，下一单元 U2/U3 会深入它的内部。

## 7. 下一步学习建议

- 想看 CSR 内部到底定义了哪些寄存器？→ 继续读 [3.build/csr_build/csr.rdl](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/csr.rdl)，U3（u3-l1）会系统讲解 SystemRDL 语法。
- 想看构建产物如何拼成完整芯片？→ 读 [1.hw/top.sv](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.sv) 与 [1.hw/top.filelist](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.filelist)，U2（u2-l2）拆解顶层模块。
- 想理解 UART 在线烧写如何省去重综合？→ 看 [3.build/imem.UART.py](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/imem.UART.py) 与 u2-l5。
- 下一讲 u1-l5 将从「构建」走向「运行」：教你上板、连 UART CLI、配置网络与密钥、用 ping 验证加密隧道。
