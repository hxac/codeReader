# FFPAAttnFunc autograd Function 前向/反向分发

## 1. 本讲目标

本讲聚焦 FFPA 在「校验归一化之后、真正算子之前」的那一座桥——`_FFPAAttnFunc` 这个 `torch.autograd.Function`。学完后你应该能够：

- 说清楚 `_FFPAAttnFunc.forward` 如何先用 `head_dim`、再用 `forward_meta` 的类型，把一次前向分发到 aten / cuda / triton / cutedsl 四条路径之一。
- 说清楚 `_FFPAAttnFunc.backward` 为何要「先看 `forward_meta` 判断大小 D、再看 `backward_meta` 选反向后端」，以及这种**前向/反向解耦**带来的「一对多」分发能力。
- 默写出 `save_for_backward` 保存的 7 个张量（q/k/v/O/lse/rng_state/unused）以及额外存在 `ctx` 上的两个非张量/张量状态。
- 解释 `_reserve_large_d_dropout_rng` 为何要按 SDPA 的 Philox 约定预留 RNG 偏移，以及它如何让前向与反向重放出**同一个** dropout 掩码。

本讲是 u3 单元「分发层」的核心一讲，承接 u3-l3（`FFPAAttnMeta` 的校验与回退），并为 u3-l5（torch.compile 与自定义算子）埋下「为什么不用 `register_autograd`」的伏笔。

## 2. 前置知识

阅读本讲前，请确认你已掌握以下概念（前几讲已建立）：

- **autograd Function**：PyTorch 里自定义可微算子的标准做法。继承 `torch.autograd.Function`，实现 `forward(ctx, ...)` 与 `backward(ctx, grad_outputs)`；`backward` 返回的梯度数量与顺序必须和 `forward` 的输入一一对应，不需要梯度的输入位置返回 `None`。
- **ctx**：forward 与 backward 之间传递状态的容器。张量用 `ctx.save_for_backward(...)` 保存（autograd 会帮你处理引用与版本），非张量（如配置对象）直接挂成 `ctx.xxx` 属性。
- **FFPAAttnMeta**（u3-l3）：一个非张量的「调度信封」，封装了 `attn_meta`（is_causal / scale / dropout_p / is_grad_enabled）和**两个**后端配置 `forward_meta` / `backward_meta`。本讲频繁读取它们。
- **四后端**（u3-l1/u3-l2）：`SDPABackend` / `CUDABackend` / `TritonBackend` / `CuTeDSLBackend`，分别对应 `name` 为 `"sdpa"` / `"cuda"` / `"triton"` / `"cutedsl"`。CUDA 仅前向（`backward=False` 被强制），其余三者前向/反向皆可。
- **回退（fallback）**（u1-l4/u3-l3）：公共 API `ffpa_attn_func` 在进入 `_FFPAAttnFunc` 之前，会先用 `FFPAAttnMeta.fallback()` 把 D≤256、D>1024、Nq/Nkv 过短等情形**短路回退到原生 SDPA**。所以本讲看到的 forward，绝大多数情况下只会处理「大 D、长序列」的真正 FFPA 路径——但 forward 内部仍自带一道大小 D 判定，作为「被直接调用」时的防御。

一个关键直觉：**前向后端和反向后端是两件独立的事**。FFPA 把它们拆成 `forward_meta` 与 `backward_meta` 两个配置对象，因此「CUDA 前向 + Triton 反向」「Triton 前向 + SDPA 反向」这种混搭是合法且被支持的。本讲大部分篇幅都在解释这套解耦是怎么落地到代码里的。

## 3. 本讲源码地图

本讲几乎全部围绕一个文件展开，辅以它调用的四个后端入口：

| 文件 | 作用 | 本讲用到的部分 |
| --- | --- | --- |
| `src/ffpa_attn/functional.py` | autograd Function 与 meta 定义 | `_FFPAAttnFunc`（forward/backward）、`_reserve_large_d_dropout_rng`、`_ffpa_apply`/`FFPAAttnFunc` 外壳 |
| `src/ffpa_attn/ffpa_attn_interface.py` | 公共 API `ffpa_attn_func` | 第 156–181 行的 `fallback → normalize → FFPAAttnFunc.apply` 三步链 |
| `src/ffpa_attn/aten/_flash_fwd.py` | 小 D 前向（aten flash） | `_flash_attn_forward_aten`，返回 4 元组 |
| `src/ffpa_attn/aten/_flash_bwd.py` | 小 D 反向（aten flash backward） | `_flash_attn_backward_aten`，返回 3 元组 |
| `src/ffpa_attn/aten/_efficient_bwd.py` | 大 D 的 SDPA 反向 | `_efficient_attn_backward_aten`，返回 4 元组 |
| `src/ffpa_attn/cuda/_ffpa_fwd.py` | 大 D 的手写 CUDA 前向包装 | `_ffpa_attn_forward_cuda`，转发到 torch op `_fwd_cuda` |
| `src/ffpa_attn/cuda/__init__.py` | CUDA 后端入口与 torch op 注册 | `ffpa_attn::_fwd_cuda` 的 define/impl/register_fake |

> Triton 与 CuTeDSL 的前向/反向实现细节分别在 u4/u5 与 u6 单元讲解；本讲只把它们当作「被调用的函数名」。

## 4. 核心概念与源码讲解

### 4.1 autograd Function 外壳：FFPAAttnFunc.apply 与图断

#### 4.1.1 概念说明

PyTorch 里实现一个可微算子的标准载体是 `torch.autograd.Function`。FFPA 真正的 forward/backward 逻辑写在私有类 `_FFPAAttnFunc` 里，但对外暴露的却是另一个名叫 `FFPAAttnFunc` 的「外壳类」，它的 `apply` 并不直接调用 `_FFPAAttnFunc.apply`，而是绕一层 `_ffpa_apply`。

为什么要绕这一层？因为 FFPA 要兼容 `torch.compile`。`_FFPAAttnFunc.apply` 是 autograd 自动生成的类方法，如果被 Dynamo（torch.compile 的追踪引擎）直接看到，Dynamo 会把它**内联**进计算图，并用一个自动生成的反向模板替换掉我们手写的 `_FFPAAttnFunc.backward`——而那个模板在「一前向多反向」的情形下会算出零梯度。解决办法是用 `@torch._dynamo.disable` 给 `_ffpa_apply` 打上「禁止追踪」标记，在 autograd 边界处**主动制造一次图断（graph break）**，让真正的 backward 在 eager 模式下完整执行。

#### 4.1.2 核心流程

公共 API 调用到真正前向的链路是：

```
ffpa_attn_func(...)
  ├─ FFPAAttnMeta.from_kwargs(**kwargs)      # 解析后端（u3-l2/u3-l3）
  ├─ meta.fallback(...) → True 则回退 SDPA   # 短路，不进 Function
  ├─ meta.normalize(...)                      # 校验 + 归一化 attn_mask
  └─ FFPAAttnFunc.apply(q,k,v,attn_bias,meta)
         └─ _ffpa_apply(...)                  # @torch._dynamo.disable 图断
                └─ _FFPAAttnFunc.apply(...)   # 真正的 forward/backward
```

#### 4.1.3 源码精读

公共 API 三步链——先回退、再校验、最后进入 autograd 边界：

[ffpa_attn_interface.py:156-181](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/ffpa_attn_interface.py#L156-L181) —— `fallback` 为真时调用底层 `torch._C._nn` 绑定（避免 monkey-patch 递归，见 u1-l4）；否则 `normalize` 后把 `(query, key, value, attn_bias, meta)` 五元组交给 `FFPAAttnFunc.apply`。注意 `meta` 是非张量，作为最后一个位置参数传进 Function。

外壳类与图断函数——真正的 backward 之所以不会被 Dynamo 吞掉，全靠这一层 `@torch._dynamo.disable`：

[functional.py:966-987](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L966-L987) —— `_ffpa_apply` 被 `torch._dynamo.disable` 装饰；`FFPAAttnFunc.apply` 只是把调用转发给它。注释明确指出：直接调用 `_FFPAAttnFunc.apply` 会被 Dynamo 内联并用自动反向模板覆盖，产生零梯度。

紧挨着外壳的这段大段注释，是理解整个分发架构「为什么长这样」的钥匙——它列出了前向↔反向的「多对多」矩阵：

[functional.py:946-965](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L946-L965) —— 说明为何不能用 `torch.library.register_autograd`：一个前向算子会被绑定到**唯一**一个反向公式，而 FFPA 的同一个前向在运行时要按 `backward_backend` 选不同的反向。硬编码单一反向会在 `torch.compile(fullgraph=True)` 下悄悄丢弃用户选的 SDPA 反向。

> 这段注释的 torch.compile 细节属于 u3-l5 的范围；本讲你只需要记住结论：**前向/反向解耦是 FFPA 分发层的根本设计**，`_ffpa_apply` 的图断是为了保护它。

#### 4.1.4 代码实践

**实践目标**：确认外壳确实绕了一层，并理解图断的位置。

**操作步骤**：

1. 在仓库根目录打开 Python（需已 `pip install -e .`）：
   ```python
   from ffpa_attn.functional import FFPAAttnFunc, _ffpa_apply, _FFPAAttnFunc
   print(FFPAAttnFunc.apply.__qualname__)   # FFPAAttnFunc.apply
   print(_ffpa_apply.__wrapped__ is _FFPAAttnFunc.apply if hasattr(_ffpa_apply, "__wrapped__") else "no __wrapped__")
   ```
2. 阅读 [functional.py:985-987](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L985-L987)，确认 `FFPAAttnFunc.apply` 的函数体只有一行 `return _ffpa_apply(*args, **kwargs)`。

**需要观察的现象**：`FFPAAttnFunc` 是一个普通 `class`（不是 `torch.autograd.Function` 子类），它只是把 `apply` 转发给被 `dynamo.disable` 包裹的 `_ffpa_apply`。

**预期结果**：你能向同伴说清「为什么对外叫 `FFPAAttnFunc.apply`、对内却另有一个 `_FFPAAttnFunc`」——前者是兼容 torch.compile 的外壳，后者才是承载 forward/backward 的真身。若本地未安装 torch 则标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `FFPAAttnFunc.apply` 直接改成 `return _FFPAAttnFunc.apply(*args, **kwargs)`（去掉 `_ffpa_apply` 这一层），在 `torch.compile` 下会出什么问题？

**参考答案**：Dynamo 会把 `_FFPAAttnFunc.apply` 内联进图，并用自动生成的反向模板替换 `_FFPAAttnFunc.backward`，导致 (a) 用户通过 `backward_backend` 选的反向被忽略、(b) 在多反向分支下可能算出零梯度。`_ffpa_apply` 上的 `@torch._dynamo.disable` 强制在此处断图，让真 backward 在 eager 完整执行。

**练习 2**：`backward` 的返回值个数为什么必须是 5？

**参考答案**：因为 `forward(ctx, q, k, v, attn_bias, meta)` 有 5 个输入（不含 ctx）。autograd 要求 `backward` 返回的梯度与 forward 的输入一一对应；`meta` 是非张量、不可微，对应位置返回 `None`。

---

### 4.2 _FFPAAttnFunc.forward：head_dim 与 backend 的前向分发

#### 4.2.1 概念说明

`forward` 是一座「四岔路口」。它先用 `head_dim` 判断要不要走小 D 的 aten flash 路径，否则再按 `forward_meta` 的具体后端类型，分发到大 D 的 cuda / triton / cutedsl 三条路径之一。

这里有一个容易忽略的细节：forward **自己**也带一道大小 D 判定（`use_aten_small_d_forward`），并不假设「公共 API 的 `fallback()` 一定替我把小 D 拦掉了」。这是防御式设计——`_FFPAAttnFunc` 可以被直接 `apply` 调用（测试、内部代码都可能这么做），此时没有 `fallback()` 兜底，forward 必须自洽。判定函数 `_should_use_aten_small_d_forward` 与 u3-l3 讲过的 `fallback()` 共用同一套阈值（D≤256 且未开启 `FFPA_TRITON_ALLOW_SMALL_D` / `FFPA_CUTE_ALLOW_SMALL_D`）。

#### 4.2.2 核心流程

```
head_dim = q.size(-1)
use_aten_small_d_forward = (head_dim ≤ 256) 且 后端未开启 small-D

分配输出缓冲 O = empty_like(q)

if use_aten_small_d_forward:           # 小 D
    O, lse, rng_state, unused = _flash_attn_forward_aten(q,k,v,O,causal,scale,dropout_p)
elif isinstance(forward_meta, CUDABackend):    # 大 D · 手写 CUDA（需 _C）
    rng_state = _reserve_large_d_dropout_rng(q,k,dropout_p)
    O, lse = _ffpa_attn_forward_cuda(q,k,v,O,attn_bias, stages, acc_code, causal, scale,
                                     dropout_p, seed, offset, 0)
elif isinstance(forward_meta, TritonBackend):  # 大 D · Triton（默认）
    rng_state = _reserve_large_d_dropout_rng(q,k,dropout_p)
    O, lse = _ffpa_attn_forward_triton(q,k,v,O,causal,scale, autotune, autotune_mode,
                                       attn_bias, dropout_p, seed, offset, enable_tma, enable_ws)
elif isinstance(forward_meta, CuTeDSLBackend): # 大 D · CuTeDSL
    O, lse = _ffpa_attn_forward_cute(q,k,v, softmax_scale, causal, return_lse=True)
    rng_state = empty(0, uint8)                # CuTeDSL 无 dropout
else:
    raise ValueError

# 大 D 路径补一个空 unused，保证 save_for_backward 张量数恒定
if is_grad: save_for_backward(q,k,v,O,lse,rng_state,unused); ctx.attn_bias=attn_bias; ctx.meta=meta
return O
```

四条路径的返回值「形状」并不一致：aten 返回 4 元组 `(O, lse, rng_state, unused)`，而三条大 D 路径只返回 `(O, lse)`，`rng_state` 在分支内部单独生成、`unused` 在分支外补一个空张量。这样最终交给 `save_for_backward` 的张量数永远是 7，满足 autograd「各分支保存张量数一致」的契约。

#### 4.2.3 源码精读

forward 全貌与四路分发：

[functional.py:746-850](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L746-L850) —— 先算 `is_grad`（只有「开启梯度且至少一个输入要梯度」才需要保存中间量），再算 `use_aten_small_d_forward`，然后四路分支。注意第 762 行 `O = torch.empty_like(q)` 是预先分配的输出缓冲。

大小 D 判定函数（与 `fallback()` 共用阈值）：

[functional.py:75-81](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L75-L81) —— `_should_use_aten_small_d_forward = head_dim ≤ 256 且 not _backend_allows_small_d(...)`。`_backend_allows_small_d`（第 65–72 行）只在「64≤D≤256 且后端是 Triton/CuTeDSL 且对应环境变量开启」时返回 True；CUDABackend / SDPABackend 永远返回 False，所以它们在 D≤256 时永远走 aten。

四个分支的关键调用点：

- 小 D aten：[functional.py:764-773](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L764-L773) —— 调 `_flash_attn_forward_aten`，拿到现成的 4 元组。
- 大 D CUDA：[functional.py:774-792](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L774-L792) —— 先 `_reserve_large_d_dropout_rng`，再把 `stages`、`acc_code`、causal、scale、dropout、seed、offset 透传给 `_ffpa_attn_forward_cuda`。`acc_code` 来自 `CUDABackend.acc_code`（f32→1，f16→0）。
- 大 D Triton：[functional.py:793-812](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L793-L812) —— 同样先预留 RNG，再透传 `autotune` / `autotune_mode` / `enable_tma` / `enable_ws` 等 Triton 旋钮。
- 大 D CuTeDSL：[functional.py:813-825](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L813-L825) —— 不传后端旋钮，只传 `softmax_scale` / `causal` / `return_lse=True`；布局转换 `[B,H,N,D]↔[B,N,H,D]` 在 `_ffpa_attn_forward_cute` 内部完成。`rng_state` 直接给一个空 uint8 张量（CuTeDSL 不支持 dropout）。

补齐 `unused` 以满足保存张量数恒定的契约：

[functional.py:831-835](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L831-L835) —— 注释明说：FFPA 大 D 前向没有 `unused` 输出，但 autograd 要求各分支保存的张量数一致，故补一个空 `uint8` 张量占位。

> 想看 CUDA 前向如何被注册成可在 `torch.compile` 下追踪的自定义算子，见 [cuda/__init__.py:22-101](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cuda/__init__.py#L22-L101)（`ffpa_attn::_fwd_cuda` 的 define / impl / register_fake），包装层见 [cuda/_ffpa_fwd.py:6-48](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cuda/_ffpa_fwd.py#L6-L48)。这是 u3-l5 的内容，本讲只点到为止。

#### 4.2.4 代码实践

**实践目标**：用源码阅读的方式，验证「D≤256 默认走 aten、D>256 默认走 Triton」。

**操作步骤**：

1. 读 [functional.py:75-81](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L75-L81) 与第 65–72 行的 `_backend_allows_small_d`，确认默认（不开环境变量）时 `_should_use_aten_small_d_forward(D≤256)` 恒为 True。
2. 想象 `forward_meta` 是默认的 `TritonBackend`、`head_dim=512`：`use_aten_small_d_forward=False` → 跳过第一个 if → 不是 CUDABackend → 命中第 793 行的 `isinstance(meta.forward_meta, TritonBackend)` 分支。
3. 再想象 `head_dim=128`：`use_aten_small_d_forward=True` → 命中第 764 行的 aten 分支。

**需要观察的现象**：决策的第一刀切在 `head_dim`（小 D / 大 D），第二刀才切在 `backend` 类型。即便 `forward_meta` 是 `CUDABackend`，只要 D≤256，也会先进 aten 分支（因为 CUDA 后端的 `_backend_allows_small_d` 永远 False）。

**预期结果**：你能画出 4.2.2 的决策树，并解释「为什么 CUDA 后端在 D≤256 时也走 aten」——因为手写 CUDA kernel 只为大 D 而生。

#### 4.2.5 小练习与答案

**练习 1**：forward 里四条路径，为什么只有 aten 分支返回 4 个值，其余三条只返回 `(O, lse)`？

**参考答案**：aten op `torch.ops.aten._flash_attention_forward` 本身返回 `(out, lse, rng_state, unused, ...)` 五元（见 [_flash_fwd.py:36-48](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/aten/_flash_fwd.py#L36-L48)），包装后给 forward 4 个；大 D 路径的 `rng_state` 由 FFPA 自己用 `_reserve_large_d_dropout_rng` 生成、`unused` 在分支外补空张量，所以分支内只需返回 `(O, lse)`。最终都凑齐 7 个张量再保存。

**练习 2**：设 `FFPA_TRITON_ALLOW_SMALL_D=1`、`forward_meta=TritonBackend`、`head_dim=128`，forward 会走哪条分支？

**参考答案**：`_backend_allows_small_d` 此时返回 True，故 `_should_use_aten_small_d_forward=False`，于是跳过 aten 分支，命中第 793 行的 TritonBackend 分支，由 Triton kernel 处理小 D。这正是该环境变量「让 Triton 接管小 D」的作用。

---

### 4.3 save_for_backward：反向所需的七个张量

#### 4.3.1 概念说明

FlashAttention 类算法的反向不能只靠「前向输入 + 上游梯度」——它还需要前向留下的两个中间量：输出 `O` 与 log-sum-exp `lse`（反向用它们做 rescale，见 u5-l1）。再加上 dropout 的 RNG 状态（反向要重放同一个掩码），以及为对齐 aten 接口而存在的 `unused`，FFPA 一共要为反向保存 7 个张量。

forward 只在「确实需要反向」（`is_grad=True`）时才保存，省下推理场景的显存与时间。这是 `torch.no_grad()` 下不保存、推理更快的原因。

#### 4.3.2 核心流程

保存的内容分两类：

| 类别 | 内容 | 保存方式 |
| --- | --- | --- |
| 7 个核心张量 | `q, k, v, O, lse, rng_state, unused`（全部 `.contiguous()`） | `ctx.save_for_backward(...)` |
| 1 个可微张量 | `attn_bias`（可能要梯度） | `ctx.attn_bias = attn_bias`（属性） |
| 1 个非张量 | `meta`（FFPAAttnMeta） | `ctx.meta = meta`（属性） |

backward 取回时：`q, k, v, O, lse, rng_state, unused = ctx.saved_tensors`，`attn_bias = getattr(ctx, "attn_bias", None)`，`meta = ctx.meta`。是否需要 `attn_bias` 的梯度由 `ctx.needs_input_grad[3]` 判定（`attn_bias` 是 forward 的第 4 个位置参数，索引 3）。

#### 4.3.3 源码精读

保存逻辑：

[functional.py:837-848](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L837-L848) —— `is_grad` 守门；7 个张量全部 `.contiguous()` 后保存；`attn_bias` 与 `meta` 挂属性。注意 `is_grad` 的定义在 [functional.py:755-757](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L755-L757)：`meta.attn_meta.is_grad_enabled and any(x.requires_grad for x in (q,k,v,attn_bias) if x is not None)`。

`lse` 是什么、由谁产生：

- aten 路径：`_flash_attn_forward_aten` 返回的 `lse`，形状 `[B, Nh_q, Nq]`（fp32）。
- CUDA 路径：`_ffpa_attn_forward_cuda` 返回 `softmax_lse_storage[..., :Q.size(2)]`，见 [cuda/_ffpa_fwd.py:34-48](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cuda/_ffpa_fwd.py#L34-L48)（按 8 对齐分配、再切到可见长度）。
- Triton / CuTeDSL 路径：各自前向返回的 `lse`。

`rng_state` 的形态因分支而异（这是 backward 里出现 `if rng_state.numel()` 的原因）：

| 前向分支 | `rng_state` 形态 |
| --- | --- |
| aten | aten op 自己的 rng_state 张量 |
| CUDA / Triton（大 D） | `_reserve_large_d_dropout_rng` 返回的 `int64` 张量 `[seed, offset]`，或 `dropout_p=0` 时的空 `int64` |
| CuTeDSL | 空 `uint8` 张量 |

#### 4.3.4 代码实践

**实践目标**：理解「推理不保存、训练才保存」对显存的影响。

**操作步骤**：

1. 读 [functional.py:755-757](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L755-L757) 与第 837–848 行。
2. 构造一段**示例代码**（非项目原有，标注为示例）对比两种模式：
   ```python
   # 示例代码：观察 is_grad 对保存行为的影响（概念演示，不在仓库内）
   import torch
   q = torch.randn(1, 8, 8192, 512, dtype=torch.float16, device="cuda", requires_grad=True)
   k = torch.randn_like(q).requires_grad_(True)
   v = torch.randn_like(q).requires_grad_(True)
   # 训练模式：is_grad=True，7 张量被保存
   # 推理模式（with torch.no_grad():）：is_grad=False，不保存
   ```

**需要观察的现象**：在 `torch.no_grad()` 下，`meta.attn_meta.is_grad_enabled` 为 False，`is_grad` 为 False，`save_for_backward` 整段被跳过。

**预期结果**：你能解释「为什么 FFPA 推理比训练省显存」——训练多保存了 `O`、`lse`、`rng_state`、`unused` 这几个中间量（q/k/v 本来就要留着）。具体显存数字待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `attn_bias` 用 `ctx.attn_bias = attn_bias` 保存，而不是放进 `save_for_backward`？

**参考答案**：`save_for_backward` 主要用于「需要 autograd 版本跟踪的输入张量」。`attn_bias` 在 FFPA 里作为属性保存也能工作，反向通过 `ctx.needs_input_grad[3]` 判断是否需要它的梯度（见 backward 第 879、919 行的 `return_attn_bias_grad=ctx.needs_input_grad[3]`）。这是一种简化写法；严格来说 PyTorch 推荐输入张量走 `save_for_backward` 以避免潜在的引用问题，但 FFPA 此处的用法是自洽的。

**练习 2**：`unused` 这个张量有什么用？能不能删掉？

**参考答案**：它是为了和 aten 路径「保存张量数一致」而存在的占位。aten op 的接口天然返回一个 `unused`（见 [_flash_fwd.py:36](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/aten/_flash_fwd.py#L36)），大 D 路径没有，但 autograd 要求所有分支保存相同数量的张量，所以大 D 分支补一个空 `uint8`。删掉它会破坏 aten 反向（`_flash_attn_backward_aten` 需要接收它，见 [backward 第 927-939 行](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L927-L939)）。

---

### 4.4 _FFPAAttnFunc.backward：backward_meta 的反向路由

#### 4.4.1 概念说明

backward 是 forward 的镜像，但有一处精妙的**不对称**：

- forward 用 `forward_meta` 决定走哪条前向路径。
- backward **先用 `forward_meta` 重新判断大小 D**（因为「该用小 D 反向还是大 D 反向」是前向的属性，必须看 `forward_meta`），**再用 `backward_meta` 在大 D 反向里选 Triton / CuTeDSL / SDPA**。

这种「大小 D 看 forward_meta、大 D 后端看 backward_meta」的两段式判定，正是前向/反向解耦的落地点。它直接催生了「一对多」能力：例如 `forward_meta=CUDABackend`（大 D 前向）可以配 `backward_meta=TritonBackend` 或 `SDPABackend`——因为 CUDA 没有反向（`CUDABackend.__post_init__` 里 `assert not self.backward`），反向必须由别的后端承担，而 forward 已经把 `O`/`lse`/`rng_state` 都存好了，反向 kernel 并不在乎前向是谁算的。

#### 4.4.2 核心流程

```
q,k,v,O,lse,rng_state,unused = ctx.saved_tensors
D = q.size(-1)
use_aten_small_d_forward = (D ≤ 256) 且 后端未开启 small-D   # 看 forward_meta！

if NOT use_aten_small_d_forward:                       # 大 D 反向
    if isinstance(backward_meta, TritonBackend):       # 看 backward_meta
        dq,dk,dv,grad_attn_bias = _ffpa_attn_backward_triton(grad_out,q,k,v,O,lse, ...)
    elif isinstance(backward_meta, CuTeDSLBackend):
        dq,dk,dv = _ffpa_attn_backward_cute(grad_out,q,k,v,O,lse, ...)
        grad_attn_bias = None                          # CuTeDSL 不支持 attn_mask
    else:  # SDPABackend
        dq,dk,dv,grad_attn_bias = _efficient_attn_backward_aten(grad_out,q,k,v,O,lse, ...)
else:                                                   # 小 D 反向（D ≤ 256）
    dq,dk,dv = _flash_attn_backward_aten(grad_out,q,k,v,O,lse,causal,rng_state,unused,scale,dropout_p)
    grad_attn_bias = None

return dq, dk, dv, grad_attn_bias, None                 # 对应 forward 的 5 个输入
```

注意大 D 反向的三条路径里**没有 CUDA**——CUDA 后端没有反向实现。这也是「CUDA 前向必须配 Triton 或 SDPA 反向」的根本原因。

各反向函数的返回元组不同：Triton 与 SDPA 返回 4 元组（含 `grad_attn_bias`），CuTeDSL 与小 D aten 返回 3 元组（`grad_attn_bias=None`）。backward 在分支内补齐 `grad_attn_bias` 后统一返回 5 个值。

#### 4.4.3 源码精读

backward 全貌：

[functional.py:852-943](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L852-L943) —— 第 857–860 行用 `meta.forward_meta` 重算 `use_aten_small_d_forward`（**不是** `backward_meta`）；第 862 行 `if not use_aten_small_d_forward` 内部再按 `meta.backward_meta` 分流。

大 D · Triton 反向：

[functional.py:863-889](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L863-L889) —— 透传 `autotune`/`enable_tma`/`enable_ws`/`persist_dkdv`/`split_launch`/`grad_*_storage_dtype` 等反向旋钮；`philox_seed/offset` 从 `rng_state` 还原（`int(rng_state[0].item()) if rng_state.numel() else 0`），实现 dropout 掩码重放；`return_attn_bias_grad=ctx.needs_input_grad[3]` 决定是否算 `attn_bias` 的梯度。

大 D · CuTeDSL 反向：

[functional.py:890-904](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L890-L904) —— 布局转换与 kernel 分发在 `_ffpa_attn_backward_cute` 内部；`grad_attn_bias = None`（CuTeDSL 不支持 attn_mask）。

大 D · SDPA 反向：

[functional.py:905-923](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L905-L923) —— `assert isinstance(meta.backward_meta, SDPABackend)`，调 `_efficient_attn_backward_aten`（包装 `torch.ops.aten._scaled_dot_product_efficient_attention_backward`，见 [_efficient_bwd.py:50-187](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/aten/_efficient_bwd.py#L50-L187)）。这条路径处理 GQA 的 expand-reduce、LSE 对齐、可选 fp32 高精度等 SDPA 特有的适配。

小 D · aten flash 反向：

[functional.py:924-940](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L924-L940) —— `_flash_attn_backward_aten` 是 `_flash_attention_forward` 的反向配对（见 [_flash_bwd.py:8-40](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/aten/_flash_bwd.py#L8-L40)），返回 3 元组。

返回值对齐 forward 的 5 个输入：

[functional.py:942-943](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L942-L943) —— `return dq, dk, dv, grad_attn_bias, None`，最后的 `None` 对应非张量的 `meta`。

#### 4.4.4 代码实践

**实践目标**：用 `backward_backend` 跑通一次「Triton 前向 + SDPA 反向」的混搭，验证反向确实走了 `_efficient_attn_backward_aten`。

**操作步骤**：

1. 参考 [tests/test_ffpa_bwd.py:911-930](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tests/test_ffpa_bwd.py#L911-L930) 的 `test_ffpa_bwd_basic`，构造大 D 输入：
   ```python
   # 示例代码：基于 test_ffpa_bwd_basic 改写
   import math, torch
   from ffpa_attn import ffpa_attn_func
   B, H, N, D = 1, 8, 8192, 512
   q = torch.randn(B, H, N, D, dtype=torch.float16, device="cuda", requires_grad=True)
   k = torch.randn_like(q).requires_grad_(True)
   v = torch.randn_like(q).requires_grad_(True)
   scale = 1.0 / math.sqrt(D)
   out = ffpa_attn_func(q, k, v, scale=scale,
                        forward_backend="triton", backward_backend="sdpa")
   out.sum().backward()   # 命中 _efficient_attn_backward_aten
   ```
2. 在 [functional.py:905-923](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L905-L923) 的 `_efficient_attn_backward_aten(...)` 调用前临时加一行 `print("sdpa backward")`，重跑确认打印出现。

**需要观察的现象**：`backward_backend="sdpa"` 时，反向进入 `else: assert isinstance(meta.backward_meta, SDPABackend)` 分支；`q.grad/k.grad/v.grad` 形状与输入一致、dtype 为 fp16。

**预期结果**：混搭合法，梯度可与纯 Triton 反向对比（容差参考 [_efficient_bwd.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/aten/_efficient_bwd.py) 与测试里的 `_tolerance`）。能否运行取决于本地是否有大 D（D=512）可用的 CUDA 设备，否则标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：为什么 backward 判断「大小 D」时看的是 `forward_meta` 而不是 `backward_meta`？

**参考答案**：因为「这次反向对应的**前向**用的是小 D flash 还是 大 D FFPA」是前向的属性，记录在 `forward_meta` 与 head_dim 上。反向必须与前向配对：前向走了小 D flash，反向就必须走 `_flash_attn_backward_aten`（它消费 forward 留下的 `unused` 与 aten 风格 rng_state）。`backward_meta` 只在「大 D 反向」内部决定用 Triton / CuTeDSL / SDPA，不影响大小 D 的拆分。

**练习 2**：`forward_backend="cuda"` 时，`backward_backend` 可以填哪些值？为什么不能填 `"cuda"`？

**参考答案**：只能填 `"triton"` 或 `"sdpa"`。因为 `CUDABackend` 强制 `backward=False`（[functional.py:164](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L164) `assert not self.backward`），不存在 CUDA 反向 kernel；填 `"cuda"` 会在构造 `CUDABackend(backward=True)` 时直接断言失败。这正是 [functional.py:946-965](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L946-L965) 注释里「cuda │ triton, sdpa」那一行的来源。

---

### 4.5 _reserve_large_d_dropout_rng：dropout 的 Philox RNG 预留

#### 4.5.1 概念说明

dropout 在前向随机置零一部分 attention score，反向必须用**完全相同**的掩码。FlashAttention 类实现靠的是 Philox 伪随机数发生器：给定相同的 `(seed, offset)`，Philox 会重放出同一个随机序列。所以 forward 只要把「用了哪个 seed、从哪个 offset 开始消费」记下来，backward 就能重放掩码。

但有个坑：FFPA 的大 D Triton/CUDA dropout 想和 PyTorch 原生 SDPA 的 dropout **逐位对齐**（这样两者结果可比、可互换）。PyTorch efficient attention 的约定是：为每一个逻辑 attention score `[B, Hq, Nq, Nkv]` 预留一个随机数，并且把 generator 的 offset 向上取整到 4 的倍数（Philox 一次产出 4 个数）。`_reserve_large_d_dropout_rng` 就是来复刻这套约定的。

#### 4.5.2 核心流程

```
def _reserve_large_d_dropout_rng(q, k, dropout_p):
    if dropout_p ≤ 0.0:                       # 不开 dropout，返回空 int64
        return empty(0, int64)
    if q.device.type != "cuda":
        raise RuntimeError(...)               # 大 D dropout 仅支持 CUDA

    seed   = torch.cuda.initial_seed()
    offset = torch.cuda._get_rng_state_offset()
    attn_elems = B * Hq * Nq * Nkv            # 逻辑 attention score 总数
    offset_increment = ⌈attn_elems / 4⌉ × 4   # 向上取整到 4 的倍数
    torch.cuda._set_rng_state_offset(offset + offset_increment)  # 推进全局 offset
    return tensor([seed, offset], int64)      # 存 CPU，供 backward 重放
```

关键数学：注意力分数总数为 \(N_{\text{elems}} = B \cdot H_q \cdot N_q \cdot N_{kv}\)。Philox 每 4 个输出为一组，故 offset 推进量为

\[
\Delta = \left\lceil \frac{N_{\text{elems}}}{4} \right\rceil \times 4
\]

代码里 `((attn_elems + 3) // 4) * 4` 正是这个 \(\Delta\) 的整数实现。推进全局 offset 是为了不让本次 dropout 消费的随机数和后续其他 RNG 抽取撞车。

#### 4.5.3 源码精读

函数全貌：

[functional.py:316-338](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L316-L338) —— 三个要点：(1) `dropout_p ≤ 0` 早返回空张量；(2) `attn_elems = q.size(0)*q.size(1)*q.size(2)*k.size(2)`（注意 KV 长度取自 `k.size(2)`）；(3) 用 `torch.cuda._set_rng_state_offset` 推进全局 offset，返回 CPU `int64` 的 `[seed, offset]`。

docstring 解释了「为何要复刻 SDPA 的 Philox 约定」：

[functional.py:321-327](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L321-L327) —— 明说 PyTorch efficient attention 为每个 `[B, Hq, Nq, Nkv]` 分数预留一个随机数，并把 offset 取整到 4 的倍数；返回的 CPU int64 张量存 `[seed, offset]` 供反向重算。

forward 如何使用返回值（透传给前向 kernel 的 `philox_seed/offset`）：

[functional.py:777-790](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L777-L790)（CUDA）与 [functional.py:796-809](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L796-L809)（Triton）—— `int(rng_state[0].item()) if rng_state.numel() else 0`：空张量（未开 dropout）给 0，否则给真实的 seed/offset。

backward 如何还原（重放掩码）：

[functional.py:883-884](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L883-L884)（Triton）与 [functional.py:921-922](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L921-L922)（SDPA）—— 用同样的 `int(rng_state[0].item()) ...` 取回 seed/offset，喂给反向 kernel，让它重放出与 forward 相同的 dropout 掩码。

#### 4.5.4 代码实践

**实践目标**：理解 dropout 开关对 RNG offset 的副作用。

**操作步骤**：

1. 读 [functional.py:316-338](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L316-L338)。
2. 跟踪一次 `dropout_p=0.2`、`B=1,H=8,Nq=Nkv=512` 的前向：`attn_elems = 1*8*512*512 = 2097152`，\(\Delta = \lceil 2097152/4\rceil \times 4 = 2097152\)（本身就是 4 的倍数）。所以全局 RNG offset 会前进 2097152。
3. 参考 [tests/test_ffpa_bwd.py:790-810](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tests/test_ffpa_bwd.py#L790-L810) 的 `test_ffpa_bwd_triton_dropout_matches_sdpa`，看测试如何用相同 seed 让 FFPA dropout 与 SDPA dropout 逐位对齐。

**需要观察的现象**：只要固定 `torch.manual_seed(...)`，FFPA 大 D dropout 与 SDPA 的置零位置应一致（因为两者复刻了同一套 Philox 约定），所以反向重放出的掩码与 forward 完全相同。

**预期结果**：你能解释「为什么 backward 能正确重放 dropout」——forward 把 `(seed, offset)` 存进 `rng_state`，反向用同样的 `(seed, offset)` 重新生成掩码。具体对齐精度（与 SDPA 的 max_abs_err）待本地验证。

#### 4.5.5 小练习与答案

**练习 1**：若 `dropout_p=0`，`_reserve_large_d_dropout_rng` 返回什么？forward/backward 如何处理？

**参考答案**：返回 `torch.empty(0, dtype=torch.int64)`（空张量，numel=0）。forward/backward 里用 `int(rng_state[0].item()) if rng_state.numel() else 0` 处理：numel=0 时给 seed=0、offset=0（占位，反正不开 dropout 不会消费随机数）。

**练习 2**：为什么 `offset_increment` 要向上取整到 4 的倍数，而不是直接等于 `attn_elems`？

**参考答案**：Philox 一次产出 4 个 uint32。PyTorch efficient attention 把 generator offset 对齐到 4 的倍数，是为了让后续的 RNG 抽取从一组完整的 Philox 输出开始，避免和本次 dropout 消费的「半个 Philox 组」重叠。FFPA 复刻这个约定，才能与 SDPA 的 dropout 逐位对齐、保证 `dropout_p>0` 时两者数值可比。数学上即 \(\Delta = \lceil N_{\text{elems}}/4\rceil \times 4\)。

---

## 5. 综合实践

把本讲四个模块串起来，完成下面这个贯穿任务。

**任务**：画出 `_FFPAAttnFunc.forward` 的完整决策树，并解释「为什么 CUDA 前向可以配 Triton 或 SDPA 反向」。

**步骤 1 · 画 forward 决策树**。阅读 [functional.py:759-829](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L759-L829)，按下面骨架补全每个分支调用的函数名与返回值：

```
forward(q, k, v, attn_bias, meta)
│
├─ head_dim ≤ 256 且 未开 small-D ?  ── 是 ──→ aten: _flash_attn_forward_aten  → (O, lse, rng_state, unused)
│   否
├─ forward_meta 是 CUDABackend ?     ── 是 ──→ cuda: _ffpa_attn_forward_cuda   → (O, lse) + 自备 rng_state
│   否
├─ forward_meta 是 TritonBackend ?   ── 是 ──→ triton: _ffpa_attn_forward_triton → (O, lse) + 自备 rng_state
│   否
├─ forward_meta 是 CuTeDSLBackend ?  ── 是 ──→ cutedsl: _ffpa_attn_forward_cute → (O, lse) + 空 uint8 rng_state
│   否
└─ raise ValueError
```

标注每一刀切在哪个变量上：第一刀切 `head_dim`（经 `_should_use_aten_small_d_forward`），第二刀切 `forward_meta` 的 `isinstance`。

**步骤 2 · 解释 CUDA 前向配 Triton/SDPA 反向**。要点（结合源码组织你的答案）：

1. `CUDABackend` 强制 `backward=False`（[functional.py:164](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L164)），不存在 CUDA 反向 kernel，故 `backward_meta` 不能是 CUDABackend。
2. CUDA 前向只用于大 D（D>256），所以 `use_aten_small_d_forward=False`，backward 进入 [functional.py:862](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L862) 的大 D 反向分支，按 `backward_meta` 选 Triton 或 SDPA。
3. forward 已经把反向所需的全部中间量（`O`、`lse`、`rng_state`）存进 `save_for_backward`（[functional.py:837-846](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L837-L846)）。反向 kernel 只消费这些中间量，不关心前向是谁产生的——只要 `O` 和 `lse` 数值正确，反向就能算出正确的 dQ/dK/dV。
4. 因此 CUDA 前向 + Triton 反向、CUDA 前向 + SDPA 反向 都合法，对应 [functional.py:946-965](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L946-L965) 注释矩阵的 `cuda │ triton, sdpa` 一行。

**步骤 3 · 用代码验证（可选，需大 D GPU）**。仿照 [tests/test_ffpa_bwd.py:911-930](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tests/test_ffpa_bwd.py#L911-L930)，分别用 `forward_backend="triton", backward_backend="sdpa"` 与 `forward_backend="triton", backward_backend="triton"` 跑同一组大 D 输入，对比两组 `q.grad` 是否在容差内一致——若一致，说明前向/反向解耦确实让反向后端可独立替换。无 GPU 则标注「待本地验证」。

## 6. 本讲小结

- `_FFPAAttnFunc` 是承载真实 forward/backward 的 `torch.autograd.Function`；对外暴露的 `FFPAAttnFunc.apply` 经 `_ffpa_apply`（`@torch._dynamo.disable`）绕一层，在 autograd 边界主动断图，保护手写反向不被 torch.compile 吞掉。
- forward 先用 `head_dim`（经 `_should_use_aten_small_d_forward`）判大小 D，再用 `forward_meta` 的类型在大 D 里分 cuda / triton / cutedsl 三路；小 D 一律走 aten flash。
- `save_for_backward` 保存 7 个张量（q/k/v/O/lse/rng_state/unused，全部 contiguous），`attn_bias` 与 `meta` 挂 ctx 属性；只有 `is_grad=True`（训练）才保存，推理跳过。
- backward 的关键不对称：大小 D 看 `forward_meta`、大 D 后端看 `backward_meta`，由此实现前向/反向解耦与「一对多」分发；CUDA 无反向，故 CUDA 前向只能配 Triton 或 SDPA 反向。
- `_reserve_large_d_dropout_rng` 复刻 SDPA 的 Philox 约定，按 \(N_{\text{elems}}=B\cdot H_q\cdot N_q\cdot N_{kv}\) 计算并推进 offset \(\Delta=\lceil N_{\text{elems}}/4\rceil\times4\)，存 `[seed, offset]` 供反向重放 dropout 掩码。
- 注释矩阵（[functional.py:946-965](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L946-L965)）是理解「为何不用 `register_autograd`」的钥匙，其 torch.compile 细节留待 u3-l5。

## 7. 下一步学习建议

- **u3-l5（torch.compile 与自定义算子）**：本讲多次提到的 `register_autograd` 限制、`_ffpa_apply` 图断、`ffpa_attn::_fwd_cuda` 的 define/impl/register_fake，都会在那里展开。建议接着读 [functional.py:946-987](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L946-L987) 与 [cuda/__init__.py:22-101](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cuda/__init__.py#L22-L101)。
- **u4-l1（Triton 前向 kernel）**：本讲把 `_ffpa_attn_forward_triton` 当黑盒，下一讲进去看它的 online softmax 主循环。
- **u5-l1（反向算法与 delta 预处理）**：本讲把 `_ffpa_attn_backward_triton` 当黑盒，u5 会解释反向为何需要保存 `O` 与 `lse`、delta 预处理在做什么。
- **u6-l2（CuTeDSL 布局转换）**：本讲提到 `_ffpa_attn_forward_cute` 内部做 `[B,H,N,D]↔[B,N,H,D]` 转换，细节在 u6。
- 若想立刻动手，可回到 [tests/test_ffpa_bwd.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tests/test_ffpa_bwd.py) 跑 `test_ffpa_bwd_basic`，用 `backward_backend` 切换反向后端，观察本讲的分发在真实运行中如何落地。
