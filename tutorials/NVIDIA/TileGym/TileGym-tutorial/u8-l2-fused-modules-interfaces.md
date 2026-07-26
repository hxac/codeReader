# 融合模块与算子接口

## 1. 本讲目标

上一讲（u8-l1）我们讲了 TileGym 如何用 monkey-patch 把自己的内核塞进 HuggingFace `transformers` 的建模模块。本讲顺着这条线往下走，回答两个问题：

1. **MLP 这一层到底替换成了什么？** 也就是 `apply_tilegym_kernel_to_deepseek` 里 `DeepseekV2MLP = get_fused_swiglu_module()`（以及 Gemma3 里 `Gemma3MLP = PartiallyFusedGEGLUMLP`）赋值右侧的那个「融合模块」长什么样、为什么能省 kernel。
2. **注意力那一层替换成的函数从哪里来？** 也就是 `ALL_ATTENTION_FUNCTIONS["sdpa"] = get_fmha_interface()` 这类语句里的工厂函数如何把「统一分发算子」包装成 HF 能直接调用的接口。

学完本讲你应该能够：

- 说清 `PartiallyFusedSwiGLUMLP` 把标准 SwiGLU MLP 的 **5 个 kernel 压缩到 3 个** 的每一步来源。
- 解释为什么融合模块仍然保留 `gate_proj` / `up_proj` / `down_proj` 这些「原始参数名」，以及融合权重为什么用 `register_buffer` 而不是 `nn.Parameter`。
- 区分两种「融合原语」：`silu_and_mul`（前半 SiLU × 后半）与 `geglu`（左 × GELU(右)），并理解 GEGLU 为何要把权重拼接顺序**反过来**。
- 看懂 `attn_interface.py` 里「工厂函数」的适配器套路：它自己不做计算，只负责默认值、布局转置、decode/prefill 分流，再转发到统一分发算子。
- 解释 `get_fused_swiglu_module` 为什么**刻意不走 `@dispatch`**，而 `get_swiglu_module` 却走了——两种后端集成风格的取舍。

## 2. 前置知识

本讲默认你已经掌握以下内容（来自前置讲义，这里只做最小回顾，不展开）：

- **统一分发机制**（u2-l1、u2-l2）：`ops.py` 里带 `@dispatch("算子名")` 的函数只是「统一签名 stub」，函数体只抛 `NotImplementedError`；真正实现由各后端用 `@register_impl` 挂到全局注册表 `_REGISTRY` 的同一算子名下，分发器按当前后端查表路由。
- **`silu_and_mul` 内核**（u4-l1、u4-l2）：一个融合了「劈半 + silu + 乘」的逐元素内核，输入最后一维是 `2*H`，输出 `SiLU(前半) * 后半`，并以 `torch.autograd.Function` 封装了前向+反向。
- **`matmul` 算子**（u5-l1、u5-l2）：统一分发入口 `tilegym.ops.matmul(a, b, trans_a, trans_b, static_persistent=True, use_tma=None, ...)`，支持 `trans_b=True` 表示对 B 转置。
- **monkey-patching 集成**（u8-l1）：替换发生在「实例化之前」，靠 `MODEL_TYPE_TO_APPLY_TILEGYM_FN` 表分发；MLP 通常替换整个类，注意力则把工厂函数塞进 `ALL_ATTENTION_FUNCTIONS` 字典或替换 `eager_attention_forward`。

一句话回顾关键认知：**算子名是全局键、后端是子键**，dispatch 只按当前后端查表，不关心实现语言。本讲要看的「融合模块」和「接口工厂」，都是建在这套分发之上的更高一层封装。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [src/tilegym/ops/fused_mlp.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/fused_mlp.py) | 两个融合 MLP 模块类：`PartiallyFusedSwiGLUMLP`（SwiGLU，5→3 kernel）与 `PartiallyFusedGEGLUMLP`（Gemma3 GEGLU，5→3 kernel）。 |
| [src/tilegym/ops/attn_interface.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/attn_interface.py) | 注意力接口层：`fmha_interface` / `mla_interface` 等转发函数，以及 `get_fmha_interface` / `get_attention_sink_interface` / `get_fmha_gemma3_interface` 等工厂函数。 |
| [src/tilegym/ops/ops.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/ops.py) | 统一算子接口。本讲重点看 `get_fused_swiglu_module`（**不走分发**）与对照的 `get_swiglu_module`（**走分发**），以及被融合模块内部调用的 `silu_and_mul`、`matmul`。 |
| [src/tilegym/ops/activation.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/activation.py) | `geglu` 算子 stub（GEGLU 第二步融合原语）。 |
| [src/tilegym/ops/cutile/swiglu.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/swiglu.py) | 对照样本：非融合的 `_SwiGLUMLP` 与 `@register_impl("get_swiglu_module")`。 |
| [src/tilegym/transformers/monkey_patch.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/transformers/monkey_patch.py) | 集成点：把融合模块与注意力工厂塞进 HF 建模模块。 |

## 4. 核心概念与源码讲解

### 4.1 融合 MLP：gate+up 权重融合与 5→3 kernel 压缩

#### 4.1.1 概念说明

标准 LLaMA / DeepSeek 系的 FFN（SwiGLU MLP）是这样的：

\[
\text{out} = W_{\text{down}}\big(\text{SiLU}(x W_{\text{gate}}^\top) \odot (x W_{\text{up}}^\top)\big)
\]

其中 \(\odot\) 是逐元素乘。如果老老实实按公式写，每一步都是一个独立的 GPU kernel：

| 序号 | 表达式 | 操作类型 |
| --- | --- | --- |
| 1 | `gate = gate_proj(x)` | 矩阵乘（Linear） |
| 2 | `up = up_proj(x)` | 矩阵乘（Linear） |
| 3 | `act = silu(gate)` | 逐元素 |
| 4 | `masked = act * up` | 逐元素乘 |
| 5 | `out = down_proj(masked)` | 矩阵乘（Linear） |

5 个 kernel 之间还要物化（materialize）3 个中间张量（`gate`、`up`、`act`），每个都要写一遍显存、再读一遍，是典型的「内存墙」浪费。

`PartiallyFusedSwiGLUMLP` 的核心思路是两步合并：

- **合并 1+2**：把 \(W_{\text{gate}}\) 与 \(W_{\text{up}}\) 在输出维上拼成一个大权重 \(W_{\text{fused}}\)，做**一次**矩阵乘得到一个两倍宽的中间结果。这一步把两个 GEMM 变成一个 GEMM（合并 kernel 1、2）。
- **合并 3+4**：对这个两倍宽的中间结果调用 `silu_and_mul`，**一个内核**里完成「劈半 + silu + 乘」（合并 kernel 3、4，这正是 u4-l1 讲过的内核）。
- kernel 5（down_proj）保持不变。

最终 3 个 kernel，中间只物化 1 个张量（`fused_output`）。

#### 4.1.2 核心流程

源码顶部 docstring 直接把这套对照写得清清楚楚，建议先读它再读代码。流程如下：

```
标准实现 (5 kernel):                  融合实现 (3 kernel):
  gate = gate_proj(x)        # k1        fused = x @ W_fused.T      # k1 (合并 gate+up)
  up   = up_proj(x)          # k2        glu   = silu_and_mul(fused)# k2 (合并 silu+mul)
  act  = silu(gate)          # k3        out   = down_proj(glu)     # k3
  mul  = act * up            # k4
  out  = down_proj(mul)      # k5
```

前向 3 步，注意第 1 步和第 3 步在训练 / 推理时可能走不同的 matmul 实现（见 4.1.3）。

#### 4.1.3 源码精读

**① 顶部 docstring：5 vs 3 的官方账本**

这段注释本身就是最好的讲义，逐行列出了「替换前 5 步」与「替换后 3 步」：[src/tilegym/ops/fused_mlp.py:10-32](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/fused_mlp.py#L10-L32)。`Total: 3 kernels vs 5 in standard implementation` 就是本模块的账面结论。

**② forward 三步**

```python
# Step 1: 融合 gate+up 投影（一次 matmul 顶原来的两次 Linear）
fused_output = matmul_fn(x, self.fused_gate_up_weight, trans_b=True)   # fused_mlp.py:96

# Step 2: 融合 SiLU + 乘（u4-l1 的 silu_and_mul 内核）
from tilegym.ops import silu_and_mul
glu_output = silu_and_mul(fused_output)                                # fused_mlp.py:101

# Step 3: down 投影
result = matmul_fn(glu_output, self.down_proj.weight, trans_b=True)    # fused_mlp.py:105
```

引用：[src/tilegym/ops/fused_mlp.py:94-105](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/fused_mlp.py#L94-L105)。注意 `trans_b=True`：`fused_gate_up_weight` 形状是 `[2*intermediate, hidden]`，而 PyTorch 的 `nn.Linear` 权重就是 `[out, in]`，做 `x @ W.T` 需要转置 B，所以这里统一传 `trans_b=True`。

**③ matmul_fn：训练走 torch，推理走 tilegym**

这里有个容易忽略的细节——`matmul_fn` 不是固定的：

```python
matmul_fn = self.apply_matmul if use_torch_matmul else self.apply_matmul_internal  # fused_mlp.py:89
```

两个实现分别长这样：

```python
def apply_matmul(self, x, weight, trans_b):                 # fused_mlp.py:109-110
    return torch.matmul(x, weight.T if trans_b else weight)

def apply_matmul_internal(self, x, weight, trans_b):        # fused_mlp.py:112-115
    from tilegym.ops import matmul
    return matmul(x, weight, trans_b=trans_b, use_tma=True, static_persistent=True)
```

`apply_matmul_internal` 调的是**统一分发**的 `tilegym.ops.matmul`，即 u5 讲过的 cuTile 分块 GEMM（`use_tma=True`、`static_persistent=True` 指定走 TMA + 持久化路径）。但 `apply_matmul_internal` 当前只在前向、且 `requires_grad=False` 的纯推理场景才被选中：

```python
if use_torch_matmul is None:
    use_torch_matmul = x.requires_grad                      # fused_mlp.py:85-86
```

也就是说：默认情况下，**当输入需要反向（训练）时，融合模块退回 `torch.matmul`**（因为 `torch.matmul` 自带 autograd，而当前 cuTile `matmul` 主要面向前向/推理基准）。这是「融合模块」权衡正确性与性能的一个关键开关，也是为什么它叫 **Partially** Fused——融合了 silu+mul 与 gate+up 权重，但 GEMM 在训练时未必用自定义内核。

> 待确认：不同 TileGym 版本里 `matmul` 是否提供完整反向会变化；以你本地版本的 `tilegym.ops.cutile.matmul` 是否注册了 backward 为准。

#### 4.1.4 代码实践

**实践目标**：亲手把「5→3」的账算一遍，并验证融合权重确实等于 `cat([gate, up])`。

**操作步骤**（阅读型 + 可选运行型）：

1. 打开 [src/tilegym/ops/fused_mlp.py:21-31](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/fused_mlp.py#L21-L31)，对照本讲 4.1.1 的表格，把 5 个原始 kernel 两两分组合并成 3 个，写出每一步的「来源」。
2. （可选运行）写一段最小脚本，构造一个假的 `config`，实例化 `PartiallyFusedSwiGLUMLP`，跑一次 forward，并断言 `fused_gate_up_weight` 等于手动 cat 的结果。

```python
# 示例代码（非项目原有，仅用于理解）
from types import SimpleNamespace
import torch
from tilegym.ops.fused_mlp import PartiallyFusedSwiGLUMLP

cfg = SimpleNamespace(hidden_size=64, intermediate_size=128, hidden_act="silu")
mlp = PartiallyFusedSwiGLUMLP(cfg).cuda().eval()
x = torch.randn(2, 8, 64, device="cuda")
out = mlp(x)                       # 触发 _initialize_fused_weights()

# 验证融合权重 == cat([gate_proj.weight, up_proj.weight])
expect = torch.cat([mlp.gate_proj.weight, mlp.up_proj.weight], dim=0)
print("fused == cat(gate,up):", torch.equal(mlp.fused_gate_up_weight, expect))
```

**需要观察的现象 / 预期结果**：

- 5→3 的来源：kernel 1+2（两个 Linear）→ 1 次 matmul（权重拼接）；kernel 3+4（silu 与逐元素乘）→ 1 个 `silu_and_mul`；kernel 5（down）不变。
- 断言应打印 `True`，说明融合权重确实是两个原始权重的拼接，没有引入新的可训练参数。
- 若在无 GPU / cutile 不可用的环境运行，`out = mlp(x)` 可能因 `apply_matmul_internal` 走分发 matmul 而失败；此时把脚本里 `mlp(x)` 的输入设为 `requires_grad=True` 即可强制走 `torch.matmul` 分支验证逻辑（但那样 `fused_gate_up_weight` 仍会被初始化）。

> 待本地验证：上述脚本的实际运行结果取决于本地后端可用性。

#### 4.1.5 小练习与答案

**练习 1**：如果某模型的 FFN 用的是「不带门控」的 ReLU MLP（`down_proj(relu(gate_proj(x)))`），`PartiallyFusedSwiGLUMLP` 还能套用吗？为什么？

> **答案**：不能直接套。ReLU MLP 只有一个上投影、没有 `up_proj`，也就没有「两半相乘」的结构，`silu_and_mul` 这一步融合无从谈起。`PartiallyFusedSwiGLUMLP` 的前提正是门控激活（GLU）族。而且构造时会校验 `config.hidden_act in ["silu", "swish"]`，否则抛 `ValueError`（见 [fused_mlp.py:50-57](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/fused_mlp.py#L50-L57)）。

**练习 2**：`forward` 里为什么先 `x = x.view(-1, x.shape[-1])`，最后又 `result.view(*orig_shape)`？

> **答案**：把 `[batch, seq, hidden]` 折叠成 `[batch*seq, hidden]` 的二维矩阵，是为了让下面的 `matmul` / `silu_and_mul` 走二维 GEMM 与逐元素行内核（它们都按二维 `[M, N]` 组织）。算完再把形状还原成 `[batch, seq, hidden]` 返回，对调用方透明。

### 4.2 权重融合与 checkpoint 兼容

#### 4.2.1 概念说明

融合权重带来一个显而易见的矛盾：

- **要融合**，就需要一个拼接好的 `W_fused` 才能做「一次 matmul」。
- **要加载预训练权重**，HuggingFace checkpoint 的 `state_dict` 里存的键是规范名 `gate_proj.weight`、`up_proj.weight`、`down_proj.weight`，根本没有 `fused_gate_up_weight` 这个键。

如果直接把模块改成只存一个融合权重，加载 checkpoint 时就会因为键名对不上而丢权重。`PartiallyFusedSwiGLUMLP` 的解法是「**两个都要**」：

- 保留三个标准 `nn.Linear`（`gate_proj` / `up_proj` / `down_proj`），它们的 `.weight` 就是 checkpoint 里的规范名——加载时照常匹配。
- 另外用一个 **buffer**（不是可训练参数）`fused_gate_up_weight` 存拼接结果，**延迟到首次 forward 才生成**。

这样 checkpoint 兼容性完全不变，融合只在运行期发生。

#### 4.2.2 核心流程

```
构造时:  注册 gate_proj / up_proj / down_proj (nn.Linear, 规范参数名)
         注册 buffer fused_gate_up_weight = None        # 占位，尚未拼接
首次 forward:
         若 fused_gate_up_weight is None:
             fused_gate_up_weight = cat([gate_proj.weight, up_proj.weight], dim=0)
权重更新后 (load_state_dict / 训练步):
         调用 update_fused_weights() 重新拼接
```

#### 4.2.3 源码精读

**① 保留三个原始 Linear**

注释直说「为了 checkpoint 兼容而保留独立权重」：[src/tilegym/ops/fused_mlp.py:40-43](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/fused_mlp.py#L40-L43)

```python
# Keep individual weights for checkpoint compatibility
self.gate_proj = nn.Linear(self.hidden_size, self.intermediate_size, bias=False)
self.up_proj   = nn.Linear(self.hidden_size, self.intermediate_size, bias=False)
self.down_proj = nn.Linear(self.intermediate_size, self.hidden_size, bias=False)
```

**② 融合权重是 buffer，初始为 None**

[src/tilegym/ops/fused_mlp.py:45-47](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/fused_mlp.py#L45-L47)

```python
self.register_buffer("fused_gate_up_weight", None)
```

为什么是 `register_buffer` 而不是 `nn.Parameter`？因为它是**派生（冗余）数据**——真值在 `gate_proj.weight` 和 `up_proj.weight` 里。做成 buffer（而非 Parameter）有两个好处：优化器不会把它当可训练参数去更新它（避免双份）；语义上明确「它只是缓存」。两个 `nn.Linear` 的权重始终是单一真值来源。

**③ 首次 forward 延迟拼接**

[src/tilegym/ops/fused_mlp.py:59-66](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/fused_mlp.py#L59-L66)

```python
def _initialize_fused_weights(self):
    with torch.no_grad():
        # gate_proj.weight: [intermediate, hidden]
        # up_proj.weight:   [intermediate, hidden]
        # fused:            [2*intermediate, hidden]
        self.fused_gate_up_weight = torch.cat(
            [self.gate_proj.weight, self.up_proj.weight], dim=0)
```

调用时机在 `forward` 开头：`if self.fused_gate_up_weight is None: self._initialize_fused_weights()`（[fused_mlp.py:78-79](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/fused_mlp.py#L78-L79)）。延迟到 forward 是因为构造时权重可能还没加载好（HF 的流程是「先实例化空模型，再 `load_state_dict`」）。

**④ 权重变了要手动刷新**

[src/tilegym/ops/fused_mlp.py:117-122](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/fused_mlp.py#L117-L122) 的 `update_fused_weights()` 重新拼接。这是延迟缓存方案的代价：`load_state_dict` 会就地改写 `gate_proj.weight`，但**不会**自动重算 buffer，所以加载后（或训练每步后若用融合路径）需要调用它刷新。注意推理 monkey-patch 路径下，权重只在加载时变一次，首次 forward 才生成 buffer，所以通常不需要额外调用。

#### 4.2.4 代码实践

**实践目标**：验证「融合权重不在 checkpoint 键里，但加载 checkpoint 后能正确生成」。

**操作步骤**：

1. 实例化 `mlp = PartiallyFusedSwiGLUMLP(cfg)`，打印 `dict(mlp.named_parameters())` 的键。
2. 构造一个伪造的 `state_dict = {"gate_proj.weight": ..., "up_proj.weight": ..., "down_proj.weight": ...}`，调用 `mlp.load_state_dict(state_dict)`。
3. 调一次 `mlp(x)`（或显式 `mlp._initialize_fused_weights()`），再打印 `mlp.fused_gate_up_weight` 的形状。

**预期结果**：

- 第 1 步：参数键只有 `gate_proj.weight` / `up_proj.weight` / `down_proj.weight`，**没有** `fused_gate_up_weight`（它是 buffer 不是 parameter）。
- 第 2 步：`load_state_dict` 正常成功（键名匹配），不报缺失/多余。
- 第 3 步：`fused_gate_up_weight.shape == torch.Size([2*intermediate_size, hidden_size])`，且前半等于新加载的 `gate_proj.weight`、后半等于 `up_proj.weight`。

> 待本地验证：不同 PyTorch 版本对值为 `None` 的 buffer 在 `state_dict()` 中的表现略有差异；以本地版本实测为准。核心结论（参数键不含融合权重、加载后再派生）稳定成立。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `register_buffer("fused_gate_up_weight", None)` 改成 `self.fused_gate_up_weight = nn.Parameter(...)`，会有什么坏处？

> **答案**：① 它会被优化器当成可训练参数，但它的值完全由 `gate_proj.weight`/`up_proj.weight` 决定，梯度上会产生重复更新甚至冲突；② 它会和真值参数「谁是 source of truth」语义混乱；③ checkpoint 里会多出一个非规范键。所以用 buffer 才正确。

**练习 2**：为什么 `_initialize_fused_weights` 要套 `torch.no_grad()`？

> **答案**：拼接操作 `torch.cat` 本身可微，但这里只是把已有的 leaf 参数重组为一份缓存，不需要建立计算图、也不应让 autograd 跟踪这份冗余拷贝。`no_grad` 避免无谓的计算图开销，也防止它在反向中被误当成中间结果。

### 4.3 silu_and_mul 融合步与 GEGLU 顺序陷阱

#### 4.3.1 概念说明

4.1 讲了融合 MLP 的「第一步」（权重拼接 GEMM），这一讲讲「第二步」——激活融合原语。TileGym 提供了两个**语义不同**的门控激活原语，决定了对中间张量的解析方式：

| 原语 | 公式 | 输入最后一维 | 适用模型 |
| --- | --- | --- | --- |
| `silu_and_mul(x)` | `SiLU(x[:, :H]) * x[:, H:]`（前半过 SiLU，乘以后半） | `2H` | LLaMA / DeepSeek / Qwen（SwiGLU） |
| `geglu(x, dim=-1)` | `left * GELU(right)`，左半直接乘右半的 GELU | `2H` | Gemma3（GEGLU） |

注意两者都把 `2H` 输入劈成两半，但**哪一半过激活函数**不同：

- `silu_and_mul`：**前半**过 SiLU，再乘后半。
- `geglu`：**右半**（后半）过 GELU，左半原样乘上去。

这个差异会反噬到「权重该怎么拼接」。

#### 4.3.2 核心流程

设拼接后的两半分别为 `A`（前半，对应拼接时第一个权重）和 `B`（后半，对应第二个权重）。

- SwiGLU 想要 `SiLU(gate) * up`。用 `silu_and_mul`（前半过 SiLU），所以前半要是 `gate` → 拼接顺序 `cat([gate, up])`。
- GEGLU 想要 `GELU(gate) * up`。用 `geglu`（右半过 GELU），所以右半要是 `gate` → 拼接顺序 `cat([up, gate])`，**顺序正好反过来**。

```
SwiGLU:  cat([gate, up]) ──silu_and_mul──> SiLU(gate) * up     ✓
GEGLU :  cat([up,  gate]) ──geglu────────> up * GELU(gate)
                                       = GELU(gate) * up        ✓
```

#### 4.3.3 源码精读

**① silu_and_mul：统一签名 stub**

[src/tilegym/ops/ops.py:172-193](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/ops.py#L172-L193)——注意这个 stub **没有** `fallback_backend`，意味着它默认不降级，缺失即报错（参考 u2-l1 的逐算子 fallback 概念）。其 cuTile 实现就是 u4-l1/u4-l2 精读过的那个内核。

**② SwiGLU 的拼接顺序**

`PartiallyFusedSwiGLUMLP._initialize_fused_weights` 拼接顺序是 `[gate, up]`：[src/tilegym/ops/fused_mlp.py:66](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/fused_mlp.py#L66)，配合 `silu_and_mul`（前半过 SiLU）得到 `SiLU(gate)*up`，正确。

**③ GEGLU 的反序拼接**

`PartiallyFusedGEGLUMLP` 是给 Gemma3 用的（Gemma3 的 MLP 是 `down_proj(GELU(gate) * up)`）。它的 docstring 和实现都强调「**反序**」：

[src/tilegym/ops/fused_mlp.py:160-175](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/fused_mlp.py#L160-L175)

```python
# REVERSED ORDER: [up, gate] instead of [gate, up]
self.fused_up_gate_weight = torch.cat([self.up_proj.weight, self.gate_proj.weight], dim=0)
```

其 `forward` 第 2 步调用的是 `geglu`：[src/tilegym/ops/fused_mlp.py:186-198](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/fused_mlp.py#L186-L198)

```python
from tilegym.ops.activation import geglu
geglu_output = geglu(fused_output, dim=-1, approximate=self.approximate)
```

`geglu` 的语义是 `left * GELU(right)`（[activation.py:32-48](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/activation.py#L32-L48)）。因为拼接顺序是 `[up, gate]`，所以 `left=up`、`right=gate`，得到 `up * GELU(gate) = GELU(gate) * up`，与 Gemma3 的定义吻合。

**④ GELU 近似模式**

GEGLU 还会按 config 选 GELU 的近似：[src/tilegym/ops/fused_mlp.py:155-158](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/fused_mlp.py#L155-L158)，当 `hidden_activation` 含 `gelu_pytorch_tanh` 或 `gelu_new` 时用 `approximate="tanh"`，否则用精确 `"none"`。这与 Gemma3 官方实现保持一致，避免近似方式不匹配带来的数值偏差。

#### 4.3.4 代码实践

**实践目标**：验证「调换拼接顺序会改变 GEGLU 结果」，从而体会顺序陷阱。

**操作步骤**：

1. 取一组随机权重 `W_gate`、`W_up`，输入 `x`。
2. 分别构造 `W_a = cat([gate, up])` 与 `W_b = cat([up, gate])`，对各自 `fused = x @ W.T`。
3. 对 `W_a` 的 fused 调 `geglu`，对 `W_b` 的 fused 也调 `geglu`，再与参考 `GELU(gate)*up` 比较。

**预期结果**：

- `geglu(cat([up, gate]))`（即 `W_b`）≈ `GELU(gate) * up`，与参考一致。
- `geglu(cat([gate, up]))`（即 `W_a`）≈ `gate * GELU(up)`，与参考**不一致**——这正说明顺序不能搞反。

> 待本地验证：实际数值取决于本地 `geglu` 后端实现是否可用；若不可用，可用 `torch.nn.functional.gelu` 手写参考来对照逻辑。

#### 4.3.5 小练习与答案

**练习 1**：为什么不直接在 GEGLU 里也用 `silu_and_mul`，而非要单独搞一个 `geglu` 原语？

> **答案**：激活函数不同（SiLU vs GELU），且「哪一半过激活」的约定不同。`silu_and_mul` 把 SiLU 固定作用在前半；要让 GELU 作用在 gate 上、又保持 `cat([gate,up])` 的顺序去复用 `silu_and_mul` 是做不到的（除非改顺序）。TileGym 选择提供语义清晰的 `geglu`（左 × GELU(右)）并相应调整拼接顺序，让每种激活都有自己的「正确拼法」。

**练习 2**：`silu_and_mul` 的 stub 没有 `fallback_backend`，而 `rms_norm` 有 `fallback_backend="triton"`（[ops.py:134-137](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/ops.py#L134-L137)）。这对融合 MLP 意味着什么？

> **答案**：意味着如果当前后端没有 `silu_and_mul` 实现，`PartiallyFusedSwiGLUMLP.forward` 调 `silu_and_mul` 会直接抛 `NotImplementedError`，不会悄悄降级到别的实现。融合模块对 `silu_and_mul` 的存在是强依赖。

### 4.4 attn_interface 工厂：把分发算子包装成 HF 接口

#### 4.4.1 概念说明

融合 MLP 解决「MLP 层怎么换」，注意力接口解决「注意力层怎么换」。问题在于：TileGym 的统一算子（如 `tilegym.ops.fmha`）签名是「内核友好」的；而 HuggingFace 的注意力函数（如 `eager_attention_forward` 或 `ALL_ATTENTION_FUNCTIONS` 里的函数）签名是「框架友好」的——它多带一个 `module` 参数、`attention_mask` 参数、默认缩放、特定的张量布局（`[B, H, S, D]`）。两边对不上。

`attn_interface.py` 就是一层**适配器（adapter）**：它自己不做任何内核计算，只做三件事——

1. **补默认值**：例如 scaling 缺省时取 `1/sqrt(head_dim)`。
2. **分流**：根据 query 序列长度判断是 prefill（多 token）还是 decode（单 token），分别转发到不同算子（`fmha` vs `fmha_decode`）。
3. **转布局**：把 HF 的 `[B, H, S, D]` 转成内核要的形状，算完再转回去。

而为了能「预先绑死 backend / kernel_configs，再交出去给 HF 当函数用」，它采用了**工厂模式**：`get_fmha_interface(backend=None, kernel_configs=None)` 返回一个闭包 `fmha_interface_wrapper`，闭包里捕获了 backend 等配置。

#### 4.4.2 核心流程

以 FMHA 为例，两层结构：

```
get_fmha_interface(backend, kernel_configs)        # 工厂，返回闭包
   └─> fmha_interface_wrapper(module, q, k, v, attention_mask, dropout, scaling, ...)
          ├─ 若 q.size(-2) == 1 (单 token decode):
          │      return fmha_decode(q, k, v, sm_scale=scaling), None
          ├─ 否则 (prefill):
          │      o = fmha_interface(q, k, v, is_causal, scaling, backend, ...)  # 转发
          │      return o.transpose(1,2).contiguous(), None
          │
fmha_interface(q, k, v, ...)                        # 薄壳，仅转发
   └─> tilegym.ops.fmha(q, k, v, ...)               # 统一分发入口 → _REGISTRY 查表
```

工厂返回的 `fmha_interface_wrapper` 正是 u8-l1 里 `ALL_ATTENTION_FUNCTIONS["sdpa"] = get_fmha_interface()` 赋值的那个函数，签名与 HF 期望的一致。

#### 4.4.3 源码精读

**① `fmha_interface`：纯转发的薄壳**

[src/tilegym/ops/attn_interface.py:28-70](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/attn_interface.py#L28-L70)。注意它函数体里 `from tilegym.ops import fmha` 然后 `return fmha(...)`——自己完全不算 attention，只是把参数整理后交给统一分发入口。这种「延迟 import」还顺带规避了循环导入（接口层与 ops 层互相引用）。

**② `get_fmha_interface`：工厂 + decode/prefill 分流**

[src/tilegym/ops/attn_interface.py:73-122](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/attn_interface.py#L73-L122)。三个要点：

```python
if scaling is None:
    scaling = 1.0 / math.sqrt(q.size(-1))            # 默认缩放（第97-98行）

if q.size(-2) == 1:                                   # 单 token → decode 内核
    from tilegym.ops import fmha_decode
    return fmha_decode(q, k, v, sm_scale=scaling), None

o = fmha_interface(q, k, v, is_causal=is_causal, scaling=scaling,
                   backend=backend, ...)              # 多 token → prefill 内核
return o.transpose(1, 2).contiguous(), None           # 转回 HF 布局
```

- `q.size(-2)` 是 query 的序列维：等于 1 说明是逐 token 解码，转走 `fmha_decode`（u6-l2 讲过的 split-KV 解码内核）；否则走 prefill `fmha`（u6-l1）。
- 返回值是 `(output, None)`——元组第二个元素是过去权重，TileGym 的内核不算它，返回 `None` 以满足 HF 的 `eager_attention_forward` 约定。
- `o.transpose(1, 2).contiguous()`：内核输出 `[B, H, S, D]`，HF 想要 `[B, S, H, D]`，故转置。

**③ 同款工厂模式的其他实例**

文件里还有两个结构完全一样的工厂，只是分流逻辑与目标算子不同：

- `get_attention_sink_interface`（[attn_interface.py:239-329](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/attn_interface.py#L239-L329)）：给 GPT-OSS 用，从 `module` 上取 `sinks`、`sliding_window`，decode（`seq_len_q==1`）时转 `attention_sink_decode`，否则 `attention_sink`。
- `get_fmha_gemma3_interface`（[attn_interface.py:456-560](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/attn_interface.py#L456-L560)）：给 Gemma3 用，额外处理 `softcap`（soft cap，u6-l4）与 `sliding_window`，decode 走 `gemma_attention_decode`、prefill 走 `gemma_attention`。

这三者共同体现了「**一个算子族 → 一个工厂**」的组织方式：工厂是 HF 世界与 TileGym 分发世界之间的翻译层。

**④ 工厂在 monkey_patch 里的接线**

[src/tilegym/transformers/monkey_patch.py:12-13](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/transformers/monkey_patch.py#L12-L13) 把工厂 import 进来，随后在各个模型的 patch 函数里 `ALL_ATTENTION_FUNCTIONS["sdpa"] = get_fmha_interface()`（如 [monkey_patch.py:61](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/transformers/monkey_patch.py#L61)、[155](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/transformers/monkey_patch.py#L155)、[390](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/transformers/monkey_patch.py#L390)）。这正是 u8-l1 讲过的「往 `ALL_ATTENTION_FUNCTIONS` 字典注册」那一路替换。

#### 4.4.4 代码实践

**实践目标**：跟踪一次「HF 风格调用 → 工厂 → 统一分发」的完整转发链，确认接口层不做计算。

**操作步骤**：

1. 在 [attn_interface.py:59](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/attn_interface.py#L59) 的 `from tilegym.ops import fmha` 与第 109 行 `o = fmha_interface(...)` 处各设一处断点（或加 `print`）。
2. 用一个最小脚本调用 `fn = get_fmha_interface(); fn(module, q, k, v, None)`，其中 `q.shape = (1, 8, 16, 64)`（prefill，seq=16）。
3. 把 `q` 换成 `(1, 8, 1, 64)`（decode，seq=1）再调一次。

**需要观察的现象**：

- prefill 调用进入了 `fmha_interface` → `tilegym.ops.fmha`（prefill 内核）。
- decode 调用**没有**进入 `fmha_interface`，而是走了 `q.size(-2)==1` 分支直接调 `fmha_decode`。
- 两次返回的第二个元素都是 `None`。

> 待本地验证：实际能否跑通取决于本地注意力后端是否可用；即便不能运行，通过阅读 4.4.3 的两个分支也能确认这条分流逻辑。

#### 4.4.5 小练习与答案

**练习 1**：`get_fmha_interface` 为什么要把 `backend` 作为工厂参数，而不是让 wrapper 每次都从全局取当前后端？

> **答案**：工厂模式允许在「构造接口时」就把 backend（或 kernel_configs）绑死进闭包，调用方（HF）只需用一套固定签名调用，不必知道后端存在。这种「先配置、后使用」的闭包，正好适配 `ALL_ATTENTION_FUNCTIONS["sdpa"] = get_fmha_interface()` 这种「注册一个现成可调用对象」的接法。当然，wrapper 内部仍然可以走统一分发，backend 只是默认值。

**练习 2**：为什么 wrapper 的返回值要带一个 `None`？

> **答案**：HF 的 `eager_attention_forward` 约定返回 `(attn_output, attn_weights)`。TileGym 的内核为效率不算注意力权重，所以用 `None` 占位，保持签名兼容。

### 4.5 get_fused_swiglu_module：刻意不走 @dispatch 的工厂

#### 4.5.1 概念说明

讲到这里，细心的读者会发现一个看似不一致的地方：`ops.py` 里有两个名字相近的工厂，但**装饰器不一样**。

- `get_swiglu_module`：带 `@dispatch("get_swiglu_module")`，是统一分发的 stub，各后端用 `@register_impl` 注册自己的 MLP 类（cuTile 注册的是 `_SwiGLUMLP`）。
- `get_fused_swiglu_module`：**没有** `@dispatch`，是个普通 Python 函数，函数体直接 `from tilegym.ops.fused_mlp import PartiallyFusedSwiGLUMLP; return PartiallyFusedSwiGLUMLP`。

为什么融合版「不需要分发」？答案藏在 4.1.3 讲过的事实里：`PartiallyFusedSwiGLUMLP.forward` **内部调用的就是统一分发算子**（`tilegym.ops.matmul`、`tilegym.ops.silu_and_mul`）。也就是说，后端选择发生在「前向时的算子层」，而不是「模块类层」。模块类本身对所有后端都一样，自然不需要按后端分发不同的类。

这是两种不同的后端集成风格：

| 风格 | 代表 | 模块类 | 后端选择发生在 |
| --- | --- | --- | --- |
| A：分发模块工厂 | `get_swiglu_module` | 每个后端一个类（cuTile 的 `_SwiGLUMLP`） | 取类时（`@dispatch` 查表） |
| B：后端无关模块 + 内部分发算子 | `get_fused_swiglu_module` | 所有后端共用一个类 | forward 内部调算子时 |

#### 4.5.2 核心流程

```
风格 A (get_swiglu_module):
  ops.py: @dispatch("get_swiglu_module") stub
  cutile/swiglu.py: @register_impl("get_swiglu_module", backend="cutile") → 返回 _SwiGLUMLP
  _SwiGLUMLP.forward → 直接 import 的 swiglu(a,b) 内核（cuTile 专属）

风格 B (get_fused_swiglu_module):
  ops.py: 普通 def，直接 return PartiallyFusedSwiGLUMLP
  fused_mlp.py: PartiallyFusedSwiGLUMLP.forward → tilegym.ops.matmul / tilegym.ops.silu_and_mul（分发）
```

#### 4.5.3 源码精读

**① `get_fused_swiglu_module`：无 `@dispatch` 的普通函数**

[src/tilegym/ops/ops.py:113-131](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/ops.py#L113-L131)

```python
def get_fused_swiglu_module():
    """
    ...
    Note: This doesn't need backend dispatch - the PartiallyFusedSwiGLUMLP class automatically
    dispatches to the correct backend kernel internally.
    """
    from tilegym.ops.fused_mlp import PartiallyFusedSwiGLUMLP
    return PartiallyFusedSwiGLUMLP
```

官方注释点明了原因：「类内部自动分发到正确后端内核」。所以这一层不必再分发。

**② 对照：`get_swiglu_module` 走分发**

[src/tilegym/ops/ops.py:81-93](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/ops.py#L81-L93) 是带 `@dispatch` 的 stub；cuTile 侧 [src/tilegym/ops/cutile/swiglu.py:176-178](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/swiglu.py#L176-L178) 用 `@register_impl` 返回 `_SwiGLUMLP`。这个 `_SwiGLUMLP.forward`（[swiglu.py:172-173](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/swiglu.py#L172-L173)）调的是**同文件内直接 import 的 cuTile 专属 `swiglu` 内核**（[swiglu.py:155-157](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/swiglu.py#L155-L157)），后端在「取类」时就已定死。

**③ 风格 B 内部分发的证据**

回到 [fused_mlp.py:91-101](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/fused_mlp.py#L91-L101)，`forward` 里 `from tilegym.ops import silu_and_mul` 与 `from tilegym.ops import matmul`（在 `apply_matmul_internal` 里）——这俩都是统一分发入口。模块类自己不分发，把后端选择「下放」给了它调用的算子。

**④ 对调用方完全透明**

无论 A 还是 B，在 monkey_patch 侧都是一句赋值：

- DeepSeek：`modeling_deepseek.DeepseekV2MLP = get_fused_swiglu_module()`（[monkey_patch.py:104](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/transformers/monkey_patch.py#L104)）。
- Gemma3：`modeling_gemma.Gemma3MLP = PartiallyFusedGEGLUMLP`（[monkey_patch.py:327-329](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/transformers/monkey_patch.py#L327-L329)，这里连工厂都省了，直接赋类）。

调用方（HF 的 `DecoderLayer`）拿到的是一个 `nn.Module` 子类，构造它、调它的 `forward`，既不知道也不关心后端选择发生在哪一层。这就是良好分层的益处。

#### 4.5.4 代码实践

**实践目标**：用「是否带 `@dispatch`」作为判据，区分 `ops.py` 里的两类工厂，并验证风格 B 的模块确实内部分发。

**操作步骤**：

1. 在 [ops.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/ops.py) 里搜索 `get_swiglu_module` 与 `get_fused_swiglu_module` 的定义，确认前者有 `@dispatch(...)`、后者没有。
2. 用 `tilegym.backend.get_registry_info()`（u2-l2 提到的自省函数；若该 API 名在不同版本有差异，以本地版本为准）查看注册表里是否存在 `"get_swiglu_module"` 键及其后端子键，确认 `"get_fused_swiglu_module"` **不在**注册表里。
3. 在 `PartiallyFusedSwiGLUMLP.forward` 的 `silu_and_mul(fused_output)` 处加一行日志，切换 `tilegym.set_backend(...)` 后再调一次，观察内部分发是否随当前后端变化。

**预期结果**：

- 第 1 步：装饰器差异确认。
- 第 2 步：`get_swiglu_module` 在注册表中（有 cutile 等后端子键），`get_fused_swiglu_module` 不在。
- 第 3 步：`forward` 内部的 `silu_and_mul` 会按当前后端路由到不同实现（前提是该后端注册了 `silu_and_mul`），证明风格 B 的「后端下放到算子层」。

> 待本地验证：第 2、3 步取决于本地 `tilegym.backend` 暴露的自省 API 与已注册的后端。

#### 4.5.5 小练习与答案

**练习 1**：如果将来 `silu_and_mul` 与 `matmul` 在所有后端都实现了完整前向+反向，风格 B 相对风格 A 还有什么优势？

> **答案**：风格 B 只需要维护**一个**模块类（`PartiallyFusedSwiGLUMLP`），新增后端时只要给 `matmul`/`silu_and_mul` 注册实现即可，模块层零改动；风格 A 则每个后端都要写一个 MLP 子类。所以当「内核原语」被多后端普遍实现后，风格 B 的复用度更高、维护更省。

**练习 2**：`get_fused_swiglu_module` 不走 `@dispatch`，那它怎么被 `from tilegym.ops import get_fused_swiglu_module` 导入的？

> **答案**：因为它就是 `ops.py` 里的一个普通顶层函数，靠 `ops/__init__.py` 的 `from .ops import *`（[ops/__init__.py:52](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/__init__.py#L52)）自然暴露。分发不是导出的前提，只是「按后端选实现」的机制——这个函数不需要选实现，所以也不用分发。

## 5. 综合实践

把本讲四块知识串起来，做一个「替换 DeepSeek MLP + 注意力」的迷你端到端追踪。本实践为**源码阅读型 + 可选运行型**。

**任务**：对照真实集成代码，画出从「HF 调用一次 MLP / Attention」到「TileGym 内核」的完整调用链，并标注每一步属于本讲的哪个模块。

**步骤**：

1. **MLP 链路（融合模块 + 权重融合 + silu_and_mul）**。从 [monkey_patch.py:104](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/transformers/monkey_patch.py#L104) 的 `DeepseekV2MLP = get_fused_swiglu_module()` 出发，画出：

   ```
   HF DecoderLayer 调 self.mlp(x)
     → PartiallyFusedSwiGLUMLP.forward(x)                 # 4.5 风格 B，后端无关
        → (首次) _initialize_fused_weights()              # 4.2 延迟生成 buffer
        → matmul_fn(x, fused_gate_up_weight, trans_b=True) # 4.1 第1步（5→3 的 1）
        → tilegym.ops.silu_and_mul(fused_output)          # 4.3 第2步（分发，u4-l1 内核）
        → matmul_fn(glu_output, down_proj.weight, True)   # 4.1 第3步
   ```

   在每一步旁边标注它对应本讲哪个小节（4.1 / 4.2 / 4.3 / 4.5）。

2. **Attention 链路（工厂接口）**。从 [monkey_patch.py:61](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/transformers/monkey_patch.py#L61) 的 `ALL_ATTENTION_FUNCTIONS["sdpa"] = get_fmha_interface()` 出发，画出：

   ```
   HF Attention 调 ALL_ATTENTION_FUNCTIONS["sdpa"](module, q, k, v, mask, ...)
     → fmha_interface_wrapper(...)                        # 4.4 工厂闭包
        ├─ seq=1: tilegym.ops.fmha_decode(...)            # decode 分流
        └─ seq>1: fmha_interface(...)
                   → tilegym.ops.fmha(...)                # 统一分发（u6-l1 内核）
                   → o.transpose(1,2).contiguous()
   ```

3. **（可选运行）数值自检**。构造一个 `PartiallyFusedSwiGLUMLP` 与一个手写的「5-kernel 参考」（直接 `down_proj(silu(gate_proj(x)) * up_proj(x))`，共享同一组权重），用同一输入跑两遍，比较最大绝对误差：

   ```python
   # 示例代码（非项目原有）
   import torch, torch.nn.functional as F
   # mlp 已实例化并加载了 gate/up/down 权重
   x = torch.randn(2, 8, mlp.hidden_size, device="cuda")
   fused_out = mlp(x)
   ref_out = mlp.down_proj(F.silu(mlp.gate_proj(x)) * mlp.up_proj(x))
   print("max abs err:", (fused_out - ref_out).abs().max().item())
   ```

**预期结果**：

- 两条链路图清晰标注了本讲四块知识的落点。
- 数值自检的误差应极小（同构计算，仅浮点顺序差异）；若误差较大，先排查 `update_fused_weights()` 是否在加载权重后被调用、`fused_gate_up_weight` 是否对应最新权重。

> 待本地验证：运行型步骤依赖 GPU 与可用的 cuTile 后端；纯阅读型步骤（画链路图）在任何环境都能完成。

## 6. 本讲小结

- `PartiallyFusedSwiGLUMLP` 把标准 SwiGLU 的 5 个 kernel 压成 3 个：①两个 Linear 合并为一次 matmul（权重拼接）；②`silu` 与逐元素乘合并为一个 `silu_and_mul` 内核；③down_proj 不变。
- 融合权重 `fused_gate_up_weight` 是 **buffer** 而非 Parameter，由 `gate_proj.weight` / `up_proj.weight` 拼接而成、首次 forward 延迟生成；三个 `nn.Linear` 保留规范参数名以保证 checkpoint 兼容，加载后需调 `update_fused_weights()` 刷新。
- 第二步融合原语有两种语义：`silu_and_mul`（前半过 SiLU × 后半）配 `[gate, up]` 拼接；`geglu`（左 × GELU(右)）配反序的 `[up, gate]` 拼接——「哪一半过激活」决定了权重拼接顺序。
- `attn_interface.py` 是一层适配器：`get_fmha_interface` 等工厂返回 HF 兼容的闭包，负责默认缩放、decode/prefill 分流、布局转置，自身不算任何 attention，只转发到统一分发算子。
- `get_fused_swiglu_module` **刻意不走 `@dispatch`**，因为 `PartiallyFusedSwiGLUMLP` 在 forward 内部调用统一分发算子（`matmul`/`silu_and_mul`），后端选择下放到算子层——这是与 `get_swiglu_module`（分发后端专属类）对照的另一种集成风格。

## 7. 下一步学习建议

- **u8-l3（HF 推理基准与内核覆盖率）**：本讲的融合模块与注意力工厂最终都服务于「真实 LLM 推理」。下一讲讲 `modeling/transformers` 的 `tilegym-hf-bench` CLI、profiling 与 kernel coverage 报告，把本讲的模块放进真实的基准脚本里看效果。
- **回头精读依赖内核**：本讲反复提到的 `silu_and_mul`（u4-l1/u4-l2）、`matmul`（u5-l1/u5-l2）、`fmha`/`fmha_decode`（u6-l1/u6-l2）是融合模块与接口工厂背后的「真正的算力」。建议结合本讲的调用链图，回到那些讲义确认「接口层调的到底是内核的哪一条路径」。
- **扩展阅读**：若你关心 MoE，可继续阅读 `src/tilegym/ops/moe_interface.py`（`fused_moe`），它与本讲的 `attn_interface.py` 是同型的「接口层」组织，能加深对「工厂 + 适配器」套路的理解。
