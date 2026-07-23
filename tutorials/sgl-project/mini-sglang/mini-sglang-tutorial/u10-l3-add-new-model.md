# 支持新模型架构

## 1. 本讲目标

学完本讲后，你应该能够：

1. 说清 `_MODEL_REGISTRY` + `get_model_class` 这套「字符串架构名 → 模型类」的动态导入机制，以及一个新模型是如何被系统「按名字找上门」的。
2. 列出接入一个新 decoder 模型时 `ModelConfig` 必须提供的字段，并判断新模型是走 dense 还是 MoE、是否需要扩展 `from_hf`。
3. 解释为什么 `llama.py` / `qwen2.py` / `qwen3.py` 三个模型文件几乎长得一模一样——它们只是在复用 `RopeAttn` / `GatedMLP`，靠 `has_qk_norm` / `has_attn_bias` 几个开关产生差异。
4. 把一个新模型的 HF 权重 key 列表，对到 `load_weight` 的 `_shard_tensor` / `_MERGE_GROUPS` / `_get_expert_stack_info` 三套规则上，判断哪些 key 能直接加载、哪些需要改命名。

本讲是模型实现单元（u8 / u9）的收尾与综合应用篇。u8-l1 讲了 `BaseOP` 骨架与 Llama 层结构，u8-l2 讲了 `from_hf` 配置翻译与权重切分/合并，u9-l1/u9-l2 讲了 TP Linear 与 embedding/norm/rope/attention 各层。本讲把这几块拼成一个可操作的问题：**给一个本项目还没支持的模型，要改哪些文件、改哪几行，才能让它跑起来？**

## 2. 前置知识

- **架构名（architectures）**：HuggingFace `config.json` 里的 `architectures` 字段是一个字符串列表（如 `["Qwen3ForCausalLM"]`），标注这个 ckpt 对应的模型类名。Mini-SGLang 用它的第 0 个元素作为「找人」的钥匙。
- **BaseOP 体系**（u8-l1）：Mini-SGLang 不用 `nn.Module`，自造 `BaseOP`，靠遍历 `self.__dict__` 递归收集权重；属性名直接决定 `state_dict()` 里的 key（如 `self.model.layers.0.self_attn.qkv_proj`）。
- **形状契约**（u8-l2）：权重加载器 `load_weight` 给出的每个张量的 local 形状，必须等于运行时对应 `Linear` 算出的 local 形状，否则 `load_state_dict` 报错。
- **RopeAttn / GatedMLP**（u8-l1、u9-l2）：标准 decoder 层的注意力与 MLP 实现，是所有 dense 模型共用的积木。
- **MoE 判定**（u8-l2）：`ModelConfig.is_moe` 只看 `"moe" in model_type`；它同时决定 Engine 是否创建 `moe_backend`、`load_weight` 是否走专家堆叠分支。

## 3. 本讲源码地图

本讲围绕「接入新模型」这条主线，串联 5 个文件。它们恰好对应接入工作的四个着力点：

| 文件 | 作用 | 接入时要不要动 |
| --- | --- | --- |
| [python/minisgl/models/register.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/register.py) | `_MODEL_REGISTRY` 注册表与 `get_model_class` 动态导入。 | **必改**：加一行注册项。 |
| [python/minisgl/models/config.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/config.py) | `ModelConfig` / `RotaryConfig` 与 `from_hf` 翻译桥。 | 多数情况不改；非标字段才扩展 `from_hf`。 |
| [python/minisgl/models/llama.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/llama.py) | Llama 模型实现，复用 `utils.RopeAttn`/`GatedMLP`。 | **新增**：照此抄一个新模型文件。 |
| [python/minisgl/models/qwen3.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/qwen3.py) | Qwen3 模型实现，与 Llama 几乎逐行相同，仅 `has_qk_norm=True`。 | 当模板对照差异点。 |
| [python/minisgl/models/weight.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/weight.py) | 流式权重加载器与切分/合并/堆叠规则。 | 多数情况不改；非标权重命名才扩展规则表。 |

辅助文件（了解即可，接入通常不动）：

| 文件 | 作用 |
| --- | --- |
| [python/minisgl/models/utils.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/utils.py) | `GatedMLP` / `MoEMLP` / `RopeAttn` 三个可复用层；接入新 dense 模型主要靠它们。 |
| [python/minisgl/models/base.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/base.py) | `BaseLLMModel`：只强制一个无参 `forward()` 抽象方法。 |
| [python/minisgl/models/__init__.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/__init__.py) | `create_model` 工厂：读 `architectures[0]` 调 `get_model_class`。 |

调用链（自顶向下）：Engine 在 [engine.py:51](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/engine.py#L51) 用 `create_model(config.model_config)` 建图 → `create_model` 在 [models/__init__.py:7-8](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/__init__.py#L7-L8) 取 `architectures[0]` 调 `get_model_class` → `get_model_class` 查注册表动态导入对应文件里的类。本讲讲的就是这条链上「新模型怎么被接进来」。

## 4. 核心概念与源码讲解

### 4.1 模型注册表与动态导入

#### 4.1.1 概念说明

Mini-SGLang 支持好几个模型家族（Llama / Qwen2 / Qwen3 / Qwen3-MoE / Mistral），但 Engine 的建图代码只有一行 `create_model(config.model_config)`，对任何模型都一样。怎么做到的？靠**注册表 + 动态导入**：

- 一张字典 `_MODEL_REGISTRY` 把「架构名」映射到「(模块路径, 类名)」。
- `get_model_class` 拿到架构名后，用 `importlib.import_module` 按需导入对应模块文件，再 `getattr` 取出类并实例化。

好处是：Engine 与各模型实现彻底解耦。新增一个模型，Engine 一行都不用改，只在注册表加一行、再放一个模型文件即可。这是「开闭原则」在源码里的直接体现——对扩展开放（加注册项），对修改关闭（不动 Engine）。

#### 4.1.2 核心流程

```
Engine 建图:
    create_model(model_config)
        -> 架构名 = model_config.architectures[0]      # 如 "Qwen3ForCausalLM"
        -> get_model_class(架构名, model_config)
            if 架构名 not in _MODEL_REGISTRY:
                raise ValueError("not supported")
            module_path, class_name = _MODEL_REGISTRY[架构名]   # 如 (".qwen3", "Qwen3ForCausalLM")
            module = importlib.import_module(module_path, package="minisgl.models")
            model_cls = getattr(module, class_name)
            return model_cls(model_config)              # 实例化，传入 ModelConfig
```

注意三个细节：

1. 钥匙是 `architectures[0]`，它来自 HF `config.json`，经 `from_hf` 原样保留进 `ModelConfig.architectures`（见 [config.py:57](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/config.py#L57)）。所以**新模型的架构名必须与 HF ckpt 里写的完全一致**，否则查不到。
2. 模块路径用「相对导入」`.qwen3`，配 `package=__package__`（即 `minisgl.models`），所以新模型文件要放在 `python/minisgl/models/` 目录下。
3. 一个架构名只对应一个类，但**多个架构名可以指向同一个类**——见 Mistral3 的多模态外壳被映射到 `MistralForCausalLM`。

#### 4.1.3 源码精读

注册表本体——每条记录是「架构名 → (模块相对路径, 类名)」：

[python/minisgl/models/register.py:5-12](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/register.py#L5-L12) —— 五个家族六个架构名。注意 `Mistral3ForConditionalGeneration` 与 `MistralForCausalLM` 都指向 `(".mistral", "MistralForCausalLM")`：多模态 Mistral3 的语言部分与纯文本 Mistral 共用同一个实现类。

动态导入与实例化——`importlib.import_module` 按需加载模块，避免一次性 import 所有重型 CUDA 依赖：

[python/minisgl/models/register.py:15-21](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/register.py#L15-L21) —— `get_model_class` 全文。未注册的架构名在 [register.py:16-17](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/register.py#L16-L17) 直接抛 `ValueError`，这是接入新模型时最常遇到的报错来源。

工厂函数 `create_model`——把「取架构名」与「找类」这两步粘起来：

[python/minisgl/models/__init__.py:7-8](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/__init__.py#L7-L8) —— `get_model_class(model_config.architectures[0], model_config)`。注意它只读第 0 个架构名，HF ckpt 列表里后面的会被忽略。

Engine 侧的唯一调用点——无论什么模型，建图都走这一行：

[python/minisgl/engine/engine.py:51](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/engine.py#L51) —— 在 `torch.device("meta")` 上下文里 `create_model(config.model_config)`，先搭零显存骨架，随后 [engine.py:52](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/engine.py#L52) `load_state_dict` 填真实权重（详见 u5-l1、u8-l1）。

#### 4.1.4 代码实践

**实践目标**：亲手追踪「架构名 → 模型类」的查找过程，并复现「未注册」报错。

**操作步骤**（纯 CPU，无需 GPU）：

1. 写一段示例脚本（**示例代码**，非项目原有），打印注册表与某个 ckpt 的架构名：

   ```python
   # 示例代码
   from transformers import AutoConfig
   from minisgl.models.register import _MODEL_REGISTRY, get_model_class
   from minisgl.models.config import ModelConfig

   print("已注册架构:", list(_MODEL_REGISTRY.keys()))

   cfg = AutoConfig.from_pretrained("Qwen/Qwen3-0.6B", trust_remote_code=True)
   mc = ModelConfig.from_hf(cfg)
   print("该 ckpt 的 architectures:", mc.architectures)
   print("命中的实现类:", _MODEL_REGISTRY[mc.architectures[0]])

   # 复现未注册报错
   try:
       get_model_class("FakeModelForCausalLM", mc)
   except ValueError as e:
       print("预期报错:", e)
   ```

2. 把模型名换成 `meta-llama/Llama-3.2-1B`，观察 `architectures` 变成 `["LlamaForCausalLM"]`，命中注册表第 1 条。

**需要观察的现象**：不同 ckpt 的 `architectures[0]` 不同，但都能在 `_MODEL_REGISTRY` 里查到 `(模块, 类名)`；未注册的名字会抛 `ValueError: ... not supported`。

**预期结果**：Qwen3-0.6B 命中 `(".qwen3", "Qwen3ForCausalLM")`，Llama-3.2 命中 `(".llama", "LlamaForCausalLM")`。若环境无法联网下载 ckpt，**待本地验证**；也可手动构造一个带 `architectures=["LlamaForCausalLM"]` 的假 config 验证查找逻辑。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `get_model_class` 用 `importlib.import_module` 动态导入，而不是在 register.py 顶部直接 `from .qwen3 import Qwen3ForCausalLM`？

**参考答案**：静态 import 会在 `import minisgl.models.register` 时把所有模型文件（及其依赖的 `torch`、`sgl_kernel`、`flashinfer` 等重型库）一次性全加载，拖慢启动、也增加无关节点的依赖。动态导入做到「用到哪个模型才加载哪个文件」，注册表本身只存两个字符串，极轻量。这也让注册表可以在没有 GPU/没有这些库的机器上被 `import` 而不报错。

**练习 2**：如果 HF ckpt 的 `config.json` 里 `architectures` 写成 `["MyAwesomeModel", "SomethingElse"]`，注册表里有 `MyAwesomeModel`，会发生什么？

**参考答案**：正常工作。[models/__init__.py:8](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/__init__.py#L8) 只取 `architectures[0]`，即 `MyAwesomeModel`，后面那个被忽略。但反过来，若 `MyAwesomeModel` 排在第二位、第一位是未注册的名字，就会报 `not supported`——这也是为什么 HF 通常把主架构名放第一个。

### 4.2 ModelConfig：新模型需要哪些字段

#### 4.2.1 概念说明

模型层代码（如 `Qwen3ForCausalLM`）不直接读 HF config，而是读 Mini-SGLang 自己的 `ModelConfig`。`from_hf` 把异构 HF 配置翻译成这套统一字段（u8-l2 已讲翻译细节）。对「接入新模型」而言，关键问题是：

> 我的新模型，`from_hf` 能不能直接吃下它的 HF config？哪些字段可能对不上？

好消息是：`from_hf` 用大量 `getattr(config, 名字, 默认值)` 兜底，**只要新模型是「Llama 系」标准 decoder 结构**（embed→decoder layers→norm→lm_head，RMSNorm + RoPE + GQA + SwiGLU/GLU MLP），几乎不用动 `from_hf`。需要动 `from_hf` 的场景是：新模型引入了 `ModelConfig` 里根本没有的字段（如滑动窗口、部分旋转维度、非 RMS 的 norm）。

#### 4.2.2 核心流程

判断一个新模型要不要扩展 `ModelConfig` 的决策树：

```
读新模型的 HF config.json，逐字段比对 ModelConfig:
    num_hidden_layers / num_attention_heads / hidden_size / vocab_size
        -> 标准字段，from_hf 直接读
    num_key_value_heads / head_dim
        -> getattr 兜底，缺失也能推
    rope_theta（顶层 or rope_scaling 字典）
        -> from_hf 的 or 链已兼容两种来源
    hidden_act（silu / gelu）
        -> 决定 MLP 激活，GatedMLP 用 FN_MAP 查
    tie_word_embeddings
        -> getattr 默认 False
    若是 MoE:
        num_local_experts/num_experts, num_experts_per_tok,
        moe_intermediate_size, norm_topk_prob
        -> from_hf 已读，且 model_type 含 "moe" 才 is_moe=True
    若有 ModelConfig 没有的字段（如 sliding_window、partial rotary_dim）:
        -> 需要扩展 ModelConfig 与 from_hf
```

特别强调 **MoE 的两个耦合点**：一个新 MoE 模型要同时满足 (a) `model_type` 字符串里含 `"moe"`（否则 `is_moe=False`，Engine 不建 `moe_backend`，`load_weight` 也不走专家堆叠）；(b) 模型层里用 `MoEMLP` 而非 `GatedMLP`。两者缺一不可。

#### 4.2.3 源码精读

`ModelConfig` 的全部字段——这是新模型必须能填满的「字段集」：

[python/minisgl/models/config.py:17-34](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/config.py#L17-L34) —— 16 个字段。dense 模型只用前 12 个左右（`num_experts` 等保持默认 0），MoE 模型才填后 5 个。

MoE 判定——只看 `model_type` 字符串：

[python/minisgl/models/config.py:36-38](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/config.py#L36-L38) —— `"moe" in self.model_type`。Qwen3-MoE 的 `model_type="qwen3_moe"` 命中；dense 的 `"llama"`/`"qwen3"` 不命中。**接入新 MoE 模型时，若其 `model_type` 不含 `"moe"`（比如叫 `"deepseek_moe"` 就行，但若叫 `"expert_mix"` 就不行），这里会误判为 dense，导致 MoE 权重加载崩在 `load_state_dict`。**

`from_hf` 对 MoE 字段的兜底读取——都带默认值 0/False：

[python/minisgl/models/config.py:53-56](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/config.py#L53-L56) —— `num_experts` 用 `num_local_experts`→`num_experts` 的 fallback 链，兼容不同家族的命名。

`RotaryConfig` 子配置——注意 `rotary_dim = head_dim` 是写死的全旋转：

[python/minisgl/models/config.py:74-80](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/config.py#L74-L80) —— 若新模型需要部分旋转维度（`rotary_dim < head_dim`），这里要改成读 HF config 的 `rotary_dim`/`rope_dim` 字段。

#### 4.2.4 代码实践

**实践目标**：拿到一个新模型的 HF config，检查 `from_hf` 能否完整翻译、有没有字段缺失。

**操作步骤**（纯 CPU）：

1. 挑一个本项目已支持的模型（如 `Qwen/Qwen3-0.6B`）和一个**未支持**的 Llama 系 decoder 模型（如 `mistralai/Mistral-7B-v0.1` 或任意 Llama 架构变体，**以本地可下载为准**）。
2. 分别跑 `ModelConfig.from_hf(AutoConfig.from_pretrained(...))`，逐字段打印对比：

   ```python
   # 示例代码
   from dataclasses import asdict
   from transformers import AutoConfig
   from minisgl.models.config import ModelConfig

   for name in ["Qwen/Qwen3-0.6B", "<你想测的新模型>"]:
       mc = ModelConfig.from_hf(AutoConfig.from_pretrained(name, trust_remote_code=True))
       print(name, asdict(mc))
   ```

3. 重点核对：`num_kv_heads`（GQA？）、`rotary_config.base`（rope_theta 取对了吗）、`is_moe`、`hidden_act`（是 silu 还是 gelu）。

**需要观察的现象**：只要新模型是标准 Llama 系，`asdict(mc)` 里 16 个字段都能被合理填充，不报错；`is_moe` 与该模型是否真为 MoE 一致。

**预期结果**：标准模型无需改 `from_hf`。若打印时某字段是兜底默认值（如 `head_dim` 落到 `hidden_size//num_attention_heads`），说明该模型没显式给该字段——通常也正确。若新模型有 `from_hf` 根本不认识的字段（如 `sliding_window`），这些字段会被**静默丢弃**，需要你判断是否影响功能。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：一个新 MoE 模型的 `config.json` 里 `model_type="qwen3_moe"`，但它的层定义里误用了 `GatedMLP`（dense 的 MLP）而不是 `MoEMLP`。会出现什么问题？

**参考答案**：`is_moe=True`（因 `"moe" in "qwen3_moe"`），所以 Engine 会建 `moe_backend`、`load_weight` 会走专家堆叠分支产出 `(num_experts, ...)` 的打包权重。但模型层用的是 `GatedMLP`，它的 `gate_up_proj` 是 2D 不是 3D，`load_state_dict` 会因形状不符报错。这正说明 MoE 的两个耦合点（`model_type` 含 moe + 层用 `MoEMLP`）必须同时满足。

**练习 2**：若新模型的激活函数是 `gelu` 而非 `silu`，`ModelConfig` 要改吗？`from_hf` 要改吗？

**参考答案**：`ModelConfig` 和 `from_hf` 都不用改。`hidden_act` 字段已被 [config.py:71](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/config.py#L71) 读入；真正的差异在 `GatedMLP` 里，[utils.py:33-37](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/utils.py#L33-L37) 的 `FN_MAP = {"silu": silu_and_mul, "gelu": gelu_and_mul}` 已支持两种，按 `config.hidden_act` 自动选。只要 HF config 的 `hidden_act` 写的是 `"gelu"`/`"silu"` 即可。

### 4.3 层复用：RopeAttn / GatedMLP 的开关与差异

#### 4.3.1 概念说明

打开 `llama.py`、`qwen2.py`、`qwen3.py` 三个文件逐行对比，会发现一个惊人的事实：**它们几乎一模一样**。三个模型都由 `XxxDecoderLayer` / `XxxModel` / `XxxForCausalLM` 三层类构成，结构完全相同：

```
XxxForCausalLM
  ├── self.model = XxxModel
  │     ├── embed_tokens = VocabParallelEmbedding
  │     ├── layers = OPList[XxxDecoderLayer × num_layers]
  │     └── norm = RMSNormFused
  └── lm_head = ParallelLMHead

XxxDecoderLayer
  ├── input_layernorm = RMSNormFused
  ├── self_attn = RopeAttn(...)        # 复用 utils.RopeAttn
  ├── post_attention_layernorm = RMSNormFused
  └── mlp = GatedMLP(...)              # 复用 utils.GatedMLP（或 MoEMLP）
```

唯一的差异集中在 `RopeAttn` 的两个开关上：

| 模型 | `has_qk_norm` | `has_attn_bias` | MLP |
| --- | --- | --- | --- |
| Llama | `False` | `False` | `GatedMLP` |
| Qwen2 | `False` | `True` | `GatedMLP` |
| Qwen3 | `True` | `False` | `GatedMLP` |
| Qwen3-MoE | `True` | `False` | `MoEMLP` |

这就是 Mini-SGLang 接入新模型「省力」的根本原因：**只要新模型是标准 decoder 架构，根本不用写新的层，照抄一个模型文件、调一两个开关即可**。需要自己写层（如 sliding window attention、不同的 norm 位置）属于「非标准架构」，工作量会陡增。

#### 4.3.2 核心流程

接入一个标准 decoder 新模型的「层」部分：

```
1. 决定开关：
   - has_qk_norm: 新模型是否有 q_norm/k_norm（per-head RMSNorm，Qwen3 有，Llama 无）
   - has_attn_bias: qkv_proj 是否带 bias（Qwen2 有，多数无）
   - MLP 类型: dense -> GatedMLP；MoE -> MoEMLP
2. 抄 llama.py，改名：
   - LlamaDecoderLayer -> NewDecoderLayer
   - self_attn = RopeAttn(config, layer_id, has_qk_norm=?, has_attn_bias=?)
   - mlp = GatedMLP(config) 或 MoEMLP(config)
3. forward 逻辑几乎不动：
   - DecoderLayer.forward: norm→attn→norm→mlp，残差融合
   - Model.forward: embed→循环 layers→norm，返回最后隐藏态
   - ForCausalLM.forward: model.forward(input_ids)→lm_head，无参，从全局 ctx 取 input_ids
```

残差融合（u8-l1 讲过）贯穿全程：每个 `RMSNormFused.forward(x, residual)` 同时做「加残差 + 归一化」，返回新的 `(x, residual)` 在层内与跨层间传递。

#### 4.3.3 源码精读

Llama 层定义——注意它直接 `from .utils import GatedMLP as LlamaMLP`、`RopeAttn as LlamaAttn`，纯粹是别名复用：

[python/minisgl/models/llama.py:10-12](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/llama.py#L10-L12) —— 把 `utils` 里的通用层起个模型专属别名。

[python/minisgl/models/llama.py:18-43](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/llama.py#L18-L43) —— `LlamaDecoderLayer`：`self_attn = LlamaAttn(config, layer_id)`（用默认开关 `has_qk_norm=False, has_attn_bias=False`）、`mlp = LlamaMLP(config)`，forward 是标准的 norm→attn→norm→mlp。

Qwen3 与 Llama 的**唯一**差异——`has_qk_norm=True`：

[python/minisgl/models/qwen3.py:20](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/qwen3.py#L20) —— `self.self_attn = Qwen3Attn(config, layer_id, has_qk_norm=True)`。整个 qwen3.py 与 llama.py 相比，实质改动只有这一行的开关，其余（Model、ForCausalLM、forward）逐行相同。

Qwen2 的差异——同时带 bias、无 qk_norm：

[python/minisgl/models/qwen2.py:20](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/qwen2.py#L20) —— `RopeAttn(config, layer_id, has_qk_norm=False, has_attn_bias=True)`。

Qwen3-MoE 的差异——MLP 换成 MoE：

[python/minisgl/models/qwen3_moe.py:11](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/qwen3_moe.py#L11) —— `from .utils import MoEMLP as Qwen3MLP`，于是 decoder layer 里的 `self.mlp` 变成 `MoEMLP`。attention 部分与 dense Qwen3 完全一致（`has_qk_norm=True`）。

`RopeAttn` 内部如何消化这两个开关——决定要不要建 `q_norm`/`k_norm` 子层、要不要给 `qkv_proj` 加 bias：

[python/minisgl/models/utils.py:79-116](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/utils.py#L79-L116) —— `RopeAttn.__init__` 全文。`has_qk_norm` 为真才建 `q_norm`/`k_norm`（[utils.py:96-102](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/utils.py#L96-L102)），否则置 `None`；`has_attn_bias` 直接透传给 `LinearQKVMerged`（[utils.py:89-95](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/utils.py#L89-L95)）。

`GatedMLP` 如何按 `hidden_act` 选激活——这样新模型换激活函数也不用改层：

[python/minisgl/models/utils.py:25-50](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/utils.py#L25-L50) —— `gate_up_proj = LinearColParallelMerged(...)`、`down_proj = LinearRowParallel(...)`，[utils.py:33-37](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/utils.py#L33-L37) 的 `FN_MAP` 把 `"silu"`/`"gelu"` 映射到融合激活 kernel。

抽象基类 `BaseLLMModel`——只约束一件事：必须有无参 `forward()`：

[python/minisgl/models/base.py:12-14](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/base.py#L12-L14) —— `forward(self) -> torch.Tensor`。新模型的顶层类必须继承 `BaseLLMModel` 且 forward 不收参数，而是从 `get_global_ctx().batch.input_ids` 取输入（见 [llama.py:79-82](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/llama.py#L79-L82)）。

#### 4.3.4 代码实践

**实践目标**：通过对比三个模型文件，提炼出「新增一个标准 decoder 模型」需要改的最小行集合。

**操作步骤**（源码阅读型，无需运行）：

1. 把 [llama.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/llama.py) 与 [qwen3.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/qwen3.py) 并排，用 diff 工具或肉眼找出所有不同行。
2. 记录差异：除了类名（`Llama*` → `Qwen3*`）和别名（`LlamaMLP`→`Qwen3MLP`），实质差异只有 [qwen3.py:20](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/qwen3.py#L20) 多了 `has_qk_norm=True`。
3. 据此写一个「假想的新模型 `FooForCausalLM`」的 decoder layer 草图（**示例代码**），假设它有 qk_norm、无 bias、dense MLP：

   ```python
   # 示例代码（接入新模型的最小层定义）
   from .utils import GatedMLP as FooMLP
   from .utils import RopeAttn as FooAttn

   class FooDecoderLayer(BaseOP):
       def __init__(self, config, layer_id):
           self.self_attn = FooAttn(config, layer_id, has_qk_norm=True)  # 唯一要定的开关
           self.mlp = FooMLP(config)
           self.input_layernorm = RMSNormFused(size=config.hidden_size, eps=config.rms_norm_eps)
           self.post_attention_layernorm = RMSNormFused(size=config.hidden_size, eps=config.rms_norm_eps)
           self._layer_id = layer_id
       # forward 与 LlamaDecoderLayer 完全相同，照抄
   ```

**需要观察的现象**：三个真实模型文件的差异行数个位数；新模型的层定义几乎就是 llama.py 改名 + 调一个开关。

**预期结果**：你应当确信——对标准 decoder 架构，**接入新模型不需要写任何新的数值计算逻辑**，只是组装现有积木。

#### 4.3.5 小练习与答案

**练习 1**：为什么三个模型文件不抽成一个带参数的通用模型类（比如 `GenericDecoderForCausalLM(config, has_qk_norm=..., has_attn_bias=...)`），而要各自抄一份？

**参考答案**：这是可读性 vs 复用性的取舍。抄一份的好处是每个文件自成一个清晰、可单独阅读的模型定义，初学者打开 `qwen3.py` 就能看到完整的 Qwen3 结构，不用追踪基类的分支逻辑；代价是几处重复。Mini-SGLang 作为教学项目偏向可读性，且重复部分很短（每个文件约 80 行）。真正的可复用逻辑（`RopeAttn`/`GatedMLP`/各 Linear）已经下沉到 `utils.py` / `layers/`，没有重复。

**练习 2**：若新模型的 attention 用了 sliding window（滑动窗口，只看最近 N 个 token），现有 `RopeAttn` 还能直接复用吗？

**参考答案**：不能直接复用。`RopeAttn` 调用的是通用 `AttentionLayer` → `ctx.attn_backend.forward`，而注意力后端（u7）的 metadata 里没有 sliding window 的窗口掩码逻辑。这种「非标准架构」需要：要么扩展注意力后端支持窗口掩码，要么在该模型文件里自定义一个带窗口的 attention 层。这就超出了「照抄 llama.py 调开关」的省力范畴，属于较大改动。

### 4.4 权重映射：ckpt key 如何对上 load_weight

#### 4.4.1 概念说明

新模型文件写好后，`model.state_dict()` 会产出一组带名字的张量（key 来自属性路径，如 `model.layers.0.self_attn.qkv_proj.weight`）。而 HF ckpt 里的 key 长得不一样（如 `model.layers.0.self_attn.q_proj.weight` + `.k_proj.weight` + `.v_proj.weight` 三份分开）。`load_weight`（u8-l2 详讲）负责把后者变换成前者。

对「接入新模型」，关键认知是：**`load_weight` 的规则是「按 key 后缀」匹配的，与具体模型家族无关**。只要新模型的权重命名遵循 HF 的通用约定（`...q_proj/k_proj/v_proj/gate_proj/up_proj/o_proj/down_proj`），加载器就自动生效，**不用改 `weight.py`**。需要改 `weight.py` 的场景是：新模型用了非标准权重名（如把 `q_proj` 叫 `query`、或 attention 已经是合并的 `qkv_proj`）。

这背后是一条贯穿 u8-l2 的「形状契约」：加载器切分/合并给出的 local 形状，必须等于层定义里 `div_even` 算出的 local 形状。复用 `RopeAttn`/`GatedMLP` + 标准 key 命名，这条契约就自动成立。

#### 4.4.2 核心流程

新模型每个 HF 权重 key 在 `load_weight` 里的路由：

```
对每个 HF key（先去 multimodal 前缀、去 language_model. 前缀）:
    1. _shard_tensor 按 TP 切:
        - 含 .q_proj/.k_proj/.v_proj/.gate_proj/.up_proj  -> 切 dim0（GQA 下 k/v 可能头复制）
        - 含 .o_proj/.down_proj                            -> 切 dim1
        - 含 lm_head/embed_tokens                          -> 词表并行向上取整
        - 其余（norm 等）                                   -> 不切，整份
    2. _get_merge_info 看是否合并:
        - q/k/v     -> 攒齐三件 -> torch.cat(dim=0) -> ...qkv_proj
        - gate/up   -> 攒齐两件 -> torch.cat(dim=0) -> ...gate_up_proj
        - 其余       -> 不合并
    3. (仅 MoE) _get_expert_stack_info 看是否专家权重:
        - 匹配 ...experts.{idx}.{name} -> 攒齐 num_experts 件 -> torch.stack(dim=0)
    4. yield (变换后的 key, tensor)
```

判断新模型要不要改 `weight.py`，就看它的 ckpt key 能不能全部被上面三步「吃掉」。

#### 4.4.3 源码精读

切分规则表——按后缀子串匹配，与模型家族无关：

[python/minisgl/models/weight.py:13-14](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/weight.py#L13-L14) —— `_SPLIT_DIM_0`（列并行后缀）/`_SPLIT_DIM_1`（行并行后缀）。新模型的 q/k/v/gate/up 只要叫这些名，自动走 dim0；o/down 自动走 dim1。

合并组——把分散投影拼成运行时融合矩阵：

[python/minisgl/models/weight.py:17-23](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/weight.py#L17-L23) —— `_MERGE_GROUPS`：`q/k/v → qkv_proj`、`gate/up → gate_up_proj`。这要求新模型层的属性名也必须是 `qkv_proj`/`gate_up_proj`——复用 `RopeAttn`/`GatedMLP` 时天然满足（见 [utils.py:89](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/utils.py#L89) 的 `self.qkv_proj`、[utils.py:27](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/utils.py#L27) 的 `self.gate_up_proj`）。

多模态/前缀清理——让语言模型部分能被干净加载：

[python/minisgl/models/weight.py:93-96](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/weight.py#L93-L96) —— 跳过 `vision_tower.`/`multi_modal_projector.`，剥掉 `language_model.` 前缀。这就是为什么多模态 Mistral3 也能用同一个 `MistralForCausalLM` 加载——视觉权重被丢弃，语言部分前缀被剥成与纯文本模型一致的 key。

形状契约的关键对账点——加载器切出的 local 形状必须等于层算出的 local 形状：

[python/minisgl/layers/linear.py:82-87](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/linear.py#L82-L87) —— `LinearQKVMerged` 算出 `local_osize = (local_num_qo + 2*local_num_kv) * head_dim`，必须等于 `load_weight` 把本卡 q/k/v 片段 cat 起来的行数。GQA 下 k/v 走头复制（[weight.py:38-41](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/weight.py#L38-L41)），对应层侧 `div_even(allow_replicate=True)`（[linear.py:83](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/linear.py#L83)）——两侧用同一套 `div_even` 语义，契约才成立。接入新模型时只要复用这些层，契约自动满足。

完整性断言——ckpt 缺权重会在加载末尾暴露：

[python/minisgl/models/weight.py:123-124](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/weight.py#L123-L124) —— 若新模型 ckpt 缺了 q/k/v 某一个，或 MoE 缺专家，`merge_buf`/`expert_buf` 非空，在此报错。这是接入新模型时第二常见的报错来源（第一是注册表未找到）。

#### 4.4.4 代码实践

**实践目标**：拿一个新模型的 ckpt key 清单，逐个判断它在 `load_weight` 里走哪条路径、最终变成什么运行时 key。

**操作步骤**（源码阅读型）：

1. 用 `safetensors` 列出新模型某个 layer 的全部 key（**示例代码**）：

   ```python
   # 示例代码
   import glob, safetensors
   f = sorted(glob.glob("<模型目录>/*.safetensors"))[0]
   with safetensors.safe_open(f, framework="pt") as fp:
       keys = [k for k in fp.keys() if "layers.0." in k]
   for k in sorted(keys):
       print(k)
   ```

2. 对每个 key，按 [weight.py:13-14](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/weight.py#L13-L14) 与 [weight.py:17-23](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/weight.py#L17-L23) 标注它的「切分维度 / 是否合并 / 最终 key」。例如 `...self_attn.q_proj.weight` → 切 dim0 → 合并进 `...self_attn.qkv_proj`。
3. 找出**对不上**的 key：任何不含 `_SPLIT_DIM_0`/`_SPLIT_DIM_1`/`lm_head`/`embed_tokens` 后缀、又不进合并组的 key（如自定义命名的投影、额外的 adapter 权重），就是需要改 `weight.py` 或在模型层消化的地方。

**需要观察的现象**：标准模型的 layer 权重会被干净地归并为 `{qkv_proj, o_proj, gate_up_proj, down_proj}` 四个 attention/MLP 权重 + 几个 norm 权重；非标 key 会「漏网」，成为需要处理的信号。

**预期结果**：标准 Llama 系模型的 key 全部命中既有规则，`weight.py` 无需改动。若有漏网 key，说明该模型有非标准组件。**待本地用真实 ckpt 验证**。

#### 4.4.5 小练习与答案

**练习 1**：新模型的 ckpt 里 attention 权重已经存成合并形态 `...self_attn.qkv_proj.weight`（而非分开的 q/k/v）。`load_weight` 能加载它吗？

**参考答案**：能，但走的是「不合并」分支。`_get_merge_info` 靠后缀 `.q_proj/.k_proj/.v_proj` 命中（[weight.py:17-23](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/weight.py#L17-L23)），`.qkv_proj` 不在表里，所以不进合并缓冲，直接 yield。但 `_shard_tensor` 仍会处理它——`qkv_proj` 含 `.q_proj` 子串吗？不含（它是 `qkv_proj` 不是 `q_proj`），所以会落到 `else` 分支**不切分整份返回**，这在多卡下是错的。因此对「ckpt 已合并」的模型，要么改 `_SPLIT_DIM_0` 让它认 `qkv_proj`，要么模型层仍用 `qkv_proj` 命名但加载器需补充切分规则。总之，**命名与规则必须配套**。

**练习 2**：为什么多模态模型（带视觉塔）也能用本讲的流程接入？

**参考答案**：因为 [weight.py:93-96](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/weight.py#L93-L96) 会跳过 `vision_tower.`/`multi_modal_projector.` 权重、剥掉 `language_model.` 前缀；[config.py:42-47](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/config.py#L42-L47) 的 `from_hf` 会下钻 `text_config`。所以只要语言模型部分是标准 decoder，视觉部分被丢弃，加载与建图都正常——本质上是「把多模态模型当它的语言子模型来跑」。注册表里 Mistral3 就是这么处理的（[register.py:11](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/register.py#L11)）。

## 5. 综合实践

把本讲四个模块串成一份可照做的**「接入新模型 checklist」**。假设要接入一个名为 `FooForCausalLM` 的标准 decoder 模型（dense，有 qk_norm，无 bias，SwiGLU，GQA），请按下表逐项落实，并标注每项对应的源码位置与「不做会怎样」。

| # | 要改/新增的文件 | 具体操作 | 对应源码 | 不做会怎样 |
| --- | --- | --- | --- | --- |
| 1 | `python/minisgl/models/foo.py`（**新增**） | 抄 `llama.py`，类名改 `FooDecoderLayer`/`FooModel`/`FooForCausalLM`；`RopeAttn(config, layer_id, has_qk_norm=True)`；`mlp=GatedMLP(config)`；继承 `BaseLLMModel`；`forward` 无参、从 `get_global_ctx().batch.input_ids` 取输入。 | [llama.py:18-82](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/llama.py#L18-L82)、[base.py:12-14](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/base.py#L12-L14) | 系统找不到模型实现类。 |
| 2 | `python/minisgl/models/register.py`（**改**） | 在 `_MODEL_REGISTRY` 加一行：`"FooForCausalLM": (".foo", "FooForCausalLM")`。架构名必须与 HF `config.json` 的 `architectures[0]` **完全一致**。 | [register.py:5-12](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/register.py#L5-L12) | `get_model_class` 抛 `ValueError: ... not supported`。 |
| 3 | `config.py` / `from_hf`（**多数不改**） | 跑 `ModelConfig.from_hf(AutoConfig.from_pretrained(...))`，确认 16 字段齐全、`is_moe=False`、`rotary_config.base` 取对。只有非标字段（sliding_window、partial rotary）才扩展。 | [config.py:41-87](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/config.py#L41-L87) | 非标字段被静默丢弃，模型行为偏差。 |
| 4 | `weight.py`（**多数不改**） | 列 ckpt key，确认全部命中 `_SPLIT_DIM_0/1`、`_MERGE_GROUPS`；只有非标命名（如已合并的 `qkv_proj` ckpt、自定义投影名）才补规则表。 | [weight.py:13-23](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/weight.py#L13-L23) | `load_state_dict` 形状不符报错，或 `assert not merge_buf` 失败。 |
| 5 | （MoE 才需要）`foo.py` + `model_type` | 若是 MoE：层里用 `MoEMLP` 而非 `GatedMLP`；且 HF `model_type` 字符串须含 `"moe"`。 | [utils.py:53-76](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/utils.py#L53-L76)、[config.py:36-38](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/config.py#L36-L38) | `is_moe` 误判，Engine 不建 `moe_backend`、专家权重堆叠维度对不上。 |
| 6 | 验证 | 单卡 `python -m minisgl --model-path <foo-ckpt>` 起服务，发一条请求看能否出 token；失败时按「注册表 → from_hf → load_weight 形状」三段排查。 | [engine.py:51-52](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/engine.py#L51-L52) | — |

**产出要求**：把上表复制到自己的笔记里，针对你真实要接入的模型，把「架构名」「开关取值」「是否 MoE」「ckpt key 抽样」四栏填上具体值。这份表就是你后续接入任何标准 decoder 模型的通用工单。

> 排查顺序提示：启动报错先看是不是 `not supported`（注册表，第 2 项）；建图后 `load_state_dict` 报形状不符，看是 key 命中（第 4 项）还是 GQA 切分（[weight.py:38-41](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/weight.py#L38-L41)）；MoE 模型行为怪异，先查 `model_type` 含不含 `"moe"`（第 5 项）。

## 6. 本讲小结

- 接入新模型的「钥匙」是 HF `config.json` 的 `architectures[0]`，`_MODEL_REGISTRY` 把它映射到 `(模块, 类名)`，`get_model_class` 用 `importlib` 动态导入——Engine 一行不用改，加注册项即可。
- `llama.py`/`qwen2.py`/`qwen3.py` 几乎逐行相同，差异只在 `RopeAttn` 的 `has_qk_norm`/`has_attn_bias` 两个开关与 MLP 类型（`GatedMLP` vs `MoEMLP`）；标准 decoder 新模型只是「抄一份 + 调开关」。
- `ModelConfig.from_hf` 用大量 `getattr` 兜底，标准 Llama 系模型无需改 config；唯一要警惕的是 MoE 判定 `"moe" in model_type`——新 MoE 模型的 `model_type` 必须含 `"moe"` 且层必须用 `MoEMLP`，两者耦合。
- `load_weight` 的切分/合并规则按 key **后缀**匹配、与模型家族无关；只要新模型权重沿用 `q_proj/k_proj/...` 通用命名并复用 `RopeAttn`/`GatedMLP`，加载器与层之间的「形状契约」自动成立，`weight.py` 通常不改。
- 多模态模型能走同一套流程，因为加载器会丢弃视觉权重、剥掉 `language_model.` 前缀，`from_hf` 会下钻 `text_config`——本质是「把多模态当它的语言子模型跑」。
- 非标准架构（sliding window、部分旋转、非标权重命名）才需要改 `weight.py`/注意力后端/自定义层，工作量陡增，不在「照抄调开关」的省力范畴。

## 7. 下一步学习建议

- 想亲手验证本讲的形状契约？回到 **u8-l2（模型配置解析与权重加载分片）** 的综合实践，在真实 ckpt 上跑 `load_weight` 打印每个 `(key, shape)`，与新模型 `state_dict()` 逐项比对。
- 接入的模型用了 MoE？读 **u10-l1（MoE 后端 Fused MoE）**，搞清打包好的 `(num_experts, ...)` 权重在两段 grouped GEMM 里怎么被消费，以及 `MoEMLP` → `ctx.moe_backend` 的委托关系。
- 想理解复用的 `RopeAttn`/`GatedMLP` 内部的 TP 通信？读 **u9-l1（张量并行 Linear 与分布式通信）** 与 **u9-l2（Embedding/Norm/RoPE/AttentionLayer）**，它们解释了 `LinearQKVMerged`/`LinearRowParallel` 的 `all_reduce` 与 `AttentionLayer.split`。
- 若你正在接入的模型用了滑动窗口或其它特殊注意力，读 **u7（注意力后端）** 两篇，评估是需要扩展 `BaseAttnMetadata` 还是自定义后端——这是判断「省力接入」还是「深度改造」的分水岭。
- 接入完成后，建议用 **u11-l1（LLM 离线推理接口与基准）** 的 `LLM.generate` 离线跑一轮正确性 + 吞吐验证，确认新模型在 prefill/decode 两个阶段都行为正确。
