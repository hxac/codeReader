# 主命令处理状态机：STATE_WAIT_COMMAND 派发

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `image_processing.v` 里**两条并行状态机** `state` 与 `state_processing` 各自的职责，以及它们为什么必须分开。
- 把 `COMMAND_*`（命令操作码）和 `STATE_*`（状态值）两套 `parameter` 枚举对应起来，并能手算每个枚举的数值。
- 画出 `STATE_WAIT_COMMAND` 这个派发 `case` 的完整分支表：每条命令跳到哪个状态、预装多少个参数字节。
- 解释一条运算命令（如 `COMMAND_APPLY_ADD`）是如何通过 `processing_command` 寄存器把控制权「交接」给运算 FSM 的。

本讲只讲**命令如何被接收、解析、派发**，以及**运算如何被启动**；参数字节的逐字节时序（`counter_read` 倒计时的细节）留给 u3-l4，各类运算的具体算法留给 u4、u5。

## 2. 前置知识

本讲假设你已掌握（来自 u3-l1、u3-l2、u2-l2）：

- **端口两扇门**：`image_processing` 模块对外只有存储器接口和通信接口；通信接口里 `comm_cmd`（8 位操作码）、`comm_data_in`（8 位数据）、`comm_data_in_valid`（字节有效选通）是主机喂命令的入口。
- **命令报文格式**：一条命令 = 1 字节操作码 + 0 到 N 字节参数（小端 16 位）。操作码取自 `Commands` 枚举，与硬件侧 `COMMAND_*` 数值一一对应。
- **双缓冲**：`buffer_input_address` / `buffer_storage_address` 指向两块 64KB 区间，可经 `COMMAND_SWITCH_BUFFERS` 互换。
- **有限状态机（FSM）直觉**：FSM 是一种「记住当前在哪个步骤、根据输入跳到下一步」的电路结构。每个时钟沿，当前状态 + 输入共同决定下一个状态。

一个关键澄清（来自仿真后端验证）：`comm_data_in_valid` 是一个**通用「字节就绪」选通**。当到来的字节是**操作码**时，它出现在 `comm_cmd` 上；当到来的字节是**参数**时，它出现在 `comm_data_in` 上。两者共用同一个 valid 选通，由发送端用 `is_command` 标志区分。因此 `STATE_WAIT_COMMAND` 只在 `comm_data_in_valid==1` 时去读 `comm_cmd`。详见 [simulation/image_processing_simulation.cpp:L238-L245](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/simulation/image_processing_simulation.cpp#L238-L245)。

## 3. 本讲源码地图

本讲几乎全部围绕一个文件：

| 文件 | 作用 |
|---|---|
| [hdl/image_processing.v](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v) | 核心模块。本讲关注其中的：状态/命令枚举、双 FSM 的 `case` 结构、`STATE_WAIT_COMMAND` 派发、`processing_command` 交接 |
| [simulation/image_processing_simulation.cpp](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/simulation/image_processing_simulation.cpp) | 仿真后端。仅在「验证 comm 握手」时引用，帮助理解命令字节如何驱动 FSM |

## 4. 核心概念与源码讲解

### 4.1 双状态机架构：state 与 state_processing

#### 4.1.1 概念说明

先想一个问题：一次卷积要扫完整整一幅 256×256 的图，可能耗时成千上万个时钟周期。如果模块只有**一条**状态机，那么在卷积进行期间，主机发来的任何命令（哪怕是「你现在忙不忙？」的状态查询）都无法被响应——因为状态机正忙着算像素。

本项目用一个优雅的办法解决：**把「接命令」和「算像素」拆成两条独立的状态机**，共享同一个时钟，但各自维护自己的状态寄存器：

- `state`：**主状态机**（命令解析 FSM）。负责接收操作码、读参数、收发图像、回答状态查询、把运算「踢」出去。
- `state_processing`：**运算状态机**。一旦被主状态机启动，就独立地扫存储器、做运算、写回结果，直到完成后回到空闲。

这样，当 `state_processing` 在跑一次漫长的卷积时，`state` 依然能立刻响应 `COMMAND_GET_STATUS`，把「忙」位报告给主机。这就是两条 FSM 必须分开的根本原因：**让命令通道永远在线，不被长运算阻塞**。

形式化一点，两条 FSM 是两个独立的转移函数，共享同一时钟域：

\[
\begin{aligned}
state^{t+1} &= \delta_{\text{main}}\!\left(state^{t},\ \text{comm\_cmd}^{t},\ \text{comm\_data\_in\_valid}^{t},\ \ldots\right) \\
state\_processing^{t+1} &= \delta_{\text{proc}}\!\left(state\_processing^{t},\ \text{processing\_command},\ \text{data\_read\_valid}^{t},\ \ldots\right)
\end{aligned}
\]

而主机最关心的「忙」信号，就是看运算 FSM 是否还在空闲态以外：

\[
busy^{t} = \left(state\_processing^{t} \neq \text{STATE\_IDLE}\right)
\]

#### 4.1.2 核心流程

两条 FSM 的协作流程：

1. 上电/构造时：`state = STATE_WAIT_COMMAND`，`state_processing = STATE_IDLE`（运算 FSM 空闲）。
2. 主状态机停在 `STATE_WAIT_COMMAND`，等 `comm_data_in_valid`。
3. 收到一条运算命令 → 读参数 → 把 `state_processing` 置为对应运算态、把命令记进 `processing_command` → **主状态机立刻回到 `STATE_WAIT_COMMAND`**，准备接下一条命令。
4. 运算 FSM 在后台独立运行，每个时钟沿推进一拍。
5. 主机随时可发 `COMMAND_GET_STATUS`，主状态机读 `state_processing` 算出 `busy` 位回传。
6. 运算 FSM 跑完，自己回到 `STATE_IDLE`，下一次状态查询的 `busy` 位变为 0。

关键点：**第 3 步之后，两条 FSM 互不阻塞**。主状态机不会被运算拖住，运算 FSM 也不会因为主状态机去接新命令而中断（只要主机遵守「等忙位清零再发下一条依赖命令」的约定）。

#### 4.1.3 源码精读

两条 FSM 的状态寄存器声明在一起，[hdl/image_processing.v:L57-L60](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L57-L60)：

```verilog
reg [7:0] state;
reg [7:0] state_processing;
reg [7:0] processing_command;
```

它们被放在**同一个 `always @(posedge clk)` 块**里，但用**两个独立的 `case`** 驱动。`always` 块开头先给输出置默认值，然后是主 FSM 的 `case(state)`，[hdl/image_processing.v:L210-L218](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L210-L218)：

```verilog
always @(posedge clk)
begin
   //default
   comm_data_out_valid <= 0;
   wr_en <= 0;
   rd_en <= 0;

   case (state)
   ...
   endcase
```

主 FSM 的 `case` 一直延伸到 [hdl/image_processing.v:L499-L501](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L499-L501) 处 `endcase`，紧接着**同一个 `always` 块里**就是运算 FSM 的 `case(state_processing)`，[hdl/image_processing.v:L503-L505](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L503-L505)：

```verilog
   case (state_processing)
   STATE_IDLE: begin
   end
   STATE_PROC_UNARY: begin : unary
   ...
```

「同一时钟沿、两个 `case` 顺序求值」正是双 FSM 并行的实现方式——每个上升沿，两条 FSM 各自根据自己的状态寄存器和输入独立跳转。

`busy` 位就长在状态查询里。`STATE_GET_STATUS` 把运算 FSM 是否空闲取反映射到回传字节的 bit0，[hdl/image_processing.v:L312-L315](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L312-L315)：

```verilog
if(counter_read == 3) begin //first status response is "is_busy"
   comm_data_out_valid <= 1;
   comm_data_out[7:0] <= 8'h0;
   comm_data_out[0] <= ~(state_processing == STATE_IDLE);
```

注意它读的是 `state_processing`，而不是 `state`。这就是双 FSM 的 payoff：当 `state` 正停在 `STATE_GET_STATUS` 回答主机时，`state_processing` 可以同时在 `STATE_PROC_CONVOLUTION` 跑卷积，二者各走各的。

#### 4.1.4 代码实践

**实践目标**：亲眼确认两条 FSM 真的互不阻塞。

**操作步骤**：

1. 打开 [hdl/image_processing.v:L210](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L210)，确认整段 `always @(posedge clk)` 块从 L210 一直到 L843（`end` ）才结束；中间 L218 和 L503 各有一个 `case`。
2. 找到 `STATE_GET_STATUS`（[L310-L333](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L310-L333)），确认它只读写 `comm_data_out*`、`counter_read`、`state`，**完全不碰** `state_processing` 的值（只读取它来算 busy）。
3. 找到 `STATE_PROC_CONVOLUTION`（[L630](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L630)），确认它只读写 `proc_conv_memory_addr_*`、`convolution_*`、`state_processing`，**完全不读** `state`。

**需要观察的现象**：两个 `case` 的状态变量互不交叉赋值——主 FSM 不改 `state_processing`（除了启动时那次交接），运算 FSM 不改 `state`。

**预期结果**：你能得出结论：在一次卷积期间，`state` 完全可以走 `STATE_WAIT_COMMAND → STATE_GET_STATUS → STATE_WAIT_COMMAND` 来回答「忙」，而卷积一拍都不停。这正是主机 `wait_end_busy` 轮询机制能工作的硬件基础。

> 「待本地验证」：若你跑过 u1-l3 的仿真，可在 `test_simple_edge_detection` 的 `wait_end_busy` 处加日志，理论上会看到多次 `COMMAND_GET_STATUS` 查询，且前期 bit0=1、卷积结束后变 0。

#### 4.1.5 小练习与答案

**练习 1**：如果把两条 FSM 合并成一条（即把 `state_processing` 的所有分支塞进 `state` 的 `case`），`COMMAND_GET_STATUS` 会出现什么问题？

**参考答案**：合并后，当 `state` 处于某个运算态（如卷积的 9 拍累加）时，它无法同时跳去处理 `STATE_GET_STATUS`，主机的状态查询会得不到响应，`wait_end_busy` 会卡死或失真。拆成两条 FSM 才能让命令通道在长运算期间保持可用。

**练习 2**：`busy` 位为什么取自 `state_processing` 而不是 `state`？

**参考答案**：因为真正代表「运算还没结束」的是运算 FSM。主 FSM 在运算期间会一直停在 `STATE_WAIT_COMMAND`（≠ `STATE_IDLE`），若用 `state` 判忙会得出「永远忙」的错误结论；而 `state_processing` 在运算结束时自动回到 `STATE_IDLE`，是判忙的正确来源。

---

### 4.2 命令枚举 COMMAND_* 与状态枚举 STATE_*

#### 4.2.1 概念说明

硬件里没有「字符串」，所有命令和状态都是 8 位整数。本项目用 Verilog 的 `parameter` 给这些整数起名字，形成两套枚举：

- **`COMMAND_*`**：命令操作码。主机发的操作码字节就是这些值，与软件侧 `image_processing.hpp` 的 `Commands` 枚举**逐字对应**（见 u2-l1）。这是软硬四方的根本契约。
- **`STATE_*`**：状态值。给两条 FSM 的状态寄存器取的名字，纯硬件内部使用，主机看不到。

两套枚举都采用「前一个 +1」的连写法定义，而不是直接写数字。好处是增删一项时后面的值自动顺延，坏处是读源码时要心算才知道某项的真实数值。

#### 4.2.2 核心流程

命令操作码的生命周期：

1. 主机把高层调用（如 `send_add(...)`）打包成「操作码字节 + 参数字节」。
2. 操作码字节的数值 = 对应的 `COMMAND_*`。
3. 模块在 `STATE_WAIT_COMMAND` 收到操作码，用 `case (comm_cmd)` 匹配到对应 `COMMAND_*` 分支。
4. 该分支决定：跳到哪个状态读参数、预装多少个参数字节、是否启动运算 FSM。

#### 4.2.3 源码精读

命令操作码枚举定义在 [hdl/image_processing.v:L63-L68](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L63-L68)：

```verilog
parameter COMMAND_PARAM = 0, COMMAND_SEND_IMG = COMMAND_PARAM+1, COMMAND_READ_IMG = COMMAND_SEND_IMG+1,
          COMMAND_GET_STATUS = COMMAND_READ_IMG+1, COMMAND_APPLY_ADD = COMMAND_GET_STATUS+1, ...
          COMMAND_APPLY_MULT = COMMAND_BINARY_MULT+1;
```

手算出的数值表如下（与 u2-l1 软件侧 `Commands` 枚举完全一致）：

| 操作码 | 数值 | 含义 |
|---|---|---|
| `COMMAND_PARAM` | 0 | 设置图像宽高（兼初始化） |
| `COMMAND_SEND_IMG` | 1 | 主机→模块发送图像 |
| `COMMAND_READ_IMG` | 2 | 模块→主机回读图像 |
| `COMMAND_GET_STATUS` | 3 | 查询 busy 状态 |
| `COMMAND_APPLY_ADD` | 4 | 逐像素加法 |
| `COMMAND_APPLY_THRESHOLD` | 5 | 阈值处理 |
| `COMMAND_SWITCH_BUFFERS` | 6 | 互换 input/storage 缓冲 |
| `COMMAND_BINARY_ADD` | 7 | 双图加法 |
| `COMMAND_APPLY_INVERT` | 8 | 逐像素取反 |
| `COMMAND_CONVOLUTION` | 9 | 3×3 卷积 |
| `COMMAND_BINARY_SUB` | 10 | 双图减法（可取绝对差） |
| `COMMAND_BINARY_MULT` | 11 | 双图乘法 |
| `COMMAND_APPLY_MULT` | 12 | 逐像素乘法（定点） |

状态枚举（给 FSM 用）定义在 [hdl/image_processing.v:L32-L43](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L32-L43)，同样用「前一个 +1」连写。其中值得现在记住的关键几个：`STATE_IDLE=0`、`STATE_WAIT_COMMAND=1`，以及运算 FSM 会用到的 `STATE_PROC_UNARY`、`STATE_PROC_BINARY`、`STATE_PROC_CONVOLUTION`（这三个是运算 FSM 的「入口态」，后续模块会反复出现）。

注意一个命名规律：每个**需要参数**的运算命令，都配一个对应的 `*_READ_PARAM` 状态（如 `COMMAND_APPLY_ADD` → `STATE_APPLY_ADD_READ_PARAM`）。读参数状态读完参数后，才去启动运算 FSM。这是下一节派发表的核心线索。

#### 4.2.4 代码实践

**实践目标**：亲手把两套枚举的数值算出来，验证命令操作码与软件侧一致。

**操作步骤**：

1. 打开 [hdl/image_processing.v:L63-L68](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L63-L68)，从 `COMMAND_PARAM = 0` 开始，依次 +1 填出上面那张数值表。
2. 打开 `software/image_processing.hpp`（u2-l1 已读过），找到 `Commands` 枚举，逐项比对数值。

**需要观察的现象**：两边数值应逐字相等。

**预期结果**：例如 `COMMAND_APPLY_INVERT` 在硬件侧 = 8，软件侧 `COMMAND_APPLY_INVERT` 也 = 8。任何不一致都会导致「主机发的命令被硬件当成另一条」的灾难性 bug——这正是 u2-l1 强调「新增命令须两侧同步」的原因。

#### 4.2.5 小练习与答案

**练习 1**：为什么枚举用 `X = Y+1` 连写，而不是 `X = 5` 这样直接写数字？

**参考答案**：连写法便于在中间插入或删除一项，后续项数值自动顺延，不用手工重排；代价是可读性略差，需心算才知道某项的值。

**练习 2**：如果有人在硬件侧 `COMMAND_CONVOLUTION` 和 `COMMAND_BINARY_SUB` 之间插入一个新命令 `COMMAND_X`，但忘了改软件侧枚举，会怎样？

**参考答案**：从 `COMMAND_BINARY_SUB` 往后的所有操作码在硬件侧整体 +1，与软件侧错位。主机发 `COMMAND_BINARY_SUB`（软件值 10）会被硬件当成插入后的 `COMMAND_X`（硬件值 10），命令被完全误解。这正是「四方契约必须同步」的现实风险。

---

### 4.3 STATE_WAIT_COMMAND 派发 case

#### 4.3.1 概念说明

`STATE_WAIT_COMMAND` 是主状态机的「总台」。模块空闲时就停在这里，等主机送来一个操作码字节。收到后，它像前台接线员一样，根据操作码把流程导向不同的后续状态：

- **数据搬运类**（发图/读图/查状态）：跳到对应状态，由该状态处理整批字节。
- **需要参数的运算类**：跳到对应的 `*_READ_PARAM` 状态，先读参数再启动运算。
- **零参数运算类**（只有取反）：**当场**启动运算 FSM，立刻回到 `STATE_WAIT_COMMAND`。
- **缓冲管理类**（切换缓冲）：**当场**完成，立刻回到 `STATE_WAIT_COMMAND`。

每条分支还会给 `counter_read` 预装一个数，告诉后续状态「接下来要读几个参数字节」。

#### 4.3.2 核心流程

`STATE_WAIT_COMMAND` 的派发逻辑（伪代码）：

```
每个时钟沿：
  若 comm_data_in_valid == 1：        # 收到一个字节
    根据 comm_cmd 选择分支：
      COMMAND_PARAM        → 去读宽高参数；顺便重置双缓冲地址
      COMMAND_SEND_IMG     → 去收图，预装 W*H 个字节
      COMMAND_READ_IMG     → 去读图，预装 W*H 个字节
      COMMAND_GET_STATUS   → 去回传 4 字节状态
      COMMAND_APPLY_ADD    → 去读 add 参数（3 字节）
      COMMAND_APPLY_THRESHOLD → 去读阈值参数（3 字节）
      COMMAND_SWITCH_BUFFERS → 当场互换缓冲，原地返回
      COMMAND_BINARY_ADD   → 去读 1 字节参数
      COMMAND_APPLY_INVERT → 当场启动运算 FSM，原地返回
      COMMAND_CONVOLUTION  → 去读 10 字节参数（1 参数 + 9 核）
      COMMAND_BINARY_SUB   → 去读 1 字节参数
      COMMAND_APPLY_MULT   → 去读 2 字节参数
      default              → 忽略
```

#### 4.3.3 源码精读

派发总台的结构在 [hdl/image_processing.v:L221-L286](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L221-L286)。外层先判 valid，内层 `case(comm_cmd)` 派发：

```verilog
STATE_WAIT_COMMAND: begin
   if(comm_data_in_valid == 1)
   begin
      case (comm_cmd)
      COMMAND_PARAM: begin //also acts as init
         state <= STATE_READ_COMMAND_PARAM_WIDTH;
         counter_read <= 1; //will be used to read the 16bits
         buffer_storage_address <= BUFFER2_LOCATION;
         buffer_input_address <= 0;
      end
      ...
```

下面把**每个分支**的跳转目标、`counter_read` 预装值、是否当场启动运算整理成派发总表（请对照源码逐行核对）：

| `comm_cmd` 分支 | 源码行 | `counter_read` 预装 | 下一 `state` | 当场启动运算 FSM？ |
|---|---|---|---|---|
| `COMMAND_PARAM` | [L225-L230](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L225-L230) | 1 | `STATE_READ_COMMAND_PARAM_WIDTH` | 否（兼重置双缓冲地址） |
| `COMMAND_SEND_IMG` | [L231-L235](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L231-L235) | `img_width*img_height` | `STATE_SEND_IMG` | 否 |
| `COMMAND_READ_IMG` | [L236-L240](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L236-L240) | `img_width*img_height` | `STATE_READ_IMG` | 否 |
| `COMMAND_GET_STATUS` | [L241-L244](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L241-L244) | 3 | `STATE_GET_STATUS` | 否 |
| `COMMAND_APPLY_ADD` | [L245-L248](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L245-L248) | 2 | `STATE_APPLY_ADD_READ_PARAM` | 否（读完参数才启动） |
| `COMMAND_APPLY_THRESHOLD` | [L249-L252](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L249-L252) | 2 | `STATE_THRESHOLD_READ_PARAM` | 否（读完参数才启动） |
| `COMMAND_SWITCH_BUFFERS` | [L253-L257](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L253-L257) | — | `STATE_WAIT_COMMAND`（原地） | 否（当场互换缓冲） |
| `COMMAND_BINARY_ADD` | [L258-L261](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L258-L261) | 0 | `STATE_BINARY_ADD_READ_PARAM` | 否 |
| `COMMAND_APPLY_INVERT` | [L262-L269](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L262-L269) | 0 | `STATE_WAIT_COMMAND`（原地） | **是**（当场启动！） |
| `COMMAND_CONVOLUTION` | [L270-L273](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L270-L273) | 9 | `STATE_CONVOLUTION_READ_PARAM` | 否（读完参数才启动） |
| `COMMAND_BINARY_SUB` | [L274-L277](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L274-L277) | 0 | `STATE_BINARY_SUB_READ_PARAM` | 否 |
| `COMMAND_APPLY_MULT` | [L278-L281](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L278-L281) | 1 | `STATE_APPLY_MULT_READ_PARAM` | 否 |
| `default` | [L282-L283](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L282-L283) | — | —（忽略） | — |

读这张表能得到三个规律：

1. **大多数运算命令**走两步：先到 `*_READ_PARAM` 读参数，再由那个状态启动运算（见 4.4 节）。
2. **`COMMAND_APPLY_INVERT` 是唯一的例外**——它没有参数，所以在 `STATE_WAIT_COMMAND` 里**当场**同时设置 `state_processing`、`processing_command` 并原地返回。
3. **`COMMAND_SWITCH_BUFFERS` 也当场完成**：用非阻塞赋值在一个时钟沿把 `buffer_input_address` 与 `buffer_storage_address` 互换（非阻塞赋值先读旧值再写新值，所以能正确交换），然后原地返回。这是 u3-l2 讲过的「零拷贝切换」在派发层的位置。

`counter_read` 的预装值含义：它是后续 `*_READ_PARAM` 状态用来**倒计「还要读几个字节」并选择「现在装第几个字节」**的多用途计数器。例如 `COMMAND_APPLY_ADD` 预装 2，`STATE_APPLY_ADD_READ_PARAM` 据此读 3 个字节（低字节、高字节、clamp 标志）；`COMMAND_CONVOLUTION` 预装 9，读 10 个字节（1 参数字节 + 9 个卷积核字节）。字节级时序细节在 u3-l4 详讲。

#### 4.3.4 代码实践

**实践目标**：把派发总表亲手从源码里「读」出来，而不是背这张表。

**操作步骤**：

1. 打开 [hdl/image_processing.v:L221-L286](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L221-L286)。
2. 对 `case(comm_cmd)` 里的**每一个**分支，抄下三件事：①它给 `state` 赋的下一个状态名；②它给 `counter_read` 赋的值（若无则记「—」）；③它有没有同时给 `state_processing` / `processing_command` 赋值。
3. 把结果填成一张表，与上面的派发总表对照。

**需要观察的现象**：只有 `COMMAND_APPLY_INVERT` 分支里出现 `state_processing <= STATE_PROC_UNARY` 和 `processing_command <= ...`；其余运算命令分支里**没有**这两句——它们的运算启动语句藏在各自的 `*_READ_PARAM` 状态里。

**预期结果**：你会清楚看到「`COMMAND_APPLY_ADD` 在 `STATE_WAIT_COMMAND` 里只跳到 `STATE_APPLY_ADD_READ_PARAM`，并没有当场启动运算」。这与本讲规格里提到的「运算命令如何启动运算 FSM」的关键区别是：**带参数的运算命令把启动动作推迟到读完参数之后**，只有零参数的取反是当场启动。

> 提示：如果你想在仿真里观察这条交接路径，可在 u1-l3 的 `simu` 运行后，于 `software/main.cpp` 取消注释 `test_add_threshold`（或任意带参数运算），它就会触发 `COMMAND_APPLY_ADD` 这条「派发→读参数→交接」链路。「待本地验证」实际波形。

#### 4.3.5 小练习与答案

**练习 1**：`COMMAND_SWITCH_BUFFERS` 分支里写的 `buffer_input_address <= buffer_storage_address; buffer_storage_address <= buffer_input_address;` 看似两条赋值会让两个寄存器变成同一个值，为什么实际上能正确交换？

**参考答案**：因为这是**非阻塞赋值（`<=`）**。非阻塞赋值在时钟沿先统一采样所有右侧表达式的「旧值」，再统一更新左侧。所以两条语句的右侧都取的是交换前的旧值，最终两个寄存器互换了内容。这是 Verilog 里标准的「一拍交换」写法。

**练习 2**：`COMMAND_APPLY_INVERT` 为什么是唯一在 `STATE_WAIT_COMMAND` 里当场启动运算的命令？

**参考答案**：因为取反运算**没有任何参数**（它就是 `data_write <= ~data_read`），不需要先读参数字节，所以可以省去 `*_READ_PARAM` 状态，直接在派发时把 `state_processing <= STATE_PROC_UNARY`、`processing_command <= COMMAND_APPLY_INVERT`，并原地返回 `STATE_WAIT_COMMAND`。

---

### 4.4 processing_command 寄存器与运算交接

#### 4.4.1 概念说明

`processing_command` 是一个 8 位寄存器，作用是**「记住运算 FSM 现在到底在执行哪条命令」**。

为什么要它？因为运算 FSM 的多个分支是**复用**的：

- `STATE_PROC_UNARY` 一个状态里要处理 **4 种**逐像素运算：加、阈值、取反、乘。它靠读 `processing_command` 来选分支。
- `STATE_PROC_BINARY` 一个状态里要处理 **3 种**双图运算：加、减、乘。同样靠 `processing_command` 选分支。

所以主状态机在启动运算时，必须把「这是哪条命令」存进 `processing_command`，运算 FSM 才知道该走哪个算法分支。这就像把一张「工单」交给车间，车间流水线是同一条，但根据工单内容决定装哪种零件。

#### 4.4.2 核心流程

运算交接的完整流程（以带参数的 `COMMAND_APPLY_ADD` 为例）：

1. `STATE_WAIT_COMMAND` 收到 `COMMAND_APPLY_ADD` → 跳到 `STATE_APPLY_ADD_READ_PARAM`，预装 `counter_read=2`。
2. `STATE_APPLY_ADD_READ_PARAM` 逐字节读完 `add_value`（2 字节）和 `clamp`（1 比特）。
3. **关键交接**：在读完最后一个字节的同一拍，它一次性写入：
   - `state_processing <= STATE_PROC_UNARY`（启动运算 FSM）
   - `processing_command <= COMMAND_APPLY_ADD`（递交工单）
   - `state <= STATE_WAIT_COMMAND`（主 FSM 回总台）
   - `proc_counter_read <= img_width*img_height`（运算 FSM 的像素倒计数）
   - `proc_memory_addr_counter <= buffer_storage_address`（运算起始地址）
4. 下一拍起，`STATE_PROC_UNARY` 在每个时钟沿读 `processing_command`，发现是 `COMMAND_APPLY_ADD`，于是走加法分支，扫完 `proc_counter_read` 个像素后回到 `STATE_IDLE`。

注意第 3 步：**主状态机自己回到 `STATE_WAIT_COMMAND`，把舞台让给运算 FSM**。这正是双 FSM 协作的关键一拍。

#### 4.4.3 源码精读

带参数运算命令的「交接拍」以 `STATE_APPLY_ADD_READ_PARAM` 为典型，[hdl/image_processing.v:L363-L370](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L363-L370)：

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

这一拍同时完成了「读最后一个参数」「启动运算 FSM」「递交工单」「主 FSM 回总台」四件事。其余带参数运算命令的交接拍完全同构，只是目标状态和命令名不同：

| 交接发生的状态 | 源码行 | `state_processing` 置为 | `processing_command` 置为 |
|---|---|---|---|
| `STATE_APPLY_ADD_READ_PARAM` | [L365-L366](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L365-L366) | `STATE_PROC_UNARY` | `COMMAND_APPLY_ADD` |
| `STATE_THRESHOLD_READ_PARAM` | [L412-L413](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L412-L413) | `STATE_PROC_UNARY` | `COMMAND_APPLY_THRESHOLD` |
| `STATE_APPLY_MULT_READ_PARAM` | [L491-L492](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L491-L492) | `STATE_PROC_UNARY` | `COMMAND_APPLY_MULT` |
| `STATE_BINARY_ADD_READ_PARAM` | [L422-L423](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L422-L423) | `STATE_PROC_BINARY` | `COMMAND_BINARY_ADD` |
| `STATE_BINARY_SUB_READ_PARAM` | [L465-L466](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L465-L466) | `STATE_PROC_BINARY` | `COMMAND_BINARY_SUB` |
| `STATE_BINARY_MULT_READ_PARAM` | [L477-L478](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L477-L478) | `STATE_PROC_BINARY` | `COMMAND_BINARY_MULT` |
| `STATE_CONVOLUTION_READ_PARAM` | [L445](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L445) | `STATE_PROC_CONVOLUTION` | （不设，卷积有专属状态） |
| （派发层当场）`COMMAND_APPLY_INVERT` | [L264-L265](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L264-L265) | `STATE_PROC_UNARY` | `COMMAND_APPLY_INVERT` |

读这张表得到三条规律：

1. **4 种逐像素运算**（加、阈值、取反、乘）统一进入 `STATE_PROC_UNARY`，靠 `processing_command` 区分。
2. **3 种双图运算**（加、减、乘）统一进入 `STATE_PROC_BINARY`，靠 `processing_command` 区分。
3. **卷积是特例**：它进入 `STATE_PROC_CONVOLUTION` 后**不设 `processing_command`**，因为卷积有自己专属的一串状态（`CALCULATION`、`WRITEBACK_1/2`），不需要复用分支。

`processing_command` 的「消费端」就在 `STATE_PROC_UNARY` 里，用一连串 `if ... else if` 选分支，[hdl/image_processing.v:L516-L545](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L516-L545)：

```verilog
if(processing_command == COMMAND_APPLY_ADD)begin
   ... // 加法
end else if(processing_command == COMMAND_APPLY_THRESHOLD) begin
   ... // 阈值
end else if(processing_command == COMMAND_APPLY_INVERT) begin
   data_write <= ~data_read;
end else if(processing_command == COMMAND_APPLY_MULT) begin
   ... // 乘法
end
```

`STATE_PROC_BINARY` 的消费端同理，按 `processing_command` 在 `COMMAND_BINARY_ADD / SUB / MULT` 间选分支（[L584-L596](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L584-L596)）。

至此，一条命令的完整接力链就清晰了：**`STATE_WAIT_COMMAND` 派发 → `*_READ_PARAM` 读参数并写 `processing_command` + 启动 `state_processing` → 运算 FSM 读 `processing_command` 选算法分支 → 完成后回 `STATE_IDLE`**。

#### 4.4.4 代码实践

**实践目标**：跟踪 `COMMAND_APPLY_ADD` 从派发到运算 FSM 接管的完整一拍一拍路径。

**操作步骤**：

1. 从 [hdl/image_processing.v:L245-L248](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L245-L248) 出发：`COMMAND_APPLY_ADD` 把 `state` 设为 `STATE_APPLY_ADD_READ_PARAM`、`counter_read` 设为 2。
2. 跳到 [L355-L372](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L355-L372)：`STATE_APPLY_ADD_READ_PARAM` 在 `counter_read==2` 时读低字节、`==1` 时读高字节、`else`（`==0`）时执行上面的「交接拍」。
3. 再跳到 [L506-L560](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L506-L560)：`STATE_PROC_UNARY` 起始，第一个 `if(processing_command == COMMAND_APPLY_ADD)` 命中，进入加法分支。

**需要观察的现象**：在步骤 2 的「交接拍」里，`processing_command <= COMMAND_APPLY_ADD` 与 `state_processing <= STATE_PROC_UNARY` 是**同一拍**赋值的；下一拍 `STATE_PROC_UNARY` 读到的就是 `COMMAND_APPLY_ADD`。

**预期结果**：你能用一句话描述交接——「`STATE_APPLY_ADD_READ_PARAM` 在读完最后一个参数字节的同一拍，既启动运算 FSM、又递交命令工单、又让主 FSM 回总台」。这正是规格里要求的「`COMMAND_APPLY_ADD` 这类运算命令如何让运算 FSM 接管」的完整答案：**交接动作发生在 `*_READ_PARAM` 状态（而非 `STATE_WAIT_COMMAND` 本身）**，只有零参数的取反例外。

#### 4.4.5 小练习与答案

**练习 1**：如果没有 `processing_command` 寄存器，`STATE_PROC_UNARY` 还能同时服务 4 种运算吗？

**参考答案**：不能（至少不能这么写）。`STATE_PROC_UNARY` 是一条复用的流水线，4 种运算共用「读存储→算→写回」的骨架，区别只在中间那步算什么。`processing_command` 就是用来在运行时告诉它「现在算哪一种」。没有它，就得为每种运算单独写一个状态（状态数翻倍），失去复用的简洁性。

**练习 2**：为什么 `STATE_CONVOLUTION_READ_PARAM` 启动卷积时**没有**写 `processing_command`？

**参考答案**：因为卷积不与别的运算复用状态——它有自己专属的 `STATE_PROC_CONVOLUTION → CALCULATION → WRITEBACK_1/2` 一串状态，没有「同一个状态里按命令选分支」的需要，所以不需要 `processing_command` 来区分。

**练习 3**：`STATE_APPLY_ADD_READ_PARAM` 里为什么除了设 `state_processing` 还要设 `proc_counter_read` 和 `proc_memory_addr_counter`？

**参考答案**：`proc_counter_read` 是运算 FSM 的「还要处理几个像素」倒计数（这里设为 `img_width*img_height`），`proc_memory_addr_counter` 是运算 FSM 的起始扫描地址（这里设为 `buffer_storage_address`，即从 storage 缓冲开始）。运算 FSM 一启动就要用这两个寄存器，所以必须在交接拍一并初始化好。

---

## 5. 综合实践

**任务**：画一张「命令生命周期」状态图，把本讲四块知识串起来。

请针对**两条命令**各画一条轨迹：

1. **`COMMAND_APPLY_INVERT`（零参数运算）**：从主机发出操作码开始，画出 `state` 与 `state_processing` 两个寄存器在每个关键时钟沿的取值变化，直到运算 FSM 回到 `STATE_IDLE`。重点标出「当场交接」那一拍。
2. **`COMMAND_APPLY_ADD`（带参数运算）**：同样画出两个寄存器的变化轨迹，但要多出「`STATE_APPLY_ADD_READ_PARAM` 读 3 个参数字节」这几拍，并标出「交接拍」上同时赋值的 5 个寄存器（`state`、`state_processing`、`processing_command`、`proc_counter_read`、`proc_memory_addr_counter`）。

**要求**：

- 用两张并排的表格或两张时序图，明确区分 `state`（主 FSM）和 `state_processing`（运算 FSM）两条轨迹。
- 在两条轨迹之间，标出「主 FSM 已经回到 `STATE_WAIT_COMMAND`，但运算 FSM 还在跑」的**重叠区**——这正是双 FSM 架构的核心。
- 对照源码核对每一拍：派发分支见 [L221-L286](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L221-L286)、交接拍见 [L363-L370](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L363-L370)、运算 FSM 见 [L506-L560](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L506-L560)。

**预期产出**：一张能让人一眼看出「主 FSM 接完命令就回总台，运算 FSM 在后台独立跑完」的图。如果你能用它向别人解释「为什么卷积期间还能查状态」，说明你已经掌握了本讲。

> 「待本地验证」：若你有仿真环境（u1-l3），可在 `image_processing.v` 的 `STATE_WAIT_COMMAND`、`STATE_APPLY_ADD_READ_PARAM`、`STATE_PROC_UNARY` 各加一行 `$display` 打印 `state`/`state_processing`/`processing_command`，跑一次加法测试，把仿真日志与你画的图对照。

## 6. 本讲小结

- `image_processing.v` 用**两条并行 FSM**：`state`（命令解析）与 `state_processing`（运算执行），放在同一个 `always @(posedge clk)` 块的两个独立 `case` 里，共享时钟、互不阻塞。
- 拆两条 FSM 的根本目的是**让命令通道永远在线**：运算 FSM 跑长卷积时，主 FSM 仍能用 `STATE_GET_STATUS` 回答 busy 位（取自 `state_processing != STATE_IDLE`）。
- `COMMAND_*` 操作码（数值 0–12）与软件侧 `Commands` 枚举逐字对应，是四方契约；`STATE_*` 是纯硬件内部状态名。
- `STATE_WAIT_COMMAND` 是派发总台：按 `comm_cmd` 把流程导向读参数状态、收发图状态，或当场完成（取反当场启动运算、切换缓冲当场互换地址）。
- `processing_command` 寄存器是「运算工单」：交接拍一次性写入它 + `state_processing`，让运算 FSM 在复用的 `STATE_PROC_UNARY` / `STATE_PROC_BINARY` 里按命令选算法分支；卷积有专属状态链，不设它。
- 带参数运算命令的交接发生在 `*_READ_PARAM` 状态（读完最后一字节那一拍），而**不是**在 `STATE_WAIT_COMMAND` 本身——只有零参数的取反例外。

## 7. 下一步学习建议

- **下一讲 u3-l4（图像发送/接收与参数读取状态）** 会钻进 `counter_read` 的字节级时序，解释 `STATE_SEND_IMG`、`STATE_READ_IMG`、`STATE_GET_STATUS` 和各 `*_READ_PARAM` 状态如何逐字节读参数、逐字读写图像——把本讲「预装 counter_read」的伏笔展开。
- **u4 单元** 会进入 `STATE_PROC_UNARY` / `STATE_PROC_BINARY` 的算法细节，看 `processing_command` 选中的分支到底怎么算像素、怎么写回存储。
- **u5 单元** 攻克卷积专属状态链 `STATE_PROC_CONVOLUTION → CALCULATION → WRITEBACK`，那是全项目最复杂的部分。
- 建议阅读：先重读 [hdl/image_processing.v:L218-L286](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L218-L286) 把派发表内化，再带着本讲建立的「双 FSM + 工单交接」框架去读 u3-l4，会非常顺畅。
