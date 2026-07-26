# MHC functional API：pre / head 的组合

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `tile_kernels/modeling/mhc/functional.py` 在整个 mhc 模块里扮演的「编排者」角色——它不写任何算子逻辑，只把多个底层 op 串成用户可调用的入口。
- 逐步追踪 `mhc_pre` 从 `residual` 到 `layer_input` 的完整调用链，标注每一步调用的底层 op 与它对应的 `torch.autograd.Function`。
- 解释 `ctx = (post_mix, comb_mix)` 这个「不透明元组」在前向如何产出、在 `mhc_post` 中如何被消费，以及它为何能让 pre 与 post 两段解耦。
- 掌握 `mhc_head` 通过 `F.pad` 把小权重补零、复用 `mhc_pre_norm_fn` kernel 的技巧，并能证明补零不改变真正需要的那段输出。

本讲是第 8 单元「Modeling 高层可训练层」的第二篇，承接 u8-l1（单个 `torch.autograd.Function` 封装范式）与 u7-l2（mhc 前处理流水线的四步拆分与融合），把视角从「单个 op」抬升到「多个 op 如何被组合成一个稳定 API」。

## 2. 前置知识

本讲默认你已经读过以下内容，不再重复其内部细节：

- **u7-l1**：mhc 流水线的心智模型（`expand → pre → sublayer → post` 四段）、`mhc_mult` 残差维度、`ctx=(post_mix, comb_mix)` 的概念性引入、`expand`/`norm_fn`/`post` 三个最基础 kernel。
- **u7-l2**：pre 段的四步拆分（`pre_norm_fn → pre_split_mixes → sinkhorn → pre_apply_mix`）、推理态的 `pre_big_fuse` 大融合、`mhc_head`/`head_compute_mix` 的定位。
- **u8-l1**：`torch.autograd.Function` 封装底层 kernel 的标准范式，以及「modeling 层只做可微封装、不写算子逻辑」的分层原则。

下面只补一个本讲反复用到的关键直觉，作为衔接：

**「编排层」与「算子层」的分工。** TileKernels 的 mhc 模块分成两层：`tile_kernels/mhc/*_kernel.py` 是用 TileLang DSL 写的、经 JIT 编译成 CUDA 的底层 kernel（对 autograd 是黑盒）；`tile_kernels/modeling/mhc/ops/*.py` 把每个底层 kernel 包成一个 `torch.autograd.Function`（u8-l1 范式），让它能参与 `loss.backward()`；而本讲的 `tile_kernels/modeling/mhc/functional.py` 再往上一层，把多个 op 按固定配方串成「一个调用就能完成 pre 处理」的入口函数。换句话说：

```
底层 kernel (TileLang DSL)
      ↓  ops/*.py 包成 autograd.Function
单个可微 op
      ↓  functional.py 按配方组合多个 op
用户入口 mhc_pre / mhc_head / expand_from_embedding
```

本讲专注最上面那一层箭头：**组合**。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 | 本讲关注点 |
|------|------|-----------|
| `tile_kernels/modeling/mhc/functional.py` | mhc 的编排层，提供三个用户入口 | 全部内容 |
| `tile_kernels/modeling/mhc/__init__.py` | 子包导出，决定对外可见的符号 | 导出哪三个函数 |
| `tile_kernels/modeling/mhc/ops/__init__.py` | ops 层的再导出清单 | 帮助看清 functional 调用了哪些 op |
| `tile_kernels/modeling/mhc/ops/post.py` | `mhc_post` 封装，消费 `ctx` 元组 | 证明 ctx 契约的另一端 |
| `tile_kernels/modeling/mhc/ops/pre_big_fuse.py` | 推理态大融合 op | 返回顺序与 functional 的重排 |

底层 kernel（`tile_kernels/mhc/*`）的内部实现已在 u7-l1/u7-l2 讲过，本讲只把它们当作「黑盒 op」引用。

## 4. 核心概念与源码讲解

### 4.1 functional 编排层与 __init__ 导出

#### 4.1.1 概念说明

`functional.py` 是 mhc 模块对外的「门面（facade）」。它的设计动机有三点：

1. **隐藏配方。** mhc 的 pre 处理其实是固定的四步组合（`pre_norm_fn → pre_split_mixes → sinkhorn → pre_apply_mix`），但模型作者不应被迫记住这个顺序。`mhc_pre` 把它封装成一次调用。
2. **自动分流训练 / 推理。** 推理态（`torch.is_grad_enabled() == False`）可以走无反向的大融合 kernel `mhc_pre_big_fuse` 换带宽；训练态必须走可反传的四步拆分。这个判断只在 `mhc_pre` 里做一次，调用方无感。
3. **稳定返回契约。** 无论走哪条路径，`mhc_pre` 都返回同一个形状的 `(layer_input, ctx)`，让下游代码不必关心内部走了融合还是拆分。

而 `__init__.py` 只做一件事：决定「外部 `from tile_kernels.modeling.mhc import X` 时能看到哪些名字」。

#### 4.1.2 核心流程

functional 层的数据流可以画成三张「入口卡片」：

```
expand_from_embedding(x, mhc_mult)
      └─→ expand_to_mhc  (一个 op)

mhc_pre(residual, fn, scale, base, ...)
      ├─ grad 关闭 → mhc_pre_big_fuse        (推理：一个融合 op)
      └─ grad 开启 → pre_norm_fn → pre_split_mixes → sinkhorn → pre_apply_mix
      返回: (layer_input, ctx=(post_mix, comb_mix))

mhc_head(residual, fn, scale, base, ...)
      └─→ pre_norm_fn(补零 fn) → 切片 → head_compute_mix → pre_apply_mix
      返回: layer_input
```

三者的共同点是：**它们本身都不是 `torch.autograd.Function`**，而是普通 Python 函数。可微性来自它们调用的每个 op 各自带反向——组合多个可微 op，autograd 图自然连成一条链。

#### 4.1.3 源码精读

先看子包入口，它只再导出三个名字：

[`tile_kernels/modeling/mhc/__init__.py:1`](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/__init__.py#L1) —— 从 `functional` 模块再导出 `expand_from_embedding`、`mhc_head`、`mhc_pre`。这三个就是 mhc 子包对外的全部公共 API；ops 层的 `mhc_post`、`mhc_pre_norm_fn` 等并没有在这里导出，说明它们被视为「内部零件」，调用方理应通过 functional 入口使用。

再看 functional 模块顶部的导入清单，它列出了编排层能调用的全部 op：

[`tile_kernels/modeling/mhc/functional.py:4-11`](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/functional.py#L4-L11) —— 从 `.ops` 子包导入 `expand_to_mhc`、`mhc_head_compute_mix`、`mhc_pre_norm_fn`、`mhc_post`、`mhc_pre_apply_mix`、`mhc_pre_big_fuse`、`mhc_pre_split_mixes`、`sinkhorn_normalize` 八个 op。注意 `mhc_post` 也被导入了——它虽然不在三个公共入口里，但与 `mhc_pre` 通过 `ctx` 元组紧密配对（4.3 节详述）。

这些 op 名字都能在 ops 层的导出清单里一一对应：

[`tile_kernels/modeling/mhc/ops/__init__.py:1-9`](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/ops/__init__.py#L1-L9) —— ops 子包再导出全部底层 op，每个都是 `SomeName.apply(...)` 的薄封装（u8-l1 范式）。

#### 4.1.4 代码实践

**实践目标：** 用源码阅读建立「入口 → op」的映射表，验证 functional 层只是组合、不新增算子。

**操作步骤：**

1. 打开 `tile_kernels/modeling/mhc/functional.py`，对三个入口函数（`expand_from_embedding`、`mhc_pre`、`mhc_head`）分别列出它们调用的 op 名字。
2. 打开 `tile_kernels/modeling/mhc/ops/__init__.py`，确认每个被调用的 op 都能在这个清单里找到对应的 `autograd.Function` 薄封装。
3. 打开 `tile_kernels/modeling/mhc/__init__.py`，确认对外只导出三个 functional 入口。

**预期结果：** 你应当得到一张表，形如：

| 入口 | 调用的 op（按顺序） |
|------|---------------------|
| `expand_from_embedding` | `expand_to_mhc` |
| `mhc_pre`（推理） | `mhc_pre_big_fuse` |
| `mhc_pre`（训练） | `mhc_pre_norm_fn` → `mhc_pre_split_mixes` → `sinkhorn_normalize` → `mhc_pre_apply_mix` |
| `mhc_head` | `mhc_pre_norm_fn` → `mhc_head_compute_mix` → `mhc_pre_apply_mix` |

且 functional.py 里没有任何 `@T.prim_func`、`tilelang.jit`、`torch.autograd.Function` ——它确实只组合、不写算子。这是纯源码阅读实践，无需 GPU。

#### 4.1.5 小练习与答案

**练习 1：** 为什么 `mhc_post` 没有出现在 `__init__.py` 的导出里，却被 `functional.py` 导入了？

**参考答案：** `__init__.py` 决定的是「子包对外推荐用法」，三个 functional 入口是面向模型作者的公共 API；`mhc_post` 是与 `mhc_pre` 强绑定的配对 op（消费 `mhc_pre` 产出的 `ctx`），通常在模型代码里紧接着 `mhc_pre` 之后被调用，但它本身仍是「零件级」接口，故不放进顶层导出。`functional.py` 导入它是为了内部使用（见 4.3 节），而非对外暴露。

**练习 2：** 假如你想新增一个「只做 sinkhorn 不做其它步骤」的入口，应该加在 `functional.py` 还是 `ops/sinkhorn.py`？

**参考答案：** 如果它只是把 `sinkhorn_normalize` 换个名字暴露，属于「零件再导出」，不必加；如果它要把 sinkhorn 与别的 op 组合成新配方，就加在 `functional.py`，遵循「组合逻辑放编排层、单算子封装放 ops 层」的分层原则。

---

### 4.2 mhc_pre：完整调用链与训练 / 推理分流

#### 4.2.1 概念说明

`mhc_pre` 是 mhc 流水线 pre 段的唯一入口。它的职责是：吃进多股残差 `residual`（形状 `[..., mhc_mult, hidden_size]`）与一组可学习权重（`fn`、`scale`、`base`、`norm_weight`），把多股残差加权压成单股 `layer_input`（形状 `[..., hidden_size]`）喂给子层（attention/FFN），同时把后续 post 段需要的混合系数打包成 `ctx` 一并返回。

它最关键的设计是 **按 `torch.is_grad_enabled()` 自动分流**：

- **推理态**（grad 关闭）：调用单个融合 op `mhc_pre_big_fuse`，把四步融进一个 TileLang kernel，牺牲可反传性换带宽（u7-l2 已述：融合 kernel 无反向实现）。
- **训练态**（grad 开启）：走四步拆分，每步是独立 `autograd.Function`、自带反向，保证 `loss.backward()` 能正确回传。

调用方完全不需要知道自己处于哪种模式——`mhc_pre` 内部判断。

#### 4.2.2 核心流程

```
mhc_pre(residual, fn, scale, base, norm_weight, ...)
   │
   ├─ if not torch.is_grad_enabled():        # 推理
   │     post_mix, comb_mix, layer_input = mhc_pre_big_fuse(...)
   │     return layer_input, (post_mix, comb_mix)
   │
   └─ else:                                  # 训练（四步拆分）
         mixes      = mhc_pre_norm_fn(residual, fn, norm_weight, ...)   # GEMM + RMSNorm
         pre_mix, post_mix, comb_mix = mhc_pre_split_mixes(mixes, ...)  # 切三段 + sigmoid
         comb_mix   = sinkhorn_normalize(comb_mix, ...)                 # 行列迭代归一化
         layer_input = mhc_pre_apply_mix(residual, pre_mix)             # hidden 维加权求和
         return layer_input, (post_mix, comb_mix)
```

两个要点：

1. **返回契约一致。** 两条路径都返回 `(layer_input, (post_mix, comb_mix))`。注意融合 op `mhc_pre_big_fuse` 自身的返回顺序是 `(post_mix, comb_mix, layer_input)`（见 4.2.3），`mhc_pre` 在推理分支里特意把它**重排**成与训练分支一致的顺序——这是编排层「稳定 API」职责的体现。
2. **可微性来自链式组合。** 训练分支的四个 op 都是 `autograd.Function`，autograd 会把它们连成一条可反传的计算图；推理分支的 `mhc_pre_big_fuse` 不是 `autograd.Function`（它是普通函数直接调 kernel），但因为 grad 本来就关闭，无需反传。

#### 4.2.3 源码精读

先看分流判断与推理分支：

[`tile_kernels/modeling/mhc/functional.py:69-82`](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/functional.py#L69-L82) —— 第 69 行 `if not torch.is_grad_enabled():` 判断是否推理态；第 70 行调用 `mhc_pre_big_fuse`，注意它返回 `(post_mix, comb_mix, layer_input)`；第 82 行 `return layer_input, (post_mix, comb_mix)` 把顺序重排成统一契约。`mhc_pre_big_fuse` 内部确认了这一返回顺序：

[`tile_kernels/modeling/mhc/ops/pre_big_fuse.py:91`](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/ops/pre_big_fuse.py#L91) —— `return post_mix, comb_mix, layer_input`，与 functional 重排前的原始顺序。

再看训练分支的四步链：

[`tile_kernels/modeling/mhc/functional.py:84-105`](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/functional.py#L84-L105) —— 四步依次为：

- 第 84-90 行 `mhc_pre_norm_fn`：对 `residual` 做分组 RMSNorm 投影，产出 `mixes`（`mhc_mult3 = mhc_mult*(mhc_mult+2)` 个通道）。`norm_weight` 非空时，`mhc_pre_norm_fn` 内部会先用 `_MHCFnNormwMerge` 把 norm 权重融进 `fn` 再做 GEMM。
- 第 92-99 行 `mhc_pre_split_mixes`：把 `mhc_mult3` 个通道切成 `pre_mix`/`post_mix`/`comb_mix` 三段，分别套不同的 sigmoid/缩放。
- 第 101 行 `sinkhorn_normalize`：只对 `comb_mix` 做行列迭代归一化（u7-l3 详述其自定义反向）。
- 第 103 行 `mhc_pre_apply_mix`：用 `pre_mix` 对 `residual` 在 hidden 维加权求和，得到单股 `layer_input`。

第 105 行返回 `(layer_input, (post_mix, comb_mix))`——注意 `pre_mix` 在产出 `layer_input` 后就完成了使命，**只有 `post_mix` 和 `comb_mix` 被打包进 `ctx`**，留给 post 段使用。

函数签名的关键字参数也值得一看，它们全是「配方旋钮」：

[`tile_kernels/modeling/mhc/functional.py:30-44`](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/functional.py#L30-L44) —— `norm_eps`、`post_mult_value`、`pre_eps`、`sinkhorn_eps`、`sinkhorn_repeat`、`n_splits` 等，每个都直接透传给对应 op，编排层不做任何修改，只是把它们集中到一个调用里方便配置。

#### 4.2.4 代码实践

**实践目标：** 用 `torch.is_grad_enabled()` 主动切换两条路径，观察 `mhc_pre` 内部走了哪个 op（无需真的跑 kernel，靠调用计数即可判定）。

**操作步骤：**

1. 写一个最小脚本，对 `mhc_pre` 做 monkey-patch，在 `mhc_pre_big_fuse` 与四步 op 的入口各插一条 `print`（或用计数器）。
2. 分别在 `with torch.no_grad():` 与默认（grad 开启）上下文下调用 `mhc_pre`，记录哪条路径被触发。

示例代码（仅用于观察调度，不依赖 GPU 也能看到分流；若实际调用 kernel 则需 SM90/SM100 环境，**待本地验证**）：

```python
# 示例代码：观察 mhc_pre 的训练/推理分流
import torch
import tile_kernels.modeling.mhc.functional as F
import tile_kernels.modeling.mhc.ops as ops

called = []
_orig_big = ops.mhc_pre_big_fuse
_orig_norm = ops.mhc_pre_norm_fn

def spy_big(*a, **k):
    called.append("big_fuse"); return _orig_big(*a, **k)
def spy_norm(*a, **k):
    called.append("norm_fn(train)"); return _orig_norm(*a, **k)

ops.mhc_pre_big_fuse = spy_big
ops.mhc_pre_norm_fn  = spy_norm
F.mhc_pre_big_fuse   = spy_big      # functional.py 是按名字导入的，需同步替换
F.mhc_pre_norm_fn    = spy_norm

# 造最小张量（形状满足断言即可；真实数值需 GPU kernel 才能算出）
# residual: [..., mhc_mult, hidden], fn: [mhc_mult3, mhc_mult*hidden]
# 省略具体构造；下面两行仅示意调用上下文
with torch.no_grad():
    called.clear()
    # F.mhc_pre(residual, fn, scale, base)   # 取消注释后运行
    print("推理态触发:", called)              # 预期 ["big_fuse"]

called.clear()
# F.mhc_pre(residual, fn, scale, base)       # 默认 grad 开启
print("训练态触发:", called)                  # 预期 ["norm_fn(train)", ...]
```

**需要观察的现象：** 推理态列表里只出现 `big_fuse`；训练态列表里出现 `norm_fn(train)` 及后续三步，且不出现 `big_fuse`。

**预期结果：** 两条路径互斥触发，证明 `mhc_pre` 第 69 行的分流生效。若你无法运行 kernel，至少可以通过阅读第 69-105 行确认控制流：`if not torch.is_grad_enabled()` 为真时 `return` 在第 82 行提前退出，训练分支的代码（84-105）根本不会执行。

#### 4.2.5 小练习与答案

**练习 1：** 训练分支里，`pre_mix` 为什么没有被打包进 `ctx`？

**参考答案：** `pre_mix` 的唯一用途就是在 `mhc_pre_apply_mix` 里把多股残差压成单股 `layer_input`（第 103 行），压完它的使命就结束了；post 段不再需要它。post 段需要的是 `post_mix`（控制子层输出如何混回）和 `comb_mix`（控制多股残差如何互相组合），所以只有这两个被进 `ctx`。

**练习 2：** 如果在训练态下强制调用 `mhc_pre_big_fuse`，会发生什么？

**参考答案：** `mhc_pre_big_fuse` 不是 `torch.autograd.Function`，它直接调底层融合 kernel 并返回普通张量；在 grad 开启时调用它，得到的 `layer_input`/`post_mix`/`comb_mix` 将**没有 grad_fn**，反向时梯度无法穿过 pre 段，导致 `fn`/`scale`/`base` 等参数收不到梯度。这正是 `mhc_pre` 要在 grad 开启时绕开它的根本原因——融合是以牺牲可反传性换带宽的。

---

### 4.3 ctx=(post_mix, comb_mix)：不透明元组的前向产出与消费

#### 4.3.1 概念说明

`mhc_pre` 返回的第二个元素 `ctx = (post_mix, comb_mix)` 是 pre 段与 post 段之间的「接力棒」。把它叫做**不透明元组（opaque tuple）**，是因为对调用方而言：你不需要理解里面两个张量的数学含义，只要原样把它们传给 `mhc_post` 即可。

这种设计的价值在于**解耦**：子层（attention 或 FFN）夹在 `mhc_pre` 和 `mhc_post` 之间，它只看到单股的 `layer_input`，对 mhc 的多股残差、混合系数一无所知；pre 段产出的耦合信息通过 `ctx`「绕过」子层直达 post 段，子层完全无需改动。

```
residual ──→ mhc_pre ──→ layer_input ──→ [ 子层(attention/FFN) ] ──→ sublayer_out
                │                                                        │
                └────── ctx=(post_mix, comb_mix) ────────────────────┐   │
                                                                     ↓   ↓
                                                          mhc_post(residual, ctx, sublayer_out)
```

需要强调：`ctx` 里的张量是**真实带 grad 历史的张量**（训练态下由 `mhc_pre_split_mixes` 产出），不是 Python 字典或裸数据。所以把它们传进 `mhc_post` 后，autograd 图依然连通，反向梯度能从 post 段经 `ctx` 流回 pre 段的参数。

#### 4.3.2 核心流程

**前向产出（mhc_pre 内）：**

- 训练分支：`mhc_pre_split_mixes` 直接把 `post_mix`（形状 `[..., mhc_mult, 1]`）与 `comb_mix`（形状 `[..., mhc_mult, mhc_mult]`）作为返回值的中间两段产出，functional 取其中两段打包。
- 推理分支：`mhc_pre_big_fuse` 同样产出 `post_mix`、`comb_mix`（形状完全一致），functional 重排后打包。
- 两条路径产出的 `ctx` 形状一致，下游无法分辨来源。

**后向消费（mhc_post 内）：**

- `mhc_post` 的签名接收 `post_layer_mix` 与 `comb_res_mix` 两个张量——它们就是 `ctx` 解包后的两个元素。
- 调用方典型写法：`layer_input, ctx = mhc_pre(...)`；`out = mhc_post(sublayer(layer_input), residual, ctx[0], ctx[1])`，或更清晰地 `out = mhc_post(sublayer(layer_input), residual, *ctx)`。

#### 4.3.3 源码精读

先确认 `ctx` 在前向的产出位置（两条路径）：

训练分支：[`tile_kernels/modeling/mhc/functional.py:92-99`](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/functional.py#L92-L99) 中 `mhc_pre_split_mixes` 返回三元组 `(pre_mix, post_mix, comb_mix)`，第 105 行把后两个打包成 `(post_mix, comb_mix)`。

推理分支：[`tile_kernels/modeling/mhc/functional.py:70-82`](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/functional.py#L70-L82) 中 `mhc_pre_big_fuse` 返回 `(post_mix, comb_mix, layer_input)`，第 82 行同样打包成 `(post_mix, comb_mix)`。

两条路径产出的 `ctx` 张量形状由各自的 op 保证一致。以训练分支为例，`mhc_pre_split_mixes` 在返回前做了形状重塑：

[`tile_kernels/modeling/mhc/ops/pre_split_mixes.py:52-54`](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/ops/pre_split_mixes.py#L52-L54) —— `post_layer_mix` 重塑为 `(..., mhc_mult, 1)`，`comb_res_mix` 重塑为 `(..., mhc_mult, mhc_mult)`。

推理分支的 `mhc_pre_big_fuse` 在返回前也做了完全相同的重塑：

[`tile_kernels/modeling/mhc/ops/pre_big_fuse.py:87-88`](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/ops/pre_big_fuse.py#L87-L88) —— `post_mix` 重塑为 `(*outer_shape, mhc_mult, 1)`，`comb_mix` 重塑为 `(*outer_shape, mhc_mult, mhc_mult)`。

再看消费端 `mhc_post` 的签名：

[`tile_kernels/modeling/mhc/ops/post.py:28-35`](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/ops/post.py#L28-L35) —— `mhc_post(x, residual, post_layer_mix, comb_res_mix, out=None)`，第三、四个参数正是 `ctx` 解包后的两个张量。它内部委托给 `MHCPost.apply`（u8-l1 范式的 autograd.Function），把 `x`（子层输出）、`residual`、`post_layer_mix`、`comb_res_mix` 一起送进 `mhc_post_fwd` kernel 算出最终多股残差。

这就完成了「pre 产出 ctx → 子层处理 layer_input → post 消费 ctx」的完整接力。`ctx` 的两个元素从被产出到被消费，中间穿越了完全不知情的子层。

#### 4.3.4 代码实践

**实践目标：** 用源码阅读验证 `ctx` 元组的形状契约在 pre/post 两端一致，并写出调用方的标准调用骨架。

**操作步骤：**

1. 在 `pre_split_mixes.py` 第 52-54 行与 `pre_big_fuse.py` 第 87-88 行确认 `post_mix`、`comb_mix` 的形状。
2. 在 `post.py` 第 28-35 行确认 `mhc_post` 形参顺序与形状预期。
3. 对照 `functional.py` 第 82、105 行，确认 `mhc_pre` 返回的 `ctx` 顺序是 `(post_mix, comb_mix)`，与 `mhc_post` 的 `(post_layer_mix, comb_res_mix)` 形参顺序一一对应。

**预期结果：** 你应当能写出如下调用骨架（示例代码，仅展示 ctx 的接力结构，不含可运行数值）：

```python
# 示例代码：pre → 子层 → post 的标准调用骨架
layer_input, ctx = mhc_pre(residual, fn, scale, base, norm_weight=norm_w)
# ctx == (post_mix, comb_mix)，对调用方不透明，原样下传
sublayer_out = my_attention_or_ffn(layer_input)      # 子层只看到单股 layer_input
new_residual = mhc_post(sublayer_out, residual, ctx[0], ctx[1])
# 或更地道：mhc_post(sublayer_out, residual, *ctx)
```

骨架里子层 `my_attention_or_ffn` 的签名与 mhc 完全无关——这就是「不透明元组」带来的解耦收益。无需 GPU 即可完成本实践。

#### 4.3.5 小练习与答案

**练习 1：** 如果调用方把 `ctx` 里两个张量的顺序搞反了（先传 `comb_mix` 再传 `post_mix`），会怎样？

**参考答案：** `mhc_post` 的第三参 `post_layer_mix` 期望形状 `(..., mhc_mult, 1)`，第四参 `comb_res_mix` 期望 `(..., mhc_mult, mhc_mult)`；顺序搞反会把 `(mhc_mult, mhc_mult)` 的 `comb_mix` 喂给期望 `(mhc_mult, 1)` 的位置，形状不匹配，要么触发断言/广播异常，要么算出数值错误的结果。这正是「不透明元组」要用固定顺序 `(post_mix, comb_mix)` 的原因——调用方虽不理解内部含义，但必须按顺序传。

**练习 2：** 为什么不把 `ctx` 实现成一个带字段名的 `namedtuple` 或 dataclass，而用普通二元组？

**参考答案：** 设计上把 `ctx` 当作「不透明接力棒」，调用方只需原样透传、不应去读字段含义；普通二元组正好传达「别打开我」的语义。若用具名结构，反而暗示调用方可以去访问 `ctx.post_mix` 等字段，破坏封装。当然，从健壮性看具名结构能防顺序写反，这是设计上的取舍。

---

### 4.4 mhc_head：lm_head 的 padding 复用技巧

#### 4.4.1 概念说明

`mhc_head` 是给最终语言模型头（lm_head）用的精简版 pre 处理。与普通子层不同，lm_head 只需要把多股残差压成单股、**只需要一个 pre mix**，不需要 post/comb 那套完整的三段切分。因此它的权重 `fn` 更小：形状是 `[mhc_mult, mhc_mult * hidden_size]`（只有 `mhc_mult` 行），而不是普通 `mhc_pre` 的 `[mhc_mult3, mhc_mult * hidden_size]`（`mhc_mult3 = mhc_mult*(mhc_mult+2)` 行）。

问题来了：`mhc_pre_norm_fn` 这个 op（及其底层 GEMM kernel）是按 `mhc_mult3` 行硬编码的，它的输出固定是 `mhc_mult3` 个通道。如果为 lm_head 单独写一个只算 `mhc_mult` 通道的 kernel，就要维护两份代码。

`mhc_head` 的解法是 **补零复用（padding reuse）**：把小权重 `fn` 用 `F.pad` 在行方向补零到 `[mhc_mult3, ...]`，直接喂给现有的 `mhc_pre_norm_fn`，跑完再把输出的前 `mhc_mult` 个通道切出来。关键在于：**补的零行不会污染前 `mhc_mult` 个真实通道的输出**（4.4.2 给出证明）。

#### 4.4.2 核心流程

```
mhc_head(residual, fn[mhc_mult, ...], scale, base, ...)
   │
   1. mhc_mult3 = mhc_mult * (mhc_mult + 2)
   2. 若 fn 行数 < mhc_mult3：
         fn = F.pad(fn, (0,0, 0, mhc_mult3 - fn.shape[0]))   # 行方向底部补零
   3. mixes = mhc_pre_norm_fn(residual, fn, ...)             # 跑现成 op，得 [..., mhc_mult3]
   4. mixes = mixes[..., :mhc_mult]                          # 只取前 mhc_mult 个通道
   5. mix = mhc_head_compute_mix(mixes, scale, base, pre_eps)  # 简化版 mix（只 sigmoid+缩放）
   6. return mhc_pre_apply_mix(residual, mix.unsqueeze(-1))    # 压成单股 layer_input
```

**为什么补零是「无害」的？** `mhc_pre_norm_fn` 的核心是一步 GEMM 加一步 RMSNorm：

- **GEMM 的行独立性。** 输出通道 `out[..., i]` 只依赖权重第 `i` 行 `fn[i, :]`。补的零行（`i ≥ mhc_mult`）使对应输出 `out[..., i] = 0`，但**不影响**前 `mhc_mult` 个真实通道的输出——它们由真实的 `fn[:mhc_mult, :]` 决定。
- **RMSNorm 分母不被抬高。** RMSNorm 的分母是所有通道平方和的根号。补零行产生的输出是 0，其平方也是 0，**不增加**平方和。因此归一化分母与「只算前 `mhc_mult` 通道」时完全一致，前 `mhc_mult` 个通道的归一化结果不变。

两点合起来：`mixes[..., :mhc_mult]`（切片后）与「假设存在一个只算 `mhc_mult` 通道的专用 kernel」的输出**逐位相等**。补零只是白算了 `mhc_mult3 - mhc_mult` 个无用的零通道，换来的是 100% 复用 `mhc_pre_norm_fn` 而无需新增 kernel。

> 注：这一等价性依赖补零行确实产生精确 0（而非极小舍入值）。由于零行 GEMM 是 `0·x = 0` 的精确乘加、bf16/fp32 下 0 均精确表示，条件成立。若你怀疑数值，可在 4.4.4 实践里本地对拍验证。

#### 4.4.3 源码精读

看补零与切片这两步：

[`tile_kernels/modeling/mhc/functional.py:141-154`](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/functional.py#L141-L154) ——

- 第 141 行 `mhc_mult3 = mhc_mult * (2 + mhc_mult)` 算出 `mhc_pre_norm_fn` 期望的行数。
- 第 143-144 行 `if fn.shape[0] < mhc_mult3: fn = F.pad(fn, (0, 0, 0, mhc_mult3 - fn.shape[0]))`。`F.pad` 对二维张量的 pad 元组是 `(左, 右, 上, 下)`，对应最后两个维度；这里 `(0, 0, 0, mhc_mult3 - fn.shape[0])` 表示：最后一维（列）左右都不补、倒数第二维（行）上方不补、**下方补 `mhc_mult3 - fn.shape[0]` 行零**——即把 `fn` 从 `[mhc_mult, H]` 补成 `[mhc_mult3, H]`，新增的全是零行。
- 第 146-152 行用补零后的 `fn` 调 `mhc_pre_norm_fn`，得 `mixes` 形状 `(..., mhc_mult3)`。
- 第 154 行 `mixes = mixes[..., :mhc_mult]` 切出前 `mhc_mult` 个通道（即与原 `fn` 真实行对应的那段）。

再看后续的简化 mix 与压扁：

[`tile_kernels/modeling/mhc/functional.py:156-160`](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/functional.py#L156-L160) ——

- 第 156-157 行 `if scale.numel() == 1: scale = scale.reshape(1)`：`mhc_head` 的 `scale` 允许是标量或 `[1]`（普通 `mhc_pre` 是 `[3]`，分别给 pre/post/comb），这里把两种都规整成 `[1]`。
- 第 158 行 `mix = mhc_head_compute_mix(mixes, scale, base, pre_eps)`：用 lm_head 专用的简化 mix op（不做三段切分、不做 sinkhorn，只对 `mhc_mult` 个通道做 sigmoid+缩放）。
- 第 160 行 `return mhc_pre_apply_mix(residual, mix.unsqueeze(-1))`：`mix` 形状 `(..., mhc_mult)`，`.unsqueeze(-1)` 变 `(..., mhc_mult, 1)`，正好匹配 `mhc_pre_apply_mix` 期望的 `mix` 形状 `(..., mhc, 1)`，把多股残差压成单股 `(..., hidden)`。

对比 `mhc_pre` 与 `mhc_head` 的差异，能看清「精简」的具体含义：

| 维度 | `mhc_pre`（普通子层） | `mhc_head`（lm_head） |
|------|----------------------|----------------------|
| `fn` 行数 | `mhc_mult3` | `mhc_mult`（补零到 `mhc_mult3` 复用 kernel） |
| `scale` 形状 | `[3]`（pre/post/comb 各一） | `[1]` 或标量 |
| `base` 形状 | `[mhc_mult3]` | `[mhc_mult]` |
| mix op | `mhc_pre_split_mixes`（三段切分）+ sinkhorn | `mhc_head_compute_mix`（单段简化） |
| 是否产出 `ctx` | 是 `(post_mix, comb_mix)` | 否，只返回 `layer_input` |

`mhc_head` 不返回 `ctx`，因为 lm_head 之后没有 post 段了——它已经是流水线的终点。

#### 4.4.4 代码实践

**实践目标：** 用纯 PyTorch 在 CPU 上复现「补零 → GEMM+RMSNorm → 切片」的过程，验证补零不影响前 `mhc_mult` 个通道（不需要 GPU/TileLang）。

**操作步骤：**

1. 用 `torch` 在 CPU 上构造一个小的 `fn_small`（行数 = `mhc_mult`）与一个补零版 `fn_padded`（行数 = `mhc_mult3`，多出的行全 0）。
2. 手写一个简化版「GEMM + 对通道 RMSNorm」（模拟 `mhc_pre_norm_fn` 的核心数学，不必与 kernel 逐位一致，只需体现「行独立 + 分母含全部通道平方和」两点）。
3. 对比 `out_padded[..., :mhc_mult]` 与只用 `fn_small` 算出的 `out_small`，看二者是否相等。

示例代码（**示例代码**，CPU 可运行）：

```python
# 示例代码：验证 mhc_head 补零复用的数值等价（CPU，纯 torch）
import torch

mhc_mult = 4
hidden = 16
mhc_mult3 = mhc_mult * (mhc_mult + 2)   # 24

torch.manual_seed(0)
x = torch.randn(2, mhc_mult, hidden)             # 模拟 residual
fn_small = torch.randn(mhc_mult, mhc_mult * hidden)        # lm_head 的真实权重
fn_padded = torch.zeros(mhc_mult3, mhc_mult * hidden)
fn_padded[:mhc_mult] = fn_small                  # 底部补零

def pre_norm_fn_core(x, fn):
    # x: (..., mhc_mult, hidden) → 展平成 (B, mhc_mult*hidden)
    B = x.shape[:-2].numel()
    xf = x.reshape(B, mhc_mult * hidden)
    out = xf @ fn.t()                            # (B, fn.shape[0])，行独立
    sqrsum = (out * out).sum(dim=-1, keepdim=True)  # 分母含全部通道平方和
    out = out / torch.sqrt(sqrsum + 1e-6)        # RMSNorm
    return out

out_small  = pre_norm_fn_core(x, fn_small)        # 假想的专用 kernel（只有 mhc_mult 通道）
out_padded = pre_norm_fn_core(x, fn_padded)[:, :mhc_mult]  # 补零复用后切片

print("最大绝对差:", (out_small - out_padded).abs().max().item())
```

**需要观察的现象：** 最大绝对差应当接近 0（仅浮点舍入量级，如 `< 1e-6`）。

**预期结果：** 二者近似相等，证明补零行既不改变前 `mhc_mult` 个通道的 GEMM 输出（行独立性），也不抬高 RMSNorm 分母（零的平方为零），因此 `mixes[..., :mhc_mult]` 与专用 kernel 等价。这个 CPU 实践可直接运行，无需「待本地验证」。

> 进阶：若想验证真实 TileLang kernel 的等价性，需在 SM90/SM100 环境下用 `mhc_head` 的真实输出与一份未补零的参考做对拍——属于「待本地验证」范畴。

#### 4.4.5 小练习与答案

**练习 1：** `F.pad(fn, (0, 0, 0, mhc_mult3 - fn.shape[0]))` 里，如果把 pad 元组写成 `(0, 0, mhc_mult3 - fn.shape[0], 0)`（即改成「上方补零」），`mhc_head` 还能正常工作吗？

**参考答案：** 不能。改成上方补零会把零行放在前面、真实权重行挤到后面，于是 `mixes[..., :mhc_mult]` 切到的是零行对应的零通道，真实输出反而被切掉了；要让它工作就得同步把切片改成 `mixes[..., -mhc_mult:]`。当前的「下方补零 + 切前 `mhc_mult`」是一对配套约定，二者必须一致。

**练习 2：** 为什么 `mhc_head` 用 `mhc_head_compute_mix` 而不是直接复用 `mhc_pre_split_mixes`？

**参考答案：** `mhc_pre_split_mixes` 假设输入是完整的 `mhc_mult3` 个通道，要切成 pre/post/comb 三段并分别套不同变换（含 post_mult 缩放），还要为 post 段产出 `post_mix`/`comb_mix`。lm_head 只需要 pre 这一段、且之后没有 post 段，用 `mhc_pre_split_mixes` 既多了无用的 post/comb 计算、又需要喂 `[3]` 形状的 scale 与 `[mhc_mult3]` 的 base（与 lm_head 实际拥有的 `[1]` scale、`[mhc_mult]` base 不匹配）。专用且更简单的 `mhc_head_compute_mix` 才贴合 lm_head 的真实需求。

---

## 5. 综合实践

**综合任务：** 把本讲的三个入口串成一个最小 mhc block，并画出完整的「入口 → op → ctx 接力」调用图。

要求完成以下事项：

1. **画调用图。** 以 `expand_from_embedding → mhc_pre → 子层(identity) → mhc_post` 为主线，标注：
   - 每一步调用的 functional 入口名；
   - `mhc_pre` 内部在训练 / 推理两种态下分别走哪些 op（参考 4.2.2 的流程）；
   - `ctx = (post_mix, comb_mix)` 在前向何处产出、在 `mhc_post` 何处消费（参考 4.3）。

2. **写调用骨架（CPU 可行，示例代码）。** 不调用真实 kernel，只用形状合法的零张量走通控制流，确认 `ctx` 的形状在 pre/post 两端对齐：

```python
# 示例代码：最小 mhc block 的形状级走查（CPU，不跑 kernel）
import torch
from tile_kernels.modeling.mhc import expand_from_embedding, mhc_pre, mhc_post  # 真实导入
# 注：以下调用会触发 TileLang JIT 与 GPU kernel，需 SM90/SM100 环境。
# 在 CPU 上你只能做「形状推导」式阅读，不能真正运行；故标 待本地验证。

mhc_mult = 4
hidden = 128
N = 8
emb = torch.zeros(N, hidden, dtype=torch.bfloat16)         # 模拟 embedding
residual = expand_from_embedding(emb, mhc_mult)            # (N, mhc_mult, hidden)
mhc_mult3 = mhc_mult * (mhc_mult + 2)
fn    = torch.zeros(mhc_mult3, mhc_mult * hidden, dtype=torch.float32)
scale = torch.zeros(3, dtype=torch.float32)
base  = torch.zeros(mhc_mult3, dtype=torch.float32)

layer_input, ctx = mhc_pre(residual, fn, scale, base)      # layer_input: (N, hidden)
# 子层用 identity 占位
sublayer_out = layer_input
new_residual = mhc_post(sublayer_out, residual, ctx[0], ctx[1])  # 消费 ctx
```

3. **切换模式观察分流（待本地验证）。** 在能跑 kernel 的环境下，分别用 `torch.no_grad()` 与默认态运行上面的 block，借助 4.2.4 的 spy 技巧确认 `mhc_pre` 走了 `big_fuse` 还是四步拆分。

4. **回答收尾问题：** 整个 block 里，子层（identity）对 mhc 的 `ctx`、`mhc_mult`、混合系数有任何依赖吗？用一句话总结「不透明元组」带来的解耦收益。

**验收标准：** 调用图能清楚区分训练 / 推理两路；`ctx` 的产出点与消费点都用 `file:line` 标注（如 `functional.py:82` 产出、`post.py:28` 消费）；CPU 形状走查能确认 `post_mix` 为 `(..., mhc_mult, 1)`、`comb_mix` 为 `(..., mhc_mult, mhc_mult)`；收尾问题答出「子层完全无感知，pre/post 通过 ctx 解耦」。

## 6. 本讲小结

- `functional.py` 是 mhc 的**编排层（facade）**：它不写任何算子，只把 ops 层的多个 `autograd.Function` 按固定配方组合成 `expand_from_embedding`、`mhc_pre`、`mhc_head` 三个用户入口；`__init__.py` 对外只导出这三个。
- `mhc_pre` 按 `torch.is_grad_enabled()` **自动分流**：推理态走单融合 op `mhc_pre_big_fuse`（无反向、换带宽），训练态走 `pre_norm_fn → pre_split_mixes → sinkhorn → pre_apply_mix` 四步可反传链；两条路径都返回统一的 `(layer_input, ctx)` 契约。
- `ctx = (post_mix, comb_mix)` 是 pre/post 之间的**不透明接力棒**：对调用方不解释内部含义，只需原样传给 `mhc_post`；它让夹在中间的子层对 mhc 多股残差完全无感知，实现解耦；训练态下它是带 grad 历史的真实张量，autograd 图经它连通。
- 编排层做了**返回顺序重排**：融合 op `mhc_pre_big_fuse` 自身返回 `(post_mix, comb_mix, layer_input)`，`mhc_pre` 在推理分支把它重排成与训练分支一致的 `(layer_input, (post_mix, comb_mix))`，保证 API 稳定。
- `mhc_head` 用 **`F.pad` 补零**把 lm_head 的小权重（`[mhc_mult, ...]`）补到 `[mhc_mult3, ...]`，100% 复用 `mhc_pre_norm_fn` kernel；由于 GEMM 行独立、补零行平方为零不抬高 RMSNorm 分母，切片 `mixes[..., :mhc_mult]` 与专用 kernel 逐位等价。
- 三个入口本身都不是 `autograd.Function`，而是普通函数——可微性全部来自它们调用的 op 各自带反向；这是 u8-l1「单 op 封装」范式在「多 op 组合」场景下的自然延伸。

## 7. 下一步学习建议

- **横向补齐 ops 层封装细节：** 本讲把 ops 层当黑盒。建议接着读 u8-l3（ops 层 autograd 封装：sinkhorn / head_compute_mix），重点看 `head_compute_mix` 的反向为何按 `num_sms` 分块输出再 `.sum(0)` 聚合——这与本讲 `mhc_head` 调用的 `mhc_head_compute_mix` 直接相关。
- **纵向深入反向链：** 若想搞清 `mhc_pre` 训练链反向时梯度如何穿过 `ctx` 流回 pre 段参数，回头精读 u7-l3（Sinkhorn 自定义反向）与 u8-l1（autograd.Function 的 forward/backward 一一对应契约），再对照 `ops/pre_split_mixes.py` 的 `backward` 看整条链的反向全貌。
- **尝试综合实战（u10-l3）：** 把本讲学到的「functional 编排 + ops 封装 + ctx 接力」三件套思路，迁移到 u10-l3 的「新增一个算子的完整流程」——你会为一个新算子同时写底层 kernel、ops 层 autograd.Function、以及（如果需要组合）functional 层入口。
