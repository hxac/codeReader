# 环境搭建：安装、测试与代码检查

## 1. 本讲目标

学完本讲，你应该能够：

1. 从源码完成 OLMo-core 的开发安装（`pip install -e '.[all]'`），并理解 PyPI 包名 `ai2-olmo-core`、核心依赖与可选依赖（extras）之间的关系。
2. 读懂 `pyproject.toml` 中依赖声明与代码风格工具（black / isort / ruff / mypy / pytest）的配置段，理解「一个文件管全部工具链」的现代化 Python 项目组织方式。
3. 用 `pytest -v src/` 运行测试，说清楚 GPU 测试为什么在没有 GPU 的机器上会**自动跳过**而不是失败。
4. 使用 `make style` / `make checks` / `make style-check` / `make lint-check` / `make type-check` 保持代码风格一致。
5. （选读）理解 `src/Dockerfile` 为什么「只装依赖、不装源码」，它与训练集群上的启动流程如何配合。

## 2. 前置知识

### 2.1 pip 的可编辑安装与 extras

- **可编辑安装（editable install）**：`pip install -e .` 会把当前目录以「开发模式」装进 Python 环境。之后你修改源码，无需重新安装，`import olmo_core` 立刻生效。这对阅读源码、加打印日志、做小实验至关重要。
- **extras（可选依赖组）**：在 `pyproject.toml` 里写成 `pip install -e '.[dev]'`、`pip install -e '.[all]'` 的形式，方括号里是依赖组名字。它表示「安装本体 + 这一额外组里的依赖」。OLMo-core 的核心依赖只有 13 个包，flash-attn、wandb 等全部按功能拆进了不同 extras。

### 2.2 pytest 的三个基础概念

- **测试发现**：pytest 按约定收集文件名以 `test_` 开头（或 `_test` 结尾）的文件里、以 `test_` 开头的函数。
- **marker（标记）**：用 `@pytest.mark.xxx` 给测试打标签，之后可以用 `-m xxx` 或 `-m "not xxx"` 在命令行筛选。
- **skip（跳过）**：`pytest.skip("原因")` 把当前测试标记为 skipped（黄色 `s`），既不算通过也不算失败；`pytest.importorskip("模块名")` 在模块不可导入时整文件跳过。

### 2.3 make 与 Makefile

`make` 是一个历史悠久的任务运行器：Makefile 里每个「target（目标）」是一段有名字的命令，`make 目标名` 就执行它。项目用 Makefile 把「格式化、检查、构建镜像」等长命令包装成 `make style` 这样的短命令，保证所有开发者执行的是完全相同的命令。注意：Makefile 的命令行**必须以 Tab 开头**，这是初学者最容易踩的坑。

### 2.4 Docker 多阶段构建（选读）

Docker 镜像可以用「多阶段构建」：第一阶段（build stage）装好编译工具链并编译重型依赖，第二阶段（release stage）只把第一阶段的成品（如 `/opt/conda` 环境）复制到一个更精简的系统里，最终镜像不带编译器等临时产物。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `pyproject.toml` | 项目元数据、核心依赖、可选依赖组（extras）、black/isort/ruff/mypy/pytest 的全部工具配置 |
| `Makefile` | `style` / `checks` / `style-check` / `lint-check` / `type-check` / `docker-image` 等任务入口 |
| `src/Dockerfile` | 官方训练镜像的两阶段构建脚本（本讲选读，第九单元会展开） |
| `README.md` | 安装说明（先装 PyTorch，再装本库）与可选依赖清单 |
| `src/test/config_test.py` | Config 基础类的纯 CPU 单元测试，本讲实践的运行对象 |
| `src/test/conftest.py` | pytest 全局 fixture（如 `tiny_model`），理解测试共享设施 |
| `src/test/nn/moe/top16_native_test.py` | 一个典型的 GPU 测试：展示 `@pytest.mark.gpu` + 运行时 skip 的组合写法 |
| `.github/workflows/main.yml` | CI 如何在 CPU 机器与 GPU 集群上分别跑测试（`-m gpu` / `-m "not gpu"`） |
| `.github/actions/setup-python-env/action.yml` | CI 安装环境的复用步骤：先装 torch，再 `uv pip install -e .[all]` |

> 顺带纠正一个容易误解的点：仓库里测试目录是 `src/test/`（不是 `src/test/olmo_core/`）。它**镜像** `src/olmo_core/` 的结构：`src/olmo_core/config.py` 对应 `src/test/config_test.py`，`src/olmo_core/train/` 对应 `src/test/train/`。后续讲义找测试时都按这个映射来。

## 4. 核心概念与源码讲解

### 4.1 pyproject.toml：依赖与工具链的唯一事实来源

#### 4.1.1 概念说明

现代 Python 项目把「我是谁、我依赖什么、我的工具怎么配置」全部写在一个 `pyproject.toml` 里，取代过去散落的 `setup.py`、`requirements.txt`、`setup.cfg`、`.flake8` 等一堆文件。OLMo-core 的 `pyproject.toml` 是我们理解这个项目的第一入口，因为它回答了三个问题：

1. **最小依赖是什么？**——只有 numpy、torch、pyyaml 等基础包，说明库本体刻意保持轻量。
2. **哪些功能需要额外安装？**——每个 extras 组对应一块可选功能（beaker 部署、wandb 日志、float8、flash-attn 4……）。
3. **贡献代码前要过哪些检查？**——black/isort/ruff/mypy 的参数全部在这一文件里声明，与 Makefile 里的命令一一对应。

#### 4.1.2 核心流程

安装 OLMo-core 的推荐流程（来自 README）：

```text
1. 按自己的操作系统/硬件，先单独安装合适版本的 PyTorch（CPU 版或 CUDA 版）
2. git clone 仓库
3. pip install -e '.[all]'   ← 可编辑安装 + 全部可选依赖
   （或最小安装：pip install -e '.[dev]'，只要测试和风格工具）
```

为什么先装 torch？因为 `pyproject.toml` 对 torch 只约束版本区间（`>=2.10.0,<2.14`），如果让 pip 自己解析，它会装一个「默认后端」的 torch——在有 NVIDIA 显卡的机器上未必是你要的那个。先手动装好，后面 `pip install -e .` 看到 torch 已满足约束就不会再动它。

#### 4.1.3 源码精读

**① 包名与 Python 版本要求**

[pyproject.toml:L5-L13](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/pyproject.toml#L5-L13) 声明 PyPI 包名是 `ai2-olmo-core`（所以从 PyPI 安装是 `pip install ai2-olmo-core`），要求 Python ≥ 3.10。

**② 核心依赖只有 13 个包**

[pyproject.toml:L15-L29](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/pyproject.toml#L15-L29) 列出全部核心依赖：`numpy`、`torch>=2.10.0,<2.14`、`cached-path`、`filelock`、`requests`、`packaging`、`dataclass-extensions`、`pyyaml`、`rich`、`safetensors`、`importlib_resources`、`bettermap`、`pandas`。

注意这里**没有** flash-attn、grouped_gemm、torchao——它们是可选依赖。这印证了 u1-l1 的结论：OLMo-core 核心只依赖 PyTorch 生态，GPU 加速组件用 try/except 按需挂载，没装也能 `import olmo_core`（只是部分后端不可用）。

**③ dev 组：测试与风格工具全在这里**

[pyproject.toml:L36-L60](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/pyproject.toml#L36-L60) 定义了 `dev` extras，包含 `black`、`isort`、`mypy`、`ruff==0.15.9`、`pytest`、`pytest-xdist`（并行跑测试）、`pytest-memray`、Sphinx 文档工具链等。只想本地跑测试、不改镜像的话，`pip install -e '.[dev]'` 就够了。

**④ all 组：一键全量安装**

[pyproject.toml:L91](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/pyproject.toml#L91) 定义 `all = ["ai2-olmo-core[dev,beaker,comet,dion,eval,fa4,fla,profiling,torchao,transformers,wandb]"]`，即所有组的并集。注意 extras 引用自身的规范写法是「包名[组名]」。另外 [pyproject.toml:L75-L81](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/pyproject.toml#L75-L81) 的注释解释了一个精妙细节：`fla` 组强制 `torch>=2.13`，因此装了 `.[all]` 就隐式要求 torch ≥ 2.13——这是 extras 之间互相约束版本的实例。

**⑤ 工具配置段（与 Makefile 命令一一对应）**

- [pyproject.toml:L126-L127](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/pyproject.toml#L126-L127)：black 行宽 100。AGENTS.md 说的 "Line length: 100" 出处就在这里。
- [pyproject.toml:L143-L145](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/pyproject.toml#L143-L145)：isort 使用 `profile = "black"`，保证 isort 和 black 不互相打架。
- [pyproject.toml:L147-L151](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/pyproject.toml#L147-L151)：ruff 行宽 100，忽略 `F403`（星号导入）、`F405`（星号导入的未知名字）、`E501`（行超长，交给 black 管）。
- [pyproject.toml:L178-L179](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/pyproject.toml#L178-L179)：对 `**/__init__.py` 额外忽略 `F401`（未使用导入）——`__init__.py` 里「导入即再导出」是包的常用手法，不算错误。
- [pyproject.toml:L195-L200](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/pyproject.toml#L195-L200)：mypy 配置，`ignore_missing_imports = true`（无类型注解的第三方库不报警）。
- [pyproject.toml:L206-L213](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/pyproject.toml#L206-L213)：pytest 配置——`testpaths = "src/test/"` 让你在仓库根目录裸敲 `pytest` 也会自动去 `src/test/` 找测试；`markers = ["gpu"]` 注册了 `gpu` 标记（4.2 节的主角）。

**⑥ CI 中的安装顺序印证**

[.github/actions/setup-python-env/action.yml:L44-L48](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/.github/actions/setup-python-env/action.yml#L44-L48) 显示 CI 的安装顺序就是 `uv pip install setuptools build wheel` → `uv pip install torch==2.13.0 --torch-backend=cpu` → `uv pip install -e .[all]`，与 README 的推荐顺序完全一致。

#### 4.1.4 代码实践

**实践目标**：搞清楚「最小安装」和「全量安装」各自装了什么，建立对依赖体积的直觉。

**操作步骤**：

1. 创建并激活一个独立虚拟环境（避免污染系统 Python）：

   ```bash
   python -m venv .venv
   source .venv/bin/activate
   ```

2. 按 [README.md:L34-L42](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/README.md#L34-L42) 的说明，先安装 CPU 版 PyTorch（去 pytorch.org 查看你平台对应的命令）。

3. 先做**最小安装**：`pip install -e '.[dev]'`，安装完成后执行 `pip list | wc -l` 记录包总数，并执行 `pip show ai2-olmo-core` 确认安装成功。

4. 再升级为**全量安装**：`pip install -e '.[all]'`，再次执行 `pip list | wc -l` 对比增量（全量会额外装上 wandb、torchao、transformers 等）。

5. 快速验证可选依赖的「可选性」：在只装 `[dev]` 的环境里执行 `python -c "import olmo_core; print('ok')"`——即使没有 flash-attn，导入也应成功（若失败请记录报错，待本地验证）。

**需要观察的现象**：`.[all]` 比 `.[dev]` 多出几十个包；`import olmo_core` 在缺少 GPU 可选依赖时依然正常。

**预期结果**：`pip show ai2-olmo-core` 显示版本号和本地源码路径（Editable location）。具体包数量与安装耗时**待本地验证**（取决于网络与平台）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `pip install ai2-olmo-core`（PyPI 版）不适合用来给本项目贡献代码？

答案：PyPI 版安装的是某个发布快照，无法反映你的源码修改。开发必须用 `pip install -e .` 可编辑安装，改代码后立即生效；且贡献前需要 dev 组里的 black/ruff/mypy/pytest 等工具，PyPI 默认安装不带这些 extras。

**练习 2**：你在代码评审中看到一行 120 个字符的 import 语句（星号导入 `from x import *`）。按本项目配置，ruff 会因为「行太长」报错吗？

答案：不会因为行长报错——`E501` 在 [pyproject.toml:L151](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/pyproject.toml#L151) 被显式忽略（行格式交给 black 处理）。但星号导入对应的 `F403`/`F405` 没有被全局忽略，仍会报警——除非这行出现在 `__init__.py` 里（那里只额外豁免 `F401`，`F403` 依然要小心）。

**练习 3**：队友说「装了 `.[all]` 之后 torch 被升级到了 2.13，为什么？」

答案：`all` 包含 `fla` 组，而 `fla` 组声明了 `torch>=2.13.0,<2.14`（见 [pyproject.toml:L81](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/pyproject.toml#L81)）：flash-linear-attention 0.5.2 要求 triton ≥ 3.7.1，只有 torch 2.13 起的捆绑版本满足。所以 extras 之间会通过共同依赖互相抬升版本下限。

### 4.2 pytest：测试的组织与 GPU 测试自动跳过

#### 4.2.1 概念说明

OLMo-core 是训练库，大量测试需要真实 GPU（flash attention、分布式训练）。但开发者经常在笔记本/无 GPU 机器上工作。项目的解法分两层：

1. **标记层**：给 GPU 测试打 `@pytest.mark.gpu` 标记，CI 在 GPU 集群上用 `-m gpu` 只跑这些，在 CPU 机器上用 `-m "not gpu"` 排除这些。
2. **运行时跳过层**：测试函数体开头写 `if not torch.cuda.is_available(): pytest.skip("CUDA required")`。即使你裸跑 `pytest -v src/`（不筛选标记）把 GPU 测试也收集进来了，它们也会在无 GPU 机器上自己变成 skipped，而不是报错失败。

「GPU 测试无 GPU 时自动跳过」说的就是第二层——**标记本身不会跳过任何东西，跳过是测试体内的 `pytest.skip()` 干的**。

#### 4.2.2 核心流程

在仓库根目录执行 `pytest -v src/` 后：

```text
1. pytest 读取 pyproject.toml 的 [tool.pytest.ini_options]
   → testpaths = "src/test/"（显式传了 src/ 时用你传的路径）
   → 注册 markers = ["gpu"]，否则用未注册标记会警告
2. 递归收集 src/test/ 下所有 test_*.py / *_test.py 中的 test_* 函数
3. 每个文件按需取用 conftest.py 提供的 fixture（如 tiny_model）
4. 逐个执行：
   - 普通测试：直接跑断言
   - GPU 测试：函数体开头检测 torch.cuda.is_available()， False → pytest.skip()
5. 汇总输出：passed / failed / skipped 三类计数
```

#### 4.2.3 源码精读

**① pytest 配置：测试根目录与 gpu 标记**

[pyproject.toml:L206-L213](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/pyproject.toml#L206-L213) 设置 `testpaths = "src/test/"` 并注册 `gpu` 标记。紧跟其后的 [pyproject.toml:L214-L222](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/pyproject.toml#L214-L222) 用 `filterwarnings` 压掉了 huggingface_hub、torchao 等第三方库的已知警告，保证输出干净。

**② 一个标准 GPU 测试的样子**

[src/test/nn/moe/top16_native_test.py:L6-L12](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/test/nn/moe/top16_native_test.py#L6-L12) 展示了完整模式：

```python
top16_native_indices = pytest.importorskip("olmo_core.ops.moe_top16").top16_native_indices

@pytest.mark.gpu
def test_top16_native_contract():
    if not torch.cuda.is_available():
        pytest.skip("CUDA required")
```

三重防护：`importorskip` 在编译扩展缺失时跳过整个文件；`@pytest.mark.gpu` 供 CI 筛选；函数体内的 `pytest.skip("CUDA required")` 在无 GPU 环境优雅跳过。读懂这一个文件，就掌握了本项目所有 GPU 测试的写法约定。

**③ 纯 CPU 测试的样子（本讲实践对象）**

[src/test/config_test.py:L7-L15](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/test/config_test.py#L7-L15) 的 `test_simple_config_as_dict` 只测 Config dataclass 的序列化行为，不碰 torch、不需要 GPU——这就是适合拿来验证环境的最小测试。它断言 `as_dict()` 与 `as_dict(exclude_none=True)` 的输出，正好预告了下一单元的 Config 主题。

**④ conftest.py：测试共享的 fixture**

[src/test/conftest.py:L53-L75](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/test/conftest.py#L53-L75) 定义了一个三层小 MLP `TinyModel` 并通过 `tiny_model_factory` / `tiny_model` fixture 提供给所有测试文件使用。任何 `src/test/` 下的测试函数只要声明参数名叫 `tiny_model`，pytest 就会自动注入一个实例。这是 pytest「依赖注入式」fixture 的典型用法：测试数据准备写一次，处处复用。

**⑤ CI 如何分层跑测试**

[.github/workflows/main.yml:L46-L53](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/.github/workflows/main.yml#L46-L53) 是 CPU 上的主测试步骤（用 `--ignore-glob` 排开了几类重目录），而 [main.yml:L86](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/.github/workflows/main.yml#L86) 对集成测试用 `-m "not gpu"` 显式排除 GPU 测试；GPU 任务则相反，例如 [main.yml:L179](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/.github/workflows/main.yml#L179) 用 `-m gpu` 在 GPU 容器里只跑打了标记的测试。CPU 任务里那些被收集到的 gpu 测试靠 ② 的体内 skip 保持绿色。

#### 4.2.4 代码实践

**实践目标**：确认环境可用；亲眼看到「GPU 测试被跳过、CPU 测试通过」的输出形态。

**操作步骤**：

1. 确认已按 4.1.4 完成安装（至少 `.[dev]`），当前在仓库根目录。

2. 跑一个最小纯 CPU 测试文件（注意：真实路径是 `src/test/config_test.py`，不是 `src/test/olmo_core/config_test.py`）：

   ```bash
   pytest -v src/test/config_test.py
   ```

3. 按「关键词」筛选单个测试函数：

   ```bash
   pytest -v src/test/config_test.py -k 'nested'
   ```

4. 感受 GPU 测试的跳过机制（无 GPU 机器上；若有 GPU 它们会真跑，注意耗时）：

   ```bash
   pytest -v src/test/nn/moe/top16_native_test.py
   ```

5. （可选，全量较慢）复刻 AGENTS.md 给出的全量命令：

   ```bash
   pytest -v src/
   ```

   结束后看末尾汇总行里 `skipped` 的数量；也可以加 `--durations=6`（CI 同款参数）看最慢的 6 个测试。若嫌慢，dev 依赖里有 pytest-xdist，可加 `-n auto` 并行。

**需要观察的现象**：步骤 2 输出中每个 `test_*` 前面是绿色 `PASSED`；步骤 4 输出中出现 `SKIPPED (CUDA required)`（无 GPU 时）或 `PASSED`（有 GPU 时）；步骤 5 的汇总行形如 `X passed, Y skipped in Zs`。

**预期结果**：`src/test/config_test.py` 的 5 个测试全部通过（本文件恰好有 5 个 `test_` 函数）；全量 `pytest -v src/` 在无 GPU 机器上出现大量 skipped 而几乎没有因缺 GPU 而 failed 的项。具体数量**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：如果你在测试文件里写了 `@pytest.mark.gpu` 但忘了写体内的 `pytest.skip()`，在无 GPU 机器上裸跑 `pytest -v src/` 会发生什么？

答案：测试会被正常执行，函数体内第一句用到 CUDA 的操作（如 `torch.zeros(..., device="cuda")`）抛异常，pytest 判定为 **FAILED** 而不是 SKIPPED。标记只影响 `-m` 筛选，不提供任何自动跳过能力——跳过必须由 `torch.cuda.is_available()` 检查 + `pytest.skip()` 完成。

**练习 2**：`pytest -m "not gpu" src/` 和 `pytest src/`（后者配合体内 skip）最终都能得到「GPU 测试不执行」的结果，两者有什么区别？

答案：`-m "not gpu"` 在**收集筛选阶段**就把 gpu 测试排除，它们根本不会出现在输出里，也不计入 skipped 计数；裸跑则**执行到函数体开头**才跳过，输出里每个 gpu 测试占一行 `SKIPPED`。前者输出更干净、略快；后者能看见「哪些测试因缺硬件被跳过」，信息更全。CI 两种都用：CPU 集成测试用前者（[main.yml:L86](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/.github/workflows/main.yml#L86)），日常本地开发常用后者。

**练习 3**：看 [src/test/config_test.py:L38-L41](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/test/config_test.py#L38-L41)，`from_dict(data, overrides=["bar.x=0"])` 在测什么行为？

答案：在测 Config 的 **dotlist 覆盖**：用一个（或多个）`路径.字段=值` 形式的字符串在反序列化时覆盖嵌套字段。这是第 2 单元要展开的「命令行覆盖配置」机制的底层能力——官方脚本的 `--train_module.optim.lr=6e-3` 最终就是走这类 merge 逻辑。

### 4.3 Makefile：make 风格的检查工作流

#### 4.3.1 概念说明

Makefile 在这个项目里不做编译，纯粹是**任务快捷方式**：把「isort + black + ruff + mypy」这些名字和参数固化下来，贡献者只需记住 `make style` / `make checks` 两个命令。它的价值在于消除「每个人记得的检查命令不一样」导致的返工——所有参数的真实来源在 `pyproject.toml`，Makefile 只负责调用。

#### 4.3.2 核心流程

贡献一段代码前后的标准节奏：

```text
写代码 → make style（isort + black 自动改格式）→ 改到满意
       → make checks（style-check + lint-check + type-check，三连检）
       → 全绿 → 提交
任何一个 check 失败 → make style 通常能自动修掉格式类问题；
                      ruff/mypy 报的则要手动改代码
```

#### 4.3.3 源码精读

**① 总入口 checks**

[Makefile:L1-L2](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/Makefile#L1-L2) 声明 `checks` 依赖 `style-check lint-check type-check` 三个目标——make 会按顺序依次执行它们，任何一个失败即中断。

**② style-check：格式检查（只查不改）**

[Makefile:L4-L9](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/Makefile#L4-L9) 依次运行 `isort --check .` 和 `black --check .`。`--check` 表示只报告「哪些文件格式不符合」，不实际修改。

**③ lint-check 与 type-check**

[Makefile:L11-L14](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/Makefile#L11-L14) 运行 `ruff check .`（lint，规则见 4.1.3 ⑤）；[Makefile:L16-L19](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/Makefile#L16-L19) 运行 `mypy src/`（类型检查，只查 `src/` 目录）。

**④ style：自动格式化（会改文件）**

[Makefile:L21-L26](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/Makefile#L21-L26) 运行不带 `--check` 的 `isort .` 和 `black .`，**会直接改写你的源文件**。第一次用建议先 `git add .` 留底，方便 `git diff` 查看它改了什么。

**⑤ Makefile 也是 Docker/Beaker 镜像的唯一入口**

[Makefile:L132-L164](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/Makefile#L132-L164) 的 `docker-image` 目标把二十多个 `--build-arg`（CUDA 版本、torch 版本、flash-attn 版本……）传给 `src/Dockerfile`。这些版本变量的「家」在 [Makefile:L51-L108](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/Makefile#L51-L108)（如 `TORCH_VERSION = 2.13.0`、`CUDA_VERSION = 12.9.1`），改镜像时只需调变量，不必碰 Dockerfile 本体。第 9 单元会回到这里；本讲只需知道 `make checks` 之外 Makefile 还管着镜像构建。

另外注意 [Makefile:L114](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/Makefile#L114) 用 `python src/olmo_core/version.py` 反查版本号——`version.py` 支持 `__main__` 直接触发打印，是个可复用的小设计（`pyproject.toml` 的 dynamic version 也来自它）。

#### 4.3.4 代码实践

**实践目标**：跑通三连检，学会解读「检查失败」的输出。

**操作步骤**：

1. 确保环境里已装 `[dev]` 组（black/isort/ruff/mypy 都在其中）。

2. 在仓库根目录执行：

   ```bash
   make checks
   ```

3. 制造一个格式问题：随便挑一个 tutorial 之外的 python 文件（**不要**真改 `src/` 下源码，可以在 `/tmp` 建一个 `bad_style_demo.py` 复制进仓库根目录做实验，结束后删除），写一段明显不合 black 风格的代码，例如：

   ```python
   x={ "a":1,"b":2 }
   import os,sys
   ```

4. 依次运行并对比输出：

   ```bash
   make style-check   # 预期报 would reformat / 检测到不合 isort
   make lint-check    # 预期报 F401 之类的未使用导入（E501 不报）
   make style         # 自动修复格式
   make style-check   # 预期变绿
   rm bad_style_demo.py
   ```

**需要观察的现象**：`style-check` 失败时 isort/black 会列出文件名并说 `would reformat`；`lint-check` 对 demo 文件报未使用导入；`make style` 之后 demo 文件的 `{}` 内被加空格、多导入被拆行。

**预期结果**：干净的仓库上 `make checks` 三段全部通过（`running isort... / running black... / running ruff... / running mypy...` 无报错退出）。本仓库在 HEAD `075a7892` 下是否全绿**待本地验证**（mypy 对本地环境敏感）。

#### 4.3.5 小练习与答案

**练习 1**：`make style-check` 和 `make style` 的区别是什么？为什么 CI 里通常只跑前者？

答案：`style-check` 用 `--check` 只检测不修改，退出码非零表示有格式问题；`style` 会直接改写文件。CI 是只读检查（发现问题让人修），不能替开发者偷偷改代码，所以跑 check 版本；本地开发者则用 `style` 自动修复。

**练习 2**：执行 `make checks` 时输出顺序是什么？为什么？

答案：`style-check` → `lint-check` → `type-check`。因为 [Makefile:L2](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/Makefile#L2) 把 `checks` 的先决条件按这个顺序写出，make 依次执行，且前面失败会中断（不会出现 mypy 跑了但 black 没过的情况）。

**练习 3**：为什么 Makefile 中每个目标前面都要写 `.PHONY`？

答案：`.PHONY` 声明该目标是「伪目标」（纯任务，不产出同名文件）。若不声明而目录里恰好存在名为 `checks` 的文件，make 会认为「文件已存在且是最新的」而拒绝执行任务。这是 Makefile 的经典防御性写法。

### 4.4 src/Dockerfile：只装依赖、不装源码的镜像（选读）

#### 4.4.1 概念说明

pip 安装解决的是「个人开发环境」，Docker 镜像解决的是「整个团队/集群的可复现环境」。OLMo-core 官方镜像有个反直觉的设计：**镜像里故意不装 olmo-core 包本身**。原因写在 README 里——训练代码经常变，镜像构建一次很慢（要编译 flash-attn、TransformerEngine 等 CUDA 扩展），如果镜像绑定了某个代码版本，每次提交都要重建镜像。所以镜像只冻结「难装的依赖」，源码在启动任务时现场 clone + `pip install -e .`（第 9 单元讲 launch 流程时你会看到这四步）。

#### 4.4.2 核心流程

```text
build 阶段（CUDA devel 基础镜像，带编译器）：
  装 miniconda + Python → 装指定版本 CUDA torch
  → 从源码编译 grouped_gemm / flash-attn 2 / flash-attn 3 / TransformerEngine 等重型扩展
  → COPY 进 pyproject.toml + version.py，pip install '.[all]' 拉齐纯 Python 依赖
  → 立刻 pip uninstall -y ai2-olmo-core 并 rm -rf *（只留依赖，扔掉源码）
release 阶段（更轻的系统镜像）：
  装 AWS CLI / GCS SDK / MLNX OFED 驱动等运维工具
  → 从 build 阶段整体拷贝 /opt/conda 环境
```

#### 4.4.3 源码精读

**① build 阶段从 CUDA devel 镜像开始**

[src/Dockerfile:L13](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/Dockerfile#L13) `FROM ${DEVEL_BASE_IMAGE} AS build`——devel 镜像自带 nvcc 和 CUDA 头文件，是编译 CUDA 扩展的前提。

**② 安装指定 CUDA 版本的 torch**

[src/Dockerfile:L44-L49](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/Dockerfile#L44-L49) 用 `--index-url https://download.pytorch.org/${INSTALL_CHANNEL}/${CUDA_VERSION_PATH}/` 安装 torch——这正是 README「按硬件先装 torch」的镜像版：CUDA 版本在这里精确锁定。

**③ 重型 CUDA 扩展示例：flash-attn 2 与 TransformerEngine**

[src/Dockerfile:L80-L85](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/Dockerfile#L80-L85) 从源码编译 flash-attn 2（用 `--no-build-isolation`，编译架构由 `FLASH_ATTN_CUDA_ARCHS` 控制）；[src/Dockerfile:L142-L148](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/Dockerfile#L142-L148) 编译 TransformerEngine。这些就是「为什么本地不装它们也能学 OLMo-core，但要在真集群复现实验就必须用官方镜像」的答案。

**④ 关键三行：装完依赖、卸掉包、删掉源码**

[src/Dockerfile:L163-L171](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/Dockerfile#L163-L171)：只 `COPY pyproject.toml` 和两个版本文件进来，`pip install '.[all]'` 把纯 Python 依赖拉齐，然后 `pip uninstall -y ai2-olmo-core` + `rm -rf *` 把 olmo-core 自身和工作目录清空。这就是「镜像含依赖不含源码」的实现处。

**⑤ release 阶段：拷环境、设工作目录**

[src/Dockerfile:L235](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/Dockerfile#L235) 把 build 阶段的整个 `/opt/conda` 拷过来；[src/Dockerfile:L269](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/Dockerfile#L269) 设 `WORKDIR /app/olmo-core`——launch 时源码就 clone 到这里。

#### 4.4.4 代码实践

**实践目标**：不构建镜像，只做源码阅读——从 Dockerfile 反推「一个能跑 OLMo-core 全部功能的镜像里有哪些东西、为什么缺一不可」。

**操作步骤**：

1. 通读 [src/Dockerfile:L13-L171](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/Dockerfile#L13-L171)，列出 build 阶段安装的所有 CUDA 扩展（grouped_gemm、flash-attn 2/3/4、TransformerEngine、ring-flash-attn、liger-kernel、可选的 QuACK/NVSHMEM）。
2. 对照 [README.md:L49-L55](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/README.md#L49-L55) 的可选依赖清单，标注每个扩展在 OLMo-core 里对应的功能（哪个解锁哪种 attention 后端、哪个解锁 MoE、哪个解锁 float8）。
3. 找到「卸载自身」的三行（4.4.3 ④），用自己的话写一两句：为什么这三行能实现「改代码不用重建镜像」？

**需要观察的现象**：清单里每个扩展都能在 README 的功能描述里找到对应；`WORKDIR /app/olmo-core` 与「launch 时 clone 源码」的约定对上。

**预期结果**：完成一张「扩展 → 解锁功能」对照表（例如 grouped_gemm → dropless MoE、torchao → float8 训练）。这是纯阅读实践，无需运行验证。

#### 4.4.5 小练习与答案

**练习 1**：既然 `pip install '.[all]'` 会装上 olmo-core，为什么紧接着又要卸载它？

答案：`.[all]` 的目的只是让 pip 解析出 olmo-core 的**依赖**（all extras 并集）并全部装上；olmo-core 包本体对这个镜像没用，因为 launch 流程会在容器里 clone 指定 commit 的源码再 `pip install -e .`。留下旧版本反而可能遮蔽现场安装的代码。

**练习 2**：为什么 build 阶段基础镜像必须是 `devel`（含 nvcc）而不能是 `runtime`？

答案：build 阶段要**从源码编译** flash-attn、TransformerEngine、grouped_gemm 等 CUDA C++/CUDA 扩展，编译需要 nvcc 与 CUDA 头文件，只有 devel 镜像提供。（[Makefile:L98-L103](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/Makefile#L98-L103) 的注释进一步说明 release 基础镜像也默认选 devel：TE ≥ 2.18 导入时要 dlopen 系统 libcudart，RMA 扩展要运行时 JIT 编译。）

**练习 3**：本地 `pip install -e '.[dev]'` 的环境和官方镜像差了什么？会影响跑哪些测试？

答案：差全部需要编译的 CUDA 扩展（flash-attn 2/3/4、TE、grouped_gemm 等）和精确锁定的 CUDA/torch 组合。影响：带 `@pytest.mark.gpu` 且依赖这些后端的测试在本地要么被 `pytest.skip` 跳过、要么因 `importorskip` 整文件跳过；纯 CPU 测试（如 config_test.py）不受影响。这正是 CI 把 GPU 测试放到装了官方镜像的集群上跑的原因。

## 5. 综合实践

**任务：从零搭好「读代码 + 跑测试 + 过检查」三件套，并留下一份环境档案。**

1. 新建虚拟环境，按 README 顺序装 torch（CPU 版即可）→ `pip install -e '.[dev]'`（激进一点可以用 `.[all]`）。
2. 验证安装：`pip show ai2-olmo-core` 应显示版本 3.0.0 与本地路径；也可以直接运行 `python src/olmo_core/version.py`（它会打印 `VERSION`，这正是 [Makefile:L114](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/Makefile#L114) 反查版本用的同一命令）。注意 `olmo_core` 包并没有导出 `__version__` 属性，别用 `olmo_core.__version__` 去验证。
3. 跑最小测试集：`pytest -v src/test/config_test.py`，确认全绿；再用 `-k` 挑出单个函数跑一遍。
4. 观察跳过机制：`pytest -v src/test/nn/moe/top16_native_test.py`，记录输出里的 `SKIPPED`（或 `PASSED`，如果有 GPU）。
5. 跑三连检：`make checks`。若 style-check 报红，先 `make style` 再复查（记得先 `git status` 确认没有意外改动）。
6. 把以上每步的命令与关键输出（passed/skipped 计数、checks 结果）写进你自己的笔记文件 `OLMo-core-tutorial/my-env-notes.md`，后续每一讲都往里追加。

**验收标准**：`pytest -v src/test/config_test.py` 全部通过；`make checks` 三段检查通过（或记录下与本地环境相关的失败原因）；能用自己的话说出「GPU 测试为什么不会在无 GPU 机器上报错」。

## 6. 本讲小结

- OLMo-core 的 PyPI 包名是 `ai2-olmo-core`，开发安装按 README：**先装匹配硬件的 PyTorch，再 `pip install -e '.[all]'`**；核心依赖仅 13 个包，重功能全部拆进 extras（`dev` / `fa4` / `torchao` / `beaker` / …），`all` 是并集。
- `pyproject.toml` 是工具链唯一事实来源：black/isort 行宽 100、ruff 忽略 `F403/F405/E501`、`__init__.py` 额外豁免 `F401`、pytest 的 `testpaths="src/test/"` 与 `gpu` 标记都在这里注册。
- 测试目录 `src/test/` 镜像 `src/olmo_core/` 的结构；GPU 测试 = `@pytest.mark.gpu` 标记（供 CI `-m gpu` 筛选）+ 函数体内 `torch.cuda.is_available()` 检查失败即 `pytest.skip()`（本地裸跑时自动跳过）+ `pytest.importorskip()`（可选编译扩展缺失时整文件跳过）。
- 贡献节奏：`make style` 自动格式化 → `make checks`（style-check + lint-check + type-check）全绿再提交。
- 官方 Docker 镜像**只冻结难编译的 CUDA 依赖、不含 olmo-core 源码**（`pip install '.[all]'` 后立刻卸载自身），源码由 launch 流程在容器内现场 clone 安装。
- 顺带预告：`config_test.py` 里那些 `overrides=["bar.x=0"]` 断言，就是第 2 单元「一切皆 Config」主题的测试侧写。

## 7. 下一步学习建议

下一讲（u1-l3《源码地图：目录结构与测试布局》）将带你在 `src/olmo_core/` 的目录树里定位模型、数据、训练、分布式四大类代码，并练习「从源码文件反查测试文件」的映射技巧——本讲的 `src/test/` 结构知识会直接用上。

在此之前，建议你在本地多做一件小事：打开 `src/test/` 目录随手翻三四个测试文件，验证你已能认出哪些是 GPU 测试（找 `@pytest.mark.gpu` 和 `pytest.skip`），哪些是纯 CPU 测试。想提前热身第 2 单元的话，可以把 `src/test/config_test.py` 完整读一遍，试着不看断言猜出 `as_dict()` / `merge()` 的行为。
