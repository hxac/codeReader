# 环境搭建与首次运行

## 1. 本讲目标

上一篇（u1-l1）我们建立了 TileOPs 的全局心智模型：它是一个建在 TileLang 之上的、Spec-driven 的 LLM GPU 算子库。本篇把这套模型落到地面——**让你的机器真的能跑起来**。读完本讲，你应当能够：

1. 对照官方前置依赖清单，判断自己手头的机器（CUDA 版本、GPU 架构、PyTorch / TileLang 版本）是否满足要求。
2. 用一条 `make install` 命令完成「依赖安装 + pre-commit 钩子」的完整开发环境搭建，并能用一条测试命令验证安装是否成功。
3. 照着 README 的 Quick Start 实例化 `GemmOp`、跑通第一个算子，并理解 `trans_b=True` 这一 NT 默认语义。
4. 用 PyTorch 作为「地面真值（ground truth）」验证算子输出的正确性。

本讲覆盖的最小模块：**前置依赖**、**`make install` 安装**、**Quick Start GEMM 示例**。

## 2. 前置知识

- **GPU 与 CUDA**：CUDA 是 NVIDIA 显卡的通用并行计算平台。TileOPs 的 kernel 会被编译成在 GPU 上跑的机器码，所以你必须有一张 NVIDIA GPU，并且系统里装了 CUDA Toolkit。
- **GPU 架构 / SM 版本**：每代 NVIDIA GPU 有一个「计算能力版本」，例如 Ampere 是 SM_80、Hopper 是 SM_90。TileLang/TileOPs 的很多优化是针对特定架构写的，目前 TileOPs 主攻 **Hopper（SM_90）**。
- **可编辑安装（editable install）**：`pip install -e .` 把包以「源码链接」方式装进 Python 环境，你改源码后无需重装即可生效，非常适合开发与阅读源码。
- **矩阵乘法的「转置」记法**：深度学习里 GEMM（通用矩阵乘）常用 `A @ Bᵀ` 这种「第二个矩阵先转置再相乘」的形式，因为权重常以 `[N, K]` 形式存放、计算时转置成 `[K, N]`。这种 `A @ Bᵀ` 在 cuBLAS/DeepGEMM 术语里叫 **NT 布局**（N = a 不转置、T = b 转置）。

> 本讲承接 u1-l1 建立的「Op(L2) / Kernel(L1) 双层分离」与「Spec-driven」认知，不再重复。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲怎么用 |
| --- | --- | --- |
| `README.md` | 项目门面。包含 Overview、Installation、Quick Start。 | 前置依赖清单、`make install`、Quick Start GEMM 示例的唯一权威来源。 |
| `pyproject.toml` | Python 包元数据。声明运行期依赖范围、Python 版本、可选的 `dev`/`bench` 依赖集合。 | 逐字段核对依赖版本范围，理解「为什么这样约束」。 |
| `Makefile` | 把常用命令封装成 `make <target>`。 | `make install` 到底替你做了哪几步。 |
| `tileops/ops/gemm.py` | `GemmOp` 与 `GemmFp8Op` 实现。 | Quick Start 里 `GemmOp()` 的 `trans_b` 默认值、调用流程。 |
| `tileops/ops/__init__.py` | 算子导出聚合。`from tileops.ops import GemmOp` 的入口。 | 确认 `GemmOp` 是公开 API。 |
| `tileops/ops/op_base.py` | `Op` 抽象基类。 | 看一眼 `__call__ → forward` 的调用契约。 |

## 4. 核心概念与源码讲解

### 4.1 前置依赖

#### 4.1.1 概念说明

TileOPs 是一个 **GPU 算子库**，不是纯 Python 包。它的 kernel 最终要被编译成 GPU 代码，因此依赖链比普通 Python 项目更长，可以分成三层：

- **系统层**：NVIDIA GPU 硬件 + CUDA Toolkit。
- **底座层**：PyTorch（提供 tensor 抽象、CUDA 张量分配）+ TileLang（把高层 kernel 描述编译成 GPU 代码的「底座」）。
- **本项目层**：`tileops` 本身，外加 `einops`（张量重排）、`pyyaml`（读 manifest）。

理解这三层很重要：当安装或运行出错时，问题往往出在**底座层**（PyTorch 与 CUDA 不匹配、TileLang 与 GPU 架构不匹配），而不是 TileOPs 本身。

#### 4.1.2 核心流程

判断环境是否就绪，按下面顺序自检：

1. **GPU 架构**：确认你的卡是 Hopper（SM_90）。非 Hopper 卡目前不在主支持范围内。
2. **CUDA Toolkit**：确认版本是 12.x。
3. **Python / PyTorch**：Python ≥ 3.10；PyTorch ≥ 2.1 且 < 2.11。
4. **TileLang**：≥ 0.1.9 且 < 0.2.0。
5. 任一项不满足，就需要先补齐底座，再装 TileOPs。

> 小贴士：CI（持续集成）只在一个固定组合上验证（见下文 README 里的「CI validates ...」注释）。本地组合只要落在范围内通常可用，但越是边缘的组合越可能踩到编译问题。

#### 4.1.3 源码精读

前置依赖清单写在 README 的 Prerequisites 小节：

[README.md:46-52](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/README.md#L46-L52) —— 官方前置依赖：Python ≥ 3.10、PyTorch ≥ 2.1 < 2.11（CI 验证 2.10）、CUDA 12.x、Hopper（SM_90）、TileLang ≥ 0.1.9 < 0.2.0（CI 验证 0.1.11）。

这些约束在 `pyproject.toml` 里有对应的机器可读声明：

[pyproject.toml:7-19](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/pyproject.toml#L7-L19) —— 运行期依赖与 Python 版本约束。注意三件事：

- `torch>=2.1.0,<2.11.0`、`tilelang>=0.1.9,<0.2.0`、`einops`、`pyyaml>=6.0` 是**运行期**依赖，用户 `pip install tileops` 时会被自动解析安装。
- 关于 `tilelang` 那段长注释（[pyproject.toml:9-14](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/pyproject.toml#L9-L14)）特别说明：这个范围是**兼容性范围**，不是一个精确固定版本；CI runner 里烤了一个特定的 tilelang 构建，CI 用 `--no-deps` 装 tileops 来对齐它。意思是——本地开发时，建议你**先按官方推荐方式装好 tilelang**，再装 tileops。
- `requires-python = ">=3.10"`（[pyproject.toml:19](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/pyproject.toml#L19)）。

`dev` 这一组可选依赖是开发期才需要的（lint / 测试），下一节讲 `make install` 时会用到：

[pyproject.toml:39-46](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/pyproject.toml#L39-L46) —— `dev` 额外依赖：`ruff`（lint）、`codespell`（拼写）、`pytest` + `pytest-xdist`（并行测试）、`pyyaml`。

#### 4.1.4 代码实践

**实践目标**：自检本机是否满足前置依赖。

**操作步骤**（在已激活的 Python 环境里执行）：

```bash
python --version                  # 期望 >= 3.10
python -c "import torch; print(torch.__version__, torch.version.cuda, torch.cuda.get_device_capability())"
python -c "import tilelang; print(tilelang.__version__)"
```

**需要观察的现象**：

- `torch.version.cuda` 应为 `12.x`。
- `torch.cuda.get_device_capability()` 返回二元组，Hopper 是 `(9, 0)`（即 SM_90）。
- `tilelang.__version__` 落在 `[0.1.9, 0.2.0)` 区间。

**预期结果**：四项全部命中范围。任一项不符，先回去补齐底座再继续。

> 如果本机没有 GPU 或不满足架构要求，后续 4.2/4.3 的「运行型实践」无法完成；可改为「源码阅读型实践」（只读不跑），下同。

#### 4.1.5 小练习与答案

**练习 1**：为什么 README 同时给出「`PyTorch >= 2.1, < 2.11`」和「CI validates 2.10」两条信息？它们矛盾吗？

**参考答案**：不矛盾。前者是**允许的兼容性范围**（用户可在此区间内任选），后者是**CI 实际验证的那个固定版本**。落在范围内但不是 CI 验证版本的组合「大概率可用但无保证」。

**练习 2**：`pyproject.toml` 里 `tilelang` 的依赖声明后面为什么跟了一大段注释，而不是直接写一个精确版本？

**参考答案**：因为 tilelang 在 CI 里是由 runner 镜像烤好的特定构建、并用 `--no-deps` 对齐的；这里只声明一个兼容范围给下游 `pip install tileops` 自动解析用，刻意不固定版本。这也提示本地开发者：最好先单独把 tilelang 装好，再装 tileops。

---

### 4.2 `make install` 安装

#### 4.2.1 概念说明

TileOPs 目前处于活跃开发期，**只能从源码安装**（README 明确说 PyPI 发布要等首个稳定版）。源码安装的「标准入口」是一条命令：`make install`。它不是魔法——`make` 只是去 `Makefile` 里找名叫 `install` 的目标（target），把目标里写好的那几条 shell 命令依次跑一遍。

之所以用 `make` 而不是让大家手敲一长串命令，是为了**统一开发体验**：无论是人还是 AI agent，只要记住 `make install`，就能拿到一个带依赖、带 lint 钩子的完整开发环境。

#### 4.2.2 核心流程

`make install` 实际做了两步：

1. `pip install -e '.[dev]' -v` —— 以**可编辑模式**安装 `tileops`，并带上 `[dev]` 这一组可选依赖（ruff / codespell / pytest 等）。`-v` 打开详细日志，方便看到编译过程。
2. `pre-commit install` —— 把项目定义的 git pre-commit 钩子装进本地 `.git/`，此后每次 `git commit` 会自动跑 lint / 拼写 / 密钥扫描（项目 CLAUDE.md 与 `.claude/rules/security.md` 明确不允许绕过 gitleaks）。

安装完，用一条 pytest 命令验证：`python -m pytest tests/ -q`。这条命令**需要 CUDA GPU**——因为很多测试真的要跑 kernel。

> 边界情况：如果 CUDA 和 TileLang 已经在系统层面装好，直接 `make install` 偶尔会因构建隔离问题失败。README 给了备用命令（见源码精读），关闭构建隔离即可。

#### 4.2.3 源码精读

README 的安装说明：

[README.md:42-60](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/README.md#L42-L60) —— 安装定位（从源码安装、需 CUDA GPU）、Prerequisites、`git clone` + `make install` 三步。

[README.md:62-64](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/README.md#L62-L64) —— 当 CUDA/TileLang 已系统级安装却遇到构建问题时的备用命令：`PIP_NO_BUILD_ISOLATION=1 pip install -e '.[dev]' -v && pre-commit install`。

[README.md:66-70](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/README.md#L66-L70) —— 验证命令：`python -m pytest tests/ -q`（需 CUDA GPU）。

对应的 `Makefile` 目标定义：

[Makefile:3-5](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/Makefile#L3-L5) —— `install` 目标就两行：`pip install -e '.[dev]' -v` 与 `pre-commit install`，与 README 完全一致。

如果你想跑基准（对照 flash-attn / vllm 等基线），还有更强的安装目标：

[Makefile:7-9](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/Makefile#L7-L9) —— `install-bench` 目标：在 `[dev,bench]` 基础上额外装 `flash-attn`、`vllm`、`flashinfer` 等基线库（对应 [pyproject.toml:47-54](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/pyproject.toml#L47-L54) 的 `bench` 可选依赖），并拉取 native-sparse-attention。本讲用不到，知道存在即可。

`make help` 列出了所有可用目标：

[Makefile:32-43](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/Makefile#L32-L43) —— `help` 打印的目标清单（`install` / `install-bench` / `lint` / `test` / `test-smoke` / `test-full` / `test-nightly` / `bench` / `clean` / `help`）。

补充一个常被忽略的点：可编辑安装会把 `manifest/*.yaml` 和一个 C 头文件一起打包进包数据——

[pyproject.toml:64-65](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/pyproject.toml#L64-L65) —— `package-data` 包含 `tileops/kernels/moe/_atomic_helper.h` 与 `manifest/*.yaml`。这就是为什么即使从 wheel 安装，manifest 规约也能被运行时读到。

#### 4.2.4 代码实践

**实践目标**：完成安装并通过最小验证。

**操作步骤**：

```bash
git clone https://github.com/tile-ai/TileOPs
cd TileOPs
make install                       # 依赖 + pre-commit 钩子
python -m pytest tests/ -q         # 验证（需 CUDA GPU）
```

**需要观察的现象**：

- `make install` 末尾出现 `pre-commit installed at ...`，说明钩子装好。
- 首次 `import tileops` 不报错；首次跑测试时，部分 kernel 会触发 JIT 编译，所以**第一次会比后续慢很多**，这是正常的（TileLang 的 compile-on-first-call 行为）。

**预期结果**：测试通过（具体数量以本机为准）。如果 `make install` 因构建隔离失败，改用 README 给的 `PIP_NO_BUILD_ISOLATION=1 pip install -e '.[dev]' -v && pre-commit install`。

> 待本地验证：测试用例总数与通过数依赖本机 GPU 与已实现算子数量，无法在此预判。

#### 4.2.5 小练习与答案

**练习 1**：`pip install -e '.[dev]'` 里的 `-e` 和 `[dev]` 分别什么作用？去掉它们会怎样？

**参考答案**：`-e` 是 editable（可编辑）模式，包以源码链接方式安装，改源码即时生效，非常适合阅读/开发；`[dev]` 是可选依赖组，额外装上 ruff / pytest 等。去掉 `-e` 会变成「拷贝式」安装，改源码不生效；去掉 `[dev]` 则不会装 lint/测试工具，`make lint` 与 `make test` 会因缺 pytest 等而失败。

**练习 2**：为什么 README 的验证命令特意标注「requires a CUDA GPU」？

**参考答案**：因为 `tests/` 里的测试不是纯逻辑单测，而是真的会调用 TileLang 把 kernel 编译并跑在 GPU 上、再与 PyTorch 参考结果比对。没有 GPU 就连编译/运行这一步都过不去。

---

### 4.3 Quick Start：用 GemmOp 跑通第一个算子

#### 4.3.1 概念说明

装好之后，第一个能跑的算子是 **GEMM**（通用矩阵乘）。README 的 Quick Start 用 `GemmOp` 演示了 TileOPs 的三个核心调用习惯：

1. **从 `tileops.ops` 导入算子类**（`from tileops.ops import GemmOp`），这是公开 API 的统一入口。
2. **构造时不指定形状/dtype**——`gemm = GemmOp()`。形状与 dtype 是**调用时**才从输入张量推断的，不是构造时固定的。这呼应 u1-l1 讲过的「input-inferred」属性。
3. **直接像函数一样调用**——`d = gemm(a, b)`。这背后是 `Op.__call__ → forward` 的代理（见源码精读）。

最容易踩坑的点是 **`trans_b=True` 这一 NT 默认语义**：默认情况下，`b` 被**当作转置后的权重**存放，所以 `gemm(a, b)` 计算的是 `a @ b.T`，而不是 `a @ b`。这与 DeepGEMM / cuBLAS 的 NT 约定一致，是 LLM 推理里权重存放的常见布局。

#### 4.3.2 核心流程

`GemmOp` 的一次调用大致经历：

1. `__init__`：记录 `trans_a`/`trans_b`（默认 `False`/`True`），调用 `dispatch_kernel` 安装默认 kernel 表，初始化空的 kernel 缓存。
2. 首次 `gemm(a, b)`：
   - 校验 dtype；
   - 由 `(trans_a, trans_b)` 与输入 shape 推断逻辑维度 `(m, n, k)`；
   - 按 `(m, n, k, dtype)` 选择并 **JIT 编译** kernel，结果存进缓存；
   - 跑 kernel，返回输出 `d`。
3. 后续相同 `(a.shape, b.shape, dtype)` 的调用：走**快路径**，跳过校验与推断，直接复用已编译的 kernel——这是基准测试/服务部署里的稳态行为。

GEMM 的逻辑维度推导（NT 默认情形）：

- 输入 `a = [M, K]`、`b = [N, K]`。
- 因为 `trans_a=False`，`a` 贡献 `m = a.shape[0] = M`、`k = a.shape[1] = K`。
- 因为 `trans_b=True`，`b` 贡献 `n = b.shape[0] = N`、`k = b.shape[1] = K`。
- 缩减维 `K` 两侧必须相等，输出 `d = [M, N]`，即 `a @ b.T`。

GEMM 的算术量（用来后面理解 roofline）：

\[
\text{FLOPs} = 2 \cdot M \cdot N \cdot K, \qquad \text{bytes} \approx (M\cdot K + N\cdot K + M\cdot N) \cdot \text{elem\_bytes}
\]

（系数 2 因为一次乘加算 2 个浮点操作。）

#### 4.3.3 源码精读

README 的 Quick Start 全文：

[README.md:72-87](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/README.md#L72-L87) —— 官方 GEMM 示例。注意三点：`gemm = GemmOp()`（构造期不传形状）、`b` 的第二维是 `K`（配合 `trans_b=True`）、注释明确 `d` 等于 `a @ b.T`。

`GemmOp` 的导出与构造：

[tileops/ops/__init__.py:47](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/__init__.py#L47) 与 [tileops/ops/__init__.py:157](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/__init__.py#L157) —— `GemmOp` 既被 `import` 也被列入 `__all__`，确认它是公开 API。

[tileops/ops/gemm.py:19-43](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/gemm.py#L19-L43) —— `GemmOp` 类文档字符串，列出四种 `(trans_a, trans_b)` 布局对应的数学含义，以及 `Example`。

[tileops/ops/gemm.py:45-66](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/gemm.py#L45-L66) —— `__init__`。关键默认值 `trans_a=False, trans_b=True`（即 NT 默认）。注意 `_kernel_cache` 初始为空，`m/n/k/dtype` 初始为 `None`——证明「形状/dtype 在首次 forward 时才绑定」，构造期什么都没提交。

`Op` 基类把 `__call__` 代理到 `forward`：

[tileops/ops/op_base.py:207-212](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/op_base.py#L207-L212) —— 基类 `forward` 默认抛 `NotImplementedError`（留给子类覆盖），`__call__` 只是 `return self.forward(*args, **kwargs)`。这就是为什么 `gemm(a, b)` 等价于 `gemm.forward(a, b)`。

`(m, n, k)` 的推断逻辑：

[tileops/ops/gemm.py:76-86](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/gemm.py#L76-L86) —— `_infer_mnk` 按 `trans_a`/`trans_b` 从 `a.shape`/`b.shape` 取出 `m/n/k`，并校验缩减维 `K` 两侧一致，否则抛 `ValueError`。

`forward` 的快路径：

[tileops/ops/gemm.py:121-146](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/gemm.py#L121-L146) —— 首次调用走完整的「校验→推断→选 kernel→编译→缓存」；之后只要 `(a.shape, b.shape, a.dtype)` 没变，就复用 `self._active`，跳过所有推断。这是稳态性能的关键。

#### 4.3.4 代码实践

**实践目标**：跑通 README Quick Start，并亲眼看一次「首次调用慢、第二次快」的 JIT 编译行为。

**操作步骤**：

```python
import time
import torch
from tileops.ops import GemmOp

M, N, K = 1024, 1024, 512
dtype = torch.float16

gemm = GemmOp()                                   # NT 默认：trans_b=True
a = torch.randn(M, K, device="cuda", dtype=dtype)
b = torch.randn(N, K, device="cuda", dtype=dtype) # 第二维是 K，配合 trans_b=True

torch.cuda.synchronize()
t0 = time.perf_counter(); d1 = gemm(a, b); torch.cuda.synchronize(); t1 = time.perf_counter()
t2 = time.perf_counter(); d2 = gemm(a, b); torch.cuda.synchronize(); t3 = time.perf_counter()

print("first  call (compile+run) s:", t1 - t0)
print("second call (cache hit)  s:", t3 - t2)
print("output:", tuple(d1.shape), d1.dtype)
```

**需要观察的现象**：第一次耗时应明显大于第二次（首次包含 TileLang 的 JIT 编译；第二次命中 `_active_sig` 快路径）。

**预期结果**：输出 shape `(1024, 1024)`、dtype `float16`。两次耗时差距的数量级**待本地验证**（取决于 GPU 与首次编译开销）。

#### 4.3.5 小练习与答案

**练习 1**：如果把示例里的 `b` 改成 `b = torch.randn(K, N, ...)`（即 `[K, N]` 而非 `[N, K]`），保持默认 `trans_b=True` 不变，会发生什么？

**参考答案**：`_infer_mnk` 会得到 `n = b.shape[0] = K`、`k_b = b.shape[1] = N`；若 `N != K`（如本例 1024 vs 512），会因 `k_a(=K) != k_b(=N)` 抛出 `GEMM contraction dim mismatch`。要让 `[K, N]` 的 b 正确工作，应构造 `GemmOp(trans_b=False)`（NN 布局），此时计算 `a @ b`。

**练习 2**：README 写 `d = gemm(a, b)  # equals a @ b.T`，但 `GemmOp` 内部并没有真的把 `b` 转置拷贝一份。它是怎么做到「数学上等价于 `b.T`」却又不付出转置拷贝代价的？

**参考答案**：通过 `trans_b` 这个**布局标志**。kernel 在编译期就知道 `b` 是按 `[N, K]` 行主存放的「转置权重」，于是直接用相应的 stride/访存模式读取，数学上等价于先转置再相乘，但物理上不发生数据搬运。这正是 NT 布局存在的意义。

---

## 5. 综合实践

把本讲的三个模块串起来：**确认环境 → 装好 → 跑通 → 用 PyTorch 当地面真值验证正确性**。

任务：写一段脚本，用 `GemmOp()` 计算 \( a @ b^{\top} \)，再与 `torch.matmul(a, b.T)` 比对，打印最大绝对误差，并对结果做一次正式的容差断言。

```python
# 示例代码（非项目原有代码，为本讲实践撰写）
import torch
from tileops.ops import GemmOp

M, N, K = 1024, 1024, 512
dtype = torch.float16

gemm = GemmOp()                                    # NT 默认：trans_b=True
a = torch.randn(M, K, device="cuda", dtype=dtype)
b = torch.randn(N, K, device="cuda", dtype=dtype)  # 第二维 = K

d = gemm(a, b)                                     # TileOPs 结果，等价于 a @ b.T
ref = torch.matmul(a, b.T)                         # PyTorch 地面真值

# 最大绝对误差
max_err = (d.float() - ref.float()).abs().max().item()
print(f"shape={tuple(d.shape)} dtype={d.dtype} max_abs_err={max_err:.3e}")

# fp16 下的正式容差断言（容差取值供参考，可按需收紧/放宽）
torch.testing.assert_close(d, ref, rtol=1e-3, atol=5e-4)
print("assert_close passed")
```

**操作步骤**：

1. 先完成 4.1 的环境自检、4.2 的 `make install`。
2. 把上面脚本存为 `run_gemm_check.py`，`python run_gemm_check.py` 运行。
3. 把 `dtype` 从 `torch.float16` 改成 `torch.bfloat16`，再跑一次，观察误差数量级是否变化。

**需要观察的现象 / 预期结果**：

- `shape=(1024, 1024)`、`dtype=float16`（或 bfloat16）。
- `max_abs_err` 应是一个很小的数（fp16 一般在 1e-3 ~ 1e-2 量级）。
- `assert_close` 通过即代表 TileOPs 的 GEMM 与 PyTorch 数值一致到 fp16 容差内。
- 具体误差数值**待本地验证**（受输入随机性与 GPU 影响），但「数量级很小、断言通过」是稳定预期。

> 进阶：试着调用 `gemm.eval_roofline()`（首次 forward 之后才有效，见 `GemmOp` 文档字符串 [tileops/ops/gemm.py:39-42](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/gemm.py#L39-L42)），它会返回 `(flops, bytes)`——这是后续 u7「Roofline 性能模型」的入口，本讲只需知道它存在。

## 6. 本讲小结

- TileOPs 的依赖分三层：**系统层**（Hopper GPU + CUDA 12.x）、**底座层**（PyTorch ≥2.1<2.11、TileLang ≥0.1.9<0.2.0）、**项目层**（einops、pyyaml）；出问题多半在底座层。
- 标准安装入口是 `make install`，它等价于 `pip install -e '.[dev]' -v` + `pre-commit install`，装出一个可编辑、带 lint/测试工具与 git 钩子的开发环境。
- 用 `python -m pytest tests/ -q` 验证安装，该命令**必须有 CUDA GPU**；首次运行会因 JIT 编译明显变慢。
- 算子的标准用法：`from tileops.ops import <Op>` → 构造（不传形状）→ `op(*inputs)` 调用；`__call__` 代理到 `forward`，形状/dtype 在调用时推断。
- `GemmOp` 默认 `trans_b=True`，即 **NT 布局**，`gemm(a, b)` 数学上等于 `a @ b.T`；kernel 通过布局标志直接读取「转置权重」，不做物理转置。
- kernel 按 `(m, n, k, dtype)` 懒编译并缓存；相同输入签名的后续调用走快路径，这是基准/服务场景的稳态。

## 7. 下一步学习建议

本讲只是「能跑起来」。要真正理解 `GemmOp` 那一行 `gemm(a, b)` 背后发生了什么，建议进入 **U2（Op 层：用户侧调度器）**：

- **u2-l1（Op 基类与生命周期）**：系统讲解 `Op` 抽象基类的 `dispatch_kernel` / `default_kernel_map` / `forward` / `__call__`，以及三个 codegen 契约方法。
- **u2-l4（跟读 GemmOp 完整链路）**：把本讲的 `GemmOp` 从 `__init__` 到 `forward` 完整跟一遍，包括 GEMV 快路径。

如果想先了解「形状/dtype 是怎么被规约下来的」，可以平行阅读 **u1-l4（算子的公开 API 与调用方式）**，它系统讲 `tileops.ops` 的导出聚合与可调用契约。

后续 **U3（Kernel 层）** 会下到 L1，看 TileLang kernel 到底怎么写；**U6（性能基准）** 则会把本讲末尾提到的 `eval_roofline()` 接到基准协议里去。
