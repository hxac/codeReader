# 编译缓存机制

## 1. 本讲目标

tilelang 把一个 Python DSL 函数编译成可执行 kernel 的代价很高：要跑几十个 Pass、生成 CUDA/HIP 源码、再调用 `nvcc`/`hipcc` 编译成二进制。一次编译动辄几百毫秒到几秒。本讲解决的核心问题是：**同一个 kernel 被反复编译时，tilelang 如何避免重复劳动？**

学完本讲你应该能够：

- 说出 tilelang 的**四层缓存**分别位于哪条调用链上、缓存什么、键是什么。
- 理解 JIT 层 `_CallFormCache` 与 `_kernel_cache` 的会话级内存缓存，以及 `parse_args` 产出的 `(p1_key, p2_key)` 键的含义。
- 掌握 `KernelCache` 的「磁盘 + 内存」两级缓存、缓存键（`_generate_key`）的构造与失效条件、命名空间（namespace）划分，以及原子写与 `from_database` 复用机制。
- 掌握 `CUDABinaryCache` 对 cubin/fatbin 设备二进制的缓存，以及为什么编译选项（如 `--use_fast_math`）必须进键。
- 会用 `enable_cache()` / `disable_cache()` 与 `TILELANG_DISABLE_CACHE` 等环境变量控制缓存行为。

## 2. 前置知识

阅读本讲前，你需要先建立以下认知（来自前置讲义）：

- **u1-l2 / u1-l3**：`tilelang.env` 用 `EnvVar` 描述符 + `Environment` 类集中管理环境变量，实例化为全局 `env` 单例；`tilelang/__init__.py` 导出公共 API。
- **u4-l1**：编译总入口 `tilelang.lower()` 把 PrimFunc 经 Pass 流水线变成 host/device IR，再经 device codegen 生成设备源码；其中设备源码经 `tilelang_callback_cuda_compile` 回调编译成 cubin。
- **u4-l2**：`@tilelang.jit` 把 Python 函数包成 `JITImpl`，有 lazy/eager 两种模式；`JITImpl.__call__` 走三层缓存（`_call_form_cache`、`_kernel_cache`、`JITFunc.p1_cache`）；`compile()` 经 `tilelang.lower()` 得到 `JITKernel`，由 adapter 包装成可调用对象。eager 模式的 `TirTemplate` 用 phase1（`T.const` 占位）/phase2（实参 shape 替换）两阶段实现「一次模板、多 shape」复用。

本讲正是在 u4-l2 提到的「三层缓存」基础上，把它们逐层拆开讲清楚，并补充跨进程的磁盘缓存与设备二进制缓存。

**两个基础概念**：

- **缓存键（cache key）**：一个能唯一标识「这次编译输入」的值（通常是字符串哈希）。键相同 ⇒ 输入完全相同 ⇒ 可以复用旧产物；键不同 ⇒ 必须重新编译。设计缓存的核心就是**键里要放什么**：放少了会「假命中」（拿到错误产物），放多了会「永远 miss」（失去缓存意义）。
- **SHA-256**：tilelang 的缓存键普遍用 SHA-256 把一个 JSON 字典压成 64 位十六进制字符串。SHA-256 的碰撞概率在工程上可视为零，且对字典键排序（`json.dumps(..., sort_keys=True)`）保证了键的构造与字段书写顺序无关。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [tilelang/env.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/env.py) | 缓存总开关 `CacheState`、`enable/disable_cache`、`TILELANG_CACHE_DIR`/`TILELANG_DISABLE_CACHE` 等环境变量 |
| [tilelang/jit/\_\_init\_\_.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/__init__.py) | JIT 层会话级内存缓存 `_CallFormCache`、`_kernel_cache`、`compile()`/`cached()` 衔接 |
| [tilelang/cache/\_\_init\_\_.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cache/__init__.py) | `cached()` 装饰器入口、按 execution_backend 分发到各 `KernelCache` 单例 |
| [tilelang/cache/kernel_cache.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cache/kernel_cache.py) | `KernelCache`：磁盘+内存两级缓存、键构造、命名空间、原子写、`from_database` 复用 |
| [tilelang/cache/cuda_binary_cache.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cache/cuda_binary_cache.py) | `CUDABinaryCache`：cubin/fatbin 设备二进制缓存 |
| [tilelang/jit/adapter/kernel_cache.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/adapter/kernel_cache.py) | `TVMFFIKernelCache`：tvm_ffi 后端对 `KernelCache` 的特化（保存 executable.so） |
| [tilelang/engine/lower.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/engine/lower.py) | `tilelang_callback_cuda_compile` 回调，在 nvcc 编译前后调用 `CUDABinaryCache` |
| [tilelang/jit/kernel.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/kernel.py) | `JITKernel.from_database`：从缓存产物重建 kernel 而不重编译 |

## 4. 核心概念与源码讲解

先给出全景：一次 `@tilelang.jit` 装饰的函数被调用时，编译产物要穿过**四层缓存**才会真正触发 `nvcc`。由近及远（越靠前越快、作用域越小）：

```
JITImpl.__call__
  ├─ ① _call_form_cache   （会话内存，lazy 无 tensor 时按原始调用形式命中）
  ├─ ② _kernel_cache      （会话内存，按 parse_args 的 (p1_key,p2_key) 命中）
  └─ miss → compile() → tilelang.compile() → cached()
                                    ├─ ③ KernelCache._memory_cache  （进程内存，按 _generate_key 命中）
                                    ├─ ③ KernelCache 磁盘缓存        （跨进程，按同名 key 命中）
                                    └─ miss → JITKernel → lower() → tilelang_callback_cuda_compile
                                                                      └─ ④ CUDABinaryCache  （cubin/fatbin 二进制）
```

四层缓存的**作用域**与**键**各不相同：①② 活在单个 `JITImpl` 实例的进程内存里，进程退出即失效；③ 的内存部分活在单例里、磁盘部分跨进程持久化；④ 只缓存裸设备二进制，连 host 封装都不含。下面按最小模块逐层拆解。

### 4.1 tilelang.env：缓存总开关与环境变量

#### 4.1.1 概念说明

所有缓存层在读写之前都要先问同一个问题：「缓存现在开着吗？」这个答案由 `tilelang.env` 集中管理。`env` 用一个全局布尔标志 `CacheState._enabled` 加一个高优先级环境变量 `TILELANG_DISABLE_CACHE` 共同决定开关状态，并暴露 `enable_cache()` / `disable_cache()` 给用户在运行时翻转。

为什么要把「禁用缓存」做成高优先级环境变量？因为单元测试和调试时最怕「上次编译的旧产物污染这次的结果」——用一个环境变量就能强制每次都重新编译，无需改代码。

#### 4.1.2 核心流程

缓存「是否启用」的判定逻辑（伪代码）：

```
is_cache_enabled() = (not TILELANG_DISABLE_CACHE 为真)  and  CacheState._enabled
                     └── 环境变量级硬开关（高优先级）──┘   └── 运行时 API 软开关 ──┘
```

- 环境变量 `TILELANG_DISABLE_CACHE=1` 一旦设置，无论 `CacheState` 怎么变都不缓存（**全局禁用**）。
- `disable_cache()` 只把 `CacheState._enabled` 置 `False`，不碰环境变量（**运行时禁用**）。
- 缓存根目录由 `TILELANG_CACHE_DIR`（默认 `~/.tilelang/cache`）决定，临时目录由 `TILELANG_TMP_DIR` 决定。

#### 4.1.3 源码精读

`CacheState` 是一个极简的类级布尔容器，三方法分别翻转/读取类属性 `_enabled`：

[缓存总开关 CacheState — tilelang/env.py:208-226](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/env.py#L208-L226) — `_enabled = True` 是默认值，`enable/disable/is_enabled` 三个 classmethod 直接读写它。注意它是**类属性**而非实例属性，因此全进程共享同一个开关。

真正组合「环境变量 + 软开关」的判定写在 `Environment` 的方法里：

[is_cache_enabled 与 enable/disable_cache — tilelang/env.py:418-428](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/env.py#L418-L428) — `is_cache_enabled()` 先调 `is_cache_globally_disabled()`（读 `TILELANG_DISABLE_CACHE`），再 `and CacheState.is_enabled()`；`enable_cache`/`disable_cache` 只代理 `CacheState`。

模块底部把这三个方法再导出为顶层函数，方便用户写 `tilelang.disable_cache()`：

[顶层导出 enable_cache/disable_cache/is_cache_enabled — tilelang/env.py:527-530](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/env.py#L527-L530) — 这正是 `import tilelang; tilelang.disable_cache()` 背后的实现。

与缓存相关的环境变量都集中定义在 `Environment` 类里，默认值一目了然：

[缓存相关环境变量 — tilelang/env.py:356-389](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/env.py#L356-L389) — 重点几个：`TILELANG_CACHE_DIR`（缓存根）、`TILELANG_TMP_DIR`（临时目录，默认在 cache 下的 `tmp`）、`TILELANG_DISABLE_CACHE`（高优先级禁用，注释明说「usually for unit testing / debugging」）、`TILELANG_KERNEL_CACHE_USE_LIB_STAMP`（把原生库内容哈希也加进缓存键）、`TILELANG_AUTO_TUNING_DISABLE_CACHE`（仅禁用 autotuner 缓存）。

#### 4.1.4 代码实践

**实践目标**：验证缓存开关对编译行为的控制。

**操作步骤**（「源码阅读型 + 待本地验证」）：

1. 在 Python 里执行下面的片段（需要可编译的 tilelang 环境）：

   ```python
   import tilelang
   import time

   # 假设 my_kernel 是一个 @tilelang.jit 装饰的 GEMM 函数（见 examples/quickstart.py）
   # 第一次编译（冷启动）
   t0 = time.perf_counter(); k1 = my_kernel.compile(M=1024, N=1024, K=1024, block_M=128, block_N=128, block_K=32); t1 = time.perf_counter()
   # 第二次：应命中 KernelCache
   t2 = time.perf_counter(); k2 = my_kernel.compile(M=1024, N=1024, K=1024, block_M=128, block_N=128, block_K=32); t3 = time.perf_counter()

   print("first  (compile): %.3fs" % (t1 - t0))
   print("second (cache)  : %.3fs" % (t3 - t2))

   # 现在禁用缓存再编译一次
   tilelang.disable_cache()
   t4 = time.perf_counter(); k3 = my_kernel.compile(M=1024, N=1024, K=1024, block_M=128, block_N=128, block_K=32); t5 = time.perf_counter()
   print("third  (no cache): %.3fs" % (t5 - t4))
   ```

2. 阅读源码确认：`disable_cache()` 把 `CacheState._enabled` 置 `False`，于是 `KernelCache.cached()` 开头的 `if not env.is_cache_enabled()` 分支直接构造一个**不缓存**的 `JITKernel` 返回。

**需要观察的现象**：第二次耗时远小于第一次（命中 ③ 内存缓存）；禁用后的第三次耗时回升到接近第一次（但可能仍略快，因为 ④ 的 `CUDABinaryCache` 仍生效——它由 `env.is_cache_enabled()` 独立判断，见 4.4）。

**预期结果**：第二次编译耗时显著下降；`disable_cache()` 后第三次耗时回升。具体数值**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：`TILELANG_DISABLE_CACHE=1` 和调用 `tilelang.disable_cache()` 都能让缓存失效，二者有什么区别？

**参考答案**：前者是环境变量级**硬开关**，进程启动前设置、优先级最高，`is_cache_globally_disabled()` 返回 `True`；后者是运行时 API，只把 `CacheState._enabled` 置 `False`（软开关）。若同时存在，二者都为「禁用」时缓存才完全关闭；`enable_cache()` 无法覆盖环境变量的硬禁用。

**练习 2**：`TILELANG_KERNEL_CACHE_USE_LIB_STAMP` 解决什么问题？默认为什么是关闭的？

**参考答案**：开发期 C++ Pass 改了生成的 kernel，但 `tilelang.__version__` 没变，旧的缓存键仍会命中 → 拿到过时产物（stale hit）。开启后会把 `libtilelang.so` 等原生库的 SHA-256 内容哈希加进键，库一变键就变。默认关闭是因为对普通用户而言「同版本 = 同产物」成立，算库哈希有额外开销，只在开发态才需要。

---

### 4.2 tilelang.jit：JIT 层会话级内存缓存

#### 4.2.1 概念说明

第 4.1 讲的是「全局开关」，本节讲 ①② 两层缓存。它们位于 `JITImpl`（`@tilelang.jit` 装饰器返回的对象）内部，是**会话级、纯内存**的——只对「同一个被装饰函数、同一个 Python 进程」有效，进程退出即失效。它们存在的意义是：当你反复用同样参数调用同一个 `@tilelang.jit` 函数时，连「去查磁盘缓存」都省了，直接返回上次编好的 kernel 对象。

两层缓存的分工：

- **`_call_form_cache`（① `_CallFormCache`）**：仅 lazy 模式、且函数**没有 tensor 参数**时启用。键是 Python 调用的「原始形式」（位置参数 + 关键字参数），命中后直接返回 `Kernel` 对象，连参数绑定都跳过。它是为「紧密循环里反复 `kernel = jit_fn(1024, 1024, ...)`」设计的最快路径。
- **`_kernel_cache`（②）**：通用层。键是 `func.parse_args(...)` 返回的 `(p1_key, p2_key)`，分别捕获「编译期参数」（如 `M/N/K/block_M`）和「运行期张量形状」（phase2）。命中则返回 `JITKernel`。

#### 4.2.2 核心流程

`JITImpl.__call__` 的缓存查找顺序（伪代码）：

```
has_tune_params?  → 走 autotuner 分支
mode 推断为 lazy/eager
if lazy 且可用 call_form_cache:
    ① lookup(args, kwargs) → 命中则直接 return kernel
key, kernel_args = func.parse_args(args, kwargs)        # 得到 (p1_key, p2_key)
if key in ② _kernel_cache: return 它
# 都没中 → 真正编译
kernel = self.compile(args, kwargs)                      # → tilelang.compile → cached() → ③
② _kernel_cache[key] = kernel
（若 lazy 且无 kernel_args）① store(call_form_key, kernel)
eager: 执行 kernel 返回结果张量；lazy: 返回 kernel 对象
```

`parse_args` 内部还有一层 `JITFunc.p1_cache`（u4-l2 讲过），它缓存的是 **TIR 模板**（phase1 产物），使「同模板、不同 shape」不必重建 TIR，只需 phase2 替换。所以严格说 eager 路径是「TIR 模板缓存 → kernel 缓存」两级。

#### 4.2.3 源码精读

先看最快的 ① `_CallFormCache`。它是一个 dataclass，用一个 dict 加一个「上一次调用」的快速记忆位：

[\_CallFormCache 数据结构 — tilelang/jit/\_\_init\_\_.py:46-88](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/__init__.py#L46-L88) — 关键点：`lookup` 先走 `_matches_last`，若本次调用与上次完全相同就直接返回 `last_kernel`，**连 dict 查找和哈希都省了**（注释写明 "Fastest path for tight loops"）；否则才构造 `call_form_key = (args, tuple(kwargs.items()))` 去 `entries` 里查。`store` 在写入的同时更新「上一次」记忆。

`_CallFormKey` 的类型与哨兵值定义在类之前：

[\_CallFormKey 与 \_CALL\_FORM\_CACHE\_MISS — tilelang/jit/\_\_init\_\_.py:42-43](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/__init__.py#L42-L43) — 哨兵 `_CALL_FORM_CACHE_MISS = object()` 是一个独一无二的对象，用来区分「缓存里存了 `None`」和「缓存未命中」。

三层缓存在 `JITImpl.__post_init__` 里初始化：

[三个会话缓存初始化 — tilelang/jit/\_\_init\_\_.py:352-354](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/__init__.py#L352-L354) — `_kernel_cache`、`_call_form_cache`、`_tuner_cache` 都是**实例属性**，所以每个被装饰函数各有一份，互不干扰。

`__call__` 把三层串起来。先看 ① 的启用条件与查找：

[\_\_call\_\_ 中 call\_form\_cache 的启用与查找 — tilelang/jit/\_\_init\_\_.py:520-524](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/__init__.py#L520-L524) — 只有 `is_lazy_mode() and self._can_use_call_form_cache(has_tune_params)` 才查 ①。`_can_use_call_form_cache` 的判定见 [tilelang/jit/\_\_init\_\_.py:490-493](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/__init__.py#L490-L493)：要求**没有 tune 参数**、`func` 是 `JITFunc`、且**没有 tensor 参数**——因为这一层直接返回 kernel 对象，无法从中提取运行期 tensor。

接着是 ② `_kernel_cache`：

[\_\_call\_\_ 中 \_kernel\_cache 的查找与回填 — tilelang/jit/\_\_init\_\_.py:526-533](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/__init__.py#L526-L533) — `parse_args` 产出 `(key, kernel_args)`；`key` 没命中就调 `self.compile(...)` 真正编译（这一步会进入 4.3 的 `cached()`），再把结果回填到 ② 和（若适用）①。

② 的键由 `parse_args` 构造。`parse_cache_key` 是一个简化版（供 autotuner 用），真正的键在 `JITFunc.parse_args`：

[parse\_args 返回 (p1\_key, p2\_key) — tilelang/language/eager/builder.py:1437-1450](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/eager/builder.py#L1437-L1450) — 没有 tensor 参数时键是 `(p1_key, None)`；有 tensor 时 `p1_key` 来自参数绑定（compile kwargs，如 `M/N/K`），先查 `p1_cache` 拿到 TIR 模板，再用 `tir_temp._parse_phase2_key(...)` 算出 `p2_key`（实际张量形状），最终键为 `(p1_key, p2_key)`。这意味着 **eager 模式下「同编译参数、不同 shape」会落在不同的 ② 条目，但共享同一份 TIR 模板（p1_cache）**。

`compile()` 函数本身只是把参数归集后转发给 `cached()`：

[compile() 委托给 cached() — tilelang/jit/\_\_init\_\_.py:161-170](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/__init__.py#L161-L170) — 注意它在转发前会从 PrimFunc 的 attrs 里合并 `tilelang_out_idx`/`tilelang_pass_configs`/`tilelang_compile_flags`，保证用户写在 DSL 里的配置进入缓存键。

#### 4.2.4 代码实践

**实践目标**：观察 ① 与 ② 的命中差异。

**操作步骤**（源码阅读型）：

1. 打开 [tilelang/jit/\_\_init\_\_.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/__init__.py)，在 `__call__` 第 521 行（call_form_cache 查找）和第 527 行（`_kernel_cache.get`）各加一行 `logger.debug`（**只读分析，不改业务逻辑**）。
2. 写一个 lazy 风格的 `@tilelang.jit` GEMM（函数内嵌套 `@T.prim_func` 并 return 它），连续调用 3 次 `(1024,1024,1024,128,128,32)`。
3. 写一个 eager 风格的版本（用 `T.const` 与 `T.Tensor` 注解），先传 shape `(1024,1024)` 再传 `(2048,2048)`。

**需要观察的现象**：

- lazy 版本：第 1 次两层都 miss → 编译；第 2 次命中 ①（`_matches_last` 快路径）；第 3 次也命中 ①。
- eager 版本：两次 shape 不同 → ② 命中不同条目，但 `p1_cache` 命中同一 TIR 模板。

**预期结果**：能在日志里看到「第 2 次起进入 ① 快路径」「eager 换 shape 后 ② miss 但 p1_cache 命中」。具体日志输出**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `_call_form_cache` 只在「没有 tensor 参数」时才启用？

**参考答案**：因为这一层命中后**直接返回 `Kernel` 对象**，跳过了 `parse_args`，也就拿不到调用方传入的 tensor。lazy 模式下 `kernel = jit_fn(...)` 返回的是 kernel 对象、tensor 要等到 `kernel(a, b)` 才传入，所以「无 tensor 的调用形式」可以安全缓存；eager 模式调用本身就带 tensor，必须走 `parse_args` 提取，不能走这一层。

**练习 2**：连续两次 `jit_fn(1024,1024,1024,128,128,32)`（lazy），第二次为什么连 dict 查找都省了？

**参考答案**：`_CallFormCache.lookup` 先调 `_matches_last`，比较 `(args, kwargs)` 是否与 `last_args/last_kwargs` 完全相等；相等就直接返回 `last_kernel`，不必构造 `call_form_key` 也不必哈希查 dict。紧密循环里反复同参调用时这省下的开销很可观。

---

### 4.3 tilelang.cache：KernelCache 与 cached() 装饰器

#### 4.3.1 概念说明

第 ② 层 miss 后，`compile()` 调用 `tilelang.compile()` → `tilelang.cache.cached()`，进入第三层 `KernelCache`。这是 tilelang 缓存的**主力**：它有两级（进程内存 `_memory_cache` + 磁盘），且**跨进程持久化**——今天编译过的 kernel，明天新开一个 Python 进程也能从磁盘直接加载，不必再跑 Pass、不必再调 nvcc。

`KernelCache` 是个**单例**，且按 execution_backend 分发：`tvm_ffi`/`cython`/`nvrtc`/`torch`/`cutedsl` 各有一个子类单例（因为不同后端保存的「库」格式不同，比如 tvm_ffi 存 `executable.so`、CUDA 存 `kernel_lib.so` + cubin）。`cached()` 函数根据 target 与 execution_backend 选出对应单例，再调它的 `cached()` 方法。

#### 4.3.2 核心流程

`KernelCache.cached()` 的完整判定（伪代码）：

```
if not env.is_cache_enabled():                # 4.1 的总开关
    return JITKernel(...)                     # 不缓存，直接编译返回
key = _generate_key(func, out_idx, backend, args, target, target_host, pass_configs, compile_flags)
                                               # → SHA256(JSON{ func哈希, out_idx, args_repr,
                                               #              target, target_host, backend,
                                               #              pass_configs, compile_flags, 版本, [库stamp] })
if key in _memory_cache:  return 它           # ③ 内存命中（进程内）
kernel = _load_kernel_from_disk(key, ...)     # ③ 磁盘命中（跨进程）
if kernel:  _memory_cache[key] = kernel; return 它
# 全 miss → 真正编译
kernel = JITKernel(func, ...)                 # 内部触发 lower() → ... → ④ CUDABinaryCache
_save_kernel_to_disk(key, kernel, ...)        # 原子写：staging 目录 → rename
_set_adapter_cache_path(kernel, cache_path)   # 让 adapter 首次执行后能存 cubin
_memory_cache[key] = kernel
return kernel
```

缓存目录布局（命名空间隔离）：

```
$TILELANG_CACHE_DIR/
└── <tilelang 版本>/                ← _format_version_namespace，如 1.2.3_cuda_gitabc
    └── <平台>-<架构>/              ← 如 linux-x86_64 / win32-amd64
        ├── kernels/<key>/          ← KernelCache 的每个 kernel 一个目录
        │   ├── device_kernel.cu
        │   ├── host_kernel.cu
        │   ├── executable.so       ← tvm_ffi 后端（其它后端为 kernel_lib.so）
        │   ├── params.pkl          ← cloudpickle 序列化的 KernelParam 列表
        │   └── resource_usage.json ←（HIP 才有）
        ├── cuda-binaries/<key>.cubin   ← CUDABinaryCache（见 4.4）
        └── .staging/                ← 原子写的临时区，进程崩溃会留残骸，定期清理
```

命名空间把「版本」和「平台」拼进路径，因此**升级 tilelang 或换机器不会读到对方的缓存**，物理上就隔开了。

#### 4.3.3 源码精读

入口 `cached()` 与分发：

[cached() 装饰器与 \_resolve\_cache\_dispatch — tilelang/cache/\_\_init\_\_.py:67-92](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cache/__init__.py#L67-L92) — 它先调 `_resolve_cache_dispatch` 解析 target/execution_backend/verbose（None 时回退到 `env` 默认值），再用解析结果选单例。注释强调：环境变量处理、target 归一化、backend 解析**只应在这里发生一次**，所有编译路径都汇聚于此。

分发用的单例表：

[\_dispatch\_map：每个 execution backend 一个单例 — tilelang/cache/\_\_init\_\_.py:23-29](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cache/__init__.py#L23-L29) — 这五个对象在模块导入时就建好，全进程共享。

`_generate_key` 是理解「何时失效」的钥匙：

[\_generate\_key 缓存键构造 — tilelang/cache/kernel_cache.py:241-282](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cache/kernel_cache.py#L241-L282) — 键的 JSON 字典包含：`func`（用 `func.script(show_meta=True)` 的 SHA-256，即整个 TIR 文本）、`out_idx`、`args_repr`（每个参数的 `repr`）、`target`/`target_host`、`execution_backend`、`pass_configs`、`compile_flags`，再 `**_get_base_key()` 拼上版本与可选库 stamp（macOS 还加 torch 版本）。最后 `json.dumps(..., sort_keys=True)` 再 SHA-256。**任何一个字段变了，键就变，就视为需要重新编译。**

基础键（版本 + 库 stamp）：

[\_get\_base\_key — tilelang/cache/kernel_cache.py:136-147](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cache/kernel_cache.py#L136-L147) — 默认只有 `version`；开启 `TILELANG_KERNEL_CACHE_USE_LIB_STAMP` 后追加 `tilelang_lib`（原生库内容哈希）；macOS 额外追加 torch 版本（因为 mac 上 host 库要和 torch 链接）。

命名空间（决定磁盘目录）：

[\_get\_cache\_namespace — tilelang/cache/kernel_cache.py:164-171](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cache/kernel_cache.py#L164-L171) — `<version>/<platform>-<machine>`，用 `@functools.cache` 缓存（同进程内只算一次）。注意它和「缓存键」是两回事：命名空间决定**目录**，缓存键决定**目录下的子目录名**。

`cached()` 主体，三段式（总开关 → 内存 → 磁盘 → 编译 → 回填）：

[cached() 总开关与内存命中 — tilelang/cache/kernel_cache.py:326-361](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cache/kernel_cache.py#L326-L361) — `is_cache_enabled()` 为假时直接构造不缓存的 `JITKernel` 返回；否则算 key、查 `_memory_cache`（命中会打 warning 提示「建议用 `@tilelang.jit`」，因为直接走 compile 缓存比走 JIT 慢）。

[cached() 磁盘命中 — tilelang/cache/kernel_cache.py:363-379](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cache/kernel_cache.py#L363-L379) — 磁盘加载放在全局锁**外面**（注释说明大 kernel 集合的磁盘 IO 很重，放锁外可让独立命中并行）。

[cached() cache miss 编译与回填 — tilelang/cache/kernel_cache.py:381-418](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cache/kernel_cache.py#L381-L418) — miss 时用 `jit_phase("cache.compile", ...)` 包裹编译（供 verbose 诊断），编译完存盘、给 adapter 设缓存路径、回填内存。

磁盘落盘是**原子**的（防崩溃产生半成品缓存）：

[\_save\_kernel\_to\_disk 原子写 — tilelang/cache/kernel_cache.py:475-547](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cache/kernel_cache.py#L475-L547) — 先写到 `.staging/<key>_<pid>_<uuid>` 临时目录，所有文件齐了再 `os.rename` 成正式目录（POSIX rename 原子）。若目标已存在（别的进程赢了竞争）就删掉自己的 staging。写之前还会 `_remove_incomplete_cache_dir` 清理旧残骸。

加载时校验「完整性」，缺文件就当 miss：

[\_load\_kernel\_from\_disk 与完整性校验 — tilelang/cache/kernel_cache.py:577-588](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cache/kernel_cache.py#L577-L588) — `_get_missing_complete_cache_files` 检查必需文件（device/host 源码、库、params.pkl）是否齐全；缺任一就返回 `None`。若加载过程抛异常（如 `.so` 损坏），还会把整个目录 `rmtree` 当 miss 处理（见 [kernel_cache.py:613-621](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cache/kernel_cache.py#L613-L621)），实现「自愈」。

必需文件清单与完整性判定：

[\_get\_complete\_cache\_files / \_is\_complete\_cache\_dir — tilelang/cache/kernel_cache.py:676-691](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cache/kernel_cache.py#L676-L691) — `device_kernel.cu`、`host_kernel.cu`、库文件、`params.pkl` 缺一不可。

命中后如何「不重编译」就拿到可调用 kernel？靠 `JITKernel.from_database`：

[from\_database：从缓存重建 JITKernel — tilelang/jit/kernel.py:145-186](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/kernel.py#L145-L186) — 它用 `from_database=True` 构造实例（跳过 `_compile_and_create_adapter`，即不调 lower、不调 codegen），再调 `_create_adapter_from_database` 从已缓存的源码 + `.so` + params 重建 adapter。这正是磁盘缓存「秒级返回」的原因。

不同后端保存的「库」不同，靠子类特化。以 tvm_ffi 为例：

[TVMFFIKernelCache 特化 — tilelang/jit/adapter/kernel_cache.py:7-30](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/adapter/kernel_cache.py#L7-L30) — 它把 `kernel_lib_path` 改成 `executable.so`，并重写 `_save_so_cubin_to_disk`：用 `adapter.get_exportable_executable()` + `export_library` 导出 TVM Executable（而不是裸 cubin）。`_save_wrapper_kernel_code_to_disk` 也改成存 host 源码。

#### 4.3.4 代码实践

**实践目标**：亲眼看到磁盘缓存的产生与跨进程复用。

**操作步骤**（待本地验证）：

1. 设一个干净的缓存目录并编译一次：

   ```bash
   export TILELANG_CACHE_DIR=/tmp/tl_cache_demo
   rm -rf /tmp/tl_cache_demo
   python examples/quickstart.py    # 第一次：会编译，日志打印 "TileLang begins to compile kernel"
   ```

2. 查看产生的目录结构：

   ```bash
   find /tmp/tl_cache_demo -type f | sort
   ```

3. **新开一个进程**再跑一次（模拟「第二天」）：

   ```bash
   python examples/quickstart.py    # 第二次：应命中磁盘缓存，不再打印 "begins to compile"
   ```

4. 手动破坏一个缓存条目（删掉某个 `params.pkl`），再跑一次，观察自愈：

   ```bash
   find /tmp/tl_cache_demo -name params.pkl -delete
   python examples/quickstart.py    # 该条目被判为 incomplete → 当 miss → 重新编译并补回
   ```

**需要观察的现象**：

- 第 1 步后能看到 `.../kernels/<64位hash>/{device_kernel.cu,host_kernel.cu,executable.so,params.pkl}` 以及 `.../cuda-binaries/<hash>.cubin`。
- 第 3 步启动明显加快，且日志里没有 "begins to compile kernel"。
- 第 4 步后该 hash 目录被重建。

**预期结果**：磁盘缓存跨进程命中；删除必需文件后自动重编译。具体耗时**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：下列哪种改动**不会**让 `KernelCache` 的缓存键变化（即仍命中旧缓存）？

(a) 把 `block_M` 从 128 改成 64；(b) 把 `target` 从 `cuda` 改成 `cuda` 且 `arch=sm_80`；(c) 只改 kernel 函数里的注释；(d) 升级 tilelang 版本。

**参考答案**：**(c)**。键里的 `func` 是 `func.script(show_meta=True)` 的哈希，注释不在 TIR script 里，所以不影响键。(a) 改变 TIR（形状/常量）→ 键变；(b) target 字符串变 → 键变；(d) 版本变 → 命名空间目录都换了，根本不会查旧目录。

**练习 2**：为什么 `_save_kernel_to_disk` 要先写 staging 目录再 `os.rename`，而不是直接写正式目录？

**参考答案**：防崩溃产生**半成品缓存**。若直接写正式目录，写到一半进程被杀，下次加载时 `_get_missing_complete_cache_files` 会发现缺文件——虽然能当 miss 处理，但残骸留在磁盘。更糟的是若两个进程同时写同一 key，直接写会互相覆盖产生损坏文件。staging + 原子 rename 保证「要么完整可见、要么完全不可见」，且 rename 是原子的、竞争失败方自行清理。

**练习 3**：`_load_kernel_from_disk` 在加载抛异常时为什么 `rmtree(cache_path)`？

**参考答案**：实现**自愈**。`.so` 损坏、cloudpickle 反序列化失败等异常说明这条缓存已不可用，与其每次都尝试加载再失败，不如直接删掉，下次当 miss 重新编译补回。测试 `test_disk_cache_load_failure_is_cache_miss` 正是验证这一点。

---

### 4.4 tilelang.cache：CUDABinaryCache 设备二进制缓存

#### 4.4.1 概念说明

第四层 `CUDABinaryCache` 位于编译链路的最深处：`tilelang_callback_cuda_compile` 回调里。它缓存的是 `nvcc` 编译出的**裸设备二进制**（cubin 或 fatbin），与 host 封装完全无关。它解决两个问题：

1. **跨 host 复用**：host 侧的 `.so` 可能因为 adapter、wrapper 代码变化而重编，但只要设备源码和编译选项没变，cubin 就能直接复用，跳过昂贵的 nvcc 调用。
2. **选项隔离**：同一份 CUDA 源码，加不加 `--use_fast_math`、`--ptxas-options` 会生成**不同的 SASS**。如果只按源码哈希缓存，fast-math 编译就会被精确数学的旧产物「假命中」。因此编译选项**必须进键**。

#### 4.4.2 核心流程

`tilelang_callback_cuda_compile` 的伪代码：

```
算 arch / gencode / compile_format（多 code → fatbin，否则 cubin）
收集 options：-std=c++20、include 路径、(可选) --use_fast_math、ptxas 选项、extra flags
key = CUDABinaryCache.make_key(code, target_kind, target_arch, target_code,
                               compile_format, options)   # 选项进键！
bin = CUDABinaryCache.load(key, compile_format)
if bin is not None: return bin                            # 命中 → 跳过 nvcc
bin = nvcc.compile_cuda(code, compile_format, arch, options)   # 真正调 nvcc
CUDABinaryCache.save(key, compile_format, bin)
return bin
```

`make_key` 的字典结构与 `KernelCache._generate_key` 类似，但**专注于设备编译输入**：`code_hash`、`target_kind/arch/code`、`compile_format`、`options`（元组）、`tilelang_version`、可选库 stamp。

#### 4.4.3 源码精读

回调注册与主体：

[tilelang\_callback\_cuda\_compile — tilelang/engine/lower.py:101-175](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/engine/lower.py#L101-L175) — 注意它被 `@tvm_ffi.register_global_func("tilelang_callback_cuda_compile", override=True)` 注册成 TVM 全局函数，C++ 侧 codegen 会回调它来把设备源码编成二进制。第 110 行决定 `compile_format`：多个 target code 时用 `fatbin`（含多架构），单个用 `cubin`。

CUDABinaryCache 的三步使用（make_key → load 短路 → save）：

[CUDABinaryCache 在回调中的使用 — tilelang/engine/lower.py:152-173](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/engine/lower.py#L152-L173) — `load` 命中就直接 `return bytearray(cached_binary)`，**完全跳过 nvcc**；miss 才调 `nvcc.compile_cuda`，编完 `save`。注意 `options` 传进了 `make_key`。

`make_key` 为什么必须包含 options——源码注释直接解释了：

[make\_key：编译选项进键的注释与实现 — tilelang/cache/cuda\_binary\_cache.py:89-118](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cache/cuda_binary_cache.py#L89-L118) — 注释原文："flags like `--use_fast_math` change the generated SASS without changing the CUDA source, so keying on the code hash alone lets a fast-math binary satisfy a precise-math compile (and vice versa)." 因此 `options` 作为元组进入 `key_data`。

`load`/`save` 都先检查总开关：

[load — tilelang/cache/cuda\_binary\_cache.py:125-134](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cache/cuda_binary_cache.py#L125-L134) 与 [save — tilelang/cache/cuda\_binary\_cache.py:136-148](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cache/cuda_binary_cache.py#L136-L148) — `if not env.is_cache_enabled(): return` / `return None`，所以 4.1 的 `disable_cache()` 同时也会关闭 ④。`save` 同样用临时文件 + `os.replace` 原子写，文件名就是 `<key>.<compile_format>`（如 `<hash>.cubin`），落在 `cuda-binaries/` 子目录：

[\_get\_cache\_root 与 get\_path — tilelang/cache/cuda\_binary\_cache.py:42-44,121-123](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cache/cuda_binary_cache.py#L42-L44) — `cuda-binaries/` 与 `KernelCache` 的 `kernels/` 平级，都在同一命名空间下，因此版本/平台隔离对它同样生效。

#### 4.4.4 代码实践

**实践目标**：验证「同源码、不同选项」不会假命中。

**操作步骤**（阅读已有测试 + 待本地验证）：

1. 阅读 [testing/python/cache/test\_tilelang\_cuda\_binary\_cache.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/testing/python/cache/test_tilelang_cuda_binary_cache.py)，重点看 `test_cuda_binary_cache_hit_skips_nvcc_compile`（第 38-74 行）。

2. 该测试用 `monkeypatch` 把 `nvcc.compile_cuda` 替换成 `fake_compile_cuda`（每次调用记录到 `compile_calls`），然后对**同一份源码**调用四次回调：

   ```python
   first  = callback(source, target)                          # 编译（call #1）
   second = callback(source, target)                          # 命中缓存
   third  = callback(source, target, fast_math_pass_configs)  # 选项不同 → 重编译（call #2）
   fourth = callback(source, target, fast_math_pass_configs)  # 命中缓存
   ```

3. 在本地运行该测试（需要 CUDA 环境）：

   ```bash
   pytest testing/python/cache/test_tilelang_cuda_binary_cache.py -v
   ```

**需要观察的现象**：断言 `len(compile_calls) == 2`（只编译两次：一次默认选项、一次 fast-math），且两次的 options 元组不同；磁盘上应出现**两个** `.cubin` 文件（`assert len(cache_files) == 2`）。

**预期结果**：fast-math 与精确数学各产生一个缓存条目，互不污染。无 GPU 环境下测试会跳过，结果**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `CUDABinaryCache` 与 `KernelCache` 要分成两层，而不是合并？

**参考答案**：它们的**失效粒度**不同。`KernelCache` 键里有 host 侧的 adapter/wrapper 信息、pass_configs 等，host 一变就要重编 `.so`；但 cubin 只依赖设备源码 + 编译选项，host 变化不应让 cubin 失效。分两层后，host 重编时仍能复用旧 cubin，省掉最贵的 nvcc 调用。

**练习 2**：`tilelang_callback_hip_compile`（HIP 后端）有没有类似 `CUDABinaryCache` 的缓存？

**参考答案**：从 [tilelang/engine/lower.py:178-195](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/engine/lower.py#L178-L195) 看，HIP 回调目前**没有**在内部做 hsaco 二进制缓存（直接 `hipcc.compile_hip` 后返回）。HIP 仍靠外层 `KernelCache` 的 `kernel_lib.so` + `resource_usage.json` 复用整体产物。这是 CUDA 与 HIP 后端在缓存上的一个差异。

---

## 5. 综合实践

**任务**：用本讲四层缓存的知识，解释并实测「同一个 GEMM kernel 在不同场景下的编译耗时差异」。

准备一个 lazy 风格的 GEMM（可直接用 `examples/quickstart.py` 改写为 `@tilelang.jit` lazy 版）。然后依次完成：

1. **冷启动基线**：清空 `TILELANG_CACHE_DIR`，新开进程编译一次，记录耗时 `t_cold`。事后用 `find` 列出产生的 `kernels/` 与 `cuda-binaries/` 文件。

2. **同进程二次调用**：在**同一进程**里用相同参数再调一次 `jit_fn(...)`，记录 `t_warm1`。结合源码说明它命中了 ① `_call_form_cache`（哪几行代码？）。

3. **同进程换 shape（eager 版）**：写一个 eager 版 GEMM，先 `(1024,1024)` 再 `(2048,2048)`，说明第二次 ② `_kernel_cache` miss 但 `p1_cache` 命中，因此不必重建 TIR 模板。

4. **跨进程命中**：新开进程、**不清缓存**，再编译同一个 kernel，记录 `t_cross`。说明它命中 ③ `KernelCache` 的磁盘缓存，走 `from_database` 重建（不调 lower、不调 nvcc）。

5. **禁用对比**：`export TILELANG_DISABLE_CACHE=1` 后新开进程编译，记录 `t_disabled`，应接近 `t_cold`。再用 `tilelang.disable_cache()`（软开关）对比，观察它与硬开关的差别。

6. **画一张表**：把 `t_cold / t_warm1 / t_cross / t_disabled` 与「命中的是哪一层（①②③④ 或全 miss）」列出来。

**验收标准**：能用源码行号解释每一步命中了哪一层；能说清 `disable_cache()` 软开关与 `TILELANG_DISABLE_CACHE` 硬开关在 ③ 和 ④ 上的不同效果（提示：二者都经 `env.is_cache_enabled()` 关闭 ③ 与 ④）。具体耗时数值**待本地验证**。

## 6. 本讲小结

- tilelang 的编译缓存是**四层叠加**：① `_CallFormCache`（lazy 无 tensor 的最快路径）→ ② `_kernel_cache`（按 `(p1_key,p2_key)` 的会话内存缓存）→ ③ `KernelCache`（进程内存 + 跨进程磁盘，按 `_generate_key`）→ ④ `CUDABinaryCache`（cubin/fatbin 设备二进制）。由近及远，作用域递增、速度递减。
- **缓存键决定一切**：③ 的键是 TIR script 哈希 + out_idx + target + backend + pass_configs + compile_flags + 版本（+ 可选库 stamp）；④ 的键是源码哈希 + target + 编译选项。**编译选项必须进 ④ 的键**，否则 fast-math 与精确数学会假命中。
- **命名空间隔离**：磁盘目录按 `<版本>/<平台>-<架构>` 划分，升级版本或换机器物理上不会读到对方缓存。
- **健壮性设计**：原子写（staging + `os.rename`）防崩溃半成品；完整性校验（`_get_complete_cache_files`）+ 加载失败自愈（`rmtree` 当 miss）防损坏。
- **磁盘命中靠 `JITKernel.from_database`**：从缓存的源码 + `.so` + params 重建 adapter，跳过 lower 与 codegen，故能秒级返回。
- **总开关**：`env.is_cache_enabled()` = 非 `TILELANG_DISABLE_CACHE` 且 `CacheState._enabled`；`enable_cache()`/`disable_cache()` 是运行时软开关，环境变量是高优先级硬开关，二者共同控制 ③④。

## 7. 下一步学习建议

- **u6-1（Pass 系统与 PassConfigKey）**：本讲多次提到 `pass_configs` 进入缓存键。下一单元会讲 `PassConfigKey` 枚举与 `PassContext`，你会更清楚哪些配置会改变产物、从而影响缓存命中。
- **u8-1（Autotuner）**：autotuner 有自己独立的 `_memory_cache` + 磁盘缓存（`generate_cache_key`、`TILELANG_AUTO_TUNING_DISABLE_CACHE`），且其底层批量编译靠 `par_compile`（多线程并发走 `cached()`）。学完 u8 你能把缓存与调优串成一条线。
- **继续阅读源码**：想深入原子写与并发安全，可精读 `KernelCache._save_kernel_to_disk`（[kernel_cache.py:475-547](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cache/kernel_cache.py#L475-L547)）与 `_cleanup_stale_staging_dirs`；想理解多后端缓存差异，可对比 `tilelang/jit/adapter/{nvrtc,torch/cutedsl}/kernel_cache.py` 对 `KernelCache` 的各自特化。
