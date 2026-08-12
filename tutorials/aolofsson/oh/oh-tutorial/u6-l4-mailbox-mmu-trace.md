# emailbox、emmu 与 etrace

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 **emailbox** 如何用一个 FIFO 把「写事务」变成「消息入队」，并在队列非空时拉起一个**电平中断**；
- 说清 **emmu** 如何用一张「以地址高位为索引的查找表」把虚拟地址**翻译**成（更宽的）物理地址，即一个最简 MMU；
- 说清 **etrace** 如何像一台**片上逻辑分析仪**那样，在检测到信号变化时把 `{信号向量, 时间戳}` 采样进存储、再通过寄存器接口读回；
- 体会这三个「小外设」如何**复用同一套范式**：emesh 104 位包接口（u5-l1）+ 地址映射（u6-l1），只是各自的「内核」不同——缓冲、翻译、采集。

> 本讲承接 u5-l1（emesh 包格式与 access/wait 握手）与 u6-l1（`.vh` 寄存器映射 + 地址译码 + 写选通），不再从零解释这两个机制。

## 2. 前置知识

在进入源码前，先用三句话建立直觉：

- **消息邮箱（mailbox）**：跨进程 / 跨核通信里最常见的结构——一方往里「投信」（写），另一方「取信」（读），中间用一个 FIFO 缓冲。硬件邮箱多出一个**中断输出**：只要信箱非空，就通知接收方「有信」。可以类比「门口的收件箱 + 一盏提示灯」。
- **MMU（Memory Management Unit，存储管理单元）**：CPU 发出的地址是「虚拟地址」，真正访问存储用的是「物理地址」。MMU 负责把前者翻译成后者，最常见的做法是**查表**——用地址的一部分当索引，去一张表里取出对应的物理基址，再拼上地址的低位。本讲的 emmu 就是一个这样的表查找式翻译器。
- **片上逻辑分析仪（logic analyzer / trace）**：调试芯片时，你往往看不到内部信号的波形。etrace 的做法是：把若干感兴趣的内部信号接进来，一旦它们发生变化，就「拍一张快照」（连同当前时间戳）写进一块片上存储，事后软件通过寄存器接口把快照读出来分析。本质是「芯片自带的行车记录仪」。

几个会反复出现的术语：**电平中断**（信号只要处于某电平就一直有效，与之相对的是只闪一个周期的**脉冲中断**）、**prog_full**（FIFO「快满了」的提前预警阈值，见 u3-l2）、**CDC / 跨时钟域**（u2-l4）、**写选通 / write strobe**（u6-l1）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `emailbox/hdl/emailbox.v` | 邮箱主模块：emesh 写口入队、寄存器读口出队、中断与状态生成 |
| `emailbox/hdl/emailbox_regmap.vh` | 邮箱三个寄存器（LO/HI/STAT）与地址组宏定义 |
| `emailbox/dv/dut_emailbox.v` | 把 emailbox 包成测试平台 dut 的包装 |
| `emmu/hdl/emmu.v` | 地址翻译器：查表把 dstaddr 翻译成更宽的物理地址 |
| `etrace/hdl/etrace.v` | 片上逻辑分析仪：变化检测 + 时间戳采样 + 存储读回 |
| `etrace/hdl/etrace_regmap.vh` | etrace 的寄存器 / 存储组地址宏 |
| `etrace/dv/test/test_trace.memh` | etrace 的 `.emf` 格式激励（扩展名是 `.memh`） |

> 注意：`emmu/` 没有 `regmap.vh`——它的「表」不是寄存器，而是直接用寄存器写口配置的查找表存储（见 4.2）。这是它与另外两个外设的一个关键差别。

### 一个贯穿三者的现实警示

这三个模块都实例化了一个叫 **`packet2emesh`**（个别地方叫 `emesh2packet`）的子模块来「把 104 位 emesh 包拆成 write / dstaddr / data / srcaddr 等字段」。但**全仓库都找不到 `packet2emesh` 的定义**（只有调用，没有 `module packet2emesh`）。这与 gpio（u6-l2）、spi（u6-l3）遇到的「`enoc_pack`/`enoc_unpack` 改名漂移」是同一类历史遗留——它的角色等价于 u5-l3 讲过的 `emesh_unpack`。因此这三个模块**都不能原样编译**，本讲的「代码实践」以**源码阅读 + 手算**为主，仿真一律标注「待本地验证」。读源码时，一律以**实际文本**为准，注释里的旧路径（如 `../../common/hdl`、`../../memory/hdl`）大多已失效。

## 4. 核心概念与源码讲解

三者共用一条主干：

```
emesh 104 位包 ──[packet2emesh 拆字段]──> write/dstaddr/data/srcaddr
                                              │
                         地址译码（u6-l1 范式）│
                                              ▼
                                    ┌─────────────────────┐
                                    │  各自的「内核」：      │
                                    │  emailbox = FIFO      │
                                    │  emmu     = 查找表     │
                                    │  etrace   = 采样存储   │
                                    └─────────────────────┘
                                              │
                            读回：case 选 read_data / 直接改包
```

差别只在中间那个「内核」和它产出的副产物（中断 / 翻译地址 / 采样数据）。下面逐个拆。

| 维度 | emailbox | emmu | etrace |
| --- | --- | --- | --- |
| 内核 | FIFO（默认深度 32） | 4096 项查找表（`oh_memory_dp`） | 采样存储（`fifo_cdc`） |
| 写入来源 | emesh 写口（投信） | 寄存器写口（配置表项） | 片内信号变化（自动采样） |
| 读出方式 | 寄存器读口（取信，弹出） | 不读表，直接改写输出包 | 寄存器读口（读快照） |
| 副产物 | 电平中断 `mailbox_irq` + 反压 `mailbox_wait` | 翻译后的物理地址 | 仿真时落盘的 `.trace` 文件 |
| regmap.vh | 有 | 无（表即存储） | 有 |

---

### 4.1 emailbox：FIFO 邮箱与电平中断

#### 4.1.1 概念说明

emailbox 是一个「FIFO 邮箱 + 中断输出」外设。文件头注释把它的用途说得最清楚：

[emailbox/hdl/emailbox.v:1-12](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emailbox/hdl/emailbox.v#L1-L12) —— 顶部注释定义了三个寄存器与三条使用须知。

三个寄存器：

- `E_MAILBOXLO`：FIFO 表项的低 32 位（读它会**弹出**一个表项，见注释「Reading E_MAILBOXLO causes a fifo rd pointer update」）；
- `E_MAILBOXHI`：FIFO 表项的高 32 位（读它**不**弹出）；
- `E_MAILBOXSTAT`：状态字 `{30'b0, fifo_full, ~fifo_empty}`（即「满」与「非空」两个标志位）。

每个表项实际宽度是 104 位（`MW = PW = 2*AW+40`），但真正承载信息的是低 64 位 `{srcaddr, data}`，高 40 位补 0（见 4.1.3）。所以一封信 = 「谁发的（srcaddr）+ 内容（data）」。

中断 `mailbox_irq` 是**电平中断**：只要 FIFO 非空（或快满 / 已满），并且中断使能打开，中断就一直拉高，直到软件把信取走、FIFO 空了才落下。这就是注释里写的「`embox_not_empty` is a level interrupt signal」。

#### 4.1.2 核心流程

一个「写事务入队 → 触发中断」的完整流程：

1. **写口（投信）**：一个 emesh **写**事务到达 `emesh_access` / `emesh_packet`；
2. **拆包**：`packet2emesh` 把包拆成 `emesh_write`、`emesh_addr`、`emesh_din`（= `{srcaddr, data}`）；
3. **地址译码**：只有当 `write=1` 且地址命中（ID + EGROUP_MMR + EGROUP_MESH + E_MAILBOXLO 都匹配）且 FIFO 非满时，才产生一次 `mailbox_write`；
4. **入队**：把 `{40'b0, emesh_din[63:0]}` 写进 FIFO，FIFO 不再为空；
5. **拉中断**：`mailbox_irq = mailbox_irq_en & (not_empty | prog_full | full)`，此时 `not_empty=1`，中断拉高；
6. **反压**：当 FIFO 到达 `prog_full` 阈值时，`mailbox_wait=1`，向上游回压（access/wait 握手中 wait 高即反压，见 u5-l1）。

读口（取信）是对称的另一侧：软件对 `E_MAILBOXLO` 发起**读**事务，命中且 FIFO 非空时产生 `mailbox_read`，FIFO 弹出一条；读回数据经 `oh_mux3` 在 LO / HI / STAT 三者间选择。

#### 4.1.3 源码精读

**参数与派生宽度** [emailbox/hdl/emailbox.v:26-36](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emailbox/hdl/emailbox.v#L26-L36)：`AW=32`、`DEPTH=32`、`CW=$clog2(DEPTH)=5`（计数值宽度）、`PW=2*AW+40=104`。`TYPE` 参数决定用同步还是异步 FIFO。

**写口译码**（这是「写事务如何入队」的关键）：

[emailbox/hdl/emailbox.v:102-108](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emailbox/hdl/emailbox.v#L102-L108)

```verilog
assign mailbox_write = ~mailbox_full &
                       emesh_access &
                       emesh_write  &
                       (emesh_addr[31:20]==ID) &      // 12'h000（默认）
                       (emesh_addr[19:16]==`EGROUP_MMR) & // 4'hF
                       (emesh_addr[10:8] ==`EGROUP_MESH) & // 3'h7
                       (emesh_addr[RFAW+1:2]==`E_MAILBOXLO); // addr[7:2]==6'hC
```

这是 u6-l1 「地址位 → 写选通」范式的直接应用，只是这里选通信号还多带了一个 `~mailbox_full`（满了就不让写）。译码宏来自 [emailbox/hdl/emailbox_regmap.vh:4-8](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emailbox/hdl/emailbox_regmap.vh#L4-L8)（`E_MAILBOXLO=6'hC` 等）和 [:10-16](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emailbox/hdl/emailbox_regmap.vh#L10-L16)（`EGROUP_MMR=4'hF`、`EGROUP_MESH=3'h7`）。

> 算一下命中的地址（默认 `ID=12'h000`、`RFAW=6`）：`[31:20]=0x000`、`[19:16]=F`、`[10:8]=7`、`[7:2]=001100`。一个代表性的目标地址是 `0x000F0730`。注意 `emailbox/dv/dut_emailbox.v` 在实例化时**没有**覆盖 `ID`，所以它保持默认 `0x000`；而 `emailbox/dv/tests/test_basic.emf` 里写的是 `0x80800000`（`[31:20]=0x808`）——这与邮箱译码**不匹配**。也就是说，那份随仓库附带的测试只是一个通用的「存储读写」样板，并没有真正往邮箱里投信。

**入队数据**：在 FIFO 实例里，写入口的数据是 `{40'b0, emesh_din[63:0]}`（[:168](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emailbox/hdl/emailbox.v#L168) 与 [:190](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emailbox/hdl/emailbox.v#L190)），其中 `emesh_din[31:0]=data`、`emesh_din[63:32]=srcaddr`（由 [:91-100](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emailbox/hdl/emailbox.v#L91-L100) 的 `p2e0` 拆出）。

**FIFO 选型** [emailbox/hdl/emailbox.v:152-195](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emailbox/hdl/emailbox.v#L152-L195)：用 `generate if(TYPE=="ASYNC")` 在 `oh_fifo_async`（双时钟，格雷码指针，见 u3-l2）与 `oh_fifo_sync`（单时钟）间二选一。写口走 `wr_clk`、读口走 `rd_clk`，所以邮箱天然支持「写时钟域 ≠ 读时钟域」——这正是异步 FIFO 的用武之地。

**中断与状态**（这是「队列非空时拉高中断」的关键）：

[emailbox/hdl/emailbox.v:201-214](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emailbox/hdl/emailbox.v#L201-L214)

```verilog
assign mailbox_not_empty    = ~mailbox_empty;

assign mailbox_irq          = mailbox_irq_en &
                              (mailbox_not_empty |
                               mailbox_prog_full |
                               mailbox_full);

assign mailbox_wait         = mailbox_prog_full;

assign mailbox_status[31:0] = {message_count[CW-1:0],
                               13'b0,
                               mailbox_prog_full,
                               mailbox_full,
                               mailbox_not_empty};
```

读法：`mailbox_irq` 是三个「有事」条件（非空 / 快满 / 满）的**或**，再与使能相**与**——典型的电平中断。`mailbox_wait` 直接等于 `prog_full`，把 FIFO 的提前预警接到 emesh 的反压通路上。状态字把 `count` 放高位、三个标志放低位，软件一次读 `E_MAILBOXSTAT` 就能知道「有几封信 + 是否快满」。

**读口与读回选择** [emailbox/hdl/emailbox.v:129-147](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emailbox/hdl/emailbox.v#L129-L147)：`mailbox_read` 只在读 `E_MAILBOXLO` 且非空时成立（因此只有读 LO 才弹栈）；`read_lo/read_hi/read_status` 是它打了一拍后的寄存版，用作 `oh_mux3` 的 one-hot 选择信号，在 `mailbox_data` 的低 32（LO）、高 32（HI）与 `mailbox_status` 之间挑出 `reg_rdata`。

#### 4.1.4 代码实践

**目标**：照着源码走一遍「一个写事务如何入队，并在队列非空时拉高 `mailbox_irq`」，再手算一条能真正命中邮箱的 `.emf` 激励。

**操作步骤**：

1. 打开 [emailbox/hdl/emailbox.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emailbox/hdl/emailbox.v)，定位 4.1.3 引用的三段：写口译码（L102-108）、FIFO 写入口（L168/L190）、中断生成（L203-206）。
2. 假设 `mailbox_irq_en=1`、FIFO 初始为空，追踪一次写事务：
   - `emesh_write=1`、地址 = `0x000F0730` → `mailbox_write=1`；
   - FIFO 写入 `{40'b0, srcaddr, data}` → `mailbox_empty` 翻为 0 → `mailbox_not_empty=1`；
   - `mailbox_irq = 1 & (1 | 0 | 0) = 1`，中断拉高（电平）。
3. 手写一条 `.emf` 行（`datahi_datalo_dstaddr_ctrlmode_access`，详见 u4-l2/u5-l1）把 `0xDEADBEEF` 投进邮箱（32 位写，`ctrlmode=05`）：
   ```
   00000000_DEADBEEF_000F0730_05_0010
   ```
4. 再写一条读 `E_MAILBOXSTAT` 的行（读、32 位，`ctrlmode=04`；状态地址 `[7:2]=6'hE` → `0x000F0738`）：
   ```
   00000000_00000000_000F0738_04_0010
   ```

**需要观察的现象**：写后 `mailbox_not_empty` 立即为 1；`mailbox_irq` 随之拉高并保持；读 LO 之后 `count` 减 1，若减到空则 `mailbox_irq` 落下。

**预期结果**：在波形上应能看到 `mailbox_write` 是单拍脉冲，`mailbox_irq` 是持续电平，且 `mailbox_status` 的低位随 `count` 变化。

> **待本地验证**：因 `packet2emesh` 在仓库中无定义，`emailbox.v` 不能原样编译。若要仿真，需先用 u5-l3 的 `emesh_unpack`（或自写一个等价拆包模块）替换 `p2e0`/`p2e1`，并确认 `oh_fifo_async`/`oh_fifo_sync` 的端口名与 [:155-192](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emailbox/hdl/emailbox.v#L155-L192) 的实例化一致后再跑。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `mailbox_irq` 用 `mailbox_not_empty`（电平）而不是「每写一封就闪一个周期」（脉冲）？
**答案**：因为这是邮箱语义——只要还有未取走的信，接收方就应被持续提醒；若改成单拍脉冲，软件一旦漏响应就会永久错过。电平中断配合「读 LO 清空」天然形成「取完才落」的闭环。

**练习 2**：读 `E_MAILBOXHI` 会不会让 FIFO 弹出？为什么？
**答案**：不会。`mailbox_read`（弹栈条件）只包含 `addr==E_MAILBOXLO`（[:129-131](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emailbox/hdl/emailbox.v#L129-L131)）；`read_hi` 仅用于选通读回数据（[:136](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emailbox/hdl/emailbox.v#L136)），不接 FIFO 的 `rd_en`。所以设计上「读 LO = 取信 + 弹栈」，HI 只是附带读同一表项的高位。

---

### 4.2 emmu：地址翻译查找表

#### 4.2.1 概念说明

emmu（e-MMU）是一个「存储事务翻译器」，文件头注释把它的原理概括得很到位：

[emmu/hdl/emmu.v:1-14](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emmu/hdl/emmu.v#L1-L14) —— 用地址高 12 位 `[31:20]` 当索引查表，输出形如 `{table_data, dstaddr[19:0]}`。

核心思想：把 32 位地址空间切成 **4096 个区**（\(2^{12}\)），每个区大小 \(2^{20}\) 个地址。用地址的最高 12 位（区号）去查一张 4096 项的表，取出该区对应的「物理基址」，再拼上地址的低 20 位（区内地偏移），就得到翻译后的（更宽的）地址。注释里把它叫作「trampolining to 64 bit space」——蹦床式地把 32 位地址弹到 64 位空间。

与 emailbox 不同，emmu **不配 `.vh` 寄存器映射**：它的「表」就是一块双口存储（`oh_memory_dp`），软件通过寄存器写口直接写表项，运行时硬件用读口查表。

#### 4.2.2 核心流程

1. **配置表（写口）**：软件通过 `reg_packet` 写口，把每个区的物理基址写进查找表；写地址 `reg_dstaddr[14:3]` 当表项索引（12 位），`mem_wem` 决定写表项的低 32 位还是高 16 位；
2. **查表（读口）**：一个 emesh 事务到达 `emesh_packet_in`，取其 `dstaddr[31:20]`（区号）作为读地址，从表里读出 `emmu_lookup_data`（48 位）；
3. **拼物理地址**：
   - `mmu_en=1`：`physical = {lookup_data[43:0], dstaddr[19:0]}`（高 44 位来自表 + 低 20 位原样透传）；
   - `mmu_en=0`：`physical = {32'b0, dstaddr}`（直通，不翻译）；
4. **重打包输出**：把翻译后的地址塞回包的 dstaddr 字段，其余字段（srcaddr/data/控制字节）原样保留，输出 `emesh_packet_out`；
5. **流水对齐**：因为查表花了 1 拍，输入包用两级寄存器（`emesh_access_out` / `emesh_packet_reg`）打拍对齐，并用 `emesh_wait_in` 做反压。

#### 4.2.3 源码精读

**参数** [emmu/hdl/emmu.v:27-31](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emmu/hdl/emmu.v#L27-L31)：`MW=48`（表项宽）、`MAW=12`（地址位宽，即 4096 项）、`PW=2*AW+40=104`。

**配置写逻辑** [emmu/hdl/emmu.v:78-103](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emmu/hdl/emmu.v#L78-L103)：

```verilog
// 写掩码：bit2=0 写低 32 位；bit2=1 写高 16 位
assign mem_wem[MW-1:0] = ~reg_dstaddr[2] ? {{(MW-32){1'b0}},32'hFFFFFFFF} :
                                           {{(MW-32){1'b1}},32'h00000000};
assign mem_write        = reg_access & reg_write;
assign mem_data[MW-1:0] = {reg_data[31:0], reg_data[31:0]};
```

读法：表项 48 位，被拆成「低 32 + 高 16」两段分别写。`mem_data` 把同一份 32 位 `reg_data` 复制到高低两段，靠 `mem_wem` 选通其中一段——这是一种朴素的「分两次写一条宽表项」的写法。

**查找表** [emmu/hdl/emmu.v:120-137](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emmu/hdl/emmu.v#L120-L137)：

```verilog
oh_memory_dp #(.DW(MW), .DEPTH(4096)) memory_dp (
    .rd_en   (emesh_access_in),
    .rd_addr (emesh_dstaddr_in[31:20]),  // 用区号查表
    .wr_en   (mem_write),
    .wr_wem  (mem_wem[MW-1:0]),
    .wr_addr (reg_dstaddr[14:3]),         // 12 位表项索引
    ...);
```

读地址是事务 `dstaddr` 的高 12 位（区号），写地址是配置地址的 `14:3`（也是 12 位）——读写两侧都把 12 位映射到 4096 项。

> **现实警示**：`oh_memory_dp` 在仓库中**没有定义**（stdlib 里有同功能的 `oh_dpram`，见 u3-l1）。这与 `packet2emesh` 一样是改名/迁移遗留，emmu 不能原样编译。

**流水对齐** [emmu/hdl/emmu.v:145-153](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emmu/hdl/emmu.v#L145-L153)：输入 `access` 与 `packet` 都在 `rd_clk` 下打一拍，且都受 `~emesh_wait_in` 控制——下游反压时整级冻结，符合 emesh 握手。

**地址翻译与重打包**（本模块最关键的两行）[emmu/hdl/emmu.v:156-164](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emmu/hdl/emmu.v#L156-L164)：

```verilog
// 64 位物理地址：使能时高 44 位来自表 + 低 20 位透传；否则直通原地址
assign emesh_dstaddr_out[63:0] = mmu_en ? {emmu_lookup_data[43:0],
                                           emesh_packet_reg[27:8]}
                                        : {32'b0, emesh_packet_reg[39:8]};
// 把新地址塞回 dstaddr 字段（[39:8]），其余字段不变
assign emesh_packet_out[PW-1:0] = {emesh_packet_reg[PW-1:40],
                                   emesh_dstaddr_out[31:0],
                                   emesh_packet_reg[7:0]};
```

用 u5-l1 的包位序看就很清楚：`packet[7:0]` 是控制字节、`[39:8]` 是 dstaddr、`[103:40]` 是 data+srcaddr。所以 `packet_reg[27:8]` 正是 dstaddr 的低 20 位（区内地偏移），`packet_reg[39:8]` 是完整 dstaddr（直通时用）。翻译后的物理地址取低 32 位塞回 dstaddr 字段、高 32 位丢弃（高半段 `emesh_packet_hi_out` 是注释掉的 TODO，见 [:167](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emmu/hdl/emmu.v#L167)）。

翻译关系可写成（使能时）：

\[
\text{physical}[63:0] \;=\; \{\,\text{table}[\,\text{va}[31:20]\,][43:0]\,,\ \text{va}[19:0]\,\}
\]

#### 4.2.4 代码实践

**目标**：手算一次完整的「配置表项 → 查表翻译」，体会蹦床式地址重映射。

**操作步骤**：

1. 读 [emmu/hdl/emmu.v:120-137](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emmu/hdl/emmu.v#L120-L137) 与 [:156-164](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emmu/hdl/emmu.v#L156-L164)，确认「区号 = `dstaddr[31:20]`」「区内地偏移 = `dstaddr[19:0]`」。
2. 设想软件要把虚拟区 `0xABC`（即所有 `0xABC?????` 地址）映射到物理基址 `0x000F0730000`（44 位）。这需要往表项索引 `0xABC` 写入 `table[0xABC][43:0] = 0x000F0730000` 的高 / 低段。
3. 现在有一笔事务 `dstaddr = 0xABC12345`（区号 `0xABC`、偏移 `0x12345`）到达，`mmu_en=1`：
   - 查表得 `lookup_data[43:0] = 0x000F0730000`；
   - 输出物理地址 = `{0x000F0730000, 0x12345}`（高 44 + 低 20 = 64 位）。
4. 把 `mmu_en` 改为 0，重算：输出应回到 `{32'b0, 0xABC12345}`（直通）。

**需要观察的现象**：使能前后，同一笔输入事务的输出 `emesh_packet_out` 中 dstaddr 字段的差异；下游反压（`emesh_wait_in=1`）时 `emesh_access_out` 是否保持不动。

**预期结果**：使能时 dstaddr 被替换为表项基址 + 偏移；反压时整级流水冻结。

> **待本地验证**：`oh_memory_dp` 无定义，仿真需先替换为 `oh_dpram` 并核对端口（`rd_dout/rd_en/rd_addr`、`wr_en/wr_wem/wr_addr` 是否与 [:124-137](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emmu/hdl/emmu.v#L124-L137) 的连接一致）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 emmu 没有 `regmap.vh`，而 emailbox / etrace 有？
**答案**：因为 emmu 的配置对象不是「若干离散寄存器」，而是一块 4096 项的**存储表**。离散寄存器适合用 `.vh` 宏编号地址；连续表项则直接用地址位当索引（`wr_addr = reg_dstaddr[14:3]`），不需要为每一项起名字。

**练习 2**：翻译公式只把物理地址的**低 32 位**塞回输出包，高 32 位丢了。这意味着什么？
**答案**：在当前 104 位 emesh 包里 dstaddr 字段只有 32 位（u5-l1），所以即便翻译出 64 位物理地址，真正随包传递的只有低 32 位；高 32 位是注释里的 TODO（`emesh_packet_hi_out`）。也就是说，完整 64 位翻译需要扩展包格式或另开通道才能落地。

---

### 4.3 etrace：片上逻辑分析仪与时间戳采样

#### 4.3.1 概念说明

etrace 是一个「参数化逻辑分析仪」，README 一句话讲清了它做什么：

[etrace/README.md:1-19](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/etrace/README.md#L1-L19) —— 把一组输入信号采样进双口存储，每条样本带一个计数器时间戳，在 `trace_clk` 上升沿采样，事后通过存储映射接口读回。

它解决的问题是：芯片流片后，内部信号无法用示波器探头直接观测。etrace 把感兴趣的信号（`trace_vector`，默认 32 位）接进来，每当这些信号**发生变化**，就写一条 `{vector, timestamp}` 进存储；软件事后读出，重建波形。

配置位（写在 `ETRACE_CFG` 寄存器，地址 `0x810F0000`，见 [etrace/hdl/etrace_regmap.vh:1-3](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/etrace/hdl/etrace_regmap.vh#L1-L3)）：`trace_enable[0]`（使能）、`loop_enable[1]`（循环缓冲，满了覆盖旧值）、`async_mode[2]`（异步 / 同步采样）、`samplerate[7:4]`（8 档采样率）。

> **重要警示**：`etrace.v` 是一个**明显未完成的重构中**模块，不能直接编译，也不能直接照搬阅读，必须与它的 dut 对照着看：
> - 它声明的端口（`trace_clk/trace_trigger/trace_vector/cfg_access_in/data_access_out/...`，[:1-35](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/etrace/hdl/etrace.v#L1-L35)）与 [etrace/dv/dut_etrace.v:79-91](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/etrace/dv/dut_etrace.v#L79-L91) 实例化用的端口（`mi_en/mi_we/mi_addr/mi_clk/mi_din/mi_dout/...`）**完全对不上**；
> - 模块体内部引用的 `mi_en/mi_we/mi_addr/...` 在端口表里**不存在**；
> - `emesh2packet e2p0` 被**实例化了两次**（[:81](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/etrace/hdl/etrace.v#L81) 与 [:93](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/etrace/hdl/etrace.v#L93)），实例名冲突；
> - 子模块用了无 `oh_` 前缀的 `dsync`（[:167](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/etrace/hdl/etrace.v#L167)）、`fifo_cdc`（[:207](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/etrace/hdl/etrace.v#L207)），而 stdlib 里是 `oh_dsync` / `oh_fifo_cdc`；
> - 文件末尾的 `endmodule` 注释写成 `// emailbox`（[:234](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/etrace/hdl/etrace.v#L234)），是复制粘贴遗留。
>
> 因此下面讲「核心流程 / 源码」时，以**逻辑上自洽的采样与计时部分**为准——这部分能清楚体现「逻辑分析仪」的设计意图；端口与封装问题作为工程现实单列。

#### 4.3.2 核心流程

1. **配置**：软件写 `ETRACE_CFG`（`0x810F0000`）的 `trace_enable=1` 并给 `trace_trigger`（外部硬件触发）拉高；`trace_enable` 经 `dsync` 同步到 `trace_clk` 域（避免配置位跨域采样产生亚稳态，见 u2-4）；
2. **计时**：`cycle_counter` 在「使能且触发」时每拍 +1，作为时间戳；
3. **变化检测**：把 `trace_vector` 打一拍得 `trace_vector_reg`，二者异或再归约，得到 1 位 `change_detect`（任意一位变化即为 1）；
4. **采样**：`trace_sample = trace_enable & trace_trigger & change_detect`——只有「使能 + 触发 + 有变化」三者同时成立才记一条；
5. **写存储**：`trace_addr` 在每次采样后 +1，把 `{vector, timestamp}` 写进采样存储（`fifo_cdc`）；
6. **读回**：软件通过存储映射接口（`ETRACE_MEM` 组，`0x810A0000` 起）按地址读出快照；仿真模式下还会把每条样本 `$fwrite` 到 `<NAME>.trace` 文件。

#### 4.3.3 源码精读

**配置寄存器解析** [etrace/hdl/etrace.v:134-153](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/etrace/hdl/etrace.v#L134-L153)：`cfg_reg` 的 bit0/1/2/7:4 分别对应使能、循环、异步模式、采样率（采样率注释列了 8 档：100→0.5 MS/s）。

**配置位跨域同步** [etrace/hdl/etrace.v:167-171](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/etrace/hdl/etrace.v#L167-L171)：`mi_trace_enable`（配置时钟域）经 `dsync` 同步到 `trace_clk` 域得 `trace_enable`——这是 u2-4「先同步、再用」范式的直接应用。

**时间戳计数器** [etrace/hdl/etrace.v:178-182](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/etrace/hdl/etrace.v#L178-L182)：

```verilog
always @ (posedge trace_clk)
  if(~trace_enable)
    cycle_counter[DW-1:0] <= 'b0;          // 失能即清零
  else if (trace_trigger)
    cycle_counter[DW-1:0] <= cycle_counter + 1'b1;  // 触发期间计数
```

**变化检测与采样条件** [etrace/hdl/etrace.v:189-197](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/etrace/hdl/etrace.v#L189-L197)：

```verilog
always @ (posedge trace_clk)
  trace_vector_reg[VW-1:0] <= trace_vector[VW-1:0];   // 打一拍

assign change_detect = |(trace_vector_reg ^ trace_vector); // 任一位变化

assign trace_sample  = trace_enable & trace_trigger & change_detect;
```

这是教科书式的「边沿 / 变化检测」——与 u2-4 的 `oh_edge2pulse` 思路一致：先寄存、再异或。区别是这里要检测的是「多位向量中任意一位变化」，所以异或后还要或归约（`|(...)`）。

**采样地址与存储** [etrace/hdl/etrace.v:200-217](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/etrace/hdl/etrace.v#L200-L217)：`trace_addr` 在 `trace_sample` 时自增，把样本送进 `fifo_cdc`（跨时钟域 FIFO，把 `trace_clk` 域的样本搬到读出时钟域）。

**仿真落盘** [etrace/hdl/etrace.v:219-232](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/etrace/hdl/etrace.v#L219-L232)：在 `-DTARGET_SIM`（见 u1-l3）下，每次采样都用 `$fwrite` 把 `trace_vector` 与 `cycle_counter` 写进 `<NAME>.trace` 文件——这是「纯仿真专用代码分支」的典型用法，真实硬件综合时这段会被宏屏蔽。

**配套激励** [etrace/dv/test/test_trace.memh:1-5](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/etrace/dv/test/test_trace.memh#L1-L5)：注意这个文件**扩展名是 `.memh` 但内容是 `.emf` 格式**（5 段下划线分隔的十六进制）。第一行 `...810F0000_05_0010 //enable tracer` 正是写 `0x1`（`trace_enable=1`）到 `ETRACE_CFG`（`0x810F0000`），印证了 4.3.1 的地址解码。

#### 4.3.4 代码实践

**目标**：读懂「配置使能 → 变化检测 → 采样落盘」这条链，并对照 dut 找出 etrace.v 与 dut_etrace.v 的端口不一致。

**操作步骤**：

1. 读 [etrace/README.md:1-19](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/etrace/README.md#L1-L19)，记下使用步骤（写 `ETRACE_CFG` 使能 → 采样 → 从 `ETRACE_MEM` 读回）。
2. 在 [etrace/hdl/etrace.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/etrace/hdl/etrace.v) 中按顺序定位：配置解析（L134-153）→ dsync 同步（L167-171）→ 时间戳（L178-182）→ 变化检测与采样（L189-197）→ 地址自增（L200-204）。
3. 对照 [etrace/dv/dut_etrace.v:79-91](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/etrace/dv/dut_etrace.v#L79-L91)：dut 用 `.mi_en/.mi_we/.mi_addr/.mi_clk/.mi_din/.mi_dout/.trace_trigger(1'b1)/.trace_vector(mi_srcaddr)` 实例化 etrace，而 etrace.v 的端口表里根本没有 `mi_en/mi_we/...`——把这些不一致逐条记下。
4. 假设 `trace_vector` 在第 0、3、7 拍各翻转一次，手画 `change_detect` 与 `trace_sample` 的时序（设 `trace_enable` 与 `trace_trigger` 恒为 1）。

**需要观察的现象**：`change_detect` 只在 `trace_vector` 翻转的那一拍为 1；`trace_addr` 只在 `trace_sample=1` 时自增；`cycle_counter` 每拍 +1。

**预期结果**：样本数 = 变化次数；每条样本的时间戳 = 该次变化发生时的 `cycle_counter` 值。

> **待本地验证**：etrace.v 当前无法编译（端口缺失、子模块名无前缀、重复实例）。若要仿真，需先统一端口表（建议以 dut_etrace.v 的 `mi_*` 契约为准补全 etrace.v 端口），并把 `dsync`→`oh_dsync`、`fifo_cdc`→`oh_fifo_cdc`、`packet2emesh`→`emesh_unpack` 替换对齐后再跑。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `trace_enable` 要先过一级 `dsync` 才在 `trace_clk` 域使用？
**答案**：因为 `trace_enable` 来自配置（寄存器）时钟域，而采样逻辑跑在 `trace_clk` 域——直接跨域使用会触发亚稳态（u2-4）。先同步再使用，是 CDC 的标准范式。

**练习 2**：`change_detect` 用的是 `|(a ^ b)`（或归约），如果换成 `&(a ^ b)`（与归约）会怎样？
**答案**：`|(a^b)` 在「任意一位变化」时为 1，符合「记录任何变化」的逻辑分析仪需求；`&(a^b)` 只在「所有位都变化」时才为 1，会漏掉绝大多数单 bit 翻转，违背设计意图。

---

## 5. 综合实践

把三个外设串成一个「调试子系统」的小设计（纸面练习）：

**场景**：一个嵌入式核要通过 emesh 调试一个远端模块。请你规划：

1. 用 **emailbox** 做核与远端的消息通道——核写一条「命令」入队，远端用中断（`mailbox_irq`）得知有命令，处理完把「结果」写回核自己的邮箱；
2. 在两者之间插一个 **emmu**，让核用一段固定的虚拟地址（如 `0xABC?????`）访问远端的真实物理区，地址对核透明；
3. 用 **etrace** 监视远端的关键状态信号，问题发生时回放采样。

**任务**：

- 画出三者与 emesh 总线的连接框图（注意 emailbox 有独立的写 / 读两口、emmu 是穿通式翻译、etrace 是旁路监听）；
- 为 emailbox 写 2 条 `.emf`（投信 + 读状态），地址按 4.1.3 的译码（`0x000F0730` / `0x000F0738`）手算；
- 指出这三个模块要真正跑起来，各自需要先补齐哪个缺失子模块（`packet2emesh`、`oh_memory_dp`/`oh_dpram`、`oh_dsync`+`oh_fifo_cdc`）；
- 思考：三个外设共用同一套「emesh 接口 + 地址映射」骨架，分别带来什么好处？（提示：复用 u5-l1/u6-l1 的协议与寄存器范式、可统一接入 dv_top 测试平台。）

> 这一题没有唯一答案，重点是让你把「缓冲 / 翻译 / 采集」三种内核与「统一的外壳」对应起来。完成后，你应能用一句话向别人解释这三个外设各自干什么、又为什么长得像。

## 6. 本讲小结

- **emailbox = FIFO 邮箱 + 电平中断**：emesh 写口入队、寄存器读口出队（读 `E_MAILBOXLO` 才弹栈），`mailbox_irq = irq_en & (not_empty|prog_full|full)` 是持续电平，`mailbox_wait=prog_full` 提前反压。
- **emmu = 表查找式地址翻译器**：用 `dstaddr[31:20]`（区号）查一张 4096 项表，输出 `{table[43:0], dstaddr[19:0]}` 的更宽物理地址；它是「配置表 + 数据通路穿通改包」结构，故无 `regmap.vh`。
- **etrace = 片上逻辑分析仪**：变化检测（`|(reg^vec)`）触发、`cycle_counter` 当时间戳、样本进跨域 FIFO；仿真时还能 `$fwrite` 落盘。当前文件处于重构中、端口与子模块名均需对齐。
- **三者同构**：都遵循 u5-l1 的 emesh 包接口 + u6-l1 的地址映射范式，内核分别是「缓冲 / 翻译 / 采集」。
- **共同的工程现实**：都依赖仓库中无定义的 `packet2emesh`（及 emmu 的 `oh_memory_dp`），不能原样编译；阅读一律以源码实际文本为准。

## 7. 下一步学习建议

- 想看「FIFO + 跨时钟域」的底层实现，回看 **u3-l2（FIFO 设计）**——emailbox 的 `oh_fifo_async` 分支就建立在其上。
- 想理解 emmu 翻译出的地址最终如何被系统消费，进入 **u8-l1（AXI 协议与 emaxi 主桥）**——那是地址事务走向标准总线的下一站。
- 想看一个「结构完整、可直接参照」的外设范例，回到 **u6-l2（GPIO 模块全解析）** 对照——它会让你更清楚 etrace「应该」长成什么样。
- 继续沿第 6 单元往下，可学 **elink（第 7 单元）**——emailbox/emmu/etrace 这类外设产生的 emesh 事务，最终常通过 elink 链路跨芯片传输。
