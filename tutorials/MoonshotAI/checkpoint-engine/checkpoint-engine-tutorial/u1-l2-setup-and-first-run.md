# 环境搭建与端到端体验:从安装到第一次权重更新

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 `pip install checkpoint-engine` 与 `pip install 'checkpoint-engine[p2p]'` 两种安装形态的区别，以及为什么 P2P 依赖是可选的。
2. 按照 README 的 Getting Started 流程准备一个 vLLM 环境，并理解 `--worker-extension-cls checkpoint_engine.worker.VllmColocateWorkerExtension` 这个启动参数在整条调用链里的作用。
3. 读懂 `examples/update.py` 的脚本骨架：torchrun 如何把它拉起、命令行参数有哪些、主流程按什么顺序调用 `ParameterServer` 的生命周期方法。
4. 区分项目测试中哪些必须要有 GPU（`pytest tests/test_update.py`）、哪些可以在纯 CPU 机器上运行（`pytest tests/ -m "not gpu"`）。

本讲是全手册第二讲，承接 u1-l1 建立的概念：checkpoint-engine 是训练侧与推理引擎之间的权重更新中间件，核心类是 `ParameterServer`，生命周期为 register → gather_metas → update → unregister。本讲不深入任何一处实现细节，只解决「怎么装、怎么跑、跑起来之后看到什么」。

## 2. 前置知识

本讲会用到以下几个基础概念，用通俗语言先解释一遍：

- **pyproject.toml 与 extra**：现代 Python 项目用一个 `pyproject.toml` 文件描述「这个包叫什么、依赖什么、怎么构建」。其中 `dependencies` 是装包时一定会装的依赖；`[project.optional-dependencies]` 里的条目叫 **extra（可选依赖组）**，只有用户显式写 `包名[组名]` 时才会安装。checkpoint-engine 用这个机制把 RDMA 传输库做成了可选项。
- **venv / uv**：虚拟环境用于隔离不同项目的依赖。README 用 `uv venv` 快速创建，用 `uv pip install` 安装，效果与 `python -m venv` + `pip install` 等价，只是更快。
- **torchrun 与 RANK / WORLD_SIZE**：`torchrun` 是 PyTorch 自带的分布式启动器。`--nproc-per-node 8` 表示在本机起 8 个进程，每个进程会自动注入环境变量 `RANK`（全局进程编号，0 到 7）、`WORLD_SIZE`（总进程数，这里是 8）、`LOCAL_RANK`（本机编号）。checkpoint-engine 的 `ParameterServer` 就是靠读这两个环境变量确定自己是谁。
- **safetensors 与 index.json**：大模型的权重通常存成一堆 `.safetensors` 文件，外加一个 `model.safetensors.index.json` 索引，里面记录「每个张量名字 → 所在文件」的映射（`weight_map`）。
- **vLLM 与 `--load-format dummy`**：vLLM 是一个推理引擎。`--load-format dummy` 表示启动时不真正读磁盘权重、只按形状随机初始化——这正好配合 checkpoint-engine 在启动后立刻把真权重灌进去，省掉一次磁盘加载。
- **pytest marker**：pytest 允许给测试打标签（marker），例如本项目定义了 `gpu` 标签。`-m "not gpu"` 表示「跳过所有带 gpu 标签的测试」，这样没有显卡的机器也能跑单元测试。
- **colocated 部署**：回顾 u1-l1——checkpoint-engine 的 `ParameterServer` 与推理引擎的 worker 进程部署在同一批 GPU 上（同机同卡），这样才能用 CUDA IPC 这类「同机零拷贝」手段。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲视角 |
| --- | --- | --- |
| [pyproject.toml](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/pyproject.toml) | 包元数据：依赖、extra、构建方式、pytest 配置 | **精读**：两种安装形态的出处 |
| [examples/update.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py) | 端到端示例驱动脚本，README 所有基准都由它跑出 | **精读**：本讲主角 |
| [README.md](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/README.md) | 安装、快速开始、测试说明 | **精读**：操作手册原文 |
| [checkpoint_engine/worker.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py) | vLLM worker 扩展 `VllmColocateWorkerExtension` | 只看类声明与 `--worker-extension-cls` 的注入说明 |
| [checkpoint_engine/api.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/api.py) | HTTP 客户端 `request_inference_to_update` 与 REST 端点 | 只看「怎么通知推理引擎开始收权重」 |
| [checkpoint_engine/ps.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py) | `ParameterServer` 核心 | 只看 `__init__` 的 `auto_pg` 参数 |
| [checkpoint_engine/p2p_store.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/p2p_store.py) | mooncake TransferEngine 封装 | 只看 mooncake 是延迟导入的证据 |
| [tests/](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_update.py) | 测试套件 | 看哪些带 `gpu` marker |

## 4. 核心概念与源码讲解

### 4.1 安装形态：pyproject.toml 与 `[p2p]` extra

#### 4.1.1 概念说明

checkpoint-engine 有两种安装形态，对应两种使用场景：

| 命令 | 装了什么 | 适用场景 |
| --- | --- | --- |
| `pip install checkpoint-engine` | 基础依赖（torch、pyzmq、fastapi 等） | 只用 **Broadcast** 广播更新（默认、最快的实现） |
| `pip install 'checkpoint-engine[p2p]'` | 基础依赖 + `mooncake-transfer-engine>=0.3.5` | 还要用 **P2P** 更新（新实例动态加入、RDMA 传输） |

为什么 mooncake 可以是可选的？因为 P2P 模式只在「动态扩容 / 实例重启后回填权重」时才用到（回顾 u1-l1 的两种更新方式）。基础广播路径完全不需要 RDMA 库。注意 shell 里写 `'checkpoint-engine[p2p]'` 要加引号——方括号在 bash/zsh 里是通配符，不加引号可能被 shell 展开导致装错包。

另外两个值得注意的点：

- **版本号是动态的**：`dynamic = ["version"]` + `setuptools-scm`，版本号来自 git tag，不从 pyproject 里写死。
- **XPU 需要从源码装**：Intel XPU 的支持还没进发布包，要 `git clone` 后 `pip install -e .`，且**不能**带 `[p2p]`（XPU 不支持 P2P）。

#### 4.1.2 核心流程

安装时的依赖解析流程：

1. pip/uv 读取 `pyproject.toml` 的 `[project]` 表。
2. 安装 `dependencies` 列表中的 9 个包（torch、fastapi、pydantic、safetensors、pyzmq、uvicorn、loguru、numpy、httpx）。
3. 若命令行带 `[p2p]`，再安装 `optional-dependencies.p2p` 列表里的 `mooncake-transfer-engine>=0.3.5`（版本下限 0.3.5 是因为批量注册内存的 `batch_register_memory` 接口从该版本才引入）。
4. 构建阶段由 setuptools 打包 `checkpoint_engine` 及其子包；XPU 的 C++ 源码 `*.cpp` 也随包分发（运行时才 JIT 编译，见 u4-l4）。

#### 4.1.3 源码精读

基础依赖声明，全部 9 项在装包时必然安装：[pyproject.toml:8-18](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/pyproject.toml#L8-L18)

```toml
dependencies = [
    "torch>=2.5.0",
    "fastapi",
    "pydantic>=2.0.0",
    "safetensors",
    "pyzmq",      # PS 与推理引擎之间的 ZMQ 消息通道
    "uvicorn",    # HTTP API 服务的 ASGI server
    "loguru",
    "numpy",
    "httpx",      # 请求推理引擎 /collective_rpc 的 HTTP 客户端
]
```

`[p2p]` extra 只有一项，且注释写明了版本下限的原因：[pyproject.toml:20-24](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/pyproject.toml#L20-L24)

```toml
[project.optional-dependencies]
p2p = [
    # `batch_register_memory` is introduced in 0.3.5
    "mooncake-transfer-engine>=0.3.5",
]
```

「不装 mooncake 也能 import 整个包」的证据——mooncake 的导入被推迟到 `P2PStore.__init__` 函数体内，而不是模块顶层：[checkpoint_engine/p2p_store.py:11-13](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/p2p_store.py#L11-L13)

```python
class P2PStore:
    def __init__(self, device_manager: DeviceManager):
        from mooncake.engine import TransferEngine   # 延迟导入：只有真正建 P2P store 时才需要
```

而 `checkpoint_engine/__init__.py` 在包加载时就 `from .p2p_store import P2PStore`：[checkpoint_engine/__init__.py:17](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/__init__.py#L17)。两处合起来说明：基础安装下 `import checkpoint_engine` 不会报错，只有走到 P2P 分支才会因为缺 mooncake 而失败。

XPU 源码安装与 `icpx` 编译器要求的说明在 README：[README.md:78-98](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/README.md#L78-L98)，其中明确写了 `pip install -e .    # no [p2p] extra on XPU`。

#### 4.1.4 代码实践

**实践目标**：在一台普通机器（无需 GPU）上验证两种安装形态的差异，以及「缺 mooncake 也能 import」这一结论。

**操作步骤**：

```bash
# 1. 建一个干净的虚拟环境（二选一）
uv venv --python 3.12 --seed && source .venv/bin/activate
# 或: python3 -m venv .venv && source .venv/bin/activate

# 2. 只装基础包
pip install checkpoint-engine

# 3. 验证可以导入、查看版本
python -c "import checkpoint_engine; print(checkpoint_engine.__version__)"

# 4. 确认当前环境里没有 mooncake
pip list | grep -i mooncake || echo "mooncake 未安装"

# 5. （可选）补装 p2p extra，再观察差异
pip install 'checkpoint-engine[p2p]' && pip list | grep -i mooncake
```

**需要观察的现象**：

- 第 3 步能正常打印出版本号（来自 git tag 的 setuptools-scm 版本，或开发环境下的 `dev`，见 [checkpoint_engine/__init__.py:1-4](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/__init__.py#L1-L4)），且**没有**出现 `ModuleNotFoundError: mooncake`——这就验证了 4.1.3 中的延迟导入结论。
- 第 5 步之后 `mooncake-transfer-engine` 出现在 `pip list` 中。

**预期结果**：基础包即可完成 import；mooncake 只在 `[p2p]` 形态下存在。若你的环境无法访问 PyPI，此实践**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：如果只执行了 `pip install checkpoint-engine`（基础形态），后面却调用了 `ps.update(checkpoint_name, req_func, ranks=[0, 1])` 走 P2P 路径，会发生什么？

**答案**：在创建 P2P store 时（`P2PStore.__init__` 里执行 `from mooncake.engine import TransferEngine`）抛出 `ModuleNotFoundError: No module named 'mooncake'`。这正是把导入放进函数体的意义：错误被推迟到真正用到 P2P 的那一刻，而不是安装/导入包的时刻。

**练习 2**：为什么 `pyproject.toml` 里 p2p extra 要写 `mooncake-transfer-engine>=0.3.5` 而不是随便一个版本？

**答案**：注释已说明——`batch_register_memory` 批量内存注册接口是 0.3.5 才引入的，P2PStore 的批量注册依赖它（u5-l5 会精读）。低于该版本的 mooncake 缺少必需接口。

**练习 3**：`pyproject.toml` 中 `dynamic = ["version"]` 配合 `[tool.setuptools_scm]` 意味着版本号从哪来？

**答案**：从 git 的 tag/提交状态推导（setuptools-scm），安装时不写死版本号。因此源码安装且没有任何 tag 时会得到类似 `0.x.y.devN+hash` 的版本，`checkpoint_engine/_version.py` 是构建时生成的版本文件（[pyproject.toml:37-38](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/pyproject.toml#L37-L38)）。

### 4.2 vLLM 环境准备与 `--worker-extension-cls`

#### 4.2.1 概念说明

Broadcast 更新是「PS 主动推、推理引擎被动收」。要让它成立，推理引擎进程里必须有人**接应**：接收 CUDA IPC 句柄、从共享显存里切出张量、调用模型自己的 `load_weights`。vLLM 提供了一个注入点叫 **worker extension**——你写一个类，它的方法会被合并进 vLLM 的 worker 类，从而能被 vLLM 的 `/collective_rpc` 端点远程调用。checkpoint-engine 提供的接应类就是 `VllmColocateWorkerExtension`。

启动 vLLM 时用 `--worker-extension-cls checkpoint_engine.worker.VllmColocateWorkerExtension` 告诉 vLLM 加载这个类。完整启动命令（README 原文）中的关键参数：

| 参数 | 作用 |
| --- | --- |
| `VLLM_SERVER_DEV_MODE=1` | 开发模式，允许加载自定义 worker 扩展等非生产配置 |
| `--tensor-parallel-size=8` | 8 卡张量并行，即 1 个 vLLM 实例占 8 个进程/8 张卡 |
| `--load-format dummy` | 不从磁盘读权重，随机初始化，等 checkpoint-engine 灌真权重 |
| `--port 19730` | API 端口，后面 `examples/update.py --endpoint` 要与之一致 |
| `--worker-extension-cls ...` | 注入 checkpoint-engine 的接应类（**必须**） |

README 还有一个硬性要求：vLLM 版本需包含 `/collective_rpc` 这个 API 端点（commit `f7cf5b51` 之后，main 分支已有），推荐 `v0.10.2`，见 [README.md:100-109](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/README.md#L100-L109)。没有这个端点，PS 无从通知 worker 开始收权重。

#### 4.2.2 核心流程

一次 Broadcast 更新中三个参与方的协作时序：

```text
checkpoint-engine (PS, torchrun 拉起 8 进程)      vLLM API server (:19730)      vLLM worker ×8 (注入了扩展)
        |                                                |                              |
        |  register_checkpoint → 权重进锁页内存            |                              |
        |  gather_metas → 制定传输计划                      |                              |
        |  准备好 IPC 句柄与 ZMQ 地址                        |                              |
        |--- POST /collective_rpc ----------------------->|                              |
        |    method="update_weights_from_ipc"             |--- 广播 RPC 到 8 个 worker -->|
        |                                                |                              |- 连回 PS 的 ZMQ socket
        |<------------------------------------------------------------- ZMQ REQ/REP 交换句柄
        |  broadcast 权重到各卡（CUDA IPC 共享显存）          |                              |- reload 进模型
```

注意「控制面」走 HTTP（`/collective_rpc`），「数据面」走 ZMQ + CUDA IPC——先由 PS 通过 HTTP 让 worker 主动连过来，之后的大块数据完全绕开 HTTP。

#### 4.2.3 源码精读

vLLM 启动命令与说明（README Getting Started 原文）：[README.md:123-130](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/README.md#L123-L130)

```bash
VLLM_SERVER_DEV_MODE=1 python3 -m vllm.entrypoints.openai.api_server --host 0.0.0.0 --port 19730 --trust-remote-code \
    --tensor-parallel-size=8 --max-model-len 4096 --load-format dummy \
    --served-model-name checkpoint-engine-demo --model /opt/models/Qwen/Qwen3-235B-A22B-Instruct-2507/ \
    --worker-extension-cls checkpoint_engine.worker.VllmColocateWorkerExtension
```

接应类的自述文档——类 docstring 明确说明了「方法会被注入 vLLM worker 类、可被 collective_rpc 调用」的机制：[checkpoint_engine/worker.py:134-148](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L134-L148)

```python
class VllmColocateWorkerExtension:
    """
    Worker extension for vLLM to update weights from checkpoint-engine.
    ...
    The methods in this worker extension will be injected into the vLLM worker class and
    are callable from the `collective_rpc` API, enabling seamless weight updates for both
    vLLM V0 and V1 versions.

    Note:
        ... The fully qualified name
        `checkpoint_engine.worker.VllmColocateWorkerExtension` should be passed as the
        `worker_extension_cls` argument when initializing the vLLM worker.
    """
```

被远程调用的入口方法：[checkpoint_engine/worker.py:168-172](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L168-L172)

```python
def update_weights_from_ipc(self, zmq_handles: dict[str, str]):
    """... 1. Receiving IPC handles ... 2. Extracting flattened metadata ...
    3. Loading weights into the model 4. Post-processing weights after loading"""
```

PS 侧发起 HTTP 调用的客户端函数——注意请求体里的 `method` 恰好就是上面的方法名 `update_weights_from_ipc`：[checkpoint_engine/api.py:15-43](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/api.py#L15-L43)

```python
def request_inference_to_update(url, socket_paths, timeout=300.0, uds=None):
    resp = httpx.Client(transport=httpx.HTTPTransport(uds=uds)).post(
        url,
        json={
            "method": "update_weights_from_ipc",
            "args": [socket_paths],     # 设备 UUID → ZMQ socket 路径 的映射
            "timeout": timeout,
        },
        timeout=timeout,
    )
    resp.raise_for_status()
```

`socket_paths` 的键是**设备 UUID**（CUDA 用 `current_platform.get_device_uuid()`，NPU 用 `NPU-{uuid}`，XPU 用 `GPU-{uuid}`，见 [checkpoint_engine/worker.py:150-162](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L150-L162)），worker 拿到映射后用**自己所在设备**的 UUID 取出属于自己的 ZMQ 地址。这个「按设备寻址」的细节在 u4-l2 会展开。

#### 4.2.4 代码实践

**实践目标**：不启动 vLLM，仅通过源码阅读确认三件事——(a) `--worker-extension-cls` 指向的类真实存在；(b) `/collective_rpc` 到达 worker 后执行的方法名；(c) 你的 vLLM 版本是否满足要求。

**操作步骤**：

1. 在安装了 checkpoint-engine 的环境中执行：

   ```bash
   python -c "
   from checkpoint_engine.worker import VllmColocateWorkerExtension
   print(VllmColocateWorkerExtension.__module__ + '.' + VllmColocateWorkerExtension.__name__)
   print([m for m in dir(VllmColocateWorkerExtension) if not m.startswith('__')])
   "
   ```

2. 阅读函数体 [checkpoint_engine/api.py:34-42](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/api.py#L34-L42)，抄下 POST 请求体的三个字段名。
3. （若环境里有 vLLM）检查版本：

   ```bash
   python -c "import vllm; print(vllm.__version__)"
   ```

**需要观察的现象**：

- 第 1 步应打印 `checkpoint_engine.worker.VllmColocateWorkerExtension`，且方法列表中包含 `update_weights_from_ipc`——这正是启动参数里的全限定名，证明参数没有拼错。
- 第 2 步应得到 `method` / `args` / `timeout` 三个字段，其中 `method` 的值与方法列表中的名字一一对应。

**预期结果**：启动参数、HTTP 请求体、worker 方法名三者闭环对上。第 3 步若不是 `0.10.2` 或不含 `/collective_rpc` 的版本，README 建议[待本地验证]其可用性。

#### 4.2.5 小练习与答案

**练习 1**：为什么 vLLM 启动要加 `--load-format dummy`？不加会有什么后果？

**答案**：dummy 模式让 vLLM 跳过磁盘权重加载、只按形状占位初始化，然后由 checkpoint-engine 直接灌入真权重。不加的话 vLLM 会先完整读一遍磁盘权重（大模型耗时可达分钟级），而这份权重马上就会被 checkpoint-engine 覆盖——纯浪费启动时间。

**练习 2**：`request_inference_to_update` 的 `uds` 参数是干什么的？从代码看它改变了什么？

**答案**：`uds` 是 Unix domain socket 路径。传了它之后 httpx 用 `httpx.HTTPTransport(uds=uds)` 建传输，请求不走 TCP 网络栈而走本机 UDS 文件——同机通信更低延迟。这也解释了 `examples/update.py` 里 `--uds` 参数的用途（见 4.3）。

**练习 3**：`VllmColocateWorkerExtension` 里 `_device_uuid` 为什么要区分 cuda/npu/xpu 三种平台返回不同格式？

**答案**：因为 PS 侧构造 `socket_paths` 字典时用的键必须与 worker 侧完全一致才能配对。不同平台的设备 UUID 获取方式不同（CUDA 走 `current_platform.get_device_uuid`，NPU 需要自行生成 `NPU-{uuid}`，XPU 用 torch.xpu 的属性并格式化成 `GPU-{uuid}`），两侧约定相同格式才能让每个 worker 精确找到自己的 ZMQ 地址（u4-l2 精读）。

### 4.3 examples/update.py：torchrun 驱动的脚本骨架

#### 4.3.1 概念说明

`examples/update.py` 虽然放在 examples 目录，但它不是玩具：README 的全部基准数据（GLM-4.5-Air、Qwen3-235B、DeepSeek-V3.1、Kimi-K2 那张表）都是用它跑出来的，它就是这个项目的「官方编排器」。它做四件事：

1. 解析命令行参数；
2. （可选）切换分布式后端；
3. 创建 `ParameterServer(auto_pg=True)`；
4. 根据是否传入 `--load-metas-file/--metas-url` 走 **join 模式**（复用已有实例的权重）或 **常规更新模式**。

它由 torchrun 拉起，`--nproc-per-node 8` 意味着同一份脚本会被复制成 8 个进程，各自带着不同的 `RANK`（0~7）执行。**单机 8 卡的典型部署里，这 8 个 PS 进程与 vLLM 的 8 个 TP worker 一一对应地共享同一批 GPU**——这就是 colocated。

#### 4.3.2 核心流程

`__main__` 入口的决策流程（对应源码 162-225 行）：

```text
torchrun --nproc-per-node 8 examples/update.py --update-method all --checkpoint-path $MODEL_PATH
        │
        ├─ argparse 解析 12 个参数
        ├─ rank = int(os.getenv("RANK")); world_size = int(os.getenv("WORLD_SIZE"))
        ├─ req_func = req_inference(endpoint, inference_parallel_size, uds)
        ├─ dist.use_backend(args.custom_dist)          # 默认 None → 用 PyTorch 默认后端
        ├─ ps = ParameterServer(auto_pg=True)
        │
        ├─ 若 --load-metas-file 或 --metas-url ──→ join(...)      # 复用权重模式
        │
        └─ 否则（常规更新模式）
             ├─ checkpoint 目录有 index.json 且不在 /dev/shm 下？
             │     是 → split_tensors(...)  按张量均分，named_tensors 非空
             │     否 → split_checkpoint_files(...) 按文件均分，files 非空
             └─ update_weights(...)                  # 生命周期主流程
        最后: time.sleep(args.sleep_time)
```

#### 4.3.3 源码精读

全部命令行参数的定义，注意 `--load-metas-file` 与 `--metas-url` 是互斥组：[examples/update.py:162-186](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py#L162-L186)

```python
parser = argparse.ArgumentParser(description="Update weights example")
parser.add_argument("--checkpoint-path", type=str, default=None)
parser.add_argument("--save-metas-file", type=str, default=None)
metas_src = parser.add_mutually_exclusive_group()
metas_src.add_argument("--load-metas-file", ...)   # 触发 join 模式
metas_src.add_argument("--metas-url", ...)         # 触发 join 模式
parser.add_argument("--sleep-time", type=int, default=0)
parser.add_argument("--endpoint", type=str, default="http://localhost:19730")
parser.add_argument("--inference-parallel-size", type=int, default=8)
parser.add_argument("--checkpoint-name", type=str, default="my-checkpoint-iter-0")
parser.add_argument("--update-method", type=str, default="broadcast")
parser.add_argument("--uds", type=str, default=None)
parser.add_argument("--custom-dist", type=str, default=None)
```

从环境变量取身份、构造 PS：[examples/update.py:187-192](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py#L187-L192)

```python
rank = int(os.getenv("RANK"))
world_size = int(os.getenv("WORLD_SIZE"))

req_func = req_inference(args.endpoint, args.inference_parallel_size, args.uds)
dist.use_backend(args.custom_dist)
ps = ParameterServer(auto_pg=True)
```

`auto_pg=True` 的含义——由 PS 自动 `init_process_group`，并在每次 update 结束后自动 `destroy_process_group`，官方推荐开启：[checkpoint_engine/ps.py:179-197](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L179-L197)

```python
def __init__(self, *, rank=None, world_size=None, auto_pg: bool = True, ...):
    """
    Args:
        ...
        auto_pg: Whether to automatically initialize the process group.
            Notice that if auto_pg is True, will destroy the process group after update.
            It is recommended to set auto_pg to True!
```

join 分支与常规分支的分派，以及 `/dev/shm` 特判（已在共享内存里的文件直接按文件分配，不必再解析 index）：[examples/update.py:193-212](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py#L193-L212)

```python
if args.load_metas_file or args.metas_url:
    join(ps, args.checkpoint_name, ...)
else:
    if os.path.exists(os.path.join(args.checkpoint_path, "model.safetensors.index.json")) \
            and not args.checkpoint_path.startswith("/dev/shm/"):
        named_tensors = split_tensors(args.checkpoint_path, rank, world_size)
        checkpoint_files = []
    else:
        checkpoint_files = split_checkpoint_files(args.checkpoint_path, rank, world_size)
        named_tensors = {}
```

`--custom-dist` 支持的后端映射（vllm_nccl / vllm_hccl，其余报错；XPU 不传即用默认后端）：[checkpoint_engine/distributed/base.py:221-242](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/distributed/base.py#L221-L242)

```python
def use_backend(backend: str | None):
    global _BACKEND_INSTANCE
    if not backend:
        return
    mapping = {
        "vllm_nccl": ".vllm_nccl.DistributedNccl",
        "vllm_hccl": ".vllm_hccl.DistributedHccl",
    }
    ...
```

#### 4.3.4 代码实践

**实践目标**：不真正发起分布式启动，先「干跑」脚本，摸清全部参数与默认值。

**操作步骤**：

```bash
# 1. 查看帮助（argparse 不需要 RANK 环境变量）
python examples/update.py --help

# 2. 故意不带 torchrun 直接运行，观察报错点
python examples/update.py
```

**需要观察的现象**：

- 第 1 步打印 12 个参数的 help 文本，与 4.3.3 中源码一一对应；`--load-metas-file` 与 `--metas-url` 显示为互斥。
- 第 2 步预期在 `rank = int(os.getenv("RANK"))` 一行抛出 `TypeError: int() argument must be a string... not 'NoneType'`（环境变量不存在时 `os.getenv` 返回 `None`）。这验证了「该脚本必须在 torchrun 下运行」。

**预期结果**：参数清单与报错位置与源码一致。注意 `--help` 能否成功取决于 `import checkpoint_engine` 等顶层导入是否顺利（需先完成 4.1 的安装）；若环境缺少依赖，此实践**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：README 的快速开始里 `--update-method all` 是什么效果？与默认值有何不同？

**答案**：`--update-method` 默认 `broadcast`。`all` 表示先做一次 Broadcast 更新（`ps.update(checkpoint_name, req_func)`），再等 2 秒后做一次 P2P 更新（`ps.update(..., ranks=list(range(inference_parallel_size)))`），两种方式各跑一遍并分别计时——演示用一条命令同时验证两条路径（见 [examples/update.py:119-128](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py#L119-L128)）。

**练习 2**：为什么脚本最后要 `time.sleep(args.sleep_time)`？

**答案**：默认 `--sleep-time 0`，脚本做完更新立刻退出，锁页内存、P2P store 等资源随进程销毁。传 `--sleep-time 300` 可以让实例存活一段时间——README「复用已有实例权重」一节正是这样让旧实例活着，好让新实例通过 `--load-metas-file` 从它那里拉权重（[README.md:142-153](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/README.md#L142-L153)）。

**练习 3**：`--inference-parallel-size` 默认 8、`--endpoint` 默认 `http://localhost:19730`，这两个默认值分别和什么对齐？

**答案**：与 vLLM 侧启动命令对齐——`--tensor-parallel-size=8`（一个实例 8 个 worker 进程）和 `--port 19730`（API 端口）。改了 vLLM 的 TP 或端口，就必须同步改这两个参数。

### 4.4 checkpoint 切分与请求推理：负载均衡的两个维度

#### 4.4.1 概念说明

`WORLD_SIZE` 个 PS 进程要合力把整个 checkpoint 读进内存，先得决定「谁读哪些」。脚本提供两种切分粒度：

- **按文件切**（`split_checkpoint_files`）：把目录里的 `.safetensors` 文件列表均分给各 rank。简单，但文件数少时负载不均（比如只有 2 个大文件、8 个 rank，6 个 rank 闲着）。
- **按张量切**（`split_tensors`）：读 `model.safetensors.index.json` 的 `weight_map`，把**张量名**均分给各 rank，再各自打开涉及到的文件取张量。粒度细、负载更均衡，代价是要解析 index 且多次打开同一文件。

选择逻辑在 4.3.3 已见：目录里有 index.json 且不在 `/dev/shm` 下就走张量切分，否则按文件切分。

`req_inference` 则回答另一个问题：**谁去敲 vLLM 的门**。8 个 PS 进程都能连 vLLM，但 `/collective_rpc` 只需要（也只能）被调用一次；脚本让每个「推理组」的第一个进程（`rank % inference_parallel_size == 0`）去发请求。

#### 4.4.2 核心流程

均分算法用的是向上取整：

\[
\text{items\_per\_rank} = \left\lceil \frac{N}{W} \right\rceil = \frac{N + W - 1}{W}
\]

其中 \(N\) 是文件数（或张量数），\(W\) 是 `world_size`。rank \(r\) 负责下标区间：

\[
[\,r \cdot \lceil N/W \rceil,\ (r+1) \cdot \lceil N/W \rceil\,)
\]

由于切片天然截断越界下标，最后一个 rank 可能分到较少甚至 0 个元素，不影响正确性。

`req_inference` 中的源 rank 计算：`src = rank // P * P`（\(P\) 为 `inference_parallel_size`），即把 rank 向下取整到所在组的第一个进程；仅当 `rank == src` 时发起 HTTP 请求，并把 `socket_paths` 裁剪成本组那一段 `socket_paths[src : src + P]`。

#### 4.4.3 源码精读

按文件均分：[examples/update.py:51-57](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py#L51-L57)

```python
def split_checkpoint_files(checkpoint_path: str, rank: int, world_size: int) -> list[str]:
    checkpoint_files = [
        os.path.join(checkpoint_path, f)
        for f in filter(lambda x: x.endswith(".safetensors"), os.listdir(checkpoint_path))
    ]
    files_per_rank = (len(checkpoint_files) + world_size - 1) // world_size
    return checkpoint_files[rank * files_per_rank : (rank + 1) * files_per_rank]
```

按张量均分（注意它把「rank → 张量列表」再折叠成「文件 → 张量列表」，避免重复打开文件）：[examples/update.py:60-74](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py#L60-L74)

```python
def split_tensors(checkpoint_path: str, rank: int, world_size: int) -> dict[str, torch.Tensor]:
    index_fn = os.path.join(checkpoint_path, "model.safetensors.index.json")
    with open(index_fn) as f:
        weight_map: dict[str, str] = json.load(f)["weight_map"]
    weights_per_rank = (len(weight_map) + world_size - 1) // world_size
    fn_tensors: dict[str, list[str]] = defaultdict(list)
    weight_keys = list(weight_map.items())
    for name, file in weight_keys[rank * weights_per_rank : (rank + 1) * weights_per_rank]:
        fn_tensors[file].append(name)
    named_tensors = {}
    for file, names in fn_tensors.items():
        with safe_open(os.path.join(checkpoint_path, file), framework="pt") as f:
            for name in names:
                named_tensors[name] = f.get_tensor(name)
    return named_tensors
```

只有组首进程发请求，且只发本组的 socket_paths：[examples/update.py:77-93](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py#L77-L93)

```python
def req_inference(endpoint, inference_parallel_size, uds=None):
    rank = int(os.getenv("RANK", None))
    src = rank // inference_parallel_size * inference_parallel_size

    def req_func(socket_paths: list[tuple[str, str]]):
        if rank == src:
            request_inference_to_update(
                f"{endpoint}/collective_rpc",
                dict(socket_paths[src : src + inference_parallel_size]),
                uds=uds,
            )

    return req_func
```

等待 vLLM 就绪的重试循环（同样只有组首进程做，每 5 秒重试一次，永不放弃）：[examples/update.py:33-48](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py#L33-L48)

```python
def check_vllm_ready(endpoint, inference_parallel_size, uds=None):
    if rank != rank // inference_parallel_size * inference_parallel_size:
        return                       # 非组首直接返回
    ...
    while True:
        try:
            response = httpx.Client(transport=transport).get(f"{endpoint}/health", timeout=10)
            response.raise_for_status()
            break
        except (httpx.ConnectError, httpx.HTTPStatusError) as e:
            ...
            time.sleep(5)
```

这也解释了 README 里那句「No need to wait for vLLM to get ready」——可以先启动 torchrun 更新脚本，它会自己轮询等 vLLM 起来（[README.md:132-136](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/README.md#L132-L136)）。

#### 4.4.4 代码实践

**实践目标**：在纯 CPU 环境验证均分算法的行为，理解「最后一个 rank 可能分到 0 个」。

**操作步骤**：以下为**示例代码**（自包含，不依赖 torch，只复刻 `split_checkpoint_files` 的核心逻辑）：

```python
# split_demo.py —— 示例代码：复刻 examples/update.py 的均分逻辑
def split(items, rank, world_size):
    per_rank = (len(items) + world_size - 1) // world_size
    return items[rank * per_rank : (rank + 1) * per_rank]

files = [f"shard-{i}.safetensors" for i in range(5)]   # 5 个文件
W = 8                                                    # 8 个进程
for r in range(W):
    print(r, split(files, r, W))
```

运行 `python split_demo.py`。

**需要观察的现象**：

- 5 个文件、8 个进程时，`per_rank = ceil(5/8) = 1`，于是 rank 0~4 各得 1 个文件，rank 5、6、7 得到**空列表**。
- 把 `files` 改成 17 个、`W` 改成 8，观察 `per_rank = 3`，rank 5 只分到 2 个（17 = 3+3+3+3+3+2+0+0，共 8 组）。

**预期结果**：输出与公式 \(\lceil N/W \rceil\) 的推演完全一致；据此理解为什么文件数少于进程数时项目倾向按张量切分。此实践只用了纯 Python，可直接在本地验证。

#### 4.4.5 小练习与答案

**练习 1**：`req_inference` 里 `src = rank // inference_parallel_size * inference_parallel_size`，当 `rank=3`、`inference_parallel_size=8` 时 `src` 是多少？该进程会不会发 HTTP 请求？

**答案**：`src = 3 // 8 * 8 = 0`。`rank != src`，所以 rank 3 不发请求；只有 rank 0（组首）会调用 `request_inference_to_update`，并附带 `socket_paths[0:8]`。

**练习 2**：为什么不把 `weight_map` 直接按文件分组再分给 rank，而是先按张量名均分、再按文件聚合？

**答案**：直接按文件分组会继承「文件大小不均」的偏斜；先按张量个数均分保证各 rank 的工作量（张量数）尽量相等，之后再按文件聚合只是为了减少 `safe_open` 打开同一文件的次数，纯属读文件效率优化。

**练习 3**：`check_vllm_ready` 的重试循环有没有最大重试次数？这在生产上意味着什么？

**答案**：没有上限，`while True` 只在 `/health` 返回成功状态码时退出。生产上如果 vLLM 永远起不来，该进程会无限轮询（每 5 秒一次）；但日志会持续输出 `fail to check vllm ready, retry N times`，可据此做外部告警。

### 4.5 update_weights 主流程与测试运行方式

#### 4.5.1 概念说明

`update_weights` 函数把 u1-l1 讲过的 PS 生命周期串成可执行序列，是理解整个项目的「主干道」。同时本节解决最后一个实操问题：**装好之后怎么验证装对了**——答案是跑测试。项目的测试分两类：

- **GPU 端到端测试**：`tests/test_update.py` 等，需要在多卡机器上真实跑一次广播更新，验证权重正确性。
- **CPU 单元测试**：数据模型、HTTP API mock、RDMA 解析、bucket 分配等，`pytest tests/ -m "not gpu"` 即可。

`gpu` 这个 marker 就定义在 `pyproject.toml` 的 pytest 配置里。

#### 4.5.2 核心流程

`update_weights`（[examples/update.py:96-128](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py#L96-L128)）的执行序列：

1. `ps.init_process_group()` — 建立分布式进程组；
2. `dist.barrier()` — 各 rank 对齐（都注册完才继续）；
3. `ps.register_checkpoint(...)` — 各 rank 把分到的文件/张量登记进锁页内存；
4. `check_vllm_ready(...)` — 组首进程轮询直到 vLLM `/health` 通过；
5. `dist.barrier()` — 再次对齐；
6. `ps.gather_metas(checkpoint_name)` — 全局收集元数据、制定传输计划（**计时**：`Gather metas`）；
7. （可选）rank 0 把 metas 写入 `--save-metas-file`；
8. 若 `update_method ∈ {broadcast, all}`：`ps.update(checkpoint_name, req_func)` — Broadcast 更新（**计时**：`Update weights without setting ranks`）；
9. 若 `update_method ∈ {p2p, all}`：睡 2 秒等进程组销毁，再 `ps.update(..., ranks=list(range(P)))` — P2P 更新（**计时**：`Update weights with setting ranks`）。

其中第 3 步之前还有两道 `dist.barrier()`，作用是防止「快的 rank 已经开始 gather_metas，慢的 rank 还没注册完」导致元数据不完整。

#### 4.5.3 源码精读

主流程函数全文（去掉类型注解后的骨架）：[examples/update.py:108-128](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py#L108-L128)

```python
ps.init_process_group()
dist.barrier()
ps.register_checkpoint(checkpoint_name, files=checkpoint_files, named_tensors=named_tensors)
check_vllm_ready(endpoint, inference_parallel_size, uds)
dist.barrier()
with timer("Gather metas"):
    ps.gather_metas(checkpoint_name)
if save_metas_file and int(os.getenv("RANK")) == 0:
    with open(save_metas_file, "wb") as f:
        f.write(_METAS_ADAPTER.dump_json(ps.get_metas()))

if update_method == "broadcast" or update_method == "all":
    with timer("Update weights without setting ranks"):
        ps.update(checkpoint_name, req_func)          # ranks=None → Broadcast

if update_method == "p2p" or update_method == "all":
    ...
    time.sleep(2)                                     # 等 broadcast 用的进程组销毁
    with timer("Update weights with setting ranks"):
        ps.update(checkpoint_name, req_func, ranks=list(range(inference_parallel_size)))
```

计时器上下文——README 基准表里的 `GatherMetas 0.33s`、`Update (Broadcast) 6.22s` 等数字就来自这些日志：[examples/update.py:25-30](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py#L25-L30)

```python
@contextmanager
def timer(msg: str):
    start = time.perf_counter()
    yield
    end = time.perf_counter()
    logger.info(f"{msg} duration: {end - start:.2f} seconds")
```

`gpu` marker 的定义处：[pyproject.toml:166-169](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/pyproject.toml#L166-L169)

```toml
[tool.pytest.ini_options]
markers = [
    "gpu: marks tests as GPU test (deselect with '-m \"not gpu\"')",
]
```

GPU 测试的样子——用 `subprocess` 拉起 torchrun 跑自身，并断言至少 2 张卡：[tests/test_update.py:239-290](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_update.py#L239-L290)

```python
@pytest.mark.gpu
@pytest.mark.parametrize("test_name,rank_list", [...])
def test_update(test_name: str, rank_list: list[list[int]] | None):
    world_size = device_manager.device_module.device_count()
    assert world_size >= 2, "This test requires at least 2 GPUs."
    cmd = ["torchrun", "--nproc_per_node", str(world_size), ..., __file__, ...]
    result = subprocess.run(cmd, ...)
    assert result.returncode == 0
```

CPU 测试的样子——`tests/test_api.py` 开头即声明自己是 CPU-only，用 mock 的 PS 构造 FastAPI TestClient：[tests/test_api.py:1-10](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_api.py#L1-L10)

README 对测试方式的约定（特别注意「不要直接用 torchrun 跑 test_update.py」）：[README.md:163-177](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/README.md#L163-L177)

```bash
pytest tests/test_update.py        # 需要多卡 GPU
pytest tests/ -m "not gpu"         # 纯 CPU 机器可跑的单元测试
```

#### 4.5.4 代码实践

**实践目标**：在没有 GPU 的机器上跑通 CPU 单元测试，作为「安装成功」的验收标准；并用 `--collect-only` 建立测试清单。

**操作步骤**：

```bash
# 1. 列出所有测试（不执行），统计哪些会被 gpu 标签过滤掉
pytest tests/ --collect-only -q

# 2. 只跑 CPU 测试
pytest tests/ -m "not gpu" -v

# 3. 单独跑一个文件，观察它为什么能在 CPU 上运行
pytest tests/test_api.py -v
```

**需要观察的现象**：

- 第 1 步会列出整个测试清单；对比第 2 步的输出，会发现 `test_update.py` 的用例、`test_inplace_unpin.py`、`test_xpu_ipc.py`、`test_reuse_pin_memory.py` 的 gpu 用例等被过滤（它们带 `@pytest.mark.gpu`，如 [tests/test_update.py:239](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_update.py#L239)）。
- 第 3 步的输出标题含 `CPU-only tests for the metas endpoints in api.py`，用例通过 mock 的 `ParameterServer` 验证 `/v1/metas` 端点，全程不触碰 GPU。

**预期结果**：CPU 测试全绿即说明基础安装可用。若你的机器没有安装 pytest，先 `pip install pytest`；XPU 相关的 `test_xpu_ipc.py` 即使带 gpu 标签，在没有 Intel GPU 的机器上也会自动 skip（[README.md:94-98](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/README.md#L94-L98) 写明它是 hardware-gated）。具体通过数量**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：README 为什么特别强调 `test_update.py` 只能用 pytest 跑、不能用 torchrun 直接跑？

**答案**：因为 `test_update.py` 内部自己用 `subprocess` 拉起 `torchrun` 去执行它自己文件里的辅助函数（见 4.5.3 的 `cmd = ["torchrun", ..., __file__, ...]`）。pytest 是外层驱动，负责参数化与断言；如果直接 torchrun 跑它，就绕过了 pytest 的用例组织与断言逻辑。

**练习 2**：`update_weights` 在 broadcast 分支与 p2p 分支之间 `time.sleep(2)`，注释写「wait destroy process group」。结合 4.3 的 `auto_pg` 说明这两秒在等什么？

**答案**：`auto_pg=True` 时每次 `ps.update` 结束都会自动 `destroy_process_group`（[checkpoint_engine/ps.py:194-195](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L194-L195)）。broadcast 分支刚销毁了进程组，p2p 分支马上又要 `init_process_group` 重建；睡 2 秒是给销毁动作留出完成时间，避免重建时撞上未释放完的资源。

**练习 3**：`--save-metas-file` 由哪个 rank 写？为什么只有它写？

**答案**：`rank == 0` 写（[examples/update.py:115-117](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py#L115-L117)）。因为 `gather_metas` 之后每个 rank 持有的全局元数据是相同的（这正是 gather 的语义），让 8 个进程写同一个文件只会互相覆盖/竞争，指定 rank 0 单点写出即可。

## 5. 综合实践

**综合任务**：完成一次「从零到验收」的完整环境搭建，并把每一步的证据记录在一张表里。分两条路线，按你的硬件条件选一条：

**路线 A（无 GPU，任何机器）——安装与静态验收**：

1. 用 uv 或 venv 创建隔离环境，安装 `checkpoint-engine`（基础形态）。
2. `python -c "import checkpoint_engine; print(checkpoint_engine.__version__)"` 验证可导入，记录版本号。
3. `python examples/update.py --help` 抄录 12 个参数及默认值，标注哪两个参数互斥。
4. `python examples/update.py`（不带 torchrun），记录报错的异常类型与对应源码行号（预期是 [examples/update.py:187](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py#L187) 的 `int(os.getenv("RANK"))`）。
5. `pytest tests/ -m "not gpu" -v`，记录通过/跳过的用例数。
6. 补装 `'checkpoint-engine[p2p]'`，用 `pip list | grep mooncake` 前后对比，验证 4.1 的结论。

**路线 B（8 卡 H800/H20 + vLLM 0.10.2）——端到端体验**：严格按 [README.md:100-136](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/README.md#L100-L136) 执行：uv 建环境装 vLLM → 装 checkpoint-engine → 下载 `Qwen/Qwen3-235B-A22B-Instruct-2507` → 带 `--worker-extension-cls` 与 `--load-format dummy` 启动 vLLM → 另一终端 `torchrun --nproc-per-node 8 examples/update.py --update-method all --checkpoint-path <模型目录>`。

**验收记录表**（建议照抄填写）：

| 步骤 | 命令/文件 | 预期证据 | 实际结果 |
| --- | --- | --- | --- |
| A1 | `pip install checkpoint-engine` | 安装成功，无 mooncake | |
| A2 | `import checkpoint_engine` | 打印版本号 | |
| A3 | `--help` | 12 个参数，2 个互斥 | |
| A4 | 直接运行 | `RANK` 未设置的 TypeError | |
| A5 | `pytest -m "not gpu"` | CPU 用例全绿 | |
| B1 | vLLM 启动日志 | `/health` 就绪，端口 19730 | |
| B2 | torchrun 更新日志 | `Gather metas duration: X.XX seconds` | |
| B3 | torchrun 更新日志 | `Update weights without setting ranks duration: X.XX seconds` | |
| B4 | torchrun 更新日志 | `Update weights with setting ranks duration: X.XX seconds` | |

路线 B 的第 B2~B4 行可以直接和 README 基准表中 `Qwen3-235B-A22B-Instruct-2507 (BF16) 8xH800 TP8` 一行（GatherMetas 0.33s / Broadcast 3.47s / P2P 4.12s）对照，看你机器上的数字差多少。B 路线的具体耗时**待本地验证**。

## 6. 本讲小结

- 安装分两档：`pip install checkpoint-engine` 覆盖默认的 Broadcast 路径；`'checkpoint-engine[p2p]'` 额外装 `mooncake-transfer-engine>=0.3.5` 以支持 RDMA 的 P2P 更新。mooncake 是延迟导入的，基础安装不影响 `import checkpoint_engine`。
- vLLM 侧必须用 `--worker-extension-cls checkpoint_engine.worker.VllmColocateWorkerExtension` 注入接应类，且版本需含 `/collective_rpc` 端点（推荐 v0.10.2）；`--load-format dummy` 配合「启动后立刻灌权重」的用法省掉一次磁盘加载。
- 控制面与数据面分离：PS 通过 HTTP `/collective_rpc`（method=`update_weights_from_ipc`）通知 worker 主动连回 ZMQ socket，之后的大块权重数据走 ZMQ + CUDA IPC，不经过 HTTP。
- `examples/update.py` 是官方编排器：torchrun 拉起 N 个进程，按「有 index.json 且不在 /dev/shm」选择按张量或按文件均分 checkpoint，组首进程（`rank % P == 0`）负责发 HTTP 请求并轮询 `/health` 等 vLLM 就绪。
- 主流程调用序列是 `init_process_group → barrier → register_checkpoint → gather_metas → update(broadcast) → [sleep 2s] → update(p2p)`，`auto_pg=True` 让进程组在每次 update 后自动销毁重建。
- 测试验收：`pytest tests/ -m "not gpu"` 在纯 CPU 上可跑（marker 定义在 pyproject.toml）；`tests/test_update.py` 需要至少 2 张 GPU 且只能由 pytest 驱动（它内部自己拉 torchrun）。

## 7. 下一步学习建议

到这里你已经能「装得上、跑得起来、验得完」，但还不知道每行代码住在哪个文件里。下一讲 **u1-l3「目录结构与代码地图」** 会逐个介绍 `checkpoint_engine` 包下的 `ps.py`、`worker.py`、`data_types.py`、`pin_memory.py`、`device_utils.py`、`ipc_handler.py`，以及 `distributed/`、`xpu_ipc/` 子包、`examples/`、`tests/`、`patches/` 的职责，并画出 `ps.py` 对其他模块的依赖关系图。

在进入下一讲之前，建议先自己动手做两件事热身：

1. 用编辑器打开 [checkpoint_engine/ps.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py)，只看目录（函数/类的折叠视图），数一数有多少个 `_` 开头的私有方法——这会让你对第三单元要拆的内容有心理预期。
2. 重读 [README.md:20-43](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/README.md#L20-L43) 的「Optimized Weight Broadcast」与「Optimized P2P Bucket Assignment」两节，把本讲看到的 `ps.update` 日志和「三阶段流水线」「bucket 分配优化」的说法对应起来——这正是 u1-l4 要展开的整体架构图。
