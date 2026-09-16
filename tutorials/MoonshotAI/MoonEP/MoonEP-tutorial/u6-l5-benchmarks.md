# u6-l5 基准测试：度量与对比方法

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `benchmarks/` 下四个基准脚本各自的度量对象与适用场景。
2. 理解 MoonEP 基准的计时方法学：CUDA 事件计时、CUDA Graph 消除 launch 开销、跨 rank 均值、以及「算子自带跨 rank 屏障 → prefetch 需手动补 `inter_rank_sync`」这类细节。
3. 逐个写出十一个被测算子的字节计数公式与带宽口径（逻辑 payload、本地 HBM 总流量、瓶颈 rank NVLink 流量三种口径的区别）。
4. 掌握用 `bias_ratio`（对数正态 sigma）与反向解出的 MaxVio 目标值扫描路由不均衡的方法。
5. 能看懂 `bench_vs_deepep.py` 如何构造一次公平的跨库对拍，并复现 README 中「MoonEP vs DeepEP v2」的三条结论。

## 2. 前置知识

本讲不再深入内核实现，但默认你已完成 u4/u5/u6 前三讲。这里补几个度量侧的概念：

- **CUDA 事件计时**：`torch.cuda.Event(enable_timing=True)` 在 GPU 流上打两个时间戳，`elapsed_time()` 返回毫秒。它测的是 GPU 侧时间，不含 host 发射延迟——除非发射延迟大到让 GPU 空转。
- **CUDA Graph 计时**：把 N 次内核发射捕获进一张图，一次 replay 完成全部执行。这样做是为了剥离「每次 launch 的 Python/PyTorch 开销」（MoonEP 的 CuTe DSL 每次发射还要走 JIT 宿主编排和 `make_ptr`），让计时只反映内核本身。代价是 profiling 工具（NCU）看不到被图隐藏的单个内核，所以脚本都保留了 `--no-graph`/`--no-cudagraph` 回退开关。
- **跨 rank 均值**：分布式基准里每个 rank 各自测自己的事件对，`all_gather` 后取平均。通信是集合操作，任何一个 rank 慢，整体就慢，所以「平均每个 rank 的耗时」是对集合耗时的无 host 同步近似。
- **瓶颈 rank（木桶效应）**：预取、梯度归约这类操作各 rank 并行执行，总时间由最坏的 rank 决定。字节口径因此按「最坏 rank 搬了多少字节」计，而不是全体求和。
- **逻辑 payload 与真实传输**：MoonEP 的去重机制使同一 token 落到同一目的 rank 时只传一份 hidden 行。但 dispatch/combine 的带宽公式仍按 `S·K·H·2`（未去重）计——这是「有效带宽」口径：以用户视角的工作量除以时间。真实传输量更少，去重比例单独作为 `dedup_ratio` 列报告。
- **maxvio 回顾**（u1-l1）：

  \[ \text{maxvio} = \max_e \frac{T_e}{\bar{T}} - 1 \]

  其中 \( T_e \) 是路由到专家 \( e \) 的 token 数，\( \bar{T} \) 是完美均衡下的期望值。maxvio = 0 表示完美均衡，maxvio = k 表示最热专家承担 (k+1) 倍负载。
- **bias_ratio**：`tests/generate_topk_routing.py` 中对数正态专家 logit 分布的 sigma。sigma 越大路由越偏；0.1 接近均衡、1 是典型 dropless-MoE 偏斜、5 接近退化。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `benchmarks/bench_comm.py` | 算子级基准：扫描 H/K/EP/bias_ratio，逐个计时十一个 MoonEP 内核并输出 CSV |
| `benchmarks/bench_vs_deepep.py` | 对比基准：MoonEP 公共 API 端到端 vs DeepEP v2 elastic，按 MaxVio 目标扫描，可出堆叠柱状图 |
| `benchmarks/bench_prefetch.py` | 预取专用基准：单发起者（rank0）拉取远程专家，覆盖 bf16/int8/uint8 与 K3 多部件层，可测跨节点 fabric |
| `benchmarks/bench_grad_reduce.py` | 梯度归约专用基准：单 owner（rank0）归约远程槽梯度，扫描槽位密度与专家形状 |
| `tests/generate_topk_routing.py` | 前两个基准共享的路由生成器：对数正态偏置 top-k 采样 |
| `README.md`（Performance 章节） | H20/EP=8 上的官方对比结论，是 `bench_vs_deepep.py` 的产出物 |

另外会顺带引用 `moonep/buffer.py` 中三个辅助函数（`create_nvl_dist_tensor` / `create_nvl_single_owner_tensor` / `pad_dim0_for_alignment`，见 u2-l2）在基准里搭测试池的方式。

## 4. 核心概念与源码讲解

### 4.1 计时方法学：CUDA 事件、CUDA Graph 与跨 rank 均值

#### 4.1.1 概念说明

三个专用基准共享同一套计时骨架（`bench_prefetch`/`bench_grad_reduce` 各自内联了一份），`bench_comm.py` 把它提炼成 `time_gpu_op`，`bench_vs_deepep.py` 用只有 eager 模式的 `time_op` 变体。这个模块解决的问题是：**如何测准一个跨 rank 通信内核的时间**。难点有三个：

1. 内核由 Python 侧的 CuTe DSL 宿主代码发射，launch 开销可能与内核本身同量级；
2. 通信内核的耗时必须对所有 rank 一致地测量，且循环内不能插入 host 同步（否则测的是同步点）；
3. 连续迭代之间，各 rank 的相对进度可能漂移，需要某种机制让迭代串行化。

#### 4.1.2 核心流程

`time_gpu_op` 的流程：

```text
warmup 次预运行（同时完成 JIT 编译）
torch.cuda.synchronize() + dist.barrier()     # 全员对齐
若 cudagraph=True:
    捕获 iters 次 launch_fn() 进 CUDA Graph
    再次 synchronize + barrier
    start.record() → graph.replay() → end.record()
否则:
    start.record() → 循环 iters 次 launch_fn() → end.record()
end.synchronize()
local_us = elapsed_time(ms) / iters × 1e3     # 本 rank 每迭代微秒
all_gather 各 rank 的 local_us → 取均值返回
```

迭代串行化不靠额外同步，而是靠被测内核**自带的跨 rank 屏障**（dispatch/combine/grad_reduce 内部的 `cross_rank_barrier`，见 u3-l6）：上一次调用的出口屏障天然挡住下一次调用的入口。唯一例外是 `launch_prefetch`——它没有跨 rank 同步，所以基准给它手动配了一个小 `inter_rank_sync` 内核。

#### 4.1.3 源码精读

计时骨架本体在 [benchmarks/bench_comm.py:120-165](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_comm.py#L120-L165)：这段 `time_gpu_op` 的 docstring 明确说了 CUDA Graph 模式的动机——「removing the per-launch Python overhead (cute JIT orchestration + make_ptr) so the timing reflects the kernels alone」，并指出 cooperative launch 与跨 rank NVLink 屏障都是可捕获的，replay 复用捕获时的输出张量地址。末尾 [benchmarks/bench_comm.py:160-165](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_comm.py#L160-L165) 把本 rank 微秒值 `all_gather` 后取均值，即「跨 rank 均值」口径的落点。

prefetch 补屏障的配对写法在 [benchmarks/bench_comm.py:351-356](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_comm.py#L351-L356)：

```python
def _prefetch_call():
    launch_prefetch(weights_full, prefetch_buf, etc_pad[group_rank],
                    num_sms=num_sms)
    launch_inter_rank_sync(ctx)
```

注释解释了原因：prefetch 自身没有 inter-rank 同步，配这个小内核是为了让连续迭代在各 rank 间保持串行——与其他算子内建屏障扮演相同角色。

`bench_vs_deepep.py` 的共享 harness 是 [benchmarks/bench_vs_deepep.py:60-77](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_vs_deepep.py#L60-L77) 的 `time_op`：同样的 warmup → sync → barrier → 事件对 → all_gather 均值结构，但只有 eager 循环，没有 graph 分支——因为对比对象 DeepEP 也按 eager 调用走公共 API，两边必须用同一种计时方式才公平（模块 docstring 第 14-16 行称之为「identical timing harness」）。

两个专用基准把同一骨架内联在 `bench_case` 里，并保留 NCU 回退：[benchmarks/bench_prefetch.py:207-226](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_prefetch.py#L207-L226) 的注释写明「`--no-graph` keeps plain stream launches so tools like NCU can intercept each kernel (graph capture/replay hides launches from kernel filters)」。`bench_grad_reduce.py` 在 [benchmarks/bench_grad_reduce.py:139-159](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_grad_reduce.py#L139-L159) 有完全相同的一段。

最后，输出列里所有浮点数都经 [benchmarks/bench_comm.py:92-100](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_comm.py#L92-L100) 的 `fmt4` 格式化为恰好 4 位有效数字（`181.0 -> '181.0'`，`1486.65 -> '1487'`）——综合实践里我们会用它来校验 CSV 的自洽性。

#### 4.1.4 代码实践

**实践目标**：在一个单卡环境（任何一块 GPU 即可，不需要多卡 NVLink）复现「eager vs CUDA Graph」的计时差异，验证 graph 计时确实剥离了 launch 开销。

**操作步骤**（示例代码，非项目原有代码）：

```python
# time_harness_demo.py — 复现 time_gpu_op 的两种计时模式（示例代码）
import torch

def time_op(fn, warmup, iters, cudagraph=True):
    for _ in range(warmup):
        fn()
    torch.cuda.synchronize()
    start = torch.cuda.Event(enable_timing=True)
    end = torch.cuda.Event(enable_timing=True)
    if cudagraph:
        g = torch.cuda.CUDAGraph()
        with torch.cuda.graph(g):
            for _ in range(iters):
                fn()
        torch.cuda.synchronize()
        start.record(); g.replay(); end.record()
    else:
        start.record()
        for _ in range(iters):
            fn()
        end.record()
    end.synchronize()
    return start.elapsed_time(end) / iters * 1e3  # us

x = torch.ones(1024, 1024, device="cuda")
fn = lambda: x.add_(1.0)          # 一个极小的内核，launch 开销占比最大
print("eager :", time_op(fn, 5, 200, cudagraph=False), "us")
print("graph :", time_op(fn, 5, 200, cudagraph=True), "us")
```

**需要观察的现象**：内核越小，eager 与 graph 的差值越大；对 `add_` 这种微秒级内核，eager 计时可能数倍于 graph 计时。

**预期结果**：graph 计时显著小于 eager 计时，且随 iters 增大两者都趋于稳定。本环境无 GPU，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `time_gpu_op` 的计时循环里不能出现 `torch.cuda.synchronize()` 或 `dist.barrier()`？

**答案**：事件对 `start`/`end` record 在 GPU 流上，本就不受 host 影响；若循环内插入同步，每次迭代都会在 host 侧制造一个全局汇合点，把各 rank 的发射抖动、NCCL 集合通信延迟混进 GPU 时间。迭代串行化应交给内核自带的设备端跨 rank 屏障完成，host 只在 warmup 后和计时结束后同步。

**练习 2**：`bench_vs_deepep.py` 的 `time_op` 为什么不提供 CUDA Graph 模式？

**答案**：见 [benchmarks/bench_vs_deepep.py:10-16](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_vs_deepep.py#L10-L16)：对比基准的第一原则是两个库共用「identical timing harness」。DeepEP v2 走它自己的公共 API，MoonEP 走 Buffer 公共 API，双方都按框架真实的调用方式（eager 逐次发射）计时；若只有一边用 graph 剥离 launch 开销，对比就失去了同等条件。

**练习 3**：如果去掉 `_prefetch_call` 里的 `launch_inter_rank_sync(ctx)`，测出的 prefetch 时间可能怎样失真？

**答案**：各 rank 的 prefetch 内核不再互相等待，快的 rank 可提前进入下一次迭代，事件对只覆盖本 rank 的内核；同时不同 rank 对同一 owner 的远程读会失去节奏对齐，测得的「跨 rank 均值」既可能偏小（重叠了别的 rank 的迭代）也可能偏大（无法反映集合行为）。配套 sync 内核把每次迭代钉在全组同一节拍上，与其他算子内建屏障的语义一致。

### 4.2 算子级基准 bench_comm.py：扫描维度、算子拆分与带宽口径

#### 4.2.1 概念说明

`bench_comm.py` 是 MoonEP 的**算子级**基准：它不经过 `Buffer.dispatch`/`Buffer.combine` 公共入口，而是直接 `launch_*` 各内核（与 u1-l3 讲的「基准直接计时 launch_* 拆分算子成本」呼应），把一次 MoE 通信拆成十一个可独立度量的算子。它回答的问题是：**规划、派发、去重修补、归并、预取、梯度归约各占多少微秒，各自跑到多少带宽，随配置和不均衡度怎样变化**。

#### 4.2.2 核心流程

扫描空间由 `build_configs` 枚举（见 4.2.3），对每个配置：

1. 在 EP 子组内构造 `Buffer`，用共享种子 1234 生成偏置路由 `topk`/`tpe`；
2. `dist.all_reduce` 汇总全局每专家负载，算出 `load_max_mean`（mx/mean 列）；
3. 依次计时：planning → dispatch_fwd → epilogue_fwd → dispatch_bwd → epilogue_bwd → combine_prologue_fwd → combine_fwd → combine_prologue_bwd → combine_bwd → prefetch（配 sync）→ grad_reduce；
4. 从 `plan` 提取去重统计与 `experts_to_copy`，算出 `max_recv`/`max_send`；
5. 按各算子的字节公式换算带宽列，组装成一行结果；`buffer.destroy()` 后进入下一配置。

#### 4.2.3 源码精读

**扫描维度**在 [benchmarks/bench_comm.py:466-494](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_comm.py#L466-L494)：

- `H ∈ {3584, 7168}`，`K ∈ {8, 16}`，`S = 8192` 固定；
- `ep ∈ {4, 8}`：EP 子组从全局 world 中切出（[benchmarks/bench_comm.py:534-539](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_comm.py#L534-L539) 用 `dist.new_group(ranks=list(range(ep)))` 预建子组，不在组内的 rank 拿到 NULL 组句柄但必须集体参与创建）；
- `bias_ratio ∈ {0.1, 1.0, 5.0}`：对数正态 sigma 的三个档位；
- `E = 896` 固定，`epn = E/ep` 随组扩大而缩小（ep=4 时 224，ep=8 时 112）。

路由生成在 [benchmarks/bench_comm.py:180-191](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_comm.py#L180-L191)：`generate_topk_routing(S, K, E, R, bias_ratio, dev, 1234, rank=group_rank)`，种子 1234 共享给专家流行度（模拟真实训练中 gate 是全局的），`rank` 种子独立驱动每 token 抽样；随后 `global_tpe` 做 all_reduce，`max/mean` 得到 `load_max_mean`——这就是本配置实际达到的不均衡度，与输入的 bias_ratio 互为印证。生成器本体在 [tests/generate_topk_routing.py:24-44](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/generate_topk_routing.py#L24-L44)：两个 `torch.Generator` 分管共享态（对数正态 expert logit）与本地态（每 token 抽样），`bias_ratio == 0` 走 round-robin 均衡分支，否则 `logits = exp(normal(0, sigma, E))` 后无放回 `multinomial` 抽 top-k，最后 `bincount` 得到每专家 token 数 `tpe`。

**十一个算子的计时点**贯穿 [benchmarks/bench_comm.py:198-368](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_comm.py#L198-L368)，模块 docstring [benchmarks/bench_comm.py:17-35](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_comm.py#L17-L35) 逐一说明了每个算子的语义。几个关键编排决策：

- dispatch_fwd 带 `build_dedup_map=True`（[benchmarks/bench_comm.py:220-225](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_comm.py#L220-L225)）：新鲜规划路径要由 builder warps 物化去重三件套，这部分时间计入 dispatch_fwd；
- dispatch_bwd 用保存的 plan 且 `build_dedup_map=False`（[benchmarks/bench_comm.py:243-248](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_comm.py#L243-L248)），只传 hidden——即 u6-l3 四象限里的「combine bwd 用保存 plan 重新 dispatch」那一象限的内核级形态；
- epilogue 紧跟在对应 dispatch 之后计时（[benchmarks/bench_comm.py:227-238](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_comm.py#L227-L238)），让它读到刚写好的 NVL 状态，匹配真实 dispatch → epilogue 顺序；
- combine 侧的输入先一次性 staged 进 `hidden_buf_local`（[benchmarks/bench_comm.py:265-269](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_comm.py#L265-L269)，不计时的 setup），此后 prologue 反复原地归约，重复迭代做的内存工作完全相同（累加值会变，但读写字节数不变）；
- zero_copy=False 情况下的边界 `tensor.copy_` 是普通 torch op，**刻意不被基准计时**（docstring [benchmarks/bench_comm.py:58-60](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_comm.py#L58-L60)）——算子级基准只测 MoonEP 内核。

**预取/梯度归约的权重池搭建**在 [benchmarks/bench_comm.py:305-344](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_comm.py#L305-L344)，是 u2-l2 知识的直接应用：`create_nvl_dist_tensor` 造出每 rank 物理持有 `epn` 个专家的对称池；因 chunk 第 0 维要补到 VMM 粒度，全局专家 `e` 的物理行号要重映射为 `(e // epn) * padded_epn + e % epn`（[benchmarks/bench_comm.py:323-330](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_comm.py#L323-L330)），空槽保持 -1。从 `experts_to_copy` 统计出两个瓶颈量（[benchmarks/bench_comm.py:307-316](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_comm.py#L307-L316)）：

- `max_recv`：最坏接收 rank 的有效预取槽数（`valid.sum(dim=1).max()`）；
- `max_send`：把每个有效槽映射回其 owner（`// epn`）后 `bincount` 取 max——最坏 owner rank 被多少个槽远程读取。

**字节口径与带宽换算**集中在 [benchmarks/bench_comm.py:370-420](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_comm.py#L370-L420)，汇总成下表（\( G \) 为跨 rank 平均重复组数，\( D \) 为跨 rank 平均重复槽数，即代码里的 `mean_groups`/`mean_dups`，由 [benchmarks/bench_comm.py:384-400](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_comm.py#L384-L400) 的 all_gather 均值得到）：

| 算子 | 字节数公式 | 口径 |
| --- | --- | --- |
| dispatch_fwd / dispatch_bwd | \( S \cdot K \cdot H \cdot 2 \) | 每 rank 逻辑 payload（bf16），去重节省不计入 |
| combine_fwd / combine_bwd | \( S \cdot K \cdot H \cdot 2 \) | 同上 |
| epilogue（fwd/bwd） | \( (G + D) \cdot H \cdot 2 \) | 本地 HBM 总流量：读 \( G \) 个主行 + 写 \( D \) 个重复槽 |
| combine_prologue（fwd/bwd） | \( (2G + D) \cdot H \cdot 2 \) | 本地 HBM 总流量：读主行 + 重复，写回主行 |
| prefetch | \( M \cdot H \cdot H_p \cdot 2 \) | 瓶颈 rank 流量，\( M = \max(\text{max\_send}, \text{max\_recv}) \)，bf16 权重 |
| grad_reduce | \( M \cdot H \cdot H_p \cdot 4 \) | 同上，fp32 梯度 |

带宽统一为 `bytes / us * 1e6 / 1e9`（微秒 → 秒、字节 → GB），落在 [benchmarks/bench_comm.py:442-451](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_comm.py#L442-L451)。注意 CSV 里的 `dup_group_count`/`dup_loff_count` 列报告的是**本 rank**（打印结果的 rank 0）的值（[benchmarks/bench_comm.py:381-382](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_comm.py#L381-L382)），而带宽公式用的是跨 rank 均值——两列数字略有差异是正常的。`dedup_ratio` 列（[benchmarks/bench_comm.py:386](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_comm.py#L386)）是 `dst < 0` 的比例，即被去重省掉传输的条目占比，告诉你逻辑带宽口径比真实 NVLink 流量「虚高」了多少。

其余可调项：`--num-sms`（默认 32）、`--hp`（预取/归约的专家内维 \( H' \)，必须 128 倍数，默认 3072）、按维度过滤的 `--ep/--hidden/--topk/--unbalance-ratio`（方便 ncu/nsys 盯单个点，[benchmarks/bench_comm.py:513-518](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_comm.py#L513-L518)）、覆盖 E 的 `--experts`（[benchmarks/bench_comm.py:519-523](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_comm.py#L519-L523)）；以及环境变量 `MOONEP_NUM_SMS_DEDUP` 可覆盖 dispatch_epilogue/combine_prologue 的 SM 数（默认满卡，docstring [benchmarks/bench_comm.py:62-64](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_comm.py#L62-L64)）。36 列 CSV 表头在 [benchmarks/bench_comm.py:558-574](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_comm.py#L558-L574)。

#### 4.2.4 代码实践

**实践目标**：手工核算一个典型配置的全部字节公式，建立对数量级的直觉（无 GPU 也能做）。

**操作步骤**（示例代码）：

1. 取配置 `S=8192, K=8, H=7168, Hp=3072, E=896, ep=8`（即 `--ep 8 --hidden 7168 --topk 8` 那一行）；
2. 逐项计算：
   - `dispatch_fwd_bytes = 8192 × 8 × 7168 × 2`；
   - 假设实测 `dedup_ratio = 0.30`，估算真实 NVLink 传输 ≈ `(1 - 0.30) × dispatch_fwd_bytes`；
   - 假设 `G = 5000`、`D = 3000`（跨 rank 均值），算 epilogue 与 prologue 字节；
   - 假设 `max_recv = 60`、`max_send = 70`，算 prefetch 与 grad_reduce 字节；
3. 用假设耗时（如 dispatch_fwd 100 µs）换算 GB/s，对照公式 `bytes / us × 1e6 / 1e9`。

**需要观察的现象**：dispatch 逻辑 payload 约 0.94 GB/rank；去重节省后真实传输约 0.66 GB；prefetch 的瓶颈 rank 流量约 `70 × 7168 × 3072 × 2 ≈ 3.08 GB`（bf16）而 grad_reduce 同槽数下翻倍（fp32）。

**预期结果**：你的手算值与 4.2.3 表格公式一一对应；若未来在真实机器上跑出 CSV，可直接用 `*_us` 列反推字节数与 `*_GBps` 列比对。本环境无 GPU，运行结果**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：dispatch 带宽用 \( S \cdot K \cdot H \cdot 2 \) 而不用去重后的真实传输量，这个口径「虚高」还是「虚低」？为什么这样设计仍然是合理的？

**答案**：虚高——真实 NVLink 传输只有非负 dst 的代表行，约为 `(1 - dedup_ratio) × S·K·H·2`。合理之处有二：其一，\( S \cdot K \cdot H \cdot 2 \) 是用户视角的「必须完成的通信工作量」，用它计的带宽衡量的是通信子系统对有效载荷的吞吐能力，可与 DeepEP 等未去重库直接比较（`bench_vs_deepep.py` 用同一公式）；其二，去重比例依赖路由分布，作为独立列 `dedup_ratio` 报告，想要真实流量口径的读者可以自行折算。

**练习 2**：为什么 epilogue 与 prologue 的字节按「本地 HBM 总流量（读 + 写）」计，而 prefetch/grad_reduce 按「瓶颈 rank 的远程流量」计？

**答案**：epilogue/prologue 是纯本地内核（u4-l4/u4-l5）：主行和重复槽都在本 rank 显存里，它的成本由本地 HBM 读写量决定，没有任何 NVLink 流量。prefetch/grad_reduce 则是跨 rank 内核，各 rank 并行执行、总时长由最坏 rank 决定（木桶效应），所以按瓶颈 rank 的 \( \max(\text{max\_send}, \text{max\_recv}) \) 行数计——发送与接收共享同一 NVLink 带宽预算，以更坏的一侧为准。

**练习 3**：`--experts` 参数的存在说明什么约束？

**答案**：默认扫描里 E 恒为 896，`(ep, E)` 组合固定为 (4, 224/人) 与 (8, 112/人)。`--experts` 允许覆盖 E 以 profile 特殊组合，但 docstring 提醒「the planning CuTe DSL path must have a matching specialization」——planning 内核按 `const_expr` 形状特化编译（u4-l1），新的 (E, R) 组合意味着新的 JIT 特化，首次运行要付编译成本，且必须保证 `K ≤ E`（[benchmarks/bench_comm.py:548-551](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_comm.py#L548-L551) 会过滤掉 K > E 的配置）。

### 4.3 对比基准 bench_vs_deepep.py：公平对拍与 MaxVio 反解

#### 4.3.1 概念说明

`bench_vs_deepep.py` 回答的是**库 vs 库**的问题：在相同路由、相同数据、相同计时方式下，MoonEP 与 DeepEP v2 的 MoE 通信关键路径谁更快、随不均衡度怎样变化。它有两个与众不同的设计：

1. **只对比 DeepEP v2 elastic 的 expanded 路径**。模块 docstring（[benchmarks/bench_vs_deepep.py:1-8](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_vs_deepep.py#L1-L8)）解释：MoonEP 把 expert permute 融合进通信路径（dispatch epilogue 做扇出、combine prologue 做归约），唯一 apples-to-apples 的对比对象是 v2 的 `do_expand=True`（expanded dispatch）+ reduced combine；v1 的 `recv_x` 按源 rank 分组，permute/unpermute 是库外的独立步骤，不具可比性。
2. **按目标 MaxVio 扫描而不是按 sigma 扫描**。sigma 与 maxvio 之间没有闭式关系，同一个 sigma 在不同 (S, K, E, R) 下实现的 maxvio 不同。脚本在 rank 0 上用对数二分把 sigma 反解出来，使横轴成为可比的「实际不均衡度」。

#### 4.3.2 核心流程

```text
rank0: 对每个目标 MaxVio t：
    solve_sigma_for_maxvio(t)          # 对数二分，60 轮，容差 max(0.02t, 0.005)
    broadcast (sigma, realized)
全组: generate_topk_routing(sigma)     # 共享种子 1234 + rank 种子
    all_reduce 全局 tpe → 实测 maxvio（打印与目标的偏差）
    固定种子 7777+rank 生成 hidden/weights
    对每个库（moonep / v2）：
        prepare()（MoonEP：一次完整 dispatch 物化 plan 与去重结构）
        d_f = time_op(dispatch_fwd)；d_b = time_op(dispatch_bwd)
        c_f = time_op(combine_fwd)；  c_b = time_op(combine_bwd)
        MoonEP 额外: plan_us（精确单独计时）、pf（prefetch+sync）
        v2 侧:        plan_us ≈ max(d_f - d_b, 0)（估计，实测 ≈ 0）
输出 CSV 行；可选 plot_rows 画 2×2 堆叠柱状图
```

两库共享的全部条件在 docstring 列明（[benchmarks/bench_vs_deepep.py:10-16](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_vs_deepep.py#L10-L16)）：同一路由矩阵、相同输入张量、相同计时 harness（warmup=20, iters=50）、相同 SM 预算 32 与 expert_alignment/token_padding 128。

#### 4.3.3 源码精读

**MaxVio 反解**：[benchmarks/bench_vs_deepep.py:84-91](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_vs_deepep.py#L84-L91) 的 `_maxvio_for_sigma` 在 rank 0 上把 R 个 rank 的 tpe 逐份累加（复用基准的固定种子，因此是确定性的），返回 `max/mean - 1`；[benchmarks/bench_vs_deepep.py:94-114](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_vs_deepep.py#L94-L114) 的 `solve_sigma_for_maxvio` 在 `[1e-5, 10]` 上做几何中点 \( \sqrt{lo \cdot hi} \) 的对数二分，60 轮内逼近目标，容差 `max(0.02 × target, 0.005)`；若目标低于均匀采样噪声地板则返回试过的最小 sigma（打印 "target unreachable, floored"，[benchmarks/bench_vs_deepep.py:431-436](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_vs_deepep.py#L431-L436)）。默认目标序列 `--maxvios 0.2,1,10,20`（[benchmarks/bench_vs_deepep.py:370-372](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_vs_deepep.py#L370-L372)），其余默认 `E=384, H=7168, K=8, S=8192`，并断言单节点 `R == 8`（[benchmarks/bench_vs_deepep.py:373-389](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_vs_deepep.py#L373-L389)）。

**MoonEP 侧走公共 API**（[benchmarks/bench_vs_deepep.py:123-285](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_vs_deepep.py#L123-L285)）：

- `dispatch_fwd` 计时体是 `buffer.dispatch(..., zero_copy=True, router_weights_zero_copy=True)` + `prefetch_weight`（[benchmarks/bench_vs_deepep.py:258-264](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_vs_deepep.py#L258-L264)）——即 u6-l3 四象限的第一象限完整关键路径（inter_rank_sync + planning + dispatch + epilogue + prefetch），零拷贝消除边界拷贝；
- `dispatch_bwd` 是保存 plan 的零拷贝重派发，并再次 prefetch（反向专家 GEMM 需要同样的权重，[benchmarks/bench_vs_deepep.py:266-270](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_vs_deepep.py#L266-L270)）；
- `combine_fwd/bwd` 直接把 `hidden_buf_local` 视图喂回去（[benchmarks/bench_vs_deepep.py:272-282](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_vs_deepep.py#L272-L282)），`combine_bwd` 额外收集 `droute_weights`；
- **plan 单独精确计时**：`runner.planning` 直接调 `launch_planning`（[benchmarks/bench_vs_deepep.py:240-242](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_vs_deepep.py#L240-L242)）；
- **[E+B] 权重池的拼法**是 u2-l2/u2-l3 的漂亮复用：因训练契约 `B == epn`（断言在 [benchmarks/bench_vs_deepep.py:163-167](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_vs_deepep.py#L163-L167)），buffer chunk 与 expert chunk 同形状，`build_full`（[benchmarks/bench_vs_deepep.py:172-209](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_vs_deepep.py#L172-L209)）直接以 `world_size=R+1` 调 `nvl_dist_map`：R 个 rank 的 expert chunk + 本 rank 自己的 buffer chunk 追加为第 R+1 块，fabric 分支用 `_all_gather_shareables` 汇 64 字节句柄、fd 分支用 `_exchange_ipc_fds` 交换后手动 close——使 `prefetch_weight` 发出的是真实 NVLink 远程读。

**DeepEP 侧**：[benchmarks/bench_vs_deepep.py:292-349](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_vs_deepep.py#L292-L349) 封装 `ElasticBuffer` 与四个对偶方法；`_expanded_args`（[benchmarks/bench_vs_deepep.py:307-312](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_vs_deepep.py#L307-L312)）钉住 `do_expand=True`、`expert_alignment=128` 等对齐条件；`dispatch_bwd` 复用 `prepare` 保存的 handle（[benchmarks/bench_vs_deepep.py:327-332](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_vs_deepep.py#L327-L332)），与 MoonEP 的 saved-plan 路径地位对等。

**不对称处的诚实处理**：v2 的 layout 计算融合在 dispatch 内部、公共 API 无独立入口，无法单独计时，脚本退而用 `d_f - d_b` 估计并注明实测约等于 0（[benchmarks/bench_vs_deepep.py:452-465](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_vs_deepep.py#L452-L465)）。带宽公式两边严格一致：`gbps(S, K, H, us) = S·K·H·2 / (us·1e-6) / 1e9`（[benchmarks/bench_vs_deepep.py:80-81](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_vs_deepep.py#L80-L81)）；MoonEP 的 prefetch 带宽按三个投影计 `3 × max_recv × H × hp × 2 / t`（[benchmarks/bench_vs_deepep.py:456-457](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_vs_deepep.py#L456-L457)）。`grad_reduce` 两边都不计：它可与后续计算重叠，不在 MoE 关键路径上（docstring [benchmarks/bench_vs_deepep.py:33-34](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_vs_deepep.py#L33-L34) 与图注 [benchmarks/bench_vs_deepep.py:616-620](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_vs_deepep.py#L616-L620)）。

**可视化**：`plot_rows`（[benchmarks/bench_vs_deepep.py:518-627](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_vs_deepep.py#L518-L627)）产出 2×2 四联图（dispatch fwd/bwd、combine fwd/bwd），MoonEP 的柱子把 planning 与 prefetch 分段堆叠在通信段之上——把「MoonEP 的额外内核」显式计入总时间，正是 README 三条结论中的第三条（[README.md:25](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/README.md#L25)）：

1. 零拷贝使裸通信更快（token 直写远端最终位置，无 permute in/out）；
2. 完美均衡使 MoonEP 对不均衡免疫（comm 时间随 maxvio 几乎平坦，DeepEP v2 由最热 rank 决定、持续劣化）；
3. 对比已计入 MoonEP 的额外内核（planning + prefetch），总 dispatch 仍与 DeepEP v2 的 dispatch 持平、高不均衡下反超，combine 全档位显著更快。

#### 4.3.4 代码实践

**实践目标**：在纯 CPU 上复现「sigma → 实测 MaxVio」的映射与反解，理解 bias_ratio 扫描的内在逻辑（`generate_topk_routing` 只用 `torch.Generator`/`multinomial`/`bincount`，全部有 CPU 实现）。

**操作步骤**（示例代码）：

```python
# maxvio_scan.py — CPU 复现 _maxvio_for_sigma（示例代码）
import torch
from tests.generate_topk_routing import generate_topk_routing

def maxvio_for_sigma(sigma, S=2048, K=8, E=896, R=8):
    total = torch.zeros(E, dtype=torch.int64)
    for r in range(R):
        _, tpe = generate_topk_routing(S, K, E, R, sigma, "cpu", 1234, rank=r)
        total += tpe.to(torch.int64)
    return total.max().item() / (total.sum().item() / E) - 1.0

for sigma in [0.1, 0.3, 0.5, 1.0, 2.0, 5.0]:
    print(f"sigma={sigma:<4} -> maxvio={maxvio_for_sigma(sigma):.3f}")
```

**需要观察的现象**：maxvio 随 sigma 单调上升；sigma=0.1 时接近 0（受均匀采样噪声地板限制，不会精确为 0），sigma=5 时非常大（接近退化路由，最热专家吃掉大部分 token）。

**预期结果**：得到一张单调的 sigma→maxvio 映射表；这正是 `solve_sigma_for_maxvio` 做 对数二分 所依赖的性质。S、E 已缩小以加速，绝对数值与 GPU 版略有差异，趋势一致。本环境未执行，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `solve_sigma_for_maxvio` 的二分中点是 `(lo * hi) ** 0.5` 而不是 `(lo + hi) / 2`？

**答案**：见 [benchmarks/bench_vs_deepep.py:103-105](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_vs_deepep.py#L103-L105)。sigma 与 maxvio 的关系在对数尺度上才接近线性（logit 服从对数正态，偏斜程度由 sigma 的倍数放大），搜索区间 `[1e-5, 10]` 跨五个数量级，线性中点会浪费大量迭代在低sigma端；几何中点每轮按固定倍数收缩区间。

**练习 2**：MoonEP 的 `d_f` 计时里为什么包含 `prefetch_weight`，而 `grad_reduce` 又被排除？

**答案**：dispatch fwd 之后紧跟的专家 GEMM 必须用预取好的权重，prefetch 是前向关键路径的真实组成部分，不计入会美化 MoonEP（README 结论三特别强调对比「counts MoonEP's extra kernels」）。而 `grad_reduce` 发生在反向 FFN 之后、optimizer step 之前，可与后续层的计算重叠，不在 MoE 通信关键路径上，两边都不计。

**练习 3**：`MoonEPRunner.prepare` 里那次「不计时的完整 dispatch」起什么作用？

**答案**：见 [benchmarks/bench_vs_deepep.py:221-231](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_vs_deepep.py#L221-L231)。它产出后续所有计时共享的 `self.plan` 与去重三件套（dispatch_bwd/combine 都走 saved-plan 路径），同时为单独的 planning 计时准备 scratch。没有它，第一次 dispatch_bwd 会意外触发规划，计时就被污染了。

### 4.4 专用基准：bench_prefetch.py 与 bench_grad_reduce.py

#### 4.4.1 概念说明

`bench_comm.py` 里的 prefetch/grad_reduce 只是顺带一测（受 `experts_to_copy` 实际分布制约），两个专用脚本则把这两个算子单独拎出来做**受控实验**：槽位数量、专家形状、dtype、多部件层组合、甚至远程 owner 的物理位置（同节点 NVLink vs 跨节点 MNNVL fabric）都可以独立控制。它们的共同设计是**单发起者**（rank0-initiated）：

- `bench_prefetch.py`：rank0 是唯一真正拉取的一方，专家表物理驻留在 `--owner-rank` 指定的 rank 上，其余 rank 空转——rank0 就是瓶颈 rank，只需计时它；
- `bench_grad_reduce.py`：rank0 拥有全部被归约的专家，是最坏的 owner rank，其余 rank 只在屏障处等待。

用例矩阵由 `counts[e]` 驱动：专家 `e` 被预取/归约 `counts[e]` 次（0 表示跳过，上限 R-1），`base_B > epn` 留出空闲列制造稀疏槽布局。两个脚本的 `DEFAULT_CASES` 几乎同构（`bench_prefetch.py` 额外多出量化用例）。

#### 4.4.2 核心流程

以 `bench_prefetch.py` 为例（`bench_grad_reduce.py` 结构相同）：

```text
对每个 case（epn, H, Hp, base_B, counts[, parts, dtype]）:
    B = pad_dim0_for_alignment([base_B, H, Hp], dtype)   # VMM 粒度对齐
    expert_plan(R, B, epn, counts) 构造 (R, B) 槽网格（-1 = 空闲）
    create_nvl_single_owner_tensor 造远程专家表（owner_rank 持有物理内存）
    rank0: experts_to_copy = plan.flatten()；其余 rank 全 -1
    warmup（顺带 JIT 编译）→ 捕获 iters 次进 CUDA Graph → 计时 replay
    只取 rank0 的 elapsed_time（worst rank）
    字节口径 → BW(GB/s) 与 CommBW(GB/s) 两列
```

#### 4.4.3 源码精读

**槽网格构造**：`expert_plan`（[benchmarks/bench_prefetch.py:134-147](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_prefetch.py#L134-L147)）对专家 `e` 让源 rank `1..counts[e]` 各占一列：`plan[src_rank, e] = (src_rank-1)*epn + e`，即该槽去拉源 rank 的本名专家；`bench_grad_reduce.py` 的镜像版本（[benchmarks/bench_grad_reduce.py:74-85](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_grad_reduce.py#L74-L85)）填 `plan[src_rank, e] = e`（owner 恒为 rank0）。断言 `0 <= c < R`、`epn <= B` 钉住合法域（[benchmarks/bench_prefetch.py:164-165](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_prefetch.py#L164-L165)）。

**远程专家表**：`bench_prefetch` 用 `create_nvl_single_owner_tensor` 把整张 `[E, th, thp]` 专家表放在 owner rank 的显存里（[benchmarks/bench_prefetch.py:168-179](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_prefetch.py#L168-L179)），owner 用固定种子填随机数据。`--owner-rank` 的 help（[benchmarks/bench_prefetch.py:276-283](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_prefetch.py#L276-L283)）点明其度量意图：owner 在 rank0 同节点测的是节点内 NVLink，放另一节点测的是跨节点 MNNVL fabric。`bench_grad_reduce` 则用 `create_nvl_dist_tensor` 造 `[B, H, Hp]` 的归约缓冲并 view 成 `[R, B, H, Hp]`（[benchmarks/bench_grad_reduce.py:103-105](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_grad_reduce.py#L103-L105)），并仅为了让 `launch_grad_reduce` 拿到跨 rank 屏障区而构造了一个最小 `Buffer(S=128, H=H, K=1, E=E, num_ep_ranks=R, num_sms=num_sms, B=B)`（[benchmarks/bench_grad_reduce.py:107-109](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_grad_reduce.py#L107-L109)）——内核签名需要 `meta_buf`/`BARRIER_OFF`/`grid_sync_bar`，这是 u3-l6 屏障基础设施的借用法。

**量化与多部件**：`derive_extents`（[benchmarks/bench_prefetch.py:100-106](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_prefetch.py#L100-L106)）按 part 推导实际 2D extent：transposed 交换 H/H′，`pack: 2` 把收缩维减半（MXFP4 e2m1 每字节两值），`scale: True` 则按每 32 值一字节算出 `H*Hp//32` 字节再重切成 `(128, nbytes//128)`（u5-l2 的 retile 语义）。`K3_MXFP4_LAYER`（[benchmarks/bench_prefetch.py:28-35](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_prefetch.py#L28-L35)）把一个完整量化 MoE 层的 6 个部件（gate/up/down 打包权重 + 3 个 scale）放进一个 case 一次测完，对应 u5-l2 的「一个量化层共六次内核发射」。

**字节口径**（两脚本刻意区分了总带宽与纯通信带宽两列）：

- `bench_prefetch`（[benchmarks/bench_prefetch.py:232-241](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_prefetch.py#L232-L241)）：
  - `slots = sum(counts)`，`per_slot = Σ(th·thp·itemsize)`；
  - `bytes_per_rank = slots · per_slot · 2`（NVLink 读一次 + 本地 HBM 写一次）→ `BW(GB/s)` 列；
  - `comm_gbs = slots · per_slot / t` → `CommBW` 列，注释写明「only the remote expert-table reads (buffer writes are local HBM, off the NVLink path)」；
- `bench_grad_reduce`（[benchmarks/bench_grad_reduce.py:165-176](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_grad_reduce.py#L165-L176)）：
  - `slots = sum(counts)`，`active = #(counts[e] > 0)`，`tile = H·Hp·4`；
  - `bytes_per_rank = slots·tile + active·tile·2`（读每个消费槽 4 B/元素 + 读写每个活跃专家梯度各一次）→ `BW` 列；
  - 槽清零发生在各源 rank 本地、与 owner 并行，**不在 owner 关键路径上，不计入**；`comm_gbs` 只算远程槽读 → `CommBW` 列。

`worst_us` 只在 rank 0 上取事件差（[benchmarks/bench_prefetch.py:228-230](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_prefetch.py#L228-L230)、[benchmarks/bench_grad_reduce.py:161-163](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_grad_reduce.py#L161-L163)）——与单发起者设计配套：rank0 天然是最坏 rank。

#### 4.4.4 代码实践

**实践目标**：不跑 GPU，从 `DEFAULT_CASES` 静态推导每个用例的 `Slots` 与 `Data(MB)` 列，练熟两个脚本的字节公式（纯 CPU 脚本即可运行）。

**操作步骤**（示例代码）：

```python
# derive_prefetch_cases.py — 从 DEFAULT_CASES 推导字节口径（示例代码）
from benchmarks.bench_prefetch import DEFAULT_CASES, resolve_parts

R, NUM_SMS = 8, 32
for case in DEFAULT_CASES:
    parts = resolve_parts(case)
    counts = case["counts"]
    slots = sum(counts)
    per_slot = sum(th * thp * dt.itemsize for th, thp, dt in parts)
    print(f"{case['label']:<18} slots={slots:>3} "
          f"per_slot={per_slot/1e6:>7.2f}MB "
          f"Data={slots*per_slot*2/1e6:>8.2f}MB")
```

**需要观察的现象**（可手工核对两例）：

- `slots_1`（counts=[1,0,…]，bf16 3584×3072）：`per_slot = 3584·3072·2 ≈ 22.02 MB`，`Data = 1×22.02×2 ≈ 44.04 MB`；
- `mxfp4_scale`：`per_slot = 128·2688·1 ≈ 0.34 MB`（= `H·Hp/32`），`slots = 12`，`Data ≈ 8.26 MB`；
- `k3_layer_bf16`：3 个 bf16 部件 `per_slot ≈ 66.06 MB`，`slots = 12`，`Data ≈ 1585 MB`——一个完整 MoE 层的权重搬运量级。

**预期结果**：脚本输出与（未来真实运行的）表格 `Slots`/`Data(MB)` 列逐行一致；对 `bench_grad_reduce.py` 把 `dt` 固定为 fp32、字节换成 `slots·tile + active·tile·2` 即可同样推导（`slots_1` → `44.04 + 2×44.04 ≈ 132.12 MB`）。本环境未运行多卡基准，**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`bench_prefetch` 的 `full_3x8` 用例（counts 全 3）测的是什么极限？

**答案**：R=8 时每专家最多被其余 7 个 rank 各拉一次，但该用例固定 counts=3，即每个专家被 3 个源 rank 预取、共 `3×8 = 24` 个槽全部占满。它代表「饱和」场景：预取槽数达到布局允许的上限（此处受 counts 约束而非 B），用于观察满负载下的带宽上限。对照 `dense_B8`（base_B=8=epn 无空闲列）可以分离「槽数」与「列密度」两个因素。

**练习 2**：为什么 `bench_grad_reduce` 的 `BW` 列不把源 rank 的槽清零流量算进去？

**答案**：见 [benchmarks/bench_grad_reduce.py:166-168](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_grad_reduce.py#L166-L168) 的注释：内核在跨 rank 屏障后**各源 rank 只本地清零自己段内被消费的槽**（u5-l3 的带宽权衡），这些清零写发生在各 rank 自己的 HBM 上、与 owner 的归约并行，不在被计时的 owner 关键路径上。这也再次说明「带宽」必须声明口径——这里的 BW 是 owner rank 的关键路径流量。

**练习 3**：两个专用基准与 `bench_comm` 的 prefetch/grad_reduce 行相比，牺牲了什么、换来了什么？

**答案**：牺牲真实性——`bench_comm` 的 `experts_to_copy` 来自真实规划器输出的分布，槽位分布、owner 分布都是自然形成的；换来完全受控——槽数（`slots`）、专家形状、dtype/量化、多部件层、owner 物理位置（节点内/跨节点）都可独立扫描，适合做内核的微基准与 profiling（`--no-graph` 配 NCU），而 `bench_comm` 更适合回答「在真实训练配置下各算子占比多少」。

## 5. 综合实践

**任务**：编写 `bench_report.py`——一个「bench_comm 输出解析 + 理论字节推导」双模式报告器，把本讲全部口径知识串起来。

**要求**：

1. **模式 A（有真实 CSV）**：读取 `torchrun --nproc_per_node=8 benchmarks/bench_comm.py --out comm.csv --ep 8 --hidden 7168 --topk 8` 产出的 CSV（36 列表头见 [benchmarks/bench_comm.py:558-574](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_comm.py#L558-L574)）。对每行：
   - 从配置列（S, K, H, Hp）代入公式 \( S·K·H·2 \) 重算 dispatch/combine 字节，除以对应 `*_us` 列得到重算带宽；
   - 与 CSV 的 `*_GBps` 列比对，差值应落在 `fmt4` 的 4 位有效数字舍入内——验证你理解的口径与代码一致；
   - 用 `dedup_ratio` 估算去重后的真实 NVLink 流量，输出「逻辑带宽 vs 折算带宽」对照。
2. **模式 B（无硬件）**：不给 CSV 时，对一组代表性配置（建议 `ep=8, H=7168, K=8, S=8192, E=896, Hp=3072`，bias_ratio 三档）打印口径表：每个算子一行，列出公式、代入值、以及在一组**假设**的实测统计（`dedup_ratio`、`G`、`D`、`max_send/max_recv`，标注为假设）下的字节数；再固定几个假设耗时输出示例带宽列。
3. 把 4.4 的 `derive_prefetch_cases.py` 并为子命令，输出预取/归约用例的理论 `Slots`/`Data(MB)` 表。

**验收标准**（示例）：

- 模式 A 中 dispatch_fwd 的重算带宽与 CSV 列相对误差 < 0.1%；
- 模式 B 的表格能让你在不看源码的情况下回答「epilogue 字节为什么是 (G+D)·H·2」；
- 所有「假设统计」在输出里显式标注，不与可实测值混淆。

本环境无 GPU，模式 A **待本地验证**；模式 B 与 4.4 的推导脚本可在纯 CPU 上先跑通。

## 6. 本讲小结

- 四个基准脚本分工明确：`bench_comm.py` 算子级扫描（十一个内核 × H/K/EP/bias_ratio）、`bench_vs_deepep.py` 跨库对拍（MoonEP 公共 API vs DeepEP v2 expanded）、`bench_prefetch.py`/`bench_grad_reduce.py` 单发起者受控微基准（槽位密度、形状、dtype、owner 位置可独立扫描）。
- 计时方法学三板斧：CUDA 事件对、CUDA Graph 剥离 launch 开销（保留 `--no-graph` 给 NCU）、跨 rank all_gather 均值；迭代串行化靠内核自带跨 rank 屏障，prefetch 例外地手动配 `inter_rank_sync`。
- 带宽必须声明口径：dispatch/combine 用逻辑 payload \( S·K·H·2 \)（去重比例另列 `dedup_ratio`）；epilogue/prologue 用本地 HBM 读写总量；prefetch/grad_reduce 用瓶颈 rank 的 \( \max(\text{max\_send},\text{max\_recv})·H·H' \) 字节；专用基准进一步区分总流量 BW 与纯 NVLink 通信 CommBW。
- 路由不均衡有两种参数化：`bench_comm` 直接扫对数正态 sigma（bias_ratio 0.1/1/5 三档）；`bench_vs_deepep` 按目标 MaxVio 反解 sigma（对数二分），横轴才是可比的实际不均衡度。
- 公平对拍的要点全部落在共享条件上：同一路由（seed 1234 共享 + rank 种子）、相同输入张量、同一 eager 计时 harness、同 SM 预算与对齐参数；无法对称计量的部分（v2 的 layout 融合在 dispatch 内）用 `d_f - d_b` 估计并显式标注。
- README 的三条对比结论（零拷贝更快、完美均衡对不均衡免疫、计入额外内核仍持平或反超）都能在 `bench_vs_deepep.py` 的堆叠柱状图与其代码里找到直接依据。

## 7. 下一步学习建议

- 下一篇 u6-l6「扩展与二次开发」会把基准当作护栏：任何改动内核 SM/smem 配置或新增 dtype 的工作，都应回到 `bench_comm.py` 的对应列验证没有性能回退。
- 若你有 8 卡 NVLink 机器，建议按顺序实跑：先 `bench_comm.py --ep 8 --hidden 7168 --topk 8`（36 列 CSV 喂给综合实践的报告器），再 `bench_vs_deepep.py --plot comm.png` 复现 README 图，最后用 `bench_prefetch.py --suite` 配合 `--no-graph` + NCU 观察单个预取内核的 NVLink 利用率。
- 源码延伸阅读：`tests/generate_topk_routing.py` 的种子设计（共享 logit vs 独立抽样）是所有基准确定性的根基，值得对照 u6-l4 的「参考实现」思想再读一遍；`bench_vs_deepep.py` 的 `build_full`（R+1 块 `nvl_dist_map` 拼 [E+B] 池）则是 u2-l2/u2-l3 内存基础设施的一次完整实战演示。
