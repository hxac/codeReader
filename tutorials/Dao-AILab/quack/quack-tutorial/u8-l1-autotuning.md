# 自动调优（Autotuning）

## 1. 本讲目标

GEMM 的性能高度依赖「tile 形状、cluster 维度、是否 pingpong、是否动态持久化」等一组参数（`GemmConfig`）。这些参数没有解析最优解，只能在目标硬件上实测。本讲解 QuACK 的自动调优系统，学完后你应能：

- 说清 `autotune` 装饰器如何把「一组候选配置 → 实测 → 选最优 → 缓存」封装成可复用的机制；
- 跟踪一次 GEMM 调用从公共 `gemm` 入口，经 `gemm_tuned` 调优，到设备内核启动的完整路径；
- 解释 `prune_invalid_gemm_configs` 如何用 `config_supports` 与 `blockscaled_config_ok` 等结构性约束剔除非法候选，避免无谓测量；
- 理解调优结果如何在内存与磁盘两级缓存，从而「一次调优、多次复用」。

本讲依赖 u4-l2（`GemmConfig` 配置空间）与 u4-l3（公共 GEMM API 表面）。若你对 `GemmConfig` 的字段（tile_m/tile_n/cluster/pingpong/swap_ab/device_capacity 等）或 `gemm` 入口的「计划缓存热路径」还不熟，建议先复习这两讲。

## 2. 前置知识

- **配置（config）**：一组决定内核行为的标量参数。在 QuACK 里就是一个 `GemmConfig`（u4-l2）。
- **tile / cluster / pingpong / 持久化**：GEMM 把输出切成 tile，每个 CTA 算一块；cluster 让多个 CTA 协作；pingpong 让两组 warp 交替算相邻 tile 以重叠 MMA 与 epilogue；持久化内核让一个 CTA 循环算多个 tile（详见 u5 系列）。这些是配置的旋钮。
- **CUDA Graph**：把一串 kernel 启动录制成一个图，之后一次 `replay()` 重放全部，省掉每次启动的 CPU 开销。本讲用它做基准测量。
- **L2 缓存**：GPU 上比 HBM 快、比寄存器慢的一级共享缓存。若基准测试反复读同一块显存，数据会驻留 L2，测出「偏快」的假象——QuACK 用「轮转克隆输入」来规避。
- **「形状进运行期，结构进编译期」**（u4-l4）：调优选出的只是结构与几何，与具体数据指针无关，因此可按「张量元信息（dtype/shape/stride）」缓存。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [quack/autotuner.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/autotuner.py) | 调优核心：`autotune` 装饰器、`Autotuner` 类、`AutotuneConfig`、两级缓存、L2-cold 基准测量、与异步编译池的重叠。改编自 Triton 的 autotuner。 |
| [quack/gemm_interface.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_interface.py) | GEMM 公共 API 表面。含 `gemm_tuned`（被 `@autotune` 装饰的调优函数）、`prune_invalid_gemm_configs`（候选剪枝）、`gemm` 入口与其计划缓存热路径。 |
| [quack/gemm_runtime/autotune.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_runtime/autotune.py) | 面向 `@gemm_epilogue` mod 的通用调优路径 `tuned_mod_gemm`：复用 `Autotuner`，自带 `_prune_for_mod` 剪枝。是 `gemm_tuned` 的「epilogue-mod 版」。 |
| [quack/gemm_config.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_config.py) | `GemmConfig` 数据类、`get_all_configs()`（候选池）、`config_supports` / `blockscaled_config_ok`（剪枝用约束的「唯一真相源」）。 |
| [quack/bench/bench_utils.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/bench/bench_utils.py) | 基准测量协议：`_bench_cuda_graph_l2_rotate`（L2-cold CUDA-graph 轮转测量）、输入克隆与 L2 轮转计数选择。 |

---

## 4. 核心概念与源码讲解

### 4.1 autotune 装饰器与测量

#### 4.1.1 概念说明

自动调优要回答一个问题：给定一个内核函数和一组候选配置，哪个配置在当前硬件 + 当前张量形状上最快？

答案是「实测」。调优器（autotuner）做的事可概括为：

1. **构造缓存键**：从这次调用的张量参数里抽出 dtype/shape/stride 等元信息，拼成键。
2. **查缓存**：键命中 → 直接用已选出的最优配置，跳过测量。
3. **未命中 → 剪枝 → 实测**：先用结构性约束剔除必然非法或必然慢的候选，再对 survivors 逐一基准测量，取最快者。
4. **缓存结果**：把「键 → 最优配置」存进内存，可选地落盘，下次同形状直接复用。

QuACK 的 `autotuner.py` 改编自 Triton 的 autotuner，核心是 `autotune` 装饰器与 `Autotuner` 类。一个「配置」由 `AutotuneConfig` 表示——本质就是一组关键字参数（meta-parameters）。

> 关键直觉：**调优是昂贵的（要编译 + 跑很多次），但只需在每个唯一形状上做一次**。所以整个设计都在围绕「如何让命中路径极快、让冷路径只走一次」做文章。

#### 4.1.2 核心流程

`Autotuner.__call__` 是每次调优调用都走的入口，流程如下（伪代码）：

```
__call__(*args, **kwargs):
    若只有一个候选 config:  config = self.configs[0]   # 无需调优
    否则:
        key = [kwargs 里 key= 指定的那些标量值]
        对每个 Tensor 参数:  key 追加 (shape, 桶化 stride, dtype)
        若 key 不在 self.cache:                # 未命中
            pruned = prune_configs(kwargs)     # 结构性剪枝
            benchmark():                        # 在 pool_scope() 下实测
                with pool_scope() as pool:
                    queue = deque(pruned)
                    while queue 非空:
                        config = queue.popleft()
                        若该 config 之前抛过 CompilePending 且 pool 还没编好:
                            旋转到队尾, continue
                        否则: timings[config] = self._bench(..., config=config)
                        若抛 CompilePending: 记 sha, 旋转到队尾
            self.cache[key] = min(timings, key=时间)
        config = self.cache[key]
    用 config 的 kwargs 真正调用 self.fn(...)
```

几个关键点：

- **缓存键不含数据指针**，只含张量元信息——这正是「形状进运行期、结构进编译期」的体现。
- **基准测量与异步编译重叠**：冷路径下很多候选内核尚未编译。`benchmark()` 在 `pool_scope()` 里跑，未编译的 config 会抛 `CompilePending`、被旋转到队尾，等 CPU worker 编好再重试。总墙钟 ≈ max(并行编译, 串行测量)，而非二者之和。
- **GPU 热身**：测量前先 `_gpu_warmup()` 把 GPU 拉到热稳态，否则第一个 config 会因为没到功率限制而测得「虚高」的好成绩。

数学上，调优就是在一组离散候选上取最小：

\[
c^\* = \underset{c \in C_{\text{pruned}}}{\arg\min}\; T(c;\;\text{shape}, \text{硬件})
\]

其中 \(T\) 是实测延迟。缓存即 \(\text{shape} \mapsto c^\*\) 的记忆化（memoization）。

#### 4.1.3 源码精读

**装饰器入口**：`autotune` 只是把被装饰函数包进 `Autotuner`，[quack/autotuner.py:525-567](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/autotuner.py#L525-L567)：

```python
def autotune(configs, key=None, prune_configs_by=None, restore_value=None, do_bench=None, cache_results=True):
    ...
    def decorator(fn):
        return Autotuner(fn, key, configs, restore_value=restore_value,
                         prune_configs_by=prune_configs_by,
                         do_bench=do_bench, cache_results=cache_results)
    return decorator
```

注意 `cache_results` 默认 `True`（装饰器层），但 `Autotuner.__init__` 又允许用环境变量 `QUACK_CACHE_AUTOTUNING=1` 强制开启，[quack/autotuner.py:126-128](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/autotuner.py#L126-L128)。

**缓存键构造**（命中路径，每次调用都走）：从 `key=` 指定的标量 kwarg 加上每个 Tensor 的元信息拼键。stride 被「桶化」成 0/1/2 三类（stride<2 原样，否则记 2），既保留连续/转置信息又避免地址变化导致键漂移，[quack/autotuner.py:305-320](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/autotuner.py#L305-L320)：

```python
key = [kwargs[k] for k in self.keys if k in kwargs]
for arg in args:
    if isinstance(arg, Tensor):
        key.append(tuple(arg.shape))      # tuple, 非 torch.Size
        key.append(tuple([s if s < 2 else 2 for s in arg.stride()]))  # 桶化
        key.append(arg.dtype)
```

注释特意点明用 `tuple(arg.shape)` 而非 `arg.shape`：`torch.Size` 的 `str()` 与普通 tuple 不同，会无谓地让磁盘缓存失效，[quack/autotuner.py:309-313](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/autotuner.py#L309-L313)。这条命中路径被压到约 26µs/调用。

**未命中 → benchmark 内层**：在异步编译池 `pool_scope()` 里用 `deque` 轮转配置。未编译好的 config 抛 `CompilePending` 被旋转到队尾，[quack/autotuner.py:380-420](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/autotuner.py#L380-L420)：

```python
with pool_scope() as pool:
    queue = deque(pruned_configs)
    while queue:
        config = queue.popleft()
        sha = awaiting.get(id(config))
        wedged = sha is not None and time.monotonic() > deadline[id(config)]
        if sha is not None and not wedged:
            state, _ = pool.poll(sha)
            if state == "pending":
                queue.append(config)        # 还没编好，旋转到队尾
                continue
        try:
            timings[config] = self._bench(*args, config=config, **kwargs)
        except CompilePending as e:
            awaiting[id(config)] = e.sha
            queue.append(config)            # 冷编译，旋转
```

为防止某个 sha 永远「pending」（worker 卡死或他人持锁不放）让循环无限旋转，设了 `_POOL_WEDGE_TIMEOUT_S=300s` 的楔死超时——到点后用 `suppress_pool()` 在进程内编译，保证扫描必终止，[quack/autotuner.py:66](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/autotuner.py#L66) 与 [quack/autotuner.py:403-409](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/autotuner.py#L403-L409)。

**测量本身（`_bench`）**：默认走「L2-cold CUDA-graph 轮转」路径。先用 `_gpu_warmup` 拉到热稳态，再预克隆若干组输入做 L2 轮转，最后在 CUDA Graph 里重放若干次取 ms/次，[quack/autotuner.py:207-223](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/autotuner.py#L207-L223)：

```python
if use_l2_cold:
    try:
        return _bench_cuda_graph_l2_rotate(
            self.fn, l2_cold_arg_sets, l2_cold_kwarg_sets,
            extra_kwargs=config.all_kwargs(), quantiles=(0.5, 0.2, 0.8))
    except (RuntimeError, MemoryError) as e:
        return [float("inf"), float("inf"), float("inf")]   # smem 溢出等 → 无穷大
```

注意这个 `except`：只吞 GPU 侧失败（smem 溢出、启动错误、OOM），把它们记为 `inf`（于是该 config 自然落选）；编程错误（TypeError/AssertionError）继续上抛让用户看到，[quack/autotuner.py:216-223](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/autotuner.py#L216-L223)。测量函数 `_bench_cuda_graph_l2_rotate` 的核心思想是「轮转克隆输入击败 L2 复用」，让测得的延迟贴近 L2-cold 的生产场景，[quack/bench/bench_utils.py:69-108](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/bench/bench_utils.py#L69-L108)。

**取最优并缓存**：`builtins.min(timings, key=timings.get)` 选最快 config 存进内存 `self.cache`，[quack/autotuner.py:441-442](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/autotuner.py#L441-L442)。

**磁盘缓存（可选）**：当 `cache_results` 为真，`check_disk_cache` 用 `FileCacheManager`（路径默认 `~/.quack/cache/<key>/`，可被 `QUACK_CACHE_DIR` 覆盖）读写 `<fn>.autotune.json`。命中时直接读回 `configs_timings` 并选最优，无需再测量，[quack/autotuner.py:255-294](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/autotuner.py#L255-L294)。磁盘键由 `VERSION + tuning_key + 各 config 的 str` 做 sha256，[quack/autotuner.py:264-266](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/autotuner.py#L264-L266)。相关环境变量：`QUACK_PRINT_AUTOTUNING=1` 打印调优过程与最优 config，`QUACK_FORCE_CACHE_UPDATE` 强制重测。

**配置对象**：`AutotuneConfig` 包装一组 kwarg，按 `tuple(all_kwargs().items())` 提供 `__hash__`/`__eq__`，从而能作字典键，[quack/autotuner.py:516-522](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/autotuner.py#L516-L522)。

#### 4.1.4 代码实践

**实践目标**：观察「第一次调用触发调优、第二次命中缓存」的行为，理解测量与缓存机制。

**操作步骤**（源码阅读型，无需 GPU 也可完成前两步）：

1. 打开 [quack/autotuner.py:296-320](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/autotuner.py#L296-L320)，手动跟踪：若 `gemm_tuned` 的 `key=["dynamic_scheduler","split_k","split_k_mode","bs_format_a","bs_format_b"]`，调用时这 5 个 kwarg 的值 + A/B/out 等 Tensor 的 (shape, 桶化 stride, dtype) 会如何拼成 `key`。
2. 阅读 [tests/test_autotuner.py:25-75](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/tests/test_autotuner.py#L25-L75)：该测试用一个 `_StubPool`（poll 第一次返回 pending、第二次返回 done）和一个会抛 `CompilePending` 的「冷 config」验证「延迟重试」循环。
3.（需 GPU）运行下面的脚本，观察首调用的扫描日志：

```python
# 示例代码（非项目自带，需 GPU 与已编译内核）
import os, torch, quack
os.environ["QUACK_PRINT_AUTOTUNING"] = "1"
A = torch.randn(2048, 2048, dtype=torch.bfloat16, device="cuda")
B = torch.randn(2048, 2048, dtype=torch.bfloat16, device="cuda")
quack.gemm(A, B)   # 第一次：触发调优，打印 "finished after ...s; best config ..."
quack.gemm(A, B)   # 第二次：命中缓存，无调优日志
```

**需要观察的现象**：第一次调用会打印调优耗时与最优 config；第二次调用静默（命中 `self.cache` 内存缓存）。

**预期结果**：第一次出现 `quack autotuning for function gemm_tuned finished after <秒>s; best config selected: ...`，第二次无此行。

> 待本地验证：实际调优耗时与候选数取决于你的 GPU 架构（SM90/100/120）与 `.o` 缓存是否已热。

#### 4.1.5 小练习与答案

**练习 1**：为什么缓存键要把 stride「桶化」成 0/1/2，而不是直接用真实 stride？

**参考答案**：真实 stride 会因张量在显存中的基地址不同而变化（同一逻辑形状、不同分配，stride 数值可能不同），导致键无谓漂移、缓存无法命中。桶化成 0/1/2 只保留「是否为 0（广播）、是否为 1（连续）、是否更大（转置/跳步）」这三种对内核行为有意义的类别，既区分了连续与转置布局，又让同布局的不同分配共享缓存。

**练习 2**：`benchmark()` 里 `deque` 把抛 `CompilePending` 的 config 旋转到队尾，而不是直接编译它。这样设计的好处是什么？

**参考答案**：编译交由后台 CPU worker 池（`pool_scope`）并行完成，主线程 meanwhile 去测量其它已就绪的 config。于是总墙钟 ≈ max(并行编译, 串行测量)，而非「先编完所有、再逐个测」的串行和。旋转+轮询只是让未就绪者排队等自己的 `.o` 落地。

---

### 4.2 gemm_tuned 调优路径

#### 4.2.1 概念说明

`Autotuner` 是通用机器，但它调优的「函数」是什么？对 GEMM 而言就是 `gemm_tuned`——一个被 `@autotune` 装饰的函数，其职责是：给定一组张量和一个 `config`，按该 config 的 tile/cluster 等参数真正启动 GEMM，并返回「解析后的决策」（config、split_k、动态调度标志、dispatch plan）。

注意一个微妙之处：`gemm_tuned` 的**返回值不是计算结果**（结果写进了输出张量 `out`），而是**决策元组** `(config, split_k, dynamic_scheduler, dispatch_plan)`。这是因为公共入口 `gemm` 会把这个决策烘焙进自己的「接口计划缓存」，于是后续同形状调用连调优函数都不必再进——直接重放 plan。

还有一条平行路径 `tuned_mod_gemm`（在 `gemm_runtime/autotune.py`），面向 `@gemm_epilogue` 创作的 epilogue mod。它复用同一套 `Autotuner` 机制，只是候选空间与剪枝规则不同。

#### 4.2.2 核心流程

从用户视角到内核启动的完整链路：

```
quack.gemm(A, B, out=None, ...)                  # 公共入口（u4-l3）
  ├── 若 plan_key 命中 _gemm_iface_plan_cache:    # 热路径
  │     _gemm_execute(..., config=plan.config, dispatch_plan=plan.dispatch_plan)
  │     return out                                 # 不进调优
  └── 否则（冷路径）:
        gemm_out / gemm_quant_out  (custom op)
          └── fn = gemm_tuned  (若 tuned=True)
                gemm_tuned(A, B, out, ..., config=None)
                  ├── @autotune 装饰器: 查/测/缓存选最优 config
                  ├── 若 config is None: 选默认 (blockscaled_default_config /
                  │     nvmmh_config / default_config)
                  └── _gemm_execute(...)           # 变换操作数 + 启动
                        └── gemm_dispatch(...)  == quack.gemm.gemm  (设备内核)
        返回的 (config, split_k, dyn, plan) 被 gemm 记录进 _gemm_iface_plan_cache
```

两个层次的「选 config」要分清：

- **调优层**（`@autotune`）：当 `config=None` 且有多种候选时，按形状实测选最优。代价高，每个形状只做一次。
- **默认层**（`config is None` 分支）：若 `gemm_tuned` 被直接调用且未走实测（如 `tuned=False` 时 `partial(gemm_tuned.fn, config=None)`），则在函数体内用启发式选一个默认 config。

#### 4.2.3 源码精读

**`gemm_tuned` 的装饰**：候选池是 `get_all_configs()` 全部配置，每个包成 `AutotuneConfig(config=c)`；`key` 只含少量标量（动态调度、split_k、量化格式名等），张量元信息由 `Autotuner.__call__` 自动补进键；剪枝函数是 `prune_invalid_gemm_configs`，[quack/gemm_interface.py:465-470](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_interface.py#L465-L470)：

```python
@autotune(
    configs=[AutotuneConfig(config=c) for c in get_all_configs()],
    key=["dynamic_scheduler", "split_k", "split_k_mode", "bs_format_a", "bs_format_b"],
    prune_configs_by={"early_config_prune": prune_invalid_gemm_configs},
)
def gemm_tuned(A, B, out, C=None, bias=None, ...):
```

**默认 config 的选取**：函数体内，`config is None` 时按场景选默认——blockscaled 用 `blockscaled_default_config`，纯 GEMM 用 nvMMH 启发式 `nvmmh_config`，否则 `default_config`，[quack/gemm_interface.py:509-532](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_interface.py#L509-L532)。注意 `@autotune` 装饰器会在 `config` 未由调用方显式给出时（候选>1）先实测选优，再进入函数体——所以这里的 `config is None` 分支主要服务 `tuned=False` 的直调路径。

**调用 `_gemm_execute` 启动**：`gemm_tuned` 把决策解析后交给 `_gemm_execute`，后者变换操作数（批次维、swap_ab、concat）并最终调用 `gemm_dispatch`（即 `quack.gemm.gemm`），[quack/gemm_interface.py:573-603](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_interface.py#L573-L603)。返回值是决策元组而非结果张量：

```python
return config, split_k, dynamic_scheduler, dispatch_plan
```

**谁调用 `gemm_tuned`**：`gemm_out`（torch.compile 用的 custom op）里 `fn = gemm_tuned if tuned else partial(gemm_tuned.fn, config=None)`，[quack/gemm_interface.py:1350](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_interface.py#L1350)。`tuned=False` 时取 `.fn`（裸函数，绕过 `@autotune`）并固定 `config=None`，即走默认 config、不调优。

**公共 `gemm` 的接口计划缓存**：`gemm` 在 `plan_key`（含 tensor_key、各 mode、`tuned`、`scalar_mode` 等）命中时，直接用烘焙好的 `plan.config` 与 `plan.dispatch_plan` 调 `_gemm_execute`，完全跳过调优与分发层，[quack/gemm_interface.py:1120-1153](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_interface.py#L1120-L1153)。这意味着调优只发生在每个形状的首次调用；后续同形状调用连 `gemm_tuned` 都不进。注释说明 plan_key 必须涵盖慢路径决策所读的一切（含 alpha/sr 模式），[quack/gemm_interface.py:1084-1099](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_interface.py#L1084-L1099)。

**`_launch` 绕过分发边界**：eager 调用下，custom-op 边界（autograd 包装 + 别名检查）每次约 85µs，只在 torch.compile 下才划算。`_launch` 在非编译时取裸实现 `_init_fn`，[quack/gemm_interface.py:172-179](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_interface.py#L172-L179)。

**平行路径 `tuned_mod_gemm`**：面向 `@gemm_epilogue` mod。它为每个 `(mod 语义指纹, epi-arg 名集, 是否有 C, 架构, transform)` 缓存一个 `Autotuner` 实例（`_get_tuner`），候选空间用 `_config_space`（按架构过滤、剔除 split_k 与 use_tma_gather），剪枝用 `_prune_for_mod`，[quack/gemm_runtime/autotune.py:352-379](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_runtime/autotune.py#L352-L379)。它的模块 docstring 把这套「任意 mod 都免费获得调优」的设计讲得很清楚，[quack/gemm_runtime/autotune.py:1-42](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_runtime/autotune.py#L1-L42)。

#### 4.2.4 代码实践

**实践目标**：跟踪一次 `quack.gemm` 调用，看清「热路径（plan 命中）」与「冷路径（进 `gemm_tuned` 调优）」的分岔。

**操作步骤**（源码阅读型）：

1. 从 [quack/gemm_interface.py:974](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_interface.py#L974) 的 `def gemm(...)` 入手，找到 `plan_key` 的构造（约 1099 行）与命中分支（约 1120 行）。
2. 沿冷路径追：`gemm` → `gemm_out`（约 1319 行）→ `fn = gemm_tuned`（1350 行）→ `@autotune` 装饰器 → `Autotuner.__call__` → `gemm_tuned` 函数体 → `_gemm_execute`（约 606 行）→ `gemm_dispatch`（755 行）。
3. 在笔记上画一张分岔图：标注哪条路径会触发调优、哪条路径只重放 plan。

**需要观察的现象**：理解为何同一个 `(A.shape, B.shape, dtype)` 第二次调用会快得多——因为 `plan_key` 命中、跳过了 `gemm_tuned` 与分发层。

**预期结果**：你能用一句话说明「调优只发生在冷路径首调；热路径完全不进 `gemm_tuned`，而是用 `_gemm_iface_plan_cache` 里烘焙的 `config` 直接调 `_gemm_execute`」。

#### 4.2.5 小练习与答案

**练习 1**：`gemm_tuned` 的返回值为什么不是计算结果张量，而是 `(config, split_k, dynamic_scheduler, dispatch_plan)` 元组？

**参考答案**：计算结果已就地写进输出张量 `out`。返回决策元组是为了让公共 `gemm` 把这些决策烘焙进 `_gemm_iface_plan_cache`：下次同形状调用时，直接用缓存的 `config` 与 `dispatch_plan` 调 `_gemm_execute`，彻底跳过调优与分发层。若返回结果张量，调用方就无法把「决策」记忆化，每次都得重走调优。

**练习 2**：`tuned=False` 时 `gemm_out` 用 `partial(gemm_tuned.fn, config=None)`。这里取 `.fn` 而非 `gemm_tuned` 本身，意义何在？

**参考答案**：`gemm_tuned` 已被 `@autotune` 包成 `Autotuner` 实例；调用它会触发实测调优。取 `.fn` 拿到的是被装饰前的裸函数，绕过 `Autotuner.__call__`，再固定 `config=None`，于是走函数体内的「默认 config 选取」分支（nvMMH/default_config），即「不调优、用启发式默认」。这是关闭调优的开关。

---

### 4.3 prune_invalid_gemm_configs 剪枝

#### 4.3.1 概念说明

`get_all_configs()` 返回的是 SM80/90/100/120 四种架构的**全部**候选，动辄数百上千个。但每次调用只用得上一小撮：

- 当前 GPU 只是一种架构，其它架构的 config 必然不能用；
- blockscaled（量化）GEMM 有额外的硬件约束（如 SF tmem 64-N 颗粒）；
- gather_A / varlen 与 swap_ab、cluster_n 等互斥；
- 量化输出（SFD）对 tile 形状有苛刻要求。

「剪枝」（prune）就是用这些**结构性、确定性的约束**，在实测之前先剔除必然非法或必然失败的候选，避免把宝贵的测量时间浪费在注定 `inf` 的 config 上。

QuACK 用两级剪枝钩子（`Autotuner.prune_configs` 支持）：

- `early_config_prune`：在测量前的「早剪」，`gemm_tuned` 用的是 `prune_invalid_gemm_configs`；
- `perf_model` + `top_k`（可选）：用性能模型预测、只测 top_k 个——QuACK 的 GEMM 路径未用，留作扩展。

约束函数本身集中在 `gemm_config.py`，被剪枝路径与解析启发式（`gemm_heuristic`）**共享**，是「唯一真相源」（single source of truth）。

#### 4.3.2 核心流程

`prune_invalid_gemm_configs(configs, named_args, **kwargs)` 作为 `early_config_prune`，按顺序套用一系列过滤器：

```
prune_invalid_gemm_configs(configs, named_args, **kwargs):
    cap = 当前 GPU 架构号
    configs = [c for c in configs if c.device_capacity == cap]   # 1. 架构过滤
    configs = [c for c in configs if config_supports(c, gather_A, varlen_m)]  # 2. 结构合法
    若非 (gather_A 且 SM100/110):
        configs = [c for c in configs if not c.use_tma_gather]    # 3. TMA gather 限制
    若有 SFA (blockscaled):
        configs = [c for c in configs if blockscaled_config_ok(c)]  # 4. 量化约束
    若有 SFD/SFDCol (量化输出):
        configs = [c for c in configs if _sfd_ok(c)]             # 5. 输出量化约束
    若 split_k is None 且非 varlen/gather 且架构支持:
        configs = _expand_split_k_configs(...)                    # 6. 扩展 split-k 变体
    return configs
```

每一步都只剔除、不增加（第 6 步除外，它是在 survivors 上追加 split-k 变体）。第 6 步特殊：仅当用户传 `split_k=None`（「让调优器选因子」）时，才为「占用率不足」的形状追加 split_k∈{2,4,8,16} 的变体让调优器实测择优。

#### 4.3.3 源码精读

**剪枝主体**：[quack/gemm_interface.py:399-462](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_interface.py#L399-L462)。架构过滤与 `config_supports` 过滤：

```python
device_capacity = get_device_capacity(kwargs["A"].device)[0]
configs = [conf for conf in configs if conf.kwargs["config"].device_capacity == device_capacity]
gather_A = kwargs.get("A_idx", None) is not None
varlen_m = kwargs.get("cu_seqlens_m", None) is not None
configs = [conf for conf in configs
           if config_supports(conf.kwargs["config"], gather_A=gather_A, varlen_m=varlen_m)]
```

`config_supports` 的判定，[quack/gemm_interface.py:406-410](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_interface.py#L406-L410)。blockscaled 过滤直接调 `blockscaled_config_ok`，[quack/gemm_interface.py:414-415](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_interface.py#L414-L415)。

**`config_supports`（gemm_config.py）—— gather/varlen 结构合法性**：gather_A/varlen 与 swap_ab 互斥；gather_A 要求 cluster_n==1；SM90 上 gather 还排除 tile_n==208 与动态持久化，[quack/gemm_config.py:98-112](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_config.py#L98-L112)：

```python
def config_supports(config, *, gather_A=False, varlen_m=False) -> bool:
    if (gather_A or varlen_m) and config.swap_ab:
        return False
    if gather_A:
        if config.cluster_n != 1:
            return False
        if config.device_capacity == 9 and (config.tile_n == 208 or config.is_dynamic_persistent):
            return False
    return True
```

docstring 明说这是「被 autotune 剪枝器与解析启发式共享的唯一真相源」，[quack/gemm_config.py:98-104](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_config.py#L98-L104)。

**`blockscaled_config_ok`（gemm_config.py）—— 量化合法性**：SM120（warp MMA）要求 tile_m/tile_n ∈ (128,256)、无 swap_ab、tile_k 为 None；SM100（tcgen05）额外要求 tile_n 是 64 的倍数且 64≤tile_n≤256（因 SF tmem 数据通路 64-N 颗粒）、cluster 各维 ≤4（SF 多播上限），[quack/gemm_config.py:71-95](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_config.py#L71-L95)：

```python
def blockscaled_config_ok(c) -> bool:
    """...THE single statement of the constraint set — both autotune prune paths call this."""
    if c.device_capacity == 12:
        return (not c.swap_ab and c.tile_k is None
                and c.tile_m in (128, 256) and c.tile_n in (128, 256))
    return (c.device_capacity in (10, 11) and not c.swap_ab and c.tile_k is None
            and c.tile_m in (128, 256)
            and c.tile_n % 64 == 0 and 64 <= c.tile_n <= 256
            and c.cluster_m <= 4 and c.cluster_n <= 4)
```

注意 docstring 强调「both autotune prune paths call this」——`gemm_tuned` 走 `prune_invalid_gemm_configs`，epilogue-mod 走 `_prune_for_mod`，二者都最终调用此函数。

**SM100 没有 pingpong**：`_get_sm100_configs` 用 `partial(GemmConfig, pingpong=False, ...)` 固定 `pingpong=False`，[quack/gemm_config.py:212-214](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_config.py#L212-L214)。这是 u4-l2 已讲过的结论：Blackwell 的 tcgen05 MMA 把累加器放进 TMEM，MMA↔epilogue 重叠由硬件原生提供，无需用 pingpong 软件重叠。

**split-k 变体扩展**：仅当 `split_k=None`（让调优器选）且形状「占用率不足」（tile 数 < SM 数）时，追加 split_k∈{2,4,8,16} 让调优器实测择优，[quack/gemm_interface.py:451-461](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_interface.py#L451-L461) 与 `_expand_split_k_configs` [quack/gemm_interface.py:371-396](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_interface.py#L371-L396)。

**epilogue-mod 平行剪枝 `_prune_for_mod`**：与 `prune_invalid_gemm_configs` 类似，但额外处理 mod 的 reduce-sink 缓冲区大小、transform_a 的 `config_ok`、gated 模式的 tile_n 偶数约束等，[quack/gemm_runtime/autotune.py:162-215](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_runtime/autotune.py#L162-L215)。它同样调用 `config_supports`（[182 行](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_runtime/autotune.py#L182)）与 `blockscaled_config_ok`（[189 行](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_runtime/autotune.py#L189)）。

#### 4.3.4 代码实践

**实践目标**：用一个具体的非法 config，手动走一遍 `config_supports` 与 `blockscaled_config_ok`，验证剪枝确实剔除了它，并理解结果如何被缓存避免重复测量。

**操作步骤**（纯源码阅读 + 手算，无需 GPU）：

1. 设想一次 blockscaled GEMM 调用（即 `kwargs["SFA"] is not None`），架构为 SM100（`device_capacity=10`），候选含这样一个 config：
   `GemmConfig(tile_m=128, tile_n=96, cluster_m=2, cluster_n=1, swap_ab=False, device_capacity=10)`。
2. 在 [quack/gemm_config.py:71-95](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_config.py#L71-L95) 手算 `blockscaled_config_ok` 对它的返回值。
3. 再取一个 `swap_ab=True` 的 config，先过 `config_supports`（非 gather/varlen，应返回 True），再过 `blockscaled_config_ok`，看是否被第 4 步剔除。
4. 在 [quack/autotuner.py:255-294](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/autotuner.py#L255-L294) 阅读磁盘缓存：理解首次测量后，`configs_timings` 被写进 `<fn>.autotune.json`；下次同 `(VERSION, tuning_key, configs)` 直接读回最优、不再测量。

**需要观察的现象 / 手算预期**：

- 对 `tile_n=96` 的 SM100 config：`blockscaled_config_ok` 中 `c.tile_n % 64 == 0` 即 `96 % 64 == 32 ≠ 0`，返回 **False** → 被剪除。这正是 docstring 所说「SF tmem 数据通路 64-N 颗粒；tile_n 为 32 的倍数但非 64（如 96、224）会被硬件阻断」。
- 对 `swap_ab=True` 的 config：`config_supports`（非 gather/varlen）返回 True，但 `blockscaled_config_ok` 因 `not c.swap_ab` 为 False → 被 blockscaled 分支剪除（量化与 swap 组合未经测试）。

**预期结果**：你能解释「剪枝是确定性的结构过滤，发生在实测之前；被剪的 config 永不进 `timings` 字典；survivors 实测后，最优 config 与全部 timings 一起缓存到磁盘，下次同形状直接读回，零测量」。

> 待本地验证：可在 Python 里 `from quack.gemm_config import blockscaled_config_ok, config_supports, GemmConfig` 构造上述 config 实际调用，核对返回值。

#### 4.3.5 小练习与答案

**练习 1**：一个 `device_capacity=9`（SM90）的 config 出现在候选池里，但当前 GPU 是 SM100。它会在哪一步、被哪个函数剔除？

**参考答案**：在 `prune_invalid_gemm_configs` 的第一步「架构过滤」被剔除：`conf.kwargs["config"].device_capacity == device_capacity` 对 `9 == 10` 为 False。这一步在 `config_supports` 与 `blockscaled_config_ok` 之前，先用最廉价的整数比较砍掉绝大多数异架构候选。

**练习 2**：为什么 `blockscaled_config_ok` 要写成「唯一真相源」，让两条剪枝路径都调用它，而不是各自内联判定？

**参考答案**：量化约束（如 SF tmem 64-N 颗粒、cluster 维 ≤4、tile_k 必须为 None）是硬件事实，与走哪条 API 路径无关。集中到一处可避免「`gemm_tuned` 路径剔了、`tuned_mod_gemm` 路径忘了剔」的不一致——那样某个非法 config 会在一条路径上被正确剪除、在另一条路径上漏网并测出 `inf` 甚至崩溃。共享函数 = 单一真相源 = 不会漂移。

**练习 3**：`_expand_split_k_configs` 只在 `split_k=None` 时触发。若用户显式传 `split_k=4`，会发生什么？

**参考答案**：不会扩展——显式整数表示「用户已决定因子」，无需调优器选择，函数体里 `split_k = config.split_k` 直接用该值。扩展仅在 `split_k=None`（「让调优器选」）时，为占用率不足的形状追加 {2,4,8,16} 变体供实测择优（见 [quack/gemm_interface.py:451-461](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_interface.py#L451-L461)）。

---

## 5. 综合实践

把三个最小模块串起来：**解释一次 blockscaled GEMM 的「剪枝 → 实测 → 缓存」全过程，并验证缓存命中**。

**任务**：

1. **剪枝追踪**：设你在 SM100 上调用 `quack.gemm(A, B)`，其中 A、B 是 `BlockScaledOperand`（MXFP8）。从 [quack/gemm_interface.py:399](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_interface.py#L399) 的 `prune_invalid_gemm_configs` 入手，列出一次这样的调用会让候选池依次经过哪几道过滤，并指出 `blockscaled_config_ok` 在 [quack/gemm_config.py:71-95](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_config.py#L71-L95) 砍掉了哪类 tile_n（答：非 64 倍数的，如 96、160、224，以及 >256 的 512）。

2. **实测与异步编译重叠**：在 [quack/autotuner.py:380-420](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/autotuner.py#L380-L420) 解释：survivors 里若有 config 的内核尚未编译，`CompilePending` 如何让它旋转到队尾、由 CPU worker 池并行编译，而主线程 meanwhile 测量已就绪的 config。说明为何总墙钟 ≈ max(并行编译, 串行测量)。

3. **缓存验证**（需 GPU）：设 `QUACK_PRINT_AUTOTUNING=1`，对同一对 (A, B) 连续调用两次 `quack.gemm(A, B)`：
   - 第一次应打印调优日志（`finished after ...s; best config ...`）；
   - 第二次应静默——因为 `gemm` 的 `_gemm_iface_plan_cache`（[quack/gemm_interface.py:1120](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_interface.py#L1120)）命中，连 `gemm_tuned` 都不进。
   - 再删掉内存缓存（重启进程），若 `cache_results=True` 已把 timings 落盘到 `~/.quack/cache/`，则新进程首次调用仍可不重新测量而直接读回最优（验证 `check_disk_cache`，[quack/autotuner.py:255-278](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/autotuner.py#L255-L278)）。

**交付物**：一段说明文字 +（可选）两次调用的日志对比，证明「剪枝剔除非法 config、实测选最优、结果两级缓存避免重复测量」。

> 待本地验证：步骤 3 依赖 GPU 与冷/热 `.o` 缓存状态；无 GPU 时完成步骤 1、2 的源码追踪即可。

## 6. 本讲小结

- **`autotune` 装饰器**把任意函数包成 `Autotuner`：按张量元信息（dtype/shape/桶化 stride）构造缓存键，命中即用，未命中则剪枝 + 实测选最优，键不含数据指针。
- **测量用 L2-cold CUDA-graph 轮转**：预克隆多组输入轮转击败 L2 复用，`_gpu_warmup` 拉到热稳态；GPU 侧失败（smem 溢出等）记 `inf` 自然落选，编程错误上抛。
- **冷编译与测量重叠**：未编译 config 抛 `CompilePending` 旋转到队尾，由 CPU worker 池并行编译，楔死超时兜底；总墙钟 ≈ max(编译, 测量)。
- **`gemm_tuned` 是被调优的函数**，返回决策元组而非结果；公共 `gemm` 把决策烘焙进 `_gemm_iface_plan_cache`，热路径连调优函数都不进。
- **`prune_invalid_gemm_configs`** 用架构过滤、`config_supports`、`blockscaled_config_ok`、`_sfd_ok` 等结构性约束在实测前剔除非法候选；约束函数集中在 `gemm_config.py` 作「唯一真相源」，两条剪枝路径共享。
- **结果两级缓存**：内存 `self.cache` 字典（进程内）+ 磁盘 `<fn>.autotune.json`（跨进程，键含 `VERSION` 与源码指纹相关量），由 `QUACK_CACHE_AUTOTUNING` / `QUACK_CACHE_DIR` / `QUACK_FORCE_CACHE_UPDATE` 等环境变量调控。

## 7. 下一步学习建议

- **u8-l2（.o JIT 缓存与异步编译池）**：本讲的 `pool_scope` / `CompilePending` / 楔死超时都依赖 `quack/cache/`。下一讲深入 `jit_cache` 的两级缓存与 `async_compile` 的多 worker 编译池，是理解「冷编译如何与测量/测试重叠」的钥匙。
- **u8-l3（Split-K 归约）**：本讲提到的 `_expand_split_k_configs` 与 `SplitKMode`（SERIAL/PARALLEL/SEPARATE）在下一讲展开，讲清三种合并模式的确定性与延迟取舍。
- **延伸阅读**：对照 [quack/gemm_runtime/autotune.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_runtime/autotune.py) 的 `tuned_mod_gemm`，体会「同一套 Autotuner 机器如何服务任意 `@gemm_epilogue` mod」；并阅读 [tools/matmul_heuristic/common.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/tools/matmul_heuristic/common.py)（若存在）了解基准测量协议在工程实践中的注意事项（AGENTS.md「Benchmarking」一节有总结）。
