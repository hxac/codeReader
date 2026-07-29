# tileiras 外部编译器调用与 cubin 生成

## 1. 本讲目标

本讲聚焦 TileIR 后端编译链路的「最后一公里」：把已经 lowering 完成的 CUDA Tile IR（cuda_tile 方言）交给来自 CUDA 工具链的**外部编译器 `tileiras`**，生成可被 GPU 加载执行的 `.cubin`。

学完本讲，你应该能够：

- 说清 `make_cubin` 如何委托 `call_tileiras`，以及 bytecode 是如何被序列化、缓存并交给外部进程的。
- 解释 `tileiras` 子进程的命令是如何拼出来的，以及 `CUDA_HOME` 为什么「从 `tileiras` 自身路径反推」而不是读系统环境变量。
- 区分两类编译失败：资源超限（`OutOfResources`，可被 autotuner 剪枝）与一般编译崩溃（`TileirasError`），并写出判别它们的正则表达式。

本讲承接 [u2-l3 三段式编译流水线](u2-l3-compile-stages.md)：`make_ttir`→`make_tileir` 产出的 cuda_tile IR，正是在本讲的 `make_cubin` 阶段离开 Triton、进入 NVIDIA 工具链的。

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

**为什么要有一个「外部编译器」？** Triton 自己能把 Python kernel 一路翻译到 cuda_tile 方言（这是本仓库 C++ 转换 Pass 的职责），但 cuda_tile 方言到 GPU 机器码（cubin）这一步，Triton 并不亲自做。它把这件事交给 NVIDIA 随 CUDA 工具链发布的 `tileiras`。这与 PTX 后端把 PTX 交给 `ptxas` 是同一种「各管一段」的分工。换言之：cuda_tile bytecode 是 Triton 与 NVIDIA 在这一层的**交接物**。

**bytecode 是什么？** MLIR 的 IR 可以有两种落盘形式：人类可读的文本（`.mlir`，即 `module { ... }` 那种）和紧凑的二进制 **bytecode**。`tileiras` 读取的是后者——它更小、解析更快、且格式稳定。所以本讲提到的「bytecode」特指 cuda_tile 方言 IR 的二进制序列化结果。

**cubin 是什么？** cubin（CUDA binary）是 NVIDIA GPU 的最终可执行产物，对应某个 SM 架构（如 `sm_100a`）。`TileIRDriver` 启动内核时加载的就是这个 cubin。

**子进程与 autotuner 剪枝。** `tileiras` 作为独立进程运行，可能因为「某个 autotune 配置要的共享内存/TMEM 超过硬件上限」而失败。这类失败是**可预期的、配置相关的**，应当被翻译成 `OutOfResources`，让 autotuner 把这个配置剪掉、继续试别的；而「`tileiras` 自己崩了（比如段错误）」则是不可预期的，翻译成 `TileirasError`。这条分类线是本讲错误处理的核心。

> 小术语对照
>
> | 术语 | 含义 |
> |------|------|
> | `tileiras` | NVIDIA 工具链里的外部编译器，cuda_tile bytecode → cubin |
> | bytecode | cuda_tile 方言 IR 的二进制序列化 |
> | cubin | GPU 最终可执行二进制 |
> | `ptxas` / `libnvvm` / `libdevice` | `tileiras` 内部依赖、需在 `$CUDA_HOME` 下定位的工具链组件 |
> | `OutOfResources` | 资源超限错误，可被 autotuner 剪枝 |
> | `TileirasError` | 一般性 `tileiras` 失败（含崩溃） |

## 3. 本讲源码地图

本讲涉及四个关键文件，分工如下：

| 文件 | 作用 |
|------|------|
| `third_party/tileir/backend/compiler.py` | `call_tileiras` 所在地：拼命令、写 bytecode、跑子进程、分类错误；也包含 `make_cubin` 入口 |
| `third_party/tileir/backend/conf.py` | `TileIREnvConf`：`tileiras` 路径的三级解析与 `CUDA_HOME` 的反推 |
| `python/triton/runtime/errors.py` | `OutOfResources` 与 `TileirasError` 的真正定义（注意：不是 `tileir` 目录下的 `errors.py`） |
| `third_party/tileir/triton_tileir.cc` | C++ pybind 层：`write_bytecode` 如何定位嵌套的 `cuda_tile::ModuleOp` 并序列化 |

> ⚠️ 容易踩的坑：`third_party/tileir/backend/errors.py` 里**只有** `HitFallback`，并没有 `OutOfResources`/`TileirasError`。后两者是在 `compiler.py` 顶部从 `triton.runtime.errors` 导入的。见 [third_party/tileir/backend/compiler.py:1](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/compiler.py#L1)。

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：**bytecode 输出** → **tileiras 命令与环境** → **错误分类**。这三者串起来就是 `call_tileiras` 的全部职责。

### 4.1 bytecode 输出：把 cuda_tile IR 序列化交给外部编译器

#### 4.1.1 概念说明

`make_cubin` 是三段式流水线的第三段（见 u2-l3），它本身只做一件事：把模块转交给 `call_tileiras`。

```python
@staticmethod
def make_cubin(mod, metadata, opt: TileIROptions, capability):
    return TileIRBackend.call_tileiras(mod, metadata, opt, capability)
```

参考 [third_party/tileir/backend/compiler.py:332-334](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/compiler.py#L332-L334)。

真正的工作在 `call_tileiras` 里。它要解决一个关键问题：**给 `tileiras` 喂什么？** 答案是 cuda_tile 方言的 bytecode。这里有个细节——传入的 `mod` 是外层 `mlir::ModuleOp`，而 `tileiras` 想要的是嵌在它里面的 `cuda_tile::ModuleOp`。因此序列化这一步必须先「找到内层那个 cuda_tile 模块」。

为什么 `tileiras` 看不到 Python 侧的编译旋钮（如 `occupancy`、`enable_approx`、`enable_ftz`）？因为这些旋钮早在 `make_tileir` 阶段就被**烘焙（bake）进了 IR**（见 u2-l2、u2-l3）。等执行到 `make_cubin` 时，旋钮已经是 IR 的一部分，`tileiras` 只看到最终 IR，命令行只接收架构与优化级别等少数全局参数。这一点是理解「为什么 tileiras 命令那么短」的关键。

#### 4.1.2 核心流程

bytecode 输出这一小步的执行流程：

```
make_cubin(mod, ...)
   └─> call_tileiras(mod, metadata, opt, capability)
          ├─ name      = metadata["name"]          # 内核名
          ├─ cache_mgr = get_cache_manager(hash)   # 拿到该内核的缓存管理器
          ├─ bytecode  = tileir.write_bytecode(mod) # C++ 序列化：定位嵌套 cuda_tile::ModuleOp
          ├─ cache_mgr.put(bytecode, "{name}.bytecode")  # 落盘缓存（便于复现/调试）
          └─ ... 后续构造 tileiras 命令（见 4.2）
```

要点：
1. 序列化由 C++ pybind 函数 `tileir.write_bytecode` 完成，Python 侧只拿到 `py::bytes`。
2. bytecode 同时被写进缓存目录（文件名 `{name}.bytecode`），这是为了出错时能复现——错误信息里的 repro 命令引用的就是这个文件。
3. bytecode 的**对象**是嵌套的 `cuda_tile::ModuleOp`，不是外层 `mlir::ModuleOp`。

#### 4.1.3 源码精读

Python 侧的 bytecode 写出，见 [third_party/tileir/backend/compiler.py:216-219](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/compiler.py#L216-L219)：

```python
# Save bytecode to cache
bytecode = tileir.write_bytecode(mod)
bytecode_cache_name = f"{name}.bytecode"
bytecode_file = fn_cache_manager.put(bytecode, bytecode_cache_name)
```

这里 `tileir` 是从 `triton._C.libtriton` 导入的 C++ 模块（见文件顶部第 6 行的 `from triton._C.libtriton import ir, passes, tileir`）。`write_bytecode` 的真正实现在 C++ 侧，见 [third_party/tileir/triton_tileir.cc:129-149](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/triton_tileir.cc#L129-L149)：

```cpp
m.def("write_bytecode", [](mlir::ModuleOp mod) {
  // Find the cuda_tile::ModuleOp within the mlir::ModuleOp.
  cuda_tile::ModuleOp cudaTileModule;
  if (!mod.getBody()->empty())
    if (auto nestedCudaTileModule =
            dyn_cast<cuda_tile::ModuleOp>(&mod.getBody()->front()))
      cudaTileModule = nestedCudaTileModule;

  if (!cudaTileModule)
    throw std::runtime_error(
        "No cuda_tile::ModuleOp found in the input module");
  // ...用 cuda_tile::writeBytecode 把 cudaTileModule 序列化成 py::bytes
});
```

这段代码做的事：取外层模块 body 的第一个操作（`mod.getBody()->front()`），若它是 `cuda_tile::ModuleOp` 就选定它；找不到就直接抛异常。这呼应了 `make_tileir` 里 `convert-triton-to-cuda-tile` 会「在外层 ModuleOp 内插入一个 cuda_tile 容器模块」的设计（见 u2-l3）。随后用 `cuda_tile::writeBytecode` 以 `kCurrentVersion` 序列化，返回 `py::bytes`。

> 关于 `write_bytecode` 如何定位嵌套模块、以及它与 cuda_tile 级 pass 的关系，更深入的剖析放在 [u3-l7 后处理与 bytecode 输出](u3-l7-fma-fusion-and-bytecode.md)。本讲只需知道：它产出 `tileiras` 的输入字节流。

#### 4.1.4 代码实践

**实践目标**：在本地缓存目录中亲眼看到 `tileiras` 的输入文件，并确认它是二进制 bytecode 而非文本 IR。

**操作步骤**：

1. 在 `ENABLE_TILE=1` 下编译任意一个 Triton kernel（如向量加法），让它正常跑通。
2. 找到 Triton 的缓存根目录（默认在 `~/.triton/cache/`，或由 `TRITON_CACHE_DIR` 指定）。
3. 在缓存目录里搜索 `*.bytecode` 文件。

**需要观察的现象**：

- 存在一个 `{kernel_name}.bytecode` 文件，与 `.ttir`、`.tileir`、`.cubin` 同目录。
- 用 `file` 命令或 `xxd | head` 查看它：它不是人类可读的 `module { ... }` 文本，而是以特定 magic 开头的二进制。

**预期结果**：`head -c 64 xxx.bytecode | xxd` 应输出一堆十六进制字节，而非 `module` 字样。这就证明 `tileiras` 吃的是 bytecode。

> 待本地验证：具体缓存路径与文件名以你机器上的实际输出为准；无 GPU/CUDA 13.1 环境时无法实际触发 `call_tileiras`，可改为纯源码阅读型实践——对照 4.1.3 的两段代码，画出「外层 ModuleOp → 嵌套 cuda_tile::ModuleOp → bytecode 字节 → `{name}.bytecode` 缓存文件」的数据流。

#### 4.1.5 小练习与答案

**练习 1**：如果 `convert-triton-to-cuda-tile` 因为 bug 没有插入嵌套的 `cuda_tile::ModuleOp`，`call_tileiras` 会在哪一步、以什么形式失败？

> **参考答案**：会在 `tileir.write_bytecode(mod)` 这一步失败。C++ 侧找不到 `cuda_tile::ModuleOp`，直接抛 `std::runtime_error("No cuda_tile::ModuleOp found in the input module")`，Python 侧表现为 RuntimeError，**根本到不了** subprocess 调用。

**练习 2**：bytecode 为什么不直接传给 `tileiras` 进程的 stdin，而是先写进缓存文件？

> **参考答案**：因为出错时需要 repro。缓存里的 `{name}.bytecode` 文件就是 `tileiras` 的输入，错误信息里的 `Repro command` 引用它，开发者可以拿同一条命令、同一个文件复现问题。

---

### 4.2 tileiras 命令与环境：子进程如何被构造

#### 4.2.1 概念说明

有了 bytecode 文件，下一步是构造并运行 `tileiras` 子进程。这一步有两个反直觉的设计需要重点理解：

1. **`CUDA_HOME` 是「算出来」的，不是「读出来」的。** `tileiras` 内部要找 `ptxas`、`libnvvm.so`、`libdevice`，它们都应在 `$CUDA_HOME` 下。但本后端**故意不读系统 `CUDA_HOME` 环境变量**，而是从 `tileiras` 可执行文件自身的位置反推（`tileiras` 总在 `<CUDA_HOME>/bin/tileiras`）。
2. **这个 `CUDA_HOME` 只注入给 `tileiras` 子进程，绝不写回 `os.environ`。** 这样一个陈旧的系统 CUDA（注释里举的例子是 13.2）永远不会盖过本仓库打包匹配的工具链。

这两点合起来回答了实践任务的核心问题：**为什么从路径推导而非读系统变量？**——为了防止工具链错配，保证 `tileiras` 永远配对到它自己同目录下那套工具。

#### 4.2.2 核心流程

子进程构造的完整流程：

```
opt.tileir_tileiras_path   ← TileIROptions 字段，默认 = TileIREnvConf.get_tileiras_path()
        │
        ▼
get_tileiras_path()  三级解析：
   ① TRITON_TILEIRAS_PATH 环境变量 → $path/tileiras
   ② 打包内置二进制 → backends/nvidia/tileir_cuda/bin/tileiras
   ③ 系统 PATH → which("tileiras")
        │
        ▼  得到 tileiras 绝对路径
get_tileir_cuda_home() = dirname(dirname(tileiras))
        │
        ▼
tileiras_env = { **os.environ, "CUDA_HOME": <上面算出的 home> }   # 只给子进程
        │
        ▼
subprocess.run([tileiras, --gpu-name, --opt-level, <bytecode_file>, -o, <cubin>],
               check=True, close_fds=False, stderr=flog, env=tileiras_env)
```

#### 4.2.3 源码精读

**命令拼装**——见 [third_party/tileir/backend/compiler.py:210-215](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/compiler.py#L210-L215)：

```python
tileiras = opt.tileir_tileiras_path
tileiras_cmd = [
    tileiras,
    f"--gpu-name=sm_{capability}",
    f"--opt-level={opt.opt_level}",
]
```

注意命令只带了三个全局参数：可执行文件、目标架构（`sm_{capability}`，如 `sm_100a`）、优化级别（`opt.opt_level`，默认 3，见 `TileIROptions.opt_level` 在 [compiler.py:67](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/compiler.py#L67)）。随后在 [compiler.py:232-234](https://github.com/lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/compiler.py#L232-L234) 追加输入 bytecode 文件与 `-o` 输出路径。

**CUDA_HOME 注入**——见 [third_party/tileir/backend/compiler.py:221-225](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/compiler.py#L221-L225)：

```python
# Scoped CUDA_HOME for the tileiras subprocess only (NOT global os.environ):
# tileiras locates ptxas + libnvvm + libdevice under $CUDA_HOME for SM100 codegen.
# Derived from the bundled tileiras location (tileir_cuda) so a stale system
# CUDA can never shadow the matching 13.3 toolchain.
tileiras_env = {**os.environ, "CUDA_HOME": TileIREnvConf.get_tileir_cuda_home()}
```

注意它先拷贝整个 `os.environ` 再**覆盖** `CUDA_HOME`，且只把这份 `tileiras_env` 传给 `subprocess.run` 的 `env=` 参数——进程级隔离。

**子进程执行**——见 [third_party/tileir/backend/compiler.py:236-237](https://github.com/lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/compiler.py#L236-L237)：

```python
subprocess.run(tileiras_cmd, check=True, close_fds=False, stderr=flog, env=tileiras_env)
```

- `check=True`：`tileiras` 非零退出立即抛 `CalledProcessError`，进入 4.3 的错误分类。
- `close_fds=False`：**故意**不关闭继承的文件描述符，这样 torch/CUDA 上下文相关的句柄（如 IPC handle）能透传给子进程。
- `stderr=flog`：把 `tileiras` 的标准错误写进临时日志文件，供后续正则匹配。

**路径三级解析**——见 [third_party/tileir/backend/conf.py:26-56](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/conf.py#L26-L56)：

```python
@staticmethod
def get_tileiras_path():
    # ① 显式环境变量优先
    env_path = os.getenv("TRITON_TILEIRAS_PATH", None)
    if env_path is not None:
        return os.path.join(env_path, "tileiras")
    # ② 打包内置二进制
    bundled_path = os.path.join(os.path.dirname(triton.__file__),
        "backends", "nvidia", "tileir_cuda", "bin", "tileiras")
    if os.path.isfile(bundled_path) and os.access(bundled_path, os.X_OK):
        return bundled_path
    # ③ 系统 PATH 兜底
    from shutil import which
    tileiras_path = which("tileiras")
    if tileiras_path is None:
        raise RuntimeError("tileiras not found: ...")
    return tileiras_path
```

**CUDA_HOME 反推**——见 [third_party/tileir/backend/conf.py:58-72](https://github.com/lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/conf.py#L58-L72)：

```python
@staticmethod
def get_tileir_cuda_home():
    # ...注释明确：DERIVED purely from the resolved tileiras location...
    # We deliberately do NOT read the system CUDA_HOME env var...
    tileiras = TileIREnvConf.get_tileiras_path()
    return os.path.dirname(os.path.dirname(tileiras))
```

`dirname(dirname(<CUDA_HOME>/bin/tileiras))` = `<CUDA_HOME>`，正好回到工具链根目录。三种解析模式下结果一致：

| 解析模式 | `tileiras` 路径 | `get_tileir_cuda_home()` 结果 |
|----------|----------------|------------------------------|
| `TRITON_TILEIRAS_PATH` 设置 | `$path/tileiras` | `dirname($path)` |
| 打包内置 | `.../tileir_cuda/bin/tileiras` | `.../tileir_cuda` |
| 系统 PATH | `which("tileiras")` | `dirname(dirname(which(...)))` |

> 版本说明：README 指出本后端「只使用 CUDA 13.1 提供的特性」，依赖 `bin/tileiras`、`bin/ptxas`、`nvvm/lib64/libnvvm.so`；而 `conf.py` 的注释把打包工具链描述为「13.3 toolchain」（见上面 `get_tileir_cuda_home` 上方注释）。两者一个是「特性集版本」，一个是「打包工具链版本」，本讲只如实引用源码，不作合并。

#### 4.2.4 代码实践

**实践目标**：回答「`tileiras` 子进程的 `CUDA_HOME` 为何要从 `tileiras` 路径推导而非读系统变量」，并对三种解析模式各写一行 shell 推导。

**操作步骤（源码阅读型）**：

1. 打开 [conf.py:58-72](https://github.com/lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/conf.py#L58-L72)，确认 `get_tileir_cuda_home` 完全没有调用 `os.getenv("CUDA_HOME")`。
2. 打开 [compiler.py:221-225](https://github.com/lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/compiler.py#L221-L225)，确认 `CUDA_HOME` 只出现在传给 `subprocess.run` 的 `env=` 里。

**需要观察的现象 / 需要回答的问题**：

- 为什么「读系统变量」是危险的？→ 系统可能装了陈旧的 CUDA（注释里的 13.2 例子），它的 `ptxas`/`libnvvm` 与本仓库打包的 `tileiras` 不匹配，会让 SM100 codegen 出错。
- 为什么只注入子进程？→ 避免污染 Triton 主进程（及 torch 等）的环境，做到「只为这次编译临时切换工具链」。

**预期结果**（参考答案）：

> `tileiras` 必须用与它**同源**的 `ptxas`/`libnvvm`/`libdevice`，否则 SM100 代码生成会出错。由于 `tileiras` 恒位于 `<CUDA_HOME>/bin/tileiras`，对其路径取两次 `dirname` 即可可靠还原出配套的 `<CUDA_HOME>`，无需依赖用户配置正确——这是一种「自描述、防错配」的设计。三级推导：
> - 设 `TRITON_TILEIRAS_PATH=/opt/cuda` → home = `/opt/cuda`（实际 `dirname(/opt/cuda/tileiras)` 不对，应为 `dirname($path)`，见 conf.py L31）。
>
> 修正：`get_tileiras_path` 在 ① 模式返回 `os.path.join(env_path, "tileiras")`，即 `<env_path>/tileiras`，故 home = `dirname(dirname(<env_path>/tileiras))` = `<env_path>`。三种模式：
> - ① `TRITON_TILEIRAS_PATH=$P` → home = `$P`
> - ② 打包内置 → home = `backends/nvidia/tileir_cuda`
> - ③ PATH → home = `dirname(dirname(which tileiras))`

> 待本地验证：若有 CUDA 13.x 环境，可用 `python -c "from triton.backends.tileir.conf import TileIREnvConf as C; print(C.get_tileiras_path(), C.get_tileir_cuda_home())"` 在 `ENABLE_TILE=1` 下打印两者，验证上表。

#### 4.2.5 小练习与答案

**练习 1**：`close_fds=False` 改成 `True` 会怎样？为什么这里偏偏要 `False`？

> **参考答案**：`True` 会在 exec 前关闭所有继承的文件描述符。这里要 `False`，是为了让 torch/CUDA 上下文持有的句柄（如 IPC、设备句柄）能透传给 `tileiras` 子进程，避免子进程因拿不到必要句柄而出错。

**练习 2**：命令行里为什么没有 `--occupancy`、`--enable-approx` 之类的旋钮？

> **参考答案**：因为这些旋钮在 `make_tileir` 阶段已被烘焙进 IR（见 u2-l2/u2-l3）。`tileiras` 只消费最终 IR 与 `--gpu-name`/`--opt-level` 等全局参数，看不到也不需要这些 Python 旋钮。

---

### 4.3 错误分类：OutOfResources 与 TileirasError

#### 4.3.1 概念说明

`tileiras` 是子进程，它的失败有两类，必须被正确分类，因为下游对它们的处理完全不同：

| 失败类别 | 含义 | 翻译成的异常 | 下游处理 |
|----------|------|------------|----------|
| 资源超限 | 某个 autotune 配置要的共享内存或 TMEM 超过硬件上限 | `OutOfResources` | autotuner **剪枝**该配置，继续试别的 |
| 一般失败/崩溃 | `tileiras` 自身报错，或被信号杀死（如 SIGSEGV） | `TileirasError` | 当作真实编译错误上报（类比 `PTXASError`） |

这条分类线至关重要：把「可剪枝的资源超限」误判成 `TileirasError`，会让 autotuner 直接整体失败而不是换配置；反过来把崩溃误判成资源超限，则会让 autotuner 错误地「跳过」本该上报的 bug。代码靠**匹配 `tileiras` 日志文本**来区分前一类，其余统统归到后一类。

两个错误类的真身在 `python/triton/runtime/errors.py`：

- `OutOfResources`（[runtime/errors.py:14-26](https://github.com/lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/triton/runtime/errors.py#L14-L26)）：构造为 `(required, limit, name)`，`__str__` 提示「降低 block size 或 `num_stages` 可能有帮助」。
- `TileirasError`（[runtime/errors.py:39-46](https://github.com/lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/triton/runtime/errors.py#L39-L46)）：与 `PTXASError`（同文件 L29-36）平行，是「外部汇编/编译器失败」的对应物。

> 补充：`third_party/tileir/backend/errors.py` 里的 `HitFallback` 是另一条链路（运行期回退，见 u4-l3），与本讲的编译期错误分类无关，不要混淆。

#### 4.3.2 核心流程

错误处理的判定流程（伪代码）：

```
try:
    subprocess.run(tileiras_cmd, check=True, stderr=flog, env=tileiras_env)
except CalledProcessError as e:
    log = 读 flog
    if "uses too much shared data" in log:
        用正则抓 used/max（十六进制）→ raise OutOfResources(used, max, "shared memory")
    if "allocated tmem out of resource" in log:
        用正则抓 used/max（十进制）→ raise OutOfResources(used, max, "tensor memory")
    # 其余一律：
    raise TileirasError(错误码 + stderr + repro 命令)
```

注意两个资源正则的**进制不同**：shared memory 的数字是十六进制（`0x...`），TMEM 的数字是十进制——这是 `tileiras` 日志格式决定的，抓取时不能搞混。

#### 4.3.3 源码精读

完整错误处理块见 [third_party/tileir/backend/compiler.py:236-272](https://github.com/lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/compiler.py#L236-L272)。

**共享内存超限**——见 [compiler.py:244-250](https://github.com/lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/compiler.py#L244-L250)：

```python
if "uses too much shared data" in log:
    pattern = r"0x([0-9a-fA-F]+) bytes, 0x([0-9a-fA-F]+) max"
    match = re.search(pattern, log)
    if match:
        used_smem = int(match.group(1), 16)
        max_smem = int(match.group(2), 16)
        raise OutOfResources(used_smem, max_smem, "shared memory")
```

**TMEM（tensor memory）超限**——见 [compiler.py:251-260](https://github.com/lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/compiler.py#L251-L260)：

```python
if "allocated tmem out of resource" in log:
    # "allocated tmem out of resource: <used> vs <max>"
    pattern = r"allocated tmem out of resource:\s*([0-9]+)\s*vs\s*([0-9]+)"
    match = re.search(pattern, log)
    if match:
        used_tmem = int(match.group(1))     # 十进制
        max_tmem = int(match.group(2))
        raise OutOfResources(used_tmem, max_tmem, "tensor memory")
```

**一般失败/崩溃**——见 [compiler.py:261-272](https://github.com/lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/compiler.py#L261-L272)：

```python
error = f"`tileiras` failed with error code {e.returncode}"
repro = ' '.join(str(item) for item in tileiras_cmd)
# 负的 returncode 表示 tileiras 被信号杀死（如 -11 SIGSEGV）——是编译器崩溃，不是用户错误。
logging.warning("tileiras failed (code %s). Repro: %s", e.returncode, repro)
raise TileirasError(
    f"{error}\n"
    f"`tileiras` stderr:\n{log}\n"
    f"Repro command: {repro}\n"
)
```

注释点明：负的 `returncode`（如 `-11` = SIGSEGV）意味着 `tileiras` 被信号杀死，是**编译器自身崩溃**而非用户配置错误，仍归为 `TileirasError`，但用 `logging.warning` 始终留痕，避免被静默吞掉。`TileirasError` 携带 stderr 全文与 repro 命令，方便复现。

成功路径——见 [compiler.py:273-277](https://github.com/lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/compiler.py#L273-L277)：读出 cubin 字节、删除临时文件、`return cubin`，cubin 随后由上游落盘并以 `.cubin` 缓存（见 u1-l4）。

#### 4.3.4 代码实践

**实践目标**：写出判别「共享内存超限」与「TMEM 超限」两类错误的正则，并与源码对照。

**操作步骤**：

1. 假设你拿到两段 `tileiras` stderr 片段（示例日志，非项目原有）：
   - 共享内存类：`... uses too much shared data: needs 0x10000 bytes, 0x8000 max ...`
   - TMEM 类：`... allocated tmem out of resource: 512 vs 256 ...`
2. 不看源码，为每段写一个 Python 正则，提取「已用」与「上限」两个数。
3. 打开 [compiler.py:244-260](https://github.com/lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/compiler.py#L244-L260) 与你的答案对照。

**需要观察的现象**：

- 共享内存的两段数字是 `0x` 开头的十六进制，正则的字符类应是 `[0-9a-fA-F]`，且 `int(..., 16)` 解析。
- TMEM 的两段数字是裸十进制，分隔符是 ` vs `，正则用 `([0-9]+)\s*vs\s*([0-9]+)`，`int(...)` 默认十进制。

**预期结果（参考答案，即源码原文）**：

```python
# 共享内存（十六进制）
re.search(r"0x([0-9a-fA-F]+) bytes, 0x([0-9a-fA-F]+) max", log)
# TMEM（十进制）
re.search(r"allocated tmem out of resource:\s*([0-9]+)\s*vs\s*([0-9]+)", log)
```

> 待本地验证：示例日志为讲解构造，实际 `tileiras` 输出文案以你机器上的真实 stderr 为准；可在故意设置超大 `num_stages` 触发共享内存超限后，查看 `OutOfResources` 的 `(required, limit, name)` 取值是否与日志一致。

#### 4.3.5 小练习与答案

**练习 1**：若把 shared memory 的 `int(match.group(1), 16)` 误写成 `int(match.group(1))`（漏掉进制参数），会发生什么？

> **参考答案**：`int("10000")` 会按十进制解析为 10000，而真实含义是十六进制 0x10000 = 65536。导致 `OutOfResources` 报告的 `required` 数值错误（偏小），误导用户判断「离上限还有多远」。进制必须与日志格式匹配。

**练习 2**：为什么 `TileirasError` 一定要带上 repro 命令和 stderr，而 `OutOfResources` 不需要？

> **参考答案**：资源超限是配置问题，autotuner 剪枝即可，用户需要的是「超了多少」（已在异常字段里）；而 `TileirasError` 往往是 `tileiras` 自身或 IR 的问题，需要开发者能精确复现，因此必须保留完整 stderr 和可重放的命令。

**练习 3**：`tileiras` 被段错误杀死时，`e.returncode` 是正数还是负数？会被归为哪类异常？

> **参考答案**：负数（SIGSEGV = 信号 11，故 `returncode = -11`）。它不匹配两条资源日志，因此走到末尾分支，归为 `TileirasError`，同时通过 `logging.warning` 留下 returncode 与 repro 痕迹。

## 5. 综合实践

把本讲三个模块串起来，完成一次「`call_tileiras` 全链路追踪」。

**任务**：阅读 [compiler.py:205-277](https://github.com/lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/compiler.py#L205-L277) 的 `call_tileiras` 全函数，画出一张完整的时序图，并标注以下要素：

1. **输入**：`mod`（外层 ModuleOp）、`metadata`（含 `name`、`hash`）、`opt`（`TileIROptions`）、`capability`。
2. **bytecode 输出**：在哪一行调用 `tileir.write_bytecode`、缓存文件名是什么。
3. **命令构造**：`tileiras_cmd` 的完整元素（可执行文件、两个 `--` 参数、bytecode 输入、`-o` cubin 输出）。
4. **环境构造**：`tileiras_env` 如何由 `os.environ` + 推导出的 `CUDA_HOME` 组成，`get_tileir_cuda_home` 的两步 `dirname`。
5. **错误分类**：三条出口（shared memory → `OutOfResources("shared memory")`、tmem → `OutOfResources("tensor memory")`、其余 → `TileirasError`），以及成功出口（读 cubin 返回）。

**进阶**：写一个小脚本（示例代码，非项目原有），模拟 `call_tileiras` 的错误分类逻辑——给定一段伪造的 stderr 字符串，判断应抛哪类异常、提取哪些数值：

```python
# 示例代码：仅供理解错误分类逻辑，非项目原有
import re

def classify_tileiras_log(log: str):
    if "uses too much shared data" in log:
        m = re.search(r"0x([0-9a-fA-F]+) bytes, 0x([0-9a-fA-F]+) max", log)
        if m:
            return ("OutOfResources", int(m.group(1), 16), int(m.group(2), 16), "shared memory")
    if "allocated tmem out of resource" in log:
        m = re.search(r"allocated tmem out of resource:\s*([0-9]+)\s*vs\s*([0-9]+)", log)
        if m:
            return ("OutOfResources", int(m.group(1)), int(m.group(2)), "tensor memory")
    return ("TileirasError", log)

# 自测用例（示例）
assert classify_tileiras_log("uses too much shared data: 0x10000 bytes, 0x8000 max")[1:] == (65536, 32768, "shared memory")
assert classify_tileiras_log("allocated tmem out of resource: 512 vs 256")[1:] == (512, 256, "tensor memory")
assert classify_tileiras_log("Segmentation fault")[0] == "TileirasError"
```

> 待本地验证：示例代码可在任何 Python 环境运行以验证分类逻辑，但真实的 `tileiras` 输出文案需在 CUDA 13.x + Blackwell 环境下确认。

## 6. 本讲小结

- `make_cubin` 只是 `call_tileiras` 的薄包装；它把 `make_tileir` 产出的 cuda_tile IR 经 `tileir.write_bytecode` 序列化为 bytecode，缓存为 `{name}.bytecode`，作为 `tileiras` 的输入。
- bytecode 序列化的对象是**嵌套的** `cuda_tile::ModuleOp`，C++ 侧若找不到它就直接抛异常，根本到不了子进程调用。
- `tileiras` 命令很短（仅 `--gpu-name` 与 `--opt-level`），因为 Python 旋钮（occupancy/approx/ftz 等）早已在 `make_tileir` 烘焙进 IR；`tileiras` 只看最终 IR。
- `CUDA_HOME` 由 `dirname(dirname(tileiras))` **反推**而非读系统变量，且**只注入子进程**，目的是防止陈旧系统 CUDA 盖过打包匹配的工具链。
- 编译失败分两类：资源超限（shared memory / TMEM）→ `OutOfResources`，可被 autotuner 剪枝；其余含信号崩溃 → `TileirasError`，类比 `PTXASError`。两类资源正则的进制不同（十六进制 vs 十进制）。
- `OutOfResources`/`TileirasError` 定义在 `python/triton/runtime/errors.py`；`tileir` 目录下的 `errors.py` 只有 `HitFallback`，属运行期回退链路（u4-l3），不要混淆。

## 7. 下一步学习建议

- 若想深入了解 `write_bytecode` 如何在 `make_tileir` 转换链中定位嵌套的 `cuda_tile::ModuleOp`，以及 fuse-fma/loop-split 等 cuda_tile 级 pass 如何嵌套进该容器，请阅读 [u3-l7 后处理：FMA 融合、loop split 与 bytecode 输出](u3-l7-fma-fusion-and-bytecode.md)。
- 若想了解 `tileiras` 之外另一条「失败退路」——运行期 `tileir_run` 把整个后端临时切回 PTX 的 fallback，请阅读 [u4-l3 编译期与运行期 Fallback 容错](u4-l3-fallback-mechanism.md)。
- 若想了解这套打包工具链（`tileir_cuda`、`tileiras`、`ptxas`、`libnvvm`）是如何在构建期被克隆与链接进插件的，请阅读 [u4-l4 构建系统与 cuda-tile 依赖管理](u4-l4-build-and-cuda-tile-deps.md)。
