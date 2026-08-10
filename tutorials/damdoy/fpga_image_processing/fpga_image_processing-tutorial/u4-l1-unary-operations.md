# 逐像素运算：STATE_PROC_UNARY

## 1. 本讲目标

本讲深入 `image_processing.v` 的运算 FSM（`state_processing`）中最基础的一类状态——`STATE_PROC_UNARY`（逐像素运算）。它负责对 **storage 缓冲**里的图像做「读一个像素→运算→写回同一个像素」的处理，覆盖加法（add）、阈值（threshold）、取反（invert）、乘法（mult）四种操作。

学完后你应该能够：

- 说清为什么 `STATE_PROC_UNARY` 要用 **两拍流水线**（先发读、再运算写回），以及 `proc_memory_addr_counter[0]` 如何在两拍之间切换。
- 解释一个 16 位存储字如何被 **同时**当作两个 8 位像素来处理（高字节、低字节并行运算）。
- 看懂 `processing_command` 是如何在同一个 `STATE_PROC_UNARY` 状态里分派出 add / threshold / invert / mult 四条不同运算分支的。
- 理解「读出 → 运算 → 写回同一个字地址」这种 in-place（原地）更新是如何用一行 `addr <= {proc_memory_addr_counter[31:1], 1'b0}` 实现的。

## 2. 前置知识

本讲假设你已经学过以下内容（若没有，请先阅读对应讲义）：

- **u3-l2 双缓冲存储模型与 16 位像素打包**：你需要知道片上 128KB 存储被切成 input / storage 两个 64KB 缓冲；每个 16 位存储字里 **打包了 2 个 8 位像素**（低字节 `[7:0]` 是一个像素，高字节 `[15:8]` 是下一个像素）；图像像素总数为偶数。
- **u3-l3 主命令处理状态机**：你需要知道模块里有两条并行 FSM——`state`（命令解析）和 `state_processing`（运算执行）；交接运算时会把命令码写进 `processing_command` 寄存器，并把 `state_processing` 设为某个运算状态，从而让运算 FSM 接管。

几个本讲会反复用到的关键事实：

| 概念 | 一句话说明 |
|------|-----------|
| 单端口 RAM（single-port RAM） | 同一个存储端口，同一时钟周期只能读 **或** 写，不能同时进行；且读地址给出后，数据要 **下一拍** 才出现在 `data_read` 上（读延迟 = 1 拍）。 |
| `data_read_valid` | 由存储器端（仿真后端的 C++ 数组逻辑，或硬件后端的 SPRAM 接口）置位的握手信号，表示「`data_read` 上现在是有效数据」。 |
| in-place（原地）更新 | 读出某地址的数据→运算→把结果写回 **同一地址**，不额外占用第二块缓冲。 |
| busy 位 | `state_processing != STATE_IDLE` 即 busy，主机靠 `wait_end_busy()` 轮询它来等待运算结束。 |

> 一句话直觉：`STATE_PROC_UNARY` 就是一个「在 storage 缓冲上原地扫描、每读一个字就顺手把两个字节能做的运算都做完、再写回去」的循环。它之所以拆成两拍，纯粹是被「单端口 RAM 读延迟 1 拍」这个物理约束逼出来的。

## 3. 本讲源码地图

本讲几乎全部内容都集中在一个文件里：

| 文件 | 作用 |
|------|------|
| [hdl/image_processing.v](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v) | 唯一的核心 HDL 模块。`STATE_PROC_UNARY` 状态机、四种运算分支、两拍流水、写回逻辑全在这里。 |

为了讲清楚「谁来启动 `STATE_PROC_UNARY`」和「主机怎么调用」，还会少量引用：

| 文件 | 作用 |
|------|------|
| [software/image_processing.hpp](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/image_processing.hpp) | 主机侧抽象接口，`send_add` / `send_threshold` / `send_image_invert` / `send_mult` 四个虚函数对应四种逐像素运算。 |
| [software/main.cpp](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp) | 主机测试程序，`test_add_threshold` 和 `test_multiplication` 给出真实的调用序列，用于实践环节。 |

---

## 4. 核心概念与源码讲解

本讲按四个最小模块展开：

1. **单端口 RAM 读延迟与两拍流水线**（`proc_memory_addr_counter` 奇偶切换）
2. **`STATE_PROC_UNARY` 主循环与计数器终止**
3. **`processing_command` 的四条运算分支**
4. **写回到同一地址：`data_read` → `data_write` 的 in-place 更新**

### 4.1 单端口 RAM 读延迟与两拍流水线

#### 4.1.1 概念说明

「逐像素运算」听起来简单——读一个像素、算一下、写回去。但在硬件里，存储器并不是「给地址立刻吐数据」的。本模块面对的是 **单端口 RAM**：

- **单端口**：一个端口，同一拍只能读或只能写，不能又读又写。
- **读延迟**：你在第 \(N\) 拍把地址送上 `addr` 并拉高 `rd_en`，数据要到 **第 \(N+1\) 拍** 才出现在 `data_read` 上，并由对端用 `data_read_valid` 告诉你「现在有效」。

这意味着「读」和「用读到的数据做运算」不可能在同一拍完成，必须分成两拍：

- **第 1 拍（发起读）**：送地址、拉高 `rd_en`。
- **第 2 拍（运算 + 写回）**：等 `data_read_valid`，拿到 `data_read`，做运算，再拉高 `wr_en` 写回。

那么同一个状态怎么知道「现在该读还是该算」？答案就在地址计数器的 **最低位**。

#### 4.1.2 核心流程

模块用一个地址计数器 `proc_memory_addr_counter` 同时充当「地址发生器」和「节拍器」。它从 `buffer_storage_address`（一个偶数，见 4.1.3）开始，每拍 +1。于是它的最低位 `proc_memory_addr_counter[0]` 会在 0/1 之间交替，天然把循环切成奇偶两拍：

```
偶拍 (addr[0]==0)：发起读
    rd_en <= 1
    addr  <= proc_memory_addr_counter
    proc_memory_addr_counter += 1     // → 变成奇数

奇拍 (addr[0]==1)：运算并写回
    等 data_read_valid
    用 data_read 算出 data_write
    wr_en <= 1
    addr  <= {proc_memory_addr_counter[31:1], 1'b0}   // 清掉最低位 → 写回刚才读的那个字
    proc_memory_addr_counter += 1     // → 变成偶数，进入下一个字的偶拍
```

换句话说，地址每 +2 就处理完一个 16 位字（= 2 个像素）。偶拍负责「要数据」，奇拍负责「用数据」，两者交替形成一条稳定的两拍流水。

> 为什么用最低位当节拍器，而不是再设一个独立的 `step` 寄存器？因为地址本来就按字递增、最低位天然 0/1 交替，复用它既省一个寄存器，又保证了「读地址」和「写地址」用同一个计数器推导，二者天然对齐。

#### 4.1.3 源码精读

`STATE_PROC_UNARY` 的开头就是这套奇偶判断。注意它假设单端口 RAM（注释 `assume single port ram`）：

[hdl/image_processing.v:506-514](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L506-L514) —— `STATE_PROC_UNARY` 状态入口，用 `proc_memory_addr_counter[0]` 区分「发起读」（偶拍）与「运算写回」（奇拍）。

```verilog
STATE_PROC_UNARY: begin : unary
   reg [15:0] temp_calc; //used for calculations
   //assume single port ram, reads the data
   if(proc_memory_addr_counter[0] == 1'b0) begin
      rd_en <= 1;
      addr <= proc_memory_addr_counter; //set by previous state to be either buffers
      proc_memory_addr_counter <= proc_memory_addr_counter+1;
   end else begin
      if (data_read_valid == 1'b1) begin //received the data, apply the unary operation
         ... // 四条运算分支 + 写回，见 4.3 / 4.4
```

那么 `proc_memory_addr_counter` 的初值从哪来？它在 **命令派发** 时被设成 `buffer_storage_address`。以「取反」命令为例，派发发生在 `STATE_WAIT_COMMAND` 里：

[hdl/image_processing.v:262-269](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L262-L269) —— `COMMAND_APPLY_INVERT` 直接（无需读参数）把运算交给 `STATE_PROC_UNARY`，并把 `proc_memory_addr_counter` 初始化为 `buffer_storage_address`。

```verilog
COMMAND_APPLY_INVERT: begin
   state <= STATE_WAIT_COMMAND;
   state_processing <= STATE_PROC_UNARY;
   processing_command <= COMMAND_APPLY_INVERT;
   counter_read <= 0;
   proc_counter_read <= img_width*img_height;
   proc_memory_addr_counter <= buffer_storage_address;
end
```

`buffer_storage_address` 在 `COMMAND_PARAM` 里被设成 `BUFFER2_LOCATION`（= `MEMORY_SIZE/2` = 65536），是一个 **偶数**，最低位为 0，所以循环从「偶拍 = 发起读」干净地起步：

[hdl/image_processing.v:225-230](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L225-L230) —— `COMMAND_PARAM` 把 `buffer_storage_address` 初始化为 `BUFFER2_LOCATION`（偶数），保证 `STATE_PROC_UNARY` 起步于偶拍。

```verilog
COMMAND_PARAM: begin //also acts as init
   state <= STATE_READ_COMMAND_PARAM_WIDTH;
   counter_read <= 1;
   buffer_storage_address <= BUFFER2_LOCATION;
   buffer_input_address <= 0;
end
```

#### 4.1.4 代码实践

**实践目标**：亲手验证「偶拍发起读、奇拍运算写回」的两拍节拍，并理解 `COMMAND_APPLY_INVERT` 为什么能用一行 `~data_read` 同时取反两个像素。

**操作步骤（源码阅读型）**：

1. 打开 [hdl/image_processing.v:506-514](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L506-L514)，确认偶拍只做 `rd_en<=1; addr<=...; counter+=1` 三件事，**不碰** `wr_en`、**不碰** `data_write`。
2. 假设 `buffer_storage_address = 65536`，在纸上推演前 4 拍 `proc_memory_addr_counter` 的取值：`65536(偶) → 65537(奇) → 65538(偶) → 65539(奇)`。标出每一拍是「读」还是「写」，以及 `addr` 上的值。
3. 打开 [hdl/image_processing.v:538-539](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L538-L539)（INVERT 分支）：
   ```verilog
   end else if(processing_command == COMMAND_APPLY_INVERT) begin
      data_write <= ~data_read;
   end
   ```
   思考：`data_read` 是 16 位 = `{像素B, 像素A}`（高字节是 B、低字节是 A）。按位取反 `~{B, A} = {~B, ~A}`。对无符号 8 位数有 \(\sim p = 255 - p\)，所以 `{255-B, 255-A}`——两个字节的取反在 **一条语句、一个时钟沿** 内同时完成。

**需要观察的现象**：

- 偶拍 `wr_en` 为 0（默认值），奇拍 `wr_en` 才被置 1。
- 每经过一个「偶→奇」对，`addr` 的写回值（清掉最低位后）恰好等于偶拍发出的读地址——即写回的就是刚读出来的那个字。

**预期结果**：你能用一句话回答「为什么用 `proc_memory_addr_counter[0]` 区分两拍」——因为单端口 RAM 读延迟 1 拍，必须把「要数据」和「用数据」分到相邻两拍，而地址计数器每拍 +1、最低位天然 0/1 交替，正好充当节拍器。你也能解释 `~data_read` 同时取反两像素：16 位按位取反独立作用于高低两个 8 位像素。

> 运行验证（可选，待本地验证）：若有 Verilator 环境，可在 `STATE_PROC_UNARY` 的偶拍与奇拍各加一行 `$display`（仅仿真可见），打印 `proc_memory_addr_counter` 与 `rd_en/wr_en`，观察它们的交替节律。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `proc_memory_addr_counter` 的初值设成一个 **奇数**，第一拍会发生什么？

**答案**：第一拍 `proc_memory_addr_counter[0]==1`，会直接进入奇拍分支去等 `data_read_valid`，但此时根本没有发起过任何读请求，`data_read` 上是旧数据/无效数据。流水起步错位，运算结果错误。这就是为什么初始化必须保证起步于偶拍（`buffer_storage_address` 是偶数）。

**练习 2**：为什么偶拍不直接把 `wr_en` 也置 1，把读和写合并到一拍？

**答案**：因为存储器是 **单端口**，同一拍不能又读又写；而且读到的数据要等到下一拍才有效（`data_read_valid`），本拍根本没有可写回的运算结果。

---

### 4.2 STATE_PROC_UNARY 主循环与计数器终止

#### 4.2.1 概念说明

知道了两拍节拍之后，还要回答两个问题：

1. 这个循环要跑多少个字（多少拍）才把整幅图处理完？
2. 跑完之后怎么停下来、怎么告诉主机「我不忙了」？

模块用第二个计数器 `proc_counter_read` 来回答第一个问题，用「回到 `STATE_IDLE`」来回答第二个问题（busy 位正是 `state_processing != STATE_IDLE`）。

#### 4.2.2 核心流程

初始化时（见 4.1.3 的派发代码），`proc_counter_read` 被设成 **像素总数** `img_width*img_height`。因为每个 16 位字包含 2 个像素，所以每处理完一个字（即每经过一次奇拍写回），`proc_counter_read` 减 2：

```
奇拍写回时：
    if (proc_counter_read > 2)
        proc_counter_read -= 2     // 还有更多字要处理
    else
        state_processing <= STATE_IDLE   // 最后一个字写完，结束
```

终止条件是 `proc_counter_read > 2` 而不是 `> 0`：因为每字对应 2 个像素，从偶数起始值每次减 2，处理到最后一个字时 `proc_counter_read` 恰好降到 2，此时 `2 > 2` 不成立，于是在 **写回最后一个字的那一拍** 同时跳回 `STATE_IDLE`。源码注释里的「shifted by one」指的就是这种「边写最后一个字边结束」的错拍。

一旦 `state_processing` 回到 `STATE_IDLE`，busy 位（`~(state_processing==STATE_IDLE)`）随即清零，主机的 `wait_end_busy()` 便解除阻塞。

#### 4.2.3 源码精读

`proc_counter_read` 与 `proc_memory_addr_counter` 的声明：

[hdl/image_processing.v:94-95](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L94-L95) —— 运算 FSM 专用的两个计数器：`proc_counter_read`（剩余像素数）、`proc_memory_addr_counter`（当前字地址 + 节拍器）。

```verilog
reg [15:0] proc_counter_read;
reg [31:0] proc_memory_addr_counter;
```

终止与减计数逻辑位于奇拍写回段的末尾：

[hdl/image_processing.v:553-557](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L553-L557) —— 每写回一个字（= 2 像素）`proc_counter_read` 减 2；不大于 2 时回到 `STATE_IDLE`，busy 位随之清零。

```verilog
if(proc_counter_read > 2) begin // > 2 and not 0 because we are shifted by one due to clk assignment
   proc_counter_read <= proc_counter_read - 2;
end else begin
   state_processing <= STATE_IDLE;
end
```

busy 位本身在 `STATE_GET_STATUS` 里生成，取值就是「运算 FSM 是否不在空闲态」：

[hdl/image_processing.v:312-315](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L312-L315) —— 状态回传字节的 bit0 = busy = `~(state_processing==STATE_IDLE)`，即只要 `STATE_PROC_UNARY` 还在跑，主机就看到 busy=1。

```verilog
if(counter_read == 3) begin //first status response is "is_busy"
   comm_data_out_valid <= 1;
   comm_data_out[7:0] <= 8'h0;
   comm_data_out[0] <= ~(state_processing == STATE_IDLE);
end
```

四种逐像素运算命令的派发 **都** 用同一套初始化（`proc_counter_read <= img_width*img_height; proc_memory_addr_counter <= buffer_storage_address`），区别只在 `processing_command`。带参数的三种（add/threshold/mult）在各自的 `*_READ_PARAM` 状态读完末字节后交接，无参数的 invert 在 `STATE_WAIT_COMMAND` 里当场交接。例如 add 命令的交接：

[hdl/image_processing.v:363-370](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L363-L370) —— `STATE_APPLY_ADD_READ_PARAM` 读到 clamp 位那一拍，把 `processing_command<=COMMAND_APPLY_ADD`、`state_processing<=STATE_PROC_UNARY`，并完成与 invert 完全相同的计数器初始化。

```verilog
end else begin
   clamp <= comm_data_in[0];
   state_processing <= STATE_PROC_UNARY;
   processing_command <= COMMAND_APPLY_ADD;
   state <= STATE_WAIT_COMMAND;
   proc_counter_read <= img_width*img_height;
   proc_memory_addr_counter <= buffer_storage_address;
end
```

#### 4.2.4 代码实践

**实践目标**：用一个小例子手算 `proc_counter_read` 的变化，确认 `> 2` 这个边界刚好在「写完最后一个字」时停机。

**操作步骤（源码阅读型）**：

1. 假设一幅极小图像 `img_width*img_height = 4`（即 2 个 16 位字）。
2. 起始 `proc_counter_read = 4`，推演每个奇拍写回后的值：
   - 第 1 个字写回：`4 > 2` 成立 → `proc_counter_read = 2`。
   - 第 2 个字写回：`2 > 2` 不成立 → `state_processing <= STATE_IDLE`（停机，且本拍仍完成了第 2 个字的写回）。
3. 核对：一共处理 2 个字 = 4 个像素，与图像大小吻合。

**预期结果**：处理字数 = `img_width*img_height / 2`；总耗时约为「字数 × 2 拍」（每个字一偶一奇）。

**待本地验证**：在仿真里给一幅 4×1 的小图发 `COMMAND_APPLY_INVERT`，数一下 `state_processing` 从进入 `STATE_PROC_UNARY` 到回到 `STATE_IDLE` 的时钟周期数，验证是否约为 `字数×2` 量级（受握手与初始化拍数影响会有少量出入）。

#### 4.2.5 小练习与答案

**练习 1**：如果把终止条件改成 `proc_counter_read > 0`，会发生什么？

**答案**：会多处理一个字。以起始值 4 为例：第 1 字写回后 `4>0` → 2；第 2 字写回后 `2>0` → 0；此时还会进入第 3 个字的偶拍去读一个 **不属于本图** 的地址并写回，造成越界处理。所以必须用 `> 2`（每字 2 像素）来对齐边界。

**练习 2**：busy 位是在主 FSM（`state`）还是运算 FSM（`state_processing`）上取的？为什么这样设计？

**答案**：取自运算 FSM（`~(state_processing==STATE_IDLE)`）。因为逐像素/双图/卷积等长运算都跑在 `state_processing` 里，而主 FSM 在运算期间仍回到 `STATE_WAIT_COMMAND` 可以响应状态查询。把 busy 绑在 `state_processing` 上，才能准确反映「运算是否真的结束」。

---

### 4.3 processing_command 的四条运算分支

#### 4.3.1 概念说明

`STATE_PROC_UNARY` 是一个 **被复用** 的状态：add、threshold、invert、mult 四种完全不同的运算共用同一段「两拍流水」骨架，靠 `processing_command` 寄存器在奇拍里选择具体算什么。这是一种典型的「控制寄存器分派 + 共用数据通路」设计——流水线骨架只写一遍，运算分支各自独立。

四个分支的特点对比：

| 命令 | 主机接口 | 运算（对每个像素） | 是否用钳位 | 钳位函数 |
|------|----------|-------------------|-----------|----------|
| `COMMAND_APPLY_ADD` | `send_add(value, clamp)` | `pixel + add_value` | 是（可选） | `apply_clamp` |
| `COMMAND_APPLY_THRESHOLD` | `send_threshold(value, replacement, upper)` | 条件替换为 `replacement` | 否（自带边界） | — |
| `COMMAND_APPLY_INVERT` | `send_image_invert()` | `255 - pixel`（即按位取反） | 否 | — |
| `COMMAND_APPLY_MULT` | `send_mult(float value, clamp)` | `pixel * mult_value_param`（定点） | 是（可选） | `apply_clamp_fixed16` |

#### 4.3.2 核心流程

奇拍拿到 `data_read`（16 位 = 两像素）后，进入 `if/else if` 链按 `processing_command` 分派：

```
if    (COMMAND_APPLY_ADD)       → 两像素各 + add_value，apply_clamp 钳到 [0,255]
else if (COMMAND_APPLY_THRESHOLD) → 先整字复制，再把满足条件的像素改成 replacement
else if (COMMAND_APPLY_INVERT)    → data_write <= ~data_read（两像素同时取反）
else if (COMMAND_APPLY_MULT)      → 两像素各 * mult_value_param，apply_clamp_fixed16 还原定点
```

关键点：**高字节 `[15:8]` 和低字节 `[7:0]` 是分别、独立运算的**，从而在一个时钟沿内并行处理两个像素。这正是「16 位字打包 2 像素」带来的吞吐翻倍。

#### 4.3.3 源码精读

完整的四分支位于奇拍内：

[hdl/image_processing.v:516-545](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L516-L545) —— 按 `processing_command` 分派四种运算；注意每个分支都把 `data_read[7:0]` 与 `data_read[15:8]` 各算一次，分别写回 `data_write` 的低/高字节。

**ADD 分支**（先把像素零扩展到 16 位，加 `add_value`，再用 `apply_clamp` 钳位）：

```verilog
if(processing_command == COMMAND_APPLY_ADD)begin
   temp_calc = {8'b0, data_read[7:0]}+add_value;
   data_write[7:0] <= apply_clamp(temp_calc, clamp);
   temp_calc = {8'b0, data_read[15:8]}+add_value;
   data_write[15:8] <= apply_clamp(temp_calc, clamp);
end
```

> `temp_calc` 是在命名块 `unary` 内用 `reg` 声明的局部变量（[第 507 行](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L507)），用阻塞赋值 `=` 串行复用：先算低字节、再算高字节。两个像素共用同一个 `add_value`（README 说明它是 16 位有符号值，所以加法可正可负，超出 `[0,255]` 时由 clamp 兜底）。

**THRESHOLD 分支**（先整字照抄，再按条件替换；`threshold_upper` 决定是「≥ 替换」还是「≤ 替换」）：

```verilog
end else if(processing_command == COMMAND_APPLY_THRESHOLD) begin
   data_write <= data_read;                       // 默认照抄
   if(threshold_upper == 1) begin                 // upper: pixel >= value 者替换
      if(data_read[7:0] >= threshold_value)  data_write[7:0]  <= threshold_replacement;
      if(data_read[15:8] >= threshold_value) data_write[15:8] <= threshold_replacement;
   end else begin                                 // 否则: pixel <= value 者替换
      if(data_read[7:0] <= threshold_value)  data_write[7:0]  <= threshold_replacement;
      if(data_read[15:8] <= threshold_value) data_write[15:8] <= threshold_replacement;
   end
end
```

> 注意 `data_write <= data_read` 是整字非阻塞赋值，随后对单字节的覆盖也是非阻塞：在同一个时钟沿，后写的字节会「赢」，所以被命中的像素改成 `replacement`、未命中的保持原值。两个像素独立判断。

**INVERT 分支**（最简洁——整字按位取反，两像素一次性完成）：

```verilog
end else if(processing_command == COMMAND_APPLY_INVERT) begin
   data_write <= ~data_read;
end
```

**MULT 分支**（像素 × 8 位定点系数，乘积 16 位，用 `apply_clamp_fixed16` 取 `[11:4]` 还原 4 位小数）：

```verilog
end else if(processing_command == COMMAND_APPLY_MULT) begin
   temp_calc = {8'b0, mult_value_param}*{8'b0, data_read[7:0]};
   data_write[7:0] <= apply_clamp_fixed16(temp_calc, clamp);
   temp_calc = {8'b0, mult_value_param}*{8'b0, data_read[15:8]};
   data_write[15:8] <= apply_clamp_fixed16(temp_calc, clamp);
end
```

> MULT 用的是 `apply_clamp_fixed16` 而不是 `apply_clamp`，因为乘法结果带有 4 位定点小数，需要右移 4 位（取 `[11:4]`）还原成整数像素。定点数与这两个钳位函数的细节是下一讲 u4-l2 的主题，这里只需记住「MULT 的结果多了一步定点还原」。

两个钳位函数的定义（供对照）：

[hdl/image_processing.v:151-163](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L151-L163) —— `apply_clamp`：取低 8 位，开启 clamp 时把有符号值超出 `[0,255]` 的部分夹到 0 或 255。

[hdl/image_processing.v:166-178](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L166-L178) —— `apply_clamp_fixed16`：取 `[11:4]`（等价于把 16 位定点乘积右移 4 位），同样可选钳位，专供定点乘法用。

主机侧四个接口的声明：

[software/image_processing.hpp:19-22](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/image_processing.hpp#L19-L22) —— `send_add` / `send_threshold` / `send_image_invert` / `send_mult` 四个纯虚函数，分别对应上述四个分支。

```cpp
virtual void send_add(int16_t value, bool clamp) = 0;
virtual void send_threshold(uint8_t threshold_value, uint8_t replacement, bool upper_selection) = 0;
virtual void send_image_invert() = 0;
virtual void send_mult(float value, bool clamp) = 0;
```

#### 4.3.4 代码实践

**实践目标**：对照四种运算，看清「同一个 `STATE_PROC_UNARY`、不同的 `processing_command`」是如何在一份主机测试里被串起来的。

**操作步骤（源码阅读型）**：

1. 打开 [software/main.cpp:38-73](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L38-L73)（`test_add_threshold`），它连续做了 **两次** 逐像素运算：
   - `send_add(32, true)`（[第 52 行](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L52)）：每个像素 +32，开启钳位 → `COMMAND_APPLY_ADD`。
   - `wait_end_busy()`：等第一次 `STATE_PROC_UNARY` 跑完（busy 清零）。
   - `send_threshold(168, 0, 0)`（[第 68 行](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L68)）：`threshold_value=168, replacement=0, upper_selection=0`，即把 `pixel <= 168` 的像素改成 0 → `COMMAND_APPLY_THRESHOLD`。
   - 注意两次运算 **都没有** 重新 `send_image`，而是直接在 storage 缓冲上链式处理——这正是 in-place 更新与「读回写同一地址」带来的好处。
2. 打开 [software/main.cpp:153-165](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L153-L165)（`test_multiplication`），`send_mult(0.5f, true)`（[第 160 行](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L160)）触发 `COMMAND_APPLY_MULT` 分支。

**需要观察的现象**：两次运算之间一定有一次 `wait_end_busy()`；如果没有它，第二次运算命令会在第一次还没写完时抢占 storage 缓冲，造成数据错乱。

**预期结果**：你能复述 add→threshold 两次「读改写」如何在不搬动整图的情况下在 storage 上链式完成。

#### 4.3.5 小练习与答案

**练习 1**：`COMMAND_APPLY_ADD` 分支里，`add_value` 是 16 位，而像素只有 8 位，为什么相加前要 `{8'b0, data_read[7:0]}` 把像素零扩展到 16 位？

**答案**：为了让加法在有符号 16 位 `add_value` 上正确进行（`add_value` 可正可负）。若不扩展，8 位像素直接与 16 位值相加会在位宽上不一致、丢失进位/符号。扩展后用 `temp_calc`（16 位）承接中间结果，再交给 `apply_clamp` 夹回 8 位的 `[0,255]`。

**练习 2**：THRESHOLD 分支里 `threshold_upper==0`、`threshold_value=168`、`replacement=0` 时，哪些像素会变成 0？

**答案**：`threshold_upper==0` 表示「`pixel <= threshold_value` 者替换」。所以所有 `pixel <= 168` 的像素变成 0，只有 `pixel > 168`（较亮）的像素保留原值——这是一种保留高亮区域的高通式阈值。

**练习 3**：为什么 INVERT 分支不需要任何钳位函数？

**答案**：按位取反 `~data_read` 把 8 位无符号像素 \(p\) 映射到 \(255-p\)，结果天然落在 `[0,255]` 内，不可能溢出，所以无需钳位。

---

### 4.4 写回到同一地址：data_read → data_write 的 in-place 更新

#### 4.4.1 概念说明

逐像素运算是 **原地（in-place）** 的：从 storage 缓冲某地址读出一个字、运算后写回 **同一个地址**。这要求奇拍的「写地址」必须等于偶拍的「读地址」。但奇拍时 `proc_memory_addr_counter` 已经 +1 变成了奇数（等于读地址 +1），怎么还原成读地址？

答案就是那行看着有点神秘的拼接：`addr <= {proc_memory_addr_counter[31:1], 1'b0}`——把最低位强行清零。因为偶拍发出的读地址是偶数（最低位为 0），奇拍时计数器 = 读地址 + 1（变成奇数），清掉最低位后正好回到读地址本身。

#### 4.4.2 核心流程

```
偶拍：addr(读) = C          （C 为偶数，最低位 0）
      counter: C → C+1
奇拍：counter 现在 = C+1（奇数）
      addr(写) = {(C+1)[31:1], 1'b0}
               = (C+1) 清掉最低位
               = C            ← 与读地址相同！
      wr_en <= 1
      counter: C+1 → C+2
```

清最低位的数学含义：对一个最低位为 1 的奇数 \(n\)，\(n - 1\) 就是它前面的偶数。清最低位等价于 \(n \mathbin{\&} \sim 1 = n - 1\)（仅当 \(n\) 为奇数时）。所以奇拍写回地址 = 偶拍读地址，完成 in-place 读改写。

由于每个字处理完地址 +2，连续两字的读/写地址分别是 \(C, C+2, C+4,\dots\)，互不重叠，因此不会有「读尚未完成就被覆盖」或「写回与下一次读冲突」的单端口冲突。

#### 4.4.3 源码精读

写回段紧跟在四分支运算之后，对所有四种运算都一样：

[hdl/image_processing.v:547-552](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L547-L552) —— 无论哪个分支，算完 `data_write` 后都用同一行 `addr <= {proc_memory_addr_counter[31:1], 1'b0}` 把结果写回刚读出的那个字地址。

```verilog
//write back the data
wr_en <= 1;
//16bits data addressing
addr <= {proc_memory_addr_counter[31:1], 1'b0};
proc_memory_addr_counter <= proc_memory_addr_counter+1;
```

存储器接口的写端口定义（16 位字宽）：

[hdl/image_processing.v:17-22](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L17-L22) —— `addr`/`wr_en`/`data_write` 是写端口，`data_read`/`data_read_valid` 是读端口；`data_write` 与 `data_read` 都是 16 位，与「一字两像素」一致。

```verilog
output reg [31:0] addr;
output reg wr_en;
output reg rd_en;
output reg [15:0] data_write;
input wire [15:0] data_read;
input wire data_read_valid;
```

#### 4.4.4 代码实践

**实践目标**：亲手验证「写地址 = 读地址」，确认 in-place 读改写的地址对齐。

**操作步骤（源码阅读型）**：

1. 设 `buffer_storage_address = 65536`，推演前两个字（4 个像素）的处理：
   - 字 1：偶拍 `addr(读)=65536`，counter→65537；奇拍 `addr(写)={65537[31:1],0}=65536`，写回字 1，counter→65538。
   - 字 2：偶拍 `addr(读)=65538`，counter→65539；奇拍 `addr(写)={65539[31:1],0}=65538`，写回字 2，counter→65540。
2. 核对每个字的「读地址」与「写地址」是否完全相同（65536→65536，65538→65538）。
3. 思考：为什么读、写地址之间隔了 +2 不会造成单端口冲突？因为单端口每拍只做一件事——偶拍读 65536、奇拍写 65536（不同拍），下一偶拍读 65538（与写 65536 不同地址），全程不冲突。

**预期结果**：你能解释「清最低位」这一行如何用最少的逻辑把奇拍的写地址还原成偶拍的读地址，从而实现 in-place 更新。

**待本地验证**：在仿真里对 `addr`、`wr_en`、`rd_en` 打印波形，确认每个 `wr_en` 脉冲的 `addr` 与上一个 `rd_en` 脉冲的 `addr` 相同。

#### 4.4.5 小练习与答案

**练习 1**：如果把写回地址误写成 `addr <= proc_memory_addr_counter`（不清最低位），会发生什么？

**答案**：奇拍时 counter 是奇数（如 65537），写回地址就变成 65537 而不是 65536。由于存储按 16 位字编址、地址最低位在字内其实无意义（会被存储器忽略或当成字节选择），但更关键的是它破坏了「写回刚读出的那个字」的语义——如果存储器把最低位当字节使能，就可能只写半个字或写错位置，导致图像错乱。清最低位是保证写回整字到正确位置的关键。

**练习 2**：in-place 更新为什么不需要第二块缓冲来存「结果图」？

**答案**：因为读和写在 **不同的时钟拍**（偶拍读、奇拍写），且写回的就是刚读出的地址。下一字读的是新地址（+2），不会读到「本字尚未写回」的旧值。所以一幅图可以在 storage 缓冲上原地被改写，节省了一整块 64KB 缓冲。

---

## 5. 综合实践

把本讲的四个最小模块串起来，完成下面这个端到端的小任务。

**任务**：以 `test_add_threshold` 为对象，把「主机调用 → 命令派发 → 两拍流水 → 四分支运算 → in-place 写回 → busy 清零」整条链路用一张时序表讲清楚。

**操作步骤**：

1. **主机侧**（[software/main.cpp:48-69](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L48-L69)）：列出 `send_image` 之后的关键调用顺序：
   - `switch_buffers()`：把 input/storage 地址互换，让待处理的图进入 storage。
   - `send_add(32, true)`：触发 `COMMAND_APPLY_ADD`。
   - `wait_end_busy()`：轮询状态字节 bit0，直到 `STATE_PROC_UNARY` 跑完。
   - `send_threshold(168, 0, 0)`：在 **同一张** storage 图上接着做阈值。
   - `wait_end_busy()`。
   - `switch_buffers()` + `read_image()`：把结果从 input 读出。
2. **硬件侧**：为 `send_add(32, true)` 这一次运算，画一张表，列出前若干拍的 `proc_memory_addr_counter`、`proc_memory_addr_counter[0]`、`rd_en`、`wr_en`、`addr`、`data_read_valid`、`proc_counter_read`，标注「偶拍=读 / 奇拍=算+写」。
3. **运算分支**：指出 add 分支对 `data_read[7:0]` 与 `data_read[15:8]` 分别做了 `{8'b0, pixel}+add_value` 再 `apply_clamp`，说明两个像素是并行处理的。
4. **写回**：确认 add 与 threshold 两次运算的写回地址都遵循 `addr <= {proc_memory_addr_counter[31:1], 1'b0}`，即 in-place 更新，所以两次运算可以无缝链式叠加，无需中间搬运。

**运行验证（可选，待本地验证）**：若有 Verilator 环境，按 u1-l3 讲义用 `./build_simulation.sh` 构建、`./simu` 运行（确保 `main()` 里激活的是 `test_add_threshold`）、`./run_gnuplot.sh` 查看 `output.dat`。预期看到原图每个像素 +32 后、再把 `<=168` 的像素压成 0 的效果——图像整体变亮，且只剩最亮的高光区域保留为非零灰度。具体数值待本地验证。

**思考题**：如果删掉两次运算之间的 `wait_end_busy()`，第二次 `send_threshold` 会在第一次 add 还没写完 storage 时就开始读 storage，会发生什么？（提示：单端口 RAM 的读改写被打断，部分像素读到的是「尚未被 add 处理」的旧值，结果不一致。）

## 6. 本讲小结

- `STATE_PROC_UNARY` 是一个被 add/threshold/invert/mult 四种运算 **复用** 的逐像素处理状态，靠 `processing_command` 在奇拍里分派具体运算。
- 单端口 RAM 读延迟 1 拍，迫使处理流程拆成 **两拍流水**：偶拍（`proc_memory_addr_counter[0]==0`）发起读、奇拍（`==1`）运算并写回。地址计数器的最低位天然充当节拍器。
- 每个 16 位存储字打包 **2 个 8 位像素**，四条分支都对 `data_read[7:0]` 和 `data_read[15:8]` 分别运算，从而在一个时钟沿内并行处理两像素，吞吐翻倍。
- 写回用 `addr <= {proc_memory_addr_counter[31:1], 1'b0}` 清掉最低位，把奇拍的写地址还原成偶拍的读地址，实现 **in-place 读改写**，无需第二块结果缓冲。
- `proc_counter_read` 从像素总数起步、每字减 2，终止条件 `> 2` 保证在写回最后一个字的那一拍正好回到 `STATE_IDLE`，busy 位（`~(state_processing==STATE_IDLE)`）随即清零。
- ADD/INVERT/MULT 用钳位函数（`apply_clamp` / `apply_clamp_fixed16`）兜底，THRESHOLD 自带边界、INVERT 天然不溢出；MULT 多了一步定点还原（取 `[11:4]`），细节留给 u4-l2。

## 7. 下一步学习建议

- **u4-l2 定点数运算与钳位函数**：本讲里 MULT 分支出现的 `apply_clamp_fixed16`（取 `[11:4]`）和主机 `send_mult(float)` 如何把浮点量化成 8 位定点，是下一讲的主题。建议先掌握本讲的「两拍流水 + 四分支」骨架，再去细究定点格式。
- **u4-l3 双图运算：STATE_PROC_BINARY**：当你想理解「同时读 input 与 storage 两个缓冲」的更复杂时序（`binary_read_buffer` 两步读、`operation_step` 两拍运算），可以对照本讲的「单缓冲两拍流水」来理解它为什么需要更多拍数。
- **源码延伸阅读**：重读 [hdl/image_processing.v:506-560](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L506-L560) 的 `STATE_PROC_UNARY` 整段，并对照 [hdl/image_processing.v:561-629](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L561-L629) 的 `STATE_PROC_BINARY`，体会「单缓冲两拍」与「双缓冲多拍」的复杂度差异。
