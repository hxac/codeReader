# TransformerConfig 与预置架构工厂方法

## 1. 本讲目标

上一讲（u3-l1）我们读了 `Transformer` 与 `TransformerBlock` 的前向主链路，知道了数据如何从 `input_ids` 一路流到 logits。本讲把视角拉回到「模型是怎么被描述出来的」：

1. 读懂 `TransformerConfig` 中每个字段对应的模型结构含义——看完你就知道一张「模型图纸」上到底画了哪些信息。
2. 会用 `olmo2_190M()`、`olmo3_7B()` 这类预置工厂方法快速构建模型配置，并说清楚 olmo2 与 olmo3 预设的差别。
3. 掌握贯穿全库的复用模式：**Config 工厂方法 + kwargs 覆盖**——一行调用既能选定规模档位，又能随手改掉任何深层参数。
4. 理解 `build()` 如何从图纸初始化出一个真实模型，以及 `num_params` 如何在建模之前就精确算出参数量。

## 2. 前置知识

本讲假设你已读过：

- **u2-l1 / u2-l2**：`Config` 是可序列化的嵌套 dataclass，`ModuleConfig` 提供抽象的 `build()`，`ModelConfig` 再加上 `num_params` 等属性。`TransformerConfig` 正是这条继承链的成员。
- **u3-l1**：`Transformer` 顶层只做编排，`n_layers` 个 `TransformerBlock` 堆叠在残差流上；OLMo 2 默认 block 是 `reordered_norm`（norm 在子层输出侧）。

几个本讲反复出现的术语，先用一句话解释：

| 术语 | 含义 |
| --- | --- |
| `d_model` | 残差流宽度（隐藏维度）。中间所有层都保持这个宽度，形状只在 embedding 入口和 lm_head 出口变化。 |
| `n_heads` / `n_kv_heads` | 查询头数 / 键值头数。`n_kv_heads < n_heads` 时就是 **GQA**（分组查询注意力，多组 Q 共享一组 KV，省 KV cache 与投影参数）；`n_kv_heads` 留空（`None`）表示等于 `n_heads`，即标准 MHA。 |
| `head_dim` | 每个注意力头的维度。留空时在构建期推导为 `d_model // n_heads`（见 [src/olmo_core/nn/attention/__init__.py:L700-L704](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/attention/__init__.py#L700-L704)）。部分模型（如 Qwen3）会显式指定与 `d_model // n_heads` 不同的 `head_dim`。 |
| FFN hidden size | 前馈网络（SwiGLU）中间层宽度。`llama_like` 里由 `d_model` 乘系数推导（见 4.3.3）。 |
| `reordered_norm` | OLMo 2 的 block 布局：两个 LayerNorm 放在注意力/FFN 的输出侧（post-norm 风格）。 |
| RoPE `theta` | 旋转位置编码的基频，越大越适合长上下文（OLMo 2/3 用 500 000，Qwen3 用 1 000 000）。 |
| 参数量预算 | `num_params`：不实例化模型，仅凭 Config 算出的精确参数总数。 |

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/olmo_core/nn/transformer/config.py](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/config.py) | **本讲主角**。`TransformerConfig`、`TransformerBlockConfig`、全部预置工厂方法、block 解析逻辑都在这一个文件里（约 2200 行）。 |
| [src/olmo_core/nn/config.py](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/config.py) | `ModuleConfig` / `ModelConfig` 抽象基类，定义 `build()` 与 `num_params` 契约。 |
| [src/olmo_core/nn/transformer/init.py](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/init.py) | `InitMethod` 枚举：权重初始化方案（详细机制在 u3-l6 展开）。 |
| [src/olmo_core/nn/attention/__init__.py](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/attention/__init__.py) | `AttentionConfig`、`SlidingWindowAttentionConfig` 等被 `TransformerConfig` 组合的子配置。 |
| [src/olmo_core/utils.py](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/utils.py) | `ensure_multiple_of`：FFN 宽度对齐工具。 |
| [src/test/nn/transformer/model_test.py](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/test/nn/transformer/model_test.py) | 断言 `config.num_params` 与实际构建参数量严格相等；展示 kwargs 覆盖用法。 |
| [src/test/nn/transformer/config_test.py](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/test/nn/transformer/config_test.py) | 配置加载、round-trip 与工厂方法的回归测试。 |

## 4. 核心概念与源码讲解

### 4.1 TransformerConfig：一张描述模型全部形状的图纸

#### 4.1.1 概念说明

u3-l1 里我们直接读了 `Transformer` 类的 `__init__` 参数——那有十几个位置参数，手工拼装既繁琐又容易错。`TransformerConfig` 解决的就是这个问题：把「造一个 Transformer 需要知道的一切」收进一个可序列化、可校验、可复用的 dataclass。

它的设计要点：

1. **复合配置**：`TransformerConfig` 不重复定义注意力、FFN、LayerNorm 的细节，而是嵌套持有 `TransformerBlockConfig`、`LMHeadConfig` 等子配置——这正是 u2-l1 讲过的「Config 嵌套组合」模式。
2. **顶层管形状，block 管内部**：`d_model`、`n_layers` 这类全局量在顶层；每个 block 内部用什么注意力、什么 FFN 由 `TransformerBlockConfig` 描述。
3. **构造即校验**：`__post_init__` 在对象诞生时就拒绝非法组合（比如 `tie_word_embeddings` 搭配 normalized transformer）。

#### 4.1.2 核心流程

`TransformerConfig` 的字段可以分成四组来记：

```text
┌─ 形状骨架 ─────────────────────────────────────────────┐
│  d_model / vocab_size / n_layers                        │
├─ block 的组织方式 ──────────────────────────────────────┤
│  block（单个 或 dict[str, 多个命名 block]）              │
│  block_pattern（命名 block 的循环模式）                   │
│  block_overrides（按层号覆盖单 block）                    │
├─ 初始化 ────────────────────────────────────────────────┤
│  init_method / init_seed / init_std / embedding_init_std │
│  dtype / freeze_params                                  │
├─ 词表与输出 ────────────────────────────────────────────┤
│  lm_head / embedding_norm / embed_scale                 │
│  tie_word_embeddings / n_extra_vocab                    │
└─────────────────────────────────────────────────────────┘
```

构造流程：`TransformerConfig(...)` → dataclass 赋值 → `__post_init__` 校验 → 得到一个可 `build()`、可 `as_config_dict()` 序列化的纯数据对象。

#### 4.1.3 源码精读

**① 继承链与字段定义。** `TransformerConfig` 继承 `ModelConfig`（[src/olmo_core/nn/transformer/config.py:L335-L362](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/config.py#L335-L362)）：前五个字段 `d_model`、`vocab_size`、`n_layers`、`block`、`lm_head` 没有默认值，是必填项；其余都有默认值。注意 `block` 字段的类型是联合类型 `TransformerBlockConfig | dict[str, TransformerBlockConfig]`——这正是 4.2 节「block 三种组织方式」的入口。`name: TransformerType = TransformerType.default` 决定顶层用哪个实现类（default → `Transformer`，normalized → nGPT，moe → `MoETransformer`，见 [src/olmo_core/nn/transformer/config.py:L80-L103](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/config.py#L80-L103)）。

**② `__post_init__` 校验。** [src/olmo_core/nn/transformer/config.py:L372-L397](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/config.py#L372-L397) 依次检查：normalized transformer 不许 tie 词嵌入；`n_extra_vocab` 非负且只支持 default 类型；然后调用 `validate_block_resolution_config` 确保 `block` / `block_pattern` / `block_overrides` 三者组合合法（细节见 4.2.3）。若 `n_layers` 不能被 `block_pattern` 长度整除，只发 warning 不报错——模式会循环后截断。

**③ `TransformerBlockConfig` 的字段。** [src/olmo_core/nn/transformer/config.py:L162-L212](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/config.py#L162-L212)：

- `sequence_mixer`：序列混合器配置（通常是 `AttentionConfig`，也可以是 `GatedDeltaNetConfig` 等线性注意力——Qwen3.5 预设就是这么用的）。它是唯一必填字段。
- `attention`：已废弃的 `InitVar`，仅为兼容旧配置保留；`__post_init__` 里会把它搬到 `sequence_mixer`（[L214-L226](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/config.py#L214-L226)）。
- `layer_norm` / `feed_forward` / `feed_forward_moe`：dense block 需要 `feed_forward`，MoE block 需要 `feed_forward_moe`，hybrid MoE 则两者都有。
- `name: TransformerBlockType`：选 block 布局（default / reordered_norm / peri_norm / moe 系列等，见 [src/olmo_core/nn/transformer/config.py:L106-L159](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/config.py#L106-L159)）。
- `dropout` / `masked_dropout` / `attention_residual_alpha` / `feed_forward_residual_alpha`：正则与残差缩放的可选项。

**④ 基类契约。** `ModuleConfig` 只要求一个抽象 `build()`（[src/olmo_core/nn/config.py:L9-L16](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/config.py#L9-L16)）；`ModelConfig` 再要求 `num_params`、`num_non_embedding_params` 两个抽象属性（[src/olmo_core/nn/config.py:L19-L43](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/config.py#L19-L43)）。`TransformerConfig` 把这些全部实现了。

#### 4.1.4 代码实践

**实践目标**：不看工厂方法，手写一个最小的合法 `TransformerConfig`，并亲身体验一次校验报错。

**操作步骤**（以下为示例代码，可在仓库根目录用 `python -I` 逐段运行）：

```python
# 手写图纸（示例代码）
from olmo_core.nn.attention import AttentionConfig
from olmo_core.nn.feed_forward import FeedForwardConfig
from olmo_core.nn.layer_norm import LayerNormConfig
from olmo_core.nn.lm_head import LMHeadConfig
from olmo_core.nn.rope import RoPEConfig
from olmo_core.nn.transformer.config import (
    TransformerBlockConfig, TransformerBlockType, TransformerConfig,
)

d_model = 64
block = TransformerBlockConfig(
    name=TransformerBlockType.reordered_norm,
    sequence_mixer=AttentionConfig(
        n_heads=4, bias=False, rope=RoPEConfig(theta=500_000),
    ),
    feed_forward=FeedForwardConfig(hidden_size=176, bias=False),
    layer_norm=LayerNormConfig(bias=False),
)
cfg = TransformerConfig(
    d_model=d_model, vocab_size=128, n_layers=2, block=block,
    lm_head=LMHeadConfig(bias=False),
)
print(cfg.num_params)
```

再故意触发一个校验错误（示例代码）：

```python
import pytest  # 或直接用 try/except 捕获 OLMoConfigurationError
from olmo_core.exceptions import OLMoConfigurationError

try:
    TransformerConfig(
        d_model=64, vocab_size=128, n_layers=2, block=block,
        lm_head=LMHeadConfig(bias=False),
        block_pattern=["a"],        # 非法：单 block 不允许配 pattern
    )
except OLMoConfigurationError as e:
    print("捕获到预期错误:", e)
```

**需要观察的现象**：第二段代码抛出的错误信息会告诉你 `block_pattern` 只在 `block` 为 dict 时可用（对应 [src/olmo_core/nn/transformer/config.py:L2150-L2155](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/config.py#L2150-L2155) 的校验分支）。

**预期结果**：第一段打印出一个整数参数量（数值待本地验证）；第二段打印「捕获到预期错误」。手写图纸繁琐正是下一模块工厂方法存在的理由。

#### 4.1.5 小练习与答案

**练习 1**：`TransformerConfig` 有约 18 个字段，为什么只有 5 个必填？
**答案**：`d_model`、`vocab_size`、`n_layers`、`block`、`lm_head` 定义了模型的最小形状骨架，库无法替你猜；其余字段（dtype、init 系列、tie_word_embeddings 等）都有合理默认值，属于「可选微调」。

**练习 2**：`TransformerBlockConfig.sequence_mixer` 为什么不叫 `attention`？
**答案**：因为序列混合器是一个可替换插槽——大多数预设放 `AttentionConfig`，但 Qwen3.5 等混合架构会放 `GatedDeltaNetConfig`（线性注意力）。旧名 `attention` 保留为废弃的 `InitVar` 仅为加载旧配置（见 [src/olmo_core/nn/transformer/config.py:L172-L177](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/config.py#L172-L177)）。

**练习 3**：`n_extra_vocab` 字段是给谁用的？
**答案**：给 Molmo2 多模态模型——把 128 个图像专用 token 作为**独立参数**追加到 embedding 表后面；这些 token 只作为输入、永远不是预测目标（字段文档见 [src/olmo_core/nn/transformer/config.py:L362-L370](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/config.py#L362-L370)）。

### 4.2 从图纸到模型：block 解析、`build()` 与参数量预算

#### 4.2.1 概念说明

图纸画好后有两个问题：`block` 字段允许「单 block」「命名 block + 模式」「单 block + 逐层覆盖」三种写法，`build()` 前要先统一成「每层一个 `TransformerBlockConfig`」的列表；此外，我们希望**不实例化模型就能知道参数量**——这对实验规划（显存预算、优化器状态估算）非常重要。

这两个需求分别由 `resolve_block_configs()` 与 `num_params` 属性满足。`num_params` 是纯算术：各子配置都实现了自己的 `num_params(d_model)`，顶层把它们加总。测试保证了它与真实参数量分毫不差。

#### 4.2.2 核心流程

**block 解析**（伪代码）：

```text
resolve_block_configs(n_layers, block, block_pattern, block_overrides):
    若 block 是 dict（命名 block）:
        pattern 按 cycle 循环，截取前 n_layers 项
        返回 [block[name] for name in 截取后的 pattern]
    否则（单 block）:
        结果 = [block] * n_layers
        对 block_overrides 中的每个 (层号, 覆盖配置): 结果[层号] = 覆盖配置
        返回 结果
```

**`build()` 流程**：

1. 先打印参数量预算日志；
2. 按 `self.name` 分发到 `Transformer` / `NormalizedTransformer` / `MoETransformer` 构造函数（`moe_fused_v2` 直接抛 `RuntimeError`，要求改用 `OLMoDDPModelConfig`）；
3. 若设置了 `freeze_params`，用 `fnmatch` 逐个参数名匹配并置 `requires_grad=False`；
4. 打印模型摘要，返回模型。

**参数量公式**（对应 `num_params` 属性的求和顺序）：

\[
N_{\text{total}} = \underbrace{d \cdot V}_{\text{embedding}} + \underbrace{N_{\text{norm}}^{emb}}_{\text{embedding norm}} + \sum_{i=1}^{L} N_{\text{block}}^{(i)}(d) + \underbrace{N_{\text{lm\_head}}(d, V)}_{\text{输出头}} - \mathbb{1}_{\text{tied}} \cdot d \cdot V
\]

其中 \(d\) 为 `d_model`，\(V\) 为 `vocab_size`，\(L\) 为 `n_layers`；最后一项表示 tie 词嵌入时 lm_head 与 embedding 共享权重，要减去一份。而**非嵌入参数量**就是：

\[
N_{\text{non-emb}} = N_{\text{total}} - d \cdot V
\]

#### 4.2.3 源码精读

**① block 解析。** [src/olmo_core/nn/transformer/config.py:L2176-L2206](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/config.py#L2176-L2206) 是 `resolve_block_configs`：dict 分支用 `islice(cycle(block_pattern), n_layers)` 把模式循环截断到 `n_layers` 长；单 block 分支先铺满再应用 `block_overrides`。配套的 `validate_block_resolution_config`（[L2144-L2173](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/config.py#L2144-L2173)）负责拒绝非法组合：dict 必须配 pattern、dict 不许再用 overrides、pattern 里的名字必须都在 dict 中。顶层属性 `resolved_block_configs`（[L497-L504](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/config.py#L497-L504)）就是对这个函数的封装。

**② `build()`。** [src/olmo_core/nn/transformer/config.py:L399-L495](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/config.py#L399-L495)。注意两个细节：其一，`init_device` 参数（[L399-L409](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/config.py#L399-L409)）默认 `"cpu"`，分布式训练时通常传 `"meta"`——这就是 u1-l4 说过的「模型先在 meta 设备占位，再由并行策略落位」；其二，`freeze_params` 的 fnmatch 匹配循环（[L477-L485](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/config.py#L477-L485)）支持 `*"wq"` 这类通配符按参数名冻结。

**③ 参数量预算。** [src/olmo_core/nn/transformer/config.py:L506-L529](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/config.py#L506-L529) 严格按上面公式求和；`num_non_embedding_params`（[L556-L561](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/config.py#L556-L561)）就是简单相减。block 一侧的 `TransformerBlockConfig.num_params(d_model)`（[L295-L322](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/config.py#L295-L322)）把序列混合器、两个 norm、FFN/MoE 逐项相加——标准 block 恰好计两次 `layer_norm`（注意力一次、FFN 一次），与 reordered_norm 布局里每 block 两个 norm 对应。MoE 场景还有 `num_active_params`（每 token 实际激活的参数量，[L531-L554](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/config.py#L531-L554)），这是评估 MoE 推理/训练成本时更贴心的指标。

**④ 测试证据。** `test_small_llama2_builder_config`（[src/test/nn/transformer/model_test.py:L82-L105](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/test/nn/transformer/model_test.py#L82-L105)）构建 `llama2_271M` 后逐参数 `numel()` 求和，并断言 `config.num_params == num_actual_params == model.num_params` 三者相等——预算不是估算，是精确值。

#### 4.2.4 代码实践

**实践目标**：验证「参数量预算 = 真实参数量」，并体会 `block_overrides` 的用法。

**操作步骤**（示例代码）：

```python
from olmo_core.nn.transformer.config import TransformerConfig

cfg = TransformerConfig.olmo2_14M(vocab_size=1_000)
print(f"预算: total={cfg.num_params:,}  non-emb={cfg.num_non_embedding_params:,}")

model = cfg.build()  # CPU 构建
actual = sum(p.numel() for p in model.parameters())
print(f"实际: {actual:,}  相等: {cfg.num_params == actual}")

# block_overrides：把第 0 层换成带 dropout 的副本
block0 = cfg.block.copy()
block0.dropout = 0.1
cfg2 = TransformerConfig.olmo2_14M(vocab_size=1_000, block_overrides={0: block0})
print(f"覆盖后层数不变: {len(cfg2.resolved_block_configs)}")
print(f"第 0 层 dropout={cfg2.resolved_block_configs[0].dropout}, "
      f"第 1 层 dropout={cfg2.resolved_block_configs[1].dropout}")
```

**需要观察的现象**：预算与实际参数量相等；`resolved_block_configs` 长度等于 `n_layers`，且只有第 0 层的 dropout 被改成 0.1，其余层仍是 `None`。

**预期结果**：两行 `相等: True`、`dropout=0.1` 与 `dropout=None`。参数量数值待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `tie_word_embeddings=True` 时 `num_params` 要减去 `d_model * vocab_size`？
**答案**：tie 意味着 lm_head 与 embedding 共享同一份权重张量，`named_parameters()` 里只会出现一次；求和时却分别计入了 embedding 和 lm_head 两份，必须扣除一份（[src/olmo_core/nn/transformer/config.py:L526-L527](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/config.py#L526-L527)）。

**练习 2**：`resolved_block_configs` 是普通字段还是 property？为什么？
**答案**：是 property（[L497-L504](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/config.py#L497-L504)）。因为它依赖 `n_layers`/`block`/`block_pattern`/`block_overrides` 四个字段的组合结果，若存成字段，任何一个字段被修改后都可能过期；property 每次现算，永远一致。

**练习 3**：`build()` 里 `name=TransformerType.moe_fused_v2` 会发生什么？
**答案**：抛出 `RuntimeError("Use OLMoDDPModelConfig")`（[L472-L473](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/config.py#L472-L473)）。融合 MoE-v2 模型要用 `TransformerConfig` 的子类 `OLMoDDPModelConfig`（[L2052-L2141](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/config.py#L2052-L2141)），它自带重计算、双 batch 重叠等额外开关——这也是「通过子类扩展 Config」的范例。

### 4.3 预置架构工厂方法：olmo2 阶梯与 llama_like 复用模式

#### 4.3.1 概念说明

手写 `TransformerConfig` 太繁琐，而且 OLMo 2、OLMo 3、Llama、Qwen 等架构的正确超参组合本身就值得沉淀。`TransformerConfig` 上挂了三类 `classmethod` 工厂：

1. **规模档位工厂**：`olmo2_14M()`、`olmo2_190M()`、`olmo2_1B()`……名字是规模档位（配 OLMo 词表时的名义参数量级），从 1M 一路到 32B。
2. **家族装配工厂**：`llama_like()`、`ngpt_like()`、`gemma3_like()`、`qwen3_5_like()`——把一组超参翻译成完整的嵌套 Config 树。
3. **Config 级组合子**：`with_rope_scaling()` 这类「拿一个现成配置、返回改造后副本」的方法。

这些工厂共享同一个复用哲学——**工厂方法 + kwargs 覆盖**：每个工厂只 `pop` 自己关心的少数旋钮并给出默认值，把剩余 `**kwargs` 原样向下游传递，最终汇入 `cls(...)`。于是 `olmo2_190M(vocab_size=..., n_layers=2, sliding_window=...)` 一次调用中：`n_layers` 在 190M 这层被吃掉、`sliding_window` 被透传给 `llama_like`——测试代码 [src/test/nn/transformer/model_test.py:L181-L205](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/test/nn/transformer/model_test.py#L181-L205) 的 `get_transformer_config` 就是这种一行式调用的活例子。

#### 4.3.2 核心流程

工厂调用是一座三层金字塔：

```text
TransformerConfig.olmo3_1B(vocab_size, **kwargs)     ← 规模档位（olmo3 = olmo2 + 滑窗 + flash_2）
        │  委托
        ▼
TransformerConfig.olmo2_1B_v2(vocab_size, **kwargs)  ← 规模档位（指定 n_layers=16）
        │  委托
        ▼
TransformerConfig.llama2_1B(vocab_size, **kwargs)    ← 架构预设（d_model=2048 等）
        │  委托
        ▼
TransformerConfig.llama_like(**所有参数)              ← 家族装配：拼出 block / lm_head / 子配置
        │  cls(...)
        ▼
TransformerConfig 实例
```

`llama_like` 内部决定 FFN 宽度的规则是（[src/olmo_core/nn/transformer/config.py:L1723-L1727](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/config.py#L1723-L1727)）：

\[
h_0 = \left\lfloor \frac{8d}{3} \right\rfloor,\qquad
h_1 = \lfloor m \cdot h_0 \rfloor,\qquad
h = k \cdot \left\lceil \frac{h_1}{k} \right\rceil
\]

其中 \(d\) 是 `d_model`，\(m\) 是可选的 `hidden_size_multiplier`，\(k\) 是 `hidden_size_multiple_of`（默认 256，`ensure_multiple_of` 见 [src/olmo_core/utils.py:L903-L904](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/utils.py#L903-L904)）。例：`d=2048, m=1.5` 时 \(h_0=5461\)，\(h_1=8191\)，对齐到 256 的倍数得 \(h=8192\)。

#### 4.3.3 源码精读

**① olmo2 规模阶梯。** 每个档位都是对 `llama_like` 的一次参数化调用。以 `olmo2_190M` 为例（[src/olmo_core/nn/transformer/config.py:L647-L660](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/config.py#L647-L660)）：`d_model=768`、`hidden_size_multiplier=1.5`、`n_layers=12`、`n_heads=12`，并统一 `block_name=reordered_norm`、`qk_norm=True`、`rope_theta=500_000`、`layer_norm_eps=1e-6`。`olmo2_14M`（[L586-L598](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/config.py#L586-L598)）则是 `d_model=128, n_layers=4, n_heads=8`。较大档位改为委托给 llama2 预设：`olmo2_1B` → `llama2_1B`（[L707-L722](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/config.py#L707-L722)，默认 `n_layers=18`）；`olmo2_7B` → `llama2_7B`（[L760-L772](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/config.py#L760-L772)）。`olmo2_32B`（[L788-L807](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/config.py#L788-L807)）值得细看：`d_model=5120, n_layers=64, n_heads=40, n_kv_heads=8`（GQA），且 `hidden_size_multiplier=27648/(8·5120/3)`、`hidden_size_multiple_of=512`——代入公式恰好凑出 FFN 宽度 27648。

**② olmo3 = olmo2 + 滑窗注意力 + flash_2。** 全部 `olmo3_*` 工厂结构一致，以 `olmo3_1B` 为例（[src/olmo_core/nn/transformer/config.py:L978-L996](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/config.py#L978-L996)）：调用对应的 olmo2 工厂（注意是 `olmo2_1B_v2`，默认 `n_layers=16`，而非 `olmo2_1B` 的 18 层！），再追加两样东西——`sliding_window=SlidingWindowAttentionConfig(force_full_attention_on_first_layer=False, force_full_attention_on_last_layer=True, pattern=[4096, 4096, 4096, -1])` 和 `attn_backend=flash_2`。滑窗语义见字段文档（[src/olmo_core/nn/attention/__init__.py:L252-L269](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/attention/__init__.py#L252-L269)）：`-1` 表示全注意力，即每 4 层里前 3 层只看 4096 窗口、第 4 层全局；`_get_window_size`（[L271-L291](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/attention/__init__.py#L271-L291)）还处理首末层的强制规则。对 16 层的 olmo3_1B 推导一遍：首层不强制全注意力、末层（第 15 层）强制，模式从第 0 层开始循环——全注意力层为 3、7、11、15。另注意 `olmo3_600M` 是唯一还改了 `d_model`（1280）的例外（[L944](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/config.py#L944)）。

**③ `llama_like` 装配主体。** [src/olmo_core/nn/transformer/config.py:L1676-L1800](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/config.py#L1676-L1800)：先算 FFN 宽度（L1723-1727），再配全局 LayerNorm（默认 `rms`，`fused_ops=True` 时 `fused_rms`，L1729-1737），然后决定注意力实现（`fused_ops` 且无 GQA 时自动切 `fused_v2` + flash_2 后端，L1739-1747），拼出单个 `TransformerBlockConfig`（L1753-1779），最后 `cls(...)` 落成 `TransformerConfig`（L1791-1800）。中间的 `block_mods` 钩子（L1781-1789）允许按层号传入「改 block 配置的函数」，比 `block_overrides` 更 programmable，两者互斥。

**④ 混合架构与命名 block。** `qwen3_5_like`（[src/olmo_core/nn/transformer/config.py:L1584-L1674](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/config.py#L1584-L1674)）构造两种 block——`gdn`（GatedDeltaNet 线性注意力）与 `attn`（全注意力），以 `block={"gdn": ..., "attn": ...}` + `block_pattern=["gdn","gdn","gdn","attn"]` 的 3:1 模式组装；`gemma3_like`（[L1910-L2003](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/config.py#L1910-L2003)）同样用 `local`/`global` 两种 block 按 5:1 循环，并设置 `embed_scale=sqrt(d_model)`。这就是 `block` 字段允许 dict 的意义。

**⑤ Config 级组合子。** `with_rope_scaling`（[src/olmo_core/nn/transformer/config.py:L2005-L2049](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/config.py#L2005-L2049)）返回配置副本，把 RoPE 缩放只加在全注意力层（滑窗层跳过），实现方式正是批量生成 `block_overrides`——长上下文训练脚本靠它在中期扩展上下文。

**⑥ MoE 预设。** `smallmoe`（[L1078-L1100](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/config.py#L1078-L1100)）、`olmoe_1B_7B`（[L1126-L1147](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/config.py#L1126-L1147)）等在调用 `llama_like` 时直接内联构造 `MoEConfig(num_experts=..., router=MoERouterConfig(top_k=...))`，展示工厂也能组装异构 block 内容。MoE 细节留待 u3-l5。

#### 4.3.4 代码实践（本讲正式实践任务）

**实践目标**：对比 `olmo2_1B` 与 `olmo3_1B` 工厂方法的差异（层数、注意力类型等）；再用 kwargs 把 `olmo2_14M` 改造成自定义小模型，`build()` 后统计参数量。

**操作步骤**：把下面的脚本存为 `OLMo-core-tutorial/preset_lab.py`（示例代码），在仓库根目录运行 `python -I OLMo-core-tutorial/preset_lab.py`（需要已按 u1-l2 完成开发安装）：

```python
# preset_lab.py（示例代码）
from olmo_core.nn.transformer.config import TransformerConfig

VOCAB = 50_304  # 任意演示词表大小

# ── 第 1 步：对比 olmo2_1B 与 olmo3_1B ──
for tag, cfg in [
    ("olmo2_1B", TransformerConfig.olmo2_1B(vocab_size=VOCAB)),
    ("olmo3_1B", TransformerConfig.olmo3_1B(vocab_size=VOCAB)),
]:
    attn = cfg.block.sequence_mixer  # 单 block 预设可直接取
    print(f"[{tag}]")
    print(f"  d_model={cfg.d_model}  n_layers={cfg.n_layers}")
    print(f"  n_heads={attn.n_heads}  n_kv_heads={attn.n_kv_heads}"
          f"  backend={attn.backend}")
    print(f"  ffn_hidden={cfg.block.feed_forward.hidden_size}")
    print(f"  sliding_window={attn.sliding_window}")
    print(f"  num_params={cfg.num_params:,}"
          f"  non_embedding={cfg.num_non_embedding_params:,}")

# ── 第 2 步：kwargs 定制 olmo2_14M ──
custom = TransformerConfig.olmo2_14M(
    vocab_size=VOCAB,
    n_layers=6,  # 4 -> 6
    n_heads=4,   # 8 -> 4（head_dim 在构建期推导为 128 // 4 = 32）
)
attn = custom.block.sequence_mixer
print("[custom olmo2_14M]")
print(f"  d_model={custom.d_model}  n_layers={custom.n_layers}"
      f"  n_heads={attn.n_heads}  ffn_hidden={custom.block.feed_forward.hidden_size}")
print(f"  预算 num_params={custom.num_params:,}")

model = custom.build()  # CPU 上直接构建
actual = sum(p.numel() for p in model.parameters())
print(f"  实际 num_params={actual:,}  一致: {custom.num_params == actual}")
```

**需要观察的现象**与**预期结果**（依据源码可推导的部分）：

- `d_model` 两者都是 2048；**`n_layers` 不同：olmo2_1B 为 18，olmo3_1B 为 16**（前者走 `llama2_1B` 默认，后者走 `olmo2_1B_v2` 默认）。
- 两者 `n_heads=16`、`n_kv_heads=None`（留空表示 MHA，构建期才展开为 16）；FFN 宽度按 4.3.2 的公式都推得 8192。
- **注意力配置是最大差异**：`olmo2_1B` 的 `backend` 为 `None`（构建时自动选择）、`sliding_window=None`（全程全注意力）；`olmo3_1B` 显式 `backend=flash_2`，且 `sliding_window.pattern=[4096, 4096, 4096, -1]`——16 层下全注意力层为 3、7、11、15（推导见 4.3.3 ②）。
- 第 2 步：定制后 `n_layers=6`、`n_heads=4`；`olmo2_14M` 未指定 multiplier，FFN 宽度按 \(h = 256 \cdot \lceil \lfloor 8 \cdot 128 / 3 \rfloor / 256 \rceil = 256 \cdot \lceil 341/256 \rceil = 512\) 推得 512。
- 最后一行应打印 `一致: True`（有 [src/test/nn/transformer/model_test.py:L88-L93](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/test/nn/transformer/model_test.py#L88-L93) 的同类断言背书）。
- 两个配置的具体参数量数值与脚本输出**待本地验证**。

顺带一提：`config_test.py` 中的 `test_load_olmo3_7b_config`（[src/test/nn/transformer/config_test.py:L14-L30](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/test/nn/transformer/config_test.py#L14-L30)）会从真实 OLMo 3 7B checkpoint 下载 `config.json` 并还原成 `TransformerConfig`（`d_model=4096, n_layers=32`）——预置工厂与序列化体系共同保证了「官方模型 ↔ Config」的双向可追溯。

#### 4.3.5 小练习与答案

**练习 1**：`olmo2_1B` 与 `olmo2_1B_v2` 有什么区别？`olmo3_1B` 基于哪一个？
**答案**：两者都委托 `llama2_1B`，但 `olmo2_1B` 用其默认 `n_layers=18`，`olmo2_1B_v2` 显式默认 `n_layers=16` 且对齐旧 OLMo trainer 的 1B（docstring 见 [L724-L731](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/config.py#L724-L731)）。`olmo3_1B` 基于 `olmo2_1B_v2`（[L983](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/config.py#L983)），所以也是 16 层。

**练习 2**：不实例化模型，如何知道 `olmo2_32B` 用了 GQA？FFN 宽度是多少？
**答案**：读工厂方法（[L788-L807](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/config.py#L788-L807)）：`n_heads=40, n_kv_heads=8`，8 < 40 即 GQA。FFN 宽度代入公式：\(h_0=\lfloor 8 \cdot 5120/3 \rfloor=13653\)，乘 \(27648/13653.33\approx 2.025\) 后取整得 27647，对齐到 512 的倍数得 **27648**——multiplier 正是反着凑出来的。

**练习 3**：想给一个 olmo3 预设换掉注意力后端，该怎么做？
**答案**：工厂的 `attn_backend` 旋钮本身就是 kwargs 可覆盖的，例如 `TransformerConfig.olmo3_190M(vocab_size=..., attn_backend=AttentionBackendName.flash_3)`——每个 olmo3 工厂都用 `kwargs.pop("attn_backend", AttentionBackendName.flash_2)` 接收它（如 [L912](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/config.py#L912)）。

## 5. 综合实践

把本讲三块知识串成一次「预设巡检 + 定制 + 序列化」的小任务。在 `OLMo-core-tutorial/` 下新建 `presets_tour.py`（示例代码）：

```python
# presets_tour.py（示例代码）
import json

from olmo_core.nn.attention import SlidingWindowAttentionConfig
from olmo_core.nn.transformer.config import TransformerConfig

VOCAB = 50_304

# 1) 巡检：把 olmo2 全阶梯的关键超参整理成表
sizes = ["olmo2_1M", "olmo2_14M", "olmo2_100M", "olmo2_190M", "olmo2_1B_v2"]
print(f"{'preset':<14}{'d_model':>8}{'layers':>7}{'heads':>6}{'ffn':>7}{'params':>14}")
for name in sizes:
    cfg = getattr(TransformerConfig, name)(vocab_size=VOCAB)
    print(f"{name:<14}{cfg.d_model:>8}{cfg.n_layers:>7}"
          f"{cfg.block.sequence_mixer.n_heads:>6}"
          f"{cfg.block.feed_forward.hidden_size:>7}{cfg.num_params:>14,}")

# 2) 定制：在 olmo2_190M 基础上加深 + 换滑窗模式
cfg = TransformerConfig.olmo2_190M(
    vocab_size=VOCAB,
    n_layers=16,
    sliding_window=SlidingWindowAttentionConfig(
        pattern=[2048, -1],                       # 1:1 交替：滑窗 / 全注意力
        force_full_attention_on_first_layer=True,
        force_full_attention_on_last_layer=True,
    ),
)

# 3) 序列化往返：定制结果应能无损存取（呼应 u2-l1 的 round-trip）
d = cfg.as_config_dict()
print(json.dumps(d, indent=2)[:400], "...")  # 检查 _CLASS_/type 线索与滑窗字段
cfg2 = TransformerConfig.from_dict(json.loads(json.dumps(d)))
print("round-trip 一致:", cfg2.as_config_dict() == d)

# 4) 参数量对账
model = cfg.build()
print("num_params 一致:", cfg.num_params == sum(p.numel() for p in model.parameters()))
```

任务要求：

1. 运行脚本，记录步骤 1 的表格——观察 d_model / n_layers / FFN 宽度如何随档位增长（各档 FFN 宽度可按 4.3.2 公式手工推导核对）。
2. 解释步骤 2 中 `pattern=[2048, -1]` 在 16 层下哪些层是全注意力（提示：首末层被强制全注意力，模式从第 1 层起循环——具体层数组合待本地验证，可与 `_get_window_size` 的推导对照）。
3. 确认步骤 3、4 都打印 `True`。若第 3 步为 `False`，说明序列化丢了信息，回头检查是不是传了不可 JSON 化的对象。

预期：四步全部成功即完成本任务；具体数值输出待本地验证。

## 6. 本讲小结

- `TransformerConfig` 是模型全部形状的图纸：5 个必填字段（`d_model`/`vocab_size`/`n_layers`/`block`/`lm_head`）定骨架，其余字段管 block 组织、初始化、词表与输出；构造时 `__post_init__` 即校验非法组合。
- `block` 有三种组织方式：单 block、命名 block + `block_pattern` 循环（混合架构用）、单 block + `block_overrides` 逐层覆盖；`resolved_block_configs` 把它们统一成「每层一个配置」的列表。
- `build()` 按 `name` 分发到 `Transformer`/`NormalizedTransformer`/`MoETransformer`，支持 `init_device="meta"` 占位与 `freeze_params` 通配冻结；`num_params` 是不实例化就精确成立的参数量预算（有测试断言背书）。
- 预置工厂是一座三层金字塔：规模档位（olmo2_1M…olmo2_32B、olmo3 系列）→ 架构预设（llama2/gemma3/qwen3…）→ 家族装配（`llama_like` 等）；**kwargs.pop + 向下透传**让一行调用既能选档位又能改任何深层参数。
- olmo3 预设 = 对应 olmo2 预设 + 滑窗模式 `[4096,4096,4096,-1]` + flash_2 后端；`olmo3_1B` 特例：基于 `olmo2_1B_v2`（16 层而非 18 层）。
- `with_rope_scaling` 展示了 Config 级组合子风格：返回改造后的副本，用 `block_overrides` 只放大全注意力层的 RoPE。

## 7. 下一步学习建议

- **下一讲 u3-l3（注意力机制与可插拔后端）**：本讲多次遇到 `attn_backend`、`sliding_window`、`qk_norm`，下一讲深入 `AttentionConfig` 背后的 `Attention` 基类与各后端实现。
- **u3-l4（核心组件）**：`FeedForwardConfig.hidden_size` 的宽度如何变成 SwiGLU 三矩阵、`LMHeadConfig` 与 `LayerNormConfig` 的字段含义。
- **u3-l6（模型初始化）**：本讲只点了 `init_method`/`init_std` 枚举，初始化方案如何按参数类型生效留到那讲展开。
- **延伸阅读源码**：通读 [src/olmo_core/nn/transformer/config.py](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/config.py) 中你还没读过的工厂（`gemma3_like`、`llama_like_moe`），对照本文的三层金字塔模型检验你的理解。
