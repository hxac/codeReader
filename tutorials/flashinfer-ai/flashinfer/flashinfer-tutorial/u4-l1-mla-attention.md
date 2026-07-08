# MLA 多潜注意力（DeepSeek）

## 1. 本讲目标

学完本讲，你应该能够：

- 说出 **Multi-Latent Attention (MLA)** 相比普通多头注意力 (MHA) 解决了什么问题，以及它用什么「低秩潜向量压缩」手段省下显存。
- 区分 DeepSeek MLA 里四个核心张量：`q_nope`、`q_pe`、`ckv`、`kpe`，并说清它们的形状与共享方式。
- 使用 `BatchMLAPagedAttentionWrapper` 完成 `plan()`/`run()` 两段式调用，并理解新一代 `trtllm_batch_decode_with_kv_cache_mla` 的定位。
- 解释 MLA 在不同 SM 架构上是如何「后端派发」的（fa2/fa3/cutlass/xqa/trtllm-gen/cute-dsl/sparse）。

本讲是「进阶注意力变体」单元的首篇，前置是 [u3-l3](u3-l3-batch-decode-wrapper.md)（decode 的 plan/run）与 [u3-l5](u3-l5-backend-selection.md)（注意力后端选择）。MLA 在数据布局与后端复杂度上都比普通 decode 高一个量级，但底层仍是 plan/run + paged KV + split-k 那套机制，所以本讲会反复承接前面建立的概念。

## 2. 前置知识

在进入 MLA 之前，先用三段话回顾必要背景。

**多头注意力 (MHA) 的 KV 显存代价。** 标准注意力里每个 token、每个头都要存一对向量 \(K\) 和 \(V\)。一个批次的 KV cache 占用约为

\[
\text{KV 显存} \approx \text{batch} \times \text{seq\_len} \times \text{num\_heads} \times \text{head\_dim} \times 2 \times \text{sizeof}(T)
\]

当 `num_heads` 很大（DeepSeek 用 128 个头）、序列很长时，这一块显存会主导整个推理服务。**MLA 的核心动机就是削减这一项。**

**低秩分解。** 如果一个大矩阵可以近似为「瘦矩阵 × 瘦矩阵」，就能用更少的参数表示它。MLA 把跨头共享的 K/V 信息压成一条低维「潜向量」（latent），所有头共享它，从而把 `num_heads` 这个因子从 KV 显存里消掉。

**RoPE 与位置编码。** 旋转位置编码 (RoPE) 必须作用在「真实的 Q/K 上」才有效，因此 MLA 里位置相关的部分不能被低秩压缩吸收掉，要单独留一条「带位置编码的 K」（即 `kpe`）。这是为什么 MLA 的 K 被拆成「不带位置的潜部分 ckv」和「带位置的 kpe」两条。

**Matrix Absorption（矩阵吸收）技巧。** DeepSeek 在 decode 阶段把上投影权重 \(W_{UK}\)、\(W_{UV}\) 分别「吸收」进 \(W_{UQ}\)、\(W_{O}\)，使得 query 可以直接投影到潜空间、注意力可以直接在压缩后的潜空间里算，**不需要在每一步把潜向量解压成多头 K/V**。这是 MLA decode 高效的关键。本讲的 `q_nope` 形状是 `head_dim_ckv=512`（潜维度），正是吸收后的结果。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [flashinfer/mla/_core.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/mla/_core.py) | MLA 全部 Python API：`MLAHeadDimensions`、`BatchMLAPagedAttentionWrapper`、`trtllm_batch_decode_with_kv_cache_mla`、`xqa_batch_decode_with_kv_cache_mla` |
| [flashinfer/utils.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/utils.py) | `determine_mla_backend` 等硬件能力查询 |
| [include/flashinfer/attention/mla.cuh](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/attention/mla.cuh) | MLA 的核心 CUDA kernel 模板（fa2 路径），含 `BatchMLAPagedAttentionKernel` |
| [include/flashinfer/attention/scheduler.cuh](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/attention/scheduler.cuh) | `MLAPlanInfo` 结构体与 `MLAPlan` 调度函数（split-k 划分） |
| [csrc/batch_mla_plan.cu](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/csrc/batch_mla_plan.cu) | `plan` 的 C++ 入口（TVM-FFI 绑定），调用 `MLAPlan` |
| [csrc/batch_mla_run.cu](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/csrc/batch_mla_run.cu) | `run` 的 C++ 入口，装配 `Params` 并启动 kernel |
| [flashinfer/jit/mla.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/mla.py) / [flashinfer/jit/attention/modules.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/attention/modules.py) | MLA 的 JIT 代码生成（fa2/fa3/cutlass 三类模块） |

> 提示：MLA 的代码量很大，`_core.py` 单文件就超过 3300 行（里面除了本讲讲的 dense MLA，还包含 DeepSeek-V4 的稀疏 MLA、SM120 的 packed sparse 等进阶路径）。本讲聚焦「dense decode MLA」这一主干，其余作为后续学习的入口。

## 4. 核心概念与源码讲解

### 4.1 MLA 原理：低秩潜向量压缩与四张量模型

#### 4.1.1 概念说明

MLA（Multi-head **Latent** Attention）出自 DeepSeek-V2，核心思想是：**不为每个头单独缓存 K/V，而是缓存一条跨头共享的低维潜向量，解码时再按需投影。**

具体到 DeepSeek-V3 的尺寸（见 `deepseek_mla_dimensions`）：

- 潜向量维度 `kv_lora_rank = 512`：这就是压缩后的「内容 K/V」，**所有 128 个头共享同一条**。
- 位置编码维度 `qk_rope_head_dim = 64`：因为 RoPE 不能被吸收，单独留一条带位置的 K（`kpe`），同样**跨头共享**。
- 于是每个 token 的 KV cache 只需要 \(512 + 64 = 576\) 个元素，**与 `num_heads` 无关**。

对比一下显存：普通 MHA（128 头、head_dim=128）每个 token 的 KV 是 \(128 \times 128 \times 2 = 32768\) 个元素；MLA 只要 576 个，约为前者的 **1/57**。这就是 MLA 在长上下文推理服务里几乎必选的原因。

在 FlashInfer 的 API 里，这 576 维被拆成两组独立的张量（而不是拼成一条），原因是矩阵吸收后的计算需要把「内容」与「位置」分开处理：

| 张量 | 形状 | 含义 |
|------|------|------|
| `q_nope` | `[batch, num_heads, 512]` | query 的「不带位置」部分，已被吸收进潜空间，维度=kv_lora_rank |
| `q_pe` | `[batch, num_heads, 64]` | query 的「带位置」(RoPE) 部分 |
| `ckv_cache` | `[num_pages, page_size, 512]` | 跨头共享的压缩 K/V（paged） |
| `kpe_cache` | `[num_pages, page_size, 64]` | 跨头共享的带位置 K（paged） |

输出 `o` 的形状与 `q_nope` 相同：`[batch, num_heads, 512]`（即 `kv_lora_rank`），因为 V 就是 `ckv` 本身。

#### 4.1.2 核心流程

MLA decode 单步注意力的计算可写成（对一个 query 头 \(h\)，token \(i\)）：

\[
s_{i} = \underbrace{q\_pe_h \cdot kpe_i}_{\text{位置项}} \;+\; \underbrace{q\_nope_h \cdot ckv_i}_{\text{内容项}}
\]

\[
\alpha = \mathrm{softmax}(s / \sqrt{d_{qk}}), \qquad o_h = \sum_i \alpha_i \cdot ckv_i
\]

关键观察：

1. **K 的两部分相加**：注意分数同时来自 `q_pe·kpe`（64 维，带位置）和 `q_nope·ckv`（512 维，不带位置），两者累加进同一个分数缓冲 `s_frag`。
2. **V 就是 ckv**：输出直接是 `α · ckv`，所以输出维度 = 512。`kpe` 只参与「打分」，不参与「加权求和」。
3. **softmax 缩放**用的是吸收**前**的原始头维度：\(d_{qk} = \text{qk\_nope\_head\_dim} + \text{qk\_rope\_head\_dim} = 128 + 64 = 192\)，即 `sm_scale = 1/sqrt(192)`。

把 MLA 与普通 MHA decode 放在一起对比：

| 维度 | 普通 MHA decode | MLA decode |
|------|----------------|------------|
| 每 token KV 元素数 | `num_heads × head_dim × 2` | `512 + 64 = 576`（与头数无关） |
| K 是否带位置 | 整条都带 RoPE | 拆成 ckv(不带位置) + kpe(带位置) |
| V 的来源 | 单独的 V 缓冲 | 复用 ckv |
| 输出头维度 | `head_dim` (如 128) | `kv_lora_rank` (512) |
| softmax 缩放维度 | `head_dim` | 吸收前的 `qk_nope_head_dim + qk_rope_head_dim` |

#### 4.1.3 源码精读

**尺寸约定在哪里定义。** `_core.py` 用一个 frozen dataclass 描述一个 MLA 头的维度：

[flashinfer/mla/_core.py:78-110](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/mla/_core.py#L78-L110) — `MLAHeadDimensions` 与 `deepseek_mla_dimensions`。注意 `kv_lora_rank=512` 是潜维度，`qk_nope_head_dim=128` 是吸收前的原始 nope 头维度（仅供非吸收模式与 softmax 缩放用）。

**kernel 模板如何承载这两个维度。** 在 CUDA kernel 里，`KernelTraits` 用 `HEAD_DIM_CKV`/`HEAD_DIM_KPE` 两个编译期常量区分「内容」与「位置」两路，并把共享内存也拆成对应的两片：

[include/flashinfer/attention/mla.cuh:71-116](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/attention/mla.cuh#L71-L116) — `KernelTraits`，关键常量 `HEAD_DIM_ALL = HEAD_DIM_CKV + HEAD_DIM_KPE`（=576），`NUM_MMA_D_CKV`/`NUM_MMA_D_KPE` 分别是两路的 MMA 指令条数。

[include/flashinfer/attention/mla.cuh:51-69](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/attention/mla.cuh#L51-L69) — `SharedStorageQKVO`，共享内存里 Q/K/V 都按 nope 与 pe 两路分别布置（`q_smem_nope`/`q_smem_pe`、`ckv_smem`、`kpe_p_smem`），这是上面「K 两部分相加」在显存布局上的体现。

**QK 的两路累加。** `compute_mla_qk` 先算位置路 `q_pe·kpe`（`init=true` 初始化 `s_frag`），再算内容路 `q_nope·ckv`（`init=false` 累加进去）：

[include/flashinfer/attention/mla.cuh:478-496](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/attention/mla.cuh#L478-L496) — `compute_mla_qk`，对应公式里 \(s_i = q\_pe\cdot kpe + q\_nope\cdot ckv\) 的两步。

**PV 用 ckv 当 V。** `compute_mla_pv` 里读取的 V 片段来自 `ckv_smem`（即 `ldmatrix_m8n8x4_trans(ckv_smem_offset_r, v_frag)`），输出 `o_frag` 维度是 `NUM_MMA_D_CKV`，印证「V=ckv、输出=512」：

[include/flashinfer/attention/mla.cuh:498-589](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/attention/mla.cuh#L498-L589) — `compute_mla_pv`。

**softmax 缩放。** 缩放系数 `sm_scale_log2 = sm_scale * log2e` 在 variant 构造时算出（[mla.cuh:33-49](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/attention/mla.cuh#L33-L49)），而 Python 侧传进来的 `sm_scale = 1/sqrt(192)`（见 4.2.3 的 docstring 注释），用的是吸收前的 192。

#### 4.1.4 代码实践

**实践目标：** 用一张纸 + 一段数值核对，亲手验证「MLA 每 token KV 元素数与 num_heads 无关」这个结论，避免只停留在抽象描述。

**操作步骤：**

1. 取 DeepSeek 尺寸：`num_heads=128, head_dim=128, kv_lora_rank=512, qk_rope_head_dim=64`。
2. 计算「普通 MHA 每 token KV 元素数」与「MLA 每 token KV 元素数」。
3. 假设 `batch=64, seq_len=4096, dtype=bfloat16`，算两种方案的 KV cache 字节数。
4. 用一段最小 Python（纯 CPU，不触发任何 CUDA）把上面的算式跑出来：

```python
# 示例代码：纯数值对比，不需要 GPU
def mha_kv_elements(num_heads, head_dim):
    return num_heads * head_dim * 2  # K 和 V

def mla_kv_elements(kv_lora_rank, qk_rope_head_dim):
    return kv_lora_rank + qk_rope_head_dim  # ckv + kpe，跨头共享

nh, hd, lora, rope = 128, 128, 512, 64
print("MHA per-token KV elems:", mha_kv_elements(nh, hd))   # 期望 32768
print("MLA per-token KV elems:", mla_kv_elements(lora, rope)) # 期望 576

batch, seq = 64, 4096
bytes_mha = batch * seq * mha_kv_elements(nh, hd) * 2   # bf16=2字节
bytes_mla = batch * seq * mla_kv_elements(lora, rope) * 2
print(f"MHA KV cache: {bytes_mha/1024**3:.2f} GB")
print(f"MLA KV cache: {bytes_mla/1024**3:.2f} GB")
```

**需要观察的现象：** MLA 的每 token KV 元素数应固定为 576；改变 `nh` 不影响它，而 MHA 的值随 `nh` 线性增长。

**预期结果：** MHA 约 64 GB，MLA 约 1.13 GB（按上面参数）。比例约 57:1。若你的数值不符，检查是否漏乘了「K 和 V 各一份」或把 `num_heads` 误代入 MLA。

> 此实践可在任何机器上运行，不需要 GPU 或安装 flashinfer。

#### 4.1.5 小练习与答案

**练习 1.** 如果把 `num_heads` 从 128 翻倍到 256，MLA 的 KV cache 显存会变多少？普通 MHA 呢？

> 答案：MLA 不变（KV 跨头共享，与 `num_heads` 无关）；普通 MHA 的 KV cache 翻倍（线性依赖 `num_heads`）。

**练习 2.** 为什么 MLA 把 K 拆成 `ckv` 和 `kpe` 两段，而不是压成一条潜向量？

> 答案：RoPE 必须作用在真实 Q/K 上才能表达相对位置；低秩潜空间里没法直接施加 RoPE，所以位置相关的那一段（`kpe`）必须独立保留、不被压缩。

**练习 3.** 输出 `o` 的最后一维为什么是 512 而不是 128？

> 答案：因为 V 就是 `ckv` 本身（维度 `kv_lora_rank=512`），输出 \(o=\sum\alpha\cdot ckv\) 自然是 512 维；后续再由吸收进 \(W_O\) 的投影把 512 映射回模型隐藏维。

---

### 4.2 MLA Wrapper：plan/run 两段式 API

#### 4.2.1 概念说明

FlashInfer 为 MLA 提供了两代面向用户的 API：

1. **`BatchMLAPagedAttentionWrapper`**（经典、跨架构）：Hopper(Ampere 亦可)/Blackwell 通用，后端在 `fa2`/`fa3`/`cutlass` 间选择，输入是**分开的四个张量** `q_nope/q_pe/ckv/kpe`，遵循 [u3-l3](u3-l3-batch-decode-wrapper.md) 讲过的 plan/run 两段式。
2. **`trtllm_batch_decode_with_kv_cache_mla`**（新一代、Blackwell 原生）：面向 SM100/SM103/SM120/SM121，后端在 `trtllm-gen`/`cute-dsl`/`xqa`/`sparse` 间自动调优，输入是**拼接的张量** `q=[q_nope||q_pe]`、`kv_cache=[ckv||kpe]`，且带 autotune。

本模块先吃透经典 wrapper（它最能体现 MLA 的数据流），再点出新一代 API 的差异。

经典 wrapper 的「plan/run」与普通 decode 完全同构（承接 u3-l3）：

- **`plan()`** 只依赖批次结构（每请求的 q/kv 长度、页表），做 split-k 调度、分配 workspace 里的 `partial_o`/`partial_lse` 与各种索引数组，返回一份 `plan_info`（一串 int64 偏移）。因为含动态调度，**不可进 CUDA Graph**，但可被同一前向的所有 Transformer 层复用。
- **`run()`** 只携带每层数据（实际的 q/kv 张量），把 `plan_info` 与四张量喂给已 JIT 编译的模块，启动 kernel。run 可进图。

> 关键差异：普通 decode wrapper 的 `plan` 输入是「paged_kv_indptr/indices/last_page_len」三件套；MLA wrapper 的 `plan` 输入是 `qo_indptr`/`kv_indptr`/`kv_indices`/`kv_len_arr`——因为 MLA 的页表只有一个 KV 头（跨头共享），所以页表更简单，但多了 `qo_indptr` 来描述变长 query（支持增量 prefill，不限于每请求 1 个 query）。

#### 4.2.2 核心流程

经典 wrapper 的典型生命周期：

```
构造 wrapper(workspace, backend="auto")
        │
        ▼
plan(qo_indptr, kv_indptr, kv_indices, kv_len_arr,
     num_heads, head_dim_ckv, head_dim_kpe, page_size,
     causal, sm_scale, q_dtype, kv_dtype)
   │  1. 选后端 (fa2/fa3)，按 dtype/维度 JIT 编译/取缓存模块
   │  2. 把 qo/kv 索引拷到 GPU（CUDA Graph 模式则拷进预分配 buf）
   │  3. 调 C++ plan → 返回 plan_info（split-k 调度结果）
   ▼
run(q_nope, q_pe, ckv_cache, kpe_cache)  ← 每层调用
   │  → C++ run → BatchMLAPagedAttentionKernel
   │       阶段1: 各 cluster 算 partial_o/partial_lse（split-k）
   │       grid.sync()
   │       阶段2: DevicePersistentMergeStates 合并 partial 结果
   ▼
返回 o（shape [batch, num_heads, 512]），可选 lse
```

kernel 是一个**协作组 kernel**（`cudaLaunchCooperativeKernel`），二维 grid `(num_blks_x=cluster_size, num_blks_y=num_clusters)`：

- 第一维 `num_blks_x` 是一个 cluster 内的 CTA 数（每 CTA 处理 `CTA_TILE_Q=64` 个 query 头）。
- 第二维 `num_blks_y` 是 KV 上的 split-k 切片数（多个 cluster 各算一段 KV 的部分结果）。
- 全 grid 同步后进入合并阶段，把各 split 的 `(partial_o, partial_lse)` 用 log-sum-exp 合并成最终 `o`。

#### 4.2.3 源码精读

**wrapper 类与构造。** `BatchMLAPagedAttentionWrapper.__init__` 分配 int workspace、缓存用户给的 qo/kv 缓冲（CUDA Graph 用），并在 `backend=="auto"` 时调 `determine_mla_backend` 选 fa3/fa2：

[flashinfer/mla/_core.py:1471-1548](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/mla/_core.py#L1471-L1548) — 构造函数，注意 `cutlass` 后端走完全不同的 run 路径（拼接张量、不同的 csrc），所以构造时直接 `return`，不分配 fa 系列的 workspace。

**plan 做了什么。** `plan` 先做 KV dtype 校验（FP8 仅 fa3+SM90），再 `get_batch_mla_module(...)` 按 `(backend, dtype, head_dim)` 取 JIT 模块，最后调模块的 C++ `plan`：

[flashinfer/mla/_core.py:1638-1682](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/mla/_core.py#L1638-L1682) — `plan` 末尾：`self._plan_info = self._cached_module.plan(...)`，`plan_info` 是一串 int64 偏移。

**run 的 fa2/fa3 路径。** 装配好 `out`/`lse` 后，把 `plan_info` 与四张量等交给模块：

[flashinfer/mla/_core.py:1925-1946](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/mla/_core.py#L1925-L1946) — `self._cached_module.run(float_ws, int_ws, plan_info, q_nope, q_pe, ckv, kpe, kv_indices, out, lse, mask_mode, num_heads, page_size, sm_scale, ...)`。

**docstring 里的可运行示例。** 类文档给了一段完整的 plan/run 示例，`sm_scale = 1.0/((128+64)**0.5)` 正是「用吸收前头维度」的体现，输出 `o.shape == [114, 128, 512]`：

[flashinfer/mla/_core.py:1397-1443](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/mla/_core.py#L1397-L1443) — 这是本讲综合实践的基础。

**C++ run 入口。** `csrc/batch_mla_run.cu` 把 Python 传来的 `plan_info`（一个 int64 数组）还原成 `MLAPlanInfo`，再用其中的偏移从 workspace 里取出各种索引数组，装配 `Params` 启动 kernel：

[csrc/batch_mla_run.cu:30-128](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/csrc/batch_mla_run.cu#L30-L128) — `BatchMLAPagedAttentionRun`。注意它用 `DISPATCH_context(...)` 宏（来自 Jinja 渲染的 `batch_mla_config.inc`）按 dtype/维度实例化模板，再调 `mla::BatchMLAPagedAttention`。

**两阶段协作 kernel。** kernel 主体在一个 `for (work_idx ...)` 循环里跑「load_q → load_kv → compute_mla_qk → logits_mask → update_mdo → compute_mla_pv」，循环结束后 `grid.sync()`，再进入 `DevicePersistentMergeStates` 合并：

[include/flashinfer/attention/mla.cuh:906-1070](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/attention/mla.cuh#L906-L1070) — kernel 主循环与合并阶段。`grid.sync()`（[1061-1062 行](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/attention/mla.cuh#L1061-L1062)）是 split-k 两阶段实现的分界。

**合并用 log-sum-exp。** `DevicePersistentMergeStates` 用 `state_t::merge(o_partial, lse_partial)` 把每个 split 的部分输出按 LSE 加权合并再 `normalize()`：

[include/flashinfer/attention/mla.cuh:640-687](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/attention/mla.cuh#L640-L687) — 这与 [u4-l2](u4-l2-cascade-attention.md) 将讲的 cascade `merge_state` 是同一套数学（在线 softmax 合并）。

**plan_info 与调度。** `MLAPlanInfo` 是 plan/run 之间的契约，记录 grid 形状与所有 workspace 偏移；`MLAPlan` 根据 SM 数与每请求长度做 split-k 划分：

[include/flashinfer/attention/scheduler.cuh:1488-1553](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/attention/scheduler.cuh#L1488-L1553) — `MLAPlanInfo` 的 `ToVector`/`FromVector`（plan 返回、run 接收的就是它）。

[include/flashinfer/attention/scheduler.cuh:1557-1602](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/attention/scheduler.cuh#L1557-L1602) — `MLAPlan` 开头：探测 SM 数、决定 `num_blks_x`/`num_blks_y`（cluster_size 与 cluster 数）。

**smem 自适应。** kernel 按 GPU 每 SM 共享内存大小选 `(NUM_STAGES, CTA_TILE_KV, QK_SHARD)` 三档配置：

[include/flashinfer/attention/mla.cuh:1072-1093](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/attention/mla.cuh#L1072-L1093) — `DISPATCH_SMEM_CONFIG`，显存大的卡用更大 tile + QK 分片跨 warpgroup。

#### 4.2.4 代码实践

**实践目标：** 跑通 docstring 里的 MLA decode，确认输出形状与张量约定。

**操作步骤：**

1. 确保已按 [u1-l2](u1-l2-installation-and-first-run.md) 安装 flashinfer，且 GPU 为 SM80/SM90（fa2/fa3 路径；Blackwell 请改用 4.3 讲的新 API）。
2. 把 [docstring 示例](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/mla/_core.py#L1397-L1443) 抄进一个脚本并运行。可在 `plan` 之前 `export FLASHINFER_LOGLEVEL=1` 观察后端选择与 JIT 编译。
3. 在 `run` 之后打印 `o.shape`、`o.dtype`。

**需要观察的现象：**

- 首次运行会触发 JIT 编译（fa2 路径编译 `batch_mla_plan/run/binding.cu`，fa3 路径额外要 SM90a flag），有可见的编译等待；第二次运行秒回。
- 输出 `o.shape == torch.Size([114, 128, 512])`，即 `[batch, num_heads, kv_lora_rank]`。
- 注意示例里 `q_nope` 最后一维是 `head_dim_ckv=512`（吸收后的潜维度），而**不是** 128，这跟普通 decode 的 q 形状完全不同。

**预期结果：** 输出形状如上。若报「FP8 kv_data_type ... only supported with the fa3 backend on SM90」，说明你在非 SM90 卡上用了 FP8 KV——回到 BF16 即可。

> 若无合适 GPU，本实践降级为「源码阅读型」：在 `_core.py:1638-1682` 的 `plan` 与 `1925-1946` 的 `run` 之间画一条数据流，标出每个参数如何映射到 C++ `BatchMLAPagedAttentionRun` 的形参（见 [csrc/batch_mla_run.cu:30-35](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/csrc/batch_mla_run.cu#L30-L35)），待本地验证运行结果。

#### 4.2.5 小练习与答案

**练习 1.** `plan()` 为什么不能进 CUDA Graph，而 `run()` 可以？

> 答案：plan 含「按批次结构动态决定 split-k 划分、往 workspace 写索引」等 host 端决策与动态 shape，无法静态捕获；run 只消费 plan 的结果与每层数据，形状固定、无动态分支，可进图。

**练习 2.** 同一个 `BatchMLAPagedAttentionWrapper` 实例能在一次前向里被 80 层 Transformer 复用吗？需要每层重新 plan 吗？

> 答案：可以复用；只要这一批请求的批次结构（qo/kv 长度、页表）不变，plan 一次即可，80 层都调同一个 `run()`。只有批次结构变化（如新请求加入）才需要重 plan。

**练习 3.** 为什么 MLA wrapper 的 `run` 要把 `q_nope` 和 `q_pe` 作为两个张量传入，而不是一个拼接张量？

> 答案：kernel 内部对这两路用不同的 MMA 形状与共享内存布局（`HEAD_DIM_CKV` vs `HEAD_DIM_KPE`），且只有 `q_pe·kpe` 这一路需要参与位置相关的计算；分开传入让 launcher 直接拿到两段指针、避免在 kernel 内做切分。新一代 API 改用拼接张量是为了适配 Blackwell 后端的统一布局。

---

### 4.3 后端派发：从 fa2/fa3 到 Blackwell 的多后端世界

#### 4.3.1 概念说明

MLA 的后端比普通注意力更「碎」，因为它要覆盖从 Ampere 到 Blackwell 的多代架构，且每代都有专门优化的 kernel。分两个层次理解：

**第一层：经典 wrapper 的 `backend`（fa2/fa3/cutlass）。** 这是 `BatchMLAPagedAttentionWrapper` 内部的二选一/三选一：

- `fa2`：基于 `mla.cuh` 的 cooperative kernel，兼容 SM80+，是兜底路径。
- `fa3`：Hopper 专用，依赖 `wgmma`/TMA，`auto` 模式下 SM90a 首选。
- `cutlass`：SM100/SM110 的 CUTLASS MLA decode kernel，输入布局不同（拼接张量），是 wrapper 内「最接近 Blackwell 原生」的备选，但 decode 性能不如专门的 trtllm-gen。

派发函数极其简洁——`determine_mla_backend` 就一行：

```python
def determine_mla_backend(device):
    return "fa3" if is_sm90a_supported(device) else "fa2"
```

**第二层：新一代函数 `trtllm_batch_decode_with_kv_cache_mla` 的 `backend`（trtllm-gen/cute-dsl/xqa/sparse）。** 这是为 Blackwell 及更新架构准备的，且带 autotune：

- SM100/SM103（B100/B200 系列）：`trtllm-gen` 与 `cute-dsl` 两个 runner 竞争，autotune 按形状选优。
- SM120/SM121（新一代）：dense decode 走 `xqa`；稀疏 MLA（`sparse_mla_top_k>0`）走 `sparse`（packed uint8 KV）。

为什么要有这一层？因为 wrapper 里的 `fa2/fa3` 在 SM≥100 上**不是 Blackwell 原生**，性能差。源码里专门有一条警告：在 Blackwell 上 `backend="auto"` 选了 fa2/fa3 时，会一次性提醒用户「decode 请改用 `trtllm_batch_decode_with_kv_cache_mla`」。

#### 4.3.2 核心流程

两个入口的后端决策可以画成一张表：

| 调用入口 | 硬件 | `backend="auto"` 实际选择 |
|----------|------|--------------------------|
| `BatchMLAPagedAttentionWrapper` | SM90a | `fa3` |
| `BatchMLAPagedAttentionWrapper` | SM80/86/89 | `fa2` |
| `BatchMLAPagedAttentionWrapper` | SM100+ | `fa2`/`fa3`（fallback，并打印警告） |
| `BatchMLAPagedAttentionWrapper` | 任意 | 可显式 `cutlass`（SM100/110） |
| `trtllm_batch_decode_with_kv_cache_mla` | SM100/SM103 | autotune: `trtllm-gen` vs `cute-dsl` |
| `trtllm_batch_decode_with_kv_cache_mla` | SM120/SM121, dense | `xqa` |
| `trtllm_batch_decode_with_kv_cache_mla` | SM120/SM121, sparse | `sparse` |

JIT 层面，后端决定编译哪组 csrc 文件（承接 [u2-l3](u2-l3-codegen-pattern.md) 的五步生成）：

- fa2 → `batch_mla_plan.cu` / `batch_mla_run.cu` / `batch_mla_binding.cu` + `batch_mla_config.jinja`
- fa3 → `batch_mla_sm90_*.cu` + `sm90a_nvcc_flags`
- cutlass → `cutlass_mla.cu` / `flashinfer_mla_binding.cu`，`supported_major_versions=[10,11]`

#### 4.3.3 源码精读

**经典派发函数。**

[flashinfer/utils.py:637-638](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/utils.py#L637-L638) — `determine_mla_backend`，一行二选一。对照 [u3-l5](u3-l5-backend-selection.md) 的 `determine_attention_backend`，MLA 的派发更简单（只看 SM90a）。

**构造时调派发 + Blackwell 警告。**

[flashinfer/mla/_core.py:1544-1548](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/mla/_core.py#L1544-L1548) — `auto` 时调 `determine_mla_backend` 并 `_maybe_warn_blackwell_auto_fallback`。

[flashinfer/mla/_core.py:1448-1469](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/mla/_core.py#L1448-L1469) — 警告逻辑：SM≥100 时一次性提示用户改用 `trtllm_batch_decode_with_kv_cache_mla`。

**新一代函数的多后端派发。**

[flashinfer/mla/_core.py:2766-2772](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/mla/_core.py#L2766-L2772) — `backend=="auto"` 的决策：SM120 且 sparse → `sparse`；非 SM10 → `xqa`；否则（SM100/103）保持 `auto` 进入下面的 trtllm-gen/cute-dsl autotune。

[flashinfer/mla/_core.py:3062-3081](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/mla/_core.py#L3062-L3081) — `auto` 路径下，分别用 `trtllm_gen_not_supported_reason` 与 `_cute_dsl_incompatibility_reason(...)` 过滤，把兼容的后端放进 `runner_names`，再交给 autotuner。

[flashinfer/mla/_core.py:3151-3157](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/mla/_core.py#L3151-L3157) — `AutoTuner.get().choose_one(...)` 在候选 runner 间选优（承接 [u10-l2](u10-l2-autotuning.md) 的 autotuner）。

**JIT 层的后端分叉。**

[flashinfer/jit/attention/modules.py:137-193](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/attention/modules.py#L137-L193) — `gen_batch_mla_module`：`fa2` 与 `fa3` 编译不同组的 csrc 文件，`fa3` 还附加 `sm90a_nvcc_flags`。这正是「后端 → 编译哪份代码」的落点。

[flashinfer/jit/mla.py:21-32](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/mla.py#L21-L32) — `gen_mla_module`（cutlass 路径），`supported_major_versions=[10,11]` 把它限制在 Blackwell。

#### 4.3.4 代码实践

**实践目标：** 不运行 kernel，仅通过源码与日志，搞清「在你的机器上 MLA 会走哪个后端」。

**操作步骤：**

1. 用 PyT查出本机 compute capability：`torch.cuda.get_device_capability()`（如 `(9,0)`）。
2. 在 Python 里直接调 `flashinfer.utils.determine_mla_backend(torch.device("cuda"))`，打印返回值。
3. 对照 [utils.py:637-638](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/utils.py#L637-L638) 与 [_core.py:2766-2772](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/mla/_core.py#L2766-L2772) 两处派发表，分别预测：经典 wrapper 会选什么？`trtllm_batch_decode_with_kv_cache_mla` 会选什么？
4. 若是 SM≥100，构造一个 `BatchMLAPagedAttentionWrapper(backend="auto")`，观察是否触发 [_core.py:1448-1469](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/mla/_core.py#L1448-L1469) 的 Blackwell fallback 警告。

```python
# 示例代码：探测后端选择（需要 GPU + flashinfer，但不会真正跑 kernel）
import torch, flashinfer
from flashinfer.utils import determine_mla_backend, get_compute_capability

dev = torch.device("cuda")
cc = get_compute_capability(dev)
print("compute capability:", cc)                       # e.g. (9, 0)
print("classic wrapper backend :", determine_mla_backend(dev))  # fa3 / fa2
# 新一代函数的派发逻辑是内联的（见 _core.py:2766-2772），可手写复刻：
major = cc[0]
if major == 12:
    new_backend = "xqa"  # dense；sparse 需 sparse_mla_top_k>0
elif major == 10:
    new_backend = "auto"  # 进入 trtllm-gen/cute-dsl autotune
else:
    new_backend = "(not native; use classic wrapper)"
print("new API backend (predict):", new_backend)
```

**需要观察的现象：** `determine_mla_backend` 在 Hopper 返回 `fa3`、在 Ampere 返回 `fa2`；Blackwell 上构造经典 wrapper 时应出现一次性 UserWarning。

**预期结果：** 与上面派发表一致。若你在 SM100 上没看到警告，检查是否已经在本进程内构造过一次（警告用类变量 `_blackwell_auto_fallback_warned` 去重，只报一次）。

> 无 GPU 时降级为源码阅读：直接读 [_core.py:2766-2772](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/mla/_core.py#L2766-L2772) 与 [_core.py:3062-3081](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/mla/_core.py#L3062-L3081)，画出 `backend="auto"` 的决策树即可，待本地验证。

#### 4.3.5 小练习与答案

**练习 1.** 在 Hopper (SM90) 上，`BatchMLAPagedAttentionWrapper(backend="auto")` 会编译哪几个 csrc 文件？

> 答案：fa3 路径编译 `batch_mla_sm90_plan.cu`、`batch_mla_sm90_run.cu`、`batch_mla_sm90_binding.cu`（外加 Jinja 渲染的 `batch_mla_sm90_config.inc`），nvcc 附带 `sm90a_nvcc_flags`。

**练习 2.** 为什么 Blackwell 上 `auto` 不直接选 `cutlass`，而是提示用户改用另一个函数？

> 答案：wrapper 内的 fa2/fa3/cutlass 都不是 Blackwell decode 的最优解；`trtllm_batch_decode_with_kv_cache_mla` 的 trtllm-gen/cute-dsl 才是 Blackwell 原生、且带 autotune。所以源码选择「给你一个警告 + 最接近的备选，同时指路新 API」。

**练习 3.** `trtllm_batch_decode_with_kv_cache_mla` 在 SM100 上 `backend="auto"` 时，autotune 在哪两个 runner 之间选？

> 答案：`TrtllmGenMlaDecodeRunner`（trtllm-gen）与 `CuteDslMlaDecodeRunner`（cute-dsl），由 `AutoTuner.get().choose_one("trtllm_batch_decode_mla", runners, tuning_config, inputs)` 按 batch 桶择优并缓存。

## 5. 综合实践

把本讲三个模块串起来：**用经典 wrapper 跑一个 DeepSeek 风格的 MLA decode，再从输出反推 MLA 的数据流。**

1. **构造数据（对照 4.1 的四张量表）。** 取 `batch_size=8, num_heads=128, head_dim_ckv=512, head_dim_kpe=64, page_size=1, kv_len=512`。分别分配：
   - `q_nope`: `[8, 128, 512]` BF16
   - `q_pe`: `[8, 128, 64]` BF16
   - `ckv`: `[8*512, 1, 512]` BF16（页表直连，每页 1 token）
   - `kpe`: `[8*512, 1, 64]` BF16
2. **构造索引。** `qo_indptr=[0,1,2,...,8]`（每请求 1 个 query）；`kv_indptr=[0,512,1024,...]`；`kv_indices=range(8*512)`；`kv_len_arr=full(8, 512)`。
3. **plan + run。** `sm_scale = 1.0/((128+64)**0.5)`，`backend="auto"`，先 `export FLASHINFER_LOGLEVEL=1` 看后端选择。
4. **验证形状与数据流：**
   - 打印 `o.shape`，确认为 `[8, 128, 512]`。
   - 回答：如果改 `num_heads=64`，`o.shape` 的最后一维会变吗？KV cache 显存会变吗？（答：都不会，因为 MLA 的潜维度与 KV 都与头数无关；只有 `o.shape[1]` 变成 64。）
   - 在源码里定位：你的 `o` 是由 kernel 的哪个函数写出的？（[mla.cuh:738-850](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/attention/mla.cuh#L738-L850) 的 `write_o`，且每个 split 的部分结果先写进 `partial_o`，再由 `DevicePersistentMergeStates` 合并。）
5. **（选做）后端对比。** 在 Hopper 上分别用 `backend="fa2"` 与 `backend="fa3"` 各跑一次，用 `FLASHINFER_LOGLEVEL=1` 观察编译的 csrc 文件不同；若有 cupti-python，可用 `bench_gpu_time`（见 [u10-l3](u10-l3-benchmarking.md)）比较两者耗时。

> 若本机无 GPU：综合实践降级为「画两张图」——(a) 从 Python `run()` 到 CUDA `write_o()` 的调用栈（含 plan_info 在 csrc 里如何被还原成指针）；(b) 一个 token 的 KV 如何被 128 个头共享计算。两张图能画清楚，本讲就通了。运行结果待本地验证。

## 6. 本讲小结

- **MLA 的本质是低秩潜向量压缩**：跨头共享一条 `ckv(512)` 当 K/V、一条 `kpe(64)` 当带位置 K，使每 token KV 元素数（576）与 `num_heads` 无关，相比 MHA 节省数十倍显存。
- **四张量模型**：`q_nope[B,H,512]`、`q_pe[B,H,64]`、`ckv_cache[P,ps,512]`、`kpe_cache[P,ps,64]`；输出与 `q_nope` 同形，因为 V=ckv。
- **注意力计算两路累加**：`s = q_pe·kpe + q_nope·ckv`，softmax 缩放用吸收前的 192 维；这是 `compute_mla_qk`/`compute_mla_pv` 的设计出发点。
- **经典 wrapper 走 plan/run**：plan 做 split-k 调度返回 `plan_info`，run 启动 cooperative kernel（先各 cluster 算 partial、`grid.sync()` 后合并），与普通 decode 同构但页表更简。
- **后端派发分两层**：经典 wrapper 在 fa2/fa3/cutlass 间选（SM90a→fa3，否则 fa2，一行 `determine_mla_backend`）；Blackwell 原生走 `trtllm_batch_decode_with_kv_cache_mla`（trtllm-gen/cute-dsl autotune，或 SM120 的 xqa/sparse）。
- **JIT 与后端绑定**：不同后端编译不同 csrc 文件（`gen_batch_mla_module` 分 fa2/fa3，`gen_mla_module` 管 cutlass），承接 [u2-l3](u2-l3-codegen-pattern.md) 的五步生成模式。

## 7. 下一步学习建议

- 想继续深入注意力变体：读 [u4-l2](u4-l2-cascade-attention.md) 共享前缀 cascade——本讲 `DevicePersistentMergeStates` 的 LSE 合并数学与 cascade 的 `merge_state` 同源，一起看会豁然开朗。
- 想搞清 Blackwell 原生 MLA 的细节：直接读 `trtllm_batch_decode_with_kv_cache_mla`（[_core.py:2542-3162](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/mla/_core.py#L2542-L3162)）与 `csrc/batch_decode_mla_run.cu`，对照 autotuner（[u10-l2](u10-l2-autotuning.md)）。
- 想理解 split-k 合并的数学：精读 `mla.cuh` 的 `DevicePersistentMergeStates` 与 `state_t::merge`，再看 `include/flashinfer/attention/cascade.cuh`。
- 想看 MLA 的上游「矩阵吸收」是怎么把 query 投影到潜空间的：阅读 wrapper docstring 引用的博客 <http://flashinfer.ai/2025/02/10/flashinfer-deepseek-mla.html>，再回看 `MLAHeadDimensions` 里 `qk_nope_head_dim` 与 `kv_lora_rank` 的关系。
- 进阶：DeepSeek-V4 的稀疏 MLA（`trtllm_batch_decode_sparse_mla_dsv4`）与本讲 dense MLA 共享同一套潜空间思想，但加入了 SWA + compressed 双段稀疏页表，可作为本讲之后的挑战阅读。
