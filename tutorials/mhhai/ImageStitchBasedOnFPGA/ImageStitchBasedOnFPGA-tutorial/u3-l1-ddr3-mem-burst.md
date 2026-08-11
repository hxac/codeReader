# DDR3突发传输控制器 mem_burst

## 1. 本讲目标

学完本讲，你应当能够：

- 说清为什么 FPGA 访问 DDR3 需要在 MIG IP 外面再包一层「突发控制器」。
- 读懂 Xilinx MIG 7 系列「应用层接口」（`app_*` 系列信号）的含义与握手时序。
- 逐状态追踪 `mem_burst` 的突发读、突发写状态机，解释 `app_rdy`、`app_wdf_rdy`、`app_rd_data_valid` 如何驱动状态切换。
- 解释地址递增规则（每条命令地址 `+8` = 一个 64bit 字）和突发长度的计数方式。
- 独立画出读突发状态转移图，并据图解释时序。

## 2. 前置知识

本讲承接 [u1-l2 仓库导航](u1-l2-repo-navigation.md) 与 [u1-l3 UART 状态机热身](u1-l3-uart-fsm.md)，复用其中的「三段式状态机」「valid/ready 握手」「复位与寄存器」等概念。下面先用大白话补几个本讲要用到的新术语。

- **DDR3**：一种大容量、高带宽的外部 SDRAM。FPGA 片内没有这么大、这么快的存储，所以图像帧缓存要放到外挂的 DDR3 里。但 DDR3 的物理协议很复杂（行/列地址、定时刷新、预充电、突发长度 BL8……），直接写 Verilog 操作它非常困难。
- **MIG（Memory Interface Generator）**：Xilinx 提供的免费 IP 核，把 DDR3 复杂的物理协议封装起来，对外暴露一组相对简单的「应用层接口」（application interface，即 `app_*` 信号）。可以把 MIG 看作一个「DDR3 翻译官」：你只管发命令，它替你把时序、刷新、突发都搞定。
- **突发（burst）**：连续读/写一串地址相邻的数据。DDR3 天生喜欢突发（激活一行后连续读写效率最高）。本讲的「突发」指：用户给一个起始地址 + 一个长度，控制器连续搬 N 个字。
- **app 命令**：MIG 应用层的一次操作请求，用 `app_cmd` 区分读/写——`3'b001` 为读，`3'b000` 为写。
- **valid/ready 握手**：发送方拉高 valid 表示「数据/请求有效」，接收方拉高 ready 表示「愿意接收」，两者同时为高才算完成一次传输。UART 一讲里已经见过类似思想。

一个关键直觉：**MIG 的 `app_*` 接口仍然偏底层**——一次只接一条命令、要自己管 `app_en`、自己看 `app_rdy`、自己递增地址、自己数数据个数、写操作还要单独喂写 FIFO。让图像处理模块直接操作它会很繁琐。`mem_burst` 的存在，就是在 MIG 之上再包一层「突发级」接口：用户只要给「起始地址 + 长度 + 读/写请求」，`mem_burst` 内部自动把 N 个字按节拍喂给 MIG。这就是它的价值。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [DDR3控制/mem_burst.v](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/DDR3%E6%8E%A7%E5%88%B6/mem_burst.v) | 本讲主角。突发读写控制器：把用户级「起始地址 + 长度」请求翻译成 MIG `app_*` 接口的逐拍命令，并完成地址递增与数据计数。 |
| [DDR3控制/mem_test.v](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/DDR3%E6%8E%A7%E5%88%B6/mem_test.v) | `mem_burst` 的「自检驱动器」。先写一组递增模式数据进 DDR3，再读回比对，用 `error` 信号报告对错。本讲用它佐证写数据握手时序（它是 `mem_burst` 的典型用户）。 |

> 说明：本仓库不包含 MIG IP（`mig_7series_0`）本身，也不包含顶层连线，`mem_burst` 只能「逐模块阅读」，不能直接综合（见 u1-l2）。

## 4. 核心概念与源码讲解

### 4.1 mem_burst 的角色：用户接口与 MIG 接口的桥梁

#### 4.1.1 概念说明

`mem_burst` 是一个「翻译/适配层」，它有两侧端口：

- **用户侧**：简洁的「突发级」接口——`rd_burst_req`/`wr_burst_req`（请求）、`rd_burst_len`/`wr_burst_len`（长度）、`rd_burst_addr`/`wr_burst_addr`（起始地址）、`rd_burst_data`/`wr_burst_data`（数据流）、各种 `*_finish`（完成）。
- **MIG 侧**：MIG 的应用层 `app_*` 信号——`app_cmd`/`app_addr`/`app_en`（命令）、`app_wdf_*`（写数据 FIFO）、`app_rd_data*`（读数据）、`app_rdy`/`app_wdf_rdy`（就绪）。

用户的视角是「从地址 A 连续读 N 个字」；MIG 的视角是「每次给我一条命令（cmd+addr+en），我返回/接收一个字」。`mem_burst` 把前者翻译成后者。

#### 4.1.2 核心流程

```
用户侧请求                         MIG 侧动作
─────────────                     ─────────────
rd_burst_req=1                    → 每拍发一条 READ 命令
rd_burst_addr=起始(字地址)        → app_addr = 起始×8（字节地址）
rd_burst_len=N                    → 连续发 N 条命令，地址每条 +8
                                  → 每条命令对应一个 64bit 字
← rd_burst_data / rd_burst_data_valid（MIG 读数据直通给用户）
← rd_burst_finish（N 个字收齐）

wr_burst_req=1                    → 每拍发一条 WRITE 命令，同时喂写数据
wr_burst_addr/wr_burst_len 同理   → app_wdf_data/wren/end 随命令节拍
← wr_burst_data_req（向用户讨下一个写数据）
← wr_burst_finish（N 个字写完）
```

#### 4.1.3 源码精读

模块声明与参数化 [DDR3控制/mem_burst.v:3-7](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/DDR3%E6%8E%A7%E5%88%B6/mem_burst.v#L3-L7)：用 `parameter` 把数据位宽 `MEM_DATA_BITS=64`、地址位宽 `ADDR_BITS=24` 做成可覆盖参数（源码注释提到顶层会把 `ADDR_BITS` 改成 29）。

用户侧端口 [DDR3控制/mem_burst.v:8-23](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/DDR3%E6%8E%A7%E5%88%B6/mem_burst.v#L8-L23)：就是「突发级」接口——请求、长度、起始地址、数据流、完成信号。

MIG 应用层端口 [DDR3控制/mem_burst.v:25-39](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/DDR3%E6%8E%A7%E5%88%B6/mem_burst.v#L25-L39)：`output` 的 `app_addr`/`app_cmd`/`app_en`/`app_wdf_*` 是控制器发给 MIG 的命令与写数据；`input` 的 `app_rd_data`/`app_rd_data_valid`/`app_rdy`/`app_wdf_rdy` 是 MIG 返回的读数据与就绪信号；`init_calib_complete` 表示 DDR3 校准完成，控制器必须等它为 1 才能工作。

[DDR3控制/mem_burst.v:42](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/DDR3%E6%8E%A7%E5%88%B6/mem_burst.v#L42) 把 `app_wdf_mask`（写数据字节掩码）固定为全 0，表示「每个字节都写、不屏蔽」。

#### 4.1.4 代码实践

**实践目标**：把 `mem_burst` 的端口分成「用户侧 / MIG 侧」两类，建立「翻译官」的直觉。

**操作步骤**：
1. 打开 [DDR3控制/mem_burst.v:8-39](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/DDR3%E6%8E%A7%E5%88%B6/mem_burst.v#L8-L39)。
2. 列两张表：一张用户侧端口（`rd_burst_*`/`wr_burst_*`/`*_finish`），一张 MIG 侧端口（`app_*`）。
3. 对每个 `app_*` 端口，写出它的方向（input/output）和一句话作用。

**需要观察的现象**：左侧（用户）是「批量、突发级」语义，右侧（MIG）是「逐拍、单字命令」语义。

**预期结果**：清楚看到 `mem_burst` 就是夹在两种语义中间的转换层。

#### 4.1.5 小练习与答案

- **练习 1**：为什么图像处理模块不直接连 MIG 的 `app_*`，而要套一层 `mem_burst`？
  - **答案**：`app_*` 接口是「一次一条命令、要自己递增地址、自己数数据个数、自己管写 FIFO 握手」，对图像流水线太琐碎。`mem_burst` 把这些封装成「给地址 + 长度就批量搬数据」的突发接口，让上层只关心数据流。
- **练习 2**：`init_calib_complete` 不为 1 时，控制器内部状态机会怎样？
  - **答案**：状态机被 `if(init_calib_complete === 1'b1)` 门控（见 4.3），校准未完成时不进入 `case`，状态保持 IDLE，不向 MIG 发任何命令。

### 4.2 MIG 应用层接口 app_* 信号详解

#### 4.2.1 概念说明

要读懂状态机，必须先认清 `app_*` 这组信号各自的意思和握手规则。按功能分成三组：

**命令组（发命令给 MIG）**
- `app_cmd[2:0]`：命令码。`3'b001` = 读，`3'b000` = 写。
- `app_addr`：字节地址（注意是字节地址，不是字地址）。
- `app_en`：命令有效。当 `app_en=1` 且 `app_rdy=1` 同一拍，MIG 才接受这条命令。
- `app_rdy`：MIG 命令侧「就绪」，高表示这拍能接受命令。

**写数据组（写操作时把数据喂进 MIG 的写 FIFO）**
- `app_wdf_data`：写数据。
- `app_wdf_wren`：写数据有效（写 FIFO 写使能）。
- `app_wdf_end`：当前写数据是本次突发的最后一个字。
- `app_wdf_rdy`：写 FIFO「就绪」，高表示这拍能收数据。
- 规则：`app_wdf_wren` 与 `app_wdf_rdy` 同时为高，才算把一个字写进 FIFO。

**读数据组（读操作时 MIG 把数据返回）**
- `app_rd_data`：读数据。
- `app_rd_data_valid`：读数据有效。每出现一拍高电平，对应一个有效读数据字。

> 关于「一条命令对应几个字」：在本控制器的设计模型里，**一条被接受的命令（`app_en & app_rdy`）对应一个 64bit 字**，控制器内部「命令计数」与「数据计数」严格 1:1。MIG 的 User Interface 通常会把物理层 BL8 吸收为字级接口；但「每条 app 命令到底返回几个 `app_rd_data_valid`」最终取决于 MIG 配置（数据位宽、nCK_per_clk 等），本仓库未收录 IP，故 IP 侧的精确映射以待确认方式看待，本讲一律以控制器代码的 1:1 模型为准来讲解。

#### 4.2.2 核心流程

命令侧和写数据侧各有自己的 valid/ready 握手，彼此不完全同步：

```
读：  app_en ──┐                          ┌── app_rd_data_valid ── app_rd_data
      app_rdy ─┘ (同时高 = 命令被接受)      (若干拍后数据回来，逐字 valid)

写：  app_en/app_cmd/app_addr ──┐  (命令侧握手)
      app_rdy            ───────┘
      app_wdf_data/app_wdf_wren ──┐  (写数据侧握手，可与命令侧错拍)
      app_wdf_rdy          ───────┘
      app_wdf_end：标记最后一个写数据
```

#### 4.2.3 源码精读

控制器把内部寄存器 `app_cmd_r`/`app_addr_r`/`app_en_r` 直接连到端口 [DDR3控制/mem_burst.v:63-65](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/DDR3%E6%8E%A7%E5%88%B6/mem_burst.v#L63-L65)。

写数据直通：`app_wdf_data = wr_burst_data`（用户的写数据直接接到 MIG）[DDR3控制/mem_burst.v:67](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/DDR3%E6%8E%A7%E5%88%B6/mem_burst.v#L67)；而 `app_wdf_wren = app_wdf_wren_r & app_wdf_rdy`，把内部写使能与 MIG 就绪「与」起来 [DDR3控制/mem_burst.v:68](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/DDR3%E6%8E%A7%E5%88%B6/mem_burst.v#L68)——这正是「两侧同时高才写」的体现。

读数据直通：`rd_burst_data = app_rd_data`、`rd_burst_data_valid = app_rd_data_valid`，MIG 的读数据原样透传给用户 [DDR3控制/mem_burst.v:73-74](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/DDR3%E6%8E%A7%E5%88%B6/mem_burst.v#L73-L74)。

#### 4.2.4 代码实践

**实践目标**：在源码里找到每条握手规则对应的代码行。

**操作步骤**：在 `mem_burst.v` 中定位：
1. 命令被接受的条件：`app_en_r` 何时为 1、`app_rdy` 在状态机里如何被判断（见 4.3）。
2. 写数据被写入的条件：第 68 行 `app_wdf_wren = app_wdf_wren_r & app_wdf_rdy`。
3. 读数据有效的来源：第 74 行 `rd_burst_data_valid = app_rd_data_valid`。

**需要观察的现象**：确认「命令侧握手」与「写数据侧握手」是两套独立握手。

**预期结果**：能讲清「命令侧」与「写数据侧」各自独立，这是读懂写状态机的前提。

#### 4.2.5 小练习与答案

- **练习 1**：`app_wdf_mask` 为什么固定全 0？
  - **答案**：掩码位为 0 表示「对应字节正常写入」。全 0 表示 64bit 的 8 个字节全部写入、不屏蔽任何一个。
- **练习 2**：为什么 `app_wdf_wren` 要和 `app_wdf_rdy` 相与，而 `app_en` 没有在端口上和 `app_rdy` 相与？
  - **答案**：`app_en` 是控制器主动驱动的命令有效信号，是否被接受由 MIG 的 `app_rdy` 决定（在状态机里用 `if(app_rdy)` 判断后再递增地址/计数）；而写数据要真正进入 MIG 的写 FIFO，必须在写使能与就绪同时有效的那一拍，所以端口层面直接相与，避免 MIG 没就绪时误写。

### 4.3 突发读状态机

#### 4.3.1 概念说明

读突发用一个状态机完成「发 N 条读命令 + 收 N 个读数据字」。难点在于：**命令发出和数据返回是流水线的两件事**——命令发完时数据可能还没回完。所以状态机分别用两个计数器（命令计数 `rd_addr_cnt`、数据计数 `rd_data_cnt`）各数到 `rd_burst_len`，并设一个 WAIT 状态专门等数据回流。

状态定义见 [DDR3控制/mem_burst.v:44-51](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/DDR3%E6%8E%A7%E5%88%B6/mem_burst.v#L44-L51)，与读相关的链路是：`IDLE → MEM_READ → MEM_READ_WAIT → READ_END → IDLE`。

#### 4.3.2 核心流程

```
IDLE：收到 rd_burst_req
      → 设 app_cmd=3'b001(读)，app_addr=起始字地址左移3位(字节地址)，app_en=1
      → 进入 MEM_READ

MEM_READ（命令侧 + 数据侧并行判断）：
  命令侧：每拍 if(app_rdy) → app_addr += 8（下一个字），rd_addr_cnt++
          当 rd_addr_cnt == rd_burst_len-1 → 命令发完：app_en=0，进入 MEM_READ_WAIT
  数据侧：每拍 if(app_rd_data_valid) → rd_data_cnt++
          当 rd_data_cnt == rd_burst_len-1 → 数据收齐：进入 READ_END

MEM_READ_WAIT（命令已发完，只等剩余数据）：
  每拍 if(app_rd_data_valid) → rd_data_cnt++
  当 rd_data_cnt == rd_burst_len-1 → 进入 READ_END

READ_END：rd_burst_finish=1（本拍），下一拍回 IDLE
```

> 注意：MEM_READ 状态里命令侧和数据侧是**并行**判断的——数据可能在命令还没发完时就陆续回来，所以两边各自独立计数，谁先数到 `rd_burst_len-1` 谁先触发对应转移。

#### 4.3.3 源码精读

IDLE 收到读请求，装载命令并进入 MEM_READ [DDR3控制/mem_burst.v:107-113](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/DDR3%E6%8E%A7%E5%88%B6/mem_burst.v#L107-L113)。其中关键一句是

```verilog
app_addr_r <= {rd_burst_addr, 3'd0};  // 字地址左移3位 = 字节地址
app_cmd_r  <= 3'b001;                  // 读命令
```

把用户给的「字地址」左移 3 位（×8）变成字节地址；`app_cmd_r` 设为读。

MEM_READ 的命令侧（地址递增 + 计数 + 发完关 `app_en`）[DDR3控制/mem_burst.v:127-138](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/DDR3%E6%8E%A7%E5%88%B6/mem_burst.v#L127-L138)：每拍 `app_rdy` 有效，地址加 8；数到 `rd_burst_len-1` 说明 N 条命令发完，关掉 `app_en` 并切到 MEM_READ_WAIT。

MEM_READ 的数据侧（数返回的数据字）[DDR3控制/mem_burst.v:140-151](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/DDR3%E6%8E%A7%E5%88%B6/mem_burst.v#L140-L151)：每拍 `app_rd_data_valid` 有效就计数；数到 `rd_burst_len-1` 说明 N 个字收齐，进 READ_END。

MEM_READ_WAIT（只跑数据侧，把剩余数据数完）[DDR3控制/mem_burst.v:153-166](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/DDR3%E6%8E%A7%E5%88%B6/mem_burst.v#L153-L166)。

READ_END 一拍即回 IDLE [DDR3控制/mem_burst.v:204-205](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/DDR3%E6%8E%A7%E5%88%B6/mem_burst.v#L204-L205)；完成脉冲 `rd_burst_finish = (state==READ_END)` 在第 69 行生成。

#### 4.3.4 代码实践 ⭐（本讲主实践）

**实践目标**：画出读突发的状态转移图，标注 `app_rdy`、`app_rd_data_valid` 与状态切换的对应关系。

**操作步骤**：
1. 准备纸笔或画图工具。
2. 画 4 个状态圆圈：IDLE、MEM_READ、MEM_READ_WAIT、READ_END。
3. 标转移条件：
   - IDLE → MEM_READ：`rd_burst_req`
   - MEM_READ 自环：`app_rdy`（命令继续发）、`app_rd_data_valid`（数据继续来）
   - MEM_READ → MEM_READ_WAIT：`rd_addr_cnt==rd_burst_len-1`（命令发完，受 `app_rdy` 门控）
   - MEM_READ → READ_END：`rd_data_cnt==rd_burst_len-1`（数据先于命令结束就收齐的快路径）
   - MEM_READ_WAIT → READ_END：`rd_data_cnt==rd_burst_len-1`（受 `app_rd_data_valid` 门控）
   - READ_END → IDLE：无条件（一拍）
4. 在每条边上用两种颜色/标记区分「命令侧条件（`app_rdy`）」和「数据侧条件（`app_rd_data_valid`）」。

**需要观察的现象**：从 MEM_READ 出发，命令结束与数据结束条件可以「同时」被判断，谁先到 `rd_burst_len-1` 谁先触发对应转移；MEM_READ_WAIT 只在命令比数据先发完时才会被进入。

**预期结果**：一张清晰的双计数器并行状态图，能解释「为什么需要 MEM_READ_WAIT 这个等待态」。

> 待本地验证：若在 Vivado 里把 `mem_burst` 接到真实 MIG 上抓波形（用 ILA），应能看到 `app_en & app_rdy` 的拍数 = `app_rd_data_valid` 的拍数 = `rd_burst_len`，三者一一对应。

#### 4.3.5 小练习与答案

- **练习 1**：为什么需要 MEM_READ_WAIT，不能在 MEM_READ 里一口气等数据收齐？
  - **答案**：命令侧可能在数据侧之前就发完（命令发完后 `app_en` 要置 0），此时状态需要离开「边发命令边收数据」的 MEM_READ，进入「只收数据」的 MEM_READ_WAIT，避免继续发多余命令。两个计数器分离，才能正确表达命令与数据的流水关系。
- **练习 2**：`rd_burst_len` 设为 128，起始字地址为 0，第 10 条命令的 `app_addr` 是多少？
  - **答案**：起始字节地址 = 0×8 = 0；每条命令 `+8`，第 10 条（`rd_addr_cnt==9`）地址 = 0 + 9×8 = 72（字节）。

### 4.4 突发写状态机与写数据握手

#### 4.4.1 概念说明

写突发比读更绕，因为除了「发 N 条写命令」，还要「把 N 个写数据按节拍喂进 MIG 的写 FIFO」，并且命令侧（`app_rdy`）与写数据侧（`app_wdf_rdy`）是两套握手。控制器通过 `wr_burst_data_req` 信号向用户「讨」下一个写数据，用户在每个 req 拍把数据放到 `wr_burst_data`。

状态定义见 [DDR3控制/mem_burst.v:44-51](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/DDR3%E6%8E%A7%E5%88%B6/mem_burst.v#L44-L51)，与写相关的链路是：`IDLE → MEM_WRITE → MEM_WRITE_WAIT → WRITE_END → IDLE`。

> 题外但重要：`MEM_WRITE_FIRST_READ`（=3'd7）虽然在 localparam 里定义了、`case` 里也有它的处理分支（第 168–173 行），但全状态机没有任何状态的下一拍会跳到它——它是一段「死状态」（dead state），阅读时可忽略，但要知道它存在，避免被误导。

#### 4.4.2 核心流程

```
IDLE：收到 wr_burst_req
      → app_cmd=3'b000(写)，app_addr=起始字节地址，app_en=1
      → app_wdf_end=1（持续高，直到最后一个字才拉低）
      → 进入 MEM_WRITE

MEM_WRITE（命令侧 + 写数据侧并行）：
  命令侧：每拍 if(app_rdy) → app_addr += 8，wr_addr_cnt++
          当 wr_addr_cnt==wr_burst_len-1 → 命令发完：app_wdf_end=0, app_en=0
  写数据侧：wr_burst_data_req = (state==MEM_WRITE) & app_wdf_rdy
            每拍 req 有效 → 向用户讨一个字，wr_data_cnt++
            当 wr_data_cnt==wr_burst_len-1 → 写数据给完：进入 MEM_WRITE_WAIT

MEM_WRITE_WAIT（命令/数据收尾，等最后一次握手）：
  若命令还没发完则继续递增地址；都完成后 → WRITE_END

WRITE_END：wr_burst_finish=1（本拍），下一拍回 IDLE
```

写数据的节拍链（关键）：

```
wr_burst_data_req (= state==MEM_WRITE & app_wdf_rdy)   ← 向用户讨数据
        │ （用户在 req 拍把数据放到 wr_burst_data）
        ▼
app_wdf_wren_r <= wr_burst_data_req   （下一拍寄存，见 78-86 行）
        ▼
app_wdf_wren = app_wdf_wren_r & app_wdf_rdy  （与 MIG 就绪相与，真正写入 FIFO）
```

#### 4.4.3 源码精读

IDLE 收到写请求，装载写命令、置 `app_wdf_end=1`、清计数 [DDR3控制/mem_burst.v:114-123](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/DDR3%E6%8E%A7%E5%88%B6/mem_burst.v#L114-L123)。

向用户「讨」写数据的请求信号 [DDR3控制/mem_burst.v:76](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/DDR3%E6%8E%A7%E5%88%B6/mem_burst.v#L76)：

```verilog
assign wr_burst_data_req = (state == MEM_WRITE) & app_wdf_rdy;
```

只有在写状态且 MIG 写 FIFO 就绪时才讨数据，避免溢出。

写使能的寄存与「相与」[DDR3控制/mem_burst.v:78-86](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/DDR3%E6%8E%A7%E5%88%B6/mem_burst.v#L78-L86)：`app_wdf_wren_r` 跟随 `wr_burst_data_req`（受 `app_wdf_rdy` 门控），再在第 68 行与 `app_wdf_rdy` 相与形成最终 `app_wdf_wren`。

MEM_WRITE 的命令侧（地址 `+8`、计数、发完关 `en`/`end`）[DDR3控制/mem_burst.v:176-188](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/DDR3%E6%8E%A7%E5%88%B6/mem_burst.v#L176-L188)；写数据侧（req 有效就计数、给完进 WAIT）[DDR3控制/mem_burst.v:190-201](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/DDR3%E6%8E%A7%E5%88%B6/mem_burst.v#L190-L201)。

MEM_WRITE_WAIT 收尾 [DDR3控制/mem_burst.v:206-225](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/DDR3%E6%8E%A7%E5%88%B6/mem_burst.v#L206-L225)：把可能未发完的命令继续发完，并在最后一次写 FIFO 握手完成后跳 WRITE_END。

WRITE_END 一拍即回 IDLE [DDR3控制/mem_burst.v:227-228](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/DDR3%E6%8E%A7%E5%88%B6/mem_burst.v#L227-L228)；`wr_burst_finish` 在第 70 行生成。

**佐证：用户侧如何响应 req**。看 `mem_test` 里 [DDR3控制/mem_test.v:47-57](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/DDR3%E6%8E%A7%E5%88%B6/mem_test.v#L47-L57)：每当 `wr_burst_data_req` 有效，就把模式数据 `{(MEM_DATA_BITS/8){wr_cnt}}`（每个字节都填 `wr_cnt`）放到 `wr_burst_data`，`wr_cnt` 自增。这就是 `mem_burst` 期望的「用户在 req 拍提供数据」的典型用法。

#### 4.4.4 代码实践

**实践目标**：用 `mem_test` 当「用户」，理清写数据握手的节拍关系。

**操作步骤**：
1. 读 [DDR3控制/mem_test.v:40-57](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/DDR3%E6%8E%A7%E5%88%B6/mem_test.v#L40-L57)，看 `wr_burst_data_reg` 如何随 `wr_burst_data_req` 更新。
2. 在纸上画时序：`state==MEM_WRITE`、`app_wdf_rdy`、`wr_burst_data_req`、`wr_burst_data`、`app_wdf_wren_r`、`app_wdf_wren` 六条线，标出「req → 下一拍 `wren_r` → 相与 `wren`」的延迟关系。
3. 解释：为什么用户在 req 拍提供的数据，正好能在下一拍被 `app_wdf_wren` 写进 FIFO。

**需要观察的现象**：`wr_burst_data_req` 与 `app_wdf_wren` 之间相差一拍（寄存器延迟），数据流恰好对齐。

**预期结果**：能讲清「req 是输入握手、wren 是输出握手、中间隔一拍寄存器」的时序链。

> 待本地验证：上板抓波形确认 `wr_burst_data_req` 的脉冲数 = `app_wdf_wren` 的脉冲数 = `wr_burst_len`。

#### 4.4.5 小练习与答案

- **练习 1**：`app_wdf_end` 在整个写突发过程中是什么电平？什么时候变化？
  - **答案**：进入 MEM_WRITE 时被置 1（第 121 行），并在写突发期间保持 1；只有当命令发到最后一个字（`wr_addr_cnt==wr_burst_len-1`）时才拉 0（第 181、213 行），告诉 MIG「这是最后一个写数据」。
- **练习 2**：`MEM_WRITE_FIRST_READ` 状态在当前代码里会被进入吗？
  - **答案**：不会。它定义在 localparam 里、`case` 里也有处理分支（第 168–173 行），但没有任何状态的下一拍指向它，属于「死状态」（dead state），阅读时可忽略。

## 5. 综合实践

把读、写两侧串起来，结合 `mem_test` 完成一次完整的「写后读回」验证理解：

1. 读 [DDR3控制/mem_test.v:78-125](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/DDR3%E6%8E%A7%E5%88%B6/mem_test.v#L78-L125) 的状态机：IDLE → MEM_WRITE（发写请求 `len=128`）→ 收到 `wr_burst_finish` → MEM_READ（发读请求，且 `rd_burst_addr <= wr_burst_addr`）→ 收到 `rd_burst_finish` → 回 MEM_WRITE（换下一段地址）。
2. 回答：为什么 `mem_test` 要让 `rd_burst_addr <= wr_burst_addr`（读地址 = 刚写的地址）？
3. 看 [DDR3控制/mem_test.v:34-38](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/DDR3%E6%8E%A7%E5%88%B6/mem_test.v#L34-L38) 的 error 逻辑：在 MEM_READ 且 `rd_burst_data_valid` 时，比较 `rd_burst_data` 是否等于 `{(MEM_DATA_BITS/8){rd_cnt}}`，不等就置 `error`。
4. 用一段话写出：一次「写 128 字 → 读回 128 字 → 比对」的完整数据通路，标出每个环节是 `mem_burst` 的哪个状态、哪条握手线在工作。

**预期产出**：一段把「`mem_test`（用户）↔ `mem_burst`（翻译）↔ MIG（物理）」三层串起来的文字说明，能解释 `error` 保持 0 意味着控制器工作正常。（参考答案：第 2 问——读回必须用刚写入的同一首地址，否则读到的是别的位置的数据，比对就无意义；第 4 问的要点是 MEM_WRITE 里 `wr_burst_data_req`↔`app_wdf_wren` 喂数据、MEM_READ 里 `app_rd_data_valid`↔`rd_burst_data` 收数据，`error` 为 0 表示每一拍读回的模式字节都与写入一致。）

## 6. 本讲小结

- `mem_burst` 是夹在用户与 MIG 之间的「突发级翻译层」：把「起始地址 + 长度」翻译成 MIG 的逐拍单字命令。
- MIG 应用层接口分三组：命令组（`app_cmd`/`app_addr`/`app_en`/`app_rdy`）、写数据组（`app_wdf_data`/`wren`/`end`/`rdy`）、读数据组（`app_rd_data`/`valid`）；命令侧与写数据侧是两套独立握手。
- 读突发用双计数器（命令计数 `rd_addr_cnt` / 数据计数 `rd_data_cnt`）+ MEM_READ_WAIT 等待态，处理「命令发完但数据未回完」的流水关系。
- 写突发多了一条「向用户讨数据」的 `wr_burst_data_req` 链，经一拍寄存器变成 `app_wdf_wren`，与 `app_wdf_rdy` 相与后真正写入 MIG 写 FIFO。
- 地址按「一个 64bit 字 = 8 字节」递增：起始 `{addr,3'd0}` 变字节地址，之后每条命令 `+8`。
- 代码里存在死状态 `MEM_WRITE_FIRST_READ`（定义了但不可达），阅读时注意甄别。

## 7. 下一步学习建议

- **下一篇 u3-l2**：精读 `mem_test.v`，完整理解「写模式数据 → 读回比对 → error 检测」的自检流程，把本讲的握手时序落到一个能跑的验证场景。
- **u3-l3**：回到 README，理解 24bit→64bit 异步 FIFO——它正是把摄像头 24bit 像素流拼成 `mem_burst` 需要的 64bit 字的那一段（实现代码未收录，标注待确认）。
- **横向延伸**：u4 的 DynamicSeam 与 u5-l2 的 IP 集成都会例化 MIG 并用到与本讲相同的 `app_*` 接口，掌握本讲后再读那些模块会轻松很多。
