# tcgen05.mma 的执行方式与 TMEM 累加器

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `tcgen05.mma` 的操作数各来自哪里（A/B 来自 SMEM、C/D 累加器在 TMEM），并解释它"单线程发起、硬件执行整个 tile"的语义。
2. 逐字段解释一条常见 `tcgen05.mma` 指令的操作数与限定符，特别是 `enable-input-d`（即 accum 标志）与 `idesc` 的作用。
3. 解释 K 维逐步累加时 accum 标志如何变化（第一次 `accum=0` 写入，之后 `accum=1` 累加），以及每次 K 步进更新的是哪个 TMEM 区域。
4. 说出 MMA 完成信号的传递方式：`tcgen05.commit` 把已发出的异步操作挂到 mbarrier 上，消费者 `try_wait` 相位后再用 `tcgen05.fence::after_thread_sync` 排序，才能 `tcgen05.ld` 读回。

## 2. 前置知识

本讲是「Blackwell Tensor Core 与 TMEM」单元的第一讲，直接建立在前两讲之上：

- **u5-l3（Blackwell：累加器进入 TMEM 与 scale factor）**：你已经知道 Blackwell 沿用 Hopper 的矩阵描述符从 SMEM 读 A/B，而 C/D 累加器搬进了 TMEM，寄存器 fragment 退居 epilogue 边界。本讲把这条数据路径展开成指令级的细节。
- **u6-l2（3D TMA 与 128B swizzle 行布局）**：你已经掌握 swizzle atom（8 行 × 128B）与 "tensor map、SMEM 布局与 MMA 指令必须描述同一物理排布" 的一致性纪律。本讲中 A/B 描述符解读的正是这块 SMEM。

此外还需要几个更早的概念，简单回顾：

| 概念 | 一句话回顾 |
| --- | --- |
| 操作 scope（u2-l1） | 谁发起、谁执行、谁受益可以分离；`tcgen05.mma` 是"单线程发起、CTA 级受益"的典型 |
| TMEM 二维地址空间（u2-l2、u4-l2） | 128 Lane × 最多 512 Col、每格 32 bit；坐标记作 \( \text{TLane}, \text{TCol} \)，Lane 是数据侧坐标而非线程 laneid |
| mbarrier 相位（u6-l3） | 一个 mbarrier 同时维护线程到达计数与在途字节计数；消费者用 `try_wait(phase)` 等"离开指定相位" |
| Hopper 矩阵描述符（u5-l2） | 64 位寄存器值，装着 start address、ldo、sdo、base offset 与 swizzle 模式，是"找矩阵的寻址配方" |

**术语提示**：本章说 "C/D" 时，D 是输出、C 是被读入的旧累加值；在 `accum=1` 时两者指向同一块 TMEM。书中图示常把累加器直接标成 C，本文沿用。

## 3. 本讲源码地图

本仓库是一本教材（Sphinx 站点），"源码"主要是章节正文、交互演示与书图脚本：

| 文件 | 作用 |
| --- | --- |
| [chapter_tensor_cores/index.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tensor_cores/index.md) | 本讲的精读对象：Blackwell Tensor Core 一章，讲 tcgen05.mma 的发起、TMEM 累加器映射、cta_group 与 block-scaled |
| [_extra/demo/tcgen05_intro.html](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/tcgen05_intro.html) | 配套交互演示：默认 M=128、N=16，点击 K iteration 观察部分积在 TMEM 中累加，底部展示描述符与 PTX 指令 |
| [chapter_gemm_basics/index.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md) | 补充材料：GEMM Step 1/Step 2 的 TIRx 内核真实代码，展示 `Tx.gemm_async` + `tcgen05.commit` + `try_wait` 的完整用法 |
| [chapter_layout_generations/index.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_layout_generations/index.md) | 补充材料：三代 Tensor Core 数据路径对比，含 Blackwell 描述符输入路径的概述 |
| [img/mma_cg1_m128.svg](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/mma_cg1_m128.svg) 等四张图 | `cta_group` × M 四种配置的 TMEM 累加器映射图（本讲只细看 M=128 一张，其余留给下一讲） |

## 4. 核心概念与源码讲解

### 4.1 模块一：tcgen05.mma 执行

#### 4.1.1 概念说明

`tcgen05.mma` 是 Blackwell 的 Tensor Core 矩阵乘累加指令。它解决的问题是：**让一个完整矩阵 tile 的乘加成为一条指令级操作，而不需要每个线程各自提交一份标量乘加**。

它与前两代的根本差别在于发起方式：

- Ampere `mma.sync`：整个 warp 协同执行，每个线程持有 A/B/C 的寄存器 fragment；
- Hopper `wgmma.mma_async`：warpgroup（4 warp）协同发起；
- Blackwell `tcgen05.mma`：**单线程语义**——一个被选出的线程发出指令，硬件启动整个 tile 级 MMA，其余线程不再各自提交同一指令的副本。

注意"单线程发起"不等于"单线程计算"：硬件仍然按 SMEM 操作数布局与 TMEM 累加器布局执行 tile 级协作运算。如果 128 个线程都发一遍，同一个计算会被启动 128 次（这是真实内核里用 `elect_sync` 只留一个线程的原因，见 4.1.3）。

对本讲考虑的路径（A、B 都在 SMEM、不做 block scaling），一条常见指令形式是：

```text
tcgen05.mma.cta_group.kind
    [d-tmem], a-desc, b-desc, idesc,
    {disable-output-lane}, enable-input-d {, scale-input-d};
```

即：操作数里**根本没有寄存器**——D 用 TMEM 地址给出，A/B 用两个共享内存描述符给出，M/N/K 等指令参数打包在 `idesc` 里。

#### 4.1.2 核心流程

一次 `tcgen05.mma`（以 `cta_group::1`、`.kind::f16`、M=128、N=16、K=16 为例）的执行过程：

1. **选出唯一发起线程**：内核通常写 `if warp_id == 0: if T.ptx.elect_sync(): ...`，warp 内一个 lane 当选。
2. **准备三样东西**：TMEM 累加器起始地址 `d-tmem`；描述 A、B 在 SMEM 中位置与布局的 `a-desc`、`b-desc`；装着 M/N/K、数据类型、major 模式等参数的 `idesc`。
3. **硬件执行 tile 级 MMA**：Tensor Core 按 SMEM 布局读 A/B 切片，按 TMEM 布局把结果写进累加器区域。
4. **指令异步返回**：发出 ≠ 完成，完成信号问题留给模块三（commit 与 mbarrier）。

K 维累加的数学形式。设第一次步进（\(k=0\)）用 `accum=0`：

\[ C[0{:}128,\,0{:}16] = A[0{:}128,\,0{:}16] \times B[0{:}16,\,0{:}16] \]

之后每次步进（\(k = 16, 32, \dots\)）用 `accum=1`：

\[ C[0{:}128,\,0{:}16] \mathrel{+}= A[0{:}128,\,k{:}k{+}16] \times B[k{:}k{+}16,\,0{:}16] \]

`enable-input-d`（true 时即 `accum=1`）决定硬件算 \(D = A \times B + D\) 还是 \(D = A \times B\)。

#### 4.1.3 源码精读

**指令的发出语义与一般形式。** 章节正文明确写出单线程语义与指令模板：

- [chapter_tensor_cores/index.md:L46-L58](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tensor_cores/index.md#L46-L58)：说明 `tcgen05.mma` 是 tile 级指令、"One elected thread issues the instruction"，并给出 A/B 均在 SMEM、无 block scaling 时的常见指令形式（含 `[d-tmem], a-desc, b-desc, idesc, {disable-output-lane}, enable-input-d`）。
- [chapter_tensor_cores/index.md:L60-L72](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tensor_cores/index.md#L60-L72)：字段角色表——`cta_group` 选当前 CTA 或 CTA 对；`kind` 选 A/B 数据类型族；`d-tmem` 给 C/D 累加器的 TMEM 起始地址；`a-desc`/`b-desc` 描述 A/B 在 SMEM 中的地址与布局；`idesc` 指定 M、N、K、具体 A/B 与 C/D 类型、operand major 模式等；`disable-output-lane` 选择不更新的 TMEM lane；`enable-input-d` 就是交互图里的 accum（false 算 \(D=A{\times}B\)，true 算 \(D=A{\times}B+D\)）。
- [chapter_tensor_cores/index.md:L73-L82](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tensor_cores/index.md#L73-L82)：`.kind::f16` 标识 16 位浮点族，`idesc` 再细分 f16/bf16 与 C/D 是 f16 还是 f32；`cta_group` 不改变单线程发起语义；SMEM 布局决定 Tensor Core 怎么读 A/B，TMEM 布局决定累加器怎么落到 lane/column——两个布局分属不同存储空间。

**演示里的描述符与 PTX 形式。** 交互演示底部把这两样画成了卡片：

- [_extra/demo/tcgen05_intro.html:L135-L145](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/tcgen05_intro.html#L135-L145)：SMEM 描述符卡片写作 `smem_desc = base_addr | leading_byte_offset | stride_byte_offset | start_addr_off`；PTX 卡片写作 `tcgen05.mma.cta_group::1.kind::f16 [taddr], a_desc, b_desc, idesc, {mask0..mask3}, enable;`。对照 u5-l2 的 Hopper 描述符五字段，你会发现是同一族"寻址配方"，Blackwell 把它沿用为 A/B 的输入路径（见 [chapter_layout_generations/index.md:L227-L236](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_layout_generations/index.md#L227-L236)：Blackwell 保留 Hopper 的描述符输入路径，某些模式还允许 A 来自 TMEM）。

**真实内核里怎么发出这条指令。** GEMM Step 1 的 TIRx 代码是全书第一次实际发出 MMA：

- [chapter_gemm_basics/index.md:L112-L122](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L112-L122)：`if warp_id == 0` 只留 warp 0，`T.ptx.elect_sync()` 再从中选一个 active lane，最终**恰好一个线程**执行 `Tx.gemm_async(tmem[:, :BLK_N], Asmem[:, :], Bsmem[:, :], accum=False, dispatch="tcgen05", cta_group=1)` 与 `T.ptx.tcgen05.commit(...)`；正文并解释"若 128 个线程都发，计算会被启动 128 次"。
- [chapter_gemm_basics/index.md:L126-L130](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L126-L130)：`Tx.gemm_async` 是 **tile 操作而非一条硬件指令**——tile 沿 K 有 64 个元素，而每条底层 MMA 指令只处理 16 个 K 元素，所以 TIRx 会把它降级为一小串 `tcgen05.mma` 指令；`accum=False` 表示不从 TMEM 读旧的部分和（本步只有一个 tile 操作，不存在更早的和）。

这条"tile 操作 ↔ 指令序列"的对应关系是本讲综合实践的桥梁：TIRx 层的一次 `accum` 参数，落在指令层就是"序列内第一条 accum=0、其余 accum=1"（与 4.2 的逐次累加一致）。

#### 4.1.4 代码实践

**实践目标**：用仓库自带的交互演示，亲眼看到"一次 K 步进读哪些 A/B 切片、accum 何时翻转"。

**操作步骤**：

1. 进入仓库目录，直接用浏览器打开 `_extra/demo/tcgen05_intro.html`（演示只依赖同目录上级的 `../viz-base.css` 与 `../viz-base.js`，均为仓库自带文件，无需服务器、无需 GPU）。若你按 u1-l2 构建过书站，也可在渲染页面的内嵌 iframe 中操作。
2. 保持默认配置：M=128（控件禁用不可改）、N=16、K=16。演示中每个格子代表一个 \(16\times16\) 的元素块。
3. 点击 **K iteration** 栏的 `0`、`1`、`2`、`3` 按钮，逐步前进；也可点 ▷ 按钮自动播放。
4. 每步观察三处：A/B 矩阵中高亮的块、C（TMEM）面板中被更新的区域、以及下方公式框里的 `accum=` 值。

**需要观察的现象**：

- 每点一步，A 面板高亮 **8 个** \(16\times16\) 块（即一个 \(128\times16\) 切片），B 面板高亮 **1 个** \(16\times16\) 块；
- 第 0 步公式框显示 `accum=0`、`C = A[m,k] × B[k,n]`；从第 1 步起显示 `accum=1`、`C += ...`；
- C 面板中被更新的 \(8\times1\) 个块（\(128\times16\) 区域）每一步都相同，只是颜色随累加变深。

**预期结果**：与演示源码一致——[_extra/demo/tcgen05_intro.html:L289-L301](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/tcgen05_intro.html#L289-L301) 中公式 `accum = ST.k === 0 ? 0 : 1`、赋值号在 `=` 与 `+=` 间切换，信息栏给出 "A reads 8 16×16 blocks, B reads 1 16×16 block"；每步 K 固定为 16（见控件 [_extra/demo/tcgen05_intro.html:L83-L88](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/tcgen05_intro.html#L83-L88)）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `tcgen05.mma` 的指令模板里看不到任何通用寄存器操作数，而 Ampere `mma.sync` 必须给出一串寄存器？

**参考答案**：因为 Blackwell 把输入与输出都移出了寄存器：A/B 经 `a-desc`/`b-desc` 直接从 SMEM 读取，C/D 累加器用 `[d-tmem]` 的 TMEM 地址表示。Ampere 的 A/B/C/D 全部以寄存器 fragment 分布在各线程中，所以操作数是一串寄存器。这正是三代演进中"寄存器退居 epilogue 边界"的落点（对照 u5-l1、u5-l3）。

**练习 2**：`kind::f16` 选定了 16 位浮点族之后，为什么还需要 `idesc` 再指定一次 A/B 与 C/D 的具体类型？

**参考答案**：`kind` 只选定"族"（16 位浮点），`idesc` 负责族内的具体组合：A/B 各是 f16 还是 bf16、C/D 是 f16 还是 f32，同时 `idesc` 还携带 M、N、K 与 operand major 模式等指令参数（见字段角色表 [chapter_tensor_cores/index.md:L68-L73](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tensor_cores/index.md#L68-L73)）。书中内核常用 `.kind::f16` + `idesc` 选 f32 累加器，以降低沿 K 累加的舍入误差。

**练习 3**：把 Step 1 中的 `if T.ptx.elect_sync():` 去掉、让 warp 0 的 32 个线程都执行 `Tx.gemm_async`，会发生什么？

**参考答案**：同一个 tile 级计算会被启动 32 次（对整个 CTA 而言是 128 次），累加器被重复写入/累加，结果错误且浪费算力。正文明确指出 "If all 128 threads issued the same operation, the computation would be launched 128 times"（[chapter_gemm_basics/index.md:L122-L124](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L122-L124)）。（实际运行行为待本地验证。）

### 4.2 模块二：TMEM 累加器

#### 4.2.1 概念说明

**累加器（accumulator）**是 GEMM 主循环中生命周期最长的数据：K 维每步进一次，部分积就往同一块累加器上加一次，直到 K 走完才被 epilogue 读走。

Ampere 与 Hopper 把累加器放在每线程寄存器里（寄存器 fragment）。代价是：输出 tile 越大，这些**长寿命 fragment 占用的寄存器越多**，挤压其他数据的寄存器预算。Blackwell 的做法是把长寿命累加器搬进 TMEM：

- TMEM 是二维、CTA 级作用域的片上存储；`sm_100a` 上每个 CTA 有 **128 个 Lane 行 × 512 个 Col 列**，每个 \( \text{Lane}/\text{Col} \) 坐标处一个 32 bit 单元；
- `tcgen05.mma` 反复更新 TMEM 中的累加器；主循环期间累加器**不再占用寄存器**；
- epilogue 最终用 `tcgen05.ld` 把它装回寄存器做类型转换、逐元素运算与写出。

由此内核的职责发生变化：不再管理寄存器 fragment，而是**管理 TMEM 的分配与布局**——MMA 必须把结果写到正确的 TMEM 坐标，epilogue 必须用匹配的布局读回（"写的布局必须等于读的布局"，u5-l3 已建立这条纪律）。

#### 4.2.2 核心流程

累加器的 TMEM 映射由四个选择决定：`cta_group`、M 的大小、A 是 dense 还是结构化稀疏、是普通 `tcgen05.mma` 还是 weight-stationary 的 `.ws`。本模块只看最直接的一种，其余三种留给下一讲（u7-l2）。

**`cta_group::1`、M=128 的直接映射**：一个 CTA 算 128 行的输出 tile，TMEM 恰好有 128 个 Lane 行，于是

\[ \text{TLane} = m, \qquad \text{TCol} = n \]

即累加器行 \(m\) 直接映射到 TMEM Lane \(m\)，N 方向沿 TMEM 列展开。结果占 128 个 Lane 行 × N 个 Col 列（f32 累加时 N 列即 N 个 32 bit 列）。

对 M=128、N=16、f32 累加器：占用 **Lane 0–127、Col 0–15** 这块区域（列起点由 `tcgen05.alloc` 的分配结果决定，演示与正文默认从 0 开始画）。K 维每次步进更新的都是**同一块**区域——只有最后一次步进完成后，区域内才是从数学意义上完整的 C tile：

\[ C = \sum_{j=0}^{K/16-1} A[0{:}128,\,16j{:}16j{+}16] \times B[16j{:}16j{+}16,\,0{:}16] \]

三条使用要点：

1. **N 的合法范围由指令描述符给出**：f16/bf16 路径下 `cta_group::1` 支持 N 从 8 到 256、步长 8；`cta_group::2` 支持 16 到 256、步长 16。
2. **读回必须兼容**：`tcgen05.ld` 要用与写入兼容的 TMEM 地址和 load shape，才能重建出原逻辑形状的 C tile。
3. **`tcgen05.ld` 自身也是异步的**：用目标寄存器之前必须 `tcgen05.wait::ld`（读回与打包细节是 u7-l4 的主题）。

#### 4.2.3 源码精读

- [chapter_tensor_cores/index.md:L100-L108](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tensor_cores/index.md#L100-L108)：正文说明 Ampere/Hopper 累加器在寄存器、fragment 随 tile 增大消耗寄存器；Blackwell 把长寿命累加器移入 TMEM，给出 128 Lane × 512 Col × 32 bit 的规格，以及 `tcgen05.ld` + `wait::ld` 的 epilogue 路径与"内核必须管理 TMEM 分配与布局"的结论。
- [chapter_tensor_cores/index.md:L118-L128](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tensor_cores/index.md#L118-L128)：累加器布局取决于 `cta_group`、M、dense/sparse、是否 `.ws` 四个选择；N 范围约束；`cta_group::1, M=128` 时 "Accumulator row m maps directly to TMEM Lane m, while N extends across TMEM columns"，结果占 128 Lane 行 × N Col 列。
- [chapter_tensor_cores/index.md:L26-L40](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tensor_cores/index.md#L26-L40)：与演示联动的文字说明——第一次迭代 `accum=0` 不读旧累加器直接写入；后续迭代 `accum=1` 累加；**每次 K 迭代更新同一 128×16 TMEM 区域**，该区域只在最后一次迭代完成后才包含最终 C tile。
- [_extra/demo/tcgen05_intro.html:L114-L124](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/tcgen05_intro.html#L114-L124)：演示的 C（TMEM）面板——纵轴标注 "128 Lanes (M)"，横轴标注 "Col (N) — allocated by `tmem.alloc`"，直观呈现"列是分配出来的资源、行就是 Lane"。
- [chapter_gemm_basics/index.md:L434-L436](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L434-L436)：真实内核里累加器的声明——`T.decl_buffer((128, 512), "float32", scope="tmem", layout=TileLayout(S[(128, 512) : (1@TLane, 1@TCol)]))`，用命名轴把逻辑坐标 \((m,n)\) 直接映射到 \((\text{TLane},\text{TCol})\)，与本模块的映射公式一一对应。

其余三种布局先给出索引（细节在 u7-l2 展开）：

- `cta_group::1, M=64`（Layout F）：128 Lane 分四个 32-lane 区，64 行分四组各 16 行放入，映射见 [chapter_tensor_cores/index.md:L134-L153](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tensor_cores/index.md#L134-L153)（`TLane = (m//16)*32 + a + m%16`，对齐量 a 取 0 或 16，两个 M=64 tile 可互补共用同一批列）；
- `cta_group::2, M=256`：M 沿 CTA 对连续切分，偶 CTA 存行 0–127、奇 CTA 存行 128–255（[chapter_tensor_cores/index.md:L168-L176](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tensor_cores/index.md#L168-L176)）；
- `cta_group::2, M=128`（dense A，Layout B）：每 CTA 存 64 行，N 的上下半分别映射到该 CTA Lane 的下半与上半（[chapter_tensor_cores/index.md:L178-L205](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tensor_cores/index.md#L178-L205)）。

#### 4.2.4 代码实践

**实践目标**：亲手算出一个具体 MMA 配置的 TMEM 占用，并与演示核对。

**操作步骤**：

1. 打开 `_extra/demo/tcgen05_intro.html`，保持 M=128，把 N 依次切到 16 / 32 / 64 / 128；
2. 对每个 N，记录 C（TMEM）面板中被点亮的列块数（每块 16 列）；
3. 在纸上对 N=16、f32 累加器写出：占用的 Lane 区间、Col 区间、总单元数（每单元 32 bit）；
4. 再计算：这样一个 tile 若按 Ampere 方式放进寄存器，128 行 × 16 列 f32 均摊到 128 个线程，每线程多少个寄存器？

**需要观察的现象**：N 每翻一倍，C 面板点亮的列块数也翻倍（1→2→4→8 块），Lane 方向始终是整条 128 行；A 面板高亮块数不变（8 块），B 面板高亮块数随 N 变化（1→2→4→8 块）。

**预期结果**：N=16 时占用 Lane 0–127、Col 0–15，共 \(128 \times 16 = 2048\) 个 32 bit 单元；寄存器对比为每线程 \(2048/128 = 16\) 个 f32 寄存器——看似不大，但 N=128 时就是每线程 128 个寄存器，这正是正文说"累加器 tile 越大 fragment 吃寄存器越多"的量化感受（对照 [chapter_tensor_cores/index.md:L100-L104](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tensor_cores/index.md#L100-L104)）。

#### 4.2.5 小练习与答案

**练习 1**：M=128、N=16、K=64、f32 累加器的一条 K 步进链中，第 3 次 MMA 指令（\(k=32\)）读和写的 TMEM 区域分别是什么？

**参考答案**：读与写是同一块区域：Lane 0–127、Col 0–15（`accum=1` 时 D 既是被读的旧累加值也是输出）。第 3 次指令读入的是前两次步进累加出的部分和 \(A_{[:,0:16]}B_{[0:16,:]} + A_{[:,16:32]}B_{[16:32,:]}\)，加上本步的 \(A_{[:,32:48]}B_{[32:48,:]}\) 后写回同区域。

**练习 2**：为什么说 "Every K iteration updates the same 128×16 TMEM region. That region contains the final C tile only after the last iteration completes"（正文原句）对 epilogue 的时机提出了要求？

**参考答案**：因为中间任何时刻区域内都只是**部分和**。epilogue 若在最后一次步进完成前用 `tcgen05.ld` 读回，读到的是不完整的累加结果。所以必须先等 MMA 完成信号（模块三的 commit/mbarrier 协议），这是"完成信号"存在的根本原因。

**练习 3**：TMEM 的 Lane 和线程的 laneid 是一回事吗？

**参考答案**：不是。TMEM 的 Lane 是**数据侧**的二维地址坐标（128 行），累加器行 \(m\) 映射到 TLane \(m\)；线程的 laneid 是执行侧 warp 内线程编号。二者只有在 epilogue 讨论"哪个 warp 能访问哪段 Lane 窗口"时才发生联系（u7-l3 的主题）。

### 4.3 模块三：commit 与完成信号

#### 4.3.1 概念说明

`tcgen05.mma` 发出后**异步执行**：指令发出时结果并未到达 TMEM。于是出现经典的生产者-消费者问题：

- 生产者（发起 MMA 的那个线程）怎么知道硬件做完了？
- 消费者（读累加器做 epilogue 的 warp）在 `tcgen05.ld` 之前怎么确保累加器已写完？

Blackwell 的答案是把完成信号挂在 **mbarrier** 上：

1. 同一个线程可以先发一条或多条 MMA，再执行 `tcgen05.commit`，让一个 mbarrier **追踪该线程此前发出的所有异步 tcgen05 操作**；
2. 硬件完成这些操作后，通过该 barrier 发出到达（arrival）信号；
3. 消费者在 `tcgen05.ld` 之前，先 `try_wait` 这道 barrier 的对应相位，再执行 `tcgen05.fence::after_thread_sync`，把"完成通知"排在后续 TMEM 访问之前——否则 epilogue 可能在累加器还在被更新时就读 TMEM。

注意与 u6-l3 的分工：TMA load 的 mbarrier 靠**在途字节数**归零判完成（`arrive.expect_tx` 登记、complete-tx 扣减）；MMA 的 mbarrier 不记账字节，而是**硬件做完后主动 arrive**。同一个 mbarrier 对象、两种触发路径。

由于这道 barrier 会被 K 循环反复复用，消费者必须跟踪**相位（phase）**：每次成功等待后把等待值异或翻转，否则第二次等待会因相位已过期而立即通过（Step 2 的经典陷阱，见 4.3.3）。

#### 4.3.2 核心流程

一次"发 MMA → 等完成 → 读回"的最小时序（单线程视角 + 消费者视角）：

```text
# 初始化阶段（每个 CTA 一次）
init:   mbarrier.init(mma_bar, count=1)          # 只登记 1 个到达方：MMA 完成信号
        tcgen05.alloc(...)                        # 分配 TMEM 列（u7-l3 详述）

# K 循环内，每步：
issue:  if warp_id == 0 and elect_sync():         # 唯一发起线程
            tcgen05.mma ... enable-input-d(accum) # 第一步 accum=0，其后 accum=1
            tcgen05.commit(mma_bar)               # 把刚发出的 MMA 挂到 barrier
overlap: 其他 warp 此刻可搬数据、准备后续 tile     # MMA 在飞，不等它

wait:   mbarrier.try_wait(mma_bar, phase)         # 等硬件 arrive，barrier 离开当前相位
        phase ^= 1                                # 翻转，供下一次等待

read:   tcgen05.fence::after_thread_sync          # 完成通知排序在 TMEM 访问之前
        tcgen05.ld  ...                           # 累加器 → 寄存器（异步）
        tcgen05.wait::ld                          # 用寄存器前等待 ld 完成
```

相位翻转表（K 循环复用同一道 `mma_bar`）：

| K 迭代 | 传入 `try_wait` 的 phase | MMA 完成后 barrier 所处相位 |
|---|---:|---:|
| 0 | 0 | 1 |
| 1 | 1 | 0 |
| 2 | 0 | 1 |

#### 4.3.3 源码精读

- [chapter_tensor_cores/index.md:L84-L98](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tensor_cores/index.md#L84-L98)：本模块的核心出处——发出 `tcgen05.mma` 只是启动异步操作；同线程执行 `tcgen05.commit` 使一个 mbarrier 追踪其此前发出的异步 tcgen05 操作，硬件完成后经 barrier 发信号；其他 warp 可在 MMA 在飞时搬数据；消费者 `tcgen05.ld` 前必须等相应 mbarrier 并执行 `tcgen05.fence::after_thread_sync` 排序，否则可能在累加器仍被更新时读 TMEM。
- [chapter_gemm_basics/index.md:L112-L120](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L112-L120)：Step 1 的真实代码——`Tx.gemm_async(...)` 后紧跟 `T.ptx.tcgen05.commit(mma_bar.ptr_to([0]), cta_group=1)`，随后 warpgroup 执行 `T.ptx.mbarrier.try_wait(mma_bar.ptr_to([0]), phase_mma)` 再读 TMEM。
- [chapter_gemm_basics/index.md:L425-L428](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L425-L428)：barrier 的初始化——`T.ptx.mbarrier.init(mma_bar.ptr_to([0]), 1)`，到达计数为 1（唯一的到达方就是 MMA 完成信号），与 `tcgen05.alloc` 一起由 warp 0 完成。
- [chapter_gemm_basics/index.md:L356-L376](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L356-L376)：Step 2 对相位复用的完整论述——`accum` 参数控制是否读旧累加器（第一块 `accum=False`，其后每块 `accum=True`）；每次迭代 `Tx.gemm_async` 后跟 `tcgen05.commit(mma_bar)`；barrier 只有在 MMA 完成并报告到达后才离开当前相位；给出上面那张相位表；并解释**漏掉 `phase_mma ^= 1` 的后果**：第二次迭代仍在等相位 0，而 barrier 已进入相位 1，该等待会立即返回，内核可能在第二个 MMA 完成前就读累加器。
- [chapter_gemm_basics/index.md:L450-L459](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L450-L459)：K 循环代码本体——`accum=(i != 0)` 一行同时体现了"首个 K 块写、后续 K 块加"的指令级规则在 tile 级的对应物；`try_wait` 之后紧跟 `phase_mma ^= 1`。
- [chapter_gemm_basics/index.md:L467-L468](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L467-L468)：读回侧的配套等待——`Tx.wg.copy_async(Dreg_wg[:, :], tmem[:, :BLK_N])` 之后必须 `T.ptx.tcgen05.wait.ld()` 才能使用目标寄存器（`tcgen05.ld` 自身异步）。
- [chapter_tensor_cores/index.md:L245-L251](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tensor_cores/index.md#L245-L251)：章末把全章收束为三条衔接条件（目标 CTA/CTA 对正确、生产者输出布局与消费者期望布局一致、消费者使用对应的完成与排序机制）和读懂 tcgen05 的三个问题（用哪个 CTA 的资源、数据映射到哪、下一阶段何时可安全消费）。

#### 4.3.4 代码实践

**实践目标**：以阅读型实践跟踪 Step 2 内核中 `mma_bar` 的完整生命周期，并预演"漏翻相位"故障。

**操作步骤**：

1. 打开 [chapter_gemm_basics/index.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md) 的 Step 2 完整内核（约 L378–L480），用纸笔列出 `mma_bar` 上发生的每一次事件，按时间排序：`init(count=1)` → 第 i 次迭代 `commit` → 硬件完成 arrive → `try_wait(phase_mma)` 通过 → `phase_mma ^= 1` → 下一迭代……
2. 假设 `K=192`（`K_TILES=3`，即 K 循环跑 3 轮），把 L362-L366 的相位表扩展到 3 行，写出每轮"传入 try_wait 的 phase"与"MMA 完成后 barrier 所处相位"。
3. 思考实验：删除 L459 的 `phase_mma ^= 1`，按 L376 的解释推演第 2 轮迭代中 `try_wait` 何时通过、读到的累加器缺了哪一块的部分和。
4. （可选，需 Blackwell GPU + u1-l3 环境）把 Step 2 内核保存为文件编译运行，实际删除 `phase_mma ^= 1` 后观察 `assert_close` 是否失败；无 GPU 则本步标注「待本地验证」，仅完成推演。

**需要观察的现象（推演）**：第 1 步应得到"init → commit₀ → arrive → try_wait(0) 通过 → phase 翻为 1 → commit₁ → ……"的循环；第 2 步扩展表为 (0→1, 1→0, 0→1)；第 3 步应发现删除翻转后第 2 轮的 `try_wait(mma_bar, 0)` 会立即返回（barrier 已在相位 1），epilogue 可能在第 2 个 MMA 完成前读 TMEM，最终 D 缺少第 2 个 K 块的贡献或读到旧值。

**预期结果**：与正文 L360-L376 的描述逐条一致；若做了第 4 步，预期数值断言失败（具体错误模式取决于时序，待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：TMA load 与 `tcgen05.mma` 都用 mbarrier 报完成，二者触发 barrier 的方式有何不同？

**参考答案**：TMA load 侧由单线程 `arrive.expect_tx` 登记期望字节数，TMA 引擎每完成一次搬运执行 complete-tx 扣减在途字节，线程到达计数与字节计数**双双归零**才翻转相位（u6-l3）；MMA 侧不登记字节——`tcgen05.commit` 只是把该线程此前发出的异步 tcgen05 操作与 barrier 关联，硬件做完后**主动 arrive** 一次（barrier 初始化计数为 1，见 [chapter_gemm_basics/index.md:L427](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L427)）。

**练习 2**：为什么 `tcgen05.fence::after_thread_sync` 是必需的，明明消费者已经 `try_wait` 过了？

**参考答案**：`try_wait` 只是让线程观察到 barrier 相位变化（一个同步事件），但它不自动保证后续 TMEM 访问在硬件层面排在完成通知之后。`tcgen05.fence::after_thread_sync` 显式建立这个顺序，防止 epilogue 在累加器仍被更新时读 TMEM（[chapter_tensor_cores/index.md:L92-L98](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tensor_cores/index.md#L92-L98)）。这属于异步同步章节深入展开的协议，本讲先记住"wait 之后还要 fence"。

**练习 3**：一个线程连发 4 条 K 步进的 `tcgen05.mma`（同一 K 块内），只在最后执行一次 `tcgen05.commit`。这 4 条指令都会被追踪到吗？

**参考答案**：会。`tcgen05.commit` 追踪的是该线程**此前发出的**异步 tcgen05 操作的集合，不是只追踪最近一条（[chapter_tensor_cores/index.md:L86-L90](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tensor_cores/index.md#L86-L90)）。这正是 TIRx 把 K=64 的 `Tx.gemm_async` 降级为 4 条 k16 指令后、只需一次 commit 的依据。

## 5. 综合实践

**任务**：对 M=128、N=16、K=64 的 MMA（`cta_group::1`、`.kind::f16`、f32 累加器），把三个模块串成一张完整的"K 步进表"。K 以 16 为一步（每条 `tcgen05.mma` 指令处理 16 个 K 元素），共 4 步。

**操作步骤**：

1. 仿照 4.1.4 打开演示核对直观图景（演示的 K 总长为 128 即 8 步，你只需对照前 4 步的行为模式）。
2. 填写下表（建议先自己填，再对照参考答案）：

| 步 j | k 区间 | 读取的 A 切片（SMEM） | 读取的 B 切片（SMEM） | 更新的 TMEM 区域 | accum（enable-input-d） | 数学含义 |
|---|---|---|---|---|---|---|
| 0 | 0–15 | ？ | ？ | ？ | ？ | \(C^{(1)} = ?\) |
| 1 | 16–31 | ？ | ？ | ？ | ？ | ？ |
| 2 | 32–47 | ？ | ？ | ？ | ？ | ？ |
| 3 | 48–63 | ？ | ？ | ？ | ？ | ？ |

3. 在表下方补写两段文字：
   - **commit 的位置**：这 4 条指令对应 TIRx 的一次 `Tx.gemm_async(..., K 跨 64)`，`tcgen05.commit(mma_bar)` 应出现在什么位置、追踪哪些指令？
   - **等待协议**：commit 之后消费者按什么顺序执行哪三条指令/操作，才能安全读到最终 C tile？
4. （可选，需 Blackwell GPU）把 Step 1 内核（`M=N=128, K=64`，见 [chapter_gemm_basics/index.md:L112-L130](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L112-L130)）的 `BLK_N` 改为 16 后编译运行，用 PyTorch 参考断言验证；无 GPU 则标注「待本地验证」。

**参考答案**：

| 步 j | k 区间 | A 切片 | B 切片 | TMEM 区域 | accum | 数学含义 |
|---|---|---|---|---|---|---|
| 0 | 0–15 | \(A[0{:}128, 0{:}16]\)（8 个 16×16 块） | \(B[0{:}16, 0{:}16]\)（1 块） | Lane 0–127, Col 0–15 | 0 | \(C^{(1)} = A_{[:,0:16]}B_{[0:16,:]}\) |
| 1 | 16–31 | \(A[0{:}128, 16{:}32]\) | \(B[16{:}32, 0{:}16]\) | 同上（同一块） | 1 | \(C^{(2)} = C^{(1)} + A_{[:,16:32]}B_{[16:32,:]}\) |
| 2 | 32–47 | \(A[0{:}128, 32{:}48]\) | \(B[32{:}48, 0{:}16]\) | 同上 | 1 | \(C^{(3)} = C^{(2)} + A_{[:,32:48]}B_{[32:48,:]}\) |
| 3 | 48–63 | \(A[0{:}128, 48{:}64]\) | \(B[48{:}64, 0{:}16]\) | 同上 | 1 | \(C = C^{(3)} + A_{[:,48:64]}B_{[48:64,:]}\)，此刻才是最终 C tile |

依据：每步读一个 \(128\times16\) 的 A 切片与一个 \(16\times16\) 的 B 切片（[chapter_tensor_cores/index.md:L24-L40](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tensor_cores/index.md#L24-L40) 与演示信息栏）；M=128 时行 \(m\) 直映 Lane \(m\)、N 沿列展开（[chapter_tensor_cores/index.md:L122-L128](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tensor_cores/index.md#L122-L128)）；首次 `accum=0`、其后 `accum=1`（[chapter_tensor_cores/index.md:L26-L38](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tensor_cores/index.md#L26-L38)）。

**commit 与等待**：4 条 k16 指令由一次 tile 操作降级而来（[chapter_gemm_basics/index.md:L126](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L126)），唯一发起线程在指令序列之后执行一次 `tcgen05.commit(mma_bar)`，一次追踪全部 4 条（[chapter_tensor_cores/index.md:L84-L90](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tensor_cores/index.md#L84-L90)）。消费者随后：① `mbarrier.try_wait(mma_bar, phase)` 等硬件 arrive（barrier 计数为 1）；② `tcgen05.fence::after_thread_sync` 排序；③ `tcgen05.ld` 读回并在用寄存器前 `tcgen05.wait::ld`。若该 barrier 还要被下一个 K 块复用，`try_wait` 通过后需 `phase ^= 1`（[chapter_gemm_basics/index.md:L356-L376](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L356-L376)）。

## 6. 本讲小结

- `tcgen05.mma` 是**单线程语义**的 tile 级指令：一个被 `elect_sync` 选出的线程发出，硬件执行整个矩阵乘累加；指令模板中没有寄存器操作数——`[d-tmem]` 给 TMEM 累加器地址，`a-desc`/`b-desc` 描述 SMEM 中的 A/B，`idesc` 打包 M/N/K 与类型等参数。
- **TMEM 累加器**：长寿命累加器从寄存器搬进 TMEM（128 Lane × 512 Col × 32 bit，CTA 级）；`cta_group::1` M=128 时行 \(m\) 直映 Lane \(m\)、N 沿列展开；K 维每次步进更新**同一块** TMEM 区域，只有最后一步完成后才是最终 C tile。
- **accum（enable-input-d）与 K 步进**：第一步 `accum=0` 写入 \(D=A{\times}B\)，其后每步 `accum=1` 算 \(D=A{\times}B+D\)；TIRx 的 `Tx.gemm_async` 是 tile 操作，K=64 会降级为 4 条 k16 指令，tile 级的 `accum=False/True` 对应"首个 K 块写、后续 K 块加"。
- **完成信号**：`tcgen05.commit` 把该线程此前发出的异步 tcgen05 操作挂到 mbarrier，硬件完成后主动 arrive（与 TMA 的字节数扣减是同一对象上的两种触发路径）；消费者先 `try_wait(phase)`、再 `tcgen05.fence::after_thread_sync`、然后 `tcgen05.ld` 并 `wait::ld`；K 循环复用 barrier 必须翻转相位，漏翻会让等待提前通过。
- 章末的三问可以作为读任何 tcgen05 代码的检查表：**这条指令用哪个 CTA 的资源？把数据映射到了哪？下一阶段何时可安全消费？**

## 7. 下一步学习建议

- **下一讲（u7-l2）**：`cta_group::1/::2` 与 block-scaled MMA——把本讲只列出索引的三种累加器映射（Layout F、M=256 跨 CTA 对切分、Layout B）逐一展开，并讲 SFA/SFB 在 CTA 对中的 sharding/replication；配套图脚本 `img/scripts/gen_mma_layouts.py` 可以重新生成四种布局图。
- **u7-l3**：TMEM 的分配生命周期（`tcgen05.alloc` / `relinquish_alloc_permit` / `dealloc`）与各 warp 可访问的 32-lane 窗口——回答"本讲的 Col 0–15 是从哪里分配出来的"。
- **u7-l4**：`tcgen05.ld/st` 的 shape 与 repeat、16-bit 打包/解包、`wait::ld`/`wait::st`——本讲 epilogue 侧一笔带过的读回细节在那里展开。
- 想看完成信号在真实内核里如何被流水线化，可跳读 `chapter_gemm_async/index.md`（Step 4/5：TMA + 双缓冲）与 `chapter_async_barriers/index.md`（phase 与 stage 复用的系统论述）。
