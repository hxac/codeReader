# Softmax 反向与 autograd 集成

## 1. 本讲目标

学完本讲，你应当能够：

- 推导并默写出 softmax 反向的解析公式 \(\mathrm{d}x_i = y_i(\mathrm{d}y_i - \mathrm{dot})\)，并理解它为何只需要**一次**行内归约。
- 逐段读懂 `SoftmaxBackward` 设备内核：双输入（\(\mathrm{d}y\) 与 \(y\)）加载、`dot` 归约、梯度计算与回写。
- 看懂 `SoftmaxFunction` 如何用 `torch.autograd.Function` 把前向/反向内核接入 PyTorch 自动微分。
- 解释测试里那句 `torch.cuda.synchronize()  # without sync, torch.autograd gets wrong results` 背后的原因。
- 画出从公开 API `softmax(x)` 到设备内核的完整调用链。

## 2. 前置知识

本讲建立在前一讲（[u2-l2](u2-l2-softmax-fwd.md) Softmax 前向内核）之上，假设你已经了解：

- `ReductionBase` 的「模板方法 + 钩子」骨架，以及 `stage` / `reduction_dtype` / `cluster_n` / `tiler_mn` 的含义（见 [u2-l1](u2-l1-reduction-base.md)）。
- 一个 CuTe-DSL 归约内核的标准数据流 `gmem → smem → rmem（寄存器）→ 归约 → gmem`。
- `@cute.jit`（主机侧编排）与 `@cute.kernel`（设备侧并行内核）的分工，以及 `const_expr` 标记编译期分支的作用。

此外需要一点 PyTorch 自动微分基础：

- **前向（forward）**：给定输入算出输出，并把反向需要用到的中间量「存起来」。
- **反向（backward）**：给定 loss 对**输出**的梯度 \(\mathrm{d}y\)，利用存好的中间量算出 loss 对**输入**的梯度 \(\mathrm{d}x\)。
- `torch.autograd.Function` 是 PyTorch 提供的自定义可微算子基类：子类实现 `forward(ctx, ...)` 和 `backward(ctx, ...)`，用 `ctx.save_for_backward(...)` 存张量、用 `ctx.saved_tensors` 取回。

> 本讲用到的术语：**上游梯度** \(\mathrm{d}y\)（loss 对 softmax 输出的梯度）、**下游梯度** \(\mathrm{d}x\)（loss 对 softmax 输入的梯度）、**dot**（\(\sum_j \mathrm{d}y_j \cdot y_j\)，行内点积）。

## 3. 本讲源码地图

本讲几乎全部内容集中在单个文件里，这是 QuACK 归约家族自包含特性的体现。

| 文件 | 作用 |
| --- | --- |
| [quack/softmax.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/softmax.py) | `Softmax`（前向）、`SoftmaxBackward`（反向）、`SoftmaxFunction`（autograd 包装）、`softmax`（公开 API）全部在此 |
| [quack/reduction_base.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/reduction_base.py) | `ReductionBase` 共享基类，提供 `_get_tiled_copy`、归约缓冲与 mbarrier 分配 |
| [quack/reduce.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/reduce.py) | `row_reduce` 行内归约原语，反向内核用它算 `dot` |
| [tests/test_softmax.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/tests/test_softmax.py) | 数值正确性测试，含「sync 后再取 grad」的关键注释 |

## 4. 核心概念与源码讲解

### 4.1 SoftmaxBackward 内核

#### 4.1.1 概念说明

softmax 把向量 \(x\) 映射成 \(y\)：

\[
y_i = \frac{e^{x_i}}{\sum_j e^{x_j}}
\]

反向的任务是：已知 loss 对输出 \(y\) 的梯度 \(\mathrm{d}y_i = \partial L/\partial y_i\)，求 loss 对输入 \(x\) 的梯度 \(\mathrm{d}x_j = \partial L/\partial x_j\)。

先求雅可比矩阵。对 \(y_i\) 关于 \(x_j\) 求导（分两种情况）：

\[
\frac{\partial y_i}{\partial x_j} = y_i(\delta_{ij} - y_j)
\]

其中 \(\delta_{ij}\) 是 Kronecker delta（\(i=j\) 时为 1，否则为 0）。用链式法则把 loss 对 \(y\) 的梯度传回 \(x\)：

\[
\mathrm{d}x_j = \sum_i \mathrm{d}y_i \cdot \frac{\partial y_i}{\partial x_j} = \sum_i \mathrm{d}y_i \, y_i(\delta_{ij} - y_j)
\]

把求和拆开：

\[
\mathrm{d}x_j = y_j \mathrm{d}y_j - y_j \underbrace{\sum_i \mathrm{d}y_i \, y_i}_{\text{记作 } \mathrm{dot}} = y_j(\mathrm{d}y_j - \mathrm{dot})
\]

这就得到了反向的核心公式（把下标换回 \(i\)）：

\[
\boxed{\quad \mathrm{dot} = \sum_j \mathrm{d}y_j \cdot y_j, \qquad \mathrm{d}x_i = y_i \cdot (\mathrm{d}y_i - \mathrm{dot}) \quad}
\]

**两个关键观察：**

1. **只需要一次行内归约**：整个反向只需求一个标量 `dot`（每行一个），其余都是逐元素运算。这与前向截然不同——前向（非 online）需要 `max` 和 `sum` 两次归约，online 版本需要把 `max` 与 `sum` 耦合归约。所以反向内核配置为 `stage=1`。
2. **只需要保存 \(y\)，不需要保存 \(x\)**：反向公式里只出现 \(y\) 和 \(\mathrm{d}y\)，根本用不到原始输入 \(x\)。这决定了 `SoftmaxFunction` 会保存 softmax 输出 \(y\) 而非输入 \(x\)（见 4.2）。

#### 4.1.2 核心流程

反向内核沿用了前向的 `gmem → smem → rmem → 归约 → gmem` 五段式骨架，但有两处结构差异：**双输入加载**（同时加载 \(\mathrm{d}y\) 和 \(y\)）与**单次 dot 归约**。

```
对每一行（每行由若干线程 + 可选 cluster 协作）：
  1. gmem → smem（异步 cp.async）：同时把 dY 与 Y 两块加载进共享内存
  2. cp.async 等待完成；OOB 位置保持为 0（反向允许 0，无需填 -inf）
  3. smem → rmem：把 dY、Y 拷进寄存器，转成 Float32
  4. 逐元素乘：tmp = dy * y
  5. 行内归约：dot = row_reduce(tmp, ADD)   ← 唯一一次归约
  6. 逐元素算梯度：dx = y * (dy - dot)
  7. rmem → gmem：写回 dX
```

其中第 5 步 `row_reduce` 会先做 warp 内归约，再（若 `cluster_n > 1`）通过 mbarrier 跨 CTA 归约，最终让该行的每个线程都拿到同一个 `dot`。得到 `dot` 后，第 6 步是纯逐元素运算，每个线程用自己的 \(y_i\)、\(\mathrm{d}y_i\) 和广播来的 `dot` 算出自己负责的那些 \(\mathrm{d}x_i\)。

#### 4.1.3 源码精读

**(a) 构造：`stage=1`、归约类型 `Float32`**

[quack/softmax.py:216-219](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/softmax.py#L216-L219) 定义 `SoftmaxBackward`，把基类的 `stage` 设为 1（只算 `dot` 一次归约）、`reduction_dtype` 设为 `Float32`：

```python
class SoftmaxBackward(ReductionBase):
    def __init__(self, dtype: Type[cutlass.Numeric], N: int):
        # 1 stage for computing dot product
        super().__init__(dtype, N, stage=1, reduction_dtype=Float32)
```

对照前向 [quack/softmax.py:27-34](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/softmax.py#L27-L34)：前向 online 时 `stage=1` 但 `reduction_dtype=Int64`（把 max/sum 打包成一个 Int64），非 online 时 `stage=2`（max、sum 各占一槽）。反向永远只需一槽 `Float32`，因为 `dot` 是单个浮点标量。

> 这里的 `stage` 直接决定基类分配的归约缓冲槽数和 mbarrier 个数（见 [quack/reduction_base.py:70-85](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/reduction_base.py#L70-L85) 中 `num_slots=self.stage` 与 `allocate_array(Int64, num_elems=self.stage)`）。

**(b) 双 SMEM 张量分配**

反向要在共享内存里同时放 \(\mathrm{d}y\) 和 \(y\)，所以分配了**两个** smem 张量 [quack/softmax.py:297-304](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/softmax.py#L297-L304)：

```python
sdY = smem.allocate_tensor(mdY.element_type, cute.make_ordered_layout(tiler_mn, order=(1, 0)), ...)
sY  = smem.allocate_tensor(mY.element_type,  cute.make_ordered_layout(tiler_mn, order=(1, 0)), ...)
reduction_buffer, mbar_ptr = self._allocate_reduction_buffer_and_mbar(smem, tv_layout)
```

前向只分配一个 `sX`。双张量使反向的 SMEM 压力翻倍，这正是 `_set_cluster_n` 与 `_num_threads` 阈值更保守的原因（见下文 (d)）。

**(c) 双输入加载与 dot 归约**

线程分区后，反向**同时**发起两条异步拷贝 [quack/softmax.py:326-331](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/softmax.py#L326-L331)：

```python
if tXcX[0][0] < shape[0]:
    copy(tdYgdY, tdYsdY, is_async=True)
    copy(tYgY, tYsY, is_async=True)
cute.arch.cp_async_commit_group()
cute.arch.cp_async_wait_group(0)
# Don't need fill_oob since cp.async will automatically fills OOB elements with zeros
```

注意第 331 行的注释：**反向不调用 `fill_oob`**。前向在 [quack/softmax.py:137-139](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/softmax.py#L137-L139) 把越界（OOB）位置填成 \(-\infty\)，好让 `exp(-∞)=0`、softmax 概率为 0；而反向的 `dot = Σ dy·y` 中，OOB 位置理应贡献 0，且 OOB 处的 \(\mathrm{d}x\) 也应为 0，所以**填 0 才是数学正确的**。源码注释指出 cp.async 会对 OOB 位置自动填 0，从而省掉显式的 `fill_oob` 调用（具体硬件填充机制待本地验证，但「OOB 应为 0」这一点是确定的）。

随后 smem→rmem、转 `Float32`，进入唯一的归约与梯度计算 [quack/softmax.py:335-351](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/softmax.py#L335-L351)，这段几乎就是 4.1.1 公式的直译：

```python
dy = tdYrdY.load().to(cute.Float32)
y  = tYrY.load().to(cute.Float32)

# Compute dot product: dot = Σⱼ dy_j × y_j
dot = row_reduce(
    dy * y,
    cute.ReductionOp.ADD,
    threads_per_row,
    reduction_buffer[None, None, 0],
    mbar_ptr if const_expr(self.cluster_n > 1) else None,
    init_val=0.0,
    hook_fn=cute.arch.cluster_wait if const_expr(self.cluster_n > 1) else None,
)

# Compute gradient: dx_i = y_i × (dy_i - dot)
dx = y * (dy - dot)
```

`row_reduce`（[quack/reduce.py:202-267](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/reduce.py#L202-L267)）先对寄存器片段做 `TensorSSA.reduce(ADD)`，再 warp 内 `warp_reduce`，最后（当 `cluster_n>1`）通过 `cluster_wait` 钩子 + mbarrier 把各 CTA 的部分和合并成全行 `dot`。`dot` 一旦得到，第 350 行的逐元素减法与乘法即可算出每个线程持有的 \(\mathrm{d}x_i\)。

**(d) 比前向更保守的资源阈值**

因为反向持有两块 SMEM，`SoftmaxBackward` 重写了两个方法使资源占用更克制：

- [_num_threads:250-251](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/softmax.py#L250-L251)：在 `N <= 8192` 时用 128 线程，否则 256；而基类默认阈值是 16384（[reduction_base.py:22-23](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/reduction_base.py#L22-L23)）。反向更早地切到 256 线程。
- [_set_cluster_n:237-239](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/softmax.py#L237-L239)：SM12x（RTX 50，99 KB SMEM）上 fp32 反向的 cluster 阈值从 8K 起步（前向 fp32 从 16K 起步），注释明确写道「fp32 bwd has 2 SMEM tensors, needs tighter clustering」。

**(e) `compile` 静态方法**

[quack/softmax.py:355-370](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/softmax.py#L355-L370) 用符号 batch 维 `cute.sym_int()` 构造三个 fake 张量（\(\mathrm{d}y\)、\(y\)、\(\mathrm{d}x\)），编译出对**任意 batch** 复用的内核产物（`@jit_cache` 会按源码指纹缓存 `.o`，详见 [u2-l6](u2-l6-cute-op-and-jit-cache.md)）。

#### 4.1.4 代码实践

**目标**：用源码阅读 + 手算的方式验证反向内核算出的 `dot` 与 `dx` 是否符合 4.1.1 的公式。

**步骤**：

1. 打开 [quack/softmax.py:338-351](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/softmax.py#L338-L351)，在脑中把 `dy * y`、`row_reduce(...)`、`y * (dy - dot)` 三步分别对应到公式里的 \(\mathrm{d}y_j y_j\)、\(\mathrm{dot}\)、\(\mathrm{d}x_i\)。
2. 取一行 4 个元素的手算样本：令 \(y = [0.1, 0.2, 0.3, 0.4]\)，\(\mathrm{d}y = [1, 0, 0, 0]\)。
   - 手算 \(\mathrm{dot} = 1\cdot0.1 + 0\cdot0.2 + 0\cdot0.3 + 0\cdot0.4 = 0.1\)。
   - 手算 \(\mathrm{d}x = y \odot (\mathrm{d}y - \mathrm{dot}) = [0.1\cdot0.9,\ 0.2\cdot(-0.1),\ 0.3\cdot(-0.1),\ 0.4\cdot(-0.1)] = [0.09, -0.02, -0.03, -0.04]\)。
3.（可选，需 GPU 与重编译）在 `dx = y * (dy - dot)` 这一行之前临时插入一句 `cute.printf("dot = %f\n", dot)`（DSL 内核用 `cute.printf` 而非 Python `print`，见项目 `AGENTS.md` 的调试建议），重跑 `pytest tests/test_softmax.py::test_softmax -x -k "float32"`，在日志里找到 `dot` 输出，与手算对照。

**需要观察的现象**：第 2 步手算的 `dot=0.1`、`dx=[0.09, -0.02, -0.03, -0.04]`；若做了第 3 步，设备端打印的 `dot` 应与该行 \(\sum \mathrm{d}y\cdot y\) 一致。

**预期结果**：内核逻辑与公式逐项对应；`cute.printf` 的输出（若执行）与手算一致。第 3 步若无法在本地编译运行，标注「待本地验证」。

> 注意：修改内核源码加 `cute.printf` 仅为本地调试，验证后应还原；本讲不要求、也不应当把调试改动提交。

#### 4.1.5 小练习与答案

**练习 1**：为什么反向 `stage=1`，而前向非 online 时 `stage=2`？

**答案**：反向只需算一个标量 `dot`，对应一次行内归约，故一槽缓冲、一个 mbarrier（`stage=1`）。前向非 online 要分别求 `max` 和 `sum` 两个标量，各占一槽，故 `stage=2`。

**练习 2**：反向为什么不把 OOB 填成 \(-\infty\)？

**答案**：反向的 `dot` 是 \(\sum \mathrm{d}y_j y_j\)，OOB 位置（超出真实列数的部分）应当贡献 0；且 OOB 处的 \(\mathrm{d}x\) 也应为 0。填 \(-\infty\) 反而会让 `dot` 变成 \(-\infty\) 而破坏结果。填 0 才是数学正确的（前向填 \(-\infty\) 是为了让 `exp` 归零，目的不同）。

**练习 3**：`SoftmaxBackward` 为什么要重写 `_num_threads`，把切到 256 线程的阈值从 16384 降到 8192？

**答案**：反向同时持有 `sdY` 与 `sY` 两块 SMEM，单 block 的共享内存占用比前向（一块）更高。更早地启用 256 线程（即更大的 `tiler_mn[0] = num_threads // threads_per_row`，每 block 处理更多行）有助于在 SMEM 压力增大时平衡占用率与 block 数量。（具体收益比例待本地 profiling 验证。）

---

### 4.2 SoftmaxFunction autograd 集成

#### 4.2.1 概念说明

有了前向、反向两个内核后，还差「胶水」把它们接进 PyTorch 的自动微分。PyTorch 提供 `torch.autograd.Function`：你声明一个子类，实现两个静态方法：

- `forward(ctx, *inputs)`：算输出；调用 `ctx.save_for_backward(*tensors)` 把反向要用的张量存起来。
- `backward(ctx, *grad_outputs)`：接收 loss 对**每个输出**的梯度，返回 loss 对**每个输入**的梯度（与 `forward` 的输入一一对应，个数与顺序必须对齐）。

设计 `backward` 的核心决策是：**存什么**。4.1.1 已指出反向公式只需 \(y\) 和 \(\mathrm{d}y\)，完全不需要输入 \(x\)。因此 `SoftmaxFunction` 选择保存 softmax 输出 \(y\)，而不是输入 \(x\)——这既省显存（\(y\) 反正要算出来），又免去了反向时重算 softmax。

#### 4.2.2 核心流程

```
SoftmaxFunction.forward(ctx, x):
    y = softmax_fwd(x)        # 调前向内核得到 y
    ctx.save_for_backward(y)  # 只存 y（不存 x）
    return y

SoftmaxFunction.backward(ctx, dy):
    (y,) = ctx.saved_tensors  # 取回当时存的 y
    dx = softmax_bwd(dy, y)   # 调反向内核：dx_i = y_i*(dy_i - dot)
    return dx                 # forward 只有一个输入 x，故只返回一个梯度
```

注意 `backward` 的返回值个数必须与 `forward` 的输入个数一致。这里 `forward(ctx, x)` 只有一个输入 `x`，所以 `backward` 只返回 `dx`。

#### 4.2.3 源码精读

[quack/softmax.py:400-411](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/softmax.py#L400-L411) 就是上面流程的源码：

```python
class SoftmaxFunction(torch.autograd.Function):
    @staticmethod
    def forward(ctx, x):
        y = softmax_fwd(x)
        ctx.save_for_backward(y)
        return y

    @staticmethod
    def backward(ctx, dy):
        (y,) = ctx.saved_tensors
        dx = softmax_bwd(dy, y)
        return dx
```

**保存的张量（saved_tensors）**：`forward` 中 `ctx.save_for_backward(y)` 只存了 \(y\) 一个张量；`backward` 中 `(y,) = ctx.saved_tensors` 解包出这一个。这就是反向所需的全部中间量——没有 \(x\)，也没有 max/sum，因为反向公式把它们都消掉了。

**为什么测试要先 `torch.cuda.synchronize()` 再取 `autograd.grad`**：

看测试 [tests/test_softmax.py:52-57](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/tests/test_softmax.py#L52-L57)：

```python
out = function(x)
out_ref = F.softmax(x_ref, dim=-1)
dy = torch.randn_like(out)
torch.cuda.synchronize()  # without sync, torch.autograd gets wrong results
(dx,) = torch.autograd.grad(out, x, grad_outputs=dy)
```

`SoftmaxFunction.forward` 调用的是 QuACK 的自定义 CUDA 内核（`softmax_fwd` → `_softmax_fwd` 这个 `@cute_op`），内核启动是**异步**的：CPU 把内核丢进 GPU 流就立即返回，此时 \(y\)（即 `out`，也是被 `save_for_backward` 存下的张量）可能还没在显存里写完。紧接着 `torch.autograd.grad` 会触发反向内核 `softmax_bwd(dy, y)`，而它要**读取同一个 \(y\)**。如果在 eager 模式下缺少足够的流依赖保证，反向内核可能在 \(y\) 写完之前就开始读，读到的是未完成的数据，于是梯度出错。

显式 `torch.cuda.synchronize()` 强制等当前 GPU 流上所有先前工作（前向内核）完成，保证 \(y\) 已落盘，再进入反向。这是把自定义 CUDA 算子包进 `torch.autograd.Function` 时在 eager 模式下的一个已知尖角；在 `torch.compile` 全图模式下，调度器会自动处理跨算子的流依赖（测试同时跑了 `use_compile=True/False` 两条路径，见 [tests/test_softmax.py:43](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/tests/test_softmax.py#L43)）。该同步必要性的底层流机制细节待本地验证，但「前向内核异步、反向要读其输出、故需先同步」这一因果链是确定的。

#### 4.2.4 代码实践

**目标**：对照源码写出反向所需的 `saved_tensors`，并亲手验证「去掉 sync 会出错」这一论断。

**步骤**：

1. 打开 [quack/softmax.py:401-411](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/softmax.py#L401-L411)，写下：`forward` 通过 `ctx.save_for_backward(y)` 存了**一个**张量 \(y\)（softmax 输出）；`backward` 通过 `(y,) = ctx.saved_tensors` 取回这**一个**张量。反向所需、且仅所需的中间量就是 \(y\)。
2. 阅读测试 [tests/test_softmax.py:45-68](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/tests/test_softmax.py#L45-L68)，确认 `dx` 是与 PyTorch 参考实现 `F.softmax` 的 autograd 结果做数值比对（容差见 [tests/test_softmax.py:14-18](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/tests/test_softmax.py#L14-L18)）。
3.（需 GPU）复制测试片段到本地脚本，把第 55 行的 `torch.cuda.synchronize()` 注释掉，重跑，观察 `(dx,) = torch.autograd.grad(...)` 得到的 `dx` 是否仍与参考一致；多次运行观察是否偶发出错（异步竞态常表现为不稳定）。

**需要观察的现象**：第 1 步结论为「仅保存 \(y\)」；第 3 步若去掉 sync，可能出现 `dx` 数值偏离参考实现，且多次运行结果不稳定。

**预期结果**：保留 sync 时测试稳定通过；去掉 sync 后可能出现数值错误（待本地验证，因竞态是否触发取决于时序）。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `ctx.save_for_backward(y)` 改成 `ctx.save_for_backward(x)`（存输入），`backward` 需要怎么改？这样做好不好？

**答案**：`backward` 需要先由 \(x\) 重算 \(y = \mathrm{softmax}(x)\)，再调用 `softmax_bwd(dy, y)`。这样既多占一份与输出等大的显存（存 \(x\)），又要在反向里额外跑一次完整 softmax 前向，得不偿失。QuACK 的选择——只存 \(y\)——是最优的。

**练习 2**：`SoftmaxFunction.backward` 为什么只返回 `dx` 一个值？如果返回 `(dx, None)` 会怎样？

**答案**：`backward` 的返回值须与 `forward` 的输入一一对应。`forward(ctx, x)` 只有一个输入 `x`，故 `backward` 返回一个梯度 `dx`。返回 `(dx, None)` 表示「有两个输入，第二个不需要梯度」——与单输入签名不符，会报错。本算子只有一个输入，所以只返回 `dx`。

**练习 3**：为什么 `forward` 里没有出现 `ctx` 的 `requires_grad` / `mark_dirty` 之类的调用？

**答案**：`forward` 的输入 `x` 只被读取（`softmax_fwd` 内部用 `torch.empty_like(x)` 新建输出，不在原地改 `x`，见 4.3.3），没有就地修改任何输入张量，因此无需 `mark_dirty`；`x` 默认需要梯度（调用方在测试里对 `x` 调了 `.requires_grad_()`），无需额外标记。

---

### 4.3 softmax 公共 API 与完整调用链

#### 4.3.1 概念说明

对使用者来说，整个 softmax 模块只暴露一个函数 `quack.softmax(x)`（在 [quack/__init__.py:19,26](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/__init__.py#L19) 导出，列入 `__all__`）。它既能在 eager 模式下工作，又能被 `torch.compile` 融合。本模块梳理从这一个函数一路向下到设备内核的完整调用链，把前两节的内核和 autograd 串起来。

调用链里有两个 `@cute_op` 注册的自定义算子（`quack::_softmax_fwd`、`quack::_softmax_backward`），它们通过 `torch.library` 注册成 PyTorch 原生算子，这是让 `torch.compile` 能识别并正确处理这些内核的关键（详见 [u2-l6](u2-l6-cute-op-and-jit-cache.md)）。

#### 4.3.2 核心流程

完整调用链（前向）：

```
quack.softmax(x)                         # 公开 API（quack/__init__.py 导出）
  └─ SoftmaxFunction.apply(x)            # torch.autograd.Function 入口
       └─ forward: softmax_fwd(x)
            └─ out = torch.empty_like(x)
            └─ _softmax_fwd(x, out)      # @cute_op("quack::_softmax_fwd") 自定义算子
                 └─ Softmax.compile(dtype, out_dtype, N)(x, out)  # 编译/取缓存 + 启动 Softmax 设备内核
```

反向调用链：

```
autograd 触发 SoftmaxFunction.backward(dy)
  └─ (y,) = ctx.saved_tensors
  └─ softmax_bwd(dy, y)
       └─ dx = torch.empty_like(dy)
       └─ _softmax_backward(dy, y, dx)   # @cute_op("quack::_softmax_backward")
            └─ SoftmaxBackward.compile(dtype, y_dtype, dx_dtype, N)(dy, y, dx)  # 启动反向设备内核
```

两条链各自经过「公开 API → autograd Function → `*_fwd`/`*_bwd` 辅助函数 → `@cute_op` 自定义算子 → `compile` + 启动设备内核」五层。

#### 4.3.3 源码精读

**(a) 公开 API**

[quack/softmax.py:414-423](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/softmax.py#L414-L423) 只有一行实质内容——委托给 `SoftmaxFunction.apply`：

```python
def softmax(x: torch.Tensor) -> torch.Tensor:
    """Softmax forward pass with automatic differentiation support."""
    return SoftmaxFunction.apply(x)
```

**`apply` 是 `torch.autograd.Function` 提供的类方法**：它会自动建立 autograd 图，确保 `forward` 在前向时执行、`backward` 在反向时被调用。所以使用者直接调 `quack.softmax(x)` 就同时获得了「前向 + 自动微分」。

**(b) `_softmax_fwd` / `softmax_fwd`（前向辅助与自定义算子）**

[quack/softmax.py:193-213](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/softmax.py#L193-L213)：

```python
@cute_op("quack::_softmax_fwd", mutates_args={"out"})
def _softmax_fwd(x: torch.Tensor, out: torch.Tensor) -> None:
    ...
    N = x.size(1)
    dtype, out_dtype = [torch2cute_dtype_map[t.dtype] for t in [x, out]]
    Softmax.compile(dtype, out_dtype, N)(x, out)

def softmax_fwd(x: torch.Tensor) -> torch.Tensor:
    out = torch.empty_like(x)
    _softmax_fwd(x, out)
    return out
```

注意 `out` 由 `softmax_fwd` 用 `torch.empty_like(x)` 新建，再以**原地修改**（`mutates_args={"out"}`）方式交给自定义算子填充。这就是 4.2.5 练习 3 里说的「`x` 不被就地修改」的来由——被修改的是新分配的 `out`，而 `out` 正是 `SoftmaxFunction.forward` 存下来给反向用的 \(y\)。

**(c) `_softmax_backward` / `softmax_bwd`（反向辅助与自定义算子）**

[quack/softmax.py:373-397](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/softmax.py#L373-L397) 结构完全对称：

```python
@cute_op("quack::_softmax_backward", mutates_args={"dx"})
def _softmax_backward(dy: torch.Tensor, y: torch.Tensor, dx: torch.Tensor) -> None:
    ...
    SoftmaxBackward.compile(dtype, y_dtype, dx_dtype, N)(dy, y, dx)

def softmax_bwd(dy: torch.Tensor, y: torch.Tensor) -> torch.Tensor:
    dx = torch.empty_like(dy)
    _softmax_backward(dy, y, dx)
    return dx
```

两个 `@cute_op` 都声明 `mutates_args` 指向输出张量（前向 `out`、反向 `dx`），让 PyTorch 清楚地知道哪个参数是被原地写入的——这对 `torch.compile` 的别名分析与正确性至关重要。

#### 4.3.4 代码实践

**目标**：亲手跑通公开 API，并用断言确认它「既有前向数值正确、又能自动微分」。

**步骤**（需 GPU）：

1. 写一个最小脚本：
   ```python
   import torch
   import torch.nn.functional as F
   from quack import softmax   # 公开 API

   torch.manual_seed(0)
   x = (0.1 * torch.randn(199, 4096, device="cuda", dtype=torch.bfloat16)).requires_grad_()
   y = softmax(x)                       # 走 SoftmaxFunction.apply → 前向内核
   y_ref = F.softmax(x.detach(), dim=-1)
   print("fwd close:", torch.allclose(y, y_ref, atol=1e-2, rtol=1e-2))

   dy = torch.randn_like(y)
   torch.cuda.synchronize()             # 关键：取 grad 前同步
   (dx,) = torch.autograd.grad(y, x, grad_outputs=dy)
   x_ref = x.detach().clone().requires_grad_(True)
   y_ref2 = F.softmax(x_ref, dim=-1)
   (dx_ref,) = torch.autograd.grad(y_ref2, x_ref, grad_outputs=dy)
   print("bwd close:", torch.allclose(dx, dx_ref, atol=1e-2, rtol=1e-2))
   ```
2. 运行该脚本（首次会触发内核编译，需等待）。
3. 对照 4.3.2 的调用链，确认 `softmax(x)` 一行同时触发了前向内核与 autograd 图的建立。

**需要观察的现象**：两次 `close` 均打印 `True`。

**预期结果**：前向与反向都与 PyTorch 参考实现数值一致。若跳过 `torch.cuda.synchronize()`，`bwd close` 可能偶发为 `False`（待本地验证，取决于竞态时序）。

#### 4.3.5 小练习与答案

**练习 1**：调用 `quack.softmax(x)` 返回的 `y`，其 `requires_grad` 属性是 `True` 还是 `False`？为什么？

**答案**：`True`（前提是输入 `x.requires_grad=True`）。因为 `softmax` 经由 `SoftmaxFunction.apply`，PyTorch 会自动把 `SoftmaxFunction` 接入 autograd 图，输出的 `requires_grad` 跟随需要梯度的输入。

**练习 2**：`softmax_fwd` 和 `softmax` 有什么区别？什么时候该用哪个？

**答案**：`softmax_fwd` 只跑前向内核、不建 autograd 图，返回的张量 `requires_grad=False`；`softmax` 经由 `SoftmaxFunction.apply`，可微。需要梯度时用 `softmax`；只关心前向输出（如纯推理手算、或自己管理反向）时可用 `softmax_fwd`。测试文件同时导入了两者（[tests/test_softmax.py:8](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/tests/test_softmax.py#L8)），分别覆盖这两种用法。

**练习 3**：为什么两个 `@cute_op` 都要写 `mutates_args={"out"}` / `mutates_args={"dx"}`？

**答案**：`out`/`dx` 是被算子**原地写入**的输出张量。`torch.library` 要求显式声明哪些参数会被原地修改，以便 `torch.compile` 做正确的别名分析与内存调度；漏标会导致编译图对数据依赖的误判，可能产生错误结果。

---

## 5. 综合实践

把本讲三块内容（反向数学、autograd 集成、调用链）串成一个任务：

**任务**：实现一个「softmax + 简单 loss」的最小可微示例，验证 QuACK softmax 的反向梯度，并解释每一步用到本讲的哪个知识点。

**步骤**（需 GPU）：

1. 构造输入与目标：`x`（需梯度）、`target`（与 `y` 同形的 one-hot 概率）。
2. 用 `quack.softmax(x)` 得到 `y`（4.3 调用链；`SoftmaxFunction.forward` 存下 \(y\)）。
3. 取上游梯度 `dy = y - target`（这是交叉熵对 softmax 输出的标准上游梯度）。
4. `torch.cuda.synchronize()` 后用 `torch.autograd.grad(y, x, grad_outputs=dy)` 得 `dx`（4.2.3 的同步必要性与 `saved_tensors`）。
5. 用 `F.softmax` + PyTorch autograd 走同样的 `dy`，得到 `dx_ref`，比较 `dx` 与 `dx_ref`。
6. 在 [quack/softmax.py:338-351](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/softmax.py#L338-L351) 找到反向内核算 `dot` 与 `dx` 的两行，把第 4 步得到的 `dx` 对应回公式 \(\mathrm{d}x_i = y_i(\mathrm{d}y_i - \mathrm{dot})\)。

**预期结果**：`dx` 与 `dx_ref` 在 dtype 容差内一致（bf16 用 1e-2，fp32 用 1e-4，见 [tests/test_softmax.py:14-18](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/tests/test_softmax.py#L14-L18)）。整个流程同时验证了反向数学（4.1）、autograd 包装与 `saved_tensors`（4.2）、以及公开 API 调用链（4.3）。

## 6. 本讲小结

- softmax 反向有闭式公式 \(\mathrm{d}x_i = y_i(\mathrm{d}y_i - \mathrm{dot})\)，其中 \(\mathrm{dot} = \sum_j \mathrm{d}y_j y_j\)，只需**一次**行内归约，因此 `SoftmaxBackward` 配置为 `stage=1, reduction_dtype=Float32`。
- 反向内核相对前向有两个结构差异：**双输入加载**（同时加载 \(\mathrm{d}y\) 与 \(y\)，分配两块 SMEM）与 **OOB 填 0**（而非前向的 \(-\infty\)，因为 `dot` 与 \(\mathrm{d}x\) 的 OOB 贡献应为 0）。
- 双 SMEM 使反向的资源阈值更保守：`_num_threads` 在 N=8192 即切到 256 线程，SM12x fp32 的 cluster 阈值也更紧。
- `SoftmaxFunction` 用 `torch.autograd.Function` 接入 PyTorch 自动微分，`forward` 只保存 softmax 输出 \(y\)（不保存 \(x\)），`backward` 取回 \(y\) 调 `softmax_bwd`。
- 测试中 `torch.cuda.synchronize()` 不可省：前向内核异步写入 \(y\)，反向内核要读同一个 \(y\)，eager 模式下需显式同步避免竞态导致梯度错误。
- 公开 API `quack.softmax(x)` 经 `SoftmaxFunction.apply` → `softmax_fwd` → `@cute_op("quack::_softmax_fwd")` → `Softmax.compile` → 设备内核，反向链路完全对称。

## 7. 下一步学习建议

- **归约原语细节**：本讲的 `dot` 归约依赖 `row_reduce` 的 warp 内 + 跨 CTA 两阶段机制。下一讲 [u2-l4 归约原语：warp/row/online](u2-l4-reduce-primitives.md) 会拆解 `warp_reduce`、`row_reduce`、`online_softmax_reduce` 的内部实现。
- **自定义算子与编译缓存**：本讲多次提到 `@cute_op` 与 `compile`/`@jit_cache`，[u2-l6 cute_op 自定义算子与编译缓存](u2-l6-cute-op-and-jit-cache.md) 会讲清 `torch.library` 注册机制与 `.o` 两级缓存。
- **进阶对照**：建议接着读 [u2-l5 RMSNorm 前向与反向](u2-l5-rmsnorm.md)，对比另一种归约算子的反向设计（RMSNorm 反向需要额外的 `rstd` 缓冲，与 softmax 只存 \(y\) 形成对照）。
- **源码延伸阅读**：直接对照 [quack/cross_entropy.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/cross_entropy.py) 与 [quack/rmsnorm.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/rmsnorm.py)，看不同归约算子如何复用 `ReductionBase` 与 `row_reduce`。
