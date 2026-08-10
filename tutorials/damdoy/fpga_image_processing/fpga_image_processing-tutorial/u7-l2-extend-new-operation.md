# 扩展实践：添加一个新的图像处理操作

## 1. 本讲目标

本讲是整本手册的 capstone（收官实战）。前面六单元我们已经把项目拆成了零件——契约层、传输层、控制层、运算层、两套后端。本讲要反过来：**把这些零件装回去，亲手新增一条从未存在过的命令 `COMMAND_APPLY_GAMMA`（近似 gamma 校正）**。

学完后你应当能够：

- 说出新增一条图像处理命令需要**触达的全部代码位置**（共 5 个文件、4 个抽象层）。
- 理解「接口枚举 + 抽象虚函数」「两套后端打包」「HDL 派发 + 参数读取 + 运算分支」这三处改动的**联动关系**与**四方契约**。
- 能用 `wait_end_busy` + 状态机分支，在 Verilator 仿真模式下完整跑通一个新运算，并用 `run_gnuplot.sh` 验证输出图像。

---

## 2. 前置知识

本讲默认你已经学完依赖讲义，下面只做一句话唤醒，不展开：

- **u2-l1 / u2-l2**：`image_processing.hpp` 里的 `Commands` 枚举与纯虚基类 `Image_processing` 是「四方契约」；命令报文是「1 字节操作码 + 变长小端参数」。
- **u3-l3**：`image_processing.v` 用双 FSM——主 `state`（命令解析）+ `state_processing`（运算）——`STATE_WAIT_COMMAND` 按 `comm_cmd` 派发，`processing_command` 寄存器充当「运算工单」。
- **u4-l1**：`STATE_PROC_UNARY` 是被 add / threshold / invert / mult 复用的逐像素处理状态，单端口 RAM 读延迟一拍 → 拆成「偶拍读、奇拍算写」两拍流水，一个 16 位字同时处理两个像素。
- **u4-l2**：FPGA 无浮点，用 1.3.4 定点数；`apply_clamp` 取 `[7:0]` 饱和到 0~255，`apply_clamp_fixed16` 取 `[11:4]` 把乘积除以 16 还原尺度——**注意它用 `$signed` 比较**，传进去的值若高位为 1 会被误判为负。
- **u6-l1**：仿真后端用 `Operation` + `fifo_in` 队列把高层调用拆成「命令字节 + 数据字节」，`main_loop_clk()` 手动翻转时钟驱动 `Vimage_processing` 模型。

两个本讲要用到的术语：

- **扩展点（extension point）**：系统中「留给未来增加功能」的接缝。本项目的扩展点就是「加一条命令」这条贯穿 4 层的链路。
- **gamma 校正**：一种非线性亮度映射，数学上是 \( \text{out} = 255 \times (\text{in}/255)^{\gamma} \)。当 \( \gamma = 2.0 \) 时图像整体变暗（中间调被压低），常用作 sRGB 编码曲线。FPGA 上没有浮点 `pow()`，所以我们要做**近似**——这正是「为什么要扩展、扩展时要注意什么」的绝佳案例。

---

## 3. 本讲源码地图

本讲会动（或在脑子里改）这 5 个文件，它们横跨全部 4 个抽象层：

| 文件 | 所属层 | 在本讲的作用 |
|------|--------|--------------|
| [`software/image_processing.hpp`](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/image_processing.hpp) | 契约层 | 加枚举值 + 加纯虚函数（四方契约的「源」） |
| [`simulation/image_processing_simulation.cpp`](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/simulation/image_processing_simulation.cpp) | 传输层（仿真后端） | 把 `send_gamma` 打包成 FIFO 字节流 |
| [`ice40/software/image_processing_ice40.cpp`](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/software/image_processing_ice40.cpp) | 传输层（硬件后端） | 把 `send_gamma` 打包成 SPI 事务 |
| [`hdl/image_processing.v`](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v) | 控制层 + 运算层 | 加派发分支 + 新增读参状态 + 加运算分支 |
| [`software/main.cpp`](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp) | 业务层 | 写 `test_gamma` 调用，端到端验证 |

> 记住一张图（来自 u7-l1）：**外壳可换，内核不变**。`image_processing.v` 与它的命令协议是**共同内核**；仿真用 FIFO 队列、硬件用 SPI 事务，剥去传输外壳后送进内核的字节流逐字节相同。所以你加的这条命令，必须让两条外壳产出**完全一样**的字节序列。

---

## 4. 核心概念与源码讲解

### 4.1 扩展点的全链路影响：一张改动地图

#### 4.1.1 概念说明

很多项目号称「可扩展」，但真要加一个功能时才发现牵一发动全身。本项目的可贵之处在于：**扩展点是结构化的、可枚举的**——加一条逐像素命令，会沿着一条固定的「四层链」一路改下去。理解这条链，就理解了整个架构为什么这样切分。

「全链路影响」的意思是：你加的不只是一个函数，而是一份从软件枚举一直穿透到硅片状态机的**契约**。任何一层漏改，都会让命令在某一层「消失」，表现为仿真卡死、像素不对、或综合报错。

#### 4.1.2 核心流程：新增一条带参数的逐像素命令，要动 4 层

下表是从 0 到 1 增加一条 `COMMAND_APPLY_GAMMA`（带 1~2 字节参数）的改动地图：

| 步骤 | 层 | 文件 | 改什么 | 为什么 |
|------|----|------|--------|--------|
| ① | 契约 | `image_processing.hpp` | 在 `Commands` 枚举加 `COMMAND_APPLY_GAMMA`（数值与 HDL 对齐）；在基类加 `virtual void send_gamma(...) = 0` | 这是四方契约的「源头定义」 |
| ② | 传输（仿真） | `image_processing_simulation.cpp` | 实现 `send_gamma`：push 命令字节 + 参数字节到 `fifo_in` | 仿真后端要能驱动新命令 |
| ③ | 传输（硬件） | `image_processing_ice40.cpp` | 实现 `send_gamma`：发 `SPI_SEND_CMD` + 若干 `SPI_SEND_DATA` | 硬件后端要能经 SPI 发新命令 |
| ④ | 控制 + 运算 | `image_processing.v` | `STATE_WAIT_COMMAND` 加派发分支；新增 `STATE_APPLY_GAMMA_READ_PARAM` 读参状态；在 `STATE_PROC_UNARY` 加运算分支 | 内核要能识别、读参、执行新命令 |
| ⑤ | 业务 | `main.cpp` | 写 `test_gamma`，仿照「三明治」套路调用 | 端到端验证 |

> 一个常被忽略的点：步骤 ① 和 ④ 的枚举**数值必须相等**。软件侧 `COMMAND_APPLY_GAMMA` 是几，HDL 侧 `parameter COMMAND_APPLY_GAMMA = ...` 就得是几。这就是 u2-l1 讲的「四方契约」——跨进程、跨语言、跨软硬件的整数相等约定。

#### 4.1.3 源码精读：用 `COMMAND_APPLY_MULT` 当「现成标本」走一遍全链路

在动手加新命令前，最好的办法是**先看一条已有的、带参数的逐像素命令是怎么贯穿四层的**。`COMMAND_APPLY_MULT`（乘法）是最理想的标本——它带参数、用定点数、复用 `STATE_PROC_UNARY`，几乎和我们要加的 gamma 一模一样。

**契约层**——枚举里它是最后一个业务命令，值为 12：

```cpp
enum Commands {COMMAND_PARAM, ..., COMMAND_APPLY_MULT, COMMAND_NONE=255};
```
> 见 [software/image_processing.hpp:4-6](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/image_processing.hpp#L4-L6)。注意 `COMMAND_NONE=255` 是哨兵，所以新命令插在 `COMMAND_APPLY_MULT` 之后会得到 13，不会和哨兵冲突。

纯虚声明，强制两套后端都实现：

```cpp
virtual void send_mult(float value, bool clamp) = 0;
```
> 见 [software/image_processing.hpp:22](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/image_processing.hpp#L22)。

**传输层（仿真）**——把 float 量化成定点字节，再 push「1 命令字节 + 2 参数字节」：

```cpp
fifo_in.push(Operation(true, COMMAND_APPLY_MULT, 0));
fifo_in.push(Operation(false, COMMAND_NONE, val_fixed_4_4));  // 定点值
fifo_in.push(Operation(false, COMMAND_NONE, clamp));          // clamp
```
> 见 [simulation/image_processing_simulation.cpp:121-123](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/simulation/image_processing_simulation.cpp#L121-L123)。

**传输层（硬件）**——语义完全相同，只是把 `fifo_in.push` 换成 SPI 事务：

```cpp
spi_command_send(SPI_SEND_CMD, COMMAND_APPLY_MULT);
spi_command_send(SPI_SEND_DATA, val_fixed_4_4);
spi_command_send(SPI_SEND_DATA, clamp);
```
> 见 [ice40/software/image_processing_ice40.cpp:127-129](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/software/image_processing_ice40.cpp#L127-L129)。

> **关键观察**：剥去传输外壳（`Operation`+队列 vs `SPI_SEND_*`），两段代码发出去了**逐字节相同**的 3 字节序列：`[12, val_fixed_4_4, clamp]`。这就是 u2-l2 的核心结论，也是你写 `send_gamma` 时必须守住的纪律——两套后端要产出一样的字节。

**控制层（HDL 派发）**——`STATE_WAIT_COMMAND` 看到 `comm_cmd==COMMAND_APPLY_MULT`，预装计数器并跳到读参状态：

```verilog
COMMAND_APPLY_MULT: begin
   state <= STATE_APPLY_MULT_READ_PARAM;
   counter_read <= 1;          // 要读 2 个参数字节（计数器语义见下）
end
```
> 见 [hdl/image_processing.v:278-281](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L278-L281)。

**控制层（HDL 读参 + 交接）**——读完最后一个参数字节那一拍，一次性把「运算工单」交给运算 FSM：

```verilog
STATE_APPLY_MULT_READ_PARAM: begin
   if(comm_data_in_valid == 1 && counter_read == 1) begin
      mult_value_param <= comm_data_in;   // 第 1 字节：定点值
      counter_read <= 0;
   end else if (comm_data_in_valid == 1 && counter_read == 0) begin
      state_processing <= STATE_PROC_UNARY;            // 复用逐像素状态
      processing_command <= COMMAND_APPLY_MULT;        // 指定算法分支
      state <= STATE_WAIT_COMMAND;
      clamp <= comm_data_in[0];                         // 第 2 字节：clamp
      proc_counter_read <= img_width*img_height;
      proc_memory_addr_counter <= buffer_storage_address;
   end
end
```
> 见 [hdl/image_processing.v:486-498](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L486-L498)。注意 `counter_read <= 1` 对应「读 2 字节」：值为 1 时读第 1 字节，值为 0 时读第 2 字节并交接。

**运算层（HDL 分支）**——`STATE_PROC_UNARY` 里用 `processing_command` 选分支：

```verilog
end else if(processing_command == COMMAND_APPLY_MULT) begin
   temp_calc = {8'b0, mult_value_param}*{8'b0, data_read[7:0]};
   data_write[7:0] <= apply_clamp_fixed16(temp_calc, clamp);
   temp_calc = {8'b0, mult_value_param}*{8'b0, data_read[15:8]};
   data_write[15:8] <= apply_clamp_fixed16(temp_calc, clamp);
end
```
> 见 [hdl/image_processing.v:540-544](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L540-L544)。高低字节各算一个像素，乘积用 `apply_clamp_fixed16` 取 `[11:4]` 还原尺度。

这就是一条命令的「完整一生」。你加 `COMMAND_APPLY_GAMMA`，就是在每一层复制这套模式。

#### 4.1.4 代码实践：用 grep 丈量一条命令的足迹

1. **实践目标**：亲手确认一条命令确实散落在 5 个文件里，建立「全链路」的肌肉记忆。
2. **操作步骤**：在仓库根目录，针对 `COMMAND_APPLY_MULT`（或 `MULT`）搜索全部出现位置：
   ```
   git grep -n "MULT" -- '*.hpp' '*.cpp' '*.v'
   ```
3. **需要观察的现象**：输出会同时命中 `image_processing.hpp`（枚举）、两个后端 `.cpp`（打包）、`image_processing.v`（派发 + 读参 + 运算分支三处）、`main.cpp`（测试调用）。
4. **预期结果**：至少 6~8 处命中，分布在 5 个文件。这就是「加一条命令」的最低改动面。
5. 若你的工具链没有 `git grep`，用 `grep -rn "MULT" software simulation ice40 hdl` 代替。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `COMMAND_NONE` 被显式设为 255，而不是跟在 `COMMAND_APPLY_MULT` 后面自然递增？

**参考答案**：255 是一个「哨兵值」，故意远离业务命令（当前最大才 12），既可作为「无命令」的占位符，又给新增命令留出了连续编号空间（13、14、…），避免新命令和哨兵撞车。

**练习 2**：如果只在 `image_processing.hpp` 加了枚举、却忘了在 `image_processing.v` 加对应的 `parameter`，仿真会发生什么？

**参考答案**：软件会发出操作码 13，但 HDL 的 `STATE_WAIT_COMMAND` 里 `case(comm_cmd)` 没有对应分支，会落入 `default`（[image_processing.v:282-283](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L282-L283) 的空 `default`），命令被静默丢弃，模块一直停在 `STATE_WAIT_COMMAND`，`wait_end_busy` 永远等不到 busy 拉低——表现为仿真卡死。

---

### 4.2 契约层：Commands 枚举扩展 + 接口新增虚函数

#### 4.2.1 概念说明

契约层是「源头」。它定义三件事：**命令叫什么名字、它的数值是多少、它对应哪个高层函数**。这一层一旦定下，后面三层都是在「实现」它。

为什么把枚举和虚函数放在同一个头文件里？因为它们共同构成了**软件侧的契约**：枚举约定「线上字节」，虚函数约定「调用姿势」。两套后端必须同时遵守这两者，否则要么字节对不上、要么根本没法调用。

#### 4.2.2 核心流程

定义新命令 `send_gamma` 的契约：

1. 在 `Commands` 枚举末尾、`COMMAND_NONE=255` 之前插入 `COMMAND_APPLY_GAMMA`，使它取到数值 **13**。
2. 在基类 `Image_processing` 加一个纯虚函数 `send_gamma`，参数按需设计。
3. **同步**在 `image_processing.v` 加 `parameter COMMAND_APPLY_GAMMA = COMMAND_APPLY_MULT+1;`（也是 13）——数值相等是硬约束。

设计决策：`send_gamma` 需要哪些参数？gamma 校正 \( \text{out}=255(\text{in}/255)^{\gamma} \) 需要指数 \( \gamma \)，但 FPGA 上算幂不现实。我们采用一个**可综合的近似**：用 \( \text{out}=(\text{in}^2) \gg n \) 来近似 \( \gamma \approx 2 \) 的暗化曲线，并把右移量 `n` 作为参数下发，从而让主机能控制曲线强度。于是签名定为：

```cpp
virtual void send_gamma(uint8_t darken_shift, bool clamp) = 0;  // 示例代码
```

其中 `darken_shift` 就是右移量 `n`（典型值 8 → 近似 gamma 2.0），`clamp` 控制是否饱和。它和 `send_mult` 一样带「1 个数值参数 + 1 个 clamp 位」，正好可以复用读参模式。

#### 4.2.3 源码精读

现有枚举的尾部结构（我们要在这里插入）：

```cpp
enum Commands {COMMAND_PARAM, COMMAND_SEND_IMG, COMMAND_READ_IMG, COMMAND_GET_STATUS, COMMAND_APPLY_ADD, COMMAND_APPLY_THRESHOLD,
               COMMAND_SWITCH_BUFFERS, COMMAND_BINARY_ADD, COMMAND_APPLY_INVERT,
               COMMAND_CONVOLUTION, COMMAND_BINARY_SUB, COMMAND_BINARY_MULT, COMMAND_APPLY_MULT, COMMAND_NONE=255};
```
> 见 [software/image_processing.hpp:4-6](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/image_processing.hpp#L4-L6)。

基类把每个操作都声明成纯虚函数（这里以 mult 为例，gamma 照葫芦画瓢）：

```cpp
class Image_processing {
public:
   ...
   virtual void send_mult(float value, bool clamp) = 0;
   ...
protected:
   uint16_t image_width;
   uint16_t image_height;
};
```
> 见 [software/image_processing.hpp:8-39](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/image_processing.hpp#L8-L39)。注意 `image_width/height` 是 `protected`，两套后端的 `send_gamma` 实现里若要用尺寸，直接复用即可。

> **示例代码**——契约层的完整改动（两处）：
> ```cpp
> // ① 枚举：在 COMMAND_APPLY_MULT 之后、COMMAND_NONE 之前插入
> ..., COMMAND_APPLY_MULT, COMMAND_APPLY_GAMMA, COMMAND_NONE=255};
>
> // ② 基类：新增纯虚函数
> virtual void send_gamma(uint8_t darken_shift, bool clamp) = 0;
> ```

#### 4.2.4 代码实践：补齐契约与声明

1. **实践目标**：在不改任何其他文件的前提下，先把契约层的两处加好，然后观察编译会报什么错——这能帮你确认「契约驱动实现」的设计。
2. **操作步骤**：
   - 按上面「示例代码」修改 `software/image_processing.hpp`。
   - 同时在两个后端头文件 [`simulation/image_processing_simulation.hpp`](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/simulation/image_processing_simulation.hpp) 与 [`ice40/software/image_processing_ice40.hpp`](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/software/image_processing_ice40.hpp) 里加 `virtual void send_gamma(uint8_t darken_shift, bool clamp);` 声明（与现有 `send_mult` 并列）。
   - 运行 `./build_simulation.sh`。
3. **需要观察的现象**：编译会**报链接错误**，提示 `Image_processing_simulation` 是抽象类、无法实例化（或 `send_gamma` 未定义引用）。
4. **预期结果**：这正是「基类加了纯虚 → 派生类必须实现」的强制力。这个错误是**好错误**——它在告诉你下一步该去两个后端写实现。记下错误信息，接着做 4.3。
5. 待本地验证：具体报错文本取决于 g++ 版本。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `COMMAND_APPLY_GAMMA` 插到了 `COMMAND_BINARY_SUB` 和 `COMMAND_BINARY_MULT` 中间，会出什么问题？

**参考答案**：枚举值是按位置连续递增的，插在中间会让 `COMMAND_BINARY_MULT`、`COMMAND_APPLY_MULT` 的**数值**整体后移，而 HDL 侧 `parameter` 是手工用 `+1` 串起来的，若不同步重排，软件发的「数值 12」和硬件认的「数值 12」就不再是同一个命令了——四方契约破裂。所以新命令务必加在**末尾**。

**练习 2**：为什么 `send_gamma` 声明为纯虚（`= 0`）而不是在基类里给个默认实现？

**参考答案**：纯虚强制每个后端必须显式实现，编译器会在你漏写时立刻报错（见 4.2.4）。若给默认实现，漏写的后端会「静默继承」一个错误或空实现，bug 会推迟到运行时才暴露。

---

### 4.3 传输层：两套后端的打包实现

#### 4.3.1 概念说明

契约层定义了「调用姿势」，传输层负责把一次高层调用**翻译成线上字节**。两套后端翻译出的字节必须**逐字节相同**——这是 u2-l2 反复强调的纪律，也是「一份 main.cpp 驱动两套后端」成立的前提。

仿真后端把字节推进内存队列 `fifo_in`；硬件后端把字节包成 SPI 事务发出去。外壳不同，内核字节流相同。

#### 4.3.2 核心流程

实现 `send_gamma(uint8_t darken_shift, bool clamp)` 的打包：

1. 推/发 1 个**命令字节** `COMMAND_APPLY_GAMMA`（值 13）。
2. 推/发 2 个**参数字节**：先 `darken_shift`（小端低字节，因它本身就是单字节），再 `clamp`（0/1）。
3. 仿真后端在 push 后跑若干拍 `main_loop_clk()` 让模型消费这些字节；硬件后端不需要（SPI 调用本身是同步阻塞的）。

对照 `send_mult`（也是「1 命令 + 2 参数」）就能照抄结构。

#### 4.3.3 源码精读

**仿真后端** `send_mult`：把 float 量化成定点字节后，push 三项（命令 + 定点值 + clamp），再跑 10 拍让模型消化：

```cpp
fifo_in.push(Operation(true, COMMAND_APPLY_MULT, 0));
fifo_in.push(Operation(false, COMMAND_NONE, val_fixed_4_4));
fifo_in.push(Operation(false, COMMAND_NONE, clamp));
for (size_t i = 0; i < 10; i++) { main_loop_clk(); }
```
> 见 [simulation/image_processing_simulation.cpp:121-127](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/simulation/image_processing_simulation.cpp#L121-L127)。`Operation(true, ...)` 表示命令字节、`Operation(false, ...)` 表示数据字节（u6-l1）。

**硬件后端** `send_mult`：语义相同，把 push 换成 SPI 事务：

```cpp
spi_command_send(SPI_SEND_CMD, COMMAND_APPLY_MULT);
spi_command_send(SPI_SEND_DATA, val_fixed_4_4);
spi_command_send(SPI_SEND_DATA, clamp);
```
> 见 [ice40/software/image_processing_ice40.cpp:127-129](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/software/image_processing_ice40.cpp#L127-L129)。

> **示例代码**——两套后端的 `send_gamma` 实现（请分别贴到对应 `.cpp`）：
> ```cpp
> // simulation/image_processing_simulation.cpp
> void Image_processing_simulation::send_gamma(uint8_t darken_shift, bool clamp){
>    fifo_in.push(Operation(true, COMMAND_APPLY_GAMMA, 0));
>    fifo_in.push(Operation(false, COMMAND_NONE, darken_shift));
>    fifo_in.push(Operation(false, COMMAND_NONE, clamp));
>    for (size_t i = 0; i < 10; i++) { main_loop_clk(); }
> }
>
> // ice40/software/image_processing_ice40.cpp
> void Image_processing_ice40::send_gamma(uint8_t darken_shift, bool clamp){
>    spi_command_send(SPI_SEND_CMD, COMMAND_APPLY_GAMMA);
>    spi_command_send(SPI_SEND_DATA, darken_shift);
>    spi_command_send(SPI_SEND_DATA, clamp);
> }
> ```
>
> 两段代码发出的字节流都是 `[13, darken_shift, clamp]`——逐字节相同，纪律守住。

#### 4.3.4 代码实践：让两套后端「说得一样」

1. **实践目标**：把 4.2.4 的链接错误消除，并验证两套后端产出相同字节。
2. **操作步骤**：
   - 按上面「示例代码」在两个后端 `.cpp` 里实现 `send_gamma`（别忘了在两个 `.hpp` 里也已加了声明）。
   - 重新 `./build_simulation.sh`，应当能通过编译。
3. **需要观察的现象**：编译通过；若你临时在 `send_gamma` 第一行加一句 `printf("send_gamma: shift=%u clamp=%d\n", darken_shift, clamp);`，调用时能看到打印。
4. **预期结果**：链接错误消失。此时命令虽能打包发出，但 HDL 还不认识 13 号命令（见 4.1.5 练习 2），所以仿真会卡在 `wait_end_busy`——这是预期的，下一步去改 HDL。
5. 待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：假如你在仿真后端把参数顺序写反了（先 push `clamp` 再 push `darken_shift`），而硬件后端写对了，会发生什么？

**参考答案**：两套后端产出**不同**的字节流，违反「逐字节相同」纪律。仿真模式下 HDL 把第 1 个参数字节当 `darken_shift`，于是拿到的是 clamp（0 或 1），gamma 曲线完全错乱；而硬件模式正常。这正是「契约必须两侧同步」的反面教材。

**练习 2**：`send_gamma` 里为什么不需要像 `send_add` 那样把 `int16_t` 拆成两个小端字节？

**参考答案**：`darken_shift` 是 `uint8_t`，本身就是单字节，无需拆分；`send_add` 的 `value` 是 16 位有符号，需要拆成低/高两个字节（见 [simulation/image_processing_simulation.cpp:72-78](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/simulation/image_processing_simulation.cpp#L72-L78)）。参数字节数由「值的位宽」决定，并要在 HDL 派发时给 `counter_read` 预装对应数值。

---

### 4.4 控制与运算层：HDL 命令派发 + 参数读取 + 处理分支

#### 4.4.1 概念说明

这是改动最重的一层。HDL 要完成三件事，对应三个修改点：

1. **派发**：在 `STATE_WAIT_COMMAND` 加一个 `COMMAND_APPLY_GAMMA` 分支，告诉主 FSM「这条命令要读几个参数、读完去哪」。
2. **读参 + 交接**：新增一个 `STATE_APPLY_GAMMA_READ_PARAM` 状态，按字节读完参数后，把「运算工单」(`processing_command` + `state_processing`) 交给运算 FSM。
3. **运算**：在 `STATE_PROC_UNARY` 加一个 `COMMAND_APPLY_GAMMA` 分支，定义「对每个像素怎么算」。

第 2 步之所以要新增状态，是因为本项目把「读参数」做成独立状态（`counter_read` 驱动的字节计数器）。无参数命令（如 invert）可以省掉这一步、直接在 `STATE_WAIT_COMMAND` 里交接（见 [image_processing.v:262-269](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L262-L269)）；但 gamma 带参数，必须走读参状态。

#### 4.4.2 核心流程

`COMMAND_APPLY_GAMMA` 的 HDL 生命周期：

```
STATE_WAIT_COMMAND (收到 comm_cmd=13)
   ├─ counter_read <= 1            // 要读 2 个参数字节
   └─ state <= STATE_APPLY_GAMMA_READ_PARAM

STATE_APPLY_GAMMA_READ_PARAM
   ├─ counter_read==1：读 darken_shift 字节 → counter_read<=0
   └─ counter_read==0：读 clamp 字节，并「交接」：
        ├─ processing_command <= COMMAND_APPLY_GAMMA
        ├─ state_processing    <= STATE_PROC_UNARY   // 复用逐像素状态
        ├─ proc_counter_read   <= img_width*img_height
        ├─ proc_memory_addr_counter <= buffer_storage_address
        └─ state <= STATE_WAIT_COMMAND

STATE_PROC_UNARY (由运算 FSM 驱动，两拍流水)
   ├─ 偶拍：rd_en<=1，发起读
   └─ 奇拍：data_read_valid==1 时，
        ├─ processing_command==COMMAND_APPLY_GAMMA 分支：
        │     out = (in*in) >> darken_shift   // 近似 gamma 2.0
        ├─ data_write 写回，wr_en<=1
        └─ proc_counter_read 减 2，到 0 → state_processing<=IDLE
```

> gamma 的近似公式 \( \text{out}=(\text{in}^2) \gg n \)：当 \( n=8 \) 时，\( \text{in}=255 \) 得 \( 65025\gg8=254 \)，\( \text{in}=128 \) 得 \( 16384\gg8=64 \)，\( \text{in}=64 \) 得 \( 4096\gg8=16 \)。中间调被大幅压低，整体变暗，逼近 \( \gamma=2 \) 曲线。这是**纯整数、可综合**的近似，不需要浮点或查找表。

#### 4.4.3 源码精读

**派发分支**——照着 `COMMAND_APPLY_MULT` 的分支抄（注意 `counter_read<=1` 表示读 2 字节）：

```verilog
COMMAND_APPLY_MULT: begin
   state <= STATE_APPLY_MULT_READ_PARAM;
   counter_read <= 1;
end
```
> 见 [hdl/image_processing.v:278-281](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L278-L281)。gamma 分支把 `STATE_APPLY_MULT_READ_PARAM` 换成 `STATE_APPLY_GAMMA_READ_PARAM` 即可。

**读参 + 交接**——照着 `STATE_APPLY_MULT_READ_PARAM` 抄：

```verilog
STATE_APPLY_MULT_READ_PARAM: begin
   if(comm_data_in_valid == 1 && counter_read == 1) begin
      mult_value_param <= comm_data_in;       // 第 1 字节
      counter_read <= 0;
   end else if (comm_data_in_valid == 1 && counter_read == 0) begin
      state_processing <= STATE_PROC_UNARY;
      processing_command <= COMMAND_APPLY_MULT;
      state <= STATE_WAIT_COMMAND;
      clamp <= comm_data_in[0];               // 第 2 字节
      proc_counter_read <= img_width*img_height;
      proc_memory_addr_counter <= buffer_storage_address;
   end
end
```
> 见 [hdl/image_processing.v:486-498](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L486-L498)。gamma 版把第 1 字节存进新寄存器 `gamma_shift`，第 2 字节照旧存 `clamp`，`processing_command` 改为 `COMMAND_APPLY_GAMMA`。

**运算分支**——照着 `COMMAND_APPLY_MULT` 分支抄，但把「乘定点系数」换成「自乘再移位」：

```verilog
end else if(processing_command == COMMAND_APPLY_MULT) begin
   temp_calc = {8'b0, mult_value_param}*{8'b0, data_read[7:0]};
   data_write[7:0] <= apply_clamp_fixed16(temp_calc, clamp);
   temp_calc = {8'b0, mult_value_param}*{8'b0, data_read[15:8]};
   data_write[15:8] <= apply_clamp_fixed16(temp_calc, clamp);
end
```
> 见 [hdl/image_processing.v:540-544](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L540-L544)。

> **⚠️ 关键陷阱（承接 u4-l2）**：不能直接把 \( \text{in}^2 \)（最大 65025）塞给 `apply_clamp`。因为 `apply_clamp` 内部用 `$signed(in)` 做比较（[image_processing.v:151-163](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L151-L163)），而 65025 = `0xFE01` 作为 16 位有符号数是 **-511**，会被判成 `<0` 而错误地钳到 0。正确做法是**先移位、再钳位**：移位后结果 ≤ 254，落在 8 位内，再 `{8'b0, ...}` 补零送给 `apply_clamp`，`$signed` 才不会误判。
>
> **示例代码**——HDL 三处改动：
> ```verilog
> // ① 枚举：在 COMMAND_APPLY_MULT 之后加（数值自动为 13）
> parameter COMMAND_APPLY_GAMMA = COMMAND_APPLY_MULT+1;
>
> // ② 新增读参状态：需同时在顶部 parameter 链里声明 STATE_APPLY_GAMMA_READ_PARAM
> STATE_APPLY_GAMMA_READ_PARAM: begin
>    if(comm_data_in_valid == 1 && counter_read == 1) begin
>       gamma_shift <= comm_data_in;           // 第 1 字节：右移量 n
>       counter_read <= 0;
>    end else if (comm_data_in_valid == 1 && counter_read == 0) begin
>       state_processing <= STATE_PROC_UNARY;
>       processing_command <= COMMAND_APPLY_GAMMA;
>       state <= STATE_WAIT_COMMAND;
>       clamp <= comm_data_in[0];              // 第 2 字节：clamp
>       proc_counter_read <= img_width*img_height;
>       proc_memory_addr_counter <= buffer_storage_address;
>    end
> end
>
> // ③ 派发分支：在 STATE_WAIT_COMMAND 的 case 里加
> COMMAND_APPLY_GAMMA: begin
>    state <= STATE_APPLY_GAMMA_READ_PARAM;
>    counter_read <= 1;                         // 读 2 字节
> end
>
> // ④ 运算分支：在 STATE_PROC_UNARY 的 if-else 链末尾加
> end else if(processing_command == COMMAND_APPLY_GAMMA) begin
>    temp_calc = {8'b0, data_read[7:0]} * {8'b0, data_read[7:0]};   // in*in，最大 65025
>    data_write[7:0]  <= apply_clamp({8'b0, temp_calc[15:0]>>gamma_shift}, clamp); // 先移位再钳位
>    temp_calc = {8'b0, data_read[15:8]} * {8'b0, data_read[15:8]};
>    data_write[15:8] <= apply_clamp({8'b0, temp_calc[15:0]>>gamma_shift}, clamp);
> end
> ```
> 另需在 `//local reg` 区（[image_processing.v:87 附近](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L87)）声明 `reg [7:0] gamma_shift;`，并在顶部 STATE parameter 链（[image_processing.v:32-43](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L32-L43)）里追加 `STATE_APPLY_GAMMA_READ_PARAM = <上一项>+1`。

> 注意 `>>gamma_shift` 是**变量移位**：Verilator 仿真可直接跑；上真实 iCE40 时为省面积，可把 `gamma_shift` 限制为 3~4 位宽的常数集（如只允许 6/7/8）。

#### 4.4.4 代码实践：让内核认识 13 号命令

1. **实践目标**：完成 HDL 三处改动后，让 `COMMAND_APPLY_GAMMA` 端到端可执行。
2. **操作步骤**：
   - 按「示例代码」修改 `hdl/image_processing.v`：加 parameter、加寄存器声明、加 STATE 声明、加派发分支、加读参状态、加运算分支。
   - 重新 `./build_simulation.sh`（Verilator 会重新翻译改过的 `.v`）。
3. **需要观察的现象**：Verilator 编译通过；运行 `./simu` 后（先临时在 `main.cpp` 里调用 `test_gamma`，见第 5 节），`wait_end_busy` 能正常返回（不再卡死），说明内核已正确交接给运算 FSM 并执行完毕。
4. **预期结果**：`output.dat` 里每个像素变成 \( (\text{原值}^2)\gg 8 \)，图像整体明显变暗。
5. 待本地验证：变量移位在 Verilator 下行为正确，但若你打算上 iCE40，需额外确认综合后资源与时序。

#### 4.4.5 小练习与答案

**练习 1**：为什么 gamma 的运算分支必须**先移位再 `apply_clamp`**，而 mult 分支是**先乘、直接 `apply_clamp_fixed16`**？

**参考答案**：mult 的乘积是「定点数 × 像素」，设计上就是 16 位定点、要用 `apply_clamp_fixed16` 取 `[11:4]` 除以 16 还原；而 gamma 的 `in*in` 是**整数平方**（不是定点编码），最大 65025 会撑破 16 位有符号范围，必须先 `>>gamma_shift` 把值压回 8 位以内，再用 `apply_clamp`（取 `[7:0]`）。混用钳位函数会触发 u4-l2 讲的 `$signed` 误判。

**练习 2**：如果 `send_gamma` 改成**无参数**（gamma 值固定），HDL 可以省掉哪两处改动？

**参考答案**：可以省掉「新增 `STATE_APPLY_GAMMA_READ_PARAM` 状态」和「派发分支里跳读参状态」，改为像 `COMMAND_APPLY_INVERT` 那样在 `STATE_WAIT_COMMAND` 里**当场交接**（直接设 `processing_command`、`state_processing<=STATE_PROC_UNARY`、`proc_counter_read`、`proc_memory_addr_counter`，见 [image_processing.v:262-269](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L262-L269)）。是否带参数，决定了你要不要新增读参状态。

---

## 5. 综合实践

把 4.2~4.4 的改动串起来，做一个端到端的 `test_gamma`。这是本讲的 capstone 任务。

### 实践目标

新增 `COMMAND_APPLY_GAMMA`，在 Verilator 仿真模式下对测试图做 gamma≈2.0 暗化，用 `run_gnuplot.sh` 查看暗化后的图像。

### 操作步骤

1. **契约层**（`software/image_processing.hpp`）：加枚举 `COMMAND_APPLY_GAMMA`（值 13）+ 纯虚 `send_gamma(uint8_t darken_shift, bool clamp)`；两个后端 `.hpp` 加同名声明。
2. **传输层**（两个后端 `.cpp`）：按 4.3.3 示例代码实现 `send_gamma`，确保两套后端字节流均为 `[13, darken_shift, clamp]`。
3. **控制+运算层**（`hdl/image_processing.v`）：按 4.4.3 示例代码加 parameter / 寄存器 / STATE / 派发分支 / 读参状态 / 运算分支。
4. **业务层**（`software/main.cpp`）：仿照 `test_multiplication` 写 `test_gamma`。

> **示例代码**——`test_gamma`（贴到 `main.cpp`，与其它 test 函数并列）：
> ```cpp
> void test_gamma(uint8_t *image_input, uint8_t *image_output, Image_processing *img_proc){
>    img_proc->send_params(image_width, image_height);
>    img_proc->send_image(image_input);   // 图进入 input 缓冲
>
>    img_proc->switch_buffers();           // input/storage 互换：原图现在在 storage
>
>    img_proc->send_gamma(8, true);        // 近似 gamma 2.0，右移 8，开启钳位
>    img_proc->wait_end_busy();            // 等运算 FSM 回 IDLE
>
>    img_proc->switch_buffers();           // 再互换：结果现在在 input
>    img_proc->read_image(image_output);   // 从 input 读回
> }
> ```
> 这个「三明治」套路（`send_params → send_image → switch_buffers → 发运算 → wait_end_busy → switch_buffers → read_image`）和 [`test_multiplication`](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L153-L165) 完全一致——因为运算都写回 storage、读回都从 input，所以两侧各 switch 一次。

5. 在 `main()` 的「test selection」区（[main.cpp:252-260](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L252-L260)）注释掉别的、启用 `test_gamma(image_input, image_output, img_proc);`。
6. 构建：`./build_simulation.sh`。
7. 运行：`./simu`。
8. 可视化：`./run_gnuplot.sh`。

### 需要观察的现象

- `./simu` 运行期间，`wait_end_busy` 打印若干次后停止（不再无限循环），说明 gamma 运算真正执行完毕。
- gnuplot 窗口里图像比原图**明显偏暗**，但轮廓仍可辨认——这是 gamma≈2.0 暗化的典型效果。

### 预期结果

- 中间灰调（如 128）被压到约 64，亮区（如 255→254）几乎不变，暗区（如 32→4）几乎全黑——非线性压低中间调。
- 若图像呈全黑或全白，排查清单：
  - 全黑 → 多半是 4.4.3 的陷阱（把 `in*in` 直接给了 `apply_clamp` 被 `$signed` 钳到 0）。
  - 完全不变 → HDL 派发分支没生效，13 号命令落进了空 `default`。
  - 仿真卡死 → 读参状态 `counter_read` 逻辑或参数字节数对不上（应是读 2 字节，`counter_read<=1`）。

### 验证契约的进阶检查

在两套后端的 `send_gamma` 第一行临时加打印，确认两者发出的字节一致；再用 4.1.4 的 `git grep "GAMMA"` 确认改动覆盖了全部 5 个文件。这一步是在培养「扩展即契约」的工程直觉。

> 待本地验证：以上现象与数值依赖本机 Verilator 版本与测试图内容。

---

## 6. 本讲小结

- 新增一条图像处理命令，要沿「契约 → 传输(×2 后端) → 控制 → 运算 → 业务」这条**四层五文件**的链路同步修改，任何一层漏改都会让命令在某一层「消失」。
- **四方契约**的核心是枚举数值相等：软件 `COMMAND_APPLY_GAMMA` 与 HDL `parameter` 必须同值（13），且新命令只能加在枚举**末尾**，不能插中间。
- 带参数的命令要在 HDL 新增一个 `*_READ_PARAM` 状态、用 `counter_read` 数字节；读完末字节那一拍一次性交接「运算工单」（`processing_command` + `state_processing`）。无参数命令可省掉读参状态、在 `STATE_WAIT_COMMAND` 当场交接。
- 逐像素运算尽量**复用 `STATE_PROC_UNARY` + 新 `processing_command` 分支**，不必新建运算状态；两拍流水（偶拍读、奇拍算写）和 16 位字双像素处理由复用自动获得。
- **钳位函数不能乱用**：整数平方等「未编码的大值」要先移位压回 8 位再用 `apply_clamp`，否则 `apply_clamp` 内部的 `$signed` 比较会把高位为 1 的值误判为负、钳到 0。
- 两套后端必须产出**逐字节相同**的字节流（仿真用 FIFO、硬件用 SPI），这是「一份 main.cpp 驱动两套后端」的根本纪律。

---

## 7. 下一步学习建议

- **把 gamma 搬上真硬件**：按 u1-l4 / u6-l2 / u6-l3 的流程，在 `ice40/hdl` 用 yosys+arachne-pnr 重新综合含 `COMMAND_APPLY_GAMMA` 的 `top.v`，烧录后用 `soft_ice40` 验证。重点观察变量移位 `>>gamma_shift` 综合后的资源占用，必要时把它收窄为常数集。
- **做一个真正的 gamma**：当前的 \( (\text{in}^2)\gg n \) 只是单点近似。若要支持任意 \( \gamma \)，可新增一个处理状态、用 `convolution_buffer` 之外的一块 **256 项查找表（LUT）** 做 `out = lut[in]`，LUT 由主机通过一条新命令预装——这会综合用到 iCE40 的 SPRAM 或 BRAM，是对 u6-l2 存储接口的进阶练习。
- **补齐 `COMMAND_BINARY_MULT` 的派发**：u4-l3 提到该运算的 HDL 分支已铺好、但 `STATE_WAIT_COMMAND` 缺派发分支而不可达。用本讲学到的「派发分支 + 读参状态」模式把它接通，作为一次小型的独立扩展练习。
- **重读 u7-l1**：带着亲手加过一条命令的经验回去看「外壳可换，内核不变」，你会对「共同内核 vs 可替换外壳」有完全不同的体会——你已经亲手摸过内核了。
