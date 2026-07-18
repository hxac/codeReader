# AXI4-Lite 与 Wishbone 适配

## 1. 本讲目标

本讲回答一个问题：**PicoRV32 的「原生内存接口」（`mem_valid`/`mem_ready`/`mem_wstrb`/…）只有一对握手线，怎么接进用 AXI4-Lite 或 Wishbone 这类工业总线的系统里？**

读完本讲，你应当能够：

1. 说清 `picorv32`、`picorv32_axi`、`picorv32_wb` 三个模块之间的分层关系——为什么后两者只是「薄包装」。
2. 读懂 `picorv32_axi_adapter` 如何用**组合逻辑 + 三个 ack 标志**（没有状态机）把原生接口桥接到 AXI4-Lite 的 AW/W/B/AR/R 五个通道。
3. 读懂 `picorv32_wb` 如何用一个**三状态 FSM**（IDLE/WBSTART/WBEND）适配 Wishbone 主接口，以及它如何处理「Wishbone 高有效复位 ↔ PicoRV32 低有效复位」的极性转换。
4. 画出原生接口到 AXI 五通道、到 Wishbone 信号的对应时序图，并讲清两种总线握手的本质差异。

本讲承接 [u5-l3 原生内存接口与传输状态机](u5-l3-memory-interface.md)。如果你已经理解了 `mem_valid`/`mem_ready` 握手、`mem_wstrb` 字节写使能和 `mem_state` 状态机，本讲只是把它们「翻译」成两种外部总线的语言。

## 2. 前置知识

### 三套总线握手速记

在进入源码前，先用一句话区分三种握手机制：

- **原生接口（native）**：一对线 `mem_valid`（主）↔ `mem_ready`（从）。同一拍两者都为高即「成交」。读/写用 `mem_wstrb` 区分（全 0 = 读）。这是 u5-l3 讲过的内容。
- **AXI4-Lite**：把一次事务拆成 **5 个独立通道**，每个通道自带一对 valid/ready：
  - 写地址 `AW`、写数据 `W`、写响应 `B`；
  - 读地址 `AR`、读数据 `R`。
  - 一次写 = 先在 AW/W 上把地址和数据送出去，再从 B 收一个响应；一次读 = 在 AR 送地址，从 R 收数据。
- **Wishbone（经典 B4）**：一组捆绑信号 + 两根握手线 `stb`/`cyc`（主）↔ `ack`（从）。主设备拉高 `cyc`（总线周期）和 `stb`（选通），从设备回 `ack` 即成交。读/写用 `we` 区分。

一个直觉性的对比：

| 总线 | 「成交」靠什么 | 一次写事务的通道数 | 是否拆分地址与数据 |
|---|---|---|---|
| 原生 | `valid & ready`（1 对） | 1 捆 | 否（同一线束） |
| AXI4-Lite | 每通道各自 `valid & ready` | 3（AW、W、B） | 是 |
| Wishbone | `cyc & stb` ↔ `ack` | 1 捆 | 否（同一线束） |

记住一句话：**AXI 把事务「拆开」以求灵活与吞吐，Wishbone 和原生一样把事务「捆在一起」求简单。**

### 复位极性

PicoRV32 核用**低有效**同步复位 `resetn`（`resetn=0` 表示在复位）。Wishbone 按惯例用**高有效** `wb_rst_i`（`wb_rst_i=1` 表示在复位）。AXI 版本则沿用 PicoRV32 自己的 `resetn`。这个极性差异是 `picorv32_wb` 里必须显式处理的。

## 3. 本讲源码地图

本讲全部代码集中在 `picorv32.v` 末尾与一个测试台：

| 文件 | 关键符号 | 作用 |
|---|---|---|
| `picorv32.v` | `picorv32_axi` | AXI4-Lite 版本的薄包装：转发参数、对外暴露 `mem_axi_*` 端口、内部实例化适配器与核 |
| `picorv32.v` | `picorv32_axi_adapter` | 真正的「翻译器」：原生接口 ↔ AXI4-Lite 五通道 |
| `picorv32.v` | `picorv32_wb` | Wishbone 版本：三状态 FSM 适配 + 复位极性转换 |
| `testbench_wb.v` | `picorv32_wrapper`、`wb_ram` | Wishbone 测试台：实例化 `picorv32_wb`，接一个带 UART 语义的 Wishbone 从 RAM |

永久链接 base（当前 HEAD）：

```
https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/
```

## 4. 核心概念与源码讲解

### 4.1 三种核的分层关系与 picorv32_axi 包装

#### 4.1.1 概念说明

README 明确指出核存在三种变体：

> The core exists in three variations: `picorv32`, `picorv32_axi` and `picorv32_wb`. The first provides a simple native memory interface… `picorv32_axi` provides an AXI-4 Lite Master interface… `picorv32_wb` provides a Wishbone master interface.

——见 [README.md:60-64](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/README.md#L60-L64)。

关键认知是：**CPU 内部逻辑只有一份**，就是 `picorv32`（原生核）。`picorv32_axi` 和 `picorv32_wb` **不是重新实现的 CPU**，而是包在 `picorv32` 外面的「总线翻译层」。它们把同一套 `parameter` 原样转发给内部的 `picorv32_core`，只是对外换了端口的「语言」。

README 还单独提到 `picorv32_axi_adapter` 的复用价值（[README.md:66-70](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/README.md#L66-L70)）：你可以拿它造自定义 SoC——核与核、核与本地 RAM/ROM 之间用简单的原生接口互连，只在「对外出口」处翻译成 AXI4。这解释了为什么适配器是独立模块而非内联。

#### 4.1.2 核心流程

`picorv32_axi` 的内部数据通路是一个「三明治」：

```
       外部 AXI4-Lite 端口 (mem_axi_*)
                ▲
        picorv32_axi_adapter   ← 翻译层
                ▲
        内部原生线 (mem_valid/ready/addr/wdata/wstrb/rdata)
                ▲
            picorv32_core       ← 唯一真正的 CPU
```

`picorv32_axi` 这个外壳模块做三件事：

1. **声明对外端口**：用 `mem_axi_awvalid/awaddr/…/rdata` 等 AXI4-Lite 信号替代原生 `mem_*` 端口。
2. **声明内部原生线**：一组 `wire`，把核与适配器连起来。
3. **实例化适配器和核**：适配器负责翻译，核负责计算。

#### 4.1.3 源码精读

`picorv32_axi` 的参数列表与原生核完全一致（[picorv32.v:2517-2542](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L2517-L2542)），这是「无缝替换」的前提——你综合时把 `picorv32` 换成 `picorv32_axi`，所有 `parameter` 都照样能用。

对外端口用 AXI4-Lite 五通道的命名（注意每通道都有 valid/ready 对，写地址通道还带 `awprot`、读地址通道带 `arprot`）：[picorv32.v:2547-2569](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L2547-L2569)，这段代码声明了 AW/W/B/AR/R 五组端口。

内部用一组 `wire` 重建原生接口：[picorv32.v:2611-2617](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L2611-L2617)：

```verilog
wire        mem_valid;
wire [31:0] mem_addr;
wire [31:0] mem_wdata;
wire [ 3:0] mem_wstrb;
wire        mem_instr;
wire        mem_ready;
wire [31:0] mem_rdata;
```

然后实例化适配器，把外部 AXI 端口与内部原生线对接（[picorv32.v:2619-2646](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L2619-L2646)）：左侧 `mem_axi_*` 接到模块对外端口，右侧 `mem_*` 接到内部线。最后实例化 `picorv32_core`，参数全量转发，原生 `mem_*` 端口接到同一组内部线（[picorv32.v:2648-2685](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L2648-L2685)）。

> 结论：`picorv32_axi` 自己一行逻辑都没有，纯粹是「端口改名 + 适配器挂载 + 核实例化」。

#### 4.1.4 代码实践

1. **目标**：确认「三种核共享同一份 CPU 逻辑」。
2. **步骤**：
   - 打开 `picorv32.v`，定位 `module picorv32_axi`（2517 行）和 `module picorv32_wb`（2815 行）。
   - 在两者内部搜索 `picorv32 #(`，确认它们都实例化了同一个 `picorv32` 核。
   - 对比 `picorv32_axi`（2648 行起）与 `picorv32_wb`（2912 行起）里 `picorv32_core` 的实例化代码，确认 parameter 转发清单完全相同。
3. **观察**：两个实例对 `picorv32_core` 的 `.mem_valid/.mem_addr/.mem_ready/…` 连接方式几乎一致，差别只在 `mem_ready`/`mem_rdata` 是 `wire` 还是 `reg`（AXI 版用线接适配器，WB 版用 reg 由 FSM 驱动）。
4. **预期结果**：你会看到 CPU 核的内部逻辑（译码、状态机、ALU）只存在一份；所谓「AXI 版/WB 版」只是外套。

#### 4.1.5 小练习与答案

**练习 1**：`picorv32_axi` 模块里，谁驱动 `mem_ready` 与 `mem_rdata`？

**答案**：由 `picorv32_axi_adapter` 驱动。它们在适配器里是 `output`，在核里是 `input`，外壳用 `wire` 把两者连起来（`mem_ready` 是 2616 行的 `wire`，适配器 2762/2766 行声明为 `output`）。

**练习 2**：为什么 README 说 `picorv32_axi_adapter` 可以「单独使用」来造自定义 SoC？

**答案**：因为它是独立的、可实例化的翻译模块——输入原生接口、输出 AXI4-Lite。你可以让多个 `picorv32` 核与本地 RAM/ROM 之间直接用原生接口互连，只在对外出口处放一个适配器翻译成 AXI4，不必把每个核都换成 `picorv32_axi`。

---

### 4.2 picorv32_axi_adapter：原生接口到 AXI4-Lite 五通道

#### 4.2.1 概念说明

这是本讲最精巧的一块。`picorv32_axi_adapter` 要把「一对线成交」的原生事务，拆/合到 AXI4-Lite 的五通道上。

挑战在于：**AXI 要求每个通道的 valid 必须保持到对端给 ready 为止**，而原生接口的 `mem_valid` 在整个事务期间一直为高。如果不做处理，AW/W/AR 通道会在「已经被接受」之后继续拉高 valid，被从设备误认为「又来一笔新事务」。

适配器的解法是：用三个一位寄存器 `ack_awvalid`、`ack_arvalid`、`ack_wvalid` 记录「这个通道本轮是否已被接受过」，一旦接受就把对应 valid 拉低（通过 `&& !ack_*`），从而**保证每个通道在一笔事务内只握手一次**。这是一个**没有显式状态机**、几乎全组合的精巧设计。

#### 4.2.2 核心流程

**读事务**（`mem_wstrb == 0`，即 load/取指）：

```
mem_valid=1, mem_wstrb=0
   └─> arvalid = 1 (直到 arready 把 ack_arvalid 置 1 后撤销)
       └─> 等从设备回 rvalid
           └─> mem_ready = rvalid = 1；mem_rdata = rdata；事务结束
```

**写事务**（`mem_wstrb != 0`，即 store）：

```
mem_valid=1, mem_wstrb!=0
   ├─> awvalid = 1 (直到 awready → ack_awvalid=1 后撤销)
   ├─> wvalid  = 1 (直到 wready  → ack_wvalid=1 后撤销)
   └─> 等从设备回 bvalid
       └─> mem_ready = bvalid = 1；事务结束
```

事务完成的判据是 `mem_ready = bvalid || rvalid`（写看 B 通道、读看 R 通道）。完成后，寄存的 `xfer_done`（以及 `mem_valid` 撤销）会在下一拍把三个 ack 标志清零，为下一笔事务做准备。

#### 4.2.3 源码精读

适配器端口分两组：上方 AXI4-Lite 五通道（[picorv32.v:2731-2756](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L2731-L2756)），下方原生接口（[picorv32.v:2758-2766](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L2758-L2766)）。注意方向：从适配器看，`mem_valid`/`mem_instr`/`mem_addr`/`mem_wdata`/`mem_wstrb` 是 **input**（核驱动），`mem_ready`/`mem_rdata` 是 **output**（适配器驱动）。

读/写路由靠 `|mem_wstrb`（归约或，非零即写）这一句组合逻辑区分（[picorv32.v:2773-2787](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L2773-L2787)）：

```verilog
// 写地址通道：仅写事务、且 AW 尚未被接受时拉高
assign mem_axi_awvalid = mem_valid && |mem_wstrb && !ack_awvalid;
assign mem_axi_awaddr  = mem_addr;
assign mem_axi_awprot  = 0;

// 读地址通道：仅读事务、且 AR 尚未被接受时拉高
assign mem_axi_arvalid = mem_valid && !mem_wstrb && !ack_arvalid;
assign mem_axi_araddr  = mem_addr;
assign mem_axi_arprot  = mem_instr ? 3'b100 : 3'b000;   // 取指打标
...
// 成交判据：写等 bvalid、读等 rvalid
assign mem_ready       = mem_axi_bvalid || mem_axi_rvalid;
assign mem_axi_bready  = mem_valid && |mem_wstrb;
assign mem_axi_rready  = mem_valid && !mem_wstrb;
assign mem_rdata       = mem_axi_rdata;
```

几个要点：

- **`mem_instr ? 3'b100`**：AXI 的 `AxProt[2]` 是「指令访问」属性位。适配器把取指事务（`mem_instr=1`）打上 `arprot=0b100`，让有能力的从设备（如带缓存的互联）能区分取指与数据读。写通道的 `awprot` 恒为 0（store 不可能是取指）。
- **地址共用 `mem_addr`**：AW 与 AR 都直接输出 `mem_addr`，因为同一时刻要么读要么写（由 `|mem_wstrb` 互斥）。

ack 标志的维护是一个小型时序逻辑（[picorv32.v:2790-2807](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L2790-L2807)）：

```verilog
always @(posedge clk) begin
    if (!resetn) begin
        ack_awvalid <= 0;
    end else begin
        xfer_done <= mem_valid && mem_ready;        // 寄存一拍「本次完成」
        if (mem_axi_awready && mem_axi_awvalid) ack_awvalid <= 1;  // AW 已收
        if (mem_axi_arready && mem_axi_arvalid) ack_arvalid <= 1;  // AR 已收
        if (mem_axi_wready  && mem_axi_wvalid ) ack_wvalid  <= 1;  // W  已收
        if (xfer_done || !mem_valid) begin          // 事务完成或核撤销请求
            ack_awvalid <= 0; ack_arvalid <= 0; ack_wvalid <= 0;
        end
    end
end
```

注意一个细节：复位分支里只显式清了 `ack_awvalid`，`ack_arvalid`/`ack_wvalid`/`xfer_done` 没有进复位清单。它们靠 `xfer_done || !mem_valid` 这条路在正常运行中被清掉（核在复位释放后 `mem_valid` 会是确定值）。这是源码的一个真实小取舍，仿真里一般不影响功能。

#### 4.2.4 代码实践

1. **目标**：手动推演一次 AXI4-Lite 写事务的逐拍信号。
2. **步骤**：
   - 设想 CPU 发起一次 `sw`：`mem_valid=1`，`mem_wstrb=4'b1111`，`mem_addr=0x100`，`mem_wdata=0xDEADBEEF`。
   - 假设 AXI 从设备「立刻就绪」：`awready=wready=1` 且同拍回 `bvalid=1`。
   - 对照 [picorv32.v:2773-2787](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L2773-L2787) 写出第 0 拍：`awvalid=1`、`wvalid=1`、`awaddr=0x100`、`wdata=0xDEADBEEF`、`bready=1`、`mem_ready=bvalid=1`。
   - 再对照 [picorv32.v:2790-2807](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L2790-L2807) 写出第 1 拍：`ack_awvalid/ack_wvalid` 被置 1，`xfer_done` 被置 1；因为核看到 `mem_ready` 会撤销 `mem_valid`，于是 `!mem_valid` 成立，ack 被清零。
3. **观察**：第 0 拍 `awvalid` 和 `wvalid` 同时拉高——这是 AXI4-Lite 允许的（AW、W 可同周期给出）；`arvalid` 因 `|mem_wstrb` 非零而恒为 0。
4. **预期结果**：整个写事务在「从设备零延迟」假设下**单拍成交**——这正是适配器全组合设计的性能优势（实际是否单拍取决于从设备的 ready 何时给）。
5. 待本地验证：若想看真实波形，运行 `make test_vcd` 后用 GTKWave 观察 `testbench_axi`/wrapper 内 `mem_axi_*` 与内部 `mem_*` 的对应关系。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `mem_axi_awvalid` 的表达式里要有 `&& !ack_awvalid`？去掉它会怎样？

**答案**：因为 AXI 要求 valid 保持到 ready，但一旦 `awready && awvalid` 成立，本通道的地址已被从设备采走；若继续拉高 `awvalid`，从设备会以为「又来一个写地址」。`!ack_awvalid` 保证每笔事务里 AW 通道只握手一次。去掉它会导致同一笔 store 被从设备误判为多次写。

**练习 2**：一次 AXI4-Lite 读事务里，`mem_rdata` 在哪一拍对核有效？

**答案**：在 `mem_axi_rvalid=1` 的那一拍。因为 `mem_ready = mem_axi_rvalid` 且 `mem_rdata = mem_axi_rdata`（直连），核正是在 `mem_valid && mem_ready` 同时为高的那一拍采 `mem_rdata`。

---

### 4.3 picorv32_wb：Wishbone 主接口适配

#### 4.3.1 概念说明

`picorv32_wb` 与 `picorv32_axi` 同为「薄包装」，但 Wishbone 的握手风格更接近原生接口（事务捆绑、单 ack），所以它没有像 AXI 那样拆成五个通道，而是用一个**三状态有限状态机**把原生请求转成 Wishbone 的 `cyc`/`stb`/`ack` 节拍。

它还顺手解决了一个工程细节：**复位极性转换**。PicoRV32 核要低有效 `resetn`，而 Wishbone 按惯例给高有效 `wb_rst_i`，于是 `picorv32_wb` 内部做了一次取反。

#### 4.3.2 核心流程

`picorv32_wb` 内部状态机有三个状态（[picorv32.v:2989-2991](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L2989-L2991)）：

```
            mem_valid=1
   IDLE ───────────────► WBSTART ──ack_i──► WBEND ──► IDLE
   (空)   锁存 adr/dat/   等从设备 ack      撤销 mem_ready
          we/sel,拉 cyc&stb 捕获 rdata,
                            拉高 mem_ready
```

- **IDLE**：看到核的 `mem_valid`，就把地址/数据/`we`/`sel` 锁存到 Wishbone 输出寄存器，拉高 `cyc_o` 与 `stb_o`，进入 `WBSTART`。
- **WBSTART**：等从设备的 `ack_i`。`ack_i` 一到，把 `dat_i`（从设备读回的数据）存入 `mem_rdata`、拉高 `mem_ready`、撤销 `cyc/stb/we`，进入 `WBEND`。
- **WBEND**：撤销 `mem_ready`，回到 `IDLE`。

注意 `mem_ready` 只在 `WBSTART` 命中 `ack_i` 的那一拍为高（`WBEND` 立刻清零），给核一个一拍宽的完成脉冲。`we` 由 `|mem_wstrb` 推导（[picorv32.v:2995-2996](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L2995-L2996)）：任意字节使能位为 1 即为写。

#### 4.3.3 源码精读

端口侧，`picorv32_wb` 暴露标准 Wishbone 主信号 `wbm_*`，外加 `wb_clk_i` 与高有效 `wb_rst_i`（[picorv32.v:2844-2855](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L2844-L2855)）。注意命名遵循 Wishbone 经典后缀：`_o` 主设备输出、`_i` 主设备输入、`m`（master）。

复位极性转换在这一行（[picorv32.v:2909-2910](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L2909-L2910)）：

```verilog
assign clk    = wb_clk_i;
assign resetn = ~wb_rst_i;   // 高有效 → 低有效
```

`mem_ready` 与 `mem_rdata` 这里是 `reg`（由 FSM 驱动），不像 AXI 版那样是适配器输出的 `wire`（[picorv32.v:2899-2904](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L2899-L2904)）。

状态机本体（[picorv32.v:2998-3048](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L2998-L3048)），核心片段：

```verilog
IDLE: begin
    if (mem_valid) begin
        wbm_adr_o <= mem_addr;     // 锁存地址
        wbm_dat_o <= mem_wdata;    // 锁存写数据
        wbm_we_o  <= we;           // 写使能
        wbm_sel_o <= mem_wstrb;    // 字节选择直通
        wbm_stb_o <= 1'b1;         // 选通
        wbm_cyc_o <= 1'b1;         // 总线周期
        state <= WBSTART;
    end else begin
        mem_ready <= 1'b0; wbm_stb_o <= 0; wbm_cyc_o <= 0; wbm_we_o <= 0;
    end
end
WBSTART: begin
    if (wbm_ack_i) begin           // 从设备应答
        mem_rdata <= wbm_dat_i;    // 采样读数据
        mem_ready <= 1'b1;         // 通知核「成交」
        state <= WBEND;
        wbm_stb_o <= 0; wbm_cyc_o <= 0; wbm_we_o <= 0;
    end
end
WBEND: begin
    mem_ready <= 1'b0;
    state <= IDLE;
end
```

> 注意：`wbm_sel_o` 直接接到 `mem_wstrb`，所以 Wishbone 的字节选择信号与原生接口的字节写使能语义一致（4 位，每位对应一字节）。但 Wishbone 的 `we_o` 是一位的「读/写」总开关，由 `|mem_wstrb` 归约而来——这是两种接口的语义差：原生用「`wstrb` 全 0 表示读」，Wishbone 用独立的 `we` 位。

#### 4.3.4 代码实践

1. **目标**：在 Wishbone 测试台里观察一次真实事务的 `cyc`/`stb`/`ack` 节拍。
2. **步骤**：
   - 编译并运行 Wishbone 测试台：`make test_wb`（它用 `testbench_wb.v` 实例化 `picorv32_wb`，见依赖说明）。
   - 若要波形：`make test_wb_vcd`，生成 `testbench.vcd` 后用 GTKWave 查看 `top.uut`（`picorv32_wb`）的 `wbm_cyc_o`/`wbm_stb_o`/`wbm_ack_i`/`state` 以及内部 `mem_valid`/`mem_ready`。
3. **观察**：每笔事务里 `cyc_o` 与 `stb_o` 同时拉高并保持到 `ack_i` 出现；`ack_i` 出现的那拍 `mem_ready` 被拉高一拍；状态在 `IDLE→WBSTART→WBEND→IDLE` 间循环。
4. **预期结果**：可见 Wishbone 是「捆绑式」握手——一组信号（adr/dat/we/sel）与 `cyc/stb` 一起给，从设备用单根 `ack` 应答，远比 AXI 五通道直观。
5. 待本地验证：若没有 Icarus Verilog，可只做源码阅读型追踪——沿 `state` 三个 case 画出状态图，标注每态对 `mem_ready` 的赋值。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `picorv32_wb` 需要 `WBEND` 这个第三状态？只用 IDLE/WBSTART 两态行不行？

**答案**：为了给核一个**确定为一拍宽**的 `mem_ready` 脉冲。若在 `WBSTART` 命中 `ack_i` 后直接回 `IDLE`，`IDLE` 分支在 `mem_valid` 仍为高时会立刻又拉起 `cyc/stb` 开始下一笔——但此刻核可能还没来得及采完 `mem_rdata`/撤销 `mem_valid`。`WBEND` 强制空一拍（`mem_ready<=0`），保证事务之间有干净的分界。

**练习 2**：`picorv32_wb` 的 `resetn`（给内部核用）与对外端口 `wb_rst_i` 是什么关系？

**答案**：互为取反。`assign resetn = ~wb_rst_i;`（[picorv32.v:2910](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L2910)）。对外遵循 Wishbone 高有效复位惯例，对内满足 PicoRV32 低有效 `resetn` 要求。

---

### 4.4 AXI4-Lite 与 Wishbone 握手时序对比

#### 4.4.1 概念说明

这一节把三种接口并排放，回答「**同一笔内存事务，三种总线分别长什么样**」。理解差异后，你就能根据目标系统的总线标准选择正确的 PicoRV32 变体，也能解释为什么两种适配器的实现风格截然不同（AXI 全组合 + ack 标志，Wishbone 三状态 FSM）。

#### 4.4.2 核心流程

下表把同一笔「向地址 A 写一个字 D」与「从地址 A 读一个字」在三种接口下的信号对齐：

| 维度 | 原生 `picorv32` | `picorv32_axi`（AXI4-Lite） | `picorv32_wb`（Wishbone） |
|---|---|---|---|
| 成交判据 | `mem_valid & mem_ready` | 每通道各自 `valid & ready`；整体完成看 `bvalid`(写)/`rvalid`(读) | `cyc & stb` ↔ `ack_i` |
| 读/写区分 | `mem_wstrb`（0=读） | 走哪组通道：AR/R=读，AW/W/B=写 | 独立的 `we_o` 位 |
| 地址载体 | `mem_addr` | `awaddr`/`araddr`（地址通道） | `wbm_adr_o` |
| 数据载体 | `mem_wdata`/`mem_rdata` | `wdata`(W) / `rdata`(R) | `wbm_dat_o`/`wbm_dat_i` |
| 字节使能 | `mem_wstrb`（4 位） | `wstrb`（W 通道，4 位） | `wbm_sel_o`（4 位） |
| 适配实现 | 核内 `mem_state` 四状态机 | **组合逻辑 + 3 个 ack 标志**，无 FSM | **三状态 FSM**（IDLE/WBSTART/WBEND） |
| 复位极性 | `resetn`（低有效） | `resetn`（低有效） | `wb_rst_i`（高有效，内部取反） |

读事务的信号对应（同一拍视角）：

```
原生：  mem_valid=1, mem_wstrb=0, mem_addr=A  ──►  (等) mem_ready=1, mem_rdata=D
AXI：   arvalid=1, araddr=A   ──►  (等 rready) rvalid=1, rdata=D
WB：    cyc=1, stb=1, adr=A, we=0  ──►  (等) ack_i=1, dat_i=D
```

写事务的信号对应：

```
原生：  mem_valid=1, mem_wstrb=1111, mem_addr=A, mem_wdata=D ─► mem_ready=1
AXI：   awvalid=1, awaddr=A; wvalid=1, wdata=D, wstrb=1111 ─► bvalid=1 (B 通道)
WB：    cyc=1, stb=1, adr=A, we=1, dat_o=D, sel_o=1111     ─► ack_i=1
```

#### 4.4.3 源码精读：两种适配风格的根因

为什么 AXI 适配器**不用状态机**，而 Wishbone 适配器**用状态机**？根因在两种总线的「完成信号」语义：

- **AXI4-Lite** 的 `bvalid`/`rvalid` 是**电平型**完成信号——从设备拉高一拍即代表「数据/响应在此」。适配器只需 `assign mem_ready = bvalid || rvalid` 就能把这拍直接转告给核，所以全组合即可，仅用 ack 标志解决「valid 保持」问题（[picorv32.v:2785](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L2785)）。
- **Wishbone** 的 `ack_i` 在经典从设备里常常是**寄存输出**（例如 `testbench_wb.v` 里 `wb_ram` 的 `wb_ack_o <= valid & !wb_ack_o`，见 [testbench_wb.v:222](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/testbench_wb.v#L222)），且 `cyc`/`stb` 需要按节拍拉高与撤销。适配器必须用状态机显式控制「拉起 cyc/stb → 等 ack → 撤销 → 给核 mem_ready 脉冲」的顺序（[picorv32.v:3008-3046](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L3008-L3046)）。

另一种说法：AXI 适配器把「时序」外包给了 AXI 协议自身的每通道握手（适配器只做组合翻译），Wishbone 适配器则要自己制造 `cyc`/`stb` 的时序节拍，所以需要状态机。

#### 4.4.4 代码实践：把两种总线跑起来对比

1. **目标**：用现成 Makefile 目标分别跑 AXI 与 Wishbone 版本，确认两者跑同一份固件、结果一致。
2. **步骤**：
   - 跑 AXI 版：`make test_axi`（复用 `testbench.vvp`，加 `+axi_test` plusarg；其内部经 `picorv32_wrapper` 实例化 `picorv32_axi`）。
   - 跑 Wishbone 版：`make test_wb`（用 `testbench_wb.vvp`，实例化 `picorv32_wb`）。
   - 两者加载的都是 `firmware/firmware.hex`，最终都应打印固件输出并以 `ALL TESTS PASSED.` 结束。
3. **观察**：两套输出的「业务结果」（打印的字符、PASS/FAIL）完全相同——证明三种总线变体在功能上等价，差异只在接口时序。
4. **预期结果**：两份日志末尾都出现 `ALL TESTS PASSED.`。若 `make test_wb` 报 `TRAP`/`ERROR!`，先检查 `firmware/firmware.hex` 是否已生成（`make firmware/firmware.hex`）。
5. 待本地验证：具体周期数与波形需本机装 Icarus Verilog 后才能观察。

#### 4.4.5 小练习与答案

**练习 1**：同一笔读事务，AXI 适配器和 Wishbone 适配器分别最少需要几个时钟周期（假设从设备零延迟、组合直通）？

**答案**：
- AXI 适配器：`arvalid/arready` 同拍成立、`rvalid` 同拍返回，则 `mem_ready` 可在 `mem_valid` 拉高的同拍成立——理论上**可单拍**成交（全组合）。
- Wishbone 适配器：IDLE 拍锁存并拉起 `cyc/stb` → WBSTART 拍等 `ack_i` → WBEND 拍撤销，至少**约 3 拍**（状态机固有节拍）。所以 Wishbone 版的 CPI 通常略高于 AXI 版与原生版。

**练习 2**：如果要让一个既有 Wishbone 外设、又要支持指令预取的系统用上 PicoRV32，应该选哪个变体？为什么 `mem_instr` 在 Wishbone 版被单独引出？

**答案**：选 `picorv32_wb`。`mem_instr` 被 `picorv32_wb` 单独作为 `output` 引出（[picorv32.v:2897](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L2897)），是因为 Wishbone 没有像 AXI `arprot[2]` 那样的「指令访问」属性位——为了让外设/测试台仍能区分取指与数据读（`testbench_wb.v` 的 `wb_ram` 正是用它给读事务标注 `INSN`，见 [testbench_wb.v:284](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/testbench_wb.v#L284)），只能单独拉一根旁路线。

---

## 5. 综合实践

**任务**：画一张「原生 `mem_*` 接口 → AXI4-Lite 五通道」的时序对应图，并用 `testbench_wb.v` 验证你对 Wishbone 握手的理解。

**步骤**：

1. **画 AXI 对应图**（纸笔或绘图工具）：
   - 横轴为时钟拍，纵轴分五组：AW、W、B、AR、R。
   - 画一笔写事务：标出 `mem_valid` 何时为高、`awvalid`/`wvalid` 何时拉起、何时因 `ack_awvalid`/`ack_wvalid` 撤销、`bvalid` 何时返回、`mem_ready` 何时成立。
   - 再画一笔读事务：`arvalid` 拉起→`rvalid`/`rdata` 返回→`mem_ready`。
   - 对照 [picorv32.v:2773-2787](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L2773-L2787) 与 [picorv32.v:2790-2807](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L2790-L2807) 检查每个跳变是否有源码依据。

2. **读 `testbench_wb.v` 的 `wb_ram`**（[testbench_wb.v:189-293](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/testbench_wb.v#L189-L293)）：
   - 找到 `wire valid = wb_cyc_i & wb_stb_i;`（[testbench_wb.v:217](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/testbench_wb.v#L217)），确认 Wishbone 「成交」靠 `cyc & stb`。
   - 找到 `wb_ack_o <= valid & !wb_ack_o;`（[testbench_wb.v:222](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/testbench_wb.v#L222)），解释为什么 `ack` 是一拍宽的脉冲（下一拍 `wb_ack_o` 已为 1，`valid & !wb_ack_o` 变 0）。
   - 找到对 `0x1000_0000` 写字符、对 `0x2000_0000` 写魔术数 `123456789` 的处理（[testbench_wb.v:247-262](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/testbench_wb.v#L247-L262)），这就是固件 Hello World（u2-l2）在 Wishbone 版里的落点。

3. **总结差异**：用一段话回答——「为什么 AXI 适配器可以全组合、无状态机，而 Wishbone 适配器必须用三状态 FSM？」（提示：完成信号的电平性 vs 节拍性、`cyc/stb` 需要被显式驱动与撤销。）

**交付物**：一张时序对应图 + 一段差异说明。若本机有 Icarus Verilog，运行 `make test_axi` 与 `make test_wb` 各一次，把两份日志末尾的 `ALL TESTS PASSED.` 截图作为「功能等价」的证据。

## 6. 本讲小结

- `picorv32_axi` 与 `picorv32_wb` 都是**薄包装**：它们转发同一套 `parameter`，内部都实例化唯一的 `picorv32` 核，只把对外端口从原生 `mem_*` 换成 AXI4-Lite 或 Wishbone 信号。
- `picorv32_axi_adapter` 是真正的 AXI 翻译器：用 `|mem_wstrb` 区分读/写、分别驱动 AW/W（写）或 AR（读），用三个 ack 标志保证每通道每笔事务只握手一次，用 `bvalid`/`rvalid` 作为完成判据——**几乎全组合、无状态机**。
- 适配器把取指事务打上 `arprot=0b100`（AXI `AxProt[2]` 指令位），让有能力的从设备能区分取指与数据读。
- `picorv32_wb` 用 **IDLE/WBSTART/WBEND 三状态 FSM** 驱动 `cyc`/`stb` 节拍、在 `WBSTART` 命中 `ack_i` 时给核一个一拍宽的 `mem_ready` 脉冲；并顺手用 `~wb_rst_i` 做高/低有效复位转换。
- 两种适配风格差异的根因：AXI 的 `bvalid/rvalid` 是电平型完成信号（可组合直通），Wishbone 的 `ack` 常是寄存输出且 `cyc/stb` 需要显式节拍（必须状态机）。
- 三种变体在功能上等价：`make test`（AXI）、`make test_axi`、`make test_wb` 跑同一份 `firmware.hex`，业务结果一致，差别仅在接口时序与 CPI。

## 7. 下一步学习建议

- 下一讲 [u7-l2 RISC-V 压缩指令集支持](u7-l2-compressed-isa.md) 会回到核内部，讲解 `COMPRESSED_ISA` 如何把 16 位指令展开成 32 位，并影响 `mem_la_firstword` 等取指时序——与本讲的内存接口时序紧密相关。
- 若想看 AXI 适配器在更复杂环境中的表现，可先读 [u8-l2 仿真测试台与执行追踪](u8-l2-simulation-and-tracing.md) 里 `testbench.v` 的 `axi4_memory` 从设备与 `+axi_test` 引入的随机延迟。
- 想动手扩展的读者，可参照 `picorv32_axi_adapter` 的写法，尝试写一个把原生接口接到 Avalon 或自定义简单总线的适配器——只要遵循「区分读写、维护 valid 保持、用电平型完成信号」三条原则即可。
