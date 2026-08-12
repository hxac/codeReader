# 寄存器映射机制：.vh 头文件模式

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 OH! 里「寄存器映射（register map，简称 regmap）」到底是什么、解决什么问题。
- 读懂任意一个 `xxx_regmap.vh` 头文件：它如何用大写宏常量给每个寄存器分配一个地址编号。
- 看懂 RTL 里那几行「地址译码 + 写选通」的标准写法，理解一次写事务是如何变成「某个寄存器在这一拍被更新」的。
- 理解地址总线上哪些位由本 IP 译码、哪些位交给片上网络去路由，以及为什么这样分工。
- 拿到一个新 IP，能对照它的 README 地址表和 `.vh` 文件，自己列出「寄存器名 ↔ 地址位 ↔ 写选通信号」的对照表。

本讲是第 6 单元（可配置外设 IP）的第一讲，承接第 5 单元的 emesh 协议。后续讲 GPIO、SPI、emailbox 等外设时，都会反复用到这里建立的 regmap 范式。

## 2. 前置知识

本讲默认你已经掌握以下概念（若不熟练，建议先看对应讲义）：

- **emesh 事务包**（u5-l1）：一个 104 位的定长包，里面有 `write`（写位）、`dstaddr`（目标地址）、`data`（数据）等字段；包外还有 `access`（≈有效）和 `wait`（高有效反压）两个伴随信号。一次写事务成立的条件是 `access=1 & wait=0 & write=1`。
- **包的解包**（u5-l3）：RTL 里会把 104 位扁平包「解包」回 `write_in / dstaddr_in / data_in` 等独立字段，本讲的译码就发生在这些解包后的字段上。
- **Verilog 宏与头文件**（u1-l4）：OH! 用 `.vh` 头文件加 `` `ifndef / `define / `endif `` 守卫定义大写宏常量；设计文件用 `` `include `` 把它引入。
- **时序逻辑与非阻塞赋值**（u2-l2）：寄存器在时钟沿用 `<=` 更新，本讲的写选通就是用来「点亮」某个 `always` 块里的更新条件。

几个本讲会用到的小术语：

- **寄存器（register）**：这里不是指 D 触发器，而是指「软件可以读写的、有固定地址的一个存储单元」，比如 GPIO 的方向控制寄存器 `GPIO_DIR`。硬件上它通常是一组触发器。
- **地址映射（memory map）**：把一整段地址空间切分成一块块，每块对应一个寄存器或一段存储，就像一栋大楼里的房间号。
- **写选通（write strobe）**：一个只在高电平维持一拍（一个时钟周期）的脉冲信号，用来表示「这一拍请把数据写进某个寄存器」。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
|------|------|
| `gpio/hdl/gpio_regmap.vh` | GPIO 的寄存器地址宏定义，本讲的「主范例」。 |
| `gpio/hdl/gpio.v` | GPIO 顶层 RTL，展示了完整的「include → 译码 → 写选通 → 更新寄存器 → 回读」链路。 |
| `edma/hdl/edma_regmap.vh` | EDMA 的寄存器地址宏定义，与 GPIO 对比，展示不同的地址位切片。 |
| `edma/hdl/edma_regs.v` | EDMA 的寄存器子模块，展示与 GPIO 完全同构的译码写法。 |
| `edma/README.md` | EDMA 的寄存器地址表（文档版），用来和 `.vh`（代码版）对照。 |
| `gpio/README.md` | GPIO 的寄存器地址表（文档版）。 |
| `spi/hdl/spi_regmap.vh`、`elink/hdl/elink_regmap.vh`、`emailbox/hdl/emailbox_regmap.vh` | 旁证：说明 regmap 范式在整个项目里是统一的，并有更丰富的变体。 |

> 阅读提醒（贯穿全手册的原则）：文档（README 的地址表）和代码（`.vh` 与 RTL）都描述了寄存器映射，但**代码才是事实**。当两者不一致时，以代码为准。本讲会专门指出 EDMA 头文件注释与 RTL 实现的一处小出入。

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：

1. **regmap `.vh` 头文件**：给每个寄存器起一个大写名字、分配一个地址编号，作为「单一事实源」。
2. **地址译码**：从 `dstaddr` 里切出一段地址位，判断这次事务要访问哪个寄存器。
3. **写选通**：把判断结果变成一组单拍脉冲，用来驱动各寄存器的更新。

最后用 GPIO 的回读（readback）把三件事串成一个完整闭环。

### 4.1 regmap `.vh` 头文件：寄存器地址的「单一事实源」

#### 4.1.1 概念说明

一个可配置外设（比如 GPIO）通常有十几个寄存器：方向、输出、输入、中断屏蔽……软件要能逐个读写它们。这就需要两样东西：

- 给每个寄存器分配一个**唯一的地址编号**，软件往这个地址写数据就是写这个寄存器。
- 让硬件和软件**看到同一张地址表**，否则软件往 `0x0` 写的数据会被硬件理解到别的寄存器上。

OH! 的做法是：把这张地址表写成一个 `.vh` 头文件，里面用大写宏常量（如 `` `GPIO_DIR ``、`` `GPIO_OUT ``）定义每个寄存器的地址编号。RTL 用 `` `include `` 引入它，这样：

- RTL 里写 `` `GPIO_DIR `` 而不是写魔法数字 `4'h0`，代码可读、改地址只改一处。
- 同一个 `.vh` 也可以被驱动软件、测试激励复用，作为「单一事实源（single source of truth）」。

这种「一个 IP 配一个 `xxx_regmap.vh`」的写法在整个项目里是统一的。仓库里一共有 7 个这样的文件：

```
edma/hdl/edma_regmap.vh      elink/hdl/elink_regmap.vh
emailbox/hdl/emailbox_regmap.vh  etrace/hdl/etrace_regmap.vh
gpio/hdl/gpio_regmap.vh      mio/hdl/mio_regmap.vh
spi/hdl/spi_regmap.vh
```

#### 4.1.2 核心流程

一个 `xxx_regmap.vh` 的标准骨架是：

```
`ifndef XXX_REGMAP_VH_        // ① 文件级 include 守卫
 `define XXX_REGMAP_VH_

 `define REG_NAME   <位宽>'d<编号>   // ② 每个寄存器一个大写宏 = 地址编号
 `define REG_NAME2  <位宽>'d<编号>
 ...

`endif
```

两件事：

1. **include 守卫**：用 `` `ifndef / `define / `endif `` 包起来，保证同一个头文件被多次 `include` 时不会重复定义宏、不报错。
2. **宏常量即地址编号**：每个 `` `define `` 给寄存器起一个大写名字，右值是这个寄存器在地址切片里的编号（用指定位宽的字面量，如 `4'h0`、`5'd2`、`6'd32`）。

头文件第一行通常还有一条注释，说明这些编号对应地址总线的**哪一段位**，例如「maps to addr[6:3]」。这条注释是理解整个译码的关键线索。

#### 4.1.3 源码精读

先看 GPIO 的 regmap，它是本讲的主范例。

[gpio/hdl/gpio_regmap.vh:1-16](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/hdl/gpio_regmap.vh#L1-L16) —— GPIO 全部 11 个寄存器的地址宏定义。逐段看：

- 第 1 行注释 `//64 bit registers, maps to addr[6:3]`：寄存器是 64 位（8 字节）的，地址编号对应 `dstaddr` 的第 6~3 位。
- 第 2~3 行、第 16 行：文件级 include 守卫 `GPIO_REGMAP_VH_`。
- 第 4~14 行：11 个寄存器宏，从 `` `GPIO_DIR ``(`4'h0`) 到 `` `GPIO_ILATCLR ``(`4'hA`)，编号 0~10。注意编号用的是 4 位十六进制（`4'h`），正好对应 4 位的 `addr[6:3]`。

再看 EDMA 的 regmap，做对比。

[edma/hdl/edma_regmap.vh:1-13](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/edma/hdl/edma_regmap.vh#L1-L13) —— EDMA 的 8 个寄存器宏。关键差异：

- 第 1 行注释 `//Registers addr[6:2], 64 bits per register`：编号对应 `addr[6:2]`（5 位，用 `5'd` 十进制字面量），编号范围 0~7。
- **一处需要留意的出入**：注释声称「64 bits per register」，但后续 RTL（`edma_regs.v`）实际写入的是 `data_in[31:0]`（32 位），地址步长也是 4 字节而非 8 字节。也就是说，EDMA 的寄存器实际是 32 位的，这条注释偏「理想化」。这正是「以代码为准」的一个活例：地址切片以 RTL 里的 `dstaddr[6:2]` 为准，注释的「64 位」仅供参考。

为什么 GPIO 用 `addr[6:3]`、EDMA 用 `addr[6:2]`？这关系到地址步长，下一节细讲。

最后看两个「旁证」，说明这个范式还有更丰富的变体：

- [spi/hdl/spi_regmap.vh:8-25](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/spi/hdl/spi_regmap.vh#L8-L25)：SPI 的寄存器编号是**稀疏**的——`` `SPI_TX ``=`6'd8`、`` `SPI_RX0 ``=`6'd16`、`` `SPI_USER ``=`6'd32`，并不连续。这说明宏的右值就是「地址切片里的真实编号」，完全可以跳号。此外它还定义了字段级常量（`` `SPI_WR ``/`` `SPI_RD ``/`` `SPI_FETCH ``），说明 `.vh` 不只能存寄存器号，也能存命令字段的编码。
- [emailbox/hdl/emailbox_regmap.vh:1-19](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emailbox/hdl/emailbox_regmap.vh#L1-L19)：这里出现了**两级守卫**。外层 `` `ifndef EMAILBOX_REGMAP_V_ `` 是文件级；内层还有 `` `ifndef E_MAILBOXLO `` 这种**符号级**守卫。原因是 `E_MAILBOXLO` 这个常量在 `elink_regmap.vh` 里也定义了（见 [elink/hdl/elink_regmap.vh:49-53](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/elink_regmap.vh#L49-L53)），如果两个文件都被 include，没有这层内守卫就会「宏重定义」报错。这是 include 守卫更精细的用法。
- [elink/hdl/elink_regmap.vh:4-13](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/elink_regmap.vh#L4-L13)：elink 是最复杂的例子，它的注释把整条地址总线 `[31:0]` 切成了 `LINKID / GROUP / 寄存器组 / 寄存器地址 / 忽略位` 多层。这提示我们：简单外设只译码低几位，复杂 IP 会把高位也用来做组/链路路由。

#### 4.1.4 代码实践

**实践目标**：亲手把「文档地址表」和「代码宏定义」对一遍，体会它们是同一张表。

**操作步骤**：

1. 打开 [gpio/README.md:13-25](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/README.md#L13-L25)，这是 GPIO 的文档地址表。
2. 打开 [gpio/hdl/gpio_regmap.vh:4-14](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/hdl/gpio_regmap.vh#L4-L14)，这是代码宏定义。
3. 逐行核对：README 里 `GPIO_DIR | 0x0` 应当对应 `` `GPIO_DIR 4'h0 ``；`GPIO_ILATCLR | 0xA` 对应 `` `GPIO_ILATCLR 4'hA ``，以此类推。

**需要观察的现象**：两份资料的寄存器名、编号、读写属性（WR/RD）应当一一对应。

**预期结果**：GPIO 的 11 个寄存器全部能对上，文档与代码一致。若做 EDMA 的对照（[edma/README.md:24-33](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/edma/README.md#L24-L33) vs `edma_regmap.vh`），同样能对上 8 个寄存器。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `gpio_regmap.vh` 要用 `` `ifndef GPIO_REGMAP_VH_ `` 把整个文件包起来？如果不包会怎样？

**参考答案**：防止同一个头文件被多次 `` `include `` 时重复定义宏导致编译报错。Verilog 里 `` `define `` 是全局的且不覆盖，重复定义同一宏名会触发警告甚至错误。包了守卫之后，第一次 include 时定义 `GPIO_REGMAP_VH_`，之后再 include 时整个内容被跳过。

**练习 2**：SPI 的 `` `SPI_TX `` 定义为 `6'd8` 而不是 `6'd3`，这说明 regmap 宏的右值必须连续吗？

**参考答案**：不必连续。宏右值就是该寄存器在地址切片里的真实编号，可以跳号。SPI 把 TX/RX/USER 排在较后的编号上，是为给寄存器组之间留出地址空间（例如 FIFO 区与控制区分开）。

### 4.2 地址译码：从 dstaddr 到寄存器号

#### 4.2.1 概念说明

有了 regmap 宏，下一步是回答：当一次写事务带着某个 `dstaddr` 到达 GPIO 时，硬件怎么知道它要写的是 11 个寄存器里的哪一个？

这就需要**地址译码**：从 32 位的 `dstaddr_in` 里切出一段「寄存器号」位段，拿它去和各个 regmap 宏比较，比中了就说明这次事务指向那个寄存器。

这里有一个关键的分工问题：`dstaddr` 有 32 位，GPIO 只看了 `addr[6:3]` 这 4 位，那高位 `[31:7]` 和低位 `[2:0]` 谁来管？

- **低位 `[2:0]`（字节偏移）**：一个 GPIO 寄存器是 64 位 = 8 字节，地址的低 3 位用来在这 8 字节里选字节，对 GPIO 这种按整字访问的 IP 来说属于「寄存器内部偏移」，译码时忽略。
- **高位 `[31:7]`（基址）**：由**片上网络（emesh 互连）的系统级路由**负责。系统事先约定「GPIO 这个 IP 挂在某段基址上」，只有目标地址落在那段基址里的事务才会被路由到 GPIO。所以 GPIO 自己不必检查高位——能到达它的事务，基址已经对了。

这是一种很干净的分层：**系统路由负责选 IP，IP 内部的 regmap 负责选寄存器**。GPIO 只需要在到达自己的事务里，用低几位挑出具体寄存器即可。

#### 4.2.2 核心流程

地址译码的标准两步：

```
// 第一步：本次事务是不是「写」？（access 有效 + 写位为 1）
assign reg_write = access_in & write_in;

// 第二步：目标地址的寄存器号位段，等于哪个宏？
assign xxx_write = reg_write & (dstaddr_in[<高位>:<低位>] == `REG_XXX);
```

地址切片的位段由 regmap 头文件的注释（如 `addr[6:3]`）和宏的位宽（如 `4'h`）共同决定。一个直观的换算：

- 寄存器步长 = 2^(被忽略的低位个数) 字节。
- GPIO 忽略低 3 位 `[2:0]` → 步长 8 字节（64 位寄存器）。
- EDMA 忽略低 2 位 `[1:0]` → 步长 4 字节（32 位寄存器）。
- 两者都在 `addr[6:0]` 这个 128 字节的窗口内译码，只是切法不同：GPIO 切成 16 个 8 字节槽，EDMA 切成 32 个 4 字节槽。

用表格对比：

| IP | 地址切片 | 切片位宽 | 最多寄存器数 | 寄存器步长 | 译码窗口 |
|----|----------|----------|--------------|-----------|----------|
| GPIO | `addr[6:3]` | 4 位 | 16（用 11） | 8 字节 | `addr[6:0]`=128B |
| EDMA | `addr[6:2]` | 5 位 | 32（用 8） | 4 字节 | `addr[6:0]`=128B |

#### 4.2.3 源码精读

先看 GPIO 怎么得到「是不是写」这个总开关。

[gpio/hdl/gpio.v:91-94](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/hdl/gpio.v#L91-L94) —— 解包后字段到 `reg_write / reg_read / reg_wdata` 的转换。这段代码做三件事：

- `reg_write = access_in & write_in`：`access` 有效且写位为 1，表示这是一次写事务。
- `reg_read = access_in & ~write_in`：`access` 有效且写位为 0，表示读事务。
- `reg_wdata = data_in[N-1:0]`：把解包出的数据取低 N 位作为待写数据。

> 顺带一提，本讲关注的是译码逻辑，而把 104 位包拆成 `write_in/dstaddr_in/data_in` 的是 [gpio/hdl/gpio.v:77-89](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/hdl/gpio.v#L77-L89) 实例化的 `enoc_unpack`。这里出现的模块名 `enoc_unpack` 是历史遗留命名（现行 emesh 库里对应的是 `emesh_unpack`），不影响译码逻辑，按 u5-l3 的约定「以源码文本为准」即可。

接着看 GPIO 的核心译码——每个寄存器一行：

[gpio/hdl/gpio.v:96-104](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/hdl/gpio.v#L96-L104) —— 9 条地址译码语句，全部遵循同一个模式 `reg_write & (dstaddr_in[6:3]==`宏`)`：

- `dir_write` = 写事务且 `addr[6:3]==`GPIO_DIR`（方向寄存器）。
- `outreg_write` = 写事务且命中 `` `GPIO_OUT ``（输出寄存器）。
- `imask_write / itype_write / ipol_write / ilatclr_write / outclr_write / outset_write / outxor_write` 依此类推。

注意：GPIO 的 11 个 regmap 宏里，这里只译出了 9 个 `*_write`。为什么？因为 `GPIO_IN` 和 `GPIO_ILAT` 是只读寄存器（输入值、中断锁存状态），本来就没有写选通；而 `GPIO_OUT` 这一个寄存器被拆成了 4 种写法（直接写 / 清 / 置 / 翻转），所以 `outreg/outclr/outset/outxor` 四个选通都指向同一个 `gpio_out` 寄存器，下一节细讲。

再看 EDMA 的同构写法，印证这是通用范式：

[edma/hdl/edma_regs.v:120-128](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/edma/hdl/edma_regs.v#L120-L128) —— EDMA 的译码。和 GPIO 几乎一模一样，唯一区别是地址切片换成了 `dstaddr_in[6:2]`、宏换成了 `` `EDMA_* ``：

- `reg_write = write_in & reg_access_in`（注意 EDMA 这里把 `write_in` 写在前面，顺序无所谓，逻辑等价）。
- `config_write = reg_write & (dstaddr_in[6:2]==`EDMA_CONFIG)`，依此类推 8 个寄存器。

> 结论：**地址切片的位段是每个 IP 自己定的（写在 regmap 注释里），译码语句的模式是全项目统一的**。你看懂 GPIO 的 9 行，就能看懂 EDMA 的 8 行、SPI 的若干行。

#### 4.2.4 代码实践

**实践目标**：验证「地址切片 = regmap 宏位宽 = 注释里的 addr[]」三者一致。

**操作步骤**：

1. 看 [gpio/hdl/gpio_regmap.vh:1](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/hdl/gpio_regmap.vh#L1) 注释 `addr[6:3]`（4 位）→ 宏用 `4'h`（4 位）→ 译码用 `dstaddr_in[6:3]`（4 位）。
2. 看 [edma/hdl/edma_regmap.vh:1](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/edma/hdl/edma_regmap.vh#L1) 注释 `addr[6:2]`（5 位）→ 宏用 `5'd`（5 位）→ 译码用 `dstaddr_in[6:2]`（5 位）。

**需要观察的现象**：三者位宽必须一致；如果某天你看到一个 IP 注释写 `addr[6:3]` 但宏用 `5'd`，那基本是 bug 或历史遗留，要警惕。

**预期结果**：GPIO 三者都是 4 位，EDMA 三者都是 5 位，完全自洽。

#### 4.2.5 小练习与答案

**练习 1**：GPIO 的译码语句里为什么没有检查 `dstaddr_in[31:7]`？如果两个不同基址的事务都到达 GPIO，会发生什么？

**参考答案**：因为高位（基址）由 emesh 系统级路由负责：系统约定只有落在 GPIO 基址段的事务才会路由到 GPIO，所以 GPIO 自己只看低位的寄存器号。如果设计正确，到达 GPIO 的事务基址必然正确；若系统路由配错导致别的事务也到达 GPIO，GPIO 会按低位去误译码，这是系统配置问题而非 GPIO 的 bug。

**练习 2**：`addr[6:3]` 对应的寄存器步长是多少？为什么 GPIO 需要这个步长？

**参考答案**：步长是 8 字节。因为 GPIO 寄存器是 64 位（8 字节）宽，地址低 3 位 `[2:0]` 用来在 8 字节内选字节，所以相邻寄存器的地址相差 8，对应忽略低 3 位、用 `addr[6:3]` 做寄存器号。

### 4.3 写选通：把一次写事务变成寄存器更新

#### 4.3.1 概念说明

地址译码产出的是一组 `*_write` 信号，它们就是**写选通（write strobe）**：高电平只在该寄存器被写入的那一拍维持一个周期，其余时刻为 0。

写选通的价值在于把「复杂的地址判断」和「简单的寄存器更新」彻底解耦：

- 译码逻辑负责「这次该写谁」，产出若干 one-hot（独热）的选通脉冲。
- 每个寄存器的 `always` 块只需关心「我的选通来了吗？来了就把 `reg_wdata` 装进去」，写法极简、极统一。

GPIO 还展示了一个高级用法——**一个寄存器多个写选通**：`GPIO_OUT`（输出寄存器）有 4 种写法，分别由 `outreg_write / outclr_write / outset_write / outxor_write` 触发，对应「直接写整体值 / 按位清零 / 按位置 1 / 按位翻转」。软件只需往不同地址写同一个目标位，就能原子地做位级操作，不必「读—改—写」。这就是 README 里说的「Special And/or/xor register access modes for atomic control」。

#### 4.3.2 核心流程

写选通驱动寄存器更新的标准写法（伪代码）：

```
always @(posedge clk or negedge nreset)
  if (!nreset)
    my_reg <= 0;              // 复位初值
  else if (my_reg_write)      // 我的写选通来了
    my_reg <= reg_wdata;      // 把数据装进来
```

对「一寄存器多选通」（如 GPIO_OUT），先用一个 mux 把不同写法算出「下一拍该变成什么」，再用一个总选通把它装进去：

```
// 总写选通 = 任一别名命中
assign out_write = outreg_write | outclr_write | outset_write | outxor_write;

// mux: 4 种写法各算一个候选值
out_dmux = (直接写) ? reg_wdata :
           (清零)   ? old & ~reg_wdata :
           (置位)   ? old | reg_wdata :
                      old ^ reg_wdata;

// 统一用总选通装进寄存器
always @(posedge clk or negedge nreset)
  if (!nreset) gpio_out <= 0;
  else if (out_write) gpio_out <= out_dmux;
```

#### 4.3.3 源码精读

先看 GPIO_OUT 的「多选通合并 + mux」实现。

[gpio/hdl/gpio.v:106-109](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/hdl/gpio.v#L106-L109) —— 把 4 个别名选通 OR 成一个总写选通 `out_write`：

- `out_write = outreg_write | outclr_write | outset_write | outxor_write`。这 4 个选通对应 `GPIO_OUT / GPIO_OUTCLR / GPIO_OUTSET / GPIO_OUTXOR` 四个地址（编号 0x2~0x5），它们都作用于同一个 `gpio_out` 寄存器。

[gpio/hdl/gpio.v:139-145](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/hdl/gpio.v#L139-L145) —— 用 `oh_mux4` 按 4 个选通挑出「下一拍的输出值」。4 个输入分别是：

- `in0 = reg_wdata`（直接写整体值，`sel0=outreg_write`）。
- `in1 = gpio_out & ~reg_wdata`（按位清零，`sel1=outclr_write`）。
- `in2 = gpio_out | reg_wdata`（按位置 1，`sel2=outset_write`）。
- `in3 = gpio_out ^ reg_wdata`（按位翻转，`sel3=outxor_write`）。

[gpio/hdl/gpio.v:147-151](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/hdl/gpio.v#L147-L151) —— 用总选通 `out_write` 把 `out_dmux` 装进 `gpio_out`，复位时清零。

再看一个「单选通」的标准例子，对比理解。

[gpio/hdl/gpio.v:117-121](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/hdl/gpio.v#L117-L121) —— 方向寄存器 `gpio_dir` 的更新，这就是最朴素的单选通写法：复位清零，否则 `dir_write` 来了就把 `reg_wdata` 装进去。GPIO 的 `imask/itype/ipol` 等寄存器都是同一个套路。

EDMA 里也是同一套。看 EDMA 的 config 寄存器：

[edma/hdl/edma_regs.v:134-138](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/edma/hdl/edma_regs.v#L134-L138) —— `config_reg` 复位为 `DEF_CFG`，`config_write` 来了就装入 `data_in[31:0]`。注意 EDMA 这里直接用 `data_in[31:0]`（32 位），与头文件注释的「64 位」不符——再次印证以 RTL 为准。

最后把写选通和**读回（readback）**配对看，形成完整闭环。写选通解决了「写谁」，回读解决「读谁」。

[gpio/hdl/gpio.v:213-223](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/hdl/gpio.v#L213-L223) —— GPIO 的回读：一个 `case(dstaddr_in[6:3])`，按地址把对应寄存器的当前值选到 `read_data` 上（`GPIO_IN` 选输入同步值、`GPIO_ILAT` 选中断锁存、`GPIO_DIR/IMASK/IPOL/ITYPE` 选各自配置，`default` 给 0）。注意回读用的是**同一个地址切片** `addr[6:3]` 和**同一组 regmap 宏**，读写共用一张地址表。`read_data` 随后被 [gpio/hdl/gpio.v:225-238](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/hdl/gpio.v#L225-L238) 的 `emesh_readback` 打包回送（u5-l3 已讲过回读流水线，这里只关注它消费 `read_data`）。

> 小结这条链路：**写事务**走 `enoc_unpack → reg_write → *_write 译码 → always 更新寄存器`；**读事务**走 `enoc_unpack → reg_read → case 选 read_data → emesh_readback 打包回送`。两者共用 regmap 宏与 `addr[6:3]` 切片。

#### 4.3.4 代码实践

**实践目标**：跟踪一次「按位置位」的 GPIO 写事务，看清它最终改的是哪个寄存器。

**操作步骤**：

1. 假设软件要原子地把 GPIO 第 0 位置 1。根据 regmap，应写地址编号 `` `GPIO_OUTSET ``=`4'h4`，即 `dstaddr[6:3]=4'h4`、`dstaddr[2:0]` 任意（整字访问取 0）、`write=1`，数据 `data=0x000001`。
2. 在 [gpio/hdl/gpio.v:91](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/hdl/gpio.v#L91) 得到 `reg_write=1`。
3. 在 [gpio/hdl/gpio.v:103](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/hdl/gpio.v#L103) 命中 `outset_write=1`（因为 `addr[6:3]==`GPIO_OUTSET`）。
4. 在 [gpio/hdl/gpio.v:106-109](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/hdl/gpio.v#L106-L109) 使 `out_write=1`。
5. 在 [gpio/hdl/gpio.v:139-145](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/hdl/gpio.v#L139-L145) 选中 `in2 = gpio_out | reg_wdata`（按位或）。
6. 在 [gpio/hdl/gpio.v:147-151](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/hdl/gpio.v#L147-L151) 下个时钟沿把结果装进 `gpio_out`。

**需要观察的现象**：6 步串起来，一次「置位」事务精准地只改了 `gpio_out` 的第 0 位，其余位不变，且整个过程是原子的（一拍完成，无需先读后改）。

**预期结果**：若 `gpio_out` 原为 `0x000000`，写 `GPIO_OUTSET` 地址 + 数据 `0x000001` 后，`gpio_out` 变为 `0x000001`。完整端到端仿真建议在学完 u4 的仿真平台后，用 `gpio/dv` 下的 `.emf` 测试验证；若当前未搭好环境，此处为「源码阅读型实践」，跟踪结论「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 GPIO_OUT 要设计成 4 个地址（OUT/OUTCLR/OUTSET/OUTXOR）而不是一个？软件想「只翻转第 3 位、其余不动」，用哪个地址最方便？

**参考答案**：为了支持原子的位级操作。若只有一个 OUT 地址，软件要翻转某位必须先读回、改位、再写回（读—改—写），在多主端或中断场景下不安全。有了别名地址，软件只需往 `GPIO_OUTXOR`（编号 0x5）写 `0x000008`，硬件一拍内就完成「`gpio_out = gpio_out ^ 0x000008`」，第 3 位翻转、其余不动，且是原子的。

**练习 2**：EDMA 的 `count_reg`（[edma_regs.v:160-164](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/edma/hdl/edma_regs.v#L160-L164)）除了 `count_write` 还有一个 `else if (update)` 分支，这说明写选通和「硬件自更新」如何共存？

**参考答案**：一个寄存器可以同时被「软件写」和「硬件更新」驱动，写在一个 `always` 块里用 `if/else if` 排好优先级即可。这里 `count_write`（软件写 count 寄存器）优先级高于 `update`（DMA 引擎每搬一笔后回写剩余计数）。这是 regmap 模式的常见扩展：寄存器不只是软件单向写，硬件也会回写状态。

## 5. 综合实践

把三个最小模块串起来，完成 spec 要求的核心任务：**列出 GPIO 各寄存器的地址位与对应的 `*_write` 信号**。

**任务**：对照 [gpio/hdl/gpio_regmap.vh](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/hdl/gpio_regmap.vh)、[gpio/hdl/gpio.v:96-109](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/hdl/gpio.v#L96-L109) 和 [gpio/README.md:13-25](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/README.md#L13-L25)，自己画出下面这张完整的「寄存器三对照表」，并回答 3 个问题。

**步骤**：

1. 填表（地址切片统一为 `dstaddr[6:3]`）：

| 寄存器宏 | 编号 (`addr[6:3]`) | 读写属性 | 对应写选通信号 | 作用的寄存器变量 |
|----------|-------------------|----------|----------------|------------------|
| `` `GPIO_DIR ``    | 4'h0 | WR | `dir_write`     | `gpio_dir`  |
| `` `GPIO_IN ``     | 4'h1 | RD | （无，只读）     | `gpio_in_sync`（回读） |
| `` `GPIO_OUT ``    | 4'h2 | WR | `outreg_write` | `gpio_out` |
| `` `GPIO_OUTCLR `` | 4'h3 | WR | `outclr_write` | `gpio_out`（清） |
| `` `GPIO_OUTSET `` | 4'h4 | WR | `outset_write` | `gpio_out`（置） |
| `` `GPIO_OUTXOR `` | 4'h5 | WR | `outxor_write` | `gpio_out`（翻） |
| `` `GPIO_IMASK ``  | 4'h6 | WR | `imask_write`  | `gpio_imask` |
| `` `GPIO_ITYPE ``  | 4'h7 | WR | `itype_write`  | `gpio_itype` |
| `` `GPIO_IPOL ``   | 4'h8 | WR | `ipol_write`   | `gpio_ipol` |
| `` `GPIO_ILAT ``   | 4'h9 | RD | （无，只读）     | `gpio_ilat`（回读） |
| `` `GPIO_ILATCLR ``| 4'hA | WR | `ilatclr_write`| `gpio_ilat`（清中断） |

2. 回答：
   - 哪些寄存器**没有**写选通？为什么？（答：`GPIO_IN`、`GPIO_ILAT`，因为它们只读——输入值和中断锁存状态由硬件驱动，软件不能直接写。）
   - 哪些写选通**最终作用于同一个寄存器变量**？（答：`outreg/outclr/outset/outxor` 四个都作用于 `gpio_out`；`ilatclr_write` 作用于 `gpio_ilat` 的清除。）
   - 若要给 GPIO 新增一个「只写」的「输出使能」寄存器，编号用 `4'hB`，你需要在哪几个地方改动？（答：① `gpio_regmap.vh` 加 `` `GPIO_OEN 4'hB ``；② `gpio.v` 声明 `oen_write` 线、加 `assign oen_write = reg_write & (dstaddr_in[6:3]==`GPIO_OEN);`；③ 写一个 `always` 块更新 `gpio_oen`；④ 若需回读，在 `case` 里加一项并更新 README 地址表。）

**预期结果**：你能独立产出上表，并解释每一列的来源（编号来自 `.vh`，写选通来自 `gpio.v` 译码段，属性来自 README）。这就是「读懂一个外设 regmap」的全部功夫。

> 待本地验证：若想在仿真中亲眼看到某个 `*_write` 拉高一拍，需用 u4 讲的 `build.sh + sim.sh` 跑 `gpio/dv` 下的 `.emf` 测试并用 gtkwave 看 `gpio.v` 内部信号。

## 6. 本讲小结

- OH! 的每个可配置 IP 都配一个 `xxx_regmap.vh`，用大写宏常量给每个寄存器分配地址编号，是寄存器地址的「单一事实源」，全项目共 7 个这样的文件。
- `.vh` 用 `` `ifndef/`define/`endif `` 做文件级 include 守卫；当多个 `.vh` 定义同一常量时（如 `E_MAILBOXLO`），还会套一层符号级守卫防重定义。
- 地址译码的标准模式是 `assign xxx_write = reg_write & (dstaddr_in[切片]==`宏)`，切片位段由 regmap 注释（如 `addr[6:3]`）和宏位宽共同决定；GPIO 切 4 位（8 字节步长），EDMA 切 5 位（4 字节步长）。
- 高位地址（基址）由 emesh 系统级路由负责「选 IP」，IP 内部只译码低位的「寄存器号」——这是系统路由与 IP regmap 的清晰分层。
- 写选通是单拍 one-hot 脉冲，把「地址判断」与「寄存器更新」解耦；GPIO_OUT 用 4 个别名选通 + mux 实现「直接写/清/置/翻」的原子位操作。
- 读写共用同一张地址表与同一个地址切片：写走 `*_write` 驱动 `always`，读走 `case` 选 `read_data` 再交 `emesh_readback` 回送。文档与代码不一致时（如 EDMA 的「64 位」注释）一律以 RTL 为准。

## 7. 下一步学习建议

- **下一讲 u6-l2（GPIO 模块全解析）**：本讲只讲了 GPIO 的「寄存器怎么选」，下一讲会把方向控制、输入同步与边沿检测、中断逻辑（ITYPE/IPOL/IMASK/ILAT）的完整数据流讲透，正好用上本讲的 regmap 与写选通。
- **u6-l3（SPI 主从）、u6-l4（emailbox/emmu/etrace）**：它们都遵循本讲的「emesh 接口 + regmap」范式，阅读时先找各自的 `*_regmap.vh` 和 `*_regs`/顶层里的译码段，就能快速建立地图。
- **延伸阅读**：想看更复杂的分层地址映射，读 [elink/hdl/elink_regmap.vh](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/elink_regmap.vh)，它把 `[31:0]` 切成 LINKID/GROUP/寄存器组/寄存器号多层，是 regmap 模式在系统级 IP 上的进阶用法，对应第 7 单元 elink。
