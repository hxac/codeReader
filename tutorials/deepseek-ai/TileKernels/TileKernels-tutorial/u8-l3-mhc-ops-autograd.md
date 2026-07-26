# ops 层 autograd 封装（sinkhorn / head_compute_mix）

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 mhc 包「四层」结构里 `modeling/mhc/ops/` 这一层的定位：它是**用 `torch.autograd.Function` 把一个底层 TileLang kernel 包成可微算子**的「桥接层」，介于 kernel 与 functional 之间。
- 看懂 `ops/__init__.py` 作为一个**纯再导出（re-export）枢纽**如何聚合 9 个算子，并对比它与 `functional.py`（做组合编排）的职责分工。
- 用 `_SinkhornNormalize` 作为「极简桥接」样板，理解一种典型技巧：**前向把编译好的反向 kernel 挂到 `ctx`，反向只做一次启动**，避免重复查 JIT 缓存。
- 精读 `MHCHeadComputeMix` 的反向，掌握本讲的核心模式——**当某个梯度输出「小而无 token 维」（如标量/小向量参数）时，让每个 SM 各写一份 `*_grad_partial`，再在 Python 层 `.sum(0)` 聚合**，从而规避海量 block 对同一地址的原子竞争。
- 把这一层与 engram 的 `grad_w_reduce`、`post` 的「kernel 内直接归约」做横向对比，知道**三种反向归约策略各自的适用边界**。

本讲只讲「ops 层桥接与反向归约」，不重复讲 sinkhorn kernel 的迭代数学与逆序回传细节（那是 u7-l3 的内容），也不重复讲 autograd.Function 的基础契约（那是 u8-l1 的内容）。

## 2. 前置知识

本讲承接 **u8-l1（autograd.Function 封装范式）** 与 **u7-l3（Sinkhorn 前向 + 自定义反向）**。进入正文前，先对齐三件事：

- **底层 kernel 对 autograd 是黑盒**（u8-l1）。TileLang 编译出的 CUDA 代码没有求导规则，要参与 `loss.backward()`，必须在 modeling 层套一个 `torch.autograd.Function`：`forward` 算输出并存盘，`backward` 接上游梯度算每个输入的梯度，返回元组与 `forward` 输入**逐位一一对应**（非张量输入用 `None` 占位）。
- **Sinkhorn 反向用了「重计算（rematerialization）」**（u7-l3）：前向只存输入 `x`，反向 kernel 从 `x` 出发把整条前向链重算一遍再逆序回传，用算力换显存。本讲会看到 head_compute_mix 在更小尺度上**复用了同一个思想**——前向不存 sigmoid 输出，反向重算一个廉价的逐元素 sigmoid。
- **`T.alloc_reducer` + `T.Persistent`**（u2-l3、u6-l1）。reducer 是跨线程的自定义累加器，配 `fill` / `finalize_reducer` 三步用；`T.Persistent` 是「持久化 kernel」循环，让一个 block 常驻、循环扫多片数据。本讲 head_compute_mix 的反向正是「每 SM 一个持久化 block + 一个 reducer」的结构。

> 记号约定：本讲用 \(x\) 表示 `input_mix`，\(s\) 表示标量 `mhc_scale[0]`，\(b_j\) 表示 `mhc_base[j]`，\(\varepsilon\) 表示 `mhc_pre_eps`；\(\sigma\) 是 sigmoid，\(\bar{\cdot}\) 表示上游梯度（grad of loss w.r.t. 某量）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [tile_kernels/modeling/mhc/ops/\_\_init\_\_.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/ops/__init__.py) | **ops 层入口**。把 9 个算子模块的对外函数纯再导出，构成 ops 层的「公共面孔」。 |
| [tile_kernels/modeling/mhc/ops/sinkhorn.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/ops/sinkhorn.py) | `_SinkhornNormalize`（autograd.Function）+ 对外函数 `sinkhorn_normalize`。本讲的「极简桥接」样板。 |
| [tile_kernels/modeling/mhc/ops/head_compute_mix.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/ops/head_compute_mix.py) | `MHCHeadComputeMix`（autograd.Function）+ 对外函数 `mhc_head_compute_mix`。本讲的「per-SM 分块 + `.sum(0)`」核心样板。 |
| [tile_kernels/mhc/head_compute_mix_kernel.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/head_compute_mix_kernel.py) | 被 head_compute_mix 调用的底层 fwd/bwd kernel 构造器，含持久化 block + reducer 的反向实现。 |
| [tile_kernels/mhc/sinkhorn_kernel.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/sinkhorn_kernel.py) | 被 sinkhorn 调用的 `_mhc_sinkhorn_fwd` / `_mhc_sinkhorn_bwd`（kernel 细节见 u7-l3，本讲不重述）。 |
| [tile_kernels/config.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/config.py) | `get_num_sms` / `set_num_sms`：head_compute_mix 反向用它决定分多少份 partial。 |
| [tile_kernels/modeling/mhc/functional.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/functional.py) | 调用点：`mhc_pre` 调 `sinkhorn_normalize`、`mhc_head` 调 `mhc_head_compute_mix`，体现 ops→functional 的依赖方向。 |

调用方向一句话：`functional.mhc_pre / mhc_head` → `ops.sinkhorn_normalize / mhc_head_compute_mix` → `_SinkhornNormalize.apply / MHCHeadComputeMix.apply` → 底层 TileLang kernel。

## 4. 核心概念与源码讲解

本讲把 ops 层拆成三块：**4.1 ops 层的定位与 `__init__` 纯再导出**、**4.2 极简桥接 `_SinkhornNormalize`**、**4.3 带 per-SM 归约的桥接 `MHCHeadComputeMix`**。

### 4.1 ops 层的定位：纯再导出与 ops/functional 分工

#### 4.1.1 概念说明

回忆 u1-l3 讲过的「四层结构」。把 mhc 单独放大，可以看到一条更细的依赖链：

```
tile_kernels/mhc/*_kernel.py            ← 第 1 层：TileLang DSL 写的算子（数学）
tile_kernels/modeling/mhc/ops/*.py      ← 第 2 层：autograd.Function 桥接（本讲主角）
tile_kernels/modeling/mhc/ops/__init__.py ← 第 2 层入口：纯再导出 9 个算子
tile_kernels/modeling/mhc/functional.py ← 第 3 层：把多个 op 组合成用户入口
tile_kernels/modeling/mhc/__init__.py   ← 第 4 层：只导出 3 个 functional 入口
```

每一层都「只做一件事、把更复杂的事留给上一层」：

- **ops 层**：每个 `.py` 文件 = **一个** `torch.autograd.Function` + 一个对外小函数。它只负责「让这一个算子可微」，**不做组合、不写算子数学**。算子数学在第 1 层 kernel 里，组合编排在第 3 层 functional 里。
- **functional 层**：本身**不写任何 `autograd.Function`**（u8-l2 已强调），只把 ops 层的若干可微算子按配方串成 `mhc_pre` / `mhc_head` 等入口；可微性全部来自 ops 层的链式组合。

> 这正是 u8-l1 那句口诀的细化：**modeling 层只做「可微封装」，不写算子逻辑**。在 mhc 里这条职责被进一步切成 ops（单算子封装）与 functional（多算子组合）两小层。

#### 4.1.2 核心流程

`ops/__init__.py` 的全部内容就是 9 行 `from .xxx import yyy`：

```text
ops/__init__.py
  ├─ expand          → expand_to_mhc
  ├─ head_compute_mix→ mhc_head_compute_mix        ← 本讲精读
  ├─ multilayer_recompute → mhc_multilayer_recompute
  ├─ norm_fn         → mhc_pre_norm_fn
  ├─ post            → mhc_post / mhc_post_bwd / mhc_post_fwd
  ├─ pre_apply_mix   → mhc_pre_apply_mix
  ├─ pre_big_fuse    → mhc_pre_big_fuse
  ├─ pre_split_mixes → mhc_pre_split_mixes
  └─ sinkhorn        → sinkhorn_normalize          ← 本讲精读
```

它不定义任何新符号，只把 9 个模块的对外函数聚到一处，让外部消费者可以 `from tile_kernels.modeling.mhc.ops import sinkhorn_normalize` 一次性拿到任意 op。functional.py 则**直接从各 op 模块**导入（`from .ops.sinkhorn import sinkhorn_normalize`），效果等价——`__init__` 主要服务于「想单独用一个 op」的外部调用方。

#### 4.1.3 源码精读

[ops/\_\_init\_\_.py:1-9](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/ops/__init__.py#L1-L9) —— 9 行 `from .xxx import yyy`，没有任何其他逻辑。注意 `post` 模块一次导出了 3 个符号（`mhc_post` 是封装了 fwd 的 autograd.Function 别名，`mhc_post_fwd` / `mhc_post_bwd` 是裸的前向/反向 wrapper，给重算场景直接调用）。

与之对照，更上一层的 [modeling/mhc/\_\_init\_\_.py:1](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/__init__.py#L1) 只导出 `expand_from_embedding, mhc_head, mhc_pre` 这三个 functional 入口——它**有意不把 ops 层的 9 个算子暴露出去**，因为对最终用户而言，「一股残差流如何压成单股」是实现细节，对外只需 `mhc_pre` 这一个入口。这种「内层宽导出、外层窄导出」是分层封装的常见做法。

而 functional.py 的导入方向印证了依赖链——[functional.py:4-11](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/functional.py#L4-L11) 全部 `from .ops.<模块> import <函数>`，其中本讲的两个主角在 functional 里的调用点是：

- `mhc_pre`（训练分支）调 `sinkhorn_normalize`：[functional.py:101](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/functional.py#L101)
- `mhc_head` 调 `mhc_head_compute_mix`：[functional.py:158](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/functional.py#L158)

#### 4.1.4 代码实践

**实践目标**：用一张表把「ops 层 9 个算子」与「functional 3 个入口」的调用关系画出来，确认 functional 只组合、ops 只封装。

**操作步骤**：

1. 打开 [ops/\_\_init\_\_.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/ops/__init__.py#L1-L9)，把 9 个再导出的算子名抄下来。
2. 打开 [functional.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/functional.py)，在 `mhc_pre`（L30-105）与 `mhc_head`（L108-160）里grep 出它们各自调用了哪些 ops 层函数。
3. 列一张「functional 入口 → 用到的 ops 算子」对照表。

**需要观察的现象**：`mhc_pre` 的训练分支用到了 `mhc_pre_norm_fn`、`mhc_pre_split_mixes`、`sinkhorn_normalize`、`mhc_pre_apply_mix` 四个 ops 算子；推理分支换成单个 `mhc_pre_big_fuse`。`mhc_head` 用到了 `mhc_pre_norm_fn` 与 `mhc_head_compute_mix`。

**预期结果**：得到一张表，例如 `mhc_pre(train) = norm_fn → split_mixes → sinkhorn_normalize → pre_apply_mix`；`mhc_head = norm_fn → head_compute_mix`。functional 不出现任何 `class .* (torch.autograd.Function)`，证实它只组合不封装。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ops/__init__.py` 要把 9 个算子都导出，而 `modeling/mhc/__init__.py` 只导出 3 个？

**参考答案**：ops 层是「零件库」，宽导出方便复用单个零件（如别处只想用 `sinkhorn_normalize`）；mhc 包的对外入口是「成品」`mhc_pre/mhc_head/expand_from_embedding`，窄导出隐藏内部组合细节，降低用户认知负担与 API 面积。

**练习 2**：functional.py 能不能不依赖 ops 层、自己在文件里定义 `autograd.Function`？

**参考答案**：技术上能，但会破坏分层——那样 functional 既做组合又做封装，单文件膨胀、复用性变差。当前设计让「单算子可微」沉淀在 ops（可被任意 functional 组合复用），functional 保持纯组合，职责单一。

---

### 4.2 极简桥接 `_SinkhornNormalize`：把编译好的反向 kernel 挂到 ctx

#### 4.2.1 概念说明

`_SinkhornNormalize` 是 ops 层最薄的桥接样板。它的 forward / backward 几乎只做「分配输出 + 启动 kernel」。本节关注的是**桥接层的通用套路**，不是 sinkhorn 的迭代数学（已在 u7-l3 详述）。

桥接层有一个值得记住的小技巧：**前向一次性把 fwd 与 bwd 两个 kernel 都编译好，并把 `bwd_kernel` 挂到 `ctx` 上**。这样反向触发时，`backward` 直接 `ctx.bwd_kernel(...)` 启动，不必再去查 JIT 缓存、也不必重新传编译期参数构造 kernel 对象。虽然 TileLang 的 `@tilelang.jit` 本身有缓存、重复调用也只是命中缓存，但把 kernel 对象显式存到 `ctx` 让「前向编译、反向复用」的意图一目了然，也省掉一次缓存查找的开销。

这也是 u8-l1 「存盘分工」的一个新例子：**张量走 `save_for_backward`，非张量（包括编译好的 kernel 对象）直接挂 `ctx` 属性**。

#### 4.2.2 核心流程

```text
forward(ctx, x, repeat, eps):
  hidden_size = x.shape[1]                      # 实为矩阵边长 M（u7-l3 的命名陷阱）
  output = empty_like(x)
  fwd_kernel = _mhc_sinkhorn_fwd(hidden_size, 1, repeat, eps)   # 编译前向
  bwd_kernel = _mhc_sinkhorn_bwd(hidden_size, 32, repeat, eps)  # 编译反向
  ctx.save_for_backward(x)                      # 只存输入 x（重计算策略）
  ctx.bwd_kernel = bwd_kernel                   # 把编译产物挂到 ctx（非张量）
  fwd_kernel(x, output)
  return output

backward(ctx, grad_output):
  x = ctx.saved_tensors[0]
  grad_input = empty_like(x)
  ctx.bwd_kernel(grad_output, x, grad_input)    # 直接启动，无需再编译
  return (grad_input, None, None)               # 对应 forward 的 (x, repeat, eps)
```

对外函数 `sinkhorn_normalize` 负责**形状归一化**：把任意 `(..., M, M)` 输入 `view(-1, *x.shape[-2:])` 喂给 `apply`，再用 `view_as(x)` 还原——这与 u8-l1 里 `forward` 做 `view` 是同一思想，只是放在了对外函数这一层。

#### 4.2.3 源码精读

forward 编译两个 kernel 并存盘：[ops/sinkhorn.py:8-21](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/ops/sinkhorn.py#L8-L21)。重点看三行：

- [L16-L17](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/ops/sinkhorn.py#L16-L17) 同时编译 `fwd_kernel`（`token_block_size=1`）与 `bwd_kernel`（`token_block_size=32`）。前向「每 block 1 个 token」、反向「每 block 32 个 token」的差异与动机已在 u7-l3 的 4.4.3 解释（反向更重，用更大 block 摊销重算开销）。
- [L18](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/ops/sinkhorn.py#L18) `ctx.save_for_backward(x)` —— **只存输入 `x`**，是重计算策略的直接体现（u7-l3）。
- [L19](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/ops/sinkhorn.py#L19) `ctx.bwd_kernel = bwd_kernel` —— 把编译好的 kernel 当普通属性存，反向直接用。

backward 取回并启动：[ops/sinkhorn.py:23-28](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/ops/sinkhorn.py#L23-L28)。`ctx.bwd_kernel(grad_output, x, grad_input)` 一行启动；返回 `(grad_input, None, None)` 与 forward 三个输入 `(x, repeat, eps)` 逐位对应——`repeat`、`eps` 是 Python 标量，对应位置返回 `None`（u8-l1 的契约）。

对外函数的形状归一化：[ops/sinkhorn.py:31-32](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/ops/sinkhorn.py#L31-L32)。`x.contiguous().view(-1, *x.shape[-2:])` 把任意前导维压平成「token 维 + 末两维 \(M\times M\)」；`.view_as(x)` 还原。注意它先 `.contiguous()`——因为 `view` 要求连续内存，而用户传进来的 `comb_mix` 可能是某个 slicing 的结果。

> 对照 u8-l1 的 `EngramGateFn`：sinkhorn 没有参数梯度、没有 `main_grad`、没有 `origin_shape` 往返（形状在对外函数里就地处理），所以它是最干净的「单输入、单输出、重计算反向」桥接。理解了它，下一节给「多输入 + 参数梯度」的 head_compute_mix 就只剩「多出来的归约」这一处新东西。

#### 4.2.4 代码实践

**实践目标**：体会「把 kernel 对象挂到 `ctx`」与「反向重新调用构造器」两种写法的等价性，并确认前者更直白。

**操作步骤**（源码阅读 + 思维实验，无需 GPU）：

1. 读 [ops/sinkhorn.py:17-20 与 25-27](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/ops/sinkhorn.py#L17-L27)，注意 `bwd_kernel` 是在 `forward` 里编译、`backward` 里直接启动的。
2. 假设把 `ctx.bwd_kernel = bwd_kernel` 这行删掉，在 `backward` 里改成重新写 `bwd_kernel = _mhc_sinkhorn_bwd(hidden_size, 32, repeat, eps)`——但 `backward` 拿不到 `hidden_size / repeat / eps`（除非把它们也存到 `ctx`）。思考：要支持这种写法，`forward` 至少要额外存哪些非张量到 `ctx`？
3. 对比两种写法，得出结论。

**需要观察的现象**：若不在 `forward` 存 `bwd_kernel`，就必须把 `hidden_size / repeat / eps` 都挂到 `ctx`，反向再重建 kernel（即便命中 JIT 缓存，也要重建 Python 对象、重传编译期参数）。

**预期结果**：把 kernel 对象直接存到 `ctx` 更省事——它已封装了所有编译期参数，反向一行启动即可。这是「前向编译、反向复用」的惯用写法。

#### 4.2.5 小练习与答案

**练习 1**：`ctx.bwd_kernel` 为什么用 `ctx.bwd_kernel = ...` 而不是放进 `save_for_backward`？

**参考答案**：`save_for_backward` 服务于**张量**（接入 autograd 版本计数，能在张量被原地改写时报错）。kernel 对象不是张量、不参与版本计数，用 `ctx` 属性更直接（u8-l1 的「存盘分工」）。

**练习 2**：`backward` 返回三元组 `(grad_input, None, None)`，三个位置分别对应 forward 的哪三个输入？

**参考答案**：对应 `forward(ctx, x, repeat, eps)` 的 `x`、`repeat`、`eps`。后两者是 Python 标量不可微，返回 `None`。

---

### 4.3 `MHCHeadComputeMix`：per-SM 分块 + `.sum(0)` 聚合的反向归约

#### 4.3.1 概念说明

`mhc_head_compute_mix` 是 `mhc_head`（lm_head 的精简版 pre 段，见 u8-l2）里的一步，前向是一个逐元素的 sigmoid 仿射：

\[
o_{i,j} = \sigma(z_{i,j}) + \varepsilon,\qquad z_{i,j} = x_{i,j}\cdot s + b_j
\]

其中 \(x\) 是 `input_mix`（形状 `(..., mhc_mult)`，末维 \(M=\)`mhc_mult`=4），\(s\) 是 `mhc_scale[0]`（**标量**），\(b_j\) 是 `mhc_base[j]`（**长度 \(M\) 的小向量**），\(\varepsilon=\)`mhc_pre_eps`。

难点全在反向。设上游梯度为 \(\bar o_{i,j}\)（代码里的 `output_mix_grad`），由链式法则（记 \(p_{i,j}:=\sigma(z_{i,j})\)）：

\[
\bar x_{i,j} = \bar o_{i,j}\,p_{i,j}(1-p_{i,j})\,s,\qquad
\bar s = \sum_{i,j} \bar o_{i,j}\,p_{i,j}(1-p_{i,j})\,x_{i,j},\qquad
\bar b_j = \sum_i \bar o_{i,j}\,p_{i,j}(1-p_{i,j})
\]

观察三个梯度的「形状」差异——这是本讲最关键的洞察：

| 梯度 | 形状 | 是否带 token 维 | 写出方式 |
| --- | --- | --- | --- |
| \(\bar x\) = `input_mix_grad` | `(num_tokens, M)` | **有** | 每 block 写自己的 token 切片，天然不冲突 |
| \(\bar s\) = `mhc_scale_grad` | `(1,)` 标量 | **无** | 全体 token 都要贡献到**同一个标量** |
| \(\bar b\) = `mhc_base_grad` | `(M,)` 小向量 | **无** | 全体 token 都要贡献到**同 \(M\) 个地址** |

`input_mix_grad` 带着 token 维，不同 block 处理不同 token 区间、写不相交的输出行，**零竞争**，直接写出即可。但 \(\bar s\)、\(\bar b\) 是「小而无 token 维」的参数梯度——**每一个 token 都要把自己的一份累加进同一个标量/同 \(M\) 个地址**。num_tokens 可能成千上万，若按朴素的「一个 block 处理 32 个 token」分 grid，会有成百上千个 block 对 1~\(M\) 个地址做 `atomicAdd`，产生**极端的原子竞争**（所有 block 抢同一两个地址）。

项目的解法是经典的 **split-K 风格分块归约**：

1. **只启动 `num_sms` 个持久化 block**（一个 SM 一个），让它循环扫完所有 token（`T.Persistent`）；
2. 每个 block 把自己扫到的 token 的贡献**累加进自己的私有 reducer**（块内跨线程归约靠 `finalize_reducer`），再写到 `partial[pid, :]`——**每 pid 写不相交的一行，零原子竞争**；
3. Python 层对 `partial` 沿第 0 维 `.sum(0)`，把 `num_sms` 份部分和加总成最终梯度。

`.sum(0)` 的代价微乎其微：被加的是 `(num_sms, 1)` 与 `(num_sms, M)` 的小张量（H100 上 `num_sms=148`、\(M=4\)，总共几百个 float），相对 kernel 本身可忽略。

> 一句话总结这个模式：**带 token 维的梯度直接写；不带 token 维的「小参数梯度」按 SM 攒 partial、再 `.sum(0)`**。

#### 4.3.2 核心流程

前向（薄）：分配 `output_mix`、编译并启动 fwd kernel、存三个张量。

```text
forward(ctx, input_mix, mhc_scale, mhc_base, mhc_pre_eps):
  mhc_mult = input_mix.shape[-1]
  output_mix = empty_like(input_mix)
  fwd_kernel = _mhc_head_compute_mix_fwd(mhc_mult, mhc_pre_eps, token_block_size=32)
  fwd_kernel(input_mix.view(-1, mhc_mult), mhc_scale, mhc_base, output_mix.view(-1, mhc_mult))
  ctx.save_for_backward(input_mix, mhc_scale, mhc_base)     # 不存 sigmoid 输出 → 反向重算
  return output_mix.view_as(input_mix)
```

反向（厚）：分配 `input_mix_grad` 与两个 `*_grad_partial`、启动 bwd kernel、`.sum(0)` 聚合。

```text
backward(ctx, output_mix_grad):
  input_mix, mhc_scale, mhc_base = ctx.saved_tensors
  num_sms = get_num_sms()                                   # 决定分多少份 partial
  input_mix_grad = empty_like(input_mix)                    # 带 token 维，直接写
  mhc_scale_grad_partial = empty(num_sms, *mhc_scale.shape) # (num_sms, 1)
  mhc_base_grad_partial  = empty(num_sms, *mhc_base.shape)  # (num_sms, M)
  bwd_kernel = _mhc_head_compute_mix_bwd(mhc_mult, token_block_size=32, num_sms=num_sms)
  bwd_kernel(..., input_mix_grad, mhc_scale_grad_partial, mhc_base_grad_partial)
  mhc_scale_grad = mhc_scale_grad_partial.sum(0)            # 跨 SM 聚合
  mhc_base_grad  = mhc_base_grad_partial.sum(0)
  return (input_mix_grad, mhc_scale_grad, mhc_base_grad, None)   # None ↔ mhc_pre_eps
```

底层 bwd kernel 的内部结构（一个 block = 一个 SM）：

```text
with T.Kernel(num_sms) as pid:                             # 恰好 num_sms 个 block
  scale_red = alloc_reducer(1, ...)                        # 本 block 私有 reducer
  base_red  = alloc_reducer(mhc_mult, ...)
  fill(scale_red, 0); fill(base_red, 0)
  for t in T.Persistent([ceildiv(num_tokens, 32)], num_sms, pid, group_size=1):
      # 每个 block 持久化地循环扫属于自己的若干 32-token 片
      重算 p = sigmoid(input_mix*s + base)                 # 廉价重算（前向没存）
      g = p*(1-p) * output_mix_grad                        # grad_frag = σ'(z)*上游
      input_mix_grad[i,j] = g * s                          # 直接写（带 token 维）
      scale_red[0] += g * input_mix[i,j]                   # 累加进本 block reducer
      base_red[j]  += g
  finalize_reducer(scale_red); finalize_reducer(base_red)  # 块内跨线程归约
  copy(scale_red → mhc_scale_grad_partial[pid, :])         # 写到本 pid 独有的一行
  copy(base_red  → mhc_base_grad_partial[pid, :])
```

#### 4.3.3 源码精读

**前向数学**在 fwd kernel 里一行：[head_compute_mix_kernel.py:28-31](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/head_compute_mix_kernel.py#L28-L31) —— `output_mix[i,j] = sigmoid(input_mix[i,j]*mhc_scale[0] + mhc_base[j]) + mhc_pre_eps`，正是 4.3.1 的 \(o_{i,j}\)。`mhc_mult`、`mhc_pre_eps`、`token_block_size` 是编译期参数，`num_tokens` 是运行时符号（u2-l1）。

**ops 层 forward**：[ops/head_compute_mix.py:8-26](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/ops/head_compute_mix.py#L8-L26)。注意 [L25](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/ops/head_compute_mix.py#L25) `ctx.save_for_backward(input_mix, mhc_scale, mhc_base)` **只存三个输入，不存 sigmoid 输出**——反向会重算 \(p\)，这是小尺度的 rematerialization（与 u7-l3 同源思想：重算一个廉价逐元素 sigmoid，换掉一个 `(num_tokens, M)` 张量的存储）。

> 读源码注意：[L15 与 L32](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/ops/head_compute_mix.py#L15) 的返回类型注解写的是 `tuple[torch.Tensor, torch.Tensor, torch.Tensor]`，但 forward 实际只返回**一个**张量 `output_mix.view_as(input_mix)`、backward 实际返回**四**元组。这是源码里陈旧的类型注解（不影响运行，`apply` 不校验注解），读源码时以实际 `return` 为准、不要被注解误导。

**ops 层 backward** 的 partial 分配与聚合：[ops/head_compute_mix.py:28-68](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/ops/head_compute_mix.py#L28-L68)。逐段看：

- [L35](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/ops/head_compute_mix.py#L35) `num_sms = get_num_sms()` —— 决定分多少份 partial。`get_num_sms`（[config.py:19-23](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/config.py#L19-L23)）返回「可用 SM 数」，可被 `set_num_sms` 限制（u1-l3、u10-l1）。
- [L37-L48](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/ops/head_compute_mix.py#L37-L48) 分配两个 partial 张量，形状是 `(num_sms, *原参数形状)`，即 `(num_sms, 1)` 与 `(num_sms, M)`。**第 0 维专门用来给每个 SM 一份独立行**。
- [L51-L55](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/ops/head_compute_mix.py#L51-L55) 编译 bwd kernel，把 `num_sms` 作为编译期参数烤进产物（kernel 的 grid 就是 `num_sms`）。
- [L56-L64](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/ops/head_compute_mix.py#L56-L64) 启动 kernel，把两个 partial 当输出张量传进去。
- [L65-L66](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/ops/head_compute_mix.py#L65-L66) `.sum(0)` 把 `num_sms` 份部分和加总——**归约发生在 Python 层**，不在 kernel 里。
- [L68](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/ops/head_compute_mix.py#L68) 返回四元组，对应 forward 的 `(input_mix, mhc_scale, mhc_base, mhc_pre_eps)`，末位 `None` 对应不可微的 `mhc_pre_eps`。

**底层 bwd kernel** 的持久化 + reducer：[head_compute_mix_kernel.py:57-83](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/head_compute_mix_kernel.py#L57-L83)。

- [L57](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/head_compute_mix_kernel.py#L57) `with T.Kernel(num_sms) as pid` —— grid 大小恰为 `num_sms`，即「一个 SM 一个 block」。
- [L58-L61](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/head_compute_mix_kernel.py#L58-L61) 每个 block 声明**自己的** reducer 并清零。`replication='all'` 是跨线程复制归约布局（u2-l3）。
- [L62-L67](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/head_compute_mix_kernel.py#L62-L67) `T.Persistent([...], num_sms, pid, group_size=1)` —— 持久化循环，让 block `pid` 在「所有 token 片」里挑出属于自己的那部分循环处理（u6-l1 详述了 persistent 的分片语义）。
- [L73-L79](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/head_compute_mix_kernel.py#L73-L79) 重算 \(p=\sigma(z)\)（[L73-L75](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/head_compute_mix_kernel.py#L73-L75)），算 `grad_frag` \(g=p(1-p)\bar o\)（[L76](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/head_compute_mix_kernel.py#L76)），写 `input_mix_grad`（[L77](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/head_compute_mix_kernel.py#L77)，直接写），累加进两个 reducer（[L78-L79](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/head_compute_mix_kernel.py#L78-L79)）。注意 reducer 的累加**只在本 block 私有地址上**，没有 atomics。
- [L80-L83](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/head_compute_mix_kernel.py#L80-L83) `finalize_reducer` 做块内跨线程归约，再 `T.copy` 到 `partial[pid, :]`——每个 pid 写不同行，零冲突。

#### 4.3.4 代码实践

**实践目标**：用一段**纯 CPU PyTorch**亲手复现「per-SM partial + `.sum(0)`」的 split-K 归约，验证它等价于直接对全体 token 求和，并量化「为何不用 `atomicAdd`」。

**操作步骤**（把下面这段「示例代码」存成 `toy_splitk.py`，`python toy_splitk.py` 运行，无需 GPU）：

```python
# 示例代码：纯 PyTorch 模拟 head_compute_mix 的 per-SM partial + sum(0)
import torch
torch.manual_seed(0)

num_tokens, mhc_mult = 1024, 4
x     = torch.randn(num_tokens, mhc_mult, requires_grad=True)   # input_mix
scale = torch.randn(1, requires_grad=True)                       # mhc_scale (标量)
base  = torch.randn(mhc_mult, requires_grad=True)                # mhc_base (小向量)
eps   = 1e-6

# === 黄金答案：直接前向 + autograd 反向 ===
out = (torch.sigmoid(x * scale + base) + eps).sum()
out.backward()
ref_scale_grad, ref_base_grad = scale.grad.clone(), base.grad.clone()

# === 模拟 kernel 的 split-K：把 token 维切成 num_sms 段，每段算自己的 partial ===
num_sms = 8                                  # 模拟 8 个 SM
xs, ss, bs = x.detach(), scale.detach(), base.detach()   # partial 阶段不需要图
chunk = (num_tokens + num_sms - 1) // num_sms
scale_partial = torch.zeros(num_sms)                 # (num_sms,) 对应 (num_sms, 1) 摊平
base_partial  = torch.zeros(num_sms, mhc_mult)       # (num_sms, M)
for pid in range(num_sms):
    lo, hi = pid * chunk, min((pid + 1) * chunk, num_tokens)
    if lo == hi:
        continue
    xt = xs[lo:hi]
    p  = torch.sigmoid(xt * ss + bs)                  # 重算 p（前向没存）
    g  = p * (1 - p)                                  # σ'(z)
    gf = torch.ones_like(p) * g                       # 上游=1（因为 out.sum()）
    scale_partial[pid] = (gf * xt).sum()              # Σ gf*x  → 本 SM 的 partial
    base_partial[pid]  = gf.sum(0)                    # Σ_i gf  → 本 SM 的 partial

# === Python 层 .sum(0) 聚合 ===
scale_grad = scale_partial.sum(0)
base_grad  = base_partial.sum(0)

print("scale 等价？", torch.allclose(scale_grad, ref_scale_grad))   # 预期 True
print("base  等价？", torch.allclose(base_grad,  ref_base_grad))    # 预期 True
print("partial 体积(num_sms*M) =", num_sms * mhc_mult, " 个 float，.sum(0) 代价可忽略")
```

**需要观察的现象**：两行 `allclose` 都打印 `True`，说明「把 token 分成 `num_sms` 段各算 partial、再 `.sum(0)`」与「直接对全体 token 求和」数学上完全等价。最后那行提示被聚合的张量极小。

**预期结果**：`scale 等价？ True` / `base 等价？ True`。

**回答实践任务的核心问题**（为何按 `num_sms` 分块再 `.sum(0)`）：

1. `mhc_scale_grad` / `mhc_base_grad` 是**不带 token 维的小参数梯度**（标量与长度 \(M\) 向量），全体 token 都要贡献到同一两个地址；而 `input_mix_grad` 带 token 维、各 block 写不相交切片，无需此处理。
2. 若让每个 32-token block 都 `atomicAdd` 进同一地址，成百上千个 block 会争抢 1~\(M\) 个地址，原子竞争极重。
3. 改成「每 SM 一个持久化 block、各写 `partial[pid]`」，每个 pid 写不相交的一行，**零原子竞争**；`.sum(0)` 只聚合 `num_sms` 行（几百个 float），代价可忽略。
4. 选 `num_sms` 作为分块数，是因为它恰好让「一个 resident wave 填满 GPU」，partial 缓冲也最小。

**待本地验证**：若有 GPU 与可运行的 TileKernels，可用 `torch.autograd.gradcheck`（需 double、小 `num_tokens`）或与 `mhc_head_compute_mix` 的真实输出对拍，进一步确认数值正确。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `input_mix_grad` 不需要走 partial + `.sum(0)`，而 `mhc_scale_grad` / `mhc_base_grad` 需要？

**参考答案**：`input_mix_grad` 形状是 `(num_tokens, M)`，带 token 维，每个 block 处理不同 token 区间、写不相交的输出行，天然无竞争；`mhc_scale_grad`/`mhc_base_grad` 形状是 `(1,)`/`(M,)`，不带 token 维，全体 token 都要累加进同一两个地址，直接写会引发严重原子竞争，故按 SM 攒 partial 再聚合。

**练习 2**：把分块数从 `num_sms` 改成 1（即只开 1 个 persistent block），结果还对吗？性能会怎样？

**参考答案**：结果仍正确（1 份 partial 的 `.sum(0)` 就是它本身，数学等价）。但性能大幅下降——只有 1 个 SM 在干活，其余 `num_sms-1` 个 SM 闲置，串行扫完所有 token。选 `num_sms` 是为了「一个 wave 填满 GPU」。

**练习 3**：backward 返回四元组 `(input_mix_grad, mhc_scale_grad, mhc_base_grad, None)`，末位 `None` 对应 forward 的哪个输入？为什么是 `None`？

**参考答案**：对应 `mhc_pre_eps`（Python `float`）。标量不可微，按 u8-l1 的契约返回 `None` 占位，保证返回数与 forward 输入数（4 个）逐位对应。

**练习 4**：对比 engram 的 `grad_w_reduce`（u6-l2、u8-l1），head_compute_mix 为何不写一个专门的 reduce kernel、而用 Python `.sum(0)`？

**参考答案**：被聚合的量量级不同。engram 的参数是完整权重矩阵（大），且要支持 `main_grad` 就地累加，值得用一个专门的 `grad_w_reduce` kernel 在 GPU 上高效归约；head_compute_mix 的参数是标量/小向量，partial 只有 `(num_sms, 1)` 与 `(num_sms, M)` 几百个 float，Python `.sum(0)` 已足够廉价，再写一个 kernel 反而是杀鸡用牛刀。两种策略是同一思想（split-K 避免原子竞争）在不同量级上的自然取舍。

---

## 5. 综合实践

把本讲三块知识串起来，完成一个「**为一个带小参数梯度的算子设计 ops 层封装**」的纸面工程 + CPU 验证。

**场景**：假设你有一个逐元素算子 \(o_{i,j} = \mathrm{tanh}(a\,x_{i,j} + c)\)，其中 \(x\) 是 `(num_tokens, D)` 的大张量，\(a\)、\(c\) 是标量参数。你要给它写一个 ops 层 `autograd.Function`。

**任务**：

1. **推导反向**：写出 \(\bar x_{i,j}\)、\(\bar a\)、\(\bar c\) 的表达式。判断哪些梯度「带 token 维可直接写」、哪些「不带 token 维需 partial + `.sum(0)`」。
2. **设计 partial 形状**：参照 4.3，写出 \(\bar a\)、\(\bar c\) 的 partial 张量形状（含 `num_sms` 维）。
3. **写 backward 返回元组**：对照 forward 的输入 `(x, a, c)`，确定返回几个值、哪位是 `None`（提示：若 `mhc_pre_eps` 那样的标量也算 forward 输入则占 `None`；本题没有 eps）。
4. **CPU 验证**：仿照 4.3.4 的示例代码，把 token 切成 `num_sms=8` 段算 partial、再 `.sum(0)`，与 PyTorch autograd 直接算出的 `a.grad`/`c.grad` 对拍。

**参考要点**：

- 反向：\(\tanh'(u)=1-\tanh^2(u)\)，记 \(t_{i,j}=\tanh(a x_{i,j}+c)\)，则
  \(\bar x_{i,j}=\bar o_{i,j}(1-t_{i,j}^2)\,a\)、\(\bar a=\sum_{i,j}\bar o_{i,j}(1-t_{i,j}^2)\,x_{i,j}\)、\(\bar c=\sum_{i,j}\bar o_{i,j}(1-t_{i,j}^2)\)。
- \(\bar x\) 带 token 维 → 直接写；\(\bar a\)、\(\bar c\) 是标量、不带 token 维 → partial + `.sum(0)`。
- partial 形状：`a_grad_partial = (num_sms,)`（或 `(num_sms,1)`）、`c_grad_partial = (num_sms,)`。
- 返回三元组 `(x_grad, a_grad, c_grad)`，无 `None`（本题无不可微标量输入）。
- CPU 对拍：把 4.3.4 的 `sigmoid` 换成 `tanh`、把 `p*(1-p)` 换成 `1-t*t`，即可复用整段脚本，预期 `allclose` 为 `True`。

> 这个任务不需要 GPU，目的是让你独立走一遍「判断哪些梯度要 partial → 定 partial 形状 → 写返回元组 → 验证等价」的完整流程，把 head_compute_mix 的模式内化为可迁移的通用方法。

## 6. 本讲小结

- **ops 层定位**：`modeling/mhc/ops/` 用 `torch.autograd.Function` 把**单个**底层 kernel 包成可微算子；`__init__.py` 是 9 个算子的纯再导出枢纽，functional 层在此基础上做组合，二者职责分离（封装 vs 编排）。
- **极简桥接 `_SinkhornNormalize`**：forward 一次性编译 fwd+bwd 两个 kernel，把 `bwd_kernel` 当 `ctx` 属性存下，反向直接启动；只 `save_for_backward(x)`（重计算策略），返回 `(grad_input, None, None)` 与三个前向输入逐位对应。kernel 细节见 u7-l3。
- **`MHCHeadComputeMix` 的核心模式**：反向里**带 token 维的梯度（`input_mix_grad`）直接写**；**不带 token 维的小参数梯度（`mhc_scale_grad`/`mhc_base_grad`）按 SM 攒 `*_grad_partial`、再 Python `.sum(0)` 聚合**，规避海量 block 对同一地址的原子竞争。
- **为何选 `num_sms`**：让持久化 block 数恰好填满一个 resident wave，partial 缓冲也最小（`num_sms` 行），`.sum(0)` 只聚合几百个 float，代价可忽略。
- **三种反向归约策略的边界**：sinkhorn（输出带 token 维，无需归约）／head_compute_mix（小参数梯度，Python `.sum(0)`）／engram `grad_w_reduce`（大权重矩阵 + `main_grad` 就地累加，专门 reduce kernel）——同一 split-K 思想在不同量级上的取舍。
- **小尺度 rematerialization**：head_compute_mix 前向不存 sigmoid 输出、反向重算一个廉价逐元素 sigmoid，与 u7-l3 的全链重算同源但更轻量。

## 7. 下一步学习建议

- **横向对比同层其他 op 的反向**：阅读 [ops/post.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/ops/post.py)（u7-l1）的 `MHCPost.backward`，它直接 `return *mhc_post_bwd(*saved, d_o)`——对照理解「kernel 内部已完成归约、Python 层无需 `.sum(0)`」的另一种情形。
- **更深的大参数梯度归约**：回看 **u6-l2 / u8-l1** 的 `grad_w_reduce` + `main_grad` 就地累加，理解当参数是大矩阵时为何值得用一个专门 kernel、以及分布式训练里返回 `None` 的优化。
- **持久化与硬件感知**：本讲的 `T.Persistent` 与 `get_num_sms` 是 u10-l1「SM/共享内存感知调优」的具体应用；想系统理解占用启发式与 `set_num_sms` 的影响，接着读 u10-l1。
- **动手方向**：仿照第 5 节综合实践，挑一个 TileKernels 里**纯前向**的逐元素算子（如 `engram_hash`，u6-l3），为它补一个 ops 层 `autograd.Function`：先判断它的反向是否会出现「小参数梯度」，再决定要不要 partial + `.sum(0)`。
