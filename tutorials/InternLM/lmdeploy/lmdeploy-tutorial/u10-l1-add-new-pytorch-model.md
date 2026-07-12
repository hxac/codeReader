# 添加新 PyTorch 模型完整流程

## 1. 本讲目标

学完本讲，你应当能够：

- 说清「让 LMDeploy 支持一个全新的 PyTorch 后端模型」从 `config.json` 到跑通推理的**完整接入链路**。
- 区分这条链路上三件互相独立、各司其职的事：**注册（module_map）**、**实现（models/xxx.py）**、**路由（archs）**。
- 理解 HuggingFace 架构类名（arch 名）是如何作为「模型身份证」贯穿这三件事的。
- 看懂 `llama.py` 这份参考实现的「五层类结构」，并掌握复用 `nn/` 积木的拼装套路。
- 掌握权重加载契约——`packed_modules_mapping`（类属性）与 `stacked_params_mapping`（`load_weights` 内）如何把 HF 分片权重灌进打包参数。
- 为一个假设的新模型，独立写出「需新增/修改哪些文件」的清单与最小重写类骨架。

本讲是 U3「PyTorch 后端：模型加载与 Patch 机制」的收尾与综合应用，**强烈依赖 u3-l3（Patch 机制）与 u3-l4（Llama 重写）**。如果你对 `build_patched_model`、`module_map`、`qualname` 这些词还陌生，请先回看那两讲。

## 2. 前置知识

本讲不引入新的底层机制，而是把前面几讲建立的概念「串成一条可操作的工程流程」。开始前，请确认你理解以下概念：

- **arch 名（架构类名）**：模型 `config.json` 里 `architectures` 字段的第一项，例如 `LlamaForCausalLM`。它是 LMDeploy 全栈的「模型身份证」，后端选择、patch 映射、任务路由都靠它查表（见 u2-l5）。
- **Patch 重写机制**：LMDeploy 不修改 HuggingFace 模型代码，而是在实例化时按 arch 名换成自己的优化实现类（见 u3-l3）。核心入口是 `build_patched_model`，它查 `MODULE_MAP` 决定用哪个类。
- **积木复用哲学**：重写类内部不写 attention/MLP 的数学公式，而是调用 `nn.Attention`、`nn.linear.build_*`、`nn.RMSNorm`、`nn.ApplyRotaryEmb` 等积木，真正的 kernel 藏在 `backends/`（见 u3-l4、u5-l1、u5-l2）。
- **打包参数**：为减少访存，HF 里的 `q_proj/k_proj/v_proj` 会被融合成 `qkv_proj`，`gate_proj/up_proj` 融合成 `gate_up_proj`。这带来了「权重重命名」需求（见 u3-l4）。
- **后端双轨**：用户调 `pipeline()`，内部在 TurboMind（C++，需在白名单内）与 PyTorch（纯 Python，靠 patch）之间选择（见 u2-l5、u6-l1）。本讲只讲 **PyTorch 后端**的接入。

> 一句话回顾：**「写一个新模型 = 写一份 models/xxx.py 重写类 + 在 module_map 里登记一次 arch 名」**。其余（archs 路由、config builder、量化映射）只在非标准情况下才需要。

## 3. 本讲源码地图

本讲涉及的关键文件及其职责：

| 文件 | 职责 | 本讲用它讲什么 |
| --- | --- | --- |
| [lmdeploy/pytorch/models/module_map.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/module_map.py) | arch 名 → LMDeploy 实现类 qualname 的**纯数据注册表** | 「注册」这一步：在哪一行加一条映射 |
| [lmdeploy/pytorch/models/patch.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/patch.py) | 执行 Patch 的引擎：查表、import 类、实例化 | 注册表是如何被消费的；三级匹配与移除模型报错 |
| [lmdeploy/pytorch/models/llama.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/llama.py) | Llama 的完整重写实现（参考样板） | 「实现」这一步：五层类结构与权重加载契约 |
| [lmdeploy/archs.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/archs.py) | 全局路由器：选后端、选配置、选任务/Pipeline 类 | 「路由」这一步：新模型如何被正确分流；VLM 注册 |
| [lmdeploy/pytorch/models/utils/model.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/utils/model.py) | `DeployModelMixinV1` 等公共 mixin 与 `build_embedding` | 重写类的可选基座与量化配置钩子 |
| [lmdeploy/pytorch/models/utils/cudagraph.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/utils/cudagraph.py) | `CudaGraphMixin` | 重写类如何对接 CUDA Graph 捕获 |
| [lmdeploy/pytorch/weight_loader/model_weight_loader.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/weight_loader/model_weight_loader.py) | 权重逐参数加载契约 | `load_weight` 如何按 `shard_id` 分发 |
| [lmdeploy/pytorch/configurations/builder.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/configurations/builder.py) | HF config → 引擎内部 `ModelConfig` 的构建器注册中心 | 非标准 config 何时需要自定义 builder |

## 4. 核心概念与源码讲解

本讲围绕三个最小模块展开：**4.1 module_map 注册**、**4.2 models 实现**、**4.3 archs 接入**。它们恰好对应接入新模型时三件最容易混淆的事——登记身份证、写实现、接路由。最后用一份「全流程清单」把它们串起来。

### 4.1 module_map 注册：给模型发一张「身份证」

#### 4.1.1 概念说明

`MODULE_MAP` 是一个普通 Python 字典，**键是 HuggingFace 的 arch 类名，值是 LMDeploy 重写类的「全限定名字符串」（qualname）**。它是一张纯数据表，本身不 import 任何类——值用字符串而非类对象，正是为了**延迟导入**：避免在加载 module_map 时把几十个模型的依赖（torch、kernels）全部拉起来。

这张表是 Patch 机制的「查表入口」。引擎启动时，会拿模型 `config.json` 的 `architectures[0]` 去这张表里查，查到字符串后，再在运行时动态 `import` 对应的类。所以「支持一个新模型」最必不可少的一步，就是在这张表里加一行。

需要厘清三张相关表的区别（详见 u3-l3）：

- `MODULE_MAP`：主表，cuda 设备默认用它。
- `DEVICE_SPECIAL_MODULE_MAP`（含 `ASCEND_MODULE_MAP` 等）：非 cuda 设备的覆盖表，键相同时覆盖主表。
- `CUSTOM_MODULE_MAP`：用户自定义表，优先级最高，可在不改源码的前提下支持新模型。
- `REMOVED_MODEL_MAP`：已下线模型的清单，不是用来匹配实现的，而是用来**给出清晰报错**。

#### 4.1.2 核心流程

注册到使用的流程如下（伪代码）：

```
1. 开发者:在 module_map.py 加一行
   MODULE_MAP.update({'MyModelForCausalLM': '...my_model.MyModelForCausalLM'})

2. 引擎启动:build_patched_model(config)
   -> 读 config.hf_config
   -> _get_module_map():  MODULE_MAP ⊕ 设备表 ⊕ CUSTOM_MODULE_MAP
   -> _get_model_class(hf_config, module_map):
        arch = hf_config.architectures[0]
        _raise_if_removed_model(arch)   # 命中 REMOVED_MODEL_MAP 则抛错
        qualname = module_map[arch]
        cls = _class_from_qualname(qualname)  # 运行时 import
        return cls

3. model = cls(hf_config, ctx_mgr, ...)   # 实例化重写类(空壳,未灌权重)
```

关键点：**arch 名必须与 HF `config.json` 里的字符串逐字符一致**（含大小写）。写错一个字母，匹配就会失败并抛出 `Can not found rewrite for architectures` 错误。

#### 4.1.3 源码精读

注册表的四个关键声明都在 `module_map.py` 开头。`MODULE_MAP` 是个空 dict，随后用大量 `MODULE_MAP.update({...})` 逐族填充：

[lmdeploy/pytorch/models/module_map.py:13-21](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/module_map.py#L13-L21) — `REMOVED_MODEL_MAP`，已下线模型清单。命中时 `_raise_if_removed_model` 会给出「请用旧版本或迁移到新模型族」的明确提示，而不是晦涩的「找不到实现」。

[lmdeploy/pytorch/models/module_map.py:24-26](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/module_map.py#L24-L26) — Llama 的注册项，是「加一行」的标准范式：键是 HF 类名 `LlamaForCausalLM`，值是 `lmdeploy.pytorch.models.llama.LlamaForCausalLM`（用前缀常量 `LMDEPLOY_PYTORCH_MODEL_PATH` 拼接）。

[lmdeploy/pytorch/models/module_map.py:288](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/module_map.py#L288) — `CUSTOM_MODULE_MAP`，用户自定义表，初始为空，经 `update_custom_module_map` 从外部文件加载。

消费这张表的核心在 `patch.py` 的 `_get_model_class`：

[lmdeploy/pytorch/models/patch.py:165-196](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/patch.py#L165-L196) — 查表逻辑。它有**两级优先级**：先看 `config.auto_map['AutoModelForCausalLM']`（`trust_remote_code` 模型走这条），再看 `config.architectures` 列表。每命中一个 arch，都先过 `_raise_if_removed_model` 校验，再 `_class_from_qualname` 动态 import。注意第 184 行的特例：deepseek-vl2 的 config 结构特殊（`architectures` 在 `language_config` 下），单独硬编码处理。

[lmdeploy/pytorch/models/patch.py:156-162](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/patch.py#L156-L162) — `_raise_if_removed_model`，移除模型报错。当你把一个老模型路径喂进来，这里会让它「死得明明白白」。

[lmdeploy/pytorch/models/patch.py:106-115](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/patch.py#L106-L115) — `_get_module_map`，三表合一：复制主表 → 非 cuda 时叠加设备表 → 最后叠加 `CUSTOM_MODULE_MAP`（优先级最高）。

> 除「整模型级」精确匹配外，`patch.py` 还有「子模块级」三级降级匹配（full name → class name → submodname + 正则兜底），由 `get_rewrite_cls` 驱动，用于替换模型内部的子模块而非整个模型。新模型接入时**绝大多数走整模型级匹配**，子模块级匹配是给特殊补丁用的。详见 u3-l3。

#### 4.1.4 代码实践

**实践目标**：在 `module_map.py` 里找到任意两个模型的注册项，确认 arch 名与文件名的对应关系；并理解 `CUSTOM_MODULE_MAP` 的免改源码扩展能力。

**操作步骤**：

1. 打开 `module_map.py`，定位 `LlamaForCausalLM`（第 25 行）与 `Qwen3ForCausalLM`（第 140 行）两条注册项。
2. 确认它们的值分别指向 `...llama.LlamaForCausalLM` 与 `...qwen3.Qwen3ForCausalLM`，即「文件名 = 模块名」的约定。
3. 阅读 `patch.py` 的 `update_custom_module_map`（[第 118-153 行](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/patch.py#L118-L153)），理解它如何从一个外部 Python 文件读取 `MODULE_MAP`/`CUSTOM_MODULE_MAP` 字典并合并进 `CUSTOM_MODULE_MAP`。

**需要观察的现象**：值是字符串而非类；arch 名含 `ForCausalLM` / `ForConditionalGeneration` 等后缀，与 HF 完全一致；`CUSTOM_MODULE_MAP` 的合并放在最后，使其能覆盖主表。

**预期结果**：你能口头复述「加一行 `MODULE_MAP.update({...})` 即可让一个 arch 名指向新的重写类」。

**待本地验证**：`update_custom_module_map` 由谁在何时调用（提示：搜索 `PytorchEngineConfig` 的 `custom_module_map` 字段或全局调用点）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `MODULE_MAP` 的值用字符串（如 `'.llama.LlamaForCausalLM'`）而不是直接写 `from .llama import LlamaForCausalLM` 后存类对象？

**参考答案**：为了延迟导入。`module_map.py` 在引擎初始化早期就会被 import，若值是类对象，则会在那一刻把全部 50+ 模型文件及其依赖（torch、各种 kernel）全部加载，拖慢启动且可能因某个模型依赖缺失而整体崩溃。用字符串后，只有真正被命中的那个 arch 才会在 `_class_from_qualname` 里被 `importlib.import_module` 加载。

**练习 2**：一个模型 arch 名同时出现在 `MODULE_MAP` 和 `DEVICE_SPECIAL_MODULE_MAP['ascend']` 里，在昇腾设备上用哪个？

**参考答案**：用昇腾的。`_get_module_map` 先复制 `MODULE_MAP`，再 `update(ascend_map)` 覆盖，最后 `update(CUSTOM_MODULE_MAP)`，所以优先级是 `CUSTOM_MODULE_MAP > 设备表 > MODULE_MAP`。

### 4.2 models 实现：重写类的五层结构与权重加载契约

#### 4.2.1 概念说明

`module_map` 只是「指路牌」，真正干活的是 `lmdeploy/pytorch/models/<model>.py` 里的重写类。LMDeploy 的重写类遵循一套**五层结构**（从内到外）：

1. **`<Model>Attention`**：QKV 投影、RoPE、attention 前向。
2. **`<Model>MLP`**：gate-up 线性、激活、down 投影（MoE 模型换成 MoE 层）。
3. **`<Model>DecoderLayer`**：把 Attention + MLP 装进一个解码层，含 norm 与残差。
4. **`<Model>Model`**：embedding 表、所有解码层、最终 norm、RoPE 建表。
5. **`<Model>ForCausalLM`**：顶层类，继承 `nn.Module`（+可选 `CudaGraphMixin` / `DeployModelMixinV1`），持有 `Model` 与 `lm_head`，并实现「引擎协议」方法。

这套结构有意**对齐 HuggingFace 的类层级**，但实现全部换成 LMDeploy 的积木。重写类的核心哲学是「**拼装而非手写**」：attention 的 softmax、MLP 的激活函数都不出现在重写文件里，而是委托给 `nn/` 积木，真正的 kernel 藏在 `backends/`。

除了 5 个类，顶层 `ForCausalLM` 还要实现一组**「引擎协议」方法**（HF 原版没有）：`forward`、`get_logits`、`prepare_inputs_for_generation`、`load_weights`、`get_input_embeddings`，以及类属性 `packed_modules_mapping`。这些方法是引擎能驱动它跑 Paged Attention、张量并行、CUDA Graph 的前提。

#### 4.2.2 核心流程

一个被 patch 进来的模型，其生命周期是「**先换类、后灌权重**」（详见 u3-l3、u3-l5）：

```
1. build_patched_model(config)  (patch.py)
   -> 查 module_map 得到 ForCausalLM 类
   -> 实例化: model = ForCausalLM(hf_config, ctx_mgr, dtype, device)
        内部按层实例化 Attention/MLP/DecoderLayer/Model/lm_head
        线性层经 build_qkv_proj 等按 quant_config 自动选 FP16/AWQ/W8A8/FP8 实现
   -> 此时模型是空壳,权重未填

2. load_model_weights(model, weights)  (model_weight_loader.py)
   -> 调 model.load_weights(weights)
   -> load_weights 用 stacked_params_mapping 把 q_proj/k_proj/v_proj 改名为 qkv_proj
   -> 委托 load_weight -> param.weight_loader 完成 shard 定位与 TP 切分
```

「换类」决定了模型结构（用哪些积木、怎么连），「灌权重」决定了权重如何从 HF 磁盘格式映射进这个结构。两者通过**两个映射表**对接：

- **`packed_modules_mapping`（类属性）**：声明打包参数名 → HF 原始参数名列表，供引擎/LoRA 在**寻址**时用（如知道 `qkv_proj` 是由 `q_proj/k_proj/v_proj` 组成）。
- **`stacked_params_mapping`（`load_weights` 内局部变量）**：在**灌权重**时把 HF 的 `q_proj` 重定向到 `qkv_proj` 并标记 `shard_id`。

#### 4.2.3 源码精读

**顶层类与协议方法**——`llama.py` 的 `LlamaForCausalLM`：

[lmdeploy/pytorch/models/llama.py:289-302](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/llama.py#L289-L302) — 顶层类声明与 `packed_modules_mapping`。注意它继承的是 `(nn.Module, CudaGraphMixin)`，**没有**继承 `DeployModelMixinV1`，因此 `get_logits`/`update_weights` 是手写的（见下）。`packed_modules_mapping` 把 `qkv_proj` 对应到 `[q_proj, k_proj, v_proj]`，`gate_up_proj` 对应到 `[gate_proj, up_proj]`——键是重写类里的参数名，值是 HF 检查点里的参数名后缀。

[lmdeploy/pytorch/models/llama.py:322-339](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/llama.py#L322-L339) — `forward`。注意它返回的是 `hidden_states`（隐状态），**不是 logits**。logits 由单独的 `get_logits` 计算——这种「forward 不出 logits、`get_logits` 才投影到词表」的解耦，让投机解码等场景能复用隐状态。

[lmdeploy/pytorch/models/llama.py:346-353](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/llama.py#L346-L353) — `get_logits`，把隐状态经 `lm_head` 投影到词表。这对应 `DeployModelMixinV1.get_logits` 的同一职责（见下文 utils/model.py）。

[lmdeploy/pytorch/models/llama.py:364-391](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/llama.py#L364-L391) — `prepare_inputs_for_generation`，从 `StepContext`（引擎每步下发的上下文）里取出 `input_ids`/`position_ids`/`attn_metadata`，并处理多模态视觉 embedding 的注入（`vision_embedding_indexing` 处用 `masked_scatter` 思路替换占位 token）。这是「引擎协议」里翻译输入的关键方法。

[lmdeploy/pytorch/models/llama.py:393-423](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/llama.py#L393-L423) — **`load_weights`，权重加载契约的核心**。它定义局部 `stacked_params_mapping`：每条是 `(param_name, shard_name, shard_id)` 三元组，例如 `('.qkv_proj', '.q_proj', 'q')` 表示「把名字含 `.q_proj` 的 HF 权重，重定向到 `.qkv_proj` 并标记为 `q` 分片」。遍历权重时，命中则改名并带 `shard_id` 调 `load_weight`，否则走默认整块 copy。开头还跳过 `rotary_emb.inv_freq` 等可推导的缓存权重，以及 `tie_word_embeddings` 时的 `lm_head.weight`。

**子模块的积木拼装**——以 Attention 与 MLP 为例：

[lmdeploy/pytorch/models/llama.py:24-67](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/llama.py#L24-L67) — `LlamaAttention.__init__`。可以看到它**没有任何 attention 的数学公式**，全是积木拼装：`build_qkv_proj` 建 QKV 融合线性层、`ApplyRotaryEmb()` 建 RoPE 施加器、`Attention(...)` 建 Paged Attention 前向器、`build_o_proj` 建 output 投影。注意 `build_qkv_proj` 接收 `quant_config`——量化线性层的选择发生在这里，重写文件无需写 `if quant` 分支。

[lmdeploy/pytorch/models/llama.py:112-146](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/llama.py#L112-L146) — `LlamaMLP`。同样纯积木：`build_gateup_linear`（gate+up 融合）、`SiluAndMul`（SiLU 激活与门控相乘）、`build_down_linear`。

**权重加载的底层契约**——`load_weight`：

[lmdeploy/pytorch/weight_loader/model_weight_loader.py:19-25](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/weight_loader/model_weight_loader.py#L19-L25) — `load_weight`：若参数自带 `weight_loader`（量化线性层、TP 切分层都会挂），则把 `shard_id` 等参数交给它做定位与切分；否则走 `default_weight_loader` 整块 copy。这就是 `stacked_params_mapping` 传 `shard_id='q'` 的归宿——它告诉 `qkv_proj` 的 weight_loader「这一份权重填到 Q 段」。

**可选基座与量化配置钩子**——`utils/model.py`：

[lmdeploy/pytorch/models/utils/model.py:116-153](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/utils/model.py#L116-L153) — `DeployModelMixinV1`。它提供了 `get_logits`/`update_weights`/`build_lm_head` 的默认实现，新模型可选择继承它以少写样板（`llama.py` 因历史原因没继承，自己手写了一份等价实现）。注意 `update_weights` 处理 `tie_word_embeddings`（权重共享）时的 `lm_head` 复用。

[lmdeploy/pytorch/models/utils/model.py:80-113](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/utils/model.py#L80-L113) — `update_quant_config` 类方法。在引擎实例化模型前会被调用（见 `build_model_from_hf_config` 第 212-213 行），用于把量化配置里的 `ignored_layers` 从 HF 的分片名（如 `.q_proj`）改写成打包后的名（如 `.qkv_proj`）。这意味着**新模型若要在量化时正确忽略某些层，可能需要确认此映射**。

**CUDA Graph 对接**——`utils/cudagraph.py`：

[lmdeploy/pytorch/models/utils/cudagraph.py:88-103](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/utils/cudagraph.py#L88-L103) — `CudaGraphMixin`。继承它即可获得 CUDA Graph 捕获能力（`support_cuda_graph`/`make_buffers_cudagraph`/`fill_buffers_cudagraph` 等），代价是模型前向必须**无数据依赖的控制流**（否则图捕获会失败）。

#### 4.2.4 代码实践

**实践目标**：以 `llama.py` 为模板，为一个假设的新模型 `FooForCausalLM` 写出**最小重写类骨架**，并对齐 HF 权重命名。

**操作步骤**：

1. 假设 `FooForCausalLM` 的 HF `config.json` 里 `architectures: ["FooForCausalLM"]`，结构是标准 Llama 式（QKV 分离、gate/up 分离），`config.json` 有 `hidden_size`、`num_attention_heads`、`num_key_value_heads`、`intermediate_size`、`num_hidden_layers`、`rms_norm_eps`、`vocab_size`。
2. 用 `python -c "from transformers import AutoConfig; print(list(AutoConfig.from_pretrained('<path>').__dict__))"` 或直接读 HF 权重文件，**确认 HF 权重的真实命名**（这是最易错的一步）。
3. 复制 `llama.py` 的五层结构，把类名与 `config.json` 字段对齐，得到下面的骨架（**示例代码**，非项目原有代码）：

```python
# 示例代码: lmdeploy/pytorch/models/foo.py (新文件)
from collections.abc import Iterable
import torch
from torch import nn
from lmdeploy.pytorch.model_inputs import StepContext, StepContextManager
from lmdeploy.pytorch.nn import ApplyRotaryEmb, Attention, RMSNorm, SiluAndMul, build_rotary_embedding_from_config
from lmdeploy.pytorch.nn.linear import build_down_linear, build_gateup_linear, build_o_proj, build_qkv_proj, build_rowwise_linear
from lmdeploy.pytorch.weight_loader.model_weight_loader import load_weight
from .utils.cudagraph import CudaGraphMixin

class FooAttention(nn.Module):
    def __init__(self, config, dtype=None, device=None, is_tp: bool = True):
        super().__init__()
        qc = getattr(config, 'quantization_config', None)
        head_dim = config.hidden_size // config.num_attention_heads
        self.qkv_proj = build_qkv_proj(config.hidden_size,
            num_q_heads=config.num_attention_heads, num_kv_heads=config.num_key_value_heads,
            head_size=head_dim, bias=getattr(config, 'attention_bias', False),
            quant_config=qc, dtype=dtype, device=device, is_tp=is_tp)
        self.apply_rotary_pos_emb = ApplyRotaryEmb()
        self.attn_fwd = Attention(config.num_attention_heads, head_dim,
            num_kv_heads=config.num_key_value_heads, v_head_size=head_dim)
        self.o_proj = build_o_proj(config.num_attention_heads * head_dim, config.hidden_size,
            bias=getattr(config, 'attention_bias', False), quant_config=qc, dtype=dtype, device=device, is_tp=is_tp)

    def forward(self, hidden_states, rotary_pos_emb, past_key_value, attn_metadata):
        # 与 LlamaAttention.forward 一致: qkv -> split -> rotary -> attn -> o_proj
        ...  # 直接照抄 llama.py 的 LlamaAttention.forward

class FooMLP(nn.Module):
    # 与 LlamaMLP 结构一致,直接照抄 llama.py 的 LlamaMLP
    ...

class FooDecoderLayer(nn.Module):
    # 与 LlamaDecoderLayer 结构一致
    ...

class FooModel(nn.Module):
    # 与 LlamaModel 结构一致: embed_tokens + layers + norm + rotary_emb
    ...

class FooForCausalLM(nn.Module, CudaGraphMixin):
    # 与 HF 权重命名对齐的打包映射
    packed_modules_mapping = {
        'qkv_proj': ['q_proj', 'k_proj', 'v_proj'],
        'gate_up_proj': ['gate_proj', 'up_proj'],
    }
    def __init__(self, config, ctx_mgr: StepContextManager, dtype=None, device=None):
        super().__init__()
        self.config = config
        self.ctx_mgr = ctx_mgr
        self.dtype = dtype
        self.model = FooModel(config, dtype=dtype, device=device)
        self.lm_head = build_rowwise_linear(config.hidden_size, config.vocab_size, bias=False, dtype=dtype, device=device)
    # forward / get_logits / get_input_embeddings / prepare_inputs_for_generation / load_weights
    # 直接照抄 llama.py,只改类名与 stacked_params_mapping(若 HF 命名不同则调整)
```

**需要观察的现象**：骨架里**没有任何 attention/激活的数学实现**，全部是积木拼装；`packed_modules_mapping` 的值必须与你 step 2 查到的 HF 权重名后缀一致。

**预期结果**：得到一个结构上与 `llama.py` 等价、仅类名与字段不同的 `foo.py`。

**待本地验证**：骨架中的 `...` 部分需照抄 `llama.py` 对应方法的实现才能真正运行；`stacked_params_mapping` 的 `shard_id` 顺序（Q→`'q'`、K→`'k'`、V→`'v'`，gate→`0`、up→`1`）写错会**静默产出错误结果**，务必核对。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `forward` 返回 `hidden_states` 而不是 logits？`get_logits` 有什么用？

**参考答案**：解耦计算与投影。`forward` 只算到隐状态，`get_logits` 再经 `lm_head` 投影到词表。这样投机解码、聚合（pooling）、奖励模型等场景能复用隐状态而不必每次都投影到庞大的词表维度，节省计算与显存。

**练习 2**：`packed_modules_mapping`（类属性）和 `stacked_params_mapping`（`load_weights` 内局部变量）都描述「qkv 由 q/k/v 组成」，为什么需要两份？

**参考答案**：用途不同、生命周期不同。`packed_modules_mapping` 是**类属性**，供引擎在运行时寻址（如 LoRA 找挂载点、`update_quant_config` 改 ignored_layers 名），是「这个模型的结构说明书」；`stacked_params_mapping` 是 `load_weights` 内的**局部变量**，仅在**灌权重那一刻**把 HF 的分片权重名重定向到打包参数并标 shard_id，灌完即弃。两者描述同一事实，但服务于不同阶段。

**练习 3**：`llama.py` 的 `LlamaForCausalLM` 没有继承 `DeployModelMixinV1`，它是怎么实现 `get_logits` 的？

**参考答案**：它手写了 `get_logits`（[第 346-349 行](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/llama.py#L346-L349)），逻辑与 `DeployModelMixinV1.get_logits`（[utils/model.py 第 118-124 行](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/utils/model.py#L118-L124)）等价。新模型可以选择继承 `DeployModelMixinV1` 来省去这份样板代码。

### 4.3 archs 接入：后端选择与任务路由

#### 4.3.1 概念说明

`module_map` 解决了「PyTorch 后端用哪个重写类」，但在到达它之前，还有两个更早的路由决策由 `archs.py` 完成：

1. **用哪个后端**（`pytorch` 还是 `turbomind`）？——由 `autoget_backend` 决定。
2. **用哪种任务/Pipeline 类**（纯文本 `AsyncEngine` 还是多模态 `VLAsyncEngine`）？——由 `get_task` 决定。

对一个**纯文本**的新 PyTorch 模型，`archs.py` 通常**不需要改动**——因为它不在 TurboMind 白名单内，`autoget_backend` 会自动回退到 pytorch；`check_vl_llm` 返回 False，`get_task` 会选 `AsyncEngine`。这就是为什么前面强调「最小修改 = 写 models/xxx.py + 改 module_map.py 两步」。

但有**两种例外**需要在 `archs.py` 动手：

- **VLM（视觉语言模型）**：必须把 arch 名加入 `check_vl_llm` 的 `supported_archs` 集合，否则引擎会把多模态模型当纯文本跑，视觉输入被丢弃。
- **希望被 TurboMind 也支持**：这属于 TurboMind 后端的工作（白名单 `SUPPORTED_ARCHS` + C++ 实现），不在本讲范围。

此外，若新模型的 HF `config.json` 是**嵌套/非标准**结构（如多模态模型的 `thinker_config.text_config`），还要在 `lmdeploy/pytorch/configurations/` 下加一个 config builder，否则引擎读不到正确的字段。

#### 4.3.2 核心流程

从用户调 `pipeline(model_path)` 到选中 PyTorch 后端重写类的决策链（详见 u2-l5、u3-l1）：

```
pipeline(model_path)
  -> autoget_backend_config(model_path, backend_config)
       -> 若传了 PytorchEngineConfig: 直接短路返回 'pytorch'  (强制 PyTorch 的入口)
       -> 否则 autoget_backend(model_path):
            turbomind_has = is_supported_turbomind(model_path)  # 查 TurboMind 白名单
            backend = 'turbomind' if turbomind_has else 'pytorch'
  -> get_task(backend, model_path):  # 选 AsyncEngine 还是 VLAsyncEngine
       -> get_model_arch(model_path):  # 从 config.json 取 arch 名
            优先级: architectures[0] > auto_map > language_config.auto_map
       -> check_vl_llm(backend, config.to_dict()):  # arch 是否在 VLM 白名单
            命中 -> VLAsyncEngine, task='vlm'
            否则 -> AsyncEngine,   task='llm'
  -> AsyncEngine.__init__: 按 backend 构造 self.engine (PyTorch Engine)
  -> Engine 启动 -> build_patched_model -> 查 module_map -> 实例化重写类
```

关键洞察：**arch 名在这条链上被查询了三次**——`get_model_arch` 取它、`check_vl_llm` 比它、`module_map` 查它。同一条 `architectures` 字符串贯穿全栈，是名副其实的「模型身份证」。

#### 4.3.3 源码精读

[lmdeploy/archs.py:12-53](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/archs.py#L12-L53) — `autoget_backend`。它尝试 import TurboMind 的 `is_supported`，若 TurboMind 未装或模型不在白名单，就 fallback 到 pytorch 并打印告警。**新 PyTorch 模型默认走这里回退到 pytorch，无需改动**。

[lmdeploy/archs.py:56-92](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/archs.py#L56-L92) — `autoget_backend_config`。第 74-75 行：**用户显式传 `PytorchEngineConfig` 是强制走 PyTorch 的唯一短路入口**（不管 TurboMind 是否支持）。第 88-91 行：跨后端字段搬运（`block_size` ↔ `cache_block_seq_len`）。

[lmdeploy/archs.py:143-165](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/archs.py#L143-L165) — `get_model_arch`。三级兜底提取 arch 名：`architectures[0]` → `auto_map['AutoModelForCausalLM']` → `language_config.auto_map[...]`。这是整条链的起点。

[lmdeploy/archs.py:95-122](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/archs.py#L95-L122) — `check_vl_llm` 与 `supported_archs` 集合（第 102-112 行）。**VLM 接入的必改点**：新 VLM 的 arch 名必须加进这个集合，否则 `get_task` 选不到 `VLAsyncEngine`。注意它还处理了几种特殊情况（`MultiModalityCausalLM`、带 `vision_config` 的 ChatGLM、DeepseekV2 语言配置等）。

[lmdeploy/archs.py:125-140](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/archs.py#L125-L140) — `get_task`。`language_model_only` 可强制走纯文本 `AsyncEngine`（即使模型是 VLM）；否则据 `check_vl_llm` 在 `AsyncEngine` 与 `VLAsyncEngine` 间选择。

**非标准 config 的兜底——configurations builder**：

[lmdeploy/pytorch/configurations/builder.py:9-40](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/configurations/builder.py#L9-L40) — `AutoModelConfigBuilder`。它用 `__init_subclass__`（第 17-21 行）实现**自动注册**：任何继承它的子类一旦被 import 就自动登记到 `_sub_classes`，无需手动改注册中心。新模型只需写一个子类、声明 `condition(hf_config)`（按 `model_type` 匹配）和 `build(...)`，并在引擎初始化路径上确保该文件被 import 即可。**标准 config 的模型跳过这一步**，由 `DefaultModelConfigBuilder`（`configurations/default.py`）自动处理。

#### 4.3.4 代码实践

**实践目标**：手动追踪一个本地 HF 模型目录的 arch 名，并预测它会走哪条路由。

**操作步骤**：

1. 准备任意一个本地 HF 模型目录（或用 `Qwen/Qwen2.5-7B-Instruct` 等）。
2. 运行以下脚本（**示例代码**），打印 arch 名、后端选择与任务类型：

```python
# 示例代码: 探查 archs 路由结果
from lmdeploy.archs import get_model_arch, check_vl_llm, autoget_backend
model_path = '<你的本地模型目录>'
arch, cfg = get_model_arch(model_path)
backend = autoget_backend(model_path)
is_vl = check_vl_llm(backend, cfg.to_dict())
print(f'arch      = {arch}')
print(f'backend   = {backend}')
print(f'is_vlm    = {is_vl}')
print(f'task      = {"vlm" if is_vl else "llm"}')
```

3. 对照 `module_map.py`，确认你的 arch 名是否已在 `MODULE_MAP` 里——若在，说明已被支持；若不在，则是你要接入的新模型。

**需要观察的现象**：`arch` 与 `config.json` 的 `architectures[0]` 完全一致；纯文本模型 `is_vlm=False`；不在 TurboMind 白名单时 `backend='pytorch'` 并伴随告警日志。

**预期结果**：你能对任意模型目录，预测它会走 PyTorch 还是 TurboMind、纯文本还是 VLM、命中哪个重写类。

**待本地验证**：若你没有 GPU 或未安装 lmdeploy，可改为纯源码阅读——打开模型的 `config.json`，人工比照 `check_vl_llm` 的 `supported_archs` 与 `module_map.py`，得出同样结论。

#### 4.3.5 小练习与答案

**练习 1**：一个纯文本的新模型，接入时 `archs.py` 一定要改吗？

**参考答案**：不用。纯文本模型默认会被 `autoget_backend` 回退到 pytorch（因为不在 TurboMind 白名单），`check_vl_llm` 返回 False 使 `get_task` 选 `AsyncEngine`。`archs.py` 只在接入 **VLM**（需加入 `supported_archs`）或想让 TurboMind 也支持时才需改。

**练习 2**：用户如何「不管模型是否被 TurboMind 支持，强制走 PyTorch 后端」？

**参考答案**：在 `pipeline(model_path, backend_config=PytorchEngineConfig(...))` 里显式传 `PytorchEngineConfig`。`autoget_backend_config` 第 74-75 行检测到该类型会直接短路返回 `'pytorch'`，跳过 TurboMind 白名单检查。

**练习 3**：`AutoModelConfigBuilder` 的子类为何不需要在别处 import 就能生效？

**参考答案**：因为它用 `__init_subclass__` 钩子实现自动注册——子类定义的那一刻就把自己加入 `_sub_classes` 列表。前提是该子类所在文件在引擎初始化路径上被 import 过（`configurations` 包的 `__init__.py` 会统一 import 全部 builder）。

### 4.4 全流程清单：接入一个新 PyTorch 模型

把上面三个模块串成一张可照做的清单。对一个**纯文本、标准 config** 的 LLM，通常只需前两步；VLM 与量化是可选项。

| 步骤 | 文件 | 必做? | 说明 |
| --- | --- | --- | --- |
| 1. 写重写类 | `lmdeploy/pytorch/models/<model>.py` | ✅ 必做 | 五层结构（Attention/MLP/DecoderLayer/Model/ForCausalLM）+ 引擎协议方法（forward/get_logits/prepare_inputs_for_generation/load_weights）+ `packed_modules_mapping` |
| 2. 注册 arch | `lmdeploy/pytorch/models/module_map.py` | ✅ 必做 | 加一行 `MODULE_MAP.update({'XxxForCausalLM': '...xxx.XxxForCausalLM'})`，arch 名与 HF `config.json` 逐字符一致 |
| 3. 配置 builder | `lmdeploy/pytorch/configurations/<model>.py` | ⬜ 仅非标准 config | 嵌套 config 或需字段重映射时才写；标准 config 由 `DefaultModelConfigBuilder` 自动处理 |
| 4. VLM 预处理 | `lmdeploy/vl/model/<model>.py` | ⬜ 仅 VLM | 实现 `VisionModel` 子类，在 `vl/model/builder.py` 加 import |
| 5. VLM 路由 | `lmdeploy/archs.py` | ⬜ 仅 VLM | 把 arch 名加入 `check_vl_llm` 的 `supported_archs` |
| 6. 量化映射 | `lmdeploy/lite/apis/calibrate.py`、`lmdeploy/lite/quantization/awq.py` | ⬜ 可选 | 需要 AWQ/SmoothQuant 校准时才加层映射（见 u7-l1、u7-l2） |

> **三个最易踩的坑**（来自仓库内置 `.claude/skills/support-new-model/SKILL.md` 的总结）：① `packed_modules_mapping`/`stacked_params_mapping` 的 HF 权重名后缀必须与检查点里**真实**名字一致——动手前先 `list(model.state_dict().keys())` 核对；② QKV 的 shard 顺序必须是 Q→K→V，写错会**静默产出错误结果**；③ VLM 的 arch 名必须与 `hf_config.architectures[0]` 逐字相等（如 `Qwen3VLForConditionalGeneration`，不是 `Qwen3VL`）。

## 5. 综合实践

**任务**：为一个虚构的新模型 `FooForCausalLM` 写出**完整的接入方案**，要求覆盖从诊断到落码的全过程。

**背景**：假设 `FooForCausalLM` 是一个标准 Llama 式 dense LLM，HF 仓库里 `config.json` 的 `architectures` 为 `["FooForCausalLM"]`，含常规字段；权重文件里 attention 用 `q_proj/k_proj/v_proj/o_proj`，MLP 用 `gate_proj/up_proj/down_proj`。它**不是** VLM，config 是**标准扁平**结构。

**要求你产出**：

1. **诊断结论**：用本讲 4.3.4 的脚本（或人工查表）回答——它会走哪个后端？哪个任务？目前 `module_map.py` 里有没有它？（预期：pytorch / llm / 没有）
2. **改动清单**：列出**必做**的文件与改动（预期只需 2 个文件：新建 `models/foo.py`、改 `module_map.py` 加一行）。明确指出 `archs.py`、`configurations/`、`vl/model/` **本次都不用动**，并说明理由。
3. **最小重写类骨架**：参照本讲 4.2.4 的骨架，给出 `foo.py` 的完整类结构（`FooAttention`/`FooMLP`/`FooDecoderLayer`/`FooModel`/`FooForCausalLM`），重点写出：
   - `packed_modules_mapping`（与 Llama 一致）；
   - `load_weights` 内的 `stacked_params_mapping`（Q/K/V 用 `'q'/'k'/'v'`，gate/up 用 `0/1`）；
   - `forward` 返回 `hidden_states` 而非 logits；
   - 明确标注哪些方法可以「照抄 `llama.py`」。
4. **验证步骤**：写出接入后如何验证——
   - 用 `LMDEPLOY_LOG_LEVEL=DEBUG python -m lmdeploy.pytorch.chat <model_path> --backend pytorch` 观察权重加载日志，确认无 missing/unexpected key；
   - 跑一次推理确认输出合理；
   - 给出如果日志出现 `Can not found rewrite for architectures` / `missing keys` 时分别该排查哪里（前者查 `module_map.py` 的 arch 名拼写；后者查 `stacked_params_mapping` 的命名与 shard 顺序）。

**交付物**：一份 Markdown 笔记，包含上述四点。这是你把 u3（Patch 机制）与本讲（接入流程）融会贯通的标志。

## 6. 本讲小结

- **三件事各司其职**：接入新 PyTorch 模型 = **注册**（`module_map.py` 加一行 arch→qualname 映射）+ **实现**（`models/xxx.py` 五层重写类 + 引擎协议方法）+ **路由**（`archs.py`，纯文本通常无需改，VLM 需登记）。
- **arch 名是模型身份证**：同一条 `architectures` 字符串在 `get_model_arch`（取）、`check_vl_llm`（比）、`module_map`（查）被使用了三次，必须与 HF `config.json` 逐字符一致。
- **纯文本标准模型的最小改动是两个文件**：新建 `models/<model>.py`、在 `module_map.py` 加一行；`archs.py`/`configurations/`/`vl/` 在非 VLM、非标准 config 时不用动。
- **重写类 = 积木拼装**：attention/MLP 的数学全在 `nn/` 积木与 `backends/` kernel 里，重写文件只负责「拼装 + 拓扑」，按 `quant_config` 自动选量化线性层实现，无需写 `if quant` 分支。
- **权重加载双契约**：`packed_modules_mapping`（类属性，供运行时寻址）与 `stacked_params_mapping`（`load_weights` 内，供灌权重时改名+标 shard_id）描述同一事实但服务不同阶段；HF 权重名后缀与 shard 顺序写错会**静默**出错。
- **可选基座**：`DeployModelMixinV1` 提供 `get_logits`/`update_weights`/`build_lm_head` 默认实现，`CudaGraphMixin` 提供 CUDA Graph 能力——新模型可按需继承以减少样板。

## 7. 下一步学习建议

本讲把「支持新模型」讲透了，接下来可以沿着几个方向深入：

- **MoE 模型接入**：若你的新模型是混合专家架构，`MLP` 要换成 MoE 层，需参考 `lmdeploy/pytorch/models/qwen3_moe.py` 与 u5-l3（MoE 模块与专家负载均衡），重点是 gate 路由与 `FusedMoE` 三段流水线。
- **量化对接**：若要让新模型支持 AWQ/SmoothQuant 校准，去读 `lmdeploy/lite/apis/calibrate.py` 的层映射表与 u7-l1、u7-l2，并对照本讲 4.2.3 提到的 `update_quant_config` 钩子，确保 `ignored_layers` 命名正确。
- **VLM 全链路**：若新模型是多模态，除本讲的 module_map 外，还要写 `vl/model/<model>.py` 预处理器、在 `vl/model/builder.py` 加 import、在 `archs.py` 登记arch，参考 u9-l1（视觉语言模型 VLM 处理）与 `lmdeploy/vl/model/qwen3.py`。
- **验证与测试**：学完接入流程后，建议阅读 u10-l3（测试体系与运行方式），学会用 `pytest tests/test_lmdeploy/` 与 `LMDEPLOY_LOG_LEVEL=DEBUG` 定位权重加载问题。
- **进一步精读积木**：本讲大量复用的 `nn.Attention`/`build_qkv_proj`/`RMSNorm` 等，其 impl 真身在 `backends/`，可结合 u5-l1（优化 nn 模块）与 u5-l4（算子后端分发）理解「薄包装 + 委托」桥接模式的全貌。
