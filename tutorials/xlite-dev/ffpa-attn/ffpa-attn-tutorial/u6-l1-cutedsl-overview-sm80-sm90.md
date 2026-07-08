# CuTeDSL 后端总览与 SM80/SM90 分发

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 **CuTeDSL 后端**在整个 FFPA 四后端版图里的定位（基于 CUTLASS CuTe DSL，H200 上最快、约束最多）。
- 读懂 `cute/__init__.py` 里三个**能力探测函数**——`cute_forward_available`、`cute_backward_available`、`cute_max_supported_head_dim`——分别负责哪一层门禁。
- 读懂 `_require_cute_supported` 这一**逐调用张量级硬校验**：它在 (arch, head_dim, dtype) 三个维度上各卡了什么、为何只报错不回退。
- 掌握 `_use_sm90_specialized` 这条**单一分发谓词**，并能据此判断任意 `(major, D, D_v)` 组合走「SM90 专用路径」还是「SM80 通用 Split-D 回退路径」。

本讲只讲 CuTeDSL 后端的**入口与分发**，不深入具体 kernel（d384/d512/generic、tile scheduler、pipeline 留给 u6-l3、u6-l4）。承接 u3-4 的「`_FFPAAttnFunc` 前/反向分发」与 u2-l5 的「varlen 逐层校验」结论，本讲只补 CuTeDSL 专属的那一环。

## 2. 前置知识

- **compute capability（SM 架构号）**：NVIDIA GPU 的型号标识，写作 `major.minor`，如 `8.0`（Ampere，A100/A30）、`8.9`（Ada，L20/4090）、`9.0`（Hopper，H100/H200）、`12.0`（Blackwell，RTX PRO 6000）。`major` 是架构大代。本讲里 `major >= 8` 即「Ampere 及以后」。
- **CUTLASS / CuTe DSL**：NVIDIA 开源的高性能矩阵乘库 CUTLASS 提供的 Python「CuTe DSL」，可在运行期用 Python 描述并 JIT 出 GPU kernel，封装了 WGMMA、TMA 等 Hopper 专用指令。CuTeDSL 后端就是用它写出的 FFPA attention kernel。
- **WGMMA / TMA / cp.async**：Hopper 的 warpgroup MMA（WGMMA）与异步张量内存加速器（TMA）是 SM90a 专用指令；Ampere 的 warp 级 MMA 与 `cp.async` 则向前兼容到 SM89/SM90/SM120。这正是 CuTeDSL 要分「SM90 专用」与「SM80 通用」两条路径的硬件根因。
- **回退（fallback）vs 拒绝（raise）**：FFPA 的 Triton 后端在不适合时**静默回退 SDPA**（见 u1-l4、u3-3）；而 CuTeDSL 后端对**不支持的特性**（attn_mask、dropout、未对齐 head_dim 等）一律**抛错**，从不静默降级——这是本讲反复出现的「无静默回退」原则。
- **`[B,H,N,D]` 与 `[B,N,H,D]` 布局**：SDPA 风格头在序列前 `[B,H,N,D]`，FlashAttention / CuTeDSL 原生头在序列后 `[B,N,H,D]`。CuTeDSL 入口在两侧做 transpose 转换（见 u6-l2，本讲只提一句）。

## 3. 本讲源码地图

| 文件 | 本讲用到的部分 | 作用 |
|---|---|---|
| [src/ffpa_attn/cute/\_\_init\_\_.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/__init__.py) | 全部 4 个最小模块都在这里 | CuTeDSL 后端的入口 shim：能力探测、校验、SM80/SM90 分发、torch custom op 注册 |
| [src/ffpa_attn/cute/\_utils.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_utils.py) | 常量与 `dense_min_supported_head_dim` | 提供 `MIN_SUPPORTED_HEAD_DIM=320`、`SM90_SUPPORTED_HEAD_DIM=512`、`SM80_SUPPORTED_HEAD_DIM=1024`、`SM80_FWD_SPLIT_D_CHUNK=32` 等门禁常量 |
| [src/ffpa_attn/cute/README.md](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/README.md) | 路由图、硬约束表、Blackwell 调研笔记 | 用文档语言复述本讲的分发规则与已知限制 |
| [src/ffpa_attn/functional.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py) | `try/except` 导入、`CuTeDSLBackend`、`fallback()`、前/反向分发 | Python 分发层如何把调用接驳进 CuTeDSL 入口 |

> 提醒：Python **包名**是 `ffpa_attn.cute`，但对外**后端名字符串**是 `"cutedsl"`、配置类是 `CuTeDSLBackend`——三者不字字对应，这是历史命名，记住即可（[src/ffpa_attn/cute/README.md:9-10](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/README.md#L9-L10)）。

## 4. 核心概念与源码讲解

### 4.1 CuTeDSL 后端定位与接驳

#### 4.1.1 概念说明

CuTeDSL 是 FFPA 四后端里**性能上限最高、约束也最多**的一个。它用 NVIDIA CUTLASS 的 CuTe DSL 在运行期 JIT 出 GPU kernel，能直接用上 Hopper 的 WGMMA + TMA，因此在 H200 SXM、`D=512` 大 head_dim 场景下可达 **427 TFLOPS**，是当前最快路径（[README.md:87](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/README.md#L87)）。代价是：

- 只支持 fp16/bf16；
- 不支持 attn_mask、不支持 dropout（`dropout_p` 必须 0）；
- head_dim 必须落在特定区间且（SM80 路径下）对齐 32；
- 为保持 torch.compile/autograd 兼容，前后向被「对称强制」绑定（见 u3-l2）。

它内部又分**两条 kernel 路径**：

- **SM90 专用路径**（`_ffpa_attn_forward_sm90` / `_ffpa_attn_backward_sm90`）：针对 Hopper、对称 `D≤512` 做了 head_dim 级特化（d384/d512/generic），加速比最高；
- **SM80 通用 Split-D 路径**（`_ffpa_attn_forward_sm80` / `_ffpa_attn_backward_sm80`）：用 Ampere 级 warp MMA + cp.async 写的通用 Split-D，向前兼容到 SM89/SM90/SM120，覆盖**所有其它支持的架构**以及 **SM90 上 `D>512`** 的情况，是兜底路径。

本讲的全部 4 个最小模块，本质上都在回答同一个问题：**给一个调用，该不该用 CuTeDSL？该用哪条路径？**

#### 4.1.2 核心流程：一次调用如何从分发层走到 CuTeDSL

承接 u3-4 的 `_FFPAAttnFunc.forward`，dense 路径的接驳如下（varlen 路径见 u2-l5、u6-l2）：

```
ffpa_attn_func(..., backend="cutedsl")
  └─ FFPAAttnMeta.fallback()          # hardware/head_dim 门禁：用 cute_forward_available / cute_max_supported_head_dim
       └─ normalize_inputs()          # attn_mask / dropout 在此直接抛错（无静默回退）
            └─ _FFPAAttnFunc.forward  # isinstance(forward_meta, CuTeDSLBackend)
                 └─ _ffpa_attn_forward_cute(q,k,v,...)      # cute/__init__.py
                      ├─ _require_cute_supported(q,k,v,...) # 张量级硬校验
                      ├─ _bhnd_to_bnhd(...)                 # [B,H,N,D]→[B,N,H,D]
                      └─ torch.ops.ffpa_attn._fwd_cute(...)
                           └─ _forward_impl_for_device(...) # 选 sm90 还是 sm80
                                ├─ _ffpa_attn_forward_sm90  # _use_sm90_specialized 为真
                                └─ _ffpa_attn_forward_sm80  # 否则
```

可以看到，门禁是**三层叠加**的：

1. **device 级**：`cute_forward_available` / `cute_backward_available`（4.2）；
2. **head_dim 上限**：`cute_max_supported_head_dim`（4.2）；
3. **逐调用张量级**：`_require_cute_supported`（4.3）。

而路径选择只由一个谓词 `_use_sm90_specialized`（4.4）决定。这套设计的关键词是：**能力探测只管「能不能用」，路径选择只管「用哪条」，校验只管「这次调用合不合法」**，三者职责分明。

#### 4.1.3 源码精读：分发层如何接驳

CuTeDSL 入口在 `functional.py` 里用 `try/except` 包裹导入，失败时置 `None`，实现**优雅降级**——没装 CUTLASS 也不会让整个包 import 失败：

[src/ffpa_attn/functional.py:L27-L36](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L27-L36) — `try/except` 导入三个 CuTeDSL 入口（dense fwd / dense bwd / varlen），失败置 `None`。

`fallback()` 在判 CuTeDSL 时，把硬件与 head_dim 上限的判定**委托**给本讲的能力探测函数：

[src/ffpa_attn/functional.py:L501-L513](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L501-L513) — CuTeDSL 分支：`D > cute_max_supported_head_dim(...)` 或 `not cute_forward_available(...)` 即回退。

注意：**attn_mask / dropout 不在 fallback 里**，而是在 `normalize_inputs` 里直接 `raise NotImplementedError`（[src/ffpa_attn/functional.py:L554-L563](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L554-L563)）。这就是「硬件不匹配→静默回退 SDPA」与「特性不支持→直接报错」的分野。

真正进入 CuTeDSL 的两处分发：

[src/ffpa_attn/functional.py:L813-L825](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L813-L825) — 前向：`isinstance(meta.forward_meta, CuTeDSLBackend)` 时调 `_ffpa_attn_forward_cute`。

[src/ffpa_attn/functional.py:L890-L904](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L890-L904) — 反向：`isinstance(meta.backward_meta, CuTeDSLBackend)` 时调 `_ffpa_attn_backward_cute`。

#### 4.1.4 代码实践

1. **目标**：在不用真实 GPU 的情况下，确认「CuTeDSL 后端的入口被分发层正确接驳」。
2. **步骤**：阅读上述 4 段源码链接，用笔把 `ffpa_attn_func(backend="cutedsl")` 从入口到 `_forward_impl_for_device` 的调用链补全。
3. **观察**：注意 `fallback()` 调的是 `cute_forward_available` / `cute_max_supported_head_dim`（**只**判硬件与 head_dim 上限），而 attn_mask/dropout 在**更早**的 `normalize_inputs` 里就抛错——二者不在同一层。
4. **预期**：你能指出「硬件不支持→回退」与「特性不支持→报错」分别发生在哪两个函数里。
5. 本实践为纯源码阅读，无需 GPU，**可在任意环境完成**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 CuTeDSL 入口要用 `try/except` 导入，而不是像 Triton 那样直接 `import`？

> **答**：CuTeDSL 依赖外部 CUTLASS（`cutlass` 包）与 `tvm_ffi`，并非所有安装都具备；`try/except` 让缺依赖时把入口置 `None`，分发层据此判定「后端不可用」并回退，而不是让 `import ffpa_attn` 整体失败。Triton 是 FFPA 的默认核心依赖，必然存在，故无需包裹。

**练习 2**：`forward_backend="cutedsl"` 但 `backward_backend="triton"` 会怎样？

> **答**：直接抛 `ValueError`。`_resolve_backend_pair` 强制 CuTeDSL 前后向对称（[src/ffpa_attn/functional.py:L272-L279](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L272-L279)），原因是其反向需要前向保存的 LSE，跨后端无法配对（详见 u3-l2、u3-l5）。

---

### 4.2 能力探测：cute_forward_available / cute_backward_available / cute_max_supported_head_dim

#### 4.2.1 概念说明

这三个函数回答**最粗粒度**的两个问题：

- **`cute_forward_available(device)` / `cute_backward_available(device)`**：这台设备的 GPU **能不能**跑 CuTeDSL？只看 device 级前提（`compute capability >= 8.0`），**不看** head_dim / dtype / 特性。设计目的是让调用方在**分配张量之前**就能预先选后端。
- **`cute_max_supported_head_dim(device)`**：CuTeDSL 在这台设备上支持的最大 dense head_dim 是多少？答案是常量 `1024`（SM80 上限），与设备无关。

注意它们只覆盖「设备/head_dim 上限」这一层；逐调用的 dtype、head_dim 落点、特性校验**不在**这里，而在 `_require_cute_supported`（4.3）。这是典型的**分层校验**：便宜的、设备级的判定放前面，贵的、张量级的判定放后面。

#### 4.2.2 核心流程

`cute_forward_available` 的判定是一条短路链：

```
cuda 是否可用？ ─否→ False
   └是→ device 为 None？─是→ 取 current_device
              └否→ device.type != "cuda"？─是→ False
                          └否→ major, _ = get_device_capability(device)
                                return major >= 8
```

`cute_backward_available` 与之**完全镜像**（同样的四步、同样的 `major >= 8`），只是 docstring 说明它管反向。`cute_max_supported_head_dim` 则直接 `del device; return SM80_SUPPORTED_HEAD_DIM`——参数仅为 API 兼容保留，**当前不依赖设备**。

#### 4.2.3 源码精读

[src/ffpa_attn/cute/\_\_init\_\_.py:L128-L146](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/__init__.py#L128-L146) — `cute_forward_available`：`torch.cuda.is_available()` → device 默认取 current → `device.type != "cuda"` 判 False → `get_device_capability` 取 `major` → `return major >= 8`。docstring 明说「只检查 device 级前提」，逐调用校验交给 `_require_cute_supported`。

[src/ffpa_attn/cute/\_\_init\_\_.py:L163-L178](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/__init__.py#L163-L178) — `cute_backward_available`：与 forward 逐行镜像，docstring 复述「SM90 保留 Hopper 专用反向 `D≤512`，其余架构与 `D>512` 走 SM80 反向」。

[src/ffpa_attn/cute/\_\_init\_\_.py:L149-L160](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/__init__.py#L149-L160) — `cute_max_supported_head_dim`：`del device; return SM80_SUPPORTED_HEAD_DIM`。docstring 解释了「为何恒为 SM80 值」：SM90 专用路径只覆盖到 `D≤512`，但 SM80 Split-D 兜底覆盖了剩余范围，故有效上限取两者之最大 = SM80 的 1024。

常量定义在 `_utils.py`：

[src/ffpa_attn/cute/\_utils.py:L21-L38](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_utils.py#L21-L38) — `MIN_SUPPORTED_HEAD_DIM=320`、`SM90_SUPPORTED_HEAD_DIM=512`、`SM80_SUPPORTED_HEAD_DIM=1024`、`SM80_FWD_SPLIT_D_CHUNK=32`。这是本讲全部门禁的「数字之源」。

#### 4.2.4 代码实践

1. **目标**：验证 `cute_forward_available` 在你的设备上返回什么，并理解它「只判 major」。
2. **步骤**：在有 CUDA GPU 的机器上运行：
   ```python
   import torch
   from ffpa_attn.cute import cute_forward_available, cute_backward_available, cute_max_supported_head_dim
   dev = torch.device("cuda", 0)
   print("cap:", torch.cuda.get_device_capability(dev))
   print("fwd:", cute_forward_available(dev))
   print("bwd:", cute_backward_available(dev))
   print("maxD:", cute_max_supported_head_dim(dev))
   ```
3. **观察**：`fwd`/`bwd` 应等于 `get_device_capability(dev)[0] >= 8`；`maxD` 恒为 `1024`。
4. **预期**：在 A100(8.0)/L20(8.9)/H200(9.0) 上三者分别为 True/True/1024；在一张假想的 sm75（T4）卡上 `fwd`/`bwd` 为 False。
5. **待本地验证**：若无 CUDA GPU，`torch.cuda.is_available()` 为 False，`cute_forward_available()` 直接返回 False；可改用源码静态推演。

#### 4.2.5 小练习与答案

**练习 1**：`cute_max_supported_head_dim` 为什么不依赖 `device`，却仍保留该参数？

> **答**：当前实现里 SM80 兜底路径覆盖所有支持架构，有效上限处处等于 `SM80_SUPPORTED_HEAD_DIM=1024`，与设备无关；保留 `device` 参数是为了「API 兼容」——未来若给 SM90 单独抬高上限、或对某些架构收紧，可不破坏调用方签名。

**练习 2**：`cute_forward_available` 返回 `True` 是否就意味着这次调用一定能跑 CuTeDSL？

> **答**：不是。它只保证「设备 major≥8」这个**必要条件**。head_dim 是否落在 `[320,1024]`、是否对齐 32、dtype 是否 fp16/bf16、是否带 attn_mask/dropout，都要由 `_require_cute_supported`（4.3）与 `normalize_inputs` 逐调用判定。`True` 只是「门开着」，不是「一定能进」。

---

### 4.3 _require_cute_supported：逐调用张量级硬校验

#### 4.3.1 概念说明

`_require_cute_supported` 是 CuTeDSL 的**张量级守门员**：在真正 transpose 与下发 op 之前，对 `q/k/v` 做 (device, arch, head_dim, dtype, 对称性) 的全套校验，**任何一项不满足都立即抛错**。它的设计哲学与 Triton 后端相反——**不回退、只报错**。理由是：用户显式写了 `forward_backend="cutedsl"`，说明他要的就是 CuTeDSL 的性能；此时若静默回退 SDPA，用户会在不知情下损失性能，比报错更糟。

它**不管**功能选项（attn_mask、dropout、window_size 等），那部分由 `_check_supported_options` 在更外层的入口 shim 负责（见 u2-l5、u6-l2）。二者分工在它的 docstring 里写得很清楚。

#### 4.3.2 核心流程

校验按「从便宜到贵」的顺序排列，任一失败即 raise：

```
1. q.device.type != "cuda"             → RuntimeError("requires CUDA tensors")
2. not torch.cuda.is_available()        → RuntimeError("requires CUDA-capable build")
3. major < 8                            → NotImplementedError(">= 8.0; got {major}.x")
4. use_sm90 = _use_sm90_specialized(major, head_dim_q, head_dim_v)
5. min_head_dim = 320            if use_sm90 else dense_min_supported_head_dim()
   max_head_dim = 512(SM90)      if use_sm90 else 1024(SM80)
6. not (min <= head_dim_q <= max)       → NotImplementedError(head_dim 区间报错)
7. (非 sm90) and head_dim_q % 32 != 0   → NotImplementedError(整除报错)
8. q.dtype not in (fp16, bf16)          → TypeError(dtype 报错)
9. k/v head_dim != q head_dim           → NotImplementedError(对称报错)
```

关键点：**第 4 步先判路径**，第 5-7 步的区间与整除要求**随路径变化**——SM90 路径要求 `[320,512]` 且不强求整除 32；SM80 路径要求 `[D_min, 1024]` 且 `D%32==0`。同一段代码、依路径给出不同门禁，这是本函数最精妙之处。

`dense_min_supported_head_dim()` 是个小开关：默认返回 `MIN_SUPPORTED_HEAD_DIM=320`；仅当设了环境变量 `FFPA_CUTE_ALLOW_SMALL_D=1` 时才降到 `SMALL_D_MIN_SUPPORTED_HEAD_DIM=64`（[src/ffpa_attn/cute/\_utils.py:L87-L89](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_utils.py#L87-L89)）。也就是说，CuTeDSL 默认**只接大 D**（≥320），小 D 默认仍走 SDPA。

#### 4.3.3 源码精读

[src/ffpa_attn/cute/\_\_init\_\_.py:L229-L289](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/__init__.py#L229-L289) — `_require_cute_supported` 全文。要点：

- [L249-L261](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/__init__.py#L249-L261)：device / cuda 可用 / `major<8` 三道门；
- [L262-L274](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/__init__.py#L262-L274)：先 `use_sm90 = _use_sm90_specialized(...)`，再据此选 `min_head_dim`/`max_head_dim`，校验 `head_dim_q` 区间；
- [L275-L279](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/__init__.py#L275-L279)：**仅 SM80 路径**要求 `head_dim_q % SM80_FWD_SPLIT_D_CHUNK(32) == 0`；
- [L280-L283](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/__init__.py#L280-L283)：dtype 必须 fp16/bf16；
- [L285-L289](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/__init__.py#L285-L289)：`k.size(-1)`、`v.size(-1)` 必须都等于 `q.size(-1)`——**强制 q/k/v 对称 head_dim**。

把这条「依路径变门禁」的区间写成数学形式：

\[
\text{合法 head\_dim 区间} =
\begin{cases}
320 \le D \le 512 & \text{SM90 专用路径} \\
D_{\min} \le D \le 1024,\quad D \bmod 32 = 0 & \text{SM80 通用路径}
\end{cases}
\]

其中 \(D_{\min} = 320\)（默认），或 \(D_{\min} = 64\)（`FFPA_CUTE_ALLOW_SMALL_D=1`）。

#### 4.3.4 代码实践

1. **目标**：用静态推演（或真机）列出 CuTeDSL 在不同架构下接受的 head_dim 集合，并理解「同一段代码依路径给不同门禁」。
2. **步骤**：按下表逐行填空（`use_sm90` 由 4.4 的谓词判定，这里先假设对称 `D==D_v`）：

   | 设备 major | head_dim | use_sm90? | min | max | 整除 32? | 结果 |
   |---|---|---|---|---|---|---|
   | 9 (Hopper) | 512 | ? | ? | ? | — | ? |
   | 9 (Hopper) | 640 | ? | ? | ? | ? | ? |
   | 8 (Ampere) | 512 | ? | ? | ? | ? | ? |
   | 9 (Hopper) | 300 | ? | ? | ? | — | ? |
   | 8 (Ampere) | 336 | ? | ? | ? | ? | ? |

3. **观察**：注意 Hopper 上 `D=640` 会落到 SM80 路径（`use_sm90=False`），从而**额外**要求 `640%32==0`（成立，故合法）；而 `D=300` 在两条路径都 `<320`，必被拒。
4. **预期结果**：第 1 行合法（sm90、`[320,512]`）；第 2 行合法（sm80、`[320,1024]`、`640%32==0`）；第 3 行合法（sm80、`512%32==0`）；第 4 行非法（`300<320`）；第 5 行合法（sm80、`336%32==0`）。
5. **待本地验证**：若有 H200，可 `ffpa_attn_func(q,k,v, backend="cutedsl")` 用 `D=640` 实测不报错；用 `D=300` 实测抛 `NotImplementedError`。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `_require_cute_supported` 对 SM90 路径**不**要求 `D%32==0`，对 SM80 路径却要求？

> **答**：SM80 Split-D 通路径沿 D 以 `SM80_FWD_SPLIT_D_CHUNK=32` 切片，D 必须被 32 整除才能均匀切分；SM90 专用路径用的是 Hopper WGMMA/TMA 的固定 tile 形状（见 `_validate_head_dims`，[src/ffpa_attn/cute/\_utils.py:L117-L131](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_utils.py#L117-L131)），只要求对称且 `D∈[320,512]`，不强求整除 32。

**练习 2**：传入 `q` 为 fp32、`k/v` 为 bf16 会怎样？

> **答**：第 8 步先因 `q.dtype not in (fp16,bf16)` 抛 `TypeError`。即便 q 也是 bf16 但 k 是 fp16，会在更外层 `_validate_qkv_common` 因「q/k/v dtype 不一致」报 `TypeError`（见 u6-l2）。CuTeDSL 全程只认 fp16/bf16。

**练习 3**：`_require_cute_supported` 抛的是 `RuntimeError`、`NotImplementedError` 还是 `TypeError`？为什么混用？

> **答**：三者混用且有语义区分——`RuntimeError` 表示运行环境问题（非 CUDA 张量、PyTorch 非 CUDA 版）、`NotImplementedError` 表示「这个组合还没实现」（架构太老、head_dim 越界、不对称）、`TypeError` 表示类型错（dtype）。混用是为了让上层与用户能据异常类型区分「环境问题 vs 功能未实现 vs 用法错误」。

---

### 4.4 _use_sm90_specialized：SM90 专用 / SM80 通用 的单一分发谓词

#### 4.4.1 概念说明

`_use_sm90_specialized` 是 CuTeDSL 后端**唯一**的路径选择谓词，前向、反向、varlen fake-mode 校验**共用**同一个函数。它的职责极其专一：给定 `(major, head_dim, head_dim_v)`，返回 `True` 表示走 SM90 专用 kernel，`False` 表示走 SM80 通用 Split-D 回退。

「单一谓词」是这里最重要的设计决策——它避免了「前向用一套判定、反向用另一套」导致的不一致风险。`_require_cute_supported`（4.3）、`_forward_impl_for_device`、`_backward_impl_for_device`、varlen 的 `_varlen_fwd_fake`（[src/ffpa_attn/cute/\_\_init\_\_.py:L765-L768](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/__init__.py#L765-L768)）全都调它，保证「同一调用在前向、反向、trace 三处的路径判定完全一致」。

#### 4.4.2 核心流程

谓词是一个纯逻辑表达式，三个条件**全部**为真才走 SM90：

\[
\text{use\_sm90} = (major = 9) \;\land\; (320 \le head\_dim \le 512) \;\land\; (320 \le head\_dim_v \le 512)
\]

由此可直接读出 SM90 专用 / SM80 通用的**分界条件**：

- 走 **SM90 专用**：当且仅当 **Hopper（major==9）** 且 **q 与 v 的 head_dim 都对称地落在 `[320,512]`**；
- 走 **SM80 通用**（兜底）：其它一切情况，包括
  - 非 Hopper 架构（SM80/SM89/SM100/SM103/SM120…）；
  - Hopper 上 `head_dim > 512`；
  - Hopper 上 `head_dim < 320`（此时即便走到这里，也会被 `_require_cute_supported` 拒，但谓词本身只判路径）；
  - q 与 v 的 head_dim 不对称（v 的 head_dim 落不到 `[320,512]`）。

注意谓词用的是**字面常量** `MIN_SUPPORTED_HEAD_DIM=320` 和 `SM90_SUPPORTED_HEAD_DIM=512`，**不**经过 `dense_min_supported_head_dim()`。这意味着：即便开了 `FFPA_CUTE_ALLOW_SMALL_D=1`，小 D（如 128）在 Hopper 上也**不会**进 SM90 专用路径，而是落到 SM80 通用路径（再由 `_require_cute_supported` 放行到 `[64,1024]`）。

`_forward_impl_for_device` / `_backward_impl_for_device` 在谓词之外只多一道 `major<8` 的保护（抛 `NotImplementedError`），然后二选一返回 kernel 入口：

[src/ffpa_attn/cute/\_\_init\_\_.py:L203-L226](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/__init__.py#L203-L226) — `_forward_impl_for_device` / `_backward_impl_for_device`：`major<8` 抛错；`_use_sm90_specialized(...)` 为真返回 `_ffpa_attn_{forward,backward}_sm90`，否则返回 `_ffpa_attn_{forward,backward}_sm80`。

#### 4.4.3 源码精读

[src/ffpa_attn/cute/\_\_init\_\_.py:L188-L200](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/__init__.py#L188-L200) — `_use_sm90_specialized` 全文。docstring 点明它是「Single routing predicate shared by forward/backward dispatch and by the fake-mode varlen validator selection」，并复述了分界规则。

辅助函数 `_cute_device_major` 给出当前设备的 major（在 fake mode 或非 CUDA 时返回默认 `9`，使 trace 期也能走通判定）：

[src/ffpa_attn/cute/\_\_init\_\_.py:L181-L185](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/__init__.py#L181-L185) — `_cute_device_major`：CUDA 设备取 `get_device_capability` 的 major，否则返回 `9`（默认按 Hopper 走 fake 判定）。

README 用文档语言复述了同一条规则与一张架构路由图：

[src/ffpa_attn/cute/README.md:L107-L131](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/README.md#L107-L131) — dense 与 varlen 共用同一组 SM90/SM80 kernel；「`major == 9` and symmetric `head_dim <= 512` → SM90 specialised；其它支持架构及更大 dense head_dim → SM80 generic」。

#### 4.4.4 关于 Blackwell（SM100/SM120）的重要说明

README 有一段专门的调研笔记，解释**为什么不能简单地把 `major == 9` 放宽成 `major >= 9`**：

[src/ffpa_attn/cute/README.md:L185-L210](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/README.md#L185-L210) — Blackwell 实验：即便放宽 Python 侧的 `sm==90` 门，SM90 专用 kernel 仍会在 CUTLASS DSL 的 Hopper warpgroup MMA 处报 `expects arch to be Arch.sm_90a, but got Arch.sm_120a`。Blackwell 必须另写基于 `tcgen05` 的栈，不能复用 Hopper warpgroup 路径。

这正是 `_use_sm90_specialized` 写死 `major == 9`（而非 `>= 9`）的根本原因：**SM90 专用路径用的是 Hopper 独占指令**，在 Blackwell 上根本编不出可用的 kernel。Blackwell 当前由 SM80 通用路径（Ampere 级 warp MMA + cp.async，向前兼容）兜底，性能不是最优但能跑。

#### 4.4.5 代码实践

1. **目标**：用谓词的数学定义，判定若干典型 `(major, D, D_v)` 组合走哪条路径。
2. **步骤**：按下表填「SM90 专用 / SM80 通用」：

   | major | head_dim | head_dim_v | use_sm90 | 路径 |
   |---|---|---|---|---|
   | 9 | 512 | 512 | ? | ? |
   | 9 | 384 | 384 | ? | ? |
   | 9 | 640 | 640 | ? | ? |
   | 9 | 512 | 320 | ? | ? |
   | 8 | 512 | 512 | ? | ? |
   | 12 | 512 | 512 | ? | ? |

3. **观察**：第 4 行（`head_dim=512, head_dim_v=320`）虽都在 `[320,512]`，但不对称——不过 `_require_cute_supported` 会更早因 `v.size(-1)!=q.size(-1)` 拒掉；谓词本身只看「v 的 head_dim 是否也落在 `[320,512]`」，这里 `320` 落在区间内，故谓词为真（但实际调用仍会被对称校验拦截）。第 6 行 Blackwell(`major=12`) 必走 SM80 通用。
4. **预期结果**：1✓SM90、2✓SM90、3✗SM80(640>512)、4谓词✓但被对称校验拒、5✗SM80(major≠9)、6✗SM80(Blackwell)。
5. **待本地验证**：若同时有 H200 与 Blackwell 卡，可对同一 `D=512` 调用观察二者分别命中哪条 kernel（H200→sm90、Blackwell→sm80）。

#### 4.4.6 小练习与答案

**练习 1**：Hopper 上 `head_dim=1024` 的 dense 注意力会走哪条路径？为什么？

> **答**：走 SM80 通用路径。`_use_sm90_specialized(9, 1024, 1024)` 因 `1024 > SM90_SUPPORTED_HEAD_DIM(512)` 返回 False。SM90 专用 kernel 只特化到 `D≤512`，更大的 D 由 SM80 Split-D 兜底（`1024%32==0`，校验通过）。

**练习 2**：为什么 `_use_sm90_specialized` 同时检查 `head_dim` 和 `head_dim_v`，而 `_require_cute_supported` 又强制 `k/v` head_dim == `q` head_dim？岂不冗余？

> **答**：职责不同、略有冗余但都必要。谓词是「路径选择」的纯函数，必须自包含（不依赖外部已校验的状态），故它自己判 `head_dim_v`；`_require_cute_supported` 是「张量合法性」校验，对称性是 CuTeDSL 的硬约束。二者解耦让谓词可被 fake-mode、varlen 等多处独立复用而不出错。

**练习 3**：把 `_use_sm90_specialized` 里的 `major == 9` 改成 `major >= 9`，在 Blackwell 上会发生什么？

> **答**：谓词会把 Blackwell 的 `D=512` 误导到 SM90 专用 kernel，但该 kernel 用 Hopper 独占的 WGMMA/TMA，在 CuTeDSL JIT 阶段就会报 `expects arch to be Arch.sm_90a, but got Arch.sm_120a`（见 README 调研笔记）。即便强行 `CUTE_DSL_ARCH=sm_90a` 编过，运行期也会 `cudaErrorNoKernelImageForDevice`。所以门不能放宽——这正是写死 `major == 9` 的原因。

---

## 5. 综合实践

把 4.2~4.4 串起来，做一次完整的「能力矩阵」推导与（可选的）真机验证。

### 实践目标

仅凭源码，列出 CuTeDSL 后端支持的 `(arch, head_dim, dtype)` 组合表，并标注每组走哪条 kernel 路径；再说明 SM90 专用与 SM80 通用的分界条件。

### 操作步骤

1. **读常量**：从 [src/ffpa_attn/cute/\_utils.py:L21-L38](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_utils.py#L21-L38) 取出门禁常量：`MIN=320`、`SM90_MAX=512`、`SM80_MAX=1024`、`CHUNK=32`。
2. **读谓词**：从 [src/ffpa_attn/cute/\_\_init\_\_.py:L188-L200](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/__init__.py#L188-L200) 写出 `use_sm90 = (major==9) ∧ (320≤D≤512) ∧ (320≤D_v≤512)`。
3. **读校验**：从 [src/ffpa_attn/cute/\_\_init\_\_.py:L229-L289](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/__init__.py#L229-L289) 写出依路径变化的合法区间（4.3.3 的分段公式）。
4. **填表**：产出下表（dtype 一律 fp16/bf16，q/k/v 对称）：

   | 架构（major） | 合法 head_dim | 路径 | 备注 |
   |---|---|---|---|
   | Hopper (9) | 320 ≤ D ≤ 512 | SM90 专用 | 最快，427 TFLOPS |
   | Hopper (9) | 512 < D ≤ 1024 且 D%32==0 | SM80 通用 | D>512 退兜底 |
   | Ampere/Ada (8.x) | 320 ≤ D ≤ 1024 且 D%32==0 | SM80 通用 | 如 A100/L20 |
   | Blackwell (10/12) | 320 ≤ D ≤ 1024 且 D%32==0 | SM80 通用 | 不能复用 SM90 kernel |

5. **（可选）真机验证**：在 H200 上跑两段对照：
   ```python
   import torch
   from ffpa_attn import ffpa_attn_func
   def run(D):
       q = torch.randn(1, 32, 8192, D, dtype=torch.bfloat16, device="cuda", requires_grad=True)
       k = torch.randn_like(q); v = torch.randn_like(q)
       out = ffpa_attn_func(q, k, v, is_causal=True, backend="cutedsl")
       out.sum().backward()
       print("D=", D, "OK", out.shape)
   run(512)   # 走 SM90 专用
   run(640)   # 走 SM80 通用（Hopper 上 D>512）
   ```

### 需要观察的现象

- 推导表里 Hopper 的合法 head_dim 被「劈成两段」：`[320,512]` 走专用、`(512,1024]` 走通用——同一架构、依 D 不同走不同 kernel。
- Blackwell 只能走通用路径，因为 SM90 专用 kernel 用了 Hopper 独占指令。
- 所有路径都只接 fp16/bf16，且 q/k/v head_dim 必须对称。

### 预期结果

得到上表；并能用一句话说出分界条件：**「Hopper + 对称 `D∈[320,512]` → SM90 专用；其余支持的架构 / 其余合法 D → SM80 通用。」** 这正是 [src/ffpa_attn/cute/README.md:L127-L131](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/README.md#L127-L131) 的官方表述。

### 待本地验证

步骤 5 的真机运行需要 H200（或任一 SM8x/SM90 卡）。若无此类 GPU，本实践的「推导与填表」部分（步骤 1-4）可纯静态完成，仍是有效产出。

## 6. 本讲小结

- CuTeDSL 后端基于 CUTLASS CuTe DSL，**H200 上 D=512 可达 427 TFLOPS**，是四后端里最快、约束也最多的；包名是 `ffpa_attn.cute`，对外后端名是 `"cutedsl"`。
- 门禁分三层：**device 级**（`cute_forward_available`/`cute_backward_available`，只判 `major>=8`）、**head_dim 上限**（`cute_max_supported_head_dim` 恒为 1024）、**逐调用张量级**（`_require_cute_supported`）。
- `_require_cute_supported` 在 (device, arch, head_dim, dtype, 对称性) 五维做硬校验，**只报错不回退**；且**依路径变门禁**——SM90 路径要 `[320,512]`、SM80 路径要 `[D_min,1024]` 且 `D%32==0`。
- `_use_sm90_specialized` 是**唯一**的路径谓词，被前向、反向、varlen fake 共用；走 SM90 专用当且仅当「Hopper(`major==9`) + 对称 `D∈[320,512]`」，其余一律 SM80 通用兜底。
- Blackwell 不能复用 SM90 专用 kernel（Hopper 独占 WGMMA/TMA），当前由 SM80 通用路径兜底——谓词写死 `major==9` 而非 `>=9` 即为此。
- 与 Triton 后端的根本差异：CuTeDSL 对不支持的特性（attn_mask/dropout/越界 D）**直接抛错**，从不静默回退 SDPA。

## 7. 下一步学习建议

- **u6-l2（CuTeDSL 布局转换、校验与 varlen 接入）**：本讲只到「入口 + 分发」，下一步看 `_ffpa_attn_forward_cute`/`_ffpa_attn_backward_cute` 内部的 `[B,H,N,D]↔[B,N,H,D]` transpose、`_check_supported_options` 对功能选项的拒绝，以及 varlen 路径如何自管 autograd。
- **u6-l3（tile scheduler 与 producer/consumer pipeline）**：进入 SM90 专用 kernel 内部，看 `SingleTileScheduler`/`PipelineTmaAsync` 如何用 TMA + barrier 重叠 g2s 与 MMA——这是 427 TFLOPS 的微观来源。
- **u6-l4（SM90 专用 kernel：d384/d512/generic）**：看为何对 d384/d512 做 head_dim 级特化、`FFPAAttnFwdSm90SplitD` 系列类的职责，以及 SM80 generic 如何做通用回退。
- 若想横向对比，可回看 **u3-l1（四后端总览）** 的能力矩阵与本讲的 `(arch, head_dim, dtype)` 表相互印证。
