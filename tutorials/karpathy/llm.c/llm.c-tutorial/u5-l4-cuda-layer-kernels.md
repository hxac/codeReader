# 各层 CUDA kernel：layernorm / encoder / gelu / adamw

## 1. 本讲目标

在 u5-l1 我们建立了 `train_gpt2.cu` 的「骨架地图」：`floatX` 精度宏、`ParameterTensors`/`ActivationTensors`、`TensorSpec`、`recompute`。本讲往下钻一层，进入 `llmc/` 头文件库里**真正由自己手写的 GPU kernel**（不走 cuBLAS/cuDNN 的那些层），看看它们是如何把 CPU 参考实现里的一层循环，翻译成 GPU 上成千上万个线程的并行计算的。

读完本讲，你应该能够：

1. 说出「元素级 kernel 模板」是什么（`idx` 守卫 + 向量化访问），并用它解释 `gelu`、`residual`、`encoder_forward` 的并行映射。
2. **核心目标**：对照 `train_gpt2.c` 的 `layernorm`，讲清楚 CUDA 版如何用 **warp 级归约（`warpReduceSum`）**替代 CPU 的内层 `for` 循环来求 mean/var，以及 `blockReduce` 在 `global_norm` 里的作用。
3. 理解 `encoder_backward` 为什么不能简单地用 `atomicAdd` 做 scatter-add，以及它用「CPU 分桶 + GPU 归约」实现**确定性**反向的思路。
4. 解释 `adamw` 如何把 1.24 亿个参数逐元素并行更新，以及 `global_norm` 如何用 grid-stride + 两阶段归约算出梯度范数供裁剪使用。

---

## 2. 前置知识

本讲默认你已经掌握以下内容（来自前置讲义）：

- **u2-l2**：LayerNorm 的数学定义（mean / var / rstd）、pre-norm 位置，以及 CPU 版 `layernorm_forward/backward` 的三重循环写法。本讲会反复回到那段 C 代码做对照。
- **u5-l1**：`floatX` 是编译期类型别名（`float` / `half` / `__nv_bfloat16`，默认 BF16）；`ActivationTensors` 是混合精度（主激活跟 `floatX`，mean/rstd 恒为 `float`）。
- **u2-l1**：encoder 的前向是 gather（查表），反向是 scatter-add（散播累加，用 `+=`）。

本讲会补充几个 GPU 编程概念，初学者不熟悉的术语在出现处会解释：

- **线程 / warp / block / grid**：GPU 的三层执行模型。一个 **warp** 是 32 个同时执行的线程（`WARP_SIZE == 32`），是最小的同步与广播单位；多个 warp 组成一个 **block**（共享一块 shared memory）；多个 block 组成一个 **grid**。
- **归约（reduction）**：把一串数合并成一个数（如求和、求最大）。本讲重点是「如何让 32 个线程各算一部分，再合并成 1 个总和」。
- **shuffle 指令 `__shfl_xor_sync`**：warp 内线程之间直接交换寄存器值，不走内存，是 GPU 上最快的通信方式。

> 一句话直觉：CPU 版的每个「位置 (b,t)」是一个串行任务，外层 `for b, for t`；GPU 版把不同 `(b,t)` 分给不同 warp 并行，把同一个 `(b,t)` 内对 C 个通道的求和，用 32 个线程「切片 + 归约」并行掉。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [llmc/layernorm.cuh](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/layernorm.cuh) | LayerNorm 前向/反向 kernel、残差前向 kernel、残差+LayerNorm 融合 kernel。**本讲核心**。 |
| [llmc/encoder.cuh](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/encoder.cuh) | token+position embedding 前向（gather）与反向（scatter-add，确定化分桶）。 |
| [llmc/gelu.cuh](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/gelu.cuh) | GELU 前向/反向（inplace）kernel，元素级并行模板的最佳示例。 |
| [llmc/adamw.cuh](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/adamw.cuh) | AdamW 逐参数并行更新 kernel、master weights 初始化 kernel。 |
| [llmc/global_norm.cuh](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/global_norm.cuh) | 全局梯度范数（平方）计算 kernel，服务于梯度裁剪。 |
| [llmc/cuda_utils.cuh](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/cuda_utils.cuh) | `Packed128`/`x128`/`f128`、128 位 load/store、`warpReduceSum`/`warpReduceMax`/`blockReduce`、`stochastic_rounding`。是上述 kernel 共用的「螺丝刀」。 |
| [llmc/cuda_common.h](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/cuda_common.h) | `WARP_SIZE`、`CEIL_DIV`、`floatX` 等基础定义。 |
| [train_gpt2.c](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c) | CPU 参考实现，本讲作对照标尺（layernorm 78-161 行）。 |

---

## 4. 核心概念与源码讲解

### 4.1 元素级 kernel 模板与 128 位向量化访问

#### 4.1.1 概念说明

最容易并行化的算子是**元素级（element-wise）**算子：每个输出元素只依赖同位置的一个或几个输入元素，彼此完全独立。GELU、残差加法、encoder 前向都属于这一类。它们共享同一个并行化模板：

> 把长度为 N 的输出数组，按线程编号切片；每个线程计算 `blockIdx.x * blockDim.x + threadIdx.x` 这个下标对应的元素，加一个 `if (idx < N)` 守卫挡住越界线程。

llm.c 在此基础上做了一层**向量化**：不一个线程算一个元素，而是一个线程一次处理 `x128::size` 个元素（BF16 下是 8 个），用一条 128 位的 `LDG.128` 指令一次性读入。`Packed128` 就是用来强制编译器生成这条指令的结构体：

```cpp
template<class ElementType>
struct alignas(16) Packed128 {
    static constexpr const size_t size = sizeof(int4) / sizeof(ElementType); // BF16 时为 8
    ElementType payload[size];
};
typedef Packed128<floatX> x128;   // 主激活用
typedef Packed128<float>   f128;  // fp32 统计量用
```

`x128::size` 随精度变化：FP32→4、FP16/BF16→8。这就是为什么启动宏里 grid 大小都要除以 `x128::size`。

#### 4.1.2 核心流程（GELU 前向为例）

GELU 的 tanh 近似式（与 u2-l5 一致）：

\[
\mathrm{GELU}(x)=0.5x\left(1+\tanh\left(\sqrt{\tfrac{2}{\pi}}\,(x+0.044715x^3)\right)\right)
\]

并行流程：

1. 算出本线程负责的起始下标 `idx = (grid,block,thread) * x128::size`。
2. 用 `load128cs` 一次性读入 8 个输入（`cs` = streaming hint，表示「这数据读完不再用，直接流过缓存」）。
3. 对这 8 个数逐个套公式。
4. 用 `store128` 写回（这里用 `store` 而非 `storecs`，因为 GELU 的输出紧接着会被下游算子用到，留在 cache 里有好处）。

#### 4.1.3 源码精读

GELU 前向 kernel，完整的四步模板：

[llmc/gelu.cuh:13-26](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/gelu.cuh#L13-L26) —— 计算 `idx`、`load128cs` 读入、逐元素套 GELU 公式、`store128` 写回。注意 `0.044715f * xi * xi * xi` 就是公式里的立方项。

启动器把 N 个元素切成 block：

[llmc/gelu.cuh:50-57](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/gelu.cuh#L50-L57) —— `block_size=512`，grid = `CEIL_DIV(N, block_size * x128::size)`，`assert(N % (block_size*x128::size)==0)` 保证不会出现「半个 128 位包」。

残差前向是更简的同款模板（`out = inp1 + inp2`）：

[llmc/layernorm.cuh:221-231](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/layernorm.cuh#L221-L231) —— 两个 `load128cs` 相加、一个 `store128`。

encoder 前向也是元素级，但它多一步 gather（查表）：先从一维 `idx` 反解出 `(b,t,c)`，再用 `inp[b*T+t]` 查到 token id `ix`，最后取 `wte[ix,c] + wpe[t,c]`：

[llmc/encoder.cuh:19-44](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/encoder.cuh#L19-L44) —— 把 u2-l1 讲的 `out[b,t,c]=wte[ix,c]+wpe[t,c]` 套进了「一个线程 8 个通道」的模板。

128 位 load/store 与 cache hint 工具函数：

[llmc/cuda_utils.cuh:53-76](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/cuda_utils.cuh#L53-L76) —— `load128/load128cs`（读）、`store128/store128cs/store128cg`（写：默认留 cache / 流过 / 只留 L2 不留 L1）。

#### 4.1.4 代码实践

**实践目标**：验证「向量化访问」对启动器 grid 计算的影响。

**操作步骤**：

1. 在 [llmc/gelu.cuh:53](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/gelu.cuh#L53) 看到 `assert(N % (block_size * x128::size) == 0)`。
2. 假设 GPT-2 124M 的 MLP 升维层 GELU 作用在 `B*T*4*C = 4*1024*4*768 = 12,582,912` 个元素上（取 B=4）。
3. 手算：默认 BF16 下 `x128::size=8`，`block_size=512`，每个 block 处理 `512*8=4096` 个元素。

**需要观察的现象 / 预期结果**：

- grid_size = `12,582,912 / 4096 = 3072` 个 block。
- 若改成 FP32（`x128::size=4`），同样 block_size 下每 block 处理 `2048` 个元素，grid 翻倍到 `6144`。
- 结论：`x128` 把精度变化「吸收」进向量化宽度，使 kernel 主体代码几乎不动。

> 是否真的逐位如此**待本地验证**（需要 GPU + 修改 `PRECISION` 重编译），但上面的算术关系直接来自源码常量，不依赖运行。

#### 4.1.5 小练习与答案

**练习 1**：为什么 GELU 前向用 `store128`，而 GELU 读取输入时却用 `load128cs`（带 streaming hint）？

**参考答案**：输入 `inp` 是前一层（fc 升维）的输出，GELU 算完就不会再用它了，用 `cs` 标记「流过缓存」可避免它挤占宝贵的 cache、给共享的权重留空间；而 GELU 的输出 `out` 马上要喂给 fcproj 降维层，留在 cache 里能减少下一次读 miss，所以用普通 `store128`。

**练习 2**：`x128::size` 在 BF16 下为什么是 8？

**参考答案**：`x128 = Packed128<floatX>`，`size = sizeof(int4)/sizeof(ElementType) = 16/2 = 8`。128 位 = 16 字节，BF16 每元素 2 字节，故装 8 个。

---

### 4.2 LayerNorm：用 warp 归约替代 CPU 内层循环（核心模块）

#### 4.2.1 概念说明

LayerNorm 不是元素级算子：每个位置 `(b,t)` 的输出依赖**同一行 C 个元素的统计量**（mean、var）。CPU 版用单线程对 C 做一个 `for` 循环求和；GPU 上若仍让一个线程串行扫 C=768 个元素，就浪费了 warp 里其余 31 个线程。

llm.c 的做法（`kernel3`）：**让一整个 warp（32 线程）合作处理一个 `(b,t)` 行**。32 个线程把 C 个通道「切片」并行求和，再用 **warp 级归约**把这 32 个部分和合并成 1 个总和。这正是本讲的核心，也是 `practice_task` 的主题。

Warp 归约依赖 shuffle 指令 `__shfl_xor_sync(mask, val, offset)`：它让 lane（warp 内线程号）`i` 与 lane `i XOR offset` 交换 `val`。把 offset 从 16 减半到 1，5 步即可让全 warp 的 32 个数两两相加、最终汇聚到每个线程都拿到全和：

```cpp
__device__ inline float warpReduceSum(float val) {
    for (int offset = 16; offset > 0; offset /= 2) {
        val += __shfl_xor_sync(0xFFFFFFFF, val, offset);
    }
    return val;
}
```

#### 4.2.2 核心流程

对位置 `(b,t)`，设它的行为 `x[0..C-1]`，warp 内 lane id 记为 `lane_id`：

1. **切片求和**（每个线程算一部分）：`for (i = lane_id; i < C; i += 32) sum += x[i];`
2. **warp 归约**：`sum = warpReduceSum(sum);` —— 现在**每个**线程的 `sum` 都等于全行总和。
3. **mean**：`m = sum / C;`（只让 lane 0 写回 mean 缓存）。
4. 同样的「切片 + 归约」算方差 `v`，得 `rstd = rsqrtf(v/C + 1e-5f)`。
5. 再用一次切片循环写归一化输出：`out[c] = rstd * (x[c] - m) * weight[c] + bias[c]`。

CPU 版对照（同一个算法，但内层循环是单线程串行）：

[train_gpt2.c:92-103](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L92-L103) —— CPU 用 `for (int i=0; i<C; i++)` 单线程求 mean 与 var；GPU 版把这层 `for` 拆成 32 个线程并行 + 一次 warpReduceSum。

并行映射关系：CPU 的外层 `for b, for t` → GPU 的不同 warp（`idx = blockIdx.x * num_warps + warp_id`）；CPU 的内层 `for i` → GPU 的「32 线程切片 + warp 归约」。

#### 4.2.3 源码精读

**`kernel3`：无 shared memory 的 warp 归约版**（本节主角）：

[llmc/layernorm.cuh:20-65](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/layernorm.cuh#L20-L65)

- [L23-L28](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/layernorm.cuh#L23-L28)：算 `warp_id`/`lane_id`，把每个 warp 映射到一个 `(b,t)` 行（`idx = blockIdx.x*num_warps + warp_id`），越界则返回。
- [L34-L39](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/layernorm.cuh#L34-L39)：**mean 的并行求和**——切片循环 + `warpReduceSum`，这就是替代 CPU 内层 `for` 的关键两行。
- [L45-L54](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/layernorm.cuh#L45-L54)：**方差/rstd 的并行求和**，同样的切片 + 归约；`rsqrtf(sum/C + 1e-5f)` 即 `rstd`。注意 mean/rstd 写成 `float`（不是 `floatX`），呼应 u5-l1 的「统计量恒为 fp32」。
- [L56-L64](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/layernorm.cuh#L56-L64)：第三次切片循环，用 `__ldcs`/`__stcs` 的 streaming hint 写归一化输出。

`warpReduceSum` 的定义：

[llmc/cuda_utils.cuh:147-152](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/cuda_utils.cuh#L147-L152) —— 5 步 `__shfl_xor_sync` 二叉归约，offset 从 16→8→4→2→1。

**`kernel6`：shared memory 优化版**（实际默认走的路径）。它额外把 weight/bias 预取到 shared memory、用 `x128` 向量化读写、并把整行输入缓存到共享内存复用（算 mean 用一次、算 var 再用一次，省一次全局内存读）：

[llmc/layernorm.cuh:67-140](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/layernorm.cuh#L67-L140) —— 同样以 `warpReduceSum` 为核心（[L105](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/layernorm.cuh#L105)、[L116](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/layernorm.cuh#L116)），数学上与 `kernel3` 完全一致。

启动器选择哪个 kernel 取决于能否申请到足够大的 shared memory：

[llmc/layernorm.cuh:433-456](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/layernorm.cuh#L433-L456) —— 用 `cudaFuncSetAttribute` 申请超过 48 KiB 的 dynamic shared memory；成功走 `kernel6`，失败回退到无 smem 的 `kernel3`。这是一种「能力探测 + 优雅降级」。

#### 4.2.4 代码实践（本讲主实践）

**实践目标**：对照 CPU 版 `layernorm` 与 CUDA 版 `kernel3`，把「CPU 内层 `for` 循环」与「GPU warp 归约」一一对上，真正理解 warp 级归约替代了什么。

**操作步骤**：

1. 打开 [train_gpt2.c:78-118](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L78-L118)（CPU `layernorm_forward`）与 [llmc/layernorm.cuh:20-65](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/layernorm.cuh#L20-L65)（`kernel3`）。
2. 填写下表（左侧 CPU 行号 → 右侧 CUDA 对应）：

   | CPU（train_gpt2.c） | 做的事 | CUDA（kernel3）对应 |
   | --- | --- | --- |
   | L87-88 `for b, for t` | 遍历每个位置 | L27 `idx = blockIdx.x*num_warps + warp_id`（每个 warp 一个位置） |
   | L93-95 `for i: m += x[i]` | 单线程求 mean | L35-38 切片 `for i=lane_id; ...; +=32` + `warpReduceSum` |
   | L99-102 `for i: v += (x-m)²` | 单线程求 var | L46-50 同样的切片 + `warpReduceSum` |
   | L105 `s = 1/sqrtf(v+eps)` | rstd | L51 `rsqrtf(sum/C + 1e-5f)` |
   | L108-112 写输出 | 归一化+缩放 | L58-64 切片循环写输出 |

3. **验证 warp 归约的代数等价性**：CPU 里 `m = m/C` 用的是全行总和除以 C；GPU 里每个线程先算 C/32 个元素的部分和，`warpReduceSum` 把 32 个部分和加成全行总和，再 `/C`。两者结果在 fp32 舍入误差内相等。

**需要观察的现象 / 预期结果**：

- CPU 的两层「位置循环 + 通道循环」被 GPU 改写成「跨 warp 并行位置 + warp 内并行通道」。
- `warpReduceSum` 用 5 步 shuffle 完成求和，比 CPU 的串行 `for i` 快得多，且不占用任何共享内存（全靠寄存器间的 shuffle）。
- 若手边有 GPU：可对比 `dev/cuda/layernorm_forward.cu` 里 kernel1（每块 1 行、串行扫 C）与 kernel3/kernel4（warp 归约）的耗时，看归约带来的加速（**待本地验证**）。

#### 4.2.5 小练习与答案

**练习 1**：`warpReduceSum` 的循环为什么是 `for (offset = 16; offset > 0; offset /= 2)`，正好 5 步？

**参考答案**：一个 warp 有 32 个线程，二叉归约需要 \(\lceil\log_2 32\rceil = 5\) 步。第一步 offset=16 让 lane `i` 与 `i^16` 配对（相距 16），求和后每对变成一致；再 offset=8、4、2、1 逐层折叠，最后全 warp 拿到同一个总和。

**练习 2**：`kernel3` 里 mean/rstd 为什么写成 `float` 而不是 `floatX`？为什么只有 `lane_id == 0` 才写缓存？

**参考答案**：(1) mean/rstd 是要被反向复用的统计量，提高精度用 fp32（呼应 u5-l1 的混合精度激活布局）。(2) warp 归约后每个线程都算出了相同的 `m`/`s`，但缓存只需写一份，故只让 lane 0 写，避免 32 个线程写同一地址产生冲突。

**练习 3**：`kernel3` 一个 warp 处理一个 `(b,t)` 行。如果 `num_warps = blockDim.x / WARP_SIZE > 1`，一个 block 会同时处理多行——这样做相比「一个 block 一行」有什么好处？

**参考答案**：让一个 block 容纳多个 warp（多行），可以提升 block 内的并行度与占满 SM 的能力，同时共享对 weight/bias 的读取（在 kernel6 里进一步用 shared memory 把这种共享显式化）。`layernorm_forward` 启动器取 `block_size=256`，即每 block 8 个 warp / 8 行。

---

### 4.3 encoder 反向：scatter-add 的确定性分桶

#### 4.3.1 概念说明

encoder 前向（4.1 已讲）是 gather，每个输出位置读一处，互不冲突。但反向是 **scatter-add**：同一行 `wte[ix,:]` 会被 batch 里所有出现该 token 的位置累加梯度。CPU 版（u2-l1）直接用 `dwte[ix,:] += grad` 串行累加，没问题；GPU 上若多个线程同时对同一个 `dwte[ix,:]` 做 `+=`，就会**数据竞争**。

最朴素的解法是 `atomicAdd`，但浮点 `atomicAdd` 的累加顺序不确定，**结果不可复现**。llm.c 选了一条更工程化的路：**在 CPU 上预先把输入按 token id 分桶（bucket），再让 GPU 上每个桶用一个 block 干净地归约**，从而做到完全确定性（注释里反复强调 "fully deterministic"）。position embedding（wpe）反向则简单得多，直接按 `(t,c)` 切片、循环 batch 求和即可天然确定。

#### 4.3.2 核心流程

**wpe 反向**（简单）：每个 `(t,c)` 由一个线程负责，跨 batch 累加 `dout[b,t,c]`，再 `+=` 到 `dwpe[t,c]`。因为每个输出位置只被一个线程写，无竞争，天然确定。

**wte 反向**（分桶）：

1. **CPU 分桶**：遍历所有 `(bt, c_group)`，按 `(token_id, c_group)` 分桶；记录每个桶里有哪些 `bt`（workload）。
2. **按桶大小降序排序**：让最大的桶先跑，避免大桶拖到结尾时其余 SM 空闲。
3. **GPU 一个桶一个 block**：block 内各 warp 分别处理桶里的若干 `bt`，把它们的 `dout` 局部累加；再在 block 内（经 shared memory）归约到 warp 0；最后 warp 0 用 stochastic rounding 把 fp32 累加结果写回 BF16 的 `dwte[ix,c]`。

#### 4.3.3 源码精读

wpe 反向 kernel（天然确定，按 `(t,c)` 切片、循环 batch）：

[llmc/encoder.cuh:119-152](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/encoder.cuh#L119-L152) —— 注意 [L135](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/encoder.cuh#L135) 的 `for (b=0; b<B; b++)` 串行跨 batch 求和，最后用 `stochastic_rounding` 写回。

wte 反向 kernel（一个桶一个 block，block 内归约）：

[llmc/encoder.cuh:46-117](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/encoder.cuh#L46-L117) —— [L73-L84](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/encoder.cuh#L73-L84) 各 warp 局部累加；[L86-L106](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/encoder.cuh#L86-L106) 经 shared memory 把其它 warp 的结果汇到 warp 0；[L109-L116](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/encoder.cuh#L109-L116) 读-改-写回 `dwte`（带 stochastic rounding）。

CPU 端的分桶与排序逻辑（启动器里）：

[llmc/encoder.cuh:187-232](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/encoder.cuh#L187-L232) —— Step 1 建 `unordered_map` 桶，Step 2 按桶大小降序 `std::sort`，Step 3 异步拷到 device 后启动 wte kernel。注意 [L175-L180](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/encoder.cuh#L175-L180) 先启动 wpe kernel，让它与 CPU 的分桶预处理**并行**（GPU 算 wpe，CPU 同时分桶 wte）。

#### 4.3.4 代码实践

**实践目标**：理解「为什么 encoder 反向要分桶、而 wpe 反向不用」。

**操作步骤**：

1. 读 [llmc/encoder.cuh:119-128](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/encoder.cuh#L119-L128)：wpe 的输出地址是 `dwpe[t,c]`，由 `(t,c)` 唯一确定，跨 batch 的累加在一个线程内串行完成。
2. 对照 wte：同一个 token id `ix` 可能在 batch 里出现多次（不同 `bt`），这些 `bt` 会被分到**同一个桶**，由同一个 block 归约——这就是为什么要分桶。

**需要观察的现象 / 预期结果**：

- wpe：每个 `dwpe[t,c]` 只被一个线程写，无竞争 → 不需要分桶。
- wte：`dwte[ix,c]` 可能被多个线程同时写 → 必须用分桶（或 atomicAdd）。llm.c 选分桶以换取确定性。
- 思考题（自答）：如果不在乎确定性，把 wte 反向改成 `atomicAdd` 会有什么后果？→ 代码简单很多，但每次运行的 `dwte` 因累加顺序不同而有微小差异，训练曲线不可逐位复现。

> 完整的「分桶 vs atomicAdd」性能对比**待本地验证**；源码层面的确定性逻辑可直接从注释确认。

#### 4.3.5 小练习与答案

**练习 1**：为什么分桶后还要「按桶大小降序排序」？

**参考答案**：GPU 上 block 调度不保证均匀。若最大的桶排在最后，它启动时其它桶可能已结束、SM 空闲，造成尾部拖尾。降序排序让大桶尽早开始，与其它桶重叠执行，缩短总时间。

**练习 2**：encoder 反向里反复出现的 `stochastic_rounding` 是干什么用的？

**参考答案**：累加是在 fp32 里做的，但 `dwte`/`dwpe` 是 BF16。直接截断会有系统性偏差；stochastic rounding 用一个确定性种子（`SquirrelNoise5`）做随机舍入，使多次更新的期望无偏，同时因种子确定而保持可复现（详见 4.4.3）。

---

### 4.4 adamw 与 global_norm：逐参数并行与归约式裁剪

#### 4.4.1 概念说明

**AdamW** 的更新规则（u3-l2 已推导）对**每个参数独立**成立——第 `i` 个参数的更新只用到它自己的梯度、动量 m/v、当前值。这意味着它天然适合「一个线程一个参数」的完全并行。CPU 版 `gpt2_update` 是一个长度约 1.24 亿的串行循环；CUDA 版把这个循环劈成 ~24 万个 block（每 block 512 线程），一次 launch 全部并行算完。

CUDA 版相对 CPU 版还多了两件事（u5-l1 埋的伏笔、u6-l1 展开）：

- **master weights**：BF16 训练时，参数本身存 BF16（省显存、快前向），但同时维护一份 fp32 的 `master_params` 用于优化器更新（保精度）。更新时读 fp32 master、算完写回 master，再把 master stochastic-round 成 BF16 供下次前向。
- **grad_scale**：梯度裁剪后的缩放系数，直接乘到梯度上。

**global_norm** 服务于梯度裁剪：先算所有梯度的平方和 \(\|g\|^2 = \sum_i g_i^2\)，再 \(\text{grad\_scale} = \min(1, \text{grad\_clip}/\|g\|)\)（`grad_clip=1.0`）。算平方和是一个典型的**归约**，且数据量巨大（上亿），单 block 算不完，需要 grid-stride 循环 + 两阶段归约。

#### 4.4.2 核心流程

**AdamW 单参数更新**（`adamw_update` device 函数）：

1. 读梯度 `g = grad_scale * grad[idx]`。
2. 一阶动量：`m = lerp(g, m, β1)`（等价 `m = β1·m + (1-β1)·g`）。
3. 二阶动量：`v = lerp(g², v, β2)`。
4. 偏差修正：`m /= (1-β1^t)`，`v /= (1-β2^t)`。
5. 更新：`param = old - lr·(m/(√v+ε) + wd·old)`。
6. master（若有）：`master[idx] = param`；参数：`stochastic_rounding(param, &params[idx], seed)`。

其中 `lerp(start, end, w) = start + w·(end-start)`，用两次 `fma`（融合乘加）实现，比朴素写法少一次运算（注释引自 NVIDIA blog）。

**global_norm 两阶段归约**：

1. **第一阶段**（`global_norm_squared_kernel`）：grid-stride 循环让每个线程扫一段梯度、各自算平方和；block 内用 `blockReduce<warpReduceSum>` 归约到每 block 一个部分和，写入 `out[]`（用 `+=` 让多个 block 的部分和落进同一格子，但这里靠 `reset` 清零 + 后续聚合，避免 atomic）。
2. **第二阶段**（`global_sum_deterministic`，单 block）：把所有 block 的部分和在一个 block 里 `blockReduce` 成最终的总平方和。

`blockReduce` 是 `warpReduceSum` 的「跨 warp 版」：先 warp 内 shuffle 归约，再经 shared memory 让 warp 间交换，最后再 warp 内归约一次。

#### 4.4.3 源码精读

AdamW 的核心更新（device 函数，一个线程一个参数）：

[llmc/adamw.cuh:18-47](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/adamw.cuh#L18-L47) —— 注意 [L30-L34](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/adamw.cuh#L30-L34) 用 `lerp` 更新 m/v，[L38](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/adamw.cuh#L38) 读 master（若有），[L40](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/adamw.cuh#L40) 是与 CPU 版完全一致的更新公式，[L43](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/adamw.cuh#L43)/[L46](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/adamw.cuh#L46) stochastic rounding 写 BF16 + 写 master。

`lerp` 的双 fma 实现：

[llmc/adamw.cuh:14-16](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/adamw.cuh#L14-L16) —— `fma(weight, end, fma(-weight, start, start))`。

启动器：一个 grid 维度并行参数、另一个维度并行「层切片」（`num_slices`，配合 ZeRO 分片）：

[llmc/adamw.cuh:74-88](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/adamw.cuh#L74-L88) —— 偏差修正 `1-β^t` 在 host 算一次传进去（避免每线程重算）；`adamw_kernel3` 的 `dim3(num_blocks, num_slices)` 是「参数维 × 层维」二维 grid。

stochastic rounding（BF16 版）：

[llmc/cuda_utils.cuh:269-278](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/cuda_utils.cuh#L269-L278) —— 用 `SquirrelNoise5` 生成确定性随机阈值，对 fp32 尾数的低 16 位做随机进位，实现无偏舍入。

global_norm 第一阶段（grid-stride + blockReduce，平方和）：

[llmc/global_norm.cuh:14-36](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/global_norm.cuh#L14-L36) —— [L19-L21](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/global_norm.cuh#L19-L21) 是 grid-stride 循环（`for i=index; i<count; i+=grid_width`），[L23](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/global_norm.cuh#L23) block 内归约，[L32-L35](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/global_norm.cuh#L32-L35) 把每 block 部分和写入 `out`。

`blockReduce` 定义（跨 warp 归约，warp 内 shuffle + warp 间 shared memory）：

[llmc/cuda_utils.cuh:165-184](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/cuda_utils.cuh#L165-L184)

在 `train_gpt2.cu` 里如何串起来（梯度裁剪 → adamw）：

[train_gpt2.cu:1850-1852](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1850-L1852) —— `grad_scale = (grad_norm>1) ? 1/grad_norm : 1`，再把 `grad_scale` 传给 `gpt2_update`，后者调 [adamw_update](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1101)。`grad_norm` 来自 [train_gpt2.cu:1027-1031](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1027-L1031) 的 `global_norm_squared` + `sqrtf`。

#### 4.4.4 代码实践

**实践目标**：验证「AdamW 单参数更新的数学公式与 CPU 版逐字一致，差异只在并行执行 + master weights + stochastic rounding」。

**操作步骤**：

1. 打开 [llmc/adamw.cuh:18-47](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/adamw.cuh#L18-L47) 与 u3-l2 讲过的 CPU `gpt2_update`。
2. 逐行比对：m/v 的 EMA 更新、偏差修正 `1-β^t`、`param -= lr·(m̂/(√v̂+ε) + wd·param)`，两边公式应完全相同。
3. 标出 CUDA 版**多出来**的三处：`grad_scale` 乘梯度（[L26](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/adamw.cuh#L26)）、读 fp32 master（[L38](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/adamw.cuh#L38)）、stochastic rounding 写 BF16 + 写 master（[L43](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/adamw.cuh#L43)/[L46](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/adamw.cuh#L46)）。

**需要观察的现象 / 预期结果**：

- 去掉 master weights 与 stochastic rounding 后，CUDA 版与 CPU 版在 fp32 下应数值等价（与 u3-l4 的正确性测试一致）。
- master weights 只在 BF16/FP16 下有意义：`use_master_weights==1` 时分配 fp32 master（[train_gpt2.cu:405-408](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L405-L408)），`init_from_master` 把 master 还原成参数。
- 若 `grad_norm=2.0`，则 `grad_scale=0.5`，所有梯度被整体缩半后再更新——这就是梯度裁剪的效果（**待本地验证**数值）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `global_norm` 不直接在一个 block 里算完整个平方和？

**参考答案**：梯度有上亿个元素，单 block 最多 1024 线程，哪怕用 grid-stride 也会因 block 数太少而占不满 GPU、且串行扫太久。两阶段法先用大量 block 并行各算一段、每 block 内 `blockReduce` 出一个部分和，再用一个 block 把少量部分和（`<1024`，见 [global_norm.cuh:L82](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/global_norm.cuh#L82) 的 assert）合成总平方和，兼顾并行度与确定性。

**练习 2**：`adamw_kernel3` 的 grid 为什么是 `dim3(num_blocks, num_slices)` 二维？

**参考答案**：`num_blocks` 维并行「一个参数张量内部」（每 block 512 参数），`num_slices` 维并行「跨层」——同一类参数（如 12 层的 ln1w）共享一份代码，靠 `blockIdx.y` 偏移到不同层（`params + blockIdx.y * w_stride`）。这样一次 launch 就能更新所有层的所有参数，配合 ZeRO 分片时 `num_slices` 还对应本 GPU 负责的层片（u6-l4）。

**练习 3**：`blockReduce` 相比 `warpReduceSum` 多了哪一步？为什么需要它？

**参考答案**：多了「经 shared memory 跨 warp 交换」这一步。`warpReduceSum` 只能在一个 warp（32 线程）内归约；block 通常有多个 warp（如 512 线程 = 16 warp），要让全 block 得到一个总和，就得先各 warp 内 shuffle 归约、把每 warp 的结果写 shared memory，再让一个 warp 读这些值做第二次归约。

---

## 5. 综合实践

**任务**：把本讲四个模块串起来，画一张「CPU 参考实现 → CUDA kernel」的翻译对照表，并给每一类算子归纳出它的并行化「套路」。

**步骤**：

1. 准备一张三列表格：`算子 | CPU 串行结构 | CUDA 并行结构`。
2. 逐个填入本讲涉及的算子，参考答案如下：

   | 算子 | CPU 串行结构 | CUDA 并行结构 |
   | --- | --- | --- |
   | gelu / residual | 外层 `for` 遍历元素 | 元素级模板：一线程 `x128::size` 个元素 + 128 位 load/store |
   | layernorm 前向 | 外层 `for b,t`；内层 `for i` 求 mean/var | 外层→不同 warp；内层→warp 内切片 + `warpReduceSum` |
   | layernorm 反向 | 同上三重循环 + 跨 (b,t) 累加 dweight/dbias | warp 归约求 dinp + block 内 shared memory 累加 dweight/dbias + 跨 block 用 atomic flag 归约（kernel10） |
   | encoder 前向 | `for b,t,c` gather | 元素级模板（gather 版） |
   | encoder 反向 | `for b,t,c` scatter-add（天然串行） | wpe：按 (t,c) 切片跨 batch 串行；wte：CPU 分桶 + 每 block 一桶归约（确定性） |
   | adamw | 一个长 `for` 遍历所有参数 | 一线程一参数（完全并行），二维 grid 兼顾跨层/分片 |
   | global_norm | 一个长 `for` 求平方和 | grid-stride 切片 + 两阶段 `blockReduce` 归约 |

3. **归纳三种套路**：
   - **元素级套路**（gelu/residual/encoder-fwd）：切片 + 守卫 + 向量化。
   - **行内归约套路**（layernorm）：一个 warp 管一行，warp 归约求统计量。
   - **全局归约套路**（global_norm）：grid-stride + block 归约 + 跨 block 聚合。
4. **延伸阅读**（可选，**待本地验证**）：对照 [dev/cuda/layernorm_forward.cu](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/dev/cuda/layernorm_forward.cu) 的 kernel1→kernel6，看本讲的 `kernel3`/`kernel6` 在那个教学库里是如何「一步步优化」出来的（u7-l1 会专门讲）。

**预期成果**：你能指着表格里任意一行，说出 CPU 的哪层循环被 GPU 的什么机制（切片 / warp 归约 / block 归约 / 分桶）替换掉，并解释为什么 layernorm 是「行内归约」而 global_norm 是「全局归约」。

---

## 6. 本讲小结

- llm.c 的手写 kernel 把 CPU 参考实现里的一层循环，按算子特性翻译成三种并行套路：**元素级切片**、**行内 warp 归约**、**全局 block 归约**。
- **核心**：LayerNorm 用「一个 warp 管一行 (b,t) + `warpReduceSum`」替代 CPU 内层 `for i` 求 mean/var；`warpReduceSum` 用 5 步 `__shfl_xor_sync` 在寄存器间二叉归约，不走内存。
- 元素级算子（gelu/residual/encoder-fwd）共用「`idx` 守卫 + `x128` 128 位向量化 load/store」模板，并用 `cs`/`cg` 等 cache hint 精细控制缓存留弃。
- encoder 反向的 scatter-add 不能用 `atomicAdd`（不可复现），改用 **CPU 分桶 + GPU 每 block 归约一桶** 实现完全确定性；wpe 反向因输出地址天然唯一而简单得多。
- AdamW 是「一个参数一个线程」的完全并行，二维 grid 兼顾跨层与 ZeRO 分片；BF16 训练额外维护 fp32 **master weights** 并用 **stochastic rounding** 无偏写回。
- global_norm 用 **grid-stride 循环 + 两阶段 `blockReduce`** 算出梯度平方和，供 `grad_scale = min(1, 1/‖g‖)` 做梯度裁剪。

---

## 7. 下一步学习建议

- **横向对比多版本优化**：本讲的 `kernel3`/`kernel6` 只是 layernorm 的两个版本。去 [dev/cuda/layernorm_forward.cu](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/dev/cuda/layernorm_forward.cu) 看 kernel1→kernel6 的完整演进（含 cooperative groups、方差巧算），这是 **u7-l1（dev/cuda 内核库）** 的主题。
- **深入 matmul / attention 的 CUDA 化**：本讲刻意没碰 cuBLASLt 和 attention，因为它们体量大。下一步看 **u5-l3（MatMul：cuBLASLt）** 和 **u5-l5（Attention：手写 + cuDNN Flash Attention）**。
- **混合精度全景**：本讲零散提到 master weights、stochastic rounding、TF32，它们属于训练工程的精度话题，系统讲解在 **u6-l1（混合精度、master weights 与 TF32）**。
- **建议精读源码**：先重读 [llmc/cuda_utils.cuh](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/cuda_utils.cuh) 把 `Packed128`/`warpReduceSum`/`blockReduce`/`stochastic_rounding` 四件套吃透——它们是本讲所有 kernel 的公共地基。
