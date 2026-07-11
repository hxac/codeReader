# GPU 连接器层

## 1. 本讲目标

本讲承接 [u2-l1 多硬件平台与设备抽象](u2-l1-multi-hardware-platform.md)，深入到「设备抽象」之下真正干活的层：**GPU 连接器（GPU Connector）**。LMCache 的存储后端（CPU/磁盘/远端）只能认识一种统一的、扁平的 `MemoryObj` 格式，而各家推理引擎（vLLM / SGLang / TensorRT-LLM）在不同硬件（CUDA / XPU / HPU / MUSA）上把 KV cache 存成五花八门的「分页内存（paged memory）」布局。两者之间需要一座桥，这就是 GPU 连接器。

学完本讲，你应当能够：

- 说清 `GPUConnectorInterface` 的职责：它是引擎 KV cache 布局 ↔ LMCache `MemoryObj` 之间的**双向翻译器**。
- 读懂 `VLLMPagedMemGPUConnectorV2` / `V3` 如何把 vLLM 的分页 KV cache 搬进 / 搬出 `MemoryObj`。
- 说明 `CreateGPUConnector` 如何根据 `EngineType` × `torch_device_type` × 配置开关，路由到正确的连接器类，并理解为什么 `cpu` 会抛 `RuntimeError`。
- 解释 `kv_format/` 子包如何自动检测 HND / NHD 等 KV 布局，以及为什么「布局解析」只能发生在一处。

---

## 2. 前置知识

- **KV cache 与分页内存**：注意力层为每个历史 token 计算 Key / Value 向量。推理引擎为了高效管理显存，把 KV cache 切成固定大小的「块（block）」，每块容纳 `block_size` 个 token。引擎里的张量形状因此常带有 `num_blocks (NB)` 和 `block_size (BS)` 两个维度，而不是连续的 token 维度。
- **head / head_dim**：多头注意力里，KV 每层有 `num_kv_head (NH)` 个头，每个头宽度为 `head_size (HS)`。
- **`MemoryObj` 与 `KV_2LTD`**：这是 LMCache 内部统一使用的 KV 缓存对象（见 u1-l6）。它的张量布局是 `[2, num_layers, num_tokens, hidden_dim]`——记号 `KV_2LTD` 就是「2(K/V) × Layers × Tokens × Dim」。这是一个**按 token 连续**的扁平布局，与引擎的分页布局截然不同。
- **`torch_device_type`**：在 [u2-l1](u2-l1-multi-hardware-platform.md) 讲过，进程启动时由 `_detect_device()` 探测出的全局字符串（`"cuda"` / `"xpu"` / `"hpu"` / `"musa"` / `"cpu"`），业务层据此做硬件相关的分支。
- **`lmc_ops`**：经过 monkey patch 后的合并模块（Python fallback + 编译的 C/CUDA 算子，见 u1-l3），底层搬运工作交给它的 `multi_layer_kv_transfer` 等 C 内核完成。

一句话直觉：**引擎说「我的 KV 是分页的、头维度的顺序我也没统一」；LMCache 说「我只认连续的 `KV_2LTD`」；连接器就是把这两边的「形状」和「数据」来回搬运、重排的翻译官。**

---

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [lmcache/v1/gpu_connector/__init__.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/gpu_connector/__init__.py) | 导出 `CreateGPUConnector` 工厂与 `_validate_vllm_device_features` 校验，是连接器的「总入口」。 |
| [lmcache/v1/gpu_connector/gpu_connectors.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/gpu_connector/gpu_connectors.py) | 定义抽象接口 `GPUConnectorInterface`，以及 vLLM / SGLang / TRT-LLM 在 **CUDA** 上的所有连接器实现（本讲主力 `VLLMPagedMemGPUConnectorV2/V3`）。 |
| [lmcache/v1/gpu_connector/kv_format/](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/gpu_connector/kv_format/detection.py) | KV 布局的自动检测子系统：`detection.py`（编排）、`contiguity.py`（零拷贝视图恢复）、`detectors/`（每引擎一个探测器）、`specs/`（每种格式的几何访问器）、`types.py`（`LayoutHints`）。 |
| [lmcache/v1/gpu_connector/utils.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/gpu_connector/utils.py) | 对外门面（facade）：`normalize_kv_and_discover_format`、`get_num_blocks` 等标量访问器、`need_gpu_interm_buffer`。 |
| [lmcache/python_ops_fallback.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/python_ops_fallback.py) | `EngineKVFormat` 枚举的权威定义（与 C++ `csrc/engine_kv_format.h` 对齐），列出所有已知 KV 布局。 |
| [docs/design/v1/gpu_connector/layout-invariant.md](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/docs/design/v1/gpu_connector/layout-invariant.md) | 设计文档：明确规定「布局解析只能发生在一处」这条核心不变量。 |

> 提示：LMCache 的约定是 `docs/design/` 镜像 `lmcache/` 包树。读连接器代码前，先读这份 `layout-invariant.md` 能省下大量猜测。

---

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：

1. **GPU 连接器的职责**（抽象接口 `GPUConnectorInterface`）
2. **连接器工厂**（`CreateGPUConnector` 与设备路由）
3. **主力实现**（`VLLMPagedMemGPUConnectorV2` / `V3`）
4. **KV 格式自动检测**（`kv_format/`，HND / NHD）

### 4.1 GPU 连接器的职责：KV 布局的双向翻译器

#### 4.1.1 概念说明

回顾 [u1-l6](u1-l6-engine-public-api.md)：`LMCacheEngine` 把工作委托给三个黑盒——`token_database`（切 chunk）、`gpu_connector`（格式转换）、`storage_manager`（分级存储）。`storage_manager` 只认 `MemoryObj`（`KV_2LTD` 这种连续布局），它根本不知道「分页」「头维度顺序」这些引擎细节。

但引擎的 KV cache 长这样（以 vLLM 非混合专家、非 MLA 的 flash attention 为例）：

```
List[Tensor]   # 每层一个张量
每个张量: [2, num_blocks, block_size, num_kv_head, head_size]
            ^  ^^^^^^^^^^  ^^^^^^^^^^  ^^^^^^^^^^^^^^^^^^^^
            K/V  分页块        每块token数        头×头宽
```

这是**分页**的（按 block 组织，不是按 token 连续），而且头维度顺序还分 HND / NHD 两种。要把它变成 LMCache 的 `MemoryObj`（连续的 `[2, num_layers, num_tokens, hidden_dim]`），需要两件难事：

- **形状重排**：把分页块「摊平」成按 token 连续；把每层单独的张量「拼」成跨层的统一对象。
- **内存搬运**：这是 GPU 显存到 CPU / 显存到显存的跨设备拷贝，必须用专门的 CUDA 内核（`multi_layer_kv_transfer`）以分页 + slot 映射的方式做。

GPU 连接器就是封装这两件事的对象。它对「上」暴露统一的 `to_gpu` / `from_gpu` 接口，对「下」调用 C 内核做真正的搬运。

#### 4.1.2 核心流程

连接器只有两个主方向，对应 store / retrieve 两条主链路：

```text
【store：把引擎算出的 KV 存进 LMCache】
引擎 KV cache (分页/各种布局)
        │  connector.from_gpu(memory_obj, start, end, slot_mapping=...)
        ▼
C 内核 multi_layer_kv_transfer(D2H)   ← 引擎分页张量 → memory_obj.tensor
        │
        ▼
MemoryObj (KV_2LTD 连续布局)  ──► 交给 storage_manager 落盘/落CPU/落远端

【retrieve：把缓存里的 KV 还给引擎】
MemoryObj (KV_2LTD)
        │  connector.to_gpu(memory_obj, start, end, slot_mapping=...)
        ▼
C 内核 multi_layer_kv_transfer(H2D)   ← memory_obj.tensor → 引擎分页张量
        │
        ▼
引擎 KV cache（恢复成分页布局，命中部分无需重算）
```

关键点：连接器本身**不存数据**，它只是「翻译 + 搬运」。`start` / `end` 表示这段数据在**整个 token 序列**里的起止下标；`slot_mapping` 是引擎给的「token → 物理块槽位」映射表，C 内核靠它把连续 token 写进正确的分页槽。

#### 4.1.3 源码精读

抽象接口定义在 `gpu_connectors.py`，只有 5 个抽象方法 + 1 个共享默认实现：

[lmcache/v1/gpu_connector/gpu_connectors.py:L43-L75](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/gpu_connector/gpu_connectors.py#L43-L75) — `GPUConnectorInterface` 声明 `to_gpu` / `from_gpu`（单对象版）两个核心抽象方法。注意源码里的 `# FIXME` 注释：作者也认为 `start/end` 这类「token 序列信息」本不该由连接器关心，但目前为了把 token 区间传给 C 内核暂时放在参数里。

[lmcache/v1/gpu_connector/gpu_connectors.py:L77-L128](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/gpu_connector/gpu_connectors.py#L77-L128) — 另外两个抽象方法 `batched_from_gpu` / `batched_to_gpu`，是批处理版本（一次搬多个 `MemoryObj`），以及 `get_shape`（给定 token 数，算出对应的 `MemoryObj` 形状）。docstring 特别说明：对 layerwise 连接器，`batched_to_gpu` 走的是「生成器」模式，memory obj 是通过 `generator.send()` 传进来的——这是 u2-l6 CacheBlend 相关的细节。

[lmcache/v1/gpu_connector/gpu_connectors.py:L135-L143](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/gpu_connector/gpu_connectors.py#L135-L143) — 这是一个**共享的默认实现**（不是抽象方法）：`initialize_kvcaches_ptr`。它从 `kwargs["kvcaches"]` 取出引擎传来的 KV 张量，并调用 `attempt_permute_to_contiguous_view` 把它「整理成物理连续视图」。注释点出一个核心事实：**vLLM 的 HND 张量有一个非连续的逻辑视图（NHD），必须 permute 回物理（HND）形状，C 内核才能正确寻址。** 这一句话是本讲 4.4 节的引子。

把这段抽象接口画成一张契约表：

| 方法 | 方向 | 作用 |
|---|---|---|
| `from_gpu` | 引擎 → MemoryObj | store 主链路：把分页 KV「拉」进连续的 `MemoryObj` |
| `to_gpu` | MemoryObj → 引擎 | retrieve 主链路：把 `MemoryObj`「推」回引擎分页槽 |
| `batched_*` | 同上 | 一次处理多个对象的批量版 |
| `get_shape(n)` | — | 算 n 个 token 对应的 `MemoryObj` 形状 |
| `initialize_kvcaches_ptr` | 初始化 | 首次调用时缓存引擎 KV 指针、恢复连续视图 |

#### 4.1.4 代码实践

**实践目标**：不写 GPU 代码，仅凭阅读抽象接口，验证「连接器 = 双向翻译器」这个心智模型，并用一个**无需真实 GPU** 的 mock 连接器观察接口行为。

**操作步骤**：

1. 打开 [gpu_connectors.py:L43-L144](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/gpu_connector/gpu_connectors.py#L43-L144)，确认 `GPUConnectorInterface` 一共声明了哪几个 `@abc.abstractmethod`。
2. 阅读配套的 `MockGPUConnector`（示例代码，用于测试与无 GPU 的独立模式）：

[lmcache/v1/gpu_connector/mock_gpu_connector.py:L1-L55](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/gpu_connector/mock_gpu_connector.py#L1-L55) — 这是一个 no-op 实现：所有 `to_gpu`/`from_gpu` 都是空函数，`get_shape` 根据 `kv_shape` 算形状。它证明接口可以被「什么都不做」地满足。

3. （可选，需能 `import lmcache`）写一段最小脚本，用 `EngineType.MOCK` 拿到一个 mock 连接器，观察 `get_shape`：

```python
# 示例代码：仅用于观察接口，不触发任何 GPU 操作
from lmcache.utils import EngineType
from lmcache.v1.gpu_connector import CreateGPUConnector
from lmcache.v1.metadata import LMCacheMetadata  # 需要构造一个最小 metadata

# 构造 metadata 的细节依赖项目内部结构，本片段只示意调用形态
# connector = CreateGPUConnector(config, metadata, EngineType.MOCK)
# print(type(connector).__name__)   # 期望: MockGPUConnector
# print(connector.get_shape(16))    # 期望: 一个 torch.Size
```

**需要观察的现象**：

- 抽象方法一共有 5 个（`to_gpu`、`from_gpu`、`batched_from_gpu`、`batched_to_gpu`、`get_shape`）。
- `MockGPUConnector` 没有任何搬运逻辑，只满足「形状」契约。

**预期结果**：你能口头复述「连接器只负责在 `MemoryObj` 和引擎 KV 之间双向翻译，真正的搬运由 C 内核 `multi_layer_kv_transfer` 完成」。可运行脚本部分若 `import lmcache` 触发的设备探测在你的环境失败，则标注「待本地验证」，不影响结论。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `storage_manager` 不直接读引擎的分页 KV，而必须经过连接器？

**参考答案**：`storage_manager` 是硬件无关、引擎无关的，它只认连续的 `MemoryObj`（`KV_2LTD`）。引擎 KV 是分页的、头维度顺序不统一、还可能跨设备。把这些「脏活」集中到连接器里，才能让存储后端保持简单和可复用——这是「关注点分离」。

**练习 2**：`from_gpu` 和 `to_gpu`，哪个对应 store（存入缓存），哪个对应 retrieve（取回缓存）？

**参考答案**：`from_gpu`（从 GPU 把 KV 拉进 `MemoryObj`）对应 store；`to_gpu`（把 `MemoryObj` 推回 GPU）对应 retrieve。命名以「数据相对于 GPU 的方向」为准。

---

### 4.2 连接器工厂：CreateGPUConnector 与设备路由

#### 4.2.1 概念说明

「连接器」是一大家族：vLLM、SGLang、TRT-LLM 三个引擎 × cuda / xpu / hpu / musa 四种硬件 × layerwise / 非 layerwise × V2 / V3 × 是否启用 blending……组合很多。`CreateGPUConnector` 就是这个家族的**唯一总入口（工厂函数）**：给它配置、metadata、引擎类型，它返回一个具体的连接器实例。

工厂要做三件事：

1. **校验**：拒绝当前硬件跑不了的配置（早失败，给出可读的错误）。
2. **路由**：按 `engine` × `torch_device_type` × 配置开关，选出正确的类。
3. **构造**：用 `from_metadata` 把 metadata 翻译成构造参数。

#### 4.2.2 核心流程

```text
CreateGPUConnector(config, metadata, engine, layout_hints)
   │
   ├─ use_gpu = need_gpu_interm_buffer(config)   # PD 分离时不需要 GPU 中转缓冲
   │
   ├─ 按 engine 分三大支:
   │     SGLANG ──► (musa 报错) / xpu / else(cuda)
   │     VLLM   ──► _validate_vllm_device_features ──► cuda/xpu/musa/hpu/else(报错)
   │     TRTLLM ──► TRTLLMGPUConnector
   │     MOCK   ──► MockGPUConnector
   │
   ├─ 在每个 (engine, device) 分支里，按配置开关二选一/四选一:
   │     use_layerwise? enable_blending? use_gpu_connector_v3?
   │
   └─ 返回 XxxConnector.from_metadata(metadata, use_gpu, device, layout_hints)
```

注意一个工程细节：每个分支里连接器类的 `import` 都是**延迟导入（函数内 import）**。这样在 CPU-only 主机上不会因为 `import` 一个 CUDA 专属类而崩溃——只有真正走到那个分支才加载它。

#### 4.2.3 源码精读

[lmcache/v1/gpu_connector/__init__.py:L60-L77](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/gpu_connector/__init__.py#L60-L77) — `CreateGPUConnector` 的签名与 docstring。参数 `layout_hints` 是引擎在注册 KV cache 时给的布局提示（如 `{"kv_layout": "HND"}`），透传给连接器。

[lmcache/v1/gpu_connector/__init__.py:L23-L57](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/gpu_connector/__init__.py#L23-L57) — `_validate_vllm_device_features`：在构造任何设备相关对象**之前**就把不支持组合挡掉，转成一个干净的 `ValueError`，而不是让代码崩在 `torch.cuda.Stream()` 或 `torch.device('musa:0')` 这种深层错误里。它管两类：① 某些布尔特性（`enable_blending`、`use_gpu_connector_v3`）只在 cuda/xpu 上有实现；② HPU 没有 layerwise 连接器。

[lmcache/v1/gpu_connector/__init__.py:L158-L184](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/gpu_connector/__init__.py#L158-L184) — vLLM + cuda 的核心路由：先 layerwise（再按 `enable_blending` 二选一），否则按 `use_gpu_connector_v3` 在 V2 / V3 之间选。V2 是默认主力，V3 是面向异构模型（如 DeepSeek V4 混合压缩层）的新实现。

[lmcache/v1/gpu_connector/__init__.py:L231-L232](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/gpu_connector/__init__.py#L231-L232) — **本讲实践任务的关键点**：vLLM 分支的 `else` 在这里抛 `RuntimeError(f"No supported {torch_device_type} connector found.")`。

`need_gpu_interm_buffer` 决定要不要在 GPU 上开一块中转缓冲：

[lmcache/v1/gpu_connector/utils.py:L58-L66](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/gpu_connector/utils.py#L58-L66) — PD（Prefill/Decode 分离）场景下返回 `False`，其余返回 `True`。PD 分离的 KV 传输走专门的 transfer channel，不需要这块中转缓冲。

把 `CreateGPUConnector` 的路由整理成一张表（这是本讲实践任务的产出）：

| `engine` | `torch_device_type` | 配置开关 | 选中的连接器类 |
|---|---|---|---|
| `VLLM` | `cuda` | 默认 | `VLLMPagedMemGPUConnectorV2` |
| `VLLM` | `cuda` | `use_gpu_connector_v3=True` | `VLLMPagedMemGPUConnectorV3` |
| `VLLM` | `cuda` | `use_layerwise=True`, `enable_blending=True` | `VLLMBufferLayerwiseGPUConnector` |
| `VLLM` | `cuda` | `use_layerwise=True` | `VLLMPagedMemLayerwiseGPUConnector` |
| `VLLM` | `xpu` | 默认 / v3 / layerwise | `VLLMPagedMem{XPUConnectorV2,VPUConnectorV3,...}` |
| `VLLM` | `musa` | 默认 / layerwise | `VLLMPagedMemMUSAConnectorV2` / `...LayerwiseMUSAConnector` |
| `VLLM` | `hpu` | — | `VLLMPagedMemHPUConnectorV2` |
| `VLLM` | **其他（含 `cpu`）** | — | **抛 `RuntimeError`** |
| `SGLANG` | `cuda` | 默认 / layerwise | `SGLangGPUConnector` / `SGLangLayerwiseGPUConnector` |
| `SGLANG` | `xpu` | 默认 / layerwise | `SGLangXPUConnector` / `SGLangLayerwiseXPUConnector` |
| `SGLANG` | `musa` | — | 抛 `ValueError`（SGLang on MUSA 不支持） |
| `TRTLLM` | （任意，通常 cuda） | — | `TRTLLMGPUConnector` |
| `MOCK` | — | — | `MockGPUConnector` |

#### 4.2.4 代码实践

**实践目标**（本讲指定实践任务）：在 `__init__.py` 中找到 `CreateGPUConnector` 的路由分支，列出每种 `torch_device_type` 对应的连接器类，并解释为什么 `cpu` 会抛 `RuntimeError`。

**操作步骤**：

1. 打开 [lmcache/v1/gpu_connector/__init__.py:L143-L247](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/gpu_connector/__init__.py#L143-L247)，定位 `elif engine == EngineType.VLLM:` 这一支。
2. 观察它的 `if torch_device_type == "cuda": ... elif "xpu": ... elif "musa": ... elif "hpu": ... else: raise RuntimeError(...)` 结构。
3. 把上方的「路由表」自己复述一遍（可对照 [L158-L232](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/gpu_connector/__init__.py#L158-L232)）。
4. 回答：当 `engine == VLLM` 且 `torch_device_type == "cpu"` 时，代码会走哪个分支？

**需要观察的现象 / 预期结果**：

- vLLM 分支**只显式处理了 cuda / xpu / musa / hpu 四种**。
- `cpu`（以及任何未列出的类型）不匹配任何 `if/elif`，落入末尾的 `else`，执行 `raise RuntimeError(f"No supported {torch_device_type} connector found.")`。

**为什么 cpu 会抛 RuntimeError？** 因为 LMCache 的 vLLM 连接器需要直接操作 GPU 显存（构造 `torch.cuda.Stream`、捕获 GPU 张量的 `data_ptr`、调用 `multi_layer_kv_transfer` 这类 CUDA 内核）。纯 CPU 上既没有这些 GPU 资源，也没有对应的「CPU 版分页连接器」实现，所以工厂**主动拒绝**，给出明确错误，而不是让代码在深处崩溃。这是一种「早失败（fail fast）」设计。

> 注：cpu 上跑 vLLM 时，vLLM CPU attention 后端的 KV 仍可被检测为某种格式（见 4.4），但那是「格式检测」层面的事；要在 cpu 上真正做连接器搬运，需要走 MP（多进程）路径，不在本讲的非 MP 连接器范围内。这一点标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么每个分支里的连接器类要用「函数内延迟 import」，而不是放在文件顶部？

**参考答案**：为了让 CPU-only 或单硬件主机不会因为 `import` 一个别的硬件专属类而失败。延迟 import 保证「只有真正用到这个类时才加载它」，与 [u2-l1](u2-l1-multi-hardware-platform.md) 讲的「按需加载、CLI-only 兜底」思路一致。

**练习 2**：`_validate_vllm_device_features` 为什么要写在 `CreateGPUConnector` 里、且放在所有设备专属构造**之前**？

**参考答案**：为了让不支持组合报出一个干净的 `ValueError`（带可读的「只在 cuda/xpu 支持、当前是 hpu」提示），而不是让用户面对 `torch.cuda.Stream()` 这种深层的、看不出根因的崩溃。这是 [coding_standards.md](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/docs/coding_standards.md) 里「用 `if/raise` 做校验、早失败」原则的体现。

---

### 4.3 主力实现：VLLMPagedMemGPUConnector（V2 / V3）

#### 4.3.1 概念说明

`VLLMPagedMemGPUConnectorV2` 是 vLLM + CUDA 的**默认主力连接器**，处理 vLLM 的「分页内存（paged memory）」KV cache。类 docstring 给出了它对数据的认识：

- 引擎 KV 是嵌套元组：`GPUTensor = Tuple[KVLayer, ...]`，`KVLayer = Tuple[Tensor, Tensor]`，单个张量形状 `[num_blocks, block_size, num_heads, head_size]`。
- 它产出 / 消费的 `MemoryObj` 是 `KV_2LTD` 格式（MLA 模型则是 `KV_MLA_FMT`）。

`V3` 是为**异构模型**（同一模型里不同层有不同 block_size / 头数，如 DeepSeek V4 的压缩层 + 稠密层）新增的实现，用 `KVLayerGroupsManager` 把层分成若干「组」，每组单独管理指针。本节以 V2 为主讲透，V3 点出差异。

#### 4.3.2 核心流程

```text
构造期: from_metadata(metadata)   ← 从 metadata 解析层数/块大小/头宽
        │  开两路 CUDA Stream: store_stream(收)、load_stream(发)
        ▼
首次 to_gpu/from_gpu 时:
        initialize_kvcaches_ptr(kvcaches=引擎KV)
        │  _initialize_pointers():
        │    1) normalize_kv_and_discover_format → 得到 engine_kv_format
        │    2) 收集每层 data_ptr 到 CPU 张量 → 拷到 GPU
        │    3) 读出 num_blocks / block_size / head_size / page_buffer_size
        ▼
每次 to_gpu (retrieve):
        multi_layer_kv_transfer(H2D, memory_obj.tensor → 分页KV, slot_mapping)
每次 from_gpu (store):
        multi_layer_kv_transfer(D2H, 分页KV → memory_obj.tensor, slot_mapping)
        （可选经 gpu_buffer 中转，再 copy_ 到 memory_obj）
```

#### 4.3.3 源码精读

[lmcache/v1/gpu_connector/gpu_connectors.py:L146-L204](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/gpu_connector/gpu_connectors.py#L146-L204) — V2 的 `__init__`：预分配一个 CPU 上的「层数长」int64 张量 `kv_cache_pointers`（用来装每层 GPU 张量的 `data_ptr`），并开两条独立的 CUDA Stream（`store_stream` / `load_stream`）。`use_gpu=True` 时额外开一块 GPU 中转缓冲 `gpu_buffer`。

[lmcache/v1/gpu_connector/gpu_connectors.py:L205-L242](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/gpu_connector/gpu_connectors.py#L205-L242) — `from_metadata`：把 `metadata.kv_shape = (num_layer, 2or1, chunk_size, num_kv_head, head_size)` 拆成构造参数。注意 `hidden_dim_size = num_kv_head * head_size`，这正是 `KV_2LTD` 最后一维。

[lmcache/v1/gpu_connector/gpu_connectors.py:L244-L265](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/gpu_connector/gpu_connectors.py#L244-L265) — `_initialize_pointers`：核心初始化。① 调 `normalize_kv_and_discover_format` 一次性完成「格式检测 + 连续视图恢复」（4.4 节详述）；② 把每层 `data_ptr` 写进 CPU 张量再拷到 GPU（C 内核要在 GPU 上读指针）；③ 读出 `num_blocks / block_size / head_size / page_buffer_size`。注意它按设备 `index` 缓存，避免重复初始化。

[lmcache/v1/gpu_connector/gpu_connectors.py:L267-L330](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/gpu_connector/gpu_connectors.py#L267-L330) — V2 的 `to_gpu`（retrieve 方向）。几个关键点：① 必须校验 `memory_obj` 是 `KV_2LTD`（或 MLA 时是 `KV_MLA_FMT`）；② 必须从 `kwargs` 拿到 `slot_mapping`；③ `skip_prefix_n_tokens` 处理「vLLM 已经自己缓存了的前缀块」避免读写竞争；④ 最终调 C 内核 `lmc_ops.multi_layer_kv_transfer(..., TransferDirection.H2D, ...)`，把 `memory_obj.tensor` 按 `slot_mapping[start:end]` 写进引擎分页槽。

[lmcache/v1/gpu_connector/gpu_connectors.py:L332-L403](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/gpu_connector/gpu_connectors.py#L332-L403) — V2 的 `from_gpu`（store 方向），对称地调 `TransferDirection.D2H`。它还展示了一个性能优化：若有 `gpu_buffer` 且尺寸匹配，先在 GPU 内 D2H 到 `gpu_buffer`，再 `copy_` 到 `memory_obj`，全程在 `store_stream` 上异步执行，仅在目标不是 CUDA 设备时才 `synchronize`。

[lmcache/v1/gpu_connector/gpu_connectors.py:L416-L418](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/gpu_connector/gpu_connectors.py#L416-L418) — `get_shape`：返回 `KV_2LTD` 的形状 `[kv_size, num_layers, num_tokens, hidden_dim]`（MLA 时 `kv_size=1`，否则 `2`）。这就是「2LTD」记号的由来。

V3 的差异（[L421-L530](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/gpu_connector/gpu_connectors.py#L421-L530)）：`_initialize_kv_cache_pointers` 调的是 `normalize_and_discover_per_layer_formats`（**逐层**检测格式），并构造 `metadata.kv_layer_groups_manager`，按「组」分别收集 GPU 指针。注释明确警告：如果检测到「组间 block_size 不一致」（异构压缩模型），非 MP 的 V3 路径只会把单个标量 `block_size` 传给内核，可能出错——这种情况应走 MP 路径。

#### 4.3.4 代码实践

**实践目标**：通过源码阅读，画出 V2 在 retrieve 一次（`to_gpu`）时的完整调用链，并指出 `slot_mapping` 在哪里被切片使用。

**操作步骤**：

1. 读 [to_gpu:L267-L330](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/gpu_connector/gpu_connectors.py#L267-L330)。
2. 找到 `slot_mapping[start:end]` 出现的行，回答：为什么传给 C 内核的不是整条 `slot_mapping`，而是 `[start:end]` 切片？
3. 找到 `TransferDirection.H2D`，确认方向（Host→Device，即 `MemoryObj` → 引擎 GPU KV）。
4. 用伪代码写下完整时序。

**需要观察的现象 / 预期结果**：

- `slot_mapping` 是「整条 token 序列」的映射，但本连接器这次只处理 `[start, end)` 这一段 chunk，所以切片 `slot_mapping[start:end]` 传给内核。
- docstring（L273-L278）还指出一个细节：开启前缀缓存时，`slot_mapping` 前缀部分会是 `-1`，而 `start/end` 永远不会和前缀重叠，保证内核永远看不到 `-1`。

**伪代码（示例）**：

```python
# 示例代码：to_gpu 的等价伪代码
def to_gpu(memory_obj, start, end, *, kvcaches, slot_mapping):
    self.initialize_kvcaches_ptr(kvcaches=kvcaches)     # 首次: 检测格式+缓存指针
    ptrs = self._initialize_pointers(self.kvcaches)
    assert memory_obj.metadata.fmt == MemoryFormat.KV_2LTD
    lmc_ops.multi_layer_kv_transfer(
        memory_obj.tensor, ptrs,
        slot_mapping[start:end],            # 只搬这一段 chunk
        self.device, self.page_buffer_size,
        direction=lmc_ops.TransferDirection.H2D,
        engine_kv_format=self.engine_kv_format,
        block_size=self.block_size, head_size=self.head_size,
    )
```

> 该实践为「源码阅读型」，不需要 GPU；若你想在真实 vLLM + GPU 上观察日志，可在 `_initialize_pointers` 末尾临时加一行 `logger.info(...)`，但本讲不要求改源码。

#### 4.3.5 小练习与答案

**练习 1**：V2 的 `__init__` 为什么要开两条 CUDA Stream（`store_stream` / `load_stream`）？

**参考答案**：store（`from_gpu` 把 KV 收进来）和 retrieve（`to_gpu` 把 KV 发出去）可能并发发生；用两条独立 stream 让收 / 发两路拷贝互不阻塞，提高重叠度。这也是 `batched_to_gpu` / `batched_from_gpu` 各自包一条 `with torch.cuda.stream(...)` 的原因。

**练习 2**：V2 与 V3 的根本区别是什么？

**参考答案**：V2 假设整个模型所有层共享同一种格式与同一个 `block_size`（用 `normalize_kv_and_discover_format` 整体检测一次、传单个标量 `block_size` 给内核）；V3 用 `normalize_and_discover_per_layer_formats` **逐层**检测，并通过 `KVLayerGroupsManager` 按「层组」分别管理指针，以支持异构模型（如混合压缩层）。V3 还会主动告警：遇到组间 block_size 不一致时，非 MP 路径不安全，应改走 MP。

---

### 4.4 KV 格式自动检测：HND 与 NHD

#### 4.4.1 概念说明

前面的连接器都依赖一个值：`engine_kv_format`。它回答「引擎给我的这堆 KV 张量，到底是什么布局？」。这件事比想象中复杂，因为：

- **同一种引擎，不同 attention 后端，布局不同**：vLLM 的 flash-attention 与 flash-infer，K/V 那个「2」所在的轴不同。
- **HND vs NHD**：头的维度（NH）在 block_size（BS）之前还是之后，物理排布不同。vLLM 默认 NHD，但开 `VLLM_KV_CACHE_LAYOUT=HND` 就是 HND；vLLM 的 CPU 后端实际存 HND 却「谎报」成 NHD。
- **MLA vs MHA**：DeepSeek 类的 MLA 模型每层只有一个矩阵（没有 K/V 的「2」），形状少一维。
- **分页 vs 跨层融合**：有的是「每层一个张量」，有的是「所有层融合成一个大张量」。

LMCache 的对策是设计文档 [layout-invariant.md](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/docs/design/v1/gpu_connector/layout-invariant.md) 里那条铁律：

> **`normalize_kv_and_discover_format` 是全项目唯一解析 KV 布局的地方。** 其它模块一律通过带 `EngineKVFormat` 参数的 helper 查询，禁止自己从原始 shape 推断布局。

把所有布局枚举成 `EngineKVFormat`，每个枚举值的名字就是它的形状说明书：

[lmcache/python_ops_fallback.py:L263-L300](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/python_ops_fallback.py#L263-L300) — 枚举定义。命名规则：用 `_` 连接维度记号，`X` 表示「列表层级」。例如 `NL_X_TWO_NB_BS_NH_HS` 读作 `NL × [2, NB, BS, NH, HS]`，即「外层是 NL 个元素的列表，每个元素是 `[2, NB, BS, NH, HS]` 的张量」。

#### 4.4.2 核心流程：HND 与 NHD 到底差在哪

先讲清楚 HND / NHD（用具体形状，避免歧义）：

- **NHD**（头维度在 block_size **之后**）：物理张量 `[2, NB, BS, NH, HS]` —— 先走完一个块内所有 token（BS），再换头。
- **HND**（头维度在 block_size **之前**）：物理张量 `[2, NB, NH, BS, HS]` —— 先走完一个头的所有 token（BS），再换头。

对应到枚举（取 K/V 轴在前的两例）：

- NHD → `NL_X_TWO_NB_BS_NH_HS`（`[2, NB, BS, NH, HS]`）
- HND → `NL_X_TWO_NB_NH_BS_HS`（`[2, NB, NH, BS, HS]`）

检测流程由 `detect_format` 编排，分两步：

```text
detect_format(kv_caches, serving_engine, layout_hints)
   │
   ├─ 1) attempt_permute_to_contiguous_view(kv_caches)   # 零拷贝！仅重排维度顺序
   │      把「逻辑视图非连续、但物理连续」的张量，按 stride 还原成物理形状
   │      （典型: vLLM 把物理 HND 暴露成逻辑 NHD，靠 permute 找回）
   │
   ├─ 2) get_detector(serving_engine)                     # 每引擎一个探测器
   │      detector.discover(kv_caches, layout_hints)
   │        → 量出 (list_depth, tensor_ndim)
   │        → 结合 layout_hints["kv_layout"] 判 HND/NHD
   │        → 返回 (EngineKVFormat, 规范化后的 kv_caches)
   │
   └─ 返回 (engine_kv_format, kv_caches)
```

为什么第一步必须是「零拷贝」？因为这些张量是引擎的显存，**拷贝一份代价极高**。`attempt_permute_to_contiguous_view` 只调整「如何看待这块内存」（维度顺序 / stride），不动数据本身。

#### 4.4.3 源码精读

[lmcache/v1/gpu_connector/kv_format/detection.py:L22-L47](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/gpu_connector/kv_format/detection.py#L22-L47) — `detect_format` 编排函数：先恢复连续视图，再交给引擎探测器，最后 `logger.info` 打印检测出的格式与符号形状。找不到探测器或不认识的结构时抛 `ValueError`。

[lmcache/v1/gpu_connector/kv_format/contiguity.py:L20-L73](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/gpu_connector/kv_format/contiguity.py#L20-L73) — `attempt_permute_to_contiguous_view`：对张量叶子，按 stride 大小排序维度，尝试 permute 出连续视图。docstring（L31-L33）精确描述了 vLLM HND 场景：物理 `[2, NB, NH, BS, HS]` 被暴露成逻辑 `[2, NB, BS, NH, HS]`，按 stride 排序即可无损还原。**绝不调用 `.contiguous()`（那会分配 + 拷贝）**。

[lmcache/v1/gpu_connector/kv_format/detectors/vllm.py:L25-L72](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/gpu_connector/kv_format/detectors/vllm.py#L25-L72) — vLLM 探测器 `VLLM_Detector.discover`。重点看 HND/NHD 判定（L52-L57）：

```python
# 示例代码：节选自 vllm.py 探测器的布局判定
kv_layout = layout_hints.get("kv_layout")
if torch_device_type == "cpu":
    kv_layout = "HND"          # vLLM CPU 后端存 HND 却谎报，强制纠正
elif kv_layout is None:
    kv_layout = "NHD"          # 默认 NHD
is_hnd = kv_layout == "HND"
```

随后用 `measure_list_depth_until_tensor` 量出 `(list_depth, tensor_ndim)`，再结合「2」轴在哪一位，分支返回不同的 `EngineKVFormat`。例如 `list_depth==1 and tensor_ndim==5 and shape[0]==2`（K/V 轴在最前）：HND 返回 `NL_X_TWO_NB_NH_BS_HS`，NHD 返回 `NL_X_TWO_NB_BS_NH_HS`。

[lmcache/v1/gpu_connector/kv_format/detectors/registry.py:L20-L43](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/gpu_connector/kv_format/detectors/registry.py#L20-L43) — 探测器的自动发现：用 `pkgutil.iter_modules` 扫描 `detectors/` 目录，每个文件定义一个 `EngineDetector` 子类，按 `engine_type` 建表。「新增引擎 = 丢一个新文件」，registry 本身不用改。这与 [u2-l1](u2-l1-multi-hardware-platform.md) 讲的「定义即注册」是同一套思路。

[lmcache/v1/gpu_connector/kv_format/types.py:L26-L52](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/gpu_connector/kv_format/types.py#L26-L52) — `LayoutHints` 的 TypedDict：`kv_layout`（`"NHD"` / `"HND"`）、`num_kv_heads`、`tokens_per_block`、`head_dim`。这是引擎在注册 KV cache 时传给 LMCache 的「布局提示」，是检测的重要输入。

[lmcache/v1/gpu_connector/utils.py:L187-L208](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/gpu_connector/utils.py#L187-L208) — `normalize_kv_and_discover_format`：对外门面，薄薄包一层 `detect_format`。这就是「全项目唯一布局解析入口」。V2 的 `_initialize_pointers` 调的正是它。

#### 4.4.4 代码实践

**实践目标**：验证「HND 与 NHD 的唯一区别是 NH 与 BS 两个维度的顺序」，并理解零拷贝视图恢复为什么不会动数据。

**操作步骤**：

1. 打开 [detectors/vllm.py:L59-L72](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/gpu_connector/kv_format/detectors/vllm.py#L59-L72)，对照下表，把每个 `(list_depth, tensor_ndim, size-2 轴位置)` 与返回的枚举对应起来：

| 结构 | NHD 对应枚举 | HND 对应枚举 |
|---|---|---|
| `list_depth=0, ndim=6`（跨层融合） | `NB_NL_TWO_BS_NH_HS` | `NB_NL_TWO_NH_BS_HS` |
| `list_depth=1, ndim=5, shape[0]==2` | `NL_X_TWO_NB_BS_NH_HS` | `NL_X_TWO_NB_NH_BS_HS` |
| `list_depth=1, ndim=5, shape[1]==2` | `NL_X_NB_TWO_BS_NH_HS` | `NL_X_NB_TWO_NH_BS_HS` |
| `list_depth=1, ndim=3`（MLA） | `NL_X_NB_BS_HS`（无 HND/NHD 之分） | 同左 |

2. 阅读 [contiguity.py:L20-L51](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/gpu_connector/kv_format/contiguity.py#L20-L51)，回答：函数注释里说「绝不 `.contiguous()`」，那它怎么「还原」HND 物理形状？

3. （可选，有 torch 即可，不需 GPU）用一段最小代码验证「permute 不拷贝数据」：

```python
# 示例代码：验证 permute 是零拷贝视图
import torch
phys = torch.randn(2, 4, 8, 16, 32)   # 假装是物理 HND: [2, NB, NH, BS, HS]
logical = phys.permute(0, 1, 3, 2, 4) # 暴露成逻辑 NHD: [2, NB, BS, NH, HS]
print(logical.data_ptr() == phys.data_ptr())  # 期望: True —— 同一块内存
```

**需要观察的现象 / 预期结果**：

- 表格里 HND 与 NHD 枚举名的差异，**只是 `NH` 与 `BS` 两个 token 交换了位置**，对应张量里那两个维度交换。
- 可选脚本输出 `True`，证明 `permute` 产生的是共享存储的视图，没有拷贝。

> 若你的环境装了 torch，可选脚本可直接运行得到 `True`；若没有 torch，则该结论为「待本地验证」，但源码注释已明确声明零拷贝语义。

#### 4.4.5 小练习与答案

**练习 1**：为什么 vLLM CPU 后端要在探测器里被「强制设成 HND」？

**参考答案**：vLLM 的 CPU attention 后端物理上存的是 HND 布局，但它对外「谎报」成默认的 NHD（`layout_hints` 里不给或给错）。如果不纠正，LMCache 会按 NHD 去索引一块 HND 的内存，导致读错位置。探测器在 `torch_device_type == "cpu"` 时强制 `kv_layout = "HND"` 就是补偿这个 bug。

**练习 2**：为什么设计文档禁止业务代码用 `tensor.shape[3]` 或 `len(shape) == 5` 来判断布局？

**参考答案**：因为「同一个 shape 数字在不同布局下含义不同」（`shape[3]` 在 NHD 里是 `NH`，在 HND 里却可能是别的），而且布局还涉及 list 嵌套深度、MLA/MHA、跨层融合等多个正交维度。把所有判断集中到 `EngineKVFormat` + `kv_format` 层，能避免「每个消费者各推一遍、结论互相打架」的混乱，也方便新增布局时只改一处。

**练习 3**（口算）：一个模型 `num_kv_head=8, head_size=128, num_layers=32`，请算出 `KV_2LTD` 的 `hidden_dim` 维。

**参考答案**：`hidden_dim = num_kv_head * head_size = 8 * 128 = 1024`。于是 `MemoryObj` 单层形状的最后一维是 1024（`[2, 32, num_tokens, 1024]`）。

---

## 5. 综合实践

**任务**：把本讲四个模块串起来，画一张「从引擎注册 KV cache 到 LMCache 完成一次 store」的完整时序图，并标注每一步发生在哪个文件、由谁负责布局判断。

**要求覆盖的环节**：

1. 引擎向 LMCache 注册 KV cache（传入 `kvcaches` 张量 + `layout_hints`，如 `{"kv_layout": "NHD"}`）。
2. `CreateGPUConnector` 根据 `engine=VLLM`、`torch_device_type=cuda` 选出 `VLLMPagedMemGPUConnectorV2`（指出在 [__init__.py:L158-L184](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/gpu_connector/__init__.py#L158-L184)）。
3. 首次 `from_gpu`（store）时，`_initialize_pointers` 调 `normalize_kv_and_discover_format`（[utils.py:L187-L208](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/gpu_connector/utils.py#L187-L208)）做两件事：`attempt_permute_to_contiguous_view` 零拷贝恢复视图（[contiguity.py:L20-L73](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/gpu_connector/kv_format/contiguity.py#L20-L73)）、`VLLM_Detector.discover` 判出 `EngineKVFormat`（[vllm.py:L25-L72](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/gpu_connector/kv_format/detectors/vllm.py#L25-L72)）。
4. `from_gpu` 调 C 内核 `multi_layer_kv_transfer(D2H, ...)`（[gpu_connectors.py:L332-L403](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/gpu_connector/gpu_connectors.py#L332-L403)），把引擎分页 KV 搬进 `MemoryObj`（`KV_2LTD`）。
5. `MemoryObj` 交给 `storage_manager`（u2-l4 详述）。

**交付物**：一张图（手画或文字版时序）+ 一句话回答「布局判断发生在哪一步、由哪个函数独占」。

**预期结论**：布局判断**只**发生在第 3 步，由 `normalize_kv_and_discover_format` 独占；连接器后续所有操作（步骤 4）都只是「拿着 `engine_kv_format` 调内核」，不再自己判断布局。

---

## 6. 本讲小结

- **GPU 连接器 = KV 布局的双向翻译器**：在引擎分页 KV 与 LMCache 连续 `MemoryObj`（`KV_2LTD`）之间做形状重排 + 内存搬运，真正干活的是 C 内核 `multi_layer_kv_transfer`。
- **接口 `GPUConnectorInterface`** 只有 5 个抽象方法（`to_gpu` / `from_gpu` / `batched_*` / `get_shape`）+ 1 个共享默认 `initialize_kvcaches_ptr`（首次调用时缓存指针、恢复连续视图）。
- **`CreateGPUConnector` 是唯一工厂**：按 `EngineType` × `torch_device_type` × 配置开关路由；vLLM 分支只覆盖 cuda/xpu/musa/hpu，`cpu` 等未覆盖类型落入 `else` 抛 `RuntimeError`——这是「早失败」设计。
- **`VLLMPagedMemGPUConnectorV2` 是 CUDA 默认主力**：用 `slot_mapping` 把连续 token 映射进引擎分页槽，靠 `store_stream` / `load_stream` 两路 stream 提升并发；`V3` 额外支持异构层组（DeepSeek V4 类）。
- **KV 布局检测集中在 `kv_format/`**：`normalize_kv_and_discover_format` 是全项目唯一解析布局的入口；`attempt_permute_to_contiguous_view` 零拷贝还原物理视图，`VLLM_Detector` 结合 `layout_hints` 判出 HND / NHD。
- **HND vs NHD = NH 与 BS 两维度的顺序**：HND 为 `[2, NB, NH, BS, HS]`，NHD 为 `[2, NB, BS, NH, HS]`；枚举名即形状说明书，禁止业务代码自己从原始 shape 推断布局。

---

## 7. 下一步学习建议

- **纵向深入存储**：连接器产出的 `MemoryObj` 接下来交给谁？去看 [u2-l3 存储后端层次结构](u2-l3-storage-backend-hierarchy.md) 和 [u2-l4 StorageManager 与异步序列化](u2-l4-storage-manager.md)，补全 store / retrieve 的后半段链路。
- **横向看其他硬件连接器**：本讲只细讲了 CUDA。可对照阅读 `xpu_connectors.py` / `hpu_connector.py` / `musa_connectors.py`，体会「接口相同、底层算子不同」的多硬件实现。
- **看连接器的调用方**：vLLM 怎么在 forward 里调 `to_gpu` / `from_gpu`？去看 [u2-l5 vLLM 集成适配器](u2-l5-vllm-integration.md) 里的 `register_kv_caches`、`get_num_new_matched_tokens` 等回调。
- **看 layerwise 与 blending**：`use_layerwise=True` 和 `enable_blending=True` 选出的 layerwise 连接器走的是「生成器」模式，与 [u2-l6 CacheBlend](u2-l6-cacheblend.md) 强相关，建议结合阅读。
- **看异构模型的尽头**：想理解 V3 / MP 路径如何正确处理「组间 block_size 不一致」，可先读 `kv_layer_groups.py`，再到 [u3 单元多进程架构](u3-l1-mp-architecture-overview.md) 看 MP 连接器路径。
