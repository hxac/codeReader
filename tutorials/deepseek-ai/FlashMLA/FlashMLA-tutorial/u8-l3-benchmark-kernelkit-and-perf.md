# Benchmark、kernelkit 与性能调优

> 本讲是 Unit 8（底层工具、测试与性能测量）的第三篇。前置讲义为 [u3-l1 MLA 解码的 compute-bound 理论分析](u3-l1-compute-bound-analysis.md)（提供算术强度与 bound 判定的理论）与 [u8-l2 测试体系与 PyTorch 参考实现](u8-l2-test-harness.md)（提供 `bench_kineto` / `check_is_allclose` 的初步认识）。本讲不再重复「正确性怎么判」，而是回答另一个问题：**「这个 kernel 到底有多快，离理论极限还差多远」**。

## 1. 本讲目标

学完本讲，你应当能够：

1. 看懂 `benchmark/bench_flash_mla.py` 如何把 torch / flash_mla / flash_infer / triton 四种实现在同一批 shape 上横向对比，并产出 CSV。
2. 手算给定 shape 的理论 FLOPs 与访存字节数，进而推出理论 TFLOPS 与 GB/s，判断一个实测耗时是否「喂饱了硬件」。
3. 区分 `tests/kernelkit` 提供的两套测时工具 `bench_kineto` 与 `bench_by_cuda_events`，知道何时用哪一个、以及 `get_e2e_time` 如何把多个 kernel 串成端到端耗时。
4. 读懂 `setup.py` 中的 NVCC 性能 flag（`--use_fast_math`、`--register-usage-level=10`、`--warn-on-spills`、`-lineinfo` 等）对 kernel 性能与稳定性的意义。
5. 把博客里的优化技巧（seesaw、细粒度 TMA 流水、cache hint、PDL、crossover）和「实测指标」对应起来，建立「改一处、测一处」的调优闭环。

## 2. 前置知识

本讲需要你大致理解以下概念（不熟可先看前置讲义）：

- **compute-bound vs memory-bound**：一个 kernel 受限于算力（FLOPS）还是带宽（byte/s）。判定方法是算术强度（FLOP/byte）与 GPU 平衡点比较，详见 [u3-l1](u3-l1-compute-bound-analysis.md)。本讲的 TFLOPS / GB/s 两个指标就是用来验证「你到底卡在哪一边」。
- **Roofline 模型**：用一条折线表达「算力上限」和「带宽上限」，kernel 的实测点落在折线下方，离折线越近说明优化越到位。
- **Tensor Core / CUDA Core**：GPU 上两类计算单元。Tensor Core 跑矩阵乘（MMA），CUDA Core 跑标量指令（如 FP8 反量化）。本讲的优化技巧很多都是为了「不让 Tensor Core 空等 CUDA Core」。
- **L2 cache 与「冷启动」测时**：如果上一次调用把数据留在了 L2，下一次调用会异常快，测出来的带宽虚高。所以严肃测时必须先「刷」L2。
- **ptxas / SASS**：NVCC 把 `.cu` 编译成 PTX（虚拟指令），再由 ptxas 编译成 SASS（真实硬件指令）。寄存器分配、是否溢出（spill）都在 ptxas 阶段决定。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `benchmark/bench_flash_mla.py` | 多实现横向对比 benchmark，含 FLOPs/bytes 公式与 CSV 输出 |
| `benchmark/visualize.py` | 把 benchmark 产出的 CSV 画成「带宽 vs seqlen」折线图 |
| `tests/kernelkit/bench.py` | 两套测时工具 `bench_kineto`（CUPTI，按 kernel 名拆分）与 `bench_by_cuda_events`（CUDA Event，交错 A/B） |
| `tests/kernelkit/compare.py` | 数值对比 `check_is_allclose`（已在 u8-l2 讲过，本讲聚焦性能）|
| `tests/kernelkit/utils.py` | `is_using_profiling_tools()` 检测 nsys/ncu，避免与 CUPTI 冲突 |
| `tests/lib.py` | `count_flop_and_mem_vol_for_decode`：sparse 解码专用的、更精确的 FLOP/字节数公式 |
| `setup.py` | NVCC 编译参数，含性能 flag |
| `docs/20250422-new-kernel-deep-dive.md` | dense decode 优化技巧博客（compute-bound 分析、seesaw、TMA 流水、PDL）|
| `docs/20250929-hopper-fp8-sparse-deep-dive.md` | FP8 sparse decode 优化技巧博客（crossover、DSM）|

## 4. 核心概念与源码讲解

本讲按四个最小模块展开：先讲「怎么把多个实现放一起比」（4.1），再讲「怎么把实测耗时翻译成理论指标」（4.2），接着讲 kernelkit 的两套测时利器（4.3），最后讲编译期 flag 与博客优化技巧的对应关系（4.4）。

### 4.1 多实现 benchmark：bench_flash_mla.py

#### 4.1.1 概念说明

一个新 kernel 写出来，最自然的验证方式是「和已有的、被广泛信任的实现比一比」。FlashMLA 的 `benchmark/bench_flash_mla.py` 就是这样一个脚本：它在同一批 `(batch, seqlen, head)` 配置上，依次跑四种实现——

- `torch`：纯 PyTorch 的 `scaled_dot_product_attention` 参考实现（最慢，但最可信）；
- `flash_mla`：本项目的 CUDA kernel；
- `flash_infer`：第三方高性能库 [FlashInfer](https://docs.flashinfer.ai/) 的 MLA wrapper；
- `flash_mla_triton`：一个公开的 Triton 版 MLA kernel（脚本顶部注明了来源链接）。

然后它把每个实现的耗时翻译成 TFLOPS 与 GB/s 两个指标，写进 CSV，供 `visualize.py` 画图。

> 注意：这个 benchmark 面向的是 **dense 解码**（`h_kv=1, d=512+64=576, dv=512`），即 MLA 的 MQA 形态。FP8 sparse 解码的性能测量走的是 `tests/` 下的 kernelkit 流程（见 4.3 与 u8-l2），而非这个脚本。

#### 4.1.2 核心流程

整个 benchmark 的流程可以概括为：

```text
shape_configs（一批配置）
        │
        ▼
对每个 shape：
   ├─ 构造 q / block_table / blocked_k（Paged KV cache）
   ├─ 选定 baseline_func 与 target_func（来自 FUNC_TABLE）
   ├─ 各跑一次 → torch.testing.assert_close 校验数值
   ├─ 用 triton.testing.do_bench 各测一次耗时 perf_a / perf_b
   ├─ 用公式算 FLOPS、bytes
   └─ 打印 "{tflops} TFLOPS, {gBps} GB/s" 并写 CSV
```

其中「测耗时」借用了 Triton 自带的 `triton.testing.do_bench`，它内部会做 warmup、多次取均值并处理 GPU 异步。这是脚本里唯一和「时间」打交道的部分；FLOPS/bytes 则是纯算术，4.2 节详述。

四种实现被注册在一张函数表里，benchmark 通过名字查表派发：

[benchmark/bench_flash_mla.py:403-408](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/benchmark/bench_flash_mla.py#L403-L408) 用 `FUNC_TABLE` 字典把字符串名字映射到对应的 `run_*_mla` 函数。

#### 4.1.3 源码精读

每个 `run_*` 函数的套路一致：构造闭包 → 调一次拿结果 → 用 `do_bench` 测闭包耗时。以 FlashMLA 自身实现为例：

[benchmark/bench_flash_mla.py:62-78](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/benchmark/bench_flash_mla.py#L62-L78) 先调 `get_mla_metadata` 拿到调度元数据（这是 [u1-l4](u1-l4-python-api-quickstart.md) 讲过的「首次初始化、后续复用」模式），再把 `flash_mla_with_kvcache` 包成无参闭包交给 `triton.testing.do_bench`。

值得注意的细节：

- **数值先校验，再测时**。`compare_ab` 在测时之前会先用 `torch.testing.assert_close`（atol=rtol=1e-2）确认两个实现结果一致，避免「比了一个错的结果」：

  [benchmark/bench_flash_mla.py:437-441](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/benchmark/bench_flash_mla.py#L437-L441) 校验 `out`，并对「不返回 lse」的 triton 实现与「lse 含义不同」的 flash_infer 跳过 lse 校验。

- **shape 矩阵固定**。脚本底部写死了一组配置，batch=128、seqlen 取 1024…65536、head=128：

  [benchmark/bench_flash_mla.py:487-490](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/benchmark/bench_flash_mla.py#L487-L490) 用列表推导生成一批 `s_q=1, d=576, dv=512, causal=True` 的解码配置。

- **`--all` / `--compare` / `--one` 三种模式**：

  [benchmark/bench_flash_mla.py:504-520](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/benchmark/bench_flash_mla.py#L504-L520) 根据 CLI 参数决定「跑全部实现」「两两对比」还是「只跑一个」，结果都落到 `{benchmark_type}_perf.csv`，列名为 `name,batch,seqlen,head,bw`。

`visualize.py` 则是配套的可视化：

[benchmark/visualize.py:16-28](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/benchmark/visualize.py#L16-L28) 读 CSV，按 `name` 分组，画出「带宽 vs seqlen」折线图。

#### 4.1.4 代码实践

**实践目标**：在不跑 GPU 的前提下，看懂 benchmark 如何派发，并验证你对四种实现的输入约定一致。

**操作步骤**：

1. 打开 `benchmark/bench_flash_mla.py`，找到 `FUNC_TABLE`（403 行附近）。
2. 分别打开 `run_torch_mla`、`run_flash_mla`、`run_flash_infer`、`run_flash_mla_triton`，确认它们接受的参数列表完全一致（都是 `q, block_table, blocked_k, max_seqlen_pad, block_size, b, s_q, cache_seqlens, h_q, h_kv, d, dv, causal, dtype`）。
3. 注意 `run_flash_infer` 与 `run_flash_mla_triton` 内部都断言 `d > dv`（87、387 行），并把 q / blocked_k 显式拆成 `nope`/`pe` 两段——这正是 MLA「NoPE 段 + RoPE 段」的拆分（见 [u5-l1](u5-l1-fp8-kvcache-format.md)）。
4. 观察 `compare_ab` 如何用 `FUNC_TABLE[baseline]` 与 `FUNC_TABLE[target]` 取出两个函数。

**需要观察的现象**：四种实现函数签名同构，意味着切换实现不需要改数据准备代码——这正是「多实现对比」能成立的前提。

**预期结果**：你能用一张表说明每个 `run_*` 函数内部把 MLA 的 `d=576, dv=512` 怎么映射到自己库的 API（FlashMLA 原样传、FlashInfer 拆 nope/pe 传 `d-dv` 作为 kpe 维度、Triton 同样拆）。

> 本实践为源码阅读型，无 GPU 也能完成。

#### 4.1.5 小练习与答案

**Q1**：为什么 `compare_ab` 对 flash_infer 和 flash_mla_triton 跳过 lse 校验？

**答**：因为 flash_mla_triton 根本不返回 lse（其函数返回 `(out, None, t)`，见 400 行），而 flash_infer 的 lse 返回值与 FlashMLA 在尺度/形状上约定不同（注释 `flash_infer has a different lse return value`）。所以只比 `out`，避免误报。

**Q2**：如果要新增第五种实现（比如「FlashMLA + FP8」），最少要改哪几处？

**答**：写一个新的 `run_*` 函数（签名与现有一致）、在 `FUNC_TABLE` 里加一条映射、把它加入 `available_targets` 列表（480 行）。shape 生成与 CSV 逻辑无需改动。

---

### 4.2 FLOPs / bytes 计算与理论 TFlops / GB/s

#### 4.2.1 概念说明

测出一个 kernel 跑了 `perf` 毫秒，这个数字本身没意义——必须除以「理论工作量」才能变成可比较的指标。FlashMLA 用两个指标：

- **TFLOPS**（每秒万亿次浮点运算）= 总浮点运算次数 / 耗时。衡量算力利用。
- **GB/s**（每秒千兆字节）= 总访存字节数 / 耗时。衡量带宽利用。

这两个指标分别对应 [u3-l1](u3-l1-compute-bound-analysis.md) 讲的 compute-bound 与 memory-bound。一个 kernel 如果 TFLOPS 接近峰值（如 H800 降频后约 865 TFlops），说明算力被打满；如果 GB/s 接近峰值（约 3.35 TB/s），说明带宽被打满。

#### 4.2.2 核心流程

给定 shape `(b, s_q, total_seqlens, h_q, h_kv, d, dv)`，dense 解码的公式（与 [u3-l1](u3-l1-compute-bound-analysis.md) 的理论推导一致）为：

\[
\text{FLOPS} = s_q \cdot \text{total\_seqlens} \cdot h_q \cdot (d + d_v) \cdot 2
\]

\[
\text{bytes} = \bigl(\text{total\_seqlens}\cdot h_{kv}\cdot d \;+\; b\cdot s_q\cdot h_q\cdot d \;+\; b\cdot s_q\cdot h_q\cdot d_v\bigr)\cdot(\text{bits}/8)
\]

\[
\text{TFLOPS} = \frac{\text{FLOPS}}{10^{9}\cdot \text{perf\_ms}}, \qquad
\text{GB/s} = \frac{\text{bytes}}{10^{6}\cdot \text{perf\_ms}}
\]

这里有两点需要特别注意（也是 MLA 与普通 attention 的区别）：

1. **FLOPS 用 `h_q` 而非 `h_kv`**：因为 Q 有 `h_q` 个头要算，而 K/V 被 MQA 广播。算量由 query 头数决定。
2. **bytes 里没有单独的 V 项**：MLA 中 K 与 V 同源（V 只是 K 的前 `d_v` 维），所以加载一次 K 就顺带覆盖了 V。访存项里只有 `total_seqlens·h_kv·d`（K，且 `h_kv=1` 只加载一次）、Q、输出 O，**没有独立的 V 项**。这正是 [u3-l1](u3-l1-compute-bound-analysis.md) 推出 `bytes ≈ 2·s_k·d_k` 的根因。

> 单位陷阱：公式里 `FLOPS/1e9/perf_ms` 直接得到 TFLOPS，因为 \(1\,\text{TFLOPS}=10^{12}\,\text{FLOP/s}=10^{9}\,\text{FLOP/ms}\)。同理 `bytes/1e6/perf_ms` 得到 GB/s（\(1\,\text{GB/s}=10^{9}\,\text{B/s}=10^{6}\,\text{B/ms}\)）。

#### 4.2.3 源码精读

这两条公式原样出现在 `compare_ab` 与 `compare_a` 中：

[benchmark/bench_flash_mla.py:443-446](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/benchmark/bench_flash_mla.py#L443-L446) 计算 `FLOPS`、`bytes`，并打印每个实现的 TFLOPS 与 GB/s。

可以看到 `bytes` 公式里 `total_seqlens * h_kv * d` 是 KV（`h_kv=1`），后面两项是 Q 和输出 O——**V 不单独计**，与上面概念说明吻合。

**sparse 解码用另一套更精确的公式**。因为 sparse attention 会用 `indices` 重复索引同一个 KV token，但真实硬件靠 L2 去重，所以应当用「去重后的唯一 token 数」而非「索引总数」来算访存；同时 FP8 KV cache 每 token 占 656 字节（见 [u5-l1](u5-l1-fp8-kvcache-format.md)），不是 `d` 个 bf16。这套逻辑写在测试工具里：

[tests/lib.py:367-398](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/lib.py#L367-L398) `count_flop_and_mem_vol_for_decode`：`num_attended_tokens` 按 topk 总和算算量，`num_retrieved_tokens` 用 `unique()` 去重算访存，`kv_token_size = 656 if d_qk==576 else 576` 反映 FP8 布局。

两条公式的对照：

| 维度 | dense benchmark（`bench_flash_mla.py`）| sparse decode（`lib.py`）|
| --- | --- | --- |
| 算量 token 数 | `total_seqlens`（全部 KV）| `num_attended_tokens`（topk 之和）|
| 访存 token 数 | 同 `total_seqlens` | `num_retrieved_tokens`（**去重**后）|
| 每 token 字节 | `d` 个 bf16 = `2d` 字节 | FP8：656 或 576 字节 |
| V 是否单算 | 否（K/V 同源）| 否（并入 KV token 字节）|

#### 4.2.4 代码实践

**实践目标**：对一组真实 shape 手算理论 TFLOPS / GB/s，判断它落在 compute-bound 还是 memory-bound 区，并与博客公开数据对照。

**给定 shape**（取自 `shape_configs` 的一格）：\(b=128,\ s_q=1,\ \text{seqlen}=8192,\ h_q=128,\ h_{kv}=1,\ d=576,\ d_v=512\)，bf16，假设测得 `perf = 0.5 ms`。

**操作步骤**（纸笔计算）：

1. `total_seqlens ≈ b·seqlen = 128·8192 = 1{,}048{,}576`（脚本里 seqlen 实际随 batch 略增，此处取均值近似）。
2. FLOPS：
   \[
   1 \cdot 1{,}048{,}576 \cdot 128 \cdot (576+512) \cdot 2
   = 1{,}048{,}576 \cdot 128 \cdot 1088 \cdot 2
   \approx 2.92\times10^{11}
   \]
3. bytes：
   \[
   (1{,}048{,}576\cdot1\cdot576 + 128\cdot1\cdot128\cdot576 + 128\cdot1\cdot128\cdot512)\cdot2
   \approx (6.04\times10^8 + 9.44\times10^6 + 8.39\times10^6)\cdot2
   \approx 1.24\times10^{9}\ \text{字节}
   \]
   （KV 项 `6.04e8` 占绝对大头，印证「访存被长 KV 主导」。）
4. TFLOPS \(= 2.92\times10^{11}/10^{9}/0.5 \approx 584\) TFLOPS。
5. GB/s \(= 1.24\times10^{9}/10^{6}/0.5 \approx 2480\) GB/s ≈ 2.48 TB/s。
6. 算术强度 \(= \text{FLOPS}/\text{bytes} = 2.92\times10^{11}/1.24\times10^{9} \approx 235\) FLOP/byte。对照 H800 平衡点 \(I^{*}\approx258\)（见 [u3-l1](u3-l1-compute-bound-analysis.md)），235 略低于 258，说明该 shape **接近边界、略偏 memory 侧**——但 TFLOPS 已达 584，接近博客宣称的 660 TFlops 量级。

**需要观察的现象**：TFLOPS（584）远比 GB/s 占峰值比例高（584/865 ≈ 67% vs 2.48/3.35 ≈ 74%），二者都接近峰值的六七成，说明 kernel 整体已较充分地利用了硬件——这与博客「80% Tensor Core 利用率、3 TB/s 带宽」的结论一致量级。

**预期结果**：手算的 TFLOPS ≈ 580、GB/s ≈ 2500，与博客公开的 dense decode 性能（compute-bound 区 580→660 TFlops）同量级。若你在真实 H800 上跑 `python benchmark/bench_flash_mla.py --target flash_mla`，`flash_mla` 那一行打印的数字应当落在这一量级。

> 待本地验证：上述耗时 `0.5 ms` 为假设定值；真实耗时取决于硬件，需在 GPU 上运行脚本获得。

#### 4.2.5 小练习与答案

**Q1**：如果把 `d_v` 从 512 增大到 1024（假设性地），TFLOPS 和 GB/s 哪个涨得更快？

**答**：FLOPS 与 `d+d_v` 成正比，会显著增大；bytes 里输出项 `b·s_q·h_q·d_v` 也增大，但 KV 项 `total_seqlens·h_kv·d` 不变且占大头，所以 bytes 涨幅小。结论：算术强度上升，TFLOPS 涨得更快，kernel 更偏 compute-bound。

**Q2**：为什么 sparse 解码的 `count_flop_and_mem_vol_for_decode` 要用 `unique()` 算访存 token 数，而 dense benchmark 不用？

**答**：sparse 的 `indices` 可能多次指向同一个 KV token；真实 kernel 靠 L2 cache 让重复访问只读一次 DRAM，所以理论访存应按「唯一 token 数」算才贴近实测 GB/s。dense 则遍历连续 KV，无重复，直接用 `total_seqlens`。

---

### 4.3 kernelkit 性能测量工具箱

#### 4.3.1 概念说明

[前置讲义 u8-l2](u8-l2-test-harness.md) 已经介绍过 `bench_kineto` 与 `check_is_allclose` 在测试里的用法。本讲从「性能测量」视角把它们和 `bench_flash_mla.py` 用的 `triton.testing.do_bench` 区分开，并补充 u8-l2 没展开的细节。

kernelkit 提供两套测时工具，定位不同：

| 工具 | 原理 | 粒度 | 适用场景 |
| --- | --- | --- | --- |
| `bench_kineto` | PyTorch profiler（CUPTI）采每个 kernel 的时间区间 | **按 kernel 名** | 想拆分「主 kernel 多少 us、combine 多少 us」，或算端到端跨 kernel 耗时 |
| `bench_by_cuda_events` | `torch.cuda.Event` 计时 | **按 callable** | 想把多个候选实现**交错**跑，抵消热降频/干扰，做公平 A/B |
| `triton.testing.do_bench`（外部）| Triton 自带，类似 cuda event | 按 callable | benchmark 脚本的轻量测时 |

#### 4.3.2 核心流程

`bench_kineto` 的核心思路是「用 PyTorch profiler 抓取一段 active 窗口内所有 kernel 的时间区间，再按名字筛选你想看的那个」：

```text
with profiler(schedule=wait0/warmup1/active1):
    第 0 轮：warmup（不计入），末尾 step()
    第 1 轮：先跑 marker kernel（做时间锚点）
            for _ in range(num_tests):
                flush L2（8GB memset）   # 保证每次冷启动
                fn()                      # 被测函数
解析 events：丢弃 marker 之前的，按 kernel 名收集 (start,end)
返回 BenchKinetoRawResult
```

随后用 `get_kernel_time("名字子串")` 取某个 kernel 的平均耗时，或用 `get_e2e_time(start, end)` 取「最后一个 end kernel 结束 − 第一个 start kernel 开始」的端到端跨度。

#### 4.3.3 源码精读

**L2 刷缓存**是严肃测时的关键。`bench_kineto` 每次调用被测函数前都做一次约 8GB 的 memset 把 L2 里的残留数据冲掉：

[tests/kernelkit/bench.py:110-121](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/kernelkit/bench.py#L110-L121) `flush_l2_size = int(8e9 // 4)` 个 int32 = 8GB；每次 `fn()` 前 `torch.empty(...).zero_()` 强制写一遍，确保 KV 不残留在 L2，测出的 GB/s 才真实。

**marker 锚点**用来剔除 warmup 阶段的噪声事件：

[tests/kernelkit/bench.py:116-146](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/kernelkit/bench.py#L116-L146) 第 1 轮先跑 `profiler_range_start_marker_kernel`，解析时找到该事件、丢弃它之前的所有事件，只统计 active 窗口内的 kernel。

**按名取时与端到端**：

[tests/kernelkit/bench.py:45-75](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/kernelkit/bench.py#L45-L75) `get_kernel_times` 用子串匹配 kernel 名、对运行次数取整校验（`run_cnt % num_tests == 0`）后求平均；`get_kernel_time` 是其单名便捷封装。

[tests/kernelkit/bench.py:77-100](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/kernelkit/bench.py#L77-L100) `get_e2e_time` 计算「第 i 轮里最后一个 end kernel 的结束时间 − 该轮第一个 start kernel 的开始时间」，再对 `num_tests` 轮求平均——这正是把 `splitkv` 与 `combine` 两个 kernel 串成「解码端到端耗时」的方法（见下方真实调用）。

**profiler 冲突检测**：nsys / ncu / compute-sanitizer 自身会占用 CUPTI，与 PyTorch profiler 互斥，所以检测到这些工具时直接退化为「返回 1 秒」占位，避免崩溃：

[tests/kernelkit/utils.py:21-31](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/kernelkit/utils.py#L21-L31) `is_using_profiling_tools` 靠环境变量 `NSYS_PROFILING_SESSION_ID` / `NV_COMPUTE_PROFILER_PERFWORKS_DIR` / `NV_SANITIZER_INJECTION_PORT_RANGE` 判断。

**真实调用样例一（dense decode 拆单 kernel）**：

[tests/test_flash_mla_dense_decoding.py:175-192](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/test_flash_mla_dense_decoding.py#L175-L192) 用 `bench_kineto(run_flash_mla, 10).get_kernel_time("flash_fwd_splitkv_mla_kernel")` 取主 kernel 耗时，再手写 FLOPS/bytes 算 TFLOPS/GB/s——和 4.2 节公式完全一致（注意它用 `mean_attended_seqlens` 取均值）。

**真实调用样例二（sparse decode 端到端，含可选 combine）**：

[tests/test_flash_mla_sparse_decoding.py:173-202](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/test_flash_mla_sparse_decoding.py#L173-L202) 先 `get_kernel_time` 分别取 `flash_fwd_splitkv_mla_fp8_sparse_kernel` 与 `flash_fwd_mla_combine_kernel` 的单 kernel 耗时；若 combine 存在则 `get_e2e_time(splitkv, combine)` 算端到端，否则用 splitkv 单 kernel 耗时兜底。这正对应 [u4-l1/u4-l2](u4-l2-combine-kernel.md) 讲的「单 split 早退、无 combine」契约。

**另一套工具 `bench_by_cuda_events`** 适合「多候选实现交错跑」：

[tests/kernelkit/bench.py:166-205](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/kernelkit/bench.py#L166-L205) 接受一组 kernel 可调用对象，每轮把它们**依次**各跑一遍、用独立的 start/end event 计时。这种「交错」能让所有候选共享同一热状态与干扰背景，比「先跑完 A 再跑 B」更公平——这也是它在 A/B 对比里比 `do_bench` 更受青睐的原因。注意它的 L2 清理缓冲只有 256MB（`int(256e6//4)`），策略比 `bench_kineto` 温和。

#### 4.3.4 代码实践

**实践目标**：理解 `get_e2e_time` 的「最后一个结束 − 第一个开始」语义，能解释它为何能正确测量多 kernel 串联。

**操作步骤**：

1. 阅读 [tests/kernelkit/bench.py:94-99](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/kernelkit/bench.py#L94-L99)。
2. 假设一轮 decode 在 active 窗口内产生了这样的 kernel 时间线（微秒）：
   - `splitkv`: 启动于 100，结束于 130（主 kernel）
   - `combine`: 启动于 128，结束于 135（与 splitkv 经 PDL 部分重叠，见 [u3-l3](u3-l3-seesaw-and-tma-pipeline.md) / [u4-l2](u4-l2-combine-kernel.md)）
3. 用 `get_e2e_time("splitkv", "combine")` 计算：取 `combine` 的**最后一个**结束（135）− `splitkv` 的**第一个**开始（100）= 35 us。
4. 对比 `get_kernel_time("splitkv") + get_kernel_time("combine")` = 30 + 7 = 37 us。

**需要观察的现象**：端到端（35）小于两单 kernel 之和（37），差额 2 us 正是 PDL 带来的重叠收益。

**预期结果**：你能复述 `get_e2e_time` 的定义并解释「为什么它 ≥ 任一单 kernel、而 ≤ 两单 kernel 之和」——因为它量的是真实墙钟跨度，自动包含了重叠与空隙。

> 待本地验证：上述微秒数字为教学假设；真实重叠量需在 GPU 上用 nsys 时间线观察。

#### 4.3.5 小练习与答案

**Q1**：为什么 `bench_kineto` 默认 `flush_l2=True`，而 `bench_by_cuda_events` 只用 256MB 小缓冲？

**答**：`bench_kineto` 想测「真实冷启动」带宽（KV 不在 L2），所以用 8GB 大缓冲彻底冲刷；`bench_by_cuda_events` 侧重多候选公平对比而非绝对带宽，温和清理即可，过大缓冲会拉长每轮、降低采样密度。

**Q2**：如果被测函数里 kernel 名含 `mla` 的有多个，`get_kernel_time("mla")` 会怎样？

**答**：会触发 `bench.py:38-39` 的 `Multiple match` 错误（除非传 `allow_multiple_match=True`）。设计上强制你给出唯一子串，避免把多个 kernel 的时间混算。

---

### 4.4 NVCC 性能 flag 与博客优化技巧汇总

#### 4.4.1 概念说明

性能不只来自 kernel 算法，还来自「怎么编译」。`setup.py` 里为 nvcc 配了一长串 flag，分两类：

- **性能 flag**：`--use_fast_math`、`--expt-relaxed-constexpr`、`--expt-extended-lambda` 等，让编译器生成更快（可能略损精度）的代码。
- **诊断 flag**：`-lineinfo`、`--source-in-ptx`、`--ptxas-options=-v,...` 等，保留调试信息并在寄存器溢出 / FP64 使用时告警，让你能定位性能回退。

同时，两篇博客公开了 FlashMLA 在算法层的优化技巧。本节把这些技巧与「实测指标」对应起来，形成一张全景。

#### 4.4.2 核心流程

`setup.py` 的 nvcc 参数构造流程：

```text
get_arch_flags()         →  -gencode sm_90a / sm_100f（见 u1-l2）
get_features_args()      →  -DFLASH_MLA_DISABLE_FP16 等宏
get_nvcc_thread_args()   →  --threads 32（并行编译）
固定 nvcc flag 列表       →  -O3 / --use_fast_math / ptxas 诊断 / -lineinfo ...
拼接 → 传入 CUDAExtension(extra_compile_args={"nvcc": [...]})
```

#### 4.4.3 源码精读

**完整的 nvcc flag 块**：

[setup.py:108-124](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/setup.py#L108-L124) 是性能与诊断 flag 的总入口。逐项含义：

| flag | 作用 | 对性能/稳定性的意义 |
| --- | --- | --- |
| `--use_fast_math` | 启用 fast math（更快的 exp/sqrt/sin 等，flush-to-zero）| 直接提升 CUDA Core 上的标量运算（如 softmax 的 exp）；代价是精度略降，FlashMLA 已在测试里用容差兜底（见 u8-l2）|
| `-U__CUDA_NO_HALF_OPERATORS__` 等 4 个 | 取消 PyTorch 默认禁用 half/bf16 运算符的宏 | 让 `__half`/`__nv_bfloat16` 的算术与转换指令正常生成，是 bf16/fp8 kernel 能写起来的前提 |
| `--expt-relaxed-constexpr` / `--expt-extended-lambda` | 放宽 constexpr 限制、允许 extended lambda | 让 CUTLASS 式的模板元编程与 device lambda 可用，间接帮助编译器做更激进的内联与特化 |
| `--ptxas-options=-v` | 打印每 kernel 的寄存器/smem 用量 | 性能调优的眼睛：一眼看出占用率与寄存器压力 |
| `--register-usage-level=10` | ptxas 专家级旋钮，引导寄存器分配水平（取值 0–31）| 在「每线程寄存器数 / SM 占用率 / 是否溢出」三者间平衡；配合 `-v` 与 `--warn-on-spills` 调优 |
| `--warn-on-spills` | 寄存器溢出到 local memory（实为 DRAM）时告警 | **稳定性护栏**：spill 会让 kernel 暴跌数倍，必须第一时间发现 |
| `--warn-on-local-memory-usage` | 任何 local memory 使用都告警 | 同上，更严格 |
| `--warn-on-double-precision-use` | FP64 使用告警 | FP64 在 consumer/降频 GPU 上慢几十倍，kernel 内不该出现，告警防回归 |
| `-lineinfo` / `--source-in-ptx` | 保留行号与源码信息 | 供 ncu / Nsight Compute 把热点定位回 `.cu` 源码行 |

> 关于 `--register-usage-level=10`：它是 ptxas 的专家级（expert）旋钮，影响寄存器分配策略，精确的内部语义 NVIDIA 未在官方手册完整公开。可以确定的是：它和 `--warn-on-spills`、`-v` 一起，构成「调寄存器 → 看是否溢出 → 看占用率」的闭环。FlashMLA 选 10 是经验调优值，**不是放之四海皆准**——改 kernel 后应重新用 `-v` 验证寄存器/sm 占用，必要时再调。

**博客优化技巧全景**（dense decode，[docs/20250422-new-kernel-deep-dive.md](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/docs/20250422-new-kernel-deep-dive.md)）：

[docs/20250422-new-kernel-deep-dive.md:11-15](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/docs/20250422-new-kernel-deep-dive.md#L11-L15) compute-bound 理论分析（承接 u3-l1）：算术强度 \( \approx 2h_qs_q \)，平衡点 \(h_qs_q\approx128\)。

[docs/20250422-new-kernel-deep-dive.md:48-61](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/docs/20250422-new-kernel-deep-dive.md#L48-L61) 汇总四类技巧：(1) **细粒度 TMA copy–GEMM 流水**（把 64×576 的 K 块拆成 9 个 64×64 子块，搬一块算一块，见 u3-l3）；(2) **cache hint** `EVICT_FIRST`（流式数据用完即出 L2，提升命中率）；(3) **Programmatic Dependent Launch (PDL)**（让 `splitkv` 与 `combine` 重叠，对应 4.3 的端到端测时）；(4) **Tile Scheduler**（负载均衡，见 u4-l3）。最终达到 80% Tensor Core 利用率、3 TB/s 带宽、最高 660 TFlops。

**FP8 sparse decode 技巧**（[docs/20250929-hopper-fp8-sparse-deep-dive.md](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/docs/20250929-hopper-fp8-sparse-deep-dive.md)）：

[docs/20250929-hopper-fp8-sparse-deep-dive.md:17-25](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/docs/20250929-hopper-fp8-sparse-deep-dive.md#L17-L25) 时钟周期分析：单 token 反量化约 50 cycle，而 64 头 MMA 仅约 34 cycle，故 **dequantization-bound**（承接 u5-l2）。

[docs/20250929-hopper-fp8-sparse-deep-dive.md:48-52](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/docs/20250929-hopper-fp8-sparse-deep-dive.md#L48-L52) crossover + DSM 把反量化砍半后，compute-bound 配置下达 410 TFLOPS（无 crossover 仅 250），topk 增大可至 460 TFLOPS。

把这些技巧与「实测指标」串起来，就是一张调优地图：

| 技巧 | 解决的瓶颈 | 体现的指标提升 |
| --- | --- | --- |
| seesaw 调度（u3-l3）| 单输出矩阵放不下两份 → 无法 ping-pong | 重叠 CUDA/Tensor Core，TFlops 580→660 |
| 细粒度 TMA 流水（u3-l3）| 访存延迟 | 提升 Tensor Core 占空比 |
| EVICT_FIRST cache hint | L2 命中率 | GB/s 接近 3 TB/s |
| PDL（u4-l2）| splitkv↔combine 串行 | `get_e2e_time` 下降 |
| crossover + DSM（u5-l3）| FP8 反量化 50 cycle > MMA 34 cycle | TFlops 250→410 |

#### 4.4.4 代码实践

**实践目标**：理解关键 flag 对「性能」与「稳定性」的双重作用，能在加错 flag 时预判后果。

**操作步骤**：

1. 假设你把 `--register-usage-level=10` 改成 `--register-usage-level=0`（最严的寄存器限制），重新编译（`FLASH_MLA_DISABLE_SM100=1 pip install -e .` 仅编 sm90 加快）。
2. 用 `--ptxas-options=-v` 观察：每线程寄存器数会下降，SM 占用率（occupancy）上升。
3. 但因为 `--warn-on-spills` 仍在，若某个 kernel 寄存器放不下被赶到 local memory，编译期会打印 spill 警告。
4. 跑 dense decode 性能用例（u8-l2 / test_flash_mla_dense_decoding 的 performance 分支），对比 TFLOPS。

**需要观察的现象**：

- 占用率上升**不一定**带来加速——如果发生 spill，TFLOPS 反而暴跌（local memory 走 DRAM，慢几十倍）。这正是 `--warn-on-spills` 的价值：它把「隐性性能杀手」变成「显性编译告警」。
- `--warn-on-double-precision-use` 则防止你不小心在 kernel 里写了 `double` 字面量（如 `1.0` 而非 `1.0f`），这类笔误会让 FP64 路径触发、性能骤降。

**预期结果**：你能口头复述「`register-usage-level` 调节占用率与 spill 的权衡、`warn-on-spills` 是稳定性护栏、`warn-on-double-precision-use` 防性能地雷」。若实测 spill 告警出现，应回退该 flag 或重写 kernel 降寄存器压力。

> 待本地验证：实际寄存器数与占用率需在 GPU 机器上编译并读 `-v` 输出确认。

#### 4.4.5 小练习与答案

**Q1**：`--use_fast_math` 和测试容差（u8-l2 讲的 abs/rel/cos_diff）有什么关系？

**答**：`--use_fast_math` 让 exp/sqrt 等更快但略不精确，会引入额外数值误差。FlashMLA 在测试里用 `check_is_allclose` 的三重容差（含 cos_diff 全局兜底）来吸收这部分误差——即「性能 flag 放宽精度、测试用容差守住正确性」是配套设计的。

**Q2**：为什么 `-lineinfo` 和 `--source-in-ptx` 不影响运行性能，却很重要？

**答**：它们只在二进制里附加调试/源码映射信息，不改变生成的指令，所以运行时几乎零开销。但它们让 ncu / Nsight Compute 能把每个热点的 stall 原因、寄存器占用定位回具体源码行——这是性能调优不可或缺的「导航」。生产构建通常会保留 `-lineinfo`。

---

## 5. 综合实践

**任务**：组装一条「测一次 dense decode、并解读结果」的最小闭环，把本讲四个模块串起来。

**要求**：

1. **选 shape**：取 \(b=128, s_q=1, \text{seqlen}=8192, h_q=128, h_{kv}=1, d=576, d_v=512\)。
2. **算理论值**（模块 4.2）：手算 FLOPS、bytes、TFLOPS（假设 perf=0.5ms）、GB/s、算术强度，判定 bound。
3. **选测时工具**（模块 4.3）：说明若想拆分「主 kernel vs combine」该用 `bench_kineto` + `get_kernel_time`，若想和 flash_infer 公平对比该用 `bench_by_cuda_events` 或 `bench_flash_mla.py` 的 `--compare`。
4. **解读 flag**（模块 4.4）：说明编译该 kernel 时 `--register-usage-level=10` + `--warn-on-spills` 如何保证它不因寄存器压力退化。
5. **对照博客**（模块 4.1/4.4）：把实测 TFLOPS 与博客的 660 TFlops 对照，估算 Tensor Core 利用率。

**参考骨架**（无 GPU 时作为可读伪代码）：

```python
# 示例代码（非项目原有）：把本讲四步串成一个解读函数
def interpret_dense_decode_perf(b, s_q, seqlen, h_q, h_kv, d, dv, perf_ms):
    total_seqlens = b * seqlen
    FLOPS   = s_q * total_seqlens * h_q * (d + dv) * 2          # 对应 bench_flash_mla.py:443
    bytes_  = (total_seqlens*h_kv*d + b*s_q*h_q*d + b*s_q*h_q*dv) * 2  # :444，V 不单算
    tflops  = FLOPS / 1e9 / perf_ms
    gBps    = bytes_ / 1e6 / perf_ms
    ratio   = FLOPS / bytes_                                     # 算术强度
    bound   = "compute" if ratio >= 258 else "memory"            # H800 平衡点，见 u3-l1
    return {"tflops": tflops, "gBps": gBps, "bound": bound}

# 对 b=128,s_q=1,seqlen=8192,h_q=128,h_kv=1,d=576,dv=512,perf=0.5ms:
# → 约 tflops≈584, gBps≈2480, bound≈memory(边界)
```

**预期产出**：一段文字说明，能讲清「这个 shape 理论上接近 memory 边界、实测若达 ~584 TFlops 则 Tensor Core 利用率约 584/865≈67%、要进一步压榨需检查 `register-usage-level` 是否引发 spill」。

> 待本地验证：所有绝对数字依赖真实 GPU 实测耗时，骨架仅供流程演示。

## 6. 本讲小结

- **多实现 benchmark**：`bench_flash_mla.py` 用 `FUNC_TABLE` 把 torch/flash_mla/flash_infer/triton 四种实现统一签名，先 `assert_close` 校验、再 `triton.testing.do_bench` 测时，结果落 CSV 供 `visualize.py` 画图。
- **FLOPs/bytes 公式**：\(\text{FLOPS}=s_q\cdot\text{total}\cdot h_q\cdot(d+d_v)\cdot2\)，bytes 里 **V 不单算**（MLA 中 K/V 同源）；除以 `1e9·ms` 与 `1e6·ms` 分别得 TFLOPS 与 GB/s。sparse 解码用 `lib.py` 的去重 + FP8 字节数公式。
- **kernelkit 两套工具**：`bench_kineto`（CUPTI，按 kernel 名拆分 + 8GB L2 刷缓存 + `get_e2e_time` 串联多 kernel）适合拆解；`bench_by_cuda_events`（cuda event，多候选交错）适合公平 A/B。二者都检测 nsys/ncu 避免冲突。
- **NVCC 性能 flag**：`--use_fast_math` 提速标量运算、`--register-usage-level=10` 平衡占用率与 spill、`--warn-on-spills`/`--warn-on-double-precision-use` 是稳定性护栏、`-lineinfo`/`--source-in-ptx` 服务于 ncu 定位。
- **技巧↔指标对应**：seesaw/TMA 流水/cache hint/PDL 把 dense decode 推到 ~660 TFlops；crossover+DSM 把 FP8 sparse 从 dequant-bound 解放、达 410+ TFlops。所有技巧的效果最终都通过本讲的 TFLOPS/GB/s 与 `get_e2e_time` 来量化验证。

## 7. 下一步学习建议

- 若你想动手把指标跑起来：在 H800/B200 上 `python benchmark/bench_flash_mla.py --compare`（baseline=torch, target=flash_mla），对照本讲手算值。
- 若你想深入「拆 kernel 时间线」：用 nsys 跑 `tests/test_flash_mla_sparse_decoding.py` 的性能用例，观察 `splitkv` 与 `combine` 的真实重叠，验证 4.3.4 的 `get_e2e_time` 语义。
- 若你想继续 Unit 8 的体系：本讲是 Unit 8 最后一篇「工具与测量」收尾，下一步进入 [Unit 9 架构取舍与二次开发](u9-l1-arch-tradeoffs-sm90-sm100.md)，把性能视角与「为什么这样设计」的架构视角合流。
- 推荐精读：`benchmark/bench_flash_mla.py` 全文（理解一个完整 benchmark 脚手架）、`tests/kernelkit/bench.py` 全文（理解严肃 GPU 测时的所有坑：L2 刷、warmup、profiler 冲突、按名取时）。
