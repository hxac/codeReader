# 每个 head_dim 代码生成与 C++ pybind 分发

> 本讲是「手写 CUDA 后端」系列第三篇（依赖 u7-l1）。上一篇讲清楚了**单个前向 kernel 在 GPU 上的执行流程**（g2s→MMA→online softmax）；本讲退回 **host 侧**，回答一个工程问题：FFPA 是怎么把「13 种 head_dim × 2 种 dtype × 2 种累加精度」这几十种 kernel 组合**编译得快、分发得对**，并暴露成一个 Python 可调用函数的。

## 1. 本讲目标

学完本讲你应当能够：

- 说清 **per-headdim（按 head_dim 拆翻译单元）代码生成**要解决的两个问题：缩短编译时间、支持模板特化。
- 读懂 `env.py::generate_split_headdim_sources` 如何按 `(dtype, acc)` 在 `csrc/cuffpa/generated/` 下生成「声明头 + 每个 head_dim 两份 `.cu` + 一份 dispatch」这套文件。
- 跟踪从 Python 调用到 GPU kernel 的 **C++ 三级分发链**：统一入口 `ffpa_attn_forward` → `(dtype, acc)` 入口 `ffpa_attn_fwd_{fp16f16,fp16f32,bf16f32}` → per-headdim 入口 `..._d{D}` → `launch_ffpa_attn_fwd_template<...>`。
- 理解 **acc 编码 `0=f16 / 1=f32`** 的含义，以及 **bf16 激活必须用 fp32 累加（acc=1）** 这一约束为何在 Python、C++、代码生成三层被同时强制。
- 看懂 `PYBIND11_MODULE` 如何把 C++ 函数 `ffpa_attn_forward` 绑定成 Python 模块属性 `ffpa_attn._C.ffpa_attn_forward`。

## 2. 前置知识

本讲会用到下面这些概念，不熟悉的读者先看这里：

- **翻译单元（Translation Unit, TU）**：C++ 编译的基本单位，一个 `.cu`/`.cc` 文件加上它 `#include` 的所有头文件展开后，就是一个 TU。nvcc 一次编译一个 TU，多个 TU 可以**并行**编译（`make -j N`）。本讲的核心技巧就是「把一个大 TU 拆成很多小 TU」来喂饱并行编译。
- **C++ 模板（template）特化**：`launch_ffpa_attn_fwd_template<int, 512, 1, 1, 2>` 中尖括号里的 `512` 是**编译期常量**。GPU kernel 的 tile 形状、循环展开次数、寄存器分配都依赖 head_dim，所以 head_dim 必须「烤进」模板参数，每种 head_dim 编译出一份独立的机器码——这正是「为什么每种 head_dim 要单独生成一个文件」的根本原因。
- **MMA 累加器精度（acc）**：MMA（Matrix Multiply-Acculate）是 GPU 上做小矩阵乘的硬件指令。`QKᵀ` 与 `PV` 两次乘法的**累加器**可以用 `fp16`（快但精度低）或 `fp32`（慢但精度高）。FFPA 用一个整数编码它：`0=f16`，`1=f32`。
- **PTX**：NVIDIA 的并行线程执行指令集（一种类汇编的中间表示）。GPU 上不存在 `bf16 输入 + bf16/fp16 累加` 的 MMA PTX 指令，这是后文 bf16 约束的硬件根因。
- **pybind11**：一个把 C++ 函数/类暴露给 Python 的库。`PYBIND11_MODULE` 宏定义一个 Python 扩展模块，`m.def("名字", &C++函数)` 把 C++ 函数注册成 Python 可调用对象。PyTorch 的 CUDA 扩展底层都靠它。

如果你还没读过 u7-l1，建议先了解前向 kernel 的四阶段主循环，本讲讲的就是「谁来调用那个 kernel」。

## 3. 本讲源码地图

本讲横跨 Python 构建侧与 C++ 运行侧，涉及的关键文件如下：

| 文件 | 角色 | 语言 |
| --- | --- | --- |
| [`env.py`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/env.py) | 构建配置中枢：决定编哪些 head_dim、生成哪些 `.cu` 文件、传哪些 nvcc 参数 | Python |
| [`csrc/cuffpa/ffpa_attn_api.cc`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/ffpa_attn_api.cc) | **统一 pybind 入口** `ffpa_attn_forward`：按 dtype + acc 二级分发 | C++ |
| [`csrc/cuffpa/generated/ffpa_attn_fwd_dispatch.cu`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/generated/ffpa_attn_fwd_dispatch.cu) | 自动生成的 **(dtype,acc) 入口**：3 个函数各自 `switch(head_dim)` | C++（生成） |
| [`csrc/cuffpa/generated/ffpa_attn_fwd_decls.h`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/generated/ffpa_attn_fwd_decls.h) | 自动生成的 per-headdim 符号声明（每个 head_dim 3 条） | C++（生成） |
| [`csrc/cuffpa/generated/ffpa_attn_fwd_fp16_hdim512.cu`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/generated/ffpa_attn_fwd_fp16_hdim512.cu) | 自动生成的 **per-headdim TU 示例**：含 `fp16f16_d512` 与 `fp16f32_d512` 两个符号，最终调用 `launch_ffpa_attn_fwd_template` | C++（生成） |
| [`src/ffpa_attn/cuda/__init__.py`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cuda/__init__.py) | Python 侧：`import _C`、用 `torch.library` 注册 `_fwd_cuda` 算子、分配输出张量 | Python |
| [`src/ffpa_attn/cuda/_ffpa_fwd.py`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cuda/_ffpa_fwd.py) | Python 侧薄封装 `_ffpa_attn_forward_cuda`：透传到 `torch.ops.ffpa_attn._fwd_cuda` | Python |
| [`src/ffpa_attn/functional.py`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py) | `CUDABackend` 配置类、acc 编码常量、`_FFPAAttnFunc.forward` 的 CUDA 分支 | Python |

一句话总览调用链（本讲的主线）：

```
ffpa_attn_func (Python)
  └─ _FFPAAttnFunc.forward  →  forward_meta 是 CUDABackend
      └─ _ffpa_attn_forward_cuda          (src/ffpa_attn/cuda/_ffpa_fwd.py)
          └─ torch.ops.ffpa_attn._fwd_cuda (src/ffpa_attn/cuda/__init__.py, 分配 O 与 lse)
              └─ _C.ffpa_attn_forward      (ffpa_attn_api.cc, 按 dtype+acc 分发)  ← C++ 边界
                  └─ ffpa_attn_fwd_{fp16f16|fp16f32|bf16f32}   (dispatch.cu, switch(d))
                      └─ ffpa_attn_fwd_{...}_d{D}              (per-headdim TU, switch(stages))
                          └─ launch_ffpa_attn_fwd_template<...> (launch_templates.cuh)
                              └─ 真 GPU kernel  (u7-l1 讲过的四阶段主循环)
```

## 4. 核心概念与源码讲解

### 4.1 per-headdim 代码生成：动机与 `generate_split_headdim_sources`

#### 4.1.1 概念说明

FFPA 支持 13 种 head_dim（默认 256→1024 步长 64）、2 种 dtype（fp16/bf16）、对 fp16 还分 f16/f32 两种累加精度。每种组合最终都要实例化成一个独立的 `launch_ffpa_attn_fwd_template<...>` 模板——因为 head_dim 是 kernel 内 tile 形状、循环展开、寄存器分配的编译期常量（见 u7-l1）。

最朴素的写法是**把所有 head_dim 的实例化堆进同一个 `.cu` 文件、用一个大 `switch(d)` 串起来**。但这样有一个致命缺点：**一个 TU = 一次 nvcc 调用**，无论 `make -j` 设多大，这个文件都只能由一个 nvcc 进程串行编译，几十个重型模板实例化会让构建时间拉到几十分钟。

FFPA 的解法是 **per-headdim 代码生成**：用 Python 在构建期**为每个 head_dim 单独生成一个 `.cu` 翻译单元**。这样 `MAX_JOBS`（即 `make -j`）就能对这几十个小文件**并行**调用 nvcc，把墙钟构建时间砍下来一个数量级。这不是「为了生成而生成」，而是为了**把并行度从「一个文件内」提升到「文件之间」**。

第二个动机是**支持模板特化**：每个 head_dim 文件只烘焙一个 `d` 值，nvcc 能针对该值做最激进的常量传播与寄存器分配，互不干扰。

#### 4.1.2 核心流程

`env.py::generate_split_headdim_sources` 在每次构建时被 `get_build_sources` 调用，流程如下：

1. 用 `get_enabled_headdims()` 算出本次要编的 head_dim 集合（默认 13 个）。
2. 渲染并写出 **声明头** `ffpa_attn_fwd_decls.h`：每个 head_dim 3 条声明（`fp16f16`/`fp16f32`/`bf16f32`）。
3. 对每个 head_dim 写出 **两份 TU**：`ffpa_attn_fwd_fp16_hdim{D}.cu`（含 fp16f16 + fp16f32 两个符号）和 `ffpa_attn_fwd_bf16_hdim{D}.cu`（仅 bf16f32 一个符号）。
4. 写出 **dispatch TU** `ffpa_attn_fwd_dispatch.cu`：3 个 `(dtype,acc)` 入口函数，各自 `switch(d)` 跳到 per-headdim 符号。
5. 用 `_write_if_changed` 落盘：内容没变就不改 mtime，好让 ninja/setuptools 跳过重编。
6. 清理陈旧文件（如旧布局遗留的 `ffpa_attn_L1_*`、被关闭的 bwd 文件）。

默认配置下产物规模：13 个 head_dim × 2 份 TU + 1 份 dispatch = **27 份 `.cu`** + 1 份 `.h`。

#### 4.1.3 源码精读

**head_dim 集合的优先级**——[`env.py:389-416`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/env.py#L389-L416)：显式 `FFPA_DEV_HEADDIMS` 子集 > `ENABLE_FFPA_ALL_HEADDIM`（步长 32，32 个）> 默认（步长 64，13 个）。开发期可用 `FFPA_DEV_HEADDIMS="512"` 只编一个 head_dim 秒级出包：

```python
# env.py:414-416  默认集合：256,320,...,1024，共 13 个
if cls.enable_all_headdim():
    return list(range(32, 1025, 32))
return list(range(256, 1025, 64))
```

**生成器主循环**——[`env.py:461-482`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/env.py#L461-L482)：每个 head_dim 调两个渲染器，分别产出 fp16/bf16 TU。注意 `fwd_generated_count = len(headdims) * 2 + 1`（每 head_dim 2 份 TU + 1 份 dispatch）：

```python
# env.py:471-482（节选）
for d in headdims:
    fp16_path = os.path.join(gen_dir, f"ffpa_attn_fwd_fp16_hdim{d}.cu")
    bf16_path = os.path.join(gen_dir, f"ffpa_attn_fwd_bf16_hdim{d}.cu")
    cls._write_if_changed(fp16_path, cls._render_per_headdim_fp16_tu(d))
    cls._write_if_changed(bf16_path, cls._render_per_headdim_bf16_tu(d))
    ...
dispatch_path = os.path.join(gen_dir, "ffpa_attn_fwd_dispatch.cu")
cls._write_if_changed(dispatch_path, cls._render_dispatch_tu(headdims))
```

**关键观察：文件名 vs 符号名不同**。文件名只编码「存储 dtype + head_dim」（如 `ffpa_attn_fwd_fp16_hdim512.cu`），而函数符号编码「dtype + acc + head_dim」（如 `ffpa_attn_fwd_fp16f32_d512`）。所以**每个 head_dim 有 2 个文件、却声明 3 个符号**（fp16 文件 2 个 + bf16 文件 1 个），bf16 因无 bf16f16 变体只有 1 个。这与声明头一一对应——[`ffpa_attn_fwd_decls.h`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/generated/ffpa_attn_fwd_decls.h) 每个 head_dim 恰好 3 行。

**为什么要把 dispatch 拆出来单独一个 TU**：dispatch TU 体积小、编译快，但它 `#include` 了所有 per-headdim 符号的声明（来自 decls.h），从而把「按 d 跳转」的 `switch` 与「重型模板实例化」彻底解耦——重头戏全部在可并行的 per-headdim TU 里。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**（生成代码无需 GPU，纯文本）：

1. **目标**：亲手验证「13 个 head_dim → 27 份 `.cu`」的产物规模与命名规则。
2. **步骤**：
   - 在仓库根目录统计 `csrc/cuffpa/generated/` 下 `ffpa_attn_fwd_*_hdim*.cu` 的数量，应得到 26（13 fp16 + 13 bf16）。
   - 用 `grep -c` 数 `ffpa_attn_fwd_decls.h` 的符号行数，应得到 39（13×3）。
   - 打开 `ffpa_attn_fwd_fp16_hdim512.cu` 与 `ffpa_attn_fwd_bf16_hdim512.cu`，确认前者有 2 个符号（`fp16f16_d512`、`fp16f32_d512`）、后者只有 1 个（`bf16f32_d512`）。
3. **观察现象**：bf16 文件体积明显更小（只有半个函数）。
4. **预期结果**：fp16 文件含 `kMmaAccFloat32QK/PV` 的两套取值（0/0 与受 `FORCE_*_F16` 控制的 1/1），bf16 文件恒为 1/1。
5. 若想看「步长 32 的 32 个 head_dim」效果，设 `ENABLE_FFPA_ALL_HEADDIM=1` 后运行 `python3 env.py`（仅打印配置，不触发编译），观察 `FFPA_DEV_HEADDIMS` 行显示 `range(32, 1024, 32)`。

> 注意：上面运行 `python3 env.py` 只会调用 `ENV.list_ffpa_env()` 打印环境变量，不会真的生成文件或编译；真正触发生成的是 `setup.py` 构建（见 4.4）。如未带 GPU，`get_build_arch_list()` 会抛错，属正常。

#### 4.1.5 小练习与答案

**练习 1**：为什么 FFPA 不直接写一个包含所有 head_dim 的 `.cu` 文件，而要代码生成几十个文件？

**答案**：因为 head_dim 是模板参数，单文件方案下所有实例化落在一个翻译单元里，只能由**一个 nvcc 进程串行编译**，构建极慢。拆成 per-headdim TU 后，`MAX_JOBS` 可以**并行**调起几十个 nvcc，墙钟时间大幅下降；同时每个文件只烘焙一个 `d`，便于编译期特化。

**练习 2**：`generate_split_headdim_sources` 的 docstring 说「generated files are committed to the repository」，结合 `_write_if_changed` 说明这样做的意义。

**答案**：生成文件被提交进仓库（非 `.gitignore`），且 `_write_if_changed` 仅在内容变化时才写盘、否则不动 mtime。这样**稳态下增量构建是 no-op**——ninja/setuptools 凭 mtime 判断，文件没变就跳过重编，让日常 `pip install -e .` 保持飞快。

---

### 4.2 统一 pybind 入口 `ffpa_attn_forward`：dtype + acc 二级分发

#### 4.2.1 概念说明

Python 侧只看到一个函数 `_C.ffpa_attn_forward`。但在 C++ 这一边，这个「统一入口」其实是一个**分发器**：它不直接算注意力，而是**看张量的 dtype 和用户传入的 acc 编码，决定调用 3 个专用函数中的哪一个**。

这里有个贯穿全篇的关键编码——**acc**：

| acc 值 | 含义 | 对应函数后缀 |
| --- | --- | --- |
| `0` | MMA 累加器用 fp16 | `fp16f16` |
| `1` | MMA 累加器用 fp32 | `fp16f32` / `bf16f32` |

注意函数名的读法：`fp16f32` = 「fp16 输入 + fp32 累加」，前半是**激活/存储 dtype**，后半是**累加精度**。这个命名在 dispatch、decls、per-headdim TU 中完全一致。

#### 4.2.2 核心流程

`ffpa_attn_forward` 的分发逻辑是一个二层判定：

```
读 dtype = Q.scalar_type()
读 acc   = 用户传入的 int64

if dtype == kHalf (fp16):
    if acc == 0:  → ffpa_attn_fwd_fp16f16
    elif acc== 1: → ffpa_attn_fwd_fp16f32
    else:         → 抛 invalid_argument("acc must be 0 or 1")
elif dtype == kBFloat16 (bf16):
    if acc != 1:  → 抛 invalid_argument("bf16 activations require acc=1")
    else:         → ffpa_attn_fwd_bf16f32
else:
    → 抛 invalid_argument("dtype must be fp16/bf16")
```

还有两个细节：①若调用方传了空的 `softmax_lse`，入口会按 `[B, Nh_q, Nq]` 自动分配（dtype=float32）；②`tma` 形参被保留但**强制忽略**（`tma_i = 0`），因为 legacy CUDA TMA 分发已从后端移除，只留形参是为了 API 兼容。

#### 4.2.3 源码精读

**统一入口签名与注释**——[`ffpa_attn_api.cc:27-33`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/ffpa_attn_api.cc#L27-L33)。第 27 行注释 `// acc encoding: 0=f16, 1=f32.` 是全篇编码的「权威定义」：

```cpp
// Public unified pybind entry.  acc encoding: 0=f16, 1=f32.
void ffpa_attn_forward(torch::Tensor Q, torch::Tensor K, torch::Tensor V,
                       torch::Tensor attn_bias, torch::Tensor O,
                       torch::Tensor softmax_lse, int64_t stages, int64_t acc,
                       int64_t causal, double softmax_scale, double dropout_p,
                       int64_t philox_seed, int64_t philox_offset,
                       int64_t tma) {
```

**fp16 分支：acc 二选一**——[`ffpa_attn_api.cc:53-64`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/ffpa_attn_api.cc#L53-L64)，acc 既不能是 0/1 之外的值：

```cpp
if (dtype == torch::kHalf) {
  if (acc == 0) {
    ffpa_attn_fwd_fp16f16(...);
  } else if (acc == 1) {
    ffpa_attn_fwd_fp16f32(...);
  } else {
    throw std::invalid_argument("ffpa_attn: acc must be 0 (f16) or 1 (f32)");
  }
}
```

**bf16 分支：强制 acc=1**——[`ffpa_attn_api.cc:65-73`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/ffpa_attn_api.cc#L65-L73)，错误信息直接点出硬件根因：

```cpp
} else if (dtype == torch::kBFloat16) {
  if (acc != 1) {
    throw std::invalid_argument(
        "ffpa_attn: bf16 activations require acc=1 (f32); "
        "no bf16-acc mma PTX exists.");
  }
  ffpa_attn_fwd_bf16f32(...);
}
```

**softmax_lse 自动分配与 tma 忽略**——[`ffpa_attn_api.cc:38-51`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/ffpa_attn_api.cc#L38-L51)，第 41 行 `(void)tma;` 显式标记「接受但不用」。

**未编译 CUDA 时的优雅降级**——[`ffpa_attn_api.cc:78-96`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/ffpa_attn_api.cc#L78-L96)：当未定义 `ENABLE_FFPA_CUDA_IMPL` 宏时，函数体用 `(void)形参;` 挨个消化未用变量，最后抛 `runtime_error` 提示「Rebuild with ENABLE_FFPA_CUDA_IMPL=1」。这保证 Triton-only 构建（不编 `_C` 的 CUDA 部分）也能链接通过。

> 配套：三个 `(dtype,acc)` 入口的前向声明在 [`ffpa_attn_api.cc:8-25`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/ffpa_attn_api.cc#L8-L25)，被 `#ifdef ENABLE_FFPA_CUDA_IMPL` 包住——Triton-only 构建里这 3 个声明直接消失。

#### 4.2.4 代码实践

1. **目标**：确认「bf16 + acc=0」在 C++ 层会被拒绝，并找出错误信息原文。
2. **步骤**：阅读 [`ffpa_attn_api.cc:65-73`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/ffpa_attn_api.cc#L65-L73)，记录抛出的异常类型与文案。
3. **观察**：异常类型是 `std::invalid_argument`（不是 `runtime_error`），文案包含 `no bf16-acc mma PTX exists`。
4. **预期结果**：这是一个**参数语义错误**（用户传了非法组合），用 `invalid_argument` 比 `runtime_error` 更准确——后者留给「能力缺失」（如未编译）。
5. 「待本地验证」：若你有编出 `_C` 的环境（`ENABLE_FFPA_CUDA_IMPL=1`），可手写 `torch.ops.ffpa_attn._fwd_cuda(..., acc=0)` 配 bf16 张量触发该异常（实际 Python 侧会更早拦截，见 4.4）。

#### 4.2.5 小练习与答案

**练习 1**：`acc` 这个参数为什么用整数 0/1 而不是字符串 `"f16"/"f32"`？

**答案**：因为它是穿过 **torch.library 算子 schema** 的参数（见 4.4 的 `_fwd_cuda` schema 把 `acc` 声明成 `int`）。torch 自定义算子的 schema 只接受有限类型（int/float/Tensor/bool 等），不支持任意字符串语义；用整数编码是最省事、可序列化、可被 `register_fake` 一致处理的做法。Python 侧的 `CUDABackend.acc="f32"` 字符串经 `acc_code` 属性翻译成 1 再传入。

**练习 2**：第 44-51 行对空 `softmax_lse` 自动分配，但调用方（`cuda/__init__.py`）其实已经分配了。这是不是冗余？

**答案**：不算冗余，是**防御性编程**。Python 侧 `_fwd_cuda_torch_op` 确实预分配了对齐到 8 的 `softmax_lse`（见 4.4），但 `ffpa_attn_forward` 作为公开 pybind 入口，也可能被其它 C++/直接绑定调用方使用。入口自己兜底分配，保证「不传也能跑」，是稳健的接口设计。

---

### 4.3 `(dtype,acc)` 入口与 per-headdim 翻译单元：`switch(d)` 与模板特化

#### 4.3.1 概念说明

上一节的 `ffpa_attn_forward` 只分发了 dtype 和 acc，**还没用到 head_dim**。真正把 `d` 烤进模板的是这一层：3 个 `(dtype,acc)` 入口函数各自一个 `switch (d)`，跳到对应的 per-headdim 符号 `ffpa_attn_fwd_{dtype}{acc}_d{D}`，而后者才真正实例化 `launch_ffpa_attn_fwd_template<t_in, D, acc_qk, acc_pv, STAGES>`。

为什么要把 `switch(d)` 单独放在 dispatch TU、而不是直接放在 per-headdim TU 里？因为 per-headdim TU **只知道自己那一个 `d`**（它就是为了单一 head_dim 而生的）；而 `switch` 需要知道「全部 13 个 `d`」，这是「跨 head_dim 的知识」，自然要放在汇总性的 dispatch TU 中。

另一个细节：**fp16f32 路径有「强制降回 f16 累加」的逃生开关**。默认 fp16f32 用 fp32 累加（精度高），但若定义了 `ENABLE_FFPA_FORCE_QK_F16`/`ENABLE_FFPA_FORCE_PV_F16`，可把 QK 或 PV 的累加器**强制降回 fp16**（更快、省寄存器），从而让「fp16f32」符号在两种精度间切换。这两个开关互斥地控制 QK 与 PV 两段累加器，详见 env.py 的运行期开关（u7-l5）。

#### 4.3.2 核心流程

以 `ffpa_attn_fwd_fp16f32(Q, ..., stages, ...)` 为例：

```
1. CHECK_TORCH_TENSOR_DTYPE(Q/K/V/O, torch::kHalf)   // 防御性 dtype 校验
2. d = Q.size(3)                                       // head_dim 来自张量最后一维
3. switch (d):
     case 256:  ffpa_attn_fwd_fp16f32_d256(...);  break;
     case 320:  ffpa_attn_fwd_fp16f32_d320(...);  break;
     ...
     case 1024: ffpa_attn_fwd_fp16f32_d1024(...); break;
     default:   throw "headdim not support!";
```

进入 `ffpa_attn_fwd_fp16f32_d512` 后：

```
1. 确定 kMmaAccFloat32QK / kMmaAccFloat32PV（受 FORCE_*_F16 宏控制，默认 1/1）
2. switch (stages):  选 STAGES ∈ {1,2} 或 {1,2,3,4}(若 ALL_STAGES)
3. launch_ffpa_attn_fwd_template<__half, 512, kMmaAccFloat32QK, kMmaAccFloat32PV, STAGES>(...)
       → 进入 u7-l1 讲过的 GPU kernel 主循环
```

#### 4.3.3 源码精读

**dispatch TU 的 `switch(d)`**——[`ffpa_attn_fwd_dispatch.cu:42-77`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/generated/ffpa_attn_fwd_dispatch.cu#L42-L77) 是 `ffpa_attn_fwd_fp16f32` 的全文。注意第 56-59 行先做 dtype 防御校验，第 60 行 `const int d = Q.size(3);` 从张量取 head_dim，第 61-76 行是 13 个 case：

```cpp
CHECK_TORCH_TENSOR_DTYPE(Q, torch::kHalf)   // L56
CHECK_TORCH_TENSOR_DTYPE(K, torch::kHalf)
CHECK_TORCH_TENSOR_DTYPE(V, torch::kHalf)
CHECK_TORCH_TENSOR_DTYPE(O, torch::kHalf)
const int d = Q.size(3);                      // L60
switch (d) {
  case 256:  ffpa_attn_fwd_fp16f32_d256(...);  break;
  ...
  case 1024: ffpa_attn_fwd_fp16f32_d1024(...); break;
  default: throw std::runtime_error("headdim not support!");
}
```

> 三个 dispatch 函数的唯一区别是：`CHECK` 的目标 dtype（`kHalf` vs `kBFloat16`）和符号前缀（`fp16f16`/`fp16f32`/`bf16f32`）。其余结构完全同构——这正是它能被 `_render_dispatch_tu` 用一个模板批量生成的原因（见 [`env.py:684-732`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/env.py#L684-L732)）。

**per-headdim TU：模板实例化**——[`ffpa_attn_fwd_fp16_hdim512.cu:40-81`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/generated/ffpa_attn_fwd_fp16_hdim512.cu#L40-L81) 是 `ffpa_attn_fwd_fp16f32_d512`。第 54-63 行用 `#ifdef ENABLE_FFPA_FORCE_QK_F16/PV_F16` 决定累加器精度，第 65-72 行按 stages 选模板实参：

```cpp
#ifdef ENABLE_FFPA_FORCE_QK_F16
  constexpr int kMmaAccFloat32QK = 0;     // 强制降回 f16
#else
  constexpr int kMmaAccFloat32QK = 1;     // 默认 f32
#endif
...
  if (stages == 2) {
    launch_ffpa_attn_fwd_template<__half, 512, kMmaAccFloat32QK, kMmaAccFloat32PV, 2>(...);
  } ...
```

**bf16 per-headdim TU：累加器恒为 f32**——[`ffpa_attn_fwd_bf16_hdim512.cu:5-38`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/generated/ffpa_attn_fwd_bf16_hdim512.cu#L5-L38)。注意第 19-20 行 `kMmaAccFloat32QK=1; kMmaAccFloat32PV=1;` **写死**，没有 `FORCE_*_F16` 逃生开关，且激活类型是 `__nv_bfloat16`。这从代码生成层面就保证了「bf16 只配 fp32 累加」：

```cpp
void ffpa_attn_fwd_bf16f32_d512(...) {
  constexpr int kMmaAccFloat32QK = 1;   // 写死 f32
  constexpr int kMmaAccFloat32PV = 1;
  ...
  launch_ffpa_attn_fwd_template<__nv_bfloat16, 512, 1, 1, 2>(...);
}
```

**这套写死的值来自代码生成器**——[`env.py:644-647`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/env.py#L644-L647) 渲染 bf16 TU 时直接硬编码 `kMmaAccFloat32QK/PV = 1`，且 [`env.py:628-652`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/env.py#L628-L652) 的 docstring 明说「bf16 has no f16-acc mma PTX; acc is forced to f32」、只发一个符号。

**CHECK 宏的定义**——[`csrc/cuffpa/logging.cuh:20-24`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/logging.cuh#L20-L24)，dtype 不符就抛 `runtime_error`，是 dispatch TU 的第一道防线。

> **三层强制 bf16=f32 一览**（本讲最重要的「纵深防御」结论）：
> - **Python 层** [`functional.py:579-585`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L579-L585)：bf16 + acc=f16 → `ValueError`。
> - **C++ 入口层** [`ffpa_attn_api.cc:65-70`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/ffpa_attn_api.cc#L65-L70)：bf16 + acc≠1 → `invalid_argument`。
> - **代码生成层** [`env.py:644-647`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/env.py#L644-L647)：bf16 TU 压根不存在 `bf16f16` 符号，累加器写死 1。
>
> 三层互为兜底：即便有人绕过 Python 直接调 C++，或绕过入口直接调 per-headdim 符号，也无法让 bf16 跑 f16 累加。

#### 4.3.4 代码实践

1. **目标**：定位「fp16、acc=f32、head_dim=512」对应的生成文件，并解释 dispatch 如何选中它。
2. **步骤**：
   - 在 `csrc/cuffpa/generated/` 找文件：存储 dtype=fp16、head_dim=512 → 文件名 `ffpa_attn_fwd_fp16_hdim512.cu`。
   - acc=f32 → 在该文件里找符号 `ffpa_attn_fwd_fp16f32_d512`（[`L40`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/generated/ffpa_attn_fwd_fp16_hdim512.cu#L40)）。
   - 看它如何被选中：`ffpa_attn_fwd_dispatch.cu` 中 `ffpa_attn_fwd_fp16f32` 的 `case 512:`（[`L66`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/generated/ffpa_attn_fwd_dispatch.cu#L66)）调用 `ffpa_attn_fwd_fp16f32_d512(...)`。
   - 而该入口又被 [`ffpa_attn_api.cc:58-61`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/ffpa_attn_api.cc#L58-L61) 的 `dtype==kHalf && acc==1` 选中。
3. **观察**：完整链路是 `dtype(fp16) + acc(1)` → `fp16f32` → `switch(d) case 512` → `fp16f32_d512` → `launch_ffpa_attn_fwd_template<__half,512,...>`。
4. **预期结果**：你能口述出这条链路上每一跳的判定条件与所在文件。
5. 「待本地验证」：设 `ENABLE_FFPA_FORCE_QK_F16=1` 重新生成（运行 `ENV.generate_split_headdim_sources()`），对比 `ffpa_attn_fwd_fp16_hdim512.cu` 中 `fp16f32_d512` 的 `kMmaAccFloat32QK` 是否从 1 变 0。

#### 4.3.5 小练习与答案

**练习 1**：dispatch TU 里每个函数都用 `CHECK_TORCH_TENSOR_DTYPE` 校验 dtype，但入口 `ffpa_attn_forward` 已经按 dtype 分发过了，这里是不是重复？

**答案**：功能上重复，但意义在于**把每个 TU 做成自包含的、可被独立调用的入口**。dispatch 函数（`ffpa_attn_fwd_fp16f16` 等）是带链接符号的公开函数，理论上可被其它 C++ 代码直接调用；它自带 dtype 校验，保证「不管谁调，传错 dtype 一定报错」。这是一种模块化的稳健设计，开销可忽略（一次比较）。

**练习 2**：为什么 bf16 的 per-headdim TU 没有 `FORCE_QK_F16`/`FORCE_PV_F16` 开关，而 fp16f32 有？

**答案**：因为 `FORCE_*_F16` 的语义是「把累加器从 f32 降回 f16」。bf16 在 GPU 上**根本没有 bf16-输入/f16-累加 的 MMA PTX 指令**，降回 f16 物理上不可行；所以 bf16 的累加器只能恒为 f32，代码生成时就写死 1、不提供开关。fp16 则两种累加器都有对应 PTX，才允许用户在精度与速度间权衡。

---

### 4.4 `PYBIND11_MODULE` 与 Python↔C++ 接入链路

#### 4.4.1 概念说明

前 3 节都在 C++ 内部，现在要回答：**Python 怎么调到 `ffpa_attn_forward`？** 答案是 pybind11 的 `PYBIND11_MODULE` 宏，它把 C++ 函数注册成一个 Python 扩展模块（编译产物 `ffpa_attn._C`），再用 `m.def(...)` 把函数挂成模块属性。

但 FFPA 没有让上层直接调 `_C.ffpa_attn_forward`，而是又包了一层 `torch.library` 自定义算子 `_fwd_cuda`。原因有二（详见 u3-l5）：①需要 `register_fake` 提供 meta 实现，好让 `torch.compile` 能 trace；②要在算子内部统一分配输出张量 `O` 与对齐的 `softmax_lse`。所以完整接入是**双层包装**：Python 算子 `_fwd_cuda`（分配输出）→ 调 `_C.ffpa_attn_forward`（C++ 分发）。

还有一个常量要对接：**acc 编码**。Python 侧 `CUDABackend.acc` 是字符串 `"f16"/"f32"`，需翻译成 0/1 才能传过算子 schema（schema 里 acc 是 `int`）。这个翻译由 `CUDABackend.acc_code` 属性完成。

#### 4.4.2 核心流程

从 Python 调用到 C++ 的完整接入：

```
1. ffpa_attn_func(...) → _FFPAAttnFunc.forward
2. forward 检测 forward_meta 是 CUDABackend
3. 调 _ffpa_attn_forward_cuda(q,k,v,O,attn_bias, stages, acc_code, ...)   # _ffpa_fwd.py
4. → torch.ops.ffpa_attn._fwd_cuda(...)                                    # cuda/__init__.py
5. _fwd_cuda_torch_op:
     a. O = empty_like(Q)
     b. softmax_lse = empty(B, Nh_q, 对齐Nq, fp32)
     c. _ffpa_attn_fwd_cuda = _C.ffpa_attn_forward   ← pybind 绑定
     d. _ffpa_attn_fwd_cuda(Q,K,V,attn_bias,O,lse, stages,acc,causal,...)
6. 进入 ffpa_attn_api.cc 的 ffpa_attn_forward（即 4.2 的二级分发）
```

acc 的翻译：`CUDABackend.acc="f32"` → `acc_code` 返回 `_ACC_F32=1` → 经算子 schema 传到 C++ `ffpa_attn_forward(..., acc=1, ...)`。

#### 4.4.3 源码精读

**acc 编码常量**——[`functional.py:47-48`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L47-L48)：`_ACC_F16 = 0`、`_ACC_F32 = 1`，与 `ffpa_attn_api.cc:27` 的注释完全对齐：

```python
_ACC_F16 = 0
_ACC_F32 = 1
```

**CUDABackend 与 acc_code**——[`functional.py:150-171`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L150-L171)。默认 `acc="f32"`，`stages` 随架构（Hopper+ 用 4，否则 3）。`acc_code` 属性把字符串翻译成整数：

```python
@dataclass
class CUDABackend(Backend):
  name: str = "cuda"
  acc: str = "f32"
  stages: int = 4 if _is_hopper_or_later() else 3
  ...
  @property
  def acc_code(self) -> int:
    return _ACC_F32 if self.acc == "f32" else _ACC_F16
```

**Python 侧的 bf16 校验**（对应 4.2 的 C++ 校验）——[`functional.py:579-585`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L579-L585)，在进入 C++ 前就拦下非法组合。

**forward 中的 CUDA 分支调用**——[`functional.py:778-792`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L778-L792)，注意传入的是 `forward_meta.stages` 与 `forward_meta.acc_code`（已翻译的整数）：

```python
O, lse = _ffpa_attn_forward_cuda(
    q, k, v, O, attn_bias,
    forward_meta.stages,
    forward_meta.acc_code,      # 整数 acc，穿过 schema
    int(meta.attn_meta.is_causal),
    meta.attn_meta.scale, ...
)
```

**Python 算子 `_fwd_cuda` 分配输出并调 C**——[`src/ffpa_attn/cuda/__init__.py:31-78`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cuda/__init__.py#L31-L78)。第 52-61 行分配 `O` 与对齐到 8 的 `softmax_lse`，第 62-77 行调用 `_ffpa_attn_fwd_cuda`（即 `_C.ffpa_attn_forward`）：

```python
O = torch.empty_like(Q)
seqlen_q_aligned = ((seqlen_q + 7) // 8) * 8
softmax_lse = torch.empty(Q.size(0), Q.size(1), seqlen_q_aligned, ...)
_ffpa_attn_fwd_cuda(Q, K, V, attn_bias, O, softmax_lse[..., :seqlen_q],
                    stages, acc, causal, softmax_scale, ...)
```

而 `_ffpa_attn_fwd_cuda` 的来源在文件顶部——[`cuda/__init__.py:4-8`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cuda/__init__.py#L4-L8)：

```python
from .. import _C as _cuda_ext
_ffpa_attn_fwd_cuda = _cuda_ext.ffpa_attn_forward    # ← pybind 绑定的 C++ 函数
CUDA_FWD_AVAILABLE = bool(getattr(_cuda_ext, "CUDA_FWD_AVAILABLE", False))
```

**`_C` 模块本身的注册**——[`ffpa_attn_api.cc:123-138`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/ffpa_attn_api.cc#L123-L138)。`PYBIND11_MODULE(TORCH_EXTENSION_NAME, m)` 把整个编译单元注册成名为 `TORCH_EXTENSION_NAME`（即 `ffpa_attn._C`）的 Python 模块；`m.def` 挂函数、`m.attr` 挂常量：

```cpp
PYBIND11_MODULE(TORCH_EXTENSION_NAME, m) {
  m.def("ffpa_attn_forward", &ffpa_attn_forward, "FFPA unified prefill attention dispatch ...");
  m.def("ffpa_attn_backward", &ffpa_attn_backward, "Deprecated ...; always raises");
#ifdef ENABLE_FFPA_CUDA_IMPL
  m.attr("CUDA_FWD_AVAILABLE") = py::bool_(true);
  m.attr("CUDA_AVAILABLE") = py::bool_(true);
#else
  m.attr("CUDA_FWD_AVAILABLE") = py::bool_(false);
  m.attr("CUDA_AVAILABLE") = py::bool_(false);
#endif
  m.attr("CUDA_BWD_AVAILABLE") = py::bool_(false);
}
```

注意第 131-137 行的 `CUDA_FWD_AVAILABLE`：它随 `ENABLE_FFPA_CUDA_IMPL` 在**编译期**定型为 `true`/`false`，Python 侧 [`cuda/__init__.py:8`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cuda/__init__.py#L8) 读它来探测「这个包到底编没编 CUDA 前向」——这正是 u3-l1 讲过的「CUDA 用编译期常量探测能力」。

**setup.py 把生成文件送进编译**——[`setup.py:121-143`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/setup.py#L121-L143)，`CUDAExtension` 的 `sources=` 来自 `ENV.get_build_sources(build_pkg=True)`，而 [`env.py:734-758`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/env.py#L734-L758) 返回的就是 `[ffpa_attn_api.cc] + 生成的所有 .cu`：

```python
# env.py:755
build_sources = [csrc("cuffpa", "ffpa_attn_api.cc")] + generated_sources
```

即 `ffpa_attn_api.cc`（含 PYBIND11_MODULE）是**唯一手写**的编译入口，其余 `.cu` 全部由 `generate_split_headdim_sources` 生成。

#### 4.4.4 代码实践

下面是一段**示例代码**（非项目原有，仅演示如何把链路跑通），演示用 `forward_backend='cuda'` 触发本讲的全链路分发：

```python
# 示例代码：触发 CUDA 后端的完整 (dtype, acc, head_dim) 分发
# 前提：已用 ENABLE_FFPA_CUDA_IMPL=1 构建，且 ffpa_attn.cuda.CUDA_FWD_AVAILABLE 为 True
import torch
from ffpa_attn import ffpa_attn_func

B, Nh, Nq, Nkv, D = 1, 8, 512, 512, 512
q = torch.randn(B, Nh, Nq,   D, dtype=torch.float16, device="cuda")
k = torch.randn(B, Nh, Nkv, D, dtype=torch.float16, device="cuda")
v = torch.randn(B, Nh, Nkv, D, dtype=torch.float16, device="cuda")

# acc 默认 "f32"（acc_code=1），stages 随架构。分发链：
#   dtype=kHalf, acc=1  →  ffpa_attn_fwd_fp16f32  →  case 512  →  fp16f32_d512
out = ffpa_attn_func(q, k, v, forward_backend="cuda", acc="f32")
print(out.shape)  # 期望 [1, 8, 512, 512]
```

1. **目标**：验证整条 Python→C++ 分发链能命中 4.3 的 `fp16f32_d512`。
2. **步骤**：先用 `FFPA_DEV_HEADDIMS="512" ENABLE_FFPA_CUDA_IMPL=1` 快速构建（只编一个 head_dim，秒级出 `_C`），再运行上面的示例代码。
3. **观察**：把 `ffpa_attn_api.cc` 第 53 行临时加一行 `printf("dispatch: dtype=fp16 acc=%ld\n", acc);`（**仅本地调试，勿提交**），重编后运行，确认打印 `acc=1`。
4. **预期结果**：输出形状 `[1,8,512,512]`，且与 SDPA 参考的 `max_abs_err` 在 fp16 量级（~1e-2）。
5. 「待本地验证」：若改 `acc="f16"`，应命中 `fp16f16_d512`（acc=0）；若把 q 改成 bf16，Python 侧 [`functional.py:579-585`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L579-L585) 会先于 C++ 抛 `ValueError`（除非同时设 `acc="f32"`）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 Python 侧不直接 `import ffpa_attn._C; _C.ffpa_attn_forward(...)`，而要再包一层 `torch.ops.ffpa_attn._fwd_cuda`？

**答案**：两个原因。①`torch.compile` 需要 `register_fake` 提供 meta 实现（只推形状/dtype 不跑真 kernel），直接调 `_C` 会被当成不透明 C 调用、无法 trace；包成 `torch.library` 算子后 Dynamo 才认。②输出张量 `O` 与对齐的 `softmax_lse` 需要在算子内部按统一规则分配（见 `cuda/__init__.py:52-61`），把这层逻辑收进算子实现，调用方就不用关心对齐细节。

**练习 2**：`PYBIND11_MODULE` 里 `m.attr("CUDA_BWD_AVAILABLE") = py::bool_(false)` 是硬编码的 `false`，这反映了什么事实？

**答案**：FFPA 的手写 CUDA 后端**只实现前向、不实现反向**（见 u7-l1、u3-l1）。native CUDA backward 已被移除（[`ffpa_attn_api.cc:99-121`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/ffpa_attn_api.cc#L99-L121) 的 `ffpa_attn_backward` 一调就抛错），反向必须配 `backward_backend='triton'` 或 `'sdpa'`。把这个能力位硬编码 `false`，让 Python 侧的探测逻辑（`CUDA_BWD_AVAILABLE`）永远返回 False，从而在配置层就阻止用户选 CUDA 反向。

---

## 5. 综合实践

把本讲四条主线串起来，完成下面这个**全链路追踪任务**（本讲指定的代码实践任务）：

**任务**：给定一次 `ffpa_attn_func(q, k, v, forward_backend="cuda", acc=...)` 调用，跟踪它从 Python 到 GPU kernel 的每一跳，并回答两个问题。

**背景数据**：

- `q.dtype = torch.bfloat16`，`D = 512`。
- 想用 `acc = "f16"`（即 acc_code=0）。

**要做的事**：

1. **写出 dtype=bf16 时 acc 必须为 1（f32）的原因**。
   - 提示：从 GPU 硬件指令集（PTX）角度找根因，再指出本讲在**哪三层**强制了这个约束（Python / C++ 入口 / 代码生成），各给出 `文件:行号` 与抛错类型。
2. **找出生成文件并解释 dispatch 如何选中**：换一组数据 `q.dtype = torch.float16`，`acc = "f32"`（acc_code=1），`D = 512`。
   - 在 `csrc/cuffpa/generated/` 下定位对应的 per-headdim 文件与符号。
   - 写出从 `ffpa_attn_forward` 到 `launch_ffpa_attn_fwd_template` 的完整 4 跳，每跳标注判定条件与所在文件行号。
3. **加分项**：解释为什么把 dispatch 写成「3 个 `switch(d)` 函数」而不是「1 个 `switch(d)` 内再嵌 `switch(dtype/acc)`」——从代码生成（`_render_dispatch_tu` 模板化）与单 TU 编译体积两个角度。

**参考答案要点**：

- 问题 1：根因是 **GPU 不存在 bf16 输入 + f16 累加的 MMA PTX**。三层强制：①Python [`functional.py:579-585`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L579-L585) `ValueError`；②C++ [`ffpa_attn_api.cc:65-70`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/ffpa_attn_api.cc#L65-L70) `invalid_argument`；③代码生成 [`env.py:628-652`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/env.py#L628-L652) bf16 TU 不发 `bf16f16` 符号、累加器写死 1。
- 问题 2：文件 `ffpa_attn_fwd_fp16_hdim512.cu`，符号 `ffpa_attn_fwd_fp16f32_d512`。4 跳：
  1. `ffpa_attn_forward`：`dtype==kHalf && acc==1` → `ffpa_attn_fwd_fp16f32`（[`api.cc:58-61`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/ffpa_attn_api.cc#L58-L61)）。
  2. `ffpa_attn_fwd_fp16f32`：`d=Q.size(3)=512` → `case 512` → `ffpa_attn_fwd_fp16f32_d512`（[`dispatch.cu:66`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/generated/ffpa_attn_fwd_dispatch.cu#L66)）。
  3. `ffpa_attn_fwd_fp16f32_d512`：按 `stages` 选 → `launch_ffpa_attn_fwd_template<__half,512,1,1,S>`（[`fp16_hdim512.cu:66`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/generated/ffpa_attn_fwd_fp16_hdim512.cu#L66)）。
  4. `launch_ffpa_attn_fwd_template` 进入 u7-l1 的 GPU kernel 主循环。
- 问题 3：①模板化——`_render_dispatch_tu` 用一个 `_fn(name, prefix, dtype)` 模板批量生成 3 个同构函数（[`env.py:694-720`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/env.py#L694-L720)），嵌套 switch 会破坏这种规整。②编译体积——dispatch TU 故意保持轻量（只 `#include` 声明头，不含模板实例化），3 个平铺函数比嵌套 switch 更利于阅读与 diff。

## 6. 本讲小结

- FFPA 用 **per-headdim 代码生成**把一个重型模板 switch 拆成 27 份 `.cu`，让 `MAX_JOBS` 能并行调 nvcc，构建时间大幅下降；`env.py::generate_split_headdim_sources` 是生成器，`_write_if_changed` 保证增量构建零开销。
- C++ 分发是**三级**：统一入口 `ffpa_attn_forward`（按 **dtype + acc** 分发）→ `(dtype,acc)` 入口 `ffpa_attn_fwd_{fp16f16,fp16f32,bf16f32}`（`switch(d)`）→ per-headdim 符号 `..._d{D}`（实例化 `launch_ffpa_attn_fwd_template`）。
- **acc 编码 `0=f16 / 1=f32`**（[`api.cc:27`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/ffpa_attn_api.cc#L27)）；函数名「`fp16f32`」= fp16 输入 + fp32 累加。
- **bf16 必须 fp32 累加**是 GPU PTX 限制，被 **Python、C++ 入口、代码生成三层**同时强制，纵深防御、互为兜底。
- `PYBIND11_MODULE` 把 `ffpa_attn_forward` 绑成 `ffpa_attn._C.ffpa_attn_forward`；上层又包了 `torch.library` 算子 `_fwd_cuda` 以支持 `torch.compile` 与统一输出分配；`CUDA_FWD_AVAILABLE` 是编译期常量能力位。
- 文件名只编码「存储 dtype + head_dim」，函数符号多编码「acc」，故每个 head_dim 2 文件、3 声明；`ffpa_attn_api.cc` 是唯一手写的编译入口。

## 7. 下一步学习建议

- **构建配置全貌** → 下一讲 **u7-l4「env.py 构建配置与 `FFPA_*` 构建期变量」**：本讲只用了 `get_enabled_headdims`/`generate_split_headdim_sources`，u7-l4 会系统讲 `get_build_arch_list`、`FFPA_BUILD_ARCH` 别名解析、`get_build_cuda_cflags` 与 head_dim/stages 集合优先级，把「编什么、怎么编」讲全。
- **运行期开关** → **u7-l5「运行时 kernel 选择开关」**：本讲提到的 `ENABLE_FFPA_FORCE_QK_F16/PV_F16`、swizzle、persist 等都是运行期（实际是经 `-D` 宏落进 kernel 的）开关，u7-l5 会分类细讲。
- **代码生成器细节** → 想深入可逐行读 [`env.py:513-732`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/env.py#L513-L732) 的 `_render_*` 系列方法，对照生成的 `.cu` 文件理解「字符串模板 → C++ 源码」的渲染过程。
- **承接 u7-1**：本讲止步于 `launch_ffpa_attn_fwd_template` 的调用；该模板内部的 GPU 执行流程（g2s/MMA/online softmax/swizzle/persist）已在 u7-l1、u7-l2 讲完，可回看对照「host 分发」与「device 执行」两侧。
