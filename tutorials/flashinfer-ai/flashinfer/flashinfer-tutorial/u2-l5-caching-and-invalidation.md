# 模块缓存与失效机制

## 1. 本讲目标

本讲是「JIT 编译系统」单元的收尾篇。前面几讲我们已经分别搞清楚了：JIT 的三层架构（u2-l1）、工作区路径与 `JitSpec`（u2-l2）、`gen_*_module` 的代码生成五步（u2-l3）、以及编译上下文与 CUDA 架构目标（u2-l4）。本讲要把这些零件拼成一张完整的图，回答三个「为什么要这样设计」的问题：

1. 为什么 FlashInfer 既有「进程内缓存」又有「磁盘缓存」？这两级缓存各自的键是什么、谁命中谁？
2. 当你改了 dtype、改了 GPU、改了源码、改了编译选项时，FlashInfer 分别靠什么机制判断「这个 kernel 必须重新编译」？
3. 多进程推理服务（例如多个 worker 进程）同时触发 JIT 时，为什么不会互相把对方的 `.so` 删掉？

学完本讲，你应该能够：准确说出一次 `flashinfer` API 调用会经过哪两级缓存、每一级的键由什么组成；在源码里定位到「缓存命中 / 失效」的判断点；并学会用环境变量与 CLI 管理缓存。

## 2. 前置知识

在进入源码前，先用通俗语言把两个 Python/构建领域的概念讲清楚。

**`@functools.cache` 是什么。** 它是 Python 标准库提供的装饰器，作用是「记答案」。被它装饰的函数，第一次以某组参数被调用时会真正执行，并把「参数 → 返回值」记在一个字典里；之后再以**完全相同**的参数调用时，直接返回字典里的旧答案，函数体根本不再执行。它的核心约束是：**参数必须是可哈希的**（不能是 list、dict 之类可变对象），因为字典要用参数当键。另外，这个字典「只进不出」——没有淘汰机制，而且只在**当前进程**存活，进程一退出就清空。

**ninja 的增量构建（incremental build）是什么。** ninja 是一个构建工具，读一个 `build.ninja` 文件，里面声明「从哪些输入（源文件、头文件）经过什么命令生成哪些输出（`.o`、`.so`）」。ninja 不会每次都把所有东西重新编译一遍，而是会比较**文件修改时间（mtime）**：只有当某个输入比它的输出更新时，才重新生成那个输出。为了让 ninja 知道「这个 `.cu` 实际上还依赖哪些 `.cuh` 头文件」，编译器会在编译时顺带产出一个 **depfile**（依赖文件，形如 `xxx.cuda.o.d`），里面列出真正被 `#include` 的所有头文件。ninja 下次据此判断头文件是否变了。这就是 u2-l1 提到的「编辑 `.cuh` 后自动重编译」的底层机制。

**关键术语速查。**

| 术语 | 含义 |
|------|------|
| 两级缓存 | 进程内 `@functools.cache`（第一级）+ 磁盘 `.so` 文件（第二级） |
| 缓存键 | 决定「命中」还是「失效」的那组信息；两级缓存的键不同 |
| URI / name | 模块的名字字符串，由 `get_*_uri` 函数生成，既当目录名又当 `.so` 文件名 |
| 编译期参数 vs 运行期参数 | 决定是否换 kernel 的参数（dtype/head_dim 等）vs 只影响一次调用结果的参数（kv_len/batch_size 等） |
| depfile | ninja 用来追踪头文件依赖的文件，是源码级失效的根据 |

## 3. 本讲源码地图

本讲聚焦三个文件，它们恰好对应两级缓存与失效判断的三条主线：

| 文件 | 作用 | 本讲关注点 |
|------|------|-----------|
| [flashinfer/decode.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/decode.py) | decode 注意力的 Python wrapper | `@functools.cache` 装饰的模块加载函数，以及它们的实参如何决定缓存键 |
| [flashinfer/jit/core.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/core.py) | `JitSpec` 数据类与编译/加载入口 | `is_aot` / `is_compiled` 判断、`build_and_load` 的加锁流程、`clear_cache_dir` |
| [flashinfer/jit/cpp_ext.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/cpp_ext.py) | ninja 构建文件生成与执行 | `generate_ninja_build_for_op` 里的 depfile 声明、`run_ninja` 的增量调用 |

辅助文件：[flashinfer/jit/env.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/env.py)（路径分层，u2-l2/u2-l4 已讲，本讲复用其结论）与 [flashinfer/jit/utils.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/utils.py)（`write_if_different` 幂等写入）。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：进程内缓存（`functools.cache`）、磁盘缓存（`.so` 路径键）、失效机制（五种变更各自由谁接管）。

### 4.1 进程内缓存：`@functools.cache` 与缓存键

#### 4.1.1 概念说明

第一级缓存发生在 **Python 进程内存里**。每个算子的「模块加载函数」都被 `@functools.cache` 装饰，作用是：同一个进程内，只要传入的**编译期参数组合**不变，第二次以后就直接返回上一次加载好的模块对象，连「去磁盘找 `.so`」这一步都省了。

为什么要这一级？因为一次推理服务可能在一秒内调用同一个 attention kernel 上千次（每个 token、每一层都调一次）。如果每次都走「查路径 → `tvm_ffi.load_module`」的磁盘加载，开销不可接受。进程内缓存把这上千次调用压缩成一次真正的加载。

关键认知：`@functools.cache` 的键就是**调用时的位置参数元组 `*args`**。所以「哪些参数算缓存键」完全等价于「这个函数被调用时传了哪些参数」。对 decode 而言，这些参数恰好就是 u1-l5 里强调的**编译期参数**（dtype、head_dim、posenc 等），而**不**包含 kv_len、batch_size 这类运行期形状——这正是「只改 batch_size 不会触发重编译」的根因。

#### 4.1.2 核心流程

以 `get_batch_decode_module` 为例，进程内命中判断可以写成伪代码：

```
get_batch_decode_module(dtype_q, dtype_kv, dtype_o, dtype_idx,
                        head_dim_qk, head_dim_vo, posenc,
                        use_swa, use_logits_cap):
    key = (上述 9 个参数组成的元组)        # functools.cache 自动用这个当键
    if key 在 functools_cache 里:
        return 缓存里的模块对象            # 命中：函数体不执行
    # 未命中：
    uri = get_batch_decode_uri(*同 9 个参数)   # 拼出描述性名字
    module = gen_batch_decode_module(*args).build_and_load()  # 走磁盘/编译
    注册 torch custom op
    把 module 存进 functools_cache[key]
    return module
```

两个要点：

1. **缓存键的粒度 = 编译期参数**。只要这 9 个参数中有任何一个变了（例如 dtype 从 f16 换成 bf16），key 就不同，触发未命中。
2. **缓存的是「已加载的模块对象」**，不是「要不要编译的判断」。也就是说，第一级缓存一旦命中，连 `is_compiled` 这种磁盘检查都不会发生。这也带来一个副作用：进程运行期间，如果你在磁盘上替换了 `.so`（例如重新编译），进程内缓存仍持有旧模块——必须重启进程才会加载新 `.so`。

#### 4.1.3 源码精读

在 [flashinfer/decode.py:232-235](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/decode.py#L232-L235) 可以看到 batch decode 的模块加载函数，它用 `*args` 接收全部编译期参数：

```python
@functools.cache
def get_batch_decode_module(*args):
    uri = get_batch_decode_uri(*args)
    mod = gen_batch_decode_module(*args).build_and_load()
    plan_func = mod.plan
    workspace_size_func = getattr(mod, "workspace_size", None)
    run_func = mod.run
    ...
```

`@functools.cache` 自动把 `args` 这个元组当作键。那么 `args` 到底是哪 9 个值？看 [flashinfer/decode.py:1510-1520](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/decode.py#L1510-L1520) 里 wrapper 在 `plan` 阶段调用它的实参：

```python
self._cached_module = get_batch_decode_module(
    q_data_type,
    kv_data_type,
    o_data_type,
    indptr.dtype,
    head_dim,  # head_dim_qk
    head_dim,  # head_dim_vo
    PosEncodingMode[pos_encoding_mode].value,
    window_left != -1,  # use_sliding_window
    logits_soft_cap > 0,  # use_logits_soft_cap
)
```

这就是缓存键的全部组成：3 个 dtype + 1 个索引 dtype + 2 个 head_dim + posenc + 是否滑窗 + 是否 soft cap。**注意 batch_size、kv_len、num_qo_heads 都不在里面**——它们是运行期参数，改变它们不会换 kernel、也不会让进程内缓存失效。

而名字字符串 `uri` 由 [flashinfer/jit/attention/modules.py:67-88](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/attention/modules.py#L67-L88) 生成，拼接的正是同样这组参数：

```python
def get_batch_decode_uri(dtype_q, dtype_kv, dtype_o, dtype_idx,
                         head_dim_qk, head_dim_vo, pos_encoding_mode,
                         use_sliding_window, use_logits_soft_cap) -> str:
    return (
        f"batch_decode_with_kv_cache_dtype_q_{filename_safe_dtype_map[dtype_q]}_"
        f"dtype_kv_{filename_safe_dtype_map[dtype_kv]}_"
        ...
        f"use_swa_{use_sliding_window}_"
        f"use_logits_cap_{use_logits_soft_cap}"
    )
```

> ⚠️ **一个容易被旧描述误导的点**：有的说法把 URI 描述成「`hash(操作类型 + 参数 + 源码哈希 + flags + arch)`」。但看真实源码，attention/activation 的 `uri` 是一段**纯描述性字符串**，只编码编译期参数，**既不含源码哈希，也不含 arch**。arch 和版本号住在「磁盘路径」里（见 4.2），源码变更由 ninja 接管（见 4.3）。把「缓存键」拆成「名字 + 路径 + 构建图」三处来理解，才和代码对得上。

同类装饰器还有几个，作用一致：[flashinfer/decode.py:91-92](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/decode.py#L91-L92)（`get_single_decode_module`）、[flashinfer/decode.py:153-154](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/decode.py#L153-L154)（`get_batch_decode_jit_module`）、[flashinfer/decode.py:411-413](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/decode.py#L411-L413)（`get_batch_decode_mla_module`）。

#### 4.1.4 代码实践

**目标**：亲手验证「进程内缓存只在编译期参数变化时失效」。

**步骤**：

1. 写一段最小脚本，连续调用两次 `single_decode_with_kv_cache`，**只改 kv_len**（运行期参数），不改 dtype/head_dim。
2. 用 `time.perf_counter()` 包住两次调用，打印耗时。
3. 第三次调用时把 q 的 dtype 从 `float16` 换成 `bfloat16`（编译期参数变化），再打印耗时。

```python
# 示例代码（非项目原有，仅作演示）
import time, torch, flashinfer

def call(dtype, kv_len):
    q = torch.randn(32, 128, dtype=dtype, device="cuda")
    k = torch.randn(kv_len, 32, 128, dtype=dtype, device="cuda")
    v = torch.randn(kv_len, 32, 128, dtype=dtype, device="cuda")
    t0 = time.perf_counter()
    flashinfer.single_decode_with_kv_cache(q, k, v)
    torch.cuda.synchronize()
    return time.perf_counter() - t0

print("第1次 f16 kv_len=4096:", call(torch.float16, 4096))   # 首次：触发 JIT 编译，慢
print("第2次 f16 kv_len=8192:", call(torch.float16, 8192))   # 命中进程内缓存，快
print("第3次 bf16 kv_len=4096:", call(torch.bfloat16, 4096)) # dtype 变了 → 新 key → 重新编译，慢
```

**预期现象**：第 1 次慢（编译 + 加载），第 2 次极快（进程内命中，连磁盘都不查），第 3 次又变慢（dtype 是缓存键的一部分，未命中，触发新一轮编译）。

**注意**：能否真正运行取决于本机是否有支持的 GPU 与已安装 flashinfer。如无法运行，请把第 2 次与第 3 次的差异作为「待本地验证」的预测，结合 4.1.3 的源码论证理解。

#### 4.1.5 小练习与答案

**练习 1**：如果你在同一个进程里，对 `num_qo_heads=32` 和 `num_qo_heads=64` 各调用一次 batch decode，会编译几次？为什么？

**答案**：**1 次**（假设 dtype/head_dim 等其余编译期参数相同）。因为 `num_qo_heads` 不在 `get_batch_decode_module` 的参数列表里（见 4.1.3 的调用点），它不参与缓存键，只作为运行期参数传给 `plan`/`run`。

**练习 2**：`@functools.cache` 装饰的函数如果在进程运行中被调用了 100 万次（参数组合相同），其中有多少次真正执行了函数体？

**答案**：**1 次**。第一次未命中执行并缓存，其余 999999 次直接返回缓存对象，函数体不执行。这就是两级缓存里「第一级」省下的开销。

### 4.2 磁盘缓存：`.so` 路径键与 `is_compiled` / `is_aot`

#### 4.2.1 概念说明

第二级缓存落在**磁盘上的 `.so` 文件**。它的作用是跨进程复用：进程 A 编译好的 kernel，进程 B 启动时可以直接加载，不必再编译一次。

磁盘缓存键不是单独一个字符串，而是「**版本 + 架构 + 名字**」三者拼出的完整目录路径。理解这一点要回到 u2-l2/u2-l4 讲过的工作区分层：

```
<FLASHINFER_WORKSPACE_BASE>/.cache/flashinfer/
└── <flashinfer_version>/          ← 版本号（来自 version.txt）
    └── <sorted_arch>/             ← 排序后的架构串，例如 80_89_90a
        ├── cached_ops/            ← FLASHINFER_JIT_DIR：编译产物 .so
        │   └── <name>/            ← URI 字符串
        │       ├── build.ninja
        │       ├── *.cuda.o
        │       └── <name>.so      ← 磁盘缓存的最终产物
        └── generated/             ← FLASHINFER_GEN_SRC_DIR：生成的 .cu/.inc
```

所以磁盘缓存键 \( K_{\text{disk}} \) 可以形式化为：

\[
K_{\text{disk}} = (\text{version},\ \text{sorted\_arch},\ \text{name})
\]

其中 `name` 又是编译期参数的描述串。三个分量任何一个变化，都会落到一个**全新目录**，等于「换了一把磁盘钥匙」，自然要重新编译。

#### 4.2.2 核心流程

`build_and_load()` 是磁盘这一级的总入口，它先判断是否已有现成 `.so`，再决定要不要编译：

```
JitSpec.build_and_load():
    if self.is_aot:                        # 预编译包里有现成 .so？
        return load(aot_path)              #   是 → 直接加载，零编译（AOT 短路）
    with FileLock(lock_path):              # 多进程互斥
        so_path = jit_library_path         # cached_ops/<name>/<name>.so
        verbose = (FLASHINFER_JIT_VERBOSE == "1")
        self.build(verbose, need_lock=False)   # write_ninja + run_ninja（增量）
        result = load(so_path)             # tvm_ffi 加载 .so
    return result
```

两个判断属性是核心：

- `is_aot`：只读的预编译包目录里有没有同名 `.so`。有就**短路**，根本不进 JIT 目录、不调 ninja。这是「装了 `flashinfer-jit-cache` 后启动飞快」的实现（u2-l2 已述）。
- `is_compiled`：JIT 目录里有没有同名 `.so`。注意它只看「文件存在」，**不区分**这个 `.so` 是上次编译的、还是别人拷进来的。

#### 4.2.3 源码精读

先看路径与判断属性的定义，全部在 [flashinfer/jit/core.py:228-265](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/core.py#L228-L265)：

```python
@property
def jit_library_path(self) -> Path:
    return jit_env.FLASHINFER_JIT_DIR / self.name / f"{self.name}.so"

def get_library_path(self) -> Path:
    if self.is_aot:
        return self.aot_path          # 只读 AOT 目录
    return self.jit_library_path       # 可写 JIT 目录

@property
def aot_path(self) -> Path:
    return jit_env.FLASHINFER_AOT_DIR / self.name / f"{self.name}.so"

@property
def is_aot(self) -> bool:
    return self.aot_path.exists()      # 只看 AOT 目录下有没有同名 .so

@property
def is_compiled(self) -> bool:
    return self.get_library_path().exists()   # 只看文件存在性
```

注意 `FLASHINFER_JIT_DIR` 已经把 version + sorted_arch 拼好了（[flashinfer/jit/env.py:148-149](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/env.py#L148-L149)）：

```python
FLASHINFER_WORKSPACE_DIR: pathlib.Path = _get_workspace_dir_name()  # cache_dir/version/sorted_arch
FLASHINFER_JIT_DIR: pathlib.Path = FLASHINFER_WORKSPACE_DIR / "cached_ops"
```

而 `sorted_arch` 由 [flashinfer/jit/env.py:135-144](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/env.py#L135-L144) 构造，`sorted()` 保证架构集合的目录名确定（避免 `75_80_89` 与 `89_75_80` 这种碎片）：

```python
def _get_workspace_dir_name() -> pathlib.Path:
    compilation_context = CompilationContext()
    arch = "_".join(
        f"{major}{minor}"
        for major, minor in sorted(compilation_context.TARGET_CUDA_ARCHS)
    )
    return FLASHINFER_CACHE_DIR / flashinfer_version / arch
```

再看 `build_and_load` 的加锁实现，[flashinfer/jit/core.py:307-319](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/core.py#L307-L319)：

```python
def build_and_load(self):
    if self.is_aot:
        return self.load(self.aot_path)
    # Guard both build and load with the same lock to avoid race condition
    # where another process is building the library and removes the .so file.
    with FileLock(self.lock_path, thread_local=False):
        so_path = self.jit_library_path
        verbose = os.environ.get("FLASHINFER_JIT_VERBOSE", "0") == "1"
        self.build(verbose, need_lock=False)
        result = self.load(so_path)
    return result
```

注释点明了 `FileLock` 的用途：把「build」和「load」包在**同一把锁**里，防止「进程 B 正在编译、中途把旧 `.so` 删了，而进程 A 此刻正好在 load」这种竞争。`lock_path` 是一个独立于 `.so` 的锁文件（[flashinfer/jit/core.py:268-269](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/core.py#L268-L269)），位于 `tmp/` 目录下。

#### 4.2.4 代码实践

**目标**：亲眼看到 `.so` 落在哪个路径、版本/架构如何嵌进路径。

**步骤**：

1. 在能运行 flashinfer 的环境里，触发一次 decode 编译。
2. 用 `flashinfer show-config` 打印出 `FLASHINFER_JIT_DIR` 的实际路径。
3. 用文件管理器或 `ls` 进入该路径，找到对应的 `<name>/<name>.so`，确认目录层级是 `cached_ops/<name>/`。
4. 查看同目录下的 `build.ninja`，确认里面引用的 `-gencode=` 架构与你当前 GPU 一致。

**预期结果**：你会看到形如 `~/.cache/flashinfer/0.6.x/<arch>/cached_ops/batch_decode_with_kv_cache_..._use_logits_cap_False/batch_decode_with_kv_cache_..._use_logits_cap_False.so` 的文件。

> 若本机无 GPU，这一步可改为「源码阅读型」：对照 [flashinfer/jit/env.py:148-150](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/env.py#L148-L150) 与 [flashinfer/jit/core.py:236-238](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/core.py#L236-L238)，手算出某个 `name="silu_and_mul"`、version=`0.6.0`、arch=`90a` 时的完整 `.so` 路径，标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：两台机器分别只有 A100（SM80）和 H100（SM90a），它们编译出的 decode `.so` 会落在同一个 `cached_ops/<name>/` 目录里吗？

**答案**：**不会**。`sorted_arch` 不同（`80` vs `90a`），工作区路径里的架构段不同，因此 `.so` 落在不同目录。这正是「换 GPU 架构 = 换磁盘缓存键」的体现，也解释了为什么 `.so` 不能跨架构拷贝共用。

**练习 2**：`is_compiled` 返回 `True` 是否意味着「这个 `.so` 一定是当前源码编译出来的、内容最新」？

**答案**：**不一定**。`is_compiled` 只调用 `.exists()`，只看文件在不在，**不校验内容/时间戳/源码哈希**。内容是否最新要靠 ninja 的增量构建来判断（见 4.3）。所以「登记 ≠ 编译」之外还有一句「编译过 ≠ 内容最新」。

### 4.3 失效机制：参数 / 版本 / 架构 / 源码 / flags 各自由谁接管

#### 4.3.1 概念说明

「缓存失效」就是判断「旧产物还能不能用，不能用就得重来」。本讲最关键的结论是：**FlashInfer 把不同类型的「变更」分摊给了三个不同的子系统**，没有一个统一的失效函数，而是各司其职：

| 变更类型 | 由谁检测 | 结果 |
|---------|---------|------|
| 编译期参数（dtype/head_dim/posenc/swa/logits_cap）变化 | `@functools.cache` 的键 + URI 名字 | 换 key → 新目录 → 重新编译 |
| CUDA 架构变化 | 工作区路径里的 `sorted_arch` 段 | 换路径 → 重新编译 |
| FlashInfer 版本变化 | 工作区路径里的 `version` 段 | 换路径 → 重新编译 |
| 源码（`.cuh`/`.cu`）变化 | **ninja 的 depfile + mtime 增量构建** | 同名同路径，但 ninja 重编受影响的目标并重新链接 |
| 编译选项（flags，如 `-O0` vs `-O3`、debug）变化 | ninja 记录的「命令指纹」 | 同名同路径，ninja 发现命令变了 → 重编 |

前三类靠「换名字/换路径」实现**目录级隔离**（互不污染、零冲突）；后两类靠 ninja 在**同一个目录内**做**增量重编**。这套分工正是 u2-l1 所说「实时重载依赖 ninja depfile 增量构建与 `@functools.cache` 进程边界协同」的具体落地。

需要特别记住的一条**进程边界**规则：ninja 在磁盘上把 `.so` 更新了，但**进程内 `@functools.cache` 还持有旧的已加载模块对象**。所以「改了源码、重启 ninja 重编」之后，要**重启 Python 进程**才会真正加载新 `.so`。这是开发循环里常见的坑。

#### 4.3.2 核心流程

把一次 `build_and_load`（进程内未命中时）拆开看，磁盘与失效是这样协同的：

```
进程内未命中 (4.1)
  → JitSpec.build_and_load()
      → if is_aot: 直接加载 AOT .so 并返回            # 预编译包短路
      → 否则进入 JIT 目录, 加 FileLock
      → spec.build():
            write_ninja()                              # 幂等重写 build.ninja
            run_ninja(build_dir, ninja_path, verbose)  # ← 失效判断在这里发生
      → load(jit_library_path)                         # 加载(可能刚重编出的).so
```

`run_ninja` 调用的是 `ninja -C <build_dir> -f <ninja_file>`。ninja 自己会做两件事：

1. 读 `.ninja_log` 里记录的「上次每条边用的命令」和「输出文件 mtime」，对比当前 `build.ninja` 里的命令与磁盘上源/头文件的 mtime；
2. 若命令变了（flags 变）或任一依赖文件更新（源码变），就重编对应 `.o`，再视需要重新链接 `.so`；若一切未变，几乎零耗时返回。

#### 4.3.3 源码精读

先看 ninja 构建文件是如何声明 depfile 与增量依赖的。[flashinfer/jit/cpp_ext.py:289-298](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/cpp_ext.py#L289-L298)：

```python
"rule compile",
"  command = $cxx_launcher $cxx -MMD -MF $out.d $cflags -c $in -o $out $post_cflags",
"  depfile = $out.d",
"  deps = gcc",
"",
"rule cuda_compile",
"  command = $nvcc_launcher $nvcc --generate-dependencies-with-compile -MF $out.d $cuda_cflags -c $in -o $out $cuda_post_cflags",
"  depfile = $out.d",
"  deps = gcc",
```

`-MMD -MF $out.d`（C++）和 `--generate-dependencies-with-compile -MF $out.d`（nvcc）就是让编译器**顺带写出 depfile**；`depfile = $out.d` + `deps = gcc` 告诉 ninja「按 depfile 追踪头文件依赖」。于是当你改了 `include/flashinfer/attention/decode.cuh`，ninja 会知道哪个 `.cu` include 了它，从而只重编那一个目标。

再看 `run_ninja` 如何把控制权交给 ninja，[flashinfer/jit/cpp_ext.py:351-380](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/cpp_ext.py#L351-L380)：

```python
def run_ninja(workdir: Path, ninja_file: Path, verbose: bool) -> None:
    workdir.mkdir(parents=True, exist_ok=True)
    command = ["ninja", "-v", "-C", str(workdir.resolve()),
               "-f", str(ninja_file.resolve())]
    num_workers = _get_num_workers()      # 读 MAX_JOBS
    if num_workers is not None:
        command += ["-j", str(num_workers)]
    ...
    subprocess.run(command, ..., check=True, text=True)
```

注意：**这里没有 Python 层的「是否需要重编」判断**——一切交给 ninja。FlashInfer 每次进程内未命中都会调一次 `run_ninja`，但因为 ninja 是增量的，未变更时它几乎是空操作。这就是「即使没改东西，第二次调用也比第一次快很多」的又一来源（除了进程内缓存之外）。

`write_ninja` 用 [flashinfer/jit/utils.py:22-30](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/utils.py#L22-L30) 的 `write_if_different` 幂等写入，避免每次都改写 `build.ninja` 的 mtime 而误触发重建：

```python
def write_if_different(path: pathlib.Path, content: str) -> None:
    if path.exists():
        with open(path, "r") as f:
            if f.read() == content:
                return              # 内容没变就不写，mtime 不动
    ...
    with open(path, "w") as f:
        f.write(content)
```

最后，缓存管理入口在 [flashinfer/jit/core.py:111-115](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/core.py#L111-L115)，对应 CLI 的 `flashinfer clear-cache`：

```python
def clear_cache_dir():
    if os.path.exists(jit_env.FLASHINFER_JIT_DIR):
        import shutil
        shutil.rmtree(jit_env.FLASHINFER_JIT_DIR)
```

它直接删掉整个 `cached_ops` 目录。配合环境变量即可灵活管理：

| 环境变量 | 作用 | 读取位置 |
|---------|------|---------|
| `FLASHINFER_WORKSPACE_BASE` | 把整个缓存根目录搬到别处（如 `/scratch`） | [env.py:51-53](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/env.py#L51-L53) |
| `FLASHINFER_DISABLE_JIT` | 置位后拒绝编译，找不到现成 `.so` 就报 `MissingJITCacheError` | [core.py:290-296](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/core.py#L290-L296) |
| `FLASHINFER_JIT_VERBOSE` | 打印 ninja 详细输出 | [core.py:315](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/core.py#L315) |
| `MAX_JOBS` | 限制 ninja 并行编译进程数（`-j`） | [cpp_ext.py:344-348](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/cpp_ext.py#L344-L348) |

#### 4.3.4 代码实践

**目标**：用 `rm` 清掉磁盘缓存，重跑同一个 API，对比是否重新编译；并观察「源码改了但进程不重启」的行为。

**步骤**：

1. 先跑一次 `single_decode_with_kv_cache`，记下耗时（含首次编译）。
2. 立刻再跑一次（**同进程**），确认极快（进程内缓存命中）。
3. 在**新终端/新进程**里执行清缓存：

   ```bash
   # 示例命令（操作前请确认路径，别误删）
   flashinfer clear-cache
   # 或手动：
   rm -rf ~/.cache/flashinfer/<version>/<arch>/cached_ops
   ```

4. 启动一个**新 Python 进程**，再跑同一个 API，记下耗时（应再次变慢，因为磁盘 `.so` 已被删，ninja 要重新编译）。
5. （可选，开发循环演示）在新进程跑过一次后，**不退出进程**，去改 `include/flashinfer/activation.cuh` 里某个注释，再在该进程内调一次对应 activation API；观察是否仍用旧模块（进程内缓存没失效）。然后重启进程，确认 ninja 检测到源码变化并重编。

**预期结果**：

- 步骤 1 慢（编译）。
- 步骤 2 极快（进程内命中）。
- 步骤 4 又慢（磁盘 `.so` 被删 → `is_compiled=False` → ninja 重新编译）。
- 步骤 5 不重启进程时用旧模块；重启后 ninja 因 depfile 检测到 `.cuh` 变化而重编——这印证了 4.3.1 的「进程边界」规则。

> 若本机无 GPU/无法编译，请把步骤 1/2/4/5 的耗时对比作为「待本地验证」的预测，并重点结合 [core.py:289-302](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/core.py#L289-L302) 与 [cpp_ext.py:289-298](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/cpp_ext.py#L289-L298) 讲清「为什么清缓存后必然重编、为什么改源码不重启不生效」。

#### 4.3.5 小练习与答案

**练习 1**：你只改了 `csrc/batch_decode.cu` 里的一行注释（不影响逻辑），重启进程后会重新编译吗？

**答案**：**会重新编译这一个 `.cu` 并重新链接 `.so`**。因为 ninja 按 mtime + depfile 判断，`.cu` 文件 mtime 变了就会被重编（注释也会改变文件内容与 mtime）。虽然产物行为不变，但 ninja 不会区分「逻辑变更」与「注释变更」。若想避免无谓重编，开发时应控制只改真正需要改的文件。

**练习 2**：把 `FLASHINFER_CUDA_ARCH_LIST` 从 `8.0 9.0a` 改成只留 `9.0a`，已有的 decode `.so` 会被复用吗？

**答案**：**不会**。`sorted_arch` 段从 `80_90a` 变成 `90a`，工作区路径变了，旧的 `.so` 在旧路径下，新路径下没有 `.so`，`is_compiled=False`，于是重新编译。旧目录会残留在磁盘上（不会自动清理），可用 `clear-cache` 整理。

**练习 3**：多 worker 进程同时首次调用同一个 decode，会不会各自编译一份、或互相覆盖？

**答案**：**不会**。`build_and_load` 用 `FileLock`（[core.py:313](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/core.py#L313)）把 build+load 包在同一把锁里。先抢到锁的进程编译并产出 `.so`，后到的进程拿锁后发现 ninja 判定无需重编（或直接 `is_compiled=True`），直接加载同一份 `.so`。

## 5. 综合实践

把本讲三个模块串成一个完整的「缓存追踪」小任务。

**任务**：写一个脚本，用 `flashinfer.BatchDecodeWithPagedKVCacheWrapper` 跑两轮 batch decode，并在中途故意制造一次「编译期参数变化」和一次「磁盘清缓存」，用日志和耗时把两级缓存与失效机制完整复现一遍。

**操作步骤**：

1. 构造一个 paged KV cache 与一组 page table，先以 `q_data_type=torch.float16` 调 `wrapper.plan(...)` + `wrapper.run(...)`，记录耗时。
2. 同进程内、同样参数再 `run` 一次，确认第二次几乎零开销（命中进程内缓存——注意 `run` 本身不重建模块，模块在 `plan` 时已通过 `get_batch_decode_module` 缓存）。
3. 把 `q_data_type` 换成 `torch.bfloat16` 重新 `plan` + `run`，确认又变慢（dtype 是缓存键，未命中 → 重新编译）。
4. 在 [flashinfer/decode.py:1510-1520](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/decode.py#L1510-L1520) 这一处下断点或加日志，打印每次进入 `get_batch_decode_module` 时的 9 个实参，**对照** [modules.py:67-88](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/attention/modules.py#L67-L88) 验证：参数如何拼成 URI、URI 又如何成为磁盘目录名。
5. 退出进程，执行 `flashinfer clear-cache`，再启动新进程跑 f16，确认再次编译（磁盘 `.so` 已删）。
6. 用 `ls ~/.cache/flashinfer/<version>/<arch>/cached_ops/` 观察每个 dtype/posenc 组合对应一个独立目录，直观验证「编译期参数 → 目录级隔离」。

**需要观察与总结的现象**（写入你的实验报告）：

- 两级缓存各自的命中边界（进程内 = 编译期参数元组；磁盘 = version+arch+name）。
- 五类变更分别由谁接管失效判断（参数/版本/架构 → 换路径；源码/flags → ninja 增量）。
- 「改源码必须重启进程」的进程边界现象。

**预期结果**：实验报告能复现「同参数第二次极快、换 dtype 变慢、清缓存后新进程变慢、改源码不重启不生效」四条结论，并能在源码里指出每条结论对应的代码位置。

> 若无 GPU 环境，请将步骤 1–6 降级为「源码阅读 + 路径手算」：固定一组编译期参数，手算出其 URI、`.so` 全路径，并指出改 dtype / 改 arch / 改源码时分别会落在哪个判定分支，标注「待本地验证」。

## 6. 本讲小结

- FlashInfer 用**两级缓存**摊薄 JIT 首次编译的开销：第一级是进程内 `@functools.cache`（键 = 编译期参数元组，命中则连磁盘都不查）；第二级是磁盘 `.so`（键 = version + sorted_arch + name，跨进程复用）。
- 进程内缓存的键只包含 dtype、head_dim、posenc、swa、logits_cap 等**编译期参数**；kv_len、batch_size、num_qo_heads 等**运行期参数**不参与缓存键，所以改它们不触发重编译。
- 磁盘 `.so` 的存在性由 `is_compiled`（`.exists()`）判断，预编译包由 `is_aot` 短路；二者都「只看文件在不在」，内容新鲜度交给 ninja。
- 失效判断被**分工**到三个子系统：参数/版本/架构变化靠「换名字或换路径」做目录级隔离；源码与 flags 变化靠 ninja 的 depfile + 命令指纹在同一目录内增量重编。
- 「改源码后必须重启 Python 进程」才会加载新 `.so`，因为进程内 `@functools.cache` 持有旧模块对象——这是开发循环里最常踩的坑。
- 多进程并发首编由 `FileLock` 保护 build+load，避免互相删除 `.so`；`clear-cache` / `FLASHINFER_WORKSPACE_BASE` / `FLASHINFER_DISABLE_JIT` / `MAX_JOBS` 是日常管理缓存的四个抓手。

## 7. 下一步学习建议

本讲把 JIT 编译系统讲完了。接下来两条路：

- **横向进入算子层**：第 3 单元「注意力基础」会把本讲的「模块加载 → plan → run」用到真实 attention wrapper 上。建议从 [u3-l3 BatchDecodeWithPagedKVCacheWrapper 的 plan/run](u3-l3-batch-decode-wrapper.md) 开始，你会再次见到 `get_batch_decode_module` 和 `self._cached_module`，本讲对缓存键的理解会让你看 plan/run 时更轻松。
- **纵向深入工程化**：如果你想了解「预编译所有模块」如何把磁盘缓存提前填好，可跳到 [u9-l4 AOT 编译与预编译包](u9-l4-aot-precompiled-packages.md)，那里会讲 `aot.py` 如何批量调用 `gen_*_module`，以及 `flashinfer-jit-cache` 包如何让 `is_aot` 大量命中、实现秒级启动。

建议继续精读的源码：在 [flashinfer/jit/core.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/core.py) 里通读 `JitSpec` 的全部 property 与 `build_jit_specs`（批量构建入口，[core.py:495-516](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/core.py#L495-L516)），把单模块与批量模块的缓存行为对照理解。
