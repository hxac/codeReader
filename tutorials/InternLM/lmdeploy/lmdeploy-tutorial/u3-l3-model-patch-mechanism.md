# 模型 Patch 重写机制

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 LMDeploy 的 PyTorch 后端为什么要「重写」HuggingFace 模型，以及重写发生在哪个阶段。
- 读懂 `module_map.py`：`MODULE_MAP` 如何把 HF 的架构名（arch）映射到 LMDeploy 自己的实现类。
- 跟踪 `patch.py` 中从 `ModelConfig` 到一个可执行模型实例的构建主链路：`build_patched_model → build_model_from_hf_config → _get_model_class`。
- 理解 `get_rewrite_cls` / `_get_rewrite_qualname` 的 qualname 级（子模块级）替换机制及其正则兜底。
- 认识三张「特殊映射表」：`DEVICE_SPECIAL_MODULE_MAP`（按设备覆盖）、`CUSTOM_MODULE_MAP`（用户自定义扩展点）、`REMOVED_MODEL_MAP`（已移除模型家族）。

## 2. 前置知识

### 2.1 什么是「arch 名」

HuggingFace 模型目录里有一份 `config.json`，其中 `architectures` 字段标注了「这个模型用哪个类来实例化」，例如 `["LlamaForCausalLM"]` 或 `["Qwen3ForCausalLM"]`。这个字符串就是**架构名（arch）**，可以理解为模型的「身份证」。在 u2-l5 中你已经学过：LMDeploy 用 `get_model_arch` 把它从 `config.json` 里取出来，作为后端选择的依据。本讲会看到，同一个 arch 名还会再次出现在 `MODULE_MAP` 里，用来决定「用哪个重写类」。

### 2.2 为什么 HF 的模型不能直接拿来推理

PyTorch 后端的策略**不是**「加载 HF 原模型后逐层改」，而是「在实例化时直接换成 LMDeploy 自己的实现」。原因是：

- LMDeploy 需要**Paged Attention**（分块 KV 缓存）、**张量并行**、**量化线性层（AWQ/W8A8/FP8）**、**CUDA Graph** 等能力，HF 原生 `nn.Linear` / `LlamaAttention` 都不支持。
- 如果等 HF 加载完再逐个 `setattr` 替换，会多走一遍无用的原生初始化、还要处理大量边界情况。

所以 LMDeploy 选择**在「造」模型这一步就调包**：读到 arch 名后，不实例化 `transformers.LlamaForCausalLM`，而是实例化 `lmdeploy.pytorch.models.llama.LlamaForCausalLM`——一个名字相同、内部完全不同的优化实现。这就是本讲的「Patch 重写机制」。

> 术语提示：CLAUDE.md 把这套机制描述为「loads HF models normally, then dynamically replaces their layers」，描述的是**设计意图**；本讲会带你读到真实代码，看清当前主链路其实是「整模型级直接替换 + 子模块级 qualname 查表」两条路径并存。

### 2.3 qualname 是什么

`qualname`（全限定名）= `模块路径.类名`，例如 `transformers.models.llama.modeling_llama.LlamaAttention`。`patch.py` 用它在注册表里查找「这个子模块该换成谁」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [lmdeploy/pytorch/models/module_map.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/module_map.py) | **映射注册表**。定义 `MODULE_MAP`（arch → 实现类）、设备专属表、自定义表、已移除模型表。 |
| [lmdeploy/pytorch/models/patch.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/patch.py) | **替换执行器**。提供 qualname 查表、动态导入、整模型构建主链路、自定义表加载等函数。 |
| [lmdeploy/pytorch/engine/model_agent/agent.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/model_agent/agent.py) | **调用方**。引擎的 `_build_model` 在这里调用 `build_patched_model`，把模型造出来后再加载权重、加 LoRA。 |
| [lmdeploy/pytorch/models/llama.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/llama.py) / [qwen3.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/qwen3.py) | **重写实现**的典型范例，即 `MODULE_MAP` 指向的落地类。 |

## 4. 核心概念与源码讲解

### 4.1 Patch 机制总览：两条替换路径

#### 4.1.1 概念说明

「Patch」在 LMDeploy 里有两种粒度，理解它们的区别是读懂 `patch.py` 的钥匙：

- **整模型级替换（主链路，必然执行）**：根据 `config.json` 的 arch 名，直接查 `MODULE_MAP`，实例化一个完整的 LMDeploy 模型类。函数入口 `build_patched_model` / `build_model_from_hf_config` / `_get_model_class`。
- **子模块级替换（qualname 查表机制）**：给定任意一个 `nn.Module`，按它的 qualname/类名查表，找出可替换的 LMDeploy 实现类。函数入口 `get_rewrite_cls` / `_find_rewrite_module_qualname` / `_get_rewrite_qualname`。

两者**共用同一张表**（`MODULE_MAP`），但查询键不同：整模型级用 arch 名精确匹配；子模块级用 qualname 三级兜底匹配并支持正则。

#### 4.1.2 核心流程

整模型构建主链路（从引擎侧调用算起）：

```text
model_agent/agent.py :: _build_model
        │  构造 BuildModelContext（打包量化/LoRA/CUDA Graph 等开关）
        ▼
patch.py :: build_patched_model(ModelConfig)        # 薄封装：取出 hf_config + dtype
        ▼
patch.py :: build_model_from_hf_config(hf_config)   # 合并 module_map、选类、实例化
        │   ├─ _get_module_map()    # MODULE_MAP ⊕ 设备表 ⊕ 自定义表
        │   ├─ _get_model_class()   # 按 arch/auto_map 查表，动态 import 类
        │   └─ model_cls(hf_config) # 直接实例化 LMDeploy 实现类
        ▼
返回一个 model.eval()（随后由 weight_loader 灌权重）
```

#### 4.1.3 源码精读

调用方在 [agent.py:1101-1131](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/model_agent/agent.py#L1101-L1131)：先用 `model_config.custom_module_map` 装载用户自定义映射，再构造 `BuildModelContext`，最后调 `build_patched_model`。关键一行：

```python
patched_model = build_patched_model(self.model_config, device=device, build_model_ctx=build_model_ctx)
```

紧接着 `load_model_weights` 把权重灌进这个「空壳但结构正确的」patched model，`add_adapters` 再挂上 LoRA。这印证了 2.2 的结论——**先换类、后灌权重**。

#### 4.1.4 代码实践

1. **目标**：确认主链路调用点与上下文。
2. **步骤**：打开 [agent.py:1123](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/model_agent/agent.py#L1123)，向上看 `BuildModelContext` 被塞了哪些字段，向下看 `load_model_weights`、`add_adapters` 的顺序。
3. **观察**：`build_patched_model` 返回的模型此刻**没有真实权重**（除非 `empty_init=False`，见 [agent.py:1125-1126](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/model_agent/agent.py#L1125-L1126)）。
4. **预期**：理解「造壳 → 灌权重 → 挂适配器」三段式，与本讲后续的「换类」职责对齐。

#### 4.1.5 小练习与答案

- **练习**：为什么 LMDeploy 不直接 `AutoModelForCausalLM.from_pretrained(...)` 加载原模型后再替换？
- **答案**：原生 `from_pretrained` 会用 HF 的 `nn.Linear` / Attention 初始化一遍参数，既浪费内存又可能与量化/Paged Attention/张量并行的布局冲突；换成「直接实例化优化类 + 一次性灌权重」更省、更可控。

---

### 4.2 MODULE_MAP 注册表：arch 名到实现类的映射

#### 4.2.1 概念说明

`MODULE_MAP` 是整个重写机制的「字典真相源」：键是 arch 名（或 auto_map 名），值是 LMDeploy 实现类的 qualname 字符串。它是**纯数据**，没有逻辑——添加一个新模型，往往只需在这里多写一行映射（详见 u10-l1）。

#### 4.2.2 核心流程

注册表的组织方式：

```text
LMDEPLOY_PYTORCH_MODEL_PATH = 'lmdeploy.pytorch.models'   # 前缀常量，避免重复写
MODULE_MAP = dict()                                        # 主表，逐个 .update() 填充
# 每个模型一行：
#   'LlamaForCausalLM' -> 'lmdeploy.pytorch.models.llama.LlamaForCausalLM'
```

注意一个设计巧思：值是**字符串 qualname** 而不是类对象。这样 `module_map.py` 不必在 import 时就把 40+ 个模型文件全部加载进来，只在真正需要某类时才由 `_class_from_qualname` 延迟 `importlib.import_module`，缩短启动时间、避免循环导入。

#### 4.2.3 源码精读

前缀常量与主表声明在 [module_map.py:3-11](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/module_map.py#L3-L11)：

```python
LMDEPLOY_PYTORCH_MODEL_PATH = 'lmdeploy.pytorch.models'
MODULE_MAP = dict()
ASCEND_MODULE_MAP = dict()
MACA_MODULE_MAP = dict()
CAMB_MODULE_MAP = dict()
DEVICE_SPECIAL_MODULE_MAP = dict(ascend=ASCEND_MODULE_MAP, maca=MACA_MODULE_MAP, camb=CAMB_MODULE_MAP)
```

本讲实践要找的两个映射（直接精确对应）：

- Llama：[module_map.py:24-26](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/module_map.py#L24-L26) — `'LlamaForCausalLM'` → `lmdeploy.pytorch.models.llama.LlamaForCausalLM`
- Qwen3（稠密）：[module_map.py:139-141](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/module_map.py#L139-L141) — `'Qwen3ForCausalLM'` → `lmdeploy.pytorch.models.qwen3.Qwen3ForCausalLM`

一个有趣的「复用」现象：多个 arch 可以指向同一个实现。例如 DeepSeek-V3 复用 V2 的实现（[module_map.py:113](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/module_map.py#L113)），Gemma2/Gemma3 文本都复用 `gemma.GemmaForCausalLM`（[module_map.py:89-96](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/module_map.py#L89-L96)）。这说明实现类的抽象程度足以覆盖同族变体。

#### 4.2.4 代码实践

1. **目标**：从注册表直接读出 Llama / Qwen3 的映射目标，并动态导入对比「替换前后的类」。
2. **步骤**（这段不依赖 GPU、不下载权重，可直接 `python` 运行）：

   ```python
   # 示例代码：直接查注册表 + 动态导入
   from lmdeploy.pytorch.models.module_map import MODULE_MAP
   from lmdeploy.pytorch.models.patch import _class_from_qualname

   for arch in ['LlamaForCausalLM', 'Qwen3ForCausalLM']:
       qualname = MODULE_MAP[arch]                 # arch -> qualname 字符串
       cls = _class_from_qualname(qualname)        # 字符串 -> 真实类对象
       print(f'{arch:24s} -> {qualname}  ({cls})')
   ```
3. **观察**：`qualname` 指向的是 `lmdeploy.pytorch.models.*` 命名空间下的类，而非 `transformers.*`。
4. **预期结果**：两个 arch 都解析成功；类名与 HF 同名（这是有意为之，便于权重键对齐），但 `__module__` 不同，内部是 LMDeploy 的优化实现。

#### 4.2.5 小练习与答案

- **练习 1**：`DeepseekV3ForCausalLM` 在 `MODULE_MAP` 中指向哪个文件？这说明 V3 与 V2 是什么关系？
- **答案**：指向 `lmdeploy.pytorch.models.deepseek_v2.DeepseekV2ForCausalLM`（[module_map.py:113](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/module_map.py#L113)），说明 V3 结构兼容 V2，复用同一套重写实现。
- **练习 2**：为什么 `MODULE_MAP` 的值是字符串而不是类？
- **答案**：延迟导入。避免 import `module_map` 时连带加载全部 40+ 模型文件，降低启动开销、规避循环依赖。

---

### 4.3 build_patched_model：模型构建主链路

#### 4.3.1 概念说明

`build_patched_model` 是引擎真正调用的「造模型」入口。它做三件事：① 把用户侧 `ModelConfig` 拆成 `hf_config` 与 `dtype`；② 合并出最终生效的 `module_map`；③ 按 arch 选出类并实例化。它的返回值随后被灌权重。

#### 4.3.2 核心流程

```text
build_patched_model(config: ModelConfig)
        │  model_config = config.hf_config ; dtype = config.dtype
        ▼
build_model_from_hf_config(model_config, dtype, device, build_model_ctx)
        │
        ├─ module_map = _get_module_map()           # 三表合一
        ├─ model_cls  = _get_model_class(hf_config, module_map)
        │       ├─ 优先 config.auto_map['AutoModelForCausalLM']
        │       └─ 否则遍历 config.architectures
        ├─ model_cls.update_quant_config(...)        # 可选钩子：修正量化忽略层
        └─ with build_model_context(build_model_ctx):
               model = model_cls(model_config, ctx_mgr, dtype, device)
        ▼
return model.eval()
```

`_get_model_class` 的查表优先级（[patch.py:165-196](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/patch.py#L165-L196)）：

1. 若 `config.auto_map` 含 `AutoModelForCausalLM`（常见于自定义/PEFT 模型），取其末段类名查表；
2. 否则遍历 `config.architectures`，第一个命中 `module_map` 的即采用；
3. 都没命中则抛 `RuntimeError('Can not found rewrite for ...')`。

此外，每查到一个 arch 都先过 `_raise_if_removed_model(arch)`，给已下线的模型家族一个清晰报错。

#### 4.3.3 源码精读

入口函数（[patch.py:220-225](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/patch.py#L220-L225)）只是薄封装：

```python
@torch.inference_mode()
def build_patched_model(config: ModelConfig, device=None, build_model_ctx=None):
    model_config = config.hf_config
    dtype = config.dtype
    return build_model_from_hf_config(model_config, dtype=dtype, device=device, build_model_ctx=build_model_ctx)
```

真正干活的是 [build_model_from_hf_config:199-217](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/patch.py#L199-L217)。其中三表合一逻辑在 [_get_module_map:106-115](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/patch.py#L106-L115)：

```python
def _get_module_map():
    module_map = MODULE_MAP.copy()                       # 1. 主表
    device_type = get_device_manager().current_context().device_type
    if device_type != 'cuda':
        module_map.update(DEVICE_SPECIAL_MODULE_MAP.get(device_type, dict()))  # 2. 设备覆盖
    module_map.update(CUSTOM_MODULE_MAP)                 # 3. 用户自定义（优先级最高）
    return module_map
```

注意合并顺序决定了**优先级**：`CUSTOM_MODULE_MAP` 最后 update，能覆盖前两者，这正是「用户自定义扩展点」的支撑（4.5 节展开）。

`update_quant_config` 钩子（[patch.py:212-213](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/patch.py#L212-L213)）让选定的模型类有机会修正量化配置——典型场景是把 HF 风格的 `.q_proj` 忽略层名改写成 LMDeploy 打包后的 `.qkv_proj`，定义见 [models/utils/model.py:80-81](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/utils/model.py#L80-L81)。

#### 4.3.4 代码实践

1. **目标**：跑通「arch → 实现类」的真实构造路径（需 GPU 与可用的 HF config）。
2. **步骤**：写一个最小脚本（**待本地验证**，因为构造会触发量化/后端 dispatch，通常需 CUDA 环境）：

   ```python
   # 示例代码（待本地验证）
   from transformers import AutoConfig
   from lmdeploy.pytorch.config import ModelConfig
   from lmdeploy.pytorch.models.patch import build_patched_model

   hf_config = AutoConfig.from_pretrained('Qwen/Qwen3-8B', trust_remote_code=True)
   # ModelConfig.from_pretrained 会补齐 dtype/dist_config 等，详见 u3-l2
   model_config = ModelConfig.from_pretrained('Qwen/Qwen3-8B')
   model = build_patched_model(model_config)            # device 默认 cuda
   print(type(model).__module__ + '.' + type(model).__name__)   # 应为 lmdeploy.pytorch.models.qwen3.Qwen3ForCausalLM
   print(type(model.model.layers[0].self_attn))                 # 子模块也已被换成 Qwen3Attention
   ```
3. **观察**：最外层类名与 HF 同名但模块路径是 `lmdeploy.pytorch.models.*`；内部的 attention/mlp 也已是 LMDeploy 的优化实现（如 [llama.py:24](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/llama.py#L24) 的 `LlamaAttention`）。
4. **预期**：`type(model)` 落在 `MODULE_MAP` 指向的类上，证明「整模型级直接替换」生效。若仅想验证查表逻辑而不构造模型，可只调用 `_get_model_class(hf_config, MODULE_MAP)`。

#### 4.3.5 小练习与答案

- **练习**：`_get_model_class` 为什么先看 `auto_map` 再看 `architectures`？
- **答案**：`auto_map` 用于 `trust_remote_code` 的自定义模型（如某些 PEFT/社区模型），其类名可能不在 `architectures` 里；先查它能覆盖这类模型，再用 `architectures` 兜底标准模型。两者都查不到才报「Can not found rewrite」。

---

### 4.4 get_rewrite_cls：qualname 级替换机制

#### 4.4.1 概念说明

`get_rewrite_cls` 是「子模块级」查表入口：给它任意一个 `nn.Module`，它返回「应当替换成的 LMDeploy 类」。这是 CLAUDE.md 所述「runtime layer 替换」的核心工具，与 4.3 的整模型构建互补。

#### 4.4.2 核心流程

`get_rewrite_cls` 的匹配采用**三级降级 + 正则兜底**策略（[_find_rewrite_module_qualname:61-93](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/patch.py#L61-L93)）：

```text
给定一个 model（nn.Module），取 module_name = inspect.getmodule(model).__name__，class_name = model.__class__.__name__

第 1 级  full name : '{module_name}.{class_name}'      # 最精确
第 2 级  class name: '{class_name}'                     # 退而求其次
第 3 级  submodname: '{尾段module}.{class_name}'        # 仅保留最后一段模块名

每一级都调 _get_rewrite_qualname(name, module_map)：
    先精确 in 判断；再遍历用 re.search(key, name) 做正则匹配
任一级命中即返回对应的 qualname；三级全空则返回 None
```

正则兜底的意义：注册表的键可以是 `'.*Attention'` 这类模式，从而用一条规则批量替换某类子模块，而不必逐个列出。

#### 4.4.3 源码精读

查表与动态导入两个原子函数：

- [_get_rewrite_qualname:24-38](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/patch.py#L24-L38)：先精确匹配，再正则。

  ```python
  if origin_qualname in module_map:          # 精确
      return module_map[origin_qualname]
  for key, value in module_map.items():      # 正则兜底
      if re.search(key, origin_qualname):
          return value
  return None
  ```

- [_class_from_qualname:41-58](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/patch.py#L41-L58)：用 `qualname.rfind('.')` 切出 `modname` 与 `clsname`，再 `importlib.import_module(modname)` + `getattr` 取类。这是「字符串 → 类对象」的统一出口，4.2.4 实践里也用到它。

- 顶层入口 [get_rewrite_cls:96-103](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/patch.py#L96-L103)：默认 `module_map=None` 时调 `_get_module_map()`（即三表合一），再走 `_find_rewrite_module_qualname`，最后 `_class_from_qualname` 取类。

> 真实性提示：在当前主分支，整模型构建走的是 `_get_model_class`（4.3），`get_rewrite_cls` 主要作为子模块级替换的公共工具保留，并为自定义/未来扩展提供统一查表语义。两者共享 `MODULE_MAP`。

#### 4.4.4 代码实践

1. **目标**：用 `get_rewrite_cls` 体验「给一个子模块 → 返回替换类」，观察三级匹配与正则兜底。
2. **步骤**（无需 GPU，但需 `transformers` 可 import）：

   ```python
   # 示例代码（待本地验证：需 transformers 提供原生类）
   import transformers
   from lmdeploy.pytorch.models.patch import get_rewrite_cls

   for cls in [transformers.LlamaForCausalLM, transformers.models.qwen3.Qwen3ForCausalLM]:
       # 取一个临时实例仅用于读取 __class__/模块信息（不分配大张量）
       # 这里用类本身的 __name__ 思路验证：get_rewrite_cls 需要实例，
       # 因此更轻量的做法是直接验证 _get_rewrite_qualname 的字符串匹配：
       from lmdeploy.pytorch.models.module_map import MODULE_MAP
       from lmdeploy.pytorch.models.patch import _get_rewrite_qualname
       full = f'{cls.__module__}.{cls.__name__}'
       print(full, '->', _get_rewrite_qualname(full, MODULE_MAP) or
                     _get_rewrite_qualname(cls.__name__, MODULE_MAP))
   ```
3. **观察**：对 `LlamaForCausalLM`，第 2 级「class name」命中；对带完整模块路径的，第 1 级「full name」也可能命中（取决于 `MODULE_MAP` 键形）。
4. **预期**：`get_rewrite_cls` 返回的类 `__module__` 都在 `lmdeploy.pytorch.models.*` 下；若注册表里既无精确键也无正则键命中，则返回 `None`（表示该子模块无需替换）。

#### 4.4.5 小练习与答案

- **练习 1**：三级匹配为什么按 full → class → submodname 的顺序？
- **答案**：从最具体到最宽泛，避免误伤同名子模块。full name 最精确；若注册表只写了裸类名（如 `'LlamaForCausalLM'`），第 2 级兜住；第 3 级处理「只记得尾段模块名」的注册风格。
- **练习 2**：`re.search(key, name)` 这种正则匹配有什么风险？
- **答案**：`re.search` 是子串/模式匹配，键若写得过宽（如 `'Llama'`）可能误匹配 `LlamaAttention`/`LlamaMLP` 等多个子模块。因此注册表的正则键需谨慎，通常配合 `^...$` 锚定。

---

### 4.5 设备特定映射、扩展点与已移除模型

#### 4.5.1 概念说明

除主表 `MODULE_MAP` 外，`module_map.py` 还维护三张辅助表，分别解决「换设备」「用户自己加模型」「模型已下线」三个问题。它们共同把 `MODULE_MAP` 变成一个可扩展、可裁剪的注册中心。

#### 4.5.2 核心流程

三表在生效路径上的位置（`_get_module_map` 与 `_get_model_class`）：

```text
最终生效表 = MODULE_MAP
           ⊕ DEVICE_SPECIAL_MODULE_MAP[当前 device_type]   # 非 cuda 时覆盖
           ⊕ CUSTOM_MODULE_MAP                              # update_custom_module_map 注入，优先级最高

查表时：每个 arch 先过 _raise_if_removed_model(arch)
        命中 REMOVED_MODEL_MAP → 抛清晰错误，提示用旧版本或迁移
```

#### 4.5.3 源码精读

- **设备专属表**：[module_map.py:7-11](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/module_map.py#L7-L11) 声明 `ASCEND/MACA/CAMB` 三套表，并由 `DEVICE_SPECIAL_MODULE_MAP` 按设备名索引。`device_type` 来自 [devices/device_manager.py:9-10](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/devices/device_manager.py#L9-L10)（默认 `'cuda'`）。只有 `device_type != 'cuda'` 时才 `update` 覆盖（见 [_get_module_map:109-112](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/patch.py#L109-L112)）——这样同一套 arch 名在昇腾等设备上可指向该设备专用的实现类。

- **已移除模型表**：[module_map.py:13-21](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/module_map.py#L13-L21) 列出 `InternLMForCausalLM`、`QWenLMHeadModel`、`Baichuan` 等已下线家族。触发点 [_raise_if_removed_model:156-162](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/patch.py#L156-L162) 在 `_get_model_class` 内对每个 arch 调用，给出「请用旧版本或迁移到新模型」的明确报错，而不是含糊的「can not found rewrite」。

- **自定义扩展表 `CUSTOM_MODULE_MAP`**：[module_map.py:288](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/module_map.py#L288) 声明为空 dict，由 [update_custom_module_map:118-153](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/patch.py#L118-L153) 在运行时从用户提供的文件加载。引擎侧的接入点是 [agent.py:1107-1109](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/model_agent/agent.py#L1107-L1109)：当 `model_config.custom_module_map` 不为空时调用它。它的工作是：用 `SourceFileLoader` 把用户 `.py` 当模块加载，读取其中的 `MODULE_MAP`/`CUSTOM_MODULE_MAP`，把不含 `.` 的值补成 `{LMDEPLOY_PYTORCH_MODEL_PATH}._custom_mod.{v}` 前缀，再 `CUSTOM_MODULE_MAP.update(...)`。由于它最后合并，**用户映射可覆盖官方映射**——这是不改 lmdeploy 源码即可支持新模型的关键扩展点（u10-l1 详述）。

#### 4.5.4 代码实践

1. **目标**：验证设备表与已移除表的生效条件。
2. **步骤（源码阅读型）**：
   - 在 [module_map.py:13-21](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/module_map.py#L13-L21) 找出 `REMOVED_MODEL_MAP` 至少 3 个键，对照 `_raise_if_removed_model` 说明会抛什么错。
   - 在 `_get_module_map` 里确认：`device_type == 'cuda'` 时**不会**合并设备表。
3. **观察**：已移除模型如果不在 `MODULE_MAP` 里、却单独维护在 `REMOVED_MODEL_MAP`，是为了给出**可操作的迁移建议**而非通用「未找到」错误。
4. **预期**：能复述「cuda 不走设备表；非 cuda 才用设备专属实现覆盖；自定义表永远合并且优先级最高」。

#### 4.5.5 小练习与答案

- **练习 1**：为什么 `DEVICE_SPECIAL_MODULE_MAP` 只在 `device_type != 'cuda'` 时合并？
- **答案**：CUDA 是默认/主战场，其实现已在 `MODULE_MAP` 内；设备表只在昇腾/MACA/CAMB 等需要不同实现时覆盖，避免在 cuda 上误用非 cuda 实现。
- **练习 2**：用户要让一个全新模型 `XxxForCausalLM` 走自定义实现，最轻量的做法是什么？
- **答案**：写一个 `.py`，里面定义 `MODULE_MAP = {'XxxForCausalLM': '你的实现类qualname'}`（或指向 `_custom_mod` 内的类），通过 `PytorchEngineConfig` 的 `custom_module_map` 传路径，引擎会自动 `update_custom_module_map` 注入，无需改 lmdeploy 源码。

## 5. 综合实践

把本讲三条主线串起来：**注册表 → 构建主链路 → 扩展点**。

任务：在不下载大模型、不依赖 GPU 的前提下，完整跑通「arch 名 → 最终生效类」的纯查表链路，并人为触发一次自定义注入，观察优先级。

```python
# 示例代码（可直接 python 运行，纯 CPU/查表，不构造大模型）
from lmdeploy.pytorch.models import module_map as mm
from lmdeploy.pytorch.models.patch import _get_model_class, _class_from_qualname

# 1) 读主表
print('Llama  :', mm.MODULE_MAP['LlamaForCausalLM'])
print('Qwen3  :', mm.MODULE_MAP['Qwen3ForCausalLM'])

# 2) 模拟一个 hf_config 对象，只关心 architectures 字段
class FakeHfConfig:
    architectures = ['LlamaForCausalLM']
    auto_map = {}
cls = _get_model_class(FakeHfConfig(), mm.MODULE_MAP.copy())
print('resolved:', cls.__module__ + '.' + cls.__name__)
assert cls.__module__.startswith('lmdeploy.pytorch.models')

# 3) 演示 CUSTOM_MODULE_MAP 覆盖（用占位 qualname，仅验证 update 优先级，不真导入）
mm.CUSTOM_MODULE_MAP.update({'LlamaForCausalLM': 'lmdeploy.pytorch.models.qwen3.Qwen3ForCausalLM'})
from lmdeploy.pytorch.models.patch import _get_module_map
final_map = _get_module_map()   # 注意：需在 device 上下文中调用，否则可能取默认 cuda
print('after override, Llama ->', final_map['LlamaForCausalLM'])
# 清理，避免污染后续
mm.CUSTOM_MODULE_MAP.clear()
```

> 说明：第 3 步把 `LlamaForCausalLM` 指向 Qwen3 实现只是为了直观展示「自定义表覆盖官方表」的优先级效果，**并非真实可用配置**。真实自定义注入应通过 `PytorchEngineConfig(custom_module_map=...)` 走 `update_custom_module_map`。`_get_module_map` 内部调用 `get_device_manager().current_context()`，需在引擎设备上下文中运行；脱离上下文时可能报错，此时可改为直接观察 `_get_module_map` 源码的合并顺序。

## 6. 本讲小结

- LMDeploy 的 PyTorch 后端走「**先换类、后灌权重**」：`build_patched_model` 直接实例化 LMDeploy 的优化实现类，而非加载 HF 原模型再改造。
- `MODULE_MAP` 是纯数据注册表：arch 名 → qualname 字符串；用字符串而非类是为了**延迟导入**。
- 整模型构建主链路：`build_patched_model → build_model_from_hf_config → _get_model_class`，查表优先级为 `auto_map` → `architectures`，每个 arch 先过 `_raise_if_removed_model`。
- `get_rewrite_cls` 提供子模块级替换：**full name → class name → submodname** 三级降级 + 正则兜底，与整模型链路共享同一张表。
- 三张辅助表分工明确：`DEVICE_SPECIAL_MODULE_MAP`（非 cuda 覆盖）、`CUSTOM_MODULE_MAP`（用户扩展，优先级最高）、`REMOVED_MODEL_MAP`（清晰报错）。
- 合并优先级为 `MODULE_MAP < 设备表 < CUSTOM_MODULE_MAP`，决定了「谁能覆盖谁」。

## 7. 下一步学习建议

- 下一讲 **u3-l4 以 Llama 为例的模型重写实现** 会钻进 `models/llama.py`，看一个被 `MODULE_MAP` 指向的实现类如何用 `nn.attention` / `nn.linear` 等积木搭起来——建议对照本讲 4.2.4 的查表结果一起读。
- 想立刻理解「灌权重」那一步，可跳读 **u3-l5 权重加载 weight_loader**（`model_weight_loader.py`），看 `load_model_weights` 如何与 patched model 对接。
- 对「不加源码支持新模型」感兴趣，可直接看 **u10-l1 添加新 PyTorch 模型完整流程**，它把本讲的 `update_custom_module_map` 扩展点串成完整接入清单。
