# ReadOnlyMem（1D 篇）：一维纹理内存缓存只读数据

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 CUDA 中「纹理内存」是什么：一条独立于普通全局内存的、**带缓存的只读数据通路**，以及把一段显存绑定到纹理引用的完整生命周期（声明 → `cudaMalloc` → `cudaBindTexture` → `tex1Dfetch` → `cudaUnbindTexture`）。
2. 读懂 `ReadOnlyMem_1D_Texture` 的三个 kernel，特别是纹理版与全局内存版的**唯一差异**，并理解这种「同进程、同配置、只换数据通路」的对照实验设计。
3. 用 nvprof 的 kernel 时间行（而不是程序打印的 wall time）判断纹理通路是否带来加速，并解读仓库自带的 Carina/Fornax 归档结果。
4. 回答一个反直觉的问题：**在这个顺序访问的 AXPY 上，纹理为什么没有更快、甚至略慢？** 并由此掌握纹理路径真正的适用边界。

## 2. 前置知识

### 2.1 只读数据与 GPU 的三条读取通路

一个 kernel 要读一块显存，硬件上可走的通路不止一条：

| 通路 | 声明方式 | 缓存特点 | 最适合的访问形态 |
| --- | --- | --- | --- |
| 普通全局内存 | `REAL* x` 参数 | L1/L2 缓存， warp 合并访问时效率最高 | 顺序、合并（u4-l2 讲过） |
| 纹理内存（本讲） | `texture<float,1>` + `tex1Dfetch` | 独立的纹理缓存入口，按空间局部性缓存 | 2D 邻域、随机 gather、只读 |
| 常量内存（下一讲 u5-l3） | `__constant__` + `cudaMemcpyToSymbol` | 同一指令读**同一地址**时一个周期广播 | 小块、全线程共享的标量/系数 |

「纹理」（texture）这个词来自图形学：GPU 最早是用来画图的，贴图（texture map）的采样由专门的纹理单元处理，自带一块针对**空间局部性**优化的缓存——相邻像素通常要读相邻的纹素。后来 CUDA 把这条通路开放给通用计算：任何**只读**的显存块都可以绑定到纹理上，让读取走这块缓存。

本讲的基准名字 ReadOnlyMem 点明了它的使用前提：数据一旦拷到设备就**不再被写**（这里是输入向量 `x`），只有读。`y` 是输出，仍然走普通全局内存。

### 2.2 与前面讲义的衔接

- **u2-l3** 讲过的显存管理五段式骨架（`cudaMalloc` → H2D → kernel → D2H → `cudaFree`）在这里原样出现，本讲只是在中间插入了一步「绑定纹理」。
- **u1-l4** 讲过的测量口径在这里是生死攸关的：程序打印的 `time` 包含 `cudaMalloc` 和 `cudaMemcpy`，Carina 归档数据里三个 kernel 合计只占墙钟的约 2%，**判断纹理快慢必须看 nvprof 的 kernel 时间行**。
- **u4-l2** 讲过的合并访问是理解结论的钥匙：顺序访问的 AXPY 对普通全局通路已是「最优客户」，这正是纹理占不到便宜的原因。

### 2.3 一个术语澄清：texture reference 与 texture object

本目录用的是**旧式纹理引用（texture reference）API**：`texture<float,1> rT1` 是一个文件级静态对象，host 侧用 `cudaBindTexture` 把显存挂上去，kernel 直接引用它。CUDA 5.0 之后官方推荐改用**纹理对象（texture object）**：`cudaTextureObject_t` 可以作为 kernel 参数传递，不再依赖全局静态变量。官方自 CUDA 11 起将纹理引用 API 标记为弃用，在很新的工具链上本目录代码可能报弃用警告甚至无法编译（待本地验证）。学习时把它当作「教学用的简化写法」即可，概念（绑定 + 带缓存的只读通路）两种 API 完全一致。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [ReadOnlyMem_1D_Texture/axpy_cudakernel.cu](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_1D_Texture/axpy_cudakernel.cu) | kernel 文件 + host 包装函数 `axpy_cuda` | 纹理声明、三个 kernel、绑定/解绑 |
| [ReadOnlyMem_1D_Texture/axpy_cuda.c](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_1D_Texture/axpy_cuda.c) | host 主程序（实验控制器） | 数据初始化、串行基线、计时循环、check |
| [ReadOnlyMem_1D_Texture/axpy.h](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_1D_Texture/axpy.h) | 接口头文件 | `REAL` 宏、`extern "C"` 契约 |
| [ReadOnlyMem_1D_Texture/Makefile](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_1D_Texture/Makefile) | 单行式编译 | `nvcc` 混编 `.c` 与 `.cu` |
| [ReadOnlyMem_1D_Texture/test.sh](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_1D_Texture/test.sh) | 实验脚本 | 5 个规模的 nvprof 运行 |
| [ReadOnlyMem_1D_Texture/axpy_cuda.output.carina.txt](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_1D_Texture/axpy_cuda.output.carina.txt) / [axpy_cuda.output.fornax.txt](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_1D_Texture/axpy_cuda.output.fornax.txt) | 两台集群机器的归档输出 | 无 GPU 环境的「云实验数据」 |
| [CoMem_AXPY/axpy_cudakernel.cu](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cudakernel.cu) | 对照基准 | 普通全局内存版 AXPY，用于跨程序对比 |

README 把 ReadOnlyMem 归入第三类挑战「合理安排 CPU 与 GPU 之间的数据搬运」，反模式是「大量只读数据」，优化手段是「把只读数据放进 constant/texture 内存以获得更高速度」（[README.md:L84-L87](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/README.md#L84-L87)）。本讲验证这句话在 1D 顺序访问场景下**是否成立**——剧透：不完全成立，而这个「不成立」正是本讲最有价值的知识点。

## 4. 核心概念与源码讲解

本讲的三个规格模块加一个结果解读模块，构成四个最小模块：

1. `texture<float,1>` 声明与纹理只读通路
2. `axpy_cudakernel_1perThread_texture`：读纹理的 kernel 与同程序对照
3. `cudaBindTexture` / `cudaUnbindTexture`：绑定生命周期
4. 顺序访问下纹理为何不占优：归档数据解读与适用边界

---

### 4.1 模块一：`texture<float,1>` 声明与纹理只读通路

#### 4.1.1 概念说明

要在 kernel 里通过纹理读数据，必须先有一个「纹理引用」。它像一根插座：声明在文件作用域，本身不占用设备显存；`cudaBindTexture` 把一段已分配的显存（插头）插上去之后，kernel 里的 `tex1Dfetch(插座, 下标)` 就从这段显存取数。

声明的三个模板参数各有含义：

```cpp
texture<float, 1, cudaReadModeElementType> rT1;
```

- `float`：元素类型。**纹理硬件支持的类型有限**，`tex1Dfetch` 支持 `int`、`float` 等基本类型，但**不支持 `double`**——本目录把 `REAL` 恰好定义为 `float`（见 4.1.3），两者才对得上。这一点决定了把 `REAL` 切成 `double` 会让本基准编译失败（对比：CoMem_AXPY 切精度只需改宏，见 u2-l4）。
- `1`：维度。一维数组用 1，下一讲 u5-l3 的矩阵加法用 `texture<float,2>`。
- `cudaReadModeElementType`：读取模式为「按元素原类型返回」。另一个取值 `cudaReadModeNormalizedFloat` 会把整型元素归一化到 \([0,1]\) 区间返回 float——那是图形学风格用法。本基准要的是原始 `float` 值，所以选前者。

#### 4.1.2 核心流程

使用一维纹理的完整时序：

```text
编译期   : texture<float,1> rT1;            // 声明插座（文件作用域静态对象）
host 期  : cudaMalloc(&d_x, ...)            // 分配普通显存
host 期  : cudaMemcpy(d_x, x, ..., H2D)     // 数据进设备（仍走普通通路）
host 期  : cudaBindTexture(0, rT1, d_x)     // 插座 ← 插头：d_x 从此可经纹理读
kernel 期: tex1Dfetch(rT1, i)               // 按元素下标读，走纹理缓存
host 期  : cudaUnbindTexture(rT1)           // 拔掉插头
host 期  : cudaFree(d_x)                    // 显存照常释放
```

三个容易踩的坑：

1. **绑定不拷贝数据**。`cudaBindTexture` 只是把 `d_x` 这个地址「记到」纹理引用上，数据搬运仍然由前面的 `cudaMemcpy` 完成。所以纹理路径省的是「读的通路」，不省「搬的流量」——它解决的是 README 三类挑战中的「读带宽」，而不是「传输量」（那是 UniMem/MiniTransfer 的主题）。
2. **`d_x` 仍是合法指针**。绑定后同一块显存仍可以用普通指针访问（本基准的 warmingup 和全局版 kernel 就这么干），两条通路并存。
3. **越界不报错**。`tex1Dfetch` 对超出绑定范围的读取通常返回 0 而不是崩溃，所以 kernel 里的 `if (i < n)` 守卫依然必要。

#### 4.1.3 源码精读

纹理声明位于 kernel 文件顶部、所有函数之外：

- [ReadOnlyMem_1D_Texture/axpy_cudakernel.cu:L12-L12](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_1D_Texture/axpy_cudakernel.cu#L12-L12)：声明一维、元素类型 `float`、按元素类型读取的纹理引用 `rT1`。它是静态全局对象，本文件所有 kernel 都能看见——这也是旧 API 的缺点：kernel 与纹理引用耦合在同一编译单元。

元素类型的「双重定义」要对照着看：

- [ReadOnlyMem_1D_Texture/axpy.h:L6-L14](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_1D_Texture/axpy.h#L6-L14)：`#define REAL float`，并用 `extern "C"` 声明 `axpy_cuda` 接口。纹理声明的 `float` 必须与这里的 `REAL` 一致；若改成 `double`，`tex1Dfetch(rT1, i)` 返回 `float` 而赋给 `double` 参与运算，或者干脆因纹理不支持 double 而编译失败（具体表现待本地验证）。

host 侧的初始化与基线在主程序里：

- [ReadOnlyMem_1D_Texture/axpy_cuda.c:L72-L81](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_1D_Texture/axpy_cuda.c#L72-L81)：分配 `y_cuda`/`y`/`x` 三块主机内存，`srand48(1<<12)` 固定种子后 `init` 填随机数，`memcpy` 让 CUDA 路径与串行路径从同一初值出发，然后先跑串行 `axpy` 基线。
- [ReadOnlyMem_1D_Texture/axpy_cuda.c:L41-L47](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_1D_Texture/axpy_cuda.c#L41-L47)：串行参考实现 `y[i] += a*x[i]`，它只作正确性参照、不参与计时（u2-l4 讲过的 oracle 概念）。

#### 4.1.4 代码实践

**实践 A：数一数绑定发生了多少次。**

1. 实践目标：确认「绑定」是每次调用 `axpy_cuda` 都执行的运行期操作，而不是一次性的初始化。
2. 操作步骤：阅读 [ReadOnlyMem_1D_Texture/axpy_cuda.c:L83-L91](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_1D_Texture/axpy_cuda.c#L83-L91)（`num_runs = 10` 的计时循环）与 [axpy_cudakernel.cu:L39-L62](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_1D_Texture/axpy_cudakernel.cu#L39-L62)（`axpy_cuda` 内部），先在纸上推出 `cudaBindTexture` 的调用次数；再打开归档输出 [axpy_cuda.output.carina.txt:L23-L25](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_1D_Texture/axpy_cuda.output.carina.txt#L23-L25) 核对。
3. 需要观察的现象：nvprof 的 API calls 表中 `cudaBindTexture` 与 `cudaUnbindTexture` 的 `Calls` 列数值。
4. 预期结果：两者都是 **10 次**（与 `num_runs` 一致），单次耗时约 2~6 µs。绑定开销本身极小，但它反复发生在 `cudaMalloc`/`cudaFree` 之间，属于本基准「计时循环内做全套生命周期」的结构（u1-l4 讲过其代价）。
5. 若无法运行 nvprof，直接读归档文件即可完成本实践。

#### 4.1.5 小练习与答案

**练习 1**：为什么纹理声明写在函数外面，而 `d_x` 指针却作为 `axpy_cuda` 的局部变量存在？

**答案**：旧式纹理引用是文件作用域的静态对象，`cudaBindTexture` 在运行期把「某个显存地址」记录到这个静态对象上，kernel 编译时只需要知道引用的名字，不需要知道地址；而 `d_x` 是运行期由 `cudaMalloc` 返回的地址，天然是局部变量。这正是 texture object API（`cudaTextureObject_t` 可作参数）想要消除的别扭之处。

**练习 2**：`cudaReadModeNormalizedFloat` 适合什么场景？本基准能用吗？

**答案**：它把整型纹素归一化成 \([0,1]\) 内的 float 返回，适合图形学中处理 8 位颜色分量的场景。本基准的数据是任意随机浮点数（不是按整数编码的），一旦归一化就丢了原始值，所以必须用 `cudaReadModeElementType`。

---

### 4.2 模块二：`axpy_cudakernel_1perThread_texture`——读纹理的 kernel 与同程序对照

#### 4.2.1 概念说明

本目录其实有三个 kernel，构成一组「同程序、同数据、同启动配置」的对照：

| kernel | `x` 的读取方式 | 在实验中的角色 |
| --- | --- | --- |
| `axpy_cudakernel_warmingup` | 普通全局内存 | 预热（不计时分析）＋第二次全局测量 |
| `axpy_cudakernel_1perThread_texture` | **纹理 `tex1Dfetch`** | 被测的优化版 |
| `axpy_cudakernel_1perThread` | 普通全局内存 | 被测的基线（反模式侧） |

注意纹理版的函数签名**没有 `x` 参数**：`x` 的入口从「形参指针」变成了「编译期可见的纹理引用」。这是识别「数据走哪条通路」的最快方法——看签名里少了谁。

这个设计与 CoMem_AXPY 形成有趣对照：CoMem_AXPY 的四个 kernel 改的是**任务划分**（1perThread / block / cyclic），数据通路不变；本目录的三个 kernel 任务划分完全相同（都是每线程一个元素、都是 `(n+255)/256` 个块），**只改数据通路**。两篇基准合起来正好覆盖了「并行化」与「访存通路」两个正交的优化维度。

#### 4.2.2 核心流程

每次调用 `axpy_cuda`，设备侧发生：

```text
warmingup<<<(n+255)/256, 256>>>(d_x, d_y, n, a)   # 读全局 x，预热
        ↓ cudaDeviceSynchronize
1perThread_texture<<<(n+255)/256, 256>>>(d_y, n, a)  # x 经纹理读 ← 被测 A
        ↓ cudaDeviceSynchronize
1perThread<<<(n+255)/256, 256>>>(d_x, d_y, n, a)  # 读全局 x ← 被测 B
        ↓ cudaDeviceSynchronize
D2H 拷回 y
```

两个被测 kernel 的每一次执行：读 `n` 个 `x` 元素（各自通路）、读改写 `n` 个 `y` 元素（同为全局内存）。因此**唯一的实验变量就是 `x` 的读取通路**——教科书级的单变量对照。代价是三个 kernel 共享 `d_y` 顺序叠加（10 轮共 30 次叠加），单独验证某一个 kernel 时需要隔离运行（u2-l2 讲过同样的陷阱）。

#### 4.2.3 源码精读

三个 kernel 逐行对比：

- [ReadOnlyMem_1D_Texture/axpy_cudakernel.cu:L14-L20](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_1D_Texture/axpy_cudakernel.cu#L14-L20)：warmingup kernel，读全局 `x`。与 CoMem_AXPY 的 warmingup 一字不差，作用是消化首次启动的一次性开销。
- [ReadOnlyMem_1D_Texture/axpy_cudakernel.cu:L23-L29](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_1D_Texture/axpy_cudakernel.cu#L23-L29)：**本讲主角**。签名是 `(REAL* y, int n, REAL a)`——没有 `x`；函数体 `y[i] += a * tex1Dfetch(rT1, i)` 中，`tex1Dfetch` 以**元素下标** `i`（不是字节偏移）读取纹理 `rT1`，读取请求发往纹理单元的缓存通路。线程编号计算 `i = blockDim.x * blockIdx.x + threadIdx.x` 与守卫 `if (i < n)` 与 u2-l1 讲的完全一致。
- [ReadOnlyMem_1D_Texture/axpy_cudakernel.cu:L31-L37](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_1D_Texture/axpy_cudakernel.cu#L31-L37)：全局内存基线版。与纹理版的差异只有一处：`a * x[i]` 换成 `a * tex1Dfetch(rT1, i)`，以及签名多一个 `x` 参数。

host 侧的启动序列与绑定位置：

- [ReadOnlyMem_1D_Texture/axpy_cudakernel.cu:L47-L55](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_1D_Texture/axpy_cudakernel.cu#L47-L55)：`cudaMalloc`/H2D 之后先 `cudaBindTexture(0, rT1, d_x)`（L47），把输入向量挂上纹理；随后三个 kernel 用**完全相同**的 `<<<(n+255)/256, 256>>>` 配置依次启动，每个后面跟一次 `cudaDeviceSynchronize`（u2-l3 讲过这是测量栅栏与错误可见性的来源）。
- [ReadOnlyMem_1D_Texture/axpy_cudakernel.cu:L57-L61](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_1D_Texture/axpy_cudakernel.cu#L57-L61)：D2H 拷回 `y`，`cudaUnbindTexture(rT1)` 解绑，最后 `cudaFree`。注意顺序：**先解绑再释放**——虽然本程序即使反过来多半也能跑，但语义上纹理不应引用已释放的显存。

编译方式是最简单的单行式 Makefile：

- [ReadOnlyMem_1D_Texture/Makefile:L1-L2](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_1D_Texture/Makefile#L1-L2)：`nvcc -o axpy_cuda axpy_cuda.c axpy_cudakernel.cu`，无 `-arch`、无 `-G`，混编 `.c` 与 `.cu` 靠 `axpy.h` 的 `extern "C"` 对齐符号（u1-l2 讲过）。纹理代码不需要任何特殊编译选项——这是它相对动态并行（要 `-rdc=true`）省心的地方。

#### 4.2.4 代码实践

**实践 B：从 nvprof 输出里读出三行 kernel 时间并排成表。**

1. 实践目标：学会用 GPU activities 表（而不是程序打印的 `time`）对比两条数据通路。
2. 操作步骤：有 GPU 的机器上执行 `make`，然后按 [ReadOnlyMem_1D_Texture/test.sh:L1-L5](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_1D_Texture/test.sh#L1-L5) 依次跑 5 个规模，例如 `nvprof ./axpy_cuda 10240000`；无 GPU 则直接读归档文件。
3. 需要观察的现象：GPU activities 表中三个 kernel 各自的 `Avg` 列，以及 `[CUDA memcpy HtoD]`/`[CUDA memcpy DtoH]` 的占比。
4. 预期结果（以 Carina、n=1024000 为例，[axpy_cuda.output.carina.txt:L11-L15](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_1D_Texture/axpy_cuda.output.carina.txt#L11-L15)）：HtoD 占 65.6%、DtoH 占 32.4%，三个 kernel 合计不到 2%；`1perThread_texture` 平均 17.007 µs，`1perThread`（全局）平均 16.377 µs——**纹理版慢约 3.8%**。结论与 README 的「更高速度」预期相反，原因见 4.4。
5. 若在你的机器上运行，请把三个 kernel 的 Avg 记成一张三列小表（规模 × kernel），供 4.4 与综合实践使用；无法运行则标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：不改任何东西，把纹理版 kernel 换成 `tex1Dfetch(rT1, i+1)`（读下一个元素），程序会崩溃吗？`check` 会报错吗？

**答案**：不会崩溃——最后一个线程的 `i+1` 越界时 `tex1Dfetch` 返回 0 而不是非法访问（越界 fetch 返回零是纹理通路的常规行为）；但结果错了，`check` 的 diffsum 会显著增大。这个思想实验说明纹理通路把「越界访问」从「可能崩溃」变成「悄悄返回 0」，守卫条件更不能省。

**练习 2**：Carina 上 n=1024000 的 checksum 是 `1.83252e+09`（[axpy_cuda.output.carina.txt:L7-L7](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_1D_Texture/axpy_cuda.output.carina.txt#L7-L7)）。请解释它为什么这么大，并用结构推算验证。

**答案**：这不是浮点误差，而是「调用次数不对称」的结构性产物（u1-l4、u2-l4 讲过同类现象）。计时循环 10 轮 × 每轮 3 个 kernel = 30 次 `y += a*x` 叠加到 `y_cuda`，串行基线只做 1 次，净差 \(29a\) 倍。\(x\) 由 `drand48` 生成、\(E[|x|] = 0.5\)，于是

\[
\text{diffsum} \approx 29 \times 123.456 \times 0.5 \times 1024000 \approx 1.833 \times 10^9
\]

与实测 `1.83252e+09` 吻合到千分之一。checksum 在这里是「30 次叠加都算对了」的探针，量级本身无意义。

**练习 3**：为什么说本目录的对照实验比「拿 ReadOnlyMem 与 CoMem_AXPY 两个程序比 wall time」更干净？

**答案**：本目录的两个被测 kernel 在同一进程、同一份 `d_x`/`d_y`、同一启动配置下先后执行，唯一的变量是 `x` 的读取通路；而跨程序对比时，两边的 kernel 数量、`d_y` 叠加次数、`cudaMalloc` 次数都不同，wall time 混入了大量与通路无关的开销。跨程序对比只应取 nvprof 里**同名 kernel 的时间行**（见综合实践步骤 1）。

---

### 4.3 模块三：`cudaBindTexture`——把显存块挂到纹理引用上

#### 4.3.1 概念说明

`cudaBindTexture` 是旧式纹理 API 的「接线」函数：它告诉运行时「纹理引用 `rT1` 从现在起映射到显存地址 `d_x` 处的数组」。理解三件事就够了：

1. **它不做任何数据搬运**，只登记地址映射（见 4.1.2 的坑 1）。
2. **绑定是有作用范围的**：从 bind 到 unbind 之间，所有用到 `rT1` 的 kernel 读取的都是这块显存。本程序每次 `axpy_cuda` 都 bind→launch→unbind 一轮，共 10 轮。
3. **同一块显存可以被普通指针与纹理同时读**：bind 之后 `d_x` 依旧是合法指针，本程序的 warmingup 与全局版 kernel 就直接用 `d_x` 访问。这既是灵活，也是隐患——如果哪段代码在绑定期间**写**了这块显存，纹理缓存中的副本不会自动失效，可能读到旧值。本基准 `x` 全程只读，安全。

#### 4.3.2 核心流程

绑定相关的生命周期嵌在 u2-l3 的五段式骨架里：

```text
cudaMalloc(d_x, d_y)
cudaMemcpy(H2D: x→d_x, y→d_y)
cudaBindTexture(0, rT1, d_x)          # ← 新增的一步：接线
  warmingup → texture 版 → 全局版     # 三个 kernel
cudaMemcpy(D2H: d_y→y)
cudaUnbindTexture(rT1)                # ← 新增的一步：拔线
cudaFree(d_x, d_y)
```

对照 CoMem_AXPY 的 [axpy_cudakernel.cu:L52-L73](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cudakernel.cu#L52-L73)：两者骨架逐行同构，本目录只是多了 bind/unbind 两行、并把 kernel 清单换成了「同任务划分、异数据通路」的三个。这是 u1-l3「骨架复用」方法论的又一次体现——读懂一个基准，就能预测下一个的结构。

#### 4.3.3 源码精读

- [ReadOnlyMem_1D_Texture/axpy_cudakernel.cu:L47-L47](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_1D_Texture/axpy_cudakernel.cu#L47-L47)：`cudaBindTexture(0, rT1, d_x);`——第一个参数是 offset（这里传 0），随后是纹理引用与设备指针；调用省略了长度参数，运行时按绑定区域的分配范围处理。注意：与本项目所有 CUDA 调用一样，**返回值没有被检查**（u2-l3 批评过的静默失败问题）。
- [ReadOnlyMem_1D_Texture/axpy_cudakernel.cu:L58-L58](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_1D_Texture/axpy_cudakernel.cu#L58-L58)：`cudaUnbindTexture(rT1);`，在 D2H 之后、`cudaFree` 之前解绑。
- [ReadOnlyMem_1D_Texture/axpy_cuda.output.carina.txt:L23-L25](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_1D_Texture/axpy_cuda.output.carina.txt#L23-L25)：归档数据中 `cudaBindTexture` 10 次共 60.8 µs、`cudaUnbindTexture` 10 次共 22 µs——相比 `cudaMalloc` 的 261 ms（L16）完全可以忽略。

#### 4.3.4 代码实践

**实践 C：给绑定加上错误检查（只改 kernel 文件的本地副本，或在纸面上完成）。**

1. 实践目标：体会「绑定失败是静默的」这一风险，并养成检查习惯。
2. 操作步骤：把 L47 改为（示例代码，非项目原有内容）：

   ```cpp
   cudaError_t err = cudaBindTexture(0, rT1, d_x);
   if (err != cudaSuccess) {
       fprintf(stderr, "bind failed: %s\n", cudaGetErrorString(err));
   }
   ```

   再把 `cudaMalloc(&d_x, ...)` 的 `n` 故意乘上一个大数制造分配失败，观察后续 bind 是否报错。（注意：请复制一份 `axpy_cudakernel.cu` 到别的文件名做实验，或改完立刻还原——**不要把改动留在源码目录里**。）
3. 需要观察的现象：分配失败后，`d_x` 为空指针时 bind 的返回值与错误字符串。
4. 预期结果：打印出错误信息而非静默通过；若不改代码，程序会在后续 `cudaMemcpy` 处悄无声息地失败，最后 `check` 输出一个巨大的 diffsum。待本地验证。
5. 无 GPU 环境可只完成「纸面推演」：列出 `axpy_cuda` 里 8 个 CUDA 调用的返回值当前都没有被检查。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `cudaBindTexture` 移到 `cudaMemcpy(d_x, x, ...)` **之前**会发生什么？

**答案**：绑定本身只登记地址，不关心此刻数据是否就位，bind 会成功；kernel 执行时（在拷贝之后）读到的仍是拷贝后的数据，所以结果碰巧正确。但这依赖「kernel 启动晚于拷贝完成」的顺序，属于脆弱写法——`cudaMemcpy` 是阻塞的才保证了这一点，若换成异步拷贝（u5-l1）就必须显式同步。

**练习 2**：nvprof 里 `cudaBindTexture` 每次 2~6 µs，而三个 kernel 每个约 16 µs（Carina，n=1024000）。有人说「绑定开销会让纹理版变慢很多」，对吗？

**答案**：不对。绑定发生在 host 侧、每个 kernel 启动前只做一次，而 kernel 内部要发生 n=1024000 次读取；把 6 µs 摊到百万级读取上可忽略。真正影响对比的是 kernel 自身的执行时间（nvprof 的 GPU activities 行），4.4 的数据正是用它。这是「API 时间 vs kernel 时间」两种口径的区分（u1-l4）。

---

### 4.4 模块四：顺序访问下纹理为何不占优——归档数据解读与适用边界

#### 4.4.1 概念说明

README 说把只读数据放进纹理内存「to get a higher speed」。但归档实验显示本基准里纹理版**没有更快**。要理解这一点，需要回到「纹理缓存到底优化什么」：

纹理缓存是为**空间局部性**（尤其 2D 邻域）与**不规则访问**设计的读取通路。而本基准的访问模式是——

\[
\text{thread } i \text{ 读 } x[i], \quad i = \text{blockDim.x} \cdot \text{blockIdx.x} + \text{threadIdx.x}
\]

warp 内 32 个线程读 **32 个连续 float（128 字节，恰好一个缓存行）**，这是 u4-l2 讲过的完美合并访问：普通全局内存通路下，一次请求就是一次合并事务，L1/L2 已经把这条路径服务到接近带宽上限。给一个已经「满分」的访问模式换通路，自然没有收益，还可能因为纹理单元的取数指令与路径差异略慢。

此外，自 Maxwell 一代之后的架构把常规 L1 与纹理缓存的物理实现合流，全局 load 也走 L1，纹理通路「独立缓存入口」的历史优势被大幅削弱；在更老的架构（Fermi/Kepler 时代，全局内存默认不经过 L1 缓存）上同样的代码更可能看到纹理获益。**「纹理更快」是一个依赖架构与访问模式的条件命题，不是无条件结论。**

那纹理什么时候值得用？三个仍然成立的场景：

1. **2D 空间局部性**：线程按图像坐标组织，线性地址上分散但 2D 上聚集——这正是下一讲 u5-l3（`texture<float,2>`）的主题。
2. **随机 gather**：下标不可预测（如查表、图结构遍历），warp 内地址高度分散，合并无从谈起；纹理缓存按请求缓存整行，且不与常规 L1 的载入竞争，此时纹理经常能追平甚至反超。综合实践会让你亲手做这个实验。
3. **需要硬件功能**：硬件插值、边界处理、归一化坐标——通用计算一般用不到，但知道这些功能「免费」附赠在纹理通路上。

现代 CUDA 还有一条更轻的替代路径：`__ldg()`（或对 `const __restrict__` 指针的读取）让普通指针走只读缓存，不需要声明与绑定。本仓库未使用它，但读别人的代码时会遇到。

#### 4.4.2 核心流程：两台机器五个规模的解读方法

对每个规模，从归档文件里取三个 kernel 的 `Avg` 列，计算两个比值：

\[
\text{纹理相对全局} = \frac{t_{\text{texture}}}{t_{\text{global}}}, \qquad
\text{warmingup 相对全局} = \frac{t_{\text{warmingup}}}{t_{\text{global}}}
\]

第二个比值是「噪音标尺」：warmingup 与全局版代码完全相同（都读全局 `x`），它俩的差值反映测量噪声与缓存状态的随机波动；只有当纹理版的偏离**超过**这个标尺时才值得讨论。

#### 4.4.3 源码精读（归档数据）

Carina（V 系列集群机器之一）的两个端点规模：

- [axpy_cuda.output.carina.txt:L13-L15](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_1D_Texture/axpy_cuda.output.carina.txt#L13-L15)（n=1024000）：texture 17.007 µs / warmingup 16.508 µs / 全局 16.377 µs。纹理比全局慢 **3.8%**，而 warmingup 比全局慢 0.8%——纹理的偏离超出了噪声标尺，是真差异，但量级很小。
- [axpy_cuda.output.carina.txt:L113-L115](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_1D_Texture/axpy_cuda.output.carina.txt#L113-L115)（n=102400000）：texture 1.5439 ms / warmingup 1.5284 ms / 全局 1.5233 ms。纹理慢 **1.4%**，warmingup 慢 0.3%。规模越大差距越收窄——数据集远超缓存容量后，两条通路都由 DRAM 带宽决定，通路差异被冲淡。

Fornax（另一台机器）给出同样的方向性：

- [axpy_cuda.output.fornax.txt:L15-L17](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_1D_Texture/axpy_cuda.output.fornax.txt#L15-L17)（n=1024000）：texture 93.217 µs / warmingup 91.345 µs / 全局 90.261 µs，纹理慢 **3.3%**。
- [axpy_cuda.output.fornax.txt:L119-L121](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_1D_Texture/axpy_cuda.output.fornax.txt#L119-L121)（n=102400000）：texture 7.7258 ms / warmingup 7.6189 ms / 全局 7.5798 ms，纹理慢 **1.9%**。

汇总成表（`Avg`，越低越好）：

| 机器 | n | texture | warmingup | 全局 | 纹理 vs 全局 |
| --- | --- | --- | --- | --- | --- |
| Carina | 1 024 000 | 17.007 µs | 16.508 µs | 16.377 µs | +3.8% |
| Carina | 102 400 000 | 1.5439 ms | 1.5284 ms | 1.5233 ms | +1.4% |
| Fornax | 1 024 000 | 93.217 µs | 91.345 µs | 90.261 µs | +3.3% |
| Fornax | 102 400 000 | 7.7258 ms | 7.6189 ms | 7.5798 ms | +1.9% |

三个结论：

1. **方向一致**：两台机器、五个规模（中间三个规模见归档文件全文）纹理都略慢，1%~4%。
2. **绝对值不可跨平台比**（u1-l4、u4-l3 的老规矩）：Fornax 同规模 kernel 比 Carina 慢 5 倍，但「纹理不占优」这个**相对结论**跨平台稳定。
3. **主时间根本不在 kernel**：五个规模里 `[CUDA memcpy HtoD]` + `[CUDA memcpy DtoH]` 合计占 GPU 时间的 90% 以上（如 [axpy_cuda.output.carina.txt:L11-L15](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_1D_Texture/axpy_cuda.output.carina.txt#L11-L15)）。若有人只看程序打印的 wall time（54.7 ms）讨论「纹理加速」，那是在 2% 的份额里找差异——测量口径必须与问题匹配。

#### 4.4.4 代码实践

**实践 D：把「噪声标尺」方法用到你的机器上。**

1. 实践目标：掌握「用同代码重复测量标定噪声」的对比方法，避免把噪声读成结论。
2. 操作步骤：在你的机器上跑 `nvprof ./axpy_cuda 10240000`，从 GPU activities 表抄下三个 kernel 的 `Avg` 与 `Min`/`Max`；计算 4.4.2 的两个比值。重复跑 3 次看比值是否稳定。
3. 需要观察的现象：`t_texture / t_global` 与 `t_warmingup / t_global` 两个比值各自的波动范围。
4. 预期结果：两个比值都接近 1（比如落在 0.97~1.05）；若 `t_texture/t_global` 的波动范围与 warmingup 标尺重叠，则**不能**断言纹理更慢或更快，只能报告「本访问模式下两通路无显著差异」。若你的 GPU 架构较老或较新，结论可能偏离归档值——这正是要记录机器型号（`nvidia-smi`）的原因。
5. 无 GPU 环境则用归档数据完成：中间三个规模（n=4096000/10240000/20480000）的比值计算可在 [axpy_cuda.output.carina.txt:L36-L40](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_1D_Texture/axpy_cuda.output.carina.txt#L36-L40)、[L63-L65](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_1D_Texture/axpy_cuda.output.carina.txt#L63-L65)、[L88-L90](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_1D_Texture/axpy_cuda.output.carina.txt#L88-L90) 找到原始数据。

#### 4.4.5 小练习与答案

**练习 1**：规模从 1024000 涨到 102400000（100 倍），Carina 上全局版 kernel 从 16.377 µs 涨到 1.5233 ms（约 93 倍）。这说明该 kernel 处于什么状态？

**答案**：时间随规模近线性增长，说明大尺度下 kernel 完全由**访存带宽**决定（读 n 个 x + 读写 n 个 y），与 u2-l1 的判断一致——访存受限。100 倍数据只换来 93 倍时间，说明小规模下还有固定的启动开销被摊薄。对带宽受限的 kernel，换缓存通路的收益上限就是「缓存命中率提升多少」；顺序扫描无重用，命中率本就趋零，所以无收益。

**练习 2**：有人说「既然纹理不快，这个基准失败了」。你如何评价？

**答案**：作为微基准它是成功的：它把「纹理更快」这个流行说法放到受控条件下检验，并诚实地给出了否定证据与适用边界。教学价值恰恰在于反例——学习者由此知道优化必须匹配访问模式与架构，而不是背「只读数据放纹理」的口诀。README 表格描述的是该类挑战的「可用手段」，不是对所有访问模式的承诺。

**练习 3**：如果把这四个 kernel 时间行与 [CoMem_AXPY 的归档输出](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.c) 中同名 `1perThread` 的时间行对比，需要注意什么？

**答案**：只比 nvprof 的 kernel 时间行，且保证命令行 n 相同；不要比 wall time（两程序 kernel 数量与叠加结构不同：CoMem_AXPY 每轮跑 4 个 kernel，本目录跑 3 个）。另外两程序的 `1perThread` 代码相同但所处位置不同（CoMem 里它是第二个 kernel，此处是第三个），L2 缓存状态略有差别，1%~3% 的差异不具解释力。

---

## 5. 综合实践

**任务：同一个 AXPY，三种 `x` 读取方式（全局顺序 / 纹理顺序 / 随机下标）的完整对照实验。**

这个实践把本讲的纹理通路、u4-l2 的合并访问、u2-l4 的校验方法串成一条线，最终产出一张 2×2 的结果表和一段结论。

### 步骤 1：基线对比（纹理 vs 全局，顺序访问）

- 分别进入 `ReadOnlyMem_1D_Texture` 与 `CoMem_AXPY`，各自 `make`，用**相同的 n**（建议 10240000）与相同 block 配置运行：`nvprof ./axpy_cuda 10240000`。
- 只摘 GPU activities 表中的 kernel 行：
  - 本目录的 `axpy_cudakernel_1perThread_texture` 与 `axpy_cudakernel_1perThread`（[axpy_cudakernel.cu:L23-L37](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_1D_Texture/axpy_cudakernel.cu#L23-L37)）；
  - CoMem_AXPY 的 `axpy_cudakernel_1perThread`（[CoMem_AXPY/axpy_cudakernel.cu:L16-L22](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cudakernel.cu#L16-L22)）——它与本目录全局版代码逐字相同，用来确认跨程序的复现性。
- 预期：三个数彼此相差在几个百分点内，纹理不占优（与 4.4 归档一致）。待本地验证。

### 步骤 2：扩展随机下标版（本讲规格要求的第二个观察）

在 `ReadOnlyMem_1D_Texture` 目录**外**建一个实验副本（例如拷贝到 `/tmp/rom-lab/`），新增两个 kernel 与一张索引表。示例代码（非项目原有内容）：

```cpp
// 示例代码：随机 gather 版 kernel
__global__ void axpy_cudakernel_random_global(REAL* x, REAL* y, int* idx, int n, REAL a) {
    int i = blockDim.x * blockIdx.x + threadIdx.x;
    if (i < n) y[i] += a * x[idx[i]];
}

__global__ void axpy_cudakernel_random_texture(REAL* y, int* idx, int n, REAL a) {
    int i = blockDim.x * blockIdx.x + threadIdx.x;
    if (i < n) y[i] += a * tex1Dfetch(rT1, idx[i]);
}
```

host 侧（示例代码）：生成 `idx[i] = (int)(drand48() * n)`，`cudaMalloc` 一个 `d_idx` 并 H2D 拷入；两个 kernel 用与原版相同的 `<<<(n+255)/256, 256>>>` 配置启动。

**实验设计要点（控制变量）**：两个 kernel 从**同一份 `d_idx`**（普通全局内存）读下标，唯一差异仍是「`x` 的读取通路」；这样读下标的开销两边对等，差异才能归因于通路。

### 步骤 3：观察与记录

- 需要观察的现象：顺序版与随机版的 `t_texture / t_global` 比值如何变化；建议再取 n=1024000（工作集约 4 MB，可能整个驻留缓存）与 n=102400000（约 400 MB，必然溢出）两档。
- 预期结果（方向性预期，具体数值**待本地验证**）：
  - 顺序访问：比值 ≈ 1，纹理可能略慢（归档数据支持）。
  - 随机访问、小工作集：warp 内 32 个地址分散到不同扇区，全局版事务数上升（u4-l2），但 L1/L2 可能吸收；纹理走独立缓存入口，比值可能向 1 以下移动或差距明显缩小。
  - 随机访问、大工作集：两版都由 DRAM 决定，比值回到 ≈ 1。
- 若你的工具链没有 nvprof，用 `nsys profile --stats=true` 或 `ncu` 获取 kernel 时间（u1-l4 的替代说明）。

### 步骤 4：写结论

用三句话回答：在本机 GPU（写明型号与计算能力）上，(a) 顺序只读访问用纹理是否值得？(b) 随机 gather 呢？(c) 你的证据是哪几行数据、噪声标尺（warmingup 比值）是多少？——这就是一份最小的微基准实验报告，格式可直接沿用 u6-l2 将要教的骨架。

## 6. 本讲小结

- 纹理内存是一条**带缓存的只读数据通路**：`texture<float,1,cudaReadModeElementType>` 声明引用、`cudaBindTexture` 把 `cudaMalloc` 出来的显存挂上去、kernel 内 `tex1Dfetch(rT1, i)` 按元素下标读取、`cudaUnbindTexture` 解绑；绑定不搬运数据，`x` 的拷贝仍由 `cudaMemcpy` 完成。
- `ReadOnlyMem_1D_Texture` 的三个 kernel（warmingup / texture 版 / 全局版）任务划分与启动配置完全相同，唯一变量是 `x` 的读取通路——纹理版签名里干脆没有 `x` 参数，这是识别数据通路的最快线索。
- 归档数据（Carina/Fornax、五个规模）一致显示：在顺序合并访问的 AXPY 上纹理版比全局版**慢 1%~4%**，且规模越大差距越小。「只读数据放纹理更快」是依赖访问模式与架构的条件命题：全局通路对顺序合并访问已近最优，且现代架构中纹理缓存与常规 L1 已合流。
- 判断这类差异必须用 nvprof 的 kernel 时间行：本基准 90% 以上的 GPU 时间在 HtoD/DtoH 拷贝，程序打印的 wall time 里 kernel 只占约 2%。
- 纹理仍然值得用的三个场景：2D 空间局部性（下一讲）、随机 gather（综合实践）、需要硬件插值/边界处理；现代轻量替代是 `__ldg()` / `const __restrict__`。
- 旧式纹理引用 API 是文件级静态对象，与 kernel 硬耦合，官方已推荐改用 `cudaTextureObject_t`；且纹理不支持 `double`，本目录 `REAL=float` 与 `texture<float,1>` 的类型耦合是切换精度前必须检查的约束。

## 7. 下一步学习建议

- **下一讲 u5-l3（ReadOnlyMem 2D 篇）**：矩阵加法上的 `texture<float,2>` + `cudaBindTexture2D` + `cudaChannelFormatDesc`，以及 `__constant__` 常量内存——那里会出现纹理真正的主场（二维邻域）与「同地址广播」的常量缓存，和本讲的「顺序 1D 纹理不占优」形成完整拼图。
- 回头对照 **u4-l2（CoMem AXPY 合并访问）**：本讲的「纹理为何不快」与那一讲的「block 分布为何劣化」是同一个硬币的两面——都由 warp 内 32 个地址的分布决定；把两讲的结果表放在一起看。
- 阅读 **u5-l4（UniMem）**了解「只读数据」在传输维度的另一种处理：按页按需迁移 vs 整块搬运，与本讲的「读取通路」正交。
- 有余力的读者可以在本地实验副本中把纹理引用改写成 `cudaTextureObject_t`（`cudaCreateTextureObject` + kernel 参数传递），对比两种 API 的代码组织方式——这同时是对付新工具链弃用警告的实用技能（待本地验证）。
