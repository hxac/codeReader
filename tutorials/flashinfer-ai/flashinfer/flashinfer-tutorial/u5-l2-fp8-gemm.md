# FP8 GEMM

## 1. 本讲目标

本讲聚焦 FlashInfer 中**FP8 矩阵乘（GEMM）**的两条主线：**per-tensor（逐张量）缩放**与 **groupwise（分组）缩放**，并顺着 u5-l1 建立的「Python wrapper → JIT 模块 → csrc launcher → include kernel」调用链，把 **CUTLASS 后端**从头走到尾。

学完后你应当能够：

- 说出 FP8（E4M3）的数据格式，以及 per-tensor 与 groupwise 两种缩放粒度在数学上与精度上的差异。
- 区分 `mm_fp8` / `bmm_fp8`（per-tensor）与 `gemm_fp8_nt_groupwise`（groupwise）这两组 API 的输入约定与适用场景。
- 复述 CUTLASS 后端从 Python runner 到 `csrc/fp8_gemm_cutlass.cu` 中 `TVM_FFI_DLL_EXPORT_TYPED_FUNC` 导出的完整数据流。
- 用 groupwise FP8 缩放独立完成一次 GEMM，并与 BF16 参考结果对比误差。

## 2. 前置知识

在进入源码前，先建立三个直觉。

### 2.1 FP8（E4M3）是什么

`torch.float8_e4m3fn` 是一种 8 比特浮点格式：1 位符号 + 4 位指数 + 3 位尾数。它的可表示范围约为 \([-448, 448]\)，最小正常数约为 \(1.95\times 10^{-3}\)。相比 BF16（同样是 8 位指数但 7 位尾数、范围 \(\pm 3.4\times 10^{38}\)），FP8 的动态范围窄得多，但每个元素只用 1 个字节，**显存与带宽减半，Tensor Core 吞吐翻倍**。

窄动态范围带来一个直接后果：原始高精度张量直接转成 FP8 会大量「饱和截断」。于是必须先**缩放（scale）**——把张量乘上一个系数让它尽量贴近 FP8 的可表示范围——再转成 FP8；计算时再把缩放的影响还原回去。

### 2.2 缩放粒度：per-tensor vs groupwise

量化误差的大小，取决于「一个缩放系数要覆盖多大的数据集合」。

- **per-tensor（逐张量）**：整张矩阵只用一个标量缩放系数。它取矩阵的全局最大绝对值（amax）来定标。简单、开销小，但如果矩阵里存在少数「离群（outlier）」的大值，全局 amax 会被它们拉高，导致绝大多数小数值被压到 FP8 的低位，精度损失大。
- **groupwise（分组）**：把矩阵切成若干小块（例如 \(1\times 128\) 或 \(128\times 128\)），每块单独算 amax、单独定标。每块只在自己的局部范围内定标，动态范围被充分用满，量化误差显著降低——尤其是 LLM 权重/激活里离群值频繁出现时。

本讲的数学核心就是这两种粒度如何被还原进 GEMM 的累加过程。

### 2.3 缩放系数的方向约定

FlashInfer 里常出现两个互为倒数的量，容易混淆：

- **量化缩放** \(s_q = \text{fp8\_max}/\text{amax}\)：把张量乘上它，使其落入 FP8 范围。
- **反量化缩放** \(s_d = \text{amax}/\text{fp8\_max} = 1/s_q\)：把 FP8 张量乘回真实量级。

源码与文档里说的「scale / descale / inv_scale」一般指 \(s_d\)（反量化方向），因为 kernel 内部先做 FP8 乘加，最后再乘还原系数。下面统一记 scale_a、scale_b 为反量化方向的 \(s_d\)。

## 3. 本讲源码地图

| 文件 | 角色 |
|------|------|
| `flashinfer/gemm/gemm_base.py` | 用户 API 与 runner 调度。本讲涉及 `mm_fp8`、`bmm_fp8`、`gemm_fp8_nt_groupwise`、`fp8_gemm_sm100`、各 CUTLASS runner 工厂函数 |
| `csrc/fp8_gemm_cutlass.cu` | CUTLASS FP8 GEMM 的 **launcher**：校验形状、按 tactic 选 CUTLASS 配置、按 out_dtype 派发模板、经 TVM-FFI 导出 `fp8_gemm` 符号 |
| `include/flashinfer/gemm/fp8_gemm_cutlass.h` | CUTLASS runner 的**接口类与模板声明**（框架无关，只认原始指针） |
| `flashinfer/jit/gemm/core.py` | `gen_gemm_sm100_module_cutlass_fp8` 生成器：渲染 Jinja 实例化多个 CTA tile，装配 `JitSpec` |
| `flashinfer/testing/utils.py` | `quantize_fp8` / `dequantize_fp8` 测试辅助函数，是理解 groupwise 缩放形状的最佳范例 |
| `tests/gemm/test_groupwise_scaled_gemm_fp8.py` | groupwise FP8 GEMM 的正确性测试，本讲实践的依据 |

回顾 u5-l1 的分层：用户调 `mm_fp8`/`gemm_fp8_nt_groupwise`（Python）→ runner 调 JIT 模块符号 → TVM-FFI 路由到 `csrc` 的 `fp8_gemm` → `include` 的 `CutlassFp8GemmRunner<T>::gemm`。数据形态沿 `torch.Tensor → TensorView → T*` 演进，FP8 张量在 `csrc` 一侧被强转为 `__nv_fp8_e4m3*` 裸指针。

## 4. 核心概念与源码讲解

### 4.1 FP8 缩放粒度：per-tensor 的数学与 API

#### 4.1.1 概念说明

per-tensor 缩放是最简单的 FP8 GEMM 形式：矩阵 A、B 各自只有一个标量缩放系数。设量化后的 FP8 矩阵为 \(\tilde A, \tilde B\)，反量化缩放为 \(s_a, s_b\)，则真实矩阵近似为 \(A \approx s_a\tilde A,\ B \approx s_b\tilde B\)。于是

\[
D = A B^\top \approx (s_a\tilde A)(s_b\tilde B)^\top = s_a s_b\,(\tilde A\tilde B^\top)
\]

也就是说：**Tensor Core 只需在 FP8 下算出 \(\tilde A\tilde B^\top\)，最后整体乘一个标量 \(s_a s_b\) 即可**。这正是 per-tensor 路径性能收益的来源——累加全程在 FP8/BF16 寄存器中完成，缩放只发生在 epilogue（收尾）阶段。

#### 4.1.2 核心流程

per-tensor FP8 GEMM 在 FlashInfer 里有两个并列入口：

- `mm_fp8`：二维 `[m,k]×[k,n]`，目前仅 `trtllm_low_latency` 后端，专为**小 M（低 batch 推理）**优化。
- `bmm_fp8`：支持二维与三维（batched），后端可在 `cudnn / cublas / cutlass` 间选择，统一走 `fp8_gemm_sm100` 调度器。

`bmm_fp8` 的 CUTLASS 路径流程：

```
bmm_fp8(A, B, A_scale, B_scale, dtype, backend)
  ├─ 选 backend 列表（"auto" 则取所有可用后端）
  └─ fp8_gemm_sm100(A,B,scale_a,scale_b,out,workspace,runner_names)
        ├─ 按 runner_names 装配 runner：cutlass_sm10x / cutlass_sm12x / cublas / cudnn
        ├─ AutoTuner.choose_one("fp8_gemm", runners, tuning_config, inputs)  # 选最优 tactic
        └─ runner(inputs, tactic)
              └─ module.fp8_gemm(...)  # → TVM-FFI → csrc fp8_gemm → CUTLASS kernel
```

关键点：per-tensor 的 scale 是标量，直接作为两个 `float*` 传给 kernel；后端选优与 tactic 选择交给 `AutoTuner`，调用者无需关心。

#### 4.1.3 源码精读

`mm_fp8` 的签名与 docstring 清楚写明了 per-tensor 约定——`a` 为 `[m,k]` 的 e4m3，`alpha` 是单个标量缩放（等于两个 inv_scale 的乘积），输出只能是 BF16：

[flashinfer/gemm/gemm_base.py:4075-4130](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/gemm/gemm_base.py#L4075-L4130) — 这里 docstring 的示例展示了 per-tensor 的典型用法：先 `to_float8` 得到 `a_inv_s`、`b_inv_s`，再 `alpha = a_inv_s * b_inv_s`，正是上面 \(s_a s_b\) 的工程写法。

`mm_fp8` 的 `trtllm_low_latency` 后端最终落到一个独立的低延迟 GEMM：

[flashinfer/gemm/gemm_base.py:4175-4182](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/gemm/gemm_base.py#L4175-L4182) — 注意它把标量 `alpha` 整体传给 `trtllm_low_latency_gemm`，验证了「FP8 累加 + 收尾乘标量」的设计。

`bmm_fp8` 是多后端的 per-tensor 入口，CUTLASS/cuBLAS/cuDNN 都收口到同一个调度器：

[flashinfer/gemm/gemm_base.py:6666-6678](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/gemm/gemm_base.py#L6666-L6678) — `backend="auto"` 时用 `bmm_fp8.suitable_auto_backends`（由 `@backend_requirement` 装饰器填入，见 u5-l1），否则按用户指定的单一后端，最后统一 `fp8_gemm_sm100(...)`。

`fp8_gemm_sm100` 是调度器本体，按名字装配 runner 并交给 AutoTuner：

[flashinfer/gemm/gemm_base.py:1454-1484](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/gemm/gemm_base.py#L1454-L1484) — `inputs = [a, b, scale_a, scale_b, out, workspace_buffer]`，6 个张量；`tuner.choose_one` 在多个 runner 的多个 tactic 里挑最快的，再 `runner(inputs=inputs, tactic=tactic)` 执行。per-tensor 的 scale_a/scale_b 在此处仍是标量张量。

#### 4.1.4 代码实践

**目标**：直观感受 per-tensor 缩放还原。**步骤**：阅读 `mm_fp8` 的 docstring 示例（上方链接的 4116–4129 行），然后手动用 PyTorch 复现「FP8 累加 + 收尾乘 alpha」的过程。**观察**：FP8 直接乘加会饱和，乘上 alpha 后量级才被还原。**预期**：BF16 参考与 FP8 还原结果最大相对误差在 1e-2 量级。若环境无 Blackwell/Hopper，则标注「待本地验证」。

```python
# 示例代码：手动复现 per-tensor FP8 GEMM 的还原逻辑（不调用 flashinfer）
import torch
fp8_max = torch.finfo(torch.float8_e4m3fn).max
a = torch.randn(128, 256, device="cuda", dtype=torch.bfloat16)
b = torch.randn(512, 256, device="cuda", dtype=torch.bfloat16)
# per-tensor 量化：每张矩阵一个 amax
a_amax = a.abs().max().clamp(min=1e-12); b_amax = b.abs().max().clamp(min=1e-12)
a_fp8 = (a * (fp8_max / a_amax)).to(torch.float8_e4m3fn)
b_fp8 = (b * (fp8_max / b_amax)).to(torch.float8_e4m3fn)
# 还原系数 s_a * s_b = (amax/fp8_max) 的乘积
alpha = (a_amax / fp8_max) * (b_amax / fp8_max)
ref = a @ b.T
approx = alpha.float() * (a_fp8.float() @ b_fp8.float().T)
print((approx - ref).abs().max() / ref.abs().max())  # 相对误差
```

#### 4.1.5 小练习与答案

**Q1**：为什么 per-tensor 路径里，Tensor Core 算完 FP8 乘加后只需要乘一个标量，而不必逐元素还原？

**答**：因为缩放系数对整张矩阵是常数，满足 \(D=s_a s_b(\tilde A\tilde B^\top)\)，标量乘法可提到累加之外，故只需在 epilogue 乘一次。

**Q2**：`mm_fp8` 的 `alpha = a_inv_s * b_inv_s`，其中 `a_inv_s` 是量化系数还是反量化系数？

**答**：是反量化系数（\(s_d=\text{amax}/\text{fp8\_max}\)）。`to_float8` 返回的 `inv_scale = scale.reciprocal()`，其中 `scale=fp8_max/amax` 是量化方向，故 inv_scale 是反量化方向，乘积即还原用的 \(s_a s_b\)。

---

### 4.2 groupwise 缩放：`gemm_fp8_nt_groupwise`

#### 4.2.1 概念说明

groupwise 把 K 维（以及可选的 M、N 维）切成固定大小的块，每块一个缩放系数。FlashInfer 用三元组 `scale_granularity_mnk = (m_g, n_g, k_g)` 描述粒度，默认 `(1, 128, 128)`：A 沿 M 每行一组（\(m_g=1\)）、沿 K 每 128 元素一块；B 沿 N、K 各每 128 一块。

设 K 维被分成 \(G = \lceil K/k_g\rceil\) 段，则输出元素为

\[
D[m,n] = \sum_{g=0}^{G-1} s_a[m,g]\,s_b[\lfloor n/n_g\rfloor,g]\,
\sum_{j=0}^{k_g-1}\tilde A[m,\,k_g g+j]\,\tilde B[n,\,k_g g+j]
\]

与 per-tensor 的区别在于：缩放系数 \(s_a,s_b\) **随 K 段变化**，不能提到整个累加之外，而要在每个 128 元素子累加段后插入一次逐段缩放。这正是 CUTLASS 的「groupwise / blockwise scaled GEMM」mainloop 所做的事——它把缩放嵌进 MMA（matrix multiply-accumulate）流水线的边界。

#### 4.2.2 核心流程

`gemm_fp8_nt_groupwise` 是 groupwise FP8 GEMM 的统一入口，后端可选 `cutlass / trtllm / cutile`，目前仅 Blackwell（SM100/103/110/120/121）支持：

```
gemm_fp8_nt_groupwise(a, b, a_scale, b_scale, scale_major_mode, ...)
  ├─ @backend_requirement 校验：形状、scale_major_mode、scale_granularity_mnk、SM
  ├─ backend="cutlass" 时按 SM 分派：
  │     ├─ SM120/121 → get_gemm_sm120_module().gemm_fp8_nt_groupwise(...)
  │     └─ SM100/103 → get_gemm_sm100_module().gemm_fp8_nt_groupwise(..., mma_sm)
  ├─ backend="trtllm" → get_trtllm_gemm_module().trtllm_gemm(...)
  └─ backend="cutile" → gemm_fp8_nt_groupwise_cutile(...)
```

两个关键参数：

- **`scale_granularity_mnk`**：缩放粒度。CUTLASS 默认 `(1,128,128)`；cuTile v1 仅支持 `(1,128,128)`；TRTLLM 强制 `(1,128,128)` 且要求 `k>=256`。
- **`scale_major_mode`**：缩放张量的主序。`"K"` 表示形如 `(*, k//128)`，`"MN"` 表示 `(k//128, *)`；TRTLLM 只支持 `"MN"`，cuTile 只支持 `"K"`。

> 注意：`a` 是行主序 `[m,k]`，`b` 是列主序 `[n,k]`（函数名里的 `nt` = A normal / B transposed），这是 CUTLASS groupwise kernel 的硬性约定。

#### 4.2.3 源码精读

公共 API 与校验由 `@backend_requirement` 声明式挂载，CUTLASS 后端的能力声明在这里：

[flashinfer/gemm/gemm_base.py:6681-6697](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/gemm/gemm_base.py#L6681-L6697) — `@supported_compute_capability([100, 103, 120, 121])` 限定 CUTLASS groupwise 仅 Blackwell 可用，并要求显式给出 `scale_major_mode`。

公共函数本体在 CUTLASS 分支按 SM 二分派：

[flashinfer/gemm/gemm_base.py:6892-6918](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/gemm/gemm_base.py#L6892-L6918) — SM120/121 与 SM100/103 走各自的 JIT 模块 `gemm_fp8_nt_groupwise`，参数为 `workspace, a, b, a_scale, b_scale, out, *scale_granularity_mnk, scale_major_mode`（SM100 多一个 `mma_sm`，可让 MMA 占用 1 或 2 个 SM，行数多时 2 更快）。

值得专门看的是 SM120 runner——它在内部把「per-tensor 的标量 scale」**扩展成 groupwise 的逐块 scale**，是把 per-tensor 请求降级复用 groupwise kernel 的巧妙设计：

[flashinfer/gemm/gemm_base.py:1006-1045](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/gemm/gemm_base.py#L1006-L1045) — 当 `scale_a.numel()==1`（即 per-tensor 标量）时，用 `.expand(scale_m_count, scale_k_count)` 把标量广播成 `(m, k//128)` 的逐块张量，再以 `(1,128,128)` 粒度调 `gemm_fp8_nt_groupwise`。这说明 **groupwise 是更一般的形态，per-tensor 是它的一个特例**（所有块共用同一系数）。

groupwise 缩放张量的形状到底长什么样？测试辅助函数 `quantize_fp8` 给出了最清晰的范例：

[flashinfer/testing/utils.py:266-361](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/testing/utils.py#L266-L361) — 它把 `x` 按 `tile_shape` 切块，对每块取 `amax/fp8_max`，并进一步量化到 2 的幂次（`torch.pow(2, torch.ceil(torch.log2(...)))`）。注意它对 `scale_major_mode="K"` 与非 K 两种切法用了不同的 `rearrange` 模式，正好对应 CUTLASS 的两种 scale 主序。

#### 4.2.4 代码实践

**目标**：理解 groupwise scale 的形状随粒度变化。**步骤**：阅读 `quantize_fp8`，手算一个 `[m=4, k=512]`、`tile_shape=(1,128)`、`scale_major_mode="K"` 的输入。**观察**：scale 形状应为 `(4, 4)`。**预期**：scale 的每个元素 = 对应 `[1×128]` 块的 `amax/fp8_max` 向上取整到 2 的幂。详细 hands-on 见第 5 节综合实践。

#### 4.2.5 小练习与答案

**Q1**：为何 groupwise 比 per-tensor 精度更高？用一句话解释。

**答**：groupwise 每块用自己的局部 amax 定标，离群大值只影响自己所在的块，不会像 per-tensor 那样把全局 amax 拉高从而压扁其余小块的精度。

**Q2**：`scale_granularity_mnk=(1,128,128)` 中三个分量分别约束哪一维？

**答**：`m_g=1` 约束 M 维逐行一组（A 的行），`n_g=128` 约束 B 沿 N 每 128 列一组，`k_g=128` 约束 K 维（A、B 共享）每 128 元素一段。

---

### 4.3 CUTLASS 后端：csrc launcher 与 include 模板

#### 4.3.1 概念说明

per-tensor 的 CUTLASS 路径（`bmm_fp8` 选 `cutlass` 后端）是观察「Python → csrc → include」分层最干净的样本。CUTLASS（NVIDIA 的 CUDA 线性代数模板库）把 GEMM 写成高度参数化的 C++ 模板：tile 大小、调度策略、数据类型、缩放方式都是模板参数。FlashInfer 用 Jinja 在编译期为若干 CTA tile 配置实例化多个 kernel，再用一个 **tactic 编号** 在运行期切换，由 AutoTuner 挑最快的那个。

CUTLASS runner 是一个 C++ 类模板 `CutlassFp8GemmRunner<T>`（`T` = 输出 dtype），暴露三个能力：`gemm`（执行）、`getWorkspaceSize`（所需 workspace）、`getConfigs`（所有 tactic 配置列表）。它只认原始指针（`__nv_fp8_e4m3*`、`float*`），完全框架无关——这就是 u1-l3 强调的「`include/` 不碰 torch 头文件」红线。

#### 4.3.2 核心流程

CUTLASS FP8 GEMM 的一次调用：

```
Python: runner.forward → module.fp8_gemm(a, b.T, scale_a, scale_b, out, ws, tactic)
   │  (TVM-FFI 路由，张量 → TensorView)
   ▼
csrc/fp8_gemm_cutlass.cu: fp8_gemm(mat1, mat2, scale_a, scale_b, out, ws, tactic)
   ├─ fp8_bmm_impl: 校验形状 → 推 m,n,k,b → getFp8GemmConfig(m,n,k,tactic) 取配置
   ├─ 按 out.dtype 派发：half → runGemm<half>；bf16 → runGemm<__nv_bfloat16>
   └─ runGemm<T>: 校验 workspace 大小 → CutlassFp8GemmRunner<T>.gemm(...) 启动 kernel
```

要点：tactic 是一个整数索引，在 `getConfigs()` 返回的配置列表里查表得到 `CutlassGemmConfig`（含 tile、cluster、schedule 等）；-1 表示「无启发式，默认 tactic 0」，真正选优在 Python 侧 AutoTuner 完成。

#### 4.3.3 源码精读

`include` 一侧定义接口类与模板声明。接口类用纯虚函数锁死 ABI，模板类 `CutlassFp8GemmRunner<T>` 提供具体实现（实现在 `fp8_gemm_cutlass_template.h`）：

[include/flashinfer/gemm/fp8_gemm_cutlass.h:29-58](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/gemm/fp8_gemm_cutlass.h#L29-L58) — 注意 `gemm` 的签名：`A,B` 是 `__nv_fp8_e4m3 const*`，`scale_a,scale_b` 是 `float const*`（per-tensor 标量），`D` 是 `void*`（输出，按 `T` 解释），外加 `m,n,k,b`、配置、workspace、stream。这是「框架无关、只认裸指针」的范本。

`csrc` 一侧是 launcher 与 TVM-FFI 导出。`runGemm<T>` 是核心模板函数：

[csrc/fp8_gemm_cutlass.cu:59-85](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/csrc/fp8_gemm_cutlass.cu#L59-L85) — 它先 `getWorkspaceSize(m,n,k)` 算出所需 workspace；若调用方提供的 buffer 不够大，就 `alloc_tensor` 临时分配一个，否则直接复用。随后把 `mat1/mat2` 的 `data_ptr()` 强转为 `__nv_fp8_e4m3*`、scale 强转为 `float*` 调 `gemmRunner.gemm(...)`。这正是 u1-l3 说的「TensorView → T* 裸指针」演进。

`fp8_bmm_impl` 负责形状推导与 dtype 派发：

[csrc/fp8_gemm_cutlass.cu:87-149](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/csrc/fp8_gemm_cutlass.cu#L87-L149) — 它同时支持 2D（`b=1`）与 3D（batched，`b=batch`），按 `mat1.ndim` 推出 `m,n,k,b`；`tactic==-1` 时退化为 0；最后 `switch(encode_dlpack_dtype(out.dtype()))` 把 fp16/bf16 分别派发到 `runGemm<half>` / `runGemm<__nv_bfloat16>`。

最后经 TVM-FFI 导出符号，Python 侧才能经 `module.fp8_gemm(...)` 调到：

[csrc/fp8_gemm_cutlass.cu:153-170](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/csrc/fp8_gemm_cutlass.cu#L153-L170) — `TVM_FFI_DLL_EXPORT_TYPED_FUNC(fp8_gemm, torch_ext::fp8_gemm)` 把 C++ 函数 `torch_ext::fp8_gemm` 暴露为可被任意框架加载的 `fp8_gemm` 符号；`fp8_gemm_tactic_num` 暴露 tactic 总数（即 `getConfigs().size()`），供 AutoTuner 枚举。

这些符号能被加载，靠的是 JIT 生成器实例化模板。`gen_gemm_sm100_module_cutlass_fp8` 用 Jinja 把 6 组 CTA tile × 2 个 dtype 渲染成 12 份 `.cu`，连同 launcher 一起装配成 `JitSpec`：

[flashinfer/jit/gemm/core.py:256-304](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/gemm/core.py#L256-L304) — `cta_m_n_k_list` 列出 6 组 tile（如 `(128,256,128)`），`supported_major_versions=[10,11,12]` 限定只编译 Blackwell 系列（呼应 u2-l4 的 CompilationContext）。每份 `.cu` 渲染出一个具体 tile 的 `CutlassFp8GemmRunner<T>` 实例化——这就是「tactic 总数 = 12（或更多，含调度变体）」的由来。

Python 侧 SM100 runner 工厂把 tactic 编号传进去：

[flashinfer/gemm/gemm_base.py:1065-1101](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/gemm/gemm_base.py#L1065-L1101) — `forward` 里 `module.fp8_gemm(a, b.transpose(-2,-1), scale_a, scale_b, out, workspace_buffer, tactic)`，注意 `b` 被转置成 CUTLASS 期望的列主序；`get_valid_tactics` 返回 `range(module.fp8_gemm_tactic_num())`，把 tactic 枚举权交给 AutoTuner。

AutoTuner 用的调优配置允许「动态 batch」（M 维可变）：

[flashinfer/gemm/gemm_base.py:1104-1125](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/gemm/gemm_base.py#L1104-L1125) — `DynamicTensorSpec((0,), (-2,), get_hybrid_num_tokens_buckets, ...)` 把第 0 个张量（A，即 M 维）按桶离散化，使同一 tactic 缓存能复用于相近的 batch size；workspace buffer 的尺寸被 wildcard 出缓存键，避免调优中途 resize 造成静默缓存未命中。

#### 4.3.4 代码实践

**目标**：追踪一次 CUTLASS FP8 GEMM 的跨层调用。**步骤**：在 `bmm_fp8` 选 `backend="cutlass"`，设 `FLASHINFER_LOGLEVEL=1`，跑一个小例子。**观察**：日志应显示 `fp8_gemm` 被调用。**预期**：能在 `~/.cache/flashinfer/<ver>/<arch>/generated/gen_gemm_sm100_cutlass_fp8/` 下看到 12 份渲染好的 `fp8_gemm_cutlass_*.cu`（见上方 JIT gen 链接）。无 Blackwell GPU 时标注「待本地验证」。

```python
# 示例代码：触发 CUTLASS FP8 per-tensor BMM（需 SM100+，否则改 backend="cublas"）
import torch, flashinfer
from flashinfer.testing.utils import quantize_fp8
a = torch.randn(2, 128, 256, device="cuda")           # [b, m, k]
b = torch.randn(2, 80, 256, device="cuda")             # [b, n, k]
a_fp8, a_s = quantize_fp8(a.float(), (2,128,1), (1,1,256), "K")  # per-tensor 退化为单 scale
# 注：per-tensor 严格用法见 bmm_fp8 docstring；此处仅供追踪调用链
```

#### 4.3.5 小练习与答案

**Q1**：`csrc/fp8_gemm_cutlass.cu` 里为什么需要同时 `template class CutlassFp8GemmRunner<__nv_bfloat16>` 和 `<half>` 两行显式实例化？

**答**：`CutlassFp8GemmRunner<T>` 是模板，C++ 编译器不会自动实例化用到的所有 `T`。显式实例化确保 bf16 与 fp16 两个版本被编译进 `.so`，运行期 `switch(out.dtype)` 才能找到对应符号。

**Q2**：tactic 编号在 Python 与 csrc 之间是如何对齐的？

**答**：Python 侧 `fp8_gemm_tactic_num()` 返回 `getConfigs().size()`，`get_valid_tactics` 枚举 `range(...)`；csrc 侧 `getFp8GemmConfig(m,n,k,tactic)` 用同一份 `getConfigs()` 列表按下标取出 `CutlassGemmConfig`。两端共享同一个静态配置列表，故编号语义一致。

## 5. 综合实践

本任务把 per-tensor 与 groupwise 两种粒度串起来对比，对应讲义指定的实践任务。依据是 `tests/gemm/test_groupwise_scaled_gemm_fp8.py` 中的 `test_fp8_groupwise_gemm`。

### 5.1 实践目标

用 **groupwise FP8 缩放**做一次 GEMM，与 BF16 参考结果对比最大误差；再额外构造 per-tensor 对照，说明 groupwise 的精度优势。

### 5.2 操作步骤

> 前置：需要 Blackwell（SM100/103/110/120/121）GPU。无此硬件时，第 1–4 步仍可在 CPU 上用 PyTorch 复现量化逻辑做「源码阅读型实践」，标注「待本地验证」。

1. **量化**：对 `a_val [m,k]`、`b_val [n,k]` 用 `flashinfer.testing.utils.quantize_fp8` 做 groupwise 量化，`tile_size=128`、`scale_major_mode="K"`，则 `a_scale` 形状 `(m, k//128)`、`b_scale` 形状 `(n//128, k//128)`（见 [test_groupwise_scaled_gemm_fp8.py:128-138](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/tests/gemm/test_groupwise_scaled_gemm_fp8.py#L128-L138)）。
2. **参考**：`ref = a_dequant @ b_dequant.T`（用 `dequantize_fp8` 还原后做 BF16 GEMM）。
3. **groupwise GEMM**：调 `gemm_fp8_nt_groupwise(a_fp8, b_fp8, a_scale, b_scale, scale_major_mode="K", out_dtype=torch.bfloat16, backend="cutlass")`（见 [test_groupwise_scaled_gemm_fp8.py:147-155](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/tests/gemm/test_groupwise_scaled_gemm_fp8.py#L147-L155)）。
4. **per-tensor 对照**：另用整矩阵 amax 做一次 per-tensor 量化，调 `bmm_fp8` 或用第 4.1.4 节的手动还原。
5. **断言**：`torch.testing.assert_close(c, ref, atol=1e-2, rtol=1e-2)`，并打印两种粒度的最大相对误差。

最小可运行脚本（示例代码）：

```python
# 示例代码：groupwise FP8 GEMM 与 BF16 参考、per-tensor 对照
import math, torch
from einops import einsum
from flashinfer.gemm import gemm_fp8_nt_groupwise
from flashinfer.testing.utils import quantize_fp8, dequantize_fp8

torch.random.manual_seed(0)
m, n, k, tile = 512, 512, 512, 128
a_val = torch.randn((m, k), dtype=torch.float, device="cuda")
b_val = torch.randn((n, k), dtype=torch.float, device="cuda") / math.sqrt(k)

# groupwise 量化
a_fp8, a_scale = quantize_fp8(a_val, (m, k // tile), (1, tile), "K")
b_fp8, b_scale = quantize_fp8(b_val, (n // tile, k // tile), (tile, tile), "K")
ref = einsum(dequantize_fp8(a_fp8, a_scale, "K"),
             dequantize_fp8(b_fp8, b_scale, "K"), "m k, n k -> m n").to(torch.bfloat16)

c = gemm_fp8_nt_groupwise(a_fp8, b_fp8, a_scale, b_scale, "K",
                          out_dtype=torch.bfloat16, backend="cutlass")
print("groupwise max rel err:", (c - ref).abs().max() / ref.abs().max())
torch.testing.assert_close(c, ref, atol=1e-2, rtol=1e-2)
```

### 5.3 观察与预期

- groupwise 输出与 BF16 参考最大相对误差应在 \(10^{-2}\) 量级（测试用 `atol=rtol=1e-2`）。
- 人为向 `a_val` 注入几个离群大值（如 `a_val[0,0] *= 100`）后重做对比：**per-tensor 的相对误差会显著升高，groupwise 几乎不变**——这就是 groupwise 的精度优势所在。

## 6. 本讲小结

- FP8（E4M3）用 1 字节元素换取显存/带宽/吞吐收益，代价是窄动态范围，必须靠**缩放**还原。
- **per-tensor** 整矩阵一个标量缩放，\(D=s_a s_b(\tilde A\tilde B^\top)\)，标量乘法可提到累加外；入口 `mm_fp8`（trtllm 低延迟）、`bmm_fp8`（多后端），CUTLASS 路径经 `fp8_gemm_sm100` 调度。
- **groupwise** 沿 `(m,n,k)` 切块逐块定标，缩放随 K 段变化、嵌进 MMA 边界；入口 `gemm_fp8_nt_groupwise`，粒度 `(1,128,128)`，仅 Blackwell；per-tensor 是其特例（SM120 runner 用 `.expand` 把标量广播成逐块）。
- **CUTLASS 后端**走标准四层：Python runner → `module.fp8_gemm` → csrc `fp8_gemm` launcher（按 tactic 选配置、按 out.dtype 派发模板）→ include `CutlassFp8GemmRunner<T>::gemm`；tactic 由 AutoTuner 在 Jinja 实例化的多组 CTA tile 中选优。
- 框架无关红线一以贯之：`include/fp8_gemm_cutlass.h` 只认 `__nv_fp8_e4m3*`/`float*` 裸指针，torch 相关逻辑全部留在 csrc 与 Python。

## 7. 下一步学习建议

- **向下到 CUTLASS 内部**：阅读 `include/flashinfer/gemm/fp8_gemm_cutlass_template.h` 与 `fp8_gemm_cutlass.jinja`，看 mainloop 如何把 groupwise 缩放嵌进 MMA 流水线。
- **横向到 FP4**：进入 u5-l3「FP4 GEMM」，对比 NVFP4/MXFP4 的 block-scale 重排与本讲的 groupwise 缩放异同。
- **到 MXFP8**：阅读 `mm_mxfp8` / `mxfp8_gemm_sm100` 及其 CUTLASS 生成器（`flashinfer/jit/gemm/core.py` 中 `gen_gemm_sm100_module_cutlass_mxfp8`），理解 32 元素块缩放（MX 格式）与本讲 128 块的区别。
- **到 grouped GEMM**：u5-l4 讲 LoRA/多专家的 grouped GEMM，可结合 `group_gemm_fp8_nt_groupwise` 看 groupwise 缩放如何扩展到分组场景。
