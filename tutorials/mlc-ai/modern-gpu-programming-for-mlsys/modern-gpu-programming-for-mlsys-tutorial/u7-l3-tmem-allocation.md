# TMEM 分配生命周期与 lane 访问窗口

## 1. 本讲目标

学完本讲，你应该能够：

1. 写出 TMEM 的完整分配生命周期模板：`pool.alloc` 预留地址槽 → `tcgen05.alloc` 分配列 → fence + CTA 同步发布结果 → `T.decl_buffer(allocated_addr=...)` 绑定缓冲 → 使用 → 确保异步操作完成 → `tcgen05.relinquish_alloc_permit` + `tcgen05.dealloc` 释放，并能说出每一步为什么必须在那个位置。
2. 记住分配大小的两条硬规则：合法 `n_cols` 只有 32、64、128、256、512 五档；同一 CTA 按程序序多次分配时，后面的分配**不能**比前面的申请更多列。
3. 给定任意 warp，确定它能通过 `tcgen05.ld`/`tcgen05.st` 访问的 32 个 TMEM Lane 位置：warp 在其 warpgroup 内的编号 \( w \) 对应 TLane 区间 \( [32w,\,32w+31] \)，列方向则不受限制。
4. 说明 `cta_group::2` 时两个 CTA 各自执行同样的 alloc/dealloc、各分配同样多的列到**各自**的 TMEM，以及"先到的一方可能等待对端"和"同一内核内所有带 `cta_group` 限定符的指令必须用同一个值"这两条纪律。

## 2. 前置知识

本讲是「Blackwell Tensor Core 与 TMEM」单元第三讲。u7-l1 讲了 MMA 怎么把累加器写进 TMEM，u7-l2 讲了累加器落在 TMEM 的哪些坐标，本讲补上被前两讲略过的问题：**这些列是从哪里来的、归谁、什么时候还回去**。

需要回顾的前置概念：

| 概念 | 一句话回顾 |
| --- | --- |
| TMEM 二维地址空间（u2-l2、u4-l2） | 每 CTA 有 128 个 Lane 位置 × 最多 512 个 Column 位置，每格 32 bit；坐标用命名轴 \( \text{TLane}, \text{TCol} \) 描述，Lane 是数据侧地址坐标而非线程 laneid |
| `tcgen05.mma` 的累加器（u7-l1） | A/B 从 SMEM 描述符读取，累加器写入 TMEM 的 `d-tmem` 地址；首步 `accum=0` 写入、其后 `accum=1` 累加；完成信号经 `tcgen05.commit` 挂到 mbarrier |
| 累加器 TMEM 映射（u7-l2） | `cta_group::1` M=128 时行 m 直映 TLane；`cta_group::2` 时累加器分布在 CTA 对两块 TMEM 上（Layout A：M 连续对半切）；每 CTA 内部有四个 32-lane 分区（硬件数据通路的 `warp-rank % 4`） |
| SMEMPool（u9-l1 将展开，此处只需直觉） | TIRx 用 `T.SMEMPool()` 在编译期管理 SMEM 分配；TMEM **不走**这个 pool，而是靠 `tcgen05.alloc` 指令在运行期动态分配 |
| fence 与 CTA 同步（u6-l3） | 异步写 SMEM 后需要 `fence.proxy_async` 建立可见性，`T.cuda.cta_sync()` 让全 CTA 线程齐步；这些原语在本讲用于"发布分配结果" |

**术语提示**：

- **`n_cols`**：`tcgen05.alloc` 申请的 TMEM 列数。分配一列即保留该列全部 128 个 Lane 位置。
- **`tmem_addr`**：一个 4 字节（`uint32`）的 SMEM 槽位，用来**接收**分配结果的基地址。注意它本身在 SMEM 里，不是 TMEM 地址。
- **warp 集体指令（warp-collective）**：warp 内 32 个线程必须一起执行同一条指令、给出相同操作数；与"单线程发起"的 `tcgen05.mma` 相对。
- **阻塞指令（blocking instruction）**：`tcgen05.alloc` 可能要等空闲 TMEM 列出现才能返回。
- **alloc permit（分配许可）**：CTA 保有后续分配资格的状态；`relinquish_alloc_permit` 主动交出。

## 3. 本讲源码地图

本仓库是教材仓库，"源码"是章节正文、真实 TIRx 内核与书图脚本：

| 文件 | 作用 |
| --- | --- |
| [chapter_tmem/index.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tmem/index.md) | 本讲精读对象。第 22-57 行讲分配生命周期与 `decl_buffer` 绑定，第 59-68 行讲尺寸限制，第 70-84 行讲释放与 `cta_group::2`，第 86-97 行讲 warp lane 窗口 |
| [chapter_gemm_basics/index.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md) | GEMM Step 1 完整内核：第 210-232 行是"分配 + 发布 + 绑定"的真实写法，第 256-265 行的写回路径印证 lane 窗口，第 267-271 行是释放段 |
| [chapter_intro_tirx/index.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md) | 第一个内核 hgemm_v1 中的同款分配/释放段（第 110-131、165-168 行），第 68 行明说这些低层调用是 tile 操作的"配套步骤" |
| [chapter_gemm_advanced/index.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md) | GEMM Step 8（`cta_group::2`）：第 477-487 行是协作分配，第 590-593 行是 `cluster_sync` 后的协作释放，第 419 行的正文解释了为何要先 `cluster_sync()` |
| [img/scripts/gen_tmem_grid.py](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_tmem_grid.py) | 生成 `tmem_grid.png`（128 TLane × 512 TCol 网格图）的 matplotlib 脚本，是本讲代码实践的主要对象 |

## 4. 核心概念与源码讲解

### 4.1 模块一：alloc / relinquish / dealloc——TMEM 的动态生命周期

#### 4.1.1 概念说明

SMEM 在 TIRx 里由 `T.SMEMPool()` 在**编译期**规划；TMEM 不同——它是**运行期动态分配**的资源。分配沿**列（Column）维度**进行，有两个直接推论：

1. **分配一列 = 保留全部 128 个 Lane**。不存在"只分配某几行"的操作；TMEM 的最小完整纵向单位就是一整列。
2. **合法列数只有五档**：\( n_{cols} \in \{32, 64, 128, 256, 512\} \)，不能申请 100 列。

分配的结果不是一个指针返回值，而是**写进一个 SMEM 槽位**：内核先在 SMEM pool 里留一个 `uint32` 小槽 `tmem_addr`，`tcgen05.alloc` 成功后把分配区域的基地址写进去。这条指令还两点特殊：

- 它是 **warp 集体指令**：warp 内 32 个线程都要执行、带相同的 `n_cols`。所以守卫条件是 `warp_id == 0`（选中整个 warp 0），**绝不能**再套一层 `lane_id == 0` 把它变成单线程操作——这与 u7-l1 中"单线程发起 `tcgen05.mma`"的写法方向相反，很容易记混。
- 它是**阻塞指令**：可能要等空闲 TMEM 列出现。

分配到地址之后，TIRx 用 `T.decl_buffer(scope="tmem", allocated_addr=...)` 在这片区域上声明一个带布局的逻辑缓冲，后续代码就可以写 `tmem[m, n]`，由布局换算成硬件坐标。

用完之后是**两步**释放，缺一不可：

- `tcgen05.relinquish_alloc_permit`：声明"本 CTA 不再分配 TMEM"，交出分配许可。此后再调 `tcgen05.alloc` 是非法的。
- `tcgen05.dealloc`：真正归还列。**每一次分配都必须显式释放**，内核退出前不能留账。

顺序是先 relinquish 再 dealloc（书中所有内核都如此），并且清理开始前必须保证所有触碰 TMEM 的异步 MMA、load、store 已经完成。

#### 4.1.2 核心流程

一次完整的 TMEM 生命周期（`cta_group::1`）：

```text
① pool.alloc((1,), "uint32")        在 SMEM 预留 4 字节槽位 tmem_addr（编译期）
        │
② if warp_id == 0:                  warp 0 集体执行（32 线程，同一 n_cols）
        │   tcgen05.alloc(&tmem_addr, n_cols, cta_group=1)
        │       · 阻塞，直到有空闲列
        │       · 成功后把 TMEM 基地址写进 tmem_addr
        │
③ fence.proxy_async + fence.mbarrier_init
   + T.cuda.cta_sync()              发布：全 CTA 都能看到 tmem_addr 的值
        │
④ T.decl_buffer(..., scope="tmem",
       allocated_addr=tmem_addr[0], 在分配区域上立起逻辑缓冲
       layout=TileLayout(...))      （布局负责 (m,n) → TLane/TCol）
        │
⑤ 使用：tcgen05.mma 写累加器；tcgen05.ld 读回（各受各自纪律约束）
        │
⑥ 确保异步操作全部完成（cta_sync / mbarrier 等到 / cluster_sync）
        │
⑦ if warp_id == 0:
       tcgen05.relinquish_alloc_permit(cta_group=1)   先交出分配许可
       tcgen05.dealloc(tmem_addr[0], n_cols, cta_group=1)  再归还列
```

`cta_group::2` 时的差异只在协作方：

```text
cta_group::1：本 CTA 一个 warp 执行 alloc/dealloc
cta_group::2：CTA 对两侧各出一个 warp，执行同一条 alloc（或 dealloc）
              · 两侧的 n_cols 等参数一致
              · 先到达的一侧可能等待对端
              · 对端 CTA 必须已经启动并最终参与
约束：同一内核内，所有带 cta_group 限定符的 tcgen05 指令必须用同一个值
      （不能 alloc 用 ::2、mma/commit 用 ::1）
```

#### 4.1.3 源码精读

**（1）章节正文给出的标准 pattern。** 先看教材自己总结的最小写法：[chapter_tmem/index.md:L28-L41](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tmem/index.md#L28-L41)。这段代码在 SMEM pool 里留出 `tmem_addr` 槽、commit 之后由 warp 0 集体执行 `tcgen05.alloc`；紧随其后的正文说明 `tmem_addr` 是 SMEM 里的 32 位槽、指令可能阻塞、以及"不要加 `lane_id == 0`"的警告。第 41 行还要求：其他 warp 读 `tmem_addr` 之前，必须用适当的 fence 和 CTA 同步把分配结果发布到全 CTA——这就是流程图中第③步的出处。

**（2）把地址变成缓冲。** [chapter_tmem/index.md:L43-L57](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tmem/index.md#L43-L57) 展示 `T.decl_buffer` 如何用 `allocated_addr=tmem_addr[0]` 把缓冲绑到分配结果上，并用 `TileLayout(S[(128, 256) : (1@TLane, 1@TCol)])` 让逻辑坐标 `(m, n)` 落到 `TLane`/`TCol`。注意这里的 `[0]`：**读出槽里的值**传给声明；而第①步 alloc 时传的是 `T.address_of(tmem_addr)`——**把槽的地址交给指令去写**。一个取址、一个取值，方向相反。

**（3）真实内核 Step 1 中的完整序列。** [chapter_gemm_basics/index.md:L210-L232](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L210-L232)：`tmem_addr` 与 `mma_bar` 这两个"控制小值"放在 SMEM 低地址区（[第 94 行](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L94)解释了 `move_base_to(1024)` 之前的空间就留给它们）；随后 warp 0 里 `lane_id == 0` 只守卫 `mbarrier.init`（那是单线程操作），而 `tcgen05.alloc` 没有这层守卫——同一段代码里两种守卫并存，正是"warp 集体 vs 单线程"最直观的对照。三件套 `fence.proxy_async` / `fence.mbarrier_init` / `cta_sync` 一次发布 mbarrier 初始化与分配结果两个事实。

释放段在 [chapter_gemm_basics/index.md:L267-L271](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L267-L271)：先 `cta_sync()` 保证写回（含 `tcgen05.wait.ld()` 之后的 GMEM 写）全部完成，再由 warp 0 依次 `relinquish_alloc_permit` 与 `dealloc`。第一个内核 hgemm_v1 用的是一模一样的序列（[chapter_intro_tirx/index.md:L110-L131](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L110-L131) 与 [L165-L168](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L165-L168)）；该章 [第 68 行](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L68) 明确把这些低层调用定位为三个 tile 操作的"配套步骤"——本讲就是在补齐这套配套步骤的语义。

**（4）`cta_group::2` 的协作分配与释放。** [chapter_gemm_advanced/index.md:L477-L483](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L477-L483)（GEMM Step 8）：守卫变成 `wg_id == 0` 且 `warp_id == 0`，`cta_group=CTA_GROUP`（值为 2）。释放段 [L590-L593](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L590-L593) 前面的同步从 `cta_sync()` 升级为 `T.cuda.cluster_sync()`；[第 419 行](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L419) 的正文直接说明原因：释放前必须确保**两个 CTA** 都用完 TMEM。章节侧的规则原文在 [chapter_tmem/index.md:L80-L84](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tmem/index.md#L80-L84)。

#### 4.1.4 代码实践

**实践：写出调用顺序模板，并在三个真实内核上核对**

1. **实践目标**：把 4.1.2 的流程图固化为一份可复用的"TMEM 生命周期模板 + 核对清单"，并回答 `cta_group::2` 的 `n_cols` 对应问题。
2. **操作步骤**：
   - 先不看 4.1.2，只读源码，自己写出七步模板（①预留槽 ②warp 集体 alloc ③fence+cta_sync ④decl_buffer ⑤使用 ⑥等异步完成 ⑦relinquish+dealloc）；
   - 打开三处真实代码逐一核对：[chapter_gemm_basics Step 1（L210-L232、L267-L271）](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L210-L232)、[chapter_intro_tirx hgemm_v1（L110-L131、L165-L168）](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L110-L131)、[chapter_gemm_advanced Step 8（L477-L487、L590-L593）](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L477-L487)；
   - 为每个内核填一张五行表：守卫条件｜`n_cols`｜发布用同步｜释放前同步｜`cta_group` 值；
   - 回答：`cta_group::2` 时两个 CTA 各自分配的 `n_cols` 如何对应？
3. **需要观察的现象**：三份代码的**步骤顺序完全一致**；差异只出现在三处——守卫（`warp_id==0` vs `wg_id==0 and warp_id==0`）、`cta_group` 值（1 vs `CTA_GROUP`=2）、释放前的同步（`cta_sync` vs `cluster_sync`）。
4. **预期结果**：模板核对零出入。`cta_group::2` 问题的参考答案：CTA 对**两侧各自**的 warp 0 执行**同一条** `tcgen05.alloc`，`n_cols` 等参数完全一致（Step 8 中两侧都是 `n_cols=512`）；由于 TMEM 是**每 CTA 一份**的资源，两侧是在**各自的 TMEM** 里各分得 512 列，协作 MMA 的累加器随后按 Layout A 的 M 对半切分布在两块 TMEM 上（承接 u7-l2）。先到的一侧可能等对端，所以对端 CTA 必须已启动并最终参与； dealloc 同样两侧执行，且之前的 `cluster_sync()` 保证双方都已用完。
5. （可选，需 Blackwell GPU 与 `tvm.tirx` 环境）把 Step 1 存成单独的 `.py` 文件编译运行，在 alloc/dealloc 前后各加一行 `T.evaluate(...)` 或用 `kernel.show()` 检视 IR，确认调用顺序进入生成的 CUDA。注意书中提醒：**每个新 Python 会话只编译一个 step**（[chapter_gemm_basics/index.md:L276](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L276)）。无 GPU 时本实践为源码阅读型，标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `tcgen05.alloc` 的守卫是 `if warp_id == 0:` 而 `mbarrier.init` 的守卫是 `if warp_id == 0: if lane_id == 0:`？

**答案**：`tcgen05.alloc` 是 warp 集体指令，warp 内 32 个线程必须一起执行并带相同 `n_cols`，所以只选 warp、不再选 lane；`mbarrier.init` 是普通单线程操作，多线程重复初始化反而错误，所以要再套 `lane_id == 0`。章节原文明确警告"不要给 alloc 加 `lane_id == 0` 条件把它变成单线程操作"（[chapter_tmem/index.md:L41](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tmem/index.md#L41)）。

**练习 2**：把 `relinquish_alloc_permit` 和 `dealloc` 的顺序反过来（先 dealloc 再 relinquish），或者干脆省略 `dealloc`，分别会怎样？

**答案**：书中的规则是：relinquish 声明此后不再分配，dealloc 归还列；且"每一次 TMEM 分配都必须在内核退出前显式释放"（[chapter_tmem/index.md:L78](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tmem/index.md#L78)）。省略 dealloc 会留下未释放的列，内核结束时资源未归还—— alloc 本身"可能等待空闲列"（L39）正说明列是会紧张的共享资源。至于顺序反转，书未直接讨论其后果（「待确认」，需查 PTX ISA）；但所有示例一律先 relinquish 再 dealloc，且语义上"先声明不再申请、再归还"更安全，应照模板执行。

**练习 3**：`tcgen05.alloc(T.address_of(tmem_addr), ...)` 与 `tcgen05.dealloc(tmem_addr[0], ...)` 一个用取址、一个用取值，为什么？

**答案**：alloc 需要**一个地方写入**分配结果的基地址，所以传入 SMEM 槽 `tmem_addr` 的地址；dealloc 需要**知道要释放哪段**，所以传入槽里已写好的值（TMEM 基地址）。`T.decl_buffer(allocated_addr=tmem_addr[0])` 与 dealloc 同侧，读的是值。

### 4.2 模块二：分配尺寸限制

#### 4.2.1 概念说明

尺寸限制有三条：

1. **档位限制**：`n_cols` 只能取 32、64、128、256、512（[chapter_tmem/index.md:L24](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tmem/index.md#L24)）。
2. **单调不增规则**：同一 CTA 按程序序做多次分配时，**后面的分配不能比前面的申请更多列**。`256 → 128` 合法，`128 → 256` 非法（[chapter_tmem/index.md:L59-L68](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tmem/index.md#L59-L68)）。
3. **设计推论**：内核必须在**设计分配序列时就想好最大需求**，不能"先小后大"地扩容。

第三条解释了书中一个普遍现象：所有 GEMM 内核一律 `n_cols=512`，哪怕 Step 1 只用 `tmem[:, :128]`（BLK_N=128）。按单调不增规则，起步就申请最大档是最省心的模式；后续步骤与 FA4 要在同一片 TMEM 上摆放多个缓冲，需要的列也确实更多。这是从规则出发的合理动机（书中未逐字解释 Step 1 为何给 512，「推断」，但 L68 的原文"必须一开始就确定最大 TMEM 需求"正是这条原则）。

容量账很好算：满配 TMEM 为

\[
128 \text{ Lane} \times 512 \text{ Col} \times 4 \text{ B} = 262144 \text{ B} = 256 \text{ KiB}
\]

即每 CTA 最多 256 KiB 的 TMEM——与 B200 每 SM 228 KiB 的 SMEM 是**两份独立**的资源（u3-l3 讲资源压力时要分别记账）。

#### 4.2.2 核心流程

规划一次 TMEM 分配的决策过程：

```text
统计内核需要放进 TMEM 的所有缓冲（累加器、scale factor、S/P 中间量…）
        │
        ▼
总列数 = max over 生命周期各阶段的并发占用（不是简单求和）
        │
        ▼
向上取整到合法档位 {32, 64, 128, 256, 512}
        │
        ├─ 一次分够 ──► 单条 alloc(n_cols=最大档)，最省心（书中所有内核的做法）
        │
        └─ 分多次   ──► 序列必须单调不增：alloc(256) → alloc(128) → alloc(64) …
                          且每次 dealloc 的 n_cols 与对应 alloc 一致（书中示例皆如此）
```

#### 4.2.3 源码精读

**（1）规则原文。** [chapter_tmem/index.md:L59-L68](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tmem/index.md#L59-L68) 给出 `256 → 128` 合法、`128 → 256` 非法的对照，并落到设计结论："内核必须在设计分配序列时就确定最大 TMEM 需求，而不是之后再扩"。

**（2）书中内核统一用 512。** 用 grep 即可验证：`chapter_intro_tirx`、`chapter_gemm_basics`、`chapter_gemm_async`、`chapter_gemm_advanced` 中每一处 `tcgen05.alloc` 都是 `n_cols=512`，每一处 `tcgen05.dealloc` 也都是 `n_cols=512`——alloc 与 dealloc 成对、档位一致。例如 [chapter_gemm_basics/index.md:L223](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L223) 与 [L271](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L271)。

**（3）分配 512、只用前 128。** Step 1 声明的缓冲是 `(128, 512)`（[chapter_gemm_basics/index.md:L229-L232](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L229-L232)），但 MMA 只写 `tmem[:, :BLK_N]`、写回也只读 `tmem[:, :BLK_N]`（[L249](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L249)、[L261](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L261)）——"分配的档位"与"使用的子区间"是两回事，列的占用按缓冲切片在布局坐标里自行安排。

**（4）图脚本里的档位标注。** [img/scripts/gen_tmem_grid.py:L63-L65](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_tmem_grid.py#L63-L65) 在横轴标题写明"up to 512 32-bit columns (allocated in units of 32)"；脚本 [L28-L32](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_tmem_grid.py#L28-L32) 每 64 列画一条浅网格线只是视觉参考。以正文为准：合法档位是 32/64/128/256/512 五档。

#### 4.2.4 代码实践

**实践：分配序列合法性判题 + 容量计算**

1. **实践目标**：把三条限制变成能机械执行的判题函数。
2. **操作步骤**：
   - 写一个 Python 小脚本（示例代码，非仓库原有）：

     ```python
     LEGAL = {32, 64, 128, 256, 512}   # 合法档位：chapter_tmem L24

     def check_sequence(seq):
         """seq: 按程序序的 n_cols 列表，返回 (整体合法?, 逐条原因)"""
         results = []
         for i, n in enumerate(seq):
             if n not in LEGAL:
                 results.append((i, n, "非法档位"))
             elif i > 0 and n > seq[i - 1]:
                 results.append((i, n, "违反单调不增（后期申请更多列）"))
             else:
                 results.append((i, n, "OK"))
         return all(r[2] == "OK" for r in results), results

     for seq in [[512], [256, 128], [128, 256], [512, 512, 256], [64, 64, 100]]:
         print(seq, "->", check_sequence(seq)[0], check_sequence(seq)[1])
     ```
   - 再补一个容量函数：`bytes_used(n_cols) = 128 * n_cols * 4`，打印 32/128/512 三档的字节数。
3. **需要观察的现象**：`[128, 256]` 与 `[64, 64, 100]` 被拒（分别违反单调不增与档位限制）；`[512, 512, 256]` 通过——多次分配是被允许的，只要单调不增。
4. **预期结果**：五组判题输出与 4.2.1 的三条规则一一对应；`bytes_used(512) = 262144`（256 KiB），`bytes_used(128) = 65536`（64 KiB）。可另跑 `python gen_tmem_grid.py`（在 `img/scripts/` 下，见 [脚本 L8](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_tmem_grid.py#L8)）重出网格图对照。

#### 4.2.5 小练习与答案

**练习 1**：一个内核先 `alloc(128)` 用完释放，再 `alloc(256)`，违规吗？

**答案**：违规。规则针对的是**程序序**的分配请求，不看中间是否释放过：后面的分配不能比前面的申请更多列（`128 → 256` 非法，[chapter_tmem/index.md:L61-L66](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tmem/index.md#L61-L66)）。想要 256 就应该在第一次就申请 256。（释放后再申请同一档位是否重置"历史最大"，书中未展开，「待确认」。）

**练习 2**：Step 1 只需要 128 列累加器，为什么分配 512？

**答案**：三条理由（前两条有原文支撑，第三条为推断）：① 单调不增规则让"先小后大"不可行，起步取最大档最安全（L68）；② 全书九个 step 与 FA4 复用同一套骨架，后续要在 TMEM 上摆更多缓冲（如 Step 8 累加器占满 256 列、FA4 还要摆 S/P）；③ 统一 `n_cols=512` 让 alloc/dealloc 成对书写不出错。代价是名义上占用整块 TMEM——由于一个 CTA 的 TMEM 本来也只有这一个内核在用，书选择不省这笔。

**练习 3**：`n_cols=512` 的分配占多少字节？它和 SMEM 是同一份预算吗？

**答案**：\( 128 \times 512 \times 4 = 256 \) KiB。不是同一份：TMEM 与 SMEM 是相互独立的资源（u2-l2），occupancy 分析时 TMEM 列数要单独记一笔（u3-l3 的资源压力清单）。

### 4.3 模块三：warp 的 32-lane 访问窗口

#### 4.3.1 概念说明

TMEM 属于整个 CTA，但 **`tcgen05.ld` / `tcgen05.st` 不让每个 warp 都摸到全部 128 个 Lane 位置**。warpgroup 内四个 warp 各自锁定一个 32-lane 窗口：

| warp 在 warpgroup 内的编号 | 可访问的 TMEM Lane 位置 |
| --- | --- |
| 0 | 0-31 |
| 1 | 32-63 |
| 2 | 64-95 |
| 3 | 96-127 |

四个要点：

1. **判据是"warp 在其 warpgroup 内的编号"**，与它是第几个 warpgroup 无关；两个 warpgroup 的 warp 0 访问的是同一个窗口 0-31（TIRx 中这个量就是 `T.warp_id_in_wg([4])`）。
2. **列方向完全开放**：所有 warp 都能访问每一列，受限的只有 Lane 窗口。
3. **这条限制只作用于 `tcgen05.ld`/`tcgen05.st` 这条 warp 级数据通路**。`tcgen05.mma` 写累加器走的是 Tensor Core 自己的通路（u7-l2 讲的 `warp-rank % 4` 四分区是硬件数据通路的事），不经过 warp 的 lane 窗口。
4. **"warpgroup 读 TMEM"的准确含义**：读一个横跨 128 Lane 的累加器需要**四次 warp 级访问**，每个窗口一次——这就是前几讲反复使用的那句"由 4 个 warp 各读自己 32-lane 窗口"的出处。

#### 4.3.2 核心流程

warp \( w \)（warpgroup 内编号，\( w \in \{0,1,2,3\} \)）的可达集合：

\[
\text{TLane} \in [\,32w,\; 32w+31\,], \qquad \text{TCol} \in [0, \text{分配列数})
\]

于是一个 warpgroup 读回完整累加器的形状是：

```text
for w in 0..3:                        # 四个 warp 并发，各管一段
    该 warp 用 tcgen05.ld 读 TLane ∈ [32w, 32w+31] × 所需列
    每线程拿到自己 lane 对应的数据 → wait::ld → 转换 dtype → 写 GMEM
```

线程与 TMEM 行的对应由此固定：warp \( w \) 内 lane \( l \) 的线程对应 TMEM 行 \( 32w + l \)。这与 `cta_group::1` M=128 的恒等映射（行 m 直映 TLane，u7-l2）拼起来，正好解释了写回时"线程 tid_in_wg 处理第 tid_in_wg 行"的写法。

#### 4.3.3 源码精读

**（1）规则原文与表格。** [chapter_tmem/index.md:L86-L97](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tmem/index.md#L86-L97)：TMEM 属于 CTA，但 `tcgen05.ld/st` 不给每个 warp 全部 128 Lane；四 warp 各占一个 32-lane 窗口、列向不受限；读横跨 128 Lane 的累加器需要四次 warp 级访问。章节开头的 Overview 第二条（[L8](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tmem/index.md#L8)）把这条规则与 `tcgen05.ld/st` 的 warp 集体性绑在一起表述。

**（2）Step 1 写回路径的印证。** [chapter_gemm_basics/index.md:L256-L265](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L256-L265)：

```python
Dreg_wg = Dreg.view(128, BLK_N,
                    layout=TileLayout(S[(128, BLK_N) : (1@tid_in_wg, 1)]))
Tx.wg.copy_async(Dreg_wg[:, :], tmem[:, :BLK_N])
T.ptx.tcgen05.wait.ld()
...
m_thr = T.meta_var(m_st + warp_id * 32 + lane_id)
Tx.copy(D[m_thr, n_st : n_st + BLK_N], Dreg_f16[:])
```

三处互相咬合：寄存器视图的布局用 `1@tid_in_wg` 把第 `tid_in_wg` 行分给编号为 `tid_in_wg` 的线程；`tid_in_wg = warp_id * 32 + lane_id` 恰是 \( 32w + l \)——线程编号就是它有资格访问的 TMEM 行；最后写 GMEM 时 `m_thr = m_st + warp_id * 32 + lane_id`，输出矩阵的行号与 TMEM 行号严格一致。**lane 窗口规则不是抽象约束，它直接决定了这行地址计算**。

**（3）Step 8 的角色变量。** [chapter_gemm_advanced/index.md:L453-L456](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L453-L456) 声明 `wg_id = T.warpgroup_id([WG_NUMBER])`、`warp_id = T.warp_id_in_wg([4])`：TMEM alloc 的守卫用的是 `wg_id == 0 and warp_id == 0`（[L478-L479](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L478-L479)），而各 warpgroup 的写回 warp 读 TMEM 时按同一 `warp_id` 落进各自窗口——同一变量在两种角色里含义不同（发起分配的 warp vs 读数据的 warp）。

**（4）网格图脚本。** [img/scripts/gen_tmem_grid.py:L16-L18](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_tmem_grid.py#L16-L18) 定义 `NCOL=512`、`NROW=128`、`ACC_N=256`；[L34-L40](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_tmem_grid.py#L34-L40) 把"一个累加器 \( S[(128,256):(1@\text{TLane},1@\text{TCol})] \)"画成占前 256 列的绿色矩形；[L29-L32](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_tmem_grid.py#L29-L32) 每 32 行画一条浅横线——恰好就是四个 32-lane 窗口的边界。

#### 4.3.4 代码实践

**实践：运行并改造 tmem 网格图，把 lane 窗口画出来**

1. **实践目标**：用可视化把"128 Lane × 512 TCol 的分配"与"四 warp 的 32-lane 窗口"钉在一起。
2. **操作步骤**：
   - 进入 `img/scripts/` 运行 `python gen_tmem_grid.py`（脚本注释 [L8](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_tmem_grid.py#L8) 写明在 `img/scripts/` 下运行、输出到 `../tmem_grid.png`）；
   - 把 `ACC_N` 从 256 改成 128 重跑（对应 Step 1 "分配 512、只用 128"的占用关系）；
   - 在 `main()` 里追加四条横带，把四个 warp 窗口着色（示例代码，非仓库原有）：

     ```python
     for w in range(4):
         ax.add_patch(Rectangle((0, 32 * w), NCOL, 32, fill=False,
                                edgecolor=["#e63946", "#457b9d", "#2a9d8f", "#e9c46a"][w],
                                linewidth=1.8, linestyle="--", zorder=6))
         ax.text(NCOL + 2, 32 * w + 16, f"warp {w}", fontsize=9,
                 color="#333333", va="center")
     ```
   - 对照 [chapter_gemm_basics/index.md:L259-L265](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L259-L265) 的 `m_thr = m_st + warp_id * 32 + lane_id`，在图上找到"线程 96 号（warp 3 的 lane 0）"应读的行。
3. **需要观察的现象**：四条虚线横带与脚本原有的每 32 行浅网格线重合；`ACC_N=128` 后绿色矩形变窄但整幅网格仍是 512 列宽——"分配档位"与"使用区间"分离的直观呈现。
4. **预期结果**：一张标注了四个 warp 窗口的 `tmem_grid.png`；能指着图回答"warp 3 的 lane 0 对应 TLane 96"。本实践只需 Python + matplotlib，无需 GPU。

#### 4.3.5 小练习与答案

**练习 1**：一个 warpgroup要读 `cta_group::1` M=128 累加器的全部 128 行，至少几次 warp 级 `tcgen05.ld`？如果内核有两个 warpgroup 都参与读回呢？

**答案**：4 次——每个 warp 只能读自己那 32 根 Lane（[chapter_tmem/index.md:L97](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tmem/index.md#L97)）。两个 warpgroup 时仍是各自 4 次窗口访问（判据是 warpgroup 内编号），除非内核显式划分"wg0 读行 0-63、wg1 读行 64-127"之类的分工——那也只是把 128 次线程级工作分组，窗口规则本身不变。

**练习 2**：warp 2 的 lane 17 对应哪一行 TMEM？它写 GMEM 时应该写输出矩阵的哪一行（Step 1，`m_st=0`）？

**答案**：\( 32 \times 2 + 17 = 81 \)，即 TLane 81；GMEM 行同样是 81（`m_thr = m_st + warp_id*32 + lane_id`，[chapter_gemm_basics/index.md:L264](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L264)）。M=128 恒等映射下 TMEM 行、累加器行、输出行三者同号。

**练习 3**：lane 窗口限制会妨碍 `tcgen05.mma` 把累加器写到 TLane 96-127 吗？

**答案**：不会。窗口限制只约束 `tcgen05.ld`/`tcgen05.st` 这条 warp 级读写通路；`tcgen05.mma` 由 Tensor Core 硬件通路直接写 TMEM（u7-l1），不经过任何 warp 的 lane 窗口。所以"写满 128 行"与"分四个窗口读回"是不对称的两件事。

## 5. 综合实践

**任务**：为 GEMM Step 1 与 Step 8 各建一份"TMEM 资源台账"，并把 lane 窗口叠加进网格图，最终形成一页可复查的 TMEM 管理速查卡。

1. **台账字段**（每个内核一行）：地址槽大小与位置（`uint32`、SMEM 低地址区）｜守卫（warp/wg）｜`n_cols`｜缓冲形状与布局（`(128,512)` + `S[(128,512):(1@TLane,1@TCol)]`）｜实际使用的列区间（`[:, :128]` vs `[:, :256]`，Step 8 的 256 列输出见 [chapter_gemm_advanced/index.md:L419](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L419)）｜每个 warp 的 lane 窗口与它写的 GMEM 行段｜释放前的同步（`cta_sync` vs `cluster_sync`）｜`cta_group` 值。
2. **源码核对**：Step 1 用 [chapter_gemm_basics/index.md:L210-L271](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L210-L271)；Step 8 用 [chapter_gemm_advanced/index.md:L453-L495](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L453-L495) 与 [L590-L593](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L590-L593)。重点标出两份台账中**不同**的三格（守卫、同步、cta_group）。
3. **图**：完成 4.3.4 的改造脚本（四色窗口横带 + `ACC_N` 分别取 128 与 256 各出一图），与台账并排贴进速查卡。
4. **（可选，需 Blackwell GPU 与 `tvm.tirx` 环境）**：按 [chapter_gemm_basics/index.md:L276](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L276) 的提醒（每个新 Python 会话只编译一个 step），分别编译运行 Step 1，把 `n_cols` 从 512 改成 128（其余不动）观察是否仍 PASS；再故意删掉 `relinquish_alloc_permit` 观察编译器/运行期反应。无 GPU 时把这两步写成"预期行为 + 待本地验证"清单。
5. **自查**：用章末的四问（[chapter_tmem/index.md:L160](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tmem/index.md#L160)）的前三问过一遍台账——分了多少列/怎么释放、当前 warp 能碰哪些 Lane、每次 ld/st 产生多少寄存器（第三问的 shape/num 属下一讲，可先留空）。

**预期成果**：两行台账、两张标注图、一份差异说明（三格不同），以及（可选的）两组实验观察或"待本地验证"清单。

## 6. 本讲小结

- TMEM 是运行期动态分配的资源：沿**列维度**申请，合法 `n_cols` 为 32/64/128/256/512；分一列即占满 128 根 Lane；满配 \( 128 \times 512 \times 4\,\text{B} = 256 \) KiB，与 SMEM 是两份独立预算。
- 生命周期七步：SMEM 预留 `uint32` 槽 → warp 0 **集体**执行 `tcgen05.alloc`（阻塞、结果写进槽）→ fence + `cta_sync` 发布 → `T.decl_buffer(allocated_addr=tmem_addr[0])` 绑定布局 → 使用 → 确保异步操作完成 → 先 `relinquish_alloc_permit` 再 `dealloc`，alloc 与 dealloc 的 `n_cols` 成对相同。
- 尺寸规则：同一 CTA 程序序多次分配必须**单调不增**（`256→128` 合法、`128→256` 非法），因此要一开始就定最大需求——这就是书中所有内核一律 `n_cols=512`、再按 `tmem[:, :BLK_N]` 切区间使用的原因。
- `cta_group::2` 时 CTA 对**两侧各出一个 warp 执行同一条** alloc/dealloc、各在自己的 TMEM 分得同样多列，先到者可能等对端；同一内核内所有带 `cta_group` 限定符的指令必须用同一个值；释放前用 `cluster_sync()` 保证双方用完。
- warp 的 32-lane 窗口：`tcgen05.ld/st` 只让 warpgroup 内编号 \( w \) 的 warp 访问 \( \text{TLane} \in [32w, 32w+31] \)，列向不受限；读 128 行累加器 = 四次 warp 级访问；Step 1 的 `m_thr = warp_id*32 + lane_id` 正是这条规则落到地址计算上的样子。

## 7. 下一步学习建议

- **下一讲 u7-l4（tcgen05.ld/st：数据搬运、打包与异步等待）**：本讲只回答了"哪个 warp 能碰哪些 Lane"，还没讲"一次搬多少、16-bit 数据怎么打包进 32-bit 列、`wait::ld`/`wait::st` 放在哪"——`.shape`/`.num`/`.pack::16b` 全在下一讲。
- **u8-l1、u8-l2（mbarrier 与 phase）**：本讲生命周期第⑥步"确保异步操作完成"的通用协议；TMEM 读写要跨线程交接时，光 wait 不够，还需 barrier + `tcgen05.fence`。
- **u13-l2（GEMM Step 8）**：本讲看到的 `wg_id`/`warp_id` 双重守卫、`cluster_sync` 释放、`cta_group=2` 会在完整内核里展开成角色分工与跨 CTA 屏障协议。
- **u14-l4（FA4 的 TMEM 布局复用）**：多个缓冲（S/P/O）共享 512 列、靠屏障防护重叠区域的实战，是对本讲"分配档位 vs 使用区间"的极限压榨。
- **PTX ISA 文档的 `tcgen05.alloc`/`relinquish_alloc_permit` 一节**：多次分配后按序释放的精确语义、与书中"单调不增"规则对应的官方表述，是本讲两处「待确认」的查证入口。
