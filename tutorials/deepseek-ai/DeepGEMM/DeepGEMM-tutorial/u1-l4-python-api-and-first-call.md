# Python 接口全貌与第一次调用

## 1. 本讲目标

在前面三讲里，我们已经知道了 DeepGEMM 是什么（u1-l1）、怎么装（u1-l2）、目录与分层架构长什么样（u1-l3）。本讲是「入门单元」的收尾，目标是让你**真正动手写出第一次 DeepGEMM 调用**。

学完本讲，你应当能够：

- 看懂 `deep_gemm` 这个 Python 包导出的全部函数，并能按用途（GEMM / 分组 GEMM / MoE / MQA / 工具）分类；
- 说清 `import deep_gemm` 时 `_C.init(...)` 到底做了什么、为什么必须在 import 阶段就完成；
- 独立写出一个最小的 FP8 GEMM 调用：构造 FP8 张量与缩放因子、调用 `fp8_fp4_gemm_nt`、用 `calc_diff` 校验数值正确性。

本讲只停留在 **Python 层**，不深入 C++ 派发与 JIT 内部——那是后续 u2-l3、u3 系列的任务。我们把 Python 当成一个「黑盒 API」来用，先把调用跑通。

## 2. 前置知识

- **GEMM**：矩阵乘法 \(C = A \times B\)。DeepGEMM 的约定是 \(D = C + A \times B\)（即可以带一个加法项 `c`，详见第 4.3 节）。
- **NT 布局**：`A` 是行主序（row-major），`B` 是列主序（col-major），等价于数学上的 \(D = C + A \times B^{\top}\)。DeepGEMM 的命名里 `nt` 就表示「A 不转置、B 转置」。这部分在 u2-l1 会详细讲，本讲你只要记住：调用 `fp8_fp4_gemm_nt` 时，`A` 形状是 `[M, K]`、`B` 形状是 `[N, K]`（两者都在 K 维上对齐）。
- **FP8 与缩放因子（Scaling Factor, SF）**：FP8（`float8_e4m3fn`）能表示的数值范围很窄，所以不能直接把一个大矩阵塞进去。常见做法是**分块量化**：把矩阵按 K 方向切成若干 `gran_k`（如 128）大小的块，每块算出一个最大绝对值 `amax`，再除以 448（FP8 最大值）得到一个缩放因子 `sf`。这样 FP8 张量配上一个形状小得多的 `sf` 张量，就能无损还原原始数值。SF 的格式分两种：
  - **SM90（Hopper）**：SF 用 `FP32` 浮点存储；
  - **SM100（Blackwell）**：SF 用打包的 **UE8M0** 格式（4 个 UE8M0 打包进一个 `torch.int`）。

  本讲的「第一次调用」会借助工具函数自动处理这两种格式，你暂时不必纠结 UE8M0 的位编码细节。
- **`_C` 扩展**：u1-l2 讲过，`setup.py` 把 `csrc/python_api.cpp` 编译成一个名叫 `deep_gemm._C` 的 C++ 扩展模块。你在 Python 里调用的所有 kernel，最终都来自这个 `_C`。

> 提示：如果你手头没有 SM90/SM100 的 GPU，本讲的部分「运行型实践」无法实际执行。文中会明确标注「待本地验证」。但**源码阅读型实践**在任意机器上都能做，请务必完成。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲用它做什么 |
|------|------|----------------|
| `deep_gemm/__init__.py` | 包入口，定义 `import deep_gemm` 后能看到的所有名字 | 看清导出接口的全貌与分类、找到 `_C.init` 调用点 |
| `tests/test_fp8_fp4.py` | FP8/FP4 各 kernel 的端到端测试 | 作为「第一次调用」的真实范例，逐行拆解 `test_gemm()` |
| `tests/generators.py` | 测试输入与参考输出生成器 | 理解 `generate_normal` 如何造出 FP8 张量 + SF 元组 |
| `deep_gemm/utils/math.py` | 量化工具函数 | 用 `per_token_cast_to_fp8` 把 BF16 张量转成 FP8 + SF |
| `deep_gemm/testing/numeric.py` | 数值误差度量 | 用 `calc_diff` 判断 kernel 输出与参考值是否一致 |
| `deep_gemm/testing/bench.py` | 性能采样 | 用 `bench_kineto` 测一次 kernel 的耗时 |
| `csrc/apis/runtime.hpp` | `_C.init` 的 C++ 绑定 | 说明 import 时到底初始化了什么 |
| `csrc/apis/gemm.hpp` | GEMM 类 kernel 的 C++ 绑定 | 确认 `fp8_fp4_gemm_nt` 的 Python 签名（派发细节留给 u2-l3） |

## 4. 核心概念与源码讲解

### 4.1 Python 导出接口分类

#### 4.1.1 概念说明

DeepGEMM 把所有面向用户的能力都挂在顶层 `deep_gemm` 命名空间下，调用方式统一是 `deep_gemm.xxx(...)`。这些能力**并非散落无序**，而是按「算子家族 + 工具」分成了若干组。理解这个分类有两个好处：

1. **找函数快**：当你想做某件事（比如 MoE 前向），能立刻定位到正确的函数名；
2. **理解命名约定**：DeepGEMM 的函数名遵循 `<精度>_<算子>_<布局>` 的模式，看名字就能猜出用途。

例如 `fp8_fp4_gemm_nt` = 「A 用 FP8、B 用 FP4、普通 GEMM、NT 布局」；`m_grouped_fp8_gemm_nt_contiguous` = 「M 轴分组、FP8、GEMM、NT 布局、连续内存布局」。

#### 4.1.2 核心流程

`import deep_gemm` 时，`__init__.py` 按以下顺序把名字「搬」到顶层命名空间：

1. 读入 `envs.py` 里的默认环境变量（如 JIT 相关开关）；
2. `from . import _C` 加载 C++ 扩展；
3. 从 `_C` 里 **`from ... import`** 一批函数（配置旋钮、cuBLASLt kernel、DeepGEMM kernel）；
4. 从 `.mega` 子模块导入 Mega MoE 相关函数；
5. 导入 `testing` 与 `utils` 工具子包（`from .utils import *`）；
6. 尝试导入 A100 的 legacy Triton kernel；
7. 调用 `_C.init(...)` 完成 JIT 初始化。

其中第 3 步是「接口全貌」的核心，下面精读。

#### 4.1.3 源码精读

**（a）配置旋钮** —— 这些是全局运行时开关，几乎所有 kernel 都会读它们：

[deep_gemm/__init__.py:16-26](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/__init__.py#L16-L26) —— 从 `_C` 导入 `set_num_sms`/`get_num_sms`（限制用多少 SM）、`set_tc_util`/`get_tc_util`（张量核利用率）、`set_ignore_compile_dims`、`set_block_size_multiple_of`、`set_pdl`/`get_pdl`（PDL，程序化依赖启动）。这些旋钮的含义会在 u4-l1、u5-l3 详讲。

**（b）cuBLASLt 对照基准** —— 用来和 NVIDIA 官方库做性能对比：

[deep_gemm/__init__.py:29-32](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/__init__.py#L29-L32) —— `cublaslt_gemm_{nt,nn,tn,tt}` 四个布局变体，作为 DeepGEMM 性能的「参照系」（测试里常打印「xx 倍 cuBLAS」）。

**（c）DeepGEMM 自家 kernel（最大的一组）**：

[deep_gemm/__init__.py:36-73](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/__init__.py#L36-L73) —— 这一段 `from ._C import (...)` 包含了几乎所有核心算子，按注释可分为：

- **FP8/FP4 混合精度 GEMM**：`fp8_fp4_gemm_{nt,nn,tn,tt}`、分组版 `m_grouped_fp8_fp4_gemm_*`、masked 版；
- **纯 FP8 GEMM**：`fp8_gemm_{nt,nn,tn,tt}`、`fp8_gemm_nt_skip_head_mid`（带 head 切分，u10-l2 讲）、分组版；
- **BF16 GEMM**：`bf16_gemm_{nt,nn,tn,tt}` 及其分组版（无缩放因子）；
- **Einsum**：`einsum`、`fp8_einsum`（硬编码爱因斯坦求和，u9-l3）；
- **Attention / MQA**：`fp8_fp4_mqa_logits`、`get_paged_mqa_logits_metadata`、`fp8_fp4_paged_mqa_logits`（u9-l1、u9-l2）；
- **HyperConnection**：`tf32_hc_prenorm_gemm`（u9-l3）；
- **布局工具**：`transform_sf_into_required_layout`（把用户格式的 SF 变换成 kernel 需要的 TMA 对齐布局，u2-l2）。

注意这一整段被包在 `try ... except ImportError` 里，注释说明「CUDA 运行时版本低于 12.1 时期望的降级行为」——即老 CUDA 上这批重型 kernel 不可用，但不阻止 import 包本身。

[deep_gemm/__init__.py:77-78](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/__init__.py#L77-L78) —— 两条 legacy 别名，把旧名字 `fp8_m_grouped_gemm_nt_masked` 指向新名字，保持向后兼容。

**（d）Mega MoE** —— 单独放在 `.mega` 子包：

[deep_gemm/__init__.py:84-90](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/__init__.py#L84-L90) —— `SymmBuffer`（对称内存缓冲）、`get_symm_buffer_for_mega_moe`、`transform_weights_for_mega_moe`、`fp8_fp4_mega_moe`、`bf16_mega_moe`。这是 u8 系列的主题，本讲只需知道它们存在。

**（e）工具与 legacy**：

[deep_gemm/__init__.py:93-95](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/__init__.py#L93-L95) —— `from . import testing`、`from . import utils`，并且 `from .utils import *`，所以 `per_token_cast_to_fp8`、`align` 等工具函数既能写成 `deep_gemm.utils.per_token_cast_to_fp8`，也能直接写成 `deep_gemm.per_token_cast_to_fp8`。

[deep_gemm/__init__.py:98-101](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/__init__.py#L98-L101) —— 尝试导入 A100（SM80）用的 legacy Triton kernel，失败只打印警告，不致命。

#### 4.1.4 代码实践

> **实践类型：源码阅读 + 接口清点（任意机器可做）**

1. **目标**：在不运行任何 GPU 代码的前提下，把 DeepGEMM 的 Python 接口清点成一张分类表。
2. **步骤**：
   - 打开 [deep_gemm/__init__.py](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/__init__.py)；
   - 找到第 36–73 行的 `from ._C import (...)` 块；
   - 按「FP8/FP4 GEMM / 纯 FP8 GEMM / BF16 GEMM / Einsum / Attention / HyperConnection / 布局工具」七类，把每个函数名填进一张表。
3. **观察现象**：你会注意到同一类算子总是成组出现 `nt/nn/tn/tt` 四个布局变体（分组版则通常只有 `nt/nn`）。
4. **预期结果**：得到一张与本讲「§4.1.3 (c)」结构一致的分类表，并且能解释 `fp8_fp4_gemm_nt` 与 `m_grouped_fp8_gemm_nt_contiguous` 名字里每个片段的含义。
5. 结论无需本地验证（纯阅读）。

#### 4.1.5 小练习与答案

**练习 1**：`fp8_gemm_nt_skip_head_mid` 这个名字里的 `skip_head_mid` 表示什么意思？提示：它不是普通 GEMM。

> **答案**：它表示在输出 N 维上「跳过中间一段（mid）」，专门用于带 head 切分的场景。其原理在 u10-l2 通过 `EpilogueHeadSplits` 讲解——epilogue 会在 N 维上做索引重映射，把连续的 `n_idx` 映射到带 mid 段的真实 N 坐标。本讲只需记住它属于「纯 FP8 GEMM」家族的一个特化版本。

**练习 2**：为什么 `from .utils import *` 之后，`deep_gemm.per_token_cast_to_fp8` 和 `deep_gemm.utils.per_token_cast_to_fp8` 都能用？

> **答案**：`from . import utils` 让 `utils` 成为 `deep_gemm` 的一个属性（子模块），所以 `deep_gemm.utils.per_token_cast_to_fp8` 可用；而 `from .utils import *` 又把 `utils` 模块里的公开名字（包括 `per_token_cast_to_fp8`）直接搬进了 `deep_gemm` 自己的命名空间，所以 `deep_gemm.per_token_cast_to_fp8` 也能用。两者指向同一个函数对象。

---

### 4.2 `_C` 初始化：`import` 时到底发生了什么

#### 4.2.1 概念说明

回顾 u1-l3 的端到端调用链：Python → pybind11 → `apis/*.hpp` → JIT 编译 → 设备 kernel。这条链里，**JIT 编译**需要知道两件关键信息：

1. **库根目录（library root）**：DeepGEMM 的设备侧 CUDA 头文件（`deep_gemm/include/deep_gemm/*.cuh`）放在哪里？JIT 生成的 `.cu` 源码要 `#include` 它们。
2. **CUDA home**：`nvcc`/`nvrtc` 编译器在哪个目录下？JIT 要调用它们把源码编成 `.cubin`。

这两条信息必须在**任何 kernel 被调用之前**就告诉 C++ 侧。DeepGEMM 的做法是：在 `import deep_gemm` 的**最后一步**自动调用 `_C.init(...)`，完成这项一次性初始化。这就是为什么你 `import deep_gemm` 之后就能直接用，不需要手动「初始化引擎」。

#### 4.2.2 核心流程

```
import deep_gemm
   │
   ├─ _find_cuda_home()   # 解析 CUDA 安装路径
   │      ├─ 优先：环境变量 CUDA_HOME / CUDA_PATH
   │      ├─ 其次：`which nvcc` 推断
   │      └─ 兜底：/usr/local/cuda
   │
   └─ _C.init(library_root, cuda_home)
          └─ （C++ 侧）Compiler / KernelRuntime / IncludeParser 的 prepare_init
```

注意 `_C.init` 是在**模块加载时**执行的（Python 里模块级语句在 import 时运行），所以 import 成功 == 初始化完成。

#### 4.2.3 源码精读

**（a）Python 侧：解析 CUDA home**

[deep_gemm/__init__.py:104-119](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/__init__.py#L104-L119) —— `_find_cuda_home()` 的查找顺序：先看 `CUDA_HOME`/`CUDA_PATH` 环境变量；没有就用 `which nvcc` 反推（取两层 `dirname`）；再不行兜底 `/usr/local/cuda`。注释特别说明：它**故意不用 PyTorch 自带的 `_find_cuda_home`**，因为某些 PyTorch 版本里那个函数会初始化 CUDA，而初始化 CUDA 与进程 fork 不兼容（测试里大量用到 fork，见 u8/u10 的多进程实践）。

**（b）Python 侧：调用 `_C.init`**

[deep_gemm/__init__.py:122-125](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/__init__.py#L122-L125) —— 第一个参数是 `deep_gemm` 包所在目录（即 `__file__` 的 dirname，设备头文件就在其下的 `include/`），第二个参数是上一步求得的 CUDA home。这一句是整个 import 流程的「点火」。

**（c）C++ 侧：`init` 的真实绑定**

[csrc/apis/runtime.hpp:42-48](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/runtime.hpp#L42-L48) —— `init` 被绑定为依次调用 `Compiler::prepare_init`、`KernelRuntime::prepare_init`、`IncludeParser::prepare_init`。这三个类分别负责：编译器（拿到 nvcc/nvrtc 路径、组装编译参数）、运行时加载器（拿到 CUDA home 用于后续 `cuLibraryLoad`）、头文件哈希解析器（拿到库根目录，递归解析 `#include <deep_gemm/*>`，详见 u3-l3）。整段被 `#if DG_TENSORMAP_COMPATIBLE` 守卫——这是一个编译期特性宏，只有启用了 TMA/JIT 能力的构建才会真正执行初始化；否则 `_C.init` 是个空函数（保证最小构建也能 import）。

**（d）版本号**

[deep_gemm/__init__.py:127](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/__init__.py#L127) —— `__version__ = '2.6.1'`，可用于 `deep_gemm.__version__` 查询，便于排查环境。

#### 4.2.4 代码实践

> **实践类型：观察型（需已 `import deep_gemm`，可在任意装好包的机器上做，不必有 GPU 计算）**

1. **目标**：确认 `_C.init` 确实在 import 时执行，并理解它解析出的两个路径。
2. **步骤**：
   ```python
   import deep_gemm
   print(deep_gemm.__version__)
   print(deep_gemm.__path__)
   ```
3. **观察现象**：
   - `__version__` 打印 `2.6.1`；
   - `__path__` 打印包目录，其下应当存在 `include/deep_gemm/` 子目录（设备头文件就在那里）。
4. **预期结果**：import 无异常即说明 `_C.init` 已成功执行（否则会在 import 阶段抛错）。
5. 如果你设置了 `DG_JIT_DEBUG=1` 再 import，控制台可能输出额外的编译器/缓存路径信息——这部分行为依赖具体构建，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `_C.init` 要传「库根目录」而不是让 C++ 自己找？

> **答案**：因为设备头文件是随 Python 包分发的（在 `deep_gemm/include/` 下），它的物理位置取决于用户把包装在哪里——可能是 site-packages，也可能是 `develop.sh` 的源码目录。只有 Python 侧用 `os.path.dirname(os.path.abspath(__file__))` 才能拿到**当前正在加载的这个包**的真实路径，C++ 侧无法可靠推断。这正是 u1-l2 讲的「随包分发头文件 + JIT」设计的关键一环。

**练习 2**：如果 `CUDA_HOME` 和 `CUDA_PATH` 都没设、机器上也没装 `nvcc`，`_find_cuda_home()` 会返回什么？

> **答案**：先兜底到 `/usr/local/cuda`；若该路径也不存在，则返回 `None`，随后 [deep_gemm/__init__.py:118](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/__init__.py#L118) 的 `assert cuda_home is not None` 会在 import 阶段直接报错。所以「能 import 成功」本身就隐含了「找到了一个 CUDA 安装」。

---

### 4.3 最小 GEMM 调用示例

#### 4.3.1 概念说明

理论讲再多，不如跑一次。本节带你完成**第一次真实的 FP8 GEMM 调用**，并用 `calc_diff` 校验结果对不对。我们直接以官方测试 `tests/test_fp8_fp4.py` 里的 `test_gemm()` 为蓝本，把它「剥」成一个最小可运行片段。

DeepGEMM 的 FP8 GEMM 调用有几个**新手容易踩坑**的关键点，先列在这里：

1. **A 和 B 是「元组」而非裸张量**：每个输入是 `(fp8_tensor, scaling_factor)` 二元组。直接传裸张量会报参数数量错误。
2. **输出 `d` 要预先分配**：DeepGEMM 不替你创建输出张量，你需要先 `torch.empty(...)` 一个 `d` 传进去。
3. **SF 格式与架构相关**：SM90 用 FP32 SF、SM100 用 UE8M0。我们用 `get_arch_major()` 自动判定，再用对应的 `disable_ue8m0_cast` 开关。
4. **SF 变换由 API 内部完成**：你只需提供「用户格式」的 SF（`per_token_cast_to_fp8` 的输出），API 内部会调用 `transform_sf_into_required_layout` 把它变成 kernel 要的 TMA 对齐布局。这一点在 u2-l2 详讲。

#### 4.3.2 核心流程

一次最小 FP8 GEMM 的步骤：

```
1. 造 BF16 参考输入 A[M,K]、B[N,K]，并算参考输出 ref_d = A·Bᵀ
2. 用 per_token_cast_to_fp8 把 A、B 量化成 (fp8_tensor, sf) 元组
3. 预分配输出 d = torch.empty([M,N], bfloat16)
4. 调用 deep_gemm.fp8_fp4_gemm_nt((a_fp8,sfa), (b_fp8,sfb), d, disable_ue8m0_cast=...)
5. diff = calc_diff(d, ref_d)；diff < 阈值 即通过
```

数值校验用 `calc_diff`，它度量的是 1 减去两个张量的「余弦相似度」：

\[
\mathrm{sim}(x, y) = \frac{2\sum_i x_i y_i}{\sum_i x_i^2 + \sum_i y_i^2}, \qquad \mathrm{diff} = 1 - \mathrm{sim}
\]

`diff` 越接近 0 表示两者越一致。测试里对 FP8xFP8 的阈值是 `0.001`。

#### 4.3.3 源码精读

**（a）官方范例：`test_gemm()` 的核心几行**

[tests/test_fp8_fp4.py:32-55](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/tests/test_fp8_fp4.py#L32-L55) —— 这是我们要模仿的「模板」。关键行：

- L46：`a, b, c, d, ref_d = generate_normal(...)` 一次性生成 FP8 输入元组、输出 buffer、参考输出；
- L52：`getattr(deep_gemm, func_name)(a, b, d, c=c, disable_ue8m0_cast=..., recipe=..., recipe_a=..., recipe_b=...)` 是真正的 kernel 调用——注意 `a`、`b` 是 `(tensor, sf)` 元组、`d` 是预分配输出、`c` 可选（累加用）；
- L53–55：`diff = calc_diff(d, ref_d)`，断言 `diff < quant_config.max_diff()`。

[tests/test_fp8_fp4.py:57-65](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/tests/test_fp8_fp4.py#L57-L65) —— 性能测量：用 `bench_kineto` 采 kernel 耗时，换算 TFLOPS、GB/s，并和 cuBLASLt 比值。

**（b）`generate_normal` 如何造出 FP8 元组**

[tests/generators.py:301-324](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/tests/generators.py#L301-L324) —— 它先用 `torch.randn` 造 BF16 的 `a`、`b`，算 `ref_d = (a.float() @ b.float().t() + ...)`，再用 `cast_fp8_fp4_with_major` 把它们量化。我们看到「参考输出」就是用 BF16/FP32 算出来的「真值」，kernel 的任务是用 FP8 近似还原它。

**（c）量化工具：`per_token_cast_to_fp8`**

[deep_gemm/utils/math.py:26-38](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/utils/math.py#L26-L38) —— 把一个 `[m, n]` 张量按 `gran_k`（默认 128）切块，每块求 `amax`，除以 448 得 `sf`，再把原张量除以 `sf` 转 FP8。返回 `(x_fp8, sf)`。当 `use_ue8m0=True` 时，`sf` 会被 `ceil_to_ue8m0` 处理成 UE8M0 浮点格式（但默认 `use_packed_ue8m0=False`，**不**打包成 int——打包这一步交给 kernel API 内部处理）。

**（d）数值校验：`calc_diff`**

[deep_gemm/testing/numeric.py:5-11](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/testing/numeric.py#L5-L11) —— 即上面 §4.3.2 给出的公式实现：先转 double，分母为 0（全零）时返回 0，否则返回 `1 - 2·Σxy / (Σx² + Σy²)`。

**（e）架构判定：`get_arch_major`**

[deep_gemm/testing/utils.py:6-8](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/testing/utils.py#L6-L8) —— 一行 `torch.cuda.get_device_capability()[0]`，返回 9（SM90）或 10（SM100）。这是全库架构派发的核心开关，u1-l1 已强调过。

**（f）C++ 侧：`fp8_fp4_gemm_nt` 的 Python 签名**

[csrc/apis/gemm.hpp:649-654](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/gemm.hpp#L649-L654) —— 确认 Python 侧签名：`fp8_fp4_gemm_nt(a, b, d, c=None, recipe=None, recipe_a=None, recipe_b=None, compiled_dims="nk", disable_ue8m0_cast=False)`。`a`、`b` 是 `(tensor, sf)` 元组、`d` 是输出、其余是可选参数。注意它被 `#if DG_FP8_COMPATIBLE and DG_TENSORMAP_COMPATIBLE` 守卫——只有 FP8 + TMA 都启用的构建才会注册这个函数。至于它内部如何按 `arch_major` 派发到 SM90/SM100 实现，留给 u2-l3。

#### 4.3.4 代码实践（本讲主实践）

> **实践类型：运行型（需要 SM90 或 SM100 GPU + 已装好的 deep_gemm）**
> 本实践直接对应任务要求：参考 `generate_normal`，构造 FP8 张量与 SF，调用 `fp8_fp4_gemm_nt`，用 `calc_diff` 校验。

1. **目标**：独立写出并跑通一次 FP8 GEMM，确认数值正确。

2. **操作步骤**：把下面的「示例代码」保存为 `first_call.py`，放在仓库根目录（这样能复用 `tests/generators.py` 的导入路径，但本例**不依赖** generators，完全自包含）。然后在装好 DeepGEMM 的机器上运行 `python first_call.py`。

   ```python
   # 示例代码：DeepGEMM 第一次 FP8 GEMM 调用（自包含，改编自 tests/test_fp8_fp4.py::test_gemm）
   import torch
   import deep_gemm
   from deep_gemm.testing import calc_diff, get_arch_major
   from deep_gemm.utils import per_token_cast_to_fp8

   torch.manual_seed(0)

   # 1) NT 布局：A=[M,K] K-major，B=[N,K] K-major，D=C+A@B.T
   M, N, K = 128, 4096, 7168
   a = torch.randn((M, K), device='cuda', dtype=torch.bfloat16)
   b = torch.randn((N, K), device='cuda', dtype=torch.bfloat16)
   ref_d = (a.float() @ b.float().t()).to(torch.bfloat16)   # 用 FP32 算“真值”

   # 2) 量化：SM90 用 FP32 SF，SM100 用 UE8M0 SF；由架构决定
   use_ue8m0 = (get_arch_major() == 10)
   a_fp8, sfa = per_token_cast_to_fp8(a, use_ue8m0=use_ue8m0, gran_k=128)
   b_fp8, sfb = per_token_cast_to_fp8(b, use_ue8m0=use_ue8m0, gran_k=128)

   # 3) 预分配输出（DeepGEMM 不替你创建输出张量）
   d = torch.empty((M, N), device='cuda', dtype=torch.bfloat16)

   # 4) 调用：注意 a/b 是 (tensor, sf) 元组；disable_ue8m0_cast 与 use_ue8m0 取反
   deep_gemm.fp8_fp4_gemm_nt((a_fp8, sfa), (b_fp8, sfb), d,
                             disable_ue8m0_cast=not use_ue8m0)

   # 5) 校验
   diff = calc_diff(d, ref_d)
   print(f'arch_major={get_arch_major()}, use_ue8m0={use_ue8m0}, diff={diff:.5f}')
   assert diff < 1e-3, f'数值误差过大: {diff}'
   print('OK: 第一次 FP8 GEMM 调用通过')
   ```

3. **观察现象**：
   - 首次运行会触发 JIT 编译（可能耗时数秒到数十秒），后续同形状调用走缓存，几乎瞬时；
   - 打印形如 `arch_major=10, use_ue8m0=True, diff=0.00012`；
   - `diff` 应明显小于 `1e-3`。
4. **预期结果**：脚本正常退出并打印 `OK: ...`。若 `diff` 偏大，常见原因是把 A 或 B 当成裸张量传入、或 `disable_ue8m0_cast` 与架构不匹配。
5. **运行结果待本地验证**（本环境无 SM90/SM100 GPU）。但该片段与官方测试 `test_gemm` 的调用方式逐行对应，逻辑可由 [tests/test_fp8_fp4.py:52](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/tests/test_fp8_fp4.py#L52) 背书。

#### 4.3.5 小练习与答案

**练习 1**：如果把第 4 步写成 `deep_gemm.fp8_fp4_gemm_nt(a_fp8, b_fp8, d, ...)`（即传裸张量而不是元组），会发生什么？

> **答案**：会抛 pybind11 的参数匹配错误。因为 [csrc/apis/gemm.hpp:649-650](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/gemm.hpp#L649-L650) 里 `a`、`b` 的 C++ 类型期望从 Python 元组里解出「张量 + 缩放因子」两部分。DeepGEMM 约定：**FP8/FP4 输入永远是 `(tensor, sf)` 元组**，这是它和普通 GEMM 库最大的使用差异之一。

**练习 2**：`calc_diff` 为什么用「1 减余弦相似度」而不是直接用最大绝对误差（max abs error）？

> **答案**：因为 FP8 量化带来的误差是**分布性的**——每个元素都有小幅扰动，但整体方向（与真值的角度）几乎不变。余弦相似度衡量的是「方向一致性」，对这种整体小幅噪声不敏感，能稳定地给出一个接近 0 的小数；而 max abs error 会被个别离群点放大，阈值难定。所以 DeepGEMM 测试统一用 `calc_diff`（见 [deep_gemm/testing/numeric.py:5-11](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/testing/numeric.py#L5-L11)），阈值随精度档位变化（FP8xFP8 用 0.001，含 FP4 时放宽到 0.01–0.02）。

**练习 3**：`ref_d` 为什么用 `a.float() @ b.float().t()` 而不是直接 `a @ b.t()`？

> **答案**：`a`、`b` 是 BF16，直接相乘会以 BF16 精度累加，引入额外的舍入误差，使「参考真值」本身就不准。转成 `float`（FP32）后再乘，能最大限度消除参考计算的误差，让 `diff` 只反映 **DeepGEMM FP8 kernel 本身**的量化误差，而不是参考实现的误差。这正是 [tests/generators.py:312](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/tests/generators.py#L312) 的做法。

---

## 5. 综合实践

把本讲的三个模块（接口清点、`_C.init`、最小调用）串起来，完成下面这个**带性能测量的小任务**：

> **任务**：在「第一次调用」脚本的基础上，加入 `bench_kineto` 测出 kernel 的耗时，换算成 TFLOPS，并与 cuBLASLt 对比，体会 DeepGEMM 的性能定位。

参考实现（在 §4.3.4 脚本末尾追加）：

```python
# 示例代码：性能采样与 cuBLASLt 对比（改编自 tests/test_fp8_fp4.py:57-65）
from deep_gemm.testing import bench_kineto, count_bytes

t = bench_kineto(
    lambda: deep_gemm.fp8_fp4_gemm_nt((a_fp8, sfa), (b_fp8, sfb), d,
                                      disable_ue8m0_cast=not use_ue8m0),
    'gemm_', suppress_kineto_output=True)

# cuBLASLt 只接受裸张量，且不支持 FP4；此处 A/B 都是 FP8，可直接对比
cublas_t = bench_kineto(
    lambda: deep_gemm.cublaslt_gemm_nt(a_fp8, b_fp8, d),
    'nvjet', suppress_kineto_output=True)

flops = 2 * M * N * K
bytes_moved = count_bytes((a_fp8, sfa), (b_fp8, sfb), d)
print(f'{t * 1e6:6.1f} us | {flops / t / 1e12:4.0f} TFLOPS | '
      f'{bytes_moved / 1e9 / t:4.0f} GB/s | {cublas_t / t:.2f}x cuBLAS')
```

**要求与观察**：

1. 解释为什么 `bench_kineto` 第二个参数传的是字符串 `'gemm_'`（提示：它是用来在 profiler 表里匹配 kernel 名字的前缀，见 [deep_gemm/testing/bench.py:120-123](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/testing/bench.py#L120-L123)）。
2. 推导 TFLOPS 公式 \(2MNK/t\) 里「2」的来历（每次乘加算 2 次浮点运算）。
3. 观察 DeepGEMM 相对 cuBLASLt 的加速比，并思考：为什么小 M（如 M=128）时加速比通常不如大 M？

运行结果**待本地验证**（需 GPU）。完成后，你应当能独立写出「构造 FP8 输入 → 调用 → 校验 → 测速」的完整闭环——这正是后续每篇进阶讲义做实验的基本骨架。

## 6. 本讲小结

- DeepGEMM 的 Python 接口全部挂在 `deep_gemm` 顶层，按「配置旋钮 / cuBLASLt 基准 / FP8·FP4·BF16 GEMM 及分组版 / Einsum / MQA / HyperConnection / 布局工具 / Mega MoE / testing·utils / legacy」分类，命名遵循 `<精度>_<算子>_<布局>` 约定（§4.1）。
- `import deep_gemm` 的最后一步自动调用 `_C.init(library_root, cuda_home)`，把「设备头文件目录」和「CUDA 安装目录」告诉 C++ 侧的 JIT 编译器/加载器/头文件解析器——import 成功即等价于初始化完成（§4.2）。
- FP8 GEMM 调用有三个新手坑：输入是 `(tensor, sf)` **元组**、输出 `d` 要**预分配**、SF 格式随**架构**（SM90=FP32 / SM100=UE8M0）变化并用 `disable_ue8m0_cast` 开关控制（§4.3）。
- SF 从「用户格式」到「kernel 所需 TMA 布局」的变换由 API 内部完成，用户只需提供 `per_token_cast_to_fp8` 的输出。
- 数值校验统一用 `calc_diff`（1 减余弦相似度），FP8xFP8 阈值 `0.001`；参考输出用 FP32 计算以排除参考误差。
- 你已能独立写出「构造 FP8 输入 → 调用 `fp8_fp4_gemm_nt` → `calc_diff` 校验 → `bench_kineto` 测速」的完整闭环。

## 7. 下一步学习建议

本讲把 Python 层当成黑盒跑通了。接下来建议：

1. **u2-l1《GEMM 命名约定与 NT 布局》**：深入理解 `D=C+A@B`、NT/TN/NN/TT 四种布局的内存含义，以及为什么 SM90 只支持 NT、SM100 支持全部四种。这能解释本讲里 `a`、`b` 形状为何如此安排。
2. **u2-l2《缩放因子 recipe 与 UE8M0 打包》**：搞懂 `gran_k`、recipe=(gran_mn, gran_k)、SM90 FP32 SF 与 SM100 打包 UE8M0 的差异，以及 `transform_sf_into_required_layout` 到底把 SF 变成了什么形状。
3. **u2-l3《C++ 绑定与 API 派发层》**：跟踪 `fp8_fp4_gemm_nt` 从 pybind11 注册到按 `get_arch_major()` 派发到 SM90/SM100 实现的完整链路——即本讲刻意跳过的「黑盒内部」。

如果你想在动手派发之前先理解「为什么 import 时要 JIT 初始化」，也可以先跳到 **u3-l1《JIT 架构总览》**，再回头读 u2 系列。
