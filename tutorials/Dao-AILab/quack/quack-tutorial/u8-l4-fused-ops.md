# 高层融合算子：Linear、MLP、fused CE

## 1. 本讲目标

本讲从「用户写神经网络」的视角出发，看 QuACK 如何在第四单元讲解的公共 GEMM API（`gemm` / `gemm_act` / `gemm_gated` / `gemm_add_inplace`）之上，搭建出三个真正会被大模型训练用到的高层算子：

1. **Linear**（`quack/linear.py`）：最基础的线性层，重点讲它的前向、反向，以及 **梯度累加融合**（fuse_grad_accum）如何把「算 dweight」和「累加进 `.grad`」合并成一次 GEMM。
2. **MLP**（`quack/mlp.py`）：两层 GEMM + 激活，重点讲 **gated（门控）融合**（swiglu 等）和**激活重算**（recompute）两种省显存/省 kernel 的手段。
3. **Fused Linear-Cross-Entropy**（`quack/linear_cross_entropy.py`）：语言模型最后一层「线性投影 + 交叉熵」，重点讲**分块（chunked）**如何避免一次性物化整张 `(序列长度, 词表大小)` 的 logits，从而把峰值显存从「全量」降到「一块」。

学完后你应当能够：

- 读懂 `torch.autograd.Function` 如何把 QuACK 的 GEMM 内核接入 PyTorch 自动微分；
- 解释 `fuse_grad_accum` 的三步走，并说清它为何与 `torch.compile` 不兼容；
- 区分 gated MLP 的 fused / recompute / concat_layout 三条路径；
- 说清 fused linear-cross-entropy 为什么要把最后一块的 `dw` 推迟到反向再算。

---

## 2. 前置知识

本讲默认你已经掌握 **u4-l3（公共 GEMM API 表面）**，尤其是：

- `gemm` 是 `D = α(A@B) + bias` 的公共入口，`out=` 指定输出张量、`out_dtype=` 指定输出精度、`alpha/beta` 是 epilogue 的线性项标量（见 `gemm_interface.py:974-1015`）。
- `gemm_act` / `gemm_gated` 把**激活函数**（或**门控激活**）融合进同一次 GEMM 的 epilogue，省掉一次额外的 kernel launch 和中间张量物化。
- `gemm_add_inplace(A, B, out)` 做的是 `out ← α(A@B) + β·out`，**就地**写回 `out`——这正是梯度累加的关键（见 `gemm_interface.py:1934-1952`）。

另外需要一点 PyTorch 自动微分常识：

- `torch.autograd.Function` 的 `forward` 用 `ctx.save_for_backward(...)` 缓存反向所需张量，`backward` 取回它们计算梯度，返回顺序与 `forward` 的输入顺序一一对应。
- 对于叶子张量（如 `nn.Linear` 的 `weight`），PyTorch 在 `backward` 结束后会把返回的梯度**累加**进 `param.grad`：若 `param.grad` 已存在则 `param.grad += 返回的梯度`，否则 `param.grad = 返回的梯度`。这条规则是理解 `fuse_grad_accum` 那行 `weight_og.grad = None` 的钥匙。

关键术语速查：

| 术语 | 含义 |
| --- | --- |
| fused（融合） | 把多个算子塞进同一次 kernel / GEMM，省中间物化与 launch 开销 |
| grad accum（梯度累加） | 多次 mini-batch 的梯度累加进同一个 `.grad`，再统一更新 |
| gated activation | 形如 `gate_fn(gate, up)` 的门控激活，如 `swiglu(g, u) = silu(g)·u` |
| recompute（激活重算） | 反向时不保存中间激活，而是重算一次，用算力换显存 |
| logits | 分类/语言模型线性层输出，未归一化的分数，维度为词表大小 V |

---

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| `quack/linear.py` | `LinearFunc` / `LinearActFunc` / `DActLinearFunc` 三个 autograd Function、各 `_LinearOps` 配置包、`Linear(nn.Linear)` 模块 |
| `quack/mlp.py` | `MLPRecomputeFunc` autograd Function、`_MLPOps` 配置包、`mlp_func` 派发器、`MLP(nn.Module)` 模块 |
| `quack/linear_cross_entropy.py` | 分块前向 `chunked_linear_cross_entropy_fwd`、`ChunkedLinearCrossEntropyFunction`、进阶 scaled-exp 流水线、`LinearCrossEntropy(nn.Linear)` 模块 |
| `quack/gemm_interface.py` | 被上面三者调用的 `gemm` / `gemm_act` / `gemm_gated` / `gemm_add` / `gemm_add_inplace` |
| `quack/activation.py` | 内核侧激活函数与 `gate_fn_map`（判断是否门控的真值表） |

一条贯穿全讲的脉络：**这三个高层算子本身不含任何 CUDA 内核**，它们只是「把若干 GEMM 调用与 PyTorch 自动微分编排到一起」的 Python 胶水。真正的算力全在第四、五单元讲过的 GEMM 内核里。

---

## 4. 核心概念与源码讲解

### 4.1 Linear：前向、反向与梯度累加融合

#### 4.1.1 概念说明

最朴素的线性层是

\[
y = x W^{\top} + b
\]

其中 \(x\) 形状 `(M, in_features)`，\(W\) 形状 `(out_features, in_features)`（注意 PyTorch 惯例：权重是「输出在前」），\(b\) 形状 `(out_features,)`。QuACK 的 `Linear` 模块继承自 `torch.nn.Linear`，所以权重布局与 PyTorch 完全一致，可以 drop-in 替换。

「融合」在这个层次上有两层含义：

1. **前向融合**：把 `x @ W.T + bias` 压成一次 `gemm` 调用（bias 作为 epilogue 的 rowvec 项，免费带上）。
2. **反向融合 + 梯度累加融合**：反向要算 \(dx\)、\(dW\)、\(db\) 三个梯度。当用户开了 `fuse_grad_accum=True` 且 `weight.grad` 已经有值（比如梯度累加训练的第 2 步以后），QuACK 不再「先算一个新 dW、再加进 .grad」，而是用 `gemm_add_inplace` **直接把新梯度累加进现有 `.grad`**，省掉一次独立的逐元素加法 kernel。

#### 4.1.2 核心流程

`LinearFunc`（最朴素变体）的流程：

**前向**：

1. autocast 类型转换（`linear_fwd_convert_type`）；
2. 关掉 autocast，把 batch 维 flatten 成 2-D；
3. `out = ops.matmul_fwd_fn(x, weight.T, bias=bias)` —— 一次融合 GEMM；
4. `linear_fwd_postprocess` 按需保存反向所需张量（关键：只有开 `fuse_grad_accum` 才额外保存 `weight_og` 引用）；
5. reshape 回 batch 形状返回。

**反向**（给定上游梯度 `dout`）：

\[
dx = \text{dout} \cdot W,\qquad dW = \text{dout}^{\top} \cdot x,\qquad db = \sum_{\text{batch}} \text{dout}
\]

- \(dx\)：`ops.matmul_bwd_dx(dout, weight)`（`dout@(out,in)` → `(M,in)`）；
- \(dW\)：`ops.matmul_bwd_dw(dout.T, x)` 或累加版 `ops.matmul_bwd_dw_inplace(dout.T, x, weight_og.grad)`；
- \(db\)：`dout.sum(0)`。

梯度累加的三步走（`linear_bwd_compute_weight_grad` 的关键分支）：

```
若 (未开 fuse_grad_accum) 或 (weight.grad 为 None) 或 (正在 torch.compile):
    dweight = gemm(dout.T, x, out_dtype=weight_dtype)      # 算一个全新 dW
否则:
    gemm_add_inplace(dout.T, x, weight_og.grad)             # 第1步: .grad ← dout.T@x + .grad（就地）
    dweight = weight_og.grad                                 # 第2步: 拿到已累加的张量
    weight_og.grad = None                                    # 第3步: 置空，防止 PyTorch 再次累加
```

第 3 步是最巧妙的地方，下一节详述。

#### 4.1.3 源码精读

先看反向的「大脑」——`linear_bwd_compute_weight_grad`，这是 `fuse_grad_accum` 的全部实现：

[quack/linear.py:48-62](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/linear.py#L48-L62) —— 计算 dweight，并在条件满足时走就地累加路径。

核心片段（已精简）：

```python
if not ctx.fuse_grad_accum or weight_og.grad is None or torch.compiler.is_compiling():
    dweight = matmul_fn(dout.T, x, out_dtype=ctx.weight_dtype)   # 普通路径
else:
    matmul_inplace_fn(dout.T, x, weight_og.grad)                 # 就地累加
    dweight = weight_og.grad
    weight_og.grad = None  # So that pytorch doesn't add dweight to weight_og.grad again
```

**为什么第 3 步要 `weight_og.grad = None`？**

因为 PyTorch 在 `backward` 返回梯度后，会自动把它累加进叶子张量的 `.grad`：若 `.grad` 存在则 `.grad += 返回值`，否则 `.grad = 返回值`。如果我们在第 1 步已经把 `dW` 加进了 `.grad`（此时 `.grad = 旧值 + dW`），又把同一个张量作为 `dweight` 返回，PyTorch 会再做一次 `.grad += dweight`，于是 `.grad` 变成 `(旧值 + dW) + (旧值 + dW)`，**旧值被加了两次**。

置空后：返回的 `dweight` 张量本身就等于「旧值 + dW」，而 `.grad` 此时是 `None`，PyTorch 直接 `.grad = dweight`，结果正确。

**为什么能省一次 kernel？**

普通路径要两步：① GEMM 算出全新的 `dW`（一次 kernel）；② PyTorch 的 `accumulate_grad` 做 `.grad += dW`（又一次逐元素 kernel，且要把整张 `.grad` 读一遍再写一遍）。融合路径只有一步：`gemm_add_inplace` 利用 GEMM epilogue 的 `β·C` 项（见 u4-l3 与 u6-l3），让每个输出 tile 在写回时自动加上已有的 `.grad` 值——**累加是搭 GEMM 顺风车，零额外开销**。

**与 `torch.compile` 的兼容性限制**：

注意那个 `torch.compiler.is_compiling()` 判断。`gemm_add_inplace` 会**就地改写一个叶子参数的 `.grad`**，这对 `torch.compile` 有两个障碍：

1. **functionalization**：dynamo 把计算图建模成「纯函数」，而就地修改 `.grad` 是副作用，会破坏 dynamo 追踪出的函数语义；
2. **fake tensor**：编译期 dynamo 在 fake tensor（无真实数据/步长）上做 trace，既无法判断 `weight_og.grad is None`，也无法拿到真实步长去走 `_ensure_contiguous` 的快路径。

因此一旦检测到正在编译，就**回退到普通路径**——保证 `torch.compile` 下仍正确（只是少了那次融合）。同一思想也体现在 `_ensure_contiguous`：

[quack/linear.py:15-21](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/linear.py#L15-L21) —— 编译期无法检查 `t.stride(-1)`，故无条件 `.contiguous()`。

再看 `LinearFunc` 本体如何把这些零件串起来：

[quack/linear.py:155-178](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/linear.py#L155-L178) —— 前向：flatten、调 `ops.matmul_fwd_fn`、保存反向张量。

[quack/linear.py:180-196](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/linear.py#L180-L196) —— 反向：算 dbias、dx、dweight，返回 `(dx, dweight, dbias, None, None)`，末尾两个 `None` 对应 `forward` 里不需求梯的 `fuse_grad_accum` 与 `ops` 参数。

注意 `linear_fwd_postprocess` 里这个细节——只有开 `fuse_grad_accum` 才会把 `weight_og`（原始 Parameter 引用）一起存下来，因为累加要写进它的 `.grad`：

[quack/linear.py:31-37](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/linear.py#L31-L37) —— `save_for_backward` 是否含 `weight_og` 取决于 `ctx.fuse_grad_accum`。

**「配置包」机制**：每个变体（带激活/不带激活/门控/concat/tuned/untuned）都用一个类把所需的几个 `partial(gemm, ...)` 函数打包成命名空间，作为一个**非张量参数**传给 `autograd.Function.apply` 并存在 `ctx` 上。这样 forward/backward 只要取 `ctx.ops.matmul_xxx_fn` 就行，不必在函数体里写一堆 if/else：

[quack/linear.py:80-84](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/linear.py#L80-L84) —— `_LinearOps` 把 `gemm` 与 `gemm_add_inplace` 各自 partial 成 fwd/dx/dw/dw_inplace 四个槽位。注意反向的 dx/dw 都加了 `dynamic_scheduler=True`，因为反向 GEMM 的形状（尤其 dW 的 `(out, M)`）常与典型前向形状不同，动态调度（CLC 工作偷取）负载更均衡（回顾 u3-l4）。

最后是用户真正接触的 `nn.Module`：

[quack/linear.py:365-369](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/linear.py#L365-L369) —— `Linear.forward`：满足「CUDA + 维度 8 对齐」才走融合内核，否则回退 `F.linear`，保证任意形状都能用。

#### 4.1.4 代码实践

**实践目标**：亲眼看到 `fuse_grad_accum` 把新梯度「加进」预存的 `.grad`，而不是覆盖；并验证在 `torch.compile` 下它会自动退回普通路径。

**操作步骤**（需要一块 CUDA GPU）：

1. 阅读源码 [quack/linear.py:48-62](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/linear.py#L48-L62)，确认三步走逻辑。
2. 运行下面的最小示例（**示例代码，非项目自带**）：

```python
import torch
from quack.linear import Linear

torch.manual_seed(0)
dev = "cuda"
lin = Linear(512, 1024, bias=False, fuse_grad_accum=True, device=dev, dtype=torch.bfloat16)
x = torch.randn(256, 512, device=dev, dtype=torch.bfloat16, requires_grad=True)

# 预填一个非零 .grad，模拟梯度累加的第 2 步
lin.weight.grad = torch.randn_like(lin.weight)
grad_init = lin.weight.grad.clone()
print("保存的 weight_og 引用是否就是 Parameter:",
      any(lin.weight.data_ptr() == lin.weight.data_ptr()))  # 占位确认引用关系

out = lin(x)
out.sum().backward()
# fuse_grad_accum 下：.grad 应当 = grad_init + 本步新梯度
print("累加成功（.grad 明显大于单步新梯度）:", lin.weight.grad.abs().mean().item() > 0)
```

3. 对照参考：把同一模型建一份 `fuse_grad_accum=False` 的副本，先 `.backward()` 一次拿到「单步新梯度」`new_grad`，再验证 `lin.weight.grad ≈ grad_init + new_grad`（参考测试 `tests/test_mlp.py::test_mlp_concat_layout_fuse_grad_accum` 的断言写法）。
4. 兼容性验证：用 `torch.compile(lin)` 包一层再反向，观察它**不会报错**——因为 `torch.compiler.is_compiling()` 命中，走了普通路径。可临时在该分支加一行 `print`（或用 `cute.printf` 的等价 Python 打印）确认。

**需要观察的现象**：

- 开启 `fuse_grad_accum` 且预填了 `.grad` 时，反向后 `.grad` 是「预填值 + 新梯度」之和；
- 关闭时，`.grad` 被新梯度直接覆盖；
- `torch.compile` 下两种模式都正确，但开启时不会真正走就地累加（被 `is_compiling()` 拦下）。

**预期结果**：`lin.weight.grad - (grad_init + new_grad)` 的最大绝对值在 bf16 量级误差内（参考测试用 `1e-2 * w1_expected.abs().mean()` 作 atol）。

> 若无 GPU，本实践为「待本地验证」；可先在 CPU 上读源码、画三步走的数据流图。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `weight_og.grad = None` 这行删掉，开启 `fuse_grad_accum`、预填 `.grad=5`、本步真实新梯度应为 `3`，反向后 `.grad` 会是多少？为什么？

**答案**：会是 `13`（即 `(5+3) + (5+3)`）。因为第 1 步 `gemm_add_inplace` 把 `.grad` 改成 `5+3=8`，返回的 `dweight` 也是这个 `8`；由于 `.grad` 没被置空，PyTorch 的自动累加再做 `.grad += 8` 得 `16`……更准确地说，`accumulate_grad` 的行为依赖 PyTorch 版本，但核心问题是「旧值被计入两次」。置空正是为了消除这次重复。

**练习 2**：为什么反向的 dx/dw 都设了 `dynamic_scheduler=True`，而前向没有？

**答案**：前向 `x@W.T` 通常是 `(大M, in)@(in, out)` 的「规整」形状，静态光栅化已足够；反向 dW 是 `(out, M)@...` 形状多变（M 随 batch/序列长度变化），容易出现某些 SM 分不到 tile 的情况，动态调度（CLC 工作偷取）能更好地负载均衡。

**练习 3**：`linear_fwd_postprocess` 在 `not needs_weight_grad` 时会把 `x` 置 None。这能省多少显存？

**答案**：省下整张输入 `x`（`(M, in_features)`）。在仅做推理或仅需求输入梯度不需求权重梯度时，避免无谓地保存反向张量是省显存的常规手段。

---

### 4.2 MLP：门控融合与激活重算

#### 4.2.1 概念说明

标准 MLP 是两层线性层夹一个激活：

\[
\text{out} = \text{act}(x W_1^{\top}) \cdot W_2^{\top}
\]

而现代大模型几乎都用**门控 MLP**（gated MLP）：

\[
\text{out} = \text{gate\_fn}(\,x W_{\text{gate}}^{\top},\; x W_{\text{up}}^{\top}\,) \cdot W_2^{\top}
\]

其中 `gate_fn` 把「门控（gate）」与「上调（up）」两路结合，典型如 SwiGLU：

\[
\text{swiglu}(g, u) = \text{silu}(g) \cdot u = g\cdot\sigma(g)\cdot u
\]

实现上，\(W_1\) 通常把 gate 和 up 拼成一个 `2*hidden` 的输出：`fc1` 输出 `(M, 2*hidden)`，再拆成 gate/up 两半做门控，得到 `(M, hidden)`，最后 `fc2` 投影回 `out_features`。QuACK 的 `gate_fn_map` 列出了所有支持的门控激活（swiglu/reglu/geglu/glu 等）：

[quack/activation.py:1007-1015](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/activation.py#L1007-L1015) —— 判断一个激活名是否「门控」的真值表。

这一层有三种 QuACK 特有的优化：

1. **门控融合**：`gemm_gated` 把「`x@W1.T`（产出 2*hidden）+ 拆 gate/up + 门控激活」压成**一次 GEMM**，直接输出 `(M, hidden)` 的 postact。省掉了中间 `(M, 2*hidden)` 的物化与一个独立激活 kernel。
2. **激活重算（recompute）**：`MLPRecomputeFunc` 反向时**不保存 preact**，而是用一次额外 GEMM 重算它，用算力换显存。
3. **concat_layout**：把 gate/up 权重交错存储，让融合读取更友好（也与 Muon 优化器的 reshape 配套）。

#### 4.2.2 核心流程

非 recompute 路径（`mlp_func` 里 `recompute=False`）就是两次 `linear_*_func` 串联：

```
fc1_fn = linear_gated_func if 门控 else linear_act_func
fc2_fn = gated_linear_func  if 门控 else act_linear_func
preact, postact = fc1_fn(x, W1, activation, store_preact=需反向, ...)   # 融合门控 GEMM
out = fc2_fn(preact, W2, postact, activation, ...)                       # 第二个 GEMM
```

recompute 路径（`MLPRecomputeFunc`）的取舍：

```
前向:
    _preact, postact = gemm_gated/act(x, W1.T, activation)   # 只是为了拿到 postact
    out = gemm(postact, W2.T)
    save_for_backward(x, W1, W2, ...)                          # 注意：不保存 preact！

反向:
    preact = gemm(x, W1.T)                                     # 重算 preact（多一次 GEMM）
    dpreact, postact = gemm_dgated/dact(dout, W2, preact, act) # 融合反向激活 + 重算 postact
    dW2 = dout.T @ postact
    dx  = dpreact @ W1
    dW1 = dpreact.T @ x
```

**省了什么**：前向不必保存 `(M, 2*hidden)` 的 preact（对大 hidden 是 `batch * 2 * hidden * dtype` 字节）。

**花了什么**：反向多一次 `x @ W1.T` 的 GEMM。

#### 4.2.3 源码精读

先看 recompute 的前向，注意它**故意只存 x 和权重，不存 preact**：

[quack/mlp.py:110-137](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/mlp.py#L110-L137) —— 前向计算 postact 与 out，但 `save_for_backward` 只保存 `saved_x/saved_w1/saved_w2`（按需置 None）。

关键片段：

```python
_preact, postact = ops.matmul_fwd_act(x_flat, weight1.T, activation=activation)  # 拿 postact
out = ops.matmul_fwd(postact, weight2.T)
# Save only x and weights — no preact (the whole point of recompute)
ctx.save_for_backward(saved_x, saved_w1, saved_w2,
                      weight1_og if fuse_grad_accum else None,
                      weight2_og if fuse_grad_accum else None)
```

反向里那行注释点明了「重算」的本质：

[quack/mlp.py:139-164](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/mlp.py#L139-L164) —— 反向重算 preact，再用 `gemm_dgated`/`gemm_dact` 一次性拿到 dpreact 与重算的 postact。

```python
# Recompute preact = x @ W1.T (the extra matmul we trade for memory)
preact = recompute_fwd(x_flat, weight1.T)
# gemm_dact computes: dpreact = d_act(dout @ W2, preact) AND recomputes postact
dpreact, postact = ops.matmul_bwd_dact(dout, weight2, preact, activation=ctx.activation)
```

注意 `gemm_dgated`/`gemm_dact` 一次返回**两个**东西：对 preact 的梯度 `dpreact`，以及顺带重算的 `postact`（后者用于算 dW2）。这样 dW2 不必再单独算一次激活的前向。

`MLPRecomputeFunc` 的梯度累加逻辑抽到了模块级函数 `_compute_weight_grad`，与 Linear 的 `linear_bwd_compute_weight_grad` 完全同构——同样的三步走、同样的 `torch.compile` 回退：

[quack/mlp.py:196-206](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/mlp.py#L196-L206) —— MLP 版的 fuse_grad_accum，逻辑与 [quack/linear.py:48-62](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/linear.py#L48-L62) 一致。

派发器 `mlp_func` 根据 `gated` / `recompute` / `concat_layout` / `tuned` 四个开关选 ops 包并调用：

[quack/mlp.py:209-253](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/mlp.py#L209-L253) —— `mlp_func`：门控与否决定 fc1/fc2 用哪组函数；`recompute` 决定走 `MLPRecomputeFunc` 还是两次 `linear_*_func` 串联。

最后看用户接口 `MLP(nn.Module)`。门控时 `fc1` 的输出维度翻倍（gate+up 拼接），`fc2` 吃 `hidden_features`：

[quack/mlp.py:283-296](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/mlp.py#L283-L296) —— 门控 MLP 的 `fc1_out = 2 * hidden_features`；concat_layout 下还会给权重挂上 `_muon_reshape_functions`（供 Muon 优化器在「合并视图」与「拆分 gate/up 视图」间转换）。

[quack/mlp.py:302-335](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/mlp.py#L302-L335) —— `MLP.forward`：满足对齐与「无偏置（训练时）」等条件才走融合路径，否则用 `nn.Linear` + PyTorch 激活回退（回退用的 `gated_to_pytorch_fn_map` 见 [quack/gemm_interface.py:94-102](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_interface.py#L94-L102)）。

> 关于 `concat_layout`：它把 gate/up 在权重里交错存放，让 `gemm_gated` 能按交错顺序免拷贝读取（回顾 u4-l3 提到的 `concat_layout` 让拼接权重免拷贝）。同时它要求反向的 dx/dW1/dW1_inplace 用不同的 concat 标签，见 [quack/mlp.py:78-95](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/mlp.py#L78-L95)。

#### 4.2.4 代码实践

**实践目标**：量化对比 recompute 开/关时的显存与反向额外 GEMM。

**操作步骤**：

1. 读 [quack/mlp.py:110-137](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/mlp.py#L110-L137) 与 [quack/mlp.py:139-164](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/mlp.py#L139-L164)，列出两种模式 `save_for_backward` 的差异。
2. 运行项目自带测试里的 recompute 用例，观察它对数值正确性的断言：

```bash
pytest tests/test_mlp.py::test_mlp_recompute -x -k "bfloat16 and swiglu"
```

3. **示例代码**（非项目自带）：用 `torch.cuda.max_memory_allocated` 对比两种模式峰值显存：

```python
import torch
from quack.mlp import MLP

def peak(fn):
    torch.cuda.reset_peak_memory_stats()
    fn()
    return torch.cuda.max_memory_allocated() / 1e6  # MB

dev, dt = "cuda", torch.bfloat16
cfg = dict(in_features=4096, hidden_features=8192, activation="swiglu",
           device=dev, dtype=dt, tuned=False)

for recompute in (False, True):
    mlp = MLP(recompute=recompute, **cfg)
    x = torch.randn(8192, 4096, device=dev, dtype=dt, requires_grad=True)
    def run():
        out = mlp(x); out.sum().backward()
    mb = peak(run)
    print(f"recompute={recompute}: peak {mb:.1f} MB")
```

**需要观察的现象**：

- `recompute=True` 峰值显存更低（省下了 `(M, 2*hidden)` 的 preact）；
- 反向多触发一次 `x @ W1.T` 的 GEMM（可用 `torch.profiler` 看内核计数）；
- 两种模式数值结果在 bf16 容差内一致（测试已断言）。

**预期结果**：recompute 省下的显存约等于 `batch * 2 * hidden * 2` 字节（bf16）；数值上 dx/dW1/dW2 与非 recompute 一致。**待本地验证**（无 GPU 时仅做源码阅读）。

#### 4.2.5 小练习与答案

**练习 1**：`MLPRecomputeFunc.backward` 里，当**只需要 dW2、不需要 dx** 时，代码走的是哪条分支？为什么这样省？

**答案**：走 [quack/mlp.py:158-162](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/mlp.py#L158-L162) 的 `elif any_grad` 分支：它重算 preact 后用 `ops.recompute_postact(preact, activation)` **便宜地**（无 GEMM）重算 postact，而不是调用较重的 `gemm_dgated`。因为 dW2 只需要 postact，不需要 dpreact，没必要走融合反向激活的 GEMM。

**练习 2**：门控 MLP 的 `fc1_out = 2 * hidden_features`，为什么不直接用两个独立的 `nn.Linear` 各算 gate 和 up？

**答案**：合成一个 `2*hidden` 输出的 GEMM 比 two 个 `hidden` GEMM 更高效——更大的 N 维让单个 MMA tile 更饱满、launch 开销减半；而且 `gemm_gated` 能在 epilogue 里直接做拆分+门控激活，省掉中间 `(M, 2*hidden)` 的显存往返。

**练习 3**：`concat_layout` 下 `MLP.__init__` 给 `fc1.weight` 挂了 `_muon_reshape_functions`。它的两个 lambda 各做什么？

**答案**：见 [quack/mlp.py:286-295](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/mlp.py#L286-L295)。一个是 `(two d) e -> two d e`（把合并的权重拆成 gate/up 两路给优化器），另一个是逆变换 `two d e -> (two d) e`（优化后合并回内核期望的布局）。concat 与非 concat 的拆分轴不同（`d two` vs `two d`），故挂不同函数。

---

### 4.3 Fused Linear-Cross-Entropy：分块省显存

#### 4.3.1 概念说明

语言模型的最后一层是「线性投影到词表 + 交叉熵」：

\[
\text{logits} = x W^{\top} \quad(\text{形状 } (B\cdot L,\; V)),\qquad
\text{loss} = \text{CrossEntropy}(\text{logits},\; \text{target})
\]

痛点在于词表 \(V\) 极大（Llama ~128k、GPT-4 类 ~200k）。一次物化整张 logits `(序列长, V)` 既费显存又费带宽。朴素实现就是这么做的：

[quack/linear_cross_entropy.py:24-36](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/linear_cross_entropy.py#L24-L36) —— 朴素参考：`F.linear` 物化全量 logits，再算交叉熵。

QuACK 的融合思路是**分块（chunked）**：沿 batch 维把 `(B·L, d)` 切成长度 `chunk_size` 的小块，每块单独算「logits → loss + dlogits → dx + dw」，于是**峰值显存从 `(B·L, V)` 降到 `(chunk_size, V)`**。同时把 `dw` 的跨块累加用 fp32 累加器保证精度。

进阶版还有一条 **scaled-exp 流水线**（仅 SM90 + bf16 + V 整除约束），用「每 (行, n-tile) 的 2 的幂次偏移」彻底不物化 dlogits，进一步省带宽——它依赖 u6-l5 讲过的 `scaled_exp_target_epi` 领域 epilogue，本讲只点出它的存在与自动启用条件，不展开。

#### 4.3.2 核心流程

分块前向 `chunked_linear_cross_entropy_fwd` 对每一块做（核心循环）：

```
对每个 chunk (x_chunk, target_chunk):
    logits_chunk = x_chunk @ weight.T            # (chunk, V)，物化但只一块
    dlogits_chunk = logits_chunk                  # 复用同一块显存！
    cross_entropy_fwd_out(logits_chunk, target, dx=dlogits_chunk)  # 就地算 loss + dlogits
    dx_chunk = dlogits_chunk @ weight             # 反向对 x 的梯度
    # 累加 dW（除最后一块）:
    若 第0块:   dw = dlogits_chunk.T @ x_chunk            # gemm
    若 中间块:  dw += dlogits_chunk.T @ x_chunk           # gemm_add_inplace（fp32 累加）
    若 最后块:  保存 last_dlogits_chunk, last_x_chunk     # 推迟到反向
```

反向 `ChunkedLinearCrossEntropyFunction.backward` 只需：

```
dx, dw *= dloss                                 # 上游梯度是标量，缩放预算好的梯度
# 补上最后一块的 dw，并把 dloss 缩放 + dtype 下转一起 fold 进这一次 GEMM:
若 多块且需下转:  dw = gemm_add(last.T, last_x, dw, alpha=dloss, beta=dloss, out_dtype=weight_dtype)
```

两个关键设计：

1. **logits 与 dlogits 复用同一块显存**：`dlogits_chunk = logits_chunk`，`cross_entropy_fwd_out` 把 softmax 反向的 dlogits 就地写回 logits 的内存，省一倍峰值显存。
2. **最后一块 dw 推迟到反向**：这样反向既能用 `alpha=dloss` 把标量缩放 fold 进 GEMM，又能用 `out_dtype=weight_dtype` 把 fp32 累加器一次性下转到权重精度——避免对整张 `(V, d)` 的 dw 单独做一次缩放+类型转换 kernel。

#### 4.3.3 源码精读

看分块前向的核心循环：

[quack/linear_cross_entropy.py:51-131](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/linear_cross_entropy.py#L51-L131) —— `chunked_linear_cross_entropy_fwd`：切块、逐块算 logits/loss/dlogits/dx/dw，最后一块 dw 延迟。

逐块处理（精简）：

```python
for i, (x_chunk, target_chunk, loss_chunk) in enumerate(zip(*(t.split(chunk_size) for t in (x, target, loss)))):
    logits_chunk = logits_chunk_preallocated[:chunk_len]      # 复用预分配显存
    torch.mm(x_chunk, weight.mT, out=logits_chunk)
    dlogits_chunk = logits_chunk if need_dx or need_dw else None  # 就地复用
    cross_entropy_fwd_out(logits_chunk, target_chunk, loss=loss_chunk, dx=dlogits_chunk, ...)
    if need_dx:
        torch.mm(dlogits_chunk, weight, out=dx[start:start+chunk_len])
    if not need_dw: continue
    if i == num_chunks - 1:        # 最后一块：留给反向
        last_dlogits_chunk, last_x_chunk = dlogits_chunk, x_chunk
    elif i == 0:                    # 第一块：直接赋值
        gemm(dlogits_chunk.T, x_chunk, out=dw, tuned=tuned)
    else:                           # 中间块：fp32 就地累加
        gemm_add_inplace(dlogits_chunk.T, x_chunk, dw, tuned=tuned)
```

注意 `dw` 是 **fp32**（[L91](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/linear_cross_entropy.py#L91)）：`torch.empty_like(weight, dtype=torch.float32)`。每个块的 GEMM 都以 fp32 写入/累加进它，精度只在反向那次单次下转时损失一次。

`cross_entropy_fwd_out` 来自 `quack.cross_entropy`（u2 家族的归约内核），它在一个内核里同时算出 loss 和 dlogits（softmax 反向 dx = y*(dy − dot) 的批量版），并把 dlogits 就地写回 logits 的显存——这就是「logits/dlogits 复用」得以成立的内核保证。

再看反向如何收尾：

[quack/linear_cross_entropy.py:175-221](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/linear_cross_entropy.py#L175-L221) —— 反向：缩放 dx/dw，并用一次 GEMM 把最后一块 dw + dloss 缩放 + 精度下转全部 fold 在一起。

```python
if dx is not None:
    dx.mul_(dloss)                              # 标量缩放
if last_dlogits_chunk is None:
    pass                                        # 权重不需求梯度
elif dw is None:                                # 只有一块：直接算（带缩放+下转）
    dw = gemm(last_dlogits_chunk.T, last_x_chunk, out_dtype=ctx.weight_dtype, alpha=dloss, tuned=tuned)
else:                                           # 多块：补最后一块
    if ctx.weight_dtype == dw.dtype:
        gemm_add_inplace(last_dlogits_chunk.T, last_x_chunk, dw, alpha=dloss, beta=dloss, tuned=tuned)
    else:                                       # fp32→bf16 下转：用 gemm_add 一次性转
        dw = gemm_add(last_dlogits_chunk.T, last_x_chunk, dw, alpha=dloss, beta=dloss,
                      out_dtype=ctx.weight_dtype, tuned=tuned)
```

`gemm_add` 与 `gemm_add_inplace` 的区别在于前者**返回新张量**（可顺带 `out_dtype` 下转），后者**就地写回**（dtype 不变）——见 [quack/gemm_interface.py:1562-1582](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_interface.py#L1562-L1582) 与 [quack/gemm_interface.py:1934-1952](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_interface.py#L1934-L1952)。反向据此二选一：当权重是 bf16 而 dw 是 fp32 时，用 `gemm_add` 把 fp32 的 dw 当作 C 读入、写出 bf16，**省掉一次整张 dw 的显式下转 kernel**。

**进阶 scaled-exp 流水线**（点到为止）：当 `scaled_exp_lce_supported` 返回 True（SM90、bf16、`V % 128 == 0`、`chunk_size % 128 == 0` 等）时，`chunked_linear_cross_entropy` 自动改走 `scaled_exp_linear_cross_entropy`，它用 `scaled_exp_target_epi` 的 GEMM 把 dlogits 替换成「按 (行, n-tile) 的 2 的幂次偏移」，配合一个 Triton 「胶水」核与两条 strip GEMM（带 `@a_transform` 的逐块缩放），在不物化 dlogits 的前提下完成 dx/dw。完整设计见源码顶部 [quack/linear_cross_entropy.py:224-247](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/linear_cross_entropy.py#L224-L247) 的整段注释，以及 u6-l5。整个 scaled-exp 前向还被包成一个 custom op `quack::lce_scaled_exp_fwd`（[quack/linear_cross_entropy.py:618-645](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/linear_cross_entropy.py#L618-L645)），这样 `torch.compile` 只记录一个图节点，而不会去 trace 主机侧的块循环。

用户接口：`chunked_linear_cross_entropy` 按 `use_scaled_exp`（None=自动）派发；`LinearCrossEntropy(nn.Linear)` 在满足对齐与 reduction 约束时走分块路径，否则回退朴素实现：

[quack/linear_cross_entropy.py:756-813](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/linear_cross_entropy.py#L756-L813) —— 公共派发器，含 scaled-exp 自动选择与「仅 loss」推理快路径。

[quack/linear_cross_entropy.py:839-870](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/linear_cross_entropy.py#L839-L870) —— `LinearCrossEntropy.forward` 的条件派发。

#### 4.3.4 代码实践

**实践目标**：对比「朴素（物化全量 logits）」与「分块」的峰值显存，并验证分块的 dx/dw 数值正确。

**操作步骤**：

1. 读 [quack/linear_cross_entropy.py:51-131](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/linear_cross_entropy.py#L51-L131)，确认 logits/dlogits 复用与最后一块延迟两处设计。
2. 运行项目自带测试（小词表、小 chunk，快速验证数值）：

```bash
pytest tests/test_linear_cross_entropy.py::test_chunked_linear_cross_entropy -x -k "mean and 32000"
```

断言写法见 [tests/test_linear_cross_entropy.py:39-54](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/tests/test_linear_cross_entropy.py#L39-L54)：loss/dx/dw 都与 fp32 参考做「同 dtype 基线相对容差」比较。
3. **示例代码**（非项目自带）：用大词表对比朴素 vs 分块的峰值显存：

```python
import torch
from quack.linear_cross_entropy import linear_cross_entropy_func, chunked_linear_cross_entropy

dev, dt = "cuda", torch.bfloat16
B_L, d, V = 4096, 4096, 128256
x = torch.randn(B_L, d, device=dev, dtype=dt, requires_grad=True)
w = torch.randn(V, d, device=dev, dtype=dt, requires_grad=True)
tgt = torch.randint(0, V, (B_L,), device=dev)

def peak(fn):
    torch.cuda.reset_peak_memory_stats(); fn()
    return torch.cuda.max_memory_allocated() / 1e9  # GB

# 朴素：物化 (4096, 128256) logits
xp, wp = x.detach().requires_grad_(), w.detach().requires_grad_()
mb_naive = peak(lambda: linear_cross_entropy_func(xp, wp, None, tgt).backward())

# 分块：峰值约 (chunk_size, V)
xp, wp = x.detach().requires_grad_(), w.detach().requires_grad_()
mb_chunk = peak(lambda: chunked_linear_cross_entropy(xp, wp, tgt, chunk_size=2048, tuned=False).backward())
print(f"naive {mb_naive:.2f} GB vs chunked {mb_chunk:.2f} GB")
```

**需要观察的现象**：

- 分块峰值显存显著低于朴素（朴素要装下 `(B_L, V)`，分块只装 `(chunk_size, V)`）；
- dx/dw 数值与朴素参考在 bf16 容差内一致；
- `chunk_size` 越小越省显存，但 GEMM 越碎、开销越大（存在一个甜点）。

**预期结果**：朴素峰值随 `B_L·V` 线性增长；分块峰值主要由 `chunk_size·V` 决定，与 `B_L` 近似无关。**待本地验证**（无 GPU 时仅做源码阅读 + 画显存对比示意）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `dw` 累加器要强制用 fp32，而不是直接用权重的 bf16？

**答案**：见 [L66-L68](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/linear_cross_entropy.py#L66-L68) 注释。每个块的 GEMM 都会写入/累加进 `dw`，若用 bf16，多次累加的舍入误差会逐块累积、显著损失精度。fp32 累加器把精度损失集中到反向那次**单次**下转，整体精度高得多。

**练习 2**：最后一块的 dw 为什么不直接在前向算掉，而要留到反向？

**答案**：反向需要把上游梯度 `dloss`（标量）乘进 dw。若前向把所有块都算完，反向就要对整张 `(V, d)` 的 dw 单独做一次「乘 dloss + 下转 dtype」的 kernel。把最后一块留到反向，可以用 `gemm_add(..., alpha=dloss, beta=dloss, out_dtype=weight_dtype)` 把「补最后一块 + 标量缩放 + fp32→bf16 下转」**三件事 fold 进一次 GEMM**，省掉那次额外的全量 dw 处理。

**练习 3**：`need_dx=False, need_dw=False`（纯推理、只要 loss）时，前向会跳过哪些步骤？

**答案**：见 [L103](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/linear_cross_entropy.py#L103) 与公共派发器 [L793-L809](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/linear_cross_entropy.py#L793-L809)。`dlogits_chunk` 置 None（连 dlogits 都不算）、跳过 dx 的 `mm`、跳过所有 dw 累加，连 fp32 `(V,d)` 累加器都不分配。纯 loss 路径开销最小。

---

## 5. 综合实践

把三个模块串起来：用 QuACK 的 `MLP` + `LinearCrossEntropy` 搭一个「最小语言模型头部」，对比「全用 PyTorch 原生」与「全用 QuACK 融合」在**峰值显存**与**前向+反向 kernel 数**上的差异。

**任务**：

1. 构造输入 `x: (B*L, d)`（如 `(4096, 4096)` bf16），权重 `W_ce: (V, d)`（`V=128256`）。
2. 路径 A（原生）：`nn.Linear` → SwiGLU（手写 `F.silu(gate)*up`）→ `nn.Linear` → `F.linear` → `F.cross_entropy`。
3. 路径 B（QuACK）：`MLP(activation="swiglu")` → `LinearCrossEntropy(chunk_size=2048)`。
4. 两条路径都开 `fuse_grad_accum=True` 并预填 `.grad`，做一次前向+反向。
5. 用 `torch.cuda.max_memory_allocated` 与 `torch.profiler` 比较：
   - 峰值显存（重点关注 logits 那一层：路径 A 物化 `(B*L, V)`，路径 B 只物化 `(2048, V)`）；
   - 反向 GEMM 数量（路径 B 的 recompute 会多一次，但省下 preact 显存）。

**观察要点**：

- QuACK 路径在 logits 层应显著省显存（这是 fused CE 的主战场）；
- MLP 层若开 recompute，preact 显存也省，但反向多一次 GEMM；
- `fuse_grad_accum` 在两条路径的梯度累加里都生效，但一旦套 `torch.compile` 会自动退回普通路径——可顺便验证这一点。

**预期结论**：融合路径以「更少的 kernel、更低的峰值显存」得到数值等价（bf16 容差内）的结果。**完整运行结果待本地验证**。

> 提示：若想进一步榨干性能，可在 SM90 + bf16 + `V % 128 == 0` 时让 `LinearCrossEntropy` 自动启用 scaled-exp 流水线（用 `use_scaled_exp=None`），并用 `pytest tests/test_linear_cross_entropy.py::test_scaled_exp_linear_cross_entropy` 验证其数值。

---

## 6. 本讲小结

- **Linear 的梯度累加融合**：`fuse_grad_accum` 用 `gemm_add_inplace` 把「算 dW」与「累加进 `.grad`」合并成一次 GEMM（靠 epilogue 的 `β·C` 项搭顺风车），并用 `weight_og.grad = None` 防止 PyTorch 自动累加导致旧值被加两次；它**与 `torch.compile` 不兼容**（就地改写叶子 `.grad` + fake tensor 无法判断 `.grad` 状态），故编译期自动回退普通路径。
- **配置包机制**：每个变体用一个 `_XxxOps` 类把所需的几个 `partial(gemm, ...)` 打包，作为非张量参数传给 `autograd.Function.apply`，避免函数体里写满 if/else；Linear 与 MLP 共享同一套 grad accum 逻辑（`linear_bwd_compute_weight_grad` ≡ `_compute_weight_grad`）。
- **门控 MLP 融合**：`gemm_gated` 把「`x@W1.T` 产 2·hidden + 拆 gate/up + 门控激活」压成一次 GEMM；`MLPRecomputeFunc` 反向不存 preact、改用一次额外 GEMM 重算，用算力换显存。
- **concat_layout**：gate/up 交错存储，让融合读取免拷贝，并配套 Muon 优化器的 reshape 函数。
- **Fused Linear-Cross-Entropy 的核心是分块**：沿 batch 维切块，峰值显存从 `(B·L, V)` 降到 `(chunk_size, V)`；logits 与 dlogits 复用同一块显存；`dw` 用 fp32 累加器跨块累加，最后一块推迟到反向，把「补尾块 + 标量缩放 + 精度下转」fold 进一次 GEMM。
- **scaled-exp 流水线**是更激进的进阶路径（仅 SM90+bf16+V 整除），不物化 dlogits，依赖 u6-l5 的 `scaled_exp_target_epi` 领域 epilogue 与 Triton 胶水核，被包成单个 custom op 以兼容 `torch.compile`。

---

## 7. 下一步学习建议

- **回到内核侧**：本讲全是「主机侧胶水」。若想知道 `gemm_add_inplace` 的 `β·C` 累加在设备侧如何实现，复习 **u6-l3（默认线性 epilogue）** 的 `apply_linear_epilogue` 与 `add_to_output`。
- **scaled-exp 深挖**：若对 fused CE 的进阶流水线感兴趣，结合 **u6-l5（领域 epilogue）** 的 `scaled_exp` / `lse_target_epi` 与 **u7-l3（A 算子变换）** 的 `@a_transform` 阅读 [quack/linear_cross_entropy.py:224-615](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/linear_cross_entropy.py#L224-L615)。
- **autograd 集成原理**：想彻底搞清 `cute_op` 自定义算子如何与 `torch.autograd.Function` 协作，回顾 **u2-l6（cute_op 与 jit 缓存）**。
- **测试方法**：本讲多处引用了 `tests/test_linear.py`、`tests/test_mlp.py`、`tests/test_linear_cross_entropy.py` 的断言写法，下一讲 **u8-l5（测试方法与基准协议）** 会系统讲解「数值正确性参考实现 + dtype 相关容差」的测试范式。
- **动手方向**：尝试给 `LinearActFunc` 加一种新的激活（参照 `act_to_pytorch_fn_map` 与 `activation.py` 里的 `dxxx` 反向函数），跑通 `tests/test_linear.py` 的参数化矩阵——这是检验你是否真正理解「配置包 + autograd Function」机制的最佳练习。
