# 图像发送/接收与参数读取状态

## 1. 本讲目标

本讲紧接 u3-l3（命令派发主 FSM），钻进 `image_processing.v` 主状态机里「搬数据」的那几个状态。学完后你应该能够：

- 说清 `counter_read` 这个倒计数器是如何驱动所有「变长参数读取 / 图像收发 / 状态回传」状态的，以及它为什么没有全局固定含义。
- 画出 `STATE_SEND_IMG` 把主机逐字节图像流「2 进 1」打包成 16 位字写入存储的时序。
- 画出 `STATE_READ_IMG` 用 `mem_data_buffer` 构成的「读存储 → 缓冲 → 逐字节输出」两级流水，并能解释为什么读地址 `memory_addr_counter` 只在输出高字节时才 +1。
- 说清 `STATE_GET_STATUS` 回传 4 字节、且第 1 字节 bit0 就是 busy 位的机制，并把它与主机 `wait_end_busy` 对应起来。

## 2. 前置知识

本讲假设你已学完 u3-l1、u3-l2、u3-l3。下面只补三点点睛之笔，其余不复述。

- **单端口 RAM 与读延迟**：模块只有一组 `addr / rd_en / wr_en / data_read / data_read_valid / data_write` 线（见 [hdl/image_processing.v:17-22](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L17-L22)）。读和写不能同拍进行；而且读数据不是当拍就到，要等一拍，用一个独立握手 `data_read_valid` 来标志「这一拍 `data_read` 上是有效数据」。所以「发起读」和「拿到数据」天然要拆成两拍。
- **16 位字打包**：存储字宽 16 位，每字装 2 个像素字节（u3-l2 已讲）。字节级地址的最低位 `[0]` 用来区分「这一字节进 16 位字的低 8 位还是高 8 位」。
- **非阻塞赋值 `<=`**：在 `always @(posedge clk)` 里，`<=` 的右边读的是「本拍开始时的旧值」，左边要等到下一个时钟沿才生效。本讲大量时序（先发起读、下一拍才锁存数据）都靠这一点理解。

> 提示：仿真后端用 C++ 数组模拟这块 RAM。在 [simulation/image_processing_simulation.cpp:254-263](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/simulation/image_processing_simulation.cpp#L254-L263) 里，`data_read_valid` 是在「下一次 `main_loop_clk` 调用」里才根据上一拍的 `rd_en` 给出的——也就是说仿真也造出了「一拍读延迟」。这正是 `data_read_valid` 握手存在的意义：让同一份 FSM 在仿真（0 物理延迟但建模成一拍）和真实 SPRAM（有硬件读延迟）下都能正确工作。

## 3. 本讲源码地图

本讲几乎只看一个文件，外加一个对照文件：

| 文件 | 作用 |
| --- | --- |
| `hdl/image_processing.v` | 唯一核心模块。本讲的 `counter_read`、`STATE_SEND_IMG`、`STATE_READ_IMG`、`STATE_GET_STATUS` 以及各 `*_READ_PARAM` 状态全部位于它的 `always @(posedge clk)` 主块内。 |
| `simulation/image_processing_simulation.cpp` | 主机侧（仿真后端）如何驱动上面这些状态：`send_image / read_image / read_status / wait_end_busy`。用于代码实践对照。 |

关键寄存器先认一下脸（都在 [hdl/image_processing.v:88-92](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L88-L92)）：

```verilog
reg [15:0] counter_read;        // 本讲主角：通用倒计数器
reg [31:0] memory_addr_counter; // 字节级存储地址指针
reg [15:0] mem_data_buffer;     // READ_IMG 的 16 位中间缓冲
reg mem_data_buffer_full;       // 上面这个缓冲是否已装满有效数据
```

## 4. 核心概念与源码讲解

### 4.1 counter_read：驱动变长读取的倒计数器

#### 4.1.1 概念说明

不同命令带的参数数量完全不同：`COMMAND_PARAM` 带 4 字节（宽高各 2 字节）、`COMMAND_APPLY_ADD` 带 3 字节、`COMMAND_CONVOLUTION` 带 10 字节、`COMMAND_SEND_IMG` / `COMMAND_READ_IMG` 带整整一幅图（`W*H` 字节）。硬件需要一种统一机制告诉某个状态「还要处理几个字节」。

项目给出的答案是 `counter_read`：一个 16 位倒计数寄存器（[hdl/image_processing.v:88](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L88)）。它的使用套路是「**派发时预装初值 → 消费状态每处理一个字节就 -1 → 减到终止条件就收工**」。

最重要的一点：**`counter_read` 没有全局固定含义**。「预装多少、减到几结束、每个计数值对应哪个参数」完全由消费它的那个状态自己定义。所以同一个寄存器，在 `STATE_SEND_IMG` 里代表「剩余像素数」，在 `STATE_GET_STATUS` 里却代表「还剩几拍输出」。这是阅读本节代码的钥匙。

#### 4.1.2 核心流程

```
STATE_WAIT_COMMAND 收到操作码 comm_cmd
   ├── 选出下一个 state
   ├── counter_read <= <该命令需要的初值>   // 预装
   └── 跳进消费状态
消费状态（每个 comm_data_in_valid 或 comm_data_out_free 的拍）
   ├── 处理一个字节
   ├── counter_read <= counter_read - 1
   └── 命中终止条件 → state <= STATE_WAIT_COMMAND（或交接给运算 FSM）
```

下表汇总了主 FSM 派发时给 `counter_read` 预装的初值与实际处理字节数（来源 [hdl/image_processing.v:221-281](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L221-L281)）：

| 命令 | 预装 `counter_read` | 消费状态 | 实际处理字节数 |
| --- | --- | --- | --- |
| `COMMAND_PARAM` | 1 | `WIDTH` → `HEIGHT` | 4（每读完一个 16 位值就在两个状态间各自重置一次） |
| `COMMAND_SEND_IMG` | `W*H` | `STATE_SEND_IMG` | `W*H` |
| `COMMAND_READ_IMG` | `W*H` | `STATE_READ_IMG` | `W*H` |
| `COMMAND_GET_STATUS` | 3 | `STATE_GET_STATUS` | 4（输出） |
| `COMMAND_APPLY_ADD` | 2 | `ADD_READ_PARAM` | 3 |
| `COMMAND_APPLY_THRESHOLD` | 2 | `THRESHOLD_READ_PARAM` | 3 |
| `COMMAND_CONVOLUTION` | 9 | `CONVOLUTION_READ_PARAM` | 10 |

注意「预装值」与「字节数」并不总是相等：`GET_STATUS` 预装 3 却输出 4 字节，`ADD` 预装 2 却读 3 字节。差别就在于每个状态的终止条件不同（后面会逐一看到）。**不要试图用一个万能公式去套，要按状态读。**

#### 4.1.3 源码精读

派发点（节选自 [hdl/image_processing.v:225-273](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L225-L273)）：

```verilog
COMMAND_SEND_IMG: begin
   state <= STATE_SEND_IMG;
   counter_read <= img_width*img_height;   // 预装成整幅图像素数
   memory_addr_counter <= buffer_input_address;
end
COMMAND_GET_STATUS: begin
   state <= STATE_GET_STATUS;
   counter_read <= 3;                       // 预装 3 → 实际回传 4 字节
end
COMMAND_APPLY_ADD: begin
   state <= STATE_APPLY_ADD_READ_PARAM;
   counter_read <= 2;                       // 预装 2 → 实际读 3 字节
end
```

以 `STATE_APPLY_ADD_READ_PARAM` 作为「最干净的 counter_read 用法」示例（[hdl/image_processing.v:355-372](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L355-L372)）：它没有存储打包，纯粹靠计数器区分三个字节。

```verilog
STATE_APPLY_ADD_READ_PARAM: begin
   if(comm_data_in_valid == 1)begin
      if(counter_read == 2) begin
         add_value[7:0] <= comm_data_in;   // 第 1 字节：加数低字节
         counter_read <= 1;
      end else if(counter_read == 1) begin
         add_value[15:8] <= comm_data_in;  // 第 2 字节：加数高字节
         counter_read <= 0;
      end else begin                        // counter_read==0：第 3 字节
         clamp <= comm_data_in[0];
         state_processing <= STATE_PROC_UNARY;   // 读完即交接给运算 FSM
         processing_command <= COMMAND_APPLY_ADD;
         state <= STATE_WAIT_COMMAND;
      end
   end
end
```

规律（承接 u3-l3）：带参数的运算命令，都在 `*_READ_PARAM` 读完最后一个字节（`counter_read==0`）的那一拍，一次性写好 `processing_command` 和 `state_processing`，把运算「工单」交接给运算 FSM。

#### 4.1.4 代码实践

**实践目标**：验证 `counter_read` 在变长读取里的字节分配。

**操作步骤**：

1. 打开 `STATE_CONVOLUTION_READ_PARAM`（[hdl/image_processing.v:431-462](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L431-L462)）。
2. 仿照上面 `ADD` 的方式，列出 `counter_read` 从 9 递减到 0 的每一拍分别读取什么、写入哪个寄存器。

**需要观察的现象 / 预期结果**（待本地逐行核对）：

| `counter_read` | 读到的字节写入 |
| --- | --- |
| 9 | `convolution_params`（位打包：`[0]=clamp`、`[1]=source`、`[2]=add_to_result`） |
| 8 → 0 | `convolution_matrix[8-counter_read]`，且做 8→16 位符号扩展 |

即：`counter_read==9` 的那一拍是「通用参数字节」，`8..0` 是 9 个卷积核字节，共 10 字节。计数器归零那一拍同样把工单交接给 `STATE_PROC_CONVOLUTION`。

#### 4.1.5 小练习与答案

**Q1**：`COMMAND_GET_STATUS` 预装 `counter_read=3`，为什么实际回传 4 个字节？

**参考答案**：因为 `STATE_GET_STATUS` 的终止条件是「`counter_read > 0` 时继续，`==0` 时才退出」，并且 `counter_read==0` 这一拍仍然会输出一个字节。所以从 3 数到 0，一共输出 4 次（3、2、1、0）。

**Q2**：为什么 `COMMAND_SEND_IMG` 预装的是 `W*H` 而不是 `W*H/2`？毕竟每个 16 位字装 2 个像素。

**参考答案**：因为主机是**逐字节、每拍一个像素**地送图，`counter_read` 数的是「主机发来的字节数 = 像素数」，而不是「写入的字数」。把两个字节凑成一个 16 位字、再 `wr_en` 一次，是 `STATE_SEND_IMG` 内部用 `memory_addr_counter[0]` 做的事（见 4.2），与 `counter_read` 数的是两套东西。

---

### 4.2 STATE_SEND_IMG：逐字节接收并打包成 16 位字写入

#### 4.2.1 概念说明

主机发图时是一条「逐字节的像素流」：每个 `comm_data_in_valid` 拍送来 1 个字节 = 1 个像素。但存储器字宽是 16 位（2 字节）。所以 `STATE_SEND_IMG` 要做「**2 进 1**」：每收满 2 个字节，拼成一个 16 位字、发一次 `wr_en`。这复用了 u3-l2 讲过的「偶地址进低字节、奇地址进高字节」技巧。

#### 4.2.2 核心流程

每收到一个字节（`comm_data_in_valid==1`）：

```
若 memory_addr_counter[0] == 0（偶字节）:
   data_write[7:0] <= comm_data_in          // 先放进低 8 位，还不写
否则（奇字节）:
   data_write[15:8] <= comm_data_in          // 放进高 8 位
   wr_en <= 1                                // 凑满 2 字节，触发一次写
   addr <= {memory_addr_counter[31:1], 1'b0} // 字地址（清掉最低位）
memory_addr_counter <= memory_addr_counter + 1   // 每收一个字节都 +1
counter_read：
   if(counter_read > 1) counter_read <= counter_read - 1
   else                  state <= STATE_WAIT_COMMAND   // 最后一个字节，收工
```

#### 4.2.3 源码精读

完整状态见 [hdl/image_processing.v:334-353](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L334-L353)：

```verilog
STATE_SEND_IMG: begin //receives image from the host
   if(comm_data_in_valid == 1) begin
      if(memory_addr_counter[0] == 1'b0) begin
         data_write[7:0] <= comm_data_in;
      end else begin
         data_write[15:8] <= comm_data_in;
         wr_en <= 1;
         addr <= {memory_addr_counter[31:1], 1'b0};  // 字地址
      end
      memory_addr_counter <= memory_addr_counter+1;
      if(counter_read > 1) begin
         counter_read <= counter_read - 1;
      end else begin
         state <= STATE_WAIT_COMMAND;
      end
   end
end
```

要点：

- `wr_en` 只在**奇字节**那一拍置 1——此时低字节（上一拍收的）和高字节（本拍）都齐了。偶字节那拍只把字节暂存进 `data_write[7:0]`，不写。
- `addr` 取 `{memory_addr_counter[31:1], 1'b0}`，即把字节地址的最低位清零得到字地址。仿真后端 `memory[addr/2]`（[simulation/image_processing_simulation.cpp:257](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/simulation/image_processing_simulation.cpp#L257)、[:262](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/simulation/image_processing_simulation.cpp#L262)）正好印证「`addr` 是字节地址，除以 2 才是 16 位字下标」。
- `memory_addr_counter` 每收一字节 +1；`counter_read` 每 -1 直到 1，因此共接收 `W*H` 字节。

#### 4.2.4 代码实践

**实践目标**：眼见为实地看到「2 进 1」打包。

**操作步骤**：

1. 在 `software/main.cpp` 里临时把测试图换成一尺寸很小的情形（或直接读 `send_image` 的源码即可，无需真的跑）。
2. 关注仿真后端 `main_loop_clk` 里的调试打印（[simulation/image_processing_simulation.cpp:260-263](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/simulation/image_processing_simulation.cpp#L260-L263)）：每次 `wr_en==1` 都会打印 `wants to write: addr:0x.. data:0x..`。

**需要观察的现象 / 预期结果**：

- 对一幅 `N` 像素（`N` 为偶数）的图，`wr_en` 被置位恰好 `N/2` 次。
- 每次打印的 `addr` 是 `0, 2, 4, …`（字地址递增 2），`data` 的低字节是第 `0,2,4,…` 个像素、高字节是第 `1,3,5,…` 个像素。
- 若你不在本机跑，可改为「源码阅读型实践」：手算发送像素序列 `[10, 20, 30, 40]` 时，两次写的 `addr` 与 `data_write` 分别是 `(addr=0, data=0x140A)` 与 `(addr=2, data=0x281E)`（即 `20<<8 | 10 = 0x140A`，`40<<8 | 30 = 0x281E`）。**待本地验证**。

#### 4.2.5 小练习与答案

**Q**：如果图像像素总数是奇数，最后一个像素会发生什么？

**参考答案**：`wr_en` 只在奇地址（`memory_addr_counter[0]==1`）置位。最后一个像素若落在偶地址，它会被放进 `data_write[7:0]`，但还没等到配对的奇字节，`counter_read` 就已经减到 1、状态跳回 `STATE_WAIT_COMMAND` 了——这一字节很可能不会被真正写入。正因如此，源码里多处注释写着 `image size mod 2 should be 0`，要求图像像素数必须是偶数。本项目自带的测试图都是 `256×256`，自然满足。

---

### 4.3 STATE_READ_IMG 与 mem_data_buffer：读存储→缓冲→逐字节输出两级流水

#### 4.3.1 概念说明

读出方向与 4.2 相反：存储里是 16 位字，主机要逐字节地收。所以 `STATE_READ_IMG` 要做「**1 出 2**」：每读 1 个字，拆成 2 个字节，分两拍发给主机。

但还有一层麻烦：单端口 RAM 的读有延迟——`rd_en` 发出后，`data_read` 要到下一拍才有效（由 `data_read_valid` 标志）。所以不能「同拍读同拍发」。项目的解法是用一个 16 位中间缓冲 `mem_data_buffer` 加一个满标志 `mem_data_buffer_full`（[hdl/image_processing.v:90-91](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L90-L91)），构成两级流水：

```
存储 ──rd_en/data_read──▶ mem_data_buffer ──comm_data_out──▶ 主机
        （有读延迟）          （满标志 full）       （受 free 反压）
```

#### 4.3.2 核心流程

整个状态按 `memory_addr_counter[0]` 分成两个阶段（它既是读地址指针，又是阶段选择器）：

```
偶阶段（memory_addr_counter[0] == 0）—— 发起读：
   rd_en <= 1
   addr  <= memory_addr_counter
   memory_addr_counter <= memory_addr_counter + 1   // 推进到奇阶段

奇阶段（memory_addr_counter[0] == 1）—— 消费与输出：
   // 子步骤 A：等读数据到达，锁进缓冲（仅 counter_read 为偶时）
   if(counter_read[0]==0 && data_read_valid) :
       mem_data_buffer      <= data_read
       mem_data_buffer_full <= 1
   // 子步骤 B：等主机准备好，按 counter_read 奇偶输出一字节
   if(comm_data_out_free && mem_data_buffer_full) :
       if(counter_read[0]==0) :                       // 输出低字节
           comm_data_out <= mem_data_buffer[7:0]
           counter_read  <= counter_read - 1
       else :                                          // 输出高字节
           comm_data_out       <= mem_data_buffer[15:8]
           counter_read        <= counter_read - 1
           memory_addr_counter <= memory_addr_counter + 1   // 推进回偶阶段
           mem_data_buffer_full<= 0
           if(counter_read <= 1) state <= STATE_WAIT_COMMAND // 收工
```

**关键结论（也是本讲练习的核心）**：`memory_addr_counter` 只在「输出高字节」时才 +1（另一处 +1 在偶阶段发起读）。原因是一个 16 位读提供两个输出字节，读地址只需每两个字推进一次：

- 偶阶段那次 +1，把指针从「本字读地址」推到「正在吐本字」（阶段切换）。
- 高字节输出那次 +1，把指针从「本字吐完」推到「下一个字的读地址」（回到偶阶段）。
- 低字节输出**不** +1，因为低、高两个字节来自同一个已读出的字——若此时就推进，会跳过还没发的高字节。

#### 4.3.3 源码精读

完整状态见 [hdl/image_processing.v:373-401](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L373-L401)：

```verilog
STATE_READ_IMG: begin
   if(memory_addr_counter[0] == 1'b0) begin          // 偶阶段：发起读
      rd_en <= 1;
      addr <= memory_addr_counter;
      memory_addr_counter <= memory_addr_counter + 1;
   end else begin                                     // 奇阶段：锁存 + 输出
      if (counter_read[0] == 1'b0 && data_read_valid == 1'b1) begin
         mem_data_buffer <= data_read;
         mem_data_buffer_full <= 1;
      end
      if( comm_data_out_free == 1 && mem_data_buffer_full == 1 ) begin
         if (counter_read[0] == 1'b0) begin           // 输出低字节
            comm_data_out_valid <= 1;
            comm_data_out <= mem_data_buffer[7:0];
            counter_read <= counter_read - 1;
         end else begin                               // 输出高字节
            comm_data_out_valid <= 1;
            comm_data_out <= mem_data_buffer[15:8];
            counter_read <= counter_read - 1;
            memory_addr_counter <= memory_addr_counter+1;   // 只在这里推进地址
            mem_data_buffer_full <= 0;
            if(counter_read <= 1) begin               // shift by one
               state <= STATE_WAIT_COMMAND;
            end
         end
      end
   end
end
```

三个细节：

1. **锁存条件 `counter_read[0]==0`**：只在输出低字节之前那一段（counter 为偶）把数据锁进缓冲；输出高字节时（counter 为奇）不再重复锁存，避免覆盖。
2. **`counter_read[0]` 同时承担「区分低/高字节」**：counter 为偶 → 吐低字节；为奇 → 吐高字节。每吐一个字节 counter -1，自然在奇偶间来回切换。
3. **收工判断 `counter_read <= 1`**（注释 `shift by one`）：因为吐高字节的那拍用的是「本拍开始时的旧 `counter_read`」，而非阻塞赋值的新值要到下拍才生效，所以要提前一拍用 `<= 1` 判断。

主机侧对应函数是 `read_image`（[simulation/image_processing_simulation.cpp:150-165](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/simulation/image_processing_simulation.cpp#L150-L165)）：它先发 `COMMAND_READ_IMG`，然后空转 `W*H*20` 拍（为两级流水 + 反压留足余量），最后从 `fifo_out` 取走 `W*H` 个字节。

#### 4.3.4 代码实践（对应本讲核心练习）

**实践目标**：把 `STATE_READ_IMG` 的两级流水时序彻底走通，并解释「读地址只在输出高字节时 +1」。

**操作步骤**：

1. 假设 `buffer_input_address = 0`，`counter_read = W*H = 4`（即 2 个 16 位字、4 个像素），存储里两个字为 `word0 = 0x140A`（低字节=10、高字节=20）、`word1 = 0x281E`（30、40）。
2. 逐拍填出下表（每一行 = 一个 `posedge clk`，列出新值均指「下一拍生效」的非阻塞结果）。

**需要观察的现象 / 预期结果**（待本地逐拍核对）：

| 拍 | `memory_addr_counter[0]` | 动作 | `rd_en`/输出 | `counter_read` | `memory_addr_counter` |
| --- | --- | --- | --- | --- | --- |
| 1 | 0（偶） | 发起读 word0 | `rd_en=1, addr=0` | 4 | 0 → 1 |
| 2 | 1（奇） | `data_read_valid`，锁存 | `mem_data_buffer=0x140A, full=1` | 4 | 1 |
| 3 | 1（奇） | 输出低字节 | `comm_data_out=0x0A`(=10) | 4 → 3 | 1 |
| 4 | 1（奇） | 输出高字节 + 推进 | `comm_data_out=0x14`(=20) | 3 → 2 | 1 → 2 |
| 5 | 0（偶） | 发起读 word1 | `rd_en=1, addr=2` | 2 | 2 → 3 |
| 6 | 1（奇） | 锁存 word1 | `mem_data_buffer=0x281E, full=1` | 2 | 3 |
| 7 | 1（奇） | 输出低字节 | `comm_data_out=0x1E`(=30) | 2 → 1 | 3 |
| 8 | 1（奇） | 输出高字节 + 收工 | `comm_data_out=0x28`(=40) | 1 → 0 | 3 → 4，回 `WAIT_COMMAND` |

**解释为什么读地址只在输出高字节时 +1**（结合上表）：`memory_addr_counter` 身兼两职——偶值时它是「下一个要读的字地址」，奇值时它是「正在吐出的当前字」。每读完一个字需要推进两次：偶阶段发起读时 `0→1`（进入吐字阶段），高字节输出完时 `1→2`（进入下一个字的读阶段）。低字节输出那拍不能 +1，否则会立刻切回偶阶段、抢在吐高字节之前发起新的读，把当前字的高字节冲掉。换句话说，**「高字节输出」是「当前字两字节都已消费完」的信号，只有这时才该推进读指针**。

#### 4.3.5 小练习与答案

**Q1**：锁存条件为什么写成 `counter_read[0]==0 && data_read_valid`，而不是只用 `data_read_valid`？

**参考答案**：因为在奇阶段里，`counter_read` 为偶表示「正准备输出低字节、缓冲还没被消费」，此时该把新读到的字锁进来；而 `counter_read` 为奇时是在输出高字节、缓冲里的字还没吐完，此时若再锁存会用下一个 `data_read`（此时还无效/不属于本字）覆盖掉正在用的数据。用 `counter_read[0]==0` 把锁存严格限定在「低字节输出之前」。

**Q2**：`mem_data_buffer_full` 在什么时候被置 1、什么时候被清 0？

**参考答案**：在奇阶段、`counter_read` 为偶且 `data_read_valid` 时置 1（表示「一个完整的字已进入缓冲，可以开始吐字节」）；在输出高字节的那一拍清 0（表示「这个字的两字节都吐完了，缓冲空了，等下一个字」）。

---

### 4.4 STATE_GET_STATUS 与 busy 位：4 字节状态回传

#### 4.4.1 概念说明

卷积这类运算要算很久，主机必须能知道硬件「忙不忙」，才能决定何时读结果。`COMMAND_GET_STATUS` 就是这个探询命令：它让硬件回传 **4 个字节**的状态，其中**第 1 个字节的 bit0 就是 busy 位**，后 3 个字节保留为 0。

主机侧的 `wait_end_busy`（[simulation/image_processing_simulation.cpp:130-148](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/simulation/image_processing_simulation.cpp#L130-L148)）就是反复发 `GET_STATUS`、读回 4 字节、看第 1 字节的 bit0，直到它变 0。

#### 4.4.2 核心流程

```
派发：counter_read <= 3                      // 预装 3
每拍（comm_data_out_free==1）输出 1 字节：
   counter_read==3 : comm_data_out[0] <= ~(state_processing == STATE_IDLE)   // busy
   counter_read==2 : comm_data_out <= 0
   counter_read==1 : comm_data_out <= 0
   counter_read==0 : comm_data_out <= 0
   if(counter_read > 0) counter_read <= counter_read - 1
   else                 state <= STATE_WAIT_COMMAND
```

`busy = ~(state_processing == STATE_IDLE)`：运算 FSM（`state_processing`）只要不在空闲态 `STATE_IDLE`，busy 就是 1。注意它取自**运算 FSM** 而非主 FSM 的 `state`——这是 u3-l3 双 FSM 架构的好处：运算期间主 FSM 早已回到 `STATE_WAIT_COMMAND`，所以它仍能响应这次 `GET_STATUS`，而 busy 位由运算 FSM 真实反映「在不在算」。

#### 4.4.3 源码精读

完整状态见 [hdl/image_processing.v:310-333](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L310-L333)：

```verilog
STATE_GET_STATUS: begin
   if(comm_data_out_free == 1) begin
      if(counter_read == 3) begin                  // 第 1 字节：busy
         comm_data_out_valid <= 1;
         comm_data_out[7:0] <= 8'h0;
         comm_data_out[0] <= ~(state_processing == STATE_IDLE);
      end else if(counter_read == 2) begin         // 第 2 字节：保留
         comm_data_out_valid <= 1;
         comm_data_out[7:0] <= 8'h0;
      end else if(counter_read == 1) begin         // 第 3 字节：保留
         comm_data_out_valid <= 1;
         comm_data_out[7:0] <= 8'h0;
      end else begin                               // 第 4 字节：保留
         comm_data_out_valid <= 1;
         comm_data_out[7:0] <= 8'h0;
      end

      if(counter_read > 0) begin
         counter_read <= counter_read - 1;
      end else begin
         state <= STATE_WAIT_COMMAND;
      end
   end
end
```

主机侧 `wait_end_busy` 的轮询循环（节选自 [simulation/image_processing_simulation.cpp:130-148](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/simulation/image_processing_simulation.cpp#L130-L148)）：

```cpp
do{
   fifo_in.push(Operation(true, COMMAND_GET_STATUS, 0));
   for (size_t i = 0; i < 100; i++) main_loop_clk();
   for (size_t i = 0; i < 4; i++) {
      status_out[i] = fifo_out.front(); fifo_out.pop();   // 取回 4 字节
   }
} while( (status_out[0] & 0x01 == 1) );                   // bit0==1 就继续等
```

第 1 个取回的字节 `status_out[0]` 正对应硬件 `counter_read==3` 那拍发出的 busy 字节。

#### 4.4.4 代码实践

**实践目标**：亲眼看到 busy 位在「运算中」与「运算后」的差别。

**操作步骤**：

1. 读 `software/main.cpp` 里任意一个耗时测试（如卷积 `test_simple_edge_detection`），注意它发出运算命令后会调用 `wait_end_busy`。
2. 在 `wait_end_busy` 的 `printf("status %lu = 0x%x\n", …)`（[simulation/image_processing_simulation.cpp:143](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/simulation/image_processing_simulation.cpp#L143)）处观察打印。

**需要观察的现象 / 预期结果**：

- 循环头几次：`status 0 = 0x1`（busy=1，运算 FSM 正在算）。
- 最后一次：`status 0 = 0x0`（busy=0，运算 FSM 回到 `STATE_IDLE`），循环退出。
- 若想「源码阅读型」验证：对照 [hdl/image_processing.v:630](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L630) 起的 `STATE_PROC_CONVOLUTION`，确认它在算完前 `state_processing` 一直 ≠ `STATE_IDLE`，故 busy 一直为 1。

#### 4.4.5 小练习与答案

**Q**：为什么 busy 位取自 `state_processing`（运算 FSM）而不是 `state`（主 FSM）？

**参考答案**：因为主 FSM 在派发完运算命令后立刻回到 `STATE_WAIT_COMMAND`（见 u3-l3），整个漫长的运算期间 `state` 都是 `STATE_WAIT_COMMAND`——若 busy 取自 `state`，就永远查不到「忙」。而运算 FSM `state_processing` 在运算期间处于 `STATE_PROC_*`，算完才回 `STATE_IDLE`，所以只有它能真实反映「在不在算」。这也正是双 FSM 分工的价值：主 FSM 腾出手来继续响应 `GET_STATUS`，运算 FSM 专心干活并提供 busy 信号。

---

## 5. 综合实践

把本讲四个状态串起来，做一次「**发图 → 取反 → 读图**」的端到端计数核对。任选其一即可。

**任务 A（运行型，待本地验证）**：在仿真模式下跑 `test_invert`（或任意先 `send_image` 再 `read_image` 的测试），借助 `main_loop_clk` 里的 `printf`（[simulation/image_processing_simulation.cpp:255-263](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/simulation/image_processing_simulation.cpp#L255-L263)）统计：

- `STATE_SEND_IMG` 阶段：`wr_en==1` 的次数 = ?（预期 `W*H/2`）
- `STATE_READ_IMG` 阶段：`read req addr` 打印次数 = ?（预期 `W*H/2`），`comm_data_out_valid` 置位次数 = ?（预期 `W*H`）

把三个数字填进下表，验证「写入 `2 进 1`、读出请求 `1 出 2`、字节输出逐个」的一致性。

| 量 | 公式预期 | 实测 |
| --- | --- | --- |
| `wr_en` 次数（发图） | `W*H/2` | 待验证 |
| `rd_en` 次数（读图） | `W*H/2` | 待验证 |
| `comm_data_out_valid` 次数（读图） | `W*H` | 待验证 |

**任务 B（源码阅读型，无需运行）**：以 `COMMAND_APPLY_INVERT`（取反，零参数运算）为线索，按时间顺序列出从主机调用到结果回读所经过的全部状态、每个状态里 `counter_read` 与 `memory_addr_counter` 的变化，画成一张时序表。参考链路：

1. `STATE_WAIT_COMMAND`（`COMMAND_SEND_IMG`）→ 预装 `counter_read=W*H`
2. `STATE_SEND_IMG`（2 进 1 打包写入 input 缓冲）
3. `STATE_WAIT_COMMAND`（`COMMAND_APPLY_INVERT`）→ 立刻交接 `state_processing<=STATE_PROC_UNARY`（无参数，不经过 `READ_PARAM`）
4. `STATE_PROC_UNARY`（逐字取反写回 storage 缓冲；这是 u4-l1 的内容，这里只需知道它在算）
5. 主机 `wait_end_busy` 反复进 `STATE_GET_STATUS`，直到 busy=0
6. `STATE_WAIT_COMMAND`（`COMMAND_SWITCH_BUFFERS`）→ 互换两个缓冲地址
7. `STATE_WAIT_COMMAND`（`COMMAND_READ_IMG`）→ 预装 `counter_read=W*H`
8. `STATE_READ_IMG`（1 出 2 两级流水，逐字节吐回主机）

## 6. 本讲小结

- `counter_read` 是一个**状态相关**的通用倒计数器：派发时预装初值，消费状态每处理一个字节就 -1，终止条件由各状态自定（`SEND_IMG` 用 `>1`、`GET_STATUS`/`READ_PARAM` 用 `==0` 还输出一字节），所以「预装值」与「字节数」未必相等，切忌套公式。
- `STATE_SEND_IMG` 做「**2 进 1**」：偶字节进 `data_write[7:0]`、奇字节进 `[15:8]` 并置 `wr_en`，地址清最低位得字地址；要求像素数为偶。
- `STATE_READ_IMG` 用 `mem_data_buffer` + `mem_data_buffer_full` 构成「**读存储 → 缓冲 → 逐字节输出**」两级流水，以吸收单端口 RAM 的读延迟与主机的 `comm_data_out_free` 反压。
- 读地址 `memory_addr_counter` 只在「发起读」和「输出高字节」时 +1：高字节输出代表「当前字两字节消费完」，唯有此时才推进到下一个字；低字节输出不推进，以免冲掉未发的高字节。
- `STATE_GET_STATUS` 回传 4 字节，第 1 字节 bit0 = `~(state_processing==STATE_IDLE)` 即 busy，取自运算 FSM；主机 `wait_end_busy` 据此轮询同步。
- 收发与状态查询都靠握手推进：输入侧 `comm_data_in_valid`、输出侧 `comm_data_out_valid` + `comm_data_out_free`、存储侧 `data_read_valid`——这三组握手是本讲所有时序的节拍器。

## 7. 下一步学习建议

- **u4 单元（运算 FSM）**：本讲只讲了「搬数据」的状态；`STATE_PROC_UNARY` / `STATE_PROC_BINARY` 如何用同样的「单口 RAM 两拍 + 16 位字双像素」套路做真正的加减/阈值/乘法运算，是下一单元的主题。读完你会发现 `proc_memory_addr_counter[0]` 的偶/奇两拍用法与本讲 `STATE_READ_IMG` 如出一辙。
- **u6-l1（仿真后端）**：本讲多次引用 `main_loop_clk`，下一阶段应系统了解它如何手动翻转时钟、用 `fifo_in/fifo_out` 模拟通信、以及 `counter_free==3` 如何模拟输出线被占满的反压。
- **u6-l2（硬件后端 RAM 接口）**：本讲的 `data_read_valid` 在仿真里是「下一拍给出」，在真实 SPRAM 上则有固定的硬件读延迟——`ram_interface.v` 用 `rd_en_buffer` 流水线来对齐这个延迟，值得对照阅读，理解「同一份 FSM 适配两种后端」的下半段。
