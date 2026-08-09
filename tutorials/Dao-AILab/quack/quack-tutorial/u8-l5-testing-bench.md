# 测试方法与基准协议

## 1. 本讲目标

QuACK 是一组面向最新 NVIDIA GPU（SM90/SM100/SM120）的高性能 CUDA 内核。内核写得再快，如果**结果不对**或**测得的性能是假象**，整个项目就失去了意义。本讲不写任何内核，而是讲解 QuACK 如何「证明内核正确」与「可信地度量内核性能」。

学完后你应该能够：

1. 用 QuACK 的标准范式写一个**数值正确性测试**：参考实现 + 容差表 + `use_compile` 双路径，并说清楚为何必须比较数值、而非只看形状。
2. 看懂 QuACK 测试套件的**参数化矩阵**为何会「爆炸」，掌握用 `-x` / `-k` / `-n` / `--async-compile` 精确切片运行的方法，以及硬件相关的跳过（skip）策略。
3. 理解两套**基准测量协议**：基于 `triton.testing.do_bench` 的简单吞吐测量，以及 QuACK 自研的「L2-cold CUDA-graph 轮转」协议，知道 L2 缓存、warmup、时钟漂移如何让基准说谎。

本讲是整个学习路线的收尾篇：前面七单元都在讲「内核是怎么实现的」，这一篇讲「怎么证明它没问题、它到底有多快」。

## 2. 前置知识

在阅读本讲前，你应当已经具备以下认知（来自前置讲义）：

- **内核即异步 CUDA 调用**（u2-l3）：QuACK 内核经 `@cute_op` 注册为 `torch.library` 自定义算子，前向是异步的；要取 autograd 梯度前必须 `torch.cuda.synchronize()`，否则会读到未完成的输出。
- **`use_compile` 双路径**（u2-l6）：每个算子既能以 eager 方式直接调用，也能经 `torch.compile(...)` 走 FX 追踪 + custom op 路径。内核必须两条路都正确。
- **JIT 缓存与冷编译**（u2-l6 / u8-l2）：第一次跑某形状会触发数百毫秒的编译（落盘成 `.o`），`--async-compile` 会把冷编译丢给后台 CPU worker 池，冷 miss 时抛 `CompilePending`。
- **SM 分发**（u1-l1）：同一算子按 GPU 的 `device_capability`（major=8/9/10/12）走不同实现，测试必须能跳过当前硬件不支持的路径。

如果你对这些概念陌生，建议先回顾 u2-l3、u2-l6、u8-l2。

下面几个术语在本讲会反复出现，先统一解释：

| 术语 | 含义 |
|------|------|
| **参考实现 (reference)** | 用 PyTorch 原生算子（多为 float32）写出的「标准答案」，用作对照基准 |
| **容差 (tolerance)** | 允许的数值误差上限，用 `atol`（绝对）+ `rtol`（相对）表示 |
| **参数化矩阵** | pytest 用 `@pytest.mark.parametrize` 把一个测试函数展开成大量用例 |
| **L2-cold** | 测量时刻意让 L2 缓存「冷」掉，模拟数据必须从 HBM 取的真实场景 |
| **CUDA Graph** | 把一组 kernel launch 录制成图后单次重放，消除每 launch 的 CPU 开销 |
| **burner / 轮转张量** | 用多份独立克隆输入轮流喂 kernel，防止 L2 命中同一个张量而虚高 |

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 | 本讲关注点 |
|------|------|-----------|
| [tests/test_softmax.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/tests/test_softmax.py) | softmax 数值正确性测试 | 数值正确性测试的**标准模板** |
| [tests/test_gemm_functional.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/tests/test_gemm_functional.py) | GEMM functional facade 测试 | 硬件跳过、FakeTensor 形状校验 |
| [tests/conftest.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/tests/conftest.py) | pytest 项目级配置 | xdist GPU 分配、OOM 重试、用例汇总 |
| [quack/testing/pytest_plugin.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/testing/pytest_plugin.py) | 可复用的异步编译 pytest 插件 | `CompilePending` 延迟-重试循环 |
| [quack/bench/bench_utils.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/bench/bench_utils.py) | 基准测量公共工具 | L2-cold CUDA-graph 轮转协议 |
| [benchmarks/benchmark_softmax.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/benchmarks/benchmark_softmax.py) | softmax 基准脚本 | `do_bench` + `perf_report` 简单协议 |
| [benchmarks/benchmark_gemm.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/benchmarks/benchmark_gemm.py) | GEMM 基准脚本 | `do_bench` + cuBLAS 对照 |

记忆要点：**测试**在 `tests/` 下（pytest），**基准**在 `benchmarks/` 下（独立脚本，`python benchmarks/xxx.py` 直接跑）；公共测量工具在 `quack/bench/bench_utils.py`。

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：① 数值正确性参考实现，② pytest 参数化与 `-k` 过滤，③ 基准测量协议。

### 4.1 数值正确性参考实现

#### 4.1.1 概念说明

什么是「数值正确性测试」？它回答一个问题：**我写的内核，算出来的数和正确答案差多少？**

对一个高性能内核来说，最容易写、也最容易**骗人**的测试是：

```python
out = my_kernel(x)
assert out.shape == x.shape   # ← 这不是测试，这是烟雾弹
assert out is not None         # ← 同上
```

为什么这是烟雾弹？因为：

1. 一个形状对、但数值全错的张量照样能通过 `shape` 检查——比如一个全零输出、或者把行和列算反的输出。
2. 内核最常见的 bug（谓词越界、归约顺序错、布局转置错）往往**不改变形状**，只改变数值。
3. QuACK 的内核会经 `torch.compile` 走一条完全不同的调度路径，形状正确不代表两条路径数值一致。

因此 QuACK 的测试哲学（见 `AGENTS.md`）非常明确：

> 每个测试都必须与一个参考实现做**数值比较**，而不是只检查形状或「不崩」。只查 `.shape` 的测试不是测试，它掩盖 bug。

「参考实现」就是用 PyTorch 已经可信的原生算子写一份「标准答案」。对 softmax 来说，参考实现就是 `torch.nn.functional.softmax`；对 GEMM 来说就是 `torch.bmm`。这两者都经过长期验证，可以当作「地面真值（ground truth）」。

但高性能内核用的是低精度 dtype（bf16/fp16），它的输出不可能与 float32 参考逐位相等。于是需要一个**容差（tolerance）**：只要误差在容差内，就认为正确。

#### 4.1.2 核心流程

一个 QuACK 数值正确性测试的标准流程：

```
1. 设定容差表 TOLERANCES: {dtype: (atol, rtol)}
2. 构造输入 x（带梯度），并克隆一份 x_ref 给参考路径
3. 选择执行路径: function = torch.compile(op) if use_compile else op
4. 跑内核: out = function(x)         ← 待测路径
5. 跑参考:   out_ref = F.softmax(x_ref, dim=-1)   ← 标准答案
6. 构造上游梯度 dy，synchronize，分别取 autograd.grad
7. 断言形状/ dtype (必要但非充分)
8. torch.testing.assert_close(out, out_ref, atol=, rtol=)   ← 核心数值断言
9. 断言语义性质 (如 softmax 行和≈1、值域 [0,1])
10. assert_close(dx, dx_ref, ...)   ← 反向也要比
```

容差的数学含义来自 `torch.testing.assert_close`，判据是：

\[
|a - b| \le \mathrm{atol} + \mathrm{rtol} \cdot |b|
\]

即误差不得超过一个绝对项加一个与真值成比例的相对项。dtype 越低精度，`atol`/`rtol` 越大。

`use_compile` 双路径的设计动机：`torch.compile` 会把算子重写、融合、经 FX graph 追踪后插入自定义算子节点，这是一条与 eager 调用**完全不同的执行路径**。一个内核可能在 eager 下正确、但在 `torch.compile` 下因 functionalization 或 fake tensor 处理出错。因此每个数值用例必须跑两遍：`use_compile=False` 和 `use_compile=True`。

#### 4.1.3 源码精读

**(a) 容差表**——按 dtype 给出 (atol, rtol)：

[tests/test_softmax.py:14-18](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/tests/test_softmax.py#L14-L18) 定义了三个 dtype 的容差：bf16 最松（1e-2），fp16 居中（1e-3），fp32 最严（1e-4）。这是因为低精度浮点的表示误差更大。

**(b) 参考实现与双路径**——测试体的核心几行：

[tests/test_softmax.py:43](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/tests/test_softmax.py#L43) 用一行三元表达式切换 eager / compile 路径：`function = torch.compile(softmax, fullgraph=True) if use_compile else softmax`。

[tests/test_softmax.py:52-53](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/tests/test_softmax.py#L52-L53) 待测路径调 `function(x)`，参考路径调 PyTorch 原生 `F.softmax(x_ref, dim=-1)`。注意 `x` 和 `x_ref` 是**独立克隆**的（`x_ref = x.detach().clone()`），保证两条路径的输入互不干扰。

**(c) 同步后取梯度**——承接 u2-l3 的关键：

[tests/test_softmax.py:55-57](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/tests/test_softmax.py#L55-L57) 先 `torch.cuda.synchronize()`，再分别对两条路径取 `torch.autograd.grad`。注释「without sync, torch.autograd gets wrong results」直接点明：前向 CUDA 内核异步，不同步就读 `out` 取梯度会拿到未完成的数据。

**(d) 数值断言 + 语义性质**：

[tests/test_softmax.py:63-68](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/tests/test_softmax.py#L63-L68) 是测试的灵魂：`assert_close(out, out_ref, atol=atol, rtol=rtol)` 做数值比对；随后 `assert_close(sums, ones, atol=1e-4)` 验证「softmax 行和为 1」这一**数学性质**（比纯数值比对更强的语义检查）；`assert (out >= 0).all()` 验证值域。最后 `assert_close(dx, dx_ref, ...)` 把反向也算一遍。

**(e) 硬件跳过**——GEMM 测试用 `requires_sm90` 标记跳过不支持 SM90 以下的硬件：

[tests/test_gemm_functional.py:15-18](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/tests/test_gemm_functional.py#L15-L18) 用 `pytest.mark.skipif` 在 `device_capability < (9, 0)` 时跳过整个用例。这是「按硬件分发测试」的标准手法。

**(f) FakeTensor 形状校验**——专给 `torch.compile` 的 FX pass 用：

[tests/test_gemm_functional.py:56-66](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/tests/test_gemm_functional.py#L56-L66) 在 `FakeTensorMode()` 下跑算子，断言它报告的 shape/dtype 正确。这与数值正确性**互补**：数值测试要真跑 CUDA（慢），FakeTensor 测试不碰 CUDA（快），只验证「自定义算子的 meta 声明与 FX 期望一致」——因为 `torch.compile` 的 shape/dtype 推断完全依赖这个 meta。

#### 4.1.4 代码实践

> **实践目标**：亲手验证「只查形状会漏掉数值 bug」。

**操作步骤**（阅读型实践，无需改源码）：

1. 打开 [tests/test_softmax.py:63](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/tests/test_softmax.py#L63)，看清数值断言这一行。
2. 设想一个「坏 softmax」：它把每行求和后**忘记归一化**，直接返回 `exp(x - max) / sum` 错写成 `exp(x - max)`（即漏除了 sum）。
3. 这个坏内核输出的形状、dtype 都和正确版**完全一样**，`assert out.shape == x.shape` 会通过。
4. 但 `[63]` 的 `assert_close(out, out_ref)` 会立刻失败，因为坏输出未归一化、数值远大于参考。

**需要观察的现象**：
- 形状断言（[59-62 行](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/tests/test_softmax.py#L59-L62)）对这种 bug **毫无反应**。
- 行和性质断言（[64-65 行](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/tests/test_softmax.py#L64-L65) `assert_close(sums, ones)` 甚至比数值比对更早暴露问题——行和会远大于 1。

**预期结果**：你能用一句话说明「形状正确 ≠ 数值正确」，并解释为何 QuACK 把 `assert_close` 列为测试的**必要条件**。

> 待本地验证：如果你手头有 GPU，可临时拷贝 `test_softmax` 改名，把参考 `out_ref` 乘以 1e6 模拟「坏内核」，运行 `pytest tests/test_softmax.py -k 'bfloat16 and not use_compile'` 观察失败信息。

#### 4.1.5 小练习与答案

**练习 1**：为什么容差表里 bf16 是 `1e-2`，而 fp32 是 `1e-4`？把 bf16 也设成 `1e-4` 会怎样？

> **答案**：bf16 只有 7 位尾数（约 3 位有效十进制），表示误差本身就接近 1e-2 量级；fp32 有 23 位尾数（约 7 位有效十进制），误差在 1e-7 量级，`1e-4` 已留足余量。若把 bf16 容差设成 `1e-4`，正确的内核也会因「合法的舍入误差」而**误报失败**，测试就不可用了。

**练习 2**：`use_compile=True` 这条路径为何不能省略？

> **答案**：`torch.compile` 会把算子重写、融合、经 FX 追踪后插入自定义算子节点，这是一条与 eager 完全不同的调度路径。eager 正确不保证 compile 正确（functionalization、fake tensor、Inductor 重排都可能引入 bug）。QuACK 的用户大多通过 `torch.compile` 调用内核，所以双路径必须都测。

**练习 3**：[tests/test_softmax.py:64-67](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/tests/test_softmax.py#L64-L67) 的「行和为 1」「值域 [0,1]」属于什么性质？它们和 `assert_close(out, out_ref)` 重复吗？

> **答案**：它们是 **数学不变量（property-based）** 检查，与逐元素数值比对**互补而非重复**。`assert_close` 要求一个参考实现；而「行和为 1」是 softmax 的定义性质，不依赖参考实现就能判定对错，有时能在参考实现本身有问题时兜住底。

---

### 4.2 pytest 参数化与 -k 过滤

#### 4.2.1 概念说明

QuACK 的测试面临一个现实困难：**测试矩阵会爆炸**。

以 [tests/test_softmax.py:24-31](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/tests/test_softmax.py#L24-L31) 为例，单个 `test_softmax` 函数被四个 `@pytest.mark.parametrize` 装饰器叠加：

- `input_dtype`：3 种（bf16/fp16/fp32）
- `N`：9 种（192…262144）
- `M`：2 种（1, 199）
- `use_compile`：2 种（True/False）

展开后是 \(3 \times 9 \times 2 \times 2 = 108\) 个用例。再加上每个用例**第一次冷编译要几百毫秒**、且**必须有一张目标 GPU**，跑全套既慢又贵。所以 QuACK 必须能精确切片运行：只跑某个 dtype、某个 N、某条 compile 路径。

这就是 `-k`（关键字过滤）、`-x`（遇错即停）、`-n`（多卡并行）、`--async-compile`（冷编译后台化）四个开关的用武之地。同时，不同 GPU 架构支持的配置不同（如 SM120 只有 99KB SMEM，超大 N 要跳过），需要**按硬件动态跳过**。

而 `tests/conftest.py` 在测试基础设施层面解决三个项目专属难题：

1. 多 GPU（xdist）下把 worker 轮流钉到不同卡，避免所有 worker 抢同一张卡。
2. 用例收集后打印总数汇总，让巨大的参数化矩阵可视化。
3. 用例 OOM 时清显存自动重试一次。

#### 4.2.2 核心流程

**用例切片（命令行层）**：

```
pytest tests/test_softmax.py -x                       # 遇错即停（快速定位首个失败）
pytest tests/test_softmax.py -k 'bfloat16'            # 只跑 bf16 用例
pytest tests/test_softmax.py -k 'bfloat16 and not use_compile'   # bf16 + eager
pytest tests/ -n 8                                    # 8 个 xdist worker 多卡并行
pytest tests/ -n 8 --async-compile=32                 # + 32 worker 后台编译
```

`-k` 的关键字匹配的是**用例名**（由参数值拼成），如 `test_softmax[1-192-bfloat16-True]`。`and`/`not`/`or` 组合可精细筛选。

**conftest 的执行时序**：

```
1. (import 期) _assign_xdist_worker_gpu()        ← 必须早于任何 CUDA 触摸
   → 每个 xdist worker 钉一张 GPU（round-robin）
2. pytest_plugins = ["quack.testing.pytest_plugin"]   ← 加载异步编译插件
3. pytest_collection_finish()                      ← 收集完打印用例数汇总
4. 每个用例: pytest_runtest_call (OOM 重试 + CompilePending 延迟)
```

关键约束：**GPU 分配必须在 import 期完成**。因为导入 `quack` 会触发 CUTLASS/PyTorch 触碰 CUDA，如果此时 `CUDA_VISIBLE_DEVICES` 还包含全部卡，CUDA 就会缓存更大的设备集，之后在 `pytest_configure` 里再收窄就太晚了——所有 worker 都会默认用「逻辑 GPU 0」。

#### 4.2.3 源码精读

**(a) 参数化矩阵**——四个叠加的 decorator：

[tests/test_softmax.py:24-31](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/tests/test_softmax.py#L24-L31) 把一个函数展开成 108 个用例。注释（[21-23 行](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/tests/test_softmax.py#L21-L23)）说明为何精挑这些值：M=37 与 M=199 共享小批量 tile 选择；被删掉的 N 值（512/2048/…）都落在相邻值已覆盖的区间内——**参数化不是越多越好，而是要覆盖不同 tile 阶段（单 stage / 多 stage / SMEM 边界）**。

**(b) 按硬件跳过**——SM120 的 99KB SMEM 限制：

[tests/test_softmax.py:36-40](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/tests/test_softmax.py#L36-L40) 在用例体里查 `device_capability`，major==12（GeForce RTX 50）时，超大 N 会超过 99KB SMEM，于是 `pytest.skip("SM12x: exceeds 99 KB SMEM")`。这是「运行期按硬件跳过」的范式——`skipif` 标记适合整类跳过，体里的 `pytest.skip` 适合依赖具体参数值的跳过。

**(c) GPU 轮询分配**——conftest 的第一职责：

[tests/conftest.py:35-55](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/tests/conftest.py#L35-L55) 的 `_get_gpu_ids()` 先读 `CUDA_VISIBLE_DEVICES`，否则 fallback 到 `nvidia-smi` 查可用卡。

[tests/conftest.py:69-89](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/tests/conftest.py#L69-L89) 的 `_assign_xdist_worker_gpu()` 把第 `worker_num` 号 worker 钉到 `gpu_ids[worker_num % len(gpu_ids)]`，然后**立刻改写** `os.environ["CUDA_VISIBLE_DEVICES"]`。它返回三元组 `(worker_id, assigned_gpu, gpu_ids)`。

[tests/conftest.py:92](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/tests/conftest.py#L92) `_PRECONFIGURED_WORKER_GPU = _assign_xdist_worker_gpu()` 在**模块 import 期**就执行——这是「赶在 CUDA 触摸之前」的关键。

**(d) 加载异步编译插件**：

[tests/conftest.py:98](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/tests/conftest.py#L98) `pytest_plugins = ["quack.testing.pytest_plugin"]` 把 `--async-compile` 命令行选项和「冷 miss 抛 `CompilePending` → 延迟 → 重试」的循环（见 [quack/testing/pytest_plugin.py:265-303](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/testing/pytest_plugin.py#L265-L303)）挂进来。注释强调：**先分配 GPU，再加载插件**，因为导入插件会触发 `quack.__init__` → CuTe/CUTLASS import → 触碰 CUDA。

**(e) 用例数汇总**：

[tests/conftest.py:123-138](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/tests/conftest.py#L123-L138) 的 `pytest_collection_finish` 在收集完后按「文件 → 函数」统计用例数，打印成 JSON。面对上百个参数化用例，这条汇总让你一眼看到「到底要跑多少」。

**(f) OOM 重试**——QuACK 专属的容错：

[tests/conftest.py:156-216](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/tests/conftest.py#L156-L216) 是一个 `pytest_runtest_call` hookwrapper：用例抛 CUDA OOM 时，先 `gc.collect()` + `torch.cuda.empty_cache()` 释放显存，再 `item.runtest()` **重试一次**。特别注意 [203-213 行](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/tests/conftest.py#L203-L213)：重试若再抛 `CompilePending`（冷编译未就绪），要交给延迟机制而非报失败；若再抛别的异常（含第二次 OOM），则用 `outcome.force_exception` 正常报失败。这段长注释记录了一个真实 CI bug：teardown 里抛异常会触发 pluggy 警告并跳过后续 teardown，所以一切异常必须经 `outcome` 路由。

#### 4.2.4 代码实践

> **实践目标**：用 `-k` 精确切片一个巨大的参数化矩阵，看清 conftest 的汇总输出。

**操作步骤**：

1. 进入仓库根目录。
2. 不带任何过滤跑**收集**（不执行），看 conftest 打印的用例数汇总：
   ```bash
   pytest tests/test_softmax.py --collect-only -q
   ```
3. 用 `-k` 只收集 bf16、eager（非 compile）的用例：
   ```bash
   pytest tests/test_softmax.py --collect-only -q -k 'bfloat16 and not use_compile'
   ```
4. 对比两次输出的用例数，确认 `-k` 把 108 缩减到了 \(9 \times 2 \times 1 \times 1 = 18\) 个。

**需要观察的现象**：
- 第 2 步会看到 conftest 的 `pytest_collection_finish` 打印类似 `Collected 108 tests: {...}` 的 JSON 汇总。
- 第 3 步用例数大幅下降，且每个用例名都含 `bfloat16`、不含 `True`（use_compile）。

**预期结果**：你能解释「`-k 'bfloat16 and not use_compile'`」如何匹配用例名，并理解为何冷编译场景下用 `-k` 切片能把首轮编译时间从「上百个 cubin」降到「少数几个」。

> 待本地验证：上述 `--collect-only` 不需 GPU、不触发编译，可在任何机器上跑；实际执行用例需要一张 SM90+ 的 GPU。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `_assign_xdist_worker_gpu()` 必须在 conftest 的 **import 期** 执行，而不是在 `pytest_configure` hook 里？

> **答案**：因为 `pytest_plugins = ["quack.testing.pytest_plugin"]` 的加载（以及随后导入 `quack` 包）会触发 CuTe/CUTLASS/PyTorch 触碰 CUDA，CUDA 会缓存当时 `CUDA_VISIBLE_DEVICES` 暴露的设备集。若此时还是全部卡，`pytest_configure`（更晚）再收窄就来不及了——所有 worker 都会默认落到逻辑 GPU 0。import 期执行才能抢在 CUDA 缓存设备之前收窄。

**练习 2**：`-k 'bfloat16'` 和直接修改测试文件把 `input_dtype` 参数列表改成只剩 `bfloat16`，哪个更好？为什么？

> **答案**：`-k` 更好。它**不修改源码**，只影响本次运行；CI 和本地都能用不同过滤跑同一份代码。改源码会缩小覆盖面、可能被误提交，且要为每台机器维护不同版本。QuACK 的原则是「源码定义全集、命令行做切片」。

**练习 3**：conftest 的 OOM 重试为何「只重试一次」而不是无限重试？

> **答案**：OOM 重试是为了应对**瞬时显存碎片**（上一个用例的显存未及时回收）这类偶然情况。若重试一次仍 OOM，说明是**真实内存不足**（用例本身就太大或硬件不够），无限重试只会浪费时间。重试一次能把「假 OOM」滤掉，又不会让真 OOM 永远挂着——同时第二次失败会被 `force_exception` 正常报告，不掩盖问题。

---

### 4.3 基准测量协议

#### 4.3.1 概念说明

数值正确性回答「对不对」，基准（benchmark）回答「快不快」。但「快」这个度量在 GPU 上**极容易测错**，原因有三：

1. **L2 缓存会撒谎**。`triton.testing.do_bench` 的常见用法是反复对**同一个张量**调用 kernel。这个张量第二次起就躺在 L2 里，kernel 实际只从 L2（快）而非 HBM（慢）取数。于是你测到的「快」是 L2 命中的虚高，不是真实生产环境（数据冷、必须从 HBM 取）的延迟。
2. **CPU launch 开销会污染短 kernel**。对一次 kernel 调用计时，如果用 Python `for` 循环跑很多次，每次 launch 的 CPU 开销（约 3.5µs）会算进「kernel 时间」里。kernel 越短（<100µs），这个污染越严重。
3. **时钟会漂移**。GPU 有 boost 时钟，短基准测试会撞上 boost（比稳态快 15-25%）；共享机器上别的租户会让时钟和 L2 状态漂移 2-3 倍。

QuACK 提供两套基准协议应对这些：

- **简单协议**（`triton.testing.do_bench` + `perf_report`）：用于 softmax 这类 I/O bound 内核的快速吞吐对比。代码在 `benchmarks/benchmark_softmax.py`。
- **L2-cold CUDA-graph 轮转协议**（`quack/bench/bench_utils.py`）：用多份克隆输入轮流喂 kernel、用 CUDA Graph 录制后单次重放计时，专门消除上述三类误差。这是 QuACK 基准的**严肃协议**。

二者并非互斥：简单协议适合「看大致趋势」，L2-cold 协议适合「出可发表的精确数字」。

#### 4.3.2 核心流程

**简单协议（do_bench）的流程**（以 [benchmarks/benchmark_softmax.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/benchmarks/benchmark_softmax.py) 为例）：

```
1. 定义 runner(M, N, provider, dtype) -> dict {"ms", "GB/s"}
2. 构造输入 x，按 provider 选 fn = lambda: kernel(x)
3. ms = do_bench(fn, warmup=10, rep=100)     ← triton 计时
4. 按 I/O 字节算带宽: GB/s = numel_rw * elem_bytes / (ms/1000) / 1e9
5. perf_report 遍历 (M,N) 组合与 provider，run_and_print 打印表格/存 CSV
```

**L2-cold CUDA-graph 轮转协议的流程**（`_bench_cuda_graph_l2_rotate`）：

```
1. _pick_l2_rotate_count(): 算需要几份克隆输入，使每轮克隆字节 > 3×L2 大小
2. _clone_l2_rotate_inputs(): 把输入克隆成 n_sets 份（独立 GMEM 地址）
3. priming: 跑 3 次预热，避开首 launch 的驱动/加载开销
4. probe: 计时单次 launch，估算 warmup 次数（凑够 warmup_target_ms=200ms GPU 工作）
5. warmup: 普通 Python 循环在轮转的 n_sets 上跑，排空流水线
6. capture: 用 torch.cuda.graph 录制「rounds × n_sets」次轮转调用为一张图
7. replay + 计时: 单次 replay 覆盖所有计时调用，ms = elapsed / total_calls
```

为什么这能消除三类误差？

- **L2 撒谎** → 步骤 1-2 保证每份输入独立、且总量超 L2，轮转访问让 L2 无法长期命中同一数据。
- **launch 开销** → 步骤 6-7 把 N 次调用录成图，单次 `replay()` 重放，Python 循环和每 launch 的 CPU 开销都消失了，测到的纯是 GPU 工作。
- **流水线欠热** → 步骤 5 的**按时间计的 warmup**（而非固定次数）保证重型流水线配置（TMA + smem_stages=3）被充分预热，否则计时窗会捕捉到「欠热」的虚高。

带宽/算力的换算公式（用于把 ms 转成 GB/s 或 TFLOPS）：

\[
\text{GB/s} = \frac{\text{传输字节数}}{t_{\text{ms}}/1000 \times 10^9}, \qquad
\text{TFLOPS} = \frac{2 \cdot M \cdot N \cdot K}{t_{\text{ms}} \times 10^9}
\]

其中 softmax 前向 I/O 是「读 x + 写 y」= \(2 \times \text{numel}\) 元素，反向是「读 y + 读 dy + 写 dx」= \(3 \times \text{numel}\) 元素（见 [benchmarks/benchmark_softmax.py:117](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/benchmarks/benchmark_softmax.py#L117) 与 [146 行](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/benchmarks/benchmark_softmax.py#L146)）。

#### 4.3.3 源码精读

**(a) 简单协议——runner 与 do_bench**：

[benchmarks/benchmark_softmax.py:98-117](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/benchmarks/benchmark_softmax.py#L98-L117) 的 `softmax_fwd_runner` 按 `provider` 选待测函数（quack / torch.compile / liger），调 `do_bench(fn, warmup=10, rep=100)` 得 ms，再用 `_result` 算带宽返回。注意 [115 行](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/benchmarks/benchmark_softmax.py#L115) `do_bench` 对**同一个 `x`** 反复调用——这就是 L2 命中问题的来源。

[benchmarks/benchmark_softmax.py:45-48](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/benchmarks/benchmark_softmax.py#L45-L48) 的 `_result` 把总读写元素数与字节/元素换算成 GB/s。

[benchmarks/benchmark_softmax.py:165-169](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/benchmarks/benchmark_softmax.py#L165-L169) 用 `perf_report(...)` 装饰 runner，`run_and_print` 遍历 (M,N) × provider 网格、打印表格、可选存 CSV。

**(b) run_and_print——结果聚合**：

[quack/bench/bench_utils.py:16-33](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/bench/bench_utils.py#L16-L33) 遍历每个 `Benchmark`，对每个 x 值、每条 line（provider）调用 runner，要求 runner 返回**同构的 dict**（键必须一致，见 [_run_one 的校验](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/bench/bench_utils.py#L56-L60)），最终拼成 `x_names + [line_name (stat)]` 的 DataFrame。

**(c) L2-cold 协议——轮转缓冲数量**：

[quack/bench/bench_utils.py:179-202](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/bench/bench_utils.py#L179-L202) 的 `_pick_l2_rotate_count` 算需要几份克隆：目标是「克隆输入总字节 > `target_ratio × L2_size`」（默认 ratio=3），再受 HBM 余量与 `[4,16]` 上下限约束。注释强调它把 `args` 和 `kwargs` 里的张量都计入（包括 `dw_partial`/`dx` 这类**写目标** kwarg），确保写路径也参与 L2 周转。

**(d) L2-cold 协议——克隆输入**：

[quack/bench/bench_utils.py:158-176](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/bench/bench_utils.py#L158-L176) 的 `_clone_l2_rotate_inputs` 把每个张量参数克隆 `n_buffers` 份，非张量（int/str/dataclass）共享不克隆。这样每次录制的 launch 触碰**不同 GMEM 地址**，L2 无法长期缓存。

**(e) L2-cold 协议——核心计时循环**：

[quack/bench/bench_utils.py:114-135](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/bench/bench_utils.py#L114-L135) 是消除误差的关键：先 3 次 priming（[116-118 行](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/bench/bench_utils.py#L116-L118)），再 probe 单次估时（[122-128 行](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/bench/bench_utils.py#L122-L128)），按 `warmup_target_ms / est_ms` 算 warmup 次数（[129 行](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/bench/bench_utils.py#L129)），然后在轮转集上 warmup（[132-135 行](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/bench/bench_utils.py#L132-L135)）。docstring（[83-87 行](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/bench/bench_utils.py#L83-L87)）明确：固定次数 warmup 会让重型流水线配置「欠热」（虚高）、轻型配置「过度预热」（浪费），按时间计才公平。

[quack/bench/bench_utils.py:137-151](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/bench/bench_utils.py#L137-L151) 把 `rounds × n_sets` 次轮转调用**录进一张 CUDA Graph**，单次 `replay()` 重放，用一对 CUDA event 计时，最后除以 `total_timed_calls` 得 ms/次。这就把 Python 循环和每 launch CPU 开销**完全排除**在计时之外。docstring（[93-99 行](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/bench/bench_utils.py#L93-L99)）说得很直白：轮转击败了 `do_bench` 在同一张量上反复调用导致的 L2 命中虚高；L2-cold 数字更能预测真实生产延迟。

**(f) GEMM 基准的 cuBLAS 对照与时钟缓冲**：

[benchmarks/benchmark_gemm.py:57-70](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/benchmarks/benchmark_gemm.py#L57-L70) 的 `_bench_and_report` 在 `do_bench` 前 `time.sleep(0.5)`——这是给时钟一个缓冲，缓解相邻测量间 L2/时钟状态串联污染。它把 ms 换算成 TFLOPS（和可选 GB/s），并和 cuBLAS 对照（[830-836 行](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/benchmarks/benchmark_gemm.py#L830-L836) 交替跑 cuBLAS 与 quack，避免一方总在 L2 热时跑）。

#### 4.3.4 代码实践

> **实践目标**：对比 L2 命中对短 kernel 计时的影响，理解为何要轮转。

**操作步骤**（阅读型 + 本地可选）：

1. 阅读 [quack/bench/bench_utils.py:93-99](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/bench/bench_utils.py#L93-L99) 的 docstring，理解「轮转击败 L2 命中虚高」这句话。
2. 阅读 [benchmarks/benchmark_softmax.py:115](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/benchmarks/benchmark_softmax.py#L115)，注意 `do_bench(fn, ...)` 里的 `fn = lambda: softmax(x)` 捕获的是**同一个 `x`**。
3. 设想一个推理：对这个固定 `x`，第 2 次起的 softmax 调用读 `x` 时它已躺在 L2，于是带宽被高估。
4. （本地有 GPU 时）跑 softmax 基准，对比 quack 在小 N（如 256）与大 N（如 65536）下的 GB/s：
   ```bash
   python benchmarks/benchmark_softmax.py --dtype bfloat16 --M 32768 --N 256
   python benchmarks/benchmark_softmax.py --dtype bfloat16 --M 32768 --N 65536
   ```

**需要观察的现象**：
- 小 N 时内核更短、L2 命中占比更高，测出的「GB/s」可能高得离谱（甚至超过 HBM 物理带宽），这是 L2 命中虚高的典型征兆。
- 大 N 时数据远超 L2，GB/s 趋近真实 HBM 带宽，数字更可信。

**预期结果**：你能用一句话解释「为什么小 N 的基准数字不可信」，并说出 QuACK 的 L2-cold 轮转协议（`_clone_l2_rotate_inputs` + `_pick_l2_rotate_count`）是如何靠「多份独立输入 + 总量超 3×L2」来修正的。

> 待本地验证：第 4 步的具体 GB/s 数值依赖具体 GPU 型号，且 `do_bench` 路径未做 L2-cold，结果仅用于体会虚高趋势，不是严肃数字。

#### 4.3.5 小练习与答案

**练习 1**：`_bench_cuda_graph_l2_rotate` 为什么用「按时间计 warmup」（凑够 200ms GPU 工作）而不是「固定跑 1000 次」？

> **答案**：不同 kernel 单次耗时差异巨大。固定次数会让重型流水线配置（TMA + smem_stages=3）欠热——计时窗捕捉到「流水线还没填满」的虚高快；同时又让轻型配置过度预热浪费。按 `warmup_target_ms / est_ms` 算次数，保证**所有配置都预热到稳态**，比较才公平。

**练习 2**：为什么把 N 次调用录进一张 CUDA Graph、再单次 `replay()` 计时，能消除 CPU launch 开销的污染？

> **答案**：录图后，N 次 kernel launch 的 CPU 编排只在**录制时**发生一次；`replay()` 由 GPU 端重放，Python 循环和每次 launch 的 CPU 开销（约 3.5µs/次）都不在计时窗内。测到的 `elapsed / total_calls` 纯粹是 GPU 工作。对短于 100µs 的 kernel，这是避免 CPU 开销淹没真实 kernel 时间的关键。

**练习 3**：`_pick_l2_rotate_count` 为什么要「目标克隆字节 > 3×L2 大小」？ratio=1 行不行？

> **答案**：ratio=1 时一轮克隆刚好等于 L2，但 L2 是组相联的、访问有局部性，刚写入的数据可能还在 L2 命中。倍数（3×）留出余量，确保轮转到同一份输入时它**已经确定被挤出 L2**，从而迫使数据从 HBM 取，复现真实生产场景。ratio 太小会让 L2 仍部分命中，基准再度虚高。

---

## 5. 综合实践

> **任务**：以 `tests/test_softmax.py` 为模板，为 QuACK 的 `cross_entropy`（交叉熵）内核写一个完整的数值正确性测试。

要求你的测试文件（例如 `tests/test_cross_entropy.py`，**示例文件，本讲不实际写入**）包含以下要素：

1. **容差表**：仿照 [tests/test_softmax.py:14-18](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/tests/test_softmax.py#L14-L18)，为 bf16/fp16/fp32 各设 (atol, rtol)。
2. **参数化矩阵**：用 `@pytest.mark.parametrize` 覆盖 dtype、序列长度 N、batch M、`use_compile`，但**精选值**（参考 softmax 注释 [21-23 行](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/tests/test_softmax.py#L21-L23) 的思路：覆盖不同 tile 阶段，而非穷举）。
3. **参考实现**：用 `F.cross_entropy`（或先 softmax 再 NLL）作 ground truth。
4. **双路径**：`function = torch.compile(cross_entropy, fullgraph=True) if use_compile else cross_entropy`。
5. **同步后取梯度**：仿 [55-57 行](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/tests/test_softmax.py#L55-L57)，前向后 `torch.cuda.synchronize()` 再取 `autograd.grad`，前后向都 `assert_close`。
6. **硬件跳过**：若 cross_entropy 在 SM12x 上有 SMEM 限制，仿 [36-40 行](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/tests/test_softmax.py#L36-L40) 用 `pytest.skip`。
7. **运行方式**：写明如何用 `-k` 切片，例如 `pytest tests/test_cross_entropy.py -k 'bfloat16 and not use_compile' -x`。

**自检问题**（做完后回答）：
- 你的测试如果只断言 `out.shape`，能否发现「交叉熵把 log 和 softmax 顺序写反」的 bug？（不能 → 证明数值断言不可或缺。）
- 你的参考实现是 float32 吗？为什么？（是；参考必须用高精度作 ground truth，否则用低精度参考等于「瞎子领瞎子」。）

> 待本地验证：本实践需要实际编写并运行测试，依赖具体 cross_entropy 公开 API 的签名（建议先用 `from quack import cross_entropy` 确认入参），本讲不假定其结果。

## 6. 本讲小结

- **数值正确性是底线**：QuACK 测试必须把内核输出与 PyTorch 参考实现做 `torch.testing.assert_close` 比对（容差 `atol+rtol·|b|`），只查形状的测试是烟雾弹；bf16 容差最松（1e-2）、fp32 最严（1e-4）。
- **双路径 + 语义性质**：每个数值用例都要跑 `use_compile=False/True` 两条路（eager 与 `torch.compile` 是不同执行路径），并辅以「行和为 1」「值域」等不依赖参考的数学不变量检查；反向梯度同样要比对。
- **异步内核要先同步**：取 autograd 梯度前必须 `torch.cuda.synchronize()`，否则读到未完成的前向输出。
- **参数化矩阵会爆炸**：四个 `parametrize` 叠加可产生上百用例，靠 `-x`（遇错即停）、`-k`（关键字过滤）、`-n`（多卡并行）、`--async-compile`（冷编译后台化）精确切片；conftest 在 import 期把 xdist worker 钉到不同 GPU、提供 OOM 重试与用例数汇总。
- **硬件跳过两件套**：整类跳过用 `pytest.mark.skipif`（如 `requires_sm90`），依赖具体参数的跳过用体里的 `pytest.skip`（如 SM12x 的 99KB SMEM 限制）。
- **基准有三种陷阱**：L2 命中虚高、CPU launch 开销污染、时钟漂移；QuACK 用「L2-cold CUDA-graph 轮转」（多份克隆输入 + 总量超 3×L2 + 录图单次重放）严肃应对，用 `do_bench` + `perf_report` 做快速吞吐对比。

## 7. 下一步学习建议

本讲是 QuACK 学习路线的最后一篇。要继续深化，建议：

1. **亲手写一个新内核的完整测试**：按「综合实践」为 `rmsnorm` 或 `cross_entropy` 写测试，并用 `--async-compile` 跑一遍，体会冷编译与测试并行的真实流程。
2. **对比两套基准协议**：拿同一个 softmax 配置分别用 `do_bench`（[benchmark_softmax.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/benchmarks/benchmark_softmax.py)）和 `_bench_cuda_graph_l2_rotate`（[bench_utils.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/bench/bench_utils.py)）测，量化「L2 命中虚高」到底有多大。
3. **通读 conftest + pytest_plugin 的延迟-重试机制**：把 [tests/conftest.py:156-216](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/tests/conftest.py#L156-L216) 与 [quack/testing/pytest_plugin.py:265-338](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/testing/pytest_plugin.py#L265-L338) 连起来读，理解 OOM 重试、`CompilePending` 延迟、`_MAX_ATTEMPTS` 兜底如何协同。
4. **回顾全路线**：从 u1-l1 的项目定位，到本讲的测试与基准，你已经走完「认识项目 → DSL 入门 → 工具层 → GEMM 主机侧 → GEMM 设备侧 → epilogue → 量化/分布式 → 调优/缓存/测试」的完整链条。现在可以挑一个内核（如 rmsnorm）做端到端的「读源码 → 写测试 → 跑基准」闭环练习。

至此，QuACK 学习手册全部完成。
