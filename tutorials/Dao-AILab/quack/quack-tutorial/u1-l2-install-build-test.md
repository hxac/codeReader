# 安装、构建与运行测试

## 1. 本讲目标

本讲承接上一讲《QuACK 是什么》。你已经知道 QuACK 是一组用 CuTe-DSL 写的 GPU 内核，包名是 `quack-kernels`、导入名是 `quack`。这一讲要解决的是「怎么让它在你自己的机器上跑起来」。

学完后你应当能够：

- 用 `pip` 安装 QuACK，并能看懂开发环境（dev）与 CUDA 13 两种安装方式的区别。
- 理解 `pyproject.toml` 中的依赖、可选 extras（dev/bench/jax 等）从哪里来。
- 用 `pytest` 运行单个测试，并能用 `-k`、`-x`、`-n`、`--async-compile` 精确控制运行范围与速度。
- 读懂 `tests/conftest.py` 这个「测试夹具总入口」做了哪些项目专属的事情（GPU 分配、收集汇总、OOM 重试）。

> 本讲里凡是「在终端里执行」的命令，都需要一台带 H100 / B200·B300 / RTX 50 的 GPU 机器和 CUDA 12.9+ 环境。本讲义无法替你执行这些命令，因此涉及具体运行现象的地方会标注「待本地验证」。

## 2. 前置知识

- **包管理（pip + extras）**：Python 包可以用 `pip install 'pkg[extra]'` 的语法安装「可选依赖组」。例如 `[dev]` 表示一组「只在做开发时才需要」的工具。QuACK 把这种用法用得很充分。
- **editable 安装（`-e`）**：`pip install -e .` 把当前目录以「可编辑模式」装进环境，源码一改、导入就生效，不需要反复重装。开发内核时几乎只用这种模式。
- **pytest**：Python 最常用的测试框架。它扫描 `tests/` 目录下的 `test_*.py`，把里面以 `test_` 开头的函数当成测试用例来跑。
- **参数化（parametrize）**：同一个测试函数可以用 `@pytest.mark.parametrize` 喂入多组参数（如不同 dtype、不同矩阵尺寸），pytest 会把它们展开成很多条独立的「用例（test item / node）」。
- **GPU 多卡与 CUDA_VISIBLE_DEVICES**：环境变量 `CUDA_VISIBLE_DEVICES` 决定进程能看到哪些 GPU。多卡并行测试时，每个 worker 必须被「钉」到一张卡上，否则会乱套。

上一讲已建立的概念（本讲直接使用，不再重复）：CUDA 内核、CuTe-DSL、SM（流多处理器）、分发名 `quack-kernels` 与导入名 `quack`、公开 API `rmsnorm/softmax/cross_entropy`。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用它讲什么 |
| --- | --- | --- |
| [README.md](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/README.md) | 面向用户的安装与使用说明 | 安装命令、CUDA 13 注意事项、依赖硬件 |
| [pyproject.toml](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/pyproject.toml) | 包的元数据与构建配置 | 依赖列表、可选 extras、pytest/ruff 配置 |
| [AGENTS.md](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/AGENTS.md) | 给代码 Agent 的开发指南 | 标准的构建、运行测试命令速查 |
| [tests/conftest.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/tests/conftest.py) | pytest 项目级夹具/钩子 | GPU 分配、收集汇总、OOM 重试 |
| [quack/testing/pytest_plugin.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/testing/pytest_plugin.py) | 可复用的异步编译插件 | `--async-compile` 选项的定义 |
| [tests/test_softmax.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/tests/test_softmax.py) | softmax 内核的数值正确性测试 | 一个「真实测试长什么样」的范例 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **4.1 包的安装与开发环境**（pip 安装、extras、pre-commit）
2. **4.2 pytest 运行方式与单测命令**（pytest、`-k`、`-x`、`-n`、`--async-compile`）
3. **4.3 conftest 测试夹具**（GPU 分配、收集汇总、OOM 重试）

### 4.1 包的安装与开发环境

#### 4.1.1 概念说明

QuACK 在 PyPI 上的分发名是 `quack-kernels`，但你在 Python 里 `import` 时用的是 `quack`。这两个名字不一样，是初学者最容易踩的第一个坑（上一讲已经强调过）。

安装分两类场景：

- **普通使用**：`pip install quack-kernels`（默认绑定 CUDA 12.9）。
- **开发**：`pip install -e '.[dev]'`（editable 模式 + 开发工具）。

注意命令里 `'.[dev]'` 的写法：`.` 代表「当前目录的源码」，`[dev]` 是一个「可选依赖组（extras）」。整个命令的意思是「把当前目录以可编辑模式装上，并额外装上 dev 这组依赖」。

#### 4.1.2 核心流程

安装一次 QuACK 的逻辑流程：

```text
pip 读取 pyproject.toml
  ├── 解析 [project].dependencies        → 必装依赖（cutlass-dsl / torch / tvm-ffi / einops ...）
  ├── 解析 [project.optional-dependencies]→ 按 extras 名挑可选依赖
  │     └── dev → pre-commit / pytest / pytest-xdist / ruff
  ├── 解析 [tool.setuptools.packages.find]→ 找到 quack* 包
  └── 动态读取 quack.__version__          → 填充版本号
```

关键点：版本号是「动态」的，不是写死在 `pyproject.toml` 里，而是从 `quack/__init__.py` 的 `__version__` 读出来的。

#### 4.1.3 源码精读

**必装依赖**写在 [pyproject.toml:L9-L15](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/pyproject.toml#L9-L15)。这里能看到上一讲提到的核心依赖 `nvidia-cutlass-dsl==4.7.0`（被锁死在 4.7.0），以及 `torch`、`apache-tvm-ffi`（FFI 桥）、`torch-c-dlpack-ext`、`einops`。`requires-python = ">=3.10"`（第 8 行）是 Python 的硬下限。

> 小提示：README 的 Requirements 一栏写着「Python 3.12」，那是作者推荐的版本；而 `pyproject.toml` 里声明的硬性下限是 `>=3.10`。以 `pyproject.toml` 为准决定「能不能装」，以 README 为准决定「用什么最顺」。

**可选依赖（extras）**写在 [pyproject.toml:L17-L27](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/pyproject.toml#L17-L27)：

| extras 名 | 作用 |
| --- | --- |
| `cu13` | 绑定 CUDA 13.x 的 cutlass-dsl |
| `heuristics` | 更好的未调优 GEMM 配置（`nvidia-matmul-heuristics`） |
| `jax` | JAX 绑定（`jax` + `jax-tvm-ffi`） |
| `bench` | 基准测试（`pandas` + `tyro`） |
| `dev` | 开发工具：`pre-commit` / `pytest` / `pytest-xdist` / `ruff` |

注意 `dev`（[pyproject.toml:L22-L27](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/pyproject.toml#L22-L27)）里的 `pytest-xdist` 就是后面 `-n` 多进程并行测试的来源——这个联系很重要。

**用户视角的安装命令**在 [README.md:L5-L23](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/README.md#L5-L23)。这里有一个**很重要的坑**：CUDA 13.x 的安装**不要用 `uv`**，要用 `pip`，因为 `uv` 可能把 `nvidia-cutlass-dsl[cu13]` 的安装顺序搞错（见 NVIDIA/cutlass#3259）。CUDA 13 的安装需要额外的 `--extra-index-url https://download.pytorch.org/whl/cu130` 来拿对应的 torch wheel。

**开发视角的安装命令**在 [README.md:L73-L86](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/README.md#L73-L86) 和 [AGENTS.md:L7-L19](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/AGENTS.md#L7-L19)，两者一致：

```bash
pip install -e '.[dev]'
pre-commit install
```

`pre-commit install` 会把 git 的 pre-commit 钩子装上，之后每次 `git commit` 前会自动跑 ruff 的检查与格式化（配置在 `pyproject.toml` 的 `[tool.ruff]` 段）。

#### 4.1.4 代码实践

**实践目标**：在你自己的机器上完成一次开发环境搭建，并验证 `quack` 能被导入。

**操作步骤**：

1. 在仓库根目录执行 `pip install -e '.[dev]'`。
2. 执行 `pre-commit install`。
3. 在 Python 里执行 `import quack; print(quack.__version__)`。

**需要观察的现象**：

- 第 1 步会拉取 `nvidia-cutlass-dsl==4.7.0`、`torch` 等依赖；editable 安装结束时会把 `quack` 以指向源码目录的方式登记到环境里。
- 第 3 步应打印版本号（上一讲提到当前为 0.6.4）。

**预期结果**：`import quack` 不报错，并能打印出版本字符串。

**待本地验证**：具体下载耗时与打印出的版本号以你本机为准。

#### 4.1.5 小练习与答案

**练习 1**：为什么开发时用 `pip install -e '.[dev]'` 而不是 `pip install quack-kernels`？

> **参考答案**：`-e` 是 editable 模式，你改 `quack/` 下的源码后 `import quack` 立刻生效，不必反复重装；`[dev]` 额外装上 `pytest`/`ruff`/`pre-commit` 等开发工具。`pip install quack-kernels` 装的是 PyPI 上打包好的快照，改源码不会生效，也不带开发工具。

**练习 2**：你想在 CUDA 13.x 上做开发，但用了 `uv` 安装，结果出了问题。根据源码，正确的安装命令应该是什么？

> **参考答案**：不要用 `uv`，改用 `pip install 'quack-kernels[dev,cu13]' --extra-index-url https://download.pytorch.org/whl/cu130`（见 README CUDA 13 段落与 [pyproject.toml:L17-L18](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/pyproject.toml#L17-L18)）。

---

### 4.2 pytest 运行方式与单测命令

#### 4.2.1 概念说明

QuACK 的测试全部在 `tests/` 目录下，用 `pytest` 跑。每个测试文件对应一个或一组内核，例如 `test_softmax.py` 测 softmax、`test_rmsnorm.py` 测 RMSNorm。

因为内核测试有**三个特点**，所以你几乎从不「一把跑全部」：

1. **参数化爆炸**：一个测试函数会被 dtype × 尺寸 × batch × `use_compile` 展开成几十甚至上百条用例。
2. **冷编译慢**：第一次跑某个内核时，CuTe-DSL 要把 Python 内核源码编译成机器码（`.o`/`.cubin`），可能要几十秒。
3. **需要 GPU**：每条用例都要真在 GPU 上跑并和 PyTorch 参考实现比数值。

所以日常迭代的核心技能是「**精确地只跑你需要的那几条用例**」。

#### 4.2.2 核心流程

调测一条内核用例的典型循环：

```text
改内核源码
   │
   ▼
pytest tests/test_xxx.py -x -k 'bfloat16'      # 只跑 bfloat16 的用例，失败立即停
   │  （第一次会触发冷编译）
   ▼
看失败信息 / 用 cute.printf 打印中间值
   │
   ▼
（冷编译很烦时）加 --async-compile=16          # 把编译丢给后台 worker 池，和测试重叠
```

常用 pytest 选项速查（QuACK 里实际会用到）：

| 选项 | 含义 |
| --- | --- |
| `tests/test_xxx.py` | 只在这个文件里收集用例 |
| `-x` | 遇到第一条失败就停下（调试时用，避免被后面几十条失败淹没） |
| `-k 'bfloat16'` | 用关键字表达式过滤用例 ID，只跑含 `bfloat16` 的 |
| `::test_softmax_fwd` | 精确指定某个测试函数（node 级别） |
| `-n 8` | 用 pytest-xdist 开 8 个并行 worker（多卡时尤其有用） |
| `--async-compile=16` | 冷编译丢给 16 个 CPU worker，与测试重叠 |

#### 4.2.3 源码精读

**标准命令速查**写在 [AGENTS.md:L21-L33](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/AGENTS.md#L21-L33)，这里直接给出了从「跑全部」到「跑单条」再到「重叠编译」的三档命令：

```bash
pytest tests/                                      # 跑全部
pytest tests/test_rmsnorm.py -x                    # 单文件，失败即停
pytest tests/test_rmsnorm.py::test_rmsnorm_fwd -x -k "bfloat16"   # 单函数 + 关键字
pytest tests/test_rmsnorm.py --async-compile=16    # 重叠冷编译
pytest tests/ -n 8 --async-compile=32              # 多进程 + 重叠编译
```

**`--async-compile` 选项的定义**在 [quack/testing/pytest_plugin.py:L29-L42](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/testing/pytest_plugin.py#L29-L42)。关键细节：它的类型是 `int`、`nargs="?"`，意思是「可以不带值（`--async-compile`，默认 32）也可以带值（`--async-compile=16`）」。`default=None` 表示不传时这个机制完全不启用——也就是说**缓存热的时候它是零开销的**。

> 这正是为什么 `--async-compile` 平时可以一直挂着：缓存命中时它什么都不做，只在冷编译 miss 时才把编译丢到后台 worker 池。

**一个真实测试长什么样**：以 softmax 为例，看 [tests/test_softmax.py:L24-L33](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/tests/test_softmax.py#L24-L33)。这里有四层 `@pytest.mark.parametrize`：

```python
@pytest.mark.parametrize("input_dtype", [torch.bfloat16, torch.float16, torch.float32])
@pytest.mark.parametrize("N", [192, 256, 760, 1024, 1128, 4096, 32768, 131072, 262144])
@pytest.mark.parametrize("M", [1, 199])
@pytest.mark.parametrize("use_compile", [False, True])
def test_softmax(M, N, input_dtype, use_compile):
```

光是 `test_softmax` 这一个函数，就被展开成 `3 × 9 × 2 × 2 = 108` 条用例！这就是为什么 `-k` 过滤如此重要——`-k 'bfloat16'` 会把 dtype 轴只留 bfloat16（1/3），降到 `9 × 2 × 2 = 36` 条。

**数值校验的逻辑**在 [tests/test_softmax.py:L52-L68](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/tests/test_softmax.py#L52-L68)。注意几个体现「QuACK 测试哲学」的细节：

- 用 `F.softmax(x_ref, dim=-1)` 作为**参考实现**，和内核输出 `out` 用 `torch.testing.assert_close` 比数值（第 63 行）。
- 容差表 `TOLERANCES`（[tests/test_softmax.py:L14-L18](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/tests/test_softmax.py#L14-L18)）按 dtype 给不同的 `atol/rtol`：bf16 最松（1e-2），fp32 最严（1e-4）。
- 第 55 行有一句 `torch.cuda.synchronize()`，注释说「不同步的话 `torch.autograd` 会拿到错误结果」——这是 GPU 异步执行的一个经典坑。
- 还顺带验证了 softmax 的**数学性质**：每行求和≈1（第 64-65 行）、输出在 [0,1] 区间（第 66-67 行），不只比数值。

`use_compile` 这条轴（第 31 行）会切换是否用 `torch.compile(softmax, fullgraph=True)`（第 43 行），确保内核既能直接调用、也能被 `torch.compile` 正确捕获。这种「双路径」是 QuACK 测试的通用模式。

#### 4.2.4 代码实践（本讲主实践）

**实践目标**：跑一次 softmax 的 bfloat16 测试，亲眼看一遍「冷编译 → 数值校验」的完整过程。

**操作步骤**：

1. 确认已按 4.1 装好开发环境，且在一张支持的 GPU 上。
2. 在仓库根目录执行：

   ```bash
   pytest tests/test_softmax.py -x -k 'bfloat16'
   ```

3. 观察输出。

**需要观察的现象**：

- **第一次运行**会看到较长的「编译」阶段（CuTe-DSL 把 softmax 内核编成机器码，命中 `.o` 缓存）。之后再跑同样的用例，这一步会快很多。
- 每条用例会显示 PASSED。`-k 'bfloat16'` 会把 `test_softmax` 中 dtype=bfloat16 的用例挑出来（约 36 条），并过滤掉 `test_softmax_extreme_values`（它只测 fp16/fp32，不含 bfloat16）。
- 若某条用例数值超容差，`-x` 会让你立刻停在那条失败上，并打印 `assert_close` 的差异。

**预期结果**：全部被选中的用例 PASSED。

**待本地验证**：具体编译耗时与用例条数以你本机的 CUDA/GPU 与缓存状态为准；本讲义没有替你执行该命令。

> 想看到「重叠编译」的效果？把命令换成 `pytest tests/test_softmax.py -x -k 'bfloat16' --async-compile=16`，并在跑之前删掉本地 `.o` 缓存制造一次冷编译，对比两次的墙钟时间（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：`pytest tests/test_softmax.py -x -k 'bfloat16'` 大约会跑多少条用例？依据是什么？

> **参考答案**：约 36 条。`test_softmax` 有 `dtype × N × M × use_compile = 3×9×2×2=108` 条，`-k 'bfloat16'` 把 dtype 轴留 1 个（1/3），得 `9×2×2=36`；`test_softmax_extreme_values` 不含 bfloat16，被过滤掉。依据是 [tests/test_softmax.py:L24-L33](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/tests/test_softmax.py#L24-L33) 的四层 parametrize。

**练习 2**：`--async-compile` 在缓存已经热的时候会不会拖慢测试？为什么？

> **参考答案**：不会。它在 `pytest_addoption` 里 `default=None`，不传或缓存命中时是零开销的（见 [pytest_plugin.py:L29-L42](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/testing/pytest_plugin.py#L29-L42) 与 conftest 文档字符串）。它只在 `.o` 缓存 miss 时才把编译交给后台 worker 池。

**练习 3**：为什么 `test_softmax` 里在取 `torch.autograd.grad` 之前要 `torch.cuda.synchronize()`？

> **参考答案**：GPU 执行是异步的，前向 `out` 还在排队时就读梯度会拿到不完整/错误的结果。`synchronize()` 强制等前向落盘后再算反向（见 [test_softmax.py:L55-L56](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/tests/test_softmax.py#L55-L56) 的注释）。

---

### 4.3 conftest 测试夹具

#### 4.3.1 概念说明

`tests/conftest.py` 是 pytest 的「项目级总入口」。pytest 启动时会自动加载它，里面定义的钩子（hook）和夹具对**所有**测试生效。QuACK 的 `conftest.py` 不是用来定义普通夹具的，而是做三件**项目专属**的事：

1. **多卡分配**：用 xdist 并行时（`-n`），把每个 worker 钉到一张 GPU 上。
2. **收集汇总**：跑测试前打印一份「每个文件收集了多少条用例」的清单。
3. **OOM 重试**：某条用例因为显存不足失败时，清一遍显存再重试一次。

此外，它通过一行 `pytest_plugins = ["quack.testing.pytest_plugin"]` 把上一节的 `--async-compile` 机制接进来。

#### 4.3.2 核心流程

pytest 启动到第一条用例之间，conftest 的介入顺序：

```text
pytest 收集
   │
   ├─ conftest 被 import  → _assign_xdist_worker_gpu() 在「import 期」就钉 GPU
   │      （必须在任何 CUDA 触摸之前，否则 CUDA 会缓存到错误的设备集合）
   │
   ├─ pytest_configure()  → 记录 xdist worker 的 GPU 分配日志
   │
   ├─ pytest_collection_finish() → 打印「Collected N tests: {文件: {函数: 条数}}」
   │
   └─ pytest_runtest_call()  → 每条用例外层包一层：OOM 则清显存重试一次
```

> 一个关键时序点：GPU 分配被故意放在 **conftest 的 import 期**（模块顶层），而不是 `pytest_configure` 里。因为导入 `quack` 包本身可能就会触发 `torch.cuda.is_available()`，那时再设 `CUDA_VISIBLE_DEVICES` 就太晚了——CUDA 已经把更大的设备集合缓存住了。

#### 4.3.3 源码精读

**整体说明与用法**在文件开头的文档字符串 [tests/conftest.py:L1-L22](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/tests/conftest.py#L1-L22)，里面直接给了两条推荐命令：

```bash
pytest tests/test_softmax.py --async-compile=16      # 编译与测试重叠
pytest tests/ -n 8 --async-compile=32                # 多进程 + 重叠
```

**多卡分配**的核心是 `_assign_xdist_worker_gpu`，见 [tests/conftest.py:L69-L89](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/tests/conftest.py#L69-L89)。它的策略是：

- 读取 worker 编号（`PYTEST_XDIST_WORKER`，如 `gw0`、`gw1`）。
- 取可见 GPU 列表（`_get_gpu_ids`，[tests/conftest.py:L35-L55](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/tests/conftest.py#L35-L55)，依次从 `CUDA_VISIBLE_DEVICES` 或 `nvidia-smi` 拿）。
- 用 `worker_num % len(gpu_ids)` 做**轮询（round-robin）**，把第 i 个 worker 钉到第 i 张卡，并把结果写回 `CUDA_VISIBLE_DEVICES`。

文档字符串总结得很到位：「workers round-robin across GPUs」。这样 `pytest tests/ -n 4` 在 4 卡机器上就是每张卡一个 worker，互不抢卡。

**接入 `--async-compile`** 就一行：[tests/conftest.py:L98](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/tests/conftest.py#L98) `pytest_plugins = ["quack.testing.pytest_plugin"]`。这行注释解释了为什么它要排在 GPU 分配之后才导入——因为导入 `quack` 会触摸 CUDA。

**收集汇总**是 `pytest_collection_finish`，见 [tests/conftest.py:L123-L138](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/tests/conftest.py#L123-L138)。它在收集完、开跑前，按「文件 → 函数 → 条数」统计并打印一行 `Collected N tests: {...}`。这能让你一眼看出 `-k` 过滤后到底剩多少、都在哪个函数里——配合 4.2 的实践非常实用。

**OOM 重试**是 `pytest_runtest_call` 钩子，见 [tests/conftest.py:L156-L216](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/tests/conftest.py#L156-L216)。逻辑是：若用例抛出 `torch.OutOfMemoryError`（`_is_oom` 判断，[tests/conftest.py:L145-L153](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/tests/conftest.py#L145-L153)），就 `gc.collect()` + `torch.cuda.empty_cache()` 清显存，再 `item.runtest()` 重试**一次**。在共享 GPU + 冷编译的 CI 里，这种「偶发 OOM」很常见，重试一次能把假失败剔掉。

> 这段钩子有一段很长的注释（[tests/conftest.py:L156-L191](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/tests/conftest.py#L156-L191)），讲的是它和 `--async-compile` 的 `CompilePending` 异常之间一个很微妙的交互：重试时若又撞上「编译尚未就绪」，必须把它正确交给 defer 机制，而不是当成真失败。这属于高级细节，初学时只要记住「OOM 会自动重试一次」即可。

**pytest 自身的配置**不在 conftest 里，而在 [pyproject.toml:L46-L75](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/pyproject.toml#L46-L75) 的 `[tool.pytest.ini_options]`。两个值得注意的点：

- `addopts = "-p no:cacheprovider -p no:unraisableexception -p no:threadexception"`（[pyproject.toml:L56](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/pyproject.toml#L56)）显式禁用了三个 pytest 插件，理由是注释里说的「对一个 2k 用例的文件省 1-2 秒，对 QuACK 的 CI 没有功能损失」。
- `filterwarnings`（[pyproject.toml:L66-L75](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/pyproject.toml#L66-L75)）屏蔽了 `torch.compile` 触发的大量 `torch.jit.script_method` 弃用警告和 profiler 警告——不然一次跑会被几百条警告刷屏。

#### 4.3.4 代码实践

**实践目标**：亲眼看到 conftest 的「收集汇总」和「GPU 分配」生效。

**操作步骤**：

1. 执行 `pytest tests/test_softmax.py -k 'bfloat16' --collect-only`（只收集不跑）。
2. 再执行 `pytest tests/test_softmax.py -k 'bfloat16' -x`，开跑后立刻按 `Ctrl+C` 中断即可，重点看开头打印的汇总。
3.（可选，多卡机器）执行 `CUDA_VISIBLE_DEVICES=0,1 pytest tests/test_softmax.py -n 2 -k 'bfloat16' --async-compile=4`，在日志里找形如 `Worker gw0 assigned CUDA_VISIBLE_DEVICES=0 ...` 的行。

**需要观察的现象**：

- 第 1、2 步会打印一行 `Collected N tests: {"tests/test_softmax.py": {"test_softmax": K, ...}}`，其中 N、K 是 `-k 'bfloat16'` 过滤后的实际条数。
- 第 3 步（若有 2 张卡）会看到 `gw0` 被钉到卡 0、`gw1` 被钉到卡 1。

**预期结果**：汇总行里 `test_softmax` 的条数与你在 4.2 练习里推算的约 36 一致（待本地验证确切数字）。

**待本地验证**：`--collect-only` 的条数与多卡日志取决于本机硬件与 `CUDA_VISIBLE_DEVICES`。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `_assign_xdist_worker_gpu` 要在 conftest 的 **import 期**执行，而不是放进 `pytest_configure` 钩子里？

> **参考答案**：因为导入 `quack` 包（及 CUTLASS/PyTorch）可能触发 `torch.cuda.is_available()`，此时 CUDA 已经缓存了设备集合；之后再在 `pytest_configure` 里收窄 `CUDA_VISIBLE_DEVICES` 就太晚了，所有 worker 仍会默认落到逻辑 GPU 0。所以必须在任何 CUDA 触摸之前就钉好卡（见 [tests/conftest.py:L69-L89](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/tests/conftest.py#L69-L89) 的注释）。

**练习 2**：`pyproject.toml` 里 `addopts` 禁用了 `cacheprovider` 等三个 pytest 插件，作者的依据是什么？这对「测试是否报失败」有影响吗？

> **参考答案**：依据是它们对 QuACK 的 CI 没有功能收益、却在一个 2k 用例的文件上各浪费约几十毫秒到 0.6 秒（见 [pyproject.toml:L46-L56](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/pyproject.toml#L46-L56) 注释）。没有影响——pytest 仍照常报告失败，只是少了「测试期间有未捕获异常」之类的次要报告。

---

## 5. 综合实践

把三个模块串起来，完成一次「从零到一条通过用例」的完整闭环：

1. **安装**：`pip install -e '.[dev]'` && `pre-commit install`（模块 4.1）。
2. **挑用例**：阅读 [tests/test_softmax.py:L24-L33](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/tests/test_softmax.py#L24-L33)，先用 `--collect-only` 确认 `-k 'bfloat16'` 会选出哪些用例（模块 4.2 + 4.3）。
3. **跑用例**：执行 `pytest tests/test_softmax.py -x -k 'bfloat16'`，观察冷编译与数值校验（模块 4.2）。
4. **看夹具**：在输出里找到 conftest 打印的 `Collected N tests: ...` 汇总行，并核对它和你 `--collect-only` 的条数是否一致（模块 4.3）。
5. **进阶（可选）**：制造一次冷编译，对比 `--async-compile=16` 开/关时的墙钟时间；若有多卡，用 `-n 2` 观察日志里的 worker-GPU 分配。

**验收标准**：

- 能说清楚「`pip install -e '.[dev]'` 里每个符号的含义」。
- 能解释 `-x`、`-k`、`-n`、`--async-compile` 分别改变什么。
- 能指出 conftest 做了哪三件项目专属的事、`--async-compile` 是从哪个插件接进来的。

> 全程需要在支持的 GPU 上进行；凡涉及具体耗时/条数的结论，请以本地实际结果为准。

## 6. 本讲小结

- QuACK 分发名 `quack-kernels`、导入名 `quack`；开发用 `pip install -e '.[dev]'`（editable + dev 工具组），CUDA 13 改用 `pip ... [dev,cu13]` 且**不要用 uv**。
- 依赖与可选 extras 全部声明在 [pyproject.toml](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/pyproject.toml)，版本号动态取自 `quack.__version__`；`dev` extra 里的 `pytest-xdist` 是 `-n` 并行的来源。
- 内核测试参数化会爆炸，日常用 `pytest tests/test_xxx.py -x -k '...'` 精确只跑需要的几条用例；softmax 单函数就展开成上百条。
- `--async-compile=N`（来自 `quack.testing.pytest_plugin`，经 conftest 接入）在冷编译时把编译丢给 N 个 CPU worker 与测试重叠，缓存热时零开销。
- `tests/conftest.py` 做三件项目专属的事：xdist 多卡 round-robin 分配（import 期就钉卡）、收集汇总打印、OOM 自动重试一次。
- QuACK 的测试哲学：每条用例都必须和 PyTorch 参考实现比**数值**（带 dtype 相关容差），而不只是查 shape 或「不崩」。

## 7. 下一步学习建议

现在你已经能把 QuACK 跑起来、并精确地调试单个内核测试了。接下来建议：

- **想要总览模块地图** → 下一讲《目录结构与模块地图》（u1-l3）：通览 `quack` 包及其子包（epilogue/gemm_runtime/blockscaled/...），建立整体认知。
- **想理解 softmax 测试背后的内核** → 进入第 2 单元，先读《ReductionBase 共享基类》（u2-l1）与《Softmax 前向内核逐行解读》（u2-l2），把本讲看到的 `test_softmax` 和真正的内核源码对上。
- **想深入 `--async-compile` 的实现** → 留到第 8 单元《`.o` JIT 缓存与异步编译池》（u8-l2），那里会讲 `cache/async_compile.py` 的 worker 池与 `cache/jit.py` 的两级缓存。

> 阅读建议：在进入第 2 单元之前，先把本讲的「主实践」（跑一次 softmax bfloat16 测试）在本地跑通，建立一个感性的「冷编译—数值校验」基线，后面读内核源码时会更有底。
