# 内存空间：GMEM、SMEM、TMEM 与 DSMEM

## 1. 本讲目标

上一讲（u2-l1）我们回答了"计算由谁、以什么规模执行"；本讲回答下一个同样根本的问题：**数据放在哪里**。学完本讲，你应该能够：

1. 说出 GMEM、SMEM、TMEM、寄存器（RF）四种存储空间各自的作用范围、典型容量与用途，并能比较它们的取舍。
2. 解释 Blackwell 为什么新增 Tensor Memory（TMEM）：它的二维结构（128 lane × 最多 512 列 × 32 bit）如何缓解寄存器压力。
3. 说明 DSMEM（分布式共享内存）在 2-CTA cluster 中如何让一个 CTA 直接读取另一个 CTA 的 SMEM，并画出 2-CTA GEMM 中 A/B 切片的数据流向图。
4. 把"内存空间"这个硬件概念与 TIRx 中 buffer 的 `scope` 参数对应起来，为后面读内核源码打基础。

## 2. 前置知识

本讲是概念课，不需要你已经写过 GPU 内核，但需要以下两点背景（均来自上一讲 u2-l1）：

- **线程执行层级**：GPU 线程被组织为 thread → warp（32 线程）→ warpgroup（4 warp / 128 线程）→ CTA → cluster → grid。一个 CTA 驻留在单个 SM 上并拥有该 SM 内一块私有的 shared memory；一个 cluster 由若干（可跨 SM 的）CTA 组成，cluster 内的 CTA 可以互相同步、互相访问对方的 shared memory。
- **操作 scope**：一项操作由哪些线程发起、谁执行、谁受益，称为它的 scope。例如 TMA copy 由单个线程发起，`tcgen05` MMA 也由一个选定线程提交，而完整的 TMEM 累加器要靠 4 个 warp 各读自己的 32-lane 窗口才能取回。

另外补充三个通俗概念：

- **存储层级的一般规律**：越靠近计算单元的存储越小、越快、作用范围越窄；越远的越大、越慢、共享范围越广。CPU 上的"寄存器 → L1 → L2 → 内存"是如此，GPU 上的"RF → SMEM/TMEM → GMEM"也是如此。内核优化的一个核心主题，就是让数据尽量停留在离 Tensor Core 近的层级上。
- **HBM**（High Bandwidth Memory）：GPU 的显存，即 GMEM 的物理载体。本书的 B200 拥有 8 TB/s 的 HBM3e 带宽。
- **tile（分块）与 accumulator（累加器）**：GEMM 内核不会一次算完整矩阵，而是把矩阵切成小块（tile）搬进片上存储逐块计算；MMA 指令反复把乘积累加到一块缓冲上，这块缓冲叫累加器。**epilogue** 指计算完成后把结果写回 GMEM 的收尾阶段。

## 3. 本讲源码地图

本讲的主要"源码"是书稿正文与交互式演示资产（本仓库的产品是教材站点，而非可执行软件包，见 u1-l1/u1-l2）：

| 文件 | 作用 |
|------|------|
| `chapter_background/index.md` | 背景章英文正文，本讲主要依据其中的 "Memory Spaces" 一节 |
| `zh/chapter_background/index.md` | 同一章的中文镜像（中英同构，见 u1-l2），可对照阅读 |
| `_extra/demo/cta_cluster.html` | 2-CTA cluster 与 DSMEM 的交互式演示，本讲的关键图示资产 |
| `_extra/demo/sm_architecture.html` | Blackwell SM 架构演示，含 SMEM/TMEM/RF 的容量标注，用于对比表 |
| `chapter_tmem/index.md` | TMEM 专题章，本讲只取其结构性结论，深入留到单元七 |
| `chapter_gemm_advanced/index.md` | GEMM Step 8（2-CTA cluster），为 DSMEM 数据流提供真实内核佐证 |
| `chapter_performance/index.md` | 性能章，提供 B200 HBM 带宽数字 |
| `tirx_guide/language_reference/cuda/buffers.rst` | TIRx 语言参考，把硬件内存空间映射为 buffer 的 `scope` 参数 |

## 4. 核心概念与源码讲解

### 4.1 四种存储空间：GMEM、SMEM、TMEM 与 RF 的对比

#### 4.1.1 概念说明

内核里的每一个数据——输入 tile、累加器、临时标量、最终输出——在任一时刻都位于某个具体的存储空间里。GPU 提供多种内存空间，它们在三个维度上做取舍：

- **容量**：能装多少数据；
- **延迟/带宽**：读写一次要等多久、单位时间能搬多少字节；
- **作用范围（access scope）**：谁能访问它——整个设备、一个 CTA、一个 warp，还是单个线程。

正文对这一节的定位说得很直接：线程层级说明计算如何组织，接下来要确定数据放在哪里，而"kernel 必须在这些空间之间高效地移动数据"——这是高性能内核的核心任务之一。

#### 4.1.2 核心流程

把四个空间串起来，一条 GEMM tile 的典型数据路径是：

```text
GMEM（HBM，全 device 共享）
   │  TMA 异步搬运（单线程发起，引擎执行）
   ▼
SMEM（CTA 私有暂存区，低延迟 scratchpad）
   │  tcgen05 MMA 直接从 SMEM 读取操作数 A/B
   ▼
TMEM（CTA 的二维累加器空间，Blackwell 新增）
   │  epilogue：4 个 warp 用 tcgen05.ld 各读 32-lane 窗口
   ▼
RF（每线程寄存器，做类型转换等收尾计算）
   │  通常先 staging 到 SMEM，再由 TMA store 写回
   ▼
GMEM（最终输出 D）
```

先记住这条纵向路径，本讲其余部分就是在逐层拆解其中的 SMEM、TMEM，以及横向的 DSMEM。

#### 4.1.3 源码精读

正文用一张四行表格概括四种空间，位于 [chapter_background/index.md:73-78](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_background/index.md#L73-L78)：GMEM 归整个 device、做持久张量存储、是大容量 HBM；SMEM 归单个 CTA、做 tile 暂存、是低延迟 scratchpad（B200 上最高 228 KB/SM）；TMEM 归单个 CTA、专存 MMA 累加器、Blackwell 新增；RF 归单个线程、放标量与每线程的 tile fragment。中文镜像的同一张表在 [zh/chapter_background/index.md:51-56](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/zh/chapter_background/index.md#L51-L56)。

几个容量/带宽数字可以在仓库其他处得到印证：

- SMEM 的 228 KB/SM 标在 SM 架构演示的 SMEM 单元上，见 [_extra/demo/sm_architecture.html:148-151](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/sm_architecture.html#L148-L151)，其说明文字强调 SMEM 是"CTA 内所有线程共享的片上 scratchpad，兼作 TMA load（HBM→SMEM）与 TMA store（SMEM→HBM）的 staging 区"，见 [_extra/demo/sm_architecture.html:250-252](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/sm_architecture.html#L250-L252)。
- GMEM 的 8 TB/s HBM3e 带宽来自性能章对 B200 规格的说明，见 [chapter_performance/index.md:16](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_performance/index.md#L16)。
- RF 是每线程私有的，容量最小但最快。Flash Attention 4 章给过一个具体的量级感受：一个 CTA 的四个 warpgroup 按角色分配每线程寄存器上限 200/200/64/48，四个 128 线程的 warpgroup 合计 \(128\times(200+200+64+48)=65{,}536\) 个 32 位寄存器，见 [chapter_flash_attention/index.md:252-255](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L252-L255)。

最后做一个从硬件到 DSL 的桥接：在 TIRx 里声明 buffer 时，`scope` 参数正是选择内存空间的开关——`"global"`（默认，设备全局内存）、`"shared"` / `"shared.dyn"`（静态/池化动态共享内存）、`"local"`（每线程寄存器）、`"tmem"`（Blackwell Tensor Memory），对应关系列在语言参考的 scope 表中，见 [tirx_guide/language_reference/cuda/buffers.rst:66-89](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/language_reference/cuda/buffers.rst#L66-L89)。也就是说，你在内核源码里看到的 `T.alloc_shared`、`T.alloc_local`、TMEM pool，就是本讲这张对比表在代码里的名字。

#### 4.1.4 代码实践

**实践目标**：把"四个空间"从表格知识变成与 SM 硬件单元对应的直观图景。

**操作步骤**（源码阅读型实践，无需 GPU）：

1. 用文本编辑器或浏览器打开 `_extra/demo/sm_architecture.html`（它是自包含 HTML，直接双击或在本地构建站点后访问嵌入它的背景章页面均可）。
2. 依次点击 SMEM、Tensor Memory、Register File 三个单元，阅读下方弹出的说明文字。
3. 对照 4.1.3 的四行表格，为每个空间记录三项信息：作用范围、容量标注、在数据路径中的角色（谁写入它、谁从它读出）。

**需要观察的现象**：TMEM 的说明文字会明确写着"HW-managed memory private to the SM……tcgen05 MMA accumulates here — no register pressure"，见 [_extra/demo/sm_architecture.html:255-257](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/sm_architecture.html#L255-L257)；RF 的说明则描述回写链路 "TMEM → Registers via `tcgen05.ld`（fp32）→ 转 fp16 → 存 SMEM → TMA 写回 HBM"，见 [_extra/demo/sm_architecture.html:262-262](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/sm_architecture.html#L262-L262)。这两段文字恰好印证 4.1.2 的纵向数据路径。

**预期结果**：你手里有了一份"空间 ↔ SM 单元 ↔ 数据路径角色"的三列笔记，后续读任何内核都能先问"这块数据现在在哪个空间"。

#### 4.1.5 小练习与答案

**练习 1**：一个 fp16 的 \(128\times128\) A tile 加一个同尺寸 B tile 放进 SMEM，共占多少字节？占 B200 每 SM 228 KB 上限的多少？

答案：\(128\times128\times2\,\text{B}\times2 = 65{,}536\,\text{B} = 64\,\text{KB}\)，约占 228 KB 的 28%。这说明一个 CTA 的双操作数 tile 完全放得下，还有余量留给多级流水线（Step 5 之后会看到 SMEM 成为需要精打细算的资源，见单元十二）。

**练习 2**：为什么 MMA 累加器不直接放在 GMEM 里，每个 K 步读写一次？

答案：每个 K 块累加都要读写一次 HBM，8 TB/s 的带宽会立刻成为瓶颈，且延迟极高。累加器必须留在片上（旧架构用 RF，Blackwell 用 TMEM），只在 epilogue 写回 GMEM 一次。

**练习 3**：TIRx 中 `T.alloc_local((4,), "float32")` 声明的 buffer 位于哪个空间？

答案：`"local"` scope，即每线程的寄存器（RF），见 [tirx_guide/language_reference/cuda/buffers.rst:84-86](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/language_reference/cuda/buffers.rst#L84-L86)。

### 4.2 TMEM：Blackwell 新增的二维累加器空间

#### 4.2.1 概念说明

TMEM 是本讲四个空间里唯一"新面孔"，也是 Blackwell 相对 Hopper 最重要的存储变化之一。要理解它的动机，先看旧架构的痛点：

- 在 Blackwell 之前，MMA 累加器通常放在**寄存器**里；
- 随着 MMA tile 越做越大，累加器占用的寄存器越来越多，挤占了其他用途（地址计算、epilogue 临时值）的寄存器预算——这就是"寄存器压力"。

Blackwell 的第五代 Tensor Core（`tcgen05`）改为把累加器写入一块专门的片上存储——TMEM，从而把这部分寄存器压力整体卸掉。

结构上，TMEM 是一个 CTA 使用的**二维** scratchpad：

- **行维度**：128 行，对应 128 条 TMEM lane，恰好对齐一个 warpgroup 的 \(4\times32=128\) 线程（这是上一讲"warpgroup 覆盖 TMEM 四个 32-lane 窗口"的硬件根源）；
- **列维度**：最多 512 列，每列宽 32 bit；
- 逻辑上归 CTA 使用，物理上仍位于 SM 内。

按此计算，TMEM 的总容量为

\[
128 \;\text{lanes} \times 512 \;\text{columns} \times 4\,\text{B} = 262{,}144\,\text{B} = 256\,\text{KB per SM}.
\]

#### 4.2.2 核心流程

TMEM 与 SMEM 最大的行为差异是：**它需要程序显式管理生命周期**。一次典型使用的流程是：

```text
1. 分配：warp 集体执行 tcgen05.alloc，沿 Column 维度申请 n_cols 列
   （合法值 32/64/128/256/512；分配一列即得到该列全部 128 个 Lane）
2. 累加：tcgen05 MMA 把乘积累加进 TMEM 的 (Lane, Column) 单元
3. 读回：epilogue 阶段，warpgroup 的 4 个 warp 各自执行 tcgen05.ld，
   读取属于自己的 32-lane 窗口，把数据带回寄存器
4. 释放：确认所有异步操作完成后 tcgen05.dealloc 归还列
```

一个实用的推算规则：一个 \(128\times N\) 的 fp32 累加器 tile 恰好占用 \(N\) 列 TMEM（每列 128 lane × 32 bit）。例如 \(128\times256\) 的 fp32 累加器占 256 列，正好用掉一半 TMEM。

#### 4.2.3 源码精读

- 动机段：正文解释了"累加器从寄存器搬到 TMEM 以降低寄存器压力"的因果链，见 [chapter_background/index.md:80-83](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_background/index.md#L80-L83)。
- 二维结构段：128 行（对应 128 条 TMEM lane）× 最多 512 列 × 每列 32 bit、逻辑归 CTA 物理在 SM，见 [chapter_background/index.md:85-87](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_background/index.md#L85-L87)；中文镜像 [zh/chapter_background/index.md:60-60](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/zh/chapter_background/index.md#L60-L60)。
- 显式管理与 4-warp 读回：内核必须分配/释放 TMEM，epilogue 要显式把累加器读回寄存器，读满 128 lane 时四个 warp 各加载自己的 32-lane 窗口，见 [chapter_background/index.md:89-91](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_background/index.md#L89-L91)。
- TMEM 专题章给出更精确的分配语义：每 CTA 在 Lane 维有 128 个位置、Column 维最多 512 个位置、每个 \((\text{Lane},\text{Column})\) 单元 32 bit，分配即沿 Column 维保留一段区间，见 [chapter_tmem/index.md:16-16](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tmem/index.md#L16-L16)；合法 `n_cols` 取值为 32/64/128/256/512，见 [chapter_tmem/index.md:24-24](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tmem/index.md#L24-L24)。
- 真实内核佐证：GEMM Step 7 的内核源码里 `T.ptx.tcgen05.alloc(T.address_of(tmem_addr), n_cols=512, cta_group=1)` 一次性分配全部 512 列给累加器，见 [chapter_gemm_advanced/index.md:185-185](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L185-L185)。

#### 4.2.4 代码实践

**实践目标**：用容量推算验证"二维结构"不是抽象概念，而是能算出具体字节数的物理资源。

**操作步骤**（纸笔/Python 均可，无需 GPU）：

1. 计算 TMEM 总容量：\(128 \times 512 \times 4\,\text{B}\)，核对是否等于 256 KB。
2. 推算 GEMM Step 7 的累加器需求：输出 tile 为 \(128\times128\) 的 fp32 累加器需要多少列？Step 8 的 \(256\times256\) cluster 输出 tile 在**每个 CTA 各自的 TMEM** 中又各占多少列（提示：两 CTA 各拥有 \(128\times256\) 的行片）？
3. 写三行 Python（示例代码）把上述三个数字打印出来：

```python
# 示例代码：TMEM 容量推算
tmem_bytes = 128 * 512 * 4          # lane * column * 32bit
print(tmem_bytes / 1024, "KB")      # 预期 256.0
print("Step7 128x128 fp32 acc:", 128, "cols")   # N 列 = N
print("Step8 每 CTA 128x256 fp32 acc:", 256, "cols")
```

**需要观察的现象**：三个数字与 4.2.2 的推算规则一致；Step 8 每个 CTA 的累加器占 256 列，恰好是 512 列的一半。

**预期结果**：你能解释为什么 Step 7/8 的内核都直接 `n_cols=512` 整段分配（此时累加器是 TMEM 的唯一占用者），以及在单元十四会看到 FA4 必须**精细切分** TMEM 列区间让 S、P、O 多块数据复用同一空间。GPU 上的实际行为待本地验证（本实践为推演型，不依赖运行）。

#### 4.2.5 小练习与答案

**练习 1**：为什么读回一个完整的 128-lane 累加器需要恰好 4 个 warp？

答案：每个 warp 只能访问 TMEM 中自己对应的 32-lane 窗口（warpgroup 内 4 个 warp 各管一段，见 [chapter_background/index.md:90-91](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_background/index.md#L90-L91)），\(128/32=4\)，所以需要 4 个 warp 各读一段。

**练习 2**：列出 TMEM 与 SMEM 的两个本质不同点。

答案：①定位不同——SMEM 是通用 tile 暂存区（TMA load/store 的 staging），TMEM 专为 `tcgen05` MMA 的累加器（以及相关数据）服务；②管理方式不同——SMEM 由 CTA 级分配（如 TIRx 的 SMEM pool），TMEM 沿 Column 维动态分配/释放且必须显式 dealloc，见 [chapter_tmem/index.md:24-24](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tmem/index.md#L24-L24)。

**练习 3**：如果累加器元素是 fp16 而不是 fp32，一个 \(128\times256\) 的累加器占多少物理列？

答案：TMEM 每个单元固定 32 bit，两个 fp16 会打包进同一列（TMEM 与寄存器之间搬运时的打包/解包只改变组织方式、不改变分配单位，见 [chapter_tmem/index.md:150-150](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tmem/index.md#L150-L150)），因此占 128 列。这个细节在单元十四 FA4 的 TMEM 布局复用中是关键。

### 4.3 DSMEM：cluster 内的跨 CTA 数据共享

#### 4.3.1 概念说明

SMEM 的作用范围是"单个 CTA"——这恰恰是它的限制：两个 CTA 想共享一份 tile 时，朴素做法是拥有者把数据写回 GMEM，另一方再从 GMEM 读进来，一次不必要的 HBM 往返就发生了。

Blackwell（自 Hopper 起）的 cluster 机制给出了直达方案：**分布式共享内存（DSMEM）** 允许同一 cluster 内的其他 CTA 直接访问本 CTA 的 SMEM。两个要点必须同时记住：

1. 数据仍分属各 CTA 自己的 SMEM 分配，DSMEM **不合并**两块 SMEM；
2. 它只是让 cluster 内的 CTA 可以**跨 SM 互访**对方的数据。

当异步操作（例如协作 MMA 读取对端 SMEM）搬动这类数据时，完成后会更新一道 completion barrier，消费方必须等 barrier 完成才能使用结果——这为单元八的 mbarrier 埋下伏笔。

#### 4.3.2 核心流程

以书中反复使用的 2-CTA GEMM 为例。设 cluster 输出 tile 为 \(256\times256\)，B 以 \(N\times K\) 形式存储（即书中的 stored-B），则两个 CTA 的分工是：

| CTA | 所在 SM | Asmem（own） | Bsmem（own） | 输出区域 D |
|-----|---------|--------------|--------------|-----------|
| CTA 0 | SM-0 | A 行 0–127 | B stored 行 0–127 | `D[0:128, 0:256]` |
| CTA 1 | SM-1 | A 行 128–255 | B stored 行 128–255 | `D[128:256, 0:256]` |

关键在于 B 的两侧都要用：B 转置后，CTA 0 加载的 stored 行 0–127 恰好对应输出列 0–127，CTA 1 的对应输出列 128–255；而**每个 CTA 都要为自己的 128 行算满全部 256 列**，所以每个 A 切片都要乘上两个 B 切片。于是数据流变成：

```text
for 每个 CTA pair (cta_group::2):
    CTA 0: 从自己 SMEM 读 A[0:128] 与 B[0:128]
           经 DSMEM 读 CTA 1 的 B[128:256]   ── 跨 SM 互访
    CTA 1: 从自己 SMEM 读 A[128:256] 与 B[128:256]
           经 DSMEM 读 CTA 0 的 B[0:128]     ── 跨 SM 互访
    一次协作 MMA 读取两边的 SMEM 操作数，
    把结果分别累加进两边的 TMEM（各 128 行 × 256 列）
```

代价与收益：cluster 让每个操作数的 staged 数据翻倍，但输出 tile 元素数变为 4 倍，且每个 staged 操作数参与的计算量约为原来的两倍——复用变高了。两个 CTA 还可组成 CTA pair，以 `cta_group::2` 模式执行协作 MMA。

#### 4.3.3 源码精读

- DSMEM 的定义段：cluster 内 CTA 可位于不同 SM，各 CTA 仍拥有自己的 SMEM，但 DSMEM 允许同 cluster 其他 CTA 访问其中数据，见 [chapter_background/index.md:93-96](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_background/index.md#L93-L96)。
- 动机段：避免不必要的 GMEM 往返——无需拥有者写回 GMEM、对端再重读；异步搬运完成时更新 completion barrier，消费者先等 barrier，见 [chapter_background/index.md:98-101](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_background/index.md#L98-L101)。
- 2-CTA GEMM 段：每个 CTA 存自己的 A/B 分片、经 DSMEM 读对端的 B 分片；并明确"共享不是合并两块 SMEM 分配"，随后引出 `cta_group::2` 协作 MMA 产出更大输出 tile，见 [chapter_background/index.md:114-119](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_background/index.md#L114-L119)；中文镜像 [zh/chapter_background/index.md:81-83](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/zh/chapter_background/index.md#L81-L83)。正文同时指出 cluster 支撑两种协作——2-CTA cooperative MMA 与 TMA multicast（一次 GMEM load 把同一 tile 送到多个 CTA），二者都依赖 cluster 与 DSMEM 机制，见 [chapter_background/index.md:142-146](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_background/index.md#L142-L146)。
- 交互演示：`cta_cluster.html` 的主体画出 CTA 0（SM-0）与 CTA 1（SM-1）两个方框，各自含 `Asmem (own)`、`Bsmem`、输出三块，中间是双向的 "cross-CTA read" 按钮，见 [_extra/demo/cta_cluster.html:62-75](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/cta_cluster.html#L62-L75)。其 `INFO` 字典逐条解释每个部件，其中 `xread` 条目写明"一个 CTA 经 cluster 互连直接读对端的 stored-B 切片、不经过 global memory 往返——正是 DSMEM 使 2-CTA 协作 MMA 成为可能"，见 [_extra/demo/cta_cluster.html:88-95](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/cta_cluster.html#L88-L95)。该演示同时被 GEMM Step 8 正文复用，见 [chapter_gemm_advanced/index.md:341-347](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L341-L347)。
- 真实内核佐证（Step 8）：正文用同一张图解释 A/B 如何在两 CTA 间切分——A 切片决定各 CTA 拥有的输出行，stored-B 切片经 `B.T` 变成两组输出列，因此协作 MMA 必须沿图中央的 cross-CTA read 访问对端 `Bsmem`，每个 A 切片乘两个 B 切片，见 [chapter_gemm_advanced/index.md:337-339](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L337-L339)；分工表（A 切片 / stored-B 切片 / D 区域）见 [chapter_gemm_advanced/index.md:351-354](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L351-L354)；"操作数翻倍、输出元素四倍、每个操作数参与约两倍计算"的量化结论见 [chapter_gemm_advanced/index.md:356-356](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L356-L356)。跨 CTA 的完成信号同样要跨 SM：Step 8 用 CTA 0 的 `tma2mma` 屏障追踪**两个** CTA 的 TMA 加载，期望字节数为 `CTA_GROUP * (BLK_M * BLK_K + BLK_N * BLK_K) * F16_SIZE`，见 [chapter_gemm_advanced/index.md:384-394](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L384-L394)。

#### 4.3.4 代码实践

**实践目标**：通过"点击—核对"确认自己对 DSMEM 数据流的理解与教材演示一致。

**操作步骤**（无需 GPU）：

1. 用浏览器打开 `_extra/demo/cta_cluster.html`（或本地构建站点后访问背景章 "Distributed Shared Memory Across a Cluster" 一节的嵌入页面）。
2. 依次点击五个部件：CLUSTER 外框、CTA 0 框、`Asmem`、`Bsmem`、中间的 `cross-CTA read`。
3. 每点一次，把弹出的说明文字（即 `INFO` 字典对应条目，见 [_extra/demo/cta_cluster.html:88-95](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/cta_cluster.html#L88-L95)）与 4.3.2 的表格逐行核对，特别注意 `Bsmem` 条目中"stored-B 行经 `B.T` 变成逻辑输出列段"这句。

**需要观察的现象**：默认选中的就是 `xread`（演示脚本最后一行 `select('xread')`，见 [_extra/demo/cta_cluster.html:109-109](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/cta_cluster.html#L109-L109)）——教材作者把跨 CTA 读取设为默认高亮，正说明它是这张图的核心。

**预期结果**：你能不看资料复述出"谁存什么、谁读谁的什么、输出怎么分"，为综合实践的数据流向图做好准备。

#### 4.3.5 小练习与答案

**练习 1**：如果没有 DSMEM，两个 CTA 想让每个 A 切片都乘上两个 B 切片，该怎么办？代价是什么？

答案：每个 CTA 都得从 GMEM 各自加载**两份** B 切片（或者拥有者写回 GMEM、对端再读），造成双倍 HBM 流量和额外同步。DSMEM 让对端直接读 SMEM，免去往返，见 [chapter_background/index.md:98-101](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_background/index.md#L98-L101)。TMA multicast 则从源头消解同一问题：一次 GMEM load 直接把同一 tile 送到多个 CTA，见 [chapter_background/index.md:143-146](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_background/index.md#L143-L146)。

**练习 2**："cluster 内共享 SMEM"是否意味着两个 CTA 的 SMEM 合并成了一块 456 KB 的大缓冲？

答案：不是。每个 CTA 仍拥有自己独立的 SMEM 分配，"共享"仅指同 cluster 的 CTA 可以跨 SM 访问对方数据，见 [chapter_background/index.md:114-116](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_background/index.md#L114-L116)。

**练习 3**：Step 8 中两 CTA 的 TMA 加载为什么可以把期望字节数记在 CTA 0 的一道屏障上，且字节数要乘 `CTA_GROUP`？

答案：协作 MMA 必须等**两边**的 A/B 切片都就绪才能发起，所以实现上让两个 CTA 的 TMA 完成都上报到 CTA 0 的 `tma2mma` 屏障（经 remote view 引用），期望字节数自然是单 CTA 的 \((\text{BLK\_M}\cdot\text{BLK\_K}+\text{BLK\_N}\cdot\text{BLK\_K})\times2\,\text{B}\) 再乘 CTA 数 2，见 [chapter_gemm_advanced/index.md:384-396](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L384-L396)。

## 5. 综合实践

本讲的综合实践把三个模块收拢成两件交付物：**一张对比表**和**一张数据流向图**。

**任务 1：四种存储空间的容量–延迟–访问范围对比表。** 先自己填空列，再对照参考答案：

| 维度 | GMEM | SMEM | TMEM | RF |
|------|------|------|------|-----|
| 作用范围 | 整个 device | 每 CTA（单个 SM 内） | 每 CTA（逻辑），物理在 SM 上 | 每 thread |
| 典型容量（B200） | 大容量 HBM，带宽 8 TB/s | 最高 228 KB/SM | 128 lane × 512 列 × 32 bit = 256 KB/SM | 每线程私有（FA4 中按角色 48–200 个不等） |
| 延迟/带宽 | 最慢、最远 | 低延迟 scratchpad | 片上、专供 MMA 累加 | 最快、容量最小 |
| 典型用途 | 持久张量存储 | tile 暂存、TMA load/store 的 staging | `tcgen05` MMA 累加器 | 标量、每线程 fragment、epilogue 临时值 |
| 管理方式（TIRx scope） | `"global"`（默认） | `"shared"` / `"shared.dyn"` | `"tmem"`（TMEM pool） | `"local"` |
| 是否需要显式分配/释放 | 否（主机侧张量） | CTA 级（pool/alloc_shared） | 是，`tcgen05.alloc`/`dealloc` | 否（编译器分配） |

数据出处：正文四行表 [chapter_background/index.md:73-78](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_background/index.md#L73-L78)；228 KB 见 [_extra/demo/sm_architecture.html:148-151](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/sm_architecture.html#L148-L151)；8 TB/s 见 [chapter_performance/index.md:16](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_performance/index.md#L16)；TMEM 结构见 [chapter_background/index.md:85-87](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_background/index.md#L85-L87)；scope 映射见 [tirx_guide/language_reference/cuda/buffers.rst:72-89](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/language_reference/cuda/buffers.rst#L72-L89)；寄存器角色预算见 [chapter_flash_attention/index.md:252-255](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L252-L255)。

**任务 2：画出 2-CTA GEMM 的 DSMEM 数据流向图。** 参考答案（ASCII 版，可直接誊抄进笔记并自行标注箭头方向）：

```text
                        GMEM（HBM，全 device 共享）
        A rows 0–127          A rows 128–255        B stored rows 0–127   B stored rows 128–255
             │                      │                      │                     │
        TMA load (cta0)        TMA load (cta1)        TMA load (cta0)       TMA load (cta1)
             ▼                      ▼                      ▼                     ▼
   ┌────── SM-0：CTA 0 ──────┐              ┌────── SM-1：CTA 1 ──────┐
   │ Asmem(own): A[0:128,:]  │              │ Asmem(own): A[128:256,:]│
   │ Bsmem(own): B[0:128,:]  │── DSMEM 读 ─▶│ Bsmem(own): B[128:256,:]│
   │                          │◀─ DSMEM 读 ──│                          │
   │ TMEM 累加器              │              │ TMEM 累加器              │
   │  D[0:128, 0:256]         │              │  D[128:256, 0:256]       │
   └──────────────────────────┘              └──────────────────────────┘
                └──────── cta_group::2 协作 MMA（一次发起，两边 SMEM 供数）────────┘
                     │                                              │
                epilogue：TMEM→RF→(SMEM staging)→GMEM        epilogue 同
```

核对要点：每个 CTA 各自 TMA 加载自己的 A、B 切片；**只有 B 切片被对端经 DSMEM 读取**（A 不跨 CTA 读）；一次协作 MMA 同时读两边 SMEM、把结果分别累加进两边的 TMEM；最后各自走 epilogue 写回 GMEM 的自己那 128 行。可与 [_extra/demo/cta_cluster.html:62-79](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/cta_cluster.html#L62-L79) 及 Step 8 分工表 [chapter_gemm_advanced/index.md:351-354](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L351-L354) 逐项比对。

**可选加分项**（示例代码，用 matplotlib 重绘数据流向图，风格可参考仓库 `img/scripts/` 下的绘图脚本约定）：

```python
# 示例代码：用 matplotlib 画 2-CTA DSMEM 数据流简图
import matplotlib.pyplot as plt

fig, ax = plt.subplots(figsize=(9, 4.5))
for x, name in [(0.05, "SM-0 : CTA 0"), (0.62, "SM-1 : CTA 1")]:
    ax.add_patch(plt.Rectangle((x, 0.25), 0.33, 0.5, fill=False, lw=2))
    ax.text(x + 0.165, 0.82, name, ha="center", fontsize=11)
ax.text(0.215, 0.62, "Asmem: A[0:128,:]\nBsmem: B[0:128,:]\nTMEM: D[0:128, 0:256]",
        ha="center", fontsize=9)
ax.text(0.785, 0.62, "Asmem: A[128:256,:]\nBsmem: B[128:256,:]\nTMEM: D[128:256, 0:256]",
        ha="center", fontsize=9)
ax.annotate("DSMEM 读 B", xy=(0.60, 0.50), xytext=(0.42, 0.50),
            arrowprops=dict(arrowstyle="->", color="green"))
ax.annotate("DSMEM 读 B", xy=(0.40, 0.42), xytext=(0.58, 0.42),
            arrowprops=dict(arrowstyle="->", color="green"))
ax.annotate("GMEM → TMA → SMEM", xy=(0.5, 0.24), xytext=(0.5, 0.90),
            arrowprops=dict(arrowstyle="-", ls="--"))
ax.axis("off")
plt.savefig("cta_dsmem_flow.png", dpi=150)   # 待本地验证：图形细节可自行调整
```

## 6. 本讲小结

- GPU 提供四种存储空间，各有取舍：GMEM（device 级、大容量 HBM、8 TB/s）、SMEM（每 CTA、最高 228 KB/SM、低延迟 tile 暂存）、TMEM（每 CTA、MMA 累加器专用）、RF（每线程、最快但容量最小）；内核的核心任务之一就是在它们之间高效移动数据。
- TMEM 是 Blackwell 新增的二维片上空间：128 lane × 最多 512 列 × 32 bit（合计 256 KB/SM），`tcgen05` 把累加器写进 TMEM 以消除旧架构的寄存器压力；它必须显式 alloc/dealloc，读满 128 lane 需 4 个 warp 各读 32-lane 窗口。
- DSMEM 让同 cluster 的 CTA 跨 SM 直接互访对方的 SMEM，避免"写回 GMEM 再重读"的往返；它不合并各 CTA 的 SMEM 分配。
- 2-CTA GEMM 中，每个 CTA 存自己的 A/B 切片、经 DSMEM 读对端的 B 切片，一次 `cta_group::2` 协作 MMA 产出 256×256 输出 tile；操作数翻倍换来 4 倍输出元素。
- 在 TIRx 源码层面，这四个空间对应 buffer 的 `scope`：`"global"`、`"shared"`/`"shared.dyn"`、`"tmem"`、`"local"`——硬件概念从此有了代码里的名字。

## 7. 下一步学习建议

1. **下一讲（u2-l3）**：计算引擎与 GEMM 数据流水线——把本讲的存储空间与上一讲的线程层级串成"TMA 加载 → tcgen05 MMA → epilogue 回写"的三段式流水线，阅读 `chapter_background/index.md` 的后半章（Compute 与 GEMM Data Pipeline 两节，[chapter_background/index.md:121-183](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_background/index.md#L121-L183)）。
2. **想深挖 TMEM**：跳读 `chapter_tmem/index.md` 的分配生命周期一节（单元七的 u7-l3 会逐指令精读 `tcgen05.alloc`/`dealloc`/`ld`/`st`）。
3. **想看 DSMEM 的完整内核**：GEMM Step 8（`chapter_gemm_advanced/index.md` 的 "Step 8: Two-CTA Cluster" 一节起，[chapter_gemm_advanced/index.md:325-378](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L325-L378)），单元十三的 u13-l2 会精读；建议先完成单元九、十一的前置。
4. **想理解"为什么片上暂存能提速"**：单元三的 roofline 模型（u3-l1）会给出算术强度与带宽屋顶的定量工具。
