# Driver、Launcher 与内核启动

## 1. 本讲目标

前几讲我们追到「编译完成、得到一个 `.cubin`」。但 cubin 不会自己跑起来——还需要有人把它加载进 GPU、准备好参数、以正确的网格形状「拉起来」。这件事在 TileIR 后端里由 **`TileIRDriver`（驱动）** 和 **`TileIRLauncher`（启动器）** 两个角色完成。

学完本讲你应该能够：

- 说出 `TileIRDriver` 继承自哪个基类、它在 `__init__` 里做了哪三件事，以及它如何复用上游 NVIDIA 的 C 工具模块（`TileIRUtils`）。
- 解释 `make_launcher` 是如何**针对每个 kernel 的签名，现场生成一段 C 源码**、再即时编译成 `.so` 的，并说清楚那段胶水代码的固定结构（9/10 个元数据参数 + 一组 kernel 参数）。
- 讲清楚 TileIR 最本质的运行期差异：**以 tile 为单位启动内核**——`grid` 维度是「tile 数」、`block` 维度恒为 \(1 \times 1 \times 1\)、`sharedMemBytes` 恒为 0，这与 PTX 后端以「线程块（warps）」为单位启动完全不同。
- 说明 `launch_pdl`（PDL，Programmatic Dependent Launch）与 cluster scheduling（SPREAD）这两个启动属性是如何被设置进 `CUlaunchConfig` 的，以及 `launch_pdl` 的值从哪条链路传进来。

本讲只读源码、不修改源码。涉及真实 GPU 启动的现象需要 Blackwell GPU + CUDA 13.1 工具链，相关运行结果标注为「待本地验证」。本讲是 u2-l1「后端选择」与 u1-l4「端到端编译链路总览」的直接续篇——它们回答了「选哪个后端、编译出什么」，本讲回答「**最后一步：怎么把它真正跑起来**」。

## 2. 前置知识

在进入源码前，先用通俗语言建立六个概念。

**Driver 与 Launcher 各管什么？** 在 Triton 的后端抽象里，**driver（驱动）** 负责「与 GPU 打交道」：探测设备、查询能力、加载编译产物；**launcher（启动器）** 负责「把这一次 kernel 调用的参数组装好并发射」。driver 在进程里通常是单例，launcher 则是「每个 kernel 签名一个实例」。

**`GPUDriver` 基类是什么？** 上游 `python/triton/backends/driver.py` 定义了抽象基类 `DriverBase`，以及一个面向 CUDA 类硬件的中间类 `GPUDriver`。`GPUDriver.__init__` 会从 `torch.cuda` 上绑定一组设备函数：`get_device_capability`、`get_current_device`、`set_current_device`、`get_current_stream`。NVIDIA 后端的 `CudaDriver` 和本仓库的 `TileIRDriver` 都继承 `GPUDriver`，复用这组 torch 设备函数。

**`cuLaunchKernelEx` 是什么？** 它是 CUDA Driver API 里「带扩展属性」的内核启动函数。老式 `cuLaunchKernel` 只能指定 grid/block 维度；`cuLaunchKernelEx` 额外接受一个 `CUlaunchConfig`，里面可以挂一组 `CUlaunchAttribute`，从而表达 **PDL（可编程依赖启动）**、**cluster（线程块集群）**、**cooperative grid（协作网格）** 等新硬件特性。本仓库两个后端的 launcher 都用它。

**什么是 PDL（launch_pdl）？** PDL 全称 Programmatic Dependent Launch，NVIDIA 引入的一种「让相邻 kernel 在同一 stream 上重叠」的机制：启用了 PDL 的 kernel 可以在**上一个 kernel 还没完全结束时**就开始被调度/预热，从而隐藏 kernel 之间的启动间隙。在驱动 API 层它对应启动属性 `CU_LAUNCH_ATTRIBUTE_PROGRAMMATIC_STREAM_SERIALIZATION`。是否启用由编译选项 `launch_pdl` 决定。

**什么是「以 tile 为单位启动」？** 在 PTX 后端里，启动的基本单位是**线程块（thread block）**：用户给的 `grid` 是线程块数，每个块的线程数由 `num_warps * 32` 决定。而在 CUDA Tile IR 编程模型里，基本单位是 **tile（瓦片）**——编译器 `tileiras` 已经把「一个 tile 内部跑多少 warp、用多少共享内存」烘焙进了 cubin。于是 host 端启动时，**`grid` 维度表示要跑多少个 tile，`block` 维度恒为 1**，共享内存也不由 host 显式指定（写 0）。这是 TileIR 与 PTX 在运行期最直观的差异。

**`compile_module_from_src` 是什么？** 它是上游 `python/triton/runtime/build.py` 提供的「把一段 C/C++ 源码即时编译成 Python 扩展模块（`.so`）并加载」的工具。driver.c、以及为每个签名生成的 launcher，都是靠它「按需编译、即编即用」的。

> 术语速查：
> - **tile**：CUDA Tile IR 的并行单位；host 启动时一个 grid 维度 = 一个 tile。
> - **PDL**：Programmatic Dependent Launch，相邻 kernel 重叠执行的启动属性。
> - **cluster / CGA**：线程块集群（Cooperative Grid Array），多个 thread block 组成的调度单元。
> - **launcher 胶水代码**：为特定签名生成的 C 代码，把 Python 传进来的参数翻译成驱动 API 要的 `void**`。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `third_party/tileir/backend/driver.py` | 本讲主战场：`TileIRUtils`、`make_launcher`、`TileIRLauncher`、`TileIRDriver`、`GlobalTileIRDriver` 全在这里。 |
| `third_party/tileir/backend/driver.c` | TileIR 专用的 C 工具模块源码，核心是 `load_tileir_binary`（把 cubin 装载进 CUDA driver）。 |
| `third_party/nvidia/backend/driver.c` | 上游 NVIDIA 后端的 C 工具模块；TileIR **复用** 它的设备属性函数，也用于对照 `_launch` 的线程块式启动。 |
| `python/triton/backends/driver.py` | 上游 `GPUDriver` 基类，`TileIRDriver` 继承它。 |
| `python/triton/runtime/build.py` | `compile_module_from_src`，即时编译 C 源码为 `.so` 的工具。 |
| `third_party/tileir/backend/compiler.py` | `TileIROptions.launch_pdl` 在此声明；它最终流入启动属性。 |

> 永久链接 base（本讲所有链接均基于此 HEAD）：
> `https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/`

## 4. 核心概念与源码讲解

按「先看 driver 如何就绪、再看 launcher 如何生成、最后看真正启动」的依赖顺序，本讲拆成三个最小模块：

- **4.1 `TileIRDriver`**：设备探测、C 工具模块的复用与单例。
- **4.2 `make_launcher` 胶水代码**：从签名现场生成 C 启动器。
- **4.3 `cuLaunchKernelEx` tile 启动**：以 tile 为单位的网格启动与两个启动属性。

### 4.1 TileIRDriver：设备探测、工具复用与单例

#### 4.1.1 概念说明

`TileIRDriver` 是 TileIR 后端的 driver 类。它继承上游 `GPUDriver`，因此天然拥有「用 `torch.cuda` 探测设备、查询计算能力、拿当前 stream」这一整套能力（这部分 u2-l1 已用过的 `get_current_target()` 就是建在此之上）。但它额外做了一件关键的事：**装载 tile cubin 的 C 函数与上游不同**——上游 NVIDIA 后端用 `load_cuda_binary`，TileIR 用自己的 `load_tileir_binary`（见 `driver.c`）。

装载 cubin 这类「与编译产物强相关」的能力，driver 不是直接调 CUDA API，而是通过一个**C 工具模块**（`TileIRUtils`）暴露出来的。有意思的是，`TileIRUtils` 并非全部从零写：它**即时编译两份 C 源码**——一份是 TileIR 自己的 `driver.c`（提供 `load_tileir_binary`），另一份**直接复用** NVIDIA 后端的 `driver.c`（提供设备属性、占用率、printf fifo 等函数）。这就把「TileIR 特有的装载逻辑」和「与 NVIDIA 共享的设备查询逻辑」干净地分开了。

#### 4.1.2 核心流程

```
ENABLE_TILE=1  →  _create_driver() 返回 TileIRDriver()  (承接 u2-l1)
                       │
TileIRDriver.__init__:
  ├── self.utils = TileIRUtils()        # C 工具模块（单例）
  ├── self.launcher_cls = TileIRLauncher # 后续每个签名用它造 launcher
  └── super().__init__()  → GPUDriver.__init__  # 绑定 torch.cuda 设备函数

TileIRUtils.__init__（首次）:
  ├── 即时编译 third_party/tileir/backend/driver.c  → tileir_utils 模块
  │     └── self.load_binary = mod.load_tileir_binary
  └── 即时编译 third_party/nvidia/backend/driver.c   → cuda_utils 模块
        └── self.get_device_properties / get_device_capability
            / cuOccupancyMaxActiveClusters / set_printf_fifo_size / unload_module
```

#### 4.1.3 源码精读

先看 `TileIRDriver` 本身。它的 `__init__` 只做三件事：建工具模块、记下 launcher 类、调父类构造：

[third_party/tileir/backend/driver.py:548-552](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/driver.py#L548-L552) —— 注意顺序：先 `self.utils = TileIRUtils()`，再 `super().__init__()`。

```python
class TileIRDriver(GPUDriver):
    def __init__(self):
        self.utils = TileIRUtils()  # TODO: make static
        self.launcher_cls = TileIRLauncher
        super().__init__()
```

`super().__init__()` 走的是上游 `GPUDriver.__init__`，把 torch 的设备函数绑到实例上：

[python/triton/backends/driver.py:159-171](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/triton/backends/driver.py#L159-L171)：

```python
class GPUDriver(DriverBase):
    def __init__(self):
        import torch
        self.get_device_capability = torch.cuda.get_device_capability
        ...
        self.get_current_device = torch.cuda.current_device
        self.set_current_device = torch.cuda.set_device
```

> 要点：`TileIRDriver` 的设备探测能力**来自 torch（经 `GPUDriver`）**，而非来自 `TileIRUtils`。`TileIRUtils` 里那个 `get_device_capability`（取自 NVIDIA 的 `driver.c`）是「备用/扩展」工具，并不在 `get_current_target` 的路径上。`get_current_target` 用的是继承来的 torch 版本：

[third_party/tileir/backend/driver.py:554-559](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/driver.py#L554-L559) —— 与 u2-l1 一致，写死返回 `backend="tileir"`：

```python
def get_current_target(self):
    device = self.get_current_device()
    capability = self.get_device_capability(device)
    capability = capability[0] * 10 + capability[1]
    warp_size = 32
    return GPUTarget("tileir", capability, warp_size)
```

[third_party/tileir/backend/driver.py:571-582](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/driver.py#L571-L582) —— `is_active()`：自动探测路径会调它，要求 CUDA 可用且 `ENABLE_TILE=="1"` 且非 HIP：

```python
@staticmethod
def is_active():
    try:
        import torch
        return (torch.cuda.is_available()
                and os.environ.get("ENABLE_TILE", "0") == "1"
                and (torch.version.hip is None))
    except ImportError:
        return False
```

再看 `TileIRUtils`——这是「复用上游 C 模块」的核心。它用 `__new__` 做成单例，并在 `__init__` 里即时编译两份 C 源码：

[third_party/tileir/backend/driver.py:33-57](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/driver.py#L33-L57)：

```python
class TileIRUtils(object):
    def __init__(self):
        tile_mod_path = dirname
        nvidia_mod_path = os.path.join(os.path.dirname(dirname), "nvidia")
        tile_mod = compile_module_from_src(
            Path(os.path.join(tile_mod_path, "driver.c")).read_text(),
            "tileir_utils", library_dirs(), include_dirs, libraries)
        nvidia_mod = compile_module_from_src(
            Path(os.path.join(nvidia_mod_path, "driver.c")).read_text(),
            "cuda_utils", library_dirs(), include_dirs, libraries)
        self.init_nvidia_function(nvidia_mod)
        self.init_tileir_function(tile_mod)
```

两个分支一目了然：
- `tile_mod` 编译的是**本目录**的 `third_party/tileir/backend/driver.c`，模块名 `tileir_utils`。
- `nvidia_mod` 编译的是**隔壁** `third_party/nvidia/backend/driver.c`，模块名 `cuda_utils`——**直接复用上游 NVIDIA 的 C 源码**。

[third_party/tileir/backend/driver.py:59-67](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/driver.py#L59-L67) —— 把两份模块的函数挂到实例上：

```python
def init_tileir_function(self, mod):
    self.load_binary = mod.load_tileir_binary      # TileIR 特有：装载 tile cubin

def init_nvidia_function(self, mod):               # 复用 NVIDIA 的设备函数
    self.get_device_properties = mod.get_device_properties
    self.get_device_capability = mod.get_device_capability
    self.cuOccupancyMaxActiveClusters = mod.cuOccupancyMaxActiveClusters
    self.set_printf_fifo_size = mod.set_printf_fifo_size
    self.unload_module = mod.unload_module
```

唯一「TileIR 专属」的是 `load_binary`（来自 `load_tileir_binary`）。它做的事是「拿到 cubin 字节流，调 `cuModuleLoadData` 把它加载成 `CUmodule`，再 `cuModuleGetFunction` 取出 `CUfunction`，顺便回读寄存器/溢出/共享内存等属性」：

[third_party/tileir/backend/driver.c:42-92](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/driver.c#L42-L92) —— 关键几行：

```c
CUDA_CHECK_AND_RETURN_NULL_ALLOW_THREADS(cuModuleLoadData(&mod, data));      // 装载 cubin
CUDA_CHECK_AND_RETURN_NULL_ALLOW_THREADS(cuModuleGetFunction(&fun, mod, name)); // 取函数句柄
// 回读属性：寄存器数、溢出寄存器、静态共享内存、每块最大线程数
CUDA_CHECK_AND_RETURN_NULL_ALLOW_THREADS(cuFuncGetAttribute(&n_regs, CU_FUNC_ATTRIBUTE_NUM_REGS, fun));
...
return Py_BuildValue("(KKiiii)", (uint64_t)mod, (uint64_t)fun, n_regs,
                     n_spills, static_smem_bytes, n_max_threads);
```

它返回的 `(mod, fun, n_regs, n_spills, static_smem_bytes, n_max_threads)` 就是后续启动需要的 `CUfunction` 句柄（`fun`）以及资源信息。注意：`static_smem_bytes` 在这里被**回读**出来——这正是 4.3 节里 host 启动时 `sharedMemBytes` 可以写 0 的原因：共享内存需求已经烘焙在 cubin 里、由驱动读取，不必 host 显式传入。

编译这两份 C 源码用的是上游的 `compile_module_from_src`：

[python/triton/runtime/build.py:199-202](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/triton/runtime/build.py#L199-L202)：

```python
def compile_module_from_src(src, name, library_dirs=None, include_dirs=None,
                            libraries=None, ccflags=None, language="c") -> ModuleType:
    return _compile_so_from_src(src, name, library_dirs, include_dirs, libraries,
                                ccflags, language, load_module=True)
```

它把 C 字符串写成临时文件、用系统 C 编译器（`-O3 -shared -fPIC`）编成 `.so`、再 `importlib` 加载为 Python 模块并返回。`library_dirs()`、`include_dirs`、`libraries`（其中 `libraries = ['libcuda.so.1']`）这三个都从 NVIDIA 后端的 `driver.py` 导入（见 [third_party/tileir/backend/driver.py:12-17](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/driver.py#L12-L17)），保证 `.so` 能链接到 `libcuda`。

最后，模块级还有一个单例，专门给 `torch.compile` 那条路径复用（u2-l1 末尾讲过 `driver.set_active(GlobalTileIRDriver)`）：

[third_party/tileir/backend/driver.py:605](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/driver.py#L605) —— `GlobalTileIRDriver = TileIRDriver()`。

> 一句话串起来：**`TileIRDriver` 继承 `GPUDriver` 拿到 torch 设备探测；它独有的只是「装载 tile cubin」这一项，其余 C 函数直接复用 NVIDIA 的 `driver.c`。** 编译产物的加载这一步，到此就绪。

#### 4.1.4 代码实践

**实践目标**：确认 `TileIRDriver` 复用了上游 NVIDIA 的 C 工具模块，并看清它把哪些函数挂到 `utils` 上。

**操作步骤**：

1. 打开 [third_party/tileir/backend/driver.py:49-55](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/driver.py#L49-L55)，对照 `nvidia_mod_path` 推导，确认 `nvidia_mod` 编译的是 `third_party/nvidia/backend/driver.c`。
2. 在该后端已安装的环境里（示例代码）内省 driver 单例：

   ```python
   # 示例代码
   import os
   os.environ["ENABLE_TILE"] = "1"
   import triton
   from triton.runtime import driver
   d = driver.active
   print("driver 类型:", type(d).__name__)
   print("load_binary 来源:", d.utils.load_binary.__module__)      # 期望含 tileir_utils
   print("get_device_properties 来源:", d.utils.get_device_properties.__module__)  # 期望含 cuda_utils
   ```

**需要观察的现象**：`load_binary.__module__` 指向 TileIR 自己编译出的 `tileir_utils`；`get_device_properties.__module__` 指向复用 NVIDIA 源码编译出的 `cuda_utils`。

**预期结果**：证实「TileIR 仅 `load_binary` 是自研，其余设备函数复用 NVIDIA」。运行结果「待本地验证」（依赖 Blackwell + CTK 13.1）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `TileIRUtils` 要同时编译两份 `driver.c`，而不是只编译 TileIR 自己的那份？

> **答案**：因为 TileIR 与 NVIDIA 共享同一类硬件（CUDA、Blackwell），设备属性查询（`get_device_properties`/`get_device_capability`）、占用率（`cuOccupancyMaxActiveClusters`）等函数与后端无关，没必要重写；只有「装载 cubin」这一步因为 cubin 格式/语义不同（tile cubin vs 普通 PTX cubin）才需要 TileIR 专属的 `load_tileir_binary`（`driver.py:49-60`）。这是典型的「差异点隔离、共性点复用」。

**练习 2**：`get_current_target()` 里调用的 `self.get_device_capability`，走的是 `GPUDriver` 绑的 torch 函数，还是 `TileIRUtils` 里那个？

> **答案**：走的是 `GPUDriver.__init__` 绑的 `torch.cuda.get_device_capability`（`python/triton/backends/driver.py:164`）。`TileIRUtils.get_device_capability`（取自 NVIDIA `driver.c`）是另一份备用实现，不在 `get_current_target` 的路径上。

---

### 4.2 make_launcher：从 signature 到 C 胶水代码

#### 4.2.1 概念说明

「启动」一个 kernel，本质是调一次 `cuLaunchKernelEx(config, function, params, extra)`。难点在于 `params`：它是一个 `void**` 数组，每个元素指向一个**按 C 类型布局的参数值**。而 Python 这边传进来的是一堆 `PyObject*`（张量、整数、浮点数……），类型和内存布局都不同。怎么把「Python 对象列表」翻译成「C 指针数组」？

TileIR 的解法和上游 NVIDIA 后端一样：**针对当前 kernel 的签名，现场生成一段 C 代码**（称为 launcher / 胶水代码），让这段 C 代码去 `PyArg_ParseTuple` 解包参数、把浮点数打包成底层位表示、把张量对象转成设备指针、最后按正确顺序组成 `void* params[]` 传给 `_launch`。因为每个 kernel 的参数个数和类型不同，所以**这段 C 代码必须按签名定制**——这就是 `make_launcher(constants, signature)` 的职责：它读签名，**拼字符串**生成 C 源码，再即时编译。

关键设计点：胶水代码的「骨架」是固定的（错误处理、`getLaunchKernelExHandle`、`_launch`、`getPointer`、浮点打包、`launch` 入口、模块注册），只有「参数声明、参数解包格式串、`params[]` 内容」是随签名变化的部分。`make_launcher` 就是把这些可变片段拼进固定模板。

#### 4.2.2 核心流程

```
TileIRLauncher.__init__(src, metadata):
  ├── 处理 signature / constants（含 tensordesc 展开，留给 u2-l6 详讲）
  ├── src = make_launcher(constants, signature)   # ① 拼 C 源码字符串
  ├── mod = compile_module_from_src(src, "__triton_launcher", ...)  # ② 即时编译成 .so
  └── self.launch = mod.launch                     # ③ 拿到 Python 可调用的 launch

make_launcher 内部（拼字符串）:
  ├── format_of(ty): 把每个签名类型映射成 PyArg_ParseTuple 的格式字符
  ├── args_format = "".join(...) ; format = _BASE_ARGS_FORMAT + args_format
  ├── arg_decls / internal_args_list / params / float_storage_decls: 按类型生成 C 片段
  └── 组装成 src（含 _launch / getPointer / pack_* / launch 入口 / PyInit___triton_launcher）
```

#### 4.2.3 源码精读

先看那条贯穿全流程的「固定元数据格式」。这是理解后面 `launch_pdl` 注入的关键：

[third_party/tileir/backend/driver.py:93-94](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/driver.py#L93-L94)：

```python
_BASE_ARGS_FORMAT = "iiiKKpOOOO"
_BASE_ARGS_FORMAT_LEN = len(_BASE_ARGS_FORMAT)
```

这个 10 字符的格式串是**所有 TileIR launcher 共享的前缀**，对应 `PyArg_ParseTuple` 解包的前 10 个固定槽位（见下面 `launch` 入口）。逐位拆开：

| 位 | 字符 | 含义 |
| --- | --- | --- |
| 1–3 | `iii` | `numTilesX`、`numTilesY`、`numTilesZ`（三个 int，即 tile 网格） |
| 4–5 | `KK` | `_stream`、`_function`（两个 `uint64`，即 `CUstream` 与 `CUfunction` 句柄） |
| 6 | `p` | `launch_pdl`（bool，`p` 把对象按真值存成 int） |
| 7–10 | `OOOO` | `kernel_metadata`、`launch_metadata`、`launch_enter_hook`、`launch_exit_hook`（四个 `PyObject*`） |

> 注意第 6 位的 `launch_pdl`：运行期「调用方」其实只传 9 个元数据（不含 `launch_pdl`），`launch_pdl` 是**编译期决定**的值，由 `TileIRLauncher.__call__` 自己在第 5 位后面插进去（见 4.3.3）。这就是为什么代码里反复出现「9 is the number of metadata arguments」而 `_BASE_ARGS_FORMAT` 却有 10 个字符——多出的那一位正是被注入的 `launch_pdl`。

接着是随签名变化的「类型映射」。`format_of` 决定每个 kernel 参数在格式串里用什么字符，`_extracted_type` 决定它在 C 里声明成什么类型：

[third_party/tileir/backend/driver.py:106-135](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/driver.py#L106-L135) —— 关键分支：指针（`ty[0]=="*"`）和 `constexpr`/`nvTmaDesc` 都映射成 `PyObject*`（格式 `O`），其余按 `ty_to_cpp` 查表。

```python
def format_of(ty):
    if isinstance(ty, tuple): ...
    if ty[0] == "*":            return "O"      # 指针：交给 getPointer 在 C 里转
    if ty in ("constexpr", "nvTmaDesc"): return "O"
    return { "double": "d", "int32_t": "i", "int64_t": "L",
             "uint32_t": "I", "uint64_t": "K", ... }[ty_to_cpp(ty)]
```

`make_launcher` 再把这些片段拼成三组 C 代码：

[third_party/tileir/backend/driver.py:137-182](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/driver.py#L137-L182) —— 三组可变片段：

```python
format = _BASE_ARGS_FORMAT + args_format          # 完整格式串 = 10 固定位 + 签名参数位

# ① 参数声明（跳过 constexpr；浮点用底层存储类型）
arg_decl_list = []
for i, ty in signature.items():
    if ty == "constexpr": continue
    if ty in FLOAT_STORAGE_TYPE:
        arg_decl_list.append(f"{FLOAT_STORAGE_TYPE[ty]} arg{i}")   # 如 uint16_t arg3
    else:
        arg_decl_list.append(f"{ty_to_cpp(ty)} arg{i}")

# ② 传给 _launch 的实参（指针→dev_ptr；浮点→打包后的存储；nvTmaDesc→解引用）
internal_args_list = []
for i, ty in signature.items():
    if ty[0] == "*":              internal_args_list.append(f"ptr_info{i}.dev_ptr")
    elif ty in FLOAT_STORAGE_TYPE:internal_args_list.append(f"_arg{i}_storage")
    elif ty == "nvTmaDesc":       internal_args_list.append(f"*tma_ptr{i}")
    elif ty != "constexpr":       internal_args_list.append(f"_arg{i}")

# ③ void* params[] 的内容（传给 cuLaunchKernelEx 的 kernel 参数指针）
params = [f"&arg{i}" for i, ty in signature.items() if ty != "constexpr"]
```

这里有两个值得注意的处理：

- **浮点打包**：Triton 的 `fp16`/`bf16`/`fp32` 等在 Python 侧统一按 `double` 收（`ty_to_cpp` 把它们映射成 `double`，格式 `d`），进入 C 后再用 `pack_fp16`/`pack_bf16`/`pack_fp32` 重新打包成底层位表示（`uint16_t`/`uint32_t`），见 [third_party/tileir/backend/driver.py:306-330](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/driver.py#L306-L330) 和模板里的 `float_storage_decls`（[177-181](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/driver.py#L177-L181)）。这样既能用 `PyArg_ParseTuple` 的 `d` 收 double，又能把精确位模式交给 kernel。
- **指针转换**：张量参数在 Python 侧是 `PyObject*`（格式 `O`），C 里由 `getPointer` 把它解析成 `CUdeviceptr`，见 [third_party/tileir/backend/driver.py:264-304](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/driver.py#L264-L304)——它会取对象的 `data_ptr()`、再经 `cuPointerGetAttribute` 拿到设备指针，CPU 张量会在这一步报错。

最后看 `launch` 入口——它把上述片段粘合起来，是真正被 Python 调用的函数：

[third_party/tileir/backend/driver.py:332-390](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/driver.py#L332-L390) —— 骨架（省略签名可变部分）：

```c
static PyObject* launch(PyObject* self, PyObject* args) {
  int numTilesX, numTilesY, numTilesZ;
  uint64_t _stream, _function;
  int launch_pdl;
  PyObject *launch_enter_hook = NULL, *launch_exit_hook = NULL;
  PyObject *kernel_metadata = NULL, *launch_metadata = NULL;
  /* ...每个签名参数的声明... */
  if(!PyArg_ParseTuple(args, "{format}",
        &numTilesX, &numTilesY, &numTilesZ,
        &_stream, &_function, &launch_pdl,
        &kernel_metadata, &launch_metadata,
        &launch_enter_hook, &launch_exit_hook /*, ...签名参数... */)) { return NULL; }

  if (launch_enter_hook != Py_None) { /* 调用进入钩子 */ }

  /* 确保 CUDA context 存在 */
  CUcontext ctx = NULL; cuCtxGetCurrent(&ctx);
  if (!ctx) { /* retain primary ctx */ }

  /* 指针参数提前校验，CPU 张量在此抛错 */
  /* 浮点参数打包 */
  Py_BEGIN_ALLOW_THREADS;
    _launch(numTilesX, numTilesY, numTilesZ, launch_pdl,
            (CUstream)_stream, (CUfunction)_function /*, ...内部实参... */);
  Py_END_ALLOW_THREADS;

  if (launch_exit_hook != Py_None) { /* 调用退出钩子 */ }
  Py_INCREF(Py_None); return Py_None;
}
```

几个要点：
1. **`Py_BEGIN_ALLOW_THREADS` / `Py_END_ALLOW_THREADS`** 把 `_launch`（含 `cuLaunchKernelEx`）包起来，启动期间**释放 GIL**，让 Python 其他线程能并行跑。
2. **hook 机制**：`launch_enter_hook`/`launch_exit_hook` 给 profiling（如 proton）留了插桩点。
3. **context 兜底**：注释 `// todo: triton doesn't need this fix` 表明 TileIR 这里额外做了 `cuCtxGetCurrent` 兜底（[359-366](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/driver.py#L359-L366)）。

生成的源码末尾注册成名为 `__triton_launcher` 的 Python 扩展模块（[third_party/tileir/backend/driver.py:392-412](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/driver.py#L392-L412)）。`TileIRLauncher` 再用 `compile_module_from_src` 把它编出来、取 `mod.launch`：

[third_party/tileir/backend/driver.py:519-526](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/driver.py#L519-L526)：

```python
src = make_launcher(self.constants, self.signature)
mod = compile_module_from_src(src, "__triton_launcher", library_dirs(), include_dirs, libraries)
if has_tensordesc:
    self.launch = wrap_handle_tensordesc(mod.launch)   # 带描述符拆解的包装
else:
    self.launch = mod.launch
self.launch_pdl = metadata.launch_pdl
```

> 一句话串起来：**`make_launcher` 读签名、拼一段定制 C 源码（固定骨架 + 可变参数片段），即时编译成 `.so`，把它的 `launch` 函数挂到 `TileIRLauncher.launch`。** 这段胶水代码负责「Python 对象 → C 参数数组」的全部翻译。

#### 4.2.4 代码实践

**实践目标**：亲手「脑补」一个简单签名的 launcher 片段，验证 `format` 串与 `params[]` 的生成逻辑。

**操作步骤**：

1. 假设某个 kernel 的签名为 `signature = {0: "*fp32", 1: "i32", 2: "fp16"}`（一个 fp32 指针、一个 i32、一个 fp16）。打开 [third_party/tileir/backend/driver.py:106-182](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/driver.py#L106-L182)，按下表逐项填写（答案见文末）：

   | 项 | 推导过程 | 结果 |
   | --- | --- | --- |
   | `format_of("*fp32")` | 指针 → `O` | `O` |
   | `format_of("i32")` | `ty_to_cpp→int32_t → i` | ①______ |
   | `format_of("fp16")` | `ty_to_cpp→double → d` | `d` |
   | 完整 `format` | `_BASE_ARGS_FORMAT + "Oid"` | ②`iiiKKpOOOOOid` |
   | `params[]` 内容 | 非指针/非 constexpr 全部取地址；指针 arg0 也取地址 | ③`[&arg0, &arg1, &arg2]` |
   | 传给 `_launch` 的 arg0 实参 | 指针 → `ptr_info0.dev_ptr` | ④______ |

2. 在已安装环境（示例代码）打印一个真实 kernel 编译时 `make_launcher` 接收到的签名与最终格式串：

   ```python
   # 示例代码
   import os; os.environ["ENABLE_TILE"] = "1"
   import torch, triton, triton.language as tl
   @triton.jit
   def k(x_ptr, n):
       pid = tl.program_id(0)
       tl.store(x_ptr + pid, n)
   compiled = k.warmup(torch.zeros(4, device="cuda"), 4, grid=(4,))
   # 在 make_launcher 里临时加一行 print(signature, ...) 观察（见下方说明）
   ```

**需要观察的现象**：签名 `*fp32`→`O`、`i32`→`i`；`params[]` 对每个非 constexpr 参数取地址；指针参数在传给 `_launch` 时变成 `ptr_info{i}.dev_ptr`。

**预期结果**：能独立推导出任意简单签名的 `format` 与 `params[]`。填空答案：①`i` ④`ptr_info0.dev_ptr`。运行观察「待本地验证」。

> 想真正看到 `make_launcher` 的输入，可在 [driver.py:97](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/driver.py#L97) 的 `make_launcher` 第一行临时加 `print("signature=", signature)` 做调试（本讲「读源码」实践，不改逻辑）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `fp16`/`bf16` 这些半精度类型，在 Python→C 这一步先用 `double` 收，再在 C 里打包？

> **答案**：因为 `PyArg_ParseTuple` 没有原生的 `fp16`/`bf16` 格式字符，而 Python 数值最容易无损表达成 `double`。所以 `ty_to_cpp` 把这些半精度映射成 `double`（格式 `d`），进 C 后再用 `pack_fp16`/`pack_bf16`（[306-321](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/driver.py#L306-L321)）把它们重新压成 16 位位模式，保证 kernel 拿到的是精确的底层表示。

**练习 2**：`params[]` 为什么用「非 constexpr 参数取地址」、而把 `constexpr` 排除在外？

> **答案**：`constexpr` 是编译期已烘焙进 cubin 的常量，运行期不再作为 kernel 参数传递（[182](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/driver.py#L182) 的列表推导 `if ty != "constexpr"`）。因此它既不出现在 `params[]` 里，也不出现在 `arg_decls` 里。

---

### 4.3 cuLaunchKernelEx：以 tile 为单位的网格启动

#### 4.3.1 概念说明

胶水代码准备好 `void* params[]` 之后，最后一步就是真正发射。这一步在生成的 C 函数 `_launch` 里，它组装一个 `CUlaunchConfig` 并调用 `cuLaunchKernelEx`。

TileIR 与 PTX 在这里的差异是最本质的运行期差异，务必对照理解：

| 维度 | PTX 后端（NVIDIA `driver.c`） | TileIR 后端 |
| --- | --- | --- |
| `gridDim` | `gridX*num_ctas` × `gridY` × `gridZ`（**线程块数**） | `numTilesX` × `numTilesY` × `numTilesZ`（**tile 数**） |
| `blockDim` | `32 * num_warps`（每块线程数） | 恒为 \(1 \times 1 \times 1\) |
| `sharedMemBytes` | 由 host 显式传入 `shared_memory` | 恒为 `0`（共享内存烘焙在 cubin 里，由驱动读取） |
| cluster | 仅当 `num_ctas != 1` 才设集群维度 + SPREAD 调度 | **无条件**设 cluster scheduling = SPREAD |
| PDL | `launch_pdl != 0` 时加 PDL 属性 | 同（`launch_pdl != 0` 时加 PDL 属性） |

直觉上：PTX 后端把「并行度」拆成「线程块 × 块内线程」，host 要同时给出 `num_warps` 和共享内存大小；而 TileIR 后端把这一切都交给 `tileiras` 编译出的 kernel 自己管理，host 只负责声明「我要跑多少个 tile」，并以 `block=1`、`sharedMem=0` 的极简配置发射。这就是「以 tile 为单位启动」。

另外两个启动属性——**cluster scheduling（SPREAD）** 和 **PDL**——决定了 kernel 在硬件上「怎么被调度」。TileIR 无条件启用 SPREAD 调度偏好（因为 CUDA Tile IR 大量使用 thread block cluster / CGA），PDL 则受编译选项 `launch_pdl` 控制。

#### 4.3.2 核心流程

```
编译期: TileIROptions.launch_pdl  (compiler.py:97)
   → metadata["launch_pdl"]  (compiler.py:291-294, **options.__dict__)
   → TileIRLauncher(self, metadata).launch_pdl = metadata.launch_pdl  (driver.py:527)

运行期: TileIRLauncher.__call__(*args)
   ├── model_args = model_args[:5] + (self.launch_pdl,) + model_args[5:]  # 把 launch_pdl 插到第5位
   └── self.launch(*model_args)  → 调到 C 的 launch()
        └── _launch(numTilesX, numTilesY, numTilesZ, launch_pdl, stream, function, ...)
              ├── 填 CUlaunchConfig: grid=tiles, block=1×1×1, sharedMem=0
              ├── attrs[0] = CLUSTER_SCHEDULING_POLICY_SPREAD   (无条件)
              ├── 若 launch_pdl!=0: attrs[numAttrs++] = PROGRAMMATIC_STREAM_SERIALIZATION
              └── cuLaunchKernelExHandle(&config, function, params, 0)
```

#### 4.3.3 源码精读

先看 `launch_pdl` 是怎么一路传到 `_launch` 的。它在编译选项里声明、默认 `False`：

[third_party/tileir/backend/compiler.py:97](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/compiler.py#L97)：

```python
launch_pdl: bool = False
```

上游 `compile()` 把 `options.__dict__`（含 `launch_pdl`）展开进 `metadata`：

[python/triton/compiler/compiler.py:291-296](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/triton/compiler/compiler.py#L291-L296)：

```python
metadata = {
    "hash": hash,
    "target": target,
    **options.__dict__,   # launch_pdl 在此进入 metadata
    **env_vars,
}
```

`TileIRLauncher` 在构造时把它存下来：

[third_party/tileir/backend/driver.py:527](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/driver.py#L527) —— `self.launch_pdl = metadata.launch_pdl`。

真正调用时，`__call__` 把它**插到实参列表的第 5 位**（即 `_BASE_ARGS_FORMAT` 的那个 `p` 槽位）：

[third_party/tileir/backend/driver.py:529-545](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/driver.py#L529-L545)：

```python
def __call__(self, *args, **kwargs):
    # 9 is the number of metadata arguments in `src` defined in `make_launcher`
    num_launch_args = 9
    num_params = len(args) - num_launch_args
    if num_params < self.ori_signature_len:
        extra_args = [self.constants[(i,)] for i in range(num_params, self.ori_signature_len)]
        model_args = args + tuple(extra_args)
    else:
        model_args = args
    model_args = model_args[:5] + (self.launch_pdl,) + model_args[5:]   # 注入 launch_pdl
    self.launch(*model_args, **kwargs)
```

注意三个细节：
1. 调用方传的 `args` 是「9 个元数据 + kernel 参数」（不含 `launch_pdl`）。`model_args[:5]` 是 `[tilesX, tilesY, tilesZ, stream, function]`，把 `launch_pdl` 插到第 5 位后，正好对应 `launch()` 里 `PyArg_ParseTuple` 的前 6 个槽（`iiiKKp`）。
2. `num_params < self.ori_signature_len` 那条分支是给某个 torch inductor 版本的兼容补丁（注释见 [530-532](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/driver.py#L530-L532)）：当 inductor 没把 constexpr 参数传进来时，用 `self.constants` 补齐。

现在进入 C 的 `_launch`——本讲最核心的一段。它组装 `CUlaunchConfig` 并发射：

[third_party/tileir/backend/driver.py:229-257](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/driver.py#L229-L257)：

```c
static void _launch(int numTilesX, int numTilesY, int numTilesZ, int launch_pdl,
                    CUstream stream, CUfunction function /*, ...签名参数... */) {
  void *params[] = { /* &arg0, &arg1, ... 按签名生成 */ };
  if (numTilesX*numTilesY*numTilesZ > 0) {
    int numAttrs = 1;
    CUlaunchAttribute launchAttr[2];
    // ① 无条件：cluster scheduling 偏好 = SPREAD
    launchAttr[0].id = CU_LAUNCH_ATTRIBUTE_CLUSTER_SCHEDULING_POLICY_PREFERENCE;
    launchAttr[0].value.clusterSchedulingPolicyPreference = CU_CLUSTER_SCHEDULING_POLICY_SPREAD;
    // ② 条件：launch_pdl != 0 时加 PDL 属性
    if (launch_pdl != 0) {
        CUlaunchAttribute pdlAttr = { .id = CU_LAUNCH_ATTRIBUTE_PROGRAMMATIC_STREAM_SERIALIZATION, .value = 1};
        launchAttr[numAttrs++] = pdlAttr;
    }
    CUlaunchConfig config;
    config.gridDimX = numTilesX;   // grid 维度 = tile 数
    config.gridDimY = numTilesY;
    config.gridDimZ = numTilesZ;
    config.blockDimX = 1;          // block 维度恒为 1
    config.blockDimY = 1;
    config.blockDimZ = 1;
    config.sharedMemBytes = 0;     // 共享内存不由 host 指定（烘焙在 cubin）
    config.hStream = stream;
    config.attrs = launchAttr;
    config.numAttrs = numAttrs;
    static cuLaunchKernelEx_t cuLaunchKernelExHandle = NULL;
    if (cuLaunchKernelExHandle == NULL) { cuLaunchKernelExHandle = getLaunchKernelExHandle(); }
    CUDA_CHECK(cuLaunchKernelExHandle(&config, function, params, 0));
  }
}
```

逐点对照 4.3.1 的表：
- `config.gridDimX/Y/Z = numTilesX/Y/Z`：**grid 是 tile 数**，这正是用户给的 `grid=(M, N, ...)` 在 TileIR 下的语义。
- `config.blockDimX/Y/Z = 1`：**block 恒为 \(1 \times 1 \times 1\)**，tile 内部的 warp/thread 结构由 `tileiras` 编译的 kernel 自己决定。
- `config.sharedMemBytes = 0`：host 不指定共享内存（对比 PTX 后端显式传 `shared_memory`）；实际共享内存需求在 cubin 里，由 `load_tileir_binary` 的 `static_smem_bytes` 回读（见 4.1.3）。
- `launchAttr[0]` **无条件**设为 `CU_CLUSTER_SCHEDULING_POLICY_SPREAD`（`numAttrs` 初值就是 1）。对照 PTX 后端，它**仅当 `num_ctas != 1`** 才设集群维度和 SPREAD（见下方对比）。TileIR 这里无条件启用，是因为 CUDA Tile IR 默认就围绕 thread block cluster（CGA）组织调度。
- PDL 属性只在 `launch_pdl != 0` 时追加，与 PTX 后端逻辑一致。

`cuLaunchKernelEx` 的函数指针不是直接链接，而是运行期用 `dlopen`/`dlsym` 从 `libcuda.so.1` 取的——这保证了**老版本驱动（没有 `cuLaunchKernelEx`）不会在链接期就失败**：

[third_party/tileir/backend/driver.py:210-227](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/driver.py#L210-L227)：

```c
static cuLaunchKernelEx_t getLaunchKernelExHandle() {
  void* handle = dlopen("libcuda.so.1", RTLD_LAZY);
  ...
  cuLaunchKernelEx_t h = (cuLaunchKernelEx_t)dlsym(handle, "cuLaunchKernelEx");
  ...
  return h;
}
```

> 对比 PTX 后端（[third_party/nvidia/backend/driver.c:931-997](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/nvidia/backend/driver.c#L931-L997)）：它的 `_launch` 用 `config.gridDimX = gridX * num_ctas;`（[943](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/nvidia/backend/driver.c#L943)）、`config.blockDimX = 32 * num_warps;`（[947](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/nvidia/backend/driver.c#L947)）、`config.sharedMemBytes = shared_memory;`（[950](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/nvidia/backend/driver.c#L950)），且 cluster 属性仅在 `num_ctas != 1` 时才挂（[970-986](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/nvidia/backend/driver.c#L970-L986)）。把两段并排看，「线程块式启动」与「tile 式启动」的差异便一目了然。

> 一句话串起来：**`launch_pdl` 从编译选项经 `metadata` 流到 `TileIRLauncher`，在 `__call__` 被注入实参第 5 位；`_launch` 以 `grid=tile 数、block=1、sharedMem=0` 的 tile 式配置发射，无条件挂 SPREAD 调度、按需挂 PDL。**

#### 4.3.4 代码实践（本讲核心实践任务）

**实践目标**：阅读 `make_launcher` 与 `_launch`，说明 TileIR 启动时 grid/block 维度的含义，以及 `launch_pdl` 与 cluster scheduling 属性是如何被设置的。

**操作步骤**：

1. 打开 [third_party/tileir/backend/driver.py:229-257](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/driver.py#L229-L257)，对照下表填写 TileIR 启动配置（答案见文末）：

   | `CUlaunchConfig` 字段 | TileIR 取值 | 含义 |
   | --- | --- | --- |
   | `gridDimX/Y/Z` | ①______ | 要启动的 tile 数（来自用户的 grid） |
   | `blockDimX/Y/Z` | ②______ | 恒为 1，tile 内部线程结构由 cubin 决定 |
   | `sharedMemBytes` | ③______ | host 不指定，共享内存烘焙在 cubin |
   | `attrs[0]` | ④______ | 无条件启用，cluster 偏好 SPREAD |
   | `attrs[?]`（条件） | ⑤______ | 仅 `launch_pdl!=0` 时挂，PDL 流式串行化 |

2. 追踪 `launch_pdl` 的来源链，补全（答案见文末）：

   ```
   TileIROptions.launch_pdl (compiler.py:97)
     → metadata（经 compiler.py:294 的 ⑥______ 展开）
     → TileIRLauncher.⑦______ = metadata.launch_pdl (driver.py:527)
     → __call__ 中插到实参第 ⑧__ 位 (driver.py:543)
     → _launch 的 launch_pdl 形参 → if (⑨______) 挂 PDL 属性
   ```

3. （可选，需真实环境）对照 PTX 后端写两行 `@triton.jit` 内核，分别在 `ENABLE_TILE=1` 与不设时运行，用 `nsys`/`ncu` 抓 `cuLaunchKernelEx` 的 grid/block，观察 TileIR 下 `blockDim=1`、PTX 下 `blockDim=32*num_warps`。

**需要观察的现象**：grid 是 tile 数；block 恒为 1；cluster scheduling 无条件 SPREAD；PDL 仅在 `launch_pdl` 开启时挂。`launch_pdl` 的值来自编译选项而非运行期调用方。

**预期结果**：能用一句话说清「TileIR 以 tile 为单位启动（grid=tile 数，block=1，sharedMem=0），无条件 SPREAD、按需 PDL」，并画出 `launch_pdl` 从选项到 `_launch` 的完整链路。填空答案：①`numTilesX/Y/Z` ②`1` ③`0` ④`CLUSTER_SCHEDULING_POLICY_SPREAD` ⑤`PROGRAMMATIC_STREAM_SERIALIZATION` ⑥`**options.__dict__` ⑦`launch_pdl` ⑧`5` ⑨`launch_pdl != 0`。nsys/ncu 观察「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 TileIR 启动时 `blockDim` 恒为 1，而 PTX 后端却是 `32 * num_warps`？

> **答案**：两个后端的并行模型不同。PTX 后端以「线程块」为单位，每个块跑 `num_warps` 个 warp（`32*num_warps` 个线程），host 必须显式给出 `blockDim`。而 CUDA Tile IR 以「tile」为单位，tile 内部的 warp/线程结构和共享内存都由 `tileiras` 在编译期烘焙进 cubin，host 只需声明 tile 数（`gridDim`），`blockDim` 写 1 即可（[driver.py:240-247](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/driver.py#L240-L247)）。这也是 TileIR 旋钮里没有 `num_warps`、改用 `occupancy` 的运行期体现。

**练习 2**：`launch_pdl` 为什么要在 `TileIRLauncher.__call__` 里「插值」，而不是让调用方直接传进来？

> **答案**：因为 `launch_pdl` 是**每次编译的固定属性**（来自 `TileIROptions.launch_pdl`，经 `metadata` 传入），不是每次调用都变化的运行期参数。让调用方传反而容易出错；所以 `make_launcher` 在 C 端为它预留了固定槽位（`_BASE_ARGS_FORMAT` 的 `p`），由 `TileIRLauncher` 在 `__call__` 把 `self.launch_pdl` 统一插进第 5 位（[543](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/driver.py#L543)），把「编译期决策」与「运行期实参」干净分离。

---

## 5. 综合实践

把三个模块串起来，完成本讲规格里的核心任务：**通读 `make_launcher` → `launch` → `_launch` 这条链，说明「一段 Python 调用如何变成一次以 tile 为单位的 `cuLaunchKernelEx`」，并补全一张启动数据流图。**

### 实践目标

验证三件事：(1) `TileIRDriver` 通过 `TileIRUtils` 复用 NVIDIA C 模块、仅自研 `load_tileir_binary`；(2) `make_launcher` 按签名生成 C 胶水代码并即时编译；(3) `_launch` 以 tile 为单位、`block=1`、无条件 SPREAD、按需 PDL 发射。

### 操作步骤

1. **补全启动数据流图**（答案见文末）：

   ```
   ┌─ 编译期 ──────────────────────────────────────────────┐
   │ TileIROptions.①______  →  metadata → TileIRLauncher    │
   │ make_launcher(②______) → 拼出 C 源码 → compile_module_from_src │
   │   → mod.③______  挂到 TileIRLauncher.launch             │
   └────────────────────────────────────────────────────────┘
   ┌─ 运行期：一次 kernel 调用 ───────────────────────────────┐
   │ TileIRLauncher.__call__(*args)                          │
   │   model_args = args[:5] + (④______,) + args[5:]         │
   │   → launch(... numTilesX/Y/Z, launch_pdl, stream, fn …)  │
   │       PyArg_ParseTuple 解包 + getPointer/打包            │
   │       Py_BEGIN_ALLOW_THREADS                            │
   │         _launch:                                        │
   │           config.gridDim = ⑤______ ; blockDim = ⑥______ │
   │           attrs[0] = ⑦______ ; 若 launch_pdl: attrs++=PDL│
   │           cuLaunchKernelExHandle(&config, fn, params, 0) │
   │       Py_END_ALLOW_THREADS                              │
   └────────────────────────────────────────────────────────┘
   ```

2. **源码定位练习**：在 `driver.py` 里找出下面每个事实对应的行号区间（用 Grep/Read 核对）：
   - `_BASE_ARGS_FORMAT` 的定义；
   - 把 `launch_pdl` 注入实参的那一行；
   - `config.blockDimX = 1` 那一段；
   - `cuLaunchKernelExHandle(...)` 的调用；
   - `load_tileir_binary` 在 C 侧的定义所在文件。

3. （可选，需 Blackwell 环境）写一个最小 kernel，分别在 `launch_pdl=False` 与 `launch_pdl=True`（通过 `@triton.jit` 的 `launch_pdl` 选项）下编译运行，确认两者都正确产出结果，且 cubin 不同（hash 不同）。

### 需要观察的现象

- 数据流图填空：①`launch_pdl` ②`signature` ③`launch` ④`self.launch_pdl` ⑤`numTilesX/Y/Z` ⑥`1` ⑦`CLUSTER_SCHEDULING_POLICY_SPREAD`。
- 第 2 步行号：`_BASE_ARGS_FORMAT` 在 [driver.py:93](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/driver.py#L93)；注入 `launch_pdl` 在 [543](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/driver.py#L543)；`blockDimX=1` 在 [244](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/driver.py#L244)；`cuLaunchKernelExHandle` 调用在 [255](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/driver.py#L255)；`load_tileir_binary` 定义在 `third_party/tileir/backend/driver.c`。

### 预期结果

能完整复述「Python 调用 → 胶水代码解包 → `_launch` 组装 tile 式 `CUlaunchConfig` → `cuLaunchKernelEx` 发射」全链路，并指出 TileIR 相对 PTX 的三处运行期差异（grid=tile 数、block=1、sharedMem=0）与两个启动属性（SPREAD 无条件、PDL 按需）。运行类观察「待本地验证」。

## 6. 本讲小结

- `TileIRDriver` 继承上游 `GPUDriver`，靠 torch 获得设备探测能力；它独有的只是「装载 tile cubin」，其余 C 函数（设备属性、占用率等）通过 `TileIRUtils` **直接复用** NVIDIA 的 `driver.c`，只多编译一份自有的 `load_tileir_binary`。
- `make_launcher` **按签名现场拼一段 C 源码**（固定骨架 + 可变参数片段），经 `compile_module_from_src` 即时编成 `.so`，把 `mod.launch` 挂成 `TileIRLauncher.launch`——这段胶水代码完成「Python 对象 → C 参数数组」的全部翻译。
- 所有 launcher 共享前缀 `_BASE_ARGS_FORMAT = "iiiKKpOOOO"`：3 个 tile 维度、2 个句柄、1 个 `launch_pdl`、4 个 hook/metadata 对象；其中 `launch_pdl` 是编译期值，由 `TileIRLauncher.__call__` 注入到第 5 位。
- `_launch` 以 **tile 为单位启动**：`gridDim = tile 数`、`blockDim = 1`、`sharedMemBytes = 0`——这是 TileIR 与 PTX（`gridX*num_ctas`、`32*num_warps`、显式 shared mem）最本质的运行期差异。
- cluster scheduling 偏好**无条件**设为 `SPREAD`（因为 CUDA Tile IR 围绕 CGA/cluster 组织）；PDL 属性仅在 `launch_pdl != 0` 时追加。
- `cuLaunchKernelEx` 的函数指针运行期用 `dlopen`/`dlsym` 取得，避免在缺乏该符号的老驱动上链接失败。

## 7. 下一步学习建议

本讲讲完了「怎么把 tile cubin 装载并启动」，接下来可以顺着两条线深入：

- **向「TMA 描述符」深入**：本讲刻意回避了 `make_tensordesc_arg` / `wrap_handle_tensordesc` / `expand_tensordesc` 的细节。建议下一讲学习 [u2-l6 TMA Tensor Descriptor 的拆解与启动](u2-l6-tma-tensor-descriptor.md)，看 TileIR 如何在「没有 host TMA」的前提下，把 `tensordesc` 参数拆解成 `ptr/shape/stride` 传入内核、并在 launcher 里展开。
- **向「性能调优」深入**：本讲的 `occupancy`/`num_ctas`/`launch_pdl` 都是调优旋钮。建议后续学习 [u4-l2 性能调优实践](u4-l2-performance-tuning.md)，理解它们对 tile 调度与资源占用的实际影响。
- **源码延伸阅读**：对照阅读上游 `python/triton/backends/driver.py` 的 `GPUDriver`/`DriverBase` 抽象，以及 NVIDIA 后端 `third_party/nvidia/backend/driver.c` 的 `_launch`（[931-997](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/nvidia/backend/driver.c#L931-L997)），体会「同一套 launcher 生成框架、两种截然不同的启动模型」。
