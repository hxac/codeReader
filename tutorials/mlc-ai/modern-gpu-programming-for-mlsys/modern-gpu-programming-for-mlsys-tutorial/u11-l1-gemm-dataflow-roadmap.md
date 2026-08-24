# u11-l1 GEMM 约定、数据路径与优化路线图

## 1. 本讲目标

本讲是 GEMM 系列（单元十一至十三）的开篇导航。学完后你应该能够：

1. 写出本书 GEMM 的维度约定（\(A: M\times K\)、\(B: N\times K\)、\(D: M\times N\)，即 \(D=AB^{\top}\)）并解释为什么 \(B\) 按 \(N\times K\) 存储。
2. 用 TFLOPS 公式 \(\text{TFLOPS} = \frac{2MNK}{t \times 10^{12}}\) 从耗时换算吞吐。
3. 按顺序描述 Blackwell GEMM 的数据路径 GMEM → SMEM → TMEM → 寄存器 → GMEM，说出每一跳由哪个硬件单元执行。
4. 列出 Step 1–9 每一步引入的机制，并标注它主要改变了 scope / layout / dispatch 三要素中的哪一个。
5. 读懂九步优化的端到端性能表（70 ms → 0.094 ms，对齐 cuBLAS），并把每段增益归因到具体机制。

本讲不逐行精读任何内核——那是后续八讲的事。本讲给你一张「地图」：约定怎么读、数据怎么流、九步怎么排。

## 2. 前置知识

本讲默认你已完成单元一、二、三和单元九。用到的旧知识快速回顾：

- **GEMM（GEneral Matrix Multiply）**：稠密矩阵乘法。线性层、attention 投影、大量卷积实现最终都落到它，通常占据 GPU 执行时间的大头——这就是为什么本书用整整三个单元打磨它。
- **四种存储空间**（u2-l2）：GMEM（HBM 显存，全 device 可见）、SMEM（CTA 私有低延迟暂存）、TMEM（CTA 内 128 lane × 512 列的片上空间，专存 MMA 累加器）、RF（每线程私有寄存器）。
- **三个引擎**（u2-l3）：CUDA Core（标量/向量与控制流）、Tensor Core（tile 级矩阵乘累加，Blackwell 上即 `tcgen05.mma`）、TMA 引擎（异步整块搬运）。一个 GEMM tile 的生命期分 Load → Compute → Epilogue 三段。
- **三要素**（u9-l3）：每个 tile 操作由 **scope**（哪些线程执行）、**layout**（数据怎么摆）、**dispatch**（走哪条硬件路径）刻画。本讲会发现：GEMM 章节的每个 Step 都自带一个「execution structure」框，正是用三要素记录该步改了什么。
- **优化阶梯**（u3-l3）：更好的算法 → 更高并行 → 更合适的引擎 → 重叠与资源调优。九步优化是这条阶梯的实例：前四步（并行 + TMA）约 142×，后五步（流水线、持久调度、warp 特化、cluster、多消费者）再约 5×。
- **roofline**（u3-l1）：方阵 GEMM 的算术强度为 \(N/3\)，远高于 B200 约 250 FLOP/byte 的拐点，属 compute-bound——所以 GEMM 优化的主战场是「让 Tensor Core 别闲着」，而不是省带宽。

不熟悉的前置不必慌：本讲只用结论，不用推导；推导在对应的旧讲义里。

## 3. 本讲源码地图

本书是「一章一目录」的 Sphinx 教材（u1-l2），GEMM 主题分布在三个章节目录中，另有生成插图的脚本：

| 文件 | 作用 |
| --- | --- |
| `chapter_gemm_basics/index.md` | GEMM 基础章：维度约定、TFLOPS、数据路径图、优化路线总述，以及 Step 1–3 三个完整内核 |
| `img/scripts/gen_memory_dataflow.py` | 生成 `img/memory_dataflow.png`（数据路径图）的 matplotlib 脚本，用数据结构显式编码了六站五跳的规范数据路径 |
| `chapter_gemm_async/index.md` | 异步章：Step 4（TMA）、Step 5（软件流水线）、Step 6（持久内核） |
| `chapter_gemm_advanced/index.md` | 高级章：Step 7（warp 特化）、Step 8（双 CTA cluster）、Step 9（多消费者）与端到端性能表 |
| `img/scripts/gen_gemm_perf.py` | 生成九步性能柱状图的脚本，综合实践会参考它的画法 |

后续引用统一用「基础章 / 异步章 / 高级章」指代前三个文件。

## 4. 核心概念与源码讲解

本讲的三个最小模块：**GEMM 约定**、**Blackwell 数据路径**、**九步优化路线**。

### 4.1 GEMM 约定：维度、精度与 TFLOPS

#### 4.1.1 概念说明

优化之前必须先统一「题目」。GEMM 有两种常见写法：\(D = A \times B\) 和 \(D = A \times B^{\top}\)。二者在数学上只差一个转置，但**内核里读哪个方向的内存**完全不同。本书全系列采用后者：

- \(A\) 形状 \(M \times K\)；
- \(B\) 形状 \(N \times K\)；
- \(D\) 形状 \(M \times N\)；
- \[ D[m,n] = \sum_k A[m,k] \cdot B[n,k] \]

为什么 \(B\) 存成 \(N \times K\)（行是输出列、列是缩减维）？因为这是线性层权重最常见的落盘布局，内核直接读 \(B[n,k]\)，**运行时不做任何转置或重排**。精度约定：A、B、D 用 fp16 存，MMA 沿 K 用 fp32 累加——累加次数随 K 增长，fp32 能压住累积舍入误差。

性能度量用 TFLOPS（每秒万亿次浮点运算）。注意计数规则：一次乘和一次加各算 1 个浮点运算， fused multiply-add（FMA）算 2 个。所以总运算量是 \(2MNK\)，而不是 \(MNK\)。

#### 4.1.2 核心流程

从耗时换算吞吐只需一步：

\[\text{TFLOPS} = \frac{2 \times M \times N \times K}{t_{\text{seconds}} \times 10^{12}}\]

速算技巧：问题规模固定时，TFLOPS 与耗时成反比，**加速比就是耗时之比**。以全书基准规模 \(M=N=K=4096\) 为例：

- 总运算量 \(2 \times 4096^3 = 2^{37} \approx 1.374 \times 10^{11}\) FLOP；
- 若耗时 0.094 ms，吞吐约 \(1.374\times10^{11} / (9.4\times10^{-5} \times 10^{12}) \approx 1462\) TFLOPS——对照 u3-l1 的 B200 算力屋顶（约 2 PFLOP/s），利用率约 73%；
- 若耗时 70 ms，吞吐只有约 2 TFLOPS。同样的问题、同样的算术强度，相差约 744 倍——这正是 u3-l1 说过的「roofline 只给上限不保证达到」的极端例证。

#### 4.1.3 源码精读

约定与公式写在基础章开头：

- [chapter_gemm_basics/index.md:L20-L35](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L20-L35) —「## GEMM」小节。依次给出：四条维度约定与逐元素定义；解释 \(B\) 存 \(N\times K\) 等价于 \(D=AB^{\top}\) 且运行时不转置；声明 fp16 存储、fp32 累加；最后给出 TFLOPS 公式。

计时脚手架里的公式落到了一行真实代码：

- [chapter_gemm_basics/index.md:L306-L320](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L306-L320) — 可选计时段：先跑 3 次预热，再用 CUDA events 包住 10 次迭代取平均，最后 `tflops = 2 * M * N * K / ms / 1e9`。分母是 `1e9` 而不是 `1e12`，因为 `ms` 本身是毫秒——\(10^{-3} \times 10^{12} = 10^{9}\)，单位换算被合并进了一个常数。读开源代码遇到「差三个数量级」的常数时，先查单位。

同一个脚手架还包含两条纪律，值得现在就记住：

- [chapter_gemm_basics/index.md:L276](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L276) — 每个 Python 会话只编译一个 step：各步示例复用内部名，编译器持有跨调用的会话状态，换个 step 要先重启。
- [chapter_gemm_basics/index.md:L323-L326](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L323-L326) — 这个短计时循环只够「冒烟测量」；正式报告要遵循基准测试附录（对应本手册 u15-l4）的完整协议。

#### 4.1.4 代码实践

**实践目标**：用 TFLOPS 公式亲手复算性能表，建立「数字敏感度」。

**操作步骤**（示例代码，非项目原有）：

```python
# tflops_check.py —— 复算九步性能表中各版本的吞吐（示例代码）
M = N = K = 4096
flop = 2 * M * N * K            # = 2**37 ≈ 1.374e11

for name, ms in [("Step 1", 70.0), ("Step 3", 53.6), ("Step 4", 0.49),
                 ("Step 7", 0.23), ("Step 8", 0.104), ("Step 9", 0.094)]:
    tflops = flop / (ms * 1e-3) / 1e12
    print(f"{name:7s} {ms:8.3f} ms -> {tflops:8.1f} TFLOPS")
```

**需要观察的现象**：Step 1 与 Step 9 的 TFLOPS 相差多少倍？Step 9 的吞吐占 2 PFLOP/s 屋顶的百分之多少？

**预期结果**：Step 1 约 2.0 TFLOPS，Step 9 约 1462 TFLOPS，相差约 744 倍；Step 9 约达稠密 fp16 算力屋顶的 73%。若把你的数字与高级章性能表（见 4.3.3）的加速比列对照，两者应一致（因为规模固定时加速比 = 耗时反比）。本脚本纯 CPU 计算，无需 GPU；与 B200 实测的精确对齐则**待本地验证**（须有 Blackwell GPU 并按附录协议计时）。

#### 4.1.5 小练习与答案

**练习 1**：为什么总运算量是 \(2MNK\) 而不是 \(MNK\)？

**答案**：\(D[m,n]\) 是 \(K\) 个乘积之和，每个乘积是「一次乘 + 一次加」。按本书计数规则，乘、加各计 1 FLOP，所以每个 \((m,n)\) 输出元素贡献 \(2K\)，共 \(MN\) 个元素，总计 \(2MNK\)。

**练习 2**：保持 \(M=N=K=4096\) 不变，某内核耗时从 0.49 ms 降到 0.23 ms，加速比是多少？TFLOPS 变为多少？

**答案**：加速比 = \(0.49 / 0.23 \approx 2.13\times\)；TFLOPS \(= 1.374\times10^{11} / (2.3\times10^{-4} \times 10^{12}) \approx 597\) TFLOPS。规模固定时直接用耗时反比即可，不必重算分子。

**练习 3**：把 \(B\) 存成 \(N \times K\) 而不是 \(K \times N\)，内核代码里省掉了什么？

**答案**：省掉了运行时转置/重排——内核按 \(B[n,k]\) 直接寻址即可参与 \(D[m,n] = \sum_k A[m,k] B[n,k]\) 的计算。数学上这等价于计算 \(AB^{\top}\)，但「转置」只发生在纸面上，数据在 GMEM 中一字节不动。

### 4.2 Blackwell 数据路径：GMEM → SMEM → TMEM → RF → GMEM

#### 4.2.1 概念说明

**数据路径（data path）** 指一个 tile 从输入到输出依次经过的存储空间序列，以及每一跳的执行者。它为什么值得单独立一节？因为基础章原话说得很直白：后续每一步优化**改变的是路径上某一步的执行方式，而路径本身不变**。把路径背下来，后面八讲出现的所有新机制（TMA、双缓冲、流水线、角色划分……）都能立刻定位到「它优化的是哪一跳」。

规范数据路径有六个站点、五条边：

```text
GMEM --TMA load--> SMEM --tcgen05 MMA--> TMEM --tcgen05.ld--> RF
     --thread write--> SMEM --TMA store--> GMEM
```

逐边读：

| 边 | 执行者 | 方向 | 说明 |
| --- | --- | --- | --- |
| TMA load | TMA 引擎 | GMEM → SMEM | 单线程发起，引擎搬运整块 tile（u6） |
| tcgen05 MMA | Tensor Core | SMEM → TMEM | 从 SMEM 描述符读 A/B，累加器写 TMEM（u7-l1） |
| tcgen05.ld | Tensor Core 通路 | TMEM → RF | warp 集体读回，异步、需 `wait::ld`（u7-l4） |
| thread write | CUDA Core | RF → SMEM | 线程把寄存器里的结果写回 SMEM 中转 |
| TMA store | TMA 引擎 | SMEM → GMEM | 整块写回，用 commit/wait group 追踪（u6-l3） |

最后两跳合称 **epilogue（尾声）**：把 TMEM 里的 fp32 累加结果读进寄存器、转成输出 dtype、再送回 GMEM。基础章正文把它压缩成一句「reads the result from TMEM into registers and stores it to GMEM」，而插图脚本保留了 SMEM 中转的完整两跳——后者的形态是后续优化内核实际使用的（TMA store 需要数据先在 SMEM 里）。**读教材时要留意这种「正文简写 vs 图示全貌」的差异**，两处都对，只是详略不同。

#### 4.2.2 核心流程

把数据路径与三要素对应起来看：

1. **站点即存储空间**：GMEM/SMEM/TMEM/RF 各是一个 scope 层级上的物理资源（u2-l2）。
2. **边即 dispatch 选择**：GMEM↔SMEM 走 TMA 还是走线程自拷贝，是 dispatch 的差异——站点相同、载具不同（Step 1 vs Step 4 的全部区别就在这条边上）。
3. **站点内的摆放即 layout**：A/B tile 在 SMEM 里按 128B swizzle 排布，累加器在 TMEM 里按 TLane/TCol 命名轴展开（u4、u10）。
4. **重叠的前提是路径分段**：load、compute、epilogue 天然使用不同引擎、不同存储，互不写对方的数据——这是 u2-l3 三段式流水线能重叠的物理基础，也是 Step 5/7 要兑现的潜力。

#### 4.2.3 源码精读

数据路径的正确定义在基础章：

- [chapter_gemm_basics/index.md:L37-L43](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L37-L43) —「### GEMM Data Path」小节：内核只做两类工作（在存储空间之间搬 tile、用 tile 计算），插图从左到右跟随数据；明确「后续优化改变某一步的执行方式，路径本身不变」。

插图的「真相源」是脚本，六站五跳全部编码在两个常量里：

- [img/scripts/gen_memory_dataflow.py:L1-L10](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_memory_dataflow.py#L1-L10) — 模块 docstring 直接写出规范流程 `GMEM --TMA load--> SMEM --tcgen05 MMA--> TMEM --tcgen05.ld--> RF --thread write--> SMEM --TMA store--> GMEM`，并给出用法。
- [img/scripts/gen_memory_dataflow.py:L18-L33](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_memory_dataflow.py#L18-L33) — `BOXES` 列出六个站点（注意 SMEM、GMEM 各出现两次：一次进、一次出），`EDGE_LABELS` 列出五条边。改这张图不需要动任何绘图代码，只改这两个列表。

再对照 Step 1 内核，看路径如何落到代码。基础章把 Step 1 的数据流总结为五步：

- [chapter_gemm_basics/index.md:L68-L76](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L68-L76) — Allocate / Load / Compute / Write back / Release：分配 SMEM 与 TMEM；128 线程协作把 A、B 从 GMEM 拷入 SMEM；选出单线程发起 MMA 并等 mbarrier；warpgroup 把 TMEM 读进寄存器、转 fp16、按行写回 GMEM；最后释放 TMEM。

三段关键代码（完整精读留给 u11-l2，这里只认站点）：

- [chapter_gemm_basics/index.md:L241-L243](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L241-L243) — Load 段：`Tx.cta.copy` 由全 CTA 线程同步搬运（GMEM → SMEM 的「线程载具」版本），随后 `T.cuda.cta_sync()` 保证写可见。
- [chapter_gemm_basics/index.md:L246-L254](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L246-L254) — Compute 段：warp 0 中 `elect_sync` 选出的单线程发 `Tx.gemm_async` + `tcgen05.commit`，全组在 mbarrier 上等完成（SMEM → TMEM）。
- [chapter_gemm_basics/index.md:L257-L265](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L257-L265) — Writeback 段：`Tx.wg.copy_async`（即 `tcgen05.ld`）把 TMEM 读进寄存器，`Tx.cast` 转 fp16 后 `Tx.copy` 写 GMEM（TMEM → RF → GMEM 的简版 epilogue，未经过 SMEM 中转）。

注意一个细节：Step 1 的回写是 RF 直写 GMEM，没有走插图里的「thread write → SMEM → TMA store」两跳。**站点序列在简版与完整版之间一致（TMEM → RF → GMEM），差的是最后一段的载具与中转**——这正好再次印证「优化改变执行方式，不改变路径」。

#### 4.2.4 代码实践

**实践目标**：亲手重生成数据路径图，并通过修改数据结构理解「图 = 数据 + 绘制」的组织方式。

**操作步骤**：

1. 在仓库根目录运行 `python img/scripts/gen_memory_dataflow.py`（需 matplotlib，无需 GPU / tvm）。
2. 确认输出 `Wrote .../img/memory_dataflow.png`，打开与书中插图比对。
3. 做一个思想实验再上机验证：把 `EDGE_LABELS` 第一项 `"TMA load"` 改成 `"Tx.cta.copy (Step 1-3)"`（示例修改），重跑脚本，观察只有第一条箭头的标签与颜色变化——站点图完全不动。

**需要观察的现象**：改 `BOXES` 会改站点数量与布局（箭头数自动跟随 `len(BOXES)-1`），改 `EDGE_LABELS` 只改边的文字与配色（配色表见 [img/scripts/gen_memory_dataflow.py:L44-L50](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_memory_dataflow.py#L44-L50)）。

**预期结果**：脚本输出与仓库中 `img/memory_dataflow.png` 一致的图；步骤 3 的修改只影响第一条边的标注。运行效果**待本地验证**（需要安装 matplotlib：`pip install matplotlib`）。

#### 4.2.5 小练习与答案

**练习 1**：规范数据路径中 SMEM 为什么出现两次？分别服务于什么？

**答案**：第一次在入口侧，作 A/B 操作数 tile 的低延迟暂存区（TMA load 的目的地、MMA 的源）；第二次在出口侧，作 epilogue 写回 GMEM 前的中转（线程把寄存器结果写入，TMA 引擎再整块 store 出去）。两次角色不同，物理上都是同一个 CTA 私有的共享内存。

**练习 2**：路径上有两条边都由 TMA 引擎执行，它们的完成检测机制相同吗？

**答案**：不同。load 方向用 mbarrier 的字节计数（`arrive.expect_tx` 登记在途字节，引擎每次搬运完成核减，账清零才算完成）；store 方向用 bulk async-group（`commit_group` 打包、`wait_group 0` 排空）判断源缓冲何时可复用。原因是等待的对象不同：load 等「目标可读」，store 等「源缓冲可覆盖」（详见 u6-l3）。

**练习 3**：如果只看「站点序列」，Step 1 与 Step 4 的数据路径有何异同？

**答案**：站点序列完全相同（GMEM → SMEM → TMEM → RF → GMEM）。不同的是第一条边的载具：Step 1 用全部 128 线程同步执行 `Tx.cta.copy`，Step 4 改为单线程发起、TMA 引擎搬运。这就是「dispatch 变了、路径没变」。

### 4.3 九步优化路线：每一步改变什么机制

#### 4.3.1 概念说明

基础章开篇说明了教学策略：数据搬运、K 累加、空间分块、Tensor Core 调度如果一次性全塞进一个内核，出错就没法定位。所以内核**一次只加一个机制**，每个旧版本保留为参照。九步的完整路线是：

- **Step 1–3（基础章，本单元）**：搭正确性骨架。单 tile 同步内核 → K 循环累加 → 空间分块多 CTA。做完这三步，内核能算任意规模的正确结果，但性能还不是目标。
- **Step 4–6（异步章，单元十二）**：异步与调度。TMA 异步搬运 → 双缓冲软件流水线 → 持久内核 + tile scheduler。
- **Step 7–9（高级章，单元十三）**：角色与协作。warp 特化（TMA/MMA/回写三角色）→ 双 CTA cluster 协作 MMA → 多消费者 warp 特化。

这条路线有一个非常好用的观察工具：三个章节给每个 Step 都写了「execution structure」框，逐项声明 Scope / Layout / Dispatch 变与不变。这正是 u9-l3 三要素框架在教材里的官方用法——**每一步优化 = 有意识地改变三要素中的一两个、按住其余不动**。

#### 4.3.2 核心流程

把九步整理成一张总表（机制类别按实践任务要求分为数据搬运 / 流水线 / 调度 / 角色划分，骨架步骤单列）：

| Step | 引入机制 | 机制类别 | 主要改变的三要素 | 数据路径上的位置 |
| --- | --- | --- | --- | --- |
| 1 | 单 tile 同步内核（`hgemm_v1`，正确性基线） | 骨架 | —（定义基线） | 全路径走通一遍 |
| 2 | K 循环累加 + 相位翻转 | 骨架 | layout/复用 + 同步：同一 SMEM tile 对与 TMEM 累加槽跨迭代复用，MMA barrier 每轮翻 phase | Compute 段循环化 |
| 3 | 空间分块，2D grid | 调度 | scope：grid 从 1×1 变 `[M/128, N/128]`，每 CTA 一个输出 tile | 全路径按 tile 复制 |
| 4 | TMA 异步加载 | 数据搬运 | dispatch：GMEM→SMEM 由线程同步拷贝改 TMA 引擎 | 第一条边换载具 |
| 5 | 双缓冲软件流水线（PIPE_DEPTH=2） | 流水线 | layout：单 SMEM tile 对变多级环形缓冲，加预取与 stage 复用 | 第一条边与第二条边具备重叠条件 |
| 6 | 持久内核 + tile scheduler | 调度 | scope：固定 CTA 池跨 tile 循环，tile 分配进内核 | 全路径按 tile 串行复用 |
| 7 | warp 特化：TMA/MMA/回写三角色 + 四道屏障 | 角色划分 | scope：一个 warpgroup 串行 → 三类角色并发交接 | 三段路径真正同时忙碌 |
| 8 | 双 CTA cluster 协作 MMA（cta_group=2） | 角色划分 | scope + layout + dispatch 全动：协作范围扩到 CTA 对，A/B 分布两侧 SMEM，累加器横跨两侧 TMEM | 路径站点不变，但横跨两个 CTA |
| 9 | 多消费者：两个 MMA 消费者共享 B tile | 角色划分 | scope + layout：第二个消费者 warp，TMEM 分两段累加器区 | 输出 tile 从 256×256 长到 512×256 |

三个不变量贯穿全程：

1. **数据路径不变**：站点序列始终是 GMEM → SMEM → TMEM → RF → GMEM（Step 8 起同一序列横跨 CTA 对）。
2. **GEMM 数学不变**：始终是 fp16 输入、fp32 累加、fp16 输出的 \(D=AB^{\top}\)。
3. **验证回路不变**：每个 step 都是「编译 → 跑 PyTorch 随机张量 → 与 fp32 参考断言」的同一套脚手架。

#### 4.3.3 源码精读

**路线总述**写在基础章的「Optimization Path」小节，六个机制对应 Step 4–9：

- [chapter_gemm_basics/index.md:L45-L54](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L45-L54) — 逐条列出：TMA 异步搬运（硬件拷贝路径 + barrier 追踪）、软件流水线（多 SMEM stage 让下一 K tile 的搬运与当前 Tensor Core 计算重叠）、持久调度（固定 CTA 池 + tile scheduler）、warp 特化（生产者 / MMA 消费者 / 回写三类角色）、CTA cluster（两 CTA 协作一个更大的 MMA tile）、多消费者（两个 MMA 消费者 warp 共享 stage 好的 B tile）。

**起点为什么是这九步**：Step 1 的四条局限就是路线图的「待办清单」：

- [chapter_gemm_basics/index.md:L330-L336](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L330-L336) — 只能算单个 K tile（→ Step 2 解决）、M/N 被钉死在 128（→ Step 3 解决）、用同步 GMEM→SMEM 拷贝而非 TMA（→ Step 4 解决）、搬运与计算从不重叠（→ Step 5/7 解决）。读优化教材时先收集「局限清单」，后面每一步都能对号入座。

**Step 4–6 的 execution structure**（异步章）：

- [chapter_gemm_async/index.md:L7-L9](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L7-L9) — 章总览三句话，分别概括 Step 4/5/6。
- [chapter_gemm_async/index.md:L21-L24](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L21-L24) — Step 4 structure 框：Scope/Layout 不变，**Dispatch 变**（GMEM→SMEM 加载从同步 `Tx.cta.copy` 改 TMA 引擎）。
- [chapter_gemm_async/index.md:L247-L250](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L247-L250) — Step 5 structure 框：**Layout 变**（单 tile 对变 `PIPE_DEPTH` 级环形缓冲）；此步只建立缓冲结构，完全重叠要等 Step 7 的角色划分。
- [chapter_gemm_async/index.md:L466-L469](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L466-L469) — Step 6 structure 框：**Scope 变**（固定 persistent CTA 池，每个经 scheduler 循环处理多个输出 tile）；Layout/Dispatch 不变。

**Step 7–9 的 execution structure**（高级章）：

- [chapter_gemm_advanced/index.md:L7-L9](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L7-L9) — 章总览：Step 7 三角色 + 四屏障；Step 8 两 CTA 协作 MMA；Step 9 第二个消费者，最终对齐 cuBLAS。
- [chapter_gemm_advanced/index.md:L24-L27](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L24-L27) — Step 7 structure 框：**Scope 变**（一个 warpgroup 的串行 load→MMA→writeback 变三个并发角色，用 full/empty 屏障交接）；Layout/Dispatch 不变。
- [chapter_gemm_advanced/index.md:L330-L333](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L330-L333) — Step 8 structure 框：三要素全动——Scope 扩到 cluster 内两 CTA；Layout 上 A/B 切片驻留两侧 SMEM、累加器横跨两侧 TMEM；Dispatch 用 `cta_group=2` 发协作 MMA、`cta_mask=3` 双侧通知。
- [chapter_gemm_advanced/index.md:L603-L606](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L603-L606) — Step 9 structure 框：Scope 加第二个消费者 warp；Layout 上 A 增加消费者轴、TMEM 分两段累加器区；两组 A 块共享同一批 stage 好的 B tile。

**端到端性能表**（解读归 u13-l4，此处先建立全景）：

- [chapter_gemm_advanced/index.md:L864-L877](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L864-L877) — B200、M=N=K=4096、fp16、锁频、每版本 1000 次迭代：Step 1 70 ms（1×）→ Step 3 53.6 ms（~1.3×）→ Step 4 0.49 ms（~142×）→ Step 7 0.23 ms（~309×）→ Step 8 0.104 ms（~676×）→ Step 9 0.094 ms（~744×）＝ cuBLAS 参考值。Step 2/5/6 是结构性铺垫，表中不单列计时。

粗读这张表有一个容易误读的地方：最大的单段跳变（142×）来自 Step 4 的 TMA，但**Step 3 的多 CTA 并行其实是它的前提**——单 CTA 内核对 4096³ 的问题只能串行扫过 32×32 个 tile，70 ms 里大部分耗在同步线程拷贝上。分段归因要到 u13-l4 结合测量协议细算，这里只需记住：前四步兑现「并行 + 合适引擎」，后五步兑现「重叠 + 角色协作」，对应 u3-l3 优化阶梯的后两级。

#### 4.3.4 代码实践

**实践目标**：把九步路线表变成程序化数据，为综合实践的绘图做准备（也是一次对上表的自我校验）。

**操作步骤**（示例代码，非项目原有）：

```python
# gemm_roadmap.py —— 九步优化路线的数据化（示例代码）
ROADMAP = [
    # (step, 机制, 类别, 改变的三要素, 耗时ms 或 None)
    (1, "单 tile 同步内核（基线）",   "骨架",     "—（基线）",           70.0),
    (2, "K 循环累加 + 相位翻转",      "骨架",     "layout/复用 + 同步",  None),
    (3, "空间分块，2D grid",          "调度",     "scope",               53.6),
    (4, "TMA 异步加载",               "数据搬运", "dispatch",            0.49),
    (5, "双缓冲软件流水线",           "流水线",   "layout",              None),
    (6, "持久内核 + tile scheduler",  "调度",     "scope",               None),
    (7, "warp 特化（三角色四屏障）",   "角色划分", "scope",               0.23),
    (8, "双 CTA cluster 协作 MMA",    "角色划分", "scope+layout+dispatch", 0.104),
    (9, "多消费者共享 B tile",        "角色划分", "scope+layout",        0.094),
]

for step, mech, cat, elem, ms in ROADMAP:
    print(f"Step {step}: {mech}  [类别={cat}, 三要素={elem}, "
          f"耗时={'—' if ms is None else str(ms) + ' ms'})")
```

**需要观察的现象**：类别为「骨架」的步骤（1–2）都没有对应性能跳变的单列计时；「角色划分」类集中在 7–9；三要素中被改变最多的是 scope。

**预期结果**：打印出与 4.3.2 表一致的九行；统计可得 scope 被改变 5 次（Step 3/6/7/8/9）、layout 3 次、dispatch 2 次——scope（谁执行、怎么分工）是九步中最常动的杠杆。数据纯来自书中性能表与各步 structure 框，无需 GPU；耗时的真实性**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：Step 5 已经有了双缓冲，为什么说「完全的 load/compute 重叠要到 Step 7 才到来」？

**答案**：Step 5 只解决**存储冲突**——把单 tile 对变成多级环形缓冲，下一次加载有了独立目的地。但此时仍是一个 warpgroup 串行控制全流程：它发完 TMA 后要等当前 MMA 完成才发起下一次加载，控制流仍集中在一份。Step 7 把 TMA、MMA、回写拆给不同 warp 角色并各配 PipelineState，三段才能各自独立推进、真正并发。

**练习 2**：哪一步第一次改变了「数据路径的站点序列」？为什么？

**答案**：严格说没有任何一步改变站点序列——这正是本讲的核心论断。最接近的是 Step 8：站点仍是 GMEM → SMEM → TMEM → RF → GMEM，但 A/B 切片分布在**两个 CTA 的 SMEM**、累加器横跨**两个 TMEM**，路径整体横跨 CTA 对（借 DSMEM 互访与 `cta_group=2` 协作 MMA）。变化的是路径的「宽度」（协作范围），不是序列本身。

**练习 3**：对照优化阶梯（更好的算法 → 更高并行 → 更合适的引擎 → 重叠与资源调优），Step 3 和 Step 4 分别属于哪一级？

**答案**：Step 3 是「更高并行」——把输出分块映射到二维 grid，用更多 CTA 同时干活；Step 4 是「更合适的引擎」——把 GMEM↔SMEM 搬运从 CUDA Core（线程自拷贝）移交专门的 TMA 引擎。二者合计贡献了表中最大的一段跳变（1.3× → 142× 的量级跃升发生在 Step 4）。

## 5. 综合实践

**任务**：绘制一张「九步优化路线图」，把本讲三个模块的产出合并在一张图上——上方是**不变的 GEMM 数据路径**，下方是**九步各自改变的机制与实测耗时**。

**操作步骤**：

1. 运行 4.3.4 的 `gemm_roadmap.py` 数据；如未做，先把九行数据补齐。
2. 参考两个现成脚本的画法：数据路径横链图看 [img/scripts/gen_memory_dataflow.py:L94-L116](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_memory_dataflow.py#L94-L116)（`main()` 里的 box/arrow 布局），log 轴耗时柱状图看 [img/scripts/gen_gemm_perf.py:L1-L30](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_gemm_perf.py#L1-L30)。
3. 用 matplotlib 画上下两个子图（示例代码骨架）：

```python
# gemm_roadmap_fig.py —— 路线图绘制骨架（示例代码，细节自行补全）
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# 上图：不变的数据路径（照抄 gen_memory_dataflow.py 的 BOXES/EDGE_LABELS 常量）
# 下图：九步柱状 log 轴 + 机制标注，例如：
#   steps  = ["S1\n基线", "S3\n分块", "S4\nTMA", "S7\nwarp特化", "S8\ncluster", "S9\n多消费者"]
#   times  = [70.0, 53.6, 0.49, 0.23, 0.104, 0.094]
#   ax.bar(...); ax.set_yscale("log")
#   每根柱子下方用注释标出：类别（数据搬运/流水线/调度/角色划分）与改变的三要素
#   顶部画一条水平箭头链 GMEM→SMEM→TMEM→RF→GMEM，写明"九步全程不变"
```

4. 有条件的话，把 Step 2/5/6（表中无单列计时的结构性步骤）以文字标注插入对应位置，说明它们铺垫了什么。

**需要观察的现象**：耗时柱在 log 轴上从 70 ms 一路压到 0.094 ms；无论哪根柱子，顶部那条 GMEM→SMEM→TMEM→RF→GMEM 的链条都不变；类别标注会自然聚成「骨架（1–2）→ 调度/搬运（3–4）→ 流水线/调度（5–6）→ 角色划分（7–9）」的带状分布。

**预期结果**：一张自解释的路线图：读者不看正文也能回答「哪一步换了搬运载具（S4）」「哪一步开始三段重叠（S7）」「哪一步把协作范围扩到 CTA 对（S8）」。绘图本身无需 GPU，**待本地验证**（需要 matplotlib）；若想把自己的实测填进图里，则需要 Blackwell GPU 并遵循基准附录协议（u15-l4）。

## 6. 本讲小结

- **约定**：全书 GEMM 统一为 \(D=AB^{\top}\)（\(A: M\times K\)、\(B: N\times K\)、\(D: M\times N\)），\(B\) 按 \(N\times K\) 存储使内核运行时零转置；fp16 输入输出、fp32 累加；性能用 \(\text{TFLOPS} = 2MNK/(t\times10^{12})\) 度量，FMA 计 2 FLOP。
- **数据路径**：GMEM →（TMA load）→ SMEM →（tcgen05 MMA）→ TMEM →（tcgen05.ld）→ RF →（thread write + TMA store，即 epilogue）→ GMEM；六站五跳，每跳有专门执行者。
- **路线**：九步一次只加一个机制——1–3 搭正确性骨架（单 tile、K 循环、空间分块），4–6 换异步与调度（TMA、双缓冲、持久内核），7–9 做角色与协作（warp 特化、双 CTA cluster、多消费者）。
- **三要素视角**：每个 Step 的 execution structure 框都用 scope/layout/dispatch 记录变化——dispatch（S4）、layout（S5）、scope（S3/6/7/9）是最常动的杠杆，S8 三者全动。
- **不变量**：数据路径站点序列、GEMM 数学、编译-验证回路三样东西贯穿九步不变；B200 上 4096³ fp16 从 70 ms 优化到 0.094 ms（~744×），与 cuBLAS 持平。

## 7. 下一步学习建议

下一讲 **u11-l2（Step 1：单 tile 同步内核）**将逐行精读 `hgemm_v1` 的完整内核——本讲 4.2.3 里三段代码的展开版，覆盖 SMEMPool 分配、`elect_sync` 选线程发起 MMA、TMEM 读回与按行写回。建议预先重读 u9-l1（hgemm_v1 首次出现处）与 u9-l3（三要素）。此后按 u11-l3（Step 2 相位翻转）、u11-l4（Step 3 grid 映射）推进本单元；Step 4 起进入单元十二前，可先回看 u6（TMA）与 u8（mbarrier）两单元。想先看终点数据的读者可跳读 [chapter_gemm_advanced/index.md:L864-L877](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L864-L877) 的性能表，带着「每段增益从哪来」的问题回来读 Step 1。
