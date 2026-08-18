# u2-l4 prefill.json 解析：双微批重叠与计算内核图谱

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 prefill.json 的采集配置：EP32 / TP1 / 4K prompt / 每 GPU 16K tokens，以及它与 train.json（EP64）的本质区别——这是**推理预填充**轨迹，没有 1F1B 注解，只有一条前向流水线。
2. 只用 `args.stream` 字段就把 4111 个 GPU 内核分成三条职责清晰的流：stream 7（计算）、stream 16（DeepEP 通信）、stream 13（NCCL 集合通信）。
3. 用数字证明「两个微批重叠计算与 all-to-all」不是 README 的一句口号：通信流忙时的 **91.2%** 期间计算流在同时跑内核。
4. 认识计算侧的四大内核家族：DeepGEMM 的 `fp8_gemm_kernel` 与 grouped GEMM、MoE 路由与融合内核链（`clean_and_count_expert → get_fused_mapping → expand_to_fused → … → reduce_fused`，上游是 `top2_sum_gate`）、以及 MLA 预填充注意力 `flash::compute_attn_ws`。
5. 用「次数的算术」反推出模型结构：61 层 = 3 个 dense 层 + 58 个 MoE 层，全程跑了两遍（两个微批）。

本讲所有统计数字都由文中给出的 `jq` / `grep` 命令在仓库当前 HEAD（`4496024`）上实际运行得出，可以逐条复现。

## 2. 前置知识

本讲假设你已读过 u1-l3（轨迹顶层结构）、u1-l4（事件字段与元数据）、u2-l1（CPU-GPU 关联）和 u2-l3（DeepEP 内核族）。在此基础上补充四个概念：

- **微批（micro-batch）**：把一个大批量切成若干小批交错执行。prefill 用 2 个微批：当微批 A 在做 MoE 的 all-to-all 通信时，GPU 同时算微批 B 的注意力/GEMM。README 明确要求两个微批的注意力负载均衡，甚至会把同一条 prompt 拆到两个微批里（[README.md:L22-L22](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/README.md#L22-L22)）。
- **流（stream）与队列**：回顾 u1-l4——`thread_name` 元数据把 GPU 0 上的 tid 命名为 `stream 7` / `stream 13` / `stream 16`，stream 是 GPU 上的任务队列：同一流内内核串行，不同流之间可以并行。这是本讲一切重叠分析的物理基础。
- **MLA（Multi-head Latent Attention）**：DeepSeek-V3/R1 的注意力。先把 7168 维隐向量压缩成低秩潜在向量（q 侧 1536 维、KV 侧 512 维 + 64 维 RoPE），推理时把 KV 权重「吸收」进查询，注意力只对压缩向量做。理解本讲不需要细节，只需要记住几个维数：隐向量 7168、128 个头、每头 qk 维 192（128 nope + 64 rope）、v 维 128。
- **FP8 量化 GEMM**：权重与激活以 FP8（E4M3）存储、逐 token（per-token）缩放因子做反量化，GEMM 累加用 FP32。轨迹里的 `per_token_cast_to_fp8_*` 就是量化内核，`dpsk::gemm::fp8_gemm_kernel` 是 DeepGEMM 的 FP8 矩阵乘内核。

还有一个容易踩的坑（u2-l3 讲过同类教训）：**不要用内核名做跨层 join 或族归类时掉进子串陷阱**。本仓库里存在命名空间 `cdpsk::ep::CUB_…`（CUB 扫描内核，属于采样尾段、跑在 stream 7），用 `contains("dpsk::ep::")` 匹配会把它们误算进 DeepEP 通信族（596 个 vs 真实 580 个）。用 `args.stream == 16` 分类才干净。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [prefill.json](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/prefill.json) | 本讲主角：EP32 预填充轨迹，85422 条 traceEvents，4111 个 GPU 内核；17.4 MB、57 万行的多行 JSON，可以做行号永久链接 |
| [README.md](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/README.md) | L16–L22 是 Prefilling 一节：EP32/TP1、4K prompt、16K tokens/GPU、双微批重叠与注意力均衡的官方说明 |
| [assets/prefill.jpg](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/assets/prefill.jpg) | README 配图：最上方 CPU 进程（pid 1097）密集成片的算子记录，下方 GPU 0 展开成两条 stream 轨道——计算流内核密集短小、通信流是一排长条，两者时间上交叠；GPU 1~7 轨道为空 |

先看文件头，确认采集口径。设备属性记录了 1 块 NVIDIA H800（132 个 SM、80 GB 显存、计算能力 9.0），分布式信息是 `backend=nccl, rank=0, world_size=32`：

[prefill.json:L2-L13](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/prefill.json#L2-L13) —— 这 12 行依次是 `schemaVersion: 1`、`deviceProperties`（H800 硬件参数，`numSms: 132`、`totalGlobalMem` 约 79.1 GiB）和 `distributedInfo`（EP32 的直接证据）。`world_size: 32` 是区分三份轨迹最快的判据（train=64、prefill=32、decode=128）。

事件总量与类别分布（`jq -r '.traceEvents[] | "\(.ph)\t\(.cat)"' prefill.json | sort | uniq -c | sort -rn`）：

| ph | cat | 条数 | 说明 |
| --- | --- | --- | --- |
| X | cuda_runtime | 25606 | CPU 侧 CUDA API 调用 |
| f/s | ac2g | 25280 / 4845 | CPU→GPU 箭头 |
| X | cpu_op | 25125 | CPU 算子 |
| X | **kernel** | **4111** | **GPU 内核，本讲主角** |
| X | cuda_driver | 367 | 驱动层 API |
| M | null | 38 | 进程/线程命名元数据 |
| X | gpu_memcpy | 37 | 拷贝（全部在 stream 7） |
| X | user_annotation | 6 | 全部是 `nccl:all_reduce` |
| X | gpu_memset | 4 | 清零 |

注意与 train.json 的关键差异：**这里没有 `ProfilerStep#1`、`1F1B` 之类的 user_annotation**——那套注解是训练框架（DualPipe）主动打的；推理预填充只有 6 条 NCCL 注解。所以分析 prefill 要直接从 GPU 内核入手，这正是本讲的方法。

## 4. 核心概念与源码讲解

### 4.1 EP32 双微批重叠策略：把 all-to-all 藏进计算里

#### 4.1.1 概念说明

MoE 层的专家分布在 32 张卡上（EP32），每个 token 要取 top-2 专家，就必须做两次方向相反的 all-to-all：`dispatch` 把 token 发往专家所在 GPU，`combine` 把专家输出聚合回来。这两步通信很贵——本轨迹里 DeepEP 内核占了 GPU 内核总忙时的 **48.0%**。如果通信和计算串行，GPU 一半时间在等网络。

预填充的解法是把批量切成两个微批 A/B，按层交错：

```
微批 A:  [ attn(L) | MoE(L): dispatch → 专家GEMM → combine | attn(L+1) | ...
微批 B:       [ attn(L) | MoE(L): dispatch → 专家GEMM → combine | ...
                    ↘ 通信与对方的计算在各自 stream 上并行 ↗
```

通信跑在 stream 16、计算跑在 stream 7，两条队列互不阻塞。代价是必须让两个微批的计算量差不多——否则一侧算完另一侧还在通信，气泡就回来了。所以 README 特意强调注意力负载要均衡（同一条 prompt 可能被拆开）。

#### 4.1.2 核心流程

按 `args.stream` 把 4111 个内核分组（命令见 4.1.4），得到本讲最基础的一张表：

| stream | 内核数 | 忙时（μs） | 角色 | 代表内核 |
| --- | --- | --- | --- | --- |
| 7 | 3525 | 1,929,558 | 计算 | `fp8_gemm_kernel`、`compute_attn_ws`、MoE 路由链、量化/归一化 |
| 16 | 580 | 1,799,499 | **DeepEP 通信** | `internode::dispatch/combine/notify_dispatch/cached_notify`、`get_dispatch_layout` |
| 13 | 6 | 20,236 | NCCL 集合通信 | `ncclDevKernel_AllReduce_*` |

时间总量：内核时间跨度 2,120,106 μs（约 2.12 s），全部内核忙时之和 3,749,293 μs（两条流并行所以超过跨度）。定义两个重叠率（同一 GPU 上通信区间与计算区间的交集）：

\[ R_{\text{comm}} = \frac{|\,\text{comm} \cap \text{comp}\,|}{|\text{comm}|}, \qquad R_{\text{comp}} = \frac{|\,\text{comm} \cap \text{comp}\,|}{|\text{comp}|} \]

实测交集为 1,641,355 μs，于是：

\[ R_{\text{comm}} = \frac{1\,641\,355}{1\,799\,499} \approx 91.2\%, \qquad R_{\text{comp}} = \frac{1\,641\,355}{1\,929\,558} \approx 85.1\% \]

即：**通信流 91.2% 的时间里有计算内核同时在跑；580 个通信内核中 579 个（99.8%）的窗口内都能找到重叠的计算内核**。再从时间线看稳态（把注意力内核与 dispatch/combine 按 `ts` 排序，时间偏移取相对第一条 cpu_op 的毫秒数）：

```
 31.2  42.0  53.0  64.2  75.4  87.1 100.0 107.7 ms   attn ×8（热身：A0 B0 A1 B1 A2 B2 A3 B3）
108.8 dispatch(4.65ms)  116.4 dispatch(4.56ms)        ← 第一对 MoE 通信
124.7 combine(7.96ms)   134.4 combine(8.43ms)
135.1 attn(3.05ms) ────────┐                          ← attn 与第二个 combine(134.4~142.8) 重叠！
144.5 dispatch(4.88ms)     │
145.2 attn(3.05ms) ←──重叠─┘  151.7 dispatch(4.53ms)
159.5 combine(8.16ms)      168.8 combine(8.71ms)
169.9 attn(2.83ms) ←重叠    179.2 dispatch(4.30ms)
179.7 attn(2.80ms) ←重叠    186.3 dispatch(4.50ms)
193.1 combine(9.41ms)      203.1 combine(8.63ms)
204.8 attn(2.75ms) ←重叠    ……稳态循环，约 34.7 ms/层/微批
```

两个可检验的推论：

1. **热身期恰好 8 个注意力内核后出现第一个 dispatch**。DeepSeek-V3 共 61 层，前 3 层是 dense（无 MoE），第 4 层起是 MoE。每个微批先跑 3 个 dense 层 + 第 1 个 MoE 层的注意力（4 次），两个微批交错共 8 次，然后第一个 MoE 的 dispatch 才需要发数据。完全吻合。
2. **两个微批的注意力负载几乎相等**：按时间排序取偶/奇序号的 122 个 `compute_attn_ws`，平均时长 2814.2 μs vs 2802.0 μs——差 0.4%。这就是「注意力计算负载均衡」的定量证据。

#### 4.1.3 源码精读

通信流的五个成员全部落在 stream 16，各出现 116 次（= 2 微批 × 58 MoE 层）：

| 内核（截断） | 次数 | 总时长(μs) | 均值(μs) | 首现行号 |
| --- | --- | --- | --- | --- |
| `dpsk::ep::internode::combine<false, 4, __nv_bfloat16,…>` | 116 | 1,006,189 | 8674 | [L178462](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/prefill.json#L178462-L178462) |
| `dpsk::ep::internode::dispatch<false, 4, false, 4>(…)` | 116 | 532,860 | 4594 | [L175934](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/prefill.json#L175934-L175934) |
| `dpsk::ep::internode::notify_dispatch<false, 4>(…)` | 116 | 123,618 | 1064 | [L176986](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/prefill.json#L176986-L176986) |
| `dpsk::ep::internode::cached_notify<false>(…)` | 116 | 119,979 | 1034 | [L178406](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/prefill.json#L178406-L178406) |
| `dpsk::ep::cuda::get_dispatch_layout<256, 32, 8>(…)` | 116 | 16,853 | 145 | [L176846](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/prefill.json#L176846-L176846) |

引用 dispatch 内核的完整事件（注意它的资源占用极低）：

[prefill.json:L175933-L175944](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/prefill.json#L175933-L175944) —— 这是一条 `ph:"X"` 完成事件：`pid: 0, tid: 16`（GPU 0 的通信流），`ts/dur` 是执行区间，`args` 里有 `stream: 16`、`correlation`、寄存器/共享内存/占用率等 CUPTI 采集的运行时信息：

```json
{
  "ph": "X", "cat": "kernel", "name": "void dpsk::ep::internode::dispatch<false, 4, false, 4>(int4*, float*, long*, float*, dpsk::ep::internode::SourceMeta*, …)", "pid": 0, "tid": 16,
  "ts": 1740462779937873, "dur": 5189,
  "args": {
    "External id": 134683,
    "queued": 0, "device": 0, "context": 1,
    "stream": 16, "correlation": 134683,
    "registers per thread": 113,
    "shared memory": 172,
    "blocks per SM": 0.18181819,
    "warps per SM": 2.909091,
    "grid": [24, 1, 1],
```

两个值得盯的细节：

- `grid: [24, 1, 1]`、`blocks per SM ≈ 0.18`——dispatch 只用 24 个 block，占满 132 个 SM 的 H800 的极小一角，主要在搬数据（RDMA）而非算数。对比 4.4 节的注意力内核 `grid: [32, 128, 2]`、`warps per SM ≈ 744`，一边是「轻内核长等待」，一边是「重内核满载」。
- 通信族的 `pid/tid` 全部是 `0/16`；文件中第一个 `"stream": 16` 出现在 [L175939](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/prefill.json#L175939-L175939)（就是上面这个 dispatch 的 args 行）。

stream 13 的 6 个内核是 `ncclDevKernel_AllReduce_Sum_{u64,u32,f32}_TREE_LL`，与 6 条 `nccl:all_reduce` user_annotation 一一对应（2 条在轨迹开头 ~0.8/20.7 ms 处，4 条在结尾 ~2093 ms 处，属于采样/同步阶段的集合通信，其中最长一次 19,393 μs）。它们与 MoE 的 all-to-all 无关——MoE 通信全走 DeepEP。

#### 4.1.4 代码实践

**实践目标**：亲手得到上面那张「流分工表」，并验证注意力均衡。

**操作步骤**（在本仓库根目录执行，均已实际运行）：

```bash
# 1) 按 stream 统计内核数量与忙时（即本讲实践任务的第一问）
jq -r '[.traceEvents[] | select(.cat=="kernel")]
  | group_by(.args.stream)
  | map({stream: .[0].args.stream, n: length, us: (map(.dur//0)|add)})
  | sort_by(.stream) | .[] | "stream=\(.stream) n=\(.n) busy_us=\(.us)"' prefill.json

# 2) 每条流的代表内核（按 (stream, name) 聚合，全局按总时长排序取前 20 行，行首 s7/s16 即流编号）
jq -r '[.traceEvents[] | select(.cat=="kernel")]
  | group_by([.args.stream, .name])
  | map({s: .[0].args.stream, n: length, us: (map(.dur//0)|add), name: .[0].name})
  | sort_by(-.us) | .[] | "s\(.s)\t\(.n)\t\(.us)\t\(.name[0:50])"' prefill.json | head -20

# 3) 验证两个微批的注意力均衡：按 ts 排序取偶/奇序号的平均时长
jq -r '[.traceEvents[] | select(.cat=="kernel" and (.name|contains("compute_attn_ws")))]
  | sort_by(.ts) | [to_entries[] | select(.key % 2 == 0) | .value.dur]
  | "even(micro-batch A): n=\(length) avg=\(add/length)"' prefill.json
jq -r '[.traceEvents[] | select(.cat=="kernel" and (.name|contains("compute_attn_ws")))]
  | sort_by(.ts) | [to_entries[] | select(.key % 2 == 1) | .value.dur]
  | "odd(micro-batch B):  n=\(length) avg=\(add/length)"' prefill.json
```

**需要观察的现象**：命令 1 输出 `stream=7 n=3525`、`stream=13 n=6`、`stream=16 n=580`；命令 2 中 stream 16 的全部条目都是 `dpsk::ep::` 内核；命令 3 两组均值只差十几个微秒。

**预期结果**：与 4.1.2 的表格完全一致（数字即上面实测值）。若你自己写 Python 版本（见第 5 节综合实践），应得到同一张表。

#### 4.1.5 小练习与答案

**练习 1**：通信流忙时占全部内核忙时的比例是多少？如果改成「通信与计算完全串行」，这一步预填充大约要多花多久？

答案：\( 1\,799\,499 / 3\,749\,293 \approx 48.0\% \)。串行下界 ≈ 计算忙时 + 通信忙时 = 1,929,558 + 1,799,499 ≈ 3.73 s，而实际时间跨度只有约 2.12 s——粗略估算双流重叠带来了至少 1.7 倍的加速（忽略流内空隙，属保守估计）。

**练习 2**：为什么元数据里给 GPU 0~7 都起了 `process_labels`，但内核只出现在 GPU 0？

答案：这是查看器版式（u1-l2 讲过）：PyTorch 导出时预留 8 条 GPU 轨道，而本轨迹是 `rank: 0` 的单进程视角，CUPTI 只采集本机 GPU 0 的活动，GPU 1~7 轨道为空。

**练习 3**：`jq` 里用 `select(.name|contains("dpsk::ep::"))` 统计 DeepEP 内核会得到 596 个而不是 580 个，多出来的 16 个是什么？

答案：是命名空间为 `cdpsk::ep::CUB_…` 的 CUB 扫描内核（`DeviceScanByKeyKernel` 等），它们属于采样阶段的 top-k/去重逻辑，跑在 stream 7；`cdpsk::ep::` 包含子串 `dpsk::ep::` 而被误匹配。按 `args.stream == 16` 过滤才准确。

### 4.2 DeepGEMM fp8_gemm_kernel 与 grouped GEMM

#### 4.2.1 概念说明

预填充的计算主干是矩阵乘。本轨迹的 GEMM 由三个实现承担：

- **`dpsk::gemm::fp8_gemm_kernel<N, K, …, GemmType>`**——DeepGEMM 的 FP8 内核，模板参数前两位是 N 和 K（输出/输入维度），最后一位是 DeepGEMM 的内核类型枚举（0 与 1 是两种变体，语义见下）。共 842 次、累计 1,155,523 μs，是单一最大的计算内核族。
- **`cutlass::device_kernel<dpsk::grouped_gemm::cuda::fp8_ptp128c_outfmt_head::GemmKernel>`**——注意力前置的 grouped GEMM，122 次、68,640 μs（它在流水线里的角色见 4.4 节）。
- **`sm90_xmma_gemm_bf16f32…_cublas`**——cuBLAS 的 BF16 小 GEMM，116 次、11,051 μs，紧贴在路由打分之前，尺寸/时序上对应 **router 的隐向量→专家 logits 投影**（BF16 精度、每次 ~90 μs，其后紧跟高斯噪声内核，符合 V3 的带噪路由；具体映射建议对照模型结构确认）。

GEMM 家族合计约 1,238,163 μs，占内核总忙时的 33.0%。

#### 4.2.2 核心流程

把 842 个 `fp8_gemm_kernel` 按模板形状聚合（完整命令见 4.2.4），每一个形状都能对上 DeepSeek-V3 的一个真实投影层维数：

| 形状 `<N, K, GemmType>` | 次数 | 均值(μs) | 对应投影（维数拆解） |
| --- | --- | --- | --- |
| `<2112, 7168, 0>` | 122 | 264 | 融合 qkv_a 下投影：\( 2112 = 1536 + 512 + 64 \)（q_lora + kv_lora + rope） |
| `<24576, 1536, 0>` | 122 | 854 | q_b 上投影：\( 24576 = 128 \times (128 + 64) \) |
| `<7168, 16384, 0>` | 122 | 1703 | o_proj：\( 16384 = 128 \times 128 \)（v_head_dim） |
| `<4096, 7168, 0>` | 116 | 448 | MoE 门/升融合投影（短变体）：\( 4096 = 2 \times 2048 \) |
| `<4096, 7168, 1>` | 116 | 3567 | MoE 门/升融合投影（长变体） |
| `<7168, 2048, 0>` | 116 | 313 | MoE 下降投影（短变体） |
| `<7168, 2048, 1>` | 116 | 2402 | MoE 下降投影（长变体） |
| `<36864, 7168, 0>` | 6 | — | dense 层 FFN 门/升：\( 36864 = 2 \times 18432 \) |
| `<7168, 18432, 0>` | 6 | — | dense 层 FFN 下降投影 |

次数的算术自洽地编码了模型结构：

\[ 842 = \underbrace{3 \times 122}_{\text{注意力三投影} \times 2\text{微批} \times 61\text{层}} + \underbrace{4 \times 116}_{\text{MoE 两投影} \times 2\text{变体} \times 2\text{微批} \times 58\text{层}} + \underbrace{2 \times 6}_{\text{dense FFN} \times 2\text{微批} \times 3\text{层}} \]

关于同一形状出现 `GemmType)0`（短，~450 μs）与 `GemmType)1`（长，~3.6 ms）两种变体各 116 次：一个与 V3 结构吻合的推断是**短变体对应共享专家的普通 GEMM、长变体对应 8 个路由专家的 grouped（连续布局）GEMM**——两者中间维数同为 2048 所以形状一致，但路由专家要处理收到的全部 token，计算量大得多。该映射在本轨迹内无法进一步证实，标注**待确认**（建议对照 DeepGEMM 源码中 `GemmType` 枚举定义）。

#### 4.2.3 源码精读

MoE 长变体下降投影（`tid: 7`，计算流）：

[prefill.json:L176649-L176660](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/prefill.json#L176649-L176660) —— `fp8_gemm_kernel<7168u, 2048u, 128u, 128u, 128u, 5u, 128u, 128u, 2u, (GemmType)1>`，`dur: 2519`，`registers per thread: 168`、`shared memory: 199312`（接近 H800 的 232448 optin 上限，见 [L3-L12](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/prefill.json#L3-L12) 的设备属性）、`grid: [108, 1, 1]`：

```json
{
  "ph": "X", "cat": "kernel", "name": "void dpsk::gemm::fp8_gemm_kernel<7168u, 2048u, 128u, 128u, 128u, 5u, 128u, 128u, 2u, (dpsk::gemm::GemmType)1>(__nv_bfloat16*, float*, int*, …)", "pid": 0, "tid": 7,
  "ts": 1740462779947924, "dur": 2519,
  "args": {
    "External id": 135062,
    "queued": 0, "device": 0, "context": 1,
    "stream": 7, "correlation": 135062,
    "registers per thread": 168,
    "shared memory": 199312,
    "blocks per SM": 0.8181818,
    "warps per SM": 9.818182,
    "grid": [108, 1, 1],
```

注意力侧三个投影与 grouped GEMM 的代表行（点开看完整签名）：

- 融合 qkv_a 投影 [prefill.json:L178750](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/prefill.json#L178750-L178750) —— `fp8_gemm_kernel<2112u, 7168u, …, GemmType)0>`，122 次。
- q_b 投影 [prefill.json:L178978](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/prefill.json#L178978-L178978) —— `fp8_gemm_kernel<24576u, 1536u, …>`，122 次。
- o_proj [prefill.json:L179482](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/prefill.json#L179482-L179482) —— `fp8_gemm_kernel<7168u, 16384u, …>`，122 次。
- MoE 门/升投影（长变体首现）[prefill.json:L176454](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/prefill.json#L176454-L176454) —— `fp8_gemm_kernel<4096u, 7168u, …>`。
- 注意力前置 grouped GEMM [prefill.json:L179169-L179180](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/prefill.json#L179169-L179180) —— `cutlass::device_kernel<dpsk::grouped_gemm::cuda::fp8_ptp128c_outfmt_head::GemmKernel>`，`dur: 627`、`shared memory: 217636`、`warps per SM: 9.8`。

配合 4.1 节 dispatch 的 `warps per SM: 2.9` 对比着看：**GEMM 内核是「重」的（大 shared memory、高占用），通信内核是「轻」的（小 grid、低占用、长时间）**——这正是两条流能并行不互相抢 SM 的原因之一。

#### 4.2.4 代码实践

**实践目标**：自己产出「GEMM 形状 ↔ 维数」对照表，验证 842 = 3×122 + 4×116 + 2×6。

**操作步骤**：

```bash
# 按完整内核名聚合计数与总时长，截取前 60 字符便于阅读
jq -r '[.traceEvents[] | select(.cat=="kernel")]
  | group_by(.name)
  | map({name: .[0].name, n: length, us: (map(.dur//0)|add)})
  | sort_by(-.us) | .[] | "\(.n)\t\(.us)\t\(.name[0:60])"' prefill.json | head -30

# 只看 fp8_gemm 家族的总量校验
jq -r '[.traceEvents[] | select(.cat=="kernel" and (.name|contains("fp8_gemm_kernel"))]
  | "n=\(length) us=\(map(.dur//0)|add)"' prefill.json
```

**需要观察的现象**：第一条命令的头部是 combine/dispatch/两个 fp8_gemm 长变体/compute_attn_ws；形状列表里每个注意力投影恰好 122 次、MoE 投影 116 次、dense 投影 6 次。

**预期结果**：`fp8_gemm_kernel` 家族 `n=842 us=1155523`（实测值）；形状表与 4.2.2 一致。

#### 4.2.5 小练习与答案

**练习 1**：`<2112, 7168>` 里的 2112 是哪三部分之和？

答案：\( 2112 = 1536\,(\text{q\_lora}) + 512\,(\text{kv\_lora}) + 64\,(\text{rope}) \)，即 MLA 把 q 压缩、KV 压缩与 RoPE key 三个下投影融合成一次 GEMM。

**练习 2**：不看名字只看次数，如何区分 `<4096, 7168, 0>`（116 次）与 `<2112, 7168, 0>`（122 次）各属于什么？

答案：122 = 2 × 61（每层都有，含 3 个 dense 层）→ 注意力侧投影；116 = 2 × 58（只有 MoE 层）→ MoE 侧投影。次数本身就是结构指纹。

**练习 3**：为什么 `<4096, 7168, 1>`（均值 ~3.6 ms）比同形状的 `GemmType)0`（~450 μs）慢约 8 倍？

答案（推断，待确认）：两者中间维数相同但处理的 token 集合不同——短变体大概率只算本卡 token（如共享专家），长变体要算经 dispatch 汇聚来的全部路由 token（grouped 布局），矩阵规模大得多；具体枚举语义以 DeepGEMM 源码为准。

### 4.3 MoE 路由与融合内核链

#### 4.3.1 概念说明

dispatch 内核只负责「搬运」，搬运之前之后各有一串小而关键的准备/收尾内核，它们共同构成 MoE 的路由数据通路。按时间顺序（同一微批、同一 MoE 层）：

```
router 打分（sm90 bf16 GEMM + 高斯噪声）
  → top2_sum_gate                # 选 top-2 专家、算组合权重与偏置修正
  → per_token_cast_to_fp8        # 量化
  → get_dispatch_layout (s16)    # 本地算路由布局：每个 token 去哪个 GPU/Expert（与计算流并行）
  → notify_dispatch / cached_notify (s16)  # 与对端握手：交换接收计数/元数据
  → dispatch (s16)               # 发送 token 到专家所在 GPU
  → [对端] gate/up GEMM → swiglu(+权重) → down GEMM   # 专家计算
  → combine (s16)                # 聚合专家输出回源端
  → reduce_fused                 # 按 top-2 权重加权求和、反量化回 bf16
```

其中 `clean_and_count_expert → get_fused_mapping → expand_to_fused_with_scales`（配合 `grouped_gemm::utils::transpose`）负责把「按 token 排列」的路由结果重排成「按专家连续排列」的融合布局——这正是 grouped contiguous GEMM 需要的输入格式。整条链每环节恰好 116 次（2 微批 × 58 MoE 层），平均单次成本都在 25~330 μs 之间，属「小内核多次数」的胶水层。

#### 4.3.2 核心流程

从轨迹抽一个稳态片段（相对首条 cpu_op 的偏移，单位 ms；s7/s16 标流），可以完整读出这条链与 GEMM/注意力的穿插：

```
183.301 s7  sm90_xmma(bf16, 91μs)        ← router 打分
183.309 s7  噪声(10μs) → top2_sum_gate(71μs)
183.327 s7  clean_and_count_expert(116μs) ┐
183.327 s16 get_dispatch_layout(128μs)    │ 路由布局与清理并行
183.339 s7  get_fused_mapping(276μs)      │
183.367 s7  expand_to_fused_with_scales(326μs) + transpose(25μs) ┘
183.402 s7  fp8_gemm<4096,7168,1>(3718μs) ← 路由专家 gate/up
183.402 s16 notify_dispatch(1924μs)       ← 握手与 GEMM 并行
183.538 s16 dispatch(4584μs)              ← 发送（此刻 s7 在算另一个微批）
183.775 s7  swiglu(325μs) → fp8_gemm<7168,2048,1>(2519μs) ← down 投影
184.059 s7  reduce_fused(589μs)           ← 加权合并（对应更早一次 combine 的输出）
184.341 s16 combine(8508μs)               ← 聚合（期间 s7 继续跑下一个微批）
```

每个 MoE 层、每个微批的通信预算约为 \( 145 + 1064 + 1034 + 4594 + 8674 \approx 15.5\,\text{ms} \)（layout + notify + cached_notify + dispatch + combine 均值之和），而这些时间里有九成以上与计算流并行。

#### 4.3.3 源码精读

路由链内核在计算流（`tid: 7`）上的原始事件：

- [prefill.json:L176157-L176167](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/prefill.json#L176157-L176167) —— `cuda::clean_and_count_expert<512>`，`ts: 1740462779943125, dur: 116`，`grid: [8,1,1]`、`est. achieved occupancy %: 2`（极小内核）。它后面紧跟着 `ph:"f"`/`ph:"s"` 的 ac2g 流事件（`id` 同为 134948）和 CPU 侧 `cudaLaunchKernel`（`correlation: 134948`）——这就是 u2-l1 讲的三层记录在原始文件里的样子。
- [prefill.json:L176190](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/prefill.json#L176190-L176190) —— `cuda::get_fused_mapping<512>`，`dur: 276`，`shared memory: 2048`。
- [prefill.json:L176270](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/prefill.json#L176270-L176270) —— `cuda::expand_to_fused_with_scales_impl`，把 token 按映射展开到融合布局并携带缩放因子。
- [prefill.json:L176706](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/prefill.json#L176706-L176706) —— `cuda::reduce_fused_impl<__nv_bfloat16, true, false>`，combine 之后按路由权重加权合并。
- [prefill.json:L179790](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/prefill.json#L179790-L179790) —— `cuda::top2_sum_gate<32, 8, 256, 8, 4>`，链的起点：top-2 门控。模板里的 256 正对 V3 的 256 个路由专家、8 对应每卡 8 个专家（256/EP32）。

次数与时长汇总（jq 实测）：

| 内核 | 次数 | 总时长(μs) | 均值(μs) | 所在流 |
| --- | --- | --- | --- | --- |
| `top2_sum_gate` | 116 | 8,684 | 75 | 7 |
| `clean_and_count_expert` | 116 | 12,743 | 110 | 7 |
| `get_fused_mapping` | 116 | 30,604 | 264 | 7 |
| `expand_to_fused_with_scales` | 116 | 37,918 | 327 | 7 |
| `grouped_gemm::utils::transpose` | 116 | 2,949 | 25 | 7 |
| `reduce_fused` | 116 | 72,293 | 623 | 7 |

对比 u2-l3 的 train.json（整条 DeepEP 族只有 36 个内核）：prefill 里 dispatch 前每次都重新执行 `get_dispatch_layout` 与握手（各 116 次），说明**推理侧的路由每个 batch 都在真实变化，布局无法缓存复用**——尽管采集时模拟了均衡路由（README L3），布局计算本身仍然要做。

#### 4.3.4 代码实践

**实践目标**：统计路由相关内核的调用次数，验证「每 MoE 层每微批各一次」。

**操作步骤**：

```bash
jq -r '[.traceEvents[] | select(.cat=="kernel")
  | select((.name|contains("top2_sum_gate")) or (.name|contains("clean_and_count_expert"))
        or (.name|contains("get_fused_mapping")) or (.name|contains("expand_to_fused"))
        or (.name|contains("reduce_fused")) or (.name|contains("get_dispatch_layout")))]
  | group_by(.name)
  | map({n: length, us: (map(.dur//0)|add), stream: .[0].args.stream, name: .[0].name})
  | sort_by(-.us) | .[] | "\(.n)\t\(.us)\tstream=\(.stream)\t\(.name[0:50])"' prefill.json
```

**需要观察的现象**：六个内核全部 116 次；`get_dispatch_layout` 的 `stream=16`，其余 `stream=7`。

**预期结果**：与 4.3.3 表格一致（116/8684、116/12743、116/30604、116/37918、116/72293、116/16853）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 dispatch 之前需要 `notify_dispatch`/`cached_notify`？

答案：all-toall 的接收端必须提前知道「将要收到多少 token、来自谁」，才能分配缓冲与同步进度。notify 内核通过与对端交换接收计数/`SourceMeta` 完成握手；`cached_notify` 是复用先前轮次缓存元数据的快速路径。两者在本轨迹各 116 次、均值都约 1 ms。

**练习 2**：`reduce_fused` 做什么？为什么它跑在计算流而不是通信流？

答案：combine 只负责把各专家的输出原路送回源 GPU，返回的是「每个 (token, expert) 一份」的未加权结果；`reduce_fused` 在本地按 top-2 门控权重把它们加权求和并反量化回 bf16，是纯本卡计算，所以在 stream 7。

**练习 3**：把本节的 116 次与 4.2 节注意力投影的 122 次相除，你能反推出模型的 dense/MoE 层数吗？

答案：能。116 = 2×58、122 = 2×61，故 61 层中共 58 个 MoE 层、3 个 dense 层——正是 DeepSeek-V3 的结构；热身期「8 个注意力后才有第一个 dispatch」也印证了这一分层。

### 4.4 MLA 预填充注意力内核

#### 4.4.1 概念说明

预填充阶段要一次性处理整段 prompt（变长序列），注意力内核因此与解码阶段完全不同。本轨迹用的是 `flash::compute_attn_ws`——带 `_ws`（weight-sharing，权重共享/吸收式 MLA）的 FlashAttention 风格前向内核，配合 `SingleTileScheduler`（单块调度）与 `SeqLenTraits<true>`（变长序列 traits）：

- 模板 `Flash_fwd_kernel_traits<192, 128, 128, 12, 2, false, 1, bf16, 128>`：192 = 每头 qk 维（128 nope + 64 rope）、128 = v_head_dim、bf16 计算。
- 它前面的 `vllm::rotary_embedding_with_kv_cache_kernel`（122 次、均值 243 μs）给 q/k 施加 RoPE；`cutlass grouped_gemm<fp8_ptp128c_outfmt_head>`（122 次、均值 563 μs）紧贴其后、与注意力成对出现，推断是为 weight-sharing 注意力准备每 token 的合并权重/头格式数据（**待确认**，建议对照 FlashMLA/DeepGEMM 源码）。
- 输入侧的 FP8 量化由 `cuda::per_token_cast_to_fp8_with_channels` 完成：`channels=true` 变体 488 次、`false` 变体 116 次（= 4×122 与 1×116，分别对应注意力侧与 MoE 侧的逐 token 量化）。

注意力族合计 342,590 μs，占内核总忙时 9.1%——在 EP32 的预填充里，**注意力不是主角，通信与 MoE GEMM 才是**。

#### 4.4.2 核心流程

每个微批、每层的注意力数据通路（全部在 stream 7，顺序即 ts 顺序）：

```
_layer_norm（输入归一化）
  → fp8_gemm<2112, 7168>        # 融合 qkv_a 下投影（1536+512+64）
  → _layer_norm ×2               # q_a / kv_a 的 RMSNorm
  → per_token_cast_to_fp8        # 量化
  → fp8_gemm<24576, 1536>        # q_b 上投影（128 头 × 192）
  → vllm::rotary_embedding       # RoPE（q 与 rope 部分）
  → per_token_cast_to_fp8
  → cutlass grouped_gemm         # weight-sharing 前置准备（推断，待确认）
  → flash::compute_attn_ws       # 核心注意力（均值 ~2.8 ms）
  → per_token_cast_to_fp8
  → fp8_gemm<7168, 16384>        # o_proj 输出投影
  → 残差相加（vectorized_elementwise_add）→ 进入 MoE（4.3 节）
```

全轨迹 `_layer_norm_kernel` 367 次 ≈ 3×122 + 1（每层三处归一化 + 最终 norm），`vectorized_elementwise` 加法 118 次，与上述通路吻合。122 个 `compute_attn_ws` 的总时长 342,590 μs，均值 2,808 μs，其中偶/奇序号（两个微批）均值 2814.2 / 2802.0 μs——负载均衡的又一次印证。

#### 4.4.3 源码精读

[prefill.json:L179285-L179296](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/prefill.json#L179285-L179296) —— `compute_attn_ws` 事件：`dur: 3095`，资源特征是全轨迹最重的内核之一：

```json
{
  "ph": "X", "cat": "kernel", "name": "void flash::compute_attn_ws<Flash_fwd_kernel_traits<192, 128, 128, 12, 2, false, 1, cutlass::bfloat16_t, 128>, true, flash::SingleTileScheduler, flash::SeqLenTraits<true> >(…)", "pid": 0, "tid": 7,
  "ts": 1740462779964226, "dur": 3095,
  "args": {
    "External id": 136221,
    "queued": 0, "device": 0, "context": 1,
    "stream": 7, "correlation": 136221,
    "registers per thread": 168,
    "shared memory": 213088,
    "blocks per SM": 62.060608,
    "warps per SM": 744.7273,
    "grid": [32, 128, 2],
```

`warps per SM ≈ 744`（每 SM 最多 64 个 warp × 驻留多 block），对比 dispatch 的 2.9——**计算内核把 SM 吃满，通信内核几乎不占 SM**，两类内核因此可以在两条流上共存而不互相挤压（解码阶段更极端，u2-l5 会看到通信干脆不占 SM）。

配套内核的原始行：

- [prefill.json:L179034](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/prefill.json#L179034-L179034) —— `vllm::rotary_embedding_with_kv_cache_kernel<c10::BFloat16, false, false, true, 32>`，RoPE。
- [prefill.json:L178050](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/prefill.json#L178050-L178050) —— `cuda::per_token_cast_to_fp8_with_channels<__nv_bfloat16, 128, true, 128, false>`，逐 token FP8 量化（channels 变体）。
- [prefill.json:L176534](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/prefill.json#L176534-L176534) —— `cuda::swiglu_forward_with_weight_and_per_token_cast_to_fp8_with_channels_impl`（238 次：MoE 侧 SwiGLU，融合了路由权重乘法与输出量化）。

#### 4.4.4 代码实践

**实践目标**：提取注意力内核的时间序列，肉眼确认「两个微批交替、且与 dispatch/combine 重叠」。

**操作步骤**：

```bash
jq -r '[.traceEvents[] | select(.cat=="kernel")
  | select((.name|contains("compute_attn_ws")) or (.name|contains("internode::dispatch<"))
        or (.name|contains("internode::combine"))]
  | sort_by(.ts) | .[0:26][]
  | "\((.ts - 1740462778109854))\tdur=\(.dur)\ts\(.args.stream)\t\(.name[0:34])"' prefill.json
```

**需要观察的现象**：前 8 行全是 `compute_attn_ws`（热身），随后进入「attn、dispatch、dispatch、combine、combine、attn…」的循环；`attn` 的起始时间总是落在某个 `combine` 或 `dispatch` 的区间内。

**预期结果**：与 4.1.2 的时间线一致（如 attn@135067 与 combine@134407 dur8430 重叠、attn@145159 与 dispatch@144534 dur4882 重叠）。把这些对子数出来：122 个 attn 中至少有一半与通信窗口直接重叠。

#### 4.4.5 小练习与答案

**练习 1**：`Flash_fwd_kernel_traits<192, 128, …>` 里的 192 和 128 分别是什么？

答案：192 = 每头 qk 维数 = 128（nope 部分，与压缩 KV 做注意力）+ 64（RoPE 部分，单独做注意力）；128 = v_head_dim（输出值维数）。两者都是 DeepSeek-V3 MLA 的标准配置。

**练习 2**：`per_token_cast_to_fp8_with_channels` 的两个模板变体各出现多少次？分别对应哪一侧？

答案：`channels=true` 变体 488 次 = 4×122（注意力侧每层 4 次量化：qkv_a 后、q_b 后、前置 GEMM 后、o_proj 前）；`false` 变体 116 次 = 2×58（MoE 侧，dispatch 前的统一量化）。此分解与维数链吻合，具体逐次归属为推断。

**练习 3**：为什么预填充用 `compute_attn_ws` 而解码要用 `flash_fwd_splitkv_mla` 两段式内核？

答案：预填充一次读完整 prompt，单个序列块内就有大量 token 可并行，`SingleTileScheduler` 直接铺满 GPU；解码时每步只有 1 个 query token、KV 却长达数万 token，必须把 KV 切片分给多个 block 再 combine 汇总（split-KV）。这是下一讲 u2-l5/u2-l6 的主题。

## 5. 综合实践

把本讲实践任务完整做一遍：**对 prefill.json 的 kernel 事件按 `args.stream` 分组，统计每个流的内核数量与代表内核名，标出 DeepEP 通信所在的流；再统计 top2_sum_gate、clean_and_count_expert 等路由相关内核的调用次数。**

**核心命令（jq 版，已在本仓库当前 HEAD 实际运行）**：

```bash
# ── 流分组与代表内核 ──────────────────────────────
jq -r '[.traceEvents[] | select(.cat=="kernel")]
  | group_by([.args.stream, .name])
  | map({s: .[0].args.stream, n: length, us: (map(.dur//0)|add), name: .[0].name})
  | sort_by(.s, -.us)
  | group_by(.s)
  | map({stream: .[0].s, kernels: (map(.n)|add), busy_us: (map(.us)|add),
         top: ([.[] | "\(.name[0:44]) ×\(.n)"] | .[0:3])})
  | .[] | "stream \(.stream): \(.kernels) kernels, busy \(.busy_us)us\n  \(.top | join("\n  "))"' prefill.json

# ── 路由内核计数 ──────────────────────────────────
jq -r '[.traceEvents[] | select(.cat=="kernel")
  | select((.name|contains("top2_sum_gate")) or (.name|contains("clean_and_count_expert"))
        or (.name|contains("get_fused_mapping")) or (.name|contains("expand_to_fused")))]
  | group_by(.name) | map("\(length)\t\(.[0].name[0:44])") | .[]' prefill.json
```

**预期结果**（实测）：流分组命令输出——

```
stream 7: 3525 kernels, busy 1929558us
  void dpsk::gemm::fp8_gemm_kernel<4096u, 7168 ×116
  void flash::compute_attn_ws<Flash_fwd_kernel ×122
  void dpsk::gemm::fp8_gemm_kernel<7168u, 2048 ×116
stream 13: 6 kernels, busy 20236us
  ncclDevKernel_AllReduce_Sum_u64_TREE_LL(nccl ×3
  ncclDevKernel_AllReduce_Sum_u32_TREE_LL(nccl ×1
  ncclDevKernel_AllReduce_Sum_f32_TREE_LL(nccl ×2
stream 16: 580 kernels, busy 1799499us
  void dpsk::ep::internode::combine<false, 4,  ×116
  void dpsk::ep::internode::dispatch<false, 4, ×116
  void dpsk::ep::internode::notify_dispatch<fa ×116
```

（`top` 列取的是该流内总时长前三的内核。）**stream 16 就是 DeepEP 通信流**。路由计数命令输出：`top2_sum_gate ×116`、`clean_and_count_expert ×116`、`get_fused_mapping ×116`、`expand_to_fused_with_scales ×116`。

**等价 Python 脚本**（示例代码，供本地复现，结果应为同一张表；本讲无法在当前环境执行 Python，待本地验证）：

```python
import json
from collections import defaultdict

ev = json.load(open("prefill.json"))["traceEvents"]
by_stream = defaultdict(lambda: defaultdict(int))   # stream -> name -> count
busy = defaultdict(int)                             # stream -> total dur
for e in ev:
    if e.get("cat") == "kernel":
        s = e["args"]["stream"]
        by_stream[s][e["name"]] += 1
        busy[s] += e.get("dur", 0)

for s in sorted(by_stream):
    top = sorted(by_stream[s].items(), key=lambda kv: -kv[1])[:3]
    print(f"stream {s}: {sum(by_stream[s].values())} kernels, busy {busy[s]}us")
    for name, n in top:
        print(f"   {name[:60]} ×{n}")

ROUTING = ("top2_sum_gate", "clean_and_count_expert",
           "get_fused_mapping", "expand_to_fused")
for key in ROUTING:
    n = sum(c for name, c in by_stream[7].items() if key in name)
    print(f"{key}: {n}")
```

**进阶一问**：用你的流分工表回答——如果两个微批的注意力负载严重失衡（比如一个 5 ms、一个 1 ms），哪条流会出现气泡？为什么 README 说「同一条 prompt 可能被拆到两个微批」？

参考答案：计算流会出现气泡。通信时长由（模拟均衡的）路由与固定批量决定、近似恒定；某个微批注意力变长会推迟它「到达 dispatch」的时刻，使通信流等待，而另一个微批算完后没有可衔接的通信窗口，两条流同时空转。把 prompt 拆开分配正是为了让两侧注意力 token 数对齐——实测两微批均值 2814.2 / 2802.0 μs（差 0.4%）就是这种对齐的结果。

## 6. 本讲小结

- prefill.json 是 **EP32/TP1、4K prompt、16K tokens/GPU** 的推理预填充轨迹：85422 条事件、4111 个 GPU 内核，全部落在 GPU 0 的三条流上——stream 7 计算（3525 个）、stream 16 DeepEP 通信（580 个）、stream 13 NCCL 集合通信（6 个）。
- **双微批重叠是可量化的**：通信流忙时的 91.2%（1,641,355 / 1,799,499 μs）期间计算流同时在跑；580 个通信窗口 579 个有计算重叠；两个微批的注意力均值只差 0.4%，印证 README 的负载均衡设计。
- DeepEP 通信族（combine 1.01 s + dispatch 0.53 s + 两种 notify 0.24 s + layout 0.017 s）占内核总忙时 **48.0%**，是预填充第一大开销；combine 平均 8.7 ms 慢于 dispatch 的 4.6 ms。
- 计算侧图谱：DeepGEMM `fp8_gemm_kernel` 842 次（1.16 s，形状精确对上 V3 维数：2112=1536+512+64、24576=128×192、16384=128×128、4096=2×2048）、MLA 注意力 `compute_attn_ws` 122 次（0.34 s）、MoE 路由链 `top2_sum_gate → clean_and_count_expert → get_fused_mapping → expand_to_fused → dispatch → combine → reduce_fused` 每环 116 次。
- **次数就是结构指纹**：122 = 2×61（全层）、116 = 2×58（MoE 层）、6 = 2×3（dense 层），由此反推出 61 层 = 3 dense + 58 MoE、全程两个微批；热身期「8 个注意力后才第一个 dispatch」再次验证。
- 方法教训：给内核分类用 `args.stream` 而不是名称子串（`cdpsk::ep::CUB` 会污染 `dpsk::ep::` 匹配）；CPU 注解在推理轨迹里几乎不存在，分析必须基于 GPU 内核自身的执行区间。

## 7. 下一步学习建议

- 下一讲 **u2-l5（decode.json 低延迟 dispatch_ll/combine_ll）**：看同一套 all-to-all 在解码阶段如何换形态——通信内核改跑低延迟版本，RDMA 消息发出后 GPU SM 全部释放，与本讲「轻内核占着流」的 internode 实现形成对照。
- 之后 **u2-l6** 会解码侧的 `flash_fwd_splitkv_mla` 两段式注意力，与本讲的 `compute_attn_ws`（SingleTileScheduler、变长 traits）对照，理解「为什么预填充和解码需要两套注意力内核」。
- 想把本讲的手工查询沉淀成工具，进入 **u3-l1（通用分析脚本）**；想系统计算本讲 4.1.2 的重叠率/气泡指标，进入 **u3-l2**；三份轨迹横向对比在 **u3-l3**。
- 延伸阅读：内核名指向的三个开源仓库——[DeepEP](https://github.com/deepseek-ai/DeepEP)（`dpsk::ep::` 通信内核）、[DeepGEMM](https://github.com/deepseek-ai/DeepGEMM)（`dpsk::gemm::fp8_gemm_kernel` 与 `GemmType` 枚举，可解开本讲 4.2 的短/长变体之谜）、以及 FlashMLA/vLLM（`compute_attn_ws`、`rotary_embedding_with_kv_cache`）。
