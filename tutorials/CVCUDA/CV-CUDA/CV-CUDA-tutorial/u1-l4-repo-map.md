# 仓库代码地图：如何高效检索这个大型仓库

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 CV-CUDA 顶层九大目录（`src`、`python`、`tests`、`bench`、`samples`、`docs`、`docker`、`ci`、`lint`）各自的职责，并知道官方把它们记录在哪里。
2. 只凭一个算子名（如 `Gaussian`、`MedianBlur`），按命名规律直接写出它在各层的全部相关文件路径：C 头、C++ 类头、C API 实现、priv 实现、CUDA 内核、Python 绑定、C++ 测试、Python 测试、C++/Python 基准、示例与文档。
3. 说清楚 `tests/cvcuda`、`bench`、`samples` 三套外围代码在组织方式上的差异，以及为什么会有这些差异。
4. 列出 `AGENTS.md` 中的仓库不变量（SPDX 头、生成式 requirements、CUDA 12/13 配对等），在动手改代码之前不至于踩雷。

本讲是第一单元的收官：前三讲让你「跑起来」，这一讲给你一张「藏宝图」——之后无论是读第二单元的张量源码，还是第五单元解剖算子内部，你都先靠这张图定位文件。

## 2. 前置知识

本讲不需要写 CUDA 代码，但依赖以下在前面几讲（尤其 u1-l1）建立的概念：

- **四层架构**：一次 `cvcuda.gaussian(...)` 调用会依次穿过 Python 绑定层（`python/mod_cvcuda`）→ C API 层（`src/cvcuda/Op*.cpp`）→ C++ 私有实现层（`src/cvcuda/priv`）→ CUDA 内核层（`src/cvcuda/priv/legacy/*.cu` 或 `priv/Op*.cu`）。
- **算子（operator）**：CV-CUDA 中的一个图像处理功能单元，如 resize、flip、gaussian。仓库里目前有 61 个算子登记在 `src/cvcuda/CMakeLists.txt` 的源文件清单里。
- **nvcv 类型层**：`src/nvcv` 提供张量、图像批等数据类型，被上面各层共同使用。
- **变长批（var-shape）**：批内每张图尺寸可以不同的输入形态。很多算子因此有两套入口：固定张量版和 `VarShape` 版。
- **allocating / `_into` 变体**：Python 侧每个算子通常同时提供 `cvcuda.xxx`（隐式分配输出）和 `cvcuda.xxx_into`（写入调用者给定的输出）。
- **rg**：一个高速的代码检索工具（`grep` 的现代替代品），本仓库的 `AGENTS.md` 明确推荐 `rg` 与 `rg --files` 做检索。

另外要认识两种「入口文件」：`AGENTS.md` 是给人类与 AI 代理看的仓库行为规范，`README.md` 是项目说明。两者是仓库的权威文档入口。

## 3. 本讲源码地图

| 文件/目录 | 作用 |
|---|---|
| [AGENTS.md](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/AGENTS.md) | 仓库地图表、权威文档索引、仓库不变量、验证命令（`CLAUDE.md` 是它的符号链接） |
| `src/cvcuda/` | 算子库本体：C API、C++ 私有实现、CUDA 内核 |
| [src/cvcuda/CMakeLists.txt](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/CMakeLists.txt) | 算子源文件的「注册清单」，是核对算子总数的第一手依据 |
| `python/mod_cvcuda/` | pybind11 绑定：`operators/` 放算子，`nvcv/` 放数据类型 |
| [tests/README.md](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/README.md) | 测试目录结构与运行方式 |
| [bench/README.md](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/README.md) | 基准目录结构、运行与基线对比方式 |
| [samples/README.md](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/README.md) | 示例的分类与运行方式 |
| `tools/` | 贡献者工具链：`make_op.py`、`mkop/`、`review_op.py`、`optimize_op.py`、`refactor_op.py` 等 |

贯穿全讲的示例算子是 **Gaussian（高斯模糊）**——它足够典型，又藏着一个反直觉的惊喜（内核文件名不叫 `gaussian.cu`），正好用来检验你是否真的掌握了检索方法。

## 4. 核心概念与源码讲解

### 4.1 顶层目录地图：AGENTS.md 是官方导航

#### 4.1.1 概念说明

大型仓库最大的痛点不是「代码难」，而是「找不到」。CV-CUDA 的解法是把目录职责写成一张表，放在 `AGENTS.md` 里。这个文件是给「在仓库里干活的智能体」（人类工程师也好、AI 编码助手也好）看的规范入口，`CLAUDE.md` 只是它为 Claude Code 准备的符号链接——所以无论你用什么工具，读这一份就够了。

#### 4.1.2 核心流程

拿到任何一个 CV-CUDA 相关的问题，按下面的决策路径选目录：

```text
问题类型                     → 去哪里找
─────────────────────────────────────────────────────────
算子怎么用（Python）         → samples/、docs/sphinx/
算子的 Python 签名/文档      → python/mod_cvcuda/operators/Op*.cpp
算子的 C/C++ API 与支持矩阵  → src/cvcuda/include/cvcuda/Op*.h/.hpp
算子的实现逻辑              → src/cvcuda/priv/
CUDA 内核                   → src/cvcuda/priv/legacy/*.cu 或 priv/Op*.cu
张量/图像批等类型            → src/nvcv/
怎么构建                    → build.sh、CMakePresets.json、docker/
怎么测试/看行为预期          → tests/
性能数据                    → bench/
仓库规则/工作流             → AGENTS.md、.agents/guidance/
```

#### 4.1.3 源码精读

[AGENTS.md:23-35](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/AGENTS.md#L23-L35) 是官方的仓库地图表，把 `src/`、`python/`、`tests/`、`bench/`、`samples/`、`docs/`、`docker/`、`ci/`、`lint/` 九个顶层目录的职责一行一个说清楚——本讲 4.1.2 的决策路径就是从这张表展开的。

[AGENTS.md:37-56](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/AGENTS.md#L37-L56) 是「权威文档」清单：测试看 `tests/README.md`、基准看 `bench/README.md`、示例看 `samples/README.md`、新增算子看 `.agents/guidance/MAKE_OP_GUIDELINES.md`。注意最后一句提醒：这些文档才是事实来源（source of truth），别在其他地方复制它们的清单。

另外，[AGENTS.md:66](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/AGENTS.md#L66) 明确推荐用 `rg` 和 `rg --files` 检索本仓库——后面所有实践我们都用它。

#### 4.1.4 代码实践

1. **实践目标**：把顶层目录地图「跑」一遍，建立肌肉记忆。
2. **操作步骤**：
   ```bash
   cd CV-CUDA 仓库根目录
   ls                       # 对照 AGENTS.md 的九大目录
   rg --files tools/        # 看看贡献者工具链有哪些
   ls .agents/guidance/     # 看看专题指南清单
   ```
3. **需要观察的现象**：`tools/` 下能看到 `make_op.py`、`mkop/`、`review_op.py`、`optimize_op.py`、`refactor_op.py` 等条目；`.agents/guidance/` 下能看到 `MAKE_OP_GUIDELINES.md` 等指南文件。
4. **预期结果**：你能在 30 秒内回答「CV-CUDA 的性能基准放在哪个目录」「新增算子的指南在哪」这两个问题。

#### 4.1.5 小练习与答案

**练习 1**：`CLAUDE.md` 和 `AGENTS.md` 是什么关系？改仓库规范应该改哪个？
**答案**：`CLAUDE.md` 是指向 `AGENTS.md` 的符号链接，`AGENTS.md` 是唯一正本（见 [AGENTS.md:6-10](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/AGENTS.md#L6-L10)）。规范只需维护 `AGENTS.md` 一份。

**练习 2**：想知道「CV-CUDA 支持哪些基准 GPU 基线」应该先读哪个文件？
**答案**：`bench/README.md`（AGENTS.md 指定的基准权威文档），其中「Compare with Committed Baselines」一节列出了 A100/H100 两种参考 GPU。

### 4.2 src/cvcuda：一个算子的四层文件与命名规律

#### 4.2.1 概念说明

`src/cvcuda` 是算子库本体。u1-l1 讲过四层架构，现在把它落到**文件名**上：对算子 `Xxx`，每一层都有固定命名模式。掌握这张表后，你不需要搜索引擎，直接手写路径就能打开对应文件：

| 层 | 文件模式 | 以 Gaussian 为例 |
|---|---|---|
| ① C API 声明（含 Limitations 契约） | `src/cvcuda/include/cvcuda/OpXxx.h` | `OpGaussian.h` |
| ② C++ RAII 类声明 | `src/cvcuda/include/cvcuda/OpXxx.hpp` | `OpGaussian.hpp` |
| ③ C API 实现 | `src/cvcuda/OpXxx.cpp` | `OpGaussian.cpp` |
| ④ C++ 私有实现 | `src/cvcuda/priv/OpXxx.cpp` + `OpXxx.hpp`（部分算子还有 `OpXxx.cu`） | `OpGaussian.cpp/.hpp` |
| ⑤ CUDA 内核（legacy 形态） | `src/cvcuda/priv/legacy/<族名>.cu` | `filter.cu`、`filter_var_shape.cu` |

两个必须警惕的「陷阱」：

- **陷阱一：⑤ 的文件名不保证是算子名。** Gaussian 的内核不在 `gaussian.cu`（那个文件属于另一个算子 GaussianNoise 噪声），而在 `filter.cu` 里——因为高斯模糊属于「滤波器族」，和 AverageBlur、BoxBlur 共用一个滤波框架。检索内核时要用类名（`Gaussian::infer`）而不是文件名。
- **陷阱二：Gaussian ≠ GaussianNoise。** `Gaussian` 是高斯模糊（滤波），`GaussianNoise` 是加噪声，两个完全不同的算子，文件名只差一个词干。搜索时要用精确词边界（`rg '\bGaussian\b'`）避免混入。

#### 4.2.2 核心流程

一个算子名如何逐步展开成一组文件路径：

```text
输入: 算子名 "Gaussian"
1. include/cvcuda/OpGaussian.h      ← 看支持矩阵（Limitations）
2. include/cvcuda/OpGaussian.hpp    ← 看 C++ 类签名
3. src/cvcuda/OpGaussian.cpp        ← 看 C API 如何转发
4. priv/OpGaussian.cpp              ← 看参数校验、内核选择
5. priv/OpGaussian.cpp 里的 using → legacy::Gaussian
6. rg "class Gaussian" priv/legacy/ → CvCudaLegacy.h:1413（声明）
7. rg "Gaussian::infer" priv/legacy/ → filter.cu:1259（实现）
```

第 5→7 步是跨过陷阱的关键：priv 实现里 `namespace legacy = nvcv::legacy::cuda_op;`（[src/cvcuda/priv/OpGaussian.cpp:29](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpGaussian.cpp#L29)），顺着 `legacy::Gaussian` 这个符号搜，而不是顺着文件名猜。

#### 4.2.3 源码精读

**CMakeLists 是算子的「户口本」。** [src/cvcuda/CMakeLists.txt:23-85](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/CMakeLists.txt#L23-L85) 的 `CV_CUDA_OP_FILES` 清单逐行列出全部 61 个算子的 ③ 层实现文件，`OpGaussian.cpp` 在 [第 59 行](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/CMakeLists.txt#L59)。想知道「这个仓库到底有多少算子、有没有我要找的那个」，先看这里。

这个 CMakeLists 还内置了一个检索加速器：[src/cvcuda/CMakeLists.txt:87-104](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/CMakeLists.txt#L87-L104) 的 `CV_CUDA_SRC_PATERN` 机制可以按模式（大小写不敏感）只编译匹配的算子文件——调试单个算子时能大幅缩短编译时间。

**③ 层长什么样？** [src/cvcuda/OpGaussian.cpp:30-46](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/OpGaussian.cpp#L30-L46) 用 `CVCUDA_DEFINE_API` 定义 `cvcudaGaussianCreate`：校验 handle 非空后，通过 `priv::CreateOperatorHandle<priv::Gaussian>` 构造实现对象并以不透明句柄返回给 C 调用方。紧随其后的 [cvcudaGaussianSubmit（L48-62）](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/OpGaussian.cpp#L48-L62) 与 [cvcudaGaussianVarShapeSubmit（L64-79）](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/OpGaussian.cpp#L64-L79) 分别是固定张量版与变长批版的执行入口——「一个算子、两个 Submit」是本仓库的普遍模式。

**④ 层如何接到 ⑤ 层？** [src/cvcuda/priv/OpGaussian.cpp:31-49](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpGaussian.cpp#L31-L49) 的构造函数用 `PerDeviceResource` 惰性创建两个 legacy 对象：`legacy::Gaussian`（固定批）和 `legacy::GaussianVarShape`（变长批）。注意注释说明 legacy 算子天生单设备，所以要按设备各建一份以支持多 GPU。

**⑤ 层的真身。** `Gaussian` 类声明在 [src/cvcuda/priv/legacy/CvCudaLegacy.h:1413](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/legacy/CvCudaLegacy.h#L1413)（所有 legacy 内核类的总头文件），实现却在 [src/cvcuda/priv/legacy/filter.cu:1247-1259](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/legacy/filter.cu#L1247-L1259)（构造函数与 `infer` 执行函数），变长批版在 [filter_var_shape.cu:2569](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/legacy/filter_var_shape.cu#L2569)。整个 `legacy/` 目录共有 57 个 `.cu` 文件，按「功能族」而不是「算子名」组织——这正是陷阱一的根源。

#### 4.2.4 代码实践

1. **实践目标**：用一个新算子（`MedianBlur`）检验四层定位法。
2. **操作步骤**：
   ```bash
   # ① + ②：公开头文件应同时有 .h 和 .hpp
   ls src/cvcuda/include/cvcuda/ | grep -i median
   # ③：C API 实现
   ls src/cvcuda/Op*.cpp | grep -i median
   # ④：priv 实现
   ls src/cvcuda/priv/ | grep -i median
   # ⑤：内核——先看 priv 实现引用了哪个 legacy 类
   rg -n "legacy::" src/cvcuda/priv/OpMedianBlur.cpp | head -5
   # 再按类名全局搜内核实现位置
   rg -ln "MedianBlur::infer" src/cvcuda/priv/legacy/
   ```
3. **需要观察的现象**：①②③④ 四步都能按 `OpMedianBlur.*` 命名直接命中；第 5 步能看到 priv 实现引用 `legacy::MedianBlur`（或其变体）；最后一步定位到的 `.cu` 文件名可能与算子名不同。
4. **预期结果**：你得到 `MedianBlur` 的 5 层完整文件清单，并且体会到「前四层靠命名、第五层靠 rg」的检索节奏。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `ls src/cvcuda/priv/legacy/ | grep gaussian` 找不到高斯模糊的内核？
**答案**：能找到，但找到的是 `gaussian_noise*.cu`（GaussianNoise 加噪声算子的内核）。高斯模糊的内核在 `filter.cu` / `filter_var_shape.cu` 里，因为 legacy 目录按功能族（滤波器族）组织文件。正确做法是搜类名：`rg "Gaussian::infer" src/cvcuda/priv/legacy/`。

**练习 2**：`src/cvcuda/include/cvcuda/` 下同一算子的 `.h` 和 `.hpp` 有什么分工？
**答案**：`.h` 是纯 C API（`cvcudaGaussianCreate/Submit/Destroy` 句柄式生命周期，供 C 使用者和 Python/其他语言绑定调用），`.hpp` 是对 C API 的 C++ RAII 内联包装（`cvcuda::Gaussian` 类，自动管理句柄）。u6-l1 会专门对比两者。

**练习 3**：不看文件内容，如何最快确认仓库里登记了多少个算子？
**答案**：数 [src/cvcuda/CMakeLists.txt:23-85](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/CMakeLists.txt#L23-L85) 里 `CV_CUDA_OP_FILES` 清单的条目数（当前 61 条，可用 `grep -c "^    Op" src/cvcuda/CMakeLists.txt` 验证）。

### 4.3 python/mod_cvcuda：绑定层的两条产品线

#### 4.3.1 概念说明

`python/mod_cvcuda` 是 pybind11 绑定模块，内部有两条清晰的「产品线」：

- **`operators/`**：61 个 `OpXxx.cpp`，与 C++ 算子一一对应，负责把 ③④ 层的能力暴露成 `cvcuda.gaussian`、`cvcuda.gaussian_into` 这样的 Python 函数。
- **`nvcv/`**：数据类型绑定（`Tensor.cpp`、`ImageBatch.cpp`、`Stream.cpp`、`Cache.cpp`、`DLPackUtils.cpp` 等），对应 `src/nvcv` 的类型层。

顶层还散布着枚举与辅助类型（`BorderType.cpp`、`InterpolationType.cpp`、`OsdElement.cpp`、`NvtxRange.hpp` 等），以及唯一的模块入口 `Main.cpp`。

Python 侧的命名规律与 C++ 侧错位一格，需要适应：**文件/类名用 PascalCase（`OpGaussian.cpp`、`ExportOpGaussian`），导出的函数名用全小写 snake_case（`gaussian`、`gaussian_into`）**。

#### 4.3.2 核心流程

一个算子绑定被「激活」要经过三站：

```text
python/mod_cvcuda/operators/OpGaussian.cpp   定义 Gaussian/GaussianInto/VarShape 版本
        ↓ 导出函数
python/mod_cvcuda/operators/Operators.hpp    声明 void ExportOpGaussian(py::module &)
        ↓ 注册调用
python/mod_cvcuda/Main.cpp                   在模块初始化时逐个调用 ExportOpXxx(m)
```

新增算子绑定时三处都要动——这是 u8-l2 的主题，这里先记住链路。

#### 4.3.3 源码精读

**allocating 与 `_into` 在绑定层的分身。** [python/mod_cvcuda/operators/OpGaussian.cpp:60-66](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpGaussian.cpp#L60-L66) 定义 Python 函数 `Gaussian`：内部先 `Tensor::Create` 分配输出，再转调 `GaussianInto`——这就是 u1-l2 见过的 allocating 变体的实现现场。变长批版本 `VarShapeGaussian`（[L92-97](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpGaussian.cpp#L92-L97)）同样如此。

**导出与注册。** [python/mod_cvcuda/operators/OpGaussian.cpp:102-160](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpGaussian.cpp#L102-L160) 的 `ExportOpGaussian` 用 `m.def("gaussian", ...)`、`m.def("gaussian_into", ...)` 把两套（固定批 + 变长批）共四个函数挂到模块上，每个都包了一层 `NvtxTrace`（性能分析埋点，u7-l4 详讲），并带完整 docstring。[Operators.hpp:63](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/Operators.hpp#L63) 声明导出函数，[Main.cpp:154-157](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/Main.cpp#L154-L157) 附近则是一长串 `ExportOpXxx(m)` 注册调用——三站链路的实体。

#### 4.3.4 代码实践

1. **实践目标**：验证「operators/ 与 CMakeLists 算子数一一对应」，并亲手追一次 Gaussian 的三站链路。
2. **操作步骤**：
   ```bash
   # 绑定文件数量
   ls python/mod_cvcuda/operators/Op*.cpp | wc -l
   # 与 CMakeLists 登记数对比
   grep -c "^    Op" src/cvcuda/CMakeLists.txt
   # 三站链路
   rg -n "ExportOpGaussian" python/mod_cvcuda/        # 定义+声明
   rg -n "ExportOpGaussian\(m\)" python/mod_cvcuda/Main.cpp  # 注册
   # 从 Python 函数名反向找绑定文件
   rg -ln 'def\("gaussian_into"' python/mod_cvcuda/
   ```
3. **需要观察的现象**：两个计数都是 61；`ExportOpGaussian` 出现在 `operators/OpGaussian.cpp`（定义）、`operators/Operators.hpp`（声明）、`Main.cpp`（注册）三个文件。
4. **预期结果**：你能从任意一个 Python API 名（如 `cvcuda.median_blur_into`）反查出它的绑定文件。注意 snake_case 到 PascalCase 的转换：`median_blur` → `MedianBlur`。

#### 4.3.5 小练习与答案

**练习 1**：`python/mod_cvcuda/nvcv/Tensor.cpp` 和 `src/nvcv/src/include/nvcv/Tensor.hpp` 是什么关系？
**答案**：前者是 pybind11 绑定（把 C++ 的 `nvcv::Tensor` 包装成 Python 的 `cvcuda.Tensor` 类型），后者是被包装的 C++ 本体。绑定层目录结构刻意镜像了 `src/nvcv` 的类型布局。

**练习 2**：为什么 `cvcuda.gaussian` 和 `cvcuda.gaussian_into` 能同名出现在同一个模块里？
**答案**：pybind11 的 `m.def` 支持 C++ 重载式注册——[OpGaussian.cpp:106 与 L140](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpGaussian.cpp#L106-L140) 把固定批版和变长批版都注册成 `"gaussian"`，运行时按第一个参数的类型（`Tensor` 还是 `ImageBatchVarShape`）分派。

### 4.4 tests/cvcuda、bench、samples：三套外围代码的组织差异

#### 4.4.1 概念说明

围绕算子库有三套「外围」代码，目的不同，组织方式也不同——理解差异后你就知道该去哪找答案：

| 维度 | `tests/` | `bench/` | `samples/` |
|---|---|---|---|
| 回答的问题 | 「结果对不对？」 | 「跑得快不快？」 | 「怎么用？」 |
| 语言 | C++（googletest）+ Python（pytest） | C++（nvbench）+ Python | 纯 Python |
| 命名（C++） | `TestOpXxx.cpp` | `BenchXxx.cpp` | — |
| 命名（Python） | `test_opxxx.py`（全小写连写） | `bench_xxx.py` | `xxx.py`（目录分类） |
| Gaussian 实例 | `TestOpGaussian.cpp` / `test_opgaussian.py` | `BenchGaussian.cpp` / `bench_gaussian.py` | `operators/gaussian.py` |

注意 Python 测试的命名是**全小写且算子名连写**（`test_opgaussian.py`，不是 `test_op_gaussian.py` 也不是 `test_opGaussian.py`），这是踩过一次坑就忘不了的细节。

#### 4.4.2 核心流程

三套代码各自的检索入口：

```text
tests/   想知道某算子的行为预期（支持哪些 dtype/布局/边界模式）
           → tests/cvcuda/system/TestOpXxx.cpp（参数化用例 + 负向用例）
           → tests/cvcuda/python/test_opxxx.py（Python 视角）
bench/   想知道某算子的性能档位与基线
           → bench/config/operators/gaussian.json（配置与基线数据）
           → bench/cpp/ops/BenchGaussian.cpp、bench/python/ops/bench_gaussian.py（驱动）
samples/ 想看某算子的最小可用代码
           → samples/operators/gaussian.py
           → docs/sphinx/samples/operators/gaussian.rst（由示例生成）
```

tests 内部再分五个子目录：`system/`（C++ 算子系统测试）、`python/`（pytest）、`unit/`（单元测试）、`stressTest/`、`nvtx_probe/`；nvcv 类型层的测试单独放在 `tests/nvcv_types/`。

#### 4.4.3 源码精读

**tests 的目录结构**在 [tests/README.md:43-56](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/README.md#L43-L56) 有官方说明：`tests/cvcuda/python/` 放 `test_op*.py`，其余是 C++ googletest 源码。运行入口是构建产物 `build-rel/bin/run_tests.sh`（[tests/README.md:33-39](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/README.md#L33-L39)），它由 `tests/run_tests.sh.in` 模板生成，构建前不存在。

**C++ 测试的用例骨架。** [tests/cvcuda/system/TestOpGaussian.cpp:77](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/system/TestOpGaussian.cpp#L77) 的 `TEST_P(OpGaussian, correct_output)` 是固定批正向用例，[L179](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/system/TestOpGaussian.cpp#L179) 是变长批版；[L355/L378](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/system/TestOpGaussian.cpp#L355-L378) 是 planar/interleaved 布局一致性用例；[L429/L463](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/system/TestOpGaussian.cpp#L429-L463) 是负向用例（故意传错参数，断言报错）。这套「正向 + 变长批 + 布局一致 + 负向」的四段式结构几乎每个 `TestOpXxx.cpp` 都有——想知道一个算子「保证什么、拒绝什么」，读这一个文件就够。

**Python 测试的风格。** [tests/cvcuda/python/test_opgaussian.py:26-31](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_opgaussian.py#L26-L31) 用 `@pytest.mark.parametrize` 把 shape/dtype/布局/核尺寸/sigma/边界模式组合成用例矩阵，测试函数 `test_op_gaussian` 在 [L61](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_opgaussian.py#L61)。共享工具 `cvcuda_tools.py`、`cvcuda_util.py` 与测试同目录。

**bench 的配置驱动设计。** [bench/README.md:253-267](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/README.md#L253-L267) 给出目录结构：`config/bench_params.json` 是算子清单，每个算子指向 `config/operators/<op>.json` 及配套的 C++/Python 驱动。以 Gaussian 为例：配置 `config/operators/gaussian.json`，C++ 驱动 [bench/cpp/ops/BenchGaussian.cpp:232-233](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/cpp/ops/BenchGaussian.cpp#L232-L233) 用 `NVBENCH_BENCH_TYPES` 注册，类型轴由配置自动生成；Python 驱动 [bench/python/ops/bench_gaussian.py:3](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/python/ops/bench_gaussian.py#L3) 的 docstring 直接说明它是 `BenchGaussian.cpp` 的 Python 等价物——两语言基准成对出现、用于校验绑定层不引入性能损失。

**samples 的分类。** [samples/README.md:68-80](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/README.md#L68-L80) 说明示例按 `operators/`、`applications/`、`interoperability/` 等分类单独运行。高斯示例 [samples/operators/gaussian.py:39-47](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/operators/gaussian.py#L39-L47) 里那对 `# docs_tag: begin_gaussian_blur / end_gaussian_blur` 注释是文档钩子：`docs/sphinx/samples/operators/gaussian.rst` 的代码片段就是从这对标记之间抽取生成的。**改示例代码时若动了标记区间，文档会跟着变**——这是 samples 与 docs 联动的隐藏机制。

#### 4.4.4 代码实践

1. **实践目标**：用「读配置」的方式列出全部可基准算子，并核对三套外围代码的命名差异。
2. **操作步骤**：
   ```bash
   # bench 的算子清单（bench_params.json 的 operators 键）
   rg -o '"(\w+)":\s*\{' -r '$1' bench/config/bench_params.json | head -20
   # 对照驱动文件命名
   ls bench/cpp/ops/BenchGaussian.cpp bench/python/ops/bench_gaussian.py
   # 三套外围对同一算子的命名并排看
   ls tests/cvcuda/system/TestOpGaussian.cpp tests/cvcuda/python/test_opgaussian.py samples/operators/gaussian.py
   ```
3. **需要观察的现象**：四类文件各自命中；C++ 测试与基准用 PascalCase（`TestOpGaussian`、`BenchGaussian`），Python 测试与基准用全小写连写（`test_opgaussian`、`bench_gaussian`），示例用小写加下划线的算子名（`gaussian`）。
4. **预期结果**：给出任意算子名，你能默写出它在三套外围代码中的四个文件名。若想实际运行基准，需先完成 u1-l3 的构建，再执行 `cd build-rel/bin && python3 run_bench.py --operator gaussian`（待本地验证，本讲不依赖）。

#### 4.4.5 小练习与答案

**练习 1**：想确认 `gaussian` 支持哪些 dtype 与边界模式的组合，最省事的文件是哪个？
**答案**：`tests/cvcuda/system/TestOpGaussian.cpp` 的参数化用例（或 [bench/config/operators/gaussian.json](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/config/operators/gaussian.json) 的 `dtypes`/轴配置）。最权威的契约则是 `src/cvcuda/include/cvcuda/OpGaussian.h` 头部的 Limitations 注释。

**练习 2**：`test_opgaussian.py`（无下划线分隔）和 `test_op_gaussian.py`（有下划线）哪个是真实文件名？
**答案**：前者。Python 测试命名是 `test_op` + 全小写连写的算子名，例如 `test_opgaussian.py`、`test_opgaussiannoise.py`。

**练习 3**：为什么 bench 要同时维护 C++ 和 Python 两份驱动？
**答案**：成对运行可以校验「绑定层没有引入性能损失」——`run_bench.py` 在两种语言都跑时会检查 C++/Python 计时在阈值内一致（见 [bench/README.md:76-79](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/README.md#L76-L79)）。

### 4.5 仓库不变量：动手改代码前必须知道的规则

#### 4.5.1 概念说明

「仓库不变量」是所有贡献者必须遵守的硬约束。它们不影响你**读**代码，但直接影响你**改**代码——违反了会在评审或 CI 阶段被打回。这些规则记录在 [AGENTS.md:71-88](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/AGENTS.md#L71-L88)。

#### 4.5.2 核心流程

改动落地前的自查清单：

```text
新增 .py/.cpp/.cu/.md 文件？   → 加 NVIDIA Apache 2.0 SPDX 头（2026 年新建用 Copyright (c) 2026，不用区间）
想改 requirements*.txt？       → 别手改；改对应 .template 或 versions.env，然后 bash generate_requirements.sh
动了 CUDA 12 相关路径？        → 检查 CUDA 13 侧是否需要配对更新（反之亦然）
改了公开 C/C++/Python API？    → 同步补文档、测试，并评估 ABI/API 兼容性
新算子消费图像？               → 默认同时支持 NHWC/HWC 与 NCHW/CHW；不适用则在 C 头
                                 Limitations 里声明 "Planar image layouts: Not applicable" + Reason
```

#### 4.5.3 源码精读

[AGENTS.md:71-74](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/AGENTS.md#L71-L74) 规定 SPDX 头规则——本讲引用过的每个源文件开头那十几行注释就是它的实例（比如 [src/cvcuda/OpGaussian.cpp:1-16](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/OpGaussian.cpp#L1-L16)）。

[AGENTS.md:75-80](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/AGENTS.md#L75-L80) 规定 requirements 文件的生成式管理：`tests/`、`bench/`、`samples/`、`docker/`、`docs/` 下的 `requirements.*.txt` 都由 `.template` 加 `versions.env` 生成。对照 [tests/README.md:58](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/README.md#L58) 的警告「do not edit them directly」——两处文档口径一致。

[AGENTS.md:96-106](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/AGENTS.md#L96-L106) 给出验证命令阶梯（`generate_requirements.sh --check`、`pre-commit`、cmake preset、`run_tests.sh`），其中 `run_tests.sh cvcuda,cpp` 与 `run_tests.sh cvcuda,python` 的套件过滤语法在 u7 单元会反复用到。

#### 4.5.4 代码实践

1. **实践目标**：体会「生成式 requirements」与 SPDX 检查的真实运作。
2. **操作步骤**：
   ```bash
   # 1) 看 samples 的模板与生成物成对出现
   ls samples/requirements.samples.cu12.* samples/requirements.samples.hello_world_cu12.*
   # 2) 对照模板与生成物的差异（生成物多出版本号）
   head -20 samples/requirements.samples.hello_world_cu12.template
   head -5 samples/requirements.samples.hello_world_cu12.txt
   # 3) 跑仓库自带的生成检查（只读校验，不改文件）
   bash generate_requirements.sh --check
   ```
3. **需要观察的现象**：`.template` 里是未钉版本的依赖描述，`.txt` 里是带具体版本号的成品；`--check` 会报告生成物与模板是否一致。
4. **预期结果**：理解为什么改依赖要改模板而不是改 `.txt`。若 `--check` 因环境缺工具而失败，记下缺失项即可（待本地验证）。

#### 4.5.5 小练习与答案

**练习 1**：你在 2026 年新增了一个 Python 脚本，SPDX 头应该怎么写年份？
**答案**：`Copyright (c) 2026`，单独一年，不写成 `2025-2026` 区间（[AGENTS.md:73-74](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/AGENTS.md#L73-L74)）。

**练习 2**：同事让你「顺手把 `bench/requirements.bench.xxx.txt` 里的 numpy 版本改高一点」，正确做法是什么？
**答案**：拒绝直接改 `.txt`。应修改对应的 `.template`（或 `versions.env`），然后运行 `bash generate_requirements.sh` 重新生成，否则下次生成会覆盖手改。

**练习 3**：你给某算子新增了只支持 NCHW 的分支，需要额外做什么？
**答案**：按 [AGENTS.md:83-86](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/AGENTS.md#L83-L86)，图像类算子默认要同时支持 interleaved 与 planar 布局；若确实不适用，必须在该算子公开 C 头的 Limitations 契约中声明 `Planar image layouts: Not applicable` 并给出 Reason。

## 5. 综合实践

**任务：制作你的「Gaussian 算子个人速查表」。**

这是本讲的收官实践——把 4.2 到 4.4 学到的定位法全部用在 Gaussian 算子上，产出一张可以长期贴在手边的表。下面的参考答案已经过逐条验证，你可以先自己找，再对照。

1. **实践目标**：从算子名 `Gaussian` 出发，穷举它在仓库各层的全部相关文件。
2. **操作步骤**：
   ```bash
   cd CV-CUDA 仓库根目录
   find src python tests bench samples docs -iname "*gaussian*" | sort
   # 注意结果里混入了 GaussianNoise 的文件——先按 gaussian 过滤再剔除噪声算子：
   rg --files | rg -i gaussian | rg -iv 'gaussiannoise|gaussian_noise'
   # 内核不在上面结果里！按类名搜：
   rg -ln "Gaussian::infer|class Gaussian" src/cvcuda/priv/legacy/
   ```
3. **需要观察的现象**：直接按文件名搜会漏掉内核（在 `filter.cu`）并混入噪声算子（`gaussian_noise*.cu`）；按类名搜才能补全最后一块拼图。
4. **预期结果**——参考答案速查表：

| # | 层 | 文件路径 |
|---|---|---|
| 1 | C API 声明 | `src/cvcuda/include/cvcuda/OpGaussian.h` |
| 2 | C++ 类声明 | `src/cvcuda/include/cvcuda/OpGaussian.hpp` |
| 3 | C API 实现 | `src/cvcuda/OpGaussian.cpp` |
| 4 | priv 实现 | `src/cvcuda/priv/OpGaussian.cpp` / `OpGaussian.hpp` |
| 5 | legacy 内核（固定批） | `src/cvcuda/priv/legacy/filter.cu`（类声明在 `legacy/CvCudaLegacy.h:1413`） |
| 6 | legacy 内核（变长批） | `src/cvcuda/priv/legacy/filter_var_shape.cu` |
| 7 | Python 绑定 | `python/mod_cvcuda/operators/OpGaussian.cpp`（经 `Operators.hpp` 声明、`Main.cpp` 注册） |
| 8 | C++ 系统测试 | `tests/cvcuda/system/TestOpGaussian.cpp` |
| 9 | Python 测试 | `tests/cvcuda/python/test_opgaussian.py` |
| 10 | C++ 基准 | `bench/cpp/ops/BenchGaussian.cpp` |
| 11 | Python 基准 | `bench/python/ops/bench_gaussian.py` |
| 12 | 基准配置与基线 | `bench/config/operators/gaussian.json` |
| 13 | 示例 | `samples/operators/gaussian.py` |
| 14 | 示例文档 | `docs/sphinx/samples/operators/gaussian.rst` |

   做完后再挑一个你感兴趣的算子（如 `CLAHE` 或 `PillowResize`）独立复现这张表，检验方法是否真正内化。

## 6. 本讲小结

- **官方导航在 `AGENTS.md`**：九大顶层目录的职责表、权威文档索引、仓库不变量都集中在这一个文件，`CLAUDE.md` 只是它的符号链接。
- **算子前四层靠命名直推**：`include/cvcuda/OpXxx.h/.hpp` → `src/cvcuda/OpXxx.cpp` → `priv/OpXxx.cpp`；第五层 CUDA 内核要靠 `rg` 搜类名，因为 `priv/legacy/` 的 57 个 `.cu` 文件按功能族（如 `filter.cu`）而非算子名组织。
- **Python 绑定分两条产品线**：`operators/OpXxx.cpp`（61 个，与 CMakeLists 登记数一致）暴露算子，`nvcv/` 绑定数据类型；导出链路是「OpXxx.cpp 定义 → Operators.hpp 声明 → Main.cpp 注册」，Python 函数名是 snake_case（`gaussian_into`）。
- **三套外围代码组织各异**：tests 回答对错（`TestOpXxx.cpp` / `test_opxxx.py` 全小写连写），bench 回答快慢（`BenchXxx.cpp` / `bench_xxx.py` 成对出现并互校性能），samples 回答用法（按 operators/applications/interoperability 分类，`docs_tag` 注释与 Sphinx 文档联动）。
- **仓库不变量是硬约束**：新文件必须带 SPDX 头；requirements `.txt` 是生成物不可手改；CUDA 12/13 改动要配对；公开 API 改动要同步文档与测试。
- **检索工具用 `rg`**：`rg --files` 列文件、`rg "类名::方法"` 定位实现，是本仓库官方推荐的工作方式。

## 7. 下一步学习建议

本讲之后，第一单元（初识 CV-CUDA）就完整了：你已经知道项目是什么（u1-l1）、怎么跑起来（u1-l2）、怎么构建（u1-l3）、怎么找文件（本讲）。接下来：

- **第二单元 u2-l1（张量模型）**：带着刚学的地图进入 `src/nvcv/src/include/nvcv/Tensor.hpp`，那是 CV-CUDA 数据层的核心，也是你第一次精读一个 `src/nvcv` 头文件。
- **提前预习第五单元 u5-l1（算子四层结构）**：本讲 4.2 只讲了「文件在哪」，u5-l1 会沿 Gaussian/Flip 的调用链逐层讲「代码怎么流动」，两讲互为表里。
- **随手验证**：把综合实践的速查表换成你项目里最常用的算子再做一遍；等进入第七、八单元写测试和基准时，这张表就是你的提交前检查清单。
