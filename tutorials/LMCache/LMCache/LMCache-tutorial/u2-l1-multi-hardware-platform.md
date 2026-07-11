# 多硬件平台与设备抽象

> 本讲是进阶层（u2）的第一篇，承接 u1-l3「代码目录结构与组织」。在 u1 里我们知道 `lmcache/__init__.py` 在导入期会做「设备检测」并把 `c_ops` 替换掉，但当时只点到为止。本讲把这件事彻底讲透：LMCache 如何用一个统一的 `torch_dev` 抽象，同时跑在 NVIDIA CUDA、Intel XPU、Habana HPU、Moore Threads MUSA 甚至纯 CPU 主机上，以及你想接一块新硬件时该改哪里。

## 1. 本讲目标

学完本讲，你应当能够：

1. 说清楚 `torch_dev` / `torch_device_type` 这对全局变量是什么、由谁产生、为什么全项目都从 `lmcache` 里 `import` 它俩。
2. 描述 `platform/_registry.py` 的 **DeviceSpec 注册表**与 **KV IPC wrapper 工厂表**两套「自动发现」机制，理解「定义一个子类即自动注册」的设计。
3. 复述 CPU stub 退化路径与「无 torch 的 CLI-only 模式」两条降级链路各自返回什么。
4. 看懂 `get_backend()` 如何把「Python fallback + 硬件算子」合并成一个模块并替换 `sys.modules["lmcache.c_ops"]`（即 monkey patch）。
5. 对照真实代码，列出新增一类硬件要改的文件清单。

## 2. 前置知识

在进入源码前，先建立几条直觉。

### 2.1 为什么不能直接 `import torch.cuda`

KV cache 管理层要做大量与设备相关的操作：在 GPU 上分配张量、把 KV 从 GPU 搬到 CPU（D2H）、跨进程传递 GPU 张量句柄（CUDA IPC）、记录异步事件、同步流（stream）等。这些操作在 NVIDIA 平台是 `torch.cuda.*`，在 Intel 平台是 `torch.xpu.*`，在 Habana 平台是 `torch.hpu.*`。

如果业务代码里到处写 `import torch.cuda` 并直接调用，那么换一个硬件就得改几千处代码。LMCache 的做法是：

- **检测一次**：进程启动时探测当前到底有哪种加速器，结果记成全局变量 `torch_device_type`（字符串 `"cuda"`/`"xpu"`/`"hpu"`/`"musa"`/`"cpu"`）和 `torch_dev`（对应的 torch 设备模块，例如 `torch.cuda`）。
- **全局复用**：业务代码统一写 `from lmcache import torch_dev`，再调用 `torch_dev.synchronize()`、`torch_dev.Stream()`，由 `torch_dev` 在运行时路由到正确硬件。
- **优雅降级**：没有加速器时，`torch_dev` 退化为一个 **stub**（占位实现），把所有方法变成 no-op，让上层「写得像有 GPU」却不会崩。

这种「检测一次，全局复用」的检测点就被称为 **monkey patch point**（猴子补丁点）。

### 2.2 三层抽象

LMCache 的多硬件抽象分成自上而下的三层，记住这个分层就能解释后面所有的代码：

| 层 | 谁负责 | 引用设备的方式 | 是否硬件无关 |
|------|--------|----------------|--------------|
| **入口层** `__init__.py` | 启动时检测 | `_detect_device()` 产出 `torch_dev` | 是（只检测一次） |
| **中间层** engine / storage / multiprocess | 业务逻辑 | `from lmcache import torch_dev` | **是**（写一次到处跑） |
| **底层** gpu_connector | 与引擎 KV 布局对接 | 直接 `torch.cuda` / `torch.xpu` 等 | **否**（每种硬件一套实现） |

关键点：**中间层是硬件无关的**，而底层是「每硬件一套、不做统一」。也就是说，抽象发生在「业务代码 ↔ 设备操作」之间，但「KV 布局适配」这一步故意不抽象——因为每种引擎在每种硬件上的 KV 内存布局差异太大，硬抽象反而更乱。

### 2.3 两个名词

- **fallback（降级实现）**：当缺少某项能力（如编译好的 C++ 算子、CUDA IPC）时，用 Python / 纯 torch 写一个功能等价但更慢的版本兜底。
- **stub（桩）**：比 fallback 更轻，只保证「调用不报错」，但什么都不真正做（返回 0、返回 True、no-op），用于 CPU-only 测试环境。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [lmcache/__init__.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/__init__.py) | 包入口：调用 `_detect_device()` 产出 `torch_dev`，并把合并后的算子模块替换进 `sys.modules["lmcache.c_ops"]`（monkey patch 点） |
| [lmcache/v1/platform/__init__.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/platform/__init__.py) | 跨平台抽象层：维护 DeviceSpec 注册表、实现 `_detect_device()` 与 `get_backend()` |
| [lmcache/v1/platform/base_device_spec.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/platform/base_device_spec.py) | `DeviceSpec` 基类：描述「如何检测 + 加载哪个算子后端」，本身可实例化为「全 False」的兜底 |
| [lmcache/v1/platform/_registry.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/platform/_registry.py) | KV IPC wrapper 的「懒发现工厂表」：按 `device_type` 选跨进程 KV 传递包装器 |
| [lmcache/v1/platform/cuda/__init__.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/platform/cuda/__init__.py) 等 | 各硬件的 `DeviceSpec` 子类（cuda / xpu / hpu / musa） |
| [lmcache/v1/platform/cpu/stub_cpu_device.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/platform/cpu/stub_cpu_device.py) | CPU-only 环境下 `torch_dev` 的占位实现 `StubCPUDevice` |
| [lmcache/v1/gpu_connector/__init__.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/gpu_connector/__init__.py) | 底层路由：按 `torch_device_type` 选 GPU 连接器（每硬件一套） |
| [docs/design/ARCHITECTURE_MULTI_HARDWARE.md](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/docs/design/ARCHITECTURE_MULTI_HARDWARE.md) | 设计文档：用一张 ASCII 大图总结本讲全部内容（读代码前先读它） |

> 提醒：按项目惯例，`docs/design/` 镜像 `lmcache/` 包树。本讲涉及的设备抽象属于 v1 公共底座，对应设计文档就是上面这份 `ARCHITECTURE_MULTI_HARDWARE.md`。

## 4. 核心概念与源码讲解

本讲拆成 5 个最小模块：先讲注册表与设备检测（4.1），再讲统一入口与 monkey patch（4.2），然后是 CPU 退化路径（4.3）、底层连接器路由（4.4），最后是「扩展新硬件」的实操指南（4.5）。

### 4.1 DeviceSpec 设备注册表与自动发现

#### 4.1.1 概念说明

要让代码「自动认识」所有硬件，而又不想写一长串 `if cuda ... elif xpu ...`，最干净的做法是 **注册表（registry）**：每种硬件自己提交一张「规格说明书」，说明三件事——

1. 我叫什么（`device_type`，如 `"cuda"`）；
2. 在 torch 里对应哪个模块（`torch_module_name`，如 `"cuda"` → `torch.cuda`）；
3. 我的算子后端模块路径（`ops_module`，如 `"lmcache.c_ops"`），没有就返回 `None`；
4. 当前这台机器上我到底可不可用（`is_available()`）。

这张「规格说明书」就是 `DeviceSpec`。所有硬件各写一个子类，启动时统一收集进一个字典 `_DEVICE_REGISTRY`，后续的「检测」「选后端」全都查这张表。

#### 4.1.2 核心流程

注册表的构建发生在 **`platform/__init__.py` 被导入的那一刻**，流程是：

1. 调用 `discover_subclasses(...)` 扫描 `lmcache.v1.platform` 包下所有子包；
2. 对每个 `DeviceSpec` 的具体子类，**实例化**它（`cls()`）；
3. 以 `spec.device_type` 为 key 存进 `_DEVICE_REGISTRY` 字典。

```
platform/__init__.py 导入
   └─ discover_subclasses("lmcache.v1.platform", DeviceSpec)
        ├─ 扫到 cuda/__init__.py  → CudaDeviceSpec()   → key="cuda"
        ├─ 扫到 xpu/__init__.py   → XpuDeviceSpec()    → key="xpu"
        ├─ 扫到 hpu/__init__.py   → HpuDeviceSpec()    → key="hpu"
        └─ 扫到 musa/__init__.py  → MusaDeviceSpec()   → key="musa"
   → _DEVICE_REGISTRY = {"cuda":..., "xpu":..., "hpu":..., "musa":...}
```

注意一个细节：扫描时用了一个过滤条件 `module_filter=lambda name: not name.startswith(("_", "base"))`，意思是 **跳过以 `_` 或 `base` 开头的模块**——所以 `_registry.py`、`base_device_spec.py` 不会被当成「某个硬件的规格说明」扫进来。

#### 4.1.3 源码精读

先看注册表的构建——[lmcache/v1/platform/__init__.py:52-64](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/platform/__init__.py#L52-L64)：

```python
_DEVICE_REGISTRY: dict[str, DeviceSpec] = {
    spec.device_type: spec
    for spec in [
        cls()
        for cls in discover_subclasses(
            "lmcache.v1.platform",
            DeviceSpec,
            module_filter=lambda name: not name.startswith(("_", "base")),
            require_defined_in_module=True,
            on_import_error=lambda name, exc: None,
        )
    ]
}
```

这段代码做的事就是「扫子类 → 实例化 → 按 `device_type` 建 dict」。`on_import_error=lambda name, exc: None` 表示某个子包导入失败时**静默跳过**——这对可选硬件很重要（例如装了 torch 但没装 Habana 的机器，`hpu/__init__.py` 里的导入不该让整个包起不来）。

再看 `DeviceSpec` 基类——[lmcache/v1/platform/base_device_spec.py:30-78](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/platform/base_device_spec.py#L30-L78)：

```python
class DeviceSpec:
    """...Instantiating DeviceSpec directly yields the fallback
    implementation with "no-op / all False" semantics..."""

    @property
    def device_type(self) -> str:
        return "cpu"          # 默认 "cpu"

    @property
    def torch_module_name(self) -> str:
        return "cpu"          # 默认 "cpu"

    @property
    def ops_module(self) -> str | None:
        return None           # 默认没有专属算子

    def is_available(self) -> bool:
        return False          # 兜底永远返回 False，绝不会被自动选中
```

这里有个非常巧妙的设计：**`DeviceSpec` 本身是可实例化的，而且实例化出来就是「全 False / no-op」的兜底实现**。这意味着当检测不到任何加速器时，代码不需要写 `if spec is None: 用默认`，直接用 `DeviceSpec()` 即可。注释里写得很清楚：「`DeviceSpec` 自身既是基类，又是 fallback 实现」。

`is_available()` 的文档串里有一条硬约束——[base_device_spec.py:73-77](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/platform/base_device_spec.py#L73-L77)：「这个方法**绝不能**从 `lmcache.__init__` 导入，否则会循环依赖；要直接 `import torch`」。这是因为 `lmcache/__init__.py` 会反过来 import `platform`，如果在检测可用性时又触发包入口，就死锁了。

接着看一个具体子类，CUDA 的——[lmcache/v1/platform/cuda/__init__.py:14-41](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/platform/cuda/__init__.py#L14-L41)：

```python
class CudaDeviceSpec(DeviceSpec):
    @property
    def device_type(self) -> str:
        return "cuda"
    @property
    def torch_module_name(self) -> str:
        return "cuda"
    @property
    def ops_module(self) -> str | None:
        return "lmcache.c_ops"          # 编译出的 C++/CUDA 扩展
    def is_available(self) -> bool:
        try:
            import torch
            return torch.cuda.is_available()
        except Exception:
            return False
```

对比几个硬件的差异，能看清 `ops_module` 的取值策略（下表来自 [xpu/__init__.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/platform/xpu/__init__.py)、[hpu/__init__.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/platform/hpu/__init__.py)、[musa/__init__.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/platform/musa/__init__.py)）：

| 硬件 | `device_type` | `torch_module_name` | `ops_module` | 有无专属算子 |
|------|--------------|---------------------|--------------|-------------|
| NVIDIA | `"cuda"` | `"cuda"` | `"lmcache.c_ops"` | 有（C++/CUDA 扩展） |
| Intel | `"xpu"` | `"xpu"` | `"lmcache.xpu_ops"` | 有 |
| Moore Threads | `"musa"` | `"musa"` | `"lmcache.v1.platform.musa.ops"` | 有 |
| Habana | `"hpu"` | `"hpu"` | `None` | 无（用 Python fallback） |

`DeviceSpec` 还承担了一项「能力描述」职责：pin memory（锁页内存）。看 [base_device_spec.py:96-133](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/platform/base_device_spec.py#L96-L133)，`pin_memory()` / `unpin_memory()` / `is_pin_supported` 通过 `_get_pin_backend()` 懒加载一个 `PinMemoryBackend`，而 CUDA 子类用 `pin_memory_backend` 属性指回 `CudaPinMemoryBackend`（见 [cuda/__init__.py:29-31](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/platform/cuda/__init__.py#L29-L31)）。这样上层代码调用 `current_device_spec.pin_memory(ptr, size)` 时，会自动路由到当前硬件的正确实现，而 fallback 的 `DeviceSpec` 这几项全是 no-op / False。

#### 4.1.4 代码实践

1. **实践目标**：亲眼看到 `_DEVICE_REGISTRY` 里有哪几个 key，并验证 `device_type` / `ops_module` 的取值。
2. **操作步骤**：写一个最小脚本（示例代码，可命名为 `peek_registry.py`，放在仓库任意位置运行即可）：

   ```python
   # 示例代码
   import lmcache.v1.platform as pf
   for dt, spec in sorted(pf._DEVICE_REGISTRY.items()):
       print(dt, "->", "torch." + spec.torch_module_name,
             "| ops:", spec.ops_module,
             "| available:", spec.is_available())
   ```
3. **需要观察的现象**：打印出的列表里应包含 `cuda`、`xpu`、`hpu`、`musa` 四个 key；`available` 字段只有你这台机器真实存在的硬件为 `True`，其余为 `False`。
4. **预期结果**：在一台普通 NVIDIA 主机上，只有 `cuda ... available: True`，其余三项 `available: False`。
5. **待本地验证**：具体哪些硬件 `available=True` 取决于你的环境，请以本地输出为准。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `_DEVICE_REGISTRY` 的扫描要过滤掉以 `_` 开头的模块？
**答案**：因为 `_registry.py`、`base_device_spec.py` 这类文件不是「某个硬件的规格说明」，而是注册机制本身或基类；不过滤的话会把基类或无关类错误地当成硬件 spec 收集进来。

**练习 2**：HPU 的 `ops_module` 返回 `None`，这对后续选后端意味着什么？
**答案**：意味着 HPU 没有专属的编译算子，`get_backend()` 会走 Python fallback（见 4.2），即用纯 torch/numba 写的等价实现，功能正确但更慢。

---

### 4.2 torch_dev 统一入口与 c_ops monkey patch

#### 4.2.1 概念说明

注册表只是「账本」，真正被全项目使用的产物有两个全局变量：

- `torch_dev`：当前设备的 torch 模块（如 `torch.cuda`），或退化后的 stub；
- `torch_device_type`：字符串形式的设备类型（如 `"cuda"`）。

它们由 `_detect_device()` 产出，并在 `platform/__init__.py` 模块级别一次性赋值。业务代码只需要 `from lmcache import torch_dev`，就拿到了「写一次到处跑」的硬件无关入口。

此外还有第二个 monkey patch：算子后端 `c_ops`。LMCache 有一批高性能算子（KV 搬运、内存拷贝等），CUDA 上是编译好的 C++ 扩展 `lmcache.c_ops`，但没有 GPU 时需要用 Python fallback 替代。`get_backend()` 的职责就是把「fallback + 当前硬件的算子」**合并成一个模块**，然后替换 `sys.modules["lmcache.c_ops"]`，让所有 `import lmcache.c_ops` 的代码透明地拿到正确的实现。

#### 4.2.2 核心流程

设备检测 `_detect_device()` 的判定优先级（从高到低）：

1. **torch 不可导入** → 返回 `(None, "cpu")`（CLI-only 模式）。
2. **环境变量 `DEVICE_TYPE` 显式指定**且该 spec 可用 → 用它。
3. **自动遍历注册表**：找到第一个 `is_available() and torch 有对应模块` 的 spec → 用它。
4. **都没命中** → 返回 CPU stub `(StubCPUDevice("cpu"), "cpu")`。

```
_detect_device()
 ├─ import torch 失败?  ──是──▶ (None, "cpu")
 ├─ os.environ["DEVICE_TYPE"] 存在且可用? ──是──▶ (torch.<module>, device_type)
 ├─ 遍历 _DEVICE_REGISTRY: 首个 is_available() 的 ──▶ (torch.<module>, device_type)
 └─ 兜底 ──▶ (StubCPUDevice("cpu"), "cpu")
```

算子合并 `get_backend()` 的流程：

```
get_backend(device_type)
 ├─ import torch 失败?  ──▶ None（CLI-only，不替换 c_ops）
 ├─ default = import python_ops_fallback        # 永远存在的 Python 兜底
 ├─ spec = _DEVICE_REGISTRY[device_type]
 │     ├─ 不存在 / 不可用 / ops_module 为 None ──▶ 返回 default
 │     └─ backend = import spec.ops_module       # 如 lmcache.c_ops
 └─ merged = 新建模块; merged.update(default); merged.update(backend)
                              └─ backend 覆盖同名函数 ──▶ 返回 merged
```

注意 `dict.update` 的顺序：先放 `default`，再用 `backend` 覆盖。效果是「硬件有的函数用硬件实现，硬件没有的函数继承 fallback」。

#### 4.2.3 源码精读

先看包入口如何消费这两个产物——[lmcache/__init__.py:12-14, 26-36](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/__init__.py#L12-L36)：

```python
from lmcache.v1.platform import get_backend
from lmcache.v1.platform import torch_dev as torch_dev
from lmcache.v1.platform import torch_device_type as torch_device_type
...
__all__ = ["__version__", "torch_dev", "torch_device_type"]

_ops = get_backend(torch_device_type)
if _ops is not None:
    # Override lmcache.c_ops with merged module,
    # in which: python_ops_fallback as base, use backend implementation if exists
    sys.modules["lmcache.c_ops"] = _ops
else:
    logger.warning("No compute backend loaded; CLI-only mode (torch/numba not installed)")
```

短短几行就把抽象的两条主线都接上了：`torch_dev`/`torch_device_type` 直接 re-export 给业务代码；`get_backend()` 的结果替换进 `sys.modules`，从此任何 `import lmcache.c_ops` 拿到的都是合并模块。当 `_ops is None`（torch 装不上）时只打一条 warning，进入 CLI-only 模式。

再看 `_detect_device()` 主体——[lmcache/v1/platform/__init__.py:72-128](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/platform/__init__.py#L72-L128)（节选关键分支）：

```python
def _detect_device() -> tuple[Any, str]:
    try:
        import torch
    except ImportError as e:
        logger.warning("load torch failed, error is %s", e)
        return None, "cpu"                       # 分支①: CLI-only

    env_device_type = os.environ.get("DEVICE_TYPE")
    if env_device_type is not None:
        env_device_type = env_device_type.strip().lower()
        spec = _DEVICE_REGISTRY.get(env_device_type)
        if spec is not None and spec.is_available():
            torch_module = getattr(torch, spec.torch_module_name, None)
            if torch_module is not None:
                return torch_module, spec.device_type   # 分支②: 环境变量强制
        # 否则打 warning，落到自动检测

    for spec in _DEVICE_REGISTRY.values():       # 分支③: 自动遍历
        if not spec.is_available():
            continue
        torch_module = getattr(torch, spec.torch_module_name, None)
        if torch_module is not None:
            return torch_module, spec.device_type

    from lmcache.v1.platform.cpu.stub_cpu_device import StubCPUDevice
    return StubCPUDevice("cpu"), "cpu"            # 分支④: CPU stub
```

注意分支②和③都额外检查 `getattr(torch, spec.torch_module_name, None) is not None`——也就是说「spec 说可用」还不够，torch 这个安装里**确实要有对应的子模块**（例如装了 CPU 版 torch 时 `torch.cuda` 不存在，即便 spec 报 available）。这是双重保险。

检测完后，模块级别直接赋值——[platform/__init__.py:206-225](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/platform/__init__.py#L206-L225)：

```python
torch_dev, torch_device_type = _detect_device()
logger.info("torch_dev=%s, torch_device_type=%s", torch_dev, torch_device_type)

_registered_device_spec = _DEVICE_REGISTRY.get(torch_device_type)
if _registered_device_spec is None:
    if torch_device_type != "cpu":
        logger.warning("No DeviceSpec registered for %r; ...", torch_device_type)
    current_device_spec: DeviceSpec = DeviceSpec()   # 兜底: 全 False
else:
    current_device_spec = _registered_device_spec
```

这里又多产出一个 `current_device_spec`：它是检测到的硬件对应的 `DeviceSpec` 实例，供需要「能力描述」（如 pin memory）的代码使用。检测不到时退化为 `DeviceSpec()`——还记得 4.1 里说的吗，`DeviceSpec()` 本身就是「全 False / no-op」的兜底。

最后看 `get_backend()` 的合并逻辑——[platform/__init__.py:151-203](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/platform/__init__.py#L151-L203)（节选）：

```python
def get_backend(device_type: str) -> Any | None:
    try:
        import torch  # noqa: F401
    except (ImportError, ModuleNotFoundError) as e:
        return None                                  # CLI-only

    default_module = importlib.import_module("lmcache.python_ops_fallback")
    spec = _DEVICE_REGISTRY.get(device_type)
    if spec is None or not spec.is_available() or not spec.ops_module:
        return default_module                        # 无专属算子 → 纯 fallback

    backend_module = importlib.import_module(spec.ops_module)
    merged_module = types.ModuleType("lmcache.c_ops")
    merged_module.__dict__.update(default_module.__dict__)  # 先放 fallback
    merged_module.__dict__.update(backend_module.__dict__)  # 再用硬件覆盖
    return merged_module
```

这就是「Python fallback + 硬件算子」合并的全部秘密：一个新建的 `types.ModuleType`，先灌入 fallback 的所有符号，再用硬件模块的同名符号覆盖。

举一个真实的「中间层如何用 `torch_dev`」的例子。业务代码里到处是这样的写法——[lmcache/v1/cache_engine.py:31, 126, 858, 1903, 2045](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/cache_engine.py#L31)：

```python
from lmcache import torch_dev, torch_device_type
...
self.broadcast_stream = ... if ... else torch_dev.Stream()
...
with torch_dev.stream(self.broadcast_stream):
...
local_rank = self.metadata.worker_id % torch_dev.device_count()
...
torch_dev.set_device(corrected_device)
```

引擎核心代码完全不知道自己跑在 CUDA 还是 XPU 上——它只认 `torch_dev`。这就是「中间层硬件无关」的落地。

#### 4.2.4 代码实践

1. **实践目标**：观察 monkey patch 前后 `lmcache.c_ops` 的身份变化，确认它确实是「合并模块」。
2. **操作步骤**（示例代码）：

   ```python
   # 示例代码
   import lmcache                          # 触发 __init__.py 的 monkey patch
   from lmcache import torch_dev, torch_device_type
   import lmcache.c_ops as c_ops

   print("torch_device_type =", torch_device_type)
   print("torch_dev         =", torch_dev)
   print("c_ops 模块名       =", c_ops.__name__)
   # 看一个典型算子来自哪里
   print("multi_layer_kv_transfer 是否存在:", hasattr(c_ops, "multi_layer_kv_transfer"))
   print("TransferDirection:", c_ops.TransferDirection)
   ```
3. **需要观察的现象**：`c_ops.__name__` 会是 `"lmcache.c_ops"`（合并模块的名字）；`torch_device_type` 与你的硬件一致。
4. **预期结果**：在 NVIDIA 主机上 `torch_device_type = cuda`，`c_ops` 里既有 fallback 的 `TransferDirection`，也有 CUDA 扩展的高性能 `multi_layer_block_kv_transfer`。
5. **待本地验证**：在无 GPU 主机上 `import lmcache` 会触发 `get_backend` 返回 `None`（torch 未装）或纯 fallback（torch 装了但无卡），请以本地现象为准。

#### 4.2.5 小练习与答案

**练习 1**：`get_backend()` 里 `merged_module.__dict__.update(default_module.__dict__)` 和 `update(backend_module.__dict__)` 顺序能反过来吗？
**答案**：不能。必须先放 `default` 再放 `backend`，这样硬件模块的同名函数才会**覆盖** fallback，最终「硬件有的用硬件实现，硬件没有的继承 fallback」。反过来会让 fallback 覆盖掉硬件高性能实现。

**练习 2**：为什么 `_detect_device()` 里分支②（`DEVICE_TYPE`）即使 spec 不可用也不报错，只是 warn 后继续自动检测？
**答案**：为了让环境变量「尽量满足但不致命」。用户设了 `DEVICE_TYPE=xpu` 但机器上没有 XPU 时，与其崩溃，不如退回自动检测找下一个可用硬件，并打 warning 提示设置被忽略了。

---

### 4.3 CPU stub 退化与 CLI-only 模式

#### 4.3.1 概念说明

并非所有运行 LMCache 的机器都有 GPU。两种典型场景：

- **有 torch、无 GPU**：例如 CI 机器、开发笔记本。这时 `torch` 能导入，但 `torch.cuda.is_available()` 为 False。`_detect_device()` 会落到分支④，返回 `StubCPUDevice`。
- **无 torch**：即 u1-l2 提到的 slim 安装（`lmcache-cli`）。`import torch` 直接失败，`_detect_device()` 返回 `(None, "cpu")`，`get_backend()` 也返回 `None`，只保留 CLI 能用。

这两种降级要区分清楚：**stub 是「有 torch 但没卡」的占位实现；CLI-only 是「连 torch 都没有」的最小模式**。

#### 4.3.2 核心流程

```
CPU-only 主机上 _detect_device() 的两条路：

(有 torch)
  import torch ✅
  DEVICE_TYPE 未设 / 不可用
  遍历 _DEVICE_REGISTRY: 全部 is_available()=False
  └─▶ return (StubCPUDevice("cpu"), "cpu")
       │  StubCPUDevice 把 Event/Stream/synchronize/... 全部实现为 no-op
       └─ torch_dev.is_available() == False   ← 关键开关

(无 torch)
  import torch ❌ ImportError
  └─▶ return (None, "cpu")
       │  get_backend() 也返回 None
       └─ sys.modules["lmcache.c_ops"] 不被替换；只打 CLI-only warning
```

`StubCPUDevice` 的设计目标是「让上层代码写得像有 GPU」。它实现了一个 `torch.cuda` 的子集 API：`Event`、`Stream`、`device`、`synchronize`、`set_device`、`current_device`、`device_count`、`get_device_properties`、`empty_cache`，全部返回 no-op 或常量。最关键的是 `is_available()` 返回 `False`——这样上层用 `hasattr(torch_dev, "xxx")` 或 `torch_dev.is_available()` 做能力探测时，会自动走「降级路径」。

#### 4.3.3 源码精读

看 `StubCPUDevice` 的骨架——[lmcache/v1/platform/cpu/stub_cpu_device.py:204-220](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/platform/cpu/stub_cpu_device.py#L204-L220)：

```python
class StubCPUDevice:
    """Stub stand-in for torch_dev in CPU-only test environments."""

    def __init__(self, device_type: str = "cpu") -> None:
        self._device_type = device_type
        self._stream = StubStream(device=device_type)
        self.Event = StubEvent
        self.Stream = StubStream

    def is_available(self) -> bool:
        """Check whether the device backend is available."""
        return False          # ← 关键: 永远 False
```

`Event` / `Stream` 直接绑定到 stub 类（`StubEvent` / `StubStream`），所以 `torch_dev.Stream()` 不会崩，只会拿到一个「什么都不做」的对象。比如 [stub_cpu_device.py:274-280](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/platform/cpu/stub_cpu_device.py#L274-L280)：

```python
def synchronize(self, device: Any = None) -> None:
    """Wait for all streams on the given device to complete."""
    return None               # no-op
```

还有一个细节值得注意——[stub_cpu_device.py:334-335](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/platform/cpu/stub_cpu_device.py#L334-L335)：

```python
def __getattr__(self, name: str) -> Any:
    raise AttributeError(f"StubCPUDevice does not implement '{name}'")
```

stub 用 `__getattr__` 兜底：任何它没显式实现的方法都会**显式抛 `AttributeError`**，而不是默默返回 None。这样上层用 `hasattr(torch_dev, "from_ipc_handle")` 探测「是否支持 CUDA IPC」时能得到确定的 False，而不是拿到一个假对象。

`is_available() == False` 这个开关在真实代码里被用来决定「要不要分配锁页内存」。看 [lmcache/python_ops_fallback.py:421-461](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/python_ops_fallback.py#L421-L461)（节选）：

```python
def _alloc_page_aligned_pinned_view(size: int) -> Tuple[torch.Tensor, int]:
    # Pin the host buffer when an accelerator is present (probed once).
    # StubCPUDevice.is_available returns False on CPU-only hosts.
    global _use_pinned
    if _use_pinned is None:
        _use_pinned = torch_dev.is_available()
    try:
        backing = torch.empty(size + _PAGE_SIZE, dtype=torch.uint8,
                              pin_memory=_use_pinned)
    except RuntimeError:
        ...
        _use_pinned = False
        backing = torch.empty(size + _PAGE_SIZE, dtype=torch.uint8, pin_memory=False)
```

`_use_pinned = torch_dev.is_available()`：有加速器才尝试分配 pin memory（锁页内存，用于加速 DMA 异步传输）；stub 返回 False，于是直接分配普通 pageable 内存。如果运行时某次 pin 分配失败（OOM 等），还会把 `_use_pinned` 永久翻成 False，之后一律走 pageable——这是一条「一次性探测 + 永久降级」的鲁棒策略。

无 torch 的 CLI-only 路径则在包入口处被处理（见 4.2 引用的 [__init__.py:33-36](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/__init__.py#L33-L36)）：`get_backend` 返回 `None`，`sys.modules["lmcache.c_ops"]` 不被替换，只打一条 `"No compute backend loaded; CLI-only mode"` 的 warning。此时 `lmcache ping` / `lmcache describe` / `lmcache query` / `lmcache bench engine` 这类 CLI 仍可工作（它们不碰张量），但 engine 与 storage 路径不能用。

> 重要：**stub 不是 CPU connector**。设计文档明确指出，`gpu_connector/__init__.py` 里没有 `"cpu"` 的路由分支，所以拿 `torch_device_type == "cpu"` 去创建 GPU connector 会抛 `RuntimeError`。stub 只服务于「L1-adapter-only 流程」和「无 torch 加载 CLI」。

#### 4.3.4 代码实践

1. **实践目标**：在 CPU-only 主机上验证 `torch_dev` 退化为 `StubCPUDevice`，并观察 `is_available()` 的返回值。
2. **操作步骤**（示例代码）：

   ```python
   # 示例代码（在无 GPU 主机或 CPU 版 torch 环境运行）
   from lmcache import torch_dev, torch_device_type
   print("type(torch_dev) =", type(torch_dev).__name__)
   print("is_available    =", torch_dev.is_available())
   print("device_count    =", torch_dev.device_count())
   # 触发 __getattr__ 兜底
   print("has from_ipc_handle:", hasattr(torch_dev, "from_ipc_handle"))
   ```
3. **需要观察的现象**：`type` 为 `StubCPUDevice`；`is_available` 为 `False`；`device_count` 为 `1`（stub 固定返回 1）；`has from_ipc_handle` 为 `False`（`__getattr__` 抛 AttributeError 被 `hasattr` 捕获）。
4. **预期结果**：与上一致。若你的主机有 GPU，则 `torch_dev` 是 `torch.cuda` 模块、`is_available` 为 True，本实践需换到无 GPU 环境才能看到 stub。
5. **待本地验证**：无 GPU 环境的具体获取方式视你的机器而定。

#### 4.3.5 小练习与答案

**练习 1**：stub 的 `is_available()` 为什么必须返回 `False`，而不是返回 `True`？
**答案**：因为它是 CPU 占位，不是真加速器。返回 False 才能让上层的能力探测（如 `_use_pinned = torch_dev.is_available()`）正确触发降级（不分配 pin memory、不走 CUDA IPC 路径）。若返回 True，上层会误以为有真 GPU 而调用不存在的硬件能力。

**练习 2**：`StubCPUDevice.__getattr__` 选择「抛 AttributeError」而不是「返回一个空函数」，这对上层代码意味着什么？
**答案**：意味着上层可以用 Python 惯用的 `hasattr(torch_dev, "xxx")` 来做能力探测，得到确定的 True/False；而不是拿到一个看似可用、调用却静默失败的假对象，后者更难排查。

---

### 4.4 底层连接器路由：torch_device_type → Connector

#### 4.4.1 概念说明

回忆 2.2 的三层抽象：中间层硬件无关，但**底层（gpu_connector）是每硬件一套、不做统一**的。原因是不同引擎（vLLM / SGLang / TensorRT-LLM）在不同硬件上的 KV cache 内存布局千差万别，硬抽象会非常混乱。

于是 LMCache 在「创建 GPU 连接器」这一步用一个集中路由函数 `CreateGPUConnector`，按 `torch_device_type` 分发到对应硬件的连接器类。这是「检测层抽象、适配层分发」的典型用法。

#### 4.4.2 核心流程

```
CreateGPUConnector(config, metadata, engine, layout_hints)
 ├─ engine == SGLANG ──▶ 按 torch_device_type 选 xpu 或通用 GPU 连接器
 ├─ engine == VLLM
 │    ├─ _validate_vllm_device_features(config)   # 拒绝不支持的组合
 │    ├─ torch_device_type == "cuda" ─▶ VLLMPagedMemGPUConnectorV2/V3 / Layerwise ...
 │    ├─ torch_device_type == "xpu"  ─▶ VLLMPagedMemXPUConnectorV2/V3 ...
 │    ├─ torch_device_type == "musa" ─▶ VLLMPagedMemMUSAConnectorV2 ...
 │    ├─ torch_device_type == "hpu"  ─▶ VLLMPagedMemHPUConnectorV2
 │    └─ 其它 ─▶ raise RuntimeError("No supported <type> connector found.")
 ├─ engine == TRTLLM ──▶ TRTLLMGPUConnector
 └─ engine == MOCK   ──▶ MockGPUConnector
```

设计文档 [ARCHITECTURE_MULTI_HARDWARE.md](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/docs/design/ARCHITECTURE_MULTI_HARDWARE.md#L90-L97) 把 vLLM 路由总结成一张表（注意：该表成文时还没列出 `musa`，**真实代码已多出 musa 分支**，以代码为准）：

```
torch_device_type == "cuda"  -->  VLLMPagedMemGPUConnectorV2/V3
torch_device_type == "xpu"   -->  VLLMPagedMemXPUConnectorV2
torch_device_type == "hpu"   -->  VLLMPagedMemHPUConnector
torch_device_type == "cpu"   -->  (no GPU connector; raises RuntimeError)
```

#### 4.4.3 源码精读

看路由函数本体——[lmcache/v1/gpu_connector/__init__.py:60-84](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/gpu_connector/__init__.py#L60-L84)（签名与 SGLang 开头）：

```python
def CreateGPUConnector(config, metadata, engine, layout_hints=None) -> GPUConnectorInterface:
    use_gpu = need_gpu_interm_buffer(config)
    if engine == EngineType.SGLANG:
        if torch_device_type == "musa":
            raise ValueError("SGLang on MUSA is not supported; ...")
        ...
```

vLLM 分支是重头戏——[gpu_connector/__init__.py:143-232](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/gpu_connector/__init__.py#L143-L232)（节选 cuda/musa/hpu/else）：

```python
elif engine == EngineType.VLLM:
    _validate_vllm_device_features(config)
    ...
    if torch_device_type == "cuda":
        ...                                          # 按层wise/V3/V2 选
        return VLLMPagedMemGPUConnectorV2.from_metadata(...)
    elif torch_device_type == "xpu":
        return VLLMPagedMemXPUConnectorV2.from_metadata(...)
    elif torch_device_type == "musa":
        return VLLMPagedMemMUSAConnectorV2.from_metadata(metadata, use_gpu, device)
    elif torch_device_type == "hpu":
        return VLLMPagedMemHPUConnectorV2.from_metadata(metadata, use_gpu, device)
    else:
        raise RuntimeError(f"No supported {torch_device_type} connector found.")
```

注意两点：

1. 进入 vLLM 分支**立即**调用 `_validate_vllm_device_features(config)`——[gpu_connector/__init__.py:23-57](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/gpu_connector/__init__.py#L23-L57)。它在「任何设备专属构造之前」就拒掉不支持组合，把潜在的深层崩溃（如 `torch.cuda.Stream()` 或 `torch.device('musa:0')` 在非对应构建上炸掉）提前转成清晰的 `ValueError`。例如 `enable_blending=True` / `use_gpu_connector_v3=True` 只在 `{cuda, xpu}` 支持，`use_layerwise=True` 在 HPU 上不支持。

2. 每个 `elif` 分支里用的是**延迟导入**（`from lmcache.v1.gpu_connector.xpu_connectors import ...` 写在分支内部）。这样在一台 CUDA 主机上，`xpu_connectors` / `musa_connectors` / `hpu_connector` 根本不会被 import——避免因为某个可选硬件依赖缺失而连累主路径。这与 4.1 里 `on_import_error` 静默跳过是同一种「按需加载」哲学。

最后，注意 `CreateGPUConnector` 里反复出现的 `torch_dev.set_device(local_worker_id)` 和 `torch.device(f"{torch_device_type}:{local_worker_id}")`——这正是中间层 `torch_dev` 与底层路由的交汇点：类型字符串 `torch_device_type` 决定选哪个连接器，而 `torch_dev` 提供真正的 `set_device` / `device` 调用。

#### 4.4.4 代码实践

1. **实践目标**：不真正创建连接器（那需要完整引擎上下文），仅通过源码阅读，画出 `torch_device_type → vLLM 连接器` 的完整路由表。
2. **操作步骤**：
   - 打开 [gpu_connector/__init__.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/gpu_connector/__init__.py)。
   - 在 `engine == EngineType.VLLM` 分支里，逐个 `elif torch_device_type ==` 收集「分支条件 + 返回的连接器类 + 来自哪个文件」。
   - 把结果与设计文档第 90–97 行的表对比，标出文档**遗漏**的 `musa`。
3. **需要观察的现象**：你能列出 cuda / xpu / musa / hpu 四个有效分支，外加一个 `else → RuntimeError`。
4. **预期结果**：路由表至少包含 cuda→V2/V3/Layerwise、xpu→XPU V2/V3/Layerwise、musa→MUSA V2/Layerwise、hpu→HPU V2，以及 `cpu`/未知 → RuntimeError。
5. **待本地验证**：连接器类的具体清单可能随版本演进，以你本地代码为准。

#### 4.4.5 小练习与答案

**练习 1**：为什么每个 `elif torch_device_type ==` 分支里用延迟导入，而不是在文件顶部一次性 import 所有连接器？
**答案**：为了「按需加载」——只 import 当前硬件需要的连接器。如果在顶部一次性 import，那么运行在 CUDA 主机上时也会去加载 `hpu_connector`、`musa_connectors`，一旦这些可选硬件的依赖缺失，就会连累整个 `gpu_connector` 包起不来。

**练习 2**：`_validate_vllm_device_features` 为什么要在「设备专属构造之前」运行？
**答案**：为了让不支持的配置以清晰的 `ValueError` 暴露，而不是等到后面真正调用 `torch.cuda.Stream()` 或 `torch.device('musa:0')` 时才崩出深层、难懂的错误。这是「fail fast + 清晰报错」的设计。

---

### 4.5 扩展新硬件：connector 路由 + IPC wrapper 自动发现

#### 4.5.1 概念说明

如果要把一块新硬件（假设叫 `"foo"`）接入 LMCache，要改哪些地方？设计文档 [ARCHITECTURE_MULTI_HARDWARE.md「Adding New Hardware」](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/docs/design/ARCHITECTURE_MULTI_HARDWARE.md#L130-L136) 给了 5 步清单。本模块重点讲其中两个「自动发现」机制，它们让扩展成本尽量低：

1. **DeviceSpec 自动发现**（4.1 已讲）：在 `platform/foo/__init__.py` 里定义一个 `DeviceSpec` 子类即可被 `_DEVICE_REGISTRY` 收集，**零改动**注册。
2. **KV IPC wrapper 自动发现**：跨进程传递 KV 张量时，每硬件需要一个「IPC 包装器」（CUDA 用 `cudaIpcMemHandle_t`，别的硬件各有各的机制）。这套包装器由 `_registry.py` 维护一张**懒加载工厂表**，同样是「定义子类即注册」。

注意这两个 registry 容易混淆，区分要点：

| 机制 | 文件 | 收集的类 | 索引 key | 用途 | 何时填充 |
|------|------|---------|---------|------|---------|
| DeviceSpec 注册表 | `platform/__init__.py` | `DeviceSpec` 子类 | `device_type` | 检测硬件 + 选算子后端 | **导入即填** |
| KV IPC wrapper 工厂表 | `platform/_registry.py` | `DeviceIPCWrapper` 子类 | `device_type` | 跨进程 KV 张量传递 | **首次调用懒填** |

#### 4.5.2 核心流程

KV IPC wrapper 工厂表的懒发现流程：

```
某处首次调用 get_kv_wrapper_factory("cuda")
 └─ _discover_wrappers_once()
      ├─ 加锁 + 双检（防止并发重复扫描）
      ├─ discover_subclasses(platform_pkg, DeviceIPCWrapper, levels=[2,2])
      │     扫第 2 层: platform/cuda/ipc_wrapper.py 等里的 DeviceIPCWrapper 子类
      └─ 对每个子类 _register_discovered_wrapper(cls):
            ├─ _is_default_wrapper == False ? ──是──▶ 跳过（如 RawCudaIPCWrapper）
            ├─ device_type 为空? ──是──▶ warn 跳过
            ├─ 该 key 已有不同工厂? ──是──▶ warn「保留第一个」，跳过
            └─ _KV_WRAPPER_FACTORIES[device_type] = cls.wrap
```

调用方在 [lmcache/integration/vllm/vllm_multi_process_adapter.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/integration/vllm/vllm_multi_process_adapter.py) 里用 `tensor.device.type` 查表，从而**完全不需要 if/elif 链**——新增硬件只要提交自己的 wrapper 子类即可被路由到。

#### 4.5.3 源码精读

看懒发现的核心——[lmcache/v1/platform/_registry.py:54-93](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/platform/_registry.py#L54-L93)：

```python
def _discover_wrappers_once() -> None:
    global _WRAPPERS_DISCOVERED
    if _WRAPPERS_DISCOVERED:        # 快路径: 已发现就直接返回
        return
    with _DISCOVERY_LOCK:
        if _WRAPPERS_DISCOVERED:    # 双检: 拿到锁后再确认一次
            return
        from lmcache.v1.platform.base_ipc_wrapper import DeviceIPCWrapper
        from lmcache.v1.utils.subclass_discovery import discover_subclasses
        import lmcache.v1.platform as platform_pkg
        for cls in discover_subclasses(platform_pkg, DeviceIPCWrapper, levels=[2, 2]):
            _register_discovered_wrapper(cls)
        _WRAPPERS_DISCOVERED = True
```

这是一个经典的「双重检查锁定（double-checked locking）」懒初始化：先无锁判断标志位（快路径），命中再加锁、再加锁后再判断一次，避免两个并发线程同时穿过扫描。注释明确说这是为了防止「第一个并发 caller 和第二个 race，导致重复扫描并打出重复的 `multiple wrappers claim device_type=...` warning」。

注册时的去重规则在 `_register_discovered_wrapper`——[_registry.py:96-127](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/platform/_registry.py#L96-L127)：

```python
def _register_discovered_wrapper(cls: type) -> None:
    if not getattr(cls, "_is_default_wrapper", False):
        return                       # 跳过非默认 wrapper
    device_type: str = getattr(cls, "device_type", "")
    if not device_type:
        logger.warning("Skipping %s: empty device_type ClassVar; ...", cls.__name__)
        return
    factory = getattr(cls, "wrap", cls)
    existing = _KV_WRAPPER_FACTORIES.get(device_type)
    if existing is not None and existing is not factory:
        logger.warning("Multiple KV-wrapper classes claim device_type=%r ...; keeping the first.", ...)
        return
    _KV_WRAPPER_FACTORIES[device_type] = factory
```

为什么要 `_is_default_wrapper` 这个开关？因为同一个 `device_type` 可能有多个 wrapper 子类。以 CUDA 为例，[lmcache/v1/platform/cuda/ipc_wrapper.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/platform/cuda/ipc_wrapper.py) 里有 `CudaIPCWrapper`（默认，`_is_default_wrapper=True`）和 `RawCudaIPCWrapper`（`_is_default_wrapper=False`）。两者 `device_type` 都是 `"cuda"`，但只有默认的那个会被收进工厂表，避免冲突。

查询入口——[_registry.py:146-169](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/platform/_registry.py#L146-L169)：

```python
def get_kv_wrapper_factory(device_type: str) -> Callable[..., Any]:
    _discover_wrappers_once()
    factory = _KV_WRAPPER_FACTORIES.get(device_type)
    if factory is None:
        raise ValueError("No KV-cache wrapper factory registered for device type %r" % device_type)
    return factory
```

这套表还为测试提供了 `snapshot()` / `restore()` / `reset_for_tests()`——[_registry.py:172-223](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/platform/_registry.py#L172-L223)，让测试可以在不污染全局的情况下替换某个硬件的 wrapper，并在 `finally` / fixture teardown 里还原。`snapshot` 甚至把 `_WRAPPERS_DISCOVERED` 标志一起保存，避免「先快照、再发现、再恢复」后看到陈旧的空表。

最后，把「扩展新硬件」的完整清单串起来（综合设计文档与源码）：

1. **新增 DeviceSpec 子类**：在 `lmcache/v1/platform/foo/__init__.py` 里写 `class FooDeviceSpec(DeviceSpec)`，覆盖 `device_type` / `torch_module_name` / `ops_module` / `is_available()`。无需手动注册，4.1 的扫描会自动收进 `_DEVICE_REGISTRY`。
2. **提供算子（可选）**：在 `c_ops/` 加 CUDA 风格 kernel，或在 `python_ops_fallback.py` 加 Python 兜底；`ops_module` 指向它。
3. **实现 GPU 连接器**：建 `gpu_connector/foo_connectors.py`，实现 `GPUConnectorInterface`。
4. **加路由分支**：在 `gpu_connector/__init__.py` 的 `CreateGPUConnector` 里加 `elif torch_device_type == "foo":`。
5. **（可选）实现 IPC wrapper**：建 `platform/foo/ipc_wrapper.py`，写一个 `DeviceIPCWrapper` 子类并设 `device_type="foo"`、`_is_default_wrapper=True`、`wrap` 工厂方法，由 `_registry.py` 自动发现。
6. **中间层代码无需任何改动**——这正是抽象的全部回报。

#### 4.5.4 代码实践

1. **实践目标**：验证 KV IPC wrapper 工厂表是「懒加载」的——首次查询前为空，查询后才有内容。
2. **操作步骤**（示例代码）：

   ```python
   # 示例代码
   from lmcache.v1.platform import _registry as reg
   import lmcache  # 触发 __init__.py

   print("发现前 _WRAPPERS_DISCOVERED =", reg._WRAPPERS_DISCOVERED)
   print("发现前 表内容 =", dict(reg._KV_WRAPPER_FACTORIES))

   # 触发懒发现（cuda 主机）
   try:
       f = reg.get_kv_wrapper_factory("cuda")
       print("cuda wrapper factory =", getattr(f, "__name__", f))
   except ValueError as e:
       print("未注册:", e)

   print("发现后 _WRAPPERS_DISCOVERED =", reg._WRAPPERS_DISCOVERED)
   print("发现后 表内容 keys =", list(reg._KV_WRAPPER_FACTORIES.keys()))
   ```
3. **需要观察的现象**：第一次打印 `_WRAPPERS_DISCOVERED=False`、表为空；调用 `get_kv_wrapper_factory("cuda")` 后变为 `True`、表里有 `cuda` 等 key。
4. **预期结果**：与上一致；表里至少能看到 `cuda`（来自 `CudaIPCWrapper`）。
5. **待本地验证**：表里具体有哪些 key 取决于本机安装了哪些硬件子包，以本地输出为准。

#### 4.5.5 小练习与答案

**练习 1**：DeviceSpec 注册表是「导入即填」，而 KV IPC wrapper 工厂表是「首次调用懒填」。为什么后者要懒？
**答案**：因为跨进程 KV 传递（IPC）只在多进程（MP）架构里才用到，单进程推理路径根本不需要它。懒加载可以让「不用 MP 的进程」完全不付出扫描 + 导入各硬件 ipc_wrapper 的代价，启动更快、依赖更少。

**练习 2**：`CudaIPCWrapper` 和 `RawCudaIPCWrapper` 的 `device_type` 都是 `"cuda"`，但工厂表里只会有一个。是哪一个、由什么决定？
**答案**：是 `CudaIPCWrapper`，因为它设了 `_is_default_wrapper=True`；`RawCudaIPCWrapper` 设了 `_is_default_wrapper=False`，被 `_register_discovered_wrapper` 在注册时跳过。这个开关正是为了让同 device_type 的多个 wrapper 共存而不冲突。

---

## 5. 综合实践

把本讲 5 个模块串起来，完成下面这个「全链路追踪」任务。

**任务**：在一个 Python 进程里，完整复述「从 `import lmcache` 到拿到一个 GPU 连接器」之间，设备抽象层做的全部决策。具体步骤：

1. 先读 [docs/design/ARCHITECTURE_MULTI_HARDWARE.md](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/docs/design/ARCHITECTURE_MULTI_HARDWARE.md)，把那张 ASCII 大图抄一遍，对照本讲 2.2 的三层抽象表，标出「入口层 / 中间层 / 底层」分别对应图里的哪个框。
2. 写一段脚本（示例代码）：

   ```python
   # 示例代码
   import lmcache
   from lmcache import torch_dev, torch_device_type
   from lmcache.v1.platform import current_device_spec, _DEVICE_REGISTRY
   import lmcache.c_ops as c_ops

   print("=== 检测结果 ===")
   print("torch_device_type   =", torch_device_type)
   print("torch_dev           =", torch_dev)
   print("current_device_spec =", type(current_device_spec).__name__)
   print("DeviceSpec 注册表    =", sorted(_DEVICE_REGISTRY.keys()))
   print("c_ops 是否合并模块   =", c_ops.__name__ == "lmcache.c_ops")
   print("pin 是否支持        =", current_device_spec.is_pin_supported)
   ```
3. 运行后，用本讲学到的知识回答：
   - 我的机器上 `_detect_device()` 走的是哪条分支（① 无 torch / ② 环境变量 / ③ 自动遍历 / ④ CPU stub）？依据是什么？
   - `c_ops` 里的算子，哪些来自 `python_ops_fallback`、哪些来自硬件专属模块？（提示：可以用 `getattr` 看函数的 `__module__`。）
   - 如果我想强制把检测切到某个硬件，该怎么用 `DEVICE_TYPE` 环境变量？设错了会怎样？
4. **画一张「torch_device_type → connector 路由」图**：以 vLLM 引擎为例，把 `CreateGPUConnector` 里 cuda / xpu / musa / hpu / else 五条分支画成流程图，并在每条分支标注「返回的连接器类 + 来自哪个文件」。
5. **回答 CPU-only 主机问题**（这是本讲规格要求的实践）：
   - 在 **有 torch、无 GPU** 的主机上，`_detect_device()` 返回什么？
     **答案**：返回 `(StubCPUDevice("cpu"), "cpu")`，即 torch_dev 是 stub、类型字符串是 `"cpu"`。
   - 在 **无 torch** 的主机上（slim 安装），`_detect_device()` 返回什么？
     **答案**：返回 `(None, "cpu")`，且 `get_backend()` 也返回 `None`，进入 CLI-only 模式，只打 warning 不替换 `c_ops`。

> 如果你的主机始终有 GPU、无法亲眼看到 stub 路径，可把第 5 步标注为「待本地验证」，并靠阅读 [stub_cpu_device.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/platform/cpu/stub_cpu_device.py) 与 [__init__.py:72-128](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/platform/__init__.py#L72-L128) 推理得出结论。

## 6. 本讲小结

- LMCache 用「**检测一次，全局复用**」的 `torch_dev` / `torch_device_type` 抽象掉硬件差异；检测点就在 `lmcache/__init__.py` 导入期，称为 monkey patch 点。
- `DeviceSpec` 注册表（`platform/__init__.py` 的 `_DEVICE_REGISTRY`）在导入期由 `discover_subclasses` 自动收集所有硬件子类，**定义子类即注册**；`DeviceSpec` 本身可实例化为「全 False」的兜底。
- `get_backend()` 把「Python fallback + 硬件算子」合并成一个模块并替换 `sys.modules["lmcache.c_ops"]`，让所有 `import lmcache.c_ops` 透明地拿到正确实现；合并顺序是先 fallback 后硬件覆盖。
- 两条降级路径要分清：**有 torch 无卡** → `StubCPUDevice`（no-op，`is_available()` 为 False）；**无 torch** → `(None, "cpu")` 的 CLI-only 模式。
- 底层 `gpu_connector` 是「每硬件一套、不做统一」，由 `CreateGPUConnector` 按 `torch_device_type` 集中路由（vLLM：cuda / xpu / musa / hpu / else→RuntimeError），且每分支用延迟导入实现按需加载。
- KV IPC wrapper 工厂表（`platform/_registry.py`）是第二套「自动发现」机制，懒加载 + 双重检查锁定 + `_is_default_wrapper` 去重，专门服务多进程跨实例 KV 传递；新增硬件无需改 dispatcher。

## 7. 下一步学习建议

- **横向**：本讲的「自动发现」思想（`discover_subclasses`）在 CLI 子命令注册（u1-l4 已提及 `discover_subclasses`）里也用了，读 [lmcache/v1/utils/subclass_discovery.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/utils/subclass_discovery.py) 把这套公共工具彻底吃透。
- **纵向（下一讲 u2-l2）**：进入底层「GPU 连接器层」，精读 `gpu_connectors.py` 里 `VLLMPagedMemGPUConnector` 如何把引擎 KV 布局转成 LMCache 内部格式，以及 `kv_format/detection.py` 如何检测 HND/NHD 布局。
- **纵深（内存）**：`torch_dev` 的下一层是内存分配器，`current_device_spec.pin_memory` 的真实用途将在 u4-l6「内存管理与分配器」展开，届时你会看到 pin memory 对异步传输的意义。
- **配套设计文档**：随时回看 [ARCHITECTURE_MULTI_HARDWARE.md](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/docs/design/ARCHITECTURE_MULTI_HARDWARE.md)，它是本讲全部内容的一页纸总结。
