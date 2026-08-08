# QuACK 是什么：项目定位与内核清单

## 1. 本讲目标

本讲是整本学习手册的**第一讲**，面向从未接触过 QuACK 的读者。读完本讲你应该能够：

- 用一句话说清楚 QuACK 是什么、解决什么问题；
- 记住 QuACK 的**目标硬件**（H100 / B200·B300 / RTX 50 三类 GPU）与**七个内核**；
- 区分「包名 `quack-kernels`」和「导入名 `quack`」，并理解它与 `nvidia-cutlass-dsl`（CuTe-DSL）的依赖关系；
- 知道 `from quack import rmsnorm, softmax, cross_entropy` 这条公开 API 是怎么来的。

本讲**不需要 GPU**，也不需要安装任何东西——它完全建立在对 README、`pyproject.toml` 和 `quack/__init__.py` 三个文件的阅读之上。

---

## 2. 前置知识

在开始前，下面几个名词最好先有个印象，看不懂也没关系，本讲会顺便解释：

- **CUDA 内核（kernel）**：运行在 NVIDIA GPU 上的小程序，传统的写法是用 C/C++（即 CUDA C++）。
- **CuTe-DSL**：NVIDIA 提供的一套工具，让你**用 Python 写 GPU 内核**，再编译成可以在 GPU 上跑的机器码。这里的「DSL」是「领域专用语言（Domain-Specific Language）」的缩写。
- **SM（Streaming Multiprocessor）**：GPU 内部的计算单元簇。不同代际的 GPU 有不同的架构代号和 SM 编号，例如 Hopper 架构是 SM90，Blackwell 架构是 SM100。
- **PyTorch / torch**：深度学习框架，QuACK 的内核最终要被 PyTorch 调用。
- **包（package）与 extras**：Python 的打包概念。一个包可以有「可选依赖」（extras），用户按需安装，例如 `pip install quack-kernels[dev]`。

> 提示：如果你还不知道「为什么 GPU 矩阵乘法需要专门优化」，可以先记住一句话——大模型训练里绝大多数算力都花在矩阵乘法（GEMM）和归约（norm/softmax）上，把它们写到极致快，就能省大量算力。QuACK 就是干这件事的。

---

## 3. 本讲源码地图

本讲只涉及三个文件，它们是理解 QuACK「是什么」的最小集合：

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/README.md) | 项目门面：定位、安装要求、内核清单、用法示例。 |
| [pyproject.toml](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/pyproject.toml) | 打包与依赖配置：包名、核心依赖、可选 extras、工具配置。 |
| [quack/\_\_init\_\_.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/__init__.py) | 包入口：版本号、启动期初始化、公开 API 导出。 |

---

## 4. 核心概念与源码讲解

### 4.1 QuACK 的定位：用 Python 写高性能 CUDA 内核

#### 4.1.1 概念说明

QuACK 的全称是 **Quirky Assortment of CuTe Kernels**（「CuTe 内核的古怪大杂烩」），是一组**高性能 GPU 内核**的集合。它最大的特点是：这些内核不是用 C/C++ 写的，而是用 NVIDIA 的 **CuTe-DSL**，也就是 **Python** 写的。

这在过去很难想象——传统上写 CUDA 内核要用 C++，要手写线程块、共享内存、寄存器分配等大量底层细节；后来有了 Triton 这类更高层的方案。而 CuTe-DSL 提供了第三条路：**在 Python 里用接近数学的写法描述张量布局和计算，由工具链编译成接近手写极限的 GPU 机器码**。QuACK 正是这套能力的实战成果，它把大模型里最吃性能的几个算子（归一化、softmax、矩阵乘）做到了「接近硬件理论上限」的水平。

一句话定位：**QuACK = 用 Python（CuTe-DSL）写的、面向最新 NVIDIA GPU 的高性能内核库，给 PyTorch 调用。**

#### 4.1.2 核心流程

从「Python 源码」到「GPU 上跑起来的内核」，QuACK 的高层路径是：

```text
你在 Python 里写 @cute.kernel / @cute.jit 函数
        │  （CuTe-DSL 通过读取源码文本来分析）
        ▼
编译期常量解析（const_expr）、张量布局推导
        │
        ▼
生成 CUDA / PTX 代码
        │
        ▼
编译成 .o 机器码（带缓存，避免重复编译）
        │
        ▼
PyTorch 调用时启动内核
```

注意这里有一个关键点（后续讲义会反复用到）：CuTe-DSL 依赖 **Python 源码文本** 来理解内核定义，所以内核代码必须落在真实的 `.py` 文件里，而不能随手写在交互式 REPL 里。本讲只需要知道「Python 写 → 编译 → GPU 跑」这条主线即可，细节留到后续讲义。

#### 4.1.3 源码精读

README 的开头三行就把 QuACK 的身份交代清楚了：

[README.md:L1-L3](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/README.md#L1-L3) —— 标题点明「这是一组 CuTe 内核」，并直接给出 [CuTe-DSL 文档链接](https://docs.nvidia.com/cutlass/latest/media/docs/pythonDSL/cute_dsl_general/dsl_introduction.html)，说明内核全部用 CuTe-DSL 编写。

含义：QuACK 的内核不是 CUDA C++，而是 CuTe-DSL（Python）。这是理解整个项目的基石。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：用自己的话讲清楚「用 CuTe-DSL 写内核」和「用 CUDA C++ 写内核」的区别。
2. **操作步骤**：
   - 打开 [README.md:L1-L3](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/README.md#L1-L3)，阅读标题和第一段。
   - 点击其中的 CuTe-DSL 文档链接，浏览导言部分（看不懂没关系，只看「它是什么语言」）。
3. **需要观察的现象**：README 没有出现任何 `.cu` / `__global__` / `cudaMalloc` 这类传统 CUDA C++ 的痕迹。
4. **预期结果**：你能写出一句类似「QuACK 用 Python（CuTe-DSL）描述内核，由工具链编译成 GPU 机器码，而不是手写 C++ CUDA」的话。
5. 本步骤不需要运行任何命令。

#### 4.1.5 小练习与答案

**练习 1**：QuACK 的内核是用什么语言写的？
**答案**：用 Python，具体说是 NVIDIA 的 CuTe-DSL。

**练习 2**：为什么说 CuTe-DSL 让 QuACK 的内核「接近手写极限」又「不用写 C++」？
**答案**：因为 CuTe-DSL 允许用 Python 高层地描述张量布局与计算，编译器再生成高度优化的 GPU 代码，兼顾了开发效率与运行性能。

---

### 4.2 目标硬件与内核清单

#### 4.2.1 概念说明

QuACK 不是「能在所有 GPU 上跑」的通用库，而是**针对最新几代 NVIDIA GPU 精心优化**的。README 在「Requirements」里明确列出了三类目标硬件，并在「Kernels」里列出了七个内核。

理解目标硬件很重要，因为 QuACK 的内核会**按 GPU 架构（SM 编号）选择不同的实现**——同一类算子（比如矩阵乘）在 Hopper 和 Blackwell 上用的是不同的指令和策略。这也是为什么后面会看到 `gemm_sm90.py`、`gemm_sm100.py`、`gemm_sm120.py` 这样的文件名。

#### 4.2.2 核心流程

把「GPU 型号 → 架构代号 / SM 编号 → 对应的内核文件」串起来，是这样的对应关系：

| GPU 型号 | 架构代号 | SM 编号 | QuACK 中对应的 GEMM 文件 |
| --- | --- | --- | --- |
| H100 | Hopper | SM90 | `quack/gemm_sm90.py` |
| B200 / B300 | Blackwell（数据中心） | SM100 | `quack/gemm_sm100.py` |
| RTX 50（GeForce） | Blackwell（消费级） | SM120 | `quack/gemm_sm120.py` |

QuACK 提供的七个内核（README 顺序）：

1. RMSNorm 前向 + 反向（`quack/rmsnorm.py`）
2. Softmax 前向 + 反向（`quack/softmax.py`）
3. Cross entropy 前向 + 反向（`quack/cross_entropy.py`）
4. Layernorm 前向 + 反向（`quack/` 中对应实现）
5. Hopper GEMM + epilogue（`quack/gemm_sm90.py`）
6. Blackwell GEMM + epilogue（`quack/gemm_sm100.py`）
7. Blackwell GeForce GEMM + epilogue（`quack/gemm_sm120.py`）

> 说明：第 4 项 Layernorm 与 RMSNorm 共享归约基础设施（后续讲义会讲 `reduction_base.py`）；第 5–7 项本质是「矩阵乘」在不同架构上的三种实现。文件名来自实际仓库目录，可在本讲的「综合实践」里亲自核对。

#### 4.2.3 源码精读

README 的「Requirements」和「Kernels」两节给出了权威清单：

[README.md:L25-L39](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/README.md#L25-L39) —— 这段同时写明了三点：
- **目标 GPU**：`H100, B200/B300, or RTX 50 GPU`（三类硬件）；
- **CUDA 工具链**：`CUDA toolkit 12.9+`；
- **Python 版本**：`Python 3.12`；
- 紧接着是七个内核的清单（带 🐥 小鸭图标）。

注意：README 这里写的 Python 版本是 `3.12`，而 `pyproject.toml` 里写的最低版本是 `>=3.10`（见 4.3.3）。两者并不矛盾——README 给的是**推荐/测试环境**，`pyproject.toml` 给的是**打包层面允许的最低门槛**。读源码时遇到这种差异要留心，以实际文件为准。

#### 4.2.4 代码实践（源码阅读型）

1. **实践目标**：把七个内核和它们的源码文件一一对应起来。
2. **操作步骤**：
   - 列出仓库根目录下 `quack/` 里的文件（例如用 `ls quack/` 或在 GitHub 上浏览 `quack/` 目录）。
   - 对照上面 4.2.2 的表格，为每个内核找到对应的 `.py` 文件。
3. **需要观察的现象**：你会看到 `rmsnorm.py`、`softmax.py`、`cross_entropy.py`、`gemm_sm90.py`、`gemm_sm100.py`、`gemm_sm120.py` 等文件确实存在。
4. **预期结果**：你能填出一张「内核名 → 文件名」的对照表。
5. 本步骤不需要 GPU，纯文件浏览即可。

#### 4.2.5 小练习与答案

**练习 1**：QuACK 的三类目标 GPU 分别对应哪个 SM 编号？
**答案**：H100→SM90（Hopper），B200/B300→SM100（Blackwell 数据中心），RTX 50→SM120（Blackwell 消费级）。

**练习 2**：为什么矩阵乘会有 `gemm_sm90.py` / `gemm_sm100.py` / `gemm_sm120.py` 三个文件？
**答案**：因为不同架构的 GPU 有不同的硬件指令（如不同的 MMA/TMA 指令），QuACK 为每代架构写了专门优化的实现，按 SM 编号分发。

---

### 4.3 包名、依赖与可选 extras

#### 4.3.1 概念说明

这里有一个初学者最容易踩的小坑：**PyPI 上的包名是 `quack-kernels`，但在 Python 里 `import` 时用的是 `quack`。**

```bash
pip install quack-kernels   # 安装时用包名（带连字符）
```

```python
import quack                # 导入时用模块名（下划线/单词）
```

这种「分发名 ≠ 导入名」在 Python 生态里很常见，`pyproject.toml` 里的 `[tool.setuptools.packages.find]` 配置就说明了「真正被打包的目录是 `quack` 及其子包」。

QuACK 的**核心依赖**是 `nvidia-cutlass-dsl`（即 CuTe-DSL 本体）和 `torch`——前者提供「Python 写内核并编译」的能力，后者提供「被深度学习框架调用」的入口。没有 CuTe-DSL，QuACK 一行内核都编译不出来。

#### 4.3.2 核心流程

安装时按需选择 extras（可选依赖组）：

```text
pip install quack-kernels           → 基础安装（CUDA 12.9 路径）
pip install quack-kernels[cu13]     → 切换到 CUDA 13.x 路径
pip install quack-kernels[heuristics]→ 额外装 NVIDIA 矩阵乘启发式（更好的默认配置）
pip install quack-kernels[jax]      → 额外装 JAX 绑定
pip install quack-kernels[dev]      → 开发者工具（pytest、ruff、pre-commit）
```

每个 extra 对应 `pyproject.toml` 里的一行，决定多装哪些包。

#### 4.3.3 源码精读

[pyproject.toml:L5-L15](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/pyproject.toml#L5-L15) —— 这里定义了：
- `name = "quack-kernels"`：**PyPI 包名**（注意和导入名 `quack` 不同）；
- `requires-python = ">=3.10"`：打包层面允许的最低 Python；
- 核心依赖里第一个就是 `nvidia-cutlass-dsl==4.7.0`（CuTe-DSL，**精确版本锁定**），其次是 `torch`、`apache-tvm-ffi`（FFI 调用层）、`torch-c-dlpack-ext`、`einops`。

[pyproject.toml:L17-L27](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/pyproject.toml#L17-L27) —— 定义了五组可选 extras：
- `cu13`：CUDA 13.x 专用版本的 CuTe-DSL；
- `heuristics`：`nvidia-matmul-heuristics`，给 GEMM 更好的默认配置；
- `jax`：`jax` + `jax-tvm-ffi`，提供 JAX 绑定；
- `bench`：`pandas` + `tyro`，用于跑基准测试；
- `dev`：`pre-commit` / `pytest` / `pytest-xdist` / `ruff`，开发与测试工具链。

[pyproject.toml:L29-L34](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/pyproject.toml#L29-L34) —— 说明实际打包的目录是 `quack`（`include = ["quack*"]`），并且**版本号是动态的**，取自 `quack.__version__`（这就和下一节的 `__init__.py` 第一行对上了）。

#### 4.3.4 代码实践（源码阅读型）

1. **实践目标**：搞清楚每条安装命令到底多装了什么。
2. **操作步骤**：
   - 打开 [pyproject.toml:L17-L27](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/pyproject.toml#L17-L27)。
   - 对 README 安装小节里的四条 `pip install` 命令（基础 / cu13 / heuristics / jax），逐一说明它们分别会带入哪些额外包。
3. **需要观察的现象**：`cu13` 这一组用的是 `nvidia-cutlass-dsl[cu13]==4.7.0`，即「同一个 CuTe-DSL，但带 CUDA 13 标记」。
4. **预期结果**：你能复述「装 `[jax]` 会拉入 jax 和 jax-tvm-ffi」「装 `[dev]` 会拉入 pytest 和 ruff」等结论。
5. 本步骤不需要 GPU。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `pip install quack-kernels` 之后，代码里却要写 `import quack`？
**答案**：因为 PyPI 分发名（`quack-kernels`，带连字符）和 Python 模块名（`quack`）可以不同；`pyproject.toml` 里 `include = ["quack*"]` 表明真正打包的目录是 `quack`。

**练习 2**：QuACK 最关键的依赖是哪一个？为什么？
**答案**：`nvidia-cutlass-dsl`（CuTe-DSL）。因为 QuACK 的所有内核都用它来写、靠它编译成 GPU 机器码，没有它整个库就无法工作。注意它被锁定在精确版本 `4.7.0`。

---

### 4.4 公开 API 导出与版本

#### 4.4.1 概念说明

对使用者来说，QuACK 的「门面」非常小。README 的「Usage」一节就一句话：

```python
from quack import rmsnorm, softmax, cross_entropy
```

这三个名字（`rmsnorm`、`softmax`、`cross_entropy`）就是 QuACK 对外暴露的**公开 API**。它们都在 `quack/__init__.py` 里被导入，并列入 `__all__`。这个 `__init__.py` 还有两个职责：声明**版本号**，以及做一些**启动期初始化**（比如可选的 ptxas 补丁、CuTe 张量索引语法的猴子补丁）。

#### 4.4.2 核心流程

当你写下 `import quack` 时，`quack/__init__.py` 会按顺序执行：

```text
1. 设定 __version__ = "0.6.4"
2. import quack.dsl（顺带安装 Pythonic 张量索引语法）
3. 如果设置了环境变量 CUTE_DSL_PTXAS_PATH → 打 ptxas 补丁
4. 从子模块导入 rmsnorm / softmax / cross_entropy / RoundingMode
5. 把它们写进 __all__，对外公开
```

#### 4.4.3 源码精读

[quack/\_\_init\_\_.py:L1](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/__init__.py#L1) —— 版本号 `__version__ = "0.6.4"`，这正是 `pyproject.toml` 动态版本取值的地方（也和 git 历史里的「Bump to v0.6.4」一致）。

[quack/\_\_init\_\_.py:L5-L13](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/__init__.py#L5-L13) —— 启动期初始化：导入 `quack.dsl` 会顺带启用「Pythonic CuTe 张量索引」（用 `:` / `...` 这种切片语法）；如果设置了环境变量 `CUTE_DSL_PTXAS_PATH`，则在导入任何内核模块**之前**打一个 ptxas 补丁（用于替换内嵌的 ptxas）。注释强调补丁必须在实例化 CuTeDSL 之前打。

[quack/\_\_init\_\_.py:L18-L21](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/__init__.py#L18-L21) —— 从三个子模块导入三个内核函数，外加一个 `RoundingMode` 枚举：

```python
from quack.rmsnorm import rmsnorm
from quack.softmax import softmax
from quack.cross_entropy import cross_entropy
from quack.rounding import RoundingMode
```

[quack/\_\_init\_\_.py:L24-L29](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/__init__.py#L24-L29) —— `__all__` 明确把 `rmsnorm`、`softmax`、`cross_entropy`、`RoundingMode` 标记为对外公开的符号。这就是 README「Usage」里那条 `from quack import ...` 能成立的原因。

> 小贴士：注意 `__init__.py` 里**没有**直接导入 `gemm`。矩阵乘（GEMM）的公开入口在 `quack.gemm` 等模块里，需要时单独 `from quack.gemm import gemm` 即可；顶层 `quack` 包只「开箱即用」地暴露了三个最常用的归约类算子。

#### 4.4.4 代码实践（源码阅读 + 可选运行）

1. **实践目标**：验证版本号与公开导出来自哪里。
2. **操作步骤**：
   - 在 [quack/\_\_init\_\_.py:L24-L29](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/__init__.py#L24-L29) 中数一下 `__all__` 有几个元素，分别是什么。
   - （**可选，需已安装环境**）如果本地已经装好 QuACK，可以在 Python 里运行：
     ```python
     import quack
     print(quack.__version__)   # 预期: 0.6.4
     print(quack.__all__)       # 预期: ['rmsnorm', 'softmax', 'cross_entropy', 'RoundingMode']
     ```
3. **需要观察的现象**：`__all__` 恰好包含四个名字，其中三个是内核函数，一个是 `RoundingMode`。
4. **预期结果**：版本号与 `pyproject.toml` 动态取值、git tag「v0.6.4」三者一致。如果未安装环境，则跳过运行步骤，仅通过阅读源码得出结论——**待本地验证**运行结果。
5. 注意：不要在没有 GPU / 未安装 CuTe-DSL 的环境里强行 `import quack`，可能因缺少依赖而报错；阅读源码即可完成本实践。

#### 4.4.5 小练习与答案

**练习 1**：`from quack import rmsnorm, softmax, cross_entropy` 这条语句能成功，根本原因是什么？
**答案**：因为 `quack/__init__.py` 在模块级别从子模块导入了这三个名字，并把它们写进了 `__all__`。

**练习 2**：`quack.__version__` 的值是从哪里来的？
**答案**：直接来自 `quack/__init__.py` 第 1 行的 `__version__ = "0.6.4"`；而 `pyproject.toml` 又通过 `version = {attr = "quack.__version__"}` 把它读走作为打包版本号。

---

## 5. 综合实践

把本讲的内容串起来，完成下面这个贯穿性小任务（纯阅读型，不需要 GPU）：

> **任务**：假设你要向一位同事用 5 分钟介绍 QuACK。请准备一张「一页纸速览」，包含以下内容：
>
> 1. **三类目标 GPU 与 SM 编号**：从 [README.md:L25-L39](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/README.md#L25-L39) 中提取 H100 / B200·B300 / RTX 50 三类硬件，并写出它们对应的 SM 编号（参考本讲 4.2.2 的表格）。
> 2. **七个内核**：照着 README 的「Kernels」小节抄下七个内核的名字，并为其中至少三个（RMSNorm / Softmax / Hopper GEMM）指出对应的源码文件。
> 3. **一句话依赖关系**：用一句话写清楚 `quack-kernels` 与 CuTe-DSL（`nvidia-cutlass-dsl`）的依赖关系。参考答案：「`quack-kernels` 依赖 `nvidia-cutlass-dsl`（CuTe-DSL），因为它的全部内核都用 CuTe-DSL（Python）编写、并由其编译成 GPU 机器码。」
> 4. **包名 vs 导入名**：在速览里提醒同事「安装用 `quack-kernels`，导入用 `quack`」，并指出公开 API 是 `rmsnorm / softmax / cross_entropy`（依据 [quack/\_\_init\_\_.py:L24-L29](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/__init__.py#L24-L29)）。

完成后，你就拥有了一份可以随时复习的「QuACK 项目身份卡」。

---

## 6. 本讲小结

- QuACK（**Quirky Assortment of CuTe Kernels**）是一组**用 Python（CuTe-DSL）编写**的高性能 GPU 内核，目标是接近硬件理论上限。
- 目标硬件是三类最新 NVIDIA GPU：**H100（SM90）/ B200·B300（SM100）/ RTX 50（SM120）**。
- 提供七个内核：RMSNorm、Softmax、Cross entropy、Layernorm（前四个为归约类），以及 Hopper / Blackwell / Blackwell-GeForce 三种 GEMM。
- **包名 `quack-kernels` ≠ 导入名 `quack`**；最核心的依赖是 `nvidia-cutlass-dsl==4.7.0`（CuTe-DSL）。
- 顶层公开 API 很小：`from quack import rmsnorm, softmax, cross_entropy`，由 `quack/__init__.py` 的 `__all__` 定义，当前版本 `0.6.4`。
- 读源码要留意「文档值」与「打包值」的差异，例如 README 写 Python 3.12，而 `pyproject.toml` 写 `requires-python = ">=3.10"`。

---

## 7. 下一步学习建议

本讲只回答了「QuACK 是什么」。要真正上手，建议按以下顺序继续：

1. **下一讲 u1-l2（安装、构建与运行测试）**：学会 `pip install -e '.[dev]'`、用 `pytest` 跑单测、用 `-k` 过滤参数化用例，亲手让一个内核编译并跑起来。
2. **u1-l3（目录结构与模块地图）**：建立对 `quack/` 下各子包（`epilogue/`、`gemm_runtime/`、`blockscaled/` 等）的整体认识。
3. **u1-l4（CuTe-DSL 编程模型入门）**：理解 `@cute.jit` / `@cute.kernel`、`const_expr` 与控制流限制——这是读懂任何 QuACK 内核源码的前提。
4. 想先看「为什么 QuACK 快」的直觉，可以读 README 里链接的博客 [media/2025-07-10-membound-sol.md](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/media/2025-07-10-membound-sol.md)（内存受限内核如何逼近理论上限）。
