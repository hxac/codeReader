# 测试架构：分层与运行方式

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 fast-ulysses 四层测试——host-only 布局测试、torchrun 包装的多卡 worker、wheel 矩阵一致性测试、CI——各自需要的运行前提（有没有 GPU、有没有 torch、有没有构建好的扩展）与覆盖范围。
2. 逐行解释 `test/test_distributed.py` 中 `_run` 的超时清理三段式：为什么普通 `subprocess.run(timeout=...)` 只会杀死 torchrun 启动器、孤儿化 rank 进程；为什么被孤儿化的自旋屏障 kernel 会一直占着 GPU；为什么要先用 SIGTERM 再升级到 `killpg`。
3. 描述 CI 的证明边界：无 GPU runner 的 CI 能证明「编译、链接、加载、布局正确」，不能证明任何多卡行为——以及项目用 `tools/preflight_gpu.sh` 这道手工门来补哪一块。
4. 理解「worker 保持直接可运行」这条调试路径的设计价值。

本讲是 u9 单元（测试、基准与设计反思）的第一讲，偏工程架构而不是算法：我们不深挖某个 worker 断言了什么（那是 u9-l2、u9-l3 的内容），而是看这些测试**如何被组织、启动、清理和编排**。

## 2. 前置知识

本讲涉及几个系统工程的基础概念，先用通俗语言过一遍。

**pytest 的 marker 与 skip 机制。**
pytest 允许用 `@pytest.mark.xxx` 给测试打标签（marker），再用 `pytest -m "not xxx"` 选择性运行。skip 则让某个测试在当前环境下「有理由地不算失败」：`pytest.importorskip("pkg")` 在包不可导入时跳过整个文件；`pytest.skip(reason)` 在运行中跳过单个测试。关键理解：**skip 是「我知道我没测」的诚实声明，不是通过**——本讲会看到一个 CI 注释，专门防范「跳过被误读为通过」。

**进程、进程组与会话。**
Linux 上每个进程属于一个进程组（process group），进程组属于一个会话（session）。`setsid()` 系统调用让调用者脱离当前会话，自成一个新会话并成为新进程组的组长——此后它的进程组 ID（pgid）等于自己的 PID。信号可以发给单个进程（`kill`），也可以发给整个进程组（`os.killpg(pgid, sig)`）。子进程默认继承父进程的进程组，所以 Ctrl-C（发给前台进程组）能同时打到一家子；而调用了 `setsid()` 的进程不在你的组里，组级信号打不到它。**孤儿进程**是指父进程先退出、被 init 收养的进程——它们不会被父进程的死亡带走，会继续运行。

**SIGTERM 与 SIGKILL。**
SIGKILL（9 号）不可捕获、不可忽略，进程立即死；SIGTERM（15 号）可以被进程捕获并做善后。这个区别是本讲超时清理的核心：torchrun 的 elastic agent 会在收到 SIGTERM 时**主动终止并回收它启动的所有 rank worker**——这是唯一能优雅触达那些「住在自己会话里」的 worker 的方式。

**torchrun。**
`torchrun`（即 `python -m torch.distributed.run`）是一个启动器：它按 `--nproc_per_node=N` 拉起 N 个训练/测试脚本副本（每个副本一个进程、一个 rank），自己充当 elastic agent，负责环境变量注入（`RANK`、`WORLD_SIZE`、`LOCAL_RANK` 等）、失败重启与善后。测试里被测对象不是 torchrun，而是它拉起的那些 worker。

**自旋内核（回顾 u5-l3）。**
fast-ulysses 的屏障不是主机侧等待，而是一个 GPU kernel：每个线程在 `do { load } while (条件不满足)` 里空转（自旋），直到 peer 发布的 flag 到达。它是一个**正在运行的 kernel**——占着 SM 的执行槽位和整个进程的 CUDA 上下文，只要进程不退出就没有人能从外部「解锁」它。这一点在 4.3 节会成为主角。

**CI（持续集成）。**
`.github/workflows/` 下的 YAML 描述 GitHub Actions 的工作流：`on:` 定义触发条件（push、PR、定时 `schedule`、手动 `workflow_dispatch`），每个 `job` 在一台全新虚拟机/容器里跑一组 `step`，`strategy.matrix` 可以让一个 job 按参数组合展开成多份。

**前讲承接。**
u1-l4 建立的调用链与分层（`a2a_plan.cc` 无 CUDA、无 torch，因此布局可以脱离 GPU 测试）；u3-l3 精读了 `test_plan.py` 的重放、参照与负控制机制——本讲不再重复那些细节，只讲这一层在**整个测试架构中的角色**。

## 3. 本讲源码地图

| 文件 | 角色 |
|---|---|
| `test/test_plan.py` | 第一层：host-only 布局测试，无 GPU、无进程组，只用 numpy 重放 plan |
| `test/test_distributed.py` | 第二层：torchrun 包装器，把 `test/distributed/` 下的 8 个 worker 变成参数化 pytest 用例，并负责超时清理 |
| `test/distributed/*.py` | 8 个多卡 worker（correctness、validation、ce_ordering、cudagraph、window_race、overlapping_barriers、subgroup、nvlink），u9-l2、u9-l3 逐一精读 |
| `test/test_wheel_matrix.py` | 第三层：发布面一致性——`wheel_matrix.json` 与 `release.yml`、`build_wheels.sh` 三方互相对账 |
| `.github/workflows/ci.yml` | 第四层：CI 编排——lint、构建+加载+布局测试、每周 8 行矩阵冒烟 |
| `pyproject.toml` | 注册 `multigpu` marker，使 `-m` 选择成为合法操作 |
| `src/barrier.cu` | 自旋屏障 kernel，4.3 节解释「孤儿占卡」的物理原因 |
| `tools/preflight_gpu.sh` | 多卡行为的手工门：打 tag 前在真多卡机上跑完整 distributed 套件 |
| `docs/develop.md` | 测试命令速查、worker 断言清单、CI 证明边界的官方陈述 |

## 4. 核心概念与源码讲解

先给全貌。fast-ulysses 的测试按「运行前提」分成四层，代价从零到高：

| 层 | 文件 | 运行前提 | 覆盖范围 |
|---|---|---|---|
| 1. host-only 布局 | `test/test_plan.py` | 仅需构建好的 `_C` 扩展（无 GPU、无进程组、无 torchrun） | 寻址/布局正确性（CopyOp 重放对照参照） |
| 2. 多卡 worker | `test/test_distributed.py` + `test/distributed/` | ≥ 2 块 GPU + torchrun | 真实传输、屏障、窗口、autograd、对抗性竞态 |
| 3. 发布面一致性 | `test/test_wheel_matrix.py` | 源码检出（stdlib + pytest，无 torch） | wheel 矩阵三处定义不漂移 |
| 4. CI 编排 | `.github/workflows/ci.yml` | GitHub runner（无 GPU） | 编译、链接、RUNPATH、目标 torch 下加载、第 1/3 层通过 |

这个分层的哲学与 u1-l4 讲过的代码分层完全同构：**能把验证下放到多便宜的环境，就下放到多便宜的环境**。布局错误在一台没有 GPU 的笔记本上就能抓住（第 1 层）；需要真实 NVLink 和对称内存的错误才上多卡（第 2 层）；而第 2 层 CI 永远跑不了，只能靠手工 preflight 补——这就是 4.4 节的「证明边界」。

### 4.1 host-only 布局测试：测试金字塔的地基

#### 4.1.1 概念说明

第 1 层就是 u3-l3 精读过的 `test/test_plan.py`。本讲换一个视角：不看它**怎么测**（numpy 重放、参照、负控制），看它**在架构里扮演什么角色**。

它的角色由三个属性决定：

1. **零外部前提。** 不需要 GPU、不需要初始化进程组、不需要 torchrun，只需要一个能 `import fast_ulysses`（即扩展 `_C` 已构建）的环境。这让它是唯一能进 CI 的正确性检查。
2. **解耦声明。** 模块 docstring 把「布局正确」与「传输正确」的边界写死：布局测试通过后，GPU 上再出的错就**不再可能是布局错**——排查范围直接砍半。
3. **诚实的跳过。** 扩展没构建时它 `importorskip` 自己，skip 理由写明 `fast_ulysses._C extension not built`。这个机制是双刃剑，见 4.1.3 最后一段。

#### 4.1.2 核心流程

```text
pytest -m "not multigpu"
  └─ 收集 test/test_plan.py（无 multigpu 标记）
       ├─ importorskip(torch)            # torch 缺 → 整文件 skip
       ├─ importorskip(fast_ulysses)     # 扩展未构建 → 整文件 skip
       └─ 若 import 成功：
            a2a_plan_debug(...) → CopyOp 表 → numpy 重放 → 对照参照 → 断言
  └─ 收集 test/test_wheel_matrix.py（同样无 multigpu 标记）
       └─ 纯 stdlib 对账，见 4.4 节
```

注意：`pytest`（不带 `-m`）会**同时**收集 multigpu 标记的 `test_distributed.py`——在有卡的机器上这是想要的（`pytest # everything runnable here`），在无卡机器上那些用例会以「needs >= 2 GPUs」skip。`-m "not multigpu"` 是显式排除，语义更清晰。

#### 4.1.3 源码精读

先看 docstring 的解耦声明：

> "If this passes, the addressing is right and any later failure is a transport or synchronisation bug, not a layout bug."

[test/test_plan.py:1-12](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_plan.py#L1-L12) 这段模块 docstring 定义了第 1 层的证明责任边界：通过 = 寻址正确；此后任何 GPU 侧失败都归传输/同步。后半段还预告了窗口语义——每个模拟 rank 的窗口装下整个输出、本 rank 份额同样走窗口，所以零拷贝路径与拷出路径都**不引入重放需要建模的额外寻址**（这正是 u3-l2 讲过的设计决定在测试侧的回响）。

[test/test_plan.py:19-21](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_plan.py#L19-L21) 两个 `importorskip` 是第 1 层的全部「环境门槛」：`torch` 可导入、`fast_ulysses` 包可导入。第二个的 `reason` 参数会出现在 pytest 的 skip 报告里（配合 pyproject 里 `addopts = "-ra"` 会汇总显示所有 skip 原因），所以扩展没构建时你看到的是一句解释，而不是一堆无声的绿点。

[pyproject.toml:36-41](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/pyproject.toml#L36-L41) `multigpu` marker 在这里注册。pytest 对未注册的 marker 只给警告，注册之后 `-m multigpu` / `-m "not multigpu"` 才是受配置约束的正式选择；`testpaths = ["test"]` 让裸 `pytest` 只收集测试目录。

[docs/develop.md:42-52](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/develop.md#L42-L52) 开发文档给出三条命令并写明分层：`pytest`（本机全部能跑的）、`pytest -m "not multigpu"`（host-only，无 GPU）、`pytest -m multigpu`（torchrun 包装的 worker），并直接点破「`test_plan.py` 只需构建好的扩展——这正是它是 CI 唯一能跑的正确性检查的原因」。

最后是整个第 1 层最微妙的一处——`importorskip` 的反面：

[.github/workflows/ci.yml:78-93](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/.github/workflows/ci.yml#L78-L93) CI 在装好 wheel 后**切到 `/tmp`、用绝对路径**跑 `test_plan.py`。注释给出了理由：检出的工作区里 `python/` 下带着同名包但没有构建好的 `_C`，而 `test_plan.py` 在导入失败时会 skip 自己——**在检出目录里跑就会「以跳过一切的方式通过」**。这是一个值得记住的测试工程教训：带 skip 门的设计必须同时防住「skip 被当作 pass」的执行路径；这里的解法是改变工作目录，强制 import 到的是真正被测的已安装 wheel。

#### 4.1.4 代码实践

**实践目标：** 在你的机器上运行第 1 层测试并记录其行为，亲眼区分「通过」「跳过」两种绿色。

**操作步骤：**

1. 在仓库根目录执行 `pytest -m "not multigpu" -v`（加 `-v` 是为了逐条看到 PASSED/SKIPPED）。
2. 记录三类数字：passed 数、skipped 数、skip 原因（`-ra` 已默认开启，会汇总在输出尾部）。
3. 再执行 `pytest --collect-only -q -m "not multigpu" | tail -5`，数一下参数化展开后的用例总数。
4. 对照 `test_plan.py` 的参数化装饰器（如 `@pytest.mark.parametrize("world_size", [1, 2, 4, 8])` × `b` × `mode`）口算用例数，与 pytest 报告的数量核对。
5. （可选，复现 CI 注释警告的场景）如果有一个**未构建扩展**的检出目录，在其中跑 `pytest test/test_plan.py -ra`，观察它以什么理由全部 skip。

**需要观察的现象 / 预期结果：**

- 扩展已构建时：`test_plan.py` 全部用例 PASSED，`test_wheel_matrix.py` 三个用例 PASSED（源码检出状态下 `release.yml` 存在，skipif 不触发）；无任何 skip。
- 扩展未构建时：`test_plan.py` 整文件 skip，理由为 `fast_ulysses._C extension not built`；此时套件**不是通过，是什么都没测**——正是 CI 注释防住的那种运行。
- 具体用例计数取决于参数化组合，「待本地验证」：把你机器上的实际数字填进下表留档。

| 项 | 数值（待本地验证） |
|---|---|
| passed | |
| skipped（及原因） | |
| collect-only 总数 | |

#### 4.1.5 小练习与答案

**练习 1：** 为什么 `test_plan.py` 能不初始化进程组就测「集合通信」的布局？

**答案：** 因为 `build_plan` 是纯主机寻址（u1-l4：无 CUDA、无 torch、无通信库），每个 rank 的 plan 由「持有全组两张 splits 表 + 自己的 rank 号」独立算出（u2-l3、u3-l1）。测试在单进程里为 rank 0..ws-1 各算一份 plan，再用 numpy 字节缓冲模拟「rank r 写进 peer p 的窗口」，集合行为被**重放**出来，无需真的通信。

**练习 2：** CI 为什么坚持 `cd /tmp` 再跑 `test_plan.py`，而不是在检出目录里跑？

**答案：** 检出目录的 `python/fast_ulysses/` 里没有构建好的 `_C`；Python 的 import 会优先命中它，`importorskip` 随即把整个文件 skip——测试「通过」了但零断言执行。切到 `/tmp` 并用绝对路径指测试文件，import 解析到的是 site-packages 里刚装好的 wheel（含 `_C`），被测对象才正确。

**练习 3：** 假如你把一个新断言写进了 `test_plan.py`，但这个断言需要真的在 GPU 上执行才能算出来。会发生什么？

**答案：** 这个断言会让第 1 层不再「host-only」：要么它在 CI（无 GPU）上失败/出错，要么你得给它加 GPU skip 门——两种都破坏了「CI 唯一正确性检查」的属性。正确做法是把断言移到 `test/distributed/` 的某个 worker 里（第 2 层），第 1 层保持纯主机。

### 4.2 torchrun 包装器：把八个 worker 变成参数化用例

#### 4.2.1 概念说明

第 2 层的被测对象是 `test/distributed/` 下的 8 个独立 Python 脚本（worker）。`test/test_distributed.py` 不 import 它们，而是把每个 worker × 每种世界规模作为**子进程**拉起来，检查退出码。这个「包装器」设计有三个动机：

1. **调试路径。** 每个 worker 保持可以直接 `torchrun --nproc_per_node=8 test/distributed/correctness.py` 单跑。多卡 bug 的现场往往是某个 rank 崩溃或挂起——独立脚本让你用最朴素的方式复现、加打印、上调试器，不用先理解 pytest 的进程模型。
2. **失败域隔离。** worker 崩溃是子进程崩溃，torchrun/worker 的完整 stdout/stderr 都能被包装器收集进失败报告（见 4.3 节的文件日志）；若在 pytest 进程内直接 `spawn` rank，一个 rank 的段错误会带走整个测试会话。
3. **世界规模是参数。** 非 2 的幂 world_size 会走 `launch_a2a_ce` 的补漏扫描分支（u5-l1），所以 3 这个奇数值值得一档参数。

#### 4.2.2 核心流程

```text
pytest -m multigpu
  └─ test_worker(worker, nproc) 对 (8 个 worker) × (nprocs 列表) 展开
       ├─ ngpu = torch.cuda.device_count()
       ├─ ngpu < max(nproc, 2)            → pytest.skip（GPU 不够）
       ├─ nproc < 2 且 worker 需要对端      → pytest.skip（ws=1 无握手可观察）
       ├─ _run(worker, nproc)
       │    └─ 子进程: python -m torch.distributed.run --nproc_per_node=nproc <worker>
       │         ├─ 正常退出 0 → 返回 stdout
       │         └─ 超时/非零  → pytest.fail / assert，附完整 stdout+stderr
       └─ stdout 含 "NVLINK BLIND"        → pytest.skip（nvlink.py 的合法盲跑）

_nprocs() 的取值逻辑：
  FAST_ULYSSES_TEST_NPROC 环境变量 → 只跑这一个值
  否则: { min(max(ngpu, 2), 8) }           # 不足 2 按 2 算（随后被 skip 拦下）
        ngpu >= 3 时再加 3                  # 奇数 world_size 走补漏扫描分支
```

#### 4.2.3 源码精读

[test/test_distributed.py:1-10](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_distributed.py#L1-L10) 模块 docstring 开宗明义两条：worker 是独立脚本、直接可跑是调试路径（还给出命令行）；不足 2 卡时整层 skip；`FAST_ULYSSES_TEST_NPROC` 可把世界规模列表覆盖成单值——`=1` 是合法请求，此时需要 peer 的 worker 走 skip 而不是装作通过。

[test/test_distributed.py:28](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_distributed.py#L28) 一行 `pytestmark = pytest.mark.multigpu` 给全文件所有用例打上标记，使 `-m "not multigpu"` 能把它整体排除。这是 marker 的典型文件级用法。

[test/test_distributed.py:30-36](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_distributed.py#L30-L36) `_NEEDS_PEERS` 集合列出四个在 world_size=1 下**拒绝通过而不是盲目跑**的 worker。注释指向了物理原因：`fast_barrier` 在 ws=1 时什么都不启动（见下一条链接），因此 ce_ordering 没有握手可观察、window_race 没有 peer 可竞争——这些 worker 在单 rank 下运行只会「什么都没测却报通过」。注意区分：subgroup.py 自己打印 SKIP 并退出 0（自己处理自己的情况），而这四个靠包装器拦截。

[src/barrier.cu:53-67](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/barrier.cu#L53-L67) `fast_barrier` 的实现：`ws == 1` 时直接 `return`（第 56-57 行）——单 rank 没有人要等，屏障整体不存在。这就是上一条注释的依据：world_size=1 下屏障路径的代码根本不执行，针对屏障行为设计的 worker 在那里必然空转。

[test/test_distributed.py:39-47](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_distributed.py#L39-L47) `_nprocs()`：环境变量覆盖优先；否则由 `torch.cuda.device_count()` 推出 `min(max(ngpu,2), 8)`，ngpu ≥ 3 再加一档 3。两个细节：`max(ngpu, 2)` 保证无卡环境下 nprocs 非空（随后被 `test_worker` 的 skip 拦下，而不是在参数化阶段崩溃）；上界 8 对应 README「单机 NVLink、world_size 1–8」的支持范围。

[test/test_distributed.py:50-63](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_distributed.py#L50-L63) 双层 `parametrize`：worker 名单 × nproc 列表，笛卡尔积展开。worker 顺序即上表顺序，8 个 worker 与 docs/develop.md 的断言清单一一对应（u9-l2、u9-l3 逐个精读）。

[test/test_distributed.py:64-76](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_distributed.py#L64-L76) `test_worker` 主体：两层 skip 检查（卡数、是否需要 peer），然后调 `_run`。第 71-76 行是最后一个分类点：`nvlink.py` 的「盲」（NVML 不可问）是**机器的属性而非测试的缺陷**，所以那个 worker 自己打印 `NVLINK BLIND` 并退出 0，包装器把它转成**可见的 skip**——盲跑不算通过。注释特意强调匹配的是精确字符串，因为 ce_ordering / window_race 的 BLIND 是**可修复的**、必须继续让套件失败。同一句输出、两种处置，区分标准是「盲是机器属性还是测试缺陷」。

#### 4.2.4 代码实践

**实践目标：** 体验「worker 直接可运行」的调试路径，并理解 skip 的两级拦截。

**操作步骤：**

1. 无多卡环境时，运行 `pytest test/test_distributed.py -m multigpu -ra -v`，观察全部用例的 skip 及理由；把理由分成两类（GPU 不够 vs 需要 peer），核对你机器的 `torch.cuda.device_count()`。
2. 阅读任一 worker（如 `test/distributed/correctness.py`）的开头 30 行，找到它解析 `LOCAL_RANK` / 初始化进程组的代码——确认它确实可以脱离 pytest 独立运行。
3. （有 ≥2 卡时）直接跑调试路径：`torchrun --nproc_per_node=<N> test/distributed/correctness.py`，再跑 `FAST_ULYSSES_TEST_NPROC=<N> pytest test/test_distributed.py -m multigpu -k correctness`，对比两条路径的输出形态（前者是 worker 原始输出，后者是包装器的 pass/fail 摘要）。

**需要观察的现象 / 预期结果：**

- 步骤 1：无卡机器上全部 SKIPPED，理由形如 `needs >= 2 GPUs, found 0`；`_NEEDS_PEERS` 的四个 worker 即使设想 nproc=1 也是同样的 skip 链。具体输出「待本地验证」。
- 步骤 2：worker 应当在文件顶部就完成 `LOCAL_RANK` 绑卡与 `init_process_group`——它是完整程序而非库。
- 步骤 3：两条路径的退出语义一致（worker 内断言失败 → 非零退出 → 包装器 `assert proc.returncode == 0` 失败并附报告）。

#### 4.2.5 小练习与答案

**练习 1：** 为什么 `_nprocs()` 在 ngpu=0 时也要返回非空列表（`max(ngpu, 2)` 至少给出 2）？

**答案：** 这个列表喂给 `@pytest.mark.parametrize`，参数化在**收集阶段**就要展开。空列表会让 `test_worker` 根本不出现在测试报告里——静默消失比「显式 skip」更糟。返回 `[2]` 让用例正常收集，再由 `test_worker` 内部的 `pytest.skip` 给出理由，报告里可见「为什么没跑」。

**练习 2：** 如果新增一个 worker `foo.py`，它测的是「窗口在两次调用间的复用」，需要 peer 吗？该不该进 `_NEEDS_PEERS`？

**答案：** 需要。窗口复用涉及「peers 直写我的窗口」——没有 peer 就没有写入者，单 rank 下窗口复用路径只剩本 rank 自份额拷贝，测不到目标行为。应该加进 `_NEEDS_PEERS`，这样 `FAST_ULYSSES_TEST_NPROC=1` 时它显式 skip，而不是盲跑报通过。（判据回看 u2-l1：world_size=1 时屏障被整体跳过，一切「等别人」的行为都不存在。）

**练习 3：** `NVLINK BLIND` 为什么在包装器里变成 skip，而 ce_ordering 的 BLIND 必须让套件失败？

**答案：** nvlink worker 的盲来自机器（没装 pynvml / NVML 不可用 / 只有一块可见卡），换台机器就好了，失败只会制造噪音；ce_ordering / window_race 的盲是测试自身缺少某段武装（armed）代码，盲跑意味着**测试失效**——此刻它恰好没在测任何东西却显示绿色，这是最危险的状态，必须红。

### 4.3 超时与进程清理：SIGTERM → killpg 的三段式

#### 4.3.1 概念说明

这是本讲的核心模块。多卡测试的典型失败不是断言失败，而是**挂起**：某个 rank 卡在自旋屏障里等一个永远不会来的 flag（比如另一个 rank 已经崩溃）。包装器必须能在这种情况下既杀死整棵进程树、又拿到诊断信息、还不污染后续测试。`subprocess.run(..., timeout=600)` 看似够用，实则有三处会在挂起场景下失效：

1. **只杀启动器，孤儿化 worker。** `subprocess.run` 超时后对**直接子进程**（torchrun）发 SIGKILL。但 torchrun 拉起的每个 rank worker 住在自己的会话里（docstring 注释原话 "they live in their own sessions"）——它们不在 torchrun 的进程组里，组级信号打不到；父进程死了它们被 init 收养，继续运行。
2. **孤儿化的自旋 kernel 一直占着 GPU。** 挂起的 rank 不是睡在主机侧，而是它的 GPU 上有一个**正在运行的 kernel** 在 `do { load } while (v < epoch)` 里空转（u5-l3）。kernel 占着 SM 执行槽位；进程占着 CUDA 上下文、设备内存和对端对称内存映射。进程不退出，这些资源永远不释放——后续任何要这块卡的测试都会被拖累（OOM、超时、假失败）。
3. **继承的 stdout 管道会卡死读端。** 若用 `subprocess.PIPE`，worker 继承了管道写端。父进程被杀后，孤儿 worker 仍持有写端且可能已把管道缓冲区（通常 64KB）写满——读端 `read()` 要等到 EOF，而 EOF 需要所有写端关闭，孤儿不关，读端永久阻塞：测试框架自己也被挂起。

`_run` 的解法对这三条各有一招：**日志落文件**（解 3）、**先 SIGTERM torchrun**（解 1，靠 elastic agent 善后 worker）、**`start_new_session` + `killpg` 兜底**（给 SIGTERM 失效时一个明确的组杀目标）。

#### 4.3.2 核心流程

三段式时间线（总预算上界 \( T_{\text{upper}} = 600\,\text{s} + 60\,\text{s} \)，之后必有一死）：

```text
Popen(start_new_session=True, stdout/err → TemporaryFile)
   │
   ├─ 正常: proc.wait(timeout=600) 返回 → 读文件 → assert returncode == 0
   │
   └─ 超时(600s): timed_out = True
        ├─ 第一段: proc.terminate()            # SIGTERM → torchrun
        │          elastic agent 捕获信号，逐一 SIGTERM 并 reap 全部 worker
        │          proc.wait(timeout=60)
        │            ├─ 退出 → 收尸完毕，worker 已被带走
        │            └─ 仍超时（agent 自己也楔死）→ 升级
        ├─ 第二段: os.killpg(proc.pid, SIGKILL) # 组杀 torchrun 所在进程组
        │          proc.wait()                  # 收尸
        └─ 第三段: 读文件（文件读取永不阻塞）→ pytest.fail(报告)
```

为什么第一段是关键：rank worker 在自己的会话里，pytest **够不着它们**；但 torchrun 的 elastic agent 是它们的父进程、知道每个 PID——SIGTERM torchrun 等于把「 terminate 我的孩子们」这个请求交给了唯一有能力执行的角色。第二段的 `killpg` 是 agent 自身失灵时的最后手段，`start_new_session=True` 保证 `proc.pid` 一定是一个合法的 pgid（torchrun 是新组的组长），组杀不会误伤 pytest 自己所在的组。

#### 4.3.3 源码精读

[test/test_distributed.py:79-84](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_distributed.py#L79-L84) 注释本身就是一个完整的设计说明，值得逐句读：先陈述问题（`subprocess.run` 超时只 SIGKILL 启动器、worker 在自己的会话里被孤儿化、自旋屏障 kernel 把它们钉在 GPU 上、继承的 stdout 管道会永久阻塞读端），再给出三招（日志落文件、先 SIGTERM torchrun 让 elastic agent 收掉 worker、最后才 killpg）。

[test/test_distributed.py:85-98](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_distributed.py#L85-L98) `Popen` 的三个关键参数：

- 子进程命令是 `sys.executable -m torch.distributed.run --nproc_per_node=nproc <worker>`——不依赖 PATH 里的 `torchrun` 可执行文件，直接用当前解释器跑模块，保证与 pytest 同一个 Python 环境。
- `stdout=out_f, stderr=err_f` 指向两个 `TemporaryFile`——文件没有管道的 EOF 语义，进程树死绝之后随时可读，永不阻塞；且不落在仓库目录里。
- `start_new_session=True`——torchrun 调 `setsid()` 自成会话与进程组，`proc.pid == pgid`，为第二段的 `killpg` 提供合法目标；顺带把 torchrun 从 pytest 的作业控制信号里隔离出去。

[test/test_distributed.py:99-109](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_distributed.py#L99-L109) 三段式的代码本体：`proc.wait(timeout=600)` 是正常运行预算；`TimeoutExpired` 后 `proc.terminate()`（SIGTERM），注释一行点明依据——elastic agent 会 SIGTERM 并 reap 它的 worker；再给 60 秒善后窗口；仍不死才 `os.killpg(proc.pid, signal.SIGKILL)` 组杀并 `proc.wait()` 收尸。两段超时把「优雅清理的机会」和「确定性死亡」分开：600 秒卡住的是被测代码，60 秒卡住的是善后路径，后者几乎从不发生但必须存在。

[src/barrier.cu:44-48](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/barrier.cu#L44-L48) 这就是「孤儿占卡」的物理本体：`do { v = ld_acquire_sys_u64(mine); } while (v < epoch);`——peer 死了 flag 就永远到不了 `epoch`，这个循环永不退出。它运行在 GPU 上（[src/barrier.cu:65](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/barrier.cu#L65) 的 `barrier_kernel<<<1, 32, 0, stream>>>`），占着 block 槽位；承载它的进程不退出，设备内存与上下文就不释放。主机侧没有任何「取消」接口能单独撤掉一个正在自旋的 kernel——唯一解药是杀进程，这正是清理协议存在的原因。

[test/test_distributed.py:110-116](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_distributed.py#L110-L116) 收尾：文件 seek(0) 后读出 stdout/stderr（此刻进程树已死绝，读文件安全）；`timed_out` 为真则 `pytest.fail` 附带完整报告（超时是失败，不是通过）；否则 `assert proc.returncode == 0`，断言消息同样附带报告。两处都把 worker 的原始输出带进 pytest 报告——挂起/崩溃的第一现场就在眼前。

#### 4.3.4 代码实践

**实践目标：** 为 `_run` 的超时分支写一份逐行注释，向未来的自己（或同事）完整论证「为什么不用 `subprocess.run` 的 timeout」。这是本讲规格指定的核心实践。

**操作步骤：**

1. 打开 `test/test_distributed.py`，找到 `_run`（第 79 行起）。
2. 在你的笔记（或一份不进仓库的副本）里，为下面每一行写 1–3 句中文注释。参考答案版如下，写完再对照：

```python
# —— 示例代码：_run 超时分支的参考注释版（不改仓库源码，仅供学习）——
proc = subprocess.Popen(..., start_new_session=True)
# start_new_session=True: torchrun 调 setsid() 自立门户，proc.pid 即 pgid。
# 作用一：killpg(proc.pid) 有了确定无疑的组目标；
# 作用二：torchrun 不再挂在 pytest 的进程组里，pytest 侧的组信号不会误杀它。

proc.wait(timeout=600)          # 正常运行预算：卡住这里说明被测代码挂起
except subprocess.TimeoutExpired:
    proc.terminate()            # SIGTERM 只发给 torchrun 本身。它够不着住在
                                # 自己会话里的 rank worker，但 elastic agent 是
                                # worker 的父进程、握有全部 PID —— SIGTERM 它，
                                # 等于委托它去 terminate+reap 孩子。这是唯一能
                                # 优雅触达 worker 的路径。
    proc.wait(timeout=60)       # 给善后 60 秒：agent 逐个 SIGTERM worker 并收尸。
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
                                # agent 自己也楔死了才走到这：组杀 torchrun 所在
                                # 组，SIGKILL 不可捕获，随后 wait() 收尸。注意它
                                # 杀的是 torchrun 的组 —— 若 worker 已被 agent 带
                                # 走，这里只是补刀 torchrun；worker 在别的会话里，
                                # 本来就只能靠 agent（上一段）触达。
```

3. 回答规格中的两个为什么，写成两段话（每段不超过 5 句）：
   - 为什么 `subprocess.run(..., timeout=600)` 只杀 torchrun、孤儿化 rank？——run 的超时实现是对**直接子进程**发 SIGKILL；worker 由 torchrun 派生且住在自己的会话，不在被杀范围，父死后被 init 收养继续运行。
   - 为什么孤儿化的自旋屏障 kernel 会一直占着 GPU？——挂起 rank 的 GPU 上有一个永不退出的 `do/while` 自旋 kernel（[src/barrier.cu:46-48](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/barrier.cu#L46-L48)），它占 SM 槽位，进程不退出则 CUDA 上下文、设备内存、对称窗口映射全部不释放；而进程没有退出动机（它在等一个死掉的 peer），也没有外部接口能单独取消一个运行中的 kernel。
4. （可选，需 ≥2 卡，且建议在可随时重置的机器上）把某个 worker 故意改成死锁（如在某 rank 上 `while True: pass`），用 `pytest test/test_distributed.py -m multigpu -k <worker>` 触发 600 秒超时，观察：报告里附带的 stdout/stderr、`nvidia-smi` 在清理后是否干净。

**需要观察的现象 / 预期结果：**

- 步骤 1–3 是纯阅读与写作，无运行依赖。
- 步骤 4：预期 pytest.fail 报告带 `--- stdout ---` / `--- stderr ---` 两段；清理结束后 `nvidia-smi` 上不应残留 python 进程。实际行为「待本地验证」（600 秒超时 + 需要多卡）。

#### 4.3.5 小练习与答案

**练习 1：** 把 `start_new_session=True` 删掉，`os.killpg(proc.pid, SIGKILL)` 会发生什么？

**答案：** 没有 setsid，torchrun 留在 pytest 的进程组里，`proc.pid` 不再是一个 pgid——`killpg` 要么抛 `ProcessLookupError`，要么（若碰巧解释成某个组）杀错对象；即便组存在，组里还有 pytest 自己，SIGKILL 会连测试进程一起带走。`start_new_session` 同时保证「组目标存在」和「组里没有自己人」。

**练习 2：** 为什么 stdout/stderr 用 `TemporaryFile` 而不是 `subprocess.PIPE`？如果坚持用 PIPE，最早在哪里卡死？

**答案：** PIPE 的读端要等所有写端关闭才有 EOF，而 rank worker 继承了写端——孤儿 worker 不关管道，超时后 `communicate()/read()` 永久阻塞，测试框架自己挂起。用文件则在进程树死绝后随时可读。坚持用 PIPE 的话，最早的卡点在超时后读取输出的那一步：即使你先杀了 torchrun，孤儿的写端还开着。

**练习 3：** 既然 SIGTERM torchrun 就能让 agent 收掉 worker，为什么还要保留 `killpg` 这一档？

**答案：** SIGTERM 依赖 torchrun **自己的代码**还在运行并能处理信号。如果 agent 本身楔死（比如卡在不可中断的状态、或 bug 导致信号处理器失效），优雅路径就不存在了。`killpg(SIGKILL)` 是不依赖对方配合的确定性兜底——两档合起来覆盖「对方讲理」和「对方不讲理」两种世界。

### 4.4 wheel 矩阵测试与 CI 的证明边界

#### 4.4.1 概念说明

第 3、4 层不再测运行时行为，测的是**工程事实的一致性**。

**第 3 层（`test_wheel_matrix.py`）**回答一个纯发布面的问题：`fast-ulysses wheel-url` 打印的安装命令所依据的 `wheel_matrix.json`，与真正构建 wheel 的 `release.yml` 矩阵、`build_wheels.sh` 脚本，三处定义是否一致？漂移的后果是工具打印出一条 404 的安装命令——安装期没有任何东西能发现「标签指着一个从未构建过的 wheel」（pip 对「标签不存在」和「版本不存在」都答 no matching distribution）。这一层只依赖 pytest 和标准库，连 torch 都不用。

**第 4 层（CI）**是编排：把 lint、构建、wheel 修复与门禁、安装、第 1/3 层测试串起来，外加一个每周定时把 8 个发布行全部构建一遍的冒烟 job。它的一台 runner 没有 GPU，于是有了一条必须写进每个人脑子里的**证明边界**：

| CI 能证明 | CI 不能证明 |
|---|---|
| 每个配置能编译（build-and-test：2 架构；matrix-smoke：8 行全量） | 对称内存窗口分配、CE 传输、屏障、竞态——任何多卡行为 |
| 链接的库正确、RUNPATH 可重定位（check_wheel 门禁） | 挂起类失败的自愈（根本不会发生：第 2 层不在 CI 跑） |
| 扩展在目标 torch 下能 import（`cd /tmp` 装真 wheel） | 性能与重叠（benchmark 属于 `tools/exclusive.sh` 管辖） |
| 布局正确（`test_plan.py`） | wheel 在真机 GPU 上的行为（`preflight_gpu.sh` 的人工责任） |

#### 4.4.2 核心流程

```text
ci.yml 触发: PR / push(master) / 每周一 05:23 定时 / 手动 workflow_dispatch
  ├─ lint              → pre-commit（ruff + 钉版 clang-format）
  ├─ build-and-test    → manylinux 容器:
  │     pip wheel → auditwheel repair → check_wheel 门禁
  │     → 装真 wheel → cd /tmp → pytest test_plan.py + test_wheel_matrix.py
  └─ matrix-smoke      → 仅 定时/手动 触发（PR 不跑，8 个构建太贵）
        8 行 (torch × CUDA) 逐行 docker 构建断言
              （多卡行为的门 = 打 tag 前: tools/preflight_gpu.sh，纯人工）
```

#### 4.4.3 源码精读

[test/test_wheel_matrix.py:1-12](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_wheel_matrix.py#L1-L12) docstring 讲清这层存在的理由（标签指着一个没人构建过的 wheel 时，pip 只会答 no matching distribution，错误要等人注意到为止）与实现约束：只用标准库正则读 release.yml 的 flow-style 行，不为四个断言引入 yaml 依赖。第 27-29 行（[test/test_wheel_matrix.py:27-29](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_wheel_matrix.py#L27-L29)）的 `skipif` 也值得注意：**不是源码检出**（比如在已安装包的环境里）时跳过——这层测的是仓库内文件的一致性，包安装环境里没有 `release.yml` 可对。

[.github/workflows/ci.yml:6-14](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/.github/workflows/ci.yml#L6-L14) 触发器的注释解释了为什么有每周定时：matrix-smoke 拿**当周解析到的 torch** 构建全部 8 行，torch 的某个点版本破坏了某一行时，问题在周一暴露，而不是在推 tag 的半路；`workflow_dispatch` 手动档则是「等不到周一」的出口——推 tag 前正是 8 行最需要被跑过一遍的时刻。

[.github/workflows/ci.yml:42-50](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/.github/workflows/ci.yml#L42-L50) `build-and-test` 在 manylinux 容器里跑，`FAST_ULYSSES_CUDA_ARCH: "90;100"`——注释自己说明这是**两个架构而非四个**：这个 job 回答「还能构建吗、plan 还成立吗」，完整架构清单是发布关切（release.yml / matrix-smoke 的职责）。这是「每个 job 只回答一个问题」的编排纪律。

[.github/workflows/ci.yml:78-93](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/.github/workflows/ci.yml#L78-L93) 上文 4.1.3 已读过 `/tmp` 细节；这里补看注释的另一半：`test_distributed.py` 在任何 GitHub 托管 runner 上都跑不了（无 GPU），由 `tools/preflight_gpu.sh` 在打 tag 前手工把关；`test_wheel_matrix.py` 读的是工作区而非安装包——它是唯一盯住「`fast-ulysses wheel-url` 打印的矩阵」与「release.yml 实际构建的矩阵」不漂移的检查。

[.github/workflows/ci.yml:95-100](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/.github/workflows/ci.yml#L95-L100) `matrix-smoke` 的 `if` 条件把触发限制在 schedule / workflow_dispatch：8 个 builder 镜像的拉取与构建超出了一个 review 周期能吸收的 runner 时间，所以 PR 永远不跑。矩阵内容（8 行 torch×CUDA，见第 106-113 行）与 release.yml 的 wheels 矩阵保持同步——注释要求两个列表人工对齐。

[docs/develop.md:82-84](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/develop.md#L82-L84) 开发文档把边界写成一句总括：CI 没有 GPU runner，`test/distributed/` 下的任何东西都不会在那里运行；CI 证明的是编译、对预期库的链接、可重定位 RUNPATH、在目标 torch 下加载、以及 `test_plan.py` 通过。紧接着（[docs/develop.md:86-94](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/develop.md#L86-L94)）给出发布三件套与纪律：`preflight_gpu.sh` 是打 tag 前的**强制**门，至少跑最新 torch 行和一个 CUDA-12 行，最老的行只带着编译+加载证据发布，且 release notes 必须如实说明。

[tools/preflight_gpu.sh:1-12](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/tools/preflight_gpu.sh#L1-L12) 脚本头注释补齐最后一块：release job 只证明 wheel 良构且能 import，**操作员真正要的一切**（对称窗口、传输、屏障）只在这里被执行，别处无人覆盖——没跑它就推的 tag 是未测试的。它把 wheel 装进干净 venv 而不是测工作树，因为工作树里的开发版 `.so` 的 RPATH 指向本机——那个构建能通过而 wheel 是坏的。

#### 4.4.4 代码实践

**实践目标：** 亲手核对 CI 的矩阵与证明边界，产出一页「CI 证明 / 不证明」清单。

**操作步骤：**

1. 读 [.github/workflows/ci.yml:104-113](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/.github/workflows/ci.yml#L104-L113) 的 matrix-smoke 8 行，抄下每行的 `torch` / `index` / `cuda_major`。
2. 打开 `python/fast_ulysses/wheel_matrix.json`（若在源码检出中），把它的 `rows` 与上表逐行对照——你在做的正是 `test_wheel_matrix.py::test_tags_match_release_matrix` 的手工版；然后运行 `pytest test/test_wheel_matrix.py -v` 看三个断言的名字与结果。
3. 写下你自己的两栏清单：左栏「一条 PR 在 CI 绿了 ⇒ 它证明了什么」，右栏「它没证明什么、那个缺口由谁补」。以本讲 4.4.1 的表格为底稿，按你的理解改写，每条不超过一行。
4. 给自己出一道检验题并书面回答：若某人改了 `release.yml` 的矩阵、忘了改 `wheel_matrix.json`，CI 的哪个 job / 哪条测试会红？（提示：`test_wheel_matrix.py` 在 build-and-test 的 pytest 步骤里；而 matrix-smoke 与 release.yml 的关系靠注释里「keep the two lists in sync」的人工纪律——想一下哪种漂移会被机器抓住、哪种只有每周一或推 tag 时才会暴露。）

**需要观察的现象 / 预期结果：**

- 步骤 2：三处（matrix.json / release.yml / build_wheels.sh）当前一致，测试全绿；若有任何一行对不上，pytest 会精确指出哪一列漂移。实际输出「待本地验证」。
- 步骤 3：右栏必须包含「一切多卡行为 → preflight_gpu.sh（人工）」与「性能/重叠 → exclusive.sh 包裹的 benchmark（人工）」两条，缺了说明还没建立边界意识。

#### 4.4.5 小练习与答案

**练习 1：** `test_wheel_matrix.py` 为什么不用 GPU 也不用 torch，甚至不用 yaml 库？

**答案：** 它对账的对象（wheel_matrix.json、release.yml、build_wheels.sh）都是**文本事实**，与运行时无关；release.yml 的矩阵行是 flow-style YAML 映射，正则能精确读取。把依赖压到最小，这层才能在任何环境（包括无 torch 的发布脚本环境）跑，且不会因 torch 版本问题给出假信号。

**练习 2：** matrix-smoke 为什么不在每个 PR 上跑？

**答案：** 它要在 8 个 manylinux builder 镜像里各做一次完整拉取+构建，runner 时间远超 review 周期能吸收的量。代价是矩阵漂移或 torch 点版本破坏不会在 PR 上即时暴露——所以有每周一的定时档兜底，加上推 tag 前的手动档（`workflow_dispatch`），两者共同把最坏暴露窗口压到一周。

**练习 3：** 一条 PR 在 CI 全绿，合并后用户报告「多卡挂起」。从测试架构看，这个失败落在哪一层的职责缺口里？

**答案：** 落在第 2 层（多卡 worker）与第 4 层之间的边界上：CI 按设计**不跑**第 2 层（无 GPU runner），多卡行为只能由 `preflight_gpu.sh` 在打 tag 前人工覆盖。若这条 PR 只过了 CI 而没在真多卡机上跑过 distributed 套件，缺口就是把「CI 绿」误当作「全部绿」——develop.md 与 preflight 脚本头都反复强调这一点。

## 5. 综合实践

**任务：为一个新的多卡 worker 制定完整的接入方案（纸面工程），并完成本讲的运行记录。**

背景：假设你要新增一个 worker `staging_growth.py`——它断言异步路径的暂存池按 `[1, 2, 3, 4, 4]` 增长（知识点来自 u7-l2：`staging_slots_debug` 探针；该断言目前在别处覆盖，这里只是把它当作一个具体的新 worker 来练习架构接入）。

请产出四样东西：

1. **运行记录表。** 在你的机器上执行 `pytest -m "not multigpu"`，把 passed / skipped / skip 理由记进 4.1.4 的表格；再执行 `pytest --collect-only -q -m multigpu`，记录第 2 层会展开的用例数（无卡时它们全部会 skip，但**收集**仍然发生——这正是 4.2 练习 1 讲的可见性设计）。数字待本地验证。
2. **接入清单（对照源码逐条写）：**
   - 脚本放在 `test/distributed/staging_growth.py`，开头独立完成 `init_process_group` 与 `LOCAL_RANK` 绑卡（参考任一现有 worker），保证「直接 torchrun 可跑」的调试路径（[test/test_distributed.py:1-4](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_distributed.py#L1-L4) 的要求）。
   - 判断是否进 `_NEEDS_PEERS`（[test/test_distributed.py:30-36](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_distributed.py#L30-L36)）：暂存池在 world_size=1 下是否仍被完整练习？——异步路径在 ws=1 时传输只剩本 rank 自份额，但 staging/窗口逻辑仍在；写下你的结论与理由。
   - 把名字加进 `test_worker` 的参数化名单（[test/test_distributed.py:51-62](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_distributed.py#L51-L62)），并估算新增用例数 = worker 数 × nproc 档数的变化。
   - 超时预算是否需要偏离 600 秒（[test/test_distributed.py:101](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_distributed.py#L101)）？给出理由。
   - 它有没有「机器盲」状态（类比 `NVLINK BLIND`，[test/test_distributed.py:71-76](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_distributed.py#L71-L76)）？没有就不引入该机制，并写一句为什么。
3. **清理协议自检。** 不看 4.3 节，从 `_run` 源码默写三段式（SIGTERM → 60s → killpg → fail 带报告），并写清每一段防的是哪一种失败（孤儿 worker / 楔死的 agent / 管道死锁 / 超时被误判为通过）。写完再对照 4.3.2 的流程图查漏。
4. **证明边界一页纸。** 把 4.4.1 的表格扩写成你自己的版本，额外加一行「本 worker（staging_growth）的断言在 CI 上是否运行、由谁兜底」——强迫自己把新测试放进四层地图里再收工。

**预期结果：** 这份产物本质上是一份「新测试的架构评审单」。判断标准：每一条接入决定都能指向本讲引用的具体源码行；运行记录表里的数字与 `--collect-only` 的展开数互相印证；清理协议默写无遗漏三段中的任何一段。

## 6. 本讲小结

- 测试按运行前提分四层：host-only 布局（仅需构建好的 `_C`）、torchrun 包装的多卡 worker（需 ≥2 卡）、wheel 矩阵一致性（仅需源码检出）、CI 编排（无 GPU）——布局错误被下放到最便宜的环境抓住，与代码分层「a2a_plan.cc 无 CUDA 无 torch」同构。
- 第 2 层的包装器把 8 个 worker × 世界规模展开成参数化用例；worker 保持独立可 `torchrun` 直跑（调试路径），skip 分两级（卡数不足 / ws=1 无握手可观察的 `_NEEDS_PEERS`），`NVLINK BLIND` 被转成可见 skip 而 ce_ordering / window_race 的盲必须红——判据是盲属于机器还是测试缺陷。
- 超时清理三段式缺一不可：日志落文件防管道 EOF 死锁；SIGTERM torchrun 委托 elastic agent 收掉住在自己会话里的 rank worker（唯一能优雅触达它们的路径）；`start_new_session=True` + `killpg(SIGKILL)` 给 agent 自身楔死时一个不误伤 pytest 的确定性兜底。
- 「孤儿占卡」的物理本体是 `barrier.cu` 里永不退出的自旋 kernel：它占 SM 槽位，进程不退出则 CUDA 上下文与设备内存永不释放，也没有外部接口能单独取消一个运行中的 kernel——杀进程是唯一解药。
- CI 的证明边界要背下来：编译、链接、可重定位 RUNPATH、目标 torch 下加载、`test_plan.py` 布局正确；**任何**多卡行为都不在其中，由打 tag 前的 `tools/preflight_gpu.sh` 人工门覆盖——CI 绿不等于全部绿。

## 7. 下一步学习建议

- **u9-l2（correctness 与 validation 工作负载）**：进入 `test/distributed/` 的前两个 worker——位精确对照的维度清单、`all_to_all_single + permute` 作为参照的理由、「校验先于第一个屏障」如何被断言。本讲只讲了它们如何被启动，下一讲解它们断言了什么。
- **u9-l3（对抗性测试）**：`ce_ordering` / `window_race` / `overlapping_barriers` / `cudagraph` 四个 worker 的 skew 与武装（armed）设计——你会再次遇到本讲的 `_NEEDS_PEERS` 名单和「测试必须还能失败才算有效」的原则，那时它从架构规则变成具体技术。
- 顺带阅读：[tools/preflight_gpu.sh:1-46](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/tools/preflight_gpu.sh#L1-L46) 全文（本讲只引了头注释）与 [docs/develop.md:65-74](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/develop.md#L65-L74) 的 worker 断言清单，是动手打 tag 前的必读材料；u8-l3 讲过的 wheel 矩阵则是 4.4 节第 3 层测试的对账对象。
