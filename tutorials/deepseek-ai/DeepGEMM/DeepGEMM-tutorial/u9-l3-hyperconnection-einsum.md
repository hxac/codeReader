# HyperConnection 与 Einsum 内核

> 本讲是「其它内核家族」单元的第三篇。在 GEMM / MoE / MQA 之外，DeepGEMM 还内置了两类「为特定模型结构量身定制」的辅助内核：HyperConnection 预归一化 GEMM，以及一批硬编码的 Einstein 求和（einsum）。它们看似零散，实则都遵循同一套规则——**把一个看起来不是 GEMM 的运算，通过 permute（换轴）或额外的旁路输出，规约成 tensor core 最擅长的 (batch, m, n, k) 矩阵乘**。

## 1. 本讲目标

学完本讲，你应当能够：

1. 说清 `tf32_hc_prenorm_gemm` 做了什么：除了算一次 GEMM，它为何还要顺带吐出一个 `sqr_sum`，以及 `num_splits`（K 轴分裂）如何把大问题切成多份并行输出。
2. 看懂 `einsum` / `fp8_einsum` 里三条硬编码表达式（`bmk,bnk->mn`、`bhr,hdr->bhd`、`bhd,hdr->bhr`、`bhd,bhr->hdr`）分别对应什么形状的矩阵乘。
3. 解释 `fp8_einsum` 如何用零拷贝 `.permute()` 把输入张量重排成 `fp8_bmm` 要求的 `(batch, m, n, k)` 顺序，并由此推导出「为什么有的表达式只有 SM100 支持」。

## 2. 前置知识

本讲依赖你已经在 u2-l3 建立的认知：DeepGEMM 的 API 层是「**校验 → early_return → 变换 SF → 按 `device_runtime->get_arch_major()` 派发**」四步范式。这里补充两个本讲要用的小概念。

- **TF32（TensorFloat-32）**：Hopper/Blackwell 的 tensor core 提供的一种「加速版 FP32」数据类型。它把 FP32 输入截断到 1 位符号 + 8 位指数 + **10 位尾数**（共 19 位，类似 BF16 的尾数宽度）再做乘加，从而把 FP32 的吞吐量提升到接近 FP16/BF16。SM90 的 WGMMA 有专门的 `mma.sync.aligned.m64n*.k8.f32.tf32.tf32.f32` 指令族。本讲内核名里的 `tf32` 就指它用 TF32 精度做 MMA。
- **permute 是视图不是拷贝**：PyTorch 的 `tensor.permute(dims)` 只改 `stride` 与逻辑形状、**不搬动数据**。DeepGEMM 大量利用这一点：只要换轴后的「主维」（stride 为 1 的那一维）落在 tensor core 需要的位置，就能直接把一个看似形状不对的张量喂进 GEMM，零拷贝。

> 名词速查：本讲反复出现的 `(batch, m, n, k)` 是 `fp8_bmm` 内部对所有输入统一理解的四元角色——**batch** 是「并行、互不干扰」的批次维，**m/n** 是输出的两个维，**k** 是被求和（收缩）掉的维。把任意 einsum 对应到这四个角色，是理解本讲的关键。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [csrc/apis/hyperconnection.hpp](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/hyperconnection.hpp) | HyperConnection 的 Python↔C++ 入口，校验形状/类型/主维后按架构派发 |
| [csrc/apis/einsum.hpp](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/einsum.hpp) | einsum / fp8_einsum 入口，包含硬编码表达式表与 permute→fp8_bmm 的归约逻辑 |
| [csrc/jit_kernels/impls/sm90_tf32_hc_prenorm_gemm.hpp](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/sm90_tf32_hc_prenorm_gemm.hpp) | SM90 宿主 Runtime：算 block 配置、stages、TMA 描述符、构造 split-K 的 grid |
| [csrc/jit_kernels/impls/sm100_tf32_hc_prenorm_gemm.hpp](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/sm100_tf32_hc_prenorm_gemm.hpp) | SM100 宿主 Runtime：与 SM90 同构，但放开了 N 上限、改用 MMA+cast/reduce 线程划分 |
| [deep_gemm/include/deep_gemm/impls/sm90_tf32_hc_prenorm_gemm.cuh](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm90_tf32_hc_prenorm_gemm.cuh) | SM90 设备 kernel：一边 WGMMA 累加 D，一边在加载 A 时顺手累加 `sqr_sum` |
| [csrc/jit_kernels/impls/sm90_bf16_gemm.hpp](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/sm90_bf16_gemm.hpp) | BF16 einsum 的 `bhr/hdr` 实现把 head 维映射成 batch（num_groups） |
| [tests/test_einsum.py](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/tests/test_einsum.py) | 各表达式的正确性与性能测试，是本讲代码实践的主要依据 |

## 4. 核心概念与源码讲解

### 4.1 HyperConnection 预归一化 GEMM

#### 4.1.1 概念说明

HyperConnection（HC）是 DeepSeek 模型里的一类结构。它的一步典型计算是：先做一次线性变换 \(D = A \cdot B^\top\)，再对结果按行做「预归一化」（类似 RMSNorm 的思想），即用到每一行 A 的平方和 \( \sum_k A_{m,k}^2 \)。

DeepGEMM 的 `tf32_hc_prenorm_gemm` 把这两件事**融合进同一个 kernel**：

\[ D[m,n] = \sum_{k} A[m,k] \cdot B[n,k] \]
\[ \text{sqr\_sum}[m] = \sum_{k} A[m,k]^2 \]

- \(A\) 是 BF16，\(B\) 是 FP32，\(D\) 与 `sqr_sum` 都是 FP32。MMA 用 **TF32** 精度（\(A\) 在核内转成 FP32 再喂 WGMMA），这就是名字里 `tf32` 的由来。
- `sqr_sum` 是一个**旁路输出**：它和 D 无关、却必须和 D 同时算出来，否则 HC 的归一化就缺数据。融合它的好处是「**反正 A 已经被搬进寄存器了，顺手平方累加几乎免费**」。
- `num_splits` 是 **K 轴分裂**：当 M 很大、单个 SM 算不完时，把 K 轴切成 `num_splits` 段，每段独立产出一份「部分 D」和「部分 sqr_sum」，由调用方在后续做一次跨 split 的归约。

#### 4.1.2 核心流程

1. **校验**：A、B 必须 K-major；D 必须 N-major；`sqr_sum` 连续；形状自洽（带 `num_splits` 时 D 形状为 `[num_splits, m, n]`、`sqr_sum` 为 `[num_splits, m]`）。
2. **算配置**：`block_m=64`、`block_k=64` 写死；`block_n = align(n, 16)`；按架构约束 N（SM90 \(n \le 32\)，SM100 \(n \le 128\)）。
3. **算 stages**：从 12 起往下减，直到共享内存装得下（A/B 各 `num_stages` 份 + CD + barriers）。
4. **构造 TMA 描述符**：`num_splits==1` 时 D 用 2D 描述符；否则用 **3D 描述符**，把 split 维折进外维，让一个 kernel 同时覆盖所有 split。
5. **构造 grid**：`grid = num_splits * ceil_div(m, block_m)`——每个 block 负责某段 K、某段 M 的 tile，写回 `D[split, m, n]` 与 `sqr_sum[split, m]`。
6. **JIT 生成 + 启动**：宿主 `generate_impl` 把 block 配置填进设备模板，编译、加载、`launch`。

带 `num_splits` 时的整体语义（split-K，需调用方归约）：

\[ D_{\text{final}}[m,n] = \sum_{s=0}^{S-1} D[s,m,n], \qquad \text{sqr\_sum}_{\text{final}}[m] = \sum_{s=0}^{S-1} \text{sqr\_sum}[s,m] \]

#### 4.1.3 源码精读

API 入口校验四件套（主维、连续性、类型、形状）后按 `arch_major` 派发：

[csrc/apis/hyperconnection.hpp:13-58](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/hyperconnection.hpp#L13-L58) —— `tf32_hc_prenorm_gemm` 的全部宿主逻辑。其中 26-43 行是形状/类型断言（带与不带 `num_splits` 两套），50-57 行是「9 → sm90 / 10 → sm100」的派发，与 u2-l3 的范式一致。

SM90 宿主把 block 配置、stages、3D/2D 描述符、split-K grid 一次性打包：

[csrc/jit_kernels/impls/sm90_tf32_hc_prenorm_gemm.hpp:66-150](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/sm90_tf32_hc_prenorm_gemm.hpp#L66-L150)。要点：
- 72-82 行：`block_m=block_k=64` 写死，`block_n=align(n,16)`，断言 SM90 上 \(n \le 32\)（HC 这一步本就是「瘦高」的小 N 线性层）。
- 95-103 行：`num_splits==1` 时 D 用 2D `make_tma_cd_desc`，否则用 3D `make_tma_3d_desc`（外维 = split 数）。
- 133-141 行：`launch_args = LaunchArgs(num_splits * ceil_div(m, block_m), num_threads, smem_size)`——split 维直接乘进 grid。

设备 kernel 的签名与 split 索引推导：

[deep_gemm/include/deep_gemm/impls/sm90_tf32_hc_prenorm_gemm.cuh:41-46](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm90_tf32_hc_prenorm_gemm.cuh#L41-L46) —— kernel 入口，模板参数含 `kNumSplits`。107-115 行把线性 `block_idx` 拆成 `m_block_idx = block_idx / kNumSplits` 与 `k_split_idx = block_idx % kNumSplits`，并据此算出本 block 负责的 K 偏移 `k_offset` 与 M 偏移 `m_offset = shape_m * k_split_idx`（sqr_sum 写入用的段基址）。

`__global__` 内存里 `sqr_sum` 是「顺带」算出来的——在把 BF16 的 A 装载进寄存器、转成 FP32 的同一个循环里，把每个元素的平方累加到 `sqr_sum_acc_*`：

[deep_gemm/include/deep_gemm/impls/sm90_tf32_hc_prenorm_gemm.cuh:179-203](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm90_tf32_hc_prenorm_gemm.cuh#L179-L203)。201-202 行 `sqr_sum_acc_0 += a.x*a.x + ...` 就是融合点：A 的数据此时已在寄存器里，平方累加几乎不增加指令。233-242 行在 K 循环结束后，把 `sqr_sum_acc` 跨 warp 归约（`warp_reduce_sum<4>`），按 `m_offset + m_idx` 写回全局 `sqr_sum`。277-284 行则是 D 的 TMA store：`kNumSplits==1` 走 2D store，否则带 split 维走 3D store。

> SM100 的宿主结构几乎一致，差别只在 N 上限放宽到 128、线程划分改成 `num_mma_threads + num_cast_and_reduce_threads`：[csrc/jit_kernels/impls/sm100_tf32_hc_prenorm_gemm.hpp:72-80](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/sm100_tf32_hc_prenorm_gemm.hpp#L72-L80)。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：确认 `num_splits` 的「split-K、需调用方归约」语义。
2. **步骤**：
   - 打开 [sm90_tf32_hc_prenorm_gemm.cuh:107-115](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm90_tf32_hc_prenorm_gemm.cuh#L107-L115)，记录 `k_offset`、`m_offset` 的公式。
   - 跟到 233-242 行，确认 `sqr_sum` 写到 `sqr_sum[m_offset + m_idx]`，即按 split 段错开。
3. **观察**：`num_splits=2` 时，第 0 段写 `sqr_sum[0..m)`，第 1 段写 `sqr_sum[m..2m)`；D 同理写到 `D[0]` 与 `D[1]` 两个 split 切片。
4. **预期**：要拿到最终结果，调用方必须再做一次 `D.sum(0)` 与 `sqr_sum.sum(0)`——**待本地验证**（仓库内无 HC 专用测试，需自行在 SM90 上构造 `[m,k]` BF16 的 A 与 `[n,k]` FP32 的 B 调用 `deep_gemm.tf32_hc_prenorm_gemm` 并对照 `torch.matmul(A.float(), B.float().T)` 与 `(A.float()**2).sum(-1)`）。

#### 4.1.5 小练习与答案

**Q1**：为什么 `sqr_sum` 不另开一个 kernel、而要塞进 GEMM kernel？
**A**：因为 \(A\) 的元素必须先搬进寄存器才能喂 WGMMA，平方累加在寄存器层几乎零成本；另开 kernel 要把 \(A\) 重新从显存读一遍，白白多一趟访存。

**Q2**：`num_splits=1` 与 `num_splits>1` 时 D 的形状与 TMA 描述符有何不同？
**A**：前者 D 是 `[m,n]`、用 2D 描述符；后者 D 是 `[num_splits,m,n]`、用 3D 描述符，split 维折进外维，一次 launch 覆盖所有 split，且输出是「部分和」需调用方归约。

---

### 4.2 硬编码 einsum（BF16 路径）

#### 4.2.1 概念说明

`deep_gemm.einsum(expr, a, b, d, ...)` 不是通用的 einsum 引擎——它**硬编码了三种表达式**，每种都对应一种 tensor core 友好的矩阵乘。源码注释也坦白写着 `// TODO: support any expression`。

| 表达式 | 数学含义 | 对应的 GEMM 角色 |
| --- | --- | --- |
| `bmk,bnk->mn` | \(D[m,n] = \sum_{s,k} A[s,m,k]\,B[s,n,k]\) | 把 batch \(s\) 与 \(k\) 一起收缩掉，是「跨 batch 归约」 |
| `bhr,hdr->bhd` | \(D[b,h,d] = \sum_{r} A[b,h,r]\,B[h,d,r]\) | 批次 GEMM：batch=\(h\), m=\(b\), n=\(d\), k=\(r\) |
| `bhd,hdr->bhr` | \(D[b,h,r] = \sum_{d} A[b,h,d]\,B[h,d,r]\) | 批次 GEMM：batch=\(h\), m=\(b\), n=\(r\), k=\(d\) |

后两条是注意力/投影里极常见的「按 head 批次的矩阵乘」。

#### 4.2.2 核心流程

1. `einsum()` 按字符串 `expr` 走 if-else 派发到 `bmk_bnk_mn` / `bhr_hdr_bhd` / `bhd_hdr_bhr` 三个函数。
2. `bmk,bnk->mn`：要求 BF16 输出时用一份临时 FP32 workspace，先 `memset(0)` 再累加（因为跨 batch 归约需要 FP32 累加器），最后隐式转回 BF16；FP32 输出时要求 `c` 与 `d` 同址（原地累加）。底层是专用 split-K kernel `sm{90,100}_bmn_bnk_mn_gemm`。
3. `bhr,hdr->bhd` / `bhd,hdr->bhr`：把 **head 维映射成 batch（`num_groups=h`）**，A/B/D 都用 **3D TMA 描述符**——head 维折进描述符外维，**不做数据 permute**，零拷贝。可选择 `use_cublaslt=True` 走 cuBLASLt 参考路径。

#### 4.2.3 源码精读

派发表：

[csrc/apis/einsum.hpp:107-135](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/einsum.hpp#L107-L135) —— `einsum()` 用字符串匹配三条表达式，其余一律 `DG_HOST_UNREACHABLE`。

`bmk,bnk->mn` 的 FP32/BF16 双路：

[csrc/apis/einsum.hpp:23-59](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/einsum.hpp#L23-L59)。26-40 行是关键：FP32 输出要求 `c->data_ptr()==d.data_ptr()`（原地累加，直接喂给 kernel 的 C/D 同址假设）；BF16 输出则分配 FP32 workspace、`cudaMemsetAsync` 清零、递归调用自身做 FP32 累加、最后 `d.copy_(workspace)` 隐式降精度。

`bhr,hdr->bhd` 把 head 维当 batch：

[csrc/jit_kernels/impls/sm90_bf16_gemm.hpp:329-379](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/sm90_bf16_gemm.hpp#L329-L379)。334-344 行构造 `GemmDesc`：`gemm_type=Batched`、`m=b, n=d, k=r, num_groups=h`——head 维就是 batch。348-362 行对 A/B/D 各建一个 `make_tma_3d_desc`，第三维（外维）是 head，**完全靠 TMA 描述符寻址，不拷贝数据**。

> 对照 cuBLASLt 参考实现：[csrc/jit_kernels/impls/smxx_cublaslt.hpp:122-134](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/smxx_cublaslt.hpp#L122-L134) 用 `lda/stride` 把 head 维编码成 cuBLASLt 的 batch stride，思路同源。

#### 4.2.4 代码实践（可运行）

1. **目标**：跑通 BF16 einsum 三条表达式，观察 TFLOPS 与相对 cuBLASLt 的加速比。
2. **步骤**：在装好 DeepGEMM 的 SM90/SM100 机器上执行 `python tests/test_einsum.py`。
3. **观察**：`test_bhr_hdr_bhd` 会打印 `t_cublaslt / t` 比值（见 [tests/test_einsum.py:39-58](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/tests/test_einsum.py#L39-L58)）；`test_bmk_bnk_mn` 会打印不同 batch `s` 下的 TFLOPS（[tests/test_einsum.py:16-36](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/tests/test_einsum.py#L16-L36)）。
4. **预期**：DeepGEMM 自研 kernel 相对 cuBLASLt 通常 ≥1.0×（比值越小越快）；若机器无 GPU，则此项**待本地验证**，可改为阅读测试断言 `calc_diff(z, ref_z) < 1e-10` 理解精度要求。

#### 4.2.5 小练习与答案

**Q1**：`bhr,hdr->bhd` 里 head 维（h）既不是 m、n 也不是 k，它去了哪？
**A**：它成了 batch（`num_groups=h`），被编码进 3D TMA 描述符的外维与 cuBLASLt 的 batch stride，每个 head 独立做一次 `[b,r]@[d,r].T->[b,d]` 的小 GEMM。

**Q2**：为什么 `bmk,bnk->mn` 的 BF16 输出要先 memset 一份 FP32 workspace？
**A**：因为它要把 batch `s` 与 `k` 一起收缩，累加次数多，BF16 精度不够；必须用 FP32 累加器，算完再隐式转回 BF16。FP32 输出则直接原地累加（`c==d`）。

---

### 4.3 permute 归约到 BMM（fp8_einsum）

> 这是本讲的核心模块，也是规格指定的代码实践重点。

#### 4.3.1 概念说明

FP8 版本的 `fp8_einsum(expr, a=(tensor,sf), b=(tensor,sf), d, ...)` 同样硬编码表达式，但它**不复用 BF16 那套 head-as-batch 的 3D 描述符 kernel**，而是统一归约到一个底层 FP8 批次矩阵乘 `fp8_bmm`。`fp8_bmm` 只认一种角色顺序：

\[ \text{fp8\_bmm}:\ [B,M,K] \times [B,N,K]^\top \to [B,M,N] \]

于是 `fp8_einsum` 的全部工作就是：**对每条表达式，用零拷贝 `.permute()` 把 A、B、D（以及它们的 SF）重排成 `(batch, m, n, k)` 这套角色**，再交给 `fp8_bmm`。重排只是改 stride，不搬数据。

`fp8_bmm` 的输入约定见 [csrc/apis/einsum.hpp:137-177](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/einsum.hpp#L137-L177)：143-148 行断言 A/B 至少有一个「主维」（`stride(-1)==1` 或 `stride(-2)==1`），D 必须 N-major；164-166 行对 SF 做 `transform_sf_pair_into_required_layout`（与 u2-l2 一致）；169-176 行按架构派发——SM100 走 `sm100_fp8_bmm`（支持任意主维与 UE8M0），SM90 走 `sm90_fp8_bmm` 且**强制 `gran_k==128` 且隐式要求 K-major**。

#### 4.3.2 核心流程：三条表达式的 permute 映射

下表把每条表达式的字母维度对号入座到 `(batch, m, n, k)`，并给出 permute 后的张量形状（源码注释里写得很清楚）：

| 表达式 | 架构 | (batch, m, n, k) | A 原状 → permute 后 | B 原状 → permute 后 | D 原状 → permute 后 | compiled_dims |
| --- | --- | --- | --- | --- | --- | --- |
| `bhr,hdr->bhd` | SM90 & SM100 | (h, b, d, r) | `[b,h,r]→{1,0,2}→[h,b,r]` | `[h,d,r]`（不动） | `[b,h,d]→{1,0,2}→[h,b,d]` | `"nk"` |
| `bhd,hdr->bhr` | **仅 SM100** | (h, b, r, d) | `[b,h,d]→{1,0,2}→[h,b,d]` | `[h,d,r]→{0,2,1}→[h,r,d]` | `[b,h,r]→{1,0,2}→[h,b,r]` | `"nk"` |
| `bhd,bhr->hdr` | **仅 SM100** | (h, d, r, b) | `[b,h,d]→{1,2,0}→[h,d,b]` | `[b,h,r]→{1,2,0}→[h,r,b]` | `[h,d,r]`（不动） | `"mn"` |

派发源码：[csrc/apis/einsum.hpp:179-214](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/einsum.hpp#L179-L214)。

#### 4.3.3 源码精读：为什么只有部分表达式在 SM100 支持

关键在 187 行与 195、204 行的 `arch_major == 10` 守卫。

[csrc/apis/einsum.hpp:187-214](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/einsum.hpp#L187-L214)：

- **`bhr,hdr->bhd`（187 行，无架构守卫）**：permute 后 A 的 K 维仍是 `r`（最后维、连续 → K-major），B 的 K 维也是 `r`（`[h,d,r]` 原本就 r 连续 → K-major）。两个操作数都 K-major，**SM90 的 FP8 WGMMA（强制 K-major）也吃得下**，所以两代架构都支持。
- **`bhd,hdr->bhr`（195 行，`arch_major==10`）**：B 从 `[h,d,r]` 经 `permute({0,2,1})` 变成 `[h,r,d]` 的视图。原连续维是 `r`（stride 1），换轴后最后维变成 `d`、其 stride 是原来的 `r`（≠1），而倒数第二维 `r` 的 stride 才是 1——即 B 变成了 **MN-major**。SM90 的 FP8 不支持 MN-major 操作数（见 u2-l1 的 `fp8_requires_k_major()==(arch_major==9)`），只有 SM100 的 UMMA 放宽了这一限制，故仅 SM100。
- **`bhd,bhr->hdr`（204 行，`arch_major==10`）**：把 batch `b` 当成被收缩的 K 维（`k=b`），是典型的权重梯度（wgrad）形态，且 `compiled_dims="mn"`（特化权重维 d、r，保留变化的 b 为运行时）。这种「收缩 token 维 + 任意 recipe」的路径只有 SM100 的 `sm100_fp8_bmm` 支持；SM90 的 `sm90_fp8_bmm` 还硬断言 `gran_k==128`（[csrc/apis/einsum.hpp:174](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/einsum.hpp#L174)），无法满足。

一句话总结根因：**SM90 的 FP8 只认 K-major、且缩放粒度被钉死在 `gran_k=128`；只要某条 einsum 在 permute 后会让 B 落到 MN-major，或要求收缩 token 维（K=b），就只能交给 SM100 的 UMMA + UE8M0 路径。**

#### 4.3.4 代码实践（规格指定，源码阅读型）

1. **目标**：说清 `bhr,hdr->bhd` 分支如何 permute，并解释「仅部分表达式支持 SM100」的根因。
2. **操作步骤**：
   - 打开 [csrc/apis/einsum.hpp:187-194](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/einsum.hpp#L187-L194)。
   - 对照 4.3.2 表格第一行，把每条 `permute({1,0,2})` 的「原始字母 → 新角色」写出来：
     - A `[b,h,r]` → `permute({1,0,2})` → `[h,b,r]` = `(batch=h, m=b, k=r)`，K 维 `r` 仍在最后、连续 → K-major。
     - SFA 跟着 A 一起 `{1,0,2}`。
     - B `[h,d,r]` 不 permute，天然 `(batch=h, n=d, k=r)`，K-major。
     - D `[b,h,d]` → `{1,0,2}` → `[h,b,d]` = `(batch=h, m=b, n=d)`。
   - 于是 `fp8_bmm` 看到的是一次标准的 `[h,b,r]@[h,d,r].T -> [h,b,d]`，`compiled_dims="nk"`（特化 n=d、k=r）。
3. **观察与推导**：为何同一条表达式 SM90 也支持、而 `bhd,hdr->bhr` 不行？答：前者 permute **不改变任何张量的主维方向**（都保持 K-major）；后者的 B 经 `{0,2,1}` 后主维从 `r` 翻到 `d`（MN-major），SM90 的 FP8 WGMMA 不接受（见 4.3.3）。
4. **可运行验证（可选）**：[tests/test_einsum.py:83-109](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/tests/test_einsum.py#L83-L109) 的 `test_fp8_bhr_hdr_bhd` 用 `per_token_cast_to_fp8`/`per_block_cast_to_fp8` 构造输入并断言 `calc_diff < 1e-3`；`test_fp8_bhd_hdr_bhr`（113 行）与 `test_fp8_bhd_bhr_hdr`（143 行）则用 `@test_filter(lambda: get_arch_major() >= 10)` 守卫，**正对应「仅 SM100」的两条**。运行它们即可实证守卫的存在——**待本地验证**（需 SM90/SM100 真机）。

#### 4.3.5 小练习与答案

**Q1**：`fp8_einsum` 里 `bhr,hdr->bhd` 的 SF（缩放因子）有没有跟着 permute？为什么？
**A**：有。`perm_sfa = a.second.permute({1,0,2})`（[einsum.hpp:191](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/einsum.hpp#L191)）。因为 SF 必须与权重/激活的逐块结构一一对应，A 换了轴，它的 SF 也得换同样的轴，否则缩放对不上。

**Q2**：`bhd,bhr->hdr` 为什么用 `compiled_dims="mn"` 而不是 `"nk"`？
**A**：它的 K 维是 `b`（token/batch 数，运行时多变），而 m=d、n=r 是固定的权重维度。按 u5-l3 的约定，应特化稳定的权重维（mn）、把易变的 b 留作运行时，避免每换一个 batch 就重编译。

**Q3**：若把 `bhd,hdr->bhr` 的架构守卫去掉、强行在 SM90 上跑，会在哪一步失败？
**A**：在 `fp8_bmm` 派发到 `sm90_fp8_bmm` 后，因 B 处于 MN-major，违背 SM90 FP8 的 K-major 强制约束（u2-l1），kernel 行为不正确（或在前置断言处报错）。

## 5. 综合实践

把本讲三个模块串起来，完成一次「**表达式 → GEMM 角色 → 派发路径**」的完整推理。

1. **任务**：给定表达式 `bhd,bhr->hdr`、输入 A=`[b,h,d]`、B=`[b,h,r]`、D=`[h,d,r]`，请：
   - 写出 `(batch, m, n, k)` 角色（答案：`(h, d, r, b)`）。
   - 写出 A、B 各自的 permute（答案：A `{1,2,0}→[h,d,b]`，B `{1,2,0}→[h,r,b]`）。
   - 判断它能在 SM90 上跑吗？为什么？（答案：不能，K 维是 token 维 b、且属 wgrad 形态，仅 SM100 的 `sm100_fp8_bmm` 支持；SM90 还要求 `gran_k==128`。）
2. **对照源码**：把你的答案与 [csrc/apis/einsum.hpp:204-210](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/einsum.hpp#L204-L210) 逐行核对。
3. **延伸**：再选 `bhr,hdr->bhd`，独立推导一遍，并解释为何这一条 SM90 也能跑（两个操作数 permute 后都仍 K-major）。

## 6. 本讲小结

- `tf32_hc_prenorm_gemm` 用 TF32 MMA 算 \(D=A B^\top\)，同时**在装载 A 的同一个循环里顺手累加 `sqr_sum`**，供 HyperConnection 的预归一化使用；`num_splits` 做 split-K，输出 `[num_splits,...]` 份部分和需调用方归约。
- `einsum` 是**硬编码**的三条 BF16 表达式：`bmk,bnk->mn` 走专用 split-K kernel（FP32 原地累加 / BF16 经 workspace），`bhr,hdr->bhd` 与 `bhd,hdr->bhr` 把 head 维当 batch、用 3D TMA 描述符零拷贝。
- `fp8_einsum` 把所有表达式统一归约到底层 `fp8_bmm` 的 `(batch,m,n,k)` 顺序，靠**零拷贝 permute** 换轴；A 的 SF 也必须同步 permute。
- 「仅 SM100」的根因是 SM90 FP8 强制 K-major 且 `gran_k==128`：凡 permute 后令 B 落到 MN-major（`bhd,hdr->bhr`）或要收缩 token 维（`bhd,bhr->hdr`），都只能走 SM100 的 UMMA + UE8M0 路径。
- 三类内核共同体现 DeepGEMM 的设计哲学：**把非标准运算，用 permute/旁路输出/3D 描述符，规约成 tensor core 最擅长的 (batch,m,n,k) 矩阵乘**。

## 7. 下一步学习建议

- 回看 u2-l1 / u2-l2，把「K-major 强制」「UE8M0 打包 SF」与本讲的 SM90/SM100 表达式差异对照，你会对架构派发有更立体的理解。
- 阅读 [csrc/jit_kernels/impls/sm100_bmk_bnk_mn.hpp](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/sm100_bmk_bnk_mn.hpp)，看 split-K 的 `split_factor` 如何由 `num_sms` 与 `num_mn_blocks` 推导——这是 `bmk,bnk->mn` 跨 batch 归约的性能核心。
- 下一讲 u10-l1 将进入 FP4 GEMM 与 FP8×FP4 路径，本讲提到的 UE8M0、TF32、(batch,m,n,k) 角色都会再次出现，可顺延学习。
