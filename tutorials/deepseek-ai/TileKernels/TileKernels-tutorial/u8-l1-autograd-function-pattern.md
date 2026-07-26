# torch.autograd.Function 封装范式（以 EngramGate 为例）

## 1. 本讲目标

学完本讲，你应当能够：

- 说清为什么底层 TileLang kernel 需要被 `torch.autograd.Function` 封装，才能参与 PyTorch 的 `loss.backward()`。
- 掌握 `forward` / `backward` 这对静态方法的核心契约：**返回值必须与 `forward` 的输入（除 `ctx` 外）逐位一一对应**。
- 看懂 `EngramGateFn` 里 `save_for_backward`（存张量）与 `ctx.xxx = ...`（存非张量）的分工，以及反向时如何「重放」前向中间量。
- 理解「参数带 `main_grad` 时就地累加、并返回 `None`」这一分布式训练里常见的梯度优化。
- 仿照这个范式，独立为 `transpose` 写一个可自动求导的封装。

本讲只聚焦「封装范式」这一层，不重复讲 engram 的门控数学（那是 u6-l1/u6-l2 的内容）。

## 2. 前置知识

在进入源码前，先用三段话建立必要的心智模型。

**为什么 autograd 默认不会求导底层 kernel。** TileKernels 的算子（如 `engram_gate_fwd`）最终会被 TileLang 编译成一段 CUDA 代码（见 u2-l1）。对 PyTorch 的 autograd 而言，这段代码是一个「黑盒」——它只看到张量进去、张量出来，并不知道输出对输入的导数是什么。因此，如果不显式告诉 autograd「反向该怎么传梯度」，一旦把这样的算子接进 `loss.backward()`，链路上的梯度就会在它这里断掉（输入拿不到 `.grad`）。

**`torch.autograd.Function` 是什么。** 它是 PyTorch 提供的「自定义可微算子」注册口。你继承它、写两个静态方法 `forward` 和 `backward`，再用 `MyFn.apply(...)` 调用，autograd 就会把你的 `backward` 接进自动微分图。`forward` 负责「怎么算输出 + 存下反向要用的东西」，`backward` 负责「拿到输出侧梯度、算出每个输入侧梯度」。

**转置的导数仍是转置（综合实践会用到）。** 对矩阵转置 \( Y = X^{\top} \)，其雅可比满足：给定输出侧梯度 \( \bar{Y} \)（与 \( Y \) 同形），输入侧梯度就是 \( \bar{X} = \bar{Y}^{\top} \)。即「转置的导数还是转置」。这条性质让本讲最后的综合实践非常干净——反向直接再调一次 `transpose` 即可。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [tile_kernels/modeling/engram/engram_gate.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/engram/engram_gate.py) | **本讲主角**。用 `torch.autograd.Function` 把底层 engram kernel 封装成可求导的 `engram_gate`。 |
| [tile_kernels/engram/engram_gate_kernel.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/engram/engram_gate_kernel.py) | 被 `forward`/`backward` 调用的底层 kernel wrapper：`engram_gate_fwd`、`engram_gate_bwd`。 |
| [tile_kernels/engram/engram_grad_w_reduce_kernel.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/engram/engram_grad_w_reduce_kernel.py) | `grad_w_reduce`：把分块参数梯度就地累加进 fp32 缓冲。 |
| [tile_kernels/modeling/mhc/ops/sinkhorn.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/ops/sinkhorn.py) | 同套范式的「极简对照组」：`_SinkhornNormalize`，便于对比封装的共性。 |
| [tile_kernels/transpose/batched_transpose_kernel.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/transpose/batched_transpose_kernel.py) | 综合实践的依赖：`transpose` / `batched_transpose`。 |

记忆口诀：**modeling 层只做「可微封装」，不写算子逻辑**；算子逻辑永远在 `tile_kernels/<家族>/` 的 kernel 文件里（见 u1-l3 的四层结构）。

## 4. 核心概念与源码讲解

本讲把 `engram_gate.py` 这一个最小模块拆成三块来讲：**4.1 契约（forward/backward 一一对应）**、**4.2 forward（形状归一化 + 存盘）**、**4.3 backward（重放 + main_grad 就地累加）**。

### 4.1 autograd.Function 的契约：forward/backward 一一对应

#### 4.1.1 概念说明

`torch.autograd.Function` 的全部约束可以浓缩成一句话：

> `backward` 必须返回与 `forward` 输入（**除 `ctx` 外**）数量相等、顺序一致的梯度元组；对某个输入不可微时，对应位置返回 `None`。

设 `forward(ctx, a, b, c)` 有 3 个输入，则 `backward(ctx, grad_output)` 必须返回：

\[
(\partial L/\partial a,\ \partial L/\partial b,\ \partial L/\partial c)
\]

注意：**非张量输入（如 Python `float`）也要占一个返回槽，填 `None`**。这是初学者最常踩的坑——少返回一个，PyTorch 会直接报形状不匹配。

调用方式不是 `MyFn.forward(...)`，而是 `MyFn.apply(...)`；`apply` 会帮你创建 `ctx` 并接好 autograd 图。

#### 4.1.2 核心流程

```text
用户代码:   out = engram_gate(x, k, v, wh, we, clamp_value, eps)
            ──等价于──>  EngramGateFn.apply(x, k, v, wh, we, clamp_value, eps)

forward:    apply 内部调用 forward(ctx, 7 个输入)
            → 计算输出、存盘 → 返回 output

反向传播:    autograd 调用 backward(ctx, grad_output)
            → 计算每个输入的梯度 → 返回 7 元组（与 7 个输入逐位对应）
```

#### 4.1.3 源码精读

类定义与文档说明：[tile_kernels/modeling/engram/engram_gate.py:6-33](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/engram/engram_gate.py#L6-L33) —— 它继承 `torch.autograd.Function`，文档里写明了前向数学与 `main_grad` 约定。

`forward` 接收 7 个输入（`ctx` 之后）：

[tile_kernels/modeling/engram/engram_gate.py:36](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/engram/engram_gate.py#L36) 声明了 `hidden_states, k, v, weight_hidden, weight_embed, clamp_value, eps`。注意后两个是 `float`，不是张量。

`backward` 返回恰好 7 个值，与上面 7 个输入逐位对应：

[tile_kernels/modeling/engram/engram_gate.py:85-92](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/engram/engram_gate.py#L85-L92) 末尾返回元组，对照表如下：

| 返回位置 | 对应 forward 输入 | 返回内容 |
| --- | --- | --- |
| 1 | `hidden_states` | `grad_x.view(origin_shape)` |
| 2 | `k` | `grad_k.view(origin_shape)` |
| 3 | `v` | `grad_v.view(v_origin_shape)` |
| 4 | `weight_hidden` | `None`（有 main_grad 时）或 `grad_wh` |
| 5 | `weight_embed` | `None`（有 main_grad 时）或 `grad_we` |
| 6 | `clamp_value` | `None`（float 不可微） |
| 7 | `eps` | `None`（float 不可微） |

最后两行 `None, None` 正是给 `clamp_value`、`eps` 这两个非张量输入占的槽。`apply` 入口别名见 [tile_kernels/modeling/engram/engram_gate.py:95](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/engram/engram_gate.py#L95)：`engram_gate = EngramGateFn.apply`，所以用户直接 `engram_gate(...)` 就行。

#### 4.1.4 代码实践

**实践目标**：用一个最小、**纯 CPU 可跑**的 `autograd.Function`，亲手验证「返回数必须等于输入数」这条契约。

**操作步骤**（把下面这段「示例代码」存成 `toy.py` 用 `python toy.py` 运行，不需要 GPU）：

```python
# 示例代码：不依赖 TileKernels，纯 PyTorch 验证 autograd.Function 契约
import torch

class DoublingFn(torch.autograd.Function):
    @staticmethod
    def forward(ctx, x, scale):          # 2 个输入：张量 x、float scale
        ctx.scale = scale
        return x * 2

    @staticmethod
    def backward(ctx, grad_output):      # 必须返回 2 个值，对应 x、scale
        return grad_output * 2, None     # scale 是 float，返回 None

x = torch.randn(4, requires_grad=True)
y = DoublingFn.apply(x, 3.0)
y.sum().backward()
print("grad of x =", x.grad)             # 预期全为 2.0

# 试错：把 backward 改成只返回一个值（注释里），
#   return grad_output * 2
# 再运行，观察 PyTorch 报的错（返回数与输入数不匹配）。
```

**需要观察的现象**：正常运行时 `x.grad` 全为 `2.0`；当你把 `backward` 改成只返回一个值时，PyTorch 会抛出梯度数量不匹配的错误。

**预期结果**：`grad of x = tensor([2., 2., 2., 2.])`。

#### 4.1.5 小练习与答案

**练习 1**：`forward(ctx, a, b, c, d)` 有 4 个输入，`backward` 应返回几个值？  
**答案**：4 个，与 `a,b,c,d` 一一对应；不可微的填 `None`。

**练习 2**：为什么 `clamp_value`、`eps` 也要在返回元组里占 `None`？  
**答案**：因为它们是 `forward` 的位置参数（第 6、7 个输入）。autograd 按「位置」把返回梯度对应回输入，跳过它们会让后面的返回值错位。

### 4.2 forward：形状归一化、调用底层 kernel、save_for_backward

#### 4.2.1 概念说明

`forward` 在「封装层」里要完成四件事：

1. **形状归一化**：底层 kernel 通常只接受固定 rank 的张量（如 `(num_tokens, hc_mult, hidden_size)`），但用户传进来的可能是任意前导形状（如 `(batch, seq, hc_mult, hidden_size)`）。`forward` 先 `view` 成 kernel 要的形状，记下原始形状 `origin_shape`，反向时再 `view` 回去。
2. **调用底层 kernel**：把归一化后的张量喂给 `engram_gate_fwd` 等底层 wrapper，拿到输出与前向中间量。
3. **存盘**：把反向要用的张量交给 `ctx.save_for_backward(...)`，把非张量（`clamp_value`、`origin_shape`）直接挂到 `ctx` 上。
4. **view 回原形**：把输出 `view(origin_shape)` 还给用户。

这里有一个关键分工，务必记住：

- **张量** → 用 `ctx.save_for_backward(...)`。它会接入 autograd 的**版本计数器**，能在张量被原地修改时给出安全警告，是官方推荐做法。
- **非张量**（float、`torch.Size` 等）→ 用 `ctx.xxx = ...` 直接存属性。它们不参与版本计数，没有 `save_for_backward` 的必要。

#### 4.2.2 核心流程

```text
hidden_states (任意前导形状)
   │ view(-1, hc_mult, hidden_size)
   ▼
x, k, v  ──►  weight_fused = fused_weight(wh, we)
   │              │
   │              ▼
   │       engram_gate_fwd(x,k,v,weight_fused,eps,clamp_value)
   │              │  返回 (output, dot, gate_score, rstd_x, rstd_k)
   ▼              ▼
ctx.save_for_backward(x,k,v,wh,we,weight_fused,dot,gate_score,rstd_x,rstd_k)
ctx.clamp_value = clamp_value      # 非张量
ctx.origin_shape = origin_shape    # 非张量
   │
   ▼
return output.view(origin_shape)   # 还原成用户给的形状
```

#### 4.2.3 源码精读

**形状归一化**：[tile_kernels/modeling/engram/engram_gate.py:37-42](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/engram/engram_gate.py#L37-L42) 先解出 `hc_mult, hidden_size`，再把 `hidden_states/k/v` 各自 `view` 成 kernel 期望的三维/二维形状。注意它存进 `save_for_backward` 的是 `view` 之后的 `x, k, v`（不是原始 `hidden_states`），反向 kernel 直接拿到它认识的形状。

**预融合权重 + 调底层前向**：[tile_kernels/modeling/engram/engram_gate.py:44-47](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/engram/engram_gate.py#L44-L47) 先算 `weight_fused`（详见 u6-l3），再调 `engram_gate_fwd` 拿到 5 元组 `(output, dot, gate_score, rstd_x, rstd_k)`。底层 wrapper 的签名与「是否存中间量」由 `save_for_backward` 形参控制，见 [tile_kernels/engram/engram_gate_kernel.py:470-494](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/engram/engram_gate_kernel.py#L470-L494)：推理时传 `False` 可省掉 `dot/gate_score/rstd_x/rstd_k` 的分配。

**存盘**：[tile_kernels/modeling/engram/engram_gate.py:49-54](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/engram/engram_gate.py#L49-L54) 把 10 个张量交给 `save_for_backward`，把两个非张量挂到 `ctx`。被存的张量分两类：

- **输入类**：`x, k, v, weight_hidden, weight_embed`（前向输入，反向复用）。
- **中间量类**：`weight_fused`（前向算出的预融合权重）、`dot, gate_score, rstd_x, rstd_k`（前向 kernel 写出的中间量，见 u6-l2 的说明——其中 `dot` 存的是未归一化的原始点积）。

**view 回原形**：[tile_kernels/modeling/engram/engram_gate.py:55](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/engram/engram_gate.py#L55) `return output.view(origin_shape)`，让用户拿到与输入同形状的输出。

#### 4.2.4 代码实践

**实践目标**：把 `forward` 的 7 个输入与 `save_for_backward` 的 10 个张量之间的映射整理清楚，并标注每个被存张量在反向里的用途。

**操作步骤**：

1. 打开 [engram_gate.py 的 forward](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/engram/engram_gate.py#L35-L55)。
2. 对照 [backward 的解包与调用](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/engram/engram_gate.py#L57-L70)，画一张「被存张量 → 反向用途」表。
3. （可选，无需改源码）在解释器里 `import tile_kernels.modeling.engram.engram_gate as m`，用 `inspect.getsource(m.EngramGateFn.forward)` 打印源码，把 `save_for_backward` 的实参顺序抄下来对照。

**需要观察的现象**：`dot/gate_score/rstd_x/rstd_k` 是前向 kernel 顺手写出的中间量，并非 `forward` 的输入或输出；它们能被 `save_for_backward` 存起来，说明 **`save_for_backward` 可以存任意张量，不限于 forward 的入参出参**。

**预期结果**：得到一张 10 行的表，例如 `gate_score` → 反向里作为门控值 `g` 参与门控导数计算、`rstd_x/rstd_k` → 反向 RMSNorm 求导复用、`weight_fused` → 反向点积对权重的梯度复用。

#### 4.2.5 小练习与答案

**练习 1**：`origin_shape` 是 `torch.Size`，为什么用 `ctx.origin_shape = ...` 而不是 `save_for_backward`？  
**答案**：它是非张量（不参与 autograd 版本计数），用 `ctx` 属性更直接；`save_for_backward` 主要服务于需要版本追踪的张量。

**练习 2**：`weight_fused` 是在 `forward` 里现算的（不是入参），它能被 `save_for_backward` 存吗？  
**答案**：能。`save_for_backward` 可以存任意「反向要用到的张量」，无论它是入参、出参还是中间量。

### 4.3 backward：重放中间量、main_grad 就地累加与返回 None

#### 4.3.1 概念说明

`backward` 的职责是把「输出侧梯度 `grad_output`」翻译成「每个输入的梯度」。它分三步：

1. **重放中间量**：从 `ctx.saved_tensors` 取出前向存的张量，连同 `grad_output` 一起喂给底层 `engram_gate_bwd`。这里的「重放」不是重算，而是**复用前向已存的中间量**（`dot/gate_score/rstd_x/rstd_k`），避免反向时再做一遍昂贵的前向计算。
2. **处理参数梯度归约**：底层 bwd kernel 对激活梯度（`grad_x/grad_k/grad_v`）直接给出最终结果，但对**参数梯度**只给出分块的 `grad_w_partial`，需要再调 `grad_w_reduce` 跨持久化块归约（split-K 风格，见 u6-l2）。
3. **main_grad 就地累加**：分布式训练里，参数常带一个 fp32 的 `main_grad` 缓冲，希望梯度直接 `+=` 进去，而不是另建一个临时 `.grad` 再累加。本封装检测到 `weight.main_grad` 存在时，就把它当作归约目标、并在返回元组里对该参数返回 `None`。

「返回 `None`」的含义要准确理解：它不是「没有梯度」，而是「**梯度已经就地累加进 `main_grad`，不要再为这个参数创建 `.grad`**」。如果这里返回了一个新张量，PyTorch 会把它写进 `.grad`，于是同一份梯度被存了两份、且还要额外做一次累加，浪费显存与算力。

#### 4.3.2 核心流程

```text
ctx.saved_tensors  ──►  取出 (x,k,v,wh,we,weight_fused,dot,gate_score,rstd_x,rstd_k)
grad_output.view(...)   ──►  grad_out
                          │
                          ▼
           engram_gate_bwd(grad_out, x,k,v,weight_fused, dot,gate_score,rstd_x,rstd_k, clamp_value)
                          │  返回 (grad_x, grad_k, grad_v, grad_w_partial)
                          ▼
检测 main_grad_wh / main_grad_we 是否存在
  ├─ 有 main_grad：把它当作累加目标 grad_wh / grad_we
  └─ 无 main_grad：torch.zeros_like(..., dtype=fp32) 新建
                          │
                          ▼
           grad_w_reduce(grad_w_partial, wh, we, grad_wh, grad_we)   # 就地 +=
                          │
                          ▼
返回 7 元组：
  ( grad_x.view(origin_shape),
    grad_k.view(origin_shape),
    grad_v.view(v_origin_shape),
    None if 有main_grad_wh else grad_wh,
    None if 有main_grad_we else grad_we,
    None,   # clamp_value
    None )  # eps
```

#### 4.3.3 源码精读

**解包与 view**：[tile_kernels/modeling/engram/engram_gate.py:59-65](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/engram/engram_gate.py#L59-L65) 把 `ctx.saved_tensors` 解包成 10 个变量（与 `save_for_backward` 存入顺序一致），并把 `grad_output` view 成 kernel 要的三维形状。

**调底层反向**：[tile_kernels/modeling/engram/engram_gate.py:67-70](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/engram/engram_gate.py#L67-L70) 调 `engram_gate_bwd`，注意它把前向存的 4 个中间量 `dot, gate_score, rstd_x, rstd_k` 原样传回——这正是「重放中间量」的体现。返回 4 个梯度：3 个激活梯度 + 1 个分块参数梯度 `grad_w_partial`。

**main_grad 检测与就地累加**：[tile_kernels/modeling/engram/engram_gate.py:74-81](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/engram/engram_gate.py#L74-L81) 用 `getattr(weight, 'main_grad', None)` 探测；有则用之、无则新建 fp32 零张量，再交给 `grad_w_reduce` 就地累加。`grad_w_reduce` 的签名与「就地修改」约定见 [tile_kernels/engram/engram_grad_w_reduce_kernel.py:67-89](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/engram/engram_grad_w_reduce_kernel.py#L67-L89)（`grad_weight_hidden/embed` 被标注为 *Modified in-place*）。

**返回元组**：[tile_kernels/modeling/engram/engram_gate.py:83-92](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/engram/engram_gate.py#L83-L92)。注意 `v` 的原始形状少了 `hc_mult` 维，所以单独算了 `v_origin_shape`（[L83](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/engram/engram_gate.py#L83)）。第 4、5 位用 `None if main_grad_xx is not None else grad_xx` 表达「就地累加则不返回」。

**对照组（极简版）**：mhc 的 Sinkhorn 封装 [tile_kernels/modeling/mhc/ops/sinkhorn.py:6-32](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/ops/sinkhorn.py#L6-L32) 是同一套范式的「最小骨架」：`forward` 存 `x` 与一个 kernel、`backward` 取出 `x` 算 `grad_input`、返回 `(grad_input, None, None)` 对应 3 个 forward 输入。对比可见：**套路完全一致，只是 EngramGate 多了 `main_grad` 与形状 view 的处理**。

#### 4.3.4 代码实践

**实践目标**：理解「有/无 `main_grad`」两种参数在反向返回上的差异。

**操作步骤**：

1. 阅读 [engram_gate.py 的 backward 返回段](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/engram/engram_gate.py#L74-L92)。
2. 假设两个场景，在纸上写出返回元组：
   - 场景 A：`weight_hidden` 有 `main_grad`、`weight_embed` 没有。
   - 场景 B：两者都没有 `main_grad`。
3. 对照 [grad_w_reduce 的就地修改约定](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/engram/engram_grad_w_reduce_kernel.py#L67-L89)，确认「就地累加」发生在 `grad_w_reduce` 内部。

**需要观察的现象**：场景 A 的返回元组第 4 位是 `None`、第 5 位是 `grad_we`；场景 B 的第 4、5 位都是张量。

**预期结果**：

- 场景 A：`(grad_x, grad_k, grad_v, None, grad_we, None, None)`。
- 场景 B：`(grad_x, grad_k, grad_v, grad_wh, grad_we, None, None)`。

**待本地验证**：若有 GPU 与可运行的 TileKernels 环境，可构造一个带 `main_grad` 属性的参数张量（`w = torch.nn.Parameter(...); w.main_grad = torch.zeros_like(w, dtype=torch.float32)`），跑一次 `engram_gate(...).sum().backward()`，断言 `w.main_grad` 被非零更新、而 `w.grad` 仍为 `None`。

#### 4.3.5 小练习与答案

**练习 1**：为什么对带 `main_grad` 的参数要在返回元组里返回 `None`？  
**答案**：梯度已由 `grad_w_reduce` 就地累加进 `main_grad`；若再返回一个张量，PyTorch 会把它写进 `.grad`，导致同一份梯度被存两份并引发重复累加。

**练习 2**：返回元组末尾的两个 `None` 分别对应 `forward` 的哪两个输入？  
**答案**：`clamp_value` 与 `eps`（它们是 `float`，不可微）。

**练习 3**：`backward` 里调用的 `dot, gate_score, rstd_x, rstd_k` 是从哪里来的？为什么不重新算？  
**答案**：来自 `ctx.saved_tensors`（前向 `save_for_backward` 存下）。重新算等于再做一遍前向，浪费带宽与算力；存盘复用是把前向算力「摊」给反向。

## 5. 综合实践

把本讲三块知识串起来，**为 `transpose` 写一个可自动求导的封装**（对应讲义规格里的实践任务）。

**任务**：实现 `TransposeFn(torch.autograd.Function)`，前向调用 `tile_kernels.transpose.transpose`，反向利用「转置的导数仍是转置」再调一次 `transpose`。

**参考实现（示例代码）**：

```python
# 示例代码：在安装了 TileKernels 的 GPU 环境运行
import torch
from tile_kernels.transpose import transpose

class TransposeFn(torch.autograd.Function):
    @staticmethod
    def forward(ctx, x):
        # 注意：transpose 要求最后两维均可被 64 整除、且最后一维连续（见 batched_transpose 的 assert）
        ctx.input_shape = x.shape
        y = transpose(x.contiguous())     # y = xᵀ，形状 (N, M)
        return y

    @staticmethod
    def backward(ctx, grad_output):
        # d/dx (xᵀ) = transpose 算子；grad_output 形状 (N, M)，再转置得 (M, N) = 输入形状
        grad_x = transpose(grad_output.contiguous())
        return grad_x                       # forward 只有 1 个输入，故返回 1 个梯度

# 可微封装的便捷别名
differentiable_transpose = TransposeFn.apply
```

**操作步骤**：

1. 确认 `forward` 只有 1 个输入 `x`，所以 `backward` 只返回 1 个梯度（对照 4.1 的「一一对应」契约）。
2. 构造 `x = torch.randn(128, 256, device='cuda', dtype=torch.float32, requires_grad=True)`（两维均被 64 整除，满足 `batched_transpose` 的约束，见 [batched_transpose_kernel.py:107](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/transpose/batched_transpose_kernel.py#L107)）。
3. 执行 `y = differentiable_transpose(x); y.sum().backward()`，检查 `x.grad` 的形状是否为 `(128, 256)` 且数值合理（梯度全为 1，因为 `sum()` 对每个元素求导为 1，转置不改变全 1 性质）。
4. 用 `torch.autograd.gradcheck`（需 `dtype=float64`，但 TileKernels 可能不支持 fp64，故此步可降级为「与 `x.T.contiguous()` 的数值对拍」）。

**需要观察的现象**：`y.shape == (256, 128)`；反向后 `x.grad.shape == (128, 256)`；`x.grad` 的每个元素都接近 `1.0`。

**预期结果**：前向输出与 `x.T.contiguous()` 位精确一致；反向后 `x.grad` 全 1、形状与 `x` 相同。

**待本地验证**：上述运行结果依赖真实 GPU 与 TileKernels 安装；若无环境，可把 `transpose(...)` 替换成 `x.contiguous().T` 跑通逻辑骨架，确认 `forward/backward` 契约正确（此时为纯 PyTorch，CPU 可跑）。

**进阶（可选）**：参考 4.3，给 `TransposeFn` 增加一个「跳过中间张量」的推理快路径——当 `torch.is_grad_enabled()` 为 `False` 时，`forward` 直接返回 `transpose(x.contiguous())` 而不存任何上下文（与 mhc 的推理大融合同源的思想，见 u7-l2）。

## 6. 本讲小结

- **modeling 层只做可微封装**：`EngramGateFn` 不写算子逻辑，只把底层 TileLang kernel 接进 PyTorch autograd。
- **核心契约**：`backward` 返回的梯度元组必须与 `forward` 输入（除 `ctx`）**逐位一一对应**；非张量输入（`clamp_value/eps`）也要占 `None` 槽。
- **存盘分工**：张量用 `ctx.save_for_backward`（接入版本计数），非张量用 `ctx.xxx = ...`；`save_for_backward` 可存任意张量，包括前向中间量。
- **形状处理**：`forward` 先 `view` 成 kernel 要的固定 rank、记下 `origin_shape`，输出与梯度再 `view` 回去。
- **main_grad 就地累加**：参数带 `main_grad` 时，`grad_w_reduce` 把梯度 `+=` 进该缓冲，并在返回元组里对该参数返回 `None`，避免双份存储与重复累加。
- **重放而非重算**：反向复用前向存的 `dot/gate_score/rstd_x/rstd_k`，把前向算力摊给反向。

## 7. 下一步学习建议

- 想看「同一范式的更复杂用法」，继续读 **u8-l2（MHC functional API）** 与 **u8-l3（mhc ops 层 autograd 封装）**，后者会涉及反向里按 `num_sms` 分块再 `.sum(0)` 聚合的归约细节。
- 想深入 engram 反向的数学，回看 **u6-l2（Engram 反向与权重梯度归约）**，理解 `dot` 为何存的是未归一化原始点积、`grad_w_partial` 为何需要二次归约。
- 想理解「推理态不存上下文」的另一种用法，阅读 **u7-l2（MHC 前处理流水线与融合）** 中 `pre_big_fuse` 的 `torch.is_grad_enabled()` 分流。
- 动手方向：仿照本讲综合实践，为 `tile_kernels` 里任意一个**纯前向**算子（如 `engram_hash`）补一个 `autograd.Function` 封装，练习 `save_for_backward` 的选择与返回元组的对齐。
