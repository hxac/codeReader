# 后端注册表与分发机制 dispatcher.py

> 本讲属于「U2 统一算子接口与后端调度」单元的第 2 讲，承接 [u2-l1 统一算子接口 ops.py](u2-l1-unified-op-interface.md)。
> 在 u2-l1 里你已经知道：`ops.py` 里每个算子是一个「只抛 `NotImplementedError` 的 stub」，而真正干活的实现被 `@register_impl` 挂到了同一个算子名下；分发器按「当前后端 → 兜底后端 → stub」三级查找。
> 本讲要回答的核心问题是：**这一切在运行时到底是怎么发生的？** 也就是：注册表 `_REGISTRY` 这个全局字典是「什么时候、被谁、按什么顺序」填满的？`dispatch` 装饰器返回的 `wrapper` 凭什么能记住「我叫 softmax、我的兜底是 pytorch」？一次 `tilegym.ops.softmax(x)` 调用，从 Python 层一直走到 `ct.launch`，中间经历了哪些步骤？

## 1. 本讲目标

学完本讲，你应当能够：

- 把 `_REGISTRY` 当作一个**运行时数据结构**来理解：它的嵌套字典契约、`"default"` 键的来源与用途、以及它「随 import 而生长」的动态特性。
- 说清 `register_impl` 的执行时机：它是**导入后端模块时的副作用**，并被 `ops/__init__.py` 的条件导入所门控——因此同一份代码在不同机器上注册表内容不同。
- 看懂 `dispatch` 是一个**装饰器工厂**，它返回的 `wrapper` 通过**闭包**记住了 `name / fallback_backend / default_impl`，并用 `@functools.wraps` 保留了 stub 的签名与文档。
- 逐行追踪一次 `tilegym.ops.softmax(x)` 调用，画出从 stub 到 `cutile/softmax.py` 再到 `ct.launch` 的完整调用链。
- 讲清 fallback 的几处工程细节：tilecpp 的**延迟可用性探测**、`_LOGGED_WARNINGS` 的**警告去重**、`DISABLE_FALLBACK` 作用在**两个**位置，并掌握 `get_registry_info / print_registry_info` 这类自省工具的用法。

## 2. 前置知识

本讲默认你已掌握 u2-l1 的全部内容（`@dispatch` / stub / `fallback_backend` / 显式 `backend=` 参数 / 三级查找的概念）。这里只补三个本讲会用到的 Python 术语：

- **装饰器工厂（decorator factory）**：形如 `@dispatch("softmax")` 的写法，`dispatch` 先被调用、返回一个「真正的装饰器」，这个装饰器再去装饰下面的函数。所以 `dispatch` 是「生产装饰器的函数」。
- **闭包（closure）**：内层函数引用了外层函数的局部变量。即使外层函数已经返回，内层函数仍然「记得」那些变量。本讲里 `wrapper` 闭包记住了 `name`、`fallback_backend`、`default_impl`。
- **导入副作用（import-time side effect）**：Python 在 `import` 一个模块时会**从上到下执行**该模块的顶层语句。所以一个写在模块顶层的 `@register_impl(...)` 会在「模块被导入的那一刻」就执行——这正是 TileGym 注册实现的时机。

一句话直觉：如果说 u2-l1 讲的是「菜单长什么样、服务员按什么规则端菜」，那么本讲讲的是「**菜单是怎么被一张张写出来、塞进柜台抽屉的，以及服务员的大脑（wrapper 闭包）是怎么工作的**」。我们关心的不再是「规则」，而是「机制与时机」。

## 3. 本讲源码地图

本讲以 `dispatcher.py` 为核心，向外辐射到「触发注册的导入链」和「被注册的 cuTile 实现」。

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [src/tilegym/backend/dispatcher.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/dispatcher.py) | **本讲主角**：`_REGISTRY`、`register_impl`、`dispatch` 与自省函数 | 注册表声明、注册与查找的全部实现细节 |
| [src/tilegym/backend/\_\_init\_\_.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/__init__.py) | backend 子包入口，重新导出 `dispatch/register_impl/...` | 名称如何从子包流出，被 `ops.py` 与各后端 `import` |
| [src/tilegym/backend/selector.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/selector.py) | 当前后端变量与可用性探测 | `get_current_backend()` 的返回值、`is_tilecpp_available()` 的延迟缓存 |
| [src/tilegym/ops/ops.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/ops.py) | 统一算子接口（stub） | softmax 的 stub 定义，作为调用链的起点 |
| [src/tilegym/ops/\_\_init\_\_.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/__init__.py) | ops 包入口，**条件导入**各后端 | 第 15-24 行的 `if is_backend_available("cutile")` 如何门控注册 |
| [src/tilegym/ops/cutile/\_\_init\_\_.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/__init__.py) | cuTile 后端聚合导入 | `from . import softmax` 如何触发 `register_impl` 执行 |
| [src/tilegym/ops/cutile/softmax.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py) | cuTile 版 softmax 真实实现 | `@register_impl("softmax", backend="cutile")` 与 `_Softmax.apply` |

> 提示：`selector.py` 的完整可用性探测机制是 [u2-l3](u2-l3-backend-selector.md) 的主题，本讲只在用到 `get_current_backend()` 和 `is_tilecpp_available()` 时点到为止。

---

## 4. 核心概念与源码讲解

本讲 4 个最小模块构成一条「由数据到机制、由机制到调用」的链：**注册表长什么样（4.1）→ 它是怎么被填满的（4.2）→ 查找它的 wrapper 大脑怎么工作（4.3）→ 查找中的兜底工程细节（4.4）**。最后在第 5 节，我们用这些知识**逐行追踪一次 softmax 调用**。

### 4.1 全局注册表 _REGISTRY：数据结构与自省

#### 4.1.1 概念说明

u2-l1 给过 `_REGISTRY` 的静态快照。本模块要把它当作一个**运行时数据结构**来理解，关注三件事：契约、`"default"` 键、以及它的「动态生长」特性。

**契约**：`_REGISTRY` 是一个嵌套字典，外层键是算子名（字符串），内层键是后端名（字符串），值是该后端对该算子的实现函数。可以记作：

\[
R:\ \text{op\_name} \times \text{backend} \longrightarrow \text{implementation}
\]

它是一张稀疏表：并非每个算子都有所有后端的实现（比如 `softmax` 可能只有 `cutile`，而 `rms_norm` 可能有 `cutile` 和 `triton`）。嵌套字典天然支持这种稀疏性。

**`"default"` 键**：u2-l1 提过 `@dispatch` 会把 stub 自动登记到 `_REGISTRY[name]["default"]`。要强调的是：**这个 `"default"` 键并不参与运行时的兜底查找**——兜底链路用的是 wrapper 闭包里捕获的 `default_impl`（见 4.3）。`"default"` 键主要服务于**自省**：让 `get_available_backends_for_op` / `get_registry_info` 能列出「这个算子至少存在一个 stub」。

**动态生长**：`_REGISTRY` 是一个**模块级可变全局**，初始为空，随着后端模块被导入而**逐步**被填充。这意味着它的内容**取决于运行环境**——同一份 TileGym 代码，在一台装了 `cuda-tile` 的机器上 `_REGISTRY["softmax"]` 含 `cutile`，在另一台没装的机器上则不含。

#### 4.1.2 核心流程

注册表的生命周期可以画成：

```text
进程启动
   │
   ▼
import tilegym  ──► _REGISTRY = {}   （dispatcher.py 模块加载时声明，此时为空）
   │
   ▼
后端模块被导入（见 4.2 的导入链）
   │  每执行一次 @register_impl("X", backend="Y")
   ▼
_REGISTRY["X"]["Y"] = <实现函数>      （表不断长大）
   │
   ▼
用户调用 ops.X(...)  ──► wrapper 查 _REGISTRY["X"][当前后端]
```

关键认知：**注册是导入的副产品，查找是调用的主线**。两者在时间上完全分离——先填表，后查表。

#### 4.1.3 源码精读

注册表的声明只有一行，但它是一个可变全局，类型标注写得很清楚：

[dispatcher.py:30-31](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/dispatcher.py#L30-L31) —— 全局注册表声明：

```python
# Global registry with structure: {function_name: {backend_name: implementation}}
_REGISTRY: Dict[str, Dict[str, Callable]] = {}
```

`@dispatch` 装饰时自动写入的 `"default"` 键：

[dispatcher.py:128-132](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/dispatcher.py#L128-L132) —— 把 stub 登记为 `"default"`：

```python
# Register default implementation
if name not in _REGISTRY:
    _REGISTRY[name] = {}
_REGISTRY[name]["default"] = default_impl
```

围绕注册表，`dispatcher.py` 提供了三个**自省工具**，它们是调试 TileGym 分发问题最直接的武器：

[dispatcher.py:139-152](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/dispatcher.py#L139-L152) —— 列出某算子支持的所有后端：

```python
def get_available_backends_for_op(name: str) -> list:
    if name not in _REGISTRY:
        return ["default"]
    return list(_REGISTRY[name].keys())
```

> 注意这个函数对「未注册算子名」也返回 `["default"]` 而不是空列表——这是一种保守设计，避免调用方还要处理空集合。

[dispatcher.py:155-172](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/dispatcher.py#L155-L172) —— 返回 `{算子: {后端: "模块.函数名"}}` 的全量信息：

```python
def get_registry_info() -> Dict[str, Dict[str, str]]:
    result = {}
    for func_name, backends in _REGISTRY.items():
        result[func_name] = {
            backend: (impl.__module__ + "." + impl.__name__ ...)
            for backend, impl in backends.items()
        }
    return result
```

它把「实现函数对象」还原成可读的 `模块.函数名` 字符串，方便定位「某实现到底写在哪个文件」。`print_registry_info()`（[dispatcher.py:175-189](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/dispatcher.py#L175-L189)）则是它的人类可读打印版。这三个函数都被 `backend/__init__.py` 重新导出（[backend/\_\_init\_\_.py:10-13](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/__init__.py#L10-L13)），最终出现在 `tilegym` 顶层（[tilegym/\_\_init\_\_.py:35-39](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/__init__.py#L35-L39)）。

#### 4.1.4 代码实践

**实践目标**：亲眼看到 `_REGISTRY` 是「随 import 生长」的动态表，并用自省工具把它的内容打印出来。

**操作步骤**：

1. 运行下面这段「源码阅读型 + 可运行」脚本：

   ```python
   # 示例代码：观察 _REGISTRY 的动态生长与自省
   import tilegym                                  # 触发整条导入链，填充 _REGISTRY
   from tilegym.backend import get_registry_info, get_available_backends_for_op

   # (a) 看 softmax 注册了哪些后端
   print("softmax 后端：", get_available_backends_for_op("softmax"))

   # (b) 看 softmax 各实现分别来自哪个模块
   for backend, where in get_registry_info()["softmax"].items():
       print(f"  softmax / {backend:8s} -> {where}")

   # (c) 全表统计：有多少个算子名、每个算子平均几个后端
   info = get_registry_info()
   print(f"注册算子总数：{len(info)}")
   ```

2. 对照 [cutile/softmax.py:356](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py#L356) 的 `@register_impl("softmax", backend="cutile")`，确认 (b) 输出里 `cutile` 那一行指向 `tilegym.ops.cutile.softmax.softmax`。

**需要观察的现象**：

- `softmax` 的后端列表里至少包含 `default`（stub）和 `cutile`（若环境可用）。这证明 `_REGISTRY` 确实在 `import tilegym` 之后被填进了内容。
- 不同机器上列表可能不同（有人多一个 `triton` 或 `tilecpp`），这正是「动态生长、环境相关」的体现。

**预期结果**：

- 典型输出形如 `softmax 后端：['default', 'cutile']`，`cutile -> tilegym.ops.cutile.softmax.softmax`。
- 若当前环境 `cuda-tile` 不可用，则只能看到 `['default']`，此时记为「待本地验证」，并回到 4.2 解释原因。

#### 4.1.5 小练习与答案

**练习 1**：`get_available_backends_for_op("不存在的算子")` 会返回什么？为什么不是空列表？

> **参考答案**：返回 `["default"]`（见 [dispatcher.py:149-150](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/dispatcher.py#L149-L150)）。这是一种保守设计：对未知算子名也返回一个非空列表，避免调用方还要特判空集合；同时语义上「至少有个默认 stub」也勉强成立（虽然该 stub 实际并不存在，调用时仍会因 `_REGISTRY` 查不到而走兜底/报错）。

**练习 2**：为什么说 `_REGISTRY` 的内容「取决于运行环境」？请举一个具体例子。

> **参考答案**：因为 `_REGISTRY` 是靠导入后端模块时的 `@register_impl` 副作用来填充的，而**是否导入某个后端**取决于该后端是否可用（见 4.2）。例如 `cuda-tile` 未安装时，`ops/__init__.py` 不会 `from . import cutile`，于是没有任何 `@register_impl(..., backend="cutile")` 被执行，`_REGISTRY["softmax"]` 里就不会有 `cutile` 键。所以同一份代码、不同机器、不同注册表。

---

### 4.2 register_impl 注册：实现如何登记（含注册时机生命周期）

#### 4.2.1 概念说明

`register_impl` 解决的问题是：**后端实现如何把自己「登记」进注册表，且与接口定义解耦？**

它的设计有三个要点：

1. **它是一个装饰器**：写在后端实现函数的上方，形如 `@register_impl("softmax", backend="cutile")`。被装饰的函数**原样返回**（`return func`），所以这个实现函数仍然可以像普通函数一样被直接调用、被 `__init__.py` 再次 `import` 导出。
2. **注册是导入副作用**：`@register_impl(...)` 这一行在**模块被导入时**就执行，把 `{算子名: {后端: 函数}}` 写进 `_REGISTRY`。换言之，**「导入」即「注册」**。
3. **注册被条件导入门控**：是否导入（因而是否注册）某个后端，由 `ops/__init__.py` 里的 `if is_backend_available(...)` 决定。这是「环境相关」特性的直接来源。

为什么要把「注册」做成导入副作用、而不是要求用户手动调用某个 `register()` 函数？因为这样可以让**只要 `import tilegym`，所有可用后端的实现就自动各就各位**，用户无需关心注册顺序。代价是注册表的最终状态依赖于「哪些后端模块成功被导入」。

#### 4.2.2 核心流程

以 softmax 的 cuTile 实现为例，它的注册由下面这条**导入链**触发：

```text
用户：import tilegym
  └─ tilegym/__init__.py: from . import ops                      # 导入 ops 包
       └─ ops/__init__.py: if is_backend_available("cutile"):    # 门控：cutile 可用？
            └─ from . import cutile                              # 导入 cutile 后端包
                 └─ cutile/__init__.py: from . import softmax     # 导入 softmax 模块
                      └─ softmax.py 顶层执行：
                           @register_impl("softmax", backend="cutile")
                           def softmax(...): ...                 # 副作用：_REGISTRY["softmax"]["cutile"] = softmax
```

这条链有 5 个环节，**任何一环中断**（比如 `is_backend_available("cutile")` 为假，或 `import cuda.tile` 失败），末端那行 `@register_impl` 就不会执行，`_REGISTRY["softmax"]` 里就不会出现 `cutile`。这就是为什么 4.1.4 的实践结果「因机器而异」。

#### 4.2.3 源码精读

先看 `register_impl` 本体——它是一个「参数化装饰器」：外层接收 `(name, backend)`，返回内层 `decorator`，`decorator` 接收被装饰函数 `func`，写表后**原样返回 `func`**：

[dispatcher.py:34-54](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/dispatcher.py#L34-L54) —— `register_impl` 的完整定义：

```python
def register_impl(name: str, backend: str):
    def decorator(func):
        if name not in _REGISTRY:
            _REGISTRY[name] = {}
        _REGISTRY[name][backend] = func          # 写表
        logger.debug(f"[Backend Register] Registered '{backend}' implementation for '{name}'")
        return func                              # 原样返回，func 仍可直接调用
    return decorator
```

> 重点：`return func` 而不是返回某个包装器。这意味着 `cutile/softmax.py` 里的 `softmax` 被装饰后**仍然是它自己**，所以 `cutile/__init__.py` 第 62 行的 `from .softmax import softmax` 能正常把这个函数再导出一次，供需要**绕过分发、直接调用**内部实现的场景使用。

被装饰的 cuTile softmax 实现就一行装饰器 + 函数定义：

[cutile/softmax.py:356-361](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py#L356-L361) —— cuTile 实现把自己挂到 `"softmax"` 名下：

```python
@register_impl("softmax", backend="cutile")
def softmax(
    x,
    use_tma=False,
    **kwargs,
):
```

这条导入链的「门控」环节在 `ops/__init__.py`，它用 `is_backend_available("cutile")` 决定是否导入 cutile 包：

[ops/\_\_init\_\_.py:15-24](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/__init__.py#L15-L24) —— cutile 的条件导入，整条注册链的开关：

```python
if is_backend_available("cutile"):
    try:
        from . import cutile
    except (ImportError, RuntimeError):
        import warnings
        warnings.warn("Cutile backend import failed, cutile operations will not be available")
        cutile = None
else:
    cutile = None
```

注意它有**两层保护**：外层 `is_backend_available("cutile")` 做「能不能用」的预判（看能否 `import cuda.tile`，详见 [selector.py:30-44](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/selector.py#L30-L44)）；内层 `try/except` 兜住「预判通过、但实际导入仍失败」的情况（比如某个内核模块有 bug）。一旦走进 `cutile = None` 分支，cutile 目录下所有 `@register_impl(..., backend="cutile")` 都不会执行。

进入 cutile 包后，`cutile/__init__.py` 把各个算子模块逐一导入，softmax 就在其中：

[cutile/\_\_init\_\_.py:10-35](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/__init__.py#L10-L35) —— cuTile 后端聚合导入，触发各算子的 `register_impl`：

```python
if is_backend_available("cutile"):
    ...
    from . import softmax      # 导入即注册：执行 softmax.py 顶层的 @register_impl
    ...
```

> 旁证：`register_impl` 和 `dispatch` 都从 `tilegym.backend` 导入（见 [cutile/softmax.py:10](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py#L10) 的 `from tilegym.backend import register_impl`，与 [ops.py:19](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/ops.py#L19) 的 `from tilegym.backend import dispatch`）。接口侧用 `dispatch`、实现侧用 `register_impl`，两者通过同一个 `_REGISTRY` 字典「隔空握手」——这就是「定义与实现解耦」在代码层面的落点。

#### 4.2.4 代码实践

**实践目标**：验证「导入即注册」，并观察到「门控关闭时注册不发生」。

**操作步骤**：

1. 阅读下面这段脚本，它通过设置环境变量在 `import tilegym` **之前**强制禁用 cutile，从而关闭注册门控：

   ```python
   # 示例代码：观察门控对注册表的影响（待本地验证）
   import os
   os.environ["TILEGYM_DISABLE_CUTILE"] = "1"   # 强制 is_cutile_available() 返回 False
   import tilegym
   from tilegym.backend import get_available_backends_for_op
   print("禁用 cutile 后，softmax 后端：", get_available_backends_for_op("softmax"))
   ```

   > `TILEGYM_DISABLE_CUTILE=1` 会让 `is_cutile_available()` 直接返回 `False`（见 [selector.py:47-51](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/selector.py#L47-L51)），进而 `is_backend_available("cutile")` 为假，于是 `ops/__init__.py` 走 `else: cutile = None` 分支。

2. 对照不设置该环境变量时的输出，比较两次 `softmax` 后端列表的差异。

**需要观察的现象**：

- 设置 `TILEGYM_DISABLE_CUTILE=1` 后，`softmax` 后端列表里**缺少** `cutile`（只剩 `default`，可能还有 triton 等）。
- 这证明：cutile 实现之所以出现在注册表里，**仅仅是因为 cutile 模块被导入了**；一旦门控关闭、模块不导入，注册就不了了之。

**预期结果**：

- 禁用前：`['default', 'cutile', ...]`；禁用后：`['default', ...]`（无 `cutile`）。
- 若你的环境本身就没有 cutile，两次输出可能一样，此时记为「待本地验证」，但你应该能从源码层面解释「为什么」。

#### 4.2.5 小练习与答案

**练习 1**：`register_impl` 装饰器返回的是 `func` 本身，而不是一个包装器。这样设计有什么好处？

> **参考答案**：好处是「注册」与「可被直接调用」两不误。返回 `func` 本身意味着被装饰的函数仍然是原来的函数对象，可以被 `__init__.py` 再次 `from .softmax import softmax` 导出，供需要绕过分发、直接调用内部实现的代码使用（例如测试或内部组合调用）。如果返回包装器，这种直接引用就会变得困难。

**练习 2**：假设有人新增了一个算子，在 `ops.py` 写了 `@dispatch("foo")` 的 stub，也写了 cutile 实现 `@register_impl("foo", backend="cutile")`，但**忘了**把实现模块加进 `cutile/__init__.py` 的导入列表。调用 `tilegym.ops.foo(x)` 会发生什么？

> **参考答案**：因为实现模块从未被导入，`@register_impl("foo", backend="cutile")` 这行从未执行，`_REGISTRY["foo"]` 里只有 `@dispatch` 自动登记的 `default`，没有 `cutile`。于是调用走兜底链路：当前后端 `cutile` 查不到 → 兜底 `pytorch` 也查不到 → 回退 stub → 抛出 `NotImplementedError: foo is not implemented for cutile`。这正是「注册时机」被忽视时最常见的故障形态——**实现写了但没被导入，等于没注册**。

---

### 4.3 dispatch wrapper：闭包与一次调用的完整路径

#### 4.3.1 概念说明

u2-l1 讲过 `@dispatch` 会把 stub 改造成「会自动选后端的 wrapper」。本模块要回答：**这个改造在 Python 层面是怎么实现的？wrapper 凭什么「记得」自己是哪个算子、兜底是谁？**

答案是**装饰器工厂 + 闭包**：

- `dispatch(name, fallback_backend)` 是**装饰器工厂**：你调用它，它返回一个「真正的装饰器」`decorator`。
- `decorator(default_impl)` 接收 stub 函数，返回 `wrapper`。
- `wrapper` 是一个**闭包**：它引用了外层 `dispatch` / `decorator` 的局部变量 `name`、`fallback_backend`、`default_impl`。即使 `dispatch` 和 `decorator` 早已返回，`wrapper` 依然「记得」这些值。

于是**每个算子都有自己专属的一个 `wrapper` 对象**，它的闭包里封存着「我叫什么、我兜底是谁、我的 stub 是谁」。这就是为什么 `ops.softmax` 和 `ops.rms_norm` 虽然长得像（都是 wrapper），却能在被调用时表现出不同的查找行为——它们的闭包内容不同。

另一个细节：`wrapper` 上有 `@functools.wraps(default_impl)`，它把 stub 的 `__name__`、`__doc__`、`__module__` 等元信息**复制**到 `wrapper`。所以 `help(tilegym.ops.softmax)` 显示的仍然是 stub 的名字和 docstring，用户看到的「接口契约」只有一份。

#### 4.3.2 核心流程

`dispatch` 的嵌套结构：

```text
dispatch(name="softmax", fallback_backend="pytorch")      # 第 1 层：工厂
   │  返回 decorator
   ▼
decorator(default_impl=<ops.py 里的 softmax stub>)         # 第 2 层：真正的装饰器
   │  定义 wrapper（闭包捕获 name, fallback_backend, default_impl）
   │  把 stub 登记为 _REGISTRY["softmax"]["default"]
   │  返回 wrapper
   ▼
wrapper  ──► 替换掉原始 softmax，成为对外暴露的 ops.softmax
```

被调用时，`wrapper(*args, **kwargs)` 内部经过 5 个决策点（详见 4.3.3）：

```text
1. 取显式 backend    kwargs.pop("backend", None)
2. 定当前后端        explicit_backend 优先，否则 get_current_backend()
3. tilecpp 健康检查  若 current=="tilecpp" 且不可用 → current 改为 fallback_backend
4. 查当前后端        _REGISTRY[name][current] 命中 → 调用并返回     ✅ 主路径
5. 查兜底 / 默认      详见 4.4
```

#### 4.3.3 源码精读

`dispatch` 的工厂骨架，注意三层嵌套与 `@functools.wraps`：

[dispatcher.py:60-74](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/dispatcher.py#L60-L74) —— 工厂 + 装饰器 + wrapper 头部：

```python
def dispatch(name: str, fallback_backend: str = "pytorch"):
    def decorator(default_impl):
        @functools.wraps(default_impl)        # 把 stub 的元信息复制给 wrapper
        def wrapper(*args, **kwargs):
            ...
        ...
        return wrapper
    return decorator
```

`wrapper` 的「决策点 1、2」——解析本次调用要用哪个后端：

[dispatcher.py:74-83](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/dispatcher.py#L74-L83) —— 取出并消费显式 `backend`，否则读进程级当前后端：

```python
def wrapper(*args, **kwargs):
    explicit_backend = kwargs.pop("backend", None)
    if explicit_backend is not None:
        current_backend = explicit_backend      # 调用级覆盖，优先级最高
    else:
        current_backend = get_current_backend() # 进程级当前后端（默认 "cutile"）
    logger.debug(f"[Backend Dispatch] Function: '{name}', Current backend: '{current_backend}'")
```

注意 `kwargs.pop` 的双重作用：既取值、又把 `backend` 从 `kwargs` 里**删除**，所以后续把 `*args, **kwargs` 转发给真实实现时，实现函数收到的 `kwargs` 里已经没有 `backend` 了。

「决策点 4」——主路径，命中当前后端就直接调用：

[dispatcher.py:94-97](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/dispatcher.py#L94-L97) —— 命中当前后端，直接返回（绝大多数调用的归宿）：

```python
if name in _REGISTRY and current_backend in _REGISTRY[name]:
    logger.debug(f"[Backend Dispatch] Using '{current_backend}' implementation for '{name}'")
    return _REGISTRY[name][current_backend](*args, **kwargs)
```

这一行 `_REGISTRY[name][current_backend](*args, **kwargs)` 就是「分发」的落点：把 4.2 注册进来的那个后端实现函数取出来、原样调用。对 softmax 而言，取出的就是 [cutile/softmax.py:357](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py#L357) 的那个 `softmax` 函数。

> 闭包验证：`name` 和 `current_backend` 都不在 `wrapper` 的局部作用域里被赋值（除了 `current_backend`），它们来自外层 `dispatch` 的形参——这就是闭包。你可以用 `ops.softmax.__wrapped__`（由 `functools.wraps` 提供）或检查 `ops.softmax.__code__.co_freevars` 来感知这些自由变量。

#### 4.3.4 代码实践

**实践目标**：用 Python 自省手段，证明「每个算子的 wrapper 都是独立的闭包对象，且封存了各自的 name/fallback」。

**操作步骤**：

1. 运行下面这段「源码阅读型 + 可运行」脚本：

   ```python
   # 示例代码：探测 wrapper 的闭包与自由变量
   import tilegym

   for opname in ["softmax", "rms_norm", "fmha"]:
       fn = getattr(tilegym.ops, opname)
       # functools.wraps 让 wrapper 保留了 stub 的 __name__ / __doc__
       print(f"{opname}: __name__={fn.__name__!r}, is wrapper={hasattr(fn, '__wrapped__')}")
       # 自由变量名揭示了闭包捕获了哪些外层变量
       print(f"   自由变量: {fn.__code__.co_freevars}")
   ```

2. 对照 [dispatcher.py:72-74](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/dispatcher.py#L72-L74)，解释 `co_freevars` 里出现的名字（通常包含 `name`、`fallback_backend`、`default_backend` 等）分别对应什么。

**需要观察的现象**：

- 三个算子的 `__name__` 都等于各自的算子名（如 `'softmax'`），这是 `@functools.wraps` 的功劳——尽管它们底层都是同一个 `wrapper` 函数模板。
- `co_freevars` 列出了闭包捕获的自由变量名，证明 wrapper 确实「记住」了外层的 `name` 等。

**预期结果**：

- 输出表明三个 wrapper 是**不同的函数对象**（因为闭包绑定的值不同），但共享同一段 `wrapper` 代码。
- 若你的 Python 版本对 `co_freevars` 的呈现略有差异，以实际输出为准，记为「待本地验证」也无妨——重点是理解「闭包捕获」这件事。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `ops.softmax` 和 `ops.rms_norm` 是两个不同的函数对象，而不是同一个？

> **参考答案**：因为 `@dispatch("softmax")` 和 `@dispatch("rms_norm", fallback_backend="triton")` 各自调用了一次 `dispatch` 工厂，各产生了一个 `decorator`，又各自装饰了各自的 stub，于是各产生了一个**独立的 `wrapper` 闭包对象**。两个 wrapper 共享同一段代码（`dispatcher.py` 里的 `wrapper` 定义），但闭包里绑定的 `name`、`fallback_backend`、`default_impl` 不同，所以是两个对象、行为不同。

**练习 2**：如果不写 `@functools.wraps(default_impl)`，会对用户体验造成什么影响？

> **参考答案**：`wrapper` 会暴露自己的元信息——`help(tilegym.ops.softmax)` 会显示成 `wrapper(*args, **kwargs)` 而不是 `softmax(x, use_tma=False, **kwargs)`，docstring 也会丢失。`@functools.wraps` 把 stub 的 `__name__/__doc__/__module__` 等复制到 wrapper，保证用户看到的「接口契约」仍然是 stub 那一份，文档与签名都正确。

---

### 4.4 fallback 的工程细节：tilecpp 延迟探测、警告去重、DISABLE_FALLBACK

#### 4.4.1 概念说明

u2-l1 已经讲过 fallback 的「三级火箭」概念（当前后端 → 兜底后端 → stub）和 `DISABLE_FALLBACK` 的基本作用。本模块聚焦 u2-l1 没有展开的三处**工程细节**，它们都藏在 wrapper 的「决策点 3 和 5」里：

1. **tilecpp 的延迟可用性探测**：即便用户把当前后端设成了 `tilecpp`，wrapper 也会在**每次 dispatch 时**复查 tilecpp 是否真的可用；若不可用，先把 `current_backend` 改写成 `fallback_backend` 再继续查。这是为了配合「tilecpp 可用性探测被设计成延迟且昂贵」这一决策。
2. **警告去重**：降级到兜底后端时会打一条 warning。为了不在循环里刷屏，同一种「算子 × 当前后端 × 兜底后端」组合只警告一次。
3. **DISABLE_FALLBACK 作用在两个位置**：它既拦截「降级到兜底后端」，也拦截「回退到默认 stub」。理解这两处才能准确预测 `DISABLE_FALLBACK=1` 时的报错文本。

这三处细节共同决定了「降级」在真实运行中的可观察行为。

#### 4.4.2 核心流程

把 wrapper 的「决策点 3、5」展开：

```text
current_backend 已定（决策点 1、2 之后）
   │
   ├─ 决策点 3：if current_backend == "tilecpp" and not is_tilecpp_available():
   │               current_backend = fallback_backend     # 静默改写，避开 tilecpp 启动失败
   │
   ├─ 决策点 4：查 _REGISTRY[name][current_backend]  ──命中──► 调用，返回
   │
   ├─ 决策点 5a：查 _REGISTRY[name][fallback_backend]
   │               ├─ DISABLE_FALLBACK=1 → raise NotImplementedError（拒绝降级）
   │               ├─ 命中 → 打【去重后的】warning，调用兜底实现，返回
   │               └─ 未命中 ↓
   │
   └─ 决策点 5b：调用 default_impl（stub）
                   ├─ DISABLE_FALLBACK=1 → raise NotImplementedError
                   └─ 否则 → stub 自己 raise NotImplementedError
```

去重用的 key 是「算子名_当前后端_兜底后端」三元组，存在模块级集合 `_LOGGED_WARNINGS` 里。

#### 4.4.3 源码精读

「决策点 3」——tilecpp 的延迟健康检查。注释解释了为什么这么做：

[dispatcher.py:85-92](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/dispatcher.py#L85-L92) —— tilecpp 不可用时，把当前后端静默改写为兜底后端：

```python
# Defer the tilecpp nvcc-version probe until the first actual
# dispatch to tilecpp. is_tilecpp_available() is cached, so the
# subprocess runs at most once per process. If unavailable, fall
# through to the registered fallback ...
if current_backend == "tilecpp" and not is_tilecpp_available():
    current_backend = fallback_backend
```

> 为什么延迟？因为 `is_tilecpp_available()` 内部要跑 `nvcc --version` 子进程（见 [selector.py:119-146](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/selector.py#L119-L146)），开销不小。把它推迟到「真正要 dispatch 到 tilecpp」的那一刻，并用 `@functools.cache` 缓存，可以保证「子进程最多跑一次」，且对不用 tilecpp的用户完全无开销。这段 dispatcher 的改写，正是该延迟策略的「兜底配套」——万一延迟探测发现 tilecpp 其实不可用，就在这里悄悄换成兜底后端，避免用户面对一个 tilecpp 启动失败。

「决策点 5a」——兜底后端查找 + `DISABLE_FALLBACK` + 警告去重：

[dispatcher.py:99-115](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/dispatcher.py#L99-L115) —— 兜底实现查找、降级开关与一次性警告：

```python
if name in _REGISTRY and fallback_backend in _REGISTRY[name]:
    if _is_fallback_disabled():                     # DISABLE_FALLBACK=1 → 拒绝降级
        raise NotImplementedError(
            f"Current backend '{current_backend}' has no implementation for '{name}'. "
            f"Fallback to '{fallback_backend}' is disabled (DISABLE_FALLBACK=1).")
    warning_key = f"{name}_{current_backend}_{fallback_backend}"
    if warning_key not in _LOGGED_WARNINGS:         # 去重：同组合只警告一次
        logger.warning(
            f"Current backend '{current_backend}' has no implementation for '{name}', "
            f"falling back to '{fallback_backend}' backend")
        _LOGGED_WARNINGS.add(warning_key)
    return _REGISTRY[name][fallback_backend](*args, **kwargs)
```

去重集合本身的声明：

[dispatcher.py:57](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/dispatcher.py#L57) —— 模块级集合，记录已警告过的降级组合：

```python
_LOGGED_WARNINGS = set()
```

`DISABLE_FALLBACK` 的读取——它是一个**每次调用都现读**的环境变量（不是 import 时读一次）：

[dispatcher.py:23-25](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/dispatcher.py#L23-L25) —— 现读环境变量，因此运行中用 `os.environ` 修改也能立即生效：

```python
def _is_fallback_disabled() -> bool:
    return os.environ.get("DISABLE_FALLBACK", "0") == "1"
```

「决策点 5b」——连兜底也没有时，调用 stub；这里也有一个 `DISABLE_FALLBACK` 拦截点：

[dispatcher.py:117-126](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/dispatcher.py#L117-L126) —— 第二处 `DISABLE_FALLBACK` 拦截点，随后调用 stub：

```python
if _is_fallback_disabled():
    raise NotImplementedError(
        f"No backend implementation found for '{name}' with backend '{current_backend}'. "
        f"Fallback to default implementation is disabled (DISABLE_FALLBACK=1).")
logger.warning(f"No backend implementation found for '{name}', using default implementation")
return default_impl(*args, **kwargs)        # default_impl 体 = raise NotImplementedError
```

> 两个拦截点的报错文本**不同**：5a 说的是「Fallback to '<兜底>' is disabled」，5b 说的是「Fallback to default implementation is disabled」。这是排查「为什么 DISABLE_FALLBACK=1 时报错」的线索——看文本能区分是「有兜底但被禁」还是「连兜底都没有」。

#### 4.4.4 代码实践

**实践目标**：观察「警告去重」与「DISABLE_FALLBACK 的两种报错文本」，加深对降级路径的把握。

**操作步骤**：

1. 阅读并（若本地有合适后端）运行下面这段脚本，它在循环里反复触发同一种降级，观察 warning 是否只出现一次：

   ```python
   # 示例代码：观察警告去重（需要 rms_norm 有 fallback 到 triton 的降级场景，待本地验证）
   import tilegym, torch
   from tilegym.backend import set_backend, get_available_backends_for_op

   # 前提：当前后端无 rms_norm 实现，但 triton 有（视本地环境，可能需要 set_backend 到某个无实现的后端）
   print("rms_norm 后端：", get_available_backends_for_op("rms_norm"))
   x = torch.randn(64, 128, device="cuda"); w = torch.ones(128, device="cuda")
   # for _ in range(100):
   #     tilegym.ops.rms_norm(x, 128, w, 1e-6)   # 若触发降级，warning 应只打印一次
   ```

2. 在另一个脚本里设置 `os.environ["DISABLE_FALLBACK"] = "1"`（在 `import tilegym` 之前或之后皆可，因为每次调用都现读），再触发降级，观察抛出的异常文本属于 5a 还是 5b。

**需要观察的现象**：

- 警告去重：即使循环 100 次，同一种降级 warning 只在 stderr 出现一次。
- `DISABLE_FALLBACK=1` 时：原本会降级的调用变成抛 `NotImplementedError`，且文本里明确写出 `disabled (DISABLE_FALLBACK=1)`。

**预期结果**：

- 去重生效：`_LOGGED_WARNINGS` 保证同三元组只警告一次。
- 报错文本：若当前后端有兜底（如 triton）但被禁，报 5a 文本；若连兜底都没有，报 5b 文本。
- 是否能复现取决于本地装了哪些后端，无法确定时记为「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `_is_fallback_disabled()` 每次 dispatch 都重新读环境变量，而不是在 `import` 时读一次缓存起来？

> **参考答案**：为了让 `DISABLE_FALLBACK` 可以**在运行中动态开关**。如果 import 时读一次就缓存，那么一个进程里就无法「先开着 fallback 跑一段、再关掉 fallback 做基准」。每次现读虽然多一次 `os.environ.get` 的开销（极小），换来了「随时生效」的灵活性，这对基准测试场景（见 u2-l1 练习）很实用。

**练习 2**：假设当前后端是 `tilecpp`，但运行环境没有 nvcc，算子 `rms_norm`（`fallback_backend="triton"`）在 triton 有实现。请描述一次 `tilegym.ops.rms_norm(...)` 调用的完整走向，以及用户能否观察到「使用了 triton」。

> **参考答案**：决策点 3 触发：`current_backend=="tilecpp"` 且 `is_tilecpp_available()` 为假（无 nvcc），于是 `current_backend` 被改写成 `fallback_backend="triton"`。随后决策点 4 查 `_REGISTRY["rms_norm"]["triton"]` 命中，直接调用 triton 实现**返回**——注意：因为已经把 current 改成了 triton 且 triton 实现存在，这里**不会**走决策点 5a，也**不会**打降级 warning（warning 只在「current 查不到、去查 fallback」时才打，而这里 current 已经是 triton 且命中了）。所以用户能正常拿到结果，但**观察不到任何提示**——这次「因 tilecpp 不可用而改用 triton」是静默发生的。

---

## 5. 综合实践：逐行追踪一次 tilegym.ops.softmax 调用

这是本讲指定的核心实践任务：**画出从 `ops.py` 的 stub 到 `cutile/softmax.py` 的 `@register_impl` 实现、再到 `ct.launch` 的完整调用链路**。请按下面的步骤，把前面 4 个模块的知识用起来。

### 5.1 任务说明

给定调用：

```python
import tilegym, torch
x = torch.randn(256, 2048, device="cuda")
y = tilegym.ops.softmax(x)            # use_tma 默认 False，未传 backend
```

在当前后端为默认 `cutile`、`DISABLE_FALLBACK` 未设、cutile 可用的前提下，追踪这一次调用。

### 5.2 完整调用链路图

下面是应当画出的调用链（箭头表示「调用 / 跳转」），每一步都标注了对应的源码位置：

```text
① tilegym.ops.softmax(x)
      │   ops.softmax 其实是 dispatch 工厂产出的 wrapper（闭包：name="softmax",
      │   fallback_backend="pytorch", default_impl=<ops.py 的 stub>）
      ▼
② dispatcher.wrapper(x)
      ├─ 2a  explicit_backend = kwargs.pop("backend", None)        → None        [dispatcher.py:76]
      ├─ 2b  current_backend = get_current_backend()               → "cutile"    [dispatcher.py:81]
      │        （读 selector._CURRENT_BACKENDS，见 selector.py:228-229）
      ├─ 2c  tilecpp 健康检查：current != "tilecpp"                  → 跳过        [dispatcher.py:91]
      ├─ 2d  查 _REGISTRY["softmax"]["cutile"]                      → 命中 ✅      [dispatcher.py:95]
      └─ 2e  调用 _REGISTRY["softmax"]["cutile"](x, use_tma=False)  [dispatcher.py:97]
            │   取出的就是 cutile/softmax.py 里被 @register_impl 装饰的 softmax
            ▼
③ cutile/softmax.py: softmax(x, use_tma=False, **kwargs)           [cutile/softmax.py:357]
      ├─ 3a  use_chunked = kwargs.get("use_chunked", False)         → False       [L376]
      ├─ 3b  use_multi_wave = kwargs.get("use_multi_wave", False)   → False       [L377]
      └─ 3c  return _Softmax.apply(x, False, False, False)          [L378]
            │   _Softmax 是 torch.autograd.Function 子类
            ▼
④ _Softmax.forward(ctx, x, use_tma=False, use_chunked=False, ...)  [cutile/softmax.py:314]
      ├─ 4a  y = torch.empty_like(x)                                [L338]
      ├─ 4b  三个开关都 False → 走 else 分支                          [L350-352]
      └─ 4c  _launch_softmax_kernel(x, y, TILE_SIZE=next_power_of_2(2048)=2048)
            ▼
⑤ _launch_softmax_kernel(input=x, output=y, TILE_SIZE=2048)        [cutile/softmax.py:173]
      ├─ 5a  input/output = .contiguous()                           [L186-187]
      ├─ 5b  NUM_SM = 设备的 SM 数；num_programs = min(NUM_SM*4, 256) [L189-190]
      ├─ 5c  grid = (num_programs, 1, 1)                             [L191]
      └─ 5d  ct.launch(stream, grid, _softmax_kernel, (y, x, 256, 2048, 2048))  [L193-204]
            │
            ▼
⑥ cuTile 运行时把 _softmax_kernel（@ct.kernel）编译并发射到 GPU    [cutile/softmax.py:18]
      └─ 真正的 softmax 数值计算在 GPU 上完成，结果写回 y
```

### 5.3 操作步骤

1. **读链路图**：对照 5.2 的每一步，打开对应源码行确认（链接见各括号标注），理解「这一步在做什么、为什么走到这里」。
2. **重点回答 3 个问题**（写在你的笔记里）：
   - (Q1) 第 ② 步的 wrapper 是怎么知道「我叫 softmax、兜底是 pytorch」的？（答：闭包，见 4.3）
   - (Q2) 第 ②b 步 `get_current_backend()` 返回 `"cutile"`，这个值从哪来？（答：`selector._CURRENT_BACKENDS`，默认 `"cutile"`，可被 `set_backend` 或环境变量 `CUTILE_TUTORIALS_BACKEND` 改写）
   - (Q3) 第 ②d 步查表命中后，为什么能直接把 `x, use_tma=False` 透传给 cuTile 实现？（答：因为 cuTile 实现的签名 `softmax(x, use_tma=False, **kwargs)` 与 stub 一致，这是「统一签名」的约束）
3. **动手验证**（可选运行）：用下面的脚本，把链路中「能从外部观测」的几个点打印出来，印证你的追踪：

   ```python
   # 示例代码：验证调用链中的可观测点
   import tilegym, torch
   from tilegym.backend import get_current_backend, get_registry_info

   print("① 当前后端 =", get_current_backend())                 # 应为 'cutile'，对应 ②b
   print("② softmax 注册的实现 =", get_registry_info()["softmax"])  # 应含 cutile 项，对应 ②d
   x = torch.randn(256, 2048, device="cuda")
   y = tilegym.ops.softmax(x)                                    # 走完整条 ①→⑥ 链
   ref = torch.softmax(x, dim=-1)
   print("③ 与 torch 参考的最大绝对误差 =", (y - ref).abs().max().item())
   ```

4. **画你自己的图**：合上本讲义，凭记忆把 5.2 的链路图重画一遍，标注每一步对应的源码文件与行号。能流畅画出来，就说明你已经掌握了 dispatcher 的完整运行机制。

### 5.4 需要观察的现象与预期结果

- 脚本应顺利跑完，`softmax` 的 `cutile` 实现出现在注册表里，且与 torch 参考的最大绝对误差在 fp32 下很小（与 u1-l3 的容差量级一致）。
- 这条链**没有**经过任何降级 warning——因为当前后端 `cutile` 直接命中（决策点 4），根本没走到 fallback。这也说明：**最常见、最快速的路径，恰恰是 dispatcher 里最短的那条**（2a→2b→2c→2d→2e，共 5 步）。
- 若你的环境 cutile 不可用，`softmax` 会因兜底 `pytorch` 也无实现而抛 `NotImplementedError`（决策点 5b），此时整条链在第 ② 步就终止——这正好反向印证了注册与门控的重要性。无法本地跑通时记为「待本地验证」。

### 5.5 验收标准

你能不看本讲义，独立完成：(1) 说出 dispatcher.wrapper 的 5 个决策点；(2) 解释 `_REGISTRY["softmax"]["cutile"]` 是在哪一步、由哪行代码写进去的；(3) 画出从 `tilegym.ops.softmax(x)` 到 `ct.launch` 的完整调用链并标注源码位置。

---

## 6. 本讲小结

- `_REGISTRY` 是一个**模块级可变全局**的嵌套字典 `{算子名: {后端: 实现}}`，初始为空，随导入而生长；`@dispatch` 还会自动把 stub 登记为 `"default"` 键，供自省使用（`get_available_backends_for_op` / `get_registry_info` / `print_registry_info`）。
- **注册是导入的副作用**：`register_impl` 是一个原样返回 `func` 的装饰器；它由「`ops/__init__.py` 条件导入 → `cutile/__init__.py` 聚合导入 → 各算子模块顶层执行 `@register_impl`」这条链触发。门控（`is_backend_available`）一关，整条链的注册都不发生。
- `dispatch` 是**装饰器工厂**，它返回的 `wrapper` 通过**闭包**记住 `name / fallback_backend / default_impl`，并用 `@functools.wraps` 保留 stub 的签名与文档——每个算子都有自己独立的 wrapper 对象。
- wrapper 的查找分 5 个决策点：取显式 `backend` → 定当前后端 → tilecpp 延迟健康检查 → 查当前后端（主路径）→ 查兜底/默认。最常见路径只有 5 步。
- fallback 有三处工程细节：tilecpp **延迟且缓存**的可用性探测（不可用则静默改写当前后端）、`_LOGGED_WARNINGS` 的**三元组去重**（同组合只警告一次）、`DISABLE_FALLBACK` 作用在**两个位置**且每次调用都现读环境变量。
- 一次 `tilegym.ops.softmax(x)` 调用，经过 `wrapper → _REGISTRY["softmax"]["cutile"] → _Softmax.apply → _launch_softmax_kernel → ct.launch` 完成计算；这条链是理解 TileGym「接口—分发—实现」三层架构的最好样本。

## 7. 下一步学习建议

本讲把 dispatcher 的内部机制与运行时路径讲透了，但有一个关键函数我们只用、没细讲：**`get_current_backend()` 返回的那个「当前后端」值，到底是怎么被探测和设置的？** 这正是下一讲 [u2-l3 后端选择与可用性 selector.py](u2-l3-backend-selector.md) 的主题——它会讲清 `_CURRENT_BACKENDS` 的初值、`set_backend` 的校验、各后端可用性的探测策略（`cuda.tile` 导入、nvcc 版本、cargo）、以及 `CUTILE_TUTORIALS_BACKEND` 等环境变量。

完成 U2 三讲后，你就拥有了 TileGym「调度层」的完整地图，接下来 [U3 cuTile 内核编程模型](u3-l1-cutile-kernel-basics.md) 会首次深入到「被调度的那个实现」内部，从 `@ct.kernel` 开始讲怎么写一个 GPU 内核。

> 延伸阅读：想立刻把本讲的「调用链」可视化出来，可在 REPL 里 `import tilegym; tilegym.print_registry_info()`（[dispatcher.py:175-189](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/dispatcher.py#L175-L189)）打印全量注册表，逐个核对每个算子的实现来源模块——这是验证你追踪的调用链是否正确的最直接方法。
