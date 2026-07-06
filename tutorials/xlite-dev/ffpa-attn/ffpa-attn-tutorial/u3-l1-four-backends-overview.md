# 四后端总览与选型矩阵

## 1. 本讲目标

前几讲我们一直在用 `ffpa_attn_func` 这一个公共入口，默认它就「 magically 」跑在了某个 GPU kernel 上。本讲要回答一个关键问题：**FFPA 到底有几个 kernel 后端？它们各自能干什么、什么时候该选哪个？**

学完本讲你应该能够：

- 说出 FFPA 的四个后端（SDPA / CUDA / Triton / CuTeDSL）的架构定位与能力差异。
- 记住每个后端在前向（Fwd）、反向（Bwd）、head_dim 范围、自动调优（Autotune）上的能力矩阵。
- 理解为什么**默认后端是 Triton**，而不是看起来最快的 CUDA 或 CuTeDSL。
- 知道 CUDA 后端是「仅前向」的，并且需要 `ENABLE_FFPA_CUDA_IMPL=1` 编译才能用。
- 看懂分发层（`functional.py`）是如何用 `try/except` 把四个后端「软装载」进来的，以及 CUDA 后端如何在运行时用 `CUDA_FWD_AVAILABLE` 优雅降级。

本讲是第 3 单元「分发层与后端架构」的第一篇，是后续所有后端深入讲义（Triton 前向/反向、CuTeDSL、手写 CUDA）的导论。

## 2. 前置知识

在进入后端之前，先用大白话澄清几个本讲会反复用到的概念。

- **后端（backend）**：同一个数学运算 `softmax(scale·QKᵀ)·V` 可以用不同的代码去实现——可以调 PyTorch 自带的 fused kernel，可以用 Triton DSL 写，可以用 CUTLASS CuTeDSL 生成，也可以手写 CUDA C++。每一种「实现方式」就叫一个后端。FFPA 把它们统一藏在 `ffpa_attn_func` 后面，用户传一个 `backend=` 参数就能切换。
- **前向（forward）与反向（backward）**：前向算注意力输出 `O`；反向（求梯度）算 `dQ/dK/dV`。训练既要前向也要反向，推理（inference / prefill）往往只要前向。**一个后端可以只实现前向**，反向借别人的——FFPA 的 CUDA 后端就是这样。
- **head_dim（D）**：每个注意力头特征向量的维度。FFPA 的核心战场是**大 head_dim（D>256）**，这是标准 FlashAttention 撑不住的场景（详见 [u1-l1](./u1-l1-what-is-ffpa-split-d.md)）。
- **SM（Streaming Multiprocessor）与架构代号**：NVIDIA GPU 的计算单元叫 SM，不同代际有不同的「compute capability」（如 `sm80`=Ampere A100/A30、`sm89`=Ada L20、`sm90`=Hopper H100/H200、`sm100/120`=Blackwell）。后端对硬件有最低 SM 要求。
- **自动调优（autotune）**：同一个 kernel 可以有很多「配置」（分块大小、warps、stages……）。autotune 指运行时把若干候选配置都试跑一遍，挑最快的那个缓存下来。这是个「能力」——只有支持它的后端才能享受。
- **MMA（Matrix Multiply-Accumulate）**：Tensor Core 上的矩阵乘加指令，FFPA 的 Split-D 就是在 MMA 指令层做精细分块（[u1-l1](./u1-l1-what-is-ffpa-split-d.md)）。

> 一句话定位：FFPA = 一个公共 API + **四个可替换的 kernel 后端**。本讲就是这四个后端的「产品说明书」。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
|:---|:---|
| [README.md](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/README.md) | 项目说明，其中 `🤖 Backends` 章节给出了官方的四后端能力对照表（本讲的权威数据来源）。 |
| [src/ffpa_attn/functional.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py) | **分发层**。定义四个 `Backend` 配置类、用 `try/except` 软装载四个后端、决定默认后端、把前向/反向路由到对应 kernel。 |
| [src/ffpa_attn/cuda/\_\_init\_\_.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cuda/__init__.py) | CUDA 后端入口。用 `try/except` 导入编译产物 `ffpa_attn._C`，并用 `CUDA_FWD_AVAILABLE` 表达运行时可用性。 |
| [src/ffpa_attn/cute/\_\_init\_\_.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/__init__.py) | CuTeDSL 后端入口。提供 `cute_forward_available` / `cute_max_supported_head_dim` 等能力探测函数，用于补全能力矩阵。 |
| [src/ffpa_attn/\_\_init\_\_.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/__init__.py) | 顶层导出。把四个 `Backend` 类与 `ffpa_attn_func` 一起暴露给用户。 |

## 4. 核心概念与源码讲解

### 4.1 四后端能力矩阵：从 README 读起

#### 4.1.1 概念说明

FFPA 的 `🤖 Backends` 章节([README.md:85-101](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/README.md#L85-L101))开宗明义：

> FFPA supports multiple backends for the forward and backward pass, including: `SDPA` (baseline), `CUDA` (forward only), `Triton`, and `CuTeDSL`.

也就是说，FFPA 把「注意力怎么算」这件事拆成了**四个可选实现**：

1. **SDPA**：PyTorch 原生的 `scaled_dot_product_attention`，是**基线（baseline）**，不是用来加速的，而是用来「兜底」和「对比」的。
2. **CUDA**：FFPA 自己**手写的 CUDA C++ kernel**（在 `csrc/cuffpa`），**只实现了前向**，是最早的成果。
3. **Triton**：用 Triton DSL 写的 kernel，**前向 + 反向都支持**，是**默认后端**，也是唯一支持 autotune 的。
4. **CuTeDSL**：用 NVIDIA CUTLASS 的 CuTeDSL 写的 kernel，**前向 + 反向都支持**，在 Hopper（H200）上能跑到极高速度，但目前仍处于早期、有不少约束。

#### 4.1.2 核心流程：官方能力对照表

下面这张表直接来自 README([README.md:91-97](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/README.md#L91-L97))，是本讲的「根证书」——后面所有源码讲解都在解释它：

| Backend | Arch | Fwd | Bwd | Headdim | Autotune | Speedup | Recommend |
|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| SDPA | sm>=75 | ✔ | ✔ | All | ❌ | **1.0x** | sm>=75 |
| CUDA | sm>=80 | ✔ | ❌ | 320~1024 | ❌ | **1.5x~3x** | sm80~89,120 |
| Triton | sm>=80 | ✔ | ✔ | 320~1024 | ✔ | **1.5x~5x** | sm>=80 |
| CuTeDSL | sm>=80 | ✔ | ✔ | 320~1024 | ❌ | **1.5x~2x** | sm80~89,120 |
| CuTeDSL | sm90 | ✔ | ✔ | 320~512 | ❌ | **3x~6x** | sm90 |

读懂这张表的几个要点：

- **Arch（最低架构）**：除了 SDPA 从 `sm75`（Turing）起，FFPA 自家的三个加速后端都要求 `sm>=80`（Ampere 及以后）。原因：Split-D 依赖较新的 Tensor Core MMA 指令。
- **Fwd/Bwd 列**：CUDA 行的 Bwd 是 ❌——**CUDA 后端只算前向**，反向必须借 Triton 或 SDPA 来算（详见 4.4 节）。
- **Headdim 列**：FFPA 三个加速后端都主打 `320~1024` 的大 head_dim。SDPA 行写 `All`，因为它只是兜底，什么 D 都能跑（但大 D 跑得慢）。
- **Autotune 列**：**只有 Triton 是 ✔**。这是 Triton 作为默认后端的一大优势——它能针对每张卡、每个形状自动挑最快配置。
- **Speedup（相对 SDPA）**：Triton 在通用场景 `1.5x~5x`；CuTeDSL 在 **sm90（Hopper）+ head_dim ≤ 512** 这个细分场景上能到 `3x~6x`，所以 README 反复强调「CuTeDSL 在 H200 上最快」（[README.md:87](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/README.md#L87)）。
- **CuTeDSL 占两行**：它在 sm90 专用路径（head_dim ≤ 512）与 sm80 通用回退路径（head_dim 可到 1024）上表现差异巨大，所以拆成两行写。

> ⚠️ 表里的 Speedup 数字是基准测试结论（来自 `bench/`，详见 [u8-l5](./u8-l5-bench-cli-tflops.md)），**会随硬件/形状变化**，不要把它当成普适常数。但「相对关系」（Triton 全面强、CuTeDSL 在 Hopper 大 D 上最强、CUDA 仅前向）是稳定的。

#### 4.1.3 源码精读：README 的两段「画外音」

除了主表，README 还有两段补充信息值得标注：

**① CuTeDSL 还在早期、H200 上最快**（[README.md:87](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/README.md#L87)）：

> The `CuTeDSL` backend is currently in early stage and has some constraints, but it can achieve up to `427` TFLOPS on H200!

这段话解释了「为什么 CuTeDSL 明明更快却不是默认」——**它有约束（不支持 dropout / attn_mask 等）、还在早期**，所以默认让给更稳的 Triton，等用户在大 D + Hopper 训练场景下再显式启用。

**② Triton 后端还能跑在 AMD ROCm GPU 上**（[README.md:104](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/README.md#L104)）：

> 🔴 **AMD ROCm/HIP support**: the `Triton` backend (forward + backward) also runs on AMD GPUs ...

这是个常被忽略的点：**Triton 后端是跨厂商的**，因为它基于 Triton DSL 而非 CUDA C++。所以「Triton 是默认」还有一个隐含理由——它的可移植性最好。

#### 4.1.4 代码实践：动手整理自己的能力矩阵

**实践目标**：把 README 那张表内化为自己的知识，并补充 README 没明说、但源码能确认的细节。

**操作步骤**：

1. 打开 [README.md 的 Backends 章节](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/README.md#L85-L101)，把上面那张 5 行表格抄到自己的笔记里。
2. 在 Speedup 列旁边加一列「Autotune 候选来源」，标注：只有 Triton 的 autotune 来自 `TritonBackend.autotune` 字段（见 4.4 节）；CUDA/CuTeDSL 写「手写固定配置」。
3. 在 Headdim 列旁边加一列「下界来源」，标注：320 这个下界来自 CuTeDSL 的 `MIN_SUPPORTED_HEAD_DIM = 320`（见 4.1.5 的延伸阅读），而 Triton 的回退条件里也以 `D≤256` 为分界。

**需要观察的现象**：你会发现自己能仅凭 README + 少量源码常量，复现出一张比 README 更细的能力表。

**预期结果**：得到一张至少 7 列（Backend/Arch/Fwd/Bwd/Headdim/Autotune/Speedup/Recommend）的表，并且每一列都能在源码里找到依据。

#### 4.1.5 小练习与答案

**练习 1**：为什么 README 把 CuTeDSL 写成两行（sm80~89,120 一行、sm90 一行），而 Triton 只有一行？

**参考答案**：因为 CuTeDSL 在 sm90 上有**专用路径（specialised path）**，仅在 `head_dim ≤ 512` 时启用，速度可达 `3x~6x`；超过该范围（head_dim > 512 或非 sm90）就回退到 sm80 通用 Split-D 路径，速度只有 `1.5x~2x`。两个路径性能差异大，故拆两行。Triton 没有这种「按架构分叉」的专用路径，单一代码路径覆盖 `sm>=80`，所以一行即可。

**练习 2**：一个用户只有一张 RTX 4090（sm89, Ada），想要训练（需要反向）。他能用哪几个后端？不能用哪个？

**参考答案**：SDPA（兜底）、Triton（默认，前向+反向+autotune）、CuTeDSL（sm80 通用路径，前向+反向）都能用。**CUDA 后端不能单独用于训练**，因为它「forward only」；如果一定要用 CUDA，得搭配 `backward_backend='triton'` 或 `'sdpa'`（见 4.4 节）。

### 4.2 分发层如何「软装载」四个后端：try/except 导入

#### 4.2.1 概念说明

四个后端的「重量」并不一样：

- **SDPA**：是 PyTorch 自带的，`import torch` 就有，永远可用。
- **Triton**：是 FFPA 默认依赖（Triton DSL），随包安装，几乎永远可用。
- **CuTeDSL**：依赖 CUTLASS CuTeDSL，是个**可选重依赖**，用户可能没装。
- **CUDA**：是**编译期可选**的 C++ 扩展（`ffpa_attn._C`），默认 `pip install` 出来的 wheel **根本没有**它（详见 [u1-l2](./u1-l2-install-and-build-modes.md)）。

如果分发层用「硬导入」（`from .cuda import _ffpa_attn_forward_cuda`），那任何缺了 CUDA 扩展的环境连 `import ffpa_attn` 都会崩。所以 FFPA 用了一个经典模式：**把「可能缺失」的后端包在 `try/except` 里，失败时把符号设成 `None`**，让核心库永远能 import，运行时再按需检查。

#### 4.2.2 核心流程：四个导入的「强弱」分级

`functional.py` 顶部的四段导入([functional.py:18-41](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L18-L41))可以分成「硬导入」和「软导入」两类：

```text
硬导入（默认必有，不 try/except）：
  Triton  → _ffpa_attn_forward_triton / _ffpa_attn_backward_triton   (大 D 默认)
  aten    → _flash_attn_forward_aten / ...                            (D <= 256 兜底)

软导入（可能缺失，try/except 包裹，失败置 None）：
  cute    → _ffpa_attn_forward_cute / _ffpa_attn_backward_cute / _ffpa_attn_varlen_cute
  cuda    → _ffpa_attn_forward_cuda
```

注意注释里还埋了一条重要信息：**Triton 与 CuTeDSL 默认都是「大 D」后端，只有设置了环境变量 `FFPA_TRITON_ALLOW_SMALL_D=1` / `FFPA_CUTE_ALLOW_SMALL_D=1` 才会接管小 D**（[functional.py:21](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L21) 与 [functional.py:32](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L32)）。这呼应了 [u1-l1](./u1-l1-what-is-ffpa-split-d.md) 讲的「FFPA 主攻大 D」。

#### 4.2.3 源码精读：四段导入逐行标注

**① Triton 与 aten：硬导入**（[functional.py:18-26](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L18-L26)）：

```python
from .triton import (
  _ffpa_attn_forward_triton,
  _ffpa_attn_backward_triton,
)  # Large-D by default; small-D when FFPA_TRITON_ALLOW_SMALL_D=1.
from .aten import (
  _flash_attn_forward_aten,
  _flash_attn_backward_aten,
  _efficient_attn_backward_aten,
)  # D <= 256
```

说明：这两段没有 `try/except`——Triton 和 PyTorch aten 都是必装依赖。`aten` 子包专门负责 `D ≤ 256` 的小 D 路径，调 PyTorch 自带的 flash / efficient attention（这就是 README 表里 SDPA 行的「All」的来源）。

**② CuTeDSL：软导入**（[functional.py:27-36](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L27-L36)）：

```python
try:
  from .cute import (
    _ffpa_attn_forward_cute,
    _ffpa_attn_backward_cute,
    _ffpa_attn_varlen_cute,
  )  # Large-D by default; small-D when FFPA_CUTE_ALLOW_SMALL_D=1.
except Exception:
  _ffpa_attn_forward_cute = None
  _ffpa_attn_backward_cute = None
  _ffpa_attn_varlen_cute = None
```

说明：捕的是宽泛的 `Exception`（不是 `ImportError`），因为 CuTeDSL 在导入时可能因依赖缺失、版本不符等多种原因失败。失败后三个符号都置 `None`，库照常可用，只是用户若显式要 `forward_backend='cutedsl'` 会在运行时拿到报错。

**③ CUDA：软导入**（[functional.py:38-41](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L38-L41)）：

```python
try:
  from .cuda import _ffpa_attn_forward_cuda  # D > 256
except Exception:
  _ffpa_attn_forward_cuda = None
```

说明：注释 `# D > 256` 标明它只服务大 D；导入失败（即默认 wheel 没编 CUDA 扩展）就置 `None`。注意这里**只导入了 forward**，没有 backward——再次印证 CUDA 后端「仅前向」。

> 这四段导入是理解整个分发层的「地基」：后面所有「按 backend 名字分发」的代码，本质上都是在检查这些符号是不是 `None`、要不要走某条路径。

#### 4.2.4 代码实践：观察「软导入」的实际效果

**实践目标**：亲手验证「默认安装下，CUDA 与 CuTeDSL 的导入符号确实是 `None` 或可用」。

**操作步骤**：

1. 在装好 `ffpa-attn`（默认 Triton-only，未设 `ENABLE_FFPA_CUDA_IMPL=1`）的环境里执行下面这段「示例代码」：

```python
# 示例代码：探查四个后端的软装载状态
import ffpa_attn.functional as F
print("triton fwd :", F._ffpa_attn_forward_triton is not None)   # 预期 True
print("aten fwd   :", F._flash_attn_forward_aten is not None)    # 预期 True
print("cuda fwd   :", F._ffpa_attn_forward_cuda is not None)     # 默认安装预期 False
print("cute fwd   :", F._ffpa_attn_forward_cute is not None)     # 取决于是否装了 CuTeDSL
```

2. 如果你装的是带 CUDA 扩展的版本（`ENABLE_FFPA_CUDA_IMPL=1`），再看一次 `cuda fwd` 那一行的输出。

**需要观察的现象**：默认安装下，`cuda fwd` 行应输出 `False`；带 CUDA 扩展的安装下应输出 `True`。

**预期结果**：四行打印里，前两行（triton / aten）恒为 `True`，后两行随安装模式变化。这直观证明了「软导入」让默认 wheel 也能正常 `import ffpa_attn`。

> 待本地验证：上述输出取决于你本地的安装方式与硬件；若无 GPU 环境，`import ffpa_attn` 本身仍可成功，但运行 kernel 会报错。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `from .cuda import ...` 用 `except Exception` 而不是 `except ImportError`？

**参考答案**：因为 `ffpa_attn.cuda` 子包在 `import` 时除了可能触发 `ImportError`（找不到 `_C`），还可能因 CUDA 扩展虽存在但与当前 PyTorch / CUDA runtime 版本不兼容而抛 `AttributeError`、`OSError` 等。用宽泛的 `Exception` 能保证「只要 CUDA 后端有任何问题，就降级为不可用、不阻塞主库 import」，代价是会吞掉真实错误信息——FFPA 在运行时（见 4.3 节）会用 `_CUDA_IMPORT_ERROR` 把原始错误还给用户。

**练习 2**：四段导入里，哪一段最能体现「CUDA 后端仅前向」？

**参考答案**：第三段 CUDA 软导入([functional.py:38-41](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L38-L41))——它**只导入了 `_ffpa_attn_forward_cuda` 一个符号**，没有任何 `_ffpa_attn_backward_cuda`。对比 Triton 那段同时导入 forward 和 backward，差异一目了然。

### 4.3 CUDA 后端的运行时探测：CUDA_FWD_AVAILABLE 与 _C

#### 4.3.1 概念说明

4.2 节讲的是「分发层」怎么看待 CUDA 后端（软导入成 `None`）。本节钻进 CUDA 后端**自己的**入口 `cuda/__init__.py`，看它怎么把「C++ 编译产物」变成「Python 可调用函数」，以及怎么用一个布尔标志 `CUDA_FWD_AVAILABLE` 来表达「我到底能不能用」。

关键概念：

- **`ffpa_attn._C`**：pybind11 绑定出来的 C++ 扩展模块。只有用 `ENABLE_FFPA_CUDA_IMPL=1` 编译才会有它（详见 [u1-l2](./u1-l2-install-and-build-modes.md)）。
- **`CUDA_FWD_AVAILABLE`**：一个**运行时布尔标志**，由 C++ 侧写入扩展模块的属性，告诉 Python「前向 CUDA kernel 真的可以跑」。它比「`_C` 能 import」更严格——即使扩展编出来了，也可能因为 GPU 架构不匹配而不可用。

#### 4.3.2 核心流程：从 C++ 产物到 Python 标志

```text
  ENABLE_FFPA_CUDA_IMPL=1 编译
            │
            ▼
  生成 ffpa_attn._C 扩展（含 ffpa_attn_forward 函数 + CUDA_FWD_AVAILABLE 属性）
            │
            ▼  cuda/__init__.py 顶部 try/except
  _ffpa_attn_fwd_cuda = _C.ffpa_attn_forward
  CUDA_FWD_AVAILABLE  = bool(getattr(_C, "CUDA_FWD_AVAILABLE", False))
  CUDA_BWD_AVAILABLE  = False                       ← 永远 False，因为没编反向
            │
            ▼  导入失败（默认 wheel）
  三个符号全部降级：_ffpa_attn_fwd_cuda=None, CUDA_FWD_AVAILABLE=False
```

#### 4.3.3 源码精读：cuda/\_\_init\_\_.py 的容错导入

**① 软导入 `_C` 并读取可用性标志**（[cuda/\_\_init\_\_.py:4-15](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cuda/__init__.py#L4-L15)）：

```python
try:
  from .. import _C as _cuda_ext

  _ffpa_attn_fwd_cuda = _cuda_ext.ffpa_attn_forward
  CUDA_FWD_AVAILABLE = bool(getattr(_cuda_ext, "CUDA_FWD_AVAILABLE", False))
  CUDA_BWD_AVAILABLE = False
  _CUDA_IMPORT_ERROR = None
except Exception as exc:
  _ffpa_attn_fwd_cuda = None
  CUDA_FWD_AVAILABLE = False
  CUDA_BWD_AVAILABLE = False
  _CUDA_IMPORT_ERROR = exc
```

逐行说明：

- `from .. import _C as _cuda_ext`：从父包 `ffpa_attn` 导入编译产物 `_C`。默认 wheel 没有它，会进 `except`。
- `_ffpa_attn_fwd_cuda = _cuda_ext.ffpa_attn_forward`：把 C++ 函数挂到一个 Python 名字上。注意函数名是 `ffpa_attn_forward`（**前向**），没有反向函数——再次印证「forward only」。
- `CUDA_FWD_AVAILABLE = bool(getattr(_cuda_ext, "CUDA_FWD_AVAILABLE", False))`：用 `getattr(..., False)` 兜底——如果 C++ 侧没写这个属性，默认就当 `False`。这是个**运行时优雅降级**标志。
- `CUDA_BWD_AVAILABLE = False`：硬编码 `False`，明确告诉外部「CUDA 后端没有反向」。
- `_CUDA_IMPORT_ERROR = exc`：把原始异常存起来，等用户真要用 CUDA 后端时再抛回去，避免「静默失败、找不到原因」。

**② 运行时调用处的友好报错**（[cuda/\_\_init\_\_.py:46-51](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cuda/__init__.py#L46-L51)）：

```python
  if _ffpa_attn_fwd_cuda is None:
    raise RuntimeError(
      "ffpa_attn forward CUDA backend is unavailable. "
      "Rebuild with ENABLE_FFPA_CUDA_IMPL=1 to enable it. "
      f"Original import error: {_CUDA_IMPORT_ERROR}"
    )
```

说明：这是 `_fwd_cuda_torch_op`（注册成 `torch.ops.ffpa_attn._fwd_cuda` 的实现）里的守卫。当用户传 `forward_backend='cuda'` 但环境里没有 CUDA 扩展时，会得到一条**可操作**的报错——既告诉你怎么修（重新加 `ENABLE_FFPA_CUDA_IMPL=1` 编译），又把原始导入错误附上。

**③ `__all__` 对外暴露的符号**（[cuda/\_\_init\_\_.py:104-109](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cuda/__init__.py#L104-L109)）：

```python
__all__ = [
  "_ffpa_attn_forward_cuda",
  "_ffpa_attn_backward_cuda",
  "CUDA_FWD_AVAILABLE",
  "CUDA_BWD_AVAILABLE",
]
```

说明：注意 `_ffpa_attn_backward_cuda` 也出现在 `__all__` 里——但结合 4.2 节，分发层只导入了 forward；这个 backward 符号来自 `from ._ffpa_bwd import _ffpa_attn_backward_cuda`（[cuda/\_\_init\_\_.py:17-18](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cuda/__init__.py#L17-L18)），它实际上是用 **Triton/aten 实现的回退反向**，而非手写 CUDA。也就是说「CUDA 后端」的反向本来就是借别人的，这也解释了为什么 `CUDA_BWD_AVAILABLE` 恒为 `False`。

#### 4.3.4 代码实践：探测 CUDA 后端可用性

**实践目标**：理解 `CUDA_FWD_AVAILABLE` / `CUDA_BWD_AVAILABLE` 在不同安装下的取值。

**操作步骤**：

1. 在你的环境里跑这段「示例代码」：

```python
# 示例代码：读取 CUDA 后端的可用性标志
from ffpa_attn.cuda import CUDA_FWD_AVAILABLE, CUDA_BWD_AVAILABLE
print("CUDA_FWD_AVAILABLE:", CUDA_FWD_AVAILABLE)
print("CUDA_BWD_AVAILABLE :", CUDA_BWD_AVAILABLE)
```

2. 先在默认安装（未设 `ENABLE_FFPA_CUDA_IMPL=1`）下跑一次；如果条件允许，重装带 CUDA 扩展的版本再跑一次。

**需要观察的现象**：`CUDA_BWD_AVAILABLE` 应**永远是 False**（无论怎么装）；`CUDA_FWD_AVAILABLE` 在默认安装下为 False，在带 CUDA 扩展且架构匹配时为 True。

**预期结果**：你将直观看到「CUDA 后端仅前向」这个事实在源码层面是用 `CUDA_BWD_AVAILABLE = False` 这行硬编码表达的。

> 待本地验证：`CUDA_FWD_AVAILABLE` 的 True/False 取决于你的 wheel 是否含 `_C` 以及 GPU 架构；若在无 GPU 的机器上，即便编了扩展也可能因架构不匹配而为 False。

#### 4.3.5 小练习与答案

**练习 1**：`CUDA_FWD_AVAILABLE` 和「`_C` 能不能 import」是一回事吗？举一个二者不一致的场景。

**参考答案**：不是一回事。`_C` 能 import 只说明 C++ 扩展文件存在且能加载；`CUDA_FWD_AVAILABLE` 是 C++ 侧额外写入的、表示「前向 kernel 真的为当前 GPU 架构编译了」的更严格标志。不一致场景：用 `ENABLE_FFPA_CUDA_IMPL=1` 但只为 Ampere（sm80）编译了扩展，却运行在 Hopper（sm90）机器上——`_C` 能 import，但对应的 forward kernel 可能没为 sm90 编译，此时 `CUDA_FWD_AVAILABLE` 可能为 False。

**练习 2**：为什么 `_CUDA_IMPORT_ERROR` 要被存下来？

**参考答案**：为了让「软导入」既能保护主库（导入失败不崩溃），又不丢失「为什么失败」的信息。当用户后来显式选用 CUDA 后端时，[cuda/\_\_init\_\_.py:46-51](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cuda/__init__.py#L46-L51) 的报错会把 `_CUDA_IMPORT_ERROR` 拼进消息，让用户看到真实的导入错误（如缺库、版本不符），而不是只得到一句干巴巴的「unavailable」。

### 4.4 默认后端与「前向 × 反向」自由配对

#### 4.4.1 概念说明

知道了四个后端各自的能力后，还要回答两个选型问题：

1. **默认后端是谁？** —— 不传 `backend=` 时跑哪个？
2. **前向和反向必须用同一个后端吗？** —— 比如「前向用 CUDA、反向用 Triton」可以吗？

答案都藏在 `functional.py` 里：

- **默认是 Triton**。原因：它前向+反向都支持、`sm>=80` 通吃、支持 autotune、还能跨厂商（AMD ROCm），是「木桶最均衡」的那个。
- **前向和反向可以自由配对**。这正是 FFPA 设计上最灵活的地方——CUDA 前向可以配 Triton 或 SDPA 反向，Triton 前向也能配 SDPA 反向，等等。

为了支持这种自由配对，FFPA 用四个 `dataclass` 把每个后端「配置化」。

#### 4.4.2 核心流程：Backend 配置类与配对规则

四个后端配置类([functional.py:108-242](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L108-L242))的定位：

| 配置类 | `name` | 前向 | 反向 | 关键专属字段 |
|:---|:---:|:---:|:---:|:---|
| `SDPABackend` | `"sdpa"` | ✔（短路兜底） | ✔（`high_precision_grad`） | `high_precision_grad` |
| `CUDABackend` | `"cuda"` | ✔ | ❌（断言禁止） | `acc`(f16/f32)、`stages` |
| `TritonBackend` | `"triton"` | ✔ | ✔ | `autotune`、`enable_tma`、`persist_dkdv` 等 |
| `CuTeDSLBackend` | `"cutedsl"` | ✔ | ✔ | `grad_kv_storage_dtype` |

`functional.py` 里还有一张非常珍贵的「前后向配对表」，写在一段解释 `register_autograd` 为何不能用的长注释里([functional.py:946-965](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L946-L965))，它列出了**每一种前向后端允许搭配哪些反向后端**：

```text
  forward_backend   │  backward_backend
  ──────────────────┼───────────────────
  sdpa              │  (n/a — always short-circuits via meta.fallback())
  cuda              │  triton, sdpa
  triton            │  triton, sdpa
  cutedsl           │  cutedsl, triton, sdpa
```

读法：

- **sdpa 前向**：直接短路走原生 SDPA，根本不进 FFPA 的 autograd Function，所以没有「配对」一说。
- **cuda 前向**：因为 CUDA 没有反向，只能配 `triton` 或 `sdpa` 反向。
- **triton 前向**：可配 `triton`（默认）或 `sdpa` 反向。
- **cutedsl 前向**：可配 `cutedsl`、`triton` 或 `sdpa` 反向——最灵活。

这张表是本讲的「隐藏宝藏」，它把 README 那张「Fwd/Bwd 列」背后的真实组合规则讲透了。

#### 4.4.3 源码精读：默认 Triton 与配对校验

**① 默认后端是 Triton**（[functional.py:253-281](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L253-L281)）：

```python
def _resolve_backend_pair(
  forward_backend: Backend | None,
  backward_backend: Backend | None,
) -> tuple[Backend, Backend]:
  forward_backend = TritonBackend(
    forward=True
  ) if forward_backend is None else forward_backend
  backward_backend = TritonBackend(
    backward=True
  ) if backward_backend is None else backward_backend
  ...
```

说明：当用户既没传 `backend=` 也没传 `forward_backend=`/`backward_backend=` 时，前后向都默认构造一个 `TritonBackend`。这就是「默认后端是 Triton」的源码出处。`FFPAAttnMeta` 的默认字段也是 Triton([functional.py:396-401](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L396-L401))。

**② CUDA 后端「禁用反向」的硬断言**（[functional.py:150-167](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L150-L167)）：

```python
@dataclass
class CUDABackend(Backend):
  name: str = "cuda"
  acc: str = "f32"
  stages: int = 4 if _is_hopper_or_later() else 3

  def __post_init__(self) -> None:
    super().__post_init__()
    assert not self.backward, "cuda backend does not support backward"
    assert self.acc in ("f16", "f32"), ...
```

说明：`__post_init__` 里 `assert not self.backward`——如果你试图构造 `CUDABackend(backward=True)` 会直接断言失败。`stages` 默认按架构选（Hopper+ 用 4 级流水线，其余 3 级），`acc` 控制累加器精度（默认 `f32`）。这些都印证了 README 表里 CUDA 行的「forward only」与「手写固定配置（无 autotune）」。

**③ 字符串简写 `backend=` 的映射**（[functional.py:284-305](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L284-L305)）：

```python
  if isinstance(backend, str):
    _BACKEND_MAP = {
      "cuda": CUDABackend,
      "triton": TritonBackend,
      "cutedsl": CuTeDSLBackend,
      "sdpa": SDPABackend,
    }
```

说明：用户可以传字符串 `backend="cutedsl"`，分发层通过这张 `_BACKEND_MAP` 把它翻译成对应的配置类实例。注意 `cutedsl` 这个对外名字和子包目录名 `cute` 不一致（详见 [u1-l3](./u1-l3-repo-layout-code-map.md)）。

**④ forward 按类型路由**（[functional.py:764-829](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L764-L829)，节选）：

```python
    if use_aten_small_d_forward:            # D <= 256 → aten
      O, lse, rng_state, unused = _flash_attn_forward_aten(...)
    elif isinstance(meta.forward_meta, CUDABackend):
      O, lse = _ffpa_attn_forward_cuda(...)         # 手写 CUDA 前向
    elif isinstance(meta.forward_meta, TritonBackend):
      O, lse = _ffpa_attn_forward_triton(...)       # Triton 前向（默认）
    elif isinstance(meta.forward_meta, CuTeDSLBackend):
      O, lse = _ffpa_attn_forward_cute(...)         # CuTeDSL 前向
```

说明：前向分发用 `isinstance(...)` 按配置类类型挑 kernel。注意第一个分支 `use_aten_small_d_forward`（`D ≤ 256`）优先级最高——**不管你选哪个后端，只要 D≤256 且没开小 D 开关，都先回退到 aten**（这呼应 [u1-l4](./u1-l4-one-line-sdpa-monkey-patch.md) 讲的 fallback 短路）。

**⑤ backward 按类型路由**（[functional.py:862-923](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L862-L923)，节选）：

```python
      if isinstance(meta.backward_meta, TritonBackend):
        dq, dk, dv, grad_attn_bias = _ffpa_attn_backward_triton(...)
      elif isinstance(meta.backward_meta, CuTeDSLBackend):
        dq, dk, dv = _ffpa_attn_backward_cute(...)
      else:
        assert isinstance(meta.backward_meta, SDPABackend), ...
        dq, dk, dv, grad_attn_bias = _efficient_attn_backward_aten(...)
```

说明：反向分发**完全没有 `CUDABackend` 分支**——因为 CUDA 没有反向。反向只能在 Triton / CuTeDSL / SDPA 三者里选。这就是为什么 4.4.2 那张配对表里，cuda 前向只能配 triton/sdpa 反向。

> 这一小节的配对表和路由代码，是理解整个第 3 单元的「钥匙」。后续 [u3-l2](./u3-l2-backend-config-dataclasses.md) 会逐字段细讲四个配置类，[u3-l4](./u3-l4-autograd-function-dispatch.md) 会细讲 forward/backward 的完整分发决策树。

#### 4.4.4 代码实践：用三种方式指定 CuTeDSL 后端

**实践目标**：亲手验证「默认是 Triton」「前后向可分离指定」「字符串简写与配置类等价」。

**操作步骤**：

1. 阅读并运行下面这段「示例代码」（需在支持 CuTeDSL 的 Hopper/Ada GPU 上才真跑 cute kernel；否则可只看报错路径）：

```python
# 示例代码：三种指定后端的方式
import torch
from ffpa_attn import ffpa_attn_func, CuTeDSLBackend

q = torch.randn(1, 32, 8192, 512, dtype=torch.bfloat16, device="cuda")
k = torch.randn(1, 32, 8192, 512, dtype=torch.bfloat16, device="cuda")
v = torch.randn(1, 32, 8192, 512, dtype=torch.bfloat16, device="cuda")

# 方式 A：不传 backend → 默认 Triton
o_a = ffpa_attn_func(q, k, v)

# 方式 B：传字符串简写
o_b = ffpa_attn_func(q, k, v, backend="cutedsl")

# 方式 C：传配置类实例（README 推荐写法）
o_c = ffpa_attn_func(q, k, v, backend=CuTeDSLBackend())
```

2. 阅读 [functional.py:408-450](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L408-L450) 的 `FFPAAttnMeta.from_kwargs`，确认 `backend=` 字符串简写和 `CuTeDSLBackend()` 实例最终都会被翻译成同样的 `forward_meta / backward_meta`。

**需要观察的现象**：方式 A 默认走 Triton；方式 B 与方式 C 都走 CuTeDSL，且 B、C 输出应数值一致（同为 cute kernel）。

**预期结果**：你能解释清楚 README 这句用法([README.md:109-112](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/README.md#L109-L112))——`ffpa_attn_func(q, k, v, backend=CuTeDSLBackend())` 背后的解析过程。

> 待本地验证：若无 Hopper/Ada GPU 或未装 CuTeDSL，方式 B/C 会在运行时报「cutedsl 不可用」或回退；方式 A（Triton）在 `sm>=80` 上一般可直接跑。

#### 4.4.5 小练习与答案

**练习 1**：请用一句话解释「为什么默认是 Triton 而非 CUDA」。

**参考答案**：因为 Triton 是四个后端里**唯一同时满足「前向+反向都支持、`sm>=80` 通吃、支持 autotune 自动调优、还能跨厂商跑 AMD ROCm」**的后端，木桶最均衡；而 CUDA 仅前向、需额外编译、不支持 autotune，无法担当默认。

**练习 2**：用户写 `ffpa_attn_func(q,k,v, forward_backend="cuda", backward_backend="sdpa")`，这条链路合法吗？为什么？

**参考答案**：合法。对照 4.4.2 的配对表，`cuda` 前向允许搭配 `triton` 或 `sdpa` 反向。前向会走 `_ffpa_attn_forward_cuda`（手写 CUDA kernel），反向会走 `_efficient_attn_backward_aten`（PyTorch 原生 efficient attention 反向）。这也是 `functional.py` 顶部那段注释([functional.py:946-965](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L946-L965))强调「不能用 `register_autograd` 绑死单一反向」的典型场景。

**练习 3**：`CUDABackend(backward=True)` 会发生什么？

**参考答案**：会在 `__post_init__` 里触发 `assert not self.backward, "cuda backend does not support backward"`([functional.py:164](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L164))，抛 `AssertionError`。这是源码层面对「CUDA 仅前向」的硬性保护。

## 5. 综合实践

把本讲全部内容串起来，完成下面这个「**FFPA 后端选型顾问**」小任务。

**任务背景**：假设你是团队的推理/训练工程师，团队成员会拿着各种场景来问你该用哪个后端。请你基于本讲的源码与 README 表格，写一份「场景 → 推荐后端」的决策文档，要求每个推荐都能在源码里找到依据。

**要求覆盖以下 5 个场景**：

1. **场景 A**：A100（sm80）上做**训练**，`D=512`，`N=8192`，需要 autotune。
2. **场景 B**：H200（sm90）上做**训练**，`D=512`，追求极致速度，不需要 dropout / attn_mask。
3. **场景 C**：L20（sm89）上做**纯前向推理**（prefill），`D=1024`，不想编译 C++ 扩展。
4. **场景 D**：4090（sm89）上做**训练**，`D=128`（小 head_dim）。
4. **场景 E**：AMD MI250X（gfx90a）上做训练。

**对每个场景，你需要给出**：

- 推荐的后端（可包含前向/反向分别用什么）。
- 引用本讲某段源码或 README 某行作为依据（给出永久链接）。
- 说明「为什么别的后端不合适」。

**参考思路**（请先自己写，再对照）：

- A：默认 **Triton**（前向+反向+autotune 全有，[functional.py:257-262](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L257-L262) 默认即 Triton）。
- B：**CuTeDSL**（sm90 + D=512 命中专用路径，README 表里 `3x~6x`；但要满足「无 dropout / 无 attn_mask」，否则被 [functional.py:554-564](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L554-L564) 拒绝）。
- C：**Triton** 前向（不编译 C++ 就用不了 CUDA 后端，[functional.py:38-41](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L38-L41) 软导入会置 None；Triton 同样支持 `D=1024`）。
- D：**回退 SDPA/aten**（`D=128 ≤ 256`，[functional.py:759-765](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L759-L765) `use_aten_small_d_forward` 直接走 aten，FFPA 不接管小 D）。
- E：**Triton**（唯一能跑 AMD ROCm 的后端，[README.md:104](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/README.md#L104)）。

> 待本地验证：场景 B 的实际加速比取决于具体形状与 H200 卡，建议用 `python -m ffpa_attn.bench`（[u8-l5](./u8-l5-bench-cli-tflops.md)）实测。

## 6. 本讲小结

- FFPA 有**四个后端**：SDPA（基线/兜底）、CUDA（手写 C++，**仅前向**）、Triton（DSL，前向+反向，**默认**）、CuTeDSL（CUTLASS，前向+反向，Hopper 上最快）。
- 能力矩阵的权威来源是 [README.md:91-97](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/README.md#L91-L97)：CUDA 不支持反向、只有 Triton 支持 autotune、CuTeDSL 在 sm90 + D≤512 上最快。
- 分发层 `functional.py` 用 **`try/except` 软导入**装载 CuTeDSL 与 CUDA，失败置 `None`，保证默认 wheel 也能 `import`；Triton 与 aten 是硬导入（必装依赖）。
- CUDA 后端入口 [cuda/\_\_init\_\_.py:4-15](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cuda/__init__.py#L4-L15) 用 `CUDA_FWD_AVAILABLE` / `CUDA_BWD_AVAILABLE` 表达运行时可用性，后者**恒为 False**。
- **默认后端是 Triton**（[functional.py:257-262](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L257-L262)），因为它最均衡（前向+反向+autotune+跨厂商）。
- 前向与反向可**自由配对**，合法组合见 [functional.py:946-965](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L946-L965) 的配对表；反向分发代码里**没有 CUDABackend 分支**，CUDA 后端的 `backward=True` 会被 `__post_init__` 断言拒绝。

## 7. 下一步学习建议

本讲是「俯瞰」四个后端，接下来建议按以下顺序「钻进去」：

1. **[u3-l2 Backend 配置类体系](./u3-l2-backend-config-dataclasses.md)**：逐字段细讲 `TritonBackend` 的 `autotune`/`enable_tma`/`persist_dkdv` 等开关，以及 `backend=` / `forward_backend=` / `backward_backend=` 三者优先级。
2. **[u3-l3 FFPAAttnMeta：输入校验与 SDPA 回退判定](./u3-l3-meta-normalize-and-fallback.md)**：深入 `fallback()` 的回退条件，理解「哪些场景会让 FFPA 把活儿交回 SDPA」。
3. **[u3-l4 autograd Function 前向/反向分发](./u3-l4-autograd-function-dispatch.md)**：完整读一遍 `_FFPAAttnFunc.forward` / `backward` 的决策树，把本讲 4.4 节的路由代码看全。
4. 如果你对某个后端特别感兴趣：前向 kernel 看 [u4 单元（Triton 前向）](./u4-l1-triton-fwd-online-softmax.md)，手写 CUDA 看 [u7 单元](./u7-l1-cuda-fwd-kernel-architecture.md)，CuTeDSL 看 [u6 单元](./u6-l1-cutedsl-overview-sm80-sm90.md)。
