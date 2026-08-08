# .o JIT 缓存与异步编译池

## 1. 本讲目标

本讲是 [u2-l6](u2-l6-cute-op-and-jit-cache.md)「cute_op 自定义算子与编译缓存」的进阶篇。u2-l6 已经建立了 `.o` 持久化缓存的**基本心智模型**：内存字典 + 磁盘 `.o` 的两级缓存、共享锁热路径、独占锁冷路径、源码指纹自动失效。

本讲把视角抬高，回答一个更具工程性的问题：

> 当**很多进程同时编译**（CI、pytest-xdist 多卡、autotune 扫描）、或**同一时刻有很多冷内核**（参数化测试爆炸）时，这套缓存如何既**正确**（不重复编译、不被半成品文件毒死）又**快**（冷编译并行化，不阻塞测试线程）？

学完后你应当掌握：

1. `jit_cache` 在并发与崩溃下的**不变量**：为何编译必须在独占锁内、损坏的 `.o` 如何被隔离、写入为何要原子 rename。
2. `async_compile` 编译池的**defer-and-retry** 模型：`CompilePending` 信号、forkserver sidecar、GPU-blind worker、`.o` 作为唯一会合点。
3. 控制「缓存是否有效」与「worker 编译目标」的**环境变量族**，以及 `_compute_source_fingerprint` 为何要哈希整个 `quack` 包。

---

## 2. 前置知识

本讲假设你已读过 u2-l6，知道下列术语：`.o` 对象文件、`cute.compile`/`export_to_c`、`jit_cache` 装饰器、内存/磁盘两级缓存、源码指纹。若没有，请先回到 u2-l6。

几个本讲会用到的并发/系统概念，先用大白话解释：

- **文件锁（`flock`）**：操作系统提供的「占用文件」机制。共享锁（`LOCK_SH`）允许多个进程同时读；独占锁（`LOCK_EX`）只允许一个进程持有，别人必须等。本讲的缓存用它来协调「谁能编译这个 key」。
- **咨询锁（advisory lock）**：`flock` 不会阻止别的进程硬读这个文件，只有「也遵守 flock 礼仪」的进程才会被挡住。QuACK 的所有路径都遵守，所以够用。
- **原子 rename（`os.replace`）**：把临时文件改名成正式文件这一步在操作系统层面是「瞬间完成」的，观察者要么看到旧文件、要么看到完整新文件，不会看到「写一半」的中间态。
- **forkserver**：Python 多进程的一种启动方式。先拉起一个「预热进程」把昂贵的库（torch/cutlass）导入一次，之后每个 worker 从它 `fork()` 出来，靠写时复制继承已导入的状态，启动成本从秒级降到亚秒级。PyTorch Inductor 的编译子进程池用的就是这套架构。
- **会合点（rendezvous）**：两个独立进程/线程约定「在哪里交换结果」。本讲里 worker 与测试线程不直接传对象，而是约定「`<sha>.o` 出现了就算完成」。

---

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [quack/cache/\_\_init\_\_.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/cache/__init__.py) | 包入口。定义三个运行期开关（`CACHE_ENABLED`/`CACHE_DIR`/`EXTRA_SOURCE_DIRS`），**必须在子模块导入之前**定义；再从子模块重导出公开 API。 |
| [quack/cache/jit.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/cache/jit.py) | `jit_cache` 装饰器、`FileLock`、`get_cache_path`、`_compute_source_fingerprint`、`_key_to_hash`。两级缓存与文件锁的全部逻辑。 |
| [quack/cache/async_compile.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/cache/async_compile.py) | `CompilePool`、`CompilePending`、`_pool_worker`、forkserver 构造、GPU-blind worker 初始化、`pool_scope`/`suppress_pool`。异步编译池的全部逻辑。 |
| [quack/cache/\_pool_preload.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/cache/_pool_preload.py) | forkserver 预热模块：在 forkserver 进程里一次性导入 torch/cutlass 并钉定架构环境变量。 |
| [quack/testing/pytest_plugin.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/testing/pytest_plugin.py) | `--async-compile` 插件。单进程与 xdist 两种 defer-and-retry 主循环，消费 `CompilePending`。 |
| [quack/autotuner.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/autotuner.py) | 自动调优的 bench 循环用 `pool_scope()` 把候选配置的编译与测量重叠。 |
| [tests/test_cache.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/tests/test_cache.py) | `quack.cache` 的回归测试，含「N 进程竞争同一冷 key 只编译一次」的并发正确性测试。 |

---

## 4. 核心概念与源码讲解

### 4.1 jit_cache 两级缓存与文件锁：并发不变量与鲁棒性

#### 4.1.1 概念说明

u2-l6 讲过 `jit_cache` 是「内存字典 + 磁盘 `.o`」两级缓存。本节不再重复两级缓存的基本概念，而是回答一个更尖锐的问题：**当多个进程同时冷启动同一个 key 时，为什么这套设计既不会重复编译、也不会读到写坏的文件？**

这里有三条核心不变量，是整个缓存正确性的根基：

1. **「锁内编译」防重复（lock-before-compile）**：真正调用 `cute.compile` 必须在独占锁内。N 个进程竞争同一个冷 key 时，只有 1 个进程编译，其余 N−1 个等锁、看到 `.o` 出现后直接加载。
2. **「锁内复查」防竞态**：拿到独占锁后，先重新检查 `.o` 是否已存在——因为在等锁期间可能正好有别的进程把它编译出来了。复查命中就直接加载，不重复编译。
3. **「临时文件 + 原子 rename」防半成品**：编译产物先写到 `.o.tmp.<pid>`，确认成功后再 `os.replace` 改名成正式 `.o`。这样即使进程在写一半时被杀（OOM、超时），也不会留下残缺的 `.o`。

这三条共同保证：磁盘上的 `.o` 要么完整可用、要么不存在，绝不会出现「写一半的损坏文件」。

> 为什么不能「先无锁编译、再独占锁导出」？这正是历史上的 bug。旧的顺序是：共享锁检查磁盘 → 缺失就释放共享锁、**无锁**编译 → 再拿独占锁导出。在 N 进程同时冷启动时，N 个进程会**并行编译同一个 key**——墙钟时间虽然不变（都差不多同时编完），但编译占用的 CPU 随 N 线性放大，在「同时有很多冷 key」（CI 冷启）时会饿死其他编译。修复后编译体在独占锁内，N−1 个进程纯等。

#### 4.1.2 核心流程

`jit_cache.wrapper` 的完整判断顺序（u2-l6 给过骨架，这里突出并发鲁棒性）：

```
wrapper(*args, **kwargs):
  cache_key = args + sorted(kwargs)
  1. 内存字典命中?            ──yes──> 返回 cache[key]      （零开销，进程内）
  2. CACHE_ENABLED 关?        ──yes──> 进程内编译，仅存内存，不落盘
  ── 算 sha、cache_path、o_path、lock_path ──
  3. o_path 存在?
     └ 共享锁内 load_module
        └ 加载成功 ──> 存内存、返回          （热路径 ~1ms，多读者并发）
        └ 加载失败(损坏) ─> 删除 .o 当 miss   （损坏隔离）
        └ 锁超时      ─> 落到慢路径
  3b. 有异步编译池?           ──yes──> 投递任务 / 抛 CompilePending（见 4.2）
  4. 慢路径：拿独占锁（带 60s 超时）
     └ 锁超时 ─> 退化为进程内编译、不落盘（宁可编两次也不让测试挂）
     └ 锁内复查 o_path ──yes──> 加载返回   （防竞态：等锁期间别人编好了）
     └ misses++; compiled = fn(*args)       （真正编译 ~500ms，被独占锁串行化）
     └ export_to_c 写 .o.tmp.<pid> → os.replace 改名（防半成品）
     └ 存内存、返回
```

并发语义可总结成一张真值表：

| 场景 | 加锁方式 | 编译次数 | 原因 |
|------|----------|----------|------|
| 内存命中 | 无锁 | 0 | 进程内字典 |
| 磁盘命中（热） | 共享锁，仅加载 | 0 | 多读者并发安全 |
| 冷启，单进程 | 独占锁 | 1 | 正常 miss |
| 冷启，N 进程竞争 | 独占锁 | **1** | 1 编译 + (N−1) 等锁后复查命中 |
| 冷启，worker 被杀留下半成品 | —— | —— | 原子 rename 保证「半成品」不在正式路径 |
| 冷启，`.o` 被截断（旧 CI 残留） | 加载失败 | 重编 | 损坏隔离 |

#### 4.1.3 源码精读

**FileLock：带超时自旋的咨询锁**（[quack/cache/jit.py:94-122](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/cache/jit.py#L94-L122)）。独占锁用 `LOCK_EX`、共享锁用 `LOCK_SH`；因为 `flock` 默认会阻塞，这里用 `LOCK_NB`（非阻塞）+ 循环 `sleep(0.1)` 自旋到 `timeout`，超时就抛 `RuntimeError` 让上层走「退化进程内编译」分支。

```python
def __enter__(self) -> "FileLock":
    flags = os.O_WRONLY | os.O_CREAT if self.exclusive else os.O_RDONLY | os.O_CREAT
    lock_type = fcntl.LOCK_EX if self.exclusive else fcntl.LOCK_SH
    self._fd = os.open(str(self.lock_path), flags)
    deadline = time.monotonic() + self.timeout
    while time.monotonic() < deadline:
        try:
            fcntl.flock(self._fd, lock_type | fcntl.LOCK_NB)
            return self
        except OSError:
            time.sleep(0.1)
    ...
    raise RuntimeError(f"Timed out waiting for lock: {self.lock_path}")
```

> 中文说明：每个 key 对应一个 `{sha}.lock` 文件。抢共享锁只读、抢独占锁才写；不同 key 的锁文件不同，所以**不同 key 互不竞争**。

**热路径：乐观检查 + 共享锁加载**（[quack/cache/jit.py:205-224](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/cache/jit.py#L205-L224)）。先用无锁的 `.exists()` 做零成本短路，再在共享锁内复查存在性并加载——共享锁挡住「另一个正在独占写入的进程」，避免读到写了一半的文件。

**损坏隔离**（[quack/cache/jit.py:191-218](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/cache/jit.py#L191-L218)）：加载失败的 `.o`（截断、缺符号）不是错误而是 miss——删掉它、让它和后续进程重编，避免 CI 里一个坏文件让这个 key 永远失败。

```python
def _quarantine_corrupt(exc: Exception) -> None:
    print(f"quack cache: corrupt cached object for key {sha} ... deleting and recompiling")
    try:
        o_path.unlink()
    except OSError:
        pass
```

> 中文说明：损坏隔离保证「CI 缓存跨 run 持久」的安全性——一个被杀 worker 留下的坏 `.o` 不会被永久信任。

**慢路径：锁内编译 + 原子 rename**（[quack/cache/jit.py:301-341](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/cache/jit.py#L301-L341)）。注意两处细节：①`lock.__enter__()` 故意放在 `try` 之外——若把锁获取和编译体放进同一个 `try`，编译抛出的 `RuntimeError` 会被误当成「锁超时」，于是又跑一次失败的编译；②导出先写 `.tmp` 再 `os.replace`。

```python
compiled_fn = fn(*args, **kwargs)
# Export to a private temp file, then atomically rename into place:
# a process killed mid-export ... must never leave a truncated .o ...
tmp_path = o_path.with_suffix(f".o.tmp.{os.getpid()}")
compiled_fn.export_to_c(object_file_path=str(tmp_path), function_name=EXPORT_FUNC_NAME)
os.replace(tmp_path, o_path)
```

**锁超时退化**（[quack/cache/jit.py:287-300](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/cache/jit.py#L287-L300)）：独占锁 60 秒抢不到（重度竞争或持锁者卡死），就放弃磁盘、纯进程内编译。哲学是「宁可编两次，也不要让测试因为拿不到锁而失败」。

**并发回归测试**（[tests/test_cache.py:72-206](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/tests/test_cache.py#L72-L206)）`test_jit_cache_lock_serializes_redundant_compiles` 用 8 个子进程同时编译同一个冷 key，每次编译体执行都写一个 `compile_*.marker`，最后断言 `len(markers) == 1`——这就是「lock-before-compile」不变量的机器化验证。

#### 4.1.4 代码实践

**实践目标**：亲眼验证「N 进程竞争同一冷 key 只编译一次」这条不变量。

**操作步骤**：

1. 打开 [tests/test_cache.py:72-90](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/tests/test_cache.py#L72-L90)，阅读测试 docstring，理解「marker 计数」的判据。
2. 阅读被测的 `@jit_cache` stub 是如何 monkey-patch `cute.runtime.load_module`、让编译体只写 marker 不真正编译（这样测试无需 GPU）。
3. 运行：
   ```bash
   pytest tests/test_cache.py::test_jit_cache_lock_serializes_redundant_compiles -x
   ```

**需要观察的现象**：测试通过；若你把断言临时改成 `>= 8`，应当**失败**（证明修复确实生效，旧逻辑会看到 ~8 个 marker）。

**预期结果**：`markers` 恰好 1 个——8 个子进程抢同一冷 key，只有 1 个进了编译体，其余 7 个等独占锁后复查命中。

> 待本地验证：本测试是纯 CPU 的（不触 CUDA），任何能 `import quack` 的环境都应能跑。若 `import quack` 因缺 cutlass-dsl 失败，标注「待本地验证（需 dev 环境）」。

#### 4.1.5 小练习与答案

**练习 1**：如果把「锁内复查 `.o` 是否存在」这步删掉，N 进程竞争冷 key 时会发生什么？

> **参考答案**：仍只编译 1 次（独占锁保证了串行化），但其余 N−1 个进程在拿到锁后会各自**重新编译一次**，因为它们不知道前一个进程已经把 `.o` 写出来了——编译次数从 1 退化到 N。复查把「等锁期间别人已编好」的竞态变成命中，省掉 N−1 次重复编译。

**练习 2**：损坏隔离为什么删 `.o` 而不是抛异常？

> **参考答案**：CI 的缓存目录会跨 run 持久化（`$HOME` 下）。一个被杀 worker 截断的 `.o` 若被当成硬错误，会让这个 key 在**之后每一次** CI run 都失败。删掉它等于把它降级为普通 miss，下一次自然重编出好文件。

---

### 4.2 async_compile 编译池与 CompilePending

#### 4.2.1 概念说明

解决了「正确性」，下一个问题是「快」。一个冷内核编译约 500ms，而一个参数化密集的测试文件可能触发几十个不同的冷 key。如果全部串行地在测试线程里编译，墙钟时间会被冷编译统治。

QuACK 的解法是 **defer-and-retry（推迟—重试）**：

- 冷 miss 时，**不**在当前线程编译，而是把这个 key 序列化后**投递给一个 CPU 子进程池**；
- 当前线程立即抛出 `CompilePending` 异常，表示「这个 key 正在后台编，我这边先放一放」；
- 调用方（pytest 插件 / autotuner）**抓住** `CompilePending`，把这个工作项推到队尾，先跑别的；
- 等后台把 `.o` 写出来（这是双方唯一的会合点），再把原工作项捞回来重试——这次热路径加载只需 ~1ms。

这套机制有三个精妙的设计判断，记在心里再读源码会顺畅很多：

1. **`.o` 是唯一会合点**。编译出来的内核对象**不可 pickle**（无法跨进程传递），所以 worker 不可能把结果对象还给主进程。于是「磁盘上的 `.o` 文件」同时充当了**结果载体**和**跨进程通信通道**——worker 写、主进程读。4.1 的每 key `flock` 则顺带充当了**跨进程去重**：多个池/xdist worker 同时编译同一个 key 也不会重复。

2. **worker 天然不启动内核**。被投递的不是「跑内核」，而是「编译内核的纯函数」`_compile_*`（如 `_compile_gemm`）。worker 调用它生成 IR、导出 `.o`，全程不碰 GPU。这让它可以跑在纯 CPU 的 fork 子进程里，与主进程的 CUDA 上下文隔离（fork 与 CUDA 上下文是不安全的组合）。

3. **`CompilePending` 继承 `BaseException` 而非 `Exception`**。这样测试体里的 `except Exception`、`pytest.raises(Exception)` 都**抓不住**它——否则一个「还没真正跑」的测试会被误判为通过。只有插件自己的 defer 钩子该抓住它。

#### 4.2.2 核心流程

整体是「生产者—会合点—消费者」三段：

```
主进程测试线程 (jit_cache wrapper)
  ├─ 3b. 冷 miss + 池在活动:
  │    ├─ pool.poll(sha) 返回 "new"
  │    │    └─ 若别的进程正独占锁编此 key → mark_external → 抛 CompilePending（省一个池槽）
  │    │    └─ 否则 pool.submit(sha, fn, args) 把 (module, qualname, args) pickle 投递 → 抛 CompilePending
  │    ├─ poll 返回 "pending" → 抛 CompilePending
  │    ├─ poll 返回 "done"    → 共享锁加载 .o 返回
  │    └─ poll 返回 "failed"  → warnings.warn + 落到 4.1 慢路径进程内编译（拿真 traceback）
  ▼
CPU worker (_pool_worker)
  ├─ importlib 按 module+qualname 解析出 _compile_* 函数
  ├─ 调用它 → jit_cache wrapper 在 worker 里编译 + export .o（.o 即会合点）
  └─ 返回 None(成功) / err string(失败)
  ▼
调用方 defer 循环 (pytest 插件 / autotuner bench)
  └─ 抓 CompilePending → 推队尾 → 轮询 .o 是否就绪 → 就绪则重试工作项
```

`CompilePool.poll(sha)` 是一个四态状态机，是 defer 循环决策的核心：

| `poll` 返回 state | 含义 | defer 循环的动作 |
|-------------------|------|------------------|
| `"new"` | 本池还没编过这个 key | wrapper 决定 submit 或 mark_external |
| `"pending"` | 已 submit，worker 还没编完 | 把工作项推到队尾，跑别的 |
| `"done"` | worker 编完、`.o` 已落盘 | 重试工作项（这次热路径） |
| `"failed"` | worker 报错 | 退化为进程内编译，拿真实 traceback |

#### 4.2.3 源码精读

**`CompilePending`：为何继承 `BaseException`**（[quack/cache/async_compile.py:109-125](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/cache/async_compile.py#L109-L125)）。携带 `sha` 让 defer 循环能只轮询这一个 key、不必重跑整个测试：

```python
class CompilePending(BaseException):
    """... Derives from BaseException (like KeyboardInterrupt) so that
    test-body `except Exception` / `pytest.raises(Exception)` blocks
    cannot swallow it and turn a not-yet-run test into a false pass."""
    def __init__(self, sha: str, qualname: str):
        super().__init__(f"kernel compile pending in pool: {qualname} [{sha[:12]}]")
        self.sha = sha
        self.qualname = qualname
```

**`_flock_held_exclusively`：跨进程去重探测**（[quack/cache/async_compile.py:87-106](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/cache/async_compile.py#L87-L106)）。尝试抢共享锁——抢得到说明没人在独占（没人正在编译），抢不到说明有进程正持有独占锁编这个 key：

```python
def _flock_held_exclusively(lock_path: str) -> bool:
    ...
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False          # 没人在编
    except OSError:
        return True           # 有人正独占编这个 key
```

> 中文说明：当 wrapper 发现某个 key 在 `pool` 里是 `"new"`、但 `_flock_held_exclusively` 为真，说明**别的进程**（比如另一个 xdist worker 的池）正在编它。此时不再给自己池投递重复任务（那会占一个池槽、然后被同一个 flock 堵住），而是 `mark_external` 记一笔，直接抛 `CompilePending` 推迟——等那个外部进程把 `.o` 写出来。

**`CompilePool.submit` 与 `submit_raw`**（[quack/cache/async_compile.py:409-443](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/cache/async_compile.py#L409-L443)）。`submit` 先做「可投递性」校验：函数不能定义在 `<locals>` 或 `__main__`（worker 无法按 module+qualname 解析），参数必须可 pickle；任一不满足就返回 `False`，让上层走进程内编译：

```python
def submit(self, sha, fn, args, kwargs, o_path) -> bool:
    if sha in self._futures:
        return True
    if "<locals>" in fn.__qualname__ or fn.__module__ == "__main__":
        return False                  # worker 无法解析，进程内编译
    try:
        key_b64 = base64.b64encode(pickle.dumps((args, kwargs))).decode("ascii")
        ...
    except Exception:
        return False                  # 不可 pickle
    self.submit_raw(sha, fn.__module__, fn.__qualname__, key_b64, str(o_path), payloads_b64)
    return True
```

**`_pool_worker`：worker 侧编译**（[quack/cache/async_compile.py:254-293](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/cache/async_compile.py#L254-L293)）。按 `(module, qualname)` 解析出 `_compile_*` 函数，调它（内部仍是 `jit_cache` wrapper，会在 worker 里编译并导出 `.o`），最后检查 `.o` 是否真的写出。失败时只保留最后 4 帧的 traceback 字符串，让会话末尾的统计行可诊断：

```python
def _pool_worker(mod_name, qualname, key_b64, o_path, payloads_b64=""):
    try:
        ...
        obj = importlib.import_module(mod_name)
        for part in qualname.split("."):
            obj = getattr(obj, part)
        args, kwargs = pickle.loads(base64.b64decode(key_b64))
        obj(*args, **kwargs)          # jit_cache wrapper: 编译 + 导出 .o
        if not os.path.exists(o_path):
            return "compile succeeded but .o was not exported"
        return None
    except Exception as e:
        ...
        return f"{type(e).__name__}: {e} [worker: {tail[-600:]}]"
```

> 中文说明：worker 解析的是 `_compile_gemm` 这种「顶层函数」，不是闭包——这正是 `submit` 里禁止 `<locals>` 的原因。

**`_make_executor`：Inductor 式 forkserver sidecar**（[quack/cache/async_compile.py:296-314](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/cache/async_compile.py#L296-L314)）。默认 `forkserver` 启动法，并设 `set_forkserver_preload(["quack.cache._pool_preload"])`——forkserver 进程一次性付掉 ~13s 的 torch/cutlass 导入，之后每个 worker `fork` 仅 ~0.1s（写时复制继承）：

```python
def _make_executor(jobs: int) -> ProcessPoolExecutor:
    start_method = os.environ.get("QUACK_ASYNC_COMPILE_START", "forkserver")
    ctx = get_context(start_method)
    if start_method == "forkserver":
        ctx.set_forkserver_preload(["quack.cache._pool_preload"])
    return ProcessPoolExecutor(
        max_workers=jobs, mp_context=ctx,
        initializer=_pool_initializer, initargs=_detect_arch_env(),
    )
```

**`_neutral_main`：阻止子进程重跑用户脚本**（[quack/cache/async_compile.py:343-368](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/cache/async_compile.py#L343-L368)）。`Process.start` 会从 `sys.modules['__main__']` 抓「子进程准备数据」并可能让子进程重跑整个用户脚本；若用户脚本在 import 期就建了 CUDA 张量，每个 worker 都会因「Cannot re-initialize CUDA in forked subprocess」而死。`_neutral_main` 在 submit 瞬间把 `__main__` 换成空壳、submit 完再换回，精准屏蔽。

**`prewarm`：让 sidecar 预热与 collection 重叠**（[quack/cache/async_compile.py:397-407](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/cache/async_compile.py#L397-L407)）。pytest 在 `pytest_configure` 里调 `pool.prewarm()`，投一个 no-op `os.getpid` 把 sidecar 的 ~13s 导入提前到「pytest 收集用例 + 早期 warm 测试」期间，而不是等到第一次冷 miss 才付。

**消费者一：pytest `--async-compile` 插件**（[quack/testing/pytest_plugin.py:29-42](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/testing/pytest_plugin.py#L29-L42)）注册选项，[pytest_plugin.py:199-217](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/testing/pytest_plugin.py#L199-L217) 在 configure 阶段 `activate` 池并 `prewarm`；[pytest_plugin.py:392-452](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/testing/pytest_plugin.py#L392-L452) 的 `_SingleProcDeferLoop` 是主循环：用一个 `deque`，碰到 `CompilePending` 的用例转回队尾、轮询其 `sha` 是否 `done`、就绪再重跑。`_MAX_ATTEMPTS = 20` 与 `_WEDGE_TIMEOUT_S = 600` 是兜底——超过任一阈值，用 `suppress_pool()` 强制进程内编译，防止一个永久 pending 的 key 把测试卡死。

**消费者二：autotuner bench 循环**（[quack/autotuner.py:380-420](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/autotuner.py#L380-L420)）跑在 `pool_scope()` 内，逐配置 bench；某个配置还没编好就抛 `CompilePending`，循环把它转回队尾、先 bench 已就绪的配置，等它的 `.o` 落地再 bench。这让「编译」与「测量」天然重叠，详见 [u8-l1](u8-l1-autotuning.md)。

**失败语义：worker 失败永不信任**（[quack/cache/jit.py:263-273](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/cache/jit.py#L263-L273)）：`poll` 返回 `"failed"` 时只 `warnings.warn`，然后**落到 4.1 的慢路径进程内编译**——这样真正的异常带着本地 traceback 冒出来，而不是只剩 worker 的字符串摘要。

#### 4.2.4 代码实践

**实践目标**：亲身体验「冷编译与测试并行重叠」，并读懂会话末尾的统计行。

**操作步骤**：

1. 先清掉 softmax 的缓存，制造一次「冷启」（缓存目录随你的环境而定）：
   ```bash
   rm -rf ${QUACK_CACHE_DIR:-/tmp/$(whoami)/quack_cache}
   ```
2. 用 `--async-compile` 跑一个小的参数化切片（注意 AGENTS.md 的建议：迭代时只跑 1–3 个参数化）：
   ```bash
   pytest tests/test_softmax.py -x -k 'bfloat16' --async-compile=16
   ```
3. 观察会话**末尾**打印的一行总结，形如：
   ```
   async-compile: N keys submitted, M failed, K test deferrals
   ```
4. 不清缓存，原样再跑一次（热路径），对比总结行里的 `deferrals`（应接近 0）与墙钟时间。

**需要观察的现象**：冷启那次的 `K test deferrals` 明显大于 0——测试在被推迟、同时后台在编译；热启那次 `deferrals` 趋近于 0，因为 `.o` 都已就绪、`jit_cache` 直接走共享锁热路径，`CompilePending` 根本不会抛。

**预期结果**：`--async-compile=N` 让「等待冷编译的时间」被「跑其他已就绪测试的时间」吸收；缓存热时零开销（这是插件 docstring 的承诺）。

> 待本地验证（需 GPU + dev 环境）：具体 `deferrals` 数与墙钟取决于本机冷 key 数量；若 `import quack` 失败则标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 worker 用 `importlib` 按 `(module, qualname)` 解析函数，而不是把函数对象本身 pickle 过去？

> **参考答案**：两条原因。①`_compile_*` 函数的闭包/定义域若在 `<locals>` 或 `__main__`，pickle 后在 worker 里解析不回来；按 module+qualname 解析要求函数是「顶层、可导入」的，这也是 `submit` 里拒绝 `<locals>`/`__main__` 的依据。②把可序列化的原始参数 pickle 过去，worker 自己重新解析函数，避免把整个调用栈状态搬过进程边界，更稳健。

**练习 2**：`CompilePending` 为什么要继承 `BaseException`？举一个「若继承 `Exception` 会出 bug」的具体场景。

> **参考答案**：若继承 `Exception`，测试体里的 `try: ... except Exception: ...` 或 `pytest.raises(Exception)` 会把它吞掉。例如一个用 `pytest.raises(RuntimeError)` 断言「内核应抛错」的测试，若内核尚未编译就抛了 `CompilePending`，会被 `raises` 当成「确实抛了异常」而**误判通过**——而这个测试其实根本没跑到内核。继承 `BaseException`（与 `KeyboardInterrupt` 同级）让它穿透这些宽泛捕获，只有 defer 钩子能接住。

**练习 3**：`prewarm()` 投递的是 `os.getpid`（一个 no-op）。为什么不投一个真实 key 来预热？

> **参考答案**：`prewarm` 的唯一目的是**提前付掉 forkserver 的 ~13s torch/cutlass 导入**，让它与 pytest 收集/早期测试重叠。它不该挑起任何真实编译（真实 key 由真实 miss 触发）。投 no-op 只是「让 sidecar 进程被拉起来」的最便宜方式，副作用最小。

---

### 4.3 缓存环境变量与源码指纹

#### 4.3.1 概念说明

缓存的最后一个问题是**有效性**：什么条件下，一个磁盘上的 `.o` 可以被信任复用？

QuACK 用「源码指纹」一刀切回答：只要「生成这个 `.o` 的源码与运行环境」和「现在」的指纹一致，就信任。指纹是 `.o` 路径的中间目录名——指纹变了，旧目录就被孤立、自然失效。

本节有两簇环境变量，分别管「缓存是否/在哪」和「worker 编译目标」，初学者容易混，务必分清：

| 变量 | 默认 | 作用 | 谁读 |
|------|------|------|------|
| `QUACK_CACHE_ENABLED` | `1` | 关磁盘缓存（仍保留内存缓存） | `jit_cache` |
| `QUACK_CACHE_DIR` | `<tmp>/<user>/quack_cache` | 缓存根目录 | `get_cache_path` |
| `EXTRA_SOURCE_DIRS` | `[]` | 下游项目把自家源码纳入指纹 | `_compute_source_fingerprint` |
| `QUACK_ARCH` | 自动探测 | Python 侧**分发**架构（trace 哪个内核类/哪套配置） | worker / `get_device_capacity` |
| `CUTE_DSL_ARCH` | 自动探测 | ptxas **编译目标**（发射哪些指令） | worker / DSL 单例 |
| `CUDA_VISIBLE_DEVICES` | 不变 | worker 设为 `""` 实现 GPU-blind | worker initializer |
| `QUACK_COMPILE_WORKERS` | `8` | 共享 executor 大小（autotune 扫描用） | `get_shared_executor` |
| `QUACK_ASYNC_COMPILE_START` | `forkserver` | 设为 `spawn` 关掉 fork sidecar | `_make_executor` |

注意 `QUACK_ARCH` 与 `CUTE_DSL_ARCH` 回答的是**两个不同问题**——这是 worker 正确性最容易踩的坑，见 4.3.2。

#### 4.3.2 核心流程

**源码指纹**的构造（公式化）：

\[
\text{fingerprint} = H\!\bigl(
\underbrace{\text{py\_ver}}_{\text{解释器 ABI}}\;\Vert\;
\underbrace{\text{cutlass\_ver}}_{\text{DSL ABI}}\;\Vert\;
\underbrace{\text{tvm\_ffi\_ver}}_{\text{FFI ABI}}\;\Vert\;
\underbrace{H(\text{所有 } *.py \in \text{quack})}_{\text{源码本身}}\;\Vert\;
\underbrace{H(\text{EXTRA\_SOURCE\_DIRS})}_{\text{下游源码}}
\bigr)
\]

\[
\text{sha} = \text{SHA256}\bigl(\text{pickle}(\text{qualname}, \text{args}, \text{kwargs})\bigr),\qquad
\text{path} = \text{CACHE\_DIR}\,/\,\text{fingerprint}\,/\,\text{sha.o}
\]

为什么把**整个 `quack` 包**都哈希进去，而不是只哈希被编译函数所在的文件？因为一个内核的最终 IR 可能被**任意一个** `.py` 影响：`copy_utils.py` 里一个常量、`gemm_config.py` 里的 tile 尺寸、`layout_utils.py` 里的布局变换，都会改写生成的机器码。若只哈希当前文件，就会漏掉这些跨文件依赖，导致「源码改了、`.o` 却没失效」的幽灵 bug（用旧 cubin 跑新逻辑）。哈希整个包是**保守但安全**的策略——这也呼应 AGENTS.md 的原则：缓存稳定性**不是**设计约束，宁可一次重编，也不要为保缓存而扭曲代码。

> 工程取舍：哈希整个包意味着任意一次 `ruff format` 都会让全部 `.o` 失效、触发完整重编。这是有意的代价，换来「绝不用到过期 cubin」的正确性保证。

**worker 架构探测**（`_detect_arch_env`）为何把 `QUACK_ARCH` 与 `CUTE_DSL_ARCH` 分开处理，是 worker 正确性的关键。两者的判据：

- `CUTE_DSL_ARCH`（ptxas 目标）**永远**默认物理 GPU，**绝不**从 `QUACK_ARCH` 推导。因为 `.o` 最终要由主进程在物理 GPU 上 `cuModuleLoad`——worker 产出的 `.o` 必须与主进程「能加载的架构」一致。
- `QUACK_ARCH`（分发架构）尊重调用方设置：CI 代理机常在 H100 上用 `QUACK_ARCH=120` 来 trace SM120 内核类（跨架构覆盖测试）。

如果在 CI 代理机（`QUACK_ARCH=120` + 物理 H100）上让 `CUTE_DSL_ARCH` 跟 `QUACK_ARCH` 走，worker 会把 `.o` 编成 `sm_120a`，而主进程的 H100 只能加载 `sm_90a`——每个池编译都会 `cuModuleLoad` 失败、退化为进程内重编，整个池形同虚设。所以 `_detect_arch_env` 让 worker 目标钉在物理架构 `sm_90a`，与主进程一致。

#### 4.3.3 源码精读

**运行期开关的定义位置**（[quack/cache/\_\_init\_\_.py:38-43](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/cache/__init__.py#L38-L43)）。注意 docstring 强调的**顺序敏感性**：

```python
CACHE_ENABLED: bool = os.getenv("QUACK_CACHE_ENABLED", "1") == "1"
CACHE_DIR: Optional[str] = os.getenv("QUACK_CACHE_DIR", None)
EXTRA_SOURCE_DIRS: List[Path] = []
```

> 中文说明：这三个名字**必须**在 `from quack.cache.jit import ...` 之前定义。因为 `jit.py` 顶部有 `import quack.cache as _state`，此时 `quack.cache` 是「部分初始化的包对象」；wrapper 运行时通过 `_state.CACHE_ENABLED` 取值，若这些名字还没定义就 `AttributeError`。[\_\_init\_\_.py:22-29](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/cache/__init__.py#L22-L29) 的注释明确警告「连 auto-formatter 重排导入顺序都会破坏首次内核编译」。这就是 `tests/test_cache.py::test_public_api_symbols_resolve` 存在的原因。

**整个包的递归哈希**（[quack/cache/jit.py:56-82](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/cache/jit.py#L56-L82)）。`_hash_source_dir` 对目录下所有 `.py`（`rglob`）按相对路径排序后逐个哈希路径名+长度+内容；`_compute_source_fingerprint` 用 `lru_cache(maxsize=1)` 只算一次：

```python
@functools.lru_cache(maxsize=1)
def _compute_source_fingerprint() -> str:
    h = hashlib.sha256()
    h.update(f"py{sys.version_info.major}.{sys.version_info.minor}".encode())
    h.update(f"cutlass={cutlass.__version__}".encode())
    h.update(f"tvm_ffi={tvm_ffi.__version__}".encode())
    import quack as _quack
    _hash_source_dir(h, Path(_quack.__file__).resolve().parent)   # 整个 quack 包
    for extra_dir in _state.EXTRA_SOURCE_DIRS:
        _hash_source_dir(h, Path(extra_dir).resolve())
    return h.hexdigest()
```

> 中文说明：注释点明「哈希整个 `quack` 包而非只 `quack/cache/`，且通过顶层包导入解析路径，保持指纹稳定」。`EXTRA_SOURCE_DIRS` 是给「在 quack 之上定义自定义内核的下游项目」用的扩展口——必须在首次 `jit_cache` 调用前填好。

**worker 架构探测**（[quack/cache/async_compile.py:128-168](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/cache/async_compile.py#L128-L168)）`_detect_arch_env`。读这段时盯住「`CUTE_DSL_ARCH` 绝不从 `QUACK_ARCH` 推导」这条不变量：

```python
if cute_arch is None:
    try:
        import torch
        if torch.cuda.is_available():
            major, minor = torch.cuda.get_device_capability()
            cc = f"{major}{minor}"
            cute_arch = f"sm_{cc}a" if major >= 9 else f"sm_{cc}"   # 物理架构
    except Exception:
        pass
if cute_arch is None and quack_arch is not None:
    # 仅在纯 CPU 盒子上，分发架构才是唯一可用的目标
    ...
    cute_arch = ...
```

> 中文说明：worker「不碰 CUDA 驱动」（每个 worker 不建上下文、fork 安全）——这个探测在**父进程**里跑一次，结果传给所有 worker。

**GPU-blind 的最后一块拼图**（[quack/cache/async_compile.py:171-202](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/cache/async_compile.py#L171-L202)）`_install_gpu_blind_device_attrs`。`cute.compile` 几乎不碰驱动，**除了一条路径**：当 launch 设 `min_blocks_per_mp > 1` 又没给 `preferred_smem_carveout` 时，DSL 会查 `MAX_SHARED_MEMORY_PER_MULTIPROCESSOR`——在 GPU-blind worker 里这会抛 `CUDA_ERROR_NOT_INITIALIZED`。解法是用 DSL 自带的静态架构表回答它：

```python
def get_device_attribute(attribute, device_id: int = 0):
    if attribute == smem_attr:
        sm = os.environ.get("CUTE_DSL_ARCH", "").removesuffix("a")
        capacity = get_smem_capacity_in_bytes(sm)
        return capacity + 1024          # 每 CTA 容量 + 1KiB 预留 = SM 总量
    return orig(attribute, device_id)
```

> 中文说明：这样 worker 产出的 `.o` 与进程内编译的 `.o` **逐位相同**（同架构），不会因 smem 占用算错而偏离。

**worker initializer**（[quack/cache/async_compile.py:229-251](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/cache/async_compile.py#L229-L251)）把 `CUDA_VISIBLE_DEVICES=""`、`QUACK_ARCH`、`CUTE_DSL_ARCH` 钉好，再 `_pin_dsl_arch` 把 ptxas 目标重新插到已构造的 DSL 单例上（cutlass-dsl ≥4.6.2 在构造时快照了 `CUTE_DSL_ARCH`，需要补插）。

**forkserver 预热里的架构钉定**（[quack/cache/\_pool_preload.py:44-64](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/cache/_pool_preload.py#L44-L64)）。用 `nvidia-smi`（不建 CUDA 上下文）探 compute capability，在导入 quack 前钉好两个架构变量，并把 `CUDA_VISIBLE_DEVICES=""` 作为「保险带」。

#### 4.3.4 代码实践

**实践目标**：亲手验证「源码改即失效」——改动一个 `.py` 后指纹变化、旧 `.o` 目录被孤立。

**操作步骤**：

1. 设一个独立缓存目录跑一次 softmax，观察生成的指纹子目录：
   ```bash
   export QUACK_CACHE_DIR=/tmp/quack_u8l2_fp
   pytest tests/test_softmax.py -x -k 'bfloat16' >/dev/null 2>&1
   ls /tmp/quack_u8l2_fp        # 看到一个 <64位十六进制> 子目录，里面是 <sha>.o
   ```
2. 记下这个指纹目录名。然后在 quack 包里**加一行无害注释**（模拟源码改动），例如给 `quack/copy_utils.py` 顶部加 `# u8l2 probe`：
   ```bash
   # (手动编辑文件加一行注释即可)
   ```
3. 用同一个缓存目录再跑一次：
   ```bash
   pytest tests/test_softmax.py -x -k 'bfloat16' >/dev/null 2>&1
   ls /tmp/quack_u8l2_fp        # 现在出现【第二个】指纹子目录
   ```

**需要观察的现象**：第二步后出现一个**新的**指纹目录，旧的还在但已成为孤儿（再也不会被命中）。这证明「整个 `quack` 包」里任意 `.py` 的改动（哪怕只是一行注释）都会改变指纹。

**预期结果**：两次运行的指纹目录名不同；新目录里重新生成 `.o`（冷编译），旧目录被废弃。

> 待本地验证（需 GPU + dev 环境）：指纹值取决于本机 quack 源码内容；若 `import quack` 失败则标注「待本地验证」。验证完记得删掉你加的注释，或用 `git checkout quack/copy_utils.py` 还原——本讲义**不允许修改源码**，这只是临时观察手段。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `_compute_source_fingerprint` 把 `cutlass.__version__` 也哈希进去？

> **参考答案**：CuTe-DSL（cutlass）是生成 IR 的工具链。不同版本的 DSL 可能对同一个 kernel 生成不同的 IR/机器码（指令选择、lowering 规则都会变）。若不把 DSL 版本纳入指纹，升级 cutlass 后会继续用旧 `.o`，而这些 `.o` 可能对应旧 lowering，行为与新版本不一致。同理 Python 主次版本（ABI）和 tvm_ffi 版本（FFI ABI）也要进指纹。

**练习 2**：CI 在 H100（SM90）上用 `QUACK_ARCH=120` 跑 SM120 的覆盖测试。worker 应该把 `.o` 编成 `sm_120a` 还是 `sm_90a`？为什么？

> **参考答案**：编成 `sm_90a`。`QUACK_ARCH=120` 只决定 Python 侧**分发**（trace `GemmSm120` 类），但 `.o` 最终要在物理 H100 上 `cuModuleLoad`，H100 只能加载 `sm_90a`。若 worker 编成 `sm_120a`，加载会失败、每个池编译退化为进程内重编，池就废了。`_detect_arch_env` 因此让 `CUTE_DSL_ARCH` 永远默认物理 GPU、绝不由 `QUACK_ARCH` 推导。

---

## 5. 综合实践

**任务**：把本讲三个模块串起来——解释「源码指纹哈希整个 quack 包」与「`--async-compile=N` 让冷编译与测试重叠」这两件事如何**协同**支撑 CI 的冷启动性能。

**操作步骤**：

1. **源码阅读（无 GPU 也可做）**：画一张时序图，描绘「CI 冷启动 + xdist 8 worker + `--async-compile=32`」的一次 pytest 运行里，下列事件如何交错：
   - `pytest_configure` → `activate(...)` → `prewarm()`（forkserver sidecar 开始导入 torch/cutlass，与用例收集重叠）。
   - 第一个冷 key 在 worker0 命中：`jit_cache` step 3b → `pool.submit` → 抛 `CompilePending` → 测试 defer，worker0 转去跑别的用例。
   - 后台 CPU worker `fork` 自 sidecar、GPU-blind 编译该 key、导出 `<指纹>/<sha>.o`。
   - worker0 的 defer 循环 `pool.poll(sha)` 见 `"done"` → 共享锁加载 `.o` → 重跑用例（这次真跑内核）。
2. **关键解释**：在图上标注两处协同——
   - 若 `_compute_source_fingerprint` 只哈希单个文件：CI 升级一个看似无关的 `.py` 后，旧 `.o` 仍被命中，但 `.o` 已过期 → 数值错误悄悄潜入。整个包哈希杜绝此风险。
   - 若没有 `--async-compile`：这 N 个冷 key 会在测试线程里串行编译，每个 ~500ms，总时间被编译统治。池让它们在后台并行、与测试执行重叠。
3. **（可选，需 GPU）实测对比**：用同一个冷缓存目录，分别跑
   ```bash
   pytest tests/test_softmax.py -x -k 'bfloat16'                      # 无池
   pytest tests/test_softmax.py -x -k 'bfloat16' --async-compile=16   # 有池
   ```
   对比墙钟时间与会话末尾的 `async-compile: ... deferrals` 行。

**预期结果**：你能用自己的话讲清——指纹保证了「缓存的 `.o` 永远与当前源码一致」（正确性），异步池保证了「冷编译不阻塞测试」（性能），二者共同让 CI 的冷启动既安全又快。

> 待本地验证：步骤 3 的实测需 GPU + dev 环境；步骤 1、2 纯源码阅读，任意环境可做。

---

## 6. 本讲小结

- **并发不变量**：`jit_cache` 用 per-key `flock`（共享锁读、独占锁写）保证 N 进程竞争同一冷 key 只编译一次；编译体在独占锁内、锁内复查防竞态、临时文件 + `os.replace` 防半成品、损坏 `.o` 自动隔离重编。
- **defer-and-retry 池**：冷 miss 投递给 CPU 子进程池并抛 `CompilePending`（继承 `BaseException` 防误吞），`.o` 是 worker 与调用方唯一会合点（编译产物不可 pickle），forkserver sidecar 让 worker 启动从 ~13s 降到 ~0.1s。
- **跨进程去重**：`_flock_held_exclusively` 探测「别的进程正在编这个 key」，避免重复投递占池槽；4.1 的 flock 与 4.2 的池共用同一套锁文件。
- **失败与兜底**：worker 失败永不信任、退化为进程内编译拿真 traceback；defer 循环有 `_MAX_ATTEMPTS=20` 与 `_WEDGE_TIMEOUT_S`（pytest 600s / autotuner 300s）兜底，超阈值 `suppress_pool()` 强制进程内编译。
- **源码指纹 = 正确性闸门**：哈希 Python/cutlass/tvm_ffi 版本 + 整个 `quack` 包 + `EXTRA_SOURCE_DIRS`，作为 `.o` 路径的中间目录，指纹变即旧缓存失效——保守但绝对安全。
- **worker GPU-blind**：`QUACK_ARCH`（分发）与 `CUTE_DSL_ARCH`（ptxas 目标）独立处理，后者永远默认物理 GPU，保证 worker 产出的 `.o` 能被主进程加载；`_install_gpu_blind_device_attrs` 用静态表回答唯一一条 trace 期驱动查询。

---

## 7. 下一步学习建议

- **回顾调用方**：本讲的池被两个消费者驱动。pytest 侧已在本讲读完；autotuner 侧的 `pool_scope()` bench 循环细节在 [u8-l1 自动调优](u8-l1-autotuning.md)，建议配合阅读，看「编译—测量重叠」如何缩短调优墙钟。
- **Split-K 与缓存的交互**：[u8-l3 Split-K 归约](u8-l3-split-k.md) 会讲 `split_k_reduce` 也是一个 `@jit_cache` 函数，本讲的并发与池机制同样作用于它。
- **缓存稳定性哲学**：重读 [AGENTS.md](AGENTS.md) 的「Cache stability is NOT a design constraint」与「`QUACK_CACHE_ENABLED=0` for const_expr ablations」两条——本讲的「整个包哈希」正是这一哲学的体现：宁可重编，不为保缓存扭曲代码。
- **动手扩展**：若你为下游项目写自定义内核，试着把项目源码目录追加进 `EXTRA_SOURCE_DIRS`，观察你的指纹被纳入缓存键——这是让「下游源码改即失效」的标准做法。
