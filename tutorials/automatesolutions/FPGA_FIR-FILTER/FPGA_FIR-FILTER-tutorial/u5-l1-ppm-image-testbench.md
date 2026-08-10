# PPM 图像仿真测试台 sim_sharp

## 1. 本讲目标

前面几讲我们读懂了 `sharp` 滤波器的硬件实现：顶层的端口转换（u3-l1）、视频时序同步（u3-l2）、二维数据流（u3-l3）、行存储（u4-l1）和定点运算（u4-l2）。但是，**这些硬件代码本身不会自己跑起来**——在把设计下载到 Cyclone V 板子之前，我们需要先在仿真器里"喂"一张图像给它，看它输出是否正确。

本讲就来读这个"喂图像、看输出"的程序：仿真测试台 `sim_sharp.vhd`。学完本讲，你应当能够：

- 看懂用 VHDL `textio` 库读写 ASCII 格式（P3）PPM 图像的完整流程；
- 理解测试台如何在没有真实摄像头的情况下，**重建 vs/hs/de 视频时序和水平消隐**，把一张静态图变成逐像素的视频流喂给被测器件（DUV）；
- 掌握测试台里**激励进程与响应进程这两个并发进程**如何通过一个 `end_tb` 信号协作、并在仿真结束时干净地收尾。

## 2. 前置知识

本讲依赖 u3-l1（顶层 `sharp.vhd` 的端口）。在进入测试台之前，先用三句话回顾必要的概念：

- **测试台（testbench / test bench）**：一段不会综合成真实电路的 VHDL 代码，专门用来给被测设计提供输入激励（stimuli）、观察其输出响应（response）。它没有对外的物理端口，自己产生时钟、复位和信号。
- **被测器件（Design Under Verification, DUV）**：这里就是把 u3-l1 讲过的顶层 `sharp` 当成一个"黑盒"例化进测试台，给它喂输入、收它的输出。`sharp` 的端口清单（`clk`、`reset_n`、`vs_in/hs_in/de_in`、`r_in/g_in/b_in` 和对应的 `_out`）就是测试台要驱动和采集的全部信号。
- **视频时序信号 vs/hs/de**：u3-l2 已深入讲过。简单说，`vs`（场同步）标一帧的边界、`hs`（行同步）标一行的边界、`de`（数据有效）逐像素标记"现在这个时钟沿送进来的是不是有效像素"。本讲里，**这三个信号不是来自摄像头，而是由测试台一行一行"伪造"出来**的。

还有一个新概念是 **PPM 图像格式**和 **`textio` 库**，我们在 4.1 节从零讲起。

## 3. 本讲源码地图

本讲只涉及两个文件，但重点几乎全在测试台上：

| 文件 | 作用 | 本讲扮演的角色 |
| --- | --- | --- |
| `FPGA-FIR-Filter-master/Verification/sim_sharp.vhd` | 仿真测试台 | **本讲主角**：读图、造时序、驱动 DUV、写回输出图 |
| `FPGA-FIR-Filter-master/FPGA-Design/sharp.vhd` | 顶层滤波器 | 被测器件 DUV，回顾它的端口即可 |

另外，`Verification/` 目录里还附带了现成的测试素材，本讲实践会用到：

- `Sample_Test.ppm`（约 8.7 MB，1040×780 的输入测试图）
- `Sample_Sharp.ppm`（约 8.2 MB，仿真产出的锐化结果图，仓库里已有一份参考）
- `Sample.jpg`（同一张图的 JPEG 版本，方便预览）

> 提示：仓库自带的 PPM 是 1040×780，并不是 720p 的 1280×720。这没关系——测试台会**从 PPM 文件头读出宽高**，动态适配；只要宽度不超过行存储器深度（1280，见 u4-l1）即可。

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：**4.1 用 textio 读写 PPM 图像**、**4.2 视频时序激励的生成**、**4.3 激励/响应双进程的协作**。三者正好对应"读图—造时序驱动—收结果写图"三件事。

### 4.1 textio 读写 PPM 图像

#### 4.1.1 概念说明

在真实硬件里，FPGA 从视频解码芯片接收逐像素的 RGB 数据流。在仿真里没有这块芯片，于是我们用一个**图像文件**来当"视频源"，再让测试台在仿真运行时一行一行地把像素读出来、按时序喂给 DUV。同样地，DUV 输出的像素流也被测试台逐个写进一个图像文件，仿真结束后用看图软件打开就能直观看到锐化效果。

本设计选用 **PPM（Portable Pixel Map）格式**，更准确地说是 **P3 子格式**（ASCII 文本、RGB 三通道）。它最大的好处是**纯文本、肉眼可读、不需要任何二进制解析库**，刚好配合 VHDL 的 `textio` 文本读写库。注释里也写明：可以用 IrfanView 等看图软件打开查看。

P3 文件的结构非常简单，前 4 行是文件头，之后是像素数据：

```
P3                                  <- 第1行：魔数(magic number)，P3 表示 ASCII 彩色
# Created by IrfanView              <- 第2行：注释行，以 # 开头
1040 780                            <- 第3行：宽度 高度（单位是像素）
255                                 <- 第4行：最大颜色值（这里每个通道 0~255，即 8 位）
93 115 92 97 119 96 ...             <- 像素数据：每 3 个整数一组 = R G B
```

关键点：像素数据里**每个整数用一个空白（空格或换行）分隔**，一行放几个像素没有硬性规定——文件里的一"行"和一个像素、一"图像行"没有对应关系。这个特点在 4.1.3 节会直接影响读图代码的写法。

> 小术语：`textio` 是 VHDL 标准库 `std` 提供的文本文件读写包，核心类型是 `text`（文件类型）和 `line`（一行字符的缓冲区指针），核心过程是 `readline`（读一行到缓冲）、`read`（从缓冲解析一个值）、`write`（往缓冲写一个值）、`writeline`（把缓冲写进文件）。本设计还额外引用了 `ieee.std_logic_textio`，方便直接读写 `std_logic` 类型。

#### 4.1.2 核心流程

测试台读写 PPM 的流程可以概括为：

**读图（在激励进程里）：**

1. 用 `file_open` 以 `read_mode` 打开输入 PPM；
2. 连续 `readline` 跳过/解析前 4 行：跳过魔数和注释，从第 3 行 `read` 出宽度 `x_size` 和高度 `y_size`，跳过第 4 行的最大值，再 `readline` 读入第一行像素数据；
3. 在逐像素喂给 DUV 的循环里，每次 `read` 三个整数（R、G、B），把它们转成 8 位 `std_logic_vector`；
4. 因为一行文本可能放多个像素、也可能提前用完，所以每次读取前要判断当前行缓冲是否快空了，空了就 `readline` 读下一行。

**写图（在响应进程里）：**

1. 用 `file_open` 以 `write_mode` 打开输出 PPM；
2. 先用 `write`/`writeline` 写出文件头：魔数 `P3`、一行注释、`x_size y_size`、最大值 `255`；
3. 每当 DUV 输出一个有效像素（`de_out='1'`），就把它的 R、G、B 三个整数连同空格 `write` 进缓冲，再 `writeline` 落盘（每个输出像素占文件里一行）。

读用 `read`/`readline`，写用 `write`/`writeline`——配对使用，非常对称。

#### 4.1.3 源码精读

**库引用**：测试台在标准库之外，显式引入了 `textio`：

这段引入了读写文件所需的全部类型与过程。见 [sim_sharp.vhd:11-15](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/Verification/sim_sharp.vhd#L11-L15) ——注意 `use std.textio.all;` 和 `use ieee.std_logic_textio.all;`，没有这两句，后面的 `file`、`readline`、`write` 全都编译不过。

**文件名常量（注意路径！）**：输入/输出文件名是两个字符串常量：

[sim_sharp.vhd:23-24](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/Verification/sim_sharp.vhd#L23-L24) 把文件名硬编码成了作者机器上的 **Windows 绝对路径** `C:\Users\jonel\OneDrive\Desktop\...`。这是本讲实践任务要改的地方——你在自己的机器上跑，这个路径几乎肯定不存在。

**读 PPM 文件头**：

```vhdl
file_open(stimuli_status, stimuli_file, stimuli_filename, read_mode);
readline(stimuli_file, l);          -- 第1行：魔数 P3
readline(stimuli_file, l);          -- 第2行：注释
readline(stimuli_file, l);          -- 第3行：宽 高
read(l, i); x_size <= i;
read(l, i); y_size <= i;
readline(stimuli_file, l);          -- 第4行：最大值 255
readline(stimuli_file, l);          -- 第一行像素数据
```

[sim_sharp.vhd:88-96](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/Verification/sim_sharp.vhd#L88-L96) 说明：先把第 3 行整行读进缓冲 `l`，再用两次 `read(l, i)` 顺序解析出宽和高，分别赋给**信号** `x_size`、`y_size`（后面响应进程写文件头时会用到它们）。注意 `x_size <= i` 是信号赋值，要经过一个 delta 延迟才生效——这会影响 4.3 节两个进程的同步方式。

**逐像素读取（带换行判断）**：

```vhdl
if l'length < 2 then readline(stimuli_file, l); end if;
read(l, r_integer);
read(l, g_integer);
read(l, b_integer);
r_in <= std_logic_vector(to_unsigned(r_integer,8));
g_in <= std_logic_vector(to_unsigned(g_integer,8));
b_in <= std_logic_vector(to_unsigned(b_integer,8));
```

[sim_sharp.vhd:135-142](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/Verification/sim_sharp.vhd#L135-L142) 是读图的核心。`l'length` 是当前行缓冲里还剩多少字符。判断条件 `< 2` 是个启发式：如果缓冲里只剩 0 或 1 个字符（很可能只是一个尾随的空格），就认为这行已经读完，`readline` 读下一行。这样无论原文件把每行塞了几个像素，都能正确地连续读出 R、G、B 三个整数。读完三个整数后，用 `to_unsigned(...,8)` 转成 8 位无符号，再 `std_logic_vector()` 转成与 `sharp` 端口一致的类型喂进去——这正好对应 u3-l1 里讲的"顶层输入端把 `std_logic_vector` 还原成像素值"的逆过程。

**写 PPM 文件头**（响应进程）：

```vhdl
write (l_sim, string'("P3"));                       -- 魔数
writeline(response_file, l_sim);
write (l_sim, string'("# generated by VHDL testbench"));  -- 注释
writeline(response_file, l_sim);
write (l_sim, x_size); write (l_sim, string'(" ")); -- 宽
write (l_sim, y_size);                              -- 高
writeline(response_file, l_sim);
write (l_sim, string'("255"));                      -- 最大值
writeline(response_file, l_sim);
```

[sim_sharp.vhd:184-193](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/Verification/sim_sharp.vhd#L184-L193) 写出标准 P3 头。这里有个 VHDL 初学者容易踩的坑：`write(l_sim, string'("P3"))` 里的 `string'(...)` 叫**限定表达式（qualified expression）**，不能省。因为 `write` 过程对多种类型都有重载（整数、字符串、`std_logic_vector`……），直接写 `write(l_sim, "P3")` 编译器无法确定 `"P3"` 是哪种字符串类型，会报歧义错误；用 `string'("P3")` 明确告诉它"这是 `string` 类型"，才能正确匹配。字符串里塞空格 `string'(" ")` 是同一回事，用来在数字之间加分隔符。

**写输出像素**（响应进程主体）：

```vhdl
r_sim := to_integer(unsigned(r_out));
g_sim := to_integer(unsigned(g_out));
b_sim := to_integer(unsigned(b_out));
write(l_sim, r_sim); write (l_sim, string'(" "));
write(l_sim, g_sim); write (l_sim, string'(" "));
write(l_sim, b_sim);
writeline(response_file, l_sim);
```

[sim_sharp.vhd:200-208](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/Verification/sim_sharp.vhd#L200-L208) 把 DUV 输出的 8 位 `std_logic_vector` 转回整数，写成 "R G B" 一行。注意输入图一行可能塞多个像素，而输出图**每个像素独占一行**——这两种排版在 P3 里都合法，看图软件都能读。

#### 4.1.4 代码实践

**实践目标**：把测试台改成能在你自己的机器上跑起来，并用仓库自带的图完成一次"读入—锐化—写出"。

**操作步骤**：

1. 复制一份 `sim_sharp.vhd`（**不要改源码仓库里的原件**，建议复制到自己的工作目录，或直接在仿真工程里挂这份测试台）。下文假设你就在 `Verification/` 目录下运行仿真器。
2. 把 [sim_sharp.vhd:23-24](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/Verification/sim_sharp.vhd#L23-L24) 的两个绝对路径常量改成相对路径，指向仓库自带的图：

   ```vhdl
   constant stimuli_filename  : string := "Sample_Test.ppm";
   constant response_filename : string := "Sample_Sharp_Mine.ppm";
   ```

   把输出改名为 `Sample_Sharp_Mine.ppm`，方便和仓库里已有的 `Sample_Sharp.ppm`（作者参考结果）对比。
3. 在仿真器（ModelSim / QuestaSim / GHDL 等）里编译 `sharp.vhd` 及其全部子模块（`sharp_slice`、`sharp_control`、`sharp_arith`、`sharp_linemem`），再编译这份测试台，然后仿真顶层 `sim_sharp`。注意**仿真器的工作目录**要能让相对路径找到 `Sample_Test.ppm`——多数情况下就是 `Verification/` 本身。
4. 仿真跑到自然停止（见 4.3 节，测试台会自己用 `assert ... severity failure` 停掉）。
5. 用看图软件（IrfanView / GIMP / macOS 预览 / VS Code 的 PPM 插件）打开 `Verification/Sample_Test.ppm` 和你新生成的 `Sample_Sharp_Mine.ppm`，并排对比。

**需要观察的现象**：输入图 `Sample_Test.ppm` 是一张相对柔和的原始图；输出图应当在边缘处（物体轮廓、文字笔画、纹理交界）明显更"硬"、对比更高，而大片平坦区域（如纯色背景）几乎不变——这正是 u2-l1 讲过的"直流增益为 1、只增强边缘"的锐化效果。

**预期结果**：你生成的 `Sample_Sharp_Mine.ppm` 与仓库自带的 `Sample_Sharp.ppm` 在视觉上应当一致（像素级一致性的严格校验是 u5-l2 自校验测试台的任务）。

> 关于"准备一张 PPM 测试图"：本实践直接用仓库自带的 `Sample_Test.ppm`，无需任何准备。若你想用自己的图，需要转成 **P3（ASCII）PPM**：GIMP 导出时选 `.ppm` 并勾选 "ASCII"；或用 ImageMagick 命令 `convert in.jpg -compress none out.ppm`；或用 u5-l3 会讲的 `write_ascii_ppm.m`。注意宽度不要超过 1280。

> 待本地验证：不同仿真器对相对路径的解析基目录、对文件 IO 的支持略有差异；若仿真报"无法打开文件"，请先把仿真器工作目录切到 `Verification/`，或改用绝对路径再试一次。

#### 4.1.5 小练习与答案

**练习 1**：P3 格式 PPM 的前 4 行分别是什么？各自含义？
**答**：第 1 行是魔数 `P3`（声明这是 ASCII 彩色图）；第 2 行是以 `#` 开头的注释（可省略或自定义）；第 3 行是宽度和高度（像素数）；第 4 行是最大颜色值（本设计是 255，对应每通道 8 位）。

**练习 2**：为什么每次读取 R/G/B 之前要先 `if l'length < 2 then readline(...)`？
**答**：因为 P3 的像素数据每行能放几个像素没有规定，文件里的一行和图像的一行并不对应。行缓冲 `l` 可能在读到一半时就被取空（只剩 0~1 个字符，多半是尾随空格），这时必须再 `readline` 加载下一行文本，才能继续 `read` 出完整的 RGB 三元组。

**练习 3**：`write(l_sim, string'("P3"))` 里的 `string'(...)` 为什么不能省略？
**答**：`write` 对整数、字符串、`std_logic_vector` 等都有重载。直接写 `"P3"` 时编译器无法判断它属于哪种字符串类型，会报歧义错误；`string'("P3")` 是限定表达式，显式标注类型为 `string`，从而唯一匹配到正确的 `write` 重载。

---

### 4.2 视频时序激励生成

#### 4.2.1 概念说明

DUV `sharp` 是一个**流式（streaming）视频处理模块**：它不存整帧图，而是像真实视频链路一样，每个时钟收一个像素、做一个像素的处理。这意味着测试台光"读出像素"还不够，还必须**按视频协议的节奏**把它们送进去——带上 `vs`/`hs`/`de` 这三个时序信号，连水平消隐（horizontal blanking）也要模拟出来。

为什么要造消隐？因为在真实视频里，每一行的有效像素之间夹着一段"没有图像"的消隐期（用于 CRT 回扫等历史原因，现代链路沿用）。本设计的 `de`（数据有效）正是用来区分"有效像素"和"消隐期"的——u4-l1 已讲过，`de` 同时充当行存储器 `sharp_linemem` 的**写使能**：只有 `de='1'` 时，像素才会被写进 RAM、地址才会推进。所以测试台必须在每行的有效像素前后插入一段 `de='0'` 的消隐，否则行存储的"一行 = 1280 槽位"的对应关系就被打破。

本测试台采用**简化时序**：它不追求严格的 720p BT.1120 / CEA-861 时序参数（真实 720p 一整行有 1650 个总时钟、其中 1280 个有效），而是用"每行 = `x_blank` 个消隐时钟 + `x_size` 个有效像素"这种最小够用的结构。只要 `de` 的窗口正确，滤波器就能正确工作。

#### 4.2.2 核心流程

测试台造时序的过程嵌套在"遍历整帧"的双重循环里，结构如下（伪代码）：

```
产生时钟 clk（10 ns 周期）
复位：reset_n 先 '0'，50 ns 后 '1'

for y in 0..y_size-1 loop                       -- 遍历每一行
    if y = 0 then vs_in <= '1'; else vs_in <= '0'; end if   -- 第 0 行拉一场同步
    hs_in <= '1';                               -- 行同步脉冲
    for x in 0..x_blank-1 loop                  -- 水平消隐（de=0）
        wait until falling_edge(clk);
    end loop
    hs_in <= '0';

    de_in <= '1';                               -- 进入有效像素区
    for x in 0..x_size-1 loop                   -- 逐像素送图
        从文件读 R,G,B → r_in,g_in,b_in
        wait until falling_edge(clk);
    end loop
    de_in <= '0';                               -- 本行有效像素结束
end loop

-- 拖尾若干时钟，把流水线里残余的有效输出冲出来
for i in 0..trail-1 loop wait until falling_edge(clk); end loop
```

三个关键设计：

1. **时钟周期是 10 ns（100 MHz），不是硬件的 74.25 MHz**——见 4.2.3。
2. **激励统一在 `falling_edge(clk)` 更新**——见 4.2.3。
3. **第一行才拉 `vs_in`**，每行都拉 `hs_in`，每行的有效段拉 `de_in`——这是 vs/hs/de 三信号的最小化构造。

设仿真时钟周期为 \(T_{clk}=10\,\text{ns}\)，那么处理一整帧的激励时钟数约为：

\[
N_{\text{frame}} \approx y_{\text{size}} \times (x_{\text{blank}} + x_{\text{size}}) + \text{trail}
\]

对自带测试图（1040×780、`x_blank=100`、`trail=1000`），大约是 \(780 \times (100 + 1040) + 1000 \approx 8.9\times10^{5}\) 个时钟，即仿真时间约 \(8.9\,\text{ms}\)。这只是仿真虚拟时间，真实跑起来通常几秒到几十秒。

#### 4.2.3 源码精读

**时钟与复位**：

```vhdl
clk <= not clk after 5 ns;            -- 并发赋值，周期 10 ns
...
reset_n   <= '0', '1' after 50 ns;    -- 复位：先低，50 ns 后拉高
```

[sim_sharp.vhd:52-53](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/Verification/sim_sharp.vhd#L52-L53) 用一句并发信号赋值造时钟：`clk` 初值 `'0'`，每 5 ns 翻转一次，故周期 \(2 \times 5\,\text{ns} = 10\,\text{ns}\)，即 100 MHz。

> 为什么仿真用 100 MHz，而真实硬件是 74.25 MHz（u1-l3、u6-l2）？因为这是**功能仿真**，关心的是"每个时钟沿数据怎么流动"，对时钟绝对频率并不敏感——锐化结果取决于数据流和时序逻辑的相对关系，与频率无关。用 10 ns 这种圆整数字只是让波形时间轴好看。u6-l2 的 74.25 MHz 是给综合工具做时序分析（STA）用的，那是另一回事。

[sim_sharp.vhd:98](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/Verification/sim_sharp.vhd#L98) 让 `reset_n` 在仿真开始时为 `'0'`（复位有效，因为 `sharp` 内部是 `reset <= not reset_n` 高有效，见 u3-l1），50 ns 后跳 `'1'` 释放复位（代码注释写作 "reset for 10 clock cycles"，实际约为 5 个 10 ns 时钟周期）。然后 [sim_sharp.vhd:108](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/Verification/sim_sharp.vhd#L108) 的 `wait for 100 ns` 保证复位彻底释放后再开始送帧。

**为什么在 falling_edge 驱动**：所有激励更新都挂在 `wait until falling_edge(clk);`（例如 [sim_sharp.vhd:123](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/Verification/sim_sharp.vhd#L123)、[sim_sharp.vhd:144](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/Verification/sim_sharp.vhd#L144)、[sim_sharp.vhd:155](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/Verification/sim_sharp.vhd#L155)）。DUV `sharp` 的所有寄存器都在 `rising_edge(clk)` 采样（u3-l1）。在下降沿改输入，意味着新数据在下降沿之后、下一个上升沿之前有整整半个周期（5 ns）保持稳定——天然满足建立/保持时间，波形上"输入变化"和"采样"也错开半拍，方便观察。这是测试台的通用良好习惯。

**场同步 vs**：

```vhdl
if (y = 0) then
  vs_in <= '1';
else
  vs_in <= '0';
end if;
```

[sim_sharp.vhd:114-118](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/Verification/sim_sharp.vhd#L114-L118) 只在遍历到第 0 行时把 `vs_in` 拉高，标出新的一帧开始。这是简化处理（真实 vs 通常是一个较窄的脉冲），但本设计对 vs 的脉宽没有严格要求，只要"每帧出现一次"即可。

**行同步 + 水平消隐**：

```vhdl
hs_in <= '1';
for x in 0 to x_blank-1 loop
  wait until falling_edge(clk);
end loop;  -- x, blanking
hs_in <= '0';
```

[sim_sharp.vhd:121-125](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/Verification/sim_sharp.vhd#L121-L125) 在每行开头先拉高 `hs_in`（行同步），然后空等 `x_blank`（常量 100，定义在 [sim_sharp.vhd:25](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/Verification/sim_sharp.vhd#L25)）个时钟作为**水平消隐**，期间 `de_in` 仍为 `'0'`，行存储不写、地址不动。这 100 拍消隐把相邻两行的有效像素在时间上隔开，让"每个有效像素对应一个 `de='1'`"的节奏成立。

**有效像素区**：

```vhdl
de_in <= '1';
for x in 0 to x_size-1 loop
  ...读一个像素的 R,G,B 赋给 r_in/g_in/b_in...
  wait until falling_edge(clk);
end loop;  -- x, active line
de_in <= '0';
```

[sim_sharp.vhd:129-149](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/Verification/sim_sharp.vhd#L129-L149) 把 `de_in` 拉高进入有效区，循环 `x_size` 次逐像素送图，循环结束再把 `de_in` 和 RGB 清零，开始下一行。这里 `de_in` 高电平的窗口正是 u4-l1 行存储写使能 `write_en = de_in` 的来源——也是 u3-l2 强调的"de 身兼同步信号与写使能两职"。

**拖尾（trail）**：

```vhdl
for i in 0 to trail-1 loop
  wait until falling_edge(clk);
end loop;
```

[sim_sharp.vhd:153-156](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/Verification/sim_sharp.vhd#L153-L156) 在整帧送完之后，再空转 `trail`（常量 1000，[sim_sharp.vhd:26](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/Verification/sim_sharp.vhd#L26)）个时钟。为什么需要拖尾？因为滤波器是流水线：最后几行像素送进去之后，对应的锐化结果要再过若干拍（约 3 行 + 6 拍，见 u3-l2、u3-l3）才会从 `data_out` 冒出来。这 1000 拍拖尾就是把流水线里残余的有效输出"冲"出来，确保响应进程能采到完整的一帧结果。

#### 4.2.4 代码实践

**实践目标**：通过观察波形，确认测试台造出来的 vs/hs/de 时序符合预期，并验证"消隐宽度不影响有效像素值"这一结论。

**操作步骤**（源码阅读 + 波形观察型实践，不修改硬件源码）：

1. 按 4.1.4 节把仿真跑起来，但在仿真器里**不要让它直接跑到底**，先在激励开始处（大约第一个 `hs_in` 脉冲前后）设断点或把仿真时间限制在 3~5 µs。
2. 把 `clk`、`reset_n`、`vs_in`、`hs_in`、`de_in`、`r_in` 加进波形窗口，放大到能看清单个时钟沿。
3. 观察并记录：`reset_n` 何时变 `'1'`？第一个 `hs_in` 脉冲持续了几个时钟？`hs_in` 拉低后多久 `de_in` 才拉高？`de_in` 高电平持续几个时钟（应等于 `x_size`）？

**需要观察的现象**：

- `reset_n` 在 50 ns 处由 `'0'` 跳 `'1'`；
- 每行开头 `hs_in` 高电平持续约 100 个时钟（即 `x_blank`），随后 `de_in` 拉高；
- `de_in='1'` 期间，`r_in/g_in/b_in` 每拍变化一次，正是从 PPM 读出的连续像素；
- `de_in` 高电平的总拍数 = `x_size`（自带图为 1040）。

**预期结果**：时序关系与上面"核心流程"的伪代码一致。可额外做一个**思想实验**：把 `x_blank` 从 100 改成 10（不必真改、只需推理），由于消隐期 `de='0'`、行存储地址冻结，有效像素仍按原顺序送入，输出图像的像素值**不会改变**——改变的只是每行之间的时间间隔。（这一点会在 4.2.5 练习 2 讨论。）

> 待本地验证：波形里的具体计数值取决于你仿真器的时钟精度设置；若数值与预期差 1 拍，多半是信号赋值 delta 延迟导致的，属正常现象。

#### 4.2.5 小练习与答案

**练习 1**：为什么所有激励都在 `falling_edge(clk)` 更新，而不是 `rising_edge`？
**答**：DUV 在 `rising_edge(clk)` 采样输入。在下降沿改输入，能让新值在下一个上升沿之前有半个周期（5 ns）的稳定时间，天然满足建立时间；同时在波形上"输入变化"与"采样"错开半拍，便于观察因果关系。

**练习 2**：如果把 `x_blank` 从 100 改成 10，输出图像 `Sample_Sharp.ppm` 的像素值会变吗？为什么？
**答**：不会变。消隐期 `de_in='0'`，行存储写使能无效、地址冻结，arith 也不推进新有效数据；有效像素仍按原顺序送入。`x_blank` 只影响每行有效段之间的"空闲"时钟数，不进入有效数据通路，故锐化结果不变。（前提是 `x_blank` 仍为正数，保证每行有一次 `de` 下降沿。）

**练习 3**：仿真时钟是 100 MHz，而真实硬件是 74.25 MHz，这会不会导致仿真出来的锐化图像和硬件上不一致？
**答**：不会。这是功能仿真，锐化结果只取决于"数据流在每个时钟沿如何被寄存器和组合逻辑处理"这种相对关系，与绝对时钟频率无关。74.25 MHz 是综合后做时序分析（STA）用的，用来判断电路能不能在那个频率下跑通（Setup/Hold 是否满足），不影响功能正确性。

---

### 4.3 激励/响应双进程与 end_tb 同步

#### 4.3.1 概念说明

`sim_sharp.vhd` 里有两个并发进程：

- **stimuli_process（激励进程）**：读输入图、造时序、把像素喂给 DUV；跑完一帧 + 拖尾后，负责停止仿真。
- **response_process（响应进程）**：等 DUV 开始输出后，把每个有效输出像素写进输出 PPM。

为什么不把这两件事写在一个进程里？因为**输入和输出在时间上是错开的**：DUV 是流水线，送进去一个像素后，对应的锐化结果要等约 3 行 + 6 拍才出来（u3-l2、u3-l3）。如果用单进程"送一个像素、马上等它的输出"，逻辑会极其别扭。分成两个并发进程后，激励进程只管"按时喂图"，响应进程只管"看到 `de_out='1'` 就收一个像素"，各自一个循环，干净利落——这正是硬件"数据流"思维的体现。

但两个进程并发就带来一个问题：**它们怎么协调开始与结束？** 尤其是结束——响应进程写文件是带缓冲的，如果激励进程跑完就直接结束仿真，响应进程可能来不及把最后几个像素刷盘、甚至来不及 `file_close`，输出 PPM 就会残缺。本设计用一个共享信号 `end_tb` 来做这次握手。

#### 4.3.2 核心流程

两个进程的协作时间线：

```
t0  仿真开始
    stimuli_process: 读 PPM 头 → x_size/y_size 赋值 → 复位 → 开始送第 1 行
    response_process: 一上来就 wait until (hs_out='1')  ← 等待

t1  DUV 输出第一个 hs_out 脉冲（hs_in 经 sharp_control 延迟 6 拍后出现）
    response_process 被唤醒：
      - 此时 x_size/y_size 早已稳定 → 用它们写输出 PPM 的文件头
      - 进入 while (end_tb /= 1) 捕获循环：每个 falling_edge 检查 de_out

t2  stimuli_process 跑完整帧 + trail 拖尾：
      end_tb <= 1;            ← 通知响应进程"我没东西可喂了"
      file_close(stimuli_file);
      assert false severity failure;  ← 主动终止整个仿真

t3  response_process 在 while 循环里检测到 end_tb=1，退出循环：
      file_close(response_file);      ← 关闭输出文件，把缓冲刷盘
      wait until (end_tb = 2);        ← end_tb 永远不会=2 → 永久挂起（干净地"卡住"自己）
```

三个巧妙之处：

1. **响应进程用 `wait until hs_out='1'` 作为起点**，既确认了 DUV 已开始工作，又顺带保证了它读 `x_size/y_size` 时这两个信号已经稳定；
2. **`end_tb <= 1` 是单向通知**：激励进程结束后通知响应进程退出捕获循环、关闭文件；
3. **真正"杀死"仿真的是激励进程的 `assert ... severity failure`**，响应进程则用 `wait until (end_tb = 2)`（一个永不成立的条件）把自己永久挂起，避免它在仿真被杀之前提前结束而触发告警。

#### 4.3.3 源码精读

**DUV 例化**：先看被测器件是怎么接进来的。

[sim_sharp.vhd:56-73](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/Verification/sim_sharp.vhd#L56-L73) 把 u3-l1 讲过的顶层 `sharp` 作为组件 `duv` 例化，端口一一对应测试台信号（端口定义见 [sharp.vhd:12-33](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp.vhd#L12-L33)）：测试台信号 `clk/vs_in/...` → DUV 的 `in` 端口，DUV 的 `_out` 端口 → 测试台信号 `vs_out/...`。这正是"黑盒例化"——测试台不需要懂 `sharp` 内部，只要端口对得上。

**`end_tb` 与 `mismatch` 信号声明**：

[sim_sharp.vhd:47-48](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/Verification/sim_sharp.vhd#L47-L48) 声明了 `end_tb`（本讲的同步握手信号）和 `mismatch`。注意：`mismatch` 在**本测试台里声明了却完全没用到**——它是为下一讲 u5-l2 的自校验测试台 `sim_sharp_self-checking.vhd` 预留的（那里会累计"输出与期望不符"的像素数）。本讲你只需关注 `end_tb`。

**激励进程的收尾**：

```vhdl
end_tb <= 1;                -- 通知响应进程关闭文件
file_close(stimuli_file);
wait for 20 ns;

assert false
  report "Simulation completed"
  severity failure;
```

[sim_sharp.vhd:158-165](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/Verification/sim_sharp.vhd#L158-L165) 是激励进程的尾声：先 `end_tb <= 1` 通知响应进程，再关闭输入文件，然后等 20 ns（给响应进程留时间反应）。最后一句是本设计的"停止开关"——`assert false ... severity failure` 故意触发一个 failure 级断言，仿真器看到 failure 会立刻停止整个仿真。用 `severity failure` 来正常结束仿真是常见技巧（也有用 `std.env.finish` / `$finish` 的写法，取决于仿真器和 VHDL 版本）。

**响应进程的起点**：

```vhdl
wait until (hs_out = '1');  -- 等到激励进程已经跑起来
```

[sim_sharp.vhd:179](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/Verification/sim_sharp.vhd#L179) 让响应进程一启动就挂起，直到 DUV 的 `hs_out` 出现第一个 `'1'`。`hs_out` 是 `hs_in` 经 `sharp_control` 延迟 6 拍后的输出（u3-l2），所以这一等既确认了"激励进程已经在送图、DUV 已经在响应"，又保证了 [sim_sharp.vhd:188-191](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/Verification/sim_sharp.vhd#L188-L191) 写文件头时读到的 `x_size/y_size` 已是稳定值。

**响应进程的捕获循环**：

```vhdl
while (end_tb /= 1) loop
  wait until falling_edge(clk);
  if (de_out = '1') then
    -- 把 r_out/g_out/b_out 转整数，写成 "R G B" 一行
  end if;
end loop;

file_close(response_file);
wait until (end_tb = 2); -- 永远等不到，进程永久挂起
```

[sim_sharp.vhd:195-214](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/Verification/sim_sharp.vhd#L195-L214) 是响应进程的主体：循环体每个 `falling_edge` 检查 `de_out`，为 `'1'` 就收一个像素。循环退出条件是 `end_tb = 1`——正是激励进程在结尾设置的那个值。退出后立刻 `file_close`（关键！把缓冲区里最后的像素刷盘），最后 `wait until (end_tb = 2)`。

**为什么以 `wait until (end_tb = 2)` 收尾**：`end_tb` 在整个仿真里只会被赋成 0（初值）或 1，永远不会变成 2。所以这条 wait 是一个**永不解除的挂起**，目的是让响应进程"占着不结束"。如果不这么做，响应进程执行到 `end process` 自然结束，某些仿真器会对"还有进程在跑却有一个进程结束了"报错或告警；让它永久挂起就回避了这个问题。真正终结仿真的是激励进程的 `assert failure`。

#### 4.3.4 代码实践

**实践目标**：通过"故意破坏同步"来理解 `end_tb` 的作用，建立对双进程协作的直观认识。

**操作步骤**（源码阅读型实践，在你自己的副本上改）：

1. 复制 `sim_sharp.vhd` 为 `sim_sharp_exp.vhd` 做实验，原件保持不动。
2. **实验 A**：把 [sim_sharp.vhd:158](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/Verification/sim_sharp.vhd#L158) 的 `end_tb <= 1;` 注释掉，重新仿真。观察输出文件 `Sample_Sharp_Mine.ppm` 的大小和能否被看图软件正常打开。
3. **实验 B**：恢复 `end_tb <= 1;`，转而在响应进程的捕获循环里加一行计数——在 `if (de_out = '1') then` 内部累加一个变量 `pixel_count`，在循环退出后用 `report "captured pixels = " & integer'image(pixel_count);` 打印出来（需把 `pixel_count` 声明为 `variable`）。重新仿真，记录这个数。

**需要观察的现象**：

- 实验 A：由于 `end_tb` 永远是 0，响应进程的 `while (end_tb /= 1)` 永不退出，`file_close(response_file)` 永远不会执行。最终仿真被激励进程的 `assert failure` 强行终止时，输出文件可能没被正确关闭——文件偏小、末尾像素丢失，或看图软件报"文件损坏"。
- 实验 B：`pixel_count` 应当等于 `x_size * y_size`（自带图为 \(1040 \times 780 = 811{,}200\)），即一整帧有效像素。

**预期结果**：实验 A 证明"`end_tb` 是让响应进程及时关文件的关键"；实验 B 则给出"捕获到一整帧输出"的量化证据，为 u5-l2 的逐像素自校验打下基础。

> 待本地验证：实验 A 中"文件损坏"的具体表现取决于仿真器对未关闭文件的处理（有的会自动 flush，文件可能仍可打开但缺尾）。无论何种表现，都说明少了 `file_close` 是不可靠的。

#### 4.3.5 小练习与答案

**练习 1**：如果删掉 [sim_sharp.vhd:158](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/Verification/sim_sharp.vhd#L158) 的 `end_tb <= 1;`，会发生什么？
**答**：响应进程的 `while (end_tb /= 1)` 永远不会退出，因此 `file_close(response_file)` 永远不会执行。当激励进程用 `assert failure` 终止仿真时，输出 PPM 缓冲区来不及刷盘，文件可能不完整或无法被正常打开。

**练习 2**：响应进程为什么以 `wait until (end_tb = 2)` 结束，而不是直接 `end process`？而真正停止仿真的又是什么？
**答**：`end_tb` 永远不会变成 2，所以这条 `wait` 是永不解除的挂起，目的是让响应进程"占着但不结束"，避免进程自然终止引发告警。真正终结整个仿真的是激励进程末尾的 `assert false ... severity failure`——它强制仿真器立即停止。

**练习 3**：为什么把"喂像素"和"收像素"拆成两个并发进程，而不是合在一个进程里顺序做？
**答**：因为 DUV 是流水线，输出相对输入有约 3 行 + 6 拍的延迟（u3-l2、u3-l3）。单进程里"送一个像素、立刻等它的输出"在时间上对不齐，逻辑会很别扭。拆成两个并发进程后，激励进程按节奏送图、响应进程见 `de_out='1'` 就收，各自独立循环，天然契合"数据流"的处理方式。

---

## 5. 综合实践

把本讲三个模块串起来，完成一次"端到端"的仿真体验。这个任务**不修改任何硬件源码**，全部在测试台副本上操作。

**任务**：在仓库自带的测试图上跑通完整仿真，并量化验证"送进去一整帧、也收回来一整帧"。

**步骤**：

1. 复制 `sim_sharp.vhd` 到自己的工作副本。
2. 按 4.1.4 把两个文件名常量改成相对路径（输入 `Sample_Test.ppm`、输出 `Sample_Sharp_Mine.ppm`）。
3. 按 4.3.4 实验 B，在响应进程里加 `pixel_count` 计数并在仿真结束前用 `report` 打印。
4. 编译 `sharp.vhd` 及全部子模块 + 这份测试台，仿真 `sim_sharp`。
5. 仿真停止后，回答三个问题：
   - 控制台打印的 `captured pixels` 是多少？是否等于从 PPM 头读出的 `x_size * y_size`？
   - 生成的 `Sample_Sharp_Mine.ppm` 文件大小是否接近仓库自带的 `Sample_Sharp.ppm`（约 8.2 MB）？
   - 用看图软件对比输入 `Sample_Test.ppm` 和你的输出 `Sample_Sharp_Mine.ppm`，能看到边缘锐化、平坦区不变的效果吗？

**预期**：捕获像素数 = 811,200（=1040×780）；输出文件大小与参考接近；视觉上边缘明显增强。三者都成立，就说明你已经把"读 PPM → 造视频时序 → 驱动 DUV → 收输出 → 写 PPM"这条完整链路跑通了。

> 待本地验证：精确的文件字节数会因每个输出像素独占一行、换行符风格（LF/CRLF）不同而略有差异，不必强求与仓库版逐字节相同；像素级一致性留给 u5-l2 的自校验测试台。

## 6. 本讲小结

- `sim_sharp.vhd` 是一个**自包含的仿真测试台**：没有外部端口，自己产生时钟、复位，把顶层 `sharp` 当黑盒例化为 DUV。
- 它用 VHDL `textio` 库读写 **ASCII PPM（P3）** 图像：输入图当"视频源"，输出图当"截图"，仿真结束后用看图软件即可直观看到锐化效果。
- 测试台**重建了简化的视频时序**：第一行拉 `vs`、每行拉 `hs` + `x_blank` 拍水平消隐、有效像素区拉 `de`；`de` 同时是行存储的写使能（承接 u4-l1）。
- 仿真用 100 MHz（10 ns）时钟而非硬件的 74.25 MHz——功能仿真只关心数据流的相对时序，与绝对频率无关（区别于 u6-l2 的 STA）。
- 激励统一在 **`falling_edge(clk)`** 更新，让输入在 DUV 的 `rising_edge` 采样前有半周期稳定时间。
- 测试台是**两个并发进程**：激励进程喂图、响应进程收图，靠共享信号 `end_tb` 协调结束；真正停止仿真的是激励进程末尾的 `assert ... severity failure`。

## 7. 下一步学习建议

本讲的测试台只负责"把输出存成图给你看"，**并不判断对错**——锐化得对不对全靠你肉眼比对。这显然不够严谨。下一讲 **u5-l2《自校验测试台与逐像素比对》** 会读 `sim_sharp_self-checking.vhd`，它同时读输入图和一张"期望图"，把 DUV 输出逐像素比对、累计 `mismatch` 数量并自动报告通过/失败。本讲里那个声明了却没用到的 `mismatch` 信号，正是为它准备的。

在那之前，你还可以：

- 重读 u3-l2，把 `delay=6` 与本讲的拖尾、`hs_out` 延迟对上号；
- 想清楚"为什么响应进程要补偿约 3 行的垂直偏移"——这是 u5-l2 的核心难点，提前思考会很有帮助；
- 如果你想自己准备测试图而不是用自带图，可以预习 u5-l3 会讲的 `write_ascii_ppm.m`，用它把任意图像转成 testbench 需要的 P3 格式。
