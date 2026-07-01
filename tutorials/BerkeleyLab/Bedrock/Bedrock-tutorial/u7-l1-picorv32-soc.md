# picorv32 RISC-V 软核 SoC

## 1. 本讲目标

本讲进入 Bedrock 的「SoC 软核、外设驱动与平台工程集成」单元的第一讲。读者学完后应能：

1. 说清楚 `soc/picorv32` 的四个子目录（`gateware` / `firmware` / `project` / `test`）各装什么、彼此如何协作。
2. 读懂 `rules.mk` 里那条 RISC-V 交叉编译链：一段 C/汇编源码如何被编译、链接成 `.elf`，再被 `objcopy` 与 `hex8tohex32.py` 加工成 FPGA 可加载的 `%32.hex` / `%32.dat`。
3. 理解 `make system_load` 串口 boot 流程：上位机脚本与片上 bootloader 之间用什么握手协议把新固件灌进 RAM。
4. 理解 `lb_bridge` / `lb_merge` 如何让 CPU 与网络侧 localbus 两个总线 master 和平共处，且为何选择「localbus 优先、CPU 让路」的策略。
5. 明白 `spimemio` 在系统启动/取指阶段扮演的存储接口角色。

本讲依赖 u2-l2（localbus 总线）和 u4-l4（Packet Badger）。建议先回顾 localbus 的「无握手、固定延迟读」特性，再看本讲的 CPU 总线桥接。

## 2. 前置知识

- **软核（soft core）**：用可综合 Verilog 描述、可以放进 FPGA 的 CPU 核，与 Intel/Xilinx 出厂的硬核相对。本讲用的是 [PicoRV32](https://github.com/YosysHQ/picorv32)，一个极精简的 RV32I 核。
- **RISC-V 与 RV32IMC**：RISC-V 是开放指令集；`RV32I` 指 32 位基础整数指令集，`M`=乘除法，`C`=压缩指令（16 位编码，省代码空间）。`pico_pack.v` 里把这三项都打开了。
- **交叉编译（cross compile）**：在 x86 主机上生成给 RISC-V 目标机跑的二进制，工具链前缀 `riscv64-unknown-elf-`。
- **picolibc**：面向裸机/嵌入式的精简 C 库，替代 newlib/glibc，`rules.mk` 用 `-specs=picolibc.specs` 让 gcc 自动选用。
- **PicoRV32 原生总线**：一组简单的 valid/ready 信号（`mem_valid`/`mem_ready`/`mem_addr`/`mem_wdata`/`mem_wstrb`/`mem_rdata`），CPU 发 `mem_valid` 后原地等 `mem_ready` 回来——天然支持「拖一拍再应答」，这正是 CPU 能为 localbus 让路的根因。
- **localbus（回顾 u2-l2）**：Bedrock 自家的无握手、固定延迟读片上总线，平时由 UDP 以太网引擎（Packet Badger）当 master 驱动。
- **本地址解码约定**：CPU 把 32 位地址的最高字节 `addr[31:24]` 当作「外设选择号」（BASE_ADDR），用来区分这段访问是去 RAM、UART、SPI 还是 localbus。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [soc/picorv32/README.md](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/soc/picorv32/README.md) | SoC 总说明：支持硬件平台、所需工具、各特性（仿真/外设/总线桥/综合/烧写/串口重载）。 |
| [soc/picorv32/rules.mk](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/soc/picorv32/rules.mk) | 交叉编译 + 仿真/综合的 Make 模式规则，是本讲的「方法学核心」。 |
| [soc/picorv32/gateware/picorv32.v](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/soc/picorv32/gateware/picorv32.v) | PicoRV32 CPU 核本体（上游 Clifford Wolf 的开源代码，原样引入）。 |
| [soc/picorv32/gateware/pico_pack.v](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/soc/picorv32/gateware/pico_pack.v) | CPU 包装层：固定一套参数、把原生总线打包成两根宽线，并处理中断边沿。 |
| [soc/picorv32/gateware/lb_bridge.v](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/soc/picorv32/gateware/lb_bridge.v) | 把 CPU 的打包总线翻译成 localbus master 接口，并在双 master 冲突时让 CPU 让路。 |
| [soc/picorv32/gateware/lb_merge.v](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/soc/picorv32/gateware/lb_merge.v) | 两个 master（CPU 侧 A、网络 localbus 侧 B）合并到一条受控总线，B 永远优先。 |
| [soc/picorv32/gateware/spimemio.v](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/soc/picorv32/gateware/spimemio.v) | SPI Flash 取指/存储控制器（来自 PicoSoC），负责上电后从外部 SPI Flash 拉指令。 |
| [soc/picorv32/common/boot_load.py](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/soc/picorv32/common/boot_load.py) | 上位机串口 bootload 脚本，`make system_load` 调用的就是它。 |
| [soc/picorv32/common/hex8tohex32.py](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/soc/picorv32/common/hex8tohex32.py) | 把 8 位字节流的 Verilog hex 转成 32 位字流的 hex，供 `$readmemh` 加载。 |
| [soc/picorv32/common/bootloader.S](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/soc/picorv32/common/bootloader.S) | 片上串口 bootloader 固件，与 `boot_load.py` 配对握手。 |
| [soc/picorv32/test/lb_bridge/lb_bridge_tb.v](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/soc/picorv32/test/lb_bridge/lb_bridge_tb.v) | 双 master 冲突场景的仿真（当前在 `test/Makefile` 中被注释，待修复）。 |

整体目录划分（README 与 `rules.mk` 的变量一致）：

- `gateware/`：可综合 Verilog，即「硬件」。CPU 核、各种 `*_pack.v` 外设包装、`lb_bridge`、`spimemio` 都在这里。
- `firmware/`：C 库驱动（`inc/*.h` 头 + `src/*.c` 实现），即「固件库」，被各工程 `#include` 复用。
- `project/`：具体上板工程（`cmod_a7`、`kc705` 等），每个再分 `common`/`sim`/`synth`。
- `test/`：每个外设/特性一个子目录，各自有 `_tb.v` + C 测试程序，`make clean all` 跑全部 PASS/FAIL。
- `common/`：跨工程共用的汇编启动代码、链接脚本、bootloader、Python 转换脚本。

## 4. 核心概念与源码讲解

### 4.1 picorv32 软核

#### 4.1.1 概念说明

PicoRV32 是一个用约 2000 行 Verilog 实现的 RV32I 软核，主打「小而慢但可综合」。Bedrock 把它原样引入，作为片上控制器，用来跑那些「纯硬件做太繁琐、但实时性要求又高」的控制任务——例如配置外设寄存器、跑状态机、与上位机协议交互。README 把它定位成一个完整 SoC：

> An open-source system on a chip based on the [PicoRV32](https://github.com/YosysHQ/picorv32) softcore.
> —— [README.md:3](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/soc/picorv32/README.md#L3)

注意区别两个名字：`picorv32.v` 是 **CPU 核本体**；`pico_pack.v` 是 Bedrock 自己加的 **包装层**，固定参数 + 把总线打包。本讲的「软核模块」其实是「核 + 包装」这一对。

#### 4.1.2 核心流程

PicoRV32 的存储访问流程很简单：

1. CPU 把地址/数据/写使能摆到 `mem_addr`/`mem_wdata`/`mem_wstrb` 上，拉高 `mem_valid`。
2. CPU **原地等待**，直到外设回 `mem_ready=1` 才结束这次访问；`mem_rdata` 上是读回的数据。
3. 因为「拉高 valid 后可以任意拍后才回 ready」，外设完全可以让 CPU 等几拍——这是后面 `lb_bridge` 能让 CPU「停住让路」的物理基础。

中断方面：`irq[31:0]` 共 32 路，`pico_pack` 把高 16 路配置成上升沿触发、低 16 路电平触发，复位地址 `PROGADDR_RESET` 默认指向 `0x0`（bootloader），中断向量 `PROGADDR_IRQ` 指向 `0x10`（用户程序）。

#### 4.1.3 源码精读

CPU 核的参数与原生总线端口在 `picorv32.v` 中定义（节选关键几行）：

```verilog
module picorv32 #(
    parameter [ 0:0] COMPRESSED_ISA = 0,
    parameter [ 0:0] ENABLE_IRQ = 0,
    parameter [ 0:0] ENABLE_MUL = 0,
    ...
    parameter [31:0] PROGADDR_RESET = 32'h 0000_0000,
    parameter [31:0] PROGADDR_IRQ = 32'h 0000_0010,
    parameter [31:0] STACKADDR = 32'h ffff_ffff
) (
    input clk, resetn,
    output reg        mem_valid,
    input             mem_ready,
    output reg [31:0] mem_addr,
    output reg [31:0] mem_wdata,
    output reg [ 3:0] mem_wstrb,
    input      [31:0] mem_rdata,
    ...
```

—— [picorv32.v:63-101](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/soc/picorv32/gateware/picorv32.v#L63-L101)：注意 `mem_valid`/`mem_ready` 这对握手，以及 `mem_wstrb`（4 位字节写使能，决定本次是整字写还是单字节写）。

包装层 `pico_pack.v` 把核的端口接全，并固定一组参数：

```verilog
picorv32 #(
    .COMPRESSED_ISA       ( 1              ),// Enable support for compressed instr. set
    .ENABLE_IRQ           ( 1              ),// Enable interrupt controller
    .ENABLE_MUL           ( 1              ),
    .ENABLE_DIV           ( 1              ),
    .BARREL_SHIFTER       ( 1              ),
    .STACKADDR            (`BLOCK_RAM_SIZE ),
    ...
) picorv32_core ( ... );
```

—— [pico_pack.v:62-87](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/soc/picorv32/gateware/pico_pack.v#L62-L87)：`COMPRESSED_ISA=1` 对应 `rules.mk` 里的 `-march=rv32imc`（C 子集）；`STACKADDR` 直接等于 `` `BLOCK_RAM_SIZE `` 宏，让栈顶贴着 RAM 顶端（见 4.3.3）。中断边沿处理在中断输入侧合并：

```verilog
irqFlagsPrev <= irqFlags;
assign irqFlagsRising = irqFlags & ~irqFlagsPrev;
...
.irq ( {irqFlagsRising[31:16], irqFlags[15:0]} ),  // 16 level + 16 rising-edge
```

—— [pico_pack.v:49-92](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/soc/picorv32/gateware/pico_pack.v#L49-L92)。

为了让顶层连线更清爽，`pico_pack` 不直接暴露 6 根总线信号，而是先用 `mpack` 把「CPU→外设」方向拼成一根 69 位线 `mem_packed_fwd`、把「外设→CPU」方向拼成 33 位线 `mem_packed_ret`：

```verilog
assign mem_packed_fwd = { mem_wdata, mem_wstrb, mem_addr, mem_valid };
assign mem_ready      =  mem_packed_ret[  32];
assign mem_rdata      =  mem_packed_ret[31:0];
```

—— [mpack.v:19-21](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/soc/picorv32/gateware/mpack.v#L19-L21)：位宽验证 \(32+4+32+1=69\)，回向 \(1+32=33\)。对应拆包在 `munpack.v` 里做，且 **故意多打一拍寄存器**：

```verilog
wire [32:0] temp = {mem_ready, mem_rdata};
// 1 cycle extra delay
reg[32:0] mem_packed_ret_ = 33'h0;
always @(posedge clk) mem_packed_ret_ <= temp;
assign mem_packed_ret = mem_packed_ret_;
```

—— [munpack.v:31-39](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/soc/picorv32/gateware/munpack.v#L31-L39)：这一拍延迟在算总线时序时必须算进去（与 `lb_bridge` 的 `READ_DELAY` 配合，见 4.2.3）。

#### 4.1.4 代码实践

- **目标**：确认 `pico_pack` 给 CPU 配的是「RV32IMC + 中断 + 乘除法」这一档，并理解打包线位宽。
- **操作步骤**：
  1. 打开 `pico_pack.v`，数清 `picorv32 #(...)` 里打开了哪些 `ENABLE_*`。
  2. 打开 `mpack.v` / `munpack.v`，按拼接位宽手算 `mem_packed_fwd`、`mem_packed_ret` 各多少位，与端口声明 `output [68:0]` / `input [32:0]` 核对。
- **观察现象**：参数里 `COMPRESSED_ISA/MUL/DIV/IRQ` 全为 1，但 `ENABLE_FAST_MUL=0`（用普通 `M` 而非 DSP 快乘）。
- **预期结果**：`mem_packed_fwd` = 69 位、`mem_packed_ret` = 33 位，与声明一致。
- **待本地验证**：若你在综合后看资源报告，可对照「开了乘除法/桶形移位器」预期 LUT 占用上升。

#### 4.1.5 小练习与答案

1. **问**：为什么 `STACKADDR` 要设成 `` `BLOCK_RAM_SIZE `` 而不是固定 `0xFFFFFFFF`？
   **答**：因为程序和数据共享一块 block RAM，栈从这块 RAM 的顶端向下生长；把栈顶设为 RAM 大小（而非全 1 地址），能让栈落在真实存在的 RAM 内，避免访问不存在的高地址空间。`BLOCK_RAM_SIZE` 由各工程 Makefile 注入（如 cmod_a7 为 16384 字节）。
2. **问**：`mem_wstrb` 是 4 位，分别表示什么？
   **答**：32 位字内 4 个字节的独立写使能，某位为 1 才写对应字节；全 1 = 整字写，全 0 = 读操作（`lb_bridge` 正是据此区分读/写，见 4.2.3）。

### 4.2 lb_bridge 总线桥

#### 4.2.1 概念说明

CPU 想当 localbus 的 master 去读写 RF 控制寄存器；可网络侧的 Packet Badger（u4-l4）也想当同一个 localbus 的 master。一条总线两个 master，必须仲裁。Bedrock 的策略写得很直白：

> The philosophy is to put localbus top priority because that `picorv32` is capable of handling retries and yields. This is done by `gateware/lb_bridge.v`.
> —— [README.md:61-68](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/soc/picorv32/README.md#L61-L68)

理由是：CPU 的 valid/ready 总线天然能「等」，让一让无伤大雅；而 localbus 无握手、固定延迟，硬插队会丢数据。所以「网络 localbus 永远优先，CPU 自己重试」。

`lb_bridge` 负责两件事：(a) 把 CPU 的打包总线翻译成 localbus 信号；(b) 当 localbus 被网络侧占用时，把 CPU 的 strobe 门控掉，让 CPU 干等。

#### 4.2.2 核心流程

合并策略在 `lb_merge.v` 顶部那张冲突表里讲得最清楚：

```
//  A  B  Merged  Policy
//  w  w  B_w     A will retry after B finish
//  r  r  B_r     A will retry after B finish
//  r  w  B_w     A will retry after B finish
//  w  r  B_r     A will hold
```

—— [lb_merge.v:1-7](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/soc/picorv32/gateware/lb_merge.v#L1-L7)

A = CPU 侧，B = 网络 localbus 侧。结论一句话：只要 B 想动，A 就让。实现上：

1. `busy = lb_strobe_b`（B 一发 strobe，busy 立刻拉高）。
2. A 的 strobe 被 `& ~busy` 门控：busy 时 A 的 `lb_write`/`lb_read` 被强制为 0，A 的请求停在原地。
3. 合并后的受控总线把 B 的地址/数据优先送出；A 等 busy 掉了再重发（CPU 的 `mem_valid` 一直高着，下一拍自然续上）。

#### 4.2.3 源码精读

先看 `lb_merge` 怎么实现「B 优先」：

```verilog
assign lb_merge_read  = lb_read_a  | lb_read_b;
assign lb_merge_write = lb_write_a | lb_write_b;

wire select_write_a = lb_write_a & ~lb_write_b & ~lb_merge_read;
wire select_read_a  = lb_read_a  & ~lb_read_b  & ~lb_merge_write;
...
assign busy = lb_strobe_b;
assign lb_merge_addr  = (select_write_a | select_read_a) ? lb_addr_a : lb_addr_b;
```

—— [lb_merge.v:39-50](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/soc/picorv32/gateware/lb_merge.v#L39-L50)：只有「A 单独发起、且 B 完全空闲」时才选 A 的地址，否则一律走 B。

再看 `lb_bridge` 怎么翻译 CPU 总线并接 `busy`：

```verilog
wire  [7:0] mem_addr_base = mem_addr[31:24];// Which peripheral   (BASE_ADDR)
...
// only react on 32 bit writes
wire mem_write = mem_valid && !ready_sum &&  (&mem_wstrb) && (mem_addr_base==BASE_ADDR);
wire mem_read  = mem_valid && !ready_sum && !(|mem_wstrb) && (mem_addr_base==BASE_ADDR);

assign lb_write = mem_write & ~busy;
assign lb_read = mem_read & ~busy;
assign lb_addr = mem_addr[ADW+1:2];
```

—— [lb_bridge.v:47-56](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/soc/picorv32/gateware/lb_bridge.v#L47-L56)：三件事——(1) 只认整字写（`&mem_wstrb`）或纯读（`!|mem_wstrb`）；(2) 只在 `addr[31:24]==BASE_ADDR` 时才落到 localbus（CPU 访问别的外设时桥不响应）；(3) `& ~busy` 实现「让路」。

读回用固定延迟（呼应 u4-l2 的 `LB_READ_DELAY` 概念）：

```verilog
// match mem_gateway.v
lb_reading #(.READ_DELAY(READ_DELAY)) reading (
    .clk(clk), .reset(busy), .lb_read(lb_read), .lb_rvalid(lb_rvalid)
);
...
always @(posedge clk) begin
    mem_ready <= lb_write_ready | lb_rvalid;
    mem_rdata <= lb_rvalid ? lb_rdata : 32'd0;
    ...
end
```

—— [lb_bridge.v:58-71](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/soc/picorv32/gateware/lb_bridge.v#L58-L71)：`lb_reading` 把 `lb_read` 沿移位寄存器推迟 `READ_DELAY` 拍出 `lb_rvalid`（见 [lb_reading.v:10-22](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/soc/picorv32/gateware/lb_reading.v#L10-L22)）；`mem_ready` 在写完成或读有效时回高，结束 CPU 这次访问。注意 `reset(busy)`：busy 时直接清读移位寄存器，避免让路期间错吐陈旧读数据。

仿真侧 `lb_bridge_tb.v` 构造了若干「CPU 与 LB 同时发起」的碰撞场景，并断言：碰撞应为 0（说明门控生效）、CPU 写重试 2 次、读重试 3 次：

```verilog
pass = (collisions == 0) && (w_retries == 2) && (r_retries == 3);
```

—— [lb_bridge_tb.v:148-159](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/soc/picorv32/test/lb_bridge/lb_bridge_tb.v#L148-L159)。

> ⚠️ **现状提示**：这个 testbench 目前在 `test/Makefile` 里是被注释掉的，旁边写着 `# TODO lb_bridge: fix & improve PASS / FAIL logic`——[test/Makefile:11](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/soc/picorv32/test/Makefile#L11)。也就是说 `make clean all` 默认不会跑它；要单独验证需手动 `make -C soc/picorv32/test/lb_bridge`，且它当前的 PASS/FAIL 判据还在修复中。

#### 4.2.4 代码实践

- **目标**：用 testbench 直观看见「让路」。
- **操作步骤**：
  1. 阅读 [test/lb_bridge/lb_bridge_tb.v:163-180](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/soc/picorv32/test/lb_bridge/lb_bridge_tb.v#L163-L180) 的碰撞注入序列（`lb1_write_task` / `lb1_read_task` 模拟网络侧 master）。
  2. （需 RISC-V 工具链）`make -C soc/picorv32/test/lb_bridge clean all`，看终端打印的 `=== CPU write retry ===` / `=== CPU read retry ===` 计数。
- **观察现象**：每当 LB master（bus B）与 CPU 同时发起，CPU 的 `mem_write`/`mem_read` 因 `busy` 被压住，日志会记一次 retry；`collision` 始终为 0。
- **预期结果**：终端出现若干 retry 行，且不出现 `=== Collision ===`。
- **待本地验证**：由于该测试 PASS/FAIL 判据待修复，最终是否打印 `PASS` 以本地实际为准；若只想看波形，可 `make -C soc/picorv32/test/lb_bridge lb_bridge.vcd` 后用 gtkwave 打开 `lb_bridge.gtkw`。

#### 4.2.5 小练习与答案

1. **问**：为什么让 CPU 让路、而不是让网络 localbus 让路？
   **答**：localbus 是无握手总线，固定延迟读（`READ_DELAY` 拍后必须取数），一旦开始一次访问无法暂停；而 PicoRV32 的 valid/ready 总线可以无限期等 ready，让 CPU 多等几拍不丢数据、不影响协议。所以让「能等的」让路。
2. **问**：`lb_bridge` 的 `BASE_ADDR` 参数（默认 `8'h00`）有什么用？
   **答**：CPU 用 `addr[31:24]` 选外设，只有等于 `BASE_ADDR` 的访问才被翻译成 localbus 读写；其它地址去 RAM/UART/SPI 等。testbench 里设成 `8'h04`（见 [lb_bridge_tb.v:77](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/soc/picorv32/test/lb_bridge/lb_bridge_tb.v#L77)），所以测试程序里所有目标地址高字节都是 `0x04`。

### 4.3 rules.mk 固件构建

#### 4.3.1 概念说明

`rules.mk` 是本 SoC 的「构建大脑」，一段 Verilog/C/汇编怎么变成 FPGA 能用的东西，全靠它。它定义了一组 Make **模式规则**（用 `%` 通配），把工具链串成流水线：

- 编译：`.c`/`.S` → `.o`
- 链接：`.o` → `.elf`
- 转码：`.elf` → `8.hex` → `32.hex` → `32.dat`
- 烧录：`32.hex` → 串口 `_load`
- 仿真/综合：`_tb`/`_check`/`.vcd`/`_synth.bit`

每个具体工程（如 `project/cmod_a7`）只需 `include` 它，再填几个变量（`TARGET`、`SRC_V`、`OBJS`、`BLOCK_RAM_SIZE`）即可。

#### 4.3.2 核心流程

一条固件从源码到上板的完整链路：

```
test.c ──gcc──> test.o ──ld──> system.elf
                                  │
                  ┌───────────────┼──────── objcopy
                  ▼               ▼
            system8.hex  ──hex8tohex32.py──> system32.hex ──cp──> system32.dat
                  │                                              (Vivado 用)
                  └──────────── boot_load.py ──串口──> 片上 RAM
```

关键转码工具 `hex8tohex32.py`：把 `$readmemh` 能读的「按字节」hex 重排成「按 32 位字、小端」hex，并对齐到 4 字节边界：

```python
print("@%08x" % (ptr >> 2))                       # 字地址 = 字节地址 / 4
...
for word_bytes in zip(*([iter(data)] * 4)):
    print("".join(["%02x" % b for b in reversed(word_bytes)]))  # 小端拼接
```

—— [hex8tohex32.py:9-15](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/soc/picorv32/common/hex8tohex32.py#L9-L15)：`\[ \text{word\_addr} = \lfloor \text{byte\_addr}/4 \rfloor \]`，4 个字节逆序拼成一个 32 位字，匹配 RISC-V 小端存储。

#### 4.3.3 源码精读

交叉编译工具链前缀与 flags：

```makefile
RISCV_TOOLS_PREFIX = riscv64-unknown-elf-
CC      = $(RISCV_TOOLS_PREFIX)gcc
...
CLFLAGS = -march=rv32imc -mabi=ilp32 -ffreestanding -DBLOCK_RAM_SIZE=$(BLOCK_RAM_SIZE) -nostartfiles $(CCSPECS)
CFLAGS  = -std=c99 -Os -Wall -Wextra ... $(CLFLAGS)
LDFLAGS = $(CLFLAGS) -Wl,--strip-debug,--print-memory-usage,-Bstatic,-Map,$*.map,--defsym,BLOCK_RAM_SIZE=$(BLOCK_RAM_SIZE),--gc-sections,--no-relax -T$(filter %.lds, $^)
```

—— [rules.mk:14-21](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/soc/picorv32/rules.mk#L14-L21)：`-march=rv32imc -mabi=ilp32` 与 `pico_pack` 的 `COMPRESSED_ISA` 对齐；`-nostartfiles` 表示用自己的 `startup.S` 而非 crt0；`--gc-sections` 去掉未用代码省 RAM；`--no-relax` 是规避 binutils 已知 bug 的 workaround；`-T…lds` 指定链接脚本。

编译/链接规则：

```makefile
%.o: %.c
	$(CC) $(INC_DIR) $(CFLAGS) -o $@ -c $<
...
%.elf: %.o
	$(CC) $(LDFLAGS) -o $@ $(filter %.o, $^) $(LDLIBS)
	chmod -x $@
```

—— [rules.mk:48-56](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/soc/picorv32/rules.mk#L48-L56)。

固件转码三连：

```makefile
%8.hex: %.elf
	$(RISCV_TOOLS_PREFIX)objcopy $< -O verilog $@

%32.hex: %8.hex
	$(PYTHON) $(COMMON_DIR)/hex8tohex32.py $< > $@

# for vivado in project mode, hex-files need to end with .dat
%32.dat: %32.hex
	cp $< $@
```

—— [rules.mk:31-39](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/soc/picorv32/rules.mk#L31-L39)：`objcopy -O verilog` 产出按字节的 hex；`hex8tohex32.py` 重排成 32 位字；`.dat` 仅是 `.hex` 的拷贝，因为 Vivado 项目模式认 `.dat` 后缀。

串口 boot 规则：

```makefile
%_load: %32.hex
	$(PYTHON) $(COMMON_DIR)/boot_load.py $< $(BOOTLOADER_SERIAL) --baud_rate $(BOOTLOADER_BAUDRATE)
```

—— [rules.mk:41-42](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/soc/picorv32/rules.mk#L41-L42)：所以 `make system_load` 实际执行的是 `boot_load.py system32.hex /dev/ttyUSB1 --baud_rate 115200`（串口名/波特率由工程 Makefile 设定，见 [cmod_a7/synth/Makefile:7-8](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/soc/picorv32/project/cmod_a7/synth/Makefile#L7-L8)）。

链接脚本 `0x000.lds` 把整块 block RAM 定为可读可写可执行，入口标号 `start`：

```ld
MEMORY{
    blockRam (RWXAI) : ORIGIN = 0x00000000, LENGTH = BLOCK_RAM_SIZE
}
ENTRY(start);
```

—— [0x000.lds:5-9](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/soc/picorv32/common/0x000.lds#L5-L9)：`LENGTH = BLOCK_RAM_SIZE` 把 RAM 大小（cmod_a7 为 16384）同时喂给链接器（算内存占用）、汇编器（设栈指针 `STACKADDR`）和综合器（设 block RAM 段大小），README 里说得很清楚——[README.md:91-95](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/soc/picorv32/README.md#L91-L95)。注意还有一份 `0x0e0.lds`：它在前端预留 0xe0 字节给 bootloader（与 `boot_load.py` 的 `--byte_offset 0xe0` 对应），综合/上板工程用它，纯仿真用 `0x000.lds`。

#### 4.3.4 代码实践（本讲主实践）

- **目标**：亲手追完一条 `.elf → %32.hex/%32.dat` 链路，并说清 `make system_load` 的握手。
- **操作步骤**：
  1. 装好工具链：`sudo apt install iverilog gtkwave gcc-riscv64-unknown-elf picolibc-riscv64-unknown-elf`（README 推荐的 Debian 11 方式——[README.md:20-21](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/soc/picorv32/README.md#L20-L21)）。
  2. `cd soc/picorv32/test` 后 `make clean all`，观察哪些子测试 PASS、哪些因缺工具/被注释而跳过。
  3. 任选一个测试（如 `gpio`），手动分解执行：
     - `make -C soc/picorv32/test/gpio gpio.elf`（链接）
     - `make -C soc/picorv32/test/gpio gpio8.hex`（objcopy）
     - `make -C soc/picorv32/test/gpio gpio32.hex`（hex8tohex32.py）
     - `make -C soc/picorv32/test/gpio gpio32.dat`（cp）
     用 `head` 看 `gpio8.hex`（每行一字节）与 `gpio32.hex`（每行一 32 位字、`@字地址`）的差异。
  4. 读 `boot_load.py` 的 `bootload()` 与片上 `bootloader.S` 的握手注释，按顺序对上每一拍。
- **观察现象**：`gpio8.hex` 行多、值小；`gpio32.hex` 行少约 1/4、每行 8 个十六进制字符。
- **预期结果**：`gpio32.hex` 第一行 `@00000000` 起步，字节经小端重排。
- **`make system_load` 流程说明（源码阅读型，无需上板）**：依据 [bootloader.S:1-13](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/soc/picorv32/common/bootloader.S#L1-L13) 与 [boot_load.py:63-115](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/soc/picorv32/common/boot_load.py#L63-L115)，握手是：

  | 步骤 | 上位机 `boot_load.py` | 片上 `bootloader.S` |
  |------|----------------------|---------------------|
  | 1 | （可选）拉 RTS 硬复位；或写 `0x14`(Ctrl+T) 软复位 | 复位后从 `0x0` 进入 bootloader |
  | 2 | 等 `ok\n` | 发 `ok\n`（[bootloader.S:33-39](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/soc/picorv32/common/bootloader.S#L33-L39)） |
  | 3 | 发 `g` | 收到 `g` 才继续，否则超时跳用户程序 |
  | 4 | 等 `o\n` | 发 `o\n` |
  | 5 | 发 4 字节长度 N | 收 N |
  | 6 | 发 N 字节数据 | 写入 `_startup_adr` 起的 RAM |
  | 7 | 读回 N 字节比对 | 回吐同样数据做校验 |

  `read_verilog_hex()` 先把 `32.hex` 还原成字节数组（[boot_load.py:29-52](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/soc/picorv32/common/boot_load.py#L29-L52)），`byte_offset=0xe0` 会把前 0xe0 字节（bootloader 自身）截掉不覆盖——这就是「不重新综合也能换程序」的关键。
- **待本地验证**：`make clean all` 全绿依赖完整工具链与 picolibc；`system_load` 需真实硬件 + 串口。缺工具时对应子测试会失败而非跳过（与 `selftest.sh` 行为不同，因为这里没有 optional 门控）。

#### 4.3.5 小练习与答案

1. **问**：为什么需要 `8.hex` 和 `32.hex` 两份？
   **答**：`objcopy -O verilog` 只能产出按字节的 hex（`8.hex`），但 RISC-V 是 32 位机、RAM 按 32 位字组织、`$readmemh` 仿真加载也按字读。`hex8tohex32.py` 负责把字节流按小端打包成 32 位字并按字地址重排，得到 `32.hex`，才能被 `memory_pack` 的 `$readmemh` 和 Vivado 的 RAM 初始化同时使用。
2. **问**：`make system_load` 会不会覆盖掉 bootloader 自己？
   **答**：不会。`boot_load.py` 默认 `--byte_offset 0xe0`，发送前先 `bin_buffer = bin_buffer[byte_offset:]` 把前 0xe0 字节扔掉；这正好对应 `0x0e0.lds` 给 bootloader 预留的 0xe0 字节空间，所以新程序只写用户区、不动 bootloader。

### 4.4 spimemio 存储接口

#### 4.4.1 概念说明

`spimemio` 来自 PicoSoC，是「让 CPU 从外部 SPI Flash 取指令/数据」的硬件控制器。cmod_a7 这类小板把引导程序存在板载 SPI Flash 里，上电后 `spimemio` 把 CPU 的内存访问翻译成 SPI 读时序，逐字把指令拉进 CPU——相当于一个「透明的 SPI→MEM 桥」。它也能工作在 QSPI/DDR 等高速模式，并通过一组 `cfgreg` 配置寄存器在线切换。

#### 4.4.2 核心流程

1. CPU 发一次取指访问（`valid=1` + `addr`）。
2. `spimemio` 维护一个预取缓冲 `buffer`/`rd_addr`：若 `addr == rd_addr && rd_valid`，说明这一字已在缓冲里，立即 `ready=1` 返回。
3. 否则发起一次 SPI 读（按 `config_qspi`/`config_ddr`/`config_dummy` 等配置），顺序填充后续地址，实现预取加速。
4. 若访问地址跳变（非顺序），触发 `jump`，重新对齐 SPI 流。

#### 4.4.3 源码精读

端口分两半：左边是 CPU 侧的简单 valid/ready 内存接口，右边是 SPI Flash 物理引脚，外加一组 `cfgreg` 配置寄存器：

```verilog
module spimemio (
	input clk, resetn,
	input valid,
	output ready,
	input [23:0] addr,
	output reg [31:0] rdata,
	output flash_csb, output flash_clk,
	output flash_io0_oe, ... output flash_io3_do,
	input  flash_io0_di, ... input  flash_io3_di,
	input   [3:0] cfgreg_we,
	input  [31:0] cfgreg_di,
	output [31:0] cfgreg_do
);
```

—— [spimemio.v:20-49](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/soc/picorv32/gateware/spimemio.v#L20-L49)。

命中即返回、跳变则重对齐的命中逻辑：

```verilog
assign ready = valid && (addr == rd_addr) && rd_valid;
wire jump = valid && !ready && (addr != rd_addr+4) && rd_valid;
```

—— [spimemio.v:71-72](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/soc/picorv32/gateware/spimemio.v#L71-L72)：顺序取指时一路命中、零等待返回；分支跳转时 `jump=1` 丢弃预取重新发起 SPI 读。配置寄存器位含义有逐位注释（QSPI、DDR、dummy 周期数等）——[spimemio.v:74-90](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/soc/picorv32/gateware/spimemio.v#L74-L90)。

在 cmod_a7 工程里，`spimemio` 与 `memory_pack`（block RAM）并列挂在 CPU 总线上，由地址区分：SPI Flash 放启动/常驻代码，block RAM 放可被 `system_load` 热更新的用户程序。两者都在 `common.mk` 的 `SRC_V` 里——[common.mk:13-17](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/soc/picorv32/project/cmod_a7/common/common.mk#L13-L17)。

#### 4.4.4 代码实践

- **目标**：理解 `spimemio` 在仿真里如何被喂「虚拟 Flash 内容」。
- **操作步骤**：阅读 [cmod_a7/sim/Makefile:12](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/soc/picorv32/project/cmod_a7/sim/Makefile#L12)：`VCD_ARGS += +firmware=$(PICORV_DIR)/test/memio/flashdata8.hex`，即仿真时用一个 hex 文件模拟 SPI Flash 内容，通过 plusarg 传给 `spiflash.v` 模型。
- **观察现象**：`make -C soc/picorv32/project/cmod_a7/sim clean system.vcd` 后，CPU 上电先经 `spimemio` 从「虚拟 Flash」取到 bootloader，再由虚拟 UART 把字符吐到终端。
- **预期结果**：终端（`system_tb.v` 里的 virtual UART）打印出 bootloader 的 `ok` 或用户程序输出。
- **待本地验证**：需 RISC-V 工具链 + iverilog。

#### 4.4.5 小练习与答案

1. **问**：`ready` 为什么写成 `valid && (addr==rd_addr) && rd_valid` 而不是直接给 1？
   **答**：因为 SPI 读是慢速异步过程，数据要等真的从 Flash 拉到缓冲里（`rd_valid`）且地址匹配（`addr==rd_addr`）才能返回；没准备好时 `ready=0`，CPU 自然等待——又一处「CPU 原地等」的设计。
2. **问**：`jump` 信号什么时候为真？
   **答**：当 CPU 访问的地址既不在缓冲、又非「当前地址 +4」（即发生分支跳转、非顺序取指）时为真，用来丢弃当前预取序列、重新发起 SPI 读。

## 5. 综合实践

**任务：画出 cmod_a7 SoC 上「一次 localbus 写」的完整数据通路，并解释每一级的让路/时序。**

1. 阅读 [project/cmod_a7/common/system.v](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/soc/picorv32/project/cmod_a7/common/system.v)（如存在），找到 `pico_pack`、`lb_bridge`、`lb_merge`、`memory_pack`、`spimemio_pack` 的实例化关系。
2. 假设用户程序执行一句「向 localbus 某寄存器写 32 位」，画出：CPU 核 → `mpack`（打包成 69 位 `mem_packed_fwd`）→ `lb_bridge`（解包 + 地址解码 `addr[31:24]==BASE_ADDR` + `& ~busy` 门控）→ `lb_merge`（与网络侧 B 仲裁，B 优先）→ 受控 localbus。
3. 标注两个时序点：(a) `munpack` 多打的 1 拍；(b) `lb_reading` 的 `READ_DELAY` 拍读延迟。说明这两处延迟如何被算进 CPU 看到的 `mem_ready` 时机。
4. （选做，需工具链）`make -C soc/picorv32/test/lb_bridge lb_bridge.vcd`，在波形里量出一次 CPU 写从 `mem_valid` 拉高到 `mem_ready` 回高的真实周期数，与你画出的时序对账。

**预期产物**：一张标注了「地址解码位、busy 门控点、两级延迟」的数据通路框图，加一句话说明「为何 CPU 写在网络侧 master 活跃时会被压住却不丢数据」。

## 6. 本讲小结

- `soc/picorv32` 按 `gateware`（硬件）/ `firmware`（C 驱动库）/ `project`（上板工程）/ `test`（仿真）/ `common`（启动脚本与 Python 工具）分层，CPU 核是上游 PicoRV32，`pico_pack` 做参数固定与总线打包。
- PicoRV32 的 valid/ready 内存总线是整个 SoC「能等、能让路」的物理基础——`mem_valid` 拉高后可任意拍后才回 `mem_ready`。
- `rules.mk` 用一串模式规则串起交叉编译链：`.c/.S → .o → .elf → 8.hex → 32.hex → 32.dat`，其中 `hex8tohex32.py` 把字节流小端重排成 32 位字，`.dat` 仅为迎合 Vivado 后缀要求。
- `make system_load` 调 `boot_load.py`，与片上 `bootloader.S` 走 `ok→g→o→长度→数据→回读` 握手，`--byte_offset 0xe0` 保证只更新用户区、不覆盖 bootloader，实现「免重新综合换程序」。
- `lb_bridge` + `lb_merge` 用「localbus 永远优先、CPU 用 `& ~busy` 让路」的策略解决双 master 冲突；读路径用 `lb_reading` 的固定 `READ_DELAY` 延迟（呼应 u4-l2）。
- `spimemio` 把外部 SPI Flash 翻译成 CPU 内存接口，靠预取缓冲加速顺序取指、用 `jump` 处理分支跳转；仿真里用 plusarg 喂虚拟 Flash hex。

## 7. 下一步学习建议

- **u7-l2 外设驱动**：本讲的 `gateware/*_pack.v`（uart/spi/gpio/memory/badger）正是 CPU 经 localbus 访问的外设，下一讲深入 `peripheral_drivers` 里的 SPI 主机、I2C 桥与 ADC/DAC 驱动。
- **回头看 u4-l4 Packet Badger**：本讲里「网络侧 localbus master」就是 Packet Badger，建议对照复习它的 `mem_gateway`/`udp_port_cam`，理解双 master 仲裁的另一端。
- **源码延伸阅读**：想理解中断/启动细节，读 `common/startup.S` 与 `common/startup_irq.S`；想看 CPU 在真工程里怎么被接线，读 `project/cmod_a7/common/system.v` 与 `project/cmod_a7/synth/system_top.v`。
- **形式化方向**：`test/fv/` 下有 `f_pack.sby`/`fifo.sby` 等用 SymbiYosys 做形式化验证的例子，可结合 u6-l1 的验证思路一起看。
