# 四后端总览与选型矩阵

## 1. 本讲目标

前面几讲我们一直在调用 `ffpa_attn_func`，但很少关心「这一次调用到底跑在哪段代码上」。本讲是**分发层（dispatch）总览篇**，目标有三个：

1. 能背出 FFPA 的 **四个后端**（SDPA / CUDA / Triton / CuTeDSL）各自的能力矩阵——支持哪些架构、是否支持前向/反向、覆盖哪些 head_dim、能否自动调优、相对加速比是多少。
2. 理解为什么 **Triton 是默认后端**，而手写 CUDA 只覆盖前向且需要单独编译。
3. 学会在 `ffpa_attn_func` 调用里用 `backend=` / `forward_backend=` / `backward_backend=` 选型，并能判断在当前硬件 + 输入下哪个后端会被真正选中。

学完本讲，你应该能在阅读后续「Triton 前向/反向」「CuTeDSL 后端」「手写 CUDA 后端」三讲之前，先建立一张全局地图，知道每条调用链的入口和落点在哪里。

## 2. 前置知识

- **后端（backend）**：完成同一个数学运算（这里是 `softmax(scale·QKᵀ)V`）的不同实现。FFPA 把「数学定义」与「具体 kernel 实现」分离，运行时再挑实现。
- **SDPA**：PyTorch 原生的 `torch.nn.functional.scaled_dot_product_attention`，是 FFPA 的基线与回退目标（见 [u1-l4](./u1-l4-one-line-sdpa-monkey-patch.md)）。
- **head_dim（D）**：每个注意力头的特征维度。FFPA 的主战场是 **D ∈ [320, 1024]** 的大 head_dim（见 [u1-l1](./u1-l1-what-is-ffpa-split-d.md)）。
- **前向（forward）/ 反向（backward）**：前向算输出 O；反向在训练时由 autograd 触发，算 dQ/dK/dV。**一个后端可以只有前向、没有反向**，这正是 CUDA 后端的情形。
- **SM 架构**：NVIDIA GPU 的 compute capability，如 sm80（Ampere，A100/A30）、sm89（Ada，L20/4090）、sm90（Hopper，H100/H200）、sm100/120（Blackwell，5090）。「sm>=80」表示 Ampere 及以后都支持。
- **自动调优（autotune）**：对一组 BLOCK_M/BLOCK_N、num_warps/num_stages 等候选配置实测耗时，挑最快的缓存起来。FFPA 的 Triton 后端支持，其余后端不支持。
- **MMA / 累加器精度（acc）**：见 [u1-l1](./u1-l1-what-is-ffpa-split-d.md)，手写 CUDA 后端可设 `acc='f16'` 或 `acc='f32'`。

> 一句话定位：FFPA = 一个公共 API + **四个可替换的 kernel 后端**。本讲就是这四个后端的「产品说明书」与「选型指南」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/README.md) | 顶层说明，其中 `Backends` 小节给出官方四后端能力矩阵与选型建议。 |
| [src/ffpa_attn/functional.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py) | 分发层核心。顶部 import 四后端、定义四个 `Backend` 子类与 `_BACKEND_MAP`、`FFPAAttnMeta.fallback()` 做回退判定、`_FFPAAttnFunc.forward` 按 backend 分发。 |
| [src/ffpa_attn/cuda/\_\_init\_\_.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cuda/__init__.py) | CUDA 后端入口。用 `try/except` 包裹 `from .. import _C`，并暴露运行时能力标志 `CUDA_FWD_AVAILABLE`。 |
| [src/ffpa_attn/cute/\_\_init\_\_.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/__init__.py) | CuTeDSL 后端入口。提供 `cute_forward_available()` / `cute_max_supported_head_dim()` 能力探测（被 `fallback()` 调用）。 |

> 本讲只做**总览与分发**，不深入任何一个 kernel 的内部实现——那是后续 u4/u5/u6/u7 各讲的任务。

## 4. 核心概念与源码讲解

### 4.1 四后端能力矩阵与定位

#### 4.1.1 概念说明

FFPA 用四个后端实现同一份注意力公式，但它们在「硬件覆盖、前后向、head_dim、自动调优、加速比」上各有取舍。理解这张矩阵，是判断「我这次调用会落到哪段代码」的前提。四个后端的分工可以这样记：

- **SDPA**：基线，也是**回退目标**。所有 FFPA 不擅长的小 D / 短序列场景都退回它。
- **CUDA**：手写 CUDA 前向 kernel，**只有前向**，跑在 sm80+，需要单独编译扩展 `ffpa_attn._C`。
- **Triton**：纯 Python（Triton DSL）的前向 + 反向，**默认后端**，跨架构（含 AMD ROCm），支持自动调优与持久化调优配置。
- **CuTeDSL**：基于 NVIDIA CUTLASS CuTe DSL 的前向 + 反向，在 Hopper（sm90）上做了 head_dim 专用特化，H200 上可达 **427 TFLOPS**，是当前最快路径但约束最多。

#### 4.1.2 核心流程

官方在 README 里直接给出了一张能力矩阵，选型时按「我的 GPU 是什么架构 + 我要前向还是前向+反向 + 要不要自动调优」三步查表即可：

```text
1. 看架构：sm>=75 才能上 SDPA；FFPA 三个加速后端都要 sm>=80；
          CuTeDSL 的 sm90 专用路径只在 Hopper 上启用。
2. 看反向：要做训练（需要 dQ/dK/dV）→ CUDA 后端排除；
          只要前向推理 → CUDA / CuTeDSL 都可。
3. 看自动调优：要持久化调优 → 只能选 Triton。
4. 综合推荐：默认 Triton；H200 训练大 D 选 CuTeDSL；纯前向推理 sm80~89/120 选 CUDA。
```

#### 4.1.3 源码精读

四后端能力矩阵写在 README 的 `Backends` 小节：

[README.md:85-101](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/README.md#L85-L101) — 官方四后端能力矩阵，列出每个后端的架构、前向、反向、head_dim、自动调优、加速比与推荐场景。

把这张表整理成中文对照表：

| 后端 | 架构 | 前向 | 反向 | head_dim | 自动调优 | 加速比 | 推荐 |
| :--: | :--: | :--: | :--: | :--: | :--: | :--: | :--: |
| **SDPA** | sm>=75 | ✔ | ✔ | 全部 | ❌ | **1.0x**（基线） | sm>=75 |
| **CUDA** | sm>=80 | ✔ | ❌ | 320~1024 | ❌ | **1.5~3x** | sm80~89, sm120 |
| **Triton** | sm>=80 | ✔ | ✔ | 320~1024 | ✔ | **1.5~5x** | sm>=80 |
| **CuTeDSL** | sm>=80 | ✔ | ✔ | 320~1024 | ❌ | **1.5~2x** | sm80~89, sm120 |
| **CuTeDSL** | sm90 | ✔ | ✔ | 320~512 | ❌ | **3~6x** | sm90（如 H200） |

注意 CuTeDSL 占了两行：它的 **sm90 专用路径** 只覆盖到 head_dim ≤ 512，但加速比最高（3~6x）；head_dim > 512 或非 Hopper 架构则退到 SM80 通用 Split-D 路径，加速比回落到 1.5~2x。这与代码里 `cute_max_supported_head_dim()` 始终返回 SM80 的 1024 上限一致（见 4.3 节）。

README 还特别说明：**Triton 后端在 AMD GPU 上也能跑**，安装到 ROCm 版 PyTorch 即可自动分发到 Triton AMD：

[README.md:103-104](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/README.md#L103-L104) — 说明 Triton 前向+反向在 AMD ROCm/HIP 上同样可用，这是「默认选 Triton」的另一个理由：跨厂商。

最后，README 给出选型的用法示例——传一个 `Backend` 配置实例给 `ffpa_attn_func`：

[README.md:106-112](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/README.md#L106-L112) — `o = ffpa_attn_func(q, k, v, backend=CuTeDSLBackend())`，对应 `from ffpa_attn import ffpa_attn_func, CuTeDSLBackend`。

#### 4.1.4 代码实践

**目标**：把 README 的能力矩阵亲手抄一遍并理解每一列含义。

**步骤**：

1. 打开 [README.md](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/README.md) 的 `Backends` 小节（约 85-101 行）。
2. 在你的学习笔记里，按 `Backend / Arch / Fwd / Bwd / Headdim / Autotune / Speedup / Recommend` 八列复刻这张表。CuTeDSL 要拆成 sm90 与 sm80+ 两行。
3. 对每个后端写一句话「为什么它被推荐在那个场景」（例如 CUDA：「前向快、但没反向，适合只推理的 sm80~89/120 卡」）。

**预期结果**：你得到一张与本讲 4.1.3 节一致的对照表，并且能口述每个后端的定位。**待本地验证**（纯阅读型实践，无需运行 GPU）。

#### 4.1.5 小练习与答案

**Q1**：在一张 sm90 的 H200 上做 head_dim=512 的**训练**（需要反向），README 推荐哪个后端？为什么不是 CUDA？

> **答**：推荐 **CuTeDSL**（sm90 行，head_dim ≤ 512 命中，加速比 3~6x）。CUDA 后端**没有反向**（`Bwd=❌`），无法支撑训练，只能用于纯前向推理。

**Q2**：我没有 NVIDIA 卡，只有 AMD Instinct MI250X，能用 FFPA 加速吗？

> **答**：能，用 **Triton** 后端。README 的 ROCm 说明指出 Triton 前/反向在 AMD GPU 上自动可用。CUDA / CuTeDSL 都不支持 AMD。

---

### 4.2 分发层如何把四个后端挂进来：import 与 try/except

#### 4.2.1 概念说明

四个后端的实现分散在不同的子包里（`aten` / `triton` / `cute` / `cuda`）。分发层 `functional.py` 要做的第一件事，就是把它们的入口函数**全部 import 到顶部**，形成一个统一的「后端函数表」。但这里有个工程难点：**不是所有后端在所有安装方式下都可用**——比如默认 Triton-only 构建不会编译 `_C`，CuTeDSL 也可能因为依赖缺失而不可用。因此 import 必须用 **`try/except` 容错**，让「可选后端缺失」不至于让整个包 import 失败。

#### 4.2.2 核心流程

`functional.py` 顶部的 import 分三类：

```text
1. triton / aten：FFPA 的核心依赖，直接 import（这两个总是可用）。
   - aten：负责 D<=256 的小 D 路径（回退/前置）。
   - triton：大 D 默认前向 + 反向。

2. cute（CuTeDSL）：可选依赖，用 try/except 包裹。
   缺失时把三个入口函数置为 None，后续分发时跳过即可。

3. cuda（手写 CUDA 前向）：可选依赖，用 try/except 包裹。
   缺失（即没编译 _C）时 _ffpa_attn_forward_cuda = None。
```

紧接着是一组**常量**，它们定义了「大 D / 小 D」的分界与 MMA 累加器编码，是后续回退判定和分发的基础。

#### 4.2.3 源码精读

[functional.py:18-26](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L18-L26) — 直接 import **Triton**（`_ffpa_attn_forward_triton` / `_ffpa_attn_backward_triton`）和 **aten**（`_flash_attn_forward_aten` 等，负责 D≤256）。这两个是核心依赖，不包 try/except。注释点明「默认大 D，小 D 需 `FFPA_TRITON_ALLOW_SMALL_D=1`」。

[functional.py:27-36](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L27-L36) — **CuTeDSL** 的三个入口（前向 / 反向 / varlen）用 `try/except Exception` 包裹，失败时全部置 `None`。这样 CuTeDSL 依赖缺失也不会影响包的 import。

[functional.py:38-41](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L38-L41) — **手写 CUDA** 前向入口同样用 `try/except`，缺失时 `_ffpa_attn_forward_cuda = None`。注意这里只 import 了 forward——CUDA 后端本来就只有前向。

紧接着的分界常量：

[functional.py:47-50](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L47-L50) — `_ACC_F16=0`、`_ACC_F32=1`（与 C++ 侧 `ffpa_attn_api.cc` 的累加器编码同步）、`_ATEN_SMALL_HEAD_DIM_MAX=256`（小 D 上限）、`_FFPA_SMALL_HEAD_DIM_MIN=64`。**256 这条线就是 SDPA/aten 与 FFPA 大 D kernel 的分界**。

四个后端各自对应一个 `Backend` 配置子类，名字与 import 一一对应：

[functional.py:132-148](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L132-L148) — `SDPABackend`，`name="sdpa"`，前向永远经 `fallback()` 短路到原生 SDPA。

[functional.py:150-171](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L150-L171) — `CUDABackend`，`name="cuda"`，带 `acc`（默认 `f32`）、`stages`（Hopper 取 4，否则 3）字段，并在 `__post_init__` 里 `assert not self.backward`（**CUDA 不支持反向**）。

[functional.py:174-219](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L174-L219) — `TritonBackend`，`name="triton"`，字段最多：`autotune` / `autotune_mode` / `enable_tma` / `enable_ws` / `persist_dkdv` / `split_launch` / `preprocess_d_chunk` / `grad_kv_storage_dtype` / `grad_q_storage_dtype`，是**默认后端**。

[functional.py:221-242](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L221-L242) — `CuTeDSLBackend`，`name="cutedsl"`，注意**目录名是 `cute`，对外后端名是 `"cutedsl"`**（见 [u1-l3](./u1-l3-repo-layout-code-map.md)）。

字符串名到类的映射在 `_coerce_backend` 里：

[functional.py:284-305](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L284-L305) — `_BACKEND_MAP` 把 `"cuda"/"triton"/"cutedsl"/"sdpa"` 四个字符串映射到对应类，所以你可以用 `backend="triton"` 字符串简写，也可以直接传实例 `backend=TritonBackend()`。

#### 4.2.4 代码实践

**目标**：确认四个后端在你当前安装下的可用性。

**步骤**：

1. 在装好 ffpa_attn 的环境里运行（无需 GPU 也行，主要是看 import 是否成功）：

   ```python
   # 示例代码
   from ffpa_attn import functional as F
   print("triton fwd:", F._ffpa_attn_forward_triton is not None)
   print("aten fwd  :", F._flash_attn_forward_aten is not None)
   print("cute fwd  :", F._ffpa_attn_forward_cute is not None)
   print("cuda fwd  :", F._ffpa_attn_forward_cuda is not None)
   ```

2. 对比输出与你的安装方式（Triton-only vs `ENABLE_FFPA_CUDA_IMPL=1`）。

**预期结果**：`triton` 与 `aten` 恒为 `True`；`cute` 取决于 CuTeDSL 依赖；`cuda` 仅在 `ENABLE_FFPA_CUDA_IMPL=1` 构建时为 `True`，否则为 `None`。**待本地验证**（取决于你的安装方式）。

#### 4.2.5 小练习与答案

**Q1**：为什么 `triton` 和 `aten` 不包 `try/except`，而 `cute` 和 `cuda` 要包？

> **答**：`triton` / `aten` 是 FFPA 的**核心依赖**，任何安装都必须可用，缺失即视为安装损坏，应该直接报错。`cute` / `cuda` 是**可选后端**：CuTeDSL 依赖可能未装、`_C` 可能没编译，用 `try/except` 把入口置 `None`，让包仍能正常 import，只是运行时该后端不可用。

**Q2**：`CUDA_BWD_AVAILABLE` 在 `cuda/__init__.py` 里被定义但没有真正实现反向，这和 `CUDABackend` 的哪个断言一致？

> **答**：和 [functional.py:164](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L164) 的 `assert not self.backward` 一致——CUDA 后端**只有前向**，构造 `CUDABackend(backward=True)` 会直接断言失败。

---

### 4.3 运行时能力探测：CUDA_FWD_AVAILABLE 与 CuTeDSL 探测

#### 4.3.1 概念说明

import 阶段的 `try/except` 只能告诉我们「这个后端的代码有没有被加载进来」，但不能告诉我们「这次调用能不能在当前 GPU 上真正跑起来」。比如 `_C` 编译进来了，但当前机器可能根本没有 CUDA 卡；CuTeDSL 代码加载了，但当前卡可能是 sm75，不满足 sm>=80。因此需要**运行时能力探测标志**：`CUDA_FWD_AVAILABLE` 与 `cute_forward_available()`。它们是分发层在「选后端」之外的第二道闸门。

#### 4.3.2 核心流程

CUDA 后端的探测分两层：

```text
try: from .. import _C            # 第一层：扩展是否编译进来
     CUDA_FWD_AVAILABLE = _C.CUDA_FWD_AVAILABLE   # 第二层：编译期上报的可用性
except Exception:
     CUDA_FWD_AVAILABLE = False
```

`_C.CUDA_FWD_AVAILABLE` 是 **C++ 侧在编译期写死并暴露给 Python 的常量**——它综合了「有没有 GPU、nvcc 编译时是否真的生成了前向 kernel」等信息。CuTeDSL 则是**纯运行时探测**：直接读 `torch.cuda.get_device_capability()`，判断 major >= 8。

#### 4.3.3 源码精读

[cuda/__init__.py:4-15](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cuda/__init__.py#L4-L15) — CUDA 后端的 import 与能力探测核心。`CUDA_FWD_AVAILABLE` 从 `_cuda_ext.CUDA_FWD_AVAILABLE` 读取（编译期上报），任何异常都降级为 `False`。`CUDA_BWD_AVAILABLE` 恒为 `False`，呼应「CUDA 只有前向」。

当用户显式选了 CUDA 后端但扩展没编译时，调用点会给出明确的修复指引：

[cuda/__init__.py:46-51](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cuda/__init__.py#L46-L51) — 报错信息直接告诉用户 `Rebuild with ENABLE_FFPA_CUDA_IMPL=1 to enable it.`，并把原始 import 异常附在后面，便于排查。

CuTeDSL 的探测在 `cute/__init__.py`：

[cute/__init__.py:128-146](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/__init__.py#L128-L146) — `cute_forward_available(device)`：先确认有 CUDA、device 是 cuda，再取 `get_device_capability` 的 major，**`major >= 8`** 才返回 `True`（即 Ampere 及以后）。注意这只检查「设备级前置条件」，head_dim 上限 / dtype / 不支持 mask+dropout 等约束由调用时的 `_require_cute_supported` 另行校验。

[cute/__init__.py:149-160](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/__init__.py#L149-L160) — `cute_max_supported_head_dim(device)` 始终返回 `SM80_SUPPORTED_HEAD_DIM`。结合 [_utils.py:25-32](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_utils.py#L25-L32)（`SM90_SUPPORTED_HEAD_DIM=512`、`SM80_SUPPORTED_HEAD_DIM=1024`），可知：sm90 专用路径只到 512，超过 512 或非 Hopper 都退到 SM80 通用路径（上限 1024）。这就解释了 README 里 CuTeDSL 为什么占两行。

这两个探测函数在 `fallback()` 里被真正消费：

[functional.py:501-513](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L501-L513) — 当 `forward_meta.name == "cutedsl"` 时，`fallback()` 调用 `cute_forward_available()` 与 `cute_max_supported_head_dim()`，只要设备不支持或 D 超出上限就回退（这里 CuTeDSL 是**回退到 SDPA**，而不是报错）。

#### 4.3.4 代码实践

**目标**：观察 CUDA 后端「未编译」时的报错路径。

**步骤**：

1. 若你当前是 Triton-only 安装，在 Python 里检查标志（模拟误选 CUDA 后端的前置条件）：

   ```python
   # 示例代码（仅当 _C 未编译时演示报错）
   from ffpa_attn.cuda import CUDA_FWD_AVAILABLE
   print("CUDA_FWD_AVAILABLE =", CUDA_FWD_AVAILABLE)
   # 若为 False，再读 cuda/__init__.py 第 46-51 行的报错文案
   ```

2. 阅读 [cuda/__init__.py:46-51](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cuda/__init__.py#L46-L51) 的报错文案，确认它告诉用户的修复命令。

**预期结果**：Triton-only 下 `CUDA_FWD_AVAILABLE=False`；若强行调 CUDA op，得到带 `Rebuild with ENABLE_FFPA_CUDA_IMPL=1` 的 `RuntimeError`。**待本地验证**（取决于是否编译了 `_C`）。

#### 4.3.5 小练习与答案

**Q1**：`CUDA_FWD_AVAILABLE` 与 `cute_forward_available()` 一个是「编译期常量」、一个是「运行时函数」，为什么设计不同？

> **答**：CUDA 后端是**预编译的 C++ 扩展**，kernel 是否生成在编译期就定了，所以用编译期写死的 `_C.CUDA_FWD_AVAILABLE` 上报。CuTeDSL 是**运行时 JIT**（CuTe DSL 在运行时生成代码），没有「编译期」可言，只能在运行时读 `get_device_capability()` 判断 major >= 8。

**Q2**：在 sm75（Turing，如 T4）的卡上，CuTeDSL 后端会被选中吗？

> **答**：不会。`cute_forward_available()` 要求 `major >= 8`，sm75 的 major=7 不满足，`fallback()` 会回退到 SDPA。

---

### 4.4 前向分发决策树与「为什么默认是 Triton」

#### 4.4.1 概念说明

把 4.1~4.3 串起来：用户调用 `ffpa_attn_func` 时，先由 `FFPAAttnMeta` 解析出 `forward_meta` / `backward_meta`（默认都是 Triton），再由 `fallback()` 决定要不要直接走 SDPA，最后才进入 `_FFPAAttnFunc.forward` 按 backend 分发到具体 kernel。理解这条链路，就能回答一个高频问题：**既然 CUDA 前向很快（1.5~3x），为什么默认后端不是 CUDA 而是 Triton？**

#### 4.4.2 核心流程

前向分发的决策树（伪代码）：

```text
head_dim = q.size(-1)
if head_dim <= 256 且未开启 allow_small_d:
    → aten flash 前向（_flash_attn_forward_aten）        # 小 D
elif forward_meta 是 CUDABackend:
    → _ffpa_attn_forward_cuda                            # 手写 CUDA
elif forward_meta 是 TritonBackend:
    → _ffpa_attn_forward_triton                          # 默认大 D
elif forward_meta 是 CuTeDSLBackend:
    → _ffpa_attn_forward_cute                            # CuTeDSL
else:
    → raise
```

注意 `D<=256` 的 aten 分支**优先级最高**，不看 backend 类型——这是 FFPA 与 SDPA 互补的硬线（见 [u1-l1](./u1-l1-what-is-ffpa-split-d.md)）。

#### 4.4.3 源码精读

前向分发主逻辑：

[functional.py:759-829](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L759-L829) — `_FFPAAttnFunc.forward` 的四分支分发：先用 `_should_use_aten_small_d_forward` 判小 D → aten（[L764](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L764)）；再依次判 `CUDABackend`（[L774](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L774)）/ `TritonBackend`（[L793](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L793)）/ `CuTeDSLBackend`（[L813](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L813)）；都不命中则 `raise`。每个分支调用各自后端的入口函数，并把 `O`、`lse`、`rng_state` 等保存供反向用。

「默认 Triton」的根因之一：未指定 backend 时，`_resolve_backend_pair` 把 `None` 补成 `TritonBackend`：

[functional.py:253-281](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L253-L281) — `forward_backend` / `backward_backend` 为 `None` 时默认构造 `TritonBackend`；同时强制约束「CuTeDSL 必须前后向都用 CuTeDSL」（不能只配一侧）。

为什么默认**不能**是 CUDA？代码里写得很直白——前向与反向是**多对多**关系，一个前向后端可能配多种反向后端：

[functional.py:946-968](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L946-L968) — 这段注释列出前向↔反向的能力矩阵：`cuda → triton/sdpa`、`triton → triton/sdpa`、`cutedsl → cutedsl/triton/sdpa`。CUDA 后端**没有自己的反向**，必须配 Triton 或 SDPA 反向。`_ffpa_apply` 用 `@torch._dynamo.disable` 在 autograd 边界制造图断，使真正的 `_FFPAAttnFunc.backward`（含完整 backend 分发）能在 eager 下运行——这正是默认选 Triton（前向+反向齐全）而非 CUDA 的核心理由。

#### 4.4.4 代码实践

**目标**：追踪一次默认调用的前向分发落点。

**步骤**：

1. 在有大 D（如 D=512）输入的场景下，用默认参数调用：

   ```python
   # 示例代码
   import torch
   from ffpa_attn import ffpa_attn_func
   q = torch.randn(1, 32, 8192, 512, dtype=torch.bfloat16, device="cuda")
   k = torch.randn(1, 32, 8192, 512, dtype=torch.bfloat16, device="cuda")
   v = torch.randn(1, 32, 8192, 512, dtype=torch.bfloat16, device="cuda")
   o = ffpa_attn_func(q, k, v)   # 不传 backend → 默认 Triton
   ```

2. 对照 [functional.py:793-812](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L793-L812) 的 `TritonBackend` 分支（脑内断点），确认这次调用走的是 `_ffpa_attn_forward_triton` 而非 `_ffpa_attn_forward_cuda`。

**预期结果**：输出 `o` 形状为 `[1, 32, 8192, 512]`；分发落到 Triton 分支。若改传 `backend="cuda"`（且已编译 `_C`），则落到 CUDA 分支，但**反商会因 `assert not self.backward` 失败而报错**——这就直观体现了「默认不能是 CUDA」。**待本地验证**（需要 GPU 与对应 head_dim）。

#### 4.4.5 小练习与答案

**Q1**：用三句话解释「为什么默认是 Triton 而非 CUDA」。

> **答**：(1) CUDA 后端**只有前向**（`CUDABackend` 断言 `not backward`），无法独立支撑训练，必须配 Triton/SDPA 反向；(2) CUDA 后端需要 `ENABLE_FFPA_CUDA_IMPL=1` 单独编译 `_C`，默认 Triton-only 构建里 `_ffpa_attn_forward_cuda` 是 `None`，根本调不动；(3) Triton 是纯 Python JIT，跨架构（含 AMD ROCm）、支持自动调优与持久化调优配置、前向反向齐全，覆盖面最广，是最稳妥的默认值。

**Q2**：`forward_backend='cutedsl'` 但没指定 `backward_backend`，会发生什么？

> **答**：`from_kwargs` 会自动把 `backward_backend` 也补成 `CuTeDSLBackend`（见 [functional.py:439-442](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L439-L442)），因为 `_resolve_backend_pair` 强制 CuTeDSL 前后向必须成对出现。

---

## 5. 综合实践

**任务**：画一张完整的四后端能力对照表，并写一段「为什么默认是 Triton 而非 CUDA」的论证。

**操作步骤**：

1. 对照 [README.md:91-97](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/README.md#L91-L97)，在你的笔记里画一张表，列为 `Backend / Arch / Fwd / Bwd / Headdim / Autotune / Speedup / Recommend`。CuTeDSL 要拆成 sm90 与 sm80+ 两行。

2. 额外补一列「关键源码锚点」，把每个后端填上：
   - SDPA：[functional.py:132-148](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L132-L148)（`SDPABackend`，前向总走 `fallback`）。
   - CUDA：[functional.py:150-171](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L150-L171)（`CUDABackend`，`assert not backward`）+ [cuda/__init__.py:4-15](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cuda/__init__.py#L4-L15)（`CUDA_FWD_AVAILABLE`）。
   - Triton：[functional.py:174-219](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L174-L219)（默认后端，字段最多）。
   - CuTeDSL：[functional.py:221-242](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L221-L242) + [cute/__init__.py:128-160](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/__init__.py#L128-L160)（能力探测）。

3. 写一段 5~8 行的论证，回答「为什么默认是 Triton 而非 CUDA」。要点参考 4.4.5 的 Q1，并引用 [functional.py:946-968](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L946-L968) 的前向↔反向多对多关系作为论据。

**预期结果**：一张带源码锚点的九列对照表 + 一段有据可查的论证。**待本地验证**（纯阅读 + 写作型实践）。

## 6. 本讲小结

- FFPA 有 **四个后端**：SDPA（基线/回退，sm>=75，全 head_dim）、CUDA（手写，**仅前向**，sm>=80，D 320~1024，需单独编译 `_C`）、Triton（默认，前向+反向，sm>=80，含 AMD ROCm，支持自动调优）、CuTeDSL（前向+反向，sm80+ 通用 / sm90 专用，H200 上最快）。
- **默认后端是 Triton**：因为它前向反向齐全、纯 Python JIT 无需单独编译、跨厂商、支持自动调优；CUDA 没有反向且需要 `ENABLE_FFPA_CUDA_IMPL=1` 编译，不能当默认。
- 分发层 `functional.py` 顶部用**直接 import** 挂核心依赖（triton/aten），用 **`try/except`** 挂可选后端（cute/cuda），缺失时入口置 `None`，保证包能 import。
- 运行时能力由两套探测把关：CUDA 用编译期常量 `CUDA_FWD_AVAILABLE`，CuTeDSL 用运行时函数 `cute_forward_available()`（判 `major>=8`）与 `cute_max_supported_head_dim()`（sm90 限 512、其余 1024）。
- `_FFPAAttnFunc.forward` 按「`D<=256`→aten」优先，再按 `CUDABackend`/`TritonBackend`/`CuTeDSLBackend` 四分支分发；前向↔反向是多对多关系，`_ffpa_apply` 用 `torch._dynamo.disable` 在 autograd 边界断图。
- 选型口诀：默认 Triton；H200 训练大 D 选 CuTeDSL；纯前向推理 sm80~89/120 可选 CUDA；小 D / 短序列自动回退 SDPA。

## 7. 下一步学习建议

本讲建立了四后端的全局地图与分发框架。接下来建议：

- **先看分发层细节**：[u3-l2 后端配置类体系](./u3-l2-backend-config-dataclasses.md) 精读四个 `Backend` 子类的字段与校验，[u3-l3 FFPAAttnMeta：输入校验与 SDPA 回退判定](./u3-l3-meta-normalize-and-fallback.md) 精读 `fallback()` 全部回退条件，[u3-l4 autograd Function 前向/反向分发](./u3-l4-autograd-function-dispatch.md) 完整跟踪 `forward`/`backward`。
- **再深入默认后端的 kernel**：第 4 单元（Triton 前向）与第 5 单元（Triton 反向）是理解 FFPA 性能来源的核心。
- **按需选读专家层**：第 6 单元（CuTeDSL）、第 7 单元（手写 CUDA 与构建系统）分别对应「最快路径」与「最快前向」，可在需要时再展开。
