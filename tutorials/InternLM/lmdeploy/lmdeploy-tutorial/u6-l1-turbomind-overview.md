# TurboMind 后端概览与 C++ 扩展

## 1. 本讲目标

学完本讲，你应该能够：

- 说清 TurboMind 与 PyTorch 后端各自的定位、分工与取舍，知道「为什么 lmdeploy 要维护两套引擎」。
- 看懂 `src/turbomind/` 这棵 C++ 目录树，能指出 `engine/`、`models/`、`kernels/` 各自的职责并各举一个代表文件。
- 理解 Python 侧 `lmdeploy/turbomind/turbomind.py` 如何通过 `_turbomind` 这个 pybind11 扩展「跨语言」调用 C++ 引擎，包括张量如何在两边互传。
- 读懂 C++ 入口类 `TurboMind`（`turbomind.h`）与 KV 缓存块管理器 `BlockManager`（`models/llama/BlockManager.h`）的接口设计。

本讲是「TurboMind 后端」单元（U6）的第一篇，只做**概览**：建立从 Python 到 C++ 的全局地图。至于 Python 包装的逐方法精读（u6-l2）、模型转换（u6-l3）、权重构建器（u6-l4）会在后续讲义展开。

## 2. 前置知识

阅读本讲前，建议你已经掌握以下概念（前序讲义已建立）：

- **两条后端，一个 Pipeline**（u1-l2、u3-l1）：用户只调 `pipeline()`，内部由 `archs.autoget_backend` 在 TurboMind 与 PyTorch 引擎间二选一。显式传 `PytorchEngineConfig` 可强制走 PyTorch。
- **Paged Attention / 分块 KV 缓存**（u4-l5）：把 KV cache 切成固定大小的 block，用「逻辑块 → 物理块」的映射表管理，是持续批处理的基础。本讲会看到它在 C++ 侧的对应实现。
- **arch 名字是模型身份证**（u2-l5、u3-l3）：后端选择、TurboMind 的 `SUPPORTED_ARCHS` 支持表，都靠 `config.json` 里的 `architectures` 字段查表。

几个本讲会用到的工程术语：

- **pybind11**：一个把 C++ 类/函数暴露给 Python 的库。C++ 侧写一个 `PYBIND11_MODULE(...)` 宏，编译后得到一个 `.so`，Python 用 `import` 就能调用。它就是 Python 和 C++ 之间的「翻译官」。
- **dlpack**：一个跨框架的张量交换标准。PyTorch 的 `torch.from_dlpack(x)` 和 TurboMind 的 `_tm.from_dlpack(x)` 都能从一个 dlpack「胶囊」零拷贝地拿到对方的张量，避免在语言边界来回搬运数据。
- **pimpl 惯用法**（pointer to implementation）：C++ 头文件里只放一个指向 `Impl` 私有结构体的指针，把真正的成员变量藏到 `.cc` 文件里。好处是改动实现不必重编所有引用方。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 语言 | 作用 |
| --- | --- | --- |
| [lmdeploy/turbomind/turbomind.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/turbomind/turbomind.py) | Python | TurboMind 的 Python 包装：建引擎、灌权重、发请求、收输出。 |
| [src/turbomind/turbomind.h](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/src/turbomind/turbomind.h) | C++ | TurboMind 引擎的顶层入口类声明。 |
| [src/turbomind/python/bind.cpp](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/src/turbomind/python/bind.cpp) | C++ | pybind11 桥接层，编译产物即 `_turbomind.so`。 |
| [src/turbomind/python/CMakeLists.txt](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/src/turbomind/python/CMakeLists.txt) | CMake | 声明 `_turbomind` 这个 pybind 模块的构建规则。 |
| [src/turbomind/engine/engine.h](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/src/turbomind/engine/engine.h) | C++ | 单卡推理引擎 `Engine` 的声明。 |
| [src/turbomind/models/llama/BlockManager.h](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/src/turbomind/models/llama/BlockManager.h) | C++ | 分块 KV 缓存的块管理器（说明见 4.4）。 |
| [lmdeploy/turbomind/supported_models.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/turbomind/supported_models.py) | Python | TurboMind 支持的 arch 白名单 `SUPPORTED_ARCHS`。 |

> 说明：本讲规划里写的 `src/turbomind/engine/BlockManager.h` 在仓库中并不存在。真实的 `BlockManager.h` 位于 `src/turbomind/models/llama/BlockManager.h`，`engine/` 目录下没有同名文件。本讲按真实路径讲解。

## 4. 核心概念与源码讲解

### 4.1 TurboMind 的定位与分工

#### 4.1.1 概念说明

lmdeploy 维护**两套推理引擎**，共用一个 `pipeline()` 入口：

- **PyTorch 引擎**（u3–u5 详解）：纯 Python，靠「加载 HF 模型 → 动态 patch 层」实现，开发门槛低、对新模型友好，但受 Python 与 PyTorch eager 开销限制。
- **TurboMind 引擎**（本单元）：C++/CUDA 手写的高性能后端，追求**极致吞吐与延迟**。它脱胎于早期的 FasterTransformer 思路，由 OpenMMLab 持续重写演进。README 的 Latest News 记录了它从 [2023/07 支持 Llama-2](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/README.md#L88) 起步，逐步加入 Paged Attention、Flash Decoding、W4A16、KV8 等特性。

一句话区分：**PyTorch 后端为「易读易扩展」服务，TurboMind 为「性能」服务**。这也决定了它们对模型的支持面不同——TurboMind 只支持一张固定的架构白名单，因为每个模型都需要在 C++ 侧专门接好。

#### 4.1.2 核心流程：后端如何落到 TurboMind

承接 u3-l1 的 `Pipeline.__init__` 五步走，当 `autoget_backend` 判定走 turbomind 时，链路是：

```text
pipeline(model_path)
  └─ AsyncEngine(engine='turbomind')
       └─ TurboMind.from_pretrained(model_path)        # Python 包装
            └─ TurboMind.__init__ → _from_hf()
                 ├─ is_supported(model_path)            # 校验 arch 是否在白名单
                 ├─ get_tm_config(...)                  # 生成 tm 配置
                 ├─ _tm.TurboMind.create(model_dir, ec) # 跨进 C++
                 │      └─ C++ TurboMind::TurboMind(...) 构造 Engine + 模型 + BlockManager
                 ├─ model_loader.export()               # 把 HF 权重灌进 C++
                 └─ _create_engine()                    # 启动每卡的推理引擎
```

注意第三步 `is_supported` 是一道**前置闸门**：不在白名单里的模型会直接被拒绝并提示改用 PyTorch 引擎。

#### 4.1.3 源码精读：TurboMind 支持哪些模型

`is_supported` 的判定逻辑很直白：要么模型目录里已经有转换好的 `triton_models/`，要么 arch 命中 `SUPPORTED_ARCHS` 白名单（`smooth_quant` 除外）：

[ lmdeploy/turbomind/supported_models.py:L7-L32 ](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/turbomind/supported_models.py#L7-L32) 定义了 arch → TurboMind 模型名的映射表，例如 `LlamaForCausalLM='llama'`、`Qwen3ForCausalLM='qwen3'`、`InternVLChatModel='internvl'`。

[ supported_models.py:L35-L72 ](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/turbomind/supported_models.py#L35-L72) 的 `is_supported` 先查 `triton_models/` 目录是否存在，再回退到 arch 查表；`Glm4MoeLiteForCausalLM` 带 vision 配置时会被排除。

在 `_from_hf` 里，这个闸门以断言形式出现：

[ lmdeploy/turbomind/turbomind.py:L211-L216 ](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/turbomind/turbomind.py#L211-L216) `assert is_supported(...)` 失败时报错信息明确写着「Plz try pytorch engine instead」——这就是 TurboMind 与 PyTorch 后端分工的兜底出口。

#### 4.1.4 代码实践：探测一个模型该走哪个后端

1. **实践目标**：不实际加载权重，只靠 `archs.autoget_backend` 判断某个 HF 模型目录会被分到 TurboMind 还是 PyTorch。
2. **操作步骤**：写一个脚本，对一个本地 HF 模型目录（或已下载的 `Qwen/Qwen2.5-7B-Instruct`）调用 `lmdeploy.archs.autoget_backend(model_path)`，打印返回值；再调用 `lmdeploy.turbomind.is_supported(model_path)` 看是否被 TurboMind 接受。
3. **需要观察的现象**：`autoget_backend` 返回 `'turbomind'` 或 `'pytorch'`，与 `is_supported` 的布尔结果应一致（在 TurboMind 扩展已编译安装的前提下）。
4. **预期结果**：Qwen2.5/Llama/InternLM2 等常见模型在安装了 `_turbomind` 时返回 turbomind；若用 `DISABLE_TURBOMIND=1` 安装（见 u1-l3），则因扩展缺失而回退 pytorch。
5. 若本地无 GPU/未编译 `_turbomind`，则此脚本会因 `import _turbomind` 失败而报错——属正常，此时只阅读源码即可，标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 TurboMind 要维护一张固定的 `SUPPORTED_ARCHS` 白名单，而 PyTorch 后端不需要？

> **答案**：TurboMind 为每个模型在 C++ 侧专门接好权重布局与算子，新增模型意味着改 C++ 源码并重新编译；而 PyTorch 后端靠「加载 HF 模型 + 动态 patch 层」（u3-l3），任意 HF 模型只要能加载就能 patch，因此无需固定白名单。

**练习 2**：`is_supported` 在什么情况下会直接返回 `False`？

> **答案**：当 `config.json` 里检测到 `quant_method == 'smooth_quant'` 时直接返回 `False`（[supported_models.py:L62-L64](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/turbomind/supported_models.py#L62-L64)）；以及 `Glm4MoeLiteForCausalLM` 带有 `vision_config` 时。

### 4.2 TurboMind C++ 源码结构

#### 4.2.1 概念说明

TurboMind 的全部 C++/CUDA 源码集中在仓库的 `src/turbomind/` 目录（回顾 u1-l3：它由 CMake 编译成 `_turbomind` 扩展装入 `lmdeploy/lib/`）。要读懂 TurboMind，第一步是建立这棵目录树的全局地图。它按「职责分层」组织，层与层之间是单向依赖：高层调度调用低层算子，低层不感知业务。

#### 4.2.2 核心流程：目录分层与代表文件

```text
src/turbomind/
├── turbomind.h           # 顶层入口类 TurboMind（4.3 节详讲）
├── core/                 # 张量、内存分配器、Stream、Module 基础设施
├── engine/               # 推理引擎：调度、请求队列、批处理、执行器
├── models/               # 模型实现（权重描述 + 前向）
│   ├── llama/            #   Llama/Qwen/InternLM 等共享的统一实现（含 BlockManager）
│   └── qwen3_5vit/       #   Qwen3.5-VL 视觉部分
├── kernels/              # CUDA 算子：attention / gemm / norm / sampling
├── comm/                 # 多卡通信后端：nccl / gloo / cuda_ipc
├── generation/           # 采样配置等生成相关
├── python/               # pybind11 桥接（bind.cpp → _turbomind.so）
└── utils/                # 日志、CUDA 工具、metrics
```

三层各取一个代表文件，帮助你在脑海里定位：

| 层 | 代表文件 | 职责一句话 |
| --- | --- | --- |
| engine/ | [`engine/engine.h`](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/src/turbomind/engine/engine.h) | 单卡推理引擎 `Engine`，持有语言模型与权重，提供 `Start()`、`GetScheduleMetrics()`。 |
| models/ | [`models/llama/unified_decoder.h`](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/src/turbomind/models/llama/unified_decoder.h) | 把若干 `unified_attention_layer` + FFN 层堆成 Transformer decoder。 |
| kernels/ | [`kernels/sampling_kernels.cu`](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/src/turbomind/kernels/sampling_kernels.cu) | top-k / top-p / 采样等 CUDA kernel，是生成阶段的最后一步。 |

> engine/ 目录的真实成员还有：`gateway.h`（请求网关）、`request.h`/`request_queue.h`（请求与队列）、`batch.h`（批构造）、`model_executor.h`（执行器）、`signal_buffer.h`（信号缓冲，用于流式回调）。它们共同实现「持续批处理」的 C++ 版本，对应 PyTorch 侧的 scheduler（u4-l4）。

#### 4.2.3 源码精读：C++ 入口类 TurboMind

[ src/turbomind/turbomind.h:L18-L61 ](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/src/turbomind/turbomind.h#L18-L61) 声明了顶层类 `TurboMind`。它的接口是一组与 Python 包装一一对应的动词：

- 构造函数 `TurboMind(std::string model_dir, EngineConfig config, FFICtxFactory ffi_ctx_factory)` —— 建好整个引擎。
- `CreateContext(int index)` / `CreateRoot(int index)` —— 为第 `index` 号 GPU 建上下文与权重树根。
- `ProcessWeights(int index)` / `CreateEngine(int index)` —— 处理权重、启动该卡的推理引擎。
- `CreateRequest()` —— 产出一个 `ModelRequest`，用于发起一次推理。
- `Sleep` / `WakeUp` / `GetScheduleMetrics` —— 休眠唤醒（PD 分离相关）、取调度指标。

注意头文件末尾的 pimpl 惯用法：

```cpp
private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
```

[ turbomind.h:L58-L60 ](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/src/turbomind/turbomind.h#L58-L60) 把所有真实成员藏进 `Impl`，头文件保持稳定——这是大型 C++ 项目控制编译依赖的常见手段。

#### 4.2.4 代码实践：浏览 C++ 目录树（源码阅读型）

1. **实践目标**：建立 `src/turbomind/` 的心智地图，能按层说出代表文件。
2. **操作步骤**：
   - 在仓库根执行 `find src/turbomind -maxdepth 2 -type d`，对照 4.2.2 的树状图核对每个子目录。
   - 进入 `src/turbomind/engine/`，列出全部文件，挑出与「请求/批/队列」相关的三个文件。
   - 进入 `src/turbomind/kernels/`，找出 `attention/`、`gemm/`、`norm/` 三个子目录各一个 `.cu` 或 `.h` 文件。
3. **需要观察的现象**：`engine/` 里没有 `BlockManager.h`（它在 `models/llama/`）；`kernels/` 下既有按算子类别分的子目录，也有平铺的 `sampling_kernels.cu`、`activation.cu` 等文件。
4. **预期结果**：你应当能凭目录名判断每个文件属于哪一层，不再需要逐个打开。

#### 4.2.5 小练习与答案

**练习 1**：`core/` 目录里的 `Module`（见 `core/module.h`）在 TurboMind 架构中扮演什么角色？

> **答案**：它是 C++ 侧的「模块基类」，提供 `get/child/param/create_child` 等导航与权重挂载接口（pybind 后暴露给 Python，见 4.3）。模型被组织成一棵 `Module` 树，权重（`LinearWeight` 等）挂在该树的叶节点上，权重加载（u6-l3）通过遍历这棵树完成。

**练习 2**：`comm/` 目录下为什么有 `nccl/`、`gloo/`、`cuda_ipc/` 三个子目录？

> **答案**：它们是多卡/多机张量并行的三种通信后端。NCCL 用于 GPU 间集合通信（最常用），Gloo 用于跨机 CPU 协调，CUDA IPC 用于单机同 GPU 进程间的显存共享。不同部署拓扑选不同后端。

### 4.3 Python 与 C++ 的桥接：pybind11 `_turbomind`

#### 4.3.1 概念说明

TurboMind 的「大脑」是 C++ 写的，但用户用的是 Python。中间那座桥就是 `_turbomind`——一个 pybind11 编译出来的扩展模块（Linux 上是 `_turbomind.so`）。它做三件事：

1. 把 C++ 的 `TurboMind`、`ModelRequest`、`Tensor`、各种 `Config` 结构体**包装成 Python 类**。
2. 提供 `from_dlpack` 让 Python 张量零拷贝进入 C++，反向亦然。
3. 把 Python 传入的参数（引擎配置、采样配置）逐字段填进 C++ 的同名结构体。

理解这座桥，是理解 TurboMind 后端「为什么既能跑得快、又能用 Python 调」的关键。

#### 4.3.2 核心流程：一次推理的跨语言往返

```text
Python TurboMindInstance            C++ ModelRequest
   │                                       │
   │  prepare_inputs → dict[input_ids,...] │
   │  ──_np_dict_to_tm_dict────────────▶   │  (dlpack 把 torch 张量变 C++ Tensor)
   │                                       │
   │  model_inst.forward(                  │
   │    tensors, mm_inputs, session,       │
   │    gen_cfg, stream_output, ...)       │
   │  ──────────────────────────────────▶  │  Forward() 进 engine 跑 forward
   │                                       │
   │  ◀──────────────────────────────────  │  返回 (tensors, state, metrics)
   │  _tm_dict_to_torch_dict               │
   │  (torch.from_dlpack 变回 torch 张量)  │
```

注意 `forward` 在 pybind 绑定时用了 `py::call_guard<py::gil_scoped_release>()`——也就是**进入 C++ 计算时释放 GIL**，这样 Python 的其它线程（如流式回调）能继续跑，是异步流式输出能成立的前提。

#### 4.3.3 源码精读：Python 侧如何 import _turbomind

[ lmdeploy/turbomind/turbomind.py:L27-L33 ](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/turbomind/turbomind.py#L27-L33) 是关键的一段：

```python
# TODO: find another way import _turbomind
lmdeploy_dir = osp.split(lmdeploy.__file__)[0]
sys.path.append(osp.join(lmdeploy_dir, 'lib'))
import _turbomind as _tm  # noqa: E402
import _xgrammar as _xgr  # noqa: E402
```

它把 `lmdeploy/lib/` 临时加入 `sys.path`，再 `import _turbomind`。注释 `TODO: find another way` 说明这是一种权宜之计——因为 `_turbomind.so` 是构建产物，安装时落在 `lmdeploy/lib/` 而非标准 `site-packages`，所以得手动加路径。这也呼应 u1-l3：`_turbomind` 由 CMake 编译并装入 `lmdeploy/lib/`。

`_tm` 这个别名之后贯穿全文件，例如 `_tm.TurboMind.create`、`_tm.EngineConfig()`、`_tm.GenerationConfig()`、`_tm.SessionParam`、`_tm.from_dlpack`。

#### 4.3.4 源码精读：张量的零拷贝互传

两端的张量靠 dlpack 互传，互转函数只有几行：

[ lmdeploy/turbomind/turbomind.py:L48-L65 ](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/turbomind/turbomind.py#L48-L65)：

```python
def _np_dict_to_tm_dict(np_dict: dict):
    ret = _tm.TensorMap()
    for k, v in np_dict.items():
        ret[k] = _tm.from_dlpack(v)   # torch/numpy → C++ Tensor
    return ret

def _tm_dict_to_torch_dict(tm_dict: _tm.TensorMap):
    ret = dict()
    for k, v in tm_dict.items():
        if v.type == _tm.DataType.TYPE_UINT32:
            v = v.view(_tm.DataType.TYPE_INT32)
        ret[k] = torch.from_dlpack(v)  # C++ Tensor → torch
    return ret
```

注意 `_tm_dict_to_torch_dict` 里对 `TYPE_UINT32 → TYPE_INT32` 的 `view`：C++ 侧某些索引张量按无符号 32 位存放，但 PyTorch 习惯用 `int32`，这里做了一次**零拷贝视图重解释**而非真正转换。C++ 侧对应的 `from_dlpack` 与 `Tensor.__dlpack__` 实现见 [ bind.cpp:L558-L571 ](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/src/turbomind/python/bind.cpp#L558-L571)。

#### 4.3.5 源码精读：配置如何逐字段搬进 C++

Python 的 `TurbomindEngineConfig`（u2-l3）和 C++ 的 `EngineConfig` 是两个独立结构体，pybind 不会自动转换，得手写搬运。[ turbomind.py:L231-L263 ](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/turbomind/turbomind.py#L231-L263) 逐行赋值：

```python
ec = _tm.EngineConfig()
ec.data_type = dtype_map[engine_config.dtype]
ec.cache_block_seq_len = engine_config.cache_block_seq_len
ec.quant_policy = engine_config.quant_policy
ec.max_batch_size = engine_config.max_batch_size
# ... 一长串字段对齐 ...
model_comm = _tm.TurboMind.create(model_dir='', engine_config=ec)
```

最后一行 `_tm.TurboMind.create(...)` 正式跨进 C++，调用的是 pybind 绑定的静态方法 `create`（见下方 bind.cpp）。采样配置 `GenerationConfig` 也走同样模式：[ turbomind.py:L833-L863 ](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/turbomind/turbomind.py#L833-L863) 的 `_get_generation_config` 把 Python 的 `GenerationConfig` 逐字段填进 `c = _tm.GenerationConfig()`。

#### 4.3.6 源码精读：bind.cpp 的绑定全貌

C++ 侧的桥接层在 [ src/turbomind/python/bind.cpp ](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/src/turbomind/python/bind.cpp)，入口是宏：

[ bind.cpp:L332-L333 ](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/src/turbomind/python/bind.cpp#L332-L333) `PYBIND11_MODULE(_turbomind, m)` 声明模块名为 `_turbomind`，`m` 是用来注册类/函数的句柄。

最核心的几处绑定：

- [ bind.cpp:L736-L754 ](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/src/turbomind/python/bind.cpp#L736-L754) 把 `TurboMind` 暴露为 Python 类，静态方法 `create(model_dir, engine_config)` 返回 `shared_ptr<TurboMind>`，并特别用 `ScopedGIL` 工厂保存 GIL 状态（因为 C++ 内部会回调 Python）。
- [ bind.cpp:L755-L796 ](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/src/turbomind/python/bind.cpp#L755-L796) 绑定 `create_request / create_context / create_root / process_weight / create_engine / get_schedule_metrics` 等方法——它们与 4.2.3 的 `turbomind.h` 接口一一对应。
- [ bind.cpp:L573-L632 ](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/src/turbomind/python/bind.cpp#L573-L632) 绑定 `ModelRequest` 的 `forward / cancel / end / set_grammar`，其中 `forward` 的签名正是 4.3.2 流程图里那串参数。

CMake 侧，[ python/CMakeLists.txt:L3 ](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/src/turbomind/python/CMakeLists.txt#L3) 用 `project(_turbomind ...)` 把模块名定为 `_turbomind`，[ :L15 ](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/src/turbomind/python/CMakeLists.txt#L15) `pybind11_add_module(${PROJECT_NAME} bind.cpp)` 编译 `bind.cpp` 为扩展，并 [ :L16 ](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/src/turbomind/python/CMakeLists.txt#L16) 链接 `turbomind` 与 `xgrammar` 两个 C++ 静态库——这就是「`_turbomind.so` = bind.cpp 胶水 + 整个 TurboMind C++ 实现」的由来。

#### 4.3.7 代码实践：定位 import 与追踪一次 forward

1. **实践目标**：在源码中验证「Python 调用 → C++ 绑定 → C++ 实现」的三段对应关系。
2. **操作步骤**：
   - 在 `lmdeploy/turbomind/turbomind.py` 中找到 `import _turbomind as _tm` 所在行（应为第 30 行），确认它前面有 `sys.path.append(.../lib)`。
   - 全文搜索 `_tm.TurboMind.create`，确认它出现在 `_from_hf` 中；再到 `bind.cpp` 找到 `"create"` 的 `def_static` 绑定，对照参数名 `model_dir` / `engine_config`。
   - 全文搜索 `model_inst.forward(`，定位到 `async_stream_infer`（约第 768 行），再到 `bind.cpp` 找到 `ModelRequest` 的 `"forward"` 绑定，核对参数顺序：`input_tensors, mm_inputs, session, gen_cfg, stream_output, enable_metrics, cb`。
3. **需要观察的现象**：Python 侧的每个 `_tm.*` 调用，都能在 `bind.cpp` 里找到一个同名绑定；参数名一致。
4. **预期结果**：你会看到这座「桥」其实是高度对称的——Python 包装只是搬运参数与张量，真正的计算全在 C++。
5. 若想验证运行：在已编译 TurboMind 的环境里 `python -c "import lmdeploy; print(lmdeploy.__file__)"`，再到同目录 `lib/` 下 `ls` 应能看到 `_turbomind*.so`。否则标注「待本地验证」。

#### 4.3.8 小练习与答案

**练习 1**：为什么 `model_inst.forward` 在 pybind 绑定时用了 `py::call_guard<py::gil_scoped_release>()`？

> **答案**：`forward` 是耗时很长的 C++ 推理。进入它之前释放 GIL，Python 其它线程就能同时运行（例如 TurboMind 的流式信号回调 `async_signal_cb`），从而实现真正的异步流式输出。若不释放 GIL，整个 Python 进程会在 forward 期间被阻塞。

**练习 2**：`_tm_dict_to_torch_dict` 里为什么要把 `TYPE_UINT32` `view` 成 `TYPE_INT32`？

> **答案**：C++ 侧部分索引/ID 张量按无符号 32 位存放，但下游 PyTorch 与 lmdeploy 约定用 `int32`。`view` 是零拷贝的位模式重解释（同样的 4 字节换个类型理解），不产生数据拷贝，比重新转换高效。

### 4.4 BlockManager：分块 KV 缓存的 C++ 实现

#### 4.4.1 概念说明

TurboMind 的核心特性之一是 **Paged Attention**：把 KV cache 切成固定大小的 block 来管理，从而支持持续批处理与显存复用（原理见 u4-l5 的 PyTorch 版）。TurboMind 在 C++ 侧用一个 `BlockManager` 类做这件事。

> 路径提示：本讲规划写的是 `src/turbomind/engine/BlockManager.h`，但仓库里该文件不存在。真正的 `BlockManager.h` 在 [ `src/turbomind/models/llama/BlockManager.h` ](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/src/turbomind/models/llama/BlockManager.h)（同目录还有配套的 `BlockTrie` 前缀缓存、`SequenceManager` 序列管理）。它被 Llama/Qwen/InternLM 等统一实现复用，因此放在 `models/llama/` 下。

#### 4.4.2 核心流程：块的三态生命周期

每个 KV 块在 `BlockManager` 里有三种状态，靠 `use_count`（被多少序列引用）与 `timestamp`（最后写入时间）区分：

```text
            Allocate(count)                 Free/Lock ref=0
   free ─────────────────────▶ active ─────────────────────▶ cached
   (空闲)    use_count:=1           (在用)      use_count:=0            (缓存)
                                 ◀────────────────────────
                                       Lock(use_count+=1)
   cached ──── Evict(count) ──▶ free   (LRU 驱逐：timestamp 最旧者优先)
```

- `free`：从未写入或已被驱逐，可立即分配。
- `active`：至少有一个序列正在用它（`use_count > 0`）。
- `cached`：没有序列在用，但内容还在（`timestamp != 0`），可被前缀缓存复用，也可在显存紧张时按 LRU 驱逐。

这套设计与 PyTorch 侧 `DefaultBlockManager` + `BlockTrie`（u4-l5、u9-l3）是同一思想的两语言实现。

#### 4.4.3 源码精读：Block 结构与状态谓词

[ models/llama/BlockManager.h:L27-L41 ](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/src/turbomind/models/llama/BlockManager.h#L27-L41) 定义单个块：

```cpp
struct Block {
    int      id;         // fixed linear id in the pool
    int      use_count;  // active sequences using the block
    uint64_t unique_id;  // unique for every block allocation
    uint64_t timestamp;
    void*    data;       // 指向真实 KV 显存的指针
};
```

`id` 是块池中的线性编号，`data` 是 KV 显存的裸指针，`unique_id` 在每次分配时唯一，用于跨进程/前缀缓存的安全校验。

[ BlockManager.h:L46-L60 ](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/src/turbomind/models/llama/BlockManager.h#L46-L60) 三个谓词精确刻画三态：

```cpp
inline bool is_active(const Block& b) { return b.use_count > 0; }
inline bool is_cached(const Block& b) { return b.use_count == 0 && b.timestamp != 0; }
inline bool is_free(const Block& b)   { return b.use_count == 0 && b.timestamp == 0; }
```

#### 4.4.4 源码精读：BlockManager 的接口

[ BlockManager.h:L71-L98 ](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/src/turbomind/models/llama/BlockManager.h#L71-L98) 暴露的方法就是 4.4.2 状态机里的那些转换：

| 方法 | 转换 | 用途 |
| --- | --- | --- |
| `Allocate(count)` | free → active | 申请新块，返回块 id 与 unique id |
| `Lock(ids)` | cached → active | 复用一块缓存（前缀命中） |
| `Unlock(ids)` | active → cached | 序列释放，但内容保留 |
| `Evict(count)` | cached → free | 显存不足时按 LRU 驱逐 |
| `Free(bs)` | cached → free | 引用计数归零时彻底回收 |
| `Touch(bs)` | 更新 timestamp | 标记最近使用，影响 LRU 顺序 |
| `TakeSnapshot()` | — | 给 metrics 报告 active/cached/free 计数 |

构造函数 `BlockManager(block_size, block_count, chunk_size, allocator, get_free_size)` 用 `block_size`（每块的 token 数）与显存比例算出能开多少块，并用 `Malloc()` 分块地从 GPU 申请大块显存（`chunks_`），再细分成 `Block`——这正是 Paged Attention「按需分配、按块管理」的物理实现。

#### 4.4.5 一点数学：每块 KV 占多少显存

承接 u4-l5 的估算，单个 block 的 KV 显存字节数为：

\[
\text{bytes\_per\_block} = 2 \times L \times B \times H_{kv} \times D \times \text{sizeof}(\text{dtype})
\]

其中 \(L\) 为层数，\(B\) 为 `block_size`（每块 token 数），\(H_{kv}\) 为 KV 头数，\(D\) 为每头维度，因子 2 对应 K 与 V 两个张量，`dtype` 通常为 fp16/bf16（2 字节）。`BlockManager` 用 `GetBlockCount(block_size, ratio, get_free_size)`（[ BlockManager.h:L138 ](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/src/turbomind/models/llama/BlockManager.h#L138)）把「剩余显存比例 `ratio`」换算成「可分配块数」，即 u2-l3 里 `cache_max_entry_count` 的 C++ 落点。

#### 4.4.6 代码实践：用 snapshot 理解三态计数

1. **实践目标**：把 `BlockManager` 的三态计数与 `ScheduleMetrics` 对应起来。
2. **操作步骤**：阅读 [ BlockManager.h:L100-L123 ](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/src/turbomind/models/llama/BlockManager.h#L100-L123) 的 `total_count/active_count/cached_count/free_count`；再到 [ bind.cpp:L381-L390 ](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/src/turbomind/python/bind.cpp#L381-L390) 看 `ScheduleMetrics` 如何暴露给 Python；最后到 [ turbomind.py:L390-L398 ](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/turbomind/turbomind.py#L390-L398) 的 `get_schedule_metrics` 看 C++ 指标如何映射成用户面 `ScheduleMetrics`。
3. **需要观察的现象**：`total_blocks = active_blocks + cached_blocks + free_blocks` 恒成立；`active_blocks` 随并发请求数上升，`free_blocks` 相应下降。
4. **预期结果**：你能解释一次推理时，prefill 阶段 `active_blocks` 增加、请求结束后这些块转入 `cached_blocks`（供前缀缓存复用），显存紧张时再被 `Evict` 成 `free_blocks`。
5. 运行层面：启动服务后访问调度指标需配合 serve 层（u8）；若仅本地阅读，标注「待本地验证」。

#### 4.4.7 小练习与答案

**练习 1**：`cached` 态的块为什么不会被立即回收？它的存在意义是什么？

> **答案**：`cached` 块代表「没人在用，但内容还在显存里」的内容，存在意义是**前缀缓存复用**——新的请求若命中相同前缀，直接 `Lock` 把它转回 active，免去重新算 prefill 的 KV。只有显存不足时才用 `Evict` 按 LRU 把最旧的 cached 块清成 free。这正是 `enable_prefix_caching` 提速多轮/重复 system prompt 的物理基础。

**练习 2**：`unique_id` 字段（每次分配递增）相对于 `id`（池内线性号）有什么额外作用？

> **答案**：`id` 会被回收复用（一块释放后其 id 可再分配给别人），无法唯一标识「某次具体分配」。`unique_id` 单调递增、永不复用，配合 `Verify(block_ids, unique_ids)`（[BlockManager.h:L96](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/src/turbomind/models/llama/BlockManager.h#L96)）可校验「引用的块是否还是当初那一块」，在前缀缓存与跨进程 KV 传输（PD 分离）里防止用错块。

## 5. 综合实践

把本讲三个最小模块串起来，画一张**「Python 调用 → pybind 桥接 → C++ 引擎 → BlockManager」的端到端追踪图**，并完成下列子任务：

1. **目录与桥接（4.2 + 4.3）**：浏览 `src/turbomind/` 目录树，按 engine/models/kernels 各列一个代表文件；在 `lmdeploy/turbomind/turbomind.py` 中标注 `import _turbomind as _tm` 的行号，并在 `bind.cpp` 中找到与之对应的 `PYBIND11_MODULE(_turbomind, m)`。
2. **跨语言调用链（4.3）**：以一次 `async_stream_infer` 为线索，在源码里依次标出：
   - `prepare_inputs`（构造输入 dict）→ `_np_dict_to_tm_dict`（dlpack 转 C++）→ `model_inst.forward(...)`（[turbomind.py:L768-L769](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/turbomind/turbomind.py#L768-L769)）；
   - 在 `bind.cpp` 中找到 `forward` 绑定，指出它用了 `py::call_guard<py::gil_scoped_release>()`；
   - 返回值经 `_tm_dict_to_torch_dict` 转回 torch 张量。
3. **块管理（4.4）**：阅读 `models/llama/BlockManager.h`，用自己的话写出 free/active/cached 三态的转换条件与对应方法名。
4. **输出**：把上述追踪画成一张含「Python 层 / pybind 层 / C++ 层」三泳道的流程图（可用文字描述每个箭头对应的函数与行号）。

> 这是一个纯源码阅读型实践（TurboMind 需编译 C++/CUDA，运行验证依赖具体环境）。如果你有可用的 GPU 环境且已 `pip install lmdeploy`，可额外用 `pipeline('Qwen/Qwen2.5-7B-Instruct')` 跑一次推理，确认默认后端确实是 turbomind；否则把运行部分标注「待本地验证」，只交付源码追踪。

## 6. 本讲小结

- TurboMind 是 lmdeploy 的 C++/CUDA 高性能后端，与纯 Python 的 PyTorch 后端**分工互补**：一个为性能、一个为易扩展，二者共用一个 `pipeline()` 入口。
- TurboMind 只支持 `SUPPORTED_ARCHS` 白名单内的模型（`supported_models.py`），不在表里的会被 `is_supported` 拒绝并提示改用 PyTorch 引擎。
- `src/turbomind/` 按 `core / engine / models / kernels / comm / python` 分层；C++ 入口类 `TurboMind`（`turbomind.h`）用 pimpl 惯用法隐藏实现。
- Python 与 C++ 靠 pybind11 扩展 `_turbomind` 桥接：`bind.cpp` 把 C++ 类绑成 Python 类，张量经 dlpack 零拷贝互传，配置逐字段搬运；`forward` 在进入 C++ 时释放 GIL 以支持异步流式。
- 分块 KV 缓存在 C++ 侧由 `models/llama/BlockManager.h` 的 `BlockManager` 管理，每个块有 free/active/cached 三态，靠 `use_count` 与 `timestamp` 区分，支撑 Paged Attention 与前缀缓存。
- 本讲规划里提到的 `src/turbomind/engine/BlockManager.h` 实际不存在，真实文件在 `src/turbomind/models/llama/BlockManager.h`。

## 7. 下一步学习建议

本讲只搭了 TurboMind 的「全局脚手架」。建议下一步：

- **u6-l2 Python 包装精读**：逐方法读 `TurboMind` / `TurboMindInstance`，搞清 `_from_hf`、`prepare_inputs`、`async_stream_infer` 的完整细节，以及 dlpack 互操作的具体场景。
- **u6-l3 模型转换 converter**：看 HF 模型如何转成 TurboMind 的 `triton_models/` 权重目录，理解 `lmdeploy convert` 背后做的事。
- **u6-l4 builders**：深入 `lmdeploy/turbomind/builders/`，看 Python 侧如何**描述**模型结构（attention/ffn/moe/mla）再交给 C++ 组装权重。
- 想对照 PyTorch 版的同类机制，可回顾 u4-l5（`DefaultBlockManager`）与 u4-l4（Scheduler），比较两套引擎在「块管理 + 调度」上的同与异。
