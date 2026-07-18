# PicoSoC：一个完整的示例 SoC

## 1. 本讲目标

前 7 个单元我们一直在拆解 PicoRV32 这颗 CPU **内部**是怎么工作的——译码、状态机、寄存器堆、ALU、内存接口、PCPI、中断、AXI/Wishbone 适配、压缩指令。本讲换个视角，把镜头**拉远**：不再看 CPU 内部，而是看「CPU 加上 surrounding 外设」如何组成一台能独立运行的最小计算机——**SoC（System on Chip）**。

PicoSoC 是作者提供的一个 turn-key（开箱即用）示例，学完本讲你应当能够：

1. 读懂 [picosoc.v](picosoc/picosoc.v) 顶层，说清 picorv32、SRAM、SPI flash 控制器（spimemio）、UART（simpleuart）和用户外设（iomem）这五块如何挂在**同一条总线**上。
2. 复述 PicoSoC 的**内存映射**，并能追踪「复位后 CPU 从 `0x00100000` 取到第一条指令」的完整硬件路径——这条路径把你在 u3-l1 学的 `PROGADDR_RESET`、u5-l3 学的 `mem_valid/mem_ready` 握手、本讲的地址译码和 SPI 控制器**串成一条链**。
3. 对比 [hx8kdemo.v](picosoc/hx8kdemo.v) 与 [icebreaker.v](picosoc/icebreaker.v) 两块 FPGA 板级封装的差异，理解什么是「SoC 顶层」、什么是「板级封装（board wrapper）」。

## 2. 前置知识

本讲会用到前几个单元已经建立的几条结论，这里只做一句话回顾，不展开：

- **原生内存接口握手**（u5-l3）：CPU 用 `mem_valid`/`mem_ready` 同拍为高表示一次事务成交，`mem_wstrb` 是 4 位字节写使能兼读写区分位，`mem_addr`/`mem_wdata`/`mem_rdata` 是地址与数据。PicoSoC 直接复用这套原生接口（**不**走 AXI/Wishbone）。
- **`PROGADDR_RESET` 参数**（u3-l1）：复位释放后 CPU 把 `reg_pc` 设为该值并从这里取第一条指令。PicoSoC 把它配成 `0x00100000`。
- **`STACKADDR` 参数**（u3-l1/u4-l2）：复位时 CPU「伪造一次对 x2 的写回」来初始化栈指针 `sp`，固件不必自己设。
- **`ENABLE_IRQ`/PCPI/乘除法**（u6）：PicoSoC 默认开启中断、乘法、除法、桶形移位器，让示例固件能跑「正常」的 C 程序。
- **冯·诺依曼结构**：CPU 不区分指令与数据访问，全部走同一套 `mem_*` 接口，由地址决定命中哪个外设。

几个本讲新引入的术语：

- **SoC（片上系统）**：把 CPU、存储、外设控制器集成在同一片芯片/设计里的完整系统。
- **内存映射（memory map）**：把整个地址空间划分成若干区间，每个区间对应一个物理资源（SRAM、flash、寄存器）。
- **片选（CS）/串行时钟（SCK）**：SPI 总线的两根控制线，CS 拉低选中芯片，SCK 提供位传输节拍。
- **XIP（eXecute In Place，就地执行）**：代码不拷进 RAM 再执行，而是直接从 flash 取指执行。PicoSoC 的招牌特性就是 XIP。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
| --- | --- | --- |
| [picosoc/picosoc.v](picosoc/picosoc.v) | SoC 顶层 | 地址译码、五大组件实例化、参数透传 |
| [picosoc/spimemio.v](picosoc/spimemio.v) | SPI flash 控制器 | 把 `mem_*` 读事务翻译成 SPI 读命令、支持标准/双线/四线/DDR |
| [picosoc/simpleuart.v](picosoc/simpleuart.v) | UART 核 | 波特率分频、收发状态机、两个 MMIO 寄存器 |
| [picosoc/hx8kdemo.v](picosoc/hx8kdemo.v) | iCE40-HX8K 板级封装 | 复位展宽、SB_IO 双向引脚、GPIO 外设、调试引脚 |
| [picosoc/icebreaker.v](picosoc/icebreaker.v) | iCEBreaker 板级封装 | 用 SPRAM、参数覆盖、RGB LED 映射 |
| [picosoc/README.md](picosoc/README.md) | PicoSoC 文档 | 内存映射表、配置寄存器位定义、构建命令 |
| [picosoc/ice40up5k_spram.v](picosoc/ice40up5k_spram.v) | iCE40 UP5K 专用 SPRAM 包装 | 用 4 块 `SB_SPRAM256KA` 拼 128 KB |
| [picosoc/start.s](picosoc/start.s) | 启动汇编 | 复位后第一条指令、清零内存、搬数据、调 `main` |
| [picosoc/sections.lds](picosoc/sections.lds) | 链接脚本 | FLASH 起点 `0x00100000`、RAM 起点 `0` |

## 4. 核心概念与源码讲解

### 4.1 SoC 顶层集成

#### 4.1.1 概念说明

PicoSoC 的顶层模块叫 `picosoc`，它的职责只有一件事：**把 CPU 和四个同伴挂在同一条「原生内存总线」上，并按地址把每次访问路由到正确的同伴**。它本身不做任何计算，是一个纯粹的「连线 + 多路选择器」模块。

五个同伴分别是：

1. **picorv32 CPU**——唯一的计算核心。
2. **SRAM**——一块小的可读写 scratchpad（默认 256 字 = 1 KB），存栈和 `.data`。
3. **spimemio**——SPI flash 控制器，把对 flash 区间的读访问翻译成 SPI 总线时序，实现 XIP。
4. **simpleuart**——串口，用两个 MMIO 寄存器（分频器、数据口）收发字节。
5. **iomem 用户外设**——通过一组 `iomem_*` 端口**引出到顶层之外**，由板级封装接 LED/GPIO 等。

注意 `picosoc` 模块**不**实例化 flash 芯片本身——flash 在 FPGA 板上是颗外接的真实芯片，`picosoc` 只输出 `flash_csb/flash_clk/flash_io0..3` 这些引脚连到芯片。这也意味着 `picosoc` 是「工艺/芯片无关」的，可以被任何有 SPI flash 的板子复用。

#### 4.1.2 核心流程

一次 CPU 访问在 `picosoc` 内部的路由流程：

```text
CPU 发起 mem_valid=1, mem_addr=A, mem_wstrb=W, mem_wdata=D
        │
        ├─ A[31:24] > 0x01  ?  ──→ iomem（用户外设区 0x0200_0000 及以上）
        │
        ├─ A == 0x0200_0000 ?  ──→ spimemio 配置寄存器
        ├─ A == 0x0200_0004 ?  ──→ UART 分频寄存器
        ├─ A == 0x0200_0008 ?  ──→ UART 数据寄存器
        │
        ├─ A < 4*MEM_WORDS  ?  ──→ SRAM（scratchpad）
        │
        └─ 4*MEM_WORDS ≤ A < 0x0200_0000 ? ──→ spimemio（flash XIP 区）
                                              （含 0x0100_0000 起的镜像）

任一被选中者拉高自己的 ready 并提供 rdata；
顶层用「优先级或 + 优先级选择」汇成 mem_ready / mem_rdata 回 CPU。
```

关键点：**同一时刻只有一个同伴应答**。设计上靠地址互斥的译码保证这一点；任一拍 `mem_ready` 由「命中的那个同伴」置 1。

#### 4.1.3 源码精读

**① 文件读取顺序与可替换的寄存器堆/存储宏**

[picosoc/picosoc.v:L20-L34](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picosoc/picosoc.v#L20-L34) 在模块定义之前先定义了两个宏，作用是让 picorv32 用本文件提供的 `picosoc_regs` 作为寄存器堆实现，并允许后续文件（如 icebreaker）把存储宏 `PICOSOC_MEM` 替换成专用的 SPRAM。注意第 22 行的 `` `error `` ——它强制要求「picosoc.v 必须先于 picorv32.v 被读取」，否则宏来不及定义。

```verilog
`define PICORV32_REGS picosoc_regs   // 告诉 picorv32.v：寄存器堆用我这个模块
`define PICOSOC_MEM picosoc_mem      // 默认存储模块；icebreaker 会覆盖它
`define PICOSOC_V                    // 供别的文件检查读取顺序
```

**② 顶层端口**

[picosoc/picosoc.v:L36-L71](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picosoc/picosoc.v#L36-L71) 列出顶层对外信号：`clk`/`resetn`、`iomem_*`（用户外设总线）、`irq_5/6/7`（外部中断）、`ser_tx/ser_rx`（串口）、`flash_*`（SPI 引脚）。这组端口就是「板级封装」要驱动的东西。

**③ 参数：CPU 配置 + 系统地址**

[picosoc/picosoc.v:L72-L83](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picosoc/picosoc.v#L72-L83) 集中定义了 SoC 级参数。前 7 个直接透传给 CPU（呼应 u3-l1 的参数体系），后 3 个是系统地址：

```verilog
parameter [0:0] BARREL_SHIFTER = 1;     // CPU 用桶形移位（单周期）
parameter [0:0] ENABLE_MUL = 1;         // 开硬件乘法
parameter [0:0] ENABLE_DIV = 1;         // 开硬件除法
parameter [0:0] ENABLE_FAST_MUL = 0;    // 不用单周期硬乘法器
parameter [0:0] ENABLE_COMPRESSED = 1;  // 开 RV32C 压缩指令
...
parameter integer MEM_WORDS = 256;                    // SRAM 容量（字）
parameter [31:0] STACKADDR = (4*MEM_WORDS);           // 栈顶 = SRAM 末尾
parameter [31:0] PROGADDR_RESET = 32'h 0010_0000;     // 复位取指地址：flash 偏移 1MB
parameter [31:0] PROGADDR_IRQ   = 32'h 0000_0000;     // 中断向量地址
```

`PROGADDR_RESET = 0x00100000` 是本讲的「钥匙」——它决定了复位后第一条指令要从 flash 的 1MB 偏移处取，这正是 4.2 节要追踪的启动路径起点。

**④ 中断聚合**

[picosoc/picosoc.v:L85-L96](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picosoc/picosoc.v#L85-L96) 用一个组合 `always @*` 把内部源（stall/uart）与外部 `irq_5/6/7` 拼成 32 位 `irq` 向量交给 CPU。bit3/4 留给内部，bit5/6/7 接外部引脚，bit0/1/2 是 CPU 内置的定时器/非法指令/总线错误（见 u6-l2）。

**⑤ 地址译码与 ready/rdata 汇流**

这是顶层最核心的一段。[picosoc/picosoc.v:L112-L132](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picosoc/picosoc.v#L112-L132):

```verilog
// 用户外设：地址高字节 > 0x01（即 >= 0x0200_0000）且不属于配置寄存器时引出
assign iomem_valid = mem_valid && (mem_addr[31:24] > 8'h 01);

// 三个 MMIO 配置寄存器的片选
wire spimemio_cfgreg_sel   = mem_valid && (mem_addr == 32'h 0200_0000);
wire simpleuart_reg_div_sel= mem_valid && (mem_addr == 32'h 0200_0004);
wire simpleuart_reg_dat_sel= mem_valid && (mem_addr == 32'h 0200_0008);

// ready = 任一被选中者（优先级或）
assign mem_ready = (iomem_valid && iomem_ready) || spimem_ready || ram_ready
                || spimemio_cfgreg_sel || simpleuart_reg_div_sel
                || (simpleuart_reg_dat_sel && !simpleuart_reg_dat_wait);

// rdata = 用嵌套三元按优先级选一个
assign mem_rdata = (iomem_valid && iomem_ready) ? iomem_rdata :
                   spimem_ready ? spimem_rdata :
                   ram_ready   ? ram_rdata :
                   spimemio_cfgreg_sel   ? spimemio_cfgreg_do :
                   simpleuart_reg_div_sel? simpleuart_reg_div_do :
                   simpleuart_reg_dat_sel? simpleuart_reg_dat_do : 32'h0;
```

注意 UART 数据口（`0x02000008`）的 ready 条件多了 `!simpleuart_reg_dat_wait`——当发送缓冲还满时（`reg_dat_wait`），不拉 ready，CPU 自然停住等，这正是 u5-l3「ready 不来 CPU 就停」的握手语义。

**⑥ CPU 实例化**

[picosoc/picosoc.v:L134-L157](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picosoc/picosoc.v#L134-L157) 把参数透传给 picorv32，并固定打开 `ENABLE_IRQ(1)`（SoC 必须支持中断）。这里实例化的是**原生接口**的 `picorv32`，**不是** `picorv32_axi`/`picorv32_wb`——PicoSoC 用最简单的那套总线。

**⑦ spimemio / simpleuart / memory 实例化**

[picosoc/picosoc.v:L159-L188](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picosoc/picosoc.v#L159-L188) 实例化 spimemio，其 `valid` 条件是 `mem_addr >= 4*MEM_WORDS && mem_addr < 0x02000000`——SRAM 之上、外设区之下的整段都归 flash XIP（包含 README 说的 0x01000000 镜像区）。

[picosoc/picosoc.v:L208-L219](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picosoc/picosoc.v#L208-L219) 给 SRAM：`ram_ready` 是个寄存器，在「未被别处应答且地址落在 SRAM 范围」时下一拍置 1；存储实体由宏 `PICOSOC_MEM`（默认 `picosoc_mem`）实例化。

[picosoc/picosoc.v:L243-L261](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picosoc/picosoc.v#L243-L261) 是默认存储模块 `picosoc_mem`——一个带 4 位字节写使能的简单双口 RAM，注释（[L222-L223](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picosoc/picosoc.v#L222-L223)）明确说「换成你自己的 SRAM 单元包装」。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：把 `picosoc` 当黑盒，理清一次访问的「谁应答」决策。
2. **步骤**：打开 [picosoc.v](picosoc/picosoc.v) 的 L98–L132，对下表每一行地址，判断 `mem_ready` 由哪一项置 1、`mem_rdata` 选的是谁（设 `MEM_WORDS=256`，故 `4*MEM_WORDS=0x400`）：

   | `mem_addr` | 命中者 | ready 来源 |
   | --- | --- | --- |
   | `0x00000100` | SRAM | `ram_ready` |
   | `0x00001000` | flash XIP | `spimem_ready` |
   | `0x00100000` | flash XIP（复位取指地址） | `spimem_ready` |
   | `0x02000000` | spimemio 配置寄存器 | `spimemio_cfgreg_sel` |
   | `0x02000008`（读） | UART 数据口 | `simpleuart_reg_dat_sel && !wait` |
   | `0x03000000` | iomem（用户外设） | `iomem_valid && iomem_ready` |

3. **观察现象**：注意 `0x00001000` 落在「SRAM 物理容量之外」却仍归 spimemio——这正是 README 所说「读 SRAM 区超出物理容量的地址会落到对应 flash 地址」的实现根源。
4. **预期结果**：你能用一句话讲清「地址高字节 + 是否低于 0x400」这两个条件如何二选一切分 SRAM 与 flash。
5. 「待本地验证」：若想实测，可用 `make hx8ksim` 跑仿真（见 4.3 节），在 VCD 里抓 `mem_addr=0x00100000` 那拍观察 `spimem_ready` 的时序。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `iomem_valid` 的条件是 `mem_addr[31:24] > 8'h01` 而不是 `>= 8'h02`？二者等价吗？
**答案**：完全等价。`mem_addr[31:24]` 是 8 位无符号整数，`> 0x01` 与 `>= 0x02` 对无符号数是同一回事，都表示地址 ≥ `0x02000000`。作者写 `> 0x01` 只是一种风格。

**练习 2**：`mem_rdata` 的选择用嵌套三元表达式，如果同一拍有两个源都声称自己 ready 会怎样？
**答案**：由于地址译码互斥（同一地址不会同时命中 SRAM 和 flash 等），正常情况下不会有两个源同时有效。嵌套三元本身有固定优先级（前者胜出），是一种防御性写法；真正防止冲突的是上层的地址译码互斥保证。

### 4.2 内存映射与 SPI 启动

#### 4.2.1 概念说明

**内存映射**是把一维的 32 位地址空间切成若干区间，每个区间绑定一个物理资源。PicoSoC 的内存映射（来自 [README](picosoc/README.md)）非常紧凑：

| 地址区间 | 资源 | 备注 |
| --- | --- | --- |
| `0x00000000 .. 0x00FFFFFF` | 内部 SRAM（底层）+ flash 镜像（超出 SRAM 部分） | SRAM 默认仅 1 KB |
| `0x01000000 .. 0x01FFFFFF` | 外部 SPI flash | 复位取指在这里 |
| `0x02000000` | SPI 控制器**配置寄存器** | 选模式/位敲 |
| `0x02000004` | UART **波特率分频**寄存器 | `clk / baud` |
| `0x02000008` | UART **数据**寄存器 | 读=接收，写=发送 |
| `0x03000000 .. 0xFFFFFFFF` | 用户外设（iomem） | 板级自定义，如 LED |

**SPI 启动（XIP）**的核心难点：CPU 只会发标准的 `mem_valid/mem_ready` 读请求，但 flash 是一颗挂在 SPI 总线上的串行芯片，需要「先发命令、再发地址、再读数据」。`spimemio` 就是这个翻译器：对 CPU 伪装成一块「慢一点但能读的内存」，对 flash 则老老实实地驱动 SPI 时序。

#### 4.2.2 核心流程

**复位后取第一条指令的全链路**（本讲最该记住的一条链）：

```text
① resetn 释放 → CPU 置 reg_pc = PROGADDR_RESET = 0x00100000（u3-l1/u4-l2）
② CPU 发取指：mem_valid=1, mem_addr=0x00100000, mem_wstrb=0（读）
③ picosoc 译码：
     - addr[31:24]=0x00, 不 > 0x01        → iomem_valid=0
     - 不等于 0x0200_0000/04/08            → 配置寄存器都不命中
     - 0x00100000 >= 0x400 且 < 0x0200_0000 → spimemio.valid=1, addr[23:0]=0x100000
     - ram_ready=0（地址不在 SRAM）
④ spimemio 状态机（首次访问）从头跑 13 个状态：
     - state 0/2：发 0xFF、0xAB（唤醒 flash，退出掉电模式）
     - state 4  ：按 {DDR,QSPI} 选读命令，默认标准 SPI → 发 0x03
     - state 5/6/7：发 24 位地址 0x10,0x00,0x00
     - state 8  ：发 mode byte（仅 QSPI/DDR 模式）
     - state 9..12：读回 4 字节，用 tag 1/2/3/4 拼成 32 位指令字
     - ready <= valid && (addr==rd_addr) && rd_valid  → 拉高
⑤ picosoc：mem_ready = spimem_ready = 1
            mem_rdata = spimem_rdata（拼好的 32 位指令）
⑥ CPU 收到指令 → 译码器（u4-l1）→ 这就是 start.s 里 start: 的第一条 addi x1,zero,0
   （链接脚本把 .text 起点固定在 FLASH=0x00100000）
```

第 ④ 步把 `spimemio` 当作一个「命令序列发生器」：每来一笔新读请求，它先发完整的一组 SPI 命令把数据读回来；连续顺序访问时它会**复用**上次的读流水线（见 `rd_inc`、`jump` 逻辑）。

读延迟的数学：标准 SPI 每个时钟传 1 位，读 32 位指令需 32 个 SCK 周期 + 命令/地址开销；若开 QSPI（4 位并行）则约 1/4。README 给了一张 [performance.png](picosoc/performance.png) 直观对比各模式的相对吞吐。

#### 4.2.3 源码精读

**① 启动地址 = flash 偏移 1MB**

[picosoc/picosoc.v:L82](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picosoc/picosoc.v#L82) 把 `PROGADDR_RESET` 设为 `0x00100000`；[picosoc/sections.lds:L11](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picosoc/sections.lds#L11) 把 `FLASH` 段起点也设为 `0x00100000`。两端对齐，才保证「复位取指 = start.s 第一条指令」。

**② flash 区间路由给 spimemio**

[picosoc/picosoc.v:L162](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picosoc/picosoc.v#L162) 的 `valid` 表达式决定了 spimemio 响应哪些地址：

```verilog
.valid  (mem_valid && mem_addr >= 4*MEM_WORDS && mem_addr < 32'h 0200_0000),
.addr   (mem_addr[23:0]),   // 只取低 24 位给 flash（最多 16MB）
```

`0x00100000` 满足 `>= 0x400` 且 `< 0x02000000`，故命中；`mem_addr[23:0] = 0x100000` 就是发给 flash 的 24 位地址。

**③ spimemio 命中判定与跳转检测**

[spimemio.v:L71-L72](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picosoc/spimemio.v#L71-L72):

```verilog
assign ready = valid && (addr == rd_addr) && rd_valid;           // 命中已读好的字
wire jump = valid && !ready && (addr != rd_addr+4) && rd_valid;   // 非顺序访问
```

顺序取指（`addr == rd_addr+4`）能命中已预读的数据；非顺序跳转（函数调用、分支）触发 `jump`，[spimemio.v:L361-L373](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picosoc/spimemio.v#L361-L373) 会重置读流水线。

**④ 命令序列状态机**

[spimemio.v:L235-L359](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picosoc/spimemio.v#L235-L359) 是 13 态（state 0–12）的命令序列发生器，关键几步：

- [L266-L280](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picosoc/spimemio.v#L266-L280) state 4：按 `{config_ddr, config_qspi}` 选读命令字节——`0x03`(标准)/`0xBB`(双线)/`0xEB`(四线)/`0xED`(DDR 四线)，对应 README 的命令表。
- [L281-L311](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picosoc/spimemio.v#L281-L311) state 5/6/7：依次发地址高、中、低字节。
- [L313-L323](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picosoc/spimemio.v#L313-L323) state 8：发 mode byte（`0xFF` 或 CRM 模式的 `0xA5`），仅 QSPI/DDR。
- [L324-L358](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picosoc/spimemio.v#L324-L358) state 9–12：读 4 字节，用 `din_tag=1/2/3/4` 标记，在 [L221-L230](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picosoc/spimemio.v#L221-L230) 拼装：`rdata <= {dout_data, buffer}`（小端，第 4 字节是最高字节）。

**⑤ 底层位移：spimemio_xfer**

[spimemio.v:L378-L579](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picosoc/spimemio.v#L378-L579) 的 `spimemio_xfer` 模块把「8 位字节」翻译成「SPI 引脚电平」，用 [L464-L531](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picosoc/spimemio.v#L464-L531) 的 `casez({xfer_ddr,xfer_qspi,xfer_dspi})` 支持 1/2/4 位并行传输。这一层与 flash 芯片的 datasheet 时序严格对应。

**⑥ 配置寄存器：在 MEMIO 与位敲模式间切换**

[spimemio.v:L99-L131](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picosoc/spimemio.v#L99-L131) 维护配置寄存器；[L158-L169](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picosoc/spimemio.v#L158-L169) 用 `config_en` 在「硬件 MEMIO 模式」（`xfer_*` 驱动）与「软件位敲模式」（`config_*` 直接驱动引脚）之间二选一。固件可写 `config_en=0` 后用 [start.s](picosoc/start.s) 里的 `flashio_worker` 手动位敲 SPI（用于发 flash 写使能、擦除等 MEMIO 不支持的命令）。

#### 4.2.4 代码实践（源码阅读 + 仿真型）

1. **目标**：亲眼看到「复位 → SPI 取指 → 第一条指令」的完整路径。
2. **步骤**：
   - 装好 iverilog 后，在 `picosoc/` 目录运行 `make hx8ksim`（[Makefile](picosoc/Makefile) 的 `hx8ksim` 目标）。
   - 它会编译 [hx8kdemo_tb.v](picosoc/hx8kdemo_tb.v)（实例化 `hx8kdemo` + `spiflash` 行为模型，并把 `+firmware=hx8kdemo_fw.hex` 注入 flash 模型），生成 `testbench.vcd`。
   - 用 GTKWave 打开 VCD，按 `clk` 对齐，依次观察：`uut.resetn` 拉高后 → `uut.soc.cpu.mem_valid` 拉高、`mem_addr=0x00100000` → `uut.soc.spimemio.flash_csb` 拉低、`flash_clk` 开始翻转（SPI 命令发出）→ 若干拍后 `spimem_ready` 拉高一拍 → `mem_rdata` 上出现 `start.s` 第一条指令的编码。
3. **观察现象**：你会看到 `mem_addr=0x00100000` 这笔读访问的 `mem_ready` **延迟很多拍**才回来（SPI 慢），这正是 XIP 的代价——取第一条指令远不止 CPI≈4。
4. **预期结果**：`spimemio` state 寄存器从 0 走到 12，最后 `rd_valid=1`；同时 `ser_tx` 后续会按 [hx8kdemo_tb.v:L86-L107](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picosoc/hx8kdemo_tb.v#L86-L107) 的采样逻辑打印出固件输出的字符。
5. 「待本地验证」：若手头没有 `hx8kdemo_fw.hex`（需要 RISC-V 工具链编译 [firmware.c](picosoc/firmware.c)），则退化为纯阅读——按 4.2.2 的 6 步在源码里逐段对照，同样能完成路径追踪。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `PROGADDR_RESET` 是 `0x00100000` 而不是 `0x00000000`？
**答案**：`0x00000000` 是 SRAM 区，复位时 SRAM 内容未定义，没法放代码。flash 的 `0x00000000` 区被 SRAM 覆盖（SRAM 区超出物理容量的部分才透到 flash），而 `0x01000000` 起是纯 flash 镜像区。把复位向量放在 flash 偏移 1MB 处（`0x00100000`），既避开了 SRAM 覆盖，又给配置/引导头留出空间。同时 `iceprog -o 1M`（[Makefile:L32-L34](picosoc/Makefile)）也是把固件烧到 flash 偏移 1MB 处，与该地址对齐。

**练习 2**：连续顺序取指（`addr` 每次 +4）时，spimemio 会重复发完整命令序列吗？
**答案**：不会。`rd_inc` 标志（[spimemio.v:L229](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picosoc/spimemio.v#L229)）在顺序命中时保持为 1，状态机从 state 12 直接回到 state 9（[L355](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picosoc/spimemio.v#L355)）继续读下一字，省掉命令和地址阶段；只有 `jump`（非顺序）才会回到 state 4/5 重发命令。

### 4.3 FPGA 板级封装

#### 4.3.1 概念说明

`picosoc` 是「芯片无关」的——它不知道自己被装在什么板子上、LED 接在哪、flash 芯片是什么型号。**板级封装**（board wrapper）就是把这层「物理现实」补齐的一层薄薄的胶水代码：它实例化 `picosoc`，并完成三件事：

1. **复位展宽**：上电时把 `resetn` 多压低几十拍，等时钟/flash 稳定。
2. **引脚缓冲**：用 FPGA 厂商专用的 IO 原语（如 Lattice 的 `SB_IO`）把 `flash_io0..3` 这些双向引脚连到物理焊盘。
3. **用户外设实现**：实现 `iomem_*` 端口对应的具体外设（如 GPIO 驱动 LED）。

仓库给了两块板的封装：`hx8kdemo`（Lattice iCE40-HX8K）和 `icebreaker`（iCEBreaker，iCE40-UP5K）。两者结构几乎相同，差异恰好凸显了「板级封装」要解决的问题。

#### 4.3.2 核心流程

两块板封装共有的工作流：

```text
外部 clk ──→ reset_cnt[5:0] 计数 ──→ resetn = &(所有位为1)  （展宽约 64 拍）
                          │
                          ▼
flash_io0..3 (inout) ⇄ SB_IO[3:0] 双向缓冲 ⇄ picosoc.flash_io*_oe/do/di
                          │
                          ▼
iomem_* 总线 ──→ 一个 GPIO 寄存器（地址高字节==0x03）──→ leds / RGB LED
                          │
                          ▼
                    实例化 picosoc（参数按板子调整）
```

差异点（在 4.3.3 详述）：复位后 LED 的映射、SRAM 容量与实现、CPU 参数覆盖、是否有调试引脚。

#### 4.3.3 源码精读

**① 复位展宽（两板相同）**

[hx8kdemo.v:L45-L50](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picosoc/hx8kdemo.v#L45-L50)（[icebreaker.v:L50-L55](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picosoc/icebreaker.v#L50-L55) 同）：

```verilog
reg [5:0] reset_cnt = 0;
wire resetn = &reset_cnt;              // 仅当 6 位全为 1 时才释放复位
always @(posedge clk) reset_cnt <= reset_cnt + !resetn;   // 复位期间持续自增
```

上电后 `reset_cnt` 从 0 计数到 `0x3F`（约 64 拍）才把 `resetn` 拉高，给 flash 上电留时间。

**② 双向 SPI 引脚缓冲（两板相同）**

[hx8kdemo.v:L57-L65](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picosoc/hx8kdemo.v#L57-L65) 用 Lattice iCE40 专用的 `SB_IO` 原语把 4 根 `flash_io*` 双向焊盘与 SoC 的 `oe/do/di` 三组内部信号连起来——`oe` 决定焊盘方向（输出 do 还是输入 di）。这是 FPGA 板级封装的典型写法。

**③ 用户外设：GPIO 寄存器（两板相同）**

[hx8kdemo.v:L77-L91](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picosoc/hx8kdemo.v#L77-L91)（[icebreaker.v:L93-L107](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picosoc/icebreaker.v#L93-L107) 同）实现了一个 32 位 `gpio` 寄存器，当地址高字节 `== 0x03`（即 `0x03000000`，对应 iomem 区）时应答：

```verilog
if (iomem_valid && !iomem_ready && iomem_addr[31:24] == 8'h 03) begin
    iomem_ready <= 1;
    iomem_rdata <= gpio;
    if (iomem_wstrb[0]) gpio[ 7: 0] <= iomem_wdata[ 7: 0];   // 字节写使能
    ...
end
```

这正是 `start.s` 里 `li a0, 0x03000000; sw a1, 0(a0)`（[start.s:L39-L41](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picosoc/start.s#L39-L41)）点亮 LED 的落点——`sw` 写到 `0x03000000`，被这个 GPIO 寄存器接住，低字节驱动 LED。

**④ hx8kdemo：用默认参数 + 调试引脚 + 8 个 LED**

[hx8kdemo.v:L93-L128](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picosoc/hx8kdemo.v#L93-L128) 实例化 `picosoc` 时**不覆盖任何 CPU 参数**（沿用默认 `BARREL_SHIFTER=1, ENABLE_MUL=1, ENABLE_DIV=1`），SRAM 用默认 1 KB 分布式 RAM（`picosoc_mem`）。`leds` 直接是 8 位（[L74-L75](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picosoc/hx8kdemo.v#L74-L75)），另外把 `ser_tx`/`flash_*` 引到 `debug_*` 输出（[L130-L138](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picosoc/hx8kdemo.v#L130-L138)）方便观察。

**⑤ icebreaker：覆盖参数 + SPRAM + RGB LED + 强制读取顺序**

这是两板差异最大处。[icebreaker.v:L20-L24](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picosoc/icebreaker.v#L20-L24) **必须在 picosoc.v 之前读取**，并在那时把存储宏替换成 iCE40-UP5K 专用的 SPRAM：

```verilog
`ifdef PICOSOC_V
`error "icebreaker.v must be read before picosoc.v!"   // 顺序检查（方向与 picosoc.v 相反）
`endif
`define PICOSOC_MEM ice40up5k_spram
```

[icebreaker.v:L48-L115](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picosoc/icebreaker.v#L48-L115) 实例化时覆盖了关键参数：

```verilog
parameter integer MEM_WORDS = 32768;          // 128 KB（UP5K 的全部 SPRAM）
picosoc #(
    .BARREL_SHIFTER(0), .ENABLE_MUL(0), .ENABLE_DIV(0),
    .ENABLE_FAST_MUL(1), .MEM_WORDS(MEM_WORDS)
) soc ( ... );
```

——iCEBreaker 用 UP5K 的硬 SPRAM 把 RAM 从 1 KB 扩到 128 KB（[ice40up5k_spram.v](picosoc/ice40up5k_spram.v) 用 4 块 `SB_SPRAM256KA` 拼），并用 `ENABLE_FAST_MUL=1` 换掉乘法实现（UP5K 有专用 DSP 乘法器，单周期更快）。LED 映射也不同：[L59-L66](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picosoc/icebreaker.v#L59-L66) 把 `leds[1..5]` 接 5 个普通 LED，把 `leds[6]/[7]` 接成 RGB LED 的红/绿（`ledr_n`/`ledg_n`，低有效）。

**两板对照速查**：

| 维度 | hx8kdemo | icebreaker |
| --- | --- | --- |
| FPGA | iCE40-HX8K | iCE40-UP5K |
| 读取顺序 | 先于 picorv32.v | 先于 picosoc.v |
| SRAM 实现 | 分布式 RAM（`picosoc_mem`） | 专用 `SB_SPRAM256KA`（`ice40up5k_spram`） |
| SRAM 容量 | 1 KB（256 字） | 128 KB（32768 字） |
| CPU 参数 | 全默认 | `BARREL_SHIFTER=0, MUL=0, DIV=0, FAST_MUL=1` |
| LED | 8 位直出 | 5 个单色 + 1 个 RGB（红/绿） |
| 调试引脚 | 有 `debug_*` | 无 |

#### 4.3.4 代码实践（源码阅读 + 综合型）

1. **目标**：理解板级封装如何「换一颗板子」。
2. **步骤**：
   - 对照 [ice40up5k_spram.v:L35-L89](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picosoc/ice40up5k_spram.v#L35-L89)，看懂 4 块 `SB_SPRAM256KA` 如何用 `addr[14]` 选片（`cs_0`/`cs_1`）拼出 128 KB——每块 16 K × 16 bit，两块拼 32 bit 宽，两组拼 32 K 深。
   - 在 [icebreaker.v:L109-L115](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picosoc/icebreaker.v#L109-L115) 改一个参数（例如把 `ENABLE_FAST_MUL(1)` 改成 `(0)`），预测：综合后资源里少了什么？乘法指令 `mul` 会退化成什么（回顾 u6-l1 的 PCPI 多周期乘法器）？
3. **观察现象**：UP5K 综合报告里 `SB_MAC16`/DSP 用量会随 `ENABLE_FAST_MUL` 变化；关掉后 `mul` 走 `picorv32_pcpi_mul` 多周期协处理器。
4. **预期结果**：你能说出「换板子 = 换存储宏 + 换 IO 原语 + 调 CPU 参数 + 重映射 LED」四件事，`picosoc` 主体一行不改。
5. 「待本地验证」：实际综合需 yosys + nextpnr-ice40；若无工具链，则停留在阅读层面，重点说清 `SB_IO`/`SB_SPRAM256KA` 这两个原语在两板中的角色。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `icebreaker.v` 必须在 `picosoc.v` 之前读取，而 `hx8kdemo.v` 没有这个要求？
**答案**：因为 icebreaker 要在 picosoc.v 读取**之前**把 `` `define PICOSOC_MEM `` 改成 `ice40up5k_spram`，picosoc.v 里实例化存储用的正是这个宏（[picosoc.v:L211](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picosoc/picosoc.v#L211)）；宏必须先于引用处定义才生效。hx8kdemo 沿用默认的 `picosoc_mem`，不需要提前改宏，故无此约束。

**练习 2**：`resetn = &reset_cnt`（6 位归约与）上电后大约保持低电平多少拍？
**答案**：约 63 拍。`reset_cnt` 从 0 自增到 `0x3F`（全 1）共需 63 个时钟，之后 `&reset_cnt` 才为 1 释放复位。这是一种简陋但有效的「上电延时复位」，等 flash 完成上电。

## 5. 综合实践

把本讲三块内容串起来，完成下面这个**阅读 + 绘图 + 复盘**任务：

**任务**：以 [picosoc.v](picosoc/picosoc.v) 为蓝本，手绘一张 PicoSoC 的完整框图，并写一段 200 字左右的「启动叙事」。

1. **画框图**，至少包含：`picorv32`、SRAM（`PICOSOC_MEM`）、`spimemio`、`simpleuart`、`iomem`（GPIO）五个方框；画出 CPU 与各组件共享的 `mem_valid/mem_ready/mem_addr/mem_wdata/mem_rdata/mem_wstrb` 总线；标注 `flash_*`、`ser_tx/rx`、`iomem_*`、`irq_5/6/7` 引到芯片外的箭头。对照 4.1.2 的流程图自查。

2. **写启动叙事**，要求覆盖以下要点（每点都要能在源码里找到出处）：
   - 复位释放后 `reg_pc = 0x00100000`（出处：`PROGADDR_RESET`）；
   - 该地址如何被 `picosoc` 译码到 `spimemio`（出处：`valid` 表达式 L162）；
   - `spimemio` 发出哪些 SPI 命令把第一条指令读回来（出处：状态机 L235–L359）；
   - 读回的 32 位字正好是 `start.s` 的第一条指令（出处：`sections.lds` 的 FLASH 起点）；
   - 之后 `start.s` 依次清寄存器、点亮 LED（写 `0x03000000`）、清内存、搬 `.data`、清 `.bss`、`call main`（出处：[start.s:L3-L91](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picosoc/start.s#L3-L91)）。

3. **复盘**：在你的框图上用三种颜色分别标出「取指路径」「数据访问路径」「外设访问路径」，体会三者共用同一套 `mem_*` 总线、仅靠地址区分的设计——这正是冯·诺依曼 + 内存映射 I/O 的精髓。

完成后再问自己一个问题：如果把 `PROGADDR_RESET` 改回 `0x00000000`，系统还能正常启动吗？为什么？（提示：看 SRAM 区与 flash 的覆盖关系，以及复位时 SRAM 的内容。）

## 6. 本讲小结

- `picosoc` 是一个**纯连线**的 SoC 顶层：把 picorv32、SRAM、spimemio、simpleuart、iomem 五块挂在同一条原生 `mem_*` 总线上，靠**地址译码**互斥地路由每次访问。
- 内存映射分四大区：SRAM+flash（`0x00..`）、纯 flash 镜像（`0x01..`）、MMIO 配置寄存器（`0x02000000/04/08`）、用户外设（`0x03..` 以上）。
- **启动链**：复位 → `reg_pc=0x00100000` → 译码命中 spimemio → SPI 命令序列（`0xFF`/`0xAB` 唤醒 + `0x03` 读 + 24 位地址）→ 拼回 32 位指令 → 正好是 `start.s` 第一条。
- `spimemio` 是 CPU 与 SPI flash 之间的**翻译器**：对内伪装成可读内存（XIP），对外驱动标准/双线/四线/DDR 时序；顺序访问复用读流水线，跳转访问重发命令。
- **板级封装**（hx8kdemo/icebreaker）补齐物理现实：复位展宽、`SB_IO` 双向引脚缓冲、GPIO 外设实现；换板子只需换存储宏 + 调参数 + 重映射 LED，`picosoc` 主体不动。
- 两板最大差异：hx8kdemo 用 1 KB 分布式 RAM + 默认 CPU；icebreaker 用 128 KB SPRAM + `ENABLE_FAST_MUL` + 覆盖存储宏，且必须在 picosoc.v 之前读取。

## 7. 下一步学习建议

本讲把 PicoRV32 放进了一个完整系统。接下来两个方向任选：

1. **向上——跑起来并观测**：学 [u8-l2 仿真测试台与执行追踪](u8-l2-simulation-and-tracing.md)，用 `make hx8ksim` + `testbench.vcd` 实际观察本讲描述的启动链波形，并用 `showtrace.py` 把执行轨迹与 `firmware.elf` 对照，验证你画的框图与启动叙事。
2. **向深——形式化与综合**：学 [u8-l3 形式化验证与综合评估](u8-l3-formal-and-synthesis.md)，看 PicoSoC 背后的 picorv32 如何被 yosys-smtbmc 证明正确，以及 small/regular/large 三档配置在 Slice LUT 上的资源差异。

如果你更关心外设本身，可以继续精读 [simpleuart.v](picosoc/simpleuart.v) 的收发状态机（一个不错的「小型串行协议状态机」练习），或 [spimemio.v](picosoc/spimemio.v) 的 `spimemio_xfer` 位传输层——后者是理解 SPI/QSPI 时序的极佳真实样本。
