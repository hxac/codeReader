# u11-l3 Step 2：K 循环累加

## 1. 本讲目标

本讲在 Step 1 的单 tile 正确性基线上**只加一个机制**：K 维循环。Step 1 只能算 \(K=64\)（恰好一个 K tile 宽），Step 2 让同一个内核处理任意（64 的倍数）大小的 K。学完后你应该能够：

1. 说出 `hgemm_v2` 相对 `hgemm_v1` 的**全部增量**：一个 `T.serial` K 循环、一个 `accum=(i != 0)` 分支、一行 `phase_mma ^= 1`——scope、layout、dispatch 三要素全部不变。
2. 推导 K 分块累加的数学：\(D\) 的每个元素是 \(K_{\text{TILES}}\) 个部分和的连加，分块求和与一次求和在数学上等价、在 fp32 累加下数值稳定。
3. 解释 **TMEM 累加器的长寿命复用**：循环外分配一次、循环内被每个 chunk 依次读-改-写、循环外读回释放；`accum=False` 写入初值、`accum=True` 累加。
4. 讲清 **barrier 相位翻转**的必要性：一道 `mma_bar` 被复用 \(K_{\text{TILES}}\) 次，每次完成硬件自动翻入下一相，第 \(i\) 轮的 `try_wait` 必须等相位 \(i \bmod 2\)。
5. 完成本讲必答题——章末练习 2：**推演（或在 GPU 上复现）删除 `phase_mma ^= 1` 后内核的行为**，写清 wait 何时会过早通过、错误沿哪条数据路径传播、最终症状是什么。

一句定位：Step 1 回答「一个 tile 怎么算对」，Step 2 回答「归约维怎么叠起来」。它同时是 u8-l2 相位复用理论在真实内核里的**最小实例**（单屏障、逐迭代翻转），是 Step 5 多级流水线 `PipelineState` 的原型。

## 2. 前置知识

本讲默认你已完成 u11-l2（Step 1）与单元八。用到的旧知识快速回顾：

- **Step 1 四部件**（u11-l2）：分配（SMEMPool + `tcgen05.alloc`）→ 加载（`Tx.cta.copy` 全 CTA 协作）→ 计算发起（warp 0 内 `elect_sync` 选出一个线程发 `Tx.gemm_async` + `tcgen05.commit`，全体在 mbarrier 上 `try_wait`）→ 回写（`tcgen05.ld` 读 TMEM 入寄存器、转 fp16 写 GMEM）。Step 2 的循环体就是这个「加载 + 计算发起」部件的重复。
- **mbarrier 状态机**（u8-l1）：arrive 与 wait 分离；本讲用的到达路径是 `tcgen05.commit`——MMA 完成后由**硬件**补一次到达，屏障每相的期望到达数因此初始化为 1。
- **相位复用**（u8-l2）：同一道屏障每完成一相（pending 归零）即原子翻入下一相，parity 在 0/1 间交替；`try_wait(P)` 阻塞当且仅当屏障当前 parity 等于 \(P\)。漏翻相位的两种故障方向：消费者侧提前放行、静默读旧数据；等待方向错误时也可能循环等待。
- **tcgen05.mma 的 accum 语义**（u7-l1）：`enable-input-d`（TIRx 里的 `accum`）决定指令是**写入**累加器还是**读出旧值再累加**；K 每步（16 元素）更新同一 TMEM 区域，首步写、其后加。
- **TMEM 分配纪律**（u7-l3）：`tcgen05.alloc` 只有 32/64/128/256/512 五档列数、同 CTA 多次分配须单调不增——所以内核起步就申请 512 列再按列切片。
- **GEMM 约定**（u11-l1）：\(D = AB^{\top}\)，A 为 \(M\times K\)、B 为 \(N\times K\)；fp16 输入输出、fp32 累加。

术语提示：**chunk（块）** 指一次循环处理的 64 宽 K 切片；**部分和（partial sum）** 指一个 chunk 的乘加结果；**相位奇偶（phase parity）** 指屏障当前处于第几相模 2；**提前通过（premature pass）** 指 wait 在等待条件尚未成立时就返回。

## 3. 本讲源码地图

| 文件 | 本讲涉及范围 | 作用 |
| --- | --- | --- |
| `chapter_gemm_basics/index.md` | L339–L480 | **主源码**。Step 2 小节（`chap_k_loop`）全部内容：动机、accum 语义、相位表、`hgemm_v2` 完整内核 |
| `chapter_gemm_basics/index.md` | L634–L638 | 章末练习；练习 2（删除相位翻转）是本讲必答题 |
| `chapter_gemm_basics/index.md` | L276–L321 | 驱动与验证脚手架；跑 Step 2 只需换 `hgemm_v2` 与问题规模，另有「每个会话只编译一个 step」的告诫 |
| `chapter_gemm_basics/index.md` | L108、L126、L130 | Step 1 的三处交叉引用：`cta_sync` 的双重作用、`gemm_async` 按 16 展开为 4 条指令、`accum=False` 的语义 |
| `chapter_async_barriers/index.md` | L45、L49、L53–L55 | 相位理论的原文出处：commit 到达路径、`try_wait` 的阻塞语义、相位如何区分同一屏障的先后使用 |

## 4. 核心概念与源码讲解

本讲的三个最小模块：**K 分块累加**、**TMEM 累加器复用**、**barrier 相位翻转**。

### 4.1 K 分块累加：把归约维装进循环

#### 4.1.1 概念说明

GEMM 的 K 维是**归约维**：\(D[m,n]\) 的值来自对整个 K 求和。Step 1 把 `BLK_K` 定为 64，等于宣布「一次硬件可见的乘加只覆盖 64 个 k」——章节在「Step 1 的局限」里把这条列为第一条：单 K tile，无法对更大的 K 分块累加（[chapter_gemm_basics/index.md:L328-L335](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L328-L335)）。

真实矩阵的 K 常常是几百上千（如 \(4096\)）。分块的依据是加法结合律：把一整条求和拆成若干段，先算每段的部分和，再把段结果连加。设 \(K_{\text{TILES}} = K / 64\)：

\[
D[m,n] \;=\; \sum_{k=0}^{K-1} A[m,k]\,B[n,k]
\;=\; \sum_{i=0}^{K_{\text{TILES}}-1} \;\underbrace{\sum_{j=0}^{63} A[m,\,64i+j]\,B[n,\,64i+j]}_{\text{第 } i \text{ 个 chunk 的部分和 } P_i[m,n]}
\]

每个 \(P_i\) 恰好是一次 \(128\times128\times64\) 的 tcgen05 乘加能产出的形状。于是内核的结构变成：**循环「搬一个 chunk → 乘加一个 chunk」，循环结束后一次性回写**。章节概述里的原话是：对每个 chunk 重复 `load -> MMA -> wait`，把每次 MMA 累加进同一 TMEM 位置。

要注意「K 循环」与「MMA 内部的 K 展开是两回事」：一条 `Tx.gemm_async` 覆盖 64 个 K 元素，内部被编译器按硬件粒度 16 展开为 4 条 `tcgen05.mma` 指令（[chapter_gemm_basics/index.md:L126](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L126)，u7-l1 已讲）；K 循环则是在 **tile 操作层面** 再叠 \(K_{\text{TILES}}\) 轮。两层粒度不要混淆。

#### 4.1.2 核心流程

`hgemm_v2` 的循环骨架（伪代码）：

```text
初始化（循环外，一次性）：
    SMEM pool 分配 tmem_addr / mma_bar / Asmem / Bsmem
    warp0.lane0: mbarrier.init(mma_bar, count=1)     # 每相只等 1 次到达
    warp0: tcgen05.alloc(512 列) → tmem 绑定布局
    phase_mma = 0

K 循环，i = 0 .. K_TILES-1：
    ① load    Tx.cta.copy(Asmem, A[:, 64i:64i+64])    # 128 线程协作，覆写同一对 SMEM
              Tx.cta.copy(Bsmem, B[:, 64i:64i+64])
    ② sync    cta_sync()                              # 拷贝线程全部到齐 + 写入对 MMA 可见
    ③ issue   单线程: Tx.gemm_async(tmem, Asmem, Bsmem, accum=(i != 0))
                     tcgen05.commit(mma_bar)          # 把 MMA_i 完成挂到屏障
    ④ wait    全体: try_wait(mma_bar, phase_mma)      # 等 MMA_i 完成（屏障翻入下一相）
    ⑤ flip    phase_mma ^= 1

回写（循环外，同 Step 1）：tcgen05.ld 读 TMEM → fp32→fp16 → 写 GMEM → 释放 TMEM
```

这个循环是**彻底串行**的：下一个 chunk 的任何一步都不会在当前 chunk 的 wait 返回之前开始。于是 wait 的返回同时守护三件事——

1. **TMEM 可读**：第 \(i\) 轮部分和已写入累加器（对循环出口的回写有意义）；
2. **SMEM 可覆写**：MMA\(_i\) 已不再读 `Asmem`/`Bsmem`，下一轮 ① 的拷贝才能安全覆写它们；
3. **下一轮 MMA 可发**：发射线程也卡在同一 wait 上，不会提前发出 MMA\(_{i+1}\)。

4.3 会看到：漏翻相位破坏的正是这三重守护。

#### 4.1.3 源码精读

- [chapter_gemm_basics/index.md:L339-L353](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L339-L353) —「## Step 2: K-Loop Accumulation」开头：先解除 K 的限制，Step 2 仍只算一个输出 tile，但 K 可以跨多个 64 宽 chunk；随后的 execution structure 框逐条声明——scope 不变（仍单 warpgroup）、layout/复用不变（同一对 SMEM tile 与同一 TMEM 累加器槽跨循环复用，**不新分配任何存储**）、同步（复用的 MMA 屏障每个 chunk 都必须推进到正确相位）、dispatch 不变。这三要素快照就是「本步只加一个机制」的书面证据。
- [chapter_gemm_basics/index.md:L342-L344](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L342-L344) — 逐 chunk 的执行模型：对每个 chunk 重复 `load -> MMA -> wait`，每次 MMA 累加进同一 TMEM 位置。这里同时把异步语义说透：`Tx.gemm_async` 返回时 Tensor Core 可能仍在更新 TMEM；`tcgen05.commit` 把该 MMA 的完成与 `mma_bar` 关联；只有累加器更新完毕硬件才在屏障上报告到达；`try_wait` 的返回即确认**当前 chunk** 已写入 TMEM。
- [chapter_gemm_basics/index.md:L356](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L356) —「### Accumulating Along K」：当 \(K>64\) 时把 K 按 `BLK_K=64` 分块，每轮加载 A、B 的一个 K 切片再发 `Tx.gemm_async`。
- [chapter_gemm_basics/index.md:L390-L400](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L390-L400) — 构建器头部：包装为 `hgemm_v2(M, N, K)`，grid 仍是 \([1,1]\)（还在算单个输出 tile，长大的只有 K）；`BLK_M, BLK_N, BLK_K = 128, 128, 64` 之后新增一行本步标志性的代码：

  ```python
  K_TILES = K // BLK_K
  ```

  整除运算意味着内核**隐含假设 K 是 64 的倍数**（见 4.1.5 练习 1）。
- [chapter_gemm_basics/index.md:L442-L448](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L442-L448) — K 循环的加载与同步段：

  ```python
  for i in T.serial(K_TILES):   # serial device loop
      Tx.cta.copy(Asmem[:, :], A[:, i*BLK_K:(i+1)*BLK_K])
      Tx.cta.copy(Bsmem[:, :], B[:, i*BLK_K:(i+1)*BLK_K])
      T.cuda.cta_sync()
  ```

  注意两点：切片区间随 `i` 平移，每轮把 GMEM 中**当前 chunk 的 64 列**搬进**同一对** SMEM 缓冲（覆写复用）；`cta_sync` 的作用与 Step 1 完全一致——128 个拷贝线程全部到齐，且它们的 SMEM 写入对后续 MMA 可见（Step 1 的原文解释在 [chapter_gemm_basics/index.md:L108](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L108)，章末练习 1 / u11-l2 已答）。`T.serial` 旁的注释说明这是串行设备循环，保持全 K 的 A/B 参数形状正确。
- [chapter_gemm_basics/index.md:L461-L472](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L461-L472) — 回写段代码自带注释 `=== Writeback (same as Step 1) ===`：与 `hgemm_v1` 逐行相同，被**提出循环外**——只有最终累加和才需要读回，中间部分和留在 TMEM 里继续累加（u11-l2 已精读，本讲不重复）。

#### 4.1.4 代码实践

**实践目标**：把「K 分块」从文字变成一张可核对的账目表，并（若有 GPU）跑通 Step 2。

**操作步骤**：

1. 源码推演（无 GPU 也可完成）：设 \(K=4096\)。计算 `K_TILES`，并写出第 \(i\) 轮迭代两条拷贝指令各自读取的 GMEM 切片（A 为 `A[:, 64i : 64i+64]`，B 同理）。
2. 统计：每轮从 GMEM 读多少字节？（A 切片 \(128\times64\) fp16 = 16 KB，B 同，共 32 KB。）整条 K 循环 GMEM→SMEM 总流量是多少？
3. 数一数 `Asmem` 这一个 SMEM 缓冲在整个内核里被覆写多少次；对照 execution structure 框里「不新分配任何存储」的说法。
4. 有 Blackwell GPU 时实跑：按 [chapter_gemm_basics/index.md:L276](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L276) 的说明，把脚手架中的内核换成 `hgemm_v2`、问题规模改为 `M, N, K = 128, 128, 256`，编译运行并观察输出。注意同一句告诫：**每个全新 Python 会话只编译一个 step**，换 step 前先重启（示例复用内部名、编译器持有会话内状态）。

**需要观察的现象**：步骤 4 中 `Max error vs torch reference` 应在 fp16 输出的容差内（脚手架用 `rtol=2e-2, atol=1e-2` 断言，见 [chapter_gemm_basics/index.md:L298-L304](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L298-L304)；容差用相对形式的原因是输出量级随 K 增长），随后打印 `PASS`。

**预期结果**：`K_TILES = 4096 / 64 = 64`；每轮 32 KB，总流量 \(64 \times 32\,\text{KB} = 2\,\text{MB}\)；`Asmem` 被覆写 64 次。GPU 实跑部分**待本地验证**（依赖 sm_100a 环境）。

#### 4.1.5 小练习与答案

**练习 1**：若令 `K = 96` 直接调用 `hgemm_v2`，会发生什么？

**答案**：`K_TILES = 96 // 64 = 1`（整除截断），循环只处理前 64 个 k 列，后 32 列完全不参与计算；内核本身不会报错，但与 torch 参考对拍时误差巨大、断言失败。从这行整除可以推断内核隐含约定 **K 必须是 `BLK_K=64` 的倍数**——驱动脚本里的问题规模要自己保证这一点（书中脚手架取的 K 都是 64 的倍数）。

**练习 2**：把回写段搬进 K 循环内部（每轮都读回 TMEM 写 GMEM）在数学上可行吗？为什么不这么做？

**答案**：数学上可行——每轮写出的中间部分和本身没有错。但不必要且有代价：最终答案只需要**总和**，中间读回会多做 \(K_{\text{TILES}}\) 次 `tcgen05.ld` + fp32→fp16 转换 + GMEM 写回（GMEM 流量放大 \(K_{\text{TILES}}\) 倍），而把这些字节的搬运留给累加结束后的**一次**回写，正是「让数据停留在离计算最近的层级」的原则（u2-l2）。正确设计是：累加留在 TMEM，回写只在循环外做一次。

**练习 3**：K 循环里的 `cta_sync` 与循环外的 mbarrier wait 各自守护哪一段交接？删掉 `cta_sync` 会先坏哪一步？

**答案**：`cta_sync` 守护「128 线程的 SMEM 写 → 单线程发起的 MMA」这段**线程间**交接（汇合 + 可见性）；`try_wait` 守护「硬件 MMA → 全体线程继续」这段**异步引擎与线程间**交接。`cta_sync` 观察不到 Tensor Core 的进度，wait 也观察不到各线程拷贝是否完成——两者不可互相替代（u8-l1 的核心前提）。删掉 `cta_sync`，MMA 可能在部分线程还没把 chunk 数据写进 SMEM 时就读走旧/半新数据，第一轮累加就出错。

### 4.2 TMEM 累加器复用：accum 标志与长寿命状态

#### 4.2.1 概念说明

加入 K 循环后，内核里出现了两种寿命截然不同的状态：

| 状态 | 寿命 | 每轮迭代发生什么 |
| --- | --- | --- |
| `Asmem` / `Bsmem`（SMEM 操作数） | **短寿命**：只活一轮 | 被下一轮的 `Tx.cta.copy` **覆写** |
| TMEM 累加器（`tmem[:, :BLK_N]`） | **长寿命**：活整个循环 | 被当前轮的 MMA **读-改-写**（首轮只写） |

复用 TMEM 累加器的开关是 `Tx.gemm_async` 的 `accum` 参数。它的语义在 Step 1 就埋好了伏笔（[chapter_gemm_basics/index.md:L130](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L130)）：`accum=False` **开启新累加器**、不从 TMEM 读旧值；`accum=True` **读出已有部分和再加**。对应到分块数学：

\[
\text{TMEM} \;\leftarrow\; P_0 \;=\; A_0 B_0^{\top} \qquad (i=0,\ \text{accum}=0)
\]

\[
\text{TMEM} \;\leftarrow\; \text{TMEM} + P_i \;=\; \text{TMEM} + A_i B_i^{\top} \qquad (i>0,\ \text{accum}=1)
\]

这正是 u7-l1 讲过的 tcgen05 原则（K 每步 16 元素、首步写其后加）在 **chunk 粒度** 的应用：每条 `gemm_async` 内部 4 条指令全部 `accum=1` 或全部跟随本次调用的标志，而 chunk 之间靠 `accum=(i != 0)` 区分「建立初值」与「继续累加」。fp32 累加则是数值层面的保障——沿 K 累加用 fp32 减少累计舍入误差（[chapter_gemm_basics/index.md:L31](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L31)），这也解释了为什么在 \(K=4096\) 这类大 K 规模下对拍要用相对容差。

还要注意一个容易忽略的事实：**TMEM 不清零**。`tcgen05.alloc` 只交出一段列区间，不保证内容为零（u7-l3）。因此第一个 chunk **必须**用 `accum=False` 把垃圾初值覆盖掉——这既是数学上的「建立初值」，也是工程上的「初始化」。4.2.4 的变异实验会验证这一点。

#### 4.2.2 核心流程

TMEM 累加器的完整生命周期（对照源码行号）：

```text
循环外一次：  tcgen05.alloc(512 列)            ← L428
              decl_buffer 绑定 TLane/TCol 布局  ← L434-L436
循环 i=0：    MMA 写入 P_0        （accum=0，覆盖垃圾初值）
循环 i=1..T-1：MMA 读旧值再加 P_i （accum=1，读-改-写）
循环外一次：  tcgen05.ld 读回最终和 ← L467（wait::ld 后转 fp16 写 GMEM）
              relinquish + dealloc  ← L475-L477
```

SMEM 侧对照：`Asmem`/`Bsmem` 在 L421-L422 分配一次后，每轮被 ① 覆写、被 ③ 读取，靠「wait 返回 → 才允许覆写」的顺序保证安全（4.1.2 的三重守护之二）。

#### 4.2.3 源码精读

- [chapter_gemm_basics/index.md:L356-L360](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L356-L360) — accum 语义的正式定义：`accum` 参数控制 MMA 是否从 TMEM 读取既有累加器；**第一个 chunk 用 `accum=False` 写入初始部分和，其后每个 chunk 用 `accum=True` 把乘积加进运行中的结果**；每轮中选出的线程在 `Tx.gemm_async` 之后跟随 `tcgen05.commit(mma_bar)`。
- [chapter_gemm_basics/index.md:L450-L455](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L450-L455) — 循环里的 MMA 发起段，与 Step 1 唯一的文字差异就是 accum 实参：

  ```python
  if warp_id == 0:
      if T.ptx.elect_sync():
          Tx.gemm_async(tmem[:, :BLK_N], Asmem[:, :], Bsmem[:, :],
                        accum=(i != 0), dispatch="tcgen05", cta_group=1)
          T.ptx.tcgen05.commit(mma_bar.ptr_to([0]), cta_group=1)
  ```

  双层守卫（`warp_id == 0` + `elect_sync`）保证恰好一个线程发射（u11-l2 已析）；`i != 0` 这个 Python 布尔表达式在编译期按迭代展开成逐轮常量。累加器 tile 始终是 `tmem[:, :BLK_N]`——同一块物理列区域。
- [chapter_gemm_basics/index.md:L425-L436](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L425-L436) — 「循环外一次」的两件事：`mbarrier.init(mma_bar, 1)`（每相期望到达数为 1——本相位唯一的到达来自 `tcgen05.commit` 挂接的硬件完成信号，对照 [chapter_async_barriers/index.md:L45](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_async_barriers/index.md#L45)：单发 `tcgen05.mma` 不会更新屏障，必须由 commit 关联一次到达，且硬件只在操作完成后才报告）；`tcgen05.alloc` 申请 512 列（本内核实际只用到前 `BLK_N=128` 列，取 512 是 u7-l3 的分配纪律：列数只有五档且同 CTA 分配须单调不增，起步即定最大需求）。`decl_buffer` 把 TMEM 绑定为 \((128,512)\) 的 fp32 视图，布局 `S[(128,512):(1@TLane,1@TCol)]`。
- [chapter_gemm_basics/index.md:L474-L477](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L474-L477) — 循环结束后先 `cta_sync` 再由 warp 0 执行 `relinquish_alloc_permit` + `dealloc` 释放 TMEM——长寿命状态的终点。这里的 `cta_sync` 保证全部线程（包括刚做完回写的 warpgroup）都不再访问 TMEM 之后才释放。

#### 4.2.4 代码实践

**实践目标**：用一张「累加器状态表」验证 accum 分支的正确性，再用两个变异体加深对「初值」与「累加」的理解。

**操作步骤**：

1. 手推状态表：设 \(K=256\)（`K_TILES=4`），逐列填写下表（把「TMEM 内容」写成部分和的符号表达式）：

   | i | accum | MMA 语义 | 该轮结束后 TMEM 内容 |
   | --- | --- | --- | --- |
   | 0 | False | 写入 | \(P_0\) |
   | 1 | True | 读-改-写 | \(P_0+P_1\) |
   | 2 | ? | ? | ? |
   | 3 | ? | ? | ? |

2. 变异体 A（推演）：把 `accum=(i != 0)` 改成恒 `True`。第 0 轮会发生什么？结合「TMEM 不清零」推断结果错误来源。
3. 变异体 B（推演）：改成恒 `False`。每一轮对 TMEM 做什么？最终写回 GMEM 的值等于哪个表达式？
4. 有 Blackwell GPU 时实测：在自己的副本文件里分别跑两个变异体（各用全新会话），记录 `max_err` 并与正确版对比。

**需要观察的现象**：变异体 A 的结果整体偏离参考值且偏差方向无规律（混入未定义初值）；变异体 B 的结果恰好等于**只做最后一个 chunk** 的乘加（前面 \(K_{\text{TILES}}-1\) 轮的计算被整轮覆盖丢弃），`max_err` 与矩阵元素量级同阶。

**预期结果**：状态表第 2、3 行依次为 True/读-改-写/\(P_0+P_1+P_2\)、True/读-改-写/\(P_0+P_1+P_2+P_3\)。变异体 A：第 0 轮把 \(P_0\) 加进 alloc 返回的未初始化内容，最终 D = 垃圾 + 全部部分和。变异体 B：最终 D = \(P_{K_{\text{TILES}}-1}\)，如 \(K=256\) 时只含 k∈[192,256) 的贡献。GPU 实测部分**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：本内核申请了 512 列 TMEM 却只用 128 列，浪费吗？为什么不多不少正好申请 128？

**答案**：不浪费——TMEM 是按列独占分配的 CTA 资源，但同一 CTA 内「申请 512、切片使用」与「申请 128」消耗的实际容量相同；而 `tcgen05.alloc` 的列数只有 32/64/128/256/512 五档、且同一 CTA 的多次分配必须单调不增（u7-l3），起步就取最大档可以让后续扩展（如 FA4 在同一 512 列里复用多个区域）不必重构分配逻辑。这是「分配策略」而非「用量」的选择。

**练习 2**：`mbarrier.init` 的期望到达数为什么是 1？这一相里谁到达？

**答案**：每相唯一的一次到达来自 `tcgen05.commit` 挂接的**硬件到达**——MMA 完成后硬件替它 arrive（[chapter_async_barriers/index.md:L45](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_async_barriers/index.md#L45)）。发射线程自己不 arrive，128 个等待线程更不 arrive（wait 只观察、不修改状态，u8-l1）。所以 count=1 恰好匹配「一相一次 MMA 完成信号」。

**练习 3**：如果把累加 dtype 从 fp32 降为 fp16（假设硬件允许），对本内核的风险是什么？

**答案**：K 维是归约维，累加次数等于 K。fp16 的有效位数约 11 bit，随着部分和个数增多，每次加法的舍入误差会不断累积，大 K 下的相对误差可能超出对拍容差；这正是书中「沿 K 用 fp32 累加以减少累计舍入误差」（[chapter_gemm_basics/index.md:L31](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L31)）与「容差随输出量级（随 K 增长）取相对形式」（[chapter_gemm_basics/index.md:L301-L303](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L301-L303)）两句注释背后的同一件事。

### 4.3 barrier 相位翻转：phase_mma 状态机与漏翻故障

#### 4.3.1 概念说明

K 循环让一道 `mma_bar` 在一个内核里被使用 \(K_{\text{TILES}}\) 次。mbarrier 的复用机制（u8-l2 / [chapter_async_barriers/index.md:L53-L55](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_async_barriers/index.md#L53-L55)）：每相完成（本讲中即那次硬件到达使 pending 归零）后，屏障**自动**进入下一相并重装期望到达数，parity 在 0/1 间交替。于是「这道屏障完成过」不再是有意义的信息——它每一相都会完成；消费者必须知道「我现在这一轮该等哪一相」。`phase_mma` 变量就是软件侧对这一问题的回答。

wait 的语义决定了一切：`T.ptx.mbarrier.try_wait` 包装了 PTX `try_wait.parity` 的重试循环，**阻塞直到请求的相位完成**（[chapter_async_barriers/index.md:L49](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_async_barriers/index.md#L49)）；本章原文同样表述为「`try_wait` 在屏障**离开**指定相位后返回」（[chapter_gemm_basics/index.md:L368](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L368)）。换言之：

- 屏障当前停在相位 \(P\)（本轮 MMA 未完成）→ `try_wait(P)` 阻塞，直到本轮完成；
- 屏障早已离开相位 \(P\)（在相位 \(1-P\)）→ `try_wait(P)` **立即返回**，不管现在这轮 MMA 进展如何。

所以第 \(i\) 轮迭代必须等相位 \(i \bmod 2\)：那是本轮 MMA 完成前屏障**停着的**相位。这与 u8-l2 的通用公式（深度 \(S\) 的环：stage \(=k \bmod S\)、phase \(=\lfloor k/S \rfloor \bmod 2\)）在 \(S=1\) 时正好退化为 phase \(= i \bmod 2\)——本讲是那个公式的最小实例，Step 5 的多级流水线则是它的推广。

#### 4.3.2 核心流程

章节给出的前三轮相位表（[chapter_gemm_basics/index.md:L362-L366](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L362-L366)）：

| K 迭代 i | `try_wait` 传入的 `phase_mma` | MMA 完成后屏障所处相位 |
|---|---:|---:|
| 0 | 0 | 1 |
| 1 | 1 | 0 |
| 2 | 0 | 1 |

时序展开（有翻转的正确版，\(K_{\text{TILES}}=4\)）：

```text
i=0: MMA0 在飞, 屏障停相位0 → wait(0) 阻塞 → MMA0 完成, parity 0→1 → 返回 → phase_mma 0→1
i=1: MMA1 在飞, 屏障停相位1 → wait(1) 阻塞 → MMA1 完成, parity 1→0 → 返回 → phase_mma 1→0
i=2: 同 i=0（相位 0）                          i=3: 同 i=1（相位 1）
出口: phase_mma 经过 4 次翻转回到 0（对本内核已无用处——回写不再碰这道屏障）
```

不变式：**进入第 \(i\) 轮 wait 时，`phase_mma` == \(i \bmod 2\) == 本轮 MMA 完成前屏障停着的相位**。翻转的时机固定在每次成功 wait 之后（[chapter_gemm_basics/index.md:L370-L374](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L370-L374)），执行 `phase_mma ^= 1` 为下一轮 MMA 更新等待值。

#### 4.3.3 源码精读

- [chapter_gemm_basics/index.md:L346](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L346) — 本模块的论纲：每个迭代复用同一道 `mma_bar`，每次完成后屏障推进到新相位，因此 `phase_mma` 标识**正在等待的是哪一个特定迭代**；如果相位跟踪错误，一次 wait 可能把**上一个迭代的完成**误当作当前 MMA，从而**静默污染结果**。
- [chapter_gemm_basics/index.md:L360-L368](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L360-L368) — 机制串联：每轮中选出的线程在 `gemm_async` 后跟随 `commit(mma_bar)`；**屏障只有在 MMA 完成并报告到达之后才离开当前相位**；`phase_mma` 记录当前迭代必须等待的相位；`try_wait(bar, phase_mma)` 在屏障离开该指定相位后返回。
- [chapter_gemm_basics/index.md:L438](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L438) — 状态初始化：`phase_mma: T.int32 = 0`——与屏障 init 后从相位 0 起步一致，第一轮等相位 0 恰好是 MMA0 完成前屏障停着的相位。
- [chapter_gemm_basics/index.md:L457-L459](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L457-L459) — 循环内的等待与翻转，注意两行都**在 `if warp_id == 0` 之外**：

  ```python
  # Wait for MMA, then flip phase
  T.ptx.mbarrier.try_wait(mma_bar.ptr_to([0]), phase_mma)
  phase_mma ^= 1
  ```

  全部 128 个线程都要执行 wait——它们既是下一轮 SMEM 覆写者也是（在出口处的）TMEM 读取者，任何一个提前跑走都会破坏 4.1.2 的三重守护。
- [chapter_gemm_basics/index.md:L376](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L376) — 漏翻后果的官方表述（练习 2 答案的锚点）：若不翻转，第二个迭代仍会等相位 0；而屏障在第一个 MMA 后已进入相位 1，该 wait **可能立即返回**，使内核在第二个 MMA 完成之前就去读累加器。
- [chapter_gemm_basics/index.md:L637](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L637) — 章末练习 2 原文：删除 K 循环中的 `phase_mma ^= 1` 会发生什么？内核还会等待每次 MMA 吗，还是更晚的 wait 会过早通过？

#### 4.3.4 代码实践（本讲必答题：章末练习 2）

**实践目标**：推演（有 Blackwell GPU 则复现）删除 `phase_mma ^= 1` 后内核的行为，写清 wait 何时过早通过、错误如何传播、最终症状。

**操作步骤**：

1. **建工作单**：设 \(K=256\)（`K_TILES=4`）。复制下表两份，分别按「有翻转」「无翻转」逐行填写。关键列是「wait 调用时屏障实际 parity」——按 4.3.2 的时序逐轮推。

   | i | 传入 try_wait 的相位 | wait 时屏障 parity | wait 阻塞？ | 返回时 MMA_i 保证完成？ |
   |---|---|---|---|---|
   | 0 | 0 | 0（MMA0 在飞） | 是 | 是 |
   | 1 | … | … | … | … |
   | 2 | … | … | … | … |
   | 3 | … | … | … | … |

2. **推演无翻转版**：`phase_mma` 恒为 0。逐轮问自己：此刻屏障停在哪个相位？`try_wait(0)` 是阻塞还是立即返回？
3. **画出受害者链**：对每个「立即返回」的轮次，标出哪些后续动作失去了保护——下一轮的 `Tx.cta.copy` 覆写 SMEM（操作数可能仍被上一轮 MMA 读）、循环出口的 `tcgen05.ld` 读 TMEM（最后一个 MMA 可能未完成）、以及出口处的 `dealloc`。
4. **（有 GPU 时）实测**：把 `hgemm_v2` 复制到自己的练习文件，删除 `phase_mma ^= 1` 那一行，用 `M, N, K = 128, 128, 256` 编译运行（全新会话；对照正确版各跑一次）。记录：是否挂死、`max_err` 数值、断言结果。可再加大 K（如 1024）观察症状是否稳定。

**需要观察的现象 / 推演结论**：

- 无翻转时的推演表（第 1 行之后）：`phase_mma` 恒 0；第 1 轮 wait 时屏障通常仍在相位 1（MMA1 在飞），`try_wait(0)` 发现屏障早已「离开相位 0」，**立即返回**——不等 MMA1。此后每一轮同理，**只有第 0 轮的 wait 是真等**。
- 受害者链按时间顺序：① 第 2 轮的 SMEM 拷贝可能覆写仍被 MMA1 读取的 `Asmem`/`Bsmem` → MMA1 用错操作数，部分和错；② 循环出口的 `tcgen05.ld` 可能在最后一个 MMA 落笔前读 TMEM → 累加器不完整；③ 更早失序时 `dealloc` 与在途 MMA 竞争。
- **时序变体（进阶）**：若某轮 MMA 恰好在 wait 调用之前就完成了（parity 已翻回 0），`try_wait(0)` 反而会**阻塞等屏障再次离开相位 0**——那需要下一轮 MMA 的到达，而下一轮 MMA 又要等这个 wait 返回后才发射，循环卡死。也就是说漏翻相位的症状**取决于时序**：多数情况下是「静默错误结果」，另一种时序下是「挂死」。两种表象同根：**wait 与完成事件的配对被打乱**。这与 u8-l2 的总结一致——消费者侧漏翻表现为提前放行/静默读旧数据，等待方向错乱时表现为循环等待。
- GPU 实测中预期看到的是第一种：`max_err` 与矩阵元素量级同阶（部分 chunk 缺失或算错）、`assert_close` 失败、通常**不**挂死。

**预期结果**：推演表填完后应能得出「除第 0 轮外全部 wait 都不再保证对应 MMA 完成」的结论；GPU 实测数值**待本地验证**（依赖 sm_100a 环境与时序）。

#### 4.3.5 小练习与答案

**练习 1**：\(K_{\text{TILES}}\) 为奇数（如 \(K=192\)，3 块）时，循环出口处 `phase_mma` 的值是多少？它会影响回写吗？

**答案**：3 次翻转后 `phase_mma` 为 1。不影响——回写段（`tcgen05.ld` + `wait::ld`）不再使用 `mma_bar`，它依赖的是 `tcgen05` 自己的 `wait::ld` 异步纪律（u7-l4）；`phase_mma` 的生命周期只覆盖 K 循环。这也是为什么翻转放在循环内最后一步是安全的。

**练习 2**：把 `try_wait` 挪进 `if warp_id == 0` 块、只让 warp 0 等待，行不行？

**答案**：不行。wait 之外的其他 3 个 warp 会立刻跑进下一轮迭代的 `Tx.cta.copy`，覆写仍被当前 MMA 读取的 SMEM（三重守护之二被单独拆掉）；同样它们也会提前到达出口读 TMEM。三重守护的受益者是**全体 128 个线程**，所以 wait 必须人人执行——这正是源码把 L458 放在守卫之外的原因。反过来，`if warp_id == 0` 内的 `elect_sync` 只约束**发起**这件事本身（scope），与等待无关。

**练习 3**：把 `tcgen05.commit` 挪到 `try_wait` 之后（先等后 commit）会发生什么？

**答案**：第 0 轮：屏障尚未收到任何到达关联，停在相位 0，`try_wait(0)` 会永远阻塞——挂死。第 1 轮及以后（若能走到）：wait 等到的是**上一轮**的完成信号，与本轮 MMA 无关。commit 的职责是「把本轮异步 MMA 的完成挂到屏障上」，必须先登记、后等待；这和 u8-l1 的结论一致——`expect_tx`/`commit` 类登记操作必须先于对应 wait。

## 5. 综合实践

**任务：为 `hgemm_v2` 制作一张「K 循环体检表」，并完成双变异预测（+可选实测）。**

1. **体检表**：设 \(K=512\)（`K_TILES=8`）。制作一张 8 行、覆盖本讲全部三个模块的表格，列为：
   `i`｜`A/B 的 GMEM 切片`｜`本轮 GMEM 字节`｜`accum`｜`MMA 发起者`｜`wait 执行者`｜`try_wait 传入相位`｜`MMA 完成后屏障 parity`｜`该轮结束后 TMEM 内容`。
   逐行填写并核对三条不变式：切片随 i 平移且宽度恒 64；accum 仅 i=0 为 False；传入相位 == i mod 2。
2. **变异预测**：对下面两个变异体，先用体检表推演出「哪一行开始出错、错在哪一列」，再预测 `max_err` 的量级与是否挂死：
   - **变异 A**：删除 `phase_mma ^= 1`（4.3.4 已详析，检验你能否独立复述受害者链）；
   - **变异 B**：`accum=(i != 0)` 改为 `accum=False` 恒定（4.2.4 变异体 B，检验它与变异 A 的症状差异：B 是**确定性**的「只剩最后一个 chunk」，A 是**时序依赖**的静默错误）。
3. **（可选，需 Blackwell GPU）实测**：正确版、变异 A、变异 B 各用一个全新 Python 会话编译运行（`M, N, K = 128, 128, 512`；每次只编译一个 step 的告诫同样适用于你的变异副本），记录三组的 `max_err`、`PASS/FAIL`、是否挂死，并与预测对照。
4. **收尾自查**：用一句话向自己解释——为什么一行只有 6 个字符的 `phase_mma ^= 1` 缺席时，内核「看起来还能跑完」却给出完全错误的结果？（提示：wait 立即返回 ≠ 等待成立；静默错误比崩溃更危险，这正是 u15-l7 调试方法论按症状分类的原因。）

无 GPU 时交付物为体检表 + 两份变异推演；有 GPU 时加三组实测记录。GPU 部分**待本地验证**。

## 6. 本讲小结

- **Step 2 = Step 1 + 三处增量**：`T.serial` K 循环、`accum=(i != 0)` 分支、`phase_mma ^= 1` 翻转；scope / layout / dispatch 三要素全部不变，SMEM 与 TMEM 均不新分配。
- **K 分块累加**：\(D[m,n]\) 拆成 \(K_{\text{TILES}}\) 个部分和的连加（结合律保证可行，fp32 累加控制舍入）；循环体 = Step 1 的「加载 + 计算发起」部件逐 chunk 重复，回写提出循环外只做一次。
- **两种寿命的状态**：SMEM 操作数短寿命（逐轮覆写），TMEM 累加器长寿命（首块 `accum=False` 覆盖垃圾初值、其余 `accum=True` 读-改-写）；wait 的返回同时守护「TMEM 可读、SMEM 可覆写、下一轮 MMA 可发」三件事。
- **相位纪律**：一道 mbarrier 服务 \(K_{\text{TILES}}\) 次 MMA，每次完成硬件自动翻相；第 \(i\) 轮必须 `try_wait(i mod 2)`，成功等待后立即翻转——不变式是「传入相位 == 本轮 MMA 完成前屏障停着的相位」。
- **漏翻相位的故障是静默的**：后续 wait 把上一轮完成误当本轮，立即返回，内核照常跑完但结果错（时序变体下也可能挂死）；这是 u8-l2「消费者侧提前放行」理论在真实内核中的实证。
- **本讲是 Step 5 的原型**：单屏障逐迭代翻转（\(S=1\)）是 u8-l2 通用公式 stage \(=k \bmod S\)、phase \(=\lfloor k/S\rfloor \bmod 2\) 的最小实例，多级流水线的 `PipelineState` 只是把「stage + phase」捆成一个对象来自动维护这套纪律。

## 7. 下一步学习建议

- **下一讲 u11-l4（Step 3：空间分块）**：解除 \(M=N=128\) 的限制，把输出按 M/N 切成 tile 网格、一 CTA 一 tile；你会发现本讲的 K 循环被**原样内嵌**、一行不改——这正是「每个旧版本保留为参照系」的教学设计。顺带完成章末练习 3（grid 形状与 CTA 数的计算），并留意章节在 Step 3 埋的伏笔：不同 CTA 会重复读取相同的 A/B tile（[chapter_gemm_basics/index.md:L528](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L528)），那是后续 L2 局部性与 cluster 协作的起点。
- **往后两章**：`chapter_gemm_async` 的 Step 4 把 `Tx.cta.copy` 换成 TMA——届时 mbarrier 开始用 `expect_tx` 追踪**字节数**（u8-l1 的另一条到达路径登场），本讲的 `phase_mma` 纪律原样保留；Step 5 引入双缓冲后，单屏障逐迭代翻转推广为**每 stage 一对 full/empty 屏障**的相位管理，建议届时重读 u8-l2 的 stage 复用环并与本讲对照。
- **源码再读建议**：回到 `chapter_gemm_basics/index.md` 把 Step 1→2→3 三个内核并排 diff 着读（L180-L273、L393-L479、L545-L631），亲眼看「每步只改几行」的版本演进方式——这种增量改写风格本身就是写内核实验的范本。
