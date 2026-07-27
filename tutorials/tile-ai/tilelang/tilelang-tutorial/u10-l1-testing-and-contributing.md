# 测试体系与贡献流程

## 1. 本讲目标

本讲是「二次开发与贡献」单元的第一课，面向想要给 tilelang 提交第一个 Pull Request 的开发者。学完本讲你应当能够：

- 说清 `testing/` 目录的组织方式，并能用 `pytest` 在本地跑通一个已有测试。
- 理解 `perf` / `slow` 两类 pytest marker 的含义，知道为什么性能测试默认被跳过、又如何用 `--run-perf` 打开。
- 会用 `tilelang.testing` 提供的硬件条件装饰器（`requires_cuda` / `requires_cuda_or_cdna` / `requires_cuda_compute_version_le` 等）让测试「有硬件才跑、没有就优雅跳过」。
- 仿照 GEMM 测试范式，为一个 `examples/` 脚本补一个最小的 pytest 用例。
- 掌握 pre-commit / ruff / clang-format / `format.sh` 这一套代码风格流水线，理解 CI 是如何强制它的。

本讲对应最小模块：`testing.python`、`testing.cpp`（后者目前为占位目录，本讲会如实说明其现状）。

## 2. 前置知识

阅读本讲前，你应当已经：

- 会用 `@tilelang.jit` 写并编译一个 GEMM kernel（见 [u1-l4 第一个 Kernel](u1-l4-quickstart-gemm.md) 与 [u4-l2 jit 装饰器](u4-l2-jit-lazy-eager-modes.md)）。
- 了解 `tilelang.compile(program, out_idx=[...])` 返回一个 `JITKernel`，并知道 `kernel.get_profiler()` 能做正确性校验与计时（见 [u8-l3 Profiler 与基准测试](u8-l3-profiler-and-benchmark.md)）。
- 大致了解 pytest 的基本用法（`def test_xxx()`、`@pytest.mark.xxx`、`conftest.py` 的作用）。

几个本讲会反复用到、但初学者可能陌生的术语，先在这里集中解释：

- **marker（标记）**：pytest 给测试打标签的机制，形如 `@pytest.mark.perf`。可以用来「筛选」或「跳过」一类测试。
- **conftest.py**：pytest 的「项目级插件」文件，放在测试目录里，里面的 `pytest_addoption`、`pytest_collection_modifyitems` 等 hook 函数会在 pytest 启动时自动被调用，用来改测试的收集与执行行为。
- **skipif**：pytest 提供的条件跳过装饰器，条件为真时把测试标记为 `skipped` 而不是 `failed`。
- **pre-commit**：一个在 `git commit` 之前自动跑格式化/检查工具的框架；tilelang 用它统一管理 ruff、clang-format、codespell 等钩子。
- **ruff**：一个极快的 Python linter + formatter，tilelang 用它替代 black + flake8 + isort。

## 3. 本讲源码地图

本讲涉及的关键文件如下表：

| 文件 | 作用 |
|------|------|
| `testing/conftest.py` | 测试根目录的 pytest 插件：固定随机种子、注册 `--run-perf` 选项、默认跳过 `perf` 测试、防止「零用例」静默通过 |
| `tilelang/testing/__init__.py` | 测试工具箱：硬件条件装饰器（`requires_cuda` 等）、`main()` 入口、性能回归辅助的再导出 |
| `tilelang/testing/perf_regression.py` | 性能回归框架：`process_func` 记录延迟、`regression()` 收集模块内全部 `regression_*` 函数并输出表格 |
| `testing/python/` | 全部 Python 测试，按子系统分目录（`kernel/`、`jit/`、`language/`、`transform/`、`carver/` 等） |
| `testing/python/kernel/test_tilelang_kernel_gemm.py` | GEMM 正确性测试的典型范例，本讲反复引用 |
| `testing/cpp/` | **当前仅含 `.gitkeep` 占位**，CMake 中 `USE_GTEST OFF`，尚无 C++ 单元测试 |
| `pyproject.toml` | 声明 pytest 的 `markers`、ruff 规则、cibuildwheel 的 `test-command` |
| `.pre-commit-config.yaml` | pre-commit 钩子配置：ruff、clang-format、codespell、pymarkdown 等 |
| `format.sh` | 一键格式化脚本，封装了 pre-commit，默认只处理「相对 merge-base 改动过的文件」 |
| `CONTRIBUTING.md` | 贡献指南：开发环境、lint、本地测试、提交 PR 的流程 |
| `.github/workflows/ci.yml` | CI 流水线：lint 作业 + 多后端（CUDA / Metal）测试作业 |
| `requirements-test*.txt` | 测试依赖，按后端拆分（`requirements-test.txt` 通用，`-cuda` / `-rocm` / `-metal` 各自追加） |

## 4. 核心概念与源码讲解

### 4.1 测试目录结构与运行方式

#### 4.1.1 概念说明

tilelang 是一个「Python 用户面 + C++ 引擎面」的双面项目（见 [u1-l3 仓库目录结构](u1-l3-repo-layout-and-entry.md)）。一个自然的问题是：**C++ 引擎的代码怎么测？** tilelang 当前的回答是——**几乎全部通过 Python 集成测试来间接驱动**：Python 测试用 DSL 写一个 kernel、编译、运行、用 PyTorch 参考实现比对结果。如果 C++ 的 Pass、tile op lowering、codegen 任何一环出错，都会以「结果数值不对」或「编译失败」的形式在 Python 测试里暴露出来。

因此你会看到 `testing/python/` 下按子系统分了 30 个目录（`kernel`、`jit`、`language`、`transform`、`carver`、`autotune`、`cuda`、`cpu`、`metal`、`webgpu`、`llvm`、`layout`、`profiler`、`tools` …），每一个目录都对应 tilelang 的一个用户面子系统。这种「目录镜像子系统」的组织方式让你能快速定位「某功能的测试在哪」。

#### 4.1.2 核心流程

本地跑测试的流程非常直接，三步：

1. **建好开发环境并编译好 tilelang**（见 `CONTRIBUTING.md` 的 *Setup Development Environment* 与 *Install Develop Version*）。
2. **安装测试依赖**：`uv pip install --requirements requirements-test.txt`（CUDA 环境再追加 `requirements-test-cuda.txt`）。
3. **用 pytest 运行**：`python3 -m pytest testing`（CONTRIBUTING 里的官方写法），或只跑某一个文件 / 某一个用例。

CI 里的运行方式本质相同，只是多套了一层 `uv run --no-project -m --` 与并发参数 `--numprocesses=8`（来自 `pytest-xdist`）。

#### 4.1.3 源码精读

`CONTRIBUTING.md` 的 *Test Locally* 一节明确给出本地测试命令：

[CONTRIBUTING.md:119-127](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/CONTRIBUTING.md#L119-L127) — 官方推荐的本地测试入口就是 `python3 -m pytest testing`。

CI 里 CUDA 测试的实际命令（注意它 `cd testing` 后用相对路径 `./python` 指向 `testing/python`，并加了 `--maxfail=3` 与 `--numprocesses=8`）：

[.github/workflows/ci.yml:380-390](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/.github/workflows/ci.yml#L380-L390) — CI 的 CUDA 测试作业，用 `pytest-xdist` 的 8 进程并发跑 `testing/python`。

同一个 CI 文件里还有一个「先跑 examples」的作业，它把 `examples/` 目录也当成 pytest 根来收集（因为很多 example 自带 `test_example_*.py`）：

[.github/workflows/ci.yml:367-377](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/.github/workflows/ci.yml#L367-L377) — CI 在跑正式测试前，会先把 `examples/` 当测试目录跑一遍，这解释了为什么每个 example 都应该配一个 `test_example_*.py`。

关于 `testing/cpp/`，必须如实说明：

[testing/cpp/.gitkeep](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/testing/cpp/.gitkeep) — 该目录目前只有这一个占位文件，没有任何 C++ 测试源码。

CMakeLists.txt 中也显式关闭了 GoogleTest：

[CMakeLists.txt:350-351](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/CMakeLists.txt#L350-L351) — `set(USE_GTEST OFF)`，且 `src/` 下不存在任何 `TEST()` 宏。结论：**tilelang 当前没有独立的 C++ 单元测试**，`testing/cpp/` 是为未来预留的位置；C++ 逻辑的正确性目前完全由 `testing/python/` 下的集成测试覆盖。本讲后续因此以 Python 测试为主。

#### 4.1.4 代码实践

**实践目标**：在本地跑通一个已有的 GEMM 正确性测试，观察它的输出结构。

**操作步骤**：

1. 确认已按 `CONTRIBUTING.md` 编译好 tilelang 并安装了 `requirements-test.txt`（CUDA 环境还要 `requirements-test-cuda.txt`）。
2. 直接运行整个 GEMM 测试文件：
   ```bash
   python3 -m pytest testing/python/kernel/test_tilelang_kernel_gemm.py -v
   ```
3. 只跑其中一个用例（pytest 的 `-k` 按名字筛选）：
   ```bash
   python3 -m pytest testing/python/kernel/test_tilelang_kernel_gemm.py -k "f16f16f16_nn" -v
   ```
4. 也可以像该文件末尾那样，把测试文件当脚本直接跑（它最后调用了 `tilelang.testing.main()`）：
   ```bash
   python3 testing/python/kernel/test_tilelang_kernel_gemm.py
   ```

**需要观察的现象**：每个用例要么 `PASSED`，要么因为缺少对应硬件而 `SKIPPED`（例如在没有 CUDA 的机器上，带 `@tilelang.testing.requires_cuda` 的用例会被跳过）。`-v` 模式下会打印每个测试函数的全名。

**预期结果**：在带 NVIDIA GPU 的机器上，`test_gemm_f16f16f16_nn` 应当 `PASSED`；在无 GPU 的机器上，绝大多数用例 `SKIPPED`。**具体耗时与是否通过属于「待本地验证」**（本讲编写环境无 GPU，无法替你实跑）。

#### 4.1.5 小练习与答案

**练习 1**：`testing/python/` 下哪个目录最可能存放「软件流水线相关 Pass」的测试？

> **答案**：`testing/python/transform/`（对应 C++ 侧 `src/transform/` 的 Pass，包括 `InjectSoftwarePipeline`）。这也印证了「目录镜像子系统」的组织规律。

**练习 2**：为什么 CONTRIBUTING 推荐用 `python3 -m pytest` 而不是直接敲 `pytest`？

> **答案**：`python3 -m pytest` 保证用的是「当前 Python 环境」里的 pytest，避免落到系统 PATH 上另一个 Python 的 pytest，从而确保 `import tilelang` 解析到你在本仓库开发的那一份。`testing/conftest.py` 还会把仓库根目录插到 `sys.path` 最前面，进一步保证导入的是 in-tree 的 `tilelang/`。

---

### 4.2 pytest 配置：marker、默认跳过与「零用例」保护

#### 4.2.1 概念说明

tilelang 的测试有两类特殊的「重测试」：一类是**性能基准测试**（`perf`），跑一次要 warmup + 多轮计时，慢且对噪声敏感；一类是**大范围正确性扫描**（`slow`），可能枚举上百种 shape/dtype 组合。这两类都不该在「日常快速回归」里每次都跑。

tilelang 的设计是：**默认只跑快测试，把 `perf` 标记的测试自动跳过**，只有显式加 `--run-perf` 才放行。这避免了「CI 默认就被性能测试拖慢」或「开发者本地跑一次测试要等半小时」。

此外还有一个容易忽视但很重要的保护：「**零用例不算通过**」。如果你写错了测试路径或过滤器，导致 pytest 一个用例都没收集到，默认情况下 pytest 会报「0 passed」并退出码为 0（成功），这会掩盖严重错误。tilelang 的 conftest 把这种情况强行变成失败。

#### 4.2.2 核心流程

marker 机制的运转流程：

1. **声明**：`pyproject.toml` 的 `[tool.pytest.ini_options].markers` 注册合法 marker 名，避免 pytest 对未知 marker 报 warning。
2. **打标**：测试函数上加 `@pytest.mark.perf`。
3. **收集期改写**：pytest 在收集完所有用例后调用 conftest 的 `pytest_collection_modifyitems` hook；若没有 `--run-perf`，就给每个带 `perf` 标记的 item 追加一个 `skip` 标记。
4. **报告期兜底**：`pytest_terminal_summary` hook 检查「真正执行（非 skip/deselect）的用例数」，若为 0 则用 `pytest.exit(..., returncode=5)` 强制失败。

#### 4.2.3 源码精读

marker 的声明在 `pyproject.toml`：

[pyproject.toml:244-250](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/pyproject.toml#L244-L250) — 注册了 `perf`（默认跳过的性能测试）与 `slow`（长耗时的正确性扫描）两个 marker。注意这里只是「声明含义」，是否跳过由 conftest 的代码决定。

「默认跳过 perf」的核心逻辑在 `testing/conftest.py`：

[testing/conftest.py:45-51](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/testing/conftest.py#L45-L51) — 注册 `--run-perf` 命令行选项，默认 `False`。

[testing/conftest.py:54-65](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/testing/conftest.py#L54-L65) — 收集期 hook：若没传 `--run-perf`，就遍历所有用例，给带 `perf` marker 的逐个追加 `skip` 标记，并记下被过滤的数量到 `config._perf_items_filtered`。

「零用例保护」在同一文件的终端总结 hook 里：

[testing/conftest.py:68-83](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/testing/conftest.py#L68-L83) — 统计真正执行（排除 `skipped`/`deselected`）的用例数；若为 0 且本次只因 `perf` 被过滤，就给出「请加 `--run-perf`」的提示；否则直接 `pytest.exit("No tests were collected.", returncode=5)`，把「没收集到用例」变成明确的失败（退出码 5）。

conftest 还做了两件让测试「可复现」的事：固定随机种子、为 torch JIT 扩展设置按 worker 隔离的目录：

[testing/conftest.py:6](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/testing/conftest.py#L6) 与 [testing/conftest.py:15-23](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/testing/conftest.py#L15-L23) — 前者固定 `PYTHONHASHSEED=0`，后者把 `TORCH_EXTENSIONS_DIR` 指向「按 pytest-xdist worker + pid 隔离」的目录，避免多进程并发编译 torch 扩展时互相踩踏。

一个真实的 `perf` 测试长这样（注意它**同时**带了硬件装饰器和 `perf` marker，两层门控）：

[testing/python/jit/test_tilelang_jit_nvrtc.py:210-213](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/testing/python/jit/test_tilelang_jit_nvrtc.py#L210-L213) — `@tilelang.testing.requires_cuda` 管「有没有 GPU」，`@pytest.mark.perf` 管「是不是性能测试」，二者叠加：默认 CI 跑会跳过它，只有 `--run-perf` 且有 CUDA 时才真正执行。

#### 4.2.4 代码实践

**实践目标**：体会 `perf` marker 的默认跳过与 `--run-perf` 的放行效果，以及「零用例保护」。

**操作步骤**：

1. 不加任何参数，列出一个 perf 测试的「是否会跑」（用 `--collect-only` 只收集不执行）：
   ```bash
   python3 -m pytest testing/python/jit/test_tilelang_jit_nvrtc.py -k "do_bench" --collect-only -q
   ```
2. 用 `--co -rs` 观察跳过原因（`-rs` 会把 skip 的理由汇总显示）：
   ```bash
   python3 -m pytest testing/python/jit/test_tilelang_jit_nvrtc.py -k "do_bench" -rs
   ```
   预期看到类似 `SKIPPED [1] ...: performance test skipped by default; pass --run-perf to include it`。
3. 触发「零用例保护」：故意拼错一个用例名，让筛选结果为空：
   ```bash
   python3 -m pytest testing/python/kernel/test_tilelang_kernel_gemm.py -k "definitely_no_such_test"
   ```
   预期以退出码 5 结束，并打印 `Error: No tests were collected.`（前提是该文件里确实没有其它非 perf 用例被这个过滤器命中——若想稳定复现，可换成 `--run-perf` 与否两种对照）。

**需要观察的现象**：第 2 步 perf 用例被 `SKIPPED` 且理由明确指向 `--run-perf`；第 3 步「零用例」被当成失败。

**预期结果**：marker 行为可复现；零用例退出码为 5。**具体输出文案属「待本地验证」**。

#### 4.2.5 小练习与答案

**练习 1**：如果你新增了一个 `@pytest.mark.perf` 测试，但忘了在 `pyproject.toml` 的 `markers` 里声明，会发生什么？

> **答案**：pytest 会发出 `PytestUnknownMarkWarning`，提示该 marker 未注册（不影响运行，但 pre-commit / CI 会因为 `filterwarnings = ["always"]` 把它显示出来，提醒你补声明）。声明 marker 的意义在于：文档化其含义，并避免拼写错误被静默忽略。

**练习 2**：为什么「零用例」要专门做成失败，而不是像 pytest 默认那样返回成功？

> **答案**：在 CI 里，「0 passed, 0 failed」经常意味着「测试根本没被收集到」（路径写错、import 报错被吞、过滤器写反）。把它当成成功会让真正的回归悄悄溜走。tilelang 选择「宁可误报也要兜底」，用退出码 5 强制人工确认。

---

### 4.3 tilelang.testing 工具箱：硬件条件装饰器与 main()

#### 4.3.1 概念说明

GPU kernel 测试有一个天然特点：**强依赖特定硬件**。一个 WGMMA（Hopper）测试在 Ampere 显卡上根本编不过；一个 MFMA（CDNA）测试在 NVIDIA 卡上毫无意义。如果这些测试在「不对的硬件」上直接报 `failed`，CI 的信号就会被噪声淹没。

`tilelang.testing` 模块就是用来解决这个问题的。它提供一组**硬件条件装饰器**：在被装饰的测试真正运行**之前**先探测当前硬件，若不满足条件就给测试打上 `skip`，从而让「跑不了」和「跑挂了」严格区分开。这层抽象复用了上游 TVM 的 `tvm.testing._compose` 机制来叠加多个 pytest mark。

此外它还提供 `main()`——一个把「测试文件当脚本直接跑」的便捷入口，让你既能用 `pytest` 跑、也能 `python test_xxx.py` 跑。

#### 4.3.2 核心流程

硬件装饰器的工作流程：

1. 在**导入/装饰期**（而非测试运行期）调用一次 `determine_target("auto", return_object=True)` 探测当前 target。
2. 用 `target_is_cuda` / `target_is_cdna` / `target_is_gfx950` 等谓词判定硬件归属。
3. 根据判定结果构造 `pytest.mark.skipif(...)`，再通过 `_compose` 把它和上游的 `requires_cuda` / `requires_rocm` 等 mark 合并，贴到测试函数上。
4. `requires_cuda_compute_version(major, minor, mode=)` 进一步比较 CUDA 计算能力版本，支持 `ge/gt/le/lt/eq` 五种比较模式。

`main()` 的流程更简单：取调用方所在文件路径，把它和命令行参数一起喂给 `pytest.main()`。

#### 4.3.3 源码精读

工具箱的公共导出清单（一眼看清有哪些装饰器可用）：

[tilelang/testing/__init__.py:16-29](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/testing/__init__.py#L16-L29) — `__all__` 暴露了 `requires_cuda`（从 TVM 复用）、自研的 `requires_cdna` / `requires_cuda_or_cdna` / `requires_gfx950`、以及一组 `requires_cuda_compute_version_{ge,gt,le,lt,eq}`。

以「CUDA 或 CDNA 任一即可」这个常用装饰器为例看实现：

[tilelang/testing/__init__.py:69-78](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/testing/__init__.py#L69-L78) — `requires_cuda_or_cdna` 先调 `_check_is_cuda_or_cdna()` 探测，再构造 `skipif`，最后用 `_compose` 贴到函数上。这就是为什么 GEMM 测试里大量出现它——同一份 GEMM 逻辑在 NVIDIA 张量核和 AMD CDNA MFMA 上都能跑。

更精细的「计算能力版本」装饰器，用来把 WGMMA（SM90）等指令约束在特定架构：

[tilelang/testing/__init__.py:108-172](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/testing/__init__.py#L108-L172) — `requires_cuda_compute_version` 通过 `nvcc.get_target_compute_version()` 拿到架构，按 `mode` 比较，无 GPU 时退化为 `(0,0)` 自动跳过。GEMM 测试里的 `@tilelang.testing.requires_cuda_compute_version_le(8, 9)`（见 [test_tilelang_kernel_gemm.py:413](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/testing/python/kernel/test_tilelang_kernel_gemm.py#L413)）正是用它把 SR 变体限制在「SM80/SM89 及以下」（因为 WGMMA 只支持 B 在 shared，SR 变体是 SM89 的 mma 路径）。

`main()` 的实现极简但很实用：

[tilelang/testing/__init__.py:95-97](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/testing/__init__.py#L95-L97) — 用 `inspect.getsourcefile(sys._getframe(1))` 拿到「调用 main() 的那个测试文件」的路径，再连同命令行参数一起交给 `pytest.main`。这就是测试文件末尾 `if __name__ == "__main__": tilelang.testing.main()` 的原理。

#### 4.3.4 代码实践

**实践目标**：直观感受硬件装饰器如何让测试「优雅跳过」。

**操作步骤**：

1. 选一个带架构约束的用例，用 `-rs` 观察它的 skip 理由：
   ```bash
   python3 -m pytest testing/python/kernel/test_tilelang_kernel_gemm.py -k "sr" -rs -v
   ```
2. 对照源码：在 [test_tilelang_kernel_gemm.py:413](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/testing/python/kernel/test_tilelang_kernel_gemm.py#L413) 找到 `requires_cuda_compute_version_le(8, 9)`，再到 [tilelang/testing/__init__.py:108-172](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/testing/__init__.py#L108-L172) 看它如何比较版本——理解你机器的 compute capability 决定了它是跑还是跳。

**需要观察的现象**：若你的 GPU 计算能力 > 8.9（如 H100 = SM90），`sr` 用例会被 `SKIPPED`，理由形如 `Requires CUDA compute le 8.9, but have 9.0`；若 ≤ 8.9 则正常运行。

**预期结果**：skip 理由里会带上「期望版本」与「实际版本」两个数字，便于排错。**具体版本号属「待本地验证」**。

#### 4.3.5 小练习与答案

**练习 1**：`requires_cuda` 和 `requires_cuda_or_cdna` 在「同一份 GEMM 代码同时支持 NVIDIA 与 AMD」时，哪个更合适？

> **答案**：`requires_cuda_or_cdna`。`requires_cuda` 会把 AMD 机器直接跳过，导致同一份逻辑在 CI 的 ROCm 作业里完全不跑；`requires_cuda_or_cdna` 则让它在两类硬件上都参与回归。观察 [test_tilelang_kernel_gemm.py:156 与 209](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/testing/python/kernel/test_tilelang_kernel_gemm.py#L156) 即可看到：f32/i8 这类「CUDA 与 CDNA 都支持张量核」的用例用 `requires_cuda_or_cdna`，而 f64 这种「只有 NVIDIA 有」的用 `requires_cuda`。

**练习 2**：为什么这些装饰器在「模块导入期」就探测硬件，而不是等测试函数跑起来再判断？

> **答案**：因为 pytest 的 skip 判断发生在**收集期/执行前**，必须在那之前就把 mark 准备好。导入期探测能确保 mark 在收集时已就位；代价是探测结果被「烤死」在一次 pytest 进程里——所以如果你中途换了 GPU，得重启 pytest。

---

### 4.4 测试编写范式：从 GEMM 测试到「为 example 补测试」

#### 4.4.1 概念说明

读到这里你已经能跑测试了，但「**怎么写一个新的正确性测试**」还需要一个范式。tilelang 的正确性测试几乎都遵循同一个套路：

> **「构造 kernel → 编译 → 用 `get_profiler().assert_allclose(ref_program)` 与 PyTorch 参考实现比对」**

这个套路的精妙之处在于：你不需要自己造随机输入、不需要自己调 kernel、不需要自己写断言——`profiler.assert_allclose` 会自动用 `TensorSupplyType` 生成输入、调用 kernel、再调用你给的 `ref_program`，最后用 `torch_assert_close` 比对（见 [u8-l3](u8-l3-profiler-and-benchmark.md)）。你只需要写两样东西：DSL kernel 和参考实现。

而对于 `examples/` 下的脚本，范式更简单：example 本身已经写了 `main()` 并自带正确性断言，测试只要**调用 `main()`** 即可。这也是 CI 能把 `examples/` 当测试目录跑的原因。

#### 4.4.2 核心流程

写一个 GEMM 风格正确性测试的流程：

1. 写一个 `matmul(...)` 工厂函数，参数化 shape/dtype/分块/线程，返回 `@T.prim_func`。
2. 写一个 `run_gemm(...)` 包装函数：调 `matmul(...)` 得 PrimFunc → `tilelang.compile(program, out_idx=[2])` → `kernel.get_profiler()` → 定义内嵌的 `ref_program(A, B)` → `profiler.assert_allclose(ref_program, atol=..., rtol=...)`。
3. 为每组想测的组合写一个 `def test_xxx()`，按需加硬件装饰器，内部调 `run_gemm(...)`。
4. 文件末尾加 `if __name__ == "__main__": tilelang.testing.main()`。

为 example 补测试的流程：

1. 确认 example 脚本里有一个可无参调用的 `main()`（如 `examples/elementwise/example_elementwise_add.py` 的 `main()`）。
2. 在同目录新建 `test_example_xxx.py`，写一个 `def test_example_xxx(): example_module.main()`。
3. 末尾加 `tilelang.testing.main()`。

#### 4.4.3 源码精读

GEMM 测试的「工厂 + run + 用例」三段式范式：

[testing/python/kernel/test_tilelang_kernel_gemm.py:6-49](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/testing/python/kernel/test_tilelang_kernel_gemm.py#L6-L49) — `matmul(...)` 工厂：把所有可调参数（含 `trans_A/trans_B/in_dtype/...`）作为入参，内部用 `@T.prim_func` 搭建 kernel 并返回。注意函数体是「搭建 IR 的蓝图」（见 [u2-l1](u2-l1-prim-func-kernel-tensor.md)），不是运行时逻辑。

[testing/python/kernel/test_tilelang_kernel_gemm.py:87-103](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/testing/python/kernel/test_tilelang_kernel_gemm.py#L87-L103) — `run_gemm` 的比对核心：内嵌一个 `ref_program(A, B)`（用 `torch.matmul`），再交给 `profiler.assert_allclose(ref_program, atol=1e-2, rtol=1e-2)`。注意 `tfloat32` 分支里 `(A.view(torch.int32) - 0x1000)` 的位操作——这是为了把 fp32 截断成 tf32 以匹配硬件 MMA 的精度行为，是写参考实现时常见的「对齐硬件精度」技巧。

[testing/python/kernel/test_tilelang_kernel_gemm.py:106-121](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/testing/python/kernel/test_tilelang_kernel_gemm.py#L106-L121) — 一个具体用例：`@tilelang.testing.requires_cuda` 门控硬件，函数体一行 `run_gemm(...)` 传具体参数。整个文件就是「一个工厂 + 一个 run + N 个一行用例」的清晰结构。

[testing/python/kernel/test_tilelang_kernel_gemm.py:550-551](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/testing/python/kernel/test_tilelang_kernel_gemm.py#L550-L551) — 文件末尾的脚本入口，让该文件既能被 pytest 收集，也能 `python xxx.py` 直接跑。

example 测试的极简范式（这是本讲「综合实践」要你模仿的模板）：

[examples/elementwise/test_example_elementwise.py:1-11](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/examples/elementwise/test_example_elementwise.py#L1-L11) — 整个「example 测试」就这么多：`import example_elementwise_add`，然后 `def test_example_elementwise_add(): example_elementwise_add.main()`。正确性断言已经在 `main()` 内部用 `torch.testing.assert_close` 做过了（见 [example_elementwise_add.py:35-41](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/examples/elementwise/example_elementwise_add.py#L35-L41)），测试只需触发它。

#### 4.4.4 代码实践（本讲核心实践）

**实践目标**：为 `examples/quickstart.py` 补一个最小的 pytest 用例，并确保它通过 pre-commit。

> 注意：`examples/quickstart.py` 当前是「脚本式」的（顶层直接 `a = torch.randn(...)` 并跑），并没有可无参调用的 `main()`。因此这个实践分两小步：先给 example 抽一个 `main()`，再写测试。如果你不想改 example，可以退而求其次选 `examples/elementwise/`（它已有 `main()`，只需照搬 4.4.3 的模板）。下面给出「标准做法」。

**操作步骤**：

1. **选一个已有 `main()` 的 example**（推荐入门用，零风险）：复制 [examples/elementwise/test_example_elementwise.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/examples/elementwise/test_example_elementwise.py#L1-L11) 的写法，确认 `python3 -m pytest examples/elementwise/test_example_elementwise.py -v` 能跑（无 GPU 则 `SKIPPED`/import 失败，属正常）。
2. **给 `examples/quickstart.py` 补 `main()`**（示例代码，非项目原有代码）：
   ```python
   # 在 examples/quickstart.py 里把「顶层执行逻辑」包成函数
   def main(M=1024, N=1024, K=1024, block_M=128, block_N=128, block_K=32):
       matmul_relu_kernel = matmul.compile(M=M, N=N, K=K, block_M=block_M, block_N=block_N, block_K=block_K)
       import torch
       a = torch.randn(M, K, device="cuda", dtype=torch.float16)
       b = torch.randn(K, N, device="cuda", dtype=torch.float16)
       c = matmul_relu_kernel(a, b)
       ref_c = torch.relu(a @ b)
       torch.testing.assert_close(c, ref_c, rtol=1e-2, atol=1e-2)

   if __name__ == "__main__":
       main()
   ```
3. **新建 `examples/test_example_quickstart.py`**（示例代码）：
   ```python
   import tilelang.testing
   import quickstart


   def test_example_quickstart():
       quickstart.main()


   if __name__ == "__main__":
       tilelang.testing.main()
   ```
   > 让 `import quickstart` 能成功，需要 `examples/` 在 `sys.path` 上。CI 跑 examples 时是 `cd testing` 后用 `../examples` 作为 pytest 根，pytest 会把每个测试文件所在目录加进 `sys.path`（rootdir/conftest 机制），因此同目录 import 通常可行；若不行，可改为 `import os, sys; sys.path.insert(0, os.path.dirname(__file__))`。
4. **跑 pre-commit 验证风格**（见 4.5）：
   ```bash
   bash format.sh --files examples/quickstart.py examples/test_example_quickstart.py
   ```
5. **跑测试**：
   ```bash
   python3 -m pytest examples/test_example_quickstart.py -v
   ```

**需要观察的现象**：第 4 步 pre-commit 应当通过（或自动修复后请你 review）；第 5 步在有 GPU 的机器上 `PASSED`，无 GPU 则因 `torch.randn(..., device="cuda")` 报错或被相关装饰器跳过——**这正说明这个 example 测试还缺一个 `@tilelang.testing.requires_cuda` 装饰器**，把它补上更规范。

**预期结果**：测试在 CUDA 机器上通过；pre-commit 无报错。**是否通过属「待本地验证」**（本环境无 GPU）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `run_gemm` 把 `ref_program` 定义成**内嵌闭包**，而不是模块级函数？

> **答案**：因为 `ref_program` 需要捕获 `trans_A/trans_B/in_dtype/out_dtype` 这些随用例变化的参数。闭包天然捕获外层变量，免去了把这些参数再传一遍的样板代码；同时它紧跟在 `run_gemm` 内部，可读性更好。

**练习 2**：`profiler.assert_allclose` 相比自己写 `torch.testing.assert_close(kernel(a,b), ref(a,b))`，多了什么？

> **答案**：它还自动负责「用 `TensorSupplyType` 生成符合 dtype 的随机输入」「按需把输入搬到正确 device」「对 fp8/fp4 等低精度做安全供给」「调用 kernel 并处理 `out_idx`」「允许一定比例元素超差」等一系列细节（见 [u8-l3](u8-l3-profiler-and-benchmark.md)）。手写断言很容易在这些细节上出错。

---

### 4.5 贡献规范：pre-commit、ruff、format.sh 与 CI

#### 4.5.1 概念说明

一个开源编译器项目每天会收到大量 PR，如果每个 PR 的代码风格都不一样，review 的成本会失控。tilelang 用一条「**本地 + CI 双重强制的风格流水线**」来兜底：

- **本地**：`pre-commit` 在你 `git commit` 时自动跑 ruff（Python lint+format）、clang-format（C++ 格式）、codespell（拼写）、pymarkdown（Markdown）等钩子；`format.sh` 是它的命令行封装。
- **CI**：`.github/workflows/ci.yml` 的 `lint` 作业会跑 `pre-commit run --all-files`，任何钩子失败都直接挂 CI。

所以「贡献」的隐含规则是：**提交前先让 `pre-commit run --all-files`（或 `bash format.sh`）在本地产出干净结果**，否则 CI 一定会红。

#### 4.5.2 核心流程

提交流程：

1. 按 `CONTRIBUTING.md` 的 *Setup Development Environment* 建好环境并 `pre-commit install --install-hooks`（把钩子挂到 git 的 pre-commit 钩子上）。
2. 改代码、写测试。
3. 跑 `bash format.sh`（默认只处理「相对 merge-base 改动过的文件」，省时间）或 `bash format.sh --all`（全量）。
4. 若 format.sh 报「Reformatted files. Please review and stage the changes」，`git add` 那些被自动改过的文件后重跑。
5. 提交、推送。CI 的 lint 作业会再跑一遍 `pre-commit run --all-files` 兜底。

#### 4.5.3 源码精读

pre-commit 的配置（注意它显式排除了 `build/` 与 `3rdparty/`，避免去格式化第三方代码）：

[.pre-commit-config.yaml:9](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/.pre-commit-config.yaml#L9) — `exclude: '^(build|3rdparty)/.*$'` 是全局排除规则。

[.pre-commit-config.yaml:32-43](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/.pre-commit-config.yaml#L32-L43) — 两个核心钩子：`clang-format`（处理 C/C++）与 `ruff-check`/`ruff-format`（处理 Python）。注意 ruff 两个钩子都带 `--exit-non-zero-on-fix/--exit-non-zero-on-format`，意思是「即使它帮你自动修了，也判失败」——强制你 review 自动改动而不是让它们悄悄进仓库。

ruff 的规则在 `pyproject.toml` 里，其中对测试与 example 有特殊放宽：

[pyproject.toml:200-204](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/pyproject.toml#L200-L204) — `testing/**.py` 与 `examples/**.py` 关闭了 `UP`（pyupgrade）和 `FA`（future annotations）两类规则。这意味着**你在测试和例子里可以保留旧的类型注解写法**，ruff 不会逼你升级——这是为了降低示例代码的阅读门槛（见 issue #1079 的说明）。写新测试时不必刻意追求最新语法。

[pyproject.toml:206-220](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/pyproject.toml#L206-L220) — ruff 启用的规则族：`E/W`（pycodestyle）、`F`（Pyflakes）、`UP/FA`（pyupgrade）、`B`（bugbear）、`SIM`（simplify）。写代码时这些就是隐式约束。

`format.sh` 的「只处理改动文件」逻辑：

[format.sh:93-109](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/format.sh#L93-L109) — 默认分支：用 `git diff --name-only --diff-filter=ACM <merge-base>` 拿到「新增/修改」的文件，只对它们跑 pre-commit，比 `--all` 快得多。`--files` 分支则只跑你显式指定的文件（本讲 4.4.4 实践就用到了）。

CI 的 lint 作业是最后一道防线：

[.github/workflows/ci.yml:70-76](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/.github/workflows/ci.yml#L70-L76) — CI 用 `pipx run pre-commit run --all-files --color=always --show-diff-on-failure` 全量检查，失败时打印帮助信息并 `exit 1`。注意它还先用 Python 3.10（项目最低支持版本）做 `compileall` 与 C++ API 风格审计，确保最低版本也能编译。

`CONTRIBUTING.md` 把这套流程总结成了人类可读的步骤：

[CONTRIBUTING.md:40-58](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/CONTRIBUTING.md#L40-L58) — Coding Style 段：`bash format.sh --files <changed-file>...`；Python 用 ruff，C++ 用 clang-format 并遵循 [docs/developer_guide/cpp_style.md](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/docs/developer_guide/cpp_style.md)。

[CONTRIBUTING.md:111-117](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/CONTRIBUTING.md#L111-L117) — Lint Check 段：`pre-commit run --all-files`。

#### 4.5.4 代码实践

**实践目标**：在你新写的测试文件上跑 pre-commit，体会「自动修复 + 强制 review」的工作流。

**操作步骤**：

1. 接 4.4.4 你新建的 `examples/test_example_quickstart.py`，故意写一处不符合 ruff 风格的地方（比如多余的空行、单引号混用）。
2. 跑：
   ```bash
   bash format.sh --files examples/test_example_quickstart.py
   ```
3. 观察 ruff-format 是否自动改写了文件；若改写，format.sh 会以退出码 1 结束并提示 `Reformatted files. Please review and stage the changes.`。
4. `git diff` 查看自动改动，确认无误后 `git add`，再跑一次 format.sh，这次应输出 `tile-lang: All checks passed`。
5. （可选）全量自检：`pre-commit run --all-files`，这就是 CI lint 作业会做的事。

**需要观察的现象**：ruff 能自动修的会被自动修；任何被自动改过的文件都会让 format.sh「失败一次」以强制你 review。

**预期结果**：最终 `All checks passed`。这一步**不依赖 GPU**，可以在任意环境完成验证。

#### 4.5.5 小练习与答案

**练习 1**：`format.sh` 默认只跑「改动过的文件」，而 CI 跑 `--all-files`。这两者会不会不一致（本地过了、CI 挂了）？

> **答案**：会，但只在一种情况——你这次没动某个旧文件，但它其实不符合现行规则。`format.sh` 不会去碰它，CI 的全量检查却会抓出来。因此首次启用新规则后，偶尔会出现「我没改的文件在 CI 上挂了」。对策是按 CI 报错 `bash format.sh --files <那个旧文件>` 单独修一下。

**练习 2**：为什么 ruff 钩子要加 `--exit-non-zero-on-fix`？让它自动修完直接放过不是更省事吗？

> **答案**：因为「自动修改」也是一次对仓库内容的改动，必须由人确认后再提交。如果自动修完直接放过，相当于让机器的改写「不经 review 进入提交历史」，可能引入意外的语义变化（ruff 绝大多数情况只改格式，但极少数规则会动到代码）。`--exit-non-zero-on-fix` 把「自动修了」也当成需要人工确认的信号。

---

## 5. 综合实践

把本讲四个主题（运行测试、marker、测试范式、贡献规范）串起来，完成一个迷你贡献闭环：

**任务**：假设你为 tilelang 的新 dtype 组合（比如 `bf16 × bf16 → bf16` 的 GEMM）写了一个 GEMM kernel，现在要为它补一个能进 CI 的测试，并确保通过风格检查。

**要求**：

1. **仿照范式写测试**：参考 [test_tilelang_kernel_gemm.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/testing/python/kernel/test_tilelang_kernel_gemm.py#L6-L49) 的 `matmul` + `run_gemm` 三段式，在 `testing/python/kernel/` 下新建一个测试文件，包含：
   - 一个 `matmul(...)` 工厂（可基本复用现有实现）；
   - 一个 `run_gemm(...)`，用 `profiler.assert_allclose(ref_program, atol=1e-2, rtol=1e-2)` 比对 `torch.matmul`；
   - 一个 `test_gemm_bf16bf16bf16_nn()` 用例，加上**合适的硬件装饰器**（思考：bf16 在 CUDA 与 CDNA 上都有张量核支持，该用 `requires_cuda` 还是 `requires_cuda_or_cdna`？）；
   - 文件末尾的 `if __name__ == "__main__": tilelang.testing.main()`。
2. **本地跑通**：`python3 -m pytest <你的文件> -v`，确保 `PASSED`（无 GPU 则合理 `SKIPPED`）。
3. **跑 pre-commit**：`bash format.sh --files <你的文件>`，确保 `All checks passed`。
4. **体会 marker**：临时给你的用例加 `@pytest.mark.perf`，验证它默认被跳过、加 `--run-perf` 才跑；验证完把 marker 删掉。
5. **自检零用例保护**：故意用 `-k "no_such"` 跑你的文件，确认退出码为 5。

**验收标准**：
- 测试在 CUDA 机器上 `PASSED`；
- pre-commit 全绿；
- 能清晰说出每一步对应的源码依据（conftest 的 hook、`tilelang.testing` 的装饰器、pyproject 的 marker 声明）。

> 提示：第 1 步的硬件装饰器选择——`bf16` GEMM 在 NVIDIA（Ampere 及以后）与 AMD CDNA 上均有张量核路径，所以用 `@tilelang.testing.requires_cuda_or_cdna` 覆盖面更广，与现有 `test_gemm_bf16bf16f32_nn`（无装饰器，全平台跑）形成互补。

## 6. 本讲小结

- tilelang 的测试**几乎全部是 Python 集成测试**，集中在 `testing/python/`，按子系统分目录；`testing/cpp/` 目前只是 `.gitkeep` 占位（`USE_GTEST OFF`），C++ 逻辑靠 Python 测试间接覆盖。
- `perf`（性能）/`slow`（长扫描）两类 marker 在 `pyproject.toml` 声明；`perf` 默认被 `testing/conftest.py` 的 `pytest_collection_modifyitems` 跳过，需 `--run-perf` 放行；`pytest_terminal_summary` 把「零用例」强行变成退出码 5 的失败。
- `tilelang.testing` 提供 `requires_cuda` / `requires_cuda_or_cdna` / `requires_cdna` / `requires_gfx950` / `requires_cuda_compute_version_{ge,gt,le,lt,eq}` 等硬件条件装饰器，在导入期探测硬件、不满足则优雅 skip；`main()` 让测试文件可当脚本直接跑。
- 正确性测试的标准范式是「`matmul` 工厂 + `run_gemm` + `profiler.assert_allclose(ref_program)`」三段式；example 测试则极简到「调 `main()`」一行。
- 贡献规范由 pre-commit（ruff + clang-format + codespell + pymarkdown）+ `format.sh` + CI 的 lint 作业三层强制；`testing/**` 与 `examples/**` 在 ruff 下放宽了 `UP/FA` 规则；提交前务必让 `bash format.sh` 全绿。

## 7. 下一步学习建议

- **下一讲 [u10-l2 扩展 TileLang：新 op、新 pass 与新后端](u10-l2-extending-op-pass-backend.md)** 会从「测试与贡献流程」进入「真正改动编译器」：讲如何新增一个 tile op（`src/op` + tileop registry）、如何加一个 Pass（`src/transform` + `pass_pipeline`）、以及如何向 cpu/metal/webgpu 移植 language 与 codegen。学完本讲的测试范式后，你在 u10-l2 里写的每一处改动都能立刻配一个测试来兜底。
- **建议继续阅读的源码**：
  - 想看「参数化测试」更复杂的写法，读 `testing/python/jit/` 下的几个文件（它们展示了 `@pytest.mark.parametrize` 与 perf marker 的组合）。
  - 想深入性能回归框架，读 [tilelang/testing/perf_regression.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/testing/perf_regression.py#L1-L160)，理解 `regression()` 如何反射收集模块内所有 `regression_*` 函数。
  - 想理解 CI 的完整矩阵（CUDA/Metal/ROCm），通读 [.github/workflows/ci.yml](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/.github/workflows/ci.yml)。
  - C++ 贡献者必读 [docs/developer_guide/cpp_style.md](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/docs/developer_guide/cpp_style.md) 与 `maint/scripts/audit_cpp_api_style.py`（CONTRIBUTING 里提到的 C++ API 风格审计脚本）。
