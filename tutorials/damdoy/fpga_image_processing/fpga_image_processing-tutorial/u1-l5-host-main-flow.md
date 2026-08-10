# 主机程序入口与测试流程

## 1. 本讲目标

本讲是「认识项目」单元的最后一篇。前面几讲已经让你知道项目是什么（u1-l1）、目录怎么分（u1-l2）、怎么构建运行仿真（u1-l3）和硬件（u1-l4）。本讲把镜头拉近到**主机程序本身**——也就是 `software/main.cpp` 这个唯一被两套后端共享的 C++ 入口。

读完本讲，你应该能够：

- 说清楚 `main()` 从打开 `output.dat` 到关闭它的**端到端调用顺序**；
- 理解一张 GIMP 导出的 `.h` 头图像如何被 `HEADER_PIXEL` 宏**解码成灰度像素数组**；
- 看懂 `send_params` / `send_image` / `switch_buffers` / `wait_end_busy` / `read_image` 这些接口是如何**串起一次完整运算**的；
- 读懂 `test_*` 系列测试函数的通用套路，并能照葫芦画瓢地读懂或改写其中一个。

本讲只讲主机侧的「调用编排」，不深入硬件 FSM 的内部细节——那是第 3、4 单元的任务。我们关心的是：**主机按下哪些按钮，硬件就会做哪些事**。

## 2. 前置知识

在进入源码前，先用通俗语言铺垫几个概念。

**预处理宏 `#ifdef` 与多态后端。** C++ 编译器在真正编译前会先跑一遍「预处理器」。`#ifdef SIMULATION ... #elif ICE40 ... #endif` 是一种条件编译：根据编译时是否定义了 `SIMULATION` 或 `ICE40` 宏，保留不同的代码块。本项目正是用这个机制让**同一份 `main.cpp`** 编译出两种可执行文件——仿真版（u1-l3）和硬件版（u1-l4）。

**纯虚基类与多态。** `main.cpp` 里所有 test 函数都拿一个 `Image_processing *img_proc` 指针当参数，调用 `img_proc->send_image(...)` 之类的方法。这个指针指向一个**纯虚基类**，仿真后端和硬件后端都是它的子类。于是 test 函数的代码**完全不关心**底下到底是 Verilator 仿真还是真实 FPGA——它只管「按契约发命令」。这正是前面讲义反复强调的「统一契约」在代码里的具体落地。

**RGB 图与单通道灰度图。** 一张彩色图的每个像素由 R、G、B 三个字节组成。本项目只处理**单通道灰度图**（每个像素一个 0~255 的字节）。本讲会看到主机把 RGB 三字节解出来后，**只取其中一个通道**当作灰度值送进硬件。

**gnuplot matrix 格式（回顾 u1-l3）。** `output.dat` 是一个纯文本矩阵：每行写 `image_width` 个数字、用空格分隔，行末换行。`run_gnuplot.sh` 用 `plot ... matrix w image` 把它渲染成灰度图。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| `software/main.cpp` | 主机程序唯一入口：解码图像、选后端、调用 test、写输出。本讲绝对主角。 |
| `software/images/image_fruits_8.h` | GIMP 导出的 C 头图像（8×8），用来讲解 `.h` 图像格式与 `HEADER_PIXEL` 宏。 |
| `software/image_processing.hpp` | 定义纯虚基类 `Image_processing` 与 `Commands` 枚举，是 test 函数所用接口的契约来源（u2-l1 会详讲）。 |
| `simulation/image_processing_simulation.cpp` | 仿真后端实现，用来佐证 `wait_end_busy` 等「接口在底层到底干了什么」（u6-l1 详讲）。 |

> 说明：`main.cpp` 当前实际 include 的是 `images/image_fruits_64.h`（64×64）。本讲为方便阅读，用更小的 `image_fruits_8.h`（8×8）作为格式示例，两者格式完全一致，只是尺寸不同。

## 4. 核心概念与源码讲解

### 4.1 `main()` 主流程：从 `fopen` 到 `fclose` 的骨架

#### 4.1.1 概念说明

`main()` 是整个主机程序的总指挥。它不亲自做图像运算，而是承担四件事：**开输出文件 → 选后端 → 准备像素数据 → 指派一个 test 去跑 → 把结果落盘**。理解了这个骨架，就理解了「主机程序在干什么」。

#### 4.1.2 核心流程

`main()` 的执行顺序可以归纳成 6 个阶段：

```text
1. fopen("output.dat")           打开结果文件
2. new 一个后端对象              #ifdef SIMULATION→仿真后端  #elif ICE40→硬件后端
3. new 三块像素数组              image_input / image_input2 / image_output
4. HEADER_PIXEL 循环解码 .h 图像  把 header_data 解成灰度像素，填进 image_input
5. 调用某个 test_* 函数          （一次只跑一个，靠注释/取消注释来选）
6. fprintf 循环写 output.dat     把 image_output 按矩阵格式落盘，fclose
```

关键点：**第 5 步一次只激活一个 test**。`main.cpp` 里写了十来个 test 函数，但靠把它们注释掉、只留一行来选择当前要跑哪一个。

#### 4.1.3 源码精读

先看头部：选哪张测试图、include 哪个抽象接口、用宏选哪个后端头文件——这三件事都在文件开头完成。

包含测试图像与抽象接口：[software/main.cpp:5-12](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L5-L12) 中，第 7 行 `#include "images/image_fruits_64.h"` 决定了本程序用哪张图（其余图被注释掉），第 12 行 include 了定义统一契约的 `image_processing.hpp`。

用宏选后端头文件：[software/main.cpp:14-18](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L14-L18) 根据 `SIMULATION` / `ICE40` 宏分别 include 仿真后端或硬件后端的头文件。这正是 u1-l3 讲的「宏→后端」依赖链在源码里的体现。

接着看 `main()` 本体。[software/main.cpp:221-275](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L221-L275) 是完整的主函数。其中：

- 第 222 行 `fopen("output.dat", "w")` 打开结果文件——阶段 1。
- 第 226-230 行用宏 new 出对应后端对象——阶段 2：

```cpp
#ifdef SIMULATION
img_proc = new Image_processing_simulation();
#elif ICE40
img_proc = new Image_processing_ice40();
#endif
```

- 第 232-234 行 new 三块 `image_width*image_height` 大小的像素数组——阶段 3。
- 第 236-241 行用 `HEADER_PIXEL` 把图解码进 `image_input`——阶段 4（下一节细讲）。
- 第 251-260 行是 test 选择区：[software/main.cpp:251-260](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L251-L260) 里大部分 test 被注释，当前只有第 256 行 `test_simple_edge_detection(...)` 是激活的——阶段 5。
- 第 264-269 行把 `image_output` 写进 `output.dat`——阶段 6（4.4 节细讲）。

#### 4.1.4 代码实践

**实践目标**：在不运行程序的前提下，能口头复述 `main()` 的 6 个阶段。

**操作步骤**：

1. 打开 [software/main.cpp:221-275](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L221-L275)。
2. 在源码旁标注每个阶段对应的行号（如「阶段 2 → 226-230」）。
3. 找到 test 选择区，确认当前激活的是哪个 test。

**需要观察的现象**：你会注意到 `image_input2` 虽然被 new 出来了，但当前主流程里第二张图的解码代码（244-249 行）是被注释掉的，所以 `image_input2` 实际未被使用——它只是为「双图运算」类 test 预留的。

**预期结果**：能画出一张「fopen → new 后端 → new 数组 → 解码 → test → fprintf → fclose」的线性流程图。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `#ifdef SIMULATION` 这段（226-230 行）整段删掉，程序还能编译通过吗？为什么？

**参考答案**：不能（在仿真模式下）。删掉后 `img_proc` 这个 `Image_processing *` 指针从未被赋值，仍是未初始化的野指针，后续任何 `img_proc->...` 调用都是未定义行为；并且若没有匹配的 `#elif`，在 `ICE40` 也未定义时编译器还可能警告 `img_proc` 未使用。这段宏是后端切换的唯一入口。

**练习 2**：`main()` 里 `image_input2` 为什么没有在当前主流程中被填充？

**参考答案**：因为第 244-249 行填第二张图的循环被注释掉了，且当前激活的 `test_simple_edge_detection` 只需要一张图。`image_input2` 仅为 `test_images_average` / `test_images_diff` 这类双图 test 预留。

---

### 4.2 图像 `.h` 解析：`HEADER_PIXEL` 宏

#### 4.2.1 概念说明

本项目不读 PNG/JPG，而是直接 include 一个**由 GIMP「导出为 C 源文件」生成的 `.h` 头**。这个头里把每个 RGB 像素的 3 个字节，编码成一串**可打印字符**。`HEADER_PIXEL` 宏的任务就是把这串字符**解包回 RGB 三字节**。理解这个编码，你才能看懂主机是怎么「喂图」给硬件的。

#### 4.2.2 核心流程

编码原理是「每 6 位编成一个字符」：GIMP 把每个字符的 ASCII 值减去 33，得到一个 6 位的值（范围 0~63，对应字符 ASCII 33~96）。

- 单个字符携带 6 位：\(v = c - 33\)，其中 \(v \in [0, 63]\)。
- 4 个字符 = \(4 \times 6 = 24\) 位，正好 \(= 3\) 字节（一组 RGB）。

把这 24 位按顺序排开，再按 8 位切成 3 个字节：

\[
\underbrace{v_0}_{6}\,\underbrace{v_1}_{6}\,\underbrace{v_2}_{6}\,\underbrace{v_3}_{6}
\;\longrightarrow\;
\underbrace{v_0\,v_1^{[\text{高2}]}}_{\text{R}}\,
\underbrace{v_1^{[\text{低4}]}\,v_2^{[\text{高4}]}}_{\text{G}}\,
\underbrace{v_2^{[\text{低2}]}\,v_3}_{\text{B}}
\]

主机拿到 RGB 三字节后，**只取 `pixel[0]`（R 通道）**当作灰度值——一张 RGB 图就这样被降成单通道灰度图。

#### 4.2.3 源码精读

图像头声明尺寸：[software/images/image_fruits_8.h:3-4](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/images/image_fruits_8.h#L3-L4) 定义 `image_width = 8`、`image_height = 8`。这两个全局变量被 `main.cpp` 直接使用。

`HEADER_PIXEL` 宏本体：[software/images/image_fruits_8.h:8-13](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/images/image_fruits_8.h#L8-L13)：

```c
#define HEADER_PIXEL(data,pixel) {\
pixel[0] = (((data[0] - 33) << 2) | ((data[1] - 33) >> 4)); \
pixel[1] = ((((data[1] - 33) & 0xF) << 4) | ((data[2] - 33) >> 2)); \
pixel[2] = ((((data[2] - 33) & 0x3) << 6) | ((data[3] - 33))); \
data += 4; \
}
```

逐行对照 4.2.2 的位运算：`pixel[0]` 取 `v0<<2 | v1>>4`（R）；`pixel[1]` 取 `(v1&0xF)<<4 | v2>>2`（G）；`pixel[2]` 取 `(v2&0x3)<<6 | v3`（B）。最后 `data += 4` 让指针前进 4 个字符，准备解下一个像素。

字符数据本体：[software/images/image_fruits_8.h:14-19](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/images/image_fruits_8.h#L14-L19) 就是 `header_data` 字符串。

主机解码循环：[software/main.cpp:236-241](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L236-L241)：

```cpp
const char *ptr_image = header_data;
for (size_t i = 0; i < image_height*image_width; i++) {
   uint8_t pixel[3];
   HEADER_PIXEL(ptr_image, pixel);
   image_input[i] = pixel[0];
}
```

循环 `image_width*image_height` 次（即每个像素一次），每次解出一组 RGB，但只把 `pixel[0]`（R）存进 `image_input`——这就是「RGB→单通道灰度」的取值点。

**手算示例**（第 1 个像素）：`header_data` 开头 4 个字符是 `'U','.', '$','1'`。

- \(v_0 = 85-33 = 52\)，\(v_1 = 46-33 = 13\)，\(v_2 = 36-33 = 3\)，\(v_3 = 49-33 = 16\)
- R \(= (52<<2)|(13>>4) = 208|0 = 208\)
- G \(= ((13\,\&\,0\text{xF})<<4)|(3>>2) = 208|0 = 208\)
- B \(= ((3\,\&\,0\text{x}3)<<6)|16 = 192|16 = 208\)

所以第 1 个像素是 \((208,208,208)\)（浅灰），主机取的灰度值就是 **208**。

#### 4.2.4 代码实践

**实践目标**：亲手把 `HEADER_PIXEL` 跑一遍，确认 4 字符→3 字节的解码正确。

**操作步骤**：

1. 取 `image_fruits_8.h` 的 `header_data` 前 4 个字符 `'U','.', '$','1'`。
2. 手算每个 `c - 33`，再代入宏的三行公式，得到 `pixel[0..2]`。
3. 用同样的方法解第 2 个像素（下 4 个字符 `'M','\\','/','T'`，注意 `'\\'` 是单个反斜杠字符）。

**需要观察的现象**：两个像素解出来的 R=G=B，说明这张测试图本身接近灰度。

**预期结果**：第 1 个像素 \(=(208,208,208)\)；第 2 个像素见下面 4.2.5 的答案。

> 若想用程序验证，可在 `main()` 的解码循环里临时加一行 `printf("%u ", pixel[0]);`（修改后记得恢复，本项目讲义不修改源码落盘）。无法本地编译时标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：手算第 2 个像素 `'M','\\','/','T'` 的 RGB 值。

**参考答案**：\(v_0=77-33=44\)，\(v_1=92-33=59\)，\(v_2=47-33=14\)，\(v_3=84-33=51\)。
R \(=(44<<2)|(59>>4)=176|3=179\)；G \(=((59\,\&\,15)<<4)|(14>>2)=176|3=179\)；B \(=((14\,\&\,3)<<6)|51=128|51=179\)。即 \((179,179,179)\)。

**练习 2**：为什么是「每 4 个字符解 3 个字节」，而不是 1 个字符解 1 个字节？

**参考答案**：因为每个可打印字符只承载 6 位（\(=c-33\)），而一个字节是 8 位。\(4\times6 = 3\times8 = 24\)，所以必须凑齐 4 个字符才能无浪费地还原 3 个字节。这是 GIMP 头格式用「类 base64」方式把二进制塞进可打印 ASCII 的结果。

---

### 4.3 `test_*` 测试函数集合

#### 4.3.1 概念说明

`main.cpp` 把每一种图像处理效果都封装成一个 `test_xxx` 函数。它们签名都一样：`test_xxx(uint8_t *image_input, uint8_t *image_output, Image_processing *img_proc)`。这些函数的共同任务是**按正确顺序对 `img_proc` 发出一串接口调用**，让硬件完成「载入图→运算→回读结果」。

每个 test 都遵循同一种「三明治」结构：

```text
send_params(...)          设尺寸
send_image(image_input)   把图载入缓冲
[ switch_buffers() ]      让图落到运算缓冲
send_<运算>(...)          发运算命令
wait_end_busy()           等硬件做完
[ switch_buffers() ]      让结果落到可读缓冲
read_image(image_output)  回读结果
```

记住这个套路，就能读懂全部 test。

#### 4.3.2 核心流程

`test_*` 函数清单（都在 `main.cpp` 里）：

| 函数 | 演示的运算 | 关键接口调用 |
| --- | --- | --- |
| `test_send_read` | 仅收发，验证通路 | `send_params` / `send_image` / `read_image` |
| `test_add_threshold` | 逐像素加法 + 阈值 | `send_add` / `send_threshold` |
| `test_binary_add` | 双图相加 | `send_clear` / `send_binary_add` |
| `test_gaussian_blur` | 卷积（高斯模糊） | `send_convolution` |
| `test_simple_edge_detection` | 4 方向梯度卷积累加 | 多次 `send_convolution` |
| `test_multiplication` | 定点乘法 | `send_mult` |
| `test_binary_diff` | 双图相减（绝对差） | `send_binary_sub` |
| `test_images_average` | 双图加权平均 | `send_mult` + `send_binary_add` |
| `test_images_diff` | 双图差 | `send_binary_sub` |

贯穿所有 test 的两个「节奏控制」接口：

- **`switch_buffers()`**：交换 input / storage 两个缓冲地址。图像从 input 进出、在 storage 运算；所以载入后要切一次让图进 storage，运算完要再切一次让结果回 input 侧被读出。
- **`wait_end_busy()`**：阻塞等待硬件完成。它反复查询状态，直到 **busy 位（状态字节 bit0）清零**。

#### 4.3.3 源码精读

以本讲指定的 `test_add_threshold` 为例。[software/main.cpp:38-73](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L38-L73) 是完整函数。把它的接口调用序列抽出来：

```cpp
img_proc->send_params(image_width, image_height);   // 1. 设尺寸
img_proc->send_image(image_input);                  // 2. 载入图 → input 缓冲
img_proc->switch_buffers();                         // 3. 切换：图落到 storage
img_proc->send_add(32, true);                       // 4. 对 storage 每像素 +32（钳位）
img_proc->wait_end_busy();                          // 5. 等加法做完
img_proc->send_threshold(168, 0, 0);                // 6. 阈值化（结果留在 storage）
img_proc->wait_end_busy();                          // 7. 等阈值做完
img_proc->switch_buffers();                         // 8. 切换：结果落到可读侧
img_proc->read_image(image_output);                 // 9. 回读结果
```

（函数里穿插的 `read_status(status)` 与 `printf` 只是诊断打印，不改变硬件状态。）

对照抽象接口契约：这些方法都声明在纯虚基类里。[software/image_processing.hpp:15-34](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/image_processing.hpp#L15-L34) 列出了 `send_params`、`send_image`、`send_add`、`send_threshold`、`wait_end_busy`、`read_image`、`switch_buffers` 等全部纯虚函数——`test_*` 能调用它们，正是多态在起作用。

`wait_end_busy` 底层到底做了什么？看仿真后端实现 [simulation/image_processing_simulation.cpp:130-148](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/simulation/image_processing_simulation.cpp#L130-L148)：它在一个 `do...while` 里反复 push 一条 `COMMAND_GET_STATUS`、驱动若干时钟周期、读取状态字节，循环条件是 `status_out[0] & 0x01`——即**状态字节 bit0 就是 busy 位**，为 1 表示硬件还在忙，为 0 才退出。这正是 test 函数里 `wait_end_busy()` 能「等硬件做完」的原理。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：把 `test_add_threshold` 的接口调用序列逐步对应到硬件侧发生的事。

**操作步骤**：

1. 打开 [software/main.cpp:38-73](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L38-L73)。
2. 逐行列出对 `img_proc` 的调用（用上面 4.3.3 的 9 步序列）。
3. 为每一步写一句「硬件侧发生了什么」。

**需要观察的现象**：你会看到「载入图 → 切换让图进运算缓冲 → 发运算 → 等完成 → 切换让结果出可读缓冲 → 回读」这个固定节拍；以及 `send_add` 和 `send_threshold` 之间各有一个 `wait_end_busy`，说明**每条运算命令后都必须等硬件做完才能发下一条**。

**预期结果**（每步对应的硬件侧行为，概念层面）：

| 步骤 | 接口调用 | 硬件侧（概念） |
| --- | --- | --- |
| 1 | `send_params(w,h)` | 发 `COMMAND_PARAM`：设置图像宽高，初始化两个缓冲地址寄存器 |
| 2 | `send_image(...)` | 发 `COMMAND_SEND_IMG` + 像素：把像素（16 位打包 2 像素）写入 input 缓冲 |
| 3 | `switch_buffers()` | 发 `COMMAND_SWITCH_BUFFERS`：交换地址寄存器，图落到 storage |
| 4 | `send_add(32,true)` | 发 `COMMAND_APPLY_ADD`：触发逐像素状态机，每像素 +32 并钳位到 255 |
| 5 | `wait_end_busy()` | 反复发 `COMMAND_GET_STATUS`：轮询直到 busy 位（bit0）清零 |
| 6 | `send_threshold(168,0,0)` | 发 `COMMAND_APPLY_THRESHOLD`：对每个像素与 168 比较，命中者置为替换值 0 |
| 7 | `wait_end_busy()` | 同 5，等阈值化完成 |
| 8 | `switch_buffers()` | 再次交换地址寄存器，结果落到可被读出的一侧 |
| 9 | `read_image(...)` | 发 `COMMAND_READ_IMG`：从缓冲逐字读出结果像素回传主机 |

> 这些硬件命令字（`COMMAND_PARAM` 等）定义在 [software/image_processing.hpp:4-6](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/image_processing.hpp#L4-L6)。命令报文的字节级打包细节是 u2-l2 的主题；硬件 FSM 怎么消费这些命令是 u3-l3、u3-l4、u4-x 的主题。本讲只需建立「接口调用 ↔ 硬件动作」的概念映射。

#### 4.3.5 小练习与答案

**练习 1**：`test_add_threshold` 里，如果删掉第 3 步的 `switch_buffers()`（载入后不切换），直接 `send_add`，会发生什么？

**参考答案**：图还在 input 缓冲，而运算（`send_add`）作用在 storage 缓冲。于是加法作用在「空的/上一次残留的」storage 上，原图没被处理；最后回读到的也不是加法结果。`switch_buffers` 是让「刚载入的图」进入运算缓冲的关键一步。

**练习 2**：为什么 `send_add` 和 `send_threshold` 后面都要紧跟一个 `wait_end_busy()`？

**参考答案**：运算命令只是「下达指令」，硬件需要若干时钟周期才做完。`wait_end_busy()` 靠轮询 busy 位保证主机在下一条命令前**同步等待**硬件完成，避免在硬件还在算时又下发新命令导致状态错乱。

**练习 3**：看 `test_binary_add`（[software/main.cpp:77-91](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L77-L91)），它比 `test_add_threshold` 多用了哪个接口来准备第二个操作数？

**参考答案**：它用了 `send_clear(32)`——把 storage 缓冲整体填成像素值 32，这样 input（载入的图）和 storage（全 32）就成了双图相加的两个操作数，随后 `send_binary_add(true)` 把两者相加。

---

### 4.4 `output.dat` 输出格式

#### 4.4.1 概念说明

运算结果存在 `image_output` 数组里，但人眼看不出「一堆数字」是什么图。`main()` 最后一步把这堆数字写成 `output.dat`——一个 gnuplot 能直接渲染的**矩阵格式**文本文件。理解这个格式，你才知道为什么 `run_gnuplot.sh` 能把它变成灰度图。

#### 4.4.2 核心流程

输出循环的逻辑非常直白：**逐个像素、用空格分隔地打印；每凑满 `image_width` 个就换一行**。

```text
对 i = 0 .. (W*H-1):
    打印 image_output[i] 和一个空格
    如果 (i+1) 是 W 的整数倍: 打印换行
```

于是 `output.dat` 长这样（W = image_width）：

```text
p0 p1 p2 ... p(W-1)
pW p(W+1) ... p(2W-1)
...
```

这正是 gnuplot 的 `matrix` 格式：`plot 'output.dat' matrix w image` 会把每个数字当作一个像素灰度，按行列铺成图。

#### 4.4.3 源码精读

输出循环：[software/main.cpp:264-269](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L264-L269)：

```cpp
for (size_t i = 0; i < image_height*image_width; i++) {
   fprintf(output_file, "%d ", image_output[i]);
   if( ((i+1) % (image_width)) == 0){
      fprintf(output_file, "\n");
   }
}
```

每个像素 `%d ` 带一个尾随空格；当 `(i+1) % image_width == 0`（即写完一整行）时补一个换行。注意判定用的是 `(i+1)` 而非 `i`，所以第一行换行发生在写完第 W 个像素之后，列对齐是正确的。

随后 [software/main.cpp:271](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L271) 的 `fclose(output_file)` 把缓冲刷盘，结果才真正落到 `output.dat`。

#### 4.4.4 代码实践

**实践目标**：把输出循环和 gnuplot 的渲染对上号。

**操作步骤**：

1. 假设一张 2×2 图的 `image_output = {10, 20, 30, 40}`，`image_width = 2`。
2. 手动模拟循环，写出 `output.dat` 的内容。
3. 想象 `run_gnuplot.sh`（u1-l3）用 `set cbrange [0:255]` 把 0→黑、255→白，确认这 4 个数字会铺成 2×2 的图。

**需要观察的现象**：`%d ` 的尾随空格让同行数字分开；换行只在行末出现，所以行数 = `image_height`、每行数字数 = `image_width`。

**预期结果**：

```text
10 20 
30 40 
```

（每行末尾有一个尾随空格再换行；gnuplot 的 `matrix` 解析容忍这种尾随空格。）

> 真实运行需先按 u1-l3 用 `build_simulation.sh` 编出 `Vimage_processing`、再运行它生成 `output.dat`、再用 `run_gnuplot.sh` 查看。无法本地运行时标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：如果把循环里的 `((i+1) % image_width)` 改成 `(i % image_width)`，输出会怎样错位？

**参考答案**：换行会提前一个像素发生（在第 W-1 个像素写完后、第 W 个像素之前），导致第一行少一个像素、后续行整体左移错位，gnuplot 渲染出来的图会「斜掉」。`(i+1)` 是为了让换行恰好在写完整整 W 个像素之后。

**练习 2**：为什么像素值范围是 0~255？

**参考答案**：`image_output` 是 `uint8_t`，每个像素 1 字节；灰度值天然在 0~255。这也是 `run_gnuplot.sh` 里 `set cbrange [0:255]` 把颜色映射固定在这个范围的原因。

## 5. 综合实践

把本讲四个模块串起来，完成下面这个端到端小任务。

**任务**：把程序从当前的 `test_simple_edge_detection` 切到 `test_add_threshold`，跑通「加法 + 阈值」，并验证结果。

**步骤**：

1. **改 test 选择**：在 [software/main.cpp:251-260](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L251-L260)，注释掉第 256 行的 `test_simple_edge_detection`，取消注释第 253 行的 `test_add_threshold`。（说明：本讲义不修改源码落盘，这里描述的是你本地实验时的操作。）
2. **复述调用链**：照 4.3.4 的 9 步表，口头说一遍 `test_add_threshold` 对硬件下达的命令序列及每步硬件侧动作。
3. **解码校验**：用 4.2.3 的方法，手算 `image_fruits_8.h` 第 1 个像素灰度值（应为 208）。若用 `image_fruits_64.h` 跑，可在解码循环临时加 `printf` 观察前几个值。
4. **构建运行**：按 u1-l3 用 `build_simulation.sh` 重新编译并运行，生成新的 `output.dat`。
5. **可视化**：用 `run_gnuplot.sh` 查看结果图，对照原图观察「整体变亮（+32）后再被阈值二值化」的效果。
6. **格式自检**：打开 `output.dat`，确认行数 = `image_height`、每行数字数 = `image_width`，与 4.4 的矩阵格式一致。

**预期现象**：结果图应是「阈值二值化」后的图——像素要么是 0（被阈值命中的部分）、要么是原图加 32 后仍未超过 168 的亮度。若结果全黑或全白，多半是忘了某个 `switch_buffers` 或 `wait_end_busy`。

## 6. 本讲小结

- `main()` 是 6 段线性骨架：`fopen` → new 后端 → new 像素数组 → 解码 `.h` 图 → 跑一个 test → `fprintf` 落盘 → `fclose`。
- 后端选择靠 `#ifdef SIMULATION / #elif ICE40`，在 include 头文件和 new 对象两处生效；test 函数通过 `Image_processing *` 多态指针与后端解耦。
- `.h` 图像是 GIMP 导出的「每 6 位编一个字符」格式：4 个字符（\(4\times6=24\) 位）正好解出 3 个 RGB 字节；`HEADER_PIXEL` 宏完成解包，主机只取 R 通道作灰度。
- 所有 `test_*` 共享「send_params → send_image → switch_buffers → 运算 → wait_end_busy → switch_buffers → read_image」三明治套路。
- `wait_end_busy` 靠轮询状态字节的 busy 位（bit0）来同步等待硬件完成；`switch_buffers` 控制 input/storage 双缓冲的切换。
- `output.dat` 是 gnuplot `matrix` 格式：每行 `image_width` 个空格分隔的数字、行末换行。

## 7. 下一步学习建议

本讲只建立了「接口调用 ↔ 硬件动作」的**概念映射**，还没回答两个关键问题：**这些接口在底层是怎么打包成字节流发给硬件的？** 以及 **硬件收到命令后又怎么执行？**

建议下一步：

- 进入 **u2-l1（抽象基类与命令枚举）**：系统学习 `image_processing.hpp` 的纯虚接口表与 `Commands` 枚举，把本讲里零散见到的 `send_*` 方法整理成一张完整的契约表。
- 接着 **u2-l2（命令协议与报文格式）**：看每个 `send_*` 如何被打包成「1 字节操作码 + 变长参数」的字节流。
- 再进入 **第 3 单元**：打开 `hdl/image_processing.v`，看硬件 FSM 如何消费这些命令、读写双缓冲存储——把本讲里「硬件侧（概念）」那一列变成真正的状态机代码。
