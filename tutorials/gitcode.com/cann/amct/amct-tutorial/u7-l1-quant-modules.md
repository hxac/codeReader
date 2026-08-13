# QuantLinear 与量化器模块

## 1. 本讲目标

本讲是「专家层·量化模块与数据类型实现」的第一讲。前面几讲我们知道了算法怎么注册（u6-l2）、怎么挂到三类 target 上（u6-l1/u6-l4）、怎么被 `QuantGatedMLP` 装配（u5-l3）。但「算法」本身只是策略，真正在每次前向里「把浮点权重/激活变成低比特值」的，是三个运行时模块：

- `QuantLinear`——把一个普通 `nn.Linear` 包成「权重量化层」；
- `WeightQuantizer`——负责权重的伪量化与部署落盘；
- `ActivationQuantizer`——负责激活的伪量化，并在校准/量化两态间切换。

学完本讲你应当能够：

1. 读懂 `QuantLinear.forward` 在「训练/校准态」与「eval 态」两条分支的差异，并能解释 eval 态的 `cached_eval_weight` 缓存为何能避免每个样本重复伪量化；
2. 读懂 `WeightQuantizer.algo_forward` 如何把「普通算法」与「带 `quantize()` 钩子的算法（如 AutoRound）」分流，并说清 `forward`（产出伪量化浮点张量）与 `export_deploy`（产出真低比特 payload 字典）的分支差异；
3. 读懂 `ActivationQuantizer` 的 `is_observe` 双通路：校准态走 `calib_forward`、量化态走算法 `forward`，再叠一层 `fake_quant`。

---

## 2. 前置知识

本讲默认你已经掌握以下概念（前序讲义已建立）：

- **伪量化（fake quant）**：量化→反量化的往返，输出仍是浮点张量，但数值被「钉」到了低比特网格上。训练态用它保持可微，部署态才真正落成整数（u2-l1）。
- **三类 target 与挂载点**：`weight` 算法进 `WeightQuantizer`、`activation` 算法进 `ActivationQuantizer`、`structure` 算法进 `QuantGatedMLP` 的 `input_transform/hidden_transform`（u6-l2）。
- **is_observe 通路开关**：同一个布尔标志让模块在「校准态（透明，但可偷记统计量）」与「量化态（施加截断/伪量化）」之间切换，由 `set_model_to_observe` 一次性翻转（u6-l1）。
- **structure_transform 与 inv_t**：FlatQuant 类算法用一个变换 `T` 在激活侧正向作用、在权重侧逆转置（`inv_t=True`）作用，两边抵消以保持 `x @ W⊤` 不变（u6-l4）。
- **DTYPE_REGISTRY 与 quant_obj**：`WeightQuantizer/ActivationQuantizer` 各持一个 `quant_obj`（如 `QuantDequantInt`），由 `--quant_dtype` 决定，提供真正的 `fake_quant`/`export_deploy` 实现（u7-l2 展开）。

一个贯穿全讲的关键区分：**训练/校准态产出「浮点伪量化张量」用于算重建损失；部署态产出「真低比特 payload 字典」用于写 checkpoint**。三个模块的方法基本都围绕这两条产出线分叉。

> 名词速查
>
> | 术语 | 含义 |
> |---|---|
> | `quant_obj` | 数据类型对象（如 `QuantDequantInt`），提供 `forward`(伪量化) 与 `export_deploy`(真量化落盘) |
> | `quantize_algo` | 带 `quantize()` 钩子的权重算法（目前仅 AutoRound），接管最内层的量化动作 |
> | `cached_eval_weight` | eval 态缓存的「已伪量化权重」，按 `transform_key` 失效 |
> | `algo_forward` | `WeightQuantizer` 内部「依次跑权重算法」的核心循环 |

---

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [amct_pytorch/quantization/modules/quant_linear.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/modules/quant_linear.py) | 定义 `QuantLinear`：把 `nn.Linear` 包成可量化层，持有 `WeightQuantizer`，处理 eval 缓存与 `structure_transform` |
| [amct_pytorch/quantization/modules/quant_base.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/modules/quant_base.py) | 定义 `ActivationQuantizer`、`WeightQuantizer`，以及算法路由辅助 `get_algo_names_by_target`/`build_algorithms_by_target`/`_build_algorithm` |

辅证（理解调用关系用，本讲不展开）：

| 文件 | 作用 |
|---|---|
| [amct_pytorch/common/models/llm/common/quant_apply.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/quant_apply.py) | `QuantGatedMLP` 构造 `QuantLinear`/`ActivationQuantizer` 并接线；`set_model_to_observe` 翻转 `is_observe` |
| [amct_pytorch/common/models/llm/common/base.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/base.py) | `do_block_forward` 在 eval 测量路径上把 `QuantLinear.eval_mode` 置 True（缓存消费者） |
| [amct_pytorch/algorithms/quant/auto_round.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/quant/auto_round.py) | AutoRound：唯一带 `quantize()` 钩子的权重算法，用来演示钩子分支 |
| [amct_pytorch/quantization/dtypes/int.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/dtypes/int.py) | `QuantDequantInt`：`quant_obj` 的一个具体实现，展示 `forward`/`export_deploy` 长什么样 |
| [amct_pytorch/common/models/llm/common/deploy_export.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/deploy_export.py) | `export_block_deploy`：消费 `QuantLinear.export_deploy()` 返回的 payload，写成 safetensors |

---

## 4. 核心概念与源码讲解

本讲按数据流的「外→中→内」三层组织：先看最外层的 `QuantLinear`（4.1），再钻进它持有的 `WeightQuantizer`（4.2），最后看与它并列的 `ActivationQuantizer`（4.3）。

### 4.1 QuantLinear：把 nn.Linear 变成可量化层

#### 4.1.1 概念说明

`QuantLinear` 是一个「包装器（wrapper）」：它接收一个原始 `nn.Linear`，原样保留其 `weight`/`bias`，但在前向时把权重先过一遍 `WeightQuantizer`（伪量化），再做 `F.linear`。

为什么要包一层而不是直接改 `nn.Linear`？因为：

1. **权重量化是「每次前向都要重做」的运算**——伪量化的输入是浮点权重，输出是「被钉到低比特网格上」的浮点权重，原始权重本身不能被破坏（部署时还要拿原始权重做真量化落盘，算法参数还要拿它训练）。
2. **要能挂多种算法**——LWC 截断、AutoRound 舍入偏移等都要在「量化之前」作用于权重，需要一个统一的位置按 target 拉起算法。
3. **要兼顾训练态与 eval 态**——训练时每个 batch 都要伪量化（可微），eval 测量精度时权重不变，应缓存结果避免重复开销。

`QuantLinear` 还要接收一个可选的 `structure_transform`（结构变换，如 FlatQuant 的正交矩阵），在权重侧以 `inv_t=True` 作用。这是为了让「激活侧正向变换 + 权重侧逆转置」配平抵消（u6-l4 已讲原理）。

#### 4.1.2 核心流程

`QuantLinear.forward(hidden_states, structure_transform)` 的两条分支：

```text
                        ┌─ eval_mode = True (精度测量/接力前向)
                        │     1. transform_key = id(structure_transform) or None
                        │     2. 若缓存命中 (有 cached_eval_weight 且 key 相同):
                        │            weight = cached_eval_weight        # 直接复用
                        │        否则 (缓存未命中):
                        │            w = linear.weight
                        │            若有 structure_transform: w = T(w, inv_t=True)
                        │            cached_eval_weight = weight_quantizer(w).detach()
                        │            记下 transform_key
                        │            weight = cached_eval_weight
                        │
forward(hidden_states, ─┤
   structure_transform) ├─ eval_mode = False (训练/校准)
                        │     w = linear.weight
                        │     若有 structure_transform: w = T(w, inv_t=True)
                        │     weight_quantizer.observe_input(hidden_states, w)   # 给算法一个看激活的机会
                        │     weight = weight_quantizer(w)                       # 现场伪量化（不缓存）
                        │
                        ▼
              output = F.linear(hidden_states, weight, linear.bias)
```

`export_deploy(structure_transform)` 则是另一条独立路径，不经过 `forward`：

```text
export_deploy:  w = linear.weight
                若有 structure_transform: w = T(w, inv_t=True)
                payload = weight_quantizer.export_deploy(w)   # 真量化 → {qweight, scale, bias}
```

#### 4.1.3 源码精读

先看构造。`QuantLinear` 持有原始 `linear`、一个 `WeightQuantizer`，以及 eval 缓存的三件套：

[amct_pytorch/quantization/modules/quant_linear.py:27-37](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/modules/quant_linear.py#L27-L37) — 构造函数：保存原始 linear，把权重形状写回 `args.w_size`（AutoRound 等算法构造时要用），按 `w_bits` 建 `WeightQuantizer`，初始化 eval 缓存为空。

再看前向。注意 eval 分支与训练分支的差异全在前半段「weight 怎么来」，后半段 `F.linear` 完全一致：

[amct_pytorch/quantization/modules/quant_linear.py:39-66](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/modules/quant_linear.py#L39-L66) — `forward`：eval 分支用 `id(structure_transform)` 当缓存键，命中则复用 `cached_eval_weight`，否则重算并 `.detach()` 落缓存；训练分支现场跑 `observe_input` + 伪量化，不缓存。

最后是部署导出，独立于 `forward`：

[amct_pytorch/quantization/modules/quant_linear.py:68-73](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/modules/quant_linear.py#L68-L73) — `export_deploy`：把变换后的权重交给 `weight_quantizer.export_deploy`，产出真低比特 payload。

那么「eval_mode 何时被置 True」？答案在 `BaseModel.do_block_forward` 的测量路径上——当我们要用「量化块」前向（`use_quant_block=True`）且不需要 hook 录数据（`hook_name is None`）时，把所有 `QuantLinear` 切到 eval 态并清空缓存：

[amct_pytorch/common/models/llm/common/base.py:253-257](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/base.py#L253-L257) — 进入测量前：`mod.eval_mode = True; mod.cached_eval_weight = None`。

这条路径下，同一个 block 会被循环喂入多个校准样本（见 `base.py` 紧随其后的 `for sample, ids in zip(samples, ...)` 循环）。这就是缓存价值所在。

> **为什么用 `id(structure_transform)` 当缓存键？**
>
> 在一次测量循环里，`structure_transform` 是同一个 Python 对象（`QuantGatedMLP.input_transform`），其 `id()` 稳定；同一层权重在循环中不变。所以第一个样本触发缓存未命中、算一次伪量化；后续样本全部命中、零成本复用。
>
> 用 `id()`（对象身份）而非内容比较，是因为结构变换是一个 `nn.Module`（含可训练参数），内容相等性既昂贵又无意义——只要对象身份没变，作用在不变权重上的结果就一定不变。
>
> `transform_key` 还能正确处理「无变换」与「有变换」的切换：传 `None` 时键为 `None`，传对象时键为 `id(...)`，二者不等即触发重算。`.detach()` 把缓存权重踢出自动求导图（eval 本就在 `torch.no_grad()` 下，这是双重保险）。

#### 4.1.4 代码实践

**实践目标**：亲手验证 eval 态缓存的「首次计算、后续复用」行为，并对照训练态「每步重算」的差异。

**操作步骤**（示例代码，可用最小 mock 跑通，不依赖真实模型与 NPU）：

```python
# 示例代码：最小化构造一个 QuantLinear 并观察 eval 缓存
from types import SimpleNamespace
import torch
from amct_pytorch.quantization.modules.quant_linear import QuantLinear

# algos=[] 表示不挂任何权重算法，只走最内层 quant_obj 的伪量化
args = SimpleNamespace(w_bits=8, quant_dtype="int", algos=[], bit_policy={})
linear = torch.nn.Linear(16, 32, bias=False)
ql = QuantLinear(args, linear, w_bits=8, name="demo")

# —— 训练态：每次前向都现场伪量化，不缓存 ——
ql.eval_mode = False
_ = ql(torch.randn(4, 16))
print("训练态缓存:", ql.cached_eval_weight)          # 预期: None

# —— eval 态：首次未命中 → 计算 → 落缓存 ——
ql.eval_mode = True
ql.cached_eval_weight = None                          # 模拟 base.py 进入测量前的清空
_ = ql(torch.randn(4, 16))
print("eval 首次后缓存非空:", ql.cached_eval_weight is not None)   # 预期: True
print("缓存的形状:", tuple(ql.cached_eval_weight.shape))          # 预期: (32, 16)

# —— eval 态：第二次前向，应命中缓存、不重算 ——
_ = ql(torch.randn(4, 16))                            # 不同输入，但权重不变 → 命中
```

**需要观察的现象**：

1. 训练态下 `cached_eval_weight` 始终为 `None`；
2. 切到 eval 态后第一次前向，`cached_eval_weight` 变为非空、形状等于 `linear.weight` 的形状；
3. 第二次前向（即便输入不同）不会改变缓存内容——因为缓存只依赖权重与 transform，与输入无关。

**预期结果**：输出符合上述三条。具体的伪量化数值取决于 `weight_quant` 实现，属「待本地验证」，但「缓存是否为 None」「形状」这些结构性结论是确定的。

> 思考延伸：把 `structure_transform` 换成一个真实的 `nn.Module` 传入，连续两次前向用同一个对象，`_cached_transform_key` 不变、命中；若第二次换一个新对象（哪怕参数相同），`id()` 不同 → 重算。这就是 `id()` 键的失效条件。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `cached_eval_weight = self.weight_quantizer(weight).detach()` 里的 `.detach()` 去掉，在 eval 态会出什么问题？

**参考答案**：eval 测量本就在 `torch.no_grad()` 下，没有自动求导图，所以 `.detach()` 在功能上是「双保险」；去掉后通常不会立即报错。但它的语义价值是**显式声明「这是一块死的、不可微的张量」**，防止后续若有人误在非 `no_grad` 上下文里复用缓存时把巨大的权重图意外连进反向传播。保留它是防御性写法。

**练习 2**：为什么训练态（`eval_mode=False`）不缓存伪量化权重？

**参考答案**：训练态下权重算法的可学习参数（如 LWC 的截断因子、AutoRound 的舍入偏移）每个优化步都在变，伪量化结果随之变化；缓存没有任何复用价值，反而会锁住过期结果。eval 态则是在「参数已固定」的前提下重复跑很多样本测精度，权重伪量化结果恒定，才值得缓存。

---

### 4.2 WeightQuantizer：权重量化器与 quantize() 钩子

`QuantLinear` 把「权重怎么量化」整体委托给了 `WeightQuantizer`。本节打开这个「中阶层」。

#### 4.2.1 概念说明

`WeightQuantizer` 做三件事：

1. **拉起权重算法**：构造时按 `weight` target 从 `ALGO_REGISTRY` 实例化所有匹配的算法（LWC、AutoRound 等），存进 `self.algorithms`（`nn.ModuleDict`）。
2. **伪量化前向 `forward(w)`**：依次跑权重算法，再叠一层 `quant_obj` 的伪量化，产出浮点张量（给训练/eval 用）。
3. **部署落盘 `export_deploy(w)`**：依次跑权重算法，再调 `quant_obj.export_deploy`，产出真低比特 payload 字典（给 deploy 写 checkpoint 用）。

这里有一个**两类算法的分流**，是 `WeightQuantizer` 最精巧的设计：

- **普通权重算法**（如 LWC）：在 `algo_forward` 里直接对权重作用（`x = algo(x)`，比如做可学习截断），返回 `(x, None)`。最终的伪量化交给外层 `fake_quant` → `quant_obj(x)`。
- **带 `quantize()` 钩子的算法**（目前仅 AutoRound）：它**不**在 `algo_forward` 里改变权重（其 `forward(w)` 直接原样返回），而是被「点名」记下来作为 `quantize_algo` 返回。最内层的量化动作由它的 `quantize(x, quant_obj)` 钩子接管——因为 AutoRound 要把「可学习舍入偏移 `v`」喂给量化函数，普通 `quant_obj(x)` 的签名接不住。

约束：带钩子的算法**至多一个**，否则在 `algo_forward` 里抛 `ValueError`（两个钩子互相抢最内层量化权，无法合并）。

#### 4.2.2 核心流程

`algo_forward(x)` 是核心循环，决定「谁动权重、谁只是被记名」：

```text
algo_forward(x):
    quantize_algo = None
    for algo in self.algorithms.values():
        if is_observe:                      # 校准态：全部透明
            x = algo.calib_forward(x); continue
        if algo 有 quantize 钩子:
            if quantize_algo 已经有人:        raise ValueError("只允许一个钩子算法")
            quantize_algo = algo; continue   # 记名，不动 x
        else:                                # 普通算法：直接作用
            x = algo(x)
    return x, quantize_algo
```

两个出口方法共用 `algo_forward`，但收尾不同：

```text
forward(x):                        # 产出：伪量化浮点张量
    x, qa = algo_forward(x)
    if qa is not None:  return qa.quantize(x, quant_obj)     # 钩子伪量化（带 v）
    else:               return fake_quant(x)                 # = is_observe ? x : quant_obj(x)

export_deploy(x):                  # 产出：真低比特 payload 字典
    x, qa = algo_forward(x)
    if qa is not None:  return qa.export_deploy(x, quant_obj)   # 钩子真量化
    else:               return quant_obj.export_deploy(x)       # 普通真量化
```

可见 `forward` 与 `export_deploy` 的**差别只在最内层**：前者调 `quant_obj(x)`/`qa.quantize(...)` 得到「浮点伪量化」结果，后者调 `quant_obj.export_deploy(x)`/`qa.export_deploy(...)` 得到「真整数 payload」。

#### 4.2.3 源码精读

构造：建算法字典 + 建 `quant_obj`（注意权重路径传 `self.bits` 作为 `ctor_args`）：

[amct_pytorch/quantization/modules/quant_base.py:122-130](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/modules/quant_base.py#L122-L130) — `WeightQuantizer.__init__`：`_init_algo` 按 weight target 建算法，`quant_obj` 由 `DTYPE_REGISTRY.get(args.quant_dtype)(bits=self.bits)` 创建（无 `is_act`，默认权重侧）。

核心循环 `algo_forward`——注意两个 `continue` 分别对应「校准态透明」与「钩子算法记名」：

[amct_pytorch/quantization/modules/quant_base.py:132-147](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/modules/quant_base.py#L132-L147) — `algo_forward`：用 `getattr(algo, "quantize", None)` 探测钩子，至多记一个 `quantize_algo`。

`export_deploy`——钩子优先，且钩子算法必须自己实现 `export_deploy`，否则 `NotImplementedError`：

[amct_pytorch/quantization/modules/quant_base.py:149-163](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/modules/quant_base.py#L149-L163) — `export_deploy`：钩子分支调 `quantize_algo.export_deploy(x, self.quant_obj)`，普通分支调 `self.quant_obj.export_deploy(x)`。

`forward` 与 `fake_quant`：

[amct_pytorch/quantization/modules/quant_base.py:165-174](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/modules/quant_base.py#L165-L174) — `fake_quant`（observe 透明 / 否则 `quant_obj(x)`）与 `forward`（钩子优先 `quantize_algo.quantize(x, quant_obj)`）。

`observe_input`——给权重算法一个「偷看激活」的口子（若算法实现了该方法）：

[amct_pytorch/quantization/modules/quant_base.py:176-180](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/modules/quant_base.py#L176-L180) — `observe_input(x, weight)`：遍历算法，调用其 `observe_input`（若有）。当前内置权重算法均未实现，这是为状态化算法预留的扩展点。

> **算法是怎么被拉起来的？** `algo_forward` 之前还有一段构造逻辑，与本讲强相关，简要带过：`_init_algo` → `build_algorithms_by_target(args, "weight", self.bits)`。它先用 `get_algo_names_by_target` 按 `targets` 元数据从 `args.algos` 里筛出 weight 类算法名，再用 `_build_algorithm` 实例化——后者用 `inspect.signature` 探测构造签名，自适应地决定是否把 `self.bits` 作为额外参数传入（LWC 需要 `w_bits`、AutoRound 需要 `w_bits`）。完整路由机制见 u6-l2。源码在 [amct_pytorch/quantization/modules/quant_base.py:28-80](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/modules/quant_base.py#L28-L80)。

现在用一个真实算法对照「钩子分支」。AutoRound 注册声明 `targets=("weight",)`，并提供 `quantize`/`export_deploy` 两个钩子，而其 `forward` 是 no-op：

[amct_pytorch/algorithms/quant/auto_round.py:67-71](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/quant/auto_round.py#L67-L71) — `@ALGO_REGISTRY.register(name="autoround", targets=("weight",))`。

[amct_pytorch/algorithms/quant/auto_round.py:121-130](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/quant/auto_round.py#L121-L130) — `export_deploy` 调 `quant_obj.export_deploy(clipped_weight, v=v)`；`quantize` 调 `quant_obj(clipped_weight, v=v)`；`forward(weight)` 原样返回（所以 `algo_forward` 里它只「记名」不动权重）。

注意 `quantize` 与 `export_deploy` 都多传一个 `v`（可学习舍入偏移），这正是它需要钩子接管的原因——普通 `quant_obj(x)` 的调用点没有 `v` 这个位置。

对照看 `quant_obj` 的两条出口（以 int 为例）：

[amct_pytorch/quantization/dtypes/int.py:45-56](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/dtypes/int.py#L45-L56) — `forward(x, v)` 走 `fake_quant`（`weight_quant(x, bits, v=v)`，浮点往返）；`export_deploy(x, v)` 用 `real_quant=True` 走 `weight_quant`，返回 `{"qweight", "weight_scale", "weight_bias"}` 字典。

最后看这个 payload 字典在 deploy 阶段怎么变成 safetensors 的键名——`export_block_deploy` 把 `qweight` 映射为 `.weight`，其余 extra 项用 `replace(".weight", f".{extra_name}")` 派生键名：

[amct_pytorch/common/models/llm/common/deploy_export.py:143-152](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/deploy_export.py#L143-L152) — 消费 `module.export_deploy()` 的返回：`payload["qweight"]` → `.weight`，`weight_scale` → `.weight_scale`，`weight_bias` → `.weight_bias`。

> **整数权重量化的数学**（帮助理解 `weight_quant` 在做什么）：
>
> 对一个权重通道，按对称量化有
>
> \[ s = \frac{\max(|w|)}{q_{\max}}, \qquad q = \mathrm{clip}\!\left(\mathrm{round}\!\left(\frac{w}{s}\right) + z,\ -2^{b-1},\ 2^{b-1}-1\right) \]
>
> 伪量化（`forward`）再做反量化 \(\hat{w} = s\cdot(q-z)\) 得到「被钉到网格上的浮点 w」；`export_deploy` 则直接把 \(q\)、\(s\)（即 `weight_scale`）、\(z\) 偏移（`weight_bias`）作为真低比特数据写盘。AutoRound 的 `v` 是对 `round` 这一步的软偏移，把「四舍五入」改成「带学习偏移的舍入」以降低误差。

#### 4.2.4 代码实践

**实践目标**：亲手对比 `WeightQuantizer.forward`（产出浮点张量）与 `export_deploy`（产出 payload 字典），直观看到「同一权重、两种产出」。

**操作步骤**（示例代码，承接 4.1.4 的 mock）：

```python
# 示例代码：观察 WeightQuantizer 的两条出口
from types import SimpleNamespace
import torch
from amct_pytorch.quantization.modules.quant_linear import QuantLinear

args = SimpleNamespace(w_bits=8, quant_dtype="int", algos=[], bit_policy={})
ql = QuantLinear(args, torch.nn.Linear(16, 32, bias=False), w_bits=8, name="demo")
wq = ql.weight_quantizer
w = torch.randn(32, 16)

# —— forward：产出「伪量化浮点张量」——
fq = wq(w)
print("forward 返回类型:", type(fq).__name__, "| dtype:", fq.dtype)   # 预期: Tensor / float32

# —— export_deploy：产出「真低比特 payload 字典」——
payload = wq.export_deploy(w)
print("export_deploy 返回类型:", type(payload).__name__)
print("payload 键:", sorted(payload.keys()))                          # 预期含 qweight/weight_scale/weight_bias
print("qweight 形状:", tuple(payload["qweight"].shape))               # 预期: (32, 16)
print("qweight dtype:", payload["qweight"].dtype)                     # 预期: 某种低比特整型（待本地验证具体类型）
```

**需要观察的现象与预期结果**：

1. `forward` 返回一个与 `w` 同形状的 `torch.Tensor`，`dtype` 仍是浮点（伪量化只改数值不改类型）——这条路径的产物会进入 `F.linear`，是可微的；
2. `export_deploy` 返回一个 `dict`，键含 `qweight`/`weight_scale`/`weight_bias`；`qweight` 与 `w` 同形状，但 `dtype` 是低比特整数（具体整型类型「待本地验证」，取决于 `weight_quant` 的 `real_quant=True` 实现）。
3. 这正好解释了 deploy 阶段为什么把 `qweight` 映射成 `.weight`、把 `weight_scale` 映射成 `.weight_scale`：payload 字典的键名就是落盘键名的来源。

> **钩子分支的差异（阅读型实践）**：上例用 `algos=[]` 走的是「普通分支」。要观察「钩子分支」，把 `args.algos` 改成 `["autoround"]` 并重建（注意 AutoRound 构造会读 `args.w_size`，需经 `QuantLinear` 构造时写入）。此时 `WeightQuantizer.forward(w)` 会走 `quantize_algo.quantize(w, quant_obj)` → `quant_obj(clipped_w, v=v)`（伪量化、带舍入偏移），而 `export_deploy(w)` 会走 `quantize_algo.export_deploy(w, quant_obj)` → `quant_obj.export_deploy(clipped_w, v=v)`（真量化 payload）。两者都经过 AutoRound 的 `prepare_deploy_weight`（裁剪 + 叠加 `v`），但最内层一个调 `quant_obj`、一个调 `quant_obj.export_deploy`——这就是「带 quantize_algo 时 forward 与 export_deploy 的分支差异」。

#### 4.2.5 小练习与答案

**练习 1**：假设同时挂了两个带 `quantize()` 钩子的权重算法，会发生什么？在第几行报错？

**参考答案**：会抛 `ValueError("Only one weight algorithm with a custom quantize() hook is supported.")`。报错点在 [amct_pytorch/quantization/modules/quant_base.py:140-143](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/modules/quant_base.py#L140-L143) 的 `algo_forward` 内——第二个钩子算法被发现时 `quantize_algo is not None`，立即拒绝。原因是两个钩子都要接管最内层量化，无法合并。

**练习 2**：`WeightQuantizer.observe_input` 由谁调用？目前有算法用它吗？

**参考答案**：由 `QuantLinear.forward`（训练态分支）在每次前向调用，签名是 `observe_input(hidden_states, weight)`，见 [quant_linear.py:62](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/modules/quant_linear.py#L62)。它把「激活 + 权重」一并喂给每个实现了 `observe_input` 的权重算法。当前内置的 LWC/AutoRound 等均未实现该方法，所以是空跑；这是为「需要看激活统计的权重量化算法」预留的扩展点。

---

### 4.3 ActivationQuantizer：observe/quantize 双通路

`QuantLinear` 量化的是权重；激活的量化由 `ActivationQuantizer` 负责，它被 `QuantGatedMLP` 挂在 `input_quant`/`hidden_quant` 两个位置（u5-l3）。

#### 4.3.1 概念说明

激活比权重难量化，根本原因是**激活的取值范围每次前向都在变**（取决于输入数据），不能像权重那样离线算一次 scale。所以激活量化器需要两种工作模式：

- **校准态（is_observe=True）**：让激活「原样穿过」，但允许激活算法在穿过时偷偷记录统计量（如 LAC 更新 `maxval/minval`）。这一态用来在校准数据上攒够统计。
- **量化态（is_observe=False）**：先用激活算法对激活施加变换（如 LAC 用攒到的统计 + 可学习因子做截断），再叠一层 `quant_obj` 的动态伪量化（如 per-token 量化）。这一态用于 PTQ 训练与精度测量。

这就是 u6-l1 讲的「is_observe 双通路」在模块层面的具体落点。与权重侧相比，激活侧**没有** `quantize()` 钩子这套机制（激活算法都是「先变换、再统一伪量化」的普通算法），因此 `ActivationQuantizer` 比 `WeightQuantizer` 简单——没有 `algo_forward` 的记名分流。

#### 4.3.2 核心流程

```text
forward(x):
    for algo in algorithms.values():
        x = algo.calib_forward(x) if is_observe else algo(x)   # 双通路分叉点
    return fake_quant(x)

fake_quant(x):
    return x if is_observe else quant_obj(x)                   # observe 透明，否则动态伪量化
```

注意两层 `is_observe` 判断叠加：

| is_observe | 算法层 (`algo` 分支) | quant_obj 层 (`fake_quant`) | 净效果 |
|---|---|---|---|
| True（校准） | `calib_forward`（透明，可记统计） | 透传 `x` | 激活原样穿过，算法可攒统计 |
| False（量化） | `algo(x)`（施加截断/变换） | `quant_obj(x)`（动态伪量化） | 激活被算法处理后再伪量化 |

#### 4.3.3 源码精读

构造——注意 `quant_obj` 用 `is_act=True`，这会让 int 走 `dynamic_per_token_quant`（动态、逐 token）而非权重的 `weight_quant`（静态）：

[amct_pytorch/quantization/modules/quant_base.py:83-93](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/modules/quant_base.py#L83-L93) — `ActivationQuantizer.__init__`：建 activation 算法、`quant_obj = DTYPE_REGISTRY.get(args.quant_dtype)(bits, is_act=True)`、`is_observe=False`。

双通路本体——一行三元表达式分叉，再叠 `fake_quant`：

[amct_pytorch/quantization/modules/quant_base.py:98-106](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/modules/quant_base.py#L98-L106) — `fake_quant`（observe 透传 / 否则 `quant_obj(x)`）与 `forward`（每个算法 `calib_forward if is_observe else algo(x)`，最后 `fake_quant`）。

对照 `quant_obj`（int、is_act=True）的动态量化：

[amct_pytorch/quantization/dtypes/int.py:38-48](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/dtypes/int.py#L38-L48) — `fake_quant`：`is_act` 为真时走 `dynamic_per_token_quant(x, bits)`（逐 token 算 scale，每次前向重算）；`forward` 仍在 observe 或 16-bit 时透传。

`is_observe` 由谁翻转？由 `set_model_to_observe` 一次性遍历 `model.modules()`，把所有带 `is_observe` 属性的模块（算法、`ActivationQuantizer`、`WeightQuantizer` 各一份）齐整翻转：

[amct_pytorch/common/models/llm/common/quant_apply.py:47-50](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/quant_apply.py#L47-L50) — `set_model_to_observe(model, flag)`：遍历所有模块，`hasattr(mod, "is_observe")` 即翻转。

这就是 u6-l1 强调的「一标志多处持有，必须批量翻转」的具体实现——若只翻 `ActivationQuantizer.is_observe` 而忘了翻内部算法的 `is_observe`，通路就会不一致。

> **per-token 量化的直觉**：对激活 `x` 形状 `[batch, seq, hidden]`，沿 `hidden` 维每个 token 各自取 `max(|x|)` 算自己的 scale。这样每个 token 的有效量化范围都铺满，避免被个别 outlier 拖累整体精度。代价是 scale 必须每次前向重算（动态）。详见 u2-l1 的量化粒度一节。

#### 4.3.4 代码实践

**实践目标**：亲手验证 `ActivationQuantizer` 在 `is_observe` 翻转前后的「透明 vs 伪量化」差异。

**操作步骤**（示例代码）：

```python
# 示例代码：观察 ActivationQuantizer 双通路
from types import SimpleNamespace
import torch
from amct_pytorch.quantization.modules.quant_base import ActivationQuantizer

args = SimpleNamespace(quant_dtype="int", algos=[])
aq = ActivationQuantizer(args, bits=8)        # algos=[] 只走 quant_obj
x = torch.randn(2, 5, 16)

# —— 校准态：应原样透传 ——
aq.is_observe = True
print("observe 输出 == 输入:", torch.equal(aq(x), x))   # 预期: True

# —— 量化态：应被动态伪量化（值变、形状不变、仍浮点）——
aq.is_observe = False
y = aq(x)
print("quant 输出形状 == 输入形状:", tuple(y.shape) == tuple(x.shape))  # 预期: True
print("quant 输出 dtype 仍是浮点:", torch.is_floating_point(y))         # 预期: True
print("quant 输出与输入不同:", not torch.equal(y, x))                   # 预期: True（数值被钉到网格）
```

**需要观察的现象与预期结果**：

1. `is_observe=True` 时 `aq(x)` 与 `x` 完全相等（透传，因为 `fake_quant` 走 observe 分支返回 `x`，算法循环虽走 `calib_forward` 但无算法时也是透传）；
2. `is_observe=False` 时输出形状不变、仍是浮点 dtype，但数值与输入不同（被 `dynamic_per_token_quant` 钉到低比特网格）。

具体的量化误差幅度属「待本地验证」，但「是否透传」「是否改 dtype」这些结构性结论是确定的。

> **进阶（阅读型实践）**：挂一个真实的 activation 算法（如 LAC）后，对照 [amct_pytorch/algorithms/quant/auto_clip.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/quant/auto_clip.py) 看：`is_observe=True` 时 `calib_forward` 更新 `maxval/minval` buffer；`is_observe=False` 时 `forward` 用这些统计 + 可学习 `clip_factor` 做截断。把 `set_model_to_observe(aq, True/False)` 的调用插在上例前后，可观察到 buffer 是否被更新。

#### 4.3.5 小练习与答案

**练习 1**：`ActivationQuantizer` 为什么没有像 `WeightQuantizer` 那样的 `quantize()` 钩子机制？

**参考答案**：因为激活算法都是「先对激活做一次变换（截断/平滑），再统一交给 `quant_obj` 动态伪量化」的普通算法，量化函数的签名不需要额外参数（不像 AutoRound 要传舍入偏移 `v`）。所以 `ActivationQuantizer.forward` 用统一的 `algo.calib_forward if is_observe else algo(x)` 就能覆盖所有激活算法，不需要「记名 + 钩子接管」的分流。

**练习 2**：如果只翻转 `aq.is_observe` 而不调 `set_model_to_observe`，挂了 LAC 算法时会出什么问题？

**参考答案**：`aq.is_observe` 翻转了，但内部 `aq.algorithms["lac"].is_observe` 没翻——两者不一致。于是 `aq.forward` 里的三元表达式 `algo.calib_forward(x) if self.is_observe else algo(x)` 会按外层 `aq.is_observe` 选分支，但 `algo` 自身（如 LAC）内部可能还按自己的 `is_observe` 行事，导致「外层要走量化、内层还在校准」的通路错乱。`set_model_to_observe` 的全模块遍历正是为避免这种半翻状态而存在。

---

## 5. 综合实践

**任务**：把本讲三个模块串成一条完整的「一次权重伪量化」追踪，并标注每一步落在哪个模块的哪个方法。

背景：`QuantGatedMLP.forward` 在 `down_proj` 这一步会调用 `self.down_proj(hidden_q, structure_transform=self.hidden_transform)`，其中 `self.down_proj` 是一个 `QuantLinear`。请追踪这一次调用内部的完整数据流：

1. **QuantLinear.forward**（[quant_linear.py:39-66](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/modules/quant_linear.py#L39-L66)）：若是训练态，先对 `linear.weight` 施加 `structure_transform(weight, inv_t=True, name=...)`（权重侧逆转置），再调 `weight_quantizer.observe_input(hidden_states, weight)`，最后 `weight = weight_quantizer(weight)`。请说明 `hidden_states` 这一参数在 `down_proj` 场景下其实是 `hidden_q`（已伪量化的中间激活），它会不会被权重路径用到（提示：只进 `observe_input`，不进 `F.linear` 之外的权重运算）。

2. **WeightQuantizer.forward**（[quant_base.py:170-174](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/modules/quant_base.py#L170-L174)）：进入 `algo_forward`。假设 `--algos lwc`，说明 LWC（普通权重算法）走 `x = algo(x)` 做可学习截断、`quantize_algo=None`；随后 `fake_quant(x)` → `quant_obj(x)` 完成 int 伪量化。

3. **回到 QuantLinear**：`output = F.linear(hidden_states, weight, bias)`，用伪量化后的 `weight` 与输入做线性变换。

**交付物**：画一张数据流图（文字版即可），标清：
- `linear.weight` 在哪一步被 `structure_transform` 作用（哪一侧？`inv_t` 是 True 还是 False？）；
- `observe_input` 把哪些张量交给了权重算法；
- 「普通算法分支」与「钩子算法分支」在 `algo_forward` 里的分叉点；
- 最内层 `quant_obj(x)` 与 `F.linear` 的先后关系。

**延伸思考**：若把 `--algos` 换成 `autoround`，步骤 2 的分叉点会走哪一支？`WeightQuantizer.forward` 最后调的是 `quantize_algo.quantize(x, quant_obj)` 还是 `fake_quant(x)`？（答案：前者，因为 AutoRound 是钩子算法，`algo_forward` 把它记名为 `quantize_algo`，`forward` 检测到非 None 即走钩子分支。）

---

## 6. 本讲小结

- `QuantLinear` 是 `nn.Linear` 的量化包装器：训练态每步现场伪量化权重（不缓存），eval 态用 `id(structure_transform)` 当键缓存 `cached_eval_weight`，避免同一层在多样本循环里重复伪量化；`export_deploy` 是独立于 `forward` 的第三条路径，产出真低比特 payload。
- `WeightQuantizer` 用 `algo_forward` 把权重算法分两类：普通算法（如 LWC）直接 `algo(x)` 作用；带 `quantize()` 钩子的算法（如 AutoRound）只「记名」不动权重，至多一个。`forward` 产出浮点伪量化张量，`export_deploy` 产出 `{qweight, weight_scale, weight_bias}` payload 字典——差别全在最内层调 `quant_obj(x)` 还是 `quant_obj.export_deploy(x)`。
- `ActivationQuantizer` 比 `WeightQuantizer` 简单（无钩子机制），靠两层 `is_observe` 判断实现双通路：校准态算法走 `calib_forward`、`fake_quant` 透传；量化态算法走 `algo(x)`、`fake_quant` 走 `quant_obj` 动态伪量化（per-token）。
- `is_observe` 是「一标志多处持有」（算法、激活量化器、权重量化器各一份），必须由 `set_model_to_observe` 全模块遍历齐整翻转，否则通路半翻致错。
- `quant_obj`（由 `--quant_dtype` 决定，如 `QuantDequantInt`）才是真正干量化活的：`forward`/`fake_quant` 出浮点伪量化、`export_deploy` 出真整数 payload；数据类型实现细节是下一讲（u7-l2）的主题。

---

## 7. 下一步学习建议

- **紧接着读 u7-l2（量化数据类型与 export_deploy 落盘）**：本讲把 `quant_obj` 当黑盒，只用了它的 `forward`/`export_deploy` 接口。下一讲会打开 `QuantDequantInt`/`QuantDequantMx`/`QuantDequantHifp`，讲清 `weight_quant`/`dynamic_per_token_quant` 这些 impl 函数的 per-token/per-group 粒度，以及 payload 字典的命名如何被 `export_block_deploy` 写成 `.weight`/`.weight_scale` 等 safetensors 键。
- **回看 u6-l4 的 FlatQuant**：理解本讲反复出现的 `structure_transform(weight, inv_t=True)` 为何要在权重侧逆转置，建议结合 FlatQuant 的 Kronecker 分解矩阵一并读，能把「激活正向 / 权重逆向」这条配平链彻底打通。
- **回看 u4-l4（部署导出 deploy）**：本讲只讲到 `QuantLinear.export_deploy` 产出 payload；这些 payload 如何被 `LlmDeployWorkflow._run_blockwise` 逐层收集、重写 weight index、生成 `quantization_config`，在 u4-l4 有完整链路。
- **动手验证**：把综合实践的数据流图自己画一遍，再挑一个真实适配器（如 [qwen3/quant_module.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/qwen/qwen3/quant_module.py)）看 `QuantLinear` 是怎么按 `w_bits` 逐投影构造的，巩固「BitPolicy 选位宽 → QuantLinear 接收 → WeightQuantizer 执行」这条链。
