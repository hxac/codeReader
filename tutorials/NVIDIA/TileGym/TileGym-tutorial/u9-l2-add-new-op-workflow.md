# 贡献新算子的完整工作流

## 1. 本讲目标

学完本讲，你应该能够：

- 说清「给 TileGym 新增一个 cuTile 算子」需要在哪几个文件、改哪几个地方。
- 独立在 `ops.py` 写一个带 `@dispatch` 的接口 stub（统一签名）。
- 独立在 `ops/cutile/` 写一个内核实现，用 `@register_impl` 把它挂进全局注册表，并补上 `cutile/__init__.py` 的导出。
- 照着 `tests/ops/test_softmax.py` 与 `tests/benchmark/bench_softmax.py` 的骨架，为你的新算子配上功能测试和性能基准。
- 把「接口 stub → 后端实现 + 注册 → 测试 → 基准」这条链路在自己脑子里跑通，知道哪一步漏了会发生什么。

本讲是一篇「工程流程」讲义：它不教你写复杂内核（那是 U3–U6 的任务），而是把前几讲零散提到的「统一接口—分发—注册」三件套收口成一份可照抄的清单。

## 2. 前置知识

本讲默认你已经掌握以下认知（它们由前置讲义建立，这里只做一句话回顾，不重复展开）：

- **统一接口与 stub**（u2-l1）：`ops.py` 里每个算子都是用 `@dispatch("算子名")` 装饰的「统一签名 stub」，函数体只 `raise NotImplementedError`，真正的计算由分发器在运行时路由到后端实现。判断一个函数走不走分发，看它有没有 `@dispatch` 装饰器。
- **注册表与分发**（u2-l2）：全局嵌套字典 `_REGISTRY` 形如 `{算子名: {后端: 实现函数}}`；后端实现用 `@register_impl("算子名", backend="后端")` 挂进去；一次调用 `tilegym.ops.softmax(x)` 实际走的是 `wrapper → 查 _REGISTRY → 后端实现 → 内核 → ct.launch`。
- **测试与基准框架**（u9-l1）：功能测试继承 `common.PyTestCase`，用 `assertCorrectness` 把「被测内核」与 `reference`（PyTorch 参考实现）按 dtype 自动推断容差做比较；基准用 `triton.testing` 驱动，默认走 CUPTI 测纯内核时间。

如果你对上面任何一条还不确定，建议先回看对应讲义再继续，否则本讲的「流程」会变成无根之木。

另外，本讲反复用到几个 cuTile 编程术语（来自 U3）：`@ct.kernel` 装饰器把 Python 函数交给运行时编译器 `tileiras` 编译成 GPU 代码；`ConstInt`（`ct.Constant[int]`）是编译期常量；`ct.gather/ct.scatter` 是按下标取/放数据瓦片的原语；`ct.launch(stream, grid, kernel, args)` 把编译好的内核提交到 GPU。这些只作为「既成事实」引用，不在本讲展开。

## 3. 本讲源码地图

本讲以 `softmax` 为「样板算子」，追踪它从接口到测试的完整链路。涉及的关键文件如下：

| 文件 | 在「新增算子」流程中的角色 |
| --- | --- |
| `src/tilegym/ops/ops.py` | 写统一接口 stub（改动点 1） |
| `src/tilegym/ops/cutile/softmax.py` | 写 cuTile 内核 + 用 `@register_impl` 注册（改动点 2 的实现部分） |
| `src/tilegym/ops/cutile/__init__.py` | 导出模块，让 `@register_impl` 的注册副作用真正发生（改动点 2 的导出部分） |
| `src/tilegym/backend/dispatcher.py` | `register_impl` / `dispatch` 的定义所在，理解注册与分发的运行时机制 |
| `tests/ops/test_softmax.py` | 功能正确性测试样板（改动点 3） |
| `tests/benchmark/bench_softmax.py` | 性能基准样板（改动点 4） |
| `skills/tilegym-adding-cutile-kernel/SKILL.md` | 官方「新增 cuTile 算子」技能卡，把本讲流程固化为 6 步清单 |

记下这条主线：**接口在顶层 `ops.py`，实现在 `ops/cutile/`，测试在 `tests/ops/`，基准在 `tests/benchmark/`，四个目录、四类改动。**

## 4. 核心概念与源码讲解

本讲把「新增算子」拆成四个最小模块，对应四个改动点：**接口 stub（4.1）→ 后端实现 + 注册（4.2）→ 测试（4.3）→ 基准（4.4）**。每个模块都先讲直觉、再讲流程、再精读 softmax 的真实源码。

### 4.1 接口 stub：在 ops.py 注册一个统一算子名

#### 4.1.1 概念说明

TileGym 的算子有两副面孔：一副是「对调用方的承诺」，一副是「对某个后端的具体实现」。接口 stub 就是第一副——它是一份**纯接口规范**：

- 用 `@dispatch("算子名")` 装饰，把算子名（一个字符串）登记进分发体系。
- 函数体**不写任何计算**，只 `raise NotImplementedError(...)`。
- 它声明的是「对所有后端都通用的签名 + docstring」，是别的后端实现必须遵守的契约。
- 它本身会被分发器登记成 `"default"` 实现，仅用于自省（比如报错信息里告诉你「当前后端没实现」），不会被真正调用去算结果。

为什么要这样设计？因为算子名是全局注册表 `_REGISTRY` 的键，各后端（cuTile、tilecpp、triton、cutile-rs）都用 `@register_impl` 把自己的实现挂到**同一个算子名**下。stub 先把这个名字「占住」，并提供一份所有人都要遵守的签名。

此外，stub 还可以带一个 `fallback_backend=...` 参数：当当前后端没有该算子实现时，分发器会回落到这个兜底后端（u2-l1 已讲）。是否设置、设成什么，是**逐算子**的决定。

#### 4.1.2 核心流程

新增一个名为 `my_op` 的算子的接口 stub，流程是：

```text
1. 打开 src/tilegym/ops/ops.py
2. 用 @dispatch("my_op") 装饰一个新函数 def my_op(...)
3. 函数体写 raise NotImplementedError(f"my_op is not implemented for {get_current_backend()}")
4. 写清楚 docstring（参数、返回值），供其他后端实现者参考
5. 决定是否需要 fallback_backend（可选）
```

伪代码骨架：

```python
@dispatch(
    "my_op",                  # 算子名，是 _REGISTRY 的键
    # fallback_backend="triton",  # 可选：主后端缺失时回落到哪个后端
)
def my_op(
    input: torch.Tensor,
    out: Optional[torch.Tensor] = None,
    **kwargs: Any,            # 给各后端留后端专属参数
):
    raise NotImplementedError(f"my_op is not implemented for {get_current_backend()}")
```

两个关键规则：① 函数体只抛异常；② 一定要带 `**kwargs`，因为各后端会有自己的专属参数（如 softmax 的 `use_tma`、`use_chunked`），靠 `**kwargs` 透传。

#### 4.1.3 源码精读

`ops.py` 顶部从 `tilegym.backend` 导入了 `dispatch` 和 `get_current_backend`，这是所有 stub 的依赖：

[src/tilegym/ops/ops.py:19-20](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/ops.py#L19-L20) — 导入 `dispatch` 装饰器与 `get_current_backend()`，后者用于拼装 stub 的报错信息。

softmax 的 stub 是本讲的样板，注意三要素：`@dispatch("softmax")`、统一签名（`x, use_tma, **kwargs`）、只抛 `NotImplementedError` 的函数体：

[src/tilegym/ops/ops.py:225-244](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/ops.py#L225-L244) — `softmax` 的统一接口 stub，声明「对 (M,N) 张量沿最后一维做 softmax」，自身不算结果。

作为对照，看一个**带 fallback_backend 的 stub**。`rms_norm` 把 `fallback_backend` 设成 `"triton"`，意味着当 cuTile 不可用、又没显式指定后端时，分发器会回落到 triton 实现（首次告警、可被 `DISABLE_FALLBACK=1` 抑制）：

[src/tilegym/ops/ops.py:134-137](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/ops.py#L134-L137) — `@dispatch("rms_norm", fallback_backend="triton")`，逐算子声明兜底后端。

再注意一个**反例**：`get_fused_swiglu_module` 故意**没有** `@dispatch` 装饰器——它不走分发，因为融合 MLP 在内部自己调 `matmul`/`silu_and_mul`，后端选择被下放到了算子层。这印证了 u2-l1 的判断标准：**看有没有 `@dispatch`，而不是看它在不在 `ops.py` 里**：

[src/tilegym/ops/ops.py:113-131](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/ops.py#L113-L131) — `get_fused_swiglu_module` 无 `@dispatch`，是非分发函数。

#### 4.1.4 代码实践

**实践目标**：建立「stub 不算结果、只占名字」的直觉，并理解 `fallback_backend` 的作用。

**操作步骤**：

1. 打开 `src/tilegym/ops/ops.py`，用搜索找到 `def softmax`，确认它函数体只有一句 `raise NotImplementedError`。
2. 再找到 `def rms_norm` 与 `def matmul`，对比它们的 `@dispatch(...)` 装饰器：rms_norm 带 `fallback_backend="triton"`，matmul 不带（即默认的 `"pytorch"`，等于「无兜底、缺失即报错」）。

**需要观察的现象 / 预期结果**：

- 三者的函数体长得几乎一样，都是一行抛异常；区别只在装饰器的参数。
- 这说明 stub 的「价值」不在函数体，而在装饰器把名字登记进分发体系、在 docstring 里写下签名契约。

> 待本地验证：如果你尝试在某个「尚未实现该算子」的后端下直接调用 stub（绕过分发），应当看到 `NotImplementedError`。但通过 `tilegym.ops.xxx` 走的是 `wrapper`，不会真跑到 stub 函数体——这一点在 4.2 会再验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 stub 的函数体只 `raise NotImplementedError`，却能在 `tilegym.ops.softmax(x)` 时算出正确结果？

**参考答案**：因为 `tilegym.ops.softmax` 实际调用的是 `@dispatch` 返回的 `wrapper`，wrapper 会查 `_REGISTRY` 路由到当前后端的真实实现（如 cuTile 的 `_Softmax.apply → 内核`）。stub 函数体本身只是「占位 + 报错」，是路由失败时的兜底信息源，正常路径根本不会执行它。

**练习 2**：判断题——「凡是在 `ops.py` 里定义的函数都会走分发。」对还是错？

**参考答案**：错。判断标准是「有没有 `@dispatch` 装饰器」。`get_fused_swiglu_module` 就在 `ops.py` 里却不走分发。

---

### 4.2 后端实现 + 注册：写内核并用 register_impl 挂进注册表

#### 4.2.1 概念说明

接口 stub 占住了「算子名」，但真正干活的是**后端实现**。本模块讲的是 cuTile 后端，新增算子要做两件事，缺一不可：

1. **写实现文件**：在 `src/tilegym/ops/cutile/my_op.py` 里写 cuTile 内核（`@ct.kernel`）和主机侧启动逻辑（`_launch_*` 函数 / autograd 封装），再用 `@register_impl("my_op", backend="cutile")` 装饰那个对外函数，把它挂进 `_REGISTRY["my_op"]["cutile"]`。
2. **导出实现文件**：在 `src/tilegym/ops/cutile/__init__.py` 里 `import` 这个模块。这一步看似多余，却**至关重要**——因为 `@register_impl` 是装饰器，**只有当模块被 Python 真正导入时，装饰器才会执行、注册才会发生**。忘了导出，注册表里就不会有你的实现，调用时只会落到 stub 的 `NotImplementedError`。

这里的认知关键点（来自 u2-l2）：注册表 `_REGISTRY` 是「随导入生长」的——初始为空，导入哪个后端就长出哪个后端的实现，且受 `is_backend_available` 门控。所以「注册」本质是一种**导入副作用**。

#### 4.2.2 核心流程

新增 cuTile 实现 + 注册 + 导出的流程：

```text
A. 写实现文件 src/tilegym/ops/cutile/my_op.py
   1) @ct.kernel 写内核（ConstInt 形参、gather/scatter 搬数据、计算、scatter 写回）
   2) 写主机侧 _launch_my_op(...)：算 grid、contiguous()、ct.launch(stream, grid, kernel, args)
   3) @register_impl("my_op", backend="cutile") 装饰对外函数 my_op(...)
      （需要 autograd 就再套一层 torch.autograd.Function，在 my_op 里调 Xxx.apply(...)）

B. 导出文件 src/tilegym/ops/cutile/__init__.py（在 if is_backend_available("cutile"): 块内）
   1) from . import my_op            # 触发 @register_impl 注册
   2) from .my_op import my_op       # 供直接访问
   3) 把 "my_op" 加进 __all__
```

内核本身的写法（tile 编程、数据搬运、调度）属于 U3–U4 的内容；本模块只关心「实现写好后，怎么把它注册并导出」。骨架示意（**示例代码**，不是项目原有代码）：

```python
# src/tilegym/ops/cutile/my_op.py （示例骨架）
import torch
import cuda.tile as ct
from tilegym.backend import register_impl

ConstInt = ct.Constant[int]

@ct.kernel
def _my_op_kernel(x, output, N_ELEMENTS: ConstInt, BLOCK: ConstInt):
    bid = ct.bid(0)
    offsets = bid * BLOCK + ct.arange(BLOCK, dtype=ct.int32)
    val = ct.gather(x, offsets, check_bounds=True)
    result = ...                       # 你的计算
    ct.scatter(output, offsets, result, check_bounds=True)

@register_impl("my_op", backend="cutile")   # 关键：挂进 _REGISTRY["my_op"]["cutile"]
def my_op(x, out=None, **kwargs):
    n = x.numel()
    if out is None:
        out = torch.empty_like(x)
    x, out = x.contiguous(), out.contiguous()
    BLOCK = 1024
    grid = ((n + BLOCK - 1) // BLOCK,)
    ct.launch(torch.cuda.current_stream(), grid, _my_op_kernel, (out, x, n, BLOCK))
    return out
```

#### 4.2.3 源码精读

先看 softmax 的**内核与启动逻辑**（理解实现长什么样）。这是 cuTile 内核的典型形态：`@ct.kernel(occupancy=4)` 装饰、`ConstInt` 编译期常量、`ct.gather/ct.scatter` 搬数据、静态持久化 grid-stride 调度：

[src/tilegym/ops/cutile/softmax.py:18-50](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py#L18-L50) — `_softmax_kernel`：gather 取一行 → 减最大值 → exp → 求和 → 归一 → scatter 写回，数值稳定的 softmax。

再看**注册那一步**——这是本模块的核心。softmax 的对外函数用 `@register_impl("softmax", backend="cutile")` 装饰，函数体里调 `_Softmax.apply(...)`（autograd 封装）来真正启动内核：

[src/tilegym/ops/cutile/softmax.py:356-383](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py#L356-L383) — `@register_impl("softmax", backend="cutile")` 把 `softmax` 函数挂到 `_REGISTRY["softmax"]["cutile"]`；函数体经 `_Softmax.apply` 走 autograd。

然后看**注册表的运行时定义**，理解「装饰器到底做了什么」。`register_impl` 返回一个装饰器，它把 `func` 塞进 `_REGISTRY[name][backend]` 并**原样返回 func**——也就是说注册是副作用，被装饰的函数照常可用：

[src/tilegym/backend/dispatcher.py:31-54](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/dispatcher.py#L31-L54) — `_REGISTRY` 的结构 `{name: {backend: impl}}`，`register_impl` 的装饰器把实现写入注册表。

分发时 wrapper 就是在这张表里按「当前后端」查实现（u2-l2 已详述）：

[src/tilegym/backend/dispatcher.py:95-97](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/dispatcher.py#L95-L97) — wrapper 查 `_REGISTRY[name][current_backend]` 命中则调用之；这是「接口→实现」的连接点。

最后看**导出**——`cutile/__init__.py` 用一个 `if is_backend_available("cutile"):` 门控块导入所有实现模块。softmax 在三处出现：模块导入、函数导入、`__all__`：

[src/tilegym/ops/cutile/__init__.py:10](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/__init__.py#L10) — 整个导入块的门控：cuTile 不可用时根本不导入任何实现，`__all__` 为空。

[src/tilegym/ops/cutile/__init__.py:35](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/__init__.py#L35) — `from . import softmax`：这一句触发 `softmax.py` 的导入，进而执行 `@register_impl`，注册才真正发生。

[src/tilegym/ops/cutile/__init__.py:62](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/__init__.py#L62) — `from .softmax import softmax`：把对外函数也暴露出来供直接访问。

[src/tilegym/ops/cutile/__init__.py:90](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/__init__.py#L90) — `"softmax"` 加入 `__all__`。

顺带理解上游：`ops/__init__.py` 只在 cuTile 可用时才 `from . import cutile`，并用 `from .ops import *` 把所有 stub 暴露成 `tilegym.ops.xxx`。所以「实现模块被导入」这条链路是从顶层的 `tilegym.ops` 一路条件导入下来的：

[src/tilegym/ops/__init__.py:15-24](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/__init__.py#L15-L24) — `ops/__init__.py` 条件导入 cutile 子包；导入失败时退化为警告并把 `cutile` 置 None。

[src/tilegym/ops/__init__.py:52](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/__init__.py#L52) — `from .ops import *`：所有 `@dispatch` stub 经此成为 `tilegym.ops.softmax` 等统一入口。

#### 4.2.4 代码实践

**实践目标**：把「一次 `tilegym.ops.softmax` 调用如何从 stub 路由到内核」这条链路在源码里走一遍，确认 4.1 + 4.2 的连接。

**操作步骤（源码阅读型实践）**：

1. 从 `src/tilegym/ops/ops.py` 的 `softmax` stub（L225）出发——注意它只是占位。
2. 跳到 `src/tilegym/backend/dispatcher.py` 的 `wrapper`（L74 起），看它如何 `kwargs.pop("backend")` 取显式后端、`get_current_backend()` 取当前后端、查 `_REGISTRY["softmax"]["cutile"]`（L95-97）。
3. 命中后跳到 `src/tilegym/ops/cutile/softmax.py` 的 `softmax`（L356），它调 `_Softmax.apply`（L312）。
4. `_Softmax.forward` 按 `use_multi_wave/use_chunked/use_tma` 四路分发到某个 `_launch_softmax_kernel_*`，后者算 grid、`.contiguous()`、`ct.launch`。

**需要观察的现象 / 预期结果**：

- 你应该能画出一条最短路径：`ops.softmax(stub) → wrapper 查表 → cutile/softmax.softmax → _Softmax.apply → _launch_softmax_kernel → ct.launch`（与 u2-l2 给出的「五步」一致）。
- 这条链路里，`cutile/__init__.py` 的 `from . import softmax`（L35）是「让第 3 步的实现存在于注册表」的前提——去掉它，链路会在第 2 步断裂，回落到 stub 抛错。

> 待本地验证：若把 `cutile/__init__.py` 里的 `from . import softmax` 临时注释掉（仅本地实验，勿提交），再调用 `tilegym.ops.softmax`，预期会触发 fallback 或 `NotImplementedError`——这就是「忘了导出」的后果。

#### 4.2.5 小练习与答案

**练习 1**：`@register_impl("softmax", backend="cutile")` 里的 `"softmax"` 必须和 `ops.py` 里 `@dispatch("softmax", ...)` 的字符串完全一致，为什么？

**参考答案**：因为算子名是 `_REGISTRY` 的顶层键。`@dispatch` 占住 `_REGISTRY["softmax"]`，`@register_impl` 往 `_REGISTRY["softmax"]["cutile"]` 写实现，wrapper 也按这个名字查表。三者名字不一致，注册会写到一个没人查的键下，调用时就查不到实现。

**练习 2**：如果你忘了在 `cutile/__init__.py` 里 `from . import my_op`，会发生什么？

**参考答案**：`my_op.py` 根本不会被导入，`@register_impl` 不会执行，`_REGISTRY["my_op"]` 下不会有 `"cutile"` 键。调用 `tilegym.ops.my_op(...)` 时，wrapper 查不到当前后端实现，于是按 `fallback_backend` 回落（若有）或最终落到 stub 抛 `NotImplementedError`。这正是 SKILL.md 把「Register in `__init__.py`」标为 **CRITICAL** 的原因。

---

### 4.3 测试：用 PyTestCase 与 assertCorrectness 验证正确性

#### 4.3.1 概念说明

内核写完、注册导出后，必须证明它「算得对」。TileGym 的功能测试有一套统一骨架（u9-l1 已讲框架，这里讲「怎么为你的算子落地一个测试」），核心三件套：

- **继承 `common.PyTestCase`**：拿到 `setUp`（重置随机种子、清显存）与 `assertCorrectness`（与参考实现比对）。
- **写一个 `reference` 静态方法**：用 PyTorch 官方算子给出「标准答案」。
- **写一个 `test_op` 方法**：构造输入、按 dtype 选容差、调 `self.assertCorrectness(被测函数, reference, kwargs, ...)`。

为了让一份断言覆盖多个后端，测试用 `@pytest.mark.parametrize("backend", _backends)` 参数化后端，并在方法体里 `set_backend(backend)`、对不可用后端 `pytest.skip`。`assertCorrectness` 会按输入 dtype 自动推断 `atol/rtol`（fp32 默认 `1e-5/1e-8`，fp16 `1e-2/1e-2`），并在 `test_fn` 调用前**深拷贝**输入张量，防止就地内核污染 reference 的输入。

命名上，测试类必须以 `Test_` 开头（如 `Test_Softmax`），方法名用 `test_op`（功能正确性），这是 `tests/ops/README.md` 明确的约定，也是 `pytest -k test_op` 能批量跑起来的依据。

#### 4.3.2 核心流程

为新算子写测试的流程：

```text
1. 新建 tests/ops/test_my_op.py
2. 类名 Test_MY_OP，继承 common.PyTestCase
3. 写 @staticmethod reference(...)：返回 PyTorch 标准答案
4. 定义 _backends = ["cutile"]（有别的后端实现就追加）
5. @pytest.mark.parametrize 覆盖 shape/dtype 等变体
6. @pytest.mark.parametrize("backend", _backends)
7. def test_op(self, ..., backend, arch):
     - 不可用后端 pytest.skip
     - set_backend(backend); self.setUp()
     - self.assertCorrectness(tilegym.ops.my_op, self.reference, {...}, rtol=, atol=)
```

> 关键纪律：**永远从 `tilegym.ops.my_op` 导入被测函数，绝不从 `tilegym.ops.cutile.my_op` 导入**——前者走完整分发，才是真实调用路径；后者绕过分发，等于没测到注册/路由。

#### 4.3.3 源码精读

softmax 的测试是本讲的样板。看类骨架、reference、`_backends`：

[tests/ops/test_softmax.py:14-22](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/ops/test_softmax.py#L14-L22) — `Test_Softmax(common.PyTestCase)`：`reference` 直接转发 `torch.nn.functional.softmax(x, dim=-1)`；`_backends` 默认含 `cutile`，tilecpp 可用时追加。

看参数化与 `test_op` 主体——它把 `(m,n,dtype)`、`backend`、`(use_tma,use_chunked,use_multi_wave)` 三个维度笛卡尔积，按 dtype 选容差，再调 `assertCorrectness`：

[tests/ops/test_softmax.py:24-77](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/ops/test_softmax.py#L24-L77) — `@pytest.mark.parametrize` 多维参数化；`test_op` 里 `tilegym.set_backend(backend)`、不可用则 `pytest.skip`，最后 `self.assertCorrectness(tilegym.ops.softmax, self.reference, {"x": x}, extra_test_kwargs={...}, gradient=dout, rtol=rtol, atol=atol)`。

注意几个可复用的细节：

- 容差**按 dtype 手写**（L64-67）：fp16 用 `1e-3/1e-5`，其余 `1e-5/1e-7`，比 `common.get_dtype_tolerances` 的默认值更贴合 softmax。
- 传 `gradient=dout`（L62、L74）会触发 `assertCorrectness` 额外比对**输入梯度**（前提是输出 `requires_grad`）；softmax 当前 `_Softmax` 只有 forward、输入 `requires_grad=False`，所以这条梯度分支实际不激活，但写法保留了扩展空间（与 u3-l4 的小结一致）。
- `extra_test_kwargs`（L73）把 `use_tma/use_chunked/use_multi_wave` 只传给被测函数、不传给 reference，因为 reference 不认这些后端专属参数。

测试命名约定由 README 固化：

[tests/ops/README.md:21-31](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/ops/README.md#L21-L31) — 约定类名 `Test_` 开头、方法名 `test_op`、继承 `common.PyTestCase`、实现 `reference`、用 `@pytest.mark.parametrize`。

#### 4.3.4 代码实践

**实践目标**：照 softmax 的结构，为新算子写一个最小测试骨架（**示例代码**）。

**操作步骤**：

1. 仿照 `test_softmax.py`，新建 `tests/ops/test_my_op.py`，写出下面的骨架。
2. 先不追求覆盖所有 shape/dtype，只让一条 case 能跑通。

```python
# tests/ops/test_my_op.py （示例骨架）
import pytest
import torch

import tilegym
from tilegym.backend import is_backend_available

from .. import common


class Test_MY_OP(common.PyTestCase):
    @staticmethod
    def reference(x):
        return torch.abs(x)          # 你的算子的 PyTorch 标准答案

    _backends = ["cutile"]

    @pytest.mark.parametrize("shape,dtype", [
        ((256, 2048), torch.float32),
        ((256, 2048), torch.float16),
    ])
    @pytest.mark.parametrize("backend", _backends)
    def test_op(self, shape, dtype, backend, arch):
        if not tilegym.is_backend_available(backend):
            pytest.skip(f"Backend {backend} is not available")
        tilegym.set_backend(backend)
        self.setUp()

        device = torch.device("cuda")
        x = torch.rand(*shape, device=device, dtype=dtype)
        rtol, atol = (1e-3, 1e-5) if dtype == torch.float16 else (1e-5, 1e-7)

        self.assertCorrectness(
            tilegym.ops.my_op,        # 注意：走分发入口
            self.reference,
            {"x": x},
            rtol=rtol,
            atol=atol,
        )
```

**需要观察的现象 / 预期结果**：

- 运行 `pytest tests/ops/test_my_op.py -k test_op -v`（参考 README 的运行方式），每个 `(shape,dtype,backend)` 组合生成一条用例。
- 在 cuTile 可用的机器上，cutile 用例应 `PASSED`；在 cuTile 不可用的机器上应 `SKIPPED` 而非报错。
- 若把被测函数误写成 `from tilegym.ops.cutile.my_op import my_op`，测试仍可能通过，但你**没有测到分发/路由**——这是要避免的反模式。

> 待本地验证：实际是否通过取决于本机 cuTile 是否可用与内核是否正确；若内核有 bug，`assertCorrectness` 会打印 `max absolute difference` 等诊断信息帮助定位。

#### 4.3.5 小练习与答案

**练习 1**：为什么测试里要 `from .. import common` 并继承 `common.PyTestCase`，而不是自己写 `torch.allclose`？

**参考答案**：`PyTestCase.assertCorrectness` 封装了一整套规范比对：按 dtype 自动推断容差、在 `test_fn` 前深拷贝输入防就地污染、可选地比对输入梯度、失败时打印详细的差异诊断。自己写 `allclose` 会漏掉这些（尤其是就地内核污染和梯度检查），也偏离了仓库统一约定。

**练习 2**：`_backends = ["cutile"]` 这一行如果漏写，会有什么后果？

**参考答案**：`@pytest.mark.parametrize("backend", _backends)` 会拿到空列表，`test_op` 不会被任何参数触发，测试文件形同虚设（`no tests ran`）。SKILL.md 也把「Missing `_backends` list」列为常见错误之一。

---

### 4.4 基准：写 bench_*.py 对照 PyTorch

#### 4.4.1 概念说明

功能对还不够，性能也要看得见。TileGym 的基准放在 `tests/benchmark/bench_*.py`，由 `run_all.sh` 递归发现并批量执行（u9-l1 已讲框架）。一个基准文件要做四件事：

1. **写一个 reference 实现**（PyTorch 版本），并用 `register_impl("算子名", "torch")` 把它注册成 `"torch"` 后端——这样基准里就能把 cuTile 与 PyTorch 放在同一张图里比。
2. **声明对比后端**：用 `ALL_BACKENDS` 列表枚举 `(后端名, 显示名, 画图样式)`，并用 `get_supported_backends()` 按可用性过滤掉不可用的。
3. **用 `triton.testing.Benchmark` 声明横纵轴**（x 轴是问题规模、line 是不同后端），用 `@triton.testing.perf_report([...])` 装饰 `bench_xxx`。
4. **在 bench 函数里**：先用 `torch.testing.assert_close` 做一次正确性兜底，再用 `profile_with_l2flush(fn)`（或 `do_bench`）测纯内核时间，最后换算成带宽 GB/s 或算力 TFLOPS 返回。

两条关键纪律（来自官方技能卡）：

- **调用算子时用 `tilegym.ops.my_op(..., backend=backend)` 显式传 `backend=`，不要用 `set_backend`**——因为基准要在同一进程里反复切换后端，显式参数是调用级覆盖，不污染进程级状态。
- **`plot_name` 要带 `-GBps` 或 `-TFLOPS` 后缀**，便于结果归档。

注意一个细节：基准里 `register_impl` 的两个参数是**位置参数** `register_impl("softmax", "torch")`，而 cuTile 实现里是 `@register_impl("softmax", backend="cutile")`（关键字）。两者等价，因为函数签名是 `register_impl(name, backend)`。

#### 4.4.2 核心流程

```text
1. 新建 tests/benchmark/bench_my_op.py
2. def reference_my_op(...): PyTorch 实现
3. register_impl("my_op", "torch")(reference_my_op)   # 注册 torch 后端供对比
4. ALL_BACKENDS = [("cutile",...) if is_backend_available("cutile") else None,
                   ("torch", "PyTorch", (...))]
5. get_supported_backends() 过滤 None
6. create_benchmark_config(...) 返回一个 triton.testing.Benchmark
7. @triton.testing.perf_report([...]) def bench_my_op(..., backend, ...):
     fn  = lambda: tilegym.ops.my_op(x, backend=backend)   # 显式 backend=
     ref = lambda: reference_my_op(x)
     torch.testing.assert_close(fn(), ref(), atol=1e-2, rtol=1e-2)
     ms = profile_with_l2flush(fn)
     return 带宽或算力
8. 文件末尾：bench_my_op.run(print_data=True)
```

#### 4.4.3 源码精读

softmax 的基准是本讲样板。先看 reference 与「注册 torch 后端」：

[tests/benchmark/bench_softmax.py:16-26](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/benchmark/bench_softmax.py#L16-L26) — `reference_softmax` 转发 `torch.nn.functional.softmax`，并经 `register_impl("softmax", "torch")(reference_softmax)` 把它挂成 `"torch"` 后端实现，使其能与 cuTile 同台对比。

看对比后端声明与可用性过滤——cuTile/tilecpp 各自按 `is_backend_available` 决定是否进图，torch 恒在：

[tests/benchmark/bench_softmax.py:30-39](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/benchmark/bench_softmax.py#L30-L39) — `ALL_BACKENDS` 用三元组 `(后端, 显示名, (颜色, 线型))`，`None` 占位表示不可用；`get_supported_backends()` 把 `None` 过滤掉。

看 `bench_softmax` 主体——注意它用 `tilegym.ops.softmax(..., backend=backend)` 显式切后端、先 `assert_close` 兜底正确性、再用 `profile_with_l2flush` 测纯内核时间，最后按「读输入 + 写输出 = 2×numel×element_size」换算带宽：

[tests/benchmark/bench_softmax.py:72-113](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/benchmark/bench_softmax.py#L72-L113) — `@triton.testing.perf_report([...])` 装饰 `bench_softmax`；内部 `fn` 显式传 `backend=`，`assert_close` 兜底，`profile_with_l2flush(fn)` 测时，返回 GB/s；末尾 `bench_softmax.run(print_data=True)` 是入口。

计时工具 `profile_with_l2flush` 用 `torch.profiler`（CUPTI）只统计 CUDA kernel 时间，并在每次测量前清 L2，以测真实带宽（与 u9-l1 一致）：

[tests/benchmark/bench_utils.py:10-31](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/benchmark/bench_utils.py#L10-L31) — `profile_with_l2flush`：warmup 后每轮先清 L2，再用 `torch.profiler` 求该轮所有 CUDA kernel 的 `device_time_total` 之和，取中位数返回毫秒。

最后，`run_all.sh` 自动发现所有 `bench_*.py` 并依次执行（串行以保证测量准确）：

[tests/benchmark/run_all.sh:68-96](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/benchmark/run_all.sh#L68-L96) — `find . -name 'bench_*.py'` 递归发现、排序后逐个 `python3 "$file"`，输出落盘为 `*_results.txt`。

#### 4.4.4 代码实践

**实践目标**：理解「为什么基准要用 `backend=` 而不是 `set_backend`」，并能照抄 softmax 的结构。

**操作步骤（源码阅读型 + 思考）**：

1. 打开 `tests/benchmark/bench_softmax.py`，定位 `fn = lambda: tilegym.ops.softmax(..., backend=backend)`（L89-95），确认 `backend` 是作为关键字参数传进去的。
2. 回想 u2-l2：`wrapper` 里 `explicit_backend = kwargs.pop("backend", None)` 是**调用级、最高优先级**的后端覆盖，且不改进程级当前后端。
3. 想象如果改成 `set_backend(backend)` 会怎样：同一次 `perf_report` 里多个后端会互相覆盖进程级状态，且 warmup 与计时阶段可能跑到不同后端上，测量失去意义。

**需要观察的现象 / 预期结果**：

- 你应当能解释：基准在同一进程内对 cutile/torch 两个 `backend` 值反复调用 `fn`，靠的是**每次调用显式传 `backend=`**，互不干扰。
- `assert_close(fn(), ref(), atol=1e-2, rtol=1e-2)`（L97）是一道安全网：性能测量前先确认结果对得上，避免「测了一个错误内核的速度」。

> 待本地验证：在有 GPU 的机器上 `python tests/benchmark/bench_softmax.py`，预期打印一张 GB/s 随 N 变化的表；cuTile 列应不低于（通常显著高于）PyTorch 列。

#### 4.4.5 小练习与答案

**练习 1**：基准里 `register_impl("softmax", "torch")(reference_softmax)` 这一句如果删掉，会发生什么？

**参考答案**：`_REGISTRY["softmax"]` 下不会有 `"torch"` 键。于是 `tilegym.ops.softmax(x, backend="torch")` 查不到 torch 实现，回落到 stub 抛 `NotImplementedError`，基准会在 `assert_close` 或 `fn()` 处直接报错。这句注册是「让 PyTorch 作为可比后端」的前提。

**练习 2**：为什么 `plot_name` 要带 `-GBps` 后缀？

**参考答案**：这是仓库的结果归档约定（SKILL.md 明确要求），便于 `run_all.sh` 产出的 `*_results.txt` 与后续绘图脚本按指标（带宽/算力）分类检索。

---

## 5. 综合实践

把四个改动点串起来：**仿照 softmax，规划新增一个 `abs` 算子（逐元素取绝对值）所需的四个改动点**。请按下表逐处列出要改的文件与函数（本任务是「规划」+ 给出骨架，骨架为**示例代码**，非项目原有代码）。

| 改动点 | 文件 | 要新增/修改的内容 |
| --- | --- | --- |
| ① 接口 stub | `src/tilegym/ops/ops.py` | 新增 `@dispatch("abs")` 装饰的 `def abs(x, **kwargs)` stub，函数体 `raise NotImplementedError(...)` |
| ② 后端实现 + 注册 | `src/tilegym/ops/cutile/abs.py`（新建） | `@ct.kernel` 内核 + `@register_impl("abs", backend="cutile")` 的对外函数 `abs` |
| ② 导出 | `src/tilegym/ops/cutile/__init__.py` | `from . import abs` / `from .abs import abs` / `__all__` 加 `"abs"` |
| ③ 测试 | `tests/ops/test_abs.py`（新建） | `Test_Abs(common.PyTestCase)` + `reference=torch.abs` + `test_op` |
| ④ 基准 | `tests/benchmark/bench_abs.py`（新建） | `reference_abs` + `register_impl("abs","torch")` + `bench_abs` |

> 注意：`abs` 是 Python 内置名，作为算子名/文件名时要注意别在 `ops.py` 里覆盖内置语义；仓库现有算子名都避免与内置冲突，实践时可用 `absval` 等替代名以免干扰。

**操作步骤**：

1. **接口 stub**：在 `ops.py` 仿照 softmax 加一个 stub（决定 `abs` 是否需要 `fallback_backend`？逐元素取绝对值很简单，通常不需要兜底，留默认即可）：

   ```python
   # 示例代码
   @dispatch("abs")
   def abs(x: torch.Tensor, **kwargs: Any):
       """Elementwise absolute value."""
       raise NotImplementedError(f"abs is not implemented for {get_current_backend()}")
   ```

2. **后端实现 + 注册**：`abs` 是纯逐元素、无跨元素归约，可借用 u4-l1 的「row-wise grid」模板（一块算一整行，不读 SM 数、不带 occupancy）。写 `src/tilegym/ops/cutile/abs.py`（示例骨架）：

   ```python
   # 示例代码
   import torch
   import cuda.tile as ct
   from tilegym.backend import register_impl

   ConstInt = ct.Constant[int]

   @ct.kernel
   def _abs_kernel(x, output, N_COLS: ConstInt, TILE: ConstInt):
       row = ct.bid(0)
       offs = ct.arange(TILE, dtype=ct.int32)
       val = ct.gather(x, (row, offs), check_bounds=True)
       ct.scatter(output, (row, offs), ct.abs(val), check_bounds=True)

   @register_impl("abs", backend="cutile")
   def abs(x, out=None, **kwargs):
       if out is None:
           out = torch.empty_like(x)
       x, out = x.contiguous(), out.contiguous()
       n_rows, n_cols = x.shape
       TILE = 1024
       grid = (n_rows, 1, 1)
       ct.launch(torch.cuda.current_stream(), grid, _abs_kernel, (x, out, n_cols, TILE))
       return out
   ```

   然后在 `cutile/__init__.py` 的 `if is_backend_available("cutile"):` 块内补三行（`from . import abs` / `from .abs import abs` / `__all__` 加 `"abs"`）。

3. **测试**：新建 `tests/ops/test_abs.py`，`reference` 用 `lambda x: torch.abs(x)`，参数化 `(shape, dtype)` 与 `backend`，调 `self.assertCorrectness(tilegym.ops.abs, self.reference, {"x": x}, rtol=, atol=)`。fp32 取绝对值是精确的，容差可给到很紧（如 `atol=0, rtol=0`）。

4. **基准**：新建 `tests/benchmark/bench_abs.py`，仿 softmax：写 `reference_abs`、`register_impl("abs","torch")(reference_abs)`、`ALL_BACKENDS` 含 cutile+torch、`bench_abs` 用 `tilegym.ops.abs(x, backend=backend)` 并按 `2*numel*element_size` 算带宽，`plot_name` 带 `-GBps`。

5. **验证**：依次运行 `pytest tests/ops/test_abs.py -k test_op -v` 与 `python tests/benchmark/bench_abs.py`，最后 `pre-commit run -a` 过 lint（与 SKILL.md Step 6 一致）。

**需要观察的现象 / 预期结果**：

- 四个改动点缺任何一个，链路都会断：漏 stub → `tilegym.ops.abs` 不存在；漏 `register_impl` → 查不到 cutile 实现；漏 `__init__` 导出 → `register_impl` 不执行，等同漏注册；漏测试/基准 → 无法证明正确性与性能。
- 若每步都到位，`pytest` 应全绿，基准应画出 cuTile ≥ PyTorch 的带宽曲线。

> 待本地验证：本综合实践要求 cuTile 可用的 GPU 环境；无 GPU 时，可退化为「源码阅读型」——逐文件核对上述四处改动是否与 softmax 的样板一一对应。

## 6. 本讲小结

- 新增一个 cuTile 算子是**四个目录、四类改动**的固定流程：`ops.py` 写 stub、`ops/cutile/` 写实现+注册、`tests/ops/` 写测试、`tests/benchmark/` 写基准。
- 接口 stub 是纯规范：`@dispatch("算子名")` + 只抛 `NotImplementedError` 的函数体 + `**kwargs`，它的价值在「占名字、立签名」，不在函数体。
- 后端实现用 `@register_impl("算子名", backend="cutile")` 挂进 `_REGISTRY`；而注册是**导入副作用**，必须在 `cutile/__init__.py` 的 `is_backend_available` 门控块里 `from . import xxx` 才会真正发生——这一步 CRITICAL，漏了等于没注册。
- 测试继承 `common.PyTestCase`，写 `reference`（PyTorch 标准答案）+ `test_op`，按 dtype 选容差，用 `@pytest.mark.parametrize("backend", _backends)` 覆盖多后端；被测函数**必须**从 `tilegym.ops.xxx` 导入以走完整分发。
- 基准用 `register_impl("算子名","torch")` 注册 PyTorch 参与对比，调用时用**显式 `backend=`** 而非 `set_backend`，`plot_name` 带 `-GBps`/`-TFLOPS` 后缀。
- 官方技能卡 `skills/tilegym-adding-cutile-kernel/SKILL.md` 把本流程固化为 6 步清单（接口→实现→导出→测试→基准→验证），可作为实操时的勾选表。

## 7. 下一步学习建议

- **若你的新算子需要 autograd**：本讲的 softmax 只示范了 forward。完整的「前向+反向+重计算」范式见 u4-l2（以 silu_and_mul 为样本），照它把 `torch.autograd.Function` 的 `forward/backward` 套到你的 `_launch_*` 之外。
- **若你的新算子需要 autotuning**：性能敏感的算子（如 GEMM）应接入 `autotune.py` 的 tune-once/cache/launch 模式，详见 u5-l3。
- **若想给同一个算子加多后端实现**：照本讲的「实现+注册」在 `ops/tilecpp/`、`ops/triton/`、`ops/cutile_rs/` 各做一份，分别 `@register_impl("算子名", backend=...)` 挂到**同一个算子名**下，分发机制会自动按当前后端选择（多后端架构见 U7）。
- **若想把新算子接进真实 LLM**：实现完算子后，参考 u8-l1 的 `monkey_patch.py`，新增一个 `apply_tilegym_kernel_to_xxx` 并在 `MODEL_TYPE_TO_APPLY_TILEGYM_FN` 表里登记。
- **持续参考官方技能卡**：实操时把 `skills/tilegym-adding-cutile-kernel/SKILL.md` 的 6 步 TodoWrite 清单打开逐项勾选，可避免漏掉导出、`_backends`、`plot_name` 后缀等高频坑。
