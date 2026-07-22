# 配置体系 EngineConfig → SchedulerConfig → ServerArgs

## 1. 本讲目标

学完本讲，你应当能够：

- 画出 Mini-SGLang 三层配置 dataclass 的继承关系，并说出每一层各自负责什么字段。
- 理解 `frozen=True`、`cached_property`、`@property` 覆写这三件事如何协同，支撑「一次解析、跨进程共享、按需延迟加载」。
- 读懂 `parse_args` 这个「适配层」：它如何把命令行字符串（`--tensor-parallel-size`、`--dtype auto`、`--shell-mode`…）归一化成一份 `ServerArgs`。
- 能够独立给 `ServerArgs` 增加一个 CLI 参数，并追踪它从 argparse 流到 Engine 的完整路径。

本讲承接 u1-l2（你已经知道 `launch_server` → `parse_args` 是入口），并把「参数是怎么变成一个对象的」这一步彻底讲透。它也是 u5（Engine 初始化/显存管理）的前置：Engine 几乎所有行为都从 `EngineConfig` 读取。

## 2. 前置知识

### 2.1 Python dataclass 极简回顾

`@dataclass` 会根据类的字段注解自动生成 `__init__`、`__repr__` 等方法。例如：

```python
@dataclass
class EngineConfig:
    model_path: str
    page_size: int = 1   # 带默认值的字段排在后面
```

加上 `frozen=True` 后，实例变成**不可变**：任何 `obj.x = ...` 都会抛 `FrozenInstanceError`。这一点本讲会反复用到。

### 2.2 继承与字段顺序

子类 dataclass 会**追加**字段到父类字段之后。要求：父类无默认值的字段必须排在子类有默认值的字段之前，否则 Python 会报 `non-default argument follows default argument`。Mini-SGLang 的父类 `EngineConfig` 给几乎所有字段都设了默认值，因此子类可以自由追加带默认值的字段。

### 2.3 property 覆写（多态）

Python 中子类定义的同名 `@property` 会**遮蔽**父类的同名 property。本讲你会看到 `distributed_addr`、`max_forward_len`、`backend_create_detokenizer_link` 三个 property 都被子类改写过——这是「同一接口、不同层级不同行为」的关键。

### 2.4 本讲术语

| 术语 | 含义 |
|------|------|
| `EngineConfig` | 引擎层基础配置：模型路径、dtype、显存/页/图相关参数 |
| `SchedulerConfig` | 调度层配置：在引擎基础上增加 prefill 切块、cache 类型、ZMQ 地址 |
| `ServerArgs` | 服务层配置：再增加 host/port/tokenizer 数量等对外参数 |
| `tp_info` | 一个 `DistributedInfo(rank, size)`，标识当前进程在第几张卡、共几张卡 |
| 归一化（normalization） | 把 CLI 字符串（如 `"auto"`、`--tp-size`）转换成运行时对象（如 `torch.dtype`、`DistributedInfo`）的过程 |

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [python/minisgl/engine/config.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/config.py) | 定义继承链的根 `EngineConfig`，含 `cached_property` 延迟加载与基础 property |
| [python/minisgl/scheduler/config.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/config.py) | 定义 `SchedulerConfig(EngineConfig)`，追加 prefill/cache/zmq 字段 |
| [python/minisgl/server/args.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/args.py) | 定义 `ServerArgs(SchedulerConfig)` 与 `parse_args` 适配层 |
| [python/minisgl/server/launch.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/launch.py) | 调用 `parse_args`，并用 `dataclasses.replace` 为每个 rank 派生配置 |
| [python/minisgl/distributed/info.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/distributed/info.py) | `DistributedInfo(rank, size)` —— `tp_info` 的真实类型 |
| [python/minisgl/engine/engine.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/engine.py) | `Engine.__init__` 与 `_adjust_config`，展示配置如何被消费 |

## 4. 核心概念与源码讲解

### 4.1 EngineConfig：继承链的根基

#### 4.1.1 概念说明

`EngineConfig` 是整个配置体系的根。它只描述**引擎**真正关心的东西：用哪个模型、什么精度、多少显存给 KV cache、每页多大、要不要 CUDA graph、要不要 PyNCCL……它**不关心**网络地址（那是调度层的事），也**不关心** HTTP 端口（那是服务层的事）。

这种「分层」的好处是：`Engine` 可以只依赖 `EngineConfig`，从而在**离线模式**（u11-l1 的 `LLM` 类，不需要 HTTP server）下也能被直接复用。

#### 4.1.2 核心流程

`EngineConfig` 的生命周期分两段：

1. **构造时**：`__init__`（由 dataclass 自动生成）只设置那些**普通字段**。此时 `hf_config` / `model_config` 还没有计算。
2. **首次访问时**：当你第一次读 `config.model_config`，`cached_property` 才真正去读 HF 配置、构造 `ModelConfig`，并把结果缓存进实例。

为什么要延迟？因为读 HF 配置需要磁盘 I/O（甚至联网下载），而有些代码路径根本用不到 `model_config`（比如只想拿 `page_size`）。延迟到「第一次访问」可以避免无谓的开销。

#### 4.1.3 源码精读

整个类只有普通字段 + 两个 `cached_property` + 三个普通 `@property`：

[python/minisgl/engine/config.py:15-31](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/config.py#L15-L31) —— 定义 `EngineConfig`，`frozen=True` 且全部字段都有默认值（除 `model_path` / `tp_info` / `dtype`）：

```python
@dataclass(frozen=True)
class EngineConfig:
    model_path: str
    tp_info: DistributedInfo
    dtype: torch.dtype
    max_running_req: int = 256
    attention_backend: str = "auto"
    moe_backend: str = "auto"
    cuda_graph_max_bs: int | None = None
    page_size: int = 1
    memory_ratio: float = 0.9
    use_pynccl: bool = True
    max_seq_len_override: int | None = None
    num_page_override: int | None = None
    ...
```

[python/minisgl/engine/config.py:33-41](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/config.py#L33-L41) —— 两个延迟加载的 `cached_property`：

```python
@cached_property
def hf_config(self):
    return cached_load_hf_config(self.model_path)

@cached_property
def model_config(self) -> ModelConfig:
    from minisgl.models import ModelConfig
    return ModelConfig.from_hf(self.hf_config)
```

注意 `model_config` 内部用了**函数内导入**（`from minisgl.models import ModelConfig`），这是为了避免模块加载时的循环依赖——`models` 包反向引用了 `engine` 的一些设施。

> **关键细节：`cached_property` 为什么能用在 `frozen=True` 的 dataclass 上？**
>
> `frozen=True` 实际上是给类装了一个会抛错的 `__setattr__`。而 `functools.cached_property` 在缓存结果时**不调用 `__setattr__`**，而是直接写进实例的 `__dict__`（`instance.__dict__[name] = val`），从而绕过了那把锁。所以延迟加载依然成立。你将在 4.4 节看到它的「反面」：`_adjust_config` 用 `object.__setattr__(...)` 故意绕过同一把锁去改 `attention_backend`。

[python/minisgl/engine/config.py:43-55](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/config.py#L43-L55) —— 三个普通 property，提供「派生量」：

```python
@property
def max_seq_len(self) -> int:
    if self.max_seq_len_override is not None:
        return self.max_seq_len_override
    return self.model_config.rotary_config.max_position   # 默认从模型配置里读

@property
def distributed_addr(self) -> str:
    return "tcp://127.0.0.1:2333"   # 引擎层给一个占位地址，调度/服务层会覆写
```

`max_seq_len` 体现了「override 优先」的常见模式：用户显式给了就用用户的，否则从 `model_config` 推断。

#### 4.1.4 代码实践

**目标**：验证 `cached_property` 确实是「第一次访问才计算、之后复用」。

**步骤**（纯 Python，无 GPU 也能跑）：

1. 在仓库根目录启动 Python，确保 `import minisgl` 可用（`uv venv && uv pip install -e .` 之后）。
2. 写一段示例代码（**示例代码，非项目原有**）：

```python
from minisgl.engine import EngineConfig
from minisgl.distributed import DistributedInfo
import torch

cfg = EngineConfig(
    model_path="Qwen/Qwen3-0.6B",
    tp_info=DistributedInfo(0, 1),
    dtype=torch.float16,
)
print("构造完成，此时还没读 HF 配置")
mc1 = cfg.model_config        # 第一次访问：触发磁盘/网络读取
mc2 = cfg.model_config        # 第二次访问：命中缓存
print(mc1 is mc2)             # 预期 True —— 同一对象
print("num_layers =", mc1.num_layers)
```

**观察**：第一次 `cfg.model_config` 会卡顿（在读 config.json），第二次立即返回；`mc1 is mc2` 为 `True`。

**预期结果**：`True`，并打印出模型的层数等字段。如果离线且本地没有缓存该模型，会下载或报错——属正常现象。

**注意**：若你在无网络环境，把 `model_path` 换成本地已存在的模型目录即可。

#### 4.1.5 小练习与答案

**练习 1**：`EngineConfig` 为什么用 `frozen=True`？删掉它会怎样？

**答案**：`frozen=True` 让配置对象不可变，从而可以安全地在 `parse_args` → 主进程 → 各子进程之间传递而不用担心被某处意外改坏；同时 frozen 的 dataclass 默认可哈希（`__hash__` 由字段生成）。删掉它后，`cached_property` 仍能工作，但配置在多 rank 间共享时失去了「不变量保护」，且 `dataclasses.replace` 派生子配置时语义会变弱。

**练习 2**：`max_seq_len` 何时来自用户、何时来自模型？

**答案**：当 `max_seq_len_override is not None`（用户传了 `--max-seq-len-override`）时用用户值；否则取 `self.model_config.rotary_config.max_position`（模型训练时支持的最大位置）。

---

### 4.2 SchedulerConfig：调度层与 ZMQ 地址

#### 4.2.1 概念说明

`SchedulerConfig` 继承自 `EngineConfig`，在引擎字段之上**追加**调度器才需要的字段：

- `max_extend_tokens`：Chunked Prefill 的单批最大 token 数（默认 8192）。
- `cache_type`：KV cache 管理策略（默认 `"radix"`，即基数树前缀缓存）。
- `offline_mode`：是否离线模式（不走 ZMQ/tokenizer，u11-l1 的 `LLM` 类用到）。
- `_unique_suffix`：一个带 PID 的后缀，用来让 ZMQ 的 ipc 套接字地址在**同一台机器上多次启动时互不冲突**。

它还覆写了 `max_forward_len`：调度器关心的是「单次前向最多算多少 token」（即 `max_extend_tokens`），而不是「序列最长多少」。

#### 4.2.2 核心流程

ZMQ 地址的生成规则可以画成这样：

```
唯一前缀 "ipc:///tmp/minisgl_N"  +  后缀 ".pid=<进程号>"
        │                              │
        │                              └─ 由 _get_pid_suffix() 在构造时生成
        │                                 → 保证同一台机上两个 server 实例地址不撞
        └─ N = 0/1/2 分别对应 backend / detokenizer / scheduler 广播
```

关键点：`_unique_suffix` 用 `field(default_factory=_get_pid_suffix)`，它在 **`SchedulerConfig` 实例化那一刻**捕获当前进程的 PID。由于 `parse_args` 在主进程里构造 `ServerArgs`，所以捕获到的是**主进程（launcher）的 PID**；随后 launch 用 `dataclasses.replace` 把同一份 `ServerArgs` 复制给每个 rank 时，`_unique_suffix` 作为普通字段被原样保留——于是所有 rank 拿到**相同**的地址，这正是它们能互相找到对方的前提。

#### 4.2.3 源码精读

[python/minisgl/scheduler/config.py:8-21](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/config.py#L8-L21) —— PID 后缀工厂与 `SchedulerConfig` 字段：

```python
def _get_pid_suffix() -> str:
    import os
    return f".pid={os.getpid()}"

@dataclass(frozen=True)
class SchedulerConfig(EngineConfig):
    max_extend_tokens: int = 8192
    cache_type: str = "radix"
    offline_mode: bool = False
    _unique_suffix: str = field(default_factory=_get_pid_suffix)
```

[python/minisgl/scheduler/config.py:23-41](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/config.py#L23-L41) —— 三条后端 ipc 地址与两处覆写：

```python
@property
def zmq_backend_addr(self) -> str:
    return "ipc:///tmp/minisgl_0" + self._unique_suffix

@property
def zmq_detokenizer_addr(self) -> str:
    return "ipc:///tmp/minisgl_1" + self._unique_suffix

@property
def zmq_scheduler_broadcast_addr(self) -> str:
    return "ipc:///tmp/minisgl_2" + self._unique_suffix

@property
def max_forward_len(self) -> int:
    return self.max_extend_tokens        # 覆写父类：调度层用「单批 token 数」

@property
def backend_create_detokenizer_link(self) -> bool:
    return True
```

注意 `max_forward_len` 在父类里返回 `max_seq_len`，在这里被覆写为 `max_extend_tokens`——同一个属性名，不同层级返回不同语义的值，这就是 2.3 节说的 property 多态。`backend_create_detokenizer_link` 在服务层还会被再次覆写（见 4.3）。

#### 4.2.4 代码实践

**目标**：理解 `_unique_suffix` 如何决定 ipc 地址、以及 `max_forward_len` 的覆写效果。

**步骤**（**示例代码，非项目原有**）：

```python
from minisgl.scheduler import SchedulerConfig
from minisgl.distributed import DistributedInfo
import torch

cfg = SchedulerConfig(
    model_path="Qwen/Qwen3-0.6B",
    tp_info=DistributedInfo(0, 1),
    dtype=torch.float16,
)
print(cfg._unique_suffix)          # 形如 ".pid=12345"
print(cfg.zmq_backend_addr)        # ipc:///tmp/minisgl_0.pid=12345
print(cfg.max_forward_len)         # 8192 —— 来自 max_extend_tokens，不是 max_seq_len
```

**观察**：两次运行这段脚本，`_unique_suffix` 里的 PID 会不同，因此 ipc 地址也不同——这正是它能防止两次启动互相抢套接字的原因。

**预期结果**：地址串里带当前进程号；`max_forward_len` 恒为 `8192`（默认值）。**待本地验证**（数值取决于你本机的 PID）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `_unique_suffix` 用 `field(default_factory=...)` 而不是直接 `_unique_suffix: str = _get_pid_suffix()`？

**答案**：直接赋默认值的话，`_get_pid_suffix()` 会在**类定义时**（模块导入时）执行一次，所有实例共用同一个 PID，失去区分意义。`default_factory` 让它在**每个实例构造时**重新调用，从而捕获构造那一刻的 PID。

**练习 2**：`max_forward_len` 在 `EngineConfig` 和 `SchedulerConfig` 里返回值分别是什么？为什么调度层要覆写它？

**答案**：父类返回 `max_seq_len`（一条序列最长多长），子类覆写为 `max_extend_tokens`（一次前向最多算多少 token，受 Chunked Prefill 控制）。调度层在排批时要按「单批预算」约束，所以更关心后者。

---

### 4.3 ServerArgs：服务层与进程拓扑开关

#### 4.3.1 概念说明

`ServerArgs` 再继承 `SchedulerConfig`，补上「对外服务」才需要的字段：监听地址 `server_host`/`server_port`、tokenizer 进程数 `num_tokenizer`、是否静默输出 `silent_output`。

但它真正的巧思不在字段，而在那一组**进程拓扑开关** property。回忆 u1-l4：默认情况下 tokenizer 并入 detokenizer（`num_tokenizer == 0`）；当用户显式 `--num-tokenizer N` 时，tokenizer 才独立成 N 个进程。这两种拓扑下，谁该创建哪条 ZMQ 链路是完全不同的——而 `ServerArgs` 用一组布尔 property 把这件事表达得干净利落。

#### 4.3.2 核心流程

`share_tokenizer`（= `num_tokenizer == 0`）是总开关，派生出四条规则：

```
num_tokenizer == 0（默认，共享）
├─ zmq_tokenizer_addr      == zmq_detokenizer_addr     （复用 _1）
├─ tokenizer_create_addr   == True                      （detokenizer 进程顺带建链）
├─ backend_create_detokenizer_link == False             （backend 不用再单独建）
└─ frontend_create_tokenizer_link   == False

num_tokenizer > 0（独立 tokenizer）
├─ zmq_tokenizer_addr      == ipc:///tmp/minisgl_4...   （新开 _4）
├─ tokenizer_create_addr   == False
├─ backend_create_detokenizer_link == True              （backend 要建到 detokenizer 的链）
└─ frontend_create_tokenizer_link   == True              （frontend 要建到 tokenizer 的链）
```

这样 `launch_server` 只需要读这几个布尔值就能决定「谁创建谁连接」，不用写一堆 if/else。

#### 4.3.3 源码精读

[python/minisgl/server/args.py:14-19](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/args.py#L14-L19) —— `ServerArgs` 追加的字段：

```python
@dataclass(frozen=True)
class ServerArgs(SchedulerConfig):
    server_host: str = "127.0.0.1"
    server_port: int = 1919
    num_tokenizer: int = 0
    silent_output: bool = False
```

[python/minisgl/server/args.py:21-51](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/args.py#L21-L51) —— 拓扑开关 + 地址派生：

```python
@property
def share_tokenizer(self) -> bool:
    return self.num_tokenizer == 0

@property
def zmq_tokenizer_addr(self) -> str:
    if self.share_tokenizer:
        return self.zmq_detokenizer_addr      # 复用 detokenizer 的地址
    result = "ipc:///tmp/minisgl_4" + self._unique_suffix
    assert result != self.zmq_detokenizer_addr
    return result

@property
def tokenizer_create_addr(self) -> bool:
    return self.share_tokenizer

@property
def backend_create_detokenizer_link(self) -> bool:   # 覆写 SchedulerConfig！
    return not self.share_tokenizer

@property
def frontend_create_tokenizer_link(self) -> bool:
    return not self.share_tokenizer

@property
def distributed_addr(self) -> str:                    # 覆写 EngineConfig！
    return f"tcp://127.0.0.1:{self.server_port + 1}"
```

注意这里**第二次覆写**了 `backend_create_detokenizer_link`（调度层给的是 `True`，服务层改成 `not share_tokenizer`），以及 `distributed_addr`（引擎层给固定 2333，服务层改成 `server_port + 1`）。MRO（方法解析顺序）保证最子类的定义胜出。

`launch.py` 正是消费这些开关的人。[python/minisgl/server/launch.py:73-87](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/launch.py#L73-L87) 里启动 detokenizer 时，`"create": server_args.tokenizer_create_addr`、`"addr": server_args.zmq_detokenizer_addr` 等参数全部来自这些 property。

#### 4.3.4 代码实践

**目标**：用代码确认「共享 vs 独立 tokenizer」两种拓扑下地址与开关的差异。

**步骤**（**示例代码，非项目原有**）：

```python
from minisgl.server.args import ServerArgs
from minisgl.distributed import DistributedInfo
import torch

def show(num_tok):
    a = ServerArgs(
        model_path="Qwen/Qwen3-0.6B",
        tp_info=DistributedInfo(0, 1),
        dtype=torch.float16,
        num_tokenizer=num_tok,
    )
    print(f"--- num_tokenizer={num_tok} ---")
    print("share_tokenizer            :", a.share_tokenizer)
    print("zmq_tokenizer_addr         :", a.zmq_tokenizer_addr)
    print("tokenizer_create_addr      :", a.tokenizer_create_addr)
    print("backend_create_detokenizer :", a.backend_create_detokenizer_link)
    print("frontend_create_tokenizer  :", a.frontend_create_tokenizer_link)

show(0)   # 默认共享
show(2)   # 独立两个 tokenizer
```

**观察**：`num_tokenizer=0` 时 `zmq_tokenizer_addr` 与 `zmq_detokenizer_addr` 完全相同；`=2` 时变成 `_4` 通道，且 `backend_create_detokenizer_link` / `frontend_create_tokenizer_link` 翻转为 `True`。

**预期结果**：两组输出在四个布尔值与 tokenizer 地址上正好相反。**待本地验证**（PID 部分不同）。

#### 4.3.5 小练习与答案

**练习 1**：`backend_create_detokenizer_link` 在三层里各返回什么？

**答案**：`EngineConfig` 没有定义它；`SchedulerConfig` 恒返回 `True`；`ServerArgs` 覆写为 `not share_tokenizer`。由于实际使用的对象总是 `ServerArgs` 实例，最终生效的是最后这个。

**练习 2**：`zmq_tokenizer_addr` 里那行 `assert result != self.zmq_detokenizer_addr` 防的是什么 bug？

**答案**：防止独立 tokenizer 模式下「tokenizer 地址」意外和「detokenizer 地址」撞车——如果两条逻辑链路共用一个 ipc 套接字，消息会串台。这个断言是一个廉价的不变量保护。

---

### 4.4 parse_args：把命令行字符串变成 ServerArgs

#### 4.4.1 概念说明

`parse_args` 是用户与配置体系之间的**唯一适配层**。它解决一个根本错配：用户在命令行打的是字符串（`--tensor-parallel-size 2`、`--dtype auto`、`--shell-mode`），而 `ServerArgs` 需要的是结构化对象（`DistributedInfo`、`torch.dtype`、布尔覆写）。`parse_args` 的工作就是：

1. 用 `argparse` 把字符串解析成一批「大致对应字段」的键值对；
2. 做若干**归一化**后处理，让这批键值对能直接喂给 `ServerArgs(**kwargs)`；
3. 返回 `(ServerArgs, run_shell)`。

#### 4.4.2 核心流程

归一化一共五步，顺序很关键：

```
parser.parse_args(args).__dict__
        │
        ▼
① shell 模式覆写：run_shell → cuda_graph_max_bs=1, max_running_req=1, silent_output=True
        │
        ▼
② 路径展开：model_path 以 "~" 开头 → expanduser
        │
        ▼
③ modelscope 下载：非本地目录 → snapshot_download（dummy 时跳过权重文件）→ del model_source
        │
        ▼
④ dtype 归一化："auto" → 读 HF config 的 dtype → str 映射成 torch.dtype
        │
        ▼
⑤ tp 重命名：tensor_parallel_size(int) → tp_info=DistributedInfo(0, size) → del tensor_parallel_size
        │
        ▼
ServerArgs(**kwargs) → logger.info 打印 → return (result, run_shell)
```

其中 ③ 和 ④ 都会触发磁盘/网络 I/O，所以 `parse_args` 不是纯函数——这也是为什么它要把「下载」「读 dtype」这些副作用集中在一处。

#### 4.4.3 源码精读

[python/minisgl/server/args.py:54-68](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/args.py#L54-L68) —— 函数签名与延迟导入（避免解析阶段就加载 attention/kvcache/moe 这些重模块）：

```python
def parse_args(args: List[str], run_shell: bool = False) -> Tuple[ServerArgs, bool]:
    from minisgl.attention import validate_attn_backend
    from minisgl.kvcache import SUPPORTED_CACHE_MANAGER
    from minisgl.moe import SUPPORTED_MOE_BACKENDS
    parser = argparse.ArgumentParser(description="MiniSGL Server Arguments")
```

**「单一真相源」的默认值技巧**：argparse 的 `default=` 直接引用 dataclass 类属性，避免默认值写两处漂移。例如 [python/minisgl/server/args.py:94-100](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/args.py#L94-L100)：

```python
parser.add_argument(
    "--max-running-requests", type=int,
    dest="max_running_req",                          # ← 映射到 dataclass 字段名
    default=ServerArgs.max_running_req,              # ← 默认值取自 dataclass
    help="The maximum number of running requests.",
)
```

注意 `dest="max_running_req"`：CLI 名是 `--max-running-requests`，但 dataclass 字段是 `max_running_req`，`dest` 负责对齐。

**布尔开关的断言技巧**：[python/minisgl/server/args.py:116-130](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/args.py#L116-L130) 先断言默认值符合预期，再定义 `store_true` / `store_false`：

```python
assert ServerArgs.use_dummy_weight == False
parser.add_argument("--dummy-weight", action="store_true", dest="use_dummy_weight", ...)

assert ServerArgs.use_pynccl == True
parser.add_argument("--disable-pynccl", action="store_false", dest="use_pynccl", ...)
```

`store_true` 只对「默认 False」的开关有意义，`store_false` 只对「默认 True」的有意义——这两条 assert 把这个隐含前提显式化了。

**五步归一化**集中在 [python/minisgl/server/args.py:226-268](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/args.py#L226-L268)：

```python
kwargs = parser.parse_args(args).__dict__.copy()

# ① shell 模式覆写
run_shell |= kwargs.pop("shell_mode")
if run_shell:
    kwargs["cuda_graph_max_bs"] = 1
    kwargs["max_running_req"] = 1
    kwargs["silent_output"] = True

# ② 路径展开
if kwargs["model_path"].startswith("~"):
    kwargs["model_path"] = os.path.expanduser(kwargs["model_path"])

# ③ modelscope 下载（然后删掉 model_source，它不是配置字段）
if kwargs["model_source"] == "modelscope":
    ...  # snapshot_download，dummy 时 ignore 权重文件
    kwargs["model_path"] = model_path
del kwargs["model_source"]

# ④ dtype 归一化
if (dtype_str := kwargs["dtype"]) == "auto":
    dtype_str = cached_load_hf_config(kwargs["model_path"]).dtype
DTYPE_MAP = {"float16": torch.float16, "bfloat16": torch.bfloat16, "float32": torch.float32}
kwargs["dtype"] = DTYPE_MAP[dtype_str] if isinstance(dtype_str, str) else dtype_str

# ⑤ tp 重命名
kwargs["tp_info"] = DistributedInfo(0, kwargs["tensor_parallel_size"])
del kwargs["tensor_parallel_size"]

result = ServerArgs(**kwargs)
logger.info(f"Parsed arguments:\n{result}")
return result, run_shell
```

注意两个「重命名」：CLI 的 `tensor_parallel_size` 不是 `ServerArgs` 字段（字段叫 `tp_info`），所以这里要 `del` 掉旧键、塞进新键；`model_source` 同理（它只是个下载来源提示，不属于运行配置），所以也被 `del` 掉。`dest` 和 `del`+重塞是处理「CLI 名 ≠ 字段名」的两套手段。

最后，`launch_server` 拿到唯一的 `ServerArgs` 后，为每张卡派生一份只改 rank 的副本——[python/minisgl/server/launch.py:59-63](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/launch.py#L59-L63)：

```python
for i in range(world_size):
    new_args = replace(server_args, tp_info=DistributedInfo(i, world_size))
    mp.Process(target=_run_scheduler, args=(new_args, ack_queue), ...).start()
```

`dataclasses.replace` 依赖 `frozen=True` 的语义：它返回一个新对象，只替换指定字段，其余（包括 `_unique_suffix`）原样复制——这正是各 rank 地址一致、仅 rank 不同的来源。

#### 4.4.4 代码实践

**目标**：在不启动 GPU 服务的前提下，单独调用 `parse_args` 观察归一化结果。

**步骤**（**示例代码，非项目原有**）：

```python
from minisgl.server.args import parse_args

# 模拟命令行：python -m minisgl --model-path Qwen/Qwen3-0.6B --tp-size 2 --dtype auto
argv = ["--model-path", "Qwen/Qwen3-0.6B",
        "--tensor-parallel-size", "2",
        "--dtype", "auto",
        "--shell-mode"]
server_args, run_shell = parse_args(argv, run_shell=False)

print("dtype        :", server_args.dtype)          # torch.bfloat16 或 torch.float16
print("tp_info      :", server_args.tp_info)        # DistributedInfo(rank=0, size=2)
print("run_shell    :", run_shell)                  # True
print("cuda_graph_max_bs:", server_args.cuda_graph_max_bs)  # 被 shell 覆写为 1
print("max_running_req  :", server_args.max_running_req)    # 被 shell 覆写为 1
```

**观察**：`--shell-mode` 触发 ①，把 `cuda_graph_max_bs` / `max_running_req` 强行设为 1、`silent_output=True`；`--dtype auto` 触发 ④，读 HF config 后变成真正的 `torch.dtype`；`--tensor-parallel-size 2` 触发 ⑤，变成 `tp_info`。

**预期结果**：`run_shell=True`，两个被覆写的字段都是 1，`dtype` 是 `torch.dtype` 而非字符串。**待本地验证**（dtype 取决于该模型的 config.json；首次会联网/读盘）。

#### 4.4.5 小练习与答案

**练习 1**：`--tensor-parallel-size` 这个 CLI 名最后变成了 `ServerArgs` 的哪个字段？中间经过哪两步？

**答案**：变成 `tp_info`（`DistributedInfo`）。argparse 先以 `tensor_parallel_size` 存为 int；归一化 ⑤ 把它包成 `DistributedInfo(0, size)` 赋给 `tp_info`，再 `del kwargs["tensor_parallel_size"]`。

**练习 2**：为什么 `model_source` 在归一化末尾要被 `del`？

**答案**：因为它只是「从哪里下载模型」的提示，**不是** `ServerArgs` 的字段；不删掉的话 `ServerArgs(**kwargs)` 会因为收到未知关键字参数而报 `TypeError`。

**练习 3**：`--shell-mode`（在 u1-l2 提到可简写为 `--shell`）为什么能生效？

**答案**：argparse 默认开启前缀缩写（`allow_abbrev=True`），`--shell` 是 `--shell-mode` 的唯一前缀，故被匹配；随后 `kwargs.pop("shell_mode")` 取出布尔值并触发 ① 的覆写。

---

## 5. 综合实践：给 ServerArgs 新增一个 `--log-level` 参数

把本讲四块内容串起来：从继承链（4.1–4.3）到适配层（4.4），亲手加一个参数并追踪它的流转。

### 5.1 实践目标

新增 `--log-level`（取值如 `INFO`/`DEBUG`/`WARNING`），让它：

1. 在 `ServerArgs` 上成为一个字段；
2. 能被 `python -m minisgl --model-path ... --log-level DEBUG` 正确解析；
3. 在 Engine/Scheduler 里能通过 `config.log_level` 读到。

### 5.2 操作步骤

**第 1 步：在 `ServerArgs` 增加字段。** 编辑 [python/minisgl/server/args.py:14-19](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/args.py#L14-L19)，加一行：

```python
@dataclass(frozen=True)
class ServerArgs(SchedulerConfig):
    server_host: str = "127.0.0.1"
    server_port: int = 1919
    num_tokenizer: int = 0
    silent_output: bool = False
    log_level: str = "INFO"          # ← 新增
```

因为 `ServerArgs` 继承自 `SchedulerConfig → EngineConfig`，这个新字段会随配置对象一路传到 `Engine(config)`，无需在父类重复声明。

**第 2 步：在 `parse_args` 注册 CLI 选项。** 在 [python/minisgl/server/args.py:205-218](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/args.py#L205-L218) 附近加：

```python
parser.add_argument(
    "--log-level",
    dest="log_level",
    default=ServerArgs.log_level,    # 复用 dataclass 默认值，单一真相源
    help="Logging level (INFO/DEBUG/WARNING).",
)
```

关键：`dest="log_level"` 与字段名对齐；`default=ServerArgs.log_level` 沿用「单一真相源」惯例。因为字段名和 `dest` 完全一致，`kwargs` 里天然就有 `log_level` 这一项，**不需要**像 `tensor_parallel_size` 那样手动 `del`+重塞——它会被 `ServerArgs(**kwargs)` 直接消费。

**第 3 步：在需要的地方读取它。** 例如在 [python/minisgl/server/launch.py:16-29](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/launch.py#L16-L29) 的 `_run_scheduler` 里，或 `Engine.__init__` 里读 `config.log_level` 并据此 `logging.getLogger().setLevel(...)`。

### 5.3 需要观察的现象

- 运行 `python -m minisgl --model-path Qwen/Qwen3-0.6B --log-level DEBUG`，启动日志里应出现 `Parsed arguments:` 一段，其中包含 `log_level='DEBUG'`（这来自 [python/minisgl/server/args.py:266-267](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/args.py#L266-L267) 的 `logger.info`）。
- 不传该参数时，`log_level` 取默认值 `'INFO'`。
- 在第 3 步埋点的位置，应能读到与 CLI 一致的值。

### 5.4 预期结果

新增字段后，`ServerArgs.log_level` 既存在于对象上（可被 Engine 读），也暴露给了 CLI（可被用户设）。整个过程**没有**改 `EngineConfig` 或 `SchedulerConfig`，体现了继承链的可扩展性：子类加字段，父类无感知。

> 注意：本实践会修改源码。若你只想阅读而不改源码，可仅完成第 1、2 步的「纸面设计」，并用 4.4.4 的方式调用 `parse_args(["--model-path", "...", "--log-level", "DEBUG"])` 验证 `server_args.log_level == "DEBUG"`（**待本地验证**）。

## 6. 本讲小结

- 配置体系是三层 frozen dataclass 继承链：`EngineConfig`（引擎/显存/页/图）→ `SchedulerConfig`（prefill 切块/cache 类型/ZMQ 地址）→ `ServerArgs`（host/port/tokenizer 拓扑）。
- `frozen=True` 既保证配置跨进程共享时的不变性，也让 `dataclasses.replace` 能干净地为每个 TP rank 派生「只差 rank」的副本。
- `cached_property` 让 `hf_config`/`model_config` 延迟到首次访问才加载，它通过直接写 `__dict__` 绕过 frozen 的 `__setattr__` 锁；`_adjust_config` 则用 `object.__setattr__` 从另一侧绕过同一把锁做 `auto` 后端选择。
- 子类用 `@property` 覆写父类（`max_forward_len`、`distributed_addr`、`backend_create_detokenizer_link`），实现「同名接口、不同层级不同语义」。
- `parse_args` 是唯一的适配层，用 `dest=` 对齐字段名、用 `default=ServerArgs.x` 复用默认值，并用五步归一化（shell 覆写、路径展开、modelscope 下载、dtype 解析、tp 重命名）把字符串变成可直接 `ServerArgs(**kwargs)` 的键值对。
- CLI 名与字段名不一致时有两套处理手段：`dest=`（名称对齐）与 `del`+重塞（如 `tensor_parallel_size → tp_info`、`model_source` 直接删除）。

## 7. 下一步学习建议

- 阅读 [python/minisgl/engine/engine.py:148](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/engine.py#L148) 的 `_determine_num_pages` 与 [python/minisgl/engine/engine.py:218](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/engine.py#L218) 的 `_adjust_config`，看 `EngineConfig` 的 `memory_ratio`/`page_size`/`attention_backend` 是如何被真正消费的（承接 u5-l1）。
- 结合 u2-l3（进程间消息与序列化），对照本讲的 `zmq_*_addr` 看 ZMQ 队列是如何建在这些地址之上的。
- 若想理解 `tp_info` 在通信中的作用，继续看 u4-l2（Scheduler I/O 与多 rank 广播）。
