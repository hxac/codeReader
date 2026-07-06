# 仓库目录结构与代码地图

## 1. 本讲目标

本讲是 FFPA 源码阅读的「地图篇」。学完之后，你应该能够：

- 说出仓库根目录下每个文件夹和关键文件（`src/`、`csrc/`、`bench/`、`tests/`、`tools/`、`docs/`、`env.py`、`setup.py`、`pyproject.toml`）的职责。
- 记住 `src/ffpa_attn` 这个 Python 包下 `aten / cuda / triton / cute / ray / cli` 六个子包分别对应什么，以及顶层的 `ffpa_attn_interface.py` 与 `functional.py` 各自管什么。
- 理解 `csrc/cuffpa`（手写 CUDA 模板）与 `csrc/cuffpa/generated/`（按 head_dim 自动生成的翻译单元）之间的关系，并能看懂生成文件的命名规则。
- 在拿到一个功能需求（例如「我想看 Triton 前向 kernel」「我想跑一个基准」）时，能立刻定位到正确的源码目录，而不是在仓库里盲目搜索。

> 本讲只画地图、不深入任何 kernel 实现。kernel 的逐行精读留到第 4～7 单元。

## 2. 前置知识

阅读本讲前，请确保已经学完 [u1-l1](./u1-l1-what-is-ffpa-split-d.md)（FFPA 是什么、Split-D 思想、适用边界）和 [u1-l2](./u1-l2-install-and-build-modes.md)（三种构建模式、`setup.py` 与 `env.py` 的分工）。本讲会用到其中几个术语：

- **head_dim（D）**：每个注意力头的维度，FFPA 的主战场是 `D ∈ [320, 1024]`。
- **四个后端**：`SDPA`（基线）、`CUDA`（手写、仅前向）、`Triton`（默认、前向+反向）、`CuTeDSL`（H200 上最快）。
- **`_C` 扩展**：手写 CUDA 编译出的二进制扩展 `ffpa_attn._C`，只有设了 `ENABLE_FFPA_CUDA_IMPL=1` 才会生成。
- **构建期 vs 运行期**：`env.py` 既在 `pip install` 时决定「编什么」，也在运行时决定「调哪个 kernel」。

另外补充两个文件系统层面的常识，初学者容易混淆：

- **翻译单元（Translation Unit, TU）**：C++/CUDA 中每个被独立编译的 `.cu`/`.cc` 文件。FFPA 把每个 head_dim 拆成独立 TU，是为了让多个 nvcc 进程并行编译、缩短构建时间。
- **`.cuh`**：CUDA 的头文件（相当于 C++ 的 `.h` / `.hpp`），用 `#include` 引入，本身不单独编译。

## 3. 本讲源码地图

| 文件 / 目录 | 作用 |
|---|---|
| `README.md` | 项目门面，含能力矩阵、安装命令、四后端对比表 |
| `src/ffpa_attn/__init__.py` | Python 包的「总出口」，决定 `from ffpa_attn import xxx` 能拿到什么 |
| `pyproject.toml` | 声明依赖、构建后端、**包发现规则**（决定哪些目录被打包进 wheel） |
| `docs/env.md` | 所有构建期 / 运行期环境变量的权威文档 |
| `csrc/cuffpa/` | 手写 CUDA 前向 kernel 的模板头文件 + pybind 入口 |
| `csrc/cuffpa/generated/` | 由 `env.py` 自动生成的「每个 head_dim 一个 TU」+ 统一 dispatch |

下面进入正文。我们先从仓库根目录看起，再钻进 `src/ffpa_attn`，最后看 `csrc/cuffpa` 与辅助目录。

## 4. 核心概念与源码讲解

### 4.1 仓库根目录布局：Python 包与 C++ 源码「双轨制」

#### 4.1.1 概念说明

FFPA 是一个典型的 **Python 包 + 可选 C++/CUDA 扩展** 的项目。理解目录结构的第一把钥匙是：仓库里其实有「两条轨」并行存在：

1. **Python 轨（`src/ffpa_attn/`）**：无论你是否编译 CUDA，这部分都存在，也是用户唯一会 `import` 的部分。默认的 Triton 后端、CuTeDSL 后端、autograd 分发、自动调优、基准 CLI 全在这里，纯 Python / Triton。
2. **C++/CUDA 轨（`csrc/cuffpa/`）**：手写的 CUDA 前向 kernel，只有设了 `ENABLE_FFPA_CUDA_IMPL=1` 时才会被编译成 `ffpa_attn._C`（详见 u1-l2）。

这两条轨通过 **`pyproject.toml` 的包发现规则**被严格隔离开：`csrc*` 不会被当作 Python 包打进 wheel。这意味着如果你只做 `pip install -e . --no-build-isolation`（Triton-only），安装产物里**完全不包含任何 C++ 源码**——这是 u1-l2 强调过的关键结论，本讲我们从目录结构的角度再确认一次。

仓库根目录的核心成员如下：

```
ffpa-attn/
├── README.md            # 项目门面
├── LICENSE              # Apache-2.0
├── pyproject.toml       # 依赖、构建后端、包发现规则
├── setup.py             # 是否编译 _C 的总开关
├── setup.cfg            # yapf / flake8 等工具配置
├── env.py               # 构建期 + 运行期的 FFPA_* 配置中心（详见 u1-l2 / 第7单元）
├── mkdocs.yml           # 文档站点配置（readthedocs）
├── MANIFEST.in          # sdist 打包清单
├── src/                 # ← Python 轨：ffpa_attn 包
├── csrc/                # ← C++/CUDA 轨：cuffpa 手写 kernel
├── bench/               # 基准测试脚本与参考实现
├── tests/               # pytest 测试套件
├── tools/               # 构建辅助脚本（build_fast.sh 等）
├── docs/                # mkdocs 文档源
└── .github/             # CI / pre-commit 配置
```

#### 4.1.2 核心流程

当你在仓库根目录执行一次安装时，目录是这样被消费的：

1. `pip` 读取 `pyproject.toml` → 找到构建后端 `setuptools.build_meta`。
2. `setuptools` 调用 `setup.py` → 根据 `ENABLE_FFPA_CUDA_IMPL` 决定要不要触发 `env.py` 生成 `csrc/cuffpa/generated/` 并编译 `_C`。
3. `setuptools` 按 `pyproject.toml` 的 `[tool.setuptools.packages.find]` 规则，从 `src/` 下发现 `ffpa_attn` 包（排除 `csrc*` / `tests*` / `bench*` 等）。
4. 最终 wheel 里只有 `ffpa_attn/`（+ 可选的 `_C` 编译产物 `.so`）。

#### 4.1.3 源码精读

**包发现规则：把 C++ 源码挡在 Python wheel 之外。** 注意 `package-dir = {"" = "src"}` 表示源码根在 `src/`，而 `exclude` 列表把所有非 Python 包排除：

[pyproject.toml:L87-L106](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/pyproject.toml#L87-L106)

> 这段中文说明：`where = ["src"]` 告诉 setuptools「到 `src/` 下找包」；`exclude` 把 `csrc*`、`tests*`、`bench*`、`tools*`、`docs*`、`build*` 等全部排除。所以 `import ffpa_attn` 永远只能看到 `src/ffpa_attn` 里的东西，看不到 `csrc/` 的 C++ 源码。

**Python 包的总出口：** 顶层 `__init__.py` 决定了 `from ffpa_attn import xxx` 能直接拿到哪些名字：

[src/ffpa_attn/__init__.py:L1-L14](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/__init__.py#L1-L14)

> 这段中文说明：对外暴露的全部家当只有两类——两个函数（`ffpa_attn_func`、`ffpa_attn_varlen_func`，来自 `ffpa_attn_interface.py`）和五个 Backend 配置类（`Backend` 基类 + 四个子类，来自 `functional.py`），外加版本号。**记住这张短清单**，它是整个公共 API 的全部。

#### 4.1.4 代码实践

1. **实践目标**：亲手验证「Triton-only 安装产物里没有 C++ 源码」。
2. **操作步骤**：
   - 在仓库根目录执行 `pip install -e . --no-build-isolation`（Triton-only，不要设 `ENABLE_FFPA_CUDA_IMPL`）。
   - 执行 `python -c "import ffpa_attn, os; p=os.path.dirname(ffpa_attn.__file__); print(p)"`，打印出已安装包的物理路径。
   - 在该路径下查看：是否存在 `_C*.so`？是否存在任何 `.cu` / `.cuh` 文件？
3. **需要观察的现象**：包目录里只有 `.py` 文件和 `triton/configs/*.json`，**没有** `_C.so`、**没有** `.cu`/`.cuh`。
4. **预期结果**：因为没设 `ENABLE_FFPA_CUDA_IMPL=1`，`setup.py` 不会编译 `_C`；又因为 `pyproject.toml` 排除了 `csrc*`，C++ 源码根本不会进包。这印证了 4.1.1 的「双轨隔离」。
5. 如果无法在本地构建（如无 GPU/nvcc），明确标注「待本地验证」。

#### 4.1.5 小练习与答案

- **练习 1**：为什么 `csrc/` 里的 `.cc`/`.cu` 文件不会出现在 `import ffpa_attn` 之后能看到的模块里？
  - **答案**：因为 `pyproject.toml` 的 `[tool.setuptools.packages.find].exclude` 显式排除了 `csrc*`，它根本不被当作 Python 包；此外 C++ 源码只有被 nvcc 编译成 `_C.so` 后才会在运行时可见，而 Triton-only 构建不会触发这一步。
- **练习 2**：`env.py` 放在仓库根目录而不是 `src/ffpa_attn/` 下，说明了它的什么身份？
  - **答案**：它是一个**构建期脚本**（被 `setup.py` import 来生成源码、拼 nvcc 参数），同时兼任运行期配置读取；放在根目录与 `setup.py` 平级，强调它是「构建工具」而非「运行时库」的一部分。注意它因此**不会**被打进 wheel。

---

### 4.2 `src/ffpa_attn` Python 包：六子包 + 两顶层模块

#### 4.2.1 概念说明

这是整个项目最核心的目录，也是后面绝大多数讲义的工作场所。它的结构遵循一个非常清晰的设计哲学：**一个公共入口 + 一层分发 + 多个可替换后端**。

- **公共入口**（`ffpa_attn_interface.py`）：用户调用的 `ffpa_attn_func`，负责输入校验、布局归一化、对外文档。
- **分发层**（`functional.py`）：定义 Backend 配置类、`FFPAAttnMeta` 校验/回退、`autograd.Function`，把一次调用路由到具体后端。
- **六个子包**：每个子包是一个（或一类）后端 / 工具的实现，互相之间基本独立，只在分发层汇合。

#### 4.2.2 核心流程

一次 `ffpa_attn_func(q,k,v)` 调用在 Python 包内部的流向：

```
ffpa_attn_func (ffpa_attn_interface.py)
        │  输入校验、布局归一化
        ▼
functional.py: _FFPAAttnFunc (autograd.Function)
        │  按 head_dim + Backend 分发
        ├──► aten/      （SDPA 后端：小 D 回退，D≤256）
        ├──► cuda/      （手写 CUDA 后端：仅前向，需 _C）
        ├──► triton/    （默认 Triton 后端：前向+反向）
        └──► cute/      （CuTeDSL 后端：H200 最快）
```

辅助子包 `ray/`（多卡自动调优）和 `cli/`（基准命令行）不参与上面的前向/反向链路，而是独立工具。

#### 4.2.3 源码精读

`src/ffpa_attn` 的子包结构（按职责分组）：

```
src/ffpa_attn/
├── __init__.py              # 包总出口（见 4.1.3）
├── version.py               # __version__ 解析（setuptools-scm 生成 _version.py）
├── ffpa_attn_interface.py   # 【公共入口】ffpa_attn_func / ffpa_attn_varlen_func
├── functional.py            # 【分发层】Backend 配置类 + Meta 校验 + autograd.Function
├── logger.py                # FFPA_LOGGER_LEVEL / once 去重 / rank0 过滤
├── bench.py                 # `python -m ffpa_attn.bench` 入口
├── autotune.py              # `python -m ffpa_attn.autotune` 持久化调优 CLI
│
├── aten/                    # SDPA 后端：封装 torch 原生 flash/efficient 注意力
│   ├── _flash_fwd.py        #   _flash_attn_forward_aten
│   ├── _flash_bwd.py        #   _flash_attn_backward_aten
│   └── _efficient_bwd.py    #   _efficient_attn_backward_aten
│
├── cuda/                    # 手写 CUDA 后端的 Python 包装（需 _C）
│   ├── _ffpa_fwd.py         #   调用 ffpa_attn._C 的前向
│   └── _ffpa_bwd.py         #   反向（实际路由到 triton/sdpa）
│
├── triton/                  # 默认 Triton 后端（kernel 实现 + 自动调优）
│   ├── _ffpa_fwd.py         #   前向 kernel：online softmax / Split-D / decode
│   ├── _ffpa_bwd.py         #   反向 kernel：delta 预处理 / dKdV / dQ
│   ├── _ffpa_fwd_sm90.py    #   SM90 专用前向变体
│   ├── _ffpa_bwd_sm90.py    #   SM90 专用反向变体
│   ├── _autotune_utils.py   #   fast/max 候选 config 生成
│   ├── _persistent_autotune.py  # 运行时持久化 config 查找
│   └── configs/*.json       #   预调优好的持久化配置（按设备分文件）
│
├── cute/                    # CuTeDSL 后端（SM80 通用 + SM90 专用）
│   ├── _fwd_*_sm{80,90}.py  #   前向 kernel（generic / d384 / d512 特化）
│   ├── _dkdv_*_sm{80,90}.py #   反向 dK/dV kernel
│   ├── _dq_*_sm{80,90}.py   #   反向 dQ kernel
│   ├── _bwd_preprocess.py   #   delta 预处理
│   ├── README.md            #   内部包说明
│   └── utils/               #   tile_scheduler / pipeline / named_barrier / ...
│
├── ray/                     # 多 GPU 并行自动调优
│   ├── _autotune_engine.py  #   actor pool + 队列调度（run_ray_autotune）
│   └── _autotune_worker.py  #   每 GPU 一个 actor（私有 Triton cache）
│
└── cli/                     # 基准 CLI 实现
    ├── _bench.py            #   `python -m ffpa_attn.bench` 主逻辑
    ├── _flops.py            #   TFLOPS 公式
    ├── _runner_fwd.py       #   前向 8 类用例
    └── _runner_bwd.py       #   反向用例
```

**SDPA 后端就是「小 D 回退」**。`aten/` 子包的 docstring 直白地道出了它的定位：

[src/ffpa_attn/aten/__init__.py:L1-L6](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/aten/__init__.py#L1-L6)

> 这段中文说明：`aten` 包是「D ≤ 256 时回退用的 SDPA 后端」，它直接封装 PyTorch 自带的 ATen flash / efficient 注意力，没有自己的 kernel。这正是 u1-l1 讲过的「FFPA 在小 D 时自动回退到 SDPA」在代码里的落点。

> **术语提示**：`aten` 指 PyTorch 的底层 C++ 张量算子库 "ATen"（A Tensor Library），`flash` 即 FlashAttention，`efficient` 指 xFormers 风格的 memory-efficient attention。`aten` 包只是给这三个原生算子套了一层 Python 适配，让分发层能用统一接口调用它们。

**CuTeDSL 子包的「包名 vs 后端名」不一致**——这是一个容易踩坑的点：

[src/ffpa_attn/cute/README.md:L1-L12](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/README.md#L1-L12)

> 这段中文说明：**内部目录叫 `cute/`，但对外后端字符串是 `"cutedsl"`，配置类叫 `CuTeDSLBackend`**。所以你在代码里看到 `backend="cutedsl"`，要去 `cute/` 目录下找实现，不要满世界找 `cutedsl/` 目录。

#### 4.2.4 代码实践

1. **实践目标**：把上面的子包结构变成你自己画出来的图，并为每个子包写一句职责说明。
2. **操作步骤**：
   - 用 `python -c "import ffpa_attn, os; print(os.path.dirname(ffpa_attn.__file__))"` 找到已安装包路径（或直接在仓库 `src/ffpa_attn/` 下操作）。
   - 用 `tree src/ffpa_attn -L 1`（或手画）列出顶层模块与子包。
   - 对 `aten / cuda / triton / cute / ray / cli` 六个子包，各写一句话职责（参考 4.2.3 的结构图）。
   - 再读一遍 `src/ffpa_attn/__init__.py`，确认「公共 API 只有 2 个函数 + 5 个 Backend 类」。
3. **需要观察的现象**：子包数量正好是 6 个；顶层除了 `__init__.py` 外，最关键的两个文件是 `ffpa_attn_interface.py`（入口）和 `functional.py`（分发）。
4. **预期结果**：你能脱稿说出「想看 Triton 前向 → `triton/_ffpa_fwd.py`；想看 SDPA 回退 → `aten/`；想看手写 CUDA 前向的 Python 包装 → `cuda/_ffpa_fwd.py`」。
5. 若本地未安装，可直接在仓库 `src/ffpa_attn/` 下阅读，结果一致。

#### 4.2.5 小练习与答案

- **练习 1**：用户传 `backend="cutedsl"`，对应的源码在哪个子包？为什么目录名和后端名不一样？
  - **答案**：在 `src/ffpa_attn/cute/`。`cute` 是 CuTeDSL 的缩写，作为内部包名更短；对外保持 `"cutedsl"` 后端字符串与 `CuTeDSLBackend` 类名是为了和文档、用户习惯一致（见 `cute/README.md` 第 9-10 行）。
- **练习 2**：`functional.py` 和 `ffpa_attn_interface.py` 谁更「靠外」（更接近用户）？
  - **答案**：`ffpa_attn_interface.py` 更靠外，它定义 `ffpa_attn_func` 并被 `__init__.py` 直接导出；`functional.py` 在它「下层」，负责把请求分发到具体后端，是入口与后端之间的桥梁。
- **练习 3**：`triton/configs/*.json` 是干什么用的？为什么它会被 `pyproject.toml` 的 `package-data` 单独声明？
  - **答案**：存放预调优好的「持久化自动调优配置」（详见第 8 单元）。因为 `.json` 不是 Python 模块，默认不会被打包，所以 `[tool.setuptools.package-data]` 里显式声明 `"ffpa_attn.triton" = ["configs/*.json"]` 才能把它们带进 wheel（见 pyproject.toml 第 89-90 行）。

---

### 4.3 `csrc/cuffpa`：手写 CUDA 模板与按 head_dim 代码生成

#### 4.3.1 概念说明

`csrc/cuffpa/` 是 CUDA 后端的源码（"cu" = CUDA，"ffpa" = 本项目）。它分成两类文件：

1. **手写的模板头文件（`.cuh` / `.cc`）**：实现 attention 前向主循环、MMA 封装、TMA/cp_async 异步加载、swizzle 布局等**与具体 head_dim 无关**的通用逻辑。
2. **自动生成的翻译单元（`generated/*.cu`）**：由 `env.py` 在构建期生成，**每个 head_dim 一个 `.cu` 文件**，把手写模板实例化为针对特定 head_dim 的具体函数。

为什么要做「按 head_dim 代码生成」这种看似繁琐的事？核心原因是**编译时间和模板特化**：

- CUDA kernel 内部大量使用 `constexpr int kHeadDim` 这类编译期常量来做循环展开、SMEM 大小计算。一个 kernel 模板为每个 head_dim 实例化一次。
- 如果把所有 head_dim 实例化塞进**一个** `.cu` 文件，nvcc 要串行编译一个巨大的翻译单元，极慢；而且改一个 head_dim 要全量重编。
- 拆成「每个 head_dim 一个 TU」后，`MAX_JOBS` 个 nvcc 进程可以并行编译（详见 u1-l2 提到的 `MAX_JOBS` 与 `FFPA_NVCC_THREADS`），大幅缩短构建时间。这就是 `docs/env.md` 里那句「per-headdim TU split」的含义。

#### 4.3.2 核心流程

构建期，`generated/` 是这样被生产出来的：

```
env.py (generate_split_headdim_sources)
   │  对每个 (dtype, head_dim) 组合
   ▼
generated/ffpa_attn_fwd_{dtype}_hdim{N}.cu   ← 每个 TU 实例化 launch_ffpa_attn_fwd_template<..., N>
generated/ffpa_attn_fwd_decls.h              ← 所有实例化函数的前向声明
generated/ffpa_attn_fwd_dispatch.cu          ← 统一入口：switch(d) 选 head_dim
   │
   ▼
ffpa_attn_api.cc (PYBIND11_MODULE)           ← 把 dispatch 函数暴露成 Python 可调的 ffpa_attn._C
```

运行期，CUDA 后端的一次前向调用：Python `cuda/_ffpa_fwd.py` → `ffpa_attn._C.ffpa_attn_forward(...)` → `ffpa_attn_api.cc` 按 (dtype, acc) 选 dispatch 函数 → `dispatch.cu` 按 `d=Q.size(3)` 选 head_dim → 对应 TU 里的实例化函数。

#### 4.3.3 源码精读

**手写模板头文件**（与 head_dim 无关的通用 CUDA 逻辑）：

```
csrc/cuffpa/
├── ffpa_attn_fwd.cuh       # 前向主循环模板（online softmax / Split-D）
├── prefill.cuh             # prefill 组织（grid / block 映射）
├── mma.cuh                 # PTX mma 指令封装
├── tma.cuh                 # TMA 异步加载（Hopper）
├── cp_async.cuh            # cp.async 异步加载（Ampere）
├── swizzle.cuh             # SMEM bank-conflict-free 的 swizzle 布局
├── launch_templates.cuh    # launch_*_template 模板函数
├── warp.cuh                # warp 级工具
├── dtype_traits.cuh        # dtype → C++ 类型映射 (__half / __nv_bfloat16)
├── utils.cuh               # 通用工具
├── logging.cuh             # CHECK_TORCH_TENSOR_DTYPE 等校验宏
└── ffpa_attn_api.cc        # ★ pybind11 入口，暴露 ffpa_attn._C
```

> 这些头文件的逐行精读留到第 7 单元（手写 CUDA 后端）。本讲你只需记住：**`.cuh` 是「写一次、到处实例化」的模板**，`ffpa_attn_api.cc` 是唯一的 pybind 入口。

**生成文件的命名规则（本讲重点）**。每个生成 TU 的文件名是：

```
ffpa_attn_fwd_{dtype}_hdim{N}.cu
```

其中 `dtype ∈ {fp16, bf16}` 是**输入张量的存储 dtype**，`N ∈ {256, 320, 384, ..., 1024}` 是 head_dim。注意：**文件名只编码「存储 dtype + head_dim」两层，但文件内部的函数名还会多编码一层「累加器精度 acc」**。函数命名是：

```
ffpa_attn_fwd_{dtype}{acc}_d{N}
```

`{dtype}{acc}` 把「存储 dtype」和「MMA 累加器 dtype」拼在一起。三种组合如下：

| 函数后缀 | 存储 dtype | MMA 累加器 | 说明 |
|---|---|---|---|
| `fp16f16` | fp16 | fp16 | 全 fp16 累加，最快、精度最低 |
| `fp16f32` | fp16 | fp32 | fp16 存储 + fp32 累加（可用 env 开关切 QK/PV 混合精度） |
| `bf16f32` | bf16 | fp32 | bf16 **必须**用 fp32 累加（无 `bf16f16`） |

这就解释了一个表面上的「数量不对齐」：`decls.h` 里每个 head_dim 声明 **3** 个函数，但生成目录里每个 head_dim 只有 **2** 个 `.cu` 文件（`fp16` + `bf16`）。原因是 **fp16 文件里装了 2 个函数**（`fp16f16` + `fp16f32`），而 **bf16 文件里只有 1 个**（`bf16f32`）。

先看 `decls.h`，每个 head_dim 确实声明了 3 个函数（以 d=512 为例）：

[csrc/cuffpa/generated/ffpa_attn_fwd_decls.h:L17-L19](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/generated/ffpa_attn_fwd_decls.h#L17-L19)

> 这段中文说明：d=512 对应三行声明——`fp16f16_d512`、`fp16f32_d512`、`bf16f32_d512`，分别对应上表的三种 (存储, 累加) 组合。

再看 fp16 文件确实**包含两个函数**，且第一行明确写着「自动生成，勿手改」：

[csrc/cuffpa/generated/ffpa_attn_fwd_fp16_hdim512.cu:L1-L5](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/generated/ffpa_attn_fwd_fp16_hdim512.cu#L1-L5)

> 这段中文说明：`AUTO-GENERATED by env.py. DO NOT EDIT.` 是这些文件的统一签名；紧接着是第一个函数 `ffpa_attn_fwd_fp16f16_d512`。

[csrc/cuffpa/generated/ffpa_attn_fwd_fp16_hdim512.cu:L40-L62](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/generated/ffpa_attn_fwd_fp16_hdim512.cu#L40-L62)

> 这段中文说明：同一个 `fp16_hdim512.cu` 文件里，第 40 行还有**第二个**函数 `ffpa_attn_fwd_fp16f32_d512`，它通过运行时读取 `kMmaAccFloat32QK / kMmaAccFloat32PV`（对应 `ENABLE_FFPA_FORCE_QK_F16 / FORCE_PV_F16`）来选择 QK 与 PV 各自的累加精度。这就是「fp16 文件装 2 个函数」的由来。

而 bf16 文件只有一个函数，且累加器固定为 fp32（`kMmaAccFloat32QK=1, kMmaAccFloat32PV=1`）：

[csrc/cuffpa/generated/ffpa_attn_fwd_bf16_hdim512.cu:L5-L20](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/generated/ffpa_attn_fwd_bf16_hdim512.cu#L5-L20)

> 这段中文说明：`bf16_hdim512.cu` 只定义 `ffpa_attn_fwd_bf16f32_d512`，且 `kMmaAccFloat32QK=1, kMmaAccFloat32PV=1` 是硬编码的——这就是「bf16 必须 fp32 累加」在生成代码里的体现，所以 bf16 没有 `bf16f16` 变体。

**统一 dispatch 入口**：`dispatch.cu` 把「按 (dtype, acc) 选定的入口函数」内部再用 `switch(d)` 路由到具体 head_dim：

[csrc/cuffpa/generated/ffpa_attn_fwd_dispatch.cu:L1-L29](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/generated/ffpa_attn_fwd_dispatch.cu#L1-L29)

> 这段中文说明：`ffpa_attn_fwd_fp16f16(...)` 是「fp16 存储 + fp16 累加」的统一入口；它在第 23 行读出 `d = Q.size(3)`，第 24 行 `switch (d)`，按 head_dim 调用对应的实例化函数（如第 29 行 `case 512` → `ffpa_attn_fwd_fp16f16_d512`）。`fp16f32` 与 `bf16f32` 各有结构相同的 dispatch 函数。这三个 dispatch 函数就是 `ffpa_attn_api.cc` 通过 pybind 暴露给 Python 的最终入口。

#### 4.3.4 代码实践

1. **实践目标**：在 `generated/` 目录里找出「fp16 存储、fp32 累加、head_dim=512」对应的生成文件，并解释它的命名规则。
2. **操作步骤**：
   - 进入 `csrc/cuffpa/generated/` 目录。
   - 根据「存储 dtype = fp16」锁定文件名前缀为 `ffpa_attn_fwd_fp16_hdim512.cu`（文件名只含**存储 dtype + head_dim**，不含 acc）。
   - 打开该文件，确认它内部**同时**定义了 `ffpa_attn_fwd_fp16f16_d512`（第 5 行）和 `ffpa_attn_fwd_fp16f32_d512`（第 40 行）两个函数。
   - 打开 `ffpa_attn_fwd_dispatch.cu`，找到 `ffpa_attn_fwd_fp16f32` 这个入口的 `switch(d)`，确认 `case 512` 调用的正是上一步的 `ffpa_attn_fwd_fp16f32_d512`。
3. **需要观察的现象**：
   - 文件名 `fp16_hdim512` **不含** acc 信息；acc 信息只出现在**函数名**里。
   - 一个 `fp16` 文件里有 2 个函数（f16/f32 acc），一个 `bf16` 文件里只有 1 个（f32 acc）。
4. **预期结果**：你能完整复述命名规则——「文件名 = `ffpa_attn_fwd_{存储dtype}_hdim{D}.cu`；函数名 = `ffpa_attn_fwd_{存储dtype}{累加dtype}_d{D}`」，并能解释为什么 decls.h 每 head_dim 有 3 个声明、而目录里每 head_dim 只有 2 个文件。
5. 此实践为纯源码阅读型，无需 GPU 或编译，本地可直接做。

#### 4.3.5 小练习与答案

- **练习 1**：为什么要把每个 head_dim 拆成独立的 `.cu` 翻译单元，而不是全放进一个文件？
  - **答案**：为了让多个 nvcc 进程并行编译（由 `MAX_JOBS` 驱动），缩短构建时间；同时改动单个 head_dim 时只重编一个 TU。代价是生成文件数量多，但它们由 `env.py` 自动生成、无需人工维护。
- **练习 2**：假设运行时 dtype=bf16、head_dim=512，dispatch 会调用哪个函数？为什么没有 `bf16f16` 选项？
  - **答案**：调用 `ffpa_attn_fwd_bf16f32_d512`（入口 `ffpa_attn_fwd_bf16f32` 的 `case 512`）。没有 `bf16f16` 是因为 bf16 数据用 fp16 MMA 累加器在数值上不可行/精度太差，项目硬性规定 bf16 必须用 fp32 累加（见 `bf16_hdim512.cu` 里 `kMmaAccFloat32*=1`）。
- **练习 3**：生成文件第一行 `AUTO-GENERATED by env.py. DO NOT EDIT.` 的含义是什么？如果你想修改某个 head_dim 的 kernel 行为，应该改哪里？
  - **答案**：这些文件是构建期由 `env.py` 的 `generate_split_headdim_sources` 生成的，手动改了会在下次构建被覆盖。要改 kernel 行为，应改**手写模板** `csrc/cuffpa/ffpa_attn_fwd.cuh` / `launch_templates.cuh` 等，或调整 `env.py` 的生成逻辑，然后重新构建。

---

### 4.4 `bench / tests / tools / docs`：基准、测试、构建脚本与文档

#### 4.4.1 概念说明

除了 `src/` 和 `csrc/` 两条主轨，仓库还有四个辅助目录，它们不进 wheel（被 `pyproject.toml` 排除），但对开发、验证、使用 FFPA 不可或缺：

- **`bench/`**：独立的基准脚本与**参考实现**（reference），用于对比 FFPA 与 FlashAttention-2 / SDPA 的性能与正确性。
- **`tests/`**：pytest 测试套件，覆盖前向/反向正确性、monkey-patch、自动调优、编译、日志等。
- **`tools/`**：构建辅助脚本，最常用的是 `build_fast.sh`（ccache 加速构建，见 u1-l2）。
- **`docs/`**：mkdocs 文档源，渲染到 readthedocs。

#### 4.4.2 核心流程

这四个目录在不同场景下被消费：

- **开发时**：`tools/build_fast.sh` 编译 → `tests/` 跑正确性 → `bench/` 测性能 → 改 `docs/` 文档。
- **CI 时**（`.github/`）：跑 `tests/`、`bench/` 的子集，校验 `docs/` 的 mkdocs 链接。
- **用户使用时**：`python -m ffpa_attn.bench`（入口在 `src/ffpa_attn/`，不是 `bench/` 目录）跑内置基准；`python -m ffpa_attn.autotune` 跑持久化调优。注意这两个 `-m` 入口都在 **`src/ffpa_attn/`** 里（`bench.py`、`autotune.py`），而 `bench/` 目录存放的是更原始的、用于出性能图表的脚本与参考实现。

#### 4.4.3 源码精读

四个目录的结构：

```
bench/
├── README.md                # 基准说明 + 各 GPU 性能表（L20 / 5090 / H800 / H200）
├── reference/               # 对比用的参考实现
│   ├── flash_attn_triton.py
│   └── flash_attn_v2_triton.py
└── autotune/
    └── verify_persistent_autotune.py

tests/
├── test_ffpa_fwd.py         # 前向正确性矩阵（dtype×headdim×seqlen）
├── test_ffpa_bwd.py         # 反向正确性
├── test_monkey_patch.py     # monkey-patch 回退不递归
├── test_ffpa_compile.py     # torch.compile 兼容
├── test_ffpa_cute.py        # CuTeDSL 后端
├── test_ffpa_cute_sm80.py   # CuTeDSL SM80 路径
├── test_perf_tflops.py      # TFLOPS 性能
├── test_persistent_autotune_config.py  # 持久化配置查找
├── test_triton_autotune_mode.py        # fast/max 模式一致性
├── test_logger.py           # 日志
└── swizzle_layout.py        # swizzle 布局工具

tools/
├── build_fast.sh            # ★ ccache shim + MAX_JOBS 加速构建
├── build_releases.sh        # 发布构建
├── bank_conflicts_check.sh  # SMEM bank 冲突检查
└── nvcc                     # ccache 用的 nvcc shim 脚本

docs/
├── index.md                 # 首页（Split-D 设计、API 示例）
├── env.md                   # ★ 所有环境变量（构建期 + 运行期）
├── api/                     # API 参考
├── user_guide/              # 用户指南（autotune 等）
├── developer_guide/         # 开发者指南（pre-commit 等）
├── bench/                   # 基准文档
└── assets/                  # 图片、论文 PDF
```

> **关键区分**：`bench/` 目录里的脚本是「仓库自带、用于产出 README 性能图表」的原始基准；而日常用 `python -m ffpa_attn.bench` 调用的是 `src/ffpa_attn/cli/_bench.py` 那套封装好的 CLI。两者不要混淆。

`docs/env.md` 是配置的**权威文档**，把所有变量分成「构建期」与「运行期」两类：

[docs/env.md:L1-L17](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/docs/env.md#L1-L17)

> 这段中文说明：文档开头就点明了划分——构建期变量（`FFPA_BUILD_ARCH` / `ENABLE_FFPA_CUDA_IMPL` / `ENABLE_FFPA_ALL_HEADDIM` 等）在 `pip install` 时读一次，决定生成哪些 TU、怎么调 nvcc；运行期变量（`ENABLE_FFPA_*` 系列）在调用时读取，决定 dispatch 到哪个 kernel。当你不确定某个 `FFPA_*` 变量是「改了要重编」还是「改了立刻生效」，第一反应应该是查这张表。

#### 4.4.4 代码实践

1. **实践目标**：熟悉 `tests/` 与 `docs/env.md` 的定位，学会「遇到配置问题先查 docs/env.md」。
2. **操作步骤**：
   - 列出 `tests/` 目录，按文件名猜每个测试覆盖什么（前向/反向/编译/...）。
   - 打开 `docs/env.md`，把所有变量抄成两张清单：一张「构建期（要重编）」、一张「运行期（即时生效）」。
   - 在 `tools/` 里找到 `build_fast.sh`，确认它确实是 u1-l2 提到的那个「ccache + MAX_JOBS」加速脚本。
3. **需要观察的现象**：构建期变量只有少数几个（arch、cuda_impl、headdim、stages、jobs 等），运行期变量则有十几个 `ENABLE_FFPA_*` 开关。
4. **预期结果**：你建立了一个习惯——「配置类疑问 → 查 `docs/env.md`；正确性疑问 → 看 `tests/`；性能疑问 → 跑 `bench/` 或 `python -m ffpa_attn.bench`」。
5. 此实践为源码阅读型，无需运行。

#### 4.4.5 小练习与答案

- **练习 1**：`python -m ffpa_attn.bench` 和 `bench/` 目录里的脚本有什么区别？
  - **答案**：前者是打进包里的、封装好的基准 CLI，实现在 `src/ffpa_attn/cli/_bench.py`，用户安装后随时可用；后者是仓库内的原始脚本（含 reference 参考实现），主要用于开发者产出 README 的性能图表，不进 wheel。
- **练习 2**：`ENABLE_FFPA_ALL_HEADDIM` 属于构建期还是运行期变量？改了它需要做什么？
  - **答案**：构建期（见 `docs/env.md` 第 16 行）。它决定生成哪些 head_dim 的 TU，改了需要重新构建，且应配合 `FFPA_CLEAN=1 bash tools/build_fast.sh` 清掉旧的生成文件（见 `docs/env.md` 第 66 行 Key notes 第 2 条）。

---

## 5. 综合实践

**任务：为 FFPA 建立一份属于你自己的「功能 → 源码位置」速查表。**

请把下面这张表填满（答案可参考本讲，但建议你亲自用 `Glob`/`Read` 在仓库里定位后再填），并把它存成你的个人笔记：

| 我想做的事 | 应该去哪个目录 / 文件 |
|---|---|
| 看 `ffpa_attn_func` 的输入校验与 docstring | |
| 看分发层如何按 Backend 选后端 | |
| 看 SDPA 回退（小 D）的实现 | |
| 看 Triton 前向 kernel（online softmax / Split-D） | |
| 看 CuTeDSL 后端入口与 SM80/SM90 分发 | |
| 看手写 CUDA 前向主循环模板 | |
| 找「fp16、fp32 累加、head_dim=512」对应的生成 TU | |
| 找 pybind 把 CUDA 暴露成 `_C` 的入口 | |
| 跑一次内置基准 CLI | |
| 查某个 `FFPA_*` 环境变量是构建期还是运行期 | |
| 多 GPU 并行自动调优的实现 | |

**进阶**：填完后，挑一个你最感兴趣的位置（例如 `triton/_ffpa_fwd.py`），只读它的「文件头注释 + 顶层函数签名」（不要陷入实现细节），猜一下它对应本讲路线图里的哪一篇后续讲义（提示：第 4 单元）。

**预期产物**：一张填好的速查表 + 一段「我猜 `triton/_ffpa_fwd.py` 对应 u4-l1」的简短说明。这张表会贯穿你后续阅读所有讲义，是本讲最重要的带走物。

## 6. 本讲小结

- FFPA 仓库是 **Python 轨（`src/ffpa_attn`）+ C++/CUDA 轨（`csrc/cuffpa`）** 的双轨结构，由 `pyproject.toml` 的包发现规则严格隔离——`csrc*` 永远不进 wheel，Triton-only 产物里没有任何 C++ 源码。
- `src/ffpa_attn` 遵循「公共入口（`ffpa_attn_interface.py`）+ 分发层（`functional.py`）+ 六个后端/工具子包（`aten / cuda / triton / cute / ray / cli`）」的清晰分层。
- `aten/` 就是「D≤256 回退 SDPA」的落点；`cute/` 目录名与对外后端名 `"cutedsl"` 不一致，是一个易踩的坑。
- `csrc/cuffpa` 分手写模板（`.cuh`）与按 head_dim 自动生成的翻译单元（`generated/`）两部分；生成是为了让多个 nvcc 进程并行编译、缩短构建时间。
- 生成文件的命名规则：**文件名** `ffpa_attn_fwd_{存储dtype}_hdim{D}.cu` 只含「存储 dtype + head_dim」；**函数名** `ffpa_attn_fwd_{存储dtype}{累加dtype}_d{D}` 多编码一层累加器精度。由此 fp16 文件装 2 个函数、bf16 文件装 1 个，bf16 无 fp16 累加变体。
- `bench / tests / tools / docs` 是辅助目录：配置疑问查 `docs/env.md`，正确性疑问看 `tests/`，性能疑问跑 `python -m ffpa_attn.bench`。

## 7. 下一步学习建议

本讲结束后，你已经拿到了完整的代码地图。下一步建议：

1. **进入第 2 单元（公共 API）**：从 [u2-l1](./u2-l1-ffpa-attn-func-signature-layout.md) 开始，逐参数学 `ffpa_attn_func` 的签名、张量布局与返回值。这是把地图「用起来」的第一步。
2. **如果你想直接理解全局架构**：可以跳到第 3 单元 [u3-l1（四后端总览）](./u3-l1-four-backends-overview.md) 与 [u3-l2（Backend 配置类）](./u3-l2-backend-config-dataclasses.md)，那里会串起本讲提到的「入口 → 分发 → 后端」整条链路。
3. **如果你对手写 CUDA 感兴趣**：第 7 单元（[u7-l1](./u7-l1-cuda-fwd-kernel-architecture.md) 起）会带你逐行读 `csrc/cuffpa` 的 `.cuh` 模板与生成机制，但建议先过完第 2～3 单元，否则会缺少「上层怎么调用」的上下文。
4. **持续携带本讲的速查表**：后续每读一篇讲义，都把它对应到速查表的一行，逐步把「源码位置」从抽象的目录名变成你脑海里的肌肉记忆。
