# 公共单元库 common_cell

## 1. 本讲目标

`src/common_cell/` 是 Ventus GPGPU 全项目共享的「标准零件库」。它把 FIFO、仲裁器、位计数、编码转换这类**在每个模块里都会重复出现的小逻辑**抽取成参数化、接口统一的可复用单元。学完本讲，你应当能够：

1. 识别 `common_cell` 中四大类基础单元（FIFO 家族、仲裁器、位计数、编码转换），并说出每类解决什么问题。
2. 看懂 `fifo.v` 用「多一位绕回指针」判满/判空的经典技巧，以及 `stream_fifo` 系列如何在它之上叠加 valid/ready 握手、流水穿透、旁路、SRAM 存储等不同行为。
3. 区分**固定优先级仲裁**与**轮询仲裁**的实现差异与适用场景。
4. 理解 `pop_cnt`、`find_first`、`bin2one`、`one2bin` 这类纯组合逻辑单元的原理，并能解释它们在 cache/调度器里被怎样复用。
5. 当自己要新增一个需要缓冲或仲裁的小模块时，知道该从库里挑哪一个现成单元。

## 2. 前置知识

- **valid/ready 握手**：Ax接口（如 TileLink/AXI-Stream 风格）的最基本流控。发送方拉高 `valid` 并给出 `data`，接收方拉高 `ready`，**同一拍两者都为 1 数据才真正传输**（本讲记作一次 push/pop）。`common_cell` 里几乎所有 FIFO 都长着 `w_valid_i/w_ready_o`（写侧）和 `r_valid_o/r_ready_i`（读侧）这组口。
- **one-hot（独热）编码**：N 位向量里只有 1 位为 1，其余为 0。常用来表示「选中了第几个」。本讲的仲裁器输出 `grant`、`bin2one`/`one2bin` 都围绕它。
- **组合逻辑 vs 时序逻辑**：本讲里 `fixed_pri_arb`、`pop_cnt`、`find_first`、`bin2one`、`one2bin` 是**纯组合**（无 clk，输入变输出立刻变）；`fifo`、`round_robin_arb` 是**时序**（有 clk/rst_n，内部有寄存器）。
- 建议先读过 [u1-l2 源码目录结构与模块组织](u1-l2-source-directory-layout.md)，了解 `common_cell` 在整个仓库里的定位（它是 `src/` 下与 `define/`、`gpgpu_top/` 并列的三大基础目录之一）。

## 3. 本讲源码地图

本讲涉及的关键文件全部位于 `src/common_cell/` 下：

| 文件 | 作用 | 类型 |
|------|------|------|
| `fifo.v` | 最底层同步 FIFO 内核，提供 `w_en/r_en` 与 `full/empty` | 时序 |
| `stream_fifo.v` | 在 `fifo` 上套 valid/ready 握手的标准流式 FIFO | 时序 |
| `stream_fifo_pipe_true.v` | 允许「同拍进出」的流水穿透型 FIFO，切断组合长路径 | 时序 |
| `stream_fifo_flow_true.v` | 空时直接旁路的 flow 型 FIFO（0 延迟直通） | 时序 |
| `stream_fifo_useSRAM.v` | 用 `dualportSRAM` 存储体实现的大容量 FIFO | 时序 |
| `dualportSRAM.v` | 双端口存储器行为模型，供 `stream_fifo_useSRAM` 调用 | 时序 |
| `fixed_pri_arb.v` | 固定优先级仲裁器（编号小者优先） | 组合 |
| `round_robin_arb.v` | 轮询（round-robin）仲裁器，带状态 | 时序 |
| `pop_cnt.v` | population count，数输入中 1 的个数 | 组合 |
| `find_first.v` | 从 MSB 向 LSB 找第一个 1（或 0）的位置 | 组合 |
| `bin2one.v` / `one2bin.v` | 二进制 ↔ 独热码互转 | 组合 |

复用规模（全项目 `src/` 内统计）：FIFO 家族被引用约 **88 处、覆盖 33 个文件**；两类仲裁器约 **63 处、35 个文件**；`pop_cnt/find_first/bin2one/one2bin` 合计约 **99 处、55 个文件**。可以说，GPU 里几乎每一个子系统都在调用这个零件库。

## 4. 核心概念与源码讲解

### 4.1 FIFO 家族：从 fifo 到 stream_fifo 系列

#### 4.1.1 概念说明

FIFO（先入先出队列）是硬件里最常用的「弹性缓冲」：上游产生数据的速率和下游消费的速率短暂不匹配时，用 FIFO 把上游先放进去、下游稍后再取，从而**解耦两边的时序**。GPU 流水线里到处都是这种需求——取指回来的指令先存一下再译码、执行单元多拍出结果先攒着再写回、AXI 多通道响应先排队再处理。

`common_cell` 把 FIFO 做成了一个**家族**：底层同一个存储内核，上层根据「要不要握手」「能不能同拍进出」「要不要旁路」「用寄存器还是 SRAM 存」等需求包出不同外壳。理解这个家族的关键是抓住**每个变体改的那一行 push/pop/ready 逻辑**。

#### 4.1.2 核心流程

所有变体都遵循同一个队列模型：

```
        push = (写侧想写 && 队列还能装)
        pop  = (读侧想读 && 队列还有货)
        写指针 w_ptr：每 push 一次 +1，指向下一个空位
        读指针 r_ptr：每 pop 一次 +1，指向下一个要读的位
        empty = (w_ptr == r_ptr)
        full  = (w_ptr 跑了一圈追上 r_ptr)
```

判满判空有一个经典技巧：**给指针多留一位「绕回位」**。设地址宽度为 `ADDR_WIDTH`，则指针用 `ADDR_WIDTH+1` 位。低 `ADDR_WIDTH` 位是真正的地址；最高位是「绕回标志」。当读写指针的低 位完全相同、但最高位相反时，说明写指针比读指针多绕了整整一圈，队列**满**；当两者全部位都相同（包括最高位），队列**空**。

#### 4.1.3 源码精读

**(a) 底层内核 `fifo.v`：绕回位判满/判空**

[/src/common_cell/fifo.v:31](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/common_cell/fifo.v#L31) 处理了一个边界：深度为 1 时 `$clog2(1)=0`，地址位宽会退化，所以强制取 1：

```verilog
localparam ADDR_WIDTH = (FIFO_DEPTH == 1) ? 1 : $clog2(FIFO_DEPTH);
```

指针多留一位（`[ADDR_WIDTH:0]`），见 [/src/common_cell/fifo.v:35](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/common_cell/fifo.v#L35)。判满判空正是用上面说的绕回位技巧，[/src/common_cell/fifo.v:94-95](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/common_cell/fifo.v#L94-L95)：

```verilog
assign full_o  = (r_ptr == {~w_ptr[ADDR_WIDTH], w_ptr[ADDR_WIDTH-1:0]}); // 最高位取反、低位相同 → 满
assign empty_o = (r_ptr == w_ptr);                                       // 全等 → 空
```

> 注意存储体用的是**打包向量** `reg [FIFO_DEPTH*DATA_WIDTH-1:0] dual_port_ram;`（[src/common_cell/fifo.v:34](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/common_cell/fifo.v#L34)），靠 part-select 读写；被注释掉的 `reg [...] dual_port_ram [0:FIFO_DEPTH-1]` 数组形式才是「存储器」。打包向量形式会被综合成**触发器堆**而非块 RAM，所以 `fifo` 适合**小而快**的 FIFO。

另外，深度为 1 时指针 `+2`（[src/common_cell/fifo.v:43](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/common_cell/fifo.v#L43) 与 [L55](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/common_cell/fifo.v#L55)），让地址位恒为 0、只翻转绕回位，是深度 1 的特例补丁。

**(b) `stream_fifo.v`：套上 valid/ready 握手**

[stream_fifo.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/common_cell/stream_fifo.v) 在 `fifo` 上把 `full/empty` 翻译成 `w_ready_o/r_valid_o`，并导出 push/pop（[src/common_cell/stream_fifo.v:36-40](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/common_cell/stream_fifo.v#L36-L40)）：

```verilog
assign w_ready_o = !full ;                       // 没满才能写
assign r_valid_o = !empty;                       // 没空才能读
assign push = (w_valid_i && !full) ;             // 真正写入条件
assign pop  = (r_ready_i && !empty);
```

这是最「老实」的 FIFO：满了就拒绝写、空了就不允许读，读写**互不影响**。

**(c) `stream_fifo_pipe_true.v`：允许同拍进出，切断长路径**

文件头注释点明了它的用处（[src/common_cell/stream_fifo_pipe_true.v:12](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/common_cell/stream_fifo_pipe_true.v#L12)）：`deq_ready=1 -> enq_ready=1`，即「读侧 ready 时，即使满了写侧也能 ready」。关键就改了一行（[src/common_cell/stream_fifo_pipe_true.v:37-39](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/common_cell/stream_fifo_pipe_true.v#L37-L39)）：

```verilog
assign push       = w_valid_i && (!full | r_ready_i);  // 满但本拍在出，就让进
assign w_ready_o  = !full | r_ready_i;                 // 写 ready 不再卡死在 full
```

含义：当 FIFO 满、但下游这一拍正好在读（`r_ready_i=1`，会 pop 走一项），那么这一拍同时 push 也不会溢出。好处是 `w_ready_o` 不再依赖「full → 上游停一拍」的反压环，能**打断组合反馈路径**，改善时序。项目中 `aluexe`、`sfu_exe`、`tensor_core_exe` 等执行单元出口都用了它来切断写回的长组合路径（见 [u4-l2](u4-l2-vector-alu.md) 提到的 `stream_fifo_pipe_true`）。

**(d) `stream_fifo_flow_true.v`：空时直接旁路**

[flow_true](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/common_cell/stream_fifo_flow_true.v) 更激进：队列空、且上下游正好同拍握手时，**数据不进存储体，直接从输入接到输出**（[src/common_cell/stream_fifo_flow_true.v:38-43](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/common_cell/stream_fifo_flow_true.v#L38-L43)）：

```verilog
assign push      = (empty && r_ready_i && w_valid_i) ? 'h0 : (w_valid_i && !full); // 旁路时不占位
assign r_valid_o = w_valid_i || !empty;                                          // 空时也可由输入驱动
assign r_data_o  = (empty && w_valid_i) ? w_data_i : r_data_fifo;                // 空时直通
```

它适合「大部分时候希望 0 延迟通过、偶尔堆积才缓冲」的场景。

**(e) `stream_fifo_useSRAM.v`：换存储体，做大容量 FIFO**

当 FIFO 又深又宽时，触发器堆（`fifo.v` 的打包向量）会吃掉大量面积。`stream_fifo_useSRAM` 改用 [dualportSRAM.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/common_cell/dualportSRAM.v) 作为存储体（[src/common_cell/stream_fifo_useSRAM.v:127-141](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/common_cell/stream_fifo_useSRAM.v#L127-L141)）。`dualportSRAM` 内部是 `reg [BITWIDTH-1:0] mem_core [0:2**DEPTH-1];` 的**数组**（[src/common_cell/dualportSRAM.v:39](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/common_cell/dualportSRAM.v#L39)），读写分离端口，便于映射到 SRAM 宏单元。

代价是 SRAM 「写后读同地址」有冲突（见 [dualportSRAM.v:36](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/common_cell/dualportSRAM.v#L36) 的约束 `AB can't be same as AA`），所以这个 FIFO 多了一层处理：

- 用 `read_en` 在「同地址写读」时干脆不读 SRAM（[src/common_cell/stream_fifo_useSRAM.v:120](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/common_cell/stream_fifo_useSRAM.v#L120)）：
  ```verilog
  assign read_en = !(push && w_addr==r_ptr);
  ```
- 并用 `read_en_reg`/`w_data_reg` 缓存上一拍的写数据，在跳过 SRAM 读的那拍用写入值本身作为输出（[src/common_cell/stream_fifo_useSRAM.v:146-157](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/common_cell/stream_fifo_useSRAM.v#L146-L157)）：
  ```verilog
  assign r_data_o = read_en_reg ? read_data : w_data_reg;
  ```
- 此外用 `fifo_cnt_in`/`fifo_cnt_out` 两个计数器（[L54-86](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/common_cell/stream_fifo_useSRAM.v#L54-L86)）来生成 `r_valid_o`/`w_ready_o`（[L115-116](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/common_cell/stream_fifo_useSRAM.v#L115-L116)），以吸收 SRAM 读延迟。

> **何时选 SRAM 版**：FIFO 深度大（比如几十、上百项）或位宽大，用触发器实现面积/功耗不划算时，选 `stream_fifo_useSRAM`（及其带 count 的 `stream_fifo_useSRAM_with_count`、`stream_fifo_with_incount_useSRAM` 变体）；深度小、对延迟敏感、希望综合成触发器的，用 `stream_fifo` / `stream_fifo_pipe_true`。

#### 4.1.4 代码实践

**实践目标**：用最小 testbench 对比 `stream_fifo` 与 `stream_fifo_useSRAM` 的行为差异，直观感受「触发器版」与「SRAM 版」的区别。

**操作步骤**（以下为**示例代码**，本仓库 `common_cell/` 下没有现成 TB，需自行新建；建议放在仓库外或临时目录，不要污染 `src/`）：

```verilog
// tb_cmp_fifo.v —— 示例 testbench，仅用于学习
`timescale 1ns/1ns
module tb_cmp_fifo;
  reg clk=0, rst_n=0;
  always #5 clk = ~clk;

  // 两路同参数 FIFO，喂同样的激励
  reg        wv=0, rr=0; reg [31:0] wd=0;
  wire       wr1, rv1;   wire [31:0] rd1;
  wire       wr2, rv2;   wire [31:0] rd2;

  stream_fifo        #(.DATA_WIDTH(32),.FIFO_DEPTH(4)) f1(
      .clk(clk),.rst_n(rst_n),.w_ready_o(wr1),.w_valid_i(wv),.w_data_i(wd),
      .r_valid_o(rv1),.r_ready_i(rr),.r_data_o(rd1));
  stream_fifo_useSRAM#(.DATA_WIDTH(32),.FIFO_DEPTH(4)) f2(
      .clk(clk),.rst_n(rst_n),.w_ready_o(wr2),.w_valid_i(wv),.w_data_i(wd),
      .r_valid_o(rv2),.r_ready_i(rr),.r_data_o(rd2));

  initial begin
    rst_n=0; #21 rst_n=1;
    // 连写 5 个，观察第 5 个被反压（w_ready 变 0）
    wv=1; wd=32'hAA;  @(posedge clk);
            wd=32'hBB;  @(posedge clk);
            wd=32'hCC;  @(posedge clk);
            wd=32'hDD;  @(posedge clk);
            wd=32'hEE;  @(posedge clk);   // 预期：wr1/wr2 此时为 0
    wv=0; rr=1; repeat(6) @(posedge clk); // 读空
    $finish;
  end
endmodule
```

**需要观察的现象**：
1. 写满 4 个后，第 5 次写时 `wr1`、`wr2` 是否都变 0（反压）。
2. `stream_fifo` 与 `stream_fifo_useSRAM` 的 `r_data_o` 出现时机是否一致（注意 SRAM 版有读延迟补偿）。

**预期结果**：两者在功能上都能正确缓冲 4 项、写满反压、读空；波形细节（尤其首次有效读出的拍位）可能因 SRAM 版的延迟补偿而略有不同。

**待本地验证**：以上波形结论需用 VCS 或 iverilog 实际仿真确认。VCS 可仿照 [u1-l4](u1-l4-simulation-and-testcases.md) 的 `run.f` 思路，把 `+incdir+src/define` 与两个 FIFO 源文件加入编译。

#### 4.1.5 小练习与答案

**练习 1**：把 `stream_fifo` 接成一个满-满的反压环（下游一直不 ready），上游连续 valid，几拍后 `w_ready_o` 会变 0？  
**答案**：`FIFO_DEPTH` 拍后写满，`full=1`，`w_ready_o=!full=0`。

**练习 2**：`stream_fifo_pipe_true` 在 FIFO 已满、且本拍 `r_ready_i=1` 时，`w_ready_o` 是多少？为什么不会溢出？  
**答案**：`w_ready_o = !full | r_ready_i = 1`。因为同一拍 pop 走了一项，腾出的位置正好给这次 push，所以不会溢出。

**练习 3**：为什么 `fifo.v` 用打包向量存而不是数组？这决定了它适合做多大的 FIFO？  
**答案**：打包向量综合成触发器堆（快、但面积随深度×宽度线性增长），适合小 FIFO；要做大 FIFO 应换 `stream_fifo_useSRAM`（数组形式，可映射 SRAM）。

---

### 4.2 仲裁器：固定优先级与轮询

#### 4.2.1 概念说明

当**多个请求方**要共享**同一个资源**（一个存储端口、一个写回总线、一个 bank、一次发射槽）时，需要一个仲裁器（arbiter）：每拍从若干个 `req` 里挑出**一个**授予（`grant`）。`common_cell` 提供两种最经典的策略：

- **固定优先级（fixed priority）**：编号小者永远优先，除非它不要，才轮到下一个。实现简单、纯组合、无状态，但可能让低优先级者「饿死」。
- **轮询（round robin）**：这一拍给了谁，下一拍就把谁排到最后，循环给每个人机会。公平，但需要记住「上一次给了谁」，所以是时序逻辑。

#### 4.2.2 核心流程

两者输入都是 `req[N-1:0]`，输出都是 one-hot 的 `grant[N-1:0]`（恰好 1 位为 1，表示选中谁），且**没有请求时 grant 全 0**。

```
固定优先级：grant = 选中 req 中编号最小的那一位
轮询：      记一个 one-hot 指针 pre_req（上次的起点）；
            从指针位置开始往后转一圈，选中第一个为 1 的 req；
            每授权一次，指针前移一位（刚授权者排到队尾）。
```

#### 4.2.3 源码精读

**(a) `fixed_pri_arb.v`：一行组合逻辑**

整个模块只有两个 assign（[src/common_cell/fixed_pri_arb.v:26-27](https://github.com/THU-DSP-LAB-ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/common_cell/fixed_pri_arb.v#L26-L27)）：

```verilog
assign pre_req = {(req[ARB_WIDTH-2:0] | pre_req[ARB_WIDTH-2:0]),1'h0};
assign grant   = req & (~pre_req);
```

理解技巧：`pre_req[i]=1` 表示「在 i 之前（编号比 i 小）已经有人请求」。它通过组合自递推展开：`pre_req[0]=0`，`pre_req[i]=req[i-1] | pre_req[i-1]`。于是 `grant = req & ~pre_req` 就是「自己是 1、且前面没人」的位——也就是 `req` 中编号最小的那一位。无 clk、无 rst，纯组合。编号 0 是最高优先级。

> 项目里凡是「有天然优先级」的地方都用它：例如 `l1cache_arb` 里取指优先于访存（[u6-l3](u6-l3-l1cache-arbiter.md)）、`cta2warp` 里选最低空闲 wid、`slowdown` 把「两条一捆」按固定顺序拆成「一条一拍」（[u3-l3](u8-l1-testbench-framework.md) 系列）。

**(b) `round_robin_arb.v`：带状态的轮询**

它需要记住起点，所以有 `pre_req` 寄存器（[src/common_cell/round_robin_arb.v:29](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/common_cell/round_robin_arb.v#L29)）。复位初值为 bit0（[L33-34](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/common_cell/round_robin_arb.v#L33-L34)）。授权选中的核心是「双倍 req 减指针」的经典位运算技巧（[src/common_cell/round_robin_arb.v:42-43](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/common_cell/round_robin_arb.v#L42-L43)）：

```verilog
assign grant_ext = {req,req} & ~({req,req} - pre_req);
assign grant     = grant_ext[ARB_WIDTH-1:0] | grant_ext[2*ARB_WIDTH-1:ARB_WIDTH];
```

`{req,req}` 把请求复制两份拼成 `2N` 位，`pre_req` 是 one-hot 起点放在低位；`X & ~(X - base)` 这一恒等式会**隔离出 X 中从 base 起点开始向后转一圈最先遇到的那一个 1**。最后把高低两半 OR 回来， collapsing 成 `N` 位 one-hot。

授权后更新指针：把刚授权的 `grant` 循环左移一位（[src/common_cell/round_robin_arb.v:35-36](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/common_cell/round_robin_arb.v#L35-L36)）：

```verilog
end else if(|req) begin
  pre_req <= {grant[ARB_WIDTH-2:0],grant[ARB_WIDTH-1]}; // 左移一位 → 刚授权者排到队尾
```

这样下一拍搜索起点就在「刚授权者之后」，保证轮流。文件里还保留了一段被注释的「掩码式」实现（[L46-76](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/common_cell/round_robin_arb.v#L46-L76)），思路等价，读者可比对两种写法。注意该模块 [`include "define.v"`](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/common_cell/round_robin_arb.v#L16)，编译时需要 `+incdir+src/define`（同 [u1-l4](u1-l4-simulation-and-testcases.md)）。

> 项目里凡是「要公平」的地方都用它：`operand_arbiter` 里多采集器争 bank 的轮询、`ibuffer2issue` 在 NUM_WARP 个 warp 间公平选路、`sm2cluster_arb` 在多 SM 间轮流、`inflight_wg_buffer` 公平选 workgroup（参见 [u2-l2](u2-l2-cu-handler-and-inflight-wg.md)、[u4-l1](u4-l1-operand-collector-and-regfile.md)）。

#### 4.2.4 代码实践

**实践目标**：分析 `round_robin_arb` 的轮询状态更新，并预测一串请求的授权序列。

**操作步骤**：
1. 打开 [round_robin_arb.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/common_cell/round_robin_arb.v)，取 `ARB_WIDTH=4`。
2. 设请求恒为 `req = 4'b1111`（四路同时一直请求）。
3. 逐拍手算 `pre_req` 与 `grant`：复位后 `pre_req=0001` → 第 1 拍 grant=`0001`、`pre_req` 更新为 `0010` → 第 2 拍 grant=`0010`、`pre_req=0100` → ……

**需要观察的现象**：grant 是否在 bit0→bit1→bit2→bit3→bit0 之间循环。

**预期结果**：四路同时请求时，授权依次轮转 `0001,0010,0100,1000,0001,...`，任何一路都不会饿死。

**待本地验证**：可写 4 行的 TB 把 `req` 接成 `4'b1111` 跑若干拍，用 VCS/Verdi 核对 `grant` 波形是否如上轮转。

#### 4.2.5 小练习与答案

**练习 1**：`fixed_pri_arb` 中，若 `req = 4'b1010`，`grant` 是多少？  
**答案**：`grant = 4'b0010`（编号最小的有效位是 bit1）。

**练习 2**：固定优先级仲裁器最大的缺点是什么？轮询仲裁器如何缓解？  
**答案**：固定优先级会让高优先级者在持续请求时使低优先级者「饿死」；轮询靠指针循环把每次授权者排到队尾，保证长期公平。

**练习 3**：`round_robin_arb` 复位时 `pre_req` 为什么设成 `0001` 而不是全 0？  
**答案**：`pre_req` 是 one-hot 搜索起点，全 0 会让 `{req,req}-pre_req` 的位运算退化（base 不能为 0）；设成 bit0 表示从最低位开始搜索。

---

### 4.3 pop_cnt：数 1 的个数（population count）

#### 4.3.1 概念说明

`pop_cnt`（population count，人口计数）统计一个向量里 1 的个数。GPU 里到处需要它：统计一个掩码里有几个 lane 活跃、一组请求里有几个有效、共享内存一次访问命中了几个 bank、CAM 比较结果里有几个候选。它是典型的「输入 N 位、输出 ⌈log₂(N+1)⌉ 位」的纯组合函数。

#### 4.3.2 核心流程

最直观的实现是「把每一位当成 0 或 1 的数，全部加起来」。硬件上展开成一条组合加法链（前缀和）：

```
count[1] = data[0] + data[1]
count[i+1] = count[i] + data[i+1]      // 逐位累加
data_o   = count[N-1]                    // 总和
```

`common_cell/pop_cnt.v` 正是这样用 `generate for` 把加法链铺开，输出位宽 `DATA_WID` 需 ≥ ⌈log₂(DATA_LEN+1)⌉（默认 DATA_LEN=4 → DATA_WID=3）。

#### 4.3.3 源码精读

加法链在 [src/common_cell/pop_cnt.v:28-38](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/common_cell/pop_cnt.v#L28-L38)：

```verilog
generate for(i=0;i<DATA_LEN-1;i=i+1) begin:B1
  always @(*) begin
    if(i == 0)
      count[DATA_WID*(i+1)-1 -: DATA_WID] = data_i[i] + data_i[i+1];      // 头两位相加
    else
      count[DATA_WID*(i+1)-1 -: DATA_WID] = count[DATA_WID*i-1 -: DATA_WID] + data_i[i+1]; // 累加下一位
  end
end
```

最终输出取累加链末端（[src/common_cell/pop_cnt.v:40](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/common_cell/pop_cnt.v#L40)）。`count` 是一段被 part-select 切成多份的打包寄存器，每份 `DATA_WID` 位装一个中间和。

> 典型复用：`shared_memory`/`bankconflict_arb` 用它数一次访问里几个 lane 命中同一 bank，以判定是否发生 bank 冲突（[u6-l2](u6-l2-shared-memory.md)）。

#### 4.3.4 代码实践

**实践目标**：验证 `pop_cnt` 对几组输入的计数结果。

**操作步骤**：取 `DATA_LEN=8, DATA_WID=4`，手算下列输入的 `data_o`，再（可选）写 4 行 TB 核对：
- `8'b00000000` → 0
- `8'b10110010` → 4
- `8'b11111111` → 8

**预期结果**：分别 0、4、8。**待本地验证**：实际波形请用仿真确认。

#### 4.3.5 小练习与答案

**练习 1**：`DATA_LEN=8` 时，`DATA_WID` 至少要多少位？  
**答案**：最多 8 个 1，需要表示 0~8，故 ≥ ⌈log₂9⌉ = 4 位。

**练习 2**：如果要数一个 32 位掩码里活跃 lane 的个数，直接例化 `pop_cnt` 的参数怎么填？  
**答案**：`.DATA_LEN(32), .DATA_WID(6)`（6 位可表示 0~32）。

---

### 4.4 find_first 与编码转换单元

#### 4.4.1 概念说明

最后一组是三个「位级小工具」，常和仲裁器/cache 配套使用：

- **`find_first`**：在一个向量里找到「第一个」等于目标值（1 或 0）的位，返回它的位置编码。典型用途是「找空闲槽位」「找第一个有效请求」。
- **`bin2one`**：二进制 → 独热码（binary to one-hot）。给一个编号，输出对应位为 1 的独热向量。常用于「把选中的编号变成 MUX 的选择信号」。
- **`one2bin`**：独热码 → 二进制（one-hot to binary），`bin2one` 的逆运算。常用于「把仲裁出的 grant 还原成编号」。

三者全是纯组合，输入输出一一对应、无状态。

#### 4.4.2 核心流程

```
find_first(data, target)：从 MSB 向 LSB 扫描，记录第一个等于 target 的位
                          （多个匹配时取最靠 MSB 的那个），输出位置编码。
bin2one(bin)             ：oh = 1 << bin            // 编号 bin 的那一位置 1
one2bin(oh)              ：bin = oh 中唯一为 1 的那位的位置
```

#### 4.4.3 源码精读

**(a) `find_first.v`**

它用一个 `generate` 链从 LSB(`i=0`) 扫到 MSB，每遇到一个匹配位就把结果更新为 `DATA_WIDTH-1-i`，扫描结束时**存活下来的是最大 `i` 的匹配**（即从 MSB 看下来第一个匹配）（[src/common_cell/find_first.v:31-34](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/common_cell/find_first.v#L31-L34)）：

```verilog
generate for(i=0;i<DATA_WIDTH;i=i+1) begin:B1
  assign data_range[i+1] = (data_i[i] == target) ? DATA_WIDTH-1-i : data_range[i];
end
```

`target` 由输入指定（[src/common_cell/find_first.v:23](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/common_cell/find_first.v#L23)），`target=1` 找第一个 1、`target=0` 找第一个 0。注意它返回的编码是 `DATA_WIDTH-1-位下标`（即「MSB 对应 0」的反序下标），调用方需按此约定解读。

> 典型复用：dcache 的 `get_entry_status_req/rsp`、icache 的 `get_entry_status` 用它找 MSHR 里第一个空闲 entry（[u3-l2](u3-l2-fetch-and-icache.md)、[u6-l1](u6-l1-dcache-and-mshr.md)）。

**(b) `bin2one.v`**

一行移位（[src/common_cell/bin2one.v:25](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/common_cell/bin2one.v#L25)）：

```verilog
assign oh = ({{(ONE_WIDTH-1){1'b0}},1'b1}<<bin);   // 1 左移 bin 位
```

**(c) `one2bin.v`**

思路：对每个位 `i`，若 `oh[i]=1` 则贡献编号 `i`，否则贡献 0；由于独热码只有一位为 1，把这些贡献「按位 OR」就还原出编号（[src/common_cell/one2bin.v:29-44](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/common_cell/one2bin.v#L29-L44)）：

```verilog
generate for(i=0;i<ONE_WIDTH;i=i+1) begin:B1
  assign bin_temp1[i] = oh[i] ? i : 'b0;      // 命中位贡献其编号
end
// 再把每一位贡献的编号按二进制位拆开、逐位 OR，得到最终 bin
```

> 典型复用：`l1cache_arb` 用 `one2bin` 把仲裁出的 one-hot grant 转成 binary，作为 MUX 下标把选中路的字段接到对外接口；响应方向再用 `bin2one` 把 source 标签解复用回 one-hot（[u6-l3](u6-l3-l1cache-arbiter.md)）。`tag_access` 等模块也大量使用二者。

#### 4.4.4 代码实践

**实践目标**：理解 one-hot 与 binary 的互转，并看清它们在 `l1cache_arb` 里如何与 `fixed_pri_arb` 配套。

**操作步骤**：
1. 手算：`bin2one` 输入 `bin=2`（`BIN_WIDTH=2,ONE_WIDTH=4`）→ `oh` = `4'b0100`；再用 `one2bin` 把 `4'b0100` 转回去 → `bin=2`。
2. 打开 [l1cache_arb.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache_arb.v)，找到 `fixed_pri_arb` 选出 one-hot grant、`one2bin` 转成 binary 下标、驱动 MUX 选中路字段的片段（参考 [u6-l3](u6-l3-l1cache-arbiter.md) 的描述）。

**需要观察的现象**：grant（one-hot）→ binary 下标 → MUX 选择，三者如何串成一条「仲裁到选路」的链。

**预期结果**：请求方向 = `fixed_pri_arb` 出 one-hot → `one2bin` 出 binary → 用作 MUX 选择信号；响应方向 = source 标签 → `bin2one` 还原 one-hot → 选目标路。**待本地验证**：具体信号名以源码为准。

#### 4.4.5 小练习与答案

**练习 1**：`one2bin` 的输入若不是合法独热码（例如 `4'b1010`，两位为 1），输出会是什么？还能用吗？  
**答案**：会把两个命中位的编号「按位 OR」叠加（bit1 和 bit3 → `01 | 11 = 11`），结果无意义。所以 `one2bin` 只能在保证输入独热的前提下使用。

**练习 2**：`find_first` 的 `target` 接 1 和接 0 分别用于什么场景？各举一例。  
**答案**：`target=1` 找第一个有效（如第一个有请求的 entry）；`target=0` 找第一个空闲（如 MSHR 里第一个空 entry）。

**练习 3**：为什么说 `bin2one`/`one2bin` 是 `fixed_pri_arb` 的天然搭档？  
**答案**：仲裁器输出 one-hot grant，但 MUX 选择端常需要 binary 下标，用 `one2bin` 转一下即可；反向路由时再用 `bin2one` 把编号变回 one-hot 去点选目标。

## 5. 综合实践

**任务**：以 `l1cache_arb.v` 为对象，做一次「零件库组装图」的源码阅读，把本讲四类单元串起来。

`l1cache_arb`（SM 内 L1 缓存仲裁器，详见 [u6-l3](u6-l3-l1cache-arbiter.md)）几乎是 `common_cell` 的「全家桶示例」：它把 icache 与 dcache 两路请求，用 `fixed_pri_arb` 仲裁、用 `one2bin` 把 grant 转 binary 作 MUX 下标、把请求字段接到对外 A 通道；响应方向再用 `bin2one`（或等价逻辑）按 source 标签解复用回对应 cache。

请完成：

1. **画组装图**：在 [l1cache_arb.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache_arb.v) 中标出 `fixed_pri_arb`、`one2bin`/`bin2one`（或同类转换）各自的实例，画出「请求：仲裁→选路」「响应：解标签→分发」两条数据流。
2. **回答**：为什么请求方向用**固定优先级**而不是轮询？（提示：取指 vs 访存的优先级谁该让谁，参考 [u6-l3](u6-l3-l1cache-arbiter.md)）
3. **延伸**（可选）：仿照该结构，写一个最小 2 输入的仲裁 + 选路模块，复用 `fixed_pri_arb` 与 `one2bin`，把两路 32 位数据按优先级选一路输出，并用一个 4 拍的小 TB 验证（标记为示例代码，结果待本地验证）。

> 提示：若想看「FIFO + 仲裁器」的另一种组合，可读 `inflight_wg_buffer`（用 `round_robin_arb` 公平选 workgroup、用 FIFO/缓冲回收，见 [u2-l2](u2-l2-cu-handler-and-inflight-wg.md)）。

## 6. 本讲小结

- `common_cell` 是全项目共享的标准零件库，FIFO 家族、仲裁器、位计数/编码转换四大类几乎被每个子系统调用（数十文件、近百度引用）。
- `fifo.v` 用「指针多留一位绕回位」判满判空；`stream_fifo` 系列在它之上靠改 push/pop/ready 一行逻辑，派生出标准型、流水穿透（`pipe_true`）、旁路（`flow_true`）、SRAM 大容量（`useSRAM`）等变体。
- 选型要点：小/快用触发器版 `stream_fifo`/`stream_fifo_pipe_true`；大/深用 `stream_fifo_useSRAM`；要切断组合长路径用 `pipe_true`。
- `fixed_pri_arb` 是纯组合、编号小者优先，适合有天然优先级的场景；`round_robin_arb` 是时序、靠指针循环保证公平，适合多方平等争用。
- `pop_cnt` 数 1、`find_first` 找首个匹配位、`bin2one`/`one2bin` 在独热与二进制间互转，三者常与仲裁器/cache 配套，是 cache 行选择、MSHR 空闲查找、bank 冲突判定的基础积木。

## 7. 下一步学习建议

- 想看 FIFO 如何「削峰」：回顾 [u2-l2](u2-l2-cu-handler-and-inflight-wg.md) 中 `wf_done_interface_single` 用 `stream_fifo` 缓冲集中完成的 wg_done。
- 想看仲裁器在存储子系统里的实战：精读 [u6-l3 L1 cache 仲裁](u6-l3-l1cache-arbiter.md) 与 [u7-l3 cluster 到 L2 互联](u7-l3-cluster-l2-interconnect.md)，观察 `sm2cluster_arb`、`cluster_to_l2_arb` 如何叠成多级仲裁网络。
- 想做二次开发：当你要给新模块加缓冲或共享端口时，**先查 `common_cell/` 是否已有现成单元**，避免重复造轮子；这与 [u8-l4 指令集扩展](u8-l4-isa-extension.md) 中「先复用、再新增」的工程思路一致。
