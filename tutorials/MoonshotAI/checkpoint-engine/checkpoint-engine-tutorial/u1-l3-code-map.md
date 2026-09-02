# u1-l3 目录结构与代码地图:每个文件负责什么

## 1. 本讲目标

学完本讲,你应该能够:

1. 拿出一张完整的仓库目录树,说出**每个目录、每个源码文件**的职责。
2. 画出 `ps.py` 对项目内其他模块的**依赖关系图**,并区分哪些依赖是「import 时立即生效」、哪些是「运行时延迟导入」。
3. 区分**服务端代码**(`ps.py` 及其依赖,运行在训练侧进程里)与**消费端代码**(`worker.py`,运行在推理引擎进程里),理解为什么这两侧必须分属两个文件。
4. 掌握两种自己动手「画地图」的工具:`git ls-files` / `wc -l` 看静态结构,`inspect` / `grep` 看类与方法布局。

本讲是全手册的「地图页」:后续每一讲深入某个文件时,你都可以回到本讲的依赖图,确认「我现在在哪里」。

## 2. 前置知识

本讲不需要任何分布式或 GPU 知识,但建议先读完 [u1-l1 项目概览](u1-l1-project-overview.md) 和 [u1-l2 环境搭建](u1-l2-setup-and-first-run.md)。这里补充几个 Python 工程概念:

- **包(package)与 `__init__.py`**:Python 里一个目录加上 `__init__.py` 就是一个包。`__init__.py` 在 `import 包名` 时**第一个被执行**,通常用来决定「包对外暴露哪些名字」。本项目的 `checkpoint_engine/__init__.py` 就是一个纯粹的「门面」:它自己几乎不写逻辑,只做再导出(re-export)。
- **导入即依赖**:`from checkpoint_engine.data_types import ParameterMeta` 这一行同时说明了两件事——`ps.py` 用到了 `ParameterMeta`,且 import `ps.py` 时必须能成功 import `data_types.py`。因此读一个文件的 import 块,就能读出它的依赖边。
- **延迟导入(lazy import)**:把 `import` 写在函数体或 `if` 分支内部,只有真正走到那行代码时才加载模块。本项目大量使用这个技巧来隔离**可选依赖**(mooncake、vLLM、torch_npu、XPU 扩展),这就是「装了基础包、没装 `[p2p]` extra 也能 `import checkpoint_engine`」的原因(u1-l2 已验证)。
- **pydantic `BaseModel`**:一个「带类型校验的数据类」。继承它的类字段会被自动校验、可转 JSON。本项目所有跨进程传递的元数据都定义成 pydantic 模型。
- **抽象基类(ABC)**:只定义方法签名、不写实现的父类,子类必须实现全部 `@abstractmethod` 才能实例化。本项目用 ABC 描述「同一件事的多种硬件实现」(IPC、通信后端)。
- **服务端 / 消费端视角**:承接 u1-l1 的结论——checkpoint-engine 夹在训练侧与推理引擎之间。**服务端(提供权重的一方)** 代码在 `ps.py`,实例化成 `ParameterServer`;**消费端(接收权重的一方)** 代码在 `worker.py`,由 vLLM 通过 worker extension 机制注入后调用。两侧**不在同一个进程里**,只通过 ZMQ 消息和设备间 IPC 句柄交流。

## 3. 本讲源码地图

先用一张表建立直觉(行数为当前 HEAD 的统计值):

| 文件 | 行数 | 一句话职责 | 侧 |
|---|---:|---|---|
| `checkpoint_engine/__init__.py` | 40 | 包门面:再导出公共 API | 共用 |
| `checkpoint_engine/ps.py` | 947 | 服务端总装:`ParameterServer` 主类与广播/P2P 更新主流程 | 服务端 |
| `checkpoint_engine/worker.py` | 231 | 消费端:`update_weights_from_ipc` REP 循环 + vLLM worker 扩展 | 消费端 |
| `checkpoint_engine/data_types.py` | 111 | 全项目共享的 pydantic 数据模型 | 共用 |
| `checkpoint_engine/pin_memory.py` | 401 | checkpoint 文件加载、TP 拼接、锁页内存 | 服务端 |
| `checkpoint_engine/ipc_handler.py` | 134 | 设备内存 IPC 句柄的导出/挂载抽象(CUDA 与 XPU 两种实现) | 共用 |
| `checkpoint_engine/device_utils.py` | 312 | `DeviceManager` 多硬件抽象 + RDMA 网卡发现 | 共用 |
| `checkpoint_engine/p2p_store.py` | 78 | 对 mooncake TransferEngine 的薄封装 | 服务端 |
| `checkpoint_engine/api.py` | 108 | FastAPI 端点 + 请求推理引擎更新的 HTTP 客户端 | 服务端 |
| `checkpoint_engine/__main__.py` | 28 | `python -m checkpoint_engine` 入口:UDS 方式启动 HTTP 服务 | 服务端 |
| `checkpoint_engine/distributed/` | 905 | 通信后端抽象:base(Torch 默认)+ vLLM NCCL + 昇腾 HCCL | 服务端 |
| `checkpoint_engine/xpu_ipc/` | 161+cpp | Intel XPU 的 SYCL IPC 原生扩展(JIT 编译) | 共用 |
| `examples/update.py` | — | torchrun 驱动的端到端示例(u1-l2 已跑过) | 编排 |
| `tests/`(12 个文件) | — | GPU 端到端测试 + CPU 单元测试 | 测试 |
| `patches/vllm_fp8.patch` | — | vLLM FP8 场景的补丁 | 补丁 |
| `docs/npu_start.md`、`figures/` | — | 昇腾环境说明、架构图 | 文档 |

整个项目 Python 源码约 **3456 行**(不含 examples/tests),是一个典型的「小而深」项目:文件不多,但每个文件都压着一层系统知识(分布式通信、锁页内存、跨进程 IPC、RDMA)。

## 4. 核心概念与源码讲解

### 4.1 目录结构:一张全景地图

#### 4.1.1 概念说明

仓库顶层只有四类东西:**Python 主包** `checkpoint_engine/`、**示例** `examples/`、**测试** `tests/`、**外围资产**(README、`docs/`、`figures/`、`patches/`、`pyproject.toml`)。

理解目录结构的关键不是背树,而是意识到:**目录结构 = 部署结构的投影**。

- `ps.py` 和 `worker.py` 同在一个包里,但运行时分别活在**训练侧进程**和**推理引擎进程**中——它们是靠 `import checkpoint_engine` 这一个包同时服务两类进程的。
- `distributed/` 和 `xpu_ipc/` 两个子包各自对应一种「可选硬件能力」,都被设计成**延迟加载**,不装对应硬件/软件时主包依然可用。

#### 4.1.2 核心流程

仓库的静态结构可以按下面四层来读(自顶向下):

```text
第 1 层  入口层
  examples/update.py      ← 训练侧编排脚本(torchrun 启动)
  checkpoint_engine/__main__.py  ← 把 PS 当独立 HTTP 服务启动(UDS)
第 2 层  门面层
  checkpoint_engine/__init__.py  ← 决定包对外暴露哪些名字
第 3 层  两侧主体
  ps.py(服务端)          worker.py(消费端)
第 4 层  共享基础设施(被第 3 层调用)
  data_types.py  pin_memory.py  ipc_handler.py
  device_utils.py  p2p_store.py
  distributed/   xpu_ipc/
```

打包配置决定了「哪些文件会进 pip 包」:[pyproject.toml:L30-L35](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/pyproject.toml#L30-L35) 声明只打包 `checkpoint_engine` 及其子包,并额外把 `xpu_ipc/*.cpp` 作为包数据带上——因为那段 C++ 源码要在用户机器上运行时 JIT 编译(见 4.3.3)。

#### 4.1.3 源码精读

**① 顶层目录树**(由 `git ls-files` 整理,只列关键部分):

```text
checkpoint-engine/
├── checkpoint_engine/            # 主包(约 3456 行 Python)
│   ├── __init__.py               # 门面:公共 API 再导出
│   ├── __main__.py               # python -m 入口
│   ├── api.py                    # FastAPI 端点 + HTTP 客户端
│   ├── data_types.py             # pydantic 数据模型
│   ├── device_utils.py           # DeviceManager + RDMA 发现
│   ├── ipc_handler.py            # IPC 句柄抽象(CUDA/XPU)
│   ├── p2p_store.py              # mooncake TransferEngine 封装
│   ├── pin_memory.py             # 文件加载 + 锁页内存
│   ├── ps.py                     # ParameterServer(最大的文件)
│   ├── worker.py                 # 消费端 REP 循环 + vLLM 扩展
│   ├── distributed/              # 通信后端抽象(base/nccl/hccl)
│   └── xpu_ipc/                  # SYCL IPC 扩展(Python + cpp)
├── examples/update.py            # 端到端示例驱动脚本
├── tests/                        # 12 个 pytest 文件
├── patches/vllm_fp8.patch
├── docs/npu_start.md
├── figures/                      # README 用架构图
└── pyproject.toml
```

**② 打包与测试标记**:

- [pyproject.toml:L8-L18](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/pyproject.toml#L8-L18) 列出基础依赖:torch、fastapi、pydantic、safetensors、pyzmq、uvicorn、loguru、numpy、httpx——注意**没有** vLLM 和 mooncake,这两者都是运行时按需导入的。
- [pyproject.toml:L20-L24](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/pyproject.toml#L20-L24) 定义 `[p2p]` extra,只多加一个 `mooncake-transfer-engine>=0.3.5`(注释说明 `batch_register_memory` 从 0.3.5 才引入)。
- [pyproject.toml:L166-L169](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/pyproject.toml#L166-L169) 注册了 pytest 的 `gpu` 标记,配合 `-m "not gpu"` 就能只跑 CPU 测试(u1-l2 用过)。

**③ tests 目录的分工**(本讲只认门牌号,细节留给 u6-l1):

| 测试文件 | 需要什么硬件 |
|---|---|
| `test_update.py`、`test_inplace_unpin.py`、`test_reuse_pin_memory.py`、`test_xpu_ipc.py`、`test_xpu_parity.py`、`test_ipc_handler.py` | GPU(标记 `gpu`) |
| `test_api.py`、`test_device_manager.py`、`test_rdma_parser.py`、`test_assign_receiver_ranks.py`、`test_p2p_guard.py`、`test_vllm_compat.py` | 纯 CPU 即可 |

#### 4.1.4 代码实践

**实践目标**:亲手生成上面那张「文件 → 行数」的地图,并区分哪些测试能在自己的机器上跑。

**操作步骤**:

1. 在仓库根目录执行 `git ls-files`,确认仓库里实际有哪些文件(避免被本地临时文件干扰)。
2. 执行 `wc -l checkpoint_engine/*.py checkpoint_engine/*/*.py | sort -n`,按行数排序。

**需要观察的现象**:本讲 3 节表格中的行数应与你的输出完全一致(`ps.py` 947 行最大,`__main__.py` 28 行最小);`distributed/` 与 `xpu_ipc/` 两个子包会以 `目录/文件` 的形式出现在 `*/*.py` 的展开里。

**预期结果**:总行数约 3456(不含 examples 与 tests)。如果和本讲不一致,说明你的 HEAD 与本讲基于的 commit(`d1de07b`)不同,阅读时注意校对行号。

#### 4.1.5 小练习与答案

**练习 1**:仓库里唯一会被打进 pip 包的 C++ 源码是哪个文件?为什么它必须随包分发?
**答案**:`checkpoint_engine/xpu_ipc/sycl_ipc.cpp`。因为 XPU 的 IPC 扩展是在**用户机器上运行时 JIT 编译**的(见 4.3.3 的 `prewarm`),如果源码不随包分发,目标机器上就没有东西可编译。

**练习 2**:如果只想验证「纯逻辑」代码(不碰 GPU),应该跑哪个命令?
**答案**:`pytest -m "not gpu"`。`gpu` 标记注册在 [pyproject.toml:L166-L169](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/pyproject.toml#L166-L169),`-m "not gpu"` 反选未标记或未标 gpu 的用例。

**练习 3**:`figures/` 目录和代码是什么关系?
**答案**:纯文档资产,只被 README 引用(架构图、流水线图),不被任何 Python 代码 import,也不打进包。

### 4.2 `__init__.py`:包的公共门面

#### 4.2.1 概念说明

[checkpoint_engine/__init__.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/__init__.py) 只有 40 行,却是理解「这个包想让你怎么用」的最佳入口。它做三件事:

1. **版本兜底**:优先读 setuptools-scm 生成的 `_version.py`,源码目录里没有该文件时退回 `"dev"`。
2. **再导出公共 API**:把散落在 6 个模块里的名字集中到包顶层,让用户写 `from checkpoint_engine import ParameterServer` 而不必记住文件名。
3. **用 `__all__` 声明契约**:`__all__` 列表就是官方承诺的公开 API 清单,「地图」上的服务端入口(`ParameterServer`)与消费端入口(`update_weights_from_ipc`、`VllmColocateWorkerExtension`)都在其中。

注意 `__init__.py` **故意不导出** `Parameter` 级别以下的内部名字(如 `pin_memory` 的函数、`distributed` 的后端类),这是一种「内部实现不对外」的约定。

#### 4.2.2 核心流程

`import checkpoint_engine` 时发生的事(按 import 顺序):

```text
import checkpoint_engine
 ├─ 执行 __init__.py
 │   ├─ from .api import request_inference_to_update     → 加载 api.py → 又 import ps.py
 │   ├─ from .data_types import ...                      → 加载 data_types.py(torch + pydantic)
 │   ├─ from .device_utils import ...                    → 加载 device_utils.py
 │   ├─ from .p2p_store import P2PStore                  → 注意:mooncake 不在这里加载!
 │   ├─ from .ps import ParameterServer                  → 加载 ps.py(连带它的全部 import)
 │   └─ from .worker import ...                          → 加载 worker.py
 └─ 完成。此时 vLLM、mooncake、torch_npu 都尚未被导入
```

也就是说:**门面层一次 import 会把除可选依赖以外的整个代码图拉起来**。这就是「import 成功 ≈ 基础依赖齐全」的原因。

#### 4.2.3 源码精读

**① 版本兜底**:[checkpoint_engine/__init__.py:L1-L4](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/__init__.py#L1-L4) 先尝试 `_version.py`(pip 安装时由 [pyproject.toml:L37-L38](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/pyproject.toml#L37-L38) 的 setuptools-scm 生成),`ImportError` 时用 `"dev"`——所以直接在源码目录 `import` 也能成功。

**② 公共 API 再导出**:[checkpoint_engine/__init__.py:L6-L19](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/__init__.py#L6-L19) 这 14 行就是一张「名字 → 来源文件」的对照表,值得逐行读:

| 导出的名字 | 来源文件 | 侧 |
|---|---|---|
| `request_inference_to_update` | `api.py` | 服务端 |
| `BucketRange` `DataToGather` `H2DBucket` `MemoryBuffer` `MemoryBufferMetaList` `MemoryBufferMetas` `ParameterMeta` | `data_types.py` | 共用 |
| `DeviceManager` `get_ip` `npu_generate_uuid` | `device_utils.py` | 共用 |
| `P2PStore` | `p2p_store.py` | 服务端 |
| `ParameterServer` | `ps.py` | 服务端 |
| `FlattenedTensorMetadata` `VllmColocateWorkerExtension` `update_weights_from_ipc` | `worker.py` | 消费端 |

**③ `__all__` 契约**:[checkpoint_engine/__init__.py:L22-L40](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/__init__.py#L22-L40) 按字母序列出全部公开名字。这张清单正是后续讲义的「课程表」:u3 讲 `ParameterServer`,u2 讲数据模型,u4 讲 `update_weights_from_ipc` 与 `VllmColocateWorkerExtension`。

**④ 一个关键的延迟导入证据**:`P2PStore` 出现在再导出列表里,但 mooncake 的 `from mooncake.engine import TransferEngine` 写在构造函数内部:[checkpoint_engine/p2p_store.py:L11-L14](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/p2p_store.py#L11-L14)。所以「能 import `P2PStore`」不等于「能用 P2P」——只有真正实例化时才要求 mooncake 存在,否则抛 `ImportError`。

#### 4.2.4 代码实践

**实践目标**:用一条命令验证门面层的「名字 → 来源文件」对照表,并确认可选依赖确实被延迟。

**操作步骤**:

1. 打印公开 API:`python -c "import checkpoint_engine as ce; print(ce.__version__); print(ce.__all__)"`。
2. 对每个名字反查真实来源模块:

```python
# 示例代码:保存为任意脚本运行,或逐条 python -c 执行
import inspect
import checkpoint_engine as ce

for name in ce.__all__:
    if name == "__version__":
        continue
    obj = getattr(ce, name)
    mod = inspect.getmodule(obj)
    print(f"{name:<35} -> {mod.__name__}")
```

3. (可选)验证延迟导入:`python -c "import checkpoint_engine, sys; print('mooncake' in sys.modules, 'vllm' in sys.modules)"`。

**需要观察的现象**:第 2 步输出应与 4.2.3 ② 的表格一一对应;第 3 步应输出 `False False`(未安装 mooncake/vLLM 的机器上,import 整个包不会拉起它们)。

**预期结果**:`__version__` 在 pip 安装环境下是形如 `0.x.y` 的版本号,在源码目录直接运行时是 `dev`(原因见 4.2.3 ①)。若第 3 步出现 `True`,说明环境里其他代码提前导入过这些库,不影响结论。

#### 4.2.5 小练习与答案

**练习 1**:`__init__.py` 为什么不把 `pin_memory.py` 里的 `_register_checkpoint` 导出?
**答案**:下划线前缀表明它是模块内部函数;`__init__.py` 只导出稳定的公共 API。`_register_checkpoint` 是 `ps.py` 的内部实现细节(见 4.3.3 ④),对外入口是 `ParameterServer.register_checkpoint` 方法。

**练习 2**:用户代码写 `from checkpoint_engine import ParameterServer`,这行代码间接导入了哪些**项目内**模块?
**答案**:至少 `api`、`data_types`、`device_utils`、`p2p_store`、`ps`、`worker` 六个(因为门面层的 import 是无条件的,见 4.2.2),以及 `ps.py` 继续拉起的 `distributed.base`、`ipc_handler`、`pin_memory`。

**练习 3**:如果不安装 `[p2p]` extra,`from checkpoint_engine import P2PStore` 会失败吗?`P2PStore(device_manager)` 呢?
**答案**:前者**成功**(类定义本身不依赖 mooncake);后者在 [p2p_store.py:L13](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/p2p_store.py#L13) 处触发延迟导入并抛 `ImportError`。`ps.py` 初始化时正是靠捕获这个 `ImportError` 把 `_p2p_store` 置为 `None` 并降级(见 4.3.3 ③)。

### 4.3 `ps.py`:服务端总装车间

#### 4.3.1 概念说明

[checkpoint_engine/ps.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py)(947 行)是全项目最大的文件,也是**唯一**把所有服务端能力「总装」起来的地方:

- 文件顶部是 6 个模块级工具函数(切桶、分配接收端、取端口、算设备 UUID 等);
- 主体是 `ParameterServer` 类——u1-l1 讲过的生命周期 `register_checkpoint → gather_metas → update → unregister_checkpoint` 全部是它的方法;
- 它自己**几乎不实现底层能力**:加载文件靠 `pin_memory.py`,跨进程共享显存靠 `ipc_handler.py`,集合通信靠 `distributed/`,RDMA 传输靠 `p2p_store.py`,硬件差异靠 `device_utils.py`。

「总装车间」的比喻:发动机、轮胎、玻璃都是别处造的,`ps.py` 负责按工序把它们装成一辆能开的车。

#### 4.3.2 核心流程

**① ps.py 的依赖关系图**(箭头 = import,「lazy」= 延迟导入):

```text
                        ┌──────────────────────────────┐
                        │  __init__.py(门面,4.2)      │
                        └──────────────┬───────────────┘
                                       ▼
   ┌─────────────────────────────── ps.py ───────────────────────────────┐
   │                        ParameterServer(服务端)                      │
   ├──────────────┬────────────────┬───────────────┬────────────────────┤
   ▼              ▼                ▼               ▼                    ▼
data_types.py  pin_memory.py   ipc_handler.py  p2p_store.py      distributed/(dist)
(数据模型)    (文件加载+锁页)  (IPC 句柄抽象)  (mooncake 封装)   (通信后端抽象)
   ▲              │                │               │(lazy: mooncake)   │(lazy: vllm 后端)
   │              └──── 共用 ──────┴───────────────┴────────────────────┘
   ▼
device_utils.py(DeviceManager:cuda/npu/xpu 探测,RDMA 网卡发现)
   │
   └─(仅 XPU 且初始化时)→ lazy: xpu_ipc/(SYCL 扩展 JIT 编译)
```

读图要点:

- `ps.py` 顶部 6 条 import **立即生效**;`xpu_ipc` 和 `distributed` 里的 vLLM 后端是**运行时按条件加载**的。
- `worker.py` 也依赖 `device_utils.py` 和 `ipc_handler.py`(见 4.4),所以这两个文件是**两侧真正的共享代码**——`data_types.py` 则是**两侧共享的「语言」**(消息格式)。

**② ParameterServer 的方法布局**(行号来自当前 HEAD,是后续讲义的导航表):

| 行号 | 方法 | 生命周期阶段 | 详细讲义 |
|---|---|---|---|
| L179 | `__init__` | 初始化(rank/设备/TCPStore/P2P store) | u3-l1 |
| L292/L295 | `get_metas` / `load_metas` | 元数据导出/导入 | u3-l3、u6-l3 |
| L305 | `register_checkpoint` | 注册(加载 + 锁页) | u3-l2 |
| L380 | `unregister_checkpoint` | 注销(解页 + 释放) | u3-l2 |
| L462 | `gather_metas` | 全局元数据收集 | u3-l3 |
| L527 | `init_process_group` | 建立进程组 | u3-l4 |
| L569 | `update` | 更新入口(broadcast/p2p 分流) | u3-l4 |
| L622 | `_bind_zmq_socket` | 绑定 ZMQ 抽象 UDS 地址 | u3-l6 |
| L632/L684 | `_detect_bucket_size` / `_copy_to_buffer` | 桶大小探测 / H2D 拷贝 | u3-l5 |
| L751 | `_update_per_bucket` | 三阶段流水线主循环 | u3-l4 |

#### 4.3.3 源码精读

**① 依赖声明的「第一现场」**:[checkpoint_engine/ps.py:L15-L28](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L15-L28) 这段 import 块就是 4.3.2 依赖图的文本形式:`import checkpoint_engine.distributed as dist`(通信抽象)、`data_types`(7 个模型)、`device_utils`(`DeviceManager` 等 3 个名字)、`ipc_handler`、`p2p_store`、`pin_memory`(`_ALIGN_SIZE` 和 `_register_checkpoint`)。读任何新项目,都建议先读这个文件的前 30 行。

**② 类的唯一性与共享内存池名**:[checkpoint_engine/ps.py:L176-L177](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L176-L177) 定义 `class ParameterServer`,类属性 `shared_memory_pool_name = "__shared_memory_pool__"` 是跨 checkpoint 复用锁页内存池时的特殊键(u2-l5、u3-l2 展开)。

**③ 初始化中的两个「条件依赖」**:[checkpoint_engine/ps.py:L237-L248](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L237-L248) 只有当 `device_manager.supports_device_p2p()` 为真才尝试创建 `P2PStore`,并捕获 `ImportError` 降级为 `None`(XPU 没有 Level Zero 后端,直接跳过);[checkpoint_engine/ps.py:L253-L264](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L253-L264) 只有设备类型是 `xpu` 才 `from checkpoint_engine import xpu_ipc` 并调用 `prewarm()` 提前 JIT 编译——这两段是「目录结构里的可选子包如何被使用」的范本。

**④ 把 `pin_memory.py` 当作下属车间**:`register_checkpoint` 的核心只是转发与记账,真正的加载在 [checkpoint_engine/pin_memory.py:L365-L401](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/pin_memory.py#L365-L401) 的 `_register_checkpoint`:它把文件分流成「可原地锁页的 `/dev/shm/*.safetensors`」(交给 `_inplace_pin_memory`,L193 起)和「其余文件/named_tensors」(交给 `_normal_pin_memory`,L277 起)。文件格式解析与 TP 拼接则在 [pin_memory.py:L30-L113](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/pin_memory.py#L30-L113)(`_load_checkpoint_file`)和 [pin_memory.py:L131-L190](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/pin_memory.py#L131-L190)(`_load_checkpoint`)。这些细节全部留给 u2,本讲只需记住:**ps.py 负责编排,pin_memory.py 负责干活**。

**⑤ 把 `data_types.py` 当作「消息语言」**:两侧进程能协作,靠的是对同一组模型的共识。[checkpoint_engine/data_types.py:L71-L75](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/data_types.py#L71-L75) 的 `ParameterMeta`(名字/dtype/shape/对齐后大小)是最小词汇;[data_types.py:L84-L87](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/data_types.py#L84-L87) 的 `H2DBucket`、[data_types.py:L96-L100](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/data_types.py#L96-L100) 的 `MemoryBuffer`、[data_types.py:L109-L111](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/data_types.py#L109-L111) 的 `DataToGather` 分别描述「一段 H2D 传输单元」「一个锁页缓冲」「gather 时交换的全局信息」。u2-l1 会逐字段精读。

#### 4.3.4 代码实践

**实践目标**:不读 947 行全文,用 `inspect` 自动生成 `ParameterServer` 的「方法 × 行号」导航表,并与 4.3.2 ② 的表格互相印证。

**操作步骤**:

1. 在装好依赖的环境(pip install -e . 或 u1-l2 的环境)里执行:

```python
# 示例代码:列出 ParameterServer 全部方法及其定义行号
import inspect
from checkpoint_engine.ps import ParameterServer

funcs = inspect.getmembers(ParameterServer, inspect.isfunction)
for name, fn in sorted(funcs, key=lambda kv: inspect.getsourcelines(kv[1])[1]):
    line = inspect.getsourcelines(fn)[1]
    print(f"ps.py:{line:<5} {name}")
```

2. 用 grep 交叉验证:`grep -n "    def " checkpoint_engine/ps.py`。

**需要观察的现象**:两种方式给出的「方法名 → 行号」应完全一致;前 6 个结果不是方法而是模块级函数(见步骤 2 的输出中不含缩进的那些 `def`),因为 `getmembers(ParameterServer, ...)` 只收类成员。

**预期结果**:方法行号与 4.3.2 ② 的表格一致(`__init__` L179、`register_checkpoint` L305、`gather_metas` L462、`update` L569、`_update_per_bucket` L751……)。此实践在**纯 CPU 环境**也能跑:`inspect` 只加载类定义,不实例化 `ParameterServer`,因此不会触碰 CUDA(对比 4.4.4 中 worker 侧的限制)。

#### 4.3.5 小练习与答案

**练习 1**:`ps.py` 顶部 import 了 `device_utils` 中的哪三个名字?分别用在哪?
**答案**:`DeviceManager`(初始化时创建,后续所有硬件判断都走它)、`get_ip`(gather_metas 时上报本机 IP,见 [ps.py:L486](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L486))、`npu_generate_uuid`(NPU 平台生成设备 UUID,见 [ps.py:L53-L54](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L53-L54))。

**练习 2**:`import checkpoint_engine.ps` 会连带 import vLLM 吗?为什么?
**答案**:不会。`ps.py` import 的是 `checkpoint_engine.distributed`(即 `distributed/__init__.py`),它只从 `base.py` 导入抽象与默认实现(见 [distributed/__init__.py:L1-L13](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/distributed/__init__.py#L1-L13));依赖 vLLM 的 `vllm_nccl.py`/`vllm_hccl.py` 只有在调用 `use_backend("vllm_nccl"|"vllm_hccl")` 时才被 `importlib` 动态加载([distributed/base.py:L221-L242](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/distributed/base.py#L221-L242))。

**练习 3**:如果让你为新推理框架(如 SGLang)写一个「服务端适配」,应该改 `ps.py` 还是新增文件?
**答案**:**都不需要大改**。`ps.py` 通过 `update` 的 `req_func` 参数把「如何通知推理引擎」外包出去([ps.py:L569-L576](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L569-L576));新框架只需要仿照 `worker.py` 的 `VllmColocateWorkerExtension` 写一个消费端扩展(这正是 u6-l4 的二次开发话题)。

### 4.4 `worker.py`:消费端与推理引擎的桥梁

#### 4.4.1 概念说明

[checkpoint_engine/worker.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py)(231 行)与 `ps.py` 是**镜像关系**:

| | 服务端 `ps.py` | 消费端 `worker.py` |
|---|---|---|
| 运行进程 | 训练侧(torchrun 拉起) | 推理引擎(vLLM worker 进程) |
| ZMQ 角色 | REQ(发起请求) | REP(应答请求) |
| 设备内存 | 拷贝权重到显存并广播 | 通过 IPC 句柄**映射同一块显存** |
| 依赖 vLLM? | 否 | 扩展类按需用 vLLM 内部接口 |
| 硬件依赖入口 | `DeviceManager`(初始化即创建) | `DeviceManager`(进入 REP 循环即创建) |

文件里只有三个公开构件:`FlattenedTensorMetadata`(切张量的元数据类型)、`update_weights_from_ipc`(REP 循环主体)、`VllmColocateWorkerExtension`(vLLM 的注入适配层)。u1-l2 讲过 vLLM 用 `--worker-extension-cls` 注入这个类——本讲只看**它为什么必须单独成文件**:它 import 了 `vllm`,绝不能被服务端路径连带导入。

#### 4.4.2 核心流程

消费端的主循环是一个**消息驱动状态机**。PS 发来的每条消息类型决定 worker 的动作:

```text
连接建立后:
  recv ipc_handle(共享显存句柄)
    → attach 到本进程设备地址空间 → 得到一维 uint8 buffer → 回复 b""
  循环:
  recv list[FlattenedTensorMetadata]   → 从 buffer 按 offset 切出张量,调 run() 装载 → 回复 b""
  recv None(第 1 次,释放信号)       → synchronize、释放 IPC、gc、清缓存 → 回复 b""(进入 released 态)
  recv None(第 2 次,收尾信号)       → 调 post_hook() → 回复 b"" → 退出循环
  recv Exception(PS 强制退出信号)    → 本地 raise,结束
```

注意两个设计:**收到业务异常时不本地 raise**,而是把 traceback 字符串发回 PS,由 PS 统一广播「全体退出」信号,保证所有 worker 以同样方式结束;**released 态之后只允许再收 None**,否则断言失败——这就是「释放资源」和「收尾钩子」两阶段分离。

#### 4.4.3 源码精读

**① 消费端的极简依赖**:[checkpoint_engine/worker.py:L10-L15](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L10-L15) 只 import `device_utils` 和 `ipc_handler` 两处项目内依赖——比 `ps.py` 少得多,因为消费端**不加载文件、不建进程组、不做广播**,只「收」。

**② 从扁平 buffer 还原张量**:[checkpoint_engine/worker.py:L39-L51](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L39-L51) 的 `_extract_weights` 是「共享语言」的消费侧:按 `FlattenedTensorMetadata` 里的 dtype/shape/offset,把一维 `uint8` buffer 切片、`view(dtype)` 再 `view(shape)`,零拷贝地还原每个张量。这个 offset 语义与服务端 `_to_named_tensor`([ps.py:L35-L48](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L35-L48))的生产侧严格对齐——两侧必须逐字节一致,这正是 `data_types.py` 作为共享语言的意义。

**③ 状态机本体**:[checkpoint_engine/worker.py:L54-L131](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L54-L131) 是 `update_weights_from_ipc` 全函数;其中 [worker.py:L78-L82](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L78-L82) 的注释就是 4.4.2 伪代码的官方版本,`released` 标志的翻转与两次 `None` 的分支在 [worker.py:L86-L107](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L86-L107)。错误回传(不本地 raise)的理由写在 [worker.py:L113-L117](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L113-L117) 的注释里。

**④ vLLM 适配层**:[checkpoint_engine/worker.py:L134-L231](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L134-L231) 的 `VllmColocateWorkerExtension` 把通用 REP 循环包装成 vLLM 可调用的方法:`_device_uuid` 缓存属性([worker.py:L150-L162](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L150-L162))按 cuda/npu/xpu 三种平台生成 UUID,**必须与 `ps.py` 的 `_get_physical_gpu_id` 格式一致** ZMQ 寻址才能对上(注释在 [worker.py:L159](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L159));真正被 vLLM `/collective_rpc` 调用的入口是 [worker.py:L168-L231](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L168-L231),它定义 `_load_weights`(含 MTP drafter)与 `_post_hook` 两个回调后转入通用循环。

#### 4.4.4 代码实践

**实践目标**:通过**源码阅读**核对状态机的每条转移,并解释为什么这个循环**不能**在纯 CPU 机器上直接运行(这是本讲特意安排的「读代码而非跑代码」训练)。

**操作步骤**:

1. 打开 [worker.py:L78-L123](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L78-L123),准备一张三列表格:`收到的消息类型` / `worker 做什么` / `回复什么`。
2. 逐个 `if/elif` 分支填表:`payload is None 且未 released`、`payload is None 且已 released`、`isinstance(payload, list)`、`isinstance(payload, Exception)`、其余(`TypeError`)。
3. 找出 CPU 不可运行的证据:[worker.py:L65](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L65) 在函数开头就执行 `device_manager = DeviceManager()`,而 [device_utils.py:L222-L230](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/device_utils.py#L222-L230) 的 `_detect_device_type` 在没有 NPU/XPU/CUDA 时**直接 raise TypeError**。
4. 看 GPU 环境下别人怎么驱动这个循环:阅读 [tests/test_update.py:L64](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_update.py#L64) 附近,测试用一个 ZMQ REQ 端按「ipc handle → 张量列表 → None → None」的顺序喂数据,正好覆盖状态机的全部四类消息。

**需要观察的现象**:步骤 2 的表格应与 4.4.2 的伪代码一致;步骤 3 应得出「`DeviceManager` 在无加速卡的机器上构造即抛错,因此该函数只能整体在有卡环境运行」的结论。

**预期结果**:填出的 4 行消息转移表 + 1 行 TypeError 兜底;CPU 复现失败的具体报错信息(`TypeError: The current device type is not supported`)——本实践为源码阅读型,结论基于源码逻辑,**待本地验证**(在有 GPU 的机器上按步骤 4 跑 `pytest tests/test_update.py` 可完整验证)。

#### 4.4.5 小练习与答案

**练习 1**:`worker.py` 和 `ps.py` 各自 import 了 `ipc_handler`,为什么说它是「两侧真正的共享代码」?
**答案**:服务端用 `build_ipc_handler(...).export(buffer)` 把设备缓冲导出成可序列化句柄(`ps.py` 的 update 流程,见 [ps.py:L602-L603](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L602-L603));消费端用同一抽象的 `attach(handle, device_id)` 把句柄映射回本进程([worker.py:L68-L70](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L68-L70))。同一份契约,两端各执一半。

**练习 2**:worker 收到 `run()` 抛出的异常后,为什么选择发回 PS 而不是自己 raise?
**答案**:见 [worker.py:L113-L117](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L113-L117) 的注释:所有 worker 必须**以同样方式**退出,由 PS 统一广播异常,避免部分进程卡死在 ZMQ 等待上。

**练习 3**:NPU 平台上,PS 侧和 worker 侧的设备 UUID 分别由谁生成?格式必须满足什么约束?
**答案**:PS 侧在 [ps.py:L51-L54](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L51-L54) 用 `npu_generate_uuid()` 拼出 `"NPU-<uuid>"`;worker 侧在 [worker.py:L156-L157](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L156-L157) 用同一函数、同一格式生成。两侧字符串必须逐字符相等,因为 PS 用 UUID 作为 ZMQ 地址的 key,worker 用自己的 UUID 查表取地址(`zmq_handles[self._device_uuid]`)。

## 5. 综合实践

**任务:亲手绘制「服务端 vs 消费端」完整依赖图,并标注每条边的加载时机。**

1. **收集边**:对 `ps.py` 和 `worker.py` 分别执行 `grep -nE "^from checkpoint_engine|^import checkpoint_engine"`,列出每个文件的项目内依赖;再对 `api.py`、`pin_memory.py`、`p2p_store.py`、`ipc_handler.py`、`device_utils.py`、`distributed/__init__.py` 重复这一步,直到不再出现新的项目内文件(图封闭)。
2. **标注加载时机**:对每条边判断是「import 时立即」还是「运行时延迟」。提示:延迟导入的 4 个已知位置——`p2p_store.py` 构造函数里的 mooncake、`ps.py` XPU 分支里的 `xpu_ipc`、`distributed/base.py` 的 `use_backend` 动态加载、`worker.py` 方法内部的 vLLM 导入。
3. **上色分层**:把节点按「服务端专属 / 消费端专属 / 两侧共享」分三组着色,与 4.3.2 的图对照——预期共享层恰好是 `data_types.py`、`device_utils.py`、`ipc_handler.py` 三个。
4. **验证**:用两条命令抽查你的结论:
   - `python -c "import checkpoint_engine, sys; print([m for m in sys.modules if 'vllm' in m or 'mooncake' in m])"` → 预期为空列表;
   - `python -c "import checkpoint_engine, sys; print('checkpoint_engine.distributed.base' in sys.modules)"` → 预期为 `True`(立即加载)。
5. **产出**:把图和验证结果记入你的笔记。这张图就是后续 u2~u5 的总导航:每开一讲,先在该图上定位文件,再看它被谁调用。

本实践在纯 CPU 环境(装好基础依赖)即可完成,第 4 步若与预期不符,请回到第 2 步检查你对某条边加载时机的判断。

## 6. 本讲小结

- 仓库约 3456 行 Python,核心是一个包 `checkpoint_engine` 加上 `examples/`、`tests/`、`patches/`;目录结构是「两侧进程共用一个包」这一部署结构的投影。
- `__init__.py` 是纯门面:40 行只做版本兜底、再导出和 `__all__` 契约;`import checkpoint_engine` 会拉起除可选依赖外的整个代码图。
- `ps.py`(947 行)是服务端总装车间:`ParameterServer` 的 `register_checkpoint → gather_metas → update → unregister_checkpoint` 生命周期编排了 `pin_memory`、`ipc_handler`、`distributed`、`p2p_store`、`device_utils` 五个下属模块。
- `worker.py`(231 行)是消费端镜像:REP 状态机四类消息(list/None/None/Exception)驱动「attach → 装载 → 释放 → 收尾」;`VllmColocateWorkerExtension` 是它的 vLLM 适配层。
- 两侧真正的共享代码是 `data_types.py`(消息语言)、`device_utils.py`(硬件抽象)、`ipc_handler.py`(IPC 契约);可选依赖(mooncake、vLLM、XPU 扩展)全部通过延迟导入隔离。
- 画地图的通用工具:`git ls-files` + `wc -l` 看静态结构,`inspect` + `grep` 看方法布局——不需要 GPU 就能完成结构分析。

## 7. 下一步学习建议

下一讲 [u1-l4 整体架构与三阶段数据流总览](u1-l4-architecture-and-dataflow.md) 会把本讲的静态地图「通电」:沿 `update` → `_update_per_bucket` 走一遍 H2D、broadcast、reload 三阶段流水线,并画出 PS 与 worker 的时序图。

在此之前,推荐做两个热身阅读(都用本讲的地图定位):

1. [data_types.py:L71-L111](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/data_types.py#L71-L111):7 个模型总共 40 行,是 u2-l1 的预习材料。
2. [worker.py:L78-L82](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L78-L82):5 行状态机注释,是 u4-l1 的预习材料。
