# Autograd 集成与反向内核

## 1. 本讲目标

上一讲（u4-l1）我们拆解了 cuTile 版 `silu_and_mul` 的**前向**内核——它是 row-wise 逐元素内核的标准模板。但一个能在真实 LLM 训练里用的算子，光有前向远远不够：PyTorch 的自动求导引擎（autograd）必须能在反向传播时调用你写的**反向内核**。

本讲就回答一个问题：**怎样把一个手写的 cuTile GPU 内核，包装成 PyTorch autograd 能理解、能反向的算子？**

学完后你应当掌握：

- 用 `torch.autograd.Function` 把自定义内核接入 autograd 引擎的标准写法。
- `save_for_backward` / `ctx.saved_tensors` 这条「前向留给反向的数据通道」。
- **反向重计算（recomputation）策略**：为什么反向内核宁可重新算一遍 `sigmoid`，也不把中间激活存下来。
- `requires_grad` 分支：同一个算子函数为何要在「走 autograd」与「直接调内核」两条路径间分叉。

本讲全程以 [`src/tilegym/ops/cutile/silu_and_mul.py`](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/silu_and_mul.py) 为唯一主样本，它是全仓库里最干净的「前向 + 反向 + autograd 封装」三位一体案例。

## 2. 前置知识

阅读本讲前，建议你已经：

- 读懂 u4-l1，知道 `silu_and_mul` 的前向内核 `_silu_and_mul_kernel_row_wise` 如何用 `ct.gather` 取出 `a`、`b` 两半、在片上算 `silu(a)*b`、再 `ct.scatter` 写回。
- 了解 PyTorch autograd 的基本直觉：每个算子在「前向」产出结果时，顺便在一张「计算图」上登记自己；调用 `.backward()` 时，引擎**逆着这张图**，对每个算子调用它的反向实现，把输出侧梯度（`grad_output`）换算成输入侧梯度（`grad_input`）。
- 知道 cuTile 内核通过 `ct.launch(stream, grid, kernel, args)` 从主机侧提交到 GPU（见 u3-l3）。

几个本讲会反复用到的术语：

| 术语 | 含义 |
|---|---|
| 前向（forward） | 由输入算输出，即 `c = silu(a) * b` |
| 反向（backward） | 由输出侧梯度 `dc` 算输入侧梯度 `da`、`db` |
| 激活（activation） | 前向算出的、可能被反向复用的中间张量（如 `sigmoid(a)`、`silu(a)`） |
| 重计算（recomputation） | 反向时不读保存的激活，而是从原始输入重新算一遍 |

## 3. 本讲源码地图

本讲只围绕一个主文件展开，辅以两个支撑文件：

| 文件 | 作用 |
|---|---|
| [`src/tilegym/ops/cutile/silu_and_mul.py`](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/silu_and_mul.py) | **主样本**。含反向内核、autograd 封装、`requires_grad` 分支 |
| [`src/tilegym/experimental.py`](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/experimental.py) | `@experimental_kernel` 装饰器，给反向内核打「实验性」一次性告警 |
| [`src/tilegym/ops/ops.py`](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/ops.py) | 统一算子接口，提供 `silu_and_mul` 的 `@dispatch` stub |
| [`tests/ops/test_silu_and_mul.py`](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/ops/test_silu_and_mul.py) | 正确性测试，用 `requires_grad=True` + `gradient=dy` 同时验证前向与反向 |

`silu_and_mul.py` 内部的关键组件分层如下：

```
_silu_and_mul_backward_kernel_row_wise   # 反向 cuTile 内核（GPU 上算 da/db）
        ↑ 被
_silu_and_mul_backward(grad_output, input)   # 主机侧启动函数（算 grid、launch）
        ↑ 被
_SiLUAndMulFunction.backward(ctx, grad_output)   # autograd 反向入口
_SiLUAndMulFunction.forward(ctx, input)          # autograd 前向入口（save_for_backward）
        ↑ 被
silu_and_mul(input, out=None)   # 对外注册的算子函数，按 requires_grad 分叉
```

自下而上看：最底层是 GPU 内核，往上是主机启动函数，再往上是 autograd 封装，最顶是被 `@register_impl` 注册到分发器的对外函数。本讲从顶层「为什么分叉」讲到底层「反向内核怎么算」，但章节顺序按概念展开。

---

## 4. 核心概念与源码讲解

### 4.1 torch.autograd.Function：把内核接入 autograd 引擎

#### 4.1.1 概念说明

PyTorch 自己内置的算子（加、乘、卷积……）天生就带反向实现。但你写了一个**自定义 GPU 内核**，autograd 引擎并不知道它怎么求导。`torch.autograd.Function` 就是 PyTorch 提供的「自定义算子 + 自定义反向」的官方契约：

- 你继承 `torch.autograd.Function`，写两个**静态方法**：`forward(ctx, ...)` 和 `backward(ctx, ...)`。
- `forward` 负责真正计算（在这里启动你的内核），`backward` 负责求导（在这里启动你的反向内核）。
- 调用时**不直接调** `forward`，而是用 `MyFunction.apply(...)`——`apply` 会替你在计算图上登记这个算子，这样后续 `.backward()` 才会自动触发你的 `backward`。

`ctx`（context）是一个上下文对象，是 `forward` 与 `backward` 之间**唯一的通信渠道**：`forward` 往里存东西，`backward` 从里取东西。

#### 4.1.2 核心流程

一个 autograd 算子的生命周期：

```text
用户代码:  out = _SiLUAndMulFunction.apply(input)
              │
              ▼
       ┌──────────────────────────────────┐
       │ forward(ctx, input):             │   ← 1. 真正算前向（启动内核）
       │   ctx.save_for_backward(input)   │   ← 2. 把反向需要的东西存进 ctx
       │   ...启动 _silu_and_mul_kernel.. │
       │   return output                  │
       └──────────────────────────────────┘
              │ autograd 引擎在计算图上登记此算子
              ▼
       （后续很多层计算……）
              │
用户代码:  out.backward(grad_output)
              │
              ▼
       ┌──────────────────────────────────┐
       │ backward(ctx, grad_output):      │   ← 3. 反向触发
       │   (input,) = ctx.saved_tensors   │   ← 4. 取出 forward 存的东西
       │   ...启动反向内核算 da/db...      │
       │   return grad_input              │   ← 5. 每个前向输入对应一个梯度
       └──────────────────────────────────┘
```

**铁律**：`backward` 的返回值个数，必须与 `forward` 的**输入个数**（不算 `ctx`）严格一一对应——哪怕某个输入不需要梯度，也要返回 `None`。`silu_and_mul` 的 `forward(ctx, input)` 只有一个输入 `input`，所以 `backward` 只返回一个 `grad_input`。

#### 4.1.3 源码精读

`_SiLUAndMulFunction` 的前向入口如下：

[`silu_and_mul.py:158-193`](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/silu_and_mul.py#L158-L193) —— 类定义与 `forward`。

关键几行：

```python
class _SiLUAndMulFunction(torch.autograd.Function):
    @staticmethod
    def forward(ctx, input: torch.Tensor):
        ctx.save_for_backward(input)          # 见 4.2
        ...
        input_flat = input.view(-1, original_shape[-1])
        batch_size = input_flat.shape[0]
        output = torch.empty((batch_size, hidden_size), ...)
        TILE_SIZE = next_power_of_2(hidden_size)
        grid = (batch_size,)
        ct.launch(
            torch.cuda.current_stream(), grid,
            _silu_and_mul_kernel_row_wise,
            (input_flat, output, TILE_SIZE, hidden_size),
        )
        ...
        return output.view(*output_shape)
```

要点逐条对应：

- `forward` 是 `@staticmethod`，第一参数永远是 `ctx`，其后才是真正的算子输入。
- `ctx.save_for_backward(input)`：把反向要用的张量登记进上下文（下一节详述）。
- 前向计算本身**复用了 u4-l1 讲过的前向内核** `_silu_and_mul_kernel_row_wise`，没有任何重写——`grid = (batch_size,)` 是 row-wise 调度，一块算一行。
- 输出张量由主机侧 `torch.empty` 分配，内核写回，最后 `view` 回原始形状。

> 注意：上一讲 u4-l3/softmax 的 `_Softmax` 只定义了 `forward`、没有 `backward`，所以它只能做推理。本讲的 `_SiLUAndMulFunction` 才是「完整的可训练算子」范例——这正是本讲相对 softmax 那一讲的新东西。

#### 4.1.4 代码实践

**实践目标**：确认「不经过 `apply` 直接调用 `forward` 会绕过 autograd」这一机制。

**操作步骤**（示例代码，非项目原有代码）：

```python
import torch
import tilegym
# 假设 cutile 后端可用
x = torch.randn(4, 128, device="cuda", requires_grad=True)

# ✅ 正确：经 apply，autograd 能反向
y = tilegym.ops.silu_and_mul(x)
print("apply 后 requires_grad:", y.requires_grad)   # 预期 True
y.sum().backward()
print("x.grad is not None:", x.grad is not None)    # 预期 True
```

**需要观察的现象**：`y.requires_grad` 为 `True`，且 `x.grad` 在 `backward()` 后非空——说明 autograd 引擎确实登记并触发了反向内核。

**预期结果**：上述两个断言都成立。**待本地验证**（本机需有可用的 cutile 后端与 GPU）。

#### 4.1.5 小练习与答案

**练习 1**：如果 `forward(ctx, input)` 的输入只有一个 `input`，`backward` 却返回了两个张量 `(g1, g2)`，会发生什么？

**答案**：运行时抛 `RuntimeError`。autograd 要求 `backward` 返回值的个数等于 `forward` 的输入个数（不计 `ctx`）。多返回会被判为签名不符。

**练习 2**：为什么调用时写 `_SiLUAndMulFunction.apply(input)` 而不是 `_SiLUAndMulFunction.forward(ctx, input)`？

**答案**：`apply` 是 PyTorch 提供的包装器，它会自动构造 `ctx`、在计算图上登记此算子、并在 `.backward()` 时回调你的 `backward`。直接调 `forward` 既拿不到正确的 `ctx`，也不会登记计算图，反向就不会触发。

---

### 4.2 save_for_backward：前向留给反向的数据契约

#### 4.2.1 概念说明

反向求导需要用到前向的一些量。比如对 `c = silu(a) * b` 求导：

\[
\frac{\partial c}{\partial b} = \text{silu}(a), \qquad
\frac{\partial c}{\partial a} = b \cdot \text{silu}'(a)
\]

两个梯度都需要前向时的 `a`、`b`（以及由 `a` 派生的 `sigmoid(a)`）。这些量要么在前向时**保存下来**留给反向，要么反向时**重新计算**。

`ctx.save_for_backward(...)` 就是「保存」这条通道：在前向把张量塞进 `ctx`，反向用 `ctx.saved_tensors` 取出来。它和直接把张量挂到 `self.xxx` 的区别在于：`save_for_backward` 会被 autograd 引擎正确地纳入版本计数与生命周期管理，是官方推荐做法。

#### 4.2.2 核心流程

```text
forward 阶段:
   ctx.save_for_backward(input)        # 存：选择「只存原始输入」
            │
            │  （ctx 持有 input 的引用，直到 backward 结束）
            ▼
backward 阶段:
   (input,) = ctx.saved_tensors        # 取：拿到同一个 input
   grad_a, grad_b = _silu_and_mul_backward(grad_output, input)
```

这里有一个**关键设计决策**：保存的是**原始输入 `input`**，而不是 `sigmoid(a)`、`silu(a)` 这类中间激活。原因有二：

1. `input` 是算子的入参，本身就会被计算图持有，`save_for_backward` 只是再 pin 住它的引用，**几乎不增加显存**。
2. 一旦有了 `input`，`a`、`b` 只是它的两半切片，`sigmoid(a)` 也能重算出来——没必要把激活额外存一份。

这就是下一节「重计算策略」的伏笔。

#### 4.2.3 源码精读

前向存入（与 4.1 同一处）：

[`silu_and_mul.py:162-164`](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/silu_and_mul.py#L162-L164) —— `forward` 开头立刻保存输入：

```python
    @staticmethod
    def forward(ctx, input: torch.Tensor):
        # Save input for backward (used in recomputation)
        ctx.save_for_backward(input)
```

注释里的 `used in recomputation` 一语道破：存的不是激活，而是「重计算所需的原料」。

反向取出：

[`silu_and_mul.py:195-202`](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/silu_and_mul.py#L195-L202) —— `backward`：

```python
    @staticmethod
    def backward(ctx, grad_output):
        (input,) = ctx.saved_tensors
        grad_a, grad_b = _silu_and_mul_backward(grad_output, input)
        # Concatenate gradients for the original input layout
        grad_input = torch.cat([grad_a, grad_b], dim=-1)
        return grad_input
```

两处细节：

- `(input,) = ctx.saved_tensors`：`saved_tensors` 是个元组，解包语法与 `forward` 里 `save_for_backward(input)` 存进去的个数一一对应。
- `torch.cat([grad_a, grad_b], dim=-1)`：反向内核分别算出了对 `a` 半、`b` 半的梯度（各 `hidden_size` 宽），但原始输入 `input` 是 `2*hidden_size` 宽的拼接布局，所以最后要 `cat` 回去，让 `grad_input` 的形状与 `input` 完全一致。这一步也保证了返回的梯度能与「`forward` 的唯一输入」对齐。

#### 4.2.4 代码实践

**实践目标**：体会「保存 input」与「保存激活」在显存上的差别。

**操作步骤**：阅读 [`silu_and_mul.py:84`](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/silu_and_mul.py#L84) 附近反向内核里的注释 `# Recompute a and b from input (saves memory vs saving in forward)`，然后回答：假如把 `ctx.save_for_backward(input)` 改成在前向额外物化并保存 `sigmoid_a`（形状与输出相同），对于一个 `(8, 1024, 2048)` 的 `input`，会多占多少显存？

**需要观察的现象 / 预期结果**：

- `input` 形状 `(8, 1024, 2*1024)` = `(8192, 2048)`，约 `1.677e7` 个元素。
- `sigmoid_a` 形状 `(8192, 1024)` ≈ `8.39e6` 个元素；若以 fp32 物化保存，多占 `8.39e6 * 4 bytes ≈ 32 MiB`。
- 结论：保存激活每多存一个 `hidden_size` 宽的 fp32 张量，就多约 32 MiB；而保存 `input` 本身几乎零增量。详见 4.3 的定量分析。

#### 4.2.5 小练习与答案

**练习 1**：为什么用 `ctx.save_for_backward(input)` 而不是 `ctx.input = input`？

**答案**：`save_for_backward` 会让 autograd 引擎正确处理该张量的版本控制（in-place 修改检测）与生命周期；直接挂属性绕过了这些检查，可能在前向输入被原地修改时给出错误梯度，官方文档明确不推荐。

**练习 2**：`backward` 里 `(input,) = ctx.saved_tensors` 的逗号能不能去掉？

**答案**：不能。`ctx.saved_tensors` 返回的是**元组**。去掉逗号 `input = ctx.saved_tensors` 会把整个元组赋给 `input`，后续 `_silu_and_mul_backward(grad_output, input)` 收到的是元组而非张量，立即报错。

---

### 4.3 反向重计算策略：为什么不存激活

#### 4.3.1 概念说明

「重计算（recomputation）」是深度学习系统里经典的**用算力换显存**技巧。思路很朴素：

- **存激活**方案：前向把反向要用的中间量（`sigmoid(a)`、`silu(a)`）算好、物化成张量、存进显存；反向直接读，省算力但吃显存。
- **重计算**方案：前向只保存最原始的输入；反向时把中间量从头再算一遍，省显存但多花算力。

对 `silu_and_mul` 这种**逐元素、访存密集**的内核，重计算几乎免费：反向内核本来就要从显存读 `input` 和 `grad_output`，多算一次 `exp` 和几次乘法只是在片上寄存器里多做几个运算，几乎不增加访存，而访存才是这类内核的瓶颈。于是「重计算」在这里是净赚——大幅省显存，几乎不降速。

#### 4.3.2 核心流程

先看反向的数学。记 \(\,s = \sigma(a)\) 为 sigmoid，\(\,\text{silu}(a) = a \cdot s\)，输出 \(c = \text{silu}(a)\cdot b\)。给定输出侧梯度 \(dc\)：

\[
db = dc \cdot \text{silu}(a) = dc \cdot a\,s
\]

\[
da = dc \cdot b \cdot \text{silu}'(a), \qquad
\text{silu}'(a) = s + a\,s\,(1-s)
\]

源码顶部把这组公式写成了注释，注意它把 \(\text{silu}'(a)\) 等价改写成了便于复用 `silu_a` 的形式：

\[
\text{silu}'(a) = s + \text{silu}(a)\cdot(1-s)
\]

反向内核的执行流程：

```text
对每一行（bid = 行号）:
  1. 用 gather 读出 grad_output 的这一行  → dc
  2. 用 gather 从 input 重算 a、b          ← 重计算（不读保存的激活）
  3. 重算 sigmoid(a) 与 silu(a)            ← 重计算
  4. db = dc * silu(a)        → scatter 写入 grad_b
  5. da = dc * b * silu'(a)   → scatter 写入 grad_a
```

整个内核与前向内核结构高度对称：同样的 row-wise grid、同样的 `gather` 取两半、同样的近似 sigmoid——区别只是输入多了 `grad_output`，输出变成 `grad_a`、`grad_b` 两个。

#### 4.3.3 源码精读

反向内核定义（注意两层装饰器）：

[`silu_and_mul.py:57-72`](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/silu_and_mul.py#L57-L72) —— 反向公式注释与内核签名：

```python
# Backward kernel for silu_and_mul
# Computes gradients using recomputation (no saved activations)
# Forward: c = silu(a) * b = a * sigmoid(a) * b
# da = dc * b * (sigmoid(a) + a * sigmoid(a) * (1 - sigmoid(a)))
#    = dc * b * sigmoid(a) * (1 + a * (1 - sigmoid(a)))
# db = dc * silu(a)
@experimental_kernel
@ct.kernel
def _silu_and_mul_backward_kernel_row_wise(
    grad_output, input, grad_a, grad_b,
    TILE_SIZE: ConstInt, TOTAL_HIDDEN_SIZE: ConstInt,
):
```

- `@experimental_kernel`（外层）打「实验性」标记，见本节末尾。
- `@ct.kernel`（内层）把它交给 tileiras 编译成 GPU 代码（同 u3-l1）。
- 两个输出 `grad_a`、`grad_b` 由主机侧预先 `torch.empty_like` 分配，内核写入——这是 cuTile 的常见模式：输出张量在主机侧分配、设备侧填充。

重计算与梯度计算的主体：

[`silu_and_mul.py:84-106`](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/silu_and_mul.py#L84-L106)：

```python
    # Recompute a and b from input (saves memory vs saving in forward)
    a_tile = ct.gather(input, (row_idx, a_col_idx), check_bounds=True)
    b_tile = ct.gather(input, (row_idx, b_col_idx), check_bounds=True)
    a_tile = ct.astype(a_tile, torch.float32)
    b_tile = ct.astype(b_tile, torch.float32)

    # Recompute sigmoid(a) and silu(a)
    denom = 1 + ct.exp(-a_tile)
    sigmoid_a = ct.truediv(1.0, denom, flush_to_zero=True, rounding_mode=RMd.APPROX)
    silu_a = a_tile * sigmoid_a

    db_tile = dc_tile * silu_a
    ...
    ct.scatter(grad_b, (row_idx, offsets), db_tile, check_bounds=True)

    one_minus_sigmoid = 1.0 + -sigmoid_a
    silu_grad = sigmoid_a + silu_a * one_minus_sigmoid   # = silu'(a)
    da_tile = dc_tile * (b_tile * silu_grad)
    ...
    ct.scatter(grad_a, (row_idx, offsets), da_tile, check_bounds=True)
```

注意三件事：

1. **重计算的近似必须与前向完全一致**：这里 `flush_to_zero=True, rounding_mode=RMd.APPROX` 与前向内核（u4-l1）逐字相同。前向和反向用同一种 sigmoid 近似，才能保证数值上 `backward` 真的是 `forward` 的导数；否则梯度检验会失败。
2. `one_minus_sigmoid = 1.0 + -sigmoid_a`：用加负数代替减法，是 cuTile DSL 里常见的写法（避免某些减法算子的低效_lowering）。
3. `da`、`db` 两次 `ct.scatter` 写入两个不同输出张量，互不干扰。

主机侧启动函数：

[`silu_and_mul.py:122-155`](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/silu_and_mul.py#L122-L155) —— `_silu_and_mul_backward`：

```python
    grad_output_flat = grad_output.contiguous().view(-1, hidden_size)
    input_flat = input.contiguous().view(-1, input.shape[-1])
    batch_size = grad_output_flat.shape[0]
    grad_a = torch.empty_like(grad_output_flat)
    grad_b = torch.empty_like(grad_output_flat)

    TILE_SIZE = next_power_of_2(hidden_size)
    grid = (batch_size,)
    ct.launch(
        torch.cuda.current_stream(), grid,
        _silu_and_mul_backward_kernel_row_wise,
        (grad_output_flat, input_flat, grad_a, grad_b, TILE_SIZE, hidden_size),
    )
    return grad_a.view(*original_output_shape), grad_b.view(*original_output_shape)
```

- 先把 `grad_output`、`input` 都 `.contiguous()` 再 `view` 成 2D——这正是 u4-l1 讲过的「连续化保证」（内核按紧密行主序算偏移）。
- `grid = (batch_size,)`：row-wise 调度，一块算一行，不读 SM 数、不带 occupancy 提示（与前向一致）。
- 返回 `(grad_a, grad_b)` 由 4.2 的 `backward` 方法 cat 成 `grad_input`。

**`@experimental_kernel` 标记**：[`experimental.py:25-65`](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/experimental.py#L25-L65) 定义该装饰器，它并不改变内核行为，只是给内核对象挂一个 `_tracked_message` 属性；tilegym 导入时会 monkey-patch `ct.launch`（[`experimental.py:68-73`](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/experimental.py#L68-L73)），在内核第一次启动时打印一次性告警「该内核由外部贡献者提交，尚未经核心团队完整验证」，打印后清空标记不再重发。所以你在跑反向时大概率会看到这条 warning——这是预期行为，不是出错。

#### 4.3.4 代码实践

**实践目标**：定量估算「重计算 vs 保存激活」的显存收益（本讲的核心实践任务）。

**操作步骤**：

1. 阅读反向内核 [`silu_and_mul.py:84-93`](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/silu_and_mul.py#L84-L93)，确认它只从 `input` 重算 `a/b/sigmoid_a/silu_a`，没有读任何额外保存的激活。
2. 取一个具体形状做估算：设 `input` 形状为 `(N, 2H)`，`N = 8*1024 = 8192`，`H = 1024`。

**分析**：

- 当前重计算方案：`save_for_backward(input)` 只 pin 住输入，**额外激活显存 ≈ 0**。
- 若改为「保存激活」：反向需要的中间量至少包括 `sigmoid_a`（`N×H`）和 `silu_a`（`N×H`）。若按 fp32 物化保存：
  - 单个 `N×H` 张量 = `8192 * 1024 * 4 bytes ≈ 32 MiB`。
  - 存两个（`sigmoid_a` + `silu_a`）≈ **64 MiB**；即使只存一个也约 **32 MiB**。
- 相比之下，重计算多花的算力是：每行多算 1 次 `exp`、几次乘加。对一个已经要从 HBM 读 `input`（`2H` 个元素）和 `grad_output`（`H` 个元素）的访存密集内核，这点片上运算几乎隐藏在访存延迟里，**实测耗时增加通常可忽略**。

**需要观察的现象 / 预期结果**：重计算用「可忽略的额外算力」换来「每个 `N×H` 的激活张量省下 32 MiB（fp32）量级显存」。在大 batch、大 hidden 的训练场景，这类节省乘以几十层 MLP，累积非常可观。**精确的耗时增幅待本地用 `tests/ops/test_silu_and_mul.py::Test_SiLUAndMul::test_perf` 基准验证**。

#### 4.3.5 小练习与答案

**练习 1**：如果前向内核把 `rounding_mode` 改成精确舍入，反向仍用 `RMd.APPROX`，会发生什么？

**答案**：梯度检验（`assertCorrectness` 对比 `torch.nn.functional.silu` 的参考梯度）可能误差变大甚至超出容差。因为反向用的 `sigmoid` 近似不再是前向 `sigmoid` 的真实导数，链式求导的数值一致性被破坏。前后向必须用同一套近似。

**练习 2**：`db = dc * silu(a)`，`da = dc * b * silu'(a)`。如果 `b` 全为 0，`da` 会怎样？这能说明重计算的什么特性？

**答案**：`b=0` 时 `da=0`，与保存激活方案结果一致。这说明重计算在数学上等价于保存激活（只要近似一致），它只是改变了「中间量何时算」，不改变结果——这正是它可作为「显存优化手段」的前提。

---

### 4.4 requires_grad 分支：autograd 路径与直调路径

#### 4.4.1 概念说明

推理（inference）时，输入张量 `requires_grad=False`，根本不会触发反向。如果此时仍走 `torch.autograd.Function.apply`，会白白多建计算图、多存上下文，纯粹是开销。于是 `silu_and_mul` 在**最外层**做了一个分叉：

- `input.requires_grad == True`：走 autograd 路径，经 `_SiLUAndMulFunction.apply`，保证能反向。
- `input.requires_grad == False`：跳过 autograd，直接启动前向内核，省去建图与存激活的开销。

这是一个非常实用的工程优化：同一个算子函数，训练和推理各走最优路径。

#### 4.4.2 核心流程

```text
tilegym.ops.silu_and_mul(input)            # 统一入口（经 dispatch 路由到本实现）
        │
        ├── input.requires_grad == True ?
        │
        ├── 是 ──▶ _SiLUAndMulFunction.apply(input)
        │            （建图 + save_for_backward；后续可 .backward()）
        │            ⚠ 若同时传了 out= 参数 → 抛 ValueError
        │
        └── 否 ──▶ 直接 ct.launch(_silu_and_mul_kernel_row_wise, ...)
                     （不建图、不存激活；支持 out= 就地写入）
```

#### 4.4.3 源码精读

对外函数与两层装饰器：

[`silu_and_mul.py:205-226`](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/silu_and_mul.py#L205-L226)：

```python
@register_impl("silu_and_mul", backend="cutile")
@_ensure_contiguous
def silu_and_mul(input, out=None):
    # Use autograd wrapper when backward is needed
    if input.requires_grad:
        if out is not None:
            raise ValueError("out parameter not supported when requires_grad=True")
        return _SiLUAndMulFunction.apply(input)

    # Direct kernel call for inference (no backward needed)
    ...
```

三个要点：

- **装饰器顺序**：`@register_impl` 在外、`@_ensure_contiguous` 在内。这意味着：分发器查表拿到的是**已经包了连续化**的 `wrapper`；每次调用先连续化所有张量参数（u4-l1 讲过，已连续则零开销），再进入真正的 `silu_and_mul` 函数体做分支判断。
- **`requires_grad` 分支**：进入 autograd 路径前，显式禁止 `out=` 参数。原因是 `autograd.Function` 的输出由 `forward` 内部 `torch.empty` 分配并返回，无法接受外部预分配的 `out` 张量——强行支持会让「就地写回」与「autograd 持有输出」语义冲突。
- **直调路径**：[`silu_and_mul.py:228-262`](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/silu_and_mul.py#L228-L262) 支持 `out=None` 或外部 `out` 张量（就地写入），逻辑与 `_SiLUAndMulFunction.forward` 里的前向启动几乎重复——这是为了推理路径绕开 autograd 而刻意保留的「重复」。

`_ensure_contiguous` 的实现：

[`silu_and_mul.py:109-119`](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/silu_and_mul.py#L109-L119)：

```python
def _ensure_contiguous(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        def maybe_to_contiguous(x):
            return x.contiguous() if isinstance(x, torch.Tensor) else x
        args = [maybe_to_contiguous(arg) for arg in args]
        kwargs = {k: maybe_to_contiguous(v) for k, v in kwargs.items()}
        return fn(*args, **kwargs)
    return wrapper
```

它对位置参数和关键字参数里的每个 `torch.Tensor` 都调 `.contiguous()`，非张量原样放行。

**分发层入口**：本函数通过 `@register_impl("silu_and_mul", backend="cutile")` 注册到全局注册表（见 u2-l2）。用户调 `tilegym.ops.silu_and_mul(input)` 时，`ops.py` 里那个只抛 `NotImplementedError` 的 stub（[`ops.py:172-193`](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/ops.py#L172-L193)）会被当前后端（默认 cutile）的实现——即本函数——替换。

> 真实测试侧的佐证：[`test_silu_and_mul.py:59-62`](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/ops/test_silu_and_mul.py#L59-L62) 在 `backend == "tilecpp"` 时会 `pytest.skip`，理由写着「tilecpp silu_and_mul does not implement backward; the gradient check would raise NotImplementedError」。这说明「是否实现反向」是逐后端的能力差异：cutile 后端有完整的 autograd 分支（本讲），tilecpp 后端则没有。`requires_grad` 分支 + 注册表分发共同决定了「同一个算子名，不同后端能否训练」。

#### 4.4.4 代码实践

**实践目标**：观察 `requires_grad` 分支如何被测试驱动，并对比两条路径。

**操作步骤**：

1. 打开 [`tests/ops/test_silu_and_mul.py`](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/ops/test_silu_and_mul.py)，定位 [`test_silu_and_mul.py:71`](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/ops/test_silu_and_mul.py#L71) 附近：
   ```python
   x = torch.randn(input_shape, dtype=dtype, device=device, requires_grad=True)
   dy = 0.1 * torch.randn((batch_size, seq_len, hidden_size), dtype=dtype, device=device)
   self.assertCorrectness(tilegym.ops.silu_and_mul, self.reference,
                          {"input": x}, gradient=dy, rtol=0.0, atol=1e-2)
   ```
2. 阅读 [`common.py:265-316`](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/common.py#L265-L316) 中 `assertCorrectness` 的梯度检验段：当 `test_out.requires_grad` 为真且传了 `gradient`，它会分别对参考实现和测试实现调 `.backward(gradient)`，再比较两者输入张量的 `.grad`。

**需要观察的现象**：

- 因为 `x.requires_grad=True`，`tilegym.ops.silu_and_mul(x)` 走的是 `_SiLUAndMulFunction.apply` 路径，`test_out.requires_grad` 为 `True`。
- `.backward(dy)` 触发 `_SiLUAndMulFunction.backward`，进而启动反向内核。
- 测试容差 `rtol=0.0, atol=1e-2` 相对宽松，正是因为反向用了近似 sigmoid + fp16/fp32 混合。

**预期结果**：在 cutile 后端可用时，该测试通过——即手写反向内核的梯度与 PyTorch 参考 `torch.nn.functional.silu(x1)*x2` 的梯度在 `atol=1e-2` 内一致。**待本地验证**（需 GPU + cutile 后端）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `requires_grad=True` 时禁止 `out=` 参数？如果允许会怎样？

**答案**：autograd 路径下，输出由 `_SiLUAndMulFunction.forward` 内部 `torch.empty` 分配并由 autograd 引擎接管（要登记为计算图节点、参与反向）。若同时允许外部 `out` 就地写入，会让「autograd 持有的输出」与「用户预分配的缓冲」指向同一块内存但生命周期/语义不同，极易在反向时读到被覆盖的数据。所以代码直接 `raise ValueError` 拦截。

**练习 2**：把 `@register_impl` 和 `@_ensure_contiguous` 的顺序对调，会有什么后果？

**答案**：注册到 `_REGISTRY` 的将是**未包连续化**的原始 `silu_and_mul`，于是分发器查表调用的函数不再自动 `.contiguous()`。非连续张量（如某些转置视图）传入后，内核按紧密行主序算偏移会读错数据——典型表现是「偶发数值错误」。所以装饰器顺序不能随意对调：连续化必须包在真正函数体的外层，而注册注册的是这个「已连续化」的 wrapper。

---

## 5. 综合实践

把本讲四块知识串起来，完成下面这个「读懂一次完整反向调用」的任务。

**任务**：追踪 `tilegym.ops.silu_and_mul(x)`（`x.requires_grad=True`）到 `x.grad` 被填上的全过程，画出调用链并标注每一步发生在「主机侧」还是「设备侧」。

**建议步骤**：

1. 从 [`ops.py:172-193`](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/ops.py#L172-L193) 的 `silu_and_mul` stub 出发，写出 dispatch wrapper 如何按当前后端查 `_REGISTRY`（回顾 u2-l2）。
2. 进入 [`silu_and_mul.py:205-226`](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/silu_and_mul.py#L205-L226)，说明 `requires_grad` 分支选中 `_SiLUAndMulFunction.apply`。
3. 跟到 [`silu_and_mul.py:162-193`](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/silu_and_mul.py#L162-L193) 的 `forward`：`save_for_backward`（主机侧）、`ct.launch` 前向内核（设备侧）。
4. 假想后续 `out.backward(dy)`，跟到 [`silu_and_mul.py:195-202`](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/silu_and_mul.py#L195-L202) 的 `backward`：取 `saved_tensors`、调 `_silu_and_mul_backward`、`cat` 返回。
5. 跟到 [`silu_and_mul.py:122-155`](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/silu_and_mul.py#L122-L155) 的主机启动函数，再到 [`silu_and_mul.py:65-106`](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/silu_and_mul.py#L65-L106) 的反向内核（设备侧）。

**预期产出**：一张类似下面的调用链表（供你对照）：

| 步骤 | 位置 | 主机/设备 | 动作 |
|---|---|---|---|
| 1 | `ops.py` stub + dispatch | 主机 | 查 `_REGISTRY` 路由到 cutile 实现 |
| 2 | `silu_and_mul` 入口 | 主机 | `requires_grad` 分叉 → `apply` |
| 3 | `_SiLUAndMulFunction.forward` | 主机 | `save_for_backward(input)` |
| 4 | `_silu_and_mul_kernel_row_wise` | 设备 | 前向算 `silu(a)*b` |
| 5 | `.backward(dy)` → `_SiLUAndMulFunction.backward` | 主机 | 取 `saved_tensors` |
| 6 | `_silu_and_mul_backward` | 主机 | 算 grid、分配 `grad_a/grad_b`、launch |
| 7 | `_silu_and_mul_backward_kernel_row_wise` | 设备 | 重计算 + 算 `da/db` |
| 8 | `backward` 返回 `cat([grad_a,grad_b])` | 主机 | autograd 填入 `x.grad` |

完成后，你应当能向别人讲清「一次带梯度的 `silu_and_mul` 调用，数据和控制流是怎么在主机与设备之间来回穿梭的」。

## 6. 本讲小结

- `torch.autograd.Function` 是把自定义 GPU 内核接入 PyTorch autograd 的官方契约：`forward(ctx, ...)` 算前向并启动内核，`backward(ctx, ...)` 算反向并启动反向内核，经 `.apply()` 调用才会在计算图上登记。
- `ctx.save_for_backward` 与 `ctx.saved_tensors` 是前向留给反向的**唯一数据通道**；本算子选择只保存原始 `input`，不保存任何中间激活。
- **反向重计算策略**：反向内核从 `input` 重新算出 `a/b/sigmoid(a)/silu(a)`，用「可忽略的额外算力」换「每个 `N×H` 激活张量约 32 MiB（fp32）的显存」；前提是反向的近似 sigmoid 必须与前向逐字一致。
- 反向梯度公式：\(db = dc\cdot\text{silu}(a)\)，\(da = dc\cdot b\cdot\text{silu}'(a)\)，其中 \(\text{silu}'(a)=s+\text{silu}(a)(1-s)\)；`da`、`db` 分别写入两个输出缓冲，最后在 autograd 层 `cat` 回 `2H` 布局。
- **`requires_grad` 分支**：同一个 `silu_and_mul` 函数在训练（走 `apply`、建图、存上下文）与推理（直调内核、支持 `out=` 就地写）之间分叉，各走最优路径；`requires_grad=True` 时禁止 `out=`。
- 装饰器顺序 `@register_impl`（外）+ `@_ensure_contiguous`（内）不可随意对调；「是否实现反向」是逐后端能力——cutile 有，tilecpp 无（测试直接 skip）。

## 7. 下一步学习建议

- **横向对比其它 autograd 内核**：仓库里有大量 `torch.autograd.Function` 实现，挑一个复杂度更高的来读，例如 [`src/tilegym/ops/cutile/rms_norm.py`](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/rms_norm.py)（归一化的反向，u4-l3）或 [`src/tilegym/ops/cutile/attention.py`](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/attention.py)（FMHA 的反向，u6）。重点看它们保存了什么、是否也用重计算。
- **进入归一化与归约**：下一讲 u4-l3 会讲 `rms_norm` / `layer_norm`，那里的反向涉及跨列归约（mean/rstd 的梯度），比本讲的逐元素反向更有挑战。
- **如果想动手**：仿照本讲结构，给 u4-l1 提到的 `gelu_and_mul` 补一个带 `backward` 的 `_GELUAndMulFunction`，前后向用同一套 gelu 近似，并写一个梯度检验测试对齐 `test_silu_and_mul.py` 的写法。
- **回顾依赖**：若对 `@register_impl` / dispatch 的查表细节有疑问，回头温习 u2-l2；若对内核启动与 grid 计算不熟，温习 u3-l3。
