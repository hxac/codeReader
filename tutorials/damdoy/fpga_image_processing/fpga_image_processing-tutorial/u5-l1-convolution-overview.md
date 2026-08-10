# 卷积原理与卷积状态机总览

## 1. 本讲目标

卷积（convolution）是本项目支持的三类图像运算中最复杂的一类，也是整个 `image_processing.v` 里状态最多、时序最精巧的部分。本讲是「3x3 卷积引擎」单元的第一篇，负责把卷积从「数学定义」到「硬件状态机」这条主线打通，但不深入行缓冲的位移细节（那留给下一篇 u5-l2）。

读完本讲，你应该能够：

1. 用一句话和一条公式说清 3x3 图像卷积在做什么。
2. 说清卷积命令的三个参数位 `clamp` / `source_input` / `add_to_result` 各自的作用，以及它们在主机侧和硬件侧是如何对应（位打包 ↔ 位解包）的。
3. 看懂卷积核（kernel）的 1.3.4 定点格式，能手算 `(1)<<4` 代表多少。
4. 说出卷积运算 FSM 的三段流水（读参数 → 读+预取 → 9 拍累加 → 写回）的职责分工与状态跳转顺序。
5. 理解 `add_to_result` 如何让多个卷积核的结果累加到同一张图上，从而实现 `test_simple_edge_detection` 那样的多方向边缘检测。

---

## 2. 前置知识

本讲建立在前面几讲已经建立的认知之上，这里只做最简短的回顾，不重复细节。

- **2D 卷积的直觉**：用一个 3x3 的小窗口（核）在图像上滑动，每到一个位置就把窗口里的 9 个像素分别乘上核里对应的 9 个系数再求和，得到输出图的一个像素。卷积常用于模糊、锐化、边缘检测。
- **1.3.4 定点数**（详见 u4-l2）：项目没有浮点单元，用「1 位符号 + 3 位整数 + 4 位小数」的定点格式表示实数，本质是「把实数乘 16 存成整数」。所以 `(1)<<4`（=16）代表 1.0，`(1)<<3`（=8）代表 0.5。
- **双缓冲 input / storage**（详见 u3-l2）：图像从 input 缓冲进出，运算在 storage 缓冲里做、结果写回 storage。卷积既可以从 input 读源图像，也可以从 storage 读，由 `source_input` 决定。
- **单端口 RAM 与两拍流水**（详见 u4-l1）：模块不自带 RAM，存储器由后端提供；单端口 RAM 一个时钟沿只能读或写一次，读数据要延迟一拍才有效（`data_read_valid` 握手）。这是卷积状态机被拆成多段的根本原因。
- **双 FSM 架构**（详见 u3-l3）：主 FSM `state` 负责命令解析与派发，运算 FSM `state_processing` 负责实际运算；两者共用时钟、互不阻塞。卷积期间主 FSM 仍能响应状态查询，`busy` 位取自 `state_processing != STATE_IDLE`。

> 关键术语：卷积核（kernel / convolution matrix）、邻域（neighborhood）、乘加（MAC, Multiply-Accumulate）、行缓冲（line buffer）、符号扩展（sign extension）、反压（backpressure）。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [hdl/image_processing.v](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v) | 核心 HDL 模块。本讲关注其中卷积相关的状态、寄存器和 `apply_clamp_fixed16` 钳位函数。 |
| [software/main.cpp](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp) | 主机程序。本讲关注 `test_gaussian_blur` 和 `test_simple_edge_detection` 两个卷积测试函数，看主机如何构造卷积核并调用 `send_convolution`。 |
| [software/image_processing.hpp](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/image_processing.hpp) | 抽象基类与 `Commands` 枚举，定义 `send_convolution` 纯虚接口和 `COMMAND_CONVOLUTION` 操作码。 |
| [simulation/image_processing_simulation.cpp](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/simulation/image_processing_simulation.cpp) | 仿真后端，提供 `send_convolution` 的字节打包实现，用来对照硬件侧的解包逻辑。 |

---

## 4. 核心概念与源码讲解

### 4.1 卷积的数学原理与硬件实现思路

#### 4.1.1 概念说明

3x3 图像卷积做的事可以这样直觉地理解：拿一个 3 行 3 列的系数小窗口（核）贴在图像每个像素上，窗口中心对准当前像素，把窗口盖住的 9 个像素分别乘上核里对应位置的 9 个系数，再加起来，就得到输出图像在该位置的一个像素值。

用公式表达，设核为 \(K\)（3x3）、输入图为 \(I\)、输出图为 \(O\)，则对像素 \((x,y)\)：

\[
O(x,y) \;=\; \sum_{i=-1}^{1}\sum_{j=-1}^{1} K(i+1,\;j+1)\;\cdot\; I(x+j,\;y+i)
\]

不同核做不同的事：

- **模糊**：核的系数全为正且和为 1（如高斯核），把每个像素与邻居加权平均，削弱高频细节。
- **边缘检测**：核的系数有正有负、和接近 0，响应图像里「亮度变化剧烈」的位置。
- **锐化**：中心系数大、四邻为负，放大中心与邻居的差异。

要把这个数学过程搬到 FPGA 上，必须回答两个问题：

1. **3x3 邻域从哪来？** 单端口 RAM 一次只能读一个字（本项目里一个 16 位字打包 2 个像素），凑齐 9 个像素需要连续多拍读取并缓存前几行——这正是「行缓冲」要解决的（细节留待 u5-l2）。
2. **9 次乘加怎么做？** FPGA 里一个时钟沿做一次乘法比较稳妥，所以把 9 次乘加拆成连续若干拍，用一个累加器（accumulator）逐拍累加。这就是后面要讲的「9 拍累加」。

#### 4.1.2 核心流程

从高层看，对输出图的每一个像素，硬件要做：

1. **取邻域**：围绕当前像素取 3x3 共 9 个输入像素（边界外按 0 处理）。
2. **乘加**：把 9 个像素分别乘上核的 9 个系数，累加成一个和。
3. **还原 + 钳位**：因为核用定点格式（系数被放大了 16 倍），累加和也要除以 16 还原到像素尺度，再钳位到 \([0,255]\)。
4. **写回**：把结果写进 storage 缓冲对应位置；若 `add_to_result` 打开，则先读出原值再相加后写回。

由于本项目每个 16 位存储字打包 2 个像素，硬件每读一次 RAM 实际拿到 2 个相邻像素，于是卷积被设计成「一次处理左右相邻的两个像素」，这也是后面 CALCULATION 状态里同时维护 `calc_left_buf` 和 `calc_right_buf` 两个累加器的原因。

#### 4.1.3 源码精读

卷积核在硬件里存放在一个 9 元素数组里，声明如下：

- [hdl/image_processing.v:145](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L145)：`reg [15:0] convolution_matrix [0:8]; //3x3 matrix` —— 9 个 16 位槽位，存 3x3 核。注意是 16 位，因为 8 位的核系数在加载时被**符号扩展**成 16 位（负系数才不会出错，详见 4.2.3）。

真正的 9 次乘加发生在 `STATE_PROC_CONVOLUTION_CALCULATION` 状态里。左像素的累加片段（节选）：

- [hdl/image_processing.v:684-694](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L684-L694)：用 `matrix_convolution_counter` 当节拍器，逐拍执行 `convolution_matrix[k]*{8'b0, convolution_buffer_local[...][...]}` 并累加进 `calc_left_buf`。这正是上面公式的硬件化：`convolution_matrix[k]` 是核系数，`convolution_buffer_local` 是当前 3x3 邻域里的某个像素。

还原与钳位用到的函数：

- [hdl/image_processing.v:166-178](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L166-L178)：`apply_clamp_fixed16`。它取 `in[11:4]`，相当于把累加和右移 4 位（除以 16），把被定点核放大的 16 倍还原回像素尺度；钳位判断则用更宽的 `in[15:4]` 做有符号比较，确保越界（>255 或 <0）时正确饱和。这一函数和 u4-l2 里乘法用的是同一个，因为「乘法」与「卷积」本质上都把结果放大了 16 倍。

> 行缓冲（`convolution_buffer`、`convolution_buffer_local`、`convolution_previous_read`）的细节是 u5-l2 的主题，本讲只需知道它们的职责是「把多拍读入的像素凑成 3x3 邻域，放进 `convolution_buffer_local` 供 CALCULATION 使用」。

#### 4.1.4 代码实践

**实践目标**：用手算建立「核系数定点值 ↔ 实数值」的直觉，并验证一次完整卷积。

**操作步骤**：

1. 打开 [software/main.cpp:93-113](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L93-L113) 的 `test_gaussian_blur`，看它的核：

   ```cpp
   uint8_t conv_kernel[9] = {(1)<<0, (1)<<1, (1)<<0, (1)<<1, ((1)<<2), (1)<<1,
                             (1)<<0, (1)<<1, (1)<<0};
   ```

2. 把这 9 个定点字节换算成实数（每个值除以 16）：

   | 字节值 | 实数 |
   | --- | --- |
   | (1)<<0 = 1 | 0.0625 |
   | (1)<<1 = 2 | 0.125 |
   | (1)<<2 = 4 | 0.25 |

   排成 3x3 即：

   ```
   0.0625  0.125  0.0625
   0.125   0.25   0.125
   0.0625  0.125  0.0625
   ```

3. 验证这 9 个实数之和 = 1.0（即字节和 = 16）。这意味着它是一个**归一化模糊核**：对一片恒定灰度区域，卷积输出等于原灰度（不被放大或变暗）。

4. 手算一个例子：假设 3x3 邻域全是 200，核用上面的高斯核。则输出 = 200 × (9 个系数之和) = 200 × 1.0 = 200。硬件侧则先算 200 × 16（系数和的定点值）= 3200，再经 `apply_clamp_fixed16` 取 `[11:4]` 得 3200/16 = 200。两条路径吻合。

**需要观察的现象 / 预期结果**：手算的「先放大 16 倍累加，再除以 16」与硬件的 `apply_clamp_fixed16` 行为一致；归一化核对匀强区域不改变亮度。

> 说明：这是源码阅读 + 手算型实践，不需要运行硬件。若要在仿真里验证，可参考 4.4.4 的运行实践。

#### 4.1.5 小练习与答案

**练习 1**：把高斯核改成中心为 `(1)<<4`（=1.0）、其余 8 个为 0，对匀强区域（全 200）卷积结果是多少？这相当于什么运算？

**答案**：中心 1.0、其余 0 是「恒等核」，输出 = 200 × 1.0 = 200，相当于原样复制图像。

**练习 2**：如果一个核的 9 个定点字节之和为 32（实数和 2.0），对匀强区域 200 卷积、且 `clamp=true`，输出是多少？

**答案**：累加和 = 200 × 32 = 6400；`apply_clamp_fixed16` 取 `[11:4]` 得 6400/16 = 400；再经 `apply_clamp` 钳位到 255。所以输出 = 255（被饱和）。

---

### 4.2 命令参数解析：STATE_CONVOLUTION_READ_PARAM

#### 4.2.1 概念说明

主机调用 `send_convolution(kernel, clamp, input_source, add_to_output)` 触发一次卷积。除了 9 个核系数，还要告诉硬件 3 个布尔开关：

| 参数位 | 名称（硬件寄存器） | 含义 |
| --- | --- | --- |
| bit0 | `clamp` | 结果是否钳位到 \([0,255]\) |
| bit1 | `convolution_source_input` | 源图像从哪个缓冲读：1 = input，0 = storage |
| bit2 | `convolution_add_to_result` | 写回时是否把卷积结果叠加到 storage 原值上 |

这三个布尔被打包进**同一个字节**（位打包技巧，详见 u2-l2），紧跟着 9 个核系数字节一起发送。整个命令报文共 1 字节操作码 + 1 字节参数位 + 9 字节核 = 主机送出 11 字节（操作码之后 10 字节参数）。

#### 4.2.2 核心流程

主机侧（仿真与 iCE40 两套后端用完全相同的表达式）打包顺序：

1. 发操作码 `COMMAND_CONVOLUTION`。
2. 发参数字节：`(add_to_output<<2) + (input_source<<1) + clamp`。
3. 依次发 9 个核字节 `kernel[0..8]`。

硬件侧 `STATE_CONVOLUTION_READ_PARAM` 的解析顺序：

1. `counter_read` 初值 9。第 1 个字节（`counter_read==9`）是参数字节：拆出 `clamp`（bit0）、`convolution_source_input`（bit1）、`convolution_add_to_result`（bit2），`counter_read` 减到 8。
2. 接下来 9 个字节（`counter_read` 从 8 递减到 0）是核系数：每个 8 位字节被**符号扩展**成 16 位，写入 `convolution_matrix[8-counter_read]`。所以 `counter_read==8` 时写 `matrix[0]`，`counter_read==0` 时写 `matrix[8]`。
3. 读到最后一个字节（`counter_read==0`）时，一次性交接：把 `state_processing` 置为 `STATE_PROC_CONVOLUTION`、各卷积计数器清零、根据 `source_input` 选定读地址基址、写地址基址固定为 storage，然后主 FSM 回到 `STATE_WAIT_COMMAND`，由运算 FSM 接管。

#### 4.2.3 源码精读

命令派发入口，预装计数器：

- [hdl/image_processing.v:270-273](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L270-L273)：`COMMAND_CONVOLUTION` 分支把 `state` 设为 `STATE_CONVOLUTION_READ_PARAM`，`counter_read <= 9`，注释写「will read 10 params」（1 字节参数位 + 9 字节核）。

参数解析与符号扩展：

- [hdl/image_processing.v:431-462](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L431-L462)：`STATE_CONVOLUTION_READ_PARAM` 主体。
  - `counter_read==9` 分支：拆解参数字节 `convolution_params <= comm_data_in; clamp <= comm_data_in[0]; convolution_source_input <= comm_data_in[1]; convolution_add_to_result <= comm_data_in[2];`。
  - `else` 分支（核系数）：`convolution_matrix[8-counter_read] <= { {8{comm_data_in[7]}}, comm_data_in};`。这里的 `{8{comm_data_in[7]}}` 是**符号扩展**——把最高位（符号位）复制 8 份填到高 8 位。例如字节 `0xF8`（= -8）扩展后变成 `0xFFF8`（16 位下的 -8），数值不变但宽度变成 16 位，后续乘法才能正确处理负系数。
  - `counter_read==0` 时的交接：根据 `convolution_source_input` 选 `proc_conv_memory_addr_read <= buffer_input_address`（从 input 读）或 `buffer_storage_address`（从 storage 读）；写地址 `proc_conv_memory_addr_write <= buffer_storage_address`（结果恒写回 storage）。

主机侧打包（两套后端逐字节一致）：

- [simulation/image_processing_simulation.cpp:203-215](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/simulation/image_processing_simulation.cpp#L203-L215)：仿真后端 `send_convolution`。参数字节用 `(add_to_output<<2)+(input_source<<1)+clamp`，对应硬件的 bit2/bit1/bit0。
- [ice40/software/image_processing_ice40.cpp:200-207](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/software/image_processing_ice40.cpp#L200-L207)：iCE40 后端 `send_convolution`，打包表达式与仿真后端**完全相同**，差别仅在于用 SPI 事务（`SPI_SEND_CMD`/`SPI_SEND_DATA`）发送而非 FIFO 入队。

接口与操作码契约：

- [software/image_processing.hpp:33](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/image_processing.hpp#L33)：纯虚 `send_convolution(uint8_t *kernel, bool clamp, bool input_source, bool add_to_output)`。
- [software/image_processing.hpp:4-6](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/image_processing.hpp#L4-L6)：`Commands` 枚举里 `COMMAND_CONVOLUTION` 的数值与 HDL `parameter` 逐字对应（四方契约，详见 u2-l1）。

#### 4.2.4 代码实践（本讲核心实践任务）

**实践目标**：把「主机打包 → 硬件解包 → 决定读写地址 → 叠加写回」整条参数链走一遍，验证自己对三个参数位的理解。

**操作步骤**：

1. **符号扩展追踪**。读 [hdl/image_processing.v:439-443](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L439-L443)。以 `test_simple_edge_detection` 里的「top gradient」核为例（[software/main.cpp:122](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L122)）：

   ```cpp
   uint8_t conv_kernel[9] = {(1)<<3, (1)<<4, (1)<<3, (0)<<4, ((0)<<4), (0)<<4,
                             (-1)<<3, (-1)<<4, (-1)<<3};
   ```

   - 前三个正系数 `(1)<<3=8`、`(1)<<4=16`、`(1)<<3=8`：符号位为 0，扩展后仍是 `0x0008`、`0x0010`、`0x0008`。
   - 后三个负系数 `(-1)<<3=-8`、`(-1)<<4=-16`、`(-1)<<3=-8`：作为 `uint8_t` 分别是 `0xF8`、`0xF0`、`0xF8`，符号位（bit7）为 1，扩展后变成 `0xFFF8`、`0xFFF0`、`0xFFF8`，即 16 位下的 -8、-16、-8。数值保持，符号正确。

2. **`source_input` 决定读地址**。读 [hdl/image_processing.v:453-458](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L453-L458)：
   - `convolution_source_input==1` → `proc_conv_memory_addr_read <= buffer_input_address`：从 input 缓冲读源图像。
   - 否则 → `proc_conv_memory_addr_read <= buffer_storage_address`：从 storage 读（用于「在已有结果上继续卷积」的场景）。
   - 写地址 `proc_conv_memory_addr_write <= buffer_storage_address` 恒为 storage——卷积结果总是写进 storage。

   对照 `test_gaussian_blur`（[software/main.cpp:102](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L102)）调用 `send_convolution(conv_kernel, true, true, false)`：`input_source=true`，所以从 input 读、storage 写，符合「图在 input、结果写到 storage」的标准用法。

3. **`add_to_result` 决定是否叠加**。读 [hdl/image_processing.v:633-649](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L633-L649)：
   - 当 `convolution_add_to_result==1` 时，状态机在卷积前先用两拍（`convolution_reading_data` 从 `2'b00`→`2'b01`→`2'b10`）把 storage 写地址处的**原值**读进 `convolution_data_to_add`。
   - 当 `add_to_result==0` 时，直接 `convolution_data_to_add <= 16'b0`，不读原值。
   - 随后在 CALCULATION 里（[hdl/image_processing.v:725](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L725)），写回值 = `apply_clamp(convolution_data_to_add[7:0] + temp_calc[7:0], 1)`，即「原值 + 卷积结果」再钳位。这就是叠加写回的实现。

**需要观察的现象 / 预期结果**：你应当能画出一张表，对 `test_gaussian_blur` 和 `test_simple_edge_detection` 的每一次 `send_convolution` 调用，标注出 `clamp/source_input/add_to_result` 三个位分别是 0 还是 1，以及它们导致硬件「从哪读、往哪写、是否叠加」。

> 注意：本实践为源码阅读 + 推理型，不需要运行硬件。第 4.4.4 节提供可运行的仿真验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么核系数必须符号扩展成 16 位，而不能直接当 8 位无符号数用？

**答案**：边缘检测等核含负系数（如 -1、-8）。若按 8 位无符号处理，`0xF8` 会被当成 +248 而非 -8，卷积结果完全错误。符号扩展成 16 位后，`0xFFF8` 在有符号语义下仍是 -8，后续乘加和钳位（`apply_clamp_fixed16` 用 `$signed`）才能得到正确结果。

**练习 2**：`counter_read` 初值是 9，但实际读了 10 个参数字节，为什么？

**答案**：第 1 个字节在 `counter_read==9` 时被当作参数字节单独处理（并把 `counter_read` 减到 8），随后 `counter_read` 从 8 递减到 0 共 9 拍读取 9 个核字节。所以「初值 9」对应「1 个参数字节 + 9 个核字节 = 10 字节」。`counter_read` 的语义是「按分支自定义的倒计数器」，不是「剩余字节数」（详见 u3-l4）。

---

### 4.3 卷积三段状态机总览：读 → 算 → 写回

#### 4.3.1 概念说明

卷积运算 FSM（`state_processing`）由 4 个状态构成一个循环，对应「读 → 算 → 写回」三段（写回拆成 2 个状态，原因见 4.3.3）：

| 状态 | 阶段 | 职责 |
| --- | --- | --- |
| `STATE_PROC_CONVOLUTION` | 读 + 预取 | （若需要）读 storage 原值进 `convolution_data_to_add`；读输入像素；把行缓冲里的历史行 + 当前读凑成 3x3 邻域放进 `convolution_buffer_local`，然后进入计算。 |
| `STATE_PROC_CONVOLUTION_CALCULATION` | 算 | 用 `matrix_convolution_counter` 当节拍器，分拍做 9 次乘加，左右两像素并行累加进 `calc_left_buf`/`calc_right_buf`；最后还原、钳位。 |
| `STATE_PROC_CONVOLUTION_WRITEBACK_1` | 写回（第 1 拍） | 把上一轮缓存的像素写回行缓冲（推断 SPRAM 的第一步）。 |
| `STATE_PROC_CONVOLUTION_WRITEBACK_2` | 写回（第 2 拍） | 更新 `convolution_previous_read` 移位寄存器；行缓冲第二步写入；管理 x/y 计数器与读写地址偏移；决定是否真正置 `wr_en`；回到 `STATE_PROC_CONVOLUTION` 进入下一像素，或在扫完整图后回到 `STATE_IDLE`。 |

为什么要拆成这么多段？根本约束是**单端口 RAM 一拍只能访存一次、且读数据延迟一拍**。卷积对一个输出像素要：读 9 个输入邻域（跨多拍、跨行）→ 算 9 次乘加（一拍一次）→ 写回 1 个结果，再加上「把刚读到的像素存进行缓冲供后续像素复用」。这些访存与计算无法挤进一个状态，只能用时序拆开。

#### 4.3.2 核心流程

每处理一对左右相邻像素，运算 FSM 走一遍这样的循环：

```
STATE_PROC_CONVOLUTION （读 + 预取邻域）
        │  （若 add_to_result：先两拍读 storage 原值）
        │  读输入字（2 像素）→ data_read_valid
        │  组装 convolution_buffer_local（4x3 邻域）
        ▼
STATE_PROC_CONVOLUTION_CALCULATION （9 拍累加）
        │  matrix_convolution_counter: 0→9
        │  每拍算一次乘加，左/右累加器并行
        │  counter==9 时还原+钳位，得到 data_write[7:0]/[15:8]
        ▼
STATE_PROC_CONVOLUTION_WRITEBACK_1 （行缓冲写第 1 步）
        ▼
STATE_PROC_CONVOLUTION_WRITEBACK_2 （行缓冲写第 2 步 + 结果写回 storage）
        │  x += 2（一次处理 2 像素）；行末换行
        │  首行不写回（等行缓冲填满）
        │  整图扫完 → STATE_IDLE；否则 → STATE_PROC_CONVOLUTION
```

边界处理在 CALCULATION 里完成（[hdl/image_processing.v:678](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L678) 和 [hdl/image_processing.v:729](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L729)）：图像上/下/左/右边缘的输出像素直接置 0，避免读越界邻域。

#### 4.3.3 源码精读

状态枚举定义（顺序递增的 parameter）：

- [hdl/image_processing.v:39-43](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L39-L43)：`STATE_CONVOLUTION_READ_PARAM`、`STATE_PROC_CONVOLUTION`、`STATE_PROC_CONVOLUTION_CALCULATION`、`STATE_PROC_CONVOLUTION_WRITEBACK_1`、`STATE_PROC_CONVOLUTION_WRITEBACK_2` 五个状态在此声明。

读 + 预取状态：

- [hdl/image_processing.v:630-673](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L630-L673)：`STATE_PROC_CONVOLUTION`。本讲只需把握三件事：(1) `add_to_result` 开启时先用 `convolution_reading_data` 两拍读出 storage 原值；(2) 用 `proc_conv_memory_addr_read[0]` 区分「发起读」与「数据有效后组装邻域」两拍；(3) 组装完邻域后 `matrix_convolution_counter<=0` 并跳到 CALCULATION。邻域组装（`convolution_buffer_local` 的填充）涉及行缓冲位移，留待 u5-l2。

9 拍累加状态：

- [hdl/image_processing.v:674-781](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L674-L781)：`STATE_PROC_CONVOLUTION_CALCULATION`。`matrix_convolution_counter` 从 0 递增到 9（共 10 个时钟周期）：counter 0–8 完成 9 次乘加，counter==9 时取回最终累加值。左像素（`calc_left_buf`）从 counter 0 开始，右像素（`calc_right_buf`）从 counter 1 开始——两者错开一拍，从而用同一个 4 列邻域算出左右两个相邻像素（左用列 0/1/2，右用列 1/2/3）。最后 `apply_clamp_fixed16` 还原尺度、`apply_clamp` 叠加 `convolution_data_to_add` 并钳位。counter==9 时跳到 WRITEBACK_1。

写回两个状态：

- [hdl/image_processing.v:782-787](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L782-L787)：`STATE_PROC_CONVOLUTION_WRITEBACK_1`。注释明确说「done in two states to infer a spram for the convolution buffer」——`convolution_buffer` 是大数组（256×2 字节），综合器只有把它推断成 SPRAM 才放得下；而一个时钟沿对一个 SPRAM 只能写一次，所以分两拍写两个字节。
- [hdl/image_processing.v:788-839](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L788-L839)：`STATE_PROC_CONVOLUTION_WRITEBACK_2`。完成第二拍行缓冲写入、更新 `convolution_previous_read` 移位寄存器、推进 x/y 计数器；关键细节：**首行不写回**（`counter_convolution_y==0` 或首拍时 `wr_en<=0`，[hdl/image_processing.v:809-811](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L809-L811)），因为行缓冲还没攒够前几行、此时算出的卷积没有意义；扫完整图（`counter_convolution_y >= img_height+1`）回到 `STATE_IDLE`，否则回到 `STATE_PROC_CONVOLUTION` 处理下一对像素。

#### 4.3.4 代码实践

**实践目标**：跟踪一对左右像素在四个状态里的旅程，建立「整段流水」的整体感。

**操作步骤**：

1. 在 [hdl/image_processing.v:630](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L630) 起的 `STATE_PROC_CONVOLUTION` 里，找到 `data_read_valid == 1` 分支（[hdl/image_processing.v:650-672](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L650-L672)）。注意它做了三件事：把读到的字存进 `data_read_store`、`matrix_convolution_counter<=0`、`state_processing<=STATE_PROC_CONVOLUTION_CALCULATION`。
2. 跟到 CALCULATION（[hdl/image_processing.v:674](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L674)），数一数 `matrix_convolution_counter` 从 0 到 9 共 10 拍；确认 counter==9 时 `state_processing<=STATE_PROC_CONVOLUTION_WRITEBACK_1`（[hdl/image_processing.v:776-780](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L776-L780)）。
3. 跟到 WRITEBACK_2（[hdl/image_processing.v:834-838](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L834-L838)），确认未到结束条件时 `state_processing<=STATE_PROC_CONVOLUTION`，形成循环。

**需要观察的现象 / 预期结果**：你能口述出「读 1 拍（不计 add_to_result 的预读）→ 算 10 拍 → 写回 2 拍 ≈ 每对像素约 13 拍」的节拍感；并理解首行像素不写回、边缘像素置 0 这两个特例。

> 待本地验证：在 Verilator 仿真里对一张 64×64 图跑 `test_gaussian_blur`，从打印的 `read req addr` / `wants to write` 日志（[simulation/image_processing_simulation.cpp:256-262](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/simulation/image_processing_simulation.cpp#L256-L262)）里数一数每写一个字之前大约发生了多少次读，验证上面的节拍估算。

#### 4.3.5 小练习与答案

**练习 1**：为什么 WRITEBACK 要拆成 `_1` 和 `_2` 两个状态，而不是一个状态里写两拍？

**答案**：`convolution_buffer` 是 256×2 字节的大数组，综合时必须推断成片上 SPRAM 才能放下；而一个时钟沿对同一块 SPRAM 只能做一次写操作。要把「上一轮缓存的两个像素字节」都写回行缓冲，只能分两个状态、各自占一个时钟沿各写一次（注释 [hdl/image_processing.v:784](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L784) 明确说明了这一点）。

**练习 2**：CALCULATION 里 `matrix_convolution_counter` 经历的值是 0,1,2,...,9，共 10 个周期，为什么本讲说它是「9 拍累加」？

**答案**：左像素在 counter 0–8 共 9 拍里完成 9 次乘加（counter 0 算第 1 次、counter 8 算第 9 次），counter==9 那拍只是取回累加器终值（`temp_calc = calc_left_buf`）并做还原/钳位，没有新的乘加。所以「9 次乘加分布在 10 个时钟周期」。「9 拍累加」是按乘加次数说的，不是按总周期数说的。

---

### 4.4 add_to_result 与多核累加：边缘检测的实现

#### 4.4.1 概念说明

`add_to_result`（硬件寄存器 `convolution_add_to_result`，主机参数 `add_to_output`）是一个一比特开关，决定卷积结果是「覆盖」还是「叠加」写回 storage：

- `add_to_result == 0`：写回值 = 卷积结果（忽略 storage 原值）。
- `add_to_result == 1`：写回值 = storage 原值 + 卷积结果（再钳位）。

这个开关让多条卷积命令可以**串联累加**到同一张图上，是「多核边缘检测」的关键。例如对同一源图依次用「上、下、左、右」四个方向梯度核做卷积，每个方向响应一类边缘，四个结果累加起来就是一张综合边缘图——这正是 `test_simple_edge_detection` 的做法。

值得注意的细节：CALCULATION 里把叠加和钳位写成了 `apply_clamp(convolution_data_to_add + temp_calc, 1)`（[hdl/image_processing.v:725](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L725)），钳位始终开启。因此每一步累加只要出现负值就会被钳到 0——这相当于对每个方向的梯度响应做了一次「负半轴清零」，多个方向的正响应再相加，最终得到边缘强度图。

#### 4.4.2 核心流程

`test_simple_edge_detection` 的流程（[software/main.cpp:116-151](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L116-L151)）：

1. `send_params` + `send_image`：图进 input 缓冲。
2. 第 1 个核（top gradient）：`send_convolution(kernel, true, true, false)` —— `add_to_output=false`，把第一个方向的结果**覆盖**写进 storage。
3. `wait_end_busy`：等这次卷积跑完。
4. 第 2、3、4 个核（bottom / left / right gradient）：每次 `send_convolution(kernel, true, true, true)` —— `add_to_output=true`，把结果**叠加**到 storage 已有内容上。每次后都 `wait_end_busy`。
5. `switch_buffers` + `read_image`：把 storage 里的累加结果切回 input 侧读出。

注意第 2 步以后，源图像仍从 input 读（`source_input=true` 不变），但写回时叠加的是 storage 里**上一轮卷积的累计结果**。所以「源图不变、结果层层叠加」。

#### 4.4.3 源码精读

主机侧四次调用：

- [software/main.cpp:121-146](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L121-L146)：四个方向梯度核。第 1 个 `add_to_output=false`（[main.cpp:123](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L123)），后三个 `add_to_output=true`（[main.cpp:130](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L130)、[main.cpp:137](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L137)、[main.cpp:144](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L144)）。

硬件侧「读原值」机制：

- [hdl/image_processing.v:633-641](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L633-L641)：`add_to_result==1` 时，`convolution_reading_data` 走 `2'b00`（发读）→ `2'b01`（收 `data_read_valid`，存进 `convolution_data_to_add`）→ `2'b10`（完成），把 storage 写地址处的原值取出来备用。
- [hdl/image_processing.v:647-649](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L647-L649)：`add_to_result==0` 时，直接 `convolution_data_to_add <= 16'b0`，不读原值。

硬件侧「叠加写回」：

- [hdl/image_processing.v:723-725](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L723-L725)：左像素写回 `data_write[7:0] <= apply_clamp({8'b0, convolution_data_to_add[7:0]} + {8'b0, temp_calc[7:0]}, 1);`。无论 `add_to_result` 是 0 还是 1，用的都是同一条「原值 + 卷积结果」公式——区别只在于 `convolution_data_to_add` 是 0（覆盖）还是真实原值（叠加）。右像素同理（[hdl/image_processing.v:773](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L773)）。

#### 4.4.4 代码实践（可运行）

**实践目标**：在仿真模式下跑通边缘检测，把本讲的参数位、累加、钳位串起来看实际效果。

**操作步骤**：

1. 确认 [software/main.cpp:256](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L256) 当前激活的是 `test_simple_edge_detection`（其他 `test_*` 已注释）。
2. 按 u1-l3 介绍的方式构建并运行仿真：执行 `build_simulation.sh`，再运行生成的 `obj_dir/Vimage_processing`，它会写出 `output.dat`。
3. 用 `run_gnuplot.sh` 把 `output.dat` 渲染成灰度图查看。
4. 改为把 `test_simple_edge_detection` 注释、启用 `test_gaussian_blur`（[main.cpp:255](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L255)），重复构建运行，对比两张输出图。

**需要观察的现象 / 预期结果**：

- 高斯模糊：输出图比原图更平滑、细节减弱，但整体亮度接近（因为核归一化和为 1.0）。
- 边缘检测：输出图在物体轮廓处出现亮线、平坦区域接近 0；由于 4 个方向叠加，响应较强的边缘会偏亮甚至饱和到 255。

> 待本地验证：上述视觉结果取决于测试图 `image_fruits_64.h` 的内容。若手头没有 Verilator/gnuplot 环境，可降级为「源码阅读型实践」：对照 4.4.2 的流程，在纸上列出 4 次 `send_convolution` 的参数位表，并预测平坦区域（邻域全相同）的输出——四个方向梯度核在平坦区域响应都为 0，叠加后仍为 0，与「平坦区接近 0」的预期一致。

#### 4.4.5 小练习与答案

**练习 1**：如果四个方向梯度核都用 `add_to_output=false`，最终 `read_image` 会得到什么？

**答案**：每次都覆盖写回，storage 里只会保留**最后一次**（right gradient）的卷积结果，前三方向的结果全部丢失。边缘图只反映单一方向的梯度。

**练习 2**：`add_to_result` 的「读原值」用的是 `proc_conv_memory_addr_write`（写地址），而不是读地址，为什么？

**答案**：因为要把卷积结果**叠加到即将写回的同一个 storage 单元**上。读地址指向源图（input 或 storage 的源区），写地址指向结果区（storage 的结果区）。要叠加，必须读出「结果区当前值」，也就是写地址处的值，所以预读用 `proc_conv_memory_addr_write`（见 [hdl/image_processing.v:635-636](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L635-L636)）。

---

## 5. 综合实践

把本讲全部内容串起来，完成下面这个「纸面 + 可选上机」的小任务。

**任务**：设计一个 3x3 **锐化核**（sharpen），中心系数 +5、四邻（上下左右）-1、四角 0，并用本讲学到的规则把它送进硬件。

**要求**：

1. **用定点字节写出核**。提示：5.0 = `(5)<<4` = 80，-1.0 = `(-1)<<4` = -16（作为 `uint8_t` 是 `0xF0`）。按 `convolution_matrix[0..8]` 的填充顺序（行优先）列出 9 个字节。
2. **写出 `send_convolution` 的参数字节**。假设你想「从 input 读、覆盖写回、结果钳位」，即 `clamp=true, input_source=true, add_to_output=false`，计算 `(add_to_output<<2)+(input_source<<1)+clamp` 的值。
3. **预测行为**。这个核的系数和 = 5 + 4×(-1) = 1（定点和 = 16），所以对匀强区域不改变亮度；对有边缘的地方会放大中心与邻居的差异，起到锐化效果。验证你的定点核和确实是 16。
4. **（可选上机）** 仿照 `test_gaussian_blur`（[software/main.cpp:93-113](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L93-L113)）在 `main.cpp` 里加一个 `test_sharpen`，构造上面的核、调用 `send_convolution(kernel, true, true, false)`、`wait_end_busy`、`switch_buffers`、`read_image`，在仿真模式下用 `run_gnuplot.sh` 查看锐化效果。

> 说明：第 1–3 步是纸面推导，是本讲的核心检验；第 4 步涉及修改 `main.cpp`（属于 u5-l3 的拓展实践范围），仅在你已掌握仿真构建流程时尝试。本讲不要求你修改源码。

**参考答案**：

1. 行优先 9 字节（定点值 / 字节）：`0, 0xF0, 0, 0xF0, 80, 0xF0, 0, 0xF0, 0`。即 `{(0)<<4, (-1)<<4, (0)<<4, (-1)<<4, (5)<<4, (-1)<<4, (0)<<4, (-1)<<4, (0)<<4}`。
2. 参数字节 = `(0<<2)+(1<<1)+1` = `0b00000111` = `0x07` = 7。
3. 定点和 = 0 + (-16) + 0 + (-16) + 80 + (-16) + 0 + (-16) + 0 = 16 → 实数和 1.0 ✓。

---

## 6. 本讲小结

- 3x3 卷积 = 用 9 个系数对 3x3 邻域做加权求和；本项目因单端口 RAM 与定点约束，把它拆成「读 → 9 拍乘加 → 写回」三段状态机。
- 卷积命令的三个参数位 `clamp`（bit0）/ `source_input`（bit1）/ `add_to_result`（bit2）被打包进一个字节，主机两套后端用相同表达式打包，硬件用 `comm_data_in[0/1/2]` 解包——四方契约一致。
- 卷积核用 1.3.4 定点格式（字节值 = 实数 × 16），加载时被符号扩展成 16 位以支持负系数；结果用 `apply_clamp_fixed16`（取 `[11:4]`）还原尺度并钳位。
- 运算 FSM 四状态循环：`STATE_PROC_CONVOLUTION`（读+预取邻域）→ `STATE_PROC_CONVOLUTION_CALCULATION`（counter 0→9，9 次乘加、左右两像素并行）→ `WRITEBACK_1` → `WRITEBACK_2`（行缓冲写入 + 结果写回，写回拆两拍是为了把行缓冲推断成 SPRAM）。
- `add_to_result=1` 时，硬件先读出 storage 写地址处的原值进 `convolution_data_to_add`，再在写回时执行「原值 + 卷积结果」——这就是多核累加的基础，`test_simple_edge_detection` 用它把四个方向梯度核的响应叠加成边缘图。

---

## 7. 下一步学习建议

本讲只搭起了卷积的「骨架」与参数契约，刻意避开了行缓冲的内部机制。下一篇 **u5-l2 行缓冲与卷积计算流水线** 会专门拆解：

- `convolution_buffer`（双行缓冲）与 `convolution_previous_read`（暂存上一次读取）如何凑齐 3x3 邻域；
- 为什么需要 4 列的 `convolution_buffer_local`（因为一次处理左右两像素）；
- 读地址与写地址（`counter_convolution_x/y` 与 `_write`）之间的偏移是怎么来的、边界像素为何置 0。

之后 **u5-l3 卷积核实践** 会回到主机侧，结合 `test_gaussian_blur` 与 `test_simple_edge_detection` 做更多核的构造与上机实验。建议在进入 u5-l2 前，先把本讲的「三段状态机 + 三个参数位」吃透——后续讲义默认你已经能口述这条主线。
