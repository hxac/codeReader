# DDR3读写验证 mem_test

## 1. 本讲目标

上一讲（u3-l1）我们读完了 DDR3 突发传输控制器 `mem_burst`，把它理解成夹在「用户」和 Xilinx MIG IP 之间的「突发级翻译层」。但 `mem_burst` 本身只是个执行者——它需要一个上层模块来下达「从哪个地址读 / 写多少个字」的命令。

本讲就来读这个上层模块：[`mem_test`](../DDR3控制/mem_test.v)。它是一个**自检验（self-test）程序**，不停地在 DDR3 里「写一组有规律的数据 → 原样读回 → 逐个比对」，一旦读回的数据和写入的不一样，就拉高 `error` 信号报警。

学完本讲你应该能够：

1. 说清 `mem_test` 作为 `mem_burst` 的「用户」所扮演的角色，以及它和 `mem_burst` 的端口如何一一对接。
2. 读懂它的三状态验证状态机：`IDLE → MEM_WRITE → MEM_READ →（推进地址）→ MEM_WRITE …` 的无限循环。
3. 理解写数据模式 `{(MEM_DATA_BITS/8){wr_cnt}}` 是怎么构造的、为什么这样设计。
4. 理解 `error` 比较逻辑里「读地址 / 写地址 / 计数器」三者是如何对齐的，以及 `rd_burst_addr` 为什么必须等于 `wr_burst_addr`。
5. 能动手写一个最小测试台（testbench）激励思路，驱动 `mem_test` 并观察 `error`。

## 2. 前置知识

在进入源码前，先用三段白话把背景补齐。

### 2.1 什么是「写后读回」环回验证

验证一块存储器（或一个存储控制器）最朴素、最有效的方法就是**环回测试（loopback test）**：

1. 往某个地址写一个「一眼能认出来」的值；
2. 再从同一个地址把它读回来；
3. 比较读回的值和写入的值是否相等。

如果相等，说明「写通路 + 存储介质 + 读通路」这条链路是通的、是对的；如果不相等，就说明某个环节出了问题。`mem_test` 把这个过程自动化了，并且**不断推进地址**，从而把整片 DDR3 都扫一遍。

### 2.2 为什么要写「有规律的数据」

如果随便写一个常数（比如全写 `0`），那么读回一个 `0` 你并不能确定它真的是你写进去的那个 `0`，还是「地址错了、读到了别人写的 `0`」，也可能是「数据线全被短接到地」。所以验证数据必须**和位置（序号）强相关**，让每个位置写入不同的、可预测的值。`mem_test` 用一个自增计数器来生成这种数据，下文会详细展开。

### 2.3 与上一讲的衔接

`mem_burst` 暴露给用户的「突发级接口」长这样（回顾 u3-l1）：

| 方向（相对 mem_burst） | 信号 | 含义 |
|---|---|---|
| 输入 | `rd_burst_req` / `wr_burst_req` | 请求一次读 / 写突发 |
| 输入 | `rd_burst_len` / `wr_burst_len` | 突发长度（多少个 64bit 字） |
| 输入 | `rd_burst_addr` / `wr_burst_addr` | 突发起始地址 |
| 输出 | `rd_burst_data_valid` | 读数据有效 |
| 输出 | `rd_burst_data` | 读数据内容 |
| 输出 | `wr_burst_data_req` | 「我现在需要你把写数据喂给我」 |
| 输入 | `wr_burst_data` | 用户喂进来的写数据 |
| 输出 | `rd_burst_finish` / `wr_burst_finish` | 本次突发结束 |

`mem_test` 就是这套接口的「标准用户」——它的端口几乎和这张表一一对应，只不过方向相反：`mem_burst` 的输入是 `mem_test` 的输出，反之亦然。本讲会反复回到这张表。

> 术语提示：**突发（burst）** 指一次性连续传输一批数据；**MIG** 是 Xilinx 的 DDR3 内存控制器 IP（Memory Interface Generator）；**ui_clk** 是 MIG 输出给用户逻辑的接口时钟，本模块里叫 `mem_clk`。

## 3. 本讲源码地图

本讲只涉及 `DDR3控制/` 目录下的两个文件：

| 文件 | 行数 | 角色 | 本讲用法 |
|---|---|---|---|
| [`DDR3控制/mem_test.v`](../DDR3控制/mem_test.v) | 127 | 自检验测试程序，`mem_burst` 的「标准用户」 | **精读主角** |
| [`DDR3控制/mem_burst.v`](../DDR3控制/mem_burst.v) | 234 | DDR3 突发读写控制器，封装 MIG | 作为「被测对象」，只引用它的用户侧接口，不重复展开内部状态机 |

此外，本讲会用到上一讲（u3-l1）已经建立的结论：`mem_burst` 的 `wr_burst_data_req`、`rd_burst_data_valid`、`*_finish` 等信号的含义。如果这些信号让你困惑，建议先复习 u3-l1。

## 4. 核心概念与源码讲解

按规格，本讲拆成三个最小模块：**mem_test 模块定位与端口**、**验证状态机（写后读回）**、**error 检测（模式数据与比对）**。

---

### 4.1 mem_test 模块：mem_burst 的「用户」角色

#### 4.1.1 概念说明

`mem_test` 不是图像拼接流水线的一部分——它是一个**独立的、上电自检模块**。可以把它理解成 DDR3 子系统的「开机体检程序」：系统一上电，它就自动开始写写读读，确认 DDR3 通路健康，之后才让真正的图像数据走进来。在本仓库里，它同时也是阅读 `mem_burst` 用户侧接口的**最佳示例**，因为它示范了「如何正确地使用 `mem_burst`」。

#### 4.1.2 核心流程

`mem_test` 把整个验证过程抽象成一个高层循环：

```
每轮循环：
  1. 向 mem_burst 申请一次「写突发」：起始地址 = 当前写基地址，长度 = 128
  2. 在 mem_burst 索要数据时（wr_burst_data_req），按序号喂入「模式数据」
  3. 等待 wr_burst_finish
  4. 向 mem_burst 申请一次「读突发」：起始地址 = 刚才的写基地址，长度 = 128
  5. 在 mem_burst 吐出数据时（rd_burst_data_valid），逐个比对
  6. 等待 rd_burst_finish
  7. 写基地址 += 128，回到第 1 步，扫描下一块区域
```

注意第 4 步：**读地址就是刚才的写地址**，这正是「写后读回」的关键，下文 4.3 会展开。

#### 4.1.3 源码精读

先看模块的参数和端口：

[DDR3控制/mem_test.v:1-22](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/DDR3控制/mem_test.v#L1-L22) 定义了 `mem_test` 的全部对外接口。两个参数：

- `MEM_DATA_BITS = 64`：每个数据的位宽，和 `mem_burst`、DDR3 数据总线一致。
- `ADDR_BITS = 24`：地址位宽。

端口可以分三组来读，它们和 2.3 节那张表完全对得上：

- **请求 / 控制（输出到 mem_burst）**：`rd_burst_req`、`wr_burst_req`、`rd_burst_len`、`wr_burst_len`、`rd_burst_addr`、`wr_burst_addr`。
- **读通路（来自 mem_burst）**：`rd_burst_data_valid`、`rd_burst_data`、`rd_burst_finish`。
- **写通路（与 mem_burst 双向）**：`wr_burst_data_req`（mem_burst 索要数据）、`wr_burst_data`（喂给 mem_burst）、`wr_burst_finish`。
- **结果**：`output reg error`，比对失败的报警信号。

再看内部声明：

[DDR3控制/mem_test.v:23-31](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/DDR3控制/mem_test.v#L23-L31) 定义了三个状态常量和几个关键寄存器：

```verilog
localparam IDLE = 3'd0;
localparam MEM_READ = 3'd1;
localparam MEM_WRITE  = 3'd2;

reg[2:0] state;
reg[7:0] wr_cnt;                 // 写数据序号（0~127）
reg[MEM_DATA_BITS - 1:0] wr_burst_data_reg;
assign wr_burst_data = wr_burst_data_reg;   // 组合透传
reg[7:0] rd_cnt;                 // 读数据序号（0~127）
```

这里有一个值得注意的对比：`mem_burst` 用了 **8 个状态**（`IDLE / MEM_READ / MEM_READ_WAIT / MEM_WRITE / …`），而 `mem_test` 只用了 **3 个状态**。原因是 `mem_test` 是高层「指挥官」，它只管「下命令 + 等完成信号」；底层那些「等 `app_rdy`、自增地址、喂数据」的脏活累活全由 `mem_burst` 这个「执行者」兜着。这种**关注点分离**是分层硬件设计的常见手法。

#### 4.1.4 代码实践

**实践目标**：建立 `mem_test` 端口与 `mem_burst` 用户侧接口的一一对应关系。

**操作步骤**：

1. 打开 [`mem_burst.v:8-23`](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/DDR3控制/mem_burst.v#L8-L23)，找到它的用户侧端口（`rd_burst_req` 等）。
2. 对照 [`mem_test.v:6-22`](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/DDR3控制/mem_test.v#L6-L22)，逐个确认「方向相反、名字相同」。
3. 注意到一个细节：`mem_burst` 还多了一个 `output burst_finish`（读写任一完成），而 `mem_test` 没用到它——`mem_test` 只关心 `rd_burst_finish` / `wr_burst_finish` 这两个更精确的信号。

**预期结果**：你能画出一张「`mem_test.<信号>` ↔ `mem_burst.<同名信号>`」的对接表，方向全部对调。这是后续动手写 testbench 时实例化两个模块、把它们连起来的依据。

#### 4.1.5 小练习与答案

**练习 1**：`mem_test` 的 `MEM_DATA_BITS` 默认是 64。如果把它改成 128（假设 DDR3 也配成 128bit），`error` 比较逻辑需要改吗？

**参考答案**：不需要。因为比较逻辑写的是 `rd_burst_data != {(MEM_DATA_BITS/8){rd_cnt}}`，位宽和复制份数都由参数 `MEM_DATA_BITS` 自动推导（见 4.3 节）。这正是用 `parameter` 参数化的好处——位宽改了，模式数据和比对逻辑自动跟着改。

**练习 2**：为什么 `mem_test` 只有 3 个状态，而 `mem_burst` 有 8 个？

**参考答案**：因为 `mem_test` 是「用户」，它把一次突发的细节（地址自增、握手、计数）全部委托给 `mem_burst`，自己只负责「发起请求 + 等 `*_finish`」。`mem_burst` 作为「执行者」必须处理 MIG 底层 `app_*` 握手的全部时序细节，所以需要更多状态。

---

### 4.2 验证状态机：写后读回与地址推进

#### 4.2.1 概念说明

`mem_test` 的核心是一个无限循环的状态机：先写一块区域，再把**同一块区域**读回来比对，然后把基地址向前推一格，扫描下一块区域，如此反复，直到扫完整片 DDR3。这种「滑动窗口式」的扫描能把存储器的每一个字都覆盖到。

#### 4.2.2 核心流程

状态转移如下（`rst` 后从 `IDLE` 进入）：

```
        rst
         │
         ▼
       IDLE ──────────────► MEM_WRITE            （发起写突发，base=当前wr_burst_addr）
                              │  wr_burst_finish
                              ▼
                            MEM_READ             （发起读突发，rd_burst_addr=wr_burst_addr）
                              │  rd_burst_finish
                              ▼
                            MEM_WRITE            （wr_burst_addr += 128，扫描下一块）
                              │
                              └─────（循环）─────►
```

每轮写突发长度 = 读突发长度 = 128 个 64bit 字。每轮结束后写基地址 `+128`，于是第 0 轮扫 `[0, 128)`、第 1 轮扫 `[128, 256)`……区域之间**无重叠、无空洞**，正好衔接。

> 关于地址单位：`wr_burst_addr` 是「以 64bit 字（即 8 字节）为单位」的基地址。在 `mem_burst` 内部，它会左移 3 位（拼 3 位 0）变成字节地址再送给 MIG，详见 [u3-l1 中 `{rd_burst_addr,3'd0}` 的讲解](../DDR3控制/mem_burst.v#L111-L111)。所以 `wr_burst_addr + 128` 表示「向前跳 128 个字 = 1024 字节」，正好等于一次长度为 128 的突发所覆盖的空间。

#### 4.2.3 源码精读

主状态机在 [DDR3控制/mem_test.v:78-125](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/DDR3控制/mem_test.v#L78-L125)。逐段读：

**复位段（第 80-89 行）**：复位时进入 `IDLE`，把突发长度都设成 `10'd128`，地址清零，请求信号拉低。注意复位是**异步高有效**（`posedge rst`）。

```verilog
if(rst) begin
    state <= IDLE;
    rd_burst_req <= 1'b0;
    wr_burst_req <= 1'b0;
    rd_burst_len <= 10'd128;
    wr_burst_len <= 10'd128;
    rd_burst_addr <= 0;
    wr_burst_addr <= 0;
end
```

**IDLE 状态（第 93-98 行）**：一复位完，立刻拉高 `wr_burst_req`，状态切到 `MEM_WRITE`。也就是说，`mem_test` 上电后**永远先写后读**，不会停。

[DDR3控制/mem_test.v:93-98](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/DDR3控制/mem_test.v#L93-L98)

```verilog
IDLE: begin
    state <= MEM_WRITE;
    wr_burst_req <= 1'b1;
    wr_burst_len <= 10'd128;
end
```

> 这里有个隐藏的设计要点：`mem_test` 一上来就拉高 `wr_burst_req`，但它**不检查 DDR3 是否已经校准完成**（`init_calib_complete`）。会不会出问题？不会——因为 `mem_burst` 内部在 [`mem_burst.v:102`](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/DDR3控制/mem_burst.v#L102) 有一句 `else if(init_calib_complete === 1'b1)` 把整个状态机挡住了，校准完成前它对请求「装聋作哑」。于是 `mem_test` 就老老实实停在 `MEM_WRITE` 状态，直到 `mem_burst` 校准完、回送 `wr_burst_finish` 才往下走。**下层把时序兜住了，上层可以写得很简单**。

**MEM_WRITE 状态（第 99-109 行）**：等 `wr_burst_finish` 一来，立刻切到 `MEM_READ`，同时把读请求拉高、读长度设为 128，并完成本讲最关键的一句：

[DDR3控制/mem_test.v:99-109](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/DDR3控制/mem_test.v#L99-L109)

```verilog
MEM_WRITE: begin
    if(wr_burst_finish) begin
        state <= MEM_READ;
        wr_burst_req <= 1'b0;
        rd_burst_req <= 1'b1;
        rd_burst_len <= 10'd128;
        rd_burst_addr <= wr_burst_addr;   // ★ 读地址 = 刚才的写地址
    end
end
```

`rd_burst_addr <= wr_burst_addr` 这一行就是「写后读回」的灵魂：从哪里写进去，就从哪里读出来。

**MEM_READ 状态（第 110-120 行）**：等 `rd_burst_finish`，然后回到 `MEM_WRITE` 开始下一轮，同时推进写基地址：

[DDR3控制/mem_test.v:110-120](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/DDR3控制/mem_test.v#L110-L120)

```verilog
MEM_READ: begin
    if(rd_burst_finish) begin
        state <= MEM_WRITE;
        wr_burst_req <= 1'b1;
        wr_burst_len <= 10'd128;
        rd_burst_req <= 1'b0;
        wr_burst_addr <= wr_burst_addr + 128;  // ★ 推进到下一块区域
    end
end
```

注意同一个时钟沿里，`rd_burst_addr` 拿到的是**旧的** `wr_burst_addr`，而 `wr_burst_addr` 被更新为**旧值 + 128**。两者用到的的是同一个旧值，所以「读回的基地址」和「刚写入的基地址」永远一致——读到的就是刚写的那块，绝不会错位。

#### 4.2.4 代码实践

**实践目标**：手工跟踪一轮完整的「写 + 读」流程，把每个寄存器的变化写下来。

**操作步骤**：

1. 假设 `rst` 已释放、`init_calib_complete` 已为 1。
2. 跟踪从 `IDLE` 开始的前若干拍：记录 `state`、`wr_burst_req`、`rd_burst_req`、`wr_burst_addr`、`rd_burst_addr` 的取值变化。
3. 重点标注「`MEM_WRITE → MEM_READ`」和「`MEM_READ → MEM_WRITE`」这两个跳变沿上 `rd_burst_addr` 与 `wr_burst_addr` 的关系。

**需要观察的现象**：在 `MEM_WRITE → MEM_READ` 跳变那一拍，`rd_burst_addr` 被赋成跳变前的 `wr_burst_addr`（例如第 0 轮就是 0）；在 `MEM_READ → MEM_WRITE` 跳变那一拍，`wr_burst_addr` 才 `+128`（变成 128）。

**预期结果**：第 0 轮写基地址 = 0、读基地址 = 0；第 1 轮写基地址 = 128、读基地址 = 128；…… 你会发现「读基地址总是等于上一轮的写基地址」，这就是环回验证能成立的前提。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `wr_burst_addr <= wr_burst_addr + 128` 改成 `+ 64`，会发生什么？

**参考答案**：每轮只扫 64 个字的区域，但每轮仍然写 / 读 128 个字。于是相邻两轮的写入区域会**重叠**（第 0 轮写 `[0,128)`，第 1 轮写 `[64,192)`，`[64,128)` 被覆盖两次）。读回时 `error` 仍可能保持 0（因为读的也是刚写的同一块），但验证覆盖率下降、且浪费时间。可见 `+128` 这个值是和「突发长度 128」刻意配对的。

**练习 2**：为什么 `rd_burst_len` 和 `wr_burst_len` 都设成 128？

**参考答案**：因为要「写多少就读多少」，读回的数据量必须和写入的完全相等，比对才能覆盖每一个字。长度不一致会导致多读或少读，计数器对不上。

---

### 4.3 error 检测：递增模式数据与实时比对

#### 4.3.1 概念说明

`error` 信号是 `mem_test` 的最终产出。它的产生分两步：

1. **写端**：用一个自增计数器 `wr_cnt` 生成「和序号强相关」的模式数据，每个字都不一样；
2. **读端**：用一个同步自增计数器 `rd_cnt` 复现「期望值」，把读回的数据和期望值逐个比对，不等就报警。

理解 `error` 的关键是搞清楚「序号对齐」：第 N 个写入的字，必须和第 N 个读回的字比。

#### 4.3.2 核心流程

**模式数据的数学表达**：

设每个字的字节数

\[
B = \text{MEM\_DATA\_BITS} / 8 = 64 / 8 = 8
\]

第 \(i\) 个写入字（\(0 \le i \le 127\)）的内容是把 8bit 计数值 \(i\) 复制 \(B\) 份：

\[
w_i = \underbrace{\,i\ \vert\ i\ \vert\ \cdots\ \vert\ i\,}_{B \text{ 个字节}}
\]

用 Verilog 写就是 `{(MEM_DATA_BITS/8){wr_cnt}}`——「大括号复制」语法 `{N{expr}}` 把 `expr` 复制 N 份拼接。于是：

- `wr_cnt = 0` → `64'h0000000000000000`
- `wr_cnt = 1` → `64'h0101010101010101`
- `wr_cnt = 2` → `64'h0202020202020202`
- …
- `wr_cnt = 5` → `64'h0505050505050505`

为什么用「字节复制」而不是直接 `wr_cnt`？因为 `wr_cnt` 只有 8 位，而数据总线 64 位。复制 8 份后，**每一个字节都等于序号**，这样即使某根数据线坏了（某个字节错），比对也会立刻失败，故障覆盖率最高。

**比对流程**：

```
读端每个 valid 拍：
  期望值 = {B{rd_cnt}}      // 与写端同一公式，用读序号
  if (rd_burst_data != 期望值)
      error <= 1            // 这一拍报警
  else
      error <= 0            // 数据正确则清零
  rd_cnt <= rd_cnt + 1      // 序号推进
```

#### 4.3.3 源码精读

**（a）写端：模式数据生成**

[DDR3控制/mem_test.v:40-57](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/DDR3控制/mem_test.v#L40-L57)

```verilog
always@(posedge mem_clk or posedge rst) begin
    if(rst) begin
        wr_burst_data_reg <= {MEM_DATA_BITS{1'b0}};
        wr_cnt <= 8'd0;
    end
    else if(state == MEM_WRITE) begin
        if(wr_burst_data_req) begin                       // mem_burst 来要数据了
            wr_burst_data_reg <= {(MEM_DATA_BITS/8){wr_cnt}}; // ★ 模式数据
            wr_cnt <= wr_cnt + 8'd1;                      // 序号 +1
        end
        else if(wr_burst_finish)
            wr_cnt <= 8'd0;                               // 本轮结束，序号归零
    end
end
```

要点：

- `wr_burst_data_req` 是 `mem_burst` 在 [`mem_burst.v:76`](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/DDR3控制/mem_burst.v#L76) 给出的「我现在准备好接数据了」信号（`(state == MEM_WRITE) & app_wdf_rdy`）。每来一拍，`mem_test` 就按 `wr_cnt` 生成一个字、序号 `+1`，形成 `0,1,2,…,127` 的序列。
- `wr_burst_data` 由第 30 行 `assign wr_burst_data = wr_burst_data_reg;` 组合透传出去，喂给 `mem_burst`。
- 一轮写完后 `wr_cnt` 归零，下一轮重新从 `0` 开始——所以每块区域写入的模式完全相同，读回的期望值也能用同一个公式复现。

**（b）读端：序号计数器**

[DDR3控制/mem_test.v:59-76](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/DDR3控制/mem_test.v#L59-L76)

```verilog
always@(posedge mem_clk or posedge rst) begin
    if(rst)
        rd_cnt <= 8'd0;
    else if(state == MEM_READ) begin
        if(rd_burst_data_valid)              // 收到一个读回的字
            rd_cnt <= rd_cnt + 8'd1;
        else if(rd_burst_finish)
            rd_cnt <= 8'd0;
    end
    else
        rd_cnt <= 8'd0;
end
```

`rd_cnt` 在 `MEM_READ` 状态下，每收到一个有效读数据（`rd_burst_data_valid`，由 `mem_burst` 在 [`mem_burst.v:74`](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/DDR3控制/mem_burst.v#L74) 直接透传自 MIG 的 `app_rd_data_valid`）就 `+1`，和写端的 `wr_cnt` 节奏完全一致——这是「序号对齐」的硬件保证。

**（c）比对：error 的产生**

[DDR3控制/mem_test.v:32-38](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/DDR3控制/mem_test.v#L32-L38)

```verilog
always@(posedge mem_clk or posedge rst) begin
    if(rst)
        error <= 1'b0;
    else
        error <= (state == MEM_READ)
              && rd_burst_data_valid
              && (rd_burst_data != {(MEM_DATA_BITS/8){rd_cnt}});
end
```

三个条件**同时成立**才报警：

1. 当前确实在 `MEM_READ` 状态（写阶段不比对，否则 `rd_cnt` 不推进会乱报）；
2. 本拍真的有有效读数据（`rd_burst_data_valid`）；
3. 读回数据 ≠ 期望模式 `{(MEM_DATA_BITS/8){rd_cnt}}`。

注意第 3 条用的期望值公式和写端**完全相同**，只是把 `wr_cnt` 换成了 `rd_cnt`。由于 `rd_cnt` 和 `wr_cnt` 在各自突发里按相同节拍自增（都是 0→127），第 N 个读回字就和第 N 个写入字比对，对齐天然成立。

> **一个容易被忽略的细节：`error` 不是「锁存」的**。从代码看，`error` 每个时钟都被重新赋值——只要本拍比对通过，下一拍 `error` 就回到 0。也就是说，如果只有一个字出错，`error` 只会**脉冲式地高一个时钟周期**，随后又变低。如果你在 testbench 里只在仿真结束时瞄一眼 `error`，很可能正好看到 0 而漏掉故障。工程上更稳妥的做法是在外部再做一个「或锁存」：`error_sticky <= error_sticky | error;`，让任何一次出错都被永久记住。这个观察在 5.1 综合实践里会用到。

#### 4.3.4 代码实践

**实践目标**：手算若干个模式数据，确认你对 `{(MEM_DATA_BITS/8){cnt}}` 的理解；并定位 error 比对的三个条件。

**操作步骤**：

1. 取 `MEM_DATA_BITS = 64`，手算 `wr_cnt = 0, 1, 2, 5, 255` 时 `wr_burst_data_reg` 的 64 位十六进制值。
2. 回到 [第 32-38 行的 error 逻辑](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/DDR3控制/mem_test.v#L32-L38)，逐个解释：为什么必须同时判断 `state == MEM_READ`？为什么必须判断 `rd_burst_data_valid`？如果去掉这两个条件分别会出什么问题？

**预期结果**：

| `wr_cnt` | `wr_burst_data_reg`（64'h…） |
|---|---|
| 0 | `0000000000000000` |
| 1 | `0101010101010101` |
| 2 | `0202020202020202` |
| 5 | `0505050505050505` |
| 255 | `ffffffffffffffff` |

去掉 `state == MEM_READ`：`rd_cnt` 在非读状态被清 0，会拿「0」去和无关信号乱比，可能误报。去掉 `rd_burst_data_valid`：在没有有效数据的空拍也会比对，读到的是上一拍的残留 `rd_burst_data`，同样会误报。两者都是「只在确有数据到来的那一拍比对」的前提条件。

> 待本地验证：上表为根据公式手算的结果，未在真实仿真器上打印验证。建议你在综合实践里加一句 `$display` 把实际写入值打出来对照。

#### 4.3.5 小练习与答案

**练习 1**：如果把模式数据改成「直接用 `wr_cnt` 不复制」（即 `wr_burst_data_reg <= wr_cnt;` 高位补 0），验证还能不能正常工作？

**参考答案**：基本还能工作（只要读端用同样公式比），但故障覆盖率会下降。因为高 56 位永远是 0，如果 DDR3 的高位数据线发生故障（比如恒为 1），由于写入和读回的高位都受同一故障影响、或都被忽略，可能检测不到。字节复制让每一位都有变化，是最稳妥的写法。

**练习 2**：`error` 信号在出现一次错误后，会一直保持高电平吗？

**参考答案**：不会。`error` 每个 `mem_clk` 都被重新求值，比对通过的那一拍它就回到 0。它只反映「当前这一拍」的比对结果，是一个脉冲式信号。要捕捉瞬时错误需要外部锁存（见 4.3.3 末尾的讨论）。

**练习 3**：为什么 `rd_cnt` 和 `wr_cnt` 都只用 8 位（`reg[7:0]`）？

**参考答案**：因为突发长度是 128，序号范围是 `0~127`，用 8 位（最大 255）已经够用且留有余量。计数到 127 后由 `*_finish` 归零，不会溢出。如果以后把突发长度加到超过 256，就需要加宽这两个计数器。

---

## 5. 综合实践

本讲的任务是**写一个 testbench 的激励思路，驱动 `mem_test` 并观察 `error` 是否保持为 0；并说明 `rd_burst_addr` 为何要等于 `wr_burst_addr`。**

### 5.1 难点：仓库里没有 MIG IP

直接实例化 `mem_burst` 来测 `mem_test` 是行不通的——`mem_burst` 依赖 Xilinx MIG IP（`app_*` 那一组信号），而 MIG 没有收录在本仓库里（见 u1-l2 的导航说明）。在普通仿真器上没有 MIG 就跑不起来。

解决办法是**写一个「假的 mem_burst」**（下文称 `mem_burst_stub`），它只实现 `mem_burst` 的**用户侧接口时序**，内部不做任何真 DDR3 访问，而是用一个数组把写进来的数据存起来、读的时候按相同顺序吐回去。这样就能在纯 Verilog 仿真器里把 `mem_test` 跑起来。

### 5.2 思路一：正确环回，error 应保持 0

下面的 stub（**示例代码，非项目原有文件**）模仿了 `mem_burst` 用户侧的关键握手：

```verilog
// 示例代码：仅供理解 mem_test 行为的最小 stub，不是项目源码
module mem_burst_stub #(parameter MEM_DATA_BITS = 64, parameter ADDR_BITS = 24) (
    input  rst, input mem_clk,
    input  rd_burst_req, input wr_burst_req,
    input  [9:0] rd_burst_len, input [9:0] wr_burst_len,
    input  [ADDR_BITS-1:0] rd_burst_addr, input [ADDR_BITS-1:0] wr_burst_addr,
    output reg rd_burst_data_valid,
    output reg wr_burst_data_req,
    output reg [MEM_DATA_BITS-1:0] rd_burst_data,
    input  [MEM_DATA_BITS-1:0] wr_burst_data,
    output reg rd_burst_finish, output reg wr_burst_finish
);
    // 用一个简单的存储器数组模拟 DDR3（按字地址索引）
    reg [MEM_DATA_BITS-1:0] model_mem [0:1023];
    integer wi = 0, ri = 0;
    integer base;
    // 写突发：来一个 wr_burst_data_req 就存一个字，存满 len 个字后给 finish
    // 读突发：按 rd_burst_addr 起始，每个周期吐一个字并拉高 valid，吐完给 finish
    // ……（具体 always 块按 mem_burst 用户侧时序补全）
endmodule
```

**操作步骤**：

1. 按上面骨架补全 stub：写突发里 `model_mem[wr_burst_addr + wi] <= wr_burst_data; wi++;`，数够 `wr_burst_len` 个就拉一拍 `wr_burst_finish`；读突发里 `rd_burst_data <= model_mem[rd_burst_addr + ri]; rd_burst_data_valid <= 1; ri++;`，数够 `rd_burst_len` 个就拉 `rd_burst_finish`。
2. 在 testbench 里实例化 `mem_test` 和 `mem_burst_stub`，把它们同名的用户侧信号一一相连（方向对接）。
3. 产生 `mem_clk`（比如 100MHz）和一次 `rst` 脉冲，然后 `run` 足够长时间。
4. 监视 `error`（建议加一个 `error_sticky` 锁存，见 4.3.3）。

**需要观察的现象**：因为 stub 把写进去的数据原样读回来，`mem_test` 的比对每次都通过，`error` 应该**一直为 0**，`error_sticky` 也保持 0。同时你能看到 `wr_burst_addr` 每「写+读」一轮就 `+128`，而 `rd_burst_addr` 在每轮读时等于上一轮的 `wr_burst_addr`。

> 待本地验证：本仓库不含任何 testbench 文件，也无可运行仿真脚本，以上为激励思路。具体能否跑通取决于你补全的 stub 时序是否和 `mem_burst` 真实时序一致。

### 5.3 思路二：人为注入错误，观察 error 脉冲

把 stub 的读回路径改成「故意把某一个字弄错」，例如：

```verilog
// 示例代码：读回第 3 个字时翻转最低位，模拟 DDR3 一位出错
if (ri == 3) rd_burst_data <= model_mem[base+ri] ^ 64'h1;
else         rd_burst_data <= model_mem[base+ri];
```

**需要观察的现象**：当 `rd_cnt == 3`、`rd_burst_data_valid` 为 1 时，`error` 会出现**一个时钟周期的高脉冲**；下一拍数据恢复正常后 `error` 又回到 0。这正好印证了 4.3.3 里「`error` 不锁存」的结论，也说明为什么需要外部 `error_sticky` 才能可靠捕捉。

### 5.4 回答：rd_burst_addr 为何要等于 wr_burst_addr

`mem_test` 是「写后读回」的环回验证：读回的数据要和**刚写入**的数据逐字比对。写入发生在「以 `wr_burst_addr` 为基地址的 128 个字」，所以读回必须从**同一个基地址**开始，才能保证第 N 个读回字对应第 N 个写入字，`rd_cnt` 和 `wr_cnt` 才能对齐、比对才有意义。

如果 `rd_burst_addr` 取了别的值（比如比写地址大或小），读到的就是另一块区域的数据——要么是历史遗留的脏数据、要么是没写过的随机值，几乎必然触发 `error`，于是这次验证就**失去了「检验写通路是否正确」的意义**，变成在报「读到了不相干的数据」。所以 [第 107 行 `rd_burst_addr <= wr_burst_addr`](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/DDR3控制/mem_test.v#L107) 不是随便写的，而是环回验证的本质要求。

## 6. 本讲小结

- `mem_test` 是 `mem_burst` 的「标准用户」和 DDR3 子系统的「上电自检程序」，端口与 `mem_burst` 用户侧接口一一对应、方向相反。
- 它用 3 个状态（`IDLE / MEM_WRITE / MEM_READ`）构成「写→读回→推进地址→再写」的无限循环，比 `mem_burst` 的 8 状态简洁得多，因为底层时序由 `mem_burst` 兜底（如 `init_calib_complete` 校准门控）。
- 写数据用 `{(MEM_DATA_BITS/8){wr_cnt}}` 把 8bit 序号复制成 64bit，让每个字节的值都等于序号，故障覆盖率最高。
- `error` 在 `MEM_READ` 且 `rd_burst_data_valid` 时，把读回值与 `{(MEM_DATA_BITS/8){rd_cnt}}` 比对，三条件缺一不可；`rd_cnt` 与 `wr_cnt` 同节拍自增，天然保证「第 N 个读回字 vs 第 N 个写入字」。
- `rd_burst_addr <= wr_burst_addr` 是环回验证的灵魂：从哪写就从哪读；`wr_burst_addr` 在每轮读完后 `+128`，无重叠、无空洞地扫遍 DDR3。
- 重要细节：`error` 是脉冲式、非锁存的，只反映当前拍的比对结果；要可靠捕捉瞬时错误需要外部或锁存。

## 7. 下一步学习建议

- **横向巩固**：回头重读 [u3-l1 的 `mem_burst` 状态机](u3-l1-ddr3-mem-burst.md)，对照本讲确认 `wr_burst_data_req`、`rd_burst_data_valid`、`*_finish` 这些信号在 `mem_burst` 内部是怎么产生的，从而把「用户视角」和「执行者视角」拼成完整画面。
- **纵向深入**：进入 u3-l3，学习 README 中描述的「24bit→64bit 异步 FIFO 数据位宽转换」——它是摄像头采集端（24bit 像素）和 DDR3 接口端（64bit 字）之间的位宽匹配桥梁，实现代码未收录于仓库，需结合 README 描述理解。
- **延伸思考**：本讲的「写后读回 + 模式数据」是非常通用的存储器自检套路。建议对照你接触过的其他 DDR3/BRAM 工程的自检逻辑，体会「与位置强相关的模式数据」「脉冲式 error 的外部锁存」这两个技巧的普适性。
