# 测试、基准与贡献流程

## 1. 本讲目标

本讲是 Unit 7（后端、算子内核与二次开发）的收尾课，目标不是讲新的编译机制，而是教你**如何把自己的改动安全地贡献回 TileScale 项目**。学完后你应当能够：

- 知道项目的测试放在哪里、怎么组织、怎么本地跑起来，并能看懂 `conftest.py` 与 `tilelang.testing` 提供的测试基础设施。
- 看懂 `benchmark/` 下各类基准（matmul / attention / mamba / distributed）的结构，并能运行 `benchmark_matmul.py` 测出一个 kernel 的延迟与 TFLOPS。
- 理解 `format.sh` + `.pre-commit-config.yaml` 背后的格式化链路（ruff / clang-format / clang-tidy），知道在 push 前应跑什么。
- 看懂 `.github/workflows/ci.yml` 的 CI 矩阵与执行顺序，并按 `CONTRIBUTING.md` 的规范完成 fork → 装环境 → lint → test → 提 PR 的完整流程。

本讲默认你已经读过 [u1-l4 仓库结构与入口文件地图](u1-l4-repo-structure.md)，了解 `src/`、`tilelang/`、`testing/`、`benchmark/` 各自的职责。

## 2. 前置知识

- **pytest**：Python 最常用的测试框架。TileScale 的所有 Python 测试都用 pytest 编写与运行，测试函数以 `test_` 开头，用 `assert` 断言。
- **测试标记（marker）**：pytest 允许用 `@pytest.mark.xxx` 给测试打标签，运行时用 `-m xxx` 筛选。TileScale 用 `distributed` 标记把「需要多卡分布式环境」的测试单独隔离。
- **pre-commit**：一个在 `git commit` 前自动跑各类检查（格式化、拼写、语法）的工具，配置写在 `.pre-commit-config.yaml`。它保证进入仓库的代码风格统一。
- **ruff**：用 Rust 写的超快 Python linter + formatter，本项目用它替代 black + flake8 + isort。
- **TFLOPS**：每秒万亿次浮点运算，衡量 GPU kernel 算力的常用指标。对 `M×N×K` 矩阵乘，浮点运算量为 \(2MNK\)，于是：

\[
\text{TFLOPS} = \frac{2 \cdot M \cdot N \cdot K}{\text{latency (秒)}} \times 10^{-12}
\]

- **CI（持续集成）**：每次提 PR，GitHub Actions 会自动在远程机器上 lint + 编译 + 跑测试，只有全绿才能合并。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `testing/conftest.py` | 测试全局 fixture：固定随机种子、防止「零用例被收集」时静默通过 |
| `tilelang/testing/__init__.py` | 测试工具库：`main()` 单文件运行包装、`requires_distributed`、按 SM 架构跳过测试的系列装饰器 |
| `tilelang/testing/perf_regression.py` | 性能回归测试记录器：`process_func` / `regression` 收集延迟并对比基线 |
| `benchmark/matmul/benchmark_matmul.py` | autotune 驱动的 matmul 基准，给出最优配置、延迟、TFLOPS |
| `format.sh` | 一键格式化脚本：按改动文件 / 全量 / 指定文件三种模式跑 pre-commit 与 clang-tidy |
| `.pre-commit-config.yaml` | pre-commit 钩子配置：ruff、clang-format、codespell、pymarkdown 等 |
| `pyproject.toml` | `[tool.ruff]` / `[tool.pytest.ini_options]` / `[tool.cibuildwheel]` 等工具配置 |
| `.github/workflows/ci.yml` | GitHub Actions CI：lint job + tests job（self-hosted NVIDIA, CUDA-12.8） |
| `CONTRIBUTING.md` | 贡献指南：fork、装开发环境、lint、test、提 PR 的标准流程 |

## 4. 核心概念与源码讲解

### 4.1 测试体系：pytest 组织、conftest 与 tilelang.testing

#### 4.1.1 概念说明

TileScale 的测试以 **pytest** 为骨架，放在仓库根的 `testing/` 目录下。它分两层：

- `testing/python/`：Python 端测试，**按子系统分目录**，目录名与 `tilelang/` 包的模块布局基本对应——`kernel/`（各类 GEMM/elementwise kernel）、`language/`（DSL 原语）、`transform/`（编译 pass）、`layout/`（布局推理）、`autotune/`、`carver/`、`jit/`、`profiler/`、`amd/`、`metal/`、`webgpu/`、`cpu/` 等，还有 `issue/`（针对历史 bug 的回归测试）。
- `testing/cpp/`：C++ 端测试目录，目前**仅含一个 `.gitkeep` 占位文件**，尚未落地实际用例，留作后续扩展。

测试文件统一以 `test_` 开头，函数以 `test_` 开头；一个文件既能被 `pytest` 收集，也能 `python test_xxx.py` 直接运行（借助下文 `tilelang.testing.main()`）。

#### 4.1.2 核心流程

1. pytest 从指定目录递归发现所有 `test_*.py`，按目录结构组织成用例。
2. 启动时先加载 `conftest.py`，它负责两件全局事务：**固定随机种子**保证可复现、**兜底防止「零用例被收集」**静默通过。
3. 每个测试函数内部通常：用 `tilelang.compile` 编译一个 kernel → 用 `kernel.get_profiler().assert_allclose(ref, ...)` 与参考实现比对正确性。
4. 需要特殊硬件/环境的测试（多卡分布式、特定 SM 架构）用装饰器打标，在不满足时自动 **skip** 而非报错。

#### 4.1.3 源码精读

**全局种子与零用例兜底** —— [testing/conftest.py:1-21](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/testing/conftest.py#L1-L21) 在导入阶段就把 `PYTHONHASHSEED` 设为 0，并给 `random` / `torch` / `numpy` 都喂种子 0，确保不同机器、不同次运行生成的随机输入一致，方便复现数值 bug。`try/except ImportError` 则保证在没有 torch/numpy 的最小环境里 conftest 仍能加载。

**防止「全部用例被 skip 时假装通过」** —— [testing/conftest.py:24-41](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/testing/conftest.py#L24-L41) 注册了 pytest 的 `pytest_terminal_summary` 钩子：如果一次运行里 passed/failed/xfailed 等真实用例数为 0（只剩 skipped/deselected），就主动以返回码 5 退出。这能挡住「过滤器写错导致一个用例都没跑到、却显示绿色」的事故。

**单文件运行包装** —— [tilelang/testing/__init__.py:39-42](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/testing/__init__.py#L39-L42)：

```python
def main():
    test_file = inspect.getsourcefile(sys._getframe(1))
    sys.exit(pytest.main([test_file] + sys.argv[1:]))
```

`main()` 取调用者所在文件作为待测文件，把命令行参数透传给 `pytest.main()`。于是每个测试文件末尾都可以写 `if __name__ == "__main__": tilelang.testing.main()`，实现「直接 `python test_xxx.py` 跑这一个文件」，参数还能照常透传（如 `-k test_xxx`）。

**按 SM 架构跳过测试** —— [tilelang/testing/__init__.py:53-117](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/testing/__init__.py#L53-L117) 的 `requires_cuda_compute_version(major, minor, mode=...)` 读 nvcc 的 target compute 版本，按 ge/gt/le/lt/eq 比对，不满足就 `skipif`。这与 [u7-l2 CUDA 模板与 GEMM 内核族](u7-l2-cuda-gemm-templates.md) 讲的 sm70~sm120 分发直接呼应：测 wgmma 的用例会用 `requires_cuda_compute_version_ge(9, 0)` 保证只在 Hopper 及以上运行。

**分布式测试隔离** —— [tilelang/testing/__init__.py:141-156](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/testing/__init__.py#L141-L156)：

```python
_distributed_enabled = os.environ.get("TILELANG_USE_DISTRIBUTED", "0").lower() in ("1", "true", "on")

def requires_distributed(func):
    func = pytest.mark.distributed(func)
    func = pytest.mark.skipif(not _distributed_enabled, reason="...")(func)
    return func
```

它同时打两个标记：`pytest.mark.distributed` 让 CI 用 `-m distributed` 把这类测试单独选出，`skipif` 保证未设 `TILELANG_USE_DISTRIBUTED=1` 时本地默认跳过——否则单机跑分布式用例会因拉不起多进程而失败。这承接 [u6 分布式编程](u6-l1-distributed-overview.md) 的运行时要求。

#### 4.1.4 代码实践

**实践目标**：跑通一个最小的 Python 测试，体会 conftest 的种子固定与单文件运行机制。

**操作步骤**：

1. 先按 [CONTRIBUTING.md](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/CONTRIBUTING.md) 装好开发环境（见 4.4）。
2. 直接运行最简单的 kernel 测试：
   ```bash
   python testing/python/kernel/test_tilelang_kernel_element_wise_add.py
   ```
3. 或用 pytest 显式收集并观察用例数：
   ```bash
   python -m pytest testing/python/kernel/test_tilelang_kernel_element_wise_add.py -v
   ```

**需要观察的现象**：用例 `test_elementwise_add_f32` / `_f16` / `_i32` / `_f32f16` 各自被收集并通过；输出里能看到种子已被固定。

**预期结果**：4 个用例全部 PASSED。该测试文件内部用 `tilelang.compile` 编译一个 elementwise add kernel，并用 `profiler.assert_allclose(ref_program, ...)` 与 `torch.add` 比对（见 [test_tilelang_kernel_element_wise_add.py:53-61](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/testing/python/kernel/test_tilelang_kernel_element_wise_add.py#L53-L61)）。若机器无 GPU 则会因 `requires_cuda` 相关机制跳过——待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `conftest.py` 要在文件顶部 `os.environ["PYTHONHASHSEED"] = "0"`？

**参考答案**：Python 字典/hash 的迭代顺序默认受 `PYTHONHASHSEED` 随机化影响。固定为 0 后，不同进程、不同机器上由 hash 派生的顺序（例如某些集合迭代）才一致，避免「本地复现不了 CI 上的数值差异」。

**练习 2**：如果误把测试文件名改成了 `foo.py`（不带 `test_` 前缀），运行 `pytest` 会发生什么？conftest 的哪个机制会兜住？

**参考答案**：pytest 默认只收集 `test_*.py`，于是零用例被收集；正常情况下会显示「0 passed」看似无害。`pytest_terminal_summary` 钩子检测到没有任何真实用例，主动以返回码 5 报错退出，让事故暴露。

---

### 4.2 基准测试：benchmark 结构与运行

#### 4.2.1 概念说明

`benchmark/` 不验证正确性，而是**测量性能**，用来回答「我们的 kernel 跑多快、离上限多远」。它按算子族分目录：

- `matmul/` / `matmul_fp8/`：稠密 / FP8 矩阵乘，autotune 驱动，给出最优配置与 TFLOPS。
- `blocksparse_attention/`：块稀疏 FMHA，与 Triton / PyTorch / dense library 多方对比。
- `mamba2/`：Mamba 的 chunk scan，状态空间模型算子。
- `distributed/`：分布式集合通信基准（all_gather / all_to_all / reduce_scatter / gemm_rs / ag_gemm），以及 `ipc_impls/` 下对 NVSHMEM 与 unrolled-CP 两种 P2P 实现的对比。

与 `testing/` 不同，benchmark 文件大多是**可执行脚本**：直接 `python benchmark_xxx.py` 跑，参数从命令行读入。

#### 4.2.2 核心流程

以 `benchmark_matmul.py` 为例，它把 [u5-l1 Autotuner 框架](u5-l1-autotuner.md) 与 [u5-l2 Carver/Roller](u5-l2-carver-roller.md) 串起来：

1. `get_configs()` 生成待搜索的配置空间——要么是**手写笛卡尔积**，要么由 **Roller 代价模型**推荐（`--with_roller`）。
2. `@autotune(configs=...)` 叠在 `@jit` 之上，自动遍历配置、编译、测延迟、选最优。
3. `matmul(...)` 调用返回一个 `AutotuneResult`，含 `.latency` / `.config` / `.ref_latency`。
4. `__main__` 块按公式算出 TFLOPS 并打印，与参考实现（`A @ B.T`）的 TFLOPS 对比得到加速比。

#### 4.2.3 源码精读

**配置空间生成** —— [benchmark/matmul/benchmark_matmul.py:34-106](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/benchmark/matmul/benchmark_matmul.py#L34-L106)。不开 roller 时，用 `itertools.product` 对 7 个维度做笛卡尔积：

```python
iter_params = dict(
    block_M=[64, 128, 256], block_N=[64, 128, 256], block_K=[32, 64],
    num_stages=[0, 1, 2, 3], thread_num=[128, 256],
    policy=[T.GemmWarpPolicy.Square], enable_rasteration=[True, False],
)
```

这总共是 \(3\times3\times2\times4\times2\times1\times2 = 288\) 个候选配置，交给 autotuner 实测筛选。开 roller 时则走 `MatmulTemplate(...).recommend_hints(topk=10)`，由代价模型只给 top-10 高质量候选——这就是 [u5-l2](u5-l2-carver-roller.md) 讲的「先用代价模型缩小空间，再实测」。

**装饰器叠放** —— [benchmark/matmul/benchmark_matmul.py:109-116](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/benchmark/matmul/benchmark_matmul.py#L109-L116)：`@autotune` 必须在 `@jit` **之上**（autotuner 依赖 jit 的内部通道传候选配置，见 u5-l1）。`warmup=3, rep=20` 控制每个配置的预热与重复测量次数。

**kernel 主体** —— [benchmark/matmul/benchmark_matmul.py:161-216](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/benchmark/matmul/benchmark_matmul.py#L161-L216) 是一个标准 TileLang matmul：`T.alloc_shared` 放 A/B 子块、`T.alloc_fragment` 放累加器、`T.Pipelined(num_stages=...)` 做软件流水、`T.gemm(..., transpose_B=True)` 做矩阵乘。这正是 [u1-l3 quickstart](u1-l3-quickstart.md) 与 [u4-l2 软件流水线](u4-l2-software-pipeline.md) 的综合应用。

**结果计算** —— [benchmark/matmul/benchmark_matmul.py:219-251](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/benchmark/matmul/benchmark_matmul.py#L219-L251)：

```python
total_flops = 2 * M * N * K
best_result = matmul(M, N, K, with_roller)
print(f"Best TFlops: {total_flops / best_latency * 1e-9:.3f}")  # 此处用 1e-9 得 GFLOPS 量纲表述
```

注意脚本里把 `total_flops / latency * 1e-9` 当作「TFlops」打印（因 `total_flops` 是纯 FLOP 计数，除以秒再换算）。`benchmark/matmul/README.md` 记录了 H800 上的可复现结果（如 K=8192 时约 758 TFLOPS），可用作性能回归参考。

#### 4.2.4 代码实践

**实践目标**：跑一次 matmul 基准，理解 autotune 输出与 TFLOPS 计算。

**操作步骤**：

1. 进入目录并查看帮助：
   ```bash
   cd benchmark/matmul
   python benchmark_matmul.py --help
   ```
2. 用小一点的尺寸先跑（默认 16384³ 编译与搜索较慢）：
   ```bash
   python benchmark_matmul.py --m 2048 --n 2048 --k 2048
   ```
3. （可选）开 roller 缩小搜索空间再跑一次：
   ```bash
   python benchmark_matmul.py --m 2048 --n 2048 --k 2048 --with_roller
   ```

**需要观察的现象**：终端逐个打印候选配置；最后输出 `Best latency`、`Best TFlops`、`Best config`，以及参考实现的 `Reference TFlops`。

**预期结果**：得到一组延迟与 TFLOPS 数字，最优 `config` 是某个 `block_M/block_N/block_K/num_stages/...` 组合。开 roller 后候选数应从 288 降到约 10，搜索明显更快。具体数值依赖本机 GPU 型号——**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：默认（不开 roller）搜索空间有多大？怎么算出来的？

**参考答案**：288。由 `itertools.product` 对 7 个列表做笛卡尔积：\(3\times3\times2\times4\times2\times1\times2=288\)。

**练习 2**：为什么 `@autotune` 要叠在 `@jit` 之上而不是之下？

**参考答案**：autotuner 需要借助 `@jit`（JITImpl）的内部通道（`__tune_params`）把每个候选配置注入并触发一次编译，从而不污染对外函数签名。若反过来装饰，autotune 拿不到 jit 的编译能力。详见 [u5-l1](u5-l1-autotuner.md)。

---

### 4.3 代码格式化与 pre-commit

#### 4.3.1 概念说明

TileScale 是 C++（`src/`）+ Python（`tilelang/`）混合项目，所以格式化要同时管两类语言。项目用 **pre-commit** 作为统一入口，把多种检查串成钩子；`format.sh` 则是 pre-commit 的薄封装，额外接上 **clang-tidy**（C++ 静态检查）并智能地只检查「改动文件」。

参与的工具有：

- **ruff**：Python 的 lint（`ruff-check`，规则在 `pyproject.toml`）+ 格式化（`ruff-format`）。
- **clang-format**：C/C++ 格式化，版本与 `requirements-lint.txt` 对齐。
- **codespell**：拼写检查。
- **pymarkdown**：Markdown lint/修复。
- **pre-commit-hooks**：一组通用检查（行尾空格、文件结尾换行、大文件、私钥检测、YAML/TOML 合法性、`check-ast` 等）。

#### 4.3.2 核心流程

`format.sh` 默认行为：

1. 找到与上游 `tile-ai/tilelang`（TileScale 的上游）的 **merge-base**，确定「自上次合并点以来改了哪些文件」。
2. 若未装 pre-commit，自动 `pip install pre-commit`。
3. 对改动文件跑 `pre-commit run --files <改动文件>`。
4. 若环境里有 `run-clang-tidy` 且存在 `build/` 目录，再对改动的 C/C++ 文件跑 clang-tidy（可带 `-fix`）。
5. 最后检查 `git diff` 是否干净——如果格式化改动了文件，脚本以 `exit 1` 失败，提示你 review 并 stage。

#### 4.3.3 源码精读

**三种运行模式** —— [format.sh:28-54](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/format.sh#L28-L54)：无参=只检查改动文件；`--all`=全量；`--files a b c`=指定文件。

**merge-base 探测** —— [format.sh:56-72](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/format.sh#L56-L72)：先尝试 `git fetch` 上游 `tile-ai/tilelang` 的 main 取 merge-base；失败则退回 `origin/main`；再失败用本地 `main`。这反映 TileScale 是 TileLang 的 fork 这一历史（见 [u1-l1](u1-l1-project-overview.md)）。

**pre-commit 执行** —— [format.sh:91-111](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/format.sh#L91-L111)：用 `git diff --name-only --diff-filter=ACM` 收集「新增/修改」的文件，转成空格分隔传给 `pre-commit run --files`。

**最终干净性检查** —— [format.sh:172-181](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/format.sh#L172-L181)：`git diff --quiet` 若失败说明格式化产生了改动，脚本 `exit 1`。这是「CI 不该因为格式问题来回拉锯」的本地防线。

**钩子配置** —— [.pre-commit-config.yaml:10-59](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/.pre-commit-config.yaml#L10-L59)：依次声明 pre-commit-hooks、clang-format（`types_or: [c++, c]`）、ruff（`ruff-check --fix` + `ruff-format`）、codespell、pymarkdown。顶部 `exclude: '^(build|3rdparty)/.*$'` 把构建产物与上游子模块（CUTLASS/TVM）排除在外。

**ruff 规则与豁免** —— [pyproject.toml:148-170](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/pyproject.toml#L148-L170)：`target-version="py39"`、`line-length=140`；select 了 E/W/F/UP/B/SIM 等规则集，并 ignore 掉 E501（行长由 formatter 管）等。关键豁免在 [pyproject.toml:166-170](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/pyproject.toml#L166-L170)：`testing/**.py` 与 `examples/**.py` 关闭 `UP`/`FA` 规则——即测试与示例代码不必强制升级类型注解语法，降低贡献门槛。

#### 4.3.4 代码实践

**实践目标**：用 `format.sh` + pre-commit 把一段自己写的 kernel 代码整理到通过 lint。

**操作步骤**：

1. 先装好钩子（一次性）：
   ```bash
   pre-commit install --install-hooks
   ```
2. 在 `examples/` 下新建一个最小 kernel 文件 `my_add.py`，**故意**写得不规范（例如用单引号、行尾空格、import 不分组）。
3. 跑全量检查：
   ```bash
   pre-commit run --all-files
   ```
   或只检查自己的文件：
   ```bash
   bash format.sh --files examples/my_add.py
   ```
4. 若被自动修复，review 改动并 `git add`。

**需要观察的现象**：ruff-format 把引号统一为双引号、codespell 报拼写可疑词、trailing-whitespace 清掉行尾空格；`format.sh` 在最后提示「Reformatted files. Please review and stage the changes.」并 `exit 1`。

**预期结果**：手动修掉 lint 报错后再跑，输出「All checks passed」（[format.sh:183](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/format.sh#L183)）。因为 `examples/**` 豁免了 UP/FA，类型注解相关的 pyupgrade 规则不会找你麻烦。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `format.sh` 要在最后做一次 `git diff --quiet` 检查？

**参考答案**：格式化工具（ruff-format、clang-format）可能自动改写了文件，导致工作区不干净。若不检查，贡献者可能把「未格式化」的代码提交上去，CI 又会因此失败。`exit 1` 强制要求本地先 review 并 stage 这些自动改动。

**练习 2**：在 `testing/` 下写代码时，为什么 ruff 不会强制你把 `Optional[X]` 改成 `X | None`？

**参考答案**：因为 [pyproject.toml](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/pyproject.toml#L166-L170) 的 `per-file-ignores` 对 `testing/**.py` 关闭了 `UP`（pyupgrade）规则。这是项目为降低测试代码贡献门槛的有意豁免。

---

### 4.4 CI 矩阵与贡献流程

#### 4.4.1 概念说明

CI（`.github/workflows/ci.yml`）在每次 PR 上自动跑两个 job：

- **lint**：用**最低支持版本 Python 3.8** 做 AST 编译检查（提前暴露使用了 3.9+ 才有的语法），再用 Python 3.9 跑 `pre-commit run --all-files`。
- **tests**：在 **self-hosted NVIDIA** 机器上，用 **CUDA-12.8 + Python 3.12** 编译安装项目，跑 `examples/` 与 `testing/python/` 的全部用例。

> 说明：项目大纲曾设想 cuda/amd/各 CUDA 版本的大矩阵，但**当前 ci.yml 实际只配了单个矩阵条目**（self-hosted NVIDIA + CUDA-12.8 + Python 3.12），ROCm/Metal 分支在脚本里以注释形式保留、尚未启用。所以「多后端 CI」目前是待扩展项，并非已落地。

#### 4.4.2 核心流程

一次完整贡献的生命周期：

1. **fork + clone**：在 GitHub fork 仓库，本地 `git clone --recurse-submodules`（必须带子模块，否则缺 `3rdparty/tvm`）。
2. **建开发环境**：用 `uv venv` 建虚拟环境，装 `requirements-dev.txt`，`pre-commit install`。
3. **editable 安装**：`pip install --no-build-isolation --editable .` 把 C++ 编成 `.so` 并让 Python 改动即时生效。
4. **写代码 + 加测试**：改源码，在 `testing/` 下补对应测试。
5. **本地验证**：`pre-commit run --all-files`（或 `bash format.sh`）+ `python -m pytest testing`。
6. **提 PR**：推送分支、开 PR；CI 自动跑 lint + tests。

#### 4.4.3 源码精读

**lint job** —— [.github/workflows/ci.yml:37-75](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/.github/workflows/ci.yml#L37-L75)：先用 Python 3.8 跑 `compileall`（[ci.yml:55-57](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/.github/workflows/ci.yml#L55-L57)）确保 `tilelang` 包能在最低支持版本上至少通过 AST 编译；再用 Python 3.9 跑 pre-commit 全量检查（[ci.yml:70-75](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/.github/workflows/ci.yml#L70-L75)）。

**tests job 与矩阵** —— [.github/workflows/ci.yml:77-95](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/.github/workflows/ci.yml#L77-L95)：`matrix.runner` 只有一项 self-hosted NVIDIA（CUDA-12.8），`matrix.python-version` 为 `"3.12"`，`fail-fast: false`。该 job 通过 `if` 条件限定只在 `tile-ai` 组织且**非 draft PR** 时运行（[ci.yml:79-82](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/.github/workflows/ci.yml#L79-L82)）。

**安装项目 + DeepEP** —— [.github/workflows/ci.yml:248-252](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/.github/workflows/ci.yml#L248-L252)：`uv pip install -v .` 以 wheel 形式装项目，并 `bash tilelang/distributed/install_deepep.sh` 装上 DeepEP 供分布式测试用（承接 [u6-l6 DeepEP](u6-l6-deepep.md)）。

**分布式与非分布式用例分跑** —— [.github/workflows/ci.yml:294-333](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/.github/workflows/ci.yml#L294-L333)：先以 `TILELANG_USE_DISTRIBUTED=1` + `-m distributed` 单进程跑分布式用例，再以 `-m "not distributed"` 多进程（`--numprocesses=2`，pytest-xdist）跑其余用例；两段都带 `|| true`（best-effort，不因部分用例失败挂掉整条 CI），并用 `--maxfail=3` 控制失败上限。这正是 4.1 讲的 `requires_distributed` 标记的 CI 用法。

**贡献规范** —— [CONTRIBUTING.md:39-68](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/CONTRIBUTING.md#L39-L68) 给出环境搭建步骤；[CONTRIBUTING.md:70-84](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/CONTRIBUTING.md#L70-L84) 的 editable 安装强调 `--no-build-isolation`（因为构建依赖已在 `requirements-dev.txt` 里）；[CONTRIBUTING.md:86-102](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/CONTRIBUTING.md#L86-L102) 给出 lint 与 test 的本地命令：

```bash
pre-commit run --all-files        # Lint Check
python3 -m pytest testing         # Test Locally
```

**wheel 冒烟测试** —— [pyproject.toml:232-234](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/pyproject.toml#L232-L234)：`cibuildwheel` 的 `test-command` 仅做 `import tilelang; print(tilelang.__version__)` 的最小 import 冒烟，确保打包出来的 wheel 能正常加载底层 `.so`（承接 [u1-l2 安装](u1-l2-installation.md) 与 [u1-l4 库加载链](u1-l4-repo-structure.md)）。

#### 4.4.4 代码实践

**实践目标**：走一遍完整的本地贡献前置检查（不实际开 PR，只验证本地通过）。

**操作步骤**：

1. fork 并克隆（带子模块）：
   ```bash
   git clone --recurse-submodules git@github.com:<你的用户名>/tilescale.git
   cd tilescale
   ```
2. 建环境并装钩子：
   ```bash
   uv venv --seed .venv && source .venv/bin/activate
   uv pip install -r requirements-dev.txt
   pre-commit install --install-hooks
   pip install --no-build-isolation --editable .
   ```
3. 跑 lint（两种等价方式）：
   ```bash
   pre-commit run --all-files
   # 或
   bash format.sh --all
   ```
4. 跑一个测试子集（全量较慢，先跑一个目录）：
   ```bash
   python -m pytest testing/python/kernel -v
   ```

**需要观察的现象**：pre-commit 第一次会把 hook 环境装好（稍慢），之后各钩子依次跑过；测试用例被收集并执行。

**预期结果**：lint 全绿（`All checks passed`）；指定目录的测试 PASSED 或在无 GPU 时 SKIPPED。若 lint 报错，按提示修后重跑。完整 CI 矩阵的真实执行——**待本地/CI 验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么 lint job 要用 Python 3.8 跑 `compileall`？

**参考答案**：项目声明最低支持 Python 3.8/3.9。用最低版本做 AST 编译能在 CI 早期就暴露「误用了高版本才有的语法」（如 3.10 的 `match`、3.9 的内置泛型 `list[int]`），避免在低版本用户机上才崩。

**练习 2**：CI 里分布式测试为什么用 `-m distributed` 而**非分布式**用 `-m "not distributed"`，而且前者单进程、后者多进程？

**参考答案**：分布式用例依赖多进程 + NVSHMEM 环境（`TILELANG_USE_DISTRIBUTED=1`），并发跑会冲突，故 `--numprocesses=1`；普通用例互相独立，用 pytest-xdist 的 `--numprocesses=2` 并行加速。用 `-m distributed` / `not distributed` 两段分跑，正是 4.1 中 `requires_distributed` 打的 `distributed` 标记的消费端。

---

## 5. 综合实践

把本讲四个模块串成一个端到端的「小型贡献」演练：

1. **写一个新 kernel 测试**：参考 [test_tilelang_kernel_element_wise_add.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/testing/python/kernel/test_tilelang_kernel_element_wise_add.py)，在 `testing/python/kernel/` 下新建 `test_my_relu.py`，用 TileLang 写一个 relu kernel，用 `profiler.assert_allclose(torch.relu)` 校验。
2. **本地跑测试**：`python test_my_relu.py`（借助 `tilelang.testing.main()`）和 `python -m pytest test_my_relu.py -v` 各跑一次，确认 PASSED。
3. **跑性能基准对比**：把你的 relu kernel 套进 `benchmark/matmul/benchmark_matmul.py` 的结构（或直接在测试里用 `kernel.get_profiler().do_bench()`），记录延迟。
4. **过 lint**：`bash format.sh --files testing/python/kernel/test_my_relu.py`，修掉所有报错直到 `All checks passed`。
5. **模拟 CI 顺序**：先 `pre-commit run --all-files`（对应 lint job），再 `python -m pytest testing/python/kernel`（对应 tests job 的一个子集）。

完成上述五步，你就走完了「写代码 → 测正确性 → 测性能 → 格式化 → 模拟 CI」的完整本地闭环，提 PR 时 CI 的大概率能一次过。

## 6. 本讲小结

- TileScale 测试以 pytest 为骨架，`testing/python/` 按子系统分目录；`testing/conftest.py` 固定随机种子并兜底「零用例被收集」；`tilelang.testing` 提供 `main()` 单文件运行、`requires_distributed` 与按 SM 架构跳过的装饰器。
- `testing/cpp/` 目前仅占位，C++ 测试尚未落地。
- `benchmark/` 按算子族分目录、是可执行脚本；`benchmark_matmul.py` 用 `@autotune`+`@jit` 叠放，默认搜索空间 288 个配置，开 roller 缩到 top-10，输出最优延迟/TFLOPS。
- 格式化统一走 pre-commit：ruff 管 Python（lint+format）、clang-format 管 C++、外加 codespell/pymarkdown；`format.sh` 是其封装，默认只查改动文件并以 `git diff --quiet` 守门。
- 当前 CI（`ci.yml`）实际只有 self-hosted NVIDIA + CUDA-12.8 + Python 3.12 的单矩阵条目；lint 用 Python 3.8 做 AST 检查 + 3.9 跑 pre-commit；tests 把分布式与非分布式用例分两段跑。
- 贡献流程：fork → `--recurse-submodules` 克隆 → `uv` 建环境 → `pre-commit install` → `--no-build-isolation --editable .` 安装 → `pre-commit run --all-files` + `pytest testing` → 提 PR。

## 7. 下一步学习建议

- **回到 Unit 7 主线**：本讲是工具与流程课，若你想深入「被测试的对象本身」，建议读 [u7-l4 Transform pass 深入与扩展](u7-l4-transform-extend.md)——那里讲了如何新增一道编译 pass，而新增 pass 的最后一步正是「加测试」，可与本讲的测试体系对照。
- **性能回归**：若你想把 benchmark 数字做成 CI 回归门禁，可深入 [tilelang/testing/perf_regression.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/testing/perf_regression.py) 的 `process_func` / `regression`，结合 `TL_PERF_REGRESSION_FORMAT=json` 做自动化采集。
- **分布式测试**：想真正跑通 `-m distributed` 那段 CI，需要先掌握 [u6-l4 pynvshmem 与启动](u6-l4-pynvshmem-launch.md) 的多进程环境搭建。
- **打包发布**：对发 wheel 感兴趣可细读 `pyproject.toml` 的 `[tool.cibuildwheel]` 段，理解 manylinux 镜像、CUDA 运行时注入与 `repair-wheel-command` 如何剥离 `libtvm_ffi.so`/`libnvshmem*` 等外部依赖。
