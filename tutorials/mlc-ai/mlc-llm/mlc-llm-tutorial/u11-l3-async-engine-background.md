# 异步引擎与后台循环管理

## 1. 本讲目标

本讲承接 [u11-l1](./u11-l1-mlc-engine-json-ffi.md) 建立的「Python↔C++ JSON FFI 桥 + ThreadedEngine 后台双循环」心智模型，把视角从「桥本身」拉到「桥周围的三块工程设施」上。学完后你应该能够：

1. 读懂 `EngineConfig` 这个贯穿引擎全生命周期的「配置信封」，说出它的字段分组与三档 `mode` 预设的含义。
2. 解释 `_check_engine_config` 为什么要在进 C++ 之前做一致性校验，以及构造参数如何「覆盖」`engine_config` 里的同名字段。
3. 区分三种 Python 引擎封装（`AsyncMLCEngine` / `MLCEngine` / `SyncMLCEngine`），尤其理解 `SyncMLCEngine` 为何是「手动驱动 step 循环」的精简调试版。
4. 说清 `ServerContext` 如何以进程级单例 + 上下文管理器（`__enter__/__exit__`）的形式，管理多个 `AsyncMLCEngine` 与 embedding 引擎的注册、查询与统一终止。

本讲仍以 Python 侧源码为主，不深入 C++ 引擎内部（那是 [u9](./u9-l1-engine-threaded-state.md) 与 [u10](./u10-l1-paged-kv-cache.md) 的内容）。

## 2. 前置知识

- **引擎三件套**：在 u11-l1 里我们已知 MLC LLM 在 Python 侧有三个引擎类——`AsyncMLCEngine`（异步，REST 服务器底层用）、`MLCEngine`（同步，OpenAI 风格 API）、以及 chat CLI 默认走的 `JSONFFIEngine`。前两者都继承自 `MLCEngineBase`，最终都创建同一个 C++ `ThreadedEngine`。
- **ThreadedEngine 的后台双循环**：`RunBackgroundLoop` 反复驱动引擎心跳 `Step()` 产出 delta；`RunBackgroundStreamBackLoop` 把 delta 批量回送 Python。生成与回送在两个独立后台线程里并行（详见 u11-l1）。
- **回调契约**：引擎在构造时把一个 `request_stream_callback` 注册进 C++，C++ 每产出一批 delta 就调它。同步引擎与异步引擎的差别，很大程度上就是这个回调「如何把结果交还调用者」的差别。
- **配置即契约**：`EngineConfig` 经 `asjson()` 序列化成 JSON 跨 FFI 传给 C++ 的 `reload`/`init`；C++ 反序列化后据此建 KV cache、装配动作链。Python 与 C++ 之间靠这份 JSON 对齐语义。
- **`with` 语句与上下文管理器**：Python 的 `__enter__` / `__exit__` 协议。本讲会看到 `ServerContext` 用它来保证「服务退出时所有引擎都被 terminate」。

> 名词速查：**FFI**（Foreign Function Interface，这里指 Python 调 C++ 的边界）、**PackedFunc**（TVM 的通用可调用对象，可跨语言）、**Singleton**（进程内唯一实例）、**KV cache 容量推断**（C++ 侧据显存预算反推可放多少 page，详见 u9-l4）。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
| --- | --- | --- |
| [python/mlc_llm/serve/config.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/config.py) | `EngineConfig` 数据类 | 字段分组、三档 `mode`、`asjson/from_json` |
| [python/mlc_llm/serve/engine_base.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/engine_base.py) | 引擎基类与共享工具 | `_check_engine_config`、`EngineMetrics`、`MLCEngineBase` 构造与 terminate |
| [python/mlc_llm/support/auto_device.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/support/auto_device.py) | 设备探测 | `detect_device("auto")` 的扫描顺序 |
| [python/mlc_llm/serve/sync_engine.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/sync_engine.py) | 同步调试引擎 `SyncMLCEngine` | 手动 `step()` 循环、`generate()` |
| [python/mlc_llm/serve/server/server_context.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/server/server_context.py) | 服务全局上下文 | 单例、多模型注册表、`__enter__/__exit__` 生命周期 |
| [python/mlc_llm/interface/serve.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/serve.py) | `mlc_llm serve` 启动编排 | `with ServerContext()` 如何把引擎登记进上下文 |

## 4. 核心概念与源码讲解

### 4.1 EngineConfig：引擎的中央配置信封

#### 4.1.1 概念说明

`EngineConfig` 是一个用 `@dataclass` 定义的「 Plain Old Data」容器，它**不包含任何业务逻辑**，只负责把几十个互相独立的引擎调参项打包在一起，方便一次性跨 FFI 传给 C++。可以把它理解成一张「引擎参数表」：

- 一部分字段是**用户可显式设置的旋钮**（如 `max_num_sequence`、`prefill_chunk_size`、`gpu_memory_utilization`）；
- 一部分字段是**功能开关**（如 `speculative_mode`、`prefix_cache_mode`、`prefill_mode`）；
- 还有一部分字段在 Python 侧留空（`None`），**由 C++ 侧推断填回**（如 `max_total_sequence_length` 受显存约束反推，详见 u9-l4 的 `InferrableEngineConfig::InferForKVCache`）。

正因为它的「纯数据」性质，`EngineConfig` 只有两个方法：`asjson()` 把自己序列化成 JSON 字符串送过 FFI，`from_json()` 从 JSON 字符串重建自己。

#### 4.1.2 字段分组与三档 mode

[config.py:L137-L160](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/config.py#L137-L160) 给出了全部字段的默认值。为了便于记忆，把它们分成五组：

| 分组 | 代表字段 | 语义 |
| --- | --- | --- |
| 模型定位 | `model` / `model_lib` / `additional_models` | 主模型（及推测解码小模型等附加模型）的权重目录与编译库路径 |
| 并发与上下文 | `max_num_sequence` / `max_total_sequence_length` / `max_single_sequence_length` / `prefill_chunk_size` | KV cache 能同时装多少序列、多少 token、单条多长、一次 prefill 切多大 |
| 显存与分页 | `gpu_memory_utilization` / `kv_cache_page_size` | 显存占用比例（默认 0.85）与分页 KV 的页大小（**硬编码 16**） |
| 高级特性 | `speculative_mode` / `spec_draft_length` / `prefix_cache_mode` / `prefill_mode` / `kv_state_kind` | 推测解码、前缀缓存、hybrid prefill、RNN 状态 |
| 并行 | `tensor_parallel_shards` / `pipeline_parallel_stages` | 张量并行与流水线并行的度数（仅在 JIT 编译时生效） |

最容易被初学者忽略的是 `mode`。它是三档「预设套餐」，当用户**没有显式**给出 `max_num_sequence` 等字段时，由 C++ 侧按下表自动填充：

[config.py:L25-L46](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/config.py#L25-L46) 文档说明了三档语义：

| `mode` | 场景 | `max_num_sequence` | 显存使用 |
| --- | --- | --- | --- |
| `local`（默认） | 本地低并发 | 固定 4 | 节省 |
| `interactive` | 单条交互（如 chat CLI） | 固定 1 | 最省 |
| `server` | 高并发服务 | 自动推断到尽量大 | 尽量用满 `gpu_memory_utilization` |

> 关键点：`mode` 只影响「未显式指定的字段」的默认值。一旦你手动写了 `max_num_sequence=80`，它就覆盖该字段，与 `mode` 无关。这正是 [u2-l3](./u2-l3-run-commands.md) 里「`serve` 靠 `--mode` 与 `EngineConfigOverride` 调整并发显存」的底层机制。

#### 4.1.3 源码精读：asjson / from_json

[config.py:L162-L169](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/config.py#L162-L169) 是跨 FFI 的关键两跳：

```python
def asjson(self) -> str:
    return json.dumps(asdict(self))       # dataclass → dict → JSON 字符串

@staticmethod
def from_json(json_str: str) -> "EngineConfig":
    return EngineConfig(**json.loads(json_str))  # JSON 字符串 → dict → 解包构造
```

`asjson()` 用 `dataclasses.asdict` 递归把所有字段（包括 `additional_models` 这种 list/tuple）展平成纯 dict，再 `json.dumps`。这是 Python↔C++ 之间唯一可靠的传参形式（PackedFunc 不直接认 Python dataclass）。`from_json()` 反过来，用于引擎构造结束后从 C++ 读回「**完整解析后**」的配置（见 4.2.3）。

#### 4.1.4 代码实践

**实践目标**：直观感受 `EngineConfig` 的「纯数据 + JSON 往返」性质，不依赖任何模型。

**操作步骤**：

1. 写一个最小脚本（**示例代码**，不是项目原有代码）：

   ```python
   from mlc_llm.serve.config import EngineConfig
   cfg = EngineConfig(mode="server", max_num_sequence=80, gpu_memory_utilization=0.9)
   s = cfg.asjson()
   print(s)
   cfg2 = EngineConfig.from_json(s)
   assert cfg2.max_num_sequence == 80
   ```

2. 把 `mode` 改成 `"interactive"`，观察 `asjson()` 输出里 `max_num_sequence` 是否仍是你写的值（应该是——显式值优先）。

**需要观察的现象**：`asjson()` 输出一个扁平 JSON；`from_json` 能无损还原；`max_total_sequence_length` 此刻仍是 `null`（因为它要到 C++ 侧才被推断填回）。

**预期结果**：脚本能 `import` 成功并打印 JSON；`assert` 通过。若 `import mlc_llm` 失败，说明环境未装好，需回到 [u1-l3](./u1-l3-install-and-quickstart.md) 验证安装。

#### 4.1.5 小练习与答案

**练习 1**：`kv_cache_page_size` 的默认值是多少？能否改成 32？
**答案**：默认 16；**不能**改成 32。`_check_engine_config` 会直接拒绝任何非 16 的值（见 4.2.2）。这是当前实现对分页大小的硬约束。

**练习 2**：`spec_draft_length=0` 代表什么？
**答案**：开启「自适应推测解码」——草稿长度不由用户固定，而是引擎运行时根据状态自动调整（参见 [u10-l4](./u10-l4-speculative-decoding.md) 的 `AutoSpecDecode`）。

---

### 4.2 配置校验、设备探测与指标查询

#### 4.2.1 概念说明

`EngineConfig` 是纯数据，本身不做校验。但引擎构造函数同时接受「构造参数」（`model`、`model_lib`、`mode`）和 `engine_config` 两个入口，两者都可能携带重叠信息——比如既传了 `MLCEngine(model="A")`，又在 `engine_config` 里写了 `EngineConfig(model="B")`。这种冲突若不提前拦截，会在 C++ 侧以隐晦的方式爆掉。`_check_engine_config` 就是「进 C++ 前的一致性闸门」。

与之配套的两个工具函数也归在本模块：

- **`detect_device`**：把字符串设备提示（如 `"auto"`、`"cuda:0"`）解析成 `tvm.runtime.Device` 对象；
- **`EngineMetrics`**：把引擎运行期统计（prefill/decode 吞吐等）包装成可读对象，并提供 Prometheus 文本格式。

#### 4.2.2 核心流程：_check_engine_config 的四道关

[engine_base.py:L62-L99](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/engine_base.py#L62-L99) 依次检查四件事，任一不满足就抛 `ValueError`：

```python
def _check_engine_config(model, model_lib, mode, engine_config) -> None:
    # 关1：engine_config.model 若非空，必须与构造参数 model 一致
    if engine_config.model is not None and engine_config.model != model: ...
    # 关2：engine_config.model_lib 与构造参数 model_lib 若都给定，必须一致
    if (engine_config.model_lib is not None and model_lib is not None
            and engine_config.model_lib != model_lib): ...
    # 关3：engine_config.mode 若非空，必须与构造参数 mode 一致
    if engine_config.mode is not None and engine_config.mode != mode: ...
    # 关4：kv_cache_page_size 必须为 16（硬约束）
    if engine_config.kv_cache_page_size != 16: ...
```

判读这四关的关键是「**None 表示『不指定、由对方决定』**」：

- `engine_config.model is None` 表示「我不在 config 里指定模型，让构造参数 `model` 说了算」——合法；
- `engine_config.model = "A"` 而构造参数 `model = "B"`——冲突，报错。

也就是说，`_check_engine_config` 不做「合并」，只做「冲突检测」。真正的「覆盖」发生在后面的 `MLCEngineBase.__init__`（见 4.2.3）。

`mode` 同理：构造参数 `mode` 是权威来源，`engine_config.mode` 若写了就必须与之一致，否则报错。第 4 关则是一个独立的硬性实现约束：当前 KV cache 只支持 page size 16（与 [u10-l1](./u10-l1-paged-kv-cache.md) 讲的分页设计绑定），任何其他值都直接拒绝。

#### 4.2.3 源码精读：配置覆盖与「完整配置」回读

「覆盖」真正发生在 [engine_base.py:L648-L657](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/engine_base.py#L648-L657) 的 `MLCEngineBase.__init__` 尾部：

```python
engine_config.model = model_args[0][0]            # 构造参数（经解析）覆盖 config
engine_config.model_lib = model_args[0][1]
engine_config.additional_models = model_args[1:]
engine_config.mode = mode
self._ffi["reload"](engine_config.asjson())       # 把「用户意图」整体送给 C++
self.engine_config = EngineConfig.from_json(
    self._ffi["get_complete_engine_config"]()      # 读回 C++ 推断后的「完整配置」
)
self.max_input_sequence_length = min(
    self.engine_config.max_single_sequence_length,
    self.engine_config.max_total_sequence_length,
)
```

这里有一个非常精妙的两段式：

1. **下行（`reload`）**：Python 把「用户想怎么配」序列化交给 C++。此时 `max_total_sequence_length` 等字段可能仍是 `None`。
2. **上行（`get_complete_engine_config`）**：C++ 在显存预算内推断完所有 `None` 字段后，把**完整**配置回吐给 Python，存进 `self.engine_config`。

于是 Python 侧也能拿到「引擎实际跑起来的真实参数」。`max_input_sequence_length` 取「单条上限」与「全局上限」的较小值，用于在 `process_chat_completion_request` 里校验 prompt 长度（见 u11-l1）。

注意 `_check_engine_config` 在 `__init__` 的**最前面**就被调用（[engine_base.py:L584-L586](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/engine_base.py#L584-L586)），保证冲突在任何重活（下载模型、JIT 编译、建 KV cache）开始之前就暴露。

#### 4.2.4 源码精读：detect_device 的扫描顺序

构造引擎时若 `device` 是字符串（如默认 `"auto"`），会调 [auto_device.py:L24-L44](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/support/auto_device.py#L24-L44) 的 `detect_device`。`"auto"` 分支按固定优先级逐个探测：

```python
AUTO_DETECT_DEVICES = ["cuda", "rocm", "metal", "vulkan", "opencl", "cpu"]
```

它对每个候选 `device_type` 实例化一个 `tvm.device` 并通过 `_device_exists` 探测（实际会起子进程跑 `mlc_llm.cli.check_device`，结果缓存在 `_RESULT_CACHE` 里避免重复探测）。第一个存在即胜出，故优先级是 **CUDA → ROCm → Metal → Vulkan → OpenCL → CPU**。非 `"auto"` 则直接 `tvm.device(device_hint)`，失败抛 `ValueError`。

> 这解释了 u1-l1 里「切换后端只需 `--device`」的实现：用户给个字符串，`detect_device` 把它变成 `tvm.runtime.Device` 对象，再原样传进 C++ 引擎。

#### 4.2.5 源码精读：EngineMetrics 与指标查询

[engine_base.py:L225-L270](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/engine_base.py#L225-L270) 的 `EngineMetrics` 是一个对 dict 的薄封装：支持 `["key"]` 取值、`__str__`，并提供 `prometheus_text()` 把嵌套 dict 递归展平成 Prometheus 文本格式（先输出当前层级的数值指标，再下钻子作用域）。

有意思的是获取指标的**通道**：[engine_base.py:L273-L305](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/engine_base.py#L273-L305) 的 `_query_engine_metrics` / `_async_query_engine_metrics` 不是直接调某个 C++ getter，而是**发一条带特殊 `debug_config` 的「假请求」**：

```python
dummy_message = {"role": "user", "context": ""}
for response in engine.chat.completions.create(
    messages=[dummy_message], model="model", stream=True,
    stream_options={"include_usage": True},
    extra_body={"debug_config": {"special_request": "query_engine_metrics"}},
):
    if response.usage is not None:
        return EngineMetrics(response.usage.extra)
```

即复用普通的 chat completion 通道，靠 `debug_config.special_request` 让引擎在最后那个 usage chunk 的 `extra` 字段里捎带返回指标。同步引擎（`MLCEngine.metrics()`）走 `_query_engine_metrics`，异步引擎（`AsyncMLCEngine.metrics()`）走 `_async_query_engine_metrics`，二者逻辑同构，只是一个 `for` 一个 `async for`。

#### 4.2.6 代码实践

**实践目标**：亲手触发 `_check_engine_config` 的冲突检测，验证它是「前置闸门」。

**操作步骤**（**示例代码**）：

```python
from mlc_llm.serve.config import EngineConfig
from mlc_llm.serve.engine_base import _check_engine_config

# 关1冲突：构造参数说 model=A，config 说 model=B
try:
    _check_engine_config("A", None, "local", EngineConfig(model="B"))
except ValueError as e:
    print("关1 拦截：", e)

# 关4冲突：page size 非 16
try:
    _check_engine_config("A", None, "local", EngineConfig(kv_cache_page_size=32))
except ValueError as e:
    print("关4 拦截：", e)

# 合法：config.model 留空，由构造参数决定
_check_engine_config("A", None, "local", EngineConfig())  # 不抛异常
print("合法配置通过")
```

**需要观察的现象**：前两个调用各自抛出措辞清晰的 `ValueError`，指出冲突的字段并给出修复建议；第三个调用静默通过。

**预期结果**：两处拦截信息被打印，「合法配置通过」在最末打印。这印证了「`None` 表示不指定、由对方决定」的规则。**待本地验证**：需要 `import mlc_llm` 成功的环境。

#### 4.2.7 小练习与答案

**练习 1**：为什么 `_check_engine_config` 对 `model_lib` 的检查条件是 `engine_config.model_lib is not None and model_lib is not None`，比 `model` 的检查多了一个 `and model_lib is not None`？
**答案**：因为构造参数 `model_lib` 允许为 `None`（表示「不提供库，走 JIT 编译」，见 [u1-l4](./u1-l4-workflow-and-artifacts.md)）。当构造参数都没给 `model_lib` 时，`engine_config` 里写不写都无所谓（会被 JIT 结果覆盖），没必要判冲突；只有**两边都明确给定了**库路径时，才需要确保它们一致。

**练习 2**：`detect_device("auto")` 在一台同时装了 CUDA 和 Vulkan 的机器上会返回哪个设备？
**答案**：CUDA。因为扫描顺序 `["cuda", "rocm", "metal", "vulkan", ...]` 中 cuda 在 vulkan 之前，第一个探测到即胜出。

---

### 4.3 同步引擎封装 SyncMLCEngine：手动驱动的 step 循环

#### 4.3.1 概念说明

u11-l1 已经讲过 `MLCEngine` / `AsyncMLCEngine` 这对「标准」引擎——它们都包了一个 C++ `ThreadedEngine`，后台线程自动驱动 `Step()`，你只管 `await` 或迭代结果。本模块讲「第三种」引擎：[sync_engine.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/sync_engine.py) 里的 `SyncMLCEngine`。

`SyncMLCEngine` 的本质区别在于：它**不包 ThreadedEngine、不起后台线程**，而是直接包了 C++ 的**裸 `Engine`**（注意是 `create_engine`，不是 `create_threaded_engine`）。引擎不会自己往前跑，**必须由调用者显式调用 `step()` 去推进一步**。文件开头的注释把它的定位说得很直白：

> [sync_engine.py:L2-L9](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/sync_engine.py#L2-L9)：「This engine directly wraps the underlying Engine implementation in C++, is not optimized by multi-threading and does not offer standard OpenAI API interface. We do not expose it and use it by default. As of now it mainly serves the test and debug purpose because of its simplicity.」

于是三种引擎的对比是：

| 引擎类 | 包的 C++ 对象 | 谁驱动 `Step()` | OpenAI API | 主要用途 |
| --- | --- | --- | --- | --- |
| `AsyncMLCEngine` | `ThreadedEngine` | 后台线程自动 | 有（异步） | REST 服务器 / 异步 Python |
| `MLCEngine` | `ThreadedEngine` | 后台线程自动 | 有（同步） | 同步 Python 调用 |
| `SyncMLCEngine` | 裸 `Engine` | **调用者手动 `step()`** | **无** | 测试 / 调试 / 学习 |

#### 4.3.2 核心流程：generate() 里的 while-step 循环

`SyncMLCEngine` 没有「后台循环」概念。它的 `generate()` 方法（[sync_engine.py:L159-L291](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/sync_engine.py#L159-L291)）用一个最朴素的 `while` 循环驱动引擎，直到所有生成都完成：

```python
# 注册回调：引擎在 step 中产出的 delta 直接写进本地累加器
self._ffi["set_request_stream_callback"](request_stream_callback)

# 把所有 prompt 加进引擎
for req_id, (prompt, generation_cfg) in enumerate(zip(prompts, generation_config)):
    self.add_request(self.create_request(...))

while num_finished_generations != num_total_generations:
    self.step()          # 关键：调用者主动推进一步

self._ffi["set_request_stream_callback"](original_callback)  # 还原回调
return output_texts, output_logprobs_str
```

这里的 `step()` 直接转发到 C++ 的 `step` 函数（[sync_engine.py:L343-L355](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/sync_engine.py#L343-L355)）。每一次 `step()` 内部，引擎可能选择 prefill 某个请求、或对所有运行中请求各 decode 一步（这正是 [u9-l2](./u9-l2-action-loop.md) 讲的「事件-动作循环」），随后通过回调把 delta 吐回 `request_stream_callback`，由回调更新 `output_texts` 和完成计数。

对比 `MLCEngine._generate`（[engine.py:L1834-L1904](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/engine.py#L1834-L1904)）：它**不调 `step()`**，而是从一个 `sync_output_queue` 里阻塞 `get()`——因为 `ThreadedEngine` 的后台线程在另一个线程里持续 `step()` 并把结果塞进队列。两相对照就能看清「谁在驱动循环」是两类引擎的分水岭。

#### 4.3.3 源码精读：构造与 FFI 函数集

`SyncMLCEngine.__init__`（[sync_engine.py:L88-L157](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/sync_engine.py#L88-L157)）与 `MLCEngineBase.__init__` 高度同构——都调 `_check_engine_config`、`_parse_models`、`detect_device`、`_process_model_args`。区别在两个细节：

第一，它创建的是裸引擎，FFI 函数集也不同（[sync_engine.py:L131-L144](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/sync_engine.py#L131-L144)）：

```python
self._ffi = _create_tvm_module(
    "mlc.serve.create_engine",      # 注意：不是 create_threaded_engine
    ffi_funcs=["init", "add_request", "abort_request", "step",
               "reset", "json_metrics", "get_request_stream_callback",
               "set_request_stream_callback", "create_request"],
)
```

注意里面**没有** `run_background_loop` / `run_background_stream_back_loop` / `exit_background_loop`——因为根本没有后台线程可启停。`step` 直接暴露给用户调用。

第二，回调是「可热插拔」的：`set_request_stream_callback` / `get_request_stream_callback` 允许 `generate()` 在进入时换上自己的回调、退出时还原（[sync_engine.py:L234](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/sync_engine.py#L234) 与 [sync_engine.py:L290](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/sync_engine.py#L290)）。这种「临时换回调」之所以安全，正是因为单线程、无并发——没有任何后台线程会抢着用旧回调。`ThreadedEngine` 版本不敢这么做，它的回调在 `init_threaded_engine` 时固定注册一次（[engine_base.py:L630-L634](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/engine_base.py#L630-L634)），运行期靠 `EngineState` 内部分流。

`_create_tvm_module`（[sync_engine.py:L36-L45](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/sync_engine.py#L36-L45)）是个小工具：用 `tvm.get_global_func(creator)` 取出 C++ 工厂函数并调用，再用字典推导按名字取出若干 PackedFunc 句柄。它体现了 TVM 跨语言调用的通用模式：「工厂函数返回一个 module 对象，module[key] 取出其中名为 key 的函数」。

#### 4.3.4 代码实践

**实践目标**：通过对照阅读，看清「手动 step」与「后台循环」两条路径在代码上的分野。

**操作步骤**（源码阅读型实践）：

1. 打开 [sync_engine.py:L286-L291](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/sync_engine.py#L286-L291)，确认 `SyncMLCEngine.generate` 用 `while ...: self.step()` 驱动。
2. 打开 [engine_base.py:L636-L645](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/engine_base.py#L636-L645)，确认 `MLCEngineBase` 起了 `_background_loop_thread` 与 `_background_stream_back_loop_thread` 两个线程。
3. 在 [engine.py:L1886-L1888](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/engine.py#L1886-L1888) 处确认 `MLCEngine._generate` 是 `while True: delta_outputs = self.state.sync_output_queue.get()`——等队列而不是调 step。

**需要观察的现象**：`SyncMLCEngine` 的驱动点在用户代码（`generate` 内）；`MLCEngine` 的驱动点在后台线程，用户代码只是从队列里取。两者回调机制也不同：前者热插拔，后者固定。

**预期结果**：能用自己的话讲清「为何 `SyncMLCEngine` 可以临时换回调而 `MLCEngine` 不行」——因为前者单线程无竞争，后者多线程回调被共享。

#### 4.3.5 小练习与答案

**练习 1**：`SyncMLCEngine` 的 `metrics()`（[sync_engine.py:L361-L363](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/sync_engine.py#L361-L363)）直接 `json.loads(self._ffi["json_metrics"]())`，而 `MLCEngine.metrics()` 却要发一条「假请求」走 chat completion 通道（4.2.5）。为什么实现方式不同？
**答案**：`SyncMLCEngine` 包的是裸 `Engine`，可以直接同步调用 `json_metrics` 取值（调用线程就是引擎线程，没有竞争）。`MLCEngine`/`AsyncMLCEngine` 包的是 `ThreadedEngine`，引擎状态被后台线程持有、不能从前台线程安全地直接读；于是借用「发请求 + 在 usage chunk 里捎带」的既有线程安全通道来查询。

**练习 2**：既然 `SyncMLCEngine` 不提供 OpenAI API 也不并发，项目为什么还要保留它？
**答案**：因为它「简单」。在单线程、手动 step 的模型下，调试引擎行为（断点、单步、检查每一步的 delta）非常直观，不必关心后台线程与队列时序；它也是引擎单测/集成测试的理想载体。生产服务才用带 ThreadedEngine 的 `AsyncMLCEngine`。

---

### 4.4 ServerContext：多模型注册中心与生命周期管理

#### 4.4.1 概念说明

REST 服务（`mlc_llm serve`）往往不止跑一个模型——可能同时托管一个 chat 模型和一个 embedding 模型，甚至多个 chat 模型（微服务路由，见 [u12-l2](./u12-l2-microserving-router.md)）。`ServerContext` 就是这个「进程级」的多模型注册中心：它把「模型名 → 引擎」的映射集中保管，让所有 HTTP 端点（`/v1/chat/completions`、`/v1/embeddings`、`/v1/models` 等）通过同一个全局入口取到正确的引擎。

它有两个职责叠在一起：

1. **全局单例**：用类变量 `server_context` 持有「当前唯一的 ServerContext」，端点用静态方法 `current()` 取它——这样 FastAPI 的路由函数不必经参数注入就能拿到上下文。
2. **生命周期管理**：实现 `__enter__/__exit__`，保证服务退出时所有引擎都被 `terminate()`（停后台线程、释放显存）。

#### 4.4.2 核心流程：单例 + 注册表 + 上下文管理器

`ServerContext` 的全部状态是两个字典与一个 api_key（[server_context.py:L19-L22](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/server/server_context.py#L19-L22)）：

```python
def __init__(self) -> None:
    self._models: Dict[str, AsyncMLCEngine] = {}            # chat/completion 引擎
    self._embedding_engines: Dict[str, AsyncEmbeddingEngine] = {}
    self.api_key: Optional[str] = None
```

单例与生命周期的核心是这三个方法：

[server_context.py:L24-L42](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/server/server_context.py#L24-L42)：

```python
def __enter__(self):
    if ServerContext.server_context is not None:
        raise RuntimeError("Server context already exists.")  # 全局只能有一个
    ServerContext.server_context = self                        # 登记为单例
    return self

def __exit__(self, exc_type, exc_value, traceback):
    for model_engine in self._models.values():
        model_engine.terminate()                # 逐个停止 chat 引擎
    for emb_engine in self._embedding_engines.values():
        emb_engine.terminate()                  # 逐个停止 embedding 引擎
    self._models.clear()
    self._embedding_engines.clear()
    ServerContext.server_context = None         # 注销单例

@staticmethod
def current():
    return ServerContext.server_context         # 端点从这里取上下文
```

关键设计点：

- **`__enter__` 的防重入**：进程里同时只能有一个 `ServerContext`。第二次 `with ServerContext()` 会直接报错。这避免了「两个服务上下文争抢同一个全局指针」。
- **`__exit__` 的对称清理**：进入时登记了几个引擎，退出时就 `terminate()` 几个。`AsyncMLCEngine.terminate()`（继承自 `MLCEngineBase`，[engine_base.py:L663-L674](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/engine_base.py#L663-L674)）会调 `exit_background_loop` 并 `join` 两个后台线程，确保显存与线程都被正确回收。
- **`current()` 是静态方法**：任何端点函数体内一行 `ServerContext.current()` 即可拿到上下文，无需 FastAPI 依赖注入（虽然 API key 用了 `Depends`，见 u11-l2）。

#### 4.4.3 源码精读：注册、查询与「单引擎特例」

注册带去重保护（[server_context.py:L44-L48](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/server/server_context.py#L44-L48)）：

```python
def add_model(self, hosted_model: str, engine: AsyncMLCEngine) -> None:
    if hosted_model in self._models:
        raise RuntimeError(f"Model {hosted_model} already running.")
    self._models[hosted_model] = engine
```

`add_embedding_engine` 同构（[server_context.py:L61-L65](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/server/server_context.py#L61-L65)）。同名重复注册直接报错，避免悄悄覆盖一个正在服务的引擎。

查询函数 `get_engine` 有一个非常体贴的「单引擎特例」（[server_context.py:L50-L55](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/server/server_context.py#L50-L55)）：

```python
def get_engine(self, model: Optional[str]) -> Optional[AsyncMLCEngine]:
    if len(self._models) == 1:
        return next(iter(self._models.values()))   # 只托管一个时，忽略请求里的 model 字段
    return self._models.get(model, None)
```

为什么需要它？OpenAI 协议要求请求里带 `model` 字段，但本地测试时用户常常只起一个模型、且懒得写对模型名。当只托管一个引擎时，无论请求里 `model` 写什么（甚至写错），都返回那唯一的引擎；只有多模型时才严格按名查表，查不到返回 `None`（端点据此返回 400「model not served」，见 [openai_entrypoints.py:L145-L150](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/entrypoints/openai_entrypoints.py#L145-L150)）。`get_embedding_engine` 同理。

`get_model_list`（[server_context.py:L57-L59](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/server/server_context.py#L57-L59)）把 chat 引擎与 embedding 引擎的键拼在一起，供 `/v1/models` 端点列出所有可服务模型。

#### 4.4.4 源码精读：serve() 如何把引擎装进上下文

[serve.py:L103-L107](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/serve.py#L103-L107) 是把「引擎」与「上下文」装配起来的关键几行：

```python
with ServerContext() as server_context:
    server_context.add_model(model, async_engine)
    if emb_engine is not None:
        server_context.add_embedding_engine(embedding_model, emb_engine)
    server_context.api_key = api_key
    # ... 组装 FastAPI app、注册路由、uvicorn.run(...) 阻塞服务 ...
```

注意 `async_engine` 与 `emb_engine` 都是在 `with` **之外**先行构造好的（见 [serve.py:L96-L101](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/serve.py#L96-L101) 的 `AsyncEmbeddingEngine(...)`）。`with ServerContext()` 只负责「登记 + 服务期间持有 + 退出时统一 terminate」。整个 `uvicorn.run` 在 `with` 块内阻塞运行；服务退出（Ctrl-C 或异常）后，控制流走出 `with`，`__exit__` 自动 terminate 所有引擎——这就是「服务启停时管理引擎生命周期」的完整闭环。

> 把这条链与 4.1/4.2 串起来：用户给的参数经 `_check_engine_config` 校验 → `AsyncMLCEngine` 构造（内部 `reload` + `get_complete_engine_config`）→ `add_model` 登记进 `ServerContext` → 端点经 `get_engine` 取用 → 服务退出时 `__exit__` 统一 `terminate`。`EngineConfig` 是贯穿始终的信封，`ServerContext` 是持有引擎实例的容器。

#### 4.4.5 代码实践

**实践目标**：亲手复现 `ServerContext` 的生命周期与「单引擎特例」，并验证 `_check_engine_config` 在配置覆盖链里的位置。

**操作步骤**（源码阅读 + 轻量复现，**示例代码**）：

1. **生命周期复现**（用假引擎，不依赖真实模型）：

   ```python
   from mlc_llm.serve.server.server_context import ServerContext

   class FakeEngine:
       def __init__(self, name): self.name = name
       def terminate(self): print(f"terminate {self.name}")

   with ServerContext() as ctx:
       ctx.add_model("m1", FakeEngine("m1"))
       assert ServerContext.current() is ctx          # 单例生效
       assert ctx.get_engine("anything") is ctx._models["m1"]  # 单引擎特例：无视请求名
   # 走出 with 后自动打印 terminate m1
   print("退出后 current():", ServerContext.current())  # 预期 None
   ```

2. **对照阅读**：打开 [server_context.py:L30-L37](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/server/server_context.py#L30-L37)，确认 `__exit__` 遍历 `_models` 与 `_embedding_engines` 调 `terminate()`，然后清空字典、把单例置 `None`。

3. **配置覆盖链追踪**：在 [engine_base.py:L584-L586](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/engine_base.py#L584-L586) 处看到 `_check_engine_config` 是构造函数第一步；再到 [engine_base.py:L648-L653](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/engine_base.py#L648-L653) 处看到构造参数 `model/model_lib/mode` 如何**覆盖**到 `engine_config` 同名字段，然后 `reload` 下发、`get_complete_engine_config` 回读。

**需要观察的现象**：

- 步骤 1 中，`with` 块内 `current()` 返回该上下文；`get_engine("anything")` 在单引擎时返回那唯一引擎；走出 `with` 时 `terminate m1` 被打印（即 `__exit__` 生效）；之后 `current()` 为 `None`。
- 步骤 3 中，`engine_config.mode` 在 `reload` 前**被构造参数 `mode` 覆盖**——这就是「配置覆盖如何生效」：构造参数是权威，`engine_config` 里同名字段要么留空（`None`）服从构造参数，要么必须与之一致（否则 4.2 的 `_check_engine_config` 早在第一步就拦截）。

**预期结果**：步骤 1 输出 `terminate m1` 与 `退出后 current(): None`；能口头复述「校验在前、覆盖在中、回读完整配置在后」的三段式。**待本地验证**：需 `import mlc_llm` 成功的环境。

#### 4.4.6 小练习与答案

**练习 1**：如果连续写两次 `with ServerContext() as ctx:`（嵌套或前后），会发生什么？
**答案**：第二次会在 `__enter__` 抛 `RuntimeError("Server context already exists.")`。因为第一次的 `__enter__` 已把类变量 `server_context` 指向自己且未退出，第二次检测到非 `None` 即拒绝。这保证全局唯一。

**练习 2**：`get_engine` 在多模型托管时，若请求的 `model` 名不在表里，返回什么？端点如何处理？
**答案**：返回 `None`。端点（如 [openai_entrypoints.py:L145-L150](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/entrypoints/openai_entrypoints.py#L145-L150)）据此返回 HTTP 400，提示 `The requested model "..." is not served.`。

**练习 3**：为什么 `__exit__` 里要 `self._models.clear()`？只 `terminate()` 不够吗？
**答案**：`terminate()` 只是停引擎，字典里的引用仍在。`clear()` 既是为了释放对引擎对象的引用（让 Python GC 回收），也是为了让 `__exit__` 后的 `ServerContext` 实例处于干净的「空」状态，避免误用已终止的引擎。同时把类变量 `server_context = None`，允许后续重新创建上下文。

## 5. 综合实践

把本讲三个模块串成一个端到端的「配置 → 引擎 → 上下文」追踪任务。

**任务**：阅读源码，画出从「用户调用 `mlc_llm serve --model M --model-lib L --mode server`」到「服务退出、显存释放」的完整生命周期时序图，至少包含以下节点，并标注每步对应的源码行：

1. `serve()` 解析参数，构造 `EngineConfig`（传入 `mode="server"` 等）。
2. 构造 `AsyncMLCEngine`：`MLCEngineBase.__init__` 先调 `_check_engine_config`（4.2 关卡）。
3. `detect_device` 解析设备；`_process_model_args` 解析模型/JIT 编译（若需）。
4. 创建 `ThreadedEngine`，`init_threaded_engine` 注册回调，启动两个后台线程。
5. `engine_config.model/model_lib/mode` 被构造参数覆盖，`reload(asjson())` 下发，`get_complete_engine_config` 回读完整配置。
6. `with ServerContext() as ctx: ctx.add_model(...)` 登记引擎，`uvicorn.run` 阻塞服务。
7. 请求到达端点 → `ServerContext.current().get_engine(model)` 取引擎 → 经 u11-l1 的 JSON FFI 桥进入 C++。
8. 服务退出 → `__exit__` → 逐个 `terminate()` → `exit_background_loop` + `join` 两线程 → 显存回收。

**要求**：

- 时序图用文字/伪代码画出（不必用绘图工具），每步标注 `[文件:行号]`。
- 特别标出 `_check_engine_config` 在哪一步、`reload`/`get_complete_engine_config` 这对「下行/上行」发生在哪一步、`__exit__` 的清理顺序。
- 写一段 3–5 句的说明，解释「为什么 `_check_engine_config` 必须在 `reload` 之前，而 `get_complete_engine_config` 必须在 `reload` 之后」。

**参考答案要点**：`_check_engine_config` 在前——因为冲突的配置一旦下发到 C++ 会引发难以定位的错误，且校验成本极低，应前置到任何重活（下载/JIT/建池）之前；`get_complete_engine_config` 在后——因为 C++ 的容量推断（受 `gpu_memory_utilization` 等约束反推 `max_total_sequence_length`）只有在 `reload` 把用户意图送达之后才能完成，Python 必须等它算完才能读到「真实可跑」的参数。

## 6. 本讲小结

- `EngineConfig` 是纯数据信封，分「模型定位 / 并发上下文 / 显存分页 / 高级特性 / 并行」五组字段；三档 `mode`（`local`/`interactive`/`server`）只是「未显式指定字段的默认套餐」，显式值永远优先；跨 FFI 靠 `asjson()` / `from_json()` 往返。
- `_check_engine_config` 是进 C++ 前的一致性闸门，四道关分别查 `model`、`model_lib`、`mode` 的构造参数与 config 字段是否冲突，以及 `kv_cache_page_size` 是否为 16；规则是「`None` 表示不指定、由对方决定」。
- 配置覆盖与回读是「两段式」：构造参数在 `reload` 前覆盖 `engine_config` 同名字段并下发，`get_complete_engine_config` 在 `reload` 后读回 C++ 推断完整的配置；`detect_device("auto")` 按 cuda→rocm→metal→vulkan→opencl→cpu 扫描。
- 三种引擎封装的分水岭是「谁驱动 `Step()`」：`AsyncMLCEngine`/`MLCEngine` 包 `ThreadedEngine`、后台线程自动驱动；`SyncMLCEngine` 包裸 `Engine`、调用者手动 `while: step()`，无 OpenAI API，仅供测试调试。
- `ServerContext` 是进程级单例 + 多模型注册中心：`__enter__` 登记单例（防重入）、`__exit__` 统一 `terminate()` 所有 chat/embedding 引擎并清空；`current()` 供端点取上下文，`get_engine` 在单引擎托管时贴心地忽略请求里的 model 名。
- `mlc_llm serve` 的启动编排把这三块串起来：构造 `AsyncMLCEngine`（含校验/覆盖/回读）→ `with ServerContext(): add_model(...)` → FastAPI + uvicorn 阻塞 → 退出时 `__exit__` 兜底回收。

## 7. 下一步学习建议

- 想看清「后台循环」在 C++ 侧到底怎么跑，进入 [u9-l1 Engine、ThreadedEngine 与 EngineState](./u9-l1-engine-threaded-state.md) 与 [u9-l2 事件-动作循环](./u9-l2-action-loop.md)，对照本讲的 `run_background_loop`/`run_background_stream_back_loop` 看 C++ 实现。
- 想理解 `EngineConfig` 里 `max_total_sequence_length` 等 `None` 字段是如何被 C++「推断填回」的，读 [u9-l4 模型运行时与 FunctionTable](./u9-l4-model-runtime-functiontable.md) 里的 `InferrableEngineConfig::InferForKVCache`。
- 想了解「多模型托管」在微服务/分离式推理场景下的进阶用法，继续 [u12-l2 微服务与路由器](./u12-l2-microserving-router.md) 与 [u12-l3 分离式推理](./u12-l3-disaggregation.md)。
- 若想动手扩展，可尝试：用一个自定义的 `EngineConfig`（如调高 `max_num_sequence` 并设 `mode="server"`）启动 serve，再用 `/v1/models` 与 `EngineMetrics`（4.2.5）观察并发能力与吞吐的变化。
