# 量化算子挂载 quant_apply

## 1. 本讲目标

本讲解决一个具体问题：**AMCT 是怎样把一个普通的浮点 decoder layer「改造成」可量化的层，同时还能让同一套代码兼顾 dense MLP、MoE 专家、共享专家、注意力投影等多种结构？**

读完本讲你应当能够：

- 说清 `apply_quant_to_attn` / `apply_quant_to_moe_mlp` 如何递归遍历 decoder layer 并**原地替换**子模块；
- 解释 `QuantGatedMLP` 如何用一个 `group` 字符串在 dense MLP、`moe.routed`、`moe.shared` 三种位置间复用，并借助 `BitPolicy` 为 `gate_proj`/`up_proj`/`down_proj` 分别选定位宽；
- 理解 `PlainLinear` 为什么必须存在——它只为让一个不量化的 `nn.Linear` 能和 `QuantLinear` 站在**同一个调用点**上；
- 画出 `QuantGatedMLP.forward` 里三个投影的完整量化数据流，并解释 `structure_transform` 为何要同时作用于激活（正向）与权重（逆向）。

## 2. 前置知识

在进入源码前，先确认四个基础概念。

**门控 MLP（gated MLP）。** 现代 LLM 的 FFN 普遍是「门控」结构，而不是单层全连接。它由三个 `Linear` 组成：

\[ \text{hidden} = \text{act\_fn}(\text{gate\_proj}(x)) \odot \text{up\_proj}(x),\quad \text{out} = \text{down\_proj}(\text{hidden}) \]

其中 \(\odot\) 是逐元素乘，`act_fn` 通常是 SiLU。`gate_proj` 决定「放行多少」，`up_proj` 提供「放行的内容」，`down_proj` 把中间维投影回隐藏维。本讲的主角 `QuantGatedMLP` 就是对这三投影做量化包装。

**`nn.Module` 的原地子模块替换。** PyTorch 里 `model.mlp = NewModule(...)` 这种 `setattr` 写法会直接把子模块换掉，且新模块的参数会正确注册到父模块的 `parameters()` 中。AMCT 的挂载全程依赖这一机制——**不重建整层，只换需要量化的子树**。

**`BitPolicy` 下标代理（回顾 [u3-l4](u3-l4-bit-policy-config.md)）。** `bit_policy[group]` 返回一个 `_GroupBits` 代理，再对它 `["gate_proj"]` 就得到一个 `LayerBits(w, a)` 命名元组：

```python
bits = quant_args.bit_policy["mlp"]      # -> _GroupBits(group="mlp")
gate = bits["gate_proj"]                 # -> LayerBits(w=8, a=8)
gate.w, gate.a                           # 权重位宽 / 激活位宽
```

底层调用 `linear_bits(name="gate_proj", group="mlp")`，沿 `mlp → gate_proj` 路径**从叶子到根逐级回退**，找到第一个 `w_bits`/`a_bits` 成对的节点。本讲会反复用到这条查询路径。

**`build_quant_block` 入口（回顾 [u5-l1](u5-l1-base-model-pipeline.md)）。** PTQ 主流程在切分 `PtqUnit` 之前，会调用模型适配器的 `build_quant_block(layer_idx)`，它返回一个「已经挂上量化算子」的 decoder layer。本讲讲的就是这个方法**内部**调用的两个挂载函数。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [amct_pytorch/common/models/llm/common/quant_apply.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/quant_apply.py) | 本讲核心。定义 `apply_quant_to_attn`/`apply_quant_to_moe_mlp` 挂载函数、`QuantGatedMLP` 量化包装类、`PlainLinear` 签名兼容包装、以及 `set_model_to_observe` 通路切换。 |
| [amct_pytorch/quantization/modules/quant_linear.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/modules/quant_linear.py) | `QuantLinear`：单投影权重量化模块，接收 `structure_transform` kwarg 对权重做逆变换。是 `QuantGatedMLP` 内部三个投影的实际类型。 |
| [amct_pytorch/quantization/modules/quant_base.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/modules/quant_base.py) | `WeightQuantizer`/`ActivationQuantizer`：真正的伪量化执行者；`build_algorithms_by_target` 把算法按 target 装进量化器。本讲把它当作「被挂载的零件」介绍。 |
| [amct_pytorch/common/models/llm/qwen/qwen3/qwen3.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/qwen/qwen3/qwen3.py) | Qwen3 适配器。其 `build_quant_block` 是最薄调用样例，展示「适配器只管把 `cls` 喂给挂载函数」。 |
| [amct_pytorch/common/models/llm/qwen/qwen3/quant_module.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/qwen/qwen3/quant_module.py) | `QuantQwen3MLP`（直接继承 `QuantGatedMLP`）与 `QuantQwen3Attn`（注意力量化包装，演示 `PlainLinear` 的真实使用场景）。 |

## 4. 核心概念与源码讲解

本讲按「挂载动作 → MLP 包装 → 签名兼容」三个最小模块展开，最后用一节串起 `structure_transform` 的双边协调。

### 4.1 挂载的本质：原地替换子模块

#### 4.1.1 概念说明

「挂载量化算子」听起来复杂，本质只有一句话：**遍历 decoder layer 的子模块树，把其中的注意力模块和 MLP 模块替换成对应的「量化包装类」**。原始权重原封不动搬进新模块，新模块额外内置量化器与算法，前向时一边算一边做伪量化。

这里有一个关键设计：挂载函数本身**不认识**具体的模型结构。它只认两个通用名字——注意力叫 `self_attn`（或 `linear_attn`）、FFN 叫 `mlp`/`experts`/`shared_experts`。真正「懂模型」的是适配器通过 `cls` 参数传进来的量化包装类。这样挂载逻辑可以跨 Qwen / DeepSeek / GLM / HyV3 复用。

#### 4.1.2 核心流程

注意力挂载的流程极简，是一个递归的「找名字 → 替换」：

```
apply_quant_to_attn(args, model, cls):
    对 model 的每个直接子模块 (name, mod):
        若 name in {"self_attn", "linear_attn"}:
            把 model.name 替换为 cls(args, mod)
        若 mod 还有子模块:
            递归 apply_quant_to_attn(args, mod, cls)
```

MLP 挂载稍复杂，需要区分 dense MLP 与 MoE，下一节单独讲。

#### 4.1.3 源码精读

注意力挂载函数只有 7 行，逻辑全部在「按名字匹配 + 递归」上：

[amct_pytorch/common/models/llm/common/quant_apply.py:117-123](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/quant_apply.py#L117-L123) —— 遍历 `named_children()`，命中 `self_attn`/`linear_attn` 就用 `cls(args, mod)` 原地替换，并对有子模块的节点继续递归（从而能穿透 `block.self_attn` 这类嵌套）。

注意 `cls(args, mod)` **没有传 `group`**——注意力包装类自己读 `args.quant_target` 来决定行为（见 4.4）。这与 MLP 路径的 `cls(args, mod, group=...)` 形成对照。

调用方在哪？看 Qwen3 适配器的 `build_quant_block`：

[amct_pytorch/common/models/llm/qwen/qwen3/qwen3.py:96-102](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/qwen/qwen3/qwen3.py#L96-L102) —— 根据 `self.quant_target` 决定调哪个挂载函数，传入对应的 `cls`（`QuantQwen3Attn` 或 `QuantQwen3MLP`）。适配器只做「选函数 + 选类」两件事，挂载细节全在 `quant_apply.py` 里。

> 一个容易忽略的点：`build_quant_block` 返回的是**被原地改写过的同一个 `decoder_layer` 对象**（`apply_quant_*` 内部用 `setattr` 改子树，并 `return model`），而不是新建一层。所以 PTQ 主流程拿到的就是浮点层「换心」后的版本。

#### 4.1.4 代码实践

**实践目标：** 验证 `apply_quant_to_attn` 只替换注意力、不碰兄弟 MLP。

**操作步骤：**

1. 打开 [tests/unit_test/common/models/llm/common/test_quant_apply.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/tests/unit_test/common/models/llm/common/test_quant_apply.py) 中的 `test_apply_quant_to_attn_replaces_self_attn_child`。
2. 阅读它如何用一个 `_FakeQuantWrapper`（记录被包装原始模块的假类）和一个手搭的 `layer.self_attn` / `layer.mlp` 来断言替换行为。

**需要观察的现象：** 调用后 `layer.self_attn` 变成 `_FakeQuantWrapper`，而 `layer.mlp` 仍是原始 `nn.Linear` 未被动过。

**预期结果：** 两个断言同时成立——注意力被替换、MLP 不受影响。这正是「按名字精准替换」的证据。

> 是否本地运行：该测试是仓库现有单测，可在 `tests/unit_test/common/models/llm/common/` 下用 pytest 直接跑；若仅做源码阅读，对照上面的断言语句即可确认行为。

#### 4.1.5 小练习与答案

**练习 1：** `apply_quant_to_attn` 为什么要对 `mod` 递归，而不是只看一层？

> **参考答案：** 因为有些模型把 decoder layer 再包一层（如 `outer.block.self_attn`）。只看一层会漏掉嵌套结构，递归保证无论注意力藏在多深都能命中。测试 `test_apply_quant_to_attn_recurses_into_grandchildren` 正是验证这一点。

**练习 2：** 如果一个新模型的注意力模块叫 `attn` 而不是 `self_attn`，直接调 `apply_quant_to_attn` 会怎样？

> **参考答案：** 不会被替换——因为 `"attn"` 不在 `{"self_attn", "linear_attn"}` 里，挂载函数会跳过它。该模型的适配器需要自带的注意力包装类直接做 `setattr`，或反馈给框架扩展这个名字集合。

---

### 4.2 apply_quant_to_moe_mlp：按位置分 group

#### 4.2.1 概念说明

FFN 比注意力复杂，因为它有三种形态：

1. **dense MLP**：一层里只有一个 `mlp`，没有专家；
2. **MoE 路由专家（routed experts）**：`mlp.experts` 是一个 `ModuleList`，每个 expert 自己是一个小 MLP；
3. **MoE 共享专家（shared experts）**：部分模型还有 `shared_experts`，所有 token 都会经过它。

这三种位置往往需要**不同的量化策略**——例如路由专家数量多、单个不关键，常用更激进的 INT4；共享专家每次必经，可能保留更高位宽。AMCT 用一个字符串 `group` 来标记位置，再让 `BitPolicy` 按 group 查 yaml 里的位宽。

`apply_quant_to_moe_mlp` 的职责就是：**识别这三种位置，给每个位置打上对应的 group 标签，然后用同一个包装类 `cls` 包装它们。**

#### 4.2.2 核心流程

```
apply_quant_to_moe_mlp(args, model, cls):
    对 model 的每个直接子模块 (name, mod):
        若 name == "mlp" 且 mod 没有 experts 属性:        # dense MLP
            替换为 cls(args, mod, group="mlp")
        若 name == "experts":                             # 路由专家列表
            对每个非空 expert:
                experts[i] = cls(args, expert, group="moe.routed")
        若 name == "shared_experts":                      # 共享专家
            用「清空 algos 的 args」替换为 cls(shared_args, mod, group="moe.shared")
        若 mod 还有子模块:
            递归
```

三个分支用 `group` 区分位置：`"mlp"` / `"moe.routed"` / `"moe.shared"`。注意 group 名用的是**点分路径**，正好对应 yaml 里 `moe` → `routed`/`shared` 的嵌套层级，`BitPolicy.linear_bits` 能直接沿这条路径回退查询。

#### 4.2.3 源码精读

[amct_pytorch/common/models/llm/common/quant_apply.py:83-114](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/quant_apply.py#L83-L114) —— 三个分支与递归，注意几个细节：

- 第 93 行用 `hasattr(mod, "experts") is False` 判断 dense MLP。这是关键判别：一个 `mlp` 子模块若带 `experts`，说明它是 MoE 容器（要进 `experts` 分支处理里面的专家），**不**整体替换；只有「裸 mlp」才替换。测试 `test_apply_quant_to_moe_mlp_does_not_wrap_when_mlp_has_experts` 验证了这点。
- 第 99-100 行跳过 `cur_mod is None` 的专家槽位，对应稀疏 MoE 里被置空的专家（测试 `test_apply_quant_to_moe_mlp_skips_none_experts`）。
- 第 101 行用 `mod[idx] = ...` 直接改 `ModuleList` 元素（这是替换列表元素的标准写法）。

shared experts 分支特殊，它用了一份「清空 algos」的 args：

[amct_pytorch/common/models/llm/common/quant_apply.py:53-62](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/quant_apply.py#L53-L62) —— `build_no_algo_args` 深拷贝 args 并把 `algos` 置空。共享专家虽复用同一个 `QuantGatedMLP` 包装类，但**跳过 PTQ 训练算法**，只按 `bit_policy` 的 `moe.shared` 位宽做直接量化。这是一个「同一外壳、不同内核」的复用技巧。

包装时还要兼容两类构造签名：

[amct_pytorch/common/models/llm/common/quant_apply.py:73-80](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/quant_apply.py#L73-L80) —— `_build_quant_wrapper` 用 `inspect.signature` 探测 `cls.__init__` 是否接受 `group` 参数（或 `**kwargs`），接受就传 `group=`，不接受就只传 `(args, module)`。这样 MLP 包装类（收 group）和注意力包装类（不收 group）能用同一个分发函数。

#### 4.2.4 代码实践

**实践目标：** 确认同一个 `cls` 在三种位置被打上不同的 `group` 标签。

**操作步骤：**

1. 阅读 [test_quant_apply.py:225-255](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/tests/unit_test/common/models/llm/common/test_quant_apply.py#L225-L255) 中的 `test_apply_quant_to_moe_mlp_with_routed_expert_bits` 与 `test_apply_quant_to_moe_mlp_with_shared_experts`。
2. 两个用例都喂了 `quant_target=["moe"]` 和一份带 `moe.routed`/`moe.shared` 分组的 `_MLP_BIT_POLICY`。

**需要观察的现象：** 路由专家包装后 `.group == "moe.routed"`；共享专家包装后 `.group == "moe.shared"`。

**预期结果：** 即便两者用的是同一个 `_FakeQuantWrapper` 类，挂载函数依据**位置名字**（`experts` vs `shared_experts`）打上不同 group，下游 `QuantGatedMLP` 就能据此查到不同位宽。

> 是否本地运行：仓库现有单测，可直接 pytest 运行。

#### 4.2.5 小练习与答案

**练习 1：** 为什么 `shared_experts` 要单独用 `build_no_algo_args`，而不能直接复用原 `args`？

> **参考答案：** 共享专家每个 token 必经、对精度影响大，AMCT 让它**不参与 PTQ 训练算法**（`algos=[]`），只按 yaml 位宽做朴素量化；而路由专家数量多、单个不敏感，才上 PTQ 算法优化。`build_no_algo_args` 就是这条策略的开关。位宽仍从 `bit_policy["moe.shared"]` 读，只是没有可学习算法。

**练习 2：** 一个 dense Qwen3 层（`quant_target=mlp`）进入 `apply_quant_to_moe_mlp` 后，会命中哪几个分支？

> **参考答案：** 只命中第 93-95 行的 `mlp` 分支，`group="mlp"`。因为没有 `experts`/`shared_experts` 子模块，后两个分支不会触发，递归也不会产生新的替换。

---

### 4.3 QuantGatedMLP：用 BitPolicy 为三投影选位宽

#### 4.3.1 概念说明

`QuantGatedMLP` 是一个**通用的门控 MLP 量化外壳**。它的精妙之处在于：dense MLP、路由专家、共享专家三种位置用的是**同一个类**，唯一的区别是构造时传入的 `group` 字符串。位宽、算法、结构变换全都由这个 `group` 驱动 `BitPolicy` 查表得到。

它内部做了三件事：

1. 用 `group` 从 `BitPolicy` 查出 `gate_proj`/`up_proj`/`down_proj` 各自的 `(w, a)` 位宽，据此为三个投影建 `QuantLinear`；
2. 建两个 `ActivationQuantizer`：`input_quant`（量化门控前的输入）和 `hidden_quant`（量化 `down_proj` 前的中间态）；
3. 可选地建两个**结构变换** `input_transform`/`hidden_transform`（如 FlatQuant 的正交变换），分别在激活侧正向作用、在权重侧逆向作用。

Qwen3 的 `QuantQwen3MLP` 就是直接 `class QuantQwen3MLP(QuantGatedMLP): pass`——零覆写，全部行为继承自基类。

#### 4.3.2 核心流程

构造期（`__init__`）建立所有量化零件：

```
ensure_bit_policy(quant_args)                 # 确保 bit_policy 存在
bits = quant_args.bit_policy[group]           # 拿到该 group 的下标代理
gate, up, down = bits["gate_proj"], bits["up_proj"], bits["down_proj"]
self.gate_proj = QuantLinear(args, mlp.gate_proj, w_bits=gate.w, name="gate_proj")
self.up_proj   = QuantLinear(args, mlp.up_proj,   w_bits=up.w,   name="up_proj")
self.down_proj = QuantLinear(args, mlp.down_proj, w_bits=down.w, name="down_proj")
self.input_quant  = ActivationQuantizer(args, gate.a)   # 复用 gate 的激活位宽
self.hidden_quant = ActivationQuantizer(args, down.a)   # 复用 down 的激活位宽
# 两个结构变换（无 structure 算法时为 None）
```

前向期（`forward`）的量化数据流：

```
input_states
  │
  ├─ (可选) input_transform(input_states)      # 结构变换·正向（激活侧）
  │
  ├─ input_quant(x)                            # 激活伪量化，位宽 = gate.a
  │       → x_q
  │
  ├─ up_proj  (x_q, structure_transform=input_transform)  # 权重量化，位宽 = up.w
  ├─ gate_proj(x_q, structure_transform=input_transform)  # 权重量化，位宽 = gate.w
  │
  ├─ hidden = act_fn(gate) * up                # 门控融合
  │
  ├─ (可选) hidden_transform(hidden)           # 结构变换·正向（中间态）
  │
  ├─ hidden_quant(hidden)                      # 激活伪量化，位宽 = down.a
  │       → hidden_q
  │
  └─ down_proj(hidden_q, structure_transform=hidden_transform)  # 权重量化，位宽 = down.w
          → down_states
```

#### 4.3.3 源码精读

构造与位宽选取：

[amct_pytorch/common/models/llm/common/quant_apply.py:139-163](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/quant_apply.py#L139-L163) —— 注意三件事：

- 第 151-152 行 `bits = quant_args.bit_policy[group]` 再 `bits["gate_proj"]`。当 `group="moe.routed"` 时，`_GroupBits.__getitem__` 调 `linear_bits(name="gate_proj", group="moe.routed")`，沿 `moe → routed → gate_proj` 查找。这正是 [u3-l4](u3-l4-bit-policy-config.md) 讲过的逐级回退：叶子有就用叶子，没有就回退到 `moe.routed`，再回退到 `moe`，最后回退到顶层。**一个 `group` 字符串就决定了整条查询路径**。
- 第 153-161 行把原始 `mlp_module.gate_proj`（浮点 `nn.Linear`）连同其权重一并搬进 `QuantLinear`，并标上 `w_bits`。位宽来自上一步查到的 `gate.w`。
- 第 162-163 行两个 `ActivationQuantizer` 分别用 `gate.a` 和 `down.a`。`input_quant` 同时喂给 `up_proj` 和 `gate_proj`——因为两者共享同一个输入 `x_q`，必须用同一位宽；约定取 `gate.a`（典型配置里 `gate`/`up` 激活位宽相同）。

前向数据流：

[amct_pytorch/common/models/llm/common/quant_apply.py:165-180](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/quant_apply.py#L165-L180) —— 对照上面的流程图逐行读。`input_quant`/`hidden_quant` 是 `ActivationQuantizer`，`up_proj`/`gate_proj`/`down_proj` 是 `QuantLinear`。注意每个投影调用都传了 `structure_transform=self.input_transform`（或 `hidden_transform`），这是给 `QuantLinear` 去**对权重做逆变换**用的（见 4.5）。

> 零件层面：`ActivationQuantizer.forward` 在 `is_observe=True` 时走 `calib_forward`（统计 min/max），`False` 时走 `fake_quant`（真正伪量化）——[amct_pytorch/quantization/modules/quant_base.py:103-106](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/modules/quant_base.py#L103-L106)。通路切换由 `set_model_to_observe` 批量控制（见 4.4.3）。`QuantLinear.forward` 则负责权重量化与缓存——[amct_pytorch/quantization/modules/quant_linear.py:39-66](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/modules/quant_linear.py#L39-L66)。本讲把它们当零件，内部算法机制留给 [u6-l1](u6-l1-algo-base-observe.md) 与 [u7-l1](u7-l1-quant-modules.md)。

#### 4.3.4 代码实践

**实践目标：** 画出 `QuantGatedMLP.forward` 的量化数据流，并验证 `group` 如何改变三个投影的位宽。

**操作步骤：**

1. 精读 [quant_apply.py:165-180](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/quant_apply.py#L165-L180)，在纸上画出 4.3.2 的流程图，标注每一段的位宽来源（`gate.w`/`up.w`/`down.w`/`gate.a`/`down.a`）。
2. 构造两份不同的 `bit_config`，分别建 `group="mlp"` 与 `group="moe.routed"` 的 `QuantGatedMLP`，打印三个投影的 `w_bits`。示例代码（仿照 [test_quant_apply.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/tests/unit_test/common/models/llm/common/test_quant_apply.py) 的 `_FakeMLP` 与 `argparse.Namespace` 写法，**示例代码**）：

   ```python
   # 示例代码：仅演示 group 如何影响位宽，需在 amct_pytorch 可 import 的环境运行
   import argparse
   from amct_pytorch.quantization.bit_policy import BitPolicy
   from amct_pytorch.common.models.llm.common.quant_apply import QuantGatedMLP

   cfg = {
       "mlp": {"gate_proj": {"w_bits": 8, "a_bits": 8}, "up_proj": {"w_bits": 8, "a_bits": 8}, "down_proj": {"w_bits": 8, "a_bits": 8}},
       "moe": {"routed": {"w_bits": 4, "a_bits": 8}},   # 路由专家权重压到 4-bit
   }
   args = argparse.Namespace(w_bits=16, a_bits=16, quant_dtype="int", quant_target=["mlp"], algos=[], bit_policy=BitPolicy(cfg))

   class FakeMLP(torch.nn.Module):
       def __init__(self):
           super().__init__()
           self.hidden_size = 8; self.intermediate_size = 16
           self.act_fn = torch.nn.SiLU()
           self.gate_proj = torch.nn.Linear(8, 16); self.up_proj = torch.nn.Linear(8, 16); self.down_proj = torch.nn.Linear(16, 8)

   dense = QuantGatedMLP(args, FakeMLP(), group="mlp")
   routed = QuantGatedMLP(args, FakeMLP(), group="moe.routed")
   print("dense  gate/up/down w_bits:",
         dense.gate_proj.w_bits, dense.up_proj.w_bits, dense.down_proj.w_bits)
   print("routed gate/up/down w_bits:",
         routed.gate_proj.w_bits, routed.up_proj.w_bits, routed.down_proj.w_bits)
   ```

**需要观察的现象：** `dense` 三个投影都是 8；`routed` 三个投影都是 4（`moe.routed` 没写叶子，回退到 `moe.routed` 的 `w_bits=4`）。

**预期结果：** 同一个 `QuantGatedMLP` 类、同一个 `args`，只因 `group` 不同，`gate_proj`/`up_proj`/`down_proj` 的 `w_bits` 就从 8 变成 4。这正是「位置标签驱动位宽」的直接体现。

> 是否本地运行：上述片段依赖完整 amct_pytorch 环境与 torch，**待本地验证**；但仓库现有单测 `test_quant_gated_mlp_forward` 已验证 `forward` 形状正确，可作为行为参照。

#### 4.3.5 小练习与答案

**练习 1：** `input_quant` 为何用 `gate.a` 而不是 `up.a`？

> **参考答案：** `up_proj` 与 `gate_proj` 共享同一个量化输入 `x_q`，所以输入激活必须用**同一个**位宽量化。代码约定取 `gate.a`。在标准配置里 `gate`/`up` 的 `a_bits` 相同，取哪个都行；选 `gate` 只是惯例。

**练习 2：** 若 yaml 里 `moe.routed` 分组只写了 `w_bits`/`a_bits`、没有写 `gate_proj`/`up_proj`/`down_proj` 叶子，三个投影的位宽会是多少？

> **参考答案：** 会全部回退到 `moe.routed` 的分组默认值（即那一组写的 `w_bits`/`a_bits`）。因为 `linear_bits` 沿 `moe → routed → gate_proj` 查找时，`gate_proj` 叶子不存在，链路在 `routed` 处停止，于是返回 `routed` 节点的成对位宽。这正是「组级默认」用法。

---

### 4.4 PlainLinear：为「不量化的投影」补齐签名

#### 4.4.1 概念说明

`QuantLinear.forward` 的签名是 `forward(self, hidden_states, structure_transform=None)`。于是在注意力包装类的 `forward` 里，所有投影都按 `self.q_proj(hidden_states, structure_transform=self.input_transform)` 这样的统一形式调用。

问题来了：注意力有四条投影 `q/k/v/o`，但它们**不一定都量化**。当 `quant_target` 是 `attn-cache`（只量化 KV cache 的 matmul）而非 `attn-linear` 时，这四条投影应当保持浮点。可浮点的 `nn.Linear.forward` 只接受一个位置参数 `x`，根本不认 `structure_transform=` 这个关键字——一旦按统一形式调用就报 `TypeError`。

`PlainLinear` 就是为填补这个缺口而生的**适配器**：它把一个普通 `nn.Linear` 包一层，对外暴露与 `QuantLinear` 完全一致的调用签名，但对 `structure_transform` 视而不见，直接透传给内部 `Linear`。

#### 4.4.2 核心流程

```
class PlainLinear:
    __init__(linear): self.linear = linear
    forward(x, structure_transform=None):
        return self.linear(x)        # 忽略 structure_transform
```

#### 4.4.3 源码精读

[amct_pytorch/common/models/llm/common/quant_apply.py:33-44](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/quant_apply.py#L33-L44) —— 类的 docstring 直白点明了它的用途：「让普通 `nn.Linear` 接受并忽略 `structure_transform`，以匹配 `QuantLinear` 在非量化注意力投影处的调用签名」。`forward` 只做 `self.linear(x)`。

真实使用场景在 Qwen3 注意力包装类：

[amct_pytorch/common/models/llm/qwen/qwen3/quant_module.py:54-73](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/qwen/qwen3/quant_module.py#L54-L73) —— 当 `enable_attn_linear` 为真时四条投影用 `QuantLinear`，为假时用 `PlainLinear`。**两者可互换**，因为 forward 调用点（[quant_module.py:113](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/qwen/qwen3/quant_module.py#L113) 等处）统一写成 `self.q_proj(hidden_states, structure_transform=self.input_transform)`。

> 顺带一提，`quant_apply.py` 里还有个 `set_model_to_observe`：[quant_apply.py:47-50](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/quant_apply.py#L47-L50)。它遍历模型，把所有带 `is_observe` 属性的模块统一置位，用于在校准/量化两态间整体切换。它服务的是 4.3 挂好的那些 `ActivationQuantizer`/`WeightQuantizer`，机制细节留待 [u6-l1](u6-l1-algo-base-observe.md)。

#### 4.4.4 代码实践

**实践目标：** 验证 `PlainLinear` 输出与内部 `Linear` 完全一致，且能吞下任意 `structure_transform`。

**操作步骤：** 阅读 [test_quant_apply.py:77-89](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/tests/unit_test/common/models/llm/common/test_quant_apply.py#L77-L89) 的两个用例 `test_plain_linear_forward_matches_inner_linear` 与 `test_plain_linear_ignores_structure_transform_kwarg`。

**需要观察的现象：** 即便传入 `structure_transform=lambda t: t*0`（本会把权重清零的变换），`PlainLinear` 的输出仍等于 `inner(x)`，变换被完全忽略。

**预期结果：** 两条 `torch.equal(...)` 断言成立——签名兼容，但行为是纯透传。

> 是否本地运行：仓库现有单测，可直接 pytest 运行。

#### 4.4.5 小练习与答案

**练习 1：** 能否在 `attn-cache` 场景直接保留原始 `nn.Linear`、不用 `PlainLinear`？

> **参考答案：** 不能。`QuantQwen3Attn.forward` 对所有投影统一调用 `proj(x, structure_transform=...)`，而 `nn.Linear.forward` 只接受 `x`，传入多余关键字会抛 `TypeError`。必须用 `PlainLinear` 把签名补齐到与 `QuantLinear` 一致。

**练习 2：** `PlainLinear` 会不会改变计算结果或引入额外参数？

> **参考答案：** 不会。它内部直接调 `self.linear(x)`，既不改变数值，也不新增可训练参数（只是把原 `Linear` 作为子模块引用）。它纯粹是「签名适配器」。

---

### 4.5 串讲：structure_transform 的双边协调

`QuantGatedMLP.forward` 里有一个容易看漏的设计：`structure_transform` 同时出现在**激活侧**和**权重侧**。这一节解释为什么。

带结构变换的量化（如 FlatQuant）核心想法是：直接量化权重 \(W\) 时，离群值会让误差暴增；若先把激活用一个可学习的正交矩阵 \(T\) 变走、权重用其逆 \(T^{-1}\) 补偿，量化就能在「更友好」的空间里进行，而数学上保持等价：

\[ y = xW \;\approx\; T(x)\cdot Q\!\left(T^{-1}(W)\right) \]

为此，同一个 `structure_transform` 对象必须以两种方式被调用：

- **激活侧（正向）**：`input_transform(input_states)`，`inv_t=False`，由 `QuantGatedMLP.forward` 直接调用——[quant_apply.py:166-167](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/quant_apply.py#L166-L167)；
- **权重侧（逆向）**：传给 `QuantLinear`，在其内部对权重调 `structure_transform(weight, inv_t=True, name=self.name)`——[quant_linear.py:53-54](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/modules/quant_linear.py#L53-L54)。

FlatQuant 的 `forward` 签名正好同时支持这两种用法：[amct_pytorch/algorithms/quant/flatquant.py:141-146](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/algorithms/quant/flatquant.py#L141-L146)，`inv_t=True` 时对权重做矩阵求逆再转置。

这就是为什么 `QuantGatedMLP.forward` 在调 `up_proj`/`gate_proj` 时要把 `self.input_transform` 再次传进去——它不是给激活用的（激活已经被 `input_transform` 正向处理过），而是**交给 `QuantLinear` 去对权重做逆向变换**。同一个变换对象、两个调用点、两种方向，共同保证「在变换空间里量化、在原空间里还原」的等价性。

当 `--algos` 里不含任何 `structure` 类算法时，`build_algorithms_by_target(..., "structure", ...)` 返回 `None`（见 [quant_base.py:58-65](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/modules/quant_base.py#L58-L65)），`input_transform`/`hidden_transform` 为 `None`，前向里所有 `if ... is not None` 分支自动跳过，退化为普通量化——这就是同一份代码「有 structure 算法则启用、否则自动关闭」的开关。

## 5. 综合实践

**任务：** 为一个 MoE 层手动预测「挂载后每个投影的 group 与位宽」。

**背景：** 假设你拿到一份 `bit_config`（yaml）：

```yaml
moe:
  routed:
    w_bits: 4
    a_bits: 8
    down_proj:
      w_bits: 8       # 路由专家的 down_proj 单独抬到 8-bit
shared:
  w_bits: 8
  a_bits: 8
mlp:                   # dense 层用不到，这里忽略
```

并假设模型是 MoE 结构，`quant_target=moe`，`--algos lwc lac`（不含 structure 算法）。

**请完成：**

1. **画挂载图：** 画出该层经过 `apply_quant_to_moe_mlp` 后的子模块树，标出哪些位置被包装、分别打上什么 `group`（注意区分 `experts` 列表里的每个专家与 `shared_experts`）。
2. **预测位宽：** 对一个**路由专家**的 `QuantGatedMLP`（`group="moe.routed"`），写出它的 `gate_proj`/`up_proj`/`down_proj` 的 `w_bits`，以及 `input_quant`/`hidden_quant` 的激活位宽。给出 `linear_bits` 的回退路径。
3. **预测通路：** 对一个**共享专家**（`group="moe.shared"`），除了位宽不同，它在算法上还有什么区别？（提示：`build_no_algo_args`。）
4. **判断变换：** 因为 `--algos` 不含 structure 算法，`input_transform`/`hidden_transform` 取何值？前向中哪些分支会被跳过？

**参考要点（先自己做再对照）：**

1. `experts[0..N-1]` 每个被包成 `QuantGatedMLP(group="moe.routed")`；`shared_experts` 被包成 `QuantGatedMLP(group="moe.shared")`；`experts` 列表本身和容器 `mlp` 不被整体替换。
2. 路由专家：`gate_proj`/`up_proj` 走 `moe → routed`（无叶子）回退到 `w_bits=4`；`down_proj` 命中叶子 `w_bits=8`；激活位宽 `input_quant=8`（`gate.a` 回退到 `routed.a_bits=8`），`hidden_quant=8`（`down.a=8`）。注意 `down_proj` 的 `w` 与 `a` 都查 `down_proj` 叶子（`a_bits` 叶子未写则 `a` 也回退到 `routed.a_bits=8`）。
3. 共享专家用 `build_no_algo_args`，`algos=[]`，不跑 PTQ 训练算法，只按位宽朴素量化；位宽取 `moe.shared`（注意 yaml 里键名是 `shared` 还是 `moe.shared`，需与 group 字符串对应，必要时以 `BitPolicy.linear_bits` 实际回退为准）。
4. `input_transform`/`hidden_transform` 均为 `None`，前向里 `if self.input_transform is not None` 与 `if self.hidden_transform is not None` 两段结构变换全部跳过，`structure_transform=None` 传给 `QuantLinear` 后其内部 `if structure_transform is not None` 也跳过，退化为不带结构变换的普通量化。

> 上述位宽回退结论建议用 4.3.4 的示例代码片段在本地实际构造 `BitPolicy` 打印验证（**待本地验证**）。

## 6. 本讲小结

- **挂载 = 原地替换子模块。** `apply_quant_to_attn`/`apply_quant_to_moe_mlp` 递归遍历 decoder layer，按 `self_attn`/`mlp`/`experts`/`shared_experts` 等通用名字把原始模块替换成量化包装类 `cls`；挂载函数不认识具体模型，「懂模型」的是适配器传入的 `cls`。
- **group 是位置标签。** dense MLP、路由专家、共享专家复用同一个 `QuantGatedMLP`，仅靠 `group`（`"mlp"`/`"moe.routed"`/`"moe.shared"`）区分；`group` 驱动 `BitPolicy.linear_bits` 沿点分路径回退查位宽。
- **三投影独立配位宽。** `QuantGatedMLP` 为 `gate_proj`/`up_proj`/`down_proj` 各建一个 `QuantLinear`，位宽分别取 `bits[...].w`；两个 `ActivationQuantizer` 分别复用 `gate.a`（输入）与 `down.a`（中间态）。
- **共享专家特殊化。** `shared_experts` 用 `build_no_algo_args` 清空 `algos`，复用外壳但跳过 PTQ 训练算法。
- **PlainLinear 是签名适配器。** 为让不量化的注意力投影能与 `QuantLinear` 站在同一调用点（`proj(x, structure_transform=...)`），`PlainLinear` 包装 `nn.Linear` 并吞掉 `structure_transform` kwarg，行为纯透传。
- **structure_transform 双边协调。** 同一个变换对象在激活侧正向（`inv_t=False`）、在权重侧逆向（`inv_t=True`），由 `QuantGatedMLP` 与 `QuantLinear` 分别调用，保证「变换空间量化、原空间还原」的等价性；无 structure 算法时自动为 `None` 退化为普通量化。

## 7. 下一步学习建议

本讲只讲了「怎么把量化算子挂上去」，挂上去的零件内部如何工作留给后续两讲：

- **[u6-l1 QuantAlgorithmBase 与 is_observe 通路](u6-l1-algo-base-observe.md)**：深入 `ActivationQuantizer`/`WeightQuantizer` 里那些算法模块的 `calib_forward`/`forward`/`trainable_params` 接口，理解 `is_observe` 如何在校准态与量化态间切换——也就是本讲多次提到的 `set_model_to_observe` 的底层机制。
- **[u7-l1 QuantLinear 与量化器模块](u7-l1-quant-modules.md)**：精读 `QuantLinear.forward` 的 `eval_mode` 缓存（`cached_eval_weight`/`transform_key`）与 `WeightQuantizer.export_deploy`，看权重量化与导出落盘的完整链路。
- 若想看「挂载产物如何被 deploy 烘焙」，可衔接 [u4-l4 部署导出 deploy](u4-l4-deploy-export.md) 中 `iter_deploy_bindings` 如何枚举这些 `QuantLinear`。
