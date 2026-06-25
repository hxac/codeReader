# 索引器的 MQA 评分内核

## 1. 本讲目标

本讲聚焦 DeepGEMM 中专门为 **DeepSeek v3.2 闪电索引器（lightning indexer）** 设计的评分内核 `fp8_fp4_mqa_logits`。读完本讲，你应当能够：

1. 用数学语言精确描述「加权 ReLU MQA logit」的定义，并解释它为什么不同于普通 attention 的 softmax logit。
2. 说清楚该内核的输入（`q / kv / weights / cu_seq_len_k_*`）与输出张量的形状、dtype、内存布局约束，以及两种输出模式（full 与 compressed）的区别。
3. 看懂宿主侧如何用 `arch_major`（9 vs 10）与 `is_fp4` 两个开关，把同一次调用派发到 SM90 FP8、SM100 FP8 或 SM100 FP4 三条实现路径，以及每条路径对 `num_heads / head_dim` 的硬约束。
4. 在设备内核里追到「张量核产出 → 加权 ReLU 归约 → 写回 logits → 可选 `-inf` 清理」这条主线，理解 FP8 走软件缩放、FP4 走硬件 block-scaled UMMA 的根因。

本讲承接 [u2-l3 C++ 绑定与派发层](u2-l3-cpp-binding-and-dispatch.md)（架构派发范式）与 [u6-l2 MMA 抽象：WGMMA vs UMMA](u6-l2-mma-wgmma-vs-umma.md)（SM100 UMMA 与缩放因子），是单元 9「其它内核家族」的第一站；分页版 `fp8_fp4_paged_mqa_logits` 留给 [u9-l2 分页 MQA logits](u9-l2-paged-mqa-logits.md)。

## 2. 前置知识

- **MQA（Multi-Query Attention）**：常规 MHA 里每个 query head 对应一组独立的 K/V head；MQA 让所有 query head **共享同一份 K/V**（即 KV head 数 = 1）。本内核里 KV 的形状是 `[seq_len_kv, head_dim]`，没有 head 维，正是 MQA 的特征。
- **逐行缩放因子（per-token SF）**：FP8/FP4 动态范围窄，需要对每个 token 的向量除以一个缩放因子 \(s\)（\(s=\text{amax}/448\)）后再量化。复习 [u2-l2 缩放因子 recipe 与 UE8M0 打包](u2-l2-scaling-factor-recipe-ue8m0.md)：SM90 用 FP32 存 SF、SM100 用打包 UE8M0。本内核里 KV 的 SF 是「每行一个标量」（`[seq_len_kv]`），与 GEMM 的逐块 SF 不同。
- **cu_seq_len（累积序列长度）**：这是变长/上下文并行（Context Parallelism, CP）场景里常见的「前缀和式边界」数组。这里每个 query token `i` 自带一对 `[start, end)`，表示它只能「看到」KV 中 `[cu_seq_len_k_start[i], cu_seq_len_k_end[i])` 这段 token——不同 query 行的可见窗口可以不同。
- **`-inf` 与掩码**：attention 里被掩掉的位置通常填 `-inf`，这样后续 softmax 会把它归零。本内核的 `clean_logits` 就是做这件事的收尾步骤。
- **UMMA / TMEM（SM100）**：复习 [u6-l2](u6-l2-mma-wgmma-vs-umma.md)——SM100 的张量核指令 `tcgen05.mma` 把累加结果写进一块特殊的 **tensor memory（TMEM）**，而不是寄存器；FP4 还能用 block-scaled UMMA 让硬件直接吸收 UE8M0 缩放因子。本讲会用到这两个概念。

## 3. 本讲源码地图

| 文件 | 层 | 作用 |
| --- | --- | --- |
| `README.md`（V3.2 MQA 章节） | 文档 | 给出 6 个输入说明与 `out[i,j]` 的伪代码，是理解数学定义的第一手资料。 |
| `csrc/apis/attention.hpp` | 宿主 API | 定义 `fp8_fp4_mqa_logits`：参数校验、输出分配、架构/dtype 派发、`clean_logits` 收尾，以及 pybind11 注册。 |
| `csrc/jit_kernels/impls/sm100_mqa_logits.hpp` | 宿主 Runtime | SM100 路径：构造 TMA 描述符、选流水线深度、`generate_impl` 生成 `.cu`、JIT 编译并启动。 |
| `csrc/jit_kernels/impls/sm90_fp8_mqa_logits.hpp` | 宿主 Runtime | SM90 路径：同上，但用 WGMMA、线程数与流水线深度不同。 |
| `deep_gemm/include/deep_gemm/impls/sm100_mqa_logits.cuh` | 设备 kernel | SM100 设备核心 `sm100_mqa_logits_core_impl`：TMA 加载、UMMA 计算、加权 ReLU 归约、logits 写回。 |
| `deep_gemm/include/deep_gemm/scheduler/sm100_mqa_logits.cuh` | 设备调度 | `SM100MQALogitsScheduler`：把 query 行的 `[start,end)` 窗口翻译成 KV 加载范围与 logits 列坐标。 |
| `deep_gemm/include/deep_gemm/impls/smxx_clean_logits.cuh` | 设备 kernel | `clean_logits` 收尾：把未填写的位置刷成 `-inf`。 |
| `tests/test_attention.py` | 测试 | `test_mqa_logits` 与 `ref_fp8_mqa_logits` 参考实现，是验证理解的最佳脚手架。 |

调用链回顾（详见 [u1-l3](u1-l3-directory-and-layered-arch.md)）：`deep_gemm.fp8_fp4_mqa_logits` → `csrc/python_api.cpp` 的 pybind11 注册 → `attention::fp8_fp4_mqa_logits`（校验 + 分配 + 派发）→ `sm100_mqa_logits` / `sm90_fp8_mqa_logits`（宿主 Runtime，JIT 编译）→ 设备 `.cuh`（tensor core 执行）→ 可选 `smxx_clean_logits`。

## 4. 核心概念与源码讲解

### 4.1 MQA logits 的数学定义与加权 ReLU 聚合

#### 4.1.1 概念说明

闪电索引器需要在海量 KV token 里快速挑出与 query 相关的少数 token。它用的打分函数**不是** softmax attention，而是一种**加权 ReLU 聚合**：对 query token \(i\) 与 key token \(j\)，先逐 head 算点积，再过 ReLU、乘权重、跨 head 求和，得到一个标量 logit。README 把它写成四行伪代码：

```python
kv_j = kv[0][j, :] * kv[1][j].unsqueeze(1)  # 反量化 KV 第 j 行 -> [head_dim]
out_ij = q[i, :, :] @ kv_j                   # 逐 head 点积 -> [num_heads]
out_ij = out_ij.relu() * weights[i, :]       # ReLU 后乘逐 head 权重 -> [num_heads]
out_ij = out_ij.sum()                        # 跨 head 求和 -> 标量
```

注意第一行 `kv[0]` 是 FP8/FP4 数据、`kv[1]` 是逐行缩放因子，二者相乘得到反量化的 KV 向量。把它写成数学式：

\[
\text{out}[i,j] \;=\; \sum_{h=0}^{H-1} w[i,h]\;\cdot\;\mathrm{ReLU}\!\left(\sum_{d=0}^{D-1} q[i,h,d]\cdot \widetilde{kv}[j,d]\right)
\]

其中 \(\widetilde{kv}[j,d]=kv[j,d]\cdot s_{kv}[j]\) 是反量化后的 KV（FP8 路径在软件里乘 \(s_{kv}\)；FP4 路径则由硬件 block-scaled UMMA 吸收，见 4.4）。与普通 attention 的关键差别有两点：

1. **ReLU 而非 softmax**：打分只保留正相关（负点积被 ReLU 清零），不需要全局归一化，因此可以逐 \((i,j)\) 独立计算，天然适合并行。
2. **跨 head 加权求和折叠成标量**：最终每个 \((i,j)\) 只剩一个数，所以输出是 `[seq_len, seq_len_kv]` 的二维 token-to-token 矩阵，而不是 attention 里 `[seq_len, num_heads, seq_len_kv]` 的逐 head 权重。

#### 4.1.2 核心流程

从「一个 \((i,j)\)」的视角，计算步骤是：

1. 取 query 行 \(q[i]\in\mathbb{R}^{H\times D}\) 与反量化 KV 行 \(\widetilde{kv}[j]\in\mathbb{R}^{D}\)。
2. 逐 head 做点积得 \(H\) 个标量 \(a_h=\sum_d q[i,h,d]\widetilde{kv}[j,d]\)。
3. 对每个 \(a_h\) 取 \(\mathrm{ReLU}(a_h)=\max(0,a_h)\)。
4. 乘逐 head 权重：\(b_h=w[i,h]\,\mathrm{ReLU}(a_h)\)。
5. 跨 head 求和：\(\text{out}[i,j]=\sum_h b_h\)。

整个内核就是把这个标量计算**铺到 tensor core 上**：用 UMMA 一次性算出一个 query 块（`BLOCK_Q` 个 token × \(H\) 个 head）与一个 KV 段（`SPLIT_KV` 个 token）的全体点积，再在寄存器里做 ReLU + 加权求和。

> 数学小贴士：设备代码用一个等价变形把 ReLU 融进 FMA。因为 \(x+\lvert x\rvert = 2\,\mathrm{ReLU}(x)\)，所以 \(\mathrm{ReLU}(x)\,w = \tfrac{1}{2}(x+\lvert x\rvert)\,w\)，最后整体除以 2 即可（见 4.4.3）。这样可以避免分支，全程用 FMA/绝对值指令完成。

#### 4.1.3 源码精读

伪代码定义在 README，先把它作为「契约」对齐认知：

- [README.md:L105-L110](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/README.md#L105-L110)：`out[i,j]` 的四行计算，注意第一行是反量化、第四行 `.sum()` 是跨 head 折叠成标量。
- [README.md:L93-L101](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/README.md#L93-L101)：6 个输入的形状与 dtype 说明，输出形状 `[seq_len, seq_len_kv]`。

设备侧把第 4 步（加权 ReLU 求和）实现在 math 线程里。FP32 权重路径的核心几行（注意 `(x+|x|)` 技巧与最后的 `/2`）：

- [sm100_mqa_logits.cuh:L470-L487](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_mqa_logits.cuh#L470-L487)：`transform` 里 `__fadd2_rn(a_0, a_1)` 即 `accum+|accum|`，乘 `weights` 后累加；末尾 `(sum.x + sum.y) / 2` 抵消掉那个 2 倍，得到 \(\sum_h \mathrm{ReLU}(a_h)w_h\)。
- [sm100_mqa_logits.cuh:L452-L468](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_mqa_logits.cuh#L452-L468)：BF16 权重路径用 `cvt_relu_bf16x2_f32`（把 ReLU 折进 bf16 转换），因此没有 `/2`，直接得到加权和。两条路径数学等价，只是精度/指令取舍不同。

逐 head 点积 \(a_h\)（第 2 步）由 tensor core 完成，结果落在 TMEM，再由 `tmem_load_no_fence` 读进 `accum[kNumHeads]`：

- [sm100_mqa_logits.cuh:L429-L444](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_mqa_logits.cuh#L429-L444)：按 `num_heads` 是 8/16/32/64 选择不同宽度的 TMEM load 指令，把一个 query token 的 \(H\) 个点积装进 `accum`。

#### 4.1.4 代码实践

**目标**：亲手验证「`(x+|x|)/2` 等价于 ReLU」这个设备代码赖以消除分支的等式。

**步骤**：

1. 读 [sm100_mqa_logits.cuh:L470-L487](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_mqa_logits.cuh#L470-L487)，把 `transform` 与末尾 `/2` 抄成 Python。
2. 用 `torch` 随机生成一组 `accum`（含正含负）与 `weights`，分别用「直接 ReLU」与「`(x+|x|)/2`」两种方式计算加权和，对比是否一致。
3. 改成全负的 `accum`，确认两者都得到 0（ReLU 把负值清零）。

**预期结果**：两种实现逐元素相等（浮点误差范围内）。这解释了为何设备内核能在不引入 `if` 分支的前提下完成 ReLU。

**示例代码**（非项目原有代码，仅为验证用）：

```python
import torch
accum = torch.randn(64)          # 模拟逐 head 点积
w     = torch.randn(64)
direct = (accum.relu() * w).sum()
trick  = ((accum + accum.abs()) * w).sum() / 2
print((direct - trick).abs().item())   # 应接近 0
```

#### 4.1.5 小练习与答案

**Q1**：若把末尾的 `/2` 去掉，输出会偏多少？
**答**：会变成正确值的 2 倍，因为 `(x+|x|)=2·ReLU(x)` 引入的因子 2 没有被抵消。

**Q2**：为什么 BF16 权重路径（4.1.3 第二条链接）没有 `/2`？
**答**：它用 `cvt_relu_bf16x2_f32` 把 ReLU 直接做进类型转换（不是用 `(x+|x|)` 技巧），得到的就是真正的 \(\mathrm{ReLU}(x)\)，因此不需要补偿因子。

**Q3**：把逐 head 点积改成普通 softmax attention，本内核的哪一步会失效？
**答**：softmax 要求对每个 query 行在所有 key token 上做全局归一化，而本内核是逐 \((i,j)\) 独立写回标量、没有跨 \(j\) 的归约；去掉 ReLU 换成 softmax 会破坏「逐 token 独立」的并行结构。

### 4.2 输入输出张量布局与 cu_seq_len 窗口

#### 4.2.1 概念说明

数学定义清楚了，接下来要约束**数据长什么样**。本内核是非分页（prefill）版，输入输出都住在连续张量里：

- `q`：query，形状 `[seq_len, num_heads, head_dim]`，**必须 contiguous**。FP8 时是 `e4m3`；FP4 时数据是 packed（每两元素一个字节），所以张量最后一维是 `head_dim/2`，dtype 是 `kPackedFP4`（实为 `int8` 别名，见 [u5-l1](u5-l1-gemmdesc-config-structs.md)），另带一个 `[seq_len, num_heads]` 的 `q_sf`。
- `kv`：key/value，形状 `[seq_len_kv, head_dim]`（MQA：所有 head 共享，无 head 维），contiguous。它的缩放因子是**逐行一个标量** `sf_kv`，形状 `[seq_len_kv]`：FP8 时 `float`、FP4 时打包 `int32`（UE8M0）。
- `weights`：逐 head 权重，形状 `[seq_len, num_heads]`，最后一维 stride 必须为 1（行主序）。dtype：SM90 只能 `float`；SM100 还允许 `bfloat16`（但此时 `logits_dtype` 也必须是 `bfloat16`）。
- `cu_seq_len_k_start` / `cu_seq_len_k_end`：均为 `[seq_len]` 的 `int32`，给每个 query token 指定可见 KV 区间 `[start, end)`。它们支持变长与上下文并行（不同 query 行窗口不同）。
- 输出 `logits`：形状 `[seq_len, seq_len_kv]`（full 模式）或 `[seq_len, max_seqlen_k]`（compressed 模式），由 API 内部分配返回。

#### 4.2.2 核心流程

输出分配与两种模式的切换在宿主 API 里完成，关键逻辑是：

1. 算 `block_q = 128 / num_heads`（要求 `128 % num_heads == 0`），把 query 行按 `block_q` 分块喂给 tensor core。
2. 行 stride 必须按 **1024 字节对齐**（TMA 写回要求）：`stride_logits_alignment = 1024 / elementSize(logits_dtype)`。
3. **full 模式**（`max_seqlen_k == 0`）：分配 `[aligned_seq_len, stride]` 再切片到 `[seq_len, seq_len_kv]`；此时允许 `clean_logits=true` 把窗口外的位置刷成 `-inf`。
4. **compressed 模式**（`max_seqlen_k > 0`）：每个 query 行的有效窗口很短，于是把第 `i` 行的有效段紧凑写到列 `[0, end-start)`，输出变成 `[seq_len, max_seqlen_k]`，省掉大段 `-inf` 存储；此模式下**禁止** `clean_logits`（已天然紧凑，没有空洞要清）。

`cu_seq_len` 的作用贯穿调度：调度器为一个 query 块（`BLOCK_Q` 行）取这 `BLOCK_Q` 个 `[start,end)` 的**最小 start 与最大 end**，决定要加载的 KV 范围；同时把每个 token 的 `start/end` 存进共享内存，供 math 线程在 compressed 模式下判断「这个 KV token 是否落在该 query 行的有效窗口内」。

#### 4.2.3 源码精读

宿主侧输出分配与两种模式：

- [attention.hpp:L159-L178](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/attention.hpp#L159-L178)：`block_qh=128`、`block_kv=256` 常量；`max_seqlen_k==0` 走 full、否则走 compressed（末行 `DG_HOST_ASSERT(not clean_logits)` 锁死 compressed 下不能 clean）。
- [attention.hpp:L166-L172](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/attention.hpp#L166-L172)：1024 字节对齐的 stride 计算与切片返回。

`cu_seq_len` 的设备侧翻译（调度器）：

- [scheduler/sm100_mqa_logits.cuh:L42-L63](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/scheduler/sm100_mqa_logits.cuh#L42-L63)：`next_q_block` 遍历一个 query 块的 `BLOCK_Q` 行，逐行读 `cu_seq_len_k_start/end`，取 `min(start)` 与 `max(end)` 得到本块要加载的 KV 范围 `kv_token_base`（还对齐到 4，服务 compressed 写回）与 `num_kv_splits`。
- [scheduler/sm100_mqa_logits.cuh:L77-L81](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/scheduler/sm100_mqa_logits.cuh#L77-L81)：`get_logits_col` 把 math 线程号映射成 KV 列坐标 `kv_token_base + split*SPLIT_KV + math_thread_idx`。
- [scheduler/sm100_mqa_logits.cuh:L69-L71](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/scheduler/sm100_mqa_logits.cuh#L69-L71)：`get_kv_tma_offset` 给 TMA 加载提供 KV 段起始偏移。

compressed 写回的设备逻辑（窗口判断）：

- [sm100_mqa_logits.cuh:L488-L497](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_mqa_logits.cuh#L488-L497)：`kIsCompressedLogits` 为真时算 `rel_kv = kv_offset - seq_k_start[i]`，仅当 `rel_kv < len` 才写到 `logits[q_offset + rel_kv]`；否则直接写到绝对列 `logits[q_offset + kv_offset]`（full 模式）。

#### 4.2.4 代码实践

**目标**：理解 `cu_seq_len_k_start/end` 如何把「每行不同的可见窗口」变成一次 kernel launch。

**步骤**：

1. 读 [scheduler/sm100_mqa_logits.cuh:L42-L63](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/scheduler/sm100_mqa_logits.cuh#L42-L63)，回答：一个 query 块里若 8 行的窗口分别是 `[0,100),[0,200),…,[0,800)`，调度器会让该块加载哪段 KV？
2. 读 `tests/test_attention.py` 里的 `generate_ks_ke_tests`（[test_attention.py:L119-L134](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/tests/test_attention.py#L119-L134)），看它在 `disable_cp=True/False` 两种情况下如何构造 `ks/ke`，理解 CP（上下文并行）场景下窗口的含义。
3. 对照参考实现 `ref_fp8_mqa_logits` 的 `mask = (cols >= cu_seqlen_ks) & (cols < cu_seqlen_ke)`（[test_attention.py:L104-L111](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/tests/test_attention.py#L104-L111)），确认窗口外位置在参考实现里被填 `-inf`，这正是 `clean_logits` 要复刻的行为。

**需要观察的现象**：调度器用「块内 min-start / max-end」保证不漏算任何有效 token，代价是块内某些行会多算一点窗口外的 KV（靠写回时的窗口判断或后续 `-inf` 清理丢弃）。这是「以少量冗余计算换简单调度」的典型取舍。

**预期结果**：能用一句话说清 `cu_seq_len_k_start[i]` / `cu_seq_len_k_end[i]` 分别是 query 行 `i` 的可见 KV 区间的**左闭端**与**右开端**。

#### 4.2.5 小练习与答案

**Q1**：full 模式下，如果 `clean_logits=false`，输出里窗口外的位置会是什么值？
**答**：是**未初始化的垃圾值**（`torch::empty` 分配未清零）。只有 `clean_logits=true` 才会调 `smxx_clean_logits` 把它们刷成 `-inf`。所以 full 模式下若下游要做 softmax，务必开 `clean_logits`。

**Q2**：为什么 compressed 模式禁止 `clean_logits`？
**答**：compressed 模式把每行有效段紧凑排在 `[0, end-start)`，输出形状 `[seq_len, max_seqlen_k]` 里**没有空洞**需要清理；窗口判断已在写回时完成（4.2.3 第三条链接），再 clean 反而会破坏紧凑布局。

**Q3**：`weights.stride(1) == 1` 这个断言（[attention.hpp:L145](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/attention.hpp#L145)）要求 weights 是什么布局？
**答**：要求最后一维（`num_heads` 维）连续、即行主序，这样 TMA 才能按 `[seq_len, num_heads]` 的 2D 瓦片连续加载。

### 4.3 架构与 dtype 派发

#### 4.3.1 概念说明

同一个 Python 入口 `fp8_fp4_mqa_logits`，背后有**三条**实现路径，由两个开关决定：

1. `device_runtime->get_arch_major()`：`9`（Hopper/SM90）或 `10`（Blackwell/SM100）——全库派发的核心开关（复习 [u4-l1](u4-l1-device-runtime-config.md)）。
2. `is_fp4`：由 `q_sf.has_value()` 判断——query 带缩放因子就是 FP4，否则 FP8。

派发矩阵是：

| arch_major | is_fp4 | 走哪条路径 | 备注 |
| --- | --- | --- | --- |
| 9（SM90） | false（FP8） | `sm90_fp8_mqa_logits` | SM90 **只支持 FP8**。 |
| 9（SM90） | true（FP4） | `DG_HOST_UNREACHABLE` | SM90 不支持 FP4。 |
| 10（SM100） | false（FP8） | `sm100_mqa_logits`（`is_fp4=false`） | — |
| 10（SM100） | true（FP4） | `sm100_mqa_logits`（`is_fp4=true`） | 仅 SM100 支持。 |

即 **FP4 是 SM100 独占**，SM90 只有 FP8 一条路。

#### 4.3.2 核心流程

派发发生在宿主 API 的末段（校验完、分配完输出之后）：

1. 先做 dtype/形状校验，且校验本身**就按 arch 分支**：FP4 分支断言 `arch_major == 10`、`num_heads ∈ {8,16,32,64}`、`head_dim ∈ {64,128}`；FP8 分支则用 `(arch==10 and heads∈{8,16,32,64}) or (arch==9 and heads∈{32,64})` 这种「arch 与允许值联动」的断言。
2. 按架构派发：`arch_major==10` 调 `sm100_mqa_logits`（内部再由 `is_fp4` 选 FP4/FP8 设备模板）；`arch_major==9 and not is_fp4` 调 `sm90_fp8_mqa_logits`；其余 `DG_HOST_UNREACHABLE`。
3. 若 `clean_logits` 为真，再统一调 `smxx_clean_logits` 收尾（不分架构，`smxx` 即跨架构通用）。

两条宿主 Runtime 在「线程划分」与「流水线深度」上有明显差异，这是 SM90（WGMMA + 寄存器累加）与 SM100（UMMA + TMEM 累加）编程模型不同的直接体现：

- SM100：`num_specialized_threads=128`（TMA/epilogue 专用）+ `num_math_threads=2*128=256`；KV 流水线 FP4 用 10 级、FP8 用 5 级。
- SM90：`num_specialized_threads=128` + `num_math_threads=512`；Q/KV 流水线各 3 级。

#### 4.3.3 源码精读

派发与校验（宿主 API）：

- [attention.hpp:L180-L190](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/attention.hpp#L180-L190)：三路 `if/else if/else` 派发，末尾 `DG_HOST_UNREACHABLE("Unsupported architecture")`。
- [attention.hpp:L90-L118](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/attention.hpp#L90-L118)：FP4 分支校验，首行 `DG_HOST_ASSERT(arch_major == 10)` 把 FP4 钉死在 SM100。
- [attention.hpp:L119-L140](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/attention.hpp#L119-L140)：FP8 分支校验，`num_heads` 的允许集合随 `arch_major` 联动（SM90 只允许 32/64）。
- [attention.hpp:L192-L194](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/attention.hpp#L192-L194)：`clean_logits` 收尾调用，不分架构。
- [attention.hpp:L440-L445](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/attention.hpp#L440-L445)：pybind11 注册 `fp8_fp4_mqa_logits`，注意默认值 `clean_logits=true`、`max_seqlen_k=0`、`logits_dtype=kFloat32`。

两条宿主 Runtime 的线程/流水线配置：

- [sm100_mqa_logits.hpp:L175-L179](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/sm100_mqa_logits.hpp#L175-L179)：SM100 的 `num_math_threads=2*128`、`num_kv_stages = is_fp4 ? 10 : 5`。
- [sm90_fp8_mqa_logits.hpp:L94-L96](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/sm90_fp8_mqa_logits.hpp#L94-L96)：SM90 的 `num_math_threads=512`、`num_q_stages=3, num_kv_stages=3`。
- [sm100_mqa_logits.hpp:L119-L148](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/sm100_mqa_logits.hpp#L119-L148)：`generate_impl` 用 `fmt::format` 把 `is_fp4/num_heads/head_dim/block_q/split_kv/...` 等编译期常量填进设备模板（JIT 代码生成，复习 [u3-l2](u3-l2-codegen-template-instantiation.md)）。

#### 4.3.4 代码实践

**目标**：把三条路径的约束整理成一张表，确认你能预判某个形状会在哪条路径跑。

**步骤**：

1. 读 [attention.hpp:L90-L140](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/attention.hpp#L90-L140) 与 [attention.hpp:L180-L190](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/attention.hpp#L180-L190)。
2. 自制一张表，列出 `(arch_major, is_fp4, num_heads, head_dim)` 在三路径下的「允许 / 拒绝」。
3. 验证两个边界用例：① SM90 + FP4（应被 FP4 分支首行断言拒绝）；② SM90 + FP8 + `num_heads=8`（应被 FP8 分支的 arch 联动断言拒绝，因为 SM90 只允许 32/64）。

**预期结果**：你能指着代码说出每个用例是被哪一行 `DG_HOST_ASSERT` 挡下的。运行验证为「待本地验证」（需要对应架构 GPU）。

#### 4.3.5 小练习与答案

**Q1**：为什么 SM90 的 FP8 路径只允许 `num_heads ∈ {32,64}`，而 SM100 允许 `{8,16,32,64}`？
**答**：因为 `block_q = 128 / num_heads`，且设备侧对 TMEM load 宽度有约束（[sm100_mqa_logits.cuh:L359-L360](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_mqa_logits.cuh#L359-L360) 列出 SM100 支持 4/8/16/32/64）。SM90 的 WGMMA 编程模型与寄存器累加方式对 head 数的约束更紧，故只开放 32/64。

**Q2**：legacy 接口 `fp8_mqa_logits`（[attention.hpp:L406-L416](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/attention.hpp#L406-L416)）与新版有何区别？
**答**：legacy 版是薄包装，把 `q` 包成 `(q, std::nullopt)`（即固定 FP8）、`logits_dtype` 固定 `kFloat`，转调 `fp8_fp4_mqa_logits`。功能子集相同，只是不支持 FP4 与 bf16 logits。

**Q3**：`weights` 在 SM100 可以是 `bfloat16`，但有个附加断言（[attention.hpp:L147](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/attention.hpp#L147)）是什么？
**答**：`weights.scalar_type() != torch::kBFloat16 or logits_dtype == torch::kBFloat16`——即「用了 bf16 权重就必须也用 bf16 logits」，因为 bf16 权重的归约路径（4.1.3 第二条链接）产出 bf16 精度，强转 float32 没有意义且与硬件路径不符。

### 4.4 设备内核精读：UMMA 产出、加权 ReLU 归约与 clean_logits 收尾

#### 4.4.1 概念说明

前面三节讲了「算什么、数据怎么摆、走哪条路」，本节下钻到 SM100 设备核心 `sm100_mqa_logits_core_impl`，看一次完整计算在 GPU 上怎么铺开。它延续 [u6-l1](u6-l1-sm90-fp8-gemm-1d1d-entry.md) 的「专用 warp 发 TMA、math warp 发 MMA」分工，但这里多了**TMEM** 这个 SM100 专属累加器，以及把逐 head 归约融进 epilogue 的设计。

线程划分：`num_specialized_threads=128`（1 个 warp group）+ `num_math_threads=256`（2 个 warp group）。专用 warp group 又细分为：
- warp `kSpecWarpStart`：发 Q/SF_Q/weights 的 TMA 加载。
- warp `kSpecWarpStart+1`：发 KV/SF_KV 的 TMA 加载。
- warp `kSpecWarpStart+2`：发 UMMA（把 KV×Q 的点积写进 TMEM）；FP4 时还负责把 UE8M0 SF 经 UTCCP 预载入 TMEM。
- 其余（`warp_idx < kSpecWarpStart`）是 math warp：从 TMEM 读点积、做加权 ReLU 归约、写回 logits。

#### 4.4.2 核心流程

设备核心的主循环（每个 SM 持久化，靠 `scheduler.next_q_block` 领 query 块）：

1. **领任务**：所有线程组各自构造 scheduler，`while (scheduler.next_q_block(...))` 领一个 query 块与对应的 KV 加载范围 `kv_base/num_kv_splits`。
2. **喂 Q**：专用 warp 0 用 `tma::copy` 把 `BLOCK_Q×num_heads` 的 Q（及 FP4 的 SF_Q、weights）搬进共享内存，配 `full/empty_q_barriers` 做多级流水线（`RingPipeline`）。
3. **喂 KV**：专用 warp 1 对每个 KV split 发 `tma::copy` 搬 KV 段与 SF_KV；FP4 时 SF_KV 是打包 UE8M0。
4. **算 UMMA**：专用 warp 2 等 KV 就绪后发 `tcgen05.mma`（FP8 用 `SM100_MMA_F8F6F4_SS`、FP4 用 `SM100_MMA_MXF4_SS` 配 block-scaled 描述符），结果落 TMEM 的 `tmem_stage_idx` 段；FP4 先用 UTCCP 把 SF 载入 TMEM 供硬件缩放。
5. **归约写回**：math warp 等 TMEM 就绪，`tmem_load_no_fence` 读出 `accum[num_heads]`（一个 query token 的 H 个点积），按 4.1 的加权 ReLU 公式归约成标量 `reduced`；FP8 再乘 `scale_kv`（逐 KV token 的 FP32 SF），FP4 不乘（已由硬件吸收）；最后按 full/compressed 模式写回 `logits`。

关键常量（决定 MMA 形状）：`UMMA_M=128`、`UMMA_N=BLOCK_Q*num_heads`、`UMMA_K=FP4?64:32`，且 `SPLIT_KV = kNumMathWarpGroups*UMMA_M = 256`。

#### 4.4.3 源码精读

线程划分与常量：

- [sm100_mqa_logits.cuh:L69-L99](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_mqa_logits.cuh#L69-L99)：`sm_idx/warp_idx/warpgroup_idx` 划分，`kSpecWarpStart` 分界，以及 `UMMA_M/N/K`、`SPLIT_KV` 等编译期常量与 `DG_STATIC_ASSERT(SPLIT_KV==kNumMathWarpGroups*UMMA_M ...)`。

FP4 vs FP8 的 MMA 发射（专用 warp 2）：

- [sm100_mqa_logits.cuh:L302-L335](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_mqa_logits.cuh#L302-L335)：FP4 用 `make_instr_desc_block_scaled` + `SM100_MMA_MXF4_SS`（硬件吸收 UE8M0 SF），FP8 用 `make_instr_desc` + `SM100_MMA_F8F6F4_SS`。这是 FP4「硬件缩放」与 FP8「软件缩放」分叉的根因（复习 [u6-l2](u6-l2-mma-wgmma-vs-umma.md)）。

math 侧归约与写回：

- [sm100_mqa_logits.cuh:L407-L427](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_mqa_logits.cuh#L407-L427)：FP8 在这里从共享内存读 `scale_kv`（逐 KV token 的 FP32 SF）；FP4 跳过（`if constexpr (not kIsFP4)`）。注意 [L488](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_mqa_logits.cuh#L488) `result = kIsFP4 ? reduced : reduced * scale_kv` 一行浓缩了两种路径的差异。
- [sm100_mqa_logits.cuh:L446-L449](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_mqa_logits.cuh#L446-L449)：在处理完一个 query 块的最后一个 token 时释放 TMEM stage（`empty_tmem_barriers`），保证流水线推进。

`RingPipeline` 与部分块处理：

- [sm100_mqa_logits.cuh:L24-L38](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_mqa_logits.cuh#L24-L38)：环形缓冲计数器，用「超过 kNumStages 即翻转 phase」避免取模（注释说 ptxas 对 TMEM 路径的取模优化较差）。
- [sm100_mqa_logits.cuh:L41-L50](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_mqa_logits.cuh#L41-L50)：`dispatch_num_block_tokens` 把运行时的有效 token 数编译期化（递归模板展开成 `cute::Int<N>`），让 token 循环保持编译期常量。

`clean_logits` 收尾（独立的小 kernel）：

- [smxx_clean_logits.cuh:L20-L26](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/smxx_clean_logits.cuh#L20-L26)：构造一块填满 `-inf` 的共享内存，作为「批量刷 `-inf`」的源。
- [smxx_clean_logits.cuh:L48-L70](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/smxx_clean_logits.cuh#L48-L70)：对每个 query 行，用 `cu_seq_len_k_start/end` 算出有效窗口 `[ks,ke)`，把窗口**外**的整块用 TMA `SM90_BULK_COPY_S2G` 从 `-inf` smem 拷过去，窗口边缘未对齐的零头用逐元素写补齐。宿主侧入口见 [smxx_clean_logits.hpp:L51-L79](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/smxx_clean_logits.hpp#L51-L79)。

#### 4.4.4 代码实践

**目标**：跟踪「TMA 加载 → mbarrier 握手 → UMMA → TMEM 读 → 归约写回」这一条同步链，并理解 `clean_logits` 的覆盖范围。

**步骤**：

1. 在 [sm100_mqa_logits.cuh:L147-L166](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_mqa_logits.cuh#L147-L166) 找到专用 warp 0 发 Q 的 `tma::copy` 与随后的 `arrive_and_expect_tx`，再到 math 侧 [L383-L384](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_mqa_logits.cuh#L383-L384) 的 `full_q_barriers.wait`，确认生产者-消费者握手（复习 [u6-l3](u6-l3-ptx-tma-and-barriers.md) 的相位翻转子机制）。
2. 对比 FP4 与 FP8 的 SF 处理：FP4 在 [L255-L269](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_mqa_logits.cuh#L255-L269) 做 `utccp_required_smem_warp_transpose` 后用 UTCCP 载入 TMEM；FP8 在 [L414-L419](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_mqa_logits.cuh#L414-L419) 直接从共享内存读 `scale_kv` 软件相乘。
3. 读 [smxx_clean_logits.cuh:L42-L59](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/smxx_clean_logits.cuh#L42-L59)，画出一行 logits 被分成哪几段：`[0,ks)` 全 `-inf`、`[ks,ke)` 保留、`[ke,seq_len_kv)` 全 `-inf`。

**需要观察的现象**：`clean_logits` kernel 的主体是「把窗口外的连续大段用 TMA 整块刷 `-inf`，只在窗口边界做逐元素补齐」，这是典型的「用 TMA 批量写换吞吐」优化。

**预期结果**：能解释为什么 `clean_logits` 要等到主 kernel 结束后才跑（它是独立 launch 的收尾 kernel，靠 `cudaGridDependencySynchronize` 等待主 kernel 完成，见 [smxx_clean_logits.cuh:L40](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/smxx_clean_logits.cuh#L40)）。

#### 4.4.5 小练习与答案

**Q1**：FP4 路径里，`scale_kv`（逐 KV token 的 SF）是怎么参与计算的？
**答**：FP4 不在软件里乘 `scale_kv`。它的 UE8M0 SF 经 UTCCP 预载入 TMEM，由 `SM100_MMA_MXF4_SS` 这条 block-scaled UMMA 指令在硬件里直接吸收（[L302-L322](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_mqa_logits.cuh#L302-L322)），所以 [L488](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_mqa_logits.cuh#L488) 是 `kIsFP4 ? reduced : reduced * scale_kv`。

**Q2**：`RingPipeline`（[L24-L38](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_mqa_logits.cuh#L24-L38)）为什么用「减法 + 翻转 phase」而不是 `stage_idx % kNumStages`？
**答**：注释明说——`ptxas` 对 TMEM 路径的取模（`%`）lowering 较差，用比较+减法+异或翻转可以避免取模，提升 TMEM 流水线性能。

**Q3**：为什么 KV pipeline 深度 FP4 用 10 级、FP8 用 5 级（[sm100_mqa_logits.hpp:L179](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/sm100_mqa_logits.hpp#L179)）？
**答**：注释说「用能放得下且留有余量的最深 KV pipeline」。FP4 每元素只占 0.5 字节（packed e2m1），同样共享内存能容纳更多级；FP8 每元素 1 字节，故级数更少。两者最终都用 `smem_size <= SM100ArchSpec::smem_capacity` 兜底校验。

## 5. 综合实践

把本讲的知识串起来，完成一次「从数学到调用」的端到端验证。推荐**直接复用项目自带的测试脚手架**（无需自己造数据）：

1. 打开 `tests/test_attention.py`，定位 `ref_fp8_mqa_logits`（[L84-L113](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/tests/test_attention.py#L84-L113)）与 `test_mqa_logits`（[L116-L245](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/tests/test_attention.py#L116-L245)）。
2. 对照参考实现，确认它做的就是 4.1 的加权 ReLU 公式：`score = einsum('mhd,nd->hmn', q, k)` → `relu` → `einsum('hmn,hm->mn', ...)`（[L106-L107](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/tests/test_attention.py#L106-L107)），并用 `cu_seq_len` 做 mask 填 `-inf`（[L109-L110](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/tests/test_attention.py#L109-L110)）。
3. 看 `test_mqa_logits` 如何枚举 `(is_fp4, logits_dtype, weights_dtype, compressed_logits, clean_logits, seq_len, seq_len_kv, num_heads, head_dim, disable_cp)`（[L136-L154](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/tests/test_attention.py#L136-L154)），把这组参数与本讲 4.3 的派发表对照，预判每个用例走哪条路径。
4. 在具备 SM90/SM100 GPU 的机器上运行（「待本地验证」）：
   ```bash
   cd tests && python test_attention.py
   ```
   或只跑少量用例：`DG_MQA_NUM_CASES=4 python test_attention.py`。
5. 观察输出的校验断言（`assert_bitwise_equal` 自一致性、`calc_diff < 1e-3`/`0.02`）与性能行（TFLOPS / GB/s / relu/cyc/SM）。

**验收标准**：能口述「给定 `(arch_major=10, is_fp4=True, num_heads=64, head_dim=128, seq_len=2048, seq_len_kv=65536, clean_logits=True, max_seqlen_k=0)`，这次调用会走 `sm100_mqa_logits` 的 FP4 模板、输出 `[2048, 65536]`、并在主 kernel 后跑一次 `smxx_clean_logits` 把窗口外刷成 `-inf`」。

> 提示：若手头没有 GPU，可只做 1–3 步的源码阅读型实践——把 `ref_fp8_mqa_logits` 当作「可运行的数学定义」，逐行对应到本讲 4.1–4.4 的源码链接，同样能完成知识闭环。

## 6. 本讲小结

- **加权 ReLU MQA logit** 的数学定义是 \(\text{out}[i,j]=\sum_h w[i,h]\,\mathrm{ReLU}(q[i,h]\cdot\widetilde{kv}[j])\)，与 softmax attention 的根本区别是 ReLU（非归一化）+ 跨 head 折叠成标量，因而逐 \((i,j)\) 独立可并行。
- **输入输出布局**：`q=[S,H,D]`、`kv=[Skv,D]`（MQA 共享）、`weights=[S,H]`、`cu_seq_len_k_*=[S]` 给每行一个可见窗口；输出 full 模式 `[S, Skv]`（可 `clean_logits`）、compressed 模式 `[S, max_seqlen_k]`（禁 `clean_logits`）。
- **架构/dtype 派发**：FP4 是 SM100 独占；SM90 仅 FP8 且 `num_heads∈{32,64}`；SM100 允许 FP8/FP4 且 `num_heads∈{8,16,32,64}`。派发由 `arch_major` 与 `is_fp4=q_sf.has_value()` 双开关驱动。
- **设备核心**（SM100）：专用 warp 组发 TMA 与 UMMA，点积落 TMEM；math warp 读 TMEM 做加权 ReLU 归约；FP4 走 block-scaled UMMA 硬件吸收 SF、FP8 走软件乘 `scale_kv`。
- **clean_logits 收尾**：独立 kernel，用填满 `-inf` 的共享内存 + TMA 批量拷贝，把每个 query 行窗口外的位置刷成 `-inf`，仅 full 模式可用。

## 7. 下一步学习建议

- 继续 [u9-l2 分页 MQA logits](u9-l2-paged-mqa-logits.md)：学习 `fp8_fp4_paged_mqa_logits` 如何在解码阶段基于 paged KV cache（`fused_kv_cache + block_table`）做同样的评分，以及 `get_paged_mqa_logits_metadata` 生成的 SM 调度元数据与 split-K（`split_kv=256`）机制。
- 回看 [u6-l2 MMA 抽象：WGMMA vs UMMA](u6-l2-mma-wgmma-vs-umma.md)：本讲的 `make_instr_desc_block_scaled` / `SM100_MMA_MXF4_SS` 与 UTCCP 正是 SM100「硬件吸收 UE8M0 SF」的具体应用，对照阅读会加深理解。
- 若对加权 ReLU 归约的数值边界感兴趣，可读 [u10-l3 测试、基准与数值校验](u10-l3-testing-benchmark-numeric.md)，看 `calc_diff` 的阈值为何 FP4 放宽到 `0.02`、FP8 收紧到 `1e-3`。
