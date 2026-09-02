# u4-l2 VllmColocateWorkerExtension：vLLM 集成与设备 UUID 寻址

## 1. 本讲目标

上一讲（u4-l1）我们精读了 `worker.py` 中不依赖 vLLM 的部分：`FlattenedTensorMetadata`、`_extract_weights` 和模块级的 `update_weights_from_ipc` 状态机。当时我们刻意留下了两个「接缝」：

- `run` 和 `post_hook` 这两个回调在生产环境里到底做什么？
- `zmq_handle` 这个地址从哪来、worker 又怎么知道该连哪一个？

本讲就补上这两条缝。读完本讲，你应该能够：

1. 说清「PS 发起更新 → HTTP POST `/collective_rpc` → vLLM 各 worker 进程执行扩展方法」这条控制面调用链的每一环。
2. 掌握 `_device_uuid` 在 CUDA / NPU / XPU 三种平台上各自的生成规则，并解释它为什么必须与 PS 侧 `_get_physical_gpu_id` 的输出**逐字符相同**。
3. 理解 `_load_weights` 对主模型与 MTP drafter（投机解码草稿模型）的双路装载，以及 `_post_hook` 里 `process_weights_after_loading` 的后处理职责。

## 2. 前置知识

### 2.1 vLLM 的进程结构

vLLM 是目前最主流的开源 LLM 推理引擎之一。一次部署里通常有两类进程：

- **API server 进程**：对外提供 HTTP 服务（如 OpenAI 兼容接口），负责接收请求、调度。
- **worker 进程**：每个 GPU 一个，持有真正的模型权重，执行前向计算。张量并行（TP）场景下，8 卡就有 8 个 worker 进程，各自持有八分之一权重。

**collective_rpc**（集体远程过程调用）是 vLLM 提供的一个特殊 HTTP 端点：外部向它 POST 一个 `{"method": ..., "args": ...}`，vLLM 会把这次方法调用**广播到本实例的所有 worker 进程**上执行，并汇总结果。它相当于给外部开了一扇「直接指挥每个 worker」的窗。README 明确要求 vLLM 版本包含该端点（v0.10.2 完整测试通过），见 [README.md:102](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/README.md#L102)。

### 2.2 worker extension（worker 扩展）

vLLM 允许通过启动参数 `--worker-extension-cls` 传入一个**全限定类名**，vLLM 会在构造 worker 时加载这个类，并把它的方法**注入** worker 类，于是这些方法就能被 `collective_rpc` 按名字调用。checkpoint-engine 的 `checkpoint_engine.worker.VllmColocateWorkerExtension` 就是这样一块「插件」。

注意这个类有两个刻意的设计：

- **不定义 `__init__`**，也不继承任何基类——它靠「鸭子类型」直接使用宿主 worker 的属性（`self.device`、`self.local_rank`、`self.model_runner`、`self.model_config`）。宿主提供什么，它就用什么，因此对 vLLM V0/V1 两代 worker 都能用（这一点写在类文档字符串里）。
- **vLLM 的 import 全部延迟到方法内部**（`from vllm.platforms import ...` 等），所以没装 vLLM 的机器也能 `import checkpoint_engine.worker`——这正是上一讲能在纯 CPU 上测试状态机的前提。

### 2.3 设备 UUID 与抽象 UDS 回顾

上一讲和 u3-l6 已建立的事实，本讲直接使用：

- PS 每个进程 `bind` 一个 ZMQ REQ socket，地址形如 `ipc://@checkpoint-engine-<设备UUID>-<计数器>.sock`，走 Linux **抽象 Unix domain socket**（不落文件、主机内有效）。
- worker 端 REP socket `connect` 到「属于自己那张卡」的地址。
- **设备 UUID 就是把 PS 进程和同一张卡上的 vLLM worker 进程配对的钥匙**。

### 2.4 MTP 与 drafter（草稿模型）

**投机解码（speculative decoding）** 用一个小而快的「草稿模型」先猜若干个 token，再由大模型一次性验证，从而加速推理。**MTP（Multi-Token Prediction）** 是一种把草稿头与主模型一起训练的方案，DeepSeek-V3、Kimi-K2 都采用。在 vLLM 中，草稿模型挂在 `model_runner.drafter` 上；**它的权重同样来自 checkpoint**，所以热更新权重时主模型和 drafter 必须一起更新，否则两者版本不一致，投机解码会基于过期权重猜 token。

### 2.5 `cached_property`

Python 标准库 `functools.cached_property`：第一次访问属性时执行函数并把结果缓存为实例属性，之后访问直接返回缓存。适合「算一次就够、结果不变」的值——设备 UUID 正是如此。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
| --- | --- | --- |
| [checkpoint_engine/worker.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py) | 推理引擎侧消费端 | `VllmColocateWorkerExtension` 全类：`_device_uuid`、`_zmq_ctx`、`update_weights_from_ipc` 及内部闭包 `_load_weights`、`_post_hook` |
| [checkpoint_engine/ps.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py) | PS 服务端 | `_get_physical_gpu_id`（UUID 的 PS 侧定义）、`_bind_zmq_socket`（地址表生成）、`gather_metas` 中 `_global_device_uuids` 的收集 |
| [checkpoint_engine/api.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/api.py) | HTTP 客户端/服务层 | `request_inference_to_update`：POST `/collective_rpc` 的请求体形状 |
| [examples/update.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py) | 示例驱动脚本 | `req_inference`：组首切片逻辑（谁负责发 HTTP、发哪几条地址） |
| [checkpoint_engine/device_utils.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/device_utils.py) | 硬件抽象 | `npu_generate_uuid`：NPU 平台的 UUID 合成算法 |
| [tests/test_update.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_update.py) | 端到端测试 | 用自定义 `run`/`post_hook` 驱动状态机的先例 |

## 4. 核心概念与源码讲解

### 4.1 模块一：VllmColocateWorkerExtension 与 collective_rpc 调用链

#### 4.1.1 概念说明

`VllmColocateWorkerExtension` 是 checkpoint-engine 与 vLLM 之间的**适配器**：它把「vLLM worker 能被 RPC 调用一个方法」这件事，翻译成「worker 进入上一讲精读的 ZMQ REP 状态机」。它自己不搬任何字节，只做三件事：找到自己的 ZMQ 地址、把 vLLM 的模型装载 API 包装成 `run` 回调、把 vLLM 的权重后处理 API 包装成 `post_hook` 回调。

#### 4.1.2 核心流程

一次 Broadcast 更新中，控制面的完整接力（数据面是 ZMQ + 设备 IPC，见 u1-l4）：

```text
PS 侧（每个 rank 各自执行 _update_per_bucket）
  ├─ _bind_zmq_socket()
  │    ├─ bind  ipc://@checkpoint-engine-<自己的设备UUID>-<计数器>.sock
  │    └─ 返回 socket_paths = [(uuid₁, addr₁), (uuid₂, addr₂), ...]   ← 覆盖本集群全部 GPU 的 UUID
  ├─ 起线程执行 req_func(socket_paths)          ← req_func 由调用方（examples/update.py）注入
  │    └─ 组首 rank（rank == rank//P*P）：
  │         request_inference_to_update(f"{endpoint}/collective_rpc",
  │                                       dict(socket_paths[src : src+P]))   ← 只取本实例 P 条
  │              └─ HTTP POST {"method": "update_weights_from_ipc",
  │                            "args": [<uuid→addr 字典>], "timeout": 300}
  ▼
vLLM API server 收到 /collective_rpc
  └─ 广播到本实例全部 worker 进程
       └─ worker 类已被 --worker-extension-cls 注入扩展方法
            └─ VllmColocateWorkerExtension.update_weights_from_ipc(zmq_handles)
                 ├─ npu/xpu 下补设 self.device
                 ├─ addr = zmq_handles[self._device_uuid]     ← 用设备 UUID 查表
                 └─ 调模块级 update_weights_from_ipc(ctx, addr, ..., run, post_hook)
                      └─ （进入 u4-l1 的 REP 状态机）
```

两个容易忽略的细节：

- **为什么只发本实例的 P 条地址？** 抽象 UDS 是**主机内**的命名空间，别的机器上 GPU 的地址在本机 `connect` 不通。多机部署时，每个节点的组首各自把本节点的切片发给本节点的 vLLM 实例，拼起来才覆盖全集群。
- **为什么地址里带计数器？** 每轮 update 计数器自增，反复更新时每轮用全新地址，避免上一轮残留的 socket 状态互相干扰。计数器靠一次 all_reduce（取负捎带、全局取 max）在所有 rank 间对齐——这是 u3-l4/u3-l6 已经讲过的技巧，本讲不重复推导。

#### 4.1.3 源码精读

扩展类的声明与文档字符串，说明了「注入 + collective_rpc 可调用 + 兼容 V0/V1」这一定位：

> [checkpoint_engine/worker.py:134-148](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L134-L148)
> 类文档字符串明确写道：方法会被注入 vLLM worker 类、可从 `collective_rpc` API 调用、全限定类名 `checkpoint_engine.worker.VllmColocateWorkerExtension` 应作为 `worker_extension_cls` 参数传入。

PS 侧的地址表生成，键就是各 rank 的设备 UUID：

> [checkpoint_engine/ps.py:622-630](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L622-L630)
> `_bind_zmq_socket` 为 `_global_device_uuids` 里的**每个** UUID 生成一个地址（`_global_device_uuids` 在首次 `gather_metas` 时收集一次，此后不变，见 [ps.py:497-519](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L497-L519)）；但只 `bind` 自己 UUID 对应的那一个，随后计数器自增。

调用方注入的 `req_func` 如何被消费：

> [checkpoint_engine/ps.py:842-849](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L842-L849)
> `_update_per_bucket` 把 `socket_paths` 交给后台线程执行 `req_func`，同时主线程立刻 `socket.send_pyobj(handle)` 发出 IPC 句柄——控制面（通知 worker 来连）与数据面（交出句柄）并行开工。

组首切片与 HTTP 请求体：

> [examples/update.py:77-93](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py#L77-L93)
> `req_inference` 计算 `src = rank // P * P`（即本 vLLM 实例的首个 rank），只有 `rank == src` 的进程发 HTTP，且只取 `socket_paths[src : src + P]` 这一段。

> [checkpoint_engine/api.py:34-42](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/api.py#L34-L42)
> `request_inference_to_update` POST 的 JSON 体就是 `{"method": "update_weights_from_ipc", "args": [socket_paths], "timeout": ...}`——`method` 的值恰好是扩展类的方法名，这就是 RPC 按名字分发到注入方法的全部机制。

启动命令中的注入点：

> [README.md:123-130](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/README.md#L123-L130)
> `--worker-extension-cls checkpoint_engine.worker.VllmColocateWorkerExtension` 配合 `--load-format dummy`：前者让 worker 拥有 `update_weights_from_ipc` 方法，后者跳过磁盘权重加载（权重完全由 checkpoint-engine 送来）。

至于 vLLM 内部如何把扩展类混入 worker 类（继承、包装还是其它方式），属于 vLLM 侧实现，本仓库只依赖「方法可被 `collective_rpc` 调用」这一契约（vLLM 侧接口对应 [PR #24295](https://github.com/vllm-project/vllm/pull/24295)，README 致谢部分有提及），具体混合方式**待确认**（需阅读 vLLM 源码）。

#### 4.1.4 代码实践

**实践目标**：不运行任何 GPU 代码，仅靠 grep 把 4.1.2 的调用链在源码里逐环「点名」，并写出请求体的真实形状。

**操作步骤**：

1. 在仓库根目录执行（依次验证每一环的存在）：
   - `grep -n "req_func" checkpoint_engine/ps.py` → 确认 `req_func` 是 `update` 的参数、在 `_update_per_bucket` 里被线程调用。
   - `grep -n "collective_rpc" examples/update.py checkpoint_engine/api.py` → 确认 URL 拼接位置。
   - `grep -n "worker-extension-cls" README.md` → 确认启动参数。
2. 用一行 Python 打印请求体形状（纯字符串操作，无需任何依赖）：

   ```python
   # 示例代码：仅演示 JSON 形状，不联网
   import json
   socket_paths = {f"GPU-FAKE-{i}": f"ipc://@checkpoint-engine-GPU-FAKE-{i}-0.sock" for i in range(8)}
   print(json.dumps({"method": "update_weights_from_ipc", "args": [socket_paths], "timeout": 300.0}))
   ```

**需要观察的现象**：grep 能在三个文件里各命中一处关键行；打印出的 JSON 与 [api.py:34-42](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/api.py#L34-L42) 的构造逐字段对应。

**预期结果**：确认「PS 不直接认识 vLLM，它只调用注入的 `req_func`；HTTP 端点和 method 名是两侧唯一的耦合点」。步骤 2 的输出可直接与源码对照，无「待本地验证」项。

#### 4.1.5 小练习与答案

**练习 1**：如果启动 vLLM 时忘记加 `--worker-extension-cls`，调用链会在哪一环断掉？

**答案**：HTTP 那一环。`/collective_rpc` 收到 `method="update_weights_from_ipc"` 时，worker 类上没有这个方法，RPC 执行失败并返回错误；PS 侧 `httpx` 的 `resp.raise_for_status()`（[api.py:43](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/api.py#L43)）抛出异常，`req_func` 线程崩溃，没有任何 worker 会来 `connect` ZMQ 地址，更新挂起直至超时。

**练习 2**：`socket_paths` 列表覆盖全集群所有 GPU 的 UUID，为什么每个组首只发自己那 P 条？

**答案**：抽象 UDS 地址只在**本主机**的内核命名空间里有效，其它节点 GPU 的地址在本机 `connect` 不通；同时一个 vLLM 实例的 `collective_rpc` 只广播到**本实例**的 worker。所以必须按实例切片（`socket_paths[src:src+P]`），多机时各节点组首各自发、拼起来正好覆盖全集群，不多不少。

**练习 3**：扩展类为什么不定义 `__init__`、不继承 vLLM 的任何基类？

**答案**：它被注入 vLLM worker 后，`self` 就是宿主 worker 实例，`self.device`、`self.model_runner` 等属性天然存在。不定义 `__init__` 可以避免干扰宿主的初始化；不继承基类（鸭子类型）则不挑宿主版本——类文档字符串说这正是兼容 vLLM V0/V1 的原因。

### 4.2 模块二：`_device_uuid`——三种平台的生成规则与寻址对齐

#### 4.2.1 概念说明

`_device_uuid` 是一个 `cached_property`，回答一个问题：**「我所在的这张物理卡，在集群范围内的唯一名字是什么？」** 它有两个使用者：

1. PS 侧：`_get_physical_gpu_id` 生成 UUID，用于 `bind` 自己的 ZMQ 地址，并在 `gather_metas` 时广播出去（[ps.py:487](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L487)）。
2. worker 侧：`_device_uuid` 生成 UUID，用作 `zmq_handles` 字典的查询键（[worker.py:227](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L227)）。

**两侧的生成规则必须逐字符一致**，否则 `zmq_handles[self._device_uuid]` 直接 `KeyError`，整个更新失败。这不是隐式约定——XPU 分支里有一行注释明说了这一点。

#### 4.2.2 核心流程

三种平台的对照表（两侧对齐是硬约束）：

| 平台 | worker 侧生成（`_device_uuid`） | PS 侧生成（`_get_physical_gpu_id`） | 对齐方式 |
| --- | --- | --- | --- |
| CUDA | `current_platform.get_device_uuid(index)`（vLLM 平台层提供） | `f"GPU-{props.uuid!s}"`，props 来自 `torch.cuda.get_device_properties(idx)` | vLLM 的实现返回带 `GPU-` 前缀的 UUID 串，与 PS 拼接格式一致（待本地验证） |
| NPU | `f"NPU-{npu_generate_uuid()}"` | `f"NPU-{npu_generate_uuid()}"`（[ps.py:53-54](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L53-L54)） | **调用同一个函数** `device_utils.npu_generate_uuid`，天然一致 |
| XPU | `f"GPU-{torch.xpu.get_device_properties(index).uuid!s}"` | `f"GPU-{props.uuid!s}"`，props 来自 `torch.xpu.get_device_properties(idx)` | 两侧同样的「`GPU-` + uuid 字符串化」拼接，源码注释显式声明必须一致 |

NPU 的 UUID 合成算法值得单独一说，因为 PS 进程和 vLLM worker 是**不同进程、不同 pid**，却要对上同一张卡：

```伪代码
npu_generate_uuid():
    对每块 NPU (npu_id = 0..7):
        运行 `npu-smi info -t proc-mem -i npu_id`，查看哪些进程占用
        若本进程 pid 出现在输出中:
            chip_count = 输出中的 "Chip Count"（A3 服务器一块 NPU 两个芯片）
            chip_id    = pid 首次出现位置之后的 "Chip ID"
            返回 f"{本机IP}-{npu_id * chip_count + chip_id}"
    报错：本进程不在任何 NPU 上
```

关键洞察：这个 UUID 标识的是**物理芯片**（IP + 芯片线性编号），不是进程。PS 进程和 colocated 的 vLLM worker 进程虽然 pid 不同，但因为占用**同一块芯片**，`npu-smi` 反查会得到相同的 `npu_id * chip_count + chip_id`，于是两侧算出同一个键——这就是「colocated 部署下跨进程寻址」能成立的原理。

#### 4.2.3 源码精读

worker 侧的完整分支实现：

> [checkpoint_engine/worker.py:150-162](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L150-L162)
> `cached_property _device_uuid` 按 `current_platform.device_type` 三路分发：CUDA 走 vLLM 平台层的 `get_device_uuid(self.device.index)`；NPU 拼 `NPU-` 前缀加 `npu_generate_uuid()`；XPU 拼 `GPU-` 前缀加 `torch.xpu.get_device_properties(...).uuid`。XPU 分支的注释原话是「Must match ps.py::_get_physical_gpu_id ("GPU-\<uuid\>") for the ZMQ key to resolve」——把「两侧必须对齐」写进了代码。不认识的平台直接 `ValueError`。

延迟导入 vLLM 平台层（也解释了为什么无 vLLM 机器能 import 本模块）：

> [checkpoint_engine/worker.py:152](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L152)
> `from vllm.platforms import current_platform` 写在函数体内而非模块顶部。

PS 侧的对偶实现：

> [checkpoint_engine/ps.py:51-65](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L51-L65)
> `_get_physical_gpu_id`：NPU 走 `NPU-{npu_generate_uuid()}`；CUDA 和 XPU 走同一条路——`device_module.get_device_properties(idx).uuid`（torch≥2.9 才在 XPU 属性里暴露 `uuid`），拼成 `GPU-{uuid!s}`；属性里没有 `uuid` 就报「需要更新的 PyTorch」。

NPU UUID 的合成算法本体：

> [checkpoint_engine/device_utils.py:29-47](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/device_utils.py#L29-L47)
> `npu_generate_uuid` 用子进程跑 `npu-smi info -t proc-mem -i <npu_id>`，在本进程 pid 命中后解析 `Chip Count` 与 `Chip ID`，返回 `f"{get_ip()}-{npu_id * chip_count + chip_id}"`；A3 服务器「一块 NPU 两个芯片」的情况由 `npu_id * chip_count + chip_id` 的线性编号消化。

UUID 在 PS 侧的两次消费：

> [checkpoint_engine/ps.py:250](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L250)
> 初始化时 `self._device_uuid = _get_physical_gpu_id(self.device_manager, device_index)`——之后 `bind` 的地址用它（[ps.py:628](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L628)）。

> [checkpoint_engine/ps.py:476-491](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L476-L491)
> `gather_metas` 把 `device_uuid=self._device_uuid` 塞进 `DataToGather` 随 `all_gather_object` 广播——控制面只传这个字符串，worker 侧的字典键就来自这里（收集逻辑见 [ps.py:497-519](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L497-L519)，仅在首次 gather 时填充）。

worker 侧的查表动作：

> [checkpoint_engine/worker.py:225-231](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L225-L231)
> `update_weights_from_ipc(self._zmq_ctx, zmq_handles[self._device_uuid], ...)`——字典查表即配对；键不匹配此处抛 `KeyError`，这是 UUID 失配的第一现场。

方法文档字符串本身就记录了三种平台的键格式（[worker.py:180-184](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L180-L184)），可作为排查时的第一参考。

关于 CUDA 分支：worker 把生成委托给 vLLM 的 `current_platform.get_device_uuid()`，PS 侧则是自己拼 `f"GPU-{props.uuid!s}"`。两者能对上，前提是 vLLM 的实现返回带 `GPU-` 前缀的格式；本仓库源码未内联 vLLM 实现，**该格式的确切形态待本地验证**（装好 vLLM 后 `python -c "from vllm.platforms import current_platform; print(current_platform.get_device_uuid(0))"` 即可一行确认）。

#### 4.2.4 代码实践

**实践目标**：在**纯 CPU、不装 vLLM** 的机器上，用桩模块（stub）驱动真实的 `_device_uuid` 代码，验证三种平台的输出格式，并亲手复现「worker 与 PS 对不上键就 KeyError」的失配场景。

**操作步骤**：

1. 在仓库根目录新建 `u4l2_uuid_lab.py`（示例代码，实践后可删除）：

   ```python
   # 示例代码：用假 vllm.platforms 驱动真实的 _device_uuid 分支逻辑
   import sys
   import types
   from types import SimpleNamespace

   import torch

   # 1) 伪造 vllm.platforms，拦截 _device_uuid 里的延迟导入
   fake_vllm = types.ModuleType("vllm")
   fake_platforms = types.ModuleType("vllm.platforms")

   class FakeCudaPlatform:
       device_type = "cuda"
       @staticmethod
       def get_device_uuid(index):
           return f"GPU-FAKE-UUID-{index}"

   fake_platforms.current_platform = FakeCudaPlatform
   fake_vllm.platforms = fake_platforms
   sys.modules["vllm"] = fake_vllm
   sys.modules["vllm.platforms"] = fake_platforms

   import checkpoint_engine.worker as w
   from checkpoint_engine.ps import _get_physical_gpu_id

   # 2) CUDA 分支：宿主属性 self.device 由我们手工补上（真实场景由 vLLM worker 提供）
   ext = w.VllmColocateWorkerExtension()      # 无 __init__，可直接实例化
   ext.device = torch.device("cuda", 3)
   print("cuda :", ext._device_uuid)          # 期望 GPU-FAKE-UUID-3

   # 3) cached_property 缓存：改掉桩函数后再读，值不应变化
   FakeCudaPlatform.get_device_uuid = staticmethod(lambda i: "GPU-CHANGED")
   print("cache:", ext._device_uuid)          # 期望仍是 GPU-FAKE-UUID-3

   # 4) NPU 分支：monkeypatch worker 命名空间里的 npu_generate_uuid（worker.py 顶部 from 导入）
   w.npu_generate_uuid = lambda: "10.0.0.1-5"
   fake_platforms.current_platform = SimpleNamespace(device_type="npu")
   ext2 = w.VllmColocateWorkerExtension()
   print("npu  :", ext2._device_uuid)         # 期望 NPU-10.0.0.1-5

   # 5) XPU 分支 + 与 PS 侧对齐验证：给 torch.xpu 注入桩属性
   torch.xpu = SimpleNamespace(get_device_properties=lambda i: SimpleNamespace(uuid="XYZ-9"))
   fake_platforms.current_platform = SimpleNamespace(device_type="xpu")
   ext3 = w.VllmColocateWorkerExtension()
   ext3.device = torch.device("xpu", 0)
   print("xpu  :", ext3._device_uuid)

   class FakeDM:                              # 冒充 DeviceManager，只提供 _get_physical_gpu_id 用到的两个属性
       device_type = "xpu"
       device_module = torch                  # 于是 get_device_properties 同样命中我们的桩
   print("ps   :", _get_physical_gpu_id(FakeDM(), 0))
   assert ext3._device_uuid == _get_physical_gpu_id(FakeDM(), 0), "worker 与 PS 的 UUID 键不一致!"

   # 6) 失配复现：PS 侧若生成的是另一种前缀，worker 查表立刻 KeyError
   zmq_handles = {"GPU-OTHER-0": "ipc://@x.sock"}
   try:
       zmq_handles[ext3._device_uuid]
   except KeyError as e:
       print("KeyError as expected:", e)
   ```

2. 运行 `python u4l2_uuid_lab.py`。

**需要观察的现象**：前 5 步打印出三种平台的 UUID 格式；第 5 步的断言通过（worker 与 PS 的 XPU 键完全相同）；第 6 步捕获 `KeyError`。

**预期结果**：

```text
cuda : GPU-FAKE-UUID-3
cache: GPU-FAKE-UUID-3
npu  : NPU-10.0.0.1-5
xpu  : GPU-XYZ-9
ps   : GPU-XYZ-9
KeyError as expected: 'GPU-XYZ-9'
```

注意事项：向 `torch` 模块注入 `xpu` 属性、以及 `torch.device("xpu", 0)` 在 CPU-only 构建的 torch 上是否可用**待本地验证**（`torch.device` 只是设备描述对象，通常无需真实硬件；若你的 torch 版本拒绝该设备类型，可把第 5 步的 `ext3.device` 换成任意 `torch.device("cuda", 0)`，只验证 `GPU-` 前缀拼接逻辑）。NPU 分支不会真正调用 `npu-smi`，因为我们替换的是 worker 命名空间里的函数引用。

#### 4.2.5 小练习与答案

**练习 1**：为什么 XPU 两侧都用 `GPU-` 前缀而不是 `XPU-`？

**答案**：因为 PS 侧的 `_get_physical_gpu_id` 对 CUDA 和 XPU 走的是**同一条代码路径**（都从 `get_device_properties(idx).uuid` 取值后拼 `GPU-` 前缀，见 [ps.py:56-63](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L56-L63)），XPU 是复用了 CUDA 的命名习惯；worker 侧只需保证与 PS 相同，前缀本身没有语义。这行注释（worker.py:159）正是防止后人「顺手改成 XPU-」而破坏对齐。

**练习 2**：NPU 上 PS 进程和 vLLM worker 进程 pid 不同，`npu_generate_uuid` 为什么能给出同一个键？

**答案**：该函数标识的是**物理芯片**而非进程：通过 `npu-smi` 反查「本 pid 占用哪块 NPU 哪个芯片」，最终键是 `f"{本机IP}-{npu_id * chip_count + chip_id}"`。colocated 部署下，PS 进程与 vLLM worker 占用同一块芯片，反查结果相同，键自然相同。

**练习 3**：把 `_device_uuid` 从 `cached_property` 改成普通 `@property`，功能上会坏吗？有什么代价？

**答案**：功能不会坏（UUID 在进程生命周期内不变），代价是每次更新都重算：CUDA/XPU 只是再读一次设备属性，但 NPU 分支要**再跑一遍 `npu-smi` 子进程**，而 `update` 可能被高频调用。同理 `_zmq_ctx`（[worker.py:164-166](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L164-L166)）也缓存 ZMQ Context 供多次更新复用。

### 4.3 模块三：`_load_weights`——主模型与 MTP drafter 的双路装载

#### 4.3.1 概念说明

`_load_weights` 是传给状态机 `run` 参数的闭包，在**每个桶**装载时被调用一次（u4-l1 的 list 分支）。它做的事看起来只有一行，却要同时伺候两个模型：主模型和 drafter（若开启 MTP/投机解码）。它的输入是一份「全量权重清单」——`_extract_weights` 从共享 IPC buffer 里零拷贝切出的 `(名字, 张量)` 列表，谁认识哪个名字，谁自己挑。

#### 4.3.2 核心流程

```伪代码
_load_weights(weights):                     # weights: [(名字, 张量), ...]，来自本桶
    主模型.load_weights(weights)              # vLLM 标准 API：按名字匹配自身参数
    若 model_runner.drafter 存在 且 drafter.model 存在:
        drafter.model.load_weights(weights=weights)   # MTP 草稿模型用同一份全量清单再装一次
```

为什么同一份全量清单能喂两个模型？vLLM 各模型的 `load_weights` 按 `(名字, 张量)` 匹配自己的参数，模型不认识的名字会被忽略（这是 vLLM `load_weights` 的通用约定）。所以主模型取走 `model.layers.*`，MTP 头取走 `model.layers.61.*`（DeepSeek/Kimi 风格的 MTP 层编号），互不干扰、无需在 checkpoint-engine 侧做任何切分。

#### 4.3.3 源码精读

设备兜底与两个闭包的定义：

> [checkpoint_engine/worker.py:197-212](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L197-L212)
> 先处理「vllm-ascend / vLLM XPU 不初始化 `self.device`」的情况：按 `self.local_rank` 补 `npu:<local_rank>` 或 `xpu:<local_rank>`，随后断言设备非空。`_load_weights` 先调 `self.model_runner.model.load_weights(weights)`；再用**两个防御性 `getattr(..., None)`** 判断 drafter 是否存在且带 `.model`，满足则 `drafter.model.load_weights(weights=weights)`。

防御性检查的必要性：

- `drafter` 可能为 `None`——未开启投机解码时 `model_runner` 上根本没有草稿模型；
- 即使有 drafter，也不是所有 drafter 实现都把权重装在 `.model` 属性上。

直接写 `self.model_runner.drafter.model` 在这两种情况下都会 `AttributeError`，而这个异常按 u4-l1 的规则会被回传 PS、经全体投票后**让整个集群的更新失败**——所以这里必须宽容。

`run` 回调在状态机中的调用位置（承上启下）：

> [checkpoint_engine/worker.py:108-117](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L108-L117)
> 模块级状态机收到 list 型 payload 后执行 `run(_extract_weights(payload, buffer))`，随后 `synchronize` 并回 ACK——即 `_load_weights` 是**每桶一次**、在双缓冲的背压控制下被驱动的。

测试里对同一接口的用法先例：

> [tests/test_update.py:62-76](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_update.py#L62-L76)
> 端到端测试正是用自定义的 `error_run`（故意在 rank 0 抛错）替换 `_load_weights` 的位置，验证错误传播链——生产代码与测试使用**同一个插槽**，这就是「回调注入」设计的直接收益。

#### 4.3.4 代码实践

**实践目标**：不运行 vLLM，通过「替身模型 + 真实闭包逻辑」验证 drafter 双路装载的触发条件，并对照测试代码理解 `run` 插槽。

**操作步骤**：

1. 阅读并抄录 [worker.py:204-212](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L204-L212) 的 `_load_weights` 逻辑（闭包无法从外部直接拿到，所以用一个等价替身函数复刻它的分支，标注「示例代码」）：

   ```python
   # 示例代码：复刻 _load_weights 的双路分支，观察三种 drafter 形态下的行为
   from types import SimpleNamespace

   calls = []

   def make_fake_model(tag):
       return SimpleNamespace(load_weights=lambda weights: calls.append((tag, len(weights))))

   def load_weights_like_worker(model_runner, weights):
       model_runner.model.load_weights(weights)
       if getattr(model_runner, "drafter", None) is not None and \
          getattr(model_runner.drafter, "model", None) is not None:
           model_runner.drafter.model.load_weights(weights=weights)

   weights = [("model.layers.0.w", None), ("model.layers.61.proj", None)]  # 长度 2，内容无所谓

   # 情形 A：未开投机解码
   load_weights_like_worker(SimpleNamespace(model=make_fake_model("main"), drafter=None), weights)
   # 情形 B：drafter 存在但无 .model
   load_weights_like_worker(
       SimpleNamespace(model=make_fake_model("main"), drafter=SimpleNamespace()), weights)
   # 情形 C：MTP 开启
   load_weights_like_worker(
       SimpleNamespace(model=make_fake_model("main"),
                       drafter=SimpleNamespace(model=make_fake_model("drafter"))), weights)
   print(calls)
   ```

2. 运行它（无第三方依赖，任何 Python ≥ 3.9 均可）。
3. 打开 [tests/test_update.py:72-76](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_update.py#L72-L76)，确认 `error_run` 占据的正是 `_load_weights` 的插槽。

**需要观察的现象**：`calls` 里一共 4 条记录——情形 A、B 各只有 `("main", 2)`，情形 C 多出 `("drafter", 2)`；两个模型收到的都是**同一份长度 2 的全量清单**。

**预期结果**：

```text
[('main', 2), ('main', 2), ('main', 2), ('drafter', 2)]
```

情形 A/B 证明防御性 `getattr` 让无 drafter 的部署安全跳过；情形 C 证明 MTP 场景同一份清单被装两次。若把替身里的 `getattr` 换成直接属性访问，情形 B 会抛 `AttributeError`——对照 u4-l1「本地异常回传 PS、全体退出」的规则，感受这个防御的分量。本实践不依赖 GPU 与 vLLM，无需「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 drafter 装载失败（例如 MTP 权重名字对不上）不会在 worker 本地直接崩溃？

**答案**：会异常，但被模块级状态机的 `except Exception` 捕获（[worker.py:113-117](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L113-L117)），格式化成文本回传 PS；PS 经 `ret_code` 全体约减决定全集群统一下发异常退出。这是 u4-l1 讲过的「同生共死」设计——单 worker 失败不能让它独自挂掉而其它实例更新成功，否则集群权重版本分裂。

**练习 2**：如果把 `_load_weights` 里对 drafter 的调用删掉，推理会发生什么？

**答案**：主模型更新为新权重、drafter 仍是旧权重。投机解码会让「旧 drafter 猜 token、新主模型验证」，验证命中率骤降（两者隐状态分布不一致），轻则投机加速失效、退化为普通解码，重则输出质量受损。这正是该分支存在的原因。

**练习 3**：`_load_weights` 为什么写成闭包而不是模块级函数？

**答案**：它要捕获 `self`（宿主 worker 的 `model_runner`）。扩展方法被注入 vLLM worker 后 `self` 才存在；闭包是把「宿主状态」打包进回调、再传给与 vLLM 完全解耦的模块级状态机的最自然方式。`_post_hook` 同理。

### 4.4 模块四：`_post_hook`——`process_weights_after_loading` 后处理

#### 4.4.1 概念说明

很多模型在「原始权重就位」之后、能推理之前，还需要一道**后处理**：典型例子是量化——磁盘上的 FP8 权重加载后，量化方法可能要重算 scale、重排布局、做算子融合。vLLM 把这一步抽象成 `process_weights_after_loading(model, model_config, device)`（来自 `vllm.model_executor.model_loader.utils`），正常启动路径中由模型加载器自动调用。

问题在于：checkpoint-engine 的热更新**绕过了 vLLM 的正常加载流程**（它只是直接调用 `load_weights`），所以这道后处理必须由 checkpoint-engine **手动补上**——这就是 `_post_hook` 的全部职责。它与 u4-l1 里「第二个 `None`」绑定：**所有桶都装完后，才执行一次**。

#### 4.4.2 核心流程

```伪代码
_post_hook():                                  # 在收到第二个 None 时被状态机调用（worker.py:87-93）
    process_weights_after_loading(主模型, model_config, device)
    若 drafter 存在 且 drafter.model 存在:
        process_weights_after_loading(drafter.model, model_config, device)   # MTP 同样要后处理
```

时机约束是关键：

- **必须在所有桶之后**：后处理针对「最终权重」做全量加工（如按整张量算量化 scale），在半更新状态下执行会基于不完整权重算错；
- **只需一次**：每桶执行一遍既是重复劳动，又可能在「部分新部分旧」的中间态上得到错误结果；
- **放在 post_hook 而非 run 里**，正是让状态机把它推迟到收尾信号的机制设计。

#### 4.4.3 源码精读

> [checkpoint_engine/worker.py:194-195](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L194-L195)
> 方法开头延迟导入 `process_weights_after_loading` 与 `current_platform`——vLLM 依赖再次被挡在函数体内。

> [checkpoint_engine/worker.py:214-223](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L214-L223)
> `_post_hook` 对主模型调用 `process_weights_after_loading(self.model_runner.model, self.model_config, self.device)`；drafter 的判断与 `_load_weights` 完全同构（同样的两个防御性 `getattr`），满足则对 drafter 模型再做一遍。

> [checkpoint_engine/worker.py:87-93](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L87-L93)
> 状态机里 `released` 为真后再收到 `None` 才调用 `post_hook()`——回看 u4-l1 的状态图：第一个 `None` 释放 IPC 资源，**第二个 `None`** 执行本模块的后处理并回 ACK 结束循环。`post_hook=None` 时（如测试里传的简单函数之外的场景）这步只是被跳过。

> [README.md:155-161](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/README.md#L155-L161)
> 「FP8 quantization」一节说明 FP8 权重在 vLLM 中目前无法原生正确热更新，需要额外补丁——后处理时机与量化加工正是这条线索的源头，u6-l4 会专门分析该补丁。

`process_weights_after_loading` 在 vLLM 内部具体遍历哪些模块、触发哪些量化钩子，属于 vLLM 侧实现，本讲不展开（**待确认**，可在装有 vLLM 的环境阅读 `vllm/model_executor/model_loader/utils.py`）；对本仓库而言，只需把它理解为「vLLM 正常加载流程末尾必做、而热更新路径必须手动补上的那道工序」。

#### 4.4.4 代码实践

**实践目标**：通过纯阅读，确认「post_hook 只执行一次、且在所有桶之后」这条时序在两侧源码中都有据可查；并把 `_post_hook` 的执行点画进 u4-l1 的状态机图。

**操作步骤**：

1. 在 [worker.py:83-123](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L83-L123) 中找出三处证据：
   - `post_hook()` 的调用只在哪个分支出现？（第 89-90 行附近，`released` 已为真的分支）
   - `run(...)`（即 `_load_weights`）在哪类 payload 下被反复调用？（第 108-110 行，list 分支）
   - 两者之间隔着哪几步？（第一个 `None` → 释放 IPC → `gc`/`ipc_collect`/`empty_cache` → ACK）
2. 在 [ps.py:842-920](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L842-L920) 的 `_update_per_bucket` 尾部找到 PS 侧发出「第一个 None、第二个 None」的顺序，确认第二个 None 在所有资源清理之后。
3. 把上述时序补进你在 u4-l1 画的状态机图：`list×N → None(释放) → 清理 → None(post_hook) → ACK → 结束`。

**需要观察的现象**：`post_hook` 的调用点在循环体内只有一处，且被 `released` 标志保护——不可能在桶装载期间被触发；PS 侧两次 `None` 之间隔着释放 views/base、`gc`、`ipc_collect`、`empty_cache` 等清理动作。

**预期结果**：得出结论「后处理被机制性地推迟到全部桶装载完毕、显存回收完成之后，恰好满足 `process_weights_after_loading` 对最终权重全量加工的前提」。本实践为源码阅读型，无需运行（无「待本地验证」项）。若本地装有 vLLM，可加做一步：阅读 `vllm/model_executor/model_loader/utils.py` 中该函数的实现并记录它调用了哪些钩子（**待本地验证**）。

#### 4.4.5 小练习与答案

**练习 1**：如果把 `process_weights_after_loading` 从 `_post_hook` 挪到 `_load_weights` 末尾（每桶调一次），会有什么问题？

**答案**：三个问题：① 在只有部分桶就位时执行，量化类后处理可能基于不完整/混合版本的权重计算出错误结果；② 每桶一遍全模型遍历，开销乘以桶数；③ 违背 vLLM 语义——该函数本来就设计为加载流程「末尾执行一次」。

**练习 2**：为什么 drafter 也要做一遍 `process_weights_after_loading`？

**答案**：drafter 同样是被 `load_weights` 更新的模型，若它含量化层或需要权重加工的模块，跳过后处理会让它停留在「原始加载态」；主模型被后处理而 drafter 没有，两者状态不一致，与练习中「忘装 drafter」是同类隐患。判断条件与 `_load_weights` 保持同构，两处代码互为镜像。

**练习 3**：`_post_hook` 执行完，状态机还做了什么才退出循环？

**答案**：`device_module.synchronize()` 等后处理在设备上真正完成，然后 `socket.send(b"")` 回最后一个 ACK、`break` 退出循环，随后 `finally` 块关闭 socket 并做兜底清理（[worker.py:91-93](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L91-L93) 与 [worker.py:125-131](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L125-L131)）。PS 收到 ACK 后继续自己的收尾（barrier、关 socket 等，见 u3-l4）。

## 5. 综合实践

**任务**：写一个「迷你控制面演习」脚本，把本讲三个模块串起来——模拟 PS 生成的地址表、组首的 HTTP 请求体、`collective_rpc` 的广播、以及 8 个 vLLM worker 各自用 `_device_uuid` 查表——全部跑在纯 CPU 上（不真正建立 ZMQ 连接，只验证到「查到属于自己的地址」为止；数据面的 REP 状态机已在 u4-l1 验证过）。

```python
# 示例代码：u4l2_drill.py，仓库根目录运行 python u4l2_drill.py，实践后可删除
import sys
import types

import torch

# ---- 模块一铺垫：桩掉 vllm.platforms，让扩展类可以在无 vLLM 环境下工作 ----
fake_vllm = types.ModuleType("vllm")
fake_platforms = types.ModuleType("vllm.platforms")

class FakeCudaPlatform:
    device_type = "cuda"
    @staticmethod
    def get_device_uuid(index):          # 模拟 vLLM 返回 GPU-<uuid> 格式
        return f"GPU-FAKE-{index}"

fake_platforms.current_platform = FakeCudaPlatform
fake_vllm.platforms = fake_platforms
sys.modules["vllm"] = fake_vllm
sys.modules["vllm.platforms"] = fake_platforms

from checkpoint_engine.worker import VllmColocateWorkerExtension

P = 8  # 单实例 8 卡 TP

# ---- 步骤 1：模拟 ps.py::_bind_zmq_socket 产出的全集群地址表（本演习只有一个节点）----
counter = 0
socket_paths = [
    (f"GPU-FAKE-{i}", f"ipc://@checkpoint-engine-GPU-FAKE-{i}-{counter}.sock") for i in range(P)
]

# ---- 步骤 2：模拟 examples/update.py::req_inference 的组首切片 + api.py 的请求体 ----
rank, src = 3, 0                                   # 组首是 rank 0
assert src <= rank < src + P
body = {"method": "update_weights_from_ipc",
        "args": [dict(socket_paths[src : src + P])],   # 只发本实例的 P 条
        "timeout": 300.0}

# ---- 步骤 3：模拟 collective_rpc 广播：每个 worker 用自身设备 UUID 查表 ----
for local_rank in range(P):
    ext = VllmColocateWorkerExtension()            # 真实的扩展类
    ext.device = torch.device("cuda", local_rank)  # 宿主 worker 本应提供的属性
    addr = body["args"][0][ext._device_uuid]       # 真实的查表逻辑
    assert addr == f"ipc://@checkpoint-engine-GPU-FAKE-{local_rank}-{counter}.sock"
    print(f"worker{local_rank} ({ext._device_uuid}) -> {addr}")

# ---- 步骤 4：故障注入——UUID 前缀失配时，第一个倒下的就是查表 ----
bad_handles = {f"gpu-fake-{i}": "ipc://@x.sock" for i in range(P)}   # 前缀大小写不同
try:
    bad_handles["GPU-FAKE-0"]     # worker 侧算出的键是 GPU-FAKE-0（大写 GPU-）
except KeyError as e:
    print("UUID 失配的后果：KeyError:", e)
print("drill passed: all", P, "workers resolved their own zmq address")
```

**验收标准**：

1. 8 行输出中，`worker i` 恰好连到 `GPU-FAKE-i` 的地址（一对一，无交叉）——验证模块一（请求体切片与广播）+ 模块二（UUID 查表）协同正确。
2. 步骤 4 打印出 `KeyError`——把「两侧 UUID 格式必须逐字符一致」从口头约束变成亲眼所见。
3. 思考题（不需写码）：把这个脚本从 1 个实例改成 2 个实例（16 卡、2 节点），`req_inference` 会发几次 HTTP、各带哪几条地址？（答案：2 次，节点 0 组首发 `socket_paths[0:8]`，节点 1 组首发 `socket_paths[8:16]`，对应 [examples/update.py:82-91](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py#L82-L91) 的 `src = rank // P * P` 计算。）

说明：步骤 2 的 `rank=3` 只是示意非组首进程的归属计算；步骤 4 中 `bad_handles` 的键故意用了小写 `gpu-` 前缀以演示失配。脚本无第三方依赖（torch + 本仓库即可），在纯 CPU 环境**待本地验证**运行输出。

## 6. 本讲小结

- `VllmColocateWorkerExtension` 是纯适配器：靠 `--worker-extension-cls` 注入 vLLM worker、被 `/collective_rpc` 按方法名调用（请求体 `{"method": "update_weights_from_ipc", "args": [uuid→addr 字典]}`），随后委托给与 vLLM 完全解耦的模块级 REP 状态机。
- PS 不直接认识 vLLM：它只调用调用方注入的 `req_func`；组首按 `src = rank // P * P` 切片后把本实例 P 条地址 POST 给本节点的 vLLM——因为抽象 UDS 地址只在主机内有效。
- `_device_uuid` 是跨进程配对的钥匙：CUDA 走 vLLM 平台层、NPU 用 `npu-smi` 反查物理芯片合成 `NPU-{IP-编号}`、XPU 拼 `GPU-{uuid}`；worker 与 PS 两侧的生成规则**必须逐字符一致**，否则查表 `KeyError`，XPU 分支的注释把这一约束写进了源码。
- `_load_weights` 每桶执行一次，同一份全量 `(名字, 张量)` 清单同时喂主模型和 MTP drafter（两个防御性 `getattr` 兜住「无投机解码/无 `.model`」的形态），由各模型的 `load_weights` 按名字各取所需。
- `_post_hook` 与「第二个 `None`」绑定，在所有桶装载、资源清理完成后执行**一次** `process_weights_after_loading`（主模型 + drafter），补上被热更新绕过的 vLLM 权重后处理工序——这也是 FP8 需要 patch 的线索起点。

## 7. 下一步学习建议

- **u4-l3（CUDA IPC：TorchIPCHandler 与 reduce_tensor）**：本讲反复出现却未展开的「IPC 句柄」到底是什么？下一讲讲 `update_weights_from_ipc` 收到的第一条消息（`socket.recv_pyobj()` 那个 handle）如何被 `reduce_tensor` 制造、又如何在 worker 进程里重建出共享显存。
- **u4-l4（XPU SYCL IPC）**：如果你对 `_device_uuid` XPU 分支背后的 Intel 平台感兴趣，该讲深入 `XpuIPCHandler` 与运行时 JIT 编译的 SYCL 原生扩展。
- **u6-l4（FP8 补丁、限制与二次开发）**：本讲 `_post_hook` 埋下的伏笔在那里兑现——分析 `patches/vllm_fp8.patch` 如何修正 FP8 量化权重的更新。
- **延伸阅读（需本地装有 vLLM）**：对照 vLLM 源码确认两处「待确认」：`collective_rpc` 端点如何把方法调用广播到 worker、以及 `vllm/platforms` 中 `get_device_uuid` 的返回格式；再读 vLLM [PR #24295](https://github.com/vllm-project/vllm/pull/24295)（README 致谢中提到的同款接口），理解这套 worker extension 契约的设计初衷。
