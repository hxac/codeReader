# 模型注册与多模型适配

## 1. 本讲目标

上一篇（u5-l1）我们读完了所有 LLM 适配器的**模板方法基类** `BaseModel`：它固化了「逐层加载 + 逐层前向 + PTQ 单元划分」的通用骨架，把若干「插槽」留给子类补齐。本讲就来回答紧接着的下一个问题：

> AMCT 是怎么用**同一套骨架**同时支持 DeepSeek / Qwen / GLM / LongCat / 混元（HyV3）等十几个不同模型的？我要接入一个全新模型，到底要改哪里？

学完本讲你应该能够：

1. 看懂 `register_llm_models()` 的注册清单，说出每个适配器在 `MODEL_REGISTRY` 里的 `name`（路由键）、`family`（家族）以及是 dense（稠密）还是 MoE（混合专家）变体。
2. 复述 `--model_name` 如何经 `MODEL_REGISTRY.get(model_name)` 路由到具体适配器类。
3. 列出一个新适配器**必须覆写**的关键方法（`get_layer_weight_prefix` / `build_quant_block` / `parse_quant_mode` 等）与**按需覆写**的扩展点（`attn_norm_name` / `load_layer_weight` / `iter_deploy_bindings` 等）。
4. 读懂 MoE 适配的公共件 `moe_common.py`（`QuantGatedExperts` / `pack_gated_expert_weights`）和一个最复杂的真实案例 `HyV3`（权重键重映射）。

## 2. 前置知识

本讲默认你已经掌握下面两篇讲义的内容，不会重复：

- **u3-l3 注册表驱动的插件架构**：`Registry` 基类的 `register` / `get` / `list_all` 接口、装饰器写法、`force=True` 覆盖语义，以及「import 副作用注册」模式。
- **u5-l1 LLM 模型适配基类 BaseModel**：`BaseModel` 的三段式前向（embedding / block / head）、`iter_ptq_units` / `iter_deploy_bindings` 两套枚举、`Catcher`、`PtqUnit`、`PtqParamStore` 等术语。

补充几个本讲会用到的最小术语：

- **适配器（adapter）**：一个继承自 `BaseModel` 的类，专门负责「把某个 HuggingFace 模型结构对接进 AMCT 的量化主流程」。
- **dense（稠密）模型**：每个 transformer 层只有一个全连接 FFN（MLP），`quant_target` 用 `mlp`。
- **MoE（Mixture of Experts，混合专家）模型**：FFN 被替换成「路由门 + 多个专家 MLP」，`quant_target` 用 `moe`，每个专家各算一个 PTQ 单元。
- **权重键（checkpoint key）**：safetensors 里每个张量的名字，例如 `model.layers.0.self_attn.q_proj.weight`。不同模型家族的命名前缀差异，是适配器要处理的核心麻烦。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [amct_pytorch/common/models/__init__.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/__init__.py) | 定义全局 `MODEL_REGISTRY = Registry("model")`，是所有适配器的注册表。 |
| [amct_pytorch/common/models/llm/__init__.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/__init__.py) | `register_llm_models()`——靠 import 副作用把所有适配器登记进 `MODEL_REGISTRY`，带 `_REGISTERED` 幂等保护。 |
| [amct_pytorch/common/models/llm/common/base.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/base.py) | `BaseModel` 模板方法基类（u5-l1 已精读，本讲只引用它的「插槽」默认值）。 |
| [amct_pytorch/common/models/llm/qwen/qwen3/qwen3.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/qwen/qwen3/qwen3.py) | `Qwen3`——最精简的 dense 适配器，是「如何接入新模型」的标准模板。 |
| [amct_pytorch/common/models/llm/qwen/moe_common.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/qwen/moe_common.py) | MoE 公共件：`QuantGatedExperts`（专家量化包装）+ `pack_gated_expert_weights`（专家权重打包）。被 qwen3_moe / qwen3_5_moe / hyv3 复用。 |
| [amct_pytorch/common/models/llm/qwen/qwen3/qwen3_moe.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/qwen/qwen3/qwen3_moe.py) | `Qwen3Moe`——在 dense 基础上叠 MoE 处理的适配器。 |
| [amct_pytorch/common/models/llm/hyv3/hyv3.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/hyv3/hyv3.py) | `HyV3`——腾讯混元 V3 适配器，演示权重键重映射 + tensor 粒度 deploy 的完整覆写。 |
| [amct_pytorch/workflows/llm_ptq.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/workflows/llm_ptq.py) | PTQ 工作流，`_build_pipeline()` 里用 `MODEL_REGISTRY.get(model_name)` 取适配器，是路由的消费方。 |

## 4. 核心概念与源码讲解

### 4.1 注册清单、家族目录与 model_name 路由

#### 4.1.1 概念说明

AMCT 要支持很多模型，但 PTQ 主流程（eval / extract / ptq / deploy）只有一套。把「一套流程」与「N 个模型」解耦的关键就是**注册表 + 适配器**：

- 每个模型写一个适配器类，用装饰器 `@MODEL_REGISTRY.register(name=..., family=...)` 把自己登记进全局注册表。
- 工作流不关心具体是哪个模型，只需拿到 `model_name` 字符串，调 `MODEL_REGISTRY.get(model_name)` 取出对应的类，再实例化即可。

这样**新增模型只动适配器，主流程一行都不用改**——这就是 u3-l3 讲的「注册表驱动插件架构」在模型层的落地。

#### 4.1.2 核心流程

注册与路由的完整链路如下：

```text
启动
  └─ Workflow.setup() 第一行 _register_components()
        └─ register_llm_models()                    # ① import 副作用注册
              └─ from .qwen.qwen3.qwen3 import Qwen3
                    └─ 类定义执行 → @MODEL_REGISTRY.register(name="qwen3") 装饰器
                          └─ MODEL_REGISTRY._items["qwen3"] = RegistryItem(...)
  └─ Workflow._build_pipeline()
        └─ model_cls = MODEL_REGISTRY.get(self.model_name)   # ② 用 name 取类
        └─ return model_cls(self.args)                       # ③ 实例化适配器
```

两步缺一不可：① 先注册（把所有适配器塞进注册表），② 后取用（按 `model_name` 取出）。顺序由 `setup()` 保证（详见 u3-l2）。

#### 4.1.3 源码精读

**注册表本体**只有一个名字为 `"model"` 的 `Registry` 实例：

[amct_pytorch/common/models/__init__.py:18-22](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/__init__.py#L18-L22) 定义并导出 `MODEL_REGISTRY`。注册表的 `get()` 在 key 不存在时会抛 `KeyError` 并列出所有可用 key（u3-l3 已讲）。

**`register_llm_models()`** 本身一行实现逻辑都没有，全部是 `import`：

[amct_pytorch/common/models/llm/__init__.py:21-40](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/__init__.py#L21-L40) 函数体里 13 行 `from .xxx import XxxClass  # noqa: F401`，每行的副作用就是触发对应类上方 `@MODEL_REGISTRY.register(...)` 装饰器执行，从而完成登记。文件级 `_REGISTERED` 标志（[第 18 行](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/__init__.py#L18)）保证幂等——重复调用直接 return，不会重复注册或报「already registered」。

把这 13 个 import 整理成注册清单（按家族归类）：

| `name`（路由键 / `--model_name`） | 家族 `family` | 类 | 类型 | 源文件 |
| --- | --- | --- | --- | --- |
| `deepseek_v3_2` | deepseek | `DeepseekV32` | dense | `deepseek/deepseek_v3_2/deepseekv3_2.py` |
| `deepseek_v4` | deepseek | `DeepseekV4` | MoE | `deepseek/deepseek_v4/deepseekv4.py` |
| `longcat_lite` | longcat | `LongcatLite` | dense | `longcat/longcat_lite/longcat_lite.py` |
| `longcat_next` | longcat | `LongcatNext` | MoE | `longcat/longcat_next/longcat_next.py` |
| `qwen3` | qwen | `Qwen3` | dense | `qwen/qwen3/qwen3.py` |
| `qwen3_moe` | qwen | `Qwen3Moe` | MoE | `qwen/qwen3/qwen3_moe.py` |
| `qwen3_next` | qwen | `Qwen3Next` | dense | `qwen/qwen3_next/qwen3_next.py` |
| `qwen3_5` | qwen | `Qwen3_5` | dense | `qwen/qwen3_5/qwen3_5.py` |
| `qwen3_5_moe` | qwen | `Qwen3_5Moe` | MoE | `qwen/qwen3_5/qwen3_5_moe.py` |
| `qwen3_6_moe` | qwen | `Qwen3_6Moe` | MoE | `qwen/qwen3_6/qwen3_6_moe.py` |
| `glm5` | glm | `GLM5` | dense | `glm/glm5/glm5.py` |
| `glm5_2` | glm | `GLM5_2` | MoE | `glm/glm5_2/glm5_2.py` |
| `hy_v3` | hyv3 | `HyV3` | MoE | `hyv3/hyv3.py` |

这张表里有两条贯穿全讲的规律：

- **家族 = 顶层目录**：`common/models/llm/` 下每个一级子目录（`deepseek` / `longcat` / `qwen` / `glm` / `hyv3`）就是一个家族，目录名与装饰器里的 `family` 字段对应；家族内每个模型变体再开一个二级子目录。
- **dense / MoE 成对出现**：同一个家族往往既有 dense 适配器又有 MoE 适配器（如 qwen 的 `qwen3`+`qwen3_moe`、`qwen3_5`+`qwen3_5_moe`）。dense 只支持 `quant_target=mlp`，MoE 只支持 `quant_target=moe`。

**路由消费方**在四条工作流里都一样，以 PTQ 为例：

[amct_pytorch/workflows/llm_ptq.py:49](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/workflows/llm_ptq.py#L49) 把命令行参数原样存为 `self.model_name`；[amct_pytorch/workflows/llm_ptq.py:136-138](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/workflows/llm_ptq.py#L136-L138) 的 `_build_pipeline()` 用 `MODEL_REGISTRY.get(self.model_name)` 取类并实例化。eval / extract / deploy 三个工作流的 `_build_pipeline()` 是同构的（[llm_eval.py:82-84](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/workflows/llm_eval.py#L82-L84)、[llm_extract_ptq_data.py:67-69](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/workflows/llm_extract_ptq_data.py#L67-L69)、[llm_deploy.py:108-110](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/workflows/llm_deploy.py#L108-L110)）。

> **关于 `--model_name` 的取值（重要、易踩坑）**：`self.model_name` 被原样当作注册表的 key，**没有做任何归一化**。因此 `--model_name` 必须填上表里的 `name`（如 `qwen3_5`、`hy_v3`），而不是 HuggingFace 模型路径。examples 脚本统一传注册键来印证，例如 [examples/eval.sh:24](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/examples/eval.sh#L24) 的 `--model_name qwen3_5`；而真正的模型文件路径是另一个参数 `--model`（在适配器里读作 `self.model_path = self.args.model`，见 [base.py:64](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/base.py#L64)）。注意 [args.py:46-50](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/cli/llm/args.py#L46-L50) 里 `--model_name` 的默认值是一个形如模型路径的占位串，不覆盖它直接跑会在 `MODEL_REGISTRY.get()` 处抛 `KeyError`。

#### 4.1.4 代码实践

1. **实践目标**：亲眼看到「注册前后注册表的变化」，理解 import 副作用注册。
2. **操作步骤**：
   ```bash
   cd <仓库根目录>
   python -c "from amct_pytorch.common.models import MODEL_REGISTRY; \
print('注册前:', MODEL_REGISTRY.list_all()); \
from amct_pytorch.common.models.llm import register_llm_models; \
register_llm_models(); \
print('注册后:', MODEL_REGISTRY.list_all())"
   ```
3. **观察现象**：第一行打印 `注册前: []`（注册表为空，因为还没触发 import）；第二行打印 13 个 key。
4. **预期结果**：`注册后` 列表应包含 `deepseek_v3_2, deepseek_v4, glm5, glm5_2, hy_v3, longcat_lite, longcat_next, qwen3, qwen3_5, qwen3_5_moe, qwen3_6_moe, qwen3_moe, qwen3_next`（按字母序）。再执行一次 `register_llm_models()` 应**不报错也不新增**（幂等保护）。
5. 若运行环境缺少 `transformers` / `compressed_tensors` 等依赖而 import 失败，则标注「待本地验证」并改用纯源码阅读：在 [llm/__init__.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/__init__.py) 里逐行核对 import 清单与上表是否一致。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `register_llm_models()` 里要用 `# noqa: F401` 标注这些 import？

> **参考答案**：这些 import 的目的不是「在函数内使用这个名字」，而是**借类定义触发的装饰器副作用**完成注册，函数体内并不会引用 `Qwen3` 这个名字。不加 `# noqa: F401`，linter 会按「导入了但未使用」报错。

**练习 2**：如果把一个适配器文件的 import 行从 `register_llm_models()` 里删掉，会发生什么？

> **参考答案**：该类不会被定义、装饰器不会执行，`MODEL_REGISTRY` 里就没有对应的 key；运行时 `MODEL_REGISTRY.get(该 name)` 会抛 `KeyError` 并在错误信息里列出当前所有可用 key（u3-l3 讲过的友好报错）。

### 4.2 适配器要覆写什么：Qwen3 dense 模板

#### 4.2.1 概念说明

`BaseModel` 是模板方法基类：通用流程（加载 config/tokenizer、逐层前向、PTQ 单元划分、断点续跑）都在父类里固化，把「**因模型而异**」的部分抽成若干方法，让子类按需覆写。这些方法分两类：

- **必须覆写（插槽）**：父类要么 `raise NotImplementedError`，要么返回无意义默认值，子类不补就跑不通。典型是 `get_layer_weight_prefix`、`build_quant_block`。
- **按需覆写（扩展点）**：父类提供了 HuggingFace 通用约定的默认实现，只有当你的模型偏离了这个约定时才需要覆写。典型是 `attn_norm_name` / `ffn_norm_name`、`load_layer_weight`、`iter_deploy_bindings`、`_embed_base_prefix`。

`Qwen3`（[qwen3.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/qwen/qwen3/qwen3.py)）是一个**完全遵循 HuggingFace 约定**的 dense 模型，它的适配器代码量极少——这就是「接入新 dense 模型」的标准模板。

#### 4.2.2 核心流程

一个 dense 适配器的 `__init__` 必须完成「四件套」初始化，随后主流程就靠父类驱动：

```text
Qwen3.__init__(args):
  super().__init__(args)              # 父类已建好 config / tokenizer / PtqParamStore
  self.textconfig = Qwen3Config       # ① transformers 的 Config 类
  self.num_layers  = config.num_hidden_layers   # ② 层数
  self.cls        = Qwen3DecoderLayer # ③ 单层构造器（用于 block(layer_idx)）
  self.model      = empty_weights_model()       # ④ meta 空壳骨架
  parse_quant_mode()                  # ⑤ 校验 quant_target 与模型类型匹配
```

之后父类的 `block()` 会用 `self.cls(config, layer_idx)` 造层、用 `get_layer_weight_prefix()` 拿权重键前缀去 safetensors 里读权重；`do_block_forward` 等流程全部复用父类。

#### 4.2.3 源码精读

先看 `BaseModel` 留出的插槽默认值，理解「为什么必须覆写」：

- [base.py:290-291](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/base.py#L290-L291) `get_layer_weight_prefix` 默认 `pass`（返回 `None`）——每个模型层的 checkpoint 前缀不同，必须覆写。
- [base.py:231-232](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/base.py#L231-L232) `build_quant_block` 默认只是 `return self.block(layer_idx)`（不挂任何量化）——必须覆写才能把 `quant_target` 指定的子模块换成量化版本。
- [base.py:53-54](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/base.py#L53-L54) `attn_norm_name = "input_layernorm"` / `ffn_norm_name = "post_attention_layernorm"` 是 HuggingFace 约定的默认值——只有命名不同的模型才需要覆写（如 DeepseekV4）。

再看 `Qwen3` 怎么填这些插槽。注册装饰器声明 `name="qwen3"`、`family="qwen"`、`description` 是给注册表存的元数据：

[qwen3.py:37-43](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/qwen/qwen3/qwen3.py#L37-L43) 装饰器与类定义。

[qwen3.py:44-51](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/qwen/qwen3/qwen3.py#L44-L51) `__init__` 完成上面说的「四件套 + 校验」。`self.cls = Qwen3DecoderLayer` 直接复用 `transformers` 自带的 Qwen3 单层实现（[第 19-22 行](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/qwen/qwen3/qwen3.py#L19-L22) 的 import）——只要模型已在 transformers 里实现，适配器就不必重写前向。

[qwen3.py:52-54](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/qwen/qwen3/qwen3.py#L52-L54) `parse_quant_mode`：dense 模型遇到 `moe` 直接报错——这是「quant_target 必须与模型类型一致」的早期校验。

[qwen3.py:62-63](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/qwen/qwen3/qwen3.py#L62-L63) `get_layer_weight_prefix` 返回 `f"model.layers.{layer_idx}."`——这是几乎所有 HuggingFace causal LM 的通用前缀。

[qwen3.py:96-102](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/qwen/qwen3/qwen3.py#L96-L102) `build_quant_block`：先 `self.block(layer_idx)` 造出浮点原始层，再按 `quant_target` 把它的子模块包装成量化版本——attn 目标调 `apply_quant_to_attn(..., QuantQwen3Attn)`，mlp 目标调 `apply_quant_to_moe_mlp(..., cls=QuantQwen3MLP)`。这两个包装函数与量化模块类（`QuantQwen3Attn`/`QuantQwen3MLP`）来自 `quant_apply`（详见 u5-l3）与同目录的 `quant_module.py`。

注意 `Qwen3` 里大量方法只是 `return super().xxx(...)`（如 [第 56-94 行](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/qwen/qwen3/qwen3.py#L56-L94) 的 `float_model`/`do_block_forward`/`iter_ptq_units` 等）——它们其实可以直接删掉，全部回退到父类默认。保留它们主要是为了可读性与未来 hook 点。真正「非覆写不可」的只有 `__init__` / `get_layer_weight_prefix` / `build_quant_block` / `parse_quant_mode` 四个。

#### 4.2.4 代码实践

1. **实践目标**：识别「必须覆写」与「按需覆写」的差异，能删冗余。
2. **操作步骤**：打开 [qwen3.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/qwen/qwen3/qwen3.py)，把所有方法分两类——A 类「函数体只有 `return super().xxx(...)`」（可删），B 类「有实质逻辑」（必须保留）。
3. **观察现象**：把 A 类方法在脑子里删掉，类还能正常工作吗？
4. **预期结果**：`float_model` / `empty_weights_model` / `block` / `do_embedding_forward` / `do_block_forward` / `do_head_forward` / `iter_ptq_units` / `iter_deploy_bindings` / `load_*` 全是 A 类（纯转发），可删；`__init__` / `parse_quant_mode` / `get_layer_weight_prefix` / `build_quant_block` / `bits_scheme` 是 B 类（删了就跑不通或 deploy 出错）。
5. 这是源码阅读型实践，不修改源码；结论可写进自己的笔记。

#### 4.2.5 小练习与答案

**练习 1**：`Qwen3` 没有覆写 `attn_norm_name` / `ffn_norm_name`，extract 阶段怎么知道该 hook 哪个 norm？

> **参考答案**：工作流从适配器实例上读这两个属性：`getattr(self.pipeline, "attn_norm_name", "input_layernorm")`（见 [llm_extract_ptq_data.py:73-76](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/workflows/llm_extract_ptq_data.py#L73-L76)）。`Qwen3` 没覆写，于是沿用父类默认值 `input_layernorm` / `post_attention_layernorm`，正好是 HuggingFace Qwen3 的命名。

**练习 2**：为什么 `Qwen3.parse_quant_mode()` 只检查 `"moe" in self.quant_target`，不检查 `attn-linear` / `attn-cache`？

> **参考答案**：`mlp` 与 `moe` 是互斥的 FFN 量化目标（dense 只有 mlp、MoE 只有 moe），二者必须与模型结构匹配，所以要校验；而 `attn-linear` / `attn-cache` 作用于注意力，dense 和 MoE 都有注意力，对所有模型都合法，无需在此校验。

### 4.3 MoE 适配：moe_common 与 qwen3_moe

#### 4.3.1 概念说明

MoE 模型的麻烦在于 FFN 变成了「门控 + N 个专家」。两个工程难点：

1. **权重布局**：很多 MoE checkpoint 把 N 个专家的 `gate_proj/up_proj/down_proj` 堆叠成两个大张量（`gate_up_proj`、`down_proj`）以省存储，但 transformers 的 `load_state_dict` 期望的是「每个专家独立的 key」。需要在加载时**把堆叠权重拆回每专家视图**。
2. **量化单元**：MoE 的 PTQ 是**逐专家**进行的（u4-l2 讲过每个 expert 一个 `PtqUnit`），需要把每个专家包装成独立的可量化 MLP，并在 deploy 时把量化后的专家名映射回 checkpoint 的堆叠 key。

`qwen/moe_common.py` 就是把这两个共性能力抽出来的**家族级公共件**，被 `qwen3_moe` / `qwen3_5_moe` / `hyv3` 等多个适配器复用。

#### 4.3.2 核心流程

MoE 适配在 dense 适配器之上叠加三件事：

```text
① 加载层权重时：pack_gated_expert_weights(state_dict)
     输入: mlp.experts.{i}.gate_proj.weight / up_proj.weight / down_proj.weight （每专家分开）
     输出: mlp.experts.gate_up_proj  [num_experts, 2*inter, hidden]  （cat(gate,up) 再 stack 专家）
           mlp.experts.down_proj     [num_experts, hidden, inter]    （stack 专家）
     → 让 transformers 的 Qwen3MoeDecoderLayer 能 strict load

② build_quant_block 时：把 mlp.experts 换成 QuantGatedExperts
     QuantGatedExperts 内部为每个专家建一个 QuantGatedMLP（经 GatedExpertView 视图访问堆叠权重）

③ iter_deploy_bindings 时：把模块名 mlp.experts.expert_modules.{i}.{proj}
     重映射回 checkpoint 键 mlp.experts.{i}.{proj}.weight
```

#### 4.3.3 源码精读

**`QuantGatedExperts`**：包装一个「已加载的堆叠 experts 模块」，为每个专家生成一个 `QuantGatedMLP`。

[moe_common.py:25-54](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/qwen/moe_common.py#L25-L54) 构造函数把 `experts_module` 存为 `self.packed_experts`，再用 `nn.ModuleList` 为每个专家建一个 `QuantGatedMLP`——其输入是一个 `GatedExpertView(..., materialize=False)`，即**不复制权重、只是给堆叠张量开一个「第 i 个专家」的视图**（省内存）。`group` 参数（默认 `"moe.routed"`）是位宽分组键，喂给 `BitPolicy` 决定该专家的 `w_bits/a_bits`（见 u3-l4）。

[moe_common.py:56-71](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/qwen/moe_common.py#L56-L71) `build_ptq_expert_module` / `iter_ptq_expert_modules`：PTQ 阶段需要「一次只把一个专家物化到显存」做训练，这里用 `materialize=True` 逐个产出独立的专家模块，正是 u4-l2 讲的「MoE 每专家一个 PtqUnit」的供给方。

**`pack_gated_expert_weights`**：把「每专家独立 key」重排成「堆叠 key」。

[moe_common.py:105-143](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/qwen/moe_common.py#L105-L143)：用正则 `mlp\.experts\.(\d+)\.(gate_proj|up_proj|down_proj)\.weight$` 抓出每个专家的三组权重（[第 110-112 行](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/qwen/moe_common.py#L110-L112)），校验三者专家集合一致（[第 132-133 行](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/qwen/moe_common.py#L132-L133)），然后 `torch.cat([gate, up], dim=0)` 再 `torch.stack(专家, dim=0)` 产出 `gate_up_proj`，`down_proj` 直接 stack（[第 135-142 行](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/qwen/moe_common.py#L135-L142)）。若一个专家 key 都没匹配到，原样返回（[第 128-129 行](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/qwen/moe_common.py#L128-L129)），这样 dense 层不受影响。

[moe_common.py:146-147](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/qwen/moe_common.py#L146-L147) `is_packed_experts` 用「同时有 `gate_up_proj` 和 `down_proj` 两个属性」判定一个 experts 模块是不是堆叠布局——qwen3_5_moe 就是靠它决定要不要包 `QuantGatedExperts`。

**`Qwen3Moe`**：在 dense `Qwen3` 之上叠加 MoE 三件事。

[qwen3_moe.py:39-45](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/qwen/qwen3_moe.py#L39-L45) 注册 `name='qwen3_moe'`、继承 `BaseModel`（注意它直接继承 `BaseModel`，不是继承 `Qwen3`）。

[qwen3_moe.py:57-61](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/qwen/qwen3_moe.py#L57-L61) `parse_quant_mode`：MoE 模型遇到 `mlp` 直接报错（与 dense 互锁）。

[qwen3_moe.py:72-75](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/qwen/qwen3_moe.py#L72-L75) `load_layer_weight` 覆写：先调父类读原始 state_dict，再调 `pack_gated_expert_weights` 把专家权重堆叠化——这是上面流程①。

[qwen3_moe.py:105-115](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/qwen/qwen3_moe.py#L105-L115) `build_quant_block`：当 `quant_target=moe` 时，把 `decoder_layer.mlp.experts` 替换成 `QuantGatedExperts(...)`（流程②）；若该层没有 experts（比如某些 MoE 模型的某些层是 dense FFN），退回 `QuantQwen3MLP`。

[qwen3_moe.py:120-137](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/qwen/qwen3_moe.py#L120-L137) `iter_deploy_bindings` 覆写（流程③）：deploy 烘焙时，量化模块在模型里的名字是 `mlp.experts.expert_modules.{i}.{proj}`（`QuantGatedExperts` 建的 ModuleList），但 checkpoint / 部署权重里的键是 `mlp.experts.{i}.{proj}.weight`。这里用 `name.split(".")` 拆出 `expert_idx` 与 `proj_name`，重组成正确的 checkpoint 键；非专家的 `QuantLinear` 走父类默认拼接。

#### 4.3.4 代码实践

1. **实践目标**：验证 `pack_gated_expert_weights` 的输入输出形状契约。
2. **操作步骤**：阅读 [moe_common.py:105-143](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/qwen/moe_common.py#L105-L143)，画一张「3 个专家、每专家 gate_proj[I,H]、up_proj[I,H]、down_proj[H,I]」前后对照表。
3. **观察现象**：`gate_proj` 与 `up_proj` 是怎么合并的？为什么 `down_proj` 不参与 cat、只 stack？
4. **预期结果**：合并后只有两个键——`mlp.experts.gate_up_proj` 形状 `[3, 2*I, H]`（先 cat(gate,up) 沿 dim=0 得 `[2I, H]`，再 stack 3 个专家得 `[3, 2I, H]`），`mlp.experts.down_proj` 形状 `[3, H, I]`。`down_proj` 不 cat 是因为它在 MLP 里是独立的第二个线性层（gate/up 之后、激活之后），没有可配对的投影。
5. 若想在本地实跑，可用 `torch.zeros` 伪造一个含 `mlp.experts.0/1/2.{gate_proj,up_proj,down_proj}.weight` 的 dict 喂给该函数验证键名与形状（标注「待本地验证」若无 NPU/GPU）。

#### 4.3.5 小练习与答案

**练习 1**：`QuantGatedExperts.__init__` 里建 `QuantGatedMLP` 时传了 `GatedExpertView(..., materialize=False)`，PTQ 时又用 `build_ptq_expert_module(materialize=True)`。这两个 `materialize` 为何不同？

> **参考答案**：`materialize=False` 只建视图、不复制权重，用于 forward/eval 时让所有专家共享同一份堆叠权重（省内存）；`materialize=True` 会真正切出单个专家的权重副本，用于 PTQ 训练——训练要把单个专家独立搬到设备上做前向反向，不能只靠视图。

**练习 2**：`Qwen3Moe.iter_deploy_bindings` 为什么要覆写，而 `Qwen3Moe.iter_ptq_units` 直接 `yield from super()`？

> **参考答案**：父类 `iter_ptq_units` 已经能处理 MoE——它检测到 `experts.iter_ptq_expert_modules()` 就逐专家产出 PtqUnit（见 [base.py:306-319](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/base.py#L306-L319)），所以不用覆写。但 `iter_deploy_bindings` 父类默认会把模块名原样拼成 checkpoint 键，而 `QuantGatedExperts` 内部的模块名带 `expert_modules.` 中缀，与 checkpoint 布局不符，必须覆写做重映射。

### 4.4 复杂适配示例：HyV3 权重键映射

#### 4.4.1 概念说明

当模型的 checkpoint 命名与 transformers 的实现命名**对不上**时，适配器就得在加载阶段做「键重映射（remap）」。`HyV3`（腾讯混元 V3）就是这种最复杂的情况：它的原生前缀（`mlp.router.gate`、`mlp.shared_mlp`、`mlp.expert_bias`）与 transformers `HYV3DecoderLayer` 期望的命名（`mlp.gate`、`mlp.shared_experts`、`mlp.e_score_correction_bias`）不同，所以要在 `load_layer_weight` 里改键名，再走 MoE 打包。

HyV3 同时演示了**tensor 粒度 deploy**的完整覆写（`generate_tensorwise_quant_layers` / `bits_scheme` / `cache_scheme`），是「一个适配器能写多深」的样板。

#### 4.4.2 核心流程

HyV3 加载一层权重的处理链：

```text
load_layer_weight(prefix):
  state_dict = super().load_layer_weight(prefix)   # 从 safetensors 原样读出
  state_dict = remap_hyv3_keys(state_dict)         # ① 改键名（router.gate→gate 等）
  if "mlp.experts.0.gate_proj.weight" in state_dict:
      state_dict = pack_gated_expert_weights(...)  # ② 复用 4.3 的 MoE 打包
  return state_dict
```

`build_quant_block` 则按层是否带 `experts` 属性分流：有 experts 走 `QuantHYV3MoE`，没有走 dense 的 `QuantHYV3MLP`。

#### 4.4.3 源码精读

**注册**用了 `force=True`：

[hyv3.py:55-61](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/hyv3/hyv3.py#L55-L61) `name="hy_v3"`、`family="hyv3"`、`force=True`。`force=True` 表示即便该 key 已存在也强制覆盖（u3-l3 讲过的覆盖语义）——用于容忍重复注册或后续覆盖默认实现。

**键重映射函数**：

[hyv3.py:38-52](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/hyv3/hyv3.py#L38-L52) `remap_hyv3_keys` 是纯字符串替换，三组映射：
- `mlp.router.gate.weight` → `mlp.gate.weight`（路由门）
- `mlp.expert_bias` → `mlp.e_score_correction_bias`（专家修正偏置）
- `mlp.shared_mlp.` → `mlp.shared_experts.`（共享专家）

**加载层权重**串起 remap + pack：

[hyv3.py:87-97](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/hyv3/hyv3.py#L87-L97) `load_layer_weight`：先 `super().load_layer_weight(prefix)` 读原始张量，再 `remap_hyv3_keys`，最后若发现专家键就调 `pack_gated_expert_weights`（**直接复用 4.3 讲的 qwen moe_common**——这就是把公共件抽出来的好处）。注意 HyV3 把 `parse_quant_mode` 设为「不支持 `mlp`、只能 `moe`」（[第 81-85 行](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/hyv3/hyv3.py#L81-L85)），因为它是纯 MoE 模型。

**build_quant_block** 按结构分流：

[hyv3.py:99-113](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/hyv3/hyv3.py#L99-L113) 检测 `mlp` 是否有 `experts` 属性——有则换成 `QuantHYV3MoE`（内部用 `QuantGatedExperts` 处理专家 + `QuantHYV3MLP` 处理 shared expert），没有则把整个 `mlp` 换成 `QuantHYV3MLP(..., group="mlp")`。这种「同一适配器内 dense 层 / MoE 层混存」的处理，比纯 MoE 的 qwen3_moe 更通用。

**iter_deploy_bindings** 重映射专家名（与 qwen3_moe 同构）：

[hyv3.py:118-133](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/hyv3/hyv3.py#L118-L133) 把 `mlp.experts.expert_modules.{i}.{proj}` 拆解重组为 `mlp.experts.{i}.{proj}.weight`，逻辑与 [qwen3_moe.py:120-137](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/qwen/qwen3_moe.py#L120-L137) 完全一致——又一次体现「MoE 部署重映射」是可以抽象的共性（只是当前各家族各写一份）。

**tensor 粒度 deploy 的元数据**：

[hyv3.py:140-162](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/hyv3/hyv3.py#L140-L162) `generate_tensorwise_quant_layers` 枚举每一层每个待量化 Linear（attn 的 q/k/v/o、每个 expert 的 gate/down/up、shared expert），从 `BitPolicy` 取各自分组的 `w` 位宽，组成 `{层名: 位宽}` 字典；[hyv3.py:164-171](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/hyv3/hyv3.py#L164-L171) `generate_tensorwise_ignore_layers` 列出不量化的层（第 0 层 mlp、embed_tokens、lm_head 等）。这两个方法供 u4-l4 的 tensor 粒度 deploy 直接消费。

[hyv3.py:173-184](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/hyv3/hyv3.py#L173-L184) `bits_scheme` 产出 deploy config 的分组位宽（`Linear` 用全局 w/a、`MoEGMM` 用 `moe.routed` 的 w/a），把 `BitPolicy`（u3-l4）的查询结果落到 compressed-tensors 格式。

#### 4.4.4 代码实践

1. **实践目标**：把一个适配器的「加载链」与「deploy 链」对齐，理解键重映射为何必要。
2. **操作步骤**：
   - 读 [hyv3.py:38-52](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/hyv3/hyv3.py#L38-L52)，列出三组 remap 映射。
   - 读 [hyv3.py:87-97](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/hyv3/hyv3.py#L87-L97)，确认 `remap` 在 `pack` 之前。
3. **观察现象**：如果调换顺序（先 pack 再 remap）会怎样？
4. **预期结果**：`pack_gated_expert_weights` 的正则匹配的是 `mlp.experts.{i}.{gate_proj|up_proj|down_proj}.weight`（4.3 讲过）。若先 remap，`shared_mlp` 已被改成 `shared_experts`，但专家键 `mlp.experts.{i}.xxx` 不在三组 remap 里、键名不变，所以理论上对专家 pack 无影响；但 `router.gate` / `expert_bias` 这些非专家键必须先 remap 才能被 transformers 正确 strict-load。因此**顺序应是 remap→pack**：先让所有键名对齐 transformers 期望，再做专家堆叠。反过来虽然专家 pack 可能仍成功，但 `router.gate` 等键会因名字不对导致 `load_state_dict(strict=True)` 报缺键错。
5. 这是源码阅读型推理实践；可在本地用伪造 state_dict 调 `remap_hyv3_keys` 验证键名变化（标注「待本地验证」）。

#### 4.4.5 小练习与答案

**练习 1**：HyV3 的 `load_layer_weight` 复用了 `qwen/moe_common.py` 的 `pack_gated_expert_weights`。一个 hyv3 家族的适配器依赖 qwen 家族的代码，这合理吗？

> **参考答案**：合理。`pack_gated_expert_weights` 处理的是「门控专家 MLP 的通用堆叠布局」（gate+up cat、down stack），与具体家族无关，本质是**跨家族的公共能力**，只是恰好放在了 qwen 目录下。理想情况下这类公共件可上移到 `common/`，但现状是按「最先复用的家族」就近放置，HyV3 直接 import 复用。这也提示读者：阅读时别被目录名限制，公共件可能在任意家族下。

**练习 2**：HyV3 的 `generate_tensorwise_quant_layers` 里，`num_layers = num_hidden_layers + num_nextn_predict_layers`（[第 146 行](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/hyv3/hyv3.py#L146)）。多出来的 `num_nextn_predict_layers` 是什么？

> **参考答案**：HyV3 是带「下一 token 预测头（Next-N prediction）」的多头架构，除了主干的 `num_hidden_layers` 个 decoder 层外，还有 `num_nextn_predict_layers` 个预测层，这些层里的 Linear 同样需要被 deploy 枚举到量化名单里，所以总层数要把两部分相加。这正说明：**当模型有特殊结构时，适配器必须在 deploy 元数据里如实反映**，否则这些层会被 deploy 漏掉。

## 5. 综合实践

**任务**：为一个假想的新 Qwen dense 变体（假设叫 `Qwen3-Z`，注册键 `qwen3_z`）规划接入 AMCT 的完整改动清单，并用源码证据支撑每一条。

要求产出一份「接入清单」，包含但不限于：

1. **目录与文件**：参照 [qwen3.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/qwen/qwen3/qwen3.py) 的位置，新建哪个目录、哪几个 `.py`（适配器主文件 + `quant_module.py`）。
2. **继承与注册**：继承 `BaseModel`；写出 `@MODEL_REGISTRY.register(name="qwen3_z", task="llm", family="qwen", description="...")` 装饰器。说明如果 transformers 里已有 `Qwen3ZDecoderLayer` / `Qwen3ZConfig`，可以直接 import 复用（参考 [qwen3.py:19-22](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/qwen/qwen3/qwen3.py#L19-L22)）。
3. **必须覆写的方法**：`__init__`（四件套：textconfig / num_layers / cls / empty_weights_model + parse_quant_mode）、`get_layer_weight_prefix`（写明前缀是 `model.layers.{idx}.` 还是别的）、`build_quant_block`（attn 用哪个 `QuantXxxAttn`、mlp 用哪个 `QuantXxxMLP`）、`parse_quant_mode`（dense 拒绝 `moe`）。
4. **按需覆写**：如果 `Qwen3-Z` 的 pre-attn norm 不叫 `input_layernorm`，需要覆写类属性 `attn_norm_name`（参考 [DeepseekV4 的 `attn_norm = "attn_norm"`](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/deepseek/deepseek_v4/deepseekv4.py#L64-L65)）；如果是多模态嵌套在 `model.language_model.` 下，需覆写 `_embed_base_prefix`（参考 [qwen3_5.py:141-142](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/qwen/qwen3_5/qwen3_5.py#L141-L142)）。
5. **注册登记**：在 [llm/__init__.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/__init__.py) 的 `register_llm_models()` 里补一行 `from .qwen.qwen3_z.qwen3_z import Qwen3Z  # noqa: F401`。
6. **验证**：跑一遍 4.1.4 的注册检查脚本，确认 `qwen3_z` 出现在 `MODEL_REGISTRY.list_all()` 里；再用 `--model_name qwen3_z` 跑 eval 命令确认能取到适配器。

预期产出：一张「文件 / 改动点 / 参考源码」三列表，能作为真实接入新模型的 checklist。本实践不要求真有 `Qwen3-Z` 权重，重点是走通「读清单→定位插槽→写最小适配器→登记」的完整心法。

## 6. 本讲小结

- **路由**：四条工作流统一用 `MODEL_REGISTRY.get(self.model_name)` 取适配器类并实例化（[llm_ptq.py:136-138](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/workflows/llm_ptq.py#L136-L138)）；`--model_name` 填的是注册键（如 `qwen3_5`），不是模型路径，且无归一化。
- **注册清单**：`register_llm_models()` 靠 13 行 import 副作用登记 13 个适配器（[llm/__init__.py:21-40](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/__init__.py#L21-L40)），分 deepseek / longcat / qwen / glm / hyv3 五个家族，dense 与 MoE 成对出现，靠 `_REGISTERED` 幂等保护。
- **覆写分层**：`get_layer_weight_prefix` / `build_quant_block` / `parse_quant_mode` 必须覆写；`attn_norm_name` / `load_layer_weight` / `iter_deploy_bindings` / `_embed_base_prefix` 等是按需覆写的扩展点。`Qwen3` 是最薄 dense 模板，大量方法只是 `super()` 转发。
- **MoE 公共件**：`moe_common.py` 的 `QuantGatedExperts`（专家量化视图包装）与 `pack_gated_expert_weights`（专家权重堆叠）被 qwen3_moe / qwen3_5_moe / hyv3 复用；MoE 适配器还要覆写 `iter_deploy_bindings` 把 `expert_modules.` 中缀重映射回 checkpoint 键。
- **复杂适配**：当 checkpoint 命名与 transformers 实现不一致时，在 `load_layer_weight` 里做键重映射（HyV3 的 `remap_hyv3_keys`），再做 MoE 打包；tensor 粒度 deploy 还要补 `generate_tensorwise_quant_layers` / `bits_scheme` / `cache_scheme` 等元数据。
- **继承复用**：家族内用继承减少重复（如 `Qwen3_6Moe → Qwen3_5Moe → Qwen3_5`、`GLM5 → DeepseekV32`），新变体应优先继承同家族已有适配器而非从 `BaseModel` 重写。

## 7. 下一步学习建议

- **u5-l3 量化算子挂载 quant_apply**：本讲反复出现的 `apply_quant_to_attn` / `apply_quant_to_moe_mlp` / `QuantGatedMLP` 到底怎么把原始 `Linear` 换成量化对应物，去 `quant_apply.py` 一探究竟。
- **u4-l4 部署导出 deploy**：本讲提到的 `iter_deploy_bindings` / `bits_scheme` / `generate_tensorwise_quant_layers` 是 deploy 的输入，结合 deploy workflow 看「适配器产出的绑定如何被烘焙成 safetensors」。
- **u6-l2 算法注册与 target 路由机制**：`QuantGatedMLP` 里按 `group` 选位宽、挂算法，其背后的 `weight/activation/structure` 三类 target 路由是下一篇的核心。
- **动手尝试**：照「综合实践」的清单，在本地仓库为一个真实存在但尚未接入的 transformers 模型（如某 Llama 变体）写一个最小适配器骨架并登记注册，用 4.1.4 的脚本验证它能出现在 `MODEL_REGISTRY.list_all()` 中——这是检验你是否真正掌握本讲的最快方式。
