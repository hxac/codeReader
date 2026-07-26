# 运行时加载与 Bisheng 设备编译

## 1. 本讲目标

在前一讲（[u6-l2 Ascend C / PTO 双 Codegen](u6-l2-dual-codegen.md)）里，我们看到 `device_codegen` 把 TIR 翻译成了一段**人类可读的 C++ 源码**（Ascend C / PTO IR）。但一段源码并不能直接在 NPU 上跑——它还要被编译成动态库、被 Python 加载、被正确地"启动"到 AI Core 上。

本讲就来补全这条链路的最后一公里。学完本讲，你应该能够：

- 说清楚一段 Ascend C 源码是如何被 **bisheng 编译器** 编译成 `.so`，又是如何被 **ctypes / Cython** 加载回 Python 的；
- 区分 **ascendc（`-xasc`）** 与 **pto（`-xcce`）** 两条 bisheng 调用路线在编译命令上的关键差异；
- 读懂 codegen 在源码里偷偷生成的那个 host 端 `extern "C" void call(...)` 函数，以及它用 `<<<core, nullptr, stream>>>` 启动 kernel 的写法；
- 理解在没有真实 A5 NPU 时，**camodel 仿真** 如何在编译期用 `libruntime_camodel.so` 替换 `libruntime.so` 来"骗"过加载器。

本讲是整个 u6 单元的"运行时"收口，承接 u6-l2（codegen 产出源码），也是 u7 实战（torch 集成、调试、camodel 仿真）的前置。

## 2. 前置知识

阅读本讲前，你需要先建立以下几个心智模型（它们都来自前置讲义）：

- **TVM 的"源码级产物"**：tile-lang 的 `lower()` 产出的是 `CompiledArtifact`，其中 `kernel_source` 是一段 **C++ 字符串源码**，而不是二进制（见 [u1-l5](u1-l5-jit-and-pipeline.md)）。本讲要做的事，就是把这段字符串变成可执行的 `.so`。
- **两条 codegen 路线**：`target.model=ascendc` 走 Catlass/AscendC 风格，`target.model=pto` 走 PTO IR 指令风格（见 [u6-l2](u6-l2-dual-codegen.md)）。两条路线产出的源码方言不同，**因此喂给 bisheng 的编译开关也不同**——这是本讲的核心差异点之一。
- **CANN 与 bisheng**：CANN（Compute Architecture for Neural Networks）是华为昇腾的软件栈；**bisheng（毕昇）** 是 CANN 提供的设备端 C/C++ 编译器，相当于昇腾版的 `nvcc`，能把 Ascend C 代码编成可在 AI Core 上运行的机器码（见 [u1-l2](u1-l2-install-and-build.md)）。
- **ctypes 与 Cython**：两者都是 Python 调用 C/C++ 动态库的手段。ctypes 是标准库，直接通过函数符号调用；Cython 则先把 `.pyx` 编成一层 C++ 胶水 `.so`，再做桥接。tile-lang 默认走 **Cython** 后端。

几个昇腾特有的概念，本讲会用到：

| 术语 | 含义 |
|------|------|
| **bisheng** | CANN 的设备端编译器，类似 `nvcc`，支持 `-xasc`（AscendC）与 `-xcce`（cce/PTO）两种语言模式 |
| **aclrtStream** | Ascend CL 运行时（ACL Runtime）的异步执行流，类似 CUDA Stream |
| **AI Core / 核启动** | kernel 不是在"线程"上跑，而是被启动到一组 AI Core 上，`<<<core, ...>>>` 即声明启动多少个核 |
| **fftsAddr** | Fast Fourier Task Share 控制地址，kernel 启动时由 host 通过 `rtGetC2cCtrlAddr` 取回并传入设备侧 |
| **camodel** | Cycle-Accurate Model，CANN 提供的节拍精确软件仿真器，可在纯 CPU 机器上模拟 NPU |

## 3. 本讲源码地图

本讲涉及的关键文件，按"从 Python 调用到设备执行"的顺序排列：

| 文件 | 角色 |
|------|------|
| `tilelang/jit/kernel.py` | `JITKernel`：JIT 外壳，`__call__` 入口与 `_compile_and_create_adapter` 编排 |
| `tilelang/jit/adapter/cython/adapter.py` | `CythonKernelAdapter`：把源码交给 bisheng 编译、加载 `.so`、构建 Python 可调用对象 |
| `tilelang/jit/adapter/cython/cython_wrapper.pyx` | `CythonKernelWrapper.forward`：运行时把 torch 张量打包成指针、调用 `lib.call` |
| `tilelang/jit/adapter/libgen.py` | `LibraryGenerator`：拼装 bisheng 命令、执行编译、加载动态库、处理 camodel 仿真替换 |
| `tilelang/jit/adapter/wrapper.py` | `TLWrapper`：源码包装层（NPU 路径是 no-op 直通，与 CUDA 路径对比） |
| `src/target/codegen_ascend.cc` | C++ codegen，`PrintHostFunc` 在源码里生成 host 端 `call` 函数与 `<<<core>>>` 启动 |
| `.agents/skills/tilelang-a5-sim-convert/scripts/run_a5_sim_template.py` | camodel 仿真脚本，展示纯 RT API 调用 `lib.call` 的运行时形态 |
| `docs/TileLang-Ascend Programming Guide.md` | 官方对"编译/运行两阶段"与 camodel 仿真的说明 |

---

## 4. 核心概念与源码讲解

### 4.1 整体运行链路：从 `func(a, b)` 到 NPU 执行

#### 4.1.1 概念说明

在动手读源码前，先把整条链路看全。tile-lang 在昇腾上的运行，本质是"**两次编译 + 一次加载 + 一次调用**"：

- **第一次编译（编译期，发生一次）**：TIR 经多轮 Pass + codegen，降级成 Ascend C **源码字符串**（[u6-l1](u6-l1-pass-overview.md)、[u6-l2](u6-l2-dual-codegen.md)）。
- **第二次编译（JIT 期，每个 shape 一次）**：`LibraryGenerator` 把这段源码字符串交给 **bisheng**，编出设备 `.so`。
- **加载**：`ctypes.CDLL` 把 `.so` 加载进 Python 进程，拿到其中的 `call` 符号。
- **调用（每次 `func(a, b)`）**：Cython 包装层把 torch 张量指针 + stream 打包，调用 `.so` 里的 `call`，`call` 内部用 `<<<core, nullptr, stream>>>` 把设备函数 `_kernel` 启动到 AI Core。

> ⚠️ 一个容易混淆的点：tile-lang 在 NPU 上**不使用 TVM 的设备端 runtime**（`tvm.runtime.Module`）。codegen 直接把"host 启动器 + 设备函数"两样东西一起写进同一份 C++ 源码，编进同一个 `.so`。Python 侧只需 `ctypes` 加载这一个 `.so`、调用其中的 `call` 即可。这一点和 CUDA/ROCm 路线（走 TVM graph runtime）有本质区别。

#### 4.1.2 核心流程

下面这张流程图标出了**每一步落在哪个文件的哪个函数**，是本讲后续各节的导读图：

```
用户: func(a, b)
  │
  │  tilelang/jit/kernel.py:184  JITKernel.__call__
  │    └─ _generate_extra_args: 把动态符号变量(如 N)从输入张量 shape 中解出
  ▼
self.torch_function(...)   ← adapter._convert_torch_func() 返回的 lambda
  │
  │  tilelang/jit/adapter/cython/cython_wrapper.pyx:75  CythonKernelWrapper.forward
  │    ├─ 为 result_idx / workspace_idx / auto_gm_idx 分配输出与临时张量(torch.empty, device=npu)
  │    ├─ 解析动态符号 → 把张量 data_ptr 转成 ctypes.c_void_p
  │    ├─ 取 torch.npu.current_stream().npu_stream
  │    └─ self.lib.call(*call_args)        # lib = kernel .so
  ▼
extern "C" void call(...)    ← 由 src/target/codegen_ascend.cc:1131  PrintHostFunc 生成
  │    ├─ rtGetC2cCtrlAddr(&fftsAddr, &fftsLen)   # 取 ffts 控制地址
  │    └─ <name>_kernel<<<core, nullptr, stream>>>(args..., fftsAddr)   # bisheng 核启动语法
  ▼
设备函数 _kernel 在 AI Core 上执行
```

而"**编译期**"那条支线（首次调用前）则是：

```
JITKernel.__init__ → _compile_and_create_adapter (kernel.py:203)
  ├─ tilelang.lower(...)  →  artifact.kernel_source (Ascend C 源码字符串)
  └─ CythonKernelAdapter.__init__ (adapter.py:211)
       ├─ TLWrapper("npu").wrap(kernel_source)   # NPU: 直通, 原样返回
       ├─ LibraryGenerator.compile_lib()          # libgen.py:142 → 调 bisheng 编出 .so
       ├─ LibraryGenerator.load_lib()             # libgen.py:130 → ctypes.CDLL
       └─ CythonKernelWrapper(..., lib)           # 绑定 .so 句柄
```

#### 4.1.3 源码精读

先看 JIT 的对外入口 `__call__`，它非常薄，只做一件事：把动态符号变量从实际输入张量的 shape 里解出来，追加到参数末尾，然后透传给真正的可调用对象。

[tilelang/jit/kernel.py:184-201](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/kernel.py#L184-L201)：`JITKernel.__call__`，先经 `_generate_extra_args` 把符号变量（如动态 `N`）按 `(buffer_idx, shape_dim)` 从 `args` 的 shape 中取出并追加，再调用 `self.torch_function`。

```python
def __call__(self, *args, **kwds):
    modify_args = self._generate_extra_args(*args)   # 追加动态符号值
    return self.torch_function(*modify_args, **kwds)
```

`self.torch_function` 来自哪？看 adapter：

[tilelang/jit/adapter/cython/adapter.py:451-457](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/adapter/cython/adapter.py#L451-L457)：`_convert_torch_func` 把 Cython 的 `forward` 包成一个普通 Python 函数返回。

```python
def _convert_torch_func(self) -> Callable:
    def lambda_forward(*args, stream: int = -1):
        return self.cython_wrapper.forward([*args], stream=stream)
    return lambda_forward
```

到这里，"Python 调用"就正式交棒给了 Cython 层的 `forward`（4.4 节展开）。整条链路的"骨架"就是这么简单——复杂性全在两处：**编译（4.2）** 和 **`call` 函数本身（4.3）**。

#### 4.1.4 代码实践

**实践目标**：不运行任何东西，纯靠阅读，把上面两张流程图与本节列出的源码行号一一对应起来。

**操作步骤**：

1. 打开 [tilelang/jit/kernel.py:184](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/kernel.py#L184)，确认 `__call__` 只调了 `self.torch_function`。
2. 顺着 `self.torch_function = adapter.func`（`__init__` 中赋值）找到 `adapter._convert_torch_func`，确认它返回的是 `cython_wrapper.forward`。
3. 跳到 [cython_wrapper.pyx:197](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/adapter/cython/cython_wrapper.pyx#L197)，确认最终落点是 `self.lib.call(...)`。

**预期结果**：你能在脑中复现"`__call__` → `forward` → `lib.call`"这条三跳链路，并能指出每跳的文件与行号。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `__call__` 里要先调 `_generate_extra_args` 再传参，而不是直接 `self.torch_function(*args)`？

> **参考答案**：因为 kernel 可能含动态符号变量（如 `T.dynamic("N", ...)`），这些变量不是用户显式传入的参数，而是要从某个输入张量的实际 shape 中推导。`_generate_extra_args` 按 `dynamic_symbolic_map` 记录的 `(buffer_idx, shape_dim)` 从 `args` 里取出实际数值并追加到参数末尾，设备函数才能拿到具体的维度。

**练习 2**：链路里说"NPU 不使用 TVM 设备端 runtime"，你能从哪个代码点看出这一点？

> **参考答案**：从 `CythonKernelAdapter` 用 `ctypes.CDLL` 直接加载 `.so`、并直接调用其中的 `call` 符号（[adapter.py:271](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/adapter/cython/adapter.py#L271) 与 [cython_wrapper.pyx:197](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/adapter/cython/cython_wrapper.pyx#L197)）。对比之下，`dlpack` 后端才会要求 `artifact.rt_mod is not None`（[kernel.py:257](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/kernel.py#L257)），那条路才走 TVM runtime。

---

### 4.2 LibraryGenerator：bisheng 把 Ascend C 源码编成 `.so`

#### 4.2.1 概念说明

`LibraryGenerator`（库生成器）是整条链路里"把字符串变成 `.so`"的那个角色。它做三件事：

1. 把 codegen 产出的源码字符串写到一个临时 `.cpp` 文件；
2. **拼装一条 bisheng 命令**（含头文件搜索路径、链接库、语言模式开关），调 `subprocess` 执行；
3. 提供 `load_lib` 用 `ctypes.CDLL` 把产物 `.so` 加载回来。

它的两个关键设计点：

- **两条 bisheng 调用路线**：`ascendc` 用 `-xasc`、`pto` 用 `-xcce`，这俩是 bisheng 的"语言模式"开关，决定了它按哪种方言去解析源码（Catlass/AscendC vs cce/PTO IR）。两套命令的头文件、宏、链接库也各不相同。
- **编译标志"后追加、最后赢"**：框架先派生一组默认 flag（优化等级、auto-sync 开关、debug 开关），再把用户传的 `compile_flags` **追加在最后**。因为 bisheng 对重复 flag 是"last-wins"（后者覆盖前者），所以用户传的 flag 总能覆盖框架默认。

#### 4.2.2 核心流程

`compile_lib` 的执行流程（[libgen.py:142-273](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/adapter/libgen.py#L142-L273)）：

```
1. tempfile 建一个临时 .cpp, 推导出同名 .so 路径
2. 取 ASCEND_HOME_PATH(CANN 根) 与 TL_ROOT(tile-lang 根)
3. 解析 compile_flags: 若为 None 则用 resolve_compile_flags() 派生默认
4. 按 self.target 选命令模板:
     - ascendc / auto  →  bisheng --npu-arch=dav-2201 -std=c++17 -xasc ... -lruntime -lascendcl ...
     - pto              →  bisheng --cce-aicore-arch=<ccec> -xcce ... -lruntime ...
5. (可选) TL_RUN_MODE=sim 时: 插入 camodel 库路径, 把 -lruntime 换成 -lruntime_camodel
6. 追加 compile_flags 到命令末尾(last-wins), 加 -o libpath
7. src.write(lib_code); subprocess.run(command)
8. 失败则抛 RuntimeError, 成功则记录 srcpath / libpath
```

#### 4.2.3 源码精读

先看 bisheng 命令是怎么按 target 分叉的。

[tilelang/jit/adapter/libgen.py:152-183](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/adapter/libgen.py#L152-L183)：**ascendc 路线**——`bisheng --npu-arch=dav-2201 -std=c++17 -xasc`，关键 `-I` 包含 CANN 头文件、`catlass`、`shmem` 三个模板库目录，链接 `-lruntime -lascendcl -ltiling_api -lplatform -lc_sec`，并定义 `-DBACKEND_HYBM`。

```python
if self.target == "ascendc" or self.target == "auto":
    command = [
        "bisheng", "--npu-arch=dav-2201", "-std=c++17", "-xasc",
        f"-I{ASCEND_HOME_PATH}/include",
        f"-I{TL_ROOT}/3rdparty/catlass/include",       # AscendC 模板库
        f"-I{TL_ROOT}/3rdparty/shmem/include",          # 核间通信模板库
        "-DBACKEND_HYBM",
        ...
        "-lruntime", "-lascendcl", "-ltiling_api", "-lplatform", "-lc_sec",
        "-fPIC", "--shared", src.name,
    ]
```

[tilelang/jit/adapter/libgen.py:184-228](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/adapter/libgen.py#L184-L228)：**pto 路线**——`bisheng --cce-aicore-arch=<ccec> -xcce`，其中 `ccec`（cce 架构名）按平台变化：A5 用 `dav-c310`、其它用 `dav-c220`；同时按平台定义 `REGISTER_BASE`（A5）或 `MEMORY_BASE` 宏。注意它的模板库是 `pto-isa`（不是 catlass），还带一组 `-mllvm -cce-aicore-*` 的后端旋钮。

```python
elif self.target == "pto":
    ccec = "dav-c310" if self.platform == "A5" else "dav-c220"
    memory = "REGISTER_BASE" if self.platform == "A5" else "MEMORY_BASE"
    command = [
        "bisheng", f"--cce-aicore-arch={ccec}", f"-D{memory}",
        "-std=gnu++17", "-xcce",
        "-mllvm", "-cce-aicore-addr-transform",
        ...
        f"-I{TL_ROOT}/3rdparty/pto-isa/include",       # PTO 指令宏库
        "-lruntime", "-lascendcl", ...
    ]
```

> 这两段命令的差异，正是 [u6-l3](u6-l3-templates.md) 讲的两套模板库（`catlass` vs `pto-isa`）在编译侧的体现：**源码方言 + 模板库 + bisheng 语言模式**三者必须配对，不能混。

接着看 flag 派生与"后追加"机制。

[tilelang/jit/adapter/libgen.py:81-110](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/adapter/libgen.py#L81-L110)：`resolve_compile_flags` 派生默认 flag：先按 `opt_level` 给 `-O{level}`；ascendc 在关闭 cce-auto-sync 时加 `--cce-auto-sync=off`（PTO 默认就关，故不发该 flag）；PTO 在 `pto_debug` 时加 `-D_DEBUG --cce-enable-print`。这些与 [u4-l3](u4-l3-auto-sync.md) 的自动同步开关联动。

[tilelang/jit/adapter/libgen.py:256-264](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/adapter/libgen.py#L256-L264)：派生 flag **追加在命令末尾**，bisheng 是 last-wins，因此用户传的 `compile_flags`（如 `["-O0"]`、`["--cce-auto-sync=on"]`）能覆盖框架默认；同时做空白切分与去重。

```python
# 追加 resolved flags 到最后, last-wins
command += [item for flag in compile_flags
            for item in flag.split() if item not in command]
command += ["-o", libpath]
```

最后是真正执行编译与加载的地方。

[tilelang/jit/adapter/libgen.py:262-273](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/adapter/libgen.py#L262-L273)：写源码、`subprocess.run(command, timeout=timeout)`、按返回码判定成败。

[tilelang/jit/adapter/libgen.py:130-140](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/adapter/libgen.py#L130-L140)：`load_lib` 用 `ctypes.CDLL(lib_path)` 加载 `.so`；若 `TL_RUN_MODE=sim`，先把 camodel 库目录塞进 `LD_LIBRARY_PATH`，让仿真运行时库优先被找到。

#### 4.2.4 代码实践

**实践目标**：亲手拼出一条 ascendc 的 bisheng 命令，理解每个 `-I` / `-l` 的来源。

**操作步骤**：

1. 读 [libgen.py:152-183](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/adapter/libgen.py#L152-L183)，把命令里所有 `-I` 路径分成三类：**CANN 头**（`ASCEND_HOME_PATH` 下）、**模板库头**（`TL_ROOT/3rdparty/...`）、**tile-lang 自带头**（`TILELANG_TEMPLATE_PATH`）。
2. 对照 [u1-l2](u1-l2-install-and-build.md) 讲过的 setup.py 打包逻辑，回答：为什么 wheel 里必须带上 `catlass/shmem/pto-isa` 的 include 目录？
3. （可选，待本地验证）在有 CANN 的机器上，给 `example_gemm.py` 加一行 `print(func.get_kernel_source())`，在生成的源码顶部找到 `#include` 行，回过头与这里的 `-I` 列表对应。

**需要观察的现象**：生成的源码会 `#include` catlass/shmem 的头（ascendc）或 pto-isa 的头（pto），这正是 `compile_lib` 必须把这些目录加进 `-I` 的原因。

**预期结果**：你能解释"JIT 编译为何需要这些头文件目录"——因为 codegen 产出的源码本身会 `#include` 它们（见 [u6-l3](u6-l3-templates.md)）。

#### 4.2.5 小练习与答案

**练习 1**：如果用户既想让 PTO 后端打印调试信息，又想把优化等级压到 `-O0`，应该怎么传 `compile_flags`？

> **参考答案**：传 `compile_flags=["-O0"]`。`-D_DEBUG --cce-enable-print` 由 `pto_debug=True`（经 pass_config `tl.pto_debug`）自动派生；而 `-O0` 由用户追加在命令末尾、last-wins 覆盖默认 `-O3`。注意 `resolve_compile_flags` 只发框架默认，用户的 `-O0` 必须走 `compile_flags` 入口。

**练习 2**：为什么 `-xasc` 和 `-xcce` 不能混用、且必须和 target 配对？

> **参考答案**：`-xasc` 让 bisheng 按 AscendC（Catlass 对象模型）方言解析，`-xcce` 让它按 cce/PTO IR（指令宏）方言解析。codegen 产出的源码方言（[u6-l2](u6-l2-dual-codegen.md)）与 `#include` 的模板库（catlass vs pto-isa，[u6-l3](u6-l3-templates.md)）都是和语言模式一一配对的，错配会导致编译期符号找不到或语义错乱。

---

### 4.3 codegen 的 `PrintHostFunc`：host 端 `call` 与 `<<<core>>>` 核启动

#### 4.3.1 概念说明

在 CUDA 路线里，"host 端怎么启动 kernel"这件事由 **Python 侧** 的 `TLCUDASourceWrapper.create_dispatch_func` 生成（它会拼出一个 `extern "C" int call(...)`，里面是 `kernel<<<grid, block, smem, stream>>>(...)`）。

而在 **NPU 路线**，这件事被**前移到了 C++ codegen 里**——`codegen_ascend.cc` 的 `AddFunction` 在打印完设备函数 `_kernel` 之后，紧接着调用 `PrintHostFunc`，把 host 端的 `call` 函数**一起写进同一份源码**。正因如此，`TLWrapper("npu").wrap()` 才能是个 no-op 直通（4.4 节会看到）。

`call` 函数做三件事：

1. **接收 host 侧参数**：每个张量参数声明为 `uint8_t*`（裸指针），最后额外加一个 `aclrtStream stream`；
2. **取 ffts 控制地址**：调 `rtGetC2cCtrlAddr(&fftsAddr, &fftsLen)`，这是 Ascend 运行时的一个 host API，取回设备侧需要的控制地址；
3. **核启动**：用 bisheng 的 `<<<core, nullptr, stream>>>` 语法，把 `_kernel` 启动到 `core` 个 AI Core 上，参数末尾带上 `fftsAddr`。

#### 4.3.2 核心流程

设备函数与 host 函数由同一个 `AddFunction` 顺序产出（[codegen_ascend.cc:1184-1299](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/codegen_ascend.cc#L1184-L1299)）：

```
AddFunction(f):
  ├─ 打印设备函数 <name>_kernel(...) { ... }      # body 由 PrintStmt(f->body) 生成
  └─ PrintHostFunc(f, name, core_num_, shape_vars):
       ├─ ProcessTilingInput  : 生成 tiling 计算函数(把 host 传入的 shape 算成分块参数)
       ├─ extern "C" void call(参数..., aclrtStream stream) {
       │     uint32_t fftsLen{0}; uint64_t fftsAddr{0};
       │     rtGetC2cCtrlAddr(&fftsAddr, &fftsLen);
       │     CallTilingInput(...)                       # 调 tiling 函数填充分块参数
       │     <name>_kernel<<<core, nullptr, stream>>>(参数..., tiling..., fftsAddr);
       │  }
```

其中 `core_num_` 是启动的核数，它来自设备函数体里 `blockIdx.x` 那条 `thread_extent`（即 `T.Kernel(block_num)` 的 `block_num`）。

#### 4.3.3 源码精读

[src/target/codegen_ascend.cc:1131-1182](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/codegen_ascend.cc#L1131-L1182)：`PrintHostFunc` 的核心——声明 `extern "C" void call(...)`，参数逐个按 `is_handle()` 判定印成 `uint8_t*` 或具体类型，末尾追加 `aclrtStream stream`；函数体先取 fftsAddr，再用 `<<<core, nullptr, stream>>>` 启动设备函数。

```cpp
os << "extern \"C\" void call(";
for (size_t i = 0; i < f->params.size(); ++i) {
  ...
  if (v.dtype().is_handle()) os << "uint8_t* " << v->name_hint;   // 张量→裸指针
  else                        os << getType(v.dtype()) << " " << v->name_hint;
}
...
os << ", aclrtStream stream) {\n  ";
os << "uint32_t fftsLen{0};\n  uint64_t fftsAddr{0};\n  ";
os << "rtGetC2cCtrlAddr(&fftsAddr, &fftsLen);\n";      // 取 ffts 控制地址
...
os << name << "<<<" << core << ", nullptr, stream>>>("; // bisheng 核启动语法
for (auto &arg_name : arg_names) os << arg_name << ", ";
...
os << ", fftsAddr);\n";                                 // 末尾带 fftsAddr
os << "}\n";
```

`core_num_` 在哪里被赋值？它在 codegen 遍历 `blockIdx.x` 的 launch/thread 绑定时捕获：

[src/target/codegen_ascend.cc:761](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/codegen_ascend.cc#L761)：`this->core_num_ = PrintExpr(op->value);`——把 `blockIdx.x` 的 extent（即 `T.Kernel(block_num)` 里的核数）记下来，供后续 `PrintHostFunc` 填进 `<<<core>>>`。对于 GEMM，`core = m_num × n_num`，即：

\[
\text{core\_num} = \text{m\_num} \times \text{n\_num} = \left\lceil \frac{M}{\text{block\_M}} \right\rceil \times \left\lceil \frac{N}{\text{block\_N}} \right\rceil
\]

最后看 `AddFunction` 如何把设备函数和 host 函数串起来：

[src/target/codegen_ascend.cc:1284-1298](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/codegen_ascend.cc#L1284-L1298)：先打印设备函数体（`PrintStmt(f->body)`），函数签名末尾固定带 `, uint64_t fftsAddr`；随后调用 `PrintHostFunc` 把 host 端 `call` 也写进同一份源码。

```cpp
stream << ", uint64_t fftsAddr";
stream << ") {\n";
this->PreFunctionBody(f);
this->PrintStmt(f->body);                       // 设备函数体
...
PrintHostFunc(f, func_name, stream, this->core_num_, shape_vars);  // host call 函数
```

> 一个细节：设备函数签名末尾那个 `uint64_t fftsAddr`，正是 host 的 `call` 在 `<<<...>>>` 里最后传进来的同一个 `fftsAddr`。两边靠这个参数把 host 取到的控制地址透传到设备侧。

#### 4.3.4 代码实践

**实践目标**：在生成的源码里亲眼看到 `call` 函数与 `<<<core>>>` 启动。

**操作步骤**：

1. 读 [src/target/codegen_ascend.cc:1131-1182](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/codegen_ascend.cc#L1131-L1182)，记住 `call` 的三段式结构（参数声明 → fftsAddr → 核启动）。
2. （待本地验证）在有 CANN 的机器上运行 GEMM 示例并打印源码：

   ```python
   # 示例代码: 在 example_gemm.py 获得 func 后加一行
   print(func.get_kernel_source())
   ```

3. 在打印出的源码里定位 `extern "C" void call(`，确认它的参数都是 `uint8_t*`、末尾是 `aclrtStream stream`；再定位 `<<<..., nullptr, stream>>>`，确认里面的核数等于 `m_num * n_num`。

**需要观察的现象**：同一份 `.cpp` 里既有设备函数 `<name>_kernel`，又有 host 函数 `call`；`call` 的核启动语法 `<<<core, nullptr, stream>>>` 里，`core` 是一个具体的整数（如 32）。

**预期结果**：你能在生成的源码里划出"设备侧"与"host 侧"两段，并指出 `fftsAddr` 如何从 host 流到设备。

#### 4.3.5 小练习与答案

**练习 1**：为什么 NPU 路线下 `TLWrapper("npu").wrap()` 可以是 no-op 直通，而 CUDA 路线不行？

> **参考答案**：因为 NPU 的 host `call` 函数已经由 C++ codegen 的 `PrintHostFunc` 直接写进了设备源码（[codegen_ascend.cc:1297](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/codegen_ascend.cc#L1297)），源码本身已"自包含"。CUDA 路线的 host 启动逻辑不在 codegen 里，必须由 Python 侧 `TLCUDASourceWrapper.create_dispatch_func` 现场拼一个 `call` 追加上去。

**练习 2**：`call` 函数里 `rtGetC2cCtrlAddr` 取到的 `fftsAddr`，最终被谁消费？

> **参考答案**：被设备函数 `<name>_kernel` 消费——`call` 把 `fftsAddr` 作为 `<<<core, nullptr, stream>>>(..., fftsAddr)` 的最后一个实参传入，设备函数签名末尾正有形参 `uint64_t fftsAddr`（[codegen_ascend.cc:1288](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/codegen_ascend.cc#L1288)）。它是 host 与设备间传递控制平面地址的桥梁。

---

### 4.4 CythonKernelAdapter：Python 到 `lib.call` 的桥

#### 4.4.1 概念说明

`CythonKernelAdapter` 是默认后端（`execution_backend="cython"`）。它一头连着 `JITKernel`（提供编译产物），一头连着 Python 用户（提供可调用对象）。它的工作分**初始化期**和**每次调用期**两段：

- **初始化期**：把 codegen 源码交给 `LibraryGenerator` 编出 `.so` 并加载，再构建一个 Cython 的 `CythonKernelWrapper` 把"张量指针打包 + 调 `lib.call`"封成 `forward`。
- **每次调用期**：`forward` 负责为输出/临时张量分配 NPU 内存、解析动态符号、把 torch 张量的 `data_ptr` 转成 `ctypes.c_void_p`、取当前 NPU stream，最后调 `lib.call`。

这里有一个**两层 `.so`** 的设计，容易让人困惑，需要特别讲清：

| 动态库 | 谁编译 | 用什么编译器 | 干什么 |
|--------|--------|--------------|--------|
| **kernel `.so`**（含 `_kernel` + `call`） | `LibraryGenerator.compile_lib` | **bisheng**（`-xasc`/`-xcce`） | 设备代码，运行在 AI Core |
| **cython_wrapper `.so`**（含 `forward`） | `CythonKernelAdapter` 模块导入时 | **系统 C++ 编译器**（`get_cplus_compiler()`，通常是 g++） | Python↔C 桥，持有 kernel `.so` 句柄并调 `lib.call` |

也就是说：bisheng 只负责设备 `.so`；那层 Cython 胶水 `.so` 是用普通 g++ 编的，跟 bisheng 无关。

#### 4.4.2 核心流程

初始化期（[adapter.py:211-287](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/adapter/cython/adapter.py#L211-L287)）：

```
CythonKernelAdapter.__init__:
  ├─ 解析动态符号/dtype/指针/静态 shape/设备 → 一组 map
  ├─ wrapper = TLWrapper("npu")
  ├─ wrapped_source = wrapper.wrap(kernel_source)   # NPU: 直通, 原样返回 kernel_source
  ├─ lib_generator = LibraryGenerator(target, platform, compile_flags)
  ├─ lib_generator.update_lib_code(wrapped_source)
  ├─ lib_generator.compile_lib()                    # → bisheng 编出 kernel.so
  ├─ lib = lib_generator.load_lib()                 # → ctypes.CDLL
  └─ cython_wrapper = CythonKernelWrapper(result_idx, workspace_idx, auto_gm_idx, params, lib)
                       └─ forward 即运行期入口
```

每次调用期（[cython_wrapper.pyx:75-203](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/adapter/cython/cython_wrapper.pyx#L75-L203)）：

```
forward(inputs, stream=-1):
  ├─ 若 stream==-1: stream = torch.npu.current_stream().npu_stream
  ├─ 解析动态符号: 从 inputs[ref_idx].shape[ref_dim] 取实际值
  ├─ 遍历 params:
  │     - result_idx/workspace_idx → torch.empty(..., device=npu) 分配
  │     - auto_gm_idx             → torch.empty(...) 自动 GM workspace
  │     - 其它                    → 取自 inputs[ins_idx]
  ├─ 把每个张量 data_ptr → ctypes.c_void_p; 追加动态符号 int64; 追加 stream
  └─ self.lib.call(*call_args)                       # 进入 host call → <<<core>>>
```

#### 4.4.3 源码精读

先看初始化期如何把源码喂给 bisheng。

[tilelang/jit/adapter/cython/adapter.py:260-271](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/adapter/cython/adapter.py#L260-L271)：`TLWrapper("npu")` + `wrap(kernel_source)`（NPU 直通）→ `LibraryGenerator` 三连（`update_lib_code` / `compile_lib` / `load_lib`）。这就是 4.2 节 bisheng 编译的触发点。

```python
self.wrapper = TLWrapper("npu")
...
self.wrapped_source = self.wrapper.wrap(self.get_kernel_source(kernel_only=True))
self.lib_generator.update_lib_code(self.wrapped_source)
self.lib_generator.compile_lib()        # bisheng 编译
self.lib = self.lib_generator.load_lib()  # ctypes.CDLL
```

为什么 NPU 的 `wrap` 是直通？看 `TLWrapper.wrap`：

[tilelang/jit/adapter/wrapper.py:648-660](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/adapter/wrapper.py#L648-L660)：`TLWrapper("npu").wrap` 直接 `return c_source`，真正会拼 host `call` 的 `TLNPUSourceWrapper` 被注释掉了——因为这件事已经由 C++ codegen 的 `PrintHostFunc` 做完了（4.3 节）。

```python
def wrap(self, c_source: str):
    assert self.scheduled_ir_module is not None, ...
    # TODO: support NPU
    return c_source          # NPU: 原样返回, host call 已由 codegen 生成
```

再看运行期 `forward` 如何把张量打包并调用。

[tilelang/jit/adapter/cython/cython_wrapper.pyx:86-90](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/adapter/cython/cython_wrapper.pyx#L86-L90)：默认 stream 的获取——`torch.npu.current_stream().npu_stream`，这就是 `call` 最后那个 `aclrtStream stream` 实参的来源。

[tilelang/jit/adapter/cython/cython_wrapper.pyx:143-160](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/adapter/cython/cython_wrapper.pyx#L143-L160)：把每个张量转成 `ctypes.c_void_p(tensor.data_ptr())`（要求 contiguous），标量分别按 int/float/bool 处理。

[tilelang/jit/adapter/cython/cython_wrapper.pyx:189-197](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/adapter/cython/cython_wrapper.pyx#L189-L197)：追加动态符号值（`c_int64`）与 stream（`c_void_p`），然后执行 `self.lib.call(*call_args)`——这一行就是 Python 到设备 `.so` 的最后一跳。

```cython
# 追加动态符号
for _, (buffer_idx, shape_idx) in self.dynamic_symbolic_map.items():
    call_args.append(ctypes.c_int64(inputs[buffer_idx].shape[shape_idx]))
# 追加 npu stream
call_args.append(ctypes.c_void_p(stream))
# 执行 kernel
self.lib.call(*call_args)        # → extern "C" void call(...)
```

最后补一眼 Cython 胶水 `.so` 自己是怎么编的（用 g++，不是 bisheng）：

[tilelang/jit/adapter/cython/adapter.py:148-159](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/adapter/cython/adapter.py#L148-L159)：模块导入时把 `cython_wrapper.pyx` → `.cpp`（`cython --cplus`），再用 `get_cplus_compiler()`（系统 g++）编成 `cython_wrapper.so`，按源码哈希缓存。这与 4.2 节的 bisheng 编译是两条独立的链路。

#### 4.4.4 代码实践

**实践目标**：跟踪"一个 torch 张量"是如何变成设备 `.so` 里 `call` 的实参的。

**操作步骤**：

1. 读 [cython_wrapper.pyx:143-160](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/adapter/cython/cython_wrapper.pyx#L143-L160)，注意 `tensor.data_ptr()` 与 `is_contiguous()` 检查。
2. 读 [cython_wrapper.pyx:189-197](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/adapter/cython/cython_wrapper.pyx#L189-L197)，对照 4.3 节 `call` 的形参顺序（张量指针 → 动态符号 int64 → stream），确认实参顺序一一对应。
3. 回答：为什么传给 tile-lang 的 torch 张量必须是 contiguous？

**需要观察的现象**：实参的拼装顺序与 `PrintHostFunc` 生成的 `call` 形参顺序完全对齐——这正是两端能对上的契约。

**预期结果**：你能解释"`tensor.data_ptr()` → `c_void_p` → `call` 形参 `uint8_t*`"这一串指针传递。

#### 4.4.5 小练习与答案

**练习 1**：kernel `.so` 和 `cython_wrapper.so` 分别用什么编译器、各自承担什么职责？

> **参考答案**：kernel `.so` 由 **bisheng**（`-xasc`/`-xcce`）编译，承载设备函数 `_kernel` 与 host 启动器 `call`，运行在 AI Core；`cython_wrapper.so` 由**系统 g++**（`get_cplus_compiler()`）在模块导入时编译，承载 `forward`，负责把 torch 张量打包成指针并调 `lib.call`，是 Python↔C 的桥。两者一个走设备工具链、一个走主机工具链。

**练习 2**：如果输入张量不是 contiguous，`forward` 会怎样？

> **参考答案**：会在 [cython_wrapper.pyx:148-149](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/adapter/cython/cython_wrapper.pyx#L148-L149) 抛 `ValueError("Input tensor at index i must be contiguous")`。因为 `data_ptr()` 只给起点指针，非 contiguous 张量的跨步无法通过裸指针表达，而 `call` 形参只是 `uint8_t*`，没有 stride 信息。

---

### 4.5 camodel 仿真：编译期替换 `libruntime`

#### 4.5.1 概念说明

camodel（Cycle-Accurate Model）是 CANN 提供的**软件仿真器**，让你在没有真实 A5 NPU 的 x86/Linux 机器上也能跑 kernel、看指令 trace。它的关键思想极其简单：**真实运行和仿真运行，对 kernel `.so` 来说唯一的区别就是链接了哪个 runtime 库**。

- 真实运行：kernel `.so` 链接 `libruntime.so`（驱动真实 NPU）；
- 仿真运行：kernel `.so` 链接 `libruntime_camodel.so`（用 CPU 模拟 NPU 行为）。

两者对外接口完全一致，所以同一份源码、同一种 `<<<core>>>` 启动语法都能工作，差别只在"启动后真正执行指令的是硬件还是软件模拟器"。tile-lang 的做法是：在 `compile_lib` 里，当 `TL_RUN_MODE=sim` 时，把命令里的 `-lruntime` **替换成 `-lruntime_camodel`**，并确保仿真库目录在链接/加载路径里排在最前。

#### 4.5.2 核心流程

camodel 替换发生在 `compile_lib` 与 `load_lib` 两处（[libgen.py:230-254](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/adapter/libgen.py#L230-L254)、[libgen.py:133-139](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/adapter/libgen.py#L133-L139)）：

```
TL_RUN_MODE == "sim":
  compile_lib:
    ├─ sim_lib_path = _get_simulator_lib_path(ASCEND_HOME_PATH, platform)  # 找 camodel lib 目录
    ├─ 在 -L{ASCEND_HOME_PATH}/lib64 之前插入:
    │     -L{sim_lib_path}  -Wl,-rpath,{sim_lib_path}  -Wl,--disable-new-dtags
    └─ command 里把 "-lruntime" 替换成 "-lruntime_camodel"
  load_lib:
    └─ LD_LIBRARY_PATH 最前插入 sim_lib_path, 让 ctypes.CDLL 优先找到 camodel 库
```

`--disable-new-dtags` 是个关键细节：它让 `-rpath` 写成 `DT_RPATH`（可传递，子库也能用）而非 `DT_RUNPATH`（不可传递），这样 `libruntime_camodel.so` 自己依赖的 `libnpu_drv_camodel.so` 等也能被解析到。

> 仿真运行时，用户侧不再用 torch，而是直接用 RT API（`rtMalloc`/`rtMemcpy`/`rtStreamCreate`）管理设备内存，然后调用同一个 `kl.call(d_A, d_B, d_C, stream)`。`.agents/skills/tilelang-a5-sim-convert/scripts/run_a5_sim_template.py` 就是这套流程的模板。

#### 4.5.3 源码精读

[libgen.py:230-254](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/adapter/libgen.py#L230-L254)：编译期的 camodel 替换——先定位仿真库目录，把它作为 `-L` 与 `-rpath` 插到 CANN lib64 **之前**（保证链接优先级），再把 `-lruntime` 改成 `-lruntime_camodel`。

```python
run_mode = os.environ.get("TL_RUN_MODE", "npu")
if run_mode == "sim":
    sim_lib_path = _get_simulator_lib_path(ASCEND_HOME_PATH, self.platform)
    ascend_lib_idx = command.index(f"-L{ASCEND_HOME_PATH}/lib64")
    command.insert(ascend_lib_idx, f"-L{sim_lib_path}")
    command.insert(ascend_lib_idx + 1, f"-Wl,-rpath,{sim_lib_path}")
    command.insert(ascend_lib_idx + 2, "-Wl,--disable-new-dtags")
    ...
    rt_idx = command.index("-lruntime")
    command[rt_idx] = "-lruntime_camodel"        # 关键: 换运行时库
```

[libgen.py:130-140](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/adapter/libgen.py#L130-L140)：加载期把仿真库目录塞进 `LD_LIBRARY_PATH` 最前，让 `ctypes.CDLL` 优先解析到 `libruntime_camodel.so`。

仿真侧运行时的真实形态，看模板脚本：

[.agents/skills/tilelang-a5-sim-convert/scripts/run_a5_sim_template.py:105-129](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/.agents/skills/tilelang-a5-sim-convert/scripts/run_a5_sim_template.py#L105-L129)：`load_runtime` 按全路径 `ctypes.CDLL(libruntime_camodel.so)` 加载仿真运行时，声明 `rtMalloc`/`rtMemcpy`/`rtStreamCreate` 等 RT API 的签名，并 `rtSetDevice(0)`。

[.agents/skills/tilelang-a5-sim-convert/scripts/run_a5_sim_template.py:235-244](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/.agents/skills/tilelang-a5-sim-convert/scripts/run_a5_sim_template.py#L235-L244)：典型的仿真调用序列——`rtMemcpy` 把 host 数据搬到仿真设备内存、`rtStreamCreate` 建流、`kl.call(d_A, d_B, d_C, stream)` 启动 kernel、`rtStreamSynchronize` 等待完成、`rtMemcpy` 取回结果。注意这里的 `kl.call` 与真实 NPU 上的 `lib.call` 是**同一个符号**，只是链接的运行时库不同。

```python
rt.rtMemcpy(d_A, M*K*2, h_A.ctypes.data, M*K*2, 1)   # H2D
rt.rtMemcpy(d_B, K*N*2, h_B.ctypes.data, K*N*2, 1)
stream = ctypes.c_void_p()
rt.rtStreamCreate(ctypes.byref(stream), 0)
...
kl.call(d_A, d_B, d_C, stream)                        # 启动(同一个 call 符号)
rt.rtStreamSynchronize(stream)
rt.rtMemcpy(h_C.ctypes.data, M*N*2, d_C, M*N*2, 2)    # D2H
```

官方文档对这套机制的概括（[docs/TileLang-Ascend Programming Guide.md:206-210](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/TileLang-Ascend%20Programming%20Guide.md#L206-L210)）：真实流程是 `DSL → JIT 编译 → torch.npu 分配 NPU 内存 → 真实 NPU 执行`；仿真流程是 `DSL → 链接 libruntime_camodel.so 编译 → rtMalloc 分配模拟内存 → CPU 模拟 NPU 执行`，核心区别就是**用 `libruntime_camodel.so` 替代 `libruntime.so`**。

#### 4.5.4 代码实践

**实践目标**：理解"换库即仿真"的机制，并知道仿真路径的触发开关。

**操作步骤**：

1. 读 [libgen.py:230-254](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/adapter/libgen.py#L230-L254)，确认 camodel 替换做了三件事：插 `-L`、插 `-rpath`、换 `-l`。
2. 读官方局限说明（[Programming Guide:268-273](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/TileLang-Ascend%20Programming%20Guide.md#L268-L273)），记下三个限制：**仅支持 PTO 后端**、慢约 1000 倍（不可用于性能测试）、不支持 `torch.npu`。
3. （待本地验证）在有 A5 simulator 镜像的 CANN 环境里，按文档运行：

   ```bash
   python .agents/skills/tilelang-a5-sim-convert/scripts/run_a5_sim_template.py
   ```

   观察 `KERNEL OUTPUT MATCH!` 输出，并按 `--log-dir` 查看 camodel 的指令 trace。

**需要观察的现象**：仿真模式下编译命令里出现 `-lruntime_camodel` 与 `-Wl,-rpath,...`；运行结果正确但远慢于真实 NPU。

**预期结果**：你能说清"为什么同一份 kernel 源码既能在真机跑、又能在仿真器跑"——因为它们链接了接口相同、实现不同的两个 runtime 库。

#### 4.5.5 小练习与答案

**练习 1**：为什么 camodel 仿真**只支持 PTO 后端**？

> **参考答案**：根据官方局限说明（[Programming Guide:270](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/TileLang-Ascend%20Programming%20Guide.md#L270)），camodel 的 A5 simulator 镜像（`Ascend950PR_9599`）面向 PTO IR 指令集；AscendC 路线的设备代码方言与该仿真器不匹配，故仅 `target="pto"` 可仿真。

**练习 2**：`--disable-new-dtags` 这个链接选项在 camodel 场景里为什么必不可少？

> **参考答案**：它让 `-rpath` 写成可传递的 `DT_RPATH` 而非不可传递的 `DT_RUNPATH`。`libruntime_camodel.so` 自身还依赖 `libnpu_drv_camodel.so` 等其它仿真库，只有 `DT_RPATH` 才能让这些间接依赖也沿 `sim_lib_path` 解析到，否则加载 `.so` 时会因找不到间接依赖而失败（见 [libgen.py:236-238](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/adapter/libgen.py#L236-L238) 注释）。

---

## 5. 综合实践

**综合任务：跟踪一次完整的 `func(a, b)` 调用，画出"文件级"调用栈。**

把本讲学的全部串起来。请按下面的步骤，在源码里走完一次 GEMM 调用的全程，并在每一步记录**涉及的文件、函数、行号**：

1. **调用入口**：从 [kernel.py:184](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/kernel.py#L184) `JITKernel.__call__` 出发，记下 `_generate_extra_args` 做了什么。
2. **Cython 桥**：进入 [cython_wrapper.pyx:75](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/adapter/cython/cython_wrapper.pyx#L75) `forward`，记录它如何分配输出张量、取 stream、打包指针。
3. **最后一跳**：定位 [cython_wrapper.pyx:197](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/adapter/cython/cython_wrapper.pyx#L197) 的 `self.lib.call(...)`，并说明 `lib` 这个 `.so` 是由 [adapter.py:270](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/adapter/cython/adapter.py#L270) 的 `compile_lib` 经 bisheng 编出来的。
4. **host 启动器**：跳到 [codegen_ascend.cc:1131](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/codegen_ascend.cc#L1131) `PrintHostFunc`，记录 `call` 如何取 `fftsAddr` 并用 `<<<core, nullptr, stream>>>` 启动 `_kernel`。
5. **编译支线**：补一条"首次调用前"的支线——[kernel.py:228](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/kernel.py#L228) `tilelang.lower` 产出源码 → [libgen.py:142](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/adapter/libgen.py#L142) `compile_lib` 调 bisheng 编 `.so` → [libgen.py:130](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/adapter/libgen.py#L130) `load_lib` 用 `ctypes.CDLL` 加载。

**产出物**：一张包含"文件:行号 → 函数 → 作用"三列的表格，覆盖 Python 层、Cython 层、bisheng 编译层、C++ codegen 层各至少一行。

**预期结果**：你能在不看讲义的情况下，向别人讲清"我写下 `c = func(a, b)` 之后，到底发生了什么"——从 Python 一路讲到 AI Core 上的 `_kernel`，并指出 bisheng 在哪一步登场、`call` 符号是谁生成的。

> 如果手头有 CANN 环境，可额外用 `print(func.get_kernel_source())` 在生成的源码里验证第 4 步的 `call` 与 `<<<core>>>`，并尝试设 `TL_RUN_MODE=sim` 走一遍 camodel 路径，对比 `compile_lib` 拼出的命令差异（待本地验证）。

## 6. 本讲小结

- tile-lang 在 NPU 上**不走 TVM 设备 runtime**：codegen 把"设备函数 `_kernel` + host 启动器 `call`"一起写进同一份源码，编进同一个 `.so`，Python 侧只需 `ctypes` 加载并调 `call`。
- **`LibraryGenerator.compile_lib`** 是把 Ascend C 源码编成 `.so` 的角色：ascendc 走 `bisheng -xasc`、pto 走 `bisheng -xcce`，两条命令的头文件/模板库/宏/链接库各成一套，**方言必须与 codegen 路线配对**。
- 编译标志采用"**框架派生默认 + 用户追加在最后、last-wins 覆盖**"的策略，故用户传的 `compile_flags`（如 `-O0`、`--cce-auto-sync=on`）总能生效。
- **`PrintHostFunc`**（C++ codegen）在源码里生成 `extern "C" void call(...)`：张量形参为 `uint8_t*`、末尾带 `aclrtStream stream`，体内用 `rtGetC2cCtrlAddr` 取 `fftsAddr`，再用 bisheng 的 `<<<core, nullptr, stream>>>` 启动 `_kernel`；`core` 来自 `blockIdx.x` 的 extent。
- **`CythonKernelAdapter`** 是默认后端，运行期 `forward` 负责：分配输出/临时张量、解析动态符号、把 `data_ptr` 转 `c_void_p`、取 `torch.npu.current_stream()`、调 `lib.call`。注意 kernel `.so`（bisheng 编）与 `cython_wrapper.so`（g++ 编）是两条独立工具链。
- **camodel 仿真**的机制就是"换库"：`TL_RUN_MODE=sim` 时把 `-lruntime` 替换成 `-lruntime_camodel` 并把仿真库目录排在链接/加载路径最前；同一份 `.so`、同一个 `call` 符号即可在 CPU 上模拟 NPU（仅限 PTO + A5，慢约 1000 倍）。

## 7. 下一步学习建议

- **走向实战调试**：本讲给出了 `get_kernel_source()` 这个窗口，下一步建议读 [u7-l4 调试与性能分析](u7-l4-debug-profiling.md)，学习用 `T.printf`/`T.dump_tensor`、`TL_PTO_DEBUG`、`msprof` 在设备侧和性能侧观察 kernel。
- **亲手跑仿真**：若你没有真机，直接按本讲 4.5 与 [u7-l5 A5 仿真运行（camodel）](u7-l5-camodel-sim.md) 在 CPU 上跑通一个 kernel，结合 camodel 的指令 trace 加深对"`.so` 如何在核上跑"的直觉。
- **集成与导出**：理解了 `call` 符号后，[u7-l3 PyTorch 集成与 ACLGraph 入图](u7-l3-torch-aclgraph.md) 会展示如何把这套 JIT 产物封装成 torch 模块、并经 NPUGraph capture/replay 进一步降低 host 交互开销。
- **回看 codegen 全貌**：若想进一步理解 `call` 与 `_kernel` 是怎么从 TIR 一步步打印出来的，可重读 [u6-l2 双 Codegen](u6-l2-dual-codegen.md) 的 `AddFunction`/`VisitExpr_` 部分，把"源码是怎么长出来的"和"源码是怎么被编/被调的"在脑中拼成闭环。
