# SampleScratch：一次分配的缓冲区布局与 selection_width

## 1. 本讲目标

学完本讲，你应该能够：

- 逐字段说出 `SampleScratch` 里每个缓冲区的角色：`row_indices`、`argmax_partial_values` / `argmax_partial_indices`、`top1_values`、`argmax_out`、`argmax_host` / `argmax_host_alt`、`identity_rows`，以及内嵌的 `BatchSamplingScratch`。
- 解释解码路径为什么要求**一次分配、指针稳定**——绝不能每个解码步重新分配设备缓冲。
- 用定长公式 \( R \times \lceil V / 4096 \rceil \) 算出 argmax partials 缓冲的元素个数，并说明它来自两阶段（split）argmax 的 tiling 结构。
- 说清 `vocab`（arena 物理宽度）与 `selection_width`（可发射词表宽度）为什么是两个数：模型把 logits GEMM 对齐到 tile 倍数后，多出来的填充列不是可发射 token，不能参与 u2-l1 讲的 `top_p <= 1/宽度` 核坍缩判定（#1046 场景）。
- 论证贪心行与非贪心行的缓冲为什么互不相交、可以共用同一份 scratch 服务一个混合批。

本讲是单元二的第二讲：u2-l1 已经确定了「一行走不走 argmax」的判定条件，本讲看这个判定的**物质基础**——判定所依赖的 `selection_width`、判定结果所写入的全部缓冲，都住在这份一次分配的 `SampleScratch` 里。下一讲 u2-l3 会沿着这些缓冲走通 `select_batch` 的三路分发。

## 2. 前置知识

本讲需要一点 GPU 内存模型的常识，全部概念先用直觉语言讲一遍。

- **解码步（decode step）**：自回归生成是循环的——每一步模型为批内每个活跃请求算出一行 logits（全词表各一个分数），采样层从每行选出一个 token id，再把 token 喂回模型。采样层因此被调用成千上万次，每次的输入输出形状几乎完全一样。
- **设备内存与主机内存**：GPU 自己的显存（device memory）与 CPU 内存（host memory）是两块独立空间，靠 H2D（host-to-device，上行）与 D2H（device-to-host，下行）拷贝交换数据。`CudaSlice<T>` 是一块显存的 Rust 句柄。
- **分配很贵，拷贝也不免费**：`cudaMalloc` 量级的显存分配是慢操作（可能牵动驱动同步），H2D / D2H 拷贝则要走 PCIe。推理引擎的常规做法是**启动时按最大形状分配一次，之后每步只写入数据、不重新分配**。
- **指针稳定（pointer stability）**：CUDA 图（CUDA Graph）会把一整段核调用录制下来重放，录制时烘焙的是**缓冲的地址**。如果某一步换了缓冲（重新分配），地址变了，图里录的旧地址就指向已释放或错误的内存。所以「解码路径的缓冲绝不能每步重分配」不是性能建议，是正确性约束（u4-l1 展开）。
- **pinned 内存（页锁定内存）**：普通主机内存是可换页的，从它做 D2H 拷贝时驱动会先把它钉住再拷，语义偏同步；用 `alloc_pinned` 预先锁定的页则支持真正的异步 D2H。本讲只需要记住：`SampleScratch` 的两个主机落地区是 pinned 的，事故级的细节（#704）留给 u4-l2。
- **两阶段（split / tiled）argmax 的直觉**：一行 15 万个 bf16 的 argmax 如果只用一个 GPU 线程块（CTA）扫，太慢。把一行切成若干 4096 元素的 tile，每个 tile 由一个 CTA 求出局部的 (最大值, 下标)——这就是 **partial**；第二阶段每行用一个 CTA 把所有 partial 归约成全局 argmax。partial 的个数决定了缓冲长度。
- **tile 对齐与填充列**：GEMM（矩阵乘）在 tile 倍数宽度上效率最高，模型可能把 logits 输出宽度从真实词表 \( V \)（如 151936）垫宽到 tile 的倍数。垫出来的列**不是可发射 token**——采样决策不能把它们数进词表宽度。
- **承接 u2-l1**：`effectively_greedy(params, vocab_size)` 在 `top_p <= 1/vocab_size` 时判行等价贪心。本讲要回答的关键问题是：这里的 `vocab_size` 该用哪个宽度？

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
| --- | --- | --- |
| `pegainfer-sample/src/lib.rs` | crate 全部实现（单文件） | `SampleScratch` 结构体定义（全部字段）、`new` / `with_selection_width` 两个构造函数、`select_batch` 顶部对 `scratch.vocab` 的校验与路由闭包里对 `selection_width` 的使用 |
| `pegainfer-sample/tests/select_batch.rs` | 需要 GPU 的模型无关集成测试 | `padded_arena_width_does_not_suppress_the_argmax_routing`——#1046 场景的回归测试，直接对比两种构造方式的行 为差异 |
| `pegainfer-kernels/src/ops/sampling.rs` | 底层核封装（crate 外，补充阅读） | `argmax_batch_bf16_split_partials_len` 定长公式、`BatchSamplingScratch`（非贪心侧 scratch）的字段与构造 |

`SampleScratch` 是策略层自己拥有的状态：底层 crate `pegainfer-kernels` 提供无状态的一次性核入口（每次调用都要求调用方把缓冲传进来），本 crate 把这些缓冲**按解码路径的最大形状一次性备齐**并长期持有——这是 u1-l1 分层里「本 crate 拥有可复用 scratch」的具体形态。

## 4. 核心概念与源码讲解

### 4.1 SampleScratch::new / with_selection_width：一次分配的构造与逐字段布局

#### 4.1.1 概念说明

`SampleScratch` 回答一个工程问题：`select_batch` 每个解码步都要用一批中间缓冲（行号表、argmax partials、回读落地区、采样通道的整行概率缓冲……），这些缓冲该放哪？

答案的原则写在结构体文档里：**复用 across decode steps，解码路径需要指针稳定的缓冲，所以绝不每步重分配**。两条推论：

1. **形状在构造时烘焙**。构造时给定 `max_rows` 与 `vocab`，所有缓冲按这两个上限一次分好；之后每步实际用的行数可以更少（kernel 只处理前 k 个贪心行），但形状不能更大——超了会被 `ensure!` 拒绝（u2-l3 展开）。
2. **贪心与非贪心各用各的缓冲，互不相交**。贪心行走 `row_indices` → partials → `argmax_out` → pinned 落地区这条链；非贪心行走内嵌的 `BatchSamplingScratch`（它的 `probs` 缓冲要装下 `max_rows × vocab` 的 f32 概率）。两条链在 `select_batch` 里**顺序执行**（贪心分支先做完并同步，采样分支后做），没有并发写冲突，所以一份 scratch 就能服务一个贪心/采样混合批——不需要调用方为混合批准备两份状态。

另一个容易忽略的角色：`identity_rows`，内容是恒等映射 \( 0, 1, \dots, \text{max\_rows}-1 \)。它是给「全贪心分阶段路径」（u4-l1 的 `greedy_argmax_ids`）准备的——那个路径没有「挑出的贪心行子集」可上传，但又想复用同一个 indexed argmax 核，于是干脆传一张恒等行表，让「每行都是贪心行」退化成 indexed 版本的特例。这就是本 crate 不需要第二个「连续行 argmax」核变体的原因。

#### 4.1.2 核心流程

构造流程（`with_selection_width` 的骨架）：

```text
输入: vocab（arena 物理宽度）、selection_width（可发射宽度）、max_rows
1. 校验: vocab > 0 且 max_rows > 0
2. 校验: 0 < selection_width <= vocab
3. partials = argmax_batch_bf16_split_partials_len(max_rows, vocab)   # 4.2 的公式
4. 分配贪心侧设备缓冲:
     row_indices          <- max_rows 个 i32
     argmax_partial_values / _indices <- partials 个
     top1_values          <- max_rows 个 bf16
     argmax_out           <- max_rows 个 i32
     identity_rows        <- 上传 0..max_rows 的 i32
5. 分配 pinned 主机落地区: argmax_host、argmax_host_alt（各 max_rows 个 i32）
6. sampling = BatchSamplingScratch::new(ctx, max_rows, vocab)   # 非贪心侧，注意传的是 arena 宽度
7. 记录 vocab、selection_width、max_rows 三个数
```

之后每个解码步，`select_batch` 只**写入**这些缓冲，从不重新分配；路由判定读 `selection_width`，形状校验读 `vocab`。

#### 4.1.3 源码精读

先看结构体的字段定义与它自带的文档——每个字段都有一句角色说明：

[src/lib.rs:L61-L90](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L61-L90) 定义了 `SampleScratch` 的全部字段：`row_indices`（贪心行号表，indexed argmax 的输入）、两个 argmax partial 缓冲、`top1_values`、`argmax_out`、两个 pinned 落地区、`identity_rows`、内嵌 `sampling`，以及最后三个纯记录字段 `vocab` / `selection_width` / `max_rows`。结构体上方的文档注释（[src/lib.rs:L56-L60](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L56-L60)）就是本节开头的两条原则：「复用 across decode steps」「贪心与非贪心行使用不相交的缓冲并顺序运行，所以单份 `SampleScratch` 覆盖完整混合批」。

两个构造函数的关系是简单的委托：

[src/lib.rs:L92-L95](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L92-L95) `new` 直接调 `with_selection_width(ctx, vocab, vocab, max_rows)`——默认把可发射宽度设成 arena 宽度。也就是说 `new` 只是「模型没有做 tile 对齐垫宽」时的便利入口；一旦 arena 比 词表宽，就必须显式用 `with_selection_width`。

构造函数本体：

[src/lib.rs:L102-L151](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L102-L151) 是 `with_selection_width` 的完整实现。关键点逐段看：

- [src/lib.rs:L108-L115](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L108-L115) 两条前置校验：形状必须为正，且 `selection_width` 必须落在 `1..=vocab`。第二条是本讲的隐藏主线——可发射宽度不能超过物理宽度，垫宽只能让分母变大、不能让它变小。
- [src/lib.rs:L116](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L116) `let partials = argmax_batch_bf16_split_partials_len(max_rows, vocab);`——partials 长度来自底层公式（4.2 节），注意传入的是 **arena 宽度**：argmax 核要扫满整行，包括填充列。
- [src/lib.rs:L134-L141](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L134-L141) 两个 pinned 落地区的分配，用的是 `unsafe { ctx.ctx.alloc_pinned::<i32>(max_rows) }`。字段定义处的注释（[src/lib.rs:L71-L79](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L71-L79)）记录了为什么必须 pinned：一次 pageable 回读的同步语义曾把亚毫秒的调用拖成 23.6ms（#704，u4-l2 专题）。`_alt` 是「上一个回读还在被收集时」的交替槽。同处的告诫：pinned 页是 write-combined 的，CPU 读不缓存，**不要把这个缓冲扩展成热点读取结构**。
- [src/lib.rs:L142-L145](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L142-L145) `identity_rows` 在构造时一次性上传 `0..max_rows`——它是常量表，之后每步都复用。
- [src/lib.rs:L146](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L146) `sampling: BatchSamplingScratch::new(ctx, max_rows, vocab)?`——非贪心侧 scratch 在这里嵌进来，注意第三个参数传的是 **arena 宽度** `vocab` 而不是 `selection_width`：采样器读整行 logits，概率缓冲按物理宽度备。

「一次分配」在消费端的样子——`select_batch` 的贪心分支只写不配：

[src/lib.rs:L234-L237](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L234-L237) 每步把贪心行号 `memcpy_htod` **写进已有的** `scratch.row_indices`（长度永远是 `max_rows`，本步只前 `greedy.len()` 个有效）；[src/lib.rs:L238-L247](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L238-L247) 把 scratch 的六个缓冲按引用喂给 `argmax_batch_bf16_split_indexed_into`；[src/lib.rs:L248-L262](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L248-L262) D2H 写进 pinned 槽 `argmax_host`、自旋等拷贝事件、再按行号把 token 归位。整条链没有任何 `alloc`。

`identity_rows` 的实际消费者在分阶段路径：

[src/lib.rs:L344-L368](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L344-L368) `greedy_argmax_ids` 用 `&scratch.identity_rows` 当行号表调用**同一个** indexed argmax 核——「处理第 0..rows 行」被表达成「处理行号表为恒等映射的前 rows 项」。贪心路由路径（上一段）上传的是本步动态挑出的贪心行子集，分阶段路径上传的是静态恒等表；同一个核、同一份 partials 与输出缓冲，两种用法通吃。

补充阅读——非贪心侧 `BatchSamplingScratch` 长什么样（本 crate 视角它是个黑盒，但字段值得认识）：

[pegainfer-kernels/src/ops/sampling.rs:L32-L77](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/src/ops/sampling.rs#L32-L77)（crate 外，永久链接指向同一 commit 的 kernels 路径）定义并构造了它：大头是 `probs: CudaSlice<f32>`，长度 `max_rows * vocab`——一行全宽概率的 f32 展开，是整份 scratch 里最大的显存消费者；其余是每行参数（`temperature` / `top_k` / `top_p` / `min_p`）的小缓冲、`out`、以及 OnlineSoftmax 词表切分用的 workspace（字节数 `max_rows * ⌈vocab/8192⌉ * 8 + 256`）。它同样只在构造时分配一次。

#### 4.1.4 代码实践

**实践目标**：把「一次分配」从口号变成一张可核对的账目表——给定 `vocab = 151936`、`max_rows = 16`，逐项写出 `SampleScratch` 每个缓冲的元素类型、长度与用途。这是纯纸面推导，不需要 GPU。

**操作步骤**：

1. 打开 [src/lib.rs:L61-L90](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L61-L90)，对每个字段问三个问题：元素类型是什么？长度由哪个公式给出（`max_rows`、`max_rows × vocab`、还是 `partials`）？谁在什么时候写它、谁读它？
2. `partials` 一项代入 [src/lib.rs:L116](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L116) 的公式手算：\( \lceil 151936 / 4096 \rceil = \lceil 37.093\ldots \rceil = 38 \)（验算：\( 4096 \times 37 = 151552 \)，余 384，所以要第 38 个 tile），再乘 `max_rows = 16`。
3. 顺手估算显存量级：贪心侧全部缓冲加起来多大？`sampling.probs` 一项又是多大？（f32 按 4 字节、bf16 按 2 字节、i32 按 4 字节。）
4. 如需机械验证，可以把长度公式抄进一个临时脚本（示例代码，不属于项目）：

   ```rust
   // 示例代码：仅用于核对长度公式，不依赖项目与 GPU
   fn main() {
       let (vocab, max_rows): (usize, usize) = (151_936, 16);
       let partials = max_rows * vocab.div_ceil(4096);
       println!("partials = {partials}");                       // 期望 608
       println!("probs(f32) = {}", max_rows * vocab);            // 期望 2_430_976
       println!("softmax_ws = {}", max_rows * vocab.div_ceil(8192) * 8 + 256); // 期望 2688
   }
   ```

**需要观察的现象 / 预期结果**（参考答案）：

| 缓冲 | 位置 | 元素类型 | 长度（vocab=151936, max_rows=16） | 用途 |
| --- | --- | --- | --- | --- |
| `row_indices` | 设备 | `i32` | 16 | 贪心行号表；每步 H2D 重写前 k 项，indexed argmax 的输入 |
| `argmax_partial_values` | 设备 | `f32` | 608 | 两阶段 argmax 第一阶段：每个 tile 的局部最大值 |
| `argmax_partial_indices` | 设备 | `i32` | 608 | 每个 tile 局部最大值的全局下标（partials 携带全局下标才能保破并列次序） |
| `top1_values` | 设备 | `bf16` | 16 | 每贪心行 top-1 logit——argmax 核的必要出参，本 crate 不读它 |
| `argmax_out` | 设备 | `i32` | 16 | 每贪心行一个 token id，按 `row_indices` 顺序 |
| `argmax_host` | 主机（pinned） | `i32` | 16 | `argmax_out` 的固定页落地区（回读槽 0） |
| `argmax_host_alt` | 主机（pinned） | `i32` | 16 | 交替落地区（回读槽 1），上一槽还在被收集时用 |
| `identity_rows` | 设备 | `i32` | 16 | 常量 `0..16`；全贪心分阶段路径借它复用 indexed argmax 核 |
| `sampling`（内嵌 `BatchSamplingScratch`） | 设备 | 多块 | `probs` = 16×151936 = 2,430,976 个 f32；参数小缓冲各 16；softmax workspace 2688 字节 | 非贪心行批式采样的全部中间状态 |

量级结论：贪心侧设备缓冲合计约 \( 608 \times 4 \times 2 + 16 \times (4+4+4+4) + 16 \times 2 \approx 5.1 \) KB，加上两个 64B 的 pinned 槽；而采样侧仅 `probs` 一项就是 \( 2{,}430{,}936 \times 4 \approx 9.3 \) MiB——**scratch 的显存大头在非贪心通道**。这也解释了为什么贪心路由（u2-l1 的核坍缩判定）是省钱省确定性的双赢：能走 argmax 的行根本不碰这块 9 MiB 的概率缓冲。

**待本地验证**：表中数字由公式直接推出；如你手头有 GPU，可在 `tests/select_batch.rs` 的调试器/日志里（或给临时脚本跑一次）核对 `partials = 608`。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `row_indices` 分配 `max_rows` 项，而每步通常只写前 `greedy.len()` 项？多余的项会造成错误吗？

<details><summary>参考答案</summary>

分配按上限烘焙、使用按本步实际行数：核调用时显式传了 `greedy.len()`（[src/lib.rs:L238-L242](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L238-L242) 的 `rows` 参数），核只读前那么多项，尾部的陈旧值不参与计算。多分配换来的正是「每步零分配、指针恒定」。
</details>

**练习 2**：`select_batch` 的贪心分支和采样分支都写 `scratch`，为什么一份 scratch 不会互相踩？

<details><summary>参考答案</summary>

两个原因叠加：**(a) 缓冲不相交**——贪心链（`row_indices`、partials、`top1_values`、`argmax_out`、pinned 槽）与采样链（内嵌 `BatchSamplingScratch` 的全部缓冲）是各自独立的分配，没有任何共享缓冲；**(b) 顺序执行**——`select_batch` 里贪心分支先运行并在读回落后（[src/lib.rs:L248-L262](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L248-L262)）才进入采样分支，同一 stream 上先后有序。结构体文档（[src/lib.rs:L56-L60](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L56-L60)）明确写了「disjoint buffers and run sequentially」。
</details>

**练习 3**：`top1_values` 这个字段在 `select_batch` 里从头到尾没被读过，为什么还必须存在？

<details><summary>参考答案</summary>

它是 argmax 核签名的必要出参（核同时产出每行 top-1 的值与下标），调用方必须提供一块合法的输出缓冲，哪怕策略层只消费下标。字段注释（[src/lib.rs:L66-L68](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L66-L68)）原话是「a required out-param of the argmax kernel, not read here」。自驱贪心路径的模型（如 Kimi-K2 的分片局部 argmax + 跨 rank 归约）恰恰要用这个 top-1 logit 值。
</details>

### 4.2 argmax partials 定长公式：\( R \times \lceil V / 4096 \rceil \)

#### 4.2.1 概念说明

partials 缓冲的长度不是拍脑袋的常数，而是由 argmax 核的并行结构**推导**出来的定值。理解这个公式等于理解「split argmax 为什么存在」：

- 单 CTA 扫一行 15 万元素，并行度太低、延迟太高；
- 把每行切成 4096 元素一段的 tile，\( \lceil V/4096 \rceil \) 个 CTA 各自求出局部 (max, 全局下标)——每个 CTA 产出一对，这正是 partial 缓冲要装的东西；
- 每行再由一个 CTA 归约本行的所有 partial，得到最终 argmax。

于是对 `max_rows` 行的批，第一阶段最多同时产出 \( R \times \lceil V/4096 \rceil \) 对 partial——缓冲长度就定死为这个数。它是**上界也是精确值**：核的校验代码要求 partial 缓冲**至少**这么长（长了浪费、短了直接报错拒绝）。

一个值得注意的对偶：partials 里存的是 (f32 值, i32 全局下标)。用 f32 存局部最大值而不是 bf16，是因为归约阶段要做跨 tile 比较，加宽可以避免归约自身引入额外舍入；而「全局下标」保证了破并列次序与单块扫描**位级一致**（最低全局下标胜出）——这与 u2-l1 讲的「argmax 按最低下标破 bf16 并列」一脉相承。

#### 4.2.2 核心流程

\[ \text{partials}(R, V) = R \times \left\lceil \frac{V}{4096} \right\rceil \]

代入几个真实形状：

| R（行） | V（词表宽度） | \( \lceil V/4096 \rceil \) | partials 元素数 |
| --- | --- | --- | --- |
| 1 | 8 | 1 | 8 |
| 1 | 151,936 | 38 | 38 |
| 16 | 151,936 | 38 | 608 |
| 17 | 151,936 | 38 | 646 |

第三行是 4.1 表里那份数；第四行正是延迟探针测试（`select_batch_span17_latency_probe`，span-17）构造 scratch 时的形状。注意 partials 随行数**线性**增长、随词表宽度**阶梯式**增长（每跨过一个 4096 倍数加一档）。

#### 4.2.3 源码精读

公式本体住在 kernels crate（本 crate 通过 [src/lib.rs:L43](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L43) 引入它）：

[pegainfer-kernels/src/ops/sampling.rs:L368-L371](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/src/ops/sampling.rs#L368-L371) `argmax_batch_bf16_split_partials_len(rows, vocab) = rows * vocab.div_ceil(4096)`，`TILE_ELEMS = 4096` 就写在这一行旁边。紧挨着的 [pegainfer-kernels/src/ops/sampling.rs:L373-L382](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/src/ops/sampling.rs#L373-L382) 是一个很好的对照组：Markov-step argmax 的 tile 是 **1024** 元素（它的每行读取量小，需要 4 倍的块数才不致延迟受限），所以它有自己的 `markov_step_argmax_partials_len`——同一思想、不同 tile 常数、各自一个定长公式。

消费端怎么用这个公式做防御：

[pegainfer-kernels/src/ops/sampling.rs:L472-L480](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/src/ops/sampling.rs#L472-L480) `argmax_batch_bf16_split_indexed_into`（也就是 `select_batch` 贪心分支调的那个核入口）在 launch 前**重算一遍** `argmax_batch_bf16_split_partials_len(rows, logits.hidden_dim)` 并要求传入的 partial 缓冲不小于它，否则报错退出。也就是说：`SampleScratch` 按构造时的 `(max_rows, vocab)` 备好了上限，每次调用时核再用**本步实际的**行数与 arena 宽度复核一遍——一层保险在策略层（构造），一层在核层（校验）。

策略层的对应物就是 [src/lib.rs:L116](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L116) 那一行：构造时按上限算好、一次分配。而 `select_batch` 顶部的 [src/lib.rs:L217-L222](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L217-L222) 再保证「本步 arena 宽度 == scratch 记录的 `vocab`」——宽度一旦不一致（partials 尺寸推断就不可信了），直接拒绝。两个 crate 各守一半不变量。

#### 4.2.4 代码实践

**实践目标**：会用、会算、会核对这个定长公式，并理解「长度不足会被拒绝」的防御链。

**操作步骤**：

1. 手算 4.2.2 表中的四行，重点验算 \( \lceil 151936/4096 \rceil = 38 \)（提示：\( 4096 \times 37 = 151552 < 151936 \le 155648 = 4096 \times 38 \)）。
2. 阅读延迟探针 [tests/select_batch.rs:L377-L390](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/select_batch.rs#L377-L390)：它用 `rows = 17`、`vocab = 151_936` 构造 `SampleScratch::new`——代入公式应是 \( 17 \times 38 = 646 \)。
3. 做一个思想实验（无需 GPU）：假如某个模型把 arena 垫宽到 \( V' = 155648 \)（4096 的 38 倍），`max_rows = 16` 的 scratch 的 partials 长度是多少？和 \( V = 151936 \) 时相比变了吗？为什么？
4. （可选，需 GPU）在集成测试里故意构造一个 `vocab = 16` 的 arena，却传给按 `vocab = 8` 建的 scratch，观察 `select_batch` 在哪条 `ensure!` 上报错（预期是 [src/lib.rs:L218-L222](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L218-L222) 的宽度不匹配，而不是核层的 partials 校验——策略层先挡住了）。**待本地验证**。

**需要观察的现象 / 预期结果**：

- 第 1、2 步：`partials(1, 8) = 8`、`partials(17, 151936) = 646`。
- 第 3 步：\( \lceil 155648/4096 \rceil = 38 \)，与 \( \lceil 151936/4096 \rceil \) 相同——**只要垫宽没有跨过下一个 4096 倍数，partials 长度不变**。这说明了公式的「阶梯」性质：tile 数只在新跨过一个整 tile 时才 +1。151936 本身离 38 倍上限只差 384，垫到 155648 恰好落进同一档。

#### 4.2.5 小练习与答案

**练习 1**：为什么 partials 存「全局下标」而不是「tile 内局部下标」？如果存局部下标，第二阶段还需要什么额外信息？

<details><summary>参考答案</summary>

破并列规则是「最低**全局**下标胜出」。partial 若只存 tile 内局部下标，归约阶段还得知道每个 partial 来自哪个 tile 起点才能还原全局下标，且并列时的比较要额外计算。直接在 partial 里携带全局下标，归约就是单纯的 (值, 全局下标) 两两比较——文档（[pegainfer-kernels/src/ops/sampling.rs:L384-L392](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/src/ops/sampling.rs#L384-L392)）强调这使每行结果与退役的单块扫描「位级一致、且与其他行无关」。
</details>

**练习 2**：`SampleScratch` 构造时用 `(max_rows, vocab)` 算 partials，核调用时用 `(rows, hidden_dim)` 复核。两层校验会不会有一层是多余的？

<details><summary>参考答案</summary>

不多余，它们守的是不同的不变量。核层校验保证「这次调用的形状有足够缓冲」，对任何调用方（包括绕过 `SampleScratch` 直接调核的代码，比如 Kimi-K2）都成立；策略层的 `vocab` 记录（[src/lib.rs:L83-L85](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L83-L85)、[src/lib.rs:L217-L222](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L217-L222)）进一步保证「同一份长期持有的 scratch 只服务同一宽度的 arena」——这守护的是指针稳定与缓存推理（不同宽度意味着重新分配），是核层管不到的调用纪律。
</details>

### 4.3 selection_width 与 tile 对齐词表：#1046 的教训

#### 4.3.1 概念说明

u2-l1 建立了核坍缩判据：\( \text{top\_p} \le 1/V \) 时核只剩 argmax 一个 token，该行等价贪心、必须走确定性的 argmax 路径。这个 \( V \) 是**可发射 token 的个数**。

麻烦在于：模型可能为了让 logits GEMM 跑在 tile 倍数宽度上，把 arena 从 \( V \)（真实词表）垫宽到 \( W > V \)。填充列不是可发射 token，但它们物理上躺在 arena 里，`logits.hidden_dim` 读出来是 \( W \)。如果路由判定错用了 \( W \)：

\[ \frac{1}{W} < \frac{1}{V} \quad\Longrightarrow\quad \exists\, \text{top\_p}:\ \frac{1}{W} < \text{top\_p} \le \frac{1}{V} \]

也就是存在一族 `top_p` 取值——在真实词表上本应坍缩成单 token 核、走 argmax——却因为分母被填充列撑大而不满足判据，被错路由到拒绝采样器。后果正是 u2-l1 分析过的：遇到 bf16 并列最大值时，采样器会随种子随机挑一个并列者，**一个本应确定的请求变成了随机的**。这就是 qwen35 tile 对齐引发 #1046 的机制。

修复是把两个宽度显式分开：

- `vocab`（= arena 物理宽度 \( W \)）：管**形状**——所有缓冲按它分配，argmax 核扫满整行，`select_batch` 校验 arena 宽度与它一致；
- `selection_width`（= 可发射宽度 \( V \)）：只管**路由**——`effectively_greedy` 的分母用它。

测试 `padded_arena_width_does_not_suppress_the_argmax_routing` 的注释（[tests/select_batch.rs:L182-L184](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/select_batch.rs#L182-L184)）把这个场景总结成一句话：在未垫宽的 arena 上坍缩为 argmax 的 top_p，垫宽一倍后必须**仍然**坍缩。

#### 4.3.2 核心流程

一次 `select_batch` 调用中两个宽度的分工：

```text
arena (物理宽度 W = logits.hidden_dim, 含 tile 填充列)
  │
  ├─ 形状校验:  W == scratch.vocab            # 不相等立即报错
  ├─ argmax 核: 扫满 W 列                      # partials、核校验都用 W
  ├─ 采样器:    读满 W 列的整行               # BatchSamplingScratch 按 W 分配
  │
  └─ 路由判定:  effectively_greedy(p, scratch.selection_width)   # 只用 V
                  └─ top_p <= 1/V ?  → argmax 路径（确定性破并列）
                                    否   → 采样路径
```

构造侧的对应选择：

```text
模型未垫宽 (W == V):        SampleScratch::new(ctx, V, max_rows)          # 默认 selection_width = vocab
模型垫宽   (W >  V):        SampleScratch::with_selection_width(ctx, W, V, max_rows)
                            # 第 2 参数管形状，第 3 参数管路由
```

#### 4.3.3 源码精读

构造函数的文档注释把动机写得非常直白：

[src/lib.rs:L97-L101](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L97-L101) 「For an arena spanning `vocab` columns whose last `vocab - selection_width` columns the model cannot emit」——arena 有 `vocab` 列，但末尾 `vocab - selection_width` 列不可发射（模型为了凑 GEMM tile 倍数垫的宽）。这些填充列**不得**撑大 `top_p <= 1/vocab` 核判定的分母，否则本应等价贪心的请求会跌进拒绝采样器、在 bf16 并列最大值上随机化。配套校验 [src/lib.rs:L112-L115](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L112-L115) 把合法域定死为 `selection_width ∈ 1..=vocab`。

路由点——两个宽度在 `select_batch` 里各就各位：

[src/lib.rs:L217-L225](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L217-L225) 先取 `vocab = logits.hidden_dim` 并校验它等于 `scratch.vocab`（形状一致性），随后注释「Pad columns a model aligned its GEMM to are not emittable tokens, so they must not move the `top_p <= 1/vocab` nucleus」，路由闭包 `is_argmax` 调 `effectively_greedy(p, scratch.selection_width)`——分母是 `selection_width`，不是上一行的 `vocab`。两个宽度在三行代码里擦肩而过、各司其职。

谓词本体（u2-l1 已精读，这里只看分母参数）：

[src/lib.rs:L455-L461](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L455-L461) `effectively_greedy(params, vocab_size)` 的第二参数语义是「参与采样的 token 数」，与 arena 物理宽度无关。注意它同时被自驱贪心路径的模型复用（文档说明），所以这个参数语义必须模型无关——「可发射词表」正是唯一模型无关的正确语义。

回归测试是本讲最好的教材——同一 arena、同一参数，两种构造方式、两种命运：

[tests/select_batch.rs:L180-L219](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/select_batch.rs#L180-L219) `padded_arena_width_does_not_suppress_the_argmax_routing`。场景构造：`decodable = 256`，arena 宽 `vocab = 512`（2 倍垫宽），行内在 `lo = 128` 与 `hi = 200` 放两个 **bf16 完全相同**的 8.0 峰（真并列），其余列是 -1.0 地板（填充列不竞争 argmax）。参数 `tiny = top_p 1/256`——恰好在真实词表上坍缩。

- **对照组**（[tests/select_batch.rs:L198-L209](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/select_batch.rs#L198-L209)）：用 `SampleScratch::new(ctx, 512, 1)`（`selection_width` = 512）。此时 \( 1/256 \le 1/512 \) 不成立，行**不走** argmax；64 个种子下断言能取到并列者 `hi`——证明这行真的落在了采样器上（分母被填充列撑大压垮了判据）。
- **修复组**（[tests/select_batch.rs:L211-L218](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/select_batch.rs#L211-L218)）：用 `with_selection_width(&ctx, 512, 256, 1)`。判据分母回到 256，\( 1/256 \le 1/256 \) 成立，行路由到 argmax；64 个种子全部返回 `lo`（argmax 按最低下标破并列）——确定性恢复。

对照组的巧妙之处值得多说一句：它不是「多余的负面测试」，而是先**证明 bug 现象真实存在**（宽度用错时行为确实变了），再证明修复有效。没有对照组，即使修复组通过，也无法排除「这个 top_p 本来就不会采到并列者」的侥幸解释。

#### 4.3.4 代码实践

**实践目标**：通过一个「故意用错构造函数」的思想实验，确认你理解了 `selection_width` 唯一的职责是路由分母。

**操作步骤**：

1. 通读 [tests/select_batch.rs:L180-L219](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/select_batch.rs#L180-L219)，标出：`decodable`、`vocab`、`lo`、`hi`、`tiny` 各是什么，对照组与修复组**唯一的差别**是哪一个构造调用。
2. 思想实验：把修复组的 [tests/select_batch.rs:L211](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/select_batch.rs#L211) `SampleScratch::with_selection_width(&ctx, vocab, decodable, 1)` 改成 `SampleScratch::new(&ctx, vocab, 1)`（即 `selection_width` 变为 512），测试的哪一条断言会失败？为什么？
3. 写出不超过 200 字的分析（这是本讲的落笔作业，参考答案见下）。

**需要观察的现象 / 预期结果**（参考答案，约 190 字）：

> 失败的是修复组循环内的 `assert_eq!(select_batch(...), vec![lo], "seed {s}: the decodable vocab must keep this row on the argmax path")`（tests/select_batch.rs:213-217）。原因：`new` 把 `selection_width` 设成 512，`is_argmax` 里 `top_p = 1/256 <= 1/512` 不再成立，`effectively_greedy` 判负，该行被路由到拒绝采样器；而 `lo` 与 `hi` 是两个 bf16 完全相同的 8.0 峰，采样器随种子二选一，64 个种子里几乎必然出现一次 `hi`（=200），与断言的 `lo`（=128）相撞。这正是对照组已演示过的行为——改回去之后，修复组退化成了对照组。

注意一个细节：失败的是**修复组**的断言，而不是对照组的 `assert!(sampled_the_peer)`——对照组本来就期待采样器行为，`selection_width` 的错误使用恰好让它「更正确」；被破坏的只有「坍缩行必须走 argmax」这条语义。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `with_selection_width` 的校验是 `selection_width <= vocab`，而不是 `selection_width < vocab`？`selection_width == vocab`（即 `new` 的默认情形）合法吗？

<details><summary>参考答案</summary>

合法，且正是 `new` 的实现方式（[src/lib.rs:L92-L95](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L92-L95) 委托 `with_selection_width(ctx, vocab, vocab, max_rows)`）。约束的方向是有意义的：可发射宽度**最多**等于物理宽度（垫宽只能造出不可发射列，不可能凭空多出可发射列），且必须至少为 1。`< ` 会把「未垫宽」这个最常见情形判为非法。
</details>

**练习 2**：填充列的 logit 值会影响 argmax 结果吗？测试是怎么保证不会的？真实模型里靠什么？

<details><summary>参考答案</summary>

会影响——argmax 核扫的是**整行** \( W \) 列（形状校验与 partials 都按 \( W \)），填充列若出现比真实 token 更大的值就会被选中。测试用 `-1.0` 地板填充（[tests/select_batch.rs:L190-L192](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/select_batch.rs#L190-L192)），远低于 8.0 峰，保证填充列不竞争。真实模型里垫宽列的值由模型 crate 的 GEMM 与填充策略负责（例如填充极小值），这属于模型 crate 与 `selection_width` 约定好的契约分工：本 crate 保证「路由只数可发射列」，模型 crate 保证「填充列不赢得 argmax」。**真实填充策略待确认**（不在本 crate 源码内）。
</details>

**练习 3**：`BatchSamplingScratch::new(ctx, max_rows, vocab)` 的第三参数为什么传 arena 宽度 \( W \) 而不是 \( V \)（[src/lib.rs:L146](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L146)）？

<details><summary>参考答案</summary>

采样器读的是**整行** logits（`gpu_sample_batch_into` 收到完整 arena），概率/softmax workspace 缓冲必须按物理宽度 \( W \) 备；`selection_width` 只出现在路由谓词的分母里，不进入任何缓冲尺寸。反过来说：如果一个模型垫宽了却把 \( V \) 传给了采样 scratch，采样通道会在读整行时缓冲不足。填充列对采样器同样「不可发射」——它们在 softmax 里贡献概率却被裁剪掉，这个语义由采样核侧保证，与路由无关。
</details>

## 5. 综合实践

**任务：为两个假想模型规划 scratch 并算清账。**

设定两个模型，都要建 `max_rows` 容量的 `SampleScratch`：

- **模型甲**：词表 \( V = 151936 \)，不做 tile 对齐（arena 宽 = 词表宽），`max_rows = 16`。
- **模型乙**：可发射词表 \( V = 151936 \)，但 logits GEMM 垫宽到下一个 4096 倍数 \( W = 155648 \)，`max_rows = 16`。

请完成：

1. **写出两个构造调用**（Rust 代码，区分用 `new` 还是用 `with_selection_width`）。
2. **分别计算**：贪心侧 partials 长度、采样侧 `probs` 元素数与字节数、softmax workspace 字节数。注意哪些量变了、哪些没变，并解释原因（提示：\( \lceil 151936/4096 \rceil = \lceil 155648/4096 \rceil = 38 \)，同一阶梯档位）。
3. **语义检查**：模型乙如果错用模型甲的构造方式（`new(ctx, 155648, 16)`），会先在哪一步被拒绝？（答案：**不会**被拒绝——`new` 完全合法，`selection_width` 静默变成 155648；错误只在运行时表现为：`top_p` 落在 \( (1/155648,\, 1/151936] \) 区间的请求被错路由到采样器，在 bf16 并列最大值上随机化。这是一个**不报错的语义 bug**，正是 #1046 的形态，也是为什么回归测试要存在。）
4. **（需 GPU，可选）**：仿照 [tests/select_batch.rs:L180-L219](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/select_batch.rs#L180-L219) 写一个新测试：`decodable = 128`，arena 宽 512，在可发射区放一对并列 8.0 峰，`top_p = 1/128`，断言 `with_selection_width(ctx, 512, 128, 1)` 下 64 个种子全取低位峰。**待本地验证**。

**参考答案要点**（第 1、2 步）：

```rust
// 示例代码
// 模型甲：W == V，直接 new
let a = SampleScratch::new(&ctx, 151_936, 16)?;
// 模型乙：W > V，必须显式给出可发射宽度
let b = SampleScratch::with_selection_width(&ctx, 155_648, 151_936, 16)?;
```

| 量 | 公式 | 模型甲 | 模型乙 |
| --- | --- | --- | --- |
| partials | \( 16 \times \lceil W/4096 \rceil \) | 16×38 = 608 | 16×38 = 608（同一档，**不变**） |
| `probs` 元素 | `max_rows × W` | 2,430,936 | 2,490,368（**变大** ~238K 个 f32） |
| `probs` 字节 | ×4 | ≈9.27 MiB | ≈9.50 MiB |
| softmax workspace | \( 16 \times \lceil W/8192 \rceil \times 8 + 256 \) | 16×19×8+256 = 2688 | 16×19×8+256 = 2688（\( \lceil 155648/8192 \rceil = 19 \)，同一档，**不变**） |

结论：垫宽跨过 4096/8192 档位之前，只有按 \( W \) 线性计费的 `probs` 变大，阶梯式计费的 partials 与 workspace 都不动；而路由分母必须由 `with_selection_width` 手工拨回 \( V \)，否则就是一个不报错、只在高 `top_p` 精度区间露馅的语义 bug。

## 6. 本讲小结

- `SampleScratch` 是**一次分配、长期持有**的解码路径缓冲集合：解码路径要求指针稳定（CUDA 图烘焙地址、每步零分配），所以形状在构造时按 `(max_rows, vocab)` 烘焙，之后只写不配。
- 逐字段分工：贪心链是 `row_indices`（行号表）→ `argmax_partial_values/_indices`（两阶段 argmax 的 tile 局部结果）→ `top1_values`（核的必要出参，本 crate 不读）→ `argmax_out` → 两个 **pinned** 交替落地区；`identity_rows` 是常量恒等表，让全贪心分阶段路径复用同一个 indexed argmax 核；非贪心链整体住进内嵌的 `BatchSamplingScratch`。
- partials 长度是推导出来的定值：\( R \times \lceil V/4096 \rceil \)，随行数线性、随词表宽度阶梯式增长；kernels 侧在每次 launch 前重算复核，策略层用 `scratch.vocab` 保证 arena 宽度一致。
- `vocab`（arena 物理宽度）与 `selection_width`（可发射宽度）必须分开：前者管所有形状与缓冲，后者只当 `effectively_greedy` 的分母。tile 垫宽列不可发射，若撑大分母会把本应坍缩为 argmax 的 `top_p` 行错推进拒绝采样器，在 bf16 并列最大值上随机化（#1046，回归测试 `padded_arena_width_does_not_suppress_the_argmax_routing`）。
- 贪心与非贪心缓冲天然不相交且两分支顺序执行，所以**一份** scratch 就能服务贪心/采样混合批；显存大头是非贪心侧的 `probs`（`max_rows × vocab` 的 f32），贪心侧全套只有几 KB。

## 7. 下一步学习建议

本讲把「路由判定住哪、写到哪」的物质基础讲完了，下一讲 **u2-l3（select_batch 三路分发）**将沿着这些缓冲走完主链路的全程：贪心行合并成一次批式 indexed argmax、无种子采样行合并成一次 FlashInfer 批式调用、带种子行逐行单调用的完整流程，以及函数顶部一连串 `ensure!` 各自守护的不变量（本讲已预习了其中两条：容量与宽度）。

延伸阅读：

- `pegainfer-kernels/src/ops/sampling.rs` 中 `argmax_bf16_split_into` / `argmax_batch_bf16_split_indexed_into` 的实现与注释——partials 校验、破并列次序的位级一致性保证。
- `BatchSamplingScratch` 的 OnlineSoftmax workspace 公式（`max_rows * ⌈vocab/8192⌉ * 8 + 256`）——另一个「阶梯式定长公式」的实例，可与本讲 4096 版对照。
- 预习 u4-l1（分阶段贪心路径）：`identity_rows`、`argmax_host` / `argmax_host_alt` 两个交替槽在 CUDA 图捕获下如何被 `greedy_stage_readback` / `greedy_collect_resident` 使用；以及 u4-l2（#704）：pinned 落地区为什么必须是 write-combined 固定页。
