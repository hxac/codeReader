# Kernel 选择与架构兼容性

## 1. 本讲目标

上一讲（u2-l1）我们俯瞰了 `Op` 基类的生命周期，知道 Kernel 安装走的是统一流程 `dispatch_kernel → _install_kernel_map`。本讲把镜头推进到这条流程的内部，回答三个问题：

1. 一个 Op 到底是怎么把「登记表」变成「可用的 `self.kernel_map`」的？——理解 `_install_kernel_map`。
2. TileOPs 怎么保证一个为 Hopper（SM_90）写的 kernel 不会被错误地拿到其它架构上运行？——理解 `supported_archs` 的运行时检查。
3. 作为用户，我能不能替换掉某个 Op 默认的 kernel 实现？——理解 `default_kernel_map override` 的 override 路径。

学完后，你应当能够：读懂任意 Op 的 `default_kernel_map`、解释架构校验在何时抛错、并用 `kernel_map` 参数替换默认 kernel。

## 2. 前置知识

- **Op / Kernel 双层分离**（u1-l1、u2-l1）：`Op` 是主机侧无状态入口，`Kernel` 是 TileLang 硬件相关实现。本讲只关心 L2（Op）这一层如何「选」kernel，不进入 L1 内部。
- **dispatch_kernel 时序**（u2-l1）：构造期调用 `dispatch_kernel(kernel_map)`，它内部再调用 `_install_kernel_map`。本讲拆的就是这第二步。
- **SM 版本号**：NVIDIA GPU 的「计算能力」（compute capability）是一个 `(major, minor)` 元组，例如 Hopper 是 `(9, 0)`。TileOPs 把它压成一个整数 `major*10 + minor`，即 Hopper 对应 `90`。后续把「当前设备的整数 SM 版本」简称 `current_arch`。
- **default_kernel_map 是登记表**（u2-l1）：它是 `Op` 的抽象属性，返回 `{"dispatch_key": KernelClass}` 的字典——是「名字 → kernel 类」的注册表，构造期填充，调用期才真正实例化。
- **dispatch_key 是字符串**：例如 `"gemm_kernel"`、`"gemv_kernel"`。它不是 Python 关键字参数名，而是 Op 内部用来在 `kernel_map` 里查找某个 kernel 实现的键。

如果你对以上术语还不熟，建议先读 u2-l1《Op 基类与生命周期》再回来。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [tileops/ops/op_base.py](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/op_base.py) | `Op` 基类。本讲重点是 `_install_kernel_map`、`dispatch_kernel`、抽象属性 `default_kernel_map`。 |
| [tileops/ops/gemm.py](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/gemm.py) | `GemmOp` —— 本讲的主案例。它的 `default_kernel_map` 展示了「条件注册」模式，`_get_kernel` 展示了运行时如何用 dispatch_key 取 kernel。 |
| [tileops/kernels/kernel_base.py](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/kernels/kernel_base.py) | `Kernel` 基类。本讲引用其中的类属性 `supported_archs`。 |
| [tileops/kernels/gemm.py](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/kernels/gemm.py) | `GemmKernel` / `GemvKernel` 的真实实现，二者都声明 `supported_archs = [90]`。 |
| [tileops/utils/utils.py](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/utils/utils.py) | `get_sm_version()` —— 把设备能力压成整数 SM 版本。 |

> 注意：有两个 `gemm.py`。一个是 **Op 层**（`tileops/ops/gemm.py`，定义 `GemmOp`），一个是 **Kernel 层**（`tileops/kernels/gemm.py`，定义 `GemmKernel`）。引用时会明确标注「Op 层」或「Kernel 层」以区分。

## 4. 核心概念与源码讲解

### 4.1 `_install_kernel_map`：统一的「校验 + 安装」路径

#### 4.1.1 概念说明

`_install_kernel_map` 是 Op 安装 kernel 的**唯一落点**。它的职责是：拿到子类声明的「默认登记表」`default_kernel_map`、合并用户可能传入的「覆盖登记表」`kernel_map`、对每一个解析出的 kernel 类做架构兼容性校验，最后把结果写回 `self.kernel_map`。

它要解决的核心问题是：**自动发现的默认 kernel 与用户手动覆盖的 kernel，必须走同一条校验路径**。否则会出现「默认 kernel 会做架构检查、用户替换的 kernel 不做检查」的不对称，导致一个为 Hopper 写的 kernel 被悄悄装到 Ampere 上，直到运行时才崩溃。

这正是 u2-l1 提到的「构造期填登记表」的下半场——上半场（`default_kernel_map`）声明了表里**应该有**什么，下半场（`_install_kernel_map`）负责**把表落定下来并把关**。

#### 4.1.2 核心流程

`_install_kernel_map(candidate_map)` 的执行过程可以概括为：

```text
1. 取 default_map = self.default_kernel_map        # 子类声明的默认登记表
2. 若 default_map 为空 / None（复合 Op 分支）：
     - 直接把 candidate_map 原样存为 self.kernel_map（若有）
     - 立即返回（架构校验交给子 Op 自己负责）
3. 否则（普通 Op 分支）：
     - current_arch = get_sm_version()              # 读当前设备 SM 版本
     - 对 default_map 里的每个 (name, default_kernel)：
         a. 若 candidate_map 里也有同名 key → 用用户的覆盖类
         b. 否则 → 用默认类 default_kernel
         c. 校验：若 该类的 supported_archs 非 None
                   且 current_arch 不在其中 → 抛 ValueError
         d. 把解析出的类写入 resolved[name]
4. self.kernel_map = resolved                       # 落定登记表
```

关键设计点是第 3.c 步：**无论是默认类还是用户覆盖类，只要它声明了 `supported_archs` 且当前架构不在其中，一律在构造期就抛 `ValueError`**。这就把「装错架构」的问题从「运行时崩溃」提前到了「构造时失败」，是 fail-fast 思想的体现。

#### 4.1.3 源码精读

先看入口与签名。[tileops/ops/op_base.py:L157-L190](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/op_base.py#L157-L190) 是 `_install_kernel_map` 的全部实现：

```python
def _install_kernel_map(self, candidate_map: Optional[dict[str, Kernel]] = None) -> None:
    default_map = self.default_kernel_map
    if default_map is None or len(default_map) == 0:
        # Composite op: store override verbatim; sub-ops enforce arch-compat themselves.
        self.kernel_map = dict(candidate_map) if candidate_map else {}
        return
    resolved: dict[str, Kernel] = {}
    current_arch = get_sm_version()
    for name, default_kernel in default_map.items():
        if candidate_map is not None and name in candidate_map:
            kernel_type = candidate_map[name]
        else:
            kernel_type = default_kernel
        if (
            kernel_type is not None
            and kernel_type.supported_archs is not None
            and current_arch not in kernel_type.supported_archs
        ):
            raise ValueError(
                f'{kernel_type.__name__} is not supported on architecture {current_arch}')
        resolved[name] = kernel_type
    self.kernel_map = resolved
```

逐段拆解：

- **复合 Op 分支（L171–L174）**：当 `default_kernel_map` 返回空（`None` 或长度为 0），说明这个 Op 是「复合 Op」——它不直接持有 kernel，而是内部编排若干子 Op（如 attention、MoE 家族）。此时 `_install_kernel_map` 不做架构校验，只把用户传入的 `candidate_map` 原样存下来。架构校验的责任下放到各子 Op 各自的 `_install_kernel_map` 调用。注释 `Composite op: ... sub-ops enforce arch-compat themselves` 说的就是这个边界划分。

- **普通 Op 分支（L175–L190）**：这是大多数 Op（含 `GemmOp`）走的路径。
  - `current_arch = get_sm_version()`：读一次当前设备的 SM 版本。`get_sm_version` 的实现在 [tileops/utils/utils.py:L24-L26](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/utils/utils.py#L24-L26)，就是 `major*10 + minor`。
  - 遍历 `default_map` 的每个 `(name, default_kernel)`。Python 3.7+ 的 dict 保序，所以遍历顺序与子类声明顺序一致。
  - **override 选择（L178–L181）**：如果用户在 `candidate_map` 里提供了同名 key，就用用户的类；否则用默认类。这就是 `kernel_map` 参数能逐 key 覆盖的原理（详见 4.3）。
  - **架构校验（L182–L188）**：三段式条件——「类非 None」且「声明了 `supported_archs`」且「当前架构不在其中」时抛错。注意第二段：`supported_archs is None` 意味着「不限架构」，直接放行（详见 4.2）。

再看它的调用方。[tileops/ops/op_base.py:L192-L197](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/op_base.py#L192-L197) 是 `dispatch_kernel`，它是构造期的「auto-discovery 入口」：

```python
def dispatch_kernel(self, kernel_map: Optional[dict[str, Kernel]] = None) -> None:
    """Resolve and install the kernel map (auto-discovery entry point)."""
    self._install_kernel_map(kernel_map)
    # Conforming __init__s all pass through here — the zero-boilerplate
    # registration point for the compile dispatch boundary.
    self._instance_key = register_instance(self)
```

它只做两件事：调 `_install_kernel_map`，再调 `register_instance` 把自己注册进 torch.compile 的分发边界（这是 u10-l1 的主题，本讲不展开）。子类的 `__init__` 几乎都会有一行 `self.dispatch_kernel(kernel_map)`——例如 [tileops/ops/gemm.py:L55](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/gemm.py#L55) 的 `GemmOp.__init__` 里就有 `self.dispatch_kernel(kernel_map)`。

#### 4.1.4 代码实践（源码阅读型）

**实践目标**：确认「默认 kernel 与用户覆盖 kernel 走同一条校验路径」这一设计意图。

**操作步骤**：

1. 打开 [tileops/ops/op_base.py:L157-L190](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/op_base.py#L157-L190)。
2. 在 `for name, default_kernel in default_map.items()` 循环体内，定位 `kernel_type = candidate_map[name]`（用户覆盖分支）与 `kernel_type = default_kernel`（默认分支）。
3. 观察两条分支之后紧跟的是**同一段** `if (... supported_archs ...): raise ValueError(...)`。

**需要观察的现象**：架构校验代码在 override 分支与 default 分支之后是**共享**的，不是分别写在两个分支里。

**预期结果**：你能用自己的话指出——无论一个 kernel 类来自 `default_kernel_map` 还是用户的 `kernel_map` 参数，只要它声明了 `supported_archs` 且当前架构不匹配，都会触发**完全相同的** `ValueError`。这正是方法 docstring 里「Both auto-discovered and user-supplied maps share this single validate-and-install path」的含义。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `_install_kernel_map` 要在**构造期**就做架构校验，而不是等到 `forward` 第一次真正实例化 kernel 时？

> **参考答案**：构造期 fail-fast 能把「装错架构」从运行时崩溃提前到构造时失败，错误信息更早、更明确（直接点名哪个 kernel 类不支持当前架构）。若推迟到 `forward`，用户可能写了一长串流程、跑了大半个 batch 才在 JIT 编译时炸出难以定位的底层错误。

**练习 2**：`_install_kernel_map` 里 `current_arch = get_sm_version()` 只调用了一次，放在循环外。这样做有什么好处？

> **参考答案**：同一进程内设备的 SM 版本不变，循环外取一次避免重复调用 `torch.cuda.get_device_capability()`（一次 CUDA 运行时查询）。这也让校验语义清晰——所有 kernel 用同一个 `current_arch` 做判断，避免在循环中出现「基准漂移」的误解。

---

### 4.2 `supported_archs`：运行时架构兼容检查

#### 4.2.1 概念说明

`supported_archs` 是 `Kernel` 类上的一个类属性，声明「这个 kernel 能在哪些 SM 版本上运行」。它出现在 [tileops/kernels/kernel_base.py:L12](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/kernels/kernel_base.py#L12)：

```python
class Kernel(ABC):
    dtype: Optional[torch.dtype] = None
    config: Dict[str, Any]
    autotune_configs: Optional[list[dict]] = None
    supported_archs: Optional[list[int]] = None   # ← 本讲主角
```

它有两种取值，语义不同：

| 取值 | 含义 | 架构校验行为 |
| --- | --- | --- |
| `None`（基类默认） | 「不限架构」——这个 kernel 不依赖任何架构特有指令 | `_install_kernel_map` 直接放行 |
| `[80, 86, 89, 90]` 这类列表 | 「只能在这些 SM 版本上运行」 | 当前架构不在列表里就抛 `ValueError` |

这就解释了 `_install_kernel_map` 里那段三段式条件的第二段：`kernel_type.supported_archs is not None`。只有 kernel **主动**声明了限制，才会触发校验；没声明就当作「随处可用」。

为什么需要这个机制？因为 TileOPs 主攻 Hopper，大量 kernel 用了 Hopper 特有的硬件特性（TMA、WGMMA、warp specialization、cp.async 流水等）。这些指令在 Ampere（SM_80）上根本不存在。`supported_archs` 就是给每个 kernel 贴一张「我能跑在哪」的标签，让 Op 层在安装时据此把关。

#### 4.2.2 核心流程

从「声明」到「检查」的完整链路：

```text
Kernel 子类定义时：
    supported_archs = [90]              # 类属性，贴标签
        ↓
Op.__init__ → dispatch_kernel → _install_kernel_map：
    current_arch = get_sm_version()     # 读设备，例如 90
        ↓
    对每个待安装的 KernelClass：
        若 KernelClass.supported_archs is not None
           且 current_arch not in supported_archs：
               raise ValueError(f'{ClassName} is not supported on architecture {current_arch}')
        ↓
    通过 → self.kernel_map[name] = KernelClass
```

关键点：校验发生在**类**这一层（`kernel_type.supported_archs`、`kernel_type.__name__`），此时 kernel 还**没有被实例化**。也就是说，架构检查发生在「把 Kernel 类登记进 `self.kernel_map`」时，远早于「在 `forward` 里 `kernel_map[name](...)` 真正 new 出实例」。

#### 4.2.3 源码精读

来看两个真实 kernel 的声明。Kernel 层的 `GemmKernel` 与 `GemvKernel` 都把自己锁死在 Hopper：

[tileops/kernels/gemm.py:L528-L537](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/kernels/gemm.py#L528-L537)：

```python
class GemmKernel(Kernel):
    """Dense GEMM kernel: a hand-written warp-specialized implementation (SM90).

    ... fp16 / bf16 inputs, fp32 accumulation. Hopper-only — TMA + WGMMA require SM90.
    """
    supported_archs: list[int] = [90]
```

[tileops/kernels/gemm.py:L680-L681](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/kernels/gemm.py#L680-L681)：

```python
class GemvKernel(Kernel):
    supported_archs: list[int] = [90]
```

`GemmKernel` 的 docstring 把理由说得直白：**「Hopper-only — TMA + WGMMA require SM90」**。TMA（Tensor Memory Accelerator）和 WGMMA（Warpgroup Matrix Multiply-Accumulate）都是 Hopper 才有的硬件单元，所以这个 kernel 物理上无法在更早的架构运行。

对照一下，全仓库并非所有 kernel 都 `[90]`。例如大多数 elementwise / pool / reduction kernel 声明的是 [tileops/kernels/elementwise.py:L656](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/kernels/elementwise.py#L656) 的 `supported_archs: list[int] = [80, 86, 89, 90]`——它们只用了通用的 GPU 并行原语，没有架构特有指令，因此能在 Ampere（80/86）、Ada（89）、Hopper（90）上一并运行。这正展示了「声明越宽，能在越多设备上安装」。

再看 SM 版本是怎么读出来的。[tileops/utils/utils.py:L24-L26](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/utils/utils.py#L24-L26)：

```python
def get_sm_version():
    major, minor = torch.cuda.get_device_capability()
    return major * 10 + minor
```

`torch.cuda.get_device_capability()` 返回当前 CUDA 设备的 `(major, minor)`，例如 Hopper 是 `(9, 0)`，压成 `90`。这个整数就是 `_install_kernel_map` 里 `current_arch` 的来源，也是与 `supported_archs` 列表做 `in` 判断的右值。

#### 4.2.4 代码实践（源码阅读型）

**实践目标**：用 `grep` 统计仓库里不同 `supported_archs` 取值的分布，建立「哪些算子锁 Hopper、哪些跨架构」的直觉。

**操作步骤**：

1. 在仓库根目录执行 `grep -rn "supported_archs" tileops/kernels/ | grep "= \["`。
2. 把结果按取值分桶：`[90]` 一组、`[80, 86, 89, 90]` 一组、`[80, 89, 90]` 一组等。
3. 对 `[90]` 这一组的文件名，猜测它们为什么锁 Hopper（多半用了 TMA / WGMMA / persistent / warp-specialized）。

**需要观察的现象**：`[90]` 集中出现在 `gemm.py`、`bmm.py`、`fp8_quant.py`、`grouped_gemm_persistent*.py`、`topk_selector.py` 这类「矩阵乘 / 持久化 / FP8」路径；而 elementwise / pool / reduction 这类「逐元素或规约」路径多是 `[80, 86, 89, 90]`。

**预期结果**：你能总结出一条经验——越是「贴近 tensor core 与 TMA 数据搬运」的 kernel，`supported_archs` 越窄（往往只 `[90]`）；越是「通用并行计算」的 kernel，列表越长。**待本地验证**：在不同 GPU 上安装 TileOPs 时，哪些 Op 会在构造期因架构不匹配而失败。

#### 4.2.5 小练习与答案

**练习 1**：一个 kernel 没有写 `supported_archs` 这一行，会发生什么？

> **参考答案**：它会继承 `Kernel` 基类的默认值 `None`（见 [kernel_base.py:L12](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/kernels/kernel_base.py#L12)）。`_install_kernel_map` 的三段式条件里有 `kernel_type.supported_archs is not None`，`None` 时这一段为假，整个 `if` 不成立，直接放行。即「不声明 = 不限架构」。

**练习 2**：假设你在 SM_89（Ada）的机器上构造 `GemmOp()`，会观察到什么？

> **参考答案**：`GemmOp.__init__` 调 `dispatch_kernel` → `_install_kernel_map`。`default_kernel_map` 返回的第一个条目是 `gemm_kernel → GemmKernel`，而 `GemmKernel.supported_archs = [90]`，`current_arch = 89` 不在其中，于是抛 `ValueError: GemmKernel is not supported on architecture 89`。构造期就失败，不会进入 `forward`。

---

### 4.3 `default_kernel_map` override：用户替换默认 kernel

#### 4.3.1 概念说明

「override」（覆盖）指的是：用户在构造 Op 时，可以传一个 `kernel_map` 参数，用它**逐 key 替换**默认登记表里的 kernel 类。这是 TileOPs 留给用户的一条扩展缝——不需要改源码，就能把某个 Op 的某条 dispatch_key 换成自定义实现。

回顾可调用契约（u1-l4、u2-l1）：`op = XxxOp(构造参数, kernel_map=...)`，其中 `kernel_map` 是可选的。`GemmOp.__init__` 的签名里就明摆着这个参数——见 [tileops/ops/gemm.py:L45-L55](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/gemm.py#L45-L55)：

```python
def __init__(
    self,
    trans_a: bool = False,
    trans_b: bool = True,
    kernel_map: Optional[Dict[str, Kernel]] = None,   # ← override 入口
    tune: bool = False,
) -> None:
    ...
    self.dispatch_kernel(kernel_map)
```

`default_kernel_map` 本身是 `Op` 的**抽象属性**，强制子类必须实现（[tileops/ops/op_base.py:L74-L77](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/op_base.py#L74-L77)）。它在 u2-l1 里被定位为「登记表」。本讲补上它的另一半身份：**它是 override 的「基准」**——`_install_kernel_map` 遍历的就是这张表的 key，用户只能覆盖表中**已存在**的 key，不能新增。

#### 4.3.2 核心流程

override 的语义可以用一张并排对照表说清：

| 场景 | `default_kernel_map` 提供的 key | 用户 `kernel_map` 提供的同名 key | 最终 `self.kernel_map` 取值 |
| --- | --- | --- | --- |
| 不 override | `gemm_kernel → GemmKernel` | （无） | `GemmKernel`（默认） |
| 覆盖某 key | `gemm_kernel → GemmKernel` | `gemm_kernel → MyGemm` | `MyGemm`（用户胜） |
| 用户传了表里没有的 key | `gemm_kernel → GemmKernel` | `foo_kernel → Foo` | `foo_kernel` 被**忽略**（不在遍历范围内） |

第三行是关键限制：`_install_kernel_map` 的循环是 `for name, _ in default_map.items()`，遍历的是**默认表**的 key，不是用户表的 key。所以用户的 `kernel_map` 只能「替换已有 key」，不能「新增 key」。

一个特殊的 override 案例：把某 key 显式设为 `None`。因为校验条件里有 `kernel_type is not None` 这一段，传 `None` 等于「把这个 kernel 关掉、且不做架构校验」。`GemmOp` 正是用这个机制实现「非 SM90 设备上不装 GEMV」——下一节细讲。

#### 4.3.3 源码精读

先看 `GemmOp.default_kernel_map` 本身——它示范了「条件注册 + override 基准」两种用法。[tileops/ops/gemm.py:L68-L74](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/gemm.py#L68-L74)：

```python
@property
def default_kernel_map(self) -> Dict[str, Kernel]:
    kernels: Dict[str, Kernel] = {"gemm_kernel": GemmKernel}
    # GemvKernel is SM90-only; only advertise it where it can install.
    if get_sm_version() in (GemvKernel.supported_archs or []):
        kernels["gemv_kernel"] = GemvKernel
    return kernels
```

这段代码做了两件事：

1. **无条件注册 `gemm_kernel → GemmKernel`**：通用矩阵乘路径，始终可用（前提是架构匹配，下面 `_install_kernel_map` 会校验）。
2. **条件注册 `gemv_kernel → GemvKernel`**：只有当 `get_sm_version() in GemvKernel.supported_archs`（即当前设备是 SM90）时，才把 GEMV 快路径 kernel 放进登记表。

**为什么 `GemvKernel` 只在 SM90 注册？** 因为 `GemvKernel` 依赖 Hopper 特有的数据搬运与归约原语。看它的默认配置 [tileops/kernels/gemm.py:L698-L716](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/kernels/gemm.py#L698-L716)：

```python
@property
def default_config(self) -> dict:
    sm_version = get_sm_version()
    if sm_version in {90}:
        # reduce_threads=32: full warp per row → coalesced B access + warp shuffle reduce
        # block_n=8: 256 threads/block, 448 blocks for n=7168 → ~3.4 blocks/SM on H200
        # num_stages=2: double-buffer B tile to hide HBM3e latency
        return {"block_n": 8, "reduce_threads": 32, "num_stages": 2}
    return {"block_n": 32, "reduce_threads": 32, "num_stages": 1}
```

注释里 `H200`、`double-buffer B tile to hide HBM3e latency`、`warp shuffle reduce` 都指向 Hopper 世代的内存子系统与 warp 级原语。这些调参是为 H200/HBM3e 量身定的，所以类声明 `supported_archs = [90]` 把它锁在 Hopper。

> **诚实补充**：由于 `GemmKernel` 同样是 `[90]`，且在字典中先被遍历，非 SM90 机器构造 `GemmOp` 时其实会**先**在 `GemmKernel` 处抛错。因此 `default_kernel_map` 里对 `GemvKernel` 的这层 `if` 守卫，在当前实现下是**防御性 + 表意性**的：它确保登记表永不「宣称」一个无法安装的 kernel，并清晰传达「GEMV 是 SM90 专属」的意图。如果将来 `GemmKernel` 被放宽到更多架构，这层守卫就会真正生效——届时 GEMM 能装、GEMV 在非 SM90 上自动缺席。

然后看运行时怎么用这张表。[tileops/ops/gemm.py:L93-L119](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/gemm.py#L93-L119) 的 `_get_kernel` 是运行时按 dispatch_key 取 kernel 的地方，其中 GEMV 快路径这段最关键：

```python
gemv_cls = self.kernel_map.get("gemv_kernel")
if (gemv_lhs_row or gemv_rhs_col) and gemv_cls is not None:
    ...
    kernel = gemv_cls(n if mode == "lhs_row" else m, k, dtype, tune=self._tune)
    ...
    return mode, kernel
```

注意 `self.kernel_map.get("gemv_kernel")`：用的是 `.get()` 而不是 `self.kernel_map["gemv_kernel"]`。因为非 SM90 设备上 `default_kernel_map` 根本没注册这个 key，`.get()` 会安全地返回 `None`，随后 `gemv_cls is not None` 判断为假，自动回退到通用 `GemmKernel` 路径。这就是「条件注册」与「运行时 `.get()`」配合形成的优雅降级——**两个地方写法一致，才不会在非 SM90 上 KeyError**。

至于 override 入口怎么落到 `_install_kernel_map`：用户 `GemmOp(kernel_map={"gemm_kernel": MyGemm})` → `__init__` 把它原样传给 `dispatch_kernel(kernel_map)` → `_install_kernel_map(kernel_map)`。后者在循环里看到 `candidate_map` 含 `"gemm_kernel"`，于是 `kernel_type = candidate_map["gemm_kernel"]`（即 `MyGemm`）替代默认的 `GemmKernel`，然后照样走架构校验。**只要 `MyGemm` 自己声明了正确的 `supported_archs`（或留 `None` 表示不限），就能成功安装**。

#### 4.3.4 代码实践（可在 GPU 机器运行）

**实践目标**：用自定义 `kernel_map` 替换 `GemmOp` 的默认 `gemm_kernel`，观察校验与安装行为。

**操作步骤**：

1. 写一段最小脚本（**示例代码**，非项目原有代码）：

   ```python
   # 示例代码：override 默认 gemm_kernel
   import torch
   from tileops.ops import GemmOp
   from tileops.kernels.kernel_base import Kernel

   # 一个「假」的自定义 kernel 类，仅用于观察安装行为
   class MyGemm(Kernel):
       supported_archs = None        # None = 不限架构，校验放行
       def forward(self, *a, **k):
           raise RuntimeError("MyGemm.forward not implemented")

   # 用它覆盖 gemm_kernel 这个 dispatch_key
   op = GemmOp(kernel_map={"gemm_kernel": MyGemm})
   print("resolved kernel_map:", op.kernel_map)
   ```

2. 把 `MyGemm.supported_archs` 改成 `[80]`，在 SM90 机器上再跑一次构造。

**需要观察的现象**：
- 第 1 步：`op.kernel_map` 应为 `{'gemm_kernel': <class 'MyGemm'>, 'gemv_kernel': <class 'GemvKernel'>}`（后者仅在 SM90 机器出现），且**不抛错**，因为 `MyGemm.supported_archs = None`。
- 第 2 步：构造时应抛 `ValueError: MyGemm is not supported on architecture 90`。

**预期结果**：验证了两个结论——(a) override 是逐 key 替换，键名必须与 `default_kernel_map` 一致；(b) 用户提供的 kernel 类同样受 `supported_archs` 校验约束，None 表示放行。

**说明**：本实践需要 CUDA GPU（`get_sm_version()` 会调 `torch.cuda.get_device_capability()`）。若无 GPU，可退化为源码阅读型实践：在 `_install_kernel_map` 的 `for` 循环里手动推演 `candidate_map={"gemm_kernel": MyGemm}`、`MyGemm.supported_archs=[80]`、`current_arch=90` 时的判断路径，确认会命中 `raise ValueError`。**待本地验证**：在不同 SM 版本机器上复现上述两种现象。

#### 4.3.5 小练习与答案

**练习 1**：用户写 `GemmOp(kernel_map={"gemv_kernel": MyGemv})`，但当前设备是非 SM90。这个 override 会生效吗？

> **参考答案**：不会。非 SM90 上 `default_kernel_map` 根本不含 `"gemv_kernel"` 这个 key（被 `if get_sm_version() in ...` 挡掉了）。而 `_install_kernel_map` 遍历的是默认表的 key，用户表里多出的 `"gemv_kernel"` 不在遍历范围，会被**忽略**，最终 `self.kernel_map` 里没有 `gemv_kernel`。结论：override 只能替换默认表里**已存在**的 key。

**练习 2**：把某个 dispatch_key 覆盖成 `None`（即 `kernel_map={"gemv_kernel": None}`）有什么效果？

> **参考答案**：在 `_install_kernel_map` 里，`kernel_type = candidate_map["gemv_kernel"]` 取到 `None`；接着架构校验条件的首段 `kernel_type is not None` 为假，整个 `if` 不成立，**不抛错**，把 `None` 写入 `resolved["gemv_kernel"]`。运行时 `self.kernel_map.get("gemv_kernel")` 取到 `None`，`gemv_cls is not None` 判断为假，自动回退到通用 kernel 路径。这是一种「显式关闭某条快路径」的写法。

---

## 5. 综合实践

把本讲三个最小模块串成一个综合任务：**为 `GemmOp` 画一张「从构造到 kernel 选择」的全景图，并解释每一处架构相关的分支**。

任务步骤：

1. **读 `default_kernel_map`**（[gemm.py:L68-L74](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/gemm.py#L68-L74)）：在图上标出两个条目——`gemm_kernel`（恒注册）与 `gemv_kernel`（仅 SM90 注册）。
2. **读 `_install_kernel_map`**（[op_base.py:L157-L190](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/op_base.py#L157-L190)）：画出「override 选择 → supported_archs 校验 → 写回 self.kernel_map」三步。
3. **读 `_get_kernel`**（[gemm.py:L93-L119](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/gemm.py#L93-L119)）：标出运行时 `self.kernel_map.get("gemv_kernel")` 的快路径与回退到 `self.kernel_map["gemm_kernel"]` 的慢路径。
4. **回答两个问题**：
   - 为什么 `default_kernel_map` 里的 `if` 守卫与 `_get_kernel` 里的 `.get()` 必须同时存在？删掉其中一个会怎样？
   - 假设你要给 `GemmOp` 接入一个第三方 `FlashGemmKernel`（声明 `supported_archs=[90]`），写出最小 override 代码，并预测在 SM89 机器上的构造结果。

**参考答案要点**：
- 问题 a：两者一起保证「登记表」与「运行时取用」对 `gemv_kernel` 的存在性认知一致。若只留 `default_kernel_map` 的 `if`、却把 `_get_kernel` 改成 `self.kernel_map["gemv_kernel"]`，非 SM90 设备上会 `KeyError`；若只留 `.get()`、却让 `default_kernel_map` 无条件注册 `GemvKernel`，则非 SM90 设备上会在 `_install_kernel_map` 抛 `ValueError`（GEMV 装不上）。当前写法是「登记期就别让它进表」，最干净。
- 问题 b：`op = GemmOp(kernel_map={"gemm_kernel": FlashGemmKernel})`。SM89 上构造会抛 `ValueError: FlashGemmKernel is not supported on architecture 89`。

## 6. 本讲小结

- `_install_kernel_map` 是 Op 安装 kernel 的**唯一落点**：它合并默认表与用户覆盖表、对每个解析出的 kernel 类做架构校验、最后写回 `self.kernel_map`。默认与覆盖走**同一段**校验代码，避免不对称。
- `supported_archs` 是 `Kernel` 的类属性：`None` 表示不限架构、直接放行；列表（如 `[90]`）表示只能在这些 SM 版本运行，不匹配即在**构造期**抛 `ValueError`。
- 架构检查发生在「kernel 类」层面（尚未实例化），时机早于 `forward`；`get_sm_version()` 用 `major*10+minor` 把设备能力压成整数（Hopper=90）。
- `default_kernel_map` 既是抽象属性（强制子类实现），也是 override 的**基准**：用户 `kernel_map` 只能逐 key **替换**默认表里已有的条目，不能新增。
- `GemmOp.default_kernel_map` 示范了「条件注册」：`GemvKernel` 仅在 SM90 才进登记表，配合运行时 `self.kernel_map.get(...)` 的 `.get()` 写法，形成优雅的「非 SM90 自动回退」降级。
- 复合 Op（`default_kernel_map` 为空）走另一分支：原样存用户表，架构校验下放给各子 Op——这是为 attention / MoE 这类编排多个子 Op 的家族留的口子。

## 7. 下一步学习建议

本讲聚焦「选哪个 kernel 类、能不能在当前架构装上」。一旦选定，接下来就是「这个 kernel 类怎么被实例化、缓存、并在 `forward` 里复用」。建议继续：

- **u2-l3《形状推断与 Kernel 缓存》**：`_cache_key` 与 `_static_axes` 如何决定哪些调用共用同一份已编译 kernel，与本文 `_install_kernel_map` 的「登记表」是互补的两层——一层管「有哪些 kernel 类」，一层管「运行时复用哪份实例」。
- **u2-l4《跟读 GemmOp 完整链路》**：把本讲的 `_get_kernel` 放进 `forward` 全流程里，看 GEMV 快路径与 GEMM 主路径如何根据 `(m, n, trans_a, trans_b)` 在运行时切换。
- 想了解「为什么有些 Op 没有平坦的 kernel_map、而是走复合分支」：可先翻 [docs/design/ops-design.md](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/docs/design/ops-design.md) 的 Family-Base 一节（u11-l1 会专门讲）。
