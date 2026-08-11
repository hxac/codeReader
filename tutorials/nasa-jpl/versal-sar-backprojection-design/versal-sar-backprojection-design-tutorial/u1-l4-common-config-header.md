# 全局配置中心：design/common.h

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 `design/common.h` 里那几个决定「设计规模」的宏（`PULSES`、`RC_SAMPLES`、`AIE_SWITCHES`、`IMG_SOLVERS_PER_SWITCH`、`IMG_SOLVERS`、`BC_ELEMENTS`）各自代表什么、彼此如何派生。
- 用一个整除方程 `(RC_SAMPLES*PULSES)/IMG_SOLVERS` 解释「为什么改一个参数就可能让整个设计崩掉」，并能手算默认配置下每个图像重建内核分到多少像素。
- 理解 `C`、`MIN_FREQ`、`RANGE_FREQ_STEP`、`RANGE_RES` 这一组雷达物理常数的来源，以及 `RANGE_RES`（距离分辨率）是怎么一步步推导出来的。
- 明白 `common.h` 为什么必须被 Host（ARM）、AIE（AI Engine）、PL（FPGA）三个域同时 `#include`，以及一旦三域「不同步」会发生什么。

本讲只读一个头文件，但这个头文件是整个项目的「单一真相源」（single source of truth）。

## 2. 前置知识

阅读本讲前，建议你已经：

- 知道本仓库按 Versal 三引擎分了三个目录：`design/aie/`（AI Engine 反投影内核）、`design/pl/`（FPGA 上的 HLS 包路由器）、`design/host/`（ARM 控制程序）。这一点在 [u1-l2 仓库结构](u1-l2-repo-structure-and-test-data.md) 已讲过。
- 大致了解 SAR 反投影要对「每个像素 × 每个脉冲」算一次双程时延，所以总计算量正比于「像素数 × 脉冲数」。背景见 [u1-l1 项目总览](u1-l1-project-overview.md)。
- 知道 `PULSES`（脉冲数）和 `RC_SAMPLES`（距离压缩样本数）这两个量会在 u1-l2 提到的 GOTCHA 数据集里出现。

另外，本讲会用到一点点「因式分解」来判断整除性，初中代数程度即可。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 | 本讲关注点 |
|------|------|-----------|
| [design/common.h](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/common.h) | 三域共享的唯一配置头文件 | 所有宏与物理常数的定义 |
| [design/aie/backprojection.cc](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc) | AIE 反投影内核实现 | 用整除约束算「每核像素数」、用物理常数算相位 |
| [design/host/sar_backproject.cpp](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.cpp) | 主机端控制程序 | 用宏来分配 buffer、读 CSV、编排循环 |
| [design/pl/dma_pkt_router.cpp](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/pl/dma_pkt_router.cpp) | PL 包路由器 HLS 内核 | 用宏算每核 DDR 偏移 |
| [Makefile](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile) | 构建系统 | 用 `grep` 从 common.h 读宏来生成 `system.cfg`、选 CSV |

注意：本讲核心只读 `design/common.h`，其余文件仅作为「这些宏到底被谁用了」的证据来引用。

---

## 4. 核心概念与源码讲解

### 4.1 决定设计规模的几个宏

#### 4.1.1 概念说明

反投影算法的总工作量是「像素数 × 脉冲数」量级，这个量非常大（详见 u1-l1）。为了让它跑得快，本项目把工作**均分**给大量 AIE 内核并行处理。于是就需要一组宏来描述：

- 一共要处理多少脉冲 → `PULSES`
- 每个脉冲有多少个距离样本（也就是图像的列数）→ `RC_SAMPLES`
- AIE 阵列被分成多少组「交换块（switch）」→ `AIE_SWITCHES`
- 每个交换块里挂多少个「图像重建内核」→ `IMG_SOLVERS_PER_SWITCH`
- 总共有多少个图像重建内核 → `IMG_SOLVERS`（这个不是手填的，是派生出来的）
- 每次要广播给所有内核的几何参数有几个 → `BC_ELEMENTS`

这几组宏一旦定下来，Host 怎么分配内存、AIE 怎么切分工作、PL 怎么把结果拼回去，就全都跟着定了。这就是为什么 `common.h` 是整个项目的「配置中心」。

#### 4.1.2 核心流程

这几个宏的关系可以用下面这张「派生图」表示：

```text
PULSES ─────────────┐  (脉冲数 = 图像行数)
                    ├──► 总像素数 = PULSES × RC_SAMPLES
RC_SAMPLES ─────────┘  (距离样本数 = 图像列数)

AIE_SWITCHES ─────────────┐
                          ├──► IMG_SOLVERS = AIE_SWITCHES × IMG_SOLVERS_PER_SWITCH
IMG_SOLVERS_PER_SWITCH ───┘   (图像重建内核总数，派生量)
                    │
                    └──► 每核像素数 = 总像素数 / IMG_SOLVERS   ← 必须是整数！(见 4.2)

BC_ELEMENTS = 4   (X,Y,Z,ref_range 四个几何量，随每脉冲广播)
```

关键点是：`IMG_SOLVERS` 不是一个独立手填的值，而是 `AIE_SWITCHES * IMG_SOLVERS_PER_SWITCH` 算出来的。源码里也明确这么写了。

#### 4.1.3 源码精读

先看规模宏的定义。注意每条注释都说明了「这个数同时还是输出图像的行/列数」：

[design/common.h:17](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/common.h#L17) 定义 `PULSES 602`，注释说它「也将是输出图像的行数」。

[design/common.h:22](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/common.h#L22) 定义 `RC_SAMPLES 512`，注释说它「也将是输出图像的列数」，并强调测试数据只支持 512/256/128/64 这几个取值。

[design/common.h:31](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/common.h#L31) 定义 `AIE_SWITCHES 7`。

[design/common.h:35](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/common.h#L35) 定义 `IMG_SOLVERS_PER_SWITCH 32`，注释强调它**必须是 2 的幂**，且**每个 switch 最多 32 个**。

然后是关键的派生关系：

[design/common.h:38](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/common.h#L38) 把 `IMG_SOLVERS` 直接定义成两个量的乘积：

```cpp
#define IMG_SOLVERS (AIE_SWITCHES*IMG_SOLVERS_PER_SWITCH)
```

也就是说，`IMG_SOLVERS` 在默认配置下等于 \(7 \times 32 = 224\)。**你不需要、也不应该单独改它**——改 `AIE_SWITCHES` 或 `IMG_SOLVERS_PER_SWITCH` 即可。

最后是广播元素数：

[design/common.h:45](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/common.h#L45) 定义 `BC_ELEMENTS 4`，注释列出了这 4 个元素：天线 X、Y、Z 位置，以及到场景中心的参考距离（`ref_range`）。这就是 slowtime CSV 每行 4 列的由来（u1-l2 讲过）。

#### 4.1.4 代码实践

**实践目标**：把「默认配置下整个设计有多少个图像重建内核」这件事在源码里跑通一遍。

**操作步骤**：

1. 打开 `design/common.h`，读出 `AIE_SWITCHES`、`IMG_SOLVERS_PER_SWITCH` 的值。
2. 按 [common.h:38](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/common.h#L38) 的派生式心算 `IMG_SOLVERS`。
3. 打开 `design/aie/graph.h`，搜索 `bpCluster`，确认顶层图里确实实例化了 `AIE_SWITCHES` 个 `bpCluster`、每个 cluster 里又有 `IMG_SOLVERS_PER_SWITCH` 个重建内核。

**需要观察的现象**：`graph.h` 里 `kernel img_rec_km[IMG_SOLVERS_PER_SWITCH];` 这个数组的长度，以及顶层 `BackProjectionSubgraph bpCluster[AIE_SWITCHES];` 的长度，两者相乘正好等于你算出的 `IMG_SOLVERS`。

**预期结果**：默认配置下 `IMG_SOLVERS = 224`，即整个 AIE 阵列上有 224 个图像重建内核在并行工作。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `IMG_SOLVERS_PER_SWITCH` 从 32 改成 16，`IMG_SOLVERS` 会变成多少？需要同时改 `common.h` 的哪些地方？

**答案**：`IMG_SOLVERS = 7 × 16 = 112`。你**只需要**改 `IMG_SOLVERS_PER_SWITCH` 这一行；`IMG_SOLVERS` 是用宏乘法派生的，会自动跟着变。但注意注释说它「必须是 2 的幂、最大 32」，16 仍满足，合法。

**练习 2**：`RC_SAMPLES` 为什么只能是 512/256/128/64？

**答案**：因为测试数据 `gotcha_phdata_*-out-of-424-rc-samples_*.csv` 只为这几个值各生成了一份（u1-l2）。Makefile 会根据 `common.h` 里的 `RC_SAMPLES` 自动挑选对应文件名的 CSV（见 4.4）。改成别的值会找不到对应测试数据。

---

### 4.2 整除约束：每个内核的工作量必须整数

#### 4.2.1 概念说明

这是 `common.h` 里**最重要的一条隐性规则**。源码注释直接把这条方程写在了 `PULSES` 的定义上方：

> 工作被均分到所有 AIE 内核，所以这个数必须小心选择，不能有「剩下的零头」。每个图像重建内核要处理的像素数必须等于整数：
> `(RC_SAMPLES * PULSES) / (IMG_SOLVERS_PER_SWITCH * AIE_SWITCHES)`

为什么「不能有零头」？因为 AIE 的反投影内核是数据驱动（Kahn 进程网络）的：每个内核被预先分到固定数量的像素去累加。如果总像素数不能被内核数整除，C++ 整数除法会**直接截断**，被截掉的那部分像素就永远不会被处理，最终图像会缺失一块、出现错位。这不是编译错误，而是**静默的图像损坏**，极难排查。

#### 4.2.2 核心流程

约束可以写成一条数学等式。设：

\[ N_{\text{px}} = \text{PULSES} \times \text{RC\_SAMPLES} \quad (\text{图像总像素数}) \]
\[ N_{\text{kern}} = \text{IMG\_SOLVERS} = \text{AIE\_SWITCHES} \times \text{IMG\_SOLVERS\_PER\_SWITCH} \quad (\text{内核总数}) \]

则必须满足：

\[ \frac{N_{\text{px}}}{N_{\text{kern}}} \in \mathbb{Z} \quad\Longleftrightarrow\quad N_{\text{px}} \bmod N_{\text{kern}} = 0 \]

用因式分解判断整除性更直观。默认配置下：

\[ N_{\text{kern}} = 224 = 2^{5} \times 7, \qquad \text{RC\_SAMPLES} = 512 = 2^{9} \]

所以：

\[ \frac{\text{RC\_SAMPLES} \times \text{PULSES}}{N_{\text{kern}}} = \frac{2^{9} \times \text{PULSES}}{2^{5} \times 7} = \frac{2^{4} \times \text{PULSES}}{7} \]

也就是说，`RC_SAMPLES` 已经提供了足够的因子 2，整除条件**退化为：`PULSES` 必须是 7 的倍数**。默认 `PULSES = 602 = 2 × 7 × 43`，正好是 7 的倍数，满足约束。

> 注意：这个「必须是 7 的倍数」的结论是**默认配置下**（`AIE_SWITCHES=7`）的特殊情况。如果改了 `AIE_SWITCHES`，约束里的质因子也会变，需要重新做因式分解。

#### 4.2.3 源码精读

这条整除约束在三个域里都以**同一条表达式**出现，可见它的核心地位：

[design/common.h:11-L16](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/common.h#L11-L16) 是 `PULSES` 上方的注释，把约束方程白纸黑字写出来，提醒后来者。

AIE 内核里，每个图像重建内核要处理的像素数就是用这条除法算出来的：

[design/aie/backprojection.cc:68](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L68) —— `const int SAMPLES = (PULSES*RC_SAMPLES)/IMG_SOLVERS;`，这就是「每个内核分到的像素数」（行号待本地确认，因为它和 `SAMPLES` 的具体行可能随版本微调，但表达式确凿存在）。

同一文件里还有一处乘 3 的版本，因为每个像素有 X/Y/Z 三个分量：

[design/aie/backprojection.cc:21](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L21) —— `int px_components_per_ai = ((PULSES*RC_SAMPLES)/IMG_SOLVERS)*3;`。

Host 端则用「每个 demux（解复用）内核分到的像素数」——注意分母是 `AIE_SWITCHES` 而不是 `IMG_SOLVERS`，因为 demux 是按 switch 切分的：

[design/host/sar_backproject.cpp:285](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.cpp#L285) —— `int px_per_demux_kern = (PULSES*RC_SAMPLES)/AIE_SWITCHES;`。

PL 端的包路由器也用同一条除法来算每个内核写回 DDR 的偏移步长：

[design/pl/dma_pkt_router.cpp:18](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/pl/dma_pkt_router.cpp#L18) —— `const int SAMPLES_PER_KERN = (PULSES*RC_SAMPLES)/IMG_SOLVERS;`。

#### 4.2.4 代码实践

**实践目标**：手算默认配置下每个图像重建内核处理多少像素，并验证它确实是整数；再预测「把 `PULSES` 改成质数」会怎样。

**操作步骤**：

1. 代入默认值：`AIE_SWITCHES=7`、`IMG_SOLVERS_PER_SWITCH=32`、`RC_SAMPLES=512`、`PULSES=602`。
2. 算 `IMG_SOLVERS = 7 × 32 = 224`。
3. 算总像素 `N_px = 602 × 512 = 308224`。
4. 算每核像素 `308224 / 224`。

**预期结果**：

\[ \frac{602 \times 512}{224} = \frac{308224}{224} = 1376 \]

1376 是整数，约束成立。也就是说，224 个图像重建内核每个处理 1376 个像素，\(224 \times 1376 = 308224\)，一个像素都不漏。

**进阶：把 `PULSES` 改成质数会怎样？**

取 `PULSES = 601`（质数）。按 4.2.2 的退化结论，整除条件是「`PULSES` 是 7 的倍数」，而 601 不是 7 的倍数（\(601 / 7 \approx 85.86\)）。

\[ \frac{601 \times 512}{224} = \frac{307712}{224} \approx 1373.71 \]

C++ 整数除法会**截断**成 1373。于是 \(224 \times 1373 = 307552\)，而图像实际有 307712 个像素——多出来的 \(307712 - 307552 = 160\) 个像素永远不会被任何内核处理，最终图像缺失 160 个像素、几何错位。

**需要观察的现象（待本地验证）**：若真在 `common.h` 里把 `PULSES` 改成 601 并跑 `aiesim`，仿真大概率仍能「跑完」（因为整除失败不会触发编译错误），但输出的 `output_img.csv` 会出现局部缺失/错位。这种「能跑但结果错」的故障是整除约束最坑的地方。

#### 4.2.5 小练习与答案

**练习 1**：保持 `AIE_SWITCHES=7`、`RC_SAMPLES=512` 不变，下列哪个 `PULSES` 值合法：595、600、603？

**答案**：只需判断是否为 7 的倍数。\(595 = 5 \times 7^2\)，是 7 的倍数，合法；\(600 / 7 \approx 85.7\)，不合法；\(603 = 7 \times 86 + 1\)，不合法。所以只有 595 合法（前提是 GOTCHA 数据集至少有 595 个脉冲可用）。

**练习 2**：如果把 `AIE_SWITCHES` 从 7 改成 8，整除约束对 `PULSES` 的新要求是什么？

**答案**：\(N_{\text{kern}} = 8 \times 32 = 256 = 2^{8}\)。而 `RC_SAMPLES = 512 = 2^{9}` 已经能提供全部所需的因子 2，所以对 `PULSES` **没有任何奇因子约束**——任何正整数 `PULSES` 都满足整除。这正说明「约束依赖 `AIE_SWITCHES` 的具体值」，换配置要重新算。

---

### 4.3 雷达物理常数与距离分辨率 RANGE_RES

#### 4.3.1 概念说明

除了「规模宏」，`common.h` 还定义了一组雷达物理常数。它们和算法本身有关，而和「分成几个内核」无关。理解这组常数，关键是搞懂**距离分辨率 `RANGE_RES`** 是怎么一步步推出来的。

直觉是这样的：SAR 发射一段带宽有限的信号，回波经过距离压缩（Range Compression，也就是 phdata 里存的复数）后，不同的「距离」会落到不同的样本槽（sample bin）里。**一个样本槽对应多远的物理距离**，就是距离分辨率。它取决于信号的带宽（频率步进 `RANGE_FREQ_STEP`）和样本数 `RC_SAMPLES`。

此外，几个标记为「Used in AIE code only」的常数（`INV_TWO_PI`、`MIN_FREQ`、`INV_RANGE_RES`）是 AIE 内核做相位校正时用的，Host/PL 不直接用，但定义在公共头里方便 AIE 包含。

#### 4.3.2 核心流程

物理常数的推导链条如下：

1. 光速 \(C = 299792458\ \text{m/s}\)。
2. 雷达最低频率 `MIN_FREQ` ≈ 9.288 GHz（X 波段）——信号扫频的起点。
3. 每个样本对应的频率步进 `RANGE_FREQ_STEP` = 1,471,301.6 Hz。
4. 信号的「合成带宽」对应一个最大不模糊距离范围 `RANGE_WIDTH`：

\[ \text{RANGE\_WIDTH} = \frac{C}{2 \times \text{RANGE\_FREQ\_STEP}} \]

   除以 2 是因为雷达波是**双程**（发出去再反射回来）的。

5. 这个总范围被 `RC_SAMPLES` 个样本等分，每份就是距离分辨率 `RANGE_RES`：

\[ \text{RANGE\_RES} = \frac{\text{RANGE\_WIDTH}}{\text{RC\_SAMPLES}} \]

6. `INV_RANGE_RES = 1/RANGE_RES` 用于把「物理距离差」换算成「样本索引增量」。
7. `HALF_RANGE_SAMPLES = RC_SAMPLES/2` 是因为场景中心对齐到样本数组正中间，需要半段偏移。

默认配置下：\(\text{RANGE\_WIDTH} = \frac{299792458}{2 \times 1471301.6} \approx 101.88\ \text{m}\)，\(\text{RANGE\_RES} = \frac{101.88}{512} \approx 0.199\ \text{m}\)，即**每个距离样本约 20 厘米**。

#### 4.3.3 源码精读

[design/common.h:48-L50](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/common.h#L48-L50) 定义圆周率相关常数，其中 `INV_TWO_PI` 注释明确写了「Used in AIE code only」：

```cpp
static constexpr float PI = 3.1415926535898;
static constexpr float TWO_PI = 6.2831853071796;
static constexpr float INV_TWO_PI = 0.1591549430919; // Used in AIE code only
```

[design/common.h:53-L59](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/common.h#L53-L59) 定义雷达参数与距离分辨率的完整推导链：

```cpp
static constexpr float C = 299792458.0;
static constexpr float MIN_FREQ = 9288080400.0;      // Used in AIE code only
static constexpr float RANGE_FREQ_STEP = 1471301.6;
static constexpr float RANGE_WIDTH = C/(2.0*RANGE_FREQ_STEP);
static constexpr float RANGE_RES = RANGE_WIDTH/RC_SAMPLES;
static constexpr float INV_RANGE_RES = 1.0/RANGE_RES; // Used in AIE code only
static constexpr int HALF_RANGE_SAMPLES = RC_SAMPLES/2;
```

注意 `RANGE_WIDTH` 和 `RANGE_RES` 都是 `constexpr` 表达式，由前面的量**编译期算出**，而不是手写一个魔法数字——这样改 `RC_SAMPLES` 或 `RANGE_FREQ_STEP` 时，分辨率会自动重算。

`MIN_FREQ` 在 AIE 内核里用于构造相位校正系数（这一细节留到 u5-l4 详讲，这里只点出它的去向）：

[design/aie/backprojection.cc:90](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L90) —— `float ph_corr_coef = (4*PI*MIN_FREQ)/C;`。

`INV_RANGE_RES` 和 `INV_TWO_PI` 分别把「距离差→索引」「相位角→折叠周期」做换算：

[design/aie/backprojection.cc:139](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L139) —— `auto px_idx_acc = aie::mul(INV_RANGE_RES, differ_range_vec);`（用 `INV_RANGE_RES` 把差分距离换算成 RC 缓冲索引）。

#### 4.3.4 代码实践

**实践目标**：用计算器（或 Python）复算 `RANGE_WIDTH` 与 `RANGE_RES`，验证「每个样本约 20 cm」的直觉。

**操作步骤**：

1. 用 `C=299792458`、`RANGE_FREQ_STEP=1471301.6` 算 `RANGE_WIDTH = C/(2*RANGE_FREQ_STEP)`。
2. 再除以 `RC_SAMPLES=512` 得到 `RANGE_RES`。
3. 把 `RC_SAMPLES` 换成 256，重算 `RANGE_RES`，观察变化。

**预期结果**：

- `RANGE_WIDTH ≈ 101.88 m`
- `RC_SAMPLES=512` 时 `RANGE_RES ≈ 0.199 m`
- `RC_SAMPLES=256` 时 `RANGE_RES ≈ 0.398 m`（样本数减半，分辨率变差一倍）

**需要观察的现象**：`RANGE_WIDTH` 与 `RC_SAMPLES` 无关（它只由带宽决定），但 `RANGE_RES` 与 `RC_SAMPLES` 成反比——样本越多，每个样本对应的物理距离越短（分辨率越高）。这也解释了为什么 u1-l2 要为不同 `RC_SAMPLES` 各存一份 phdata：样本数变了，距离量化粒度就变了。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `RANGE_WIDTH` 的分母里有 `2.0` 而 `MIN_FREQ` 相位系数 `ph_corr_coef = (4*PI*MIN_FREQ)/C` 的分母 `C` 前面没有 2？

**答案**：`RANGE_WIDTH` 算的是**距离**，雷达波双程传播，所以要把「时延↔距离」除以 2；而 `ph_corr_coef` 算的是**相位角**（\(\omega t\)），是单程频率 × 双程时延再 × \(2\pi\)，因子 2 已经被吸收进系数里的 `4`（即 \(2 \times 2\pi\)）中。两者处理的物理量不同，不能简单类比。

**练习 2**：`INV_RANGE_RES` 为什么不直接在 AIE 代码里写 `1.0/RANGE_RES`，而要预先算好放进 `common.h`？

**答案**：因为 AIE 是面向向量/流水的处理器，乘法远快于除法。把倒数预先算成 `constexpr`，内核里用 `aie::mul(INV_RANGE_RES, ...)` 做乘法来代替除法，能省下宝贵的时钟周期。这是高性能信号处理代码的常见套路。

---

### 4.4 三域同步：common.h 作为唯一真相源

#### 4.4.1 概念说明

最后一个最小模块回答一个「为什么」：为什么非要把这些宏集中放进一个公共头，让三个域都来 `#include`？

答案是：**Host、AIE、PL 是三套独立的编译产物**（分别是 aarch64 主机 elf、AIE 库 `libadf.a`、PL 内核 `dma_pkt_router.xo`），它们之间没有 C++ 链接期的共享变量。运行时它们能协同，全靠「编译期各自读到同一个 `PULSES=602`」。如果 Host 用 602、AIE 用 600、PL 用 602，三者各自都能单独编译通过，但运行起来 buffer 大小对不上、循环次数对不上、DDR 偏移对不上，图像必然错乱——而且没有任何编译器警告。

所以 `common.h` 是项目里少数几个「牵一发而动全身」的文件。改它，等于同时改了三域的契约。

#### 4.4.2 核心流程

三域「消费」`common.h` 的方式略有不同：

| 域 | 文件 | 如何 include | 用到了哪些宏 |
|----|------|-------------|-------------|
| AIE | `design/aie/backprojection.cc`、`custom_kernels.h` | `#include "../common.h"` | `IMG_SOLVERS`、`RC_SAMPLES`、`PULSES`、`BC_ELEMENTS`、全部物理常数 |
| Host | `design/host/sar_backproject.cpp/.h` | `#include "../common.h"` | `PULSES`、`RC_SAMPLES`、`BC_ELEMENTS`、`AIE_SWITCHES`、`IMG_SOLVERS`（分配 buffer、读 CSV、编排循环） |
| PL | `design/pl/dma_pkt_router.cpp/.h` | `#include "../common.h"` | `PULSES`、`RC_SAMPLES`、`IMG_SOLVERS`（DDR 偏移、`depth` pragma） |
| 构建 | `Makefile` | **不用 include，用 `grep`** | `AIE_SWITCHES`、`RC_SAMPLES`（生成 `system.cfg`、选 CSV） |

注意最后一行：Makefile 不是 C++ 文件，不能 `#include`，于是它用 shell 的 `grep` 直接从 `common.h` 里把宏的数值抠出来用。这是「单一真相源」在构建脚本侧的延伸。

#### 4.4.3 源码精读

三域都用相对路径 `../common.h` 引入同一个文件：

[design/aie/custom_kernels.h:9](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/custom_kernels.h#L9)、[design/host/sar_backproject.h:13](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.h#L13)、[design/pl/dma_pkt_router.h:10](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/pl/dma_pkt_router.h#L10) 全都是 `#include "../common.h"`。

Host 端用宏来分配 buffer，大小公式一目了然：

[design/host/sar_backproject.cpp:34-L38](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/host/sar_backproject.cpp#L34-L38) —— 用 `PULSES*BC_ELEMENTS`、`PULSES*RC_SAMPLES*...` 算出三个 buffer 的字节数。

AIE 端用宏决定循环次数：

[design/aie/backprojection.cc:53](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L53) —— `for(unsigned i=0; i < RC_SAMPLES/16; i++)`（每 16 个 cfloat 一组搬运）。

PL 端用宏标注 AXI 接口深度：

[design/pl/dma_pkt_router.cpp:14](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/pl/dma_pkt_router.cpp#L14) —— `#pragma HLS INTERFACE m_axi port=ddr_mem offset=slave bundle=gmem depth=PULSES*RC_SAMPLES`。

Makefile 侧「grep 抠宏」的两个关键点：

[Makefile:92](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L92) —— 在 `package` 目标里 `grep '^#define RC_SAMPLES' common.h`，挑出对应文件名的 phdata CSV（u1-l2 讲过这套数据命名）。

[Makefile:201](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L201) —— 在 PL 编译规则里 `grep '^#define AIE_SWITCHES' common.h`，据此自动生成 `system.cfg` 里的 `nk`/`stream_connect`/`sp` 行（这部分细节留到 u7-l1）。

#### 4.4.4 代码实践

**实践目标**：用搜索工具确认「三域 + Makefile 确实在消费同一批宏」，建立对「单一真相源」的直观印象。

**操作步骤**：

1. 在仓库根目录用 `grep -rn "common.h" design/ Makefile`，统计有多少个文件 include 或 grep 了它。
2. 再用 `grep -rn "IMG_SOLVERS\b" design/`，看哪些域用了这个派生宏。
3. 在 `design/pl/dma_pkt_router.cpp` 里找到 `depth=PULSES*RC_SAMPLES`，思考：如果 Host 端按 `PULSES=602` 分配 buffer、而 PL 这里读到的 `PULSES` 是别的值，会发生什么。

**需要观察的现象**：`common.h` 被 AIE、Host、PL 三个目录下的文件 include，同时被 Makefile grep——四处引用，一份定义。

**预期结果**：你会看到「同一个宏名出现在至少 4 个不同位置」，这正是「单一真相源」的代价与价值：改一处，四处生效；但若有人偷偷在某处 hardcode 了一个数而不走 `common.h`，三域就会失配。

#### 4.4.5 小练习与答案

**练习 1**：为什么 Host、AIE、PL 不通过一个运行时配置文件（比如 JSON）来传这些参数，而要用编译期宏？

**答案**：因为这些值决定了 **buffer 大小、AIE 图的内核实例数、PL 接口位宽/深度**等「硬件结构」。AIE 图的拓扑（几个内核、怎么连）和 PL 的 AXI 接口宽度在编译/链接时就固化进 bitstream 了，运行时无法改。所以必须编译期常量。运行时参数（RTP）只用于那些**能在不改变硬件结构的前提下调整**的量（比如「这一脉冲要不要 dump 图像」），详见 u2-l2。

**练习 2**：Makefile 用 `grep '^#define AIE_SWITCHES'` 抠宏值，而不是 `#include`。这种做法有个潜在风险是什么？

**答案**：`grep` 靠文本匹配，如果有人把 `AIE_SWITCHES` 的定义写成 `#define  AIE_SWITCHES  7`（多空格）或放在条件编译 `#ifdef` 里，正则 `^#define AIE_SWITCHES` 仍可能匹配失败或匹配错位置，导致 Makefile 拿到空值或错值，进而生成错误的 `system.cfg`。相比之下，C++ 的 `#include` 由预处理器保证语义正确。这是「让构建脚本读 C 头文件」这种做法的固有脆弱点。

---

## 5. 综合实践

把本讲的知识串起来，做一次「假如我把 `PULSES` 改坏」的破坏性推演（**只在脑子里/纸上做，不要真的改源码**）：

**任务**：假设你要把 `PULSES` 从 602 改成 599（质数）。请分别预测以下四个环节会发生什么，并指出哪个环节会最先暴露问题：

1. **Host 端 buffer 分配**（`sar_backproject.cpp:34-L38`）：`PULSES*RC_SAMPLES` 等大小会怎么变？大小本身还算得出来吗？
2. **CSV 读取**（`sar_backproject.cpp:164`、`191`）：`while (... && pulse_idx < PULSES)` 循环会读几行？GOTCHA slowtime CSV 有没有这么多行？
3. **AIE 整除约束**（`backprojection.cc:68`）：`(599*512)/224` 等于多少？会有像素被丢弃吗？丢弃多少？
4. **Makefile 选 CSV**（`Makefile:92`）：`PULSES` 改动会影响 Makefile 选哪个 phdata 文件吗？为什么？

**完成后**，回答：上面 4 个环节里，**哪个会在编译/构建阶段就报错，哪个会「静默跑错」**？这能说明「单一真相源」为什么既是保护、也是风险。

**参考思路**：

1. Host buffer 大小照常算出（编译期表达式），只是数值变小，不会报错。
2. slowtime CSV 有完整 360° 约 4.2 万行（u1-l2），599 行绰绰有余，读取正常。
3. \((599 \times 512)/224 = 306688/224 \approx 1369.14\)，截断成 1369；\(224 \times 1369 = 306656\)，丢弃 \(306688 - 306656 = 32\) 个像素。**静默错误**。
4. Makefile 只 grep `RC_SAMPLES`，不 grep `PULSES`，所以选哪个 phdata 文件不受影响。

结论：4 个环节**没有一个会在构建阶段报错**，但 AIE 那一步已经悄悄丢了 32 个像素。这就是为什么 `common.h:11-L16` 的注释要专门把整除方程写出来——它是唯一能提醒你「这个数不能乱改」的文档。

## 6. 本讲小结

- `design/common.h` 是整个项目的「单一真相源」，集中定义了规模宏与雷达物理常数。
- 规模宏里 `PULSES`、`RC_SAMPLES`、`AIE_SWITCHES`、`IMG_SOLVERS_PER_SWITCH` 是手填的；`IMG_SOLVERS` 是派生量（\(= \text{AIE\_SWITCHES} \times \text{IMG\_SOLVERS\_PER\_SWITCH}\)）。
- 核心约束是 \((\text{RC\_SAMPLES} \times \text{PULSES}) / \text{IMG\_SOLVERS}\) 必须为整数，否则整除截断会静默丢弃像素、损坏图像。默认配置下它退化为「`PULSES` 必须是 7 的倍数」。
- 距离分辨率 `RANGE_RES` 由带宽与样本数推出：\(\text{RANGE\_WIDTH} = C/(2\cdot\text{RANGE\_FREQ\_STEP})\)，\(\text{RANGE\_RES} = \text{RANGE\_WIDTH}/\text{RC\_SAMPLES}\)，默认约 20 cm。
- Host、AIE、PL 三域用 `#include "../common.h"` 共享同一份定义，Makefile 用 `grep` 读取宏值生成 `system.cfg` 与选 CSV——改一个宏，三域 + 构建脚本同时受影响。
- 「能编译过」不等于「配置正确」：整除约束失败是典型的静默错误，改 `common.h` 时务必核对方程。

## 7. 下一步学习建议

本讲把「配置宏」讲透了，接下来可以沿着两个方向走：

- **往「主机应用」走**：读 [u3-l1 主机应用流程](u3-l1-host-application-flow.md) 系列，看 Host 端具体怎么用 `PULSES`/`RC_SAMPLES`/`BC_ELEMENTS` 来分配 `xrt::bo`、读 CSV、编排双层循环。这会让本讲 4.4 里那些行号真正「动」起来。
- **往「AIE 图拓扑」走**：读 [u4-l1 图与子图架构](u4-l1-graph-subgraph-architecture.md)，看 `AIE_SWITCHES` 和 `IMG_SOLVERS_PER_SWITCH` 是如何变成 `bpCluster[]` 数组与 `img_rec_km[]` 内核数组的，从而理解整除约束背后的物理结构。

如果你暂时只想巩固本讲，建议先做第 5 节的综合实践，再回头读一遍 `common.h` 全文（只有 61 行），确认每个宏和常数你都能解释。
