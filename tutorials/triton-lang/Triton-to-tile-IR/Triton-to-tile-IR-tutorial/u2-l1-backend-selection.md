# 后端选择机制：ENABLE_TILE 如何切换后端

## 1. 本讲目标

本讲聚焦一个看似简单、实则贯穿整条编译链路的问题：**当你在 shell 里写下 `ENABLE_TILE=1` 时，Triton 内部究竟发生了什么，才让一个 kernel 走上 CUDA Tile IR 后端而不是默认的 NVIDIA PTX 后端？**

学完本讲你应该能够：

- 说出 `driver` 这个全局单例是如何被惰性创建的，以及 `_create_driver()` 如何被 `ENABLE_TILE` 短路。
- 解释 `compile()` 为什么要把 `target.backend` 从 `cuda` 改写成 `tileir`，以及不改写会出什么问题。
- 描述 `triton.backends` entry point 与 in-tree 后端发现机制，并知道 `triton.backends.tileir` 这个包在磁盘上究竟指向哪里。
- 画出「环境变量 → driver 实例 → target.backend」的完整决策流。

本讲是 u1-l4「端到端编译链路总览」的直接续篇。上一讲我们已经知道编译分 `make_ttir`/`make_tileir`/`make_cubin` 三段，但刻意回避了一个前置问题：**Triton 一开始是怎么决定用 TileIRBackend 的？** 本讲就来补上这块拼图。

## 2. 前置知识

在进入源码前，先用通俗语言建立三个概念。

**后端（backend）是什么？** 在 Triton 里，一个「后端」是一个同时提供 `compiler`（编译器，负责把 IR 变成机器码）和 `driver`（驱动，负责探测设备、生成启动器、真正把 kernel 拉起来）的组合。本仓库里有三个后端：`nvidia`（走 PTX，默认）、`amd`（走 HIP）、`tileir`（本仓库新增，走 CUDA Tile IR）。

**什么是 driver 单例？** 全局只有一个 `driver` 对象（`DriverConfig` 的实例），它缓存「当前用哪个后端的 driver」。绝大多数代码都通过 `driver.active` 拿到当前 driver。因为是缓存，所以**环境变量必须在第一次访问 `driver.active` 之前设置好**——这正是 u1-l2 强调「开关须在 `import triton` 之前设置」的原因。

**什么是 entry point？** 它是 Python 打包标准里的一种「插件注册表」。一个包在安装时可以声明「我属于 `triton.backends` 这个组」，之后任何程序都能用 `importlib.metadata.entry_points()` 枚举出所有声明过的成员。Triton 用这个机制让后端可插拔：只要装了某个后端包，运行时就能自动发现它，而不需要把后端名字写死在代码里。

> 术语速查：
> - **oait**：上游 OpenAI Triton，走 PTX 后端，默认。
> - **nvtriton**：本仓库发布的 wheel，走 TileIR 后端。
> - **in-tree backend**：源码内置在 `third_party/<名字>/` 下、随仓库一起构建的后端，`tileir` 就是其中之一。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `python/triton/backends/__init__.py` | 后端发现：枚举所有已注册后端，构建全局 `backends` 字典。 |
| `python/triton/runtime/driver.py` | driver 单例 `DriverConfig` 与选择函数 `_create_driver()`，`ENABLE_TILE` 在此短路。 |
| `python/triton/compiler/compiler.py` | `compile()` 入口，改写 `target.backend`；`make_backend()` 按 target 路由到具体后端。 |
| `python/triton/backends/compiler.py` | `GPUTarget` 数据类与 `BaseBackend` 抽象基类（`supports_target` 约定）。 |
| `third_party/tileir/backend/compiler.py` | `TileIRBackend`，其 `supports_target` 只认 `backend == "tileir"`。 |
| `third_party/tileir/backend/driver.py` | `TileIRDriver`、`get_current_target()`、`is_active()` 与全局单例 `GlobalTileIRDriver`。 |
| `setup.py` | 构建期注册 entry point、生成 `triton.backends.tileir` 符号链接。 |

> 永久链接 base（本讲所有链接均基于此 HEAD）：
> `https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/`

## 4. 核心概念与源码讲解

按「先发现、再选择、最后路由」的依赖顺序，本讲拆成三个最小模块：

- **4.1 后端发现**：`triton.backends.tileir` 这个包是怎么出现的。
- **4.2 driver 选择逻辑**：`ENABLE_TILE` 如何让单例 `driver` 指向 `TileIRDriver`。
- **4.3 target 改写**：`compile()` 如何保证最终路由到 `TileIRBackend`。

### 4.1 后端发现：entry point 与 in-tree 后端

#### 4.1.1 概念说明

Triton 不把「有哪些后端」写死，而是在运行时动态发现。发现的结果是一个全局字典 `backends`，形如：

```
{
  "nvidia":  Backend(compiler=CudaBackend,   driver=CudaDriver),
  "amd":     Backend(compiler=HIPBackend,    driver=HIPDriver),
  "tileir":  Backend(compiler=TileIRBackend, driver=TileIRDriver),
}
```

其中 `Backend` 只是一个装着两个类（`compiler`、`driver`）的冻结数据类。

发现分两条路径：

1. **默认路径（entry point）**：枚举 `triton.backends` 这个组的所有 entry point。每个 entry point 声明了「后端名 = 包路径」，例如 `tileir = triton.backends.tileir`。
2. **快路径（in-tree）**：当 `TRITON_BACKENDS_IN_TREE=1` 时，跳过（可能很慢的）entry point 扫描，直接遍历 `triton.backends` 命名空间下的子目录。

无论哪条路径，对每个后端都做同一件事：导入它的 `compiler` 和 `driver` 子模块，然后用 `_find_concrete_subclasses` 在模块里找出「唯一的那个具体子类」。

关键问题是：`triton.backends.tileir` 这个包在磁盘上根本不存在于源码树里（上一讲 u1-l3 已经提过）。它是构建期由 `setup.py` 建立的**符号链接**：`python/triton/backends/tileir` → `third_party/tileir/backend`。所以「发现」其实是发现了一个指向 `third_party/tileir/backend` 的入口。

#### 4.1.2 核心流程

```
安装期 (setup.py)
  ├── prepare("tileir")  → backend_dir = third_party/tileir/backend
  ├── add_link_to_backends() → 符号链接 python/triton/backends/tileir -> third_party/tileir/backend
  └── get_entry_points() → 注册 entry point: "tileir = triton.backends.tileir"

导入期 (import triton)
  └── backends/__init__.py 末尾: backends = _discover_backends()
        ├── (默认) 枚举 entry_points(group="triton.backends")
        │     或
        ├── (TRITON_BACKENDS_IN_TREE=1) 扫描 triton.backends.* 目录
        └── 对每个后端:
              ├── import triton.backends.<name>.compiler  → 找唯一 BaseBackend 子类
              └── import triton.backends.<name>.driver    → 找唯一 DriverBase 子类
        ⇒ backends = { name: Backend(compiler, driver), ... }
```

#### 4.1.3 源码精读

先看运行时发现入口。`_discover_backends` 是模块级函数，在 `import triton` 时被调用一次：

[python/triton/backends/__init__.py:38-63](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/triton/backends/__init__.py#L38-L63) —— 两条发现路径的分支。关键片段：

```python
# 快路径：只扫 in-tree 目录
if skip_entrypoints_env == "1":
    for name in os.listdir(root):           # root = triton/backends/
        ...
        compiler = importlib.import_module(f"triton.backends.{name}.compiler")
        driver   = importlib.import_module(f"triton.backends.{name}.driver")
        backends[name] = Backend(_find_concrete_subclasses(compiler, BaseBackend),
                                 _find_concrete_subclasses(driver, DriverBase))
    return backends

# 默认路径：枚举 entry point
for ep in entry_points().select(group="triton.backends"):
    compiler = importlib.import_module(f"{ep.value}.compiler")
    driver   = importlib.import_module(f"{ep.value}.driver")
    backends[ep.name] = Backend(_find_concrete_subclasses(compiler, BaseBackend),
                                _find_concrete_subclasses(driver, DriverBase))
```

注意 `ep.value` 就是 `triton.backends.tileir`，所以 `import_module(f"{ep.value}.compiler")` 实际加载的是 `triton.backends.tileir.compiler`——而这个包通过符号链接指向 `third_party/tileir/backend/compiler.py`。

[python/triton/backends/__init__.py:19-29](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/triton/backends/__init__.py#L19-L29) —— `_find_concrete_subclasses` 用反射在模块里找出唯一的、非抽象的具体子类；找到 0 个或超过 1 个都报错。这正是「每个后端必须恰好有一个 compiler 类和一个 driver 类」的强制约定。

最终结果挂在模块级变量上：

[python/triton/backends/__init__.py:66](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/triton/backends/__init__.py#L66) —— `backends: dict[str, Backend] = _discover_backends()`，导入即执行。

再看构建期：entry point 是怎么注册的、符号链接是怎么来的。

[setup.py:510-518](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/setup.py#L510-L518) —— `get_entry_points()` 注册 `triton.backends` 组，对每个后端生成 `"tileir = triton.backends.tileir"`：

```python
entry_points["triton.backends"] = [f"{b.name} = triton.backends.{b.name}" for b in backends]
```

[setup.py:375](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/setup.py#L375) —— `backends = [*BackendInstaller.copy(["nvidia", "amd", "tileir"]), ...]`，`tileir` 在这里被列为 in-tree 后端之一。

[setup.py:62-97](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/setup.py#L62-L97) —— `prepare()` 算出 `backend_dir = third_party/tileir/backend`、`install_dir = python/triton/backends/tileir`，并断言 `backend/` 下必须存在 `compiler.py` 和 `driver.py`（第 91-92 行）。

[setup.py:427-432](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/setup.py#L427-L432) —— `add_link_to_backends()` 用 `update_symlink(install_dir, backend_dir)` 建立符号链接，把 `python/triton/backends/tileir` 指向 `third_party/tileir/backend`。

> 一句话串起来：**构建期 setup.py 建 symlink + 注册 entry point；导入期 `__init__.py` 通过 entry point（或目录扫描）发现后端、反射出唯一的 compiler/driver 类。** 这一整套机制与 `ENABLE_TILE` 无关——它只负责「让 TileIRBackend/TileIRDriver 成为可被发现、可被 import 的存在」。真正决定「要不要用」的是下一节。

#### 4.1.4 代码实践

**实践目标**：亲手确认 `triton.backends.tileir` 在磁盘上是一个符号链接，并追踪它指向哪里。

**操作步骤**：

1. 在仓库已安装（`pip install -e .`）的环境里，用 Python 打印出 `tileir` 后端模块的真实路径：

   ```python
   # 示例代码
   import triton.backends.tileir.compiler as c
   print(c.__file__)
   ```

2. 在 shell 里查看该路径是不是符号链接（把上一步打印的路径代入）：

   ```bash
   ls -l $(python -c "import triton.backends.tileir.compiler as c; print(c.__file__)" | xargs dirname | xargs dirname)
   ```

**需要观察的现象**：`c.__file__` 应当落在 `python/triton/backends/tileir/compiler.py`，而 `ls -l` 应显示 `python/triton/backends/tileir -> ../../../../third_party/tileir/backend`（相对层数视安装方式而定）这样的符号链接。

**预期结果**：确认源码树里没有 `python/triton/backends/tileir/` 实体目录，它只是一个指向 `third_party/tileir/backend` 的链接。若你用的是 wheel 并存安装（u1-l2），链接关系同样存在于 wheel 安装目录内。

> 若环境未安装 triton 或缺少符号链接（例如纯源码浏览），可改为直接阅读 `setup.py:427-432` 与 `third_party/tileir/backend/` 目录，理解映射关系即可，运行结果「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：如果 `TRITON_BACKENDS_IN_TREE=1`，`_discover_backends` 还会去读 entry point 吗？

> **答案**：不会。`TRITON_BACKENDS_IN_TREE=1` 走快路径，直接 `os.listdir` 扫描 `triton.backends` 命名空间目录，函数末尾的 `return backends` 提前结束，跳过 entry point 枚举（见 `__init__.py:44-55`）。这条快路径适合 CI 等场景，能避免 entry point 扫描的开销。

**练习 2**：假如有人不小心在 `third_party/tileir/backend/compiler.py` 里定义了两个 `BaseBackend` 的具体子类，会发生什么？

> **答案**：`_find_concrete_subclasses`（`__init__.py:19-29`）在发现「>1 个具体子类」时会抛 `RuntimeError`。后端必须恰好暴露一个 compiler 类、一个 driver 类，这是硬性约定。

---

### 4.2 driver 选择逻辑：_create_driver 与 DriverConfig 单例

#### 4.2.1 概念说明

后端「被发现」不等于「被启用」。`backends` 字典只是把三个后端都列出来；到底当前用哪一个，由全局单例 `driver`（`DriverConfig` 实例）决定。

`DriverConfig` 用**惰性缓存**：第一次访问 `driver.active` 时才真正创建 driver 对象，之后一直复用。创建动作委托给 `_create_driver()`，而后者最开头就有一条 `ENABLE_TILE` 的硬短路——只要 `ENABLE_TILE==1`，就无条件返回 `TileIRDriver()`，完全跳过后端按 GPU 自动探测的逻辑。

这一点很关键：**普通情况下 driver 是「探测出来的」**（看哪块 GPU 活跃、是否设了 `TRITON_DEFAULT_BACKEND`），而 `ENABLE_TILE` 把它变成「指定出来的」——不管你机器上有什么卡，都强制走 TileIR。代价是 `TileIRDriver.is_active()` 内部仍会校验 CUDA 可用性与硬件（Blackwell），真正编译/启动失败时才暴露。

#### 4.2.2 核心流程

```
任意代码访问 driver.active  (首次)
  └── DriverConfig.active (driver.py:42)
        └── 若 _active is None → self.default
              └── DriverConfig.default (driver.py:36)
                    └── 若 _default is None → _create_driver()

_create_driver()  (driver.py:8)
  ├── ENABLE_TILE=="1" ?  ──是──▶ return TileIRDriver()      ← 硬短路，不探测
  │
  └── 否 ──▶ TRITON_DEFAULT_BACKEND 设了？
              ├── 是 ──▶ backends[selected].driver()          ← 显式指定
              └── 否 ──▶ 在 is_active() 为真的后端里挑，要求恰好一个  ← 自动探测
```

#### 4.2.3 源码精读

先看单例本身。`DriverConfig` 只缓存两样东西：`_default`（默认 driver）和 `_active`（当前 driver，初始等于 default）。

[python/triton/runtime/driver.py:30-52](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/triton/runtime/driver.py#L30-L52)：

```python
@property
def default(self) -> DriverBase:
    if self._default is None:
        self._default = _create_driver()   # 惰性创建
    return self._default

@property
def active(self) -> DriverBase:
    if self._active is None:
        self._active = self.default        # active 初始等于 default
    return self._active
```

因为 `active` 一旦被访问过就被缓存（`_active` 不再是 `None`），所以**开关必须在首次访问前设置**——这就是 u1-l2 反复强调的环境变量时序问题。

[python/triton/runtime/driver.py:55](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/triton/runtime/driver.py#L55) —— `driver = DriverConfig()`，进程级唯一实例。

再看选择函数。`_create_driver` 最优先的就是 `ENABLE_TILE` 分支：

[python/triton/runtime/driver.py:8-27](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/triton/runtime/driver.py#L8-L27)：

```python
def _create_driver() -> DriverBase:
    # If tile is explicitly enabled, force TileIRDriver
    if os.environ.get("ENABLE_TILE", "0") == "1":
        from ..backends.tileir.driver import TileIRDriver   # 延迟 import
        return TileIRDriver()

    # 否则：TRITON_DEFAULT_BACKEND 显式指定，或按 is_active() 自动探测
    selected = os.environ.get("TRITON_DEFAULT_BACKEND", None)
    ...
    active_drivers = [x.driver for x in backends.values() if x.driver.is_active()]
    ...
```

注意三个细节：

1. **短路优先级最高**：`ENABLE_TILE` 判断在 `TRITON_DEFAULT_BACKEND` 和自动探测之前，且不调用 `is_active()`，是无条件强制。
2. **延迟 import**：`from ..backends.tileir.driver import TileIRDriver` 只在真正命中时才执行。这个 import 能成功，依赖 4.1 节的符号链接/entry point 把 `triton.backends.tileir` 暴露出来。
3. **对比普通路径**：非 TileIR 分支用 `x.driver.is_active()` 过滤 `backends.values()`（第 24 行），要求恰好一个活跃后端；而 TileIR 分支完全绕过这套探测。

被选中的 `TileIRDriver` 决定了「当前 target 是什么」。看它如何回报 target：

[third_party/tileir/backend/driver.py:554-559](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/driver.py#L554-L559) —— `get_current_target` 把设备能力算成整数后，返回一个 `backend="tileir"` 的 `GPUTarget`：

```python
def get_current_target(self):
    device = self.get_current_device()
    capability = self.get_device_capability(device)
    capability = capability[0] * 10 + capability[1]
    warp_size = 32
    return GPUTarget("tileir", capability, warp_size)
```

注意它**写死返回 `backend="tileir"`**。这一点和 NVIDIA 后端返回 `backend="cuda"` 形成对照，也正是下一节 `compile()` 要做改写的对照基准。

[third_party/tileir/backend/driver.py:571-582](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/driver.py#L571-L582) —— `is_active()` 静态方法，是普通自动探测路径会调用的判断：要求 `torch.cuda.is_available()` 且 `ENABLE_TILE=="1"` 且非 HIP：

```python
@staticmethod
def is_active():
    try:
        import torch
        return (torch.cuda.is_available()
                and os.environ.get("ENABLE_TILE", "0") == "1"
                and (torch.version.hip is None))
    except ImportError:
        return False
```

[third_party/tileir/backend/driver.py:605](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/driver.py#L605) —— `GlobalTileIRDriver = TileIRDriver()`，模块级单例，给 `torch.compile` 路径复用（见 4.3 节）。

> 小结：`ENABLE_TILE=1` → `_create_driver` 短路返回 `TileIRDriver()` → 它的 `get_current_target()` 永远回报 `backend="tileir"`。于是「选哪个 driver」和「target.backend 是什么」被绑在了一起。

#### 4.2.4 代码实践

**实践目标**：观察 `driver.active` 的惰性创建，并验证 `ENABLE_TILE` 对选中 driver 类型的影响。

**操作步骤**：

1. 不设 `ENABLE_TILE`，启动 Python：

   ```python
   # 示例代码
   import triton
   from triton.runtime import driver
   print(type(driver.active).__name__)
   ```

2. 退出，再以 `ENABLE_TILE=1 python -c '...'` 重新跑同样两行。

**需要观察的现象**：第一次大概率打印 `CudaDriver`（默认 PTX 后端）；第二次应打印 `TileIRDriver`。

**预期结果**：证实 `_create_driver` 的 `ENABLE_TILE` 分支把 driver 换成了 `TileIRDriver`。这与 u1-l2 的「验证金标准 `type(driver.active).__name__`」一致。

> 由于本仓库需要 Blackwell GPU + CTK 13.1 才能真正激活，在没有合适硬件时 `is_active()` 可能为假、或 `TileIRDriver()` 构造报错。此时请把本实践当作「源码阅读型实践」：对照 `driver.py:8-12` 说明「若能成功构造，类型必为 TileIRDriver」即可，实际运行结果「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `ENABLE_TILE` 分支里不调用 `TileIRDriver.is_active()`，而普通自动探测分支却要调用？

> **答案**：`ENABLE_TILE` 表达的是用户的**显式强制意图**，应当无条件生效，所以直接 `return TileIRDriver()`（`driver.py:10-12`）。而普通分支是在多个后端里**按硬件自动挑选**，需要 `is_active()` 判断哪个后端的 GPU 真实存在并可用（`driver.py:24`）。注意强制归强制，硬件不满足时真正的失败会推迟到编译/启动阶段才暴露。

**练习 2**：如果先执行了 `import triton`，再 `os.environ["ENABLE_TILE"]="1"`，`driver.active` 会变成 TileIRDriver 吗？

> **答案**：通常不会。`driver.active` 是惰性缓存的 `DriverConfig` 属性（`driver.py:42-46`），一旦首次访问过，`_active` 就不再是 `None`，后续读取只返回缓存值，不再走 `_create_driver()`。所以环境变量必须在首次访问 `driver.active`（通常是 `import triton` 触发的链路）之前设置。这正是 u1-l2 的核心结论。

---

### 4.3 target 改写：compile() 中 cuda→tileir

#### 4.3.1 概念说明

选好了 driver，只是确定了「谁来探测设备、谁来启动」。但 `compile()` 最终要用一个 `target` 去路由到具体的 **backend（compiler）**，而路由依据是 `target.backend` 这个字符串。这里有个坑：**`torch.compile` / Inductor 调用 Triton 时，会显式塞进来一个 `target.backend=="cuda"` 的 target。**

如果不处理，`make_backend(target)` 会按 `"cuda"` 匹配到 NVIDIA 后端，于是即便 driver 是 TileIRDriver，编译却走了 PTX 后端——driver 和 backend 割裂，行为错乱。

解决办法就是 `compile()` 开头的一行改写：当 `ENABLE_TILE==1` 且传入的 `target.backend=="cuda"` 时，把 target 重建为 `GPUTarget("tileir", ...)`。这样后续 `make_backend` 就能正确路由到 `TileIRBackend`。

于是存在两条通往 `tileir` 的路径，殊途同归：

- **隐式路径**：调用方没传 target → `target = driver.active.get_current_target()` → TileIRDriver 已写死返回 `"tileir"`。
- **显式路径**：调用方（torch.compile）传了 `target.backend=="cuda"` → `compile()` 改写成 `"tileir"`。

#### 4.3.2 核心流程

```
compile(src, target, options)  (compiler.py:231)
  │
  ├── ENABLE_TILE=="1" 且 target.backend=="cuda"?
  │     └── 是 → target = GPUTarget("tileir", target.arch, target.warp_size)   ← 改写
  │
  ├── target is None?
  │     └── 是 → target = driver.active.get_current_target()   ← 隐式，已是 "tileir"
  │
  ├── backend = make_backend(target)   (compiler.py:243)
  │     └── make_backend: 过滤 supports_target(target)==True 的后端，要求恰好一个
  │           └── 只有 TileIRBackend.supports_target 认 "tileir"  ⇒ 选中 TileIRBackend
  │
  └── backend.add_stages(...)  ⇒ 注册 make_ttir / make_tileir / make_cubin  (承接 u1-l4)
```

#### 4.3.3 源码精读

[python/triton/compiler/compiler.py:231-243](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/triton/compiler/compiler.py#L231-L243) —— `compile()` 的开头，改写、兜底、路由三步：

```python
def compile(src, target=None, options=None, _env_vars=None):
    ...
    if os.environ.get("ENABLE_TILE", "0") == "1" and target is not None and target.backend == "cuda":
        # torch.compile will set the target to cuda, but we need to compile the kernel for tileir
        target = GPUTarget("tileir", target.arch, target.warp_size)

    if target is None:
        target = driver.active.get_current_target()
    assert isinstance(target, GPUTarget), "target must be of GPUTarget type"
    backend = make_backend(target)
```

注意第 237 行注释直接点明了动机：torch.compile 会把 target 设成 cuda，但我们需要为 tileir 编译。

[python/triton/compiler/compiler.py:375-380](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/triton/compiler/compiler.py#L375-L380) —— `make_backend` 用 `supports_target` 做唯一匹配：

```python
def make_backend(target: GPUTarget) -> BaseBackend:
    actives = [x.compiler for x in backends.values() if x.compiler.supports_target(target)]
    if len(actives) != 1:
        raise RuntimeError(f"{len(actives)} compatible backends for target ({target.backend}) ...")
    return actives[0](target)
```

只要 `target.backend=="tileir"`，三个后端里只有 `TileIRBackend` 会让 `supports_target` 返回真：

[third_party/tileir/backend/compiler.py:140-142](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/compiler.py#L140-L142)：

```python
@staticmethod
def supports_target(target: GPUTarget):
    return target.backend == "tileir"
```

而 `target` 本身只是个三字段冻结数据类：

[python/triton/backends/compiler.py:8-14](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/triton/backends/compiler.py#L8-L14)：

```python
@dataclass(frozen=True)
class GPUTarget(object):
    backend: str           # "cuda" / "hip" / "tileir"
    arch: Union[int, str]  # 计算能力，如 100 (Blackwell)
    warp_size: int
```

改写之所以可行，正是因为 `arch` 和 `warp_size` 在 cuda 与 tileir 之间含义一致（都是计算能力与 warp 大小），只是 `backend` 标签不同。

最后看启动侧的一条互补分支。`CompiledKernel` 在加载二进制时，如果发现 `ENABLE_TILE==1`，会把 active driver 显式设成全局单例 `GlobalTileIRDriver`，确保 launcher 也走 TileIR：

[python/triton/compiler/compiler.py:481-483](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/triton/compiler/compiler.py#L481-L483)：

```python
if os.environ.get("ENABLE_TILE") == "1":
    from ..backends.tileir.driver import GlobalTileIRDriver
    driver.set_active(GlobalTileIRDriver)
```

这里复用的正是 4.2 节末尾的 `GlobalTileIRDriver` 单例（`third_party/tileir/backend/driver.py:605`）。这段代码紧接其后还用 `driver.active.utils.load_binary(...)`（`compiler.py:489-509`）走 TileIR 专用的二进制加载路径——但那些属于「启动」细节，本讲点到为止，详细展开留给 u2-l5。

> 旁证：autotuner 也是从 driver 的 target 读 backend 的——[python/triton/runtime/autotuner.py:32](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/triton/runtime/autotuner.py#L32) `self.backend = driver.active.get_current_target().backend`。当 driver 是 TileIRDriver 时，这里读到的就是 `"tileir"`，autotune 自然也只在 TileIR 后端里搜配置。

#### 4.3.4 代码实践

**实践目标**：跟踪 `compile()` 中 target 的来源，确认两条路径都收敛到 `"tileir"`。

**操作步骤**：

1. 打开 [compiler.py:236-243](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/triton/compiler/compiler.py#L236-L243)，对照下表填写每种调用场景下 `target.backend` 的取值演变：

   | 调用场景 | 传入 target | ENABLE_TILE | 命中改写？ | 最终 target.backend |
   | --- | --- | --- | --- | --- |
   | 普通 `@triton.jit` 编译 | `None` | `1` | 否（target is None） | `"tileir"`（来自 `get_current_target`） |
   | torch.compile 调用 | `backend=="cuda"` | `1` | 是 | `"tileir"`（改写） |
   | 普通 `@triton.jit` 编译 | `None` | 未设 | 否 | `"cuda"`（CudaDriver 探测） |

2. 在 `make_backend`（`compiler.py:375-380`）处 mentally 代入：当 `target.backend=="tileir"`，`backends.values()` 里哪几个的 `supports_target` 返回真？

**需要观察的现象**：第三列「命中改写」只在「显式传 cuda 且 ENABLE_TILE=1」时为是；其余路径靠 driver 自身回报。

**预期结果**：只有 `TileIRBackend` 认 `"tileir"`，`actives` 长度恰为 1，`make_backend` 返回 `TileIRBackend(target)`，随后 `backend.add_stages` 注册 TileIR 的三段编译（衔接 u1-l4）。如果改写失效、target 残留 `"cuda"`，则 `TileIRBackend.supports_target` 返回假、`actives` 会落到 NVIDIA 后端，driver 与 backend 错配。

#### 4.3.5 小练习与答案

**练习 1**：假如删掉 `compile()` 里第 236-238 行的改写，普通 `@triton.jit`（不经过 torch.compile）的 kernel 还能正确走 TileIR 吗？

> **答案**：能。普通 `@triton.jit` 调用 `compile()` 时通常不传 target（`target is None`），于是走第 240-241 行 `target = driver.active.get_current_target()`；而 `ENABLE_TILE=1` 时 driver 是 `TileIRDriver`，它的 `get_current_target` 写死返回 `backend="tileir"`（`driver.py:554-559`）。改写只服务于「调用方硬塞 cuda target」的场景（主要是 torch.compile）。

**练习 2**：为什么 `compile()` 改写 target 时只替换 `backend` 字段，而保留 `arch` 和 `warp_size`？

> **答案**：因为对 CUDA 类硬件而言，计算能力（arch）和 warp 大小（warp_size）对 cuda 与 tileir 含义一致，只是后端「标签」不同。`GPUTarget("tileir", target.arch, target.warp_size)`（`compiler.py:238`）只换标签，保留了 torch 传来的真实硬件信息，让后续 `TileIRBackend` 能拿到正确的 arch 去编译。

---

## 5. 综合实践

把三个模块串起来，完成本讲规格里的核心任务：**画出「环境变量 → driver 实例 → target.backend」的完整决策流，并用一段 Python 内省验证 `backends` 字典与 driver 类型。**

### 实践目标

验证三件事：(1) `tileir` 后端确实在 `backends` 字典里被发现；(2) `ENABLE_TILE=1` 让 `driver.active` 成为 `TileIRDriver`；(3) 它的 target 回报 `backend=="tileir"`。

### 操作步骤

1. **阅读并补全决策流图**。把下面方框里的缺省处补齐（答案见文末）：

   ```
   ┌─ 导入期 ─────────────────────────────────────────────┐
   │ backends = _discover_backends()                      │
   │   通过 ①__________ 枚举，或 TRITON_BACKENDS_IN_TREE=1 │
   │   时扫描目录；对 tileir 导入其 ②______ / driver 子模块 │
   │   → backends["tileir"] = Backend(TileIRBackend, ...) │
   └──────────────────────────────────────────────────────┘
   ┌─ 首次访问 driver.active ────────────────────────────┐
   │ _create_driver():                                    │
   │   ENABLE_TILE=="1" → return ③__________()  (硬短路)  │
   │   否则按 is_active() 自动探测                          │
   └──────────────────────────────────────────────────────┘
   ┌─ compile() ──────────────────────────────────────────┐
   │ target.backend=="cuda" 且 ENABLE_TILE==1             │
   │   → 改写为 GPUTarget("④______", arch, warp_size)     │
   │ target is None → driver.active.get_current_target()  │
   │   (TileIRDriver 写死返回 backend="④______")          │
   │ backend = make_backend(target)                       │
   │   只有 ⑤__________.supports_target 认 "tileir"       │
   └──────────────────────────────────────────────────────┘
   ```

2. **Python 内省**（示例代码，需要已安装本仓库）：

   ```python
   # 示例代码
   import triton.backends as B
   print("已发现后端:", list(B.backends.keys()))
   tileir = B.backends.get("tileir")
   print("tileir compiler 类:", tileir.compiler.__name__ if tileir else None)
   print("tileir driver   类:", tileir.driver.__name__   if tileir else None)

   from triton.runtime import driver
   print("当前 driver 类型:", type(driver.active).__name__)
   print("当前 target.backend:", driver.active.get_current_target().backend)
   ```

3. 分别在「不设 `ENABLE_TILE`」与「`ENABLE_TILE=1`」两种环境下跑第 2 步，对比输出。

### 需要观察的现象

- 第 1 步：`backends` 字典应包含 `tileir` 键，其 compiler/driver 类分别为 `TileIRBackend`/`TileIRDriver`。
- 第 2 步（`ENABLE_TILE=1`）：`type(driver.active).__name__` 为 `TileIRDriver`，`get_current_target().backend` 为字符串 `"tileir"`。
- 第 2 步（未设）：通常为 `CudaDriver`，`backend` 为 `"cuda"`。

### 预期结果

三模块闭环：发现机制让 TileIR 类可见 → `ENABLE_TILE` 让单例 driver 指向 TileIRDriver → `compile()` 的改写与 `make_backend` 的路由共同锁定 `TileIRBackend`。决策流图填空答案：① `entry_points(group="triton.backends")` ② `compiler` ③ `TileIRDriver` ④ `tileir` ⑤ `TileIRBackend`。

> 本综合实践依赖真实 GPU/CUDA 环境才能真正激活 TileIRDriver；在纯源码阅读环境下，请以「读懂三段源码、补全决策流图」为完成标准，Python 内省的运行结果「待本地验证」。

## 6. 本讲小结

- 后端发现与启用是两件事：`_discover_backends()`（entry point 或 in-tree 目录扫描）只让 `tileir` 后端**可见**，`setup.py` 通过符号链接 + entry point 注册把它接入。
- 全局单例 `driver`（`DriverConfig`）惰性缓存当前 driver；`ENABLE_TILE==1` 让 `_create_driver()` 最优先短路返回 `TileIRDriver()`，跳过 GPU 自动探测。
- 因为 `DriverConfig.active` 是缓存属性，`ENABLE_TILE` 必须在首次访问 `driver.active` 之前设置（承接 u1-l2）。
- `TileIRDriver.get_current_target()` 写死返回 `backend="tileir"`，把「选哪个 driver」与「target.backend」绑定。
- `compile()` 开头把 `target.backend=="cuda"` 改写成 `"tileir"`，专门兜底 `torch.compile` 显式塞 cuda target 的场景；普通 `@triton.jit` 靠隐式 target 收敛。
- `make_backend()` 用 `supports_target` 做唯一匹配，只有 `TileIRBackend` 认 `"tileir"`，从而锁定编译器与 `add_stages` 注册的三段流水线。

## 7. 下一步学习建议

本讲回答了「为什么是 TileIRBackend」，接下来可以顺着两条线深入：

- **向「选项」深入**：建议下一讲学习 [u2-l2 编译选项与环境配置](u2-l2-options-and-env-config.md)，了解 `TileIROptions` 的 occupancy/num_ctas 等旋钮，以及 `TileIREnvConf` 如何解析 `TILEIR_ENABLE_APPROX` 等环境变量——它们决定了「在 TileIRBackend 内部如何编译」。
- **向「编译阶段」深入**：建议学习 [u2-l3 三段式编译流水线](u2-l3-compile-stages.md)，看 `TileIRBackend.add_stages` 如何挂载 `make_ttir`/`make_tileir`/`make_cubin`——这正是本讲末尾 `make_backend` 选定后端之后的下一步。
- **源码延伸阅读**：对照阅读 `python/triton/runtime/autotuner.py:32`，理解 autotuner 也通过 `driver.active.get_current_target().backend` 读后端，体会「driver 单例 + target.backend」这套机制在编译、启动、autotune 三处的统一性。
