# 在线 Softmax 数值核心

## 1. 本讲目标

学完本讲，你应当能够：

- 说清「在线 softmax（online softmax）」要解决的根本矛盾：分块计算时，每一块的行最大值都还可能被后续块更新，softmax 的归一化分母因此无法一次性确定。
- 用三步递推 `row_max → row_sum → rescale` 写出分块 softmax 的数学公式，并证明它和「一次性对整行做 softmax」在数学上完全等价（精确，而非近似）。
- 读懂 FA4 中 `Softmax` 类的 `online_softmax` / `finalize` / `rescale_O` 三个核心方法，知道 `row_max`、`row_sum` 这两个寄存器（rmem）张量是如何被维护的。
- 理解 FA4 为什么把底数从 `e` 换成 `2`：用 `scale_log2 = softmax_scale · log₂e` 配合硬件 `exp2` 指令加速，并用最大值减法保证数值稳定。
- 用纯 PyTorch 复现一个分块 online softmax，验证它与 `torch.softmax` 在 fp32 下几乎完全一致。

## 2. 前置知识

在进入源码前，先统一三个概念。本讲承接 u1-l1（tiling / online softmax 直觉）、u2-l1（公共 API 与 LSE）和 u3-l2（BlockInfo 分块）已建立的认知，不重复它们。

**普通 softmax 的「一次性」定义。** 给定一行分数 \(s_1, \dots, s_N\)，softmax 输出为：

\[
p_i = \frac{e^{s_i}}{\sum_{j=1}^{N} e^{s_j}}
\]

直接算会overflow：若某个 \(s_i\) 很大，\(e^{s_i}\) 会爆炸。工程上的标准做法是「减去行最大值 \(m\)」：

\[
p_i = \frac{e^{s_i - m}}{\sum_{j=1}^{N} e^{s_j - m}}, \qquad m = \max_j s_j
\]

因为分子分母同乘 \(e^{-m}\)，结果不变，而所有指数都 \(\le 0\)，不会溢出。

**矛盾来了。** FlashAttention 把长序列切成一块块（tiling，见 u3-l2），每次只把一小段 key 放进 SRAM 算。但「减最大值」要求我们先知道**整行**的最大值 \(m\)——可整行根本没同时出现在显存里。这就是 online softmax 要解决的问题：**在还不知道最终最大值的情况下，边算边维护一个会被不断修正的归一化状态。**

**HBM / SRAM / rmem 三级存储。** 回顾 u1-l1：HBM 是显存（大但慢），SRAM 是片上共享内存（小但快），rmem（register memory）是线程私有的寄存器。FA4 把 `row_max`、`row_sum` 这两个标量状态放在 rmem 里（每个 Q 行一对），让它们跟得上 MMA 计算的速度。本讲会出现 `cute.make_rmem_tensor`，就是「在寄存器里开一个张量」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [flash_attn/cute/softmax.py](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/softmax.py) | 定义 `Softmax` 类，是 online softmax 的全部状态与方法所在：`row_max`/`row_sum` 跟踪、`online_softmax` 主循环、`finalize` 输出 LSE、`rescale_O` 重缩放输出累加器。 |
| [flash_attn/cute/utils.py](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/utils.py) | 提供数值工具：`compute_softmax_scale_log2`（换底）、`fmax_reduce`/`fadd_reduce`（行归约）、`ex2_emulation` 与 `POLY_EX2`（exp2 多项式逼近）。 |

> 阅读建议：先看本讲第 4.1 节的数学，再带着公式去读 `softmax.py` 的 `online_softmax`，最后到 `utils.py` 看换底与归约细节。

## 4. 核心概念与源码讲解

### 4.1 在线 softmax 的核心思想与数学等价性

#### 4.1.1 概念说明

「在线（online）」算法的特点是：输入数据**逐块到来**，算法必须用一个固定大小的状态去「消化」每一块，且最终结果要等价于「看到全部数据后一次性计算」。

online softmax 要维护的状态只有两个标量（按 Q 行）：

- `row_max` \(m\)：到目前为止见过的最大分数。
- `row_sum` \(\ell\)：到目前为止的「未归一化分母」，即 \(\sum e^{s_j - m}\)（以当前 \(m\) 为基准）。

注意 \(\ell\) 总是相对于**当前的** \(m\) 定义的。一旦 \(m\) 被一个更大的值更新，旧的 \(\ell\) 就「失真」了，必须乘一个修正因子。这个修正因子就是本讲的灵魂——**rescale（重缩放）**。

#### 4.1.2 核心流程：三步递推

假设我们已经处理完前 \(k-1\) 块，状态为 \((m_{k-1},\ \ell_{k-1})\)，输出累加器为 \(O_{k-1}\)。现在第 \(k\) 块的分数为 \(\{s_i\}_{i\in \text{chunk}_k}\)，对应 value 为 \(\{v_i\}\)。三步递推如下：

**第一步：更新行最大值。**

\[
m_k = \max\bigl(m_{k-1},\ \max_{i\in \text{chunk}_k} s_i\bigr)
\]

**第二步：重缩放旧状态，再累加新块。** 因为基准从 \(m_{k-1}\) 变成了 \(m_k\)，旧的 \(\ell_{k-1}\) 与 \(O_{k-1}\) 都要乘以 \(e^{m_{k-1}-m_k}\)（注意 \(m_{k-1}-m_k \le 0\)，所以这个因子 \(\le 1\)，安全）：

\[
\ell_k = \ell_{k-1}\cdot e^{m_{k-1}-m_k} \;+\; \sum_{i\in \text{chunk}_k} e^{s_i - m_k}
\]

\[
O_k = O_{k-1}\cdot e^{m_{k-1}-m_k} \;+\; \sum_{i\in \text{chunk}_k} e^{s_i - m_k}\, v_i
\]

**第三步：全部块处理完后，做最终归一化。**

\[
\text{softmax 输出} = \frac{O_K}{\ell_K}
\]

**为什么精确等价？** 用数学归纳法。记完整的未归一化分母为 \(Z=\sum_{j=1}^{N} e^{s_j}\)。我们要证：处理完任意前缀后，\(\ell_k \cdot e^{m_k} = \sum_{j\in \text{前 }k\text{ 块}} e^{s_j}\)。

- 归纳基础：第一块时 \(m_1=\max s\)，\(\ell_1=\sum e^{s_i-m_1}\)，于是 \(\ell_1 e^{m_1}=\sum e^{s_i}\)，成立。
- 归纳 step：设前缀成立，即 \(\ell_{k-1}e^{m_{k-1}}=\sum_{\text{前}} e^{s_j}\)。那么

\[
\ell_k e^{m_k} = \bigl(\ell_{k-1}e^{m_{k-1}-m_k} + \sum_{\text{chunk}_k}e^{s_i-m_k}\bigr)e^{m_k} = \ell_{k-1}e^{m_{k-1}} + \sum_{\text{chunk}_k}e^{s_i} = \sum_{\text{前 }k\text{ 块}} e^{s_j}
\]

成立。最终 \(\ell_K e^{m_K}=Z\)，于是 \(O_K/\ell_K = (\sum e^{s_j-m_K}v_j)/\ell_K\)，分子分母同除以 \(e^{m_K}\) 后正是精确的 \((\sum e^{s_j}v_j)/Z\)。

> 关键结论：online softmax 的误差**只来自浮点舍入**，算法本身是精确的。这也是 FlashAttention 敢自称「exact attention」（见 u1-l3）的数学根基。

#### 4.1.3 源码精读：状态在哪里

`Softmax` 类用两个 rmem 张量持有这两份状态。[softmax.py:L92-L110](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/softmax.py#L92-L110) 定义了字段与工厂方法：

```python
@dataclass
class Softmax(ParamsBase):
    scale_log2: Float32
    num_rows: cutlass.Constexpr[int]
    row_max: cute.Tensor      # 行最大值，每个 Q 行一个
    row_sum: cute.Tensor      # 行和（未归一化分母），每个 Q 行一个
    ...
    @staticmethod
    def create(...):
        row_max = cute.make_rmem_tensor(num_rows, Float32)  # 寄存器张量
        row_sum = cute.make_rmem_tensor(num_rows, Float32)
        return Softmax(scale_log2, num_rows, row_max, row_sum, ...)
```

- `num_rows` 是这个线程组负责的 Q 行数（`cutlass.Constexpr[int]`，编译期常量，会进 kernel 特化）。
- `row_max`/`row_sum` 用 `cute.make_rmem_tensor` 开在**寄存器**里，保证 softmax 统计跟得上 MMA 的节拍。
- 初始值在 [softmax.py:L112-L114](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/softmax.py#L112-L114) 的 `reset` 中给出：`row_max=-inf`、`row_sum=0.0`，对应「还没见到任何分数」的空状态。

#### 4.1.4 代码实践

**目标：** 用最直白的方式验证「分块 + rescale」真能还原整行 softmax。

**操作步骤（示例代码，可在任意带 PyTorch 的环境运行）：**

```python
# 示例代码：用递推公式手算两块拼接的 softmax，与一次性结果对比
import torch
torch.manual_seed(0)
s = torch.randn(1, 8) * 3.0        # 一行 8 个分数
chunk_a, chunk_b = s[:, :4], s[:, 4:]

# 一次性参考
ref = torch.softmax(s, dim=-1)

# 第一块
m1 = chunk_a.amax(-1)
l1 = torch.exp(chunk_a - m1).sum(-1)

# 第二块到来，m 被更新
m2 = torch.maximum(m1, chunk_b.amax(-1))
l2 = l1 * torch.exp(m1 - m2) + torch.exp(chunk_b - m2).sum(-1)

p = torch.cat([torch.exp(chunk_a - m2), torch.exp(chunk_b - m2)], dim=-1) / l2
print("max abs err:", (p - ref).abs().max().item())
```

**需要观察的现象：** 打印的 `max abs err` 应在 \(10^{-7}\) 量级（fp32 舍入级别）。

**预期结果：** 两者几乎完全一致，证明递推公式精确等价于一次性 softmax。若误差显著偏大，多半是忘了在第二块用**新的** `m2`（而不是 `m1`）去算 `exp(chunk_a - m2)`。

#### 4.1.5 小练习与答案

**练习 1.** 若把第二步的 rescale 因子写成 \(e^{m_k - m_{k-1}}\)（符号反了），数值上会发生什么？

**答案：** 因为 \(m_k \ge m_{k-1}\)，指数变号后因子 \(\ge 1\)，旧累加器会被放大；当某块带来一个很大的新最大值时，\(e^{m_k-m_{k-1}}\) 可能溢出成 `inf`，结果完全错误。所以因子**必须是** \(e^{m_{\text{旧}}-m_{\text{新}}}\le 1\)。

**练习 2.** 为什么 `reset` 把 `row_max` 初始化成 `-inf` 而不是 `0.0`？

**答案：** 第一块到来时要计算 \(\max(-\inf,\ \max s)=\max s\)，等价于「没有任何先验」。若初始化成 `0.0`，当所有真实分数都为负时，最大值会被错误地钉在 `0.0`，后续 `exp(s - 0)` 仍可能溢出。

---

### 4.2 row_max 与 row_sum：Softmax 类的状态跟踪

#### 4.2.1 概念说明

知道了递推公式，下一个问题是：在 GPU kernel 里，`row_max` 和 `row_sum` 怎么从「一个 tile 的分数张量」归约成「一个标量」？

一个 tile 的分数 `acc_S` 形状是 `(num_rows, n_block_size)`——`num_rows` 行 Q，每行 `n_block_size` 个 K 分数。对每一行，我们要做两件事：

- **行最大值**：沿 `n_block_size` 维取 max，再和「历史 `row_max`」取 max。
- **行和**：把 `exp(score - row_max)` 沿 `n_block_size` 维求和，再 rescale 进「历史 `row_sum`」。

这两件事都依赖**跨线程归约（reduction）**：因为一个 tile 的数据散布在一个 warp/warp-group 的众多线程里，必须用 warp shuffle 把它们归约到每行一个值。

#### 4.2.2 核心流程

`online_softmax` 方法对每一行 `r` 做如下处理（伪代码）：

```
for r in 每一行:
    acc_S_row = 加载第 r 行分数到寄存器                # (n_block_size,)
    row_max_cur = fmax_reduce(acc_S_row, init=历史 row_max[r])   # 跨线程求 max
    row_max_cur = warp_reduction_max(row_max_cur, group=4)       # warp 内再归约
    row_max_prev = row_max[r]                          # 留住旧值算 rescale
    row_max[r] = row_max_cur                           # 更新状态
    # （下面进入 rescale 与 row_sum，见 4.3）
```

两个细节值得留意：

1. **`init_val` 的二义性**。`fmax_reduce` 的 `init_val` 在「第一块（`is_first=True`）」时传 `None`（从 `-inf` 起步），否则传历史 `row_max[r]`（把历史最大值纳入比较）。这由 `cutlass.const_expr` 在编译期裁剪分支，特化出无冗余的 kernel（回顾 u2-l2 讲过的 compile_key）。
2. **`-inf` 的安全化**。若整行被掩码成 `-inf`（例如全因果越界），`row_max_cur` 会是 `-inf`，后续 `exp(score - (-inf))` 会出 NaN。所以代码在 [softmax.py:L164-L165](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/softmax.py#L164-L165) 把 `-inf` 替换成 `0.0` 再参与运算。

#### 4.2.3 源码精读

行最大值归约 `fmax_reduce` 在 [utils.py:L366-L414](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/utils.py#L366-L414)，它按架构分两条路径（`arch < 100` 用标量循环 + `fmax`，`arch >= 100` 用 `fmax(a, b, c)` 三输入指令强压吞吐）：

```python
@cute.jit
def fmax_reduce(x, init_val=None, arch=80):
    if const_expr(arch < 100 or cute.size(x.shape) % 8 != 0):
        ...
        local_max = [res[0], res[1], res[2], res[3]]
        for i in cutlass.range_constexpr(4, cute.size(x.shape), 4):
            local_max[0] = fmax(local_max[0], res[i + 0]); ...
        return local_max[0] if const_expr(init_val is None) else fmax(local_max[0], init_val)
    else:
        # SM100+: 强制使用 3-input max 指令，提升吞吐
        local_max_0 = fmax(init_val, res[0], res[1]) if ... else fmax(res[0], res[1])
        ...
```

`fmax` 本身 ([utils.py:L351-L363](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/utils.py#L351-L363)) 直接 lower 到 NVVM 的 `fmax` 指令。注意它会处理 NaN 语义（`fmax` 对 NaN 的处理与 `max` 不同），这正是数值稳定所需要的。

`online_softmax` 中真正调用它的地方在 [softmax.py:L153-L165](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/softmax.py#L153-L165)：

```python
row_max_cur = utils.fmax_reduce(
    acc_S_row,
    init_val=row_max[r] if cutlass.const_expr(not is_first) else None,
    arch=arch,
)
row_max_cur = cute.arch.warp_reduction_max(row_max_cur, threads_in_group=4)
row_max_prev = row_max[r]          # 保留旧最大值，供 rescale 使用
row_max[r] = row_max_cur           # 写回新最大值
if cutlass.const_expr(check_inf):
    row_max_cur = 0.0 if row_max_cur == -Float32.inf else row_max_cur
```

`row_max_prev` 的保留是点睛之笔：rescale 因子 \(e^{m_{\text{旧}}-m_{\text{新}}}\) 必须用旧值，而 `row_max[r]` 已经被覆盖，所以先把旧值存到 `row_max_prev`。

#### 4.2.4 代码实践

**目标：** 通过阅读测试，理解 FA4 如何用参考实现校验这些统计量的正确性。

**操作步骤：**

1. 打开 [tests/cute/testing.py](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/testing.py)，定位 `attention_ref` 函数。
2. 观察它如何在 fp32 下计算参考注意力，以及是否显式计算了与 `lse` 对应的 log-sum-exp。
3. 在 [tests/cute/test_flash_attn.py](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/tests/cute/test_flash_attn.py) 中找到断言 `out` 与 `lse` 误差的用例（如 `test_flash_attn_output`），记录其容忍阈值。

**需要观察的现象：** 参考实现里 softmax 的归一化分母如何与 FA4 返回的 `lse` 对应（`exp(lse)` 即归一化分母，见 u2-l1）。

**预期结果：** 你会发现测试对 `lse` 的误差容忍与 `out` 同量级，说明 online softmax 维护的 `row_sum`（进而 `lse`）和 `out` 一样精确。**（具体阈值以本地实际运行为准，待本地验证。）**

#### 4.2.5 小练习与答案

**练习 1.** `fmax_reduce` 为什么要为 `arch >= 100` 单独写一条强制 3-input max 的路径？

**答案：** 注释（[utils.py:L393-L394](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/utils.py#L393-L394)）说明默认的 `x.reduce(MAX)` 只用到 50% 的 3-input max 指令。Blackwell 的 `fmax(a,b,c)` 是单条三输入指令，手动展开能强制全部用上，把归约吞吐提升近一倍。

**练习 2.** 若 `check_inf=False`，全 `-inf` 行会发生什么？

**答案：** `row_max_cur` 保持 `-inf`，`exp2(score*scale_log2 - (-inf)*scale_log2)` 会产生 `+inf - +inf = NaN`，进而污染整行输出。所以 `check_inf` 默认为 `True` 是一道必要的护栏。

---

### 4.3 rescale 重缩放：分块累加主循环

#### 4.3.1 概念说明

rescale 是 online softmax 区别于普通 softmax 的核心动作。每来一个新块，只要它带来了更大的最大值，**之前累加的所有结果（`row_sum` 和输出累加器 `O`）都得乘以一个 ≤ 1 的修正因子**，把它们的「基准」从旧最大值搬到新最大值。

在 FA4 里，这个修正因子不是作用在 `row_sum` 上就完事——它还要作用在前向的输出累加器 `acc_O` 上（因为 `O = Σ P·V`，`P` 依赖最大值）。`Softmax` 类提供了两个相关接口：

- `online_softmax` 返回 `row_scale`（每行一个修正因子），由前向主循环拿去乘 `acc_O`；
- `rescale_O` 真正把 `row_scale` 乘到 `acc_O` 上。

#### 4.3.2 核心流程

`online_softmax` 的每行处理分两种情形：

**情形 A：第一块（`is_first=True`）。** 没有历史状态需要 rescale：

\[
\text{row\_scale}[r] = 1.0,\qquad \ell_1 = \sum_i e^{s_i - m_1}
\]

**情形 B：后续块（`is_first=False`）。** 先算新块贡献，再算 rescale 因子并累加进 `row_sum`：

\[
\text{row\_scale}[r] = e^{m_{\text{prev}} - m_{\text{cur}}},\qquad
\ell_k = \ell_{k-1}\cdot \text{row\_scale}[r] + \sum_i e^{s_i - m_k}
\]

注意 `row_scale` 用的是 4.1.2 里的 \(e^{m_{\text{旧}}-m_{\text{新}}}\)。前向主循环随后调用 `rescale_O`，把 `acc_O` 的每一行乘以 `row_scale[r]`，完成输出累加器的基准迁移。

**最终归一化（`finalize`）。** 所有块处理完后，`row_sum` 持有 \(\ell_K\)，输出还需除以它。`finalize` 计算 `row_scale = 1/row_sum`（用 `rcp_approx` 近似倒数），主循环再用它做最后一次 `rescale_O`，得到真正的 softmax 输出。`finalize` 同时把 `row_sum` 改写成 LSE（见 4.3.3）。

#### 4.3.3 源码精读

主循环的两种情形在 [softmax.py:L167-L188](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/softmax.py#L167-L188)：

```python
if cutlass.const_expr(is_first):
    row_max_cur_scaled = row_max_cur * scale_log2
    acc_S_row_exp = cute.math.exp2(acc_S_row * scale_log2 - row_max_cur_scaled, fastmath=True)
    acc_S_row_sum = utils.fadd_reduce(acc_S_row_exp, init_val=None, arch=arch)
    row_scale[r] = 1.0
else:
    row_max_cur_scaled = row_max_cur * scale_log2
    acc_S_row_exp = cute.math.exp2(acc_S_row * scale_log2 - row_max_cur_scaled, fastmath=True)
    row_scale[r] = cute.math.exp2((row_max_prev - row_max_cur) * scale_log2, fastmath=True)
    acc_S_row_sum = utils.fadd_reduce(
        acc_S_row_exp, init_val=row_sum[r] * row_scale[r], arch=arch
    )
row_sum[r] = acc_S_row_sum
acc_S_mn[r, None].store(acc_S_row_exp)
```

逐行对照公式：

- `acc_S_row * scale_log2` 是把原始分数 \(s\) 换底成 `exp2` 的输入（详见 4.4）。
- `acc_S_row_exp = exp2(s_scaled - m_scaled)` 就是 \(e^{s - m}\)（换底后），即当前块的 \(P\) 行。
- `row_scale[r] = exp2((row_max_prev - row_max_cur) * scale_log2)` 正是 \(e^{m_{\text{旧}}-m_{\text{新}}}\)。
- `fadd_reduce(..., init_val=row_sum[r] * row_scale[r])` 把「rescale 后的旧 `row_sum`」作为初值，加上当前块 \(P\) 之和，得到新 `row_sum`。`fadd_reduce` 在 [utils.py:L417-L455](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/utils.py#L417-L455)，SM100 路径用 `add_packed_f32x2` 做双精度打包加法。
- `acc_S_mn[r, None].store(acc_S_row_exp)` 把算好的 \(P\) 行写回 tile，供后续 `P·V` 的 MMA 使用。

输出累加器的 rescale 在 [softmax.py:L229-L240](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/softmax.py#L229-L240)：

```python
@cute.jit
def rescale_O(self, acc_O, row_scale):
    acc_O_mn = layout_utils.reshape_acc_to_mn(acc_O)
    for r in cutlass.range(cute.size(row_scale), unroll_full=True):
        acc_O_mn[r, None].store(acc_O_mn[r, None].load() * row_scale[r])
```

`finalize` 的最终归一化与 LSE 改写在 [softmax.py:L207-L226](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/softmax.py#L207-L226)：

```python
# 最终除以 row_sum（用近似倒数 rcp_approx），并乘 final_scale
acc_O_mn_row_is_zero_or_nan = row_sum[r] == 0.0 or row_sum[r] != row_sum[r]   # NaN 判定
row_scale[r] = (
    cute.arch.rcp_approx(row_sum[r] if not acc_O_mn_row_is_zero_or_nan else 1.0)
) * final_scale
# 把 row_sum 改写成 LSE = ln(row_sum) + row_max * softmax_scale
row_sum[r] = (
    (row_max[r] * scale_log2 + cute.math.log2(row_sum_cur, fastmath=True)) * LN2
    if not acc_O_mn_row_is_zero_or_nan
    else -Float32.inf
)
```

这里有两个要点：

1. **NaN/零保护**：`row_sum == 0` 或为 NaN 时，`row_scale` 退化成 `1.0 * final_scale`，避免 `1/0 = inf` 污染输出；对应的 LSE 记为 `-inf`（4.1.5 解释过这种行的成因）。
2. **LSE 公式**：代码里的 `(row_max * scale_log2 + log2(row_sum)) * LN2` 展开后正是 \(\text{LSE}=\ln(\ell)+m\cdot\text{softmax\_scale}\)。验证：`scale_log2 = softmax_scale · log₂e`，`log₂e · LN2 = 1`，`log₂(ℓ) · LN2 = ln(ℓ)`，于是 `(m·softmax_scale·log₂e + log₂ℓ)·LN2 = m·softmax_scale + ln(ℓ)`，恰是 log-sum-exp。这个 LSE 就是 u2-l1 讲过的、被反向传播和 SplitKV 合并复用的归一化对数。

#### 4.3.4 代码实践

**目标：** 用源码阅读型实践，把 `row_scale` 的两次使用串成一条完整调用链。

**操作步骤：**

1. 在 [flash_fwd.py](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd.py) 的前向主循环里，搜索 `online_softmax(`，定位它的调用点。
2. 观察它返回的 `row_scale` 紧接着被传给了 `rescale_O(`。
3. 继续向下找到主循环结束后的 `softmax.finalize(`，看它返回的 `row_scale` 如何被最后一次 `rescale_O` 使用。

**需要观察的现象：** 你应能看到 `row_scale` 在「每一块」和「循环结束后」各被使用一次——前者做块间基准迁移，后者做最终除以 `row_sum`。

**预期结果：** 画出 `online_softmax → rescale_O`（每块）和 `finalize → rescale_O`（一次性）的调用关系图，理解 rescale 贯穿整个前向。**（具体行号以本地 HEAD 为准；若调用点位置有出入，待确认。）**

#### 4.3.5 小练习与答案

**练习 1.** 为什么 `finalize` 里用 `rcp_approx`（近似倒数）而不是精确的 `1.0 / row_sum`？

**答案：** `rcp_approx` 映射到 GPU 的快速倒数指令，比精确除法快得多；softmax 的最终精度主要由 fp16/bf16 输出决定，倒数近似引入的误差远小于输出格式的舍入误差，所以这里用近似是「用最小代价换足够精度」的典型取舍。

**练习 2.** `row_sum[r] != row_sum[r]` 这个看似奇怪的表达式在判断什么？

**答案：** 这是判断 NaN 的经典技巧——NaN 是唯一一个「不等于自己」的浮点值。用它检测 `row_sum` 是否意外变成 NaN（例如全 `-inf` 行未保护好的情况），从而走退化路径。

---

### 4.4 scale_log2 与 exp2：换底加速与数值技巧

#### 4.4.1 概念说明

FA4 在 softmax 里做了一个看似奇怪的选择：**全程用 `exp2`（\(2^x\)）而不是 `exp`（\(e^x\)）**。原因有二：

1. **硬件指令**。GPU 有专用的 `ex2`（`exp2`）指令，通常比 `exp` 更快、吞吐更高。
2. **换底无损**。利用恒等式 \(e^x = 2^{x \cdot \log_2 e}\)，可以把任意 `exp` 调用改写为 `exp2(x · log₂e)`。FA4 干脆把这个常数 \(\log_2 e\) **折进缩放因子**，定义 `scale_log2 = softmax_scale · log₂e`，于是：

\[
e^{\text{softmax\_scale} \cdot s} = \text{exp2}(\text{scale\_log2} \cdot s)
\]

这样一来，分数在算 `QK^T` 时是「未缩放」的，进入 softmax 时一次性乘上 `scale_log2`，既完成了缩放又完成了换底。

注意一个分支细节（u4-l2 会详讲 `score_mod`）：当用户**没有**自定义 `score_mod` 时，`softmax_scale` 被设为 `None`，`log₂e` 完全折进 `scale_log2`；当有 `score_mod` 时，`softmax_scale` 保留为运行期标量（因为要在 `score_mod` 之前先乘），`scale_log2` 只剩 `log₂e`。这个决策在 [utils.py:L185-L197](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/utils.py#L185-L197)：

```python
LOG2_E = math.log2(math.e)

def compute_softmax_scale_log2(softmax_scale, score_mod):
    if const_expr(score_mod is None):
        return softmax_scale * LOG2_E, None     # 折进 scale_log2
    else:
        return LOG2_E, softmax_scale            # 保留 softmax_scale 给 score_mod
```

#### 4.4.2 核心流程

softmax 内部的数值流水可以总结为三步：

1. **换底缩放**：`s_scaled = s · scale_log2`。
2. **减最大值**：`s_scaled - m · scale_log2`，保证指数 \(\le 0\)，避免溢出。
3. **exp2**：`exp2(s_scaled - m_scaled)`，得到当前块的 \(P\) 行。

当硬件 `exp2` 不够快或不可用时，FA4 还内置了**多项式逼近** `ex2_emulation`：把 \(x\) 拆成整数部分与小数部分，用 Sollya 求得的系数 `POLY_EX2` 对小数部分做多项式逼近，再用位运算把整数部分拼回去。这是「数值技巧」的极致体现。

#### 4.4.3 源码精读

`online_softmax` 里换底 + 减最大值 + exp2 一气呵成，[softmax.py:L168-L171](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/softmax.py#L168-L171)：

```python
row_max_cur_scaled = row_max_cur * scale_log2
acc_S_row_exp = cute.math.exp2(
    acc_S_row * scale_log2 - row_max_cur_scaled, fastmath=True
)
```

`fastmath=True` 允许编译器使用更激进的近似（牺牲一点 UL 级精度换吞吐）。rescale 因子同样用 exp2 换底（[softmax.py:L180-L182](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/softmax.py#L180-L182)），且巧妙地把减法放进指数里，省掉一次乘法：

\[
\text{row\_scale} = \text{exp2}\bigl((m_{\text{prev}} - m_{\text{cur}}) \cdot \text{scale\_log2}\bigr)
\]

exp2 的多项式逼近 `ex2_emulation` 在 [utils.py:L744-L757](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/utils.py#L744-L757)，其系数 `POLY_EX2` 由 Sollya 用 `fpminimax` 在 \([0,1]\) 上相对误差最优求得（[utils.py:L30-L64](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/utils.py#L30-L64)）：

```python
# Obtained from sollya:
# fpminimax(exp(x * log(2.0)), 1, [|1,24...|],[0;1],relative);
POLY_EX2 = { 0: (1.0,), 1: (1.0, 0.9224...), 2: (1.0, 0.6657..., 0.3301...), ... }
```

`ex2_emulation` 的核心思路（[utils.py:L745-L757](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/utils.py#L745-L757)）：把 \(x\) 拆成整数下取整 `x_rounded` 与小数部分 `x_frac`，对 `x_frac`（落在 \([0,1)\)）用多项式逼近 `POLY_EX2` 算出 \(2^{\text{x\_frac}}\)，再用 `combine_int_frac_ex2` 把整数部分（通过左移 23 位塞进浮点指数域）和小数部分拼成最终 \(2^x\)。这条路径主要在 Blackwell（SM100）的 `apply_exp2_convert` 里被选用（见 `SoftmaxSm100`），用来绕开默认 `exp2` 指令在某些情况下的吞吐瓶颈。

> 小贴士：`softmax_scale` 不进 `compile_key`（u2-l1），所以运行期改它不会重编译；但 `scale_log2` 是从它派生的运行期 `Float32`，每个 kernel 实例化时作为参数传入 `Softmax.create`。

#### 4.4.4 代码实践

**目标：** 直观感受 `exp2` 换底与多项式逼近的精度。

**操作步骤（示例代码）：**

```python
# 示例代码：对比 exp、exp2(换底)、以及一个 3 阶多项式逼近
import math
import torch

LOG2_E = math.log2(math.e)
x = torch.linspace(-5, 5, 1000, dtype=torch.float64)

ref = torch.exp(x)
via_exp2 = torch.exp2(x * LOG2_E)            # 换底
print("exp vs exp2(x*log2e) max rel err:",
      ((via_exp2 - ref).abs() / ref.abs()).max().item())
```

**需要观察的现象：** `exp` 和「换底 exp2」在 fp64 下应几乎完全一致（误差远小于 fp32 舍入），证明换底无损。

**预期结果：** 误差在 \(10^{-16}\) 量级。这验证了 FA4 用 `scale_log2` 换底在数学上不引入额外误差，速度优势是「白捡」的。

#### 4.4.5 小练习与答案

**练习 1.** 为什么 `POLY_EX2` 的多项式只在 \([0,1)\) 上拟合就够了？

**答案：** `ex2_emulation` 先把 \(x\) 拆成整数下取整 `x_rounded` 和小数 `x_frac ∈ [0,1)`，分别处理 \(2^{\text{整数}}\)（用浮点指数域位操作，精确）和 \(2^{\text{小数}}\)（多项式逼近）。因此多项式只需在 \([0,1)\) 这个小区间上足够准即可，低阶就能达到 fp32 精度。

**练习 2.** 若把 `scale_log2` 误设成 `softmax_scale`（漏乘 `log₂e`），输出会发生什么？

**答案：** 整个 softmax 的指数会少一个 `log₂e` 因子，等价于对分数做了错误的缩放，注意力分布会变得过尖或过平，输出与参考实现出现显著数值偏差。这正说明 `compute_softmax_scale_log2` 的换底是正确性强约束。

## 5. 综合实践

把本讲三个最小模块（`row_max`/`row_sum` 跟踪、rescale、`scale_log2`/`exp2`）串起来，完成下面这个**贯穿性任务**：

> 用纯 PyTorch 实现一个「分块 online softmax」，按 128 列分块累加并 rescale，与 `torch.softmax` 在 fp32 下对比最大相对误差，验证二者等价。

**操作步骤（示例代码，可直接运行）：**

```python
# 示例代码：分块 online softmax 完整实现
import torch
torch.manual_seed(0)

M, N = 4, 1024
block_size = 128
# 放大数值范围，逼出潜在的 overflow / 数值不稳定
x = torch.randn(M, N, dtype=torch.float32) * 5.0

# online softmax 状态（对应 Softmax 类的 row_max / row_sum）
row_max = torch.full((M,), -float("inf"), dtype=torch.float32)
row_sum = torch.zeros((M,), dtype=torch.float32)
P = torch.zeros_like(x)          # 累加器，对应前向的 acc_O（这里是纯 softmax，无 V）

for start in range(0, N, block_size):
    end = min(start + block_size, N)
    chunk = x[:, start:end]                          # 当前块 (M, B)

    # 第一步：更新 row_max
    chunk_max = chunk.amax(dim=-1)
    new_max = torch.maximum(row_max, chunk_max)

    # 第二步：rescale 旧状态 + 累加新块
    rescale = torch.exp(row_max - new_max)           # e^{m_旧 - m_新}，恒 <= 1
    chunk_exp = torch.exp(chunk - new_max[:, None])  # 当前块的 P 行
    row_sum = row_sum * rescale + chunk_exp.sum(dim=-1)

    # 输出累加器也要 rescale（对应 rescale_O）
    P = P * rescale[:, None]
    P[:, start:end] = chunk_exp

    row_max = new_max

# 第三步：最终归一化（对应 finalize 的 1/row_sum）
online_out = P / row_sum[:, None]

# 对照参考
ref = torch.softmax(x, dim=-1)
rel_err = (online_out - ref).abs() / ref.abs().clamp_min(1e-12)
print("max relative error:", rel_err.max().item())
```

**需要观察的现象与预期结果：**

- 打印的 `max relative error` 应在 \(10^{-6}\) 量级（fp32 舍入级别），证明分块 online softmax 与一次性 softmax **数学等价**。
- 把 `block_size` 从 128 调小到 16 或调大到 512，误差量级应基本不变——分块粒度只影响性能与中间 rescale 次数，不影响最终精度。这正是 FA4 敢用 tiling 的根本原因。
- 把 `x` 的尺度从 `*5.0` 调到 `*50.0`：朴素实现 `torch.exp(x)` 会溢出成 `inf`，而你的 online 版本（和 `torch.softmax`）因为「减最大值」依然稳定。这对应源码里 `row_max` 减法的数值保护作用。

**进阶（可选）：** 把上面的 `torch.exp(...)` 全部替换成换底形式 `torch.exp2(... * math.log2(math.e))`，重测误差，验证 4.4 讲的「换底无损」。

## 6. 本讲小结

- online softmax 用两个寄存器状态 `row_max`（\(m\)）与 `row_sum`（\(\ell\)）逐块消化分数，靠 rescale 因子 \(e^{m_{\text{旧}}-m_{\text{新}}}\) 修正基准迁移，数学上与一次性 softmax **精确等价**，误差仅来自浮点舍入。
- FA4 把这两个状态存放在 rmem 张量里（[softmax.py:L108-L109](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/softmax.py#L108-L109)），用 `fmax_reduce`/`fadd_reduce` 跨线程归约，并对 `-inf` 行做安全化处理。
- `online_softmax` 的每行处理分 `is_first` 两支：第一块 `row_scale=1.0`，后续块用 `exp2((row_max_prev - row_max_cur)·scale_log2)` 算 rescale 因子，并通过 `rescale_O` 把它作用到输出累加器 `acc_O`。
- `finalize` 做最终除以 `row_sum`（`rcp_approx`）并把 `row_sum` 改写成 LSE \(=\ln(\ell)+m\cdot\text{softmax\_scale}\)，供反向与 SplitKV 合并复用。
- FA4 全程用 `exp2` 而非 `exp`：靠 `scale_log2 = softmax_scale·log₂e` 换底，把缩放与换底合一，并配合硬件 `ex2` 指令或 `ex2_emulation` 多项式逼近提升吞吐。
- `softmax_scale` 不进 `compile_key`（改值不重编译），而 `num_rows`、`is_first`、`check_inf` 等是 `Constexpr`（会触发特化），这与 u2-l1/u2-l2 的结论一致。

## 7. 下一步学习建议

- **紧接着读 u4-l2（score_mod）**：本讲的 `online_softmax` 只处理「减最大值 + exp2」，但分数在 exp 之前还可以被用户回调 `score_mod` 改写（如 ALiBi、softcap）。u4-l2 会讲解 `call_score_mod` 如何在编译期内联进 softmax 流程，它是本讲 4.4「换底缩放」步骤的自然延伸。
- **回顾前向主循环 u6-l1**：带着本讲的 `online_softmax → rescale_O → finalize` 三件套，去 [flash_fwd.py](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd.py) 的前向主循环里看它们如何被嵌入「load Q → 遍历 K/V → softmax 累加 → store O」的骨架。
- **进阶到 SplitKV（u7-l2）**：`finalize` 产出的 LSE 是 SplitKV 合并多个 split 部分结果的数学基础——多个 split 各自的 `(O_split, lse_split)` 用 log-sum-exp 合并，本质上是对「块」再做一次 online softmax。理解了本讲，u7-l2 的合并公式会非常自然。
- **想看 Blackwell 特化**：可预习 [softmax.py](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/softmax.py) 的 `SoftmaxSm100` 子类（`num_rows=1`、`rescale_threshold` 跳过微小 rescale 的优化、`apply_exp2_convert` 选用 `ex2_emulation`），它对应 u8 单元的 Blackwell kernel。
