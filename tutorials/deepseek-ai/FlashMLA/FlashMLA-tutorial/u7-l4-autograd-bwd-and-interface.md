# Autograd fwd/bwd 与 dense 接口

## 1. 本讲目标

本讲是 Unit 7 的收尾篇。前面 u7-l1~u7-l3 已经把 SM100 dense MHA prefill 的 CUTLASS 分层、mainloop、tile scheduler 讲透了，但都是「前向」。本讲要把这条 dense prefill 路径补成**可微分**的：读者学完后应当能够——

1. 说清 `FlashAttnVarlenFunc` 这个 `torch.autograd.Function` 在 forward 里保存了哪些张量、backward 里如何复用它们，以及为什么 LSE 不可反传。
2. 手算 `_flash_attn_varlen_backward` 的 `workspace_bytes`，讲清 `dQ_acc` / `sum_OdO` / `scaled_lse` 三块缓冲各占多少字节、为何这样分配。
3. 解释为什么 SM100 bwd 暂不支持 GQA（`num_qo_heads != num_kv_heads`），并能从源码里指出「预留但未启用」的 dKV_acc 分支。
4. 看懂 `dense_prefill_fwd` / `dense_prefill_bwd` 两个 pybind 绑定如何落到 `FMHACutlassSM100Fwd/BwdRun`，以及里面的 `mask_mode` 与 `head_dim` 二维派发。

## 2. 前置知识

- **autograd 与 `torch.autograd.Function`**：PyTorch 的自动微分靠计算图。自定义算子要进图，就继承 `torch.autograd.Function`，实现 `forward`（算前向并把反向需要的中间量用 `ctx.save_for_backward` 存起来）和 `backward`（用存下来的量算梯度）。`forward` 的每个输入位置，`backward` 都要返回一个对应梯度（非张量或不需要梯度的位置返回 `None`）。
- **varlen（变长拼接）**：把一个 batch 里长度不一的序列首尾拼成一根长张量，再用 `cu_seqlens`（前缀和，长度 `b+1`）记录每段起止。这样 batch 内每条序列独立做 attention，却只用一次 kernel 启动。本讲的 dense prefill 默认就是 varlen 形态。
- **LSE（log-sum-exp）**：注意力里 `lse = log Σ exp(S_j)`，是 softmax 的分母取对数，数值稳定地承载了归一化常数。本讲里它既是前向输出之一，也是反向必需的中间量。
- **MLA 开关**（承接 u7-l1）：dense prefill kernel 用一个编译期布尔 `IsMla` 同时切换 tile 形状、problem shape、kernel/mainloop/load。MLA 形状是 `head_dim_qk=192, head_dim_vo=128`（latent 128 + rope 64 拼成 192，value 只取 latent 128）；普通 MHA 是 `128/128`。本讲会看到 fwd 与 bwd 都按这两个组合做 `head_dim` 分派。
- **CUTLASS device 层**（承接 u7-l1）：`FMHA<Kernel>` 走 `can_implement → initialize(Arguments→Params) → run` 三步。bwd 在 device 层之上还多包了一层「三 kernel 串联」，这是本讲的重点之一。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `flash_mla/flash_mla_interface.py` | Python 侧：`_flash_attn_varlen_forward/backward`、`FlashAttnVarlenFunc`、三个 `flash_attn_varlen_*` 便捷封装。本讲的 autograd 与 workspace 计算全在这里。 |
| `csrc/api/api.cpp` | pybind 注册：`dense_prefill_fwd` / `dense_prefill_bwd` 两个绑定。 |
| `csrc/api/dense_fwd.h` | 极薄头文件，仅 `#include` common.h 与 SM100 dense interface.h，无独立接口函数。 |
| `csrc/sm100/prefill/dense/interface.h` | 声明 `FMHACutlassSM100FwdRun` / `FMHACutlassSM100BwdRun` 两个入口函数签名。 |
| `csrc/sm100/prefill/dense/fmha_cutlass_fwd_sm100.cu` | 前向入口实现：dtype/`mask_mode`/`head_dim` 派发到 `run_fmha_fwd`。 |
| `csrc/sm100/prefill/dense/fmha_cutlass_bwd_sm100.cu` | 反向入口实现：dtype/`mask_mode`/`head_dim` 派发到 `run_fmha_bwd`，含 bwd 专属 TileShape。 |
| `csrc/sm100/prefill/dense/fmha_cutlass_bwd_sm100.cuh` | `BwdRunner`：装配 problem shape、stride、Arguments，调用 CUTLASS `Sm100FmhaBwd`。 |
| `csrc/sm100/prefill/dense/device/fmha_device_bwd.hpp` | `Sm100FmhaBwd` device 层：三 kernel 串联（sum_OdO → 主 bwd → convert）、workspace 布局与 `get_workspace_size`。 |
| `csrc/sm100/prefill/dense/common/mask.cuh` | `MaskMode` 枚举（kNone=0 / kCausal=1 / kCustom=2）。 |
| `tests/test_fmha_sm100.py` | dense MHA fwd/bwd 正确性与性能测试，含 GQA 跳过 bwd 的逻辑。 |

## 4. 核心概念与源码讲解

### 4.1 FlashAttnVarlenFunc autograd 上下文

#### 4.1.1 概念说明

CUTLASS 写出的 SM100 dense kernel 本身是个「裸」CUDA 算子，只认张量指针与参数，不懂 PyTorch 的计算图。要让它参与 `loss.backward()`，需要一层 `torch.autograd.Function` 把它包成可微算子。FlashMLA 里这层就是 `FlashAttnVarlenFunc`，它的职责只有两件：

- **forward**：调用前向 kernel 算出 `(out, lse)`，把反向要用的张量存进 `ctx`。
- **backward**：从 `ctx` 取出存的张量，调用反向 kernel 算 `(dq, dk, dv)`。

反向需要的中间量是什么？标准 FlashAttention 反向公式要求：前向的 `q/k/v/out/lse`，外加 `cu_seqlens`（知道每段边界）和几个标量（`max_seqlen`、`causal`、`softmax_scale`、`is_varlen`）。`out` 和 `lse` 看似是「输出」，但反向里要复用——`out` 参与 `sum_OdO` 计算（见 4.2），`lse` 决定归一化常数的反向缩放。所以它们必须被存下来。

#### 4.1.2 核心流程

```
flash_attn_varlen_func(q,k,v,cu_q,cu_k,maxq,maxk,causal,scale,is_varlen)
        │  (以及 qkvpacked / kvpacked 两个便捷封装, 都拆成 q,k,v 后汇入此处)
        ▼
FlashAttnVarlenFunc.apply(...)
        │
        ├── forward(ctx, q,k,v,cu_seqlens_qo,cu_seqlens_kv,maxq,maxk,causal,scale,is_varlen)
        │       ├── _flash_attn_varlen_forward(...)  → 调 flash_mla_cuda.dense_prefill_fwd
        │       ├── ctx.save_for_backward(q,k,v,out,lse,cu_seqlens_qo,cu_seqlens_kv)
        │       └── ctx.max_seqlen_qo/kv, causal, softmax_scale, is_varlen  (标量存属性)
        │
        └── backward(ctx, do, dlse)
                ├── del dlse                      # LSE 不支持反向
                ├── q,k,v,out,lse,cu_q,cu_k = ctx.saved_tensors
                ├── _flash_attn_varlen_backward(...)  → 调 flash_mla_cuda.dense_prefill_bwd
                └── return dq,dk,dv, None×7       # 对应 10 个 forward 输入位置
```

`forward` 有 10 个输入（`ctx` 之后），所以 `backward` 必须返回 10 个值：前 3 个是 `dq,dk,dv`，后 7 个对应 `cu_seqlens_qo / cu_seqlens_kv / max_seqlen_qo / max_seqlen_kv / causal / softmax_scale / is_varlen`——这些是整型张量或标量，不需要梯度，一律返回 `None`。

#### 4.1.3 源码精读

前向保存上下文（[flash_mla/flash_mla_interface.py:328-354](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/flash_mla/flash_mla_interface.py#L328-L354)）：

```python
def forward(ctx, q, k, v, cu_seqlens_qo, cu_seqlens_kv,
            max_seqlen_qo, max_seqlen_kv, causal=False,
            softmax_scale=None, is_varlen=True):
    out, lse = _flash_attn_varlen_forward(...)
    ctx.save_for_backward(q, k, v, out, lse, cu_seqlens_qo, cu_seqlens_kv)
    ctx.max_seqlen_qo = max_seqlen_qo
    ctx.max_seqlen_kv = max_seqlen_kv
    ctx.causal = causal
    ctx.softmax_scale = softmax_scale
    ctx.is_varlen = is_varlen
    return out, lse
```

注意 `save_for_backward` 只存张量（`q,k,v,out,lse,cu_seqlens_*`），标量（`max_seqlen_*`、`causal`、`softmax_scale`、`is_varlen`）直接挂到 `ctx` 属性上——这是 PyTorch 的惯例：`save_for_backward` 专用于张量的版本追踪与安全校验，标量走属性。

反向取回并丢弃 `dlse`（[flash_mla/flash_mla_interface.py:356-369](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/flash_mla/flash_mla_interface.py#L356-L369)）：

```python
def backward(ctx, do, dlse):
    del dlse  # LSE doesn't support backward currently
    q, k, v, out, lse, cu_seqlens_qo, cu_seqlens_kv = ctx.saved_tensors
    dq, dk, dv = _flash_attn_varlen_backward(
        do, q, k, v, out, lse, cu_seqlens_qo, cu_seqlens_kv,
        ctx.max_seqlen_qo, ctx.max_seqlen_kv,
        causal=ctx.causal, softmax_scale=ctx.softmax_scale, is_varlen=ctx.is_varlen)
    return dq, dk, dv, None, None, None, None, None, None, None
```

`del dlse` 一行很关键：前向返回了两个输出 `(out, lse)`，所以反向会收到两个上游梯度 `(do, dlse)`。但当前实现**不支持对 lse 求导**，于是显式 `del dlse` 丢弃。后果是：若用户的 loss 依赖 `lse`，反向会因 `dlse` 无法计算而报错——这是「LSE 暂不支持 bwd」的精确含义。

三个便捷封装都汇入 `FlashAttnVarlenFunc.apply`，区别仅在入参打包（[flash_mla/flash_mla_interface.py:372-435](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/flash_mla/flash_mla_interface.py#L372-L435)）。例如 `flash_attn_varlen_qkvpacked_func` 把一个 `qkv` 张量按 `head_dim_qk` 切成 q/k/v 三段再调用，`cu_seqlens` 复用同一份。

#### 4.1.4 代码实践

**实践目标**：确认 autograd 上下文保存的字段与 `backward` 返回的 `None` 个数一一对应。

**操作步骤**：

1. 打开 [flash_mla/flash_mla_interface.py:329-341](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/flash_mla/flash_mla_interface.py#L329-L341)，数 `forward` 在 `ctx` 之后的形参：`q,k,v,cu_seqlens_qo,cu_seqlens_kv,max_seqlen_qo,max_seqlen_kv,causal,softmax_scale,is_varlen`，共 10 个。
2. 打开 [flash_mla/flash_mla_interface.py:369](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/flash_mla/flash_mla_interface.py#L369)，数 `return` 里的元素：`dq, dk, dv` + 7 个 `None`，共 10 个。
3. 给每个 `None` 标注它对应的 forward 形参名（如第 4 个 `None` ↔ `cu_seqlens_qo`）。

**预期结果**：10 个返回值严格对应 10 个输入；前 3 个是真实梯度，后 7 个是 `None`（整型张量或标量参数不可导）。

**待本地验证**：在装有 SM100 GPU 的环境里，给 `lse` 接一个 loss 反传，观察是否报 `element 0 of tensors does not require grad` 之类的错，验证 LSE 不可反传。

#### 4.1.5 小练习与答案

**练习 1**：`forward` 里 `ctx.save_for_backward` 存了 7 个张量，但 `out` 和 `lse` 明明是「输出」，为什么要存？

**答案**：反向公式需要它们。`out = softmax(QK^T)V`，反向里 `sum_OdO = Σ O⊙dO`（见 4.2）要用 `out`；`lse` 是归一化常数 `Z=log Σ exp(S)`，反向缩放softmax 权重时要用。存下来避免反向重算一遍前向。

**练习 2**：如果把 `flash_attn_varlen_func` 的 `causal` 参数改成需要梯度的张量，会发生什么？

**答案**：`backward` 第 8 个返回值是 `None`（对应 `causal`），PyTorch 会据此判定 `causal` 不接受梯度；若上游强行要求 `causal` 的梯度，会报「不支持的求导」错误。布尔开关本就不该可导。

---

### 4.2 bwd workspace 的三块缓冲与字节计算

#### 4.2.1 概念说明

反向比前向「重」得多。前向只算一个 `O`，反向要算 `dQ/dK/dV` 三个梯度，而且 `dQ` 在遍历 K-tile 时是**累加**的（每个 K 块贡献一部分 dQ），用 bf16 累加会丢精度，所以需要一个 **fp32 的 dQ 累加器** `dQ_acc`，最后再转回 bf16 写到 `dQ`。

除了 `dQ_acc`，反向还需要两个逐 query 行的标量向量：

- **`sum_OdO`**：每条 query 行上 `O` 与 `dO` 的点积。它的数学身份是 softmax 反向的「D 对角项」。推导如下：设 `P = softmax(S)`、`O = P·V`，softmax 反向需要 `D_q = Σ_k P_k·dP_k`（P 加权的 dP 行和）。而

  \[ \sum_k P_k\,dP_k = \sum_k P_k \sum_d dO_d V_{k,d} = \sum_d dO_d \underbrace{\sum_k P_k V_{k,d}}_{=O_d} = \sum_d O_d\,dO_d \]

  于是 `sum_OdO = Σ_d O_d·dO_d`，恰好等于 `Σ_k P_k·dP_k`——这正是 softmax Jacobian 反向 `dS = P ⊙ (dP − D)` 里的 `D`。**因为 `O = P·V`，所以可以用 `O` 和 `dO` 直接算出这个量，免得在主 kernel 里再算一遍 `dP`。**

- **`scaled_lse`**：把 `lse` 预乘 `−log2 e`，使主 kernel 内部用 `exp2(scaled_lse) = 2^(−log2 e · lse) = e^(−lse) = 1/Z` 直接得到归一化常数的倒数（base-2 形式，配合 Hopper/Blackwell 上高效的 `exp2f`）。

这三块缓冲（`dQ_acc`、`sum_OdO`、`scaled_lse`）就是 bwd workspace 的全部内容（GQA 下的第四块见 4.2.3 末尾）。

#### 4.2.2 核心流程

`Sm100FmhaBwd` device 层把反向拆成**三个 kernel 串联**（[csrc/sm100/prefill/dense/device/fmha_device_bwd.hpp:287-312](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/device/fmha_device_bwd.hpp#L287-L312)）：

```
1) op_sum_OdO.run()      →  读 O,dO,lse  写 sum_OdO, scaled_lse     (辅助 kernel)
2) cudaMemsetAsync(dQ_acc, 0, dQ_acc_size)                          (清零 fp32 累加器)
3) op.run()              →  读 q,k,v,lse,sum_OdO,scaled_lse  写 dQ_acc,dK,dV  (主 bwd kernel)
4) op_convert.run()      →  读 dQ_acc(fp32)  写 dQ(bf16)            (精度下转换 kernel)
```

workspace 在缓冲区里的排列顺序由 `initialize` 决定（[csrc/sm100/prefill/dense/device/fmha_device_bwd.hpp:267-282](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/device/fmha_device_bwd.hpp#L267-L282)）：先 `sum_OdO`，再 `scaled_lse`，最后 `dQ_acc`。Python 侧只负责分配**足够大**的一段连续 `uint8` 缓冲，具体怎么切分由 CUTLASS 自己用指针递推完成。

#### 4.2.3 源码精读

Python 侧的 workspace 计算（[flash_mla/flash_mla_interface.py:297-304](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/flash_mla/flash_mla_interface.py#L297-L304)）：

```python
max_seqlen_qo_aligned = (max_seqlen_qo + 7) // 8 * 8
bs = cu_seqlens_qo.shape[0] - 1
workspace_bytes = 0
workspace_bytes += 4 * bs * max_seqlen_qo_aligned * num_qo_heads * head_dim_qk          # dQ_acc
workspace_bytes += 4 * max_seqlen_qo_aligned * bs * num_qo_heads * 2                    # sum_OdO and scaled_lse
if num_qo_heads != num_kv_heads:
    workspace_bytes += 2 * kv_total_len * num_kv_heads ... # 见下方说明
```

逐项拆解（`4` = `sizeof(float)`，`2` = `sizeof(bf16)`）：

| 缓冲 | 字节数 | 元素类型 | 形状（逻辑） | 含义 |
| --- | --- | --- | --- | --- |
| `dQ_acc` | `4 × bs × Q_aligned × H_qo × D_qk` | fp32 | `[B, H, Q_aligned, D]` | dQ 的 fp32 累加器，主 kernel 写、convert kernel 读 |
| `sum_OdO` | `4 × Q_aligned × bs × H_qo` | fp32 | `[B, H, Q_aligned]` | softmax 反向 D 对角项 = `Σ O⊙dO` |
| `scaled_lse` | `4 × Q_aligned × bs × H_qo` | fp32 | `[B, H, Q_aligned]` | `lse × (−log2 e)`，主 kernel 用 `exp2` 取 1/Z |

其中 `Q_aligned = (max_seqlen_qo+7)//8*8` 是把 batch 内最长 q 序列向上对齐到 8。为什么用 **per-batch max** 而不是 `total_q`？因为 CUTLASS device 层把 varlen 当成「B 个固定大小 `max_seqlen_q` 的独立问题」来排布 workspace（见 [fmha_device_bwd.hpp:222-233](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/device/fmha_device_bwd.hpp#L222-L233) 里 `Q = round_up(Q_, 8)` 与 `B*H*Q`），每个 batch 槽位预留 `max_seqlen_q` 行，短序列的尾部是填充。这与 CUTLASS 的 `get_workspace_size` 完全对齐：

```cpp
// device/fmha_device_bwd.hpp:226-233
workspace_bytes += sizeof(ElementAccumulator) * B*H*Q;   // sum_OdO
workspace_bytes += sizeof(ElementAccumulator) * B*H*Q;   // scaled_lse
workspace_bytes += sizeof(ElementAccumulator) * B*H*Q*D; // dQ_acc
```

一个对齐细节：CUTLASS 里 `D = round_up(D, 8)`（[fmha_device_bwd.hpp:224](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/device/fmha_device_bwd.hpp#L224)），而 Python 用的是 `head_dim_qk`（未对齐）。当前只支持 `D_qk ∈ {128, 192}`，都是 8 的倍数，两者一致；若未来加入非 8 倍数的 head_dim，Python 公式会少算，需同步改。

**关于 GQA 的第四块**：Python 里还有一行（[flash_mla/flash_mla_interface.py:302-303](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/flash_mla/flash_mla_interface.py#L302-L303)）：

```python
if num_qo_heads != num_kv_heads:
    workspace_bytes += 2 * kv_total_len * num_qo_heads * (head_dim_qk + head_dim_vo)  # dKV_acc
```

这是为 GQA（多 Q 头共享一个 KV 头）**预留**的 `dK_acc/dV_acc` 缓冲（bf16，故 `2` 字节；`dK+dV` 拼一起故 `D_qk+D_vo`；按 `num_qo_heads` 展开因为每个 Q 头各需一份再归约）。但这一分支**当前是死代码**——因为在它之前的 [flash_mla/flash_mla_interface.py:282-284](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/flash_mla/flash_mla_interface.py#L282-L284) 已经对 GQA 直接 `raise ValueError`：

```python
# TODO: fix bwd GQA
if num_qo_heads != num_kv_heads:
    raise ValueError(f"SM100 bwd doesn't support GQA now. ...")
```

而 CUTLASS device 层的 `get_workspace_size` 也只分配 `sum_OdO/scaled_lse/dQ_acc` 三块（[fmha_device_bwd.hpp:226-233](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/device/fmha_device_bwd.hpp#L226-L233)），**完全没有 dKV_acc**。也就是说：Python 那行 `dKV_acc` 是为「未来修复 GQA」准备的占位，但底层 kernel 还没消费它。

**为什么 bwd 暂不支持 GQA**：GQA 下 `num_qo_heads > num_kv_heads`，一组 Q 头共享同一份 K/V，反向算 `dK/dV` 时必须把组内所有 Q 头的贡献累加起来。这要么需要 per-Q-head 的独立累加器（即上面预留的 `dKV_acc`，且为省显存降到 bf16），要么需要原子归约；当前 SM100 bwd kernel（`Sm100FmhaBwdKernelTmaWarpSpecialized` / `Sm100FmhaBwdMlaKernelTmaWarpSpecialized`）还没实现这套分组累加逻辑，device 层也假设 `H = num_qo_heads` 直接当 KV 头数用。因此用 `ValueError` 把 GQA 显式挡在门外，测试里也对应把 `has_bwd` 关掉（见 4.4.4）。顺带一提：**前向是支持 GQA 的**（前向没有这个检查），所以 GQA 是「只能前向、不能反传」的非对称限制。

作为对比，前向的 workspace 是写死的 32 MiB（[flash_mla/flash_mla_interface.py:241](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/flash_mla/flash_mla_interface.py#L241)），因为前向 CUTLASS kernel 只需要少量调度状态、与问题规模无关；bwd 的 workspace 随 `B·H·Q·D` 增长，必须按公式精确分配。

#### 4.2.4 代码实践（本讲主实践）

**实践目标**：追踪 `_flash_attn_varlen_backward` 的 `workspace_bytes` 计算，对给定 shape 手算三块缓冲各占多少字节，并解释 GQA 限制。

**操作步骤**：

1. 打开 [flash_mla/flash_mla_interface.py:261-325](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/flash_mla/flash_mla_interface.py#L261-L325)，定位到 `workspace_bytes` 三行（L300-L303）。
2. 取一组真实参数（对应 `tests/test_fmha_sm100.py` 量级）：

   ```
   b = 2,  max_seqlen_qo = 1024,  num_qo_heads = 32,  num_kv_heads = 32
   head_dim_qk = 128,  head_dim_vo = 128,  kv_total_len = 2048
   ```

   即普通 MHA、非 GQA。
3. 手算：
   - `Q_aligned = (1024+7)//8*8 = 1024`
   - `dQ_acc = 4 × 2 × 1024 × 32 × 128 = 33,554,432 B = 32 MiB`
   - `sum_OdO + scaled_lse = 4 × 1024 × 2 × 32 × 2 = 524,288 B = 512 KiB`
   - GQA 分支：`num_qo_heads == num_kv_heads`，不进入；且即便进入也会先被 L283 的 `ValueError` 拦下。
   - 合计 `workspace_bytes = 34,078,720 B ≈ 32.5 MiB`
4. 把这份缓冲与 CUTLASS `get_workspace_size`（[fmha_device_bwd.hpp:220-234](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/device/fmha_device_bwd.hpp#L220-L234)）对账：`B*H*Q = 2*32*1024 = 65536`，`sum_OdO = 4*65536 = 262144`，`scaled_lse` 同，`dQ_acc = 4*65536*128 = 33554432`，三者相加 = 34,078,720，与 Python 完全一致。
5. 把 `num_kv_heads` 改成 4（GQA，`num_qo_heads=32 != 4`），重读 [L282-L284](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/flash_mla/flash_mla_interface.py#L282-L284)，确认会抛 `SM100 bwd doesn't support GQA now`，根本到不了 workspace 计算。

**需要观察的现象**：

- `dQ_acc` 一项就占了 workspace 的绝大多数（本例 32/32.5 ≈ 98.6%），因为它是 `B·H·Q·D` 四维 fp32，而另两块只是 `B·H·Q` 三维标量向量。
- 改大 `max_seqlen_qo` 或 `num_qo_heads` 时，`dQ_acc` 线性增长；改 `head_dim_qk` 同理。

**预期结果**：手算 32.5 MiB 与 CUTLASS `get_workspace_size` 对账一致；GQA 在 L283 被拦下。

**待本地验证**：在 SM100 上跑 `pytest tests/test_fmha_sm100.py`，用 `nsys` 或在 [L304](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/flash_mla/flash_mla_interface.py#L304) 处 `print(workspace_bytes)` 打印实际分配值，与手算对比。

#### 4.2.5 小练习与答案

**练习 1**：把上面例子里的 `max_seqlen_qo` 从 1024 改成 4096、`num_qo_heads` 改成 128（仍 MHA），`dQ_acc` 变成多少？

**答案**：`dQ_acc = 4 × 2 × 4096 × 128 × 128 = 536,870,912 B = 512 MiB`。可见 bwd workspace 在真实大模型量级会到几百 MiB 量级，这也是它必须精确计算、不能像前向那样写死 32 MiB 的原因。

**练习 2**：为什么 `dQ_acc` 用 fp32，而 `dK/dV` 直接写 bf16（不需要单独 acc）？

**答案**：`dQ` 在主 kernel 遍历 K-tile 时被多次累加（每个 K 块贡献一部分），累加次数多、bf16 累加误差大，所以用 fp32 累加器 `dQ_acc` 收集，最后由 convert kernel 转成 bf16。`dK/dV` 在主流水里由各自的累加器（CUTLASS 内部 fp32 accumulator）直接收敛后一次性写出 bf16，不需要额外的外部 fp32 缓冲。

**练习 3**：`scaled_lse = lse × (−log2 e)`，为什么要在辅助 kernel 里预算这一步？

**答案**：主 bwd kernel 内部用 base-2 指数 `exp2f`（Hopper/Blackwell 上比 `expf` 便宜），需要 `2^(−log2 e · lse) = e^(−lse) = 1/Z`。把乘 `−log2 e` 提前到 `op_sum_OdO` 这个轻量辅助 kernel，主 kernel 里就只剩一次 `exp2f`，省去主热路径里的乘法与精度转换。

---

### 4.3 dense_prefill_fwd / bwd 接口与 mask_mode 派发

#### 4.3.1 概念说明

`FlashAttnVarlenFunc` 在 Python 侧最终调到 `flash_mla_cuda.dense_prefill_fwd` / `dense_prefill_bwd`。这两个名字是 pybind 绑定，背后直接指向 C++ 的 `FMHACutlassSM100FwdRun` / `FMHACutlassSM100BwdRun`。

这里有一个与 decode 路径（u3-l4）显著不同的设计：decode 路径在 `csrc/api/dense_decode.h` 里有一个独立的「校验+编排」接口函数（`dense_attn_decode_interface`），而 dense prefill 路径**没有这样的中间接口函数**——[csrc/api/dense_fwd.h](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/dense_fwd.h#L1-L5) 只有 5 行，仅 `#include` common.h 与 interface.h：

```cpp
#pragma once
#include "common.h"
#include "sm100/prefill/dense/interface.h"
```

pybind 直接把绑定指到 SM100 dense 的入口函数（[csrc/api/api.cpp:13-14](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/api.cpp#L13-L14)）：

```cpp
m.def("dense_prefill_fwd", &FMHACutlassSM100FwdRun);
m.def("dense_prefill_bwd", &FMHACutlassSM100BwdRun);
```

所以对 dense prefill 而言，「接口层」就是 `FMHACutlassSM100Fwd/BwdRun` 这两个 `.cu` 文件本身——校验与派发都写在这里面。

#### 4.3.2 核心流程

两个 Run 函数的派发是同一个套路，只是 fwd/bwd 用的掩码类型不同：

```
FMHACutlassSM100FwdRun / BwdRun(workspace, q,k,v,...,mask_mode_code,...)
  ├── CUDAGuard            (锁定 device)
  ├── 读 scalar_type / head_dim_qk / head_dim_vo
  ├── MaskMode mode = static_cast<MaskMode>(mask_mode_code)   // 0=None, 1=Causal
  ├── dtype 分支: 仅支持 bf16 in/out, 否则 FLASH_MLA_ASSERT(false)
  ├── apply_config: mask × varlen 二维派发为编译期类型
  │     causal? → CausalMask / CausalForBackwardMask
  │     else    → ResidualMask / ResidualMaskForBackward
  │     varlen? → true_type / false_type
  └── head_dim 二维派发 → call_run_fmha_fwd/bwd(Mla=true/false_type, ...)
```

`mask_mode_code` 来自 Python 的 `mask_mode_code = 1 if causal else 0`（[flash_mla_interface.py:231](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/flash_mla/flash_mla_interface.py#L231) 与 [L286](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/flash_mla/flash_mla_interface.py#L286)），对应 `MaskMode::kCausal(1)` / `kNone(0)`（[common/mask.cuh:3-7](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/common/mask.cuh#L3-L7)）。

#### 4.3.3 源码精读

前向的 mask+varlen 派发（[fmha_cutlass_fwd_sm100.cu:49-63](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/fmha_cutlass_fwd_sm100.cu#L49-L63)）：

```cpp
auto apply_config = [&](auto fn) {
  if (mask_mode == MaskMode::kCausal) {
    if (is_varlen) fn(CausalMask<false>{}, cute::true_type{}, Element{}, ElementOut{});
    else           fn(CausalMask<false>{}, cute::false_type{}, Element{}, ElementOut{});
  } else {
    if (is_varlen) fn(ResidualMask{}, cute::true_type{}, Element{}, ElementOut{});
    else           fn(ResidualMask{}, cute::false_type{}, Element{}, ElementOut{});
  }
};
```

反向的派发结构完全一致，但掩码类型换成 **bwd 专属**（[fmha_cutlass_bwd_sm100.cu:47-62](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/fmha_cutlass_bwd_sm100.cu#L47-L62)）：

```cpp
if (mask_mode == MaskMode::kCausal) {
  if (is_varlen) fn(CausalForBackwardMask<false>{}, cute::true_type{}, ...);
  else           fn(CausalForBackwardMask<false>{}, cute::false_type{}, ...);
} else {
  if (is_varlen) fn(ResidualMaskForBackward{}, cute::true_type{}, ...);
  else           fn(ResidualMaskForBackward{}, cute::false_type{}, ...);
}
```

为什么 bwd 要用 `CausalForBackwardMask` / `ResidualMaskForBackward` 而不是复用前向的 `CausalMask` / `ResidualMask`？因为反向遍历 tile 的方向与掩码语义不同：前向按 Q 行 × K 列算上三角被掩掉；反向算 `dK/dV` 时要把 K 当作「行」、Q 当作「列」，掩码的因果方向翻转。bwd 专属掩码类型封装了这种翻转，让主 kernel 内部仍可用同一套 `apply_mask` 调用。这与 u7-l3 讲的「MaskMode→编译期类型消除分支」一脉相承，只是 bwd 多了一套对应类型。

`apply_config` 用「立即调用的 lambda（IIFE）」把运行时的 `mask × varlen` 组合编译期化——和 u2-l3 的 `DISPATCH_*` 宏同构，只是这里手写 lambda 而非宏。每种组合都会实例化出一份独立的模板特化，运行时只走一条分支。

#### 4.3.4 代码实践

**实践目标**：对比 fwd 与 bwd 的掩码类型，理解 bwd 专属掩码的存在意义。

**操作步骤**：

1. 并排打开 [fmha_cutlass_fwd_sm100.cu:49-63](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/fmha_cutlass_fwd_sm100.cu#L49-L63) 与 [fmha_cutlass_bwd_sm100.cu:47-62](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/fmha_cutlass_bwd_sm100.cu#L47-L62)。
2. 列出两表：

   | mask_mode \ 方向 | fwd | bwd |
   | --- | --- | --- |
   | kCausal(1) | `CausalMask<false>` | `CausalForBackwardMask<false>` |
   | kNone(0) | `ResidualMask` | `ResidualMaskForBackward` |

3. 在 `csrc/sm100/prefill/dense/common/mask.cuh` 或 collective 目录里搜索 `CausalForBackwardMask` 的定义，阅读它的 `apply_mask`，对比 `CausalMask` 的 `apply_mask`，看掩码判定方向是否翻转。

**预期结果**：bwd 用独立掩码类型，使主 kernel 代码不感知「方向翻转」，把翻转封装在掩码对象里。

**待本地验证**：若 `CausalForBackwardMask` 定义不在 mask.cuh，用 `Grep` 在 `csrc/sm100/prefill/dense/` 下定位其头文件并阅读。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `dense_fwd.h` 不像 `dense_decode.h` 那样写一个独立的接口函数，而是直接把绑定指到 `FMHACutlassSM100FwdRun`？

**答案**：dense prefill 直接基于 CUTLASS，校验（dtype/shape/mask）与派发逻辑天然写在 `FMHACutlassSM100FwdRun` 里就够了，不需要再包一层；而 decode 路径有 split-KV、sched_meta、combine 等编排逻辑，必须有一个独立接口函数串联。dense_fwd.h 存在主要是为了 `api.cpp` 的 include 对称与命名空间整洁。

**练习 2**：`mask_mode_code` 取值 2（`kCustom`）会发生什么？

**答案**：`apply_config` 的 `if/else` 只覆盖 `kCausal` 与 `else`，`kCustom(2)` 会落入 `else` 分支被当 `ResidualMask` 处理。当前 Python 端只产生 0/1，所以 `kCustom` 实际未使用。

---

### 4.4 head_dim 分派与 MLA 开关

#### 4.4.1 概念说明

`mask_mode` 之外，第二个派发维度是 `head_dim`。dense prefill 支持两种形状：

- `(head_dim_qk=192, head_dim_vo=128)`：**MLA** 模式（latent 128 + rope 64 = 192，value 只取 latent 128）。对应 DeepSeek 风格的潜在注意力。
- `(head_dim_qk=128, head_dim_vo=128)`：**普通 MHA** 模式。

这个分派与 u7-l1 讲的「MLA 开关」是同一件事：一个编译期布尔 `IsMla` 同时切换 tile 形状、problem shape、kernel 选择、mainloop、load collective。fwd 与 bwd 都按这两个组合做 `if/else` 派发，把 `IsMla` 钉成 `true_type`/`false_type` 传进模板。

#### 4.4.2 核心流程

```
head_dim_qk==192 && head_dim_vo==128  →  Mla=true_type   (MLA)
head_dim_qk==128 && head_dim_vo==128  →  Mla=false_type  (普通 MHA)
其他                                   →  std::cout "No kernel instantiated ..."
```

在 bwd 里，`IsMla` 还决定 `TileShape`（[fmha_cutlass_bwd_sm100.cu:21](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/fmha_cutlass_bwd_sm100.cu#L21)）：

```cpp
using TileShape = std::conditional_t<IsMla, Shape<_64,_128,_192,_128>, Shape<_128,_128,_128,_128>>;
```

即 MLA 用 `Shape<_64,_128,_192,_128>`，普通 MHA 用 `Shape<_128,_128,_128,_128>`。这个四元组是 `(Q_tile, K_tile, D_qk, D_vo)` 的 CUTLASS tile 描述——MLA 的 `D_qk=192` 直接体现在 tile 里。

再往下，`IsMla` 在 device 层选择 kernel（[fmha_cutlass_bwd_sm100.cuh:103-115](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/fmha_cutlass_bwd_sm100.cuh#L103-L115)）：

```cpp
using OperationMha = cutlass::fmha::device::FMHA<
    cutlass::fmha::kernel::Sm100FmhaBwdKernelTmaWarpSpecialized<...>>;
using OperationMla = cutlass::fmha::device::FMHA<
    cutlass::fmha::kernel::Sm100FmhaBwdMlaKernelTmaWarpSpecialized<...>>;
using Operation = std::conditional_t<IsMla, OperationMla, OperationMha>;
```

MLA 与普通 MHA 各有独立的 bwd kernel 类（`...MlaKernel...` vs `...Kernel...`），因为 MLA 的 K/V 同源、head_dim 非对称，mainloop 与 load 都要专门处理（承接 u7-l2）。

#### 4.4.3 源码精读

bwd 的 head_dim 派发（[fmha_cutlass_bwd_sm100.cu:64-78](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/fmha_cutlass_bwd_sm100.cu#L64-L78)）：

```cpp
apply_config([&](auto mask, auto varlen, auto in, auto out) {
  if (head_dim_qk == 192 && head_dim_vo == 128) {
    call_run_fmha_bwd(mask, varlen, in, out, true_type{}, ...);   // MLA
  } else if (head_dim_qk == 128 && head_dim_vo == 128) {
    call_run_fmha_bwd(mask, varlen, in, out, false_type{}, ...);  // 普通 MHA
  } else {
    std::cout << "No kernel instantiated for head_dim_qk=" << head_dim_qk
              << " head_dim_vo=" << head_dim_vo << std::endl;
  }
});
```

fwd 的对应代码结构完全一致（[fmha_cutlass_fwd_sm100.cu:65-78](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/fmha_cutlass_fwd_sm100.cu#L65-L78)）。注意「未实例化」时只 `std::cout` 打印一行而不抛异常——这是一种宽松处理，意味着调用方传错 head_dim 会静默走到 `FLASH_MLA_ASSERT(false)`（dtype 分支）或后续 CUTLASS `can_implement` 失败。

`BwdRunner::run` 装配 `Arguments` 后调用 CUTLASS device 层三件套（[fmha_cutlass_bwd_sm100.cuh:185-187](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/fmha_cutlass_bwd_sm100.cuh#L185-L187)）：

```cpp
CUTLASS_CHECK(op.can_implement(arguments));
CUTLASS_CHECK(op.initialize(arguments, workspace_ptr));
CUTLASS_CHECK(op.run(at::cuda::getCurrentCUDAStream()));
```

`op.run` 内部就是 4.2 讲的三 kernel 串联（sum_OdO → memset dQ_acc → 主 bwd → convert）。`can_implement` 会分别校验三个子 kernel（[fmha_device_bwd.hpp:197-217](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/device/fmha_device_bwd.hpp#L197-L217)），任一不满足即返回错误码。

#### 4.4.4 代码实践

**实践目标**：把 head_dim 派发与测试用例对上，理解测试如何覆盖 MLA / 普通 MHA / GQA 三类。

**操作步骤**：

1. 打开 [tests/test_fmha_sm100.py:176-184](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/test_fmha_sm100.py#L176-L184)，看测试矩阵：

   ```python
   for (h, h_k) in [(128, 128), (32, 4)]:
       if h != h_k: has_bwd = False      # (32,4) GQA → 关闭 bwd
       else:        has_bwd = True
       for (d, dv) in [(128, 128), (192, 128)]:
           for causal in [False, True]:
               test_flash_attention(..., has_bwd, ...)
   ```

2. 把四个 `(d,dv)` × `(h,h_k)` 组合映射到 kernel 路径：

   | (d,dv) | (h,h_k) | fwd kernel | bwd kernel |
   | --- | --- | --- | --- |
   | (192,128) | (128,128) | MLA fwd | MLA bwd（`Sm100FmhaBwdMlaKernel...`） |
   | (128,128) | (128,128) | MHA fwd | MHA bwd（`Sm100FmhaBwdKernel...`） |
   | (192,128) | (32,4) | MLA fwd | **不跑**（GQA） |
   | (128,128) | (32,4) | MHA fwd | **不跑**（GQA） |

3. 对 `(192,128)+(128,128)` 这格，回看 [fmha_cutlass_bwd_sm100.cu:21](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/fmha_cutlass_bwd_sm100.cu#L21)，确认 `TileShape = Shape<_64,_128,_192,_128>` 被选中。

**需要观察的现象**：

- `(32,4)` 即 GQA，测试把 `has_bwd=False`，只跑前向正确性——这正是 4.2 讲的「前向支持 GQA、bwd 不支持」在测试侧的体现。
- `causal=True/False` 两轮都跑，覆盖 `CausalForBackwardMask` 与 `ResidualMaskForBackward` 两条 bwd 掩码路径。

**预期结果**：测试矩阵的 4 格能完整对应到 fwd/bwd 的 `head_dim` 与 mask 派发分支；GQA 格的 bwd 被测试主动跳过。

**待本地验证**：在 SM100 上 `python tests/test_fmha_sm100.py`，观察 `(32,4)` 组合是否只打印 `fwd` 计时、不打印 `bwd` 计时。

#### 4.4.5 小练习与答案

**练习 1**：MLA 的 bwd `TileShape` 是 `Shape<_64,_128,_192,_128>`，普通 MHA 是 `Shape<_128,_128,_128,_128>`。为什么 MLA 的第一个元素（Q_tile）是 64 而不是 128？

**答案**：MLA 的 `D_qk=192` 比 MHA 的 128 大，单个 tile 的寄存器/smem 开销更高；把 Q_tile 从 128 缩到 64 是为了在 MLA 的非对称大 head_dim 下保持 tile 总量不爆 smem/寄存器，维持占用率。这是「MLA 开关同时调 tile 形状」的具体落点。

**练习 2**：如果有人想加 `head_dim_qk=256` 的支持，要改哪些地方？

**答案**：至少要在 [fmha_cutlass_fwd_sm100.cu:65-78](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/fmha_cutlass_fwd_sm100.cu#L65-L78) 与 [fmha_cutlass_bwd_sm100.cu:64-78](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/fmha_cutlass_bwd_sm100.cu#L64-L78) 加一个 `else if` 分支并选一个 `Mla` 类型；为它实例化对应的 mainloop/kernel 模板；并确认 workspace 公式里 `D = round_up(D,8)` 与 Python `head_dim_qk` 仍对齐（256 是 8 倍数，OK）。本质上就是给「MLA 开关」再加一档。

---

## 5. 综合实践

把本讲四个模块串起来：写一个最小脚本，用 `flash_attn_varlen_func` 跑一次前向+反向，并打印 `_flash_attn_varlen_backward` 为该 shape 计算出的 `workspace_bytes`，再与手算对账。以下为「源码阅读型 + 可运行骨架」实践（无 GPU 时也能读完并手算）。

```python
# 示例代码：综合实践骨架（需 SM100 GPU + 已安装 flash_mla.cuda 方可实际运行）
import torch
from flash_mla import flash_attn_varlen_func
import flash_mla.flash_mla_interface as iface

# 1. 选一组普通 MHA 配置（非 GQA，bwd 可用）
b, h, d, dv = 2, 32, 128, 128
seqlens_q = torch.tensor([1024, 1024], dtype=torch.int32)
seqlens_k = torch.tensor([2048, 2048], dtype=torch.int32)
cu_q = torch.cumsum(torch.nn.functional.pad(seqlens_q, (1, 0)), 0, dtype=torch.int32)
cu_k = torch.cumsum(torch.nn.functional.pad(seqlens_k, (1, 0)), 0, dtype=torch.int32)
total_q, total_k = seqlens_q.sum().item(), seqlens_k.sum().item()

q = torch.randn(total_q, h, d, device="cuda", dtype=torch.bfloat16, requires_grad=True)
k = torch.randn(total_k, h, d, device="cuda", dtype=torch.bfloat16, requires_grad=True)
v = torch.randn(total_k, h, dv, device="cuda", dtype=torch.bfloat16, requires_grad=True)
grad_out = torch.randn(total_q, h, dv, device="cuda", dtype=torch.bfloat16)

# 2. 前向 + 反向（触发 4.1 的 autograd 与 4.3/4.4 的派发）
out, lse = flash_attn_varlen_func(q, k, v, cu_q, cu_k, 1024, 2048, causal=True, is_varlen=True)
out.backward(grad_out)
print("dq/dk/dv 非空:", q.grad is not None, k.grad is not None, v.grad is not None)

# 3. 手算 bwd workspace（4.2）
bs = cu_q.shape[0] - 1
Q_aligned = (1024 + 7) // 8 * 8
dQ_acc = 4 * bs * Q_aligned * h * d
sum_odo_scaled_lse = 4 * Q_aligned * bs * h * 2
print(f"手算 workspace_bytes = {dQ_acc + sum_odo_scaled_lse} (dQ_acc={dQ_acc}, sum_OdO+scaled_lse={sum_odo_scaled_lse})")

# 4. 验证 LSE 不可反传（4.1）：给 lse 接 loss 应报错
#    lse.sum().backward()  # 取消注释应抛错
```

**操作步骤**：

1. 阅读骨架，标注每一步对应本讲哪个模块（前向→4.3/4.4 派发；`backward`→4.1 autograd + 4.2 workspace；手算→4.2）。
2. 手算 `dQ_acc` 与 `sum_OdO+scaled_lse`，预期分别 `33,554,432` 与 `524,288`，合计 `34,078,720`。
3. 若有 SM100 环境：运行脚本，在 [flash_mla_interface.py:304](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/flash_mla/flash_mla_interface.py#L304) 临时 `print(workspace_bytes)` 对照。
4. 把 `h` 改成 32、`h_k` 用一份独立的 4 头 K/V（GQA），重跑应在前向正常、反向被 `ValueError` 拦下。

**预期结果**：手算与运行值一致；GQA 反向被拦。无 GPU 时完成步骤 1-2 的手算与对账即可，标注「待本地验证」。

## 6. 本讲小结

- `FlashAttnVarlenFunc` 用 `save_for_backward(q,k,v,out,lse,cu_seqlens_*)` 存张量、用 `ctx` 属性存标量；`backward` 返回 `dq,dk,dv` 加 7 个 `None` 对应 10 个输入；`del dlse` 体现 LSE 不可反传。
- bwd workspace 三块：`dQ_acc`（fp32，`4·B·Q·H·D`，占绝大多数）、`sum_OdO`（fp32，`Σ O⊙dO`，softmax 反向 D 对角项）、`scaled_lse`（fp32，`lse·−log2 e`，供 `exp2f` 取 1/Z）；布局与字节与 CUTLASS `get_workspace_size` 逐项对齐。
- bwd 是三 kernel 串联：`op_sum_OdO` → `memset dQ_acc` → 主 bwd → `op_convert`（fp32→bf16）。
- bwd 暂不支持 GQA：L283 `ValueError` 拦截，device 层只分配三块缓冲、无 dKV_acc；Python 里的 `dKV_acc` 分支是预留死代码。前向支持 GQA，形成非对称限制。
- `dense_prefill_fwd/bwd` 经 pybind 直指 `FMHACutlassSM100Fwd/BwdRun`（无中间接口函数）；二维派发 `mask_mode`（前向 `CausalMask/ResidualMask`，bwd 专属 `CausalForBackwardMask/ResidualMaskForBackward`）与 `head_dim`（`192/128`→MLA，`128/128`→MHA，由 `IsMla` 切换 TileShape 与 kernel 类）。

## 7. 下一步学习建议

- **横向对比 decode 的反向**：本讲只讲 dense prefill 的 bwd。decode 路径（u3/u4）目前是推理专用、无反向，可对比两者为何一个需要 autograd、一个不需要。
- **深入 bwd kernel 内部**：本讲到 `Sm100FmhaBwd` device 层为止。若想看 `dQ_acc` 在主 kernel 里如何被 K-tile 循环累加、`sum_OdO` 如何参与 `dS = P⊙(dP−D)`，可读 `csrc/sm100/prefill/dense/kernel/sm100_fmha_bwd_kernel_tma_warpspecialized.hpp` 与 MLA 版本。
- **扩展实践**：仿照 u9-l2 的思路，写一份「为 bwd 新增 GQA 支持」的改动计划——需要让 device 层分配并消费 `dKV_acc`、在 kernel 里实现分组累加，把本讲识别的死代码激活。
- **回到全局**：至此 Unit 7 的 dense prefill fwd/bwd 全链路（u7-l1 分层 → u7-l2 mainloop → u7-l3 scheduler/mask → u7-l4 autograd/接口）已闭环，可进入 Unit 8 的底层工具与测试体系，或 Unit 9 的架构取舍总览。
