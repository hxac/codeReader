# Python 包装 TurboMind / TurboMindInstance

> 本讲承接 u6-l1《TurboMind 后端概览与 C++ 扩展》。上一讲我们建立了「Python → C++」的全局地图：TurboMind 是 C++/CUDA 高性能后端，C++ 源码集中在 `src/turbomind/`，两侧靠 pybind11 扩展 `_turbomind`（下文简称 `_tm`）桥接，张量经 DLPack 零拷贝互传。本讲就钻进这层「桥」的 Python 侧——`lmdeploy/turbomind/turbomind.py`，看它如何把一串 token 变成一次真正的 C++ forward 调用，又如何把 C++ 产出的 token 流送回给上层。

## 1. 本讲目标

学完本讲，你应当能够：

1. 说清 `TurboMind` 类的职责——它如何解析 HF 模型、构造 C++ 引擎、把权重灌进 C++ 运行时，并对外暴露 `from_pretrained` / `create_instance` 等工厂方法。
2. 跟踪 `TurboMindInstance` 类从「拿到 input_ids」到「逐 token 流式产出」的完整数据流，特别是 `prepare_inputs` 与 `async_stream_infer`。
3. 解释 `input_meta` 这个字典里到底装了什么（以 MRoPE 为例）。
4. 看懂 Python 张量与 C++ 张量之间靠 `torch.from_dlpack` / `_tm.from_dlpack` 做零拷贝互转的两条封装函数。
5. 在源码里准确定位「`from_pretrained` → `create_instance`」这条最常被调用的构造链。

## 2. 前置知识

本讲默认你已掌握 u6-l1 的内容，并具备以下背景：

- **两个同名概念的区分**：用户面 `TurbomindEngineConfig`（定义在 `lmdeploy/messages.py`）是「引擎长什么样」的配置；本讲涉及的 `_tm.EngineConfig` 是 C++ 侧的配置结构体，二者字段几乎一一对应但类型不同，需要在 `_from_hf` 里逐字段搬运。同理，用户面 `GenerationConfig` 与 `_tm.GenerationConfig` 也是一对镜像。
- **pybind11 桥接**：C++ 类经 `bind.cpp` 编译成 `_turbomind.so`（装入 `lmdeploy/lib/`），在 Python 里以 `_tm` 模块出现。`_tm.TurboMind`、`_tm.EngineConfig`、`_tm.TensorMap`、`_tm.SessionParam` 都是 C++ 对象的 Python 影子。
- **DLPack**：一个跨框架的张量内存交换标准。只要两个框架都认 DLPack，就能在不拷贝数据的前提下共享同一块显存/内存。`torch.from_dlpack(x)` 把别处的 DLPack 张量变成 `torch.Tensor`，反向同理。
- **句柄模式（Handle）**：`TurboMind` 是「重量级」对象（持有整个引擎、权重、KV 池），全局只建一份；`TurboMindInstance` 是「轻量级」句柄（持有一个 C++ request 对象），一次推理领一个，可并发。这与 u4-l3 PyTorch 侧的 `EngineInstance` 思路一致。
- **持续批处理 / 会话（session）**：TurboMind 用 `session_id` 标识一条对话，`sequence_start` / `sequence_end` 标记一条会话的首尾，KV cache 在同一 session 内跨请求复用。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [lmdeploy/turbomind/turbomind.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/turbomind/turbomind.py) | 本讲主战场。定义 `TurboMind`（引擎创建/权重）与 `TurboMindInstance`（单次推理句柄）两个类，以及 DLPack 互转的两个辅助函数。约 860 行。 |
| [lmdeploy/turbomind/text_model.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/turbomind/text_model.py) | 每个模型架构对应的「C++ 配置生成器」抽象基类 `TextModel`。它由 `converter.get_tm_config` 产出，被 `ModelLoader.export()` 调用，把权重逐层提交给 C++。理解它有助于看清「权重是怎么进 C++ 的」。 |
| [lmdeploy/turbomind/model_loader.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/turbomind/model_loader.py) | `ModelLoader` 负责把权重真正灌进 C++ 运行时（`export()` 调用 `model.model(Prefix(ckpt))`）。本讲只点到为止，权重细节留给 u6-l4。 |
| [lmdeploy/serve/core/async_engine.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/core/async_engine.py) | 上层 `AsyncEngine._build_turbomind` 调用 `tm.TurboMind.from_pretrained(...)`，是 `TurboMind` 的主要调用方，帮助看清它在整个栈中的位置。 |

---

## 4. 核心概念与源码讲解

### 4.1 TurboMind 类：引擎创建与权重装配

#### 4.1.1 概念说明

`TurboMind`（Python 类，注意首字母大写，区别于 C++ 的 `_tm.TurboMind`）是 C++ 引擎的 Python「门面」。它不自己跑 forward，而是承担三件事：

1. **解析**：读取 HuggingFace 模型目录，经 `converter.get_tm_config` 把 HF 的 `config.json` 翻译成一份 TurboMind 能理解的模型拓扑（`TextModel` 子类实例）和数据类型。
2. **建引擎**：把 `TurbomindEngineConfig`（用户面）逐字段搬进 `_tm.EngineConfig`（C++ 面），调用 `_tm.TurboMind.create(...)` 创建 C++ 引擎通信句柄 `model_comm`，并为每张卡创建 context 与权重根节点。
3. **灌权重**：让 `ModelLoader` 把磁盘上的权重逐层提交给 C++ 运行时，随后 `_process_weights` / `_create_engine` 完成 C++ 侧的后处理与引擎定稿。

对外，它把这一切藏在两个工厂方法背后：`from_pretrained(...)`（类方法，最常用）与 `create_instance(...)`（返回一个推理句柄）。上层 `AsyncEngine` 永远只调这两个方法。

#### 4.1.2 核心流程

一次「从模型路径到可推理对象」的构造链如下（伪代码）：

```
AsyncEngine._build_turbomind(model_path)
  └─ TurboMind.from_pretrained(model_path, engine_config)        # 类方法，转发给 __init__
       └─ TurboMind.__init__(...)
            ├─ update_parallel_config(engine_config)             # 推导 tp/dp/cp 拓扑
            ├─ self.model_comm, loader = self._from_hf(...)      # ★ 核心：解析+建引擎+建loader
            │     ├─ is_supported(model_path)                    # 白名单校验，不在表内直接报错
            │     ├─ get_tm_config(...)  → model, dtype           # HF config → TextModel 拓扑
            │     ├─ ec = _tm.EngineConfig(); 逐字段搬运          # 用户配置 → C++ 配置
            │     ├─ model_comm = _tm.TurboMind.create(ec)        # ★ 创建 C++ 引擎句柄
            │     ├─ self._create_weight(model_comm)              # 每张卡 create_context + create_root
            │     └─ ModelLoader(model, model_comm, ...)          # 绑定运行时，待会儿 export 权重
            ├─ loader.export()          # 把磁盘权重灌进 C++（仅非 empty_init）
            ├─ self._process_weights()  # C++ 侧 process_weight（多卡并发）
            └─ self._create_engine()    # C++ 侧 create_engine（多卡并发），标记 _engine_created=True

TurboMind.create_instance(cuda_stream_id)
  └─ TurboMindInstance(self, cuda_stream_id)                     # 返回轻量句柄
```

三个要点：

- **延迟初始化（empty_init）**：当 `engine_config.empty_init=True` 时，`__init__` 跳过 `export/_process_weights/_create_engine`，引擎先「空着」创建，权重随后由 `update_params` 分批喂入（用于服务端热更新权重，见 `update_params` 方法）。`TurboMindInstance` 也对应地延迟创建 C++ request 对象。
- **多卡并发**：凡是「每张卡都要做一遍」的 C++ 调用（`create_context`、`process_weight`、`create_engine`），都用 `ThreadPoolExecutor(max_workers=gpu_count)` 并发触发，以便各 rank 同时撞上 C++ 里的同步屏障（`h_global->Sync()`）。
- **白名单把关**：`_from_hf` 第一步就断言 `is_supported(model_path)`，不支持直接抛错并提示改用 PyTorch 后端——这是 u6-l1 提到的「TurboMind 只认 `SUPPORTED_ARCHS`」在代码里的落点。

#### 4.1.3 源码精读

**入口工厂 `from_pretrained`**：纯粹的转发，真正干活的是 `__init__`。

[lmdeploy/turbomind/turbomind.py:338-369](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/turbomind/turbomind.py#L338-L369) —— 类方法 `from_pretrained`，把 `pretrained_model_name_or_path` 当作 `model_path` 透传给 `__init__`，docstring 里说明了它接受的三类路径（本地 tm 目录 / 量化模型 id / 普通 HF 模型 id）。

**构造函数 `__init__`**（节选关键步骤）：

[lmdeploy/turbomind/turbomind.py:136-175](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/turbomind/turbomind.py#L136-L175) —— 深拷贝配置避免污染用户传入对象；默认补 `max_batch_size`；调 `update_parallel_config` 推导并行拓扑；多机时建 `TCPStore` 同步各 rank；最后调 `_from_hf` 拿到 `model_comm` 与 `loader`，非 empty_init 时依次 `export → _process_weights → _create_engine`。

**核心装配 `_from_hf`**（最值得读的一段）：

[lmdeploy/turbomind/turbomind.py:211-275](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/turbomind/turbomind.py#L211-L275) —— 这段做了四件事：

1. `is_supported` 白名单校验（行 214-216）。
2. `get_tm_config` 得到 `model`（`TextModel` 拓扑）、`model_path`、`data_type`（行 218-222）。
3. 用 `dtype_map` 把字符串 `'float16'/'bfloat16'` 映射成 `_tm.DataType.TYPE_FP16/TYPE_BF16`，并构造 `ec = _tm.EngineConfig()`，**逐字段把用户面 `engine_config` 搬进 C++ 面 `ec`**（行 227-253）。这一段是「配置搬运」的典型写法——没有任何魔法，就是一行一个字段赋值。
4. `model_comm = _tm.TurboMind.create(model_dir='', engine_config=ec)`（行 263）真正创建 C++ 引擎；`self._create_weight(model_comm)`（行 264）为每张卡建 context + root；最后构造 `ModelLoader`（行 266-273）待后续灌权重。

**权重定稿与引擎创建**：

[lmdeploy/turbomind/turbomind.py:183-188](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/turbomind/turbomind.py#L183-L188) —— `_create_engine` 多卡并发调 `model_comm.create_engine`，并把 `self._engine_created` 置 `True`。健康检查（见 4.1.3 末尾）正是靠这个标志位判断引擎是否就绪。

**对外句柄工厂 `create_instance`**：

[lmdeploy/turbomind/turbomind.py:380-388](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/turbomind/turbomind.py#L380-L388) —— 一行实现：`return TurboMindInstance(self, cuda_stream_id)`。`cuda_stream_id` 仅为与 PyTorch 后端 API 对齐保留（u4-l1 已说明），TurboMind 内部并不真正按它分流。

> 补充：`TurboMind` 还有 `sleep` / `wakeup`（KV cache 与权重的显存换出/换入）、`update_params`（热更新权重）、`get_health_status`（健康探针）等运维方法，本讲不展开，但它们都遵循同一个模式：用 `ThreadPoolExecutor` 把单个 C++ 调用并发到每张卡。健康状态判定见 [lmdeploy/turbomind/turbomind.py:400-422](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/turbomind/turbomind.py#L400-L422)。

#### 4.1.4 代码实践

**目标**：在源码里画出 `from_pretrained` 到 `create_instance` 的完整调用链，并定位「逐字段搬运配置」的那段代码。

**操作步骤**：

1. 打开 [lmdeploy/turbomind/turbomind.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/turbomind/turbomind.py)。
2. 从第 338 行 `from_pretrained` 开始，按 4.1.2 的伪代码逐层跳转：`from_pretrained → __init__ → _from_hf → _tm.TurboMind.create → _create_weight → _process_weights → _create_engine → create_instance`。
3. 在第 227–253 行的 `dtype_map` 与 `ec.xxx = engine_config.xxx` 区块旁，用注释标出「这是用户面 `TurbomindEngineConfig` 到 C++ 面 `_tm.EngineConfig` 的逐字段搬运」。

**需要观察的现象**：

- `ec.data_type` 的取值来自 `dtype_map`，dtype 字符串到 `_tm.DataType` 枚举的映射只有 `'bfloat16'` 与 `'float16'` 两个键——说明 TurboMind 推理只支持这两种浮点精度（量化则由 `quant_policy` 另算）。
- `ec.cache_max_block_count = engine_config.cache_max_entry_count`：注意这里字段名发生了「语义重命名」，用户面的 `cache_max_entry_count` 在 C++ 侧叫 `cache_max_block_count`，但赋值是直接搬的（u2-l3 提到 TurboMind 的该字段可传整数表示 KV block 总数）。

**预期结果**：你能用一句话向同伴解释「TurboMind 实例化时，C++ 引擎是怎么被一行行配置出来的」，并且能指出 `model_comm` 这个变量是后续一切 C++ 调用的总入口。

> 待本地验证：上述行为依赖编译好的 `_turbomind` 扩展。若当前环境未编译（`lmdeploy/lib/` 下无 `_turbomind.so`），`import _turbomind` 会失败，届时只能做源码阅读型实践，无法真正实例化引擎。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `TurboMind.__init__` 里要 `copy.deepcopy(engine_config)`？

**答案**：因为 `update_parallel_config` 会就地修改 `engine_config` 的诸多并行字段（`attn_tp_size`、`mlp_tp_size`、`devices` 等）。若不深拷贝，会污染调用方（`AsyncEngine`）持有的配置对象，导致同一份配置被多次复用时拓扑计算错误。

**练习 2**：`empty_init=True` 时，`__init__` 会跳过哪三步？这些步骤后来由哪个方法补做？

**答案**：跳过 `loader.export()`（灌权重）、`self._process_weights()`（C++ 权重后处理）、`self._create_engine()`（C++ 引擎定稿）。这三步后来由 `update_params` 方法在收齐全部权重后（`request.finished=True` 时）补做，见 [turbomind.py:333-336](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/turbomind/turbomind.py#L333-L336)。

---

### 4.2 TurboMindInstance 类：输入准备与流式输出

#### 4.2.1 概念说明

`TurboMindInstance` 是「一次推理会话」的句柄。它本身极轻量——构造时只持有一个由 `model_comm.create_request()` 创建的 C++ request 对象（`self._model_inst`），以及一个把 C++ 错误码翻译成 `ResponseType` 的映射表 `errcode_map`。真正干活的是它的核心方法 `async_stream_infer`，它完成四件事：

1. **准备输入**：`prepare_inputs` 把 Python 侧的 `input_ids`（列表）、可选的图像 `input_embeddings`、可选的多模态位置编码 `input_meta` 组装成一个规整的输入字典。
2. **翻译生成配置**：`_get_generation_config` 把用户面 `GenerationConfig` 逐字段搬进 `_tm.GenerationConfig`，并处理 stop/bad words 的打包格式。
3. **触发 forward**：把输入字典经 DLPack 转成 `_tm.TensorMap`，连同 `session`、`gen_cfg` 一起交给 `model_inst.forward(...)`，拿到输出张量映射、`shared_state`（共享状态，用于流式消费）和 `metrics`。
4. **流式消费**：在一个 `while True` 循环里反复 `await sem.acquire()`（等 C++ 信号线程的通知）+ `shared_state.consume()`（读最新进度），按 `seq_len` 增量切片 `output_ids_buf`，逐段 `yield EngineOutput`。

`StreamingSemaphore` 是这条流式链路的关键同步原语：C++ 的信号线程通过回调 `async_signal_cb` 在 Python 的事件循环上 `release()` 信号量，Python 协程则在 `acquire()` 上挂起等待，从而把「C++ 线程」的产出安全地喂回「Python asyncio 协程」。

#### 4.2.2 核心流程

`async_stream_infer` 的数据流（伪代码）：

```
async_stream_infer(session_id, input_ids, gen_config, input_meta=None, ...)
  ├─ gen_cfg = self._get_generation_config(gen_config)     # 用户面 → _tm.GenerationConfig
  ├─ inputs, input_len = self.prepare_inputs(...)          # 组装 torch 张量字典
  │     ├─ input_ids → torch.IntTensor
  │     ├─ prepare_embeddings(...)        # 多模态：把多段 embedding 拼成 values + ranges
  │     └─ prepare_mrope(input_meta,...)  # 若 input_meta 含 mrope_position_ids
  ├─ （可选）guided decoding：gen_config.response_format → _xgr.GrammarCompiler → model_inst.set_grammar
  ├─ session = _tm.SessionParam(id, step, start, end)
  ├─ inputs = _np_dict_to_tm_dict(inputs)                 # ★ torch 张量 → _tm.Tensor（DLPack）
  ├─ outputs, shared_state, metrics = model_inst.forward( # ★ 进入 C++
  │       inputs, mm_inputs, session, gen_cfg, stream_output,
  │       enable_metrics, signal_cb)
  ├─ outputs = _tm_dict_to_torch_dict(outputs)            # ★ _tm.Tensor → torch 张量（DLPack）
  ├─ extra_fs = self._get_extra_output_processors(...)    # logits/ppl/hidden/logprobs/metrics 钩子
  └─ while True:
       await sem.acquire()              # 等 C++ 信号线程唤醒
       state = shared_state.consume()   # 读 status / seq_len
       if status in (7, 8):  finish / cancel
       output_ids = output_ids_buf[prev_len:seq_len].tolist()   # 增量切片
       yield EngineOutput(ret_status, output_ids)
       if finish: break
```

几个关键概念：

- **增量切片**：C++ 的 `output_ids_buf` 是一个预分配的整块缓冲，存放到目前为止的全部生成 token。Python 侧用 `prev_len = step + input_len` 初始化游标，每次只取 `[prev_len:seq_len]` 这一段作为「本步新产出」，再把 `prev_len` 推进到 `seq_len`。这与 PyTorch 侧 `EngineInstance` 用 `output_offset` 切累计 token 是同一思路（见 u4-l3）。
- **错误码映射**：C++ 返回的 `status` 是整数（对应 `src/turbomind/engine/request.h` 的 `struct Request`），`errcode_map` 把它翻译成用户面的 `ResponseType`（`SUCCESS / FINISH / CANCEL / INPUT_LENGTH_ERROR / ...`）。例如 `status==7` 是 `FINISH`，`status==8` 是 `CANCEL`，`status==6` 是 `INPUT_LENGTH_ERROR`。
- **首道防线**：`status==6`（输入超长）会在循环里被当作错误 `yield` 出去，是 TurboMind 对「prompt 超过 session_len」的兜底响应。

#### 4.2.3 源码精读

**构造与错误码表**：

[lmdeploy/turbomind/turbomind.py:555-579](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/turbomind/turbomind.py#L555-L579) —— `TurboMindInstance.__init__`：持有 `tm_model`（父引擎）、`cuda_stream_id`；若 `empty_init` 则延迟创建 `_model_inst`（由 `model_inst` property 懒加载，见 [581-585](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/turbomind/turbomind.py#L581-L585)）；`errcode_map` 把 C++ 整数状态码映射到 `ResponseType`，行内注释明确指向 `src/turbomind/engine/request.h`。

**C++ request 对象的创建**：

[lmdeploy/turbomind/turbomind.py:587-589](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/turbomind/turbomind.py#L587-L589) —— `_create_model_instance` 一行 `self.tm_model.model_comm.create_request()`，返回的是 C++ 侧的请求对象，所有 forward/cancel/end 调用都挂在它上面。

**`prepare_inputs` 与 `input_meta`**（本讲实践重点）：

[lmdeploy/turbomind/turbomind.py:643-668](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/turbomind/turbomind.py#L643-L668) —— 这是 `input_meta` 真正被消费的地方。逻辑分三段：

1. 把 `input_ids`（任意 Sequence）转成 `torch.IntTensor`，记下 `input_len`。
2. 调 `prepare_embeddings` 处理多模态图像 embedding（见 [612-634](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/turbomind/turbomind.py#L612-L634)）：把多段 embedding 拼成一个大 `values` 张量，并记录每段在序列中的起止区间 `ranges`。
3. **若 `input_meta` 且含 `'mrope_position_ids'`**，调 `prepare_mrope`（见 [636-641](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/turbomind/turbomind.py#L636-L641)），把 MRoPE 的三维位置 id（时间/高/宽三个维度）转置成连续内存，连同 `mrope_position_delta`、`mrope_length` 一并塞进 `inputs`。

那么 `input_meta` 里到底有什么？目前它只承载 **MRoPE**（多模态旋转位置编码，Qwen2-VL 等模型用）两类字段：

| 字段 | 含义 |
| --- | --- |
| `mrope_position_ids` | 形状 `(3, seq_len)` 的位置 id，三行分别对应时间、高度、宽度三个维度的旋转位置（MRoPE 把一维 RoPE 推广到三维以编码图像的网格结构）。 |
| `mrope_position_delta` | 标量，记录本段输入结束后的位置基准偏移，供续写下一请求时复用。 |

这两个字段并非 TurboMind 自己生成，而是由多模态前端在预处理时算好塞进 `input_meta`——例如 [lmdeploy/vl/model/qwen2.py:198-200](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/vl/model/qwen2.py#L198-L200) 里 `Qwen2VL` 的处理器就构造了 `meta = dict(mrope_position_ids=..., mrope_position_delta=...)` 并放进 `input_meta`。纯文本模型走默认一维 RoPE，`input_meta` 为 `None`，这一段直接跳过。

**生成配置翻译 `_get_generation_config`**：

[lmdeploy/turbomind/turbomind.py:832-863](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/turbomind/turbomind.py#L832-L863) —— 把用户面 `GenerationConfig` 逐字段搬进 `_tm.GenerationConfig`。注意几处特殊处理：`stop_token_ids` 同时映射到 C++ 的 `eos_ids`（终止采样）和 `stop_ids`（停止词，仅当 `ignore_eos=False`）；`bad_token_ids` 经 `_construct_stop_or_bad_words` 打包成 `[words, offsets]` 二元组；`logprobs` 上限被 `MAX_LOGPROBS=1024` 截断（见 [37](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/turbomind/turbomind.py#L37)）。

**核心推理循环 `async_stream_infer`**：

[lmdeploy/turbomind/turbomind.py:760-827](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/turbomind/turbomind.py#L760-L827) —— 这段是整条流式链路的主体：

- 行 760 构造 `_tm.SessionParam(id=session_id, step=step, start=sequence_start, end=sequence_end)`，把会话边界信息交给 C++。
- 行 768-769 调 `model_inst.forward(...)` 真正进入 C++，返回三元组 `outputs, shared_state, metrics`；`signal_cb` 是 C++ 信号线程回调，经 `partial(self.async_signal_cb, sem)` 绑到信号量上（行 766）。
- 行 775 取出 `output_ids_buf = outputs['output_ids']`——这是 C++ 写、Python 读的整块 token 缓冲。
- 行 781 `prev_len = step + input_len` 初始化增量游标。
- 行 783-812 的 `while True` 循环：`await sem.acquire()` 等通知 → `shared_state.consume()` 读 `status/seq_len` → 若 `status` 为 7/8 标记结束 → 增量切片 `output_ids_buf[prev_len:seq_len]` → 组装 `EngineOutput` 并跑各 `extra_fs` 钩子（logits/ppl/logprobs/metrics）→ `yield` → 结束则 break。
- 行 822-827 的 `finally`：契约保证 status 非零后回调不再被调用，故需自旋等待 `state.status != 0`，确保会话干净收尾。

> 配套同步原语 `StreamingSemaphore` 见 [lmdeploy/turbomind/turbomind.py:524-544](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/turbomind/turbomind.py#L524-L544)：`release()` 用 `call_soon_threadsafe` 安全跨线程点亮 future，是「C++ 信号线程 → Python 协程」的桥梁。回调 `async_signal_cb` 见 [683-685](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/turbomind/turbomind.py#L683-L685)。

#### 4.2.4 代码实践

**目标**：找到 `prepare_inputs`，说清 `input_meta` 每个字段是什么、从哪来、怎么进 C++。

**操作步骤**：

1. 打开 [lmdeploy/turbomind/turbomind.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/turbomind/turbomind.py)，定位 643 行 `prepare_inputs` 与 636 行 `prepare_mrope`。
2. 列出 `input_meta` 当前承载的字段（`mrope_position_ids`、`mrope_position_delta`），并用一句话写清每个字段的形状与含义（参考 4.2.3 的表格）。
3. 追溯字段来源：跳到 [lmdeploy/vl/model/qwen2.py:198-200](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/vl/model/qwen2.py#L198-L200)，确认这两个字段是「多模态前端算好后塞进 `input_meta`」的，TurboMind 本身只负责消费。
4. 跟踪这些字段如何进入 C++：在 `async_stream_infer` 里，`prepare_inputs` 返回的 `inputs` 字典在第 762 行被 `_np_dict_to_tm_dict` 整体转成 `_tm.TensorMap`，随后随 `forward` 进入 C++。也就是说 `mrope_position_ids` 经 DLPack 零拷贝送进 C++，由 C++ 侧的 RoPE 实现读取。

**需要观察的现象**：

- `prepare_mrope` 里有断言 `mrope_position_ids.size(-1) == input_len`（行 639）：MRoPE 的位置 id 最后一维必须严格等于输入 token 数，否则说明前端算错了。
- `mrope_position_ids` 在送入 `inputs` 前被 `.t().contiguous()`（行 640）转置并连续化：前端给的是 `(3, seq_len)`，C++ 期望的是 `(seq_len, 3)` 连续布局——这是一处典型的「Python 侧为 C++ kernel 调整内存布局」的适配。

**预期结果**：你能向同伴解释「为什么纯文本推理不需要 `input_meta`，而图文推理（Qwen2-VL）必须传 `input_meta`」，并能指出 MRoPE 三维位置编码的三个维度分别对应什么。

> 待本地验证：完整跑通图文推理需安装带 `_turbomind` 扩展的 lmdeploy 与一个支持 MRoPE 的 VLM。无 GPU/未编译环境只能做源码阅读。

#### 4.2.5 小练习与答案

**练习 1**：`async_stream_infer` 里，`prev_len = step + input_len` 这一行的作用是什么？为什么后续切片用 `output_ids_buf[prev_len:seq_len]`？

**答案**：`step` 是已有 KV cache 的长度（续写场景下非 0），`input_len` 是本次输入的 token 数，二者之和就是「到本次输入为止序列的总长度」，即生成 token 的起点 `prev_len`。C++ 的 `output_ids_buf` 是累计缓冲，包含输入与全部已生成 token；用 `[prev_len:seq_len]` 切片能精确取出「本步新生成」的那一段，避免重复 yield 旧 token。每步结束后 `prev_len = seq_len` 推进游标。

**练习 2**：`status==6` 在 `errcode_map` 里对应什么 `ResponseType`？它通常在什么场景下出现？

**答案**：对应 `ResponseType.INPUT_LENGTH_ERROR`（见 [573](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/turbomind/turbomind.py#L573)）。它出现在 prompt 折算后的 token 数加 step 超过了引擎配置的 `session_len` 时，是 TurboMind 对输入超长的兜底拒绝，被称为「首道防线」。

---

### 4.3 DLPack 张量互操作

#### 4.3.1 概念说明

Python 与 C++ 之间频繁传递张量（输入 token id、embedding、位置编码、输出的 logits / output_ids / logprobs）。如果每次都拷贝一遍数据，开销巨大。DLPack 标准解决了这个问题：它定义了一个跨框架的张量「信封」（含数据指针、形状、stride、dtype、device），任何实现该标准的框架都能「拆信封」拿到同一块内存，实现零拷贝共享。

TurboMind 的 Python 包装里，两个方向各有一个封装函数：

- **进 C++**：`_np_dict_to_tm_dict`——把 Python 字典里的每个张量用 `_tm.from_dlpack(v)` 包成 `_tm.Tensor`，装进 `_tm.TensorMap`。
- **出 C++**：`_tm_dict_to_torch_dict`——把 `_tm.TensorMap` 里每个 `_tm.Tensor` 用 `torch.from_dlpack(v)` 变回 `torch.Tensor`，并处理一处 `UINT32 → INT32` 的视图转换。

这两个函数是 Python 与 C++ 张量交互的全部「翻译官」，理解它们就理解了 TurboMind 的数据边界。

#### 4.3.2 核心流程

```
Python 侧                                C++ 侧 (_tm)
─────────                                ────────────
inputs = { 'input_ids': torch.IntTensor, ...
         }
   │
   ▼  _np_dict_to_tm_dict(inputs)
ret = _tm.TensorMap()
for k, v in inputs.items():
    ret[k] = _tm.from_dlpack(v)   ───►  _tm.Tensor（共享同一块内存）
   │
   ▼
outputs, shared_state, metrics = model_inst.forward(ret, ...)
                                          │  C++ forward 读写这块共享内存
                                          ▼
   ◄────────────────────────────────  outputs: _tm.TensorMap
   │
   ▼  _tm_dict_to_torch_dict(outputs)
ret = {}
for k, v in outputs.items():
    if v.type == TYPE_UINT32:            # output_ids 在 C++ 是 uint32
        v = v.view(TYPE_INT32)           # Python 习惯用 int32
    ret[k] = torch.from_dlpack(v) ◄───  torch.Tensor（零拷贝共享）
```

两个细节：

- **`view` 而非 `astype`**：`v.view(_tm.DataType.TYPE_INT32)` 是零拷贝的位模式重解释（uint32 与 int32 同为 4 字节），不分配新内存。这是把 C++ 的 `uint32` token id 交给 PyTorch（`int32` 友好）的高效做法。
- **方向不对称**：进 C++ 用 `_tm.from_dlpack`（`_tm` 的构造器），出 C++ 用 `torch.from_dlpack`（PyTorch 的构造器）。函数名相同、命名空间不同，分别把 DLPack 张量「吸纳」进各自的框架。

#### 4.3.3 源码精读

**进 C++ 的翻译官**：

[lmdeploy/turbomind/turbomind.py:48-54](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/turbomind/turbomind.py#L48-L54) —— `_np_dict_to_tm_dict`：构造一个空的 `_tm.TensorMap()`，遍历 Python 字典，逐个 `ret[k] = _tm.from_dlpack(v)`。函数名虽带 `np`（历史遗留，早期用 numpy），但现在 `v` 实际是 `torch.Tensor`（见 `prepare_inputs` 里全是 `torch.IntTensor`/`torch.empty`），同样能被 `_tm.from_dlpack` 接受，因为 PyTorch 张量也实现了 DLPack 协议。

**出 C++ 的翻译官**：

[lmdeploy/turbomind/turbomind.py:57-65](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/turbomind/turbomind.py#L57-L65) —— `_tm_dict_to_torch_dict`：遍历 `_tm.TensorMap`，若遇到 `TYPE_UINT32` 先 `view` 成 `TYPE_INT32`，再 `torch.from_dlpack(v)` 变成 torch 张量。这段就是「C++ 用 uint32 存 token id、Python 用 int32 读」的适配点。

**在 `async_stream_infer` 中的成对使用**：

[lmdeploy/turbomind/turbomind.py:762](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/turbomind/turbomind.py#L762) —— `inputs = _np_dict_to_tm_dict(inputs)`，进 C++ 前的最后一步。
[lmdeploy/turbomind/turbomind.py:768-771](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/turbomind/turbomind.py#L768-L771) —— `forward` 拿到 `outputs` 后立即 `outputs = _tm_dict_to_torch_dict(outputs)` 变回 torch 张量，之后所有读取（`output_ids_buf`、logprobs、logits 等）都在 torch 张量上进行。

**为什么是「零拷贝」很重要**：因为 `outputs['output_ids']` 这块缓冲是 C++ 边写边更新的（每生成一个 token 就追加），Python 侧通过 `output_ids_buf[prev_len:seq_len]` 切片读取的正是这块共享内存的最新内容。若中间发生拷贝，就需要额外的同步机制保证一致性；DLPack 共享让「C++ 写、Python 读」天然指向同一块显存，`shared_state.consume()` 提供的 `seq_len` 就是发布-订阅的「已写水位」。

#### 4.3.4 代码实践

**目标**：亲手验证 `torch.from_dlpack` 的零拷贝语义，理解 Python 与 C++（以 torch 模拟）如何共享一块内存。

**操作步骤**（纯 Python，无需 `_turbomind`，用 PyTorch 自身的双向 DLPack 模拟）：

1. 写一个最小脚本（**示例代码，非项目源码**）：

   ```python
   import torch
   # 模拟 C++ 侧产出一个张量
   cpp_buf = torch.zeros(8, dtype=torch.int32, device='cpu')
   # 模拟 _tm_dict_to_torch_dict：经 DLPack 拿到 Python 视图
   view = torch.from_dlpack(cpp_buf)   # 等价于 _tm 出方向
   # 在「Python 视图」上切片写入，再回到「C++ 缓冲」读取
   view[3:5] = torch.tensor([7, 8], dtype=torch.int32)
   print('cpp_buf =', cpp_buf.tolist())   # 期望看到位置 3、4 被改写
   print('same storage?', view.data_ptr() == cpp_buf.data_ptr())
   ```

2. 运行它，观察 `cpp_buf` 是否被 `view` 的写入修改、两个 `data_ptr()` 是否相同。

**需要观察的现象**：

- `cpp_buf` 在 `[3:5]` 位置变成了 `[7, 8]`——说明 `view` 与 `cpp_buf` 共享同一块内存，写一个等于写另一个。
- `data_ptr()` 完全相同——零拷贝的铁证。

**预期结果**：你直观体会到「`from_dlpack` 不复制数据，只共享指针」。把这一结论映射回 TurboMind：C++ 的 `output_ids_buf` 与 Python 拿到的 `outputs['output_ids']` 是同一块显存，所以流式切片能读到 C++ 实时追加的最新 token。

> 待本地验证：本实践只需 PyTorch，与 `_turbomind` 是否编译无关，应可直接跑通。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `_tm_dict_to_torch_dict` 里要对 `TYPE_UINT32` 做 `view(TYPE_INT32)`，而不是用某种转换函数？

**答案**：`uint32` 与 `int32` 都是 4 字节整数，仅最高位的解释不同。`view` 是零拷贝的位模式重解释，不分配新内存、不改数据，开销最小。TurboMind 在 C++ 侧用 `uint32` 存 token id（避免负数语义），而 PyTorch 与下游 Python 代码习惯 `int32`，用 `view` 是最高效的桥接。

**练习 2**：进 C++ 用 `_tm.from_dlpack`、出 C++ 用 `torch.from_dlpack`，为什么不统一用一个？

**答案**：`from_dlpack` 是各框架提供的「把外部 DLPack 张量吸纳成本框架张量」的构造器，方向是「外部 → 本框架」。进 C++ 时目标是 `_tm.Tensor`，故用 `_tm.from_dlpack`；出 C++ 时目标是 `torch.Tensor`，故用 `torch.from_dlpack`。命名空间不同反映的是「吸纳方」不同，二者底层共享同一个 DLPack 标准。

---

## 5. 综合实践

把三个模块串起来，做一次「全链路源码追踪」任务。

**任务**：模拟一条图文推理请求，从上层 `AsyncEngine` 一路追到 C++ forward，画出一张包含以下节点的数据流图（可用文字+箭头），并标注每一步发生在哪个文件、哪一行：

1. `AsyncEngine._build_turbomind`（[async_engine.py:185-195](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/core/async_engine.py#L185-L195)）调用 `TurboMind.from_pretrained`。
2. `TurboMind.__init__ → _from_hf`（[turbomind.py:211-275](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/turbomind/turbomind.py#L211-L275)）逐字段搬运配置、`_tm.TurboMind.create` 建 C++ 引擎、`ModelLoader.export` 灌权重。
3. `create_instance`（[turbomind.py:380-388](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/turbomind/turbomind.py#L380-L388)）拿到 `TurboMindInstance`。
4. 推理时 `async_stream_infer`：`prepare_inputs`（含 `input_meta` 的 MRoPE 字段）→ `_get_generation_config` → `_np_dict_to_tm_dict`（DLPack 进）→ `model_inst.forward` → `_tm_dict_to_torch_dict`（DLPack 出）→ `while` 循环增量切片 `yield EngineOutput`。

**进阶**（可选）：在图中用不同颜色标出三类边界——

- **配置边界**：用户面 `TurbomindEngineConfig` / `GenerationConfig` ↔ C++ 面 `_tm.EngineConfig` / `_tm.GenerationConfig`（4.1 与 4.2 的字段搬运）。
- **张量边界**：`torch.Tensor` ↔ `_tm.Tensor`（4.3 的 DLPack 互转）。
- **线程边界**：C++ 信号线程 ↔ Python asyncio 协程（`StreamingSemaphore`，4.2 的流式同步）。

**预期产出**：一张清晰的数据流图 + 三类边界的说明。完成后，你应当能用一句话总结 TurboMind Python 包装的核心职责：「**它是一个翻译层——把用户面的配置与张量翻译成 C++ 能懂的 `_tm` 对象，把 C++ 流式产出的 token 翻译回 Python 的 `EngineOutput`，全程靠 DLPack 零拷贝与信号量跨线程同步。**」

## 6. 本讲小结

- `TurboMind` 是 C++ 引擎的 Python 门面，核心构造链是 `from_pretrained → __init__ → _from_hf`（解析 HF 配置 + 逐字段搬运成 `_tm.EngineConfig` + `_tm.TurboMind.create` 建引擎 + `ModelLoader` 灌权重）→ `_process_weights` / `_create_engine` 定稿，对外用 `create_instance` 发句柄。
- 用户面 `TurbomindEngineConfig` 与 C++ 面 `_tm.EngineConfig` 是「同义异型」的一对，靠 `_from_hf` 里逐字段赋值搬运；`GenerationConfig` 与 `_tm.GenerationConfig` 同理，靠 `_get_generation_config` 搬运，并处理 stop/bad words 打包与 logprobs 截断。
- `TurboMindInstance` 是轻量推理句柄，核心方法 `async_stream_infer` 完成「准备输入 → 翻译配置 → 进 C++ forward → 流式增量切片 yield」四步，靠 `StreamingSemaphore` 把 C++ 信号线程的产出安全喂回 Python 协程。
- `prepare_inputs` 负责把 `input_ids`、多模态 embedding、以及 `input_meta` 里的 MRoPE 三维位置编码组装成输入字典；`input_meta` 当前只承载 `mrope_position_ids` 与 `mrope_position_delta`，由多模态前端（如 `vl/model/qwen2.py`）算好塞入，纯文本推理时为 `None`。
- DLPack 互转是 Python 与 C++ 的张量翻译官：`_np_dict_to_tm_dict`（`_tm.from_dlpack`）进 C++、`_tm_dict_to_torch_dict`（`torch.from_dlpack`，含 `uint32→int32` 的 `view`）出 C++，全程零拷贝共享同一块显存，是流式切片能读到 C++ 实时追加 token 的基础。
- 错误码映射 `errcode_map` 把 C++ 整数 status（如 6=超长、7=完成、8=取消）翻译成用户面 `ResponseType`，是 TurboMind 对外暴露错误的统一出口。

## 7. 下一步学习建议

- **u6-l3《模型转换 converter 与权重格式》**：本讲只点到 `_from_hf` 调 `get_tm_config` 与 `ModelLoader.export`，下一讲深入 `converter.py` / `loader.py` / `weight_format.py`，看 HF 权重如何映射重排成 TurboMind 权重目录。
- **u6-l4《TurboMind 模型构建器 builders》**：本讲的 `TextModel`（`text_model.py`）只是抽象基类，真正的「逐层描述权重布局」发生在 `turbomind/builders/`（attention/ffn/moe/mla），下一讲拆解这些 builder 如何对接 C++。
- **对比阅读 u4-l3《引擎实例与流式推理》**：把 PyTorch 侧的 `EngineInstance.async_stream_infer` 与本讲的 `TurboMindInstance.async_stream_infer` 对照，体会「同步外观+异步内核」与「C++ 信号线程驱动 asyncio」两种流式实现哲学的异同。
- **延伸阅读 `src/turbomind/engine/request.h`**：`errcode_map` 注释指向的 C++ 头文件，对照看一遍 `struct Request` 的状态码定义，能彻底搞清 status 整数与 `ResponseType` 的对应关系。
