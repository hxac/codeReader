# 自校验测试台与逐像素比对

## 1. 本讲目标

上一篇（u5-l1）我们读完了 `sim_sharp.vhd`：它能把一张 PPM 输入图喂给顶层 `sharp`，再把输出像素写回成一张锐化图——但**最终对不对，要靠人眼去看图**。本篇要解决的是「让仿真器自己判 PASS/FAIL」。

精读 `sim_sharp_self-checking.vhd` 之后，你应当能够：

1. 说清「期望图像（expected）」是什么、它从哪里来，以及测试台如何把仿真输出与它**逐像素**比对。
2. 解释为什么比对前要补偿 **3 行垂直偏移**，以及为什么要**跳过图像左/右/上边缘**。
3. 看懂 `mismatch` 计数如何在两个并发进程间累加，并在仿真结束时给出 `EVERYTHING OK` 或 `MISMATCHES` 的最终判决。

## 2. 前置知识

在进入本讲前，你需要已经掌握以下概念（前序讲义已建立）：

- **PPM(P3) 与 textio**：测试台用 VHDL `textio` 读写 ASCII 格式 PPM 图像（u5-l1）。
- **视频时序 vs/hs/de**：`de` 标记有效像素，同时充当行存储写使能（u3-l2、u4-l1）。
- **可分离二维滤波的硬件结构**：`sharp_slice` 先用 6 块 `sharp_linemem` 级联得到 7 个**垂直抽头** `v_tap(0..6)`，再用 6 级移位寄存器得到 7 个**水平抽头** `h_tap(0..6)`，两次 `sharp_arith` 串联完成「先垂直后水平」的二维卷积（u3-l3）。
- **窗口中心与延迟**：输出 `data_out` 对应原图第 `(R-3, C-3)` 格，即 7×7 窗口的中心；中心抽头 `v_tap(3)` 落在当前输入行的 **3 行之前**（u3-l3、u4-l1）。这条结论是本讲「3 行垂直偏移」的直接来源。
- **同步延迟对齐**：`sharp_control` 用 `delay=6` 把 `de_out` 与输出像素在**时钟级（子行级）**对齐（u3-l2）。

一个新术语：**自校验测试台（self-checking testbench）**。普通测试台只产生激励、把响应留给人看；自校验测试台则在仿真内部持有一份「标准答案」（expected），每个输出像素都与标准答案做比较，仿真结束自动报告通过还是失败，**不需要人去看图**——这是回归测试（regression test）能自动跑起来的前提。

## 3. 本讲源码地图

本讲只精读一个文件，但会与前一篇的测试台和 Octave 生成脚本对照。

| 文件 | 作用 |
| --- | --- |
| [sim_sharp_self-checking.vhd](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/Verification/sim_sharp_self-checking.vhd) | **本讲主角**。在 `sim_sharp` 基础上新增「读期望图 + 逐像素比对 + mismatch 计数 + 最终判决」，是自校验测试台。 |
| [sim_sharp.vhd](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/Verification/sim_sharp.vhd) | 上一篇的「看图型」测试台。本讲用它做对照，看出自校验版**多了什么**。 |
| [sharp_generate_testbench_images.m](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/Verification/sharp_generate_testbench_images.m) | Octave 脚本。对输入图做两次 `imfilter` 得到**软件参考结果**，写成输入 PPM 与期望 PPM 两份文件——期望图就从这儿来（详见 u5-l3）。 |

> 永久链接基址（当前 HEAD `3f7aef9`）：
> `https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/`

**与 `sim_sharp.vhd` 的关键差异（一眼看完）**：

1. 多了一个常量 `expected_filename`，指向软件参考图。
2. `response_process` 里**同时**打开两份文件：把仿真输出写进 `response_file`，从 `expected_file` 读标准答案。
3. 每个有效输出像素都与期望像素比对，不等就把共享信号 `mismatch` 加 1。
4. 仿真结束的 `assert` 不再只说 `"Simulation completed"`，而是按 `mismatch` 是否为 0 报告 `EVERYTHING OK` 或 `N MISMATCHES`。

理解了这四点差异，就理解了本讲全部新增内容。

## 4. 核心概念与源码讲解

### 4.1 期望图像逐像素比对

#### 4.1.1 概念说明

「期望图像」就是同一套锐化核 `[1,0,-9,48,-9,0,1]/32` 用**软件**（Octave `imfilter`）算出来的结果。它由 `sharp_generate_testbench_images.m` 写成 `Lindau_Harbour_expected.ppm`：

```matlab
img_tmp = imfilter(img_in, f_ver);   % 垂直
img_out = imfilter(img_tmp, f_hor);  % 水平
write_ascii_ppm(img_out, "Lindau_Harbour_expected.ppm");
```

见 [sharp_generate_testbench_images.m:17-21](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/Verification/sharp_generate_testbench_images.m#L17-L21)。这正好是 u2-l1 讲的「先垂直后水平」可分离滤波的软件原型。

于是我们有了一条**软硬件对照链**：同一组系数，软件算一遍得到 expected，硬件（`sharp`）算一遍得到 response，逐像素比对。两者一致就证明硬件实现正确。这就是「自校验」的本质——把人的肉眼判断换成机器的逐像素相等判断。

逐像素比对的含义很简单：对于每一个有效输出像素 `(r, g, b)`，找到期望图里**对应位置**的 `(r_ex, g_ex, b_ex)`，三个通道逐一比较；只要有一个通道不等，就是一个 mismatch。

#### 4.1.2 核心流程

`response_process` 每个时钟沿检查 `de_out`，有效时做三件事：

```
每个 falling_edge(clk):
  若 de_out = '1':                      # 一个有效输出像素
    1) 读 r_out/g_out/b_out → r_sim/g_sim/b_sim
    2) 把 (r_sim g_sim b_sim) 写进 response 文件   # 始终写，留底
    3) 若已过垂直偏移补偿点:
       从 expected 文件读一个像素 (r_ex, g_ex, b_ex)
       若不在要跳过的边缘区:
          若 (r_sim,g_sim,b_sim) != (r_ex,g_ex,b_ex):
             mismatch <= mismatch + 1   # 记一笔
    4) x_pos++；行末则换行 y_pos++
```

关键在于步骤 3 的两个「若」——它们分别处理**垂直偏移**和**边缘跳过**，是下一节（4.2）的主题。本节先把「读期望 + 比对」的主干看清。

注意一个细节：**期望文件是按输出节奏逐像素读取的**，不是一次读进内存。两个文件（response 写、expected 读）在同一个进程里、按同一个 `x_pos/y_pos` 节奏同步推进，省去了存整张图的内存。

#### 4.1.3 源码精读

文件常量定义了三份文件名——输入、期望、响应——以及水平消隐和收尾拍数：

[sim_sharp_self-checking.vhd:22-27](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/Verification/sim_sharp_self-checking.vhd#L22-L27) —— 定义 `stimuli_filename`/`expected_filename`/`response_filename` 三份文件、`x_blank=100` 水平消隐、`trail=1000` 收尾时钟周期。`expected_filename` 是本版相对 `sim_sharp.vhd` 新增的常量。

`response_process` 里为两份文件分别声明了独立的行缓冲变量与状态：

[sim_sharp_self-checking.vhd:177-189](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/Verification/sim_sharp_self-checking.vhd#L177-L189) —— `l_sim`/`r_sim/g_sim/b_sim` 服务于写响应文件；`l_ex`/`r_ex/g_ex/b_ex` 与 `x_size_expected/y_size_expected` 服务于读期望文件。`x_pos, y_pos` 是**输出像素的行列计数器**，初始为 0。

写响应文件头与读期望文件头的对照（两个文件都先跳过 P3 头四行）：

[sim_sharp_self-checking.vhd:197-222](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/Verification/sim_sharp_self-checking.vhd#L197-L222) —— 先写 response 文件的 `P3` 头（magic number、注释、`x_size y_size`、`255`），再打开 expected 文件并读它的头四行。读头时还做了一道**尺寸一致性检查**：

[sim_sharp_self-checking.vhd:216-220](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/Verification/sim_sharp_self-checking.vhd#L216-L220) —— 若期望图的宽高与输入图不符，立即 `assert ... severity failure` 终止仿真并报告 `FAILURE: image size of expected values does not match stimuli`。这是自校验的第一道防线：**在比对之前先确认两份图可比**，避免拿大小不一样的图硬比造成无意义的海量 mismatch。

逐像素比对的核心循环（主干，边缘跳过的判断留到 4.2 详讲）：

[sim_sharp_self-checking.vhd:224-237](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/Verification/sim_sharp_self-checking.vhd#L224-L237) —— `while (end_tb /= 1)` 主循环，每个 `falling_edge(clk)` 检查 `de_out='1'`；有效则把 `r_out/g_out/b_out` 经 `to_integer(unsigned(...))` 转成整数 `r_sim/g_sim/b_sim`，写成一行 `"r g b"` 写进响应文件。这部分与 `sim_sharp.vhd` 完全相同——**自校验版保留了「写响应图」的全部能力**，只是额外追加了比对。

读期望像素与比较（提取关键三行）：

```vhdl
read(l_ex, r_ex); read(l_ex, g_ex); read(l_ex, b_ex);   -- L244-247
...
if (r_sim /= r_ex) or (g_sim /= g_ex) or (b_sim /= b_ex) then   -- L250
   mismatch <= mismatch + 1;                                      -- L251
```

见 [sim_sharp_self-checking.vhd:244-255](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/Verification/sim_sharp_self-checking.vhd#L244-L255)。三通道任一不等即记一笔 mismatch，并用 `assert ... severity note` 在日志里打印该像素坐标，方便定位。

#### 4.1.4 代码实践（源码阅读型）

**目标**：把 u5-l1 的 `sim_sharp.vhd` 与本讲的 `sim_sharp_self-checking.vhd` 并排对比，亲手找出所有新增/改动行。

**步骤**：

1. 同时打开两个文件。
2. 用编辑器的逐行对比（或 `git diff`-式阅读），找出 `response_process` 中**只在自校验版出现**的代码块。
3. 把这些新增块按功能归成三类：① 打开/读 expected 文件；② 逐像素比较与 mismatch 计数；③ 结束时的条件判决。
4. 注意 `sim_sharp.vhd` 里其实**也声明了** `signal mismatch : integer := 0;`（L48/L49 两版都有），但旧版从未使用它——这是作者为升级到自校验预留的「钩子」。确认这一点。

**需要观察的现象**：旧版的结束 assert 只有一句 `report "Simulation completed"`（[sim_sharp.vhd:163-165](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/Verification/sim_sharp.vhd#L163-L165)）；自校验版则根据 `mismatch` 二选一报告。

**预期结果**：你能列出至少 3 处新增代码块，并解释每处的作用。无需运行仿真。

#### 4.1.5 小练习与答案

**练习 1**：为什么 expected 文件用「逐像素现读」而不是仿真一开始就整张读进一个大数组？

> **答案**：VHDL `textio` 没有现成的大数组便利读取，且图像分辨率大（1280×720）时整张存数组会占用大量仿真内存、变量声明也繁琐。按输出节奏逐像素读，response 写与 expected 读共用同一个 `x_pos/y_pos` 节奏，简洁且省内存。

**练习 2**：尺寸检查（L216-220）为什么放在读期望图**头部**之后、进入主循环**之前**？

> **答案**：头部第 3 行才含 `x_size y_size`，必须先读到才能比较；又必须在开始逐像素比对之前就拦截，否则尺寸不符会导致两个文件的像素错位读取，产生大量无意义 mismatch，掩盖真正的问题。

---

### 4.2 垂直偏移补偿与边缘跳过

#### 4.2.1 概念说明

这是本讲最容易出错、也最关键的一节。它回答一个问题：**为什么不直接把第 N 个输出像素和期望图的第 N 个像素比？** 答案是两类「对不齐」：

1. **整行级的垂直偏移（3 行）**。回顾 u3-l3/u4-l1：输出 `data_out` 对应原图 `(R-3, C-3)`，即 7 抽头垂直滤波的中心抽头 `v_tap(3)` 落在当前输入行的 **3 行之前**。因此**硬件输出第 N 行携带的是原图第 N−3 行的滤波结果**——整整改了 3 整行。`sharp_control` 的 `delay=6` 只能处理**时钟级（子行级）**对齐，搬不动「3 整行」这种粗粒度偏移，必须由测试台显式补偿。

   量化地说：设输出行号为 \(N\)、期望行号为 \(E\)，则 \(N = E + 3\)，即

   \[
   \text{hardware}(N,\cdot) \;\leftrightarrow\; \text{expected}(N-3,\cdot)
   \]

2. **边缘像素本身算不准**。在图像左/右/上边缘，7×7 窗口会伸出图像之外：硬件的行存储器初始为空、移位寄存器也未填满，边缘输出是「半窗口」结果；而 Octave `imfilter` 默认用 **0 填充（zero-padding）** 处理边缘，给出的是另一种边界结果。两者算法不同，边缘必然对不上——**这不是硬件错，而是边界条件不可比**，所以必须跳过。

结论：比对只在「窗口完整、且已补偿垂直偏移」的内部区域进行。测试台用两个嵌套 `if` 分别处理这两件事。

#### 4.2.2 核心流程

两个判断条件逐层把关：

```
if (y_pos > 2) then                 # ① 垂直偏移补偿：跳过前 3 行输出后再开始读 expected
   读一个期望像素
   if (x_pos > 2  and  x_pos < x_size-3  and  y_pos > 5) then   # ② 边缘跳过
      比较；不等则 mismatch++
   end if;
end if;
```

把它们映射到图像区域（设图像高 \(H\)、宽 \(W\)）：

| 条件 | 跳过的区域 | 原因 |
| --- | --- | --- |
| `y_pos > 2`（输出行号 > 2） | 输出最上方 3 行 | 垂直滤波 3 行偏移：让 expected 行 0 对齐输出行 3 |
| `y_pos > 5`（输出行号 > 5） | 输出最上方 6 行（含上面 3 行） | 上边缘窗口伸出图外，硬件/软件边界处理不同 |
| `x_pos > 2` | 最左 3 列 | 水平窗口左半伸出图外 |
| `x_pos < x_size-3` | 最右 3 列（约） | 水平窗口右半伸出图外 |

注意 ① 和 ② 在「上边缘」上是**叠加**的：`y_pos > 2` 让 expected 读指针晚 3 行起步（补偿偏移）；`y_pos > 5` 又额外把输出第 3~5 行（对应 expected 第 0~2 行，即 expected 的最上 3 行）排除在比对之外。合起来，**输出最上方 6 行都不参与比对**，其中前 3 行用于偏移补偿、后 3 行用于上边缘容忍。

为什么水平方向**没有**类似的偏移补偿、只有边缘跳过？因为水平移位寄存器造成的列偏移是**时钟级（子行级）**的，已被 `sharp_control` 的 `delay=6` 在 `de_out` 与输出像素之间对齐掉了（见 u3-l2）；只有「整行」级别的垂直偏移才需要测试台手工补 3 行。

#### 4.2.3 源码精读

垂直偏移补偿的「门」——只有过了输出第 3 行才开始读 expected：

[sim_sharp_self-checking.vhd:239-241](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/Verification/sim_sharp_self-checking.vhd#L239-L241) —— 注释原文写得很清楚：`output of VHDL is shifted verticaly by three lines / compensate by start reading expected image in line 3`。即输出第 3 行（`y_pos=3`）才开始读 expected，从而让 expected 第 0 行对齐输出第 3 行。

边缘跳过的「门」——左/右各 3 列、上方共 6 行之内不比对：

[sim_sharp_self-checking.vhd:248-249](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/Verification/sim_sharp_self-checking.vhd#L248-L249) —— `if (x_pos > 2 and x_pos < x_size-3 and y_pos > 5) then`，注释 `do not check left, right and top edge of image`。这三个边界分别对应水平左半窗、水平右半窗、上方窗口伸出图外的区域。

比对与计数（已在上节引用 L244-255，此处关注其与两个 `if` 的嵌套关系）：

[sim_sharp_self-checking.vhd:239-257](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/Verification/sim_sharp_self-checking.vhd#L239-L257) —— 完整展示「外层 `y_pos>2` 读 expected → 内层边缘判断 → 比较 → mismatch++」的三层嵌套。注意 `mismatch <= mismatch + 1` 用的是**信号赋值** `<=`（不是变量 `:=`），因为 `mismatch` 是跨进程共享的 architecture 级信号（见 4.3）。

输出像素的行列计数（每个有效像素推进一次）：

[sim_sharp_self-checking.vhd:259-263](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/Verification/sim_sharp_self-checking.vhd#L259-L263) —— `x_pos` 每像素加 1，到达 `x_size` 则换行（`y_pos+1`、`x_pos` 归零）。这正是上面所有 `x_pos/y_pos` 判断所依赖的坐标来源。

#### 4.2.4 代码实践（动手改边界）

**目标**：通过**收紧边缘跳过范围**，亲手制造 mismatch，从而验证「边缘本来就对不上」这一论断。

**步骤**：

1. 复制一份 `sim_sharp_self-checking.vhd` 作为实验副本。
2. 把第 248 行的边缘判断改成更窄的跳过，例如把 `x_pos > 2` 改成 `x_pos > 0`、把 `y_pos > 5` 改成 `y_pos > 2`（即少跳边缘、多比对）。
3. 准备好三份 PPM：输入图、expected 图（由 `sharp_generate_testbench_images.m` 生成）、并保留 response 输出路径。
4. 运行仿真（GHDL/ModelSim/Quartus 仿真器均可，参考 u5-l1 的时钟与 textio 用法）。
5. 观察仿真日志里 `MISMATCH in simulation at position x=... y=...` 的坐标分布。

**需要观察的现象**：mismatch 会**明显增多**，且新增 mismatch 集中出现在**图像边缘**（很小的 `x_pos`、很小的 `y_pos`，以及接近 `x_size` 的右侧列）。

**预期结果**：日志中出现大量位于边缘的 mismatch，`mismatch` 计数显著大于 0；最终报告变为 `... N MISMATCHES ...`。这反向证明：原版的边缘跳过范围（左/右 3 列、上 6 行）是必要的。

**待本地验证**：具体 mismatch 数量取决于测试图内容与仿真器；如果你用的 expected 图边界恰好大面积平坦，边缘 mismatch 可能偏少。关键看「坐标是否落在边缘带」。

> 反向操作：如果你把边缘跳过改得**更宽**（如 `x_pos > 5 and x_pos < x_size-6 and y_pos > 8`），mismatch 应趋于 0，但你也丢掉了更多本可比对的内部区域——这是「严格性 vs 鲁棒性」的取舍。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `y_pos > 2` 误删（直接从输出第 0 行就读 expected），会出现什么现象？

> **答案**：expected 的读取会整体**提前 3 行**，于是每个输出像素都和一个错位 3 行的期望像素相比，几乎**每个内部像素都 mismatch**，计数爆炸。这正是垂直偏移必须补偿的原因。

**练习 2**：为什么左边缘跳过用 `x_pos > 2`（3 列），而上方跳过合计是 6 行（`y_pos > 5`）？两者不对称的依据是什么？

> **答案**：水平方向只有「边缘窗口伸出」需要跳过，3 列 = 水平 7 抽头的半窗宽，故 `x_pos > 2`。垂直方向除了同样的 3 行半窗外，**还要再加 3 行偏移补偿**（`y_pos > 2` 那层），两者叠加得 6 行，故比对门是 `y_pos > 5`。3 列 vs 6 行的不对称，正源于垂直方向多了一份整行级偏移。

**练习 3**：代码里**没有**对图像**下边缘**（很大的 `y_pos`）做跳过。这会带来什么潜在问题？

> **答案**：单帧仿真下，接近底部的几行其垂直窗口会伸出图像下沿，硬件行存储里此时已是有效数据但 Octave 那侧 zero-padding，二者同样不可比，却**会被比对**，可能贡献少量底部 mismatch。作者在此权衡中接受这一点（或所选测试图底部平坦，影响可忽略）。若你换用底部有强边缘的测试图，可能需要在下方也加跳过。

---

### 4.3 mismatch 计数与通过/失败报告

#### 4.3.1 概念说明

前两节解决了「比什么、在哪比、怎么比」。本节解决最后一个问题：**比完之后，怎么收尾、怎么判 PASS/FAIL**。

设计要点有三个：

1. **`mismatch` 是跨进程共享信号**。它在 `response_process` 里被累加（每发现一处不等就 +1），却要在 `stimuli_process` 末尾被读取并据此判决。两个进程靠这个共享信号传递「累计错误数」。
2. **mismatch 报告用 `severity note`**。每发现一处不等只打一条 `note` 级日志，**不中断仿真**——这样才能在一帧里把所有错误都数完，而不是遇到第一个就停。
3. **最终判决用 `assert false ... severity failure`**。仿真末尾的 `assert false` 永远为假，必然触发；`severity failure` 会让仿真器停止仿真。判决消息根据 `mismatch` 是否为 0 二选一，于是「仿真停止 + 一目了然的结论」一举两得。

> 小术语：**`assert false report "..." severity failure`** 是 VHDL 测试台里**强制结束仿真**的惯用法。`assert false` 不依赖任何条件（恒假），`severity failure` 在绝大多数仿真器（GHDL、ModelSim、Questa 等）里默认会终止运行。

#### 4.3.2 核心流程

```
# response_process（响应进程）
while end_tb /= 1:                      # 持续接收输出
  发现不等 → mismatch <= mismatch + 1   # 累加，severity note 不停仿真

# stimuli_process（激励进程）末尾
发完一帧 + trail 拍收尾
end_tb <= 1                             # 通知响应进程关闭文件
file_close(stimuli_file); wait 20 ns
if mismatch = 0:
   assert false report "EVERYTHING OK"           severity failure  # PASS 且停仿真
else:
   assert false report "<mismatch> MISMATCHES"   severity failure  # FAIL 且停仿真
```

两个进程的**结束协作**值得注意：

- 真正「杀死」仿真的是 **`stimuli_process` 末尾**那个 `assert false ... severity failure`（L164-172）。
- `response_process` 收到 `end_tb=1` 后退出 `while`、关闭两个文件（L268-269），然后执行 `wait until (end_tb = 2)`（L270）——而 `end_tb` 永远不会变成 2，所以这条 `wait` 是**永久挂起**，响应进程安静地停在那里，把「结束」权交给激励进程。
- `end_tb <= 1`（L159）之所以要在 `assert` 之前发出，是为了**先把响应文件刷盘关闭**（`file_close` 在 L268-269），否则响应图可能写不全。

#### 4.3.3 源码精读

`mismatch` 与 `end_tb` 都是 architecture 级共享信号（旧版 `sim_sharp.vhd` 也声明了 `mismatch` 但没用，自校验版才真正用起来）：

[sim_sharp_self-checking.vhd:48-49](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/Verification/sim_sharp_self-checking.vhd#L48-L49) —— `signal end_tb : integer := 0;` 与 `signal mismatch : integer := 0;`。两者都跨 `stimuli_process` / `response_process` 共享。

激励进程末尾的「通知 + 收尾 + 判决」三段式：

[sim_sharp_self-checking.vhd:159-172](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/Verification/sim_sharp_self-checking.vhd#L159-L172) —— 先 `end_tb <= 1` 通知响应进程关文件，再 `file_close(stimuli_file)`、`wait for 20 ns` 给响应进程留出刷盘时间，最后按 `mismatch` 是否为 0 选择 `EVERYTHING OK` 或 `integer'image(mismatch) & " MISMATCHES"`，统一用 `severity failure` 结束仿真。注意 `integer'image(mismatch)` 把计数值转成字符串拼进报告，于是失败信息里直接带 mismatch 总数。

响应进程里 mismatch 的累加（已见于 4.2，这里聚焦其 severity 与坐标报告）：

[sim_sharp_self-checking.vhd:250-255](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/Verification/sim_sharp_self-checking.vhd#L250-L255) —— 比较不等则 `mismatch <= mismatch + 1` 并用 `assert false report "MISMATCH in simulation at position x=.. y=.." severity note` 记录坐标。`severity note` 关键：它只留日志、不停仿真，保证一帧内所有 mismatch 都能被数到。

响应进程的「永久挂起」收尾：

[sim_sharp_self-checking.vhd:268-270](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/Verification/sim_sharp_self-checking.vhd#L268-L270) —— `file_close(response_file)` 与 `file_close(expected_file)` 确保两份文件落盘，随后 `wait until (end_tb = 2)` 因 `end_tb` 永不变 2 而永久挂起，把结束权完整交给激励进程的 `severity failure`。

#### 4.3.4 代码实践（注入错误，验证判决）

**目标**：故意让硬件算错，确认测试台真的能报出 `MISMATCHES`；再恢复，确认能报 `EVERYTHING OK`。这是验证「自校验」是否生效的最直接实验。

**步骤**：

1. **注入错误**：打开 `FPGA-Design/sharp_arith.vhd`，找到定点乘加表达式里的中心系数 `48`（这是 u4-l2 讲过的锐化核中心项）。临时把它改成 `47`（偏离 1），保存。这相当于换了一个略微不同的滤波核，硬件输出将与 expected 系统性偏离。
2. 重新编译 `sharp_arith.vhd` 与测试台，运行仿真。
3. 观察仿真日志与最终报告。
4. **恢复**：把 `47` 改回 `48`，重新编译并仿真，再次观察报告。

**需要观察的现象**：

- 改成 `47` 后：日志里出现大量 `MISMATCH in simulation at position ...`（且坐标分布在图像内部各处，尤其边缘附近），最终报告形如 `Simulation completed, <N> MISMATCHES XXXX ...`。
- 改回 `48` 后：日志干净（或仅有极个别可忽略的边缘 mismatch），最终报告 `Simulation completed, EVERYTHING OK`。

**预期结果**：注入错误时 `mismatch > 0`、报告 FAIL；恢复后 `mismatch = 0`、报告 PASS。这证明整条「读 expected → 逐像素比对 → 计数 → 判决」链路确实在工作。

**待本地验证**：偏离 1 时是否每个内部像素都 mismatch，取决于该像素邻域是否对中心系数敏感（平坦区可能不变）。若你想强制看到大量 mismatch，可把 `48` 改成更悬殊的值（如 `40`），或在 `sharp_arith.vhd` 里临时去掉 `+16` 舍入项（u4-l2 讨论过其作用），都会引发系统性偏差。

> 工程提示：这是**回归测试**的标准用法——以后你每改一次硬件（u6-l3 的二次开发），都跑一遍自校验测试台，只要仍报 `EVERYTHING OK`，就说明改动没破坏正确性。

#### 4.3.5 小练习与答案

**练习 1**：为什么 mismatch 报告用 `severity note` 而最终判决用 `severity failure`？

> **答案**：`note` 不停仿真，能在一帧里把所有 mismatch 数完；`failure` 在末尾强制停止仿真并给出唯一结论。前者负责「收集证据」，后者负责「宣判并收尾」，分工明确。

**练习 2**：`mismatch` 是信号（`signal`）而非变量（`variable`），且在 `response_process` 里用 `<=` 累加。这在时序上有什么需要注意的？

> **答案**：信号赋值要到下一个 delta 周期才生效。好在本设计里每个 mismatch 判断都隔着一个 `wait until falling_edge(clk)`（一个时钟周期才比一个像素），远大于 delta 周期，所以累加不会「撞车」。但若你把多个比较塞进同一时钟沿，信号赋值的延迟特性会导致漏计——届时应改用变量。本测试台不存在这个问题。

**练习 3**：若删掉 `stimuli_process` 末尾的 `assert false ... severity failure`，仿真会怎样结束？

> **答案**：激励进程会正常执行到 `end process` 后挂起（没有更多语句），响应进程也在 `wait until (end_tb = 2)` 永久挂起——两个进程都静默等待，仿真器通常会因「无事件」而自然停机或一直空转，但你**得不到**那条 `EVERYTHING OK / N MISMATCHES` 的明确结论。所以这个 `assert` 既是「判决书」也是「终止信号」，缺一不可。

---

## 5. 综合实践

把本讲三块知识串起来，做一个完整的「自校验闭环」小任务。

**背景**：假设你接受了一个改动需求——把锐化核的中心系数从 `48` 微调为 `50`（让锐化更强一点）。在交付前，你要用自校验测试台证明：改动后的硬件**与新的软件参考一致**。

**任务步骤**：

1. **改算法侧（Octave）**：在 `sharp_filter_coefficients.m` 或一个临时脚本里，构造一个中心为 `50` 的新核（注意要让系数和仍合理；可参考 u2-l2 的「叠加恒等核」思路调整其它项，或先不管直流增益只看相对效果）。用 `sharp_generate_testbench_images.m` 重新生成**新的** `Lindau_Harbour_720p.ppm`（输入不变）与**新的** `Lindau_Harbour_expected.ppm`（期望图变了）。
2. **改硬件侧（VHDL）**：把 `sharp_arith.vhd` 里乘加表达式的 `48` 同步改成 `50`，与软件核保持一致。
3. **跑自校验**：运行 `sim_sharp_self-checking.vhd`，三份文件名对齐（输入、新 expected、response）。
4. **判读**：
   - 若报告 `EVERYTHING OK`——软硬件一致，改动交付成功。
   - 若报告 `N MISMATCHES`——先看 mismatch 坐标是否**全在边缘带**（左/右 3 列、上 6 行）；若是，多半是 expected 图的边界处理与硬件不同（4.2 讨论过），可视为可接受；若 mismatch 落在图像**内部**，说明软硬件核没对齐，回到第 1/2 步检查。
5. **反思**：写下一句话——为什么这个流程里**必须同时**改软件 expected 和硬件系数，只改一边会怎样？

**参考答案（反思）**：自校验比对的本质是「硬件输出 vs 软件参考」。只改硬件不改 expected，等于拿旧答案对价新硬件，必然 FAIL；只改 expected 不改硬件，则是在测一个没实现的核。二者必须同步——这正是 u5-l3 要讲的「软硬件结果对接」的核心。

**待本地验证**：本综合实践需要可用的 Octave 与 VHDL 仿真器；具体 mismatch 分布与数量依赖你的新核设计与测试图，无法在此给出确定数值。

## 6. 本讲小结

- 自校验测试台 = 普通测试台 + **期望图逐像素比对** + **mismatch 计数** + **自动 PASS/FAIL 判决**；期望图来自 Octave `imfilter` 的软件参考（u5-l3 详讲）。
- 比对在 `response_process` 内随输出节奏逐像素进行：写响应图（保留看图能力）的同时，从 expected 文件现读一个像素做三通道比较。
- **垂直偏移 3 行**源于 7 抽头垂直滤波的中心抽头 `v_tap(3)` 落在当前输入行 3 行之前，用 `y_pos > 2` 显式补偿（`delay=6` 只管时钟级、管不了整行级）。
- **边缘跳过**（左/右各 3 列 `x_pos > 2` 与 `x_pos < x_size-3`、上方合计 6 行 `y_pos > 5`）是因为边界处硬件行存储/移位寄存器未填满，与 Octave zero-padding 不可比。
- `mismatch` 是跨进程共享信号，累加用 `severity note`（不停仿真）、最终判决用 `assert false ... severity failure`（停仿真 + 给结论）；响应进程靠 `wait until (end_tb = 2)` 永久挂起，把结束权交给激励进程。

## 7. 下一步学习建议

- **下一篇 u5-l3**：精读 `sharp_generate_testbench_images.m` 与 `write_ascii_ppm.m`，搞清 expected 图到底怎么一行行写成 P3 PPM，把本讲里「expected 从哪儿来」这一环彻底补上，完成软硬件对照闭环。
- **横向回看 u6-l3**：当你做「换一个滤波核」的二次开发时，本讲的自校验测试台就是你的回归测试工具——每改一次系数，都跑一遍 `EVERYTHING OK`。
- **延伸阅读**：对照 [sim_sharp.vhd](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/Verification/sim_sharp.vhd) 与 [sim_sharp_self-checking.vhd](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/Verification/sim_sharp_self-checking.vhd) 两个文件，体会从「看图型」到「自校验型」测试台的演进，这是从「能跑」到「能回归」的工程化关键一步。
