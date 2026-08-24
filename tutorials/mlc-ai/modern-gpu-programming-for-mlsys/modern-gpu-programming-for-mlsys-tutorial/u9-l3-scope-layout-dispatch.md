# u9-l3 Scope、Layout、Dispatch 三要素

## 1. 本讲目标

前两讲（u9-l1、u9-l2）已经让 `hgemm_v1` 跑了起来：我们知道它按「分配 → 拷贝 → MMA → 回写」四阶段组织，也走通了一遍「编译 → 数值验证 → 两级检视」的回路。本讲停下来做一件更重要的事——**给这台机器拆开看齿轮**。

读完本讲，你应该能够：

1. **为任意 tile 操作标注三要素**：拿出 `hgemm_v1` 里的任何一个 `Tx.*` 调用，说清它的 scope（哪些线程执行它）、layout（它的数据每个元素摆在哪个物理位置）、dispatch（它最终走哪条硬件路径）。
2. **预测修改某一要素后的行为变化**：例如把 GMEM→SMEM 拷贝从「全 CTA 线程执行」改成「TMA 派发」，能提前列出需要改动哪几类代码位置。
3. **理解编译器如何由三要素推出具体实现**：理解 `LowerTIRx` 为什么只凭这三样信息，就能把一条 `Tx.gemm_async` 展开成线程级守卫、地址计算和一串 `tcgen05.mma` 指令。

这三个问题不是我们发明的，它们写在章节正文的第一段结论里：TIRx 中每项 tile 操作都需要回答「由谁执行、数据放在哪里、使用哪种硬件实现」——[chapter_intro_tirx/index.md:209-211](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L209-L211)（中文版同页同行：[zh/chapter_intro_tirx/index.md:209-211](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/zh/chapter_intro_tirx/index.md#L209-L211)）。

## 2. 前置知识

本讲是单元九的收尾，会把前面大量零散结论「拧成一股绳」。先用通俗语言把要用到的概念过一遍。

**tile 操作与底层辅助调用（u9-l1）**。TIRx 内核里的调用分两层：`Tx.*` 是 tile 操作，只声明「对一整块 tile 做什么」（如拷贝、矩阵乘、类型转换），不写怎么做；`T.ptx.*` / `T.cuda.*` 是底层辅助，负责资源与同步（分配 TMEM、初始化 barrier、`cta_sync` 等）。本讲分析的对象是前者。

**线程层级与「发起 ≠ 执行」（u2-l1、u7-l1）**。一个 CTA 在本书内核里有 128 个线程（4 个 warp 组成一个 warpgroup）。很多硬件操作是「一个线程发起、硬件执行」：`tcgen05.mma` 由单个线程提交，真正的矩阵乘由 Tensor Core 完成。所以「谁执行一项操作」要区分**发起者**和**执行者**两个层面。

**命名轴布局记号（u4-l2）**。布局写成 `S[(shape) : (strides)]`，strides 上用 `@轴名` 标注每个维度落到哪根物理轴，例如 `4@laneid` 表示「乘 4 后贡献到 laneid 这根轴」。本讲会见到三根新轴的用法：`TLane`/`TCol`（TMEM 的二维地址）和 `tid_in_wg`（warpgroup 内 0–127 的线程编号）。检验布局是否合法的基本工具是**元素数守恒**：逻辑元素总数必须等于各物理轴取值数之积（双射）。

**tcgen05.mma 的数据路径（u7-l1、u5-l2）**。Blackwell 的 MMA 指令不经过寄存器：A、B 用矩阵描述符直接从 SMEM 读（描述符要求 SMEM 是特定 swizzle 模式的排布），累加结果写进 TMEM。一条指令沿 K 前进 16 个元素，长 K 由多条指令接力，首条 `accum=0` 清零写入、后续 `accum=1` 累加。

**TMA 与 mbarrier（u6、u8）**。TMA 让单个线程发起整块 tile 搬运、由 TMA 引擎执行；load 的完成靠 mbarrier 的字节数计数（`arrive.expect_tx` 登记在途字节，引擎每完成一段就扣减），`cta_sync` 只能同步线程、观察不到引擎进度。这些结论将在本讲的「改 TMA」预测题里直接用上。

如果对以上任何一条只有模糊印象，建议先回看对应讲义再继续——本讲的价值恰恰在于把这些结论装进同一个框架。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注的部分 |
| --- | --- | --- |
| [chapter_intro_tirx/index.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md) | TIRx 入门章正文（英文），含 `hgemm_v1` 完整源码与三要素讨论 | L30-40 动机、L84-170 内核清单、L209-228 三要素讨论、L230-252 编译 |
| [zh/chapter_intro_tirx/index.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/zh/chapter_intro_tirx/index.md) | 上一文件的中文镜像，与英文版**逐行对齐**（行号完全相同） | 同上；中文读者可对照阅读 |
| [_extra/demo/tirx_dispatch.html](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/tirx_dispatch.html) | 交互式演示：摘出内核关键行，点 Scope/Layout/Dispatch 按钮高亮对应行 | L62-99 代码行数组、L100-105 三要素解说词 |

正文通过 iframe 嵌入这个演示（[chapter_intro_tirx/index.md:215-220](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L215-L220)），中文版嵌的是它的中文镜像 `demo_zh` 版本（见 [zh/chapter_intro_tirx/index.md:215-220](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/zh/chapter_intro_tirx/index.md#L215-L220) 中的 iframe 地址）。本讲以英文版演示为引用对象，两者逻辑相同。

## 4. 核心概念与源码讲解

先看全书对这三要素的「官方定义」。章节开头的动机段说：同样的工作直接用 CUDA/PTX 也能做，但底层程序会把几个关键决定**分散**在 intrinsic 参数、地址计算和代码约定里，编译器很难把它们当作一个整体来检查和变换——[chapter_intro_tirx/index.md:30-32](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L30-L32)。TIRx 的回答是把这三个决定显式写进结构化 IR：

```text
- **Scope**: which threads execute an operation;
- **Layout**: how a logical tile maps to memory, lanes, or registers;
- **Dispatch**: which hardware implementation executes a tile operation.
```

> 引自 [chapter_intro_tirx/index.md:34-38](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L34-L38)（中文版 [zh/chapter_intro_tirx/index.md:34-38](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/zh/chapter_intro_tirx/index.md#L34-L38)）：三要素的正式定义。

注意一个漂亮的对应关系：CUDA 里这三个决定分别藏在**代码约定**（谁到达这条指令）、**地址计算**（元素摆哪）和 **intrinsic 参数**（用哪条指令）里——而它们恰好对应编译器生成代码的三个侧面：**控制流、地址计算、指令选择**。这就是本讲的分析框架：

| 要素 | 回答的问题 | 在内核源码里的样子 | 决定生成代码的哪个侧面 |
| --- | --- | --- | --- |
| scope | 谁执行？ | 操作名前缀（`Tx.cta.` / `Tx.wg.`）+ `if` 守卫 | 线程级控制流（守卫、每线程循环） |
| layout | 数据摆哪？ | `layout=` 参数、`TileLayout(...)` | 地址计算 |
| dispatch | 走哪条硬件路径？ | `dispatch="tcgen05"` 等参数 | 指令选择（intrinsic / 引擎） |

下面逐个精读。

### 4.1 模块一：Scope——谁执行这项 tile 操作

#### 4.1.1 概念说明

scope 回答的是：当这条 tile 操作被 lowering 之后，**哪些线程会真正执行它生成的代码**。

初学者最容易犯的误解是：「取得线程编号」就是 scope。演示里专门有一条注释纠正这一点——线程/lane ID 的取得（`T.warp_id_in_wg`、`T.lane_id`）本身**不是** scope，它们只是给你写守卫和控制流用的原材料（见 [_extra/demo/tirx_dispatch.html:74-75](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/tirx_dispatch.html#L74-L75) 中标注为 `(not scope)` 的那一行）。真正定义 scope 的是两类东西：

1. **操作名自带的作用域**：`Tx.cta.copy` 的 `cta`、`Tx.wg.copy_async` 的 `wg`（warpgroup）——操作名本身就声明了它的协作单位。
2. **`if` 守卫**：`if warp_id == 0:`、`if T.ptx.elect_sync():` 把执行者进一步收窄。`hgemm_v1` 里 MMA 被**两层守卫**包住，最终只剩一个被选中的线程发起它。

还有一个贯穿始终的区分：**发起者 ≠ 执行者**。MMA 由 1 个线程发起，但矩阵乘是 Tensor Core 硬件做的；`Tx.cta.copy` 由 128 个线程一起做，谁也不多谁也不少。分析 scope 时要同时说出这两个层面。

#### 4.1.2 核心流程

编译器拿到一个 tile 操作的 scope 后，会生成对应的线程级控制流。用伪代码表示（**示例伪代码**，帮助理解，非项目源码）：

```text
scope = CTA（Tx.cta.copy）              scope = 单线程发起（Tx.gemm_async）
─────────────────────────              ─────────────────────────────
每个线程 tid ∈ [0,128) 各自计算          if (warp_id == 0 && elected):
    本线程负责搬运的元素集合                  发出整块 MMA 的指令描述
    GMEM 读 → SMEM 写                        tcgen05.commit 挂到 mbarrier
随后 __syncthreads() 汇合              所有线程: mbarrier.try_wait 等完成

scope = warpgroup（Tx.wg.copy_async）   scope = 每线程各自（Tx.cast / Tx.copy）
─────────────────────────              ─────────────────────────────
128 个线程执行同一条集体指令，            每个线程处理自己名下的那部分元素，
硬件按 tid_in_wg 把 TMEM 行              互不通信、无需汇合
分发到各线程的寄存器
```

可以看到：scope 不同，生成的控制流形状完全不同——CTA 级要「分工 + 汇合」，单线程级要「守卫」，warpgroup 级要「集体执行」，线程级要「各自为政」。

#### 4.1.3 源码精读

**(1) 原材料：线程层级 API。** 内核开头用四个 API 取得线程坐标：

[chapter_intro_tirx/index.md:104-107](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L104-L107) —— `T.cta_id` / `T.warpgroup_id` / `T.warp_id_in_wg` / `T.lane_id` 分别取得 CTA 坐标、warpgroup 编号、warp 编号和 lane ID。正文 L82 说明这些 API「暴露线程层级，用于定义 tile 坐标和执行守卫」——注意措辞：它们是给守卫用的，本身不定义 scope。

**(2) CTA 级 scope：全 CTA 协作拷贝。**

[chapter_intro_tirx/index.md:137-140](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L137-L140)

```python
        # --- Load: all threads synchronously copy A and B from GMEM to SMEM ---
        Tx.cta.copy(Asmem[:, :], A[m_st:m_st + BLK_M, :])
        Tx.cta.copy(Bsmem[:, :], B[n_st:n_st + BLK_N, :])
        T.cuda.cta_sync()
```

这段代码做的事情：两条 `Tx.cta.copy` 由 CTA 内全部 128 个线程协作执行，把 A、B 两个 tile 从 GMEM 搬进 SMEM；搬完用 `cta_sync()` 汇合（它会被 lowering 成 `__syncthreads()`，u9-l2 已验证过这条映射）。操作名里的 `cta` 就是 scope 声明。

**(3) 单线程 scope：两层守卫包住 MMA。**

[chapter_intro_tirx/index.md:142-151](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L142-L151)

```python
        # --- Compute: one elected thread issues the MMA ---
        if warp_id == 0:
            if T.ptx.elect_sync():
                Tx.gemm_async(...)
                T.ptx.tcgen05.commit(mma_bar.ptr_to([0]), cta_group=1)

        T.ptx.mbarrier.try_wait(mma_bar.ptr_to([0]), phase_mma)
```

这段代码做的事情：`warp_id == 0` 先把范围从 4 个 warp 收窄到 1 个，`elect_sync()` 再从中选出唯一一个线程去发起 `Tx.gemm_async` 并把它 commit 到 mbarrier。两行守卫就是 scope 的全部代码体现。注意 `try_wait` 写在守卫**外面**——MMA 的结果随后要被整个 warpgroup 消费，所以等待是全员参与的。

顺带观察一个有趣的对比：[chapter_intro_tirx/index.md:119-122](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L119-L122) 里初始化 mbarrier 用的是 `if lane_id == 0:`，而发起 MMA 用的是 `elect_sync()`——两种写法都能把执行者收窄到 warp 0 里的一个线程，是同一 scope 的两种表达。

**(4) warpgroup 级 scope：集体读回。**

[chapter_intro_tirx/index.md:158-159](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L158-L159) —— `Tx.wg.copy_async(Dreg_wg[:, :], tmem[:, :BLK_N])` 不在任何守卫内，由整个 warpgroup 的 128 个线程集体执行，把 TMEM 累加器分发到各线程寄存器；这正是 u7-l4 讲过的 `tcgen05.ld` 的 warp 集体语义。它**不能**被守卫包住——集体指令少一个线程参与就是错的。

**(5) 正文的 scope 总结段。**

[chapter_intro_tirx/index.md:222](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L222)（中文 [zh/chapter_intro_tirx/index.md:222](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/zh/chapter_intro_tirx/index.md#L222)）—— 正文官方总结：`Tx.cta.copy` 全 CTA 128 线程执行；`Tx.gemm_async` 被 `warp_id == 0` 和 `elect_sync()` 双重守卫、只剩一个线程发起；`Tx.wg.copy_async` 由 128 线程协作分发累加器。

**(6) 演示中的 scope 标注。** 演示把内核摘出行数组，每行第二个字段是它归属的设计要素（[_extra/demo/tirx_dispatch.html:61-62](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/tirx_dispatch.html#L61-L62) 注明格式为 `[code text, element|null]`）。被标成 `"scope"` 的行有：两条 `Tx.cta.copy`（[L83-84](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/tirx_dispatch.html#L83-L84)）、两层守卫（[L87-88](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/tirx_dispatch.html#L87-L88)）、`Tx.wg.copy_async`（[L95](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/tirx_dispatch.html#L95)）。演示的解说词还给出了单线程发起的**原因**：`tcgen05.mma` 是单指令协作操作，整块 MMA 只需发起一次（[L101](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/tirx_dispatch.html#L101)）。

#### 4.1.4 代码实践

**实践目标**：用交互演示核对「哪些行受 scope 控制」，并把这些摘出行映射回真实内核清单的行号。

**操作步骤**：

1. 用浏览器打开 `_extra/demo/tirx_dispatch.html`（可直接双击打开，它通过相对路径引用同目录的 `viz-base.css/js`；也可按 u1-l2 的方法本地构建书站后从正文进入）。
2. 点击 **Scope** 按钮（按钮定义在 [_extra/demo/tirx_dispatch.html:50-54](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/tirx_dispatch.html#L50-L54)，点击逻辑在 [L149-157](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/tirx_dispatch.html#L149-L157)：给代码区加 `k-scope` 类、其余行降透明度）。
3. 记录被高亮的行，再打开 [chapter_intro_tirx/index.md:137-162](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L137-L162)，把每条摘出行对应回正文清单里的真实代码行。
4. 再依次点击 **Layout** 和 **Dispatch**，观察三组高亮行是否互不重叠（演示的数据结构保证了每行只归属一个要素或都不归属）。

**需要观察的现象**：点 Scope 时只有协作/守卫相关的行亮起；`T.cuda.cta_sync()`、`T.ptx.tcgen05.commit` 等同步辅助行**不**属于任何一个要素的高亮集——它们是围绕 tile 操作的支撑代码（正文 L68 也是这么划分的）。

**预期结果**：得到一张「演示行 → 正文行号」映射表，例如演示 L83（`Tx.cta.copy` 标 scope）→ 正文 L138；演示 L87-88（守卫）→ 正文 L143-144；演示 L95（`Tx.wg.copy_async`）→ 正文 L158。无浏览器环境时也可直接读 HTML 源码里的 `LINES` 数组（L62-99）完成同样分析。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Tx.gemm_async` 需要 `warp_id == 0` 和 `elect_sync()` **两层**守卫，只用 `elect_sync()` 一层行不行？

**参考答案**：`elect_sync()` 的语义是在**每个 warp 内**各选一个活跃 lane——只用一层时，4 个 warp 会各选出一个线程，共 4 个线程同时发起同一块 MMA 并各自 commit，既造成重复发起，也会让到达数为 1 的 mbarrier（L121 初始化时传入 `1`）的计数出错。先加 `warp_id == 0` 把范围收窄到单个 warp，`elect_sync()` 才真正保证「唯一一个线程发起」。此行为推演自源码语义，具体故障表现**待本地验证**（可在 Blackwell GPU 上删掉外层守卫编译观察）。

**练习 2**：`T.ptx.mbarrier.try_wait(...)`（L151）写在守卫外面，由谁执行？为什么不能也包进 `elect_sync()` 里？

**参考答案**：由 CTA 内全部线程执行。因为等待之后的 `Tx.wg.copy_async`（L158）是 warpgroup 集体操作，需要所有线程都确认「MMA 已完成」再一起前进；如果把 `try_wait` 包进单线程守卫，其余 127 个线程会在未确认完成时就往下执行，读到未就绪的 TMEM。

**练习 3**：演示解说词里说「`Tx.wg.copy_async` 用整个 128 线程 warpgroup 读回累加器」。结合 u7-l4 的 `tcgen05.ld` 语义，说明为什么这条操作**不能**加 `if warp_id == 0:` 守卫。

**参考答案**：`tcgen05.ld` 是 warp 集体指令：warp 内 32 个线程必须执行同一条指令、由硬件按 lane ID 分发数据，且每个 warp 只能读自己的 32-lane 窗口（warp w 读 TLane∈[32w, 32w+32)）。读满 128 行累加器需要 4 个 warp **全部**到场；若只有 warp 0 执行，不仅只读到前 32 行，连 warp 0 自己那条集体指令的语义都不完整。

### 4.2 模块二：Layout——tile 的每个元素放在哪个物理位置

#### 4.2.1 概念说明

layout 回答的是：逻辑上一个 tile 写作 `(128, 64)` 这样的二维索引，但物理世界里**每个元素到底落在哪**——是 SMEM 里某个带 swizzle 的地址？TMEM 的第几条 lane 第几列？还是哪个线程的第几个寄存器槽位？这正是 u4-l1/u4-l2 建立的「布局 = 逻辑索引到物理坐标的函数」在 TIRx 代码里的落地。

`hgemm_v1` 一共出现三族 layout，分别驻守三处存储：

| 存储位置 | 承载数据 | layout 记号 | 物理坐标 |
| --- | --- | --- | --- |
| SMEM | A、B 两个输入 tile | `mma_shared_layout(..., SWIZZLE_128B_ATOM, ...)` | SMEM 线性地址（128B swizzle 原子内重排） |
| TMEM | fp32 累加器 | `TileLayout(S[(128, 512) : (1@TLane, 1@TCol)])` | (TLane, TCol) 二维坐标 |
| 寄存器 | 读回后的结果行 | `TileLayout(S[(128, BLK_N) : (1@tid_in_wg, 1)])` | (线程编号 tid_in_wg, 线程内槽位) |

layout 有一条铁律，正文写得很清楚：**一次 tile 操作读、写两端的 layout 必须对同一逻辑元素给出相同的物理位置**，MMA 和 copy 才能正确工作（[chapter_intro_tirx/index.md:224](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L224)，中文版 [zh/chapter_intro_tirx/index.md:224](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/zh/chapter_intro_tirx/index.md#L224)）。这也是 u5-l2「描述符必须与 SMEM 实际字节一致」、u6-l1「tensor map、SMEM 布局与 MMA 指令必须描述同一物理排布」在 IR 层面的统一表述。

#### 4.2.2 核心流程

沿数据路径看三族 layout 如何交接：

1. **进入 SMEM（A/B）**：`mma_shared_layout(a_type, SwizzleMode.SWIZZLE_128B_ATOM, (BLK_M, BLK_K))` 生成一个带 128B swizzle 原子的 SMEM 布局。为什么必须 swizzle？因为 tcgen05 的 A/B 是经 SMEM 矩阵描述符读取的，描述符的编码约定就是按 swizzle 原子组织的（u5-l2/u7-l1）——layout 在这里不是可选项，而是硬件通路对数据排布的硬性要求。
2. **MMA 落盘 TMEM**：目的操作数是 `tmem[:, :BLK_N]`——在 `(128, 512)` 的 TMEM buffer 上切出前 128 列。元素 \((m, n)\)（\(m < 128,\ n < 128\)）按布局映射到 \(\text{TLane}=m,\ \text{TCol}=n\)，这与 u7-l2 讲过的 cta_group::1、M=128 恒等映射一致，所以这个切片恰好就是 MMA 天然的累加器排布。
3. **TMEM → 寄存器**：`Dreg.view(128, BLK_N, layout=TileLayout(S[(128, BLK_N) : (1@tid_in_wg, 1)]))` 把每个线程私有的 128 元素本地数组 `Dreg` 重新解释为 warpgroup 级的 `(128, 128)` tile：逻辑元素 \((r, c)\) 落在 **线程 `tid_in_wg = r`** 的本地数组第 \(c\) 个槽位。注意第二个维度 stride 是裸的 `1`（没有 `@轴名`）——按 u4-l2 的规则，它落在默认线性轴 `m` 上，即「线程内连续槽位」。
4. **寄存器 → GMEM**：L161 计算 `m_thr = m_st + warp_id * 32 + lane_id`，恰好就是 `tid_in_wg` 的展开式，于是每个线程把自己那**一行**结果写进 GMEM 的 `D[m_thr, :]`。layout 与后续代码在此严格咬合。

验证布局合法性的老工具依然好用——**元素数守恒**：读回这一步 \(128 \times 128 = 16384\) 个逻辑元素，等于 128 个线程 × 每线程 128 个本地槽位，双射成立。

#### 4.2.3 源码精读

**(1) 布局工具的导入。**

[chapter_intro_tirx/index.md:72-78](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L72-L78) —— 从 `tvm.tirx.layout` 导入 `TileLayout, S, TLane, TCol, tid_in_wg`（命名轴记号），从 TVM 的 `tma_utils` 导入 `mma_shared_layout, SwizzleMode`（SMEM 侧布局构造器）。layout 这件事在 TIRx 里有一整套独立 API，u10 单元会专门展开。

**(2) SMEM 布局的构造与挂载。**

[chapter_intro_tirx/index.md:91-93](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L91-L93)

```python
    BLK_M, BLK_N, BLK_K = 128, 128, 64
    A_layout = mma_shared_layout(a_type, SwizzleMode.SWIZZLE_128B_ATOM, (BLK_M, BLK_K))
    B_layout = mma_shared_layout(b_type, SwizzleMode.SWIZZLE_128B_ATOM, (BLK_N, BLK_K))
```

这段代码做的事情：按输入 dtype 与 tile 形状，为 A、B 各生成一个 128B swizzle 原子模式的 SMEM 布局。注意 `BLK_K = 64` 个 fp16 元素恰好占 \(64 \times 2 = 128\) 字节，正好铺满一个 swizzle 原子的宽度（u6-l1 的 128B 约束）。

[chapter_intro_tirx/index.md:114-115](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L114-L115) —— `pool.alloc((BLK_M, BLK_K), a_type, layout=A_layout)`：布局作为 `layout=` 参数挂到 SMEM 分配上，从此 `Asmem` 这个 buffer 名下的每个逻辑元素都有了确定的物理地址。

**(3) TMEM 布局：命名轴登场。**

[chapter_intro_tirx/index.md:128-131](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L128-L131)

```python
        tmem = T.decl_buffer(
            (128, 512), "float32", scope="tmem", allocated_addr=tmem_addr[0],
            layout=TileLayout(S[(128, 512) : (1@TLane, 1@TCol)])
        )
```

这段代码做的事情：用 `T.decl_buffer` 把 `tcgen05.alloc`（L122）分配到的 TMEM 地址绑定成一个 `(128, 512)` 的 fp32 buffer，并用命名轴布局声明「第 0 维走 TLane、第 1 维走 TCol、步长都是 1」——u4-l2 的记号在这里原样出现。`scope="tmem"` 同时声明了这块数据的存储层级（u2-l2 的四种存储空间在 TIRx 里对应 buffer 的 scope 参数）。

**(4) 寄存器布局：每线程一行。**

[chapter_intro_tirx/index.md:156-157](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L156-L157)

```python
        Dreg_wg = Dreg.view(128, BLK_N,
                            layout=TileLayout(S[(128, BLK_N) : (1@tid_in_wg, 1)]))
```

这段代码做的事情：给每线程私有的本地数组 `Dreg`（L154，形状 `(BLK_N,)`）加一个 warpgroup 级视图——行号映射到线程 `tid_in_wg`，列号映射到线程内槽位。**layout 在这里把「scope（128 个线程）」和「数据（每人 128 个值）」缝合在一起**：没有这个视图，编译器无从知道 `Tx.wg.copy_async` 该把 TMEM 的哪一行发给哪个线程。

**(5) 演示中的 layout 标注。**

[_extra/demo/tirx_dispatch.html:68-69](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/tirx_dispatch.html#L68-L69)（`A_layout`/`B_layout` 两行）、[L77-81](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/tirx_dispatch.html#L77-L81)（SMEM 分配与 `tmem` 的 `decl_buffer`，含 `TileLayout(S[(128, 512) : (1@TLane, 1@TCol)])`）都被标为 `"layout"`。演示解说词（[L102](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/tirx_dispatch.html#L102)）概括：A/B 放进 Tensor Core 所要求的 swizzled `mma_shared_layout`，累加器以 `TLane`/`TCol` 布局住进 TMEM，MMA 正是读这些布局才知道 tile 的物理排布。

#### 4.2.4 代码实践

**实践目标**：手推 `hgemm_v1` 中三个关键布局的元素映射，并用元素数守恒验证双射性。

**操作步骤**：

1. 对 TMEM 累加器布局，写出元素 \((m, n)\) 的物理坐标（答案：\(\text{TLane}=m\)，\(\text{TCol}=n\)，其中 \(n < 128\) 是 `tmem[:, :BLK_N]` 切片内的列号）。
2. 对 `Dreg_wg` 视图，写出元素 \((r, c)\) 的物理坐标（答案：线程 \(\text{tid\_in\_g}=r\) 的本地数组第 \(c\) 槽）。
3. 做两次守恒检查：TMEM 侧 \(128\times128 = 16384\) 个元素对 128 lane × 128 列；寄存器侧 \(128\times128\) 对 128 线程 × 128 槽。
4. 若已按 u1-l3 装好 `apache-tvm==0.26.0`（无需 GPU），把正文的导入（L72-78）与内核（L84-170）抄进一个 `hgemm_v1.py` 文件（TIRx 依赖源码检视，不能放 `python -c`），然后运行下面的**示例代码**打印 IR 里的布局：

```python
# 示例代码：仅构造 IR 并打印，不需要 GPU
from hgemm_v1 import hgemm_v1        # 抄自正文 L84-L170
kernel = hgemm_v1(128, 128, 64)
kernel.show()                        # 或 print(kernel.script())
```

**需要观察的现象**：打印出的 PrimFunc 中，`Asmem`/`Bsmem`/`tmem`/`Dreg_wg` 各自携带的 layout 注解与正文 L92-93、L128-131、L156-157 一致。

**预期结果**：三张映射表全部通过守恒检查；`kernel.show()` 的输出中能找到 `S[(128, 512) : (1@TLane, 1@TCol)]` 与 `S[(128, 128) : (1@tid_in_wg, 1)]` 字样。第 4 步的打印输出**待本地验证**（取决于本地 TVM 版本的打印格式）。

#### 4.2.5 小练习与答案

**练习 1**：`tmem` 声明为 512 列，但 MMA 只写 `tmem[:, :BLK_N]`（前 128 列）。为什么分配 512 列而不是恰好 128 列？

**参考答案**：u7-l3 讲过 TMEM 的分配规则——`n_cols` 只有 32/64/128/256/512 五档，且同一 CTA 的多次分配必须单调不增，因此惯用做法是起步就申请最大档（512 列）再按列切片，后续内核（如 FA4 要在同一块 TMEM 上重叠放置 S/P/O 多个区域）才有腾挪空间。这属于「分配策略」，与单次 MMA 需要的 128 列是两回事。

**练习 2**：如果把 `A_layout` 的 swizzle 模式从 `SWIZZLE_128B_ATOM` 换成别的模式、而 MMA 侧的读取约定不变，会发生什么？

**参考答案**：字节一个不少地到达 SMEM，但元素被放在了与描述符解码不一致的位置上——MMA 会「认错元素」，把错位的 A 当作正确排布来乘，结果数值错误。这正是 layout 铁律（读写两端必须一致）被破坏的典型现场，也是 u5-l2「描述符必须与 SMEM 实际字节一致」的反面案例。

**练习 3**：`S[(128, BLK_N) : (1@tid_in_wg, 1)]` 里第二个 stride 是裸的 `1`，没有 `@轴名`。它落在哪根轴上？物理含义是什么？

**参考答案**：按 u4-l2 的规则，未标注的 stride 落在默认线性轴 `m` 上。物理含义是：行内第 \(c\) 列对应线程本地数组的第 \(c\) 个连续槽位（寄存器/本地内存中的连续位置）。

### 4.3 模块三：Dispatch——走哪条硬件路径

#### 4.3.1 概念说明

dispatch 回答的是：当一项 tile 操作**存在多种硬件实现**时，选哪一条。正文的说法是「dispatch 决定 tile 操作使用哪种硬件实现」，并给了两个正反例（[chapter_intro_tirx/index.md:226](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L226)，中文版 [zh/chapter_intro_tirx/index.md:226](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/zh/chapter_intro_tirx/index.md#L226)）：

- `Tx.gemm_async` 本身只表示「一个异步 tile GEMM」，`dispatch="tcgen05"` 进一步**点名**要走 Blackwell 的 `tcgen05.mma` 路径；
- 本版内核里 GMEM→SMEM 的拷贝由**普通线程**完成（没有 dispatch 参数可填）；「后面的版本会将同一类 tile copy 改为 TMA」——这句话就是本讲综合实践那道预测题的出处。

dispatch 还有一条**合法性约束**，写在演示解说词里（[_extra/demo/tirx_dispatch.html:103](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/tirx_dispatch.html#L103)）：一个 dispatch 只有当该实现**支持这个操作的 scope 和操作数布局**时才有效；把 copy 改成 TMA 同时要求「单线程发起者」和「与 TMA 兼容的布局」。也就是说，三要素不是三个独立旋钮——dispatch 的可选值受 scope 和 layout 制约。

#### 4.3.2 核心流程

把 `hgemm_v1` 的派发决策列成表：

| tile 操作 | 可选路径 | 本版选择 | 依据/约束 |
| --- | --- | --- | --- |
| `Tx.gemm_async`（L145-148） | Tensor Core 各代路径 | `dispatch="tcgen05"` | A/B 走 SMEM 描述符、D 落 TMEM（u7-l1）；`cta_group=1` 单 CTA |
| `Tx.cta.copy`（L138-139） | 线程级 copy 循环；（后续章节）TMA | 线程级 | 本版未指定 dispatch，由编译器取默认实现 |
| `Tx.wg.copy_async`（L158） | TMEM→RF 通路 | `tcgen05.ld` 数据通路 | warp 集体读回（u7-l4） |
| `Tx.cast` / `Tx.copy`（L160/162） | 线程级计算/访存 | 线程级 | 每线程处理自己那行数据 |

与 `dispatch` 并排还有两个「邻居参数」值得认识，它们与 dispatch 一起描述这条 MMA：

- `accum=False`：tile 级语义是「整块 GEMM 从零开始」。编译器展开出的多条 `tcgen05.mma` 内部仍是首条 `accum=0`、后续 `accum=1`（u7-l1），但那是**指令级**的接力，tile 级只需声明一次。
- `cta_group=1`：MMA 的协作范围限定单 CTA。若改为 `::2`，scope 就升级成「CTA 对」（u7-l2），这直接说明 **dispatch 参数会反过来扩大 scope 要求**。

一条 `Tx.gemm_async` 如何被展开，正文 L59 给出了量化说明：该操作描述完整的 \(128\times128\times64\) tile GEMM，而 `tcgen05.mma` 每条沿 K 前进 16 个元素，故编译器生成 \(\lceil 64/16 \rceil = 4\) 条 MMA 指令——「具体指令序列由编译器根据 shape、layout 和 dispatch 决定」。

#### 4.3.3 源码精读

**(1) 唯一显式 dispatch 参数。**

[chapter_intro_tirx/index.md:145-148](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L145-L148)

```python
                Tx.gemm_async(
                    tmem[:, :BLK_N], Asmem[:, :], Bsmem[:, :],
                    accum=False, dispatch="tcgen05", cta_group=1
                )
```

这段代码做的事情：声明一次完整的 tile GEMM——目的操作数是 TMEM 切片，源操作数是两个 SMEM buffer，`dispatch="tcgen05"` 点名 Blackwell Tensor Core 路径，`accum=False` 表示不叠加旧值，`cta_group=1` 限定单 CTA 协作。整个内核里**显式的 dispatch 参数只有这一处**；演示中全内核被标成 `"dispatch"` 的也只有 `dispatch="tcgen05"` 那一行（[_extra/demo/tirx_dispatch.html:91](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/tirx_dispatch.html#L91)）。

**(2) dispatch 的存在前提：多路径。**

[chapter_intro_tirx/index.md:38](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L38) —— 定义「Dispatch: which hardware implementation executes a tile operation」。演示解说词（[L103](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/tirx_dispatch.html#L103)）补充了关键语境：**当存在不止一种 lowering 时**才需要 dispatch 来选择——`dispatch="tcgen05"` 选中 Blackwell Tensor Core，于是 tile MMA lowering 成写 TMEM 的 `tcgen05.mma`。

**(3) 「后续改 TMA」的伏笔。**

[chapter_intro_tirx/index.md:226](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L226) —— 正文最后一句明确说：这个版本中的 GMEM 到 SMEM copy 由普通 threads 完成；后面的版本会将同一类 tile copy 改为 TMA。同一段还说明 dispatch 的合法性约束。u12-l1（GEMM Step 4）将兑现这句话。

#### 4.3.4 代码实践

**实践目标**：在不写新内核的前提下，预测「把 GMEM→SMEM 拷贝改为 TMA 派发」需要动哪几类代码位置，形成一份待验证的改造清单。

**操作步骤**：

1. 精读演示解说词 [_extra/demo/tirx_dispatch.html:103](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/tirx_dispatch.html#L103)，提取它给出的两条硬性前提（单线程发起者；与 TMA 兼容的布局）。
2. 对照内核源码，逐项写下改动位置：
   - **scope 侧**（L137-140 附近）：`Tx.cta.copy` 由 128 线程协作改为单线程发起——需要仿照 L143-144 用 `if warp_id == 0:` + `elect_sync()` 包住新的拷贝调用；
   - **layout 侧**（L92-93 / L114-115）：SMEM 布局须能被 TMA tensor map 描述。好消息是 `SWIZZLE_128B_ATOM` 布局本来就是 TMA 支持的写入 swizzle 模式（u6-l1 讲过 TMA 写入时顺带 swizzle），且 `BLK_K=64` 个 fp16 恰为 128B，未超 box 宽度上限；
   - **同步侧**（L140）：`cta_sync()` 观察不到 TMA 引擎（u6-l3），须换成 mbarrier 的 `expect_tx`/`try_wait` 协议。登记字节数按 u8-l1 的公式计算：\( (128\times64 + 128\times64)\times 2 = 32768 \) 字节。
3. 把这份清单保存下来，学到 u12-l1（Step 4）时逐条核对。

**需要观察的现象**：本步骤是纯源码推演，无运行现象；核对点在 Step 4 的真实代码里——它的拷贝调用是否被单线程守卫包住、是否出现 `expect_tx`、字节数是否与你的计算一致。

**预期结果**：得到「scope / layout / 同步」三类共 3–4 处改动位置的预测清单。预测是否与 Step 4 实际实现完全一致（例如 TMA 版 tile 拷贝的具体 API 名称）**待 Step 4 验证**，本讲不预先编造。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `Tx.gemm_async` 有 `dispatch=` 参数，而 `Tx.cta.copy` 没有？

**参考答案**：dispatch 只在「存在多种 lowering」时才有意义。本版内核里 GMEM→SMEM 拷贝只有线程级循环这一条自然实现，无事可选择；而 tile GEMM 存在多条 Tensor Core 路径，必须点名。一旦后续章节给拷贝增加了 TMA 路径，这类拷贝操作也就有了「选择哪条路」的问题。

**练习 2**：保持 M=128、grid 仍是 1×1，只把 `cta_group=1` 改成 `cta_group=2`，哪里先出问题？

**参考答案**：scope 先出问题。`cta_group::2` 要求 cluster 内存在一对最低位 rank 不同的 CTA 协作（u7-l2），而 1×1 的 grid 里根本没有对端 CTA；同时累加器的 TMEM 映射也会换成双 CTA 的 Layout 规则，`tmem[:, :BLK_N]` 的切片解释随之失效。这说明了三要素的耦合：改 dispatch 参数可能同时抬高 scope 与 layout 要求。编译器具体报什么错**待本地验证**。

**练习 3**：根据演示解说词，一个 dispatch 有效的前提是什么？由此说明为什么「把 copy 改成 TMA」不能只改一个参数。

**参考答案**：前提是所选实现必须支持该操作的 scope 和操作数布局。TMA 的发起模型是单线程提交、引擎搬运，与「全 CTA 协作」的 scope 不匹配；tensor map 对全局张量形状/步长与 SMEM swizzle 也有自己的描述方式。所以改造必须同时调整守卫（scope）、布局描述（layout）和完成机制（同步），单换一个调用名是不够的。

### 4.4 模块四：三要素合力——编译器如何推出具体实现

#### 4.4.1 概念说明

前面三个模块分别看了三根「旋钮」，本模块回答学习目标的第三条：**编译器拿到三要素后做了什么**。

结论一句话（正文原句）：「编译器会结合 scope、layout 和 dispatch，生成具体的 thread-level 控制流、地址计算和硬件指令」——[chapter_intro_tirx/index.md:228](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L228)（中文版 [zh/chapter_intro_tirx/index.md:228](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/zh/chapter_intro_tirx/index.md#L228)）。对照本讲开头的框架表：

- **scope → 控制流**：编译器据此生成守卫谓词（单线程发起）或每线程分工循环（CTA 协作）；
- **layout → 地址计算**：命名轴布局被翻译成具体的地址算式（SMEM 线性地址、TMEM 的 lane/列编码、寄存器槽位）；
- **dispatch → 指令选择**：选定实现后，操作被展开成对应的 intrinsic 序列（如 `tcgen05.mma`）。

反过来看也成立：这正是 L32 说 CUDA「把决定分散掉」的那三个藏身处——代码约定对应控制流、地址计算对应布局、intrinsic 参数对应指令选择。TIRx 没有引入新硬件能力，它做的是把这三样从「约定俗成」提升为「IR 里可检查、可变换的一等公民」（[chapter_intro_tirx/index.md:40](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L40)）。

#### 4.4.2 核心流程

以 `Tx.gemm_async` 为例走一遍 lowering 的推导链（在 u9-l2 介绍的 `tirx` 流水线里，这项工作由首个核心 pass `LowerTIRx` 完成）：

1. **读 dispatch**：`"tcgen05"` → 目标指令族确定为 `tcgen05.mma`，操作数来源确定为 SMEM 描述符 + TMEM 累加器；
2. **读 shape**：\(K=64\)、每条指令推进 16 → 展开成 4 条指令的接力序列，首条清零（对应 tile 级 `accum=False`）、后续累加；
3. **读 layout**：A/B 的 swizzle 布局翻译成矩阵描述符的编码字段；D 的 `TLane/TCol` 布局翻译成累加器地址；
4. **读 scope**：双层守卫保证只有 1 个线程执行这串指令；commit/try_wait 的位置（守卫内/外）随 scope 定型；
5. 后续 pass 再做缓冲展平、host/device 拆分与 CUDA 代码生成（u9-l2 已走完全流程）。

正文对第 1-2 步有一句精确的量化描述，见下面的源码精读。

#### 4.4.3 源码精读

**(1) 一条 tile 操作 = 一串指令序列。**

[chapter_intro_tirx/index.md:59](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L59)（中文版 [zh/chapter_intro_tirx/index.md:59](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/zh/chapter_intro_tirx/index.md#L59)）—— 「矩阵乘法写成一次 `Tx.gemm_async`……`tcgen05.mma` 每次处理 16 个 K 元素，因此编译器会沿 K 维生成 4 次 MMA。具体指令序列由编译器根据 shape、layout 和 dispatch 决定。」这是「三要素 → 具体实现」最直接的证据。

**(2) LowerTIRx 的职责表述。**

[chapter_intro_tirx/index.md:239](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L239)（中文版 [zh/chapter_intro_tirx/index.md:239](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/zh/chapter_intro_tirx/index.md#L239)）—— 「核心 pass `LowerTIRx` 根据每项 tile primitive 的 scope、layout 和 dispatch 选择具体实现，将 `Tx.gemm_async`、`Tx.cta.copy` 等高层 tile 操作展开成更底层的 TIR」。u9-l2 已从流水线角度看过它在 19 个 pass 里的位置，本讲补上了「它凭什么能展开」的答案：凭三要素。

**(3) 后续 pass 与两级检视。**

[chapter_intro_tirx/index.md:241](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L241) —— 后续 pass 完成缓冲展平、host/device 拆分与设备代码生成。[L245-252](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L245-L252) —— 用 `kernel.show()`/`kernel.script()` 看 lowering 前的 tile 级 IR，用 `ex.mod.imports[0].inspect_source()` 看最终 CUDA 源码；「对照这两层代码，可以看到一个 tile 操作最终生成了哪些底层指令，也可以检查 layout 和 thread scope 如何变成具体的地址计算与控制流」——正文把两级检视的**观察目标**直接对准了三要素。

**(4) 演示的点题句。**

[_extra/demo/tirx_dispatch.html:104](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/tirx_dispatch.html#L104) —— 初始解说词：「这个单 MMA GEMM 的每一行，都是沿 TIRx 三个设计要素之一做的一次选择」。这句可以作为你以后读任何 TIRx 内核的口头禅：**每读一行，问它属于哪个要素**。

#### 4.4.4 代码实践

**实践目标**：在两级代码（lowering 前的 IR 与生成的 CUDA）中分别找到三要素的踪迹，验证「scope→控制流、layout→地址计算、dispatch→指令选择」的对应关系。

**操作步骤**：

1. 无 GPU 环境：运行 4.2.4 的**示例代码**打印 `kernel.script()`，在输出中逐个找出 6 个 tile 操作调用，给每条标注三要素（这就是综合实践表格的数据来源）。
2. 有 Blackwell GPU 环境：按 u9-l2 的流程编译（`tvm.compile(..., tir_pipeline="tirx")`），再打印 `ex.mod.imports[0].inspect_source()`。
3. 在生成的 CUDA 源码中做三个检索：
   - 搜索 `tcgen05.mma`——统计出现次数，验证是否为 4 条（对应 L59 的说法）；
   - 搜索 `elect`（或被选线程的守卫模式）——确认 MMA 发起代码被单线程守卫包住；
   - 观察 `Tx.cta.copy` 生成的访存循环——确认它由全体线程分工执行。

**需要观察的现象**：第 1 步的 IR 中 tile 操作保持「整块」形态（看不到 4 条 MMA）；第 2 步的 CUDA 中控制流、地址算式与指令全部落地。

**预期结果**：得到「同一操作在两级代码中的形态对照」笔记：`Tx.gemm_async` 一行 → （守卫内的）4 条 `tcgen05.mma` + commit。第 2、3 步需要 Blackwell GPU，**待本地验证**；无 GPU 读者完成第 1 步即可。

#### 4.4.5 小练习与答案

**练习 1**：内核算出了错误数值但正常运行。按三要素框架，第一嫌疑是哪个要素？为什么？

**参考答案**：优先怀疑 layout。数值错而程序不死，通常是「字节都搬对了、元素被放错/读错位置」——读写两端布局不一致（swizzle 模式不符、TMEM 切片错位）都会精确地产生这种症状。scope 错通常表现为部分线程没干活或集体操作不完整，dispatch 错通常是压根选错了指令族。

**练习 2**：内核挂死（不返回）。哪个要素最可疑？给出一个具体成因。

**参考答案**：scope 与同步的错配最可疑。例如把某个集体操作（`Tx.wg.copy_async`）放进了单线程守卫：其余 127 个线程先到达下一道 `cta_sync`，而执行集体操作的线程永远凑不齐参与者，形成互等死锁。u8-l2 讲过的「漏翻相位导致循环等待」同属此类；系统性的排查方法将在 u15-l7 的调试附录展开。

**练习 3**：用一句话向同事解释：TIRx 相比「直接写 CUDA/PTX」到底多给了什么？

**参考答案**：硬件能力一件没多——它把 CUDA 里散落在 intrinsic 参数、地址计算和代码约定中的三个决定（谁执行、数据摆哪、走哪条路）提升为结构化 IR 中显式、可检查、可整体变换的信息，编译器据此自动生成原本要手写的控制流与地址计算。

## 5. 综合实践

**任务**：完成本讲的两张「毕业卷」——三要素对照表与 TMA 改造预测。这是本讲规格中指定的实践任务，也是后续 GEMM 章节的随身参考卡。

### 实践一：hgemm_v1 全部 tile 操作的三要素对照表

逐行读完 [chapter_intro_tirx/index.md:137-162](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L137-L162) 后，自行填表，再对照下面的参考答案。参考答案（行号均指正文清单）：

| tile 操作（行号） | scope：谁执行 | 涉及的 layout | dispatch：硬件路径 |
| --- | --- | --- | --- |
| `Tx.cta.copy(Asmem, A)`（[L138](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L138)） | CTA 内全部 128 线程协作（`cta` 前缀） | 源：GMEM 行主序 `A`；目的：`Asmem`（`A_layout`，128B swizzle 原子） | 线程级 copy 循环（本版默认实现） |
| `Tx.cta.copy(Bsmem, B)`（[L139](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L139)） | 同上 | 同上，B 版（`B_layout`） | 同上 |
| `Tx.gemm_async`（[L145-148](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L145-L148)） | warp 0 内 `elect_sync` 选出的 **1 个线程**发起；Tensor Core 执行 | A/B：SMEM swizzle 布局 → 矩阵描述符；D：`tmem[:, :BLK_N]`（`S[(128,512):(1@TLane,1@TCol)]` 切片） | **`dispatch="tcgen05"`**（`cta_group=1`，`accum=False`） |
| `Tx.wg.copy_async(Dreg_wg, tmem)`（[L158](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L158)） | 整个 warpgroup 128 线程集体（`wg` 前缀，无守卫） | 源：TMEM `TLane/TCol`；目的：`Dreg_wg`（`S[(128,BLK_N):(1@tid_in_wg,1)]`） | TMEM→RF 通路（`tcgen05.ld` 语义，u7-l4） |
| `Tx.cast(Dreg_f16, Dreg)`（[L160](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L160)） | 每线程处理自己的 128 个值（线程级） | 两端均为线程本地数组，无跨线程布局 | 线程级计算循环 |
| `Tx.copy(D[m_thr, ...], Dreg_f16)`（[L162](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L162)） | 每线程写 D 的一行（行号由 L161 的 `warp_id*32+lane_id` 即 `tid_in_wg` 决定） | 源：本地数组；目的：GMEM 行主序 `D` | 线程级 store |

说明：前四行的依据是正文 L222-226 的官方讨论；`Tx.cast`/`Tx.copy` 两行的 scope 与 dispatch 是依据 `Dreg_wg` 布局与 L161 行号算式做出的**推演**，正文未逐条讨论，建议用 4.4.4 的 `kernel.script()` 检视确认——**待本地验证**。

### 实践二：把 GMEM→SMEM 拷贝改为 TMA 派发，要动哪里？

这是正文 L226 埋下的伏笔（「后面的版本会将同一类 tile copy 改为 TMA」）。参考答案按三类改动组织：

1. **scope 侧**：拷贝的执行者从「全 CTA 128 线程协作」改为「单个被选线程发起」——需要仿照 [L143-144](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L143-L144) 的双层守卫包裹新的 TMA 拷贝调用（依据：u6-l1 的单线程发起模型 + 演示 L103 的「单线程发起者」前提）。
2. **layout 侧**：SMEM 侧布局须可用 TMA tensor map 描述。`SWIZZLE_128B_ATOM` 本就是 TMA 支持的写入 swizzle 模式，`BLK_K=64` 个 fp16 恰为 128B、未超 box 最内维上限（u6-l1/u6-l2），因此布局大体可复用，但 tensor map 需要按全局张量 `A`/`B` 的形状与字节步长重新登记。
3. **同步侧**：`cta_sync()`（[L140](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L140)）观察不到 TMA 引擎，须换成 mbarrier 协议——发起后 `arrive.expect_tx` 登记在途字节，消费者 `try_wait` 等账清零（u6-l3/u8-l1）；双 tile 的登记字节数为 \( (128\times64 + 128\times64)\times2 = 32768 \) 字节。
4. **操作本身**：`Tx.cta.copy` 换成走 TMA 的 tile 拷贝操作（具体 API 名称以 Step 4 章节源码为准，此处不预设）。

**验证方式**：学到 u12-l1（GEMM Step 4，`chapter_gemm_async`）时逐条核对这份清单；也可以现在就打开该章浏览 TMA 版内核，但注意那里同时引入了 K 循环等新内容，需剥离出「拷贝派发」这一条线来对照。

## 6. 本讲小结

- **每个 tile 操作 = scope + layout + dispatch**：谁执行、数据摆哪、走哪条硬件路径。CUDA 把这三个决定分散在代码约定、地址计算和 intrinsic 参数里，TIRx 把它们显式写进结构化 IR（正文 L32-38）。
- **scope 看两处**：操作名前缀（`Tx.cta.` / `Tx.wg.`）与 `if` 守卫（`warp_id == 0` + `elect_sync`）。同时要区分发起者与执行者——MMA 由 1 个线程发起、Tensor Core 执行；取得线程 ID 本身不是 scope。
- **layout 是逻辑坐标到物理坐标的函数**：本讲三族实例——SMEM 的 128B swizzle、TMEM 的 `TLane/TCol`、寄存器的 `tid_in_wg`；读写两端对同一元素必须给出同一位置，检验工具是元素数守恒。
- **dispatch 只在多路径时出现**：本版唯一显式参数是 `dispatch="tcgen05"`；其合法性受 scope 与 layout 制约（演示 L103），正文明言后续会把拷贝改为 TMA 派发。
- **编译器按三要素落地**：scope→线程级控制流、layout→地址计算、dispatch→指令选择；`LowerTIRx` 据此把一条 `Tx.gemm_async` 展开为 4 条 `tcgen05.mma`（K=64、每条推进 16）。
- **读任何 TIRx 内核的口头禅**：每读一行，问它属于哪个要素（演示 L104）。

## 7. 下一步学习建议

本讲完成了单元九，TIRx 编程模型的「骨架」已经立起来。接下来三条路，按建议优先级排列：

1. **u10 单元（TIRx Layout API）**：本讲只把 layout 当「标注」用，u10 把它展开成一套完整 API——`TileLayout` 的 `S[...]`/`R[...]` 构造、`apply()` 前向映射、`ComposeLayout` 与 swizzle 变换。三要素中 layout 的信息量最大，值得单独一个单元。
2. **u11 单元（GEMM 基础）**：从 `hgemm_v1` 出发加入 K 循环与空间分块（Step 1-3）。读这些内核时随身带上本讲的对照表，每见一个新 `Tx.*` 调用先标三要素，你会发现新机制（如 barrier 相位翻转）都能挂到已有框架上。
3. **u12-l1（Step 4：TMA）**：拿你的「TMA 改造预测清单」去核对真实实现，检验本讲的预测框架；差异处就是你需要补的认知缺口。

语言细节（全部 tile 操作清单、线程同步原语、控制流语法）见 `tirx_guide/` 语言参考，将在 u15-l1/u15-l2 系统过一遍；对三要素与流水线机制的综合运用，留到 u16-l2 的 capstone 实战。
