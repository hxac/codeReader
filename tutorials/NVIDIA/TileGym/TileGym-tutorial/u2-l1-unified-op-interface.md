# 统一算子接口 ops.py

> 本讲属于「U2 统一算子接口与后端调度」单元的第 1 讲，承接 [u1-l3 第一次调用 TileGym 算子](u1-l3-first-op-call.md)。
> 在 u1-l3 中你已经学会调用 `tilegym.ops.softmax(...)`，并知道真正的计算「在别处」完成。
> 本讲要回答的核心问题是：**那个「别处」是怎么被找到的？为什么 `ops.py` 里的函数体只会抛异常，却能把 softmax 真正算出来？**

## 1. 本讲目标

学完本讲，你应当能够：

- 看懂 `ops.py` 中任意一个算子的「定义形式」——它由 `@dispatch("算子名", ...)` 装饰器和一个「只抛 `NotImplementedError`」的函数体组成。
- 解释 `@dispatch` 的第一个参数（算子名）在全局注册表里扮演的「键」的角色。
- 区分「统一签名 / stub（占位实现）」与「后端真实实现」两层，并理解为什么要把它们分开。
- 说清 `fallback_backend`（兜底后端）的含义：当前后端没有实现时，按什么顺序去找替代实现。
- 理解调用时显式传入 `backend="..."` 参数会发生什么，以及它与全局 `set_backend` 的优先级关系。
- 能仅凭 `ops.py` 和分发器源码，**预测**一次调用会落到哪个实现上。

## 2. 前置知识

本讲默认你已经掌握 u1-l3 的内容。这里再用三句话回顾并补几个新术语：

- **统一入口 / 门面（facade）**：`tilegym.ops` 是对外的唯一门面，用户只跟它打交道，不直接接触某个具体后端。
- **stub（占位实现）**：一个「签名写好、但函数体只是 `raise NotImplementedError`」的函数。它的作用是**声明接口**（参数、返回值、文档字符串），而不是真正干活。
- **后端（backend）**：同一套算子的不同实现来源，TileGym 目前有 `cutile`（默认）、`triton`、`tilecpp`、`cutile-rs`。
- **注册表（registry）**：一个全局字典，记录「算子名 → {后端 → 真实实现函数}」。这是本讲最重要的数据结构。
- **当前后端（current backend）**：进程级的一个全局变量，默认是 `"cutile"`，可用 `set_backend(...)` 修改。

一句话直觉：`ops.py` 是一张「菜单」，每道菜（算子）只有一个名字和一份说明；厨房（后端实现）在别处，由「服务员」（分发器 `dispatch`）按你当前选的厨房去端菜。本讲只讲菜单和服务员怎么读菜单；厨房长什么样留到 U3 及以后。

## 3. 本讲源码地图

本讲涉及的关键文件及其职责：

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [src/tilegym/ops/ops.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/ops.py) | **统一算子接口定义**，全部算子的 stub 都在这里 | `@dispatch` 装饰、stub、`fallback_backend` |
| [src/tilegym/ops/\_\_init\_\_.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/__init__.py) | `ops` 包入口，把 `ops.py` 的名字重新导出 | `from .ops import *` 如何让 stub 成为公共 API |
| [src/tilegym/backend/dispatcher.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/dispatcher.py) | `@dispatch` / `register_impl` 的**真正实现**与全局注册表 | 注册表结构、查找顺序、fallback、`DISABLE_FALLBACK` |
| [src/tilegym/backend/\_\_init\_\_.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/__init__.py) | 把 `dispatch` / `register_impl` / `set_backend` 等对外暴露 | 名称如何从 backend 子包流出 |
| [src/tilegym/backend/selector.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/selector.py) | 当前后端变量 `_CURRENT_BACKENDS` 与 `get_current_backend` | 「当前后端」这个值从哪来 |
| [src/tilegym/ops/cutile/softmax.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py) | cuTile 版 softmax 的真实实现 | `@register_impl("softmax", backend="cutile")` 如何把实现塞进注册表 |

> 说明：`@dispatch` 装饰器**定义在** `backend/dispatcher.py`，但在 `ops/ops.py` 里被大量使用。本讲会在两份文件之间来回对照，这正是理解 TileGym 架构的关键。

---

## 4. 核心概念与源码讲解

本讲的 4 个最小模块依次回答：**用什么标记一个算子（4.1）→ 算子体为什么是空的（4.2）→ 找不到实现时怎么办（4.3）→ 用户如何临时指定后端（4.4）**。它们共同构成一次 `tilegym.ops.softmax(x)` 调用的完整决策链。

### 4.1 @dispatch 装饰器与算子名

#### 4.1.1 概念说明

`@dispatch` 是 TileGym 自定义的装饰器，它的核心作用是：**把一个「普通函数」改造为「会自动选择后端的分发函数」**，同时给这个函数在注册表里分配一个全局唯一的「键」——也就是**算子名（op name）**。

注意一个容易混淆的点：**算子名是一个字符串，不一定等于函数名**。只是 TileGym 的约定是让两者保持一致（例如算子名 `"softmax"` 对应函数 `def softmax(...)`），所以平时看起来像是同一个东西。但分发器内部只用那个字符串去注册表里查，函数名只是给 Python 看的。

为什么要把「名字」单独拎出来？

- 同一个算子名可以被**多个后端**各自注册一份实现（`cutile` 一份、`triton` 一份……）。名字是它们的「共同 ID」。
- 注册时（`register_impl`）和调用时（`dispatch`）都只认这个字符串，于是「定义接口的人」和「写后端实现的人」可以彻底解耦，互不依赖对方的代码。

#### 4.1.2 核心流程

用一段伪代码描述 `@dispatch(name, fallback_backend=...)` 装饰一个函数后，调用它时发生的事：

```text
用户调用 ops.softmax(x)
        │
        ▼
进入 dispatch 生成的 wrapper
        │
        ├─ 1. 取「当前后端」current = get_current_backend()   # 默认 "cutile"
        │     （若调用方传了 backend="xxx"，则改用 xxx，见 4.4）
        │
        ├─ 2. 在注册表里查 _REGISTRY["softmax"][current]
        │     ├─ 命中 → 直接调用该后端实现，返回           ✅ 最常见路径
        │     └─ 未命中 ↓
        │
        ├─ 3. 查兜底后端 _REGISTRY["softmax"][fallback_backend]
        │     ├─ 命中（且允许 fallback）→ 打一条警告，调用它，返回
        │     └─ 未命中 ↓
        │
        └─ 4. 调用「默认实现」default_impl
              └─ 而 default_impl 的函数体就是 raise NotImplementedError  ❌
```

关键结论：**第 4 步的「默认实现」就是 `ops.py` 里那个只会抛异常的函数本身**。只有当前端和兜底端都找不到实现时，才会真正执行它——于是你就看到了 `NotImplementedError`。

#### 4.1.3 源码精读

先看 `@dispatch` 的定义，它位于 `backend/dispatcher.py`：

[dispatcher.py:60-70](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/dispatcher.py#L60-L70) —— `dispatch` 的签名，第一个参数 `name` 就是算子名（注册表键），第二个 `fallback_backend` 默认是 `"pytorch"`：

```python
def dispatch(name: str, fallback_backend: str = "pytorch"):
    """
    Create a dispatcher that selects the correct implementation based on current backend
    ...
    """
```

> 注意默认兜底是 `"pytorch"`。但 `ops.py` 里很多算子（如 `softmax`、`fmha`、`matmul`）并没有显式写 `fallback_backend`，于是它们的兜底就是 `"pytorch"`；而 TileGym 并不为这些算子注册 pytorch 实现，所以一旦 cutile 不可用，它们会直接走到第 4 步抛异常。这正是 `fallback_backend` 取值很重要的原因（见 4.3）。

再看 `ops.py` 里一个最朴素的例子——`softmax`，它没有指定 `fallback_backend`：

[ops.py:225-232](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/ops.py#L225-L232) —— `@dispatch("softmax")` 装饰器 + 函数签名：

```python
@dispatch(
    "softmax",
)
def softmax(
    x: torch.Tensor,
    use_tma: bool = False,
    **kwargs: Any,
):
```

而真正干活的 cuTile 实现写在另一个文件里，用 **`@register_impl`** 把自己挂到同一个算子名 `"softmax"` 下：

[cutile/softmax.py:356-361](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py#L356-L361) —— cuTile 后端把实现注册到算子名 `"softmax"`：

```python
@register_impl("softmax", backend="cutile")
def softmax(
    x,
    use_tma=False,
    **kwargs,
):
```

`register_impl` 的实现就是把 `{算子名: {后端: 函数}}` 写进全局字典：

[dispatcher.py:46-52](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/dispatcher.py#L46-L52) —— 往注册表里塞一项：

```python
def decorator(func):
    if name not in _REGISTRY:
        _REGISTRY[name] = {}
    _REGISTRY[name][backend] = func
    ...
    return func
```

于是注册表的结构就是（见 [dispatcher.py:30-31](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/dispatcher.py#L30-L31)）：

```text
_REGISTRY = {
    "softmax": {
        "default": <ops.py 里的 stub>,     # 由 @dispatch 自动登记
        "cutile": <cutile/softmax.py 里的实现>,
    },
    "rms_norm": { "default": ..., "cutile": ..., "triton": ... },
    ...
}
```

> 重要细节：`@dispatch` 在装饰时会自动把 stub 登记到 `_REGISTRY[name]["default"]`（见 [dispatcher.py:128-132](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/dispatcher.py#L128-L132)）。但分发时的兜底链路**并不去查 `"default"` 这个键**，而是直接调用闭包里的 `default_impl`（见 4.1.2 第 4 步）。`"default"` 键主要给 `get_available_backends_for_op` / `get_registry_info` 这类自省工具用。

#### 4.1.4 代码实践

**实践目标**：亲眼看到「算子名」作为键把 stub 和后端实现连起来。

**操作步骤**：

1. 在已安装 TileGym（且 cutile 可用）的环境中，运行下面这段「源码阅读型 + 可运行」脚本：

   ```python
   # 示例代码：观察注册表，理解算子名如何把 stub 与实现关联
   import tilegym                       # 触发后端初始化与 cutile 实现的导入注册
   from tilegym.backend import get_registry_info, get_available_backends_for_op

   # softmax 在 ops.py 里的算子名就是 "softmax"
   print("softmax 的可用后端：", get_available_backends_for_op("softmax"))
   info = get_registry_info()["softmax"]
   for backend, impl in info.items():
       print(f"  {backend:8s} -> {impl}")
   ```

2. 对照 `ops.py` 中 `@dispatch("softmax")` 的算子名，确认它和 `register_impl("softmax", ...)` 用的是同一个字符串。

**需要观察的现象**：

- 输出里应同时出现 `"default"`（指向 `tilegym.ops.ops.softmax`）和 `"cutile"`（指向 `tilegym.ops.cutile.softmax.softmax`）。这正是「同一算子名、多个实现」的直观体现。

**预期结果**：

- `get_available_backends_for_op("softmax")` 返回类似 `['default', 'cutile']` 的列表。
- 若你的环境还装了 triton/tilecpp 且它们也为 softmax 注册了实现，列表里会更多。

> 若运行环境没有 GPU 或 cutile 不可用，`cutile` 这一项不会出现，只能看到 `['default']`——这恰好说明：**注册是「按需」发生的**，只有对应后端被成功 import 才会登记（详见 `ops/__init__.py` 的条件导入）。这种情况记为「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：`ops.py` 里 `fmha` 算子的算子名是什么？它的兜底后端是哪个？

> **参考答案**：算子名是字符串 `"fmha"`（见 [ops.py:313-315](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/ops.py#L313-L315)）。它没有显式写 `fallback_backend`，因此兜底是 `dispatch` 的默认值 `"pytorch"`。

**练习 2**：如果有人把 `@register_impl("softmax", backend="cutile")` 里的 `"softmax"` 误写成 `"soft_max"`，调用 `tilegym.ops.softmax(x)` 会怎样？

> **参考答案**：注册表里会多出一个无关的键 `"soft_max"`，而 `"softmax"` 下只剩 `"default"`。于是调用走兜底链路：当前后端 `cutile` 查不到 → 兜底 `pytorch` 也查不到 → 调用 stub 本体 → 抛出 `NotImplementedError: softmax is not implemented for cutile`。可见**算子名必须两侧严格一致**。

---

### 4.2 统一签名与 NotImplementedError stub

#### 4.2.1 概念说明

打开 `ops.py`，你会发现**每一个**算子的函数体都长得几乎一样——只有一行 `raise NotImplementedError(...)`。这不是没写完，而是**有意为之**的设计，叫做 **stub（占位实现）**。

这种设计要解决的问题是「接口与实现分离」：

- **统一签名**：`ops.py` 给每个算子规定了「对所有后端都适用」的参数列表、类型注解和文档字符串。无论你用 cutile 还是 triton，调用方式完全一样。
- **stub 作为兜底默认实现**：它本身不计算任何东西，只在「找不到任何后端实现」时被执行，给出一条**清晰、统一**的错误信息，而不是让用户面对某个后端内部莫名的 `KeyError`。
- **文档即合同**：`help(tilegym.ops.softmax)` 显示的就是 stub 的 docstring（这得益于 wrapper 上的 `@functools.wraps`，见 [dispatcher.py:73](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/dispatcher.py#L73)）。用户读到的「契约」只有一份。

换句话说，`ops.py` 是一份**纯接口规范**，它故意「什么都不做」，好让真正的实现可以自由地放在各后端目录里。

#### 4.2.2 核心流程

stub 的生命周期：

```text
@dispatch("X") 装饰 def X(...):
    1. X 的签名/注解/docstring 被保留（@functools.wraps）
    2. X 被登记为 _REGISTRY["X"]["default"]
    3. 对外暴露的 X 被 wrapper 替换 → 真正被调用的是 wrapper，不是这个函数体

只有当 wrapper 在注册表里「当前后端」和「兜底后端」都查不到实现时，
才会回退来执行这个函数体 → 抛出 NotImplementedError
```

#### 4.2.3 源码精读

以 `rms_norm` 为例，注意它的函数体只有一行抛错：

[ops.py:134-159](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/ops.py#L134-L159) —— 完整的「装饰器 + 统一签名 + stub」三件套：

```python
@dispatch(
    "rms_norm",
    fallback_backend="triton",
)
def rms_norm(
    input: torch.Tensor,
    normalized_shape: Any,
    weight: torch.Tensor,
    eps: float,
    bias: Optional[torch.Tensor] = None,
    mode: Optional[str] = None,
    **kwargs: Any,
):
    """Returns the Root-Mean-Squared Norm of input along dimension N. ..."""
    raise NotImplementedError(f"rms_norm is not implemented for {get_current_backend()}")
```

这里有三个值得注意的点：

1. **签名是统一的合同**：`input / normalized_shape / weight / eps / bias / mode / **kwargs` 这套形参，对所有后端都成立。后端实现可以忽略自己不关心的参数，但不能要求用户换一种调用方式。
2. **错误信息带上了当前后端**：`f"... is not implemented for {get_current_backend()}"`，调用的是 [selector.py:228-229](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/selector.py#L228-L229) 的 `get_current_backend()`。于是报错会告诉你「是在哪个后端上缺失」，非常便于排查。
3. **`**kwargs` 是后端逃生口**：统一签名里普遍带一个 `**kwargs: Any`，注释写着 "Additional arguments for backend-specific configurations"。它的作用是：在不破坏统一签名的前提下，让某个后端可以接收自己专属的参数（例如 `use_tma`、`kernel_configs`、`use_chunked` 等）。

再看一个**反例**：并不是 `ops.py` 里所有函数都走 dispatch。`get_fused_swiglu_module` 就**没有** `@dispatch` 装饰，注释明确说明原因：

[ops.py:113-131](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/ops.py#L113-L131) —— 一个故意不参与分发的「普通」函数：

```python
def get_fused_swiglu_module():
    """
    ...
    Note: This doesn't need backend dispatch - the PartiallyFusedSwiGLUMLP class automatically
    dispatches to the correct backend kernel internally.
    """
    from tilegym.ops.fused_mlp import PartiallyFusedSwiGLUMLP
    return PartiallyFusedSwiGLUMLP
```

这个对比很有教学意义：**dispatch 是一种选择，不是强制**。当某个「工厂函数」返回的类会在内部自己处理分发时，外层就没必要再套一层 `@dispatch`。这也提醒我们：判断一个算子是否走注册表，**看它有没有 `@dispatch` 装饰器，而不是看它在不在 `ops.py` 里**。

#### 4.2.4 代码实践

**实践目标**：验证「stub 体确实只在无实现时执行」，并读懂它的报错。

**操作步骤**：

1. 阅读测试 [tests/ops/test_softmax.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/ops/test_softmax.py) 的参数化方式，确认它的调用形参与 `ops.py` 中 `softmax(x, use_tma=False, **kwargs)` 完全一致——这就是「统一签名」的现实约束。
2. （可选运行）写一段脚本，强制让分发走投无路，触发 stub：

   ```python
   # 示例代码：人为制造「无实现」场景，观察 stub 的报错（待本地验证）
   import os
   os.environ["DISABLE_FALLBACK"] = "1"        # 关掉兜底，详见 4.3
   import tilegym
   from tilegym.backend import set_backend, get_available_backends
   print("可用后端：", get_available_backends())
   # 选一个【没有】softmax 实现的后端做当前后端（视本地环境而定，例如 "triton" 若它没注册 softmax）
   # tilegym.ops.softmax(torch.randn(4, 8, device="cuda"))   # 预期抛 NotImplementedError
   ```

**需要观察的现象**：

- 触发的异常文本形如 `NotImplementedError: softmax is not implemented for <某后端>`，且文本里的后端名正是 `get_current_backend()` 的返回值。

**预期结果**：

- 这条信息由 stub 体（[ops.py:244](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/ops.py#L244)）产生，证明「平时根本不会执行到的函数体，在找不到实现时确实会被执行」。若你的环境中每个可用后端都注册了 softmax，难以复现该异常，请记为「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 stub 的错误信息要用 `get_current_backend()` 动态拼接，而不是写死成 `"cutile"`？

> **参考答案**：因为当前后端是进程级可变状态（可被 `set_backend` 或环境变量 `CUTILE_TUTORIALS_BACKEND` 修改）。写死后端名会让报错在切换后端后产生误导；动态拼接能准确告诉用户「是在哪个后端上缺失实现」。

**练习 2**：`ops.py` 里几乎所有算子都带 `**kwargs`，但这会削弱类型检查。请从「多后端共存」的角度说明它为什么是必要的妥协。

> **参考答案**：不同后端有各自的调优旋钮（TMA 开关、tile 大小、`kernel_configs` 字典等）。如果把这些参数全部写进统一签名，则每新增一个后端或一个旋钮都要改公共接口，破坏稳定性。用 `**kwargs` 承载「后端专属参数」，可以保持统一签名长期不变，代价是牺牲一部分静态类型检查——由 docstring 里的 `**kwargs` 说明来弥补。

---

### 4.3 fallback_backend 概念

#### 4.3.1 概念说明

`fallback_backend`（兜底后端）回答一个问题：**当「当前后端」没有某个算子的实现时，要不要自动换一个后端顶上？换哪个？**

它的取值就写在 `@dispatch` 里，是**每个算子各自定义**的属性：

- 写 `fallback_backend="triton"`：当前端缺失时，去查 triton 有没有实现，有就用它（并打一条警告）。
- 不写（默认 `"pytorch"`）：当前端缺失时，去查 pytorch 有没有实现。

为什么要设计「兜底」而不是「缺失即报错」？因为 TileGym 的各后端覆盖面不同：

- `cutile` 覆盖最广，是默认主力。
- `triton` 作为「稳定的兜底主力」，为一批算子（RoPE、rms_norm、dropout、layer_norm 等）提供了实现。所以这些算子的 `fallback_backend` 被显式设成 `"triton"`——即便用户当前后端因为某些原因没有实现，也能平滑降级到 triton。
- 而像 `softmax`/`fmha`/`matmul` 这类高度优化的算子，没有等价的 triton 兜底，于是保留默认 `"pytorch"`，实际上意味着「没有兜底，缺失就报错」。

一句话：`fallback_backend` 体现了 TileGym **「优先用当前后端、必要时优雅降级」**的策略。

#### 4.3.2 核心流程

分发器查找实现的「三级火箭」可以形式化描述。设当前后端为 \(c\)、兜底后端为 \(f\)、算子名为 \(n\)，注册表为 \(R\)：

\[
\text{impl} =
\begin{cases}
R[n][c] & \text{若 } c \in R[n] \quad \text{(命中当前后端)} \\
R[n][f] & \text{若 } c \notin R[n] \,\land\, f \in R[n] \,\land\, \neg\text{DISABLE\_FALLBACK} \quad \text{(降级到兜底)} \\
\text{default\_impl} & \text{否则} \quad \text{(执行 stub，抛 NotImplementedError)}
\end{cases}
\]

对应的源码决策树（注意第二、三级都受环境变量 `DISABLE_FALLBACK` 影响）：

```text
查 _REGISTRY[n][c]  ──命中──► 调用，返回
        │未命中
        ▼
查 _REGISTRY[n][f]  ──命中──► DISABLE_FALLBACK=1 ?
        │                       ├ 是 → raise NotImplementedError（拒绝降级）
        │未命中                 └ 否 → 打 warning，调用兜底实现，返回
        ▼
调用 default_impl（stub）
        └─ DISABLE_FALLBACK=1 ? 是 → raise；否 → 由 stub 自己 raise NotImplementedError
```

#### 4.3.3 源码精读

`ops.py` 中带 `fallback_backend="triton"` 的算子一共 6 处（可用搜索确认）：`get_apply_rope_func`、`apply_rope_base`、`rms_norm`、`dropout`、`layer_norm_legacy`、`persistent_layer_norm`。下面看其中两个代表性声明：

[ops.py:27-30](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/ops.py#L27-L30) —— 带 `fallback_backend="triton"` 的 `get_apply_rope_func`：

```python
@dispatch(
    "get_apply_rope_func",
    fallback_backend="triton",
)
```

[ops.py:44-47](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/ops.py#L44-L47) —— 同样兜底到 triton 的 `apply_rope_base`：

```python
@dispatch(
    "apply_rope_base",
    fallback_backend="triton",
)
```

兜底逻辑的真正实现在分发器的 wrapper 里，分两段读：

[dispatcher.py:99-115](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/dispatcher.py#L99-L115) —— 第二级：尝试兜底后端，处理 `DISABLE_FALLBACK` 与「只警告一次」：

```python
# Try implementation from fallback backend
if name in _REGISTRY and fallback_backend in _REGISTRY[name]:
    if _is_fallback_disabled():           # DISABLE_FALLBACK=1
        raise NotImplementedError(
            f"Current backend '{current_backend}' has no implementation for '{name}'. "
            f"Fallback to '{fallback_backend}' is disabled (DISABLE_FALLBACK=1).")
    warning_key = f"{name}_{current_backend}_{fallback_backend}"
    if warning_key not in _LOGGED_WARNINGS:
        logger.warning(f"Current backend '{current_backend}' has no implementation for '{name}', "
                       f"falling back to '{fallback_backend}' backend")
        _LOGGED_WARNINGS.add(warning_key)     # 同一组合只警告一次
    return _REGISTRY[name][fallback_backend](*args, **kwargs)
```

[dispatcher.py:117-126](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/dispatcher.py#L117-L126) —— 第三级：连兜底也没有时，调用 stub（即 `default_impl`）：

```python
# Use default implementation
if _is_fallback_disabled():
    raise NotImplementedError(...)
logger.warning(f"No backend implementation found for '{name}', using default implementation")
return default_impl(*args, **kwargs)   # default_impl 的函数体 = raise NotImplementedError
```

两个细节值得记住：

- **`DISABLE_FALLBACK`**（环境变量，见 [dispatcher.py:23-25](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/dispatcher.py#L23-L25)）：把它设为 `1` 会**关闭一切自动降级**，把「悄悄换后端」变成「直接报错」。这在性能基准或回归测试里非常有用——你绝不希望一个本该跑 cutile 的 kernel 偷偷跑成了 triton 却没人发现。
- **警告去重**（`_LOGGED_WARNINGS`，见 [dispatcher.py:57](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/dispatcher.py#L57)）：同一种「算子+当前后端+兜底后端」组合只警告一次，避免在循环里刷屏。

> 还有一个**特殊兜底**：即便用户把当前后端设成了 `tilecpp`，但运行时探测到 `tilecpp` 实际不可用（nvcc 版本不够），wrapper 会**先把当前后端改成 `fallback_backend`** 再继续查，见 [dispatcher.py:91-92](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/dispatcher.py#L91-L92)。这是「tilecpp 可用性探测被延迟到首次 dispatch」这一设计的配套措施（详见 u2-l3）。

#### 4.3.4 代码实践（本讲指定的实践任务）

**实践目标**：选一个带 `fallback_backend="triton"` 的算子，**讲清并验证**「当前后端无实现、且不显式指定 backend」时会发生什么。

**操作步骤**：

1. 在 `ops.py` 中任选一个带 `fallback_backend="triton"` 的算子，推荐 `rms_norm`（[ops.py:134-137](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/ops.py#L134-L137)）。
2. 用下面的推理模板，**预测**三种环境下调用 `tilegym.ops.rms_norm(...)` 的结果（先不运行，对照 4.3.2 的决策树写下来）：
   - (a) 当前后端 = `cutile`，且 cutile 注册了 rms_norm；
   - (b) 当前后端 = `tilecpp`（假设它没注册 rms_norm），triton 注册了 rms_norm，未设 `DISABLE_FALLBACK`；
   - (c) 同 (b)，但 `DISABLE_FALLBACK=1`。
3. （可选运行验证）写一段脚本，打印 `get_available_backends_for_op("rms_norm")`，并尝试用 `set_backend` 切到一个没有 rms_norm 实现的后端来复现 (b)/(c)。是否可复现取决于本地装了哪些后端，**无法确定运行结果时记为「待本地验证」**。

**需要观察的现象与预期结果**：

- (a) 命中第一级，直接调用 cutile 实现，**无警告**。
- (b) 命中第二级，**打印一条 warning**（形如 `Current backend 'tilecpp' has no implementation for 'rms_norm', falling back to 'triton' backend`），然后调用 triton 实现正常返回。
- (c) 第二级被 `DISABLE_FALLBACK=1` 拦下，**直接抛** `NotImplementedError: Current backend 'tilecpp' has no implementation for 'rms_norm'. Fallback to 'triton' is disabled (DISABLE_FALLBACK=1).`。

**一句话回答实践任务**：当当前后端无实现、且不显式指定 `backend` 时，分发器会**先尝试 `fallback_backend`**（对 `rms_norm` 就是 triton）；若 triton 有实现则**降级使用并打一条一次性警告**；若连 triton 也没有（或 `DISABLE_FALLBACK=1`），则**回退到 stub 抛出 `NotImplementedError`**。

#### 4.3.5 小练习与答案

**练习 1**：`softmax` 的 `fallback_backend` 是什么？如果 cutile 不可用，调用 softmax 会成功降级吗？

> **参考答案**：`softmax` 没有显式指定 `fallback_backend`，故取默认 `"pytorch"`（见 [ops.py:225-227](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/ops.py#L225-L227) 与 [dispatcher.py:60](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/dispatcher.py#L60)）。由于 TileGym 并未为 softmax 注册 pytorch 实现，cutile 不可用时**不会成功降级**，而是走第三级，由 stub 抛出 `NotImplementedError`。

**练习 2**：为什么基准测试场景下推荐设置 `DISABLE_FALLBACK=1`？

> **参考答案**：基准测试要求「明确知道跑的是哪个后端的实现」。若放任 fallback，某个 cutile 内核可能因为临时缺失被悄悄换成 triton，导致测得的性能数据名不副实。`DISABLE_FALLBACK=1` 把「静默降级」变成「显式报错」，保证测到的就是目标后端。

---

### 4.4 显式 backend 参数

#### 4.4.1 概念说明

除了用 `set_backend(...)` 修改**进程级**的当前后端，TileGym 还提供了一种**调用级**的临时切换方式：在调用算子时传入关键字参数 `backend="xxx"`。

它的意义在于：

- **不污染全局状态**：`set_backend` 是全局的，改了之后所有后续调用都受影响；而 `backend=` 只对这一次调用生效，调用结束即恢复。
- **便于对比/A-B 测试**：你可以在同一个脚本里，用 `backend="cutile"` 和 `backend="triton"` 分别调用同一个算子，直接对比结果与性能，而不用反复 `set_backend`。
- **优先级最高**：显式 `backend=` 会**覆盖** `get_current_backend()` 的返回值——它是「一次性最高优先级指令」。

注意：`backend` 并**不在** stub 的形参列表里（你看 `softmax(x, use_tma=False, **kwargs)` 里没有 `backend`），它是被 `**kwargs` 吃进去、再由 wrapper 用 `kwargs.pop("backend", None)` 拦截的。所以它对所有算子都通用，且不会进入真正的后端实现。

#### 4.4.2 核心流程

```text
调用 ops.softmax(x, backend="triton")
        │
        ▼
wrapper 执行：
   explicit_backend = kwargs.pop("backend", None)   # = "triton"
   if explicit_backend is not None:
       current_backend = explicit_backend            # 直接用，忽略 get_current_backend()
   ...
   此后流程与 4.1.2 完全一致（查当前 → 查兜底 → stub）
```

关键点：`kwargs.pop` 一方面取出 `backend`，另一方面把它**从 kwargs 里删掉**，所以后续 `_REGISTRY[...][current_backend](*args, **kwargs)` 转发给真实实现时，`kwargs` 里已经没有 `backend` 这一项了——后端实现函数根本看不到它。

#### 4.4.3 源码精读

显式后端的拦截逻辑在 wrapper 开头：

[dispatcher.py:74-81](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/dispatcher.py#L74-L81) —— 从 kwargs 里取出并消费 `backend`：

```python
def wrapper(*args, **kwargs):
    # Check if backend is explicitly specified in kwargs
    explicit_backend = kwargs.pop("backend", None)

    if explicit_backend is not None:
        current_backend = explicit_backend
    else:
        current_backend = get_current_backend()
```

随后第 91 行起的查找链（4.3.3 已详解）用的就是这个 `current_backend`。也就是说：

- 不传 `backend` → 用进程级当前后端（`set_backend` / `CUTILE_TUTORIALS_BACKEND` / 默认 `cutile`）。
- 传 `backend="triton"` → 这一次强制用 triton，进程级状态不动。

> 注意：显式 `backend="tilecpp"` 仍然会触发 4.3.3 提到的「tilecpp 实际不可用则降级」逻辑（[dispatcher.py:91-92](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/dispatcher.py#L91-L92)），因为它修改的是 `current_backend`，而那段判断看的就是 `current_backend`。换言之，「显式指定」只是跳过 `get_current_backend()`，并不跳过 tilecpp 的健康检查。

#### 4.4.4 代码实践

**实践目标**：在同一脚本里用显式 `backend=` 做一次 A/B 对比，验证它不影响全局状态。

**操作步骤**：

1. 阅读并运行下面这段「源码阅读型 + 可运行」脚本（需要至少两个后端可用；若只有一个后端，则把它当作「观察优先级」的演示）：

   ```python
   # 示例代码：显式 backend 参数 vs 全局 set_backend
   import torch, tilegym
   from tilegym.backend import get_current_backend, get_available_backends_for_op

   print("进程当前后端：", get_current_backend())
   print("rms_norm 可用后端：", get_available_backends_for_op("rms_norm"))

   x = torch.randn(64, 128, device="cuda")
   w = torch.ones(128, device="cuda")

   # 假设 cutile 与 triton 都有 rms_norm 实现
   # y_a = tilegym.ops.rms_norm(x, 128, w, 1e-6, backend="cutile")   # 一次性用 cutile
   # y_b = tilegym.ops.rms_norm(x, 128, w, 1e-6, backend="triton")   # 同一脚本里再换 triton
   # print("调用后进程当前后端仍为：", get_current_backend())         # 应保持不变
   ```

2. 重点体会：两次调用用了不同后端，但 `get_current_backend()` 在调用前后**不变**。

**需要观察的现象**：

- 显式指定 `backend=` 后，调用走的就是那个后端的实现；调用结束后，进程级当前后端没有被改写。
- 若指定的 `backend` 没有该算子实现，会进入兜底/stub 链路（与 4.3 完全一致）。

**预期结果**：

- 调用前后 `get_current_backend()` 输出相同（例如都是 `cutile`），证明 `backend=` 是「调用级临时覆盖」。
- 具体哪些后端可用取决于本地环境，无法确定时记为「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：如果用户既调用了 `set_backend("triton")`，又在调用时传了 `backend="cutile"`，最终用哪个后端？

> **参考答案**：用 `cutile`。显式 `backend=` 的优先级高于 `get_current_backend()`（[dispatcher.py:78-79](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/dispatcher.py#L78-L79)）。但这次调用结束后，进程级当前后端仍是 `triton`——`backend=` 不改全局状态。

**练习 2**：为什么 `backend` 不写在 stub 的形参里（比如 `def softmax(x, use_tma=False, backend=None, **kwargs)`），而是靠 `**kwargs` + `kwargs.pop`？

> **参考答案**：把 `backend` 写进每个 stub 的签名会造成几十处重复且容易遗漏；而且 `backend` 是「分发层」的元参数，不属于算子本身的语义。用 `**kwargs` 统一承接、由 wrapper 集中 `pop`，既保证所有算子都支持 `backend=`，又让真实后端实现完全感知不到这个参数（它已被移除），职责更清晰。

---

## 5. 综合实践

把本讲 4 个模块串起来，完成下面这个「读菜单 + 预测 + 验证」的综合任务：

**任务**：任选 `ops.py` 中的 3 个算子（建议至少包含一个 `fallback_backend="triton"` 的，如 `rms_norm`；一个不写 fallback 的，如 `softmax`；一个注意力度相关的，如 `fmha`），为每个算子填写下面这张「接口卡片」，并用代码验证你的判断。

| 算子名 | 统一签名（关键形参） | fallback_backend | 是否带 `**kwargs` | get_available_backends_for_op 的实际返回 |
| --- | --- | --- | --- | --- |
| rms_norm | input, normalized_shape, weight, eps, bias, mode | `"triton"` | 是 | （待本地验证） |
| softmax | x, use_tma | `"pytorch"`（默认） | 是 | （待本地验证） |
| fmha | q, k, v, scaling, is_causal | `"pytorch"`（默认） | 是 | （待本地验证） |

**步骤**：

1. **读菜单**：打开 [ops.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/ops.py)，定位这 3 个算子的 `@dispatch(...)` 与签名，填前 4 列。
2. **预测**：对每个算子，假设「当前后端无实现、未设 DISABLE_FALLBACK、不传 backend」，用 4.3.2 的决策树预测结果（降级到哪个后端？还是抛异常？）。
3. **验证**：运行下述脚本，把 `get_available_backends_for_op` 的真实返回填入第 5 列，并对照你的预测。

   ```python
   # 示例代码：批量打印若干算子的可用后端
   import tilegym
   from tilegym.backend import get_available_backends_for_op, get_registry_info

   for op in ["rms_norm", "softmax", "fmha"]:
       print(op, "->", get_available_backends_for_op(op))
   # 想看实现分别定义在哪个模块，可打印 get_registry_info()[op]
   ```

4. **反思**：若某个算子的第 5 列里**没有** `cutile`，请结合 `ops/__init__.py` 的条件导入（[ops/\_\_init\_\_.py:15-24](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/__init__.py#L15-L24)）解释原因——通常是当前环境里 `is_backend_available("cutile")` 为假，导致 `from . import cutile` 这一步没发生，自然没有任何 `@register_impl(..., backend="cutile")` 被执行。

**验收标准**：你能不看源码，仅凭「算子名 + fallback_backend + 注册表状态」准确说出一次调用会落到哪个实现或抛什么错。

## 6. 本讲小结

- `ops.py` 是一份**纯接口规范**：每个算子由 `@dispatch("算子名", fallback_backend=...)` 装饰，函数体只是一个抛 `NotImplementedError` 的 **stub**。
- **算子名**（字符串）是全局注册表 `_REGISTRY` 的键，把 `ops.py` 的 stub 和各后端用 `@register_impl` 登记的实现关联起来；`@dispatch` 还会自动把 stub 登记为 `"default"`。
- 分发器按**三级火箭**查找：当前后端 → `fallback_backend`（受 `DISABLE_FALLBACK` 控制，且只警告一次）→ stub（抛 `NotImplementedError`）。
- `fallback_backend` 是**每个算子各自**的属性：RoPE/rms_norm/dropout/layer_norm 等显式设为 `"triton"` 以便降级；softmax/fmha/matmul 等保留默认 `"pytorch"`，实际等于「无降级、缺失即报错」。
- 显式 `backend="..."` 关键字参数由 wrapper 通过 `kwargs.pop` 拦截，提供**调用级、最高优先级**的后端覆盖，且不改变进程级当前后端；它被消费后不会传给真实实现。
- 判断一个 `ops.py` 函数是否走分发，**看它有没有 `@dispatch`**（反例：`get_fused_swiglu_module` 故意不分发）。

## 7. 下一步学习建议

本讲只讲了「菜单」和「服务员读菜单的规则」，但还没讲「服务员本人」是怎么实现的。下一讲 [u2-l2 后端注册表与分发机制 dispatcher.py](u2-l2-backend-dispatcher.md) 会深入 `dispatcher.py`，把 `_REGISTRY` 的写入（`register_impl`）、读取（`dispatch` wrapper）和注册时机讲透，并完整追踪一次 `tilegym.ops.softmax` 从 stub 到 `cutile/softmax.py` 的调用链。之后再进入 [u2-l3 后端选择与可用性 selector.py](u2-l3-backend-selector.md)，理解「当前后端」这个值是如何被探测和设置的。

> 延伸阅读：想立刻看到全量注册表，可在 REPL 里调用 `tilegym.backend.print_registry_info()`（[dispatcher.py:175-189](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/dispatcher.py#L175-L189)），它会打印每个算子名下所有后端实现的来源模块——这是验证本讲内容最直观的工具。
