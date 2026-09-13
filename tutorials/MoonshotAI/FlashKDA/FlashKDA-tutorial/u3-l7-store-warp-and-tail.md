# STORE warp 与尾块处理：整块 TMA 与逐元素回退

## 1. 本讲目标

本讲是 Kernel 2（`_flash_kda_fwd_recurrence`）warp 专用化三部曲的最后一讲：在 u3-l3 读完 LOAD warp、u3-l4/u3-l5 读完 MMA warp 之后，我们把目光转向最后一个角色——STORE warp，以及整个递推 kernel 中最容易写错正确性的分支：**尾块（tail tile）处理**。

学完本讲你应当能够：

1. 逐行讲出 STORE warp 主循环的执行流程：`consumer_wait` → 计算 `actual_len` → 双分支（整块 TMA / 逐元素手动写）→ `tma_store_wait<0>` → `consumer_release`。
2. 解释**为什么尾块必须绕过 TMA**：TMA 的搬运盒固定为 16 行，varlen 模式下序列在时间轴上紧凑排布，整块写会把下一条序列的前几行输出覆盖掉，造成跨 CTA 竞态。
3. 跟踪 `store_pipeline` 的完整生命周期：MMA warp（128 线程生产者）的 `producer_acquire`/`producer_commit` 与 STORE warp（单线程消费者）的 `consumer_wait`/`consumer_release` 如何围绕 2 个 out stage 交替翻转。
4. 说清 `tma_store_wait<0>()` 在 TMA 路径上是**正确性必需**、在手动写路径上是**无害的空转**，以及背后 cp.async.bulk「组提交/组等待」的语义。

## 2. 前置知识

本讲假设你已读过 u3-l2（Kernel 2 架构总览）与 u3-l3（LOAD warp）。下面把四个容易混淆的底层概念先用大白话过一遍。

### 2.1 TMA store：一次搬「一整盒」

TMA（Tensor Memory Accelerator）是 SM90 引入的异步批量搬运引擎。一次 TMA store 把 smem 里一个固定形状的「盒」（box）写到 gmem——盒的形状在 host 侧创建描述符时就固定死了。本项目中 out 的描述符是：

[csrc/smxx/fwd_launch.cu:50-58](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L50-L58) 定义了 out 的 gmem 视图 `(H, T_total, D) : (D, D·H, 1)`，随后 [csrc/smxx/fwd_launch.cu:116](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L116) 用它造出 `tma_store_out` 描述符：

```cpp
auto tma_store_out = make_tma_copy(SM90_TMA_STORE{}, m_out, TMAVOLayout{});
```

smem 侧布局 `TMAVOLayout` 是 `16×128`（CHUNK×D）的 swizzle 布局，所以**一次 TMA store 恒定写 16 行 × 128 列**。它没有「只写前 k 行」的旋钮——这正是尾块问题的根源。

### 2.2 cp.async.bulk 的 commit / wait 组语义

TMA store 是异步的：`cute::copy(tma_store_out, ...)` 发出后指令立即返回，硬件在后台从 smem 读、向 gmem 写。完成情况用「组」跟踪：

- `tma_store_arrive()`：把自上次 commit 以来发出的 bulk store 编成一组（PTX 层面是 `cp.async.bulk.commit_group`）；
- `tma_store_wait<0>()`：等到「在途组数 ≤ 0」（PTX 层面是 `cp.async.bulk.wait_group.read 0`，具体指令可在 CUTLASS 的 `include/cute/arch/copy_sm90_tma.hpp` 中核对）。

注意 `.read` 变体的语义：只需等到**smem 源不再被读取**，不必等到 gmem 写对所有观察者可见。这恰好是「释放 smem stage」所需的最低保证——我们只需要知道 TMA 不会再碰这块 smem，就允许 MMA warp 覆写它。

对比 u3-l3 讲过的 LOAD 侧：TMA load 的完成通知必须走 mbarrier（事务字节记账），因为消费者要等「数据到了」才能算；而 TMA store 的源是 smem、目的是 gmem，STORE warp 自己就是最后一个使用者，用组等待即可，不需要 barrier。

### 2.3 PipelineAsync：纯软件信号量

`cutlass::PipelineAsync` 是 CUTLASS 里最简单的双缓冲流水线：**没有事务字节机制**，full / empty 两个 barrier 全靠「到达计数」翻转。本讲涉及两个参数：

- `producer_arv_count = 128`：4 个 MMA warp 共 128 线程，每线程 `producer_commit` 时 arrive 一次，攒满 128 次 full barrier 翻转；
- `consumer_arv_count = 1`：STORE warp 只有 leader 一个线程参与（`lane_predicate`，见 4.1.3），empty barrier 由它单独 arrive 翻转。

参数装配在 [csrc/smxx/utils.cuh:118-143](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/utils.cuh#L118-L143) 的 `make_store_pipeline` 中：MMA 角色映射为 Producer、STORE 角色映射为 Consumer，其余线程是 NonParticipant。

### 2.4 generic 代理与 async 代理（回顾）

SM90 的内存一致性模型把访问分成两套代理（u3-l3、u3-l6 反复出现）：

- **generic 代理**：普通的 `ld/st`，包括读 smem 的 `s_out(row, col)`、写 gmem 的 `out_raw_ptr[...]`、MMA warp 的 `stmatrix`；
- **async 代理**：TMA 引擎的读写。

跨代理的可见性不自动成立：generic 写 → TMA 读之间必须插 `fence_view_async_shared()`。这个判据在 4.3 里会直接决定「为什么手动写分支不需要 fence、TMA 分支前面必须有」。

## 3. 本讲源码地图

| 文件 | 本讲关注点 | 作用 |
| --- | --- | --- |
| `csrc/smxx/fwd_kernel2.cuh` | L745-L838（STORE warp 全部）、L434-L442 与 L733-L741（MMA 侧对 store_pipeline 的调用） | 本讲主战场：store 循环、双分支、尾块手动写、流水线回收、状态输出收尾 |
| `csrc/smxx/utils.cuh` | L79-L84（WarpRole）、L118-L143（make_store_pipeline） | warp 角色枚举与存储流水线的构造参数 |
| `csrc/smxx/fwd_launch.cu` | L29-L31（stages 常量）、L116（tma_store_out）、L183-L214（K2 启动与 `out_ptr` 传参） | host 侧如何把 out 描述符和裸指针 `out_raw_ptr` 一起传进 kernel |
| `tests/test_fwd.py` | L265-L312（test_fwd_varlen） | 综合实践的仿写模板（exact match 对拍） |
| `benchmarks/ncu.sh` | 全文 | ncu 抓取 recurrence kernel 的正则与参数模板 |

一句话串联：`fwd_launch.cu` 把「TMA 描述符 + 裸指针」两套 out 寻址手段同时传给 kernel；MMA warp 把算好的 tile 写进 `output[stage]`；STORE warp 等流水线信号，然后按 tile 是否为尾块选一条路写回 gmem。

## 4. 核心概念与源码讲解

### 4.1 store 循环与双分支

#### 4.1.1 概念说明

回忆 u3-l2 的角色划分：K2 的 192 线程 = 4 个 MMA warp + 1 个 LOAD warp + 1 个 STORE warp。STORE warp 是**输出流水线**（`PipelineAsync`，深度 `kOutputStages = 2`，见 [csrc/smxx/fwd_launch.cu:29-30](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L29-L30)）的唯一消费者。它的全部工作是：

> 对本 CTA 负责的序列（`seq_idx`）与头（`head_idx`），沿 tile 序列 `t = 0..t_tiles-1`，把 MMA warp 产出的 `output[stage].out`（16×128 bf16）写到 gmem 的 `out` 张量。

麻烦在于 `out` 张量是所有序列**紧凑共享**的：token 轴上序列 A 的结尾紧跟序列 A+1 的开头（`cu_seqlens` 划分）。而 KDA 每步计算以 16 个 token 为一个 chunk，序列长度不一定是 16 的倍数——于是**最后一个 tile 可能只有不足 16 行真实数据**。TMA 盒却是固定 16 行的（2.1 节）。两者冲突，就产生了本讲的双分支。

#### 4.1.2 核心流程

单个 CTA 内 STORE warp（实际只有 leader 线程在跑，见 4.1.3）的主循环：

```text
初始化: g_out ← TMA 视角的 out 张量; out_read ← 消费者流水线状态(stage 0)
for t in 0 .. t_tiles-1:
    consumer_wait(out_read)                 # 等 MMA warp 填满本 stage
    stage    ← out_read.index()
    actual_len ← min(16, seq_len - 16·t)    # 本 tile 有效行数

    if actual_len < 16:                     # 尾块分支
        单线程逐元素写: 只写前 actual_len 行 × D 列
    else:                                   # 整块分支
        发 TMA store(16×128 盒); tma_store_arrive()

    tma_store_wait<0>()                     # 确认 smem 源不再被读
    consumer_release(out_read)              # 释放 stage 给 MMA warp 复用
    ++out_read                              # 推进到下一 stage
```

几个量之间的关系（\( \lceil \cdot \rceil \) 为上取整）：

\[ t\_tiles = \left\lceil \frac{seq\_len}{16} \right\rceil, \qquad actual\_len = seq\_len - 16(t\_tiles - 1) \;\; (\text{最后一个 tile}) \]

尾块只在 \( seq\_len \bmod 16 \neq 0 \) 时出现，此时越界行数为 \( 16 - actual\_len \)。

#### 4.1.3 源码精读

先看入口与循环骨架。整个 STORE warp 逻辑被 `lane_predicate` 包住——这是 [csrc/smxx/fwd_kernel2.cuh:237](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L237) 定义的 `cute::elect_one_sync()` 选出的 leader lane，即 **32 个线程里只有 1 个真正执行**（与 u3-l3 讲过的 LOAD warp「单 lane 发 TMA」同一手法）：

[csrc/smxx/fwd_kernel2.cuh:745-755](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L745-L755) —— STORE warp 入口：构造 TMA 视图、初始化消费者状态，进入 tile 循环并等待流水线：

```cpp
if (warp_role == WarpRole::STORE && lane_predicate) {
    Tensor g_out = tma_store_out.get_tma_tensor(make_shape(H, T_total, D));
    auto cta_tma_store = tma_store_out.get_slice(Int<0>{});
    StorePipelineState out_read;
    for (int t = 0; t < t_tiles; ++t) {
        store_pipeline.consumer_wait(out_read);
        int stage = out_read.index();
        int actual_len = min(CHUNK, seq_len - t * CHUNK);
        BF16* out_stage_ptr = shared_storage.output[stage].out.begin();
```

这段做了四件事：拿到 gmem 的 TMA 张量视图 `g_out`；`out_read` 是消费者流水线状态（默认从 stage 0、相位 0 起步，而生产者侧用 `make_producer_start_state` 起步，见 [csrc/smxx/fwd_kernel2.cuh:430](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L430)）；`consumer_wait` 阻塞到 MMA warp 的 128 线程全部 `producer_commit`；`actual_len` 算出本 tile 有几行真实输出。

[csrc/smxx/fwd_kernel2.cuh:767-779](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L767-L779) —— **整块分支**：非尾块时发一次 TMA store 写完整 16 行：

```cpp
} else {
    // TMA store for full tiles
    auto out_off = g_out.layout()(head_idx, int(bos) + t * CHUNK, 0);
    Tensor g_out_tile = make_tensor(g_out.data() + out_off,
        make_layout(make_shape(Int<1>{}, Int<CHUNK>{}, Int<D>{}), stride(g_out.layout())));
    Tensor s_out_tile = make_tensor(make_smem_ptr(out_stage_ptr), TMAVOLayout{});
    cute::copy(tma_store_out,
        cta_tma_store.partition_S(s_out_tile),
        cta_tma_store.partition_D(g_out_tile)
    );
    tma_store_arrive();
}
```

要点有三：

1. **寻址**：`out_off = g_out.layout()(head_idx, bos + 16t, 0)` 按 `(H, T_total, D):(D, D·H, 1)` 布局算出 tile 左上角偏移，即 \( head\_idx \cdot D + (bos + 16t) \cdot D \cdot H \)。注意 smem 源用 `TMAVOLayout`（swizzle 布局），它与 host 侧描述符里编码的盒形状逐比特匹配——这是 u2-l4「位一致契约」在输出侧的体现。
2. **无 barrier 的 copy**：`cute::copy(tma_store_out, ...)` 不带 mbarrier 参数（对比 LOAD 侧的 `tma_load_v.with(*tma_barrier)`），store 的完成用组机制跟踪（2.2 节）。
3. `tma_store_arrive()` 把这次 bulk store 编组，供后面的 `tma_store_wait<0>` 等待。

[csrc/smxx/fwd_kernel2.cuh:781-784](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L781-L784) —— **两分支共用的收尾**：等待、释放、推进：

```cpp
        tma_store_wait<0>();
        store_pipeline.consumer_release(out_read);
        ++out_read;
    }
```

这三行是流水线回收的关键，4.3 节展开。

最后交代 STORE warp 的「片尾工作」：主循环结束后，若 `HasStateOut && !StateFP32`，leader lane 直接从常驻的 `state_acc` 发一次 TMA store 写出最终状态（[csrc/smxx/fwd_kernel2.cuh:786-801](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L786-L801)）；fp32 状态路径则由全块同步转换后再由 STORE warp 发 TMA（[csrc/smxx/fwd_kernel2.cuh:804-835](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L804-L835)），细节已在 u3-l6 讲过，本讲不再重复。

#### 4.1.4 代码实践

**实践目标**：把「序列长度 → tile 划分 → 尾块属性」的算术彻底变成肌肉记忆，并验证它与 kernel 侧公式一致。

**操作步骤**：保存以下脚本为 `tests/tile_census.py` 并运行 `python tests/tile_census.py`（纯 CPU 脚本，不需要 GPU）：

```python
# 示例代码：独立小脚本，与仓库无依赖
CHUNK = 16

def census(seq_lens):
    total_tiles = 0
    print(f"{'seq':>6} {'len':>6} {'t_tiles':>8} {'tail_actual':>12} {'manual_elems':>13}")
    for i, L in enumerate(seq_lens):
        t_tiles = (L + CHUNK - 1) // CHUNK
        tail = L - CHUNK * (t_tiles - 1) if t_tiles > 0 else 0
        manual = tail * 128 if tail < CHUNK else 0
        total_tiles += t_tiles
        print(f"{i:>6} {L:>6} {t_tiles:>8} {tail:>12} {manual:>13}")
    return total_tiles

for name, seq_lens in [("spec", [1300, 547, 16]),
                       ("bench_varlen", [1300, 547, 2048, 963, 271, 3063]),
                       ("bench_fixed", [8192])]:
    print(f"\n{name}: {seq_lens}")
    n = census(seq_lens)
    # 对照 C++ 上界公式（u2-l2）: ceil(T_total/CHUNK) + N
    T_total, N = sum(seq_lens), len(seq_lens)
    ub = (T_total + CHUNK - 1) // CHUNK + N
    print(f"  exact tiles = {n}, alloc upper bound = {ub}, ok = {n <= ub}")
```

**需要观察的现象**：每条非 16 倍数序列都产生恰好一个尾块；尾块的手写元素数 = `actual_len × 128`；`bench_fixed` 的 `[8192]` 全是整块、手动写元素数为 0。

**预期结果**（可直接手工核对）：

| seq_len | t_tiles | 尾块 actual_len | 手写元素数 |
| --- | --- | --- | --- |
| 1300 | 82 | 4 | 512 |
| 547 | 35 | 3 | 384 |
| 16 | 1 | 16（整块） | 0 |
| 8192 | 512 | 16（整块） | 0 |

（本脚本为讲义示例代码，输出为确定性算术，无需 GPU 验证。）

#### 4.1.5 小练习与答案

**练习 1**：varlen 输入 `seq_lens=[1300, 547, 16]` 中，每条序列各有多少个 tile？哪些 tile 走手动写分支？

答：1300 → 82 个 tile（`81×16=1296`，尾块 4 行）；547 → 35 个 tile（`34×16=544`，尾块 3 行）；16 → 1 个整块 tile。走手动写分支的只有前两条序列的最后一个 tile，共 2 次；`16` 恰为 16 的倍数，无尾块。

**练习 2**：batched 模式（`B=3, T=1000`，即 `T_seq=333`）会走手动写分支吗？这说明该分支和 `IsVarlen` 什么关系？

答：会。`333 = 20×16 + 13`，每条序列的最后一个 tile `actual_len=13 < 16`。这说明分支条件只看 `actual_len < CHUNK`，与 `IsVarlen` 模板参数无关——varlen 只是最容易踩坑的场景（序列间无缝衔接），batched 同样可能有不满 16 行的尾 tile。

**练习 3**：为什么 STORE warp 的 32 个线程里只有一个（`lane_predicate`）真正干活？

答：两方面。（a）TMA 指令本身单线程发起即可（u3-l3 的 `elect_one_sync` 同理），多线程重复发起反而出错；（b）手动写路径依赖单线程程序序，天然无竞争。代码上 `make_store_pipeline` 也与之配套：`consumer_arv_count = 1`（[csrc/smxx/fwd_kernel2.cuh:209-212](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L209-L212) 传入的消费者数是 1），如果 32 个线程都 participate，到达计数就对不上了。

---

### 4.2 尾块逐元素写：为什么必须绕过 TMA

#### 4.2.1 概念说明

先把这个分支的存在理由说透。设想 varlen 输入 `seq_lens=[1300, 547, ...]`，`cu_seqlens=[0, 1300, 1847, ...]`。序列 0 的最后一个 tile 覆盖 token 行 `[1296, 1312)`，但序列 0 只拥有 `[0, 1300)`——行 1300..1311 **属于序列 1**。如果这个 tile 走 TMA 整块写：

1. **跨序列覆盖**：TMA 盒会写下 16 行，其中 12 行（1300..1311）落在序列 1 的领地。此时负责 `(seq 1, head h)` 的另一个 CTA 可能正在（或已经、或将要）写这些行——两个 CTA 写同一片 gmem、顺序由调度器随机决定，**结果非确定**。
2. **写的内容还是错的**：smem 尾行（行 4..15）里的输出是什么？回顾 u2-l7：K1 只对 **k 的尾行清零**（保护状态更新 δs = k_restoredᵀU 不被污染），而 **q 的尾行不清零**——`q_decayed` 的尾行来自 gmem 中「顺延下来的下一条序列 token 的 q」（TMA load 的 16 行盒读到的是物理上连续的数据，除非序列是最后一条，越界行才会被 TMA 填零）。于是 out 的尾行是「用错误的 q 算出的、语法合法但语义错误的输出」，写出去就是把垃圾伪装成结果。
3. **缓冲区越界**：若尾块属于最后一条序列，行号会越过 `out` 张量的末尾，造成 gmem 非法写——轻则污染相邻分配，重则触发 CUDA 错误。

所以正确策略只有一个：**尾块只写前 `actual_len` 行**。而 2.1 节说过 TMA 盒形状是描述符里固定的，没有「写 k 行」的开关（要按行拆成多次非 swizzle 的小搬运又会引入新的描述符与记账开销——这是从代码结构可以读出的权衡推断）。项目选择的方案最朴素也最稳：单线程、逐元素、只写有效行。

#### 4.2.2 核心流程

尾块分支的寻址模型。`out` 在 gmem 中按 token 展平为 `[T_total, H, D]`（即 `[B·T, H, D]`，B 恒为 1），每个元素地址为：

\[ addr(row, col) = \underbrace{(bos + 16t + row) \cdot H \cdot D}_{\text{token 行}} + \underbrace{head\_idx \cdot D}_{\text{head 偏移}} + col, \quad row \in [0, actual\_len),\; col \in [0, D) \]

它和整块分支的 TMA 偏移公式其实是同一个布局的两种写法：

\[ g\_out.layout()(h, tok, c) = h \cdot D + tok \cdot D \cdot H + c \]

二者由乘法交换律统一（\(H \cdot D = D \cdot H\)）。区别只在于 TMA 一次写满 16 行，手动写以 `row < actual_len` 为界。

#### 4.2.3 源码精读

[csrc/smxx/fwd_kernel2.cuh:757-766](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L757-L766) —— 尾块手动写分支全文：

```cpp
if (actual_len < CHUNK) {
    // Manual store for tail tile to avoid overwriting next sequence
    // Only one thread (lane_predicate) runs here, so loop over all D
    Tensor s_out = make_tensor(make_smem_ptr(out_stage_ptr), VOLayout{});
    for (int row = 0; row < actual_len; ++row) {
        int64_t global_base = (bos + t * CHUNK + row) * H * D + head_idx * D;
        for (int col = 0; col < D; ++col) {
            out_raw_ptr[global_base + col] = s_out(row, col);
        }
    }
}
```

四个值得咀嚼的细节：

1. **`out_raw_ptr` 是第二套寻址手段**。它是 host 侧直接传进来的 out 基地址（[csrc/smxx/fwd_launch.cu:213](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L213) 传的是 `out_ptr`），绕过一切 TMA 描述符，普通 `st.global` 寻址。同一个 out 张量于是有两套写法：整块用描述符、尾块用裸指针。
2. **`s_out(row, col)` 仍走 CuTe 布局函数**。smem 侧挂的是 `VOLayout`（swizzle 的 K_INTER 布局），`(row, col)` 由布局函数映射到 swizzle 后的偏移——单线程读 swizzle 布局毫无障碍，异或逻辑藏在布局求值里。这也保证了手动读到的与 TMA 会读到的（若发 TMA）是同一批比特。
3. **只写 `actual_len` 行**——分支的全部正确性就系于这个循环上界；写多了就是 4.2.1 的三宗罪。
4. **`int64_t` 基址**：`(bos + 16t + row) * H * D` 在大形状下（如 `T_total=8192, H=96` 时 `token·H·D` 可达上千万、乘 2 字节后接近 `int32` 上限的量级）必须用 64 位，这里显式声明了 `int64_t`。

顺带一提分支判据的对称性：手动分支处理 `actual_len ∈ [1, 16)`；`actual_len = 16` 走 TMA。不存在 `actual_len = 0` 的调用（`t_tiles` 是上取整结果，最后一个 tile 至少 1 行）。

#### 4.2.4 代码实践

**实践目标**：用区间算术亲眼看到「TMA 盒 vs 序列边界」的重叠，把 4.2.1 的三宗罪变成可计算的结论。

**操作步骤**：保存为 `tests/overlap_sim.py`，`python tests/overlap_sim.py`（纯 CPU）：

```python
# 示例代码：模拟 TMA 整块写（错误做法）与序列边界的重叠
CHUNK = 16

def simulate(seq_lens, head="h"):
    bounds, t = [], 0                     # (seq_idx, start, end)
    for i, L in enumerate(seq_lens):
        bounds.append((i, t, t + L)); t += L
    T_total = t
    print(f"T_total={T_total}, sequences={bounds}\n")
    for (i, s, e) in bounds:
        t_tiles = (e - s + CHUNK - 1) // CHUNK
        for tt in range(t_tiles):
            rows = range(s + tt * CHUNK, s + tt * CHUNK + CHUNK)  # TMA 盒固定 16 行
            over = [r for r in rows if r >= e]                     # 越出本序列的行
            if over:
                victim = next((j for j, (j2, s2, e2) in enumerate(bounds) if s2 <= over[0] < e2),
                               "OUT_OF_BUFFER")
                print(f"seq{i} tile{tt}: TMA 盒行[{rows[0]},{rows[-1]}] "
                      f"越界 {len(over)} 行 -> 侵入 {victim}；"
                      f"手动写只写 [{rows[0]},{rows[0] + min(CHUNK, e - s - tt*CHUNK)}) 无重叠")

simulate([1300, 547, 16])
```

**需要观察的现象**：seq0 的 tile81 盒覆盖 `[1296,1312)`，其中 12 行侵入 seq1；seq1 的 tile34 盒覆盖 `[1844,1860)`，其中 13 行侵入 seq2；seq2（恰 16 行）无重叠；没有行越过 `T_total`（因为 seq2 恰好补齐——若把 seq2 改成 16 的倍数之外的末序列，会看到 `OUT_OF_BUFFER`）。

**预期结果**：输出两行越界报告，越界行数分别为 12 和 13，与 4.1.4 表格中「16 − actual_len」一致。可自行把 `simulate([1300, 547, 21])` 的末序列改为非 16 倍数，观察最后一条序列的 TMA 盒越出整个缓冲区。

#### 4.2.5 小练习与答案

**练习 1**：假设删掉手动分支、所有 tile 都走 TMA，`seq_lens=[1300, 547, 16]` 的测试（exact match）会怎样失败？

答：序列 0 尾 tile 覆写行 1300..1311（属序列 1），与 `(seq 1, h)` CTA 的写入构成数据竞态——若它先写后被覆盖，序列 1 的前 12 行输出变成「序列 0 的错误尾行」；若它后写，测试碰巧通过。失败模式**取决于 CTA 调度顺序**，表现为偶发的 exact match 断言失败（`out_kernel != out_ref`），且错误元素集中在每条非整序列的头 `16 − actual_len` 行。

**练习 2**：证明手动写分支读 smem **不需要**任何 fence，而整块分支的发起点之前 MMA warp 必须有 `fence_view_async_shared()`。

答：套 2.4 节的代理判据。手动路径：MMA warp 用 `stmatrix` 写 out smem（generic 代理写），STORE leader 用 `s_out(row, col)` 读（generic 代理读），两端的先后由 `store_pipeline` 的 barrier 到达顺序（`producer_commit` → `consumer_wait`）建立 happens-before，同代理内可见性自动成立，无需 fence。整块路径：TMA 读 smem 属于 **async 代理**，generic 写 → async 读之间必须显式 `fence_view_async_shared()`——它由 MMA warp 在每 tile 收尾统一完成（见 4.3.3 的 [csrc/smxx/fwd_kernel2.cuh:736](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L736)），STORE warp 因此「免费」享受到了这条 fence。

**练习 3**：手动写循环的时间复杂度是多少？为什么可以接受？

答：每次尾块 `actual_len × D`（最坏 `16×128 = 2048`）次串行的单线程标量写，是最慢的写法。但每个 `(seq, head)` CTA 至多触发一次（且 `actual_len < 16`），对长序列而言占比可忽略（如 `seq_len=1300` 时尾块只占 1/82 个 tile）。这是「正确性优先、频率极低、代价可摊薄」的典型取舍；其真实开销在综合实践里用 ncu 度量。

---

### 4.3 流水线回收：acquire/commit 与 wait/release 的闭环

#### 4.3.1 概念说明

双分支只是「怎么写」，还有「什么时候能写、写完什么时候归还缓冲」——这就是 `store_pipeline` 的生命周期问题。`SharedStorageK2` 里 out 的缓冲是 `output[OutputStages]`（2 个 16×128 stage，[csrc/smxx/fwd_kernel2.cuh:95-97](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L95-L97) 与 [L101-L107](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L101-L107)）。它被两类角色以互斥节奏共享：

- **MMA warp（128 线程，Producer）**：往 `output[stage]` 写计算结果；
- **STORE warp（1 线程，Consumer）**：从 `output[stage]` 读出并写回 gmem。

`PipelineAsync` 用 full/empty 两个 barrier 做「写者不踩读者、读者不等空转」的双向握手。深度 2 意味着 MMA 最多领先 STORE 两个 tile——STORE 还在写 tile `t` 的输出时，MMA 已经在算 tile `t+1`、`t+2` 了。

一个容易忽视的约束：**stage 不能在被 TMA 读取时被 MMA 覆写**。回收（release）时机必须严格晚于「smem 不再被任何代理访问」——这正是 `tma_store_wait<0>()` 出现在 `consumer_release` 之前的原因。

#### 4.3.2 核心流程

按 tile 时间线展开（`OS = OutputStages = 2`）：

```text
MMA warp (每 tile t):
    producer_acquire(out_write)     # 阻塞到 stage (t mod OS) 的 empty barrier 翻转
                                    #   ← 即 STORE 已 release tile t-OS
    ... Phase 1-6 计算 ...
    Phase 5: stmatrix 把 out 写入 output[t mod OS]   (generic 写)
    NamedBarrier(128): 4 个 MMA warp 会师
    fence_view_async_shared()       # generic 写 → async 代理可见
    producer_commit(out_write)      # 128 arrive → full barrier 翻转
    ++out_write

STORE warp (每 tile t):
    consumer_wait(out_read)         # 等 full barrier 翻转（MMA 全部 commit）
    双分支写出（4.1 / 4.2）
    tma_store_wait<0>()             # 在途 bulk 组清零（详见 4.3.3 的分支差异分析）
    consumer_release(out_read)      # empty barrier 翻转 → 放行 MMA 对该 stage 的复用
    ++out_read
```

两 barrier 的翻转条件（到达计数）：

\[ full:\; \sum_{\text{MMA threads}} arrive = 128, \qquad empty:\; \sum_{\text{STORE threads}} arrive = 1 \]

#### 4.3.3 源码精读

**（a）MMA 侧的持有区间**。[csrc/smxx/fwd_kernel2.cuh:434-442](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L434-L442) 显示 `producer_acquire` 位于 tile 循环**最开头**——MMA 从 Phase 1 开始计算时就已「持有」out stage，而不是等到 Phase 5 要写时才申请：

```cpp
for (int t = 0; t < t_tiles; ++t) {
    store_pipeline.producer_acquire(out_write);
    load_pipeline.consumer_wait(load_read);
    ...
```

这意味着深度 2 的缓冲在「MMA 计算 tile t」与「STORE 写出 tile t−2」之间形成完全重叠，MMA 几乎不停等 STORE。

**（b）MMA 侧的提交三连**。[csrc/smxx/fwd_kernel2.cuh:733-741](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L733-L741)：

```cpp
compute_barrier.arrive_and_wait();          // 4 个 MMA warp 会师（NamedBarrier, 128 线程）
#ifndef TMA_DISABLE_ALL
    cutlass::arch::fence_view_async_shared();  // generic(STSM) 写 → async(TMA) 可见
    store_pipeline.producer_commit(out_write); // full barrier 翻转，唤醒 STORE
    load_pipeline.consumer_release(load_read); // 顺手归还输入 stage（u3-l3 的对称面）
    ++load_read;
    ++out_write;
#endif
```

会师的原因：一个 out stage 由 4 个 MMA warp 各写 2 个 16×16 列块拼成（u3-l4），必须凑齐 4 个 warp 的 STSM 才能宣布 stage「满」；fence 紧随其后，把 STSM 的 generic 写推进 async 代理的可见域，为 TMA 读 smem 铺路。这回答了 4.2.5 练习 2 的「STORE 为何免费享受 fence」。

**（c）STORE 侧的回收三连与 `tma_store_wait<0>` 的双重身份**。回到 [csrc/smxx/fwd_kernel2.cuh:781-783](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L781-L783)：

```cpp
tma_store_wait<0>();
store_pipeline.consumer_release(out_read);
++out_read;
```

- **TMA 路径上它是正确性必需**：`cute::copy` 返回时 bulk store 还在从 smem 读源。若不等就 `consumer_release`，MMA warp 的下一个 `producer_acquire` 立即通过并开始覆写该 stage，TMA 读到的是新旧混合的数据——输出损坏且难以复现。`wait<0>` 保证在途组清零（至少 smem 不再被读，2.2 节的 `.read` 语义），此后 release 才安全。
- **手动路径上它（几乎）是空转**：手动分支没有发起任何 bulk copy（也就没有配对的 `tma_store_arrive`），且**前面每个 tile 结尾都执行过 `wait<0>`**，进入本分支时在途组恒为 0，等待立即返回。它对 smem 安全仍然无害且语义正确——单线程的 `s_out(row, col)` 读完即止，不涉及异步访问。作者把它放在两分支之外，让两条路径共享同一收尾序列，代码更统一。
- **`consumer_release` 是唯一的归还动作**：empty barrier 翻转后，MMA warp 在 `t + OS` 个 tile 之后对同一 stage 的 `producer_acquire` 才会放行。out stage 的生命周期闭环：`acquire（MMA）→ STSM 写 → fence → commit → wait（STORE）→ 写出（TMA/手动）→ wait<0> → release（STORE）`。

**（d）参数从哪来**。[csrc/smxx/fwd_kernel2.cuh:209-212](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L209-L212) 构造 store_pipeline 时传入 `kComputeThreads=128` 与 `1`，对应 `make_store_pipeline` 里的 `producer_arv_count / consumer_arv_count`（[csrc/smxx/utils.cuh:136-138](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/utils.cuh#L136-L138)）：

```cpp
params.role = role;
params.producer_arv_count = num_producers;   // 128: 4 个 MMA warp 全体
params.consumer_arv_count = num_consumers;   // 1:   STORE leader lane
```

对照 u3-l3 的 load pipeline（`transaction_bytes` 驱动、生产者 1 个 leader、消费者 128 线程），store pipeline 恰好是**角色与计数完全镜像**的纯软件版：生产者变多数（128）、消费者变少数（1）、无事务字节。

#### 4.3.4 代码实践

**实践目标**：以源码阅读方式把 out stage 的占用-回收时间线画出来，验证「MMA 领先至多 2 个 tile」。

**操作步骤**：

1. 通读 [csrc/smxx/fwd_kernel2.cuh:425-442](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L425-L442)（MMA 循环头）与 [L745-784](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L745-L784)（STORE 循环），在纸上为 `t = 0..4` 列一张双行时间线表：MMA 行写 `acquire(t)…commit(t)`，STORE 行写 `wait(t)…release(t)`，stage 列填 `t mod 2`。
2. 回答：MMA 的 `acquire(2)` 依赖 STORE 的哪个动作？`acquire(0)` 呢？

**需要观察的现象**：`acquire(0)` 与 `acquire(1)` 因初始全空而立即通过；从 `acquire(2)` 起每次都要等 `release(t−2)`。

**预期结果**（时间线答案，节选）：

| tile | MMA（Producer） | stage | STORE（Consumer） |
| --- | --- | --- | --- |
| 0 | acquire(0) 立即过 → commit(0) | 0 | wait(0) → 写出 → wait<0> → release(0) |
| 1 | acquire(1) 立即过 → commit(1) | 1 | wait(1) → … → release(1) |
| 2 | acquire(2) **阻塞至 release(0)** → commit(2) | 0 | wait(2) → … → release(2) |

即 STORE 的 `release(t−2)` 是 MMA `acquire(t)` 的唯一放行条件——领先度被 `OutputStages` 精确钳制在 2。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `tma_store_wait<0>()` 从循环里删掉（其余不动），哪条路径会坏、怎么坏？

答：整块 TMA 路径会坏。release 后 MMA 可能立即覆写 stage，而 TMA 尚未读完 smem 源，后续 tile 的输出里会混入**下一个 tile 的数据**（读到被覆写后的新值）。手动路径不受影响（它没有异步 smem 访问）。由于是否踩中取决于 TMA 与 MMA 的相对进度，症状是偶发的输出错误而非稳定复现。

**练习 2**：`producer_arv_count=128` 说明 `producer_commit` 由谁执行几次？为什么 out 的 full barrier 不像 load 那样由 leader 单线程翻转？

答：4 个 MMA warp 的全部 128 线程各 arrive 一次（`producer_commit` 在每个 MMA 线程上都被调用，[csrc/smxx/fwd_kernel2.cuh:737](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L737) 位于 warp 角色分支内的 per-thread 代码路径）。原因：一个 out stage 的内容由 4 个 warp 分片拼成，「满」的判定必须汇聚所有写者；而 load 侧的数据由 TMA 硬件一次写齐，full 的判定交给 mbarrier 的事务字节，只需 leader 登记（u3-l3）。两套计数的差异正是「软件生产者 vs 硬件生产者」的镜像。

**练习 3**：综合练习里你会用 ncu 对比 fixed 与 varlen。在动手前先推断：两者的 recurrence kernel 平均每 CTA 指令数差在哪个代码段？

答：差在尾块手动写循环——varlen 每个「非 16 倍数序列 × 每个 head」的 CTA 多执行一次 `actual_len × D` 次的标量写循环（含地址自增与循环跳转，每元素约数条指令；`[1300,547,2048,963,271,3063]` 共 5 个尾块，`[1024]×8` 无尾块），fixed（`[8192]`）为 0。注意两模式 CTA 数与 tile 数不同，必须按「每 CTA」或「每 tile」归一化后比较（综合实践中会处理）。

## 5. 综合实践

综合实践把本讲三个模块串成一条线：**构造带尾块的 varlen 输入 → 确认无越界且 bit-exact → 用 ncu 度量尾块分支的真实指令开销**。

### 5.1 实践目标

用 `seq_lens=[1300, 547, 16]`（前两条都非 16 的倍数，尾块 `actual_len` 分别为 4 和 3；最后一条恰为整块作对照）验证尾块处理的正确性与代价。

### 5.2 操作步骤

**步骤 1：正确性测试。** 在 `tests/` 目录下新建 `tail_test.py`（仿照 [tests/test_fwd.py:265-312](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/test_fwd.py#L265-L312) 的 `test_fwd_varlen`）：

```python
# 示例代码：需在 tests/ 目录下运行（依赖 torch_ref）
import math
import torch
import torch.nn.functional as F
import flash_kda
from torch_ref import torch_ref

H, D = 96, 128
LOWER_BOUND = -5.0
seq_lens = [1300, 547, 16]
T_total, N = sum(seq_lens), len(seq_lens)
cu_seqlens = torch.tensor([0] + list(torch.cumsum(torch.tensor(seq_lens), 0).tolist()),
                          dtype=torch.long, device='cuda')
torch.manual_seed(0)

q = F.normalize(torch.randn((1, T_total, H, D), dtype=torch.float32, device='cuda'), p=2, dim=-1).to(torch.bfloat16)
k = F.normalize(torch.randn((1, T_total, H, D), dtype=torch.float32, device='cuda'), p=2, dim=-1).to(torch.bfloat16)
v = torch.randn((1, T_total, H, D), dtype=torch.bfloat16, device='cuda')
g = torch.randn((1, T_total, H, D), dtype=torch.bfloat16, device='cuda')
beta = torch.randn((1, T_total, H), dtype=torch.bfloat16, device='cuda')
A_log = torch.rand(H, dtype=torch.float32, device='cuda')
dt_bias = torch.rand(H, D, dtype=torch.float32, device='cuda')
h0 = torch.arange(N * H * D * D, dtype=torch.float32, device='cuda').reshape(N, H, D, D).to(torch.bfloat16)
scale = 1.0 / math.sqrt(D)

out_k = torch.zeros_like(q); s_k = torch.zeros_like(h0)
flash_kda.fwd(q, k, v, g, beta, scale, out_k, A_log=A_log, dt_bias=dt_bias,
              lower_bound=LOWER_BOUND, initial_state=h0.clone(), final_state=s_k,
              cu_seqlens=cu_seqlens)
torch.cuda.synchronize()

out_r = torch.zeros_like(q); s_r = torch.zeros_like(h0)
torch_ref(q, k, v, g, beta, scale, out_r, A_log=A_log, dt_bias=dt_bias,
          lower_bound=LOWER_BOUND, initial_state=h0.clone(), final_state=s_r,
          cu_seqlens=cu_seqlens)

assert torch.equal(out_k, out_r), "output mismatch (possible cross-sequence overwrite!)"
assert torch.equal(s_k, s_r), "final_state mismatch"
print("tail-tile varlen test passed: no out-of-bounds, bit-exact")
```

运行：`cd tests && python tail_test.py`。

**步骤 2：ncu 抓取两种模式。** 参考 [benchmarks/ncu.sh](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/benchmarks/ncu.sh) 的正则，把 `-k` 收窄到 recurrence kernel（待本地验证；需 root 或 `--target-processes all` 等环境条件同 ncu.sh）：

```bash
# fixed：T=8192 单序列，512 个 tile 全整块、0 次手动写
ncu --set full --kernel-name-base function -k "regex:_flash_kda_fwd_recurrence" \
    --clock-control none --import-source yes --source-folders . \
    --export rep_fixed.ncu-rep \
    python benchmarks/bench_fwd.py --mode fixed --warmup 0 --iters 5 --repeats 1

# varlen：[1300,547,2048,963,271,3063]（5 个尾块）+ [1024]*8（0 个尾块）
ncu --set full --kernel-name-base function -k "regex:_flash_kda_fwd_recurrence" \
    --clock-control none --import-source yes --source-folders . \
    --export rep_varlen.ncu-rep \
    python benchmarks/bench_fwd.py --mode varlen --warmup 0 --iters 5 --repeats 1
```

**步骤 3：导出并归一化对比。** 用 `--import` 导出 CSV（具体列名以本地 ncu 版本为准，待本地验证）：

```bash
ncu --import rep_fixed.ncu-rep  --csv --page raw > fixed_raw.csv
ncu --import rep_varlen.ncu-rep --csv --page raw > varlen_raw.csv
# 从中抽取 smsp__inst_executed.sum、launch__grid_size、gpu__time_duration.sum
```

注意归一化：fixed 的 grid 是 `(1, 96)`（1 序列 × 96 头），varlen 第一组是 `(6, 96)`；且 varlen 每 CTA 的 tile 数不同（82/35/128/61/17/192）。建议把指标除以 CTA 数后再比，并单独看 `[1024]*8` 这一档（无尾块的 varlen）作隔离对照。

### 5.3 需要观察的现象与预期结果

1. `tail_test.py` 两次 `torch.equal` 全部通过——说明尾块手动写在真实的跨 CTA 并发下没有覆盖任何相邻序列（若有覆盖，错误会集中在 token 1300..1311 与 1847..1849 等边界行）。
2. ncu 侧预期：varlen（含尾块档）的 recurrence kernel 平均每 CTA 指令数**略高于** fixed 与 `[1024]*8` 档；差距量级应与「每 CTA 一次尾块手动写 ≈ `actual_len × 128` 次标量 store 及配套地址/分支指令」相称（如 `actual_len=4` 约 512 个元素）。具体差值**待本地验证**——若差异小于手写循环的估算量级，说明该分支被流水线掩盖（它在 STORE warp 上执行，而关键路径在 MMA warp）。

### 5.4 如果没有 ncu 环境

退化为计时对比也可接受：`python benchmarks/bench_fwd.py --mode all --warmup 30 --iters 200` 观察 fixed 与 varlen 档的 mean 差异中尾块的贡献（受序列形状混杂影响，只能定性）。或者在 `tests/tail_test.py` 中把三条序列分别改成 `[1300]`、`[1304]`（补齐为 16 倍数）单独计时对比（`1304 = 81×16+8`，仍非整块，可改为 `[1312] = 82×16`）。

## 6. 本讲小结

- **STORE warp 是输出流水线的单线程消费者**：leader lane 独自执行 `consumer_wait → 双分支写出 → tma_store_wait<0> → consumer_release` 的循环，`consumer_arv_count=1` 与之配套。
- **双分支的分水岭是 `actual_len = min(16, seq_len − 16t)`**：整块走一次 16×128 的 TMA store（`tma_store_arrive` 编组）；尾块退化为单线程逐元素写 `out_raw_ptr`，只写 `actual_len × D` 个元素。
- **尾块必须绕过 TMA**：varlen 下序列紧凑排布，固定 16 行的 TMA 盒会覆盖下一条序列的头部 token，与相邻 CTA 竞态；末序列还会写出缓冲区。smem 尾行本身也是「用下一条序列的 q 算出的无意义值」（K1 只清零 k 尾行、不清 q 尾行）。
- **两套 out 寻址手段并存**：TMA 描述符（`(H,T,D):(D,D·H,1)` 布局求偏移）与裸指针（`(bos+16t+row)·H·D + head·D + col`），二者是同一布局的两种写法；手动读 smem 仍经 `VOLayout` 布局函数处理 swizzle。
- **流水线回收闭环**：MMA（128 线程 Producer）`acquire → STSM → NamedBarrier 会师 → fence_view_async_shared → commit`；STORE `wait → 写出 → wait<0> → release`。`tma_store_wait<0>` 在 TMA 路径上是防止「TMA 还在读、MMA 已覆写」的正确性必需，在手动路径上是立即返回的空转。
- **深度 2 的领先度**：STORE 的 `release(t−2)` 放行 MMA 的 `acquire(t)`，out 缓冲让 MMA 计算与 STORE 写出最多重叠两个 tile。

## 7. 下一步学习建议

本讲之后，K2 的三个 warp 角色（LOAD/MMA/STORE）已全部读完，建议：

1. **读 u3-l8（数值精度总账）**：把本讲出现的 generic/async 代理、bf16 量化点放进全 kernel 的精度地图，理解为什么手动写路径写的也是 bf16 直出值。
2. **进入 u3-l10（基准与剖析）**：本讲综合实践里的 ncu 命令将在那里系统化，学会解读 occupancy、smem、寄存器与指令数指标。
3. **动手实验（衔接 u3-l12 的消融方法学）**：尝试把 `kOutputStages` 从 2 改成 3（[csrc/smxx/fwd_launch.cu:29-30](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L29-L30)），重编译后跑 `tests/test_fwd.py` 与 benchmark，观察「MMA 领先度 +1」对 varlen 尾块场景是否有可测收益（注意 u3-l2 讲过的 union 钳制效应可能让 smem 不变）。
4. 若想继续源码细读，可回头对照 [csrc/smxx/fwd_kernel2.cuh:786-835](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L786-L835) 的状态输出路径，体会 STORE warp 在主循环之外如何复用同一套「TMA + arrive/wait」手法写出 128×128 的最终状态。
