# u6-l1 模型注册机制

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚「HuggingFace 的架构名字（如 `LlamaForCausalLM`）」是如何变成「vLLM 内部一个具体的 PyTorch 模型类」的。
- 理解 `_ModelRegistry` 这个数据结构，以及它内部两种「已注册模型」表示：`_RegisteredModel`（已导入）与 `_LazyRegisteredModel`（懒导入）。
- 解释为什么 vLLM 要用懒注册，以及为什么检查模型能力时要把导入放到**子进程**里跑。
- 掌握对外暴露的单例 `ModelRegistry`、公共的 `register_model` 接口，以及 `model_class_overrides` 这种运行时替换机制。
- 能够动手追踪一条「从 `config.json` 的 architectures 字段 → 解析出实现类」的完整链路。

## 2. 前置知识

在进入源码前，先建立三个直觉。

**(1) 架构名（architecture）是什么？** HuggingFace 的每个模型仓库根目录都有一个 `config.json`，里面有一个 `architectures` 字段，例如：

```json
{ "architectures": ["LlamaForCausalLM"], "hidden_size": 4096, ... }
```

这个 `LlamaForCausalLM` 就是「架构名」。它既不是模型权重，也不是 vLLM 的代码，而只是一个**字符串标签**，告诉加载器「这个权重应该用哪段 Python 类来跑」。vLLM 做推理服务，首先要解决的就是「拿到这个字符串，去哪找对应的实现类」。

**(2) 为什么不能简单地 `import` 所有模型？** vLLM 支持 300+ 种架构，对应的模型实现模块会 `import torch`、`import flash_attn` 等，这些导入会**初始化 CUDA**。而 vLLM 是多进程架构（见 u3-l1）：API Server 进程会 fork 出 EngineCore、再 fork 出 GPU Worker。一旦主进程提前初始化了 CUDA，fork 子进程就会报经典的 `RuntimeError: Cannot re-initialize CUDA in forked subprocess`。所以「用到哪个模型才导入哪个类」是硬需求，这就是懒注册（lazy registration）的根本动机。

**(3) 检查能力 ≠ 加载类。** 调度器、配置校验阶段常常只需要知道「这个模型支不支持多模态、是不是 MoE、能不能流水并行」这些**元信息**，并不需要真的把类导入内存。vLLM 把「查元信息」和「真正加载类」拆成两条路径，前者尽量轻、后者才重。

带着这三点，我们进入源码。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `vllm/model_executor/models/registry.py` | 注册表核心：定义架构字典、`_ModelInfo`、`_RegisteredModel` / `_LazyRegisteredModel`、`_ModelRegistry` 类，并在模块末尾实例化全局单例 `ModelRegistry`。 |
| `vllm/model_executor/models/__init__.py` | 包入口：对外导出 `ModelRegistry` 单例以及一批模型能力接口（`supports_multimodal` 等）。 |
| `vllm/config/model.py` | `ModelConfig` 持有架构名，并在初始化时调用 `registry.inspect_model_cls(...)` 查元信息；`registry` 属性会按需应用 `model_class_overrides`。 |
| `vllm/model_executor/model_loader/utils.py` | 真正加载模型类的地方：调用 `registry.resolve_model_cls(...)` 得到实现类，供权重加载器使用。 |

本讲聚焦前两个文件，后两个用于说明「谁在调用注册表」，帮助你建立方位感。

## 4. 核心概念与源码讲解

本讲拆为四个最小模块：`ModelRegistry`（单例与架构字典）、`_RegisteredModel`、`_LazyRegisteredModel`、`_ModelRegistry`（类与解析方法）。

### 4.1 从架构字典到全局单例 ModelRegistry

#### 4.1.1 概念说明

注册表的本质是一张「架构名 → (模块名, 类名)」的大字典。vLLM 按模型用途把这张大字典拆成若干小字典（文本生成、嵌入、多模态、奖励模型、推测解码等），最后合并成一张总表 `_VLLM_MODELS`，再在模块加载时一次性转成 `_ModelRegistry` 实例 `ModelRegistry`。

#### 4.1.2 核心流程

1. 每个架构登记为一行 `"架构名": ("模块相对名", "类名")`，例如 `"LlamaForCausalLM": ("llama", "LlamaForCausalLM")`。
2. 多个小字典（`_TEXT_GENERATION_MODELS`、`_MULTIMODAL_MODELS` …）合并成 `_VLLM_MODELS`。
3. 模块末尾用一个字典推导，把每一行的 `(模块相对名, 类名)` 包成 `_LazyRegisteredModel`，构造出全局单例 `ModelRegistry`。
4. `__init__.py` 把这个单例 re-export 给外部。

注意第 3 步包的是 `_LazyRegisteredModel`（不是已导入的 `_RegisteredModel`）——也就是说，**建表本身不导入任何模型模块**，只记录「去哪儿找」。这正是懒注册的关键。

#### 4.1.3 源码精读

文本生成模型字典的一个片段，`LlamaForCausalLM` 的登记项就在其中：

[registry.py:72-217](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/model_executor/models/registry.py#L72-L217) 定义 `_TEXT_GENERATION_MODELS`，其中 [registry.py:147](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/model_executor/models/registry.py#L147) 这一行：

```python
"LlamaForCausalLM": ("llama", "LlamaForCausalLM"),
```

含义是：架构名 `LlamaForCausalLM` 对应的实现类是 `llama` 模块里的 `LlamaForCausalLM`。注意模块名是**相对名**，真正路径要补前缀（见下文 `_resolve_module_name`）。这种「一对字符串」的登记形式就是懒注册的最小单元——既不 import、也不执行任何模型代码。

各小字典合并为总表：

[registry.py:723-734](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/model_executor/models/registry.py#L723-L734) 把九个小字典 `**` 展开合并成 `_VLLM_MODELS`：

```python
_VLLM_MODELS = {
    **_TEXT_GENERATION_MODELS,
    **_EMBEDDING_MODELS,
    ...
    **_TRANSFORMERS_BACKEND_MODELS,
}
```

模块相对名 → 全限定模块名的转换：

[registry.py:1439-1445](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/model_executor/models/registry.py#L1439-L1445) `_resolve_module_name` 规定：以 `vllm.` 开头的视为全限定路径直接用（用于硬件隔离、放在 `vllm/models/<name>` 下的实现），否则补上默认前缀 `vllm.model_executor.models.`：

```python
def _resolve_module_name(mod_relname: str) -> str:
    if mod_relname.startswith("vllm."):
        return mod_relname
    return f"vllm.model_executor.models.{mod_relname}"
```

所以 `"llama"` → `vllm.model_executor.models.llama`；而 `"vllm.models.deepseek_v4"` 保持不变。这一层间接让 vLLM 既支持传统的扁平目录布局，也支持新式的隔离布局。

最后，构造全局单例：

[registry.py:1448-1456](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/model_executor/models/registry.py#L1448-L1456) 把总表的每一项转成 `_LazyRegisteredModel`，实例化 `_ModelRegistry`：

```python
ModelRegistry = _ModelRegistry(
    {
        model_arch: _LazyRegisteredModel(
            module_name=_resolve_module_name(mod_relname),
            class_name=cls_name,
        )
        for model_arch, (mod_relname, cls_name) in _VLLM_MODELS.items()
    }
)
```

这一句执行后，`ModelRegistry.models` 就是「架构名 → `_LazyRegisteredModel` 实例」的字典，但**没有一个模型模块被 import**。

包入口对外导出这个单例：

[__init__.py:26](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/model_executor/models/__init__.py#L26) `from .registry import ModelRegistry`，并写入 `__all__`。这样上层代码 `from vllm.model_executor.models import ModelRegistry` 拿到的就是同一个全局单例。本版本（c2881ce）在该文件新增导出了 `SupportsMultiModalEmbeddings` / `supports_multimodal_embeddings` 两个多模态 embedding 能力接口（[__init__.py:9](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/model_executor/models/__init__.py#L9)、[__init__.py:14](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/model_executor/models/__init__.py#L14)），注册表本身在本区间无改动，架构表随每个版本持续新增条目。

#### 4.1.4 代码实践

**实践目标**：亲手从架构字符串读到「目标全限定模块名」，验证建表过程不触发任何模型导入。

**操作步骤**（在装好 vLLM 的 `.venv` 中；若无环境则改为「源码阅读型实践」，见下）：

1. 进入 Python 解释器，导入单例并查表：
   ```python
   from vllm.model_executor.models.registry import ModelRegistry, _VLLM_MODELS
   print(_VLLM_MODELS["LlamaForCausalLM"])          # ('llama', 'LlamaForCausalLM')
   reg = ModelRegistry.models["LlamaForCausalLM"]
   print(type(reg).__name__, reg.module_name, reg.class_name)
   ```
2. 检查此刻 `llama` 模块**尚未**被导入：
   ```python
   import sys
   print("vllm.model_executor.models.llama" in sys.modules)  # 期望 False
   ```

**需要观察的现象**：第 1 步能拿到 `_LazyRegisteredModel(module_name='vllm.model_executor.models.llama', class_name='LlamaForCausalLM')`；第 2 步应为 `False`，证明建表阶段确实没有导入实现模块。

**预期结果**：架构名→模块名映射可读，且 `sys.modules` 里看不到对应模型模块。若在无 GPU 环境无法运行，则改为阅读 [registry.py:1448-1456](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/model_executor/models/registry.py#L1448-L1456) 复述「字典推导如何把字符串登记项转成 `_LazyRegisteredModel`」，并解释为何这一步不会触发 CUDA 初始化（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：`_VLLM_MODELS` 里有这样一行 `"DeepseekV4ForCausalLM": ("vllm.models.deepseek_v4", "DeepseekV4ForCausalLM")`。它的模块名为什么以 `vllm.` 开头而不是只写 `deepseek_v4`？

**参考答案**：因为该实现放在硬件隔离的 `vllm/models/deepseek_v4` 目录下，而非传统扁平目录 `vllm/model_executor/models/`。`_resolve_module_name` 对以 `vllm.` 开头的字符串原样保留为全限定路径，从而能正确定位。

**练习 2**：为什么 vLLM 选择用「模块相对名 + 类名」两个字符串来登记，而不是直接在字典里写 `LlamaForCausalLM` 这个类对象？

**参考答案**：直接写类对象会触发 `import`，进而初始化 CUDA，破坏多进程 fork。用字符串登记实现「建表零导入」，真正用到时才按需导入。

---

### 4.2 两种已注册模型：_RegisteredModel 与 _LazyRegisteredModel

#### 4.2.1 概念说明

注册表里每个架构对应一个「已注册模型」对象，它必须回答两个问题：

- `inspect_model_cls()`：给出这个模型的元信息 `_ModelInfo`（支持多模态吗、是 MoE 吗……）。
- `load_model_cls()`：给出真正的 PyTorch 类 `type[nn.Module]`。

抽象基类 `_BaseRegisteredModel` 定义了这两个抽象方法，有两个具体实现：

- `_RegisteredModel`：类**已经导入**，直接持有 `model_cls`。轻量、直接返回。
- `_LazyRegisteredModel`：类**尚未导入**，只持有 `(module_name, class_name)` 字符串。需要时才 `importlib.import_module` + `getattr` 取出类。

上一节构造的 `ModelRegistry` 里全是 `_LazyRegisteredModel`；而 `register_model` 在传入一个**真实类对象**时会构造 `_RegisteredModel`。

#### 4.2.2 核心流程

`_LazyRegisteredModel` 的难点在 `inspect_model_cls`：要算 `_ModelInfo` 就得导入类、运行一堆接口检查器（`supports_multimodal` 等），这会触发 CUDA。vLLM 的解法是：

1. 计算模型实现源文件的哈希，先查磁盘缓存 `$VLLM_CACHE_ROOT/modelinfos/*.json`；命中且哈希一致就直接反序列化 `_ModelInfo`，**完全不导入类**。
2. 未命中则在**子进程**里导入类、计算 `_ModelInfo`，再 pickle 回主进程，并写回磁盘缓存。
3. 子进程导入即使初始化了 CUDA，也不会污染主进程（fork 安全）。

`load_model_cls` 则简单得多：`importlib.import_module(module_name)` 再 `getattr(class_name)`，只在「真正要实例化模型」时才调用。

#### 4.2.3 源码精读

抽象基类与「已导入」实现：

[registry.py:851-881](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/model_executor/models/registry.py#L851-L881) 定义 `_BaseRegisteredModel`（两个抽象方法）和 `_RegisteredModel`。后者 [registry.py:877-881](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/model_executor/models/registry.py#L877-L881) 直接返回缓存的 `interfaces` 与 `model_cls`：

```python
def inspect_model_cls(self) -> _ModelInfo:
    return self.interfaces

def load_model_cls(self) -> type[nn.Module]:
    return self.model_cls
```

懒注册实现的 `load_model_cls`：

[registry.py:1017-1019](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/model_executor/models/registry.py#L1017-L1019) 这才是「字符串 → 类」的真正落点：

```python
def load_model_cls(self) -> type[nn.Module]:
    mod = importlib.import_module(self.module_name)
    return getattr(mod, self.class_name)
```

`_ModelInfo` 是什么——一张模型能力快照：

[registry.py:792-848](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/model_executor/models/registry.py#L792-L848) `_ModelInfo` 是个 `frozen` dataclass，字段如 `is_text_generation_model`、`supports_multimodal`、`supports_pp`、`is_hybrid`、`has_inner_state` 等。它由 `from_model_cls` 静态方法 [registry.py:816-848](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/model_executor/models/registry.py#L816-L848) 用一组接口检查器（如 `supports_multimodal(model)`、`is_hybrid(model)`）填出来。这一步必须导入类才能跑，所以被设计成在子进程执行。

懒注册的 `inspect_model_cls`——查缓存否则子进程计算：

[registry.py:969-1015](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/model_executor/models/registry.py#L969-L1015) 先定位源文件路径并算哈希 [registry.py:975-986](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/model_executor/models/registry.py#L975-L986)，命中缓存则直接返回 [registry.py:988-995](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/model_executor/models/registry.py#L988-L995)；未命中则在子进程计算 [registry.py:1004-1006](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/model_executor/models/registry.py#L1004-L1006)：

```python
mi = _run_in_subprocess(
    lambda: _ModelInfo.from_model_cls(self.load_model_cls())
)
```

算完后写回磁盘缓存 [registry.py:1012-1013](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/model_executor/models/registry.py#L1012-L1013)。

子进程执行器：

[registry.py:1461-1488](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/model_executor/models/registry.py#L1461-L1488) `_run_in_subprocess` 用 `cloudpickle` 把 lambda 序列化，启动一个独立 Python 进程（命令为 `[sys.executable, "-m", "vllm.model_executor.models.registry"]`，见 [registry.py:740](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/model_executor/models/registry.py#L740)），把结果 pickle 写到临时文件再读回。入口 `_run` 见 [registry.py:1491-1502](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/model_executor/models/registry.py#L1491-L1502)。这样即便导入触发了 CUDA，也只发生在那个一次性子进程里。

#### 4.2.4 代码实践

**实践目标**：观察 `load_model_cls` 触发导入、而 `inspect_model_cls` 走缓存/子进程两条不同路径。

**操作步骤**（源码阅读型 + 可选运行）：

1. 阅读 [registry.py:1017-1019](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/model_executor/models/registry.py#L1017-L1019)，确认 `load_model_cls` 用 `importlib.import_module` + `getattr` 取类。
2. 在解释器中（若有环境）：
   ```python
   import sys
   from vllm.model_executor.models.registry import ModelRegistry
   reg = ModelRegistry.models["LlamaForCausalLM"]
   cls = reg.load_model_cls()                     # 触发 import
   print("vllm.model_executor.models.llama" in sys.modules)  # 期望 True
   print(cls.__name__)                            # LlamaForCausalLM
   info = reg.inspect_model_cls()                 # 第二次走子进程/缓存
   print(info.is_text_generation_model, info.is_hybrid)
   ```
3. 关注 `$VLLM_CACHE_ROOT/modelinfos/` 目录：第一次 `inspect_model_cls` 后会生成 `vllm-model_executor-models-llama-LlamaForCausalLM.json` 缓存文件（见 [registry.py:897-899](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/model_executor/models/registry.py#L897-L899) 的文件名规则）。

**需要观察的现象**：调用 `load_model_cls` 后 `sys.modules` 出现对应模型模块；`inspect_model_cls` 返回的 `_ModelInfo` 各布尔字段能反映 Llama 的能力（文本生成、非 MoE、非 hybrid）。

**预期结果**：导入只在显式 `load_model_cls` 时发生；`inspect` 不污染主进程。若无法运行，标注「待本地验证」，并据源码说明 `_run_in_subprocess` 如何隔离 CUDA 初始化。

#### 4.2.5 小练习与答案

**练习 1**：`_LazyRegisteredModel.inspect_model_cls` 为什么要先算源文件哈希再查缓存？

**参考答案**：模型实现代码会变（升级版本、改接口）。哈希作为缓存键的一部分，源文件一改哈希就变，旧缓存被判为 stale 而失效（见 [registry.py:934-940](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/model_executor/models/registry.py#L934-L940)），保证元信息与当前代码一致，避免用过期的能力快照做调度决策。

**练习 2**：既然 `inspect_model_cls` 也要导入类，为什么不直接在主进程导入，而非要起子进程？

**参考答案**：导入模型类会初始化 CUDA；主进程随后要 fork worker，CUDA 已初始化的进程 fork 会报 `Cannot re-initialize CUDA in forked subprocess`。子进程是一次性的、不会再去 fork，所以它的 CUDA 初始化不会扩散到主进程，既拿到了 `_ModelInfo` 又保全了 fork 安全。

---

### 4.3 解析链路：inspect_model_cls 与 resolve_model_cls

#### 4.3.1 概念说明

`_ModelRegistry` 提供两个核心入口，名字相近但用途不同：

- `inspect_model_cls(architectures, model_config)` → 返回 `(_ModelInfo, arch)`：只查元信息，**不真正加载类**（用于配置校验、调度决策）。
- `resolve_model_cls(architectures, model_config)` → 返回 `(type[nn.Module], arch)`：真正加载实现类（权重加载器用它来 new 出模型）。

两者共享同一套「解析顺序」：先看 `model_impl` 是否强制走 Transformers 后端；再尝试架构名归一化（`_normalize_arch`）后查表；都不行才报「不支持」。

#### 4.3.2 核心流程

`resolve_model_cls` 的大致解析顺序（`inspect_model_cls` 同构）：

```
输入 architectures（来自 config.json）+ model_config
 │
 ├─ model_impl == "transformers"？ → _try_resolve_transformers，命中即返回
 ├─ 都不在表里 且 model_impl=="auto" 且 convert_type=="none"？
 │      → 先试 Transformers 回退（resolve convert_type 之后）
 ├─ 逐个 architecture：
 │      normalized = _normalize_arch(arch)   # 后缀归一化
 │      命中 _try_load_model_cls → 返回 (cls, arch)
 ├─ 仍没命中 且 model_impl=="auto"？ → 再试 Transformers 回退（resolve runner_type 之前）
 └─ 全失败 → _raise_for_unsupported
```

其中 `_normalize_arch` 处理「变体」：像 `LlamaForTokenClassification` 这种没直接登记的名字，会按后缀（`ForTokenClassification` → 默认 `pooling/classify`）剥成基座 `LlamaForCausalLM`，从而复用已登记的实现。

#### 4.3.3 源码精读

`inspect_model_cls`（查元信息）：

[registry.py:1244-1294](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/model_executor/models/registry.py#L1244-L1294) 是上面的流程实现。它的逐架构查表在 [registry.py:1277-1281](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/model_executor/models/registry.py#L1277-L1281)：

```python
for arch in architectures:
    normalized_arch = self._normalize_arch(arch, model_config)
    model_info = self._try_inspect_model_cls(normalized_arch)
    if model_info is not None:
        return (model_info, arch)
```

`resolve_model_cls`（真正加载类）：

[registry.py:1296-1348](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/model_executor/models/registry.py#L1296-L1348) 与 `inspect_model_cls` 结构一致，只是把 `_try_inspect_model_cls` 换成 `_try_load_model_cls`。逐架构查表在 [registry.py:1331-1335](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/model_executor/models/registry.py#L1331-L1335)。

底层带缓存的解析（`@lru_cache`）：

[registry.py:1022-1046](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/model_executor/models/registry.py#L1022-L1046) `_try_load_model_cls` / `_try_inspect_model_cls` 用 `@lru_cache(maxsize=128)` 保证「同一架构在同一进程只解析一次」。注意 `_try_load_model_cls` 还会调用 [registry.py:1029](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/model_executor/models/registry.py#L1029) `current_platform.verify_model_arch(model_arch)`，让平台层（CUDA/ROCm/CPU…）有机会否决某架构。

变体归一化 `_normalize_arch`：

[registry.py:1218-1242](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/model_executor/models/registry.py#L1218-L1242) 当架构名不在表里时，借助 [config/model.py:2071-2088](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/config/model.py#L2071-L2088) 的 `try_match_architecture_defaults` 按后缀匹配默认 (runner, convert)，再把后缀剥掉回查基座：

```python
for repl_suffix, _ in iter_architecture_defaults():
    base_arch = architecture.replace(suffix, repl_suffix)
    if base_arch in self.models:
        return base_arch
```

Transformers 回退 `_try_resolve_transformers`：

[registry.py:1148-1216](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/model_executor/models/registry.py#L1148-L1216) 对没有原生实现的架构，回退到直接跑 HuggingFace `transformers` 库的实现（登记在 `_TRANSFORMERS_BACKEND_MODELS`，见 [registry.py:688-721](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/model_executor/models/registry.py#L688-L721)）。这给了「vLLM 没专门写、但 transformers 有」的模型一条退路。

报错与历史归档 `_raise_for_unsupported`：

[registry.py:1103-1134](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/model_executor/models/registry.py#L1103-L1134) 在确实不支持时，会区分三种情况给更友好的报错：曾在旧版本支持过（`_PREVIOUSLY_SUPPORTED_MODELS`，见 [registry.py:742-782](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/model_executor/models/registry.py#L742-L782)）、已移到外部插件（`_OOT_SUPPORTED_MODELS`，见 [registry.py:784-789](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/model_executor/models/registry.py#L784-L789)）、或完全未知。

谁在调用这两个入口：

- 配置阶段查元信息——[config/model.py:642](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/config/model.py#L642) `ModelConfig` 初始化时 `registry.inspect_model_cls(architectures, self)`，把结果存进 `self._model_info` 与 `self._architecture`。
- 加载阶段取类——[model_loader/utils.py:209](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/model_executor/model_loader/utils.py#L209) `_get_model_architecture` 调 `model_config.registry.resolve_model_cls(architectures, model_config=model_config)`，拿到真正的类交给权重加载器实例化。

#### 4.3.4 代码实践

**实践目标**：手动驱动一次 `resolve_model_cls`，复现「架构字符串 → 实现类」的解析，并观察 lru_cache 命中。

**操作步骤**（若有环境）：

1. 构造一个最小 `ModelConfig`（或直接复用现有测试 fixture），取 `architectures=["LlamaForCausalLM"]`。
2. 调用：
   ```python
   cls, arch = ModelRegistry.resolve_model_cls(
       ["LlamaForCausalLM"], model_config=cfg)
   print(arch, cls.__module__, cls.__name__)
   ```
3. 再调一次相同架构，对比耗时（第二次应被 `@lru_cache` 命中，几乎为零）。
4. 试着传一个未登记的名字，例如 `["TotallyFakeForCausalLM"]`，观察 `_raise_for_unsupported` 抛出的错误信息。

**需要观察的现象**：第 2 步得到 `(LlamaForCausalLM, 'vllm.model_executor.models.llama', 'LlamaForCausalLM')`；第 4 步报错信息会列出全部已支持架构。

**预期结果**：解析链路按「逐架构查表 → 命中 lazy load」走通。若手头没有可运行的 `ModelConfig`，改为阅读 [registry.py:1296-1348](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/model_executor/models/registry.py#L1296-L1348) 与 [model_loader/utils.py:204-212](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/model_executor/model_loader/utils.py#L204-L212) 复述链路即可（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：`inspect_model_cls` 与 `resolve_model_cls` 返回值类型不同，为什么要在配置阶段用前者、在加载阶段用后者？

**参考答案**：配置阶段（如 `ModelConfig.__post_init__`）只需要能力元信息来决定 runner/调度策略，用 `inspect_model_cls` 拿到 `_ModelInfo` 即可，避免提前导入类、触发 CUDA；真正要 new 模型实例时（权重加载器）才用 `resolve_model_cls` 拿类。这种「先轻后重」的分层降低了无关路径的导入成本。

**练习 2**：`_normalize_arch` 解决了什么问题？举一个例子。

**参考答案**：解决「变体架构名未直接登记」的问题。例如 `LlamaForTokenClassification` 没有独立登记项，`_normalize_arch` 识别后缀 `ForTokenClassification`（默认 pooling/classify），把名字剥成基座 `LlamaForCausalLM` 回查命中，于是能复用 Llama 的实现做序列分类。

---

### 4.4 注册自定义模型：register_model 与运行时覆盖

#### 4.4.1 概念说明

除了建表时的静态登记，`_ModelRegistry` 还提供公共方法 `register_model(model_arch, model_cls)`，让用户在运行时把自己的模型接进来。`model_cls` 可以是：

- 一个真实的 `torch.nn.Module` 子类 → 包装成 `_RegisteredModel`；
- 一个 `"module:class"` 字符串 → 包装成 `_LazyRegisteredModel`（同样不立即导入，避免 CUDA 初始化）。

字符串形式正是 vLLM 自己登记内部模型的方式，也是给「自定义/第三方模型」的推荐用法。此外，`ModelConfig` 暴露了 `model_class_overrides` 字段，允许在配置层面把某个架构替换成另一个实现类（主要用于开发调试）。

#### 4.4.2 核心流程

```
register_model(arch, model_cls)
 ├─ model_cls 是 str 且形如 "module:class"？
 │      → _LazyRegisteredModel(module, class)   # 不导入
 ├─ model_cls 是 nn.Module 子类？
 │      → _RegisteredModel.from_model_cls(...)  # 已导入
 └─ 否则 TypeError

# 运行时覆盖（model_class_overrides）：
ModelConfig.registry（property）
 └─ _maybe_register_model_class_overrides()
      → 对每个 (arch, target) 调 ModelRegistry.register_model(arch, target)
      → 用进程内集合 _REGISTERED_MODEL_CLASS_OVERRIDES 去重，保证每个进程只注册一次
```

因为每个 worker 都是独立进程，`_maybe_register_model_class_overrides` 用一个模块级集合做**进程内去重**，确保覆盖在每个进程里只生效一次，而不是信任从主进程 pickle 过来的标志。

#### 4.4.3 源码精读

公共注册方法：

[registry.py:1057-1101](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/model_executor/models/registry.py#L1057-L1101) `register_model` 的核心分支在 [registry.py:1085-1099](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/model_executor/models/registry.py#L1085-L1099)：

```python
if isinstance(model_cls, str):
    split_str = model_cls.split(":")
    ...
    model = _LazyRegisteredModel(*split_str)
elif isinstance(model_cls, type) and issubclass(model_cls, nn.Module):
    model = _RegisteredModel.from_model_cls(model_cls)
else:
    ... raise TypeError(...)
self.models[model_arch] = model
```

注意 [registry.py:1077-1083](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/model_executor/models/registry.py#L1077-L1083)：同名架构重复注册时只打 debug 日志并覆盖，不报错——这正是 `model_class_overrides` 能「顶掉」默认实现的机制。

`model_class_overrides` 的声明：

[config/model.py:305-311](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/config/model.py#L305-L311) `ModelConfig.model_class_overrides` 是个 `dict[str, str]`，把架构名映射到 `"module:class"` 目标，文档明示这与 `ModelRegistry.register_model` 同格式：

```python
model_class_overrides: dict[str, str] = field(default_factory=dict)
# e.g. {"GlmMoeDsaForCausalLM":
#   "vllm.models.deepseek_v32.nvidia.model:DeepseekV32ForCausalLM"}
```

`registry` 属性作为唯一「咽喉」：

[config/model.py:929-957](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/config/model.py#L929-L957) `ModelConfig.registry` 是个 property，每次访问都先调 `_maybe_register_model_class_overrides()`，再返回全局 `ModelRegistry`。注释强调这是「所有 inspect/resolve 都必经的咽喉」，所以把覆盖应用在这里能保证前端与每个 worker 进程都生效：

```python
@property
def registry(self):
    self._maybe_register_model_class_overrides()
    return me_models.ModelRegistry
```

`_maybe_register_model_class_overrides` 见 [config/model.py:934-957](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/config/model.py#L934-L957)，它用 `_REGISTERED_MODEL_CLASS_OVERRIDES` 集合做进程内去重，并 `logger.warning_once` 提示这是开发/调试用途。

#### 4.4.4 代码实践

**实践目标**：用 `register_model` 的字符串形式临时注册一个自定义架构，并验证它随后能被 `resolve_model_cls` 解析到。

**操作步骤**（示例代码，非项目原有代码）：

1. 写一个最小模型类文件 `my_arch.py`（放在能被 import 的路径，例如当前目录）：
   ```python
   # 示例代码：my_arch.py
   import torch.nn as nn
   class MyToyForCausalLM(nn.Module):
       pass
   ```
2. 在解释器中用字符串形式注册（避免主进程导入）：
   ```python
   from vllm.model_executor.models.registry import ModelRegistry
   ModelRegistry.register_model("MyToyForCausalLM", "my_arch:MyToyForCausalLM")
   # 注意：这里 model_config 需要一个最小 ModelConfig，可复用现有测试 fixture
   cls, arch = ModelRegistry.resolve_model_cls(["MyToyForCausalLM"], model_config=cfg)
   print(arch, cls)   # MyToyForCausalLM <class 'my_arch.MyToyForCausalLM'>
   ```
3. 想体验运行时覆盖，可在构造 `LLM` / `EngineArgs` 时传 `model_config={"model_class_overrides": {"LlamaForCausalLM": "my_arch:MyToyForCausalLM"}}`（具体传参方式以当前 API 为准），观察日志中出现 `Applying model_class_overrides ...` 的告警（见 [config/model.py:950-954](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/config/model.py#L950-L954)）。

**需要观察的现象**：第 2 步解析成功；注册前调用 `resolve_model_cls(["MyToyForCausalLM"], ...)` 会报「不支持」，注册后才成功——说明 `register_model` 实时改了 `self.models` 字典。

**预期结果**：自定义架构能在运行时被接进来并解析。第 3 步若 API 细节与本版本不符，以阅读 [config/model.py:934-957](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/config/model.py#L934-L957) 复述覆盖机制为准（待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：用 `register_model("X", MyClass)`（直接传类）和 `register_model("X", "mod:MyClass")`（传字符串）有什么实际区别？

**参考答案**：传类意味着调用前已经 `import` 了该类（可能已初始化 CUDA），注册后包装成 `_RegisteredModel`，`load_model_cls` 直接返回类；传字符串则包装成 `_LazyRegisteredModel`，注册时不导入，直到 `load_model_cls` 才 `importlib.import_module`。对多进程安全的自定义模型，推荐字符串形式。

**练习 2**：为什么 `_maybe_register_model_class_overrides` 要用模块级集合 `_REGISTERED_MODEL_CLASS_OVERRIDES` 去重，而不是在 `ModelConfig` 上存一个布尔标志？

**参考答案**：`ModelConfig` 会被 pickle 传到每个 worker 进程；若标志跟着 pickle 进来，每个进程都会「以为已经注册过」而跳过，但 `ModelRegistry` 是进程内单例、worker 进程里其实是全新的，于是覆盖不会在 worker 生效。用进程内集合做去重，能让每个进程各自真正地往自己的 `ModelRegistry` 注册一次。

## 5. 综合实践

把四个模块串起来：追踪一次完整的「`config.json` → 模型类」解析。

任务：假设有一个 HuggingFace 仓库，其 `config.json` 里 `"architectures": ["Qwen2ForCausalLM"]`，引擎以默认 `model_impl="auto"` 启动。请完成：

1. **定位登记项**：在 [registry.py:72-217](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/model_executor/models/registry.py#L72-L217) 找到 `Qwen2ForCausalLM` 的登记行，写出它的 `(模块相对名, 类名)` 与经 `_resolve_module_name` 后的全限定模块名。
2. **构造单例**：说明这一行是如何经 `_VLLM_MODELS`（[registry.py:723-734](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/model_executor/models/registry.py#L723-L734)）→ 推导（[registry.py:1448-1456](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/model_executor/models/registry.py#L1448-L1456)）变成 `ModelRegistry.models["Qwen2ForCausalLM"]` 里的一个 `_LazyRegisteredModel`，并指出此刻实现模块是否已被导入。
3. **配置阶段**：在 [config/model.py:642](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/config/model.py#L642) 处，`ModelConfig` 调 `inspect_model_cls` 得到 `_ModelInfo`。说明这一次 inspect 走的是「磁盘缓存命中」还是「子进程计算」，并解释为什么不会污染主进程。
4. **加载阶段**：在 [model_loader/utils.py:209](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/model_executor/model_loader/utils.py#L209) 处调 `resolve_model_cls`，经 [registry.py:1017-1019](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/model_executor/models/registry.py#L1017-L1019) 的 `load_model_cls` 真正 `import` 出 `Qwen2ForCausalLM` 类。
5. **变体扩展**：若 `config.json` 里是 `"Qwen2ForSequenceClassification"`（未直接登记），说明 [registry.py:1218-1242](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/model_executor/models/registry.py#L1218-L1242) 的 `_normalize_arch` 如何借 [config/model.py:2071-2088](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/config/model.py#L2071-L2088) 的后缀默认把它剥成基座 `Qwen2ForCausalLM` 命中。

完成第 1～4 步即覆盖了「架构字符串 → 实现类」的主链路；第 5 步是进阶。

## 6. 本讲小结

- 注册表的本质是「架构名 → (模块名, 类名)」的大字典，按用途分九个小表合并为 `_VLLM_MODELS`，再在模块加载时转成全局单例 `ModelRegistry`（一个 `_ModelRegistry` 实例）。
- 建表过程**零导入**：每一项都包成 `_LazyRegisteredModel(module_name, class_name)` 字符串对，避免初始化 CUDA、保住多进程 fork 安全。
- 已注册模型有抽象基类 `_BaseRegisteredModel` 与两个实现：`_RegisteredModel`（已导入，直接返回类）和 `_LazyRegisteredModel`（按需 `importlib` 导入）。
- 两个核心入口职责不同：`inspect_model_cls` 只查 `_ModelInfo` 元信息（必要时在子进程计算并磁盘缓存），`resolve_model_cls` 才真正加载类。两者共享「Transformers 回退 → 逐架构查表（含 `_normalize_arch` 变体归一化）→ 报错」的解析顺序，并用 `@lru_cache` 去重。
- 公共 `register_model` 接受类对象或 `"module:class"` 字符串；`ModelConfig.model_class_overrides` 经 `registry` 这个「咽喉」property 在每个进程内应用覆盖，用于开发调试。

## 7. 下一步学习建议

- 读完解析链路后，自然的下一步是看「拿到类之后，权重怎么按层灌进去」——即 u6-l2（HuggingFace 模型适配：权重名映射、模型组合、`adapters.py`）与 u5-l4（模型加载与权重加载器）。
- 若想理解 `_ModelInfo` 里那些能力标志（`supports_multimodal`、`is_hybrid`…）是如何在模型类上声明并被检查器读出的，可阅读 `vllm/model_executor/models/interfaces.py` 与 `interfaces_base.py`（本讲引用了它们但未展开）。
- 对「同一架构在不同硬件上的不同实现」（如 `vllm.models.deepseek_v4` 这类隔离布局）感兴趣，可对照 u10-l1 平台抽象，理解平台层 `verify_model_arch` 如何与注册表协作。
