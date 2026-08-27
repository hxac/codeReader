# Shuffle：warp 内寄存器交换 __shfl_down_sync

## 1. 本讲目标

学完本讲，你应该能够：

1. 掌握 `__shfl_down_sync(mask, val, offset)` 的**掩码（mask）**与**偏移（offset）**两个参数的含义，手推一个 warp 内 5 步 shuffle 归约后每个 lane 持有的值。
2. 读懂 `Shuffle/cuda_shuffle` 的 `reduce3`：它如何用 `warpReduceSum` 让数据**全程停留在寄存器**里完成 warp 内归约，只在「warp 间汇合」时借道 8 个共享内存字。
3. 说清 shuffle 归约相对共享内存树形归约（`reduce2` 的 `cg::sync` + `sdata` 循环）的优势来源：**无块级栅栏、无共享内存往返、跨 warp 汇合一次完成**。
4. 辨认本基准的**反模式**版本——`Shuffle/cuda_global` 的 `reduce3` 把中间结果写回**全局内存** `g_odata[i]` 逐步归约——并解释它为什么最慢、为什么它的主机代码被迫为 `d_odata` 分配整块 `bytes`。
5. 读懂 `reduce()` 模板分发函数如何用 `whichKernel` 把 7 个 kernel 接到同一份测量框架上，并注意 `test.sh` **没有传 `kernel=` 参数**、默认运行的是 kernel 3 这个关键细节。

## 2. 前置知识

### 2.1 归约的真正瓶颈：线程间「交换介质」

归约（reduction）是把 N 个数用满足结合律的运算合成 1 个数。u4-l1（分块矩阵乘）与 u4-l5（bank 冲突）已经建立了两条背景：

- 树形归约每步让一半线程把另一半线程的部分和加进来，block 内 256 个元素需要 \(\log_2 256 = 8\) 步；
- u4-l5 的 `sum_cudakernel` 与本讲 `reduce2` **逐字同构**——折半步长、连续下标、无 bank 冲突；`sum_cudakernel_bc` 对应本仓库的 `reduce1`（交错寻址、有冲突）。

本讲往前再走一步：归约的本质困难不是「算」，而是**线程间交换数据必须经过某种介质**。介质有三个候选，代价依次升高：

| 交换介质 | 典型写法 | 同步方式 | 相对代价 |
| --- | --- | --- | --- |
| 寄存器 + shuffle 指令 | `__shfl_down_sync` | 指令自身（mask 即参与者集合） | 最低：数据不出寄存器堆 |
| 共享内存 | `sdata[tid] += sdata[tid+s]` | `__syncthreads()` / `cg::sync` 块级栅栏 | 中：写-读往返 + 块内所有线程陪跑栅栏 |
| 全局内存 | `g_odata[i] += g_odata[i+s]` | 同上，且跨 block 时还要主机中转 | 最高：走 DRAM，延迟与带宽都差一个数量级 |

README 汇总表对本基准的定性正是如此（[README.md:66-68](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/README.md#L66-L68)）：反模式是「线程间交换数据」（借道共享/全局内存），优化技术是「用 shuffle 让同一 warp 内的线程直接在**寄存器之间**共享部分结果」。仓库用两个目录把这句话做成了可运行的对照实验。

### 2.2 lane 与 warp 编号（承接 u3-l1）

u3-l1 已确立：warp 是 32 个线程（lane 0～31）组成的调度与执行单位。本讲频繁使用两个下标，先固定记号——对 block 内线程号 `threadIdx.x`（记作 `tid`）：

\[
\text{lane} = tid \bmod 32, \qquad \text{wid} = tid \,\div\, 32
\]

`lane` 是线程在**自己 warp 里**的座位号，`wid` 是它属于第几个 warp。256 线程的 block 恰好有 `256/32 = 8` 个 warp。

### 2.3 `__shfl_down_sync`：一条在寄存器之间搬数的指令

shuffle（洗牌）指令族是 Kepler（sm_35）起引入的 warp 内数据交换原语。本讲用的是「向下取」的变体：

```cpp
T neighbor = __shfl_down_sync(mask, val, offset);
```

- `val`：**自己寄存器里**的值，每线程一份；
- `offset`：向「lane 编号更大」的方向偏移多少个座位取数，即 lane \(i\) 取到 lane \(i+\text{offset}\) 的 `val`；若 \(i+\text{offset} \ge 32\)（源在 warp 外），返回**调用者自己的值**；
- `mask`：32 位掩码，声明「哪些 lane 参与这次交换」。本讲源码写 `(unsigned int)-1` 即 `0xFFFFFFFF`，表示全部 32 个 lane 都参与。

两个必须理解的要点：

1. **mask 兼任 warp 内同步**。Volta 之前 warp 严格锁步，老的 `__shfl` 不需要 mask；Volta 之后线程可以独立调度，`*_sync` 版本要求参与线程带着同一 mask 到达同一指令——这本身就是一次「warp 内栅栏」，比块级 `__syncthreads()` 便宜得多（只等 32 个人，不等整个 block）。
2. **shuffle 不产生任何内存事务**。它由 warp 内的专用数据交换通路完成，没有地址计算、没有 bank、没有缓存——这正是它相对共享内存的全部优势来源。

### 2.4 这两个目录从哪来

`Shuffle/` 下没有自己的 README，两个子目录各带一份源自 **NVIDIA CUDA Samples** 的 `reduction` 样例（版权头是 NVIDIA 2019，[Shuffle/cuda_shuffle/README.md](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/README.md#L1-L13) 说明它演示数据并行算法的性能优化策略）。git 历史显示它们在 `c82eb3f`（"Added the tests for new CUDA features"）加入、`0becbb2`（"Restructure the repo"）移到当前位置；另有 `8c8cd59` 添加了一个指向外部仓库的 git 子模块 `case_study_shuffle`（工作区中未检出，内容待确认）。用 `diff` 核对可以确认：**两目录的全部差异只有两处**——

1. `reduction_kernel.cu`：`cuda_shuffle` 多了 `warpReduceSum`，且 `reduce3` 的实现完全不同；
2. `reduction.cpp`：`cuda_global` 把 `d_odata` 的分配从 `numBlocks * sizeof(T)` 改成了整块 `bytes`（原因见 4.2.3）。

Makefile、test.sh、reduction.h 逐字相同。这是全仓库里**最干净的单变量对照**之一。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [Shuffle/cuda_shuffle/reduction_kernel.cu:144-169](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/reduction_kernel.cu#L144-L169) | `reduce2`：共享内存树形归约，本讲参照系（两目录逐字相同） |
| [Shuffle/cuda_shuffle/reduction_kernel.cu:171-208](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/reduction_kernel.cu#L171-L208) | 本讲主角：`warpReduceSum`（shuffle 归约子程序）+ `reduce3`（shuffle 版块内归约） |
| [Shuffle/cuda_global/reduction_kernel.cu:175-202](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_global/reduction_kernel.cu#L175-L202) | 反模式：`reduce3` 的**全局内存**版本，中间结果写回 `g_odata[i]` |
| [Shuffle/cuda_shuffle/reduction_kernel.cu:409-658](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/reduction_kernel.cu#L409-L658) | `reduce()` 模板分发函数：`whichKernel` 0～6 → 7 个 kernel；显式模板实例化 |
| [Shuffle/cuda_shuffle/reduction.cpp:421-427](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/reduction.cpp#L421-L427) | 主机侧实验参数：`size`、`maxThreads=256`、**默认 `whichKernel=3`**、`maxBlocks=64` |
| [Shuffle/cuda_shuffle/reduction.cpp:201-321](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/reduction.cpp#L201-L321) | `getNumBlocksAndThreads`（网格计算）与 `benchmarkReduce`（100 轮平均 + 多级归约级联） |
| [Shuffle/cuda_global/reduction.cpp:493-495](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_global/reduction.cpp#L493-L495) | 关键旁证：全局内存版被迫为 `d_odata` 分配 `bytes`（而非 `numBlocks*sizeof(T)`） |
| [Shuffle/cuda_shuffle/Makefile](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/Makefile#L242-L253) | CUDA Samples 风格 Makefile：`-I../../Common`、`SMS` 多架构列表 |
| [Shuffle/cuda_shuffle/test.sh:1-4](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/test.sh#L1-L4) | 实验设计：4 个规模（16M～128M），**注意它没有传 `kernel=`** |
| [Shuffle/cuda_shuffle/result.txt](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/result.txt#L63-L67) 与 [Shuffle/cuda_global/result.txt](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_global/result.txt#L63-L67) | V100（Tesla V100-PCIE-32GB）上 kernel 3 的归档测量，无 GPU 环境的云实验数据 |

## 4. 核心概念与源码讲解

### 4.1 reduce2：共享内存树形归约（参照系）

#### 4.1.1 概念说明

`reduce2` 是「正确的共享内存归约」的标准写法：**顺序寻址**（sequential addressing）——步长折半、下标连续。u4-l5 已经分析过它为什么无 bank 冲突：warp 内活跃线程访问的 `sdata` 下标连续，落在不同 bank 上。它也是 NVIDIA 样例注释里认定的第一个「无发散、无冲突」版本（[Shuffle/cuda_shuffle/reduction_kernel.cu:141-143](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/reduction_kernel.cu#L141-L143)）。

本讲把它当作参照系：它已经把共享内存方案能做到的都做到了（无冲突、无模运算、线程连续），**剩下的开销就是共享内存这种介质本身的开销**——这正是 shuffle 要消灭的对象。

#### 4.1.2 核心流程

设 block 大小 \(B = 256\)，每线程先把自己的元素搬进共享内存，然后：

```
载入：sdata[tid] = g_idata[全局下标]        # 256 次全局读 + 256 次共享写
栅栏：cg::sync(cta)
归约：for s = B/2, B/4, ..., 1:            # 8 次迭代
        if tid < s: sdata[tid] += sdata[tid+s]   # 活跃线程减半：128,64,...,1
        cg::sync(cta)                      # 每步一次全块栅栏（256 线程都到）
写出：tid==0 把 sdata[0] 写到 g_odata[blockIdx.x]
```

用开销的眼光数一遍（这是本讲的核心账本）：

- **迭代步数**：\(\log_2 B = 8\) 步；
- **块级栅栏**：8 次（不含载入后的那次共 9 次），每次都要 256 个线程全部到场；
- **闲置**：从 \(s=32\) 起，活跃线程 \(\le 32\)，即整个 block 只有 1 个 warp 在干活，**其余 7 个 warp 什么都不做、只是反复过栅栏**；最后一步只有 1 个线程在加法；
- **共享内存流量**：每次迭代活跃线程读 2 个字、写 1 个字，累计约 \(3 \times 255\) 次访问；
- **每线程只处理 1 个元素**：处理 \(n\) 个元素需要 \(n/256\) 个 block（见 4.3.2 的网格公式）。

#### 4.1.3 源码精读

载入与同步（[Shuffle/cuda_shuffle/reduction_kernel.cu:150-156](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/reduction_kernel.cu#L150-L156)）：`cg::this_thread_block()` 取得块组句柄，越界线程补 0 保证任意 `n` 都能算。

归约主循环（[Shuffle/cuda_shuffle/reduction_kernel.cu:159-165](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/reduction_kernel.cu#L159-L165)）：`s` 从 `blockDim.x/2` 向下折半；`cg::sync(cta)` 是 cooperative groups 对 `__syncthreads()` 的封装，注意它**在 `if (tid < s)` 之外**——所有线程无论活跃与否都要过栅栏，这正是树形归约「大部队陪跑」的根源。

结果写出（[Shuffle/cuda_shuffle/reduction_kernel.cu:167-168](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/reduction_kernel.cu#L167-L168)）：每个 block 只写 1 个字到 `g_odata[blockIdx.x]`，块间求和留给主机侧级联（4.3.2）。

顺带指出：u4-l5 的 `sum_cudakernel`（BankRedux）与这段循环结构完全一致；本仓库 `reduce1`（[Shuffle/cuda_shuffle/reduction_kernel.cu:112-139](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/reduction_kernel.cu#L112-L139)）就是那里的 `bc` 冲突版——两讲互为印证。

#### 4.1.4 代码实践：数账本 + 换 block 大小

1. **实践目标**：把 4.1.2 的开销账本落到具体数字，并验证「对这个 kernel，block 大小不是主导因素」。
2. **操作步骤**（需 GPU，待本地验证）：
   ```bash
   cd Shuffle/cuda_shuffle && make            # 若默认 SMS 含老架构报错，改用 make SMS="80" 之类按本机算力
   ./reduction.out n=1048576 kernel=2 threads=256    # 记录 Time 与 blocks
   ./reduction.out n=1048576 kernel=2 threads=128
   ./reduction.out n=1048576 kernel=2 threads=512
   ```
3. **需要观察的现象**：打印的 `blocks` 随 threads 反比变化（4096 / 8192 / 2048）；`Time` 变化幅度远小于 threads 的变化幅度。
4. **预期结果**：该 kernel 的主要时间花在**从全局内存载入** `g_idata`（访存受限，与 u2-l1 的 AXPY 同理），块内归约只占尾部；threads=512 时归约少一步栅栏，但载入量不变，故 Time 仅有小幅波动。
5. 若无 GPU：在纸上为 threads=128/256/512 三种情况分别写出「迭代步数、栅栏次数、最后一次迭代时活跃 warp 数」三行数字即可（答案分别是 7/8/9 步、同栅栏数、恒为 1 个活跃 warp）。

#### 4.1.5 小练习与答案

**练习 1**：`reduce2` 在 256 线程下，哪几步迭代整个 block 只有一个 warp 活跃？此时其它 7 个 warp 在做什么？

**答案**：\(s = 32, 16, 8, 4, 2, 1\) 共 6 步活跃线程 \(\le 32\)（\(s=32\) 时恰好占满 1 个 warp）。其余 warp 的线程不满足 `tid < s`，跳过加法，但必须执行循环里的 `cg::sync(cta)`——它们在「陪跑栅栏」。

**练习 2**：把 `reduce2` 的循环方向反过来（`s` 从 1 到 `blockDim.x/2` 翻倍、下标改成 `2*s*tid`），会发生什么？

**答案**：这正是本仓库 `reduce1` 的写法，产生 u4-l5 分析过的共享内存 bank 冲突（2、4 乃至 8 路），同时线程集合是交错的（没有整 warp 活跃）。它比 `reduce0`（模运算版）好，但比 `reduce2` 慢。

**练习 3**：`reduce2` 每个线程只处理 1 个输入元素。若不改归约引擎、只让每线程串行处理 2 个元素（载入时 `mySum = g_idata[i] + g_idata[i+B]`），block 数会怎么变？这对后面 `reduce3` 的对比意味着什么？

**答案**：block 数减半（每 block 覆盖 \(2B\) 个元素）。这意味着将来对比 `reduce2` 与 `reduce3` 时，**两者其实差了两个变量**（每线程元素数、归约引擎），解读数据时必须把「块数减半」的收益从「shuffle 的收益」里剥离——这正是 4.2.4 实践要做的变量分离。

### 4.2 warpReduceSum 与 reduce3（cuda_shuffle）：shuffle 版归约

#### 4.2.1 概念说明

`reduce3`（shuffle 版）把块内归约拆成**两级**：

- **第一级（warp 内）**：每个 warp 用 5 条 `__shfl_down_sync` 指令把 32 个寄存器值归约到 lane 0——**零共享内存、零块级栅栏**；
- **第二级（warp 间）**：8 个 warp 的 lane 0 各写 1 个部分和到共享内存（共 8 个字），一次 `cg::sync` 后，由 warp 0 把这 8 个值再用同样的 shuffle 梯子加起来。

关键洞察：**warp 内的通信根本不需要共享内存**。共享内存只在「跨 warp」这一步不可替代（不同 warp 之间没有直接的数据通路），而这一步的数据量从 `reduce2` 的逐层折半缩小到了「8 写 + 8 读 + 1 次栅栏」。

同时它做了一处算法级改动：每线程在**载入全局内存时**就顺手累加 2 个元素（第一级归约融进载入），block 数因此减半。NVIDIA 注释明说这是 "uses n/2 threads"（[Shuffle/cuda_shuffle/reduction_kernel.cu:178-181](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/reduction_kernel.cu#L178-L181)）。

#### 4.2.2 核心流程

**shuffle 梯子的数学**。设 warp 内 lane \(i\) 的寄存器初值为 \(v_i\)，第 \(k\) 步的偏移 \(o_k = 2^{4-k}\)（即 16, 8, 4, 2, 1），每步执行：

\[
v_i \leftarrow v_i + v_{i+o_k} \quad (\text{若 } i+o_k \ge 32 \text{ 则取自身})
\]

归纳可得：完成偏移 \(o\) 的那一步之后，lane \(i\) 持有区间 \([i,\; i+2o)\) 的和；5 步走完，lane 0 持有

\[
v_0^{(5)} = \sum_{j=0}^{31} v_j
\]

高编号 lane 的值逐步变成「垃圾」（源越界取自身），但没人读它们，不影响正确性。用一个 8-lane 的缩小版演示（偏移 4, 2, 1）：

| 步 | lane 0 | lane 1 | lane 2 | lane 3 | lane 4+ |
| --- | --- | --- | --- | --- | --- |
| 初始 | \(v_0\) | \(v_1\) | \(v_2\) | \(v_3\) | … |
| \(o=4\) | \(v_0{+}v_4\) | \(v_1{+}v_5\) | \(v_2{+}v_6\) | \(v_3{+}v_7\) | 不再被使用 |
| \(o=2\) | \(v_0{+}v_4{+}v_2{+}v_6\) | \(v_1{+}v_5{+}v_3{+}v_7\) | 垃圾 | 垃圾 | 垃圾 |
| \(o=1\) | \(\sum_{0}^{7} v_j\) | 垃圾 | 垃圾 | 垃圾 | 垃圾 |

**kernel 全流程伪代码**（B=256，8 个 warp）：

```
i = blockIdx.x * 2B + threadIdx.x
mySum = g_idata[i] + g_idata[i + B]      # 第一级归约融进载入（每线程 2 元素）
mySum = warpReduceSum(mySum)              # 5 条 shuffle，每 warp 独立完成
if lane == 0: sdata[wid] = mySum          # 8 个 warp 首领各写 1 个字
cg::sync(cta)                             # 全 kernel 唯一一次块级栅栏
mySum = (tid < B/32) ? sdata[lane] : 0    # warp 0 的 lane 0..7 读回 8 个部分和
if wid == 0: mySum = warpReduceSum(mySum) # 再来 5 条 shuffle 收尾
if tid == 0: g_odata[blockIdx.x] = mySum
```

开销账本对比 `reduce2`（B=256）：块级栅栏 **9 → 1** 次；共享内存访问 **约 765 次 → 16 次**（8 写 + 8 读）；block 数 **减半**；取而代之的是每 warp 10 条 shuffle 指令（两段梯子各 5 条），这些指令只动寄存器。

#### 4.2.3 源码精读

**`warpReduceSum`**（[Shuffle/cuda_shuffle/reduction_kernel.cu:171-176](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/reduction_kernel.cu#L171-L176)）：整个优化只靠这 6 行。`offset` 从 `warpSize/2`（32/2=16）除 2 降到 1；掩码 `(unsigned int)-1` 是全 32 lane。注意它被 `__inline__ __device__` 修饰并写成模板，编译器会把它直接内联成 5 条连续的 shuffle 指令，**循环本身不产生任何分支开销以外的同步**。

**载入即第一级归约**（[Shuffle/cuda_shuffle/reduction_kernel.cu:190-195](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/reduction_kernel.cu#L190-L195)）：全局下标公式 `blockIdx.x * (blockDim.x * 2) + threadIdx.x` 里的 `*2` 表明每 block 覆盖 `2B` 个元素；两个 `if` 越界保护处理任意 `n`。这两个全局读是**合并访问**（warp 内地址连续，u4-l2 的原则），且间距 `B` 的两段各自连续。

**两段式归约主体**（[Shuffle/cuda_shuffle/reduction_kernel.cu:197-204](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/reduction_kernel.cu#L197-L204)）：`lane`/`wid` 的计算就是 2.2 节的公式；`if (lane == 0)` 保证每个 warp 只有一个线程写 `sdata[wid]`。读回行的三目运算符 `(threadIdx.x < blockDim.x / warpSize) ? sdata[lane] : 0` 把 warp 0 的 lane 8..31 补 0，凑满 32 个值再走一遍梯子——因此第二段梯子实际只加 8 个非零数。最后 `if (tid == 0)` 写出（[Shuffle/cuda_shuffle/reduction_kernel.cu:206-207](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/reduction_kernel.cu#L206-L207)）。

一个值得玩味的细节：`if (wid == 0) mySum = warpReduceSum(...)` 的条件是 **warp 一致**的（warp 0 的 32 个 lane 同时进、其余 warp 整体跳过），所以内部用全 1 掩码是合法的——`_sync` 系列的掩码约束的是「warp 内参与的 lane 一致到达」，与别的 warp 无关。这与 u3-l1 的分支发散判据（同一 warp 内部分裂才有害）完全呼应。

**反模式对照：cuda_global 的 `reduce3`**（[Shuffle/cuda_global/reduction_kernel.cu:189-198](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_global/reduction_kernel.cu#L189-L198)）：前 5 行载入逻辑与 shuffle 版**完全相同**（同样每线程 2 元素），分岔在之后——它把 `mySum` 写进**全局内存** `g_odata[i]`，然后 `for (s = blockDim.x/2; s > 0; s >>= 1)` 每步对 `g_odata` 做一次「读两个全局字、写一个全局字」，共 8 轮、每轮 256 线程过栅栏。也就是说它把 `reduce2` 的树原样搬进了**全局内存**：既有 `reduce2` 的全部栅栏与陪跑，又把介质从片上共享内存换成走 DRAM 的全局内存（延迟高一个量级、且占用真实带宽）。它是本基准要打掉的「用错介质」极端样本。

**主机侧的铁证**（[Shuffle/cuda_global/reduction.cpp:493-495](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_global/reduction.cpp#L493-L495) vs [Shuffle/cuda_shuffle/reduction.cpp:492-493](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/reduction.cpp#L492-L493)）：全局版必须为 `d_odata` 分配整块 `bytes`（作者还留了一行被注释掉的 `numBlocks * sizeof(T)` 作对照），因为 `g_odata[i]` 的 `i` 覆盖了每个线程，需要 `n/2` 个元素的暂存空间；shuffle 版每 block 只写 1 个字，`numBlocks * sizeof(T)` 就够。介质选择的影响甚至体现在**显存占用量**上。

**归档数据**（V100，int，默认 kernel 3，256 线程）：n=134217728 时 shuffle 版 0.00091 s / 590.82 GB/s（[Shuffle/cuda_shuffle/result.txt:63-67](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/result.txt#L63-L67)），全局版 0.00121 s / 459.69 GB/s（[Shuffle/cuda_global/result.txt:63-67](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_global/result.txt#L63-L67)）——全局版慢约 33%，且差距随规模扩大（32M 时两者几乎打平，64M 时约 10%，128M 时约 28% 吞吐差）。

#### 4.2.4 代码实践：把「块数减半」从「shuffle 收益」里剥离

1. **实践目标**：`reduce3` 与 `reduce2` 同时差了两个变量（每线程元素数、归约引擎）。构造一个只差「归约引擎」的对照，量化 shuffle 本身的贡献。
2. **操作步骤**（在自己的一份本地拷贝上修改，不动仓库；需 GPU，待本地验证）：
   - 先跑基线：
     ```bash
     cd Shuffle/cuda_shuffle
     ./reduction.out n=134217728 kernel=2 threads=256    # 记 Time
     ./reduction.out n=134217728 kernel=3 threads=256    # 记 Time
     ```
   - 再做单变量改造：复制 `reduction_kernel.cu`，把 `reduce3` 中两处 `warpReduceSum` 调用替换为 `reduce2` 式的共享内存循环（**保留**「每线程 2 元素」的载入不变），即（示例代码，非项目原有代码）：
     ```cpp
     sdata[tid] = mySum;
     cg::sync(cta);
     for (unsigned int s = blockDim.x / 2; s > 0; s >>= 1) {
       if (tid < s) sdata[tid] = mySum = mySum + sdata[tid + s];
       cg::sync(cta);
     }
     // 删除原 197-204 行的两段 shuffle，写出仍用 if (tid == 0) g_odata[blockIdx.x] = mySum;
     ```
   重新编译（`make` 前先 `make clean`），同参数运行。
3. **需要观察的现象**：三者的 Time 排序应为 `cuda_global 版 reduce3（若也测）> 改造版（共享内存引擎 + 2 元素/线程）> 原版 reduce3（shuffle 引擎）`。
4. **预期结果**：改造版相对 `kernel=2` 的提升来自「块数减半 + 载入即归约」；改造版与原版 `reduce3` 的差才是 **shuffle 引擎本身**的净收益。用 `nvprof ./reduction.out n=134217728 kernel=3`（新工具链用 `nsys profile` / `ncu`，见 u1-l4）看 GPU activities 里 `reduce3<int>` 与改造版的 kernel 时间，比主机打印的 Time 更纯净。
5. 若无 GPU：完成 4.2.2 的 8-lane 表格推广到 32 lane（写出 5 步中每步 lane 0 持有的区间），并手算 `reduce3` 在 B=256 时的共享内存访问字数（16）与栅栏数（1），与 `reduce2`（约 765 次、9 次）对照。

#### 4.2.5 小练习与答案

**练习 1**：`warpReduceSum` 里若把掩码从 `0xFFFFFFFF` 改成 `0x00000001`（只有 lane 0 参与），会发生什么？

**答案**：语义被破坏。`__shfl_down_sync` 要求 mask 中列出的 lane 一致到达同一指令，且 lane 0 想取数的源 lane（16, 8, 4, 2, 1）并不在 mask 里——源 lane 不参与时返回值未定义，归约结果错误；正确做法是源与目的都进 mask，warp 内完整归约就直接用全 1 掩码。

**练习 2**：`reduce3` 读回行用 `sdata[lane]` 而不是 `sdata[tid]`，两者在 warp 0 里等价吗？在其它 warp 里呢？

**答案**：warp 0 内 `tid < 32`，`lane == tid`，两者等价。其它 warp 不执行读回（三目条件 `tid < blockDim.x/warpSize = 8` 已把范围限定在 warp 0 的前 8 个线程），故差别不可见；但写 `sdata[lane]` 表达了「按下标对齐 warp 布局」的意图，是更稳的写法。

**练习 3**：既然 shuffle 这么好，`reduce3` 为什么不把 8 个 warp 间的汇合也用 shuffle 完成？

**答案**：shuffle 只在**单个 warp 内部**有效，跨 warp 没有寄存器通路；跨 warp 交换必须经过共享内存（或全局内存）。所以最优结构是「warp 内 shuffle、warp 间共享内存」，这正是 `reduce3` 的两段式设计，也是后续 `reduce4/5/6` 反复复用的骨架。

### 4.3 reduce 模板分发与主机侧测量框架

#### 4.3.1 概念说明

7 个 kernel 是**函数模板**（类型参数 `T`，reduce4-6 还有编译期 `blockSize`），主机代码不能拿模板名当运行期变量用。`reduce()` 包装函数用两层 `switch` 完成从「运行期整数 `whichKernel`/`threads`」到「编译期模板实例」的分发，是 C++ 模板元编程与 CUDA kernel 启动配置结合的典型样例。同时它决定了三个实验要素：共享内存分配量、网格形状、以及（配合 `reduction.cpp`）测量口径。

#### 4.3.2 核心流程

**分发**：`switch (whichKernel)` 选 kernel（0-6），kernel 4-6 再嵌套 `switch (threads)` 把块大小变成模板参数（512/256/…/1），kernel 6 还按 `isPow2(size)` 再分两支——共实例化出数十个 kernel，最后用显式实例化（[Shuffle/cuda_shuffle/reduction_kernel.cu:660-668](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/reduction_kernel.cu#L660-L668)）为 int/float/double 各生成链接期符号。

**共享内存大小**（[Shuffle/cuda_shuffle/reduction_kernel.cu:415-418](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/reduction_kernel.cu#L415-L418)）：`threads <= 32` 时分配双倍，防止 reduce4/5/6 里 `sdata[tid + 32]` 越界。`reduce3` 实际只需要 `B/32` 个字，但框架统一按 `threads * sizeof(T)` 给——省下的共享内存并没有被用来提高 occupancy，是一个可以继续优化的点（留给 u6-l1）。

**网格计算**（[Shuffle/cuda_shuffle/reduction.cpp:209-215](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/reduction.cpp#L209-L215)）：kernel 0-2 每线程 1 元素，`blocks = ceil(n/threads)`；kernel 3-6 每线程 2 元素，`blocks = ceil(n/(threads*2))`——同一 n 下 kernel 3 的 block 数恰是 kernel 2 的一半。

**多级归约级联**（[Shuffle/cuda_shuffle/reduction.cpp:279-295](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/reduction.cpp#L279-L295)）：一次 `reduce` 调用只把 n 归到 `numBlocks` 个部分和；`benchmarkReduce` 在 GPU 上反复「D2D 拷贝 + 再归约」直到只剩 1 个值。**所以程序打印的 `Time` 是 100 轮「整个级联」的平均墙钟**（计时区间见 [reduction.cpp:257-311](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/reduction.cpp#L257-L311)，`sdkStartTimer`/`sdkStopTimer`），不只是单个 kernel 的时间——这解释了 result.txt 里 16M 与 32M 的 Time 几乎相同（小规模时级联的启动与拷贝开销主导，吞吐数字被压低），与 u1-l4/u2-l4 反复强调的「看清计时口径」一脉相承。

**默认参数的坑**（[Shuffle/cuda_shuffle/reduction.cpp:421-427](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/reduction.cpp#L421-L427) 与 [437-439](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/reduction.cpp#L437-L439)）：`whichKernel` 默认是 **3**。`test.sh`（[Shuffle/cuda_shuffle/test.sh:1-4](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/test.sh#L1-L4)）四条命令都没传 `kernel=`，因此**两目录归档结果全部是 kernel 3 的对比**（shuffle 版 vs 全局内存版）。要测 `reduce2` 必须显式加 `kernel=2`。命令行解析会剥掉前导 `-`（Common/helper_string.h 的 `stringRemoveDelimiter`），所以 `kernel=3`、`-kernel=3`、`--kernel=3` 三种写法等价。

**编译与运行**：Makefile 是 CUDA Samples 多架构模板（u6-l3 会展开讲），关键两行是 `INCLUDES := -I../../Common`（[Makefile:243](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/Makefile#L243)，helper 头文件来自 Common 目录）与默认 `SMS ?= 35 37 50 52 60 61 70 75`（[Makefile:249-253](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/Makefile#L249-L253)，新版 CUDA 已不支持 sm_35，会报错，需 `make SMS="<本机算力>"` 覆盖）。链接产物是 `reduction.out`（[Makefile:295-296](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/Makefile#L295-L296)），与 test.sh 一致；但 `run:` 目标执行的是 `./reduction`（[Makefile:298-299](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/Makefile#L298-L299)），与产物名不一致，`make run` 预计会找不到可执行文件（待本地验证）——请按 test.sh 的方式直接运行 `./reduction.out`。

#### 4.3.3 源码精读

分发函数签名与 kernel 2/3 两个分支（[Shuffle/cuda_shuffle/reduction_kernel.cu:409-436](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/reduction_kernel.cu#L409-L436)）：三尖括号的第三个参数 `smemSize` 就是动态共享内存字节数；kernel 0-3 的块大小是运行期的（直接用 `dimBlock`），kernel 4-6 是编译期的（嵌套 switch）。

warmup 与 100 轮平均（[Shuffle/cuda_shuffle/reduction.cpp:501-504](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/reduction.cpp#L501-L504)）：`testIterations = 100`，比本项目其它基准的 10 轮更多——因为单次级联只有 ~1 ms 量级，需要更多轮摊薄计时抖动。吞吐打印公式 `1.0e-9 * bytes / reduceTime`（[reduction.cpp:516-520](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/reduction.cpp#L516-L520)）里的 `bytes` 只算输入数组，级联中的 D2D 拷贝流量并未计入，故「吞吐」是有效口径而非物理带宽。

正确性验证（[Shuffle/cuda_shuffle/reduction.cpp:523-559](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/reduction.cpp#L523-L559)）：CPU 参考用 **Kahan 补偿求和**（[reduction.cpp:165-178](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/reduction.cpp#L165-L178)）压住浮点误差；int 要求严格相等，float/double 按 `1e-8*size` / `1e-12*size` 阈值判过。result.txt 中 `GPU result == CPU result` 且 `Test passed` 说明 shuffle 改写没有破坏数值行为（加法顺序改变仍落在阈值内）。

#### 4.3.4 代码实践：用 nvprof 的 Calls 列核对程序结构

1. **实践目标**：验证「打印的 Time 包含整条级联」，并确认 test.sh 跑的是 kernel 3。
2. **操作步骤**（需 GPU，待本地验证）：
   ```bash
   cd Shuffle/cuda_shuffle && make
   ./reduction.out n=16777216                 # 不加 kernel=，观察打印的 blocks 数
   nvprof ./reduction.out n=16777216 kernel=2  # 记 GPU activities 中 reduce2 的 Calls 与时间
   nvprof ./reduction.out n=16777216 kernel=3  # 记 reduce3 的 Calls 与时间
   ```
3. **需要观察的现象**：不加 `kernel=` 时打印 `32768 blocks`（= 16777216/512，反推每线程 2 元素、即 kernel 3；若默认是 kernel 2 应为 65536）；nvprof 中 kernel 的 `Calls` 数远大于 1（warmup 1 次 + 100 轮 × 每轮级联若干次），且伴随多次 `[CUDA memcpy DtoD]`。
4. **预期结果**：`reduce3` 的单 kernel 时间明显小于 `reduce2`（同为 100 轮平均），且 `reduce2` 的 Calls 更多（block 数翻倍导致级联多一级）。把两行 kernel 时间相除，得到与 4.2.4 互补的另一个口径的加速比。
5. 若无 GPU：读 [Shuffle/cuda_shuffle/reduction.cpp:279-295](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/reduction.cpp#L279-L295)，对 n=16777216、kernel=3 手算级联的前三级：s = 32768 → 64 → 1，写出每级的 blocks/threads，并数出一轮迭代共发起几次 kernel 与几次 D2D 拷贝。

#### 4.3.5 小练习与答案

**练习 1**：为什么 kernel 4-6 必须用嵌套 `switch (threads)` 把块大小变成模板参数，而 kernel 0-3 不用？

**答案**：kernel 4-5 把归约循环**手动展开**成 `if ((blockSize >= 512) && (tid < 256)) ...` 的固定层级（编译期常量才能被展开与裁剪），kernel 6 还有 `nIsPow2` 模板参数用于消掉越界判断；kernel 0-3 的循环用运行期 `blockDim.x` 就能写，无需编译期已知。

**练习 2**：`reduce3` 每块只需 8 个共享内存字，框架却分配了 256 个。这会带来什么潜在收益没有兑现？

**答案**：共享内存占用是决定每 SM 可驻留 block 数（occupancy）的资源之一。把分配降到 `B/32` 个字理论上能提高 occupancy、增加可驻留 warp 数；但框架统一分配未做此优化，且本 kernel 受全局载入主导，收益未必显现——这是 u6-l1 优化阶梯上「还有下一级」的例证。

**练习 3**：程序打印的 `Throughput` 为什么在 16M 时只有 73 GB/s、128M 时却有 590 GB/s（同一 V100、同一 kernel）？难道带宽随规模变大？

**答案**：不是。`Time` 是整条多级归约级联的墙钟，小规模时级联的 kernel 启动、D2D 拷贝等固定开销占比高，把有效吞吐压低；大规模时主 kernel 的真实带宽主导。这是「打印吞吐 ≠ 硬件带宽」的典型口径陷阱，应回到 nvprof 的单 kernel 时间判断。

## 5. 综合实践

把本讲全部内容串成一次三组对照实验（需 GPU；无 GPU 则以 result.txt 归档数据完成第 4 步的解读）：

1. **编译两套代码**：
   ```bash
   cd Shuffle/cuda_shuffle && make          # 产物 reduction.out
   cd ../cuda_global     && make
   ```
   若报 sm_35 之类架构错误，用 `make SMS="<本机算力>"` 覆盖默认列表。
2. **按 test.sh 复现归档实验**：在两个目录分别执行
   ```bash
   ./reduction.out n=16777216
   ./reduction.out n=33554432
   ./reduction.out n=67108864
   ./reduction.out n=134217728
   ```
   记录每行的 `Time` 与 `Throughput`。两目录命令完全相同，**测的都是默认 kernel 3**——差别只在 `reduce3` 的实现（shuffle vs 全局内存）。
3. **补上共享内存参照系**：回到 `cuda_shuffle`，对同样 4 个规模追加 `kernel=2` 运行；再对 n=134217728 用 `nvprof ./reduction.out n=134217728 kernel=2` 与 `kernel=3` 各采一次，取 GPU activities 中 `reduce2<int>` / `reduce3<int>` 的单 kernel 时间。
4. **整理成一张三列表**（n=134217728 一行示例，括号内为 V100 归档参考值，待本地验证你的机器）：

   | 版本 | 介质与同步 | Time（V100） | 相对结论 |
   | --- | --- | --- | --- |
   | `cuda_global` kernel=3 | 全局内存树 + 8 次块栅栏 | 0.00121 s（459.69 GB/s） | 反模式：中间结果走 DRAM |
   | `cuda_shuffle` kernel=2 | 共享内存树 + 9 次块栅栏 | 待本地测量 | 参照系：片上但栅栏重 |
   | `cuda_shuffle` kernel=3 | warp 内 shuffle + 1 次块栅栏 | 0.00091 s（590.82 GB/s） | 优化：数据留在寄存器 |

5. **写一段差异归因**（不少于 5 句），要求把观测到的差距拆到三个因素上：①介质（全局内存 → 共享内存 → 寄存器）；②栅栏次数（9 → 1）与 warp 陪跑；③每线程元素数（kernel 2 与 3 之间还差「块数减半」这一算法级变量，必须单独说明）。若你完成了 4.2.4 的单变量改造，用改造版数据支撑第 ③ 点。

## 6. 本讲小结

- 归约的本质开销在**线程间交换介质**：寄存器 + shuffle < 共享内存 < 全局内存；本基准用 `cuda_global/reduce3`（全局内存树）、`reduce2`（共享内存树）、`cuda_shuffle/reduce3`（shuffle）三个实现把这条代价阶梯做成了可测的对照。
- `__shfl_down_sync(mask, val, offset)` 让 lane \(i\) 直接读 lane \(i+\text{offset}\) 的寄存器；mask（全 1 即 32 lane 参与）兼作 warp 内同步；偏移 16→1 共 5 步即可把 32 个值归约到 lane 0，且高 lane 的「垃圾值」不影响正确性。
- shuffle 版 `reduce3` 的结构是「**warp 内 shuffle、warp 间共享内存**」：8 个 warp 首领各写 1 个部分和，唯一一次 `cg::sync` 后由 warp 0 再走一遍梯子；栅栏从 9 次降到 1 次，共享内存访问从约 765 次降到 16 次。
- shuffle 无法跨 warp——这是硬件边界而非写法限制；跨 warp 汇合必须借道共享内存。
- `reduce3` 相对 `reduce2` 还差「每线程 2 元素、块数减半」这一算法级变量，归因时要分离；V100 归档数据显示 shuffle 版对全局内存版在 128M 时吞吐高约 28%。
- 实验框架三处口径要盯住：`test.sh` 未传 `kernel=`（默认 3）、打印 `Time` 是 100 轮**整条级联**的平均墙钟、`Throughput` 只按输入字节数计算。

## 7. 下一步学习建议

- **本单元下一讲 u4-l7（GSOverlap）** 继续深挖存储层次的另一维：用 `__pipeline_memcpy_async` 把全局内存到共享内存的拷贝与计算重叠，形成多级流水线——它与本讲同属「让数据搬运离开关键路径」的主题。
- **u6-l1（reduce0→reduce6 优化阶梯）** 是本讲的自然延伸：`reduce4/5/6` 在 shuffle 之上叠加「块内共享内存循环只做到 s>32、最后一 warp 交给 shuffle」「循环完全展开」「每线程多元素（Brent 定理）」，把七级优化串成完整决策链，本讲的 `reduce2/reduce3` 正是阶梯的第 3、4 级。
- 继续阅读 [Shuffle/cuda_shuffle/reduction_kernel.cu:224-402](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/reduction_kernel.cu#L224-L402) 中的 `reduce4/5/6`，注意它们改用 `cg::thread_block_tile<32>::shfl_down`——那是 `__shfl_down_sync` 的 cooperative groups 封装，语义与本讲裸指令一致。
