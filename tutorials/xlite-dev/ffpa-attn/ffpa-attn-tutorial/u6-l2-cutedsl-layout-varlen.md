# CuTeDSL 布局转换、校验与 varlen 接入

> 承接：[u6-l1 CuTeDSL 后端总览与 SM80/SM90 分发](u6-l1-cutedsl-overview-sm80-sm90.md) 讲清楚了「该不该用 CuTeDSL、用哪条 SM 路径」的门禁。本讲回答下一个问题：**确定要用 CuTeDSL 后，从公共 API 到真正 kernel 之间，这一层薄薄的 Python『胶水』做了哪些事？**

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 SDPA 布局 `[B, H, N, D]` 与 FlashAttention（CuTeDSL 原生）布局 `[B, N, H, D]` 的差异，以及为什么 dense 路径要在入口做一次转置、varlen 路径却不用转置。
- 读懂 `_ffpa_attn_forward_cute` / `_ffpa_attn_backward_cute` 这一对入口 shim（垫片），能手动追踪一次 dense 前向「转置 → 调 op → 反转置」的全过程。
- 理解 `_check_supported_options` 为什么要把一长串 FlashAttention 扩展选项统一拒绝，以及它「只报错、不回退」的设计原则。
- 看懂 varlen 路径用 `custom_op` + `register_autograd` 自管 autograd 边界的原因，以及它与 dense 路径「不绑反向」的根本差异。
- 认识 varlen 内部两个关键辅助件：`seqlen_info.py`（按 batch 切片）与 `pack_gqa.py`（GQA 头折叠进序列维）。

## 2. 前置知识

在进入源码前，先用三段话把要用的术语铺平。

**两种注意力张量布局。** PyTorch 的 `scaled_dot_product_attention`（SDPA）约定输入是 `[B, H, N, D]`：四个维度分别是 batch、注意力头数 H、序列长度 N、头维度 D。而 FlashAttention 家族（包括 CuTeDSL 后端）习惯把头放在序列之后，即 `[B, N, H, D]`。两者只是把第 1、2 维交换了一下（`transpose(1, 2)`），但这一换会导致张量在内存里不再连续，所以通常要跟一个 `.contiguous()` 把数据真正重排。

**密集（dense）与变长（varlen）。** 密集注意力每个 batch 的序列都一样长，用 `[B, H, N, D]` 直接表示；变长注意力里各序列长度不同，为了避免把短序列补齐（padding）到最长而浪费算力，把整个 batch 的 token 首尾相接拍平成 `[T, H, D]`（T 是总 token 数），再用一个长度 B+1 的累计偏移张量 `cu_seqlens` 标出每条序列的起止边界。这种打包方式常叫 **THD 布局** 或 packed 布局。

**torch 自定义算子的三种注册方式。** `torch.library` 提供三件套：`define` 声明算子的参数与返回 schema、`impl("CUDA")` 给出真实 CUDA 实现、`register_fake` 给出一个「假实现」（meta 实现），让 `torch.compile` 在追踪（trace）阶段就能推出输出形状而不真正运行 kernel。此外还有 `custom_op`（一种更现代、声明式写法）和 `register_autograd`（把某个前向算子绑定到一个反向函数，让 autograd 自动调用它）。本讲会看到 dense 用前三件套**但不绑反向**，varlen 用 `custom_op` **并绑反向**，原因后面细讲。

如果对 `torch.compile` 与图断（graph break）还不熟，建议先读 [u3-l5 torch.compile 兼容与 torch.library 自定义算子](u3-l5-torch-compile-custom-ops.md)。

## 3. 本讲源码地图

本讲涉及的文件都围绕「入口胶水」这一层：

| 文件 | 作用 |
| --- | --- |
| [src/ffpa_attn/cute/__init__.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/__init__.py) | **本讲主角**。存放 dense/varlen 入口 shim、布局转换、选项校验、以及所有 torch 自定义算子的注册。 |
| [src/ffpa_attn/functional.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py) | 分发层。`_FFPAAttnFunc.forward/backward` 在选中 CuTeDSL 时调用本讲的 dense shim。 |
| [src/ffpa_attn/cute/_utils.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_utils.py) | 常量（最小/最大 head_dim）与各种校验函数、可选 int 的编码工具。 |
| [src/ffpa_attn/cute/utils/seqlen_info.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/utils/seqlen_info.py) | varlen 内部把「序列长度信息」打包成一个对象，kernel 每个 tile 只读一次 batch 边界。 |
| [src/ffpa_attn/cute/utils/pack_gqa.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/utils/pack_gqa.py) | varlen 的 GQA 优化：把多个共享 KV 的 query 头折叠进序列维，提升访存局部性。 |

一句话定位：`cute/__init__.py` 是「门面 + 注册台」，`functional.py` 是「上游调度员」，`_utils.py` 是「规则手册」，后两个 `utils/` 文件是 varlen kernel 内部要用的「数据打包工具」。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：① dense 入口的布局转换；② dense torch 算子的注册与 SM 分发；③ 选项统一拒绝 `_check_supported_options`；④ varlen 接入与自管 autograd。

---

### 4.1 dense 入口：SDPA `[B,H,N,D]` ↔ FA `[B,N,H,D]` 布局转换

#### 4.1.1 概念说明

公共 API `ffpa_attn_func` 与 PyTorch SDPA 对齐，输入输出都用 `[B, H, N, D]`（头在序列前）。但 CuTeDSL 后端真正的 kernel（`_ffpa_attn_forward_sm80` / `_ffpa_attn_forward_sm90` 等）沿用的是 FlashAttention 惯例 `[B, N, H, D]`（头在序列后）。这两套布局不能直接对接，需要一层**入口 shim（垫片函数）**在两边做翻译：

- 入口：把用户传进来的 `[B, H, N, D]` 转成 `[B, N, H, D]` 喂给 kernel；
- 出口：把 kernel 吐出来的 `[B, N, H, D]` 转回 `[B, H, N, D]` 还给用户。

把转换集中在这层 shim 里，有两个好处：第一，kernel 内部不需要关心 SDPA 的布局约定，保持与上游 FlashAttention 代码同构；第二，所有调用 kernel 的代码路径都经过同一处转换，不会有的转了有的没转。注意 **varlen 路径不需要这层转置**——它的 packed `[T, H, D]` 本来就是 FA 原生布局（见 4.4）。

#### 4.1.2 核心流程

dense 前向的胶水流程只有三步：

```
用户输入 q,k,v : [B, H, N, D]
        │
        ▼  (1) _bhnd_to_bnhd：transpose(1,2).contiguous()
q_nhd, k_nhd, v_nhd : [B, N, H, D]
        │
        ▼  (2) torch.ops.ffpa_attn._fwd_cute(...)  ← 注册过的 torch 算子
out_nhd : [B, N, H, D],  lse : [B, H, N]
        │
        ▼  (3) _bnhd_to_bhnd：把 out 转回 [B, H, N, D]；lse 不动
返回 (out, lse)
```

关键细节：输出 `out` 要反转置，但 `lse`（log-sum-exp，反向要用）**不用转**。原因是 kernel 写 `lse` 时就按 `[batch, num_head, seqlen]` 这个「头在前」的顺序写了，正好就是 SDPA 友好的 `[B, H, N]`，所以直接返回。

反向 shim 同理，只是要转的张量更多：`q/k/v/out/dout` 五个都要 BHND→NHD，算完的三个梯度 `dq/dk/dv` 要 NHD→BHND 转回。

#### 4.1.3 源码精读

先看一对极简的转换工具，它们就是 `transpose(1, 2)` 加 `.contiguous()`：

```python
def _bhnd_to_bnhd(t: torch.Tensor) -> torch.Tensor:
  """Reshape [B, H, N, D] (SDPA) to the CuTeDSL-native [B, N, H, D] (FA)."""
  return t.transpose(1, 2).contiguous()


def _bnhd_to_bhnd(t: torch.Tensor) -> torch.Tensor:
  """Reverse of _bhnd_to_bnhd: FA [B, N, H, D] → SDPA [B, H, N, D]."""
  return t.transpose(1, 2).contiguous()
```

—— [_bhnd_to_bnhd / _bnhd_to_bhnd，cute/__init__.py:292-299](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/__init__.py#L292-L299)：交换第 1（H）和第 2（N）维。`.contiguous()` 不可省——`transpose` 只产生一个带新 stride 的「视图」，数据并未搬动，直接传给底层 kernel 会因非连续布局而出错或降速，所以这里强制物化成连续内存。

接着是前向 shim 主体：

```python
def _ffpa_attn_forward_cute(q, k, v, softmax_scale, causal, *, return_lse=True):
  requires_grad = any(t.requires_grad for t in (q, k, v))
  _require_cute_supported(q, k, v, requires_grad=requires_grad)   # 张量级校验

  q_nhd, k_nhd, v_nhd = (_bhnd_to_bnhd(t) for t in (q, k, v))     # BHND → NHD
  out_nhd, lse = torch.ops.ffpa_attn._fwd_cute(                   # 调注册算子
    q_nhd, k_nhd, v_nhd, softmax_scale, int(causal), int(return_lse),
  )
  out_bhnd = _bnhd_to_bhnd(out_nhd)                               # NHD → BHND
  return out_bhnd, lse                                             # lse 不转
```

—— [_ffpa_attn_forward_cute，cute/__init__.py:302-345](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/__init__.py#L302-L345)。注意三个细节：① 先调 `_require_cute_supported` 做张量级硬校验（设备、架构、head_dim 区间、dtype、q/k/v 同 head_dim），不通过直接抛错、不回退（这点和 Triton 的静默回退 SDPA 截然不同，详见 u6-l1）；② 布尔参数 `causal`/`return_lse` 被转成 `int` 再传，因为 torch 自定义算子的 schema 要求基本类型；③ `lse` 原样返回。

反向 shim 结构对称，只是多了一个 `grad_kv_storage_dtype` 参数要先编码成整数再过算子边界：

—— [_ffpa_attn_backward_cute，cute/__init__.py:348-399](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/__init__.py#L348-L399)：把 `q/k/v/out/grad_out` 五个张量 BHND→NHD，调 `torch.ops.ffpa_attn._bwd_cute`，再把 `dq/dk/dv` 三个梯度 NHD→BHND 返回。

最后看上游是谁在调用前向 shim。在分发层 `_FFPAAttnFunc.forward` 里，当 `meta.forward_meta` 是 `CuTeDSLBackend` 时走这一支：

```python
elif isinstance(meta.forward_meta, CuTeDSLBackend):
  # CuTeDSL backend. Layout conversion (B,H,N,D ↔ B,N,H,D) is
  # handled inside _ffpa_attn_forward_cute.
  O, lse = _ffpa_attn_forward_cute(
    q, k, v,
    softmax_scale=meta.attn_meta.scale,
    causal=meta.attn_meta.is_causal,
    return_lse=True,
  )
  rng_state = torch.empty(0, dtype=torch.uint8, device=q.device)  # CuTeDSL 无 dropout
```

—— [_FFPAAttnFunc.forward 的 cutedsl 分支，functional.py:813-825](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L813-L825)。注释明确说「布局转换在 shim 内部处理」，所以分发层只管传 SDPA 布局的 q/k/v，干净利落。还顺带把 `rng_state`（dropout 随机数状态）置空——因为 CuTeDSL 根本不支持 dropout。

#### 4.1.4 代码实践

**实践目标**：手动追踪一次 dense CuTeDSL 前向，画出从 `ffpa_attn_func` 到 `_ffpa_attn_forward_cute` 的完整调用链与每一步的张量形状。

**操作步骤（源码阅读型，无需 GPU）**：

1. 从 [ffpa_attn_interface.py:170-181](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/ffpa_attn_interface.py#L170-L181) 看 `ffpa_attn_func` 末尾：它做完 `normalize` 后调用 `FFPAAttnFunc.apply(...)`。
2. `FFPAAttnFunc.apply` 实际是 [_ffpa_apply，functional.py:985-987](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L985-L987)（带 `torch._dynamo.disable` 图断）。
3. 进入 [_FFPAAttnFunc.forward，functional.py:746-850](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L746-L850)，`head_dim` 大于 256 且 `forward_meta` 是 `CuTeDSLBackend` 时，命中 4.1.3 引用的 cutedsl 分支。
4. 该分支调用 [_ffpa_attn_forward_cute，cute/__init__.py:302-345](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/__init__.py#L302-L345)，完成转置→op→反转置。

**需要观察/记录的现象**：在下表填入每一步 q 的形状（以 `B=2, H_q=32, N_q=1024, D=512` 为例）：

| 步骤 | 位置 | q 的形状 |
| --- | --- | --- |
| 用户传入 `ffpa_attn_func` | interface | `[2, 32, 1024, 512]` |
| `_bhnd_to_bnhd` 之后 | shim 内 | `[2, 1024, 32, 512]` |
| `_fwd_cute` 返回的 out | shim 内 | `[2, 1024, 32, 512]` |
| `_bnhd_to_bhnd` 之后 | shim 返回 | `[2, 32, 1024, 512]` |
| 最终返回用户的 O | forward 返回 | `[2, 32, 1024, 512]` |

**预期结果**：进出的用户视角形状始终是 `[B, H, N, D]`，只有 shim 内部短暂变为 `[B, N, H, D]`；`lse` 全程是 `[B, H, N]`=`[2, 32, 1024]` 不变。若你在 SM80+/SM90 的 GPU 上实际运行（需安装 CuTeDSL 依赖），可用下面脚本验证（**待本地验证**）：

```python
# 示例代码：仅当本地有 SM8x/SM90 GPU + cutlass/quack 依赖时可运行
import torch, ffpa_attn
from ffpa_attn import ffpa_attn_func
B, H, N, D = 2, 32, 1024, 512
q = torch.randn(B, H, N, D, device="cuda", dtype=torch.bfloat16)
k = torch.randn_like(q); v = torch.randn_like(q)
out = ffpa_attn_func(q, k, v, forward_backend="cutedsl")
print(out.shape)   # 预期 torch.Size([2, 32, 1024, 512])
```

#### 4.1.5 小练习与答案

**练习 1**：为什么 `_bhnd_to_bnhd` 末尾必须 `.contiguous()`，而去掉它会出什么问题？

**参考答案**：`transpose(1, 2)` 只改 stride、不搬数据，得到的是非连续视图。下游 CuTeDSL kernel（及它注册的 torch 算子的 `register_fake`/CUDA 实现）假设张量按 `[B, N, H, D]` 连续排布来计算指针偏移与 TMA 描述符，喂入非连续视图会导致读到错位的元素或直接报错；`.contiguous()` 强制物化成连续内存以匹配 kernel 的布局假设。

**练习 2**：前向 shim 返回 `lse` 时没有调用 `_bnhd_to_bhnd`，这是不是漏写了？

**参考答案**：不是。kernel 在 `_fwd_cute_torch_op` 里分配 `lse` 时用的形状就是 `(batch, num_head, seqlen_q)`（见 4.2.3），即头在前的 `[B, H, N]`，本来就是 SDPA 友好布局，无需再转。

---

### 4.2 dense torch 算子的三件套注册与 SM80/SM90 分发

#### 4.2.1 概念说明

4.1 里的 shim 调的是 `torch.ops.ffpa_attn._fwd_cute`——这不是一个普通 Python 函数，而是一个**注册进 torch 库的自定义算子**。为什么要注册？因为 FFPA 的 kernel 是手写/CuTeDSL 生成的「黑盒」，要让 `torch.compile` 把它当成一个合法的、不可内联的算子节点来对待，就得用 `torch.library` 三件套登记它的「身份证」（schema）和「形状规则」（fake 实现）。

回顾 u3-l5 讲过的三件套：

- `define`：声明算子的参数类型与返回类型（schema）；
- `impl("CUDA")`：给出真正的 CUDA 实现（懒导入 kernel，避免无 GPU 时 import 报错）；
- `register_fake`：给出 meta 实现，让 Dynamo 在 trace 期推出输出形状/dtype，而无需真跑 kernel。

**本模块的重点是**：dense 路径**只注册前三件套，刻意不调 `register_autograd`**——反向不绑定到这个算子上，而是留在 `_FFPAAttnFunc.backward` 里由运行时 `meta.backward_meta` 决定。这与 varlen 路径（4.4）形成鲜明对比，是理解两条路径差异的钥匙。

#### 4.2.2 核心流程

dense 前向算子的注册与执行流程：

```
模块导入时（一次性）：
  torch.library.define("ffpa_attn::_fwd_cute", schema)        ← 登记 schema
  @torch.library.impl("ffpa_attn::_fwd_cute", "CUDA")         ← 真 CUDA 实现
  @torch.library.register_fake("ffpa_attn::_fwd_cute")        ← meta 实现
  （不调 register_autograd）                                    ← 反向留给 _FFPAAttnFunc

运行时一次调用：
  _ffpa_attn_forward_cute 调 torch.ops.ffpa_attn._fwd_cute(...)
      └─> _fwd_cute_torch_op(q,k,v,...)
            ├─ 分配 o（输出）、lse
            └─ _forward_impl_for_device(device, head_dim, head_dim_v)
                  ├─ 若 _use_sm90_specialized(...) 为真 → _ffpa_attn_forward_sm90
                  └─ 否则                                → _ffpa_attn_forward_sm80
```

`_forward_impl_for_device` 是「选哪条 SM 路径」的决策点，它复用 u6-l1 讲过的同一个谓词 `_use_sm90_specialized`：只有 Hopper（major==9）且 q、v 的 head_dim 都对称落在 `[320, 512]` 才走 SM90 专用 kernel，其余（含 Blackwell、Hopper 上 D>512）一律走 SM80 通用 Split-D 兜底。

#### 4.2.3 源码精读

先看 schema 定义与 CUDA 实现：

```python
torch.library.define(
  "ffpa_attn::_fwd_cute",
  "(Tensor q, Tensor k, Tensor v, float softmax_scale, int causal, int return_lse) "
  "-> (Tensor o, Tensor lse)",
)

@torch.library.impl("ffpa_attn::_fwd_cute", "CUDA")
def _fwd_cute_torch_op(q, k, v, softmax_scale, causal, return_lse):
  batch, seqlen_q, num_head, head_dim_v = q.shape            # q 已是 [B,N,H,D]
  o = torch.empty(batch, seqlen_q, num_head, head_dim_v, dtype=q.dtype, device=q.device)
  need_lse = bool(return_lse)
  lse = (torch.empty(batch, num_head, seqlen_q, dtype=torch.float32, device=q.device)
         if need_lse else torch.empty(0, device=q.device))   # lse 形状 [B,H,N]
  _forward_impl_for_device(q.device, q.size(-1), v.size(-1))(
    q, k, v, softmax_scale=softmax_scale, causal=bool(causal),
    return_lse=need_lse, out=o, lse=lse if need_lse else None,
  )
  return o, lse
```

—— [dense 前向算子 define + impl，cute/__init__.py:518-554](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/__init__.py#L518-L554)。注意 `lse` 分配成 `(batch, num_head, seqlen_q)`，正是 4.1 里「lse 不用转置」的根源——它在 kernel 出口就已经是头在前的 `[B, H, N]` 了。还要注意 schema 里所有布尔/可选语义都被压成 `int`（`causal`、`return_lse`），因为 torch schema 只接受基本标量类型。

再看 meta 实现（fake）：

```python
@torch.library.register_fake("ffpa_attn::_fwd_cute")
def _fwd_cute_fake(q, k, v, softmax_scale, causal, return_lse):
  o = torch.empty_like(q)
  lse = (q.new_empty(q.size(0), q.size(-2), q.size(-3), dtype=torch.float32)
         if return_lse else q.new_empty(0))
  return o, lse
```

—— [_fwd_cute_fake，cute/__init__.py:557-571](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/__init__.py#L557-L571)。`register_fake` 只用张量的「形状/dtype/device」属性（FakeTensor）推出输出，不分配真显存。`lse` 的形状从 q 的 size 反推：`q.size(0)`=batch、`q.size(-2)`=num_head、`q.size(-3)`=seqlen_q，与真实现里 `(batch, num_head, seqlen_q)` 逐字对齐——**meta 实现必须与真实现形状严格一致**，否则 torch.compile 会图前后矛盾。

最后看 SM 路径分发器：

```python
def _forward_impl_for_device(device, head_dim, head_dim_v):
  major = _cute_device_major(device)
  if major < 8:
    raise NotImplementedError(f"cutedsl forward requires compute capability >= 8.0; got {major}.x")
  if _use_sm90_specialized(major, head_dim, head_dim_v):
    return _ffpa_attn_forward_sm90
  return _ffpa_attn_forward_sm80
```

—— [_forward_impl_for_device，cute/__init__.py:203-213](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/__init__.py#L203-L213)。`_backward_impl_for_device`（[:216-226](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/__init__.py#L216-L226)）结构完全对称。这两者把「设备能力 → 具体 kernel 函数」的映射收敛到一处，被 dense 的 `_fwd_cute`/`_bwd_cute` 和 varlen 的 `_varlen_fwd_cute`/`_varlen_bwd_cute` 共用。

关于「为什么不绑反向」：模块里 `_bwd_cute` 同样走 define+impl+register_fake 三件套（[_bwd_cute 算子，cute/__init__.py:574-673](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/__init__.py#L574-L673)），但没有任何 `register_autograd("ffpa_attn::_fwd_cute", ...)`。反向 `_bwd_cute` 是一个「被主动调用的工具算子」，由 `_FFPAAttnFunc.backward` 在选中 CuTeDSL 反向时显式调用（[functional.py:890-904](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L890-L904)），而不是被 autograd 引擎按绑定公式自动触发。原因见 4.4.1。

#### 4.2.4 代码实践

**实践目标**：确认 dense 路径「不绑反向」的事实，并理解一前向多反向的由来。

**操作步骤（源码阅读型）**：

1. 在 [cute/__init__.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/__init__.py) 中搜索 `register_autograd`，你会发现它只出现在 varlen 段（[:940](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/__init__.py#L940)），dense 的 `_fwd_cute`/`_bwd_cute` 完全没有。
2. 对照 [functional.py:890-904](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L890-L904)：`backward` 里 `isinstance(meta.backward_meta, CuTeDSLBackend)` 时主动调 `_ffpa_attn_backward_cute`；而同一个文件里 `backward` 还有 `TritonBackend`、`SDPABackend` 分支。也就是说，**同一个 CuTeDSL 前向，反向可以被配置成 cutedsl / triton / sdpa 三者之一**（由 `backward_backend` 决定）。

**需要观察的现象**：dense 前向算子 `_fwd_cute` 没有绑定任何反向函数；反向算子 `_bwd_cute` 是独立注册、被 `_FFPAAttnFunc.backward` 按运行时 meta 主动调用的工具。

**预期结果**：你能用自己的话解释——正因为「一前向多反向」，FFPA 才不能用 `register_autograd` 给 `_fwd_cute` 绑死唯一反向（那样会在 `torch.compile(fullgraph=True)` 下静默忽略用户的 `backward_backend` 选择）。这条理由与 u3-l5 给出的整体设计完全一致。

#### 4.2.5 小练习与答案

**练习 1**：`_fwd_cute` 的 schema 把 `causal`、`return_lse` 都写成 `int` 而非 `bool`，为什么？

**参考答案**：torch 自定义算子的 schema 对标量类型有严格约束，`bool` 在跨算子边界（尤其 FakeTensor 追踪与 Inductor 后端）支持不如 `int` 稳；用 `int` 编码（0/1）最稳妥，进入实现后再 `bool(...)` 还原。

**练习 2**：如果有人误给 `_fwd_cute` 加了 `register_autograd` 绑定到 CuTeDSL 反向，会出现什么问题？

**参考答案**：当用户传 `forward_backend='cutedsl', backward_backend='triton'` 时，autograd 引擎会按绑定公式调用 CuTeDSL 反向，**静默忽略**用户要的 triton 反向，结果与预期不符；若在 `torch.compile(fullgraph=True)` 下更会直接破坏图。所以 dense 路径坚持把反向留在 `_FFPAAttnFunc.backward` 里按 meta 分发。

---

### 4.3 `_check_supported_options`：把不支持的功能一次性点名拒绝

#### 4.3.1 概念说明

注意力算子在各大框架里演化出了一长串「扩展选项」：滑动窗口（window_size）、softcap、ALiBi 偏置（alibi_slopes）、score_mod、block_table（PagedAttention）、seqused_k（变长 mask）等等。CuTeDSL 后端**只实现了最核心的 dense/varlen 注意力 + 可选因果掩码**，其余统统没写 kernel。

面对一堆没实现的功能，有两种工程选择：

- **静默回退**：默默改用 Triton 或 SDPA。坏处是用户以为自己在用 CuTeDSL，实际跑的是别的后端，性能/行为都不符预期，还极难排查。
- **显式拒绝**：在入口把所有非默认选项一次性收集，抛一个清晰的 `NotImplementedError`，并告诉用户该改用哪个后端。

FFPA 选择了后者，把这套逻辑收口到一个函数 `_check_supported_options`。它的设计原则叫 **「无静默回退」**——宁可立刻报错，也不悄悄换路径。这与 Triton 后端「D≤256 就静默回退 SDPA」的策略相反，因为 CuTeDSL 是用户**主动**通过 `forward_backend='cutedsl'` 选的高速路径，用户明确知道自己要什么，不该被偷偷换掉。

#### 4.3.2 核心流程

```
调用方（dense 的 normalize 阶段 / varlen 的 _ffpa_attn_varlen_cute 入口）
        │
        ▼
_check_supported_options(source=..., dropout_p=..., window_size=..., softcap=...,
                         attention_mask=..., score_mod=..., block_table=..., ...)
        │
        ├─ 逐项检查每个「扩展选项」是否为默认值
        ├─ 把所有非默认的选项名收集进 unsupported 列表
        └─ 若列表非空：
             raise NotImplementedError(
               "<source> only supports dense/varlen attention with optional "
               "causal masking; unsupported options: window_size, softcap, ... "
               "Use forward_backend='triton' when these options are required.")
```

要点：① 它**先收集所有违规项再一次性报错**，而不是遇到第一个就抛——这样用户改一次就能看到全部问题；② 错误信息里嵌入了 `source`（如 `"ffpa_attn_varlen_func"`），让用户知道是哪条公共 API 报的；③ 末尾给出可操作建议（改用 triton 后端）。

#### 4.3.3 源码精读

```python
def _check_supported_options(
  *, source, dropout_p=0.0, window_size=None, sink=None, attention_mask=None,
  block_mask=None, softcap=None, score_mod=None, aux_tensors=None,
  seqused_k=None, block_table=None, num_splits=None, alibi_slopes=None,
) -> None:
  unsupported: list[str] = []
  if dropout_p not in (None, 0.0):          unsupported.append("dropout_p")
  if window_size is not None and window_size != (None, None): unsupported.append("window_size")
  if sink is not None:                       unsupported.append("sink")
  if attention_mask is not None:             unsupported.append("attention_mask")
  if block_mask is not None:                 unsupported.append("block_mask")
  if softcap not in (None, 0.0):             unsupported.append("softcap")
  if score_mod is not None:                  unsupported.append("score_mod")
  if aux_tensors is not None:                unsupported.append("aux_tensors")
  if seqused_k is not None:                  unsupported.append("seqused_k")
  if block_table is not None:                unsupported.append("block_table")
  if num_splits is not None:                 unsupported.append("num_splits")
  if alibi_slopes is not None:               unsupported.append("alibi_slopes")
  if unsupported:
    raise NotImplementedError(
      f"{source} only supports dense/varlen attention with optional causal masking; "
      f"unsupported options: {', '.join(unsupported)}. "
      f"Use forward_backend='triton' when these options are required."
    )
```

—— [_check_supported_options，cute/__init__.py:65-125](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/__init__.py#L65-L125)。逐条对照：`dropout_p` 只接受 0.0（CuTeDSL 无 dropout）；`window_size` 只接受 `None` 或 `(None, None)`（无滑动窗口）；`attention_mask`/`block_mask` 只接受 `None`（无自定义掩码，dense 路径在 functional.py 里另有更早的校验，见下）；`softcap` 只接受 0.0；`score_mod`/`aux_tensors`（FlexAttention 风格）只接受 `None`；`seqused_k`/`block_table`/`num_splits`/`alibi_slopes`（FlashAttention 变长/PagedAttention 扩展）只接受 `None`。

值得注意的是 **dense 路径的 attn_mask/dropout 校验更早发生**，不在本函数里：

```python
if dropout_p > 0.0 and query.size(-1) > 256 and isinstance(self.forward_meta, CuTeDSLBackend):
  raise NotImplementedError("ffpa_attn_func: large-D dropout is not supported by forward_backend='cutedsl'")
if attn_mask is not None and isinstance(self.forward_meta, CuTeDSLBackend):
  raise NotImplementedError("ffpa_attn_func: attn_mask is not supported by forward_backend='cutedsl'. "
                            "Use forward_backend='triton' when attn_mask is required.")
```

—— [normalize_inputs 里的 cutedsl 专属拒绝，functional.py:554-564](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L554-L564)。这是因为 dense 走 `_FFPAAttnFunc` 分发，attn_mask/dropout 在进入 shim 前的 `normalize_inputs` 阶段就要拦下；而 varlen 不走 `_FFPAAttnFunc`，所以它的选项拒绝统一收在 `_ffpa_attn_varlen_cute` 里调本函数。两条路径各自在最合适的入口拦截，但**原则一致**：只报错、不回退，并指向 `forward_backend='triton'`。

#### 4.3.4 代码实践

**实践目标**：枚举 varlen 路径会拒绝的所有非默认 kwarg，并验证报错信息。

**操作步骤（源码阅读型为主）**：

1. 读 [_ffpa_attn_varlen_cute 中对 _check_supported_options 的调用，cute/__init__.py:427-441](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/__init__.py#L427-L441)，确认它从 `kwargs` 里取出了哪些键传给本函数。
2. 对照 [ffpa_attn_interface.py:232-249](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/ffpa_attn_interface.py#L232-L249) 的 docstring `:raises NotImplementedError:` 段落，列出的就是会被拒绝的 kwarg 全集。

**需要观察的现象**：被拒绝的 kwarg 共有 11 个：`dropout_p`、`window_size`、`sink`、`attention_mask`/`attn_mask`、`block_mask`、`softcap`、`score_mod`、`aux_tensors`、`seqused_k`、`block_table`、`num_splits`、`alibi_slopes`。

**预期结果**：任取其中一个非默认值传入 `ffpa_attn_varlen_func`，应得到形如 `ffpa_attn_varlen_func only supports dense/varlen attention with optional causal masking; unsupported options: <名字>. Use forward_backend='triton' when these options are required.` 的 `NotImplementedError`。下面是一段触发示例（**待本地验证**，需 CuTeDSL 环境）：

```python
# 示例代码：触发 _check_supported_options 拒绝
import torch, ffpa_attn
from ffpa_attn import ffpa_attn_varlen_func
q = torch.randn(1024, 32, 512, device="cuda", dtype=torch.bfloat16)
k = torch.randn(1024, 32, 512, device="cuda", dtype=torch.bfloat16)
v = torch.randn_like(k)
cu = torch.tensor([0, 1024], dtype=torch.int32, device="cuda")
try:
    ffpa_attn_varlen_func(q, k, v, cu, cu, 1024, 1024, softcap=50.0)
except NotImplementedError as e:
    print(e)   # 预期含 "unsupported options: softcap"
```

#### 4.3.5 小练习与答案

**练习 1**：`window_size` 的判断写成 `window_size is not None and window_size != (None, None)`，为什么不直接 `window_size is not None`？

**参考答案**：因为调用方可能传 `(None, None)` 表示「左右窗口都无限」（即不启用滑动窗口），这在语义上等价于默认值，应被放行；只有真正给出了有限窗口（如 `(128, 128)`）才该被拒绝。多加一个 `!= (None, None)` 就是为了把这种「显式默认」也当作默认处理。

**练习 2**：为什么 dense 路径不直接复用 `_check_supported_options` 来拦 attn_mask，而要在 `normalize_inputs` 里另写一段？

**参考答案**：dense 路径走 `_FFPAAttnFunc`，其上游 `FFPAAttnMeta.normalize_inputs` 已经在统一处理 attn_mask 的归一化与互斥校验（attn_mask 与 is_causal 不能同设等，见 u2-l3），在那里顺带针对 `CuTeDSLBackend` 做专属拒绝最自然，避免把一个未归一化的原始 mask 传到 shim；而 varlen 不经过 `_FFPAAttnFunc`，没有这套归一化，所以用 `_check_supported_options` 在 varlen 入口集中拒绝。两条路径各取所需，但「只报错不回退」的原则一致。

---

### 4.4 varlen 接入：packed THD + `custom_op` 自管 autograd

#### 4.4.1 概念说明

变长注意力入口 `ffpa_attn_varlen_func` 与 dense 有一个根本不同：**它只支持 CuTeDSL 后端，且只有唯一的反向公式**。这带来两个后果：

1. **不需要布局转置**。varlen 输入是 packed `[T, H, D]`，本来就是 FlashAttention 原生布局（序列维在最前），kernel 直接吃，省掉了 dense 那套 BHND↔NHD 转换。
2. **自己管 autograd**。既然前向只有 CuTeDSL 一条路、反向也只有 CuTeDSL 一个公式，就不需要 dense 那种「按运行时 meta 在多个反向间分发」的机制。于是 varlen 用 `custom_op` 声明前向算子，再用 `register_autograd` 把它**直接绑定**到自己的反向函数，整条 varlen 链路绕开 `_FFPAAttnFunc`，自成一体。

这正好和 dense 形成对偶：

| | dense（`_fwd_cute`） | varlen（`_varlen_fwd_cute`） |
| --- | --- | --- |
| 注册方式 | `define` + `impl` + `register_fake` | `custom_op` + `register_fake` |
| 是否绑反向 | **否**（一前向多反向） | **是**（唯一反向） |
| autograd 边界 | `_FFPAAttnFunc`（共用） | 自己独有（`_varlen_fwd_setup_context` + `_varlen_fwd_backward`） |
| 布局转换 | 要（BHND↔NHD） | 不要（已是 `[T,H,D]`） |
| 图断 | `_ffpa_apply` 带 `dynamo.disable` | `_ffpa_varlen_apply` 带 `dynamo.disable` |

为什么两条路径都要在 `apply` 外面套一层 `torch._dynamo.disable`？因为 `torch.autograd.Function.apply` 在 `torch.compile` 下会被 Dynamo 内联，生成一个「零梯度模板反向」覆盖真实反向；用 `dynamo.disable` 主动图断，让 autograd 边界原封不动地保留（详见 u3-l5）。

#### 4.4.2 核心流程

varlen 的完整调用链：

```
ffpa_attn_varlen_func(q,k,v,cu_q,cu_k,...)        ← 公共 API（interface）
   └─ FFPAAttnVarlenFunc.apply(...)
        └─ _ffpa_varlen_apply(...)                ← @torch._dynamo.disable 图断
             └─ _ffpa_attn_varlen_cute(...)       ← 选项校验 + 形状/dtype/cu_seqlens 校验
                  └─ _ffpa_attn_varlen_impl(...)  ← 归一化输入（编码可选 int、推断 pack_gqa）
                       └─ _varlen_fwd_custom(...) ← @custom_op("ffpa_attn::_varlen_fwd_cute")
                            ├─ _trim_trailing_empty_varlen_segments  去掉尾部空段
                            └─ _forward_impl_for_device(...)(...)     → SM90/SM80 kernel

反向（autograd 引擎自动触发，因 register_autograd 绑定）：
   _varlen_fwd_backward(ctx, dout, dlse)
      └─ torch.ops.ffpa_attn._varlen_bwd_cute(...)   ← 另一个 custom_op
```

varlen 算子的 schema 有一个难点：`window_size_left`/`window_size_right` 是「可选 int」（`Optional[int]`），但 torch 算子 schema 要的是确定的 `int`。FFPA 的解法是用一个**哨兵整数** `_VARLEN_CUSTOM_OP_NONE_INT = -(2**31)`（int32 的最小值）来编码 `None`，过算子边界时 `_encode_optional_int_for_custom_op` 把 `None` 转成这个哨兵值，对端再 `_decode_optional_int_from_custom_op` 还原。这样既满足 schema 的类型约束，又能表达「未设置」。

#### 4.4.3 源码精读

先看 varlen 入口 shim 的校验部分：

```python
def _ffpa_attn_varlen_cute(q, k, v, cu_seqlens_q, cu_seqlens_k,
                           max_seqlen_q, max_seqlen_k, *,
                           dropout_p, softmax_scale, causal, enable_gqa,
                           return_lse, kwargs):
  _check_supported_options(source="ffpa_attn_varlen_func",
                           dropout_p=dropout_p,
                           window_size=kwargs.get("window_size"),
                           attention_mask=kwargs.get("attention_mask", kwargs.get("attn_mask")),
                           softcap=kwargs.get("softcap"), ...)   # 见 4.3
  if q.dim() != 3 or k.dim() != 3 or v.dim() != 3:
    raise ValueError("q/k/v must be 3-D packed [T, H, D]")
  if k.shape != v.shape:
    raise ValueError("k/v must share shape")
  if q.dtype not in (torch.float16, torch.bfloat16):
    raise TypeError("q/k/v must be fp16/bf16")
  ...
  requires_grad = any(t.requires_grad for t in (q, k, v))
  max_head_dim = cute_max_supported_head_dim(q.device)
  if not (MIN_SUPPORTED_HEAD_DIM <= q.size(-1) <= max_head_dim):
    raise NotImplementedError(...)
  _require_cute_supported(q, k, v, requires_grad=requires_grad)   # 张量级校验，与 dense 共用
  return _ffpa_attn_varlen_impl(q, k, v, cu_seqlens_q=cu_seqlens_q, ...)
```

—— [_ffpa_attn_varlen_cute，cute/__init__.py:402-507](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/__init__.py#L402-L507)。注意它复用了 dense 的 `_require_cute_supported` 做张量级校验（设备/架构/head_dim/dtype），保证两条路径的硬件门禁完全一致；额外做了 varlen 专属的校验：3 维 packed、k/v 同形、cu_seqlens 是 int32 且长度 ≥2、enable_gqa 与头数关系等。

再看输入归一化与可选 int 编码：

```python
def _normalize_varlen_custom_op_inputs(q, k, cu_seqlens_q, cu_seqlens_k,
                                       max_seqlen_q, max_seqlen_k, softmax_scale,
                                       window_size, pack_gqa, score_mod, aux_tensors):
  ...
  if softmax_scale is None:
    softmax_scale = 1.0 / math.sqrt(q.shape[-1])          # 默认 1/√D
  if pack_gqa is None:
    pack_gqa = q.shape[-2] > k.shape[-2]                  # 自动推断：query 头多于 kv 头则启用
  return (cu_seqlens_q, cu_seqlens_k, max_seqlen_q, max_seqlen_k,
          float(softmax_scale),
          _encode_optional_int_for_custom_op(window_size[0]),   # None → -(2**31)
          _encode_optional_int_for_custom_op(window_size[1]),
          bool(pack_gqa))
```

—— [_normalize_varlen_custom_op_inputs，cute/__init__.py:947-999](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/__init__.py#L947-L999)。`pack_gqa` 的自动推断很关键：当 query 头数 > kv 头数（GQA/MQA）时自动启用 GQA 打包（见 4.4.3 末尾的 pack_gqa.py）。

接着是前向 `custom_op` 本体：

```python
@torch.library.custom_op("ffpa_attn::_varlen_fwd_cute", mutates_args=())
def _varlen_fwd_custom(q, k, v, cu_seqlens_q, cu_seqlens_k, max_seqlen_q, max_seqlen_k,
                       softmax_scale, causal, window_size_left, window_size_right,
                       softcap, pack_gqa) -> tuple[torch.Tensor, torch.Tensor]:
  cu_seqlens_q, cu_seqlens_k = _trim_trailing_empty_varlen_segments(cu_seqlens_q, cu_seqlens_k)
  window_size_left_opt, window_size_right_opt = _decode_custom_op_window(window_size_left, window_size_right)
  return _forward_impl_for_device(q.device, q.size(-1), v.size(-1))(
    q, k, v, cu_seqlens_q=cu_seqlens_q, cu_seqlens_k=cu_seqlens_k,
    max_seqlen_q=max_seqlen_q, max_seqlen_k=max_seqlen_k,
    softmax_scale=softmax_scale, causal=causal,
    window_size_left=window_size_left_opt, window_size_right=window_size_right_opt,
    softcap=softcap, pack_gqa=pack_gqa, return_lse=True,
  )
```

—— [_varlen_fwd_custom，cute/__init__.py:708-745](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/__init__.py#L708-L745)。`custom_op` 比 `define`+`impl` 更简洁——它用 Python 类型注解直接当 schema，并要求声明 `mutates_args=()`（本算子不改输入）。`_trim_trailing_empty_varlen_segments` 是一个小优化：若 batch 末尾有长度为 0 的空段，把它们裁掉，避免 kernel 跑空 tile。

然后是 autograd 绑定（varlen 路径的「独家动作」）：

```python
def _varlen_fwd_setup_context(ctx, inputs, output) -> None:
  q, k, v, cu_seqlens_q, cu_seqlens_k = inputs[:5]
  max_seqlen_q, max_seqlen_k, softmax_scale, causal = inputs[5:9]
  window_size_left, window_size_right, softcap = inputs[9:12]
  out, lse = output
  ctx.save_for_backward(q, k, v, out, lse, cu_seqlens_q, cu_seqlens_k)
  ctx.max_seqlen_q = max_seqlen_q; ctx.max_seqlen_k = max_seqlen_k
  ctx.softmax_scale = softmax_scale; ctx.causal = causal
  ctx.window_size_left = window_size_left; ctx.window_size_right = window_size_right
  ctx.softcap = softcap
  ctx.set_materialize_grads(False)

def _varlen_fwd_backward(ctx, dout, dlse):
  q, k, v, out, lse, cu_seqlens_q, cu_seqlens_k = ctx.saved_tensors
  if dout is None:
    dout = torch.zeros_like(out)
  dq, dk, dv = torch.ops.ffpa_attn._varlen_bwd_cute(
    q, k, v, out, dout, lse, cu_seqlens_q, cu_seqlens_k,
    ctx.max_seqlen_q, ctx.max_seqlen_k, ctx.softmax_scale, ctx.causal,
    ctx.window_size_left, ctx.window_size_right, ctx.softcap, dlse,
  )
  return dq, dk, dv, *((None,) * 10)   # 其余输入不求梯度

torch.library.register_autograd("ffpa_attn::_varlen_fwd_cute",
                                _varlen_fwd_backward,
                                setup_context=_varlen_fwd_setup_context)
```

—— [varlen autograd 绑定，cute/__init__.py:899-944](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/__init__.py#L899-L944)。这是 varlen 与 dense 最大的不同：`setup_context` 在前向结束后保存反向要用的张量（q/k/v/out/lse/cu_seqlens），`_varlen_fwd_backward` 反向时调另一个 custom_op `_varlen_bwd_cute`。`ctx.set_materialize_grads(False)` 表示「不要自动给 None 梯度造零张量」，由 backward 自己处理。`return` 里 `(None,) * 10` 是因为 schema 有 12 个输入，只有前 3 个（q/k/v）需要梯度，其余返回 None。

**两个 varlen 内部辅助件**（了解即可，它们服务于 kernel 内部，不在调用链主线上）：

第一，`seqlen_info.py` 把「序列长度信息」打包进一个不可变对象，让 kernel 每个 tile 只读一次 batch 边界：

```python
@dataclass(frozen=True)
class SeqlenInfoQK:
  offset_q: Int32; offset_k: Int32
  padded_offset_q: Int32; padded_offset_k: Int32
  seqlen_q: Int32; seqlen_k: Int32
  has_cu_seqlens_q: Constexpr[bool]; has_cu_seqlens_k: Constexpr[bool]

  @staticmethod
  def create(batch_idx, seqlen_q_static, seqlen_k_static, mCuSeqlensQ=None, mCuSeqlensK=None, ...):
    offset_q = 0 if mCuSeqlensQ is None else mCuSeqlensQ[batch_idx]
    ...
    seqlen_q = seqlen_q_static if mCuSeqlensQ is None else mCuSeqlensQ[batch_idx + 1] - offset_q
    ...
```

—— [SeqlenInfoQK，cute/utils/seqlen_info.py:79-127](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/utils/seqlen_info.py#L79-L127)。它的 `offset_batch_Q`/`offset_batch_K` 方法在 kernel 内被用来定位「当前 batch 在 packed 张量里的起始偏移」，既支持 dense（按 batch 维索引）也支持 ragged（按 cu_seqlens 偏移）。`padded_offset_*` 用 `cute.assume(..., divby=tile)` 告诉编译器对齐信息，便于 TMA 寻址优化。

第二，`pack_gqa.py` 处理 GQA/MQA 的访存局部性——把多个共享同一组 KV 的 query 头「折叠」进序列维：

```python
def pack_gqa_layout(T, qhead_per_kvhead, nheads_kv, head_idx):
  # For Q/O tensors (head_idx=2):
  # (seqlen_q, headdim, nheads, batch) -> ((qhead_per_kvhead, seqlen_q), headdim, nheads_kv, batch)
  head_stride = T.stride[head_idx]
  shape_packed = ((qhead_per_kvhead, T.shape[0]), nheads_kv, ...)
  stride_packed = ((head_stride, T.stride[0]), head_stride * qhead_per_kvhead, ...)
  return cute.make_tensor(T.iterator, cute.make_layout(shape_packed, stride=stride_packed))
```

—— [pack_gqa_layout，cute/utils/pack_gqa.py:15-42](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/utils/pack_gqa.py#L15-L42)。它的妙处是**只改 layout（shape+stride），不搬数据**：把「同一 KV 头对应的 `qhead_per_kvhead` 个 query 头」在逻辑上排成连续行，使 kernel 一次加载就能喂给多组 query，省去对 K/V 的重复访存。这正是 4.4.3 里 `pack_gqa=True`（自动推断）后 kernel 内部要用的布局。

#### 4.4.4 代码实践

**实践目标**：追踪 varlen 前向调用链，并对比 dense 与 varlen 在 autograd 边界上的差异。

**操作步骤（源码阅读型）**：

1. 从 [ffpa_attn_interface.py:257](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/ffpa_attn_interface.py#L257) 的 `FFPAAttnVarlenFunc.apply` 开始。
2. 看 [FFPAAttnVarlenFunc / _ffpa_varlen_apply，functional.py:1023-1033](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L1023-L1033) 与 [:990-1020](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L990-L1020)：`_ffpa_varlen_apply` 带 `@torch._dynamo.disable`，把 `**kwargs` 原样透传给 `_ffpa_attn_varlen_cute`。
3. 顺着 4.4.3 的链路一路追到 `_varlen_fwd_custom`。
4. 最后定位 [register_autograd，cute/__init__.py:940-944](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/__init__.py#L940-L944)，确认 varlen 确实绑定了唯一反向。

**需要观察的现象**：varlen 链路里**没有任何 `_bhnd_to_bnhd` 调用**（因为 packed `[T,H,D]` 本就是 FA 布局）；反向是被 `register_autograd` 自动触发的（而非像 dense 那样由 `_FFPAAttnFunc.backward` 主动调）。

**预期结果**：你能填出下表的对比（答案已在 4.4.1 的对偶表）。若本地有 SM8x/SM90 + CuTeDSL 环境，可用下面脚本验证 varlen 正反向（**待本地验证**）：

```python
# 示例代码：varlen 前向 + 反向
import torch, ffpa_attn
from ffpa_attn import ffpa_attn_varlen_func
T, H, D = 1024, 32, 512
q = torch.randn(T, H, D, device="cuda", dtype=torch.bfloat16, requires_grad=True)
k = torch.randn(T, H, D, device="cuda", dtype=torch.bfloat16, requires_grad=True)
v = torch.randn(T, H, D, device="cuda", dtype=torch.bfloat16, requires_grad=True)
# 两条序列，长度 512 + 512，cu_seqlens 长度 B+1=3
cu = torch.tensor([0, 512, 1024], dtype=torch.int32, device="cuda")
out = ffpa_attn_varlen_func(q, k, v, cu, cu, 512, 512)
out.backward(torch.ones_like(out))
print(out.shape, q.grad.shape)   # 预期均为 [1024, 32, 512]
```

#### 4.4.5 小练习与答案

**练习 1**：为什么 varlen 用 `register_autograd` 绑定反向，而 dense 不行？

**参考答案**：`register_autograd` 只能把一个前向绑定到**唯一**的反向函数。varlen 只有 CuTeDSL 一个后端、一条反向公式，绑死没问题；而 dense 的同一个 CuTeDSL 前向，反向可被用户配置成 cutedsl/triton/sdpa 三者之一（一前向多反向），绑死会静默覆盖用户选择，所以 dense 把反向留在 `_FFPAAttnFunc.backward` 里按运行时 `meta.backward_meta` 分发。

**练习 2**：`_varlen_fwd_backward` 末尾返回 `dq, dk, dv, *((None,) * 10)`，为什么有 10 个 `None`？

**参考答案**：`_varlen_fwd_cute` 的 schema 有 12 个输入（q,k,v,cu_seqlens_q,cu_seqlens_k,max_seqlen_q,max_seqlen_k,softmax_scale,causal,window_size_left,window_size_right,softcap），autograd 要求 backward 返回值与输入一一对应。只有前 3 个（q/k/v）是可训练张量需要梯度，其余 9 个是非张量或不需要梯度，故返回 `None`；加上 dlse 对应的占位共补齐到与输入数匹配的 `None` 个数，这里 `(None,)*10` 是把「dq,dk,dv 之后的全部输入」统一标记为不求梯度。

**练习 3**：varlen 路径为什么不需要 `_bhnd_to_bnhd`？

**参考答案**：varlen 的输入是 packed `[T, H, D]`，序列维 T 已在最前、头维 H 在中间，这本来就是 FlashAttention/CuTeDSL kernel 的原生布局，无需任何转置；只有 dense 的 SDPA 布局 `[B, H, N, D]`（头在序列前）才需要与 kernel 原生布局互换。

---

## 5. 综合实践

把本讲四个模块串起来，完成下面这个「 CuTeDSL 入口层全景图」小任务：

**任务**：阅读 [src/ffpa_attn/cute/__init__.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/__init__.py)，自己画一张「dense 与 varlen 两条路径并排」的对照图，要求标注：

1. **公共入口**：dense 是 `ffpa_attn_func` → `FFPAAttnFunc.apply` → `_ffpa_apply`（图断）→ `_FFPAAttnFunc.forward`；varlen 是 `ffpa_attn_varlen_func` → `FFPAAttnVarlenFunc.apply` → `_ffpa_varlen_apply`（图断）→ `_ffpa_attn_varlen_cute`。
2. **shim**：dense 有 `_ffpa_attn_forward_cute`/`_ffpa_attn_backward_cute` 做布局转换；varlen 的 `_ffpa_attn_varlen_cute` 只做校验、不转置。
3. **注册算子**：dense 是 `_fwd_cute`/`_bwd_cute`（define+impl+register_fake，不绑反向）；varlen 是 `_varlen_fwd_cute`/`_varlen_bwd_cute`（custom_op + register_autograd，绑反向）。
4. **校验关卡**：两条路径共用 `_require_cute_supported`（张量级）与 `_check_supported_options`（功能选项级，varlen 专属调用点）；dense 还多了 `normalize_inputs` 里的 attn_mask/dropout 拒绝。
5. **SM 分发**：两条路径都经 `_forward_impl_for_device`/`_backward_impl_for_device` → `_use_sm90_specialized` 选 SM90 专用或 SM80 通用。

**进阶（可选）**：在图上额外标出 `_encode_optional_int_for_custom_op`（哨兵整数编码）发生在 varlen 哪一层，以及 `pack_gqa`/`SeqlenInfoQK` 是在 kernel 内部（算子实现里）被使用的，而非在 shim 层。

完成后，你应当能用一句话向别人解释：「CuTeDSL 后端的入口层 = 布局转换（仅 dense）+ 三道校验关卡 + torch 算子注册（dense 不绑反向、varlen 绑反向）+ 共用的 SM 路径分发」。

## 6. 本讲小结

- **布局转换是 dense 专属**：`_bhnd_to_bnhd`/`_bnhd_to_bhnd` 把 SDPA 的 `[B,H,N,D]` 与 kernel 原生的 `[B,N,H,D]` 互转，集中在 `_ffpa_attn_forward_cute`/`_ffpa_attn_backward_cute` 两个 shim 里；varlen 的 packed `[T,H,D]` 本就是原生布局，无需转置。
- **dense 用三件套但不绑反向**：`_fwd_cute`/`_bwd_cute` 走 `define`+`impl`+`register_fake`，反向留在 `_FFPAAttnFunc.backward` 按运行时 meta 分发，以支持「一前向多反向」。
- **varlen 用 custom_op 并自管 autograd**：`_varlen_fwd_cute` 经 `register_autograd` 绑定唯一反向，整条链路绕开 `_FFPAAttnFunc`；可选 int 用哨兵 `-(2**31)` 编码以过 schema 边界。
- **无静默回退原则**：`_check_supported_options` 把 dropout/window/softcap/mask/score_mod 等 11 类扩展选项一次性点名拒绝并指向 `forward_backend='triton'`，区别于 Triton 的静默回退 SDPA。
- **三道校验关卡分层**：张量级 `_require_cute_supported`（dense/varlen 共用）、功能选项级 `_check_supported_options`、dense 额外的 `normalize_inputs` 内 attn_mask/dropout 拒绝。
- **SM 分发共用**：`_forward_impl_for_device`/`_backward_impl_for_device` 复用 u6-l1 的 `_use_sm90_specialized` 谓词，dense 与 varlen 走同一套 SM90 专用 / SM80 通用选择逻辑。

## 7. 下一步学习建议

本讲把「入口胶水层」讲透了，但还没碰 kernel 内部。建议接下来：

- 读 **[u6-l3 Tile scheduler 与 producer/consumer pipeline](u6-l3-tile-scheduler-pipeline.md)**：进入 CuTeDSL kernel 内部，看 `SingleTileScheduler` 如何做 tile 映射与 L2 swizzle，`PipelineTmaAsync` 如何用 TMA + barrier 让 g2s 与 MMA 重叠。本讲提到的 `SeqlenInfoQK`、`pack_gqa_layout` 正是 scheduler 与 pipeline 在 varlen/GQA 场景下要消费的数据。
- 读 **[u6-l4 CuTeDSL SM90 专用 kernel：d384 / d512 与 generic](u6-l4-cutedsl-sm90-specialized-kernels.md)**：看本讲里 `_forward_impl_for_device` 选中的 `_ffpa_attn_forward_sm90` 内部到底特化了什么，理解为什么 d384/d512 要单独写、generic 如何兜底。
- 若想横向对照，可重读 [u3-l5 torch.compile 兼容与 torch.library 自定义算子](u3-l5-torch-compile-custom-ops.md)，把 Triton 的 `_fwd_*` 三件套与本讲的 CuTeDSL `_fwd_cute`/`_varlen_fwd_cute` 做一次逐行对比，体会「为什么 dense 不绑反向、varlen 绑反向」是跨后端通用的设计取舍。
