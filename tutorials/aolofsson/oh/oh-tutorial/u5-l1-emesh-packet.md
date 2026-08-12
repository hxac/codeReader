# u5-l1 emesh 包格式与协议

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚一个 emesh 事务包（packet）有多少位、由哪些字段拼成、每个字段在哪几个比特。
- 解释包宽 `PW` 与地址宽 `AW` 的关系 `PW = 2*AW + 40`，并能推导出 `AW=32` 时 `PW=104`。
- 区分写事务、读请求、读响应三种事务，以及 `ctrlmode` 控制模式的用途。
- 把一行 `.emf` 测试激励手工拆解成 `dstaddr / datamode / write / access` 等字段并解释含义。
- 理解与包「并排」传送的 `access`（有效）和 `wait`（反压）握手信号是如何决定一个事务是否成立的。

本讲是第 5 单元「emesh 片上网络协议」的第一讲，只讲**包本身**和**握手语义**；至于包在网格里如何路由、如何拼装/拆解，留给 u5-l2、u5-l3。

## 2. 前置知识

在进入源码前，先用通俗语言建立三个直觉。

**片上网络（Network-on-Chip, NoC）。** 一颗大芯片里有很多个「计算核心 + 本地存储」。如果每两个核心之间都拉一组专用线，连线数量会爆炸。片上网络的思路是：所有核心共用一套「数据包格式 + 路由规则」，像互联网传 IP 包一样，把事务打成包，一跳一跳传到目的地。emesh 就是 OH! 项目（最初为 Epiphany 多核芯片）定义的这样一套片上网络协议。

**事务（transaction）。** 一次「读」或「写」就是一次事务。比如「把数据 0x1234 写到地址 0x80800000」就是一个写事务；「读出地址 0x80800000 上的值」是一个读请求事务，对方把数据送回来又是一个读响应事务。emesh 用**同一个包格式**承载这几种事务，用包里的字段来区分。

**有效-就绪握手（valid/ready）。** 两个模块之间传包，发送方不能自顾自地塞，接收方可能正忙。于是除了「包数据」这组线之外，还要两根伴随信号：一根表示「我这个包是有效的」（OH! 里叫 `access`，等价于常见的 `valid`），一根表示「我准备好了，能收」（OH! 里叫 `wait` 的反相，等价于 `ready`）。只有当 `access=1` 且 `wait=0`（即 ready）同时成立，一个包才算真正被收下。这个握手是后续所有外设、链路复用的公共语言。

> 名词对照：本讲里 `access`≈`valid`，`wait`≈「没准备好」（高有效，拉高表示反压），`~wait`≈`ready`。不同教材命名不同，抓住「有效 + 就绪」这对语义即可。

本讲承接 u4-l1（通用测试平台 `dv_top` 的三段式骨架）——那里出现的 `access_in/access_out`、`packet_in/packet_out`、`wait_in/wait_out` 正是 emesh 接口，本讲把「这 104 位的 packet 里到底装了什么」彻底讲透。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用法 |
| --- | --- | --- |
| `elink/README.md` | elink 链路说明文档，内含**最权威的 emesh 包格式表** | 包字段定义、字节级 IO 映射、access/wait 语义、`.emf` 测试格式 |
| `stdlib/testbench/dv_top.v` | 通用仿真平台顶层 | 给出 `PW = 2*AW+40` 公式与 dut 的 access/packet/wait 端口契约 |
| `emesh/hdl/emesh_if.v` | emesh 到列/行/片网格的接口电路 | 用真实代码确认 bit[0]=write、access/wait 反压逻辑 |
| `emesh/hdl/emesh_monitor.v` | 仿真用事务监视器 | 确认默认包宽 `PW=104`、以及 `valid & ready` 才记录一次事务 |
| `emesh/dv/egen.pl` | 随机事务生成器 | 给出 `.emf` 一行的字段打印顺序，是字段布局的「权威打印格式」 |
| `elink/dv/tests/test_hello.emf` | 一份真实测试激励 | 提供本讲实践任务要拆解的那一行 |
| `emesh/hdl/emesh_constants.v` | （预留的）常量头文件 | **当前为空文件（0 行）**，本讲会如实说明 |

> 提醒（承接 u1-l1「代码为事实、文档可能滞后」）：`emesh/README.md` 目前只有一行标题、`emesh/hdl/emesh_constants.v` 是空文件，二者都不能提供包格式信息。emesh 包格式的权威定义实际落在 `elink/README.md`，本讲以那里为准，并辅以 RTL（`emesh_if.v`、`emesh_monitor.v`）和生成器（`egen.pl`）相互印证。

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：**包格式**（这 104 位怎么排）、**字段语义**（每个字段什么意思、读写与控制模式怎么分）、**握手协议**（access/wait 怎么让包真正被收下）。

### 4.1 包格式：104 位事务包

#### 4.1.1 概念说明

emesh 把一次事务装进一个定长的「包」里。这个包是一束并行的线（在 `AW=32` 的默认配置下是 104 位），一个时钟周期就能从发送方搬到接收方。包里需要装下「做什么事务、往哪个地址、带什么数据、回信寄到哪」这几样信息，于是把 104 位切成若干**字段（field）**。

设计上有一个朴素的目标：**用一个统一格式同时承载写、读请求、读响应**。为此包里必须有：一个区分读/写的标志位（`write`）、一个说明数据宽度的标志（`datamode`）、一个目标地址（`dstaddr`）、一份数据（`data`）、以及一个回信地址（`srcaddr`，读请求用它告诉对方「把数据寄回这个地址」，写事务时它可当高位数据用）。

#### 4.1.2 核心流程

包宽 `PW` 由地址宽 `AW` 决定。把字段宽度加起来：

\[ PW = \underbrace{AW}_{\text{dstaddr}} + \underbrace{AW}_{\text{srcaddr}} + \underbrace{32}_{\text{data}} + \underbrace{8}_{\text{控制字节}} = 2\cdot AW + 40 \]

默认 `AW = 32` 时：

\[ PW = 2 \times 32 + 40 = 104 \]

所以「104 位包」和「`PW = 2*AW+40`」是同一件事。这个公式直接出现在仿真平台顶层：

[stdlib/testbench/dv_top.v:L5-L8](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/testbench/dv_top.v#L5-L8) —— 定义 `AW=32`、`PW=2*AW+40`，于是 `PW` 恰好算出 104。

emesh 接口电路里也用同一公式：

[emesh/hdl/emesh_if.v:L14-L15](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/hdl/emesh_if.v#L14-L15) —— `parameter AW=32; parameter PW=2*AW+40;`，与 `dv_top` 完全一致。

104 位从低位到高位的字段排布如下表（这是本讲最重要的一张表，来自 `elink/README.md`）：

| 包字段 | 比特位 | 宽度 | 含义 |
| --- | --- | --- | --- |
| `write` | [0] | 1 | 1=写事务，0=读事务 |
| `datamode[1:0]` | [2:1] | 2 | 数据宽度：00=8b, 01=16b, 10=32b, 11=64b |
| `ctrlmode[3:0]` | [6:3] | 4 | Epiphany 芯片的特殊路由/控制模式 |
| `reserved` | [7] | 1 | 保留 |
| `dstaddr[31:0]` | [39:8] | 32 | 写/读请求/读响应的目标地址 |
| `data[31:0]` | [71:40] | 32 | 写数据，或读响应带回的数据 |
| `srcaddr[31:0]` | [103:72] | 32 | 读请求的回信地址；写事务时可作高 32 位数据 |

也就是说，低 8 位 `[7:0]` 是一个「控制字节」，紧接着 32 位地址、32 位数据、32 位源地址。`AW` 若改成 64/128，地址字段变宽，`PW` 按上式相应放大；本讲如无特别说明，一律用默认 `AW=32, PW=104`。

#### 4.1.3 源码精读

权威字段表来自 elink 说明文档的「Packet format」一节：

[elink/README.md:L86-L100](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/README.md#L86-L100) —— 明确写了「104 bit parallel packet interfaces」并给出 `write/datamode/ctrlmode/reserved/dstaddr/data/srcaddr` 七个字段的比特位表（即上表来源）。

文档还点明了 `access`/`wait` 与包的关系——它们是**和包并排的伴随信号**，不在 104 位之内：

[elink/README.md:L88-L90](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/README.md#L88-L90) —— 「The 'access' signals indicate a valid transaction. The wait signals indicate that the receiving block is not ready to receive the packet.」即 access=有效，wait=收方没准备好（反压）。

这一点在仿真平台 dut 的端口契约里看得最直观——`packet` 是 `PW` 位的数据，`access` 和 `wait` 都是独立的 1 位（按通道数 N 展开）：

[stdlib/testbench/dv_top.v:L67-L84](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/testbench/dv_top.v#L67-L84) —— dut 同时输出 `access_out`、`packet_out`（`[N*PW-1:0]`）、`wait_out`，并接收 `access_in`、`packet_in`、`wait_in`。可见 access/packet/wait 是三组并列的线。

仿真监视器则把默认包宽写死成 104：

[emesh/hdl/emesh_monitor.v:L11-L13](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/hdl/emesh_monitor.v#L11-L13) —— `parameter PW = 104`，并监听 `dut_valid`、`dut_packet`、`ready_in` 三路信号。

#### 4.1.4 代码实践

**实践目标**：用一张图把 104 位「钉」在脑子里。

**操作步骤**：

1. 打开 [elink/README.md:L92-L100](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/README.md#L92-L100) 的字段表。
2. 在纸上画一条 104 格的长条，从右到左标上 bit `0` 到 bit `103`。
3. 按 `[0]`、`[2:1]`、`[6:3]`、`[7]`、`[39:8]`、`[71:40]`、`[103:72]` 把它切成七段，分别写上 `write / datamode / ctrlmode / reserved / dstaddr / data / srcaddr`。

**需要观察的现象**：你会发现低 8 位 `[7:0]` 恰好是一个字节，正好对应链路把包拆成字节流传输时（elink 的 8 位 DDR 数据线）的第一个控制字节——这并非巧合，而是把「并行包」与「串行字节流」对齐的设计（详见字节级 IO 表 [elink/README.md:L46-L64](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/README.md#L46-L64)，其中 B05 字节正是 `{dstaddr[3:0],datamode[1:0],write,access}`，与并行包低段一一对应）。

**预期结果**：能默写出「低 8 位是控制字节，再 32 位 dstaddr，再 32 位 data，再 32 位 srcaddr」。

#### 4.1.5 小练习与答案

**练习 1**：若把地址宽 `AW` 改成 64，包宽 `PW` 是多少？新增的位主要分给了哪个字段？

**答案**：\( PW = 2 \times 64 + 40 = 168 \)。新增的 64 位几乎全部分给两个地址字段（`dstaddr`、`srcaddr` 各从 32 变 64），控制字节与数据段宽度不变。

**练习 2**：`reserved`（bit[7]）为什么单独留出来？

**答案**：留给未来扩展（如新增控制位），保证现有字段比特位不挪动，维持向后兼容——这是协议设计的常见手法。

---

### 4.2 字段语义：读写与控制模式

#### 4.2.1 概念说明

光知道字段在哪几位还不够，还要懂它们「取什么值、代表什么」。核心是三件事：

- **`write`（bit[0]）**：最关键的一位，1 表示写，0 表示读。它重要到接口电路直接拿它来给事务分流（写走一条通道、读走另一条）。
- **`datamode`（bits[2:1]）**：这次事务搬几个字节。`00=1 字节(8b)`、`01=2 字节(16b)`、`10=4 字节(32b)`、`11=8 字节(64b)`。可推导：字节数 \( = 8 \ll \text{datamode} \)（即 datamode 每加 1，字节数翻倍）。
- **`ctrlmode`（bits[6:3]）**：给 Epiphany 芯片用的特殊路由/控制模式（如强制往北/东/南/西路由、多播等）。在普通存储映射外设（如 gpio）里通常保持 0。

`dstaddr / data / srcaddr` 三个 32 位字段则按事务类型切换角色：写事务时 `data` 是要写的数据；读请求时 `srcaddr` 是「请把读到的数据寄回这个地址」；读响应时 `data` 是被读回的数据，`dstaddr` 通常是当初请求的地址。

#### 4.2.2 核心流程

三种事务的字段使用对比：

```
写事务 (write=1):  dstaddr=目标地址, data=要写的数据, srcaddr=可作高位数据
读请求 (write=0):  dstaddr=要读的地址, srcaddr=回信地址(数据寄回这里)
读响应 (write=0):  dstaddr=原请求地址, data=读到的数据, srcaddr=来源
```

把低 8 位控制字节用一个 8 位十六进制数表示时（这正是 `.emf` 文件第 4 个字段的写法），各事务的典型取值：

| 事务 | 控制字节(hex) | 二进制 [7:0] | write | datamode | 字节宽度 |
| --- | --- | --- | --- | --- | --- |
| 8 位写 | `0x01` | `0000_0001` | 1 | 00 | 1 |
| 16 位写 | `0x03` | `0000_0011` | 1 | 01 | 2 |
| 32 位写 | `0x05` | `0000_0101` | 1 | 10 | 4 |
| 64 位写 | `0x07` | `0000_0111` | 1 | 11 | 8 |
| 32 位读 | `0x04` | `0000_0100` | 0 | 10 | 4 |

规律：**写事务 bit[0]=1，读事务把 bit[0] 清零**；datamode 不变。这正是生成器把「写」改成「读」时做的事。

#### 4.2.3 源码精读

接口电路 `emesh_if.v` 用包的 bit[0] 给事务分流，是最有力的「bit[0]=write」证据：

[emesh/hdl/emesh_if.v:L55-L57](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/hdl/emesh_if.v#L55-L57) —— `cmesh_access_out = emesh_access_in & emesh_packet_in[0];`（bit[0]=1 的写事务发往列网格 cmesh）；`rmesh_access_out = emesh_access_in & ~emesh_packet_in[0];`（bit[0]=0 的读事务发往行网格 rmesh）。包的 bit[0] 在这里直接被当作「写标志」使用。

随机事务生成器 `egen.pl` 打印 `.emf` 一行的顺序，是字段布局的「权威打印格式」，也展示了「写改读」就是清 bit[0]：

[emesh/dv/egen.pl:L161-L162](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/dv/egen.pl#L161-L162) —— `printf("%08x_%08x_%08x_%02x_0000//WRITE ...", $datahi, $datalo, $dstaddr, $ctrlmode, ...)`。可见一行五段依次是：`datahi(=srcaddr)`、`datalo(=data)`、`dstaddr`、`控制字节`、`第5段`。

[emesh/dv/egen.pl:L177-L182](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/dv/egen.pl#L177-L182) —— 生成读事务时用 `$transaction[$i]{ctrlmode} & hex(0x6)`。`0x6 = 0b0110`，按位与会**保留 bits[2:1]（datamode）和 bits[6:3]（ctrlmode），却清掉 bit[0]（write）**，于是同一个控制字节从「写」变「读」，数据宽度不变。例如写 `0x05`（32 位写）对应的读就是 `0x05 & 0x06 = 0x04`（32 位读）。

真实测试激励里的两类事务对照：

[elink/dv/tests/test_hello.emf:L2](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/dv/tests/test_hello.emf#L2) —— `00000000_00000000_80800000_05_0010 //WRITE`：32 位写（控制字节 `0x05`）。

[elink/dv/tests/test_hello.emf:L18](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/dv/tests/test_hello.emf#L18) —— `810d0000_DEADBEEF_80800000_04_0000 // read`：32 位读（控制字节 `0x04`，bit[0] 已清零，回信地址 `0x810d0000`）。

elink 文档里也把这套 `.emf` 文本格式总结成一行模板：

[elink/README.md:L500-L516](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/README.md#L500-L516) —— 格式 `<srcaddr>_<data>_<dstaddr>_<ctrlmode><datamode><wr/rd>_<delay>`，并给出 32 位写、32 位读两组示例。

#### 4.2.4 代码实践

**实践目标**：把实践任务给出的那行 `.emf` 拆开，逐字段解释。这是本讲的核心实践。

待拆解的行：

```
00000000_00000000_80800000_05_0010
```

**操作步骤**：按 `egen.pl` 的打印顺序 `_datahi_datalo_dstaddr_控制字节_第5段` 对应到 emesh 包字段：

| 段（从左到右） | 值 | 对应包字段 | 含义 |
| --- | --- | --- | --- |
| 第 1 段 | `00000000` | `srcaddr` / `datahi` | 高 32 位数据 / 回信地址；这里全 0，写事务中高位数据为 0 |
| 第 2 段 | `00000000` | `data`（低 32 位） | 要写入的数据 = 0x00000000 |
| 第 3 段 | `80800000` | **`dstaddr`** | 目标地址 = 0x80800000 |
| 第 4 段 | `05` | 控制字节 `[7:0]` | 见下方拆解 |
| 第 5 段 | `0010` | 测试台时序字段 | 见下方说明 |

**关键拆解（任务问的 datamode / write / access）**：

- **`dstaddr` = `0x80800000`**：这次事务访问的地址（第 3 段）。
- 第 4 段控制字节 `0x05 = 0b0000_0101`：
  - **`write` = bit[0] = 1** → 这是一个**写事务**。
  - **`datamode` = bits[2:1] = `10` = 2** → **32 位（4 字节）**写（\( 8 \ll 2 = 32 \)）。
  - `ctrlmode` = bits[6:3] = `0000` = 0 → 普通模式（无特殊路由）。
  - `reserved` = bit[7] = 0。
- **`access`（第 5 段）= `0x0010`**：这是 `.emf` 的第 5 段。需要特别说明术语：在 emesh **硬件协议**里，`access` 是一个独立的 1 位「有效」信号（见 4.3），不在 104 位包内；而 `.emf` 文本的第 5 段，elink 文档把它标注为 `delay`（[elink/README.md:L500-L502](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/README.md#L500-L502)），是一个**测试台时序参数**，用来在注入本事务前后插入若干空闲周期（`0x0010` 即 16）。生成器 `egen.pl` 默认把它填 `0000`（[emesh/dv/egen.pl:L162](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/dv/egen.pl#L162)），手写测试则常用 `0010` 给接收方留点喘息时间。所以任务里的「access」对应到这一段时，本质是一个测试台注入时序字段，与协议层的 1 位 `access` 同名但不同物。

**整行读出来就是**：「向地址 `0x80800000` 写入一个 32 位的 `0x00000000`，普通控制模式，注入前后留约 16 个时钟节拍」——与该行末尾注释 `//WRITE` 完全吻合。

**需要观察的现象**：对比 [test_hello.emf:L18](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/dv/tests/test_hello.emf#L18) 的读事务（控制字节 `0x04`），你能看出读就是把写的 `0x05` 清掉 bit[0] 得到的。

**预期结果**：能说出该行是「32 位写、地址 0x80800000、数据 0」。本地能否跑仿真受仓库脚本遗留路径问题影响（见 u1-l3），仿真结果「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：写一个「16 位写」事务的控制字节（第 4 段）hex 值。

**答案**：`0x03`。bit[0]=1（写），bits[2:1]=01（16 位），其余 0，即 `0000_0011 = 0x03`。

**练习 2**：为什么 `.emf` 一行的第 1 段既叫 `srcaddr` 又叫 `datahi`？

**答案**：因为它对应包的 bits[103:72]，这一段在不同事务里扮演不同角色——读请求时是「回信地址」`srcaddr`，64 位写时是「高 32 位数据」`datahi`。一个字段两顶帽子，靠 `write` 位和事务类型来区分。

**练习 3**：把 `.emf` 行 `810d0000_DEADBEEF_80800000_04_0000` 的 srcaddr、dstaddr、write、datamode 拆出来。

**答案**：srcaddr=`0x810d0000`（回信地址）、data=`0xDEADBEEF`（占位）、dstaddr=`0x80800000`、控制字节 `0x04`→write=0（读）、datamode=`10`=2（32 位）。即「32 位读地址 0x80800000，数据回寄到 0x810d0000」。

---

### 4.3 握手协议：access 与 wait

#### 4.3.1 概念说明

光有一个 104 位的包还不算一次成功的事务。发送方还得告诉接收方「现在线上这个包是有效的，请收」（`access=1`），接收方也得能表态「我此刻没空，别塞给我」（`wait=1` 表示反压）。这就是上一章 u4-l1 里 `dv_top` 三段之间那组 `access/packet/wait` 握手的真身。

约定：

- `access` 高有效：`=1` 表示「本周期线上有一个有效包」。
- `wait` 高有效表示反压：`=1` 表示「我没准备好」；接收方能在 `wait` 拉高前再收**最后一个**包（elink 的描述）。
- 「一次事务成立」的充要条件是：**`access=1` 且 `wait=0`**（即 valid 且 ready）。监视器只在这种情况下才记录一次事务。

`wait` 的反相就是常见的 `ready`。下文源码里你会看到 `xxx_ready_out = ~(access_in & ~downstream_ready)` 这样的式子，本质就是把「我给了有效包但下游没 ready」翻译成「对上游反压」。

#### 4.3.2 核心流程

一次事务从发送到被收下的判定流程（伪代码）：

```
每个时钟上升沿:
  if (access == 1 && wait == 0):     # valid 且 ready
      事务成立 → 接收方在本周期采样 packet[103:0]
  elif (access == 1 && wait == 1):   # 有效但下游忙
      包保持，下一周期重试（反压回传给发送方）
  else:                              # access==0
      空闲，无事务
```

反压是「分布或」传播的：任何一级下游没准备好，都要逐级把 `wait`（或 `ready` 的反相）回传给上游，使整条链路在源头暂停。多主端共享资源时（如多条通道汇入一个 FIFO），`ready` 常取各下游 ready 的「与」，表示「所有人都得准备好我才算准备好」。

#### 4.3.3 源码精读

elink 文档对 access/wait 语义的文字定义：

[elink/README.md:L70-L72](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/README.md#L70-L72) —— 描述 wait 信号：「receiver raises WAIT during an active transmission indicating it can receive ONLY one more transaction」，且 wait 要用两级同步器采样（因为相位不定）。这说明 `wait` 是高有效反压、且跨时钟域时要先同步。

接口电路 `emesh_if.v` 把上面的握手写成了一行行组合逻辑。最经典的一句——把「下游没 ready」翻译成「对上游反压」：

[emesh/hdl/emesh_if.v:L86](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/hdl/emesh_if.v#L86) —— `assign cmesh_ready_out = ~(cmesh_access_in & ~emesh_ready_in);`。读法：当本通道给了有效包（`cmesh_access_in=1`）且下游 emesh 没 ready（`emesh_ready_in=0`）时，`ready_out` 拉低（反压上游）；其余情况 `ready_out=1`。这正是 `ready = NOT(valid AND NOT downstream_ready)`。

多输入会合时 ready 取「与」：

[emesh/hdl/emesh_if.v:L68-L70](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/hdl/emesh_if.v#L68-L70) —— `emesh_ready_out = cmesh_ready_in & rmesh_ready_in & xmesh_ready_in;`。要往列、行、片三个方向都分发，必须三方都 ready，emesh 这一级才 ready。

监视器只在「valid 且 ready」时记录事务，是对「事务成立条件」最好的注释：

[emesh/hdl/emesh_monitor.v:L28-L33](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/hdl/emesh_monitor.v#L28-L33) —— 端口监听 `dut_valid`、`dut_packet`、`ready_in`；在 `always @(posedge clk)` 里用 `if(nreset & dut_valid & ready_in)` 决定是否把包写进 trace 文件。即「只有 access 且 ready 同一拍，才算一次真正发生的事务」。

> 注意：`emesh_if.v` 标题注释写「WARNING: Pass through logic」（[L1](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/hdl/emesh_if.v#L1)），目前是直通占位实现（如 xmesh 方向强制 `access_out=0`），但 ready/access 的反压公式已是标准范式，后续 u5-l2 会展开路由细节。

#### 4.3.4 代码实践

**实践目标**：在源码层面验证「事务成立 = access 且 ready」。

**操作步骤**：

1. 打开 [emesh/hdl/emesh_monitor.v:L28-L33](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/hdl/emesh_monitor.v#L28-L33)。
2. 找到 `always @ (posedge clk)` 里的 `if(nreset & dut_valid & ready_in)`。
3. 回答：如果 `dut_valid=1` 但 `ready_in=0`，这个包会被记进 trace 吗？

**需要观察的现象**：只有当 `dut_valid` 与 `ready_in` 同一拍都为 1，包才被记录——这正是 access/wait 握手的「成立条件」。

**预期结果**：`dut_valid=1, ready_in=0` 时**不记录**（事务被反压，尚未成立）。结论可直接从代码条件得出，无需运行仿真。

#### 4.3.5 小练习与答案

**练习 1**：用一句话解释 `ready_out = ~(access_in & ~downstream_ready)` 的含义。

**答案**：当本端给了有效包（`access_in=1`）且下游没准备好（`downstream_ready=0`）时，本端对上游拉低 ready（反压）；否则本端 ready。

**练习 2**：`wait=1` 时，发送方还能不能再塞一个包？

**答案**：按 elink 描述，`wait=1` 表示接收方「还能再收最后一个包」，之后必须停止。所以 `wait` 是「预警式」反压，而不是立刻断流——工程设计上给发送方留了一点余量来处理同步后的 wait。

**练习 3**：为什么 `emesh_ready_out` 要取 cmesh/rmesh/xmesh 三路 ready 的「与」而不是「或」？

**答案**：因为一个 emesh 事务要同时分发到列、行、片三个方向，必须三方都能接收才算「这一级准备好」；只要任一方没 ready，包就可能丢失，所以用「与」做最保守的会聚。

---

## 5. 综合实践

把本讲三个最小模块串起来，做一次「人肉协议解码器」。

**任务**：下面是从 [gpio/dv/tests/test_basic.emf:L9](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/dv/tests/test_basic.emf#L9) 摘的一行真实激励：

```
DEADBEEF_DEADBEEF_00000008_04_0010 // read gpio_in
```

请完成：

1. **拆字段**：按 `egen.pl` 的五段顺序，把 srcaddr/datahi、data、dstaddr、控制字节、第 5 段分别列出来。
2. **解控制字节**：把 `0x04` 拆成 write / datamode / ctrlmode / reserved，说出这是「几位」的「读/写」事务。
3. **说语义**：结合注释 `// read gpio_in`，用一句话描述这次事务要干什么、数据寄回哪个地址。
4. **换写法**：如果把这次事务改成「32 位写、写入数据 `0xDEADBEEF`」，控制字节第 4 段应改成什么？为什么？
5. **判握手**：假设注入这一行时接收方正忙，监视器看到的 `ready_in=0`，这行事务会在本时钟周期被记录进 trace 吗？为什么？

**参考答案**：

1. srcaddr/datahi=`0xDEADBEEF`、data=`0xDEADBEEF`（占位）、dstaddr=`0x00000008`（gpio_in 寄存器地址）、控制字节=`0x04`、第 5 段=`0x0010`（测试台时序字段）。
2. `0x04 = 0b0000_0100`：write=bit[0]=0（读）、datamode=bits[2:1]=`10`=2（32 位）、ctrlmode=0、reserved=0。即「32 位读」。
3. 「从 gpio 的输入寄存器 `gpio_in`（地址 `0x00000008`）读出一个 32 位值，结果寄回 `0xDEADBEEF` 指定的回信地址」（此处回信地址为占位值）。
4. 改成 32 位写，控制字节应为 `0x05`（把 `0x04` 的 bit[0] 置 1 即可，datamode 不变）。这正是「写改读清 bit[0]」的逆操作，也符合 `0x05 & 0x06 = 0x04` 的关系。
5. 不会。监视器只在 `dut_valid & ready_in` 同时为 1 时记录；`ready_in=0` 表示接收方反压，事务尚未成立，故本周期不记录，包会等到下游 ready 的那一拍才被采样。

> 提示：第 4、5 题分别对应「字段语义」与「握手协议」两个最小模块；能把这两题说清，说明本讲的核心已经掌握。仿真运行受仓库脚本遗留问题影响（见 u1-l3），运行结果「待本地验证」。

## 6. 本讲小结

- emesh 用一个定长包承载片上事务；默认 `AW=32` 时包宽 `PW = 2*AW+40 = 104` 位。
- 104 位 = 低 8 位控制字节（`write`/`datamode`/`ctrlmode`/`reserved`）+ 32 位 `dstaddr` + 32 位 `data` + 32 位 `srcaddr`；字段表以 [elink/README.md:L92-L100](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/README.md#L92-L100) 为准。
- `write` 是 bit[0]，被 `emesh_if.v` 直接用来给写/读事务分流；`datamode` 决定 8/16/32/64 位宽度；写改读只需清 bit[0]（`& 0x6`）。
- `.emf` 一行五段为 `datahi(srcaddr)_datalo(data)_dstaddr_控制字节_第5段`，第 5 段是测试台时序字段（elink 文档称 `delay`），与协议层 1 位 `access` 同名但不同物。
- `access`（有效）与 `wait`（反压）是包外伴随信号；事务成立的充要条件是 `access=1 且 wait=0`，监视器只在 `valid & ready` 同拍记录事务。
- 权威定义落在 `elink/README.md` 与 RTL；`emesh/README.md`、`emesh_constants.v` 当前为空/桩，阅读须以源码为准。

## 7. 下一步学习建议

本讲只讲了「包长什么样、怎么握手」。接下来：

- **u5-l2 emesh 接口与路由**：读 `emesh_if.v`、`emesh_mux.v`、`emesh_decode.v`，看包如何根据 `dstaddr` 和 `write` 位在列/行/片网格间分发，以及 ready 反压如何逐级会聚——本讲 4.3 的反压公式会那里被反复用到。
- **u5-l3 包的打包、解包与回读**：读 `emesh_pack.v` / `emesh_unpack.v` / `emesh_readback.v`，看字段如何拼成包又拆回字段。注意 `emesh_pack.v` 实现的是**更新的命令字段格式**（16 位 cmd：opcode/length/size/user，支持 burst 与原子操作），与本讲的经典 104 位格式并存，届时会对比说明。
- 想立刻看到包在跑，可回到 u4-l2/u4-l3，用 `build.sh` + `sim.sh` 对 `elink/dv/tests/test_hello.emf` 做一次仿真（注意仓库脚本的遗留路径问题），在波形里对照 `access/packet/wait` 三组信号验证本讲的握手结论。
