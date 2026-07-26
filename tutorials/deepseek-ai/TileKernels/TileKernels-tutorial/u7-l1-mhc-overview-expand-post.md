# MHC 概览：残差扩展与后处理

## 1. 本讲目标

本讲是 Manifold HyperConnection（简称 mhc，流形超连接）单元的第一篇，目标只有一个：**建立 mhc 整条数据流水线的心智模型，并把其中三个最基础的算子（expand / norm_fn / post）读通**。

读完本讲你应该能够：

- 说清楚 `mhc_mult` 这个新增残差维度是什么、为什么要把单个残差流复制成多股并行流；
- 用一句话讲清 `expand_from_embedding`、`mhc_pre`、`mhc_post` 在一层 Transformer 子层（attention / FFN）的前后做了什么；
- 读懂 `expand_kernel.py`（前向复制、反向求和）、`norm_fn_kernel.py`（分组 RMS 归一化投影）、`post_kernel.py`（子层输出重组回多残差流）这三段真实 TileLang 代码；
- 独立写出一个 `expand_from_embedding` 的 PyTorch 参考实现，并与 kernel 输出对拍。

本讲只讲「概览 + 三个基础算子」。`mhc_pre` 内部用到的 `pre_split_mixes` / `sinkhorn` / `pre_big_fuse` / `multilayer_recompute` 等更复杂的算子留到 u7-l2、u7-l3、u7-l4。

## 2. 前置知识

本讲承接 u1-l3（包结构与 wrapper 入口）与 u2-l1（TileLang 的 `@tilelang.jit` + `@T.prim_func` + `T.dynamic` 骨架）。在进入 mhc 之前，先回顾两件你应当已经熟悉的事，并补一个本讲独有的背景概念。

### 2.1 已建立、本讲直接复用的认知

- **wrapper 才是用户入口**：底层 `*_kernel.py` 里的 `@T.prim_func` 内核不是直接给用户调的，它们被包在 Python wrapper（往往再套一层 `torch.autograd.Function`）里。mhc 也不例外——`tile_kernels/mhc/__init__.py` 是空的，真正入口在 `tile_kernels/modeling/mhc/functional.py`。
- **编译期 vs 运行时**：`@tilelang.jit` 构造函数的 Python 参数（如 `mhc_mult`、`hidden`）被烤进编译产物；张量形状里的 `T.dynamic('num_tokens')` 是运行时符号，由启动时的张量提供具体值。这条切分在 mhc 的每个 kernel 里都会反复出现。
- **存储层级与搬运**：`T.alloc_fragment`（寄存器）/ `T.alloc_shared`（共享内存，写后需同步）/ `T.copy(..., disable_tma=True)`（关掉 TMA、走向量化搬运）这些原语在 mhc kernel 中大量使用，本讲不再重复解释它们的含义。

### 2.2 本讲独有背景：什么是 Hyper-Connection

标准 Transformer 的残差结构很朴素：

\[ x_{\text{out}} = x + \mathrm{sublayer}(x) \]

每个位置只有**一股**残差流，子层（attention 或 FFN）的输出直接加回这股流。Hyper-Connection（超连接）把「一股残差流」升级成「`mhc_mult` 股并行残差流」，并允许子层的输入和输出在这多股流之间做**加权混合**。直觉上有两点收益：

1. **更深的残差表达力**：不同股可以携带不同语义的信号，子层在融合时能选择性地读取/写入各股，而不是把所有信息挤进同一向量。
2. **更稳定的梯度流**：多股并行流提供了更多条从损失通向底层的「高速公路」，缓解深层网络的梯度衰减。

代价是残差张量多了一个维度：从 `(..., H)` 变成 `(..., mhc_mult, H)`，访存量也随之变大。mhc 系列算子的全部工作，就是把这「多股流的混合」做到 GPU 上、做到接近带宽极限。在 TileKernels 当前实现里，**`mhc_mult` 只有 4 是被保证可用的取值**（见 `post_kernel` 内的 `assert mhc == 4`），本讲后续以 `mhc_mult = 4` 为主例。

## 3. 本讲源码地图

本讲涉及的关键文件及其职责：

| 文件 | 作用 |
| --- | --- |
| [tile_kernels/modeling/mhc/functional.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/functional.py) | mhc 的**用户入口层**。把底层 op 组合成 `expand_from_embedding` / `mhc_pre` / `mhc_head` 三个可调用函数，是理解整条流水线的总纲。 |
| [tile_kernels/mhc/expand_kernel.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/expand_kernel.py) | 残差流**展开**算子。前向把 `(...,H)` 复制为 `(...,mhc_mult,H)`，反向沿 `mhc_mult` 维求和。 |
| [tile_kernels/mhc/norm_fn_kernel.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/norm_fn_kernel.py) | mhc 的**归一化函数 norm_fn**：对残差做分组 RMS 归一化的线性投影，产出后续要切分的「混合系数 mixes」。 |
| [tile_kernels/mhc/post_kernel.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/post_kernel.py) | 子层输出**重组回多残差流**的 post 算子（前向 + 反向），是 pre 的逆方向收尾。 |
| [tile_kernels/modeling/mhc/ops/expand.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/ops/expand.py) | expand 的 `torch.autograd.Function` 封装与 `expand_to_mhc` wrapper。 |
| [tile_kernels/torch/mhc.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/torch/mhc.py) | 全部 mhc 算子的纯 PyTorch 参考实现，供测试对拍（本讲实践要用到 `expand_to_mhc_ref`、`mhc_post_ref`）。 |

调用层次（自顶向下）：`functional.py`（入口）→ `ops/*.py`（autograd 封装）→ `mhc/*_kernel.py`（TileLang 内核）。这正延续了 u1-l3 讲的「包入口 → 子模块 wrapper → kernel」三级结构。

## 4. 核心概念与源码讲解

### 4.1 mhc 流水线全貌与 functional 编排

#### 4.1.1 概念说明

mhc 把一层 Transformer 子层的计算拆成 **expand → pre → sublayer → post** 四段。用符号写，设 `M = mhc_mult`：

1. **expand**（进入网络时调用一次）：把原始嵌入 `x: (..., H)` 复制成 `M` 股，得到残差 `R: (..., M, H)`。
2. **pre**（每个子层前调用一次）：吃进残差 `R` 和本子层的权重 `fn`，算出一组「混合系数」，再把 `M` 股残差按系数加权求和，压成单股 `layer_input: (..., H)` 喂给子层。同时把供 post 用的系数打包成一个不透明 `ctx` 元组 `(post_mix, comb_mix)` 返回。
3. **sublayer**：标准 attention 或 FFN，输入输出都是 `(..., H)`，**对多股流毫无感知**。
4. **post**（每个子层后调用一次）：吃进子层输出 `x: (..., H)`、残差 `R`、以及 pre 返回的 `ctx`，把单股输出重新展开并混合回 `M` 股残差 `R_{\text{new}}: (..., M, H)`，作为下一层的残差。

一句话：**pre 把多股压成一股喂子层，post 把一股输出展开混回多股**；多股流只在 pre/post 之间存在，子层本身不变。

`norm_fn` 是 pre 内部的第一步——它用权重 `fn` 对残差做投影并做分组 RMS 归一化，产出后续要切分成 `pre_mix / post_mix / comb_mix` 的原始 `mixes`。所以本讲讲的三个算子在流水线里的位置是：

```
       expand                pre (norm_fn 是其中第一步)            post
x(.,H) ──────► R(.,M,H) ──► layer_input(.,H), ctx=(post,comb) ──► R_new(.,M,H)
                        │            │                              ▲
                        └─ norm_fn ──┘                              │
                          产出 mixes                                 │
                                          sublayer(x) ───────────────┘
```

`functional.py` 就是这条流水线的「总装车间」，它把底层 op 按上面顺序拼起来，并对外暴露 `expand_from_embedding`、`mhc_pre`、`mhc_head` 三个函数。

#### 4.1.2 核心流程

`mhc_pre` 的关键设计是**训练 / 推理两条路径**，由 `torch.is_grad_enabled()` 切换：

```text
mhc_pre(residual, fn, scale, base, ...):
    if 推理模式 (no_grad):
        layer_input, post_mix, comb_mix = mhc_pre_big_fuse(...)   # 一个大融合 kernel，把下面 4 步合一
        return layer_input, (post_mix, comb_mix)
    else:  # 训练模式
        mixes     = mhc_pre_norm_fn(residual, fn, norm_weight, eps)   # ① norm_fn：归一化投影
        pre_mix, post_mix, comb_mix = mhc_pre_split_mixes(mixes, scale, base, ...)  # ② 切分+sigmoid
        comb_mix  = sinkhorn_normalize(comb_mix, ...)                 # ③ 行列归一化（u7-l3 详讲）
        layer_input = mhc_pre_apply_mix(residual, pre_mix)            # ④ 加权求和压成单股
        return layer_input, (post_mix, comb_mix)
```

推理路径用 `pre_big_fuse` 把 4 步融成一个 kernel（省中间访存）；训练路径必须拆开，因为 sinkhorn 需要自定义反向（u7-l3）、各步还要单独存反向所需的中间量。这正是 u2-l1 讲的「编译期/运行时」之外、mhc 特有的「训练/推理特化」切分。

`mhc_head` 是流水线在 lm_head（语言模型输出头）处的变体：它和 `mhc_pre` 共用 `mhc_pre_norm_fn`，但不做完整的 split/sinkhorn/apply，而是用一个更轻的 `mhc_head_compute_mix`，并且会先把窄权重 `fn` 用 `F.pad` 补零到与 block 级相同的宽度以**复用 norm_fn kernel**。

#### 4.1.3 源码精读

**入口 `expand_from_embedding`**——它的 docstring 把本讲的核心变换说得最清楚：

[functional.py:14-27](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/functional.py#L14-L27) 定义入口函数，把 `(..., H)` → `(..., mhc_mult, H)` 的变换委托给底层 `expand_to_mhc`，并明确注释「目前只有 `mhc_mult=4` 被保证可用」。

**`mhc_pre` 的两条路径切换**：

[functional.py:69-82](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/functional.py#L69-L82) 是推理分支：`if not torch.is_grad_enabled()` 时直接调 `mhc_pre_big_fuse`，把整段 pre 融合成一个 kernel，返回 `(post_mix, comb_mix)` 不透明元组。

[functional.py:84-105](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/functional.py#L84-L105) 是训练分支：依次调用 `mhc_pre_norm_fn`（第 84 行，即 4.3 节要讲的 norm_fn）、`mhc_pre_split_mixes`（第 92 行）、`sinkhorn_normalize`（第 101 行）、`mhc_pre_apply_mix`（第 103 行）。注意第 105 行返回的 `ctx = (post_mix, comb_mix)` 正是 `mhc_post` 要消费的——pre 和 post 通过这个元组耦合。

**`mhc_head` 的补零复用技巧**：

[functional.py:141-160](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/functional.py#L141-L160) 中，第 141 行算出 block 级权重宽度 `mhc_mult3 = mhc_mult * (2 + mhc_mult)`，第 143-144 行用 `F.pad` 把 lm_head 较窄的 `fn` 补到这个宽度，从而**直接复用** `mhc_pre_norm_fn` kernel；第 154 行再 `mixes[..., :mhc_mult]` 只取前 `mhc_mult` 列用于 head。

#### 4.1.4 代码实践

**实践目标**：把 `mhc_pre` 在训练模式下依次调用的底层 op 列出来，标注每个 op 的职责。

**操作步骤**：

1. 打开 [functional.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/functional.py)，定位训练分支（第 84-105 行）。
2. 对每一行调用，跳转到对应 `ops/*.py` 看它的 autograd 封装，再跳到 `mhc/*_kernel.py` 看 TileLang 内核。

**需要观察的现象 / 预期结果**：应得到一张形如下表的映射（答案见 4.1.5）。

| 调用 | 产出 | 对应底层 kernel |
| --- | --- | --- |
| `mhc_pre_norm_fn` | `mixes` | `norm_fn_kernel.py` |
| `mhc_pre_split_mixes` | `(pre_mix, post_mix, comb_mix)` | `pre_split_mixes_kernel.py` |
| `sinkhorn_normalize` | 归一化后的 `comb_mix` | `sinkhorn_kernel.py` |
| `mhc_pre_apply_mix` | `layer_input` | `pre_apply_mix_kernel.py` |

> 本实践为「源码阅读型」，无需 GPU；若要实际运行 `mhc_pre` 需 SM90/SM100 硬件，运行结果待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么推理路径敢用 `pre_big_fuse` 融合，训练路径却不敢？

> **答案**：推理不需要反向，中间值无需单独保留，融合能省访存；训练时 sinkhorn 等步骤要手写反向（u7-l3），且各步要保存 `save_for_backward` 的中间量，必须保持拆分形态才能正确回传梯度。

**练习 2**：`mhc_pre` 返回的 `ctx = (post_mix, comb_mix)` 为什么是不透明元组？

> **答案**：它只是 pre 产出、post 消费的中间载体，对调用方无意义；用元组打包是为了让 `mhc_pre` 的返回签名固定为 `(layer_input, ctx)`，无论内部需要传几个系数都不破坏接口。

---

### 4.2 expand_kernel：残差流的展开与反向求和

#### 4.2.1 概念说明

`expand` 是 mhc 流水线的第一步，数学上极其简单——**复制**：

\[ o_{i, m, j} = x_{i, j}, \quad m \in \{0,\dots,M-1\} \]

即把 `(..., H)` 的张量沿新增的 `mhc_mult` 维复制 `M` 份，得到 `(..., M, H)`。难点不在数学，而在「如何高效地在 GPU 上做一次纯带宽的广播写」。它的反向同样简单——前向是复制（一对多），反向就是**沿 `mhc_mult` 维求和**（多对一）：

\[ \nabla x_{i,j} = \sum_{m=0}^{M-1} \nabla o_{i,m,j} \]

这正是自动微分对「广播/复制」算子的标准反向规则。

#### 4.2.2 核心流程

前向 kernel 的策略是「读一次、写 `M` 次」：把输入的一小块 `(blk_n, blk_h)` 读进寄存器 fragment，然后串行遍历 `mhc_mult` 个输出通道，每个通道都把这块 fragment 写到全局内存的对应位置。用伪代码表示：

```text
grid: (ceildiv(n, blk_n), ceildiv(h, blk_h)),  每个 block 处理一块
for (pid_i, pid_j) in grid:
    if n > 0:                              # 零规模守卫
        xl = load x[pid_i*blk_n : , pid_j*blk_h :]    # 读一次到 fragment
        for m in serial(mhc_mult):          # 串行遍历 M 股
            parallel(ti, tj):               # 并行写出这块
                o[pid_i*blk_n+ti, m, pid_j*blk_h+tj] = xl[ti, tj]
```

反向则反过来——「读 `M` 次、累加、写一次」：开一个 fp32 累加 fragment 并清零，串行遍历 `M` 股把梯度累加进去，最后写回 `x_grad`。

注意循环嵌套的选择（承接 u2-l3）：`mhc_mult` 维用 `T.serial`（轮间共享同一 fragment、有隐式数据依赖），`(blk_n, blk_h)` 用 `T.Parallel`（写各元素彼此独立、分派线程并行）。

#### 4.2.3 源码精读

**前向 kernel** [expand_kernel.py:6-31](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/expand_kernel.py#L6-L31)：

- 第 8 行 `n = T.dynamic('num_tokens')` 把 token 数声明为运行时符号（u2-l1 讲过），`hidden` 与 `mhc_mult` 则是编译期参数。
- 第 12-13 行固定分块 `blk_n = 32`、`blk_h = 128`，第 20 行据此定义二维网格。
- 第 21 行 `if n > 0` 是零规模守卫——`n` 是运行时符号，这个判断在 kernel 内用符号值保护空输入。
- 第 22-23 行把一块输入 `T.copy` 进 `alloc_fragment` 寄存器（仅读一次全局内存）。
- 第 24-29 行 `for m in T.serial(mhc_mult)` 串行遍历输出股，内层 `T.Parallel(blk_n, blk_h)` 把同一块 fragment 广播写到每个 `m` 切片。这是「读一次写 `M` 次」的关键。

**反向 kernel** [expand_kernel.py:34-60](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/expand_kernel.py#L34-L60)：

- 第 50-51 行开 fp32 累加器 `xgl` 并 `T.fill(xgl, 0)`（用 fp32 累加保证精度，输入梯度是 bf16）。
- 第 52-57 行 `for m in T.serial(mhc_mult)` 把每个 `m` 切片的梯度 `+=` 进累加器——这就是反向求和。
- 第 58 行把累加结果写回 `x_grad`。

**autograd 封装** [ops/expand.py:6-34](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/ops/expand.py#L6-L34)：`ExpandToMHCFn`（第 6-26 行）的 forward 在第 14-19 行分配输出 `(..., mhc_mult, H)`、`flatten(0, -2)` 抹掉前导 batch 维后启动前向 kernel；backward（第 21-26 行）启动反向 kernel，返回 `(hidden_grad, None, None)`——后两个 `None` 对应 `mhc_mult` 和 `out` 这两个非张量输入。wrapper `expand_to_mhc`（第 29-34 行）就是 `ExpandToMHCFn.apply(...)`。这正是 u8-l1 要系统讲的 `autograd.Function` 封装范式的一个最简实例。

#### 4.2.4 代码实践

**实践目标**：亲手写一个 `expand_from_embedding` 的 PyTorch 参考，与 kernel 输出对拍（前向复制 + 反向求和）。

**操作步骤**：把下面脚本存为 `expand_check.py`（示例代码，非项目原有文件）并运行。

```python
# 示例代码
import torch
from tile_kernels.modeling.mhc import expand_from_embedding   # 被测 kernel 入口
from tile_kernels.torch.mhc import expand_to_mhc_ref           # 项目自带参考，用于三方对拍

# 你自己写的参考实现：unsqueeze 出 mhc 维再 expand+contiguous
def my_expand_ref(x: torch.Tensor, mhc_mult: int) -> torch.Tensor:
    return x.unsqueeze(-2).expand(*x.shape[:-1], mhc_mult, x.shape[-1]).contiguous()

torch.manual_seed(0)
n0, n1, h, mhc = 1, 1024, 1280, 4
x = torch.randn(n0, n1, h, dtype=torch.bfloat16, device='cuda')

out_kernel = expand_from_embedding(x, mhc)
out_ref    = my_expand_ref(x, mhc)
out_ref2   = expand_to_mhc_ref(x, mhc)

torch.testing.assert_close(out_kernel, out_ref)
torch.testing.assert_close(out_kernel, out_ref2)
print("forward match: OK")

# 反向：复制算子的反向是沿 mhc 维求和
o_grad = torch.randn_like(out_kernel)
x_kernel = x.clone().requires_grad_()
o_kernel = expand_from_embedding(x_kernel, mhc)
torch.autograd.backward([o_kernel], [o_grad])

x_ref = x.clone().requires_grad_()
o_ref = my_expand_ref(x_ref, mhc)
torch.autograd.backward([o_ref], [o_grad])

torch.testing.assert_close(x_kernel.grad, x_ref.grad)   # 期望: x.grad == o_grad.sum(dim=mhc 维)
print("backward match: OK")
```

**需要观察的现象 / 预期结果**：

- 前向三者（kernel / 你的参考 / 项目参考）逐元素相等（复制操作无舍入，可位精确）。
- 反向 `x_kernel.grad` 等于 `o_grad` 沿 `mhc_mult` 维求和，与 `my_expand_ref` 的 autograd 结果一致。

**运行结果**：本脚本需 SM90/SM100 GPU 与 tilelang 环境才能跑通；具体数值待本地验证。若暂无 GPU，可先把 `expand_from_embedding` 换成 `my_expand_ref` 单独验证你写的参考逻辑正确（纯 PyTorch，CPU 可跑）。

#### 4.2.5 小练习与答案

**练习 1**：前向 kernel 里为什么 `mhc_mult` 维用 `T.serial`、而 `(blk_n, blk_h)` 用 `T.Parallel`？反过来写会怎样？

> **答案**：`mhc_mult` 维的各轮复用同一块 fragment（隐式数据依赖、且只是重复写），必须串行；`(blk_n, blk_h)` 的每个元素写不同地址、彼此独立，适合并行分派线程。若反过来对 `mhc_mult` 用 `T.Parallel`，多股会竞争同一 fragment 寄存器且语义上也不需要并行（瓶颈在带宽不在计算）。

**练习 2**：反向 kernel 为什么用 fp32 累加器 `xgl`，而输入输出都是 bf16？

> **答案**：反向要把 `M` 份 bf16 梯度相加，连加 bf16 会丢精度；用 fp32 累加器把每次加法升精度，最后再转回 bf16 写出，是带宽类算子保精度的常规做法（与量化/engram 里的做法一致）。

---

### 4.3 norm_fn_kernel：分组 RMS 归一化投影

#### 4.3.1 概念说明

`norm_fn` 是 `mhc_pre` 的第一步，它的职责是：**用权重 `fn` 对多股残差做线性投影，并对投影做分组 RMS 归一化**，产出一组「混合系数雏形」`mixes`。它是 mhc 区别于普通 RMSNorm 的核心——普通 RMSNorm 只做归一化，而 norm_fn 是「归一化 + 投影」合一，且归一化是**分组**进行的。

设残差 `R` 沿 hidden 维被切成若干 RMS 组（每组 `rms_group_size` 个元素），权重 `fn` 形状为 `(mhc_mult3, mhc_hidden)`，其中 `mhc_mult3 = mhc_mult*(mhc_mult+2)`、`mhc_hidden = mhc_mult*hidden`。直观上：

\[ \text{mixes}_{i,m} = \sum_{k}\left(\sum_{g} R_{i,k,g}\, f_{m,k,g}\right) \cdot \mathrm{rsqrt}\!\left(\tfrac{1}{|g|}\sum_{g} R_{i,k,g}^{2} + \varepsilon\right) \]

其中 `m` 遍历 `mhc_mult3` 个输出通道，`k` 遍历 `mhc_mult` 股残差，`g` 遍历组内元素。也就是说：先在每组内做「点积 + 平方和」，再用平方和的 rsqrt 归一化该组的点积，最后跨组求和。`mhc_mult3 = mhc_mult*(mhc_mult+2)` 这个数后面会被 `pre_split_mixes` 切成 `pre_mix`（`mhc_mult`）、`post_mix`（`mhc_mult`）、`comb_mix`（`mhc_mult*mhc_mult`）三段。

此外还有一个**权重归一化合并**的小算子：当传入可选的 `norm_weight` 时，先把它逐元素乘进 `fn`（`out_fn = fn * normw`），避免在主 kernel 里再乘一次。

#### 4.3.2 核心流程

norm_fn 的前向被拆成两个 kernel（**先乘后归一**），这是典型的 split-K 友好拆分：

```text
① _mhc_fn_normw_merge_fwd（可选）: out_fn[m,k] = fn[m,k] * normw[k]      # 权重合并
② _mhc_pre_norm_fn_fwd_mul:        out_mul[token,group,m] += Σ_g x·f      # 分组点积
                                    sqrsum[token,group]    += Σ_g x²        # 分组平方和
③ _mhc_pre_norm_fn_fwd_norm:       out[token,m] = Σ_group out_mul·rsqrt(sqrsum/|g|+eps)
```

第 ② 步是计算密集型（用 `T.gemm` 走 tensor core），第 ③ 步是带宽密集型（逐 token 归一化求和）。把它们拆开，是因为 split-K（多 SM 分担同一个 GEMM）只在 ② 受益，而 ③ 是逐 token 独立、用一维网格处理。注意 [ops/norm_fn.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/ops/norm_fn.py) 第 87-91 行有一句重要注释：**TileLang 实现暂不支持 split-K，所以 `n_splits` 实际被强制改成 1**——若要 split-K 收益需换 DeepGEMM 实现。本讲只读前向两个 kernel，反向与 split-K 细节留作了解。

#### 4.3.3 源码精读

**权重合并** [norm_fn_kernel.py:11-28](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/norm_fn_kernel.py#L11-L28)：极简的逐元素 kernel，第 26 行 `out_fn[pid_m, i_n] = fn[pid_m, i_n] * normw[i_n]`，把 `norm_weight` 在 hidden 维广播乘进 `fn`。注意 `normw` 只有一维（按 hidden 索引），与 `fn` 的最后一维对齐。

**分组点积 + 平方和** [norm_fn_kernel.py:64-123](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/norm_fn_kernel.py#L64-L123)：

- 第 84 行的二维网格 `(ceildiv(num_tokens, token_block), n_rms_group)`——每个 block 处理一块 token × 一个 RMS 组。
- 第 89 行 `T.Pipelined(rms_group_size // hidden_block, num_stages=2)` 把组内分块用 2 级软件流水（重叠 load 与 compute）。
- 第 103-105 行手写循环累加平方和 `sqrsum_part += x*x`（分 4 路部分和，最后再 `T.reduce_sum`，第 115-116 行）。
- 第 107-114 行用 `T.gemm(x_frag, fn_smem, out_frag, transpose_B=True, clear_accum=False)` 调 tensor core 做分组点积，`clear_accum=False` 表示跨组内分块累加。
- 第 119-121 行把 `out_frag` 的前 `mhc_mult3`（代码里写死 24，对应 `mhc_mult=4` 时 `4*(4+2)=24`）列写回全局，第 120 行 `if j < 24` 是因为 fragment 按 32 对齐、多出来的 8 列是填充。

**归一化求和** [norm_fn_kernel.py:126-165](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/norm_fn_kernel.py#L126-L165)：一维网格，每个 token 一个 block。第 150-161 行的 `for k in T.serial(n_rms_group)` 跨组累加：第 156 行 `rms[0] = T.rsqrt(rms[0] / rms_group_size + rms_eps)` 算出该组的 rsqrt，第 157-161 行用它归一化 `out_l0` 并累加进 `out_l`，第 163 行写出最终 `mixes`。

**PyTorch 参考** [torch/mhc.py:66-85](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/torch/mhc.py#L66-L85)：第 78-84 行用 `einsum('mbk,nbk->mbn', ...)` 算分组点积、第 83 行算平方和、第 84 行用 `rsqrt` 归一化求和——与上面三个 kernel 的数学完全对应，是对拍依据。

#### 4.3.4 代码实践

**实践目标**：理解 norm_fn 为何要拆成「先乘后归一」两步，并对照参考实现确认其数学。

**操作步骤**：

1. 阅读 [torch/mhc.py:66-85](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/torch/mhc.py#L66-L85) 的 `mhc_pre_norm_fn_ref`，把它的 `einsum`、`square().sum()`、`rsqrt` 三步分别对应到 `_mhc_pre_norm_fn_fwd_mul` 与 `_mhc_pre_norm_fn_fwd_norm` 两个 kernel。
2. 运行现有对拍测试：`pytest tests/mhc/test_norm_fn.py -n 4`（见 [test_norm_fn.py:49-92](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/mhc/test_norm_fn.py#L49-L92)）。

**需要观察的现象 / 预期结果**：

- 参考（启用 `allow_tf32`）与 kernel 输出 `torch.testing.assert_close(..., atol=1e-3, rtol=1e-3)` 通过——norm_fn 涉及 GEMM 与 rsqrt，是浮点近似而非位精确，故容差比 expand 宽松。
- 注意测试里 `fn` 被放缩了 `*1e-4`（[test_norm_fn.py:25-28](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/mhc/test_norm_fn.py#L25-L28)），这是为了模拟真实训练里投影权重的小量级。

**运行结果**：需 GPU 环境，具体通过/失败待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 norm_fn 不直接写成一个 kernel，而要拆成 `fwd_mul`（GEMM）和 `fwd_norm`（归一化）两个？

> **答案**：两者计算特性不同——`fwd_mul` 是计算密集型，用 `T.gemm` 走 tensor core、可受益于 split-K 多 SM 分担；`fwd_norm` 是带宽密集型、逐 token 独立。拆开后各自选最优网格与并行策略；此外 `fwd_norm` 还要把 split-K 的多份部分和合并，这只能在 GEMM 之后做。

**练习 2**：`out_frag` 形状是 `(token_block, 32)`，但只写回前 24 列（`if j < 24`），为什么？

> **答案**：`mhc_mult=4` 时 `mhc_mult3=24`，但 `T.gemm`/fragment 为对齐 tensor core（通常按 8 或 16 对齐）按 32 宽分配，多出的 8 列是补零填充；写回时用 `if j < 24` 只保留真实通道，避免把填充值写进 `mixes`。

---

### 4.4 post_kernel：子层输出重组回多残差流

#### 4.4.1 概念说明

`post` 是 `pre` 的逆方向收尾：子层输出是单股 `x: (..., H)`，post 要把它和 `M` 股残差重新混合，得到新的多股残差 `R_new: (..., M, H)`。核心数学（对每个 token `i`、输出股 `m_o`、hidden `h`）：

\[ y_{i, m_o, h} \;=\; \underbrace{c_{i,m_o}\, d_{i,h}}_{\text{子层输出加权}} \;+\; \underbrace{\sum_{m_i=0}^{M-1} a_{i,m_i,m_o}\, b_{i,m_i,h}}_{\text{多股残差交叉混合}} \]

其中：

- \(d = x\)：子层输出，形状 `(n, H)`；
- \(c\) = `post_layer_mix`：子层输出在每股的权重，形状 `(n, M)`，由 pre 切分而来；
- \(b\) = `residual`：进入本层前的多股残差，形状 `(n, M, H)`；
- \(a\) = `comb_res_mix`：`M×M` 的股间混合矩阵，形状 `(n, M, M)`，由 pre 切分并经 sinkhorn 归一化而来。

直觉：新残差 = （子层输出按股加权）+（旧残差按 `comb_mix` 矩阵在各股间重新分配）。第一项是「子层贡献」，第二项是「残差流的重新洗牌」。这与标准残差 `x + sublayer(x)` 的关系是：当 `M=1`、`c=1`、`a=1` 时，上式退化为 `y = x + residual`，即普通残差。所以 **mhc 是普通残差在多股流上的推广**。

#### 4.4.2 核心流程

post 前向按 hidden 分块，每个 token 一个 block：

```text
grid: n (一维，每 token 一个 block), threads=n_thr
load a_local (M×M), c_local (M)        # 小矩阵全装进 fragment
for h_blk in Pipelined(ceildiv(h, h_blk), num_stages=2):   # hidden 分块 + 双缓冲
    load b_local (M×h_blk), d_local (h_blk)                 # 旧残差块 + 子层输出块
    parallel(m_o, h_in_blk):
        x_local[m_o, h] = c_local[m_o] * d_local[h]          # 第一项
        for m_i in serial(M):
            x_local[m_o, h] += a_local[m_i, m_o] * b_local[m_i, h]   # 第二项（股间混合）
    store x_local → x (新残差)
```

反向要对 4 个输入 `(x, residual=b, post_layer_mix=c, comb_res_mix=a)` 全部求梯度。因为 `a`、`c` 是小矩阵（与 hidden 无关），它们的梯度需要跨 hidden 维归约——用 `T.alloc_reducer`（u2-l3 讲过的跨线程累加器）在每个 block 内累加，最后 `finalize_reducer`。`b`、`d` 的梯度形状与原输入相同、带 hidden 维，直接逐元素写。注意反向 kernel 开头 `assert mhc == 4`：它针对 `M=4` 做了特化（循环展开成 4×4）。

#### 4.4.3 源码精读

**前向 kernel** [post_kernel.py:8-58](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/post_kernel.py#L8-L58)：

- 第 8-14 行的 `pass_configs` 关掉 warp specialize、限制 register usage、关 256-bit 向量化——post 计算密集度低，这些开关换更稳的代码生成。
- 第 19 行 `h_blk = math.gcd(hidden, h_blk)` 把块宽对齐到 hidden 的因子，保证整除分块。
- 第 29 行一维网格 `T.Kernel(n, threads=n_thr)`，每 token 一个 block。
- 第 38-42 行把小矩阵 `a (M×M)`、`c (M)` 整块装进 fragment，第 42 行 `T.pdl_sync()` 是 Hopper 的 block 级同步屏障。
- 第 44 行 `T.Pipelined(..., num_stages=2)` 双缓冲，第 45-46 行 `disable_tma=True` 走向量化 load。
- **核心计算第 50-53 行**：`x_local[m_o, h] = c_local[m_o]*d_local[h]`（第一项），再 `for m_i in serial: x_local += a_local[m_i,m_o]*b_local[m_i,h]`（第二项）——正是上面公式的直译。

**反向 kernel** [post_kernel.py:61-146](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/post_kernel.py#L61-L146)：

- 第 67 行 `out_idx=[5,6,7,8]`：声明第 5-8 个形参（`da,db,dc,dd`）为 kernel 自动分配并返回的输出张量。
- 第 70 行 `assert mhc == 4`，第 78-87 行形参直接写死 `4`。
- 第 106-109 行开 `da_reducer (4×4)` 与 `dc_reducer (4)` 两个跨线程累加器并清零。
- 第 122-126 行算 `db`（残差梯度，带 hidden）与 `da`（混合矩阵梯度，需跨 hidden 归约进 reducer）。
- 第 130-133 行算 `dc`（股权重梯度，进 reducer）与 `dd`（子层输出梯度，带 hidden 直接写）。
- 第 141-144 行 `finalize_reducer` 把 `da`、`dc` 的累加结果合并写出。

**wrapper** [post_kernel.py:149-181](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/post_kernel.py#L149-L181)（`mhc_post_fwd`）：第 158-169 行做一组 dtype/形状断言（`x`/`residual` 必须 bf16，`post_layer_mix`/`comb_res_mix` 必须 fp32），第 166-169 行强制连续，第 173-180 行取 JIT 编译产物并 `flatten(0,1)` 抹掉前两维（num_seqs, num_tokens）后启动 kernel。

**autograd 封装** [ops/post.py:6-35](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/ops/post.py#L6-L35)：`MHCPost.forward`（第 9-18 行）调 `mhc_post_fwd` 并 `save_for_backward` 四个输入；`backward`（第 20-25 行）调 `mhc_post_bwd` 返回 4 个梯度 + 一个 `None`（对应 `out` 参数）。

**PyTorch 参考** [torch/mhc.py:56-63](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/torch/mhc.py#L56-L63)：`mhc_post_ref` 用 `einsum('abmn,abmc->abnc', comb_res_mix, residual)` 算第二项（股间混合）、`x.unsqueeze(-2)*post_layer_mix` 算第一项，二者相加转回 bf16——与 kernel 公式完全对应。

#### 4.4.4 代码实践

**实践目标**：验证 post 的「第一项 + 第二项」结构与 PyTorch einsum 参考等价。

**操作步骤**：运行现有对拍测试 `pytest tests/mhc/test_post.py -n 4`，并阅读 [test_post.py:45-72](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/mhc/test_post.py#L45-L72)。

**需要观察的现象 / 预期结果**：

- 前向 `out_tl` 与 `out_ref` 用默认容差 `assert_close` 通过（bf16 计算，近似相等）。
- 反向对 `x`、`residual` 梯度用默认容差；对 `post_layer_mix`、`comb_res_mix` 梯度用更宽的 `atol=1e-4, rtol=1e-4`（第 61-72 行）——因为这两个是 fp32 小矩阵、反向经过 reducer 归约，容差需放宽。

**运行结果**：需 GPU，待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：把 post 公式里 `M=1`、`c=1`、`a=1`、`b=residual` 代入，会得到什么？说明 mhc 与普通残差的关系。

> **答案**：得到 `y = 1·x + Σ_{m_i=0}^{0} 1·residual = x + residual`，即标准 Transformer 残差。所以普通残差是 mhc 在 `mhc_mult=1` 时的退化特例，mhc 是它向多股流的推广。

**练习 2**：反向 kernel 里为什么 `da`、`dc` 要用 `alloc_reducer`，而 `db`、`dd` 不用？

> **答案**：`da (M×M)`、`dc (M)` 形状与 hidden 无关，但梯度需要对 hidden 维（被 `Pipelined` 分成多块）求和归约，故用跨线程 reducer 累加后 `finalize`；`db (M×H)`、`dd (H)` 带 hidden 维、每块各写各的位置无需归约，直接累加进带 hidden 维的 fragment 即可。

---

## 5. 综合实践

把本讲三个算子串起来，完成一个「手工模拟单层 mhc 前向」的小任务（**纯 PyTorch 源码阅读型实践**，无需 GPU，目的是验证你理解了流水线）。

**任务**：给定嵌入 `x: (1, T, H)`，用 `tile_kernels/torch/mhc.py` 里的参考实现，手工拼出 `expand → （简化版 pre：只做 norm_fn + apply_mix）→ 假子层 identity → post` 的前向，并回答：

1. 每一步张量形状如何变化？
2. post 输出形状为何又回到了 `(..., M, H)`？
3. 若把 `mhc_mult` 设为 1 且令 `pre_mix=1, post_mix=1, comb_mix=1`，整条流水退化为？

**参考拼装**（示例代码，非项目原有文件）：

```python
# 示例代码
import torch
from tile_kernels.torch.mhc import (
    expand_to_mhc_ref, mhc_pre_norm_fn_ref, mhc_pre_apply_mix_ref, mhc_post_ref,
)

T, H, M = 16, 1280, 4
x = torch.randn(1, T, H, dtype=torch.bfloat16, device='cuda')

# ① expand: (1,T,H) -> (1,T,M,H)
residual = expand_to_mhc_ref(x, M)

# ② 简化 pre: norm_fn 投影 + 直接拿前 M 列当 pre_mix
mhc_mult3 = M * (M + 2)
fn = torch.randn(mhc_mult3, M * H, dtype=torch.float32, device='cuda') * 1e-4
mixes = mhc_pre_norm_fn_ref(residual, fn, None, 1e-6)        # (1,T,mhc_mult3)
pre_mix = torch.sigmoid(mixes[..., :M]).unsqueeze(-1)        # (1,T,M,1)
layer_input = mhc_pre_apply_mix_ref(residual, pre_mix)       # (1,T,H)

# ③ 假子层: identity
sub_out = layer_input

# ④ post: (1,T,H) -> (1,T,M,H)
post_mix = torch.ones(1, T, M, 1, dtype=torch.float32, device='cuda')
comb_mix = torch.ones(1, T, M, M, dtype=torch.float32, device='cuda') / M
out = mhc_post_ref(sub_out, residual, post_mix, comb_mix)    # (1,T,M,H)
print(out.shape)   # 期望 torch.Size([1, 16, 4, 1280])
```

**预期结论**：

1. `(1,T,H) → expand → (1,T,M,H) → apply_mix → (1,T,H) → sublayer → (1,T,H) → post → (1,T,M,H)`。
2. post 把单股子层输出重新展开混回多股，故回到 `(..., M, H)`，作为下一层残差。
3. 退化时 `comb_mix=1` 使第二项为各股残差之和、`post_mix=1`，整体趋近 `x + sum(residual)`，即普通残差的多份叠加。

> 运行结果待本地验证（需 CUDA 环境；若只想验证形状逻辑，可把 `device='cuda'` 改 `'cpu'`，但 `mhc_pre_norm_fn_ref` 内有断言要求 float，需相应调整 dtype）。

## 6. 本讲小结

- **mhc 的本质**是把单股残差流升级为 `mhc_mult` 股并行流，pre 把多股压成一股喂子层、post 把子层输出展开混回多股；普通残差是 `mhc_mult=1` 的退化特例。
- **`mhc_mult` 维度**是新增的残差维度，当前实现只有 `mhc_mult=4` 被保证可用（post kernel 内 `assert mhc == 4`）。
- **expand** 前向「读一次写 `M` 次」复制、反向沿 `mhc_mult` 维求和；是纯带宽算子，用 `T.serial` 遍历股、`T.Parallel` 写元素。
- **norm_fn** 是「分组 RMS 归一化投影」，拆成 `fwd_mul`（tensor core GEMM + 平方和）与 `fwd_norm`（rsqrt 归一化求和）两步，产出 `mhc_mult3 = mhc_mult*(mhc_mult+2)` 个 mixes 通道。
- **post** 用公式 `y = c·d + a@b` 把子层输出与多股残差重组，反向对 `a/c` 小矩阵用 `alloc_reducer` 跨 hidden 归约、对 `b/d` 带 hidden 梯度直接写。
- **functional.py** 是总装车间：`mhc_pre` 用 `torch.is_grad_enabled()` 在推理（`pre_big_fuse` 融合）与训练（四步拆分）间切换，并通过 `ctx=(post_mix, comb_mix)` 元组把 pre 与 post 耦合。

## 7. 下一步学习建议

本讲只读了 mhc 流水线里最基础的 expand / norm_fn / post 三个算子和总装入口。建议下一步：

- **u7-l2（MHC 前处理流水线与融合）**：精读 `pre_split_mixes` / `pre_apply_mix` / `pre_big_fuse`，搞清 `mixes` 如何被切分成 `pre_mix/post_mix/comb_mix` 三段，以及推理路径的大融合 kernel 长什么样。
- **u7-l3（Sinkhorn 归一化前向 + 自定义反向）**：这是 mhc 里数学最绕的部分，讲清为何迭代行列归一化必须手写反向、如何保存 `xs[step]/sums[step]` 逆序回传。
- **u8-l1（autograd.Function 封装范式）**：本讲的 `ExpandToMHCFn` / `MHCPost` 是最简实例，u8-l1 会以 engram 为例系统讲 `save_for_backward`、`main_grad` 就地累加与返回 `None` 的优化约定。
- 继续阅读 [functional.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/functional.py) 的 `mhc_head`，对照本讲的 `mhc_pre`，体会 lm_head 处如何用 `F.pad` 复用 norm_fn kernel。
