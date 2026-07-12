# 架构注册与后端自动选择 archs.py

## 1. 本讲目标

本讲解决一个关键问题：**当用户只给了一个模型路径 `pipeline('Qwen/Qwen2.5-7B-Instruct')`，lmdeploy 凭什么知道该用 PyTorch 引擎还是 TurboMind 引擎？又凭什么知道这是一个纯文本模型还是图文多模态模型？**

答案全部集中在一个不到 200 行的文件 `lmdeploy/archs.py` 里。它是 lmdeploy 的「路由器 / 调度台」——读一份 HuggingFace `config.json`，回答三个问题：

1. 用哪个后端？（`pytorch` / `turbomind`）
2. 用哪种引擎配置对象？（`PytorchEngineConfig` / `TurbomindEngineConfig`）
3. 用哪种任务类型与 Pipeline 类？（纯文本 `llm` / 多模态 `vlm`）

学完本讲你应该能够：

- 说清「模型架构（arch）」是什么，以及 `get_model_arch` 如何从 HF config 把它提取出来。
- 看懂 `autoget_backend` / `autoget_backend_config` 的判定分支，并能复现整条后端选择逻辑。
- 理解 `check_vl_llm` 如何区分纯文本与多模态任务，以及 `get_task` 如何把三者串成一条决策链。

## 2. 前置知识

阅读本讲前，你需要先建立以下概念（这些在 u2-l3、u1-l4 已讲过，这里只做一句话回顾）：

- **两套引擎**：lmdeploy 有 PyTorch 引擎（纯 Python，易开发）与 TurboMind 引擎（C++，追求极致性能），二者并存互补。详见 u1-l1、u1-l2。
- **两类引擎配置**：`PytorchEngineConfig` 与 `TurbomindEngineConfig` 都定义在 `lmdeploy/messages.py`，创建 pipeline 时通过 `backend_config` 传入。详见 u2-l3。
- **Pipeline 是统一入口**：用户只调 `pipeline(...)`，内部根据模型自动选后端。详见 u1-l4。
- **arch（architecture，模型架构）**：HuggingFace 的 `config.json` 里有一个 `architectures` 字段，例如 `["LlamaForCausalLM"]`，它本质上是 transformers 用来实例化模型的 **Python 类名**。lmdeploy 把这个名字当作「模型身份证」，用它来做一切路由判定。

一个核心直觉先记在心里：**archs.py 的所有判定，归根结底都来自这一个 arch 名字**。后续的 PyTorch patch（u3-l3）和 TurboMind 支持表，也都是以 arch 名字为 key 的注册表。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
|------|------|
| `lmdeploy/archs.py` | **本讲主角**。提供 `autoget_backend`、`autoget_backend_config`、`check_vl_llm`、`get_task`、`get_model_arch` 五个函数，构成后端/任务路由的全部逻辑。 |
| `lmdeploy/pipeline.py` | 调用方。`Pipeline.__init__` 里两行调用把路由结果接入引擎。 |
| `lmdeploy/turbomind/supported_models.py` | TurboMind 侧的「支持清单」`SUPPORTED_ARCHS` 与 `is_supported`，是 `autoget_backend` 判定 TurboMind 能否接手的依据。 |

## 4. 核心概念与源码讲解

本讲按 4 个最小模块拆分，对应 archs.py 里从「最底层取 arch」到「最顶层给结论」的决策链：

```
get_model_arch        ← 最底层：从 config 取 arch 名字
      │
autoget_backend       ← 用 arch 判定后端（pytorch / turbomind）
      │
autoget_backend_config← 把后端结论 + 用户配置协调成最终的 config 对象
      │
check_vl_llm + get_task ← 判定纯文本/多模态，选出 Pipeline 类
```

### 4.1 模型架构 arch：从 HF config 到一个名字

#### 4.1.1 概念说明

模型权重本身只是一堆张量，没有「自我说明」。要让 lmdeploy 知道该怎么加载它，必须先回答一个问题：**这是什么结构的模型？**

HuggingFace 的约定是：在 `config.json` 里写一个 `architectures` 字段，值是模型类的名字，例如：

```json
{ "architectures": ["Qwen2ForCausalLM"], "hidden_size": 3584, ... }
```

这个名字就是 **arch**。它是一切路由的起点——TurboMind 用它查支持清单，PyTorch 用它查 patch 映射表（见 u3-l3 的 `module_map`），多模态判定也用它查白名单。

#### 4.1.2 核心流程

`get_model_arch` 要做的，就是「读 config → 提取 arch 名字」。但现实中 HF 模型的 config 形态多样，所以它有 **3 条兜底路径**，按优先级依次尝试：

```
1. 读 config['architectures'][0]                       ← 标准路径，绝大多数模型走这里
2. 否则读 config['auto_map']['AutoModelForCausalLM']    ← 自定义模型（trust_remote_code）走这里
   取 '.' 分隔的最后一段作为类名
3. 否则读 config['language_config']['auto_map'][...]    ← 某些多模态模型把语言模型配置嵌套在
   language_config 里，再取其 auto_map 的类名
4. 都找不到 → 抛 RuntimeError
```

> 小贴士：`auto_map` 是 HuggingFace 给「需要执行远程代码的自定义模型」（`trust_remote_code=True`）用的字段，值形如 `"modeling_qwen.Qwen2ForCausalLM"`，`.` 后面才是真正的类名。

#### 4.1.3 源码精读

先看读取 config 的部分，这里用了「先 `AutoConfig`，失败再退回 `PretrainedConfig`」的双保险：

[archs.py:149-153](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/archs.py#L149-L153) —— 先用 `AutoConfig.from_pretrained` 读取，捕获异常后回退到更基础的 `PretrainedConfig.from_pretrained`。`AutoConfig` 会根据 config 里的模型类型加载对应的 config 子类，但部分自定义模型会让它抛错，所以需要兜底。

再看提取 arch 的三级判定：

[archs.py:156-164](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/archs.py#L156-L164) —— 依次尝试 `architectures` → `auto_map.AutoModelForCausalLM` → `language_config.auto_map.AutoModelForCausalLM`，每条路径都把类名取出来赋给 `arch`；若全失败则抛出明确的错误。函数最终返回 `(arch, cfg)` 元组，`cfg` 是完整的 config 对象，后续判定多模态时还会用到它。

#### 4.1.4 代码实践

**实践目标**：亲手从一份 HF config 里把 arch 名字提取出来，验证「arch 就是模型类名」这件事。

**操作步骤**：

1. 准备一个本地模型目录（任何一个从 HuggingFace 下载的模型均可，例如 `Qwen/Qwen2.5-7B-Instruct`）。如果你没有现成模型，可以直接用一个最小的、只含 `config.json` 的目录做演示。
2. 运行下面这段脚本（**示例代码**，把 `MODEL_PATH` 换成你本地的路径）：

```python
# 示例代码
from lmdeploy.archs import get_model_arch

MODEL_PATH = '/path/to/your/model'   # ← 换成你的本地模型目录
arch, cfg = get_model_arch(MODEL_PATH)
print('arch =', arch)
print('hidden_size =', getattr(cfg, 'hidden_size', None))
```

**需要观察的现象**：打印出的 `arch` 应当与你打开该模型 `config.json` 看到的 `architectures[0]` 完全一致，例如 `Qwen2ForCausalLM` 或 `LlamaForCausalLM`。

**预期结果**：`arch` 是一个形如 `XxxForCausalLM` 的字符串。若你拿一个自定义模型（带 `auto_map`）测试，可观察它是否走了第 2 条路径。

> 若本地没有模型，此项「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：一个模型的 `config.json` 里既没有 `architectures`，也没有 `auto_map`，`get_model_arch` 会发生什么？

**参考答案**：会走到 [archs.py:163-164](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/archs.py#L163-L164) 抛出 `RuntimeError(f'Could not find model architecture from config: {_cfg}')`，因为三条路径都找不到 arch。

**练习 2**：为什么读取 config 要用 `AutoConfig` 失败后再回退 `PretrainedConfig`，而不是直接只用一个？

**参考答案**：`AutoConfig` 会按 `model_type` 动态加载对应的 config 子类，功能更强但对自定义/远程代码模型兼容性较差、可能抛异常；`PretrainedConfig` 是最基础的实现，几乎不会失败。双保险保证了「只要 config.json 能读，arch 就一定能尝试解析」。

---

### 4.2 后端判定：autoget_backend

#### 4.2.1 概念说明

拿到 arch 之后，第一个决策是：**这个模型该交给 PyTorch 还是 TurboMind？**

判定原则很朴素：**优先 TurboMind（更快），但如果 TurboMind 不支持，就回退 PyTorch（更通用）。** 而「TurboMind 是否支持」由两件事决定：

1. 当前安装的 lmdeploy 是否成功编译了 TurboMind 这个 C++ 扩展（`DISABLE_TURBOMIND=1` 安装时就没有）。
2. 即使编译了，该模型的 arch 是否在 TurboMind 的支持清单里。

#### 4.2.2 核心流程

```
autoget_backend(model_path)
        │
        ▼
try: from lmdeploy.turbomind.supported_models import is_supported
     turbomind_has = is_supported(model_path)        ← 能 import 且判定支持
except ImportError:
     is_turbomind_installed = False                  ← 根本没装 TurboMind

        │
        ▼
若 TurboMind 已装但不支持该模型 → 打 warning「Fallback to pytorch ... not supported」
若 TurboMind 没装               → 打 warning「Fallback to pytorch ... not installed」

        │
        ▼
backend = 'turbomind' if turbomind_has else 'pytorch'
```

注意三个布尔量的关系：`turbomind_has` 只有在「成功 import」且「`is_supported` 返回 True」时才为 True；只要 import 失败，`turbomind_has` 保持初值 `False`，并额外记下「没装」。

而 `is_supported` 内部又是两层判定，逻辑见下一小节。

#### 4.2.3 源码精读

先看 `autoget_backend` 主体，注意它用 `try/except ImportError` 来探测 TurboMind 是否安装：

[archs.py:33-53](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/archs.py#L33-L53) —— 第 35-37 行尝试 import 并调用 `is_supported`；第 38-39 行捕获 `ImportError` 标记「未安装」；第 41-50 行分别对「装了但不支持」和「根本没装」打两条不同的 warning；第 52 行给出最终结论：`turbomind_has` 为真走 turbomind，否则回退 pytorch。

再看 TurboMind 侧的支持清单 `SUPPORTED_ARCHS`，它就是一张「arch 名字 → turbomind 内部模型代号」的映射表：

[supported_models.py:7-32](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/turbomind/supported_models.py#L7-L32) —— 例如 `Qwen2ForCausalLM='qwen2'`、`LlamaForCausalLM='llama'`、`InternLM3ForCausalLM='llama'`（InternLM3 复用 llama 的 turbomind 实现）。这张表就是 TurboMind 的「支持清单」。

最后看 `is_supported` 的两层判定：

[supported_models.py:56-72](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/turbomind/supported_models.py#L56-L72) —— 第一层（第 57-59 行）：如果模型目录下有 `triton_models/` 子目录，说明这已经是 `lmdeploy convert` 转换好的 TurboMind 原生格式，直接判定支持；第二层（第 61-70 行）：否则取 arch，先排除 `smooth_quant` 量化方法（第 62-64 行），再看 arch 是否在 `SUPPORTED_ARCHS` 里（第 66 行），并对 `Glm4MoeLiteForCausalLM` 做一个「带视觉模块则不支持」的特殊拦截（第 68-70 行）。

> 设计要点：`triton_models/` 这条「短路」路径很关键——它让 TurboMind 不必依赖 arch 名字就能认出自己转换过的模型，这也是为什么 `lmdeploy convert` 产出的目录能被 TurboMind 无条件加载。

#### 4.2.4 代码实践

**实践目标**：对一个本地 HF 模型目录调用 `autoget_backend`，观察它返回的后端名与打印的 warning。

**操作步骤**：

1. 运行下面这段**示例代码**（替换 `MODEL_PATH`）：

```python
# 示例代码
from lmdeploy.archs import autoget_backend

MODEL_PATH = '/path/to/your/model'
backend = autoget_backend(MODEL_PATH)
print('选中的后端 =', backend)
```

2. 若你用的是 `DISABLE_TURBOMIND=1` 安装的 lmdeploy（u1-l3 讲过这种纯 PyTorch 安装），观察终端是否打印 `Fallback to pytorch engine because turbomind engine is not installed correctly.`

**需要观察的现象**：

- 若模型 arch 在 `SUPPORTED_ARCHS` 表里且 TurboMind 已安装，返回 `'turbomind'`，无 warning。
- 若 arch 不在表里（例如一个较新的、TurboMind 还没支持的模型），返回 `'pytorch'`，并打印「not supported by turbomind」warning。
- 若 TurboMind 没装，无论什么模型都返回 `'pytorch'`，并打印「not installed」warning。

**预期结果**：`backend` 是 `'turbomind'` 或 `'pytorch'` 之一。若无法本地运行，此项「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：假设你用 `DISABLE_TURBOMIND=1` 安装了 lmdeploy，对一个 `LlamaForCausalLM` 模型调用 `autoget_backend`，返回值是什么？会打印什么 warning？

**参考答案**：返回 `'pytorch'`。因为 `from lmdeploy.turbomind.supported_models import is_supported` 会 `ImportError`（TurboMind 扩展没编译），`is_turbomind_installed=False`，于是打印「Fallback to pytorch engine because turbomind engine is not installed correctly. ...」。

**练习 2**：`is_supported` 为什么要在查 `SUPPORTED_ARCHS` 之前，先单独判断 `triton_models/` 目录是否存在？

**参考答案**：因为 `lmdeploy convert` 产出的 TurboMind 原生权重目录里包含 `triton_models/`，这种目录是 TurboMind 自己的格式，应当无条件被 TurboMind 加载，不必依赖 arch 名字是否在支持表里。先做这个短路判断，能让转换后的模型稳定地走 TurboMind 后端。

---

### 4.3 配置协调：autoget_backend_config

#### 4.3.1 概念说明

`autoget_backend` 只回答了「用哪个后端」，但 `Pipeline.__init__` 还需要拿到一个**具体类型的 config 对象**（`PytorchEngineConfig` 或 `TurbomindEngineConfig`）。问题来了：

- 如果用户根本没传 `backend_config`（默认 `None`），需要按后端新建一个空的。
- 如果用户传了 `PytorchEngineConfig`，应当**直接强制走 PyTorch**（用户显式选择优先于自动判定）。
- 如果用户传了 `TurbomindEngineConfig` 但自动判定却选了 PyTorch（或反之），需要把用户填写的字段**尽可能搬迁**到正确类型的 config 上。

`autoget_backend_config` 就是处理这三件事的「协调器」。

#### 4.3.2 核心流程

```
autoget_backend_config(model_path, backend_config)
        │
        ├─ backend_config 是 PytorchEngineConfig? ──是──▶ 直接返回 ('pytorch', backend_config)
        │        （用户显式短路，最高优先级）
        │
        └─ 否：backend = autoget_backend(model_path)        ← 上一节的判定
              config = PytorchEngineConfig() 或 TurbomindEngineConfig()（按 backend 新建空对象）
              │
              ├─ 用户没传 backend_config? ──是──▶ 直接返回 (backend, 新建的空 config)
              │
              └─ 用户传了（且类型与新建的相同）? ──是──▶ 直接用用户的 config
              │
              └─ 用户传了（但类型不同，即「跨后端搬迁」）:
                    遍历用户 config 的每个字段，若值非空且新 config 有同名属性，则 setattr 过去
                    再单独处理两边名字不同的字段：block_size ↔ cache_block_seq_len
```

#### 4.3.3 源码精读

先看「用户显式指定 PyTorch 就短路」这条最高优先级分支：

[archs.py:74-75](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/archs.py#L74-L75) —— 只要 `backend_config` 是 `PytorchEngineConfig` 实例，就立即返回 `('pytorch', backend_config)`，完全跳过自动判定。这就是 u1-l4 提到的「传 `PytorchEngineConfig()` 可短路强制 PyTorch」的实现位置。

再看「按后端新建空 config + 字段搬迁」：

[archs.py:77-92](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/archs.py#L77-L92) —— 第 77-78 行调用 `autoget_backend` 并按结果新建对应类型的空 config；第 79-81 行处理「用户传的 config 类型与后端一致」直接复用；第 82-91 行是关键的**跨后端字段搬迁**逻辑：用 `asdict` 把用户 config 展开成字典，逐字段 `setattr` 到新 config（第 84-86 行，条件是「值非空 `v`」且「新 config 有同名属性 `hasattr`」），并在第 87-91 行专门映射两个名字不同但语义相通的字段 `block_size` ↔ `cache_block_seq_len`。

> 注意第 84 行的条件 `if v and hasattr(config, k)`：`v` 为假（0、None、空串）的字段不会搬迁。这意味着用户若显式把某字段设成 0 来表示「用默认」，搬迁时会被跳过——这是阅读源码时值得留意的一个细节。

最后看调用方 `Pipeline.__init__` 如何把这两行串起来：

[pipeline.py:72-77](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pipeline.py#L72-L77) —— 第 72-73 行调用 `autoget_backend_config` 得到 `(backend, backend_config)`，覆盖掉局部变量；第 74-77 行紧接着调用 `get_task`（下一节）拿 Pipeline 类。这两行就是「两条后端一个 Pipeline」架构里「选后端」的全部入口。

#### 4.3.4 代码实践

**实践目标**：验证「用户传 `PytorchEngineConfig` 会短路强制 PyTorch」与「跨后端字段搬迁」两种行为。

**操作步骤**：

1. 运行下面这段**示例代码**（替换 `MODEL_PATH`）：

```python
# 示例代码
from lmdeploy.archs import autoget_backend_config
from lmdeploy.messages import PytorchEngineConfig, TurbomindEngineConfig

MODEL_PATH = '/path/to/your/model'

# 场景 A：显式传 PytorchEngineConfig —— 必定短路为 pytorch
backend_a, cfg_a = autoget_backend_config(MODEL_PATH, PytorchEngineConfig(tp=2))
print('场景A:', backend_a, type(cfg_a).__name__)

# 场景 B：什么都不传 —— 走自动判定，返回对应类型的空 config
backend_b, cfg_b = autoget_backend_config(MODEL_PATH, None)
print('场景B:', backend_b, type(cfg_b).__name__)
```

**需要观察的现象**：

- 场景 A 无论模型是否被 TurboMind 支持，`backend_a` 都应是 `'pytorch'`，`cfg_a` 是 `PytorchEngineConfig`。
- 场景 B 的后端名取决于 `autoget_backend` 的判定（可能 turbomind 也可能 pytorch），`cfg_b` 的类型随之确定。

**预期结果**：场景 A 恒为 pytorch；场景 B 与模型/安装情况有关。若无法本地运行，此项「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：用户调用 `pipeline(model_path, backend_config=TurbomindEngineConfig(tp=4))`，但该模型 arch 不在 TurboMind 支持表里。最终后端是什么？`tp=4` 还生效吗？

**参考答案**：最终后端是 `'pytorch'`（因为 `autoget_backend` 回退）。`tp=4` **仍然生效**：`autoget_backend_config` 在第 82-91 行的跨后端搬迁逻辑里，会把 `TurbomindEngineConfig` 的 `tp=4` 字段 `setattr` 到新建的 `PytorchEngineConfig` 上（两者都有 `tp` 字段）。

**练习 2**：为什么只有当 `backend_config` 是 `PytorchEngineConfig` 时才有第 74-75 行的短路，而 `TurbomindEngineConfig` 没有？

**参考答案**：因为 TurboMind 不一定可用（可能未安装，也可能 arch 不支持）。如果用户传 `TurbomindEngineConfig` 就无条件强制 turbomind，一旦 TurboMind 不可用就会直接失败，失去了「自动回退 PyTorch」的健壮性。而 PyTorch 后端只要装了 lmdeploy 就一定可用，所以可以安全短路。TurboMind 的偏好通过「自动判定 + 字段搬迁」来尊重，而非硬性强制。

---

### 4.4 任务类型判定：check_vl_llm 与 get_task

#### 4.4.1 概念说明

后端选定后，还要回答最后一个问题：**这是纯文本任务（llm）还是图文多模态任务（vlm）？**

这两类任务用的是**不同的 Pipeline 类**：

- 纯文本：`AsyncEngine`（来自 `lmdeploy.serve.core`）
- 多模态：`VLAsyncEngine`（多模态专用，会在推理前预处理图像/视频，详见 u9-l1）

判定依据仍是 arch 名字：`check_vl_llm` 内部维护了一张多模态架构白名单（如 `Qwen2VLForConditionalGeneration`、`InternVLChatModel` 等），arch 命中白名单即为多模态。

#### 4.4.2 核心流程

`get_task` 把 arch 提取与多模态判定串起来：

```
get_task(backend, model_path, backend_config)
        │
        ├─ backend_config.language_model_only 为真? ──是──▶ 返回 ('llm', AsyncEngine)
        │        （用户强制「只用语言模型部分」，跳过多模态判定）
        │
        ├─ arch, config = get_model_arch(model_path)         ← 复用 4.1 的取 arch
        │
        └─ check_vl_llm(backend, config.to_dict())?          ← 多模态白名单判定
              ├─ True  ──▶ 返回 ('vlm', VLAsyncEngine)
              └─ False ──▶ 返回 ('llm', AsyncEngine)
```

而 `check_vl_llm` 内部的判定是一组 if/elif：

```
check_vl_llm(backend, config):
  1. 若 config 有 language_config + vision_config 且语言部分是 DeepseekV2 → 多模态
  2. arch = config['architectures'][0]
  3. 若 arch == 'MultiModalityCausalLM' 且有 language_config → 多模态
  4. 若 arch 是 ChatGLM 系列 且有 vision_config → 多模态
  5. 若 arch 在 supported_archs 白名单 → 多模态
  6. 否则 → 非多模态（False）
```

> 小贴士：`language_model_only` 是引擎配置里的一个开关，用于「这个多模态模型我只想用它的语言模型部分」。一旦打开，就直接当纯文本处理，跳过 `VLAsyncEngine`。

#### 4.4.3 源码精读

先看 `check_vl_llm` 的白名单与判定分支：

[archs.py:95-122](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/archs.py#L95-L122) —— 第 97-99 行处理一类特殊的多模态 DeepseekV2 变体（同时具备 `language_config` 与 `vision_config`）；第 101 行取出 arch；第 102-112 行是核心的多模态架构白名单 `supported_archs`（含 Llava/InternVL/Qwen-VL/Qwen3VL/Gemma3/Llama4 等）；第 114-121 行按「MultiModalityCausalLM 嵌套」「ChatGLM+vision_config」「白名单命中」等分支依次判定。注意第 113 行 `turbomind_unsupported_archs = []` 目前是空列表，是预留的「某些 arch 在 turbomind 下不当多模态处理」的扩展点。

再看 `get_task` 如何调用它并返回 Pipeline 类：

[archs.py:125-140](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/archs.py#L125-L140) —— 第 130 行先 import `AsyncEngine`；第 132-133 行若用户设了 `language_model_only`，直接返回纯文本管线；第 134 行复用 `get_model_arch` 取 arch；第 135-137 行若 `check_vl_llm` 为真则延迟 import `VLAsyncEngine` 并返回多模态管线；第 140 行兜底返回纯文本管线。注意第 130、136 行都用**延迟 import**（函数内 import），是为了避免循环导入——archs.py 是底层模块，不能在文件顶部直接 import serve 层的引擎类。

最后回到调用方，看这两步如何拼接：

[pipeline.py:74-85](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pipeline.py#L74-L85) —— 第 74-77 行拿到 `pipeline_class`（`AsyncEngine` 或 `VLAsyncEngine`）；第 78-85 行用 `pipeline_class(...)` 实例化引擎，把前面确定的 `backend`、`backend_config` 一并传入。至此，「两条后端一个 Pipeline」的选型全部完成。

#### 4.4.4 代码实践

**实践目标**：用 `get_task` 和 `check_vl_llm` 判断一个模型是纯文本还是多模态，并对照其 arch 名字验证判定结果。

**操作步骤**：

1. 运行下面这段**示例代码**（替换 `MODEL_PATH` 为一个多模态模型如 `Qwen/Qwen2-VL-7B-Instruct`，或一个纯文本模型）：

```python
# 示例代码
from lmdeploy.archs import autoget_backend, get_model_arch, get_task, check_vl_llm

MODEL_PATH = '/path/to/your/model'
backend = autoget_backend(MODEL_PATH)
arch, cfg = get_model_arch(MODEL_PATH)
is_vl = check_vl_llm(backend, cfg.to_dict())
task, pipeline_cls = get_task(backend, MODEL_PATH)

print('arch        =', arch)
print('backend     =', backend)
print('is_vlm      =', is_vl)
print('task        =', task)
print('Pipeline 类 =', pipeline_cls.__name__)
```

**需要观察的现象**：

- 对纯文本模型（如 Qwen2.5-7B-Instruct，arch=`Qwen2ForCausalLM`）：`is_vlm=False`，`task='llm'`，`Pipeline 类=AsyncEngine`。
- 对多模态模型（如 Qwen2-VL，arch=`Qwen2VLForConditionalGeneration`）：`is_vlm=True`，`task='vlm'`，`Pipeline 类=VLAsyncEngine`。

**预期结果**：`task` 与 `Pipeline 类` 严格对应：`'llm'↔AsyncEngine`、`'vlm'↔VLAsyncEngine`。若无法本地运行，此项「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：`get_task` 里为什么 `from lmdeploy.serve.core import AsyncEngine` 和 `VLAsyncEngine` 都写在函数体内部（延迟 import），而不是放在文件顶部？

**参考答案**：为了避免循环导入。`archs.py` 是被 `pipeline.py`、`turbomind/supported_models.py` 等底层模块 import 的基础模块；而 `AsyncEngine`/`VLAsyncEngine` 位于 `lmdeploy.serve.core`，属于更上层，且其自身（或其依赖）很可能反过来依赖 `archs.py` 或 `messages.py`。若在文件顶部 import，就会在模块加载阶段形成循环。延迟到函数调用时才 import，打破了这个环。

**练习 2**：`check_vl_llm` 第 113 行的 `turbomind_unsupported_archs = []` 是空列表，它会影响判定结果吗？

**参考答案**：当前不会。第 118 行的 `elif arch in turbomind_unsupported_archs and backend == 'turbomind'` 分支因为列表为空，永远不会命中。这是一个预留的扩展点，留作将来「某些多模态 arch 在 turbomind 后端下不当多模态处理」时填充。

## 5. 综合实践

**综合任务**：写一个「模型体检脚本」`inspect_model.py`，输入一个模型路径，完整复现 `Pipeline.__init__` 的路由决策，并打印出一份「体检报告」。

报告应当包含：

1. 模型的 arch 名字（来自 `get_model_arch`）。
2. 该 arch 是否在 TurboMind 的 `SUPPORTED_ARCHS` 表里（直接 import 这张表查）。
3. 自动选定的后端（`autoget_backend`）。
4. 是纯文本还是多模态（`check_vl_llm`）。
5. 最终会用的 Pipeline 类（`get_task`）。

**参考实现骨架**（**示例代码**）：

```python
# 示例代码 inspect_model.py
import sys
from lmdeploy.archs import autoget_backend, get_model_arch, get_task, check_vl_llm
from lmdeploy.turbomind.supported_models import SUPPORTED_ARCHS

def inspect(model_path):
    arch, cfg = get_model_arch(model_path)
    backend = autoget_backend(model_path)
    in_tm_table = arch in SUPPORTED_ARCHS
    is_vl = check_vl_llm(backend, cfg.to_dict())
    task, pipeline_cls = get_task(backend, model_path)

    print('======== 模型体检报告 ========')
    print(f'模型路径        : {model_path}')
    print(f'arch 名字       : {arch}')
    print(f'TurboMind 支持表: {"是" if in_tm_table else "否"}')
    print(f'自动选定后端    : {backend}')
    print(f'是否多模态      : {is_vl}')
    print(f'任务类型        : {task}')
    print(f'Pipeline 类     : {pipeline_cls.__name__}')
    print('==============================')

if __name__ == '__main__':
    inspect(sys.argv[1])
```

**操作与观察**：

1. 用至少两个模型分别运行：一个纯文本（如 `Qwen/Qwen2.5-7B-Instruct`）、一个多模态（如 `Qwen/Qwen2-VL-7B-Instruct`）。
2. 对照报告，验证你的心智模型：
   - TurboMind 支持表里的 arch + 已安装 TurboMind → 后端 turbomind。
   - 多模态 arch → 任务 vlm + `VLAsyncEngine`。
3. 进阶：再传一个 `PytorchEngineConfig(tp=2)` 给 `autoget_backend_config`，确认它无视 arch 无条件返回 pytorch（验证 4.3 的短路逻辑）。

> 若本地没有这些模型或未安装 lmdeploy，相关运行结果「待本地验证」；但你可以仅靠阅读源码完成「梳理后端选择判定分支」的部分——把本讲 4.2.2、4.3.2、4.4.2 三张流程图抄一遍，就是一份完整的判定分支说明。

## 6. 本讲小结

- **archs.py 是路由器**：它读一份 HF config，依次回答「哪个后端、哪种 config、哪种 Pipeline 类」三个问题，是「两条后端一个 Pipeline」架构的决策中枢。
- **一切判定的起点是 arch 名字**：`get_model_arch` 用「`architectures` → `auto_map` → `language_config.auto_map`」三级兜底从 config 里提取出这个模型类名。
- **后端选择 = 优先 TurboMind，不支持则回退 PyTorch**：`autoget_backend` 通过探测 TurboMind 是否安装 + `is_supported`（`triton_models/` 目录 或 arch 命中 `SUPPORTED_ARCHS`）来判定。
- **用户传 `PytorchEngineConfig` 可短路强制 PyTorch**：`autoget_backend_config` 把「用户显式选择」置于「自动判定」之上，并对跨后端的情形做字段搬迁（含 `block_size ↔ cache_block_seq_len` 的名字映射）。
- **多模态判定靠 arch 白名单**：`check_vl_llm` 维护多模态架构集合，`get_task` 据此在 `AsyncEngine`（纯文本）与 `VLAsyncEngine`（多模态）间选择；`language_model_only` 可强制走纯文本。
- **延迟 import 防循环**：`get_task` 在函数体内才 import 引擎类，避免底层 archs.py 与上层 serve 模块间的循环导入。

## 7. 下一步学习建议

本讲讲清了「如何根据 arch 选后端与 Pipeline 类」，但还有两个自然延伸的方向：

1. **跟踪 PyTorch 后端的实现接入**：arch 名字在 PyTorch 侧如何映射到优化实现？请进入 u3-l3《模型 Patch 重写机制》，看 `lmdeploy/pytorch/models/module_map.py` 这张「arch → LMDeploy 重写类」的注册表——它和本讲的 `SUPPORTED_ARCHS` 是同一套思路（以 arch 名字为 key 的注册表），只是用途从「路由」变成了「代码替换」。
2. **跟踪 TurboMind 后端的实现接入**：arch 名字如何对应到 TurboMind 的 C++ 模型实现？可在 u6-l1《TurboMind 后端概览》中浏览 `src/turbomind/models/` 目录，对照本讲 `SUPPORTED_ARCHS` 表里的 turbomind 内部代号（如 `qwen2`、`llama`）找到对应的 C++ 实现。

无论走哪条路，记住一句话：**lmdeploy 用 arch 名字作为贯穿全栈的「模型身份证」**——本讲的后端选择、u3 的 patch 映射、u6 的 TurboMind 分发，都是这张身份证的不同查表动作。
