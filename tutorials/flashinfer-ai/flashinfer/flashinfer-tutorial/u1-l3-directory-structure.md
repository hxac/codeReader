# 仓库目录结构与代码分层

## 1. 本讲目标

上一讲我们把 FlashInfer 装好了、`show-config` 也跑通了。本讲不写任何新代码，目标是让你**在仓库里不再迷路**。读完本讲，你应当能够：

1. 说出 `include/`、`csrc/`、`flashinfer/`、`tests/`、`benchmarks/`、`3rdparty/` 这几个顶层目录各自的职责。
2. 理解 FlashInfer 最重要的一条架构红线：**框架无关的 kernel（`include/`）与框架绑定层（`csrc/`）必须物理分离**。
3. 认识 `3rdparty/` 下的 `cutlass`、`spdlog`、`cccl`、`nixl` 四个子模块分别提供什么能力。
4. 用「decode 注意力」这一个真实算子，串起从 Python API 到底层 CUDA kernel 的**四层调用栈**，并画出层次图。

本讲是后续所有进阶讲义（JIT 系统、attention/GEMM/MoE 算子）的「地图课」——先有地图，后面才不会迷路。

## 2. 前置知识

本讲只需要你已经完成 [u1-l2](./u1-l2-installation-and-first-run.md) 的安装，并对以下两个概念有大致印象即可：

- **header-only（仅头文件）库**：指那些不需要预编译成 `.so`、直接 `#include` 头文件就能用的 C++ 库。CUDA 模板代码尤其喜欢这种写法，因为模板必须在使用处才能实例化。FlashInfer 的 `include/` 就是 header-only 的。
- **ABI / FFI（外部函数接口）**：不同语言之间互相调用函数的约定。FlashInfer 的 kernel 是 C++/CUDA 写的，但用户从 Python 调用，中间需要一个「翻译层」。FlashInfer 用的翻译层叫 **TVM-FFI**（详见 [u9-l2](./u9-l2-tvm-ffi-bindings.md)），它能让同一份 kernel 被多种框架复用。

如果你对「JIT 编译」这个概念还只停留在上一讲的名词层面也没关系，本讲只看**代码是怎么摆放的**，不看编译过程（那是 [第 2 单元](./u2-l1-jit-overview.md) 的主题）。

## 3. 本讲源码地图

本讲主要看「项目级」的文件，用来建立全局观感：

| 文件 / 目录 | 作用 |
|-------------|------|
| `CLAUDE.md` | 仓库自带的工程指南，里面有最权威的目录结构与架构红线说明 |
| `README.md` | 项目对外说明，列出核心能力与 GPU 支持矩阵 |
| `pyproject.toml` | Python 包配置，定义了 `flashinfer.data.*` 如何把 `3rdparty` 与源码打包进去 |
| `.gitmodules` | 声明 `3rdparty/` 下的四个 git 子模块 |
| `include/` | 框架无关的 header-only CUDA kernel 模板 |
| `csrc/` | 框架绑定层（launcher + TVM-FFI 导出 + Jinja 模板） |
| `flashinfer/` | Python 包（用户 API + JIT 代码生成器） |

注意：本讲引用的源码路径都来自**实际仓库**，行号基于当前 HEAD（`a25af45`）。

## 4. 核心概念与源码讲解

### 4.1 顶层目录布局

#### 4.1.1 概念说明

FlashInfer 是一个体量很大的项目，但顶层目录的分工非常清晰。我们可以把它想成一个「三层蛋糕 + 配套设施」：

- **蛋糕底层**：`include/` —— 纯 CUDA kernel，只认原始指针，不知道 PyTorch 存在。
- **蛋糕中层**：`csrc/` —— 把 PyTorch 张量拆成原始指针，再调用底层 kernel；同时把函数通过 TVM-FFI 导出给 Python。
- **蛋糕顶层**：`flashinfer/`（Python 包）—— 用户直接 `import flashinfer` 看到的 API，内部负责 JIT 编译和加载。
- **配套设施**：`3rdparty/`（第三方依赖）、`tests/`（测试）、`benchmarks/`（性能基准）、`examples/`（示例）、`docs/`（文档）。

#### 4.1.2 核心流程

当你写 `flashinfer.single_decode_with_kv_cache(q, k, v)` 时，调用是这样穿越三层的：

```
你的 Python 代码
   │  import flashinfer
   ▼
flashinfer/decode.py            ← 顶层：Python API + @functools.cache 加载 JIT 模块
   │  通过 TVM-FFI 调用 C++ 符号 run()
   ▼
csrc/batch_decode_jit_binding.cu ← 中层：TVM-FFI 导出 plan/workspace_size/run
csrc/batch_decode.cu             ← 中层：launcher，把张量拆成指针
   │  调用模板 kernel
   ▼
include/flashinfer/attention/decode.cuh ← 底层：header-only CUDA kernel
```

中间还藏着一个「代码生成器」`flashinfer/jit/`：它在你第一次调用时，把 `csrc/` 的 `.cu` 文件和 Jinja 渲染出的配置拷贝到一个生成目录，再用 ninja 编译成 `.so`（这一步详见 [第 2 单元](./u2-l1-jit-overview.md)，本讲只需知道它的存在）。

#### 4.1.3 源码精读

仓库根目录的结构可以一目了然地列出来。`include/flashinfer/` 下既有按功能聚合的子目录，也有顶层公共头文件：

| 目录 / 文件 | 含义 |
|-------------|------|
| `include/flashinfer/attention/` | 注意力 kernel（decode/prefill/mla/cascade/pod…） |
| `include/flashinfer/gemm/` | GEMM kernel（BF16/FP8/FP4） |
| `include/flashinfer/comm/` | 多 GPU 通信 kernel |
| `include/flashinfer/mma.cuh` | 矩阵乘（tensor core）公共工具 |
| `include/flashinfer/utils.cuh` | 通用工具（错误检查、向量类型等） |

以 decode 注意力的底层 kernel 为例，`compute_qk`（计算 Q·K 点积）是一个典型的 `__device__` 模板函数，定义在 [include/flashinfer/attention/decode.cuh:L34-L62](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/attention/decode.cuh#L34-L62)——注意第 34 行 `namespace flashinfer {` 和第 62 行的 `template <...>` 模板签名，这就是 header-only kernel 的典型长相：**不依赖任何框架头文件**。

中间层 launcher 把张量解包后调用这些模板。`BatchDecodeWithPagedKVCacheRun` 是 decode 的运行入口，见 [csrc/batch_decode.cu:L129-L130](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/csrc/batch_decode.cu#L129-L130)，它的参数是一堆 `TensorView`（TVM-FFI 的张量视图），而不是 PyTorch 的 `torch::Tensor`——这是「框架中立」的关键。

> 名词解释：`TensorView` 是 TVM-FFI 提供的轻量张量封装，只持有「数据指针 + 形状 + dtype」，不绑定到 PyTorch。这样同一份 launcher 既能被 PyTorch 调用，将来也能被别的框架调用。

#### 4.1.4 代码实践

**实践目标**：用 `ls` / `tree` 在本地仓库里走一遍目录，建立空间感。

**操作步骤**：

1. 在仓库根目录执行 `ls -d */`，对照本节列表数出顶层目录。
2. 进入 `include/flashinfer/`，列出它的子目录，确认 `attention/`、`gemm/`、`comm/`、`mamba/`、`norm/` 都在。
3. 进入 `csrc/`，观察这里有 `.cu`（launcher）、`*_jit_binding.cu`（FFI 导出）、`*.jinja`（模板）、`*_config.jinja`（类型配置）四类文件混在一起——这正是「中层」的混合特征。
4. 进入 `flashinfer/`，注意它和 `csrc/` 在文件名上几乎一一对应（`decode.py` ↔ `batch_decode.cu`），这是有意的命名约定。

**需要观察的现象**：

- `include/` 里**没有**任何 `torch` 相关头文件（你可以用 `grep -rl "torch" include/` 验证，应当为空）。
- `csrc/` 里既有 C++ 也有 Jinja，是一个「生成 + 绑定」混合层。
- `flashinfer/`（Python 包）里有一个 `jit/` 子目录，它和根目录的 `csrc/` 在职责上是一对（一个生成、一个被生成）。

**预期结果**：你能凭目录名猜出每个文件属于哪一层。**待本地验证**：如果你用的是非 editable 安装（`pip install flashinfer-python`），仓库根目录的源码可能和实际运行的不是同一份——本实践请用 [u1-l2](./u1-l2-installation-and-first-run.md) 的 editable 安装来保证「改了就生效」。

#### 4.1.5 小练习与答案

**练习 1**：`include/flashinfer/attention/decode.cuh` 为什么必须是 header-only（即把实现写在 `.cuh` 里而不是编译成 `.so`）？

> **参考答案**：因为它是 C++ **模板**——`compute_qk` 的完整签名带了 `PosEncodingMode`、`vec_size`、`bdx`、`tile_size`、`typename T` 等一堆模板参数。模板在使用处（launcher 里传入了具体的 `head_dim`、`dtype`）才能实例化，所以实现必须可见，只能放进头文件。

**练习 2**：在 `csrc/` 目录下，和 `batch_decode` 相关的文件至少有 4 个，分别是什么角色？

> **参考答案**：① `batch_decode.cu` = launcher（解包张量、调用 kernel）；② `batch_decode_jit_binding.cu` = TVM-FFI 导出 `plan/workspace_size/run` 三个符号；③ `batch_decode_customize_config.jinja` = 类型/变体的 Jinja 配置模板；④ `batch_decode_kernel_inst.jinja` = kernel 实例化模板。

---

### 4.2 框架分离原则（最重要的一条红线）

#### 4.2.1 概念说明

这是整个仓库最重要、也最容易被忽视的设计原则，`CLAUDE.md` 里用大写强调过：

> **Torch headers MUST NOT be included in `include/` directory files.**
> （`include/` 里的文件**绝不能** `#include` 任何 Torch 头文件。）

为什么要这么严格？因为 FlashInfer 不想被绑死在 PyTorch 上。今天用户用 PyTorch，明天可能有人想用 JAX、用裸 C++、用 Rust。如果底层 kernel 直接 `#include <torch/...>`，那它就只能被 PyTorch 调用了。

于是 FlashInfer 划了一条物理边界：

| 层 | 目录 | 能否 `#include` torch | 输入类型 |
|----|------|----------------------|----------|
| 框架无关 kernel | `include/` | ❌ 禁止 | 原始指针 `T*` |
| 框架绑定层 | `csrc/` | ✅ 可以（目前） | `TensorView` / `torch::Tensor` |

`include/` 是「纯净」的，`csrc/` 是「脏」的（沾染了框架）。所有框架相关的脏活累活都集中在 `csrc/`。

#### 4.2.2 核心流程

一次调用的数据形态变化：

```
Python:  torch.Tensor q          ← 用户手里是 PyTorch 张量
   │ TVM-FFI 自动转换
   ▼
csrc:    TensorView q            ← 中层只拿到「指针 + 形状 + dtype」
   │  q.data_ptr<T>() 或解包
   ▼
include: T* q_ptr                ← 底层 kernel 只认裸指针
```

这样的好处是：**底层 kernel 永远不知道上层用了什么框架**。今天 `csrc/` 用 TVM-FFI + PyTorch，将来要换框架，只需重写 `csrc/` 这一层，`include/` 的全部 kernel 一行都不用改。

#### 4.2.3 源码精读

注意力的 TVM-FFI 绑定在 [csrc/batch_decode_jit_binding.cu:L23-L50](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/csrc/batch_decode_jit_binding.cu#L23-L50) 中。这段代码用三个宏把 C++ 函数导出成 Python 可调用的符号：

```cpp
// 第 23、30、36 行分别定义了三个 C++ 函数
Array<int64_t> BatchDecodeWithPagedKVCachePlan(...);
Array<int64_t> BatchDecodeWithPagedKVCacheWorkspaceSize(...);
void BatchDecodeWithPagedKVCacheRun(TensorView float_workspace_buffer, ...);

// 第 46-50 行用宏把它们导出为固定名字 plan / workspace_size / run
TVM_FFI_DLL_EXPORT_TYPED_FUNC(plan, BatchDecodeWithPagedKVCachePlan);
TVM_FFI_DLL_EXPORT_TYPED_FUNC(workspace_size, BatchDecodeWithPagedKVCacheWorkspaceSize);
TVM_FFI_DLL_EXPORT_TYPED_FUNC(run, BatchDecodeWithPagedKVCacheRun);
```

注意第 36 行 `BatchDecodeWithPagedKVCacheRun` 的参数是 `TensorView`（见 [csrc/batch_decode.cu:L129-L130](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/csrc/batch_decode.cu#L129-L130)），而不是 `torch::Tensor`。这就是「脏活」：launcher 在这里把 `TensorView` 拆成裸指针，再喂给 `include/` 里那些只认 `T*` 的 kernel。

> 名词解释：`TVM_FFI_DLL_EXPORT_TYPED_FUNC(符号名, C++函数)` 是 TVM-FFI 的导出宏，它生成一段胶水代码，让 Python 能按「符号名」调用到这个 C++ 函数，并自动做类型转换。plan/run/workspace_size 这套「三件套」命名是 FlashInfer 注意力 wrapper 的统一约定（详见 [u3-l3](./u3-l3-batch-decode-wrapper.md)）。

与之对照，顶层 Python wrapper [`flashinfer/decode.py`](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/decode.py) 里的 `single_decode_with_kv_cache`（[第 411-417 行](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/decode.py#L411-L417)）是用户直接调用的 API，它接收的就是地道的 `torch.Tensor`。

#### 4.2.4 代码实践

**实践目标**：亲手验证「`include/` 里没有 torch」这条红线确实成立。

**操作步骤**：

1. 在仓库根目录运行：
   ```bash
   grep -rl "torch" include/flashinfer/ | head
   ```
2. 再运行对照组：
   ```bash
   grep -rl "torch" csrc/ | head
   ```
3. 打开 [csrc/batch_decode_jit_binding.cu](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/csrc/batch_decode_jit_binding.cu)，定位三个 `TVM_FFI_DLL_EXPORT_TYPED_FUNC` 宏，确认导出的符号名是 `plan`/`workspace_size`/`run`。

**需要观察的现象**：

- 第一条命令应当**几乎为空**（理想情况完全为空；个别 header 若有引用通常是注释或测试桩，需人工确认）。
- 第二条命令会列出大量 `csrc/*.cu` 文件——说明 torch 只在绑定层出现。

**预期结果**：你会直观看到「干净的 kernel / 脏的绑定层」二分。**待本地验证**：少数 `include/` 文件可能包含 `torch` 字样（如示例注释），请结合上下文判断是否真的 `#include`。

#### 4.2.5 小练习与答案

**练习 1**：如果有一天 FlashInfer 要支持 JAX，需要改哪些层？

> **参考答案**：`include/` 一行都不用改（它本来就框架无关）。只需新增一个 `csrc/` 的 JAX 绑定层（把 JAX 张量转成 `TensorView`），再在 `flashinfer/` Python 包里加 JAX 入口即可。这正是物理分离的红利。

**练习 2**：为什么 launcher（`batch_decode.cu`）的参数用 `TensorView` 而不是直接用 `torch::Tensor`？

> **参考答案**：为了保持中层也尽量框架中立。`TensorView` 是 TVM-FFI 的通用张量视图，任何能产生「指针+形状+dtype」的框架都能转成它。这样即便上层框架变了，launcher 代码也基本可复用。

---

### 4.3 第三方依赖（3rdparty 与 data 目录）

#### 4.3.1 概念说明

FlashInfer 不从零造所有轮子，它站在 NVIDIA 几个重量级库的肩膀上。这些库以 **git 子模块**的形式放在 `3rdparty/` 里，由 `.gitmodules` 声明。`u1-l2` 里强调的 `--recursive` 就是为了把它们拉下来——缺了它们，kernel 编译会找不到头文件。

当前 `3rdparty/` 下有四个子模块：

| 子模块 | 作用 |
|--------|------|
| `cutlass` | NVIDIA 的 CUDA 线性代数模板库，GEMM/tensor-core 的核心依赖 |
| `spdlog` | 一个快速的 C++ 日志库，FlashInfer 的运行时日志用它 |
| `cccl` | CUDA Core Compute Libraries（CUB / libcudacxx / Thrust），提供设备端原语 |
| `nixl` | NVIDIA Inference Transfer Lib，用于多节点通信（MNNVL/Expert Parallelism 后端） |

#### 4.3.2 核心流程

子模块代码不会被打进 Python wheel 的「源码包」里随便放——它有专门的映射规则。`pyproject.toml` 用 `package-dir` 把 `3rdparty/*` 映射成 `flashinfer.data.*` 命名空间：

```
3rdparty/cutlass  →  flashinfer.data.cutlass
3rdparty/spdlog   →  flashinfer.data.spdlog
3rdparty/cccl     →  flashinfer.data.cccl
仓库根目录 .        →  flashinfer.data           （从而带上 csrc/、include/）
```

这样无论用户是 editable 安装（软链接）还是 wheel 安装（拷贝），JIT 编译时都能在 `flashinfer.data` 下找到 `include/`、`csrc/` 和 cutlass 头文件。详见 [u1-l2](./u1-l2-installation-and-first-run.md) 关于构建后端的讨论。

#### 4.3.3 源码精读

四个子模块的来源在 [.gitmodules](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/.gitmodules) 中声明（四个 `[submodule "..."]` 段）。而它们如何被打包，定义在 [pyproject.toml](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/pyproject.toml) 的两段配置里：

```toml
# package-dir：把物理目录映射成 Python 包命名空间
[tool.setuptools.package-dir]
"flashinfer.data" = "."
"flashinfer.data.cutlass" = "3rdparty/cutlass"
"flashinfer.data.spdlog" = "3rdparty/spdlog"
"flashinfer.data.cccl" = "3rdparty/cccl"

# package-data：声明每个命名空间要打包哪些文件
[tool.setuptools.package-data]
"flashinfer.data" = ["csrc/**", "include/**"]
"flashinfer.data.cutlass" = ["include/**", "tools/util/include/**"]
"flashinfer.data.spdlog" = ["include/**"]
"flashinfer.data.cccl" = ["cub/cub/**", "libcudacxx/include/**", "thrust/thrust/**"]
```

这段配置说明：`flashinfer.data` 这个「包」其实指向仓库根目录（`.`），但只打包 `csrc/**` 和 `include/**`——也就是说，你 `pip install` 之后，在 site-packages 里会看到 `flashinfer/data/csrc/` 和 `flashinfer/data/include/`，它们就是 kernel 源码在用户机器上的落脚点。

> 名词解释：**子模块（submodule）** 是 git 的机制，让一个仓库能在某个子目录里嵌入另一个仓库的特定 commit。`--recursive` 克隆时会递归把这些嵌入仓库也拉下来；不带的话，`3rdparty/cutlass` 等目录会是空的，编译就会失败。

#### 4.3.4 代码实践

**实践目标**：搞清楚「我安装的 flashinfer，到底从哪里读 kernel 源码」。

**操作步骤**：

1. 确认子模块已拉取：
   ```bash
   ls 3rdparty/cutlass/include 2>/dev/null | head -3   # 应当非空
   ```
   若为空，执行 `git submodule update --init --recursive`。
2. 在 Python 里定位 `flashinfer.data` 实际指向的路径：
   ```bash
   python -c "import flashinfer, os; print(os.path.dirname(flashinfer.data.__file__))"
   ```
3. 对比该路径下的 `csrc/`、`include/` 是否就是你仓库里的源码（editable 安装下应是同一个目录）。

**需要观察的现象**：

- `3rdparty/cutlass/include/cutlass/` 下有大量 `.h` 文件，这些是 GEMM kernel 在编译时 `#include` 的来源。
- `flashinfer.data` 指向的目录与仓库源码对应。

**预期结果**：你能在 `flashinfer.data` 下同时看到自己的 `csrc/`、`include/` 以及 `cutlass/`、`spdlog/`、`cccl/`，理解 JIT 编译时头文件搜索路径是怎么拼出来的。**待本地验证**：editable 与 wheel 安装下 `data` 目录的形式（软链接 vs 拷贝）不同，但内容等价。

#### 4.3.5 小练习与答案

**练习 1**：`cccl` 子模块打包时只取了 `cub/cub/**`、`libcudacxx/include/**`、`thrust/thrust/**` 三部分，为什么不全打包？

> **参考答案**：因为 cccl 仓库里只有这三块（CUB 设备端原语、libcudacxx 头文件、Thrust）是 kernel 编译真正需要的，其余内容（测试、文档、示例）与运行无关。只打包必要的部分能显著减小 wheel 体积。

**练习 2**：如果用户没有用 `--recursive` 克隆，会在什么时候报错？

> **参考答案**：在**首次 JIT 编译**时。JIT 会用 nvcc 编译 `csrc/*.cu`，而这些文件 `#include <cutlass/...>`；子模块缺失时头文件找不到，编译失败。（注：FlashInfer 也会尝试用 cubin 包兜底，但开发场景下必然要子模块。）

---

## 5. 综合实践：画出 decode 注意力的四层调用图

这是本讲的核心实践，目的是把前面的「目录布局」和「框架分离」串成一张可操作的图。我们要追踪 `flashinfer.single_decode_with_kv_cache(q, k, v)` 这一次调用，找到它在四层的对应文件，并画出层次图。

**实践目标**：定位 decode 注意力的四个层次对应文件，理解每一层的输入/输出类型，画一张从 Python 到 CUDA kernel 的调用层次图。

**操作步骤**：

1. **顶层 Python wrapper**。打开 [flashinfer/decode.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/decode.py)，定位：
   - [第 411-417 行 `single_decode_with_kv_cache`](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/decode.py#L411-L417)：面向用户的最简 API。
   - [第 91 行 `@functools.cache`](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/decode.py#L91)：装饰在模块加载函数上，做 Python 级缓存（[u2-l5](./u2-l5-caching-and-invalidation.md) 会详讲）。

2. **代码生成器（连接顶层与中层）**。打开 [flashinfer/jit/attention/modules.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/attention/modules.py)，定位：
   - [第 915 行 `gen_batch_decode_module`](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/attention/modules.py#L915)：decode 的 JIT 生成入口。
   - [第 1515 行 `gen_customize_batch_decode_module`](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/attention/modules.py#L1515)：真正干活的函数，它在第 1560、1563 行读取两个 Jinja 模板（`batch_decode_customize_config.jinja`、`batch_decode_kernel_inst.jinja`），第 1572 行渲染出 `batch_decode_kernel.cu`，第 1580 行把 `batch_decode.cu` 拷进生成目录。

3. **中层 launcher + TVM-FFI 绑定**。
   - [csrc/batch_decode.cu 第 129 行 `BatchDecodeWithPagedKVCacheRun`](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/csrc/batch_decode.cu#L129)：launcher，参数是 `TensorView`。
   - [csrc/batch_decode_jit_binding.cu 第 23-50 行](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/csrc/batch_decode_jit_binding.cu#L23-L50)：三个 `TVM_FFI_DLL_EXPORT_TYPED_FUNC` 宏导出 `plan`/`workspace_size`/`run`。

4. **底层 header-only kernel**。
   - [include/flashinfer/attention/decode.cuh 第 34-62 行](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/attention/decode.cuh#L34-L62)：`namespace flashinfer` 内的 `__device__` 模板 kernel（如 `compute_qk`），只认裸指针。

5. **画图**。把以上四处填进下面的模板，标出每一层的数据类型：

```
┌──────────────────────────────────────────────────────────────┐
│ ① Python 顶层  flashinfer/decode.py                          │
│    single_decode_with_kv_cache(torch.Tensor q,k,v)           │
│    ↓  @functools.cache 加载 JIT 模块                          │
└──────────────────────────────────────────────────────────────┘
                            │
              (JIT 生成器在首次调用时介入)
    flashinfer/jit/attention/modules.py: gen_batch_decode_module
    渲染 Jinja + 拷贝 csrc/*.cu → 生成目录 → ninja 编译成 .so
                            │
┌──────────────────────────────────────────────────────────────┐
│ ② TVM-FFI 路由  csrc/batch_decode_jit_binding.cu              │
│    导出符号 plan / workspace_size / run                       │
└──────────────────────────────────────────────────────────────┘
                            │
┌──────────────────────────────────────────────────────────────┐
│ ③ Launcher  csrc/batch_decode.cu                              │
│    BatchDecodeWithPagedKVCacheRun(TensorView ...)            │
│    把 TensorView 解包成裸指针                                  │
└──────────────────────────────────────────────────────────────┘
                            │
┌──────────────────────────────────────────────────────────────┐
│ ④ Kernel  include/flashinfer/attention/decode.cuh             │
│    template<...> __device__ compute_qk(T* ...)               │
│    框架无关，只认原始指针                                       │
└──────────────────────────────────────────────────────────────┘
```

**需要观察的现象**：

- 每一层的数据类型在变：`torch.Tensor` → `TensorView` → `T*` 裸指针。这正是「框架分离」的物证。
- 「JIT 生成器」并不在运行时调用链里常驻，它只在**首次调用**或**源码变更**后介入一次，之后走磁盘缓存（详见 [u2-l5](./u2-l5-caching-and-invalidation.md)）。

**预期结果**：你能指着这张图，对每个箭头解释「数据形态发生了什么变化、为什么这么变」。这就是本讲的毕业证——你已经在仓库里不再迷路了。

## 6. 本讲小结

- 仓库是「三层蛋糕」：`include/`（header-only kernel，框架无关）→ `csrc/`（launcher + TVM-FFI 绑定）→ `flashinfer/`（Python API + JIT 生成器）。
- **最重要的一条红线**：`include/` 绝不能 `#include` torch；所有框架相关的脏活在 `csrc/`。这让 kernel 可被多框架复用。
- `csrc/` 是「生成 + 绑定」混合层，文件分四类：`*.cu`（launcher）、`*_jit_binding.cu`（FFI 导出）、`*_customize_config.jinja`（类型配置）、`*_kernel_inst.jinja`（实例化）。
- `3rdparty/` 有四个 git 子模块：`cutlass`（GEMM）、`spdlog`（日志）、`cccl`（设备原语）、`nixl`（多节点通信），由 `.gitmodules` 声明，`pyproject.toml` 把它们映射成 `flashinfer.data.*`。
- decode 注意力是贯穿四层的范例：`decode.py` → `jit/attention/modules.py` → `csrc/batch_decode*.cu` → `include/flashinfer/attention/decode.cuh`，数据形态沿 `torch.Tensor → TensorView → T*` 演进。
- `pip install` 后，kernel 源码以 `flashinfer/data/{csrc,include}` 的形式落在用户机器上，这是 JIT 编译的工作原料。

## 7. 下一步学习建议

本讲建立了「地图」，接下来该理解地图上最关键的那条公路——JIT 编译系统。建议进入 **[第 2 单元 u2-l1：JIT 编译概览](./u2-l1-jit-overview.md)**，那里会讲清楚本讲反复提到的「JIT 生成器」究竟是怎么把 `csrc/*.cu` 变成一个可加载的 `.so` 的。

如果你想先看一个具体算子怎么用起来，也可以先读 **[u1-l5：第一个注意力算子实践](./u1-l5-first-attention-kernel.md)**，动手跑一次 `single_decode_with_kv_cache`，再回头看第 2 单元。

继续阅读建议的源码顺序：
1. `flashinfer/decode.py`（Python 顶层，本讲已扫过一遍）。
2. `flashinfer/jit/core.py`（JitSpec，第 2 单元核心）。
3. `csrc/batch_decode_jit_binding.cu`（再细看一遍三个导出宏，为 TVM-FFI 那讲做准备）。
