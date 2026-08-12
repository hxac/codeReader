# GPIO 模块全解析

## 1. 本讲目标

本讲是「可配置外设 IP」单元的核心一篇。我们将把前面学过的零散能力——stdlib 时序原语、emesh 包格式、.vh 寄存器映射——汇拢到一个真实、完整、可流片的外设 `gpio` 里，看清它们如何协同工作。

学完后你应当能够：

- 读懂一个完整外设从「地址译码 → 寄存器更新 → 中断产生 → 读回响应」的端到端数据流；
- 理解 GPIO 的方向控制（input/output）是如何由 `GPIO_DIR` 寄存器决定的；
- 理解中断为何能做到「边沿/电平可配、上升/下降可配、每引脚可屏蔽」，并能手写 `irq_event` 的布尔表达式；
- 掌握 `GPIO_OUT` 的「直接写 / 清 / 置 / 翻」四种原子位操作模式，理解它为何能避免读—改—写；
- 看懂 `emesh_readback` 如何把内部寄存器值塞回一个 emesh 响应包。

## 2. 前置知识

本讲默认你已经掌握以下内容（若陌生，请先回看对应讲义）：

- **emesh 包格式与握手**（u5-l1、u5-l3）：一个 104 位（`PW=2·AW+40`，AW=32）的定长包承载一次片上事务，低 8 位是控制字节（含 `write[0]`、`datamode[2:1]`）；`access`（≈valid）与 `wait`（高有效反压，`~wait`≈ready）是并排的伴随信号。
- **.vh 寄存器映射范式**（u6-l1）：每个 IP 用 `xxx_regmap.vh` 里的大写宏给寄存器编号，再用 `dstaddr` 的某一段位切片做地址译码，产出 one-hot 写选通。
- **跨时钟域同步器**（u2-l4）：外部 IO 引脚的电平对芯片内部时钟而言是异步信号，必须先用 `oh_dsync` 同步两级再使用，否则会亚稳态。
- **one-hot 多路选择器**（u2-l1）：`oh_mux4` 用 `{(N){sel}} & in` 的 AND-OR 模式按位选择。

一个关键直觉：GPIO 是「软件可编程的通用引脚」。CPU 不直接拉线，而是通过写寄存器来设方向、写输出值、读输入值、配置中断。`gpio` 模块就是这些寄存器的硬件实现，并且对外只暴露一套 emesh 接口。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [gpio/hdl/gpio.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/hdl/gpio.v) | GPIO 主体 RTL，本讲的主角。含地址译码、方向、输出、输入同步、中断、读回全部逻辑 |
| [gpio/hdl/gpio_regmap.vh](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/hdl/gpio_regmap.vh) | 寄存器地址宏定义，软硬件共用的「单一事实源」 |
| [gpio/README.md](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/README.md) | 寄存器地址表、功能说明（文档，可能与代码有出入，以 RTL 为准） |
| [stdlib/rtl/oh_mux4.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_mux4.v) | 4 选 1 one-hot 选择器，被 `GPIO_OUT` 的四种写模式复用 |
| [stdlib/rtl/oh_dsync.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_dsync.v) | 两级同步器，被 `GPIO_IN` 用来同步外部引脚 |
| [emesh/hdl/emesh_readback.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/hdl/emesh_readback.v) | 读响应装配器，把 `read_data` 拼成 emesh 响应包回送 |
| [gpio/dv/tests/test_basic.emf](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/dv/tests/test_basic.emf) | 一段贯穿各寄存器的事务激励，本讲多处用它做手算练习 |

> **读源码先告示**：`gpio.v` 第 77 行实例化的 `enoc_unpack`，以及 `emesh_readback.v` 第 48/91 行实例化的 `enoc_unpack`/`enoc_pack`，在当前仓库里**找不到对应模块名**——真实模块叫 `emesh_unpack`/`emesh_pack`（见 [emesh/hdl/emesh_unpack.v:L11](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/hdl/emesh_unpack.v#L11)，文件尾注释却仍写 `enoc_unpack`）。这是一次「enoc → emesh」改名做了一半留下的接口漂移，`gpio.v` 当前不能原样对 iverilog 编译通过。本讲以**代码逻辑**为准讲解，编译问题在实践部分单独说明。

## 4. 核心概念与源码讲解

本讲按数据流分四个最小模块：**4.1 寄存器译码**（地基）→ **4.2 方向控制与输出**（方向控制）→ **4.3 输入同步与可配置中断**（边沿中断）→ **4.4 读回通路**（readback）。

### 4.1 寄存器译码：从 emesh 包到写选通

#### 4.1.1 概念说明

`gpio` 对外只有一个 emesh 接口，但内部有 11 个寄存器。CPU 写一个地址，模块必须先回答两个问题：

1. 这是一次写还是读？（看控制字节的 `write` 位）
2. 写的是哪个寄存器？（看地址的一段位）

回答完这两个问题，才能产出正确的「写选通」（write strobe）——一个只在命中那一拍为 1 的单脉冲，用来驱动对应寄存器的 `always` 块。这套范式在 u6-l1 已建立，本讲看它在真实外设里的完整落地。

#### 4.1.2 核心流程

```
packet_in(104位)
    │  enoc_unpack 解包
    ▼
write_in, dstaddr_in, data_in, datamode_in ...
    │
    ├── reg_write = access_in & write_in        // 是写事务且有效
    ├── reg_read  = access_in & ~write_in       // 是读事务且有效
    ├── reg_wdata = data_in[N-1:0]              // 写数据（截到引脚宽度）
    │
    └── 地址译码：dstaddr_in[6:3] 与各 `GPIO_xxx 宏比较
            │
            ▼
   dir_write / outreg_write / outclr_write / ... （one-hot 写选通）
```

关键点：寄存器号取自 `dstaddr_in[6:3]`——共 4 位、16 个槽位，步长由被忽略的低 3 位（`addr[2:0]`）决定，即每个寄存器占 **8 字节对齐的一段**。这与 `.emf` 测试里地址以 `0x00, 0x08, 0x10, 0x18 ...` 递增完全对应。

#### 4.1.3 源码精读

模块头与端口契约见 [gpio/hdl/gpio.v:L9-L27](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/hdl/gpio.v#L9-L27)：参数 `N`（引脚数，默认 24）、`AW`（地址宽，32）、`PW`（包宽，104）；端口分三组——emesh 从机侧（`access_in/packet_in/wait_out/access_out/packet_out/wait_in`）、IO 引脚侧（`gpio_out/gpio_dir/gpio_in`）、中断（`gpio_irq`）。

解包由 `enoc_unpack` 完成见 [gpio/hdl/gpio.v:L77-L89](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/hdl/gpio.v#L77-L89)：

```verilog
enoc_unpack #(.AW(AW), .PW(PW)) p2e (
   .write_in(write_in), .datamode_in(datamode_in[1:0]),
   .dstaddr_in(dstaddr_in[AW-1:0]), .srcaddr_in(srcaddr_in[AW-1:0]),
   .data_in(data_in[AW-1:0]),
   .packet_in(packet_in[PW-1:0]));
```

读写性质与写数据的派生见 [gpio/hdl/gpio.v:L91-L94](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/hdl/gpio.v#L91-L94)：

```verilog
assign reg_write  = access_in & write_in;       // 写有效
assign reg_read   = access_in & ~write_in;      // 读有效
assign reg_double = datamode_in[1:0]==2'b11;    // 64位访问标志（本讲暂不用）
assign reg_wdata[N-1:0] = data_in[N-1:0];       // 写数据截到引脚宽
```

地址译码产出 9 路写选通见 [gpio/hdl/gpio.v:L96-L104](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/hdl/gpio.v#L96-L104)，每一路都是同一套句式：

```verilog
assign dir_write     = reg_write & (dstaddr_in[6:3]==`GPIO_DIR);
assign outreg_write  = reg_write & (dstaddr_in[6:3]==`GPIO_OUT);
assign outclr_write  = reg_write & (dstaddr_in[6:3]==`GPIO_OUTCLR);
// ... OUTSET / OUTXOR / IMASK / ITYPE / IPOL / ILATCLR 同理
```

宏定义在 [gpio/hdl/gpio_regmap.vh:L4-L14](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/hdl/gpio_regmap.vh#L4-L14)：`` `GPIO_DIR=4'h0 ``、`` `GPIO_IN=4'h1 ``、`` `GPIO_OUT=4'h2 `` … `` `GPIO_ILATCLR=4'hA ``。注意宏值会跳号（这里 0xA 之后没有继续），且编号就是地址 `addr[6:3]` 的直接取值。

#### 4.1.4 代码实践

**实践目标**：把一条 `.emf` 事务行追到它触发的写选通，验证你对地址译码的理解。

**操作步骤**：

1. 打开 [gpio/dv/tests/test_basic.emf:L3](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/dv/tests/test_basic.emf#L3)，该行是 `DEADBEEF_33330000_00000018_05_0010`。
2. 按五段拆分：`srcaddr=DEADBEEF`、`data=33330000`、`dstaddr=00000018`、控制字节 `05`、时序 `0010`。
3. 控制字节 `0x05 = 0b00000101`：`write[0]=1`（写），`datamode[2:1]=0b10=2`（32 位字）。
4. 算寄存器号：`dstaddr=0x00000018`，取 `[6:3]`：`0x18 = 0b0001_1000`，`[6:3]=0b0011=3`，对应 `` `GPIO_OUTCLR(4'h3) ``。

**需要观察的现象**：该拍 `reg_write=1`、`outclr_write=1`，其余 8 路写选通为 0；`reg_wdata=0x33330000`。

**预期结果**：你的手算结果应与 [gpio/hdl/gpio.v:L102](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/hdl/gpio.v#L102) 的 `outclr_write` 译码式一致。地址每加 `0x08` 就切到下一个寄存器，这正是 `.emf` 里地址序列的规律。

#### 4.1.5 小练习与答案

**练习 1**：地址 `0x40` 落在哪个寄存器？是读还是写由谁决定？

**答案**：`0x40 = 0b0100_0000`，`[6:3]=0b1000=8` → `` `GPIO_IPOL ``。读写不由地址决定，而由控制字节的 `write` 位（`reg_write` vs `reg_read`）决定。

**练习 2**：为什么译码用 `dstaddr_in[6:3]` 而不是整个 32 位地址？

**答案**：高位地址由 emesh 系统级路由负责「选到 GPIO 这个 IP」，IP 内部只需译「寄存器号」这段低位（u6-l1 的分层原则）。4 位给出 16 个槽位，足够当前 11 个寄存器。

---

### 4.2 方向控制与输出的原子位操作

#### 4.2.1 概念说明

GPIO 引脚是双向的——同一根线既能当输入也能当输出。方向由 `GPIO_DIR` 寄存器逐位决定（`0=输入`、`1=输出`，见 [gpio/hdl/gpio.v:L114-L115](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/hdl/gpio.v#L114-L115) 的注释）。

输出值由 `GPIO_OUT` 控制。但「改某几个位」这件事有个经典难题：如果只能整体写 `GPIO_OUT`，那么「把第 3 位清零」就必须先读回当前值、改第 3 位、再写回——一次读—改—写（RMW）。RMW 在多主端或中断并发时会丢更新。`gpio` 的解法是提供 **4 个别名寄存器**，用不同的位运算把「要改的位」和「保持的位」一次性算出来，实现原子位操作。

#### 4.2.2 核心流程

`GPIO_DIR` 最简单——直接把写数据装进寄存器：

```
dir_write 拍：gpio_dir <= reg_wdata
```

`GPIO_OUT` 则先经过一个 `oh_mux4`，按命中的别名选择「下一拍输出值」的计算方式，再写入寄存器：

```
              ┌─ outreg_write (GPIO_OUT)    → next = reg_wdata            （直写）
out_dmux  ←──┤─ outclr_write (GPIO_OUTCLR) → next = gpio_out & ~reg_wdata（清零掩码位）
              ├─ outset_write (GPIO_OUTSET) → next = gpio_out | reg_wdata （置位掩码位）
              └─ outxor_write (GPIO_OUTXOR) → next = gpio_out ^ reg_wdata （翻转掩码位）

out_write 拍：gpio_out <= out_dmux
```

`out_write` 是四种别名选通的「或」，只要命中其中一种就允许写入（见 [gpio/hdl/gpio.v:L106-L109](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/hdl/gpio.v#L106-L109)）。「掩码位」指 `reg_wdata` 里为 1 的位——只有这些位参与运算，其余位因 `&~0=原值`、`|0=原值`、`^0=原值` 而保持不变。

#### 4.2.3 源码精读

`GPIO_DIR` 寄存器见 [gpio/hdl/gpio.v:L117-L121](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/hdl/gpio.v#L117-L121)：异步低有效复位、复位值为全 0（全部输入）：

```verilog
always @ (posedge clk or negedge nreset)
  if(!nreset)            gpio_dir[N-1:0] <= 'b0;
  else if(dir_write)     gpio_dir[N-1:0] <= reg_wdata[N-1:0];
```

`GPIO_OUT` 的 mux4 计算见 [gpio/hdl/gpio.v:L139-L145](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/hdl/gpio.v#L139-L145)：

```verilog
oh_mux4 #(.DW(N)) oh_mux4 (
   .out (out_dmux[N-1:0]),
   .in0 (reg_wdata),                     .sel0 (outreg_write),  // 直写
   .in1 (gpio_out & ~reg_wdata),         .sel1 (outclr_write),  // 清
   .in2 (gpio_out | reg_wdata),          .sel2 (outset_write),  // 置
   .in3 (gpio_out ^ reg_wdata),          .sel3 (outxor_write)); // 翻
```

注意四个 `in` 是**四套预先算好的下一态值**，`oh_mux4` 只负责按 one-hot 选出其一。`oh_mux4` 本体是 AND-OR 选择见 [stdlib/rtl/oh_mux4.v:L21-L24](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_mux4.v#L21-L24)：

```verilog
assign out = ({(N){sel0}} & in0 | {(N){sel1}} & in1
            | {(N){sel2}} & in2 | {(N){sel3}} & in3);
```

把 mux 结果写进寄存器见 [gpio/hdl/gpio.v:L147-L151](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/hdl/gpio.v#L147-L151)（复位为全 0）：

```verilog
always @ (posedge clk or negedge nreset)
  if(!nreset)        gpio_out[N-1:0] <= 'b0;
  else if(out_write) gpio_out[N-1:0] <= out_dmux[N-1:0];
```

四种别名模式的数学表达：

\[
\text{next} = \begin{cases}
w & \text{OUT} \\
q \mathbin{\&} \lnot w & \text{OUTCLR} \\
q \mathbin{|} w & \text{OUTSET} \\
q \oplus w & \text{OUTXOR}
\end{cases}
\]

其中 \(q\) 是当前 `gpio_out`、\(w\) 是 `reg_wdata`。

#### 4.2.4 代码实践

**实践目标**：手算 `test_basic.emf` 前 5 行写完后 `gpio_out` 的值，验证对原子位操作的理解。

**操作步骤**：假设 `N=32`（与 `dut_gpio` 一致），逐行算 `gpio_out`（初值 `0xFFFFFFFF` 是第 2 行 `GPIO_OUT` 直写后的状态）：

| 行 | 事务 | 模式 | 计算 | `gpio_out` 结果 |
|----|------|------|------|-----------------|
| L1 | `GPIO_DIR=FFFF0000` | 直写 | — | dir=F...F0000（与本练习无关） |
| L2 | `GPIO_OUT=FFFFFFFF` | 直写 | \(w\) | `FFFFFFFF` |
| L3 | `GPIO_OUTCLR=33330000` | 清 | `FFFFFFFF & ~33330000` = `FFFFFFFF & CCCCFFFF` | `CCCCFFFF` |
| L4 | `GPIO_OUTSET=33330000` | 置 | `CCCCFFFF \| 33330000` | `FFFFFFFF` |
| L5 | `GPIO_OUTXOR=55550000` | 翻 | `FFFFFFFF ^ 55550000` | `AAAAFFFF` |

**需要观察的现象**：清/置/翻三种模式下，`reg_wdata` 为 0 的位都保持原值不变（`&~0`、`|0`、`^0` 都等于原值）。

**预期结果**：第 5 行后 `gpio_out = 0xAAAAFFFF`。若你能在脑海里复现这张表，就真正理解了原子位操作。如需验证，可用 `build.sh`+`sim.sh` 跑 `test_basic.emf` 并在 `gtkwave` 里盯 `gpio_out` 信号——但因为前述 `enoc_unpack` 改名问题，可能需要先把 `gpio.v:77` 的 `enoc_unpack` 临时改成 `emesh_unpack` 才能编译，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：若当前 `gpio_out=0xAAAAFFFF`，写 `GPIO_OUTCLR=0x0000FFFF` 后结果是什么？

**答案**：`0xAAAAFFFF & ~0x0000FFFF = 0xAAAAFFFF & 0xFFFF0000 = 0xAAAA0000`。低 16 位被清零。

**练习 2**：为什么「翻转」用 `GPIO_OUTXOR` 而不是提供「置 1 翻转」和「置 0 不变」两个寄存器？

**答案**：异或本身就只对掩码为 1 的位翻转、为 0 的位不变（\(q\oplus 0=q\)）。一个寄存器已足够表达「翻转指定位」，无需拆成两个。

**练习 3**：`oh_mux4` 的四个 `sel` 是否可能同时为 1？

**答案**：不会。四个选通来自「同一拍 `dstaddr[6:3]` 只能等于一个宏」的译码，天然互斥。`oh_mux4` 在仿真模式下还有一个断言拦截非 one-hot 输入见 [stdlib/rtl/oh_mux4.v:L26-L35](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_mux4.v#L26-L35)。

---

### 4.3 输入同步与可配置中断

#### 4.3.1 概念说明

`GPIO_IN` 读到的是外部引脚的电平。但外部引脚相对芯片时钟是异步的——直接用会撞上亚稳态（u2-l4）。所以 `gpio` 先用 `oh_dsync` 把每个引脚同步两级，得到 `gpio_in_sync`，所有后续逻辑（读回、边沿检测、中断）都只看同步后的值。

中断是 GPIO 最有意思的部分。它要做到「**每根引脚独立可配**」：

- 触发**类型**：边沿触发还是电平触发（`GPIO_ITYPE`，0=边沿、1=电平）；
- 触发**极性**：上升沿/高电平，还是下降沿/低电平（`GPIO_IPOL`，1=rising/high）；
- **屏蔽**：该引脚的中断是否上报（`GPIO_IMASK`，1=屏蔽）；
- **锁存与清除**：触发的中断锁进 `GPIO_ILAT`，软件写 `GPIO_ILATCLR` 清除。

四者按位组合，每根引脚都能独立配置成「上升沿中断」「下降沿中断」「高电平中断」「低电平中断」或「不报」。

#### 4.3.2 核心流程

```
gpio_in[N-1:0]  ──oh_dsync(两级)──▶  gpio_in_sync
                                           │
                            ┌──────────────┼──────────────┐
                            ▼              ▼              ▼
                     data_old(打一拍)  边沿检测         读回(GPIO_IN)
                            │              │
                            └─▶ rising = sync & ~old
                            └─▶ falling = ~sync & old
                                           │
                            irq_event = 四种(itype,ipol)组合之一
                                           │
                                  & ~gpio_imask  (屏蔽)
                                           │
                            gpio_ilat <= (old & ~ilat_clr) | irq_event
                                           │
                            gpio_irq = |gpio_ilat   (按位或归约)
```

边沿检测的套路是 u2-l4 的「打一拍再比较」：把同步后的当前值 `gpio_in_sync` 与上一拍值 `data_old` 比较，异或得任意沿、`sync & ~old` 得上升沿、`~sync & old` 得下降沿。

#### 4.3.3 源码精读

每个引脚各起一个 `oh_dsync` 实例（实例数组），同步外部输入见 [gpio/hdl/gpio.v:L127-L130](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/hdl/gpio.v#L127-L130)：

```verilog
oh_dsync oh_dsync[N-1:0] (.dout(gpio_in_sync[N-1:0]),
                          .clk(clk), .nreset(nreset),
                          .din(gpio_in[N-1:0]));
```

`oh_dsync` 内部是两级（默认 `SYNCPIPE=2`）移位寄存器见 [stdlib/rtl/oh_dsync.v:L20-L31](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_dsync.v#L20-L31)，输出取 `sync_pipe[SYNCPIPE-1]`（即第 2 级）。这意味着读到的 `GPIO_IN` 比真实引脚**晚约 2 拍**。

打一拍存旧值见 [gpio/hdl/gpio.v:L132-L133](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/hdl/gpio.v#L132-L133)：

```verilog
always @ (posedge clk)
    data_old[N-1:0] <= gpio_in_sync[N-1:0];
```

上升/下降沿见 [gpio/hdl/gpio.v:L181-L183](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/hdl/gpio.v#L181-L183)：

```verilog
assign rising_edge  = gpio_in_sync & ~data_old;
assign falling_edge = ~gpio_in_sync & data_old;
```

核心的 `irq_event` 四选一见 [gpio/hdl/gpio.v:L185-L188](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/hdl/gpio.v#L185-L188)：

```verilog
assign irq_event = (rising_edge  & ~gpio_itype &  gpio_ipol) |  // 边沿+上升
                   (falling_edge & ~gpio_itype & ~gpio_ipol) |  // 边沿+下降
                   (gpio_in_sync &  gpio_itype &  gpio_ipol) |  // 电平+高
                   (~gpio_in_sync & gpio_itype & ~gpio_ipol);   // 电平+低
```

写成真值表：

| `itype` | `ipol` | 触发条件 | 语义 |
|---------|--------|----------|------|
| 0（边沿） | 1 | `rising_edge` | 上升沿中断 |
| 0（边沿） | 0 | `falling_edge` | 下降沿中断 |
| 1（电平） | 1 | `gpio_in_sync==1` | 高电平中断 |
| 1（电平） | 0 | `gpio_in_sync==0` | 低电平中断 |

锁存与清除见 [gpio/hdl/gpio.v:L196-L201](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/hdl/gpio.v#L196-L201)：

```verilog
always @ (posedge clk or negedge nreset)
  if(!nreset) gpio_ilat <= 'b0;
  else
    gpio_ilat <= (gpio_ilat & ~ilat_clr) |   // 清除选定 bit
                 (irq_event & ~gpio_imask);   // 置位新的、未屏蔽的中断
```

`ilat_clr` 由 `GPIO_ILATCLR` 写入驱动见 [gpio/hdl/gpio.v:L194](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/hdl/gpio.v#L194)：未写清除时为 0（不清）。`gpio_imask` 复位为全 1（**默认全部屏蔽**）见 [gpio/hdl/gpio.v:L157-L161](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/hdl/gpio.v#L157-L161)，与 README「IMASK default H」一致。

中断汇总输出见 [gpio/hdl/gpio.v:L207](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/hdl/gpio.v#L207)：

```verilog
assign gpio_irq = |gpio_ilat[N-1:0];
```

#### 4.3.4 代码实践

**实践目标**：给定一组配置，推断哪一种引脚电平跳变会把 `GPIO_ILAT` 的某位置 1。

**操作步骤**：

1. 设定 `gpio_itype=0`（边沿）、`gpio_ipol=1`（上升）、`gpio_imask=0`（不屏蔽）某一位。
2. 在 `gtkwave` 或纸面上让对应的 `gpio_in` 位经历 `0 → 1 → 0 → 1` 的跳变（注意每跳变后至少留 2 拍让 `oh_dsync` 同步稳定）。
3. 观察 `rising_edge`、`irq_event`、`gpio_ilat`、`gpio_irq` 的时序。

**需要观察的现象**：

- `rising_edge` 仅在 `gpio_in_sync` 从 0 变 1 的**那一拍**为 1（单周期脉冲）；
- `irq_event` 同拍跟随；
- `gpio_ilat` 对应位被置 1 后**一直保持**，即使引脚电平又变回去也不会自动清零；
- `gpio_irq` 在 `gpio_ilat` 非零期间持续为高。

**预期结果**：两次 `0→1` 跳变应触发两次 `rising_edge`，但若两次跳变之间没有写 `GPIO_ILATCLR`，`gpio_ilat` 该位只是「保持为 1」（锁存语义，不计数）。要复位它必须软件写 `GPIO_ILATCLR` 对应位。**待本地验证**（受前述 `enoc_unpack` 改名问题影响，可能需先打补丁）。

#### 4.3.5 小练习与答案

**练习 1**：源码注释写「ONE CYCLE IRQ PULSE」（见第 203-205 行的小节标题），但 `gpio_irq` 真的是单周期脉冲吗？

**答案**：不是。`assign gpio_irq = |gpio_ilat;` 是对**锁存寄存器**的按位或归约，只要 `gpio_ilat` 有任一位置 1，`gpio_irq` 就持续为高，直到软件写 `GPIO_ILATCLR` 清除。注释与实现不符，以 RTL 为准。

**练习 2**：同一拍里，某位既被 `GPIO_ILATCLR` 清除、又来了一个未屏蔽的 `irq_event`，结果如何？

**答案**：新中断优先。代入式子：`(old & ~1) | (1 & ~0) = 0 | 1 = 1`。即清除与置位同拍发生时，置位胜出，该位保持为 1。

**练习 3**：`data_old` 没有 `nreset` 复位分支（[gpio/hdl/gpio.v:L132-L133](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/hdl/gpio.v#L132-L133)），这会出问题吗？

**答案**：最多在复位后头一两拍产生一次虚假边沿。但此时 `gpio_imask` 复位为全 1（默认屏蔽），虚假 `irq_event` 被 `& ~gpio_imask` 挡住，不会污染 `gpio_ilat`。这是「默认安全」的设计默契。

---

### 4.4 读回通路：把寄存器值塞回响应包

#### 4.4.1 概念说明

读事务（`reg_read`）来了一个 emesh 包，问「地址 X 的寄存器值是多少？」。`gpio` 要把内部寄存器的当前值塞进一个 emesh 响应包回送。这件事分两步：

1. **选数据**：用一个 `case`，按地址把对应寄存器的值送到一根统一的 `read_data` 总线上（提前一拍备好）；
2. **打包回送**：把 `read_data` 交给 `emesh_readback`，由它装配响应包（地址回送、注入数据、重打控制字段）。

这套机制是 u5-l3 讲过的 `emesh_readback` 范式的真实应用。

#### 4.4.2 核心流程

```
reg_read ──▶ case(dstaddr[6:3])
              GPIO_IN    → read_data = gpio_in_sync
              GPIO_ILAT  → read_data = gpio_ilat
              GPIO_DIR   → read_data = gpio_dir
              GPIO_IMASK → read_data = gpio_imask
              GPIO_IPOL  → read_data = gpio_ipol
              GPIO_ITYPE → read_data = gpio_itype
              default    → read_data = 0
            （注：GPIO_OUT 不可读！）

read_data ──▶ emesh_readback ──▶ packet_out（响应包）
              · dstaddr_out ← srcaddr_in   （回信地址变目的地址）
              · data_out    ← read_data[31:0]
              · write_out   = 1            （响应恒为"写"包）
              · access_out  在 ready_in 时拉高
```

#### 4.4.3 源码精读

选数据的 `case` 见 [gpio/hdl/gpio.v:L213-L223](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/hdl/gpio.v#L213-L223)：

```verilog
always @ (posedge clk)
  if(reg_read)
    case(dstaddr_in[6:3])
      `GPIO_IN   : read_data <= gpio_in_sync;
      `GPIO_ILAT : read_data <= gpio_ilat;
      `GPIO_DIR  : read_data <= gpio_dir;
      `GPIO_IMASK: read_data <= gpio_imask;
      `GPIO_IPOL : read_data <= gpio_ipol;
      `GPIO_ITYPE: read_data <= gpio_itype;
      default    : read_data <= 'b0;
    endcase
```

注意可读集合是 `{GPIO_IN, GPIO_ILAT, GPIO_DIR, GPIO_IMASK, GPIO_IPOL, GPIO_ITYPE}`——`GPIO_OUT` 不在其中，与 README「GPIO_OUT: WR（只写）」一致。读时序上，`read_data` 是**寄存器**，所以数据在 `reg_read` 那拍之后才备好，恰好赶上 `emesh_readback` 的下一拍装配。

`emesh_readback` 实例化见 [gpio/hdl/gpio.v:L225-L238](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/hdl/gpio.v#L225-L238)，把 `read_data` 喂进去：

```verilog
emesh_readback #(.AW(AW), .PW(PW)) emesh_readback (
   .wait_out(wait_out), .access_out(access_out), .packet_out(packet_out),
   .nreset(nreset), .clk(clk),
   .access_in(access_in), .packet_in(packet_in),
   .read_data(read_data[63:0]),   // ← 见下方注意点
   .wait_in(wait_in));
```

`emesh_readback` 内部把读数据注入响应包见 [emesh/hdl/emesh_readback.v:L66-L82](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/hdl/emesh_readback.v#L66-L82)：

```verilog
always @ (posedge clk or negedge nreset)            // access_out
  if(!nreset)                 access_out <= 1'b0;
  else if(ready_in)           access_out <= access_in & ~write_in;  // 仅读请求回响应
always @ (posedge clk)                               // 控制字段 + 回信地址
  if(ready_in & access_in & ~write_in) begin
     datamode_out <= datamode_in;
     dstaddr_out  <= srcaddr_in;                     // 请求方 srcaddr → 响应目的地址
  end
assign data_out    = read_data[31:0];                // 低 32 位 = 读数据
assign srcaddr_out = read_data[63:32];               // 高 32 位 = 响应 srcaddr
```

> **注意点（待本地验证）**：`gpio.v` 里 `read_data` 声明为 `reg [N-1:0]`（[gpio/hdl/gpio.v:L38](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/hdl/gpio.v#L38)），但传给 `emesh_readback` 时写成 `read_data[63:0]`。当 `N<64`（如默认 `N=24`）时，`read_data[63:32]` 越界读到 `z/x`，导致响应包的 `srcaddr_out` 字段（`read_data[63:32]`）为未知值。好在响应路由靠的是 `dstaddr_out ← srcaddr_in`，不受影响；仅响应包的「来源地址」元数据是垃圾。读 `GPIO_IN` 这种 32 位寄存器时 `data_out` 走 `read_data[31:0]` 是对的。

#### 4.4.4 代码实践

**实践目标**：在纸面上还原「读 `GPIO_IN`」的完整响应包，把读回通路串起来。

**操作步骤**：取 [gpio/dv/tests/test_basic.emf:L9](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/dv/tests/test_basic.emf#L9) 的读事务 `DEADBEEF_DEADBEEF_00000008_04_0010`：

1. 拆包：`srcaddr=DEADBEEF`、`data=DEADBEEF`、`dstaddr=00000008`、控制字节 `04`。
2. `0x04 = 0b00000100`：`write[0]=0`（读），`datamode[2:1]=0b10=2`（32 位）。
3. 寄存器号：`0x08 = 0b0000_1000`，`[6:3]=0b0001=1` → `` `GPIO_IN ``。
4. `dut_gpio` 里 `gpio_in` 与 `gpio_out` 接成回环（[gpio/dv/dut_gpio.v:L69](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/hdl/../../gpio/dv/dut_gpio.v#L69)），而前续写序列把 `gpio_out` 算到 `0xAAAAFFFF`。所以 `gpio_in` 也应是 `0xAAAAFFFF`。
5. 经 `oh_dsync` 两级同步后 `read_data <= gpio_in_sync`，再由 `emesh_readback` 把它放进响应包的 `data` 字段。

**需要观察的现象**：响应包 `packet_out` 的 `data` 字段在 `reg_read` 之后约 2 拍出现 `0xAAAAFFFF`；响应的 `dstaddr` 字段等于请求的 `srcaddr`（`0xDEADBEEF`）；`access_out` 在 `wait_in=0` 时被拉高。

**预期结果**：响应包形如 `????????_AAAAFFFF_DEADBEEF_<ctrl>_...`（`data=AAAAFFFF`、`dstaddr=DEADBEEF`）。因前述 `read_data[63:0]` 越界，响应 `srcaddr` 字段为未知，标注为 `????????`。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `GPIO_OUT` 不能读？

**答案**：`case` 里没有 `GPIO_OUT` 分支，且 `default` 把 `read_data` 清 0；读 `GPIO_OUT`（地址 `0x10`，`[6:3]=2`）会落入 `default`，回读为 0。这是设计选择——输出值软件自己知道，无需回读。

**练习 2**：`read_data` 为什么声明为 `reg` 而不是 `wire`？

**答案**：它在 `always @(posedge clk)` 块里用 `case` 赋值，是时序寄存器，所以必须是 `reg`。这也让读数据天然延迟一拍，与 `emesh_readback` 的流水线对齐。

**练习 3**：读 `GPIO_IN` 回的值比真实引脚晚几拍？为什么？

**答案**：约 3 拍——`oh_dsync` 两级同步 + `read_data` 寄存一拍（再算上 `emesh_readback` 的装配共约 4 拍出现在 `packet_out`）。软件读 GPIO 必须接受这个固有延迟。

---

## 5. 综合实践

**任务**：为 GPIO 新增一个可写寄存器 `GPIO_DIRSET`（地址 `0xB`），作用是「对掩码中为 1 的位，把对应 `GPIO_DIR` 置 1（设为输出），其余位不变」——方向版的原子置位，免去读—改—写。

这是一个贯穿四个最小模块的端到端练习：你会改 regmap（4.1）、动方向寄存器（4.2）、并确认它不影响中断（4.3）与读回（4.4，`GPIO_DIR` 已可读，无需改 `case`）。

**操作步骤**：

1. **改 regmap**：在 [gpio/hdl/gpio_regmap.vh](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/hdl/gpio_regmap.vh) 适当位置增加一行：
   ```verilog
   `define GPIO_DIRSET  4'hB  // alias, sets specific bits in GPIO_DIR
   ```

2. **加写选通**：在 [gpio/hdl/gpio.v:L96-L104](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/hdl/gpio.v#L96-L104) 的译码区增加：
   ```verilog
   assign dirset_write = reg_write & (dstaddr_in[6:3]==`GPIO_DIRSET);
   ```
   并在端口声明区（第 52-61 行附近）加 `wire dirset_write;`。

3. **改方向寄存器逻辑**：把 [gpio/hdl/gpio.v:L117-L121](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/hdl/gpio.v#L117-L121) 的 `GPIO_DIR` always 块改为：
   ```verilog
   always @ (posedge clk or negedge nreset)
     if(!nreset)            gpio_dir <= 'b0;
     else if(dir_write)     gpio_dir <= reg_wdata;        // 直写
     else if(dirset_write)  gpio_dir <= gpio_dir | reg_wdata;  // 原子置位
   ```

4. **加测试事务**：在 `test_basic.emf` 末尾加一行写 `0xB` 地址（`[6:3]=0xB`，对应字节地址 `0xB<<3 = 0x58`）：
   ```
   DEADBEEF_000000FF_00000058_05_0010 // write gpio_dirset
   ```
   即把最低 8 位置为输出。

5. **验证**：先确认 `GPIO_DIR` 已在 readback `case` 里（4.4，无需新增可读性）。仿真后看 `gpio_dir` 是否在原来值的基础上，低 8 位被置 1。

**需要观察的现象**：

- 写 `GPIO_DIRSET=0x000000FF` 后，`gpio_dir` 的低 8 位变为 1，高位保持原值（即第 1 行 `GPIO_DIR=FFFF0000` 留下的高 16 位输出 + 现在低 8 位也输出）；
- `dirset_write` 是 one-hot，不会与 `dir_write` 同拍冲突；
- 中断逻辑完全不受影响（它只读 `gpio_in_sync`，与 `gpio_dir` 无关）。

**预期结果**：若原来 `gpio_dir=0xFFFF0000`，写 `GPIO_DIRSET=0x000000FF` 后 `gpio_dir=0xFFFF00FF`。你可以对称地再实现一个 `GPIO_DIRCLR`（`gpio_dir & ~reg_wdata`），完全复刻 `GPIO_OUT` 的四模式原子位操作设计。

> 由于前述 `enoc_unpack`/`enoc_pack` 改名漂移，直接 `build.sh` 可能报「unknown module enoc_unpack」。若要实际跑通，可临时把 `gpio.v:77` 与 `emesh_readback.v:48/91` 的 `enoc_*` 改为 `emesh_*`（这只是为了本地学习验证，**不要提交到仓库**）。本练习的核心收获是 regmap→decode→寄存器→readback 的完整改动链路。

## 6. 本讲小结

- `gpio` 用一套 emesh 接口串起 11 个寄存器：地址译码取 `dstaddr[6:3]` 共 4 位、步长 8 字节，产出 one-hot 写选通，与 u6-l1 的范式完全一致。
- **方向控制**：`GPIO_DIR` 逐位决定 input/output，复位全 0（全输入）。
- **输出原子位操作**：`GPIO_OUT` + `OUTCLR/OUTSET/OUTXOR` 三个别名，通过 `oh_mux4` 预算四套下一态值再 one-hot 选出，用 `&`/`|`/`^` 实现「只改掩码位」，避免读—改—写。
- **输入与中断**：`gpio_in` 经 `oh_dsync` 两级同步；`irq_event` 由 `itype`（边沿/电平）和 `ipol`（上升/下降、高/低）四选一，屏蔽后锁进 `gpio_ilat`，软件写 `GPIO_ILATCLR` 清除，`gpio_irq` 是 `gpio_ilat` 的按位或（电平输出，并非注释所称的单周期脉冲）。
- **读回**：`case` 选出 `read_data`，交 `emesh_readback` 装配响应包（`dstaddr_out←srcaddr_in`、`data_out←read_data`、响应恒为写包）；`GPIO_OUT` 不可读。
- **遗留警示**：`enoc_unpack`/`enoc_pack` 改名漂移使 `gpio.v` 不能原样编译；`read_data[63:0]` 在 `N<64` 时越界使响应 srcaddr 字段为未知值。两处都以源码为准、待本地验证。

## 7. 下一步学习建议

- **横向对照另一个外设**：带着本讲建立的「regmap + 译码 + readback」框架去读 [edma/hdl/edma.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/edma/hdl/edma.v) 或 [emailbox/hdl/emailbox.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emailbox/hdl/emailbox.v)，体会「同一套模式、不同的内部数据通路」。
- **进入时序更复杂的外设**：下一讲 u6-l3 讲 SPI 主从，你将看到移位寄存器、CPOL/CPHA 时序如何叠加在本讲的寄存器映射之上。
- **向上走**：当外设都就绪后，第 7 单元的 `elink` 会把这些 emesh 接口接到高速 LVDS 链路上——本讲的 `access/wait` 握手正是 elink TX/RX 通道处理的基本单位。
- **动手实验**：完成综合实践的 `GPIO_DIRSET`，并尝试对称实现 `GPIO_DIRCLR`，验证你是否能独立按 OH! 约定新增一个寄存器。
