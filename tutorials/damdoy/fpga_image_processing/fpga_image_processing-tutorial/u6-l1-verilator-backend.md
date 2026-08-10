# Verilator 仿真后端

## 1. 本讲目标

本讲带你走进项目的「仿真后端」——`simulation/` 目录下的 C++ 代码。读完本讲，你应该能够：

- 说清楚 `Operation` 结构和 `FIFO_OP` 队列如何把一次高层调用（如 `send_add`）拆成一条「命令 + 若干数据字节」的脚本。
- 理解 `main_loop_clk()` 如何用「手动翻转 `clk` + 两次 `eval()`」驱动 Verilator 生成的 `Vimage_processing` 模型。
- 看懂 `counter_free` 反压机制模拟了什么物理现象，以及它为什么能逼真地考验硬件握手逻辑。
- 读懂仿真侧如何用一块 `uint16_t` 数组 `memory[]` 模拟单端口 RAM 的读写时序（含 1 拍读延迟）。

本讲是 u6 单元「仿真与硬件两条后端」的第一篇，承接 u2-l1（抽象接口契约）和 u3-l1（核心模块端口），重点回答一个问题：**核心 HDL 模块不绑通信方式，那仿真后端究竟用什么「喂」它？**

## 2. 前置知识

本讲默认你已经读过：

- **u1-l3 构建并运行：Verilator 仿真模式**：知道 `verilator --exe` 把 `image_processing.v` 翻译成 C++ 类 `Vimage_processing`，编译成可执行文件 `simu`。
- **u2-l1 抽象基类与命令枚举**：知道 `Image_processing` 是纯虚基类，`Commands` 枚举与硬件 `COMMAND_*` 数值一一对应。
- **u3-l1 image_processing.v 的端口与两大接口**：知道模块对外只有「存储器接口」和「通信接口」两扇门，通信侧用 `comm_cmd`/`comm_data_in`/`comm_data_in_valid`（输入握手）和 `comm_data_out`/`comm_data_out_valid`/`comm_data_out_free`（输出反压握手）。

几个本讲会用到的术语，先做通俗解释：

- **Verilator 模型（`Vimage_processing`）**：Verilator 把 Verilog 的 `always @(posedge clk)` 翻译成 C++ 的成员函数。它不是在「模拟电路波形」，而是一个能用 C++ 变量驱动、用 `eval()` 推进一拍的「离散状态机」。
- **手动时钟（manual clocking）**：在真实 FPGA 上 `clk` 由晶振以固定频率翻转；在仿真里没有晶振，由 C++ 代码自己把 `simulator->clk` 在 0 和 1 之间拨动，每拨一次再调用 `eval()`，模型就「走一拍」。
- **反压（back-pressure）**：当下游来不及消费时，上游必须暂停。本模块输出侧用 `comm_data_out_free`（由接收方置位）实现反压——`free=0` 表示「别再发了，我还没消费完」。

## 3. 本讲源码地图

本讲只涉及两个文件，它们共同构成仿真后端的全部实现：

| 文件 | 作用 | 本讲关注点 |
|------|------|-----------|
| [simulation/image_processing_simulation.hpp](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/simulation/image_processing_simulation.hpp) | 声明 `Operation` 结构、`FIFO_OP` 类型别名，以及派生类 `Image_processing_simulation` 的接口与私有成员 | `Operation`/`FIFO_OP` 的定义、`fifo_in`/`fifo_out`/`memory`/`simulator` 四个私有成员 |
| [simulation/image_processing_simulation.cpp](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/simulation/image_processing_simulation.cpp) | 实现全部虚函数与驱动函数 `main_loop_clk()` | 每个高层调用如何「入队」，`main_loop_clk()` 如何「消费」 |

回想 u2-l2 的结论：仿真后端与 iCE40 硬件后端把**同一份高层调用**打包成**逐字节相同**的字节流，差别只在传输外壳。本讲的 `fifo_in` 就是仿真侧的「传输外壳」——一条先进先出的命令队列；而 iCE40 侧的外壳是 SPI 总线（留给 u6-l3/u6-l4）。

## 4. 核心概念与源码讲解

### 4.1 Operation 结构与 FIFO_OP 命令队列

#### 4.1.1 概念说明

核心模块 `image_processing` 只认两样东西：**一个命令字节**（`comm_cmd`）和**若干数据字节**（`comm_data_in`）。它不知道字节从哪儿来。仿真后端要做的，就是把 C++ 侧一次语义化调用（如 `send_add(value, clamp)`）翻译成一串字节，再一个时钟沿喂一个字节进去。

`Operation` 就是这串字节里「一个原子项」的载体。它用一个布尔标志 `is_command` 区分这一项是「命令字节」还是「数据字节」，从而把两种字节塞进同一条队列。`FIFO_OP` 则是 `std::queue<Operation>` 的别名，即一条先进先出的命令队列。

#### 4.1.2 核心流程

任何一个高层调用的套路都一样，分两步：

```text
第一步：入队（先把这次调用对应的字节脚本一次性 push 进 fifo_in）
  ┌─────────────────────────────────────────────┐
  │ push(Operation(true,  命令码, 0))            │ ← 命令字节
  │ push(Operation(false, COMMAND_NONE, 字节))   │ ← 数据字节（若干）
  │ ...                                          │
  └─────────────────────────────────────────────┘

第二步：拨时钟（循环调用 main_loop_clk 若干次，每次消费队列里 1 项）
  每拍：从 fifo_in 弹出 1 个 Operation → 驱动 comm_cmd 或 comm_data_in
```

关键直觉：**整个 `fifo_in` 就像一卷提前录好的「打孔纸带」**，`main_loop_clk` 是读带机的磁头，每拍读一格。主机在调用时先把纸带录好，再让读带机跑起来。

#### 4.1.3 源码精读

`Operation` 结构与 `FIFO_OP` 定义在头文件里：

[simulation/image_processing_simulation.hpp:L7-L19](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/simulation/image_processing_simulation.hpp#L7-L19) —— 定义了 `Operation`（含 `is_command`/`command`/`data` 三字段）和 `FIFO_OP = std::queue<Operation>`。注意数据字节项的 `command` 字段填的是 `COMMAND_NONE`，因为这一项不携带命令，真正有意义的只有 `data`。

以 `send_params` 为例看「入队」是怎么发生的：

[simulation/image_processing_simulation.cpp:L18-L38](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/simulation/image_processing_simulation.cpp#L18-L38) —— 先把 16 位的宽高拆成小端两字节，再依次 `push`：1 个命令（`COMMAND_PARAM`）+ 4 个数据字节（宽低、宽高、高低、高高），最后循环 5 次 `main_loop_clk()` 把这 5 项消费掉。这与 u2-l2 讲的「操作码 + 小端 16 位参数」报文格式完全对应——这里你能亲眼看到报文是如何被「装」出来的。

再看一个最纯粹的例子 `switch_buffers`：

[simulation/image_processing_simulation.cpp:L167-L173](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/simulation/image_processing_simulation.cpp#L167-L173) —— 只入队 1 个命令字节（零参数命令），然后拨 10 拍时钟。它印证了 u3-l3 的结论：`COMMAND_SWITCH_BUFFERS` 是当场完成的零参数命令。

输出方向的队列 `fifo_out`（类型 `std::queue<uint16_t>`）在头文件第 [L52](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/simulation/image_processing_simulation.hpp#L52) 行声明，用来接住模块吐出的字节（状态字节、回读像素），它在哪里被填充见 4.2.3。

#### 4.1.4 代码实践

**实践目标**：亲手验证「一次高层调用 = 一段入队脚本」。

**操作步骤**：

1. 打开 `simulation/image_processing_simulation.cpp`，找到 `send_threshold`（第 L85–L94 行）。
2. 数一下它一共 `push` 了几项、其中几个 `is_command==true`、几个 `false`。
3. 对照 u2-l2 的报文表，确认 `COMMAND_APPLY_THRESHOLD` 的参数字节数与这里入队的数据项数一致。

**需要观察的现象 / 预期结果**：

- 应该是 1 个命令项 + 3 个数据项（`threshold_value`、`replacement_value`、`upper_selection`），共 4 项。
- 这与硬件侧 `STATE_THRESHOLD_READ_PARAM` 读取的参数个数相符——你可以去 [hdl/image_processing.v](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v) 里 `STATE_WAIT_COMMAND` 派发到该状态时预装的 `counter_read` 值核对（u3-l4 讲过 `counter_read` 预装值 = 参数字节数 − 1）。

> 本实践为「源码阅读型」，不需要运行；若要运行可按 u1-l3 的 `build_simulation.sh` 编译后把 `main.cpp` 里激活的测试换成 threshold 类。

#### 4.1.5 小练习与答案

**练习 1**：`send_image`（第 L61–L70 行）入队了多少项？为什么命令项只有 1 个、数据项却有 `image_width*image_height` 个？

**答案**：1 个命令项（`COMMAND_SEND_IMG`）+ `W*H` 个数据项（每个像素 1 字节）。因为图像是逐字节流的（见 u2-l3），每个像素作为一个数据字节入队；而命令只需在最开头宣告一次「接下来是送图」。

**练习 2**：为什么数据项的构造写成 `Operation(false, COMMAND_NONE, 字节)`，第二个参数填 `COMMAND_NONE`？

**答案**：因为这一项是数据而非命令，`is_command=false` 决定了 `main_loop_clk` 会把它送往 `comm_data_in` 而非 `comm_cmd`（见 4.2.3）。`command` 字段在这一项里毫无用处，填 `COMMAND_NONE`（=255）只是占位，避免未初始化。

---

### 4.2 main_loop_clk：手动翻转时钟 + eval 的仿真驱动

#### 4.2.1 概念说明

`main_loop_clk()` 是整个仿真后端的「心脏」。每调用一次，模型就走一个时钟周期。它做了三件事：

1. **翻转时钟**：把 `clk` 从 0 拨到 1，并在两个边沿各调用一次 `eval()`，触发 Verilator 模型里 `always @(posedge clk)` 的寄存器更新。
2. **喂输入**：从 `fifo_in` 弹出 1 个 `Operation`，据此驱动 `comm_cmd` 或 `comm_data_in`（并拉高 `comm_data_in_valid`）。
3. **接输出 / 模拟存储**：检查模块是否要吐字节（接进 `fifo_out`），并按 `rd_en`/`wr_en` 模拟 RAM 读写。

#### 4.2.2 核心流程

一个 `main_loop_clk()` 调用的时序如下（对应函数从上到下的代码顺序）：

```text
1. clk=0; eval()                      ← 先在低电平稳定组合逻辑
2. clk=1; comm_data_in_valid=0        ← 默认撤销输入有效
3. 维护 counter_free → 置 comm_data_out_free        （详见 4.3）
4. 若 fifo_in 非空：弹出 1 项
     - is_command==true  → comm_cmd=op.command;          comm_data_in_valid=1
     - is_command==false → comm_data_in=op.data;         comm_data_in_valid=1
5. 若 comm_data_out_valid==1 → fifo_out.push(comm_data_out); counter_free=3  （输出回收）
6. data_read_valid=0
     - 若 rd_en==1 → data_read=memory[addr/2]; data_read_valid=1   （喂回读数据）
     - 若 wr_en==1 → memory[addr/2]=data_write                     （接收写入）
7. eval()                             ← 上升沿，模型走一拍
```

注意第 4 步：**每拍只弹 1 项**。这就是为什么高层调用在入队后必须循环足够多次 `main_loop_clk()`——队列里有多少项，就至少要拨多少拍。

#### 4.2.3 源码精读

完整的 `main_loop_clk()`：

[simulation/image_processing_simulation.cpp:L223-L266](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/simulation/image_processing_simulation.cpp#L223-L266) —— 仿真驱动核心。逐段对应上面流程图。

时钟翻转与两次 `eval()`：

[simulation/image_processing_simulation.cpp:L224-L227](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/simulation/image_processing_simulation.cpp#L224-L227) —— 先 `clk=0; eval()`（让依赖 `clk` 的组合逻辑先稳定），再 `clk=1`。这是 Verilator 手动时钟的标准范式：两次 `eval()` 之间驱动输入，第二次 `eval()` 在 `clk=1` 时触发上升沿寄存器更新。

从队列弹出一项并分流到 `comm_cmd` / `comm_data_in`：

[simulation/image_processing_simulation.cpp:L235-L246](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/simulation/image_processing_simulation.cpp#L235-L246) —— `is_command` 决定字节去往 `comm_cmd`（命令）还是 `comm_data_in`（数据），两种情况都拉高 `comm_data_in_valid`。这正是 4.1 里「纸带读带机」的磁头。

**`fifo_out` 在哪里被填充**（回答实践任务第三问）：

[simulation/image_processing_simulation.cpp:L248-L252](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/simulation/image_processing_simulation.cpp#L248-L252) —— 当模块把 `comm_data_out_valid` 拉高（它要吐一个字节出来）时，仿真侧就把 `comm_data_out` 的值 `push` 进 `fifo_out`，同时把 `counter_free` 置 3（启动反压，详见 4.3）。后续 `read_status` / `read_image` / `wait_end_busy` 再从 `fifo_out` 里把这些字节取走。所以 `fifo_out` 是「模块输出 → 主机」的回收管道，与 `fifo_in` 方向相反。

#### 4.2.4 代码实践

**实践目标**：理解「每拍只弹 1 项」带来的后果，回答实践任务第二问——为什么 `send_image` 入队后要循环 `image_width*image_height+500` 次 `main_loop_clk`。

**操作步骤 / 推理**：

1. `send_image` 共入队 `1 + W*H` 项（1 命令 + W*H 像素），见 [L62-L65](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/simulation/image_processing_simulation.cpp#L62-L65)。
2. `main_loop_clk` 每拍只弹 1 项（第 L235–L246 行）。
3. 因此排空队列至少需要 `1 + W*H` 拍。
4. 循环写的是 `W*H + 500`（[L67-L69](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/simulation/image_processing_simulation.cpp#L67-L69)），多出来的约 499 拍里 `fifo_in` 已空，`comm_data_in_valid` 保持 0，模块在 `STATE_SEND_IMG` 里不再收到新字节，得以完成最后一两个像素的打包写入并回到 `STATE_WAIT_COMMAND`。

**预期结果 / 结论**：`+500` 是一个宽裕的安全余量，确保即便最后一拍的输入需要再过一两拍才在模块内部生效（寄存器更新有一拍延迟），队列也能被彻底消费、模块也回到空闲态。`W*H+500` 不是精确公式，而是「排空队列的下界 + 余量」。> 待本地验证：可在循环前后各打印 `fifo_in.size()`，确认循环结束时队列为空。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `main_loop_clk` 里要在 `clk=0` 和 `clk=1` 各 `eval()` 一次，而不是只 `clk=1; eval()` 一次？

**答案**：Verilator 模型里可能有依赖 `clk` 的组合逻辑。先在 `clk=0` 时 `eval()` 让这些逻辑稳定到一个一致状态，再在 `clk=1` 时 `eval()` 触发上升沿，才能正确模拟「组合逻辑先就绪、寄存器再采样」的真实时序。这是 Verilator 官方推荐的手动时钟写法。

**练习 2**：`read_status` 在入队 1 个命令后循环了 **100** 次 `main_loop_clk`（[L43-L45](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/simulation/image_processing_simulation.cpp#L43-L45)），但状态回传只有 4 字节，为什么循环次数远大于 4？

**答案**：因为回传受 `comm_data_out_free` 反压影响——每吐 1 字节，仿真侧就把 `counter_free` 置 3，于是接下来 3 拍 `comm_data_out_free=0`，模块必须等待（见 4.3 与硬件 [STATE_GET_STATUS](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L310-L333) 的 `if(comm_data_out_free==1)` 门控）。所以 4 个字节并非连续 4 拍就能吐完，需要留出足够拍数。100 拍是一个宽裕的上界。

---

### 4.3 comm_data_out_free 反压模拟

#### 4.3.1 概念说明

在真实硬件里，模块吐出的字节要经过 SPI 总线（u6-l3）才能到达主机。SPI 总线带宽有限、主机也可能正忙，所以模块**不能假设每拍都能吐一个字节**。模块的设计因此内置了反压握手：只有当 `comm_data_out_free==1`（接收方说「我准备好了」）时，它才拉高 `comm_data_out_valid` 发送一个字节（见 HDL 的 [STATE_GET_STATUS](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L310-L333) 与 [STATE_READ_IMG](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L373-L399) 的门控条件）。

如果仿真里永远把 `comm_data_out_free` 固定成 1，这段握手代码就永远走「快车道」，潜在的反压 bug 会被掩盖。所以仿真侧用 `counter_free` **人为制造周期性的反压**，逼着模块在 `comm_data_out_free=0` 时老老实实等待——从而让仿真覆盖到与真实硬件相同的握手路径。

#### 4.3.2 核心流程

`counter_free` 是一个 `static int`，充当「线路占用剩余拍数」的倒计时：

```text
每拍开头：
  若 counter_free > 0：counter_free--              （占用逐拍释放）
  comm_data_out_free = (counter_free == 0)

若这一拍模块吐了字节（comm_data_out_valid==1）：
  fifo_out.push(comm_data_out)                      （收下字节）
  counter_free = 3                                  （线路重新占满 3 拍）
  comm_data_out_free = (counter_free == 0) = 0      （立即告诉模块：先别发了）
```

于是模块每吐 1 字节，接下来 3 拍都会看到 `comm_data_out_free=0` 而暂停发送，第 4 拍起 `counter_free` 减到 0 才恢复。

#### 4.3.3 源码精读

反压的全部代码就嵌在 `main_loop_clk` 里：

[simulation/image_processing_simulation.cpp:L230-L234](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/simulation/image_processing_simulation.cpp#L230-L234) —— 注释 `//simulates the fact that the comm line can be full` 直白点明了意图。每拍先把 `counter_free` 递减，再把 `comm_data_out_free` 设为 `counter_free==0`。

[simulation/image_processing_simulation.cpp:L248-L252](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/simulation/image_processing_simulation.cpp#L248-L252) —— 一旦模块拉高 `comm_data_out_valid`，立刻把 `counter_free` 置 3，并同步刷新 `comm_data_out_free=0`。这就完成了「发一字节 → 占线 3 拍」的模拟。

把 4.3 和硬件侧对照看，会得到一个清晰的闭环：

- 仿真侧 `comm_data_out_free` 由 `counter_free` 决定。
- 模块侧 `STATE_GET_STATUS` / `STATE_READ_IMG` 用 `if(comm_data_out_free==1)` 门控是否拉高 `comm_data_out_valid`。
- 模块一旦发送，仿真侧又把 `comm_data_out_free` 拉低 3 拍。

这正是真实 SPI 链路上「主机忙→从机等」的缩影。

#### 4.3.4 代码实践

**实践目标**：回答实践任务第一问——`counter_free==3` 模拟了什么物理现象，并定量算出它对吞吐的影响。

**操作步骤**：

1. 阅读 [L248-L252](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/simulation/image_processing_simulation.cpp#L248-L252)，确认每发 1 字节置 `counter_free=3`。
2. 假设模块要连续吐 N 个字节（如 `STATE_GET_STATUS` 的 4 字节、`STATE_READ_IMG` 的 W*H 字节）。
3. 计算在不考虑其它开销时，吐 N 字节需要多少拍。

**需要观察的现象 / 预期结果**：

- `counter_free==3` 模拟「通信线路被占满」——即主机/链路来不及立即取走模块刚吐出的字节，对应硬件里 SPI 从机输出缓冲有限、主机 MPSSE 读取需要时间的现实（u6-l3/u6-l4）。
- 吞吐估算：第 1 字节在第 0 拍发出，随后第 1、2、3 拍 `free=0`，第 4 拍 `counter_free` 减到 0 才能发第 2 字节……所以平均约 **每 4 拍发 1 字节**。N 字节约需 4N 拍（首字节省 3 拍）。这也解释了 4.2.5 练习 2 里 `read_status` 为何循环 100 次而非 4 次。> 待本地验证：可在 `main_loop_clk` 里统计一次 `read_image` 期间 `comm_data_out_valid` 被拉高的总拍数，与 `4*W*H` 比较。

#### 4.3.5 小练习与答案

**练习 1**：如果要把反压「调松」一些（让模块更少等待），应该改 `counter_free = 3` 里的 3 为更大的数还是更小的数？

**答案**：改成**更小的数**（如 1）。`counter_free` 越小，`comm_data_out_free=0` 的拍数越少，模块等待越短，反压越松。改成 0 等于取消反压（`free` 恒为 1，除非正好本拍刚发完）。改成更大的数则反压更紧、吞吐更低。

**练习 2**：为什么反压只作用在输出方向（`comm_data_out_free`），而输入方向（`comm_data_in_valid`）没有对应的反压？

**答案**：因为仿真里输入由 `fifo_in` 驱动，主机（仿真程序）完全掌控发送节奏，且 `fifo_in` 是无限长的 `std::queue`，不会「装不下」。而输出方向模拟的是模块主动吐字节、主机未必能即时消费的真实场景，所以才需要反压。在硬件后端，输入方向的反压由 SPI 从机的 `buffer_full`/`spi_data_in_free` 承担（见 u6-l3），那是物理缓冲有限导致的，仿真里用不上。

---

### 4.4 memory[]：单端口 RAM 读写模拟

#### 4.4.1 概念说明

核心模块不含 RAM——它的存储器接口（`addr`/`wr_en`/`rd_en`/`data_read`/`data_write`/`data_read_valid`）是给「外面的存储器」用的（u3-l1）。在硬件后端，外面是 4 片 SPRAM（u6-l2）；在仿真后端，外面就是一块普通的 C++ 数组 `uint16_t memory[]`。

仿真要用这块数组**忠实再现单端口 RAM 的两个关键时序特性**：

1. **一拍只能做一次访存**：读和写不能在同拍同时进行（这正是 u4-1 讲的「两拍流水」的根本原因）。
2. **读延迟一拍**：`rd_en` 拉高后，数据要在下一拍才出现在 `data_read` 上，并用 `data_read_valid` 标注有效。

#### 4.4.2 核心流程

存储模拟的时序（对应 `main_loop_clk` 末尾几行）：

```text
每拍：
  data_read_valid = 0                         ← 默认无效
  若 rd_en==1：                                ← 模块上一拍发起了读
      data_read      = memory[addr/2]          ← 按字地址取数
      data_read_valid = 1                      ← 这一拍把数据喂回去
  若 wr_en==1：                                ← 模块这一拍要写
      memory[addr/2] = data_write              ← 直接写入对应字
```

注意 `addr` 是**字节地址**，而 `memory` 是 `uint16_t`（每元素 2 字节）数组，所以要 `addr/2` 换算成字下标。这呼应了 u3-l2 讲的「16 位字打包 2 像素、字节粒度地址用 `addr[0]` 区分高低字节」。

#### 4.4.3 源码精读

数组分配在构造函数里：

[simulation/image_processing_simulation.cpp:L7-L11](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/simulation/image_processing_simulation.cpp#L7-L11) —— `new uint16_t[512*128]` 共 65536 个 16 位字 = 131072 字节 = 128 KB。这正好等于 HDL 里 `MEMORY_SIZE = 1024*128`（[hdl/image_processing.v:L81](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L81)）。两者必须一致，否则地址会越界。

读写模拟的核心：

[simulation/image_processing_simulation.cpp:L254-L263](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/simulation/image_processing_simulation.cpp#L254-L263) —— 读：`rd_en==1` 时用 `addr/2` 取字、置 `data_read_valid=1`；写：`wr_en==1` 时把 `data_write` 写回 `addr/2`。

关于「读延迟一拍」：注意这几行处在两次 `eval()` 之间、且 `data_read_valid=0` 是每拍先清零再按 `rd_en` 置位。模块在某个上升沿把 `rd_en<=1`（写入 `rd_en` 寄存器），下一次 `main_loop_clk` 进入时，`simulator->rd_en` 已经是 1，于是本拍把 `data_read`/`data_read_valid` 准备好，再经第二次 `eval()` 让模块采样到 `data_read_valid==1`。所以从模块视角，`rd_en` 到 `data_read_valid` 之间隔了一拍——与硬件后端 `ram_interface` 用 `rd_en_buffer` 流水线对齐 SPRAM 读延迟（u6-l2）是**同一套时序契约**。这正是同一份 HDL 能在仿真和硬件两侧都跑通的关键。

字节地址到字地址的换算也可在 HDL 侧印证：`STATE_SEND_IMG` 里写地址是 `addr <= {memory_addr_counter[31:1], 1'b0}`（[hdl/image_processing.v:L342](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L342)），最低位清零，保证 `addr` 恒为偶数（字对齐），所以 `addr/2` 永远落在合法的整数下标上。

#### 4.4.4 代码实践

**实践目标**：验证「字节地址 / 字下标」换算的正确性，并体会读延迟一拍。

**操作步骤**：

1. 假设模块要从字节地址 `0x04` 读一个 16 位字。`addr/2` 得到字下标 `2`，即 `memory[2]`。
2. 在 [L255-L259](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/simulation/image_processing_simulation.cpp#L255-L259) 处想象时序：第 N 拍模块置 `rd_en=1, addr=0x04`；第 N+1 拍仿真侧看到 `rd_en==1`，置 `data_read=memory[2], data_read_valid=1`；第 N+1 拍的第二次 `eval()` 让模块采样到数据。
3. （可选）在 `main_loop_clk` 的读写分支各加一条日志，标注当前 `addr`、`addr/2`、`rd_en`/`wr_en`、`data_read`/`data_write`，重新编译运行。

**需要观察的现象 / 预期结果**：

- `addr` 永远是偶数（因为 HDL 写地址清最低位、读地址在 `memory_addr_counter[0]==0` 分支发起）。
- `rd_en` 和对应的 `data_read_valid` 出现在**相邻两拍**——日志里能看到「N 拍 rd_en=1 → N+1 拍 data_read_valid=1」的配对。
- 写操作没有「延迟一拍」的握手：`wr_en==1` 当拍直接写入（`memory` 是普通数组，写即生效），模块也无需等待 `data_write_valid`。> 待本地验证：实际运行需先按 u1-l3 编译，再观察日志。

#### 4.4.5 小练习与答案

**练习 1**：为什么仿真用 `addr/2` 而不是 `addr` 作下标？如果把 `memory` 声明成 `uint8_t memory[128*1024]` 并改用 `addr` 下标，逻辑还对吗？

**答案**：因为 `memory` 是 `uint16_t` 数组，每元素 2 字节，而 `addr` 是字节地址，所以除以 2 换算成字下标。如果改成 `uint8_t[128KB]` 并用 `addr` 直接索引，那么每个字节各占一格——但这与模块「16 位字打包 2 像素、一次读写一个字」的设计相悖：`data_read`/`data_write` 都是 16 位，无法用 `uint8_t` 数组一次性承载。所以保持 `uint16_t` + `addr/2` 是为了与 16 位存储字宽对齐。

**练习 2**：仿真侧 `memory[]` 没有实现「读延迟不可读」之类的仲裁，那 u4-1 讲的「单端口 RAM 两拍流水」在仿真里还有意义吗？

**答案**：有意义。两拍流水是**模块内部**（HDL）为适配单端口 RAM「一拍只能一次访存」而做的设计；仿真侧虽然用无限快数组实现了 RAM，但它**忠实保留了 `rd_en`→`data_read_valid` 的一拍延迟**（L255-L259），所以模块在仿真里依然会按两拍流水走。换句话说，节拍约束来自模块设计，仿真只是「按契约」给出一拍读延迟，并不放宽它。

---

## 5. 综合实践

把本讲四个最小模块串起来，完成下面这个**源码阅读 + 时序推理**的综合任务（即本讲指定的实践任务）。

**任务**：以 `wait_end_busy`（[L130-L148](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/simulation/image_processing_simulation.cpp#L130-L148)）为线索，端到端追踪一次「主机查询状态」的全过程，并回答三个问题。

**步骤**：

1. **入队**：`wait_end_busy` 先 `push` 一个 `COMMAND_GET_STATUS` 命令项，然后循环 100 次 `main_loop_clk`（[L135-L139](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/simulation/image_processing_simulation.cpp#L135-L139)）。说明这 100 拍里发生了什么：第 1 拍把命令送进 `comm_cmd`，之后 99 拍 `fifo_in` 为空、`comm_data_in_valid=0`，模块进入 `STATE_GET_STATUS` 开始回吐字节。
2. **回收**：模块每吐 1 字节，`main_loop_clk` 就把它 `push` 进 `fifo_out` 并把 `counter_free` 置 3（[L248-L252](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/simulation/image_processing_simulation.cpp#L248-L252)）。回答三个问题：
   - **(a) `counter_free==3` 模拟了什么物理现象？** → 通信线路被占满：主机/链路无法即时取走模块刚吐出的字节，对应硬件里 SPI 输出缓冲与 MPSSE 读取带宽有限（u6-l3/u6-l4）。每发 1 字节占线 3 拍，模块必须靠 `comm_data_out_free` 握手等待。
   - **(b) 为什么 `send_image` 要循环 `image_width*image_height+500` 次 `main_loop_clk`？** → 因为 `main_loop_clk` 每拍只从 `fifo_in` 弹 1 项，而 `send_image` 入队了 `1 + W*H` 项，至少要 `W*H+1` 拍才能排空；`+500` 是宽裕的安全余量，确保最后一两个像素打包写入完成、模块回到 `STATE_WAIT_COMMAND`。
   - **(c) `fifo_out` 在哪里被填充？** → 在 `main_loop_clk` 第 [L248-L249](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/simulation/image_processing_simulation.cpp#L248-L249) 行：当 `simulator->comm_data_out_valid==1` 时，把 `comm_data_out` 的值 `push` 进去。它是「模块输出 → 主机」的回收管道。
3. **取数**：`wait_end_busy` 从 `fifo_out` 弹出 4 字节，检查 `status_out[0]` 的 bit0（busy 位）决定是否继续轮询（[L141-L147](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/simulation/image_processing_simulation.cpp#L141-L147)）。

**延伸观察（可选，待本地验证）**：注意 [L147](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/simulation/image_processing_simulation.cpp#L147) 的循环条件 `status_out[0]&0x01 == 1`。由于 C++ 中 `==` 优先级高于 `&`，它实际等价于 `status_out[0] & (0x01==1)` 即 `status_out[0] & 1`——恰好在测 bit0，与 HDL [STATE_GET_STATUS](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L315) 里 `comm_data_out[0] <= ~(state_processing==STATE_IDLE)` 的 busy 位定义一致，所以行为正确。这是一个「恰好正确」的运算符优先级细节，阅读源码时值得留意（建议改成 `(status_out[0]&0x01) == 1` 更清晰）。

**预期结果**：你能用一张时序图把「主机 `wait_end_busy` → `fifo_in` 入队 → `main_loop_clk` 喂命令 → 模块 `STATE_GET_STATUS` 吐字节 → 反压 3 拍 → `fifo_out` 回收 → 主机读 bit0」这条完整链路画出来。

## 6. 本讲小结

- 仿真后端用 **`Operation` 结构 + `FIFO_OP` 队列**把一次高层调用翻译成一串「命令字节 + 数据字节」的脚本，等价于 u2-l2 的报文格式，只是传输外壳是内存队列而非 SPI。
- **`main_loop_clk()`** 是心脏：手动把 `clk` 在 0/1 间翻转、两次 `eval()` 走一拍，每拍从 `fifo_in` 弹 1 项驱动输入、把模块输出回收到 `fifo_out`。
- **`counter_free` 反压**模拟「通信线路被占满」：模块每吐 1 字节，`comm_data_out_free` 拉低 3 拍，迫使模块按真实握手等待，从而覆盖到与硬件相同的代码路径。
- **`memory[]`** 用一块 128 KB 的 `uint16_t` 数组模拟单端口 RAM：`addr/2` 把字节地址换算成字下标，`rd_en`→`data_read_valid` 保留一拍读延迟，与硬件后端的 SPRAM 时序契约一致。
- **`fifo_out`** 在 `comm_data_out_valid==1` 时被填充，是「模块输出 → 主机」的回收管道，供 `read_status`/`read_image`/`wait_end_busy` 取用。
- 仿真与硬件两套后端之所以能让同一份 HDL 跑通，是因为它们**对模块实现了同一组接口契约**（输入握手、输出反压、存储器时序）——仿真用队列+数组模拟，硬件用 SPI+SPRAM 实现。

## 7. 下一步学习建议

本讲把「仿真后端如何驱动核心模块」讲透了，但对应的「硬件后端」还在后面：

- **u6-l2 iCE40 硬件顶层与 SPRAM 接口**：看 `top.v` 如何把 `image_processing` 与 `ram_interface`、`spi_interface` 连起来，以及 `ram_interface` 如何用 4 片 `SB_SPRAM256KA` 实现本讲 `memory[]` 对应的真实存储（含读延迟流水对齐）——可与本讲 4.4 对照阅读。
- **u6-l3 SPI 从机接口与 SB_SPI 硬件块**：看硬件侧的「通信外壳」如何取代本讲的 `fifo_in`/`fifo_out` 与 `counter_free` 反压。
- **u6-l4 主机 SPI 软件：FTDI 与命令封装**：看 iCE40 后端的 C++ 侧如何用 MPSSE 收发 SPI，与本讲的「入队 + 拨时钟」对照，体会两种后端的对称与差异。

如果想在读完本讲后做点动手实验，建议：在 `main_loop_clk` 的读写分支和 `comm_data_out_valid` 分支各加一条带 `addr`/`data` 的 `printf`，重新编译运行一次任意 `test_*`，对照本讲的时序图核对日志——你会看到「每拍弹 1 项」「读延迟一拍」「发一字节占线三拍」真实发生在终端里。
