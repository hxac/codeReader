# axi_lite_mailbox：邮箱与中断

## 1. 本讲目标

本讲精读 `axi_lite_mailbox`——一个带两个 AXI4-Lite 从端口、用于核间通信的硬件邮箱。学完后你应当能够：

- 说清「双端口邮箱」的拓扑：两个端口之间靠两条单向 FIFO 互联，端口 0 写的数据从端口 1 读出，反之亦然。
- 列举邮箱内部的 10 个寄存器（MBOXW/MBOXR/STATUS/ERROR/WIRQT/RIRQT/IRQS/IRQEN/IRQP/CTRL）及其读写在源码中的实现位置。
- 解释中断的整条触发链：阈值比较 → sticky 的 IRQS → IRQEN 使能 → IRQP 挂起 → 电平/边沿转换输出 `irq_o`。
- 看懂 `tb_axi_lite_mailbox` 如何用两个随机 Lite 主端把数据从端口 0 投递到端口 1、并在邮箱非空时验证中断拉高。

## 2. 前置知识

本讲建立在 u12-l1（AXI-Lite 接口与 lite 连接器）之上。阅读前请确认你已经了解：

- **AXI4-Lite 的信号集合**：相比完整 AXI4，它删去了 `len/size/burst/last/id/atop` 等字段，每个事务恒为单拍，只有 `addr/prot/data/strb/resp`。
- **`req_lite_t` / `resp_lite_t` 结构体**：由 `AXI_LITE_TYPEDEF_*` 宏生成，是 AXI-Lite 内核模块的标准端口类型（参见 u12-l1）。
- **valid/ready 握手与 `RESP_OKAY/RESP_SLVERR`**：握手铁律「valid 一旦拉高在握手前不可撤」；本模块用 `RESP_SLVERR` 表示「写满 FIFO」「读空 FIFO」「访问只读/未映射寄存器」。
- **`addr_decode` 与 `spill_register`**：`addr_decode`（来自 common_cells）把地址译成寄存器下标；`spill_register` 切断组合路径并加一拍延迟，本模块用它隔离 B/R 响应。
- **`fifo_v3`**：common_cells 提供的标准 FIFO，带 `usage_o`（当前填充深度）与 `flush_i`（清空）。

如果你对上述任一项不熟悉，请先回看 u12-l1 与 u3 系列。

## 3. 本讲源码地图

| 文件 | 作用 |
|:--|:--|
| [src/axi_lite_mailbox.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_mailbox.sv) | 全部实现都在这一个文件里，含三个模块：顶层 `axi_lite_mailbox`、从端 `axi_lite_mailbox_slave`、接口外壳 `axi_lite_mailbox_intf`。 |
| [test/tb_axi_lite_mailbox.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_lite_mailbox.sv) | 定向验证测试台，两个 `axi_lite_rand_master` 分别驱动端口 0/1，覆盖寄存器读、阈值中断、错误中断、flush、未映射访问。 |
| [doc/axi_lite_mailbox.md](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/doc/axi_lite_mailbox.md) | 官方寄存器手册，逐位说明 10 个寄存器，是本讲的权威字段表。 |

本讲把内容拆成四个最小模块：① 双端口数据通路（两条方向 FIFO）；② 从端寄存器组与地址映射；③ 中断体系；④ flush、错误恢复与测试台。

---

## 4. 核心概念与源码讲解

### 4.1 双端口邮箱的数据通路：两条方向 FIFO

#### 4.1.1 概念说明

「邮箱（mailbox）」是多核/多主系统里最常见的通知原语：一方把消息丢进一个缓冲，另一方在合适的时候取走。硬件邮箱用 FIFO 实现这个缓冲，天然解耦「什么时候写」和「什么时候读」。

`axi_lite_mailbox` 把这个概念做成**对称双端口**：它对外暴露两个独立的 AXI4-Lite 从端口（端口 0 与端口 1），两者之间用**两条单向 FIFO** 互联：

- 一条 FIFO「从端口 0 流向端口 1」：端口 0 写、端口 1 读。
- 一条 FIFO「从端口 1 流向端口 0」：端口 1 写、端口 0 读。

这样任一端口写下的数据，都只能被**对端**读出——这正是「向对端投递消息」的语义。两条 FIFO 互不干扰，所以两个方向可以同时通信。

#### 4.1.2 核心流程

端口 `p` 写一笔数据（写 MBOXW 寄存器）到对端 `1-p` 的过程：

1. 主端在端口 `p` 上发起一次 AXI-Lite 写，地址命中 MBOXW。
2. 从端把写数据 `w.data` 按 `w.strb` 选通压入「`p → 1-p`」那条 FIFO；FIFO 满则拒绝并置错误位。
3. 对端 `1-p` 主端发起一次读（读 MBOXR 寄存器），从端从同一条 FIFO 弹出一拍返回。
4. FIFO 的 `usage_o`（填充深度）同时反馈给两个端口的寄存器逻辑，用于状态位与阈值中断。

「谁写谁读」由顶层把 FIFO 的 push/pop 端口交叉连到两个从端实例决定。

#### 4.1.3 源码精读

顶层 [`axi_lite_mailbox` 的端口列表](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_mailbox.sv#L21-L40) 用数组打包两个端口的请求/响应、中断与基址：

```systemverilog
input  req_lite_t  [1:0] slv_reqs_i,
output resp_lite_t [1:0] slv_resps_o,
output logic       [1:0] irq_o,       // interrupt output for each port
input  addr_t      [1:0] base_addr_i  // base address for each port
```

接着定义 usage（填充深度）类型，位宽比 FIFO 自身 usage 多 1 位——最高位拼上 `full` 标志，方便阈值比较：

```systemverilog
localparam int unsigned FifoUsageWidth = $clog2(MailboxDepth);
typedef logic [FifoUsageWidth:0] usage_t;  // 宽度 = clog2(Depth)+1
```

两条 FIFO 用 `fifo_v3` 实例化，关键看它们的 push/pop/data 怎么交叉连接。 [`i_mbox_0_to_1`](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_mailbox.sv#L125-L143)（端口 0 写 → 端口 1 读）：

```systemverilog
fifo_v3 #(.FALL_THROUGH(1'b0), .DEPTH(MailboxDepth), .dtype(data_t)) i_mbox_0_to_1 (
  .flush_i ( w_mbox_flush[0] | r_mbox_flush[1] ),
  .data_i  ( mbox_w_data[0] ),   // 端口 0 提供写数据
  .push_i  ( mbox_push[0]  ),    // 端口 0 push
  .data_o  ( mbox_r_data[1] ),   // 端口 1 取走读数据
  .pop_i   ( mbox_pop[1]   ),
  ...
);
assign mbox_usage[0] = {mbox_full[0], mbox_0_to_1_usage};
```

注意两个交叉点：`data_i/push_i` 接端口 0 的写侧（`[0]`），`data_o/pop_i` 接端口 1 的读侧（`[1]`）。`flush_i` 是两端 flush 的或——任一端口请求清空都能冲刷这条 FIFO。第二条 [`i_mbox_1_to_0`](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_mailbox.sv#L145-L162) 方向对称。

两个从端实例的接线把这个交叉关系写死。看 [端口 0 实例的 FIFO 端口](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_mailbox.sv#L74-L84)：

```systemverilog
// write FIFO port （端口 0 自己的写侧 -> i_mbox_0_to_1）
.mbox_w_data_o  ( mbox_w_data[0]  ),
.mbox_w_full_i  ( mbox_full[0]    ),
.mbox_w_usage_i ( mbox_usage[0]   ),
// read FIFO port  （端口 0 的读侧 <- i_mbox_1_to_0）
.mbox_r_data_i  ( mbox_r_data[0]  ),
.mbox_r_empty_i ( mbox_empty[1]   ),
.mbox_r_usage_i ( mbox_usage[1]   ),
```

这里有一个容易绊倒读者的**索引约定不一致**，必须看清：写侧三类信号（`mbox_w_data/full/usage`）的数组下标 = FIFO 实例号，而 `mbox_r_data` 的下标 = **读者端口号**。于是端口 0 的读数据写成 `mbox_r_data[0]`（下标 0 是「读者端口 0」），但读空、读深度却写成 `mbox_empty[1]`/`mbox_usage[1]`（下标 1 是「FIFO 实例 1」）。

两者其实指向同一条物理 FIFO。验证一下：`i_mbox_1_to_0.data_o` 接的正是 `mbox_r_data[0]`（[L159](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_mailbox.sv#L159)），而 `mbox_empty[1]`/`mbox_usage[1]` 也来自 `i_mbox_1_to_0`（[L154-L156](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_mailbox.sv#L154-L156)、[L162](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_mailbox.sv#L162)）。所以端口 0 的整条读侧（data/empty/usage）都来自 `i_mbox_1_to_0`——也就是端口 1 写下的数据。这正是「端口 0 读端口 1 写下的数据」的物理实现。端口 1 实例（[L107-L117](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_mailbox.sv#L107-L117)）方向相反：读侧汇聚到 `i_mbox_0_to_1`。

#### 4.1.4 代码实践

**实践目标**：用阅读而非运行的方式，确认两条 FIFO 的方向交叉关系。

**操作步骤**：

1. 打开 [src/axi_lite_mailbox.sv 的 FIFO 实例区](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_mailbox.sv#L125-L162)。
2. 对每条 FIFO，记录其 `data_i/push_i` 接到哪个端口的写侧（`[0]` 还是 `[1]`），`data_o/pop_i` 接到哪个端口的读侧。
3. 再打开两个从端实例（[L57-L88](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_mailbox.sv#L57-L88) 与 [L90-L121](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_mailbox.sv#L90-L121)），核对每个端口的 `mbox_w_*` 与 `mbox_r_*` 索引。

**需要观察的现象**：端口 0 的写侧（`mbox_w_*[0]`）与读侧（`mbox_r_*` 指向 `[1]`）分属两条不同 FIFO；端口 1 同理。

**预期结果**：你应当得到一张表——「端口 0 写 → `i_mbox_0_to_1` → 端口 1 读」「端口 1 写 → `i_mbox_1_to_0` → 端口 0 读」。这正是邮箱「写下的数据只能被对端读出」的实现基础。

#### 4.1.5 小练习与答案

**练习 1**：如果两条 FIFO 都从端口 0 写、端口 0 读，会发生什么？模块还能叫「邮箱」吗？

**答案**：那它就退化成一个单端口的 FIFO 回环，端口 0 写什么就读什么，端口 1 完全失效，失去了「向对端投递」的语义，不再是邮箱。顶层的交叉连线正是为了保证方向性。

**练习 2**：`FifoUsageWidth = $clog2(MailboxDepth)`，当 `MailboxDepth = 16` 时 `usage_t` 是几位？为什么要把 `full` 拼到最高位？

**答案**：`clog2(16) = 4`，`usage_t` 是 5 位（`[4:0]`）。把 `full`（1 位）拼到最高位后，FIFO 满 时 usage 取到最大值，使得「满」与「深度超过阈值」可以统一用一次 `>` 比较处理（详见 4.3 节）。

---

### 4.2 从端寄存器组与地址映射

#### 4.2.1 概念说明

每个端口背后是一个 `axi_lite_mailbox_slave`，它把 AXI-Lite 事务翻译成对一组**寄存器**的读写。这些寄存器分两类：

- **数据寄存器** MBOXW（只写）/ MBOXR（只读）：直接对接 FIFO 的 push/pop。
- **控制/状态寄存器** STATUS / ERROR / WIRQT / RIRQT / IRQS / IRQEN / IRQP / CTRL：用于查询 FIFO 状态、配置中断阈值、应答中断、清空 FIFO。

共 10 个寄存器（`NoRegs = 10`）。每个寄存器占 `AxiDataWidth/8` 字节地址空间，从 `base_addr_i` 起线性排布。

#### 4.2.2 核心流程

寄存器访问的标准套路（两个 `addr_decode` + 两个 `spill_register`）：

1. AW/AR 地址各自经一份 `addr_decode` 译出寄存器下标 `w_reg_idx`/`r_reg_idx` 与命中标志 `dec_w_valid`/`dec_r_valid`。
2. 一个大 `always_comb` 块按下标做 `unique case`，决定本拍是否 push/pop FIFO、是否改写寄存器、回什么 B/R。
3. B/R 响应各经一级 `spill_register` 输出，既切断组合路径又满足 AXI「响应可延拍」。

地址映射规则在编译期由 generate 块生成，前闭后开，每段宽度为 `AxiDataWidth/8`。

#### 4.2.3 源码精读

寄存器枚举与数量定义见 [reg_e 枚举](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_mailbox.sv#L240-L260)：

```systemverilog
localparam int unsigned NoRegs = 32'd10;
typedef enum logic [3:0] {
  MBOXW=4'd0, MBOXR=4'd1, STATUS=4'd2, ERROR=4'd3,
  WIRQT=4'd4, RIRQT=4'd5, IRQS=4'd6, IRQEN=4'd7,
  IRQP=4'd8, CTRL=4'd9
} reg_e;
```

地址映射用 generate 自动铺出 10 条规则，每条 `[start_addr, end_addr)` 跨度恰为 `AxiDataWidth/8`（[L269-L276](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_mailbox.sv#L269-L276)）：

```systemverilog
assign addr_map[i] = '{
  idx:        i,
  start_addr: base_addr_i +  i      * (AxiDataWidth / 8),
  end_addr:   base_addr_i + (i + 1) * (AxiDataWidth / 8),
  default:    '0
};
```

读通路的 `unique case` 处理每个寄存器的读语义。最值得关注的是 MBOXR（[L371-L381](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_mailbox.sv#L371-L381)）：FIFO 非空则弹出并回 `OKAY`，空则回固定数据 `0xFEEDDEAD` 加 `SLVERR` 并置读错误位：

```systemverilog
MBOXR: begin
  if (!mbox_r_empty_i) begin
    r_chan       = '{data: data_t'(mbox_r_data_i), resp: axi_pkg::RESP_OKAY};
    mbox_r_pop_o = 1'b1;
  end else begin
    r_chan      = '{data: data_t'(32'hFEEDDEAD), resp: axi_pkg::RESP_SLVERR};
    error_d[0]  = 1'b1;   irqs_d[2] = 1'b1;   update_regs = 1'b1;
  end
end
```

注意 MBOXW 虽然是「只写」寄存器，但读它返回固定幻数 `0xFEEDC0DE`（[L370](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_mailbox.sv#L370)），方便软件探测寄存器是否存在。

写通路里 MBOXW 对称地处理 FIFO 满（[L416-L426](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_mailbox.sv#L416-L426)）：非满则 push 并回 `OKAY`，满则回 `SLVERR` 并置写错误位 `error_d[1]`。写数据按 `strb` 逐字节选通（[L302-L304](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_mailbox.sv#L302-L304)）。

B/R 响应各经一级 spill_register 输出（[L506-L543](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_mailbox.sv#L506-L543)），这正是注释里强调的「没有这级 spill，B 通道会违反 AXI stable 要求」——因为 `b_ready` 来自内部寄存器而非直接透传主端 ready（[L485-L486](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_mailbox.sv#L485-L486) 注释）。

#### 4.2.4 代码实践

**实践目标**：通过测试台断言确认每个寄存器的复位默认值。

**操作步骤**：

1. 打开 [tb_axi_lite_mailbox.sv 的初始读寄存器段](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_lite_mailbox.sv#L118-L171)。这是复位后端口 0 依次读 10 个寄存器并断言期望值的「黄金序列」。
2. 对照 [doc/axi_lite_mailbox.md 的寄存器表](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/doc/axi_lite_mailbox.md#L44-L55)，逐条核对断言里的期望值与文档「Default Value」列是否一致。

**需要观察的现象**：复位后第一次读 MBOXR 会触发一次「读空」错误——所以紧接着读 STATUS 得到 `1`（Empty=1）、读 ERROR 得到 `1`（Read Error=1）、读 IRQS 得到 `3'b100`（EIRQ 已被置位）。

**预期结果**：测试台在 [L125-L127](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_lite_mailbox.sv#L125-L127) 断言读 MBOXR 返回 `0xFEEDDEAD` 且 resp 为 `SLVERR`；在 [L151](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_lite_mailbox.sv#L151) 断言 IRQS 为 `3'b100`。这一连串断言证明「读空 FIFO」会级联地置 ERROR 与 EIRQ。若本地有 vsim，可执行 `make sim-axi_lite_mailbox.log` 观察日志末尾 `Errors: 0,`；若无法运行，明确标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 MBOXR 是「只读」、MBOXW 是「只写」？如果允许写 MBOXR 会怎样？

**答案**：MBOXR 对应本端口「从对端收到的」那条 FIFO 的读出口，读一次就弹出一拍；MBOXW 对应「发往对端」那条 FIFO 的写入口。两者物理上分属不同 FIFO、方向相反。若允许写 MBOXR，就等于往收件箱里伪造对端消息，破坏了「消息只能由对端发出」的语义，所以设计上禁止。

**练习 2**：访问一个未落入任何规则区间的地址（例如测试台里的 `16'hDEAD`），返回什么？

**答案**：`addr_decode` 的 `dec_valid` 为 0，`unique case` 不命中任何分支，B/R 保持默认的 `RESP_SLVERR`（见 [L319](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_mailbox.sv#L319) 与 [L322](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_mailbox.sv#L322) 的默认赋值）。测试台在 [L302-L305](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_lite_mailbox.sv#L302-L305) 正是据此断言未映射读写都回 `SLVERR`。

---

### 4.3 中断体系：阈值触发、sticky 状态、使能与挂起

#### 4.3.1 概念说明

邮箱最实用的能力是「有消息时主动通知 CPU」，而不是让 CPU 不停轮询。`axi_lite_mailbox` 用一组寄存器构建了一条完整的中断链：

- **阈值寄存器** WIRQT/RIRQT：设定「FIFO 填充深度超过多少就触发」。
- **状态位** STATUS[3:2]：实时反映读/写 FIFO 是否已超阈值。
- **sticky 状态寄存器** IRQS：一旦触发条件成立，对应位被置 1 并**保持**，直到软件显式应答；即使中断未使能也会被记录。
- **使能寄存器** IRQEN：决定哪些中断源真正能输出。
- **挂起寄存器** IRQP = IRQS & IRQEN：真正「待处理」的中断。
- **输出** `irq_o`：IRQP 各位的或，可配电平/边沿、高/低有效。

#### 4.3.2 核心流程

中断从产生到输出的链路：

1. 每拍比较 FIFO 深度与阈值：`read_usage > RIRQT` → STATUS[3]；`write_usage > WIRQT` → STATUS[2]。
2. 若对应 IRQS 位尚未置位且 STATUS 对应位为 1，则把 IRQS 位置 1（[L344-L352](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_mailbox.sv#L344-L352)）——这就是 sticky「锁存」。
3. IRQP = IRQS & IRQEN；`slv_irq = |IRQP`。
4. 顶层按 `IrqEdgeTrig`/`IrqActHigh` 把 `slv_irq` 转成最终 `irq_o`：电平模式直接跟随，边沿模式仅在「新中断上升沿」输出一拍脉冲。
5. 软件处理完后写 IRQS 的对应位 `1` 应答，sticky 位清零；边沿模式下应答还会通过 `clear_irq` 复位边沿寄存器。

阈值比较的数学关系（usage 已把 `full` 拼到最高位）：

\[
\text{STATUS}[3] = (\text{read\_usage} > \text{RIRQT}),\qquad \text{STATUS}[2] = (\text{write\_usage} > \text{WIRQT})
\]

由于阈值写入时被钳位到 `MailboxDepth-1`，而 FIFO 满 时 usage 取最大值（含 full 位），故「FIFO 满」必然满足 `usage > MailboxDepth-1`，保证满时中断一定能触发。

#### 4.3.3 源码精读

状态位的组合赋值（[L307-L313](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_mailbox.sv#L307-L313)）一目了然：

```systemverilog
assign status_q = { mbox_r_usage_i > usage_t'(rirqt_q),  // [3] RFIFOL 读 FIFO 超阈值
                    mbox_w_usage_i > usage_t'(wirqt_q),  // [2] WFIFOL 写 FIFO 超阈值
                    mbox_w_full_i,                        // [1] Full
                    mbox_r_empty_i };                     // [0] Empty
assign irqp_q   = irqs_q & irqen_q;     // 挂起 = 状态 AND 使能
assign irq_o    = |irqp_q;              // 本端口电平中断
```

注意 `status_q` 是「即时」的（纯组合），而 `irqs_q` 是「sticky」的（寄存器），二者职责不同。

sticky 锁存在 `always_comb` 顶端（[L344-L352](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_mailbox.sv#L344-L352)），用 `!irqs_q[x] && status_q[x+?]` 做「只在未置位时置位」，保证只锁存一次跳变：

```systemverilog
if (!irqs_q[1] && status_q[3]) begin irqs_d[1] = 1'b1; update_regs = 1'b1; end  // 读阈值
if (!irqs_q[0] && status_q[2]) begin irqs_d[0] = 1'b1; update_regs = 1'b1; end  // 写阈值
```

阈值写入带钳位（WIRQT，[L430-L440](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_mailbox.sv#L430-L440)）：写入值 ≥ `MailboxDepth` 则截到 `MailboxDepth-1`，确保满 FIFO 一定能触发：

```systemverilog
if (wirqt_d >= data_t'(MailboxDepth)) begin
  wirqt_d = data_t'(MailboxDepth) - data_t'(32'd1); // Threshold to maximal value
end
```

应答（写 IRQS）把对应 sticky 位清零并发出 `clear_irq`（[L452-L464](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_mailbox.sv#L452-L464)）：写 `1` 应答、写 `0` 保持，巧妙地用「数据位为 1 才清」实现「按位选择性应答」。

顶层的中断模式转换是一个 generate 循环（[L164-L188](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_mailbox.sv#L164-L188)）。电平模式极简：

```systemverilog
end else begin : gen_irq_level
  assign irq_o[i] = (IrqActHigh) ? slv_irq[i] : ~slv_irq[i];
end
```

边沿模式则用一个 `irq_q` 寄存器锁存「已见过新中断」，仅在上升沿输出一拍脉冲，`clear_irq` 复位之（[L165-L184](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_mailbox.sv#L165-L184)）。

#### 4.3.4 代码实践

**实践目标**：复现测试台里「端口 0 写消息 → 端口 0 收到读阈值中断」的场景，验证中断在邮箱非空时拉高。

**操作步骤**：

1. 阅读 [tb_axi_lite_mailbox.sv 端口 0 的中断段](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_lite_mailbox.sv#L228-L245)：先把 RIRQT 设为 2、使能 RTIRQ（写 IRQEN=2），然后向 MBOXW 写 `0xFEEDFEED`，最后 `wait(irq[0])`。
2. 思考：端口 0 写 MBOXW 把数据推进了 `i_mbox_0_to_1`（对端 1 的收件箱），但端口 0 自己的中断凭什么会拉高？提示：端口 0 的**读** FIFO 是 `i_mbox_1_to_0`，这条链路由端口 1 的回写（[L381-L386](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_lite_mailbox.sv#L381-L386)）填充——端口 1 收到消息后循环回写，填满端口 0 的读 FIFO 到 RIRQT，于是端口 0 的 RTIRQ 触发。
3. 若本地有仿真器，跑 `make sim-axi_lite_mailbox.log`，关注日志里两条 `Recieved interrupt from slave port 0/1` 的 `$info`（来自 [L409-L421](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_lite_mailbox.sv#L409-L421)）。

**需要观察的现象**：`irq[0]` 在端口 0 的读 FIFO 深度超过 RIRQT(=2) 时拉高；读 IRQP 得到 `3'b010`（RTIRQ pending）；读 STATUS 得到 `4'b1000`（RFIFOL=1）。

**预期结果**：测试台在 [L243-L248](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_lite_mailbox.sv#L243-L248) 正是断言这两点。随后端口 0 在 [L250-L256](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_lite_mailbox.sv#L250-L256) 循环读 MBOXR 直到 STATUS[0]（Empty）为真，把读 FIFO 排空。无法本地运行时标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：IRQS 是 sticky 的，IRQP 是组合的。如果软件只读 IRQP 而不读/写 IRQS，中断清得掉吗？

**答案**：清不掉。IRQP = IRQS & IRQEN 是纯组合读视图，读它不会改变 IRQS。只有**写 IRQS** 的对应位为 `1` 才能把 sticky 位清零（边沿模式还会顺带复位边沿寄存器）。所以正确的中断处理流程是：读 IRQP 看哪个 pending → 处理 → 写 IRQS 应答。

**练习 2**：把 WIRQT 写成 `100`（大于 `MailboxDepth=16`），读回来是多少？为什么？

**答案**：读回来是 `MailboxDepth-1 = 15`。因为写通路做了钳位（[L434-L437](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_mailbox.sv#L434-L437)），任何 ≥ Depth 的值都被截到 Depth-1，以保证 FIFO 满时（usage 含 full 位取最大）阈值中断必然触发。测试台在 [L205-L211](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_lite_mailbox.sv#L205-L211) 正好断言了这一点。

---

### 4.4 flush、错误恢复与测试台实战

#### 4.4.1 概念说明

邮箱还需要两个「运维」能力：

- **flush（清空 FIFO）**：软件可随时把任一方向的 FIFO 清空重来。本模块让任一端口都能触发清空——因为两条 FIFO 分属两个端口，flush 信号在顶层取或。
- **错误中断**：读空 / 写满是正常运维中的常见情况，模块用 ERROR 寄存器记录、并可配置成中断（EIRQ），让 CPU 在出错时也能被通知，而不必轮询。

测试台 `tb_axi_lite_mailbox` 用两个随机 Lite 主端，把上述所有功能串成一条定向脚本，是本模块的事实验证基线。

#### 4.4.2 核心流程

flush 的实现链：

1. 软件写 CTRL 的 bit[1]（flush 读 FIFO）/ bit[0]（flush 写 FIFO）。
2. 从端把它们输出为 `mbox_r_flush_o`/`mbox_w_flush_o`。
3. 顶层把同一条 FIFO 两端的 flush 信号取或，接 `fifo_v3.flush_i`——任一端口请求都能清空。
4. 读 CTRL 回读时返回的是 flush 信号本身（组合），写一拍后 FIFO 即空。

错误处理链：读空置 `error_d[0]`、写满置 `error_d[1]`，同时都置 `irqs_d[2]`（EIRQ）；ERROR 寄存器读时自清（[L384-L388](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_mailbox.sv#L384-L388)）。

#### 4.4.3 源码精读

CTRL 写处理（[L473-L479](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_mailbox.sv#L473-L479)）只认 byte 0 的 strb，把两位数据分别送到读/写 FIFO 的 flush 输出：

```systemverilog
CTRL: begin
  if (slv_req_i.w.strb[0]) begin
    mbox_r_flush_o = slv_req_i.w.data[1]; // Flush read  FIFO
    mbox_w_flush_o = slv_req_i.w.data[0]; // Flush write FIFO
  end
  b_chan = '{resp: axi_pkg::RESP_OKAY};
end
```

这两条 flush 在顶层取或（[L133](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_mailbox.sv#L133) 与 [L153](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_mailbox.sv#L153)）：`w_mbox_flush[0] | r_mbox_flush[1]`，对 `i_mbox_0_to_1` 而言，端口 0 的写 flush 与端口 1 的读 flush 任一为真都清空它——因为这条 FIFO 同时被这两个端口共享。

ERROR 读时自清（[L384-L388](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_mailbox.sv#L384-L388)）：返回当前错误值的同时 `error_d = '0`，符合「读即清」的常规错误寄存器惯例。

测试台的停止判定见 [proc_stop_sim](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_lite_mailbox.sv#L423-L434)：等两个主端都拉高 `end_of_sim`，统计 `test_failed` 数组，全 0 才 `$info(... Success)`，否则 `$fatal`。这与全库「以日志内容判成败」的约定一致（见 u1-l4）。DUT 例化用接口外壳 `axi_lite_mailbox_intf`，两个主端分别接 `master[0]`/`master[1]`，`base_addr_i` 设为全零（[L450-L463](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_lite_mailbox.sv#L450-L463)）。

#### 4.4.4 代码实践

**实践目标**：跑通官方测试台，理解一次完整的「端口 0 → 端口 1」投递与回写。

**操作步骤**：

1. 阅读 [端口 1 主端脚本](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_lite_mailbox.sv#L328-L407)：端口 1 先 flush、设 RIRQT=0（任何数据即触发）、使能 RTIRQ，然后 `wait(irq[1])`；收到中断后读 IRQP/IRQS 确认是 RTIRQ，读 MBOXR 取出端口 0 写入的 `0xFEEDFEED`（[L369-L371](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_lite_mailbox.sv#L369-L371) 断言），再循环回写一批数据触发端口 0 的中断。
2. 若本地装了 Questasim/vsim，在仓库根执行（沿用 u1-l4 的 Makefile 约定）：

   ```bash
   make sim-axi_lite_mailbox.log
   ```

3. 在生成的日志末尾查找 `Errors: 0,` 与 `Success` 字样。

**需要观察的现象**：日志应出现两次 `Recieved interrupt from slave port 0/1`（[L411/L418](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_lite_mailbox.sv#L409-L421)），最后打印 `Slave port 0 failed tests: 0` / `Slave port 1 failed tests: 0` 与 `Success`。

**预期结果**：仿真以 `Errors: 0` 通过，证明两个主端经邮箱完成了双向投递、阈值中断、错误中断、flush 全部行为。如果本地没有 EDA 工具链，明确写「待本地验证」，并以上述源码断言作为预期依据。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `i_mbox_0_to_1` 的 flush 是 `w_mbox_flush[0] | r_mbox_flush[1]`，而不是只接端口 0 的 flush？

**答案**：因为这条 FIFO 被两个端口共享——端口 0 是它的写者，端口 1 是它的读者。任一端口都可能想清空它（写者想作废已发消息，或读者想丢弃不感兴趣的消息），所以 flush 取两端之或。只接端口 0 会让端口 1 无法清空自己的收件箱。

**练习 2**：ERROR 寄存器「读即清」。如果同一拍既读 ERROR（应清旧错）又因别的原因产生新错，会发生什么？

**答案**：源码注释（[L357-L361](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_mailbox.sv#L357-L361)）专门说明：读与写逻辑放在同一个 `always_comb` 里，正是为处理这种竞争——此时错误**不会**被清除，而是保留并继续触发中断。这避免了「读清」与「新生错误」同拍冲突导致错误丢失。

---

## 5. 综合实践

把本讲四个最小模块串起来，设计并（在纸面上）实现一次完整的核间通知：

**场景**：核 A（接端口 0）要通知核 B（接端口 1）「数据已就绪」，核 B 收到后回一个 ACK。

**要求你完成**：

1. **规划寄存器操作序列**（两端各写一份）：
   - 核 A：设 WIRQT、使能 WTIRQ、向 MBOXW 写一个 32 位 token、`wait(irq)`、读 IRQP 确认、应答 IRQS。
   - 核 B：设 RIRQT=0（任何数据即触发）、使能 RTIRQ、`wait(irq)`、读 MBOXR 取 token、向 MBOXW 回写 ACK、应答 IRQS。
2. **画出数据通路**：标出 token 从核 A 的 MBOXW 写，经哪条 FIFO、被核 B 的 MBOXR 读；ACK 又经哪条 FIFO 回到核 A。
3. **指出错误处理**：若核 B 还没读、核 A 又连续写直到 `i_mbox_0_to_1` 写满，会出现什么 RESP？哪个 ERROR 位置位？如何用 CTRL 恢复？

**参考要点**：

- token 经 `i_mbox_0_to_1`（端口 0 写 → 端口 1 读）；ACK 经 `i_mbox_1_to_0`（端口 1 写 → 端口 0 读）。两条 FIFO 独立、可同时进行。
- 写满时 MBOXW 写回 `RESP_SLVERR`、`error_d[1]` 置位、`irqs_d[2]`（EIRQ）置位；软件可写 CTRL 的 bit[0] flush 写 FIFO，再读 ERROR 清错误位。
- 这套流程与 [tb_axi_lite_mailbox.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_lite_mailbox.sv) 里端口 0/1 的脚本高度同构，可作为自检参照。

完成后再尝试一个进阶改动：把 DUT 的 `IRQ_EDGE_TRIG` 从 `1'b0` 改为 `1'b1`（在测试台 [L452](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_lite_mailbox.sv#L452) 处），预测 `wait(irq[1])` 的行为会不会变化，并说明边沿模式下应答 IRQS 为何额外重要（提示：`clear_irq` 复位 `irq_q`）。

## 6. 本讲小结

- `axi_lite_mailbox` 是对称双端口邮箱：两个 AXI4-Lite 从端口之间靠**两条单向 FIFO**（`i_mbox_0_to_1`、`i_mbox_1_to_0`）互联，任一端口写下的数据只能被对端读出。
- 每个端口背后是一个 `axi_lite_mailbox_slave`，含 10 个寄存器（MBOXW/MBOXR/STATUS/ERROR/WIRQT/RIRQT/IRQS/IRQEN/IRQP/CTRL），地址按 `AxiDataWidth/8` 线性排布，由 generate 自动生成 `addr_decode` 规则表。
- 数据寄存器直连 FIFO：写 MBOXW push（满回 `SLVERR` + 写错误位），读 MBOXR pop（空回 `0xFEEDDEAD` + `SLVERR` + 读错误位）；B/R 各经一级 `spill_register` 输出以满足 AXI stable 要求。
- 中断是一条链：阈值比较 → STATUS → sticky 的 IRQS（锁存一次）→ IRQEN 使能 → IRQP（= IRQS & IRQEN）→ `|IRQP` 得电平中断 → 顶层按 `IrqEdgeTrig`/`IrqActHigh` 转电平或边沿输出；软件写 IRQS 应答。
- flush 与错误恢复是运维能力：CTRL 写触发两端 flush 取或清空 FIFO；ERROR 读即清，且读清与新生错误同拍时错误不丢失（读/写逻辑共用一个 `always_comb`）。
- `tb_axi_lite_mailbox` 用两个随机 Lite 主端把上述全部行为编成定向脚本，以 `test_failed` 计数与 `Errors: 0` 判成败，是本模块的事实验证基线。

## 7. 下一步学习建议

- **横向对比**：本模块的「双端口 + FIFO + 寄存器组」与 u12-l3 的 `axi_lite_regs`（单端口、纯寄存器映射）对照阅读，体会「带数据通路的从端」与「纯寄存器从端」在 `always_comb` 组织上的差异。
- **进入协议转换**：下一单元 U13 讲协议转换桥（`axi_to_axi_lite` / `axi_lite_to_apb`），它们会复用本讲巩固的 AXI-Lite 从端写法，建议先确认你能独立画出本模块的寄存器译码与 spill 响应结构。
- **源码延伸**：若对中断控制器模式感兴趣，可对照阅读 `axi_lite_mailbox` 的 IRQS/IRQEN/IRQP 三件套与通用中断聚合器（如 pulp-platform 的 `apb_interrupt_router`，不在本库）的设计异同。
