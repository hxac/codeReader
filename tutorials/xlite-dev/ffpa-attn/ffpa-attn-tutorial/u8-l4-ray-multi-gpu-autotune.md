# Ray 多 GPU 并行调优

## 1. 本讲目标

本讲承接 u8-l2（持久化调优配置生成器 CLI）与 u8-l3（运行时配置查找）。在那里我们学过：`python -m ffpa_attn.autotune` 会把一张目标显卡上各种 `head_dim × seqlen × 变体` 的最优 launch config 写成一张「设备 JSON」。但**生成一张完整 JSON 的总耗时，是所有形状调优时间之和**——`--full-tasks` + `--mode max` + 双 dtype 下，任务数很容易上百，单卡串行跑可能要数小时。

本讲解决一个问题：**当机器有多张相同型号的 GPU 时，如何把这些互相独立的调优任务并行分摊到多张卡上，把总墙钟时间降到接近 1/N。**

学完后你应该掌握：

1. 理解 `TritonAutotuneWorker` 这个 Ray actor 的「一卡一 actor」设计与按 **PCI bus 隔离私有 Triton cache** 的动机。
2. 读懂 `run_ray_autotune` 的 **actor pool + 队列调度**：Phase A 先给每个 worker 塞一个任务填满，Phase B 哪个 worker 先完成就立刻给它派下一个任务。
3. 理解 `ray.wait` 动态派发如何始终把 GPU 占满、又如何在某个任务失败时容错而不让整批崩溃。
4. 掌握 `--num-gpus` / `--ray-address` 两个 CLI 开关的用法、校验与本地/远程两种模式。

---

## 2. 前置知识

阅读本讲前，最好先建立以下直觉（对应前置讲义）：

- **持久化 autotune 是离线的**（u8-l2）：它一次性把很多形状的 launch config benchmark 出来落盘成 `{device_name}.json`，运行时（u8-l3）只查不算。本讲就是把这一步「一次性算很多形状」从单卡串行变成多卡并行。
- **TuneTask 是最小调度单元**：每个待调优形状被封装成一个 [`TuneTask`](src/ffpa_attn/autotune.py#L71-L99)（含 `direction / dtype / headdim / seqlen_q / seqlen_k / causal / ...`），任务之间**完全独立、无依赖**——这正是可并行的前提。
- **Triton 会 JIT 编译 kernel 并写本地缓存**：同一个 kernel family 第一次 benchmark 时，Triton 把编译产物落进 `TRITON_CACHE_DIR`。多个进程**共用同一个 cache 目录并发编译同一族 kernel，会触发文件竞争**——这是本讲要解决的核心工程坑。

需要补充的几个 Ray 基础概念（不熟悉 Ray 的读者看这里）：

| 术语 | 含义 |
| --- | --- |
| **Ray actor** | 一个常驻的有状态对象，跑在独立进程里。用 `@ray.remote` 装饰一个类即可定义，用 `.remote()` 创建实例。 |
| `@ray.remote(num_gpus=1)` | 声明每个该 actor 实例**独占 1 张 GPU**。Ray 会给该 actor 进程的 `CUDA_VISIBLE_DEVICES` 只暴露一张物理卡，进程内 `torch.cuda` 永远只看到 device 0。 |
| **ObjectRef（future）** | 调用 `actor.method.remote(...)` 立即返回一个「未来值」引用，真正的计算在 actor 进程里异步进行，用 `ray.get(ref)` 阻塞取回结果。 |
| `ray.wait(refs, num_returns=1)` | 阻塞直到 `refs` 里**至少有 1 个**完成，返回 `(ready, not_ready)` 两组。这是「完成一个就立刻处理一个」的关键原语。 |
| `ray.cluster_resources()` | 返回整个集群（本地或远程）的资源总量，`.get("GPU", 0)` 即可见 GPU 总数。 |
| `ray.init(address=...)` | 连接 Ray 集群；`address=None` 时启动一个本地单机 Ray。 |

> 关于依赖：仓库的 `pyproject.toml` 把 `ray` 列为核心依赖（[pyproject.toml:L12-L21](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/pyproject.toml#L12-L21)），但 `import ffpa_attn.ray` **不会**触发 `import ray`——Ray 运行时只有在真正调用 `run_ray_autotune` 时才被加载（见 4.1）。

---

## 3. 本讲源码地图

本讲全部围绕 `src/ffpa_attn/ray/` 这个小包（三个文件）展开，外接 CLI 的两行分发。

| 文件 | 行数规模 | 作用 |
| --- | --- | --- |
| [src/ffpa_attn/ray/__init__.py](src/ffpa_attn/ray/__init__.py) | ~28 行 | 包的**唯一公共入口** `run_ray_autotune`，刻意做成懒导入，保证 `import ffpa_attn.ray` 时不加载 Ray。 |
| [src/ffpa_attn/ray/_autotune_engine.py](src/ffpa_attn/ray/_autotune_engine.py) | ~143 行 | **调度引擎**：初始化 Ray、校验 GPU 数、建 actor 池、跑 Phase A/B 派发循环、收结果、关 Ray。 |
| [src/ffpa_attn/ray/_autotune_worker.py](src/ffpa_attn/ray/_autotune_worker.py) | ~122 行 | **Worker actor**：`@ray.remote(num_gpus=1)`，每张卡一个；在 `__init__` 里按 PCI bus 隔离 Triton cache；`run_task` 委托给已有的 `_tune_forward/_tune_backward`。 |
| [src/ffpa_attn/autotune.py](src/ffpa_attn/autotune.py)（L1043-L1056） | — | CLI 主流程里 `--num-gpus>1` 时切到 Ray 路径的分发点，以及对缺 `ray` 包的友好报错。 |
| [docs/user_guide/autotune.md](docs/user_guide/autotune.md)（L139-L203） | — | 官方多卡调优用法、远程集群、smoke test 命令。 |

一句话记忆：**`__init__.py` 是门面、`_autotune_engine.py` 是调度器、`_autotune_worker.py` 是干活的卡**。

---

## 4. 核心概念与源码讲解

### 4.1 多卡调优的动机与整体架构（CLI 接入与校验）

#### 4.1.1 概念说明

持久化生成是一组**尴尬并行（embarrassingly parallel）**的任务：每个 `TuneTask` 只依赖自己的形状参数，不依赖别的任务的结果。单卡串行时总时间 ≈ 任务数 × 单任务时间；如果有 N 张同型号卡，理想情况下能降到 ≈ (总时间)/N。

但要安全地并行，必须先解决两个工程问题：

1. **资源独占**：Triton autotune 会把目标 GPU 跑满（反复 benchmark 各种 config），两张卡上的任务**绝不能抢同一张物理 GPU**，否则 benchmark 出来的耗时不准、配置就废了。
2. **Triton cache 竞争**：多个进程并发编译同一族 kernel、共用一个 cache 目录会互相踩文件。需要给每个物理 GPU 一个**私有 cache 目录**。

FFPA 用 **Ray actor** 一次性解决这两点：`@ray.remote(num_gpus=1)` 天然实现「一卡一进程、互不抢卡」；在 actor 内按物理卡的 PCI bus id 建私有 cache 目录，又解决了竞争（见 4.2）。

#### 4.1.2 核心流程

整体链路（从 CLI 到结果落盘）：

```text
python -m ffpa_attn.autotune --num-gpus 4 ...
        │
        ▼
autotune.main()  解析 args、生成 tasks 列表
        │  args.num_gpus > 1 ?
        ▼  是
autotune.py L1043  try: import ray（友好报错）
        │
        ▼
ray.run_ray_autotune(tasks, args)        ← 包门面（懒导入）
        │
        ▼
_autotune_engine.run_ray_autotune        ← 本讲主角
   1) ray.init(address=args.ray_address)
   2) 校验 GPU 数 >= num_gpus，否则 SystemExit
   3) 建 num_gpus 个 TritonAutotuneWorker actor（各占 1 GPU）
   4) Phase A：给每个 worker 塞 1 个任务
   5) Phase B：ray.wait 循环，谁完成给谁派下一个
   6) 收集 all_entries，finally ray.shutdown()
        │
        ▼
autotune.py L1055  for entry: _record_entry(...)  合并去重
        │
        ▼
写 {device_name}.json（与单卡路径完全相同的输出格式）
```

关键设计：**输出与单卡路径完全一致**——worker 不写任何中间文件，结果汇总后走同一个 `_record_entry` 合并、同一个 `device_config_path` 落盘。多卡只是「怎么算」的并行，不改变「算出什么、写到哪」。

#### 4.1.3 源码精读

CLI 分发点在 `autotune.py` 的主流程里：

[autotune 分发到 Ray 路径:L1043-L1056](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/autotune.py#L1043-L1056) — 当 `args.num_gpus is not None and args.num_gpus > 1` 时走 Ray；先 `import ray` 探测，缺包则给出「Install: pip install ray」的友好报错，再调用 `run_ray_autotune`，结果逐条交给 `_record_entry` 合并。

注意判定的边界：`num_gpus == 1` 或 `None` 都走 **else 单卡串行分支**（[autotune.py:L1057-L1058](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/autotune.py#L1057-L1058)）。即「多卡」专指 ≥2。

两个 CLI 开关的定义：

[--num-gpus / --ray-address:L968-L982](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/autotune.py#L968-L982) — `--num-gpus` 默认 `None`（保持单卡、零 Ray 依赖）；`--ray-address` 默认 `None`（本地单机 Ray）。help 文本明确：本地模式 `--num-gpus` 不得超过 `CUDA_VISIBLE_DEVICES` 数量，远程模式则向集群请求那么多 GPU、忽略本地 `CUDA_VISIBLE_DEVICES`。

包门面的懒导入：

[run_ray_autotune 懒导入实现:L15-L27](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/ray/__init__.py#L15-L27) — 公开函数只是个壳，**第一次被调用时**才 `from ._autotune_engine import run_ray_autotune as _impl`。结合该文件顶部注释「Importing this module does **not** import `ray`」（[ray/__init__.py:L8-L10](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/ray/__init__.py#L8-L10)），意味着不用多卡的用户从不会为 Ray 运行时付出 import 代价。

#### 4.1.4 代码实践

1. **实践目标**：在不真正运行多卡的前提下，确认「多卡路径是 opt-in 的、且对单卡用户透明无感」。
2. **操作步骤**：
   - 打开 [src/ffpa_attn/autotune.py:L1043-L1058](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/autotune.py#L1043-L1058)，确认默认 `num_gpus=None` 走 else 串行分支。
   - 在 Python 里执行（单卡机器即可）：`import ffpa_attn.ray; print("ok")`。
3. **需要观察的现象**：第二步不应报「找不到 ray」之类的错。
4. **预期结果**：打印 `ok`。因为 `import ffpa_attn.ray` 只加载了 `__init__.py` 这个壳，并未触发 `import ray`；真正的 `import ray` 发生在 `_autotune_engine.py` 顶部（[L14-L15](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/ray/_autotune_engine.py#L14-L15)），而引擎是懒导入的。
5. 若你想进一步验证懒导入边界，可在 `import ffpa_attn.ray` 之后查 `sys.modules.get("ray")` 应为 `None`（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `num_gpus == 1` 也走单卡串行，而不走只有一个 actor 的 Ray 路径？

**答案**：单 actor 的 Ray 路径没有任何并行收益，反而要付出 `ray.init`、actor 启动、序列化往返的开销，且引入 Ray 运行时依赖。串行路径（[autotune.py:L1057-L1094](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/autotune.py#L1057-L1094)）直接在主进程里循环调 `_tune_forward/_tune_backward`，最简单也最快。Ray 只在 ≥2 卡时才划算。

**练习 2**：`--num-gpus` 与 `CUDA_VISIBLE_DEVICES` 是什么关系？

**答案**：本地模式下，Ray 把 `CUDA_VISIBLE_DEVICES` 列出的物理卡作为资源池，每张卡算 1 个 GPU 资源；`--num-gpus` 不得超过该数量，否则 [`run_ray_autotune` 在 L71-L76 抛 SystemExit](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/ray/_autotune_engine.py#L71-L76)。远程模式（`--ray-address`）下本地 `CUDA_VISIBLE_DEVICES` 被忽略，`--num-gpus` 改为向远程集群请求那么多 GPU。

---

### 4.2 TritonAutotuneWorker：一卡一 actor + PCI bus 隔离的 Triton cache

#### 4.2.1 概念说明

`TritonAutotuneWorker` 是真正「在一张 GPU 上跑一个调优任务」的单元。它有两个刻意设计：

- **一卡一 actor**：`@ray.remote(num_gpus=1)` 让 Ray 给每个 actor 进程的 `CUDA_VISIBLE_DEVICES` 只塞一张物理卡。于是 actor 内 `torch.cuda` 永远只看得到 device 0，`torch.cuda.set_device(0)` 即绑定了那张唯一的物理卡。这从根本上保证两个任务不会抢同一张卡、benchmark 计时才准。

- **按 PCI bus 隔离 Triton cache**：Triton 在 JIT 编译时会写本地文件缓存。多个 actor 并发编译同一族 kernel 时若共用一个 cache 目录，会出现「两个进程同时写同一个 `.lock` / `.ttir` / `.cubin`」的竞争，轻则报错重试、重则写出半截文件。FFPA 的做法是：用**物理 GPU 的 PCI bus id** 给每张卡命名一个私有 cache 目录（如 `/tmp/ffpa_triton_cache/gpu_bus_id_<bus>`）。这样无论 `CUDA_VISIBLE_DEVICES` 怎么重排，同一张物理卡始终复用自己的 cache——既避免竞争，又能在跨 session 时复用编译产物。

#### 4.2.2 核心流程

Worker 生命周期：

```text
Ray 建 actor → TritonAutotuneWorker.__init__():
   1) torch.cuda.set_device(0)              # 绑定 Ray 分给它的那张物理卡
   2) bus_id = device_properties(0).pci_bus_id   # 物理卡的 PCI 总线号
   3) TRITON_CACHE_DIR = /tmp/ffpa_triton_cache/gpu_bus_id_<safe_id>
   4) os.environ["TRITON_CACHE_DIR"] = ...  # 本进程私有

调度器派任务 → run_task(task, batch, mode, enable_...):
   1) 懒 import _tune_forward / _tune_backward（断循环依赖）
   2) with exact_autotune_seqlen_keys():     # 离线用精确 key，不走运行时分桶
        forward → _tune_forward(...)  /  backward → _tune_backward(...)
   3) torch.cuda.synchronize()
   4) 成功 → 返回 [entry, ...]；OOM → empty_cache + 返回 []；其它异常 → 记 warning + 返回 []
```

注意：`run_task` 把 import 推迟到方法内部（懒导入），是为了打破 `ffpa_attn.ray` 与 `ffpa_attn.autotune` 之间潜在的循环引用——worker 模块被 Ray 加载时不会立刻把整个 autotune 模块拉起来。

#### 4.2.3 源码精读

类定义与 docstring：

[@ray.remote(num_gpus=1) class TritonAutotuneWorker:L33-L44](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/ray/_autotune_worker.py#L33-L44) — 装饰器 `num_gpus=1` 即「一卡一 actor」的源头；docstring 明确「Ray isolates the actor to a single GPU via `num_gpus=1`, so the actor only ever sees device 0」。

PCI bus cache 隔离：

[__init__ 按 PCI bus 建 cache:L46-L52](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/ray/_autotune_worker.py#L46-L52) — 读 `torch.cuda.get_device_properties(0).pci_bus_id`，把其中的 `:` 和 `.` 替换成 `_`（避免在文件路径里出现非法字符），拼成 `/tmp/ffpa_triton_cache/gpu_bus_id_{safe_id}` 并写进进程级 `os.environ["TRITON_CACHE_DIR"]`。关键在于用 **PCI bus id（物理标识）** 而非 Ray 分配的逻辑 device 0——后者会随 `CUDA_VISIBLE_DEVICES` 顺序变化，无法稳定复用 cache。

run_task 的懒导入与分发：

[run_task 主体:L81-L106](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/ray/_autotune_worker.py#L81-L106) — 方法内才 `from ..autotune import _tune_backward, _tune_forward` 与 `exact_autotune_seqlen_keys`；按 `task.direction` 分流到前向/反向调优函数，把 `enable_fwd_tma/ws`（或 `enable_bwd_tma/ws/split_launch`）透传过去；`torch.cuda.synchronize()` 确保计时完整后再返回 `[entry for entry, _ in tuned]`（丢掉每个 entry 旁的候选计数）。

OOM 与异常容错：

[异常处理:L107-L121](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/ray/_autotune_worker.py#L107-L121) — 对 `RuntimeError` 且消息含 `out of memory` 的特判：调 `torch.cuda.empty_cache()` 后返回空列表（即「这个形状跳过、不产生 entry」），让长序列大 head_dim 在显存不够的卡上优雅跳过而非炸掉整批；其它异常则记一条 warning 也返回空列表。这一点对多卡容错至关重要（见 4.4）。

#### 4.2.4 代码实践

1. **实践目标**：验证「同一物理卡跨 session 复用 cache、不同物理卡互不干扰」。
2. **操作步骤**：
   - 在装有 ≥2 张同型号 GPU 的机器上跑一次多卡 smoke test（来自 [docs/user_guide/autotune.md:L196-L203](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/docs/user_guide/autotune.md#L196-L203)）：
     ```bash
     CUDA_VISIBLE_DEVICES=0,1 \
     FFPA_AUTOTUNE_MAX_CONFIGS=4 \
     python -m ffpa_attn.autotune --mode fast --num-gpus 2 --overwrite \
       --output-dir /tmp/ffpa-config-smoke
     ```
   - 任务结束后查看 cache 目录：`ls /tmp/ffpa_triton_cache/`。
3. **需要观察的现象**：`/tmp/ffpa_triton_cache/` 下应出现**两个**子目录，名字形如 `gpu_bus_id_<busA>` 与 `gpu_bus_id_<busB>`，对应两张物理卡各自的 PCI bus id。
4. **预期结果**：两个目录各自独立、互无重叠文件。再跑一次同样的命令，第二次因为 cache 已存在，Triton 编译阶段会明显更快（命中各自目录里的 `.cubin`）。
5. 若你的机器只有 1 张卡，可改为 `--num-gpus 1`……但注意 `num_gpus==1` 走的是**串行路径、不会建 actor**，因此看不到 `gpu_bus_id_*` 目录（该目录只在 actor `__init__` 里创建）。要观察 PCI bus 隔离至少需要 2 卡（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：为什么用 PCI bus id 而不是「actor 序号 0/1/2/3」给 cache 目录命名？

**答案**：actor 序号是 Ray 的逻辑编号，与物理卡的对应关系受 `CUDA_VISIBLE_DEVICES` 顺序影响，跨 session 不稳定。PCI bus id 是物理卡的硬件标识，恒定不变；用它命名能保证「同一张物理卡在任何 session 里都复用同一个 cache 目录」，既避免并发竞争又能跨 session 复用编译产物（见 worker docstring [L40-L44](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/ray/_autotune_worker.py#L40-L44)）。

**练习 2**：`run_task` 里为什么要把 `from ..autotune import ...` 写在函数体内，而不是模块顶部？

**答案**：为了打破循环 import。worker 模块（`ffpa_attn.ray._autotune_worker`）会被引擎在运行时导入，而 `ffpa_attn.autotune` 反过来又会在 CLI 里 `from .ray import run_ray_autotune`。把 autotune 的 import 推迟到 `run_task` 内部（[L81-L82](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/ray/_autotune_worker.py#L81-L82)），让模块加载阶段不形成环；这也是 worker 模块顶部 docstring 明确说的设计（[L4-L7](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/ray/_autotune_worker.py#L4-L7)）。

---

### 4.3 run_ray_autotune：actor pool + 队列调度（Phase A 填充、Phase B 完成即派发）

#### 4.3.1 概念说明

有了「一卡一 actor」的 worker，下一步是：**怎么把 M 个任务分给 N 个 actor？** 最朴素的两种做法都有问题：

- **静态均分**（每个 worker 预先分到 `M/N` 个任务）：问题在于各任务耗时差异巨大（`D=1024, N=16384` 比 `D=320, N=1` 慢几十倍），均分会让先跑完的 worker 闲着等最慢的那个——**负载不均**。
- **一次全派**（把 M 个 future 同时发出去）：问题在于 Ray 会把它们排队，但每个 actor 同时只能跑 1 个 `run_task`（因为 GPU 独占），反而把调度复杂度推给 Ray，且违背「一卡一任务」。

FFPA 采用经典的 **actor pool + 工作队列** 模式，分两阶段：

- **Phase A（预热填充）**：给每个 worker 先各派**一个**任务，把所有 GPU 同时点起来。
- **Phase B（完成即派发）**：进入 `ray.wait` 循环，**哪个 worker 先完成，就立刻把队列里下一个任务派给同一个 worker**。

这样天然实现**动态负载均衡**：慢任务占住的 worker 不影响别人，快任务的 worker 会自动多领活儿，GPU 始终保持满载直到队列清空。它严格遵守「一卡一任务」约束（每个 actor 同时只有 1 个在跑的 `run_task`），又把派发决策简化成「谁空了给谁」。

#### 4.3.2 核心流程

调度主循环（伪代码，对应 `_autotune_engine.py` 的真实结构）：

```text
workers = [TritonAutotuneWorker.options(num_gpus=1).remote() for _ in range(num_gpus)]
pending = {}            # future -> (worker_idx, task, submit_time)
task_index = 0

# Phase A: 每个 worker 先领一个任务
for i, worker in enumerate(workers):
    if task_index >= total: break
    future = _submit_task(worker, tasks[task_index], args)
    pending[future] = (i, tasks[task_index], now)
    task_index += 1

# Phase B: 谁完成给谁派下一个
while pending:
    ready, _ = ray.wait(list(pending), num_returns=1)   # 等任意 1 个完成
    for future in ready:
        worker_idx, task, submit_time = pending.pop(future)
        entries = ray.get(future)          # 取结果（失败见 4.4）
        all_entries.extend(entries)
        log 进度
        if task_index < total:             # 队列还有任务
            next_future = _submit_task(workers[worker_idx], tasks[task_index], args)
            pending[next_future] = (worker_idx, tasks[task_index], now)
            task_index += 1
```

两个要点：

- **派给「同一个 `worker_idx`」**：`ready` 里的 future 来自哪个 worker，下一个任务就交给那个 worker——因为那个 actor 此刻正好空出来了。这就是「完成即派发」。
- **`task_index` 是全局游标**：所有 worker 共享同一个待派任务队列，由 `task_index` 单调推进，绝不重复派、绝不漏派。

#### 4.3.3 源码精读

初始化 Ray 与 GPU 数校验：

[ray.init + GPU 校验:L65-L83](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/ray/_autotune_engine.py#L65-L83) — `ray.init(address=args.ray_address, ignore_reinit_error=True, include_dashboard=False)`；从 `ray.cluster_resources().get("GPU", 0)` 取可见 GPU 总数，小于 `args.num_gpus` 即 `raise SystemExit`（提示检查 `CUDA_VISIBLE_DEVICES` 或 `--ray-address`）；随后建 `num_gpus` 个 actor，每个 `.options(num_gpus=1)`。`os.environ.setdefault("RAY_ACCEL_ENV_VAR_OVERRIDE_ON_ZERO", "0")`（[L66](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/ray/_autotune_engine.py#L66)）是为了在资源计数可能为 0 的环境里让 Ray 行为可预期。

任务提交辅助函数：

[_submit_task:L34-L51](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/ray/_autotune_engine.py#L34-L51) — 把 `worker.run_task.remote(task, args.B, args.mode, enable_fwd_tma=..., ...)` 这一行包成函数，返回 future。所有 `enable_*` 开关从 CLI namespace 透传给 worker，保证多卡与单卡调的是**同一组开关**。

Phase A 填充：

[Phase A:L90-L97](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/ray/_autotune_engine.py#L90-L97) — `for i, worker in enumerate(workers)` 给每个 worker 提交一个任务，记进 `pending`（value 是三元组 `(worker_idx, task, submit_time)`），同时推进 `task_index`。`if task_index >= total: break` 处理任务数少于 worker 数的边界。

Phase B 完成即派发：

[Phase B:L99-L131](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/ray/_autotune_engine.py#L99-L131) — `ray.wait(list(pending.keys()), num_returns=1, timeout=None)` 阻塞到至少一个 future ready；对每个 ready 的 future，从 `pending` 弹出对应三元组，`ray.get` 取结果（失败处理见 4.4），`all_entries.extend(entries)` 累积；只要 `task_index < total`，就向**同一个 `workers[worker_idx]`** 派下一个任务并推进游标。`timeout=None` 表示无限等待，直到有任务完成。

进度统计的巧妙算式：

[done 计数:L115](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/ray/_autotune_engine.py#L115) — `done = total - len(pending) - (total - task_index)`。其中 `len(pending)` 是「在途未完成数」，`(total - task_index)` 是「尚未派发数」，二者都从总数里扣掉，剩下的恰好是「已完成数」。这条等式 `已完成 = 总数 − 在途 − 未派发` 在派发下一个任务**之前**计算，恰好反映刚pop掉的那个任务已落地。

收尾：

[汇总与 shutdown:L133-L142](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/ray/_autotune_engine.py#L133-L142) — 跑完打印总耗时与平均每任务耗时；`return all_entries`；`finally: ray.shutdown()` 保证无论正常退出还是异常都释放 Ray 运行时。

#### 4.3.4 代码实践

1. **实践目标**：把 Phase A/B 的派发顺序在纸上推演一遍，确认它确实做到「谁空给谁、不漏不重」。
2. **操作步骤**：
   - 假设 `num_gpus=2`、`tasks=[t0,t1,t2,t3,t4]`（共 5 个），且 t0 比 t1 慢。参照 [Phase A:L90-L97](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/ray/_autotune_engine.py#L90-L97) 与 [Phase B:L99-L131](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/ray/_autotune_engine.py#L99-L131) 画一张表，记录每一步的 `pending`、`task_index`、`done`。
3. **需要观察的现象（推演）**：
   - Phase A 后：worker0 跑 t0、worker1 跑 t1，`task_index=2`，`pending={f0,f1}`。
   - 设 t1 先完成 → pop f1 → `done=1` → worker1 领 t2（`task_index=3`）。
   - t2 完成 → worker1 领 t3（`task_index=4`）；t3 完成 → worker1 领 t4（`task_index=5`）。
   - 最后 t0 完成 → `done=5`，`pending` 空，循环结束。
4. **预期结果**：worker1 因为任务轻，连续领了 t2/t3/t4；worker0 一直跑那个重的 t0。这正是动态负载均衡——总墙钟时间 ≈ max(慢任务链, 快任务链) 而非任务时间之和。`task_index` 从 0 单调走到 5，5 个任务每个恰好被派发一次。
5. 想看真实派发日志，可在多卡机器上跑（日志格式见 [engine L116-L122](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/ray/_autotune_engine.py#L116-L122) 的 `[AUTOTUNED][done/total]` 行）。待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：如果把 Phase A 改成「先给 worker0 派完全部任务再给 worker1」，会损失什么？

**答案**：会退化成静态均分，丢失动态负载均衡。各任务耗时差异极大，均分后先跑完的 worker 会闲置等待最慢的那个，总时间从「接近最慢任务链」退化成「接近任务时间之和」——这正是 Phase B「完成即派发」要避免的（见 [L99-L131](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/ray/_autotune_engine.py#L99-L131)）。

**练习 2**：`pending` 这个 dict 的 value 为什么要存 `worker_idx`？

**答案**：因为 Phase B 派发下一个任务时，必须知道「刚完成的那个 future 来自哪个 worker」，才能把新任务交给**那个此刻已空闲的 actor**（[L125](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/ray/_autotune_engine.py#L125) `workers[worker_idx]`）。`submit_time` 则用于日志里打印单任务耗时（[L114](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/ray/_autotune_engine.py#L114)）。

---

### 4.4 ray.wait 动态派发、容错与收尾

#### 4.4.1 概念说明

本模块把 4.3 的循环里几个「细节但关键」的点单独拎出来：为什么用 `ray.wait` 而不是 `ray.get` 轮询、单个任务失败怎么办、以及为什么绝不让一个失败拖垮整批。

`ray.wait(refs, num_returns=1)` 是事件驱动的：它**阻塞到至少 1 个 future 完成**就返回，不必知道是哪一个、也不必设超时（`timeout=None`）。相比「挨个 `ray.get` 顺序取结果」，它能在任意 worker 先完成时**立刻**响应，是把 GPU 占满的关键。

容错分两层：worker 进程崩了抛 `RayActorError`、任务体里抛 `RayTaskError`，引擎层只记一条 warning 继续（[engine L107-L113](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/ray/_autotune_engine.py#L107-L113)）；而最常见的失败——显存 OOM——已经在 worker 内部被捕获并转成空结果（[worker L107-L111](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/ray/_autotune_worker.py#L107-L111)），根本不会冒泡到引擎。两层配合的效果是：**某个大形状在某张卡上炸了，只会让那一个 entry 缺失，其余形状照常完成**。

#### 4.4.2 核心流程

```text
while pending:
    ready, _ = ray.wait(pending, num_returns=1, timeout=None)   # 事件驱动等待
    for future in ready:
        pop (worker_idx, task, submit_time)
        try:
            entries = ray.get(future)          # 取结果；OOM 已在 worker 内被吞成 []
            all_entries.extend(entries)
        except (RayActorError, RayTaskError) as exc:
            logger.warning(...)                # 进程崩 / 任务抛错：记 warning，继续
        log 进度
        if 队列还有任务: 派给 workers[worker_idx]
# 循环自然结束（pending 空）→ 打印总耗时 → finally ray.shutdown()
```

#### 4.4.3 源码精读

事件驱动等待：

[ray.wait 调用:L101](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/ray/_autotune_engine.py#L101) — `ready, _ = ray.wait(list(pending.keys()), num_returns=1, timeout=None)`。`num_returns=1` 意味着「有一个完成就返回」（不是等全部）；`timeout=None` 意味着「一直等到有完成」。返回的 `ready` 是已完成 future 列表，`_` 是仍在途的。这是把任意 worker 的完成事件**第一时间**转化为派发动作的核心。

引擎层容错：

[RayActorError / RayTaskError 捕获:L104-L113](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/ray/_autotune_engine.py#L104-L113) — `ray.get(future)` 可能抛两种异常：`RayActorError`（actor 进程本身死了，比如 GPU 掉卡）或 `RayTaskError`（任务体抛了未被 worker 自己捕获的异常）。这里只 `logger.warning(...)` 并继续循环——**不 re-raise**，保证一个失败不拖垮其余 N−1 张卡的进度。注意它仍然会走到下面的「派下一个任务」分支（因为这段 except 之后没有 `continue`），所以这个 worker 会被复用去跑后续任务。

worker 层 OOM 吞没：

[worker OOM 特判:L107-L111](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/ray/_autotune_worker.py#L107-L111) — 这是「第一道防线」：大 head_dim + 长序列在显存不够时抛 OOM，worker 捕获后 `torch.cuda.empty_cache()` 释放碎片、返回 `[]`。对引擎而言这次 `ray.get` 正常返回空列表，**不算失败**，不触发 warning。最终效果是这个形状在 JSON 里没有对应 entry，运行时（u8-l3）会自动就近匹配到相邻形状的 entry 或回退默认 config。

两层容错的分工：worker 吞「可预期的形状相关错误」（OOM），引擎吞「不可预期的进程/任务错误」。这样既不让偶发失败卡住整批，又把信息（哪张卡、哪个任务、什么错）记进日志便于排查。

#### 4.4.4 代码实践

1. **实践目标**：理解「单个任务失败不影响其余任务」的容错语义。
2. **操作步骤**：
   - 阅读 [engine L100-L131](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/ray/_autotune_engine.py#L100-L131)，注意 `except (RayActorError, RayTaskError)` 之后**没有 `continue` 或 `break`**——控制流会继续走到 `if task_index < total: 派下一个任务`。
   - 再读 [worker L107-L121](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/ray/_autotune_worker.py#L107-L121)，确认 OOM 被转成 `[]`、其它异常也被转成 `[]`。
3. **需要观察的现象（推演）**：假设 5 个任务里 t2 在 worker 上抛了非 OOM 的异常，t2 会被记一条 warning、`entries` 为空；但 worker 仍会被派去跑 t3、t4，最终 `all_entries` 里包含 t0/t1/t3/t4 的 entry（缺 t2）。
4. **预期结果**：整批不中断，生成的 JSON 只缺 t2 对应的那条；运行时查找（u8-l3）对 t2 形状会就近匹配到相邻 entry 或回退默认 config。这条性质让长时间多卡调优可以「跑完再看哪几个形状需要单独补」。
5. 若要真实验证 OOM 路径，可在显存较小的卡上故意开 `D=1024, N=16384`（`--full-tasks --mode max`）观察是否出现 warning 而非崩溃（待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：为什么引擎层捕获异常后**不**重新抛出（不 `raise`）？

**答案**：因为目标是一次跑完尽可能多的形状、生成尽可能完整的 JSON。重新抛出会让一个失败终止整批，浪费其余 N−1 张卡已完成的进度和剩余队列。记 warning + 继续（[L107-L113](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/ray/_autotune_engine.py#L107-L113)）是「尽力而为、缺的形状事后补」的务实策略，配合 u8-l3 的就近匹配回退，缺失个别 entry 不会让运行时崩溃。

**练习 2**：`ray.wait` 的 `num_returns=1` 改成更大的值会怎样？

**答案**：会变成「等够 K 个完成才返回」，在此期间先完成的 worker 得不到新任务而闲置，破坏「完成即派发」的及时性，降低 GPU 占用率。`num_returns=1` 才能保证「有一个空就立刻补一个」（[L101](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/ray/_autotune_engine.py#L101)）。

---

## 5. 综合实践

把本讲三个核心机制（一卡一 actor + PCI bus cache 隔离、Phase A/B 队列调度、两层容错）串起来，完成下面这个「源码阅读 + 命令实操」综合任务。

**任务**：为 FFPA 在一台 4 卡同型号机器上设计一次多卡持久化调优，并解释数据如何流动。

1. **写命令**：参照 [docs/user_guide/autotune.md:L145-L154](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/docs/user_guide/autotune.md#L145-L154)，写出一条 `CUDA_VISIBLE_DEVICES=4,5,6,7`、`--num-gpus 4`、`--mode max`、`--full-tasks`、双 dtype、`--overwrite` 的命令。说明它为何要限定 4 张卡（与 `--num-gpus` 的校验关系）。
2. **画数据流**：从 CLI 输入到 `{device_name}.json` 落盘，标出每一步在哪个文件、哪一行：CLI 分发（[autotune.py:L1043-L1056](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/autotune.py#L1043-L1056)）→ 引擎建池与 Phase A/B（[_autotune_engine.py:L65-L131](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/ray/_autotune_engine.py#L65-L131)）→ worker 跑 `_tune_forward/_tune_backward`（[_autotune_worker.py:L81-L106](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/ray/_autotune_worker.py#L81-L106)）→ 结果汇总回主进程 → `_record_entry` 合并 → 写 JSON。
3. **解释 PCI bus 隔离**：用你自己的话说，为什么 4 个 actor 必须各用 `/tmp/ffpa_triton_cache/gpu_bus_id_<bus>` 而不是共用一个目录（结合 Triton JIT 并发编译的文件竞争）。
4. **预测容错**：若卡 6 上某个 `D=1024,N=16384` 任务 OOM，描述它会经过哪两层处理（worker 吞 OOM → 引擎正常收到 `[]`），最终 JSON 里这一条缺失、运行时如何就近回退。
5. **运行验证（可选，多卡机器）**：实际跑一条 smoke 版（`FFPA_AUTOTUNE_MAX_CONFIGS=4 --num-gpus 4`，见 [docs:L196-L203](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/docs/user_guide/autotune.md#L196-L203)），观察日志里 `[AUTOTUNED][done/total]` 的 `done` 是否单调递增、`/tmp/ffpa_triton_cache/` 是否出现 4 个 `gpu_bus_id_*` 目录。无多卡机器则标注「待本地验证」。

---

## 6. 本讲小结

- FFPA 用 **Ray actor pool + 工作队列**把离线持久化调优从单卡串行变成多卡并行；`--num-gpus>1` 才走这条路（[autotune.py:L1043-L1056](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/autotune.py#L1043-L1056)），输出与单卡完全一致。
- [`TritonAutotuneWorker`](src/ffpa_attn/ray/_autotune_worker.py#L33-L52) 用 `@ray.remote(num_gpus=1)` 实现「一卡一 actor」，并用物理 GPU 的 **PCI bus id** 给每张卡建私有 `TRITON_CACHE_DIR`，既避免并发 JIT 编译竞争又支持跨 session 复用。
- [`run_ray_autotune`](src/ffpa_attn/ray/_autotune_engine.py#L54-L142) 用 **Phase A 填充 + Phase B 完成即派发**实现动态负载均衡：哪个 worker 先空出来就把队列下一个任务派给同一个 actor，天然适配各任务巨大的耗时差异。
- [`ray.wait(..., num_returns=1, timeout=None)`](src/ffpa_attn/ray/_autotune_engine.py#L101) 是事件驱动派发的关键——有一个完成就立刻响应，始终把 GPU 占满。
- 两层容错：worker 内吞 OOM 转 `[]`（[worker L107-L111](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/ray/_autotune_worker.py#L107-L111)），引擎层吞 `RayActorError/RayTaskError` 记 warning 继续（[engine L107-L113](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/ray/_autotune_engine.py#L107-L113)），单个失败不拖垮整批。
- Ray 运行时是**懒加载**的：`import ffpa_attn.ray` 不触发 `import ray`，只有真正调用 `run_ray_autotune` 才加载（[ray/__init__.py:L8-L27](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/ray/__init__.py#L8-L27)），单卡用户零感知。

---

## 7. 下一步学习建议

- **回到运行时**：本讲生成的设备 JSON 如何被运行时查找与就近匹配，是 u8-l3（运行时配置查找与就近匹配回退）的主题；建议结合本讲的 entry 字段（`direction/kernel/causal/dtype/has_attn_bias/has_dropout`）重读 `_persistent_autotune.py` 的过滤逻辑。
- **基准与验证**：多卡调出 JSON 后，下一步建议学 u8-l5（基准测试 CLI 与 TFLOPS 评估），用 `python -m ffpa_attn.bench` 验证持久化配置是否真的带来了加速，并用 `FFPA_SKIP_PERSISIT_TUNED_CONFIG=1` 做开/关对照。
- **继续阅读源码**：若想扩展到其它后端（如 CuTeDSL），可仿照 `TritonAutotuneWorker` 的模式（worker 模块顶部 docstring [L15-L18](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/ray/_autotune_worker.py#L15-L18) 明确预留了这条扩展路径），新增一个 `@ray.remote(num_gpus=1)` 的 worker 类并在 `run_task` 里委托给对应后端的 tune 函数。
- **深入 Ray**：本讲的 actor pool + `ray.wait` 是 Ray 调度的经典模式；若对远程集群（`--ray-address`）感兴趣，可阅读 Ray 官方文档的 [Actor Pool 模式](https://docs.ray.io/) 对照 [engine 的 Phase A/B](src/ffpa_attn/ray/_autotune_engine.py#L90-L131) 理解。
