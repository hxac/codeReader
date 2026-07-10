# 向量化访存：float4 / half2 / 128-bit pack

## 1. 本讲目标

上一讲（u2-l2）我们得出了一个略显「绝望」的结论：像 ReLU、elementwise add 这类一维算子，访存模式已经天然是合并访问（coalesced access），算术强度 AI 低到 0.083~0.125，严重 memory-bound，而且**访问模式没有改进空间**了。那么它们还能再快吗？能——本讲给出 CUDA 里最常用、性价比最高的一招：**向量化访存（vectorized memory access）**。

学完本讲你应该能够：

1. 说清楚「向量化访存」优化的到底是什么——它不改变算术强度，而是**减少访存指令的条数**。
2. 掌握 LeetCUDA 里五个向量宏 `FLOAT4` / `HALF2` / `BFLOAT2` / `INT4` / `LDST128BITS` 的含义与用法，理解它们「把指针重解释为 128-bit 类型再读写」的共同本质。
3. 理解向量化后为什么 grid/block 维度要相应调整（block 缩小、每线程多管几个元素），并能从绑定宏里读出真实的 launch 配置。
4. 能把一个 naive kernel（如 LeakyReLU）改写成 float4 向量化版本，并正确处理「N 不是 4 的倍数」的边界。

## 2. 前置知识

本讲承接 u2-l1（线程层次）与 u2-l2（内存层次与合并访问），你需要先具备：

- **线程层次与全局索引**：知道 `idx = blockIdx.x * blockDim.x + threadIdx.x`，以及 host 端用向上取整 `grid=(N+block-1)/block` 保证线程数 ≥ N，device 端用 `if (idx < N)` 兜底（见 u2-l1）。
- **内存层次**：HBM（global memory）→ L2 → Shared Memory → Register，越近越快越小；优化总纲是「减少对最慢 HBM 的访问」（见 u2-l2）。
- **合并访问**：一个 warp 的 32 个线程访问连续对齐地址时，32 次访问合并为 1 次 128 字节的内存事务（见 u2-l2）。
- **算术强度 AI = FLOPs / Bytes**：判断 memory-bound 还是 compute-bound 的第一性指标。

一个关键直觉要先建立起来：**合并访问是「一个 warp 内 32 个线程如何合作搬数据」的问题；向量化访存是「单个线程一次能搬多少数据」的问题。** 二者正交，可以叠加。上一讲解决的是前者（已经做到最优），本讲解决后者。

还有一条硬件事实：GPU 上单条普通 load/store 指令最多搬运 **128 bit（16 字节）**。也就是说，无论你的数据类型是 `float`（32 bit）还是 `half`（16 bit），一条指令的理论上限就是 128 bit。向量化访存的本质，就是想办法让每条指令都「搬满 128 bit」，而不是只搬 32 bit 甚至 16 bit。

## 3. 本讲源码地图

本讲只涉及两个文件，它们结构高度对称，是 LeetCUDA「三件套」里 `.cu` 文件的典型样板：

| 文件 | 作用 |
| --- | --- |
| [kernels/relu/relu.cu:11-16](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/relu/relu.cu#L11-L16) | 顶部统一的向量宏定义（`FLOAT4`/`HALF2`/`LDST128BITS` 等），是全仓库复用的样板。 |
| [kernels/relu/relu.cu:21-106](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/relu/relu.cu#L21-L106) | 6 个 relu kernel：`relu_f32` / `relu_f32x4` / `relu_f16` / `relu_f16x2` / `relu_f16x8` / `relu_f16x8_pack`，对应「标量 → float4 → half2 → half2×8 → 128-bit pack」的递进。 |
| [kernels/relu/relu.cu:118-171](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/relu/relu.cu#L118-L171) | `TORCH_BINDING_RELU` 宏 + 实例化 + `PYBIND11_MODULE`，**这里是 grid/block 真实 launch 配置的权威来源**。 |
| [kernels/elementwise/elementwise.cu:33-50](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/elementwise/elementwise.cu#L33-L50) | `elementwise_add_f32x4_kernel`，展示「向量主体 + 标量尾部」的正确边界处理写法。 |
| [kernels/elementwise/elementwise.cu:107-129](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/elementwise/elementwise.cu#L107-L129) | `elementwise_add_f16x8_pack_kernel`，与 relu 的 pack 版本对照。 |
| [kernels/relu/relu.py:27-66](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/relu/relu.py#L27-L66) | `run_benchmark` 计时脚手架（warmup + iters + 同步），实践环节验证加速比时要用。 |

阅读建议：先看宏定义（11-16 行），再看 naive（21-25 行）与 vec4（29-40 行）的对比，最后看绑定宏（118-155 行）确认真实 launch。

## 4. 核心概念与源码讲解

### 4.1 向量化访存的动机与向量宏总览

#### 4.1.1 概念说明

先回答一个核心问题：**上一讲不是说访问模式已经最优了吗，那向量化到底优化了什么？**

答案是：向量化**不改变搬运的总字节数，也不改变算术强度 AI**，它改变的是**访存指令的条数**。

以 ReLU 为例，处理 N 个 fp32 元素：

- 总搬运字节数：读 N×4 字节 + 写 N×4 字节 = 8N 字节（naive 和 vec4 **完全一样**）。
- 算术强度：每个元素 1 次 `fmaxf`（1 FLOP）/ 8 字节 = 0.125 FLOP/Byte（naive 和 vec4 **完全一样**）。

那为什么 vec4 更快？因为 naive 每个元素要发 **1 条 load + 1 条 store** 指令，N 个元素就是 2N 条访存指令；vec4 用一条 128-bit 指令一次搬 4 个 fp32，N 个元素只需 2×(N/4) = N/2 条访存指令。**指令条数减少 4 倍。**

指令条数减少带来两个好处：

1. **降低指令发射开销**：每条指令都要经过取指、译码、发射、记分牌排队，这些都是「纯开销」。指令少了，开销就少了。
2. **更容易打满带宽**：单条 128-bit 指令比 4 条 32-bit 指令更容易让内存子系统保持忙碌，尤其在 kernel 受指令发射速率（issue rate）限制时效果明显。

一句话总结动机：**合并访问让一个 warp 的 32 个线程高效合作搬数据（已是最优）；向量化让其中每个线程一次搬满 128 bit，把访存指令数压到最低。二者叠加，才是 elementwise 算子的完整优化。**

#### 4.1.2 核心流程

LeetCUDA 在每个 `.cu` 文件顶部都放了一组统一的向量宏，它们的定义完全一致的模式——`reinterpret_cast` 把变量地址重解释为某种「宽类型」指针再取下标 `[0]`：

```text
FLOAT4(value)      = reinterpret_cast<float4 *>(&(value))[0]       // 4×fp32 = 128 bit
HALF2(value)       = reinterpret_cast<half2 *>(&(value))[0]        // 2×fp16 = 32 bit
BFLOAT2(value)     = reinterpret_cast<__nv_bfloat162 *>(&(value))[0] // 2×bf16 = 32 bit
INT4(value)        = reinterpret_cast<int4 *>(&(value))[0]         // 4×int32 = 128 bit
LDST128BITS(value) = reinterpret_cast<float4 *>(&(value))[0]       // 任意类型，固定 128 bit
```

注意一个关键细节：`LDST128BITS` 的定义和 `FLOAT4` **字面完全相同**（都是 `reinterpret_cast<float4 *>`）。那为什么还要单独起个名字？因为语义不同：

- `FLOAT4(x)` 暗示「我把它当 4 个 float」——要求底层是 fp32 数据。
- `LDST128BITS(x)` 暗示「我不关心你是什么类型，只要给我 128 bit」——可以套在 `half pack[8]`（8×16=128 bit）、`int8 arr[16]` 等任意数组上。

这正是一切 pack 优化的核心思路：**把「加载宽度」与「元素类型」解耦**，永远用一条 128-bit 指令搬运，至于里面装的是 4 个 float、8 个 half 还是 16 个 int8，由后续计算决定。

各精度对应的「一次搬几个」关系：

| 类型 | 元素位宽 | 一条 128-bit 指令搬几个 | LeetCUDA 宏 |
| --- | --- | --- | --- |
| fp32 / int32 | 32 bit | 4 个 | `FLOAT4` / `INT4` |
| fp16 / bf16 | 16 bit | 8 个 | `LDST128BITS`（套在 `half[8]` 上） |
| fp16 / bf16 | 16 bit | 2 个（32-bit 半字） | `HALF2` / `BFLOAT2` |

#### 4.1.3 源码精读

向量宏统一定义在文件顶部，relu 与 elementwise 两个文件**逐字一致**：

这段定义了本讲所有向量化的基础工具——`FLOAT4`、`HALF2`、`BFLOAT2`、`LDST128BITS`，统一用 `reinterpret_cast` 把变量地址重解释为宽类型指针：[kernels/relu/relu.cu:11-16](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/relu/relu.cu#L11-L16)

```c
#define WARP_SIZE 32
#define INT4(value) (reinterpret_cast<int4 *>(&(value))[0])
#define FLOAT4(value) (reinterpret_cast<float4 *>(&(value))[0])
#define HALF2(value) (reinterpret_cast<half2 *>(&(value))[0])
#define BFLOAT2(value) (reinterpret_cast<__nv_bfloat162 *>(&(value))[0])
#define LDST128BITS(value) (reinterpret_cast<float4 *>(&(value))[0])
```

可以看到 `&(value)` 取地址、`reinterpret_cast` 换类型、`[0]` 取首元素——这套写法既能用在全局内存指针上（如 `FLOAT4(x[idx])`），也能用在局部数组上（如 `LDST128BITS(pack_x[0])`），是 LeetCUDA 全仓库统一风格。

#### 4.1.4 代码实践

1. **实践目标**：建立「一条指令搬多少字节」的直觉。
2. **操作步骤**：打开 [kernels/relu/relu.cu:11-16](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/relu/relu.cu#L11-L16)，对每个宏算出它一次搬运的字节数。
3. **需要观察的现象**：`FLOAT4` = 4×4 = 16 字节；`HALF2` = 2×2 = 4 字节；`LDST128BITS` = 16 字节。
4. **预期结果**：`FLOAT4` 与 `LDST128BITS` 都是 16 字节（128 bit），即单条指令的理论上限；`HALF2` 只有 4 字节，是「半精度 2 路 SIMD」的最小搬运单元。
5. 待本地验证：无（纯定义阅读）。

#### 4.1.5 小练习与答案

**练习 1**：既然 `LDST128BITS` 和 `FLOAT4` 定义完全相同，为什么不直接用 `FLOAT4` 处理 `half` 数据？

**答案**：`FLOAT4` 语义上把内存当作 4 个 `float`（fp32），若套在 `half` 数组上，会把 8 个 half 的位模式重新解释成 4 个 fp32，破坏数据含义，后续还要再重解释回来，容易出错且可读性差。`LDST128BITS` 明确表达「只搬 128 bit、不解释类型」，意图清晰，是处理任意类型 pack 的正确抽象。

**练习 2**：一个 fp32 的 naive elementwise kernel 改成 float4 后，算术强度 AI 变了吗？为什么？

**答案**：没变。AI = FLOPs / Bytes，向量化既不改变总 FLOPs（每个元素该算的还是算一次），也不改变总搬运字节数（读写的元素总数不变），只改变「这些字节被分成几条指令搬」。所以 kernel 依然 memory-bound，只是搬得「更省指令」。

---

### 4.2 relu_f32x4_kernel：float4 向量化与索引/网格调整

#### 4.2.1 概念说明

`relu_f32x4_kernel` 是本讲的核心最小模块之一。它用 `float4`（4 个 fp32 = 128 bit）把「4 次 load + 4 次 store」压成「1 次 load + 1 次 store」。需要重点理解三件事：

1. **索引要对齐到 4 的倍数**：每个线程负责连续 4 个元素，所以全局下标要先算出「线程号线性索引」再乘 4，保证起点落在 4 元素边界上。
2. **grid/block 要相应调整**：每个线程干 4 倍的活，线程总数可以减少 4 倍。
3. **计算仍是标量**：`fmaxf` 是标量函数，没有「float4 版 fmaxf」，所以 4 个元素的计算还是要写 4 次 `fmaxf`。向量化只省了访存，没省计算——对 memory-bound 的 ReLU 来说，省的恰恰是瓶颈。

#### 4.2.2 核心流程

naive 与 vec4 的对照（每线程负责的元素数、索引公式、指令数）：

```text
naive  (1 元素/线程):  idx = blockIdx.x*blockDim.x + threadIdx.x
                      load  x[idx]        (1 条 32-bit)
                      y[idx] = fmaxf(0,x) (1 次 fmaxf)
                      store y[idx]        (1 条 32-bit)
                      → 每 4 元素：4 load + 4 fmaxf + 4 store = 12 条

vec4   (4 元素/线程):  idx = (blockIdx.x*blockDim.x + threadIdx.x) * 4
                      float4 rx = FLOAT4(x[idx])  (1 条 128-bit load，搬 4 个)
                      4 次 fmaxf（标量，逐 lane）
                      FLOAT4(y[idx]) = ry        (1 条 128-bit store，写 4 个)
                      → 每 4 元素：1 load + 4 fmaxf + 1 store = 6 条
```

访存指令从 8 条降到 2 条（4 倍），计算指令不变。launch 配置上，绑定宏保持「每 block 处理 256 个元素」不变，于是 block 线程数从 256 降到 64，grid 数量不变。下面用数学刻画这个关系。

设 block 处理的元素数为 \(E = 256\)（保持恒定），每线程处理 \(p\) 个元素（naive \(p=1\)，vec4 \(p=4\)），则：

- block 内线程数：\(T = E / p\)
- block 数（grid）：\(G = \lceil N / E \rceil = \lceil N / 256 \rceil\)
- 总线程数：\(G \cdot T = \lceil N/256 \rceil \cdot (256/p) \approx N/p\)

所以从 naive 到 vec4，\(p\) 从 1 变 4，**总线程数下降 4 倍，grid 数量不变**。这是「向量化调整 grid/block」的通用公式。

#### 4.2.3 源码精读

先看 naive 基线，作为对照——每线程 1 个元素，索引就是线性下标：[kernels/relu/relu.cu:21-25](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/relu/relu.cu#L21-L25)

```c
__global__ void relu_f32_kernel(float *x, float *y, int N) {
  int idx = blockIdx.x * blockDim.x + threadIdx.x;
  if (idx < N)
    y[idx] = fmaxf(0.0f, x[idx]);
}
```

再看 vec4 版本——索引乘 4 对齐，用 `FLOAT4` 一次性 load/store 4 个元素，计算仍逐 lane 标量 `fmaxf`：[kernels/relu/relu.cu:29-40](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/relu/relu.cu#L29-L40)

```c
__global__ void relu_f32x4_kernel(float *x, float *y, int N) {
  int idx = (blockIdx.x * blockDim.x + threadIdx.x) * 4;
  if (idx < N) {
    float4 reg_x = FLOAT4(x[idx]);
    float4 reg_y;
    reg_y.x = fmaxf(0.0f, reg_x.x);
    reg_y.y = fmaxf(0.0f, reg_x.y);
    reg_y.z = fmaxf(0.0f, reg_x.z);
    reg_y.w = fmaxf(0.0f, reg_x.w);
    FLOAT4(y[idx]) = reg_y;
  }
}
```

注意两点：①`idx` 末尾 `* 4` 让每个线程的起点落在 4 元素边界；②`if (idx < N)` 只判断了起点，没有判断 `idx+3 < N`——当 N 不是 4 的倍数时，最后一个线程的 `FLOAT4(x[idx])` 会**越界多读最多 3 个元素**。对 ReLU 这种「只读不写越界处」的情况，靠 128 字节对齐分配通常不会崩，但严格说有隐患（4.3 节给出正确写法）。

最后看真实 launch 配置——它不在 kernel 上方注释里，而在绑定宏 `TORCH_BINDING_RELU` 里，这是权威来源：[kernels/relu/relu.cu:118-155](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/relu/relu.cu#L118-L155)

其中关键的 grid/block 计算（以非 2D 分支为例）：[kernels/relu/relu.cu:128-129](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/relu/relu.cu#L128-L129)

```c
dim3 block(256 / (n_elements));
dim3 grid((N + 256 - 1) / 256);
```

宏参数 `n_elements` 由实例化决定：`TORCH_BINDING_RELU(f32, ..., 1)` 传 1，`TORCH_BINDING_RELU(f32x4, ..., 4)` 传 4（见 [kernels/relu/relu.cu:157-158](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/relu/relu.cu#L157-L158)）。所以 vec4 的真实 launch 是 `block=64, grid=ceil(N/256)`，与 4.2.2 的公式完全吻合：grid 不变、block 缩 4 倍。

> **关于注释的小提醒**：`relu_f32x4_kernel` 上方注释写着 `grid(N/256/4), block(256/4)`（[kernels/relu/relu.cu:27-28](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/relu/relu.cu#L27-L28)），这里的 `N/256/4` 与绑定宏的实际 `grid=(N+255)/256` 不一致（若 grid 真取 N/1024，则只能覆盖 N/4 的元素，明显不足）。这是源码注释的一处笔误，**以绑定宏的 launch 为准**。对照 elementwise 的同款注释 `grid(N/256), block(256/4)`（[kernels/elementwise/elementwise.cu:30-32](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/elementwise/elementwise.cu#L30-L32)）才是与宏一致的写法。这给我们一个阅读教训：grid/block 的真相永远看 `<<<grid, block>>>` 那一行，而不是上方注释。

#### 4.2.4 代码实践

1. **实践目标**：量化 naive 与 vec4 的 grid/线程数/指令数差异。
2. **操作步骤**：取 N=1024×1024=1048576，分别对 `relu_f32`（n_elements=1）与 `relu_f32x4`（n_elements=4），按绑定宏公式计算 `block`、`grid`、总线程数、每元素平均访存指令数。
3. **需要观察的现象**：
   - 两者 `grid` 都是 `ceil(1048576/256) = 4096`（相同）。
   - `relu_f32`：block=256，总线程=4096×256=1048576；每元素 2 条访存指令。
   - `relu_f32x4`：block=64，总线程=4096×64=262144（少 4 倍）；每元素 0.5 条访存指令（1 load + 1 store 摊到 4 元素）。
4. **预期结果**：vec4 总线程数为 naive 的 1/4，访存指令数为 1/4，grid 不变。
5. 待本地验证：可在 GPU 上跑 `python3 relu.py`（见 [kernels/relu/relu.py](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/relu/relu.py)），README 给出的 S=K=1024 结果是 `f32: 0.00528ms` vs `f32x4: 0.00371ms`，vec4 快约 30%~40%（[kernels/relu/README.md:29-31](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/relu/README.md#L29-L31)）。由于该 kernel 极度 memory-bound，带宽上限相同，所以加速比远小于「指令数 4 倍」，这正印证了「向量化省的是指令开销而非带宽」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `relu_f32x4_kernel` 里 `fmaxf` 还要写 4 次，而不能像 load 那样「一次算 4 个」？

**答案**：CUDA 内置的 `fmaxf` 是标量函数，没有 `float4` 版本（不像 `half2` 有 `__hmax2` 这种 SIMD 内建函数）。所以 fp32 的 4 个元素必须逐 lane 调用 4 次 `fmaxf`。向量化只压缩了访存，计算仍是标量。要「一次算多个」需要换到 half 精度用 `__hmax2`（见 4.4 节）。

**练习 2**：把 `relu_f32x4_kernel` 的 `idx` 公式里的 `* 4` 去掉、block 改回 256，会发生什么？

**答案**：线程数会变回 naive 的规模（4 倍），但每个线程仍 `FLOAT4(x[idx])` 读 4 个元素并 `FLOAT4(y[idx])` 写 4 个——结果是**每 4 个相邻线程读写同一组 4 个元素**，大量重复计算与重复写回，不仅没用还可能产生写竞争。`* 4` 与「block 缩 4 倍」必须配套，二者是同一件事的两面。

---

### 4.3 elementwise_add_f32x4_kernel：向量主体 + 标量尾部

#### 4.3.1 概念说明

4.2 节留下一个问题：当 N 不是 4 的倍数时，`relu_f32x4_kernel` 的 `if (idx < N)` 会让最后一个线程越界多读最多 3 个元素。对 ReLU 这种「读了也只用、不写越界处」的场景通常不崩，但这是个隐患。

`elementwise_add_f32x4_kernel` 给出了**正确且健壮的写法**：把 kernel 分成两段——

- **向量主体**：当 `idx + 3 < N` 时，用 `float4` 一次处理 4 个元素。
- **标量尾部（tail）**：当不满足上面条件但 `idx < N` 时，退化为逐元素循环处理剩余的 1~3 个元素。

这种「向量主体 + 标量尾部」是 SIMD/SIMT 编程的通用范式，从 CPU 的 AVX 到 GPU 的 pack 优化都用它。它保证了：①永远不越界读；②每个元素都被恰好处理一次；③性能几乎无损（尾部只有最后一个 block 的少量线程走标量路径）。

#### 4.3.2 核心流程

```text
idx = 4 * (blockIdx.x*blockDim.x + threadIdx.x)

if (idx + 3 < N):          # 向量主体：完整 4 元素，安全
    reg_a = FLOAT4(a[idx])  # 1 条 128-bit load
    reg_b = FLOAT4(b[idx])  # 1 条 128-bit load
    reg_c = reg_a + reg_b   # 4 次标量加（可写成 .x/.y/.z/.w）
    FLOAT4(c[idx]) = reg_c  # 1 条 128-bit store
else if (idx < N):          # 标量尾部：剩余 1~3 个元素
    for i in 0..:            # 逐元素，直到 idx+i >= N
        if idx+i < N: c[idx+i] = a[idx+i] + b[idx+i]
```

边界判断用 `idx + 3 < N` 而不是 `idx < N`，这是关键差异——前者保证 `[idx, idx+3]` 全部合法，后者只保证起点合法。

#### 4.3.3 源码精读

`elementwise_add_f32x4_kernel` 完整实现，注意 `if ((idx + 3) < N)` 与 `else if (idx < N)` 的两段式结构：[kernels/elementwise/elementwise.cu:33-50](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/elementwise/elementwise.cu#L33-L50)

```c
__global__ void elementwise_add_f32x4_kernel(float *a, float *b, float *c,
                                             int N) {
  int idx = 4 * (blockIdx.x * blockDim.x + threadIdx.x);
  if ((idx + 3) < N) {
    float4 reg_a = FLOAT4(a[idx]);
    float4 reg_b = FLOAT4(b[idx]);
    float4 reg_c;
    reg_c.x = reg_a.x + reg_b.x;
    reg_c.y = reg_a.y + reg_b.y;
    reg_c.z = reg_a.z + reg_b.z;
    reg_c.w = reg_a.w + reg_b.w;
    FLOAT4(c[idx]) = reg_c;
  } else if (idx < N) {
    for (int i = 0; (idx + i) < N; i++) {
      c[idx + i] = a[idx + i] + b[idx + i];
    }
  }
}
```

对照 naive 基线：[kernels/elementwise/elementwise.cu:23-28](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/elementwise/elementwise.cu#L23-L28)

```c
__global__ void elementwise_add_f32_kernel(float *a, float *b, float *c,
                                           int N) {
  int idx = blockIdx.x * blockDim.x + threadIdx.x;
  if (idx < N)
    c[idx] = a[idx] + b[idx];
}
```

它的 launch 配置同样由绑定宏 `TORCH_BINDING_ELEM_ADD` 决定，公式与 relu 完全一致（`block=256/n_elements, grid=ceil(N/256)`）：[kernels/elementwise/elementwise.cu:141-183](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/elementwise/elementwise.cu#L141-L183)，实例化 `TORCH_BINDING_ELEM_ADD(f32x4, ..., 4)` 在 [kernels/elementwise/elementwise.cu:186](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/elementwise/elementwise.cu#L186)。

elementwise 比 relu 多读一路输入（a、b 两个），所以每个元素搬 12 字节（4+4+4），AI = 1/12 ≈ 0.083，比 relu 的 0.125 还低，更 memory-bound，向量化收益更明显。

#### 4.3.4 代码实践

1. **实践目标**：理解「向量主体 + 标量尾部」如何安全处理非 4 对齐的 N。
2. **操作步骤**：取 N=10（不是 4 的倍数）。按 `block=64, grid=ceil(10/256)=1`，共 64 个线程，`idx = 4*threadIdx.x` 取值 0,4,8,12,…。手动跟踪前几个线程走哪个分支。
3. **需要观察的现象**：
   - `tid=0`→idx=0：`0+3=3 < 10`，走向量主体，处理 [0,3]。
   - `tid=1`→idx=4：`4+3=7 < 10`，走向量主体，处理 [4,7]。
   - `tid=2`→idx=8：`8+3=11 ≥ 10`，但 `8 < 10`，走标量尾部，循环处理 idx=8、9（i=0 时 8<10 处理，i=1 时 9<10 处理，i=2 时 10≥10 停止）。
   - `tid=3`→idx=12：`12 ≥ 10`，两个分支都不进，该线程空转。
4. **预期结果**：元素 0~7 走向量路径，8~9 走标量路径，10 之后不处理，无越界访问。
5. 待本地验证：可把 N 改成奇数（如 1001）跑 `elementwise.py`，确认 Max Err 仍为 0，证明尾部逻辑正确。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `if ((idx + 3) < N)` 改回 `if (idx < N)`（像 relu 那样），当 N=10、idx=8 时会发生什么？

**答案**：`8 < 10` 成立，会走向量主体，执行 `FLOAT4(a[8])` 读取 a[8],a[9],a[10],a[11]——后两个越界。对 ReLU 这种「只读不写越界处」可能不崩，但 elementwise add 同样会写 `FLOAT4(c[8])` 即 c[8..11]，**越界写**会破坏 c 数组之后的内存，属于真实 bug。所以 elementwise 用 `idx+3 < N` 是必要的。

**练习 2**：标量尾部的 `for` 循环里为什么还要 `if (idx + i < N)` 而不是固定循环 4 次？

**答案**：因为尾部剩余元素个数不确定（1~3 个），必须用 `(idx + i) < N` 动态判断何时停。固定循环 4 次会再次越界。这是「向量主体 + 标量尾部」的标准安全写法。

---

### 4.4 进阶预览：half2 与 LDST128BITS（FP16 向量化与 128-bit pack）

#### 4.4.1 概念说明

fp32 的向量化止步于 `float4`（128 bit）。但降到 fp16 后，128 bit 能装 **8 个 half**，向量化空间更大，而且 fp16 还有一类 fp32 没有的「真 SIMD」指令——`half2` 的 `__hmax2` / `__hadd2` 可以**一条指令算 2 个 half**。于是 fp16 的向量化能同时省「访存指令」和「计算指令」，是性价比最高的精度。

LeetCUDA 在 relu 里展示了 fp16 向量化的三级递进，正好对应三种思路：

1. **`relu_f16x2_kernel`**：用 `HALF2` 一次搬 2 个 half（32 bit），但计算仍逐 lane 标量 `__hmax`——只省了点访存。
2. **`relu_f16x8_kernel`**：用 4 次 `HALF2` 搬 8 个 half（共 128 bit），计算逐 lane 标量——搬得多了但计算没并行。
3. **`relu_f16x8_pack_kernel`**：用 1 次 `LDST128BITS` 一次搬 8 个 half（128 bit），计算用 `__hmax2` 一次算 2 个——**访存与计算同时最大化**，这是最终形态，也是后续 HGEMM/FlashAttention 里反复使用的 pack 范式。

`__hmax2` / `__hadd2` 这类 `half2` 内建函数实现了 **SIMD-within-a-register**：一个 32-bit 寄存器里装 2 个 half，一条指令同时对两个 lane 运算。这是 fp16 相对 fp32 的结构性优势。

#### 4.4.2 核心流程

`relu_f16x8_pack_kernel` 的数据流（每线程 8 个 half）：

```text
idx = 8 * (blockIdx.x*blockDim.x + threadIdx.x)
half pack_x[8], pack_y[8]                       # 局部数组，8×16=128 bit
LDST128BITS(pack_x[0]) = LDST128BITS(x[idx])    # 1 条 128-bit load，搬 8 个 half
for i in 0,2,4,6:                                # 4 次循环
    HALF2(pack_y[i]) = __hmax2(HALF2(pack_x[i]), z2)  # __hmax2 一次算 2 个
LDST128BITS(y[idx]) = LDST128BITS(pack_y[0])    # 1 条 128-bit store，写 8 个 half
```

每 8 个 half：1 load + 4 `__hmax2` + 1 store = 6 条指令。naive fp16 是 8 load + 8 `__hmax` + 8 store = 24 条。指令数降到 1/4，且其中计算也并行了。

注意 `pack_x[8]` 是**局部数组**（注释说是 `.local` space，即放在寄存器或栈），它只是一个「容器」，让我们能用 `LDST128BITS` 一次性搬运。这跟 4.2 节直接 `FLOAT4(x[idx])` 读到 `float4 reg_x` 里是两种等价风格——`float4` 是现成类型，`half[8]` 需要靠 `LDST128BITS` 重解释。

#### 4.4.3 源码精读

`relu_f16x8_pack_kernel`——用 `LDST128BITS` 把 8 个 half 一次搬入局部数组，再用 `__hmax2` 做 half2 SIMD 计算，最后一次搬出：[kernels/relu/relu.cu:89-106](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/relu/relu.cu#L89-L106)

```c
__global__ void relu_f16x8_pack_kernel(half *x, half *y, int N) {
  int idx = 8 * (blockIdx.x * blockDim.x + threadIdx.x);
  const half2 z2 = {__float2half(0.0f), __float2half(0.0f)};
  half pack_x[8], pack_y[8]; // 8x16 bits=128 bits.
  LDST128BITS(pack_x[0]) = LDST128BITS(x[idx]); // load 128 bits

#pragma unroll
  for (int i = 0; i < 8; i += 2) {
    HALF2(pack_y[i]) = __hmax2(HALF2(pack_x[i]), z2); // __hmax2 for half2 x 4
  }
  if ((idx + 7) < N) {
    LDST128BITS(y[idx]) = LDST128BITS(pack_y[0]); // store 128 bits
  }
}
```

对比上一级 `relu_f16x8_kernel`——同样处理 8 个 half，但用 4 次独立 `HALF2` 加载、计算逐 lane 标量 `__hmax`，指令数更多：[kernels/relu/relu.cu:60-87](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/relu/relu.cu#L60-L87)

而更基础的 `relu_f16x2_kernel`——每次只搬 2 个 half：[kernels/relu/relu.cu:49-58](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/relu/relu.cu#L49-L58)

elementwise 侧有完全对称的 pack 版本，用 `__hadd2`：[kernels/elementwise/elementwise.cu:107-129](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/elementwise/elementwise.cu#L107-L129)

pack 版的边界用 `if ((idx + 7) < N)`，与 4.3 节的 `idx+3 < N` 同理，保证 8 个 half 全部合法。注意它**只在 store 处判断**，load 处没判断——因为多读不写通常无害（与 relu_f32x4 同样的「宽松」风格），但严格说仍有改进空间（可仿照 elementwise_f32x4 加标量尾部）。

#### 4.4.4 代码实践

1. **实践目标**：量化 fp16 三级向量化的指令数差异，理解 pack 版为何最快。
2. **操作步骤**：对 `relu_f16` / `relu_f16x2` / `relu_f16x8` / `relu_f16x8_pack`，按每线程元素数和宏用法，列出「每 8 元素的 load 次数、计算指令数、store 次数」。
3. **需要观察的现象**（每 8 个 half）：
   - f16（1 元素/线程）：8 load + 8 `__hmax` + 8 store = 24 条。
   - f16x2（2 元素/线程）：4 `HALF2` load + 8 `__hmax` + 4 store = 16 条。
   - f16x8（8 元素/线程）：4 `HALF2` load + 8 `__hmax` + 4 store = 16 条（load 次数同 x2，但线程数少 4 倍）。
   - f16x8_pack（8 元素/线程）：1 `LDST128BITS` load + 4 `__hmax2` + 1 store = 6 条。
4. **预期结果**：pack 版指令数最少（6 条），且计算用 `__hmax2` 真并行，是四级里最快的。
5. 待本地验证：跑 `python3 relu.py`，README 在 S=K=4096 给出 `f16: 0.0407ms`、`f16x8: 0.0367ms`、`f16x8pack: 0.0147ms`，pack 版约为 naive 的 1/3（[kernels/relu/README.md:129-133](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/relu/README.md#L129-L133)）。

#### 4.4.5 小练习与答案

**练习 1**：`relu_f16x8_kernel` 和 `relu_f16x8_pack_kernel` 都处理 8 个 half，为什么 pack 版更快？

**答案**：两点差异。①访存：`f16x8` 用 4 次 `HALF2`（4 条 32-bit 指令）搬 8 个 half，`f16x8_pack` 用 1 次 `LDST128BITS`（1 条 128-bit 指令）搬同样 8 个 half，load/store 指令数从 8 条降到 2 条。②计算：`f16x8` 逐 lane 用标量 `__hmax`（8 次），`f16x8_pack` 用 `__hmax2` 一次算 2 个（4 次），计算指令也减半。两者叠加，pack 版明显更快。

**练习 2**：为什么 fp32 没有像 `__hmax2` 那样「一条指令算 4 个 float」的内建函数？

**答案**：GPU 的 SIMD 计算指令主要面向低精度（fp16/bf16/int8）以提升吞吐，一条 32-bit 寄存器能装 2 个 fp16 才有 `half2` 这类 2 路 SIMD。fp32 一个元素就占满 32-bit 寄存器，没有「塞多个」的空间，所以 fp32 的向量化只能省访存（`float4`），不能省计算。要省计算只能走 Tensor Core（如 WMMA/MMA），那是后续 HGEMM 章节的内容。

---

## 5. 综合实践

把本讲三件事——**向量宏、索引对齐、向量主体 + 标量尾部**——串起来，完成下面这个贯穿任务。

**任务**：在 u2-l1 里你应该写过（或构思过）一个 naive 的 LeakyReLU kernel。现在把它改写成 float4 向量化版本，并补上正确的标量尾部。

LeakyReLU 定义（slope 取 0.01）：

\[ y = \begin{cases} x & \text{若 } x > 0 \\ \text{slope} \cdot x & \text{若 } x \le 0 \end{cases} \]

**步骤 1：naive 基线**（示例代码，若 u2-l1 已写可复用）：

```c
// 示例代码：naive LeakyReLU，每线程 1 元素
__global__ void leaky_relu_f32_kernel(float *x, float *y, int N, float slope) {
  int idx = blockIdx.x * blockDim.x + threadIdx.x;
  if (idx < N)
    y[idx] = x[idx] > 0.0f ? x[idx] : slope * x[idx];
}
```

**步骤 2：float4 向量化 + 标量尾部**（示例代码，仿照 `elementwise_add_f32x4_kernel` 的两段式结构）：

```c
// 示例代码：float4 向量化 LeakyReLU，向量主体 + 标量尾部
__global__ void leaky_relu_f32x4_kernel(float *x, float *y, int N, float slope) {
  int idx = 4 * (blockIdx.x * blockDim.x + threadIdx.x);
  if ((idx + 3) < N) {
    float4 reg_x = FLOAT4(x[idx]);
    float4 reg_y;
    reg_y.x = reg_x.x > 0.0f ? reg_x.x : slope * reg_x.x;
    reg_y.y = reg_x.y > 0.0f ? reg_x.y : slope * reg_x.y;
    reg_y.z = reg_x.z > 0.0f ? reg_x.z : slope * reg_x.z;
    reg_y.w = reg_x.w > 0.0f ? reg_x.w : slope * reg_x.w;
    FLOAT4(y[idx]) = reg_y;
  } else if (idx < N) {
    for (int i = 0; (idx + i) < N; i++) {
      float v = x[idx + i];
      y[idx + i] = v > 0.0f ? v : slope * v;
    }
  }
}
```

**步骤 3：自查清单**（逐条对照本讲要点，确认你写的代码满足）：

1. 索引公式是否有 `* 4`？（对应 4.2 的索引对齐）
2. 是否用了 `if ((idx + 3) < N)` 而非 `if (idx < N)`？（对应 4.3 的边界安全）
3. 标量尾部循环是否有 `(idx + i) < N` 动态终止？（对应 4.3 的标量尾部）
4. launch 配置是否 `block=64, grid=ceil(N/256)`？（对应 4.2 的 grid/block 调整）
5. 向量主体与标量尾部写的是否都是同一个输出指针 `y`、用的是同一个 `slope`？

**步骤 4：验证**（可选，待本地验证）：

仿照 [kernels/relu/relu.py:27-66](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/relu/relu.py#L27-L66) 的 `run_benchmark`，构造 `x = torch.randn(N).cuda()`，与 `torch.nn.functional.leaky_relu(x, 0.01)` 比对 Max Err（应为 0），并对比 naive 与 vec4 的耗时，观察 vec4 是否更快、加速比是否远小于 4 倍（因为 LeakyReLU 同样 memory-bound）。

**预期结果**：Max Err = 0；vec4 比 naive 快 30%~50%；加速比不到 4 倍（带宽瓶颈所致）。若 Max Err 不为 0，重点检查步骤 2 的标量尾部与 `idx` 对齐。

## 6. 本讲小结

- **向量化优化的本质是减少访存指令条数**，不改变总字节数、不改变算术强度 AI，kernel 依旧 memory-bound——它省的是指令发射开销，不是带宽。
- 五个向量宏 `FLOAT4`/`HALF2`/`BFLOAT2`/`INT4`/`LDST128BITS` 的共同本质是 `reinterpret_cast` 把地址重解释为宽类型，其中 `LDST128BITS` 与 `FLOAT4` 定义相同但语义是「类型无关、固定 128 bit」，是处理任意精度 pack 的通用工具。
- 向量化后**索引要乘以元素数对齐**（`idx = (...)*4`），**grid/block 要相应调整**：绑定宏保持「每 block 256 元素」恒定，故 `block = 256/n_elements`（缩小）、`grid = ceil(N/256)`（不变），总线程数降为 1/n_elements。
- **grid/block 的真相看 `<<<grid, block>>>`，不是上方注释**——`relu_f32x4` 上方注释 `grid(N/256/4)` 是笔误，以绑定宏为准。
- 正确的边界处理是「**向量主体（`idx+3 < N`）+ 标量尾部（逐元素循环）**」两段式，elementwise 给出了范本；relu 的 `if (idx < N)` 是宽松写法，依赖不越界写，不够健壮。
- fp16 向量化空间更大：128 bit 能装 8 个 half，且 `__hmax2`/`__hadd2` 提供「一条算两个」的真 SIMD；`relu_f16x8_pack` 用 `LDST128BITS` + `__hmax2` 同时最大化访存与计算，是后续 HGEMM/FlashAttention 的 pack 范式预览。

## 7. 下一步学习建议

本讲把「单线程一次搬多少数据」压到了 128 bit 上限，elementwise 算子的访存优化基本到顶。接下来该转向**需要线程间合作**的算子：

- **u4-l1（Warp/Block Reduce）**：当归约类算子（dot、softmax、norm）需要把多个线程的中间结果求和时，向量化只解决了「搬」，还要解决「合」。warp reduce 用 `__shfl_xor` 在寄存器间同步树求和，是下一块基石。
- **u4-l2（Dot Product）**：把本讲的 `dot_vec4`（向量化加载 + warp reduce）作为练手，串联「向量化访存」与「归约」两条线。
- 如果想立刻看到向量化在「计算密集」算子里的更大价值，可跳读 **u9（SGEMM）** 和 **u10（HGEMM）**，那里 `float4`/`half2`/`LDST128BITS` 配合 shared memory 分块，是 pack 优化的主战场。

建议读者先把本讲的 LeakyReLU vec4 实践跑通，确认理解「索引对齐 + 标量尾部 + launch 配置」三件套，再进入归约章节。
