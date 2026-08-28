# ReadOnlyMem（2D 篇）：二维纹理与常量内存

## 1. 本讲目标

学完本讲，你应该能够：

1. 独立完成二维纹理的完整生命周期：`texture<float,2>` 声明 → `cudaCreateChannelDesc<float>()` 通道描述 → `cudaBindTexture2D` 按「宽 × 高 × 行距」绑定显存 → kernel 内 `tex2D` 取数 → `cudaUnbindTexture` 解绑。
2. 读懂 `ReadOnlyMem_2D_Texture` 的五个 kernel，特别是 **普通全局版 `add`、常量版 `add_const`、纹理版 `add_texture`、纹理+常量版 `add_texture_constant`** 这条逐级叠加的对照链。
3. 掌握常量内存的写入方式（`__constant__` + `cudaMemcpyToSymbol`）与它的**广播**特性，并解释为什么本基准里常量版几乎测不出差别（剧透：kernel 参数本来就走常量通路）。
4. 从机制上区分两条只读通路：**纹理缓存按空间局部性缓存，常量缓存按「同址广播」加速**；并结合仓库自带的 Carina/Fornax 两份归档，讨论「只读数据放纹理/常量更快」这句 README 断言在什么条件下成立、什么条件下不成立。
5. 学到一个重要的实验方法论教训：**先核对 nvprof 的 Calls 列再跨机器对比**——本目录的两份归档甚至不是同一份二进制产生的（4.4.3 有 git 证据）。

## 2. 前置知识

### 2.1 从一维到二维：为什么 2D 纹理多出了一个「行距 pitch」

u5-l2 讲过一维纹理：`texture<float,1>` 绑定一段连续显存，`tex1Dfetch(r, i)` 按元素下标取数，硬件不需要知道任何「形状」信息。但矩阵是二维的，线程用 `(x, y)` 两个坐标去取数，硬件必须回答一个问题：**给定的 \(x\)、\(y\) 对应线性地址里的哪个元素？** 这就需要三样东西：

- **width**：每行多少个元素（\(x\) 方向的合法范围）；
- **height**：一共多少行（\(y\) 方向的合法范围）；
- **pitch（行距）**：**字节**为单位的「上一行开头到下一行开头的距离」。

CUDA 对绑定到 pitch-linear（普通 `cudaMalloc` 出来的行主序显存）的二维纹理，取数规则是：

\[
\text{element}(x,\,y) \;=\; \text{base}\Bigl[\, y \times \frac{\text{pitch}}{\text{sizeof}(\text{float})} + x \,\Bigr]
\]

也就是「\(y\) 乘行距、加 \(x\)」。只要 pitch 与数组的真实布局对上，`tex2D(tex, x, y)` 就等价于 `base[y*N + x]`（N 为每行元素数）。**pitch 写错，取的就是另一个元素**——本目录代码恰好在这里埋着一个被「方阵 + 全 1 数据」双重掩盖的疑点（见 4.1.2 的推导）。

> 补充：CUDA 还有一类专门为纹理准备的 `cudaMallocPitch` / `cudaArray` 内存，其行距由硬件按对齐要求自动选择，绑定时应把返回的 pitch 原样传给 `cudaBindTexture2D`。本目录没有用它们，而是直接绑定 `cudaMalloc` 的裸指针，所以 pitch 必须由程序员手工算对。

### 2.2 常量内存：第三条只读数据通路

u5-l2 的三通路表格里，常量内存是最后一行，本讲正式展开。它的要点：

- 声明：`__constant__ int cons_M;`，位于设备代码文件作用域，**所有 kernel 共享同一份**；
- 写入：host 侧用 `cudaMemcpyToSymbol(符号, 源地址, 字节数, 偏移)` 把主机数据拷进去，kernel 不能写它；
- 容量：每个设备常量内存总量固定（CUDA 编程指南给出的经典值是 64 KB），但每个 SM 上有一块小得多的**常量缓存**（经典值 8 KB）；
- 加速机理：当 warp 内 32 个线程在同一条指令处读**同一个地址**时，硬件一次取出后**广播**给全部线程，只消耗一次访问；若 32 个线程各读不同地址，则退化为逐个串行访问，反而比全局内存更糟。

所以常量内存的最优客户是「**小体积、全线程共享、每次读同一个值**」的数据：物理常数、卷积核系数、查询表、以及本讲的矩阵维数 `M`/`N`。

### 2.3 与前面讲义的衔接

- **u5-l2（直接前置）**：一维纹理的生命周期、纹理引用 API 已弃用、顺序访问下 1D 纹理不占优的结论，本讲直接沿用，不重复推导。
- **u2-l3**：`cudaMalloc → H2D → kernel → D2H → cudaFree` 五段式骨架，本讲的 `matadd` 包装函数完全遵循，只是中间多了「绑定纹理」与「写常量」两步。
- **u1-l4**：测量口径。本目录的 host 程序**根本不打印时间**（见 4.1.3），一切时间数据都来自 nvprof 的 kernel 时间行——这比其他基准更极端，口径问题在这里不存在歧义，但也意味着没有 GPU 就只能读归档。
- **u4-l2**：合并访问。矩阵加法是最典型的合并访问负载，这一点是解读「纹理为什么（不）快」的钥匙。
- **u4-l7 / u5-l1**：「校验 PASS 不等于实现无瑕疵」的审查习惯，本讲会再次用到，而且抓到一条更隐蔽的鱼。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [ReadOnlyMem_2D_Texture/matadd_2D_cudakernel.cu](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_2D_Texture/matadd_2D_cudakernel.cu) | kernel 文件 + host 包装函数 `matadd` | 两个纹理引用、两个 `__constant__` 标量、五个 kernel、绑定/写常量/解绑 |
| [ReadOnlyMem_2D_Texture/matadd_2D_cuda.c](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_2D_Texture/matadd_2D_cuda.c) | host 主程序（实验控制器） | 全 1 初始化、串行基线、`num_runs=5`、check；注意它不计时 |
| [ReadOnlyMem_2D_Texture/matadd_2D.h](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_2D_Texture/matadd_2D.h) | 接口头文件 | `#define REAL float`、`extern "C"` 契约 |
| [ReadOnlyMem_2D_Texture/test.sh](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_2D_Texture/test.sh) | 实验脚本 | 4 个规模的 nvprof 运行（1024 → 40960） |
| [ReadOnlyMem_2D_Texture/Makefile](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_2D_Texture/Makefile) | 单行式编译 | `nvcc` 混编 `.c` 与 `.cu` |
| [ReadOnlyMem_2D_Texture/matadd.output.carina.txt](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_2D_Texture/matadd.output.carina.txt) / [matadd.output.fornax.txt](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_2D_Texture/matadd.output.fornax.txt) | 两台集群机器的归档输出 | 无 GPU 环境的「云实验数据」；两份并不来自同一版代码 |

README 把 ReadOnlyMem 归入第三类挑战「合理安排 CPU 与 GPU 之间的数据搬运」，优化手段写的是「把只读数据放进 constant/texture 内存以获得更高速度」（[README.md:L84-L87](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/README.md#L84-L87)）。u5-l2 在 1D 顺序访问上验证了这句话**不成立**；本讲换到 2D 场景，会发现答案更有意思：**在一台机器上不成立，在另一台机器上成立得很彻底**。

## 4. 核心概念与源码讲解

本讲按规格拆成四个最小模块：

1. `texture<float,2>` 声明与 `cudaBindTexture2D` 绑定（含 `tex2D` 寻址疑点）
2. `add_texture`：纹理读、全局写的 kernel 与逐级叠加的对照链
3. `__constant__` 与 `cudaMemcpyToSymbol`：`add_const` 与 `add_texture_constant`
4. 纹理缓存 vs 常量缓存：机理对比与两台机器归档的解读

---

### 4.1 模块一：`texture<float,2>` 声明与 `cudaBindTexture2D` 绑定

#### 4.1.1 概念说明

二维纹理引用是一个文件作用域的静态对象，声明方式与 1D 唯一的差别是模板第二参数（维度）从 1 变 2：

```cpp
texture<float, 2> texMatrixA;   // 示例格式，与源码等价（源码写作 texture<float,2>texMatrixA）
```

要把一块普通显存挂上去，比 1D 多了两件事：

1. **通道描述符 `cudaChannelFormatDesc`**：告诉硬件每个元素由哪些分量构成、各占多少位。`cudaCreateChannelDesc<float>()` 生成「单分量 32 位浮点」的描述符。图形学里一个 RGBA 纹素是四分量，通用计算里的 `float` 矩阵只有一个分量——但描述符照样要给。
2. **几何三元组 `(width, height, pitch)`**：见 2.1。`cudaBindTexture2D` 的完整参数依次是偏移、纹理引用、设备指针、通道描述符、宽、高、行距。

绑定本身仍然**不搬运任何数据**（u5-l2 的结论在 2D 原样成立）：数据进设备靠前面的 `cudaMemcpy`，绑定只是把「形状 + 行距」登记到纹理引用上，让 `tex2D` 能把 `(x, y)` 坐标翻译成线性地址。

#### 4.1.2 核心流程

`matadd` 包装函数中与纹理相关的一段时序：

```text
cudaMalloc(d_matrixA / d_matrixB / d_result)          // 三块普通显存
cudaMemcpy(d_matrixA, h_matrixA, ..., H2D)            // 数据进设备（普通通路）
cudaMemcpy(d_matrixB, h_matrixB, ..., H2D)
cudaBindTexture2D(0, texMatrixA, d_matrixA, desc, N, M, M*sizeof(float))
cudaBindTexture2D(0, texMatrixB, d_matrixB, desc, N, M, M*sizeof(float))
... 启动各 kernel，kernel 内 tex2D(texMatrixA, x, y) ...
cudaUnbindTexture(texMatrixA / texMatrixB)            // 拔掉插头
cudaFree(...)                                          // 显存照常释放
```

**一个值得动手推导的疑点：pitch 与 `tex2D` 的坐标顺序。** 按 2.1 的寻址公式，本目录的绑定与调用组合起来是：

\[
\text{tex2D}(\text{tex},\,\text{tidx},\,\text{tidy}) \;\Rightarrow\; \text{base}\bigl[\,\text{tidy}\times M + \text{tidx}\,\bigr]
\]

（pitch \(= M\times 4\) 字节，除以 4 得每行 \(M\) 个元素；\(x\) 是第一个参数 tidx。）

而普通全局版 kernel 读的是：

\[
\text{base}\bigl[\,\text{tidx}\times N + \text{tidy}\,\bigr]
\]

两者相差一个**转置**。要让纹理版取到与全局版相同的元素，应当写 `tex2D(tex, tidy, tidx)` 且 pitch 用 \(N\times\text{sizeof(float)}\)（对行主序 M 行 N 列的数组而言）。也就是说：

- 疑点一：pitch 传了 `M * sizeof(float)`，正确值应是 `N * sizeof(float)`；
- 疑点二：`tex2D` 的两个坐标传成了 `(tidx, tidy)`，与「宽 N、高 M」的绑定方向相反。

这两个错误在当前程序里**完全测不出来**，因为有双重掩盖：

1. 主程序强制 `M = N`（[matadd_2D_cuda.c:L91](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_2D_Texture/matadd_2D_cuda.c#L91)），方阵下 pitch 数值碰巧正确，且坐标都在绑定范围内（不会越界）；
2. 初始化把所有元素填成同一个常数 1（4.1.3），读「转置位置的 1」和读「原位置的 1」值相同，`check` 自然打印 0。

这是「校验 PASS 不等于实现无瑕疵」的又一实例，且比 u4-l7 的案例更隐蔽。对**性能结论**没有影响（两种取法都是同类的一维跨步/合并模式，流量相同），但如果你想复用这段绑定代码，务必先修掉。待本地验证（验证方法见 4.1.4 实践 B）。

#### 4.1.3 源码精读

纹理引用与常量标量并排声明在 kernel 文件顶部：

- [ReadOnlyMem_2D_Texture/matadd_2D_cudakernel.cu:L13-L14](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_2D_Texture/matadd_2D_cudakernel.cu#L13-L14)：声明两个二维纹理引用 `texMatrixA`、`texMatrixB`，分别对应加法的两个输入矩阵。它们是静态全局对象，本文件所有 kernel 都能看见。
- [ReadOnlyMem_2D_Texture/matadd_2D_cudakernel.cu:L11-L11](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_2D_Texture/matadd_2D_cudakernel.cu#L11-L11)：`#define BLOCK_SIZE 16`，决定 16×16 的二维线程块（见 4.2.2）。

host 侧的绑定三步：

- [ReadOnlyMem_2D_Texture/matadd_2D_cudakernel.cu:L72-L72](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_2D_Texture/matadd_2D_cudakernel.cu#L72-L72)：`cudaCreateChannelDesc<float>()` 生成「单分量 32 位 float」的通道描述符，两个矩阵共用一份。
- [ReadOnlyMem_2D_Texture/matadd_2D_cudakernel.cu:L74-L79](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_2D_Texture/matadd_2D_cudakernel.cu#L74-L79)：`cudaMalloc` 三块显存（两个输入一个输出），随后两次 H2D `cudaMemcpy` 把数据搬进设备——注意绑定发生在**数据已就位之后**。
- [ReadOnlyMem_2D_Texture/matadd_2D_cudakernel.cu:L80-L81](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_2D_Texture/matadd_2D_cudakernel.cu#L80-L81)：`cudaBindTexture2D(0, texMatrixA, d_matrixA, channelDesc, N, M, M * sizeof(float))`——偏移 0、宽 N、高 M、行距 `M*sizeof(float)`。对照 2.1 与 4.1.2 的推导理解最后三个参数；pitch 以**字节**为单位是初学者最常错的地方。
- [ReadOnlyMem_2D_Texture/matadd_2D_cudakernel.cu:L104-L105](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_2D_Texture/matadd_2D_cudakernel.cu#L104-L105)：D2H 拷回结果后 `cudaUnbindTexture` 解绑两个纹理引用，随后 `cudaFree` 三块显存。

主程序的数据初始化与验证：

- [ReadOnlyMem_2D_Texture/matadd_2D_cuda.c:L37-L43](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_2D_Texture/matadd_2D_cuda.c#L37-L43)：`init_matrix` 把每个元素都写成 1，真正的随机初始化 `(REAL)drand48()` 被注释掉了。全 1 数据让加法结果恒为 2、`check` 恒为 0——也让 4.1.2 的转置疑点彻底隐身。
- [ReadOnlyMem_2D_Texture/matadd_2D_cuda.c:L60-L67](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_2D_Texture/matadd_2D_cuda.c#L60-L67)：串行参考实现 `mat_add_serial`，双重循环逐元素相加，只作正确性 oracle、不参与计时（u2-l4 的结论）。
- [ReadOnlyMem_2D_Texture/matadd_2D_cuda.c:L70-L79](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_2D_Texture/matadd_2D_cuda.c#L70-L79)：`check` 计算归一化差值（u2-l4 讲过口径），这里因全 1 数据恒等于 0。
- [ReadOnlyMem_2D_Texture/matadd_2D_cuda.c:L102-L105](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_2D_Texture/matadd_2D_cuda.c#L102-L105)：`num_runs = 5`，循环调用 `matadd`，最后只打印一行 `check:%f`。**整个程序没有任何时间输出**——`read_timer_ms`（[matadd_2D_cuda.c:L18-L22](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_2D_Texture/matadd_2D_cuda.c#L18-L22)）是死代码。本基准是「nvprof 原生」的：一切时间数据都必须从 profiler 拿。另外注意每次 `matadd` 里五个 kernel 共享并先后覆写同一块 `d_result`，D2H 只发生一次，所以 `check` 只能验证**最后一个** kernel（`add_texture_constant`）。

#### 4.1.4 代码实践

**实践 A：从 nvprof 的 Calls 列反推程序结构（无需 GPU，读归档即可）。**

1. 实践目标：证明「绑定/写常量是每次调用 `matadd` 都发生的运行期操作」，并练会用 Calls 列核对源码。
2. 操作步骤：先在纸上数——`num_runs = 5`，每次 `matadd` 调用里有 2 次 `cudaBindTexture2D`、2 次 `cudaUnbindTexture`、2 次 `cudaMemcpyToSymbol`、2 次 H2D `cudaMemcpy`、1 次 D2H。然后打开 [matadd.output.carina.txt:L25-L31](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_2D_Texture/matadd.output.carina.txt#L25-L31) 与 [matadd.output.fornax.txt:L24-L29](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_2D_Texture/matadd.output.fornax.txt#L24-L29) 对照。
3. 需要观察的现象：两份归档里 `cudaBindTexture2D = 10`、`cudaUnbindTexture = 10`、`cudaMemcpyToSymbol = 10`，正好是每轮 2 次 × 5 轮。
4. 预期结果：Calls 列全部对上。同时注意 GPU activities 里 `[CUDA memcpy HtoD] = 20`（[matadd.output.carina.txt:L14](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_2D_Texture/matadd.output.carina.txt#L14)），而 API calls 里 `cudaMemcpy = 15`——差的 5 次去哪了？（答案见 4.3.5 练习 3。）

**实践 B：戳穿「方阵 + 全 1」的双重掩盖（需 GPU，待本地验证）。**

1. 实践目标：验证 4.1.2 的转置疑点确实存在。
2. 操作步骤（两种做法任选）：
   - 做法一（改数据）：把 [matadd_2D_cuda.c:L40](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_2D_Texture/matadd_2D_cuda.c#L40) 里被注释的 `(REAL)drand48()` 恢复、删掉 `1`，保持 `M = N` 不变，重新编译运行；
   - 做法二（改形状）：保持全 1 数据，把 [matadd_2D_cuda.c:L91](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_2D_Texture/matadd_2D_cuda.c#L91) 的 `M=N` 改成 `M = N/2`（注意矩阵分配与串行基线都用 M、N，一并核对）。
3. 需要观察的现象：程序打印的 `check` 值。
4. 预期结果：做法一下 `check` 从 0.000000 变为明显非零（随机数据下转置读取拿到的是别的位置的值）；做法二下方阵掩盖消失、`tex2D` 的 \(x\) 坐标（tidx 最大到 M−1）可能超出绑定宽度 N，默认的钳制地址模式会返回边缘值，`check` 同样应非零。**两个结果都待本地验证**；若 `check` 仍为 0，说明本讲的推导有误，请回推 2.1 的寻址公式再下结论。
5. 修复方向（供对照）：绑定改为 `(…, N, M, N * sizeof(float))`，kernel 里改写为 `tex2D(texMatrixA, tidy, tidx)`，恢复全 1 数据后 `check` 应回到 0。

#### 4.1.5 小练习与答案

1. **练习**：如果矩阵按行主序存储为 M 行 N 列（元素 `(i,j)` 在 `i*N+j`），`cudaBindTexture2D` 的 width、height、pitch 三个参数各应传什么？
   **答案**：width = N（每行元素数，即 \(x\) 方向范围），height = M（行数，即 \(y\) 方向范围），pitch = `N * sizeof(float)`（相邻两行开头的字节距离）。本目录源码传的是 `(N, M, M*sizeof(float))`，第三个参数在 M≠N 时是错的。
2. **练习**：`cudaBindTexture2D` 执行之后，`d_matrixA` 这个裸指针还能用吗？数据有没有被复制？
   **答案**：能用、也没被复制。绑定只是在纹理引用上登记「地址 + 形状 + 行距」，显存里没有第二份数据；同一个地址既可走纹理通路（`tex2D`）也可走普通通路（解引用指针），本基准的 `add` kernel 与 `add_texture` kernel 正是分别用这两条通路读同一块显存。
3. **练习**：通道描述符 `cudaCreateChannelDesc<float>()` 描述的是什么？为什么 `double` 矩阵不能直接照搬这套写法？
   **答案**：它描述每个元素（纹素）的分量构成与位宽——`float` 是「1 个分量 × 32 位」。纹理硬件支持的元素类型有限，经典纹理通路不支持 `double`；本目录 `.h` 里 `REAL` 恰好定义为 `float`（[matadd_2D.h:L6](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_2D_Texture/matadd_2D.h#L6)），两者才对得上（与 u5-l2 的结论一致）。

---

### 4.2 模块二：`add_texture`——纹理读、全局写的 kernel 与逐级叠加的对照链

#### 4.2.1 概念说明

本基准的精妙之处在于它不是「两个程序各跑一遍」，而是**同一份代码里四个被测 kernel 共享同一份输入、同一个线程配置、同一块输出**，逐级叠加变量：

| kernel | 输入读取通路 | 维数来源 | 变化了什么 |
| --- | --- | --- | --- |
| `add` | 普通全局内存 | kernel 参数 `d_M`/`d_N` | 基线 |
| `add_const` | 普通全局内存 | `__constant__` 的 `cons_M`/`cons_N` | + 常量内存 |
| `add_texture` | 二维纹理 `tex2D` | kernel 参数 `d_M`/`d_N` | + 纹理通路 |
| `add_texture_constant` | 二维纹理 `tex2D` | `__constant__` 的 `cons_M`/`cons_N` | 两者叠加 |

这样 `add` vs `add_const` 单独隔离出「常量内存」一个变量，`add` vs `add_texture` 单独隔离出「纹理通路」一个变量，`add_texture_constant` 则检验两者是否可叠加——这正是微基准「控制变量」方法论的教科书式落地（u1-l3 讲过的同构性思想）。

还有一个容易被忽略的事实：**纹理只加速「读」**。`add_texture` 的输入 A、B 走纹理缓存，但输出 `d_Result` 仍是普通全局内存写。所以它优化的是输入侧的读取通路，输出的写流量一点没少。

#### 4.2.2 核心流程

每个 kernel 的执行流程（五者结构相同，只差读通路与维数来源）：

```text
tidx = blockDim.x * blockIdx.x + threadIdx.x     // x 方向全局编号（本代码当作行号用）
tidy = blockDim.y * blockIdx.y + threadIdx.y     // y 方向全局编号（本代码当作列号用）
if (tidx < M && tidy < N):                        // 守卫：矩阵边缘
    u = 读取 A[tidx*N + tidy]                     // ← 唯一变化点：全局指针 / tex2D
    v = 读取 B[tidx*N + tidy]
    d_Result[tidx*N + tidy] = u + v               // 写永远是普通全局内存
```

线程组织是本仓库第一次出现的**二维网格**：

- 每块 `dim3 threadsperblock(16, 16, 1)`，即 16×16 = 256 个线程，正好平铺一个 16×16 的矩阵瓦片（BLOCK_SIZE 与 u4-l1 的分块思想呼应，但这里没有共享内存，瓦片只是线程编排）；
- 网格 `blocks.x = ceil(M / 16)`、`blocks.y = ceil(N / 16)`，用「整除判断 + 条件加一」实现了向上取整，配合 kernel 内的 `if` 守卫处理非整倍规模。

一次 `matadd` 调用里五个 kernel **串行**启动（`add_warmingup` → 四个被测 kernel），每个后面跟一次 `cudaDeviceSynchronize()`。注意 `add_warmingup` 与 u2-l1 的 warmingup 动机相同但处境不同：这里没有 host 计时，预热的意义只剩「让首次启动的一次性开销落在一个不参与对比的 kernel 里」，使四个被测 kernel 的 nvprof 首次调用尽量干净。

#### 4.2.3 源码精读

四个被测 kernel 的关键差异点：

- [ReadOnlyMem_2D_Texture/matadd_2D_cudakernel.cu:L29-L36](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_2D_Texture/matadd_2D_cudakernel.cu#L29-L36)：基线 `add`。两个输入矩阵走普通全局指针，边界用 kernel 参数 `d_M`/`d_N` 守卫。它就是 u4-l2 讲过的**完美合并访问**模式：warp 内相邻线程的 `tidy` 连续，地址连续。
- [ReadOnlyMem_2D_Texture/matadd_2D_cudakernel.cu:L48-L57](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_2D_Texture/matadd_2D_cudakernel.cu#L48-L57)：`add_texture`。**签名里没有两个输入矩阵指针**——输入地址已经在 `texMatrixA`/`texMatrixB` 里了，只剩输出指针和维数。两条 `tex2D` 语句以 `(tidx, tidy)` 为坐标取数（坐标顺序的疑点见 4.1.2），相加后写回普通全局内存。开头的 `__global__ static` 中 `static` 对 kernel 无实际作用，只是风格残留。
- [ReadOnlyMem_2D_Texture/matadd_2D_cudakernel.cu:L20-L27](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_2D_Texture/matadd_2D_cudakernel.cu#L20-L27)：`add_warmingup`，函数体与 `add` 完全一致、只是名字不同，先于四个被测 kernel 启动以吸收一次性开销。
- [ReadOnlyMem_2D_Texture/matadd_2D_cudakernel.cu:L86-L100](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_2D_Texture/matadd_2D_cudakernel.cu#L86-L100)：host 侧二维网格计算（`blocks.x` 由 M、`blocks.y` 由 N，均向上取整）与五个 kernel 的串行启动，每个后面跟一次同步。第 102 行还有一次冗余的 `cudaDeviceSynchronize()`（[L102](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_2D_Texture/matadd_2D_cudakernel.cu#L102)），紧跟着的 D2H `cudaMemcpy` 本身就是同步语义（u2-l3），无害但多余。
- [ReadOnlyMem_2D_Texture/test.sh:L1-L4](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_2D_Texture/test.sh#L1-L4)：实验脚本，四个规模的 `nvprof` 运行。注意最大规模 40960：40960² × 4 B ≈ **6.7 GB/矩阵**，三块合计约 20 GB，小显存 GPU 会在 `cudaMalloc` 处失败（而本代码不检查返回值，是静默失败，u2-l3 的教训）。归档 [matadd.output.fornax.txt](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_2D_Texture/matadd.output.fornax.txt) 恰好只有前三个规模、缺 40960 段，显存不足是一个合理猜测（待确认）。

#### 4.2.4 代码实践

**实践：算一算每个 kernel 的有效带宽，判断它离硬件上限有多远（无需 GPU，读归档即可）。**

1. 实践目标：用「流量 ÷ 时间」把 nvprof 的原始时间变成可解释的物理量。
2. 操作步骤：
   - 取 Carina 归档 40960 规模的四个 kernel 平均时间（[matadd.output.carina.txt:L119-L124](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_2D_Texture/matadd.output.carina.txt#L119-L124)）；
   - 每次调用读 A、B 各一遍、写 Result 一遍，流量 \(Q = 3 \times M \times N \times 4\,\text{B}\)；
   - 有效带宽 \(B_{\text{eff}} = Q / t\)。
3. 需要观察的现象：以 `add` 的 90.784 ms 为例，\(Q = 3 \times 40960^2 \times 4 \approx 20.1\,\text{GB}\)，算得 \(B_{\text{eff}} \approx 222\,\text{GB/s}\)。
4. 预期结果：把四个 kernel 的带宽都算出来填进表里；它们彼此接近（差距 ≤ 5%），说明四个 kernel 都把同一条访存通路跑到了相近的利用率，差异只在「走哪条缓存入口」。再用 Fornax 的 20480 数据（[matadd.output.fornax.txt:L71-L77](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_2D_Texture/matadd.output.fornax.txt#L71-L77)，\(Q \approx 5.0\,\text{GB}\)）重算一遍——`add` 约 18 GB/s、`add_texture` 约 78 GB/s，两台机器的数量级差异就是 4.4 要解释的现象。

#### 4.2.5 小练习与答案

1. **练习**：`add_texture` 的函数签名为什么比 `add` 少了两个指针参数？这带来什么工程上的副作用？
   **答案**：因为输入矩阵的设备地址已经登记在文件作用域的纹理引用 `texMatrixA`/`texMatrixB` 上，不必再经参数传递。副作用是 kernel 与纹理引用强耦合在同一个编译单元里，无法把 kernel 复制成通用组件；这也是官方后来推荐纹理对象（`cudaTextureObject_t` 可作参数传）的原因之一（u5-l2 的弃用说明）。
2. **练习**：这个矩阵加法负载在 u4-l2 的合并访问框架下属于哪种访问模式？
   **答案**：warp 内相邻线程的 `tidy`（列号）连续、`tidx`（行号）相同，地址步长 4 字节连续——是完美合并访问。换句话说，基线 `add` 已经是普通全局内存的「最优客户」，这为「纹理占不到便宜」的 Carina 结论埋下伏笔。
3. **练习**：既然四个 kernel 都写同一块 `d_result`，host 程序的 `check` 能同时验证它们四个吗？
   **答案**：不能。五个 kernel 在一次 `matadd` 内先后覆写 `d_result`，D2H 只在最后发生一次，所以 `check` 只验证最后一个 `add_texture_constant` 的输出（且因全 1 数据的掩盖，它的验证力还要再打折，见 4.1.2）。要单独验证某个 kernel，得把其他启动注释掉重编译——u2-l2 讲过的「隔离验证」。

---

### 4.3 模块三：`__constant__` 与 `cudaMemcpyToSymbol`——`add_const` 与 `add_texture_constant`

#### 4.3.1 概念说明

常量内存解决的问题是：**有一小块所有线程都要读、且每次读的值都相同的数据，能不能只从缓存里取一次、广播给全 warp？** 答案是能，这正是常量缓存的设计目标（2.2）。

使用上的三段式：

1. **声明**：`__constant__ int cons_M;` 写在 `.cu` 文件作用域。它住在设备上一块独立的 64 KB 区域（经典值），生命周期同程序，所有 kernel 可见、只读。
2. **写入**：host 侧 `cudaMemcpyToSymbol(cons_M, &M, sizeof(float), 0)`，参数依次是设备侧符号、主机源地址、字节数、符号内偏移。它是一次真正的 H2D 拷贝（在 nvprof 的 GPU activities 里计入 `[CUDA memcpy HtoD]`，在 API calls 里单列一行）。
3. **读取**：kernel 里直接写 `cons_M`，语法上像读全局变量。

本基准放进常量内存的不是数据矩阵，而是**两个维数标量 M、N**——它们恰好是「广播」的理想客户：kernel 里每条守卫判断、每次地址计算都要读它们，且 warp 内 32 个线程读的是同一个地址。

但这里藏着一个本讲最重要的认知：**kernel 参数本来就放在常量内存里**。CUDA 的调用约定把 `<<<>>>` 的实参（如 `add` 的 `d_M`、`d_N`）放进一块专用的 constant bank，kernel 取参数时同样享受同址广播。所以 `add` 与 `add_const` 的差别只是「维数放在编译器管理的常量区」还是「放在用户声明的常量区」，**两条物理通路几乎相同**——这解释了为什么归档里两者时间差在 1%–3% 的噪声水平（4.4.3 的表）。想真正展示常量内存的优势，实验设计应该放「一大块全线程共享的系数数组」，而不是两个标量。

#### 4.3.2 核心流程

```text
编译期 : __constant__ int cons_M, cons_N;          // 在设备上预留两个 4 字节槽位
host 期: cudaMemcpyToSymbol(cons_M, &M, 4, 0)      // 把主机栈上的 M 拷进槽位（真正的 H2D）
host 期: cudaMemcpyToSymbol(cons_N, &N, 4, 0)
kernel : if (tidx < cons_M && tidy < cons_N) ...   // 32 线程同址读 → 一次访问 + 广播
```

**什么时候广播会失效**：若 warp 内各线程读常量内存的**不同**地址（比如 `coef[threadIdx.x]`），硬件无法广播，退化为逐个串行服务，32 个不同地址最坏要 32 次——比读全局内存还慢。这是常量内存的头号反模式，选用前必须先确认访问形态是「同址」。

另外注意一个小瑕疵：源码写的是 `cudaMemcpyToSymbol(cons_M, &M, sizeof(float), 0)`，拷贝字节数用了 `sizeof(float)`（4 字节），而 `cons_M` 是 `int`（也是 4 字节）——两者数值恰好相等所以现在没错，但这是**巧合而非正确**：若有人把 `REAL`/元素类型改掉，或换到 `int` 非 4 字节的平台，这里就悄悄变成越界/欠拷贝。工程上应写 `sizeof(cons_M)`。

#### 4.3.3 源码精读

- [ReadOnlyMem_2D_Texture/matadd_2D_cudakernel.cu:L16-L18](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_2D_Texture/matadd_2D_cudakernel.cu#L16-L18)：注释 `//constant memory` 之下声明两个 `__constant__ int`。它们与纹理引用并排放着，正好构成本讲的两条只读通路。
- [ReadOnlyMem_2D_Texture/matadd_2D_cudakernel.cu:L83-L84](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_2D_Texture/matadd_2D_cudakernel.cu#L83-L84)：host 侧两条 `cudaMemcpyToSymbol`，每轮 `matadd` 都执行一次（归档里 Calls = 10 = 2×5）。第四个参数 `0` 是符号内偏移。
- [ReadOnlyMem_2D_Texture/matadd_2D_cudakernel.cu:L38-L45](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_2D_Texture/matadd_2D_cudakernel.cu#L38-L45)：`add_const`。与 `add` 逐行对照：签名少掉 `d_M`/`d_N` 两个参数，函数体内的 `d_M`/`d_N` 全部换成 `cons_M`/`cons_N`，其余一字不差——单变量对照的范本。
- [ReadOnlyMem_2D_Texture/matadd_2D_cudakernel.cu:L59-L68](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_2D_Texture/matadd_2D_cudakernel.cu#L59-L68)：`add_texture_constant`，签名最短——输入在纹理里、维数在常量里，只剩一个输出指针。它是 `add_texture` 与 `add_const` 的叠加，也是每次调用五个 kernel 里最后启动的那个，因此是 host `check` 实际校验的对象。
- [ReadOnlyMem_2D_Texture/matadd_2D_cudakernel.cu:L95-L100](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_2D_Texture/matadd_2D_cudakernel.cu#L95-L100)：`add_const`、`add_texture`、`add_texture_constant` 的启动序列，可见四个被测 kernel 的启动配置完全相同（同一个 `blocks`、同一个 `threadsperblock`），差异只可能在 kernel 内部。

#### 4.3.4 代码实践

**实践：把「常量内存对标量无优势」的论断量化（无需 GPU，读归档即可；有 GPU 可复测）。**

1. 实践目标：用归档数据验证 4.3.1 的判断——kernel 参数与 `__constant__` 变量走的是同类广播通路，`add` 与 `add_const` 应无显著差别。
2. 操作步骤：从两份归档抄出每个规模下 `add` 与 `add_const` 的平均时间（Carina：[L18-L19](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_2D_Texture/matadd.output.carina.txt#L18-L19)、[L51-L52](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_2D_Texture/matadd.output.carina.txt#L51-L52)、[L86-L87](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_2D_Texture/matadd.output.carina.txt#L86-L87)、[L123-L124](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_2D_Texture/matadd.output.carina.txt#L123-L124)；Fornax：[L13-L15](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_2D_Texture/matadd.output.fornax.txt#L13-L15)、[L44-L45](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_2D_Texture/matadd.output.fornax.txt#L44-L45)、[L74-L75](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_2D_Texture/matadd.output.fornax.txt#L74-L75)），计算比值 `add_const / add`。
3. 需要观察的现象：比值围绕 1 微幅波动。
4. 预期结果：Carina 四个规模约 0.97–1.00，Fornax 约 0.98–1.00——全部在百分之几的噪声带内，方向还不一致，符合「无显著差别」的预期。若你在本机复测（`make` 后按 [test.sh](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_2D_Texture/test.sh) 跑 nvprof），比值应同样贴近 1；若显著偏离，优先怀疑规模太小被启动开销淹没（1024 档单次仅 30–400 µs）。

#### 4.3.5 小练习与答案

1. **练习**：把一个长度 1024 的系数数组 `coef[1024]` 放进 `__constant__`，kernel 里按 `coef[threadIdx.x % 1024]` 读取。warp 内 32 个线程读的地址是什么关系？性能会怎样？
   **答案**：`threadIdx.x` 连续，`% 1024` 后仍连续，所以 warp 内 32 个线程读的是 32 个**不同**地址——广播失效，常量缓存被迫串行服务（最多 32 次），比读全局内存更糟。要么换成 warp 内同址的访问（`coef[lane/32]` 之类），要么老老实实放全局/纹理内存。这正是 4.3.2 说的头号反模式。
2. **练习**：`cudaMemcpyToSymbol` 的第四个参数 `0` 是什么意思？什么场景下会用到非零值？
   **答案**：它是「符号内部偏移」——从设备侧符号地址开始偏移多少字节再写入。常用于只更新 `__constant__` 结构体/数组的某一段（例如只刷新 `coef[10..19]` 写 offset = `10*sizeof(float)`）。本基准两个标量都从 0 写起。
3. **练习**：API calls 表里 `cudaMemcpy` 是 15 次（每轮 2 次 H2D + 1 次 D2H），为什么 GPU activities 里 `[CUDA memcpy HtoD]` 是 20 次？
   **答案**：`cudaMemcpyToSymbol` 在 API 层单列（10 次 = 2×5 轮），但在 GPU 层它也是一次主机到设备的数据搬运，被计入 `[CUDA memcpy HtoD]`。所以 HtoD 总数 20 = 普通 H2D 的 10 次 + 写常量符号的 10 次。这个小账本是核对「常量写入确实搬了数据」的直接证据（[matadd.output.carina.txt:L14-L25](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_2D_Texture/matadd.output.carina.txt#L14-L25)）。

---

### 4.4 模块四：纹理缓存 vs 常量缓存——机理对比与跨平台归档解读

#### 4.4.1 概念说明

两条只读通路经常被混为一谈，它们的加速机理完全不同：

| 维度 | 纹理缓存 | 常量缓存 |
| --- | --- | --- |
| 加速对象 | **有空间局部性的读取**（邻域、随机 gather） | **warp 内同址的读取**（广播） |
| 缓存粒度 | 缓存行/块，按地址局部性命中 | 一次取 4 字节量级，同址即广播 |
| 不同地址访问 | 正常工作（这正是它的主场） | 串行化惩罚，最多 32 路 |
| 典型容量 | 每 SM 一块（架构相关，几十 KB 量级），与 L1 关系随架构变化 | 设备共 64 KB、每 SM 缓存约 8 KB（经典值） |
| 额外能力 | 坐标钳制、滤波、归一化等图形学功能 | 无，就是一块只读小内存 |
| 本基准的用法 | 读两个输入矩阵 | 读 M、N 两个维数标量 |

一句话记忆：**纹理赌的是「这次读的旁边上次刚读过」，常量赌的是「warp 里 32 个人读的是同一个字」**。矩阵加法里，输入矩阵是前者（虽然合并访问下全局 L1/L2 已经把局部性吃干净了），维数标量是后者（但 kernel 参数本来就在常量区）。

还有一个 u5-l2 提过、这里再次成立的时代背景：纹理引用 API 自 CUDA 11 起被官方弃用，现代写法优先 `__ldg()`（只读数据走纹理通路而不写纹理代码）或纹理对象；`nvprof` 也已被 `nsys`/`ncu` 取代。本目录代码是「教学用的经典写法」。

#### 4.4.2 核心流程

解读归档数据的流程（也是第 5 节综合实践的主线）：

```text
1. 只取 GPU activities 表里四个被测 kernel 的 Avg 列（不要用 Time(%) 排序，占比受 memcpy 干扰）
2. 同一规模内算两组比值：add_texture/add（纹理效应）、add_const/add（常量效应）
3. 跨规模看趋势：比值是否随规模稳定？随规模漂移的结论多半是噪声
4. 跨机器看方向：两台机器的比值是否同号？不同号说明该效应与架构强相关
5. 先核对 Calls 列与源码调用次数是否一致，再相信上面的任何对比
```

#### 4.4.3 源码精读（归档数据解读）

**先做第 5 步——两份归档不是同一份代码产生的。** 把 Calls 列与当前源码对账：

- Carina 归档：只有 4 个 kernel（无 `add_warmingup`），`cudaLaunchKernel = 20`、`cudaDeviceSynchronize = 5`（[matadd.output.carina.txt:L13-L27](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_2D_Texture/matadd.output.carina.txt#L13-L27)）——即每轮 4 个 kernel、1 次同步；
- Fornax 归档：5 个 kernel（含 `add_warmingup`，[matadd.output.fornax.txt:L13-L17](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_2D_Texture/matadd.output.fornax.txt#L13-L17)），`cudaLaunchKernel = 25`、`cudaDeviceSynchronize = 30`（[matadd.output.fornax.txt:L19-L25](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_2D_Texture/matadd.output.fornax.txt#L19-L25)）——即每轮 5 个 kernel、6 次同步，与当前源码 [matadd_2D_cudakernel.cu:L91-L102](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_2D_Texture/matadd_2D_cudakernel.cu#L91-L102) 完全吻合。

git 历史可以解释：提交 `8a27766`（Added constant memory version）时代的代码正是「4 个 kernel 连续启动 + 末尾一次同步」；随后 `01252b5`（MatAdd final tests on Carina）才加入 `add_warmingup` 和逐 kernel 同步。也就是说 Carina 归档对应旧版本、Fornax 归档对应新版本。好在**四个被测 kernel 的函数体在两版之间没有变化**，kernel 级对比仍然有效——但如果不做这步对账，你无法知道这一点。

**再做第 1–4 步——两台机器给出方向相反的结论。** 四个被测 kernel 的平均时间（Avg 列，5 次调用）：

| 规模 | 机器 | add | add_const | add_texture | add_tex_const | 纹理效应 \(t_{\text{tex}}/t_{\text{add}}\) |
| --- | --- | --- | --- | --- | --- | --- |
| 1024 | Carina [L16-L19](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_2D_Texture/matadd.output.carina.txt#L16-L19) | 29.8 µs | 29.0 µs | 30.5 µs | 31.8 µs | 1.02（略慢） |
| 10240 | Carina [L51-L54](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_2D_Texture/matadd.output.carina.txt#L51-L54) | 4.485 ms | 4.463 ms | 4.280 ms | 4.266 ms | 0.95（略快） |
| 20480 | Carina [L86-L89](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_2D_Texture/matadd.output.carina.txt#L86-L89) | 18.819 ms | 18.798 ms | 18.660 ms | 18.650 ms | 0.99 |
| 40960 | Carina [L121-L124](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_2D_Texture/matadd.output.carina.txt#L121-L124) | 90.784 ms | 89.522 ms | 94.322 ms | 94.093 ms | 1.04（略慢） |
| 1024 | Fornax [L13-L17](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_2D_Texture/matadd.output.fornax.txt#L13-L17) | 397.9 µs | 398.4 µs | 120.5 µs | 119.9 µs | **0.30（3.3 倍快）** |
| 10240 | Fornax [L43-L47](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_2D_Texture/matadd.output.fornax.txt#L43-L47) | 33.065 ms | 33.070 ms | 13.914 ms | 13.913 ms | **0.42（2.4 倍快）** |
| 20480 | Fornax [L73-L77](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_2D_Texture/matadd.output.fornax.txt#L73-L77) | 277.31 ms | 272.99 ms | 64.486 ms | 64.469 ms | **0.23（4.3 倍快）** |

三条结论：

1. **常量效应在两台机器上都不存在**（`add_const ≈ add`，偏差 ≤ 3% 且方向乱跳）——这是 4.3.1「kernel 参数本就在常量区」的实测印证。
2. **纹理效应在两台机器方向相反**：Carina 上 ±5% 内抖动（等于噪声），Fornax 上纹理稳定快 2.4–4.3 倍且随规模扩大。叠加版 `add_texture_constant` 总是紧贴 `add_texture`，说明两个变量互不干扰。
3. **归档不记录 GPU 型号**，两份文件都没有机器配置信息（Fornax 只有 nvprof 的 Auto boost 警告，[matadd.output.fornax.txt:L6](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_2D_Texture/matadd.output.fornax.txt#L6)）。我们能说的诚实结论是：纹理通路对这条「读两个矩阵、写一个矩阵」的流式负载，收益**强依赖硬件代际**——在较新的架构上全局内存通路已经够快（L1/L2 吃满局部性，纹理占不到便宜），在较旧的架构上纹理缓存曾是绕开全局通路瓶颈的重要出口。历史背景也支持这个读法：纹理路径正是旧架构时代（全局加载默认不走 L1 缓存的年代）社区常用的优化手段。但「哪台机器是什么 GPU」无法从归档确证，标注待确认。

另注意 memcpy 的统治地位：40960 规模下三次搬运合计约 48 秒（[matadd.output.carina.txt:L119-L120](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_2D_Texture/matadd.output.carina.txt#L119-L120)），四个 kernel 加起来不到 2 秒——u1-l4 的「kernel 时间 vs 墙钟」口径差异在此达到极端。

#### 4.4.4 代码实践

**实践：设计一个「常量内存真正的 showcase」改造方案（源码阅读 + 设计型实践）。**

1. 实践目标：理解为什么本基准测不出常量内存优势，并能设计出测得出的实验。
2. 操作步骤（纸面设计，不必改码）：
   - 对照 [matadd_2D_cudakernel.cu:L29-L36](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_2D_Texture/matadd_2D_cudakernel.cu#L29-L36) 与 [L38-L45](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_2D_Texture/matadd_2D_cudakernel.cu#L38-L45)，确认两者唯一差异是把两个 `int` 从参数挪进 `__constant__`；
   - 设计对照实验 A（应无差别）：维数 M/N 放常量，如源码现状；
   - 设计对照实验 B（应有差别）：把一个长度 32 的系数数组 `coef[32]` 分别放在全局内存与 `__constant__`，kernel 内**所有线程同址**读 `coef[k]`（k 为循环变量，warp 内同值），做乘加；
   - 设计对照实验 C（应反向）：同 B 但读 `coef[threadIdx.x % 32]`，warp 内 32 个不同地址。
3. 需要观察的现象（如实现并运行，待本地验证）：A 组两者时间持平；B 组常量版明显更快（每 warp 只需一次缓存访问）；C 组常量版可能**更慢**（串行化惩罚）。
4. 预期结果：得出「常量内存的优势来自广播，而广播的前提是同址」的可检验命题。若本机无 GPU，把三组设计的预期写成表、注明待本地验证即可。

#### 4.4.5 小练习与答案

1. **练习**：为什么 40960 规模下四个 kernel 只占 GPU 时间的 4% 左右，而 memcpy 占了 96%？
   **答案**：矩阵加法的算术强度极低（读两个数、加一次、写一个数），而每个矩阵在 40960² 规模下有 6.7 GB，H2D + D2H 要把约 20 GB 在 PCIe 上搬两来回；kernel 本身只需约 90 ms，搬运却要数十秒。这不是 kernel 慢，是负载天生被搬运支配——想优化总时间应先动数据搬运（u5 单元的主题），而不是 kernel。
2. **练习**：Fornax 上纹理版快 2.4–4.3 倍，能否据此在你自己的机器上预期同样的加速比？
   **答案**：不能。归档没有记录 GPU 型号、驱动与 nvprof 版本，且 Fornax 的 Auto boost 警告意味着时钟可能漂移；两台机器上该效应方向都相反。正确做法是在自己机器上按 test.sh 复测，并把「架构代际」当作第一解释变量，把跨机器结论限定为相对方向而非倍数。
3. **练习**：u5-l2 的 1D 纹理实验里纹理版慢 1%–4%，本讲 2D 场景在 Carina 上也是 ±5% 的噪声。这两个「不占优」的共同原因是什么？
   **答案**：两个负载都是完美合并的顺序/流式访问——普通全局内存通路在这类负载上已把 L1/L2 与合并事务用到极致，纹理缓存没有可再抢的局部性。纹理的主场是 2D 邻域采样、随机 gather、跨步读取这类全局通路吃亏的访问形态（以及旧架构上全局加载不走 L1 的年代）。

---

## 5. 综合实践

**综合实践：三个 kernel 的同机对比 + 与两台集群归档的交叉验证。**

这是本讲规格指定的主实践，把 4.1–4.4 串成一条完整的实验链。

1. **实践目标**：在自己的机器上测出「纹理通路 / 常量通路」对矩阵加法的真实影响，并判断仓库归档的结论能否跨平台成立。

2. **操作步骤**：
   - 编译：进入 `ReadOnlyMem_2D_Texture/`，`make`（单行 `nvcc`，见 [Makefile:L1-L2](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_2D_Texture/Makefile#L1-L2)）；若工具链已移除 nvprof，用 `nsys profile` / `ncu` 替代并记下工具版本（u1-l2）；
   - 运行：按 [test.sh:L1-L4](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_2D_Texture/test.sh#L1-L4) 依次跑 1024 / 10240 / 20480；显存 ≥ 21 GB 再尝试 40960，否则跳过并注明；
   - 建表：抄下四个被测 kernel（`add`、`add_const`、`add_texture`、`add_texture_constant`）的 Avg 时间与 Calls 数，按 4.4.3 的表式计算纹理效应、常量效应两组比值；
   - 交叉验证：把你的一行数据并进 4.4.3 的表，与 Carina/Fornax 对齐。
   - 无 GPU 的替代方案（源码阅读型）：跳过运行，直接把 4.4.3 的表补充成「Fornax 各规模的纹理加速比 + 你从两份归档算出的有效带宽」双列，并把结论写成三句话。

3. **需要观察的现象**：
   - 你机器上的 `Calls` 列是否等于「每轮 5 个 kernel × 5 轮 = 25」、`cudaDeviceSynchronize = 30`（与 Fornax 版代码对账）；
   - 纹理效应比值落在 Carina（≈1，噪声）与 Fornax（0.2–0.4）之间的哪个区段；
   - 常量效应比值是否同样贴近 1。

4. **预期结果**：
   - 较新架构的消费级/数据中心卡上，大概率复现 Carina 型结论（纹理不占优），因为这类负载的全局通路已被 L1/L2 喂饱；
   - `add_const ≈ add` 在任何机器上都应成立（kernel 参数同走常量通路）；
   - 你的结论应当写成**条件命题**：「在本机（注明 GPU 型号、CUDA 版本）上，对合并访问的矩阵加法，纹理/常量通路不带来收益」，而不是「纹理没用」。待本地验证。

5. **附加题（可选）**：把 4.1.4 实践 B 的验证做掉——恢复随机初始化后 `check` 是否非零、修复 pitch 与坐标顺序后是否回到 0——把你对本讲 4.1.2 疑点的裁决写进实验报告。

## 6. 本讲小结

- 二维纹理比一维多出「形状」信息：`cudaBindTexture2D` 需要 `cudaChannelFormatDesc` + `(width, height, pitch)` 三件套，`tex2D(tex, x, y)` 按 \(y \times \text{pitch}/4 + x\) 寻址；pitch 以字节为单位、x 是列方向，这两点最易写错。
- 源码的绑定把 pitch 写成 `M*sizeof(float)` 且坐标传成 `(tidx, tidy)`，与行主序布局的正确写法（`N*sizeof(float)`、`(tidy, tidx)`）相比相当于读了转置元素——被「M = N」与「全 1 数据」双重掩盖，`check:0.000000` 不能证明实现正确。
- 常量内存的生命周期是 `__constant__` 声明 → `cudaMemcpyToSymbol` 写入 → kernel 直接读；它的收益来自 warp 内**同址广播**，异址访问反而串行化。本基准只放了 M/N 两个标量，而 kernel 参数本来就住在常量区，所以 `add_const ≈ add`（两台机器、全部规模，偏差 ≤ 3%）。
- 两条只读通路的机理：纹理缓存赌**空间局部性**（邻域/gather 主场），常量缓存赌**同址广播**（小体积共享标量/系数主场）；对完美合并的流式负载，纹理在新架构上无利可图（Carina ±5%），在旧架构上曾是关键出口（Fornax 快 2.4–4.3 倍）。
- 方法论两课：① 先对账再对比——两份归档并非同一份二进制（Carina 缺 `add_warmingup`、每轮 1 次同步，对应提交 `8a27766`；Fornax 对应当前代码），靠 Calls 列与 git 历史才能确认 kernel 级对比仍然公平；② 归档不记录 GPU 型号，跨平台只能下相对结论，且要写成条件命题。
- 本基准是「nvprof 原生」的：host 程序不打印任何时间（`read_timer_ms` 是死代码），一切时间来自 profiler 的 kernel 时间行；而 memcpy 占掉 96% 的 GPU 时间，提醒你优化的主战场可能根本不在 kernel。

## 7. 下一步学习建议

- **下一讲 u5-l4（UniMem：统一内存）**：继续 CPU-GPU 数据搬运单元。本讲结尾已经看到 memcpy 支配一切，下一讲正面处理「要不要整体搬运」的问题——`cudaMallocManaged` 按页按需迁移 vs `cudaMalloc` + 整体 `cudaMemcpy`，用访问密度决定胜负。
- **回补 u4-l1（Shmem 分块）**：本讲的输入矩阵是「读一次就扔」的流式数据，纹理缓存没有可复用的局部性；如果同一块数据要被反复读（比如矩阵乘），共享内存分块才是正解，两条路线的边界值得对照一遍。
- **延伸阅读建议**：CUDA C Programming Guide 的「Texture Fetching」与「CUDA C Programming Model → Constant Memory」两节，对照本讲的寻址公式与广播机理读原文；再了解现代替代——`__ldg()` 只读装饰、纹理对象 `cudaTextureObject_t`、以及 ncu 中与只读通路相关的指标，理解本目录经典写法在今天该如何翻译。
- **动手方向**：完成第 5 节综合实践与 4.4.4 的三组常量内存实验设计；如果你要二次开发，先修掉 4.1.2 的绑定疑点与 `sizeof(float)` 拷贝字节数问题，再谈任何性能对比。
