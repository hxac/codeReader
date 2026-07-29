# tileiras 外部编译器调用与 cubin 生成

## 1. 本讲目标

本讲承接 u2-l3「三段式编译流水线 make_ttir/make_tileir/make_cubin」,聚焦流水线的**最后一段** `make_cubin`:当 TTIR 已经被 lowering 成 `cuda_tile` 方言 IR 之后,这份 IR 是怎么变成一块可以丢给 GPU 执行的 `.cubin` 二进制的?

答案不是「Triton 自己接着编」,而是**交给一个外部的 NVIDIA 编译器 `tileiras`**。本讲就是要把这次「交接」讲透。学完本讲,你应该能够:

1. 说清楚 `make_cubin` 为什么只是 `call_tileiras` 的薄壳,以及 IR 是怎样被序列化成 bytecode 交给外部工具的。
2. 读懂 `tileiras` 命令行是怎么拼出来的、`CUDA_HOME` 为什么刻意从 `tileiras` 自身的路径反推而不是读系统环境变量。
3. 区分两类资源超限错误(`shared memory` / `tensor memory`)与一般编译崩溃错误,理解为什么前者要归为 `OutOfResources`、后者归为 `TileirasError`,以及这样分类对 autotuner 的意义。

---

## 2. 前置知识

### 2.1 什么是 cubin,为什么需要它

`cubin`(CUDA binary)是 NVIDIA GPU 真正能执行的机器码二进制格式。Triton 的 `@triton.jit` kernel 最终都要变成 cubin 才能被 `cuLaunchKernelEx` 启动。

在**上游 NVIDIA PTX 后端**里,从 IR 到 cubin 的最后一步是调用 NVIDIA 的 `ptxas`(把 PTX 汇编编成 cubin)。而在 **CUDA Tile IR 后端**里,这一步换成了 `tileiras`——它是随 NVIDIA `cuda-tile`(CUDA 13.1 / 13.3 工具链)发布的编译器,专门消费 **cuda_tile 方言**的 IR,产出 SM100(Blackwell)的 cubin。

> 一句话:`ptxas` 消费 PTX 文本;`tileiras` 消费 cuda_tile bytecode。两者都是「外部独立可执行文件」,都不在 Triton 进程内。

### 2.2 什么是 MLIR bytecode

`cuda_tile` IR 有两种表示:

- **文本形式(printable `.mlir`)**:人能读,体积大,解析慢。
- **bytecode(字节码)**:二进制,紧凑、解析快,是 MLIR 官方推荐的序列化交换格式。

`tileiras` 作为独立进程,接收的就是 **bytecode 文件**——它看不到 Triton 内存里那个 IR 对象。所以 Triton 必须先把内存里的 `cuda_tile::ModuleOp` 序列化成字节,落盘成一个文件,再把文件路径作为命令行参数传给 `tileiras`。

### 2.3 复习:u2-l3 的三段式流水线

本讲频繁用到 u2-l3 建立的认知:

- 后端在 `add_stages` 里把 `make_ttir` / `make_tileir` / `make_cubin` 三个工厂函数注册进一个有序字典,上游 `compile()` 按插入顺序驱动。
- `make_tileir` 把 IR 从 `tt.*` lowering 成 `cuda_tile` 方言,并在最外层 builtin `ModuleOp` 之内插入一个嵌套的 `cuda_tile.module` 容器。
- `make_cubin` 是流水线终点,它的产物是 `.cubin`。

本讲要回答的,就是**这个 `make_cubin` 内部到底做了什么**。

---

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `third_party/tileir/backend/compiler.py` | TileIR 后端主体。本讲主角:`make_cubin`(薄壳)、`call_tileiras`(真正干活的静态方法),包含 bytecode 落盘、命令构造、`CUDA_HOME` 注入、错误分类。 |
| `third_party/tileir/backend/conf.py` | 环境配置。`get_tileiras_path`(三级路径解析)、`get_tileir_cuda_home`(从 tileiras 路径反推 CUDA_HOME)。 |
| `python/triton/runtime/errors.py` | 错误类定义。`OutOfResources`、`TileirasError`——`call_tileiras` 抛出的两类异常。 |
| `third_party/tileir/triton_tileir.cc` | C++ pybind 插件。`write_bytecode`——在嵌套 `cuda_tile::ModuleOp` 上序列化 bytecode 的真正实现。 |

> 注意:`OutOfResources` 与 `TileirasError` 定义在 `python/triton/runtime/errors.py`,而 `compiler.py` 第 1 行 `from triton.runtime.errors import OutOfResources, TileirasError` 把它们导入。本讲讲的是「编译期」错误分类;与之相对的「运行期 fallback」(jit.py 里的 `tileir_run` / `HitFallback`)属于 u4-l3,本讲只在最后做一句话区分。

---

## 4. 核心概念与源码讲解

### 4.1 bytecode 输出:把 cuda_tile.module 序列化给外部工具

#### 4.1.1 概念说明

`make_tileir` 跑完之后,Triton 内存里有一个 IR 模块,它的结构长这样(伪 IR):

```
module {                 // 最外层是 MLIR 内置 builtin ModuleOp
  "cuda_tile.module" : { // 嵌套的 cuda_tile 方言模块容器
    ... 真正的 cuda_tile 算子(load / store / dot / ...)
  }
}
```

而 `tileiras` 是一个**独立进程**,它只能读文件。所以我们面临一个问题:

- 不能把最外层的 builtin `ModuleOp` 整个序列化——`tileiras` 只认 `cuda_tile` 方言。
- 必须从 builtin 模块里**挑出那个嵌套的 `cuda_tile::ModuleOp`**,只对它做 bytecode 序列化。

这一步由 C++ pybind 函数 `write_bytecode` 完成,Python 侧通过 `tileir.write_bytecode(mod)` 调用。

#### 4.1.2 核心流程

`make_cubin` 到 bytecode 落盘的流程:

```
make_cubin(mod, metadata, opt, capability)
   │  (薄壳,直接转发)
   ▼
call_tileiras(mod, metadata, opt, capability)
   │
   ├── ① tileir.write_bytecode(mod)   ──► bytes(在 C++ 里挑出嵌套 cuda_tile::ModuleOp)
   │
   ├── ② get_cache_manager(metadata["hash"]).put(bytes, f"{name}.bytecode")
   │        ──► bytecode_file(落盘到缓存目录,带函数名)
   │
   └── ③ 把 bytecode_file 作为命令行位置参数传给 tileiras(见 4.2)
```

要点:

- **bytecode 同时被缓存**。`{name}.bytecode` 会写入缓存目录,这样既给 `tileiras` 用,又方便事后人工复现(repro),不必每次重跑整条流水线。
- 缓存键用的是 `metadata["hash"]`——也就是说同一份「旋钮配置」的 bytecode 会命中同一份缓存,与 cubin 一起分级缓存(参见 u2-l3 / u1-l4 讲过的 `.ttir` / `.tileir` / `.cubin` 三级落盘)。

#### 4.1.3 源码精读

`make_cubin` 只是一个把工作转交给 `call_tileiras` 的薄壳:

[compiler.py:332-334](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/compiler.py#L332-L334) —— `make_cubin` 静态方法,直接返回 `call_tileiras` 的结果,本身不做任何变换。

真正序列化发生在 `call_tileiras` 的开头。先取出 `tileiras` 可执行文件路径,然后调 `write_bytecode`,再把字节写进缓存:

[compiler.py:210-219](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/compiler.py#L210-L219) —— 取 `tileiras` 路径、构造命令开头;`bytecode = tileir.write_bytecode(mod)` 得到字节;`bytecode_cache_name = f"{name}.bytecode"`,用缓存管理器落盘得到 `bytecode_file`。

而 `write_bytecode` 的真正实现在 C++ 侧,关键是它「只序列化嵌套的 cuda_tile::ModuleOp」:

[triton_tileir.cc:129-149](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/triton_tileir.cc#L129-L149) —— `write_bytecode` 在 `mod.getBody()->front()`(模块体的第一个操作)上做 `dyn_cast<cuda_tile::ModuleOp>`;命中则对它调 `cuda_tile::writeBytecode`;找不到就抛 `"No cuda_tile::ModuleOp found in the input module"`。最后包成 `py::bytes` 返回。

> 这里解释了为什么 `make_tileir` 必须在转换开头**插入 `cuda_tile.module` 容器**(见 u2-l3):`write_bytecode` 依赖「模块体第一个操作就是 cuda_tile.module」这个约定。如果没有它,`write_bytecode` 会直接抛异常,`make_cubin` 也就无法进行。

#### 4.1.4 代码实践(源码阅读型)

**目标**:确认 bytecode 缓存确实会被落盘,并能人工取到它用于复现。

1. 打开 [compiler.py:205-219](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/compiler.py#L205-L219),对照本节流程图,确认 `bytecode` 变量来自 `tileir.write_bytecode(mod)`,且文件名是 `f"{name}.bytecode"`。
2. 在一个真实跑过 TileIR kernel 的环境里(需 Blackwell GPU,待本地验证),设置 `TRITON_CACHE_DIR` 指向一个干净目录,例如 `export TRITON_CACHE_DIR=/tmp/tcache`。
3. 跑一个最小的 `@triton.jit` kernel(开 `ENABLE_TILE=1`)。
4. 到缓存目录里翻找 `*.bytecode` 文件,确认它和对应 kernel 名同名。

**需要观察的现象**:缓存目录里出现 `<kernelname>.bytecode` 文件,且该文件是二进制(开头通常有 MLIR bytecode 魔数字节)。

**预期结果**:能找到这份字节码文件,且后续可以用它脱离 Triton、直接手动喂给 `tileiras` 做复现(见 4.2 的命令拼接)。

> 如果没有 Blackwell 硬件,这一步属于「待本地验证」,但你可以完成源码阅读部分:确认 `write_bytecode` 只挑嵌套 `cuda_tile::ModuleOp` 序列化。

#### 4.1.5 小练习与答案

**Q1**:`write_bytecode` 为什么不直接序列化传入的 `mlir::ModuleOp`,而要先 `dyn_cast` 找嵌套的 `cuda_tile::ModuleOp`?

**参考答案**:因为 `tileiras` 只认识 `cuda_tile` 方言;最外层 `mlir::ModuleOp` 是 MLIR 内置容器,序列化它会带上 builtin 方言信息,不是 `tileiras` 期望的输入。所以必须精确挑出 `cuda_tile` 模块容器,只对它做 bytecode。

**Q2**:`bytecode_file` 这个变量最后被用在命令的哪个位置?

**参考答案**:作为 `tileiras` 命令的倒数第二个位置参数(输入文件),紧随其后是 `-o <cubin_file>`(见 4.2)。

---

### 4.2 tileiras 命令构造与 CUDA_HOME 注入

#### 4.2.1 概念说明

bytecode 文件准备好后,下一步就是**拼出一条 `tileiras` 命令行,起子进程执行它**。这里有两个关键设计:

1. **命令行怎么拼**:固定几个 flag(`--gpu-name`、`--opt-level`)+ 输入 bytecode 文件 + `-o` 输出 cubin 文件。注意:`tileiras` **看不到任何 Python 旋钮**(occupancy / num_ctas / num_stages / approx / ftz),这些旋钮在 u2-l3 的 `make_tileir` 里就已经被「烘焙」进 IR 了,到 `tileiras` 时它们已经体现为 IR 里的具体算子与属性。
2. **`CUDA_HOME` 怎么给**:`tileiras` 内部还要调用 `ptxas`、`libnvvm.so`、`libdevice` 来做 SM100 codegen,它靠 `CUDA_HOME` 来定位这些工具。Triton 这里做了一个**刻意的决定**:不从系统的 `CUDA_HOME` 环境变量读,而是从 `tileiras` 自身的安装路径反推。

#### 4.2.2 核心流程

最终拼出的命令形如:

```
tileiras --gpu-name=sm_<capability> --opt-level=<opt_level> <bytecode_file> -o <cubin_file>
```

环境与执行的流程:

```
call_tileiras
   │
   ├── 命令构造:[tileiras, "--gpu-name=sm_{cap}", "--opt-level={opt_level}"]
   │             └── 在临时文件块里再 append: bytecode_file, "-o", fbin.name
   │
   ├── 环境构造:tileiras_env = {**os.environ, "CUDA_HOME": get_tileir_cuda_home()}
   │             └── CUDA_HOME = dirname(dirname(tileiras))   ← 从 tileiras 路径反推
   │
   ├── subprocess.run(cmd, check=True, close_fds=False, stderr=flog, env=tileiras_env)
   │
   └── 成功 → 读 fbin.name 得到 cubin 字节,删临时文件,返回 cubin
```

`CUDA_HOME` 的反推逻辑基于一个布局约定:

\[ \text{tileiras 位于}\ \langle \text{CUDA\_HOME} \rangle / \text{bin/tileiras} \]

所以对 `tileiras` 路径做两次 `dirname`(先去掉 `tileiras`,再去掉 `bin`)就得到 `CUDA_HOME`:

\[ \text{CUDA\_HOME} = \mathrm{dirname}(\mathrm{dirname}(\text{tileiras})) \]

`tileiras` 路径本身有三级解析顺序(在 `get_tileiras_path` 里),所以 `CUDA_HOME` 也随之有三条推导路径:

| `tileiras` 来源 | `CUDA_HOME` 推导结果 |
|---|---|
| `TRITON_TILEIRAS_PATH` 已设 | `dirname(TRITON_TILEIRAS_PATH)` |
| 内置二进制 `<triton>/backends/nvidia/tileir_cuda/bin/tileiras` | `<triton>/backends/nvidia/tileir_cuda` |
| 系统 PATH(`which tileiras`) | `dirname(dirname(which tileiras))` |

#### 4.2.3 源码精读

命令行的前半段(标志位):

[compiler.py:210-215](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/compiler.py#L210-L215) —— `tileiras = opt.tileir_tileiras_path`;`tileiras_cmd` 初始为 `[tileiras, "--gpu-name=sm_{capability}", "--opt-level={opt_level}"]`。注意这里的 `capability` 是数字 SM(例如 100),直接拼成 `sm_100`。

`CUDA_HOME` 的注入——**只进子进程环境,不污染全局 `os.environ`**:

[compiler.py:221-225](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/compiler.py#L221-L225) —— 注释写明:这是「仅给 tileiras 子进程的作用域内 `CUDA_HOME`」(NOT global os.environ);`tileiras` 用它定位 SM100 codegen 所需的 `ptxas` + `libnvvm` + `libDevice`;值取自 `TileIREnvConf.get_tileir_cuda_home()`,刻意从内置 `tileiras` 位置反推,让「一个陈旧的系统 CUDA 永远不会遮蔽匹配的 13.3 工具链」。

`get_tileir_cuda_home` 的实现——两次 `dirname`:

[conf.py:58-72](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/conf.py#L58-L72) —— 注释明确:「DELIVERED purely from the resolved tileiras location」「deliberately do NOT read the system CUDA_HOME env var」,最后 `return os.path.dirname(os.path.dirname(tileiras))`。

`tileiras` 路径本身的三级解析:

[conf.py:26-56](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/conf.py#L26-L56) —— 顺序为 `TRITON_TILEIRAS_PATH`(在其下拼 `tileiras`)> 内置 `<triton>/backends/nvidia/tileir_cuda/bin/tileiras` > 系统 PATH(`shutil.which`);都找不到则抛 `RuntimeError("tileiras not found ...")`。

命令后半段(临时文件 + 输入输出路径)+ 子进程执行:

[compiler.py:228-237](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/compiler.py#L228-L237) —— 用 `NamedTemporaryFile` 开两个临时文件(`.log` 接 stderr、`.cubin` 接输出);把 `bytecode_file`、`-o`、`fbin.name` append 到命令;`subprocess.run(..., check=True, close_fds=False, stderr=flog, env=tileiras_env)`。`close_fds=False` 是为了让子进程能继承 Triton 已打开的 CUDA 句柄(如 stream)。

> 默认 `opt_level` 是 3,来自 u2-l2 讲过的 `TileIROptions.opt_level`(见 [compiler.py:67](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/compiler.py#L67))。而 `tileir_tileiras_path` 默认值是 `TileIREnvConf.get_tileiras_path()`(见 [compiler.py:72](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/compiler.py#L72)),也就是说 `TileIROptions` 实例化时就解析好了路径并冻结进选项对象。

#### 4.2.4 代码实践(源码阅读型)

**目标**:在脑中复现 `CUDA_HOME` 的三套推导结果,并理解为什么不能读系统变量。

1. 读 [conf.py:26-72](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/conf.py#L26-L72),填出下表:

   | 场景 | `get_tileiras_path()` 返回 | `get_tileir_cuda_home()` 返回 |
   |---|---|---|
   | 设了 `TRITON_TILEIRAS_PATH=/opt/tc` | ? | ? |
   | 未设,有内置二进制 | `<triton>/backends/nvidia/tileir_cuda/bin/tileiras` | ? |
   | 未设,无内置,PATH 里有 `/usr/local/cuda/bin/tileiras` | ? | ? |

2. 回答:假设系统里装了一个**旧的 CUDA 13.2**,且 `CUDA_HOME=/usr/local/cuda-13.2`,而 Triton 内置的是 **13.3** 的 `tileir_cuda`。如果 `get_tileir_cuda_home` 改成读系统 `os.environ["CUDA_HOME"]`,会发生什么问题?

**预期结果**(表格答案):

| 场景 | `get_tileiras_path()` | `get_tileir_cuda_home()` |
|---|---|---|
| `TRITON_TILEIRAS_PATH=/opt/tc` | `/opt/tc/tileiras` | `/opt/tc` |
| 内置二进制 | `<triton>/backends/nvidia/tileir_cuda/bin/tileiras` | `<triton>/backends/nvidia/tileir_cuda` |
| PATH 里 `/usr/local/cuda/bin/tileiras` | `/usr/local/cuda/bin/tileiras` | `/usr/local/cuda` |

第二问:旧版 13.2 的 `ptxas` / `libnvvm` / `libdevice` 会被 `tileiras` 拿去给 SM100 生成代码,可能产出**与 13.3 不匹配或不正确的 cubin**,且这种错最难排查(因为编译可能「成功」但结果错)。所以代码刻意从 `tileiras` 路径反推,保证工具链版本与 `tileiras` 自身一致。

> 待本地验证:在有内置 `tileir_cuda` 的环境里,`print(TileIREnvConf.get_tileir_cuda_home())` 应指向 `backends/nvidia/tileir_cuda`,且该目录下确有 `bin/tileiras`、`bin/ptxas`、`libnvvm.so` 等。

#### 4.2.5 小练习与答案

**Q1**:为什么 `tileiras_env` 要用 `{**os.environ, "CUDA_HOME": ...}` 这种写法,而不是直接 `os.environ["CUDA_HOME"] = ...`?

**参考答案**:前者是「复制一份环境字典,只在副本里覆盖 `CUDA_HOME`,且**只传给子进程**」,后者会**全局污染** Triton 自身进程的环境,影响其它代码对 `CUDA_HOME` 的认知。注释里特意标注 `NOT global os.environ`,目的就是隔离。

**Q2**:`tileiras` 命令行里为什么没有 `occupancy`、`num_ctas` 这些旋钮?它们去哪了?

**参考答案**:这些旋钮在 `make_tileir` 阶段就已经被 lowering pass 烘焙进了 `cuda_tile` IR(具体算子布局、`cuda_tile.module` 的属性等),到 `make_cubin` 时 IR 已经定型。`tileiras` 只看到最终的 bytecode,所以不需要、也看不到这些 Python 旋钮。

---

### 4.3 资源超限与编译崩溃的错误分类

#### 4.3.1 概念说明

`tileiras` 可能因为各种原因失败,Triton 必须把它们**分成两类**对待,因为 autotuner 对这两类的处理完全不同:

| 错误类型 | 触发场景 | 抛出的异常 | autotuner 行为 |
|---|---|---|---|
| **资源超限** | kernel 用的 shared memory 或 TMEM(tensor memory)超过硬件上限 | `OutOfResources` | 可剪枝:跳过该配置,换更小的 |
| **编译崩溃/其它失败** | 包括被信号杀死(负 returncode,如 SIGSEGV -11)、未知错误 | `TileirasError` | 不当资源超限处理;记录日志并上抛 |

为什么要这样分?

- **资源超限是「正常的、可预期的」失败**:某个 autotune 配置把 block 开得太大,shared memory 装不下。这和上游 PTX 后端用 `ptxas` 时 shared memory 超限的情况一样——上游抛 `OutOfResources`,autotuner 据此剪枝。TileIR 后端在这里做了**镜像**(mirror)的处理。
- **编译崩溃是「不正常的」失败**:工具链本身出 bug(segfault)或遇到不支持的情形,这不该被 autotuner 当成「配置太大」而悄悄跳过,而应该让用户看见。

其中 **TMEM(tensor memory)** 是 Blackwell(SM100)特有的片上存储资源,被 `tcgen05` MMA 使用。`shared memory` 则是所有架构都有的概念。这两类是 Blackwell TileIR 最常踩的资源上限。

#### 4.3.2 核心流程

`call_tileiras` 的错误处理流程:

```
subprocess.run(..., check=True)
   │
   ├── 成功(exit 0) → 读 cubin,返回
   │
   └── 抛 CalledProcessError(e)
         │
         ├── 读 flog(stderr 日志)
         ├── 删临时 log 文件
         │
         ├── 日志含 "uses too much shared data" ?
         │     ├── 是 → 正则取 hex used/max → OutOfResources(used, max, "shared memory")
         │     └── 否 ↓
         ├── 日志含 "allocated tmem out of resource" ?
         │     ├── 是 → 正则取 十进制 used/max → OutOfResources(used, max, "tensor memory")
         │     └── 否 ↓
         │
         └── 其它(含负 returncode 即被信号杀死)
               ├── logging.warning(记录 returncode + repro 命令,绝不静默吞掉)
               └── raise TileirasError(error + stderr + repro)
```

两个判别正则(本讲的实践重点):

- **shared memory**——日志里是十六进制的「用了多少字节 vs 最大多少」:

  ```
  0x([0-9a-fA-F]+) bytes, 0x([0-9a-fA-F]+) max
  ```

  捕获组 1 = 已用(十六进制),捕获组 2 = 上限(十六进制)。

- **TMEM**——日志里是十进制的「用了多少 vs 最大多少」:

  ```
  allocated tmem out of resource:\s*([0-9]+)\s*vs\s*([0-9]+)
  ```

  捕获组 1 = 已用(十进制),捕获组 2 = 上限(十进制)。

> 注意两个正则的进制不同:shared memory 用十六进制(`0x` 前缀,`int(.., 16)`),TMEM 用十进制(`int(..)`)。这是由 `tileiras` 实际输出格式决定的,写正则时必须照搬。

#### 4.3.3 源码精读

shared memory 超限的识别与抛出:

[compiler.py:244-250](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/compiler.py#L244-L250) —— 触发字符串是 `"uses too much shared data"`;正则 `r"0x([0-9a-fA-F]+) bytes, 0x([0-9a-fA-F]+) max"`;命中后用 `int(match.group(1), 16)` / `int(match.group(2), 16)` 解析,**raise `OutOfResources(used_smem, max_smem, "shared memory")`**。

TMEM 超限的识别与抛出:

[compiler.py:251-260](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/compiler.py#L251-L260) —— 触发字符串是 `"allocated tmem out of resource"`;正则 `r"allocated tmem out of resource:\s*([0-9]+)\s*vs\s*([0-9]+)"`;命中后用 `int(match.group(1))` / `int(match.group(2))`(十进制)解析,**raise `OutOfResources(used_tmem, max_tmem, "tensor memory")`**。

其余一切失败 → `TileirasError`,并且**先记 warning 再抛**:

[compiler.py:261-272](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/compiler.py#L261-L272) —— 注释解释:负 returncode 意味着 `tileiras` 被信号杀死(如 -11 SIGSEGV),是**编译器崩溃**而非用户错误;抛 `TileirasError` 让 autotuner 能剪枝(镜像 PTX 后端的 `PTXASError`),但**总是先 `logging.warning`**,让底层失败保持可见、绝不被静默吞掉;异常信息里附上退出码、stderr 全文、以及可复现的 repro 命令。

两个错误类的定义(注意它们的字段与可 pickle 化):

[errors.py:14-26](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/triton/runtime/errors.py#L14-L26) —— `OutOfResources(required, limit, name)`,`__str__` 提示「Reducing block sizes or `num_stages` may help」;实现了 `__reduce__` 以便在多进程 autotuner 间可 pickle 传递。

[errors.py:39-46](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/triton/runtime/errors.py#L39-L46) —— `TileirasError(error_message)`,只承载一段错误信息字符串。

> 区分一下「编译期错误」与「运行期 fallback」:本讲的 `OutOfResources` / `TileirasError` 都是 **JIT 编译期**(`make_cubin` 阶段)抛出的;而 u4-l3 会讲 jit.py 里 `tileir_run` 在**运行期**失败时回退到 PTX 后端的机制(由 `TRITON_TILEIR_RUNTIME_FALLBACK` 开关,抛 `HitFallback`)。两者发生在不同阶段,不要混淆。

#### 4.3.4 代码实践(源码阅读型)

**目标**:亲手验证两条资源超限正则能正确解析 `tileiras` 的真实输出格式。

1. 打开 [compiler.py:244-260](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/compiler.py#L244-L260),抄下两条触发字符串与正则。
2. 写一个最小 Python 脚本(示例代码,**不是项目原有代码**),模拟 `tileiras` 的 stderr 片段,验证正则能取到 used/max:

   ```python
   # 示例代码:验证两条资源超限正则
   import re

   # 模拟 shared memory 超限日志(十六进制)
   smem_log = "... uses too much shared data. 0x10000 bytes, 0xc000 max ..."
   m = re.search(r"0x([0-9a-fA-F]+) bytes, 0x([0-9a-fA-F]+) max", smem_log)
   if m:
       print("smem:", int(m.group(1), 16), int(m.group(2), 16))  # 期望 65536 49152

   # 模拟 tmem 超限日志(十进制)
   tmem_log = "... allocated tmem out of resource: 600 vs 512 ..."
   m = re.search(r"allocated tmem out of resource:\s*([0-9]+)\s*vs\s*([0-9]+)", tmem_log)
   if m:
       print("tmem:", int(m.group(1)), int(m.group(2)))           # 期望 600 512
   ```

3. 运行它,确认两条 print 的输出与注释里的期望值一致。

**需要观察的现象**:shared memory 行打印 `65536 49152`(十六进制 `0x10000` / `0xc000` 转十进制),tmem 行打印 `600 512`。

**预期结果**:两组数字都能被正确解析,说明正则与 `tileiras` 输出格式匹配。若想验证真实格式,可在能触发超限的 kernel 上捕获 `tileiras` 的 stderr(待本地验证,需 Blackwell 且故意调大配置)。

#### 4.3.5 小练习与答案

**Q1**:`OutOfResources` 的 `__str__` 提示「Reducing block sizes or `num_stages` may help」。结合 u2-l2,为什么减小 `num_stages` 有助于缓解 shared memory 超限?

**参考答案**:`num_stages` 控制 software pipelining 的流水级数,每一级通常要额外分配一份 shared memory 缓冲。级数越多,shared memory 占用越大。减小 `num_stages` 能直接降低 shared memory 占用,从而可能让超限的配置变得合法。

**Q2**:为什么不把 `tileiras` 的所有失败都归为 `OutOfResources`,让 autotuner 全部剪枝掉?

**参考答案**:因为崩溃类失败(segfault、不支持的情形)**不是「配置太大」**。如果把它们也当资源超限剪枝,autotuner 会悄悄跳过,用户根本看不到工具链出了 bug;正确的做法是归为 `TileirasError` 并 `logging.warning` 记录,让失败可见、可复现、可上报。只有真正「配置超过硬件上限」这一类可预期的失败,才适合剪枝。

---

## 5. 综合实践

把本讲三个模块串起来,完成下面这个**源码阅读 + 命令复现**任务。

**任务背景**:假设你在调一个 TileIR kernel,`tileiras` 报错退出了。你需要搞清楚:(a) 它是被怎么调起来的;(b) 它的 `CUDA_HOME` 从哪来;(c) 它的报错属于哪一类。

**操作步骤**:

1. **追命令构造**:从 [compiler.py:205](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/compiler.py#L205) 开始,通读 `call_tileiras` 到第 277 行,写出 `tileiras` 的完整命令模板,标出每个参数的来源(`opt_level` 来自哪、`capability` 来自哪、bytecode 文件来自哪)。

2. **解释 CUDA_HOME 推导**:用自己的话回答——为什么 `CUDA_HOME` 要从 `tileiras` 路径「`dirname(dirname(...))`」反推,而不是直接读系统 `CUDA_HOME`?如果系统装的是 CUDA 13.2、而内置工具链是 13.3,直接读系统变量会有什么风险?(提示:对照 [conf.py:58-72](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/conf.py#L58-L72) 的注释。)

3. **手写两条资源超限正则**:不查源码,默写出 shared memory 与 tmem 两条正则,并指出它们一个用十六进制、一个用十进制的原因。然后对照 [compiler.py:244-260](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/compiler.py#L244-L260) 校对。

4. **分类判断**:给定三种 `tileiras` 退出情形,说出会抛哪个异常:
   - (a) stderr 含 `uses too much shared data. 0x20000 bytes, 0x10000 max`;
   - (b) 退出码 `-11`(SIGSEGV),stderr 是段错误堆栈;
   - (c) stderr 含 `allocated tmem out of resource: 700 vs 512`。

**预期结果**:

- (1) 命令模板:`tileiras --gpu-name=sm_<cap> --opt-level=<opt_level> <bytecode_file> -o <cubin_file>`,其中 `opt_level` 来自 `TileIROptions.opt_level`(默认 3),`capability` 来自 `add_stages` 里 `int(self._parse_arch(options.arch))`,bytecode 文件来自 `tileir.write_bytecode(mod)` 经缓存管理器落盘。
- (2) 见 4.2.4 第二问的答案(避免旧工具链遮蔽匹配的 13.3)。
- (3) shared memory:`0x([0-9a-fA-F]+) bytes, 0x([0-9a-fA-F]+) max`(十六进制,因为输出带 `0x` 前缀);tmem:`allocated tmem out of resource:\s*([0-9]+)\s*vs\s*([0-9]+)`(十进制,因为输出是纯数字)。进制差异是 `tileiras` 输出格式决定的,必须照搬,否则解析出错。
- (4) (a) `OutOfResources(used=131072, limit=65536, "shared memory")`;(b) `TileirasError`(先 `logging.warning` 记录 repro);(c) `OutOfResources(used=700, limit=512, "tensor memory")`。

---

## 6. 本讲小结

- `make_cubin` 只是 `call_tileiras` 的薄壳;真正的 cubin 生成由外部 NVIDIA 编译器 `tileiras`(随 CUDA 13.1/13.3 的 cuda-tile 发布)完成,它是独立进程,只消费 `cuda_tile` bytecode。
- Triton 用 `write_bytecode` 把 IR 序列化:它从 builtin `ModuleOp` 里挑出**嵌套的 `cuda_tile::ModuleOp`**(即模块体第一个操作),只对它做 bytecode,产物以 `{name}.bytecode` 落盘缓存,既给 `tileiras` 用、又便于事后复现。
- `tileiras` 命令是 `tileiras --gpu-name=sm_<cap> --opt-level=<opt_level> <bytecode> -o <cubin>`,**没有任何 Python 旋钮**——occupancy/num_ctas/num_stages/approx/ftz 已在 `make_tileir` 烘焙进 IR。
- `CUDA_HOME` 刻意从 `tileiras` 路径做两次 `dirname` 反推,**不读系统变量**,且只注入子进程环境(不污染全局),目的是防止陈旧系统 CUDA 遮蔽匹配的 13.3 工具链。
- 错误分两类:资源超限(shared memory / TMEM)抛 `OutOfResources`,autotuner 可剪枝,分别用十六进制与十进制正则解析;其余失败(含被信号杀死的崩溃)抛 `TileirasError`,先 `logging.warning` 再上抛,保证失败可见。
- 本讲讲的是**编译期**错误分类;与 jit.py 里**运行期** fallback(`tileir_run` / `HitFallback` / `TRITON_TILEIR_RUNTIME_FALLBACK`)是两回事,后者在 u4-l3 讲。

---

## 7. 下一步学习建议

本讲把流水线最后一段 `make_cubin` 讲完了,至此「Python 后端层」(u2 全单元)已闭合。接下来建议:

1. **进入 u3 单元(MLIR 转换 Pass 体系)**:`tileiras` 消费的是 `cuda_tile` bytecode,而这些算子是怎么从 `tt.*` 转换来的?u3-l1「转换 Pass 的 C++ 插件入口与骨架」会从 `triton_tileir.cc` 的 pybind、`Passes.td`、转换 target 骨架讲起,正好承接本讲提到的 `write_bytecode`、`only_contain_legal_dialects` 等 C++ 入口。
2. **若对工具链与测试感兴趣**:可先跳读 u4-l1「triton-cuda-tile-opt 工具与 lit/FileCheck 测试」——你会学到如何用 `triton-cuda-tile-opt` 独立跑 pass、用 lit/FileCheck 验证 IR 转换,这能帮你在不动 `tileiras` 的情况下复现和调试 IR。
3. **若对容错机制感兴趣**:直接看 u4-l3「编译期与运行期 Fallback 容错」,把本讲的编译期错误与运行期 `HitFallback` 串成一张完整的容错图。
