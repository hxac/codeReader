# 夜行基准 CI 运行器（进程隔离与失败隔离）

## 1. 本讲目标

本讲承接 [u6-l1 bench_kernel 与 SOL 计时协议](u6-l1-bench-kernel-sol-protocol.md) 与 [u6-l3 报告与基线对比](u6-l3-benchmark-report-and-baselines.md)：

- u6-l1 解决了「**单次**测出的 kernel latency 是否可信」。
- u6-l3 解决了「一份基准文件里的数字如何收集、归类、落盘」。
- 本讲解决的是上一层的工程问题：**当夜行 CI 要把几十个基准文件串起来跑时，如何保证一个文件的崩溃不会拖垮整个 session，并且每一份结果都不丢失**。

读完本讲，你应该能够：

1. 解释「每文件一进程」的隔离动机：一次原生的 hang / segfault / OOM 只损失一个文件，而不是整个 session。
2. 画出父进程与子进程的完整交互（collect-only → stdin grant → 运行 → rc 回传），并解释为何父进程绝不导入 torch、为何延后 CUDA 初始化。
3. 理解超时与信号死亡两条失败路径：py-spy 抓栈、SIGKILL 清理、合成 junit 条目如何携带日志尾部、teardown 崩溃如何不被误读为成功。

> 本讲对应的代码实践任务：阅读 `run_benchmarks.py`，画出父/子交互时序，回答三个设计问题（见第 4.4 节）。

---

## 2. 前置知识

### 2.1 为什么要单独讲「运行器」

u6-l1 已经讲过 `bench_kernel` 用 CUPTI 做纯 kernel 计时，u6-l3 讲过 `BenchmarkReport` 把每个用例的结果收集进 `profile_run.log` 与 JUnit XML。这些都是**一个基准文件、一个 pytest 进程内部**的事。

但夜行 CI 真实运行时，`benchmarks/ops/` 下有几十个 `bench_*.py` 文件。如果用一个巨大的 `pytest benchmarks/ops/` 一次跑完，会遇到三个麻烦：

| 麻烦 | 后果 |
| --- | --- |
| 某个算子 kernel 触发 GPU **段错误（segfault）** | 整个 pytest 进程被杀，后面所有文件的数字全部丢失 |
| 某个 kernel **挂死（hang）** | 整个 session 卡住，CI 超时，什么报告都拿不到 |
| 某个大 MoE 算子 **OOM** | 同上，进程被内核 SIGKILL |

「夜行」跑一次代价很高（独占一块 H200、几十分钟），如果因为一个文件的崩溃就丢掉全部数字，太浪费。**进程隔离**就是针对这三类「原生失败」的工程兜底。

### 2.2 关键术语

- **进程隔离（per-file isolation）**：每个基准文件在自己的子进程里跑，互不牵连。
- **父进程 / 子进程**：父进程只做调度与合并，绝不碰 GPU；子进程才是真正跑 pytest + torch + CUDA 的那个。
- **stdin grant（stdin 授权）**：子进程启动后先在 `sys.stdin.readline()` 上阻塞，等父进程写一个换行符才开跑——这是父进程「授权」子进程独占 GPU 的信号。
- **状态管道（status pipe）**：子进程跑完，把 pytest 退出码通过一根管道写回父进程，**写在解释器 teardown 之前**。
- **合成 junit 条目（synthetic suite）**：子进程死了来不及写 junit 时，父进程替它伪造一条 error 条目，让报告里不缺这一格。

### 2.3 你需要熟悉的两个前置事实

1. u6-l3 讲过：`BenchmarkReport.dump("profile_run.log")` 是挂在 `pytest_sessionfinish` 钩子上的（见 [benchmarks/conftest.py:23-28](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/benchmarks/conftest.py#L23-L28)）。**每个子进程有自己的一次 session**，所以每个文件会写自己的一份 `profile_run.log`，再由父进程拼起来。本讲的第 4.3 节会接上这条线。
2. u6-l1 讲过 SOL-ExecBench 协议的**第 1 步是「外部锁定 GPU 时钟」**（见 [benchmarks/benchmark_base.py:196-197](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/benchmarks/benchmark_base.py#L196-L197)）。这个「外部」就是本讲的夜行 CI 在运行器之外、用 `nvidia-smi` 做的时钟校验（见 4.2 节）。

---

## 3. 本讲源码地图

| 文件 | 角色 | 本讲用它讲什么 |
| --- | --- | --- |
| [scripts/ci/run_benchmarks.py](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/scripts/ci/run_benchmarks.py) | **核心**。父进程调度器 | 每文件一进程、stdin grant、失败隔离、junit 合并 |
| [benchmarks/tests/test_run_benchmarks.py](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/benchmarks/tests/test_run_benchmarks.py) | 运行器的自测 | 用真实的 `os.abort()` / `time.sleep(600)` 验证三条失败路径 |
| [.github/workflows/nightly.yml](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/.github/workflows/nightly.yml) | 夜行 CI 编排 | 时钟校验、py-spy 安装、运行器调用、产物上传 |
| [benchmarks/conftest.py](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/benchmarks/conftest.py) | 每个 pytest session 的钩子 | `clear()` / `dump()` 生命周期，解释每文件一份 profile log |

---

## 4. 核心概念与源码讲解

### 4.1 每文件一进程：把爆炸半径压到一个文件

#### 4.1.1 概念说明

先讲直觉。把基准跑成一串子进程，本质上是把「一次 CI 运行的爆炸半径」从「全部文件」缩小到「一个文件」。

形式化一点，设共有 \(N\) 个基准文件，单文件原生失败概率为 \(p\)：

- **单进程串跑**：任何一个文件失败，整条报告报废。全绿的期望代价约为 \(N \cdot p\) 个文件的损失（实际更糟，因为一旦崩了就停）。
- **每文件一进程**：一次原生失败最多损失 1 个文件，其余 \(N-1\) 个文件的数字照常进合并报告。

\[ 
\text{爆炸半径}_{\text{单进程}} = N,\qquad \text{爆炸半径}_{\text{每文件}} = 1 
\]

注意「每文件一进程」**不能**提高正确率（该崩的还是崩），它的价值是**保住其余文件的测量结果**，并给崩溃的那个文件留下诊断证据（堆栈、日志尾部）。这与 TileOPs「宁可失败，不可撒谎」的测量哲学一致（见 u6-l1 末尾的 `TILEOPS_ALLOW_CUDA_EVENTS_FALLBACK`）。

#### 4.1.2 核心流程

```
父进程 main()
  ├─ _discover_bench_files(targets)   # 文件系统遍历，绝不 import 任何基准模块
  ├─ for 每个文件：
  │    ├─ spawn 一个子进程（python -c _CHILD ...）
  │    ├─ child.release()              # 写 \n 到子进程 stdin → 授权它独占 GPU
  │    ├─ child.wait_result(timeout)   # 从状态管道读 rc；超时返回 None
  │    └─ 根据 rc：合并真实 junit / 合成 error junit
  └─ 把所有 testsuite 合并写出 → bench_results.xml
```

注意第一个细节：**文件发现绝不 import 基准模块**。如果发现阶段就 `import`，一个在模块顶层 `os.abort()` 的文件会在「还没开始隔离」时就把整个父进程炸了。所以发现阶段只做文件系统遍历：

> [_discover_bench_files](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/scripts/ci/run_benchmarks.py#L170-L187)：用 `Path.rglob("bench_*.py")` 遍历，**不导入任何模块**；如果某目录下一个可跑的文件都没有，pytest 会返回退出码 5（nothing collected），运行器据此区分「全跳过」与「真失败」。

这条注释把设计意图讲得很直白：

> [run_benchmarks.py:1-8](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/scripts/ci/run_benchmarks.py#L1-L8)（模块 docstring）：一次原生失败只损失一个文件；挂死的子进程会留下 py-spy 堆栈；**父进程绝不导入 torch**——子进程必须是全新进程。

#### 4.1.3 源码精读

子进程其实是一段**用字符串内联的 Python 前导码** `_CHILD`，通过 `python -c _CHILD <write_fd> <pytest args...> <bench_file>` 启动：

```python
# scripts/ci/run_benchmarks.py:35-47 （子进程前导码，精简）
import ctypes, os, sys
ctypes.CDLL(None).prctl(0x59616D61, os.getppid(), 0, 0, 0)   # PR_SET_PTRACER
import pytest
pytest.main(["--collect-only", "-q", sys.argv[3]])            # ① collect-only，必须 GPU 静默
sys.stdin.readline()                                          # ② 阻塞等授权
rc = int(pytest.main(sys.argv[2:]))                           # ③ 真正运行
os.write(int(sys.argv[1]), str(rc).encode())                  # ④ 退出码走管道（早于 teardown）
sys.exit(rc)
```

四个步骤对应四个设计点：

1. **collect-only 先跑**（行 42）：这一步只收集用例、不执行，所以不碰 GPU。它的目的是让「接下来的子进程」在**当前文件还占着 GPU** 的时候就把 Python/torch/pytest 的导入做完——这就是 nightly.yml 注释里说的「hide startup cost」。
2. **阻塞等授权**（行 43）：`sys.stdin.readline()` 卡住，直到父进程往 stdin 写一个换行。这是「GPU 串行授权」的核心，4.2 节详讲。
3. **真正运行**（行 44）：拿到授权后才 `pytest.main(sys.argv[2:])`，此时 CUDA 才开始初始化。
4. **退出码走管道**（行 45）：`os.write(write_fd, str(rc))` 把退出码写进状态管道，**然后**才 `sys.exit(rc)` 触发解释器 teardown。这个顺序很关键——4.3 节会讲它如何防止「teardown 崩溃被误读成成功」。

`prctl` 那一行（行 38）的魔数 `0x59616D61` 是 Linux 内核常量 `PR_SET_PTRACER`（拼写自 "Yama"）。它把父进程登记为允许 ptrace 本子进程的对象，这样在默认的 `yama ptrace_scope=1` 安全策略下，`py-spy` 仍能 attach 进来抓堆栈（见 4.3 节超时路径）。

#### 4.1.4 代码实践（源码阅读型）

**目标**：确认「文件发现不导入」与「退出码 5 的语义」。

**操作步骤**：

1. 打开 [_discover_bench_files](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/scripts/ci/run_benchmarks.py#L170-L187)，确认它只调 `rglob`、不调 `import`。
2. 打开 [main 里的成功/崩溃分支](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/scripts/ci/run_benchmarks.py#L330-L338)，找到这一句：

```python
# scripts/ci/run_benchmarks.py:332-334
# 0 = all passed, 5 = nothing collected (e.g. all skipped).
if rc not in (0, 5):
    failed.append(rel)
```

**需要观察的现象 / 预期结果**：当某文件被全部跳过、pytest 返回 5 时，运行器把它当成「正常」（不进 `failed`），但仍会把它的真实 junit 片段合并进报告。请回答：为什么「全部跳过」不算失败？——因为「跳过」是 pytest 的正常退出语义，不是原生崩溃。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `_discover_bench_files` 改成「先 `importlib.import_module` 再收集」，会破坏本讲的哪条不变量？

> **答案**：会破坏「文件发现绝不导入」。一个在模块顶层 `os.abort()` 或死循环的基准文件会在父进程尚未建立任何隔离时就把父进程炸掉/卡住，导致整条 CI 报废——正是本讲要消除的爆炸半径。

**练习 2**：为什么子进程前导码要先跑 `--collect-only` 再阻塞，而不是「先阻塞、拿到授权后再 collect-only」？

> **答案**：collect-only 阶段要做大量导入（torch / tileops / pytest）。让它在「等授权的阻塞期」里并发完成，是在**当前文件还占着 GPU** 时把导入开销藏起来；若放到授权之后，导入开销就会挤占被授权文件的 GPU 独占窗口，拖慢整体且无益。

---

### 4.2 GPU 串行授权：stdin grant 与延迟 CUDA 初始化

#### 4.2.1 概念说明

夜行基准跑的是 **Speed-of-Light** 效率（u6-l1、u6-l7），任何并发抢 GPU 的进程都会污染计时。所以必须保证：**任意时刻只有一个子进程在用 GPU**。

运行器用的不是锁，而是一个极简的「授权」协议：

- 子进程一启动就先做 GPU 静默的 collect-only，然后在 `sys.stdin.readline()` 上**阻塞**，自觉「等通知」。
- 父进程按顺序处理文件，**轮到某个文件时**才往它的 stdin 写一个换行符（`release`），解锁它。
- 因为父进程在主循环里**一次只 `release` 一个**，所以 GPU 天然独占。

这需要配合两个外部条件（都在 `nightly.yml` 里，不在运行器里）：

1. **GPU 时钟外部锁定**（SOL 协议第 1 步）：在跑基准前，用 `nvidia-smi` 校验 GPU 图形时钟锁在 1500 MHz，防止降频污染计时。
2. **每子进程内捕获 GPU 元数据**：`benchmark_base.py` 里用 `nvidia-smi` 查 driver 版本、时钟等，写进报告（[benchmarks/benchmark_base.py:373-387](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/benchmarks/benchmark_base.py#L373-L387)）。

> 说明：本讲的「NVML 校验」指的是这层基于 `nvidia-smi`（NVML 的命令行前端）的环境就绪与元数据捕获；**GPU 独占本身**不是靠 nvidia-smi 强制的，而是靠 stdin grant——父进程一次只放行一个子进程。两者职责分离：nightly.yml 负责「GPU 处于已知状态」，run_benchmarks.py 负责「同一时刻只有一个子进程用它」。

#### 4.2.2 核心流程

```
父进程                                   子进程 (_CHILD)
──────                                   ──────────────
spawn child ──────────────────────────►  prctl(PR_SET_PTRACER, ppid)
                                         import pytest
                                         collect-only（GPU 静默）
                                         readline()  ◄── 阻塞
...（prewarm 窗口里还预启了 N 个）
主循环：child = pending.popleft()
         child.release()  ──write \n──►  readline() 返回
                                         pytest.main(...)（CUDA 现在才初始化）
                                         os.write(status_fd, rc)
         wait_result(timeout)  ◄──────── （然后 sys.exit(rc)，开始 teardown）
```

两个时序要点：

- **CUDA 初始化延后到授权之后**：collect-only 不碰 GPU，`pytest.main`（真跑）在 `readline()` 返回后才执行，所以 CUDA 初始化一定发生在「已被授权独占」之后。
- **预热窗口 prewarm**：父进程不是「跑完一个再 spawn 下一个」，而是维持一个最多 `prewarm`（默认 4）个已 spawn、正在 collect-only/阻塞的子进程池（[top_up](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/scripts/ci/run_benchmarks.py#L274-L280)）。这些子进程的导入开销与当前文件的 GPU 运行**时间重叠**。

#### 4.2.3 源码精读

**授权动作 `release`**——父进程往子进程 stdin 写换行符：

```python
# scripts/ci/run_benchmarks.py:69-77
def release(self) -> None:
    """Unblock the child waiting on stdin; a child already dead is fine."""
    stdin = self.proc.stdin
    assert stdin is not None
    with contextlib.suppress(BrokenPipeError):
        stdin.write(b"\n")
        stdin.flush()
    with contextlib.suppress(BrokenPipeError):
        stdin.close()
```

注意 `suppress(BrokenPipeError)`：如果子进程在等授权前就已经死了（比如 collect-only 阶段崩溃），写 stdin 会抛 `BrokenPipeError`，这里吞掉即可——子进程的死亡会通过状态管道的 EOF 被另一条路径捕获（见 4.3）。

**主循环里一次只 release 一个**：

> [main 主循环](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/scripts/ci/run_benchmarks.py#L282-L291)：每个文件从 `pending` 队首取出一个子进程，调 `child.release()` 解锁它，然后立刻进入 `wait_result` 的轮询。这正是「串行授权」的落点。

**预热窗口**：

> [top_up](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/scripts/ci/run_benchmarks.py#L274-L280)：只要 `pending` 队列长度 ≤ `prewarm` 且还有文件没 spawn，就继续 spawn。`prewarm` 默认 4，可由 `--prewarm` 覆盖（[命令行参数](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/scripts/ci/run_benchmarks.py#L238-L243)）。

**外部时钟校验（在 nightly.yml，不在运行器）**：

> [nightly.yml:65-76](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/.github/workflows/nightly.yml#L65-L76)：跑基准前用 `nvidia-smi --query-gpu=clocks.current.graphics` 重试 5 次校验时钟是否等于 1500 MHz，不达标直接 `::error::` 退出。这是 SOL 协议「外部锁时钟」的落地。

#### 4.2.4 代码实践（源码阅读 + 可选运行）

**目标**：亲手看到「stdin grant 串行」的效果。

**操作步骤**：

1. 阅读 [Child.__init__](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/scripts/ci/run_benchmarks.py#L52-L67)，确认子进程是 `stdin=subprocess.PIPE` + `pass_fds=(write_fd,)`（只有状态管道和 stdin 被继承）。
2. （可选运行）跑运行器自测里的合并测试：

```bash
pytest benchmarks/tests/test_run_benchmarks.py::test_fragments_and_profile_logs_merge -v
```

**需要观察的现象 / 预期结果**：测试构造了三个文件，其中 `bench_import_abort.py` 在模块顶层 `os.abort()`。预期 `bench_alpha` 和 `bench_beta` 仍出现在合并后的 junit 里（说明隔离生效），`bench_import_abort` 出现为一条 error 条目。如果 `import` 阶段不隔离，前两个文件根本不会出现在报告里。

> 若本地无 GPU/无 pytest 环境，此项可标为「待本地验证」，改为纯阅读：在 [test_fragments_and_profile_logs_merge](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/benchmarks/tests/test_run_benchmarks.py#L112-L142) 里跟踪断言，说明它如何证明「模块级崩溃只损失自己」。

#### 4.2.5 小练习与答案

**练习 1**：父进程为什么**绝对不能** `import torch`？

> **答案**：因为 `import torch` 会在父进程里初始化 CUDA 运行时、占用 GPU 上下文。父进程要把 GPU 完完整整地让给「当前被授权的子进程」独占，自己一旦也建了 CUDA 上下文，就和子进程抢 GPU，污染 SOL 计时。父进程只做子进程调度与 XML 合并，不需要 torch——模块 docstring 第一句就钉死了这条不变量。

**练习 2**：把 `--prewarm` 设成 0 会让基准变快还是变慢？为什么？

> **答案**：变慢。`prewarm=0` 表示不预启任何子进程，每个文件跑完后才 spawn 下一个，子进程的 torch/tileops 导入开销全部串行暴露在 GPU 空闲期，整体墙钟时间变长。预热的代价只是同时多挂几个「在 readline 阻塞、不碰 GPU」的轻量子进程。

---

### 4.3 失败隔离与 junit 合并：超时、信号死亡、teardown 崩溃

#### 4.3.1 概念说明

子进程可能以四种方式结束，运行器必须把每一种都翻译成「junit 报告里的一格」，不能让任何一种变成黑洞：

| 结束方式 | 父进程怎么知道 | 运行器产出 |
| --- | --- | --- |
| 正常跑完 | 状态管道收到非负 rc | 合并子进程自己写的真实 junit 片段 |
| 超时（hang） | `wait_result` 在 deadline 内返回 `None` | py-spy 抓栈 → SIGKILL → **合成** error 条目（带日志尾部） |
| 信号死亡（segfault/OOM） | 状态管道 EOF，`proc.wait()` 返回负 rc | **合成** error 条目，message 含信号名 |
| **teardown 崩溃** | rc 已正常上报，但解释器退出时又崩 | 进入 `lingering` 队列，下一文件运行期间继续观测 |

第四种最隐蔽：子进程已经把 pytest 退出码（比如 0，全绿）写进管道，**然后**在 `sys.exit` 触发的解释器 teardown 阶段（CUDA 上下文析构、atexit 钩子等）崩溃。如果父进程只看管道里的 rc，就会把这个文件当成「成功」——但它其实结尾崩了。运行器用「状态上报早于 teardown」+「teardown 观测窗口」两招对付它。

#### 4.3.2 核心流程

```
wait_result(timeout):
  select(status_fd, timeout)
  ├─ 超时无数据      → 返回 None                      ──► 走超时路径
  ├─ 读到 rc 字节     → 返回 int(rc)（可能 <0？不会，负数走下一行）
  └─ EOF（管道空）    → proc.wait() 返回负信号         ──► 走信号死亡路径

主循环根据 rc 分派：
  rc is None          → _dump_stack → kill → _synthetic_suite(timeout)
  rc >= 0             → 进 lingering 队列观测 teardown → 合并真实 junit（或 rc<0 合成）
                        poll_teardown 在「下一个文件运行期间」每秒查一次
  rc 是负信号(EOF路径) → _synthetic_suite(signal)
```

**teardown 观测的精妙处**：父进程不会傻等某个子进程的 teardown 结束才跑下一个文件（那会浪费 GPU 独占窗口）。它把「已上报 rc 但进程还在 teardown」的子进程塞进 `lingering` 列表，给一个 `--teardown-timeout`（默认 120s）的 deadline，然后在**主循环的短轮询步**里（每 1 秒一次，见 [run_benchmarks.py:295-299](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/scripts/ci/run_benchmarks.py#L295-L299)）顺便检查它们。这样 teardown 异常「在下一个文件跑的同时」被发现并记录，而不是拖到全部跑完。

#### 4.3.3 源码精读

**wait_result——三种结局**：

> [Child.wait_result](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/scripts/ci/run_benchmarks.py#L79-L93)：`select` 带超时；无数据返回 `None`；读到字节返回 `int(data)`；读到 EOF（`data` 为空）说明子进程没来得及上报就死了，转去 `proc.wait()` 收割「负的信号值」。

**超时路径——抓栈再杀**：

```python
# scripts/ci/run_benchmarks.py:302-314 （超时分支，精简）
if rc is None:
    dump_path = dump_dir / f"{Path(bench_file).stem}.txt"
    _dump_stack(child.proc.pid, dump_path)          # py-spy 抓栈
    child.kill()                                     # SIGKILL 整个进程组
    sys.stdout.write(log_path.read_text(...))        # 打印日志
    message = f"timed out after {timeout:.0f}s; killed, stack dump at {dump_path}"
    suites.append(_synthetic_suite(bench_file, message, _log_tail(log_path)))
    failed.append(rel)
```

**_dump_stack——先试 `--native`，失败回退普通 dump**：

> [_dump_stack](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/scripts/ci/run_benchmarks.py#L144-L167)：先调 `py-spy dump --pid <pid> --native`（`--native` 能显示被阻塞的 C/CUDA 栈帧），失败再回退不带 `--native`；都没装 py-spy 就写一行「py-spy is not installed」。抓栈本身有 `PY_SPY_TIMEOUT_S=120` 超时保护，避免抓栈自己挂住。这也解释了 4.1.3 里子进程为何要 `prctl(PR_SET_PTRACER, ppid)`——为了让这里 attach 成功。

**SIGKILL 整个进程组**：

> [Child.kill](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/scripts/ci/run_benchmarks.py#L112-L118)：用 `os.killpg(pid, SIGKILL)` 杀整个进程组（子进程是 `start_new_session=True` 启动的，自己是组长），把可能 spawn 出的 CUDA worker 子进程一并清掉，再 `proc.wait()` 收尸。

**合成 junit——替死掉的孩子占一格**：

> [_synthetic_suite](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/scripts/ci/run_benchmarks.py#L190-L200)：造一个含 1 个 testcase、1 个 error 的 testsuite；classname 取自文件相对路径，error 的 message 写明原因，**error 的文本内容是日志尾部**（`_log_tail`，最后 80 行）。这样在 GitHub 的 JUnit 报告 UI 里，这一格点开就能看到崩溃前的日志，不丢诊断信息。

**teardown 崩溃——不被误读为成功**：

```python
# scripts/ci/run_benchmarks.py:95-110
def poll_teardown(self, deadline) -> str | None:
    rc = self.proc.poll()
    if rc is not None:
        if rc < 0:
            sig = signal.Signals(-rc)
            return f"died in teardown: signal {sig.value} ({sig.name})"
        return ""                  # 正常退出
    if time.monotonic() < deadline:
        return None                # 还在 teardown，继续等
    self.kill()
    return "stuck in teardown; killed at deadline"
```

只有 rc 已通过管道上报的子进程才会进 `lingering`（[main 里 rc>=0 分支](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/scripts/ci/run_benchmarks.py#L316-L322)）。`_reap_lingering` 在主循环每次轮询时被调用（[run_benchmarks.py:297](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/scripts/ci/run_benchmarks.py#L297)），把已结算的 teardown 异常变成合成 junit 条目（`note_anomalies`）。注意：上报 rc 后的崩溃**不覆盖**该文件已合并的真实结果——它额外追加一条 teardown 异常记录，让「文件用例本身通过，但结尾崩了」这件事可见。

**最后合并所有 testsuite**：

> [main 结尾合并](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/scripts/ci/run_benchmarks.py#L346-L359)：真实片段（`_fragment_suites`，来自每个子进程的 `--junit-xml` 文件）与合成片段一起 `extend` 进一个根 `testsuites`，写出 `bench_results.xml`；只要 `failed` 非空，父进程返回 1。

#### 4.3.4 代码实践（运行型——本讲主实践）

**目标**：用运行器自测亲手验证三条失败路径（崩溃、超时、teardown 崩溃）。

**操作步骤**：

1. 跑全部 5 个运行器测试（它们不需要 GPU，构造的是纯 Python 的假基准文件）：

```bash
pytest benchmarks/tests/test_run_benchmarks.py -v
```

2. 重点看这三个测试：
   - [test_native_crash_loses_only_the_crashing_file](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/benchmarks/tests/test_run_benchmarks.py#L66-L83)：`bench_crash.py` 里 `os.abort()`（SIGABRT）。断言 `bench_ok` 仍在合并报告里、`bench_crash` 的 error message 含 `SIGABRT`。
   - [test_hung_file_is_killed_dumped_and_reported](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/benchmarks/tests/test_run_benchmarks.py#L86-L109)：`bench_hang.py` 里 `time.sleep(600)`，`--timeout-per-file 10`。断言 error message 含 `timed out`、且 `dumps/` 下生成了一个堆栈文件；若装了 py-spy，堆栈里能看到 `test_hang`。
   - [test_teardown_crash_is_reported](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/benchmarks/tests/test_run_benchmarks.py#L158-L174)：`atexit.register(os.abort)`——用例通过了，但解释器退出时崩。断言 stdout 含 `died in teardown`、junit 里有一条 error。

3. 再看 [test_teardown_deadline_enforced_during_next_file](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/benchmarks/tests/test_run_benchmarks.py#L177-L202)：`bench_a` 的 teardown `time.sleep(60)`，`--teardown-timeout 2`。断言 `stuck in teardown` 这行出现在 `bench_b_next.py finished` **之前**——证明 teardown deadline 是在「下一个文件运行期间」被强制执行的，而不是等所有文件跑完。

**需要观察的现象 / 预期结果**：5 个测试全绿；合并后的 `bench_results.xml` 里，每个假文件都占了一格，崩溃/超时/teardown 的格子是 error，正常文件是 passed。

> 若本地装不了 py-spy，`test_hung_file...` 仍会过（断言对 py-spy 存在与否做了 `if shutil.which("py-spy")` 判断，见 [test_run_benchmarks.py:108-109](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/benchmarks/tests/test_run_benchmarks.py#L108-L109)），只是堆栈文件里写「py-spy is not installed」。

#### 4.3.5 小练习与答案

**练习 1**：为什么退出码要从状态管道发，而不是直接让子进程 `sys.exit(rc)`、父进程用 `proc.wait()` 取 rc？

> **答案**：因为 `sys.exit(rc)` 会触发解释器 teardown（atexit、CUDA 析构），而 teardown 本身可能崩溃或挂死。若父进程靠 `wait()` 取 rc，就分不清「rc=0 跑完」和「rc=0 跑完后 teardown 又崩了」。把 rc 经管道**在 teardown 之前**上报，父进程就能立刻知道「用例本身的结果」，再把进程扔进 `lingering` 单独观测 teardown——两条信息解耦。

**练习 2**：合成 junit 条目的 error 文本为什么是「日志最后 80 行」而不是全文？

> **答案**：崩溃前的诊断信息通常在日志尾部（traceback、最后几条 print），全文往往很长且大多是无关的 JIT 编译日志。`LOG_TAIL_LINES = 80`（[run_benchmarks.py:26](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/scripts/ci/run_benchmarks.py#L26)）是「带够诊断、又不撑爆报告」的折中；完整日志仍作为 artifact 上传（见 [nightly.yml:104-114](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/.github/workflows/nightly.yml#L104-L114) 的 `bench_stack_dumps/` 与 `tileops_benchmarks.log`）。

**练习 3**：`profile_run.log` 是每个子进程各写一份的（u6-l3 的 `pytest_sessionfinish` 钩子），父进程怎么把它们合成一份？

> **答案**：每个子进程 session 结束时，conftest 的 `pytest_sessionfinish` 调 `BenchmarkReport.dump("profile_run.log")` 写一份（[conftest.py:27-28](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/benchmarks/conftest.py#L27-L28)）。父进程在每个文件跑完后调 [_absorb_profile_log](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/scripts/ci/run_benchmarks.py#L212-L216)：读取这份 `profile_run.log`、追加进 `profile_parts`、删掉原文件；全部跑完后再把 `profile_parts` 拼接写回 `profile_run.log`（[main 末尾](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/scripts/ci/run_benchmarks.py#L350-L351)）。`test_fragments_and_profile_logs_merge` 正是验证这条拼接链。

---

## 5. 综合实践

把本讲三条主线（隔离、授权、失败合并）串起来，完成下面这个**端到端时序图 + 设计问答**任务。

### 任务

**第一步：画时序图。** 用你习惯的工具（纸笔即可）画出一次「3 个文件、第 2 个文件 hang」的完整时序，对象包括：父进程、文件1子进程、文件2子进程（hang）、文件3子进程、GPU。时间轴上至少标出这些事件：

1. 文件1子进程被 spawn，做 collect-only，在 `readline` 阻塞。
2. 文件2、文件3子进程被 prewarm spawn（也各自 collect-only 后阻塞）。
3. 父进程 `release` 文件1 → 它独占 GPU 跑完 → 经状态管道回 rc=0。
4. 文件1 进 `lingering` 观测 teardown；父进程 `release` 文件2。
5. 文件2 hang → `wait_result` 在 deadline 返回 `None` → `_dump_stack` → `kill` → 合成 timeout error。
6. 父进程 `release` 文件3 → 跑完 → 合并真实 junit。
7. 最终合并写出 `bench_results.xml`（含文件1真实片段、文件2合成 error、文件3真实片段）。

**第二步：回答三个设计问题**（对应规格里的实践任务）：

1. **父进程为什么不能导入 torch？** 结合「GPU 独占」与「CUDA 上下文污染 SOL 计时」回答，并指出 docstring 哪一句钉死了这条不变量。
2. **子进程为什么用 `prctl(PR_SET_PTRACER, ppid)`？** 解释 `yama ptrace_scope=1` 默认策略下，没有这一行会发生什么，以及它服务于本讲的哪条失败路径。
3. **超时时合成的 junit 条目如何携带日志尾部？** 追踪 `_synthetic_suite` → `error.text = log_tail` → `_log_tail` 这条链，说明 80 行这个数字来自哪个常量。

### 验证

- 时序图能解释「为何 hang 只损失文件2」即合格。
- 三个问题的参考答案分别见 4.2.5（练习1）、4.1.3（prctl 段）、4.3.5（练习2）。
- 进阶：跑 `pytest benchmarks/tests/test_run_benchmarks.py::test_teardown_deadline_enforced_during_next_file -v -s`，对照你的时序图确认 `stuck in teardown` 出现在 `bench_b_next.py finished` 之前。

---

## 6. 本讲小结

- **每文件一进程**把一次原生失败（hang / segfault / OOM）的爆炸半径从「全部文件」压到「一个文件」；文件发现绝不 `import` 任何基准模块，否则隔离在建立前就被击穿。
- **GPU 串行授权**靠 stdin grant：子进程启动后先做 GPU 静默的 collect-only，再在 `readline` 阻塞等父进程放行；父进程一次只 `release` 一个，保证独占。CUDA 初始化被刻意延后到授权之后。
- **prewarm 预热窗口**让接下来的子进程在当前文件占 GPU 时并发完成导入，藏起启动开销；父进程自始至终不导入 torch。
- **失败合并**保证任何结局都不丢报告：正常 → 合并真实 junit 片段；超时 → py-spy 抓栈 + SIGKILL + 合成 error（带 80 行日志尾）；信号死亡 → 合成 error（带信号名）。
- **teardown 崩溃**是最隐蔽的一种：靠「rc 经管道早于 teardown 上报」+「lingering 队列在下一文件运行期间用短轮询观测」两招，确保它不被误读为成功。
- **prctl(PR_SET_PTRACER)** 让 py-spy 能在 yama 安全策略下 attach，服务于超时抓栈；`nvidia-smi`（NVML 前端）则在工作流层负责「GPU 时钟锁定校验」与子进程内的元数据捕获，与运行器的独占职责分离。

---

## 7. 下一步学习建议

- **横向对照**：阅读 [scripts/ci/verify_nightly_runner.sh](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/scripts/ci/verify_nightly_runner.sh) 与 [nightly.yml 的 benchmark job](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/.github/workflows/nightly.yml#L36-L119)，理解「运行器之外的 GPU 环境就绪层」如何与运行器配合（时钟校验、cache 目录、`PYTORCH_ALLOC_CONF=expandable_segments:True`）。
- **顺数据流而下**：合并出的 `bench_results.xml` 会被 Phase 2 的 `scripts/nightly_report.py` 消费（[nightly.yml:248-254](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/.github/workflows/nightly.yml#L248-L254)），并发布到 `nightly-bench` orphan 分支供文档站渲染。建议接着读 `nightly_report.py`，看它如何把 junit 的 `user_properties`（u6-l3 讲过的 tileops/baseline tag）转成性能对比表。
- **回看测量根基**：若对「为什么必须独占 GPU、为什么必须锁时钟」还停留在直觉，建议重读 [u6-l1](u6-l1-bench-kernel-sol-protocol.md) 的 SOL-ExecBench 协议六步与本讲的「外部锁时钟」步骤对照——两者是同一套测量哲学的内外两层。
