# 多端口存储器与初始化

## 1. 本讲目标

本讲承接 u7-l1（单端口与双端口 RAM 推断）。u7-l1 的三个模块最多只能提供 **两个端口**——单端口 1RW、Simple 双端口 1W1R、True 双端口 2WR。可真实的片上存储需求往往不止两个口：CPU 的寄存器堆要多个读口同时取操作数、信号量存储要一拍整体清零、并行功能单元要同时读写多块小存储。这些场景超出了底层 BRAM「每块固定两三个口」的物理限制。

读完本讲，你应当能够：

- 说清楚**多端口 LE RAM**（`RAM_Multiported_LE`）是如何用「逻辑单元（LUT + 触发器）」而非 BRAM 实现任意数量的读写口的，以及它为什么**能在一拍内整体清零**。
- 读懂它的**端口拼接**约定：Verilog 模块的端口数是固定的，多个口被拼成一根宽向量，按口编号切分。
- 描述**写冲突**是怎么被检测出来的（数命中数），并讲清楚四种处理策略（PRIORITY / ROUNDROBIN / DISCARD / 布尔归约）各自的语义与电路代价。
- 理解 **1WnR 复制法**（`RAM_1WnR_Replicated`）用「把存储复制 n 份、写入广播到所有副本」这一最朴素办法换出多个独立读口，并算清它的面积代价。
- 掌握**内存初始化文件**的最小格式（每行一个十六进制字），会用 `$readmemh` 加载，并用 `RAM_generate_empty_init_file.py` 生成一个空白初始化文件。

## 2. 前置知识

本讲站在 u7-l1 的肩膀上。请先确认你已掌握：

1. **推断（inference）一块 RAM** 的含义：用 `reg [WORD_WIDTH-1:0] ram [DEPTH-1:0]` 这种「寄存器数组 + 时钟块」的行为级描述，让综合器自己识别成存储器；本书一律用推断、不实例化厂商原语（见 [RAM_Simple_Dual_Port.v:89-L92](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/RAM_Simple_Dual_Port.v#L89-L92) 的 `ram` 数组）。
2. **三种片上存储资源**：触发器（最贵、1 位/个）、分布式/LUT RAM（小而快）、块 RAM / BRAM（大容量专用块）。本讲的 LE RAM 本质上是「触发器堆 + 查找逻辑」，属于最贵但最灵活的那一类。
3. **「最后赋值胜出」复位惯用法**（u3-l2）：时钟块里先正常写、再用 `if (clear)` 覆盖，只把需要复位的寄存器卷进复位树。本讲 LE RAM 的存储体正是这么写的。

再补充两条本讲要用的常识：

- **端口数是硬约束**。Verilog-2001 模块的端口个数在编译时就定死了，不能「运行时多开一个读口」。所以「任意端口数」只能靠参数化深度 + `generate` 循环来实现，多个逻辑口在物理上被拼成一根宽向量。
- **BRAM 的口数是固定的**。一块 BRAM 通常只能提供 1～2 个口（True Dual-Port 最多两个对等口）。想要 4 读 2 写这种「口比 BRAM 多」的存储，要么用多块 BRAM 拼（复制法、LVT、XOR 等技巧），要么干脆放弃 BRAM、用逻辑单元搭（本讲的 LE RAM）。

> 术语提示：下文反复出现的 **1W1R / 1WnR / nWnR** 是描述存储器端口形态的简写——「1 个写口、n 个读口」等。BRAM 的口是「读/写共用」的，而本书的 RAM 模块常把读写拆成独立口。

## 3. 本讲源码地图

| 文件 | 作用 | 关键看点 |
| --- | --- | --- |
| `RAM_Multiported_LE.v` | 用逻辑单元实现的多端口 RAM，带写冲突处理 | 端口拼接；`generate` 逐存储单元译码写地址；四种冲突策略；读口流水线 |
| `RAM_1WnR_Replicated.v` | 用「存储复制」实现的 1WnR 多端口 RAM | 内部零逻辑——只是把 `RAM_Simple_Dual_Port` 复制 n 份、写口绑在一起 |
| `RAM_generate_empty_init_file.py` | 生成空白内存初始化文件的小工具 | 计算每行十六进制位数、按深度逐行写出 |
| `RAM_Simple_Dual_Port.v` | 1W1R 基座（u7-l1 已讲） | `RAM_1WnR_Replicated` 复制的就是它 |
| `RAM_Multiported_LE.v` 末段 | 初始化逻辑（`USE_INIT_FILE` / `$readmemh`） | 与 u7-l1 一致的两种初始化路径 |

这两个多端口模块在 `index.html` 里都属于 **Memory** 分类（[index.html:L166-L178](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/index.html#L166-L178)），紧挨在 u7-l1 讲过的三个 RAM 之后：

- [index.html:L173](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/index.html#L173)：Multi-Ported Memory using Logic Elements, with Conflict Handling（即 `RAM_Multiported_LE`）
- [index.html:L174](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/index.html#L174)：Multi-Ported Memory using Replication (1WnR)（即 `RAM_1WnR_Replicated`）

同分类下还有 LVT、XOR、I-LVT 三种「更省面积的多端口存储」规划项（[index.html:L175-L177](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/index.html#L175-L177)，尚无超链接＝尚未实现）——它们是比「复制法」更高级的技巧，留作拓展阅读。

## 4. 核心概念与源码讲解

本讲把多端口存储拆成四个最小模块：**(4.1) LE RAM 的存储结构与端口拼接**、**(4.2) 写冲突检测与四种处理策略**、**(4.3) 1WnR 复制法**、**(4.4) 内存初始化文件与 `$readmemh`**。前两个模块都在 `RAM_Multiported_LE.v` 里，是同一份源码的两条主线。

### 4.1 多端口 LE RAM：用逻辑单元实现任意多端口

#### 4.1.1 概念说明

`RAM_Multiported_LE` 的核心思想可以用一句话概括：**既然一块 BRAM 给不了那么多口，那就干脆不用 BRAM，用触发器存、用查找逻辑选**。

作者在文件开头就把定位说得很清楚：这种多端口存储**不指望映射到底层 RAM 块**，而是用「随机逻辑 + 寄存器」实现，从而可以支持**任意数量的读口和写口**，并且**能在一拍内整体清零**（bulk clear）。代价是不适合做大——深度和宽度一大，触发器和查找表的用量就爆炸。它适合「小而并发」的存储：信号量、小型 CPU 寄存器堆、并行功能单元的局部存储，也是「更高效的大容量多端口存储」的构建块（见 [RAM_Multiported_LE.v:L7-L15](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/RAM_Multiported_LE.v#L7-L15)）。

关键设计取舍：

- **读与写并发**：同一地址同时被读和（单个、不冲突的）写时，**返回当前存储的旧值**，被写的新值要到下一拍才可读（[RAM_Multiported_LE.v:L19-L21](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/RAM_Multiported_LE.v#L19-L21)）。注意：它**不支持写转发**（write-forwarding），这点和 u7-l1 的 `RAM_Simple_Dual_Port` 不同（[RAM_Multiported_LE.v:L42-L45](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/RAM_Multiported_LE.v#L42-L45)）。
- **越界安全**：地址超过 `DEPTH` 时，读返回 0、写无效，且越界写**不会**引发写冲突（[RAM_Multiported_LE.v:L47-L50](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/RAM_Multiported_LE.v#L47-L50)）。
- **整体清零**：因为存储体就是普通触发器，一个 `clear` 就能在同一拍把所有单元复位回 `INIT_VALUE`——这是 BRAM 做不到的「特权」。

#### 4.1.2 核心流程

整个模块的骨架是一个 **`generate` 外层循环，逐个存储单元（per ram unit）处理**。对深度为 `DEPTH` 的存储，循环 `DEPTH` 次，每次负责「第 i 个存储单元」的写译码、冲突检测、存储与读出扁平化。读口则在另一个 `generate` 循环里，每个读口一个深度为 `DEPTH` 的多路选择器。

伪代码如下：

```
参数: WORD_WIDTH, READ_PORT_COUNT, WRITE_PORT_COUNT, ADDR_WIDTH, DEPTH, ...

端口(全部是拼接宽向量):
    write_data   [WORD_WIDTH * WRITE_PORT_COUNT 位]
    write_address[ADDR_WIDTH  * WRITE_PORT_COUNT 位]
    write_enable [WRITE_PORT_COUNT 位]
    read_data    [WORD_WIDTH * READ_PORT_COUNT  位]   // 输出
    read_address [ADDR_WIDTH  * READ_PORT_COUNT  位]
    read_enable  [READ_PORT_COUNT 位]

存储体: reg [WORD_WIDTH-1:0] ram [DEPTH-1:0];   // DEPTH 个触发器字

for i in 0..DEPTH-1:                              // 逐存储单元
    // (1) 把每个写口的地址译码成 "是否命中第 i 单元"
    for j in 0..WRITE_PORT_COUNT-1:
        write_addr_hit[j] = (write_address[口j] == i) && write_enable[j]
    // (2) 数命中数 → 是否冲突 (留给 4.2)
    // (3) 按冲突策略选出写入数据与写使能 (留给 4.2)
    // (4) 存储 (last-assignment-wins 复位)
    always @(posedge clock):
        if (write_enable_ram) ram[i] <= write_data_selected
        if (clear)            ram[i] <= INIT_VALUE
    // (5) 把 ram[i] 拍平进 stored_data 大向量, 供读口选择
    stored_data[i*WORD_WIDTH +: WORD_WIDTH] = ram[i]

for k in 0..READ_PORT_COUNT-1:                    // 逐读口
    // 可选读流水线 (捕获地址 + 整个 stored_data)
    // 用 read_address 从 stored_data 里选出一个字
    read_data[口k] = MUX(stored_data, read_address[口k])
```

这里有两处「拼接」是理解端口的关键：

- **写侧**：`WRITE_PORT_COUNT` 个写口的数据、地址、使能分别首尾相接，拼成一根宽向量。
- **读侧**：同理，`READ_PORT_COUNT` 个读口的地址拼成一根宽向量，读出的数据也拼成一根宽向量。

派生参数直接给出每根向量的位宽（[RAM_Multiported_LE.v:L89-L92](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/RAM_Multiported_LE.v#L89-L92)）：

\[
\text{TOTAL\_WRITE\_DATA} = \text{WORD\_WIDTH} \times \text{WRITE\_PORT\_COUNT}
\]

\[
\text{TOTAL\_READ\_DATA} = \text{WORD\_WIDTH} \times \text{READ\_PORT\_COUNT}
\]

「切某一口的位段」用变址部分位选 `base +: width`：例如第 j 个写口的数据是 `write_data[WORD_WIDTH*j +: WORD_WIDTH]`（[RAM_Multiported_LE.v:L167](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/RAM_Multiported_LE.v#L167)），第 k 个读口地址是 `read_address[ADDR_WIDTH*k +: ADDR_WIDTH]`（[RAM_Multiported_LE.v:L494](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/RAM_Multiported_LE.v#L494)）。这套 `+:` 惯用法在 u5-l2 的二进制 mux 里已经见过。

#### 4.1.3 源码精读

**存储体**——一个普通的 `reg` 数组，附带 `ramstyle`/`ram_style` 属性（Quartus/Vivado 各一份），但正如注释所言，除非器件有特殊 RAM 块，它通常会被映射成通用逻辑寄存器（[RAM_Multiported_LE.v:L122-L130](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/RAM_Multiported_LE.v#L122-L130)）：

```verilog
(* ramstyle  = RAMSTYLE *) // Quartus
(* ram_style = RAMSTYLE *) // Vivado
reg [WORD_WIDTH-1:0] ram [DEPTH-1:0];
```

**逐单元的写地址译码**——外层 `for (i...)` 遍历每个存储单元，内层 `for (j...)` 遍历每个写口，用 `Address_Decoder_Behavioural` 判断「第 j 个写口是否要写第 i 个单元」。注意它把单元号 `i` 截成 `ADDR_WIDTH` 位再当 `base_addr`/`bound_addr`，纯粹是为了消掉位宽告警（[RAM_Multiported_LE.v:L152-L170](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/RAM_Multiported_LE.v#L152-L170)）：

```verilog
for (i=0; i < DEPTH; i=i+1) begin: per_ram_unit
    wire [WRITE_PORT_COUNT-1:0] write_addr_hit;
    for (j=0; j < WRITE_PORT_COUNT; j=j+1) begin: per_write_port
        Address_Decoder_Behavioural #(.ADDR_WIDTH(ADDR_WIDTH)) write_address_decoder (
            .base_addr  (i [ADDR_WIDTH-1:0]),
            .bound_addr (i [ADDR_WIDTH-1:0]),
            .addr       (write_address [ADDR_WIDTH*j +: ADDR_WIDTH]),
            .hit        (write_addr_hit [j])
        );
    end
```

**屏蔽未使能的写口**——把 `write_addr_hit` 与 `write_enable` 按位与，使「地址命中但写口没开」不算数，也就不会无端引发冲突（[RAM_Multiported_LE.v:L175-L179](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/RAM_Multiported_LE.v#L175-L179)）：

```verilog
reg [WRITE_PORT_COUNT-1:0] write_addr_hit_enabled = WRITE_ADDR_HIT_ZERO;
always @(*) begin
    write_addr_hit_enabled = write_addr_hit & write_enable;
end
```

**存储（last-assignment-wins）**——作者特意说明：这里**没有**用 `Register` 模块，而是把寄存器逻辑直接抄进来，因为只有 `reg` 数组才能在后面用 `$readmemh` 从文件初始化。复位用「最后赋值胜出」惯用法（[RAM_Multiported_LE.v:L387-L402](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/RAM_Multiported_LE.v#L387-L402)）：

```verilog
always @(posedge clock) begin
    if (write_enable_ram == 1'b1) begin
        ram [i] <= write_data_selected;
    end
    if (clear == 1'b1) begin
        ram [i] <= INIT_VALUE;   // 整体清零：所有单元同一拍复位
    end
end
```

**拍平存储**——把 `DEPTH` 个 `ram[i]` 拼成一根超宽的 `stored_data` 向量，这样每个读口只要一个「深度为 DEPTH 的多路选择器」就能选字，比「为每个读口写嵌套循环」更模块化（[RAM_Multiported_LE.v:L404-L413](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/RAM_Multiported_LE.v#L404-L413)）：

```verilog
always @(*) begin
    stored_data [WORD_WIDTH*i +: WORD_WIDTH] = ram [i];
end
```

**读口**——每个读口先用一个 `Multiplexer_Binary_Structural`（深度 `DEPTH` 个输入）从 `stored_data_pipelined` 里选出本口要的字（[RAM_Multiported_LE.v:L561-L574](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/RAM_Multiported_LE.v#L561-L574)）。这正是「不适合大深度」的根源：深度 32 就要一个 32 输入的大 mux，深度 1024 几乎不可行。

```verilog
Multiplexer_Binary_Structural #(
    .WORD_WIDTH(WORD_WIDTH), .ADDR_WIDTH(ADDR_WIDTH), .INPUT_COUNT(DEPTH), ...
) read_data_selector (
    .selector (read_port_address_pipelined),
    .words_in (stored_data_pipelined),
    .word_out (read_data [WORD_WIDTH*k +: WORD_WIDTH])
);
```

读流水线（`READ_PIPELINE_DEPTH`）会先寄存「本口地址 + **整个** stored_data」再选，目的是把选字逻辑留给重定时（retiming）去优化时钟频率，代价是读延迟变长、且每级流水线要复制一份超宽的 `stored_data`（[RAM_Multiported_LE.v:L478-L556](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/RAM_Multiported_LE.v#L478-L556)）。

#### 4.1.4 代码实践

**实践目标**：亲手算清端口拼接的位宽与位段布局，确认你能从宽向量里切出「第 j 个写口的数据」。

**操作步骤**：

1. 假设要实例化一个 `RAM_Multiported_LE`：`WORD_WIDTH=8`、`WRITE_PORT_COUNT=2`、`READ_PORT_COUNT=3`、`ADDR_WIDTH=4`、`DEPTH=16`。
2. 用上面的两个公式，算出 `write_data`、`write_address`、`write_enable`、`read_data`、`read_address`、`read_enable` 这 6 个端口的向量位宽。
3. 画出 `write_data`（共 16 位）的位段图，标出第 0 个写口的 8 位落在 `[7:0]`、第 1 个写口落在 `[15:8]`。
4. 写出取「第 1 个写口数据」的表达式（答案应为 `write_data[8 +: 8]`，即 `[15:8]`）。

**需要观察的现象**：端口位宽随口数线性增长；切位段时下标 = `WORD_WIDTH * 口号`。

**预期结果**：

| 端口 | 位宽 |
| --- | --- |
| `write_data` | 16 |
| `write_address` | 8 |
| `write_enable` | 2 |
| `read_data` | 24 |
| `read_address` | 12 |
| `read_enable` | 3 |

> 待本地验证：上表是按公式手算的结果；若你把它写进一个测试台并 `%d` 打印各 `$bits(...)`，应与上表完全一致。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `RAM_Multiported_LE` 不支持写转发（write-forwarding），而 u7-l1 的 `RAM_Simple_Dual_Port` 支持？

**参考答案**：写转发依赖「同一时钟沿先写后读」的阻塞赋值技巧，且通常由 BRAM 的专用旁路逻辑实现。LE RAM 的存储体是触发器 + 组合选择，读口是从 `stored_data`（即 `ram` 的组合拍平）里现选的，写要在时钟沿后才落进 `ram`，所以同址读写只能拿到旧值、新值下一拍才可读（[RAM_Multiported_LE.v:L19-L21](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/RAM_Multiported_LE.v#L19-L21)）。

**练习 2**：模块顶部的注释说这种存储「能在一拍内整体清零」。请从源码指出实现这一点的具体语句，并解释为什么 BRAM 做不到。

**参考答案**：见 [RAM_Multiported_LE.v:L399-L401](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/RAM_Multiported_LE.v#L399-L401)——每个存储单元的时钟块里都有一句 `if (clear == 1'b1) ram[i] <= INIT_VALUE;`，由于所有单元共用同一个 `clear`，一拍即可全部复位。BRAM 的内容住在专用 SRAM 阵列里、没有逐位的同步复位端，无法一拍清空全部内容。

---

### 4.2 写冲突检测与四种处理策略

#### 4.2.1 概念说明

「写冲突」是「两个写口在同一拍、向**同一个地址**写数据」这件事。单端口 RAM 不会有冲突（只有一个口），True Dual-Port 也只在「两口同址都写」时才有；但 LE RAM 可以有任意多个写口，冲突几乎不可避免，必须定义清楚「撞上了怎么办」。

`RAM_Multiported_LE` 用参数 `ON_WRITE_CONFLICT` 选择策略（[RAM_Multiported_LE.v:L17-L38](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/RAM_Multiported_LE.v#L17-L38)）。无论哪种策略，只要某写口卷入冲突，它的 `write_conflict` 位就会在**写后的下一拍**拉高一个周期，告诉外部「你这次写没完全生效」。

五种（实为四大类）策略：

- **PRIORITY**：编号最小的冲突写口胜出，其数据被写入；其余冲突口什么都不做，`write_conflict` 拉高。
- **ROUNDROBIN**：同样是「一个口胜出」，但胜出口由轮询仲裁器决定，避免低编号口长期霸占。
- **DISCARD**：一旦冲突，**所有**冲突写全部丢弃，没有任何数据写入；所有冲突口 `write_conflict` 拉高。
- **AND / OR / XOR / NAND / NOR / XNOR**：把所有冲突写口的数据按指定位运算**归约成一个字**再写入；所有冲突口 `write_conflict` 拉高（提示「你的数据被合并了」）。

#### 4.2.2 核心流程

冲突处理分三步走，全部在「逐存储单元」的 `generate` 循环里完成：

```
对第 i 个存储单元:
  步骤1: 算 write_addr_hit_enabled (4.1 已做)  // 每个写口是否命中且使能
  步骤2: 数命中个数 count = popcount(write_addr_hit_enabled)
         冲突 = (count != 0) && (count != 1)   // 即 count >= 2
  步骤3: 按 ON_WRITE_CONFLICT 选策略:
         PRIORITY:  胜出口 = isolate_rightmost_1(hit_enabled)  // 最低位
                    write_enable_ram = (count != 0)
                    write_data = mux_one_hot(胜出口掩码)
         ROUNDROBIN:胜出口 = arbiter_round_robin(hit_enabled)
                    (其余同 PRIORITY)
         DISCARD:   write_enable_ram = (count == 1)            // 只有恰好 1 个才写
                    write_data = mux_one_hot(hit_enabled)      // 数据反正用不到
         布尔归约:   write_enable_ram = (count != 0)
                    write_data = reducer(hit_enabled, op)      // AND/OR/XOR...
  步骤4: 把"哪些口最终卷入了冲突"汇总进 write_conflict_all, 最后归约成每口 1 位
```

「数命中个数」用 `Population_Count`（u16-l1 会细讲，这里只需知道它是「数向量里 1 的个数」的电路），见 [RAM_Multiported_LE.v:L186-L196](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/RAM_Multiported_LE.v#L186-L196)。冲突判定很简单——命中数既不是 0 也不是 1，就是冲突（[RAM_Multiported_LE.v:L200-L202](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/RAM_Multiported_LE.v#L200-L202)）：

\[
\text{冲突} = (\text{count} \neq 0) \wedge (\text{count} \neq 1) \quad\Longleftrightarrow\quad \text{count} \geq 2
\]

> 这里作者特意用两个等式比较而非算术 `>=`，呼应 u3-l1 的「布尔式写成等式比较」风格，也便于综合器优化。

#### 4.2.3 源码精读

**检测命中数**——实例化 `Population_Count`，输入是屏蔽后的命中向量，输出是命中个数（[RAM_Multiported_LE.v:L186-L202](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/RAM_Multiported_LE.v#L186-L202)）：

```verilog
Population_Count #(.WORD_WIDTH(WRITE_PORT_COUNT)) detect_multiple_writes (
    .word_in  (write_addr_hit_enabled),
    .count_out(write_addr_hit_count)
);
reg write_conflict_raw = 1'b0;
always @(*) begin
    write_conflict_raw = (write_addr_hit_count != WRITE_ADDR_HIT_ONE)
                      && (write_addr_hit_count != WRITE_ADDR_HIT_ZERO);
end
```

**PRIORITY 策略**——用 `Bitmask_Isolate_Rightmost_1_Bit`（即 `x & (-x)` 技巧）把命中向量里最低位的 1 单独挑出来当胜出口，再用 `Multiplexer_One_Hot` 选出该口数据（[RAM_Multiported_LE.v:L240-L272](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/RAM_Multiported_LE.v#L240-L272)）：

```verilog
Bitmask_Isolate_Rightmost_1_Bit #(.WORD_WIDTH(WRITE_PORT_COUNT)) write_data_priority (
    .word_in (write_addr_hit_enabled),
    .word_out(write_addr_hit_masked_priority)
);
always @(*) begin
    write_conflict_ports_masked = write_conflict_ports & ~write_addr_hit_masked_priority;
    write_enable_ram            = write_addr_hit_enabled != WRITE_ADDR_HIT_ZERO;
end
```

**ROUNDROBIN 策略**——结构几乎相同，只是把「挑最低位」换成 `Arbiter_Round_Robin` 仲裁器（[RAM_Multiported_LE.v:L282-L321](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/RAM_Multiported_LE.v#L282-L321)）：

```verilog
Arbiter_Round_Robin #(.INPUT_COUNT(WRITE_PORT_COUNT)) write_data_round_robin (
    .clock(clock), .clear(clear),
    .requests      (write_addr_hit_enabled),
    .requests_mask (WRITE_ROUNDROBIN_NOMASK),
    .grant_previous(),
    .grant         (write_addr_hit_masked_roundrobin)
);
```

**DISCARD 策略**——只有命中数**恰好为 1** 时才写；冲突时（命中数 ≥ 2）一律不写（[RAM_Multiported_LE.v:L329-L350](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/RAM_Multiported_LE.v#L329-L350)）：

```verilog
always @(*) begin
    write_conflict_ports_masked = write_conflict_ports;
    write_enable_ram            = write_addr_hit_count == WRITE_ADDR_HIT_ONE; // 恰好 1 个才写
end
```

**布尔归约策略（默认分支）**——`else` 分支处理 AND/OR/XOR/... 六种，把 `ON_WRITE_CONFLICT` 字符串直接当 `Multiplexer_One_Hot` 的 `OPERATION` 传进去，对所有命中口的数据做归约（[RAM_Multiported_LE.v:L357-L376](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/RAM_Multiported_LE.v#L357-L376)）：

```verilog
Multiplexer_One_Hot #(
    .WORD_WIDTH(WORD_WIDTH), .WORD_COUNT(WRITE_PORT_COUNT),
    .OPERATION(ON_WRITE_CONFLICT), .IMPLEMENTATION("AND")
) write_data_mux_boolean (
    .selectors (write_addr_hit_enabled),
    .words_in  (write_data),
    .word_out  (write_data_selected)
);
```

> 注意 `Multiplexer_One_Hot` 在 u5-l2 里讲过：它先按选择位清零/放行各路数据，再用 `Word_Reducer` 归约。这里「复用 mux 当布尔归约器」正是 u4-l1「构建块库」思想的体现。

**汇总冲突报告**——每个存储单元各自算出「哪些口卷入冲突」（一位/口，重复 `DEPTH` 份存进 `write_conflict_all`），最后用 `Word_Reducer` 按位 OR 折叠成「每口 1 位」，再过一级 `Register` 延迟一拍输出（[RAM_Multiported_LE.v:L419-L451](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/RAM_Multiported_LE.v#L419-L451)）。延迟一拍是为了和「写入在时钟沿生效、下一拍才能判断」对齐。

#### 4.2.4 代码实践

**实践目标**：在纸上跑一遍写冲突，看清四种策略下「存储体最终存了什么、哪个口的 `write_conflict` 会亮」。

**操作步骤**：

1. 设定一个 2 写口、`WORD_WIDTH=4`、深度为 1（只看地址 0）的 LE RAM，`ram[0]` 初值为 `0000`。
2. 某一拍：写口 0 要写 `0011`、写口 1 要写 `0101`，**两口地址都指向 0**、都使能。
3. 分别在 `ON_WRITE_CONFLICT` 取 **PRIORITY**、**DISCARD**、**AND**、**OR** 时，写出该拍结束后 `ram[0]` 的新值，以及两口各自的 `write_conflict`（下一拍）。
4. 再把写口 1 的使能关掉（仅写口 0 写），重复一遍，确认「无冲突」时 `write_conflict` 全 0。

**需要观察的现象**：DISCARD 时即便有有效数据也不写入；PRIORITY 恒定偏向低编号口；AND/OR 把两个数据合并。

**预期结果**（两口同址、都使能）：

| 策略 | `ram[0]` 新值 | 口0 `write_conflict` | 口1 `write_conflict` |
| --- | --- | --- | --- |
| PRIORITY | `0011`（口0 胜） | 1 | 1 |
| DISCARD | `0000`（不变） | 1 | 1 |
| AND | `0011 & 0101 = 0001` | 1 | 1 |
| OR | `0011 \| 0101 = 0111` | 1 | 1 |

仅写口 0 写时：四种策略下 `ram[0]` 都变成 `0011`、两口 `write_conflict` 全为 0。

> 待本地验证：上表为按源码语义手算的结果；建议在仿真台里实例化 `RAM_Multiported_LE` 并按上表施加激励对照。

#### 4.2.5 小练习与答案

**练习 1**：为什么 DISCARD 分支里仍然实例化了一个 `Multiplexer_One_Hot` 选数据，注释却说「Data never used under conflict」？

**参考答案**：因为 `write_enable_ram` 在冲突时（命中数 ≥ 2）为假，存储体根本不会写入，所以选出来的 `write_data_selected` 在冲突时是「算出来但被丢弃」的。保留 mux 只是为了让无冲突（命中数恰为 1）时能正常选出唯一数据。见 [RAM_Multiported_LE.v:L338-L350](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/RAM_Multiported_LE.v#L338-L350)。

**练习 2**：`write_conflict` 为什么是「写后下一拍」才拉高，而不是当拍？

**参考答案**：写是否生效、是否与别人冲突，要等时钟沿把（屏蔽后的）命中信息汇总后才能判定。模块把汇总结果过一级 `Register`（[RAM_Multiported_LE.v:L439-L451](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/RAM_Multiported_LE.v#L439-L451)），使 `write_conflict` 与「数据真正落入 `ram`」发生在同一时序节奏上——也就是写操作生效后的下一拍。

**练习 3**：若你的应用「同一拍绝不可能有两个写口写同址」，该选哪种策略最省逻辑？

**参考答案**：选 **PRIORITY** 或任一布尔归约。既然冲突永不发生，冲突处理分支永远不会被真正触发，综合器会把死分支优化掉；但 PRIORITY 的 `Bitmask_Isolate_Rightmost_1_Bit` 通常比 `Arbiter_Round_Robin`（含状态）更省。务必确认你的「绝不冲突」前提真的成立，否则 DISCARD 会静默丢数据。

---

### 4.3 1WnR 复制法：用存储复制换多个读端口

#### 4.3.1 概念说明

`RAM_Multiported_LE` 用逻辑单元搭，灵活但贵。另一种很常见的需求是 **1WnR**：1 个写口、多个读口——典型如「多个执行单元同时读同一份配置表」。这种场景有一个极其朴素的解法：**把整份存储复制 n 份，每个读口独享一份副本，写入时广播到所有副本**。这就是 `RAM_1WnR_Replicated`。

作者在文件开头就点明它的取舍：**没有逻辑**——它只是「若干份 1W1R 存储的副本、写口绑在一起」。它不是最省面积的，但当容量放得下时，它**最简单、最快、最灵活**（[RAM_1WnR_Replicated.v:L83-L86](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/RAM_1WnR_Replicated.v#L83-L86)）。

为什么「复制」能给出多个独立读口？因为每个副本都是一个完整的 1W1R 双端口 RAM（`RAM_Simple_Dual_Port`），自带一个独立的读口。n 个副本就有 n 个相互独立的读口，互不干扰；而它们的写口接同一组 `write_data/write_address/write_enable`，所以任何写入会同时落到所有副本，保证 n 份内容始终一致。

#### 4.3.2 核心流程

```
参数: WORD_WIDTH, READ_PORT_COUNT=n, ADDR_WIDTH, DEPTH, ...
      (写侧只有 1 个口, 所以是标量 write_data/write_address/write_enable)

for i in 0..n-1:                              // 每个读口一份副本
    实例化 RAM_Simple_Dual_Port (1W1R):
        .clock      <- clock
        .wren       <- write_enable           // 所有副本共用同一个写使能
        .write_addr <- write_address          //   同一个写地址
        .write_data <- write_data             //   同一份写数据  => 广播写入
        .rden       <- read_enable[i]         // 各自独立的读使能
        .read_addr  <- read_address[口i 位段]  // 各自独立的读地址
        .read_data  -> read_data[口i 位段]    // 各自独立的读出
```

写广播使所有副本内容一致；每个副本的独立读口给出一个独立读结果。代价是存储总量随读口数线性增长：

\[
\text{存储总量} = \text{READ\_PORT\_COUNT} \times \text{DEPTH} \times \text{WORD\_WIDTH} \text{ (位)}
\]

例如「8 位字、深度 256、4 个读口」的 1WnR 复制 RAM，要 4 份 256×8 的存储 = 8192 位，是单份的 4 倍。

读口的并发性也来自复制：第 0 口读地址 5、第 1 口同时读地址 200、第 2 口也读地址 5——互不阻塞，因为它们查的是各自独立的副本。这是 LE RAM 的「真多口」语义，只是用「浪费面积」换来的。

#### 4.3.3 源码精读

整个模块的实体就是一个 `generate` 循环，循环体里实例化一个 `RAM_Simple_Dual_Port`——仅此而已，没有任何组合逻辑（[RAM_1WnR_Replicated.v:L87-L117](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/RAM_1WnR_Replicated.v#L87-L117)）：

```verilog
generate
genvar i;
    for (i=0; i < READ_PORT_COUNT; i=i+1) begin: per_read_port
        RAM_Simple_Dual_Port #(
            .WORD_WIDTH(WORD_WIDTH), .ADDR_WIDTH(ADDR_WIDTH), .DEPTH(DEPTH),
            .RAMSTYLE(RAMSTYLE), .READ_NEW_DATA(READ_NEW_DATA),
            .RW_ADDR_COLLISION(RW_ADDR_COLLISION),
            .USE_INIT_FILE(USE_INIT_FILE), .INIT_FILE(INIT_FILE), .INIT_VALUE(INIT_VALUE)
        ) Replicated_Storage_Bank (
            .clock     (clock),
            .wren      (write_enable),                              // 广播写使能
            .write_addr(write_address),                             // 广播写地址
            .write_data(write_data),                                // 广播写数据
            .rden      (read_enable  [i]),                          // 独立读使能
            .read_addr (read_address [ADDR_WIDTH*i +: ADDR_WIDTH]), // 独立读地址
            .read_data (read_data    [WORD_WIDTH*i +: WORD_WIDTH])  // 独立读出
        );
    end
endgenerate
```

注意三个细节：

1. **写侧是标量**：`write_data`/`write_address`/`write_enable` 都是单口宽度（[RAM_1WnR_Replicated.v:L74-L76](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/RAM_1WnR_Replicated.v#L74-L76)），因为只有 1 个写口；它们被原样接到每个副本，实现「广播写」。
2. **读侧是拼接向量**：`read_address`/`read_data` 是按口拼接的宽向量，用 `+:` 切出第 i 口的位段——和 4.1 的端口拼接约定完全一致。
3. **每个副本透传所有 RAM 参数**：包括 `READ_NEW_DATA`、`RAMSTYLE`、初始化参数，所以每个副本都能各自被推断成 BRAM（或 LUT RAM），由 `READ_NEW_DATA` 决定写转发行为（见 u7-l1 对 `RAM_Simple_Dual_Port` 的讲解）。

这正是 u4-l1「构建块库」的范本：**不重写存储，复用已验证的 `RAM_Simple_Dual_Port`，外面包一层 `generate` 就得到新功能**。

#### 4.3.4 代码实践

**实践目标**：说清楚 `RAM_1WnR_Replicated` 如何通过「复制 + 写广播」支持多个读口，并量化面积代价。

**操作步骤**：

1. 阅读 [RAM_1WnR_Replicated.v:L87-L117](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/RAM_1WnR_Replicated.v#L87-L117)，确认循环体里**只有一个** `RAM_Simple_Dual_Port` 实例，没有任何其他逻辑。
2. 用一句话解释「写广播」：为什么所有副本的 `.wren/.write_addr/.write_data` 接的是**同一个**信号，而 `.rden/.read_addr/.read_data` 却各接各的？
3. 计算：`WORD_WIDTH=32`、`DEPTH=1024`、`READ_PORT_COUNT=3` 的 1WnR 复制 RAM 共需多少位存储？若每份能放进一块 36 Kb BRAM，需要几块？
4. 思考：若把 `READ_PORT_COUNT` 从 3 加到 6，写口的时序/资源会不会受影响？读口呢？

**需要观察的现象**：复制法把「多读口」问题转化成了「多份单读口存储」——读口之间完全解耦，但写口要驱动 n 份存储的写端口。

**预期结果**：

- 存储总量 = \(3 \times 1024 \times 32 = 98304\) 位 = 96 Kb，是单份（32 Kb）的 3 倍。
- 若每份 32 Kb 能放进一块 36 Kb BRAM，则需 3 块 BRAM（每块给一个读口）。
- `READ_PORT_COUNT` 增到 6：写口要同时驱动 6 份副本的写端口，写地址/数据的扇出（fanout）翻倍，可能拖慢写时序；读口仍是各自独立的单口，互不影响。

> 待本地验证：综合后查看资源报告里的 BRAM 使用数与写端口网络的最大扇出，应与上述判断一致。

#### 4.3.5 小练习与答案

**练习 1**：`RAM_1WnR_Replicated` 支持「两个读口在同一拍读同一地址」吗？支持「同一拍写一个地址、同时又读这个地址」吗？

**参考答案**：两种都支持。多个读口读同址毫无问题——它们查的是各自独立的副本。同址的「写 + 读」行为由副本（`RAM_Simple_Dual_Port`）的 `READ_NEW_DATA` 决定：`=0` 读到旧值、`=1` 读到新值（写转发）。这正是 1WnR 把细节下放给 1W1R 基座的好处（[RAM_1WnR_Replicated.v:L13-L32](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/RAM_1WnR_Replicated.v#L13-L32)）。

**练习 2**：既然复制法这么简单，为什么 `RAM_Multiported_LE` 还要存在？

**参考答案**：复制法只能给「多读口」，给不了「多写口」；而且每多一个读口就多复制一整份存储，读口一多面积就吃不消。LE RAM 用逻辑单元可以同时支持任意多写口和多读口，还能一拍整体清零，适合小而高并发的存储（如寄存器堆、信号量）。两者互补，按需求选（[RAM_Multiported_LE.v:L9-L15](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/RAM_Multiported_LE.v#L9-L15)）。

---

### 4.4 内存初始化文件与 `$readmemh`

#### 4.4.1 概念说明

FPGA 上电时，BRAM 和分布式 RAM 的初值由位流（bitstream）写死。本书的 RAM 模块都提供两条初始化路径（u7-l1 已在 `RAM_Single_Port` 见过其一）：

- **`USE_INIT_FILE = 0`**：用一个 `initial` + `for` 循环把所有单元写成同一个 `INIT_VALUE`。适合「整体清零」这类需求，无需维护外部文件。CAD 工具会据此生成初始化文件烧进位流。
- **`USE_INIT_FILE = 1`**：用 `$readmemh(INIT_FILE, ram)` 从一个外部十六进制文件逐行加载。适合「每个单元初值各不相同」的场景——比如 ROM 查找表、预计算的系数表、处理器上电后的初始微码。

`$readmemh` 是 Verilog 自带的系统任务：把文本文件里的十六进制数，按行依次填进 `ram` 数组的各个单元。文件格式很简单——**每行一个十六进制字，从地址 0 到 `DEPTH-1`**，用「裸十六进制」（bare hex），不要 `16'h` 前缀（见 [RAM_Multiported_LE.v:L588-L593](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/RAM_Multiported_LE.v#L588-L593) 的注释）。

例如，初始化一个 `WORD_WIDTH=16`、`DEPTH=4` 的 RAM，文件可以是：

```
0012
0034
0056
0078
```

第 0 个单元装 `16'h0012`，第 1 个装 `16'h0034`，依此类推。

每行的十六进制位数由字宽决定。一个 `WORD_WIDTH` 位的字需要多少个十六进制字符？

\[
\text{每行字符数} = \left\lceil \frac{\text{WORD\_WIDTH}}{4} \right\rceil
\]

例如 `WORD_WIDTH=16` → 4 个字符；`WORD_WIDTH=10` → 3 个字符（`10/4 = 2` 余 `2`，向上取整 = 3）。注释特别提醒：若 `WORD_WIDTH` 不是 4 的倍数，CAD 工具可能报位宽不匹配告警（[RAM_Multiported_LE.v:L588-L593](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/RAM_Multiported_LE.v#L588-L593)）。

#### 4.4.2 核心流程

两种初始化路径在源码里用 `generate if/else` 二选一（[RAM_Multiported_LE.v:L595-L609](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/RAM_Multiported_LE.v#L595-L609)）：

```
if (USE_INIT_FILE == 0):
    initial:
        for l in 0..DEPTH-1:
            ram[l] = INIT_VALUE          // 全部写成同一个值
else:
    initial:
        $readmemh(INIT_FILE, ram)        // 从文件逐行加载
```

注意两点：

1. **初始化在 `initial` 块里完成**，不是在时钟块里。`initial` 在仿真开始（以及综合时的「上电」语义）执行一次，把初值烧进位流。
2. **`for` 循环版本可能触发 lint 告警**——深度很大时循环次数多，CAD 工具会抱怨「循环次数太多」，需调大工具的循环上限（见注释 [RAM_Multiported_LE.v:L581-L587](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/RAM_Multiported_LE.v#L581-L587)）。

生成初始化文件靠 `RAM_generate_empty_init_file.py`：给它 `width`、`depth`、`filename` 三个参数，它算出每行字符数、写出 `depth` 行、每行填同一个 `fill`（默认 0）。它生成的是「空白」文件（全 0），但你可以照它的格式改写成自己想要的初值（[RAM_generate_empty_init_file.py:L1-L6](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/RAM_generate_empty_init_file.py#L1-L6)）。

#### 4.4.3 源码精读

**`RAM_Multiported_LE` 的初始化分支**（与 `RAM_Simple_Dual_Port` 完全同构，见 [RAM_Multiported_LE.v:L595-L609](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/RAM_Multiported_LE.v#L595-L609)）：

```verilog
generate
    if (USE_INIT_FILE == 0) begin
        integer l;
        initial begin
            for (l=0; l < DEPTH; l=l+1) begin: per_ram_word
                ram[l] = INIT_VALUE;
            end
        end
    end
    else begin
        initial begin
            $readmemh(INIT_FILE, ram);
        end
    end
endgenerate
```

**`RAM_generate_empty_init_file.py` 的核心**——先算每行的十六进制字符数（用整除 + 取余实现「向上取整」），再用 `"{:0Nx}".format(fill)` 把填充值格式化成定宽十六进制串（[RAM_generate_empty_init_file.py:L13-L27](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/RAM_generate_empty_init_file.py#L13-L27)）：

```python
def dump_format(width):
    """Numbers must be represented as zero-padded whole hex numbers"""
    characters = width // 4
    remainder  = width % 4
    characters += min(1, remainder)         # 余数非 0 就再补 1 个字符 => 向上取整
    format_string = "{:0" + str(characters) + "x}"
    return format_string

def file_dump(width, depth, file_name, fill=0):
    with open(file_name, 'w') as f:
        f.write(file_header + "\n")
        format_string = dump_format(width)
        for i in range(depth):
            output = format_string.format(fill)
            f.write(output + "\n")
```

文件第一行是头部注释（仿照 Modelsim `$writememh` 的输出格式，部分 CAD 软件可能要求它，见 [RAM_generate_empty_init_file.py:L9-L11](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/RAM_generate_empty_init_file.py#L9-L11)）：

```python
file_header = """// format=hex addressradix=h dataradix=h version=1.0 wordsperline=1 noaddress"""
```

命令行入口要求正好 3 个参数（`width depth filename`），否则报错退出（[RAM_generate_empty_init_file.py:L29-L36](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/RAM_generate_empty_init_file.py#L29-L36)）。

#### 4.4.4 代码实践

**实践目标**：用 `RAM_generate_empty_init_file.py` 真正生成一个空白初始化文件，看清它的格式，并理解它如何配合 `$readmemh`。

**操作步骤**：

1. 在仓库根目录执行（本机需有 `python3`）：

   ```bash
   python3 RAM_generate_empty_init_file.py 16 8 my_mem_init.hex
   ```

   含义：生成一个「字宽 16 位、深度 8」的空白初始化文件 `my_mem_init.hex`。
2. 打开生成的 `my_mem_init.hex`，观察：第 1 行是头注释，第 2～9 行各是 4 个十六进制字符 `0000`（共 8 行 = 深度 8）。
3. 再生成一个「字宽 10、深度 4」的文件，确认每行变成 **3** 个字符（`10/4` 向上取整）：

   ```bash
   python3 RAM_generate_empty_init_file.py 10 4 my_mem_init_10.hex
   ```
4. 思考如何把它接到 RAM 上：实例化时设 `USE_INIT_FILE=1`、`INIT_FILE="my_mem_init.hex"`，模块里的 `$readmemh` 会把这 8 行依次装进 `ram[0..7]`。
5. 进阶：手动把 `my_mem_init.hex` 的某几行改成非零值（如 `0003`、`0007`），仿真时上电后读对应地址应得到这些值。

**需要观察的现象**：

- 行数 == 深度；每行字符数 == `ceil(WORD_WIDTH/4)`；内容默认全 0。
- 字宽不是 4 的倍数时仍能正常生成（字符数向上取整）。

**预期结果**（`python3 RAM_generate_empty_init_file.py 16 8 my_mem_init.hex` 生成的文件）：

```
// format=hex addressradix=h dataradix=h version=1.0 wordsperline=1 noaddress
0000
0000
0000
0000
0000
0000
0000
0000
```

`python3 RAM_generate_empty_init_file.py 10 4 my_mem_init_10.hex` 每行应为 `000`（3 个字符）。

> 待本地验证：上述文件内容为按源码 `dump_format`/`file_dump` 逻辑手推的结果；请在本机运行命令并 `cat` 生成的文件对照。

#### 4.4.5 小练习与答案

**练习 1**：如果你忘了设 `USE_INIT_FILE=1`，却仍希望从文件加载初值，会发生什么？

**参考答案**：模块会走 `USE_INIT_FILE == 0` 分支，用 `for` 循环把所有单元写成 `INIT_VALUE`（默认 0），`INIT_FILE` 参数被完全忽略——你的文件根本不会被读取（[RAM_Multiported_LE.v:L596-L603](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/RAM_Multiported_LE.v#L596-L603)）。这是典型的「参数选错路径」错误，仿真里表现为「RAM 上电全 0，读不到我写进文件里的值」。

**练习 2**：`RAM_generate_empty_init_file.py` 里 `characters += min(1, remainder)` 这一句在做什么？能否换成 `characters = (width + 3) // 4`？

**参考答案**：它在做「向上取整到 4 的倍数」——`width//4` 是整数部分，若有余数（`remainder != 0`）就再补 1 个字符。`min(1, remainder)` 当余数为 0 时加 0、否则加 1，等价于向上取整。可以换成 `characters = (width + 3) // 4`，这是「向上除以 4」的经典写法，两者数学等价（见 [RAM_generate_empty_init_file.py:L13-L19](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/RAM_generate_empty_init_file.py#L13-L19)）。

**练习 3**：为什么所有 RAM 模块都「不给 `read_data` 同步清零」，而是让下游用 `Annuller` 清零？

**参考答案**：给读出寄存器加同步清零会**抑制寄存器重定时**（retiming），在 Quartus 上尤其明显，可移植性也差；而且清零是「数据通路」操作，本书倾向于把它做成独立的 `Annuller` 模块挂在下游，保持存储模块纯粹（见 `RAM_1WnR_Replicated.v` 开头 [L8-L11](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/RAM_1WnR_Replicated.v#L8-L11) 与 u7-l1 的同名讨论）。这呼应 u4-l1 的「数据/控制/接口分离」。

---

## 5. 综合实践

把本讲四个模块串起来，设计并初始化一个**小型双读口寄存器堆**（用复制法实现 1W2R）。

**任务**：

1. 选型：你需要「1 个写口、2 个读口」，写口不会和别人抢（无写冲突）。在 `RAM_Multiported_LE` 和 `RAM_1WnR_Replicated` 之间选一个，并说明理由。
   - *预期选择*：`RAM_1WnR_Replicated`。理由：只要多读口、不要多写口、无冲突，复制法最简单且每份能推断成 BRAM；LE RAM 的冲突处理与触发器存储是多余的代价。
2. 参数：`WORD_WIDTH=16`、`ADDR_WIDTH=3`、`DEPTH=8`、`READ_PORT_COUNT=2`、`RAMSTYLE` 留空（让工具自选）、`READ_NEW_DATA=0`（读旧值，贴 MLAB/分布式 RAM）。
3. 用工具生成空白初始化文件：

   ```bash
   python3 RAM_generate_empty_init_file.py 16 8 regfile_init.hex
   ```

   然后手动编辑 `regfile_init.hex`，把第 0～3 行改成 `0001`、`0002`、`0003`、`0004`，其余保持 `0000`。
4. 实例化（伪代码，仅示意端口连接，非可综合工程）：

   ```verilog
   // 示例代码：仅示意实例化端口连接
   RAM_1WnR_Replicated #(
       .WORD_WIDTH(16), .ADDR_WIDTH(3), .DEPTH(8), .READ_PORT_COUNT(2),
       .USE_INIT_FILE(1), .INIT_FILE("regfile_init.hex"), .INIT_VALUE(16'h0000),
       .READ_NEW_DATA(0)
   ) regfile (
       .clock(clock),
       .write_data(write_data), .write_address(write_address), .write_enable(we),
       .read_address({read_addr_1, read_addr_0}),   // 口1 在高位、口0 在低位
       .read_enable({rden_1, rden_0}),
       .read_data({read_data_1, read_data_0})
   );
   ```
5. 验证（在仿真台里）：
   - 上电后读地址 0 → 应得 `0001`；读地址 2 → 应得 `0003`。
   - 两个读口同时分别读地址 1 和地址 3 → 互不影响，分别得 `0002`、`0004`。
   - 写口写地址 5 = `00AB`，下一拍读地址 5 → 应得 `00AB`，且两个副本都更新。
6. 回答：复制法在这里用了几份存储？若读口加到 4 个，存储面积翻几倍？

> 待本地验证：第 5 步的预期读出值依赖你手动编辑的 `regfile_init.hex`；请在本机用 cocotb（参考 `tests/Counter_Gray_Tb.py` 的写法）或 Verilog 测试台施加激励并自检。

## 6. 本讲小结

- **`RAM_Multiported_LE`** 用「触发器 + 查找逻辑」实现**任意数量读写口**的多端口 RAM，能**一拍整体清零**，但不适合大深度大宽度；适合信号量、小型寄存器堆等「小而并发」的存储（[RAM_Multiported_LE.v:L7-L15](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/RAM_Multiported_LE.v#L7-L15)）。
- **端口拼接**是应对「Verilog 端口数固定」的唯一办法：多个口的数据/地址/使能按口编号拼成宽向量，用 `base +: width` 切位段；位宽 = `每口位宽 × 口数`。
- **写冲突**靠 `Population_Count` 数命中个数判定（命中数 ≥ 2 即冲突）；`ON_WRITE_CONFLICT` 在 **PRIORITY / ROUNDROBIN / DISCARD / 布尔归约** 四类策略里选一种，被卷入冲突的写口会在下一拍拉高 `write_conflict`。
- **`RAM_1WnR_Replicated`** 用「把存储复制 n 份、写广播到所有副本」的最朴素办法换出 n 个独立读口，内部零逻辑、只是 `generate` 出 n 个 `RAM_Simple_Dual_Port`，是「构建块库」复用的范本（[RAM_1WnR_Replicated.v:L83-L117](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/RAM_1WnR_Replicated.v#L83-L117)）。
- **内存初始化**有两条路径：`USE_INIT_FILE=0` 用 `initial`+`for` 写同一个 `INIT_VALUE`；`=1` 用 `$readmemh` 从「每行一个十六进制字」的文件加载（[RAM_Multiported_LE.v:L595-L609](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/RAM_Multiported_LE.v#L595-L609)）。
- **`RAM_generate_empty_init_file.py`** 帮你生成空白初始化文件：每行字符数 = `ceil(WORD_WIDTH/4)`、行数 = `DEPTH`，默认全 0，可照此格式改写成自定义初值。

## 7. 下一步学习建议

- **回看构建块**：本讲反复用到 `Address_Decoder_Behavioural`（u5-l2）、`Multiplexer_One_Hot`/`Multiplexer_Binary_Structural`（u5-l2）、`Population_Count`（u16-l1）、`Bitmask_Isolate_Rightmost_1_Bit`（u16-l2）、`Arbiter_Round_Robin`（u11-l1）、`Word_Reducer`（u5-l1）。如果你对其中某个还不熟，回去补对应讲义，会大大提升阅读 `RAM_Multiported_LE` 的流畅度。
- **继续存储器主题**：`index.html` 的 Memory 分类下还有规划中的 **LVT / XOR / I-LVT** 多端口存储（[index.html:L175-L177](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/index.html#L175-L177)）和 **CAM**（[index.html:L178](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/index.html#L178)）——它们是比「复制法」更省面积的多端口与内容寻址存储技巧，目前尚未实现，可作为阅读经典论文（LVT、XOR 减少 1WnR 副本数）的入门指引。
- **下一单元**：u8（整数算术与计数器）会把 `RAM_Multiported_LE` 里出现的 `Population_Count`、`Address_Decoder` 等构件组合成加减法器与计数器，是从「存储」走向「运算」的自然过渡。
- **动手贡献**：如果你正打算给本书加一个新存储模块，先按 u18-l1 的流程用 `generate_file_skeleton.py` 生成骨架（参数默认全 0、含 `default_nettype none`），再参照本讲两种初始化路径与端口拼接约定写实现，最后用 `RAM_generate_empty_init_file.py` 配套生成示例初始化文件。
