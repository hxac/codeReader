# tensorrt_llm Python 包与公共 API

## 1. 本讲目标

在上一讲（u2-l1）里，我们从「高空」认识了仓库的目录组织：`tensorrt_llm/` 是 Python 调度与 API 层，`cpp/` 是 C++/CUDA 加速层。本讲要把镜头拉近，钻进 `tensorrt_llm/` 这个 Python 包本身，回答三个问题：

1. 当你写下 `import tensorrt_llm` 的那一刻，包内部到底发生了什么？
2. 你在教程里反复见到的 `LLM`、`AsyncLLM`、`SamplingParams`、`Mapping`、`TorchLlmArgs` 这些公共对象，分别从哪里「冒出来」？
3. 包的版本号是怎么定义和暴露的？

学完后你应当能够：

- 看懂 `tensorrt_llm/__init__.py` 的导出与库初始化逻辑（包括为什么必须先 `import torch`）。
- 区分 `llmapi` 子包的角色，理解它是「面向用户的高级 API 聚合层」。
- 准确说出 `LLM`、`TorchLlmArgs`、`SamplingParams`、`Mapping` 等核心导出对象的来源与用途。
- 会用 `dir(tensorrt_llm)` 自助排查「某个类能不能从顶层包直接拿到」。

---

## 2. 前置知识

本讲默认你已经读过 u2-l1（顶层目录与代码组织）。下面补充几个理解 Python 包必备的小概念：

- **包（package）与 `__init__.py`**：在 Python 里，一个目录只要含 `__init__.py` 就是一个包。当你 `import tensorrt_llm` 时，解释器会**执行** `tensorrt_llm/__init__.py` 里的所有顶层代码。这意味着包的初始化可以带「副作用」——例如加载动态库、设置环境变量、打印日志。
- **`__all__`**：一个列表，声明「`from tensorrt_llm import *` 时会导出哪些名字」。它既是给用户的 API 契约，也是给 IDE/文档工具的提示。
- **`dataclass` vs Pydantic `BaseModel`**：两者都用来定义「带字段的配置对象」，但理念不同。`dataclass` 是标准库里轻量的「带类型的容器」；Pydantic 的 `BaseModel` 则会在构造时做严格的类型校验与解析。本讲你会看到两种风格并存：`SamplingParams` 用 `dataclass`，`TorchLlmArgs` 用 Pydantic。
- **公共 API（public API）**：指面向最终用户的、相对稳定的接口。TensorRT-LLM 仓库里有专门的「API 稳定性测试」（`tests/unittest/api_stability/`）来保护这些签名不被随意改动（见 AGENTS.md 的「Protected APIs exist」提醒）。
- **Python↔C++ 绑定**：承接 u2-l1，C++ 核心通过 nanobind 暴露给 Python。本讲里你会看到 `tensorrt_llm.bindings.executor as tllme`（简称 `tllme`），它就是 C++ 运行时对象在 Python 里的「投影」。

> 一句话心智模型：`tensorrt_llm` 包 = **顶部一层薄薄的 Python 公共 API** + 下面一整套 Python 子模块 + 通过 `bindings` 调用的 C++ 核心。

---

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用它讲什么 |
|------|------|----------------|
| [tensorrt_llm/__init__.py](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/__init__.py) | 顶层包入口，承担平台/库预加载、子模块导入、公共符号导出、库初始化 | 包初始化与公共 API 导出 |
| [tensorrt_llm/_common.py](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_common.py) | 提供 `_init()`，做一次性的 C++ 库加载与全局符号提升 | 解释 `import` 末尾的 `_init()` |
| [tensorrt_llm/llmapi/__init__.py](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/llmapi/__init__.py) | 高级 API 子包的聚合点，re-export 一大批用户面向的类 | llmapi 导出层 |
| [tensorrt_llm/sampling_params.py](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/sampling_params.py) | `SamplingParams` 等采样相关数据类 | 典型公共对象精读 |
| [tensorrt_llm/version.py](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/version.py) | 单行定义 `__version__` | 版本信息 |
| [tensorrt_llm/mapping.py](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/mapping.py) | `Mapping` 类，描述并行拓扑 | 典型公共对象速览 |
| [tensorrt_llm/llmapi/llm.py](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/llmapi/llm.py) | `LLM` 类定义 | 典型公共对象速览 |

---

## 4. 核心概念与源码讲解

### 4.1 包初始化：`import tensorrt_llm` 时发生了什么

#### 4.1.1 概念说明

很多 Python 库的 `__init__.py` 只写几行 `from .x import y`。但 TensorRT-LLM 不是一个纯 Python 库——它带着 C++/CUDA 编译产物（`.so` 动态库），还要在多机多卡、容器、venv 里跑。因此它的 `__init__.py` 必须在「用户能用 `LLM` 之前」先把环境收拾好：处理跨平台动态库、保证 `libpython` 可被加载、避免 `triton_kernels` 版本冲突，最后才导入真正的业务符号，并触发一次性的 C++ 库初始化。

理解这一段的意义在于：**当 `import tensorrt_llm` 报错时，错误往往不在你的业务代码，而在这一段初始化**（典型如 `libc10.so: cannot open shared object file`）。

#### 4.1.2 核心流程

`import tensorrt_llm` 执行 `__init__.py` 时的顺序大致是：

```text
1. 设置环境变量（关掉 UCC 集合通信后端的一个已知问题）
2. 平台相关预加载
   ├─ Windows：把 libs 目录加进 DLL 搜索路径
   └─ Linux：预加载 libpython3.x.so（venv 场景必需）
3. 接管 vendored triton_kernels（清掉冲突版本，优先用仓内自带）
4. 【关键】import torch —— 必须在加载本库的 C++ 部分之前
5. 导入子模块命名空间（models / runtime / quantization / tools ...）
6. 从各子模块 re-export 公共符号（LLM / SamplingParams / Mapping ...）
7. 定义 __all__
8. 调用 _init() —— 一次性加载 C++ 核心库
9. 打印版本号
```

第 4 步是「踩坑高发区」，下面会专门讲。

#### 4.1.3 源码精读

**第 1 步——最早设置环境变量。** 文件一开头就关掉了 OMPI 的 UCC 后端，注释说明这是「NGC PyTorch 25.12 升级前」的临时规避（workaround）：

[tensorrt_llm/__init__.py:16-19](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/__init__.py#L16-L19) —— 在任何重型 import 之前先把 `OMPI_MCA_coll_ucc_enable` 置 0，避免 allgather 出问题。

**第 2 步——Linux 预加载 libpython。** 在非系统 Python 上套一层 venv 时，`libpython3.x.so` 不会被 venv 自动符号链接，导致后续 `.so` 加载失败。这里用 `ctypes` 提前手动加载：

[tensorrt_llm/__init__.py:35-61](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/__init__.py#L35-L61) —— `_preload_python_lib()` 通过 `cdll.LoadLibrary` 先后加载 `libpythonX.Y.so.1.0` 与 `libpythonX.Y.so`。

**第 3 步——vendored triton_kernels。** 仓库自带了一份 `triton_kernels`（见 u2-l1 提到的顶层 `triton_kernels/` 目录）。这里要保证它压过环境里可能已经装了的同名包：先清 `sys.modules` 缓存，临时把仓库根加入 `sys.path`，导入后再移除：

[tensorrt_llm/__init__.py:67-100](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/__init__.py#L67-L100) —— `_setup_vendored_triton_kernels()` 确保仓内版本胜出；如果找不到会直接 `raise RuntimeError`。

**第 4 步——必须先 import torch。** 这是最容易让初学者困惑的一条规则：

[tensorrt_llm/__init__.py:102-105](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/__init__.py#L102-L105) —— 注释明确写道：本库的 C++ 共享库依赖 PyTorch 的 `libc10.so` 等符号，若不先 `import torch`，这些 `.so` 找不到依赖，会抛 `ImportError: libc10.so: cannot open shared object file`。

**第 5 步——子模块命名空间。** 下面这几行让 `tensorrt_llm.models`、`tensorrt_llm.runtime` 等作为属性可直接访问（你可以写 `tensorrt_llm.runtime.xxx`）：

[tensorrt_llm/__init__.py:107-112](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/__init__.py#L107-L112) —— 把 `torch_models`、`math_utils`、`models`、`quantization`、`runtime`、`tools` 都绑定到包属性上。

**第 6 步——re-export 公共符号。** 这是「公共 API 从哪来」的答案所在。注意来源很分散：

[tensorrt_llm/__init__.py:114-129](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/__init__.py#L114-L129) —— 例如 `LLM, AsyncLLM, MultimodalEncoder` 来自 `.llmapi`；`LlmArgs, TorchLlmArgs` 来自 `.llmapi.llm_args`；`Mapping` 来自 `.mapping`；`SamplingParams` 来自 `.sampling_params`；`__version__` 来自 `.version`；`VisualGen` 等来自 `.visual_gen`。

**第 7 步——`__all__`。** 它把上面 re-export 的名字整理成一份正式清单：

[tensorrt_llm/__init__.py:131-170](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/__init__.py#L131-L170) —— 这就是「顶层包对外承诺的公共 API 全集」。注意里面既有类（`LLM`、`Mapping`），也有子模块（`runtime`、`models`、`quantization`），甚至还有 `__version__`。

**第 8 步——一次性库初始化。** 文件末尾调用 `_init()`：

[tensorrt_llm/__init__.py:172-176](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/__init__.py#L172-L176) —— 调 `_init()`，随后打印版本号并 flush 标准输出（这就是你每次 import 都会看到的那行 `[TensorRT-LLM] TensorRT LLM version: ...`）。

`_init()` 本体在 `_common.py`，关键点是「只跑一次」和「把核心 `.so` 提升到全局符号表」：

[.../_common.py:29-44](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_common.py#L29-L44) —— `_inited` 标志位保证幂等；可用环境变量 `TRT_LLM_NO_LIB_INIT=1` 跳过（调试时有用）。

[.../_common.py:48-56](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/_common.py#L48-L56) —— 注释解释了为什么要用 `RTLD_GLOBAL` 加载 `libtensorrt_llm.so`：NIXL/UCX 的 wrapper 库是运行时 `dlopen` 的，不链接本库，只能从进程全局符号表里解析本库的符号；而 Python 扩展默认是 `RTLD_LOCAL`，所以需要这里手动「提升」。

#### 4.1.4 代码实践

**实践目标**：亲眼确认 `import tensorrt_llm` 的副作用，并验证 `_init()` 的幂等性。

**操作步骤**（需要本地已装好 TensorRT-LLM；若未安装，参见 u1-l2）：

1. 在终端进入 Python，执行：

   ```python
   import tensorrt_llm
   print(tensorrt_llm.__version__)
   ```

2. 观察终端：第一次 import 时应打印一行 `[TensorRT-LLM] TensorRT LLM version: x.y.z`（见 `__init__.py:174`）。

3. 在同一个进程里再次 `import tensorrt_llm`，观察是否还会再打印一次版本号。

**需要观察的现象**：

- 第一次会打印版本号，第二次不会（因为模块只执行一次，且 `_init()` 有 `_inited` 守卫）。

**预期结果**：

- 版本号与 `tensorrt_llm/version.py` 里的 `__version__` 一致。
- 若设了 `TRT_LLM_NO_LIB_INIT=1` 再 import，日志里会出现 `Skipping TensorRT LLM init.`。

> 如果本机没有 GPU 或没装好库，这一步无法实际运行，请标注「待本地验证」并仅做源码阅读理解。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `__init__.py` 里 `import torch` 必须出现在加载本库 C++ 部分之前？

**参考答案**：因为本库的 C++ 共享库依赖 PyTorch 的符号（如 `libc10.so`）。若先加载本库 `.so`、再 import torch，动态链接器在加载 `.so` 时找不到这些符号，会报 `ImportError: libc10.so: cannot open shared object file`。先 `import torch` 让 PyTorch 的 `.so` 与符号就位，后续加载才能成功（见 `__init__.py:102-105` 注释）。

**练习 2**：`_init()` 为什么要用 `_inited` 标志位？

**参考答案**：因为 import 是幂等的——同一进程里多次 `import tensorrt_llm` 只会执行一次 `__init__.py` 的副作用，但即便通过其他路径再次调用 `_init()`，也应当只加载一次 C++ 核心库。`_inited` 保证「一次性、幂等」，避免重复加载和重复打印（见 `_common.py:29-36`）。

---

### 4.2 llmapi 导出层：公共 API 的聚合点

#### 4.2.1 概念说明

`tensorrt_llm/llmapi/` 是一个**子包**，它的定位是「面向用户的高级 API」。你在 u1-l3 已经用过它的 `LLM.generate()`，在 u4-l1 还会深入它的 `TorchLlmArgs`。本节聚焦它的 `__init__.py`：这个文件几乎不写新逻辑，只做一件事——**把散落在子包各处的、用户需要的类，统一 re-export 出来**，让用户写 `from tensorrt_llm.llmapi import LLM` 或直接 `from tensorrt_llm import LLM` 就能拿到。

这是一种常见的设计模式：**「聚合门面（aggregating facade）」**。真实定义散在各模块（`llm.py`、`llm_args.py`、`llm_utils.py`、`mm_encoder.py`、`mpi_session.py`），门面把它们收拢成一份稳定的导入入口。好处是：内部文件重构、改名时，只要更新门面的 re-export，用户代码不必跟着改。

#### 4.2.2 核心流程

`llmapi/__init__.py` 的导出来自五类来源，可归成三组公共对象：

```text
┌─ 运行器类（怎么跑模型）─────────────────────────┐
│  LLM            ← .llm             （同步离线推理）│
│  AsyncLLM       ← .._torch.async_llm（异步流式）  │
│  MultimodalEncoder ← .mm_encoder   （多模态编码） │
│  RequestOutput / CompletionOutput ← ..executor    │
└──────────────────────────────────────────────────┘
┌─ 配置类（用什么参数跑）─────────────────────────┐
│  TorchLlmArgs / LlmArgs  ← .llm_args（顶层配置）  │
│  + 一大族子配置：SchedulerConfig / KvCacheConfig  │
│    CudaGraphConfig / MoeConfig / 各 DecodingConfig│
│  QuantConfig / QuantAlgo ← .llm_utils（量化）     │
└──────────────────────────────────────────────────┘
┌─ 采样 / 会话相关 ───────────────────────────────┐
│  SamplingParams / GuidedDecodingParams ← ..sampling_params │
│  ConversationParams / DisaggregatedParams 等     │
└──────────────────────────────────────────────────┘
```

注意：`SamplingParams`、`Mapping` 的**真实定义并不在 `llmapi/` 子包里**——`SamplingParams` 在 `tensorrt_llm/sampling_params.py`，`Mapping` 在 `tensorrt_llm/mapping.py`。门面只是把它们再转发一次。这也是为什么顶层 `tensorrt_llm/__init__.py` 既可以直接 `from .sampling_params import SamplingParams`，也可以经由 `llmapi` 拿到。

#### 4.2.3 源码精读

**re-export 来源。** 文件开头用一组 `from ... import ...` 把对象搬进来，注释里能看到每个对象的真实出生地：

[tensorrt_llm/llmapi/__init__.py:1-32](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/llmapi/__init__.py#L1-L32) —— 注意路径前缀的差异：`..sampling_params`（退到上一层包）、`.llm`（子包内）、`.llm_args`（子包内）、`.llm_utils`、`.mm_encoder`、`.mpi_session`、`.thinking_budget`。这些前缀本身就是一张「文件归属地图」。

其中最显眼的是配置族，一次性从 `.llm_args` 搬进来几十个类：

[tensorrt_llm/llmapi/__init__.py:9-27](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/llmapi/__init__.py#L9-L27) —— 这里你能一眼看到 `SchedulerConfig`、`KvCacheConfig`、`CudaGraphConfig`/`DecodeCudaGraphConfig`/`EncodeCudaGraphConfig`、`MoeConfig`、各种 `...DecodingConfig`（Eagle/Eagle3/MTP/Medusa/NGram/PARD/Lookahead/SA/DFlash/DSpark 等，对应投机解码家族）、`TorchCompileConfig`、`CapacitySchedulerPolicy` 等。这些是 u4、u8、u10 各讲的主角，本讲只需知道「它们都从 `llm_args.py` 来、都经这个门面导出」。

**`__all__` 契约。** 文件下半部分是正式导出清单：

[tensorrt_llm/llmapi/__init__.py:34-93](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/llmapi/__init__.py#L34-L93) —— 这份清单约 50 个名字，是「从 `llmapi` 拿东西」的合法范围。

**典型对象①：`LLM`。** 它的真实定义在 `llm.py`，是一个继承自内部 `_TorchLLM` 的薄壳：

[tensorrt_llm/llmapi/llm.py:1648-1664](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/llmapi/llm.py#L1648-L1664) —— `LLM.__init__` 的关键字参数（`model`、`tokenizer`、`tensor_parallel_size`、`dtype="auto"` 等）是用户最常用的入口；其余大量配置都收在 `**kwargs` 里，最终交给 `TorchLlmArgs` 解析（详见 u4-l1）。

**典型对象②：`SamplingParams`。** 这是用户每次请求都要构造的类，下一节（4.3）单独精读。

**典型对象③：`Mapping`。** 真实定义在 `mapping.py`，描述多卡并行拓扑。它的 docstring 用具体数字画出了 tp/pp/cp/moe_ep 分组的含义：

[tensorrt_llm/mapping.py:453-547](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/mapping.py#L453-L547) —— 例如「8 卡、tp_size=4、pp_size=2」会形成 `[0,1,2,3]` 与 `[4,5,6,7]` 两个 tp 组、`[0,4]/[1,5]/[2,6]/[3,7]` 四个 pp 组。

构造参数覆盖了所有并行维度：

[tensorrt_llm/mapping.py:556-575](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/mapping.py#L556-L575) —— `world_size`、`rank`、`gpus_per_node`、`cp_size`、`tp_size`、`pp_size`，以及 MoE 相关的 `moe_tp_size`/`moe_ep_size`、注意力专用的 `attn_tp_size`/`attn_cp_size`、数据并行开关 `enable_attention_dp` 等。（并行语义详见 u9-l1，本讲只认识它的「公共对象」身份。）

#### 4.2.4 代码实践

**实践目标**：用 `dir()` 自助确认「哪些公共对象能从顶层包直接拿到」，并对比顶层包与 `llmapi` 子包的导出差异。

**操作步骤**（同样需要本地已安装；未安装则按源码阅读理解并标注「待本地验证」）：

```python
# 示例代码：探查公共 API 表面
import tensorrt_llm as t
import tensorrt_llm.llmapi as api

# 1) 顶层包公开的名字（过滤掉下划线开头的内部成员）
top = [n for n in dir(t) if not n.startswith("_")]
print("顶层 __all__ 条数：", len(t.__all__))

# 2) 验证几个核心对象确实在顶层
for name in ["LLM", "AsyncLLM", "SamplingParams", "Mapping",
             "TorchLlmArgs", "LlmArgs", "__version__", "logger"]:
    print(f"{name:18} -> {hasattr(t, name)}")

# 3) 对比 llmapi 子包暴露的配置族数量
api_names = set(api.__all__)
print("llmapi 导出条数：", len(api_names))
print("是否含 SchedulerConfig：", "SchedulerConfig" in api_names)
print("是否含 KvCacheConfig：", "KvCacheConfig" in api_names)
```

**需要观察的现象**：

- 第 2 步里所有名字都应打印 `True`（说明它们都已 re-export 到顶层）。
- 第 3 步里 `llmapi.__all__` 条数明显多于顶层 `tensorrt_llm.__all__`——因为 `llmapi` 把全部配置族都导出了，而顶层只导出最常用的一小撮。

**预期结果**：

- `tensorrt_llm.__all__` 约 30+ 项；`llmapi.__all__` 约 50 项（与源码 [llmapi/__init__.py:34-93](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/llmapi/__init__.py#L34-L93) 一致）。
- 顶层 `tensorrt_llm.__all__` 里**没有** `SchedulerConfig`（它只经 `llmapi` 导出，顶层未直接列），但 `tensorrt_llm.llmapi.SchedulerConfig` 可用。这正好演示了「门面分层」。

> 说明：上面是示例代码，用于演示探查方法，不是项目自带脚本。

#### 4.2.5 小练习与答案

**练习 1**：`from tensorrt_llm import SchedulerConfig` 能成功吗？为什么？

**参考答案**：不能。顶层 `tensorrt_llm/__init__.py` 的 `__all__`（[131-170 行](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/__init__.py#L131-L170)）并未把 `SchedulerConfig` 直接 re-export，也没有把它绑成顶层属性。要拿它应写 `from tensorrt_llm.llmapi import SchedulerConfig`，因为它在 [llmapi/__init__.py:9-27](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/llmapi/__init__.py#L9-L27) 经门面导出。

**练习 2**：`SamplingParams` 的真实定义文件在哪？为什么 `from tensorrt_llm import SamplingParams` 和 `from tensorrt_llm.llmapi import SamplingParams` 都能用？

**参考答案**：真实定义在 `tensorrt_llm/sampling_params.py`。顶层 `__init__.py:125` 直接 `from .sampling_params import SamplingParams`；而 `llmapi/__init__.py:5` 又 `from ..sampling_params import ... SamplingParams` 把它转发进 `llmapi`。同一个类，两条 import 路径都能到达，这就是门面 re-export 的效果。

---

### 4.3 SamplingParams 精读与版本信息

本节把「最常用的公共数据类 `SamplingParams`」讲透，并顺带完成版本信息这一最小模块。

#### 4.3.1 概念说明

`SamplingParams` 是用户描述「这次生成要怎么采样」的对象：温度、top-k、top-p、罚分、是否 beam search、要不要返回 logprobs、guided decoding 约束……它在 u1-l3 已出场，在 u8-l3 会再深入。本讲关注它的**公共 API 设计**：它是用 `@dataclass(slots=True, kw_only=True)` 定义的，内部把字段映射到 C++ 运行时的 `tllme.SamplingConfig` / `tllme.OutputConfig`。

理解它有几个关键点：

- **它是 Python 用户面与 C++ 运行时之间的「翻译层」**：用户写语义清晰的 `temperature`/`top_p`，内部 `_get_sampling_config()` 把它们打包成 C++ 的 `tllme.SamplingConfig`。
- **它有比 C++ 更严格的校验**：`_validate()` 会拒绝一些 C++ 本可接受但 LLM API 不希望出现的组合（如贪心解码 + `best_of>1`）。
- **贪心（greedy）判定有一套统一规则**：`temperature`/`top_p`/`top_k` 都不设 = 隐式贪心；任一设为 0 或 `top_k=1` = 显式贪心。

> 版本信息：`tensorrt_llm/version.py` 只有一行 `__version__ = "1.3.0rc23"`，是全包版本号的唯一来源，被 `__init__.py:126` 导入并在 import 末尾打印（见 4.1）。

#### 4.3.2 核心流程

`SamplingParams` 的生命周期：

```text
用户构造 SamplingParams(temperature=0.8, top_p=0.9, max_tokens=128)
        │
        ▼  __post_init__()
   归一化（如 pad_id ← end_id；embedding_bias 转 tensor）
        │
        ▼  _validate()
   范围检查 + 贪心/采样一致性检查（比 C++ 更严格）
        │
   （请求提交时）
        ▼  _get_sampling_config()
   打包成 tllme.SamplingConfig 交给 C++ 运行时
        ▼  _get_output_config()
   打包成 tllme.OutputConfig（logprobs/return_*_logits 等）
```

贪心判定的判定树（决定是否走贪心解码）：

```text
use_beam_search?            —— 是 → 非贪心（走 beam search）
        │否
显式贪心?（top_k==1 或 top_p==0.0 或 temperature==0）—— 是 → 贪心
        │否
temperature/top_p/top_k 全为 None?         —— 是 且 无 top_p_decay → 贪心（隐式默认）
        │ 否
   → 采样
```

#### 4.3.3 源码精读

**类定义与定位。** 注意它带 `[TO DEVELOPER]` 注释，明确这是「面向 LLMAPI 用户的接口」，内部映射到 C++ 对象：

[tensorrt_llm/sampling_params.py:171-178](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/sampling_params.py#L171-L178) —— `@dataclass(slots=True, kw_only=True)`；`slots=True` 省 `__dict__` 省内存，`kw_only=True` 强制关键字传参（字段太多，避免位置参数混乱）。

**关键字段（节选）。** 这是用户最常碰的一组：

[tensorrt_llm/sampling_params.py:269-307](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/sampling_params.py#L269-L307) —— `max_tokens: int = 32`、`n: int = 1`、`best_of`、`use_beam_search`、`top_k`/`top_p`/`temperature`/`seed`、各类罚分（`repetition_penalty`/`presence_penalty`/`frequency_penalty`）、`min_tokens`、`min_p` 等。注意默认值里 `top_k`/`top_p`/`temperature` 都是 `None`（即「未指定」），这正是「隐式贪心」的由来。

**贪心判定的「单一事实来源」。** 源码用一组静态方法集中表达规则，注释指出下游 `sampling_utils` 也复用同一套逻辑：

[tensorrt_llm/sampling_params.py:461-498](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/sampling_params.py#L461-L498) —— `params_imply_explicit_greedy`（`top_k==1 or top_p==0.0 or temperature==0`）与 `params_imply_greedy_decoding`（综合 beam search、显式贪心、隐式贪好、top_p_decay）。把规则写成静态方法，是为了让「持有 `bindings.SamplingConfig`（而非 `SamplingParams`）的下游代码」也能调用同一套判定，避免逻辑分叉。

实例属性 `_greedy_decoding` 就是对它的包装：

[tensorrt_llm/sampling_params.py:500-508](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/sampling_params.py#L500-L508) —— `@property` 把当前实例的字段喂给静态判定函数。

**更严格的校验。** `_validate()` 里有一段专门拒绝「贪心 + 多返回」：

[tensorrt_llm/sampling_params.py:399-411](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/sampling_params.py#L399-L411) —— 注释说明：C++ Executor 其实允许贪心 + `best_of>1`，但 LLM API 不允许，除非设置环境变量 `TLLM_ALLOW_N_GREEDY_DECODING=1`。这正是「公共 API 比底层更严格」的典型例证。

**翻译到 C++。** `_get_sampling_config()` 把字段打包成 `tllme.SamplingConfig`，并处理 `n`/`best_of`/`use_beam_search` 到 C++ `num_return_sequences`/`beam_width` 的映射（注释里有一张对照表）：

[tensorrt_llm/sampling_params.py:602-632](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/sampling_params.py#L602-L632) —— 采样分支里 `num_return_sequences=best_of`、`beam_width=1`；beam search 分支里 `num_return_sequences=n`、`beam_width=best_of`。这就是「`best_of` 在 C++ 没有直接对应字段、需在后处理裁剪」的根源（注释也点明了）。

**版本信息（最小模块）。** 全包版本号来自单独的小文件，单一来源：

[tensorrt_llm/version.py:15](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/version.py#L15) —— `__version__ = "1.3.0rc23"`。这个值被 `__init__.py:126` 导入，既挂到顶层包属性 `tensorrt_llm.__version__`，又在 import 末尾被打印（`__init__.py:174`），同时被 `setup.py` 的 `BinaryDistribution` 用于打包元数据（见 u1-l2）。把版本号单独放一个文件、不在多处硬编码，是「单一事实来源（single source of truth）」的好实践。

#### 4.3.4 代码实践

**实践目标**：亲手构造几种 `SamplingParams`，观察贪心判定与校验行为，并印证版本号的来源。

**操作步骤**（可纯源码阅读理解；若本地已装则可运行并标注现象）：

```python
# 示例代码：观察 SamplingParams 的公共行为
from tensorrt_llm import SamplingParams, __version__
print("version =", __version__)                      # 应等于 version.py 的值

# (a) 隐式贪心：三个核心参数都不设
sp_a = SamplingParams(max_tokens=10)
print("a greedy?", sp_a._greedy_decoding)            # 预期 True

# (b) 显式采样
sp_b = SamplingParams(temperature=0.8, top_p=0.9, max_tokens=10)
print("b greedy?", sp_b._greedy_decoding)            # 预期 False

# (c) 显式贪心：temperature=0
sp_c = SamplingParams(temperature=0, max_tokens=10)
print("c greedy?", sp_c._greedy_decoding)            # 预期 True

# (d) 触发更严格校验：贪心 + best_of>1 应抛 ValueError
try:
    SamplingParams(temperature=0, best_of=3, max_tokens=10)
    print("d: 没抛错（意外）")
except ValueError as e:
    print("d: ValueError ->", str(e)[:60], "...")
```

**需要观察的现象**：

- (a)(c) 判定为贪心，(b) 不是。
- (d) 会抛 `ValueError`（除非设置了 `TLLM_ALLOW_N_GREEDY_DECODING=1`）。
- `__version__` 与 `tensorrt_llm/version.py:15` 完全一致。

**预期结果**：

- 输出形如 `version = 1.3.0rc23`、`a greedy? True`、`b greedy? False`、`c greedy? True`、`d: ValueError -> Greedy decoding in the LLM API does not allow multiple returns ...`。

> 若本机未安装库，以上为「源码阅读型实践」：依据 [sampling_params.py:461-498](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/sampling_params.py#L461-L498) 与 [399-411](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/sampling_params.py#L399-L411) 推断上述现象即可，标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`SamplingParams()`（全用默认）是贪心还是采样？为什么 `max_tokens` 默认是 32？

**参考答案**：是贪心。因为 `temperature`/`top_p`/`top_k` 默认都是 `None`，命中 `params_imply_greedy_decoding` 的「隐式贪心」分支（[461-498 行](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/sampling_params.py#L461-L498)）。`max_tokens` 默认 32 见 [271 行](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/sampling_params.py#L271)，是一个保守的小值，避免用户忘了设上限时无限生成。

**练习 2**：为什么把贪心判定写成 `@staticmethod` 而不是普通方法？

**参考答案**：因为下游采样代码（如 `sampling_utils.resolve_sampling_strategy`）手里只有 `bindings.SamplingConfig`（C++ 对象），并没有 `SamplingParams` 实例。把判定写成静态方法，让它只依赖入参（`temperature`/`top_p`/`top_k` 等），两种调用方都能复用同一套规则，避免「Python 侧一套规则、采样侧另一套规则」的不一致（见 [444-448 行注释](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/sampling_params.py#L444-L448)）。

---

## 5. 综合实践

把本讲的「包初始化」「llmapi 导出」「公共对象」串起来，完成下面这个**源码阅读 + API 探查**小任务：

1. **追踪一个公共对象的「身世」**：选 `Mapping`，回答三个问题——
   - 它在 `tensorrt_llm/__init__.py` 的哪一行被 re-export？（提示：[123 行附近](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/__init__.py#L123)）
   - 它是否出现在 `tensorrt_llm/__all__` 与 `llmapi/__all__` 里？分别在哪个？
   - 它的真实定义文件与构造参数是什么？（提示：[mapping.py:556-575](https://github.com/NVIDIA/TensorRT-LLM/blob/4b7d7199752f41960eedbf2846755e174940f164/tensorrt_llm/mapping.py#L556-L575)）

2. **画一张「公共 API 来源图」**：以 `tensorrt_llm`（顶层）为根，画出 `LLM`、`AsyncLLM`、`SamplingParams`、`Mapping`、`TorchLlmArgs`、`SchedulerConfig`、`__version__` 各自来自哪个文件、经由哪一层 `__init__.py` 到达用户。标注哪些只到 `llmapi`、哪些直达顶层。

3. **（可选，需本地环境）写一段最小用法**：用 `LLM` + `SamplingParams` + `Mapping` 各构造一次（`Mapping` 只构造、不必真跑模型），打印它们的类型与 `tensorrt_llm.__version__`，验证它们都从顶层包可达。

> 这个任务的核心收获：建立「公共 API = 多层 `__init__.py` 逐级 re-export 出来的稳定门面」这一心智模型。之后遇到任何「这个类从哪 import」的问题，你都知道去翻对应层的 `__all__`。

---

## 6. 本讲小结

- `import tensorrt_llm` 不是无副作用的：它会做平台预加载（Windows DLL 路径、Linux 预加载 `libpython`）、接管 vendored `triton_kernels`、**先 `import torch`** 再加载 C++ 库，最后调用一次性的 `_init()`。
- `_init()`（`_common.py`）用 `_inited` 守卫保证幂等，并把 `libtensorrt_llm.so` 以 `RTLD_GLOBAL` 提升，让 NIXL/UCX wrapper 能解析其符号；可用 `TRT_LLM_NO_LIB_INIT=1` 跳过。
- `tensorrt_llm/__init__.py` 的 `__all__` 是顶层公共 API 的正式契约，但只导出最常用的一小撮（如 `LLM`、`SamplingParams`、`Mapping`、`TorchLlmArgs`、`__version__`）。
- `llmapi/__init__.py` 是「聚合门面」，把散落在 `llm.py`/`llm_args.py`/`llm_utils.py`/`mm_encoder.py`/`mpi_session.py` 等处的类统一 re-export，导出范围比顶层更大（含 `SchedulerConfig` 等配置族）。
- `SamplingParams` 是 `@dataclass` 风格的公共数据契约，内部把字段翻译成 C++ 的 `tllme.SamplingConfig`/`OutputConfig`，并有比 C++ 更严格的 `_validate()` 和一套集中的贪心判定静态方法。
- `__version__` 由 `version.py` 单一来源定义，被顶层包属性化、import 时打印、构建打包时复用。

---

## 7. 下一步学习建议

- 想看「请求怎么一路走到底」：进入 u3（端到端推理流程总览），尤其是 u3-l1（请求全链路）与 u3-l2（PyExecutor 单步循环），把本讲的 `LLM.generate()` 入口接到调度器与前向。
- 想深入配置体系：进入 u4-l1（TorchLlmArgs 与配置层级），本讲只是点到 `SchedulerConfig`/`KvCacheConfig` 的来源，u4 会逐个拆开它们的字段。
- 想理解采样本身：进入 u8-l3（Decoder 与 Sampling），本讲的 `SamplingParams` 在那里会连上 sampler 与 guided decoder。
- 想理解并行：进入 u9-l1（Mapping 与并行策略），本讲认识的 `Mapping` 在那里会展开 TP/PP/CP/EP 的完整语义。
- 想自己改公共 API：务必先读 `CODING_GUIDELINES.md` 的 Pydantic 规范，并留意 `tests/unittest/api_stability/`——公共 API 签名受保护，改动需 code owner 评审。
