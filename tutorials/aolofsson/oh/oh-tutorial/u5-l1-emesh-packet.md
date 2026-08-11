# emesh 包格式与协议

## 1. 本讲目标

学完本讲，你应当能够：

- 画出 emesh **104 位事务包**的字段布局，并解释包宽 `PW` 与地址宽 `AW` 的关系；
- 说清 `access` / `wait` 这对握手信号如何实现**反压（backpressure）**；
- 区分**写事务、读事务、读响应**三种操作的语义，理解 `srcaddr` 为什么是"回信地址"；
- 把一行 `.emf` 测试激励拆解成 `dstaddr / datamode / write / access` 各字段。

本讲是第 5 单元的入口。emesh 是 OH! 全项目的"公共语言"——gpio、spi、emailbox、edma、elink、axi 桥……几乎所有模块之间的数据交换都用同一套 104 位包格式。掌握了它，后面读任何外设源码都能看懂它的接口。

## 2. 前置知识

本讲承接 [u4-l1 通用测试平台架构](u4-l1-testbench-framework.md) 里讲过的 `dv_top` 三段式骨架，以及 `access / packet / wait` 这组握手信号。如果你还不清楚"什么是 testbench、什么是 DUT"，请先读 u4-l1。

本讲用到几个术语，先用大白话解释：

- **片上网络（Network-on-Chip, NoC）**：把很多 IP 核连在一起的"数据高速公路"。emesh 就是 OH! 自定义的一种 NoC 协议。
- **事务（transaction）**：一次完整的"请求—响应"。比如"向地址 `0x80800000` 写入 `0x1234`"就是一次写事务。
- **内存映射（memory-mapped）**：每个外设的寄存器都占一段地址空间，CPU/主设备通过读写地址就能操作外设。emesh 是内存映射式的。
- **主设备 / 从设备（master / slave）**：发起事务的一方叫主，被动响应的一方叫从。写事务由主发出；读响应由从发回。
- **反压（backpressure）**：当接收方一时处理不过来，用一根信号告诉发送方"慢一点/停一下"。emesh 用 `wait` 做这件事。
- **DDR（Dual Data Rate）**：仅在 4.x 里一笔带过，指 elink 物理链路上"时钟上升沿和下降沿都传数据"，**本讲不展开**（留给第 7 单元）。

一条贯穿本讲（也是全手册）的原则：**代码和协议文件才是事实，文档可能滞后**。emesh 的包格式你会发现 README、`.vh` 常量、`pack/unpack` 源码之间有版本差异，阅读时务必多处核对（本讲末尾的"源码地图"会标出这些坑）。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用它讲什么 |
|------|------|----------------|
| [elink/README.md](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/README.md) | elink 模块说明 | **104 位包格式的权威定义表**（"Packet format"小节） |
| [stdlib/testbench/dv_top.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/testbench/dv_top.v) | 通用仿真顶层 | `PW = 2*AW+40` 参数、dut 的 access/packet/wait 端口 |
| [emesh/hdl/emesh_if.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/hdl/emesh_if.v) | emesh 路由接口 | 用 `packet[0]`（写位）分流、`ready` 的分布式反压 |
| [emesh/hdl/emesh_memory.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/hdl/emesh_memory.v) | 带 emesh 口的 SRAM 从设备 | 读响应如何把 `srcaddr` 当作回信地址 |
| [elink/dv/elink_monitor.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/dv/elink_monitor.v) | elink 链路监视器 | 在字节流里采样出 `write/datamode/access` 字段 |
| [elink/dv/tests/test_hello.emf](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/dv/tests/test_hello.emf) | 一份真实测试激励 | 代码实践要拆解的那一行 |
| [emesh/dv/egen.pl](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/dv/egen.pl) | 随机事务生成器 | 用 `printf` 反推 `.emf` 五字段格式 |

**一个必须知道的坑**：`emesh/hdl/emesh_constants.v` 在当前 HEAD 是**空文件**（0 字节），所以包常量别指望从头文件查；同样，`emesh/hdl/emesh_pack.v` / `emesh_unpack.v` 实现的是一套**带 16 位命令字段（opcode/length/size/user）的扩展编码**，它支持的 `PW` 是 `80/112/144…`，**并不包含 104**——这与 elink 及所有外设实际使用的"经典 104 位（8 位 ctrl）"格式不是同一套字节布局。本讲讲授全系统通用的**经典 104 位格式**，以 elink README 的 Packet format 表为准；`pack/unpack` 的差异留作"已知不一致"，读源码时注意区分。

## 4. 核心概念与源码讲解

### 4.1 包格式：104 位从何而来

#### 4.1.1 概念说明

emesh 的核心思想是：**把一次事务的所有信息打包成一个固定宽度的并行数据块**，称为"包（packet）"。无论你是要写一个字节、读一个字，还是发一个读请求，都塞进同一个模子的包里，通过一组握手信号传给对方。

为什么是固定宽度？因为片上网络的连线、FIFO、仲裁器都希望"每拍搬运的东西等宽"，这样流水线和缓冲队列最好做。emesh 选了一个折中宽度：默认 **104 位**，刚好够装下"控制信息 + 目标地址 + 数据 + 回信地址"。

#### 4.1.2 核心流程

包宽由地址宽 `AW` 决定，公式是：

\[
PW = 2\cdot AW + 40
\]

默认 `AW = 32`，所以：

\[
PW = 2\times 32 + 40 = 104
\]

这 104 位切成 4 段：

| 段 | 位数 | 宽度 | 含义 |
|----|------|------|------|
| 控制 ctrl | `[7:0]` | 8 | write/datamode/ctrlmode/reserved |
| 目标地址 dstaddr | `[39:8]` | 32 | 写/读的目标地址 |
| 数据 data | `[71:40]` | 32 | 写入的数据（或读响应回读的数据） |
| 回信地址 srcaddr | `[103:72]` | 32 | 读请求的返回地址；写事务时可放高 32 位数据 |

其中常数 `40 = 8（控制）+ 32（dstaddr）`，而 `2·AW = data(AW) + srcaddr(AW)`——也就是说，地址段在经典格式里固定 32 位，只有 data/srcaddr 两段随 `AW` 伸缩。

控制字节 `[7:0]` 内部再细分为：

| 字段 | 位 | 含义 |
|------|----|------|
| write | `[0]` | 1=写事务，0=读事务 |
| datamode | `[2:1]` | 数据宽度：00=8b, 01=16b, 10=32b, 11=64b |
| ctrlmode | `[6:3]` | Epiphany 芯片的特殊路由/控制模式（普通用法为 0） |
| reserved | `[7]` | 保留 |

包的位序可以画成一张位图：

```
高位  [103 ──────────── 72][71 ─── 40][39 ──── 8][7 ────── 0]  低位
       srcaddr (回信地址)    data (数据)  dstaddr (目标地址)  ctrl (控制)
```

#### 4.1.3 源码精读

**① 包格式权威表（elink README）。** emesh 经典格式的字段定义直接写在 elink README 的 "Packet format" 小节：

[elink/README.md:86-100](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/README.md#L86-L100) —— 这就是上表 `write/datamode/ctrlmode/dstaddr/data/srcaddr` 的出处，也是本讲最权威的依据。注意它明确说："The 'access' signals indicate a valid transaction. The wait signals indicate that the receiving block is not ready"——**access 和 wait 不在 104 位包里，它们是独立的握手信号**（见 4.2）。

**② `PW = 2*AW+40` 的来源（仿真顶层）。** 这个公式不是文档随口一说的，它就写在 `dv_top` 的参数声明里：

```verilog
parameter AW  = 32;
parameter PW  = 2*AW+40;
```

见 [stdlib/testbench/dv_top.v:5-8](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/testbench/dv_top.v#L5-L8)。整个仿真平台和所有 dut 包装都靠它派生出 104 位的包总线 `[N*PW-1:0]`。

**③ 字段位序在 pack/unpack 注释里的另一套（注意差异）。** [emesh/hdl/emesh_pack.v:8-78](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/hdl/emesh_pack.v#L8-L78) 给出了一张"16 位命令字段"的包映射表，并把命令拆成 `USER/SIZE/LEN/OPCODE`，支持原子操作（ADD/AND/OR/XOR/CAS）和 1–16 拍突发。这是 emesh 的**扩展编码**，与经典 104 位格式并存于仓库中、互不兼容。读外设源码（gpio/spi/…）时看到的是经典格式；读到 pack/unpack 时要意识到它换了一套规则。

> 小结：**经典 104 位格式（本讲主线）= elink README 的 Packet format + dv_top 的 PW 派生**；pack/unpack 的 16 位命令版仅作了解。

#### 4.1.4 代码实践：手画一张包位图

1. **实践目标**：把 104 位包的 4 段及其内部字段"可视化"，建立位号到字段的直觉。
2. **操作步骤**：
   - 在纸上画一条 104 格的长条，标出位号 `103 … 0`。
   - 按 4.1.2 的表，从低位到高位依次切出 `ctrl[7:0]`、`dstaddr[39:8]`、`data[71:40]`、`srcaddr[103:72]`。
   - 再把 `ctrl[7:0]` 细分成 `write[0]`、`datamode[2:1]`、`ctrlmode[6:3]`、`reserved[7]`。
3. **需要观察的现象**：`dstaddr` 为什么从第 8 位而不是第 0 位开始？——因为最低 8 位被控制字节占了。
4. **预期结果**：得到一张与 4.1.2 位图一致的划分；`packet[0]` 落在 `write` 位上（这一点 4.3 会用到）。
5. 本实践为纸笔练习，无需运行命令。

#### 4.1.5 小练习与答案

**练习 1**：若 `AW = 32`，一个"64 位写"事务需要包里的哪些字段都有效？

> **答案**：`write=1`，`datamode=11`（64b），此时 `data[71:40]` 放低 32 位、`srcaddr[103:72]` 放高 32 位（elink README 称之为 "upper data for write"），`dstaddr[39:8]` 给目标地址。这正是 64 位写把 srcaddr 段"借用"为高 32 位数据的用法。

**练习 2**：为什么 emesh 不把 `access`（有效位）也塞进 104 位包里？

> **答案**：因为 `access/wait` 是**链路传输层**的握手信号，每拍都要参与"这一拍的数据算不算数、对方能不能收"的判断，把它独立出来更便于 FIFO、仲裁器等统一处理（类比 AXI 把 valid/ready 放在通道边上而不是数据里）。elink README 明确把 access/wait 描述为独立信号。

---

### 4.2 握手协议：access / wait 的反压

#### 4.2.1 概念说明

光定义"包长什么样"还不够，还得规定**双方怎么交接这个包**。emesh 用一对信号完成交接：

- **`access`**（发送方驱动，1 位）：="这一拍我给你的是一个有效包"。高有效。
- **`wait`**（接收方驱动，1 位）：="我还没准备好，你别急着发"。**注意是"高有效表示忙"**——这一点和很多总线（ready 高有效）相反，容易记错。

交接规则一句话：**当 `access=1` 且 `wait=0` 时，这一拍的事务被成功接收**。只要 `wait=1`，发送方就必须保持当前包不动，等下一拍再试。这构成了一个标准的 valid/ready 式反压握手（emesh 里"valid"叫 access、"not-ready"叫 wait）。

#### 4.2.2 核心流程

一个事务的传输时序可以用伪代码描述：

```
每个时钟上升沿：
  if (access_in == 1 && wait_in == 0):
      事务成功落入接收方      # 此时 packet_in 上的 104 位被采样
  else:
      发送方必须保持 packet_in 不变，下一拍重试
```

反压的精髓在于 **`wait` 是接收方逐级往回传的**。在一个多选一的路由器里，如果下游某条出口堵塞，它的 `wait` 会变高，路由器据此把自己对上游的 `wait` 也拉高，于是拥塞从下游"传染"回上游——这叫**分布式反压**。

`emesh_if.v` 里有一段非常精炼的反压逻辑：

\[
\text{cmesh\_ready\_out} = \neg\,(\text{cmesh\_access\_in} \;\wedge\; \neg\,\text{emesh\_ready\_in})
\]

意思是：本入口对上游说"我 ready"，当且仅当"我没有正在发包（access_in=0）"或"下游 emesh 口确实 ready"。一旦我正在发包且下游不 ready，我就对上游不 ready（即 wait 拉高）。

#### 4.2.3 源码精读

**① access/packet/wait 三件套端口。** 在 [stdlib/testbench/dv_top.v:67-84](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/testbench/dv_top.v#L67-L84) 里，dut 包装的接口就是典型：输入侧 `access_in / packet_in / wait_in`，输出侧 `access_out / packet_out / wait_out`（外加 `dut_active` 状态）。这就是 emesh 一个通道的标准插头。

**② 用写位分流 + ready 汇总。** [emesh/hdl/emesh_if.v:55-70](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/hdl/emesh_if.v#L55-L70) 里：

```verilog
assign cmesh_access_out = emesh_access_in &  emesh_packet_in[0];  // 写包→列网
assign rmesh_access_out = emesh_access_in & ~emesh_packet_in[0];  // 读包→行网
...
assign emesh_ready_out  = cmesh_ready_in & rmesh_ready_in & xmesh_ready_in;
```

两件事一目了然：**`packet[0]` 就是 write 位**（写走 cmesh、读走 rmesh）；以及对上游的 ready 是**三条下游支路 ready 的与**——任何一支不 ready，整条上游都被反压。

**③ 分布式反压式。** [emesh/hdl/emesh_if.v:86-93](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/hdl/emesh_if.v#L86-L93) 把上面那个 ready 公式实例化到三条支路，优先级从 cmesh→rmesh→xmesh 逐级累积 OR（越靠后的支路要等前面都不忙才敢说自己 ready）。这正是 4.2.2 说的"拥塞传染"。

**④ elink README 对 wait 的物理层补充。** [elink/README.md:70-72](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/README.md#L70-L72) 指出在 elink 物理链路上 wait 还要经"两级同步器"采样——这是跨时钟域/相位对齐的需要（细节留第 7 单元），但**语义不变**：wait 高 = 接收方只能再收最后一个。

#### 4.2.4 代码实践：阅读反压式

1. **实践目标**：确认你对 `cmesh_ready_out` 那个反压公式的理解。
2. **操作步骤**：打开 [emesh/hdl/emesh_if.v:86-93](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/hdl/emesh_if.v#L86-L93)，对 `rmesh_ready_out` 列真值表：分别考虑 `rmesh_access_in`、`emesh_ready_in`、`cmesh_ready_in` 的 0/1 组合。
3. **需要观察的现象**：`rmesh_ready_out` 在什么条件下为 0（即对上游 wait 拉高）？
4. **预期结果**：当 `rmesh_access_in=1` 且（`emesh_ready_in=0` 或 `cmesh_ready_in=0`）时，`rmesh_ready_out=0`，即"我正在发包，但下游或更高优先级的 cmesh 不 ready，于是我对上游也不 ready"。这体现了"下游不 ready 会逐级回压到上游"。
5. 本实践为源码阅读型，无需运行；若想运行仿真确认，可参考 u1-l3 的 build/sim 流程（注意仓库脚本存在 `src/` 等历史路径问题，详见 u1-l3）。

#### 4.2.5 小练习与答案

**练习 1**：`wait` 是高有效还是低有效？它和"ready"是什么关系？

> **答案**：`wait` 是**高有效**（=1 表示"忙、别发"）。它和 ready 是**反相关**：很多模块内部用 `ready` 推导 `wait`，比如 `wait_out = ~ready` 或类似。读源码时务必先确认这个极性，否则反压逻辑会完全看反。

**练习 2**：如果上游连续发来 3 个包，但下游第 2 拍起 `wait=1`，发送方该怎么做？

> **答案**：第 1 个包（`access=1, wait=0`）被收下；从第 2 拍起 `wait=1`，发送方必须**保持第 2 个包的 `packet` 与 `access=1` 不变**，每拍重试，直到 `wait=0` 才真正交付第 2 个包，之后才轮到第 3 个。期间不能推进地址或数据。

---

### 4.3 字段语义：读/写事务与 .emf 映射

#### 4.3.1 概念说明

有了"包格式"和"握手"，最后来看**一次事务到底怎么用包的各字段**。emesh 有三种基本事务：

- **写事务（write）**：主设备把 `data` 写到 `dstaddr`。`write=1`，`srcaddr` 段可放 64 位写的高 32 位数据，否则不用。
- **读事务（read request）**：主设备请求从 `dstaddr` 读数据。`write=0`，关键是 **`srcaddr` 填"回信地址"**——告诉从设备"数据读出来后寄到这里"。
- **读响应（read response）**：从设备把读出的数据包成一个新的写事务，**目标地址 = 原请求的 `srcaddr`**。这样就完成了"请求—回信"的闭环。

`datamode` 决定每次搬运的字节数（8/16/32/64 位），`ctrlmode` 在普通用法里是 0，留给 Epiphany 芯片的特殊路由模式。

#### 4.3.2 核心流程

读事务的"一来一回"流程：

```
主设备 ──read 包(dstaddr=目标, srcaddr=回信地址, write=0)──▶ 从设备
主设备 ◀─write 包(dstaddr=回信地址, data=读出的值, write=1)── 从设备
```

注意第二个包是个**写事务**，它的 `dstaddr` 正是第一个包的 `srcaddr`。从设备之所以"知道往哪回信"，全靠请求方在 srcaddr 里留下回信地址。

写事务则简单些：一去即可，无需回信（除非上层协议要求确认）。

#### 4.3.3 源码精读

**① `packet[0]` = write，一处即证。** [elink/dv/dut_axi_elink.v:168-169](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/dv/dut_axi_elink.v#L168-L169) 用一行就能拆出写/读：

```verilog
assign write_in = access_in &  packet_in[0];   // 写
assign read_in  = access_in & ~packet_in[0];   // 读
```

这说明 **bit0 就是 write 位**，且必须与 `access` 同时有效才算一次真事务。

**② 读响应回到 srcaddr。** [emesh/hdl/emesh_memory.v:177-188](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/hdl/emesh_memory.v#L177-L188) 是 emesh 从设备（带 emesh 口的 SRAM）组装读响应的关键几行：

```verilog
valid_out          <= mem_rd;
write_out           <= 1'b1;                 // 读响应是一个"写"事务
dstaddr_out[AW-1:0] <= srcaddr[AW-1:0];      // 目标地址 = 原请求的回信地址
```

第 187 行那句 `dstaddr_out <= srcaddr` 把 4.3.2 的"回信闭环"落到了代码上——读出的数据被包成一个写包，发往请求方留下的 `srcaddr`。

> 注意：`emesh_memory.v` 内部还引用了 `write_in/datamode/ctrlmode` 等未在显式 wire 列表里声明的名字（与它实例化的 `emesh_unpack` 输出名对不上），属于仓库的接口漂移；但**第 187 行那句回信逻辑是干净自洽的**，足以说明读响应语义。读其它部分请以源码实际文本为准。

**③ `.emf` 五字段格式。** `.emf` 是 OH! 的事务激励文本，一行一个事务。其字段顺序可由随机生成器 `egen.pl` 的 `printf` 反推——见 [emesh/dv/egen.pl:161-162](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/dv/egen.pl#L161-L162)：

```perl
printf("%08x_%08x_%08x_%02x_0000//WRITE ...\n", $datahi, $datalo, $dstaddr, $ctrlmode, ...);
```

对应到包的位序（从高位字段写到低位字段，再补一个控制槽）：

| .emf 字段 | 宽度 | 对应包字段 |
|-----------|------|-----------|
| 第 1 段 | 32 位 | `srcaddr[103:72]`（读时是回信地址；写时是高 32 位数据 datahi） |
| 第 2 段 | 32 位 | `data[71:40]`（datalo） |
| 第 3 段 | 32 位 | `dstaddr[39:8]` |
| 第 4 段 | 8 位 | 控制 `ctrl[7:0]`（write/datamode/ctrlmode/reserved） |
| 第 5 段 | 16 位 | 事务级流控/时序槽（**不在 104 位包内**，见下方说明） |

elink README 的 "Test format" 小节给出同样的顺序：`<srcaddr>_<data>_<dstaddr>_<ctrlmode><datamode><wr/rd>_<delay>`，见 [elink/README.md:497-517](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/README.md#L497-L517)。

> **关于第 5 段的命名（务必留意）**：elink README 把它叫 **`delay`**（事务间时序/间隔控制）；而通用 dv 框架（见 [u4-l2](u4-l2-emf-stimulus.md) 与 `stimulus.v`）把同一位置当作 **access/控制字**（bit0=有效，高位=时间戳/延迟）。两套叫法指的是同一个槽，本手册沿用 u4-l2 的叫法称其为 **access 字段**。它的精确位含义取决于具体用哪个 driver 消费该文件——这又是一处"文档/版本不一致"，以实际驱动的源码为准。

**④ 生成器如何"把写变成读"。** [emesh/dv/egen.pl:177-182](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/dv/egen.pl#L177-L182) 为每个写事务追加一个读事务时，用 `$ctrlmode & 0x6` 把 bit0 清零：

```perl
$transaction[$i]{ctrlmode} & hex(0x6)   #turn write into read
```

`0x6 = 0110₂` 保留 `datamode` 位、清掉 `write` 位——这恰好再次印证 **bit0 = write**，且读写共用同一套 `datamode`。

#### 4.3.4 代码实践：拆解一行 .emf（★ 本讲核心实践）

1. **实践目标**：把真实测试文件里的一行拆成 `dstaddr / datamode / write / access`，解释每个值的含义。
2. **操作步骤**：
   打开 [elink/dv/tests/test_hello.emf:2](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/dv/tests/test_hello.emf#L2)，该行是：

   ```
   00000000_00000000_80800000_05_0010 //WRITE
   ```

   按 4.3.3 的五字段表切分，并解码第 4 段的控制字节。

3. **拆解结果**：

   | 字段 | 原始值 | 解码 |
   |------|--------|------|
   | 第 1 段 srcaddr | `0x00000000` | 回信地址。本行是**写**事务，不需要回信，故填 0 |
   | 第 2 段 data | `0x00000000` | 要写入的数据 = 32 位全 0 |
   | 第 3 段 **dstaddr** | `0x80800000` | **写入的目标地址** |
   | 第 4 段 ctrl | `0x05` = `0000_0101₂` | 见下 |
   | 第 5 段 **access** | `0x0010` = 16 | 事务级流控/时序槽（elink README 记为 delay），值为 16 |

   把第 4 段 `0x05 = 0000_0101₂` 按控制字节布局拆开：

   - `bit0 (write)` = **1** → **写事务**
   - `bits[2:1] (datamode)` = `10₂` = 2 → **32 位**（00=8b,01=16b,10=32b,11=64b）
   - `bits[6:3] (ctrlmode)` = `0000` → 普通模式
   - `bit7 (reserved)` = 0

   **一句话解释**：这是一条"向地址 `0x80800000` 写入 32 位数据 `0x00000000`"的写事务；`access/delay` 槽值为 16。和文件里的注释 `//WRITE`、以及其后读事务（`ctrl=0x04`，bit0=0）完全自洽。

4. **需要观察的现象**：对比同文件第 18 行的读事务
   [elink/dv/tests/test_hello.emf:18](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/dv/tests/test_hello.emf#L18)
   `810d0000_DEADBEEF_80800000_04_0000 // read`——它的第 1 段 srcaddr 是 `0x810d0000`（回信地址）、第 2 段 data 是占位 `DEADBEEF`、ctrl=`0x04`（bit0=0 读、datamode 仍是 2=32 位）。结合 4.3.3 的回信闭环：这个读请求会让从设备把 `0x80800000` 处的值读出，包成一个写事务发往 `0x810d0000`。
5. **预期结果**：能独立把任意一行 `.emf` 拆成五字段，并说出它是读还是写、位宽多少、目标地址和回信地址各是多少。
6. 本实践为源码/文本阅读型，"待本地验证"的部分仅指：若你想在仿真中观察读响应是否真的回到 `0x810d0000`，需要按 u1-l3 搭好 iverilog 环境并绕开仓库的历史路径问题。

#### 4.3.5 小练习与答案

**练习 1**：`.emf` 控制字节 `0x07` 代表什么事务？

> **答案**：`0x07 = 0000_0111₂`：bit0=1（写），bits[2:1]=11=3（64 位），ctrlmode=0。即"64 位写"。注意此时 data 段（第 2 段）放低 32 位、srcaddr 段（第 1 段）放高 32 位数据。

**练习 2**：为什么读请求的第 1 段（srcaddr）几乎总是一个像 `0x810d0000` 的"看起来像地址"的值，而写请求的第 1 段常常是 0？

> **答案**：读请求必须告诉从设备"读出来后把数据寄到哪"，所以 srcaddr 是一个真实的回信地址（如 `0x810d0000` 对应某个主设备的返回缓冲区）；写事务不需要回信，srcaddr 段空闲，故常填 0（除非是 64 位写，借它放高 32 位数据）。

**练习 3**：如果接收方第 4 段写成 `0x04` 但 `.emf` 注释写的是"WRITE"，谁对？

> **答案**：以**控制字节为准**。`0x04 = 0000_0100₂`，bit0=0，是**读**事务；注释"WRITE"是错的（或笔误）。代码按 bit0 判读/写（见 4.3.3①），注释不影响行为——这是"代码为事实、文档可能滞后"的又一例。

---

## 5. 综合实践

把本讲三块知识串起来，完成一次"人肉路由器"练习：

**任务**：给你下面三条 `.emf` 事务（仿照 `test_hello.emf` 风格），逐条拆解并画出它们在 emesh 接口上的传输过程。

```
810d0000_DEADBEEF_80800000_04_0010
22222222_22222222_80800004_05_0010
810d0004_DEADBEEF_80800004_04_0000
```

要求：

1. 对每条，写出 `dstaddr / datamode / write / access` 四个字段的值和含义。
2. 指出哪条会触发"读响应"，读响应包的 `dstaddr` 应该是多少（提示：看请求的 srcaddr）。
3. 假设第二条写事务发出时接收方 `wait=1`，描述发送方在第 2、3 拍的行为。

**参考答案要点**：

1. 第 1 条：读 32 位，`dstaddr=0x80800000`，回信地址 `0x810d0000`；第 2 条：写 32 位 `0x22222222` 到 `0x80800004`；第 3 条：读 32 位，`dstaddr=0x80800004`，回信地址 `0x810d0004`。
2. 第 1、3 条是读请求，会触发读响应。第 1 条的读响应是一个写包，`dstaddr=0x810d0000`（= 其 srcaddr）、`data` 为 `0x80800000` 处读出的值（本测试里正是第 2 条……之前的写入值 0，参见 `test_hello.emf` 的写入序列）。
3. `wait=1` 期间发送方保持 `packet_out` 与 `access_out=1` 不变，每拍重试，直到 `wait=0` 才完成交付；期间不能推进到下一条事务。

## 6. 本讲小结

- emesh 经典包是 **104 位**，由 `PW = 2·AW+40`（AW=32）派生，切成 `ctrl[7:0]` + `dstaddr[31:0]` + `data[31:0]` + `srcaddr[31:0]` 四段。
- 控制字节 `ctrl` 里：**bit0=write**、bits[2:1]=datamode（8/16/32/64 位）、bits[6:3]=ctrlmode、bit7=reserved。
- 握手用 **`access`（有效）/ `wait`（忙，高有效）**：`access=1 && wait=0` 这一拍才成交；`wait` 经路由器逐级回传形成**分布式反压**。
- 三种事务：写（write=1）、读请求（write=0，`srcaddr` 填回信地址）、读响应（一个写包，`dstaddr`=请求的 `srcaddr`）。
- `.emf` 一行五字段：`srcaddr_data_dstaddr_ctrl_access`，其中第 4 段是 8 位控制字节、第 5 段是事务级流控/时序槽（elink README 称 delay、dv 框架称 access，**不在 104 位包内**）。
- 仓库存在已知不一致：`emesh_constants.v` 为空、`emesh_pack/unpack.v` 是 16 位命令的扩展编码（无 PW=104 分支）——经典格式以 elink README + dv_top 为准。

## 7. 下一步学习建议

- 想看包如何被**拆字段**和**回读拼装**，进入 [u5-l2 emesh 接口与路由](u5-l2-emesh-if-routing.md) 与 [u5-l3 包的打包、解包与回读](u5-l3-pack-unpack-readback.md)；届时你会更清楚地看到 `packet[0]`、dstaddr 如何驱动路由与 readback。
- 想看一个外设如何**完整消费**一个 emesh 包（地址译码成写选通、读时把寄存器值塞回包），跳到第 6 单元的 [u6-l1 寄存器映射机制](u6-l1-regmap-pattern.md) 和 [u6-l2 GPIO 模块全解析](u6-l2-gpio-module.md)。
- 想了解 emesh 包如何被**串行化成 LVDS 比特**在芯片间传输（字节 B00–B15、FRAME、DDR），留给第 7 单元 [u7-l1 elink 总体架构与 IO 协议](u7-l1-elink-overview.md)；本讲的 `elink_monitor.v` 已是那里的伏笔。
- 建议同时复习 [u4-l2 激励驱动与 .emf 测试格式](u4-l2-emf-stimulus.md)，把 `.emf` 的"回放/监视/黄金参考"机制与本讲的字段语义对齐。
