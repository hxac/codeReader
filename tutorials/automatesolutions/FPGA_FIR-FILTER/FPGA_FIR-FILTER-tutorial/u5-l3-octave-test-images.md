# 用 Octave 生成测试图像与期望结果

## 1. 本讲目标

本讲承接 u5-l1（PPM 仿真测试台）与 u5-l2（自校验测试台），回答一个关键问题：**自校验测试台里用来比对的「期望图像」从哪里来？**

学完本讲你应该能够：

- 说清 `sharp_generate_testbench_images.m` 如何用 Octave 跑一遍与硬件完全相同的锐化算法，产出「输入 PPM」和「期望 PPM」两份文件；
- 看懂 `write_ascii_ppm.m` 如何把一幅图像写成 testbench 能读取的 P3（ASCII）PPM；
- 把「Octave 软件参考 → 期望 PPM → 硬件逐像素比对」这条软硬件协同链路完整打通，并理解为什么图像边缘要跳过、为什么内部像素能逐个相等。

## 2. 前置知识

在进入源码前，先用通俗语言铺垫三个概念。

**软件参考（golden model / 黄金模型）。** 验证一片数字电路，最稳妥的办法是先有一个「已知正确」的答案，再让硬件跑同样的输入，把硬件输出和这个答案逐项对比。Octave 在本项目中扮演的就是这个角色：它用高级语言、高精度地算一遍锐化结果，作为硬件的对错标尺。

**PPM 图像格式。** PPM 是一种极简的无损图像格式，本项目用的是它的 ASCII 变体 **P3（plain PPM）**。P3 把每个像素的 R、G、B 三个分量直接写成十进制整数，人眼可读、也能用 VHDL 的 `textio` 一行行读。这正好和 u5-l1 里 testbench 用 `textio` 读写图像的需求对上。

**可分离二维滤波。** u2-l1 已讲过：二维锐化核可以拆成「先垂直、再水平」两次一维卷积。本讲的 Octave 脚本就是这条原理的软件实现，它和硬件 `sharp_slice`（u3-l3）用的是同一组系数 `[1, 0, -9, 48, -9, 0, 1]/32`。

> 提示：本讲不涉及 VHDL 语法，核心是 Octave 脚本与 PPM 文件格式。如果你还没读过 u2-l2（系数从哪来）和 u5-l1（testbench 怎么读写 PPM），建议先看这两讲。

## 3. 本讲源码地图

本讲涉及的关键文件如下表。

| 文件 | 所在目录 | 作用 |
|------|----------|------|
| `sharp_generate_testbench_images.m` | `Verification/` | 主脚本：读 JPEG → 可分离锐化 → 写出输入 PPM 和期望 PPM |
| `write_ascii_ppm.m` | `Verification/` | 工具函数：把图像矩阵写成 P3 格式 PPM 文件 |
| `sharp_image_filter.m` | `Octave/` | 算法原型：同样的锐化，但输出给人看的 JPEG（对比用） |
| `sim_sharp_self-checking.vhd` | `Verification/` | 消费方：读期望 PPM，和硬件输出逐像素比对 |

注意一个容易被忽略的细节：**生成测试图的脚本 `sharp_generate_testbench_images.m` 放在 `Verification/` 目录而不是 `Octave/` 目录**。这是因为它的产物是给仿真测试台用的，归属验证流程；而 `Octave/` 目录放的是纯算法探索脚本。两者共享同一套滤波代码。

## 4. 核心概念与源码讲解

### 4.1 软件参考滤波生成期望图

#### 4.1.1 概念说明

`sharp_generate_testbench_images.m` 是整条验证链的起点。它做的事可以一句话概括：**用 Octave 跑一遍锐化，把「原始输入」和「正确输出」分别写成两份 PPM，交给 testbench。**

这两份文件各自的角色：

- **输入 PPM**（如 `Lindau_Harbour_720p.ppm`）：testbench 把它当成视频源，逐像素喂给硬件顶层 `sharp`（见 u5-l1 的激励进程）。
- **期望 PPM**（如 `Lindau_Harbour_expected.ppm`）：软件算出的「正确答案」。testbench 把硬件实际输出和它逐像素比对（见 u5-l2 的自校验逻辑）。

为什么用 Octave 当参考，而不是手算？因为锐化是一个邻域卷积，手算 1280×720 个像素不现实；而 Octave 的 `imfilter` 几行代码就能跑完全图，且精度足够当作标尺。

#### 4.1.2 核心流程

脚本的执行流程非常线性：

1. 加载 `image` 包，用 `imread` 读一张 JPEG 测试图（推荐 1280×720）；
2. 定义锐化系数 `f_hor = [1, 0, -9, 48, -9, 0, 1]/32`，并转置得到垂直系数 `f_ver`；
3. **可分离两次滤波**：先 `imfilter(img_in, f_ver)` 做垂直方向，再对中间结果 `imfilter(img_tmp, f_hor)` 做水平方向；
4. 用 `write_ascii_ppm` 把原图写成输入 PPM，把锐化结果写成期望 PPM。

这里的两次 `imfilter` 对应的数学就是可分离二维卷积。记一维核为 \(h=[1,0,-9,48,-9,0,1]/32\)，则两次一维滤波等价于一次二维卷积：

\[
\text{img\_tmp}(y,x) = \sum_{j=-3}^{3} h(j)\,\text{img\_in}(y+j,\,x)
\]

\[
\text{img\_out}(y,x) = \sum_{i=-3}^{3} h(i)\,\text{img\_tmp}(y,\,x+i)
\]

合起来就是用 \(7\times7\) 的二维核（核矩阵是 \(h\) 与自身的外积）做卷积。这正是 u2-l1 讲的「运算量从 49 降到 14」的软件版本，也是 u3-l3 硬件 `sharp_slice` 的算法原型。

> 一个容易踩的坑：Octave/MATLAB 的 `imfilter` 默认做的是**相关（correlation）**而不是卷积（convolution）。两者的差别在于核是否翻转。**本项目的核是对称的**（`[1,0,-9,48,-9,0,1]` 正读反读一样），所以相关和卷积结果完全相同，不必纠结。但如果你以后改成非对称核（比如 u6-l3 的边缘检测方向核），就必须注意 `imfilter` 默认是相关，要保证软件和硬件对「核方向」的理解一致。

#### 4.1.3 源码精读

先看脚本开头的图像读取和系数定义：

[sharp_generate_testbench_images.m:L10-L15](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/Verification/sharp_generate_testbench_images.m#L10-L15) —— 加载 `image` 包、用 `imread` 读 JPEG；第 14–15 行定义水平系数 `f_hor` 并转置得到垂直系数 `f_ver`。注意输入图注释里写明推荐 1280×720（720p），这和硬件 `sharp_linemem` 里 1280 项行存储（u4-l1）以及 74.25 MHz / 720p 视频时钟（u1-l3）严格对应。

接着是两次可分离滤波，也是全脚本最核心的两行：

[sharp_generate_testbench_images.m:L17-L18](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/Verification/sharp_generate_testbench_images.m#L17-L18) —— 先垂直 `imfilter(img_in, f_ver)`，再水平 `imfilter(img_tmp, f_hor)`，得到软件参考结果 `img_out`。

最后把两份结果落盘成 PPM：

[sharp_generate_testbench_images.m:L20-L21](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/Verification/sharp_generate_testbench_images.m#L20-L21) —— 原图写成输入 PPM，锐化结果写成期望 PPM。这两个文件名后面会被 testbench 用字符串常量引用，**改名时必须和 testbench 里的常量保持一致**（见 4.3）。

现在做一个关键对比。把本脚本和 `Octave/sharp_image_filter.m` 放在一起看：

[sharp_image_filter.m:L13-L19](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/Octave/sharp_image_filter.m#L13-L19) —— 这是 u2-l1 里的算法原型：**同样的系数、同样的两次 `imfilter`**，但它第 19 行用 `imwrite` 输出一张 JPEG 给人看。

也就是说，`sharp_generate_testbench_images.m` 和 `sharp_image_filter.m` 的滤波代码几乎逐字相同，**唯一的区别是输出方式**：前者写 PPM 喂给 testbench，后者写 JPEG 给人眼预览。这正说明同一份「黄金参考算法」被复用了两次——一次用于算法探索，一次用于硬件验证。

#### 4.1.4 代码实践

1. **实践目标**：亲手跑通测试图生成脚本，得到两份 PPM。
2. **操作步骤**：
   - 安装 GNU Octave 并加载 `image` 包（在 Octave 命令行执行 `pkg install -forge image`，再 `pkg load image`）。
   - 准备一张 1280×720 的 JPEG 测试图，放到 `Verification/` 目录。
   - 修改 `sharp_generate_testbench_images.m` 第 11、20、21 行的文件名，指向你的测试图。
   - 在 `Verification/` 目录下运行 `sharp_generate_testbench_images`。
3. **需要观察的现象**：Octave 控制台打印 `Edge enhancement with vertical and horizontal FIR-filter`，目录下出现两个新文件：`<你的图>.ppm` 和 `<你的图>_expected.ppm`。
4. **预期结果**：两个 PPM 文件大小相近（都是同尺寸的 ASCII 文本），用图像查看器（如 IrfanView，README 里推荐的）打开 `_expected.ppm`，应能看到比原图更锐利、边缘更清晰的版本。
5. **待本地验证**：具体 Octave 版本和 `image` 包的安装方式可能略有差异，以你本机环境为准。

#### 4.1.5 小练习与答案

**练习 1**：为什么脚本要写出**两份** PPM，而不是一份？

**参考答案**：一份是「输入」，作为 testbench 的视频激励喂给硬件；另一份是「期望」，作为正确答案和硬件输出比对。两份缺一不可——没有输入无法驱动硬件，没有期望无法判断对错。

**练习 2**：把本脚本的滤波部分和 `sharp_image_filter.m` 对比，它们最大的区别是什么？为什么要有这个区别？

**参考答案**：滤波算法（系数、两次 `imfilter`）完全相同；区别只在输出格式——本脚本写 PPM（ASCII、testbench 可读），`sharp_image_filter.m` 写 JPEG（压缩、人眼可读）。原因是 testbench 用 VHDL `textio` 只能读 ASCII 文本，必须用 P3 PPM；而算法探索阶段人看效果用 JPEG 更方便。

---

### 4.2 ASCII PPM 写出函数

#### 4.2.1 概念说明

`write_ascii_ppm(img, file_name)` 是一个独立的工具函数，负责把一幅图像矩阵写成 P3 格式的 PPM 文件。理解它要从 P3 的文件结构入手。

一个 P3 PPM 文件由「文件头 + 像素数据」两部分组成，文件头固定四行：

```
P3                          ← 魔数，表示 ASCII 彩色 PPM
# generated by Octave       ← 注释行（可有可无，testbench 会跳过）
1280 720                    ← 宽 高（注意顺序：先宽后高）
255                         ← 每个分量的最大值（8 位即 255）
```

像素数据紧随其后，每个像素是三个十进制整数（R G B），用空白分隔。本项目约定**每行写一个像素**（`R G B\n`），这样格式干净、便于排查。

为什么必须是 P3（ASCII）而不是 P6（二进制）？因为 testbench 用 VHDL 的 `textio` 库逐字符、逐行解析文件，二进制 P6 无法这样读取。P3 是软件（Octave）和硬件仿真（VHDL `textio`）之间唯一的「共同语言」。

#### 4.2.2 核心流程

`write_ascii_ppm` 的执行步骤：

1. `size(img)` 取得图像尺寸（返回 `[行数, 列数, 3]`）；
2. `fopen` 以写模式打开目标文件；
3. 依次 `fprintf` 写入四行文件头：魔数 `P3`、注释、`宽 高`、`255`；
4. 两层 `for` 循环遍历每个像素，`fprintf "%i %i %i"` 写出 R、G、B；
5. `fclose` 关闭文件，返回状态码。

这里有一个**最容易写反的点**：图像矩阵 `img` 的维度是 `[行(y), 列(x), 通道]`，所以 `size(img)` 返回 `(行数, 列数)`。但 PPM 头要求「先宽后高」，即先列数（宽）后行数（高）。代码里写成 `img_size(2) img_size(1)`，正是为了把这个顺序调对。

#### 4.2.3 源码精读

先看文件头写入：

[write_ascii_ppm.m:L8-L14](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/Verification/write_ascii_ppm.m#L8-L14) —— 第 8 行取尺寸；第 10 行 `fopen` 打开文件；第 11–14 行依次写入 `P3`、注释、`宽 高`、`255`。注意第 13 行 `%i %i` 对应的是 `img_size(2)`（列数=宽）在前、`img_size(1)`（行数=高）在后，**顺序与 `size` 的返回相反**，这是为了符合 PPM 规范。

再看像素数据写入：

[write_ascii_ppm.m:L15-L19](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/Verification/write_ascii_ppm.m#L15-L19) —— 外层循环 `y` 遍历行、内层 `x` 遍历列，第 17 行 `fprintf(file_handle,"%i %i %i\n",img(y,x,:))` 把第 y 行第 x 列像素的三个通道一次性写出。`img(y,x,:)` 返回一个 3 元向量，正好喂给三个 `%i`。

最后关闭文件：

[write_ascii_ppm.m:L21](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/Verification/write_ascii_ppm.m#L21) —— `fclose` 刷盘并返回状态码，调用方可用它判断写入是否成功。

把这份输出和 testbench 的读取对照看，格式是严丝合缝的：testbench 依次读「魔数行、注释行、尺寸行、最大值行、像素数据」，正好对应这里写入的四行头 + 像素体。testbench 还用 `if l'length < 2 then readline` 来容忍一行末尾可能残留的空白字符，所以「一行一像素」和「一行多像素」它都能读。

#### 4.2.4 代码实践

1. **实践目标**：用肉眼确认生成的 PPM 头格式与 testbench 的读取顺序一致。
2. **操作步骤**：
   - 运行 4.1 的脚本，生成一个 PPM 文件。
   - 用文本编辑器或 `head -n 5 <文件>.ppm` 查看前几行。
3. **需要观察的现象**：第一行 `P3`，第二行注释，第三行 `1280 720`（先宽后高），第四行 `255`，第五行起是 `R G B` 三元组。
4. **预期结果**：头部四行的内容和顺序与上面「文件头结构」完全一致。把第三行的两个数字和你的原图尺寸核对——第一个数是宽度（列数），第二个是高度（行数）。
5. **待本地验证**：不同图像查看器对注释行的容忍度不同，但 testbench 的解析逻辑已验证可读。

#### 4.2.5 小练习与答案

**练习 1**：如果把第 13 行误写成 `img_size(1) img_size(2)`（即先高后宽），仿真会发生什么？

**参考答案**：PPM 头会变成「先高后宽」，testbench 读到的 `x_size`、`y_size` 会被互换。这会导致 testbench 按错误的行列数驱动视频时序、读取像素，画面错位甚至尺寸校验失败（自校验测试台里有尺寸一致性断言，会直接报 `image size of expected values does not match stimuli`）。

**练习 2**：为什么每个像素单独占一行（`%i %i %i\n`），而不是把多个像素挤在一行？

**参考答案**：主要是可读性和健壮性。一行一像素方便人眼排查、也方便 `diff`；而且 testbench 的读取逻辑用 `l'length < 2` 判断是否需要读新行，对「一行一像素」和「一行多像素」都兼容，所以选哪种都能跑，但一行一像素更不易出错。

---

### 4.3 软硬件结果对接

#### 4.3.1 概念说明

本模块把前两个模块串起来：Octave 产出的期望 PPM，到底怎么和硬件输出对上号？这就是「自校验」的闭环。

核心思想很朴素——**逐像素比对**：testbench 一边把输入图喂给硬件，一边在每个有效输出像素到来时，从期望 PPM 里顺序读一个像素，比较两者的 R、G、B 是否相等；不等就累加 `mismatch` 计数；仿真结束根据 `mismatch` 是否为 0 报 PASS/FAIL。

但有两个工程细节决定了这条链路能否真的「对得上」：

1. **文件名必须一致。** testbench 用字符串常量写死了输入、期望、响应三个文件名。Octave 脚本写出的文件名必须和这些常量逐字符匹配，否则 testbench 打不开文件。
2. **边界区域不可比。** 软件和硬件在图像边缘的行为不同，必须跳过边缘，只在「内部」比对。

#### 4.3.2 核心流程

整条闭环可以这样表示：

```
            Octave (软件参考)                         VHDL testbench
┌──────────────────────────────────┐      ┌─────────────────────────────────┐
│ imread(JPEG)                      │      │                                 │
│   │                               │      │                                 │
│   ├─ write_ascii_ppm ──► 输入.ppm │ ───► │ 读输入.ppm 喂给硬件 sharp        │
│   │                               │      │   │                             │
│   └─ imfilter(垂直+水平)          │      │   ▼ 硬件输出 r_out/g_out/b_out   │
│        │                          │      │   │                             │
│        └─ write_ascii_ppm ─►期望.ppm│ ──► │ 读期望.ppm，逐像素比对          │
│                                   │      │   │                             │
│                                   │      │   ▼ mismatch 计数 → PASS/FAIL    │
└──────────────────────────────────┘      └─────────────────────────────────┘
```

比对能成立的两个一致性条件：

- **系数一致**：Octave 用的 `[1,0,-9,48,-9,0,1]/32` 与硬件 `sharp_arith` 硬编码系数完全相同（u2-l2、u4-l2）。
- **舍入与饱和一致**：Octave `imfilter` 对 `uint8` 输入在内部以双精度计算，再四舍五入并饱和回 \([0,255]\)；硬件 `sharp_arith` 同样做「+16 四舍五入 + 三分支饱和截断」（u4-l2）。两者方式一致，所以内部像素逐个相等。

为什么边缘要跳过？因为两边在边界处的「无中生有」方式不同：

- **软件**：`imfilter` 在图像外侧做填充（默认补 0），让卷积窗口在边缘也有值可算，结果是一种「假设边缘外全是黑」的估计值。
- **硬件**：前 3 行的行存储器、每行前 3 列的移位寄存器都还没填满有效数据，窗口里混入了未初始化/无效像素，输出不可信。

这两种「边界处理」不等价，所以边缘像素无法对齐，必须跳过。具体跳过范围（左右各 3 列、上方合计 6 行）和 3 行垂直偏移补偿的细节，u5-l2 已详细推导，本讲只需记住结论：**只有图像内部区域是软件和硬件的共同可信区**。

#### 4.3.3 源码精读

先看 testbench 里写死的三个文件名常量——它们就是和 Octave 脚本输出对接的「接口契约」：

[sim_sharp_self-checking.vhd:L23-L25](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/Verification/sim_sharp_self-checking.vhd#L23-L25) —— `stimuli_filename`（输入）、`expected_filename`（期望）、`response_filename`（硬件输出）。Octave 脚本里 `write_ascii_ppm` 的文件名参数必须和这里前两个常量完全一致，否则 testbench 找不到文件。

再看 testbench 如何读期望 PPM 的文件头（和 4.2 写出的头一一对应）：

[sim_sharp_self-checking.vhd:L210-L222](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/Verification/sim_sharp_self-checking.vhd#L210-L222) —— 依次跳过/读入魔数、注释、尺寸（含尺寸一致性断言）、最大值，然后读第一行像素数据。这段正是 `write_ascii_ppm` 四行头的「镜像读取」。

最后看逐像素比对与边缘跳过的核心逻辑：

[sim_sharp_self-checking.vhd:L244-L257](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/Verification/sim_sharp_self-checking.vhd#L244-L257) —— 先用 `y_pos > 2` 补偿 3 行垂直偏移才开始读期望；再用 `x_pos > 2 and x_pos < x_size-3 and y_pos > 5` 跳过左右各 3 列和上方边缘；只有在内部区域才比较 R/G/B，不等就 `mismatch + 1`。

最终 PASS/FAIL 判决：

[sim_sharp_self-checking.vhd:L164-L172](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/Verification/sim_sharp_self-checking.vhd#L164-L172) —— 仿真末尾根据 `mismatch` 是否为 0，报告 `EVERYTHING OK` 或 `N MISMATCHES`。`mismatch = 0` 就是软硬件一致的最直接证据。

> 调试小贴士：如果某天你改了点东西，仿真报出零星的 `MISMATCH`，最先怀疑的就是「舍入方式不一致」。硬件的 `+16` 四舍五入和 Octave 的四舍五入一旦对不上，会在亮度跳变处出现大量 ±1 的偏差。这是软件参考链路最敏感的检查点。

#### 4.3.4 代码实践

1. **实践目标**：跑通「生成测试图 → 自校验仿真 → 看到 EVERYTHING OK」的完整闭环。
2. **操作步骤**：
   - 按 4.1 的步骤生成输入 PPM 和期望 PPM，**确保文件名**与 `sim_sharp_self-checking.vhd` 第 23–24 行的常量一致。
   - 检查 testbench 里的文件路径（u5-l1 提到原脚本里是 Windows 硬编码绝对路径），改成你本地的相对路径或仿真工作目录。
   - 用 VHDL 仿真器（ModelSim、Questa、GHDL 等）编译 `FPGA-Design/` 下的 5 个 `sharp*.vhd`，再编译 `sim_sharp_self-checking.vhd`，然后运行仿真。
3. **需要观察的现象**：仿真器控制台最终打印 `Simulation completed, EVERYTHING OK`（若 `mismatch = 0`）。
4. **预期结果**：`mismatch = 0`，证明硬件输出和 Octave 软件参考在图像内部完全一致。
5. **待本地验证**：具体仿真器的编译与运行命令依你安装的工具而定；若出现 `MISMATCH`，先用 4.3.3 的调试贴士排查舍入/系数/文件名。

#### 4.3.5 小练习与答案

**练习 1**：如果你故意把 `sharp_arith.vhd` 里的中心系数从 48 改成 47（直流增益不再为 1），自校验仿真会在哪里、以什么形式报错？

**参考答案**：会在图像内部区域（`x_pos > 2 and x_pos < x_size-3 and y_pos > 5` 的范围）出现大量 `MISMATCH in simulation at position ...` 的 `note` 报告，`mismatch` 计数远大于 0，最终打印 `N MISMATCHES`。因为系数变了，硬件输出和期望 PPM 不再逐像素相等。

**练习 2**：为什么即使软件和硬件都「正确」，图像最上面几行和左右几列仍然不能用来比对？

**参考答案**：因为两边对边界的处理方式不同。软件 `imfilter` 在图像外侧补 0 来填满卷积窗口；硬件在前 3 行（行存储未填满）和每行前 3 列（移位寄存器未填满）的窗口里是无效数据。两种边界估计不可比，所以 testbench 用 `x_pos`/`y_pos` 范围判断跳过边缘，只在内部可信区比对。

---

## 5. 综合实践

把本讲三个模块串成一个完整任务：**用你自己的 1280×720 测试图，独立完成一次软硬件闭环验证。**

1. 准备一张 1280×720 的 JPEG（内容最好有明显的边缘和细节，比如建筑、文字，这样锐化效果看得见）。
2. 修改 `sharp_generate_testbench_images.m` 的三处文件名，运行它，得到输入 PPM 和期望 PPM。
3. 用文本工具检查生成的 PPM 头（确认 `P3`、尺寸「先宽后高」、`255`）。
4. 把两个 PPM 放到仿真工作目录，确认 `sim_sharp_self-checking.vhd` 里的文件名常量与之匹配，并修正路径。
5. 编译并运行自校验仿真，目标是看到 `EVERYTHING OK`。
6. （进阶）故意把期望 PPM 换成「输入 PPM」（即把 `expected_filename` 指向未锐化的原图），重新仿真，观察 `mismatch` 数量暴涨——这能直观证明「期望图必须是锐化后的软件参考，而不是原图」。

> 如果第 5 步报 `MISMATCH`，回到 4.3.3 的调试贴士：按「文件名 → 路径 → 系数 → 舍入」的顺序逐项排查。

## 6. 本讲小结

- `sharp_generate_testbench_images.m` 用 Octave 跑一遍与硬件相同的可分离锐化，产出**输入 PPM**（激励）和**期望 PPM**（黄金参考）两份文件。
- 它和 `sharp_image_filter.m` 共享同一套滤波代码，区别只在输出格式（PPM vs JPEG），体现了「同一算法参考被算法探索和硬件验证复用」。
- `write_ascii_ppm.m` 把图像写成 P3（ASCII）PPM：四行头（魔数、注释、宽高、255）+ 像素体；关键是尺寸「先宽后高」、与 `size()` 返回顺序相反。
- P3 是软件（Octave）和硬件仿真（VHDL `textio`）之间唯一的共同文本格式，testbench 的读取顺序与 `write_ascii_ppm` 的写入顺序严格镜像。
- 闭环成立靠两个一致性：**系数相同**、**舍入与饱和方式相同**（Octave 四舍五入+饱和 ↔ 硬件 +16 舍入+饱和），所以图像内部逐像素相等。
- 边缘区域因软件补零与硬件未填满缓冲的处理不同而不可比，testbench 跳过边缘、补偿 3 行垂直偏移后比对，`mismatch = 0` 即软硬件一致。

## 7. 下一步学习建议

- 本讲完成了「验证」单元（U5）的最后一篇。至此你已经掌握：PPM testbench（u5-l1）、自校验比对（u5-l2）、测试图与期望图的生成（本讲）。
- 接下来进入 **U6 专家层**：建议先读 **u6-l1（QSF 引脚约束）** 和 **u6-l2（SDC 时序约束）**，把「RTL 如何绑定到物理器件和时钟」补齐。
- 如果你对二次开发更感兴趣，可以直接跳到 **u6-l3（架构取舍与二次开发）**——那里会用到本讲的闭环：在 Octave 改系数 → 重新生成期望 PPM → 用自校验测试台验证新滤波效果（如边缘检测、平滑）。本讲建立的「软件参考 → 逐像素比对」流程正是 u6-l3 二次开发闭环的验证骨架。
- 想深入理解比对时跳过边缘的几何原因，可回看 **u3-l3（sharp_slice 数据流）** 和 **u4-l1（行存储循环缓冲）**；想理解舍入饱和为什么影响一致性，可回看 **u4-l2（sharp_arith 定点运算）**。
