# 线程层次：grid / block / warp

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚 GPU 的四级执行层次：**grid → block → warp → thread**，以及它们之间的数量关系。
- 理解 **SIMT**（单指令多线程）执行模型：为什么 32 个线程必须一起做同一件事。
- 掌握用 `blockIdx / blockDim / threadIdx` 三个内置变量计算「全局线程索引」的公式。
- 能根据数据规模 N 设计合理的 grid / block 配置，并理解为什么需要 `if (idx < N)` 边界保护。
- 读懂 LeetCUDA 中 `relu_f32_kernel` 与 `elementwise_add_f32_kernel` 这两个最朴素的 kernel，并仿写一个新的 elementwise kernel。

本讲是整个 CUDA 学习的「地基」。后面所有优化（向量化、共享内存、Tensor Core）都是在这一层线程骨架之上做的，所以请务必把线程索引的映射关系算清楚。

## 2. 前置知识

在开始前，你只需要具备：

- 会用 PyTorch（知道 `torch.randn`、`.cuda()` 是什么）。
- 知道一个数组在内存里是「连续排列」的，可以用下标 `x[i]` 访问。
- 理解 ReLU 是什么：\( y = \max(0, x) \)，即「负数变 0，正数不变」。

如果你已经读过本手册的 **u1-l3（目录结构与 kernel 模块约定）**，知道「一个 kernel 到 Python 入口要经过四层接力」，那就更好——本讲只聚焦其中第一层：`__global__` kernel 内部。

几个本讲会用到的术语，先用大白话解释一遍：

| 术语 | 大白话 |
|------|--------|
| **kernel（核函数）** | 一段「只负责处理一个/几个数据元素」的函数，GPU 会把它复制成成千上万份并行执行 |
| **host（主机）** | CPU 这边，负责「编排」：决定启动多少线程、传哪些参数 |
| **device（设备）** | GPU 这边，负责真正执行 kernel |
| **launch（启动）** | host 通过 `<<<grid, block>>>` 语法告诉 GPU「按这个线程规模去跑 kernel」 |

## 3. 本讲源码地图

本讲只涉及两个文件，都是「一维 elementwise」算子，结构几乎一模一样：

| 文件 | 作用 | 本讲关注点 |
|------|------|-----------|
| [kernels/relu/relu.cu](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/relu/relu.cu) | ReLU 的多个版本 kernel + host 启动函数 + PyTorch 绑定 | `relu_f32_kernel`（naive 版）和 host 里 grid/block 的计算 |
| [kernels/elementwise/elementwise.cu](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/elementwise/elementwise.cu) | 逐元素加法的多个版本 kernel + 绑定 | `elementwise_add_f32_kernel`（naive 版） |

辅助参考（不展开）：

| 文件 | 作用 |
|------|------|
| [kernels/relu/relu.py](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/relu/relu.py) | 用 `load()` JIT 编译 `relu.cu` 并做基准/正确性测试 |

> 阅读顺序建议：先看本讲的概念部分，再对照 `relu.cu` 的 `relu_f32_kernel` 和 `elementwise.cu` 的 `elementwise_add_f32_kernel`，两者代码加起来不到 10 行，但浓缩了整个 CUDA 线程模型。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **GPU 线程层次与 SIMT 模型**（以 `relu_f32_kernel` 为载体）
2. **全局索引公式与边界保护**（如何把「线程」映射到「数据下标」）
3. **elementwise_add：同一骨架的多输入推广**（以 `elementwise_add_f32_kernel` 验证模型的可复用性）

---

### 4.1 GPU 线程层次与 SIMT 模型

#### 4.1.1 概念说明

CPU 的程序是「顺序」的：一个 `for` 循环把数组 `x[0], x[1], ..., x[N-1]` 逐个处理。如果你想让它快，得想办法并行。而 GPU 的思路完全不同——**它假设你的问题天然就是「对大量数据做同一件事」**，于是它把那「一件事」（kernel）复制成成千上万份，让大量线程同时各跑一份。

为了管理这么多线程，CUDA 给了一个四级层次，从大到小：

```
Grid（网格）
 └── Block（线程块）   ← 多个 block 组成一个 grid
      └── Warp（线程束）← 32 个相邻线程组成一个 warp
           └── Thread（线程）← 最小执行单元
```

数量上的直觉：

- **一个 warp 恒定 32 个线程**。这是硬件级的「不可分割」单位，是 SIMT 的基本单位。
- **一个 block 包含若干个 warp**。比如 256 个线程 = 8 个 warp。block 内的线程可以同步、可以共享一块共享内存（后面讲义会用到）。
- **一个 grid 包含若干个 block**。grid 是一次 kernel launch 启动的全部线程。

> **什么是 SIMT？** Single Instruction, Multiple Threads。意思是：**同一个 warp 里的 32 个线程，在任意一个瞬间执行的都是同一条指令**，只是各自操作不同的数据。这就像一个教练（控制单元）对着 32 个学员（线程）喊口令「现在做 ReLU！」，32 个学员同时执行 `y = max(0, x)`，但他们手里的 `x` 各不相同。
>
> 这跟 CPU 的 SIMD（如 AVX）思想类似，但 GPU 把这种并行藏在了硬件里，你写出来的代码看起来就像「普通处理单个元素的函数」，不需要手动写向量指令（向量化是后面的优化，见 u2-l3）。

SIMT 的一个重要推论：**如果同一 warp 内的 32 个线程走了不同的分支（比如有的进 `if`、有的不进），硬件会让所有线程把两条分支都走一遍，再各取所需，这叫 branch divergence（分支分歧），会浪费算力。** 本讲的 naive kernel 里，唯一可能产生分歧的是 `if (idx < N)` 的尾部边界，后面会讲怎么看待。

#### 4.1.2 核心流程

一次 kernel launch 的执行流程：

1. **host**（CPU）调用 `kernel<<<grid, block>>>(args)`，告诉 GPU：启动 `grid` 个 block，每个 block 有 `block` 个线程。
2. GPU 把这些 block 分配到各个 SM（Streaming Multiprocessor，流多处理器）上，每个 SM 一次能容纳若干个 block。
3. 每个 block 内部，硬件把线程按 32 个一组切成若干个 warp。
4. 每个 warp 里的 32 个线程**锁步（lockstep）**执行同一段 kernel 代码，各自用 `threadIdx` 区分自己。
5. 每个线程用 `blockIdx`、`blockDim`、`threadIdx` 三个内置变量算出自己负责的「全局下标」，去读写对应的数据。

三个内置变量是本讲的灵魂，必须记住：

| 变量 | 含义 | 类型/维度 |
|------|------|----------|
| `threadIdx` | 当前线程在**所在 block 内**的编号 | `.x .y .z`，每个 0 ~ blockDim-1 |
| `blockDim` | 一个 block 有多少个线程 | `.x .y .z` |
| `blockIdx` | 当前 block 在**整个 grid 内**的编号 | `.x .y .z`，每个 0 ~ gridDim-1 |
| `gridDim` | 整个 grid 有多少个 block | `.x .y .z` |

> 注意：`threadIdx` 和 `blockIdx` 是「相对编号」，而 `blockDim` 和 `gridDim` 是「尺寸」。LeetCUDA 的这些 elementwise kernel 都只用一维（`.x`），所以我们暂时只关心 `xxx.x`。

用伪代码表示「线程 → 全局下标」的心智模型：

```
我（一个线程）是谁？
  局部编号 threadIdx.x        （我在我所在 block 里是第几号，0~255）
  block 尺寸 blockDim.x        （我这个 block 一共多少线程，通常是 256）
  block 编号 blockIdx.x        （我所在 block 在整个 grid 里是第几个，0~grid-1）

我对应的数据下标：
  global_idx = blockIdx.x * blockDim.x + threadIdx.x
```

#### 4.1.3 源码精读

来看 LeetCUDA 里最简单的 kernel——单精度 ReLU：

[relu_f32_kernel — naive 版本](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/relu/relu.cu#L21-L25)

```cpp
// grid(N/256), block(K=256)
__global__ void relu_f32_kernel(float *x, float *y, int N) {
  int idx = blockIdx.x * blockDim.x + threadIdx.x;   // 关键：全局索引
  if (idx < N)
    y[idx] = fmaxf(0.0f, x[idx]);                    // 这就是 ReLU 本体
}
```

逐行解读：

- **`__global__`**：这个修饰符告诉编译器「这是一个可以从 host 启动、在 device 上执行的 kernel」。普通 `__host__` 函数跑在 CPU 上，`__device__` 函数只能被 GPU 调用。
- **`int idx = blockIdx.x * blockDim.x + threadIdx.x;`**：本讲的核心公式。把「我属于第几个 block」乘以「每个 block 的线程数」，再加上「我在 block 内的编号」，就得到我在全局唯一的编号。读到这里请务必在脑中演算：如果 `blockIdx.x=2`、`blockDim.x=256`、`threadIdx.x=5`，那 `idx = 2*256 + 5 = 517`。
- **`if (idx < N)`**：边界保护，下一节细讲。
- **`y[idx] = fmaxf(0.0f, x[idx]);`**：ReLU 的数学定义 \( y = \max(0, x) \)。`fmaxf` 是单精度取最大值的内置函数。注意每个线程只处理**一个** `idx`，没有 `for` 循环——并行是 GPU 自己铺开的。

> **关键观察：kernel 函数体里完全没有「循环」**。CPU 写法会写 `for (int i=0;i<N;i++) y[i]=max(0,x[i]);`，而 GPU 把这个循环「摊开」到了 N 个线程上，每个线程只负责一个下标。这就是并行思维的核心转变。

文件顶部还定义了一个常量，揭示 warp 的固定大小：

[WARP_SIZE 宏定义](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/relu/relu.cu#L11)

```cpp
#define WARP_SIZE 32
```

这印证了前面说的：**一个 warp 固定 32 个线程**，这是 NVIDIA 硬件的物理常数，从 Volta 到 Hopper 都没变过（本讲虽然不直接用它，但它会贯穿后续所有讲义，比如 warp reduce、MMA 矩阵分块）。

#### 4.1.4 代码实践

**实践目标**：亲手演算「线程 → 全局下标」的映射，建立对线程层次的肌肉记忆。

**操作步骤（纯纸笔，无需 GPU）**：

1. 假设一次 launch 配置为 `blockDim.x = 256`，`gridDim.x = 4`。
2. 填写下面这张表，算出每个线程的 `idx`：

| blockIdx.x | threadIdx.x | blockIdx.x * blockDim.x | idx |
|-----------|-------------|------------------------|-----|
| 0 | 0 | 0*256=0 | 0 |
| 0 | 255 | ? | ? |
| 1 | 0 | ? | ? |
| 1 | 10 | ? | ? |
| 3 | 255 | ? | ? |

3. 回答：这个 launch 一共启动了多少个线程？最后一个线程的 `idx` 是多少？

**预期结果**：
- 表格第三行 `idx = 255`；第四行 `idx = 256`；第五行 `idx = 266`；第六行 `idx = 3*256+255 = 1023`。
- 总线程数 = `gridDim.x * blockDim.x = 4 * 256 = 1024`，最后一个 `idx = 1023`。

**需要观察的现象**：你会发现 `idx` 从 0 连续递增到 1023，**没有重复、没有遗漏**——这正是「全局索引」公式的正确性保证：它给每个线程分配了一个唯一且连续的下标。后续所有更复杂的 kernel（softmax、GEMM）的索引计算，本质都是这个公式的推广。

#### 4.1.5 小练习与答案

**练习 1**：一个 block 有 256 个线程，等于多少个 warp？

**参考答案**：256 / 32 = 8 个 warp。

**练习 2**：为什么说 warp 是 GPU 的「最小执行单位」？能不能只启动 1 个线程？

**参考答案**：因为 SIMT 要求同一 warp 的 32 个线程锁步执行同一条指令，硬件以 warp 为单位调度、取指、执行。即使你只要 1 个线程干活，硬件也会启动一个完整的 32 线程 warp，其中 31 个线程处于「不活动」状态（浪费），所以 warp 是不可分割的调度单位。

---

### 4.2 全局索引公式与边界保护

#### 4.2.1 概念说明

上一节我们知道了每个线程算自己的 `idx`。但还有一个现实问题：**数据规模 N 不一定是 blockDim 的整数倍**。

比如 N=1000、每个 block 256 个线程，那么需要 `ceil(1000/256) = 4` 个 block = 1024 个线程。但数据只有 1000 个，多出来的 24 个线程（`idx` 从 1000 到 1023）如果没有保护，就会去读写 `x[1000]...x[1023]`——而这些位置要么是越界（导致段错误），要么是别人的数据（导致写坏结果）。

所以每个 kernel 都必须有**边界保护**：

```cpp
if (idx < N) { /* 真正干活 */ }
```

当 `idx >= N` 时，线程直接什么都不做地返回。代价是这 24 个线程（实际是不到一个 warp 的线程）会产生一点 branch divergence，但这是必要且微不足道的代价。

#### 4.2.2 核心流程

host 端决定「启动多少个 block」的标准做法是**向上取整除法**（ceiling division）：

\[
\text{grid} = \left\lceil \frac{N}{\text{block}} \right\rceil = \frac{N + \text{block} - 1}{\text{block}}
\]

用整数除法实现「向上取整」是一个经典技巧：` (N + block - 1) / block `。

完整的「host 编排 → device 执行」流程：

```
[host]
1. 选定 block 尺寸，例如 256
2. grid = (N + 256 - 1) / 256        ← 向上取整，保证线程数 ≥ N
3. relu_f32_kernel<<<grid, block>>>(x, y, N)   ← 启动
         │
         ▼
[device，每个线程各自执行一遍下面的代码]
4. idx = blockIdx.x * blockDim.x + threadIdx.x
5. if (idx < N):                      ← 边界保护
6.     y[idx] = max(0, x[idx])
```

注意 host 传给 device 的 `N` 既是「数据规模」又是「边界判断依据」。

#### 4.2.3 源码精读

device 端的边界保护已经在 4.1.3 看到了（`if (idx < N)`）。现在看 host 端怎么算 grid/block。

LeetCUDA 用一个宏 `TORCH_BINDING_RELU` 把「算配置 + 启动 kernel」封装起来。对一维输入（`ndim != 2` 分支）的核心两行：

[host 端计算 grid/block 并启动 kernel](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/relu/relu.cu#L128-L132)

```cpp
dim3 block(256 / (n_elements));          // block 尺寸
dim3 grid((N + 256 - 1) / 256);          // grid 尺寸：向上取整
relu_##packed_type##_kernel<<<grid, block>>>(
    reinterpret_cast<element_type *>(x.data_ptr()),
    reinterpret_cast<element_type *>(y.data_ptr()), N);
```

解读：

- **`dim3`**：CUDA 提供的三维向量类型，用于描述 block/grid 的维度。`dim3 block(256)` 表示 `block.x=256, block.y=1, block.z=1`，即一维 256 个线程。
- **`<<<grid, block>>>`**：kernel launch 语法，把配置夹在四角括号里。这是「从 host 启动 device kernel」的标准写法。
- **向上取整**：`(N + 256 - 1) / 256` 正是上一节的公式，保证 grid 足够大。

这个宏会针对不同精度/向量化版本实例化，其中 naive 的 f32 版本传入 `n_elements=1`：

[实例化 relu_f32（n_elements=1，即每个线程处理 1 个元素）](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/relu/relu.cu#L157)

```cpp
TORCH_BINDING_RELU(f32, torch::kFloat32, float, 1)
```

于是对 `relu_f32`，`block = 256/1 = 256`，`grid = ceil(N/256)`，与 kernel 注释 `// grid(N/256), block(K=256)` 完全吻合。

> 小提醒：宏里还处理了二维张量（`ndim == 2`）的情况，把 `grid` 设为行数 S、`block` 设为列方向线程数。本讲聚焦一维映射，二维分支你只需知道「同样的线程模型可以换一种 grid/block 维度来组织」即可。

#### 4.2.4 代码实践

**实践目标**：手算 grid 大小，理解边界保护的必要性。

**操作步骤**：

1. 阅读上面的 host 代码，确认对 `relu_f32`，`block=256`，`grid = (N+255)/256`。
2. 手算以下三个 N 的 grid 与总线程数：

| N | grid = ceil(N/256) | 总线程数 = grid×256 | 越界线程数（idx≥N） |
|---|--------------------|-------------------|------------------|
| 1000 | ? | ? | ? |
| 1024 | ? | ? | ? |
| 1025 | ? | ? | ? |

3. 思考：对 N=1000，第 4 个 block（`blockIdx.x=3`）里 `threadIdx.x` 从多少号开始越界？

**预期结果**：

| N | grid | 总线程数 | 越界线程数 |
|---|------|---------|----------|
| 1000 | (1000+255)/256 = 1255/256 = **4** | 4×256 = 1024 | 1024-1000 = 24 |
| 1024 | (1024+255)/256 = 4（整除）| 1024 | 0 |
| 1025 | (1025+255)/256 = 1280/256 = **5** | 5×256 = 1280 | 1280-1025 = 255 |

N=1000 时，第 4 个 block 覆盖 `idx = 3*256 + t = 768..1023`，其中 `idx ≥ 1000` 即 `768+t ≥ 1000` → `t ≥ 232`，所以 `threadIdx.x` 从 232 号开始越界，这些线程会被 `if (idx < N)` 挡住。

**需要观察的现象**：N=1024 恰好整除，没有越界线程；而 N=1025 多 1 个元素就要多启动一整个 block（256 个线程），其中 255 个线程空转。这就是为什么 grid 大小总是「宁可多不可少」，靠边界保护来兜底。

> 如果无法在本地运行，以上为纸笔推导的预期结果，可直接验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么用 `(N + block - 1) / block` 而不是 `N / block`？

**参考答案**：`N / block` 是向下取整，当 N 不是 block 的整数倍时会少算一个 block，导致末尾一部分数据（`idx` 在 `block*(N/block)` 到 N-1 之间的元素）没有任何线程处理，结果出错。`(N + block - 1) / block` 实现向上取整，保证线程数 ≥ N。

**练习 2**：如果删掉 `if (idx < N)`，对 N=1000、block=256 会发生什么？

**参考答案**：`idx` 最大到 1023 的线程会去写 `y[1000..1023]` 和读 `x[1000..1023]`。若 `y`/`x` 数组恰好分配了恰好 N 个元素，则越界访问可能引发越界写（UB/段错误），或写坏相邻内存；即使侥幸没崩，结果也不正确。`if (idx < N)` 是必备保护。

**练习 3**：把 `dim3 block(256)` 改成 `dim3 block(64)`，其他不变，对正确性有影响吗？

**参考答案**：没有正确性影响。grid 会相应变成 `ceil(N/64)`，每个线程仍用同样的公式算 `idx`，仍能唯一连续覆盖 0..N-1。block 大小主要影响「占用率（occupancy）」和性能，不影响朴素 elementwise 的正确性（只要 grid 算对）。这一点会在 u16-l1（性能分析）深入讨论。

---

### 4.3 elementwise_add：同一骨架的多输入推广

#### 4.3.1 概念说明

理解了 ReLU 之后，「逐元素加法」几乎是免费的——**线程层次、索引公式、边界保护全都一模一样**，唯一的区别是 kernel 接收多个输入张量（a、b），把结果写到 c。

这一节的目的不是讲新概念，而是让你建立信心：**这一套 grid/block/thread 骨架是所有 elementwise 算子的通用模板**。无论是 ReLU、加法、GELU、SiLU、scale……只要你「每个输出元素只依赖对应位置的输入元素」，就能直接套用这个模板，只改 kernel 函数体里那一行计算。

#### 4.3.2 核心流程

逐元素加法的执行流程，与 ReLU 完全同构：

```
[host] grid = ceil(N/256), block = 256
[device 每个线程]
  idx = blockIdx.x * blockDim.x + threadIdx.x
  if (idx < N)
      c[idx] = a[idx] + b[idx]     ← 唯一不同的地方：读两个、加起来
```

注意三个输入张量 a、b、c 用**相同的** `idx` 索引——因为它们形状一致、布局一致，所以同一个线程的 `idx` 在三个数组里指向「同一位置」。

#### 4.3.3 源码精读

[elementwise_add_f32_kernel — naive 版本](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/elementwise/elementwise.cu#L23-L28)

```cpp
// ElementWise Add grid(N/256),
// block(256) a: Nx1, b: Nx1, c: Nx1, c = elementwise_add(a, b)
__global__ void elementwise_add_f32_kernel(float *a, float *b, float *c,
                                           int N) {
  int idx = blockIdx.x * blockDim.x + threadIdx.x;   // 和 ReLU 完全相同
  if (idx < N)
    c[idx] = a[idx] + b[idx];                        // 读两个输入，做加法
}
```

对照 `relu_f32_kernel`，唯一的区别是：

| 维度 | relu_f32_kernel | elementwise_add_f32_kernel |
|------|----------------|---------------------------|
| 输入指针 | `float *x`（1 个） | `float *a, *b`（2 个） |
| 输出指针 | `float *y`（1 个） | `float *c`（1 个） |
| 索引公式 | `idx = blockIdx.x*blockDim.x + threadIdx.x` | **完全相同** |
| 边界保护 | `if (idx < N)` | **完全相同** |
| 核心计算 | `y[idx] = fmaxf(0,x[idx])` | `c[idx] = a[idx] + b[idx]` |
| host 配置 | `block=256, grid=ceil(N/256)` | **完全相同**（见宏） |

> 这张表是本讲最重要的一张表。它告诉你：**学一个 elementwise kernel 等于学了一类**。线程骨架是「公共基础设施」，算子只是插在上面的「插头」。

elementwise 的 host 端 grid/block 计算在 `TORCH_BINDING_ELEM_ADD` 宏里，逻辑与 ReLU 一致：

[elementwise 的 host 端配置（与 ReLU 同构）](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/elementwise/elementwise.cu#L153-L154)

```cpp
dim3 block(256 / (n_elements));
dim3 grid((N + 256 - 1) / 256);
```

#### 4.3.4 代码实践

**实践目标**：亲手把这套模板套到一个新算子上，验证「改一行就能换算子」的直觉。

**操作步骤（仿写 LeakyReLU 的 naive kernel，示例代码）**：

LeakyReLU 的定义是：

\[
y = \begin{cases} x, & x \geq 0 \\ \alpha x, & x < 0 \end{cases}
\]

其中 \( \alpha \) 是一个很小的正斜率（如 0.01），让负数不是直接归零而是保留一点梯度。请仿照 `relu_f32_kernel`，写一个 `leaky_relu_f32_kernel`。grid/block 配置与 `relu_f32` 完全相同。

下面是**示例代码**（不是仓库原有代码），供你对照：

```cpp
// 示例代码：LeakyReLU naive kernel
// grid(ceil(N/256)), block(256)
__global__ void leaky_relu_f32_kernel(float *x, float *y, int N, float alpha) {
  int idx = blockIdx.x * blockDim.x + threadIdx.x;   // 与 relu 完全相同的索引
  if (idx < N) {                                      // 与 relu 完全相同的边界
    float v = x[idx];
    y[idx] = (v >= 0.0f) ? v : alpha * v;             // 仅这一行是 LeakyReLU 本体
  }
}
```

完成后请自检：

1. 索引公式是否与 `relu_f32_kernel` 一字不差？
2. 是否保留了 `if (idx < N)` 边界保护？
3. host 端能否直接复用 `dim3 block(256); dim3 grid((N+255)/256);`？（答案：能，因为每个线程仍只处理 1 个元素。）

**需要观察的现象 / 预期结果**：

- 索引和边界这两行应当与仓库里的 `relu_f32_kernel` 完全一致，证明骨架可复用。
- 给定输入 `x = [2.0, -3.0, -0.5, 4.0]`，`alpha = 0.01`，期望输出 `y = [2.0, -0.03, -0.005, 4.0]`。
- 若要真正运行验证，可参考 `relu.py` 的测试模式（构造张量、与 PyTorch 参考实现比对最大误差）。本讲不要求运行，能写出 kernel 并手算几组结果即可。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：把 `elementwise_add_f32_kernel` 改成 `elementwise_mul_f32_kernel`（逐元素相乘），需要改动几处？分别是什么？

**参考答案**：只需改 1 处——核心计算那一行从 `c[idx] = a[idx] + b[idx];` 改成 `c[idx] = a[idx] * b[idx];`。函数名、参数、索引公式、边界保护、host 配置都可以保持不变（函数名按惯例改成 mul 以免混淆）。

**练习 2**：`elementwise_add` 里有 3 个数组指针 a、b、c，它们的 `idx` 是同一个吗？为什么可以这样？

**参考答案**：是同一个 `idx`。因为 a、b、c 形状完全相同（都是 `N`，且内存布局一致），所以「同一个线程」对应这三个数组的「同一位置」，直接用同一个 `idx` 访问三者即可。这是 elementwise 算子的共性。

**练习 3**：如果要把 ReLU 的 block 从 256 改成 128，kernel 函数体需要改吗？host 那行需要改吗？

**参考答案**：kernel 函数体**不需要改**（索引公式和边界保护与 block 大小无关）。host 那行需要把 `dim3 block(256)` 改成 `dim3 block(128)`，并把 grid 相应改成 `(N + 128 - 1) / 128`。再次说明：block 大小是 host 端的「编排」决策，device 端代码对它透明。

---

## 5. 综合实践

把本讲三块知识串起来，完成一个小任务：

**任务**：为 LeetCUDA 写一个新的 elementwise 算子 `relu_add_f32`，计算 \( c = \text{ReLU}(a) + b \)（先对 a 取 ReLU，再逐元素加 b）。

要求：

1. **device 端**：仿照 `relu_f32_kernel` / `elementwise_add_f32_kernel`，写一个 `__global__` kernel，接收 `float *a, float *b, float *c, int N`。
2. **索引与边界**：复用本讲的核心公式 `idx = blockIdx.x * blockDim.x + threadIdx.x` 和 `if (idx < N)`。
3. **host 编排**：用 `dim3 block(256)`、`dim3 grid((N + 255) / 256)` 启动它（与 `relu_f32` 同配置）。
4. **手算验证**：对输入 `a = [1, -2, 3, -4]`、`b = [10, 10, 10, 10]`，写出期望输出 `c`。

**参考思路**：

```cpp
// 示例代码
__global__ void relu_add_f32_kernel(float *a, float *b, float *c, int N) {
  int idx = blockIdx.x * blockDim.x + threadIdx.x;
  if (idx < N) {
    float ra = fmaxf(0.0f, a[idx]);   // 复用 ReLU
    c[idx] = ra + b[idx];              // 复用逐元素加
  }
}
```

**预期输出**：

- ReLU(a) = [1, 0, 3, 0]
- c = [11, 10, 13, 10]

这个练习把「ReLU」「逐元素加」「索引公式」「host 配置」全部串到了一起。完成后你就拥有了独立写任意一维 elementwise kernel 的能力——这是后面向量化（u2-l3）、归约（u4）、softmax（u5）的起点。

## 6. 本讲小结

- GPU 用四级层次组织线程：**grid → block → warp → thread**，其中 **warp 固定 32 个线程**（`WARP_SIZE = 32`），是 SIMT 的最小执行单位。
- **SIMT** 要求同一 warp 的 32 个线程锁步执行同一条指令，所以 kernel 里看不到循环——并行由硬件把 kernel 复制成海量线程来体现。
- 全局索引的灵魂公式：`idx = blockIdx.x * blockDim.x + threadIdx.x`，它给每个线程分配唯一且连续的下标。
- host 用**向上取整** `grid = (N + block - 1) / block` 保证线程数 ≥ N，device 用 **`if (idx < N)`** 兜住越界线程。
- `relu_f32_kernel` 与 `elementwise_add_f32_kernel` 共享同一套线程骨架，**改算子只需改核心计算那一行**——这就是 elementwise 模板的可复用性。
- kernel 函数体对 block 大小「透明」，block 尺寸是 host 端的编排决策，主要影响性能而非正确性。

## 7. 下一步学习建议

本讲建立了一维 elementwise 的线程骨架，接下来建议：

- **u2-l2（内存层次与合并访问）**：理解 `idx` 连续为什么重要——同一 warp 里 32 个线程访问连续对齐地址才能合并（coalesce）成一次内存事务，这是 elementwise kernel 性能的关键。
- **u2-l3（向量化访存）**：回到 `relu_f32x4_kernel`，看怎么让每个线程处理 4 个元素（float4），把 grid/block 缩小 4 倍、把访存指令数减少 4 倍。
- 继续阅读 [kernels/relu/relu.cu](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/relu/relu.cu) 里 fp16 系列的 kernel（`relu_f16_kernel` 等），它们用的也是同一套线程骨架，区别只在数据类型是 `half`，可作为本讲的延伸练习。
