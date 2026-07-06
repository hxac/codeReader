# FA2 与 FA4 接口对比与共存机制

## 1. 本讲目标

学完本讲后，你应当能够：

- 说出 `import flash_attn` 和 `import flash_attn.cute` 为什么能同时指向**两套不同的实现**，而彼此不冲突。
- 区分 FA2 与 FA4 各自的 `flash_attn_func`：它们来自哪个文件、底层内核用什么写成、参数和返回值有什么不同。
- 在拿到一块 GPU、一个场景时，判断应该调用 `flash_attn.flash_attn_func` 还是 `flash_attn.cute.flash_attn_func`。

本讲是入门层的收尾：u1-l2 讲了「仓库里三代代码放在哪」，u1-l3 讲了「怎么装好、第一次跑通 FA4」，本讲把它们串起来——**为什么这两个名字几乎一样的函数能并存，且我们必须分清楚该用哪一个**。

## 2. 前置知识

阅读本讲前，你应已掌握（来自前置讲义）：

- **SDPA 与 FlashAttention 的直觉**（u1-l1）：注意力即 softmax(QKᵀ/√d)V，FlashAttention 靠 tiling + 在线 softmax 避免实例化 N×N 矩阵。
- **仓库三代共存**（u1-l2）：FA2 在顶层 `flash_attn/`+`csrc/`（C++/CUDA，包名 `flash-attn`），FA4 在 `flash_attn/cute/`（Python+CuTeDSL，包名 `flash-attn-4`），靠 `setup.py` 排除 + `pyproject.toml` 声明 + `extend_path` 三件套共存。
- **FA4 的最小调用与返回值**（u1-l3）：FA4 的 `flash_attn_func` **恒返回元组 `(out, lse)`**，输入布局固定为 `(batch, seqlen, nheads, head_dim)`。

下面三个名词是本讲的关键，先做一个一句话定义：

| 名词 | 一句话解释 |
|------|-----------|
| 命名空间包（namespace package）| 一个包名 `flash_attn` 的代码可以**分散在多个目录**里，Python 在 import 时把它们拼成一个逻辑包 |
| `extend_path` | `pkgutil` 提供的函数，把磁盘上所有名为 `flash_attn` 的子目录追加到该包的 `__path__` 搜索列表 |
| `flash_attn_2_cuda` | FA2 在安装期用 `nvcc` 编译出的 C++/CUDA 扩展模块，是 FA2 内核的真正入口 |

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [`flash_attn/__init__.py`](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/__init__.py) | FA2 包的入口：用 `extend_path` 把自己变成命名空间包，再 re-export FA2 的接口 |
| [`flash_attn/flash_attn_interface.py`](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/flash_attn_interface.py) | FA2 的 Python 接口层：定义 `flash_attn_func` 等，底层调用 `flash_attn_2_cuda` 编译扩展 |
| [`flash_attn/cute/interface.py`](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py) | FA4 的接口层：定义另一套 `flash_attn_func`，按 GPU 架构分发到 CuTeDSL kernel 类 |
| [`flash_attn/cute/__init__.py`](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/__init__.py) | FA4 子包入口：仅导出 `flash_attn_func` 与 `flash_attn_varlen_func` 两个名字 |

## 4. 核心概念与源码讲解

### 4.1 extend_path 命名空间包机制

#### 4.1.1 概念说明

u1-l2 已经告诉你：FA2 和 FA4 是**两个独立的 wheel**，装到同一个 site-packages 里却都能用 `flash_attn` 这个名字访问。本讲深入它**在 import 时刻到底发生了什么**。

关键问题：Python 怎么允许「两个包共用 `flash_attn` 这个名字却互不覆盖」？

答案是**命名空间包（namespace package）**。普通包（regular package）的代码只放在一个目录里，那个目录里的 `__init__.py` 就是这个包的全部入口。命名空间包不同：**同一个包名可以对应磁盘上多个目录**，Python 在 `import` 时把这些目录「拼」成一个逻辑包。

`flash_attn` 采用的是 `pkgutil` 风格的命名空间包（PEP 382 时代的显式做法），靠一行 `extend_path` 实现。

#### 4.1.2 核心流程

理解 `extend_path`，需要先理解普通包的 `__path__`：

- 对普通包，`flash_attn.__path__` 是一个只含**一个目录**的列表，例如 `['/site-packages/flash_attn']`。
- 当你 `import flash_attn.cute` 时，Python 会遍历 `flash_attn.__path__` 里的每个目录，寻找名为 `cute` 的子目录。

`extend_path(__path__, __name__)` 的作用是：**扫描 `sys.path` 上所有位置，把每一个「含名为 `flash_attn` 子目录」的路径追加到 `__path__` 里**。于是 `__path__` 从单元素列表变成多元素列表。

把它套到本仓库，import 时刻的流程是：

```text
1. import flash_attn
   → Python 在 site-packages 找到 FA2 的 flash_attn/__init__.py 并执行
   → 该 __init__ 执行 __path__ = extend_path(__path__, __name__)

2. extend_path 扫描 sys.path，发现 FA4 的 wheel 也安装了一个 flash_attn/
   （里面只含 cute/ 子目录），把这个目录追加到 __path__

3. 现在 flash_attn.__path__ = [FA2的目录, FA4的目录]

4. import flash_attn.cute
   → Python 遍历 __path__，在 FA2 目录里没找到 cute/，
     在 FA4 目录里找到 cute/ → 导入 FA4 的 cute 子包
```

为什么不会冲突？因为分工明确：

- **FA2 的 wheel** 负责提供 `flash_attn/__init__.py`（也就是「谁是 `flash_attn`」的定义权）。
- **FA4 的 wheel** 只提供 `flash_attn/cute/` 子目录，**不**提供 `flash_attn/__init__.py`。

两者在文件层面完全不重叠，所以谁也不会覆盖谁。FA4 的 `pyproject.toml` 显式声明 `packages = ["flash_attn.cute"]`、`package-dir = {"flash_attn.cute" = "."}`（见 [flash_attn/cute/pyproject.toml:46-48](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/pyproject.toml#L46-L48)），说明它只把自己装成 `flash_attn.cute` 这一个子包；而 FA2 的 `setup.py` 在打包时用 `find_packages` 主动排除了 `flash_attn.cute`（见 [setup.py:760-772](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/setup.py#L760-L772)），避免把 FA4 的代码也打进 FA2 的 wheel。

#### 4.1.3 源码精读

整套机制的运行时「开关」就在 FA2 包入口的前 4 行：

[flash_attn/__init__.py:1-6](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/__init__.py#L1-L6) —— 这几行先用 `extend_path` 把 `flash_attn` 升级为命名空间包，注释直接点明了目的：「让 FA2 和 FA4 能共存安装」。

```python
from pkgutil import extend_path
# look for every subdir with flash_attn base name such that fa2 and fa4 can be co-installed
__path__ = extend_path(__path__, __name__)
```

注意第 4 行：`extend_path` 的返回值重新赋给 `__path__`，这是 `pkgutil` 风格命名空间包的固定写法。没有这一行，`flash_attn.cute` 就 import 不进来。

紧接着 [flash_attn/__init__.py:8-16](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/__init__.py#L8-L16) 从 FA2 自己的接口模块里 re-export 了 `flash_attn_func` 等一串名字：

```python
from flash_attn.flash_attn_interface import (
    flash_attn_func,
    flash_attn_kvpacked_func,
    ...
)
```

也就是说：当你写 `from flash_attn import flash_attn_func`，拿到的是 **FA2** 的实现；当你写 `from flash_attn.cute import flash_attn_func`，拿到的是 **FA4** 的实现。**同名函数，两个来源**——这正是本讲要厘清的核心。

对照 FA4 子包入口 [flash_attn/cute/__init__.py:10-18](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/__init__.py#L10-L18)，它只导出两个名字，且来自 FA4 自己的 `interface`：

```python
from .interface import (
    flash_attn_func,
    flash_attn_varlen_func,
)
```

两份 `__init__.py` 各自只 re-export 自己实现里的 `flash_attn_func`，互不引用对方，这就是「同名不同实」在源码层的落点。

#### 4.1.4 代码实践

**实践目标**：亲手看到 `extend_path` 把 `flash_attn.__path__` 变成多目录列表。

**操作步骤**：

1. 确认同时装了 `flash-attn`（FA2）和 `flash-attn-4`（FA4）：`pip list | grep flash-attn`。
2. 运行下面这段脚本（命名为 `inspect_path.py`）：

```python
import flash_attn                      # 先触发 FA2 的 __init__，从而执行 extend_path
print("flash_attn.__path__ =", list(flash_attn.__path__))
import flash_attn.cute                 # 再 import FA4 子包
print("flash_attn.cute.__file__ =", flash_attn.cute.__file__)
```

**需要观察的现象**：`flash_attn.__path__` 应当打印出**多于一个**目录路径（至少包含 FA2 的安装目录；若 FA4 正确共存，还应包含 FA4 贡献 `cute/` 的那个目录）。

**预期结果**：`__path__` 是一个列表，元素个数 ≥ 1；`flash_attn.cute.__file__` 指向 FA4 的 `__init__.py`。

> 若本机只装了其中一代，`__path__` 就只有单元素、`flash_attn.cute` 可能 import 失败——这本身也验证了机制。**待本地验证**：在装齐两代的环境里确认列表含两个不同目录。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `flash_attn/__init__.py` 里的 `__path__ = extend_path(...)` 这行删掉，会发生什么？

**参考答案**：`flash_attn` 退化为普通包，`__path__` 只含 FA2 自己的目录；`import flash_attn.cute` 会找不到 FA4 的 `cute/` 子目录而抛 `ModuleNotFoundError`（除非 FA4 恰好装在 FA2 目录内部）。

**练习 2**：为什么 FA4 的 wheel 不能也带一个 `flash_attn/__init__.py`？

**参考答案**：两个 wheel 各自带 `flash_attn/__init__.py` 会在安装时互相覆盖，且 import 时只能执行其中一份，另一份的初始化逻辑就丢了。FA4 主动只装 `flash_attn/cute/` 子目录、把 `__init__.py` 的「定义权」让给 FA2，是共存的前提。

---

### 4.2 两套接口的来源差异

#### 4.2.1 概念说明

虽然名字都叫 `flash_attn_func`，但 FA2 和 FA4 的 `flash_attn_func` 在「内核用什么写成、何时编译、能跑在哪些 GPU 上」这三点上完全不同。下表是核心对比：

| 维度 | FA2 `flash_attn.flash_attn_func` | FA4 `flash_attn.cute.flash_attn_func` |
|------|----------------------------------|----------------------------------------|
| 内核语言 | C++/CUDA（手写 kernel） | Python + CuTeDSL |
| 编译时机 | `pip install` 时用 `nvcc` 预编译 | 首次调用时 JIT 编译为 PTX/CUBIN |
| 入口模块 | `flash_attn_2_cuda`（pybind 扩展） | CuTeDSL kernel 类 |
| 目标架构 | 主要优化 Ampere（SM80），支持到 SM90 | SM80/SM90/SM100/SM110/SM120 |
| 默认返回值 | 只返回 `out` | 返回 `(out, lse)` 元组 |
| 是否有 dropout | 有 `dropout_p` 参数 | 无 `dropout_p` |

数学上两者都是**精确注意力** softmax(QKᵀ/√d)V，结果只差 fp16/bf16 的舍入误差——区别全在「怎么算」，不在「算什么」。

#### 4.2.2 核心流程

**FA2 的调用链**（短而直接）：

```text
flash_attn_func(...)                      # Python 接口
  → FlashAttnFunc.apply(...)              # torch.autograd.Function
    → _wrapped_flash_attn_forward(...)
      → _flash_attn_forward(...)
        → flash_attn_gpu.fwd(q, k, v, ...)   # 直接进 C++ 扩展，没有架构分发
```

FA2 在 Python 层几乎不做架构选择：选哪份 CUDA kernel 是在 C++ 扩展内部按 `device_capability` 决定的，Python 侧只负责传参和 shape 处理。

**FA4 的调用链**（多一步显式架构分发）：

```text
flash_attn_func(...)                      # Python 接口（flash_attn.cute）
  → FlashAttnFunc.apply(...)              # 另一个 torch.autograd.Function
    → _flash_attn_fwd(...)
      → arch = _get_device_arch()         # 读 GPU 架构（可被环境变量覆盖）
      → 按 arch 选 kernel 类:
           arch//10 == 8  → FlashAttentionForwardSm80
           arch//10 == 9  → FlashAttentionForwardSm90
           arch//10 in [10,11] → FlashAttentionForwardSm100（或 hd256 专用 kernel）
           arch//10 == 12 → FlashAttentionForwardSm120
      → 实例化 kernel 类 → CuTeDSL JIT 编译 → 执行
```

FA4 把架构分发**显式写在 Python 里**，因为不同架构用不同的 kernel 类、不同的 tile 配置，且整个 kernel 都是 Python 源码，便于阅读和修改（这也是本手册以 FA4 为主线的原因）。

#### 4.2.3 源码精读

**FA2 这一边：** 接口文件顶部就把 C++ 扩展 import 进来当 `flash_attn_gpu` 用：

[flash_attn/flash_attn_interface.py:12-23](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/flash_attn_interface.py#L12-L23) —— 注意第 15、23 行：FA2 的内核就是 `flash_attn_2_cuda` 这个编译扩展（AMD ROCm 走另一条 Triton 路径）。

```python
import flash_attn_2_cuda as flash_attn_gpu
```

[flash_attn/flash_attn_interface.py:99-113](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/flash_attn_interface.py#L99-L113) —— 真正的前向只是一次对 `flash_attn_gpu.fwd(...)` 的调用，参数原样透传给 C++ 内核：

```python
out, softmax_lse, S_dmask, rng_state = flash_attn_gpu.fwd(q, k, v, None, alibi_slopes, ...)
```

FA2 的 `flash_attn_func` 签名见 [flash_attn/flash_attn_interface.py:1156-1168](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/flash_attn_interface.py#L1156-L1168)，注意它带 `dropout_p`、`alibi_slopes`、`return_attn_probs` 这些 FA4 没有的参数。其 autograd 类 [FlashAttnFunc at flash_attn/flash_attn_interface.py:828](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/flash_attn_interface.py#L828) 的 forward 在 [第 878 行](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/flash_attn_interface.py#L878) 默认**只返回 `out`**：

```python
return out if not return_softmax else (out, softmax_lse, S_dmask)
```

**FA4 这一边：** 架构探测函数 `_get_device_arch` 是分发枢纽：

[flash_attn/cute/interface.py:76-92](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L76-L92) —— 它先看环境变量 `FLASH_ATTENTION_ARCH`，没有才去查真实 GPU 的 `get_device_capability()`，返回一个整数（如 80、90、100）。

拿到 `arch` 后，[flash_attn/cute/interface.py:446-447](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L446-L447) 做合法性断言，随后进入一段 `if arch // 10 == 8 / 9 / [10,11]` 的分支，挑出对应的 CuTeDSL kernel 类（节选见 [flash_attn/cute/interface.py:823-913](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L823-L913)）：

```python
if arch // 10 == 8:
    fa_fwd = FlashAttentionForwardSm80(...)        # Ampere
elif arch // 10 == 9:
    fa_fwd = FlashAttentionForwardSm90(...)        # Hopper
elif arch // 10 in [10, 11]:
    fa_fwd = FlashAttentionForwardSm100(...)       # Blackwell
```

FA4 的 `flash_attn_func` 签名见 [flash_attn/cute/interface.py:2709-2731](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L2709-L2731)：参数里有 FA2 没有的 `score_mod`、`mask_mod`、`num_splits`、`pack_gqa`、`block_sparse_tensors`、`return_lse` 等，但**没有 `dropout_p`**。其 autograd 类 [FlashAttnFunc at flash_attn/cute/interface.py:2419](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L2419) 的 forward 在 [第 2488 行](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L2488) **恒返回 `(out, lse)` 元组**：

```python
return out, lse
```

> 这是迁移代码时最常踩的坑：把 `out = flash_attn_func(q,k,v)` 从 FA2 换成 FA4，`out` 会变成一个 `(out, lse)` 元组，后续 `out.shape` 会报错。FA4 必须写成 `out, lse = flash_attn_func(q,k,v)`。

#### 4.2.4 代码实践

**实践目标**：用 Python 自省工具确认两套 `flash_attn_func` 确实来自不同文件、不同模块。

**操作步骤**：

```python
# inspect_fa.py
import inspect
from flash_attn import flash_attn_func as fa2_fn
from flash_attn.cute import flash_attn_func as fa4_fn

print("FA2 module :", fa2_fn.__module__)
print("FA2 file   :", inspect.getfile(fa2_fn))
print("FA4 module :", fa4_fn.__module__)
print("FA4 file   :", inspect.getfile(fa4_fn))
print("是同一个函数吗？", fa2_fn is fa4_fn)
```

**需要观察的现象**：

- `fa2_fn.__module__` 形如 `flash_attn.flash_attn_interface`，文件指向 `flash_attn_interface.py`。
- `fa4_fn.__module__` 形如 `flash_attn.cute.interface`，文件指向 `cute/interface.py`。
- `fa2_fn is fa4_fn` 为 `False`。

**预期结果**：两行的文件路径分属两个不同的 `.py`，证明同名函数来自两套实现。**待本地验证**：若 FA2 的 C++ 扩展未编译成功，`from flash_attn import ...` 可能在 import 阶段就报 `flash_attn_2_cuda` 找不到，这本身也说明 FA2 依赖安装期编译。

#### 4.2.5 小练习与答案

**练习 1**：FA2 的内核在哪个阶段被编译成机器码？FA4 呢？

**参考答案**：FA2 在 `pip install` 阶段由 `nvcc` 编译成 `.so`（即 `flash_attn_2_cuda`），import 时直接加载；FA4 在**首次调用** `flash_attn_func` 时由 CuTeDSL JIT 编译为 PTX/CUBIN，之后命中缓存。

**练习 2**：下面这段从 FA2 迁到 FA4 的代码有什么错误？
```python
out = flash_attn_func(q, k, v, causal=True)   # 改成 import 自 flash_attn.cute
print(out.shape)
```

**参考答案**：FA4 的 `flash_attn_func` 恒返回 `(out, lse)`，所以 `out` 实际是元组，`out.shape` 会报 `AttributeError: 'tuple' object has no attribute 'shape'`。应改为 `out, lse = flash_attn_func(q, k, v, causal=True)`。

---

### 4.3 选型建议：什么时候用哪一代

#### 4.3.1 概念说明

既然两套都能算精确注意力，实际项目里该用哪个？决策依据主要是**硬件架构**和**功能需求**两点。

- **按硬件**：FA2 的 CUDA kernel 主要为 Ampere（A100，SM80）优化，也能在 Hopper（SM90）上跑；但它**没有**针对 Blackwell（SM100/SM110）优化。FA4 则原生覆盖 SM80/SM90/SM100/SM110/SM120，是 Blackwell 上的首选。
- **按功能**：FA4 支持 FA2 没有或较弱的一批新特性——可编程 `score_mod`/`mask_mod`、SplitKV（长上下文/解码）、分页 KV cache、块稀疏、MLA、fp8 前向等。如果你需要这些，只能用 FA4。
- **按稳定性/成熟度**：FA2 历史最久、部署最广、依赖最简单（一个编译好的 `.so`）；FA4 仍在快速迭代（`pyproject.toml` 里 `Development Status :: 3 - Alpha`），首次调用有 JIT 编译开销。

#### 4.3.2 核心流程

选型可以归结为一棵简单决策树：

```text
你的 GPU 是什么架构？
├─ Blackwell (SM100/SM110/SM120, 如 B200/B300)
│      → 必选 FA4（flash_attn.cute），FA2 无优化路径
├─ Hopper (SM90, 如 H100/H200)
│      → 两者皆可；需要 score_mod/SplitKV/paged/MLA → FA4
│        否则追求最小依赖、最稳 → FA2
└─ Ampere (SM80, 如 A100) 或更早
       → FA2 成熟稳定；FA4 也支持 SM80 可作学习/实验用
```

一个常被忽略的细节：FA4 允许用环境变量 `FLASH_ATTENTION_ARCH` **强制指定**架构路径（见 4.2.3 的 `_get_device_arch`）。这意味着即使你只有一块 H100，也能让 FA4 跑它的 SM80（Ampere）kernel 来对比学习——这正是后续讲义（如 u6 前向 kernel 对比）会反复用到的技巧。

#### 4.3.3 源码精读

选型的「硬件边界」在 FA4 接口里被一行断言明确写死：

[flash_attn/cute/interface.py:446-447](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L446-L447) —— FA4 支持的架构范围一目了然：

```python
arch = _get_device_arch() if _arch is None else _arch
assert arch // 10 in [8, 9, 10, 11, 12], "Unsupported compute capability. Supported: 8.x, 9.x, 10.x, 11.x, 12.x"
```

而 FA4 的「功能广度」可以从它接口里那些 FA2 没有的参数看出，[flash_attn/cute/interface.py:2709-2731](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L2709-L2731) 中的 `score_mod`、`mask_mod`、`num_splits`、`pack_gqa`、`block_sparse_tensors`、`gather_kv_indices` 等参数，分别对应本手册后面会专门讲的 score_mod（u4）、SplitKV（u7）、pack_gqa（u7）、块稀疏（u10）、top-k gather（u10）等特性——这些是选 FA4 的理由清单。

反过来，FA2 接口 [flash_attn/flash_attn_interface.py:1156-1168](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/flash_attn_interface.py#L1156-L1168) 里的 `dropout_p`、`alibi_slopes`、`return_attn_probs` 则是 FA2 的「老接口」特征：它用专门的 `alibi_slopes` 张量做 ALiBi，而 FA4 把这类位置偏置统一收进 `score_mod` 回调（u4-l2 会详讲）。

#### 4.3.4 代码实践

**实践目标**：在同一组输入上同时跑 FA2 与 FA4，验证它们数学等价（仅差 fp 舍入），并亲历「返回值不同」这个坑。

**操作步骤**：

```python
# compare_fa2_fa4.py
import torch
from flash_attn import flash_attn_func as fa2        # FA2
from flash_attn.cute import flash_attn_func as fa4   # FA4

torch.manual_seed(0)
b, s, h, d = 2, 512, 8, 64
q = torch.randn(b, s, h, d, dtype=torch.float16, device="cuda")
k = torch.randn_like(q); v = torch.randn_like(q)

# FA2：默认只返回 out
out2 = fa2(q, k, v, causal=True)
# FA4：恒返回 (out, lse)
out4, lse4 = fa4(q, k, v, causal=True)

print("FA2 out type :", type(out2), getattr(out2, "shape", None))
print("FA4 out type :", type((out4, lse4)), out4.shape)
print("max abs diff:", (out2.float() - out4.float()).abs().max().item())
```

**需要观察的现象**：

- FA2 的 `out2` 直接是张量；FA4 的 `out4` 才是张量，`lse4` 是 `(b, h, s)` 的 float32 log-sum-exp。
- 两者输出的最大绝对差应在 fp16 舍入量级（约 1e-2 ~ 1e-3 量级，视数值范围而定）。

**预期结果**：差值很小，证明两套实现算的是同一个精确注意力。**待本地验证**：若机器非 Ampere/Hopper，FA2 可能无法运行；若非 SM80/90/100/110/120，FA4 会命中 4.3.3 的 assert 报错。

#### 4.3.5 小练习与答案

**练习 1**：你在 B200（SM100）上训练，应该用哪一代？为什么？

**参考答案**：FA4。FA2 的 CUDA kernel 没有为 Blackwell 优化，而 FA4 原生提供 `FlashAttentionForwardSm100`（UMMA/tmem/persistent kernel），是 SM100 上的最优路径。

**练习 2**：你想在注意力打分上加一个自定义的相对位置偏置，FA2 和 FA4 哪个更方便？

**参考答案**：FA4 更方便——把偏置写成一个 `score_mod` 回调传入即可（u4-l2 详讲）；FA2 只能用预定义的 `alibi_slopes`，自定义偏置需要改 C++ 源码重编译，成本高得多。

---

## 5. 综合实践

把本讲三个最小模块串成一个「FA2 vs FA4 体检脚本」。请编写 `fa_coexistence_audit.py`，完成以下任务，并把结论写成几行注释：

1. **命名空间验证**：`import flash_attn`，打印 `list(flash_attn.__path__)`，确认它含多个目录；再 `import flash_attn.cute`，打印其 `__file__`。
2. **来源区分**：用 `inspect.getfile` 分别打印 FA2 与 FA4 的 `flash_attn_func` 所在文件，确认两者不同。
3. **行为对比**：在同一组 fp16 `(2, 512, 8, 64)` 输入上分别调用两者（`causal=True`），打印：
   - FA2 返回值的类型与形状；
   - FA4 返回元组中 `out` 与 `lse` 的形状；
   - 两者 `out` 的最大绝对差。
4. **架构探测**：读取并打印 `flash_attn.cute.interface._get_device_arch()`（可用 `FLASH_ATTENTION_ARCH=sm_80` 覆盖后再跑一次），体会 FA4 的显式架构分发。

把脚本输出整理成一张小表：行是「命名空间 / 来源 / 返回值 / 数值差 / 架构」，列是「FA2 / FA4」。这张表就是你判断「该用哪一个」的速查卡。

> 注意事项：本实践需要 CUDA GPU 与同时安装好的两代包；若环境不全，至少完成第 1、2 步的自省部分（不需 GPU），并对第 3 步标注「待本地验证」。

## 6. 本讲小结

- `flash_attn/__init__.py` 用 `__path__ = extend_path(__path__, __name__)` 把 `flash_attn` 变成命名空间包，使 FA2 与 FA4 的代码能分居不同目录却共用一个包名。
- `from flash_attn import flash_attn_func` 拿到的是 **FA2**（C++/CUDA，安装期 `nvcc` 编译）；`from flash_attn.cute import flash_attn_func` 拿到的是 **FA4**（CuTeDSL，首次调用 JIT 编译）。
- FA2 默认只返回 `out`；FA4 恒返回 `(out, lse)` 元组——这是迁移代码时最常见的坑。
- FA4 在 Python 层用 `_get_device_arch()` 显式按 SM80/90/100/110/120 分发到不同 kernel 类；FA2 的架构选择藏在 C++ 扩展内部。
- 选型主依据：Blackwell 选 FA4；Hopper 看功能（需要 score_mod/SplitKV/paged/MLA 选 FA4，否则可 FA2）；Ampere 两者皆可，FA2 更成熟。
- FA4 的 `FLASH_ATTENTION_ARCH` 环境变量可强制覆盖架构选择，是后续学习/对比 kernel 时常用的开关。

## 7. 下一步学习建议

本讲是入门层（第 1 单元）的最后一篇，你已经能分清两代接口并跑通 FA4。接下来进入第 2 单元「公共接口与架构分发」：

- **u2-l1 公共 API 详解**：逐项精读 FA4 `flash_attn_func` 的全部参数与返回的 `lse`，理解哪些参数会触发 kernel 重编译。
- **u2-l2 架构分发与 tile 配置**：深入 `_get_device_arch`、`_validate_head_dims`、`FwdConfig` 与 tile 尺寸选择——本讲 4.3 提到的架构分发，在那里会完整展开。

如果你更想先看「FA4 到底怎么算的」，也可以先跳到第 4 单元（在线 softmax）和第 6 单元（前向主循环），但建议先过 u2，把接口契约和架构分发吃透，再看 kernel 内部会更顺。
