# 双图运算：STATE_PROC_BINARY

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `STATE_PROC_BINARY` 这个状态要解决的核心问题：让硬件**同时**读取两幅图（input 与 storage 两个缓冲），逐字做加 / 减 / 乘，并把结果写回 storage。
- 解释三个关键标志位 `binary_read_buffer`、`operation_step`、`absolute_diff` 各自扮演的角色，以及为什么运算要被拆成「两步读 + 两拍算」。
- 跟踪 `COMMAND_BINARY_SUB` 在 `absolute_diff=1` 时如何把负的减法结果取反成「绝对差」。
- 把主机侧 `send_binary_sub(clamp, absolute_diff)` 的两个布尔参数，一路追到 HDL 里的 `comm_data_in[0]` 与 `comm_data_in[1]`。

本讲是「处理运算的状态机实现」单元的第三篇。前两篇已经讲过逐像素运算 `STATE_PROC_UNARY`（u4-l1）和定点数 / 钳位函数（u4-l2）。本讲在它们之上，新增一个难点：**运算需要两个操作数，分别来自两块不同的存储缓冲，而存储器只有一个端口**。

## 2. 前置知识

在进入源码前，先用三段话补齐最关键的背景。

**（1）单端口 RAM 的硬约束。** 本项目的片上存储是单端口 RAM：同一个时钟沿里，要么读、要么写，不能同时读写；而且读有一拍延迟——你这一拍给出地址和 `rd_en`，下一拍数据才出现在 `data_read` 上、并由对端拉高 `data_read_valid` 表示有效。u4-l1 讲过，正是因为这个约束，`STATE_PROC_UNARY` 被拆成「偶拍发起读 / 奇拍运算写回」两拍。双图运算需要从**两块缓冲各读一次**、再向其中一块**写回一次**，约束更紧，因此需要更复杂的节拍编排。

**（2）双缓冲（input / storage）。** 128KB 存储被切成两块各 64KB 的连续区间，分别叫 input 缓冲和 storage 缓冲，基地址由 `buffer_input_address` 与 `buffer_storage_address` 两个寄存器决定（u3-l2）。常规流程是：图像从 input 进、在 storage 上做运算、结果写回 storage，两者可经 `COMMAND_SWITCH_BUFFERS` 零拷贝互换。**双图运算的特殊之处在于：input 和 storage 同时充当操作数**——所以本状态下两条基地址都会被用到。

**（3）16 位字打包 2 像素。** 每个 16 位存储字装两个 8 位像素：高字节 `[15:8]` 一个、低字节 `[7:0]` 一个（u3-l2）。所以「处理一个字」=「处理两个像素」。本讲的源码里你会反复看到对 `[7:0]` 和 `[15:8]` 分别做同样的运算，就是这个原因。

**（4）钳位函数 `apply_clamp`。** u4-l2 讲过：`apply_clamp(in, clamp_en)` 取 `in[7:0]`，若 `clamp_en==1` 则把超过 255 的饱和到 255、低于 0 的饱和到 0。双图运算的加 / 减结果都经过它处理。

## 3. 本讲源码地图

本讲几乎全部内容集中在一个文件里：

| 文件 | 作用 |
| --- | --- |
| [`hdl/image_processing.v`](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v) | 核心模块。本讲的「主角」`STATE_PROC_BINARY`、三个标志位寄存器、`apply_clamp` 函数、命令派发与参数读取状态都在这里。 |
| [`software/image_processing.hpp`](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/image_processing.hpp) | 主机侧抽象接口，声明 `send_binary_add / send_binary_sub / send_binary_mult` 三个纯虚函数。 |
| [`simulation/image_processing_simulation.cpp`](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/simulation/image_processing_simulation.cpp) | 仿真后端，把布尔参数打包成「opcode + 1 字节参数」送入命令队列。用来对照 HDL 的解包逻辑。 |
| [`software/main.cpp`](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp) | 主机程序，`test_binary_add`、`test_simple_edge_detection` 等测试函数演示双图运算的端到端调用。 |

---

## 4. 核心概念与源码讲解

### 4.1 STATE_PROC_BINARY：双图运算总览

#### 4.1.1 概念说明

「逐像素运算」`STATE_PROC_UNARY`（u4-l1）只有一个操作数：读 storage 的一个字、做变换（加常数 / 阈值 / 取反 / 乘常数）、写回 storage。它的操作数和目的地是同一块缓冲。

「双图运算」`STATE_PROC_BINARY` 则有**两个**操作数：一个来自 storage 缓冲，一个来自 input 缓冲。它支持的运算是两幅图之间的逐像素运算：

- `COMMAND_BINARY_ADD`：`storage + input`，写回 storage。
- `COMMAND_BINARY_SUB`：`storage − input`，写回 storage；可选「绝对差」。
- `COMMAND_BINARY_MULT`：`storage × input`，写回 storage。

典型用途：两幅图相加做平均、相减做差异检测、取绝对差做运动检测 / 边缘梯度。注意三个运算都把结果写回 **storage**，所以 storage 既是「操作数 A」又是「目的地」——这个事实直接决定了下文「先读 storage」的时序安排。

与 unary 相比，binary 的状态机复杂在哪？多了一次读。unary 是「读 1 次 → 算 → 写 1 次」，binary 是「读 storage 1 次 → 读 input 1 次 → 算 → 写 storage 1 次」。单端口 RAM 一拍只能做一次访存，于是这三到四次访存必须分散在多个时钟沿上，靠两个标志位来编排节拍。

#### 4.1.2 核心流程

把一个 16 位字（2 个像素）的处理流程画成伪代码：

```
# 处理第 N 个字（字节地址 base = 2*N，偶地址）
A. 读 storage 第 N 字      → 发起读，置 binary_read_buffer=1
B. 等 storage 数据到达     → 存入 buffer_read；发起读 input 第 N 字；地址+1（变奇）；置 binary_read_buffer=0
C0. 等 input 数据到达      → 用 buffer_read(低字节) 与 data_read(低字节) 算出 data_write[7:0]；置 operation_step=1
C1. 算高字节并写回         → 算出 data_write[15:8]；置 wr_en=1 写回 storage 第 N 字；地址+1（变偶，进入下一字）
   每写回一个字，proc_counter_read 减 2；减到 ≤2 时回到 STATE_IDLE。
```

几个要点先记在脑子里：

- `proc_memory_addr_counter[0]`（最低位）是**阶段选择器**：偶（`[0]==0`）走读阶段 A/B，奇（`[0]==1`）走算 / 写回阶段 C。每个阶段各把计数器 +1，一个字累计 +2——而 +2 个字节地址正好前进一个 16 位字。
- `binary_read_buffer` 区分「读第一个缓冲 storage」还是「读第二个缓冲 input」。
- `operation_step` 把运算拆成「先算低字节、再算高字节」两拍。
- 结果始终写回 storage，地址为 `{proc_memory_addr_counter[31:1], 1'b0}`（清掉最低位得到当前字的偶地址）。

#### 4.1.3 源码精读

先看三个标志位和缓冲寄存器的声明：

- [`hdl/image_processing.v:92`](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L92) —— `reg [15:0] buffer_read;` 用来暂存从 storage 读出的整字（2 像素）。
- [`hdl/image_processing.v:99-100`](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L99-L100) —— `binary_read_buffer` 与 `operation_step`，各 1 bit 的节拍标志。
- [`hdl/image_processing.v:134`](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L134) —— `reg absolute_diff;` 二值减法的「取绝对差」参数。

再看命令如何被派发到 binary 的参数读取状态。在主 FSM 的 `STATE_WAIT_COMMAND` 里：

- [`hdl/image_processing.v:258-261`](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L258-L261) —— `COMMAND_BINARY_ADD` 跳到 `STATE_BINARY_ADD_READ_PARAM`。
- [`hdl/image_processing.v:274-277`](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L274-L277) —— `COMMAND_BINARY_SUB` 跳到 `STATE_BINARY_SUB_READ_PARAM`。

> **一个值得留意的现状（待本地验证）：** `COMMAND_BINARY_MULT` 作为 parameter 是有定义的，也有对应的参数读取状态 `STATE_BINARY_MULT_READ_PARAM`（[`L475-484`](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L475-L484)）和 `STATE_PROC_BINARY` 内部的乘法分支（[`L593`](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L593)、[`L610`](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L610)），但**在 `STATE_WAIT_COMMAND` 的派发 `case` 里没有它的分支**——如果主机真的发来 `COMMAND_BINARY_MULT`，会落入 `default` 空分支而什么也不发生。换句话说，乘法这条双图运算「线路已铺好但当前从命令接口还到不了」。下面的讲解仍会覆盖它的运算分支，因为它和 add / sub 共用同一段代码骨架。

参数读取状态读完 1 字节参数后，统一把运算 FSM 切到 `STATE_PROC_BINARY`，并设置「工单」`processing_command`。以 `COMMAND_BINARY_SUB` 为例：

```verilog
// hdl/image_processing.v:463-473  STATE_BINARY_SUB_READ_PARAM
STATE_BINARY_SUB_READ_PARAM: begin
   if(comm_data_in_valid == 1)begin
      state_processing <= STATE_PROC_BINARY;     // 启动运算 FSM
      processing_command <= COMMAND_BINARY_SUB;  // 工单：做减法
      state <= STATE_WAIT_COMMAND;               // 主 FSM 回去等下一条命令
      clamp <= comm_data_in[0];                  // bit0 = clamp
      absolute_diff <= comm_data_in[1];          // bit1 = absolute_diff
      proc_counter_read <= img_width*img_height; // 待处理像素数
      proc_memory_addr_counter <= 0;             // 从字节地址 0 开始
      binary_read_buffer <= 0;                   // 从「读 storage」开始
   end
end
```

要点：
- `proc_counter_read` 装入**像素总数**（不是字数），每处理一个字（2 像素）减 2，所以它能正确表示「还剩多少像素」。
- `proc_memory_addr_counter` 从 0 开始，作为字节地址使用。
- `binary_read_buffer <= 0` 确保「先读 storage」。

最后看整个 `STATE_PROC_BINARY` 的骨架（细节在 4.2 / 4.3 / 4.4 分别展开）：

```verilog
// hdl/image_processing.v:561-628  STATE_PROC_BINARY
STATE_PROC_BINARY: begin : binary
   reg [15:0] temp_calc;
   // —— 读阶段：偶地址 ——
   if(proc_memory_addr_counter[0]==1'b0 && binary_read_buffer==0) begin
      // 阶段 A：发起读 storage
   end
   if (proc_memory_addr_counter[0]==1'b0 && binary_read_buffer==1) begin
      // 阶段 B：等 storage 到达 → 存 buffer_read，发起读 input
   end
   else begin
      // —— 算/写阶段：奇地址 ——
      // 阶段 C0（operation_step==0）：算低字节
      // 阶段 C1（operation_step==1）：算高字节 + 写回 storage
   end
end
```

注意 Verilog 语法上的一个细节：这里写的是「第一个 `if`」之后紧跟「第二个 `if ... else ...`」。也就是说阶段 A 是一条独立的 `if` 语句，而「阶段 B / else（阶段 C）」是另一条 `if-else` 语句，两者在同一时钟沿顺序求值。阶段选择主要靠 `binary_read_buffer`、`operation_step`、`data_read_valid` 三个条件来保证每个时钟沿只有一处实质推进，避免冲突（4.2 会详细说明）。

#### 4.1.4 代码实践

**实践目标：** 用主机侧的 `test_binary_add` 把「双图运算」整条链路在仿真模式跑通，亲眼看到 storage 的「常数图」与 input 的「真实图」相加。

**操作步骤（源码阅读型 + 可选运行）：**

1. 打开 [`software/main.cpp:77-91`](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L77-L91) 的 `test_binary_add`，阅读它的调用序列。
2. 列出它对 `img_proc` 发出的每一步调用，并写出该步对应硬件侧发生了什么：

   | 主机调用 | 硬件侧动作 |
   | --- | --- |
   | `send_params(W, H)` | 写入图像尺寸到 `img_width/img_height` |
   | `send_image(image_input)` | 像素流写入 **input** 缓冲 |
   | `send_clear(32)` | （实为 `send_threshold(0,32,true)`）把 **storage** 缓冲整片填成 32 |
   | `switch_buffers()` | input / storage 基地址互换 |
   | `send_binary_add(true)` | 发 `COMMAND_BINARY_ADD` + 参数字节(clamp=1)，启动 `STATE_PROC_BINARY` |
   | `wait_end_busy()` | 轮询状态字节 bit0，等运算 FSM 回到 `STATE_IDLE` |
   | `switch_buffers()` | 再次互换，把结果换回 input 侧以便读出 |
   | `read_image(image_output)` | 从（现在的）input 缓冲读出结果 |

3. 若本地已装好 Verilator：按 u1-l3 的方法 `./build_simulation.sh` 编译，把 `main.cpp` 里 `main` 末尾改为只激活 `test_binary_add(...)`（注释掉其余 `test_*`，参考 [`main.cpp:252-256`](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L252-L256) 的注释切换写法），再 `make`、运行、`./run_gnuplot.sh output.dat` 查看结果。

**需要观察的现象：** 输出图像应当是「原图每个像素 + 32」（因为 storage 是常数 32）。整体偏亮、但被 `clamp=true` 钳位到 255 的像素会饱和成纯白。

**预期结果：** 原图中灰度值 `≥ 224` 的像素，加 32 后超过 255，显示为 255（白）；其余像素均匀变亮 32 级。若结果整体没变亮，说明缓冲角色没理清。

#### 4.1.5 小练习与答案

**练习 1.** 为什么 `proc_counter_read` 装入的是 `img_width*img_height`（像素数），而每个字处理完只减 2？

**参考答案：** 因为一个 16 位字装 2 个像素。装入像素数、每字减 2，那么 `proc_counter_read` 始终表示「还剩多少像素没处理」，与字的物理粒度无关；当它减到 ≤2 时，说明最后一个字（最后 2 个像素）也已处理完，于是回到 `STATE_IDLE`。

**练习 2.** 如果一幅图的像素总数是奇数，这套机制会出什么问题？

**参考答案：** 16 位字必须凑满 2 个字节才写回（参见 u3-l2 的打包逻辑）。奇数个像素意味着最后一个字只凑得齐 1 个字节，最后一个像素会被漏掉或写成无效数据。本项目要求图像像素数为偶（测试图均为偶数尺寸）。

---

### 4.2 binary_read_buffer 两步读：先读 storage 再读 input

#### 4.2.1 概念说明

双图运算要在「读 storage」「读 input」之间切换，但两条读命令用的都是同一组端口（`addr` / `rd_en` / `data_read`）。`binary_read_buffer` 就是用 1 bit 记住「现在该读哪块缓冲」：0 表示要读 storage，1 表示要读 input。

为什么必须**先读 storage**？源码注释写得很直白：

> `//must read the buffer storage first o/w seems to be problems with the writeback (timing constraints?)`

直觉原因是：**storage 既是操作数来源、又是写回目的地**。读阶段必须先把 storage 的旧值安全地取出来存进 `buffer_read`，之后才能在写回阶段用 `wr_en` 覆盖同一个字。如果改成「先读 input、再读 storage」，那么 storage 的读会和紧随其后的写回挤在一起，在单端口 RAM 上读 / 写争用同一个端口的同一段时序，产生冒险。先读 storage，相当于把「对 storage 的读」和「对 storage 的写」在时间上拉开，留出安全裕量。

#### 4.2.2 核心流程

两步读的时序（假设当前处理第 N 个字，`proc_memory_addr_counter` 为偶数 `2N`）：

```
拍 T   [阶段 A] binary_read_buffer==0：
        rd_en<=1; addr<=buffer_storage_address + 2N;  binary_read_buffer<=1;
        （发起对 storage 第 N 字的读）
拍 T+1 [阶段 B] binary_read_buffer==1，等 data_read_valid：
        buffer_read <= data_read;            // 暂存 storage 的值
        rd_en<=1; addr<=buffer_input_address + 2N;  // 发起对 input 第 N 字的读
        binary_read_buffer<=0;
        proc_memory_addr_counter <= 2N+1;    // 变奇，下一拍交给算/写阶段
        operation_step<=0;
拍 T+2 [阶段 C0] data_read 现在是 input 的值，buffer_read 是 storage 的值 → 可以算了
```

注意：阶段 B 把 `proc_memory_addr_counter` 从偶变奇（最低位变 1）。这一变，下一拍「阶段 A / B」的条件 `proc_memory_addr_counter[0]==0` 不再成立，控制权自然交到 `else` 分支（阶段 C），开始运算与写回。

#### 4.2.3 源码精读

阶段 A —— 发起对 storage 的读（[`hdl/image_processing.v:564-568`](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L564-L568)）：

```verilog
if(proc_memory_addr_counter[0] == 1'b0 && binary_read_buffer == 0) begin
   rd_en <= 1;
   //must read the buffer storage first o/w seems to be problems with the writeback
   addr <= buffer_storage_address+proc_memory_addr_counter;
   binary_read_buffer <= 1;
end
```

阶段 B —— 等 storage 数据到达，暂存后发起对 input 的读（[`hdl/image_processing.v:569-578`](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L569-L578)）：

```verilog
if (proc_memory_addr_counter[0] == 1'b0 && binary_read_buffer == 1) begin
   if (data_read_valid == 1'b1) begin
      buffer_read <= data_read;                              // 暂存 storage 整字
      rd_en <= 1;
      addr <= buffer_input_address+proc_memory_addr_counter; // 读 input 同一字
      binary_read_buffer <= 0;
      proc_memory_addr_counter <= proc_memory_addr_counter + 1; // 偶→奇
      operation_step <= 0;
   end
end
```

两个细节：
- `buffer_read` 与 `data_read` 一起，恰好凑齐「storage 操作数 + input 操作数」，供阶段 C 使用。
- `proc_memory_addr_counter + 1` 把最低位置 1，是「读阶段 → 算 / 写阶段」的交接信号。

#### 4.2.4 代码实践

**实践目标：** 验证「先读 storage」的必要性——通过思想实验理解如果把两步读顺序对调会发生什么。

**操作步骤：**

1. 在 [`hdl/image_processing.v:564-578`](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L564-L578) 中，阶段 A 读的是 `buffer_storage_address+...`，阶段 B 读的是 `buffer_input_address+...`。
2. 假想一次「对调实验」：把阶段 A 改成读 `buffer_input_address`、阶段 B 改成读 `buffer_storage_address`（即先 input 后 storage）。**不要真的改源码**，只在纸上推演。
3. 追踪写回阶段 C1：它在阶段 B 之后的两拍，用 `wr_en<=1; addr<=buffer_storage_address+{...}` 写回 storage（见 4.3.3）。这意味着「对 storage 的读（阶段 B）」与「对 storage 的写（阶段 C1）」只隔了一个 operation_step 拍。

**需要观察 / 推演的现象：** 对调后，「读 storage」和「写 storage」紧贴在一起，单端口 RAM 在同一段时序里既要交付刚读出的 storage 值、又要接受写入，数据可能尚未稳定就被覆盖，产生写回错误（这正是注释里说的 "problems with the writeback"）。而现状（先读 storage）把 storage 的读放在最前面，与写回之间隔着「读 input + 两拍运算」，时序裕量充足。

**预期结果（推演）：** 现状下 storage 旧值在阶段 B 就被锁进 `buffer_read` 寄存器，与后续 RAM 写入完全解耦，所以写回安全。这是一个典型的「靠数据通路寄存器隔离读写冒险」的手法。

> 待本地验证：如果你有 Verilator 环境，可以临时对调两步读顺序、重新编译运行 `test_binary_add`，观察输出是否出现错乱像素，从而印证注释的判断。

#### 4.2.5 小练习与答案

**练习 1.** `binary_read_buffer` 在 `initial` 块里被初始化成什么值？为什么是这个值？

**参考答案：** 在 [`hdl/image_processing.v:194`](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L194) 初始化为 0；而且每次进入 binary 运算前，`STATE_BINARY_*_READ_PARAM` 也都把它置 0（如 [`L428`](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L428)、[`L472`](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L472)、[`L483`](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L483)）。0 对应阶段 A「读 storage」，确保每次运算都从「先读 storage」开始。

**练习 2.** 阶段 B 里 `proc_memory_addr_counter <= proc_memory_addr_counter + 1`，为什么这一步是「读阶段 → 算阶段」的关键开关？

**参考答案：** 阶段 A / B 的进入条件都要求 `proc_memory_addr_counter[0]==0`（偶）。阶段 B 把计数器 +1 后最低位变 1（奇），于是下一拍阶段 A / B 的条件都不成立，控制权落到 `else` 分支（阶段 C），开始运算与写回。所以这一句不只是「挪地址」，更是「交班」。

---

### 4.3 operation_step 两拍运算：低字节与高字节分开

#### 4.3.1 概念说明

两个操作数都到手后（`buffer_read` = storage 整字、`data_read` = input 整字），就要做运算并写回。源码注释再次提示了拆拍的理由：

> `operation_step <= 0; //binary op done in multiple clk counts due to timing constr.`
> `//separated into two steps due to what seemed to be timing constraints`

`operation_step` 这个 1 bit 把一个字的运算拆成两拍：
- `operation_step==0`：用两个操作数的**低字节** `[7:0]` 算出 `data_write[7:0]`。
- `operation_step==1`：用两个操作数的**高字节** `[15:8]` 算出 `data_write[15:8]`，**然后**才置 `wr_en<=1` 把拼好的 16 位 `data_write` 写回 storage。

为什么要拆？因为「算 + 写」若挤在同一拍，组合路径（减法 / 取绝对值 / `apply_clamp` 钳位比较 + RAM 写驱动）太长，难以满足时序。拆成两拍后，低字节的运算结果先在 `data_write[7:0]` 上稳定一拍，第二拍再算高字节并同时发起写回，给综合工具留出更宽松的时序预算。这和 u4-l1 里 unary 的「读 / 写分拍」是同一种节流思路。

#### 4.3.2 核心流程

```
[阶段 C0] data_read_valid==1 且 operation_step==0：
   temp_calc = buffer_read[7:0] 〈op〉 data_read[7:0]   // 低字节运算（含 absolute_diff 处理）
   data_write[7:0] <= apply_clamp(temp_calc, clamp);
   operation_step <= 1;                                  // 交给第二拍

[阶段 C1] operation_step==1：
   temp_calc = buffer_read[15:8] 〈op〉 data_read[15:8]  // 高字节运算（同上）
   data_write[15:8] <= apply_clamp(temp_calc, clamp);
   operation_step <= 0;
   wr_en <= 1;                                            // 现在两字节都齐了，写回
   addr <= buffer_storage_address + {proc_memory_addr_counter[31:1], 1'b0};
   proc_memory_addr_counter <= proc_memory_addr_counter + 1;  // 奇→偶，进入下一字
   if(proc_counter_read > 2)  proc_counter_read <= proc_counter_read - 2;
   else                       state_processing <= STATE_IDLE;
```

注意写回地址 `{proc_memory_addr_counter[31:1], 1'b0}`：此时计数器是奇数（最低位 1），把最低位清零，正好还原回当前字的偶地址——与阶段 A / B 读的是同一个字。

#### 4.3.3 源码精读

阶段 C0 —— 低字节运算（[`hdl/image_processing.v:581-598`](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L581-L598)）：

```verilog
if (data_read_valid == 1'b1 && operation_step == 0) begin
   temp_calc = 0;
   if( processing_command == COMMAND_BINARY_ADD) begin
      temp_calc = {8'b0, buffer_read[7:0]} + {8'b0, data_read[7:0]};
      data_write[7:0] <= apply_clamp(temp_calc, clamp);
   end else if (processing_command == COMMAND_BINARY_SUB) begin
      temp_calc = {8'b0, buffer_read[7:0]} - {8'b0, data_read[7:0]};
      if(absolute_diff == 1 && $signed(temp_calc) < 0) temp_calc = -temp_calc;
      data_write[7:0] <= apply_clamp(temp_calc, clamp);
   end else if (processing_command == COMMAND_BINARY_MULT) begin
      temp_calc = {8'b0, buffer_read[7:0]} * {8'b0, data_read[7:0]};
      data_write[7:0] <= apply_clamp(temp_calc, clamp);
   end
   operation_step <= 1;
end
```

阶段 C1 —— 高字节运算 + 写回（[`hdl/image_processing.v:600-626`](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L600-L626)）：

```verilog
end else if (operation_step == 1) begin
   if( processing_command == COMMAND_BINARY_ADD) begin
      temp_calc = {8'b0, buffer_read[15:8]} + {8'b0, data_read[15:8]};
      data_write[15:8] <= apply_clamp(temp_calc, clamp);
   end else if (processing_command == COMMAND_BINARY_SUB) begin
      ... // 同结构，处理 [15:8]
   end else if (processing_command == COMMAND_BINARY_MULT) begin
      ...
   end
   operation_step <= 0;
   //wrtie back data into storage, same 16bits address
   wr_en <= 1;
   addr <= buffer_storage_address+{proc_memory_addr_counter[31:1], 1'b0};
   proc_memory_addr_counter <= proc_memory_addr_counter+1;
   if(proc_counter_read > 2) begin // > 2 and not 0 because we are shifted by one
      proc_counter_read <= proc_counter_read - 2;
   end else begin
      state_processing <= STATE_IDLE;
   end
end
```

三处要点：
- 低、高字节是**两套独立但同构**的运算分支，差别只在取 `[7:0]` 还是 `[15:8]`。
- 写回 `wr_en<=1` 只在第二拍（`operation_step==1`）发生，此时 `data_write` 的高低字节都已就绪。
- `proc_counter_read > 2` 而非 `> 0`：注释解释为 "shifted by one"——因为流水线里地址 / 计数有固定偏移，用 `> 2` 才能保证最后一个字被完整处理后再回 IDLE。

关于 `apply_clamp`（[`hdl/image_processing.v:151-163`](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L151-L163)）：它取 `in[7:0]`，并在 `clamp_en==1` 时把 `$signed(in)>255` 饱和到 255、`$signed(in)<0` 饱和到 0。双图 add / sub 都用它。注意它不是定点还原函数 `apply_clamp_fixed16`——因为双图 add / sub 的结果天然落在 8 位像素尺度（两个 0~255 的值相加减），不需要除以 16 还原。

#### 4.3.4 代码实践

**实践目标：** 手算一个字的运算全过程，验证「两拍拆分」与「钳位」的配合。

**操作步骤：**

1. 设两个操作数的某个字为：storage `buffer_read = 16'hC840`（高字节 200、低字节 64），input `data_read = 16'h5A64`（高字节 90、低字节 100）。`clamp=1`，运算为 `COMMAND_BINARY_ADD`。
2. 按 C0 / C1 两拍分别计算：

   | 拍 | 字节 | temp_calc | apply_clamp 结果 |
   | --- | --- | --- | --- |
   | C0 | 低字节 | \(64 + 100 = 164\) | 164（未越界） |
   | C1 | 高字节 | \(200 + 90 = 290\) | 255（>255，饱和） |

3. 写回的 `data_write` 应为 `16'hFFA4`（高字节 255、低字节 164）。

**需要观察的现象：** 高字节因相加超过 255 被钳到 255；低字节正常。这正是 `clamp=true` 的效果——若 `clamp=false`，高字节会取 `290[7:0] = 0x24 = 36`（回绕，通常是无意义的）。

**预期结果：** 该字写回 storage 的值为 `0xFFA4`。理解了这一点，你就明白了「为什么默认要传 `clamp=true`」。

#### 4.3.5 小练习与答案

**练习 1.** 为什么 `wr_en` 只在 `operation_step==1` 那一拍拉高，而不是 C0 拍就拉高？

**参考答案：** C0 拍只算出了低字节 `data_write[7:0]`，高字节尚未更新。如果此刻写回，会把上一字残留的（或未初始化的）高字节一起写进去。等到 C1 拍高字节也就绪后，整个 16 位 `data_write` 才完整，此时拉高 `wr_en` 才能写回正确数据。

**练习 2.** 写回地址 `{proc_memory_addr_counter[31:1], 1'b0}` 在功能上等价于「把当前奇地址变成对应的偶字地址」。请用一句话解释为什么这一步对 in-place 写回是必须的。

**参考答案：** 阶段 A / B 读的是偶地址（字地址），到 C1 时计数器已变奇；清掉最低位后正好回到当初读的那个字的偶地址，从而保证「读哪个字、写回哪个字」，实现 in-place 更新而不破坏相邻字。

---

### 4.4 absolute_diff：取绝对差

#### 4.4.1 概念说明

`COMMAND_BINARY_SUB` 比 add 多一个参数 `absolute_diff`（主机侧 `send_binary_sub(clamp, absolute_diff)` 的第二个布尔）。它的语义：

- `absolute_diff==0`：普通减法 `storage − input`。
- `absolute_diff==1`：绝对差 \(|\,\text{storage} − \text{input}\,|\)，结果恒非负。

绝对差是图像处理里很有用的运算——比如把当前帧减去背景帧再取绝对值，就能得到「运动区域」；或者把两个方向的梯度取绝对差做边缘。它的数学定义很简单：

\[
\text{diff}(a, b) = |a - b| =
\begin{cases}
a - b, & a \ge b \\
b - a, & a < b
\end{cases}
\]

在硬件里，因为已经算出了 `temp_calc = storage − input`，只需在结果为负时取反（二补码取负）即可，不必重新做一次减法。

#### 4.4.2 核心流程

```
temp_calc = {8'b0, buffer_read[X]} - {8'b0, data_read[X]};   // storage - input
if (absolute_diff==1 && $signed(temp_calc) < 0)
    temp_calc = -temp_calc;                                  // 二补码取负 → |差|
data_write[X] <= apply_clamp(temp_calc, clamp);
```

几个细节：
- `{8'b0, ...}` 把 8 位像素零扩展成 16 位，使减法结果能表示负数（用第 16 位当符号位）。
- `$signed(temp_calc) < 0` 把 `temp_calc` 当有符号数看，等价于检查最高位（bit15）是否为 1。
- `-temp_calc` 是 Verilog 对 16 位向量的二补码取负，等价于「按位取反再加 1」。

#### 4.4.3 源码精读

`absolute_diff` 参数在 `STATE_BINARY_SUB_READ_PARAM` 里从参数字节的 bit1 解出（已在 4.1.3 引用，见 [`hdl/image_processing.v:469`](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L469)）。

低字节的减法分支（[`hdl/image_processing.v:587-592`](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L587-L592)）：

```verilog
end else if (processing_command == COMMAND_BINARY_SUB) begin
   temp_calc = {8'b0, buffer_read[7:0]} - {8'b0, data_read[7:0]};
   if(absolute_diff == 1 && $signed(temp_calc) < 0) begin
      temp_calc = -temp_calc;
   end
   data_write[7:0] <= apply_clamp(temp_calc, clamp);
end
```

高字节分支（[`hdl/image_processing.v:604-609`](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L604-L609)）结构完全相同，只是换成 `[15:8]`。

再对照主机侧的打包，确认参数位对齐。仿真后端（[`simulation/image_processing_simulation.cpp:185-192`](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/simulation/image_processing_simulation.cpp#L185-L192)）：

```cpp
void Image_processing_simulation::send_binary_sub(bool clamp, bool absolute_diff){
   fifo_in.push(Operation(true, COMMAND_BINARY_SUB, 0));                  // opcode
   fifo_in.push(Operation(false, COMMAND_NONE, (absolute_diff<<1)+clamp));// 参数字节
   ...
}
```

参数字节 `(absolute_diff<<1)+clamp`：bit0 = clamp，bit1 = absolute_diff。这与 HDL 的 `clamp <= comm_data_in[0]`、`absolute_diff <= comm_data_in[1]` 逐位对应——正是 u2-l2 讲过的「布尔标志位打包」约定，双图减法是它的一个典型用例。

真实调用示例：[`software/main.cpp:214`](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L214) 的 `test_simple_edge_detection` 里就用了 `send_binary_sub(true, true)`——两个 true 分别表示「启用钳位」和「取绝对差」，用绝对差来累加多方向梯度。

#### 4.4.4 代码实践

**实践目标：** 追踪 `absolute_diff=1` 时一个负结果如何被取反成绝对差，并手算验证。

**操作步骤：**

1. 设某字节：storage `buffer_read[7:0] = 50`，input `data_read[7:0] = 200`，`absolute_diff=1`，`clamp=1`。
2. 按源码逐步计算：
   - `temp_calc = 50 − 200 = −150`。在 16 位二补码里是 `0xFF6A`。
   - `$signed(temp_calc) < 0` 为真（最高位是 1），且 `absolute_diff==1`，于是 `temp_calc = -temp_calc = 150`。
   - `apply_clamp(150, 1)`：150 在 0~255 内，结果 150。
3. 对比 `absolute_diff=0` 的情形：`temp_calc` 仍是 −150，但不取反；`apply_clamp(−150, 1)` 因 `$signed < 0` 而饱和到 0。

**需要观察的现象：**

| 参数 | 50 − 200 的结果 |
| --- | --- |
| `absolute_diff=1, clamp=1` | \(|50-200| = 150\) |
| `absolute_diff=0, clamp=1` | 饱和到 0 |
| `absolute_diff=0, clamp=0` | 取 `(-150)[7:0] = 0x6A = 106`（回绕，通常无意义） |

**预期结果：** 只有 `absolute_diff=1` 能给出「有意义的距离 150」；其余两种要么饱和到 0、要么给出回绕的垃圾值。这解释了为什么差异检测场景几乎总是配合 `absolute_diff=true` 使用。

> 待本地验证：在 `test_simple_edge_detection`（[`main.cpp`](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp) 约 L160-L220）里把 `send_binary_sub(true, true)` 临时改成 `send_binary_sub(true, false)`，重新仿真，观察输出图像在「应该有差异」的区域是否大面积变黑（因为负差被钳到 0）。

#### 4.4.5 小练习与答案

**练习 1.** 为什么减法前要把两个 8 位像素用 `{8'b0, ...}` 零扩展成 16 位？

**参考答案：** 8 位减法无法表示负数（会在第 8 位之外丢失符号）。零扩展到 16 位后，结果能用 bit15 当符号位表示正负，`$signed(temp_calc) < 0` 才能正确判断；同时给 `apply_clamp` 的 `$signed` 比较留出了足够的比较位宽。

**练习 2.** 如果 `absolute_diff==1` 但 `clamp==0`，结果一定落在 0~255 吗？

**参考答案：** 不一定。取绝对差后 \(|a-b|\) 的最大值是 \(|255-0|=255\)，所以两个 0~255 像素的绝对差确实在 0~255 内——但这是数学性质保证的，不是 `clamp` 保证的。如果未来把运算改成可能产生更大范围的结果（例如先放大再相减），`clamp=0` 就可能写出越界值。当前双图 sub 的取值范围恰好安全，但依赖 `clamp=true` 更稳妥。

---

## 5. 综合实践

**任务：** 把本讲的三个机制（两步读、两拍算、绝对差）串起来，写一份「单字处理时序说明」，并对照主机调用验证。

请完成以下事项：

1. **画时序图。** 以 `COMMAND_BINARY_SUB`、`absolute_diff=1`、`clamp=1`、某个字 `storage=0x0032`（高字节 0、低字节 50）、`input=0x00C8`（高字节 0、低字节 200）为例，画出从进入 `STATE_PROC_BINARY` 到写回完成的**逐拍**时序表，包含每一拍的：当前阶段（A/B/C0/C1）、`proc_memory_addr_counter` 值、`binary_read_buffer`、`operation_step`、`addr`、`rd_en`/`wr_en`、`buffer_read`、`data_read`、`temp_calc`、`data_write`。

   参考骨架（请你补全每一列）：

   | 拍 | 阶段 | addr_counter | bin_rd_buf | op_step | 动作 | 关键数据 |
   | --- | --- | --- | --- | --- | --- | --- |
   | T0 | A | 0(偶) | 0→1 | — | 读 storage 字 0 | addr=storage+0 |
   | T1 | B（等 valid） | 0 | 1 | — | buffer_read←0x0032；读 input 字 0 | addr=input+0 |
   | T2 | C0 | 1(奇) | 0 | 0→1 | 算低字节：50−200=−150→取反=150 | data_write[7:0]=150 |
   | T3 | C1 | 1→2(偶) | 0 | 1→0 | 算高字节：0−0=0；写回 | data_write=0x0096，wr_en=1 |

   （上表中 T1 是否需要多等一拍 `data_read_valid`，取决于具体时序；请据源码自行判断并标注。）

2. **对照主机调用。** 打开 [`software/main.cpp`](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp) 中 `test_simple_edge_detection`（约 L160-L220），找到 `send_binary_sub(true, true)`（[`L214`](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L214)）。说明这次减法的两个操作数分别来自哪两块缓冲（提示：在该函数里追踪 `send_image` 与 `switch_buffers` 的顺序，确定此刻 input / storage 各装的是什么）。

3. **回答关键问题（本讲实践任务的核心）：**
   - 为什么要先用 `binary_read_buffer` 读 storage、再读 input？（见 4.2）
   - 为什么 `operation_step` 要把运算拆成「先低字节、再高字节」两拍？（见 4.3）
   - `COMMAND_BINARY_SUB` 在 `absolute_diff=1` 时，如何把负的减法结果取反成绝对差？（见 4.4）

**预期产出：** 一张完整的逐拍时序表 + 一段对上述三个问题的中文说明。完成后，你应当能用一句话向别人解释「双图运算在单端口 RAM 上为什么需要这么多拍」。

---

## 6. 本讲小结

- `STATE_PROC_BINARY` 实现两幅图（input / storage）之间的逐像素 add / sub / mult，结果始终写回 storage；storage 既是操作数 A、又是写回目的地。
- 处理一个 16 位字（2 像素）需要经历：**阶段 A 读 storage → 阶段 B 读 input → 阶段 C0 算低字节 → 阶段 C1 算高字节并写回**，靠 `proc_memory_addr_counter[0]`（偶 = 读阶段 / 奇 = 算写阶段）和两个标志位 `binary_read_buffer`、`operation_step` 编排节拍。
- `binary_read_buffer` 区分「读 storage」与「读 input」；**必须先读 storage**，因为 storage 还要被写回，先读能把它的旧值安全锁进 `buffer_read`，避免单端口 RAM 的读写冒险。
- `operation_step` 把运算拆成低字节、高字节两拍，并在第二拍才拉高 `wr_en`，保证 16 位 `data_write` 完整后再写回，同时缓解组合路径的时序压力。
- `absolute_diff` 让减法在结果为负时取反（`temp_calc = -temp_calc`），得到 \(|storage - input|\)；参数位 bit1 由主机 `(absolute_diff<<1)+clamp` 打包、HDL `comm_data_in[1]` 解包。
- `apply_clamp`（非定点版）负责把结果饱和到 0~255；双图 add/sub 结果天然在像素尺度，故用 `apply_clamp` 而非 `apply_clamp_fixed16`。
- 现状提醒：`COMMAND_BINARY_MULT` 已铺好参数读取状态与运算分支，但 `STATE_WAIT_COMMAND` 里暂无派发分支，当前从命令接口尚不可达（待本地验证）。

## 7. 下一步学习建议

- **进入卷积（u5 单元）。** 双图运算是「两个操作数」的极端；接下来的 3×3 卷积是「一个像素 × 9 个邻域」的运算，会引入行缓冲与 9 拍累加，是全项目最复杂的处理状态机。建议先读 u5-l1 的卷积总览，你会发现在本讲学到的「单端口 RAM 两拍流水」「写回地址清最低位」等手法在卷积里被进一步发扬。
- **重读双缓冲与打包（u3-l2）。** 如果你对本讲的「字节地址 +2 = 前进一个字」「写回地址清最低位」还有疑虑，回到 u3-l2 复习 16 位字打包模型会很有帮助。
- **动手扩展（可选）。** 试着在 `STATE_WAIT_COMMAND` 里为 `COMMAND_BINARY_MULT` 补一个派发分支（跳到已存在的 `STATE_BINARY_MULT_READ_PARAM`），在仿真模式下验证两幅图相乘能否跑通——这是理解「命令→参数读取→运算」三段式如何挂接的好练习（注意：修改源码仅用于自学，勿提交到仓库）。
