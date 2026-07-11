# Pipeline 如何选择并实例化后端

## 1. 本讲目标

本讲要回答一个关键问题:**当我们写下 `pipeline('Qwen/Qwen2.5-7B-Instruct')` 时,lmdeploy 内部到底经历了哪些步骤,才把一个模型路径变成一个可以跑推理的对象?**

读完本讲你应当能够:

1. 顺着 `Pipeline.__init__` 的代码,讲清楚「定位模型 → 选后端 → 选 Pipeline 类 → 构建引擎 → 启动循环」这五步。
2. 画出 **`Pipeline → AsyncEngine → Engine → create_instance`** 的完整对象组合链,并说清每一层各自负责什么。
3. 看懂 `_infer` / `_request_generator` 这一对方法如何用「一个后台事件循环线程 + 一个 `queue.Queue`」把**异步引擎**包装成**同步接口**。
4. 明确 Pipeline 与 Engine 的职责边界:Pipeline 是面向用户的同步外观( façade ),Engine 才是真正干活的人。

> 承接前讲:`u1-l4` 已经让你跑通了 `pipeline()`,并给出了「同步外观 + 异步内核 + `_EventLoopThread`」的宏观结论;`u2-l5` 已经讲透了 `archs.py` 里 `autoget_backend` / `autoget_backend_config` / `get_task` 的**判定逻辑**。本讲**不再重复判定规则**,而是聚焦于:`Pipeline.__init__` 如何把这些判定结果**组装成可运行的对象**、以及 `_infer` 如何**驱动**这个对象。如果你对「为什么 TurboMind 优先于 PyTorch」还有疑问,请先回到 `u2-l5`。

## 2. 前置知识

本讲假设你已经理解下面几个概念(均在前几讲建立):

- **两条后端**:TurboMind(C++ 高性能后端)与 PyTorch(纯 Python 后端),二者并存互补。详见 `u1-l1`。
- **`PytorchEngineConfig` / `TurbomindEngineConfig`**:描述「引擎长什么样」的配置类,定义在 `lmdeploy/messages.py`,在创建 pipeline 时传入一次。详见 `u2-l3`。
- **arch 名字**:HuggingFace `config.json` 里 `architectures[0]` 取到的模型类名,是贯穿全栈的「模型身份证」。详见 `u2-l5`。
- **同步与异步**:`async def` 定义的协程不能直接 `for` 遍历,必须放进一个事件循环(event loop)里跑;Python 的 `queue.Queue` 是线程安全的跨线程传值工具。本讲会用到这两个概念。

补充两个本讲新出现、但很轻量的术语:

- **外观模式(façade)**:给一个复杂的子系统(异步引擎、线程、队列)提供一个简化的统一入口。`Pipeline` 就是这么一个外观。
- **句柄(handle)**:这里指一次推理流(stream)所持有的、与底层引擎交互的轻量代理对象(`EngineInstance` / `TurboMindInstance`)。

## 3. 本讲源码地图

本讲涉及的关键文件及其职责:

| 文件 | 作用 |
| --- | --- |
| `lmdeploy/pipeline.py` | 用户面入口。`Pipeline` 类负责选后端、装引擎、把异步引擎包成同步接口;`_EventLoopThread` 提供后台事件循环。 |
| `lmdeploy/archs.py` | 路由器。提供 `autoget_backend_config`(返回后端名 + 配置)与 `get_task`(返回 Pipeline 类)。判定细节见 `u2-l5`。 |
| `lmdeploy/serve/core/async_engine.py` | 异步推理引擎。`AsyncEngine` 持有真正的 `self.engine`,并在此处做「turbomind / pytorch」的第二层分支。 |
| `lmdeploy/serve/managers/session_manager.py` | 句柄池。在引擎建好后批量调用 `engine.create_instance()` 生成推理句柄。 |
| `lmdeploy/pytorch/engine/engine.py` | PyTorch 后端引擎主类,定义 `create_instance`。 |
| `lmdeploy/turbomind/turbomind.py` | TurboMind 后端的 Python 包装,定义 `create_instance`。 |

> 这 6 个文件构成「从一次 `pipeline()` 调用到产生可推理句柄」的完整纵向切面。本讲只读其中与「选择 / 实例化 / 驱动」相关的段落,其余细节留给后续单元。

## 4. 核心概念与源码讲解

### 4.1 Pipeline.__init__:后端选择的三个步骤

#### 4.1.1 概念说明

`Pipeline.__init__` 是整条链的起点。它要做的事情可以归纳为一句话:**把一个「模型路径」翻译成一棵「已经启动、随时可以接请求」的对象树**。

这棵树长这样:

```
Pipeline                         # 同步外观(用户持有)
└── self.async_engine            # AsyncEngine 或 VLAsyncEngine(异步内核)
      ├── self.engine            # 真正的后端:pytorch Engine 或 TurboMind
      │     └── create_instance() → EngineInstance / TurboMindInstance(句柄)
      ├── self.tokenizer
      ├── self.chat_template
      └── self.session_mgr       # 句柄池 + 会话管理
└── self.internal_thread         # _EventLoopThread(后台事件循环)
```

理解这棵树,就理解了「Pipeline 与 Engine 的职责边界」:**Pipeline 不碰张量、不碰 CUDA**,它只负责调度请求与翻译返回值;**Engine 才是真正跑 forward 的人**。

#### 4.1.2 核心流程

`__init__` 的执行顺序是严格分层的五步:

```
1. 准备阶段
   ├─ 设置日志级别 (TM_LOG_LEVEL)
   └─ 若 model_path 在本地不存在 → get_model() 从 HuggingFace 下载

2. 选后端        autoget_backend_config() → (backend, backend_config)
3. 选 Pipeline 类  get_task()              → (task, pipeline_class)

4. 实例化引擎
   ├─ self.async_engine = pipeline_class(...)
   ├─ self.internal_thread = _EventLoopThread(daemon=True)
   └─ self.async_engine.start_loop(self.internal_thread.loop, use_async_api=False)

5. 暴露便捷引用
   └─ self.backend_config = self.async_engine.backend_config
```

其中第 2、3 步是「选择」,第 4 步是「实例化」。注意一个常被忽略的细节:**`backend_config` 经历了一次「回填」**——你传进去的配置可能被 `autoget_backend_config` 改写(例如把 PyTorch 的 `block_size` 搬到 TurboMind 的 `cache_block_seq_len`),也可能在 `AsyncEngine.__init__` 里被进一步补全(例如算出真实的 `session_len`),所以最后 `self.backend_config` 取的是 `self.async_engine.backend_config`,**而不是你最初传入的那一份**。

#### 4.1.3 源码精读

先看 `__init__` 的签名,它就是用户调 `pipeline(...)` 时全部能用的参数:

```python
def __init__(self,
             model_path: str,
             backend_config: TurbomindEngineConfig | PytorchEngineConfig | None = None,
             chat_template_config: ChatTemplateConfig | None = None,
             log_level: str = 'WARNING',
             max_log_len: int | None = None,
             trust_remote_code: bool = False,
             speculative_config: SpeculativeConfig | None = None,
             **kwargs):
```

> 见 [lmdeploy/pipeline.py:35-43](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pipeline.py#L35-L43)。这一段定义了 `backend_config`、`chat_template_config`、`speculative_config` 三个「配置包」,它们都会原样向下传递。

**第 1 步:下载模型。** 只有当本地路径不存在时才触发,且会从 `backend_config` 里取 `download_dir` / `revision`:

```python
if not os.path.exists(model_path):
    download_dir = backend_config.download_dir if backend_config else None
    revision = backend_config.revision if backend_config else None
    model_path = get_model(model_path, download_dir, revision)
```

> 见 [lmdeploy/pipeline.py:60-64](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pipeline.py#L60-L64)。`get_model` 定义在 [lmdeploy/utils.py:255](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/utils.py#L255),内部走的是 HuggingFace 的下载逻辑。投机解码用的 draft 模型用同样方式下载([pipeline.py:67-69](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pipeline.py#L67-L69))。

**第 2、3、4 步:选后端、选类、装引擎。** 这是本讲最核心的 15 行:

```python
# Create inference engine
backend, backend_config = autoget_backend_config(model_path, backend_config,
                                                 trust_remote_code=trust_remote_code)
_, pipeline_class = get_task(backend,
                             model_path,
                             trust_remote_code=trust_remote_code,
                             backend_config=backend_config)
self.async_engine = pipeline_class(model_path,
                                   backend=backend,
                                   backend_config=backend_config,
                                   chat_template_config=chat_template_config,
                                   max_log_len=max_log_len,
                                   trust_remote_code=trust_remote_code,
                                   speculative_config=speculative_config,
                                   **kwargs)
self.internal_thread = _EventLoopThread(daemon=True)
self.limiter: asyncio.Semaphore = None
self.session_mgr = self.async_engine.session_mgr
self.backend_config = self.async_engine.backend_config
self.async_engine.start_loop(self.internal_thread.loop, use_async_api=False)
```

> 见 [lmdeploy/pipeline.py:71-90](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pipeline.py#L71-L90)。注意三个要点:

1. `autoget_backend_config` **同时返回** `backend` 字符串和(可能被改写过的)`backend_config`,后者**覆盖**了入参同名变量——这就是「配置回填」的第一站。
2. `get_task` 的第一个返回值 `_` 是任务类型(`'llm'` / `'vlm'`),`Pipeline` 不用它,只取 `pipeline_class`(`AsyncEngine` 或 `VLAsyncEngine`)。
3. `start_loop(..., use_async_api=False)` 表示这个 Pipeline **绑定到同步接口**(`__call__` / `infer` / `stream_infer`)。同一个引擎实例在生命周期内**只能在「同步」与「异步」二选一**,这一点写在 `start_loop` 的文档里(见 4.1 末尾的引用)。

**第 4 步的「启动循环」细节** 委托给了 `AsyncEngine.start_loop`:

```python
def start_loop(self, loop, use_async_api=False):
    self.session_mgr.attach_event_loop(loop)
    if hasattr(self.engine, 'start_loop'):
        if use_async_api:
            return self.engine.start_loop()
        else:
            fut = concurrent.futures.Future()
            def _start_loop(fut):
                res = self.engine.start_loop()
                fut.set_result(res)
            loop.call_soon_threadsafe(_start_loop, fut)
            return fut.result()
    else:
        return True
```

> 见 [lmdeploy/serve/core/async_engine.py:794-818](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/core/async_engine.py#L794-L818)。这段做了两件事:把句柄池的事件循环绑定到 `Pipeline` 的后台线程循环;然后用 `loop.call_soon_threadsafe` 把 `engine.start_loop()` **安全地投递到那个后台循环里执行**,并用 `Future` 等它完成。TurboMind 后端没有 `start_loop` 方法,走 `else` 直接返回 `True`。

#### 4.1.4 代码实践

**实践目标**:在不真正加载权重的前提下,定位并理解 `__init__` 的执行顺序。

**操作步骤**:

1. 打开 [lmdeploy/pipeline.py:35](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pipeline.py#L35),在 `__init__` 的五步处各加一行**伪注释**(只读阅读,不修改源码——可以用纸笔或本地副本)。
2. 用 Python 反射查看 `Pipeline.__init__` 接受哪些参数:

```python
# 示例代码:仅查看签名,不会触发模型下载
import inspect
from lmdeploy.pipeline import Pipeline
for name, p in inspect.signature(Pipeline.__init__).parameters.items():
    print(f'{name:25s} default={p.default}')
```

**需要观察的现象**:打印出的参数列表应当与 4.1.3 中引用的源码签名完全一致;注意 `backend_config`、`chat_template_config`、`speculative_config` 三个参数默认都是 `None`。

**预期结果**:你能在不连 GPU、不下载模型的情况下,确认 `pipeline(...)` 到底接受哪些旋钮。这一步**不需要本地验证 GPU**,纯 Python 反射即可运行。

#### 4.1.5 小练习与答案

**练习 1**:为什么 `self.backend_config = self.async_engine.backend_config`,而不是直接 `self.backend_config = backend_config`(函数局部变量)?

> **答案**:因为局部变量 `backend_config` 只是「选择阶段」的产物,可能仍未补全(例如 `session_len` 此时可能还是 `None`)。`AsyncEngine.__init__` 会进一步算出真实 `session_len` 并回写到它自己的 `backend_config`(见 [async_engine.py:128-130](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/core/async_engine.py#L128-L130))。取引擎那一份才能拿到最终生效的配置。

**练习 2**:`get_task` 的第一个返回值被赋给了 `_`,这代表什么?

> **答案**:那是任务类型字符串 `'llm'`(纯文本)或 `'vlm'`(多模态)。`Pipeline` 本身不关心这个标签——因为它已经通过 `pipeline_class`(`AsyncEngine` / `VLAsyncEngine`)拿到了对应的类,任务类型只是同一次判定的「副产物」,所以用 `_` 丢弃。

---

### 4.2 后端分支:autoget_backend_config 与 AsyncEngine 的两层判定

#### 4.2.1 概念说明

「选后端」这件事在代码里其实发生在**两个层次**,初学者很容易把它们混为一谈:

1. **第一层(archs.py)**:决定 `backend` 字符串是 `'turbomind'` 还是 `'pytorch'`,并产出对应的配置对象。这是 `u2-l5` 的主角。
2. **第二层(async_engine.py)**:拿到 `backend` 字符串后,在 `AsyncEngine.__init__` 里用它做 `if/elif` 分支,真正 `import` 并构造出 `self.engine`。这一层是本讲的增量。

之所以要分两层,是因为第一层只看「模型 + 是否安装了 TurboMind」,无法决定「具体怎么 import 引擎类、传哪些参数」——那需要等 `AsyncEngine` 这个更内层的对象来收口。换句话说:**第一层回答「走哪条路」,第二层回答「在路上把车造出来」**。

此外,第一层还有一个容易踩坑的行为:**跨后端的字段搬迁**。当用户传的配置类型与最终选中的后端不一致时(比如传了 `TurbomindEngineConfig` 但最终走了 PyTorch),lmdeploy 会把同义字段搬过去,而不是直接丢弃。

#### 4.2.2 核心流程

```
autoget_backend_config(model_path, backend_config)        # 第一层
  ├─ 若 backend_config 是 PytorchEngineConfig → 直接短路返回 ('pytorch', backend_config)
  ├─ backend = autoget_backend(model_path)                 # TurboMind 优先,否则 'pytorch'
  ├─ config = PytorchEngineConfig() / TurbomindEngineConfig()  # 按后端建空配置
  ├─ 若用户传了 backend_config:
  │     ├─ 类型一致 → 整体替换
  │     └─ 类型不一致 → 逐字段搬运 + block_size ↔ cache_block_seq_len 改名
  └─ return (backend, config)

AsyncEngine.__init__(...)                                  # 第二层
  └─ if   backend == 'turbomind': self.engine = self._build_turbomind(...)
     elif backend == 'pytorch'  : self.engine = self._build_pytorch(...)
     else: raise ValueError
```

#### 4.2.3 源码精读

**第一层:`autoget_backend_config` 的短路逻辑。** 注意第一行——**用户只要显式传 `PytorchEngineConfig`,就直接强制走 PyTorch**,连 `autoget_backend` 都不调用:

```python
if isinstance(backend_config, PytorchEngineConfig):
    return 'pytorch', backend_config

backend = autoget_backend(model_path, trust_remote_code=trust_remote_code)
config = PytorchEngineConfig() if backend == 'pytorch' else TurbomindEngineConfig()
```

> 见 [lmdeploy/archs.py:74-78](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/archs.py#L74-L78)。这是**强制 PyTorch 的唯一干净入口**:不需要改环境变量,只要把 `backend_config` 换成 `PytorchEngineConfig()` 即可。注意没有对 `TurbomindEngineConfig` 的对称短路——传 `TurbomindEngineConfig` 并不会强制走 TurboMind,若该模型不被 TurboMind 支持,仍会回退到 PyTorch 并触发字段搬运。

**第一层:跨后端字段搬运。** 当用户传的配置类型与最终后端不一致时,逐字段拷贝,并对两个「同名不同义」的字段做改名:

```python
if backend_config is not None:
    if type(backend_config) is type(config):
        config = backend_config
    else:
        data = asdict(backend_config)
        for k, v in data.items():
            if v and hasattr(config, k):
                setattr(config, k, v)
        # map attributes with different names
        if type(backend_config) is TurbomindEngineConfig:
            config.block_size = backend_config.cache_block_seq_len
        else:
            config.cache_block_seq_len = config.block_size
```

> 见 [lmdeploy/archs.py:79-91](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/archs.py#L79-L91)。注意搬运条件是 `if v and hasattr(config, k)`:只搬**非空且目标类拥有**的字段。`block_size`(PyTorch 叫法)与 `cache_block_seq_len`(TurboMind 叫法)指的是同一个东西——一个 KV cache 块里能装多少 token,只是两个后端历史上用了不同名字。

> ⚠️ **勘误提醒**:源码第 91 行 `config.cache_block_seq_len = config.block_size` 读的是 `config.block_size`,而非直觉上的 `backend_config.block_size`。在「用户传 `PytorchEngineConfig` 但回退到 TurboMind」这种边界场景下,这个赋值语义值得你在本地用断点确认(见 4.2.4 实践)。本讲对此存疑、不妄下结论。

**第二层:`AsyncEngine` 里的真正分支。** 拿到 `backend` 字符串后,这里才 `import` 真正的引擎类:

```python
# build backend engine
if backend == 'turbomind':
    self.engine = self._build_turbomind(model_path=model_path,
                                        backend_config=backend_config,
                                        trust_remote_code=trust_remote_code,
                                        **kwargs)
elif backend == 'pytorch':
    self.engine = self._build_pytorch(model_path=model_path,
                                      backend_config=backend_config,
                                      trust_remote_code=trust_remote_code,
                                      speculative_config=speculative_config,
                                      **kwargs)
else:
    raise ValueError(f'unsupported backend {backend}')
```

> 见 [lmdeploy/serve/core/async_engine.py:134-146](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/core/async_engine.py#L134-L146)。两个 `_build_*` 都是**延迟 import**(`from lmdeploy.pytorch.engine import Engine`、`from lmdeploy import turbomind as tm`),这样即使用户只装了其中一个后端,也不会因为另一个的缺失而在 import 阶段就报错。注意投机解码配置只传给 PyTorch 分支——TurboMind 不支持投机解码,这一点在上方第 131-132 行有告警。

**两个 builder 各自指向哪里**:

```python
def _build_pytorch(self, ...):
    from lmdeploy.pytorch.engine import Engine
    return Engine.from_pretrained(model_path,
                                  engine_config=backend_config,
                                  speculative_config=speculative_config,
                                  trust_remote_code=trust_remote_code,
                                  **kwargs)
```

> 见 [lmdeploy/serve/core/async_engine.py:197-209](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/core/async_engine.py#L197-L209)。PyTorch 后端走 `Engine.from_pretrained`(详见 `u3-l5` 权重加载);TurboMind 走 `tm.TurboMind.from_pretrained`(详见 `u6-l2`)。无论哪条分支,返回的对象都被统一赋给 `self.engine`,从此 `AsyncEngine` 只跟这个统一接口打交道。

#### 4.2.4 代码实践

**实践目标**:用一个最小的本地 HF 模型目录,亲眼看到「两层判定」各返回了什么,并验证强制走 PyTorch 的短路。

**操作步骤**:

1. 准备一个本地模型目录(任意 HF 纯文本模型即可,例如已下载的 Qwen2.5 小尺寸)。
2. 跑下面这段「探测脚本」(示例代码):

```python
# 示例代码:探测 autoget_backend_config 的返回
from lmdeploy.archs import autoget_backend_config, get_task
from lmdeploy.messages import PytorchEngineConfig, TurbomindEngineConfig

MODEL = '/path/to/local/hf/model'   # 换成你的本地路径

# 场景 A:不传配置,交给自动判定
backend, cfg = autoget_backend_config(MODEL, None)
print('A auto ->', backend, type(cfg).__name__)

# 场景 B:强制 PyTorch(短路)
backend2, cfg2 = autoget_backend_config(MODEL, PytorchEngineConfig())
print('B force pytorch ->', backend2, type(cfg2).__name__)

# 场景 C:看 get_task 选哪个 Pipeline 类
task, pipeline_cls = get_task(backend, MODEL)
print('C task ->', task, pipeline_cls.__name__)
```

3. 在 [lmdeploy/archs.py:74](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/archs.py#L74) 处下断点(或在场景 B 前后打印),确认场景 B **没有**进入 `autoget_backend` 分支。

**需要观察的现象**:

- 场景 A 的 `backend` 取决于该模型是否被 TurboMind 支持(若不支持,日志里会出现 `Fallback to pytorch engine because ...`)。
- 场景 B 无论模型是什么,`backend2` 必为 `'pytorch'`。
- 场景 C 的 `pipeline_cls` 对纯文本模型是 `AsyncEngine`,对多模态模型是 `VLAsyncEngine`。

**预期结果**:**待本地验证**(具体输出依赖你选的模型与是否安装了 TurboMind 扩展)。若你安装时用了 `DISABLE_TURBOMIND=1`,场景 A 也必然回退到 `pytorch`。

**画出调用链**:结合本节与 4.1,完整的「选择 → 实例化」调用链如下,请在笔记里照抄一遍并标注每段对应的源码行号:

```
pipeline('model')
└─ Pipeline.__init__                                     # pipeline.py:35
   ├─ autoget_backend_config(model_path, backend_config) # pipeline.py:72  → archs.py:56
   │     └─ (短路) isinstance(PytorchEngineConfig)       #              archs.py:74
   │     └─ autoget_backend(model_path)                  #              archs.py:77 → archs.py:12
   ├─ get_task(backend, model_path, ...)                 # pipeline.py:74 → archs.py:125
   │     └─ check_vl_llm(...) → AsyncEngine/VLAsyncEngine#              archs.py:135
   ├─ self.async_engine = pipeline_class(...)            # pipeline.py:78
   │     └─ AsyncEngine.__init__                         # async_engine.py:108
   │           └─ _build_turbomind / _build_pytorch      # async_engine.py:134
   │                 └─ self.engine = Engine / TurboMind # async_engine.py:197
   │           └─ session_mgr.build_request_handle_pool(self.engine, max_batch_size)  # async_engine.py:164
   │                 └─ [engine.create_instance() for _ in range(size)]               # session_manager.py:174
   │                       └─ EngineInstance(self) / TurboMindInstance(self)          # engine.py:661 / turbomind.py:380
   └─ self.async_engine.start_loop(self.internal_thread.loop, use_async_api=False)     # pipeline.py:90 → async_engine.py:794
```

#### 4.2.5 小练习与答案

**练习 1**:用户传了 `TurbomindEngineConfig`,但模型不被 TurboMind 支持。最终 `self.engine` 是什么类型?配置对象经历了什么?

> **答案**:`self.engine` 是 PyTorch 的 `Engine`。配置对象先在 `autoget_backend_config` 里被「逐字段搬运」到一个新建的 `PytorchEngineConfig()` 上([archs.py:82-91](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/archs.py#L82-L91)),再在 `AsyncEngine.__init__` 里补全 `session_len`,最终赋给 `self.engine.engine_config`。用户传的 TurboMind 配置里**同义**的字段会被保留,不同义的被丢弃。

**练习 2**:为什么两个 `_build_*` 方法都写成延迟 import,而不是在文件顶部 import?

> **答案**:为了让「只装了 PyTorch 后端」和「只装了 TurboMind 后端」的两种安装都能正常 `import lmdeploy`。若在顶部就 `from lmdeploy.pytorch.engine import Engine`,那么用 `DISABLE_TURBOMIND` 只装 PyTorch 的用户(或反过来没装 PyTorch 依赖的环境)会在 import `async_engine` 时直接失败。延迟 import 把失败推迟到「真正要用某个后端」的那一刻。

**练习 3**:`AsyncEngine.__init__` 里有一行 `backend_config = backend_config or (...)`([async_engine.py:120-121](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/core/async_engine.py#L120-L121))。既然 `Pipeline` 已经保证传进来非空,这行为什么还有必要?

> **答案**:因为 `AsyncEngine` 是**公开类**,也可以脱离 `Pipeline` 被用户或 `serve` 层直接实例化(例如 `u8-l3` 的异步引擎封装)。这一行是 `AsyncEngine` 对「自己可能被独立调用」的防御性兜底:如果调用者既没传 `backend_config` 又没走 `Pipeline`,它就按 `backend` 字符串给一个默认空配置。

---

### 4.3 _infer:同步外观如何驱动异步引擎

#### 4.3.1 概念说明

后端选好了、引擎建好了,但还有一个「阻抗不匹配」问题没解决:**底层 `AsyncEngine.generate` 是 `async def`,产出的是异步生成器;而用户面的 `Pipeline.infer` 是普通同步函数,要用 `for` 遍历、要返回 `Response` 列表。** 两套世界如何对接?

答案是 `_infer` + `_request_generator` 这对方法,再配合 `_EventLoopThread` 提供的后台事件循环。它们共同构成了一个经典的「**跨线程队列桥接**」模式:

- **主线程**(用户调用 `infer` 的线程)负责把请求丢进队列、从队列取结果。
- **后台事件循环线程**(`_EventLoopThread`)负责跑异步引擎、把产出丢回队列。
- `queue.Queue` 是两边唯一的接触点,天然线程安全。

理解了这个模式,你就理解了 `u1-l4` 留下的那句「同步外观 + 异步内核」的具体实现。

#### 4.3.2 核心流程

```
用户调 Pipeline.infer(prompts)
  │
  ├─ _request_generator(prompts, ...)         # 把 prompts 展开成一串 dict 请求
  │     每条形如 {session_id, messages, gen_config, stream_response, ...}
  │
  └─ _infer(requests, multiplex=False)        # 同步驱动
        │
        │  ┌─── 主线程 ───────────────────────────────────────────┐
        ├─→│  run_coroutine_threadsafe(_infer(), loop)            │
        │  │  创建一个 Queue,return iter(que.get, None)           │  ← 用户从这里 for 取结果
        │  └──────────────────────────────────────────────────────┘
        │
        │  ┌─── 后台事件循环线程(loop)────────────────────────────┐
        │  │  async def _infer():                                 │
        │  │    for req in requests:                              │
        │  │      sem.acquire()           # 用 max_batch_size 限流│
        │  │      gen = async_engine.generate(**req)  # 异步生成器│
        │  │      create_task(_sync_resp(gen, que, idx, sem))     │
        │  │    await gather(*tasks)                              │
        │  └──────────────────────────────────────────────────────┘
        │
        │  ┌─── 每个 _sync_resp 任务 ──────────────────────────────┐
        └─→│  async for out in gen:   # 逐个 token 消费异步生成器  │
           │    que.put(out.to_response(idx))   # 转 Response 入队 │
           │  sem.release()                                         │
           └──────────────────────────────────────────────────────┘
```

`multiplex` 是一个关键开关:`infer` 走 `multiplex=False`(请求间结果**不交错**,按提交顺序成块返回);`stream_infer` / `chat` 走 `multiplex=True`(多个请求的结果**交错流式**返回)。两者的区别主要体现在「每个任务用哪个队列」和「哨兵怎么发」。

#### 4.3.3 源码精读

**先看 `_request_generator`:把用户的 prompt 变成引擎能吃的 dict。**

```python
def _request_generator(self, prompts, sessions=None, gen_config=None, **kwargs):
    is_single = self._is_single(prompts)
    prompts = [prompts] if is_single else prompts
    ...
    if gen_config is None:
        gen_configs = [GenerationConfig()] * len(prompts)
    elif isinstance(gen_config, list):
        gen_configs = gen_config
    else:
        gen_configs = [gen_config] * len(prompts)
    ...
    for prompt, gen_cfg, session in zip(prompts, gen_configs, sessions):
        yield dict(session_id=session, messages=prompt, gen_config=gen_cfg, **kwargs)
```

> 见 [lmdeploy/pipeline.py:323-358](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pipeline.py#L323-L358)(节选)。它是一个**同步生成器**(`yield`),做三件事:把单个 prompt 包成列表、给每条 prompt 配一个 `GenerationConfig`(默认空配置即用默认采样参数,见 `u2-l2`)、给每条配一个 `session`。最后 `yield` 出来的 dict 的键,正好对应 `AsyncEngine.generate` 的形参([async_engine.py:479-498](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/core/async_engine.py#L479-L498))。这就是「请求驱动」的契约:**dict 即请求**。

**再看 `_infer`:核心的跨线程桥接。**

```python
def _infer(self, requests, multiplex, pbar=None, loop=None):
    async def _sync_resp(g, que, idx, sem):
        async for out in g:
            que.put(out.to_response(idx))
        sem.release()
        if not multiplex:
            que.put(None)  # sentinel of inner generator
        if pbar:
            pbar.update(1)

    que = Queue()

    async def _infer():
        sem = self._get_limiter()
        tasks = []
        for idx, req in enumerate(requests):
            await sem.acquire()
            gen = self.async_engine.generate(**req)
            dst = que if multiplex else Queue()
            if not multiplex:
                que.put(iter(dst.get, None))
            task = asyncio.create_task(_sync_resp(gen, dst, idx, sem))
            tasks.append(task)
        if not multiplex:
            que.put(None)
        await asyncio.gather(*tasks)
        if multiplex:
            que.put(None)

    loop = loop or self.internal_thread.loop
    asyncio.run_coroutine_threadsafe(_infer(),
                                     loop).add_done_callback(lambda f: None if f.cancelled() else f.result())
    return iter(que.get, None)
```

> 见 [lmdeploy/pipeline.py:365-401](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pipeline.py#L365-L401)。逐行拆解几个最关键的点:

1. **`gen = self.async_engine.generate(**req)`**(第 383 行):这是**真正把请求交给引擎**的一行。`generate` 是 `async def`,返回异步生成器,**注意它不会被立刻消费**,而是交给一个后台任务 `_sync_resp` 去慢慢 `async for`。
2. **`asyncio.create_task(_sync_resp(gen, dst, idx, sem))`**(第 388 行):为每条请求起一个并发任务,从而实现「多条请求并发推理」——这正是持续批处理(continuous batching)在用户面的体现。
3. **`asyncio.run_coroutine_threadsafe(_infer(), loop)`**(第 398 行):把整个 `_infer()` 协程**从主线程投递到后台事件循环线程**执行。这是跨线程的唯一桥梁。`add_done_callback` 里的 `f.result()` 用来把协程里未捕获的异常重新抛到主线程,避免异常被静默吞掉。
4. **`return iter(que.get, None)`**(第 401 行):主线程立刻返回一个「从队列取值、遇到 `None` 停止」的迭代器。**这一行在协程还没跑完时就执行了**——主线程随后阻塞在 `que.get()` 上等结果,后台线程一边算一边 `que.put()`。这种「生产者-消费者」解耦,就是同步外观能驱动异步引擎的根本机制。

5. **`sem = self._get_limiter()`**(第 379 行):信号量初值是 `max_batch_size`([pipeline.py:360-363](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pipeline.py#L360-L363)),每提交一条请求 `acquire` 一次,每完成一条 `_sync_resp` 里 `release` 一次。它**把用户面的并发量限制在引擎配置的 `max_batch_size` 以内**,避免一次性灌入过多请求把引擎压垮。

**哨兵机制(`None`)的作用**:Python 的 `queue.Queue.get()` 是阻塞调用,需要一个特殊值告诉消费者「后面没有了,别再等」。`iter(que.get, None)` 的第二个参数 `None` 就是这个哨兵——取到 `None` 就停止迭代。代码里多处 `que.put(None)` 都是在不同层级发送「结束」信号。

**`_EventLoopThread`:后台事件循环的宿主。**

```python
class _EventLoopThread:
    def __init__(self, daemon=False):
        fut = concurrent.futures.Future()
        self.thread = Thread(target=partial(self._thread_entry, fut), daemon=daemon)
        self.thread.start()
        self.loop: asyncio.AbstractEventLoop = fut.result()
        ...
    def _thread_entry(self, fut):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        fut.set_result(loop)
        ...
        loop.run_forever()
```

> 见 [lmdeploy/pipeline.py:415-431](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pipeline.py#L415-L431)(节选)。它在一个子线程里 `new_event_loop()` + `run_forever()`,然后用一个 `Future` 把这个 loop 句柄安全交回主线程。Pipeline 默认 `daemon=True`,意味着主程序退出时这个线程会被自动回收,并通过 `atexit` 注册了 `close()` 来清理任务(第 423-424 行)。

#### 4.3.4 代码实践

**实践目标**:验证「`generate` 产出的异步生成器」与「`_infer` 取回的 `Response`」之间的对应关系,并理解限流信号量。

**操作步骤**:

1. 阅读 [lmdeploy/pipeline.py:365](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pipeline.py#L365) 的 `_infer`,在 4.3.3 列出的 5 个要点旁各写一句中文注释。
2. 用「源码阅读型实践」回答:若用户同时提交 100 条 prompt,而 `max_batch_size=8`,这 100 条请求会怎样被处理?

```text
提示:看 _infer 内层 _infer() 的 for 循环 + sem.acquire()。
     第 9 条请求会阻塞在 await sem.acquire() 上,
     直到前面 8 条中有一条在 _sync_resp 里 sem.release() 才放行。
```

3. (可选,需 GPU)跑一段真实推理,对比 `infer`(非流式)与 `stream_infer`(流式)在 `_infer` 里的区别——前者 `multiplex=False`,后者 `multiplex=True`:

```python
# 示例代码:需 GPU + 本地模型,具体输出待本地验证
from lmdeploy import pipeline, GenerationConfig, PytorchEngineConfig

pipe = pipeline('/path/to/local/model',
                backend_config=PytorchEngineConfig(tp=1, cache_max_entry_count=0.4))

# 非流式:一次性拿完整 Response
for resp in pipe(['你好', '介绍一下你自己'], gen_config=GenerationConfig(do_sample=False, max_new_tokens=32)):
    print(resp.response, resp.generate_token_len)

# 流式:逐段 yield
for pieces in pipe.stream_infer(['你好', '介绍一下我自己']):
    for out in pieces:
        print(out.response, end='', flush=True)
    print()
```

**需要观察的现象**:

- 非流式模式下,`_infer` 用**每请求独立**的 `dst = Queue()`([pipeline.py:384](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pipeline.py#L384)),所以每条请求的结果各自成块、互不交错;外层 `for g in self._infer(...)` 按**提交顺序**拿到每条请求的完整结果。
- 流式模式下,所有任务共享同一个 `que`,`_sync_resp` 把每个 token 直接 `put` 进去,因此**多个请求的片段会交错出现**。

**预期结果**:**待本地验证**(需 GPU 与已下载的模型)。即使不跑,你也应能从 `dst = que if multiplex else Queue()` 这一行([pipeline.py:384](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pipeline.py#L384))推断出上述行为差异。

#### 4.3.5 小练习与答案

**练习 1**:`_infer` 里的 `asyncio.run_coroutine_threadsafe(_infer(), loop)` 如果改成直接 `await _infer()`,会出什么问题?

> **答案**:会直接报错——`Pipeline.infer` 是普通同步函数,根本没有 `await` 可用;即使改成 async,也会把整个推理跑在主线程的事件循环里,与「后台专用事件循环」的设计冲突。`run_coroutine_threadsafe` 的意义就在于**跨线程**把协程投递到 `_EventLoopThread` 的循环里,让主线程不被阻塞。

**练习 2**:`_sync_resp` 里 `sem.release()` 紧跟在 `async for out in g` 之后。如果某个请求在生成中途抛异常,信号量会被释放吗?这会有什么后果?

> **答案**:不会——异常会从 `async for` 抛出,跳过 `sem.release()`,信号量永久少一个名额。后果是:连续多次失败后,`_get_limiter()` 的信号量被耗尽,后续请求会永远阻塞在 `await sem.acquire()`。这也是为什么 `Pipeline.chat` 在 `_gen()` 里用 `try/except` 捕获异常后主动调 `session.async_abort()`([pipeline.py:223-226](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pipeline.py#L223-L226))来兜底清理。完整的异常清理是一个值得你继续追踪的改进点。

**练习 3**:`return iter(que.get, None)` 中,如果协程还没来得及 `put` 任何东西,主线程会怎样?

> **答案**:主线程会在 `que.get()` 上**阻塞等待**,直到后台线程 `put` 了第一个结果或 `None` 哨兵。这正是期望行为——同步调用本就该「等结果」。这种「先返回迭代器、迭代时再阻塞」的写法,让流式接口(`stream_infer`)得以实现:调用方拿到迭代器后,按需逐个取结果。

## 5. 综合实践

**任务:把本讲三节串起来,手工「复述」一次 `pipeline()` 的完整构造过程,并验证你的理解。**

1. **画对象树**:在笔记上画出 4.1.1 那棵对象树(`Pipeline → async_engine → engine → 句柄`),并在每个节点旁标注:**它由哪一行源码创建?它的类型在 PyTorch 后端下是什么、在 TurboMind 后端下又是什么?**
   - 参考答案:`self.async_engine` 由 [pipeline.py:78](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pipeline.py#L78) 创建,类型是 `AsyncEngine` 或 `VLAsyncEngine`;`self.engine` 由 [async_engine.py:134-146](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/core/async_engine.py#L134-L146) 创建,PyTorch 下是 `lmdeploy.pytorch.engine.Engine`,TurboMind 下是 `lmdeploy.turbomind.turbomind.TurboMind`;句柄由 [session_manager.py:174](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/managers/session_manager.py#L174) 批量创建,分别是 `EngineInstance` / `TurboMindInstance`。

2. **手动构造 PytorchEngineConfig 并创建 pipeline**(承接本讲规格里的实践任务):

```python
# 示例代码:强制走 PyTorch 后端
from lmdeploy import pipeline, PytorchEngineConfig

cfg = PytorchEngineConfig(tp=1, cache_max_entry_count=0.4, max_batch_size=8)
pipe = pipeline('/path/to/local/model', backend_config=cfg)

# 验证配置回填:取回的应当是引擎补全后的配置
print('backend      =', pipe.backend_config)        # 应为 PytorchEngineConfig 实例
print('max_batch_size =', pipe.backend_config.max_batch_size)
print('type(async_engine) =', type(pipe.async_engine).__name__)
print('type(engine)       =', type(pipe.async_engine.engine).__name__)
pipe.close()
```

3. **回答三个问题**(写在笔记里,作为本讲出口检查):
   - 为什么 `pipe.backend_config` 可能与 `cfg` 不完全相同?(提示:`AsyncEngine.__init__` 回填了 `session_len`。)
   - `pipeline_class` 是 `AsyncEngine` 还是 `VLAsyncEngine`,由哪个函数、依据什么决定?(提示:`get_task` + `check_vl_llm`,见 [archs.py:132-140](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/archs.py#L132-L140)。)
   - 当你在 `pipe(...)` 里同时提交 100 条 prompt,是谁在限制并发?上限是多少?(提示:`_get_limiter` 的信号量,上限 = `max_batch_size`。)

**预期结果**:第 2 步的具体输出**待本地验证**(需 GPU 与本地模型)。第 1、3 步是纯阅读理解题,本讲已给出全部线索,无需运行即可完成。

## 6. 本讲小结

- `Pipeline.__init__` 是一棵对象树的根:它把模型路径变成 `Pipeline(async_engine(engine(句柄)))` 的四层结构,核心代码在 [pipeline.py:71-90](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pipeline.py#L71-L90)。
- 「选后端」分两层:`archs.autoget_backend_config` 决定 `backend` 字符串与配置对象(第一层),`AsyncEngine.__init__` 用 `if/elif` 真正构造 `self.engine`(第二层,[async_engine.py:134-146](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/core/async_engine.py#L134-L146))。
- **强制走 PyTorch** 的唯一干净入口是显式传 `PytorchEngineConfig`,会触发 [archs.py:74-75](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/archs.py#L74-L75) 的短路;传 TurboMind 配置但模型不被支持时,会触发跨后端字段搬运(`block_size ↔ cache_block_seq_len`)。
- **Pipeline → Engine → create_instance** 的完整链路:`Pipeline.__init__` → `pipeline_class(...)`(即 `AsyncEngine`)→ `_build_pytorch/_build_turbomind` 产出 `self.engine` → `session_mgr.build_request_handle_pool` 批量 `engine.create_instance()` 生成句柄。
- `_infer` 用「后台事件循环线程 + `queue.Queue` + 哨兵 `None`」把异步引擎桥接成同步接口;并发由 `max_batch_size` 的信号量限制;`multiplex` 开关区分非流式(`infer`)与流式(`stream_infer`)。
- **职责边界**:Pipeline 只调度与翻译,不碰张量;Engine 才是真正跑 forward 的地方。`self.backend_config` 取的是引擎回填后的最终配置,而非用户传入的那一份。

## 7. 下一步学习建议

本讲止步于「`self.engine` 是什么、怎么被建出来、怎么被驱动」。接下来建议:

1. **`u3-l2` PyTorch 引擎配置数据类**:`config.py` 里的 `ModelConfig` / `CacheConfig` / `SchedulerConfig` 是 `Engine.from_pretrained` 真正消费的配置——它们与用户面的 `PytorchEngineConfig` 是两套东西,搞清二者的转换是下一讲的核心。
2. **`u3-l5` 权重加载**:`Engine.from_pretrained` 内部如何把 HF 权重灌进模型,是 4.2 里 `_build_pytorch` 的自然延伸。
3. **`u4-l1` Engine 主类与请求管理**:本讲把 `self.engine` 当黑盒,`u4-l1` 会打开它,讲清 `Engine` 如何管理 session、绑定 `RequestManager`、启动推理循环。
4. **`u6-l2` TurboMind 的 Python 包装**:如果你更关心 TurboMind 分支,`u6-l2` 会讲 `TurboMind.from_pretrained` 与 `TurboMindInstance.prepare_inputs`,对应本讲 4.2 里被略过的那条分支。

> 阅读建议:先把本讲的「对象树」和「调用链」两幅图抄在笔记扉页,后续几讲每次遇到新对象,都试着把它挂到这棵树上——这是阅读大型项目源码最有效的定位手段。
