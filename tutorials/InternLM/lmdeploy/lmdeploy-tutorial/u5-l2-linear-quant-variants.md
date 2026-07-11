# 线性层与权重量化变体

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `LinearBase` 作为「所有线性层公共基座」承担了哪些职责（张量并行、LoRA、DP-TP 派发），以及它**不**承担哪些职责（具体的量化数学）。
- 看懂「运行时按量化策略选用线性层」的两段式派发：先用 `quant_method` 在 `build_linear` 选类，再用 `OpType` 在每个类里选设备 kernel。
- 对比 AWQ（W4A16）、W8A8（SmoothQuant）、Blocked-FP8 三种量化线性层的**权重参数名、dtype 与形状**差异，理解它们各自的前向签名。
- 认识 LoRA 线性层是如何作为「叠加项」挂到任意量化线性层之上的扩展点。

本讲是 u5-l1（attention/norm/rope 积木）的直接延续。u5-l1 已经确立了 nn 积木的「薄包装 + 委托」桥接模式：构造时 `get_backend() → get_layer_impl_builder(OpType.X) → self.impl`，前向时 `self.impl.forward(...)`，`OpType` 是接口树与实现树唯一的共同词汇。本讲把同一套模式套到**线性层**上，并重点讲清楚「量化」这根额外维度。

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

**什么是线性层。** Transformer 里绝大部分计算量和显存都是线性层（`nn.Linear`）：attention 的 `q/k/v/o_proj`、MLP 的 `gate/up/down_proj`、最后的 `lm_head`。一次线性层就是矩阵乘 `y = xW^T + b`，其中 `x` 是激活（activation），`W` 是权重（weight）。量化的目标就是把这两者的存储与计算从 FP16 压到更低比特，以省显存、提速度。

**权重 量化 vs 激活 量化。** 这是理解本讲三种变体的关键坐标轴：

| 名称 | 权重比特 | 激活比特 | lmdeploy 里的类 | 典型来源 |
|---|---|---|---|---|
| W4A16（AWQ） | 4 bit int | 16 bit float | `AwqLinear` | `lmdeploy lite auto_awq` 离线量化 |
| W8A8（SmoothQuant） | 8 bit int | 8 bit int | `W8A8Linear` | `lmdeploy lite smooth_quant` |
| Blocked-FP8 | 8 bit float | 8 bit float | `BlockedF8Linear` | 模型自带 fp8 权重 |

「WxAy」里第一个字母是权重位数，第二个是激活位数。AWQ 只压权重、激活仍是 FP16，所以前向只需**反量化权重**再做 FP16 矩阵乘；W8A8 与 FP8 同时压权重和激活，前向要做**整数/FP8 矩阵乘**，对硬件 INT8/FP8 指令有依赖。

**「选类」与「选 kernel」是两件事。** LMDeploy 把线性层拆成两层选择：

1. **选类**：根据 `quant_method`（`None` / `'awq'` / `'smooth_quant'` / `'fp8'`）决定实例化哪个 Python 类——这决定了权重怎么打包、怎么从磁盘加载。
2. **选 kernel**：每个类在构造时再用 `OpType`（`Linear` / `LinearW4A16` / `LinearW8A8` / `LinearBlockedF8`）去 `backends` 里挑一个**设备相关**的真实实现（cuda 的 Triton/CUDA kernel、ascend 的、dlinfer 的……）。

第 1 件事在本讲的 `__init__.py` 里做，第 2 件事在每个量化类的 `__init__` 里做。这条「双层派发」与 u5-l1 讲的桥接模式完全一致，只是多绑了一根量化的轴。

## 3. 本讲源码地图

本讲涉及的关键文件，全部位于 `lmdeploy/pytorch/nn/linear/`：

| 文件 | 作用 |
|---|---|
| [`base.py`](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/linear/base.py) | `LinearBase` 公共基座 + `LinearForwardDPTP`（DP-TP 模式下的分轮 GEMM） |
| [`__init__.py`](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/linear/__init__.py) | 派发器：`build_linear` / `build_colwise_linear` / `build_qkv_proj` 等「按 quant_method 选类」的工厂函数 |
| [`default.py`](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/linear/default.py) | `BaseLinear`——不量化的普通 FP16 线性层（对照组） |
| [`awq.py`](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/linear/awq.py) | `AwqLinear`（W4A16）及其 Merged / QKV 变体 |
| [`w8a8.py`](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/linear/w8a8.py) | `W8A8Linear`（SmoothQuant，INT8）及其变体 |
| [`blocked_fp8.py`](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/linear/blocked_fp8.py) | `BlockedF8Linear`（分块 FP8）及其变体 |
| [`lora.py`](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/linear/lora.py) | `LoRA`——挂到任意线性层之上的低秩适配器 |
| [`utils.py`](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/linear/utils.py) | `QKVMixin`（把 q/k/v 头数折算成 out_features 的混入）、`update_tp_args` |

另需引用两个「上游」文件：[`config.py`](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/config.py) 的 `QuantizationConfig`（决定 `quant_method`），以及 [`backends/base.py`](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/backends/base.py) 的 `OpType` 枚举（决定 kernel）。

## 4. 核心概念与源码讲解

### 4.1 LinearBase：所有线性层的公共基座

#### 4.1.1 概念说明

`LinearBase` 是 `AwqLinear` / `W8A8Linear` / `BlockedF8Linear` / `BaseLinear` 的共同父类。它解决一个**与量化无关**的问题：**张量并行（TP）下的一次线性层前向该怎么组织**。具体地，它统一处理：

- **TP 参数初始化**：本层是否切分（`is_tp`）、切多狠（`tp`）、按哪种模式切（`tp_mode`，`DEFAULT` 或 `DP_TP`）、属于 attention 还是 MLP（`layer_type`）。
- **三种前向路径的派发**：普通路径、LoRA 叠加路径、DP-TP 分轮路径。
- **LoRA 挂载点**：`self.lora_adapters = nn.ModuleDict()`，任何子类都可挂 LoRA。

它**不**关心权重的 dtype 与打包方式——那是子类的 `create_weights` / `_forward_default` 的职责。`LinearBase` 把「真正算矩阵乘」的 `_forward_default` 声明为抽象方法，交给子类实现。

> 术语：**colwise（列切）** 把输出的 `out_features` 维按 TP 切，各卡算输出的一段，无需通信即可独立产出（attention 的 qkv_proj、MLP 的 gate/up_proj 都是列切）；**rowwise（行切）** 把输入的 `in_features` 维按 TP 切，各卡算部分输入的乘积，最后要 `all_reduce` 求和（attention 的 o_proj、MLP 的 down_proj 都是行切）。`all_reduce` 仅对行切 + TP>1 才需要。

#### 4.1.2 核心流程

`LinearBase.forward(x)` 的派发逻辑用伪代码表达：

```
def forward(x):
    if tp > 1 and tp_mode == DP_TP:      # DP-TP 模式：分轮 all-gather + GEMM + reduce-scatter
        return _forward_dp_tp(x)
    if 没有挂 LoRA:
        return _forward_default(x, all_reduce, None)   # 交给子类的真正矩阵乘
    else:
        return _forward_lora(x)           # 先算 base，再叠加每个 LoRA 适配器
```

其中 `_forward_lora` 的结构是「base 输出 + Σ adapter(x)」，最后按需 `all_reduce`：

```
def _forward_lora(x, tp_sizes):
    out = _forward_default(x, all_reduce=False, tp_sizes)   # base 矩阵乘（不在此处 reduce）
    for adapter in lora_adapters.values():
        out = adapter(x, out)            # LoRA: 把低秩增量加到 out 上
    if all_reduce:
        all_reduce(out, group=tp_group)  # 行切时才通信
    return out
```

DP-TP 模式（`TPMode.DP_TP`）更复杂：把一整批 token 按 `max_tokens_per_round` 切成若干轮，每轮做 `all-gather → GEMM → reduce-scatter`，让通信与计算重叠。这部分由独立的 `LinearForwardDPTP` 类承担，初学可先把它当黑盒。

#### 4.1.3 源码精读

**`LinearBase` 的构造与 TP 初始化**：

[lmdeploy/pytorch/nn/linear/base.py:111-L138](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/linear/base.py#L111-L138) —— 构造函数记录 `dtype/device/colwise/layer_type`，并预留 `lora_adapters` 空字典。注意默认 `dtype=torch.float16`、`device` 缺省回退到 CPU。

[lmdeploy/pytorch/nn/linear/base.py:140-L174](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/linear/base.py#L140-L174) —— `init_tp_args` 解析本层的 TP 拓扑：从 `dist_manager` 取当前 layer 的 `tp_rank/tp/tp_mode/tp_group`；若 `tp>1 且 tp_mode==DP_TP`，则构造一个 `LinearForwardDPTP` 实例挂到 `self.linear_dptp_forward`。`_tp_args_initialized` 标志位保证重复构造时幂等（Merged/QKV 子类会先调一次 `init_tp_args` 再 `super().__init__`，靠这个标志避免重复初始化）。

**前向派发**：

[lmdeploy/pytorch/nn/linear/base.py:219-L227](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/linear/base.py#L219-L227) —— `forward` 的三分支派发，对应 4.1.2 的伪代码。

[lmdeploy/pytorch/nn/linear/base.py:185-L200](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/linear/base.py#L185-L200) —— `_forward_default` 是抽象方法（`raise NotImplementedError`），`_forward_lora` 则是可复用的模板方法，先调子类的 `_forward_default(all_reduce=False)`，再叠加 LoRA、再视情况 `all_reduce`。

**DP-TP 分轮 GEMM**：

[lmdeploy/pytorch/nn/linear/base.py:21-L108](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/linear/base.py#L21-L108) —— `LinearForwardDPTP`。其 `forward` 的主循环（L96-L107）是典型的「预取重叠」写法：先切出第一批并启动 `all-gather`（`pre`），随后进入 `while` 循环——**每轮先切下一批启动 gather，再等当前批 gather 完成做 GEMM+reduce-scatter**，让下一批的通信与当前批的计算重叠。这与 u4-l2 讲的 EngineLoop「预取写在等待之前」是同一种工程套路。

#### 4.1.4 代码实践

**实践目标**：在只读层面理清 `forward` 的三条分支何时触发。

**操作步骤**：

1. 打开 [`base.py` 的 `forward`](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/linear/base.py#L219-L227)。
2. 用一张表列出三个分支的触发条件与各自最终调用的方法：

| 分支条件 | 调用 | 说明 |
|---|---|---|
| `tp>1 and tp_mode==DP_TP` | `_forward_dp_tp` | DP-TP 模式分轮 GEMM |
| `len(lora_adapters)==0` | `_forward_default(x, all_reduce, None)` | 无 LoRA，直接矩阵乘 |
| 否则 | `_forward_lora(x)` | base + LoRA 叠加 |

3. 追问自己：一个普通单卡、无 LoRA 的 AWQ 模型，会走哪条？（答：第二条。）

**需要观察的现象**：单卡时 `is_tp=False`、`tp=1`，`forward` 直接落到 `_forward_default`，`all_reduce` 在 `update_tp_args` 中因 colwise 或非 TP 被置为 `False`（见 [`utils.py:update_tp_args`](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/linear/utils.py#L20-L29)）。

**预期结果**：能用一句话说清「`LinearBase` 管的是 TP/LoRA/DP-TP 派发，不管量化的具体数学」。本步为纯源码阅读，无需运行，结果**待本地验证**仅在你尝试 import 触发实际 forward 时成立（需 GPU 与编译好的 backends）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `_forward_default` 在 `LinearBase` 里是 `raise NotImplementedError`，而不是直接写 `F.linear(x, self.weight, self.bias)`？

**参考答案**：因为不同子类的权重存储方式完全不同（AWQ 是打包的 int32，W8A8 是 int8，FP8 是 fp8 + block scale），不存在统一的矩阵乘实现。`LinearBase` 只定义「前向的骨架（TP/LoRA 派发）」，把「真正的矩阵乘」留给子类各自委托给 `self.impl`。

**练习 2**：`init_tp_args` 里为什么要 `_tp_args_initialized` 标志？

**参考答案**：`MergedAwqLinear` / `QKVAwqLinear` 等子类在调 `super().__init__()` 之前会先调一次 `init_tp_args(...)` 以便提前拿到 `tp/tp_rank` 去切分头数；之后 `LinearBase.__init__` → `init_tp_args` 会再调一次。标志位让第二次调用直接返回，保证幂等、不重复构造 `LinearForwardDPTP`。

---

### 4.2 量化分发器 build_linear：把 quant_method 翻译成具体类

#### 4.2.1 概念说明

`build_linear`（以及 `build_colwise_linear` / `build_qkv_proj` / `build_gateup_linear` 等同族工厂）是模型重写类（如 u3-l4 讲的 `LlamaForCausalLM`）创建线性层的**唯一入口**。它做两件事：

1. 解析本层该用什么 `quant_method`；
2. 按结果 `return` 对应的类实例。

`quant_method` 不是凭空来的——它来自 `QuantizationConfig.get_quant_method(prefix, module_kind)`，该函数会结合**全局量化方法**、**忽略层名单（ignored_layers）**、**fp8 作用域（fp8_quant_scope）** 给出「这一层具体用不用量化、用哪种」。也就是说，同一个 fp8 模型里，某些层（比如 `lm_head` 或被显式忽略的层）可能 `quant_method=None` 走普通 FP16 线性层，而 MoE 专家层走 fp8。

> 术语：`quant_config` 在 `build_linear` 入口处被**重新取自** `get_build_model_context().quant_config`（见 [__init__.py:L39](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/linear/__init__.py#L38-L40)），而不是用调用方传进来的那个 `dict`。这是因为真正可用的 `quant_config` 是引擎在加载模型时从 HF `config.json` 解析出来的 `QuantizationConfig` 对象，挂在「构建上下文」上。调用方传的 `quant_config` 参数只是一个「是否关心量化」的开关。

#### 4.2.2 核心流程

`build_linear` 的派发用伪代码表达：

```
def build_linear(in_features, out_features, bias, quant_config, ...):
    quant_method = None
    if quant_config is not None:
        quant_config = 构建上下文里的 QuantizationConfig
        quant_method = quant_config.get_quant_method(prefix, module_kind='linear')

    if quant_method is None:      return BaseLinear(...)      # 不量化
    if quant_method == 'awq':         return AwqLinear(w_bit=quant_config.bits, group_size=quant_config.group_size, ...)
    if quant_method == 'smooth_quant': return W8A8Linear(quant_dtype=quant_config.quant_dtype, ...)
    if quant_method == 'fp8':         return BlockedF8Linear(fp8_dtype=quant_config.quant_dtype, scale_fmt=quant_config.scale_fmt, ...)
    raise RuntimeError('Unsupported quant method')
```

注意每种量化类从 `quant_config` 取的字段不同：AWQ 取 `bits/group_size`，W8A8 取 `quant_dtype`，FP8 取 `quant_dtype/scale_fmt`。这正对应 `QuantizationConfig` 上那几个字段。

`build_qkv_proj` / `build_merged_colwise_linear` 是同一套思路的「打包版」：把 q/k/v 或 gate/up 融合成一个线性层，分别返回 `QKVXxxLinear` / `MergedXxxLinear`（如 `QKVAwqLinear`、`MergedBlockedF8Linear`）。它们与对应的「单层版」共享父类，只是多了分片权重加载逻辑（u3-l5 讲的 `stacked_params_mapping` 对接点）。

#### 4.2.3 源码精读

**派发主逻辑**：

[lmdeploy/pytorch/nn/linear/__init__.py:18-L100](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/linear/__init__.py#L18-L100) —— `build_linear` 全文。关键四段：`quant_method` 解析（L37-L40）、`None → BaseLinear`（L45-L58）、`'awq' → AwqLinear`（L60-L72）、`'smooth_quant' → W8A8Linear`（L73-L83）、`'fp8' → BlockedF8Linear`（L84-L98）、兜底 `raise`（L99-L100）。

**量化方法的决定者 `QuantizationConfig`**：

[lmdeploy/pytorch/config.py:749-L762](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/config.py#L749-L762) —— `get_quant_method`。两条规则值得记：① 当 `quant_method=='fp8'` 且 `fp8_quant_scope=='moe_only'` 且本层不是 MoE 时，返回 `None`（即非 MoE 层不量化，u9 讲的「fp8 moe only」场景）；② 当本层 `prefix` 命中 `ignored_layers` 时返回 `None`。这两条决定了「同一模型里不同层可以走不同线性层类」。

[lmdeploy/pytorch/config.py:663-L673](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/config.py#L663-L673) —— `QuantizationConfig` 的字段定义，可见 `quant_method / quant_dtype / scale_fmt / bits / group_size / weight_block_size / activation_scheme / ignored_layers / fp8_quant_scope` 这一整套词汇表，正是各量化类构造参数的来源。

**OpType——选类的下一步（选 kernel）的词汇表**：

[lmdeploy/pytorch/backends/base.py:12-L41](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/backends/base.py#L12-L41) —— `OpType` 枚举。线性层相关的四个：`Linear`（L16，不量化）、`LinearW8A8`（L24）、`LinearW4A16`（L27）、`LinearBlockedF8`（L31）。每个量化类构造时各取其一去 `backends` 选 impl，这就是「选 kernel」那一层。

#### 4.2.4 代码实践

**实践目标**：把「quant_method → 类 → OpType」三者的对应关系亲手列出来。

**操作步骤**：

1. 读 [`build_linear`](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/linear/__init__.py#L18-L100)，填出下表的第 2、3 列。

| `quant_method` | 实例化的类（`__init__.py`） | 该类构造时用的 `OpType`（去对应文件 `__init__` 里找 `get_layer_impl_builder(OpType.X)`） |
|---|---|---|
| `None` | `BaseLinear` | `OpType.Linear` |
| `'awq'` | ? | ? |
| `'smooth_quant'` | ? | ? |
| `'fp8'` | ? | ? |

2. 对照答案：`'awq' → AwqLinear → OpType.LinearW4A16`（[awq.py:L40](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/linear/awq.py#L40)）；`'smooth_quant' → W8A8Linear → OpType.LinearW8A8`（[w8a8.py:L36](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/linear/w8a8.py#L36)）；`'fp8' → BlockedF8Linear → OpType.LinearBlockedF8`（[blocked_fp8.py:L46](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/linear/blocked_fp8.py#L46)）。

**需要观察的现象**：注意 `'smooth_quant'` 这个字符串——它在 `build_linear` 里映射到 `W8A8Linear`（W8A8 是 SmoothQuant 的产物）。不要被类名 `W8A8` 和 `quant_method='smooth_quant'` 的名字不一致迷惑，这是历史命名。

**预期结果**：能画出 `模型层 prefix → get_quant_method → quant_method → build_linear 选类 → OpType → backends 选 kernel` 的完整链路。本步纯阅读，**待本地验证**仅在你构造真实 `QuantizationConfig.from_config(...)` 并观察 `get_quant_method` 返回时成立。

#### 4.2.5 小练习与答案

**练习 1**：一个 fp8 模型，`fp8_quant_scope='moe_only'`，它的 attention 的 `qkv_proj` 会走哪个线性层类？

**参考答案**：走 `BaseLinear`（不量化）。因为 [`get_quant_method`](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/config.py#L749-L762) 在 `quant_method=='fp8' and fp8_quant_scope=='moe_only' and module_kind != 'moe'` 时返回 `None`，于是 `build_linear` 落入 `quant_method is None` 分支返回 `BaseLinear`。只有 MoE 专家层（`module_kind='moe'`）才走 `BlockedF8Linear`。

**练习 2**：为什么 `build_linear` 把调用方传进来的 `quant_config` 参数覆盖成构建上下文里的那个？

**参考答案**：调用方（如 `llama.py`）手里并没有真正的 `QuantizationConfig` 对象，它只是在 `build_*` 时传一个非空占位（表示「这一层关心量化」）。真正由 HF `config.json` 解析出来的 `QuantizationConfig` 挂在 `get_build_model_context()` 上，`build_linear` 必须取那个权威对象，才能拿到 `bits/group_size/quant_dtype/scale_fmt` 等真实参数。

---

### 4.3 AwqLinear：W4A16 权重量化

#### 4.3.1 概念说明

`AwqLinear` 实现 **W4A16**：权重压成 4 bit 整数（pack 进 int32），激活仍是 FP16。它由 `lmdeploy lite auto_awq` 离线量化产出（见 u7-l2）。前向时只需把权重**反量化**回 FP16 再做 FP16 GEMM，因此对硬件没有 INT8 指令要求，兼容性最好、是省显存的首选。

AWQ 的核心存储有三件套：

- **qweight**：打包的 4bit 权重。`elem_per_int = 32 // w_bit` 个权重 pack 进一个 int32（w_bit=4 时是 8 个）。
- **scales**：每组权重的缩放系数，FP16。
- **qzeros**：每组权重的零点，同样 pack 进 int32。

分组是沿着**输入维（in_features）**进行的，每 `group_size` 个连续输入共享一组 scale/zero。反量化公式：

\[
w_{real}[i,j] = s_g \cdot (\text{unpack}(qweight)[i,j] - z_g), \quad g = \lfloor i / \text{group\_size} \rfloor
\]

其中 \(s_g\)、\(z_g\) 由输入下标 \(i\) 落在第几个组决定，\(j\) 是输出维下标。

#### 4.3.2 核心流程

`AwqLinear.__init__` 的步骤：

1. 调 `super().__init__(dtype=torch.float16, ...)`（AWQ 强制 FP16 激活）。
2. 若 `is_tp`，用 `_get_io_features` 按 TP 切 `in_features/out_features`，且对齐量是 `max(32//w_bit, group_size)`——因为打包与分组都要求整除。
3. `create_weights` 分配 qweight/scales/qzeros/bias 四个张量。
4. `get_backend().get_layer_impl_builder(OpType.LinearW4A16).build(...)` 选设备 kernel，挂到 `self.impl`。
5. `register_all_parameters` 注册参数，`setup_loaders` 给每个参数挂上 `weight_loader`（供 u3-l5 的权重加载器调用，按 TP 切片）。

前向只有一行：`_forward_default` 把四个权重张量连同 `all_reduce`、`tp_group` 交给 `self.impl.forward`。

#### 4.3.3 源码精读

**构造与选 kernel**：

[lmdeploy/pytorch/nn/linear/awq.py:17-L53](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/linear/awq.py#L17-L53) —— `AwqLinear.__init__`。注意 L40 `OpType.LinearW4A16`，L53 `elem_per_int = 32 // w_bit`。

**权重的 dtype 与形状（本讲重点）**：

[lmdeploy/pytorch/nn/linear/awq.py:143-L159](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/linear/awq.py#L143-L159) —— `create_weights`。三项断言（`in_features % group_size == 0`、`out_features % elem_per_int == 0`）保证了分组与打包都对齐。三个张量的形状与 dtype：

| 参数 | dtype | 形状 |
|---|---|---|
| `qweight` | `torch.int32` | `(in_features, out_features // elem_per_int)` |
| `scales` | `torch.float16` | `(in_features // group_size, out_features)` |
| `qzeros` | `torch.int32` | `(in_features // group_size, out_features // elem_per_int)` |

> 关键观察：AWQ 的 qweight 形状是 **`(in, out//elem)`**——**第一维是 in_features**，且 4bit 权重是沿**输出维** pack 进 int32 的。这与下面 W8A8/FP8 的 `[out, in]`（PyTorch 约定）**布局相反**，是对比两者时最容易踩的坑。

**前向**：

[lmdeploy/pytorch/nn/linear/awq.py:166-L168](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/linear/awq.py#L166-L168) —— `_forward_default` 把 `x, qweight, scales, qzeros, bias, all_reduce, group` 全部转交给 `self.impl`。真正的「unpack + 反量化 + FP16 GEMM」藏在 `backends` 的 W4A16 kernel 里（u5-l4/u5-l5 讲）。

[lmdeploy/pytorch/nn/linear/awq.py:161-L164](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/linear/awq.py#L161-L164) —— `update_weights`：调 `self.impl.update_weights(...)` 做派生计算后重新注册参数，这是引擎在权重加载完成后统一收口的钩子。

#### 4.3.4 代码实践

**实践目标**：手算一个真实尺寸下 AWQ 权重各张量的形状，体会「4bit 压缩 + 分组」的显存收益。

**操作步骤**：

假设一个 `in_features=4096, out_features=4096, w_bit=4, group_size=128` 的 AWQ 线性层：

1. `elem_per_int = 32 // 4 = 8`。
2. `qweight` 形状 = `(4096, 4096 // 8)` = `(4096, 512)`，dtype int32。
3. `scales` 形状 = `(4096 // 128, 4096)` = `(32, 4096)`，dtype float16。
4. `qzeros` 形状 = `(32, 512)`，dtype int32。
5. 估算显存：`qweight = 4096*512*4 B = 8 MiB`；对比不量化的 FP16 权重 `4096*4096*2 B = 32 MiB`——权重本体压缩到约 1/4。

**需要观察的现象**：`scales`/`qzeros` 的第一维 `in_features // group_size` 正是「分组数」，`group_size` 越小、精度越高但显存越大。

**预期结果**：你能用 `torch.empty(...)` 按上表 dtype/形状亲手构造出三个张量（CPU 上即可，纯张量操作）。实际跑 `AwqLinear.__init__` 需要 `get_backend()` 返回真实后端，**待本地验证**（需 GPU 与编译产物）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 AWQ 的对齐量是 `max(32 // w_bit, group_size)` 而不是单纯的 `group_size`？

**参考答案**：因为 TP 切分时既要保证切出来的片段能被 `group_size` 整除（否则分组被切断），又要保证能被 `elem_per_int = 32//w_bit` 整除（否则一个 int32 里 pack 的权重被切断，无法独立 unpack）。取两者的较大值同时满足这两条约束。

**练习 2**：AWQ 的前向需要量化激活吗？

**参考答案**：不需要。AWQ 是 W4**A16**，激活始终是 FP16。前向只需把 4bit 权重反量化回 FP16 再做 FP16 GEMM。这也是它对硬件 INT8 指令无依赖、兼容性最好的原因。

---

### 4.4 W8A8Linear：SmoothQuant 权重激活量化

#### 4.4.1 概念说明

`W8A8Linear` 实现 **W8A8（INT8）**，对应 `lmdeploy lite smooth_quant`（见 u7-l3）。与 AWQ 不同，它**权重和激活都压成 8 bit 整数**，前向走 INT8 矩阵乘，依赖硬件 INT8 指令（如 NVIDIA 的 `int8` Tensor Core）。好处是计算更快、带宽更省；代价是需要 activation scale 来量化激活。

它的存储是三件套，但语义与 AWQ 不同：

- **weight**：INT8 权重（`quant_dtype`，默认 `torch.int8`）。
- **scale**：**逐输出通道（per-output-channel）** 的缩放系数，FP32，形状 `(out_features, 1)`。
- **bias**：FP（dtype）偏置。

> 注意 SmoothQuant 的「逐通道」与 AWQ 的「逐组」是两种不同的量化粒度：W8A8 的 scale 沿输出维每通道一个，AWQ 的 scale 沿输入维每 `group_size` 一个。

#### 4.4.2 核心流程

构造流程与 `AwqLinear` 同构，差异在三处：

1. `super().__init__` 用调用方传入的 `dtype`（不像 AWQ 强制 FP16）。
2. 选 `OpType.LinearW8A8` 的 impl，且 `build` 多传一个 `quant_dtype`（支持 int8 / fp8 的 W8A8 变体）。
3. `create_weights` 的布局是 **`[out, in]`**（PyTorch `nn.Linear` 约定，权重转置存储），`scale` 是 `[out, 1]`。

前向签名也比 AWQ 少一个 zero 点参数：`impl.forward(x, weight, scale, bias, all_reduce, group=tp_group)`。

#### 4.4.3 源码精读

**构造**：

[lmdeploy/pytorch/nn/linear/w8a8.py:17-L47](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/linear/w8a8.py#L17-L47) —— `W8A8Linear.__init__`。L36 `OpType.LinearW8A8`，L37 记录 `quant_dtype`。

**权重 dtype 与形状（与 AWQ 对照的重点）**：

[lmdeploy/pytorch/nn/linear/w8a8.py:109-L117](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/linear/w8a8.py#L109-L117) —— `create_weights`：

| 参数 | dtype | 形状 |
|---|---|---|
| `weight` | `self.quant_dtype`（默认 `torch.int8`） | `(out_features, in_features)` |
| `scale` | `torch.float32` | `(out_features, 1)` |
| `bias` | `dtype`（如 float16/bfloat16） | `(out_features,)` |

> 关键对比：W8A8 的 weight 是 **`(out, in)`** 且**不再 pack**（int8 一字节一个元素），与 AWQ 的 `(in, out//elem)` 打包布局**形状、维度顺序、是否打包**都不同。

**前向**：

[lmdeploy/pytorch/nn/linear/w8a8.py:124-L126](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/linear/w8a8.py#L124-L126) —— `_forward_default`，签名 `(x, weight, scale, bias, all_reduce, group)`，比 AWQ 少 qzeros。INT8 GEMM + per-channel scale 的真正计算在 `backends` 的 W8A8 kernel（u5-l5 会讲 `w8a8_triton_kernels.py`）。

**TP 行切时的特殊处理**：

[lmdeploy/pytorch/nn/linear/w8a8.py:82-L96](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/linear/w8a8.py#L82-L96) —— `_weight_loader_tp_rowwise`：行切时 weight 沿 dim=1（in 维）切；但 scale 的形状是 `(out,1)`，`loaded_weight.size(1)==1` 时直接整块拷贝（不切），bias 则只在 rank=0 保留、其余卡清零。这套规则在 AWQ 里也类似，只是对齐量不同。

#### 4.4.4 代码实践

**实践目标**：把 AWQ 与 W8A8 的 `create_weights` 并排对比，列出 dtype 与形状差异（本讲总实践任务的第一半）。

**操作步骤**：

1. 同时打开 [awq.py:create_weights](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/linear/awq.py#L143-L159) 与 [w8a8.py:create_weights](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/linear/w8a8.py#L109-L117)。
2. 填出对比表：

| 维度 | AWQ (W4A16) | W8A8 |
|---|---|---|
| weight 参数名 | `qweight` | `weight` |
| weight dtype | `int32`（pack） | `quant_dtype`（默认 `int8`） |
| weight 形状 | `(in, out//elem)` | `(out, in)` |
| 缩放参数 | `scales` (FP16, `(in//gs, out)`) + `qzeros` (int32) | `scale` (FP32, `(out,1)`) |
| 量化粒度 | 沿输入维按 `group_size` 分组 | 沿输出维逐通道 |
| 前向签名 | `(x, qweight, scales, qzeros, bias, all_reduce, group)` | `(x, weight, scale, bias, all_reduce, group)` |

3. 再对照 [`_forward_default`](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/linear/w8a8.py#L124-L126) 确认前向签名确实少一个 `qzeros`。

**需要观察的现象**：两者**权重布局维度顺序相反**（AWQ 是 `[in, ...]`，W8A8 是 `[out, in]`），且 AWQ 有 zero point、W8A8 没有。

**预期结果**：能不看源码复述上表。纯阅读型实践，**待本地验证**仅在你真跑前向时成立。

#### 4.4.5 小练习与答案

**练习 1**：W8A8 的 `scale` 形状是 `(out_features, 1)`，为什么不是标量（per-tensor）？

**参考答案**：SmoothQuant 采用**逐输出通道**量化（per-output-channel），每个输出通道有自己的缩放系数，精度比 per-tensor 高。`(out, 1)` 的形状方便在 INT8 GEMM 后沿输出维广播相乘。

**练习 2**：`W8A8Linear` 的 `quant_dtype` 默认是 `torch.int8`，但它还能接受什么？为什么？

**参考答案**：还能接受 `torch.float8_e4m3fn / float8_e5m2`（见 [`_weight_loader_tp_rowwise`](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/linear/w8a8.py#L82-L96) 里 `param.dtype in (int8, float8_e4m3fn, float8_e5m2)` 的判断）。这说明 `W8A8Linear` 也被复用做「W8A8 但权重是 fp8」的场景（per-channel scale 的 fp8）。而真正的**分块** fp8（128×128 block scale）才用 `BlockedF8Linear`。

---

### 4.5 BlockedF8Linear：分块 FP8 量化

#### 4.5.1 概念说明

`BlockedF8Linear` 实现 **分块 FP8（Blocked FP8）**，权重与激活都是 8 bit 浮点（默认 `float8_e4m3fn`），但缩放系数不是逐通道、而是按 **128×128 的块（block）** 给出。这类权重通常由模型作者直接训练/转换产出（HF `config.json` 里 `quant_method='fp8'`），而非 lmdeploy lite 量化。

分块量化的反量化公式：

\[
W_{real}[j,i] = W_{fp8}[j,i] \cdot \sigma\!\left(\left\lfloor j/128 \right\rfloor, \left\lfloor i/128 \right\rfloor\right)
\]

其中 \(\sigma\) 是形状 `(ceil(out/128), ceil(in/128))` 的块缩放矩阵。128×128 的块粒度比逐通道更细，精度更高，是现代 FP8 推理（如 DeepSeek、Qwen3.5 等大模型）的主流选择。

`BlockedF8Linear` 还有一个 AWQ/W8A8 都没有的能力：**在线量化（online quantization）**。如果磁盘上的权重还是 FP16/FP32（即 HF 模型本身没存成 fp8），`weight_loader_with_quant` 会在**加载时**调用 `quant_blocked_fp8` 把它压成分块 fp8 再存进参数。这让「拿一个普通 FP16 模型 + 一句 fp8 配置」也能跑 fp8 推理。

#### 4.5.2 核心流程

构造流程与 W8A8 几乎一样，差异：

1. 固定 `block_size = 128`，记录 `fp8_dtype` 与 `scale_fmt`（缩放系数的存储格式）。
2. 选 `OpType.LinearBlockedF8` 的 impl，并额外 `impl.set_scale_fmt(scale_fmt)`。
3. `create_weights` 的 `weight_scale_inv` 形状按 128×128 块向上取整（`div_up`）。
4. weight 参数挂的 loader 是 `weight_loader_with_quant`（而非普通 `weight_loader`），从而支持在线量化。

前向有一个 DP-TP 分支：`tp_mode==DP_TP` 时多传 `rank` 与 `scatter_size` 给 impl。

#### 4.5.3 源码精读

**构造**：

[lmdeploy/pytorch/nn/linear/blocked_fp8.py:19-L57](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/linear/blocked_fp8.py#L19-L57) —— `BlockedF8Linear.__init__`。L41 `block_size=128`，L46 `OpType.LinearBlockedF8`，L52 `set_scale_fmt`。

**权重 dtype 与形状**：

[lmdeploy/pytorch/nn/linear/blocked_fp8.py:132-L142](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/linear/blocked_fp8.py#L132-L142) —— `create_weights`：

| 参数 | dtype | 形状 |
|---|---|---|
| `weight` | `self.fp8_dtype`（默认 `torch.float8_e4m3fn`） | `(out_features, in_features)` |
| `weight_scale_inv` | `torch.float32` | `(div_up(out,128), div_up(in,128))` |
| `bias` | `dtype` | `(out_features,)` |

> 关键观察：FP8 的 weight 布局与 W8A8 一致是 `(out, in)`，但 scale 从「逐通道 `(out,1)`」变成了「逐块 `(out/128, in/128)`」——这是它与 W8A8 的本质区别。`div_up` 即向上取整除法（见 [`nn/utils.py:div_up`](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/utils.py#L5-L8)），保证不能整除时块数足够。

**在线量化加载器（FP8 独有）**：

[lmdeploy/pytorch/nn/linear/blocked_fp8.py:119-L130](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/linear/blocked_fp8.py#L119-L130) —— `weight_loader_with_quant`：当 `loaded_weight.dtype != param.dtype`（即磁盘是 FP16、参数要 fp8）时，调 `quant_blocked_fp8(loaded_weight, param.dtype, block_size, scale_fmt)` 同时产出量化权重与块缩放，分别灌进 `weight` 与 `weight_scale_inv`。`quant_blocked_fp8` 实现在 [`lite/quantization/weight/quant_utils.py`](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/quant_utils.py#L2)（u7 讲量化时会细讲）。

**前向（含 DP-TP 分支）**：

[lmdeploy/pytorch/nn/linear/blocked_fp8.py:149-L162](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/linear/blocked_fp8.py#L149-L162) —— `_forward_default`：DP-TP 模式下多传 `rank/scatter_size`，普通模式签名 `(x, weight, weight_scale_inv, bias, all_reduce, group)`。真正的 fp8 GEMM 在 backends 的 BlockedF8 kernel。

**与 DP-TP 的耦合**：

[lmdeploy/pytorch/nn/linear/__init__.py:42-L43](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/linear/__init__.py#L42-L43) 与 [L205-L206](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/linear/__init__.py#L205-L206) —— `dp_gather` 目前只允许与 `fp8` 同用（`assert quant_method in ['fp8']`），说明 DP-TP 的分轮 GEMM（`LinearForwardDPTP`）目前只对 fp8 线性层启用。

#### 4.5.4 代码实践

**实践目标**：手算 FP8 的块缩放形状，并理解在线量化的触发条件。

**操作步骤**：

对 `in_features=4096, out_features=11008`（典型 MLP 中间维）的 FP8 线性层：

1. `weight` 形状 = `(11008, 4096)`，dtype `float8_e4m3fn`（每元素 1 字节）。
2. `weight_scale_inv` 形状 = `(div_up(11008,128), div_up(4096,128))` = `(86, 32)`，dtype `float32`。
3. 估算显存：`weight = 11008*4096*1 B ≈ 43 MiB`；对比 FP16 的 `11008*4096*2 B ≈ 86 MiB`，权重压到 1/2，外加极小的 scale 开销。
4. 读 [`weight_loader_with_quant`](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/linear/blocked_fp8.py#L119-L130)，确认触发条件是 `loaded_weight.dtype != param.dtype`——即「磁盘 dtype 与目标 dtype 不一致」时才量化。

**需要观察的现象**：若你直接加载一个已经存成 fp8 的 HF 模型，`loaded_weight.dtype` 本就是 `float8_e4m3fn`，与 `param.dtype` 相同，于是走 `else` 分支**不做**在线量化、直接拷贝；只有拿 FP16 模型配 fp8 配置时才触发量化。

**预期结果**：能说出「在线量化只在 dtype 不匹配时发生」。`quant_blocked_fp8` 的真实数值行为**待本地验证**（需 GPU）。

#### 4.5.5 小练习与答案

**练习 1**：`weight_scale_inv` 为什么用 `div_up`（向上取整）而不是普通整除？

**参考答案**：因为 `out_features` / `in_features` 不一定是 128 的整数倍（如 11008 不是 128 的倍数，11008/128=85.99）。向上取整保证最后那个「不完整的块」也有一个 scale，否则尾部元素无法反量化。

**练习 2**：同样是 8 bit，`BlockedF8Linear` 与 `W8A8Linear` 的最大区别是什么？

**参考答案**：① 数据类型：FP8 是浮点（`float8_e4m3fn`），W8A8 默认是整数（`int8`）；② 缩放粒度：FP8 按 128×128 块（`weight_scale_inv`），W8A8 按输出通道（`scale` 为 `(out,1)`）；③ FP8 支持 `dp_gather`（DP-TP 分轮 GEMM）与在线量化，W8A8 不支持。

---

### 4.6 LoRA 线性层：挂到任意量化层之上的扩展点（拓展）

> 本节超出四个最小模块的硬性范围，但「认识 LoRA 线性层的扩展点」是学习目标之一，且它能与前四节自然衔接，故作为拓展模块。

#### 4.6.1 概念说明

LoRA（Low-Rank Adaptation）不改变 base 权重，而是新增一个低秩增量 \(\Delta W = B A\)（\(A \in \mathbb{R}^{r \times in}, B \in \mathbb{R}^{out \times r}\)，\(r \ll \min(in,out)\)），前向变成：

\[
y = xW^T + x A^T B^T \cdot \text{scaling} = \text{base}(x) + \text{LoRA}(x)
\]

LMDeploy 的 LoRA 设计很优雅：`LoRA` 是一个**独立的小模块**，并不重写 base 线性层。任何量化线性层（`AwqLinear`/`W8A8Linear`/`BlockedF8Linear`/`BaseLinear`）都从 `LinearBase` 继承了 `self.lora_adapters: nn.ModuleDict`，运行时把 `LoRA` 实例挂进去即可，`forward` 自动走 4.1.2 的 `_forward_lora` 路径——**base 输出 + Σ adapter(x)**。这意味着「量化」与「LoRA 微调」可以叠加，无需为每种量化各写一个 LoRA 变体。

#### 4.6.2 源码精读

[lmdeploy/pytorch/nn/linear/lora.py:12-L58](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/linear/lora.py#L12-L58) —— `LoRA` 类。它构造时同样走 `get_backend().get_layer_impl_builder(OpType.LoRA).build()` 选 impl（u5-l1 的桥接模式），注册 `lora_A` / `lora_B` 两个参数；`forward(x, base_output=None)` 把 base 的输出作为可选参数传进 impl，由 kernel 决定是「先算 base 再加」还是「融合在一起」。

LoRA 的多适配器管理与生命周期在 [`pytorch/adapter/adapter.py`](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/adapter/adapter.py)（u10-l2 详讲），本节只认它在 linear 层的接入点。

## 5. 综合实践：一张表串起所有线性层变体

**任务**：综合本讲内容，制作一张「线性层全家桶对照表」，并用源码佐证每一格。

请填完下表（部分已给出），所有信息必须能在对应文件里找到出处：

| 维度 | 不量化 | AWQ | W8A8 (SmoothQuant) | Blocked-FP8 |
|---|---|---|---|---|
| `quant_method` | `None` | `'awq'` | ? | ? |
| 类名 | `BaseLinear` | ? | ? | ? |
| 选类的工厂 | `build_linear` | `build_linear` | ? | ? |
| `OpType` | `Linear` | ? | ? | ? |
| 权重参数名 | `weight` | ? | ? | ? |
| 权重 dtype | `dtype`(FP16/BF16) | ? | ? | ? |
| 权重形状 | `(out,in)` | ? | ? | ? |
| 缩放参数 | 无 | ? | ? | ? |
| 是否支持 `dp_gather` | 否 | 否 | 否 | ? |
| 是否支持在线量化 | 否 | 否 | 否 | ? |

**参考答案**（填好后核对）：

- `quant_method`：`'smooth_quant'`、`'fp8'`。
- 类名：`AwqLinear`、`W8A8Linear`、`BlockedF8Linear`。
- `OpType`：`LinearW4A16`、`LinearW8A8`、`LinearBlockedF8`。
- 权重参数名：`qweight`、`weight`、`weight`。
- 权重 dtype：`int32`(pack)、`quant_dtype`(默认 int8)、`fp8_dtype`(默认 float8_e4m3fn)。
- 权重形状：`(in, out//elem)`、`(out, in)`、`(out, in)`。
- 缩放参数：`scales`(FP16)+`qzeros`(int32)、`scale`(FP32,`(out,1)`)、`weight_scale_inv`(FP32,`(out/128,in/128)`)。
- `dp_gather`：仅 FP8 支持。
- 在线量化：仅 FP8 支持（`weight_loader_with_quant`）。

**进阶（可选）**：写一小段脚本，构造一个 `QuantizationConfig`（用 [`from_config`](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/config.py#L682-L747) 喂一个假的 `quantization_config` 字典），打印其 `quant_method/bits/group_size/quant_dtype/scale_fmt` 字段，验证你对「不同 quant_method 取不同字段」的理解。此步**待本地验证**（需可 import lmdeploy）。

## 6. 本讲小结

- `LinearBase` 是所有线性层的公共基座，统一处理 **TP/LoRA/DP-TP 三种前向路径的派发**，但把真正的矩阵乘 `_forward_default` 留给子类——它**不管量化的具体数学**。
- 「运行时按量化策略选层」是**两段式派发**：`build_linear`（`__init__.py`）按 `quant_method` 选 Python 类（决定权重打包与加载），每个类再按 `OpType` 在 `backends` 选设备 kernel（决定真实计算）。
- `quant_method` 由 `QuantizationConfig.get_quant_method(prefix, module_kind)` 决定，受 `ignored_layers` 与 `fp8_quant_scope='moe_only'` 影响，因此**同一模型不同层可走不同线性层类**。
- 四种权重布局对比：`BaseLinear` FP16 `(out,in)`；`AwqLinear` 打包 int32 `(in, out//elem)` 且有 zero point；`W8A8Linear` int8 `(out,in)` 逐通道 scale；`BlockedF8Linear` fp8 `(out,in)` 逐 128×128 块 scale，且独有**在线量化**与 `dp_gather` 能力。
- LoRA 是挂到 `LinearBase.lora_adapters` 上的独立模块，`forward` 自动走 `_forward_lora` 的「base + Σadapter」路径，可与任意量化叠加。

## 7. 下一步学习建议

- **向「下」追 kernel**：本讲所有量化类的 `_forward_default` 最终都委托给 `self.impl`，而 `self.impl` 的真身住在 `backends/`。下一讲 **u5-l4（算子后端分发 backends）** 讲 `selector.py` 如何按 `device + quant` 选定这些 impl，建议结合本讲的 `OpType` 表一起读。
- **向「深」追 kernel 实现**：u5-l5（Triton/CUDA Kernel）会讲 `w8a8_triton_kernels.py` 等，是 W8A8 线性层 `self.impl.forward` 的最终落点。
- **向「上游」追调用方**：回看 u3-l4 的 `llama.py`，确认 `build_qkv_proj / build_o_proj / build_gateup_linear / build_down_linear` 就是本讲工厂函数的调用点，体会「模型重写文件不写 `if quant` 分支」的设计。
- **向「量化链路」追来源**：AWQ 权重来自 u7-l2（`auto_awq`），W8A8 来自 u7-l3（`smooth_quant`），FP8 的 `quant_blocked_fp8` 工具在 u7-l4。本讲只讲「怎么用」，u7 讲「怎么造」。
