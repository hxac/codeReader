# 基准测试 CLI 与 TFLOPS 评估

## 1. 本讲目标

读完前几讲，你已经知道 FFPA 怎么分发到四个后端、Triton/CuTeDSL kernel 长什么样、自动调优如何工作。本讲回答一个工程上最现实的问题：**这些 kernel 到底比 PyTorch 原生 SDPA 快多少？数值对不对？** 为此 FFPA 提供了一条命令 `python -m ffpa_attn.bench`，它能在任意一张支持的显卡上跑出一组标准用例，输出延迟、TFLOPS、相对 SDPA 的加速比，同时校验正确性。

学完本讲你应当掌握：

1. `python -m ffpa_attn.bench` 的入口链路、关键 CLI 参数与八类标准用例。
2. 前向 `4·B·Hq·D·pairs`、反向 `2.5x` 前向的 TFLOPS 理论公式，并能手算一个用例的 FLOPS。
3. 延迟测量的「warmup + iters」流程，以及前向/反向两条不同的计时方式。
4. 对 SDPA 的正确性校验：`torch.allclose` 的容差取值与 dtype/causal 的关系。

## 2. 前置知识

- **TFLOPS（每秒万亿次浮点运算）**：衡量算力吞吐的指标，等于「理论浮点运算次数 ÷ 实测耗时」。本讲的「理论运算次数」只统计注意力里两次主矩阵乘（`QKᵀ` 和 `PV`）的工作量，不含 softmax、reshape 等边缘开销，因此叫「dominant GEMM FLOPs」。这样得到的 TFLOPS 是一个**相对可比**的吞吐估计，而不是 kernel 的真实指令数。
- **SDPA**：`torch.nn.functional.scaled_dot_product_attention`，PyTorch 内置注意力，是 FFPA 的基线和回退目标（参见 u3-1）。
- **加速比（speedup）**：`speedup = SDPA 延迟 / FFPA 延迟`，>1 表示 FFPA 更快。
- **`torch.allclose`**：判断两个张量是否逐元素接近，由绝对容差 `atol` 和相对容差 `rtol` 控制。
- **CUDA event 计时**：用 `torch.cuda.Event` 在 GPU 时间轴上打点，比 CPU 端 `time.perf_counter` 更精确，因为能避开「CPU 发射 kernel」与「GPU 真正执行」之间的错位。

本讲依赖 u3-1（四后端总览），因为你需要知道 FFPA 至少有 Triton/CuTeDSL/CUDA/SDPA 几个后端，才能理解基准 CLI 里 `--fwd-backend`/`--bwd-backend` 的取值。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [src/ffpa_attn/bench.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/bench.py) | `python -m ffpa_attn.bench` 的模块入口，仅一行转发到 `_bench.main`。 |
| [src/ffpa_attn/cli/_bench.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cli/_bench.py) | 基准主程序：参数解析、用例调度、TFLOPS/speedup PNG 与 Markdown 表格生成。 |
| [src/ffpa_attn/cli/_flops.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cli/_flops.py) | 理论 FLOPS 公式与 TFLOPS/字符串格式化。 |
| [src/ffpa_attn/cli/_runner_fwd.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cli/_runner_fwd.py) | 前向基准：构造八类用例、跑 FFPA 与 SDPA、计时、算 TFLOPS、校验 allclose。 |
| [src/ffpa_attn/cli/_runner_bwd.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cli/_runner_bwd.py) | 反向基准：与前向对称，但计时只测 `backward()`，并比较 dQ/dK/dV 误差。 |
| [bench/README.md](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/bench/README.md) | 官方在 L20/H200/H20 上跑出的参考表格，用来核对你本地的结果是否合理。 |

调用关系一句话总结：`bench.py` → `_bench.main` → `_benchmark_rows` → `run_forward_examples` / `run_backward_examples` → 每个 case 在 `_run_case` 里调 `ffpa_attn_func` 与 SDPA，用 `_time_fn` 计时、`attention_fwd_flops`/`attention_bwd_flops` 算 FLOPS、`tflops_from_ms` 折算 TFLOPS。

## 4. 核心概念与源码讲解

### 4.1 bench CLI 入口与参数解析

#### 4.1.1 概念说明

`python -m ffpa_attn.bench` 是一个**端到端的基准套件**：它不是一个「跑一次 kernel 看看耗时」的临时脚本，而是一个会同时产出（a）一张 speedup 柱状图 PNG、（b）一张 TFLOPS 柱状图 PNG、（c）一份 README 风格 Markdown 表格的完整工具。它的设计目标是用同一套**标准用例**在不同后端（triton / cutedsl / cuda）和不同显卡上得到**可比**的数字。

理解入口链路的关键是分清三层：

1. **模块入口** `bench.py`：Python 的 `-m` 机制要求被运行的模块顶层有 `if __name__ == "__main__"`，它只做转发。
2. **主程序** `_bench.main`：编排「解析参数 → 校验后端/形状 → 跑用例收集结果行 → 画图 → 写 Markdown」整个流程。
3. **参数解析** `_parse_args`：把命令行的 `--fwd-backend`、`--D`、`--warmup`、`--iters` 等翻译成一个 `argparse.Namespace`。

#### 4.1.2 核心流程

`main` 的执行顺序可以用下面伪代码概括：

```
main():
    args = _parse_args()                       # 解析 CLI
    fallback = args.show_fallback              # 是否用硬编码假数据
    is_cutedsl = _resolve_cute_backends(args)  # cutedsl 前后向自动配对
    校验 head_dim 区间（默认 64..1024，cutedsl 受设备上限约束）
    解析 --tasks → 用例集合；cutedsl 再过滤掉不支持的用例
    解析 --dtype → 要测的 dtype 元组
    if fallback:
        rows = _build_fallback_rows(...)       # 硬编码 speedup
    else:
        rows = _benchmark_rows(args, ...)      # 真跑 kernel
    plot_speedup(...)      # 画 speedup 柱状图
    plot_tflops(...)       # 画 TFLOPS 柱状图
    markdown = render_speedup_markdown(...)
    写 PNG + Markdown 到 ./.tmp（或 --save-path）
```

`_benchmark_rows` 是「真跑」分支，它根据 `args.forward` / `args.backward` 分别调用前向、反向 runner。注意一个细节：`--tune` 只在前向/反向后端**恰好是 triton** 时才真正打开 Triton 在线 autotune（见 4.1.3 节的源码）。

#### 4.1.3 源码精读

模块入口仅转发，逻辑全在 `_bench.main`：

[src/ffpa_attn/bench.py:10-13](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/bench.py#L10-L13) —— `from .cli._bench import main`，再在 `if __name__ == "__main__"` 里调 `main()`。这是 `-m` 协议的最小骨架。

`main` 主体里两段最关键。第一段是后端配对与上限校验：

[src/ffpa_attn/cli/_bench.py:1445-1454](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cli/_bench.py#L1445-L1454) —— `_resolve_cute_backends` 让 `--fwd-backend cutedsl` 与 `--bwd-backend cutedsl` 自动配对（CuTeDSL 前后向必须对称，见 u6-1）；`_require_cute_device` 探测当前显卡是否被 CuTeDSL 支持，并返回该卡最大 head_dim，把非 cutedsl 路径的上限固定为 1024；`_validate_benchmark_head_dim` 强制 `64 ≤ D ≤ max` 且 `D % 64 == 0`。

第二段是「真跑 vs 假数据」分流：

[src/ffpa_attn/cli/_bench.py:1471-1478](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cli/_bench.py#L1471-L1478) —— `fallback` 模式用 `FALLBACK_SPEEDUPS` 这张硬编码表（无显卡时也能画图）；否则进 `_benchmark_rows` 真跑。

`_benchmark_rows` 里能看到 autotune 的精确触发条件，这是理解「为什么 `--tune` 有时不生效」的钥匙：

[src/ffpa_attn/cli/_bench.py:1396-1397](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cli/_bench.py#L1396-L1397) —— `triton_autotune=args.forward_backend == "triton" and tune_mode is not None`。也就是说 `--tune fast` 只有在 `--fwd-backend triton` 时才会把 `autotune=True` 传给 `TritonBackend`；如果前向是 cutedsl，`--tune` 被静默忽略。

参数解析看几个有代表性的开关。`--fwd-backend` 的取值受 choices 限制：

[src/ffpa_attn/cli/_bench.py:210-223](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cli/_bench.py#L210-L223) —— 前向后端可选 `cuda/triton/cutedsl`（默认 triton），反向可选 `sdpa/triton/cutedsl`（默认 triton）。注意反向没有 cuda，因为手写 CUDA 后端不实现反向（见 u3-1）。

`--tasks` 用 `_parse_tasks_arg` 解析成用例名集合，合法用例由 `VALID_TASKS` 约束：

[src/ffpa_attn/cli/_bench.py:378-399](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cli/_bench.py#L378-L399) —— 支持逗号或空白分隔（如 `self-attn,cross-attn`），未知用例名直接 `SystemExit`，`full/all/none` 表示全集。

#### 4.1.4 代码实践

1. **实践目标**：在不跑 kernel 的前提下熟悉 CLI 的全部参数与帮助文本。
2. **操作步骤**：
   ```bash
   python -m ffpa_attn.bench --help
   ```
3. **需要观察的现象**：帮助文本里列出 `--fwd-backend`、`--bwd-backend`、`--tune`、`--tasks`、`--B/--H/--N/--D`、`--warmup/--iters`、`--dtype`、`--show-fallback`、`--save-path` 等开关。
4. **预期结果**：你能从帮助文本里读出「默认前向后端是 triton、默认 D=512、默认 warmup=2/iters=10」。
5. 待本地验证（需要 CUDA 环境）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `--bwd-backend` 没有 `cuda` 选项？

**答案**：手写 CUDA 后端（`CUDABackend`）只实现前向，没有反向 kernel（见 u3-1 与 u7-1），因此反向只能在 `sdpa/triton/cutedsl` 三者里选。

**练习 2**：执行 `python -m ffpa_attn.bench --fwd-backend cutedsl --tune max` 时，`--tune max` 会生效吗？

**答案**：不会。因为 `_benchmark_rows` 里 `triton_autotune` 要求 `forward_backend == "triton"`，cutedsl 前向时 `--tune` 被忽略；autotune 是 Triton 后端专有的机制（见 u8-1）。

### 4.2 八类标准基准用例与形状

#### 4.2.1 概念说明

要让不同显卡、不同后端的数字可比，必须用**同一组固定形状**。FFPA 把注意力的典型使用场景浓缩成八类用例（case），每一类用 `(Nq, Nkv, Nh_q, Nh_kv, causal, dropout_p, attn_mask)` 的特定组合刻画。这八类覆盖了 FFPA 在真实大模型里会遇到的几乎全部形状：自注意力、交叉/解码注意力、分组注意力（GQA）、因果注意力、带偏置注意力、dropout 注意力、非对齐序列长度。

> 八类用例是**横切**的真实场景，不是 kernel 路径。同一条用例在不同后端（triton vs cutedsl）可能走完全不同的 kernel（generic vs split-KV vs SM90 专用），但这些差异由分发层处理，基准 runner 只负责「按形状调 `ffpa_attn_func`」。

#### 4.2.2 核心流程

用例集合在 `PLOT_CASES` 里固定排序，`VALID_TASKS` 就是这八个名字：

```
self-attn, cross-attn, decode-attn, gqa, causal, attn-mask, dropout, non-aligned
```

每个用例的 `(Nq, Nkv)` 由「基准序列长度 `N`（默认 8192）」派生：

| 用例 | Nq | Nkv | 说明 |
|---|---|---|---|
| self-attn | N | N | 标准方阵自注意力 |
| cross-attn | 1024 | N | 短 query、长 KV（交叉注意力） |
| decode-attn | 1 | N | 单 query 解码（走 split-KV 路径，见 u4-3） |
| gqa | N | N | `Nh_kv = Nh_q // 4` 的分组注意力（见 u2-4） |
| causal | N | N | `is_causal=True`，query 对齐 KV 尾部 |
| attn-mask | max(N,512) | max(N,512) | 带可加 `[1,1,1,Nkv]` key 位置偏置 |
| dropout | N | N | `dropout_p=0.1` |
| non-aligned | N-1 | N-1 | 序列长非 64 的倍数（如 8191），测尾部 tile |

其中 `gqa` 的 KV 头数由 `_resolve_gqa_heads(H)` 算出：取 `H//4` 再向下凑到能整除 `H` 的值。`non-aligned` 的头数也用同样方式收敛，因为非对齐用例会减小 head 数以控制显存。

注意：**CuTeDSL 后端不支持 `attn_mask` 和 `dropout`**，所以 `--fwd-backend cutedsl` 时这两类用例会被 `_filter_cutedsl_tasks` 直接剔除，且 decode-attn 也不在 CuTeDSL 的兼容集合里（`CUTEDSL_COMPAT_TASKS`）。

#### 4.2.3 源码精读

八类用例的权威排序定义在 `PLOT_CASES`：

[src/ffpa_attn/cli/_bench.py:58-67](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cli/_bench.py#L58-L67) —— 八个 `(case_name, 显示标签)` 元组；`VALID_TASKS`（[第 84 行](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cli/_bench.py#L84)）是 `--tasks` 合法取值的来源。

Markdown 表格里的 `Nq/Nkv` 列由 `_case_shape` 给出：

[src/ffpa_attn/cli/_bench.py:620-637](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cli/_bench.py#L620-L637) —— cross-attn 固定 `Nq=1024`、decode-attn 固定 `Nq=1`、attn-mask 用 `max(N,512)`、non-aligned 用 `N-1`，其余默认 `N,N`。

真正构造张量并跑 kernel 的用例规格在 runner 里。前向 runner 的 `case_specs`：

[src/ffpa_attn/cli/_runner_fwd.py:460-531](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cli/_runner_fwd.py#L460-L531) —— 列出每个用例的 `Nh_q/Nh_kv/Nq/Nkv` 与开关；GQA 用 `gqa_heads`、non-aligned 用 `non_aligned_heads` 与 `N-1`。注意 `attn-mask` 用 `_make_broadcast_additive_attn_mask` 造一个 `[1,1,1,Nkv]` 的紧凑 key 偏置（见 u2-3）。

反向 runner 的用例集合与之几乎对称：

[src/ffpa_attn/cli/_runner_bwd.py:953-1022](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cli/_runner_bwd.py#L953-L1022) —— 同样八类，但 cutedsl 后端（`mask_dropout_supported=False`）会跳过 attn-mask 和 dropout。

GQA 的 KV 头数计算：

[src/ffpa_attn/cli/_runner_fwd.py:127-138](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cli/_runner_fwd.py#L127-L138) —— `_resolve_gqa_heads`：取 `H//4`，若不整除就递减直到能整除，保证 `Nh_q % Nh_kv == 0`。

#### 4.2.4 代码实践

1. **实践目标**：核对用例形状与 `bench/README.md` 的 L20 表格一致。
2. **操作步骤**：打开 [bench/README.md:26-44](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/bench/README.md#L26-L44)（Forward Pass, Triton, L20, 8K, D=512），对照上表逐行核对 `Nq/Nkv` 列。
3. **需要观察的现象**：表格里 self-attn 是 `8192/8192`、cross-attn 是 `1024/8192`、decode-attn 是 `1/8192`、non-aligned 是 `8191/8191`，与本节形状表完全吻合。
4. **预期结果**：八行（每 dtype）的 `Nq/Nkv` 列都能用 `_case_shape(用例名, 8192)` 推导出来。
5. 待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：默认 `--H 32` 时，gqa 用例的 `Nh_kv` 是多少？

**答案**：`_resolve_gqa_heads(32)`：`candidate = 32//4 = 8`，`32 % 8 == 0`，所以 `Nh_kv = 8`，group_size = 4。

**练习 2**：为什么 non-aligned 用例要把序列长度设成 `N-1=8191`？

**答案**：FFPA 的 tile 宽度通常是 64 的倍数（`Bc=64`）。8191 不是 64 的倍数，会触发尾部 tile 的越界处理（cp.async 零填充 + softmax `-inf` 掩码 + 逐行 store 谓词），专门压测边界正确性，正如 `_runner_fwd.py` 模块 docstring 第 11-13 行所述。

### 4.3 TFLOPS 公式：从形状到理论算力

#### 4.3.1 概念说明

TFLOPS 的分母是**实测延迟**（下一节讲怎么测），分子是**理论 FLOPS**。FFPA 的理论 FLOPS 只统计注意力里两次主矩阵乘：

- **前向** = `QKᵀ`（一次）+ `PV`（一次）= 两次 GEMM。
- **反向** = 一次 `QKᵀ` 重算 + 四个大矩阵乘 = 五次 GEMM，恰好是前向的 `2.5x`。

一个 `[Nq, D] × [D, Nkv]` 矩阵乘的 FLOPS 是 `2·Nq·Nkv·D`（乘和加各算一次）。前向两次 GEMM 合起来就是 `4·Nq·Nkv·D`，再乘 batch 和 head 数得到 `4·B·Hq·D·pairs`，其中 `pairs` 是有效的 query/key 对数。这就是本讲标题里「前向 `4*bnh*D*pairs`」的来源。

为什么强调「dominant GEMM only」？因为真实 kernel 还要做 softmax、rescale、reshape、dropout 等大量非矩阵乘指令，但这些比起两次主 GEMM 是小头。只算主 GEMM 能让不同实现（FFPA vs SDPA、Triton vs CuTeDSL）在**同一把尺子**下比较吞吐，这也是 flash-attention 官方 benchmark 采用的约定（`_flops.py` 顶部注明了引用来源）。

#### 4.3.2 核心流程

前向 FLOPS：

\[
\text{FLOPS}_{\text{fwd}} = 4 \cdot B \cdot H_q \cdot D \cdot \text{valid\_pairs}(N_q, N_{kv}, \text{causal})
\]

其中 `valid_pairs` 是真正参与运算的 query/key 对数。非因果时就是 `Nq·Nkv`；因果时因 FFPA 的「query 对齐 KV 尾部」约定（见 u2-3），第 `i` 行只看列 `col ≤ i + (Nkv − Nq)`：

\[
\text{valid\_pairs}_{\text{causal}} = \sum_{i=0}^{N_q-1} \max\bigl(0,\ \min(N_{kv},\ i + (N_{kv}-N_q) + 1)\bigr)
\]

反向 FLOPS：

\[
\text{FLOPS}_{\text{bwd}} = \frac{5}{2} \cdot \text{FLOPS}_{\text{fwd}} = \frac{5 \cdot \text{FLOPS}_{\text{fwd}}}{2}
\]

TFLOPS 折算（延迟单位 ms）：

\[
\text{TFLOPS} = \frac{\text{FLOPS}}{\text{latency\_ms} \times 10^{9}}
\]

（因为 `1 TFLOPS = 10^12 FLOP/s`，而 `latency_ms × 10^{-3}` 是秒，所以 `FLOPS / (latency_ms × 10^{-3}) = FLOPS × 10^3 / latency_ms` 是 GFLOPS……注意这里的常数。实际代码写的是 `flops / (latency_ms * 1.0e9)`：把 `latency_ms × 1e9` 当成「以 10^{-3}·ns… 」并不直观，下面源码精读里会验证它确实得到 TFLOPS 量级。）

#### 4.3.3 源码精读

非因果/因果的有效对数：

[src/ffpa_attn/cli/_flops.py:15-34](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cli/_flops.py#L15-L34) —— `attention_valid_pairs`：非因果返回 `nq*nkv`；因果逐行累加 `max(0, min(nkv, row_idx + kv_offset + 1))`，`kv_offset = nkv - nq`，精确刻画尾部对齐因果。

前向 FLOPS：

[src/ffpa_attn/cli/_flops.py:37-53](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cli/_flops.py#L37-L53) —— `attention_fwd_flops = 4 * batch * num_heads_q * headdim * valid_pairs`，正是本节公式。

反向 FLOPS：

[src/ffpa_attn/cli/_flops.py:56-75](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cli/_flops.py#L56-L75) —— `attention_bwd_flops = 5 * attention_fwd_flops(...) // 2`，即 `2.5x` 前向；用整数 `//` 避免 float。

TFLOPS 折算：

[src/ffpa_attn/cli/_flops.py:78-87](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cli/_flops.py#L78-L87) —— `tflops_from_ms`：`flops / (latency_ms * 1.0e9)`。验证量级：FLOPS 单位是「次」，`latency_ms * 1e-3` 是秒，`flops / (秒) = FLOPS/s`；代码写成 `flops / (latency_ms * 1e9)`，等价于 `flops / (latency_s * 1e12)`，即「以 10^12 FLOPS（=1 TFLOPS）为单位」的数值——所以返回的就是 TFLOPS。`latency_ms` 非正或非有限时返回 `None`。

紧凑格式化（表格里 `97T`/`0.69T`）：

[src/ffpa_attn/cli/_flops.py:90-102](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cli/_flops.py#L90-L102) —— `format_tflops_short`：≥10 显示整数 `90T`，≥1 显示一位小数 `1.8T`，否则两位小数 `0.69T`，`None` 显示 `-`。

runner 里把 FLOPS 喂给 `tflops_from_ms` 得到 FFPA 与 SDPA 各自的 TFLOPS：

[src/ffpa_attn/cli/_runner_fwd.py:346-371](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cli/_runner_fwd.py#L346-L371) —— `flop_count = attention_fwd_flops(B, Nh_q, Nq, Nkv, D, causal)`，再分别用 FFPA 延迟与 SDPA 延迟折算 `ffpa_tflops` / `sdpa_tflops`，`speedup = ms_sdpa / ms_ffpa`。反向在 [_runner_bwd.py:804-848](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cli/_runner_bwd.py#L804-L848) 用 `attention_bwd_flops` 做同样的事。

#### 4.3.4 代码实践

1. **实践目标**：手算 self-attn 在 L20、8K、D=512 下的前向理论 FLOPS，并验证它能还原表格里的 `97T`。
2. **操作步骤**（纸上计算，不需要 GPU）：
   - 形状：`B=1, Hq=32, Nq=8192, Nkv=8192, D=512, causal=False`。
   - `valid_pairs = 8192 × 8192 = 2^26 = 67,108,864`。
   - `FLOPS_fwd = 4 × 1 × 32 × 512 × 67,108,864`。
     - 用 2 的幂：`4=2², 32×512=2^5·2^9=2^14, 8192²=2^26`，合计 `2²·2^14·2^26 = 2^42 = 4,398,046,511,104`（约 `4.398×10^12`）。
   - 查 [bench/README.md:28](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/bench/README.md#L28)：self-attn fp16 前向 `45.40 ms / 97T`。
   - `TFLOPS = 4.398×10^12 / (45.40 × 1e9) ≈ 96.9`，与 `97T` 吻合。
3. **需要观察的现象**：手算 TFLOPS 与表格 FFPA 列几乎相等（误差来自表格四舍五入）。
4. **预期结果**：用同一 FLOPS 除以 SDPA 延迟 `74.76 ms` 得 `4.398e12 / 7.476e10 ≈ 58.8T`，也与表格 SDPA 列 `59T` 吻合。
5. 待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：同一个 self-attn 8K/D=512 用例，反向理论 FLOPS 是多少？对照表格 bwd self-attn fp16 的 `191.89 ms / 57T` 验证。

**答案**：`FLOPS_bwd = 5/2 × FLOPS_fwd = 5 × 2^42 / 2 = 5 × 2^41 ≈ 1.0995×10^13`。`TFLOPS = 1.0995e13 / (191.89 × 1e9) ≈ 57.3`，与表格 `57T` 吻合。

**练习 2**：为什么 decode-attn（Nq=1）的 TFLOPS 只有 `0.69T` 这么低？

**答案**：decode 的 query 只有 1 行，`valid_pairs = 1 × 8192 = 8192`，`FLOPS = 4×1×32×512×8192 ≈ 5.37×10^8`，分子极小；而延迟受限于访存带宽而非算力，所以折算出的「算力吞吐」很低。这正是 decode 走 split-KV 路径的原因——它本质是访存密集型，不是算力密集型（见 u4-3）。

### 4.4 延迟测量与对 SDPA 的正确性校验

#### 4.4.1 概念说明

**延迟测量**必须解决两个 GPU 计时的经典坑：

1. **首次启动开销**：第一次跑 kernel 会触发驱动初始化、context 建立等一次性开销，不能计入。所以要先跑若干次 **warmup**。
2. **异步执行**：CPU 发射 kernel 后立刻返回，GPU 还在算。必须 `torch.cuda.synchronize()` 强制等 GPU 跑完再读时间。

前向和反向的计时方式不同：

- **前向**：用 `time.perf_counter`（CPU 端高精度计时器）包裹「跑 iters 次前向」，每次之间 `synchronize`。因为前向只测一次 `ffpa_attn_func` 调用。
- **反向**：默认 `timing_mode="backward-only"`，用 **CUDA event** 只包裹 `out.backward(grad_out)`，**不把前向算进反向延迟**。因为反向基准想隔离「反向 kernel 本身」的耗时，避免前向污染。

**正确性校验**用 `torch.allclose` 把 FFPA 输出与 SDPA 输出逐元素比，容差随 dtype 和方向变化：低精度（bf16）容差大，因果反向容差最大。

#### 4.4.2 核心流程

前向 `_time_fn` 流程：

```
_time_fn(fn, *args, warmup, iters, rng_seed):
    for _ in range(warmup):         # 预热
        if rng_seed is not None: torch.manual_seed(rng_seed)   # dropout 复现
        fn(*args)
    synchronize()                   # 等 warmup 跑完
    t0 = perf_counter()
    for _ in range(iters):          # 正式测 iters 次
        if rng_seed is not None: torch.manual_seed(rng_seed)
        fn(*args)
    synchronize()                   # 等最后一次跑完
    return (perf_counter() - t0) * 1000.0 / iters   # 平均每次的毫秒
```

反向 `_time_backward_only` 流程（关键差异：CUDA event 只套住 backward）：

```
_time_backward_only(fn, q, k, v, grad_out, warmup, iters, rng_seed):
    for _ in range(warmup):
        q_i,k_i,v_i = clone+requires_grad          # 每次都要新叶子
        out = fn(q_i,k_i,v_i)                      # 前向（不计入）
        synchronize()
        out.backward(grad_out)                     # 反向（warmup）
    synchronize()
    elapsed = 0
    for _ in range(iters):
        q_i,k_i,v_i = clone+requires_grad
        out = fn(q_i,k_i,v_i)
        synchronize()
        start.record()                              # ← event 起点
        out.backward(grad_out)
        end.record()                                # ← event 终点
        synchronize()
        elapsed += start.elapsed_time(end)          # 仅反向耗时
    return elapsed / iters
```

每次迭代重新 `clone().requires_grad_(True)` 是为了让 `out.backward()` 能从干净梯度图跑起——同一组叶子张量连续 `backward` 会累加梯度，破坏测量。

#### 4.4.3 源码精读

前向计时器：

[src/ffpa_attn/cli/_runner_fwd.py:83-102](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cli/_runner_fwd.py#L83-L102) —— `_time_fn`：`warmup` 次预热 + `synchronize`，再 `iters` 次正式测，`rng_seed` 在 dropout 用例里每次重置以保证 FFPA 与 SDPA 用同一 dropout 掩码（见 u4-4 的 Philox 重放）。返回「平均每次毫秒」。

反向计时器（只测 backward）：

[src/ffpa_attn/cli/_runner_bwd.py:322-362](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cli/_runner_bwd.py#L322-L362) —— `_time_backward_only`：用 `torch.cuda.Event(enable_timing=True)`，`start.record()` 在 `backward` 前、`end.record()` 在 `backward` 后，`start.elapsed_time(end)` 取 GPU 时间轴上的真实毫秒。注意反向 runner 也有一个 `_time_fn`（[第 300-319 行](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cli/_runner_bwd.py#L300-L319)），仅在 `timing_mode != "backward-only"` 时用于「前向+反向一起测」的旧模式。

计时参数校验：

[src/ffpa_attn/cli/_runner_fwd.py:37-47](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cli/_runner_fwd.py#L37-L47) —— `_validate_timing_args`：`warmup` 非负、`iters` 正数，否则 `ValueError`。

前向正确性校验的容差与比较：

[src/ffpa_attn/cli/_runner_fwd.py:313-314](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cli/_runner_fwd.py#L313-L314) —— `tol = 5e-2 if dtype == torch.bfloat16 else 2e-2`；`_tensor_allclose` 把两边提升到 fp32 再 `torch.allclose(atol=tol, rtol=tol)`（[第 197-205 行](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cli/_runner_fwd.py#L197-L205)）。

反向容差更宽，且因果 bf16 单独放宽：

[src/ffpa_attn/cli/_runner_bwd.py:806-819](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cli/_runner_bwd.py#L806-L819) —— `tol = 7.5e-2 if (bf16 and causal) else 5e-2 if bf16 else 2e-2`；反向要 dQ/dK/dV 三个都 allclose（有 mask 时还要求 dMask allclose）才算通过。

SDPA 参考实现里有 dropout 时强制走 efficient attention 后端：

[src/ffpa_attn/cli/_runner_fwd.py:50-71](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cli/_runner_fwd.py#L50-L71) —— `_sdpa_ref`：`dropout_p > 0` 时用 `with sdpa_kernel(SDPBackend.EFFICIENT_ATTENTION)` 锁定后端，注释解释默认 dispatcher 在 D≤256 会选 flash-attention，用不同 RNG 流，破坏 dropout 的逐位对比。这是把 FFPA 与 SDPA 放在「同一 dropout 语义」下的必要操作。

#### 4.4.4 代码实践

1. **实践目标**：用最小的 warmup/iters 跑一次前向基准，观察计时与 allclose 输出。
2. **操作步骤**：
   ```bash
   CUDA_VISIBLE_DEVICES=0 python -m ffpa_attn.bench \
       --no-bwd --fwd-backend triton \
       --tasks self-attn --dtype bf16 \
       --warmup 2 --iters 10 --D 512 --N 8192 --show-allclose
   ```
3. **需要观察的现象**：终端逐行打印每个 case 的一行摘要，含 `max|diff|`、`mean|diff|`、`allclose(atol=0.05)`、`FFPA=xx ms  SDPA=xx ms`、`TFLOPS=xxT/xxT`、`speedup=x.xx`。同时在 `./.tmp/` 下生成 PNG 与 Markdown。
4. **预期结果**：`allclose` 列出现 `✅`；speedup 与 [bench/README.md:29](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/bench/README.md#L29) 的 self-attn bf16（`1.66x`）量级一致（不同显卡数值会不同，但应 >1）。
5. 待本地验证（需要一张 SM≥75 的 NVIDIA GPU，Triton-only 安装即可）。

#### 4.4.5 小练习与答案

**练习 1**：为什么反向计时要在每次迭代重新 `clone().requires_grad_(True)`，而不是复用同一组张量？

**答案**：`backward()` 会把梯度累加到叶子张量的 `.grad` 上。如果复用同一组叶子，第二次 `backward` 的梯度会叠在第一次之上，测到的就不再是单次反向的耗时与正确结果。每次新建叶子保证梯度图从零开始。

**练习 2**：前向用 `time.perf_counter`、反向却用 CUDA event，为什么不对称？

**答案**：前向只测一次完整的 `ffpa_attn_func`，CPU 端 `perf_counter` 包裹 `iters` 次（含 `synchronize`）足以；反向想**隔离** `backward()` 本身、排除前向，就必须在 GPU 时间轴上用 event 精确卡住「backward 起点→终点」这一段，CPU 端计时做不到这种局部隔离。

## 5. 综合实践

把本讲四条主线串起来：跑一次真实前向基准，核对 TFLOPS 与正确性。

**任务**：在本地 GPU 上执行

```bash
CUDA_VISIBLE_DEVICES=0 python -m ffpa_attn.bench --no-bwd --fwd-backend triton --D 512
```

然后完成下面三件事：

1. **读表**：打开生成的 `.tmp/ffpa_speedup_<device>_B1_H32_N8192_D512.md`，对照 [bench/README.md:26-44](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/bench/README.md#L26-L44) 的 L20 前向表。确认列含义：`FFPA / SDPA` 是延迟（ms）、`TFLOPS` 是 `FFPA / SDPA` 两栏、`speedup = SDPA延迟 / FFPA延迟`。

2. **手算验证**：挑 `self-attn fp16` 一行，用 4.3.4 的公式手算前向 FLOPS（应得 `2^42 ≈ 4.398×10^12`），再除以你本地测到的 FFPA 延迟，核对等于表格 FFPA 列的 TFLOPS。

3. **正确性排查**：加 `--show-allclose` 重跑，观察 `allclose` 列。若某行是 `❌`，结合 4.4.3 的容差表判断：是 dtype 导致（bf16 用 5e-2）还是 causal 反向导致（bf16+causal 用 7.5e-2），再决定是否需要调容差或排查 kernel。

**验收标准**：

- speedup 全部 >1（除非 decode-attn 在某些卡上接近 1，属正常）。
- 手算 TFLOPS 与表格误差 <5%。
- `allclose` 在合理容差内为 `✅`。

如果本地没有支持的 GPU，可用 `python -m ffpa_attn.bench --show-fallback` 走 `FALLBACK_SPEEDUPS` 硬编码路径，至少能验证 PNG/Markdown 生成链路（但 TFLOPS 与 allclose 列会是 `-`，因为假数据没有延迟）。

## 6. 本讲小结

- `python -m ffpa_attn.bench` 经 `bench.py` → `_bench.main` → `_benchmark_rows` → 前向/反向 runner 的链路，把八类标准用例在同一张卡上跑出延迟、TFLOPS、speedup 与正确性。
- 八类用例（self/cross/decode/gqa/causal/attn-mask/dropout/non-aligned）由基准序列长度 `N` 派生固定形状，CuTeDSL 后端会剔除不支持的 attn-mask/dropout/decode。
- 前向 FLOPS = `4·B·Hq·D·valid_pairs`，反向 = `2.5×`前向；只统计主 GEMM 以保证跨实现可比，TFLOPS = `FLOPS / (latency_ms × 1e9)`。
- 计时用 warmup + iters + `synchronize`；前向用 `perf_counter`，反向默认用 CUDA event 只测 `backward()`。
- 正确性用 `torch.allclose` 对比 SDPA，容差 fp16=2e-2、bf16=5e-2、bf16+causal 反向=7.5e-2，dropout 时强制 SDPA 走 efficient attention 后端以对齐 RNG。
- `--tune` 只在前向/反向后端为 triton 时才生效，`FALLBACK_SPEEDUPS` 让无 GPU 环境也能出图。

## 7. 下一步学习建议

- **回到自动调优**：本讲的 `--tune fast/max` 只是触发开关，背后的候选 config 生成、缓存 key、持久化机制在 u8-1/u8-2/u8-3。建议读完这几讲后，用 `--tune max` 重跑基准，对比开启 autotune 前后的 speedup 变化。
- **多卡基准**：u8-4 讲了 Ray 多 GPU 并行调优；如果你想在一台多卡机上为每张卡生成持久化 config，那条链路是基准之后的自然延伸。
- **测试体系**：本讲的 allclose 是「基准里的快速校验」，更系统的正确性矩阵在 u9-1（`tests/test_ffpa_fwd.py` / `test_ffpa_bwd.py`），建议对照阅读，理解 `CORRECTNESS_SHAPES` 如何比八类用例覆盖得更细。
- **源码延伸**：若想自定义基准用例，可直接仿照 `_runner_fwd.py` 的 `case_specs` 增加一条，并把它加进 `_bench.PLOT_CASES` 与 `VALID_TASKS`，就能让自己的用例出现在表格与柱状图里。
