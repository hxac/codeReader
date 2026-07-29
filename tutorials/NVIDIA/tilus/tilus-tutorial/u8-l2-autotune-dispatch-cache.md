# 自动调优调度与硬件感知 dispatch 缓存

## 1. 本讲目标

上一讲（u8-1）我们看清了 Tilus 的「内容寻址磁盘缓存」：一个编译好的 `.so` 由它的 Tilus IR 文本哈希唯一确定，落在 `programs/<hash>/`。但那只解决了「同一份内核二进制不重复编译」的问题。本讲要回答一个更上层的问题：

> 当一个内核带有 `@autotune`，会编译出**很多份**候选内核（不同分块/线程数），运行时 Tilus 怎么知道**对当前输入大小、当前 GPU，该选哪一份最快**？而且这份「选优结果」怎么在第二次运行时直接复用、换台机器又不会误用？

学完本讲，你应当能够：

1. 说清 `__call__` 的参数如何被分成**常量参数 / 调优参数 / 内核参数**三类，以及它们分别进 JIT key 还是 tuning key。
2. 描述「并行转译 → 并行编译 → benchmark 选优」的完整调优链路，以及为什么失败的一份调度不会让整个调优崩溃。
3. 解释 dispatch 表的**环境指纹**机制：tilus 版本、target、GPU 名称、算力、CUDA 版本如何防止跨环境误用一张调优表。
4. 亲手运行一个带 autotune 的内核两次，从日志和缓存文件确认第二次直接命中 dispatch 缓存。

## 2. 前置知识

本讲建立在两篇前置讲义之上，请确认你已经理解：

- **u2-4 自动调优：@autotune 与调度空间**：`@tilus.autotune` 装饰器只负责把调优子空间累积到类属性 `_autotune_space`；`span_space` 用笛卡尔积把子空间展开成一份份扁平的 **schedule**（调度 dict）；`generate_schedules` 把每份 schedule 绑定到 `__init__` 签名。一句话：**schedule = 给「只影响性能、不影响结果」的编译期超参填入一组具体值**。
- **u8-1 缓存机制与缓存目录结构**：缓存键是 `sha256(options_text + 程序文本)[:12]`，落在 `programs/<hash>/`，键里**不含 codegen/emitter 版本**，改发射器要手动删缓存。

此外需要两个基本概念：

- **JIT（即时编译）**：Tilus 在你**第一次**用某组参数调用内核时，才把 Python 写的 `__call__` 翻译、编译成 `.so`。参数变了可能要重新 JIT。
- **dispatch（分派）**：在已经编译好的多份内核里，挑一份当前用的。本讲的核心就是「挑」的规则与「挑完记下来」的缓存。

一句话区分两个「缓存」：u8-1 的 `programs/<hash>/` 缓存的是**单份编译产物**；本讲的 `scripts/<name>/.../dispatch_table.json` 缓存的是**「输入大小桶 → 最优 schedule 编号」的选优结果**。两者叠加，才让第二次运行又快又对。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [python/tilus/lang/instantiated_script.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instantiated_script.py) | 本讲主角。包含参数分类 `CallParameters`、键提取 `extract_keys`、调优实例 `JitInstance`（转译/编译/选优/dispatch 表读写）、环境指纹 `collect_tuning_metadata`。 |
| [python/tilus/utils/multiprocess.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/utils/multiprocess.py) | `parallel_imap`：用 fork 进程池并行执行转译与编译任务，用 `JobQueue` 避免序列化大任务。 |
| [python/tilus/utils/bench_utils.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/utils/bench_utils.py) | `benchmark_func`：测内核延迟的标准方法，含 L2 清缓存与多次取中位数。 |
| [python/tilus/option.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/option.py) | 全局选项：`bench_warmup`、`bench_repeat`、`parallel_workers` 控制调优的精度与并行度。 |
| [python/tilus/lang/script.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/script.py) | `@autotune` 装饰器，把搜索空间写入 `_autotune_space`。 |

## 4. 核心概念与源码讲解

### 4.1 参数分类与调优触发

#### 4.1.1 概念说明

一个 Tilus 内核的 `__call__` 方法可以有很多参数，但它们对编译的影响并不相同。Tilus 在实例化时（`InstantiatedScript.__init__`）用 `CallParameters` 把 `__call__` 的每个参数按**类型标注**分成三类：

| 类别 | 标注类型 | 含义 | 进入哪个键 |
| --- | --- | --- | --- |
| 常量参数 const | `bool / int / float / str` | 编译期常量，**值变就重编译** | **jit_key**（精确值） |
| 调优参数 tuning | 整数 `DataType`（如 `int32`/`int64`） | 运行时尺寸，**不重编译**，但影响「选哪份最快」与编译时的整除性 | **jit_key**（整除性指纹）+ **tuning_key**（尺寸桶） |
| 内核参数 kernel | 指针/非整数 `DataType`（如 `~float16`） | 纯运行时实参，只传给内核 | 都不进键，仅作 launch 实参 |

理解这张表是本讲的关键。核心是「**值的不同侧面**分别走两条路」：

- 一个 `m_size: int32` 参数，它的**精确数值**（如 4096）不会进 jit_key（否则每个新尺寸都要重编译，灾难）。它只影响「在已编译的多份内核里挑哪份」，于是进 tuning_key。
- 但它的**整除性**（能否被 2/4/8… 整除）会影响转译产物——因为内核里可能用 `assume(m_size % block_m == 0)` 之类的约束（见 u2-3、u5-4），整除性被烤进 IR 元数据。所以整除性变化要触发重编译，它进 jit_key。

于是同一个 `int32` 参数被拆成两个侧面：整除性 → jit_key（决定编译哪组二进制），尺寸桶 → tuning_key（决定选哪份二进制）。

#### 4.1.2 核心流程

每次调用内核，`InstantiatedScript.__call__` 走这样一条路：

```text
调用 kernel(m, n, k, a, b, c)
        │
        ▼
extract_keys(args) ──► (jit_key, tuning_key)
        │
        ▼
查内存 dispatch_table[(jit_key, tuning_key)]
   ├── 命中（compiled_func 已有） ──► 直接 launch（最快路径，热路径）
   └── 未命中（慢路径）
        │
        ▼
按 jit_key 取/建 JitInstance（一组编译好的候选内核）
        │
        ▼
JitInstance._pick_best_program(args)
   ├── tuning_key 已在 dispatch 表 ──► 直接返回对应内核
   └── 不在 ──► benchmark 所有候选，选最快，写 dispatch 表
```

热路径（第二次以后同样的尺寸）只做：提取键 → 字典查 → launch。慢路径才触发编译和选优。

#### 4.1.3 源码精读

**参数分类** 在 `CallParameters.extract_params`：

[python/tilus/lang/instantiated_script.py:257-302](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instantiated_script.py#L257-L302) —— 逐个检查 `__call__` 参数：必须带类型标注、必须是位置或关键字参数；Pythonic 标注（`int/float/str/bool`）归入 `const_params`，其余 Hidet 类型归入 `kernel_params`，其中「整数 DataType」再细分进 `tuning_params`。

分类的核心两行：

```python
if annotation in [bool, int, float, str]:
    self.const_params.append(index)        # 值变 → 重编译
else:
    self.kernel_params.append(index)       # 指针/非整数 → 仅作实参
    if isinstance(annotation, DataType) and annotation.is_integer():
        self.tuning_params.append(index)   # 整数 → 还参与调优
```

**键提取** 在 `extract_keys`（这是性能敏感的热路径）：

[python/tilus/lang/instantiated_script.py:327-358](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instantiated_script.py#L327-L358) —— 常量参数直接把值塞进 `jit_key`；调优参数把「整除性指纹」塞进 `jit_key`、把「尺寸桶」塞进 `tuning_key`：

```python
for i in const_params:
    jit_key.append(args[i])                      # 常量：精确值
for i in tuning_params:
    arg = args[i]
    jit_key.append(divisibility_key[arg % 32])   # 整除性 → jit_key
    block = 1 << max((arg.bit_length() - 2), 0)
    tuning_key.append((arg + block - 1) // block * block)  # 尺寸桶 → tuning_key
```

其中 `divisibility_key` 是导入时一次性建好的查表（[instantiated_script.py:310-324](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instantiated_script.py#L310-L324)），按 `arg % 32` 的余数查一个整除性代表值——把「这个尺寸能被哪些因子整除」压缩进 jit_key，使整除性不同的尺寸落到不同 JIT 实例。

**尺寸桶的数学**：`block = 1 << max(arg.bit_length() - 2, 0)` 把桶宽设成约等于 `arg/4`，再把 `arg` 向上取整到该桶宽的整数倍。效果是「同一量级、相差不超过约 1/4 的尺寸」共享一个 tuning_key，从而共享一条 dispatch 记录。例如（以下为示例推算，待本地验证）：

\[ \text{block} = 2^{\,\max(\lfloor\log_2 \text{arg}\rfloor - 1,\;0)},\qquad \text{bucket}(\text{arg}) = \left\lceil \tfrac{\text{arg}}{\text{block}} \right\rceil \cdot \text{block} \]

- `arg=4096`：bit_length=13 → block=2048 → bucket=4096
- `arg=4000`：bit_length=12 → block=1024 → bucket=4096（与 4096 同桶）
- `arg=4097`：bit_length=13 → block=2048 → bucket=6144（新桶）

这样 dispatch 表的条目数被控制成「每个数量级约 4 条」，既尺寸敏感又不会爆炸。

**调用入口** 在 `InstantiatedScript.__call__`：

[python/tilus/lang/instantiated_script.py:824-858](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instantiated_script.py#L824-L858) —— 先 `extract_keys`，再查内存 `dispatch_table`；命中就直接 launch，未命中才进慢路径建/取 `JitInstance` 并调 `_pick_best_program`。注意最后 launch 时只把 `kernel_params` 位置的实参传给内核（第 855 行），因为只有它们是真正的运行时张量/指针。

#### 4.1.4 代码实践

**实践目标**：亲手验证「调优参数的精确值不进 jit_key，只进 tuning_key」。

**操作步骤**（源码阅读型）：

1. 打开 [instantiated_script.py:327-358](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instantiated_script.py#L327-L358)。
2. 假设 `__call__(self, m: int32, n: int32, k: int, a: ~float16, ...)`，分别对 `m=4096` 与 `m=4000` 手算 `jit_key` 与 `tuning_key`（注意 `k: int` 是常量参数，值进 jit_key）。
3. 思考：两次调用的 jit_key 是否相同？tuning_key 是否相同？

**需要观察的现象 / 预期结果**：

- `k` 作为 `int`（常量）值相同 → 对 jit_key 贡献相同。
- `m` 的整除性指纹（`divisibility_key[m % 32]`）若相同 → jit_key 相同 → **复用同一组已编译内核，不重编译**。
- `m=4096` 与 `m=4000` 的 tuning_key 都算到 4096 → **连 dispatch 选择都复用**；而 `m=4097` → 6144 是新桶 → 会触发一次新的 benchmark 选优（但仍不重编译）。

> 待本地验证：可在 `extract_keys` 入口临时加一行 `print("jit", jit_key, "tun", tuning_key)` 观察实际输出（仅用于学习，勿提交）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `__call__` 里某个本该是 `int32` 的尺寸参数误标成 `int`，会发生什么？

**答案**：它会从 tuning_params 变成 const_params，其**精确值**进入 jit_key。结果是每个不同的尺寸都要重新 JIT 编译整套 schedule，调优表也失去意义。这正是类型标注决定编译行为的体现。

**练习 2**：常量参数（如 `k: int`）的值变化，会改 jit_key 还是 tuning_key？为什么这样设计？

**答案**：改 jit_key（精确值进入）。因为常量参数的值在编译期就「烤」进了内核（比如展开次数、数组大小），值变了内核二进制就必须不同，所以必须重编译——这正是它进 jit_key 的原因。

---

### 4.2 并行 benchmark 选优

#### 4.2.1 概念说明

当 tuning_key 第一次出现（既不在内存表、也不在磁盘表）时，Tilus 需要**实际跑一遍**每一份候选内核，测出延迟，挑最快的。这一步叫「选优」。它由 `JitInstance._pick_best_program` 主导，涉及三件事：

1. **并行转译**：把每份 schedule 用 `Transpiler` 翻成 Tilus IR `Program`（[u3-2](u3-l2-transpiler-ast-to-ir.md)）。转译失败的 schedule 只记录 traceback，不中断整体。
2. **并行编译**：把每个 `Program` 经 `build_program` 编成 `.so`（[u8-1](u8-l1-caching-mechanism.md)）。编译失败同样只记录、不中断。
3. **benchmark 选优**：对每个存活的候选内核，用 `benchmark_func` 测延迟，取最小值对应的编号写入 dispatch 表。

「失败不中断」是工程上的关键设计：调优空间里难免有几份 schedule 在某些尺寸下不合法或编译不过，丢掉它们即可，不能让一个坏配置毁掉整个内核。

#### 4.2.2 核心流程

```text
_pick_best_program(args)
        │
   (若无 valid_programs) _build_programs()
        │
        ▼
   extract_keys 取 tuning_key
        │
   查内存 dispatch_table[tuning_key]?
   ├── 有 ──► 返回 compiled_programs[choice]
   └── 无 ──► 加 FileLock
        │       ├─ 重新 load_dispatch_table()（别的进程可能已写）
        │       ├─ 仍无 ──► 逐份 benchmark_func 测延迟
        │       │            （只有 1 份时跳过 benchmark，直接选它）
        │       ├─ choice = argmin(latency)
        │       ├─ dispatch_table[tuning_key] = choice
        │       ├─ dump_dispatch_table()（写盘）
        │       └─ 写 latency 报告 + 软链到最优 program
        ▼
   返回 compiled_programs[choice]
```

并行发生在转译和编译阶段（`parallel_imap`），benchmark 阶段是串行的（要独占 GPU 测量）。

#### 4.2.3 源码精读

**并行执行器 `parallel_imap`**：

[python/tilus/utils/multiprocess.py:42-64](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/utils/multiprocess.py#L42-L64) —— 用 `multiprocessing.get_context("fork")` 建进程池，把每个 job 的**下标**（而非 job 本身）交给池，worker 内部再从全局 `_job_queue` 取真实 job：

```python
ctx = multiprocessing.get_context("fork")
with ctx.Pool(num_workers) as pool:
    yield from pool.imap(_wrapped_func, range(len(jobs)))
```

这个 `_wrapped_func`（[multiprocess.py:28-39](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/utils/multiprocess.py#L28-L39)）的设计是为了**避免序列化（pickle）任务**——fork 出来的子进程天然共享父进程内存里的 `_job_queue`，只需传一个整数下标。注意第 49-50 行：`parallel_imap` **不能递归调用**（全局只有一个 `_job_queue`），所以转译和编译是分别两次独立的并行调用，不会嵌套。worker 数量来自 `parallel_workers` 选项（[option.py:60-65](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/option.py#L60-L65)，默认 `os.cpu_count()`）。

**并行转译 `_transpile_programs`**：

[python/tilus/lang/instantiated_script.py:475-516](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instantiated_script.py#L475-L516) —— 为每份 schedule 准备一个 job（含 script 类、schedule、常量值、整除性），并行调 `_instantiate_schedule`。返回值若是 `Program` 则收入 `transpiled_programs`，若是 `str`（traceback）则收入 `failed_scheduling`（第 502-515 行）——失败的 schedule 不影响其他 schedule。

**并行编译 `_build_programs`**：

[python/tilus/lang/instantiated_script.py:604-640](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instantiated_script.py#L604-L640) —— 对每个转译成功的 Program 并行调 `build_program`（[u8-1](u8-l1-caching-mechanism.md)，内部命中 `programs/<hash>/` 缓存则秒回）。返回 `"success"` 或 traceback，成功的收入 `compiled_programs`，失败的写进 `failed/building/`。

**选优 `_pick_best_program`**：

[python/tilus/lang/instantiated_script.py:700-771](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instantiated_script.py#L700-L771) —— 这是本模块的核心。关键片段：

```python
if tuning_key not in self.dispatch_table:
    with filelock.FileLock(self.cache_dir_lock):        # 多进程安全
        self.load_dispatch_table()                       # 重读磁盘（防别的进程已写）
        if tuning_key in self.dispatch_table:
            return self.compiled_programs[self.dispatch_table[tuning_key]]
        # 真的要 benchmark
        latency = []
        if len(self.compiled_programs) == 1:
            latency.append(0.0)                          # 只有一份，跳过测量
        else:
            for compiled_program in ...:                 # 逐份测
                lat = benchmark_func(lambda: compiled_func(*kernel_args),
                                     warmup=..., repeat=...)
                latency.append(lat)
        best_program_idx = latency.index(min(latency))   # 选最快
        self.dispatch_table[tuning_key] = best_program_idx
        self.dump_dispatch_table()                        # 写盘
```

两个要点：一是 **FileLock + 重读磁盘**，保证多进程并发调优时不会重复 benchmark 也不会互相覆盖；二是 **只有一份候选时直接跳过 benchmark**（第 716-718 行），省掉无意义的测量。

**测量函数 `benchmark_func`**：

[python/tilus/utils/bench_utils.py:70-103](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/utils/bench_utils.py#L70-L103) —— 先写一块两倍 L2 大小的内存清掉 L2 缓存（`_l2_clear_nables`，[bench_utils.py:54-67](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/utils/bench_utils.py#L54-L67)），保证每次测量都是「冷 L2」；用 CUDA event 计时；`warmup` 次预热后再正式测 `repeat` 次，返回**中位数**延迟。预热/重复次数由 `bench_warmup`（默认 5）/`bench_repeat`（默认 50）选项控制（[option.py:80-91](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/option.py#L80-L91)）。

#### 4.2.4 代码实践

**实践目标**：观察「并行编译 + benchmark 选优」的进度条与产物。

**操作步骤**：

1. 删掉旧缓存目录，确保从零开始。
2. 运行 [examples/matmul/matmul_v2.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/matmul/matmul_v2.py)（它带三层 `@autotune`，共 2×3×2=12 份 schedule）。
3. 观察终端的 `tqdm` 进度条顺序。

**需要观察的现象 / 预期结果**：

- 先出现 `[Scheduling] ...` 条（并行转译 12 份 schedule）。
- 再出现 `[Building] ...` 条（并行编译，首次较慢，调用 nvcc）。
- 再出现 `[Tuning] ...` 条（对每个新 tuning_key 逐份 benchmark）。
- 结束后，缓存目录下出现 `schedule.txt`（所有候选 schedule）、`programs/`（指向各编译产物的软链）、`latency/<tuning_key>/report.txt`（每份延迟排名）。

> 待本地验证：实际进度条是否出现取决于终端是否为 TTY；`tqdm` 在非交互环境可能不显示，但文件产物一定会生成。

#### 4.2.5 小练习与答案

**练习 1**：为什么转译和编译要设计成「失败的一份不影响其他份」？

**答案**：调优空间是笛卡尔积，难免有个别 schedule 在特定尺寸下不合法（如 shared memory 超限、布局无解）或编译失败。如果一份失败就抛异常，整个内核就无法使用。把失败者记入 `failed/` 目录、从候选里剔除，能让「能跑的那些」继续参与选优，极大提升健壮性。

**练习 2**：benchmark 阶段为什么是串行而不是像编译那样并行？

**答案**：benchmark 要独占 GPU 才能测准延迟（并行跑会互相争抢 SM、污染 L2、计时失真）。编译是纯 CPU 的 nvcc 工作，可以并行；测量必须在 GPU 上串行排队。

---

### 4.3 dispatch 环境指纹与缓存校验

#### 4.3.1 概念说明

选优结果是**环境相关**的：在 B200 上最快的 schedule，换到 B300 未必最快；同一个内核在不同 CUDA 版本下编译出的 PTX 也不同。如果一张在 A 机器调好的 dispatch 表，通过共享缓存目录被 B 机器悄悄拿来用，就会用一个「在 B 上并非最优」甚至错误的配置。

为防止这种「跨环境误用」，Tilus 给每张 dispatch 表附上一份**环境指纹**（metadata），落盘时一起写入 `dispatch_table.json`；加载时把磁盘上的指纹和当前环境比对，**任何一项不符就当作没有这张表、重新调优**。这一思路借鉴自 FlashInfer 的 autotuner 缓存（见源码注释）。

指纹包含五项：tilus 版本（取**发行基线**如 `0.2.1`，而非完整开发版 `0.2.1.dev19+g<hash>`）、target、GPU 名称、算力（compute capability）、CUDA 版本。

#### 4.3.2 核心流程

```text
选优成功
   └─ dump_dispatch_table()
        └─ data = {"_metadata": collect_tuning_metadata(), "entries": [...]}
        └─ 写 dispatch_table.json + 人类可读 dispatch_table.txt

下次加载
   └─ load_dispatch_table()
        ├─ 读 dispatch_table.json
        ├─ tuning_metadata_matches(saved["_metadata"], collect_tuning_metadata())?
        │     ├─ 全部匹配 ──► 载入 entries，复用选优结果
        │     └─ 任一不匹配（或无 metadata 的旧表）──► 丢弃，重新调优
```

#### 4.3.3 源码精读

**采集指纹 `collect_tuning_metadata`**：

[python/tilus/lang/instantiated_script.py:193-208](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instantiated_script.py#L193-L208) —— 返回一个 dict，每项都用 `_safe_str` 包裹（取不到就写 `"unknown"` 而非崩溃）：

```python
return {
    "tilus_version": _tilus_version(),                 # 发行基线版本
    "target": _safe_str(get_current_target),
    "gpu": _safe_str(lambda: torch.cuda.get_device_name(...)),
    "compute_capability": _safe_str(lambda: "{}.{}".format(*torch.cuda.get_device_capability())),
    "cuda_version": _safe_str(lambda: torch.version.cuda),
}
```

其中 `_tilus_version`（[instantiated_script.py:170-190](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instantiated_script.py#L170-L190)）特意取 `packaging.Version(raw).base_version`，把 `.devN`/`+g<hash>` 这类开发后缀剥掉。注释说明原因：开发期每次提交 SCM 版本都会变，若用完整版本会把 dispatch 缓存在每次提交后都失效；用发行基线则「同一发行版的所有 dev 构建共享缓存，不同发行版互相隔离」。

**比对指纹 `tuning_metadata_matches`**：

[python/tilus/lang/instantiated_script.py:211-227](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instantiated_script.py#L211-L227) —— 规则：当前环境的**每一项**都必须与磁盘上的对应项相等；磁盘上某项若为通配符 `"*"` 则匹配任意值；磁盘数据不是 dict（如没有 metadata 的旧表）**永不匹配**：

```python
if not isinstance(saved, dict):
    return False
for key, current_value in current.items():
    saved_value = saved.get(key)
    if saved_value == "*":
        continue
    if saved_value != current_value:
        return False
return True
```

`"*"` 通配符是给高级用户的「后门」：手动编辑 `dispatch_table.json` 把某项设成 `"*"`，可以放宽单项校验（例如明知 CUDA 小版本差异不影响，强制复用表）。

**写表 `dump_dispatch_table`**：

[python/tilus/lang/instantiated_script.py:787-804](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instantiated_script.py#L787-L804) —— 把 `_metadata`（指纹）和 `entries`（`[[tuning_key_list, choice], ...]`）一起写成 JSON；同时用 `tabulate` 输出一份人类可读的 `dispatch_table.txt`（表头是各调优参数名 + `choice`）。

**读表 `load_dispatch_table`**：

[python/tilus/lang/instantiated_script.py:773-785](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instantiated_script.py#L773-L785) —— 读 JSON，取出 `_metadata` 与当前指纹比对；不匹配直接 `return`（保持空表，随后触发重新调优）；匹配才把 `entries` 还原成 `dict[tuple,key→choice]`。

**dispatch 缓存目录结构**（`<cache_dir>/scripts/<snake_name>/<jit_key 与 hash 拼成的目录>/`）：

[python/tilus/lang/instantiated_script.py:517-531](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instantiated_script.py#L517-L531) —— 目录名由 jit_key 各项 + 一个 8 位哈希（`sha256(options + 拼接的程序文本)[:8]`）用 `-` 拼成。目录内典型文件：

| 文件/目录 | 内容 |
| --- | --- |
| `source.txt` | 内核类的源码 |
| `meta.json` | 参数名/类型/分类、jit_key |
| `build_options.txt` | 构建选项 |
| `schedule.txt` | 所有有效 schedule（人类可读表） |
| `programs/0,1,...` | 软链，指向各候选在 `programs/<hash>/` 的编译产物 |
| `dispatch_table.json` | **指纹 + 选优表**（机器读） |
| `dispatch_table.txt` | 选优表（人类可读） |
| `latency/<tuning_key>/report.txt` | 该尺寸桶下各候选延迟排名 |
| `latency/<tuning_key>/<best_idx>` | 软链，指向最优 program |
| `failed/scheduling/`、`failed/building/` | 失败 schedule 的 traceback |

注意第二层映射关系：`scripts/.../programs/<idx>` 是软链，指向 u8-1 讲的 `programs/<hash>/`。也就是说 **dispatch 缓存层并不重复存储 `.so`，它只是「选优结果 + 指向已编译产物的软链」**。

#### 4.3.4 代码实践

**实践目标**：看清一张真实 dispatch 表的指纹与条目结构。

**操作步骤**：

1. 跑一次带 autotune 的内核（见第 5 节综合实践）。
2. 在缓存目录定位到 `dispatch_table.json`，用 `cat` 或编辑器打开。
3. 对照 [instantiated_script.py:193-208](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instantiated_script.py#L193-L208) 与 [787-804](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instantiated_script.py#L787-L804) 阅读其结构。

**需要观察的现象 / 预期结果**：

- JSON 顶层有两个键：`_metadata`（含 `tilus_version`/`target`/`gpu`/`compute_capability`/`cuda_version`）和 `entries`（`[[<tuning_key 列表>, <choice 整数>], ...]`）。
- `dispatch_table.txt` 是一张表，列名是各调优参数名 + `choice`。

> 待本地验证：在另一台 GPU 上复用同一缓存目录时，`load_dispatch_table` 会因指纹不符而忽略该表并重新调优——你应能看到又一次 `[Tuning]` 进度条。

#### 4.3.5 小练习与答案

**练习 1**：为什么指纹里的 tilus 版本用「发行基线」而不是完整 SCM 版本？

**答案**：开发期每个 commit 都会让 SCM 版本（如 `0.2.1.dev19+g<hash>`）变化，若用它做指纹，每次提交都会使 dispatch 缓存失效、被迫重新调优，极其浪费。取 `base_version`（`0.2.1`）则让同一发行版的所有 dev 构建共享缓存，而跨发行版（`0.2.1` → `0.2.2`）仍互相隔离。

**练习 2**：一张没有 `_metadata` 字段的旧 dispatch 表，加载时会怎样？

**答案**：`tuning_metadata_matches` 第一行 `if not isinstance(saved, dict): return False` 会判其不匹配（`saved` 为 `None`），于是整张表被忽略、内核重新调优。这保证旧格式缓存不会在新版 Tilus 里被误用。

---

## 5. 综合实践

**任务**：运行一个带 `@autotune` 的内核两次，第二次确认**直接命中 dispatch 缓存而无需重新调优**，并读懂缓存目录里的每一类文件。

**示例代码**（基于 [examples/matmul/matmul_v2.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/matmul/matmul_v2.py) 改写，仅用于学习）：

```python
# 示例代码：演示 dispatch 缓存的「首次调优 / 二次命中」
import math
import torch
import tilus
from examples.matmul.matmul_v2 import MatmulV2    # 复用官方示例内核（示例写法）

tilus.option.cache_dir("/tmp/tilus-dispatch-demo")  # 用独立缓存目录便于观察

m = n = k = 4096
a = (torch.rand(m, k, dtype=torch.float16).cuda() - 0.5) / math.sqrt(k)
b = (torch.rand(k, n, dtype=torch.float16).cuda() - 0.5) / math.sqrt(k)
c = torch.empty(m, n, dtype=torch.float16).cuda()

matmul = MatmulV2()      # 此时不编译
matmul(m, n, k, a, b, c) # 首次调用：触发转译 + 编译 + 调优
```

**操作步骤**：

1. **首次运行**（清空 `/tmp/tilus-dispatch-demo`）：
   - 观察终端依次出现 `[Scheduling]`、`[Building]`、`[Tuning]` 三类 `tqdm` 进度条。
   - 运行结束后，进入缓存目录 `scripts/matmul_v2/<jit 目录>/`，逐一查看：`schedule.txt`、`programs/`（软链）、`dispatch_table.json`（含 `_metadata` 与 `entries`）、`dispatch_table.txt`、`latency/<桶>/report.txt`。
   - 用 `torch.testing.assert_close(c, a @ b)` 校验结果正确。

2. **第二次运行**（**新进程**，保留同一缓存目录，同样的 `m,n,k`）：
   - 把上面的脚本再跑一遍（或开新的 Python 进程）。
   - 观察进度条：应**只出现 `[Scheduling]` 与 `[Building]`，不再出现 `[Tuning]`**。
     - `[Building]` 之所以仍出现但很快，是因为 `build_program` 命中了 u8-1 的 `programs/<hash>/` 缓存（`.so` 直接复用，不再调 nvcc）。
     - 没有 `[Tuning]` 是因为 `load_dispatch_table` 读到了 `dispatch_table.json` 且指纹匹配，tuning_key 直接命中、跳过 benchmark。

3. **（进阶）改尺寸到相邻桶**：把 `m=n=k=4096` 改成 `4000` 再跑（新进程）：
   - 因为 `bucket(4000)=4096` 与 `bucket(4096)=4096` 同桶，dispatch 表里已有该桶记录 → 仍不调优、直接命中。
   - 再改成 `4097`（`bucket=6144`，新桶）→ 出现一次 `[Tuning]`，给新桶补一条 dispatch 记录。

**需要观察的现象 / 预期结果**：

| 运行 | 缓存状态 | Scheduling | Building | Tuning | dispatch_table.json |
| --- | --- | --- | --- | --- | --- |
| 第 1 次 | 空 | 有 | 有（慢，nvcc） | 有 | 新建 |
| 第 2 次（同尺寸） | 有 | 有 | 有（快，复用 .so） | **无** | 只读不改 |
| 改 4000（同桶） | 有 | 有 | 有（快） | **无** | 只读 |
| 改 4097（新桶） | 有 | 有 | 有（快） | 有（仅新桶） | 追加一条 |

> 待本地验证：以上「是否出现 Tuning」的判断，以终端是否打印 `[Tuning] ...` 进度条为准；非 TTY 环境下 `tqdm` 可能静默，可改为在 `_pick_best_program` 处加日志确认。

## 6. 本讲小结

- `__call__` 参数按类型标注三分：**常量参数**（值进 jit_key，值变重编译）、**调优参数**（整除性进 jit_key、尺寸桶进 tuning_key，不重编译）、**内核参数**（仅作 launch 实参）。
- 同一个 `int32` 尺寸参数被拆成两个侧面：整除性决定**编译哪组二进制**（jit_key），尺寸桶决定**选哪份最快**（tuning_key）；桶宽约为尺寸的 1/4，使 dispatch 表条目数受控。
- 调优链路是「并行转译 → 并行编译 → 串行 benchmark 选优」，失败的 schedule 只记录不中断；`parallel_imap` 用 fork 池 + 下标传参避免序列化，且不可递归。
- benchmark 用「清 L2 + CUDA event + 取中位数」测准延迟，参数由 `bench_warmup`/`bench_repeat` 控制；只有一份候选时跳过测量。
- dispatch 表带**环境指纹**（tilus 发行基线版本、target、GPU、算力、CUDA 版本），落盘进 `dispatch_table.json`；加载时任一不符即丢弃重调优，旧表永不匹配；`"*"` 通配符可手动放宽单项。
- dispatch 缓存层（`scripts/<name>/...`）只存「选优结果 + 软链」，真正的 `.so` 仍在 u8-1 的 `programs/<hash>/` 里，两层缓存各司其职。

## 7. 下一步学习建议

- 想看「不调优、只编译」的入口（CI 里验证内核能在某架构编译通过）：阅读 `InstantiatedScript.compile` 与 `_jit_instance_for`（[instantiated_script.py:860-904](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instantiated_script.py#L860-L904)），它转译并编译所有 schedule 但不 benchmark、不落 dispatch 表，这正是 u8-4 要讲的 compile-only 测试模式的基础。
- 想理解选出的 `CompiledProgram` 如何被真正 launch：进入 u8-3「运行时：CompiledProgram 与内核启动」，看 `.so` 加载、torch 张量到设备指针的映射、Metadata 如何决定 grid/cluster/warps。
- 想把「调优 + 缓存 + 调试」串成完整工作流：继续阅读 u8-4「调试、测试与性能剖析」，结合 `dump_ir`、`disable_ptxas_opt` 与 ncu 剖析，理解为何某份 schedule 在 benchmark 中胜出。
