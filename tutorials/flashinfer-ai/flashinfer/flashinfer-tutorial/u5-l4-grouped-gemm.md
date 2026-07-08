# Grouped GEMM

## 1. 本讲目标

本讲聚焦 FlashInfer 中「分组矩阵乘（Grouped GEMM）」这一类算子。它服务于大模型推理里两个最典型的场景：**混合专家（MoE）的专家 FFN** 与 **多适配器（LoRA）的按段选权重**，再加上一个与之配套的**路由器小矩阵乘（router GEMM）**。

学完后你应当能够：

- 说清楚「分组矩阵乘」与普通 `mm_*` 的区别，以及为什么 MoE/LoRA 不能直接用普通 GEMM。
- 区分 FlashInfer 中两套并存的 grouped GEMM 实现：基于 cuDNN 的 `grouped_mm_*`（面向 MoE）与基于 CUTLASS 的 `SegmentGEMMWrapper`（更通用、支持按段选权重）。
- 看懂 `m_indptr` / `seg_indptr` 这种「前缀和」索引如何描述「每段 token 数不同」的变长批次（承接 u3-l1 的 ragged 张量概念）。
- 理解 router GEMM 为什么做成「写死形状」的专用 kernel，以及它与 `tinygemm_bf16` 的关系。
- 自己跑通一次分组矩阵乘，并用 cuBLAS（`torch.matmul`）逐专家验证结果。

## 2. 前置知识

在进入源码前，先建立几条直觉。

### 2.1 为什么 MoE 不能直接用普通 GEMM

MoE 层的结构是：先有一个**路由器（router/gate）**给每个 token 算出「该去哪几个专家」，然后把 token 按专家分组，分别送进**每个专家自己的 FFN**（两个矩阵乘 + 激活）。

关键在于：每个专家的权重矩阵 \(W_e\) 都不一样，且分到每个专家的 token 数 \(M_e\) 也随输入变化（有的专家热门、有的冷门）。于是对一个 batch 的 token，你需要做：

\[
Y_e = A_e \cdot W_e^{\top}, \quad e = 0,1,\dots,E-1
\]

其中 \(A_e\) 的行数 \(M_e\) 因专家而异。

- **朴素做法**：循环 `for e in experts: torch.matmul(...)`，每个专家 launch 一次 kernel。问题：MoE 的 \(M_e\) 经常很小（个位数到几十），小 GEMM launch 开销和尾延迟会把 GPU 拖垮。
- **分组做法**：把所有专家的 GEMM 打包进**一次 kernel launch**，让 kernel 内部自己去分发「这一段 token 用哪块权重」。这就是 grouped GEMM。

### 2.2 前缀和索引（ragged 张量）

承接 u3-l1/u3-l2 讲过的 ragged/paged 约定：把每段长度 \(M_e\) 做前缀和，得到一个长度为 \(E+1\) 的数组 `m_indptr`：

\[
\texttt{m\_indptr}[0]=0,\quad \texttt{m\_indptr}[e+1]=\texttt{m\_indptr}[e]+M_e
\]

于是第 \(e\) 个专家的 token 段就是拼接张量 `a` 的 `[m_indptr[e] : m_indptr[e+1]]` 切片。这样所有专家的输入可以紧凑地拼成一张 `(cum_m, k)` 的二维张量，`cum_m = sum(M_e)`，无需 padding。

### 2.3 grouped GEMM 在仓库里的两条线

读完源码后你会发现，FlashInfer 里「分组矩阵乘」其实有**两条独立的实现线**，对应不同的后端和适用场景。这是本讲最容易混淆的点，先列在这里：

| | `grouped_mm_*`（MoE 专用） | `SegmentGEMMWrapper`（通用段式） |
|---|---|---|
| 入口 | `flashinfer.grouped_mm.grouped_mm_bf16/fp8/mxfp8/fp4` | `flashinfer.gemm.SegmentGEMMWrapper` |
| 后端 | cuDNN 的 `moe_grouped_matmul` 算子 | CUTLASS 的 `GemmGrouped` |
| 索引约定 | `m_indptr`（cum_m 累计偏移） | `seg_lens` 或 `seg_indptr` |
| 按段选权重（LoRA 风格） | 不支持（专家与段一一对应） | **支持 `weight_indices`** |
| 典型场景 | DeepSeek-V3 等大 MoE 的专家 FFN | 变长段式 GEMM、多 LoRA 适配器 |

二者数学上等价，都是上面那个 \(\;Y_e=A_e W_e^\top\;\) 公式，区别在后端与「能否按段选权重」。下面分别精读。

## 3. 本讲源码地图

本讲涉及的关键文件（按「Python 入口 → cuDNN/CUTLASS 后端 → C++ 绑定 → kernel 模板」的层次组织）：

| 文件 | 作用 |
|------|------|
| [flashinfer/grouped_mm/core.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/grouped_mm/core.py) | cuDNN MoE grouped GEMM 的后端无关入口：`grouped_mm_bf16/fp8/mxfp8/fp4`，负责参数校验与后端派发 |
| [flashinfer/grouped_mm/cudnn/core.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/grouped_mm/cudnn/core.py) | cuDNN 后端实现：构造/缓存 cuDNN 计算图 `moe_grouped_matmul`，把 torch 张量喂进图执行 |
| [flashinfer/gemm/gemm_base.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/gemm/gemm_base.py) | 通用 `SegmentGEMMWrapper`（CUTLASS 段式 GEMM），含 sm80/sm90 派发与 triton 参数准备 |
| [include/flashinfer/gemm/group_gemm.cuh](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/gemm/group_gemm.cuh) | CUTLASS `GemmGrouped` kernel 模板（sm80 路径），框架无关，只认裸指针 |
| [csrc/group_gemm.cu](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/csrc/group_gemm.cu) | `CutlassSegmentGEMM` launcher：做 dtype 派发后调用上面的 kernel 模板 |
| [csrc/flashinfer_gemm_binding.cu](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/csrc/flashinfer_gemm_binding.cu) | TVM-FFI 绑定：用 `TVM_FFI_DLL_EXPORT_TYPED_FUNC` 把 `cutlass_segment_gemm` 导出给 Python |
| [flashinfer/gemm/routergemm.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/gemm/routergemm.py) | 固定形状的专用 router GEMM（DeepSeek-V3/Mistral-Large-3/GLM-MoE）与 `tinygemm_bf16` |
| [tests/gemm/test_group_gemm.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/tests/gemm/test_group_gemm.py) | `SegmentGEMMWrapper` 的正确性测试，可作为运行范例参考 |

---

## 4. 核心概念与源码讲解

### 4.1 Grouped GEMM 是什么：MoE 专家与 LoRA 适配器

#### 4.1.1 概念说明

「分组矩阵乘（Grouped/Segment GEMM）」指的是：**一次 kernel launch 里，完成多个形状（主要是行数 M）各异的 GEMM**。它跟普通 GEMM 的关系，类似于 u3-l4 里「变长 prefill 批次」跟「单序列 prefill」的关系——都是为了处理「变长、又不想 padding、又不想循环 launch」的场景。

两个核心动机：

1. **MoE 专家 FFN**：每个专家一套权重 \(W_e\)，分到每个专家的 token 数 \(M_e\) 不同。把所有专家压成一次 launch，避免 E 次小 GEMM 的 launch 开销。
2. **多 LoRA 适配器**：同一个基座模型挂载多个 LoRA 适配器（每条请求可能用不同适配器）。把「按请求选哪套权重」下推到 kernel 内部，省掉 host 端的分支与同步。这就是 `SegmentGEMMWrapper` 的 `weight_indices` 参数要解决的问题（仓库另有更专用的 `bgmv_moe` 内核，见 `flashinfer/fused_moe/bgmv_moe.py`）。

一个常被忽略的约束：grouped GEMM 里**每个分组的 \(K\) 和 \(N\) 是固定不变的**（因为权重张量是 `(E, N, K)` 这种统一形状），变化的只有每段的行数 \(M_e\)。所以它适合「专家权重同形、token 数各异」的 MoE/LoRA，不适合「每个分组连 K、N 都不同」的极端情况。

#### 4.1.2 核心流程

一次 grouped GEMM 的逻辑流程（两种实现共用）：

```text
输入：a (cum_m, k)         ← 所有 token 拼成的二维张量
     b (E, n, k)           ← E 个专家权重（共享 n, k）
     m_indptr (E+1,)       ← 每个专家 token 数的前缀和

for e in range(E):
    start, end = m_indptr[e], m_indptr[e+1]
    out[start:end] = a[start:end] @ b[e].T        # 仅 e 这段用 b[e]
return out (cum_m, n)
```

关键点：**整个循环发生在单次 kernel 内部**，host 端只 launch 一次。内核靠 `m_indptr` 知道「我这个 thread block 该处理哪个专家的哪段 token」。

#### 4.1.3 源码精读

数学定义直接写在 `grouped_mm_bf16` 的 docstring 里，是理解所有变体的总纲：

[flashinfer/grouped_mm/core.py:96-101](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/grouped_mm/core.py#L96-L101) — 这段 docstring 把 grouped GEMM 的公式 \(\text{out}[\text{start}:\text{end}] = a[\text{start}:\text{end}] \times b[e]^T\) 写死，并约定 `start, end = m_indptr[e], m_indptr[e+1]`。

参数校验把上面这些「形状约定」固化成显式断言，例如要求 `b` 必须是三维 `(num_experts, n, k)`、`m_indptr` 长度恰为 `num_experts+1`：

[flashinfer/grouped_mm/core.py:53-65](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/grouped_mm/core.py#L53-L65) — 这里检查 `a` 为 2D `(cum_m, k)`、`b` 为 3D `(num_experts, n, k)`、`m_indptr` 为 int32 且长度等于 `num_experts+1`，以及 `a.shape[1] == b.shape[2]`（K 维一致）。这些断言就是「分组 GEMM 形状约束」的可执行版本。

> 术语：**cum_m**（cumulative m）= 所有分组的行数之和，即 `m_indptr[-1]`。这是 grouped GEMM 文档里反复出现的量。

#### 4.1.4 代码实践（源码阅读型）

本节先做一个阅读型实践，建立全局印象，可运行实践放在 4.2.4。

1. **实践目标**：在源码里把「分组」这件事的两条实现线定位出来。
2. **操作步骤**：
   - 打开 `flashinfer/grouped_mm/__init__.py`，确认对外暴露的是 `grouped_mm_bf16/fp8/mxfp8/fp4` 这四个 cuDNN 入口（外加一个 `moe_gemm_mxfp8_nt_groupwise`）。
   - 打开 `flashinfer/gemm/__init__.py`，确认 `SegmentGEMMWrapper` 与 `tinygemm_bf16`、`mm_M1_16_K7168_N256` 等 router 入口都在 `gemm_base` / `routergemm` 里。
3. **需要观察的现象**：两条线分别处在 `flashinfer.grouped_mm` 与 `flashinfer.gemm` 两个包，互不依赖。
4. **预期结果**：你能用一句话说出「`grouped_mm_*` 走 cuDNN、`SegmentGEMMWrapper` 走 CUTLASS」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 grouped GEMM 要求所有专家的 \(K\)、\(N\) 相同，却允许 \(M_e\) 不同？

**答案**：\(K\)、\(N\) 相同才能把权重紧凑存成 `(E, N, K)` 一块张量、且让 kernel 对所有分组复用同一套 tile/block 参数；而 \(M_e\) 是「分到这个专家的 token 数」，天然因输入而变，正是 grouped GEMM 要解决的变长问题，故必须允许不同。

**练习 2**：给定 `m_indptr = [0, 3, 3, 7]`，说明每个专家各分到几个 token、哪个专家被「冷落」了。

**答案**：\(M_0=3-0=3\)、\(M_1=3-3=0\)、\(M_2=7-3=4\)。专家 1 一个 token 都没分到（冷门专家），`cum_m=7`。grouped GEMM 能自然处理这种「空段」。

---

### 4.2 grouped_mm_*：基于 cuDNN 的 MoE 分组矩阵乘

#### 4.2.1 概念说明

`grouped_mm_*` 是 FlashInfer 面向 MoE 的「主推」分组 GEMM 入口，实现压在 cuDNN 的 `moe_grouped_matmul` 算子上。它的特点是：

- **不写 kernel，而是构造 cuDNN 计算图**：这与 u3-l5 讲过的 cuDNN 注意力后端同一思路——用 cuDNN frontend 描述一个 SDPA/MOE 计算图，让 cuDNN 自己选最优 execution plan。
- **精度档位齐全**：`grouped_mm_bf16`（BF16/FP16）、`grouped_mm_fp8`（per-tensor 缩放）、`grouped_mm_mxfp8`（MXFP8 块缩放）、`grouped_mm_fp4`（NVFP4/MXFP4）。命名规律与 u5-l1 讲过的「形状前缀 + 精度后缀 + 缩放粒度」一致。
- **依赖外部 cuDNN**：需要安装 `nvidia-cudnn-cu12` 与 `nvidia-cudnn-frontend`，且 backend 版本 ≥ 9.21.0（`_CUDNN_MOE_MIN_VERSION`）。这是它不如 CUTLASS 路线「开箱即用」的地方。

#### 4.2.2 核心流程

`grouped_mm_bf16` 的执行链路：

```text
grouped_mm_bf16(a, b, m_indptr)           # Python 入口（core.py）
  └─ _check_grouped_mm_bf16(...)          # 形状/dtype 校验（supported_compute_capability 守门）
     └─ _run_cudnn_moe_grouped_gemm(...)  # cuDNN 后端（cudnn/core.py）
        ├─ 把 m_indptr[:-1] 转成 first_token_offset（每个专家的起始 token 偏移）
        ├─ _build_cudnn_moe_grouped_gemm_graph(...)  # @lru_cache 缓存计算图
        │     └─ graph.moe_grouped_matmul(token, weight, first_token_offset)
        ├─ 准备 variant_pack（UID → torch 张量）
        └─ graph.execute(variant_pack, workspace)    # 真正执行
```

关键巧思：cuDNN 的 MOE 算子不读「前缀和」，而读**每个专家的起始 token 偏移** `first_token_offset`（即 `m_indptr[:-1]`）。所以后端做了一次 `[::1]` 切片把前缀和翻译成 cuDNN 要的语义。

#### 4.2.3 源码精读

入口 `grouped_mm_bf16` 用本单元 u5-l1 讲过的两个声明式装饰器把校验与后端选择抽离：

[flashinfer/grouped_mm/core.py:79-90](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/grouped_mm/core.py#L79-L90) — `@backend_requirement({}, common_check=_check_grouped_mm_bf16)` 把形状校验挂上去，`@flashinfer_api` 挂上日志/trace 能力。注意 `backend="cudnn"` 是默认且目前唯一支持的后端，函数体内 `if backend == "cudnn": ... else: raise`。

实际派发到 cuDNN 的那一行：

[flashinfer/grouped_mm/core.py:139-145](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/grouped_mm/core.py#L139-L145) — 先 `_check_cudnn_version` 探测 cuDNN 版本与 `moe_grouped_matmul_mode` 符号是否就绪，再调用 `_run_cudnn_moe_grouped_gemm`。`tactic=-1` 走启发式最优 plan，非负值指定某个 execution plan。

进入 cuDNN 后端后，先做一次语义翻译：

[flashinfer/grouped_mm/cudnn/core.py:245-248](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/grouped_mm/cudnn/core.py#L245-L248) — `token_3d = a.unsqueeze(0)` 给 token 升一维、`weight_3d = b.transpose(1, 2)` 把权重转成 cuDNN 期望的布局、`fto = m_indptr[:-1].reshape(-1,1,1)` 把前缀和翻译成 cuDNN 的 `first_token_offset`。这一步就是上面流程图里「前缀和 → 起始偏移」的落点。

然后构造 cuDNN 计算图，核心是 `moe_grouped_matmul` 节点：

[flashinfer/grouped_mm/cudnn/core.py:182-189](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/grouped_mm/cudnn/core.py#L182-L189) — `graph.moe_grouped_matmul(token=token, weight=weight, first_token_offset=fto, mode=..., compute_data_type=FLOAT)`。这个图被 `@functools.lru_cache(maxsize=1024)` 缓存（[第 136 行](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/grouped_mm/cudnn/core.py#L136)），key 是所有形状/stride/dtype 元组，故同形状反复调用不会重建图——这是 cuDNN 路线摊薄开销的关键。

> 术语：**UID（unique identifier）**：cuDNN 图里每个张量有一个全局唯一 ID，`variant_pack` 通过 UID 把运行期 torch 张量绑回图节点。`_CUDNN_UIDs` 枚举（[第 51-60 行](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/grouped_mm/cudnn/core.py#L51-L60)）给 token/weight/output 等分配了固定 UID 命名空间。

#### 4.2.4 代码实践（可运行）

本实践对应本讲的核心任务：用 `grouped_mm_bf16` 做一组行数各异的 GEMM（模拟多专家），再用 cuBLAS（`torch.matmul`）逐专家核对。

1. **实践目标**：验证 grouped GEMM 的输出 = 逐专家普通 GEMM 的拼接。
2. **操作步骤**：把下面这段「示例代码」保存为 `grouped_mm_check.py` 并运行（需要一张 SM80+ 的卡，且已 `pip install` 了 cuDNN frontend）。

```python
# 示例代码：grouped_mm_bf16 多专家正确性核对
import torch
import flashinfer

torch.manual_seed(0)
E, k, n = 4, 512, 256              # 4 个专家，K=512，N=256 固定
tokens_per_expert = [3, 0, 5, 2]   # 每个专家分到的 token 数各异（含一个空段）
cum_m = sum(tokens_per_expert)     # = 10

# 1) 构造拼接输入 a (cum_m, k) 与专家权重 b (E, n, k)
a = torch.randn(cum_m, k, dtype=torch.bfloat16, device="cuda")
b = torch.randn(E, n, k, dtype=torch.bfloat16, device="cuda")

# 2) 构造前缀和索引 m_indptr (E+1,) int32：[0,3,3,8,10]
m_indptr = torch.tensor([0] + torch.cumsum(torch.tensor(tokens_per_expert), 0).tolist(),
                        dtype=torch.int32, device="cuda")

# 3) 一次 launch 完成 4 个专家的 GEMM
out = flashinfer.grouped_mm.grouped_mm_bf16(a, b, m_indptr)   # (cum_m, n)

# 4) 用 cuBLAS（torch.matmul）逐专家核对
for e in range(E):
    s, t = m_indptr[e].item(), m_indptr[e + 1].item()
    if s == t:
        continue  # 空段跳过
    ref = torch.matmul(a[s:t].float(), b[e].float().T).to(torch.bfloat16)
    assert torch.allclose(out[s:t], ref, rtol=1e-2, atol=1e-2), f"expert {e} mismatch"
print("grouped_mm_bf16 与逐专家 cuBLAS 结果一致 ✓")
```

3. **需要观察的现象**：首次调用会触发 cuDNN 图构建（`_build_cudnn_moe_grouped_gemm_graph`），第二次同形状调用直接命中 `lru_cache`。输出形状为 `(10, 256)`，且每段与 `torch.matmul` 参考一致。
4. **预期结果**：打印「结果一致 ✓」。若机器上没有 cuDNN 或版本低于 9.21.0，会抛出 `_check_cudnn_version` 的清晰错误（提示安装 `nvidia-cudnn-cu12 nvidia-cudnn-frontend`）。
5. **待本地验证**：上述误差容限 `rtol/atol=1e-2` 与具体硬件相关，若核对不过可放宽到 `5e-2` 再观察 BF16 的典型精度范围。

> 提示：如果你没有可用的 cuDNN frontend，可改用 4.3 节的 `SegmentGEMMWrapper`（CUTLASS 路径，不依赖 cuDNN），它同样能完成这个核对实验。

#### 4.2.5 小练习与答案

**练习 1**：把上面示例里 `tokens_per_expert` 改成全相同（如 `[3,3,3,3]`），结果还正确吗？这种「等长」场景为什么仍然值得用 grouped GEMM？

**答案**：仍然正确。即便每段等长，grouped GEMM 也只用一次 kernel launch 完成 4 次乘法，省掉了 3 次 launch 开销；对 MoE 这种「E 个专家、每个专家都是小 GEMM」的场景，launch 开销往往比计算本身还贵。

**练习 2**：`tactic` 参数取 `-1` 与取一个非负整数时，cuDNN 后端走的代码分支有何不同？

**答案**：`-1`（默认）用 `build_plan_policy.HEURISTICS_CHOICE`，让 cuDNN 启发式挑最优 plan，调 `graph.execute`；非负值用 `build_plan_policy.ALL` 构建全部候选 plan，再调 `graph.execute_plan_at_index(..., tactic, ...)` 选指定序号的 plan（见 [cudnn/core.py:287-293](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/grouped_mm/cudnn/core.py#L287-L293)），用于 benchmark/tactic 搜索。

---

### 4.3 SegmentGEMMWrapper：基于 CUTLASS 的段式分组 GEMM

#### 4.3.1 概念说明

`SegmentGEMMWrapper` 是 FlashInfer 里**更通用、更底层**的分组 GEMM，后端是 CUTLASS 的 `GemmGrouped` device kernel。它和 `grouped_mm_*` 解决同一个数学问题，但有几个关键不同：

- **不依赖 cuDNN**：纯 CUTLASS，SM80（Ampere）以上即可用，开箱即用。
- **支持按段选权重 `weight_indices`**：第 \(i\) 段可以用 `weights[weight_indices[i]]`，而不必 `weights[i]`。这正是**多 LoRA 适配器**「不同请求用不同适配器权重」所需的能力。
- **支持 FP8**：输入若是 FP8，输出自动提升到 BF16（见 4.3.3）。
- **plan-like 两步**：构造时传入 workspace（建议 128 MiB），`run()` 时执行。这与注意力 wrapper 的 plan/run 风格一致（u3-l3）。

它内部还要区分 sm80 与 sm90 两个 CUTLASS 路径，分别用 `cutlass_segment_gemm` 和 `cutlass_segment_gemm_sm90` 两个 TVM-FFI 符号。

#### 4.3.2 核心流程

`SegmentGEMMWrapper.run` 的执行链路（sm80 路径）：

```text
run(x, weights, batch_size, weight_column_major, seg_lens=...)
  ├─ 由 seg_lens 算出 seg_indptr（前缀和，ragged 约定）
  ├─ launch_compute_sm80_group_gemm_args(...)        # 一个 triton kernel
  │     产出 7 个 device 数组：all_problems(M,N,K), x_ptr/w_ptr/y_ptr, x_ld/w_ld/y_ld
  ├─ get_gemm_module().cutlass_segment_gemm(...)     # TVM-FFI 调用
  │     └─ CutlassSegmentGEMM (csrc/group_gemm.cu)
  │           └─ CutlassSegmentGEMMRun (include/.../group_gemm.cuh)
  │                 └─ cutlass::gemm::device::GemmGrouped  ← 真正的 grouped kernel
  └─ return out
```

这里有个**关键设计**：CUTLASS 的 `GemmGrouped` 不接受「前缀和」，而接受**一个显式的问题数组 `all_problems`（每项是 `(M,N,K)`）+ 每段的指针数组 `x_ptr/w_ptr/y_ptr` + 每段的 leading dimension `x_ld/w_ld/y_ld`**。所以 Python 侧先用一个 triton kernel（`compute_sm80_group_gemm_args`，[gemm_base.py:1836-1854](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/gemm/gemm_base.py#L1836-L1854)）把这些「指针/stride 描述符」在 GPU 上算出来，再整体喂给 CUTLASS。

#### 4.3.3 源码精读

Python 入口 `SegmentGEMMWrapper.run` 的派发逻辑：

[flashinfer/gemm/gemm_base.py:2118-2188](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/gemm/gemm_base.py#L2118-L2188) — `backend="auto"` 时调 `determine_gemm_backend`（Hopper+CUDA12.3 → `"sm90"`，否则 `"sm80"`），再分别走 `launch_compute_sm90_group_gemm_args`/`launch_compute_sm80_group_gemm_args` 准备描述符，最后调 `cutlass_segment_gemm_sm90` 或 `cutlass_segment_gemm`。

> 术语：**leading dimension（ld / stride）**：二维矩阵按一维线性内存存放时，「跨行」的步长。CUTLASS grouped kernel 要为每一段单独提供 `x_ld/w_ld/y_ld`，因为每段虽共享 K、N，但其在拼接张量里的行步长可能不同（尤其是有 `weight_indices` 间接时）。

`weight_indices`（LoRA 选权重）的处理很轻量——只是传一个可为空的索引数组进去，真正的「按索引取权重指针」发生在那个 triton 参数准备 kernel 里：

[flashinfer/gemm/gemm_base.py:2097-2099](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/gemm/gemm_base.py#L2097-L2099) — `weight_indices is None` 时给一个空的 CPU 占位张量；否则把它作为「第 i 段用哪块权重」的索引传入。`launch_compute_sm80_group_gemm_args`（[第 1812 行](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/gemm/gemm_base.py#L1812)）把它一并喂给 triton kernel，后者据此生成正确的 `w_ptr[i] = weights[weight_indices[i]]`。

绑定层用 TVM-FFI 把 `CutlassSegmentGEMM` 导出：

[csrc/flashinfer_gemm_binding.cu:30-34](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/csrc/flashinfer_gemm_binding.cu#L30-L34) — `TVM_FFI_DLL_EXPORT_TYPED_FUNC(cutlass_segment_gemm, CutlassSegmentGEMM)` 把 C++ 函数 `CutlassSegmentGEMM` 以名字 `cutlass_segment_gemm` 暴露给 Python（承接 u9-l2 的 TVM-FFI 跨语言 ABI）。

csrc launcher 做一次 dtype 派发，再转交给头文件里的模板：

[csrc/group_gemm.cu:23-40](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/csrc/group_gemm.cu#L23-L40) — `CutlassSegmentGEMM` 用 `DISPATCH_DLPACK_DTYPE_TO_CTYPE_FP16` 把 fp16/bf16 映射到 CUTLASS 的 `cutlass_dtype_t`，再调 `CutlassSegmentGEMMRun<cutlass_t>`。注意它恪守 u1-l3 的框架无关红线——只接收 `TensorView`（裸指针+形状），不碰 torch 头文件。

真正的 grouped kernel 模板在头文件里，核心是 `cutlass::gemm::kernel::DefaultGemmGrouped`：

[include/flashinfer/gemm/group_gemm.cuh:60-90](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/gemm/group_gemm.cuh#L60-L90) — 这里把 CUTLASS grouped GEMM 的全部模板参数钉死：元素类型 `DType`（fp16/bf16）、A 行主序/B 可列可行、`Sm80` 架构、ThreadblockShape `128×128×32`、WarpShape `64×64×32`、InstructionShape `16×8×16`、FP32 累加。下面 `GemmGrouped::Arguments` 把 `all_problems`、`x/w/y` 指针数组、leading dimension 数组、`threadblock_count=4` 组装成参数。

两个分发宏决定运行期行为：

[include/flashinfer/gemm/group_gemm.cuh:28-44](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/gemm/group_gemm.cuh#L28-L44) — `DISPATCH_WEIGHT_LAYOUT` 按运行期 `weight_column_major` 选 CUTLASS 的 `ColumnMajor`/`RowMajor` 布局；`DISPATCH_SMEM_CONFIG` 按设备每 SM 共享内存上限选 4 stage（≥147968 字节）或 2 stage，让大共享内存的卡用更深流水线。这正是 u2-l3 提到的「DISPATCH 宏处理组合参数空间」的实例。

#### 4.3.4 代码实践（源码阅读型 + 可选运行）

1. **实践目标**：理解「前缀和 → 问题数组 + 指针数组」的翻译过程，并跑通一个 LoRA 风格的按段选权重例子。
2. **操作步骤**：
   - 阅读 [flashinfer/gemm/gemm_base.py:1929-1978](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/gemm/gemm_base.py#L1929-L1978) 里 `SegmentGEMMWrapper` 的 docstring 示例（含 `weight_indices` 用法）。
   - 阅读 `tests/gemm/test_group_gemm.py` 的 `test_segment_gemm`（[第 53-124 行](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/tests/gemm/test_group_gemm.py#L53-L124)），看它如何用 `torch.matmul` 逐段核对。
3. **可选运行**（不依赖 cuDNN，SM80+ 即可）：

   ```python
   # 示例代码：SegmentGEMMWrapper 的 LoRA 风格按段选权重
   import torch, flashinfer
   torch.manual_seed(42)
   ws = torch.empty(32 * 1024 * 1024, dtype=torch.int8, device="cuda")
   seg = flashinfer.gemm.SegmentGEMMWrapper(ws)            # backend="auto"
   x = torch.randn(10, 128, dtype=torch.float16, device="cuda")
   weights = torch.randn(4, 256, 128, dtype=torch.float16, device="cuda")  # column-major
   # 让第 3 段复用第 0 块权重（模拟「不同请求选不同适配器」）
   wi = torch.tensor([0, 1, 2, 0], dtype=torch.int64, device="cuda")
   y = seg.run(x, weights, batch_size=4, weight_column_major=True,
               seg_lens=torch.tensor([1, 2, 3, 4], dtype=torch.int64),
               weight_indices=wi)
   # 核对第 3 段（token 6:10）确实用了 weights[0]
   ref = torch.matmul(x[6:].float(), weights[0].float().T).to(torch.float16)
   assert torch.allclose(y[6:], ref, rtol=1e-3, atol=2e-3)
   print("weight_indices 按段选权重行为正确 ✓")
   ```

4. **需要观察的现象**：第 3 段虽然逻辑上是「第 4 个段」，但因为 `weight_indices[3]=0`，它用的是 `weights[0]`，与第 1 段共用同一块权重。
5. **预期结果**：打印「行为正确 ✓」。
6. **待本地验证**：sm80 路径默认在 Ampere/Hopper 都可用；若你的卡是 Hopper 且 `determine_gemm_backend` 返回 `"sm90"`，会改走 `cutlass_segment_gemm_sm90`，行为同样正确但 kernel 不同。

#### 4.3.5 小练习与答案

**练习 1**：`SegmentGEMMWrapper` 的 `backend="auto"` 是怎么决定走 sm80 还是 sm90 的？

**答案**：调 `flashinfer.utils.determine_gemm_backend(device)`（[utils.py:379-384](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/utils.py#L379-L384)）：若 `compute_capability` 的 major==9 且 CUDA≥12.3 返回 `"sm90"`（Hopper 专用 CUTLASS 路径，用 TMA/wgmma），否则返回 `"sm80"`（Ampere 兼容路径）。

**练习 2**：为什么 CUTLASS grouped kernel 需要 `x_ptr/w_ptr/y_ptr` 这种「每段一个指针」的数组，而不能像普通 GEMM 那样只传基地址？

**答案**：因为每段对应不同的专家权重（基地址不同）、在拼接张量里位置不连续（尤其 `weight_indices` 间接后，相邻段可能指向完全不同的权重块）。CUTLASS 的 `GemmGrouped` 把每段当成一个独立 GEMM，需要为每段提供各自的基地址与 stride，所以必须传「指针数组 + leading dimension 数组」。

---

### 4.4 Router GEMM：固定形状的小矩阵乘

#### 4.4.1 概念说明

回到 2.1 节的 MoE 流程：在「专家 FFN」之前还有一步**路由器矩阵乘**——\( \text{logits} = A \cdot W_{\text{router}}^{\top} \)，得到每个 token 对所有专家的打分，再做 top-k 选专家。

这步矩阵乘有两个鲜明特征：

1. **M 极小**：M 就是当前 batch 的 token 数，decode 阶段常常只有 1~16 行。
2. **延迟极敏感**：它在 MoE 的关键路径上，每一步推理都要跑，必须压到极致。

通用 GEMM（包括 grouped GEMM）在小 M 下并不快——它们为「大 M、高吞吐」设计。所以 FlashInfer 为几个主流 MoE 模型的 router 写了**形状写死、M 取值枚举（1~16）的专用 kernel**，即 `flashinfer.gemm.routergemm` 里的 `mm_M1_16_*` 系列，外加一个更通用的 `tinygemm_bf16`。

#### 4.4.2 核心流程

router GEMM 的「写死形状」体现在 M、N、K 都是编译期常量：

```text
mm_M1_16_K7168_N256(mat_a, mat_b, out)
  └─ get_dsv3_router_gemm_module().mm_M1_16_K7168_N256(...)
        └─ module.dsv3_router_gemm_op(...)   # TVM-FFI
              └─ generic_router_gemm_op<Tout, tout_code, kNumExperts=256, kHiddenDim=7168, 1, 16>
                    └─ LoopUnroller<1,16,256,7168>::unroll(num_tokens, ...)
                          └─ invokeRouterGemm<Tout, bf16, num_tokens, 256, 7168>(...)
                                └─ router_gemm_kernel<..., 128, VPT=8, num_tokens, 256, 7168>
```

关键巧思是 `LoopUnroller`：因为 M（num_tokens）只在 1~16 之间，编译期用模板递归把 16 种 M 取值**全部实例化**成不同的 kernel，运行期按 `num_tokens` 选对应那个。这把「运行期分支」变成「编译期展开」，零分支开销。

#### 4.4.3 源码精读

三个写死形状的 router 入口分别对应三个模型：

[flashinfer/gemm/routergemm.py:213-256](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/gemm/routergemm.py#L213-L256) — `mm_M1_16_K7168_N256`（DeepSeek-V3，K=7168/N=256/out=fp32）。同类还有 `mm_M1_16_K7168_N128`（Mistral-Large-3，[第 167-210 行](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/gemm/routergemm.py#L167-L210)）与 `mm_M1_16_K6144_N256`（GLM-MoE-DSA，[第 259-302 行](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/gemm/routergemm.py#L259-L302)）。它们都要求 SM100/SM103（Blackwell）、`mat_a` 行主序、`mat_b` 列主序、M∈[1,16]。

形状校验把「写死形状」断言化：

[flashinfer/gemm/routergemm.py:51-65](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/gemm/routergemm.py#L51-L65) — `_router_gemm_shape_checks` 强制 `num_tokens ∈ [1,16]`、`hidden_dim` 与 `num_experts` 必须等于该模型的固定值（如 7168/256）、且 `mat_a` 行主序、`mat_b` 列主序、`out` 行主序。不匹配就抛 `ValueError`。

模块加载用 u2-l5 讲过的 `@functools.cache`，且把每个 op 注册成 torch custom op（支持 torch.compile / CUDA Graph）：

[flashinfer/gemm/routergemm.py:120-164](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/gemm/routergemm.py#L120-L164) — `get_dsv3_router_gemm_module` 调 `gen_dsv3_router_gemm_module().build_and_load()`（JIT），再用 `@register_custom_op("flashinfer::dsv3_router_gemm_op", mutates_args=["out"])` 注册三个 op。

C++ 侧的 `LoopUnroller` 把 M 的 16 种取值在编译期全展开：

[csrc/dsv3_router_gemm.cu:30-57](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/csrc/dsv3_router_gemm.cu#L30-L57) — `LoopUnroller<kBegin,kEnd,kNumExperts,kHiddenDim>::unroll` 用模板递归，当 `num_tokens==kBegin` 时实例化 `invokeRouterGemm<..., kBegin, ...>`，否则递归到 `kBegin+1`，直到 `kBegin==kEnd` 的特化为终止。每个 `kBegin` 取值都生成一个独立的、M 已知的 kernel。

> 术语：**PDL（Programmatic Dependent Launch）**：Hopper 起支持的流式串行启动，前一个 kernel 还没完全结束时就可启动后一个，靠 `cudaGridDependencySynchronize()` 协调。`launch_with_pdl=True` 让 router GEMM 与上游 kernel 重叠（见 [routergemm.py:196-198](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/gemm/routergemm.py#L196-L198) 与 [dsv3_router_gemm.cu:18-21](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/csrc/dsv3_router_gemm.cu#L18-L21)）。注意 u7-l4 的 activation 也会用到 PDL。

更通用的 `tinygemm_bf16` 覆盖「形状没写死但仍然很小」的场景（如任意线性层、bias 加法）：

[flashinfer/gemm/routergemm.py:403-453](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/gemm/routergemm.py#L403-L453) — `tinygemm_bf16` 计算 `out = input @ weight.T + bias`（等价 `F.linear`），SM90+ 可用，warp-specialized（384 线程：4 计算 + 8 DMA）、16 级流水，专为 M=1~8 的极小 batch 低延迟优化，源自 TensorRT-LLM 的 `tinygemm2`。

#### 4.4.4 代码实践（源码阅读型）

router GEMM 是 Blackwell 专用，多数读者手头没有 SM100/103，所以本节做阅读型实践。

1. **实践目标**：理解「写死形状 + LoopUnroller」如何把小 GEMM 的延迟压到最低。
2. **操作步骤**：
   - 阅读 [csrc/dsv3_router_gemm.cu:59-88](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/csrc/dsv3_router_gemm.cu#L59-L88) 的 `generic_router_gemm_op`，看它如何同时持有「写死形状（`kNumExperts`/`kHiddenDim` 模板参数）」与「运行期形状（`mat_a.sizes()`）」，并在两者匹配时才走 custom kernel。
   - 对比 [flashinfer/gemm/routergemm.py:19-75](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/gemm/routergemm.py#L19-L75) 的 `_router_gemm_shape_checks`，确认 Python 端的断言与 C++ 端的运行期检查一致。
3. **需要观察的现象**：C++ 端对 `num_tokens/num_experts/hidden_dim/dtype` 同时做了运行期断言（不匹配抛 `NotImplementedError`），这是对 Python 端校验的兜底。
4. **预期结果**：你能解释「为什么 router GEMM 不做成通用 grouped GEMM」——因为 M 固定到 1~16、N/K 也固定，编译期就能把 tile/block 参数和循环边界全部算死，运行期零分支、零启发式。
5. **待本地验证**：若有 Blackwell 卡，可仿照 [routergemm.py:208-210](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/gemm/routergemm.py#L208-L210) 调用 `mm_M1_16_K7168_N256` 并与 `torch.matmul` 核对。

#### 4.4.5 小练习与答案

**练习 1**：router GEMM 为什么把 `num_tokens` 限制在 1~16，而不是任意值？

**答案**：因为 `LoopUnroller` 会为每个 `num_tokens` 取值实例化一个独立 kernel，限制在 1~16 才能把实例化数量控制在 16 个、把 M 编进 kernel 名字与 tile 参数里实现零分支。M>16 时这种特化收益递减，不如直接用通用 grouped/tiny GEMM。该约束在 [`_router_gemm_shape_checks`](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/gemm/routergemm.py#L51-L65) 与 [C++ 运行期检查](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/csrc/dsv3_router_gemm.cu#L74-L79) 两处都断言。

**练习 2**：`tinygemm_bf16` 与 `mm_M1_16_K7168_N256` 都是小 GEMM，它们的定位差别是什么？

**答案**：`mm_M1_16_*` 是**形状写死**的专用 kernel（绑定某个模型的 K/N/dtype，靠 LoopUnroller 把 M 也写死），延迟最低但完全不通用；`tinygemm_bf16` 是**形状通用**的低延迟 kernel（任意 K/N、可选 bias，只要 output_features 是 16 的倍数），牺牲一点峰值性能换取通用性，适合「小但形状不固定」的线性层。

---

## 5. 综合实践

把本讲三类算子串起来，模拟一次「极简 MoE 前向」的数据流（只做正确性演示，不求性能）：

1. **router 打分**：用 `torch.matmul`（或 Blackwell 上的 `mm_M1_16_K7168_N256`）算 token 对专家的 logits。
2. **选 top-1 专家**：用 `torch.argmax` 给每个 token 分配一个专家，统计每个专家分到的 token 数 `tokens_per_expert`。
3. **构造 `m_indptr`**：对 `tokens_per_expert` 做前缀和。
4. **scatter 拼接**：把 token 按专家顺序重排成 `(cum_m, k)` 的 `a`（可用 `torch.argsort` 模拟）。
5. **专家 GEMM**：用 `flashinfer.grouped_mm.grouped_mm_bf16(a, b, m_indptr)` 一次算完所有专家。
6. **核对**：用 cuBLAS（`torch.matmul`）逐专家核对 `out`。

要求：

- 画出从「原始 token」到「grouped GEMM 输出」的数据形状变化图。
- 在报告里标注：哪些步用了 FlashInfer 算子、哪些步用了 PyTorch、`m_indptr` 在哪一步生成、`cum_m` 等于多少。
- 若机器上没有 cuDNN，把第 5 步换成 `SegmentGEMMWrapper`，并把 `m_indptr` 转成对应的 `seg_indptr`（注意二者长度与 dtype 约定不同：`m_indptr` 是 int32 长度 E+1，`seg_indptr` 是 int64 长度 batch_size+1）。

这个任务覆盖了本讲全部三个最小模块：router GEMM（第 1 步）、grouped GEMM 的索引构造（第 3-4 步）、grouped GEMM 的执行与验证（第 5-6 步）。

## 6. 本讲小结

- **分组矩阵乘**把「多个行数各异的小 GEMM」压成一次 kernel launch，是 MoE 专家 FFN 与多 LoRA 适配器的关键算子；它要求 K、N 固定，只有每段行数 \(M_e\) 可变。
- FlashInfer 有**两条实现线**：`grouped_mm_*` 走 cuDNN `moe_grouped_matmul`（MoE 专用、精度档全、需 cuDNN≥9.21），`SegmentGEMMWrapper` 走 CUTLASS `GemmGrouped`（SM80+ 通用、支持 `weight_indices` 按段选权重即 LoRA 风格）。
- **前缀和索引**（`m_indptr`/`seg_indptr`）是 grouped GEMM 描述变长批次的统一语言；cuDNN 端还要把它翻译成 `first_token_offset`，CUTLASS 端要翻译成 `all_problems` + 指针/stride 数组。
- CUTLASS grouped kernel 用 `DISPATCH_WEIGHT_LAYOUT`/`DISPATCH_SMEM_CONFIG` 在运行期选权重布局与流水线级数，用 `threadblock_count=4` 调度，A 行主序、B 可列可行、FP32 累加。
- **router GEMM** 是 MoE 路由器的固定形状小 GEMM，靠 `LoopUnroller` 把 M∈[1,16] 在编译期全实例化、运行期零分支；`tinygemm_bf16` 是其通用版，覆盖任意形状的小线性层。
- 两类小 GEMM 都支持 **PDL**（programmatic dependent launch），与上游 kernel 重叠以压低延迟。

## 7. 下一步学习建议

- **走向 MoE 全流程**：本讲只覆盖了「专家 FFN 的分组 GEMM」与「router 小 GEMM」，完整的 MoE 还包含路由（top-k、softmax、DeepSeek-V3 的 group 路由）与量化专家权重。下一单元 u6-l1「MoE 基础与统一 API」会把这些串起来，重点读 `flashinfer/fused_moe/api.py`。
- **深入量化分组 GEMM**：本讲的 `grouped_mm_fp4`/`grouped_mm_mxfp8` 只是入口，其块缩放布局（128×4 交织）与 u5-l3 的 FP4 GEMM 同源；建议接着读 `csrc/group_gemm_*_groupwise_sm100.cu` 系列。
- **理解 LoRA 专用内核**：若你对多 LoRA 服务感兴趣，`SegmentGEMMWrapper.weight_indices` 是通用方案，但生产里更常用的是 `flashinfer/fused_moe/bgmv_moe.py` 的 `bgmv_moe_*` 专用内核（batched matrix-multiply for multi-adapter），可对比二者取舍。
- **TVM-FFI 与 torch custom op**：router GEMM 同时用了 `TVM_FFI_DLL_EXPORT_TYPED_FUNC`（[binding](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/csrc/flashinfer_gemm_binding.cu#L34)）与 `@register_custom_op`（[routergemm.py:124](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/gemm/routergemm.py#L124)），这两套机制在 u9-l2「TVM-FFI 绑定」会系统讲解。
