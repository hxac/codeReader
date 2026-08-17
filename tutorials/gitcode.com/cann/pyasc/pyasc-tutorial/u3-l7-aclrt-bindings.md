# acl runtime 封装：lib/runtime 的 ctypes 绑定

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚 `_lazy_init` 中「加载动态库 → 绑定设备 → 创建流」三步惰性初始化的顺序与触发条件。
2. 掌握 `use_model` / `use_npu` 两个开关与 `config.set_platform` 的协作方式，理解 Model（仿真器）与 NPU（真机）两种后端到底差在哪里（链接了哪些不同的动态库）。
3. 读懂 `state.py` 中 `RuntimeInterface` 与 `NPUUtils` 的分工：一份随包源码编译成 ctypes 可调的 `extern "C"` 包装库，另一份编译成 CPython 扩展模块。
4. 理解 `build_utils.py` 如何在运行时「在线编译」这两个 cpp 文件、如何用文件缓存避免重复编译。
5. 能绕过 JIT，直接调用 `asc.lib.runtime` 的接口查询设备核数、SOC 版本、设备数量——这正是上一讲 Launcher 调用的最底层。

## 2. 前置知识

本讲是全手册离操作系统最近的一讲，先补齐几个底层概念：

- **动态库（shared library）与 ctypes**：C/C++ 编译产物 `.so` 文件里放着一堆函数符号；Python 标准库 `ctypes` 用 `ctypes.CDLL(path)` 把 `.so` 装进进程，之后 `lib.func(...)` 就能直接调 C 函数。调用前通常要设置 `restype`（返回值类型）与 `argtypes`（参数类型），否则 ctypes 只按默认的 `int` 猜。
- **句柄（handle）**：C API 不把「流」「内存」「kernel」这些对象暴露给调用方，只返回一个不透明指针（`rtStream_t*`、`void*`）。Python 侧用 `ctypes.c_void_p` 承接，只存不解析。pyasc 把它们统称为 Stream / Memory / Kernel / Function。
- **aclrt 运行时**：昇腾的设备运行时库（CANN 里的 `libruntime.so`、`libascendcl.so`）提供 `rtSetDevice`、`rtStreamCreate`、`rtMalloc`、`rtKernelLaunch` 等一整套 `rt` 前缀的 C 接口，函数返回 `rtError_t` 错误码，0 表示成功。这一层等价于 CUDA 里的 `cudaRuntime`。
- **camodel 仿真器**：CANN 提供的 `libruntime_camodel.so`，在 x86 主机上软件模拟同一套 `rt` 接口。没有 NPU 的机器只要装了 CANN Toolkit 就能用它跑通整个流程——这就是 pyasc 的 Model 模式。
- **extern "C" 与名字修饰**：C++ 编译器会把函数名改写成带类型信息的符号（如 `_ZN9tm_engine7tm_timeEv`），ctypes 按名字找符号就会失败；`extern "C"` 强制保留原始函数名。pyasc 的 `rt_wrapper.cpp` 全部用 `extern "C"` 导出，就是为了让 ctypes 能按名直调。
- **CPython 扩展模块**：另一种封装方式——cpp 里实现 `PyInit_模块名`，编出的 `.so` 本身就是一个可 `import` 的 Python 模块。`npu_utils.cpp` 走这条路，因为它要操作 Python 对象（profiling 上报）。
- **惰性初始化（lazy init）**：不在 `import` 时做任何重活，而是推迟到第一次真正用时。好处是 `import asc` 永远不会因为没装驱动而失败。

承接上一讲（u3-l6）：Launcher 中出现的 `rt.launch_kernel`、`rt.register_device_binary_kernel`、`rt.current_stream` 等调用，全部定义在本讲的 `interface.py` 里。本讲就是要把这些调用的「最后一层纸」捅破。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [python/asc/lib/runtime/interface.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/runtime/interface.py) | 对外的运行时接口层：`_lazy_init` 惰性初始化、设备/流管理、内存搬运、kernel 注册与下发，全部是对 `state.lib.call(...)` 的薄封装 |
| [python/asc/lib/runtime/state.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/runtime/state.py) | 模块级全局状态（当前设备、流表、kernel 表、内存表）+ `RuntimeInterface` / `NPUUtils` 两个加载器 |
| [python/asc/lib/runtime/support.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/runtime/support.py) | 纯数据定义：句柄类型别名、`CoreType` 等枚举、`DevBinary` 等 ctypes 结构体，镜像 CANN 头文件 |
| [python/asc/lib/runtime/build_utils.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/runtime/build_utils.py) | 在线编译器：`build_npu_ext` 拼接编译命令把随包 cpp 编成 `.so` |
| [python/asc/lib/runtime/rt_wrapper.cpp](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/runtime/rt_wrapper.cpp) | 随包 C++ 源码：`extern "C"` 包装 `rtGetDeviceInfo` 等 aclrt 接口，编译后由 `ctypes.CDLL` 加载 |
| [python/asc/lib/runtime/npu_utils.cpp](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/runtime/npu_utils.cpp) | 随包 C++ 源码：CPython 扩展模块，提供 `acl_init` / `msprof_*` 等 NPU 专属能力 |
| [python/asc/runtime/config.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/config.py) | 用户入口：`Backend`/`Platform` 枚举与 `set_platform`，内部转调 `rt.use_model`/`rt.use_npu` |
| [python/asc/runtime/launcher.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/launcher.py) | （上一讲主角）`get_core_num` 等方法示范了 `rt.device_info` 的标准用法 |
| [python/asc/lib/utils.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/utils.py) | `get_ascend_path`（读 `ASCEND_HOME_PATH`）等小工具 |

## 4. 核心概念与源码讲解

### 4.1 interface.py 前置接口：惰性初始化与运行时 API

#### 4.1.1 概念说明

`interface.py` 是 pyasc 对 aclrt 的「平铺」封装：文件里没有类，只有约 40 个模块级函数，每个函数对应一条 aclrt 原语（设设备、建流、分配内存、拷贝、注册 kernel、下发、同步……）。上一讲 Launcher 调用的 `rt.*` 全在这里。

它解决两个问题：

1. **隔离**：上层（Launcher、config、memory_handle）只依赖 Python 函数签名，不接触 ctypes 细节，也不关心底下链接的是 `libruntime.so` 还是 `libruntime_camodel.so`。
2. **统一守门**：所有函数第一行都调 `_lazy_init()`（或带 `need_device=False` / `need_stream=False` 的变体），保证「先初始化、后使用」这一时序在任何调用顺序下都成立。

#### 4.1.2 核心流程

`_lazy_init` 是三级台阶，每级都有独立的「是否已完成」判据，因此可以只走部分台阶：

```text
_lazy_init(need_device=True, need_stream=True):
    1. state.lib is None?                        # 台阶一：加载动态库
         state.load_lib()                        #   编译/加载 rt_wrapper.so（见 4.2）
         atexit.register(reset_device, None)     #   进程退出时复位设备
         注册 SIGINT 处理器（先 flush 再退出）
    2. need_device 且 state.device_id is None?   # 台阶二：绑定设备
         device_id = default_device()            #   默认 0 号卡
         set_device(device_id)
    3. need_stream 且该设备的流还是 None?          # 台阶三：创建流
         state.streams[device_id] = create_stream()
```

关键点：三个判据互相独立。例如 `device_count()` 传 `need_device=False`——数一下有几张卡不需要先选中某张卡；`current_device()` 传 `need_stream=False`——查询当前设备不需要流。而 `malloc`、`launch_kernel` 这类真正干活的原语走完整三级。

各接口的典型调用形态是「准备 ctypes 缓冲区 → `state.lib.call(包装函数名, 参数...))` → 读取结果」：

- 查询类（`device_count`、`device_info`、`current_platform`）：先造一个 `ctypes.c_int32` / `c_int64` / 字符数组，把它的地址传进去，C 侧填充，Python 侧读 `.value`。
- 句柄类（`create_stream`、`malloc`、`register_device_binary_kernel`）：传一个空 `c_void_p` 的地址进去，C 侧填句柄，Python 侧返回该句柄并登记进 `state.streams` / `state.allocs` / `state.kernels` 三张表，供后续 `free` / `unregister` 反查。

#### 4.1.3 源码精读

惰性初始化本体（注意三个独立判据与两个进程级副作用）：

[python/asc/lib/runtime/interface.py:L25-L38](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/runtime/interface.py#L25-L38) —— `_lazy_init`：首次调用时加载动态库并注册退出清理与 Ctrl-C 处理；随后按需绑定默认设备 0、为其惰性创建流。`atexit` 与 `signal` 的注册被「包在 `state.lib is None` 判断里」，因此整个进程只执行一次。

设备信息查询——本讲实践的主角：

[python/asc/lib/runtime/interface.py:L151-L163](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/runtime/interface.py#L151-L163) —— `device_info(module_type, info_type, device_id)`：先 `_lazy_init(need_stream=False)`（查设备信息不需要流），再组装 `GetDeviceInfoWrapper` 调用，四个参数依次是设备号、模块类型、信息类型、输出地址，返回 `c_int64` 的值。

它与 `support.py` 的两个枚举配套使用：

[python/asc/lib/runtime/support.py:L28-L37](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/runtime/support.py#L28-L37) —— `DeviceModuleType`：要查哪个模块（AICORE、AICPU、VECTOR_CORE……），值与 CANN 头文件中的 `DEV_MODULE_TYPE` 一致。

[python/asc/lib/runtime/support.py:L40-L52](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/runtime/support.py#L40-L52) —— `DeviceInfoType`：要查哪项信息。`INFO_TYPE_CORE_NUM = 3` 是核数；注意 `INFO_TYPE_CUBE_NUM = 0x775A5A5A` 是个特殊魔数值，Cube 核数量走的是 CANN 约定的专用通道，而不是连续编号。

上一讲 Launcher 查询核数就是这两个枚举的组合：

[python/asc/runtime/launcher.py:L60-L63](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/launcher.py#L60-L63) —— `Launcher.get_core_num`：静态方法，一行转调 `rt.device_info(RT_MODULE_TYPE_AICORE, INFO_TYPE_CORE_NUM, device_id)`。本讲实践会绕过 Launcher 直接调 `rt.device_info` 复现它。

SOC 版本与设备数量：

[python/asc/lib/runtime/interface.py:L129-L138](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/runtime/interface.py#L129-L138) —— `current_platform`：预分配 32 字节字符缓冲，调 `GetSocVersionWrapper` 让 C 侧填入形如 `Ascend910B1` 的字符串，解码返回。这就是 `set_platform` 在 NPU 模式下自动识别芯片型号的依据。

[python/asc/lib/runtime/interface.py:L141-L148](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/runtime/interface.py#L141-L148) —— `device_count`：`GetDeviceCountWrapper` 填 `c_int32` 后返回。

内存分配的 512 字节对齐技巧：

[python/asc/lib/runtime/interface.py:L269-L285](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/runtime/interface.py#L269-L285) —— `malloc`：多申请 512 字节（`size + 512`），再把返回地址向上取整到 512 的倍数：

\[ \text{对齐地址} = 512 \times \left\lceil \frac{p}{512} \right\rceil = 512 \times \left\lfloor \frac{p + 511}{512} \right\rfloor \]

因为多申请了 512 字节，向上取整后必然仍在分配范围内，不会越界。原始基地址存进 `state.allocs`，`free` 时用基地址（而不是对齐地址）去释放——这正是 `MemoryHandle.release_memory` 能正确回收显存的原因。

kernel 下发原语（衔接上一讲的参数 blob）：

[python/asc/lib/runtime/interface.py:L318-L334](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/runtime/interface.py#L318-L334) —— `launch_kernel`：把上一讲拼好的参数 blob（`uint64` 数组）连同核数、字节数（`num_args * 8`）、流句柄一起交给 `KernelLaunchWrapper`；不显式传流时默认用 `current_stream()`。这里能看到参数 blob「按 8 字节对齐」约定最终的消费点：`ctypes.c_uint32(num_args * 8)` 直接把 blob 总字节数告诉运行时。

`support.py` 顶部的四个类型别名是全部句柄的统一表示：

[python/asc/lib/runtime/support.py:L15-L18](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/runtime/support.py#L15-L18) —— `Stream` / `Memory` / `Kernel` / `Function` 全是 `ctypes.c_void_p` 的别名。Python 侧从不解析句柄内容，只在 `state` 的三张表里按 `.value`（即地址整数）做键来记账。

二进制注册用的结构体与魔数：

[python/asc/lib/runtime/support.py:L55-L68](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/runtime/support.py#L55-L68) —— `DevBinary` 结构体（magic/version/data/length）与 `MagicElf` 四个魔数。`interface.py` 的 `magic_elf_value`（[interface.py:L83-L93](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/runtime/interface.py#L83-L93)）按 `CoreType` 选魔数：AiCore 用 `0x43554245`、VectorCore 用 `0x41415246`、CubeCore 用 `0x41494343`——上一讲 `CompiledKernel.core_type` 在这里被翻译成运行时认得的二进制类型标记。

#### 4.1.4 代码实践

**实践目标**：绕过 JIT 与 Launcher，直接用 `asc.lib.runtime` 查询设备信息，验证 `_lazy_init` 确实是按需触发的。

**操作步骤**（示例代码，保存为 `probe_rt.py`）：

```python
# 示例代码
import asc.lib.runtime as rt
import asc.runtime.config as config

print("before init: is_initialized =", rt.is_initialized())   # 此时还没加载任何库

config.set_platform(config.Backend.Model)          # 只置开关，尚不真正初始化
print("after set_platform: is_initialized =", rt.is_initialized())

# 复现 Launcher.get_core_num（launcher.py:L61-L63 的实现）
core_num = rt.device_info(rt.DeviceModuleType.RT_MODULE_TYPE_AICORE,
                          rt.DeviceInfoType.INFO_TYPE_CORE_NUM)
print("AiCore core num =", core_num)
print("soc version  =", rt.get_soc_version())      # Python 侧记录的 Platform
print("real platform =", rt.current_platform())    # 从库侧读回的字符串
print("device count =", rt.device_count())
print("is_model =", rt.is_model())
```

运行：`python3 probe_rt.py`（需已安装 pyasc 并 source CANN 环境变量，Model 模式还需按 `set_platform` 报错提示导出 `LD_LIBRARY_PATH=$ASCEND_HOME_PATH/tools/simulator/Ascend910B1/lib`）。

**需要观察的现象**：

1. 第一行打印 `is_initialized = False`，说明 `import` 和 `set_platform` 都没有触发动态库加载。
2. `device_info` 执行之后 `is_initialized` 变为 `True`——第一次真正调用原语才走 `_lazy_init`。
3. `core_num` 返回一个正整数（Ascend910B1 仿真器下通常为 50，即该芯片 AI Core 数）。

**预期结果**：`get_soc_version()` 返回 `Platform.Ascend910B1`（Model 默认值），`current_platform()` 返回字符串 `"Ascend910B1"`，`device_count()` 在 Model 模式下返回 1。具体核数与设备数因环境而异，属「待本地验证」项。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `device_count` 传 `need_device=False`，而 `malloc` 必须走完整的三级初始化？

**答案**：`device_count` 只统计系统里有几张卡，`GetDeviceCountWrapper` 不依赖「当前线程绑定了哪张卡」，跳过台阶二可以避免在无卡环境下查数量也被迫初始化设备；`malloc` 分配的是设备内存，必须先有设备（台阶二）且后续拷贝/释放要走流（台阶三），所以需要完整初始化。

**练习 2**：`malloc` 为什么要 `size + 512` 再向上对齐，而不是直接申请 `size`？

**答案**：运行时返回的地址只保证是字节对齐，而后续 kernel 访问通常要求 512 字节对齐。多申请 512 字节后把地址向上取整到 512 倍数，既满足对齐，又保证对齐后的区间仍在分配范围内。代价是最多浪费约 1KB；`free` 时必须用登记在 `state.allocs` 里的原始基地址，否则会把地址释放错。

**练习 3**：`INFO_TYPE_CUBE_NUM` 的值为什么是 `0x775A5A5A` 而不是 11？

**答案**：Cube 核数量不在 `rtGetDeviceInfo` 的常规连续编号里，CANN 为它约定了一个专用魔数通道（`0x775A5A5A`），驱动侧识别到该魔数就走 Cube 查询分支。pyasc 在 `support.py` 里如实镜像了这个约定，说明这层枚举是 CANN 头文件的逐值对照，不是 pyasc 自创的编号。

### 4.2 state 全局状态：两份随包源码、两个动态库

#### 4.2.1 概念说明

`state.py` 承担两个角色：

1. **全局状态板**：文件底部一组模块级变量（`model`、`soc_verison`、`device_id`、`streams`、`kernels`、`allocs`、`lib`、`npu_utils`）就是整个运行时的全部可变状态。`interface.py` 里所有函数读写的都是这几个变量——pyasc 没有为运行时建类，状态就是模块本身。
2. **两个加载器**：
   - `RuntimeInterface`：把随包的 `rt_wrapper.cpp` 编译成 `.so`，用 `ctypes.CDLL` 加载。它只有 `call` 一个方法，纯粹是「函数名 + 参数 → 调 C」。
   - `NPUUtils`：把随包的 `npu_utils.cpp` 编译成 CPython 扩展模块，用 `importlib` 当作 Python 模块加载，暴露 `acl_init`、`msprof_report_api` 等 NPU 专属能力。Model 模式下它是个「空壳单例」，访问任何属性都会报错。

为什么需要两个库？`rt_wrapper` 的职责是「把 `rt` C 接口的错误码与句柄翻译成 ctypes 能用的形态」，与 Python 对象无关，用 `extern "C"` 最简单；`npu_utils` 要在 msprof 回调里操作 Python 对象（构造上报数据），必须走 CPython 扩展模块这条路。一个面向机器（ctypes），一个面向 Python（模块），故分而治之。

#### 4.2.2 核心流程

`state.load_lib()`（由 `_lazy_init` 首次触发）的完整流程：

```text
load_lib():
    lib       = RuntimeInterface(is_model=model, soc=soc_verison)
    npu_utils = NPUUtils(is_model=model, soc=soc_verison)

RuntimeInterface.__init__(is_model, soc):
    1. 读取随包 rt_wrapper.cpp 全文（就在 state.py 同目录）
    2. cache_key = sha256( cpp全文 + version.cfg内容 + str(is_model) )
    3. 去文件缓存目录找 librt_wrapper<EXT_SUFFIX>
       命中 → 直接返回路径
       未命中 → 写临时目录 → build_npu_ext 在线编译 → .so 字节存入缓存
    4. ctypes.CDLL(so路径, RTLD_LOCAL) 得到 lib
    5. 若 is_model：准备 CAMODEL_LOG_PATH（环境变量已有则用之，
       否则 mkdtemp 一个 pyasc_camodel_ 前缀目录，atexit 时删除）

NPUUtils(is_model, soc):
    is_model 为真 → __init__ 直接 return（空壳单例）
    否则 → 同样的"读源码-算key-查缓存-在线编译"流程得到 libnpu_utils.so，
           再用 importlib 把它当 Python 模块加载，取出 acl_init / msprof_* 函数
```

缓存 key 的设计值得注意：

\[ \text{key} = \mathrm{sha256}\big(\,\text{rt\_wrapper.cpp 全文} \;\|\; \text{version.cfg 全文} \;\|\; \text{str(is\_model)}\,\big) \]

三个因子分别对应三种失效场景：cpp 改了（pyasc 升级）、CANN 版本变了（version.cfg 变了，ABI 可能不兼容）、模式切换了（Model 与 NPU 链接的库不同，产物必然不同）。任何一个变化都会得到新 key，从而重新编译，保证缓存里的 `.so` 永远与当前环境匹配。

#### 4.2.3 源码精读

`RuntimeInterface` 的构造与缓存查找：

[python/asc/lib/runtime/state.py:L27-L48](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/runtime/state.py#L27-L48) —— 读取同目录 `rt_wrapper.cpp`，拼接 `version.cfg` 内容与 `is_model` 计算 sha256 作为缓存 key，向 `FileCacheManager` 要 `librt_wrapper<EXT_SUFFIX>`；拿不到就把源码写进临时目录、调 `build_npu_ext` 现场编译，再把 `.so` 字节 `put` 进缓存。注意 `EXT_SUFFIX` 来自 `sysconfig`（如 `.cpython-310-x86_64-linux-gnu.so`），所以不同 Python 版本天然得到不同缓存文件名。

加载与 Model 日志目录：

[python/asc/lib/runtime/state.py:L50-L61](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/runtime/state.py#L50-L61) —— `ctypes.CDLL(rt_lib, ctypes.RTLD_LOCAL)` 加载（`RTLD_LOCAL` 表示符号不污染全局符号表，避免与进程里其他 CANN 库冲突）；Model 模式下准备 `CAMODEL_LOG_PATH`：环境变量没设就 `mkdtemp` 建临时目录并用 `atexit` 注册退出清理，仿真器的日志会写在这里——调试仿真行为时可以先 `export CAMODEL_LOG_PATH=/tmp/mylog` 固定位置。

统一的调用与错误检查：

[python/asc/lib/runtime/state.py:L63-L75](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/runtime/state.py#L63-L75) —— `call_from` 按函数名从 `CDLL` 取符号、把 `restype` 统一设为 `c_uint64`（对应 `rtError_t`），调用后 `check_error` 发现非 0 就抛 `RuntimeError("Function xxx returned N")`。这就是 `interface.py` 里所有函数不写 try/except 的原因：错误在最后一公里被统一翻译成 Python 异常。

`NPUUtils` 的单例与 Model 空壳：

[python/asc/lib/runtime/state.py:L80-L83](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/runtime/state.py#L80-L83) —— `__new__` 里用 `cls.instance` 实现单例：无论构造几次，拿到的都是同一个对象。

[python/asc/lib/runtime/state.py:L85-L121](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/runtime/state.py#L85-L121) —— `__init__` 第一行 `if is_model: return`：Model 模式直接返回空壳，不编译不加载；NPU 模式则重复「读 npu_utils.cpp → 算缓存 key → 查缓存/在线编译」的流程，之后用 `importlib.util.spec_from_file_location` 把 `.so` 当 Python 模块加载（因为里面有 `PyInit_npu_utils`），并挑出 `acl_init`、`acl_finalize`、`msprof_*` 五个函数挂到实例属性上。

[python/asc/lib/runtime/state.py:L123-L126](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/runtime/state.py#L123-L126) —— `__getattr__` 兜底：Model 模式下访问任何未初始化的属性抛出「properties are not available when Model backend is active」。阅读细节：这里判断的是模块级全局变量 `model`（定义在 L129），不是参数也不是 `state.model`——`__getattr__` 只在常规属性查找失败后才触发，因此 NPU 模式下真实存在的属性不会走到这里。

全局状态板与加载入口：

[python/asc/lib/runtime/state.py:L129-L137](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/runtime/state.py#L129-L137) —— 八个模块级变量：`model`（是否仿真）、`soc_verison`（芯片型号，注意变量名拼写即如此）、`custom_lib_prefix`、`device_id`（当前设备，`None` 表示未绑定）、`streams`（设备号 → 流句柄的字典）、`kernels`（句柄 → 二进制字节的登记表）、`allocs`（对齐地址 → 原始基地址）、`lib` 与 `npu_utils`（两个加载器实例）。

[python/asc/lib/runtime/state.py:L140-L145](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/runtime/state.py#L140-L145) —— `load_lib`：用当前 `model` / `soc_verison` 快照构造两个加载器。也就是说，**在 `load_lib` 之前调用 `use_model` / `use_npu` 才有效**；库一旦加载完成，再切开关不会重建已加载的库（需要重新 `set_platform` 并复位设备，见 4.3）。

#### 4.2.4 代码实践

**实践目标**：亲眼确认「进程里真的多了一个在线编译出来的 `.so`」，并观察 `state` 状态板的变化。

**操作步骤**（示例代码）：

```python
# 示例代码
import asc.lib.runtime as rt
from asc.lib.runtime import state
import asc.runtime.config as config

config.set_platform(config.Backend.Model)
_ = rt.device_count()          # 触发 _lazy_init 完整走一遍

print("device_id =", state.device_id)
print("streams   =", {k: v for k, v in state.streams.items()})
print("lib type  =", type(state.lib).__name__)

# 看进程实际加载了哪些相关动态库
with open("/proc/self/maps") as f:
    for line in f:
        if "rt_wrapper" in line or "camodel" in line or "npu_utils" in line:
            print(line.split()[-1])
            break   # 同一路径会重复多行，每类打印一行即可
```

**需要观察的现象**：

1. `device_id` 从 `None` 变成 `0`；`streams` 里出现键 `0`。
2. `/proc/self/maps` 中能找到 `librt_wrapper*.so`（在线编译的包装库）与 `libruntime_camodel.so`（仿真器本体，被前者链接依赖）的路径。
3. 找不到 `libnpu_utils*.so`——因为 Model 模式下 `NPUUtils` 是空壳，根本没有编译加载它。

**预期结果**：打印出两个 `.so` 路径，其中 `librt_wrapper` 位于 pyasc 缓存目录——由 [python/asc/runtime/cache.py:L22-L23](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/cache.py#L22-L23) 定义：`PYASC_CACHE_DIR` 环境变量优先，默认是 `~/.pyasc/cache`（`PYASC_HOME` 下），且实际文件在以缓存 key 命名的子目录里；`libruntime_camodel.so` 位于 `$ASCEND_HOME_PATH/tools/simulator/<soc>/lib`。

#### 4.2.5 小练习与答案

**练习 1**：缓存 key 为什么必须把 `str(is_model)` 混进去？不加会怎样？

**答案**：Model 与 NPU 两个模式链接的运行时库不同（`-lruntime_camodel` vs `-lruntime -lmsprofiler`），编出的 `librt_wrapper.so` 符号依赖完全不同。若不把模式混进 key，先在 Model 模式编译的 `.so` 会被 NPU 模式直接复用，进程里却没有它依赖的 `libruntime.so` 等真机库，加载或调用时报「symbol not found」。同理，`version.cfg` 防 CANN 版本 ABI 变化，cpp 全文防 pyasc 自身升级。

**练习 2**：`ctypes.CDLL(..., ctypes.RTLD_LOCAL)` 的 `RTLD_LOCAL` 起什么作用？

**答案**：`RTLD_LOCAL` 让该库导出的符号只对本库内部可见，不进入进程全局符号表。pyasc 进程里可能同时存在 torch_npu、其他 CANN 组件加载的多份 CANN 动态库，若用 `RTLD_GLOBAL` 让包装库符号全局可见，可能与别处的同名符号（如各 Wrapper 函数依赖的 rt 实现）发生解析冲突。这是多库共存场景的常规防御。

**练习 3**：Model 模式下调用 `rt.acl_init()` 会发生什么？为什么这样设计？

**答案**：`acl_init` 转调 `state.npu_utils.acl_init()`（[interface.py:L397-L398](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/runtime/interface.py#L397-L398)），而 Model 模式下 `NPUUtils` 是空壳单例，属性查找落到 `__getattr__`，抛出「properties are not available when Model backend is active」。设计原因：`aclInit`/msprof 上报只在真机+profiling 场景有意义，仿真器不需要也做不到；与其让调用静默失败，不如显式报错把模式配错的问题尽早暴露。

### 4.3 Model/NPU 模式：use_model/use_npu 与 config.set_platform

#### 4.3.1 概念说明

pyasc 有两种执行后端：

| 维度 | Model（仿真器） | NPU（真机） |
| --- | --- | --- |
| 底层库 | `libruntime_camodel.so`（软件模拟 rt 接口） | `libruntime.so` + `libascendcl.so` + `libmsprofiler.so` |
| 库的位置 | `$ASCEND_HOME_PATH/tools/simulator/<soc>/lib` | `$ASCEND_HOME_PATH/lib64` |
| 是否需要 NPU 硬件 | 否，x86 主机即可 | 是 |
| 芯片型号来源 | 用户指定（默认 `Ascend910B1`） | `rt.current_platform()` 从设备读真值 |
| `npu_utils`（acl/msprof） | 不可用（空壳） | 可用 |
| 典型用途 | 开发调试、CI、无卡环境 | 性能测量与生产 |

两个开关函数极其简单——只改 `state` 里的两个变量：

```python
def use_model(custom_lib_prefix=None):
    state.model = True
    state.custom_lib_prefix = custom_lib_prefix
```

真正「合起来」的地方是 `config.set_platform`：它是用户侧唯一推荐入口，负责把后端选择、芯片型号、设备号、可用性检查串成一步。示例代码 `examples/01_add/add.py` 中的 `config.set_platform(backend, platform)` 走的就是它。

#### 4.3.2 核心流程

`set_platform(backend, soc_version=None, device_id=None, check=True)` 的决策树：

```text
backend == "Model":
    soc_version 为空 → 默认 Ascend910B1
    rt.use_model()                       # 置 state.model = True
backend == "NPU":
    soc_ver = Platform(rt.current_platform())   # 惰性初始化并从设备读真实型号
    用户传了 soc_version 且与真实值不符 → ValueError
    soc_version = 真实值
    rt.use_npu()
其他 → ValueError

rt.set_soc_version(soc_version)           # 记录到 state.soc_verison
device_id 不为空 → rt.set_device(device_id)
check 为真 且 not rt.is_available() → RuntimeError
    （Model 模式的报错信息会提示补 LD_LIBRARY_PATH 指向 simulator lib）
```

时序要点：`use_model`/`use_npu` 只置开关，**不加载库**；库在第一次原语调用时按当时的开关快照加载（见 4.2）。因此「先 `set_platform` 再跑算子」的顺序不是形式主义——如果库已按 Model 加载，再切 NPU 开关，已加载的 `lib` 不会自动换。`is_available` 用 `need_device=False` 的试探初始化探测「wrapper 库能否加载」，异常时把 `state.lib` 复位为 `None` 并返回 `False`，把「环境没配好」从崩溃降级为可检查的布尔值。

#### 4.3.3 源码精读

两个模式开关：

[python/asc/lib/runtime/interface.py:L48-L55](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/runtime/interface.py#L48-L55) —— `use_model` / `use_npu`：各三行，只写 `state.model` 与 `state.custom_lib_prefix`。`custom_lib_prefix` 允许用自定义前缀的库替换随包 wrapper（定制环境的逃生口）。

可用性探测：

[python/asc/lib/runtime/interface.py:L70-L76](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/runtime/interface.py#L70-L76) —— `is_available`：尝试 `need_device=False` 的初始化，任何异常都吞掉并把 `state.lib` 复位为 `None` 返回 `False`。复位这一步很关键——失败后 `state.lib` 停留在 `None`，下次调用还有机会重试，而不是带着半初始化状态继续跑。

用户入口：

[python/asc/runtime/config.py:L48-L67](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/config.py#L48-L67) —— `set_platform` 主体：Model 分支补默认芯片并 `use_model`；NPU 分支先 `rt.current_platform()` 读真实型号，用户指定值与之不符则抛 `ValueError`（防止按 910B1 编译却跑在 910B4 上），一致则采纳真实值并 `use_npu`；最后统一 `set_soc_version`。

[python/asc/runtime/config.py:L68-L76](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/config.py#L68-L76) —— 尾部：可选 `set_device`；`check=True` 时用 `is_available` 验证，失败信息在 Model 分支追加「请把 simulator lib 加入 LD_LIBRARY_PATH」的提示——这是新手最常遇到的第一个环境错误。

模式差异在代码里的三个「if is_model」落点：

[python/asc/lib/runtime/interface.py:L369-L379](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/runtime/interface.py#L369-L379) —— `c2c_ctrl_addr`（混合核间通信控制地址）：Model 模式直接返回仿真器约定的魔数 `255086295400448`，NPU 模式才真正调 `GetC2cCtrlAddrWrapper` 查询。这是上一讲 FftsAddr 隐藏参数的来源。

[python/asc/lib/runtime/interface.py:L409-L416](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/runtime/interface.py#L409-L416) —— `current_tick`：Model 专属的仿真时钟。注意它不走 Wrapper，而是直接按 C++ 修饰名 `_ZN9tm_engine7tm_timeEv` 从 `state.lib.lib`（即 camodel 侧符号）取函数——仿真器内部引擎的 C++ 类方法没有 extern "C" 包装，只能按修饰名直取，且先用 `is_model()` 挡住 NPU 模式（返回 `None`）。

模式差异在 C++ 侧的根源：

[python/asc/lib/runtime/build_utils.py:L77-L80](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/runtime/build_utils.py#L77-L80) —— 编译命令的链接段是唯一按模式分叉的地方：Model 链接 `tools/simulator/<soc>/lib` 下的 `-lruntime_camodel`；NPU 链接 `-lruntime -lmsprofiler` 系真机库。同一个 `rt_wrapper.cpp`，链接哪套库，就决定了它包的是仿真器还是真机运行时——这就是「模式」的本质。

[python/asc/lib/runtime/rt_wrapper.cpp:L66-L69](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/runtime/rt_wrapper.cpp#L66-L69) —— 包装层本体一瞥：`GetDeviceInfoWrapper` 四行代码原样转调 `rtGetDeviceInfo`。整份 `rt_wrapper.cpp` 就是几十个这样的「改名转发」，把 `rt` 前缀 C 接口统一成 `XxxWrapper` 命名并保持 `extern "C"`。

#### 4.3.4 代码实践

**实践目标**：对比 Model 与 NPU 两种模式下同一批查询接口的返回值，并确认加载的是不同的动态库。

**操作步骤**：

1. 用 4.1 的 `probe_rt.py`（Model 版）跑一遍，记下 `core_num`、`current_platform()`、`/proc/self/maps` 里的库名。
2. 把 `config.set_platform(config.Backend.Model)` 改为 `config.set_platform(config.Backend.NPU)`，放到另一台装了 NPU 的机器（或同一台有卡的环境）上再跑一遍，记录同样三项。
3. 在无 NPU 的机器上直接跑 NPU 版，观察报错。

**需要观察的现象**：

- Model 版 maps 里出现 `libruntime_camodel.so`；NPU 版 maps 里出现 `libruntime.so` / `libascendcl.so`，且两者都有各自的 `librt_wrapper*.so`（缓存路径不同，因为 key 里混了 `str(is_model)`）。
- NPU 版的 `current_platform()` 返回真实芯片（如 `Ascend910B4`）；若代码里硬编码 `set_platform(config.Backend.NPU, config.Platform.Ascend910B1)` 而真机不是 910B1，会在 `set_platform` 内抛 `ValueError`。
- 无卡机器跑 NPU 版：报错信息与 `set_platform` 的 `check` 分支或库加载失败相关。

**预期结果**：两模式的核数一般不同（不同芯片 AI Core 数不同）；`is_model()` 分别为 `True` / `False`。NPU 侧的具体数值与报错文案属「待本地验证」——本环境无 NPU，无法替你跑真机分支。

#### 4.3.5 小练习与答案

**练习 1**：为什么 NPU 分支里用户传入的 `soc_version` 与真实值不一致要抛异常，而不是以用户传入为准？

**答案**：芯片型号决定了后端编译目标（`CompilationTarget` 会按 Platform 选 `dav-c220-vec/cube` 架构，见 u3-l5）以及 Model 模式下仿真器库的路径。若允许「按 910B1 编译、跑在 910B4 上」，生成的二进制与真实硬件指令集/核数不匹配，错误会推迟到运行期才爆出且难以定位。在 `set_platform` 处一次性校验，把错误前移到配置阶段。

**练习 2**：先 `set_platform("Model")` 跑了一个算子，再 `set_platform("NPU")` 继续跑，会发生什么？

**答案**：第二次 `set_platform` 只把 `state.model` 置为 `False` 并调用 `set_soc_version`/`is_available`。但 `state.lib` 已经按 Model 快照加载完成（`_lazy_init` 的 `state.lib is None` 判据不再成立），后续原语仍走 camodel 包装库；`NPUUtils` 倒会在下次 `load_lib` 时才构造，而 `load_lib` 不会再被调用。也就是说同一进程内切模式并不会真正切换底层库——正确做法是一个进程一种模式，切换需重启进程。（精确行为「待本地验证」，但按 `_lazy_init` 与 `load_lib` 的判据可从源码推出。）

**练习 3**：`current_tick` 为什么能用 `getattr(state.lib.lib, "_ZN9tm_engine7tm_timeEv")` 拿到函数，而其他接口必须用 `XxxWrapper` 名字？

**答案**：其他接口调的是 `rt_wrapper.cpp` 里显式 `extern "C"` 导出的包装函数，符号名未修饰，ctypes 按原名即可找到；`current_tick` 要的是 camodel 库内部 `tm_engine::tm_time()` 这个 C++ 方法，没有 extern "C" 包装，符号经过了 C++ 名字修饰（`_ZN9tm_engine7tm_timeEv` 就是 `tm_engine::tm_time()` 的 Itanium ABI 修饰名），只能按修饰名字符串查找，还要手工声明 `restype = c_uint64`。

### 4.4 build_utils：在线编译随包 cpp 的工具链

#### 4.4.1 概念说明

一个自然的问题：为什么不预编译好 `.so` 随 wheel 分发，而要在用户机器上「在线编译」？

因为编译产物依赖三样极易变化的东西：

1. **CANN 版本**：`rt_wrapper.cpp` include 的 `runtime/rt.h` 来自本机 `$ASCEND_HOME_PATH`，不同 CANN 版本的 ABI（结构体布局、枚举值）可能不同；还有两代头文件目录布局（`pkg_inc` 分架构布局 vs `include/experiment` 旧布局）。
2. **Python 版本**：`npu_utils.cpp` 是 CPython 扩展模块，必须链接当前解释器的头文件，产物后缀 `EXT_SUFFIX` 随 Python 版本变化。
3. **目标模式**：Model / NPU 链接不同的运行时库（见 4.3）。

预编译无法覆盖这些组合，所以 pyasc 把两个 cpp 源文件随包分发，在目标机器上首次使用时现场编译，编好放进文件缓存，之后一直复用。`build_utils.build_npu_ext` 就是那次「现场编译」的执行者——本质上是一段拼 `g++`/`clang++` 命令行并 `subprocess` 执行的代码。

#### 4.4.2 核心流程

`build_npu_ext(obj_name, is_model, soc, src_path, src_dir)` 的步骤：

```text
1. 定位编译器：优先 $CC 环境变量 → g++ → clang++，都没有则报错
2. 定位 Python 头文件目录：sysconfig 按 scheme 取 include
   （Debian 的 posix_local scheme 需归一为 posix_prefix）
3. 定位 CANN 头文件，两种布局二选一：
   新布局：$ASCEND_HOME_PATH/<arch>-linux/pkg_inc 存在
        → -I pkg_inc -I pkg_inc/profiling -I pkg_inc/runtime -DSEPARATE_PKG_ARCH
   旧布局：→ -I include/experiment -I include/experiment/msprof
4. 公共参数：-I include -I pybind11头 -L lib64
5. 按模式链接：
   Model → -L tools/simulator/<soc>/lib -lruntime_camodel
   NPU   → -lruntime -lmsprofiler
6. 收尾：-lascendcl -std=c++17 -shared -fPIC -o lib<name><EXT_SUFFIX>
   subprocess.check_call 执行；返回 .so 路径
```

产物 `lib<obj_name><EXT_SUFFIX>`（`librt_wrapper*.so` / `libnpu_utils*.so`）要么落在调用方给的临时目录（随后被读入缓存），要么根本不再生成（缓存命中时）。

#### 4.4.3 源码精读

CANN 根目录定位（环境门槛）：

[python/asc/lib/runtime/build_utils.py:L22-L27](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/runtime/build_utils.py#L22-L27) —— `get_ascend_path`：读 `ASCEND_HOME_PATH`，为空直接抛 `EnvironmentError` 并提示「先 source set_env.sh」。这是 pyasc 里最靠前的环境检查之一。注意 `lib/utils.py` 里还有一个加了 `functools.lru_cache` 的同名函数（[python/asc/lib/utils.py:L16-L21](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/utils.py#L16-L21)），`state.py` 实际 import 的是后者；两处实现一致，读缓存的版本只查一次环境变量。

编译器与 Python 头文件：

[python/asc/lib/runtime/build_utils.py:L34-L50](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/runtime/build_utils.py#L34-L50) —— 编译器取 `$CC`，缺省 `g++` 优先于 `clang++`；Python include 目录用 `sysconfig` 按 scheme 求取，并处理 Debian 系 `posix_local` scheme 的兼容问题（注释说明了 Python 3.10 起默认安装路径带 `local`，需要归一化才能配合系统 Python 使用）。

CANN 头文件的两代布局适配：

[python/asc/lib/runtime/build_utils.py:L53-L69](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/runtime/build_utils.py#L53-L69) —— 先按 `platform.machine()` 拼 `<arch>-linux/pkg_inc`，三个子目录（pkg_inc、profiling、runtime）都存在就走新布局并加 `-DSEPARATE_PKG_ARCH` 宏——这个宏决定了 `rt_wrapper.cpp` 顶部 include 哪个头文件（[rt_wrapper.cpp:L11-L15](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/runtime/rt_wrapper.cpp#L11-L15)：新布局 include `runtime/rt.h`，否则 include `experiment/runtime/runtime/rt.h`）；任一不存在则回退旧布局 `include/experiment`。一段 Python 适配两代 CANN 安装结构。

命令拼装与按模式链接：

[python/asc/lib/runtime/build_utils.py:L71-L83](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/runtime/build_utils.py#L71-L83) —— 公共段 `-I include -I pybind11 -L lib64` 之后就是 4.3 分析过的模式分叉（camodel vs 真机库），最后统一 `-lascendcl -std=c++17 -shared -fPIC` 生成共享库，`subprocess.check_call` 非零即抛错。整个函数没有平台相关的高级逻辑，就是一条可读的命令行装配线。

手工编译入口（调试用）：

[python/asc/lib/runtime/build_utils.py:L91-L93](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/runtime/build_utils.py#L91-L93) —— `__main__` 直接以 Model 模式编译 `npu_utils.cpp` 与 `rt_wrapper.cpp` 到当前目录。注意它用的是相对路径 `./npu_utils.cpp`，所以要在 `python/asc/lib/runtime` 目录下运行才能找到源文件。想复现编译命令排查链接错误时，这里是现成的入口。

缓存侧的配合（产物从哪拿、往哪放）：

[python/asc/runtime/cache.py:L32-L37](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/cache.py#L32-L37) —— `CacheManager` 的两个抽象方法：`get_file` 按文件名查缓存路径（未命中返回 `None`），`put` 把字节写入缓存并返回路径。`state.py` 正是用这两个方法实现「先查、未命中再编译」。

[python/asc/runtime/cache.py:L103-L105](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/cache.py#L103-L105) —— `get_cache_manager(key)`：目前唯一实现是 `FileCacheManager`，key 先经 `_base32` 转成目录名安全的形态。注意这套文件缓存与 u3-l8 要讲的 kernel 缓存共用同一套 `FileCacheManager` 基础设施，只是 key 的构成不同（这里是 cpp 哈希+CANN 版本+模式，kernel 那里是源码哈希+编译选项）。

#### 4.4.4 代码实践

**实践目标**：观察「首次在线编译、二次缓存命中」，并亲手触发一次编译。

**操作步骤**：

1. 记录缓存根目录（默认由 `cache_options.dir` 决定；也可 `export PYASC_CACHE_DIR=/tmp/pyasc_cache` 指定）。
2. 清空该目录后运行 4.1 的 `probe_rt.py`，用 `time python3 probe_rt.py` 计时——首跑应包含一次 `g++` 编译（可另开终端 `ps -ef | grep g++` 抓到编译进程，或直接观察首跑明显更慢）。
3. 再跑第二次、第三次，计时对比；在缓存目录下 `find /tmp/pyasc_cache -name 'librt_wrapper*'` 应能找到编好的 `.so`。
4. （可选）进入 `python/asc/lib/runtime` 目录，设置好 `ASCEND_HOME_PATH` 后运行 `python3 -m asc.lib.runtime.build_utils`（需 pyasc 已安装或 `PYTHONPATH` 指向 `python/`），在当前目录得到 `libnpu_utils*.so` 与 `librt_wrapper*.so`，即手工复现在线编译。

**需要观察的现象**：

1. 第一次运行明显慢（多了编译），第二三次几乎瞬时完成。
2. 缓存目录里出现 `librt_wrapper<EXT_SUFFIX>.so`；Model 模式下**不会**出现 `libnpu_utils*.so`。
3. 用 `ldd` 检查该 `.so`（`ldd $(find /tmp/pyasc_cache -name 'librt_wrapper*' | head -1)`），能看到它依赖 `libruntime_camodel.so`——印证 4.3 的模式分叉。

**预期结果**：以上三点均可在装好 CANN 的 x86 机器上复现；具体耗时与缓存路径因环境而异。若第 4 步模块入口因导入路径问题失败，属「待本地验证」项，可退回用第 1—3 步的观察结论。

#### 4.4.5 小练习与答案

**练习 1**：在线编译失败最常见的原因有哪些？分别对应 `build_npu_ext` 的哪一段？

**答案**：三类。① 找不到编译器——L34-L40 的 `$CC`/`g++`/`clang++` 探测全空，报「Failed to find C++ compiler」，装 g++ 或设 `CC` 即可；② `ASCEND_HOME_PATH` 未设置——`get_ascend_path` 抛 `EnvironmentError`，提示 source `set_env.sh`；③ 头文件/库路径不对——新布局探测失败回退旧布局后仍找不到 `rt.h`，或 Model 模式下 `tools/simulator/<soc>/lib` 不存在导致链接失败（此时 `set_platform(check=True)` 的报错会提示补 `LD_LIBRARY_PATH`）。

**练习 2**：`-DSEPARATE_PKG_ARCH` 这个宏在整个链路里起了什么作用？

**答案**：它是 Python 探测端与 C++ 源码端的「布局握手信号」。Python 侧发现新布局 `pkg_inc` 存在时加上该宏（build_utils.py:L63），`rt_wrapper.cpp` 顶部据此选择 `#include "runtime/rt.h"`（新布局路径）而非 `experiment/runtime/runtime/rt.h`（旧布局路径）。两端必须一致，否则要么编译期找不到头文件，要么 include 到另一代 API 声明。

**练习 3**：为什么 `npu_utils.cpp` 编出来的 `.so` 能被 `importlib` 当模块加载，而 `rt_wrapper.cpp` 的不行？

**答案**：`npu_utils.cpp` 是标准 CPython 扩展模块：定义了 `PyMethodDef` 方法表与 `PyInit_npu_utils` 入口（[npu_utils.cpp:L200-L222](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/runtime/npu_utils.cpp#L200-L222)），链接了 Python 头文件，符合扩展模块协议，所以能被 `spec_from_file_location` 加载；`rt_wrapper.cpp` 只有 `extern "C"` 的普通 C 函数，没有 `PyInit_` 入口，不实现模块协议，只能用 `ctypes.CDLL` 按符号调用。

## 5. 综合实践

**任务**：写一个约 30 行的 `rt_probe_all.py`，不写任何 `@asc.jit` 算子，仅用 `asc.lib.runtime` 这一层把「初始化 → 查询设备 → 内存搬运回路 → 清理」整条运行时链路亲手走一遍，并在最后自检加载了哪些动态库。这相当于把 u3-l6 的 `MemoryHandle` 三段生命周期手动复现，把两讲串起来。

**操作步骤**（示例代码）：

```python
# 示例代码
import ctypes
import asc.lib.runtime as rt
from asc.lib.runtime import state
import asc.runtime.config as config

config.set_platform(config.Backend.Model)

# 1) 设备查询（复现 Launcher.get_core_num）
print("platform:", rt.current_platform(), "| devices:", rt.device_count(),
      "| aicore:", rt.device_info(rt.DeviceModuleType.RT_MODULE_TYPE_AICORE,
                                  rt.DeviceInfoType.INFO_TYPE_CORE_NUM))

# 2) 内存搬运回路（复现 MemoryHandle 的三段生命周期，见 memory_handle.py:L46-L57）
data = bytearray(b"pyasc-rt-roundtrip" * 4)          # 72 字节
host_ptr = ctypes.cast(ctypes.pointer(ctypes.c_char.from_buffer(data)), ctypes.c_void_p)

dev_mem = rt.copy_data_to_device(host_ptr, len(data))  # 上设备：malloc + H2D
rt.synchronize()                                       # 等搬运完成
data.clear(); data.extend(b"\x00" * 72)
rt.copy_data_from_device(host_ptr, dev_mem, len(data)) # 回拷：D2H
rt.synchronize()
print("roundtrip ok:", bytes(data) == b"pyasc-rt-roundtrip" * 4)
rt.free(dev_mem)                                       # 释放：按 state.allocs 里的基地址

# 3) 状态自检
print("device_id:", state.device_id, "| streams:", len(state.streams),
      "| allocs left:", len(state.allocs), "| kernels:", len(state.kernels))
with open("/proc/self/maps") as f:
    libs = {line.split()[-1] for line in f
            if "rt_wrapper" in line or "camodel" in line or "npu_utils" in line}
print("loaded libs:", *sorted(libs), sep="\n  ")
```

**需要观察的现象与预期结果**：

1. 第 1 步三个查询一次性触发 `_lazy_init` 的完整三级（库、设备、流），随后 `state.device_id == 0`。
2. 第 2 步 `roundtrip ok: True`——数据经 H2D → D2H 一个来回后逐字节还原，证明 `malloc` 的 512 对齐、`memcpy` 的方向参数、`free` 的基地址反查全部正确协作。
3. 第 3 步 `allocs left: 0`，说明 `free` 正确从 `state.allocs` 删除了登记项；`kernels` 为 0（没注册任何 kernel）。
4. `loaded libs` 列表含缓存目录下的 `librt_wrapper*.so` 与 CANN simulator 目录下的 `libruntime_camodel.so`，不含 `libnpu_utils*`。
5. 如有 NPU 环境，把 `Backend.Model` 换成 `Backend.NPU` 再跑：查询值变为真机参数，`loaded libs` 换成 `libruntime.so` / `libascendcl.so`。

本环境无 NPU 且未安装 CANN，以上第 2、4 点的具体输出属「待本地验证」；但每一步调用的接口与行号均已对照源码核实。

## 6. 本讲小结

- `interface.py` 是 aclrt 的平铺封装，所有函数入口处的 `_lazy_init` 按「加载库 → 绑定设备 → 创建流」三级惰性初始化，每级判据独立，可按需只走部分台阶。
- `state.py` 用模块级变量充当全局状态板（设备、流表、kernel 表、内存表），并内置两个加载器：`RuntimeInterface` 把随包 `rt_wrapper.cpp` 编成 `extern "C"` 库供 `ctypes.CDLL` 调用，`NPUUtils` 把 `npu_utils.cpp` 编成 CPython 扩展模块（Model 模式下为空壳单例）。
- Model/NPU 模式的本质是「同一个 wrapper 源码，链接不同的运行时库」：Model 链 `libruntime_camodel`（仿真器，无需硬件），NPU 链 `libruntime`/`libascendcl`/`libmsprofiler`；`config.set_platform` 负责选模式、校验芯片型号（NPU 下以设备真值为准）并做可用性检查。
- `build_utils.build_npu_ext` 在目标机上在线编译随包 cpp（选编译器 → Python 头文件 → 两代 CANN 头文件布局适配 → 按模式链接），产物以 `sha256(cpp全文 + version.cfg + 模式)` 为 key 存入 `FileCacheManager`，跨 CANN 版本、Python 版本、模式切换天然隔离。
- 上一讲的 `launch_kernel` 参数 blob、`register_device_binary_kernel` 的魔数、`MemoryHandle` 的「上设备-回拷-释放」，在本讲全部落地为具体源码：blob 在 `KernelLaunchWrapper` 处按 `num_args*8` 字节下发，核数查询就是 `rt.device_info(AICORE, INFO_TYPE_CORE_NUM)`。

## 7. 下一步学习建议

- **u3-l8（JIT 缓存机制）**：本讲的 `FileCacheManager` 只是 kernel 缓存体系的冰山一角，下一讲讲 `cache_factors`、`pyasc_key` 与两级缓存的完整设计。
- **`python/asc/lib/runtime/print_utils.py` 与 `print_utils.cpp`**：设备侧 printf/dump 的实现，同样是随包 cpp + 在线编译套路，学完本讲可直接读。
- **`python/asc/lib/host/loader.py`**：Host 侧（tiling 计算）库的加载走的是另一套代理机制，与本讲对照阅读能加深对「随包源码 + 动态加载」两种形态的理解（u7-l3 展开）。
- **u7-l4（调试与调优）**：本讲出现的 `msprof_task_type`、`set_pro_switch`、`npu_utils.msprof_report_api` 会在 profiling 主题下完整闭环。
- 动手方向：尝试给 `interface.py` 风格写一个只读小工具（例如批量打印 `DeviceModuleType × DeviceInfoType` 查询矩阵），体会「ctypes 缓冲区 → call → 读 .value」这套固定模式。
