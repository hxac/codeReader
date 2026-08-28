# HDOverlap：主机-设备传输的同步与异步（cudaMemcpyAsync + stream）

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 `cudaMemcpy`（阻塞）与 `cudaMemcpyAsync`（异步）在**语义**上的差别：前者返回即"搬运完成"，后者返回只代表"任务已排入队列"。
2. 掌握「`cudaStreamCreate` 建流 → `cudaMemcpyAsync` 把拷贝排入流 → kernel 启动」这一项目内反复出现的编程模式，理解异步传输为什么能把主机 CPU 从"干等搬运"中解放出来。
3. 识别本基准计时代码的**口径缺陷**：`axpy_cuda_async` 在两次异步拷贝刚入队、尚未发生任何同步时就停止了计时，测到的是 API 调用耗时而不是真实传输耗时；并能用 `cudaEventRecord` / `cudaEventElapsedTime` 或 `cudaStreamSynchronize` 修正它。
4. 顺带认识 pageable（可分页）内存与 pinned（页锁定）内存对"真异步"的决定性影响。

本讲对应 README 汇总表中第三类性能挑战（合理安排 CPU 与 GPU 之间的数据搬运）的第一项：反模式是 "Host-device memory copy takes much time"，优化手段是 "Use cudaMemcpyAsync function to accelerate the data transfer"，见 [README.md:L79-L81](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/README.md#L79-L81)。

## 2. 前置知识

### 2.1 阻塞调用与异步调用

- **阻塞（同步）调用**：函数返回时，它承诺的工作已经完成。`cudaMemcpy` 就是这样——它返回时数据已经到达目的地，代价是调用线程在搬运期间只能空等。
- **异步调用**：函数返回时，工作只是**提交**了，真正执行交给后台。`cudaMemcpyAsync` 返回时拷贝可能还没开始，主机可以继续做别的事。

### 2.2 stream：GPU 侧的任务队列

stream（流）是 CUDA 中按提交顺序执行的任务队列（第 u3-l3 讲 Conkernels 已用它实现并发 kernel，本讲只用它的最小子集）：

- 同一条流内的操作严格按入队顺序先后执行；
- `cudaMemcpyAsync(dst, src, size, kind, stream)` 把一次拷贝作为任务排入指定流；
- `cudaStreamSynchronize(stream)` 阻塞主机，直到该流中全部任务完成——异步模式下"等结果"要靠它显式地做。

### 2.3 搬运由谁执行：DMA 引擎与主机解放

GPU 侧有独立的拷贝引擎（DMA），异步拷贝入队后由它执行，不占用主机 CPU，也不占用 SM 的计算资源。理想情况下"主机发起搬运 → 主机转身去算别的 → 搬完再会合"，这就是 HDOverlap（Host-Device Overlap）名字的含义。

### 2.4 一个关键前提：pageable 与 pinned 主机内存

本基准用 `malloc` 分配主机缓冲区，这是**可分页内存**（pageable），操作系统可以把它换出或搬移。按 CUDA 编程指南的描述：从 pageable 主机内存向设备做异步拷贝时，驱动需要先把数据中转（staging）到一块稳定的内部缓冲区，函数"把 pageable 缓冲区拷入中转区后"即返回，DMA 未必完成。要获得**真异步**（入队即返回、DMA 全程独立进行），主机内存必须用 `cudaMallocHost` / `cudaHostAlloc` 分配成**页锁定内存**（pinned）。这一点决定了本基准实验结果该如何解读，细节在 4.2 节展开。

### 2.5 承接前面几讲

本讲依赖 u2-l3 建立的显存管理五段式（`cudaMalloc` → H2D → kernel → D2H → `cudaFree`）与 u1-l3/u1-l4 建立的骨架认知（warmup、`num_runs` 平均、`read_timer_ms` 墙钟、check 是弱校验探针）。一个新差异要先指出：**HDOverlap 不再是三件套**，host 主程序、kernel、计时、main 全部合在单个 `.cu` 文件里，且实验输出不叫 `.output.*.txt` 而是 `results.txt`。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [HDOverlap/axpy_cudakernel.cu](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/HDOverlap/axpy_cudakernel.cu#L1-L175) | 单文件基准：计时器、kernel、`axpy_cuda_normal` 与 `axpy_cuda_async` 两条路径、串行基线、check、main 全在此。文件头注释（L6）写明它源于对 CUDA11 异步拷贝函数的实验 |
| [HDOverlap/Makefile](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/HDOverlap/Makefile#L1-L3) | 单行式：`nvcc -o axpy_cuda axpy_cudakernel.cu` |
| [HDOverlap/test.sh](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/HDOverlap/test.sh#L1-L6) | 实验设计：6 个规模（1024000 → 102400000）依次运行 |
| [HDOverlap/results.txt](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/HDOverlap/results.txt#L1-L21) | fornax 集群上的实测转录，本讲的"云实验数据" |

## 4. 核心概念与源码讲解

### 4.1 模块一：axpy_cuda_normal——阻塞 cudaMemcpy 路径

#### 4.1.1 概念说明

这个函数是"反模式"一侧的样本：用阻塞的 `cudaMemcpy` 把 `x`、`y` 两个向量从主机搬到设备。阻塞语义意味着两次调用期间主机线程完全空等——它既不能准备下一批数据，也不能做任何计算。整个函数仍然沿用 u2-l3 讲过的五段式骨架，只是这里**被计时的只有中间的 H2D 搬运一段**。

#### 4.1.2 核心流程

```text
axpy_cuda_normal(x, y, n, a):
    cudaMalloc(d_x), cudaMalloc(d_y)        # 分配（不在计时窗口内）
    t0 = read_timer_ms()                     # ── 计时开始
    cudaMemcpy(d_x <- x, H2D)                # 阻塞：返回时 x 已在设备
    cudaMemcpy(d_y <- y, H2D)                # 阻塞：返回时 y 已在设备
    t = read_timer_ms() - t0                 # ── 计时结束（只覆盖两次 H2D）
    kernel<<<(n+255)/256, 256>>>(d_x, d_y)   # 不计时
    cudaDeviceSynchronize()                  # 不计时
    cudaMemcpy(y <- d_y, D2H)                # 不计时
    cudaFree × 2                             # 不计时
    return t                                 # 返回值 = 两次阻塞 H2D 的墙钟
```

对 pageable 内存来说，阻塞 `cudaMemcpy` 返回时拷贝已完成，所以这段计时**大致就是真实的 H2D 传输时间**（含驱动中转开销）。搬运数据量为 \(2n \times 8\) 字节（两个 double 向量），有效带宽约为

\[ B_{\mathrm{H2D}} = \frac{2n \times 8\ \text{字节}}{t_{\text{ms}} \times 1000} \ \text{GB/s} \]

#### 4.1.3 源码精读

计时窗口的开与关紧贴两次阻塞拷贝，见 [HDOverlap/axpy_cudakernel.cu:L44-L62](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/HDOverlap/axpy_cudakernel.cu#L44-L62)。关键三行：

```c
double time = read_timer_ms();                                              // L48 计时开始
cudaMemcpy(d_x, x, n*sizeof(REAL), cudaMemcpyHostToDevice);                // L50 阻塞搬运 x
cudaMemcpy(d_y, y, n*sizeof(REAL), cudaMemcpyHostToDevice);                // L51 阻塞搬运 y
time = read_timer_ms() - time;                                             // L52 计时结束
```

- [HDOverlap/axpy_cudakernel.cu:L36-L42](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/HDOverlap/axpy_cudakernel.cu#L36-L42)：kernel 本体，守卫条件是 `if (i > 0 && i < n)`——注意它**从 1 开始**。这与串行版 `for (i = 1; i < n; ++i)`（[HDOverlap/axpy_cudakernel.cu:L104-L110](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/HDOverlap/axpy_cudakernel.cu#L104-L110)）刻意对齐：两侧都不修改 `y[0]`，保证对照范围一致。
- [HDOverlap/axpy_cudakernel.cu:L55-L58](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/HDOverlap/axpy_cudakernel.cu#L55-L58)：kernel 启动、同步、D2H 拷回都在计时窗口**之外**——所以 main 打印的 `time` 不含计算与拷回，这一点与 CoMem_AXPY（整个包装函数计时）不同，读数时不能混淆。

#### 4.1.4 代码实践

**实践目标**：用 results.txt 里 normal 路径的数据估算 H2D 有效带宽，检验"阻塞拷贝耗时与数据量成正比、但带宽远低于 PCIe 上限"的说法。

**操作步骤**：

1. 阅读 [HDOverlap/results.txt:L5-L21](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/HDOverlap/results.txt#L5-L21)，取 6 个规模的 `axpy(n): time` 数值。
2. 对每个规模计算 \( B = 2n \times 8 / (t \times 1000) \) GB/s。例如 n=102400000、t=2395.00ms 时，搬运 \(2 \times 102400000 \times 8 \approx 1.64\) GB，带宽约 \(1.64 / 2.395 \approx 0.68\) GB/s。
3. 若本机有 GPU：`make` 后按 [HDOverlap/test.sh:L1-L6](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/HDOverlap/test.sh#L1-L6) 逐个规模运行，把自己的数字填进同一张表。

**需要观察的现象**：耗时随 n 大致线性增长（13.30ms → 2395.00ms，n 增大 100 倍耗时增大约 180 倍）；换算出的"带宽"只有零点几 GB/s 量级，远低于 PCIe 3.0 x16 的十余 GB/s。

**预期结果**：线性关系成立说明计时确实反映了搬运本身；带宽偏低则提示 pageable 内存中转、共享集群的负载等都在稀释有效带宽。fornax 数据的具体数值**待本地验证**——请在自己的机器上复测后再下结论。

#### 4.1.5 小练习与答案

**练习 1**：`cudaMemcpy` 返回之后，主机 CPU 在搬运上还花了时间吗？
**答案**：没有了。阻塞语义保证返回时拷贝已完成，主机的等待发生在调用内部、返回之前。

**练习 2**：main 打印的 `axpy(n): time` 包含 kernel 计算和 D2H 拷回吗？
**答案**：不包含。计时窗口只在两次 H2D `cudaMemcpy` 之间（L48–L52），kernel、`cudaDeviceSynchronize`、D2H、`cudaFree` 都在窗口外。

**练习 3**：判断题——把 `cudaMemcpy` 换成 `cudaMemcpyAsync` 但仍用 pageable 内存，主机就能在搬运期间做任意计算。
**答案**：错。pageable 内存下驱动需同步中转（见 2.4 节），入队调用本身仍要花与数据量成比例的 CPU 时间；真异步要求 pinned 内存。

### 4.2 模块二：axpy_cuda_async——cudaMemcpyAsync + stream 路径（含计时缺陷审查）

#### 4.2.1 概念说明

这是"优化技术"一侧的样本：先 `cudaStreamCreate` 建一条流，再把两次 H2D 拷贝用 `cudaMemcpyAsync` 排入流中。设计意图是让拷贝在 DMA 引擎上独立执行、主机入队后立即返回去干别的活。但本基准的**计时实现没有兑现这个实验设计**：计时窗口同样只夹住两个 API 调用，而异步调用返回时传输尚未完成，所以测到的只是"提交任务"的耗时。这个模块的另一半内容，就是作为读者去审查并修复它。

#### 4.2.2 核心流程

```text
axpy_cuda_async(x, y, n, a):
    cudaStreamCreate(stream1)                # 建流（从不销毁）
    cudaMalloc(d_x), cudaMalloc(d_y)
    t0 = read_timer_ms()                     # ── 计时开始
    cudaMemcpyAsync(d_x <- x, H2D, stream1)  # 入队即返回！
    cudaMemcpyAsync(d_y <- y, H2D, stream1)  # 入队即返回！
    t = read_timer_ms() - t0                 # ── 计时结束：只测了"入队"
    kernel<<<...>>>(...)                     # 注意： launches 到默认流，不是 stream1
    cudaDeviceSynchronize()                  # 全设备同步（在计时窗口外）
    cudaMemcpy(y <- d_y, D2H)
    cudaFree × 2
    return t
```

三个要害问题：

1. **计时口径错位**：`t` 是主机发起两次异步拷贝的墙钟，不是传输耗时。事件计时或 `cudaStreamSynchronize(stream1)` 被遗漏在计时窗口之外。
2. **正确性却没坏**：kernel 虽未指派到 stream1，但它落在传统默认流上；传统默认流对其他（阻塞型）流有隐式同步语义——默认流中的操作会等待此前发给所有阻塞流 的操作完成。于是 kernel 仍排在两次拷贝之后执行，随后的 `cudaDeviceSynchronize` 又兜住了 D2H 之前的所有事。**结果对、测量错**，这正是它隐蔽的原因。
3. **流泄漏**：`stream1` 从未 `cudaStreamDestroy`。

#### 4.2.3 源码精读

完整函数见 [HDOverlap/axpy_cudakernel.cu:L64-L90](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/HDOverlap/axpy_cudakernel.cu#L64-L90)。逐段看：

```c
cudaStream_t stream1;
result = cudaStreamCreate(&stream1);                                       // L65-L67 建流
...
double time2 = read_timer_ms();                                            // L72 计时开始
cudaMemcpyAsync(d_x, x, n*sizeof(REAL), cudaMemcpyHostToDevice, stream1);  // L74 入队拷贝 x
cudaMemcpyAsync(d_y, y, n*sizeof(REAL), cudaMemcpyHostToDevice, stream1);  // L75 入队拷贝 y
time2 = read_timer_ms() - time2;                                           // L76 计时结束
```

- [HDOverlap/axpy_cudakernel.cu:L72-L76](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/HDOverlap/axpy_cudakernel.cu#L72-L76)：与 normal 版的 L48–L52 逐行同构，唯一差别是 `cudaMemcpy` → `cudaMemcpyAsync`，并多了流参数。**计时窗口没有任何同步**——这就是缺陷所在。
- [HDOverlap/axpy_cudakernel.cu:L82-L83](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/HDOverlap/axpy_cudakernel.cu#L82-L83)：kernel 以两参数形式 `<<<(n+255)/256, 256>>>` 启动，即运行在**传统默认流**而非 stream1。如上所述，靠默认流的隐式同步保住了正确性。
- [HDOverlap/axpy_cudakernel.cu:L86-L89](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/HDOverlap/axpy_cudakernel.cu#L86-L89)：D2H 拷回、释放、返回——注意函数返回时对 stream1 没做任何等待或销毁。

再对照 [HDOverlap/results.txt:L6-L21](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/HDOverlap/results.txt#L6-L21)：async 的耗时与 normal 几乎相同且随 n 线性增长（13.30/13.30、246.70/243.60、2395.00/2370.10ms）。如果 `t` 真是"纯入队"时间，它应当是微秒级且与 n 无关——实测却与数据量成比例。这与 2.4 节的文档描述吻合：pageable 内存的 H2D 异步拷贝在驱动内部仍要做一次与数据量成比例的中转拷贝，入队调用本身就把这段时间花在了主机侧。所以 fornax 上这两条路径的差别主要来自驱动实现细节，而**不是**"异步加速了传输"。

#### 4.2.4 代码实践（本讲核心实践）

**实践目标**：审查 `axpy_cuda_async` 的计时与同步位置，指出实验设计缺陷，并补上事件计时或流同步，重新对比 normal 与 async 的真实 H2D 传输耗时。

**操作步骤**：

1. **审查**：对照 [HDOverlap/axpy_cudakernel.cu:L72-L76](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/HDOverlap/axpy_cudakernel.cu#L72-L76) 写出结论——计时窗口内只有两次入队调用，窗口在传输完成前就关闭；对照 L82–L86 说明正确性靠默认流隐式同步与 `cudaDeviceSynchronize` 兜底。
2. **修复（示例代码，需自行在副本中试验，不要改动源码文件）**：用 CUDA 事件包裹 stream1 中的拷贝段：

   ```c
   // 示例代码：替换 L72-L76 的计时段
   cudaEvent_t start, stop;
   cudaEventCreate(&start);  cudaEventCreate(&stop);
   cudaEventRecord(start, stream1);                                        // 计时事件排入流
   cudaMemcpyAsync(d_x, x, n*sizeof(REAL), cudaMemcpyHostToDevice, stream1);
   cudaMemcpyAsync(d_y, y, n*sizeof(REAL), cudaMemcpyHostToDevice, stream1);
   cudaEventRecord(stop, stream1);                                         // 记录流内终点
   cudaEventSynchronize(stop);                                             // 主机等到流内工作真正完成
   float ms = 0;  cudaEventElapsedTime(&ms, start, stop);                  // 设备侧口径的传输耗时
   ... // kernel、D2H、cudaFree 照旧；函数末尾 cudaStreamDestroy(stream1)
   ```

   事件记录在流内，测得的是"从拷贝开始到拷贝完成"的 GPU 侧时间；`cudaEventSynchronize` 保证读数前工作已结束。也可以采用更简单的方案：在 L76 之后、关计时器之前插入 `cudaStreamSynchronize(stream1);`。
3. **进阶对照（示例代码）**：把主机缓冲区换成 pinned 内存（`cudaHostAlloc((void**)&x, n*sizeof(REAL), cudaHostAllocDefault)`，结束处 `cudaHostFree`），再测一次 async 的事件耗时，以及"入队调用本身的返回耗时"（`read_timer_ms()` 夹住两次 `cudaMemcpyAsync` 即可，不做任何等待）。
4. 按 [HDOverlap/test.sh](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/HDOverlap/test.sh#L1-L6) 的规模序列运行修正版，制作「normal / async(pageable) / async(pinned)」三列对照表。

**需要观察的现象**：

- 修正后 pageable+async 的事件耗时与 normal 相近（传输量一样）；
- pinned+async 的**入队返回耗时**骤降为微秒级、与 n 基本无关——这才是"主机被解放"的直接证据；
- pinned+async 的事件耗时（纯传输）通常还比 pageable 更快（省去中转）。

**预期结果**：若以上三条都成立，则 README 中"用 cudaMemcpyAsync 加速传输"的说法应修正为——加速的前提是 pinned 内存，收益的本体是主机时间与搬运时间的重叠（以及更高的拷贝引擎效率），而非异步调用本身更快。本机无 GPU 时以上均为**待本地验证**项；也可以用 `nvprof`（或新版 `nsys`/`ncu`）的 Memcpy 行核对事件计时，方法见第 u1-l4 讲。

#### 4.2.5 小练习与答案

**练习 1**：`axpy_cuda_async` 返回的 `time2` 测量的是什么？为什么它不能当传输耗时用？
**答案**：测量的是主机执行两次 `cudaMemcpyAsync` 调用（含 pageable 内存下的驱动中转）的墙钟。异步语义下调用返回不保证传输完成，且随后没有任何流同步被纳入窗口，所以它既不是设备侧传输时间，也不能证明重叠发生。

**练习 2**：kernel 没有排入 stream1，为什么程序结果仍然正确？
**答案**：它落在传统默认流，而传统默认流与所有阻塞型流之间存在隐式同步：默认流里的 kernel 会等此前发往 stream1 的两次拷贝完成才开始；其后的 `cudaDeviceSynchronize` 再保证 D2H 拷回发生在 kernel 之后。

**练习 3**：要让异步拷贝真正与主机计算重叠，除了用 `cudaMemcpyAsync` 还必须做什么？
**答案**：用 `cudaMallocHost`/`cudaHostAlloc` 分配页锁定主机内存（否则 pageable 中转把代价留在主机侧），并在需要结果处显式 `cudaStreamSynchronize`（或事件等待）。

### 4.3 模块三：实验组织与计时口径——main、results.txt 与两个隐藏缺陷

#### 4.3.1 概念说明

前两个模块看清了"被测对象"，这个模块看清"实验怎么组织、数字怎么读"。它回答三件事：main 如何安排 warmup 与多轮平均；`results.txt` 里那两个与 n 无关的 checksum 是怎么来的；以及代码里一个会影响所有打印数字的未初始化变量缺陷。学会这三件事，才能对本基准的结论做可信度评估——这正是微基准读者的必备功。

#### 4.3.2 核心流程

```text
main:
    分配 x, y, y_cuda, y_cuda_async；同一随机源初始化，memcpy 保证四者同源
    串行基线：10 次 axpy(x, y)（计时外，作参照）
    warmup：各调用 normal、async 一次（消化首次 CUDA 上下文等一次性开销）
    计时：10 次 axpy_cuda_normal 求和 → elapsed；10 次 axpy_cuda_async 求和 → elapsed1
    check(y_cuda, y) 与 check(y_cuda_async, y) → 打印 checksum 与两个平均时间
```

#### 4.3.3 源码精读

- [HDOverlap/axpy_cudakernel.cu:L146-L152](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/HDOverlap/axpy_cudakernel.cu#L146-L152)：串行 10 次累加进 `y`；随后 warmup 调用 normal 与 async——注意两者都写在 `y_cuda_async` 上。
- [HDOverlap/axpy_cudakernel.cu:L155-L161](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/HDOverlap/axpy_cudakernel.cu#L155-L161)：计时循环。**缺陷一**：`double elapsed;` 与 `double elapsed1;` 未初始化就执行 `elapsed += ...`（注释里被划掉的 `// = read_timer_ms();` 正是原本的初始化）。这是未定义行为，打印出的两个 time 从一个随机初值起步——碰巧为 0 时数字才"看起来正常"。
- [HDOverlap/axpy_cudakernel.cu:L163-L167](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/HDOverlap/axpy_cudakernel.cu#L163-L167)：**缺陷二（口径）**：`y_cuda` 从头到尾没有被任何 CUDA 函数写过（它只在 L142 被初始化、被复制），`check(y_cuda, y)` 拿"初始随机向量"对比"串行累加 10 次后的 y"；而 `y_cuda_async` 上叠了 2 次 warmup + 10 次 normal + 10 次 async 共 22 次 kernel。两次 check 的对象都算不上公平对照，checksum 只能作结构探针（与 u2-l4 的结论同型）。
- [HDOverlap/results.txt:L5-L21](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/HDOverlap/results.txt#L5-L21)：两个 checksum（0.99919 / 1.19903）在所有规模下恒定——因为 check 是差值和与绝对值和的**比值**：记 \(S=\sum|x_i|\)、初始 \(I=\sum|y_i|\)，两侧近似有 \(\text{checksum}_1 \approx \frac{10aS}{I+10aS}\)、\(\text{checksum}_2 \approx \frac{22aS-10aS}{I+10aS}=\frac{12aS}{I+10aS}\)，其中 \(a=123.456\)、\(S\approx I\approx n/2\)，比值与 n 无关，且数值恰与 0.999 / 1.199 吻合。这印证了 checksum 反映的是**调用次数的结构**，不是数值精度。

#### 4.3.4 代码实践

**实践目标**：验证两个缺陷的存在与影响。

**操作步骤**：

1. **未初始化累加**：阅读 [HDOverlap/axpy_cudakernel.cu:L155](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/HDOverlap/axpy_cudakernel.cu#L155) 与 L159。在本地副本中先原样编译运行一次记录输出；再把两行改为 `double elapsed = 0;`、`double elapsed1 = 0;` 重新编译运行，对比两次打印的 time 是否变化。
2. **checksum 结构**：不改代码，直接用 4.3.3 的公式代入选 4 个规模，预测 checksum，再对照 [HDOverlap/results.txt](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/HDOverlap/results.txt#L5-L21) 验证恒定性。

**需要观察的现象**：步骤 1 中若两次输出一致，说明该机器上未初始化值恰好是 0（常见但不保证）；步骤 2 中预测值与实测的 0.99919 / 1.19903 偏差应在百分之一量级内。

**预期结果**：确认 elapsed 未初始化是真实缺陷（依赖运行环境的运气）；确认 checksum 与 n 无关且可用调用次数结构解释。无 GPU 环境时步骤 1 **待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 6 个规模下 checksum 完全不变？
**答案**：check 返回的是归一化比值，分子分母都随 n 线性增长，比值只依赖累计次数与 \(a\) 的相对大小（见 4.3.3 的推导），与 n 无关。

**练习 2**：`check(y_cuda, y)` 想对比的应该是什么？实际对比的是什么？
**答案**：本意应是"CUDA 结果 vs 串行结果"；实际是"从未上过 GPU 的初始向量 vs 串行累加 10 次的结果"，两侧累计次数与起点都不对齐，只能当结构性探针读。

**练习 3**：`main` 结尾释放了哪些缓冲？漏了谁？
**答案**：释放了 `y_cuda`、`y`、`x`（L171–L173），漏掉了 `y_cuda_async`；另外 `axpy_cuda_async` 里创建的 `stream1` 也从未销毁。对本基准无碍，但属工程瑕疵。

## 5. 综合实践

把本讲三个模块串起来，做一次「修正版 HDOverlap 小实验」：

1. **复制骨架**：把 `HDOverlap` 目录复制为自己的实验目录（保持源码不动），保留 [HDOverlap/Makefile](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/HDOverlap/Makefile#L1-L3) 与 [HDOverlap/test.sh](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/HDOverlap/test.sh#L1-L6) 的规模序列。
2. **修口径**：按 4.2.4 引入事件计时替换两处 `read_timer_ms` 夹逼，并修掉 `elapsed`/`elapsed1` 未初始化问题。
3. **加一臂**：新增 pinned 内存 + 异步拷贝的第三条路径，并在两次 `cudaMemcpyAsync` 之后、等待之前插入一段主机端工作（例如对 `y` 的主机副本做一次 `axpy` 串行计算），度量"入队返回耗时"与"事件耗时"两个口径。
4. **采数与解读**：对 test.sh 的 6 个规模产出四列表——normal(pageable)、async(pageable)、async(pinned) 的事件耗时、async(pinned) 的入队返回耗时；用 `nvprof`（或 `nsys`）的 Memcpy 时间核对事件口径。
5. **一页报告**：回答三个问题——(a) pageable 下 async 相对 normal 有无真实收益？(b) pinned 把收益体现在哪个口径上？(c) 与 [HDOverlap/results.txt](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/HDOverlap/results.txt#L1-L21) 的 fornax 结论是否一致，若不一致列出机器差异因素（PCIe 代际、驱动、是否共享节点）。

无 GPU 环境时，第 1–2 步的代码改写与第 5 步 (c) 的对照仍可完成，数值部分标注**待本地验证**。

## 6. 本讲小结

- `cudaMemcpy` 阻塞：返回即完成，主机在搬运期间空等；`cudaMemcpyAsync` 异步：返回仅代表任务入队，需要流同步或事件等待才能确认完成。
- `axpy_cuda_normal` 的计时窗口只覆盖两次阻塞 H2D 拷贝，测到的近似真实传输时间；数据量为 \(2n \times 8\) 字节，fornax 实测带宽仅零点几 GB/s 量级。
- `axpy_cuda_async` 的计时窗口在传输完成前就关闭，测到的是"入队（含 pageable 中转）"耗时；正确性靠传统默认流与 stream1 的隐式同步及 `cudaDeviceSynchronize` 兜底——结果对、测量错。
- pageable 内存下异步拷贝的入队调用仍与数据量成比例（fornax 上 async ≈ normal 且同步线性增长）；真异步与主机解放的前提是 pinned 内存。
- 修复方案：`cudaEventRecord` + `cudaEventSynchronize` + `cudaEventElapsedTime` 取设备侧口径，或在窗口内 `cudaStreamSynchronize`。
- 附带发现两处工程缺陷：`elapsed`/`elapsed1` 未初始化累加（未定义行为）；`y_cuda` 从未上过 GPU 却参与 check，checksum 只是调用次数结构的探针（0.99919 / 1.19903 与推导吻合）。

## 7. 下一步学习建议

- 下一讲 u5-l2（ReadOnlyMem 1D 纹理）继续"CPU-GPU 搬运"主题的另一个方向：减少搬运代价之外，还可以让只读数据走有缓存的纹理通路。
- 想深入 stream 的并发与依赖编排（多流、事件依赖、页锁定内存的异步拷贝），回顾 u3-l3（Conkernels）——那里 8 条流 + 事件 join 的用法比本讲更完整。
- 建议延伸阅读 CUDA C++ Programming Guide 中 «Asynchronous Simultaneous Execution» 与 «Data Transfer» 两节，核对 2.4 节引述的 pageable/pinned 行为，并以本机 CUDA 版本为准。
- 源码层面可对照阅读 [Conkernels/concurrentKernels.cu](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Conkernels/concurrentKernels.cu#L1-L1) 中 `cudaHostAlloc` + `cudaMemcpyAsync` 的成对用法，看"正确打开方式"长什么样。
