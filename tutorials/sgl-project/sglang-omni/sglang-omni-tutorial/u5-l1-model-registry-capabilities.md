# 模型注册表与能力声明

## 1. 本讲目标

学完本讲，你应该能够：

- 说清楚 SGLang-Omni 是如何「零配置」地把一个新模型家族接入运行时的：只要把代码放进 `models/<name>/`、声明 `architecture` 与 `EntryClass`，框架就能自动发现它。
- 解释 `PIPELINE_CONFIG_REGISTRY` 这个全局注册表的数据结构（「架构名 → 配置类」的字典）以及它的自动扫描机制 `import_pipeline_configs`。
- 描述从「磁盘上的模型权重路径」到「对应的 `PipelineConfig` 子类」的完整匹配链路：读 HF `config.json` → 取 `architectures` 字段 → 查注册表。
- 理解 `ModelCapabilities` 这五个布尔能力标志（`reference_audio` / `batch_vocoder` / `streaming_vocoder` / `cuda_graph` / `torch_compile`）的语义，以及它们与「具体 checkpoint 部署策略」的边界。
- 能动手在一个真实模型包里找到 `architecture`、`EntryClass`、`CAPABILITIES` 三处声明，并解释注册表如何据此把它们串起来。

本讲是单元 u5「模型家族集成与预处理」的入口。承接 u2-l5 讲过的「`PipelineConfig` 是模型定义与运行时之间的契约」，本讲回答：**框架怎么知道某个磁盘上的模型该用哪份 `PipelineConfig`？** 答案就是注册表与架构匹配。

## 2. 前置知识

在进入源码前，先建立三个直觉。

### 2.1 为什么要「按架构匹配」而不是「按名字匹配」

SGLang-Omni 要服务一整族结构迥异的模型：Qwen3-Omni（多模态对话）、Qwen3-TTS（文本转语音）、Higgs Audio（语音克隆）、Whisper/Fun-ASR（语音识别）……每个家族的 stage 拓扑（见 u2-l5）完全不同。用户启动服务时只给一个权重路径（如 `Qwen/Qwen3-Omni`），框架必须自己判断「这是哪一类模型、该套哪套拓扑」。

巧妙之处在于：**HuggingFace 的权重目录里天然有一张「身份证」——`config.json` 里的 `architectures` 字段**。比如 Qwen3-Omni 的 `config.json` 里写着 `"architectures": ["Qwen3OmniMoeForConditionalGeneration"]`。SGLang-Omni 只要把每个 `PipelineConfig` 子类挂到一个架构名上，再读出权重的架构名去查表，就完成了匹配。这比让用户手写「我用的是哪个 config 类」要鲁棒得多。

### 2.2 「自动发现」靠的是 Python 包扫描，不是手写清单

很多框架让你在某个 `__init__.py` 里手动 `register("xxx", MyConfig)`。SGLang-Omni 不这样：它用 `pkgutil` 遍历 `models/` 下所有子包，约定每个子包里只要有 `config.py`、且该文件里有一个名为 `EntryClass` 的符号，就被当作一个可服务模型。这种「约定优于配置」（convention over configuration）的好处是——**新增模型家族时无需修改任何框架代码，只要新建目录**。

### 2.3 「能力」是架构级静态事实，不是 checkpoint 级动态事实

一个容易混淆的点：`ModelCapabilities` 描述的是「**这个架构能做什么**」，而不是「**这次部署允不允许做**」。例如 Qwen3-TTS 架构支持参考音频克隆（`supports_reference_audio=True`），但如果你加载的是 `CustomVoice` 这种受版权保护的 checkpoint，部署策略会拒绝用户上传参考音频。所以能力标志是「上限」，具体能不能用还要看 `PipelineConfig` 的方法（如 `supports_uploaded_voice_references()`）和当前 checkpoint。记住这个区分，后面读源码就不会困惑。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
|------|------|
| [sglang_omni/models/registry.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/registry.py) | 全局注册表 `PIPELINE_CONFIG_REGISTRY`，定义自动扫描 `import_pipeline_configs` 与查找方法 |
| [sglang_omni/models/model_capabilities.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/model_capabilities.py) | `ModelCapabilities` 数据类与 `get_model_capabilities()` 查询函数 |
| [sglang_omni/config/manager.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/config/manager.py) | `resolve_config_cls_for_model_path()`：从权重路径解析出配置类的入口 |
| [sglang_omni/utils/hf.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/utils/hf.py) | `architecture_from_hf_config()` 等：从 HF config 提取架构名 |
| [sglang_omni/config/schema.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/config/schema.py) | `PipelineConfig` 基类，声明 `architecture` / `architecture_aliases` / `requires_model_capabilities` 三个 ClassVar 契约 |
| [sglang_omni/models/qwen3_omni/config.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/config.py) | 真实示例：Qwen3-Omni 的 `architecture`、`EntryClass`、`Variants` |
| [sglang_omni/models/qwen3_tts/__init__.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_tts/__init__.py) | 真实示例：`CAPABILITIES` 的导出方式 |
| [sglang_omni/serve/launcher.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/serve/launcher.py) | 启动时把能力摘要写进日志的 `_model_capabilities_log_summary()` |

---

## 4. 核心概念与源码讲解

### 4.1 PIPELINE_CONFIG_REGISTRY：全局注册表与自动发现

#### 4.1.1 概念说明

`PIPELINE_CONFIG_REGISTRY` 是一个进程级的单例对象，本质就是「**架构名 → `PipelineConfig` 子类**」的字典，外加几个查询方法。它是整个模型匹配体系的「单一真相」：`resolve_config_cls_for_model_path`、CLI 的 `config` 子命令、能力查询，全都最终落到这个字典上。

它的注册过程发生在模块导入时——一旦 `import sglang_omni.models.registry`，最后一行就会立刻扫描整个 `models/` 目录并填好字典。也就是说，**这个字典在你写任何业务代码之前就已经建好了**。

#### 4.1.2 核心流程

注册表的建立是一个「扫描 → 抽取契约 → 索引」的流水线：

```
import sglang_omni.models.registry
        │
        ▼
register_config("sglang_omni.models", "config")
        │
        ▼
import_pipeline_configs(...)          ← @lru_cache 包裹，只跑一次
        │
        ├─ pkgutil.iter_modules 遍历 models/ 下每个子包（ispkg=True）
        ├─ 对每个子包 import 它的 config.py
        ├─ 检查 config.py 是否有 EntryClass 符号（没有就 AssertionError）
        ├─ 从 EntryClass 取 architecture + architecture_aliases 作为键
        └─ 写入 dict: {架构名: EntryClass}   （重名则 ValueError）
        │
        ▼
PIPELINE_CONFIG_REGISTRY.configs  ← 最终字典
```

查询时则非常直接：拿到架构名，调 `get_config(arch)` 返回配置类；或者用类名调 `get_config_cls_by_name(name)`（后者会额外翻 `Variants` 字典）。

#### 4.1.3 源码精读

注册表本体是一个 dataclass，核心就是 `configs` 字典加三个方法：

[sglang_omni/models/registry.py:87-119](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/registry.py#L87-L119) — `_PipelineConfigRegistry` 数据类。注意 `get_config` 在查不到时抛 `ValueError`（这是后续「未知模型报错」的源头），而 `register_config` 在非覆盖模式下遇到同名架构也会抛 `ValueError`，保证一个架构只能被一个配置类注册。

自动扫描的核心是 `import_pipeline_configs`。它被 `@lru_cache()` 装饰，意味着同一组参数只会执行一次扫描：

[sglang_omni/models/registry.py:35-44](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/registry.py#L35-L44) — 用 `pkgutil.iter_modules` 枚举 `package.__path__`（即 `sglang_omni/models/` 这个目录）下的所有子包；只处理 `ispkg=True` 的（必须是子目录/子包，单文件不算）。注意这里对 import 失败很宽容：`strict=False` 时只打 warning 并跳过，**一个模型包导入失败不会拖垮整个注册表**。

[sglang_omni/models/registry.py:45-71](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/registry.py#L45-L71) — 这一段定义了「模型包契约」：尝试 import `<子包>.config` 模块，若该模块没有 `EntryClass` 属性就直接 `AssertionError`（说明这是硬约定，不是可选的）；若有，则取 `config_module.EntryClass` 作为配置类。这里特意区分了 `ModuleNotFoundError`（子模块干脆不存在，静默跳过）与 `ImportError`（存在但内部出错，打 warning）。

模块最后两行是「自启动」：

[sglang_omni/models/registry.py:135-136](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/registry.py#L135-L136) — 创建单例并立刻注册 `sglang_omni.models` 包下的所有 `config` 子模块。这就是「import 即注册」魔法的全部。

`Variants` 的查询走 `get_config_cls_by_name`：

[sglang_omni/models/registry.py:121-132](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/registry.py#L121-L132) — 除了按 `config_cls.__name__` 匹配，还会 import 配置类所在模块、读取它的 `Variants` 字典，匹配其中任意一个变体类。这让 YAML 里写 `config_cls: Qwen3OmniPipelineConfig`（text 变体）而不是默认的 `EntryClass` 成为可能。

#### 4.1.4 代码实践

**实践目标**：亲眼看到注册表在 import 后自动填满，并验证「新增目录即被发现」。

**操作步骤**（源码阅读型实践，不需要 GPU）：

1. 在装好包的虚拟环境里启动 Python：
   ```bash
   python -c "from sglang_omni.models.registry import PIPELINE_CONFIG_REGISTRY as R; print(sorted(R.get_supported_archs()))"
   ```
2. 数一下输出的架构数量，再和 `ls sglang_omni/models/` 下的子目录数量对比（应大致一一对应，少数包可能没有 `config.py` 或 import 失败）。
3. 挑一个架构名验证查找：
   ```bash
   python -c "from sglang_omni.models.registry import PIPELINE_CONFIG_REGISTRY as R; print(R.get_config('Qwen3OmniMoeForConditionalGeneration'))"
   ```
   预期输出形如 `<class 'sglang_omni.models.qwen3_omni.config.Qwen3OmniSpeechPipelineConfig'>`。

**需要观察的现象**：第 1 步应列出十几个架构名（如 `Qwen3OmniMoeForConditionalGeneration`、`Qwen3TTSForConditionalGeneration`、`HiggsMultimodalQwen3ForConditionalGeneration` 等），无需手动 `register` 任何东西。

**预期结果**：注册表的 keys 与磁盘上的 `models/<name>/` 子包一一对应，证明扫描是自动的。若某架构缺失，多半是对应包 `config.py` import 报错被跳过——可加 `strict=True` 复现（见 4.1.5 第 2 题）。

> 待本地验证：若当前环境未安装 GPU 相关依赖导致部分包 import 失败，输出架构数会少于目录数；这是 `strict=False` 容错跳过的结果。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `import_pipeline_configs` 要用 `@lru_cache()` 装饰？如果去掉会发生什么？

**参考答案**：注册表内容是确定的（由磁盘上的代码静态决定），扫描有成本（要 import 一堆模块、触发模型类的定义）。`lru_cache` 保证对同一组 `(package_name, config_path, strict)` 只扫描一次，后续调用直接返回缓存字典。去掉后每次调用 `register_config` 都会重新遍历并重新 import，既慢又可能因重复 import 产生副作用。

**练习 2**：一个新模型包因为笔误在 `config.py` 里 import 了不存在的符号，注册表会怎样？如何让它「显式报错」而不是「静默跳过」？

**参考答案**：默认 `strict=False`，扫描时只打一条 warning 日志并跳过该包，注册表里不会有这个架构——结果就是「明明目录在，却查不到模型」，排查困难。若调 `register_config(..., strict=True)`（或直接调 `import_pipeline_configs(..., strict=True)`），同样的错误会直接 `raise`，把笔误暴露在启动阶段。

---

### 4.2 architecture 与 architecture_aliases：HF 架构匹配契约

#### 4.2.1 概念说明

`architecture` 是 `PipelineConfig` 的一个 `ClassVar`（类变量），它声明「**这份配置类对应哪种 HF 架构**」。它的值必须和模型权重 `config.json` 里 `architectures` 字段的某一项**逐字符相等**——这是注册表匹配的唯一依据。

`architecture_aliases`（别名元组）用来兼容「同一个架构在历史上用过多个名字」的情况。比如 MOSS-TTS 的权重在不同版本里可能叫 `MossTTSDelay`、`MossTTSDelayForConditionalGeneration`、`MossTTSDelayWithCodec` 等，它们其实都指向同一份配置类。注册表会把主名和所有别名都登记进去，任一命中即可。

#### 4.2.2 核心流程

从「权重路径」到「配置类」的完整匹配链路如下（这是 u1-l5 提到的 `resolve_config_cls_for_model_path` 的内部）：

```
用户传入 model_path (如 "Qwen/Qwen3-Omni")
        │
        ▼
AutoConfig.from_pretrained(model_path)        ← 读 config.json
        │
        ▼
architecture_from_hf_config(hf_config)         ← 取 architectures[0]
        │  （失败则回退到 model_type 映射、raw json、Mistral params.json）
        ▼
得到 arch = "Qwen3OmniMoeForConditionalGeneration"
        │
        ▼
PIPELINE_CONFIG_REGISTRY.get_config(arch)      ← 查字典
        │
        ▼
返回 Qwen3OmniSpeechPipelineConfig 类
```

注册表建立索引时，主名和别名一视同仁地成为字典键（见 4.1.3 的 `_iter_config_architectures`）。因此匹配阶段根本不需要关心别名——它们在注册时就已经「展开」成多个键指向同一个类了。

#### 4.2.3 源码精读

先看契约在基类上的声明：

[sglang_omni/config/schema.py:220-222](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/config/schema.py#L220-L222) — `PipelineConfig` 基类把 `architecture` 默认设为 `None`、`architecture_aliases` 默认设为空元组。子类必须覆盖 `architecture`（否则注册时 `_iter_config_architectures` 会因为 `not arch` 而跳过它，导致该架构无法被索引）。`requires_model_capabilities` 这一行也在这里，4.4 节会用到。

再看别名如何展开成多个键：

[sglang_omni/models/registry.py:13-24](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/registry.py#L13-L24) — `_iter_config_architectures` 把 `architecture`（主名）和 `architecture_aliases`（别名）拼成一个列表，去重后返回。注意它兼容 `aliases` 是字符串的写法（自动包成元组），也过滤掉空值和重复。这就是「主名 + 别名全部登记」的实现。

一个真实的别名声明（MOSS-TTS 有 4 个别名）：

[sglang_omni/models/moss_tts/config.py:16-23](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/moss_tts/config.py#L16-L23) — 主名是 `MossTTSDelayModel`，另登记了 `MossTTSDelay`、`MossTTSDelayForConditionalGeneration`、`MossTTSDelayWithCodec`、`MossTTSDelayWithCodecModel` 四个别名。无论权重的 `config.json` 写的是哪个，都能命中同一个 `MossTTSPipelineConfig`。

匹配链路的入口：

[sglang_omni/config/manager.py:16-31](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/config/manager.py#L16-L31) — `resolve_config_cls_for_model_path` 是整个匹配的总入口。它依次尝试三条解析路径（`AutoConfig` → 原始 `config.json` → Mistral `params.json`），任一拿到架构名就交给 `PIPELINE_CONFIG_REGISTRY.get_config(arch)`。三条回退路径的存在，是为了应对「权重没有标准 `config.json`」（如 Voxtral 用 Mistral 的 `params.json`）或「`trust_remote_code` 不可用」等极端情况。

从 HF config 取架构名的具体逻辑：

[sglang_omni/utils/hf.py:36-49](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/utils/hf.py#L36-L49) — `architecture_from_hf_config` 的优先级是 `architectures`（列表，取第一个非空）→ `architecture`（单值）→ `model_type` 映射表。最后这条 `model_type` 回退对应 [sglang_omni/utils/hf.py:26-33](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/utils/hf.py#L26-L33) 的 `_CONFIG_MODEL_TYPE_TO_ARCH` 字典，用于 `config.json` 里只写了 `model_type` 没写 `architectures` 的权重。

#### 4.2.4 代码实践

**实践目标**：验证别名确实能让多个架构名命中同一个配置类。

**操作步骤**：

1. 运行下面这段（不需 GPU）：
   ```bash
   python - <<'PY'
   from sglang_omni.models.registry import PIPELINE_CONFIG_REGISTRY as R
   for arch in ["MossTTSDelayModel", "MossTTSDelay", "MossTTSDelayWithCodec"]:
       cls = R.get_config(arch)
       print(f"{arch:40s} -> {cls.__name__}")
   PY
   ```

**需要观察的现象**：三个不同的架构名（主名 + 两个别名）都映射到同一个 `MossTTSPipelineConfig` 类。

**预期结果**：三行输出的 `->` 右侧完全相同，证明别名在注册时已被展开为多个键指向同一类。

#### 4.2.5 小练习与答案

**练习 1**：如果你新建了一个模型包，忘了在配置类里写 `architecture: ClassVar[str] = "..."`，会发生什么？

**参考答案**：`_iter_config_architectures` 返回空列表，该配置类不会作为任何架构的键被写入注册表。于是 `resolve_config_cls_for_model_path` 在读到该模型权重的架构名后，`get_config` 会抛 `ValueError: Config for architecture ... not found`。也就是说，模型「装了但永远找不到」。

**练习 2**：两个不同的模型包不小心把 `architecture` 设成了同一个字符串，启动时会怎样？

**参考答案**：`import_pipeline_configs` 在第二轮写入时检测到 `existing_config_cls is not config_cls`，抛 `ValueError: Config for architecture ... is registered by both A and B`（见 [registry.py:73-82](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/registry.py#L73-L82)）。这个冲突在 import 阶段就暴露，不会潜伏到运行时。

---

### 4.3 EntryClass 与 Variants：模型包的入口约定

#### 4.3.1 概念说明

如果说 `architecture` 是配置类「对外」的匹配键，那么 `EntryClass` 是模型包「对内」告诉注册表「**我这个 `config.py` 里哪一个类才是默认入口**」的标记。一个 `config.py` 可以定义多个 `PipelineConfig` 子类（比如 text-only 版、speech 版、colocated 版），但必须用 `EntryClass = XXX` 指明一个默认的。

`EntryClass` 的值会被登记进注册表——它是「架构名 → 类」字典里的那个「类」。注意：`EntryClass` 自己必须携带有效的 `architecture` 声明（见 4.2），否则即便被指认为入口也不会被索引。

`Variants` 是可选的「变体字典」，供用户在 YAML 里通过 `config_cls` 字段显式选用非默认的配置类（见 u1-l5）。它不影响架构匹配，只影响 `get_config_cls_by_name` 的查找。

#### 4.3.2 核心流程

```
models/qwen3_omni/config.py
├─ class _Qwen3OmniBasePipelineConfig(PipelineConfig):      # 基类，architecture 在这里
│      architecture = "Qwen3OmniMoeForConditionalGeneration"
├─ class Qwen3OmniPipelineConfig(...):                       # text-only 变体
├─ class Qwen3OmniSpeechPipelineConfig(...):                 # speech 变体
├─ class Qwen3OmniSpeechColocatedPipelineConfig(...):        # colocated 变体
│
├─ EntryClass = Qwen3OmniSpeechPipelineConfig                # ← 默认入口（被注册）
└─ Variants = {"text": ..., "speech": ..., "speech-colocated": ...}  # ← 可选变体
```

注册时，`EntryClass`（即 `Qwen3OmniSpeechPipelineConfig`）的 `architecture`（继承自基类）成为注册表键；`Variants` 里的类只有在用户通过类名显式查找时才被用到。

#### 4.3.3 源码精读

`EntryClass` 是硬约定——没有它直接报错：

[sglang_omni/models/registry.py:67-71](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/registry.py#L67-L71) — 若 `config.py` 没有 `EntryClass` 属性，抛 `AssertionError`。注意这与「子模块不存在」的静默跳过不同：**一旦你提供了 `config.py`，就必须提供 `EntryClass`**，这是强约束。

Qwen3-Omni 的真实声明：

[sglang_omni/models/qwen3_omni/config.py:272-273](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/config.py#L272-L273) — `architecture` 声明在基类 `_Qwen3OmniBasePipelineConfig` 上，子类通过继承获得同一个架构名。这是常见写法：多个变体共享一个架构，区别只在 stage 拓扑。

[sglang_omni/models/qwen3_omni/config.py:376-382](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/config.py#L376-L382) — `EntryClass = Qwen3OmniSpeechPipelineConfig`（默认走 8 阶段语音管线），`Variants` 字典里另外暴露 text、speech、speech-colocated 三个变体。注意 `EntryClass` 指向的是 `Qwen3OmniSpeechPipelineConfig` 而非更上层的基类——入口必须是**可实例化的叶子类**（有 `model_path` 字段等）。

`Variants` 如何被查找：

[sglang_omni/models/registry.py:121-132](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/registry.py#L121-L132) — `get_config_cls_by_name` 先按 `__name__` 匹配注册表里的类；找不到就 import 配置类所在模块，遍历它的 `Variants` 字典按类名匹配。这就是 YAML 里写 `config_cls: Qwen3OmniPipelineConfig` 能选中 text 变体的原理。

#### 4.3.4 代码实践

**实践目标**：观察「默认入口」与「变体」的区别。

**操作步骤**：

1. 运行：
   ```bash
   python - <<'PY'
   from sglang_omni.models.registry import PIPELINE_CONFIG_REGISTRY as R
   # 默认入口（架构匹配命中的类）
   default = R.get_config("Qwen3OmniMoeForConditionalGeneration")
   print("default EntryClass ->", default.__name__)
   # 通过类名选变体
   for name in ["Qwen3OmniPipelineConfig",
                "Qwen3OmniSpeechPipelineConfig",
                "Qwen3OmniSpeechColocatedPipelineConfig"]:
       print(name, "->", R.get_config_cls_by_name(name).__name__)
   PY
   ```

**需要观察的现象**：第一行输出 `Qwen3OmniSpeechPipelineConfig`（即 `EntryClass`）；后三行说明即便它们都不在注册表的键里，仍能通过 `Variants` 按类名找到。

**预期结果**：默认架构匹配命中 `EntryClass` 指定的 speech 版；三个变体类都能被 `get_config_cls_by_name` 解析到，证明 `Variants` 提供了「同一架构、不同拓扑」的选用通道。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `EntryClass` 通常指向一个叶子子类，而不是带有 `architecture` 的那个基类？

**参考答案**：因为注册表返回的类会被 `ConfigManager` 实例化（需要 `model_path`、`stages` 等字段）。基类（如 `_Qwen3OmniBasePipelineConfig`）只负责共享 `architecture` 声明，本身缺少完整的字段定义、不是可部署的具体管线；叶子类（如 `Qwen3OmniSpeechPipelineConfig`）才有完整的 stage 列表和字段，能被实例化成一份真实配置。

**练习 2**：如果用户既不传 `--config` 也不传 `config_cls`，启动 Qwen3-Omni 服务时会用哪个配置类？

**参考答案**：会用架构匹配命中的默认入口，即 `EntryClass = Qwen3OmniSpeechPipelineConfig`（8 阶段语音管线）。想用 text-only 或 colocated 版，必须在 YAML 里写 `config_cls: Qwen3OmniPipelineConfig` 等指明变体（见 u1-l5 的配置覆盖机制）。

---

### 4.4 ModelCapabilities：静态能力声明

#### 4.4.1 概念说明

`ModelCapabilities` 是一个**冻结的（frozen）dataclass**，用五个布尔字段静态描述「这个架构能支持什么」：

| 字段 | 含义 |
|------|------|
| `supports_reference_audio` | 架构能否以参考音频做条件（声音克隆） |
| `supports_batch_vocoder` | 架构能否产生批量化波形输出 |
| `supports_streaming_vocoder` | 架构能否在生成负载完成前就流式吐出 vocoder 输出 |
| `supports_cuda_graph` | 架构是否有 CUDA graph 路径 |
| `supports_torch_compile` | 架构是否有自有的 `torch.compile` 路径（含 codec/codebook/frame-sampler 编译，不限 SGLang 通用开关） |

它的声明位置很特别：**不在 `config.py`，而在模型包的 `__init__.py`**，符号名固定为 `CAPABILITIES`。查询函数 `get_model_capabilities(architecture)` 先用架构名查注册表拿到配置类，再从配置类所在包的 `__init__.py` 读 `CAPABILITIES`。

关键边界（见 2.3）：能力标志是「架构级上限」，具体 checkpoint 能不能用还由 `PipelineConfig` 的方法（如 `supports_uploaded_voice_references()`）决定。源码注释里专门举了 Qwen3-TTS CustomVoice 的例子：架构声明支持参考音频，但该 checkpoint 会拒绝上传。

#### 4.4.2 核心流程

能力声明的「挂载—查询」链路：

```
模型包 sglang_omni/models/qwen3_tts/
├── __init__.py
│      from . import config
│      CAPABILITIES = ModelCapabilities(supports_reference_audio=True, ...)
│             ↑ 符号名固定
└── config.py
       class Qwen3TTSPipelineConfig(PipelineConfig):
            architecture = "Qwen3TTSForConditionalGeneration"   # ← 配置类知道自己在哪个包
            requires_model_capabilities = True                  # ← 声明「我需要能力元数据」


查询 get_model_capabilities("Qwen3TTSForConditionalGeneration")
        │
        ├─ PIPELINE_CONFIG_REGISTRY.configs[arch] → 配置类
        ├─ 配置类.__module__ = "sglang_omni.models.qwen3_tts.config"
        ├─ 取包名 "sglang_omni.models.qwen3_tts"，import 它
        └─ 读 module.CAPABILITIES → ModelCapabilities 实例
```

注意配置类如何「找到自己的包」：靠 `__module__` 这个 Python 内置属性——它记录了「这个类是在哪个模块里定义的」，去掉末尾的 `.config` 就是模型包路径。这是一个很优雅的「反向定位」技巧。

#### 4.4.3 源码精读

能力数据类本体（冻结、全布尔、无默认值）：

[sglang_omni/models/model_capabilities.py:11-37](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/model_capabilities.py#L11-L37) — `@dataclass(frozen=True)` 保证实例创建后不可变（符合「静态架构元数据」语义）；五个字段全是 `bool` 且**没有默认值**，因此构造时必须显式给全五个（漏写会 `TypeError`）。注释明确区分了「架构能力」与「checkpoint/部署策略」两层。

查询函数的入口：

[sglang_omni/models/model_capabilities.py:40-45](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/model_capabilities.py#L40-L45) — `get_model_capabilities` 返回 `ModelCapabilities | None`：架构未注册或包里没导出 `CAPABILITIES` 都返回 `None`。这就是为什么非 TTS 模型（如 Qwen3-Omni）查询时返回 `None`——它们没有这份声明。

「配置类 → 模型包」的反向定位：

[sglang_omni/models/model_capabilities.py:48-62](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/model_capabilities.py#L48-L62) — `_model_package_for_architecture` 用 `config_cls.__module__.rsplit(".", 1)[0]` 把 `sglang_omni.models.qwen3_tts.config` 截成 `sglang_omni.models.qwen3_tts`；`_module_model_capabilities` 读 `module.CAPABILITIES`。这里刻意做了一次延迟 import（在函数内 `from sglang_omni.models.registry import ...`），避免 `model_capabilities.py` 与 `registry.py` 之间形成 import 循环。

类型校验（防止乱填）：

[sglang_omni/models/model_capabilities.py:65-68](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/model_capabilities.py#L65-L68) — 若某包把 `CAPABILITIES` 写成了非 `ModelCapabilities` 实例（比如随手写了个字典），`_ensure_model_capabilities` 会抛 `TypeError`。配合单元测试 `test_get_model_capabilities_rejects_malformed_capabilities_export`（[test_model_capabilities.py:138-158](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/tests/unit_test/models/test_model_capabilities.py#L138-L158)），保证声明类型正确。

一个真实的能力声明（Qwen3-TTS）：

[sglang_omni/models/qwen3_tts/__init__.py:8-14](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_tts/__init__.py#L8-L14) — 注意 `supports_streaming_vocoder=False`：Qwen3-TTS 架构目前不支持流式 vocoder。对比 Higgs Audio：

[sglang_omni/models/higgs_tts/__init__.py:22-28](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/higgs_tts/__init__.py#L22-L28) — Higgs 的 `supports_streaming_vocoder=True`。这两个标志的差异会直接影响运行时是否启用流式 vocoder 调度路径（见 u4-l4）。

#### 4.4.4 代码实践

**实践目标**：亲手查询一个架构的能力，并验证「架构能力 ≠ checkpoint 部署策略」。

**操作步骤**：

1. 运行：
   ```bash
   python - <<'PY'
   from sglang_omni.models.model_capabilities import get_model_capabilities as g
   print("Qwen3-TTS:", g("Qwen3TTSForConditionalGeneration"))
   print("Higgs    :", g("HiggsMultimodalQwen3ForConditionalGeneration"))
   print("Qwen3-Omni:", g("Qwen3OmniMoeForConditionalGeneration"))   # 非 TTS，预期 None
   print("Unknown  :", g("TotallyFakeArchitecture"))                 # 未注册，预期 None
   PY
   ```

2. 验证「架构能力 vs checkpoint 策略」的区分（对应单元测试）：
   ```bash
   python - <<'PY'
   from sglang_omni.models.registry import PIPELINE_CONFIG_REGISTRY as R
   from sglang_omni.models.model_capabilities import get_model_capabilities as g
   cls = R.get_config("Qwen3TTSForConditionalGeneration")
   cfg = cls(model_path="Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice")
   print("架构支持参考音频?", g(cls.architecture).supports_reference_audio)
   print("该 checkpoint 允许上传参考音频?", cfg.supports_uploaded_voice_references())
   PY
   ```

**需要观察的现象**：第 1 步，Qwen3-TTS 与 Higgs 都返回完整 `ModelCapabilities`（且 streaming 字段不同），Qwen3-Omni 与未知架构都返回 `None`。第 2 步，「架构支持参考音频」为 `True`，但 CustomVoice checkpoint 的「允许上传」为 `False`。

**预期结果**：直观看到「架构级 `True`」与「checkpoint 级 `False`」可以并存——这正是 2.3 节强调的边界。

> 待本地验证：`supports_uploaded_voice_references()` 的具体返回值取决于 checkpoint 名字（CustomVoice 这类受保护版本会返回 `False`）。若手头没有该权重，可只读 `higgs_tts/config.py` 等 `supports_uploaded_voice_references` 的实现确认逻辑。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `ModelCapabilities` 要做成 `frozen=True` 且字段无默认值？

**参考答案**：能力是「架构级静态事实」，一旦声明就不应在运行时被改写——`frozen=True` 防止意外篡改（修改会抛 `FrozenInstanceError`，对应测试 `test_model_capabilities_are_frozen_and_explicit`）。字段无默认值则强制每个模型作者**显式**对五个能力逐一表态，避免「漏写一个就悄悄变成默认 False」的隐患。

**练习 2**：为什么 `CAPABILITIES` 声明在 `__init__.py` 而不是 `config.py`？

**参考答案**：能力是与「整个模型包」绑定的架构级元数据，而 `config.py` 里可能有多个变体配置类（text/speech/colocated），它们共享同一个架构、同一份能力——把能力挂在包的 `__init__.py` 上，语义上是「一份架构、一份能力」，且查询时只需 import 包本身（更轻量），不必触发 `config.py` 里所有配置类的定义。

---

## 5. 综合实践

把本讲四个模块串起来，完成下面这个「**逆向追踪一次模型匹配**」的任务。

**背景**：你的同事发来一个权重路径 `Qwen/Qwen3-Omni`，问你「SGLang-Omni 是怎么知道该用哪套管线的？」请用本讲学到的机制，逐步追踪并回答。

**任务步骤**：

1. **找到身份证**：仿照 `architecture_from_hf_config`，说明你会从 `Qwen/Qwen3-Omni` 的 `config.json` 里读哪个字段、得到什么架构名。
2. **找到注册入口**：在 `sglang_omni/models/qwen3_omni/config.py` 里定位 `architecture` 与 `EntryClass` 两处声明（给出文件名与行号），解释注册表在 `import_pipeline_configs` 扫描时，如何凭这两者把架构名映射到 `EntryClass`。
3. **解释自动发现**：用一句话说明「为什么框架不需要在任何中心清单里登记 Qwen3-Omni」——即 `pkgutil.iter_modules` + `EntryClass` 约定是如何实现零配置发现的。
4. **查能力**：调用 `get_model_capabilities("Qwen3OmniMoeForConditionalGeneration")`，记录返回值，并用本讲的概念解释为什么是这个结果（提示：Qwen3-Omni 是 omni 模型，不是 TTS 模型；看 `requires_model_capabilities`）。

**参考答案要点**：

- 步骤 1：读 `config.json` 的 `architectures[0]`，得到 `"Qwen3OmniMoeForConditionalGeneration"`。
- 步骤 2：`architecture` 声明在 [qwen3_omni/config.py:273](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/config.py#L273)（基类上），`EntryClass` 在 [qwen3_omni/config.py:376](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/config.py#L376)。扫描时 `import_pipeline_configs` 取 `EntryClass`，用 `_iter_config_architectures` 从它的 `architecture` 取键，写入 `configs["Qwen3OmniMoeForConditionalGeneration"] = Qwen3OmniSpeechPipelineConfig`。
- 步骤 3：因为扫描靠的是「遍历 `models/` 子包 + 每个子包的 `config.py` 必须有 `EntryClass`」这条约定，Qwen3-Omni 只要放在 `models/qwen3_omni/` 并满足约定就会被自动登记，无需修改任何中心清单。
- 步骤 4：返回 `None`。因为 Qwen3-Omni 的配置类没有设 `requires_model_capabilities=True`，其包的 `__init__.py` 也不导出 `CAPABILITIES`，故 `get_model_capabilities` 返回 `None`。能力声明目前只服务于 TTS 家族（见 `test_model_capabilities.py` 的 `EXPECTED_TTS_CAPABILITIES`）。

## 6. 本讲小结

- `PIPELINE_CONFIG_REGISTRY` 是「架构名 → `PipelineConfig` 子类」的全局字典，由 `import_pipeline_configs` 在 import 时用 `pkgutil` 自动扫描 `models/` 子包建立，靠 `@lru_cache` 只跑一次。
- 模型包的硬约定有两点：`config.py` 必须有 `EntryClass`（否则 `AssertionError`），且 `EntryClass` 必须携带有效的 `architecture` ClassVar（否则不会被索引）。
- `architecture` 是注册键，必须与 HF `config.json` 的 `architectures` 字段逐字符相等；`architecture_aliases` 用于兼容同架构的历史多命名，注册时与主名一视同仁地展开成多个键。
- 匹配链路是 `resolve_config_cls_for_model_path`：权重路径 →（`AutoConfig` / raw json / Mistral params）取架构名 → `get_config(arch)` 查表，三条回退路径应对非标准权重。
- `ModelCapabilities` 是冻结的、全布尔、无默认值的静态能力声明，固定以 `CAPABILITIES` 符号挂在模型包 `__init__.py`；查询靠配置类的 `__module__` 反向定位到包。
- 关键边界：能力标志是「架构级上限」，具体 checkpoint 的部署策略由 `PipelineConfig` 方法（如 `supports_uploaded_voice_references()`）决定，两者可以不一致。

## 7. 下一步学习建议

本讲讲清了「框架如何认出一个模型」，接下来应该看「**认出之后，这份 `PipelineConfig` 如何描述一条具体的多阶段管线**」：

- 下一讲 **u5-l2 Qwen3-Omni 端到端管线**：以 Qwen3-Omni 为例，把本讲提到的 `EntryClass`（`Qwen3OmniSpeechPipelineConfig`）展开，看它的 `stages` 列表、`route_fn`、`stream_to` 如何串起 preprocessing → encoder → thinker → talker → code2wav 全链路。建议对照本讲 4.3 节读过的 [qwen3_omni/config.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/config.py) 继续往下读。
- 若你更关心「如何接入一个全新模型」，可直接跳到 **u5-l3 TTS 模型接入流程** 和 **u7-l5 综合实战：新增一个模型家族**，它们会把本讲的 `architecture` / `EntryClass` / `CAPABILITIES` 三件套作为「接入清单」的第一步。
- 想深入理解能力标志如何影响运行时调度，可回顾 **u4-l4 流式调度器与流式 vocoder**——本讲提到的 `supports_streaming_vocoder` 正是决定是否启用那条流式路径的依据。
