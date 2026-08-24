# u9-l1 TIRx 是什么与第一个内核 hgemm_v1

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 TIRx（Tensor IR next）要解决的问题：把散落在 CUDA/PTX 中的三个关键决策（谁执行、数据怎么摆、走哪条硬件路径）显式表达为结构化 IR。
2. 逐段读通第一个可运行的单 tile GEMM 内核 `hgemm_v1`，并按「分配 SMEM/TMEM → 拷贝 A/B → 发起 MMA → 读回写 GMEM」四个阶段给每一行代码归位。
3. 区分两类调用：`Tx.*` 开头的 **tile 操作**（一条调用描述一个整块 tile 的工作）与 `T.ptx.*` / `T.cuda.*` 开头的**底层 PTX 辅助调用**（分配 TMEM、初始化 barrier、建立同步）。
4. 说出三个核心 tile primitive——`Tx.cta.copy`、`Tx.gemm_async`、`Tx.wg.copy_async`——各自的分工与执行者。

本讲是单元九「TIRx 编程模型入门」的第一讲：先把整个内核当成一个黑盒跑起来、再打开看结构；编译验证细节由 u9-l2 展开，scope/layout/dispatch 三要素的深入讨论留给 u9-l3。

## 2. 前置知识

本讲默认你已读过以下讲义，遇到相关概念只做简短回顾：

- **u1-l3 运行环境**：TIRx 编译器是 Apache TVM wheel 中的 `tvm.tirx` 模块，需与 `cuda-bindings` 一起安装（`pip install apache-tvm==0.26.0 cuda-bindings`）；书中内核需要 Blackwell GPU（`sm_100a`）。TIRx 依赖 Python 源码检视解析内核，**示例必须写在文件或 notebook 单元格中**，不能塞进 `python -c`。
- **u2-l1 线程层级**：thread → warp（32 线程）→ warpgroup（4 warp = 128 线程）→ CTA → cluster → grid。本内核只动用 1 个 CTA，内部 1 个 warpgroup。
- **u2-l3 GEMM 数据流水线**：一个输出 tile 的生命期分 Load（GMEM→SMEM）、Compute（MMA 累加进 TMEM）、Epilogue（读回寄存器、转 dtype、写回 GMEM）三段。本讲的四阶段就是这个三段式在代码里的落地。
- **u7 系列（概念层面即可）**：`tcgen05.mma` 是单线程语义的 tile 级指令；累加器放在 TMEM（128 Lane × 512 Column，每格 32 bit）；TMEM 须先 `tcgen05.alloc` 分配、用完 `dealloc` 释放；TMEM→寄存器用 warp 集体的 `tcgen05.ld`，读完要 `wait::ld`。
- **u8-l1 mbarrier**：「已发起」不等于「已完成」；mbarrier 用到达计数加在途字节计数追踪异步完成，`arrive` 与 `try_wait` 分离。

一个不需要硬件背景的直观比喻：tile 操作像「填一张整托盘的订单」，底层辅助调用像「开门、开灯、锁门」。前者描述工作量，后者维持秩序。本讲要训练的，就是把这两类调用在一颗真实内核里分拣清楚。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [chapter_intro_tirx/index.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md) | 「Introduction to TIRx」章正文。包含 TIRx 动机、`hgemm_v1` 完整源码、编译验证代码与 scope/layout/dispatch 讲解，是本讲唯一的正文来源 |
| [_extra/demo/tirx_dispatch.html](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/tirx_dispatch.html) | 自包含交互演示：从内核中摘出关键行，点 Scope / Layout / Dispatch 按钮即高亮对应行。正文经 iframe 嵌入，也是本讲实践的工具 |

说明：本仓库是教材仓库，`hgemm_v1` 的「源码」以 Markdown 代码块形式存在于章节正文中，后续 GEMM 章节直接把它当作优化的起点。

## 4. 核心概念与源码讲解

### 4.1 模块一：TIRx 的动机——把三个决策写进结构化 IR

#### 4.1.1 概念说明

Part I 已经把硬件机制（线程层级、SMEM/TMEM、TMA、Tensor Core、异步 barrier）讲清楚了。接下来的问题是：**怎么把这些机制组织成能跑的内核？**

直接写 CUDA/PTX 当然可以，但章节指出低层程序有个通病：几个重要决策**散落**在 intrinsic 参数、地址计算和编码约定里——

1. 这条操作由**哪些线程**执行？
2. 操作数 tile **放在哪里**、按什么物理排布摆放？
3. 这条操作最终由**哪个硬件实现**执行？

这些信息全都存在，但分散在指令的各个角落，编译器很难把它们当作一个整体来检查和变换。

TIRx（Tensor IR next）是一个 Python DSL，它仍然直接操作线程、SMEM、TMEM、barrier、`tcgen05.mma` 这些硬件概念——**硬件是主角，DSL 是载体**——区别在于把上述三个决策显式写进结构化 IR：

- **Scope**：哪些线程执行一条操作；
- **Layout**：一个逻辑 tile 如何映射到内存、lane 或寄存器；
- **Dispatch**：一条 tile 操作由哪个硬件实现执行。

#### 4.1.2 核心流程

用一句话概括设计动机的推导链：

```text
低层代码里三个决策散落各处
    ↓ 编译器难以整体检视与变换
把 scope / layout / dispatch 显式提升为 IR 的一等公民
    ↓ 编译器可以检查它们、组合它们
再由编译流水线把它们降级（lowering）成线程级控制流、地址计算与硬件指令
```

这就是「在 IR 层面写内核」的含义：作者声明意图（谁干、放哪、走哪条路），编译器负责展开成几百行线程级代码。

#### 4.1.3 源码精读

章节开头总览就点明了这三件事（[chapter_intro_tirx/index.md:L7-L9](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L7-L9)）：TIRx 是写 GPU 内核的 Python DSL，通过结构化 IR 暴露线程、SMEM、TMEM、barrier、Tensor Core 等硬件概念；一条 tile 操作由三条信息决定（scope、layout、dispatch）。

动机段落在 [chapter_intro_tirx/index.md:L30-L32](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L30-L32)：同样的工作可以直接用 CUDA 或 PTX 完成，但低层程序把「哪些线程执行、操作数 tile 在哪、最终由哪条硬件指令实现」散落在 intrinsic 参数、地址计算和编码约定中——信息都在，编译器却难以整体检视和变换。

TIRx 的定义与三要素在 [chapter_intro_tirx/index.md:L34-L40](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L34-L40)：`TIRx (Tensor IR next) is a Python DSL that makes these three decisions explicit in structured IR`。注意最后一段强调 TIRx 仍然直接接触硬件概念，区别只是这些选择被显式表示在 IR 里，编译器可以检查并降级它们。

章节的教学顺序在 [chapter_intro_tirx/index.md:L42](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L42)：不从语言构件清单开始，而是先给一个完整内核——这正是本讲 4.2 的内容。

#### 4.1.4 代码实践

**实践目标**：在阅读内核之前，先用 5 分钟做一个「反例体验」，理解「决策散落」到底有多难受。

**操作步骤**：

1. 回忆（或重新打开）u5-l1 中 Ampere `mma.sync` 的 fragment 映射：`g = l//4, t = l%4`。
2. 针对下面三个问题，分别写下「答案藏在代码的什么位置」：
   - 这条 MMA 由多少线程协同执行？（答案藏在指令名与 warp 语义约定里）
   - B 矩阵每行铺在 SMEM 哪些字节上？（答案藏在手写的 XOR 地址计算里）
   - 这条乘加走哪个计算单元？（答案藏在指令名的 intrinsic 选择里）
3. 对比本讲 4.3 将看到的 TIRx 写法：scope 写在 tile 操作的命名与守卫里、layout 写在 `layout=` 参数里、dispatch 写在 `dispatch=` 参数里。

**需要观察的现象**：CUDA/PTX 里三个答案没有统一的落脚点；TIRx 里三个答案各自有一个显式的语法位置。

**预期结果**：你能体会章节 L32 那句「difficult for a compiler to inspect and transform as a whole」——不是信息缺失，而是信息不成结构。本实践为纯阅读型，无需运行。

#### 4.1.5 小练习与答案

**练习 1**：TIRx 与「高层张量 DSL」（比如自动调度的算子库）有什么本质区别？

**参考答案**：TIRx 不隐藏硬件——它仍然直接操作线程、SMEM、TMEM、barrier 与 `tcgen05.mma` 这些概念（章节 L40）。它改变的只是表达方式：把这些选择从指令参数与地址计算中提取出来，显式表示为结构化 IR 中的 scope/layout/dispatch，供编译器检查和降级。换句话说，它是「结构化的低层编程」，而不是「抽象掉硬件的高层编程」。

**练习 2**：章节说三个决策在 CUDA/PTX 中「scattered across intrinsic arguments, address calculations, and coding conventions」。请把 scope、layout、dispatch 分别对应到这三类藏身处。

**参考答案**：scope 主要藏在 intrinsic 参数与编码约定中（如某指令要求整个 warp 执行同一条指令、由约定的 lane 提供地址）；layout 主要藏在地址计算中（每线程手算自己那份数据在哪里，含 swizzle 的 XOR 公式）；dispatch 藏在 intrinsic 参数/指令名选择中（选哪条指令就选了哪条硬件路径）。三者都没有独立的语法位置。

### 4.2 模块二：hgemm_v1 四阶段精读

#### 4.2.1 概念说明

`hgemm_v1` 是全书第一个可运行的 TIRx 内核：一个**单 tile、单 CTA、全同步**的 GEMM。它计算：

\[ D = A\,B^{\mathsf{T}} \]

其中 `A`、`B` 形状均为 `128×64`，输出 `D` 为 `128×128`；只算一个输出 tile，所以 grid 里只有一个 CTA。浮点量为 fp16 输入、fp16 输出、fp32 累加（hgemm 的 h 即 half）。

它的数据路径（章节原文）是：

```text
A/B: GMEM -> SMEM -> tcgen05.mma
D:   tcgen05.mma -> TMEM -> registers -> GMEM
```

对照 u2-l3 的三段式：Load（GMEM→SMEM）＋ Compute（MMA 进 TMEM）＋ Epilogue（TMEM→寄存器→GMEM）。

一个重要直觉：**整条矩阵乘在源码里只有一条 tile 操作 `Tx.gemm_async`**。这一条调用描述完整的 `128×128×64` tile GEMM；由于底层每条 `tcgen05.mma` 沿 K 前进 16 个元素，编译器根据形状、布局与 dispatch 信息自动展开出 4 条 MMA 指令：

\[ \text{MMA 指令条数} = \lceil K / 16 \rceil = \lceil 64 / 16 \rceil = 4 \]

这也是「tile 操作」一词的含义：作者以 tile 为单位声明工作，指令级展开交给编译器。

#### 4.2.2 核心流程

章节明确给出读代码的四阶段框架（[chapter_intro_tirx/index.md:L61-L66](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L61-L66)）：

1. **分配 SMEM 与 TMEM**；
2. **把 A、B 从 GMEM 拷贝到 SMEM**；
3. **通过 `Tx.gemm_async` 发起 MMA**；
4. **把结果从 TMEM 读入寄存器，再写回 GMEM**。

并提醒（L68）：重点关注 `Tx.cta.copy`、`Tx.gemm_async`、`Tx.wg.copy_async` 三个 tile 操作，其余低层调用负责分配释放 TMEM、初始化 barrier、建立同步，暂且当作四阶段的支撑步骤。

加上最后的 TMEM 释放，完整流程图如下：

```text
kernel(A, B, D)
│
├─ 0 外壳：类型/块尺寸/SMEM 布局准备（宿主侧 Python）
│
├─ ① 分配：SMEMPool 划出 tmem_addr 槽 + mma_bar + A/B 缓冲
│           warp 0 初始化 mbarrier、tcgen05.alloc 分配 512 列 TMEM
│           fence + cta_sync 发布；decl_buffer 把 TMEM 绑定为 buffer
│
├─ ② 拷贝：Tx.cta.copy ×2（全 128 线程协作搬 A、B）→ cta_sync
│
├─ ③ 计算：warp 0 中 elect_sync 选出的 1 个线程
│           Tx.gemm_async（dispatch="tcgen05"）→ tcgen05.commit
│           全员 mbarrier.try_wait 等 MMA 完成
│
├─ ④ 回写：Tx.wg.copy_async（warpgroup 集体读 TMEM → 寄存器）
│           wait.ld → Tx.cast(fp32→fp16) → Tx.copy 写回 GMEM
│
└─ ⑤ 收尾：cta_sync → relinquish_alloc_permit → tcgen05.dealloc
```

#### 4.2.3 源码精读

完整源码在 [chapter_intro_tirx/index.md:L84-L171](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L84-L171)。以下按阶段摘取关键行。imports 见 [L72-L78](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L72-L78)：`T` 是 TIRx 脚本命名空间，`Tx` 是 tile 操作命名空间，`TileLayout/S/TLane/TCol/tid_in_wg` 来自布局模块，`mma_shared_layout/SwizzleMode` 来自 TMA 工具。

**阶段 0：外壳与准备（宿主侧 Python，非设备代码）**

[L86-L93](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L86-L93) 设定 dtype（fp16 输入输出、fp32 累加）、块尺寸 `BLK_M, BLK_N, BLK_K = 128, 128, 64`，并用 `mma_shared_layout(..., SwizzleMode.SWIZZLE_128B_ATOM, ...)` 生成 A/B 的 SMEM 布局——即 u6 讲过的 128B swizzle，Tensor Core 从 SMEM 读数据时要求的物理排布：

```python
BLK_M, BLK_N, BLK_K = 128, 128, 64
A_layout = mma_shared_layout(a_type, SwizzleMode.SWIZZLE_128B_ATOM, (BLK_M, BLK_K))
B_layout = mma_shared_layout(b_type, SwizzleMode.SWIZZLE_128B_ATOM, (BLK_N, BLK_K))
```

[L95-L101](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L95-L101) 定义内核签名与设备入口：`@T.prim_func` 定义 GPU 函数，`T.device_entry()` 标记设备代码入口，三个 `T.Buffer` 参数直接对接 GMEM 中的 A、B、D。

[L104-L107](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L104-L107) 取出线程层级坐标（u2-l1 的六级层级在此暴露为 API）。方括号内是各维规模：1 个 warpgroup、每 warpgroup 4 个 warp、每 warp 32 个 lane，合计 \(1 \times 4 \times 32 = 128\) 个线程；本例 grid 为 1×1，故 `bx, by` 皆为 0：

```python
bx, by = T.cta_id([M // BLK_M, N // BLK_N])
wg_id = T.warpgroup_id([1])
warp_id = T.warp_id_in_wg([4])
lane_id = T.lane_id([32])
```

**阶段 ①：分配 SMEM 与 TMEM**

[L110-L116](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L110-L116) 用 `SMEMPool` 在共享内存里连续划块：先分配 `tmem_addr`（一个 uint32 槽，稍后存放 TMEM 基地址）和 `mma_bar`（uint64、8 字节对齐的 mbarrier），随后 `pool.move_base_to(1024)` 把分配基址抬到 1024 字节边界，再分配带 swizzle 布局的 `Asmem`、`Bsmem`，最后 `pool.commit()` 生效：

```python
pool = T.SMEMPool()
tmem_addr = pool.alloc((1,), "uint32")
mma_bar = pool.alloc((1,), "uint64", align=8)
pool.move_base_to(1024)
Asmem = pool.alloc((BLK_M, BLK_K), a_type, layout=A_layout)
Bsmem = pool.alloc((BLK_N, BLK_K), b_type, layout=B_layout)
pool.commit()
```

把基址对齐到 1024B 有双重意义：低地址区留给两个控制变量；同时 SWIZZLE_128B 的 atom 是 8 行 × 128B = 1KB 的连续块（u5-l2、u6-l2），A/B 大缓冲从 1024B 边界起排，恰好让每个 swizzle atom 天然对齐。

[L119-L126](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L119-L126) 是 warp 0 的初始化与全 CTA 发布。`mbarrier.init(..., 1)` 把 barrier 的期望到达数设为 1（后续只有发起 MMA 的那个线程会 `commit` 到它）；`tcgen05.alloc(..., n_cols=512, cta_group=1)` 分配整 512 列 TMEM 并把基地址写进 `tmem_addr`（这正是 u7-l3 的建议：起步即分配最大档，之后按列切片）。三条 fence/sync 建立「init 完成对全 CTA 可见」的顺序——`fence.proxy_async` 让通用代理的写入对异步代理可见，`fence.mbarrier_init` 保证 barrier 初始化先于其他线程的 arrive/wait 被观察到，`cta_sync` 让 128 个线程齐步后才继续：

```python
if warp_id == 0:
    if lane_id == 0:
        T.ptx.mbarrier.init(mma_bar.ptr_to([0]), 1)
    T.ptx.tcgen05.alloc(T.address_of(tmem_addr), n_cols=512, cta_group=1)

T.ptx.fence.proxy_async("shared::cta")
T.ptx.fence.mbarrier_init()
T.cuda.cta_sync()
```

[L128-L135](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L128-L135) 把刚分配的 TMEM 绑定成一个 buffer：`scope="tmem"`、基址取自 `tmem_addr[0]`，布局 `TileLayout(S[(128, 512) : (1@TLane, 1@TCol)])` 即 u4-l2 的命名轴写法——逻辑行映射到 TMEM 的 Lane 轴、逻辑列映射到 TCol 轴，步长均为 1（恒等映射）。`m_st/n_st` 是本 tile 在全局矩阵中的起点（本例均为 0），`phase_mma` 是等待 MMA barrier 时要用的相位初值：

```python
tmem = T.decl_buffer(
    (128, 512), "float32", scope="tmem", allocated_addr=tmem_addr[0],
    layout=TileLayout(S[(128, 512) : (1@TLane, 1@TCol)])
)
```

**阶段 ②：拷贝 A、B（GMEM → SMEM）**

[L137-L140](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L137-L140)：两条 `Tx.cta.copy` 分别把 A 的 `128×64` 切片与 B 的 `128×64` 切片搬进 SMEM。这是 **CTA 级 tile 操作**——由整个 CTA 的 128 个线程协作执行（这一版走普通线程路径，后续版本会改派发给 TMA）。拷完一条 `cta_sync`：MMA 只由一个线程发起，它必须确认全体线程都写完了 SMEM 才能开算：

```python
Tx.cta.copy(Asmem[:, :], A[m_st:m_st + BLK_M, :])
Tx.cta.copy(Bsmem[:, :], B[n_st:n_st + BLK_N, :])
T.cuda.cta_sync()
```

**阶段 ③：发起 MMA 并等待完成**

[L142-L151](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L142-L151) 是全内核的心脏。两层守卫 `warp_id == 0` + `T.ptx.elect_sync()` 把执行者收窄到**一个被选出的线程**——因为 `tcgen05.mma` 是单线程语义的指令（u7-l1）。`Tx.gemm_async` 一条调用声明完整的 `128×128×64` 乘累加：目的为 TMEM 的前 `BLK_N` 列、源为 SMEM 中的 A/B tile；`accum=False` 表示这是第一次（也是唯一一次）写入而非累加到旧值——本版没有 K 循环，不存在累加器复用；`dispatch="tcgen05"` 选定 Blackwell Tensor Core 路径；`cta_group=1` 单 CTA 模式：

```python
if warp_id == 0:
    if T.ptx.elect_sync():
        Tx.gemm_async(
            tmem[:, :BLK_N], Asmem[:, :], Bsmem[:, :],
            accum=False, dispatch="tcgen05", cta_group=1
        )
        T.ptx.tcgen05.commit(mma_bar.ptr_to([0]), cta_group=1)

T.ptx.mbarrier.try_wait(mma_bar.ptr_to([0]), phase_mma)
```

`tcgen05.commit` 把该线程已发出的异步操作挂到 `mma_bar` 上，硬件在 MMA 真正完成后主动补一次到达（u7-l1）；随后**所有线程**执行 `mbarrier.try_wait(..., phase_mma)` 阻塞到相位 0 完成。这一对 commit/try_wait 正是 u8-l1 讲的「发起与完成分离」。

**阶段 ④：读回 TMEM 并写回 GMEM**

[L153-L162](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L153-L162) 是 epilogue。先在每线程寄存器里开两个长度 `BLK_N` 的局部数组（fp32 原值与 fp16 转换值），再把 fp32 数组**重看**成一个 `128 × BLK_N` 的 tile——布局 `S[(128, BLK_N) : (1@tid_in_wg, 1)]` 用命名轴 `tid_in_wg`（warpgroup 内线程号 0..127）作行轴：即每个线程持有并负责一行。`Tx.wg.copy_async` 是 **warpgroup 级 tile 操作**，128 个线程集体执行，硬件按各自 lane 把 TMEM 累加器切片分发进各自寄存器（底层即 u7-l4 的 `tcgen05.ld`），随后必须 `tcgen05.wait.ld()` 才能使用这些寄存器：

```python
Dreg = T.alloc_local((BLK_N,), acc_type)
Dreg_f16 = T.alloc_local((BLK_N,), d_type)
Dreg_wg = Dreg.view(128, BLK_N,
                    layout=TileLayout(S[(128, BLK_N) : (1@tid_in_wg, 1)]))
Tx.wg.copy_async(Dreg_wg[:, :], tmem[:, :BLK_N])
T.ptx.tcgen05.wait.ld()
```

[L160-L162](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L160-L162) 收尾三步：`Tx.cast` 把 fp32 逐元素转成 fp16；`m_thr = m_st + warp_id * 32 + lane_id` 算出本线程负责的输出行号（0..127 与 `tid_in_wg` 一一对应）；`Tx.copy` 每线程把自己那一行 `BLK_N` 个 fp16 写进 GMEM 的 `D`：

```python
Tx.cast(Dreg_f16[:], Dreg[:])
m_thr = T.meta_var(m_st + warp_id * 32 + lane_id)
Tx.copy(D[m_thr, n_st : n_st + BLK_N], Dreg_f16[:])
```

**阶段 ⑤：释放 TMEM**

[L164-L168](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L164-L168)：先 `cta_sync` 确认所有线程都读完了 TMEM，再按 u7-l3 的生命周期顺序先 `relinquish_alloc_permit`（交出继续分配的许可）后 `dealloc`（归还 512 列）：

```python
T.cuda.cta_sync()
if warp_id == 0:
    T.ptx.tcgen05.relinquish_alloc_permit(cta_group=1)
    T.ptx.tcgen05.dealloc(tmem_addr[0], n_cols=512, cta_group=1)
```

最后 [L170-L173](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L170-L173) 返回构造好的 `PrimFunc`，并且章节明言：后面所有 GEMM 章节都以这个版本为起点，逐步加 K 循环、更多输出 tile、TMA 与 warp specialization。

#### 4.2.4 代码实践

**实践目标**：合上讲义，仅凭 [chapter_intro_tirx/index.md:L84-L171](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L84-L171) 的源码，独立完成四阶段标注与两类调用清单。

**操作步骤**：

1. 打开章节源码（或本地构建的书页），从 `def hgemm_v1` 开始逐行阅读。
2. 为每一行设备代码（`T.device_entry()` 之后到 `return kernel` 之前）标注它属于哪个阶段，填入下表：

   | 阶段 | 对应源码行（章节文件行号） | 关键内容 |
   | --- | --- | --- |
   | ① 分配 SMEM/TMEM | 110–135 | SMEMPool、mbarrier.init、tcgen05.alloc、decl_buffer |
   | ② 拷贝 A/B | 138–140 | Tx.cta.copy ×2 + cta_sync |
   | ③ 发起 MMA | 143–151 | elect_sync、Tx.gemm_async、commit、try_wait |
   | ④ 读回写 GMEM | 154–162 | Tx.wg.copy_async、wait.ld、Tx.cast、Tx.copy |
   | ⑤ 释放 TMEM | 165–168 | cta_sync、relinquish、dealloc |

3. 把所有调用分拣进两张清单（答案见 4.3.3 的汇总表，先自己列再对照）：
   - **tile 操作清单**：所有 `Tx.` 前缀的调用；
   - **底层辅助调用清单**：所有 `T.ptx.` / `T.cuda.` 前缀的调用。
4. 有 Blackwell GPU 时，可把章节 [L181-L205](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L181-L205) 的验证脚本与内核一起存成 `.py` 文件运行，看到 `PASS` 后再回头做标注（编译细节属 u9-l2）。注意内核必须写在文件里，不能放进 `python -c`。

**需要观察的现象**：标注过程中你会发现——两条 `Tx.cta.copy` 之间夹着一条 `cta_sync`？不对，`cta_sync` 在两条拷贝**之后**；而 `Tx.gemm_async` 之前没有显式 sync 之外的额外动作，靠的正是那条 CTA 栅栏。再数一数 `T.cuda.cta_sync()` 总共出现了几次（3 次：L126、L140、L165），分别守护「init 发布」「拷贝完成」「TMEM 读毕」。

**预期结果**：得到一张 86 行设备代码的完整归属表；两张清单分别有 6 处 `Tx.*` 调用点（5 种操作，`Tx.cta.copy` 出现两次）与约 11 种底层辅助调用（详见 4.3.3）。无 GPU 环境下本实践为源码阅读型，全部可完成；运行验证部分**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：这个内核有多少个线程参与执行？A、B、D 各是多大、各占多少字节？

**参考答案**：1 个 CTA 内 1 warpgroup × 4 warp × 32 lane = 128 个线程。A、B 均为 `128×64` fp16，各 \(128 \times 64 \times 2 = 16\,\text{KB}\)；D 为 `128×128` fp16，\(128 \times 128 \times 2 = 32\,\text{KB}\)。

**练习 2**：`Tx.gemm_async` 的 `accum=False` 在这个内核里为什么成立？如果之后加入 K 循环（u11-l3 的 Step 2），这个标志应该怎么变？

**参考答案**：本版没有 K 循环，`Tx.gemm_async` 只执行一次、TMEM 累加器从零写入，所以 `accum=False`（对应 u7-l1 的「首步 accum=0 写入」）。加入 K 循环后，同一块 TMEM 要被多个 K 块依次累加，除第一个 K 块外都应 `accum=True`（首步写入、其后累加）。

**练习 3**：`Tx.gemm_async` 之前为什么必须有 `T.cuda.cta_sync()`（L140）？

**参考答案**：A/B 的 SMEM 拷贝由全 CTA 128 个线程协作完成，而 MMA 只由 warp 0 中 elect 出的**一个**线程发起。发起线程自己写完的那份数据并不代表其他 127 个线程也写完了；`cta_sync` 汇合全体线程，保证发起 MMA 时 SMEM 里的 A/B tile 已完整。这与章节 GEMM 章末对 Step 1 的练习是同一问题（u11-l2 会再遇到）。

### 4.3 模块三：三个核心 tile primitive 与底层辅助调用的分工

#### 4.3.1 概念说明

TIRx 内核里的调用天然分两层：

- **tile 操作（`Tx.*`）**：以整块 tile 为单位声明工作。作者只说「把这块搬过去」「算这块乘加」「把累加器分发到寄存器」，由谁执行（scope）、数据怎么摆（layout）、走哪条硬件路径（dispatch）由 tile 操作的名称、参数与布局共同表达，编译器据此展开成线程级代码。
- **底层 PTX 辅助调用（`T.ptx.*`、`T.cuda.*`）**：直接对应单条 PTX 指令或 CUDA 级原语，负责 tile 操作管不到的「秩序」——TMEM 分配与释放、mbarrier 初始化与等待、fence 排序、CTA 栅栏、elect 等。

本内核三个最核心的 tile primitive 恰好对应三个不同的执行 scope：

| tile 操作 | 作用 | 执行者（scope） | 底层实现（dispatch） |
| --- | --- | --- | --- |
| `Tx.cta.copy` | GMEM→SMEM 拷贝 A/B tile | 整个 CTA 的 128 线程协作 | 普通线程访存路径（本版；后续可改派 TMA） |
| `Tx.gemm_async` | 完整 `128×128×64` tile 乘累加 | warp 0 中 `elect_sync` 选出的 1 个线程发起，硬件执行 | `dispatch="tcgen05"` → `tcgen05.mma`，展开为 K/16=4 条指令 |
| `Tx.wg.copy_async` | TMEM 累加器 → 各线程寄存器 | 整个 warpgroup 的 128 线程集体 | warp 集体的 `tcgen05.ld`，需 `wait.ld` |

另有三个「小」tile 操作承担收尾：`Tx.cast`（fp32→fp16 逐元素转换）、`Tx.copy`（每线程一行写回 GMEM）。它们同样是 `Tx.` 前缀的 tile 操作，只是章节建议先聚焦前三个（L68）。

#### 4.3.2 核心流程

三类调用的协作关系可以画成一张分工图：

```text
                 ┌────────────── tile 操作（声明"做什么"） ──────────────┐
                 │  Tx.cta.copy   Tx.gemm_async   Tx.wg.copy_async      │
                 │  Tx.cast       Tx.copy                              │
                 └──────────────────────┬───────────────────────────────┘
                                        │ 需要：资源 + 秩序
                 ┌──────────────────────┴───────────────────────────────┐
                 │ 底层辅助调用（保障"能做、做对、做完"）                  │
                 │  资源：tcgen05.alloc / relinquish_alloc_permit /     │
                 │        dealloc                                        │
                 │  秩序：mbarrier.init / try_wait、tcgen05.commit、    │
                 │        tcgen05.wait.ld、elect_sync、                  │
                 │        fence.proxy_async / fence.mbarrier_init、     │
                 │        cta_sync                                       │
                 └───────────────────────────────────────────────────────┘
```

每条 tile 操作的执行都嵌在这些辅助调用建立的「脚手架」里：alloc 提供目的地，init 提供信号对象，fence/sync 提供可见性顺序，commit/try_wait/wait 提供完成检测，dealloc 收回资源。

#### 4.3.3 源码精读

章节在「Scope, Layout, and Dispatch」一节回到内核本身（[chapter_intro_tirx/index.md:L209-L228](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L209-L228)），逐条说明三要素如何控制同一个内核。其中 scope 一段（[L222](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L222)）正是本模块三行表格的出处：

> `Tx.cta.copy(...)` is executed cooperatively by the entire CTA... `Tx.gemm_async(...)` is guarded by both `warp_id == 0` and `elect_sync()`, leaving one elected thread to issue it... `Tx.wg.copy_async(...)` then cooperatively distributes the TMEM accumulator across the registers of all 128 threads in the warpgroup.

layout 一段（[L224](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L224)）指出 A/B 用 128B swizzle 进 SMEM、累加器用 `TLane/TCol` 映射、寄存器视图用 `tid_in_wg` 一人一行，并且**凡生产或消费同一 tile 的操作必须对每个元素的物理位置达成一致**。dispatch 一段（[L226](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L226)）说明 `dispatch="tcgen05"` 选定 Blackwell Tensor Core 路径，且本版的 GMEM→SMEM 拷贝由普通线程执行、后续版本会改派 TMA——这是同一个 tile 操作换 dispatch 的第一个伏笔。

这一节配有一个交互演示，正文以 iframe 嵌入（[chapter_intro_tirx/index.md:L215-L220](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L215-L220)），源文件是 [_extra/demo/tirx_dispatch.html](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/tirx_dispatch.html)。它的实现很直白：脚本把内核关键行连同所属要素存进 `LINES` 数组（[_extra/demo/tirx_dispatch.html:L62-L99](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/tirx_dispatch.html#L62-L99)），每行标 `"scope"`、`"layout"`、`"dispatch"` 或 `null`；点击按钮后 CSS 把非当前要素的行降为 30% 透明度、当前要素的行加高亮背景（[L32-L37](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/tirx_dispatch.html#L32-L37)），并在下方显示对应的解说文字（`EXPL`，[L100-L105](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/tirx_dispatch.html#L100-L105)）。演示里被标为 `dispatch` 的只有一行——`dispatch="tcgen05"` 那个参数（[L89-L92](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/tirx_dispatch.html#L89-L92)），这本身就说明：dispatch 在 TIRx 里是一个**显式参数**，而不是藏在指令名里的副作用。

作为 4.2.4 实践的答案汇总，下面是 `hgemm_v1` 的完整两类调用清单（行号均指章节文件）：

**tile 操作（`Tx.*`，共 6 个调用点、5 种操作）**

| 调用 | 行号 | 阶段 |
| --- | --- | --- |
| `Tx.cta.copy`（A） | [L138](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L138) | ② |
| `Tx.cta.copy`（B） | [L139](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L139) | ② |
| `Tx.gemm_async` | [L145-L148](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L145-L148) | ③ |
| `Tx.wg.copy_async` | [L158](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L158) | ④ |
| `Tx.cast` | [L160](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L160) | ④ |
| `Tx.copy` | [L162](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L162) | ④ |

**底层 PTX/CUDA 辅助调用（共 11 种、13 个调用点）**

| 调用 | 行号 | 职责 |
| --- | --- | --- |
| `T.ptx.mbarrier.init` | [L121](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L121) | 初始化 mbarrier（期望 1 次到达） |
| `T.ptx.tcgen05.alloc` | [L122](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L122) | 分配 512 列 TMEM，写回基地址 |
| `T.ptx.fence.proxy_async` | [L124](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L124) | 通用/异步代理间可见性 |
| `T.ptx.fence.mbarrier_init` | [L125](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L125) | barrier 初始化的顺序保证 |
| `T.cuda.cta_sync`（×3） | [L126](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L126)、[L140](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L140)、[L165](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L165) | CTA 栅栏（init 发布 / 拷贝完成 / 读毕） |
| `T.ptx.elect_sync` | [L144](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L144) | 在 warp 内选出唯一线程 |
| `T.ptx.tcgen05.commit` | [L149](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L149) | 把异步 MMA 挂到 mbarrier |
| `T.ptx.mbarrier.try_wait` | [L151](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L151) | 等待 MMA 完成相位 |
| `T.ptx.tcgen05.wait.ld` | [L159](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L159) | 等待 TMEM 读回到寄存器 |
| `T.ptx.tcgen05.relinquish_alloc_permit` | [L167](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L167) | 交出 TMEM 分配许可 |
| `T.ptx.tcgen05.dealloc` | [L168](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L168) | 释放 512 列 TMEM |

此外还有一类**结构性 API**（既非 tile 操作也非 PTX 辅助）：`T.prim_func`、`T.device_entry`、`T.cta_id / T.warpgroup_id / T.warp_id_in_wg / T.lane_id`、`T.SMEMPool` 及 `pool.alloc / move_base_to / commit`、`T.decl_buffer`、`T.alloc_local`、`Dreg.view`、`T.meta_var`。它们负责定义函数结构、暴露线程坐标、组织内存资源，是两类调用的共同地基。

#### 4.3.4 代码实践

**实践目标**：用交互演示自查你对「哪些行被哪个要素控制」的判断。

**操作步骤**：

1. 用浏览器直接打开仓库中的 `_extra/demo/tirx_dispatch.html`（它是自包含的，只相对引用同目录上一层的 `viz-base.css` / `viz-base.js`，仓库里都存在）；或按 u1-l2 的方法本地构建书站后在页面上操作。
2. 依次点击 `Scope`、`Layout`、`Dispatch` 三个按钮，观察代码区高亮行的变化与底部解说文字。
3. 在点击之前先预测：`pool.alloc(..., layout=A_layout)` 这行会被哪个要素点亮？`if T.ptx.elect_sync()` 呢？`dispatch="tcgen05"` 呢？
4. 对照 `LINES` 数组源码（[_extra/demo/tirx_dispatch.html:L62-L99](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/tirx_dispatch.html#L62-L99)）核对你的预测，特别注意哪些行被标为 `null`（不属于任何单一要素，例如 `T.cuda.cta_sync()`）。

**需要观察的现象**：Scope 点亮的是守卫与 tile 操作行（`Tx.cta.copy`、`if warp_id == 0` / `elect_sync`、`Tx.wg.copy_async`）；Layout 点亮的是 `mma_shared_layout`、`pool.alloc(..., layout=...)`、`decl_buffer(..., layout=TileLayout(...))`；Dispatch 只点亮 `dispatch="tcgen05"` 一处。线程坐标行（`warp_id = T.warp_id_in_wg([4])`）被刻意标注为「thread/lane id (not scope)」——坐标本身不是 scope，守卫才是。

**预期结果**：你能对演示中每一行说出「它属于哪个要素、为什么」，并且理解 `cta_sync` 这类纯同步调用不专属于任何要素。本实践无需 GPU，纯浏览器操作。

#### 4.3.5 小练习与答案

**练习 1**：`Tx.cta.copy`、`Tx.gemm_async`、`Tx.wg.copy_async` 三个操作的执行者分别是谁？请同时说出「谁发起」与「谁真正干活」。

**参考答案**：`Tx.cta.copy` 由整个 CTA 的 128 个线程协作执行，发起者就是全体参与者；`Tx.gemm_async` 由 warp 0 中 `elect_sync` 选出的一个线程**发起**，真正干活的是 Tensor Core 硬件（`tcgen05.mma` 单线程语义、硬件完成整个矩阵乘累加）；`Tx.wg.copy_async` 由整个 warpgroup 的 128 个线程**集体**执行（warp 集体的 `tcgen05.ld`），硬件按各线程 lane 分发数据——没有哪个线程单独发起它。

**练习 2**：如果把阶段 ② 的两条 `Tx.cta.copy` 改为 TMA 派发（后续 Step 4 的做法），除了换函数名，还要按三要素补齐什么？

**参考答案**：scope 上，TMA 由**单个线程**提交（不能再靠 128 线程分摊搬运），需要 `elect` 出发起者；layout 上，SMEM 缓冲与 TMA 描述符（tensor map）必须描述同一物理排布（swizzle 模式一致），并遵守 box 最内维不超过 swizzle 宽度的约束（u6-l1/u6-l2）；dispatch 上，等待方式从 `cta_sync` 换成 mbarrier 的 `expect_tx` 字节计数与 `try_wait`（u6-l3、u8-l1）。这也印证了演示解说里的那句话：换 dispatch 是否合法，取决于目标实现是否支持该操作的 scope 与操作数布局。

**练习 3**：`Tx.cast` 和 `Tx.copy` 为什么也算 tile 操作，而不是普通逐元素语句？

**参考答案**：它们都以 `Tx.` 前缀出现、以 tile（`Dreg_f16[:]`、`D[m_thr, n_st:n_st+BLK_N]`）为操作数、带 scope 语义（`Tx.cast` 在本内核中由全 CTA 各自执行自己的片段，`Tx.copy` 由每线程写自己负责的一行），并会被编译器结合布局信息展开成具体的向量访存与转换指令——符合 tile 操作「以整块为单位声明工作」的定义，只是规模比前三个小。

## 5. 综合实践

**任务：写一份《hgemm_v1 解剖报告》。** 把本讲三个模块串成一份可长期查阅的文档，建议直接存进你的学习笔记仓库：

1. **四阶段标注表**：按 4.2.4 的表格格式，把 L101–L168 的每一行设备代码归入五个阶段（①分配 ②拷贝 ③计算 ④回写 ⑤释放），并给每行写一句中文说明。
2. **两类调用清单**：列出 6 个 `Tx.*` 调用点与 11 种底层辅助调用（对照 4.3.3 的汇总表自查，漏一处都算不完整）。
3. **三要素对照表**：为每个 tile 操作填 scope / layout / dispatch 三列（例如 `Tx.gemm_async`：scope=warp 0 中 elect 出的单线程；layout=A/B 为 SMEM 128B swizzle、目的为 TMEM `TLane/TCol` 恒等映射；dispatch=`"tcgen05"`）。
4. **一个推演实验**：不改代码，回答：若 `K` 从 64 变为 128（同时 `A_layout/B_layout` 的 tile 形状随之变为 `(128,128)`），编译器要展开几条 `tcgen05.mma`？依据是什么？
   - 参考答案：8 条。依据是章节 L59——每条底层指令沿 K 前进 16 个元素，条数为 \(\lceil K/16 \rceil = \lceil 128/16 \rceil = 8\)。注意这只是指令数推演；布局与 SMEM 容量是否需要随之调整，留待 GEMM 章节验证，此处标注「待本地验证」。
5. **（可选，需 Blackwell GPU）运行验证**：把内核与章节 L181–L205 的验证脚本存为 `.py` 文件运行，确认打印 `PASS`，并在报告里附上最大误差数值。无 GPU 环境则注明「源码推演完成，运行待本地验证」。

这份报告将直接服务于后续讲义：u9-l2 在它上面加编译与 IR 检视，u9-l3 深挖三要素，单元十一以后每一步 GEMM 优化都从这份骨架出发。

## 6. 本讲小结

- TIRx（Tensor IR next）是 Python DSL，把散落在 CUDA/PTX 中 intrinsic 参数、地址计算与编码约定里的三个决策——scope（谁执行）、layout（数据怎么摆）、dispatch（走哪条硬件路径）——显式表达为结构化 IR；它不隐藏硬件，只是给硬件选择一个可检视的语法位置。
- `hgemm_v1` 计算单 tile 的 `D = ABᵀ`（A/B 为 128×64、D 为 128×128，单 CTA、128 线程），四阶段为：分配 SMEM/TMEM → `Tx.cta.copy` 拷 A/B → `Tx.gemm_async` 发起 MMA → `Tx.wg.copy_async` 读回寄存器并写 GMEM，最后释放 TMEM。
- 一条 `Tx.gemm_async` 声明完整的 `128×128×64` tile GEMM，编译器按 `⌈K/16⌉` 展开为 4 条 `tcgen05.mma`；`accum=False` 因为没有 K 循环复用累加器。
- 三个核心 tile primitive 对应三个 scope：`Tx.cta.copy` 全 CTA 协作、`Tx.gemm_async` 单线程发起（`warp_id==0` + `elect_sync`）、`Tx.wg.copy_async` 全 warpgroup 集体；另有 `Tx.cast`、`Tx.copy` 承担收尾。
- 底层辅助调用负责「资源与秩序」：TMEM 的 alloc/relinquish/dealloc，mbarrier 的 init/commit/try_wait，`tcgen05.wait.ld`，两类 fence，以及 3 处 `cta_sync`（init 发布、拷贝完成、TMEM 读毕）各守一道交接。
- 同一 tile 操作可以换 dispatch：本版 GMEM→SMEM 走普通线程路径，后续版本改派 TMA——这正是 TIRx 三要素设计的直接收益。

## 7. 下一步学习建议

- **下一讲 u9-l2「编译与验证 TIRx 内核」**：走通 `tvm.compile(..., tir_pipeline="tirx")` 与 PyTorch 参考断言的完整回路，并用 `kernel.show()` / `kernel.script()` / `ex.mod.imports[0].inspect_source()` 对比 lowering 前后的两级代码——验证你本报告里「一条 tile 操作展开成 4 条 MMA」的推演。
- **u9-l3「Scope、Layout、Dispatch 三要素」**：把本讲 4.3 的初步分类系统化，学习编译器如何由三要素推出具体实现。
- **延伸阅读**：章节末尾（[chapter_intro_tirx/index.md:L254-L256](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L254-L256)）指向 `TileLayout` 专章（对应单元十）与 GEMM 章节系列；语言参考（`tirx_guide/language_reference/`）可在你写内核遇到语法疑问时按需查阅。
