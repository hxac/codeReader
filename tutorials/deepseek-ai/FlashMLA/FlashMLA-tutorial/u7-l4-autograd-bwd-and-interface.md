# Autograd fwd/bwd 与 dense 接口

## 1. 本讲目标

本讲是 Unit 7（CUTLASS Dense MHA Prefill/Backward，SM100）的收尾篇。前面三讲已经把 CUTLASS 的分层结构（u7-l1）、mainloop 与 fusion（u7-l2）、tile scheduler 与 mask（u7-l3）讲清楚了，本讲要把视线拉回「上层调用方」：Python 用户怎么触发 forward/backward，反向传播需要多大的 workspace，以及 C++ 接口如何把运行时的 `mask_mode` 与 `head_dim` 派发到具体的 CUTLASS kernel 模板特化。

学完后你应当能够：

- 说清 `FlashAttnVarlenFunc` 这个 `torch.autograd.Function` 如何保存前向上下文、如何把反向请求转交给底层 kernel，以及为什么 LSE 暂时不支持反向。
- 手算 `_flash_attn_varlen_backward` 的 workspace 字节数，分清 `dQ_acc` / `sum_OdO` / `scaled_lse`（以及尚未启用的 `dKV_acc`）三块各占多少，并与 CUTLASS device 层的 `get_workspace_size` 对齐。
- 看懂 `FMHACutlassSM100FwdRun` / `FMHACutlassSM100BwdRun` 两个接口函数如何用「立即调用的 lambda」把 `mask_mode × is_varlen × head_dim` 编译期化，进而选中 MLA 或普通 MHA 模板。
- 解释为什么 dense prefill 的反向暂时不支持 GQA（`num_qo_heads != num_kv_heads`）。

## 2. 前置知识

在进入源码前，先建立几个本讲要用到的基础概念。本讲假设你已读过 u7-l1（CUTLASS 分层与 MLA 开关）与 u7-l2（mainloop/fusion），这里只补 autograd 与接口层所需的背景。

### 2.1 torch.autograd.Function

PyTorch 的自动微分靠计算图上的 `grad_fn` 串联。要让一段 C++/CUDA kernel 参与 autograd，标准做法是写一个 `torch.autograd.Function` 的子类，实现两个静态方法：

- `forward(ctx, ...)`：算前向，并把反向需要用到的张量通过 `ctx.save_for_backward(...)` 存起来，把非张量标量挂在 `ctx` 属性上。
- `backward(ctx, *grad_outputs)`：接收前向每个输出的梯度，按前向输入的顺序返回每个输入的梯度（非张量输入返回 `None`）。

调用 `MyFunc.apply(...)` 时，PyTorch 自动把 `forward` 的输出接上 `backward`，无需手写反传。FlashMLA 的 dense MHA 前向/反向就是用这套机制包起来的。

### 2.2 varlen（变长拼接）

真实训练里一个 batch 的每条序列长度不同，如果按最长序列 padding 会浪费算力。varlen（variable length）做法是把所有序列沿序列维拼接成一根长张量，再用 `cu_seqlens`（累加长度，首部补 0）记录每条序列的起止：

```
cu_seqlens_q = [0, s_q_0, s_q_0+s_q_1, ...]   # 长度 = batch+1
```

这样 attention 只在每条序列内部计算，跨序列不交互。FlashMLA 的 dense prefill 接口是 varlen 风格的（`is_varlen` 也可关闭走定长 `[B, H, Q, D]`）。

### 2.3 lse（log-sum-exp）

attention 的 softmax 分母取对数就是 lse：\( \mathrm{lse} = \log\sum_k e^{p_k} \)。前向除了输出 `out`，还输出 `lse`，它是反向传播的关键中间量（用来重建 softmax 权重 \(S = \mathrm{softmax}(QK^\top)\)）。FlashMLA 内部用 base-2 计算、对外返回 base-e 的 lse（dense prefill 路径由 CUTLASS 直接产出 base-e）。

### 2.4 MLA 模式 vs 普通 MHA 模式（head_dim 视角）

回顾 u7-l1：dense prefill 在 README 里统称「MHA 模式」，但代码里有一个编译期布尔 `IsMla` 区分两种 head_dim 组合：

- `head_dim_qk == 192, head_dim_vo == 128`：MLA 模式，192 = 128 latent + 64 rope（复合 head_dim），V 只取 latent 的 128 维，对应模板 `IsMla=true`。
- `head_dim_qk == 128, head_dim_vo == 128`：普通 MHA，K/V 同宽 128，对应 `IsMla=false`。

注意这与 decode 路径的「MQA 模式（576/512）」无关——dense prefill 不走 576/512 那条线。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| :--- | :--- |
| `flash_mla/flash_mla_interface.py` | Python 接口层：`_flash_attn_varlen_forward/backward`、`FlashAttnVarlenFunc`、三个 `flash_attn_varlen_*func` 入口 |
| `csrc/api/api.cpp` | pybind 绑定，把 5 个 C++ 函数注册成 `flash_mla.cuda.*` |
| `csrc/api/dense_fwd.h` | dense prefill 接口头文件（仅 include） |
| `csrc/sm100/prefill/dense/interface.h` | `FMHACutlassSM100FwdRun` / `FMHACutlassSM100BwdRun` 函数声明 |
| `csrc/sm100/prefill/dense/fmha_cutlass_fwd_sm100.cu` | fwd 接口实现：mask/head_dim 派发 → `run_fmha_fwd` |
| `csrc/sm100/prefill/dense/fmha_cutlass_bwd_sm100.cu` | bwd 接口实现：mask/head_dim 派发 → `run_fmha_bwd` |
| `csrc/sm100/prefill/dense/fmha_cutlass_fwd_sm100.cuh` | `FwdRunner` 模板与 `run_fmha_fwd`，组装 CUTLASS `FMHA` device op |
| `csrc/sm100/prefill/dense/fmha_cutlass_bwd_sm100.cuh` | `BwdRunner` 模板与 `run_fmha_bwd`，组装 `Sm100FmhaBwd` |
| `csrc/sm100/prefill/dense/device/fmha_device_bwd.hpp` | `Sm100FmhaBwd`：sum_OdO → 主 bwd → convert 三 kernel 串联，含 `get_workspace_size` |
| `csrc/sm100/prefill/dense/collective/fmha_fusion.hpp` | mask 策略对象（`NoMask`/`ResidualMask`/`CausalMask` 及其 ForBackward 变体） |
| `csrc/sm100/prefill/dense/common/mask.cuh` | `MaskMode` 枚举（kNone/kCausal/kCustom） |
| `csrc/api/common.h` | `Arch`、`int64_stride_to_int`、`DISPATCH_*` 宏、`ImplBase`（本讲复用其思路） |

## 4. 核心概念与源码讲解

### 4.1 FlashAttnVarlenFunc autograd

#### 4.1.1 概念说明

`FlashAttnVarlenFunc` 是 dense MHA prefill 对外暴露的 autograd 入口。它本身不含 CUDA 逻辑，只做三件事：

1. 在 `forward` 里调用 `_flash_attn_varlen_forward`（转交 `flash_mla_cuda.dense_prefill_fwd`），把前向需要的张量存进 autograd 上下文。
2. 在 `backward` 里调用 `_flash_attn_varlen_backward`（转交 `flash_mla_cuda.dense_prefill_bwd`），用保存的 `q/k/v/out/lse/cu_seqlens` 算出 `dq/dk/dv`。
3. 丢弃 `lse` 的梯度——当前实现里 LSE 不参与反向。

对外还提供三个糖函数 `flash_attn_varlen_func` / `flash_attn_varlen_qkvpacked_func` / `flash_attn_varlen_kvpacked_func`，分别对应「分开传 q/k/v」「qkv 打包」「kv 打包」三种入参习惯，最终都汇入 `FlashAttnVarlenFunc.apply`。

#### 4.1.2 核心流程

前向流程（`FlashAttnVarlenFunc.forward` → `_flash_attn_varlen_forward`）：

```text
输入 q,k,v,cu_seqlens_qo,cu_seqlens_kv,max_seqlen_qo,max_seqlen_kv,causal,softmax_scale,is_varlen
  │
  ├─ 计算 mask_mode_code = 1 if causal else 0
  ├─ softmax_scale 默认 = head_dim_qk ** -0.5
  ├─ 分配 out [qo_total_len, num_qo_heads, head_dim_vo] (bf16)
  ├─ 分配 lse [num_qo_heads, qo_total_len].T (fp32，seqlen 维连续)
  ├─ 分配 workspace_buffer = 32 MiB (fwd 当前不实际使用，占位)
  ├─ flash_mla_cuda.dense_prefill_fwd(workspace, q,k,v,cu_q,cu_kv,out,lse,
  │                                   mask_mode_code, softmax_scale,
  │                                   max_seqlen_qo, max_seqlen_kv, is_varlen)
  └─ ctx.save_for_backward(q,k,v,out,lse,cu_seqlens_qo,cu_seqlens_kv)
     ctx 保存 max_seqlen_qo/kv, causal, softmax_scale, is_varlen
```

反向流程（`FlashAttnVarlenFunc.backward` → `_flash_attn_varlen_backward`）：

```text
输入 do (out 的梯度), dlse (lse 的梯度，直接丢弃)
  │
  ├─ del dlse  # LSE 暂不支持反向
  ├─ 取回 saved_tensors: q,k,v,out,lse,cu_seqlens_qo,cu_seqlens_kv
  ├─ GQA 检查: num_qo_heads != num_kv_heads → raise ValueError
  ├─ 分配 dq,dk,dv (bf16)
  ├─ 计算 workspace_bytes (见 4.2)
  ├─ flash_mla_cuda.dense_prefill_bwd(workspace, do,q,k,v,o,lse,cu_q,cu_kv,
  │                                   dq,dk,dv, mask_mode_code, softmax_scale,
  │                                   max_seqlen_qo, max_seqlen_kv, is_varlen)
  └─ return dq, dk, dv, None, None, None, None, None, None, None  # 对应 10 个前向输入
```

`backward` 的返回值个数必须等于 `forward` 的输入个数（这里是 10：`q,k,v,cu_seqlens_qo,cu_seqlens_kv,max_seqlen_qo,max_seqlen_kv,causal,softmax_scale,is_varlen`），非张量输入用 `None` 占位。

#### 4.1.3 源码精读

`FlashAttnVarlenFunc` 的 forward/backward 定义在：

[flash_mla/flash_mla_interface.py:328-369](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/flash_mla/flash_mla_interface.py#L328-L369) —— `FlashAttnVarlenFunc` 类，`forward` 调用 `_flash_attn_varlen_forward` 并 `save_for_backward`，`backward` 调用 `_flash_attn_varlen_backward`。

几个关键点：

[flash_mla/flash_mla_interface.py:348](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/flash_mla/flash_mla_interface.py#L348) —— `ctx.save_for_backward(q, k, v, out, lse, cu_seqlens_qo, cu_seqlens_kv)`，反向需要的七张张量都存这里。注意 `max_seqlen_qo/kv`、`causal`、`softmax_scale`、`is_varlen` 是标量，不能进 `save_for_backward`（那个只收张量），所以挂在 `ctx` 属性上：

[flash_mla/flash_mla_interface.py:349-353](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/flash_mla/flash_mla_interface.py#L349-L353) —— 把标量参数存到 `ctx`。

[flash_mla/flash_mla_interface.py:361](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/flash_mla/flash_mla_interface.py#L361) —— `del dlse`，显式丢弃 LSE 的梯度。注释 `# LSE doesn't support backward currently` 说明这是已知限制：底层 bwd kernel 不接收 `dlse`，所以即便上层传了也用不上。

前向张量分配与 kernel 调用：

[flash_mla/flash_mla_interface.py:235-239](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/flash_mla/flash_mla_interface.py#L235-L239) —— `out` 按 `[qo_total_len, num_qo_heads, head_dim_vo]` 分配；`lse` 先建成 `[num_qo_heads, qo_total_len]` 再 `.T`，使其在 seqlen 维（第 0 维）上连续——这正是 CUTLASS 端 `TORCH_CHECK(lse_stride0 == 1)` 要求的布局。

[flash_mla/flash_mla_interface.py:241](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/flash_mla/flash_mla_interface.py#L241) —— fwd 的 workspace 固定 32 MiB。注意 fwd 的 CUTLASS runner 注释里写明「we don't use workspace in current version」（见 4.3.3），所以这 32 MiB 目前只是占位缓冲，真正用到 workspace 的是 bwd。

[flash_mla/flash_mla_interface.py:242-256](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/flash_mla/flash_mla_interface.py#L242-L256) —— 调用 `flash_mla_cuda.dense_prefill_fwd`，参数顺序与 C++ 端 `FMHACutlassSM100FwdRun` 完全对应。

三个糖函数把不同打包方式的输入拆成 `q,k,v` 再 `apply`：

[flash_mla/flash_mla_interface.py:395-412](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/flash_mla/flash_mla_interface.py#L395-L412) —— `flash_attn_varlen_qkvpacked_func` 从 `qkv` 张量切出 q/k/v（按 `head_dim_qk` 切片），`cu_seqlens` 同时作 q 和 kv 的累加长度。

#### 4.1.4 代码实践

**实践目标**：确认 autograd 链路确实把 `out` 的梯度接到 `backward`，并验证 LSE 梯度被丢弃。

**操作步骤**（无 GPU 时为源码阅读型实践）：

1. 阅读 [flash_mla/flash_mla_interface.py:356-369](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/flash_mla/flash_mla_interface.py#L356-L369)，数清 `backward` 返回的元素个数，与前向输入一一对应。
2. 在有 SM100 GPU 的环境里，参考 `tests/test_fmha_sm100.py` 的写法，构造 `q1/k1/v1.requires_grad_()`，调用 `flash_attn_varlen_func(...)`，再 `out.backward(grad_out)`，打印 `q1.grad` 形状。
3. 试着把一个非零 `dlse` 传进去（例如对返回的 `lse` 求和再反传），观察 `lse` 的梯度是否真的影响 `dq/dk/dv`。

**需要观察的现象**：

- `backward` 返回 10 个值，前 3 个是 `dq,dk,dv` 张量，后 7 个是 `None`。
- `q1.grad` 形状与 `q1` 一致。
- 无论 `lse` 上接多大的梯度，`dq/dk/dv` 都不变——因为 `del dlse` 把它丢了。

**预期结果**：autograd 正常产出 `dq/dk/dv`；LSE 的梯度对结果无影响。无 GPU 时「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：`forward` 返回 `(out, lse)` 两个张量，那 `backward` 为什么收到的是 `(do, dlse)` 两个梯度？如果将来要支持 LSE 反向，需要改哪一行？

**答案**：PyTorch 规定 `backward` 的输入梯度数量等于 `forward` 的输出数量。`forward` 输出 `(out, lse)`，所以 `backward` 收 `(do, dlse)`。要支持 LSE 反向，需删除 [flash_mla/flash_mla_interface.py:361](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/flash_mla/flash_mla_interface.py#L361) 的 `del dlse`，并让底层 `dense_prefill_bwd` 接收并使用 `dlse`（当前 C++ 接口签名里没有 `dlse` 参数）。

**练习 2**：`save_for_backward` 里为什么没有 `softmax_scale` 和 `causal`？它们怎么传到反向？

**答案**：`save_for_backward` 只接受需要参与反向计算图的张量；`softmax_scale`、`causal`、`is_varlen`、`max_seqlen_*` 是标量，不是张量，存进去会破坏 autograd 对张量的引用计数管理。它们改挂 `ctx` 属性，见 [flash_mla/flash_mla_interface.py:349-353](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/flash_mla/flash_mla_interface.py#L349-L353)。

### 4.2 bwd workspace 字节计算

#### 4.2.1 概念说明

attention 的反向比前向复杂：除了算 `dQ`，还要算 `dK/dV`，并且 softmax 的非线性要求在反传时重建注意力权重 \(S = \mathrm{softmax}(QK^\top)\)。FlashAttention 的反向算法需要几个 fp32 的中间缓冲：

- **dQ_acc**：`dQ` 的 fp32 累加器。反向里 `dQ` 由多个 K-tile 块的贡献累加而成，而最终输出 `dQ` 是 bf16，所以用一个 fp32 缓冲做高精度累加，最后再转 bf16。
- **sum_OdO**：反向 dQ 公式配套的行级统计量，shape 是 `[B, H, Q]`（每个 query 行一个标量），由 `O` 和 `dO` 点积归约得到。
- **scaled_lse**：被缩放过的 lse，反向里用来重建 softmax 权重，shape 同样是 `[B, H, Q]`。

这三块缓冲由 Python 层预估字节数、一次性分配成一个 `uint8` 大 buffer，再交给 C++ 端按需切分。CUTLASS 的 `Sm100FmhaBwd` device 层在 `initialize` 里把这个 buffer 切成三段分别喂给三个子 kernel。

#### 4.2.2 核心流程

`_flash_attn_varlen_backward` 的 workspace 计算（[flash_mla/flash_mla_interface.py:297-304](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/flash_mla/flash_mla_interface.py#L297-L304)）：

```text
max_seqlen_qo_aligned = ceil_div(max_seqlen_qo, 8) * 8   # 8 对齐
bs = cu_seqlens_qo.shape[0] - 1                           # batch 数
workspace_bytes = 0
  += 4 * bs * max_seqlen_qo_aligned * num_qo_heads * head_dim_qk          # dQ_acc      (fp32)
  += 4 * max_seqlen_qo_aligned * bs * num_qo_heads * 2                    # sum_OdO + scaled_lse (各 fp32，共 2 份)
  if num_qo_heads != num_kv_heads:
      += 2 * kv_total_len * num_qo_heads * (head_dim_qk + head_dim_vo)    # dKV_acc (bf16，GQA 用，当前不可达)
分配 workspace_buffer = empty(workspace_bytes, uint8)
```

三块字节数（设 `Qa = max_seqlen_qo_aligned`，`H = num_qo_heads`，`D = head_dim_qk`，`B = bs`）：

| 缓冲 | 字节数 | 元素类型 | 元素个数 | 对应 CUTLASS 量 |
| :--- | :--- | :--- | :--- | :--- |
| dQ_acc | \(4 \cdot B \cdot Qa \cdot H \cdot D\) | fp32 | \(B \cdot Qa \cdot H \cdot D\) | `sizeof(float)*B*H*Q*D` |
| sum_OdO | \(4 \cdot B \cdot Qa \cdot H\) | fp32 | \(B \cdot Qa \cdot H\) | `sizeof(float)*B*H*Q` |
| scaled_lse | \(4 \cdot B \cdot Qa \cdot H\) | fp32 | \(B \cdot Qa \cdot H\) | `sizeof(float)*B*H*Q` |

其中 `4 = sizeof(float)`。注意 Python 用 `B * max_seqlen_qo_aligned` 作为 Q 的上界估计（每条序列都按最大长度算），而 CUTLASS 用真实总长 `Q = round_up(total_seqlen_q, 8)`。因为 \(B \cdot \max_i s_i \ge \sum_i s_i\)，Python 的估计是安全上界，C buffer 足够 CUTLASS 切分使用。

CUTLASS device 层的对应分配在：

[device/fmha_device_bwd.hpp:220-234](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/device/fmha_device_bwd.hpp#L220-L234) —— `get_workspace_size`：`OdO vector` + `scaled LSE vector` + `FP32 dQ_acc`，三块都是 `sizeof(ElementAccumulator) * B*H*Q`（dQ_acc 多乘一个 D）。

device 层在 `initialize` 里把这段 buffer 切成三段：

[device/fmha_device_bwd.hpp:267-282](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/device/fmha_device_bwd.hpp#L267-L282) —— 依次切出 `sum_OdO`、`scaled_lse`、`dQ_acc`，再调 `initialize_split`。

#### 4.2.3 源码精读

[flash_mla/flash_mla_interface.py:297-304](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/flash_mla/flash_mla_interface.py#L297-L304) —— Python 端 workspace 字节计算，三块（外加 GQA 死分支）。

[flash_mla/flash_mla_interface.py:300](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/flash_mla/flash_mla_interface.py#L300) —— `dQ_acc`：`4 * bs * max_seqlen_qo_aligned * num_qo_heads * head_dim_qk`。每个 query 头的 `dQ` 是一个 `head_dim_qk` 维向量，按 fp32 累加。

[flash_mla/flash_mla_interface.py:301](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/flash_mla/flash_mla_interface.py#L301) —— `sum_OdO and scaled_lse`：注意末尾 `* 2`，是两个 shape 相同的 `[B, Qa, H]` fp32 缓冲合并写在一行。

[flash_mla/flash_mla_interface.py:302-303](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/flash_mla/flash_mla_interface.py#L302-L303) —— `dKV_acc`：仅当 `num_qo_heads != num_kv_heads`（GQA）才分配，用 bf16（`2` 字节）累加 dK/dV。由于函数开头 [flash_mla/flash_mla_interface.py:282-284](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/flash_mla/flash_mla_interface.py#L282-L284) 已对 GQA 抛异常，这段代码当前不可达，是为将来修复 GQA bwd 预留的。

CUTLASS 端三段切分：

[device/fmha_device_bwd.hpp:275-281](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/device/fmha_device_bwd.hpp#L275-L281) —— `workspace_chr` 指针依次前移 `B*H*Q*4`、`B*H*Q*4`、剩下给 `dQ_acc`，与 Python 的三块顺序一致。

device 层 `run` 把三个子 kernel 串起来跑：

[device/fmha_device_bwd.hpp:287-312](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/device/fmha_device_bwd.hpp#L287-L312) —— 顺序为 `op_sum_OdO.run` → `cudaMemsetAsync(dQ_acc, 0)`（dQ 累加器必须清零）→ `op.run`（主 bwd）→ `op_convert.run`（把 fp32 dQ_acc 转 bf16 写回 dQ）。这正是 u7-l1 提到的「sum_OdO → 主 bwd → convert」三段式。

#### 4.2.4 代码实践

**实践目标**：给定一组具体参数，手算 workspace 三块字节数，并与 CUTLASS `get_workspace_size` 对齐。

**操作步骤**：

1. 取参数：`bs=2`，`max_seqlen_qo=4096`，`num_qo_heads=128`，`head_dim_qk=128`，`num_kv_heads=128`（非 GQA）。
2. 先算 `max_seqlen_qo_aligned = ceil(4096/8)*8 = 4096`（已 8 对齐）。
3. 算三块：
   - dQ_acc = `4 * 2 * 4096 * 128 * 128` 字节
   - sum_OdO = `4 * 2 * 4096 * 128` 字节
   - scaled_lse = `4 * 2 * 4096 * 128` 字节
4. 汇总成总 workspace_bytes。
5. 对照 [device/fmha_device_bwd.hpp:226-233](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/device/fmha_device_bwd.hpp#L226-L233) 的公式，注意 CUTLASS 用真实 `Q = total_seqlen_q`（若两条序列都恰为 4096，则 `Q = 8192`，与 Python 的 `bs*Qa = 2*4096 = 8192` 相等；若序列不等长，Python 估计偏大）。

**需要观察的现象**：

- dQ_acc 是三块里最大的（多一个 `head_dim_qk` 因子，这里 128 倍）。
- sum_OdO 与 scaled_lse 字节数完全相等。
- 非 GQA 时 `dKV_acc` 不分配。

**预期结果**：

- dQ_acc = 4 × 2 × 4096 × 128 × 128 = 536,870,912 字节 = 512 MiB。
- sum_OdO = scaled_lse = 4 × 2 × 4096 × 128 = 4,194,304 字节 = 4 MiB。
- 总计 ≈ 520 MiB（非 GQA）。

可见 `dQ_acc` 占了绝大部分 workspace。若开 GQA 还会再加 `2 * kv_total_len * num_qo_heads * (head_dim_qk + head_dim_vo)` 字节的 bf16 `dKV_acc`。「待本地验证」指在有 GPU 环境打印 `workspace_bytes` 实际值核对。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `dQ_acc` 用 fp32（4 字节）而最终 `dQ` 是 bf16（2 字节）？为什么不直接写 bf16？

**答案**：反向 `dQ` 由多个 K-tile 块的贡献累加而成，fp32 累加保证数值精度、避免 bf16 累加的舍入误差放大。累加完成后再由 `op_convert` 一次性转 bf16 写回。这也是 device 层 `run` 里 `cudaMemsetAsync(dQ_acc, 0)` 必须先清零的原因——累加器要从 0 开始。

**练习 2**：Python 用 `bs * max_seqlen_qo_aligned` 估 Q，CUTLASS 用真实 `total_seqlen_q`。为什么这样估是安全的？

**答案**：varlen 下每条序列长度 ≤ `max_seqlen_qo`，所以 \(\sum_i s_i \le B \cdot \max_i s_i\)，即 `total_seqlen_q <= bs * max_seqlen_qo`。Python 按 `bs * max_seqlen_qo_aligned` 分配必然 ≥ CUTLASS 实际所需，是安全上界；多出的尾部空间不被访问，无副作用。

### 4.3 dense_prefill_fwd / bwd 接口与 pybind 绑定

#### 4.3.1 概念说明

Python 层的 `flash_mla_cuda.dense_prefill_fwd` / `dense_prefill_bwd` 是 pybind 绑定，背后对应两个 C++ 函数 `FMHACutlassSM100FwdRun` / `FMHACutlassSM100BwdRun`。这两个函数是 dense prefill 的「门面」：接收 PyTorch 张量，做最小的 dtype 校验，然后把运行时的 `mask_mode`、`is_varlen`、`head_dim` 三个值编译期化，选中对应的 CUTLASS 模板特化并启动。

注意 dense prefill 只支持 SM100（Blackwell），没有 SM90 实现——这一点在支持矩阵里写死（README：Dense Prefill | SM100）。与 sparse 路径用 `ImplBase` feature 派发不同，dense prefill 用的是更直接的「立即调用 lambda」编译期化手法（与 u2-l3 的 `DISPATCH_*` 宏同构）。

#### 4.3.2 核心流程

fwd 接口派发（`FMHACutlassSM100FwdRun`）：

```text
输入 workspace,q,k,v,cu_q,cu_kv,o,lse,mask_mode_code,sm_scale,max_q,max_kv,is_varlen
  ├─ CUDAGuard 锁定设备
  ├─ 校验 q/k 同 dtype；只接受 bf16→bf16
  ├─ head_dim_qk = q.size(-1); head_dim_vo = v.size(-1)
  ├─ apply_config([&](mask, varlen, in, out) { ... })：
  │     用 if/else 选 mask 类型 + varlen 标签
  │     ├─ mask_mode==kCausal → CausalMask<false> (+ varlen true/false)
  │     └─ else              → ResidualMask     (+ varlen true/false)
  │     再按 head_dim 选 IsMla：
  │     ├─ 192/128 → call_run_fmha_fwd(..., true_type{})   # MLA
  │     └─ 128/128 → call_run_fmha_fwd(..., false_type{})  # 普通 MHA
  └─ call_run_fmha_fwd 内部再决定 kIsPersistent，调 run_fmha_fwd → FwdRunner → CUTLASS FMHA
```

bwd 接口派发（`FMHACutlassSM100BwdRun`）结构几乎一致，只是 mask 用 `ForBackward` 变体：

```text
  ├─ mask_mode==kCausal → CausalForBackwardMask<false>
  └─ else              → ResidualMaskForBackward
  再按 head_dim 选 IsMla（192/128 或 128/128）→ call_run_fmha_bwd → BwdRunner → Sm100FmhaBwd
```

pybind 注册：

[api.cpp:8-15](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/api.cpp#L8-L15) —— 5 个绑定里，`dense_prefill_fwd` → `FMHACutlassSM100FwdRun`，`dense_prefill_bwd` → `FMHACutlassSM100BwdRun`。另三个（`sparse_*`、`dense_decode_fwd`）是其他 kernel 家族的入口，本讲不展开。

#### 4.3.3 源码精读

接口声明：

[interface.h:5-14](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/interface.h#L5-L14) —— `FMHACutlassSM100FwdRun` 与 `FMHACutlassSM100BwdRun` 的签名，参数顺序与 Python 调用一一对应。

fwd 实现的设备守卫与 dtype 校验：

[fmha_cutlass_fwd_sm100.cu:36-38](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/fmha_cutlass_fwd_sm100.cu#L36-L38) —— `OptionalCUDAGuard` 锁定 `q` 所在设备（最近一次 commit「Add CUDAGuard and device id assignment in sm100 dense fmha」专门补的），`CHECK(q.scalar_type() == k.scalar_type())` 保证 q/k 同 dtype。

mask + varlen 的 4 路派发：

[fmha_cutlass_fwd_sm100.cu:49-63](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/fmha_cutlass_fwd_sm100.cu#L49-L63) —— `apply_config` 用两层 if/else 选出 `(mask, varlen)` 的 4 种组合之一，再传给内层 lambda。这里 `CausalMask<false>` 的模板参数 `false` 表示「Q 在矩阵开头」（默认因果方向，见 [fmha_fusion.hpp:191](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/collective/fmha_fusion.hpp#L191)）；非因果用 `ResidualMask`（只做末尾不整 tile 的掩码，见 [fmha_fusion.hpp:83](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/collective/fmha_fusion.hpp#L83)）。

head_dim 选 IsMla：

[fmha_cutlass_fwd_sm100.cu:65-78](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/fmha_cutlass_fwd_sm100.cu#L65-L78) —— `head_dim_qk==192 && head_dim_vo==128` 走 `true_type`（MLA），`128/128` 走 `false_type`（普通 MHA），其它打印「No kernel instantiated」。注意 fwd 调用的是 `call_run_fmha_fwd(..., true_type{}, ...)`，第 5 个标签参数 `Mla` 决定 `IsMla`。

`call_run_fmha_fwd` 还会根据 mask/varlen 决定 `kIsPersistent`：

[fmha_cutlass_fwd_sm100.cu:19-24](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/fmha_cutlass_fwd_sm100.cu#L19-L24) —— 因果或 varlen 时 `kIsPersistent=false`（走 individual scheduler），否则 `kIsPersistent=true`（走 persistent scheduler）。这与 u7-l3 讲的 tile scheduler 选择一致。

fwd 不使用 workspace 的注释：

[fmha_cutlass_fwd_sm100.cuh:274-282](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/fmha_cutlass_fwd_sm100.cuh#L274-L282) —— `FwdRunner::run` 里 `get_workspace_size` 被注释掉，注释明说「we don't use workspace in current version」，所以 Python 端那 32 MiB 是占位。bwd 则相反，真正用 workspace。

bwd 实现的派发结构对称：

[fmha_cutlass_bwd_sm100.cu:47-62](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/fmha_cutlass_bwd_sm100.cu#L47-L62) —— bwd 的 mask 用 `CausalForBackwardMask<false>` / `ResidualMaskForBackward`。反向的因果掩码方向与前向不同（`ForBackward` 变体，见 [fmha_fusion.hpp:280](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/collective/fmha_fusion.hpp#L280)），因为反向时 dK/dV 的掩码逻辑沿 K 维作用。

[fmha_cutlass_bwd_sm100.cu:64-77](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/fmha_cutlass_bwd_sm100.cu#L64-L77) —— 同样的 192/128 vs 128/128 二选一。`call_run_fmha_bwd` 里 MLA 与普通 MHA 的 `TileShape` 不同：

[fmha_cutlass_bwd_sm100.cu:21](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/fmha_cutlass_bwd_sm100.cu#L21) —— MLA 用 `Shape<_64, _128, _192, _128>`，普通 MHA 用 `Shape<_128, _128, _128, _128>`，对应 MLA 的复合 head_dim。

bwd runner 把 workspace 指针透传给 CUTLASS：

[fmha_cutlass_bwd_sm100.cuh:183-187](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/fmha_cutlass_bwd_sm100.cuh#L183-L187) —— `workspace_ptr = workspace_buffer.data_ptr()`，`op.initialize(arguments, workspace_ptr)` 把 buffer 交给 device 层切分（即 4.2 的三段）。

#### 4.3.4 代码实践

**实践目标**：把 fwd 接口的派发决策画成一张表，验证 `mask_mode × is_varlen × head_dim` 的所有合法组合都能落到一条模板特化。

**操作步骤**：

1. 读 [fmha_cutlass_fwd_sm100.cu:31-83](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/fmha_cutlass_fwd_sm100.cu#L31-L83)。
2. 列出 `mask_mode_code` 的可能取值（来自 [mask.cuh:3-7](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/common/mask.cuh#L3-L7)：0=kNone, 1=kCausal, 2=kCustom）。
3. 注意 Python 端 [flash_mla/flash_mla_interface.py:231](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/flash_mla/flash_mla_interface.py#L231) 只会产生 `0` 或 `1`（`1 if causal else 0`），`kCustom` 不对 Python 暴露。
4. 画出 8 格表：`(mask ∈ {None, Causal}) × (varlen ∈ {T, F}) × (head_dim ∈ {192/128, 128/128})`，每格填 `IsMla`、`Mask` 类型、`kIsPersistent`。

**需要观察的现象**：

- 因果或 varlen 时 `kIsPersistent=false`；非因果且定长时 `kIsPersistent=true`。
- `head_dim_qk=192` 永远对应 `IsMla=true`，`128` 对应 `false`。
- 若传 `head_dim_qk=256`，会落到 [fmha_cutlass_fwd_sm100.cu:74-77](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/fmha_cutlass_fwd_sm100.cu#L74-L77) 打印「No kernel instantiated」而非崩溃（注意是 `std::cout`，不是 assert）。

**预期结果**：合法组合（去掉 kCustom）均有对应特化；非法 head_dim 静默跳过。无 GPU 时「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 dense prefill 接口不用 `ImplBase` feature 派发（像 sparse 那样），而用直接 if/else？

**答案**：dense prefill 的派发维度（mask/varlen/head_dim）取值有限且彼此正交，每个组合直接对应一个明确的 CUTLASS 模板特化，没有「多个实现竞争同一 feature 子集」的需求。直接 if/else + 立即调用 lambda 更直观。sparse 路径因实现多、能力子集复杂才用 `ImplBase`（见 u2-l4）。

**练习 2**：`mask_mode == kCustom (2)` 会在哪一步被处理？

**答案**：不会。fwd 的 `apply_config` 只判断 `kCausal` 与 `else`，`kCustom` 会落到 `else` 分支被当成 `ResidualMask` 处理（即无自定义掩码、只做末尾对齐）。而 Python 端只产生 0/1，所以 `kCustom` 实际不会从 Python 触发。若要支持自定义掩码，需在 `apply_config` 加分支并在 fusion 层实现对应 mask 策略。

### 4.4 head_dim 分派与 GQA 限制

#### 4.4.1 概念说明

本模块把 4.3 的 head_dim 分派单独拎出来讲透，并回答实践任务里的关键问题：为什么 bwd 暂不支持 GQA。

「head_dim 分派」有两层含义：

1. **MLA vs 普通 MHA 的二选一**：`head_dim_qk==192` → MLA（`IsMla=true`），`128` → 普通 MHA。这决定 `TileShape`、`ProblemShape`、mainloop、load collective 等一整套模板（u7-l1 的「MLA 开关」）。
2. **入口校验**：`head_dim_vo` 必须等于 128，且 `head_dim_qk - head_dim_vo` 在 MLA 下等于 64（rope 维）。Python 端通过 `head_dim_v` 参数（必须 128）与 `q.size(-1)/v.size(-1)` 隐式约束。

「GQA 限制」指：反向要求 `num_qo_heads == num_kv_heads`。GQA（Grouped-Query Attention）是 `num_qo_heads > num_kv_heads`、多个 query 头共享一组 K/V 的结构，前向支持、反向不支持。

#### 4.4.2 核心流程

前向 head_dim 分派（fwd 与 bwd 同构）：

```text
head_dim_qk = q.size(-1)
head_dim_vo = v.size(-1)
if head_dim_qk == 192 and head_dim_vo == 128:
    IsMla = true     # MLA: 128 latent + 64 rope, V 取 latent
elif head_dim_qk == 128 and head_dim_vo == 128:
    IsMla = false    # 普通 MHA: K/V 同宽 128
else:
    "No kernel instantiated"
```

GQA 限制的源头（Python 层）：

```text
_flash_attn_varlen_backward 开头:
  if num_qo_heads != num_kv_heads:
      raise ValueError("SM100 bwd doesn't support GQA now. ...")
```

#### 4.4.3 源码精读

fwd 的 head_dim 分派：

[fmha_cutlass_fwd_sm100.cu:66-73](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/fmha_cutlass_fwd_sm100.cu#L66-L73) —— 192/128 走 `true_type`（MLA），128/128 走 `false_type`。

bwd 的 head_dim 分派：

[fmha_cutlass_bwd_sm100.cu:65-74](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/fmha_cutlass_bwd_sm100.cu#L65-L74) —— 同样的二选一，但 `call_run_fmha_bwd` 里 MLA 的 `TileShape` 是 `Shape<_64, _128, _192, _128>`（[fmha_cutlass_bwd_sm100.cu:21](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/fmha_cutlass_bwd_sm100.cu#L21)），第一维 64（Q tile）比普通 MHA 的 128 小，因为 MLA 的复合 head_dim 占更多寄存器/smem。

GQA 限制：

[flash_mla/flash_mla_interface.py:282-284](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/flash_mla/flash_mla_interface.py#L282-L284) —— `# TODO: fix bwd GQA`，`num_qo_heads != num_kv_heads` 直接抛 `ValueError`。

为什么 bwd 不支持 GQA？看 bwd runner 的 ProblemShape：

[fmha_cutlass_bwd_sm100.cuh:75-79](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/fmha_cutlass_bwd_sm100.cuh#L75-L79) —— ProblemShape 是 `(Q, K, D, D_VO, (H, B))`，只有一个头数 `H = num_qo_heads`，没有独立的 `h_k`。也就是说底层 bwd kernel 假设 `num_qo_heads == num_kv_heads`，K/V 与 Q 头数相同。

[fmha_cutlass_bwd_sm100.cuh:113-124](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/fmha_cutlass_bwd_sm100.cuh#L113-L124) —— `num_qo_heads = q.size(1)`，problem_shape 里只放 `num_qo_heads`，没有 `num_kv_heads`。对 GQA，`dK/dV` 的 shape 是 `[kv_total_len, num_kv_heads, ...]`，需要把多个 query 头的梯度归约到共享的 kv 头上——当前 kernel 没建模这个归约，也没有 GQA 专用的 `dKV_acc` 累加缓冲（Python 里 [flash_mla/flash_mla_interface.py:302-303](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/flash_mla/flash_mla_interface.py#L302-L303) 那段死代码就是为它预留的）。

测试也印证了这点：

[tests/test_fmha_sm100.py:176-180](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/test_fmha_sm100.py#L176-L180) —— `if h != h_k: has_bwd = False`，GQA 配置（如 `(32, 4)`）只测前向、不测反向。

#### 4.4.4 代码实践

**实践目标**：追踪 SM100 + MLA（`h=128, h_k=128, d=192, dv=128`）配置的前向调用，确认它落到哪条模板；再构造一个 GQA 反向调用，确认它被 Python 层拦下。

**操作步骤**：

1. 在 `tests/test_fmha_sm100.py` 的 `__main__` 里找到 `(h, h_k) in [(128, 128), (32, 4)]` 与 `(d, dv) in [(128, 128), (192, 128)]` 的循环（[test_fmha_sm100.py:176-181](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/test_fmha_sm100.py#L176-L181)）。
2. 对 `(128, 128, 192, 128, causal=True, varlen=True)`：手动追踪到 `call_run_fmha_fwd(CausalMask<false>, true_type, ..., true_type)` → `IsMla=true, IsVarlen=true, IsCausal=true, kIsPersistent=false`。
3. 想象把 `h_k` 改成 32（GQA）并开 `has_bwd`：会触发 [flash_mla/flash_mla_interface.py:283-284](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/flash_mla/flash_mla_interface.py#L283-L284) 的 `ValueError`。

**需要观察的现象**：

- MLA 配置（192/128）总是选 `IsMla=true`，bwd 的 `TileShape` 第一维是 64 而非 128。
- GQA + bwd 在 Python 层就被拦下，根本到不了 C++。

**预期结果**：MLA 前向走 `Sm100MlaFwdMainloopTmaWarpspecialized` 等 MLA 专用 collective；GQA 反向抛 `ValueError: SM100 bwd doesn't support GQA now`。无 GPU 时「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：为什么 MLA 的 bwd `TileShape` 用 `Shape<_64, _128, _192, _128>`，而普通 MHA 用 `Shape<_128, _128, _128, _128>`？第一个维度为何减半？

**答案**：MLA 的 `head_dim_qk=192`（128 latent + 64 rope）比普通 MHA 的 128 大，单个 tile 的寄存器/smem 占用更高。把 Q tile 的第一维从 128 减到 64，是为了在同样的 SM 资源下放下 MLA 的更大 head_dim，保证占用率与不溢出寄存器。

**练习 2**：如果要在 bwd 支持 GQA，从 Python 到 CUTLASS 大致要改哪些地方？

**答案**：(1) 去掉 [flash_mla/flash_mla_interface.py:282-284](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/flash_mla/flash_mla_interface.py#L282-L284) 的抛异常，启用 [flash_mla/flash_mla_interface.py:302-303](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/flash_mla/flash_mla_interface.py#L302-L303) 的 `dKV_acc` 分配；(2) 把 `num_kv_heads` 透传进 `FMHACutlassSM100BwdRun` 签名；(3) 改 `BwdRunner` 的 ProblemShape 引入独立 `h_k`，让 dK/dV 按 `num_kv_heads` 输出并跨 query 头组归约；(4) CUTLASS bwd kernel 模板支持 `h_r = num_qo_heads/num_kv_heads` 的分组逻辑与 `dKV_acc` 累加。这正是 `TODO: fix bwd GQA` 的工作量。

## 5. 综合实践

把本讲四个模块串起来，做一次「端到端 autograd 追踪 + workspace 核对」。

**任务**：参照 `tests/test_fmha_sm100.py`，针对一组 MLA 配置（`b=2, mean_sq=mean_sk=4096, varlen=True, h=h_k=128, d=192, dv=128, causal=True`），完成以下全部步骤：

1. **构造数据**：按 [test_fmha_sm100.py:55-76](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/test_fmha_sm100.py#L55-L76) 生成 `cu_seqlens_q/kv`、拼接的 `q/k/v`、`grad_out`，设置 `requires_grad_()`。
2. **前向**：调用 `flash_attn_varlen_func(q1, k1, v1, cu_seqlens_q, cu_seqlens_k, max_seqlen_q, max_seqlen_k, softmax_scale=..., causal=True, is_varlen=True)`，拿到 `(out, lse)`。
3. **派发追踪**：口述这次调用经 `flash_mla_cuda.dense_prefill_fwd` → `FMHACutlassSM100FwdRun` → 因 `head_dim_qk=192` 选 `IsMla=true`、`causal` 选 `CausalMask<false>`、`is_varlen=true` 选 `kIsPersistent=false`，最终落到 MLA 的 `FwdRunner`。
4. **反向**：`out.backward(grad_out)`，触发 `FlashAttnVarlenFunc.backward` → `_flash_attn_varlen_backward`。
5. **workspace 核对**：在调用 bwd 前，用 [flash_mla/flash_mla_interface.py:297-303](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/flash_mla/flash_mla_interface.py#L297-L303) 的公式手算 `workspace_bytes`（`max_seqlen_qo=4096, bs=2, num_qo_heads=128, head_dim_qk=192`），并与 CUTLASS [device/fmha_device_bwd.hpp:220-234](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/device/fmha_device_bwd.hpp#L220-L234) 对齐。
6. **正确性校验**：用 [test_fmha_sm100.py:31-43](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/tests/test_fmha_sm100.py#L31-L43) 的 `sdpa` 参考实现算 `out_torch/lse_torch` 与 `dq/dk/dv` 的 torch 参考值，按测试里的三重容差（`abs_tol=1e-3, rel_tol=8.01/128, cos_diff_tol=7e-6`）比对。
7. **GQA 验证**：把 `h_k` 改成 32 重跑反向，确认抛 `ValueError`。

**无 GPU 时的可运行骨架**：写一个不依赖 CUDA 的脚本，只做步骤 3（派发追踪，用注释说明每步落到哪个模板）和步骤 5（workspace 字节手算并打印），给出预期数值。步骤 1/2/4/6/7 标注「待本地验证」。

**预期结果**：

- 前向 `out` 与 torch 参考在容差内一致；`lse` 同样。
- 反向 `dq/dk/dv` 在容差内一致。
- workspace 手算值：dQ_acc = 4×2×4096×128×192 = 805,306,368 字节（768 MiB）；sum_OdO = scaled_lse = 4×2×4096×128 = 4,194,304 字节（4 MiB）；总计 ≈ 776 MiB。
- GQA 反向抛 `ValueError`。

## 6. 本讲小结

- `FlashAttnVarlenFunc` 是 dense MHA prefill 的 autograd 外壳：`forward` 调 `dense_prefill_fwd` 并 `save_for_backward(q,k,v,out,lse,cu_seqlens)`，`backward` 调 `dense_prefill_bwd` 返回 `dq/dk/dv`，`dlse` 被 `del` 丢弃（LSE 暂不支持反向）。
- bwd 的 workspace 由 Python 端按安全上界预估：`dQ_acc`（fp32，`4·B·Qa·H·D`）占绝大部分，`sum_OdO` 与 `scaled_lse` 各一份 fp32（`4·B·Qa·H`），GQA 的 `dKV_acc` 分支当前因前置 `ValueError` 不可达。CUTLASS `Sm100FmhaBwd` 的 `get_workspace_size` 与之一致，并在 `run` 里按 sum_OdO → memset dQ_acc → 主 bwd → convert 的顺序串联三 kernel。
- `FMHACutlassSM100FwdRun/BwdRun` 是 dense prefill 的 C++ 门面，用「立即调用 lambda」把 `mask_mode × is_varlen × head_dim` 编译期化：`192/128 → IsMla=true`、`128/128 → IsMla=false`；因果用 `CausalMask` / `CausalForBackwardMask`，非因果用 `ResidualMask` / `ResidualMaskForBackward`。
- bwd 暂不支持 GQA：底层 `BwdRunner` 的 ProblemShape 只有一个头数 `H=num_qo_heads`，没有独立 `h_k`，无法做 query 头组到 kv 头的梯度归约，Python 层用 `# TODO: fix bwd GQA` 的 `ValueError` 提前拦截。
- fwd 当前不实际使用 workspace（`FwdRunner` 注释明说），Python 那 32 MiB 是占位；bwd 才真正消费 workspace。

## 7. 下一步学习建议

- **横向对比 sparse 接口**：回到 u6-l4（`sparse_attn_prefill_interface`）与 u5-l4（`sparse_attn_decode_interface`），对比它们用 `ImplBase` feature 派发与本讲 dense prefill 用直接 if/else 派发的差异，理解为什么 dense prefill 不需要 feature 校验。
- **深入 bwd kernel 内部**：本讲只到 `Sm100FmhaBwd` device 层。若想看反向的 softmax 重建与 `dQ/dK/dV` 的具体 GEMM 安排，可读 `csrc/sm100/prefill/dense/kernel/sm100_fmha_bwd_kernel_tma_warpspecialized.hpp` 与 `sm100_fmha_bwd_mla_kernel_tma_warpspecialized.hpp`，以及 `fmha_kernel_bwd_sum_OdO.hpp` / `fmha_kernel_bwd_convert.hpp` 两个辅助 kernel。
- **CUTLASS workspace 机制**：u8-l1 会讲 `kerutils` 的张量校验宏与 device 工具，可结合本讲的 workspace 切分理解 CUTLASS `device::FMHA` 的 `can_implement → initialize → run` 三段式约定。
- **尝试修复 GQA bwd**：作为进阶二次开发练习，按 4.4.5 练习 2 的清单动手，把 `num_kv_heads` 贯通 Python → C++ → CUTLASS bwd kernel，这是理解整套接口层的最佳试金石。
