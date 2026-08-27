# DynParallel：动态并行——让 GPU 自己生成工作（Mandelbrot 实例）

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清「不规则负载（irregular workload）」为什么会让固定网格并行吃亏，以及 Mandelbrot 集绘制为什么是它的教科书案例。
2. 掌握 CUDA 动态并行（Dynamic Parallelism）的写法：设备端代码用与主机端完全相同的三尖括号语法 `<<<grid, block>>>` 启动子 kernel，并理解嵌套 grid 的语义（父 grid 要等所有子孙 grid 全部完成才算结束）。
3. 精读本目录的四个关键 kernel：基线 `compute`、递归调度器 `mandelbrot_block_k`、以及两种叶子工作 `dwell_fill_k` 与 `pixel_calc`。
4. 知道 `-rdc=true`（可重定位设备代码）是动态并行的编译前提，并能解释 Makefile 里 `--cudart=shared`、`-arch=sm_86`、`-g -G`、`-lpng` 各自的作用与代价。
5. 亲手完成「固定网格 vs 自适应细分」的对比实验，并通过调整初始分块粒度观察动态并行收益的变化。

本讲承接 u2-l3（显存管理与同步）与 u1-l2（Makefile 体系），并把 u3-l1 中「warp 是调度单位」的视角提升到「grid/block 是工作分配单位」的视角。

## 2. 前置知识

### 2.1 Mandelbrot 集与 dwell

Mandelbrot 集是复平面上的一个点集。对复平面上一点 \( c \)，从 \( z_0 = c \) 开始反复迭代

\[
z_{n+1} = z_n^2 + c
\]

如果 \( |z_n|^2 \) 始终不超过 4，则 \( c \) 属于集合内部；否则序列会逃逸。我们把「逃逸前迭代的次数」叫作该点的 **dwell**（驻留次数）。绘制图像时，每个像素对应复平面上的一个 \( c \)，dwell 决定像素颜色。

关键在于：**不同像素的 dwell 差异极大**。集合深处的点要迭代满上限（本项目里 `MAX_DWELL` 为 2048 次），集合远处的点一两次就逃逸。同一个 16000×16000 的画面里，单像素代价相差可达三个数量级——这正是 GPU 编程里典型的**不规则负载**。

### 2.2 固定网格的反模式与自适应网格

在 u2-l1 我们学过：host 用 `<<<grid, block>>>` 把一个静态写好的线程组织发射到 GPU。问题是 host 在发射时**不知道每个像素要算多久**，只能机械地「每线程一个像素」。于是：

- 碰巧分到边界像素的线程算很久，分到外围像素的线程瞬间结束；
- 整个 kernel 的耗时被最慢的线程拖住，大量 SM 在等少数线程。

README 里对 DynParallel 一行的表述正是这个对照：

| 基准 | 性能挑战（反模式） | 优化技术 |
|---|---|---|
| DynParallel | 需要嵌套并行的工作负载，例如使用自适应网格（adaptive grids）的场景 | 用动态并行让 GPU 自己生成工作 |

解决思路来自经典的 **Mariani–Silver 算法**：如果一个矩形区域的四条边界 dwell 全部相同，那么内部大概率也是这个值，直接整块填充；否则把矩形切小递归处理，切到足够小就逐像素算。「用不用切、切几块」这个决策只有在**运行中**才能做出——这就是「自适应」三个字的含义，也是它必须由 GPU 自己来完成的原因。

### 2.3 动态并行是什么

CUDA 动态并行（计算能力 3.5 起支持，README 也提醒「dynamic parallelism and memcpy_async require the GPU to support CUDA11」级别的较新环境）允许**设备端代码启动 kernel**：

- 语法与主机端完全一样：`kernel<<<grid, block>>>(args)`，只是这次这行代码写在了一个 `__global__` 函数体内；
- 被启动的叫**子 grid（child grid）**，启动者叫**父 grid（parent grid）**；
- 语义保证：**父 grid 只有在它启动的所有子孙 grid 都完成后才算完成**。所以主机在根 kernel 之后做一次 `cudaDeviceSynchronize()`，等到的就是整棵递归树；
- 代价：每次设备端启动有额外开销，且子 grid 的调度要经过「设备运行时（device runtime）」，因此它不是免费午餐——本讲的实验正是要量化这笔账。

### 2.4 阅读本讲需要的其他储备

- `cudaMalloc / cudaMemcpy / cudaFree` 生命周期（u2-l3）；
- `divup(x, y)` 向上取整技巧（u2-l1 讲过 `(n+255)/256`，这里是它的函数化写法）；
- 块内共享内存归约（u4-l5 BankRedux 的主题，本讲的 `border_dwell` 会用到同款写法，看不懂可以先跳过 4.4 节的归约部分）。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [DynParallel/Non_Dynamic_Parallelism.cu](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/DynParallel/Non_Dynamic_Parallelism.cu) | 反模式基线：固定网格 + 均匀划分 + 边界检查，每线程算一个像素（`compute` kernel） |
| [DynParallel/Dynamic_Parallelism.cu](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/DynParallel/Dynamic_Parallelism.cu) | 优化版：Mariani–Silver 递归细分，`mandelbrot_block_k` 在设备端反复启动子 kernel |
| [DynParallel/Makefile](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/DynParallel/Makefile) | 变量式 Makefile，含 `-rdc=true`、`--cudart=shared` 等动态并行必需/相关选项 |
| `DynParallel/include/`、`DynParallel/lib/` | 自带的 libpng / zlib 头文件和 Windows 静态库（`.lib`），说明作者也在 MSVC 环境编译过；Linux 下通常直接用系统 libpng |
| [README.md:L28-L30](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/README.md#L28-L30) | 项目总表中 DynParallel 的「挑战 → 技术」两行 |
| [README.md:L107-L108](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/README.md#L107-L108) | 前提条件：动态并行需要支持 CUDA 11 的 GPU |

两点提醒（承接 u1-l1 的实地核验习惯）：

- **本目录没有 `test.sh`**，实验命令要手工敲（u1-l1 提过 6 个目录缺 test.sh，DynParallel 是其中之一）；
- 两个程序会把结果写成 PNG（`./mandelbrot.png` 与 `./mandelbrot_dp.png`），目录下的 `.gitignore` 写了 `*.png`，所以仓库里看不到成图。

## 4. 核心概念与源码讲解

### 4.1 基线 kernel：`compute`——固定网格、均匀划分、边界检查

#### 4.1.1 概念说明

`Non_Dynamic_Parallelism.cu` 是本讲的「反模式」样本。它把 16000×16000 的画面均匀切成静态网格，每线程老老实实算一个像素的 dwell，完全无视各像素代价的天壤之别。它简单、正确、对任何 GPU 都友好——但在负载极不均匀的画面上，快线程陪慢线程一起等。

#### 4.1.2 核心流程

```text
main:
  cudaMalloc(dwellsD, w*h*4)              # 约 1.0×10⁹ 字节 ≈ 0.95 GiB
  blocks = (BSX=64, BSY=16)               # 每块 1024 线程
  grid   = (divup(w,64), divup(h,16))     # = (250, 1000) → 250,000 块
  计时开始 omp_get_wtime()
  compute<<<grid, blocks>>>(dwellsD, w, h, cmin, cmax)
  cudaThreadSynchronize()                 # 等整张图画完
  计时结束，打印 "Work took ... seconds"
  cudaMemcpy 回主机 → save_image 存 PNG
```

kernel 内部每个线程：

```text
x = threadIdx.x + blockDim.x * blockIdx.x     # 二维行主序线程编号
y = threadIdx.y + blockDim.y * blockIdx.y
if (x < w && y < h):
    dwells[y*w + x] = render(x, y, ...)       # 逐像素迭代求 dwell
```

单线程代价就是 `render` 的 while 循环次数，上限 `MAX_DWELL = 2048`。

#### 4.1.3 源码精读

先看单像素代价的来源，[DynParallel/Non_Dynamic_Parallelism.cu:L112-L124](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/DynParallel/Non_Dynamic_Parallelism.cu#L112-L124)：这段 `render` 把像素坐标归一化到复平面区间 \([c_{min}, c_{max}]\)，再反复执行 \( z \leftarrow z^2 + c \) 直到逃逸或触及 `MAX_DWELL`。注意循环次数完全由该点在复平面上的位置决定——这就是负载不均的源头。

被测 kernel 本体在 [DynParallel/Non_Dynamic_Parallelism.cu:L125-L132](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/DynParallel/Non_Dynamic_Parallelism.cu#L125-L132)：`compute` 是一个标准的二维 kernel，用 `if (x < w && y < h)` 做边界检查。由于 \( w = 16000 \) 恰好能被 64 和 16 整除，`divup` 向上取整在这里并不产生多余线程，边界检查在此配置下形同虚设（但换别的图像尺寸就必须有，见 4.1.5 练习 2）。

网格计算用的 `divup` 在 [DynParallel/Non_Dynamic_Parallelism.cu:L85-L87](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/DynParallel/Non_Dynamic_Parallelism.cu#L85-L87)，就是 u2-l1 见过的整数向上取整技巧的函数形式：\( \lceil x/y \rceil = x/y + (x \bmod y \ne 0) \)。

main 函数的启动与计时在 [DynParallel/Non_Dynamic_Parallelism.cu:L199-L228](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/DynParallel/Non_Dynamic_Parallelism.cu#L199-L228)。注意三点：

1. 图像尺寸硬编码为 `H = W = 8 * 2000 = 16000`，`dwells_size = 16000 × 16000 × 4 B ≈ 0.95 GiB`；
2. 计时只包住「launch + 同步」这一段（L216–L220），**不含** `cudaMalloc`（在计时前）、也不含 D2H 拷贝和 PNG 保存（在计时后）——这与 CoMem_AXPY 那种「整个包装函数十轮平均」的口径完全不同，对比两版时反而更公平；
3. `cudaThreadSynchronize()`（L218）是 `cudaDeviceSynchronize()` 的已废弃旧名，功能等价：阻塞到设备上所有工作（此处就是这一个 kernel）完成。

最后是一个很有意思的考古细节：[DynParallel/Non_Dynamic_Parallelism.cu:L68-L78](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/DynParallel/Non_Dynamic_Parallelism.cu#L68-L78) 的注释里已经完整写出了 Mariani–Silver 算法的伪代码（「边界同 dwell 则整块填充，否则细分递归」），而 [DynParallel/Non_Dynamic_Parallelism.cu:L90-L101](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/DynParallel/Non_Dynamic_Parallelism.cu#L90-L101) 的 `border_check` 只有注释骨架、函数体是空的，且全文件没有任何地方调用它。也就是说：**基线文件故意把「自适应」这一半留白了，等动态并行版来实现**。这是阅读对照式微基准时一个常见套路——反模式文件里往往藏着优化版的草稿。

#### 4.1.4 代码实践（源码阅读型，无需 GPU）

1. **实践目标**：亲手算出基线 kernel 的并行规模，建立「256M 个线程」的量级直觉。
2. **操作步骤**：
   - 打开 [DynParallel/Non_Dynamic_Parallelism.cu:L27-L28](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/DynParallel/Non_Dynamic_Parallelism.cu#L27-L28) 确认 `BSX=64, BSY=16`；
   - 用 L213 的 `divup(w, blocks.x), divup(h, blocks.y)` 计算 grid 维度；
   - 用 L205 的 `w * h * sizeof(int)` 计算显存需求。
3. **需要观察的现象**：纸上得到三行数字：每块线程数、grid 块数、总线程数、显存字节数。
4. **预期结果**：1024 线程/块；grid = (250, 1000) = 250,000 块；总线程 256,000,000 = 像素数（一一对应）；显存 1,024,000,000 字节（约 0.95 GiB）。若你的 GPU 显存不足 1 GiB 空闲，运行会失败——这正是后面综合实践建议先缩小图像的原因。
5. 运行部分「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么说 `compute` 的负载不均会降低硬件利用率？（用 SM 与线程束的语言回答）

答案：GPU 以 warp（32 线程）为调度单位、以 SM 为执行单位。同一画面里，边界区域的一个 warp 可能每个线程都要迭代近 2048 次，而外围区域一个 warp 一两轮就结束。SM 上先做完的 warp/block 释放后，若没有足够多新的可调度块补位（或剩余工作集中在少数块），SM 就会空闲等待，整机的有效利用率取决于最慢的那批工作。固定网格无法在运行时把空闲算力调去帮慢区域。

**练习 2**：把图像尺寸改成 `W = H = 7999`（不改动其他代码），`compute` 还能算对吗？为什么？

答案：能算对，但效率下降。`divup(7999,64) = 125`、`divup(7999,16) = 500`，grid 共 62,500 块、64,000,000 线程，多于 7999×7999 ≈ 63.98M 个像素。多出来的线程靠 `if (x < w && y < h)` 立即退出，正确性由边界检查保证；代价是这些空线程白白占了启动与调度资源。这也解释了为什么 u2-l1 强调「向上取整 + 边界检查」是成对出现的。

**练习 3**：基线程序的计时包含 `cudaMalloc` 吗？包含 PNG 保存吗？

答案：都不包含。`cudaMalloc` 在 L208（计时开始 L216 之前），PNG 保存 `save_image` 在 L223（计时结束 L219 之后）。计时区间内只有 kernel 启动、执行与同步。因此这个数字最接近「纯 kernel 时间 + 一次启动开销」，与 AXPY 系基准那种含分配与拷贝的十轮平均墙钟不同（对照 u1-l4 的口径讨论）。

### 4.2 动态并行调度核心：`mandelbrot_block_k`——在设备上递归细分

#### 4.2.1 概念说明

`Dynamic_Parallelism.cu` 实现了基线文件注释里那套 Mariani–Silver 算法。主角 `mandelbrot_block_k` 是一个**既做计算决策、又当调度器**的 kernel：每个 block 负责画面上一个 \( d \times d \) 的方块，先花小代价探测四条边界的 dwell；

- 边界 dwell 全部相同 → 大概率整块同色，发射 `dwell_fill_k` 一次填满，代价从 \( d^2 \) 次迭代降到 \( 4d \) 次；
- 边界不同且方块还够大 → **发射一个新的 `mandelbrot_block_k`**，把自己切成 16×16 个小方块 recursively 处理；
- 边界不同且已经很小 → 发射 `pixel_calc` 退化为逐像素计算（即基线做法）。

「发射子 kernel」这个动作发生在 GPU 上运行的代码里——这就是动态并行：「让 GPU 自己生成工作」（README 原话 *allow the GPU to generate its own work*）。

#### 4.2.2 核心流程

```text
main（host，只发射一次根 kernel）:
  grid = (INIT_SUBDIV=64, INIT_SUBDIV=64)        # 4096 个根方块
  mandelbrot_block_k<<<grid, (64,16)>>>(dwells, w, h, cmin, cmax,
                                        x0=0, y0=0, d=W/64=250, depth=1)
  cudaThreadSynchronize()                        # 等整棵树

mandelbrot_block_k（device，每个 block 一个 d×d 方块）:
  本 block 的方块原点: x0 += d*blockIdx.x; y0 += d*blockIdx.y
  全块线程协作: comm_dwell = border_dwell(...)    # 探测四条边界
  只有线程 (0,0) 执行决策与发射:
    if comm_dwell != DIFF_DWELL:
        dwell_fill_k<<<(divup(d,64), divup(d,16)), (64,16)>>>(...)   # 整块填充
    else if depth+1 < MAX_DEPTH && d/SUBDIV > MIN_SIZE:
        mandelbrot_block_k<<<(16,16), (64,16)>>>(..., d/16, depth+1) # 递归细分
    else:
        pixel_calc<<<(divup(d,64), divup(d,16)), (64,16)>>>(...)     # 逐像素
```

对应的递归树：

```text
根 grid (64×64 = 4096 块，每块 250×250)
 ├── 均匀方块 ──► dwell_fill_k       （便宜：写常数）
 └── 非均匀方块 ─► 细分 mandelbrot_block_k (16×16 块)  或  pixel_calc（逐像素）
                      └── ……（理论上可继续往下长）
```

语义要点：父 grid 未完成前其子孙必须先完成，因此 main 里那一次 `cudaThreadSynchronize()` 等到的是**整棵递归树**的结果，无需逐层手工同步。

**一个必须亲自验证的重要发现**：把出厂常量代入递归条件。根方块 \( d_0 = W / 64 = 250 \)，递归分支要求 \( d/\text{SUBDIV} > \text{MIN_SIZE} \)，即 \( 250/16 = 15 > 64 \)，**为假**。所以：

- 出厂配置下，递归分支（L254–L258）**一次都不会执行**，非均匀方块直接落入 `pixel_calc`；
- 递归树实际深度只有 1（根发射叶子，叶子不再发射）；
- 此时动态版相对基线的收益来自「4096 个方块里凡是边界均匀的，用 4d 次边界迭代 + 一次填充代替 d² 次逐像素迭代」，而不是多层自适应细分；
- 想看到真正的多层递归，必须调小 `INIT_SUBDIV`（让根方块变大）或调小 `MIN_SIZE` / `SUBDIV`——这正是综合实践的核心实验。

#### 4.2.3 源码精读

调度器本体在 [DynParallel/Dynamic_Parallelism.cu:L244-L268](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/DynParallel/Dynamic_Parallelism.cu#L244-L268)。逐段看：

- L246：`x0 += d * blockIdx.x, y0 += d * blockIdx.y;` —— 本 block 负责的方块原点由块编号乘方块边长得到，与 u2-l1 的一维编号公式是同一思想的二维版；
- L247：`border_dwell(...)` —— **必须全 block 线程一起调用**，因为其内部有 `__syncthreads()`（见 4.4）；
- L248：`if (threadIdx.x == 0 && threadIdx.y == 0)` —— 决策与三个子发射都由块内第一个线程完成。设备端 kernel 启动允许出现在分支代码中，每个发射线程各发一次即可；这里若让 1024 个线程都执行发射，会发射 1024 份重复工作；
- L249–L253：均匀分支，发射 `dwell_fill_k`，把 `comm_dwell` 写满整个方块；
- L254–L259：细分分支，子 grid 固定为 `(SUBDIV, SUBDIV) = (16,16)`，子方块边长 `d / SUBDIV`，深度 `depth + 1`；终止条件由 `MAX_DEPTH = 16` 和 `MIN_SIZE = 64` 双重把关；
- L260–L266：叶子分支，发射 `pixel_calc`。L263 那行注释「maybe broke since not treating as kernel launch」是作者自己留下的疑虑标记——实际上把三尖括号当作 kernel 发射在这里是正确用法。

控制全部行为的常量集中在 [DynParallel/Dynamic_Parallelism.cu:L29-L43](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/DynParallel/Dynamic_Parallelism.cu#L29-L43)：

| 宏 | 值 | 含义 |
|---|---|---|
| `MAX_DWELL` | 2048 | 单像素迭代上限（负载差异的来源） |
| `BSX` / `BSY` | 64 / 16 | 每块 1024 线程，所有 kernel 共用 |
| `MAX_DEPTH` | 16 | 递归深度上限（算法层面的人工闸门） |
| `SUBDIV` | 16 | 每次细分子轴的份数（子 grid 为 16×16） |
| `MIN_SIZE` | 64 | 方块边长低于此值就不再细分，转逐像素 |
| `INIT_SUBDIV` | 64 | 根层划分数（根 grid 为 64×64，d₀ = W/64 = 250） |
| `DIFF_DWELL` | −1 | 边界 dwell 互不相同（「需要继续切」的信号） |
| `NEUT_DWELL` | 2049 | 归约单位元（见 4.4） |

根发射在 [DynParallel/Dynamic_Parallelism.cu:L275-L301](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/DynParallel/Dynamic_Parallelism.cu#L275-L301)：main 里唯一一次 host 端发射是 L290 的 `mandelbrot_block_k<<<grid, blocks>>>(dwellsD, w, h, cmin, cmax, 0, 0, W / INIT_SUBDIV, 1)`，注意初始 `depth` 传的是 1，初始 d = W/INIT_SUBDIV = 250。计时方式与基线完全对称（L289–L293 的 `omp_get_wtime` 包住 launch + `cudaThreadSynchronize`），这正是两版可以公平对比的原因。

#### 4.2.4 代码实践（源码阅读型，无需 GPU：递归决策表）

1. **实践目标**：在不运行代码的前提下，推导不同 `INIT_SUBDIV` 取值下递归树的实际形态，为综合实践建立预测。
2. **操作步骤**：对 `W = 16000`、`SUBDIV = 16`、`MIN_SIZE = 64`，按下表逐格计算「根方块边长 d₀」与「d₀/16 是否 > 64」，并判断递归是否发生、能到第几层。
3. **需要观察的现象**：完成表格。
4. **预期结果**：

| INIT_SUBDIV | d₀ = W/INIT_SUBDIV | d₀/16 | > 64 ? | 实际行为 |
|---|---|---|---|---|
| 64（出厂） | 250 | 15 | 否 | 树深 1，非均匀块直接 `pixel_calc` |
| 16 | 1000 | 62 | 否 | 树深 1 |
| 8 | 2000 | 125 | 是 | 树深 2：细分到 d=125 后停（125/16=7） |
| 4 | 4000 | 250 | 是 | 树深 2：细分到 d=250 后停 |
| 2 | 8000 | 500 | 是 | 树深 2：细分到 d=500 后停 |
| 1 | 16000 | 1000 | 是 | 树深 2：细分到 d=1000 后停（1000/16=62） |

   注意一个规律：`SUBDIV=16, MIN_SIZE=64` 时，递归要长到第 3 层需要根方块 \( d_0 > 64 \times 16 \times 16 = 16384 \)，而 \( W = 16000 \)，**无论 INIT_SUBDIV 取多小都到不了第 3 层**。想加深树，只能改 `SUBDIV`（如 4）或 `MIN_SIZE`。此表为纯推算，运行验证「待本地验证」。

5. 顺带核对：`SUBDIV` 个子方块要无缝铺满父方块，需要 \( d \) 能被 `SUBDIV` 整除。上表中会发生细发的各层 d（16000、8000、4000、2000）都能被 16 整除，所以出厂常量下不存在覆盖缝隙；但若你实验时选了不能整除的配置（例如设法让 d=1000 再细分：16×62 = 992 < 1000），就会出现 8 像素宽的漏算条带——这是动手改参数时要警惕的正确性坑。

#### 4.2.5 小练习与答案

**练习 1**：为什么子 kernel 的发射要放在 `if (threadIdx.x == 0 && threadIdx.y == 0)` 里，而 `border_dwell` 却要全块线程参与？

答案：`border_dwell` 内部把边界像素分散给块内所有线程计算，并用共享内存 + `__syncthreads()` 做块内归约——`__syncthreads()` 要求块内所有线程都到达，缺人就会死锁或结果错误，所以必须全员参与。而发射子 kernel 是「块级」只需一次的动作：如果 1024 个线程都执行发射语句，GPU 会把同一个子 grid 发射 1024 遍（每个发射线程一次），既是巨大浪费也是错误。所以决策阶段收敛到单线程。

**练习 2**：主机只调用了一次根 kernel，为什么一次 `cudaThreadSynchronize()` 就能拿到整幅图的结果？

答案：动态并行的语义保证父 grid 的生命周期覆盖其所有子孙 grid：父 kernel 返回前，它发射的子 grid 必须全部完成（发射本身虽是异步的，但父 grid 的完成条件包含全部后代完成）。因此根 kernel 结束时，整棵递归树都已落盘到 `dwells` 数组，主机再同步一次即可。

**练习 3**：假设某 250×250 方块的四条边界 dwell 完全相同（全部等于集合内部的最大值 2048），比较基线与动态版在这个方块上的迭代次数。

答案：基线 `compute` 需要全部 \( 250^2 = 62500 \) 个像素各迭代 2048 次，共约 \( 1.28 \times 10^8 \) 次迭代；动态版只需 \( 4d = 1000 \) 次边界像素的 dwell 计算（每个也是 2048 次迭代，共约 \( 2.05 \times 10^6 \) 次迭代），再由 `dwell_fill_k` 用约 62500 次「写常数」收尾。迭代量相差约 62 倍，这就是均匀区域上动态并行的收益来源；反过来，若方块边界不均匀，这 1000 次边界计算加上一次子发射就纯属额外开销。

### 4.3 两种叶子工作：`dwell_fill_k`（整块填充）与 `pixel_calc`（逐像素）

#### 4.3.1 概念说明

递归的尽头只有两种命运，对应两个极简的叶子 kernel：

- **`dwell_fill_k`**：区域已被判定均匀，把同一个 dwell 常数写满方块。每个线程只做一次访存写，几乎零计算——这是 Mariani–Silver 省下的一切红利兑现的地方；
- **`pixel_calc`**：区域无法判定均匀，退回基线做法，逐像素老老实实迭代。它等价于把基线 `compute` 的战场从整幅图缩小到一个方块。

两者都是再普通不过的 `__global__` 函数——动态并行并不改变 kernel 本身的写法，改变的是**谁、在什么时候、以多大的粒度发射它们**。

#### 4.3.2 核心流程

两个 kernel 的骨架完全同构：

```text
块内线程坐标 (x, y)，x,y ∈ [0, d)
若越界则退出           # 由父级 divup 网格产生的多余线程
坐标平移到全局画面: x += x0, y += y0
写 dwells[y*w + x]:
    pixel_calc  → 写 dwell_function(x, y) 的计算结果   # 贵：最多 2048 次迭代
    dwell_fill_k → 写传入的常数 dwell                   # 便宜：一次写
```

父级发射时的 grid 尺寸都取 `(divup(d, BSX), divup(d, BSY))`。以出厂的 d = 250 为例：`divup(250,64) = 4`、`divup(250,16) = 16`，即每个叶子 kernel 被发射为 4×16 = 64 个块、65,536 线程，覆盖 62,500 个像素（多余线程被边界检查挡掉）。

#### 4.3.3 源码精读

`pixel_calc` 在 [DynParallel/Dynamic_Parallelism.cu:L195-L202](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/DynParallel/Dynamic_Parallelism.cu#L195-L202)：注意参数里有 `w`、`h`（供 dwell 计算归一化坐标）和方块原点 `x0, y0` 与边长 `d`。L199 的 `x += x0, y += y0;` 是「先在方块局部坐标系判断边界、再平移到全局坐标系寻址」的两步写法，比先平移再判断更不容易越界。

`dwell_fill_k` 在 [DynParallel/Dynamic_Parallelism.cu:L204-L211](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/DynParallel/Dynamic_Parallelism.cu#L204-L211)：参数表里没有 `h`（填常数不需要知道图像高，写地址 `y * w + x` 只用到 `w`），`dwell` 就是从边界测试得来的公共值。

逐像素的代价函数 `dwell_function` 在 [DynParallel/Dynamic_Parallelism.cu:L181-L193](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/DynParallel/Dynamic_Parallelism.cu#L181-L193)，与基线文件的 `render`（4.1.3）逐行等价，只是换了个名字——两版程序计算的是同一个数学对象，这保证了对比实验的公平性。

#### 4.3.4 代码实践（源码阅读型，无需 GPU）

1. **实践目标**：手工追踪一次叶子 kernel 的坐标变换，确认你能准确预言它写哪个数组元素。
2. **操作步骤**：设 `w = h = 16000`，某方块原点 `(x0, y0) = (750, 1250)`，`d = 250`，子 kernel 的块大小 `(64, 16)`。取 `blockIdx = (2, 3)`、`threadIdx = (5, 7)`，计算该线程最终写入的 `dwells` 下标。
3. **需要观察的现象**：一组 `(局部 x, 局部 y) → (全局 x, 全局 y) → 线性下标` 的推演。
4. **预期结果**：局部 `x = 5 + 64×2 = 133`，`y = 7 + 16×3 = 55`（均 < 250，通过检查）；全局 `x = 750+133 = 883`，`y = 1250+55 = 1305`；线性下标 `y*w + x = 1305×16000 + 883 = 20,880,883`。若是 `dwell_fill_k`，则该元素被写为传入常数；若是 `pixel_calc`，则写 `dwell_function(883, 1305, ...)`。
5. 若想用运行验证，可在叶子 kernel 里临时加一行 `printf`（设备端 printf 可用但很慢，只适合调试）——「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`pixel_calc` 与基线的 `compute` 相比有哪些差异？哪些是本质的？

答案：差异有三点：(1) 作用域——`compute` 覆盖整幅图（原点固定为 0），`pixel_calc` 只覆盖一个 \( d \times d \) 方块（原点由参数传入）；(2) 边界检查对象——前者对 `w, h` 检查，后者对 `d` 检查；(3) dwell 计算函数名不同（`render` vs `dwell_function`，实现等价）。本质差异只有作用域：`pixel_calc` 就是「缩小版 compute」，这说明动态并行并未发明新的计算 kernel，只是用调度把同一个计算以更聪明的粒度铺开。

**练习 2**：`dwell_fill_k` 是访存受限还是计算受限？它的存在为什么能代表 Mariani–Silver 的全部收益？

答案：它每个线程只写一个 int、几乎不计算，是纯粹的访存/带宽型工作。因为判定「整块同色」后，省掉的是 \( (d^2 - 4d) \) 个像素的完整迭代（每个最多 2048 次乘加），这些省下的计算最终都以这一次廉价填充「兑现」。若没有填充 kernel，即使正确判定均匀也无处兑现收益。

**练习 3**：`dwell_fill_k` 的参数表没有 `h`，而 `pixel_calc` 同时有 `w` 和 `h`，为什么？

答案：写地址 `dwells[y*w + x]` 只需要行宽 `w`；而 `pixel_calc` 调用的 `dwell_function` 要把像素坐标归一化为复平面坐标 \( f_x = x/w, f_y = y/h \)，两个维度都用得到。参数表精确反映了每个 kernel 用到什么——阅读 CUDA 代码时，参数表本身就是一份「依赖说明书」。

### 4.4 决策依据：`border_dwell` 与 `get_dwell_eq` 的块内归约

#### 4.4.1 概念说明

`mandelbrot_block_k` 的三路决策全靠一个问题：「这个方块四条边界的 dwell 是否全部相等？」`border_dwell` 负责回答它。这本质上是一次**块内归约（block reduction）**：把 1024 个线程各自算出的局部结论，用折叠函数合并成一个块级结论。它与 u4-l5 BankRedux 的共享内存归约是同款技术，只是归约运算从「加法」换成了自定义的「是否一致」。

这里有一个精巧的设计：归约需要一个「不影响结果的单位元」。作者为此专门定义了 `NEUT_DWELL = MAX_DWELL + 1 = 2049`——一个合法 dwell（0..2048）永远取不到的值，作为每个线程的初始值，在折叠中不改变别人的答案。

#### 4.4.2 核心流程

```text
border_dwell(w, h, cmin, cmax, x0, y0, d):     # 由整块 1024 线程共同执行
  tid = threadIdx.y * blockDim.x + threadIdx.x  # 块内一维编号
  comm_dwell = NEUT_DWELL                       # 单位元初始化
  for r = tid; r < d; r += 1024:                # 跨步分发 d 个边界位置
      for b = 0..3:                             # 东南北西四条边
          (x, y) = 第 b 条边上第 r 个像素
          comm_dwell = get_dwell_eq(comm_dwell, dwell_function(x, y))
  # ---- 块内树形归约 ----
  ldwells[tid] = comm_dwell（tid < nt 的线程）
  for nt = min(d,1024); nt > 1; nt /= 2:
      ldwells[tid] = get_dwell_eq(ldwells[tid], ldwells[tid + nt/2])（tid < nt/2）
      __syncthreads()
  return ldwells[0]                             # 公共 dwell 或 DIFF_DWELL(-1)
```

折叠运算 `get_dwell_eq(d1, d2)` 的真值表：

| d1 | d2 | 结果 | 含义 |
|---|---|---|---|
| = d2 | = d2 | d1 | 仍然一致 |
| NEUT | 任意 | min(d1,d2) = 另一方的值 | 单位元不参与 |
| ≠ d2（且都不是 NEUT） | | −1 (`DIFF_DWELL`) | 出现分歧，不可再统一 |

它满足交换律和（在「全部相等」语义下）结合律，且 `NEUT_DWELL` 是单位元——因此这是一个合法的归约算子，可以套用树形归约框架：

\[
\text{comm\_dwell} = \underset{i}{\bigoplus}\; \text{dwell}_i, \quad \text{其中 } a \oplus \text{NEUT} = a
\]

#### 4.4.3 源码精读

归约算子定义在 [DynParallel/Dynamic_Parallelism.cu:L171-L179](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/DynParallel/Dynamic_Parallelism.cu#L171-L179)。注意基线文件里有一个同名的 `get_dwell_eq`（Non_Dynamic_Parallelism.cu L103–L111，返回值写法略有出入）但从未被调用——又一处「基线只留半成品」的痕迹。

`border_dwell` 主体在 [DynParallel/Dynamic_Parallelism.cu:L213-L241](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/DynParallel/Dynamic_Parallelism.cu#L213-L241)：

- L215–L227：把 \( 4d \) 个边界像素（四条边各 d 个，角点会被重复计算一次，属可忽略的少量冗余）跨步分给块内线程，每个线程边算边用 `get_dwell_eq` 折叠；
- L222–L223：用条件表达式从「边的编号 b + 位置 r」推出该边界像素的坐标，四条边分别是 `x0+d-1`（东）、`y0+d-1`（北）、`x0`（西）、`y0`（南），建议对照阅读；
- [DynParallel/Dynamic_Parallelism.cu:L229-L238](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/DynParallel/Dynamic_Parallelism.cu#L229-L238)：经典的共享内存折半归约——`__shared__ int ldwells[BSX * BSY]`，每轮活跃线程数减半，每轮之间 `__syncthreads()`。它假设所有线程都到达 `__syncthreads()`，这正是 4.2 练习 1 中「`border_dwell` 必须全员参与」的原因。

#### 4.4.4 代码实践（源码阅读型，无需 GPU）

1. **实践目标**：算出边界探测的实际工作量，验证「4d 远小于 d²」这一收益前提。
2. **操作步骤**：对出厂配置 d = 250、块内 1024 线程：(a) 数一数总共要做多少次 `dwell_function` 调用；(b) 推出第一轮循环里哪些线程有活干；(c) 写出归约循环的 `nt` 序列。
3. **需要观察的现象**：三个数字/序列。
4. **预期结果**：(a) 每个线程对每个 r 执行 4 次调用，总调用约 \( 4d = 1000 \) 次；(b) 跨步循环 `for (r = tid; r < 250; r += 1024)` 中只有 `tid < 250` 的 250 个线程各干一轮，其余 774 个线程直接空手进入归约；(c) `nt = min(250, 1024) = 250`，序列为 250 → 125 → 62 → 31 → 15 → 7 → 3 → 1（每轮 `nt /= 2` 向下取整），共约 8 轮、每轮一次 `__syncthreads()`。对照整块逐像素的 62,500 次迭代，边界探测只占 \( 1000/62500 = 1.6\% \) 的规模——判定成本足够低，跳过才有净赚。
5. 归约在非二次幂 `nt` 下的正确性走查属于进阶话题，可标记「待确认」后结合 u4-l5 再回头看。

#### 4.4.5 小练习与答案

**练习 1**：为什么需要 `NEUT_DWELL` 这个特殊值？直接用 0 或 −1 初始化 `comm_dwell` 行不行？

答案：不行。折叠语义是「遇到两个不相等的合法 dwell 就判定分歧」。0 和 −1 里，0 是合法 dwell（像素一步就逃逸时结果就是 0），用它初始化会把「还没算任何像素」误判成「已经算到一个 dwell=0 的像素」；−1 恰是 `DIFF_DWELL` 的哨兵值，语义冲突更直接。`NEUT_DWELL = MAX_DWELL + 1 = 2049` 是合法值域 [0, 2048] 之外的第一个整数，天然空出来当单位元，且 `min` 让它折叠时永远让位给真实值。

**练习 2**：`border_dwell` 是 `__device__` 函数而不是 `__global__` 函数，它和 kernel 的区别是什么？

答案：`__device__` 函数只能在设备上被调用（这里是被 kernel 内的普通函数调用方式调用），不能被发射为 grid，也没有自己的 block/thread 组织——它运行在调用者的线程上下文里。`border_dwell` 之所以能做「块内」归约，是因为它被一个 block 的所有线程同时调用、内部用共享内存与 `__syncthreads()` 协作，而不是因为它自己是个 kernel。区分「函数在哪个空间执行」与「并行组织属于谁」是读 CUDA 代码的基本功。

**练习 3**：如果把 `BSX/BSY` 改成 (32, 8)（每块 256 线程），`border_dwell` 的边界分发循环会怎么变？

答案：块内线程数变为 256，跨步 `r += 256`。d = 250 时 tid < 250 的线程各干一轮（与之前相同的工作总量 1000 次调用），但每轮能并行的线程少了，且归约的共享数组变小为 `ldwells[256]`。工作总量不变、组织方式变化——这正是「计算量由算法决定、执行组织由启动配置决定」的一个直观例子。

### 4.5 编译与链接：`-rdc=true`、`--cudart=shared` 与其余标志

#### 4.5.1 概念说明

动态并行对编译流程有一个硬性要求。普通 CUDA 编译时，nvcc 把每个 kernel 编译成最终设备码，host 端的发射地址在链接期直接绑定。但当 kernel 里的发射语句写在**设备代码内部**时，被调 kernel 的设备码地址必须等到「设备链接（device linking）」阶段才能解析——这要求设备代码以**可重定位（relocatable）**形式生成，即 `-rdc=true`。不给这个选项，nvcc 会在 `mandelbrot_block_k` 内部那三处三尖括号处直接报错拒绝编译（错误信息大意是设备端调用 kernel 需要可重定位设备代码，具体措辞「待本地验证」，见下方实践）。

`--cudart=shared` 则指定以**动态链接**方式使用 CUDA 运行时（`libcudart.so`），而不是把运行时静态嵌进可执行文件——设备端启动要经由运行时的设备侧部分协调，作者选择共享链接方式构建本基准（官方文档明确列为前提的是 `-rdc=true`；`--cudart=shared` 是该 Makefile 采用的配套链接方式）。

Makefile 里还有几个必须看懂的标志：

| 标志 | 作用 | 提醒 |
|---|---|---|
| `-arch=sm_86` | 只为 Ampere GA10x（如 RTX 30 系列、A40/A6000）生成代码 | 换机器必须改；动态并行要求计算能力 ≥ 3.5 |
| `--cudart=shared` | 动态链接 CUDA 运行时 | 运行机器需有匹配的 `libcudart.so` |
| `-rdc=true` | 生成可重定位设备码 | **动态并行的编译前提** |
| `-Xcompiler -fopenmp` | 把 `-fopenmp` 传给主机编译器 | 因为代码用了 `omp_get_wtime()` 计时 |
| `-lpng` | 链接 libpng（保存 PNG 用） | Linux 需先装 `libpng-dev` |
| `-g -G`（OPT） | 主机+设备调试信息，`-G` **关闭设备优化** | 沿革自 WarpDivRedux 的「保真演示」风格；做性能结论前应去掉 `-G`（见 u1-l2、u3-l1） |

#### 4.5.2 核心流程

`make` 在这个 Makefile 里实际展开的命令序列（简化）：

```text
nvcc -g -G -arch=sm_86 --cudart=shared -rdc=true -Xcompiler -fopenmp -lpng -c Dynamic_Parallelism.cu
nvcc -g -G -arch=sm_86 --cudart=shared -rdc=true -Xcompiler -fopenmp -lpng -c Non_Dynamic_Parallelism.cu
nvcc -arch=sm_86 --cudart=shared -rdc=true -Xcompiler -fopenmp -lpng -o Dynamic_Parallelism Dynamic_Parallelism.o
nvcc -arch=sm_86 --cudart=shared -rdc=true -Xcompiler -fopenmp -lpng -o Non_Dynamic_Parallelism Non_Dynamic_Parallelism.o
```

注意两个工程细节：

1. **`.o` 是带 `-g -G` 编译的**：`OPT` 出现在 [DynParallel/Makefile:L18-L21](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/DynParallel/Makefile#L18-L21) 的编译规则里，所以链接产物天生带着「设备优化被关闭」的属性。直接拿这两个可执行文件比时间，量级可能明显偏慢——严谨的性能对比应当去掉 `-G` 重新编译（综合实践会做这一步）；
2. **Makefile 的规则组织比较随意**（u1-l2 说它是四种风格里的「变量式」）：`Dynamic_Parallelism.o` 这一条规则同时编译两个 `.cu`，`Dynamic_Parallelism` 这一条规则同时链接出两个可执行文件，还留了一个从未被 `all` 依赖的 `main:` 目标。读起来别扭，但 `make && make clean` 是能正常工作的。

#### 4.5.3 源码精读

[DynParallel/Makefile:L1-L9](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/DynParallel/Makefile#L1-L9) 定义了变量与默认目标 `all: Dynamic_Parallelism Non_Dynamic_Parallelism`；[DynParallel/Makefile:L18-L26](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/DynParallel/Makefile#L18-L26) 是编译与链接规则；[DynParallel/Makefile:L28-L31](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/DynParallel/Makefile#L28-L31) 是清理规则。

关于 PNG 依赖：两个源文件都 `#include <png.h>`（用于 `save_image`）。目录下自带的 `include/png.h` 等头文件与 `lib/*.lib` 是 **Windows/MSVC 风格的静态库**（`.lib` 是 MSVC 的库格式），说明作者在 Windows 上也构建过这份代码；在 Linux 上编译时，系统会找 `/usr/include/png.h`，因此需要先安装开发包（Debian/Ubuntu 为 `libpng-dev`）。这也是为什么 `CUDAFLAGS` 里有 `-lpng`。

#### 4.5.4 代码实践（编译实验，需要 CUDA 工具链但不需要 GPU）

1. **实践目标**：亲眼确认 `-rdc=true` 是动态并行的编译前提，并排清 PNG 依赖。
2. **操作步骤**：
   - `nvcc --version` 确认工具链；`sudo apt-get install libpng-dev`（或其他发行版等价命令）；
   - 进入 `DynParallel/`，先按原样 `make`，观察编译命令被打印出来的顺序与 4.5.2 的预测是否一致；
   - 然后**在 Makefile 外**手工编译一次去掉 `-rdc=true` 的版本（示例代码，请勿直接覆盖原 Makefile）：
     ```bash
     nvcc -arch=sm_86 --cudart=shared -Xcompiler -fopenmp -lpng \
          -c Dynamic_Parallelism.cu -o /tmp/dp_nordc.o
     ```
3. **需要观察的现象**：第二次编译在 `mandelbrot_block_k` 的三尖括号处报错；而基线 `Non_Dynamic_Parallelism.cu` 不含设备端发射，去掉 `-rdc=true` 也能编过。
4. **预期结果**：`-rdc=true` 缺失时动态版无法编译，基线版不受影响——这正说明该选项只为「设备端启动 kernel」这一特性服务。具体报错文本与你的 nvcc 版本有关，「待本地验证」。
5. 若没有 GPU 但装了 CUDA 工具链，本实验仍然可做（编译不需要 GPU，运行才需要，见 u1-l2）。

#### 4.5.5 小练习与答案

**练习 1**：`-rdc=true` 解决的是什么问题？为什么普通程序不需要它？

答案：它让 nvcc 生成「可重定位」的设备目标码，并启用设备链接阶段。普通 CUDA 程序里 kernel 只被 host 调用，发射所需的设备码地址在普通链接期即可确定；而动态并行中，发射语句写在 kernel（设备码）内部，被调 kernel 的地址必须等设备码之间互相链接时才能敲定。因此凡设备端要引用别的 kernel，就需要可重定位设备码。

**练习 2**：Makefile 把 `-lpng` 同时放进了编译（`-c`）命令和链接命令，这在语义上有什么问题？为什么实际没出事？

答案：`-c` 只编译不链接，链接器相关的 `-lpng` 在这一步是无效参数（nvcc 会容忍并忽略它），真正起作用的是最后两条链接命令里的 `-lpng`。没有出事只是因为 nvcc 对多余链接选项不报错——这属于 Makefile 写法不严谨的例子，读真实项目代码时要能分辨「哪些选项在哪一步真正生效」。

**练习 3**：为什么说用这个 Makefile 直接得到的可执行文件不适合作为最终性能结论的依据？如何修正？

答案：因为对象文件是带 `-g -G` 编译的，`-G` 会关闭设备端优化（u1-l2、u3-l1 都强调过），两版的耗时都被整体抬高，收益比例可能失真。修正方法：把 `OPT` 改为空或只保留 `-g`（去掉 `-G`）后重新 `make clean && make`，再计时；同时两版要用同一组标志编译，保持除「调度方式」外其他因素一致——这是微基准对照实验的基本纪律。

## 5. 综合实践

把本讲全部内容串成一次完整的对照实验。**需要一台计算能力 ≥ 3.5 的 NVIDIA GPU**；若无 GPU，请完成 4.2.4 的决策表推演与 4.5.4 的编译实验作为替代。

### 5.1 环境准备

```bash
nvidia-smi                                # 记录 GPU 型号与显存
nvcc --version                            # 记录 CUDA 版本
sudo apt-get install libpng-dev           # PNG 依赖
cd DynParallel/
# 查看你 GPU 的计算能力，必要时把 Makefile 里 -arch=sm_86 改成对应值
```

### 5.2 第一阶段：跑通并取得基线数据

```bash
make
ls -la Dynamic_Parallelism Non_Dynamic_Parallelism
./Non_Dynamic_Parallelism                 # 记录 "Work took X seconds"，产出 mandelbrot.png
./Dynamic_Parallelism                     # 记录耗时，产出 mandelbrot_dp.png
```

提醒：出厂 `W = H = 16000` 意味着约 0.95 GiB 显存和 2.56×10⁸ 个像素的逐像素计算，且可执行文件带 `-G`（设备优化关闭）。若太慢或显存不足，可先把 [DynParallel/Dynamic_Parallelism.cu:L272-L273](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/DynParallel/Dynamic_Parallelism.cu#L272-L273) 与 [DynParallel/Non_Dynamic_Parallelism.cu:L199-L200](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/DynParallel/Non_Dynamic_Parallelism.cu#L199-L200) 的 `H`/`W` 同步改小（如 `4 * 500 = 2000`，注意 `W` 最好保持能被你选的 `INIT_SUBDIV`、`SUBDIV` 整除），并保证两个文件改得一致——别破坏对照性。改完源码记得最后 `git checkout DynParallel/` 恢复。

打开两张 PNG 对比：动态版颜色分区应与基线一致（同一数学对象），但因两文件 `dwell_color` 的调色不同（`CUT_DWELL` 一个取 `MAX_DWELL/4`、一个取 `MAX_DWELL/MAX_DEPTH`），色调会有差异——不要把配色差异当成计算差异。

### 5.3 第二阶段：性能口径修正

按 4.5 练习 3 的方法去掉 `-G` 重新编译两版，重复运行各 3 次取中位数，记录：

| 版本 | 运行 1 / 2 / 3 | 中位数（秒） |
|---|---|---|
| Non_Dynamic_Parallelism | … | … |
| Dynamic_Parallelism | … | … |

预期（「待本地验证」）：动态版应明显快于基线，因为画面大部分区域（集合内部与远外围）边界均匀、被整块填充跳过；收益幅度取决于画面中「均匀大块」的占比。

### 5.4 第三阶段：调整分块粒度，观察收益变化

只改 [DynParallel/Dynamic_Parallelism.cu:L41](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/DynParallel/Dynamic_Parallelism.cu#L41) 的 `INIT_SUBDIV`，依次取 64、16、8、4、2、1（保持 W 不变），每次 `make Dynamic_Parallelism && ./Dynamic_Parallelism`，记录耗时，并与 4.2.4 预测表逐行核对：

- `INIT_SUBDIV` 变小 → 根方块变大 → 均匀方块能「一口吞」的面积变大，填充收益上升；
- 但非均匀大方块要经过更多层「边界探测 + 细分」才能到达叶子，边界计算与设备端发射次数上升；
- 递归真正开始出现的门槛（`d₀/16 > 64`，即 `d₀ ≥ 1025`，对应 `INIT_SUBDIV ≤ 15`）前后，收益曲线应当有可察觉的形态变化。

进一步（可选）：把 `SUBDIV` 从 16 改成 4（保持其他不变），观察递归树能长到第 4 层左右时的表现；再调 `MAX_DWELL`（如 512 vs 2048）观察「负载差异被放大/压缩」对动态版相对收益的影响——`MAX_DWELL` 越大，被跳过的内部像素原本越贵，填充的相对收益越高。

### 5.5 实验报告要点

写一段简短结论，须包含：(1) 你的 GPU 型号与 `-arch` 取值；(2) 是否带 `-G`；(3) 粒度扫描表；(4) 用「均匀块占比、边界探测成本 4d、设备端发射开销」三个因素解释你观察到的趋势；(5) 至少一条与 4.2.4 预测不符或出乎意料的地方（如果全部吻合，也写明预测依据）。

## 6. 本讲小结

- Mandelbrot 绘制是**不规则负载**的典型：单像素代价（dwell，1..2048 次迭代）相差三个数量级，固定网格 + 均匀划分的 `compute` 无法在运行时把空闲算力调配给慢区域。
- **动态并行**让设备端代码用与主机端相同的三尖括号语法发射子 kernel；父 grid 要等所有子孙完成，因此主机一次 `cudaDeviceSynchronize` 即可收割整棵递归树。
- `mandelbrot_block_k` 是「计算 + 调度」合一的 kernel：`border_dwell` 以 \( 4d \) 的低成本探测边界一致性，均匀则 `dwell_fill_k` 整块填充（收益兑现），否则细分或退化为 `pixel_calc` 逐像素（Mariani–Silver 算法）。
- 一个重要实证：出厂常量（`W=16000, INIT_SUBDIV=64, SUBDIV=16, MIN_SIZE=64`）下 \( d_0 = 250 \)，递归条件 \( 250/16 > 64 \) 不成立，**递归分支实际不可达**，收益来自「跳过均匀区域」而非多层自适应；调小 `INIT_SUBDIV` 才会出现真正的递归树。
- 编译前提是 `-rdc=true`（可重定位设备码，供设备链接期解析被调 kernel 地址），配套 `--cudart=shared`、`-arch=sm_86`（按机器改）、`-lpng`（需 libpng-dev）；Makefile 以 `-g -G` 编译对象，做性能结论前应去掉 `-G`。
- 本目录无 `test.sh`，实验命令需手工执行；两版计时口径对称（只含 launch + 同步），可直接对比。

## 7. 下一步学习建议

- **下一讲 u3-l3（Conkernels：并发 kernel 与 CUDA stream）**：本讲的「设备端生成工作」是纵向的嵌套，下一讲的「多条流并发执行多个 kernel」是横向的并行，两者同属「让 GPU 排满工作」这一主题的不同侧面，对比学习效果最好。
- 若想再夯实本讲的归约细节，可先读 u4-l5（BankRedux：共享内存 bank 冲突与归约算法改写），回头再看 `border_dwell` 的树形归约会非常轻松。
- 延伸阅读：NVIDIA《CUDA C++ Programming Guide》中 *Dynamic Parallelism* 一章（ nesting 语义、设备运行时的限制、与流的交互），以及 Mariani–Silver 关于「边界同色即整块填充」的经典自适应细分讨论。
- 回到仓库：把本讲的「读 Makefile 认标志」方法用到 `Conkernels/Makefile` 与 `Conkernels/Makefile_serialized` 上，预习下一讲的串行/并发对照设计。
