# 测试与 examples 基础设施

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 tilelang-metax 仓库里 **两套测试根目录**（`examples/` 与 `testing/`）各自的职责与组织方式。
- 看懂 `conftest.py` 在测试启动前做了哪些「全局装配」（随机种子、torch 扩展目录隔离、空集守卫、性能测试开关）。
- 用 `tilelang.testing` 提供的后端感知标记（`requires_cuda` / `skip_on_maca` / `requires_cuda_or_cdna`）写出一个跨后端都能正确「跑或跳」的 pytest 用例。
- 区分 `requirements-test-*.txt` 的分层结构，理解为什么 MACA 的测试依赖文件几乎是空的。
- 为自己写的 kernel（elementwise / layer norm）补一个数值正确性校验的 pytest 用例。

本讲是整个学习手册的收尾篇之一：前面你已经会写 kernel、会调优、会剖析性能，本讲教你把它们**固化成可回归、可在 CI 上按后端自动筛跑的测试**。

## 2. 前置知识

在进入测试源码前，先用三句话补两个背景概念。

**pytest 与 conftest。** pytest 是 Python 最常用的测试框架。它约定把测试函数命名为 `test_*`，把测试文件命名为 `test_*.py`，运行时自动收集。`conftest.py` 是 pytest 的「本地装配文件」——它不会被当作测试收集，但里面的若干固定名字的钩子函数（如 `pytest_collection_modifyitems`、`pytest_terminal_summary`）会在测试生命周期的特定时机被 pytest 自动调用，常用来改写收集到的测试项、注册命令行选项、做全局初始化。一个目录树下可以有多个 `conftest.py`，各自只对自己的子树生效。

**测试夹具（fixture）与标记（marker）。** 标记是挂在测试函数上的注解（如 `@pytest.mark.skipif(...)`），用来声明「这个测试在什么条件下才跑」；本讲里 tilelang 的后端感知标记本质上都是 `pytest.mark.skipif` 的封装。理解了这两点，下面的源码就是「在固定钩子里填业务逻辑」。

**一个贯穿全讲的关键事实：在 tilelang-metax 里，MACA 被视为「类 CUDA」后端。** 这点在 u7 系列已建立（MACA 复用 CUDA 风格的 runtime、`warp_size=64`），它直接决定了测试基础设施的形态：很多「CUDA 专属」的测试标记在 MACA 上**也会通过**，于是需要专门一个 `skip_on_maca` 把真正不兼容的测试剔出去。这是本讲反复出现的主题。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [examples/conftest.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/conftest.py) | `examples/` 测试树的装配文件：随机种子、torch 扩展目录隔离、CuTeDSL 已知失败标记、空集守卫 |
| [testing/conftest.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/testing/conftest.py) | `testing/` 测试树的装配文件：在 `sys.path` 前插仓库根、`--run-perf` 开关、性能测试默认跳过、空集守卫 |
| [examples/pytest.ini](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/pytest.ini) | `examples/` 的 pytest 配置，目前只排除一个子目录 |
| [tilelang/testing/__init__.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/testing/__init__.py) | 测试工具模块：后端感知的跳过标记、`main()` 入口、随机种子工具 |
| [tilelang/testing/perf_regression.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/testing/perf_regression.py) | 性能回归测试框架：发现并运行 `regression_*` 函数，输出表格或 JSON |
| [tilelang/utils/tensor.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/utils/tensor.py) | `torch_assert_close`：允许少量元素失配的数值校验函数 |
| [requirements-test-maca.txt](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/requirements-test-maca.txt) | MACA 专属测试依赖（目前为空，只继承公共依赖） |
| [requirements-test-cuda.txt](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/requirements-test-cuda.txt) | CUDA 专属测试依赖（含 flash-attn、cuda-python 等） |
| [examples/gemm/test_example_gemm.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/gemm/test_example_gemm.py) | 「示例即测试」模式的最薄包装范例 |
| [.github/workflows/ci.yml](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/.github/workflows/ci.yml) | CI 流水线：按 toolkit（CUDA/ROCm/MACA/Metal）矩阵装依赖、跑测试 |

## 4. 核心概念与源码讲解

### 4.1 测试目录全景：examples 与 testing 双根

#### 4.1.1 概念说明

tilelang-metax 有**两个并行的测试根目录**，理解它们的分工是本讲的地基：

- `examples/`：既是**教学示例库**（你从 u1-l4 起一直在读它），又是**端到端算子正确性测试集**。每个算子目录（如 `examples/gemm/`、`examples/elementwise/`）里通常有三个文件：`example_*.py`（kernel + `main()` 做数值校验）、`test_example_*.py`（pytest 薄包装）、可选的 `regression_example_*.py`（性能回归）。CI 直接 `pytest ../examples` 把它们当测试跑。
- `testing/`：编译器**细粒度单元测试**，针对单个 pass、单个算子、单个 intrinsic、JIT 缓存等内部机制，**不**跑完整算子。它的子树 `testing/python/` 按编译器子系统分目录（`transform/`、`layout/`、`jit/`、`cache/`、`language/`、`cuda/`、`amd/`、`issue/`……），`testing/cpp/` 目前只有一个 `.gitkeep` 占位，预留给未来的 C++ gtest。

一句话区分：`examples/` 答「这个算子算得对不对、快不快」，`testing/` 答「编译器这一级降级/缓存/布局推断对不对」。

#### 4.1.2 核心流程

两套测试在 CI 里的运行方式高度一致——都是「`cd testing` 后用 pytest 跑」，只是目标路径不同：

```text
CI（ci.yml）
 ├── cd testing && pytest ../examples      # 端到端算子正确性（examples 树）
 └── cd testing && pytest ./python          # 编译器单元测试（testing 树）
```

`testing/python/` 下按子系统组织的子目录（节选）及其含义：

| 子目录 | 测试对象 |
|--------|----------|
| `transform/` | 各 lowering pass（`lower_tile_op`、`inject_pipeline` 等） |
| `layout/` | Layout Inference、swizzle |
| `jit/` | JIT 编译、各执行后端（tvm_ffi / cython / nvrtc）、缓存 |
| `cache/` | KernelCache 原子落盘、CUDA binary cache |
| `language/` | DSL 语法（copy、cluster、async_copy 等） |
| `cuda/` `amd/` `metal/` | 后端专属（MFMA intrinsic、TMA、codegen 细节） |
| `issue/` | 针对 GitHub issue 的回归测试（文件名带 issue 号） |
| `debug/` | `T.print`、pass diff、device assert |
| `autotune/` | 自动调优器 |

注意：**没有 `maca/` 子目录**。MACA 没有独立的编译器单元测试集——它复用 `cuda/` 下的测试（因为 MACA 是「类 CUDA」后端），靠测试标记决定哪些跑、哪些跳。

#### 4.1.3 源码精读

先看「示例即测试」的最薄包装。一个完整的 GEMM 算子示例被 pytest 化，只需要这么几行：

```python
# examples/gemm/test_example_gemm.py
import tilelang.testing
import example_gemm_intrinsics
import example_gemm

def test_example_gemm_intrinsics():
    example_gemm_intrinsics.main(M=1024, N=1024, K=1024)

def test_example_gemm():
    example_gemm.main()

if __name__ == "__main__":
    tilelang.testing.main()
```

见 [examples/gemm/test_example_gemm.py:1-15](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/gemm/test_example_gemm.py#L1-L15)。这里有三件事值得注意：

1. `import example_gemm`（**无包名前缀**）能成立，是因为 pytest 以 `examples/` 为根目录收集时，把每个算子目录都加进了 `sys.path`。`test_*` 函数体里直接调用示例脚本的 `main()`，而 `main()` 内部已含数值校验（`torch.testing.assert_close` 或 `tilelang.testing.torch_assert_close`）。
2. `if __name__ == "__main__"` 分支调 `tilelang.testing.main()`，让你**单文件**也能跑：`python test_example_gemm.py` 等价于 `pytest test_example_gemm.py`。`main()` 的实现见 [tilelang/testing/__init__.py:126-128](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/testing/__init__.py#L126-L128)，它用 `inspect.getsourcefile(sys._getframe(1))` 取到调用者自身的文件路径，再交给 `pytest.main`。
3. elementwise 示例的测试包装更典型，一行调用 `main()` 即可，见 [examples/elementwise/test_example_elementwise.py:1-10](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/elementwise/test_example_elementwise.py#L1-L10)。

这种「示例自带 `main()`、测试只转发调用」的模式意味着：**你每写一个示例，几乎免费得到一个测试**。被测的 `main()` 本身长什么样？以 elementwise 为例：

```python
# examples/elementwise/example_elementwise_add.py
def main(M=1024, N=1024):
    a = torch.randn(M, N, dtype=torch.float32, device="cuda")
    b = torch.randn(M, N, dtype=torch.float32, device="cuda")
    out = elementwise_add(a, b, block_M=32, block_N=32, threads=128,
                          in_dtype=T.float32, out_dtype=T.float32)
    torch.testing.assert_close(out, ref_program(a, b), rtol=1e-2, atol=1e-2)
```

见 [examples/elementwise/example_elementwise_add.py:35-41](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/elementwise/example_elementwise_add.py#L35-L41)。这就是一个「造输入 → 跑 kernel → 对参考实现比数值」的最小骨架，任何新算子都可以照抄。

#### 4.1.4 代码实践

**实践目标：** 亲手验证「示例即测试」链路，确认一条用例到底在跑什么。

**操作步骤：**

1. 打开 `examples/elementwise/example_elementwise_add.py`，找到 `main()`，确认它内部有 `torch.testing.assert_close`。
2. 打开同目录 `test_example_elementwise.py`，确认它只是转发调用 `main()`。
3. 在仓库根目录执行（需要已安装 tilelang 与可用 GPU 后端）：
   ```bash
   cd testing
   uv run --no-project -m -- pytest -v ../examples/elementwise/test_example_elementwise.py
   ```
   或单文件直接跑：
   ```bash
   python examples/elementwise/test_example_elementwise.py
   ```

**需要观察的现象：** pytest 收集到 `test_example_elementwise_add` 一条用例并 PASSED；用例日志里能看到 torch 造输入、调 kernel 的过程。

**预期结果：** 1 passed。若无 GPU 或后端不可用，用例应被 `requires_cuda` 之类标记跳过（SKIPPED）而非报错——这取决于示例 `main()` 自身是否加了后端守卫，elementwise 的 `main()` 未加守卫，故无设备时会因 `device="cuda"` 失败；正式 CI 在有设备的环境跑。**若你无法在本地运行，请标注「待本地验证」并只做源码阅读。**

#### 4.1.5 小练习与答案

**练习 1：** `examples/` 下的测试为什么能 `import example_gemm`（无目录前缀）？

**参考答案：** pytest 以 `examples/` 为 rootdir 收集时，会把每个含有 `test_*.py` 的目录加入 `sys.path`（pytest 的默认 rootdir/import mode 行为），因此同目录的 `example_gemm.py` 可直接按模块名导入。

**练习 2：** `testing/python/` 下为什么没有 `maca/` 子目录？

**参考答案：** MACA 是「类 CUDA」后端，编译器内部机制（布局推断、pass 流水线、JIT 缓存等）与 CUDA 同构，因此复用 `cuda/`、`transform/`、`jit/` 等公共测试，靠 `tilelang.testing` 的后端标记（`requires_cuda` 对 MACA 成立、`skip_on_maca` 剔除不兼容项）来筛跑，无需另立一套。

---

### 4.2 conftest.py：测试全局装配与守卫

#### 4.2.1 概念说明

两个测试根目录各有一个 `conftest.py`，它们解决同一类问题：**在 pytest 收集和运行之前，把测试环境调到「确定、隔离、可观测」的状态**。具体承担四件事：

1. **确定性**：钉死 Python 哈希种子、`random`/`torch`/`numpy` 的随机种子，让「过不过」不随运行波动。
2. **并行隔离**：当用 `pytest-xdist` 多进程并行跑测试时，为每个 worker 分配独立的 torch 扩展编译目录，避免多个 worker 同时编译 Cython 扩展时互相踩踏。
3. **按需筛跑**：`testing/conftest.py` 默认跳过性能测试（`@pytest.mark.perf`），只有显式加 `--run-perf` 才跑；`examples/conftest.py` 在 `TILELANG_TARGET=cutedsl` 时自动给已知失败的用例打 `xfail`。
4. **空集守卫**：如果一次 pytest 收集后「没有任何用例被执行」（全被跳过/取消选择），就以非零码退出，防止 CI 把「什么都没跑」误判成「全绿」。

#### 4.2.2 核心流程

以 `testing/conftest.py` 为例，其装配流程：

```text
conftest 加载（收集前）
 ├── 设 PYTHONHASHSEED=0
 ├── 把 REPO_ROOT 插到 sys.path[0]   # 保证用仓库内 tilelang，而非全局装的
 ├── _configure_torch_extensions_dir() # 每个 worker 一个独立扩展目录
 ├── 钉死 random/torch/numpy 种子
 │
 ├── pytest_addoption()              # 注册 --run-perf 命令行选项
 │
收集完成后
 └── pytest_collection_modifyitems()  # 若未给 --run-perf，给所有 perf 用例加 skip
     │
运行结束
 └── pytest_terminal_summary()        # 统计：若除 skipped/deselected 外无任何用例被执行 → exit(5)
```

#### 4.2.3 源码精读

**（a）torch 扩展目录的并行隔离。** 两个 conftest 都有这段（仅变量名不同）：

```python
def _configure_torch_extensions_dir():
    cache_dir = os.environ.get("TILELANG_CACHE_DIR", os.path.expanduser("~/.tilelang/cache"))
    worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
    path = os.path.join(cache_dir, "torch_extension", f"{worker}-{os.getpid()}")
    os.makedirs(path, exist_ok=True)
    os.environ["TORCH_EXTENSIONS_DIR"] = path
```

见 [examples/conftest.py:8-16](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/conftest.py#L8-L16) 与 [testing/conftest.py:15-23](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/testing/conftest.py#L15-L23)。`PYTEST_XDIST_WORKER` 是 `pytest-xdist` 给每个并行 worker 设的环境变量（如 `gw0`、`gw1`），单进程时取默认 `"main"`；再拼上 `os.getpid()`，保证每个进程的 `TORCH_EXTENSIONS_DIR` 唯一。这一点对 tilelang 很关键——它的某些执行后端（如 cython）会用 torch 的 cpp extension 机制即时编译，多 worker 共用目录会触发竞态。

**（b）保证用「仓库内」的 tilelang。** 这是 `testing/conftest.py` 独有、`examples/conftest.py` 没有的一段：

```python
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
```

见 [testing/conftest.py:10-12](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/testing/conftest.py#L10-L12)。编译器单元测试要求测的是「当前 checkout 的源码」，而非系统里 `pip install` 的某个旧版本，故把仓库根强制插到 `sys.path` 最前。

**（c）性能测试的默认跳过与 `--run-perf`。** 这是 `testing/conftest.py` 的独有能力：

```python
def pytest_addoption(parser):
    parser.addoption("--run-perf", action="store_true", default=False,
                     help="run performance and benchmark-oriented tests")

def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-perf"):
        config._perf_items_filtered = 0
        return
    perf_skip = pytest.mark.skip(reason="performance test skipped by default; pass --run-perf to include it")
    perf_items_filtered = 0
    for item in items:
        if item.get_closest_marker("perf") is not None:
            item.add_marker(perf_skip)
            perf_items_filtered += 1
    config._perf_items_filtered = perf_items_filtered
```

见 [testing/conftest.py:45-65](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/testing/conftest.py#L45-L65)。带 `@pytest.mark.perf` 的用例（如 [testing/python/jit/test_tilelang_jit_tvm_ffi.py:209-211](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/testing/python/jit/test_tilelang_jit_tvm_ffi.py#L209-L211) 的 `test_tvm_ffi_kernel_do_bench`）默认不跑——性能测试慢且对机器噪声敏感，平时只做正确性回归，性能回归需显式开启。

**（d）CuTeDSL 已知失败的自动标记。** 这是 `examples/conftest.py` 独有的逻辑：

```python
CUTEDSL_KNOWN_FAILURES = {
    "minference/test_vs_sparse_attn.py::test_vs_sparse_attn",
    "deepseek_v4/test_tilelang_example_deepseek_v4.py::test_example_act_quant",
}

def pytest_collection_modifyitems(config, items):
    if os.environ.get("TILELANG_TARGET") != "cutedsl":
        return
    for item in items:
        nid = item.nodeid
        if _match_any(nid, CUTEDSL_KNOWN_FAILURES):
            item.add_marker(pytest.mark.xfail(
                reason="CuTeDSL: known limitation (unimplemented op or flaky)", strict=False))
```

见 [examples/conftest.py:41-67](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/conftest.py#L41-L67)。当 CI 把 `TILELANG_TARGET` 设为 `cutedsl`（见 4.4.3 节）时，这两个已知在 CuTeDSL 后端上跑不过的用例被自动标成 `xfail(strict=False)`——预期失败，意外通过也不报错。这是一种「渐进迁移」策略：新后端不必一次性把所有示例跑通，先把已知坏例登记在表里。

**（e）空集守卫。** 两个 conftest 都有 `pytest_terminal_summary`，逻辑是：若除 `skipped`/`deselected` 外，`passed/failed/xfailed/...` 的总和为零，就判定「没收集到任何用例」，以 returncode=5 退出：

```python
known_types = {"failed","passed","skipped","deselected","xfailed","xpassed","warnings","error"}
executed = sum(len(terminalreporter.stats.get(k, [])) for k in known_types.difference({"skipped","deselected"}))
if executed == 0:
    ...
    pytest.exit("No tests were collected.", returncode=5)
```

见 [examples/conftest.py:70-87](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/conftest.py#L70-L87) 与 [testing/conftest.py:68-83](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/testing/conftest.py#L68-L83)。`testing/conftest.py` 版本还多一个分支：如果跳过的全是 perf 用例，则只打印提示而非报错（见 [testing/conftest.py:71-77](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/testing/conftest.py#L71-L77)）。这个守卫的现实意义：CI 上若某个后端把全部用例都 `skip` 了（比如标记配错、target 探测失败），会立刻被这条规则抓住，而不是静默「全绿」。

#### 4.2.4 代码实践

**实践目标：** 体验 `--run-perf` 与空集守卫的真实行为。

**操作步骤：**

1. 在 `testing/` 下先不加 `--run-perf` 跑一个含 perf 标记的文件，观察它被跳过：
   ```bash
   cd testing
   uv run --no-project -m -- pytest -v ./python/jit/test_tilelang_jit_tvm_ffi.py -k perf
   ```
2. 再加 `--run-perf` 重跑同一条，观察它是否变为执行（取决于是否有 GPU）。
3. 故意构造空集：用一个匹配不到任何用例的表达式 `-k nonexistent_name`，观察退出码。

**需要观察的现象：** 第 1 步 perf 用例显示 SKIPPED，且终端出现 `Skipped N perf test(s). Re-run with --run-perf to include them.` 的分隔线；第 3 步若确实「除 skipped/deselected 外无执行」则出现 `Error: No tests were collected.` 并以 returncode=5 退出。

**预期结果：** SKIP/SKIPPED（第 1 步）；exit code 5（第 3 步，待本地验证）。无环境时仅做源码阅读即可，重点理解 `pytest_collection_modifyitems` 与 `pytest_terminal_summary` 的协作。

#### 4.2.5 小练习与答案

**练习 1：** 为什么 `TORCH_EXTENSIONS_DIR` 要拼上 `worker-{pid}`？

**参考答案：** `pytest-xdist` 多 worker 并行时，若所有进程共用同一个扩展目录，多个 worker 同时触发 torch cpp extension 的即时编译会写同名临时文件、互相覆盖或读到半成品；按 `worker + pid` 分目录后每个进程独占，消除竞态。

**练习 2：** 如果一个 PR 不小心把某后端的全部用例都标成了 skip，CI 会怎样？

**参考答案：** `pytest_terminal_summary` 会发现「除 skipped/deselected 外无可执行用例」，打印 `Error: No tests were collected.` 并以 returncode=5 退出，CI 步骤失败——从而把「静默全绿」暴露为显式错误。

---

### 4.3 用 pytest 写 kernel 测试：标记、校验与入口

#### 4.3.1 概念说明

写一个 kernel 测试本身不难（4.1 已示范「转发 `main()`」），**难点在于让它跨多个后端都行为正确**：同一份测试在 CUDA 上要跑、在 MACA 上有的要跑有的要跳、在无 GPU 的机器上要优雅 skip、在算力不够的卡上要按架构版本跳。tilelang 把这套「后端感知的跳过判定」集中收口在 `tilelang/testing/__init__.py`，对外暴露成一组装饰器。

核心思想：**每个标记本质都是「先探测当前 target，再返回一个 `pytest.mark.skipif(...)`」**。探测用的是 u3-l1 讲过的 `determine_target("auto", return_object=True)`——它走 target 检测注册表（CUDA→HIP→Metal→MACA），失败返回 `False` 表示「当前不是这个后端」，于是用例被 skip。

另外一个常被忽略但很重要的工具是 `torch_assert_close`：它比 `torch.testing.assert_close` 多一个 `max_mismatched_ratio` 参数，**允许一定比例的元素失配**——这对低精度（fp16/bf16/fp8）张量核 kernel 至关重要，因为个别元素的舍入误差不可避免。

#### 4.3.2 核心流程

一个后端感知的 kernel 测试，从被收集到执行/跳过的判定流程：

```text
pytest 收集到 test_xxx
 │
装饰器 tilelang.testing.requires_cuda 在「导入时」执行
 ├── _check_is_maca() → determine_target("auto") → target_is_maca(target)
 │     （此刻就定好 is_maca 是 True/False，烘焙进 skipif）
 │
运行到该用例
 └── pytest 查看其 skipif 标记
      ├── 条件成立 → SKIPPED（带 reason）
      └── 条件不成立 → 执行函数体 → torch_assert_close(C, ref) → PASSED/FAILED
```

关键点：**判定发生在导入期**（装饰器函数体执行时），结果被「烘焙」进 `skipif`。这意味着标记依赖当前机器的实际 target 探测结果，所以同一份测试文件在不同 CI runner 上自动表现不同。

#### 4.3.3 源码精读

**（a）「类 CUDA」判定：MACA 被算作 CUDA。** 这是理解整个标记体系的关键一例：

```python
def requires_cuda(func):
    is_maca = _check_is_maca()
    marks = [
        pytest.mark.skipif(not is_maca, reason="Requires CUDA like target"),
    ]
    return _compose([func], marks)
```

见 [tilelang/testing/__init__.py:65-73](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/testing/__init__.py#L65-L73)。注意函数名叫 `requires_cuda`，**但实际放行条件是「当前是 MACA」**——reason 写的是 "Requires CUDA like target"。这并非 bug，而是 metax 分支的有意改动：在 metax fork 里，MACA 承担了原本 CUDA 示例/测试的角色（CI 主力后端是 MACA），所以「类 CUDA」测试要对 MACA 放行。`_check_is_maca` 的探测逻辑：

```python
def _check_is_maca() -> bool:
    try:
        target = determine_target("auto", return_object=True)
        return target_is_maca(target)
    except (ValueError, RuntimeError):
        return False
```

见 [tilelang/testing/__init__.py:57-62](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/testing/__init__.py#L57-L62)。`target_is_maca` 来自 `tilelang.maca.target`（u3-l3 讲过的 MACA 可用性检测）；探测不到（无 SDK / 无设备）返回 `False`，用例被 skip。

**（b）反向操作：`skip_on_maca` 剔除 MACA 不兼容项。** 既然 `requires_cuda` 对 MACA 放行，那些真正只属于 NVIDIA CUDA（如 SM80+ 的向量化、nvrtc 专属行为）的测试就需要一个反向开关：

```python
def skip_on_maca(func):
    is_maca = _check_is_maca()
    marks = [
        pytest.mark.skipif(is_maca, reason="Skip on MACA target"),
    ]
    return _compose([func], marks)
```

见 [tilelang/testing/__init__.py:76-84](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/testing/__init__.py#L76-L84)。实际用法是「叠标记」——一个用例同时挂 `skip_on_maca` 和 `requires_cuda`，表示「类 CUDA 后端都跑，但 MACA 除外」。真实例子见 [testing/python/cuda/test_cuda_f32x2_intrinsics.py:252-255](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/testing/python/cuda/test_cuda_f32x2_intrinsics.py#L252-L255)：

```python
@tilelang.testing.skip_on_maca
@tilelang.testing.requires_cuda
@pytest.mark.parametrize("op_key", _AUTO_VEC_OP_NAMES)
def test_codegen_auto_vec_f32_no_sm80(op_key):
    ...
```

这条用例验证「SM80 之前不应生成 `tl::<op>2` 向化」，是 NVIDIA 架构专属语义，故在 MACA 上跳过。

**（c）按算力版本跳过。** `requires_cuda_compute_version(mode="ge"|"gt"|...)` 把 NVIDIA 的 `(major, minor)` 算力版本作为门控，例如只让 SM90+ 的 WGMMA 测试跑：

```python
def requires_cuda_compute_version(major_version, minor_version=0, mode="ge"):
    is_maca = _check_is_maca()
    ...
    try:
        if is_maca:
            compute_version = torch.cuda.get_device_capability()
        else:
            arch = nvcc.get_target_compute_version()
            compute_version = nvcc.parse_compute_version(arch)
    except ValueError:
        compute_version = (0, 0)   # 无 GPU → 后续 skipif 必然成立
    ...
```

见 [tilelang/testing/__init__.py:139-177](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/testing/__init__.py#L139-L177)。注意它对 MACA 用 `torch.cuda.get_device_capability()`（MACA 经兼容层暴露 CUDA 风格的能力查询），对真 CUDA 走 `nvcc`；无设备时落到 `(0, 0)`，任何「≥某版本」的判定都为假，用例被 skip。

**（d）数值校验 `torch_assert_close`。** 测试函数体里最常出现的断言之一，它被 re-export 进 `tilelang.testing` 命名空间（见 [tilelang/testing/__init__.py:14](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/testing/__init__.py#L14)）。其签名与关键参数：

```python
def torch_assert_close(tensor_a, tensor_b, rtol=1e-2, atol=1e-3,
                       max_mismatched_ratio=0.001, ...):
```

见 [tilelang/utils/tensor.py:205-219](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/utils/tensor.py#L205-L219)。`max_mismatched_ratio` 是它区别于 `torch.testing.assert_close` 的核心：允许「最多占总元素多少比例」的元素超出 `atol/rtol` 容差。真实用法见 [testing/python/jit/test_tilelang_jit_gemm.py:103](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/testing/python/jit/test_tilelang_jit_gemm.py#L103)：

```python
tilelang.testing.torch_assert_close(C, ref_C, atol=1e-2, rtol=1e-2, max_mismatched_ratio=0.05)
```

这里允许 5% 的元素失配——对一个 fp16 输入、fp32 累加的 GEMM 是合理的容差策略。

**（e）单文件入口 `main()` 与种子工具。** 这两个小工具让测试文件既能被 pytest 收集、又能独立运行：

```python
def main():  # pytest.main() 包装，支持单文件运行
    test_file = inspect.getsourcefile(sys._getframe(1))
    sys.exit(pytest.main([test_file] + sys.argv[1:]))

def set_random_seed(seed: int = 42) -> None:  # 统一钉死三库种子
    random.seed(seed); np.random.seed(seed); torch.manual_seed(seed)
    if torch.cuda.is_available(): torch.cuda.manual_seed_all(seed)
```

见 [tilelang/testing/__init__.py:126-136](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/testing/__init__.py#L126-L136)。

**（f）性能回归：`process_func` / `regression`。** 当你想给一个 kernel 加性能回归（不是正确性），用 `regression_example_*.py` 模式。它通过 `tilelang.testing.process_func` 登记一个返回延迟的函数，再由 `tilelang.testing.regression()` 统一驱动：

```python
# examples/elementwise/regression_example_elementwise.py
import tilelang.testing
import example_elementwise_add

def regression_example_elementwise_add():
    tilelang.testing.process_func(example_elementwise_add.run_regression_perf)

if __name__ == "__main__":
    tilelang.testing.regression()
```

见 [examples/elementwise/regression_example_elementwise.py:1-10](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/elementwise/regression_example_elementwise.py#L1-L10)。`regression()` 会扫描调用者模块里所有以 `regression_` 开头的函数，逐个执行并收集延迟，最后输出表格（或经 `TL_PERF_REGRESSION_FORMAT=json` 输出 JSON 行，见 [tilelang/testing/perf_regression.py:109-159](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/testing/perf_regression.py#L109-L159)）。`process_func` 对「延迟 ≤ 0」会最多重试 5 次（`_MAX_RETRY_NUM`），仍非正则告警，见 [tilelang/testing/perf_regression.py:89-106](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/testing/perf_regression.py#L89-L106)。

#### 4.3.4 代码实践

**实践目标：** 写一个带后端感知标记、用 `torch_assert_close` 校验的 pytest 用例，覆盖 4.1–4.3 的全部知识点。

**操作步骤：** 见本讲 **第 5 节「综合实践」**——那里会带你完整写一个 layer norm kernel 的测试。此处先做一个最小热身：复制 `examples/elementwise/test_example_elementwise.py`，把它的函数体从 `main()` 改成对一组固定 `(M, N)` 显式造输入、调 kernel、用 `tilelang.testing.torch_assert_close` 比对，并挂上 `@tilelang.testing.requires_cuda`。

**需要观察的现象：** 在 CUDA/MACA 机器上 PASSED；在无 GPU 机器上 SKIPPED 且 reason 为 "Requires CUDA like target"。

**预期结果：** 1 passed（有类 CUDA 后端）或 1 skipped（无后端）。**待本地验证。**

#### 4.3.5 小练习与答案

**练习 1：** 为什么 `requires_cuda` 在 metax fork 里放行的是 MACA 而不是检查真 CUDA？

**参考答案：** metax 分支把 MACA 作为主力后端（CI 主跑 MACA），原本面向 NVIDIA CUDA 的示例与测试需要继续发挥作用，于是「类 CUDA」的概念被扩成「MACA 也算」。`requires_cuda` 的判定改成 `_check_is_maca()`，让这批测试在 MACA 上也跑；真正 NVIDIA 专属的语义则用 `skip_on_maca` 单独排除。

**练习 2：** `torch_assert_close` 的 `max_mismatched_ratio` 在什么场景下必须用？设成 0 会怎样？

**参考答案：** 低精度（fp16/bf16/fp8/int8）张量核 kernel 由于累加顺序、舍入与硬件指令的精度特性，个别元素可能略微超出 `atol/rtol`，但整体仍正确。设成 0（等价于不允许任何失配）会让这类用例因极少数边缘元素而误报失败。允许一个小比例（如 0.05）能过滤掉这些噪声，只在大面积失配时才报警。

---

### 4.4 按 target 的测试依赖与 CI 矩阵

#### 4.4.1 概念说明

不同后端要装的 Python 依赖差别巨大：CUDA 测试要 `flash-attn`、`cuda-python`、`nvidia-cutlass-dsl`；ROCm、MACA、Metal 大多不需要。tilelang 用**分层 requirements 文件**管理这件事：一份公共的 `requirements-test.txt`，加上每后端一份 `requirements-test-<target>.txt` 追加（或为空）。CI 按 toolkit（CUDA/ROCm/MACA/Metal）矩阵，先装公共依赖，再装对应后端的那一份。

本节的另一条主线是 **CI 如何把「按后端筛跑测试」落地**——既靠 4.3 的标记自动跳过，也靠 CI 在命令行显式 `--ignore` 掉某些不适用目录/文件。

#### 4.4.2 核心流程

依赖分层（自顶向下叠加）：

```text
requirements-lint.txt                 # 基座：lint 工具
   ↑
requirements-test.txt                 # 公共：pytest 全家桶 + scipy + z3 等
   ↑
requirements-test-<cuda|rocm|maca|metal>.txt   # 后端追加
```

CI 单条测试任务的生命周期：

```text
matrix 选定 toolkit（如 MACA-3.8）
 ├── Set environment (MACA)：设 UV_INDEX、CLANG_TIDY 加 USE_MACA=ON
 ├── uv pip install -r requirements-test.txt        # 公共依赖
 ├── uv pip install -r requirements-test-maca.txt   # 后端追加（MACA 为空）
 ├── uv pip install -v .                             # 装本仓库（编译 libtilelang.so）
 ├── cd testing && pytest ../examples                # 跑示例测试
 └── cd testing && pytest ./python [--ignore ...]    # 跑编译器测试（MACA 多处 ignore）
```

#### 4.4.3 源码精读

**（a）分层 requirements。** 公共依赖里测试相关的关键几行：

```text
# requirements-test.txt（节选）
pytest-xdist>=2.2.1     # 并行跑（对应 conftest 的 worker 隔离）
pytest-timeout          # 单测超时保护
pytest-durations        # 统计每条用例耗时
pytest>=6.2.4
scipy
z3-solver>=4.13.0,<4.15.5
```

见 [requirements-test.txt:14-36](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/requirements-test.txt#L14-L36)。注意它本身用 `--requirement requirements-lint.txt` 和 `--requirement requirements.txt` 递归引入 lint 与运行时依赖——pip 的 `-r` 文件里再写 `-r` 是支持的，形成链式叠加。

**（b）MACA 的测试依赖为什么几乎是空的？**

```text
# requirements-test-maca.txt
--requirement requirements-lint.txt
--requirement requirements-test.txt
# MACA specific requirements
# Currently: none
```

见 [requirements-test-maca.txt:1-8](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/requirements-test-maca.txt#L1-L8)。对比 CUDA 的：

```text
# requirements-test-cuda.txt
--requirement requirements-lint.txt
--requirement requirements-test.txt
# CUDA specific requirements
flash-attn==2.5.8
cuda-python==13.0.3
nvidia-cutlass-dsl[cu13]==4.5.0
```

见 [requirements-test-cuda.txt:1-11](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/requirements-test-cuda.txt#L1-L11)。MACA 不需要 flash-attn / cuda-python / cutlass-dsl，因为它的测试不覆盖这些 CUDA 专有生态（FlashAttention 参考实现、CUDA python 直连、CuTeDSL 后端）。`Currently: none` 这行注释是给后续维护者的提示：将来若有 MACA 专属测试依赖，加在这里。ROCm 与 Metal 同样为空（见 [requirements-test-rocm.txt:1-9](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/requirements-test-rocm.txt#L1-L9)、[requirements-test-metal.txt:1-9](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/requirements-test-metal.txt#L1-L9)）。

**（c）CI 矩阵：MACA 与 Metal 是主力。** metax 分支的 CI 矩阵与上游 tilelang 明显不同——ROCm 被注释掉，主力是自托管的 MACA runner：

```yaml
strategy:
  matrix:
    runner:
      - tags: tilelang-metax-runner
        name: self-hosted-metax
        toolkit: MACA-3.8
      - tags: [macos-latest]
        name: macos-latest
        toolkit: Metal
```

见 [.github/workflows/ci.yml:85-101](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/.github/workflows/ci.yml#L85-L101)（ROCm 的三行被注释）。这正是 metax 作为 MACA fork 的印记：CI 优先保 MACA 与 Metal 两条线。装依赖阶段按 toolkit 分支选 requirements：

```yaml
if [[ "${{ matrix.runner.toolkit }}" == *"CUDA"* ]]; then
  uv pip install --no-build-isolation-package=flash-attn -v -r requirements-test-cuda.txt
elif [[ "${{ matrix.runner.toolkit }}" == *"MACA"* ]]; then
  uv pip install -r requirements-test-maca.txt
...
```

见 [.github/workflows/ci.yml:325-337](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/.github/workflows/ci.yml#L325-L337)。

**（d）MACA 测试步骤的显式 `--ignore`。** MACA 跑编译器测试时，会显式排除一批 NVIDIA 专属（Hopper/Blackwell/nvrtc）与 Metal 的测试：

```yaml
- name: Run MACA tests with Python ${{ matrix.python-version }} (${{ matrix.runner.toolkit }})
  if: contains(matrix.runner.toolkit, 'MACA')
  run: |
    cd testing
    PYTEST=(uv run --no-project -m -- pytest --verbose --color=yes --durations=0 --showlocals --cache-clear)
    "${PYTEST[@]}" --maxfail=3 --numprocesses=2 \
      --ignore=./python/metal \
      --ignore=./python/issue/test_tilelang_issue_sm120_tma_smem_alignment.py \
      --ignore=./python/jit/test_tilelang_jit_cutedsl_host_codegen.py \
      --ignore=./python/jit/test_tilelang_jit_nvrtc.py \
      --ignore=./python/transform/test_tilelang_transform_im2col_fallback.py \
      --ignore=./python/transform/test_tilelang_transform_inject_tcgen05_fence.py \
      --ignore=./python/transform/test_tilelang_transform_fuse_mbarrier_arrive_expect_tx.py \
      --ignore=./python/transform/test_tilelang_transform_lower_hopper_intrin.py \
      ./python
```

见 [.github/workflows/ci.yml:439-457](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/.github/workflows/ci.yml#L439-L457)。这些 ignore 项揭示了一个事实：**「标记自动跳过」与「CI 显式 ignore」是两道互补的筛网**。标记（4.3）适合「单条用例在某后端无意义」的细粒度场景；CI 的 `--ignore` 适合「整类测试（Hopper intrin、tcgen05、nvrtc）整个不适用」的粗粒度场景——这些是 SM90/SM100 专属或 CUDA 编译器专属，MACA 既无对应硬件能力也无 nvrtc，整文件忽略比逐条标记更清晰。注意并行度也不同：CUDA 用 `--numprocesses=8`，MACA 只用 2（见对应步骤），这通常反映了 runner 的规模或稳定性差异。

**（e）CuTeDSL 后端的 `TILELANG_TARGET` 注入。** CuTeDSL 这条独立 job 把环境变量注入，触发 4.2.3(d) 讲过的 conftest xfail 逻辑：

```yaml
- name: Run CuTeDSL examples with Python 3.12 (CUDA-auto)
  env:
    TILELANG_TARGET: cutedsl
  run: |
    cd testing
    ... pytest ... ../examples
```

见 [.github/workflows/ci.yml:632-642](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/.github/workflows/ci.yml#L632-L642)。这条 job 当前被 `if: ... && false && ...` 暂时停用（见 [.github/workflows/ci.yml:484-487](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/.github/workflows/ci.yml#L484-L487)），但机制完整保留：`TILELANG_TARGET` 是 u3-l1 讲过的默认 target 环境变量，这里它让示例全部以 cutedsl 后端编译，配合 conftest 的已知失败表做渐进迁移。

#### 4.4.4 代码实践

**实践目标：** 读懂依赖分层与 CI 筛跑策略，能为自己新增的后端/算子决定「依赖放哪、测试怎么筛」。

**操作步骤：**

1. 读 `requirements-test-maca.txt`，确认它只继承公共依赖、无追加项。
2. 读 `requirements-test-cuda.txt`，列出 CUDA 多出的三个包。
3. 打开 `.github/workflows/ci.yml`，定位 MACA 测试步骤（约 439 行起），数一数它 `--ignore` 了哪些文件/目录，并按「为什么 ignore」分类（Metal 专属 / Hopper 专属 / Blackwell 专属 / nvrtc 专属 / 其它）。
4. 思考：如果你给 tilelang 新增了一个 MACA 专属的测试文件 `testing/python/maca/test_xxx.py`，CI 的现有 `--ignore` 会不会影响它？

**需要观察的现象：** 你应能给出一张「ignore 项 → 原因」对照表；并意识到现有 ignore 清单里没有 `./python/maca`（因为目前不存在该目录），所以未来新增 MACA 专属测试目录不会被误伤。

**预期结果：** 完成对照表（纯源码阅读，无需运行）。例：`python/metal`→Metal 专属；`test_tilelang_jit_nvrtc.py`→nvrtc 是 CUDA 专属即时编译器；`test_tilelang_transform_lower_hopper_intrin.py`→Hopper(SM90) 专属 intrinsic pass。

#### 4.4.5 小练习与答案

**练习 1：** 为什么 MACA 的 `requirements-test-maca.txt` 里写 `Currently: none`，而不是直接删掉这个文件？

**参考答案：** CI 的装依赖脚本是按 toolkit 分支查找 `requirements-test-<target>.txt` 的（见 ci.yml 的 if/elif 链）。保留这个文件、注释说明「暂无」，既让 CI 的 MACA 分支有一条明确的 `-r` 目标（即便只继承公共依赖），也为将来追加 MACA 专属测试依赖留好了落点，比删文件更可维护。

**练习 2：** 「标记自动 skip」和「CI 命令行 `--ignore`」两道筛网，分别适合什么场景？

**参考答案：** 标记适合**单条或少数用例**在某后端无意义（如某条向化测试仅 SM80 前的 NVIDIA 有意义），粒度细、随用例走、本地复现也生效；`--ignore` 适合**整文件/整目录**在某后端整体不适用（如 Hopper intrin、tcgen05、nvrtc 这类整个机制 MACA 没有），粒度粗、集中可读、避免在每条用例上重复堆标记。两者互补：标记管「后端感知的细筛」，ignore 管「后端不支持的粗筛」。

---

## 5. 综合实践

把本讲四个模块串起来，给你在 u8-l4 接触过的 **layer normalization** 算子写一个完整的、跨后端友好的 pytest 用例。layer norm 遵循 u8-l4 总结的依赖顺序「搬进来 → 规约出统计量 → 逐元素用统计量 → 搬出去」。

**目标：** 产出一个 `test_example_layernorm.py`，它在 CUDA/MACA 上跑数值校验，在无后端机器上优雅 skip，并支持单文件 `python` 直接运行。

**步骤：**

1. **写 kernel 与 `main()`。** 仿照 [examples/elementwise/example_elementwise_add.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/elementwise/example_elementwise_add.py) 的结构，新建（示例代码，非项目原有文件）`example_layernorm.py`：
   - 用 `@tilelang.jit` 定义 kernel，输入 `X: T.Tensor((M, N), in_dtype)`，输出同形 `Y`。
   - kernel 内：`alloc_shared` 取一行（`block_N`），`alloc_fragment` 存均值/方差/输出；沿 `N` 方向用 `T.reduce_sum`（u8-l4 讲过的 `T.reduce` 分块规约）算均值与方差，再逐元素 `T.Parallel` 做 `(x-mean)/sqrt(var+eps)`，最后 `T.copy` 回 global。
   - `main(M=512, N=512)`：用 `torch.randn` 造输入，参考实现直接调 `torch.nn.functional.layer_norm`，断言用 `tilelang.testing.torch_assert_close(out, ref, atol=1e-2, rtol=1e-2, max_mismatched_ratio=0.01)`（layer norm 数值较稳，比例可给小一点）。

2. **写测试包装。** 仿照 [examples/elementwise/test_example_elementwise.py:1-10](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/elementwise/test_example_elementwise.py#L1-L10)：
   ```python
   # 示例代码：test_example_layernorm.py
   import tilelang.testing
   import example_layernorm

   @tilelang.testing.requires_cuda          # 类 CUDA 后端（含 MACA）才跑
   def test_example_layernorm():
       example_layernorm.main()

   if __name__ == "__main__":
       tilelang.testing.main()
   ```
   如果你写了降低精度的变体（如 fp16 输入），且发现 MACA 上行为有差异，再叠加 `@tilelang.testing.skip_on_maca`（见 4.3.3(b)）。

3. **加性能回归（可选）。** 仿照 [examples/elementwise/regression_example_elementwise.py:1-10](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/elementwise/regression_example_elementwise.py#L1-L10)，给 `example_layernorm.py` 加一个返回延迟的 `run_regression_perf()`（内部用 `tilelang.profiler.do_bench`，参考 u8-l3），再写 `regression_example_layernorm.py` 调 `tilelang.testing.process_func` 登记它。

4. **本地运行验证。**
   ```bash
   cd testing
   uv run --no-project -m -- pytest -v ../examples/your_dir/test_example_layernorm.py
   ```

**需要观察的现象：**

- 有类 CUDA 后端时 PASSED；无后端时 SKIPPED，reason 为 "Requires CUDA like target"。
- 故意把参考实现的 `eps` 改错，应看到 `torch_assert_close` 报告失配元素比例超过 `max_mismatched_ratio` 而失败。
- 单文件 `python test_example_layernorm.py` 与 pytest 结果一致（验证 `main()` 入口）。

**预期结果：** 1 passed（有后端）或 1 skipped（无后端）。**若本地无 GPU 环境，请标注「待本地验证」，并将交付物限定为「可读的源码 + 对每一步会观察到什么的书面预判」。** 本实践的关键不在跑通，而在你能否正确选用 `requires_cuda` / `skip_on_maca` / `torch_assert_close` 这三件工具，并解释为何这么选。

> 说明：以上 `example_layernorm.py` / `test_example_layernorm.py` 均为**示例代码**，不是 tilelang-metax 仓库既有文件；请勿写进仓库源码树，按本讲角色约束只写 `tilelang-metax-tutorial/` 之内。

## 6. 本讲小结

- tilelang-metax 有**两个测试根目录**：`examples/`（端到端算子正确性，「示例即测试」，`test_*.py` 薄包装转发 `main()`）与 `testing/python/`（编译器细粒度单元测试，按子系统分目录）；`testing/cpp/` 目前为占位。
- **两个 `conftest.py`** 负责全局装配：钉死随机种子、按 worker 隔离 torch 扩展目录、`testing` 版强制用仓库内 tilelang、`--run-perf` 控制性能测试、CuTeDSL 已知失败自动 `xfail`，并用空集守卫（returncode=5）防「静默全绿」。
- `tilelang/testing` 提供后端感知标记：`requires_cuda` 在 metax fork 里**放行 MACA**（类 CUDA），`skip_on_maca` 反向剔除 NVIDIA 专属项，`requires_cuda_compute_version` 按算力版本门控；数值校验用允许少量失配的 `torch_assert_close`。
- **依赖分层**：公共 `requirements-test.txt` + 每后端 `requirements-test-<target>.txt`；MACA 版仅继承公共依赖（`Currently: none`），CUDA 版额外要 flash-attn / cuda-python / cutlass-dsl。
- **CI 筛跑两道筛网**：标记自动 skip（细粒度、单用例）与命令行 `--ignore`（粗粒度、整文件，如 MACA 排除 Hopper/tcgen05/nvrtc）；metax 的 CI 矩阵主力是 MACA 与 Metal，ROCm 暂停。
- 性能回归走 `regression_example_*.py` + `tilelang.testing.process_func` / `regression()`，支持文本表与 JSON 输出。

## 7. 下一步学习建议

本讲是学习手册的收官篇之一，建议按以下方向把所学持续化：

- **把你写的每个 kernel 都补上 `test_*.py`。** 套用「`main()` 做数值校验 + `test_*.py` 转发 + `requires_cuda` 守卫」三件套，让 u6/u8 的 GEMM、FlashAttention、layer norm 全部进入回归。
- **回归 u9-l1/l2 的扩展主题。** 当你新增一个 target 后端（u9-l1）或 tile 算子（u9-l2）时，配套考虑：新后端的测试依赖要不要加进 `requirements-test-<target>.txt`？新算子的「标记 + ignore」策略怎么定？这是检验你是否真正理解本讲的标尺。
- **深入性能回归与 CI。** 结合 u8-l3 的剖析方法，把你关心的 kernel 接入 `regression_example_*.py`，用 `TL_PERF_REGRESSION_FORMAT=json` 让外部机器人（如仓库里的 `pr-regression-test-bot.yml`）自动读取延迟、做 PR 级性能回归。
- **阅读 `testing/python/` 下的真实用例。** 重点看 `issue/`（GitHub issue 回归，命名带编号）、`jit/`（各执行后端）、`transform/`（各 pass）——它们是你为自己的编译器改动写测试的最佳模板。
