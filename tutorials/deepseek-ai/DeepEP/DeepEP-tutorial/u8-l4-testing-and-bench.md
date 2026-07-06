# 测试、参考实现与基准测试体系

## 1. 本讲目标

DeepEP 用 C++/CUDA 手写了极致优化的 EP 通信内核，越是这样「不可读」的高性能代码，越需要一套**可独立验证的测试与基准体系**来保证正确性、衡量性能。本讲带读者读懂 `tests/` 与 `deep_ep/utils/` 下三件彼此独立的「测试基建」：

学完本讲，你应当能够：

- 说清 `deep_ep/utils/refs.py` 中的参考实现 `dispatch`/`combine` 为何能作为**正确性金标准**，以及它输出「按源全局索引排序」这一关键性质从何而来。
- 掌握 `deep_ep/utils/testing.py` 中 `bench_kineto` 如何用 **PyTorch Kineto + 通信 barrier** 测出单内核带宽，并理解它为何比 CUDA event 计时更适合通信内核。
- 了解 `deep_ep/utils/gate.py` 中 `get_unbalanced_scores` 如何构造**可控负载不均衡**的 MoE 门控分数，为压测提供贴近真实场景的输入。
- 能动手解释 `tests/elastic/test_ep.py` 里那条 `torch.equal(ref_recv_src_token_idx, sorted_src_token_global_idx)` 断言为何成立。

## 2. 前置知识

本讲是 U8「底层原语、后端工程与测试体系」单元的收尾，依赖你已经掌握的内容：

- **EP/dispatch/combine 与 EPHandle**（u2-l3）：dispatch 把 token 按路由发往专家所在 rank，combine 把专家输出加权归约回原 rank；二者靠 `EPHandle` 中的 `recv_src_metadata` 等元数据串联。
- **`recv_src_metadata` 的语义**（u5-l1）：其第 0 列 `recv_src_metadata[:, 0]` 记录每个接收 token 的**源全局索引** `src_token_global_idx = src_rank_idx * num_max_tokens_per_rank + src_token_local_idx`。
- **确定性排序**（u6-l3）：DeepEP 默认输出是**非确定**的——多个 warp/通道通过 `atomicAdd` 抢占 buffer 槽位，到达顺序随运行抖动。
- **NCCL `all_to_all_single`**：PyTorch 分布式原语，把每个 rank 的发送缓冲按 `output_split`/`input_split` 切分交换。这是参考实现赖以「天然正确」的基础。

两个初学者容易混淆的点，先点明：

1. **「参考实现」不是 DeepEP 的代码**。它是写在 `deep_ep/utils/refs.py`、**只用标准 PyTorch + NCCL `all_to_all_single`** 实现的「笨但显然正确」的 dispatch/combine。DeepEP 的高性能内核与它逐位（bitwise）比对，以证明自己没算错。
2. **命名陷阱**：本讲的实践任务与大纲里常说「`ref_dispatch`」，但 `refs.py` 中函数本名就是 `dispatch`；`test_ep.py` 在导入时给它起了别名：`from deep_ep.utils.refs import dispatch as ref_dispatch`。下文统一用 `ref_dispatch` 指代它，引用源码时仍指向 `dispatch`。

## 3. 本讲源码地图

| 文件 | 职责 | 本讲用到的关键符号 |
| --- | --- | --- |
| `deep_ep/utils/refs.py` | 参考实现：纯 NCCL/PyTorch 的 dispatch/combine 与辅助数据生成 | `dispatch`(别名 `ref_dispatch`)、`combine`、`generate_pre_combine_data`、`ordered_accumulate` |
| `deep_ep/utils/testing.py` | 基准测试工具：L2 flush、CUDA event 计时、Kineto 单内核计时 | `bench_kineto`、`flush_l2_cache`、`suppress_stdout_stderr` |
| `deep_ep/utils/gate.py` | 测试数据生成：构造可控不均衡的 MoE 门控分数 | `get_unbalanced_scores`、`get_precise_unbalanced_scores`、`get_random_unbalanced_scores`、`generate_rank_count` |
| `tests/elastic/test_ep.py` | 正确性 + 性能主测试：枚举所有配置，逐位比对 + 带宽打印 | `test_dispatch_combine`、`enumerate_ep_modes`、`launch` |
| `deep_ep/utils/math.py` | 测试用到的辅助算子 | `count_bytes`、`calc_diff`、`safe_div`、`align` |

调用方向：`test_ep.py` 调 `gate.get_unbalanced_scores` 造输入 → 调 `buffer.dispatch/combine`（被测对象）→ 调 `refs.dispatch/combine`（参考）→ 用 `torch.equal`/`calc_diff` 比对；性能部分则把被测调用包进 `testing.bench_kineto`。

## 4. 核心概念与源码讲解

### 4.1 参考实现：ref_dispatch / ref_combine 作为正确性金标准

#### 4.1.1 概念说明

「参考实现（reference implementation）」是一种软件工程常用手段：用一个**逻辑简单、行为显然正确**的实现作为「金标准」，再让被优化的实现与之比对。DeepEP 的 EP 内核为了省 SM、压延迟，用了 TMA、atomicAdd 抢槽、两级归约等复杂手段，单靠肉眼看不出对错；而 `refs.py` 用一行 `dist.all_to_all_single` 就完成了交换，**正确性不证自明**。

参考实现有两个设计约束：

- **不追求性能**：它用 `all_to_all_single` 逐张量交换，调度简单但慢。测试里 `ref_dispatch` 只在 `--num-tokens` 较小时跑，且可用 `--skip-check` 关掉。
- **必须定义清晰的输出规范**：包括 token 的排列顺序、`recv_topk_idx` 中跨 rank 专家的掩码（置 -1）、FP8 的 `(data, scale)` 元组等。被测内核必须严格遵守同一规范，才能逐位相等。

`ref_dispatch` 的输出规范里有一条对后续断言至关重要：**接收到的 token 按 `src_token_global_idx` 升序排列**（即「先按源 rank，再按源 rank 内 token 局部序号」）。下一节的实践任务正是要解释这条性质。

#### 4.1.2 核心流程

`ref_dispatch` 的执行流程：

```text
对每个目标 rank dst_rank_idx in [0, num_ranks):
    1. 算出本 rank 哪些 token 的 topk 专家落在 [expert_start, expert_end) → mask_to_send
    2. indices_to_send = mask_to_send.nonzero()        # 升序的 token 本地索引
    3. 打包 send_x / send_topk_idx / send_topk_weights / send_src_token_idx
       (send_src_token_idx = indices_to_send + rank_idx * num_max_tokens_per_rank)
把各 rank 的发送缓冲 torch.cat 成一条（按 dst_rank_idx 顺序拼接）
dist.all_to_all_single 交换"每 rank 发送 token 数"   → num_recv_tokens_per_rank
dist.all_to_all_single 交换 x / sf / topk_idx / weights / src_token_idx
                                                     （按 num_recv_tokens_per_rank 切分）
把本 rank 专家范围外的 recv_topk_idx 减去偏移并置 -1
返回 (recv_x, recv_topk_idx, recv_topk_weights, recv_src_token_idx, num_recv_tokens_per_rank)
```

关键性质来自两步叠加：

- 第 2 步 `nonzero()` 返回的索引天然**升序**；
- 第 4 步 `all_to_all_single` 在接收端按**源 rank 顺序**拼接各段（rank 0 的段在最前，rank 1 次之……），段内仍保持升序。

而 `src_token_global_idx = src_rank_idx * num_max_tokens_per_rank + src_token_local_idx`，因为 `num_max_tokens_per_rank` 不小于任一 rank 的真实 token 数，**全局索引随 (rank, 局部序号) 严格递增**。所以「rank 优先、段内升序」恰好等价于「按全局索引升序」——`ref_recv_src_token_idx` **本身就已经排好序了**。

`ref_combine` 的流程是对称的「按组归约」：先用 `grouped_reduce` 在指定分组（rank 内或 scaleup 内）做分段有序求和，再用 `ordered_accumulate` 沿 `num_topk` 维累加，模拟 DeepEP 的两级归约。

#### 4.1.3 源码精读

`ref_dispatch` 的函数签名与文档明确写出「按 `src_token_global_idx` 排序」的语义，这是整套断言的契约：

- [deep_ep/utils/refs.py:L10-L29](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/utils/refs.py#L10-L29) —— `dispatch` 函数定义与 docstring，第 16 行写明 *Sorted by rank and then by token within each rank (i.e. sorted by `src_token_global_idx`)*。

每 rank 的发送 token 筛选与升序索引：

- [deep_ep/utils/refs.py:L62-L83](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/utils/refs.py#L62-L83) —— `mask_to_send` 标记要发往 `dst_rank_idx` 的 token；`indices_to_send = mask_to_send.nonzero(...)` 得到升序本地索引；`send_src_token_idx_list.append(indices_to_send)` 收集。

按 rank 顺序拼接并加上 rank 偏移得到全局索引：

- [deep_ep/utils/refs.py:L85-L90](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/utils/refs.py#L85-L90) —— `torch.cat(send_*_list)` 按 `dst_rank_idx` 顺序拼接；`send_src_token_idx += rank_idx * num_max_tokens_per_rank` 把本地索引升级为全局索引。

逐张量交换（`all_to_all_single` 的接收端按源 rank 段拼接，正是「已排序」性质落地的关键）：

- [deep_ep/utils/refs.py:L92-L110](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/utils/refs.py#L92-L110) —— 先交换每 rank 发送数量，再交换 5 路张量；`recv_src_token_idx` 即由此产生。

`ref_combine` 的分组有序归约（用 `torch.sort(stable=True)` + 分段累加模拟 DeepEP 内核的严格求和顺序）：

- [deep_ep/utils/refs.py:L201-L234](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/utils/refs.py#L201-L234) —— `grouped_reduce` 内层函数；`is_segment_break` 判定分组边界，把累加值落到每组最右 token，其余清零。

测试主流程里如何调用 `ref_dispatch` 并取用其结果：

- [tests/elastic/test_ep.py:L107-L112](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/tests/elastic/test_ep.py#L107-L112) —— 调用 `ref_dispatch` 得到 `ref_recv_src_token_idx` 等 5 个返回值。

#### 4.1.4 代码实践

> **实践目标**：亲手解释 `test_ep.py` 中 `torch.equal(ref_recv_src_token_idx, sorted_src_token_global_idx)` 为何成立。

**操作步骤**：

1. 打开 `tests/elastic/test_ep.py`，定位到第 186–190 行：

   ```python
   src_token_global_idx = handle.recv_src_metadata[:num_recv_tokens, 0]
   if not args.skip_check:
       sorted_src_token_global_idx = torch.sort(src_token_global_idx).values
       assert torch.equal(ref_recv_src_token_idx, sorted_src_token_global_idx), ...
   ```

   其中 `handle` 是 DeepEP `buffer.dispatch(...)` 返回的 `EPHandle`，`ref_recv_src_token_idx` 来自 `ref_dispatch`。

2. 打开 `deep_ep/utils/refs.py` 第 62–90 行，确认参考实现里 `indices_to_send` 由 `nonzero()` 产生（升序），并按 `dst_rank_idx` 顺序 `torch.cat`。

3. 在脑中（或纸上）跑一个 2 rank、每 rank 4 token、`num_topk=2` 的小例子：假设 rank 0 的 token 0、3 要发到当前 rank，rank 1 的 token 1 要发到当前 rank。写出 `send_src_token_idx` 拼接顺序、`all_to_all` 后的接收顺序，验证它就是 `[0*stride+0, 0*stride+3, 1*stride+1]` 这样的升序全局索引。

**需要观察的现象**：

- DeepEP 的 `src_token_global_idx`（未排序）每次运行**顺序可能不同**（非确定性，见 u6-l3），但**所含元素的多重集（multiset）与 `ref_recv_src_token_idx` 完全相同**。
- `torch.sort(src_token_global_idx).values` 之后，它变成了与 `ref_recv_src_token_idx` 逐元素相等的升序序列。

**预期结果 / 解释**：

断言成立，是因为两边满足三个条件：

1. **同一多重集**：两套实现都把「topk 专家落在当前 rank 的那些 token」正确路由过来，故接收到的 `src_token_global_idx` 集合一致（与到达顺序无关）。
2. **参考端已升序**：`ref_dispatch` 因 `nonzero()` 升序 + `all_to_all_single` 按源 rank 段拼接 + 全局索引随 (rank, 局部序号) 递增，输出的 `ref_recv_src_token_idx` 天然升序。
3. **DeepEP 端排序后对齐**：把 DeepEP 非确定的输出 `torch.sort` 之后，与「天然升序」的参考端逐位相等。

> 这也是更靠后的逐张量比对（`test_ep.py` 第 472–500 行）所用的技巧：先对 `recv_src_metadata[:,0]` 全局排序，取其 `indices` 作为重排置换，把 DeepEP 任意到达顺序的 `recv_x`/`recv_topk_weights` 重排到参考的规范位序，再做 `torch.equal`。若本机有 8 卡 Hopper，可运行 `torchrun`/`python tests/elastic/test_ep.py`（默认 8 进程）实际观察；若无 GPU 环境，则本任务为**源码阅读型实践**，结论如上。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `refs.py` 第 90 行的 `send_src_token_idx += rank_idx * num_max_tokens_per_rank` 删掉（只发本地索引），`ref_recv_src_token_idx` 还能和 DeepEP 的 `recv_src_metadata[:,0]` 对齐吗？为什么？

> **答**：不能。删掉后参考端发的是「源 rank 内的局部索引」，多 rank 的局部索引会重叠（都在 `[0, num_max_tokens_per_rank)` 内），失去全局唯一性，也就无法与 DeepEP 的全局索引一一对应。`num_max_tokens_per_rank` 在参考实现里既是**位宽分离器**（把 rank 与局部序号编码进同一个整数），也是文档强调的、必须与 DeepEP 对齐的参数（见 refs.py 第 22 行 docstring）。

**练习 2**：`test_ep.py` 第 472–482 行做逐张量比对时，为什么先 `torch.sort(check_handle.recv_src_metadata[:, 0])`、再用 `.indices` 重排其他张量，而不是直接 `torch.equal(recv_x, ref_recv_x)`？

> **答**：因为 DeepEP 的到达顺序非确定，直接比对几乎必然失败。先用元数据列的全局排序得到一个「DeepEP 输出 → 参考规范位序」的置换，把 `recv_x` 等张量按该置换重排，再与同样排好序的参考张量逐位比较，就能把「内容正确」与「顺序无关」两件事解耦。

---

### 4.2 基准测试：bench_kineto 用 barrier + Kineto 测单内核带宽

#### 4.2.1 概念说明

测一个普通 GPU 算子的耗时，常用 CUDA event 计时（`torch.cuda.Event(enable_timing=True)`，本文件的 `bench` 函数即如此）。但测**多 rank 通信内核**有两道额外难题，必须用更讲究的 `bench_kineto`：

1. **要分辨「主内核」与「收尾内核」**。DeepEP 一次 dispatch 实际下发两个 GPU kernel——省 SM 的 `dispatch_impl`（负责跨 rank 搬数据）和满 SM 的 `dispatch_copy_epilogue_impl`（负责本地拆包重排）。CUDA event 包住的是整段 `fn()`，会把两者和 launch 开销混在一起；测试想分别报告「通信带宽」与「copy 带宽」，必须拿到**单内核粒度**的耗时。
2. **多 rank CPU launch 不均会污染测量**。8 个进程各自 `fn()` 的下发时刻有先有后，早下发的 rank 可能让通信内核提前开始，单测某一 rank 时计入额外等待。需要先做一次**全 rank 同步**把大家拉齐。

`bench_kineto` 用 PyTorch 的 Kineto profiler（`torch.profiler`）拿到 per-kernel 耗时解决问题 1，用「大 sleep 内核 + 通信 barrier」解决问题 2。

> 术语：**Kineto** 是 PyTorch 内建的、基于 CUPTI 的性能分析后端，`torch.profiler.profile(activities=[ProfilerActivity.CUDA])` 即用它采集每个 CUDA kernel 的起止时间。**Chrome trace** 是它可导出的 JSON，可被 chrome://tracing 或 Perfetto 打开。

#### 4.2.2 核心流程

`bench_kineto(fn, kernel_names, ...)` 的执行流程：

```text
若 EP_USE_NVIDIA_TOOLS=1：直接返回占位 1（让位给 Nsight/Compute Sanitizer，避免采集冲突）
fn() 一次 + synchronize（预热，吞掉 auto-tuning 的打印）
进入 torch.profiler（schedule: wait=0, warmup=1, active=1）
  循环 2 个周期，每周期跑 num_tests 次:
      flush_l2_cache()                  # 写 256MB 零张量冲刷 L2，隔离缓存影响
      若 barrier_comm_profiling:
          torch.cuda._sleep(2e7)        # ~10ms 大内核，吸掉残存的 launch 抖动
          barrier()  (默认 dist.all_reduce，可传 buffer.barrier)   # 全 rank 拉齐
      fn()                              # 被测调用
  synchronize + profiler.step()
解析 key_averages().table():  # 一张含每个 kernel 总耗时与调用次数的文本表
  对每个 kernel_name: 定位所在行，读出 总时间/次数，算平均耗时（秒）
（可选）num_kernels_per_period>1 时，导出 chrome trace，按时间排序后把连续 N 个 kernel 分组求平均
返回（单名 → 标量；多名 → 列表）
```

调用方拿到单内核耗时 `t`（秒）后，自己算带宽：`字节数 / t / 1e9` → GB/s。注意这是 u1-l1 讲过的「**逻辑带宽**」——分子是本 rank 视角下经过内核的字节，不是物理链路纯带宽。

#### 4.2.3 源码精读

`bench_kineto` 的签名与文档（参数 `barrier_comm_profiling`、`barrier`、`num_kernels_per_period` 是通信测试的关键旋钮）：

- [deep_ep/utils/testing.py:L111-L137](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/utils/testing.py#L111-L137) —— 函数定义，docstring 解释了 barrier 的作用是 *reduce unbalanced CPU launch overhead*。

`EP_USE_NVIDIA_TOOLS` 短路（与 Nsight Systems / Compute / Compute Sanitizer 共存时让出采集权）：

- [deep_ep/utils/testing.py:L141-L144](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/utils/testing.py#L141-L144) —— 命中即返回 `(1,)*len` 或 `1`，避免 Kineto 与外部工具抢 CUPTI。

「大 sleep + barrier」拉齐多 rank 的核心片段（解决问题 2）：

- [deep_ep/utils/testing.py:L163-L174](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/utils/testing.py#L163-L174) —— `torch.cuda._sleep(int(2e7))` 注入约 10ms 空转内核；`barrier is None` 时用 `dist.all_reduce(dummy)`，注释提醒 *Some network may have ring-based implement, so be careful to use `all_reduce`*（环形实现的集合通信在不同 rank 上的耗时不对称），故支持传入自定义 `barrier`（DeepEP 传的是自家 GPU 级 `buffer.barrier`，见 u7-l1）。`EP_DISABLE_BARRIER_PROFILING=1` 可关掉这步。

解析 profiler 表格、按 kernel 名取平均耗时（解决问题 1）：

- [deep_ep/utils/testing.py:L177-L202](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/utils/testing.py#L177-L202) —— `key_averages().table(sort_by='cuda_time_total')` 得到文本表；逐行匹配 `kernel_names`，从行尾解析 `时间值+单位`（ms/us）与 `调用次数`，换算回秒后 `总时间/总次数` 得平均。

测试里如何调用并把耗时换算成带宽：

- [tests/elastic/test_ep.py:L253-L263](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/tests/elastic/test_ep.py#L253-L263) —— `count_bytes` 统计每 token 字节，乘以 token 数得 `num_scaleup_bytes`/`num_scaleout_bytes`；`bench_kineto(lambda: buffer.dispatch(**dispatch_args), kernel_names=('dispatch_impl','dispatch_copy_epilogue_impl'), barrier_comm_profiling=True, barrier=buffer.barrier, ...)` 返回 `(t, copy_t)`，于是 SO/SU 带宽分别为 `num_scaleout_bytes/t/1e9`、`num_scaleup_bytes/t/1e9`。

辅助工具：L2 冲刷与字节数统计：

- [deep_ep/utils/testing.py:L12-L21](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/utils/testing.py#L12-L21) —— `flush_l2_cache`：写一块 256MB 的零 `int` 张量冲刷 L2，使每次 `fn()` 的缓存状态一致。
- [deep_ep/utils/math.py:L96-L103](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/utils/math.py#L96-L103) —— `count_bytes`：递归地把若干张量（含元组、`None`）折算成总字节数，供带宽公式使用。

#### 4.2.4 代码实践

> **实践目标**：跑一次性能测试，亲眼看 `bench_kineto` 打印的带宽，并理解两个旋钮的作用。

**操作步骤**：

1. 在单机 8 卡环境运行（默认 `--num-processes 8`）：

   ```bash
   python tests/elastic/test_ep.py
   ```

   观察输出里形如 `EP: 0/8 | dispatch: 0 GB/s (SO), 280 GB/s (SU), 12.3 us, ... bytes | copy: ...` 的行。

2. 加 `--skip-check` 关掉正确性比对、只跑性能，可缩短耗时；加 `--dump-profile-traces ./traces` 可让 `bench_kineto` 把每个被测调用的 chrome trace 落盘，用 Perfetto 打开即可看到 `dispatch_impl` 与 `dispatch_copy_epilogue_impl` 两个内核的实际时间轴。

3. 重新跑时分别设 `EP_DISABLE_BARRIER_PROFILING=1` 与 `EP_USE_NVIDIA_TOOLS=1`，对比输出：

   ```bash
   EP_DISABLE_BARRIER_PROFILING=1 python tests/elastic/test_ep.py --skip-check
   EP_USE_NVIDIA_TOOLS=1 python tests/elastic/test_ep.py --skip-check
   ```

**需要观察的现象 / 预期结果**：

- 正常模式下：SU 带宽（NVLink 节点内）应接近 README 的参考值；SO 带宽在单机时恒为 0（无 RDMA 流量，见 u1-l4）。
- `EP_DISABLE_BARRIER_PROFILING=1` 后：因省去了全 rank barrier，单 rank 的测量可能偏小或方差变大（被 launch 抖动污染），印证 barrier 的「拉齐」作用。
- `EP_USE_NVIDIA_TOOLS=1` 后：`bench_kineto` 全部返回占位 `1`，打印的带宽会变成荒谬的「字节数 GB/s」——这正是它让位给 Nsight 的设计。若无 GPU 环境，本任务为**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么不直接用 `testing.py` 里的 `bench`（CUDA event）来测 dispatch 带宽，而非要写一个 `bench_kineto`？

> **答**：`bench` 用一对 CUDA event 包住整个 `fn()`，测到的是「主内核 + copy epilogue + launch 开销」的总和，无法分别报告 `dispatch_impl` 与 `dispatch_copy_epilogue_impl` 的耗时；而 README 与测试想分别给出「通信带宽」和「copy 带宽」。`bench_kineto` 借 Kineto 拿到 per-kernel 耗时，才能把两者拆开。

**练习 2**：`bench_kineto` 里 `torch.cuda._sleep(int(2e7))` 和随后的 `barrier()` 各解决什么问题？能否只保留一个？

> **答**：`_sleep` 注入约 10ms 的空转内核，把各 rank 残留的、错落的 CPU launch「吸」进同一段 GPU 空转里，避免上一轮的尾流影响本轮；`barrier()`（`dist.all_reduce` 或 `buffer.barrier`）做一次全 rank 同步，确保各 rank 在同一时刻才开始下发 `fn()`。两者配合才能把「多 rank launch 不均」压到最小；只保留一个会留下另一维度的抖动，测量方差变大。

---

### 4.3 可控不均衡：get_unbalanced_scores 构造 MoE 门控分数

#### 4.3.1 概念说明

真实 MoE 的 token 分布**天然不均衡**：热门专家会吸引远超均值的 token，某些 rank 因此「撑爆」、其它 rank 空转。EP 通信内核的性能对这种不均衡极其敏感（撑爆的 rank 决定 `num_recv_tokens` 上界），所以基准测试必须能**按指定倍率**注入不均衡，否则测到的只是「理想均匀」下的乐观带宽。

`gate.py` 的 `get_unbalanced_scores` 接受一个 `ratio`（≥1.0），生成「某个特殊 rank 收到的 token 数约为其它 rank 均值的 `ratio` 倍」的门控分数 `scores`，形状 `[num_tokens, num_experts]`，供 `torch.topk` 选出 `topk_idx`。它提供两条路径：

- **`precise=True`（精确构造）**：用 `generate_rank_count` 直接算出每个 token 在各 rank 上的 topk 计数，严格满足倍率；但作者注释提醒 *differs from real distribution*，是「人工搭出来」的极端场景。
- **`precise=False`（随机构造，默认）**：把「目标倍率 ratio」二分搜索映射成一个 score 缩放因子 `factor`，再随机采样；分布更自然，倍率是统计意义上的近似。

#### 4.3.2 核心流程

`get_unbalanced_scores(num_tokens, num_experts, num_ranks, num_topk, ratio, precise)`：

```text
if precise:
    rank_count = generate_rank_count(...)        # [num_tokens, num_ranks]，每行和=num_topk
                                                  # 某特殊 rank 比其它 rank 多 ~ratio 倍
    scores ← 均匀(low)；topk 位改采均匀(threshold,1)（高分），其余低位
    scores 由 generate_topk_idx(rank_count) 反推成 topk_idx，再把 topk 位的 score 抬到高分
else:
    factor = map_unbalanced_ratio_to_factor(...)  # 二分搜索：把 ratio 映射成 score 因子
    scores = get_scores_by_factor(factor)         # rank0 专家 score∈[0,factor)，其余∈[0,1)
                                                  # factor 越小，rank0 越容易被 topk 选中 → 越多 token 涌向 rank0
return scores
```

`get_scores_by_factor` 的直觉：把「特殊 rank（rank 0）的专家」的 score 上限压到 `factor`（默认搜索范围 `[1,100]`），其余 rank 的 score 上限为 1。`factor` 越小，rank 0 的专家越难竞争过别人？——其实方向相反：当 `factor<1` 时 rank 0 的 score 普遍更小，更难进 topk，于是 token **避开** rank 0；当 `factor` 大时 rank 0 反而吸 token。`map_unbalanced_ratio_to_factor` 用二分找到恰好产生目标 `ratio` 的 `factor`。

`map_unbalanced_ratio_to_factor` 的判据（第 160 行）：当 `counts[0] > mean(counts[1:]) * ratio` 时说明 rank 0 已经比均值多出 `ratio` 倍以上，说明当前 `factor` 让 rank 0 太「吸 token」了，应减小上界 → `factor_r = factor_mid`；否则 `factor_l = factor_mid`。二分 20 次收敛。

#### 4.3.3 源码精读

分发入口（按 `precise` 选路）：

- [deep_ep/utils/gate.py:L176-L180](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/utils/gate.py#L176-L180) —— `get_unbalanced_scores`：`precise` 为真走精确构造，否则走随机构造。

精确路径：`generate_rank_count` 严格构造每 token 的跨 rank 计数（含 `upper_bound_per_token = num_normal_ranks/ratio + 1` 的上界、特殊 rank 的强制/可选纳入逻辑）：

- [deep_ep/utils/gate.py:L32-L113](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/utils/gate.py#L32-L113) —— `generate_rank_count`：第 54 行算每 token 跨 rank 数上界；第 60–68 行把特殊 rank 的 token 数压到约 `normal_token_count * ratio`；末尾用 `scatter_add_` 汇总成 `[num_tokens, num_ranks]` 计数。
- [deep_ep/utils/gate.py:L116-L137](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/utils/gate.py#L116-L137) —— `get_precise_unbalanced_scores`：低位 score 采样到 `threshold=0.9`，topk 位抬到 `(0.9,1.0)`，保证 `torch.topk` 选出想要的 `topk_idx`。

随机路径：二分搜索 `factor`：

- [deep_ep/utils/gate.py:L148-L164](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/utils/gate.py#L148-L164) —— `map_unbalanced_ratio_to_factor`：20 次二分，判据为 `counts[0] > counts[1:].mean() * ratio`。
- [deep_ep/utils/gate.py:L140-L145](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/utils/gate.py#L140-L145) —— `get_scores_by_factor`：rank 0 段 score 上限 `factor`，其余段上限 1。
- [deep_ep/utils/gate.py:L167-L173](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/utils/gate.py#L167-L173) —— `get_random_unbalanced_scores`：`ratio==1.0` 时 `factor=1.0`（完全均匀）。

测试里的接入点：

- [tests/elastic/test_ep.py:L74-L77](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/tests/elastic/test_ep.py#L74-L77) —— `scores = get_unbalanced_scores(...)`，随即 `torch.topk` 得 `topk_weights, topk_idx`。`--unbalanced-ratio`（默认 1.0）与 `--precise-unbalanced-ratio` 两个命令行开关控制路径（见第 596–597 行参数定义）。
- [tests/elastic/test_ep.py:L539-L542](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/tests/elastic/test_ep.py#L539-L542) —— `--precise-unbalanced-ratio` 时打印黄色警告：测试数据是人工构造的，可能与真实分布不同。

#### 4.3.4 代码实践

> **实践目标**：直观感受 `ratio` 如何改变 token 的跨 rank 分布。

**操作步骤**（可在单卡上跑，不需要分布式）：

```python
import torch
from deep_ep.utils.gate import get_unbalanced_scores, map_unbalanced_ratio_to_factor

num_tokens, num_experts, num_ranks, num_topk = 4096, 256, 8, 6
for ratio in (1.0, 2.0, 4.0):
    scores = get_unbalanced_scores(num_tokens, num_experts, num_ranks, num_topk, ratio, precise=False)
    _, topk_idx = torch.topk(scores, num_topk, dim=-1, largest=True, sorted=False)
    rank_idx = topk_idx // (num_experts // num_ranks)
    # 每个 token 至少命中哪些 rank
    hits = torch.nn.functional.one_hot(rank_idx, num_ranks).any(dim=1).sum(0)
    print(f"ratio={ratio}: 命中各 rank 的 token 数 = {hits.tolist()}, "
          f"rank0/均值 = {hits[0].float()/(hits[1:].float().mean()):.2f}, "
          f"factor = {map_unbalanced_ratio_to_factor(num_tokens, num_experts, num_ranks, num_topk, ratio):.3f}")
```

**需要观察的现象 / 预期结果**：

- `ratio=1.0` 时各 rank 命中数接近均匀，`rank0/均值 ≈ 1`。
- `ratio` 增大时，rank 0 命中数显著高于其它 rank 的均值，比值向 `ratio` 靠拢；`factor` 相应变化。
- 改用 `precise=True`，分布更「硬」，比例更贴合 `ratio`，但形态更人工。

> 若本机无 GPU，可改在 CPU 上把 `'cuda'` 改 `'cpu'`（`gate.py` 内部硬编码了 `'cuda'`，需局部改写为示例代码），或仅阅读上述逻辑作**源码阅读型实践**。

#### 4.3.5 小练习与答案

**练习 1**：`map_unbalanced_ratio_to_factor` 的二分区间是 `[1.0, 100.0]`，`factor` 越大，rank 0 越容易吸 token 还是越容易失 token？请结合 `get_scores_by_factor` 解释。

> **答**：越容易**吸** token。`get_scores_by_factor` 中 rank 0 段 score 上限是 `factor`，其余 rank 段上限是 1。`factor` 越大，rank 0 的 score 普遍越高，越容易在 `torch.topk(largest=True)` 中胜出 → 更多 token 的 topk 落到 rank 0 → rank 0 命中数增多。当 `counts[0] > mean(counts[1:]) * ratio` 时二分就把上界收窄（`factor_r = factor_mid`），逐步逼近目标 ratio。

**练习 2**：`generate_rank_count` 里 `upper_bound_per_token = int(num_normal_ranks / ratio) + 1` 这个上界是干什么用的？

> **答**：它限制「单个 token 最多跨多少个 rank」。`ratio` 越大，特殊 rank 越要独占 token，普通 rank 共同分摊的份额 `a` 越小，故每 token 跨 rank 数上界随 `ratio` 下降。这是精确构造路径为了能稳定把 token 分到「特殊 rank + 少量普通 rank」、并把特殊 rank 流量做到 `ratio` 倍所必需的容量约束。

---

## 5. 综合实践

把本讲三件事串起来：**造不均衡输入 → 跑被测内核与参考实现 → 逐位比对 + 带宽测量**。

> **任务**：在单机 8 卡上，用 `test_ep.py` 的命令行旋钮组合出一个「高不均衡 + 跳过正确性只看性能」的压测，并解读输出。

**步骤**：

1. 运行（精确不均衡，ratio=4，只测性能）：

   ```bash
   python tests/elastic/test_ep.py --precise-unbalanced-ratio --unbalanced-ratio 4 \
       --skip-check --num-tokens 8192
   ```

2. 记录打印的 dispatch / combine 的 SU 带宽与延迟（us）。

3. 改 `--unbalanced-ratio 1`（均匀）再跑一次，对比 SU 带宽与延迟。

4. （可选）去掉 `--skip-check`，确认即便在 ratio=4 的极端不均衡下，`ref_dispatch` 与 DeepEP 的逐位断言仍然通过。

**需要解释的现象**：

- 不均衡加大时，某些 rank 的 `num_recv_tokens` 显著变大，单次 dispatch 的延迟（us）通常上升；但因不均衡也改变了字节总量，带宽（GB/s）的升降需结合具体分布看——这正是为什么测试要同时报告 `bytes` 与 `GB/s`。
- 正确性断言（4.1 的 `torch.equal`）与不均衡程度**无关**：无论 token 怎么分布，DeepEP 与参考实现路由到的 `src_token_global_idx` 多重集始终一致，排序后始终逐位相等。

> 若无 8 卡环境，可改为阅读 `test_dispatch_combine` 全函数，画出「输入生成（gate）→ dispatch（被测+参考）→ 比对（refs）→ 计时」的调用关系图作为替代任务。本综合实践涉及真实分布式运行，部分数字为**待本地验证**。

## 6. 本讲小结

- **参考实现是金标准**：`refs.py` 的 `dispatch`/`combine` 用标准 `all_to_all_single` 实现，「笨但显然正确」，与 DeepEP 高性能内核逐位比对；其 `ref_recv_src_token_idx` 因 `nonzero()` 升序 + 按源 rank 段拼接而**天然按全局索引升序**。
- **断言成立的三要素**：同一多重集 + 参考端已升序 + DeepEP 端排序后对齐，三者共同使 `torch.equal(ref_recv_src_token_idx, sorted_src_token_global_idx)` 成立。
- **`bench_kineto` 解决两个测量难题**：用 Kineto 拿 per-kernel 耗时（拆分 `dispatch_impl` 与 copy epilogue），用「大 sleep + 通信 barrier」压平多 rank launch 不均；返回的单内核耗时由调用方换算成「逻辑带宽」。
- **`get_unbalanced_scores` 提供可控不均衡**：精确路径（`generate_rank_count`）严格构造倍率但人工，随机路径（二分 `factor`）分布自然；二者都为压测贴近真实 MoE 负载服务。
- **测试体系三件套各司其职**：`refs.py` 管「对不对」、`testing.py` 管「快不快」、`gate.py` 管「输入像不像真实」；`test_ep.py` 是把它们与被测 `ElasticBuffer` 缝合在一起的主驱动。

## 7. 下一步学习建议

- **横向看其它测试**：用本讲的方法论去读 `tests/elastic/test_barrier.py`、`test_engram.py`、`test_pp.py`、`test_agrs.py`，它们同样依赖 `refs`/`testing`/`gate`，但分别对应 u7 的四个实验特性。可注意 AGRS 测试（u7-l4）不走 JIT kernel，基准方式略有不同。
- **回看被测对象**：现在你已理解测试如何验证正确性，可带着「断言在检查什么」的视角重读 u5-l1（直接 dispatch）与 u6-l1（combine 主流程），看 `recv_src_metadata[:,0]`、`psum_num_recv_tokens_per_expert` 等字段是如何被测试逐项断言的。
- **动手扩展测试**：尝试给 `test_ep.py` 加一个新的不均衡 ratio 档位，或写一个仅比较 `ref_combine` 与 DeepEP combine 在 `allow_multiple_reduction` 开关下输出差异的最小脚本，巩固对参考实现归约语义（u6-l2）的理解。
