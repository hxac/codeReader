# rms_matvec_pipeline 与 matvec / rms_norm 设备函数

## 1. 本讲目标

在 [u8-l2](u8-l2-matvec-pipeline.md) 里，我们把 `matvec_pipeline` 的「load / compute / store 三条异步流水线 + 跨指令 page 轮转」彻底拆透。但真实的 Llama 推理 op，几乎都不是「拿到激活就直接做 matvec」——它要先对激活做一次 **RMSNorm**（归一化 + 乘以可学习缩放），再把归一化后的向量喂进 matvec。本讲就打开 `matvec_pipeline` 的子模板 `rms_matvec_pipeline`，以及它真正干活的三个**设备函数**：`rms_norm`、`matvec`、`matvec_reduce`。

具体来说，学完本讲你应该能说清楚：

1. `rms_matvec_pipeline` 在父模板 `matvec_pipeline` 之上**额外加了哪几样东西**：一个异步加载的 `rms_scale_arrived` 信号量、一次 `rms_norm` 预处理、一个（仅 Blackwell 用的）`launcher_loop`；以及为什么 RMSNorm 的输入缩放向量要和激活向量**共用同一个物理页**（activation page）。
2. `rms_norm` 设备函数为什么是**两阶段归约**：先在每个 warp 内部做寄存器级（warp shuffle）归约得到 16 个 partial sum，再通过共享内存做跨 warp 求和得到全 2048 维的方差。
3. `matvec` 设备函数在 **Hopper** 和 **Blackwell** 上的两种实现为什么长得完全不同：Hopper 用 FP32 寄存器 tile 的**逐元素乘 + 行求和**（`warp::mul` + `row_sum`），Blackwell 用 bf16 tile 的 **tensor core `mma_ABt` + `row_max`** 列折叠。理解这两条分支「为什么连最后的折叠算子都不一样（`row_sum` vs `row_max`）」。
4. `matvec_reduce` 如何把 16 个 consumer warp 各自算出的 **16 元素部分和**累加成最终的 16 元素输出——也就是「split-K matvec」的跨 warp 收尾。

本讲是「读懂流水线骨架」到「读懂骨架里真正在算什么」的跨越。`rms_matvec_pipeline` 是 Llama 里 `rms_qkv_rope_append`、`rms_double_matvec_silu` 等 op 的共同基类；而 `utils.cuh` 里的三个设备函数，是这些 op 在 tensor core 上**实际执行**的计算。

## 2. 前置知识

本讲是 advanced 阶段，默认你已经掌握以下内容（否则建议先读对应讲义）：

- **matvec_pipeline 的三流水线骨架**（[u8-l2](u8-l2-matvec-pipeline.md)）：`loader_loop` / `consumer_loop` / `storer_loop` 三个异步循环，靠信号量而非 `__syncthreads` 握手；输出放在 scratch（不是 page）里做 3 级缓冲；`release_lid` 在指令之间轮转 13 个物理页。本讲的 `rms_matvec_pipeline` **继承**它，你只需要理解「子模板在父模板之上加了什么」。
- **信号量约定**（[u8-l2 §4.6](u8-l2-matvec-pipeline.md)）：`*_arrived` = 数据就绪、`*_finished` = 可复用；**init 计数 = 生产者数量**；配对的 arrived/finished 用互补相位位。本讲会看到子模板如何「追加」一个信号量。
- **op 的五子结构与回调**（[u8-l1](u8-l1-op-interface-noop-reference.md)）：op 提供 controller / loader / launcher / consumer / storer 五个子结构。`rms_matvec_pipeline` 把这些都实现了，宿主 op（如 `rms_qkv_rope_append`）只需转发 `::run`，并补充 `pipeline_specifics`（`load_iter` / `store` / `gmem_wait`）回调。
- **RMSNorm 的数学定义**：给定向量 \(x \in \mathbb{R}^{d}\) 与可学习缩放 \(g \in \mathbb{R}^{d}\)，输出

\[
\mathrm{RMSNorm}(x) = \frac{x}{\sqrt{\frac{1}{d}\sum_{i} x_i^2 + \varepsilon}} \odot g = x \cdot \mathrm{rsqrt}\!\left(\tfrac{1}{d}\textstyle\sum_i x_i^2 + \varepsilon\right) \odot g
\]

  其中 \(\odot\) 是逐元素乘。注意 RMSNorm **没有减均值**（区别于 LayerNorm），只需算「平方和的均值」开方。本讲的难点不是这个公式，而是「2048 维向量的平方和怎么在 16 个 warp 之间高效求和」。

- **kittens 寄存器 tile / 向量抽象**：`st_bf<R,C>`（共享内存 bf16 tile）、`rt_bf<R,C>` / `rt_fl<R,C>`（寄存器 bf16/float tile）、`rv_fl<N>`（寄存器 float 向量）、`sv_bf<N>` / `sv_fl<N>`（共享内存向量）。`warp::load/store/mul/add/copy/zero` 是作用在寄存器对象上的「逐 warp」操作（32 个 lane 协作）；`kittens::group<N>::sync(...)` 是「N 个 warp 之间的」同步。

一句话心智模型：**`rms_matvec_pipeline` = 父模板 matvec_pipeline + 「consumer 开算前先做一次 RMSNorm」**；而 RMSNorm 与 matvec 的计算量，被拆成了「16 个 warp 各算自己那 1/16 的维度，再跨 warp 收拢」的 split-K 结构——`rms_norm` 收拢平方和，`matvec_reduce` 收拢部分积。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [demos/low-latency-llama/matvec_pipeline.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh) | **本讲主角之一**。L7-L244 是父模板 `matvec_pipeline`（[u8-l2](u8-l2-matvec-pipeline.md) 已讲）；**L246-L349 是本讲重点的 `rms_matvec_pipeline` 子模板**：追加 `rms_scale_arrived` 信号量、重写 `loader_loop` / `consumer_loop`、新增 `launcher_loop`。 |
| [demos/low-latency-llama/utils.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/utils.cuh) | **本讲主角之二**。三个设备函数：`rms_norm`（L5-L37，两阶段归约）、`matvec`（L39-L101，Hopper 与 Blackwell 两条分支）、`matvec_reduce`（L103-L120，跨 warp 归约）。 |
| [demos/low-latency-llama/rms_matvec_rope_append.cu](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/rms_matvec_rope_append.cu) | 真实宿主 op `rms_qkv_rope_append`。本讲用它说明 `rms_matvec_pipeline` 如何被继承（L170-L173）、`matvec_reduce` 如何在 storer 的 `store` 回调里被调用（L90-L92）。 |
| [demos/low-latency-llama/llama.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cuh) | `globals_t` 定义 `hidden_dim=2048`（L67）、`rms_norm_eps`（L141）、各权重指针（如 `attn_norm_weights` L114）。这些是 RMSNorm 的输入。 |

## 4. 核心概念与源码讲解

### 4.1 rms_matvec_pipeline：在父模板之上加一层 RMSNorm

#### 4.1.1 概念说明

`rms_matvec_pipeline` 是一个**模板子类**，它 public 继承 `matvec_pipeline`，并多了两个模板参数 `ActPtr` 和 `RmsPtr`——它们是两个**指向成员的指针（pointer-to-member）**，分别指明「激活向量在 `globals` 里的字段」和「RMSNorm 缩放权重在 `globals` 里的字段」。看 [demos/low-latency-llama/matvec_pipeline.cuh:246-252](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh#L246-L252)：

```cpp
template <typename Config, typename Globals, typename parsed_instruction,
          typename pipeline_specifics, auto ActPtr, auto RmsPtr>
struct rms_matvec_pipeline
    : public matvec_pipeline<Config, Globals, parsed_instruction, pipeline_specifics> {
    using pipeline = matvec_pipeline<Config, Globals, parsed_instruction, pipeline_specifics>;
```

为什么要用「指向成员的指针」而不是把指针值传进来？因为 `ActPtr`/`RmsPtr` 是编译期常量（如 `&Globals::hidden_states`），把它做成模板参数后，编译器能把 `g.*ActPtr` 这种成员访问完全内联、零间接寻址开销。宿主 op `rms_qkv_rope_append` 在 [rms_matvec_rope_append.cu:170-173](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/rms_matvec_rope_append.cu#L170-L173) 正是这样实例化的：

```cpp
using pipeline = rms_matvec_pipeline<Config, Globals, parsed_instruction,
                    pipeline_specifics, &Globals::hidden_states,
                    &Globals::attn_norm_weights>;
```

即「激活 = `hidden_states`，RMSNorm 缩放 = `attn_norm_weights`」。

子模板相对父模板，一共做了四件事：

1. **多一个信号量 `rms_scale_arrived`**（异步加载缩放向量用）。
2. **重写 `loader_loop`**：在父类加载权重之前，先用 TMA 异步把 `rms_scale` 拉进 activation page 的「后半段」。
3. **新增 `launcher_loop`**：仅 Blackwell 需要，等 tensor core 描述符就绪。
4. **重写 `consumer_loop`**：16 个 warp 先各自加载自己的激活切片 → 等 `rms_scale` 到位 → 调 `rms_norm` 算出归一化向量 → 再把归一化向量交给父类的 `consumer_loop` 跑 matvec。

注意一个关键的复用：**激活向量和 rms_scale 共用同一个物理页（activation page, lid 0）**。父类的 `get_activations` 返回 activation page 开头的 `sv_bf<hidden_dim>`（[matvec_pipeline.cuh:59-63](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh#L59-L63)）；子类的 `get_rms_scale` 则返回**同一页、偏移一个 `sv_bf<hidden_dim>` 之后**的那段（见 4.1.3）。这样 RMSNorm 的两份数据（待归一化激活、可学习缩放）只需一个物理页，省 shared memory。

#### 4.1.2 核心流程

`rms_matvec_pipeline` 把一条指令的生命周期排成下面这样（只画子模板**新增**的部分，其余沿用父模板）：

```
loader_loop (新增前缀，lane 0 干活):
    wait_page_ready(activation_page)          # 等激活页可用
    TMA 异步加载 rms_scale → activation_page 的后半段
    arrive(rms_scale_arrived)                  # 通知 consumer：缩放向量好了
    [然后转父类 loader_loop，正常加载权重]      # 见 u8-l2 §4.3

consumer_loop (16 warp):
    各 warp 取自己的 128 元激活切片 & 128 元 rms_scale 切片
    group<16>::sync                            # 全体 consumer warp 同步
    各 warp 从 gmem 直接 load 自己的激活切片 (warp::load)
    wait(rms_scale_arrived, 0)                 # 等 loader 把缩放向量搬好
    activations_vec = rms_norm(rms_scale_slice, activation_slice,
                               rms_norm_eps, scratch)   # 归一化，见 4.2
    warp::sync; warp_finish_page(activation_page, 1)    # 激活页用完，放手
    pipeline::consumer_loop(s, g, activations_vec)       # 父类跑 matvec 流水线，见 u8-l2 §4.4

launcher_loop (仅 Blackwell):
    wait_tensor_ready; arrive(tensor_finished, NUM_CONSUMER_WARPS)
```

三条新增逻辑合起来就是「**在 matvec 开跑前，先把激活异步归一化好**」。`rms_scale` 由 loader 异步加载（不阻塞权重加载），consumer 在调 `rms_norm` 前才 `wait` 它——这正是「用信号量让加载和计算重叠」的一贯套路（和 [u8-l2](u8-l2-matvec-pipeline.md) 的 `weights_arrived` 同理）。

#### 4.1.3 源码精读

**(a) 多出的信号量与缩放向量位置。** `SEM_COUNT` 比父类多 1（[matvec_pipeline.cuh:257](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh#L257)），新增的 `rms_scale_arrived` 占用 `semaphores()[pipeline::SEM_COUNT]`（[matvec_pipeline.cuh:259-261](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh#L259-L261)），紧跟父类那 13 个之后。`get_rms_scale` 用 `ptr(sizeof(sv_bf<hidden_dim>))` 取 activation page 内偏移后的地址（[matvec_pipeline.cuh:263-268](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh#L263-L268)），印证「激活与缩放共用 lid 0 页」。

`init_semaphores` 先调父类初始化 13 个，再追加 `rms_scale_arrived`（[matvec_pipeline.cuh:270-274](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh#L270-L274)）：

```cpp
pipeline::init_semaphores(s);
init_semaphore(rms_scale_arrived(s), 1);   // loader 单生产者 → init 1
return SEM_COUNT;                          // 14
```

init=1 仍遵守 [u8-l2 §4.6](u8-l2-matvec-pipeline.md) 的铁律：`rms_scale_arrived` 的生产者是 loader（单 warp，TMA 载入完成 arrive 1 次），所以 init 为 1。

**(b) 异步加载缩放向量。** 子模板的 `loader_loop` 在父类加载之前，先做一段 lane-0 专属逻辑（[matvec_pipeline.cuh:276-291](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh#L276-L291)）：

```cpp
if (kittens::laneid() == 0) {
    int activation_page = get_activation_page(s);
    s.wait_page_ready(activation_page);
    auto &rms_scale = get_rms_scale(s);
    auto &sem = rms_scale_arrived(s);
    kittens::tma::expect(sem, rms_scale);                                 // 预告 TMA
    kittens::tma::load_async<kittens::cache_policy::EVICT_LAST>(          // 异步载入
        rms_scale, g.*RmsPtr, {layer_idx, 0}, sem);
}
pipeline::loader_loop(s, g);   // 转父类，加载 3 级权重
```

注意 `g.*RmsPtr` 就是用模板参数成员指针取出 `attn_norm_weights`，`{layer_idx, 0}` 选第 `layer_idx` 层的归一化权重。`tma::expect` + `tma::load_async` + 信号量 arrive 是标准异步 TMA 三件套（[u8-l2 §4.3](u8-l2-matvec-pipeline.md) 同款）。

**(c) consumer 里先做 RMSNorm。** 这是子模板最核心的新逻辑（[matvec_pipeline.cuh:303-348](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh#L303-L348)）。先用指针重解释，把整段 activation / rms_scale 各自切成 16 个 128 元素切片，每个 warp 拿自己 `warpid()` 那一片（[matvec_pipeline.cuh:306-310](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh#L306-L310)）：

```cpp
using sv_t = kittens::sv_bf<REDUCTION_DIM_PER_WARP>;          // sv_bf<128>
auto &rms_scale_smem   = reinterpret_cast<sv_t*>(&get_rms_scale(s))[kittens::warpid()];
auto &activations_smem = reinterpret_cast<sv_t*>(&get_activations(s))[kittens::warpid()];
```

`REDUCTION_DIM_PER_WARP = hidden_dim / NUM_CONSUMER_WARPS = 2048/16 = 128`（[matvec_pipeline.cuh:254-255](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh#L254-L255)）。接下来是一个**只为 gmem 同步**的小块（[matvec_pipeline.cuh:312-332](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh#L312-L332)）：warp0/lane0 调宿主回调 `pipeline_specifics::gmem_wait`，确保上一层的残差已经写回 gmem（避免读到没算完的激活），然后 `group<16>::sync` 让所有 consumer warp 汇合。

归一化本体三行（[matvec_pipeline.cuh:334-342](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh#L334-L342)）：

```cpp
kittens::warp::load(activations_smem_vec_equivalent, g.*ActPtr, {kittens::warpid()});  // 从 gmem 取激活切片
...
kittens::wait(rms_scale_arrived(s), 0);          // 等缩放向量到位
auto activations_vec = rms_norm<Config>(rms_scale_smem, activations_smem,
        g.rms_norm_eps,
        pipeline::get_output_start(s, pipeline::OUTPUT_PIPELINE_STAGES));   // 见 4.2
```

最后 `warp::sync` + `warp_finish_page(activation_page, 1)` 放手激活页（[matvec_pipeline.cuh:344-345](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh#L344-L345)），再把归一化向量 `activations_vec` 喂给父类 `consumer_loop`（[matvec_pipeline.cuh:347](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh#L347)）。注意父类 `consumer_loop` 是**模板函数**，参数 `activations_vec` 由引用传入（[matvec_pipeline.cuh:162-164](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh#L162-L164)）——子模板正是利用这个「注入激活向量」的钩子，把 RMSNorm 的产物无缝接进 matvec 流水线。

> 小细节：`rms_norm` 的 `scratch_memory` 实参用了 `get_output_start(s, OUTPUT_PIPELINE_STAGES)`，即输出 scratch 区**第 4 段（下标 3）**的起始地址。三个输出级占 `[0, 3·SCRATCH_BYTES_PER_STAGE)`，RMSNorm 的临时 partial sum 借用紧随其后那段、且只在 consumer 进 matvec 流水线**之前**用一次，因此不会和输出缓冲冲突。

#### 4.1.4 代码实践

**实践目标**：确认「子模板只新增、不改坏父模板」——把 `rms_matvec_pipeline` 相对 `matvec_pipeline` 的「增量」逐一列出来。

**操作步骤**：

1. 打开 [demos/low-latency-llama/matvec_pipeline.cuh:246-349](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh#L246-L349)。
2. 对比父类（[matvec_pipeline.cuh:7-244](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh#L7-L244)），列出子类**重写或新增**的成员：`SEM_COUNT`、`rms_scale_arrived`、`get_rms_scale`、`init_semaphores`、`loader_loop`、`launcher_loop`、`consumer_loop`。
3. 找出子类**没有重写**、直接继承的成员（例如 `get_weight_page`、`release_lid`、`storer_loop`），确认它们对 RMSNorm 流程依然成立。

**需要观察的现象**：`release_lid`、`storer_loop`、三级权重缓冲等**完全不变**——RMSNorm 只动了「consumer 开跑前」的那一小段和「多加载一个缩放向量」，matvec 流水线的主体原封不动复用。这正是「模板骨架 + 子类钩子」设计的价值。

**预期结果**：你能用一句话概括「`rms_matvec_pipeline` = `matvec_pipeline` + 一个异步 rms_scale 信号量 + consumer 开算前的 rms_norm 预处理」。**待本地验证**：若想确认激活页释放时序，可在 `warp_finish_page(activation_page, 1)` 前后临时打印（仅调试），观察它在父类 `consumer_loop` 进入第一次 `matvec` 之前完成。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `rms_scale` 和激活向量共用 lid 0 页，而不是各占一页？

**答案**：两者都是「一条指令内、consumer 开算前一次性消费」的 2048 维向量，且 rms_norm 一旦算完、激活页就被 `warp_finish_page` 放手。把它们放同一页（激活在前、缩放在后）能省下一个物理页，而 `NUM_PAGES==13` 是硬约束（[u8-l2 §4.1](u8-l2-matvec-pipeline.md)），能省则省。代价是必须保证两段不重叠：`get_rms_scale` 用 `ptr(sizeof(sv_bf<hidden_dim>))` 跳过激活段（[matvec_pipeline.cuh:266-267](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh#L266-L267)）。

**练习 2**：`launcher_loop` 里为什么有一段 `#ifdef KITTENS_BLACKWELL` 包起来的 `wait_tensor_ready`？

**答案**：Blackwell 的 tensor core 操作依赖一个显式构造的 **tensor 描述符（descriptor）**，必须等它就绪后 consumer warp 才能 issue mma。`launcher_loop` 在 Blackwell 上 `wait_tensor_ready()` 再 `arrive(tensor_finished, NUM_CONSUMER_WARPS)`，让 consumer 在跑 matvec（用到 mma_ABt）前确认描述符已好。Hopper 没有这套机制，所以该分支为空。这与 4.3 节「Blackwell 用 mma_ABt」直接相关。

### 4.2 rms_norm 设备函数：为什么要两阶段归约

#### 4.2.1 概念说明

`rms_norm` 是 `utils.cuh` 里的设备函数（[utils.cuh:5-37](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/utils.cuh#L5-L37)）。它由每个 consumer warp 各调一次，输入是**本 warp 的 128 元切片**（`rms_scale_smem` 与 `activations_smem` 都是 `sv_bf<REDUCTION_DIM_PER_WARP>`），输出是归一化后的 128 元寄存器向量。

难点在于 RMSNorm 公式里的分母 \(\sqrt{\frac{1}{d}\sum_i x_i^2}\) 需要**全 2048 维**的平方和，但单个 warp 手里只有其中 128 维。所以必须先「局部求和」再「全局汇总」——这就是**两阶段归约**的由来：

- **阶段 1（warp 内，寄存器级）**：每个 warp 对自己 128 维算 \(p_w = \sum_{i \in \text{warp}} x_i^2\)，得到一个标量。
- **阶段 2（跨 warp，共享内存级）**：16 个 warp 把各自的 \(p_w\) 写进共享内存，全员读出 16 个 partial sum 求和，得到全 2048 维的 \(\sum_i x_i^2\)。

这是经典的「**层次化归约（hierarchical reduction）**」：先用 warp shuffle（`warp::sum`，零额外存储）消化掉一部分，再用一次共享内存往返处理 warp 之间的剩余。它比「所有元素直接写共享内存再求和」省带宽、省同步。

#### 4.2.2 核心流程

```
# —— 阶段 1：warp 内平方和（每 warp 一个标量）——
load activations_vec  (128 元, 寄存器)
sq_activations_vec = activations_vec ⊙ activations_vec        # 逐元素平方
partial_sum = warp::sum(sq_activations_vec)                    # 寄存器内 shuffle 归约 → 1 个 float/warp

# —— 阶段 2：跨 warp 汇总（经共享内存）——
if laneid == 0: smem_rms_partial_sums[warpid()] = partial_sum  # 16 个 warp 各写一格
group<NUM_CONSUMER_WARPS>::sync                                # 等 16 个 warp 都写完
full_sum = Σ_{i=0..15} smem_rms_partial_sums[i]               # 每 thread 各自读 16 格求和

# —— 算缩放因子并归一化 ——
variance   = full_sum / 2048.0f                                # 注意硬编码的 2048
rms_factor = rsqrtf(variance + rms_norm_eps)
activations_vec = activations_vec ⊙ rms_factor                 # 除以 RMS
rms_scale_vec   = load(rms_scale_smem)                         # 可学习缩放 g
activations_vec = activations_vec ⊙ rms_scale_vec              # 乘 g
return activations_vec
```

数学上，单个 warp 返回的是它负责的那 128 维的归一化结果；16 个 warp 拼起来就是完整的 2048 维 RMSNorm 输出。注意每个 warp 算的是**同一份** `rms_factor`（因为 `full_sum` 跨 warp 一致），但乘的是**各自的**激活切片与缩放切片。

#### 4.2.3 源码精读

**阶段 1** 见 [utils.cuh:9-15](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/utils.cuh#L9-L15)：

```cpp
rv_t activations_vec, sq_activations_vec, rms_scale_vec;     // rv_t = rv_fl<128>
kittens::warp::load(activations_vec, activations_smem);
kittens::warp::copy(sq_activations_vec, activations_vec);
kittens::warp::mul(sq_activations_vec, sq_activations_vec, sq_activations_vec);  // 平方
float partial_sum = kittens::warp::sum(sq_activations_vec);                      // warp 内归约
```

`warp::sum` 是 warp 内的 shuffle 归约，结果广播到该 warp 所有 lane（每个 lane 都拿到同一个 `partial_sum`）。

**阶段 2** 见 [utils.cuh:17-27](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/utils.cuh#L17-L27)：

```cpp
float *smem_rms_partial_sums = (float *)scratch_memory;        // 借用 4.1 提到的 scratch 第 4 段
if (kittens::laneid() == 0) {
    smem_rms_partial_sums[kittens::warpid()] = partial_sum;    # 每 warp 只 lane 0 写
}
kittens::group<Config::NUM_CONSUMER_WARPS>::sync(0);           # 16-warp 屏障

float full_sum = 0;
#pragma unroll
for (int i = 0; i < Config::NUM_CONSUMER_WARPS; i++)           # 每 thread 各自读 16 格
    full_sum += smem_rms_partial_sums[i];
```

为什么阶段 1 用 warp shuffle、阶段 2 用共享内存？因为阶段 1 把 128 个数归约成 1 个，正好在一个 warp 内，shuffle 无需访存；但 16 个 warp 的 partial sum 分布在不同 warp 的寄存器里，必须经共享内存「碰头」。这就是「能用 shuffle 就不碰共享内存」的层次化归约原则。

**归一化与缩放** 见 [utils.cuh:29-36](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/utils.cuh#L29-L36)：

```cpp
float variance   = full_sum / 2048.0f;                         # ⚠ 硬编码 2048 = hidden_dim
float rms_scale  = rsqrtf(variance + rms_norm_eps);
kittens::warp::mul(activations_vec, activations_vec, rms_scale);   # × 1/RMS
kittens::warp::load(rms_scale_vec, rms_scale_smem);                # 可学习 g
kittens::warp::mul(activations_vec, activations_vec, rms_scale_vec);# × g
return activations_vec;
```

> ⚠️ 一个值得注意的硬编码：`full_sum / 2048.0f` 把 `hidden_dim` 写死成 2048，而不是用 `Globals::hidden_dim` 或 `REDUCTION_DIM_PER_WARP * NUM_CONSUMER_WARPS`。在本 demo（Llama-1B，hidden=2048）下正确，但若改 hidden_dim 需同步改这里。阅读时把它当 `hidden_dim` 理解即可。

#### 4.2.4 代码实践（本讲核心实践之一）

**实践目标**：解释「RMSNorm 为什么必须两阶段」，并把每阶段对应到具体源码行。

**操作步骤**：

1. 读 [utils.cuh:5-37](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/utils.cuh#L5-L37)。
2. 回答：单个 warp 持有的 `activations_vec` 是全 2048 维，还是只占其中 128 维？（提示：`sv_t::length = REDUCTION_DIM_PER_WARP = 128`，见 [matvec_pipeline.cuh:306](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh#L306) 与 4.1.3 的切片逻辑。）
3. 据此推出：只靠阶段 1 的 `partial_sum` 算出来的「方差」是哪一段的方差？为什么必须再过阶段 2 才能得到真正的 \(\frac{1}{2048}\sum x_i^2\)？
4. 想一个反例：如果**删掉阶段 2**（直接用 `partial_sum/2048` 当方差），数值会偏大还是偏小？归一化后的向量会发生什么？

**需要观察的现象**：

- 单个 warp 只持有 128/2048 维，`partial_sum` 只是这 128 维的平方和，约为真值的 1/16。
- 若删阶段 2，方差被低估到约 1/16，`rsqrtf` 给出的 `rms_scale` 放大 4 倍，激活整体被错误放大 → 数值发散。
- 阶段 2 的共享内存往返把 16 个 partial sum 汇成真值，是「跨 warp」不可省的一步。

**预期结果**：你能讲清楚「warp 内 shuffle 归约（省访存）+ 跨 warp 共享内存归约（跨 warp 必需）」的分工，并指出 `group<NUM_CONSUMER_WARPS>::sync`（[utils.cuh:21](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/utils.cuh#L21)）是两阶段之间的唯一同步点。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：阶段 2 里，为什么只在 `laneid() == 0` 时写共享内存，却让**所有** lane 都参与读？

**答案**：写共享内存只需一个代表即可（16 个 warp 各派 lane 0 写自己那一格），多写浪费且没必要。但读必须全员：因为后续 `warp::mul` 需要**每个 lane** 寄存器里都有正确的 `rms_scale` 标量去乘自己的那部分激活。所以写是「每 warp 一份」，读是「每 lane 一份」（每 lane 都把 16 格求和得到同一个 `full_sum`，再各自 `rsqrtf`）。

**练习 2**：能不能把阶段 2 的共享内存往返也换成 `warp::sum` 之类的 shuffle？

**答案**：不能直接换。`warp::sum` 只在一个 warp 的 32 个 lane 之间 shuffle，跨不到别的 warp。要跨 warp shuffle 得用 cooperative groups 的多 warp shuffle 或 `__syncwarp` + 反复交换，开销与复杂度高于「写一格共享内存 + 一次 group sync + 读」。在本规模的固定 16-warp 场景下，共享内存归约是更简洁清晰的选择。

### 4.3 matvec 设备函数：Hopper 乘加 vs Blackwell mma_ABt（本讲核心实践之二）

#### 4.3.1 概念说明

`matvec` 是 consumer 流水线里**每个迭代、每个 warp** 调一次的核心计算（被父类 `consumer_loop` 在 [matvec_pipeline.cuh:195](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh#L195) 调用）。它的签名（[utils.cuh:41-43](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/utils.cuh#L41-L43) 与 [73-76](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/utils.cuh#L73-L76)）是：

```cpp
void matvec(sv_fl<st_t::rows> &out_smem,   // 16 元 float 输出（写共享内存）
            st_t &weights_smem,             // st_bf<16, 128> 权重 tile
            rv_fl<st_t::cols> &activations) // 128 元 float 激活（本 warp 那段）
```

它要算的是一个 **partial matvec**：

\[
\text{out}[r] = \sum_{c=0}^{127} W[r][c] \cdot a[c], \qquad r \in [0,16)
\]

即用本 warp 的 128 维激活切片，乘以权重的对应 128 列，得到 16 元部分和（注意：这 16 元只是「全 2048 列中 128 列的贡献」，还不是最终输出，需经 4.4 的 `matvec_reduce` 跨 warp 累加）。

关键在于：**同一个数学运算，Hopper 和 Blackwell 用了两套完全不同的寄存器/tensor-core 实现**，由 `#ifdef KITTENS_BLACKWELL` 切换（[utils.cuh:39](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/utils.cuh#L39) 与 [72](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/utils.cuh#L72)）。理解这两条分支的差异，是本讲的核心目标之一。

#### 4.3.2 核心流程

两条分支都先把一维激活向量 `activations`（`rv_fl<128>`）转成行向量 `row_activations`，再用 `broadcast_col` 把它**沿列方向广播**成一个 `[16, 128]` 的 tile——这个 tile 的每一行都等于激活向量：

```cpp
broadcast_activations[r][c] = a[c]   （对所有 r 相同）
```

到此为止两条分支相同。分歧在「怎么把 `broadcast_activations` 和 `weights` 结合成输出」：

**Hopper 分支（`#else`，[utils.cuh:73-101](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/utils.cuh#L73-L101)）** —— 用 **FP32 寄存器 tile 的逐元素乘 + 行求和**：

```
rt_t = rt_fl<16,128>                       # FP32 寄存器 tile（注意是 float，不是 bf16！）
broadcast_activations = broadcast(a)       # [r][c]=a[c]
weights = load(weights_smem)               # 提升为 FP32
broadcast_activations ⊙= weights           # [r][c] = a[c]·W[r][c]   ← 逐元素乘
sum_col_vec = row_sum(broadcast_activations) # sum_col_vec[r] = Σ_c a[c]·W[r][c]  ← 行求和 = matvec
```

这里 `rt_t = rt_fl<...>`（[utils.cuh:77](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/utils.cuh#L77)），整条路径走的是 FP32 的 FFMA（融合乘加）寄存器运算，**不碰 tensor core**。因为逐元素乘之后每个 `[r][c]` 是不同的乘积，`row_sum` 把一行里 128 个乘积真正加起来，正好就是点积。

**Blackwell 分支（`#ifdef KITTENS_BLACKWELL`，[utils.cuh:39-71](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/utils.cuh#L39-L71)）** —— 用 **bf16 tile + tensor core `mma_ABt` + `row_max` 列折叠**：

```
rt_t = rt_bf<16,128>                       # bf16 寄存器 tile
broadcast_activations = broadcast(a)       # [r][c]=a[c]（bf16）
weights = load(weights_smem)               # bf16
out_activations = zero : rt_fl<16,16>      # FP32 累加器
mma_ABt(out_activations, weights, broadcast_activations, out_activations)
#   即 out = W · broadcast_activationsᵀ
#   out[r][j] = Σ_c W[r][c] · broadcast_activations[j][c] = Σ_c W[r][c]·a[c]  （与 j 无关！）
sum_col_vec = row_max(out_activations)     # 列折叠：因为每列都相等，max 直接取到点积值
```

这里 `rt_t = rt_bf<...>`（[utils.cuh:44](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/utils.cuh#L44)），`mma_ABt` 是 tensor core 的矩阵乘（\(C = A B^\top + C\)），权重与激活都以 bf16 参与、FP32 累加。

#### 4.3.3 源码精读

**两条分支共同的前奏**（取激活广播）：Hopper 在 [utils.cuh:83-87](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/utils.cuh#L83-L87)，Blackwell 在 [utils.cuh:51-56](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/utils.cuh#L51-L56)，代码几乎一样，只是 tile 类型（`rt_fl` vs `rt_bf`）不同：

```cpp
rrv_t row_activations;
kittens::warp::copy(row_activations, activations);            # rv → row_vec
rt_t broadcast_activations, weights;
kittens::warp::broadcast_col(broadcast_activations, row_activations);  # 广播成 [R,C]
kittens::warp::load(weights, weights_smem);
```

**Hopper 的计算核心**（[utils.cuh:89-91](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/utils.cuh#L89-L91)）：

```cpp
kittens::warp::mul(broadcast_activations, broadcast_activations, weights);  # 逐元素乘
rcv_t sum_col_vec;
kittens::warp::row_sum(sum_col_vec, broadcast_activations);                 # 行求和
```

**Blackwell 的计算核心**（[utils.cuh:57-62](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/utils.cuh#L57-L62)）：

```cpp
kittens::rt_fl<16, 16> out_activations;
kittens::warp::zero(out_activations);
kittens::warp::mma_ABt(out_activations, weights, broadcast_activations, out_activations);
rcv_t sum_col_vec;
kittens::warp::row_max(sum_col_vec, out_activations);   # ← 注意是 row_max，不是 row_sum
```

**最妙的对比：为什么 Blackwell 用 `row_max`、Hopper 用 `row_sum`？** 这不是随意的。回到 `mma_ABt` 的结果：`out_activations[r][j] = Σ_c W[r][c]·a[c]`，它**与列号 j 无关**（因为 `broadcast_activations` 每行都等于 a）。也就是说 `out_activations` 的每一「逻辑行」里，所有列位置上的值**完全相等**，都等于该行的点积。因此：

- `row_max`（取一行里的最大值）= 那个处处相等的值 = 点积本身。✓ 直接得到结果。
- 若改用 `row_sum`，会得到 `n_cols × 点积`，还要再除一次，所以这里**故意用 `row_max` 免去除法**。

反观 Hopper 路径：逐元素乘之后 `[r][c] = a[c]·W[r][c]` 在不同 c 上**真的不同**，`row_sum` 把它们加起来才得到点积，没有「处处相等」可言，所以必须用 `row_sum`。**两条分支连最后的折叠算子都不一样，正是因为它们的中间表示（mma 的列冗余 vs 逐元素乘的逐列不同）结构不同。**

> 顺带一提：Blackwell 选 `mma_ABt` + tensor core，而 Hopper 选 FP32 寄存器 FFMA，是两代硬件上「对这个 tile 形状最划算的路径」不同：Blackwell 的 tensor core 走 bf16→FP32 累加的高吞吐 mma；Hopper 在此 tile 规模下用 FP32 FFMA 寄存器运算。这是工程上的硬件适配，不是数学差异——两者算的是同一个 partial matvec。

**两条分支共同的收尾**（写出 16 元输出）：Hopper 在 [utils.cuh:93-99](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/utils.cuh#L93-L99)，Blackwell 在 [utils.cuh:64-70](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/utils.cuh#L64-L70)，完全一致：

```cpp
rv_t sum_vec;
kittens::warp::copy(sum_vec, sum_col_vec);     # col_vec → row_vec
if (kittens::laneid() < 16) {
    out_smem[kittens::laneid()] = sum_vec[0][0];   # 16 个 lane 各写一个元素
}
kittens::warp::sync();
```

`out_smem` 是 `sv_fl<16>`，16 个 lane（lane 0-15）协作各写一个元素，把这 warp 的 16 元部分和落进 scratch 输出区。

#### 4.3.4 代码实践

**实践目标**：把 Hopper 与 Blackwell 两个 `matvec` 分支逐行对比，说清「计算原语」与「折叠算子」两处差异的成因。

**操作步骤**：

1. 并排打开 [utils.cuh:39-71](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/utils.cuh#L39-L71)（Blackwell）与 [utils.cuh:73-101](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/utils.cuh#L73-L101)（Hopper）。
2. 填下面这张对比表（答案见「需要观察的现象」）：

   | 维度 | Hopper 分支 | Blackwell 分支 |
   | --- | --- | --- |
   | tile 类型 `rt_t` | ? | ? |
   | 核心计算原语 | ? | ? |
   | 累加器精度 | ? | ? |
   | 折叠算子 | ? | ? |
   | 中间表示是否「列冗余」 | ? | ? |

3. 推导：为什么 Blackwell 的 `mma_ABt` 结果每列相等？为什么这让你能（也必须）用 `row_max` 而不是 `row_sum`？
4. 反向推导：如果把 Hopper 分支的 `row_sum` 也换成 `row_max`，结果对不对？为什么？

**需要观察的现象（对照答案）**：

| 维度 | Hopper 分支 | Blackwell 分支 |
| --- | --- | --- |
| tile 类型 `rt_t` | `rt_fl<16,128>`（FP32） | `rt_bf<16,128>`（bf16） |
| 核心计算原语 | `warp::mul`（逐元素 FFMA） | `warp::mma_ABt`（tensor core 矩阵乘） |
| 累加器精度 | FP32（寄存器 tile 本身即 FP32） | FP32 累加器 `rt_fl<16,16>`，bf16 输入 |
| 折叠算子 | `row_sum` | `row_max` |
| 中间表示「列冗余」 | 否（每列乘积不同） | 是（`out[r][j]` 与 j 无关） |

- Blackwell 因 `broadcast_col` 使每行相等，`mma_ABt` 后每列相等 → `row_max` 直接取点积。
- Hopper 若改 `row_max`：逐元素乘后 `[r][c]` 各不相同，`row_max` 只取最大那个乘积，**不是**点积 → 错。

**预期结果**：你能不看源码复述「两条分支算的是同一个 partial matvec，但 Hopper 走 FP32 逐元素乘 + row_sum，Blackwell 走 bf16 tensor-core mma_ABt + row_max；折叠算子的选择由中间表示是否列冗余决定」。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`broadcast_col` 把 128 元向量广播成 `[16,128]` tile。为什么 matvec 需要这个广播？不能直接让一维向量和二维权重相乘吗？

**答案**：`mma_ABt`（tensor core）和 `warp::mul`（逐元素）都要求两个操作数是**同形状的 tile**。权重是 `[16,128]`，激活是一维 `[128]`，要相乘就得先把激活「撑」成 `[16,128]`（每行复制一份）。广播正是这个「撑」操作，且不实际搬运数据（寄存器里只是逻辑视图），开销极低。广播后，每行与权重逐行相乘/相加，正好是 16 个独立的点积。

**练习 2**：Blackwell 分支里累加器为什么是 `rt_fl<16,16>`，而权重是 `rt_bf<16,128>`？两个形状对不上是怎么回事？

**答案**：`mma_ABt(C, A, B, C)` 里 A=`weights`（`[16,128]`），B=`broadcast_activations`（`[16,128]`），它计算 \(C = A B^\top\)，于是 \(C\) 的形状是 `[16, 16]`（16×128 乘 128×16）。所以累加器 `out_activations` 是 `rt_fl<16,16>`——形状由 mma 的矩阵乘法维度决定，不必和权重同形。这也是为什么结果天然「列冗余」：B 的每行都等于 a，\(B^\top\) 的每列都等于 a，于是 C 的每列都是 `W·a`。

### 4.4 matvec_reduce：跨 warp 归约（split-K 收尾）

#### 4.4.1 概念说明

`matvec_reduce`（[utils.cuh:103-120](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/utils.cuh#L103-L120)）完成 matvec 的**跨 warp 收尾**。回顾 4.3：每个 consumer warp 的 `matvec` 只用自己那 128 列权重算出 16 元**部分和**；而真正的输出元素要累加**全 2048 列**的贡献。16 个 warp 各自覆盖 128 列（16×128=2048），所以最终输出 = 16 个 warp 的部分和之**和**。`matvec_reduce` 就是把这个求和做了。

用并行计算的术语，这是一个 **split-K（沿归约维切分）matvec**：把权重矩阵的 K 维（=hidden_dim=2048）切成 16 段分给 16 个 warp 并行算部分积，再由 `matvec_reduce` 做 K 维上的最终归约。split-K 的代价是「需要一次跨 warp 归约」，收益是「16 个 warp 同时算、把 K 维并行掉」。

它在哪里被调？不是在 consumer 里，而是在 **storer 的 `store` 回调**里。看宿主 `rms_qkv_rope_append::pipeline_specifics::store`（[rms_matvec_rope_append.cu:90-92](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/rms_matvec_rope_append.cu#L90-L92)）：

```cpp
matvec_reduce<Config, kittens::sv_fl<16>, kittens::rv_fl<16>,
              pipeline::SCRATCH_BYTES_PER_WARP>(output_scratch_start, qkv_proj);
```

这是因为：consumer 把 16 个部分和写进 scratch 的 16 个连续槽（每 warp 一槽，`SCRATCH_BYTES_PER_WARP` 间距）；等 `outputs_arrived` 翻转后，storer 才能安全地把这 16 槽读出来求和。所以归约发生在 store 阶段，而不是 compute 阶段。

#### 4.4.2 核心流程

```
sum_vec = zero                                  # 清零 16 元累加器
for i in [0, NUM_CONSUMER_WARPS):               # 遍历 16 个 warp 的输出槽
    part = scratch[i * SCRATCH_BYTES_PER_WARP]  # 第 i 个 warp 的 16 元部分和（sv_fl<16>）
    load part_vec ← part
    sum_vec += part_vec                          # 累加
return sum_vec（经引用写出）
```

它把 scratch 里「16 warp × 16 float」的输出区，沿 warp 维累加成单个 16 元向量。注意这是**单 warp**（storer）做的串行循环——16 次加载 + 16 次加法，量很小（16×16=256 个 float），不值得再上并行归约。

#### 4.4.3 源码精读

`matvec_reduce` 全文见 [utils.cuh:103-120](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/utils.cuh#L103-L120)：

```cpp
template <typename Config, kittens::ducks::sv::all sv_t, typename rv_t,
          int SCRATCH_BYTES_PER_WARP>
__device__ static inline void matvec_reduce(uint8_t *scratch, rv_t &sum_vec) {
    rv_t part_vec;
    kittens::warp::zero(sum_vec);
#pragma unroll
    for (int i = 0; i < Config::NUM_CONSUMER_WARPS; i++) {
        // TODO: for now, deliberately not using sizeof(sv_t) here because we've
        // had alignment issues before.
        sv_t &part = *reinterpret_cast<sv_t *>(scratch + (i * SCRATCH_BYTES_PER_WARP));
        kittens::warp::load(part_vec, part);
        kittens::warp::add(sum_vec, sum_vec, part_vec);
    }
}
```

几个要点：

- **模板参数 `SCRATCH_BYTES_PER_WARP`**：跨槽步长。注释明确说「故意不用 `sizeof(sv_t)`，因为之前踩过对齐坑」——即槽间距由父类的 `SCRATCH_BYTES_PER_WARP = 16·sizeof(float) = 64` 字节决定（[matvec_pipeline.cuh:20](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh#L20)），与实际 `sv_fl<16>` 的大小一致，但用显式常量更稳妥。
- **`scratch` 起点**：调用方传入 `output_scratch_start = get_output_start(s, output_stage)`（[rms_matvec_rope_append.cu:80-81](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/rms_matvec_rope_append.cu#L80-L81)），即「当前 output_stage 那一段」的起始。16 个 warp 的部分和就连续排在 `[start, start + 16·SCRATCH_BYTES_PER_WARP)` 里（见 [u8-l2 §4.4](u8-l2-matvec-pipeline.md) consumer 按 `warpid()` 写 scratch）。
- **与父类 scratch 布局吻合**：父类 `consumer_loop` 用 `out_smem = get_output_start(s, output_stage) + warpid()*SCRATCH_BYTES_PER_WARP`（[matvec_pipeline.cuh:185-187](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh#L185-L187)）写部分和，这里用同样的公式读回——读写布局严格对称。

#### 4.4.4 代码实践

**实践目标**：把「split-K 切分 → 各 warp 算部分和 → matvec_reduce 归约」串成一条完整数据通路，确认跨 warp 归约是必要的。

**操作步骤**：

1. 读 [utils.cuh:103-120](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/utils.cuh#L103-L120) 与它在 storer 里的调用点 [rms_matvec_rope_append.cu:80-92](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/rms_matvec_rope_append.cu#L80-L92)。
2. 回答：单个 warp 的 `matvec` 算出的 16 元向量，覆盖了全 2048 列里的多少列？为什么最终输出需要 16 个这样的向量相加？
3. 跟踪数据落点：consumer 在 [matvec_pipeline.cuh:185-195](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh#L185-L195) 把部分和写到 scratch 的哪个偏移？`matvec_reduce` 又从哪个偏移读回？两者对得上吗？
4. 推断：如果没有 `matvec_reduce`，storer 直接把 scratch 里**某一个 warp**的部分和当最终输出写回 gmem，会发生什么？

**需要观察的现象**：

- 单 warp 只覆盖 128/2048 列，其部分和约为真值的 1/16。
- consumer 写：`get_output_start(s, stage) + warpid()·64`；reduce 读：`scratch + i·64`（`scratch = get_output_start(s, stage)`），`i` 遍历 0..15 对应 16 个 warp。读写布局完全对称。✓
- 若不归约直接写一个 warp 的部分和 → 输出只有 1/16 的贡献，数值严重偏小、模型输出全错。

**预期结果**：你能画出「2048 维激活 → 16 warp 各算 128 列的 16 元部分和（写 scratch）→ matvec_reduce 累加 16 份 → 16 元最终输出 → storer 写回 gmem」的通路图，并指出 `matvec_reduce` 是 split-K 必备的跨 warp 归约步骤。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`matvec_reduce` 为什么在 **storer** 而不是 **consumer** 里做？

**答案**：因为 16 个 consumer warp 是**异步并发**写各自的部分和的，必须等它们全部写完（即 `outputs_arrived` 攒满 `NUM_CONSUMER_WARPS` 次、相位翻转）后，读这 16 槽才是完整的。storer 正是「等 `outputs_arrived` 之后才动作」的角色（[u8-l2 §4.5](u8-l2-matvec-pipeline.md)），所以在 storer 的 `store` 回调里归约天然安全。若在 consumer 里归约，某个 warp 读别人还没写的槽，会读到脏数据。

**练习 2**：`matvec_reduce` 用「单 warp 串行循环 16 次」做归约，而不是再搞一次跨 warp 并行归约。为什么不并行？

**答案**：因为归约总量很小（16 个 16 元向量 = 256 个 float 加法），且它本就跑在单个 storer warp 上。再拆给 16 个 warp 并行反而要额外的同步与共享内存往返，得不偿失。串行循环 + `#pragma unroll` 编译期展开，对这种小规模归约是最简单高效的选择。

## 5. 综合实践：追踪一个 16 元输出片段的完整数据通路

**实践目标**：把本讲四个模块（pipeline 扩展 / rms_norm / matvec / matvec_reduce）串成一条端到端的数据通路，确认你理解了「原始激活 → RMSNorm → split-K matvec → 跨 warp 归约 → 写回」的完整旅程。这是检验本讲理解程度的最有效方式。

**场景设定**：考虑 `rms_qkv_rope_append` op 处理一条指令，其中 `iters=1`（只算 1 个 16 元输出片段），聚焦于「这 16 个输出元素是怎么从 2048 维激活算出来的」。设 `NUM_CONSUMER_WARPS=16`，`hidden_dim=2048`。

**操作步骤**：

1. **激活归一化**（4.1 + 4.2）：画一张「16 warp × 128 元」的网格，表示 2048 维激活被切成 16 片。在网格上标出：哪个 warp 持有哪 128 维？`rms_norm` 阶段 1 各 warp 算出的 `partial_sum` 对应网格的哪一片？阶段 2 怎么把 16 个 `partial_sum` 汇成全 2048 维方差？归一化后，每个 warp 持有的 128 元向量是否都乘了同一个 `rms_factor`？
2. **split-K matvec**（4.3）：把权重矩阵画成 `[16, 2048]`（16 行输出 × 2048 列），按列切成 16 个 `[16,128]` 块分给 16 个 warp。标出：每个 warp 的 `matvec` 用自己的 128 元激活 × `[16,128]` 权重块，算出 16 元部分和。注意这 16 元只是「该 128 列的贡献」。
3. **跨 warp 归约**（4.4）：在 scratch 里画出 16 个连续的 16 元槽（16 warp × 16 元）。标出 `matvec_reduce` 如何把这 16 槽沿 warp 维加成最终的 16 元输出。验证：最终输出的第 r 个元素 = 全 2048 列的点积 = \(\sum_{c=0}^{2047} W[r][c]\cdot \hat{a}[c]\)（\(\hat{a}\) 是归一化后的激活）。
4. **Hopper/Blackwell 对照**（4.3）：在步骤 2 旁边标注两条分支——Hopper 用 `rt_fl` + `mul` + `row_sum`，Blackwell 用 `rt_bf` + `mma_ABt` + `row_max`——并注明它们在第 2 步的「单 warp 内部计算」上是等价的，区别只在寄存器/tensor-core 实现路径。

**参考通路图**：

```
原始激活 a ∈ R^2048 (gmem)
   │  loader 异步 TMA 载入 activation page（含 rms_scale）
   ▼
┌──────── 16 warp × 128 元 ────────┐
│ w0: a[0:128]    w1: a[128:256] … w15: a[1920:2048] │   ← 各 warp 切片
└──────────────────────────────────┘
   │  rms_norm (4.2): 阶段1 各 warp 算 partial_sum；阶段2 共享内存汇总 → 同一 rms_factor
   ▼
归一化激活 â (每 warp 128 元，都 × 同一 rms_factor 再 × 各自的 g 切片)
   │  pipeline::consumer_loop 注入 â，进入 matvec 流水线
   ▼
split-K matvec (4.3):  权重 W[16,2048] 按列切 16 块
   │   warp i:  out_i[r] = Σ_{c=i·128}^{(i+1)·128} W[r][c]·â[c]   (r∈[0,16))
   │   Hopper: rt_fl + mul + row_sum   |   Blackwell: rt_bf + mma_ABt + row_max
   ▼
scratch: 16 个 16 元部分和（16 warp × 16 元，连续存放）
   │  storer::store 等 outputs_arrived 后调 matvec_reduce (4.4)
   ▼
最终 16 元输出  out[r] = Σ_{i=0}^{15} out_i[r] = Σ_{c=0}^{2047} W[r][c]·â[c]
   │  tma::store_async → q_post_rope / k_cache / v_cache（经 RoPE，见 rms_matvec_rope_append.cu:106-152）
   ▼
gmem
```

**需要观察的现象与解释**：

- **归一化只做一次**：`rms_norm` 在 matvec 流水线**开跑前**执行一次（[matvec_pipeline.cuh:340-347](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh#L340-L347)），归一化后的 `â` 被所有迭代复用——因为 RMSNorm 是对输入激活的一次性变换，与 matvec 的迭代次数无关。
- **K 维被并行掉**：2048 列被 16 个 warp 并行处理，每个 warp 只算 128 列的点积；代价是必须有一次 `matvec_reduce` 跨 warp 归约。这就是 split-K 的权衡。
- **两套实现、一个结果**：Hopper 与 Blackwell 在步骤 2 内部走不同路径，但喂给 `matvec_reduce` 的部分和形状、含义完全一致，所以归约与后续步骤对两代硬件都通用。

**预期结果**：你能指着通路图上任一段，说清楚「此刻哪个角色（loader/consumer/storer）在干什么、数据在 gmem/smem/寄存器哪个层、调用了 4.1-4.4 哪个函数」。若某一段说不清，回到对应小节重读。

**延伸（可选）**：把 `iters` 改成 4（即一条指令算 4 个 16 元片段），重画通路图，注意此时 RMSNorm 仍只做一次，但 `matvec` 跑 4 轮（每轮 16 warp 各写一个 16 元部分和到对应 output_stage 的 scratch），`matvec_reduce` 也被 storer 调 4 次。把它与 [u8-l2 §5](u8-l2-matvec-pipeline.md) 的三级流水甘特图对接，看清「归一化在流水线最前端、归约在每轮 store 时」的位置关系。**待本地验证**：若能拿到 `TEVENT_FIRST_LOAD`/`TEVENT_FIRST_USE`/`TEVENT_FIRST_STORE` 等时间戳（[matvec_pipeline.cuh:144-146](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh#L144-L146)），可验证「RMSNorm 的开销被权重加载重叠掉」。

## 6. 本讲小结

- `rms_matvec_pipeline` 继承 `matvec_pipeline`，增量只有四样：一个异步 `rms_scale_arrived` 信号量（loader 单生产者，init 1）、重写的 `loader_loop`（先 TMA 拉缩放向量，再转父类加载权重）、重写的 `consumer_loop`（开算前先 `rms_norm`）、仅 Blackwell 的 `launcher_loop`（等 tensor 描述符）。rms_scale 与激活共用 lid 0 页。
- `rms_norm` 是**两阶段归约**：阶段 1 在 warp 内用 shuffle（`warp::sum`）算 128 维平方和的 partial sum，阶段 2 经共享内存（`group::sync`）把 16 个 partial sum 汇成全 2048 维方差。原因是单个 warp 只持 128/2048 维，必须跨 warp 才能算全平方和。
- `matvec` 在两代硬件上实现不同：**Hopper** 用 FP32 寄存器 tile 的 `warp::mul`（逐元素乘）+ `row_sum`；**Blackwell** 用 bf16 tile 的 tensor core `mma_ABt` + `row_max`。两者算同一个 partial matvec，但因中间表示是否「列冗余」，折叠算子一为 `row_sum`、一为 `row_max`。
- `matvec` 是 **split-K matvec** 的单 warp 片段：每个 warp 只算 2048 列中自己那 128 列的 16 元部分和，写进 scratch；`matvec_reduce` 在 storer 的 `store` 回调里把 16 个部分和跨 warp 累加成最终 16 元输出。
- 整条数据通路是「原始激活 →（RMSNorm 一次）→ 归一化激活 →（matvec 多轮，split-K）→ scratch 部分和 →（matvec_reduce 每轮）→ 最终输出 → store 回 gmem」；RMSNorm 在流水线最前端只做一次，matvec/matvec_reduce 随迭代重复。
- 模板化设计的价值再次体现：宿主 op（如 `rms_qkv_rope_append`）只需继承 `rms_matvec_pipeline`、提供 `ActPtr`/`RmsPtr` 与 `pipeline_specifics` 回调，就自动获得「RMSNorm + 三级 matvec 流水线」全套能力。

## 7. 下一步学习建议

- **看其它继承 `rms_matvec_pipeline` 的 op**：阅读 [demos/low-latency-llama/upgate.cu](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/upgate.cu) 与 [demos/low-latency-llama/matvec_adds.cu](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_adds.cu)，对比它们如何复用本讲的三个设备函数，又如何在 `store` 回调里定制（如 up/gate 双 matvec、SiLU、残差加法）。注意它们是否也调 `matvec_reduce`，以及 `iter_scale>1` 的累加场景。
- **横向对照 attention 的 split-K**：[demos/low-latency-llama/attention_partial.cu](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_partial.cu) 与 [attention_reduction.cu](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_reduction.cu) 是另一类「partial → reduction」结构（attention 的 log-sum-exp 归约比本讲的简单求和更复杂）。对比本讲的 `matvec_reduce`（纯加法）与 attention reduction（带 LSE 校正），能看清「跨 SM/跨 warp 归约」在不同算子里的形态。可配合 [u9-l1](u9-l1-attention-partial-flash.md)、[u9-l2](u9-l2-cross-op-global-barriers.md) 阅读。
- **回到 Hopper vs Blackwell 的全局视角**：本讲看到 matvec 与 launcher_loop 都有 `#ifdef KITTENS_BLACKWELL` 分支。建议在仓库里搜一遍 `KITTENS_BLACKWELL`，体会「同一套 op 逻辑、两套硬件后端」是如何贯穿整个 demo 的。这为将来移植到新硬件（或理解为何某段代码在某卡上更快）打下基础。
