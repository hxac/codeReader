# Launcher：Kernel 参数打包与任务下发

## 1. 本讲目标

上一讲（u3-l5）结束时，`Compiler` 已经交出一个 `CompiledKernel`：里面有 Kernel 二进制（`binary`）、核类型（`core_type`）和参数 ABI 表（`kernel_args`）。本讲沿着主链路的最后一段往下走，读完 `runtime/launcher.py` 与 `runtime/memory_handle.py`，你应当掌握：

1. `kernel[核数, 流](...)` 里的中括号选项如何变成 `LaunchOptions`，并如何决定下发行为。
2. `expand_kernel_args` 如何把用户实参（Python int/float/bool、numpy 标量、Struct、张量）规整成两类对象：定宽标量与 `MemoryHandle`。
3. `launch_kernel` 如何把所有参数序列化成一条**按 8 字节对齐的连续字节流**（参数 blob），再切成 `uint64` 字数组交给运行时。
4. `MemoryHandle` 体系如何在 Host 与 Device 之间自动搬运数据、执行后回拷结果并释放显存。
5. `enable_debug` 时为什么会在参数尾部追加一块 75 MB 的 dump 缓冲，以及 msprof 打点的注入位置。

## 2. 前置知识

- **Host 侧与 Device 侧**：Host 指 CPU 侧的 Python 进程，Device 指 NPU（或 Model 仿真器模拟的 NPU）。Kernel 在 Device 上执行，参数在 Host 上准备，中间必须有一次「打包 → 拷贝 → 下发」的过程。
- **ABI（Application Binary Interface）**：调用双方对「参数放在哪、占几个字节、按什么顺序排」的约定。Device 侧的 Kernel 入口函数不知道 Python 对象，它只能从一块连续内存里按固定偏移读取参数，所以 Host 侧必须严格按同一套布局拼装这块内存。
- **小端序（little-endian）**：多字节整数在内存中低位字节在前。`numpy` 的 `tobytes()` 与 `int.from_bytes(x, "little")` 都按本机（x86/ARM 均为小端）处理。
- **`rt`（acl runtime 封装）**：`asc.lib.runtime` 包用 ctypes 把 aclruntime 动态库包成 Python 函数（下一讲 u3-l7 精读）。本讲只需把它当作一组设备操作原语：注册二进制、下发 Kernel、拷贝内存、同步。
- 承接 u3-l3 的结论：kernel 参数 ABI 只由**定宽标量**与**8 字节指针**拼成；承接 u3-l4/u3-l5：`CompiledKernel.kernel_args` 记录每个参数的类别（Explicit / FftsAddr），由 `LegalizeKernelArgs` 等 Pass 生成。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [python/asc/runtime/launcher.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/launcher.py) | 本讲主角：`LaunchOptions`、`Launcher.expand_kernel_args` / `launch_kernel` / `run`、`MsprofLauncher` |
| [python/asc/runtime/memory_handle.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/memory_handle.py) | `MemoryHandle` 抽象与四个实现（bytes / ndarray / CPU 张量 / NPU 张量），`resolve_memory_handle` 工厂 |
| [python/asc/runtime/utils.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/utils.py) | `TOTAL_DUMP_SIZE` 等 debug dump 尺寸常量与文件工具 |
| [python/asc/runtime/jit.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py) | 调用方：`__getitem__` 构造 `LaunchOptions`，`_run_launcher` 实例化 `Launcher` |
| [python/asc/runtime/compiler.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py) | `CompiledKernel` 产物信封的定义（本讲的输入） |
| [python/asc/lib/runtime/interface.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/runtime/interface.py) | 底层原语：`register_device_binary_kernel`、`launch_kernel`、`copy_data_to_device` 等 |
| [python/asc/lib/runtime/print_utils.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/runtime/print_utils.py) | debug dump 缓冲的解析打印（`call_print_interface`） |
| [examples/01_add/add.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/01_add/add.py) | 实践使用的端到端示例 |

## 4. 核心概念与源码讲解

### 4.1 LaunchOptions：只属于「这一次下发」的选项

#### 4.1.1 概念说明

pyasc 把选项分成三个选项袋（u1-l5）：`CodegenOptions`、`CompileOptions` 影响代码生成、参与缓存 key；而 `LaunchOptions` 只描述**怎么下发**——用多少个核、发到哪条流。它来自中括号语法 `kernel[核数, 流]`，同一个编译产物可以换不同的核数与流反复下发，因此它**不参与缓存 key**。

#### 4.1.2 核心流程

```text
kernel[8](...)            ->  __getitem__(8)     -> LaunchOptions(core_num=8)
kernel[8, stream](...)    ->  __getitem__((8, s))-> LaunchOptions(core_num=8, stream=s)   # 按位置解包
_run(...)                 ->  self.launcher(launch_options) -> Launcher.__init__ 暂存选项
```

两个边界行为：

- `stream` 缺省为 `None`，下发时兜底取 `rt.current_stream()`。
- `core_num <= 0` 时在 `run()` 里直接抛 `ValueError`。

#### 4.1.3 源码精读

`LaunchOptions` 是一个冻结 dataclass，只有两个字段：

```python
@dataclass(frozen=True)
class LaunchOptions:
    core_num: int = 0
    stream: Optional[rt.Stream] = None
```

见 [python/asc/runtime/launcher.py:L48-L51](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/launcher.py#L48-L51)，这段代码定义了下发选项袋：核数与流，均不影响已编译的二进制内容。

中括号语法在 `JITFunction.__getitem__` 里解析，整数按 `core_num`、元组按位置展开：

```python
if isinstance(user_launch_options, int):
    self.launch_options = LaunchOptions(core_num=user_launch_options)
else:
    self.launch_options = LaunchOptions(*user_launch_options)
```

见 [python/asc/runtime/jit.py:L48-L57](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L48-L57)，这段代码把 `kernel[8]` / `kernel[8, stream]` 两种写法统一成 `LaunchOptions` 并返回绑定的 `_run`。

`JITFunction` 用类属性 `launcher: Type[Launcher] = Launcher` 组合下发器（[python/asc/runtime/jit.py:L33](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L33)），真正实例化发生在 `_run_launcher`：

```python
def _run_launcher(self, kernel: CompiledKernel, options: LaunchOptions, runtime_args: Tuple[Any]) -> None:
    launcher = self.launcher(options)
    launcher.run(kernel, self.fn.__name__, runtime_args)
```

见 [python/asc/runtime/jit.py:L200-L202](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L200-L202)，这段代码在编译命中后以函数名为 kernel 名、以运行时参数元组调用 `Launcher.run`。

`Launcher.__init__` 暂存选项并按当前模式创建 msprof 打点器；`get_core_num` 是查询设备 AI Core 核数的静态工具（Model 模式下由仿真器回答）：

```python
def __init__(self, options: LaunchOptions):
    self.options = options
    self.msprof = MsprofLauncher(rt.is_model())

@staticmethod
def get_core_num(device_id: Optional[int] = None) -> int:
    return rt.device_info(rt.DeviceModuleType.RT_MODULE_TYPE_AICORE, rt.DeviceInfoType.INFO_TYPE_CORE_NUM,
                          device_id)
```

见 [python/asc/runtime/launcher.py:L54-L63](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/launcher.py#L54-L63)，这段代码完成下发器初始化并提供「设备一共有多少核」的查询入口。

#### 4.1.4 代码实践

1. **实践目标**：确认中括号语法只影响 `LaunchOptions`，不影响缓存。
2. **操作步骤**：在 01_add 示例的 `vadd_launch` 中把 `USE_CORE_NUM` 分别改为 4 与 16（保持能整除 `size`），连续运行两次；再在脚本开头加一行打印（示例代码）：

   ```python
   # 示例代码：打印 launch_options 的内容
   print(vadd_kernel.launch_options)
   ```

3. **需要观察的现象**：第二次运行命中缓存（无重编译日志），输出张量仍满足 `allclose`；打印形如 `LaunchOptions(core_num=16, stream=<Stream ...>)`。
4. **预期结果**：核数变化不触发重新编译——因为 `LaunchOptions` 不进缓存 key，核数只是下发给运行时的 `block_num`。
5. Model 模式下 `get_core_num` 的返回值取决于仿真器配置，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：`kernel[8]` 与 `kernel[(8, rt.current_stream())]` 生成的 `LaunchOptions` 有何区别？
**答案**：前者走 int 分支得到 `LaunchOptions(core_num=8, stream=None)`，后者按元组位置解包得到 `LaunchOptions(core_num=8, stream=<当前流>)`；stream 为 None 时 `launch_kernel` 内部会用 `rt.current_stream()` 兜底，两者最终行为等价。

**练习 2**：把 `core_num` 误设为 0 会发生什么？在哪一行被发现？
**答案**：`Launcher.run` 中 `if self.options.core_num <= 0: raise ValueError(...)`（[python/asc/runtime/launcher.py:L148-L149](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/launcher.py#L148-L149)）。注意它发生在二进制已注册之后、下发之前——即编译与注册都已完成，只是拒绝下发。

**练习 3**：为什么 `LaunchOptions` 不参与缓存 key？
**答案**：核数与流只改变「这次怎么执行」，不改变生成的 Kernel 二进制；同一份 `CompiledKernel` 可用不同核数/流多次下发，把它们混入缓存 key 会导致无意义的重编译。

### 4.2 expand_kernel_args：用户实参的类型规整

#### 4.2.1 概念说明

用户传给 kernel 的是普通 Python 对象（`int`、`float`、`bool`、numpy 标量、`Struct`、torch 张量），而打包层只认识两种形态：

- **定宽标量**（`np.generic`）：按 `tobytes()` 直接进 blob；
- **设备指针**（`MemoryHandle`）：先 `copy_to_device()` 拿到设备地址，再把 8 字节地址放进 blob。

`expand_kernel_args` 就是这趟「规整列车」：把杂七杂八的 Python 值统一成上面两类。

#### 4.2.2 核心流程

```text
for arg in args:
    int          -> np.int32(arg)          # 定宽标量
    float        -> np.float32(arg)        # 定宽标量
    bool         -> np.int8(int(arg))      # 定宽标量（注意：见 4.2.3 的顺序问题）
    np.generic   -> 原样通过
    Struct       -> arg.pack() 得 bytes -> resolve_memory_handle -> ByteArrayHandle（指针）
    其他（ndarray / torch.Tensor / bytes）
                 -> resolve_memory_handle(arg)（指针）
```

注意这里的宽度约定与 u3-l3 的 `get_arg_type` 一致：Python `int` 一律按 int32、`float` 按 float32 传给设备，需要 int64 时请显式传 `np.int64(x)`（走 `np.generic` 分支原样通过）。

#### 4.2.3 源码精读

```python
@staticmethod
def expand_kernel_args(args: Iterable[Any]) -> List[Union[np.generic, MemoryHandle]]:
    kernel_args = []
    for arg in args:
        if isinstance(arg, int):
            kernel_args.append(np.int32(arg))
        elif isinstance(arg, float):
            kernel_args.append(np.float32(arg))
        elif isinstance(arg, bool):
            kernel_args.append(np.int8(int(arg)))
        elif isinstance(arg, np.generic):
            kernel_args.append(arg)
        elif isinstance(arg, Struct):
            kernel_args.append(resolve_memory_handle(arg.pack()))
        else:
            kernel_args.append(resolve_memory_handle(arg))
    return kernel_args
```

见 [python/asc/runtime/launcher.py:L65-L81](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/launcher.py#L65-L81)，这段代码把每个实参归类为定宽 numpy 标量或 `MemoryHandle`，是 Host/Device ABI 的第一道转换。

**一个值得注意的阅读细节**：Python 中 `bool` 是 `int` 的子类，而这里 `isinstance(arg, int)` 写在 `bool` 之前，所以 `True`/`False` 实际命中 **int 分支**被规整为 `np.int32`，L73-L74 的 bool 分支按代码顺序不可达。对比 [python/asc/runtime/jit.py:L67-L73](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L67-L73) 的 `get_arg_type`，那里是**先判 bool**（映射为 int8 类型的 `PlainArgType`）。两者顺序不同却仍兼容：bool 参数在设备侧占 1 字节槽位（blob 里补齐到 4 字节，见 4.3），而小端序下 `np.int32(True)` 的最低字节恰好是 1、其余为 0，设备读到 int8 时取到的值正确。

`Struct` 参数走 `pack()`：把 ctypes 结构体实例按内存布局导出为 `bytes`（[python/asc/language/core/struct.py:L237-L240](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/struct.py#L237-L240)），随后作为一块设备内存整体拷下去，kernel 内再用 `create_local` 拷到 UB（承接 u3-l3 的 Struct「三面体」）。

`resolve_memory_handle` 的分发规则在 4.4 详解。

#### 4.2.4 代码实践

1. **实践目标**：直观看到规整结果的两类形态。
2. **操作步骤**：安装好 pyasc 后在 Python 交互环境执行（示例代码）：

   ```python
   # 示例代码：直接调用静态方法观察类型规整
   import numpy as np
   from asc.runtime.launcher import Launcher
   from asc.runtime.memory_handle import MemoryHandle, ByteArrayHandle, NumpyArrayHandle

   out = Launcher.expand_kernel_args([3, 2.5, True, np.int64(7), b"\x01\x02"])
   for x in out:
       print(type(x).__name__, "->", x if isinstance(x, np.generic) else "handle")
   ```

3. **需要观察的现象**：前四项依次是 `int32 / float32 / int32 / int64` 四个 numpy 标量（印证 4.2.3 的 bool 顺序问题），最后一项是 `ByteArrayHandle`。
4. **预期结果**：输出列表只含 `np.generic` 与 `MemoryHandle` 两类实例。
5. 本片段不触设备，无 NPU 也可运行；若导入路径报错请确认 `pip3 list | grep pyasc`，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：给 kernel 传一个 `str` 参数会在哪里、以什么方式失败？
**答案**：更早一步就失败了——`JITFunction.get_arg_type` 对不支持的类型抛 `TypeError`（[python/asc/runtime/jit.py:L94](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L94)）。即便有漏网之鱼到达打包层，`resolve_memory_handle` 也会抛 `RuntimeError: Unsupported memory handle`（[python/asc/runtime/memory_handle.py:L125](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/memory_handle.py#L125)），两道防线互补。

**练习 2**：`np.float16(1.5)` 标量在 blob 里占几个字节？
**答案**：`itemsize=2`，`tobytes()` 产出 2 字节，随后因 `itemsize < 4` 追加 `4-2=2` 字节零填充，槽位共 4 字节（规则见 [python/asc/runtime/launcher.py:L93-L97](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/launcher.py#L93-L97)）。

**练习 3**：为什么需要传 64 位整数时必须写 `np.int64(x)` 而不能直接写 `x`？
**答案**：Python `int` 在 `expand_kernel_args` 里被无条件规整为 `np.int32`（L69-L70），高位会被截断；`np.int64(x)` 命中 `np.generic` 分支原样通过，占据 8 字节槽位。同时要注意参数的 IR 类型标注也应匹配（u3-l3 的 `PlainArgType`）。

### 4.3 launch_kernel 与 run：拼参数 blob、注册并下发

#### 4.3.1 概念说明

这是本讲的核心：**Kernel 参数 ABI 的具体布局**。设备侧的 kernel 入口从一块连续内存里读参数，这块内存（称为 input blob）的拼装规则是：

1. 每个定宽标量占 4 或 8 字节：`itemsize < 4` 补零到 4；`4 < itemsize < 8` 补到 8；`itemsize` 为 4 或 8 的不动。
2. 每个指针参数前，若当前总字节数不是 8 的倍数，插入 4 字节零（因为所有标量槽都是 4 的倍数，失配只可能是 `mod 8 == 4`，补一个 4 字节零字即可），然后放入 8 字节的设备地址。
3. 末尾把总长补齐到 8 的倍数，整体按 8 字节切成若干 `uint64` 字（小端），交给 `rt.launch_kernel`。

`run()` 则是编排者：补齐隐藏参数、规整类型、注册二进制、校验核数、调用 `launch_kernel`、收尾释放。

#### 4.3.2 核心流程

`Launcher.run` 的全流程：

```text
run(kernel, function_name, user_args):
    DRY_RUN 环境变量存在?  -> 直接 return（只编译不下发）
    按 kernel.kernel_args 逐项取参:
        Explicit  -> next(user_args)                 # 用户实参
        FftsAddr  -> np.array([rt.c2c_ctrl_addr()])  # 运行时自动注入
    enable_debug? -> 追加 np.zeros(TOTAL_DUMP_SIZE, int8)   # 75 MB dump 缓冲
    expand_kernel_args(...)                          # 4.2 的规整
    register_device_binary_kernel(binary, magic_elf_value(core_type))
    register_function(kernel_handle, function_name, mode=0)
    core_num <= 0 ? -> ValueError
    launch_kernel(function, kernel_args, enable_debug, name, core_type)
    rt.free_mem()                                    # 注销二进制并释放缓存分配
```

`launch_kernel` 内部：

```text
拼 input_blobs（规则见上）-> 切 8 字节字 -> inputs: List[c_uint64]
stream = options.stream 或 current_stream()
msprof.start()                  # 记起始 cycle（NPU 模式）
rt.launch_kernel(fn, core_num, inputs, stream)
msprof.process(...)             # 上报打点（NPU 模式）
rt.synchronize()                # 等 Kernel 执行完
for arg in memory_args:
    enable_debug 且是最后一个? -> call_print_interface(inputs[-1], TOTAL_DUMP_SIZE, ...)
    否则                       -> arg.copy_from_device()   # 回拷输出
    finally: arg.release_memory()
```

#### 4.3.3 源码精读

先看 blob 拼装与切分：

```python
input_blobs: List[bytes] = []
memory_args: List[MemoryHandle] = []
for arg in kernel_args:
    if isinstance(arg, np.generic):
        input_blobs.append(arg.tobytes())
        if arg.itemsize < 4:
            input_blobs.append(b"\0" * (4 - arg.itemsize))
        elif arg.itemsize > 4 and arg.itemsize < 8:
            input_blobs.append(b"\0" * (8 - arg.itemsize))
    elif isinstance(arg, MemoryHandle):
        if blobs_size(input_blobs) % 8 != 0:
            input_blobs.append(b"\0" * 4)
        handle = arg.copy_to_device()
        input_blobs.append(np.uint64(handle).tobytes())
        memory_args.append(arg)
```

见 [python/asc/runtime/launcher.py:L89-L103](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/launcher.py#L89-L103)，这段代码实现参数 ABI：标量补到 4/8 字节、指针前对齐到 8 字节、调用 `copy_to_device` 把数据搬上设备并把返回的设备地址以小端 `uint64` 写入 blob。

一个小阅读提示：内嵌的 `blobs_size` 定义为

```python
def blobs_size(inputs: List[bytes]) -> int:
    return sum(len(x) for x in input_blobs)
```

见 [python/asc/runtime/launcher.py:L86-L87](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/launcher.py#L86-L87)——形参 `inputs` 并未被使用，实际统计的是闭包里的 `input_blobs`。读源码时不要被参数名迷惑，语义是「当前已拼 blob 的总字节数」。

接着是补齐、切字与下发：

```python
aligned_len = int(np.ceil(blobs_size(input_blobs) / 8)) * 8
combined_inputs = bytes().join(input_blobs).ljust(aligned_len, b"\0")
chunks = [combined_inputs[i:i + 8] for i in range(0, len(combined_inputs), 8)]
inputs = [ctypes.c_uint64(int.from_bytes(x, "little")) for x in chunks]

stream = self.options.stream or rt.current_stream()

self.msprof.start()
rt.launch_kernel(function, self.options.core_num, inputs, stream_handle=stream)
self.msprof.process(func_name, self.options.core_num, rt.msprof_task_type(core_type))

rt.synchronize()
```

见 [python/asc/runtime/launcher.py:L106-L117](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/launcher.py#L106-L117)，这段代码把 blob 补齐到 8 的倍数、切成 `uint64` 字数组，经 `rt.launch_kernel` 下发，随后 msprof 打点并同步等待 Kernel 完成。

底层 `rt.launch_kernel` 把字数组变成 C 数组，连同「字节数 = 参数个数 × 8」一并传给 aclruntime 的 `KernelLaunchWrapper`：

```python
args_arr = (ctypes.c_uint64 * num_args)(*(arg.value if isinstance(arg, ctypes.c_void_p) else arg for arg in args))
state.lib.call(
    "KernelLaunchWrapper",
    fn_handle,
    ctypes.c_uint32(block_num),
    ctypes.c_void_p(ctypes.addressof(args_arr)),
    ctypes.c_uint32(num_args * 8),
    ...
)
```

见 [python/asc/lib/runtime/interface.py:L318-L334](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/runtime/interface.py#L318-L334)，这段代码揭示了 8 字节约定的来源之一：**运行时接口本身就把参数区当作 `uint64` 字数组传递**，每个参数槽天然按 8 字节粒度对齐。

执行完之后的回拷与释放：

```python
for index, arg in enumerate(memory_args):
    try:
        if enable_debug and index == len(memory_args) - 1:
            rt.call_print_interface(inputs[-1], utils.TOTAL_DUMP_SIZE, stream, func_name)
        else:
            arg.copy_from_device()
    finally:
        arg.release_memory()
```

见 [python/asc/runtime/launcher.py:L118-L125](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/launcher.py#L118-L125)，这段代码在同步之后逐个处理指针参数：普通参数回拷 Host，最后一个参数若是 debug dump 缓冲则改走打印接口；无论走哪条路都释放设备内存。注意 `inputs[-1]` 恰好是 dump 缓冲的设备地址字——因为该缓冲被追加在参数表最末尾。

再看 `run` 的隐藏参数与 debug 注入：

```python
dry_run = os.environ.get('DRY_RUN')
if dry_run:
    return
...
for kind in kernel.kernel_args:
    if kind == ir.KernelArgument.Explicit:
        kernel_args.append(next(explicit_arg))
    elif kind == ir.KernelArgument.FftsAddr:
        ffts_addr = np.array([rt.c2c_ctrl_addr()], dtype=np.uint64)
        kernel_args.append(ffts_addr)
if kernel.enable_debug:
    kernel_args.append(np.zeros(utils.TOTAL_DUMP_SIZE, dtype=np.int8))
kernel_args = self.expand_kernel_args(tuple(kernel_args))
kernel_handle = rt.register_device_binary_kernel(kernel.binary, rt.magic_elf_value(kernel.core_type))
function = rt.register_function(kernel_handle, function_name, mode=0)
```

见 [python/asc/runtime/launcher.py:L127-L147](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/launcher.py#L127-L147)，这段代码做了四件事：DRY_RUN 短路（[L128-L130](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/launcher.py#L128-L130)）、按 `kernel_args` 类别表装配参数（用户实参按 `Explicit` 顺序消费，`FftsAddr` 由运行时注入 Cube 核 FFTS 控制区地址，其具体硬件语义**待确认**）、追加 debug dump 缓冲、注册二进制与函数入口。

dump 缓冲的大小来自 [python/asc/runtime/utils.py:L13-L14](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/utils.py#L13-L14)：单核 1 MiB、共 75 核即约 75 MiB 的 int8 数组——它是一个 ndarray，经 `expand_kernel_args` 变成 `NumpyArrayHandle` 整块拷上设备；设备侧的 `AscendC::InitDump` 调用由 `Compiler` 在 `enable_debug` 时注入 ascendc.cpp（[python/asc/runtime/compiler.py:L169-L170](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L169-L170)，`enable_debug` 本身由 IR 上的 `asc.enable_debug` 属性加 `ASCENDC_DUMP` 环境变量决定，[python/asc/runtime/compiler.py:L190-L191](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L190-L191)）。执行结束后 `call_print_interface` 按需编译/加载 `PrintWorkSpace` 动态库来解析这块缓冲（[python/asc/lib/runtime/print_utils.py:L86-L89](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/runtime/print_utils.py#L86-L89)）。

msprof 打点器 `MsprofLauncher` 在 Model 模式下全部短路，NPU 模式下记录 cycle 时间并上报算子名、block 数与任务类型（[python/asc/runtime/launcher.py:L24-L45](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/launcher.py#L24-L45)）；任务类型由 `CoreType` 映射（[python/asc/lib/runtime/interface.py:L96-L106](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/runtime/interface.py#L96-L106)）。

最后 `rt.free_mem()`（[python/asc/runtime/launcher.py:L151](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/launcher.py#L151)）会注销**所有**已注册二进制并释放缓存的设备分配（[python/asc/lib/runtime/interface.py:L166-L171](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/runtime/interface.py#L166-L171)）——这意味着每次 `run` 都会重新走一遍注册流程，是一个值得注意的实现现状。

#### 4.3.4 代码实践

1. **实践目标**：体验「只编译、不下发」的调试开关。
2. **操作步骤**：`DRY_RUN=1 python3 examples/01_add/add.py -r Model`。
3. **需要观察的现象**：程序正常退出、无执行日志；配合 `PYASC_DUMP_PATH` 可拿到全部四级产物。
4. **预期结果**：断言 `allclose` 不会被触发（因为没有真正执行，z 保持全零）——所以 DRY_RUN 只用于检查编译链路，不能验证数值；完整行为**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `copy_from_device` 必须在 `rt.synchronize()` 之后？
**答案**：Kernel 在设备上是异步执行的，`rt.launch_kernel` 返回只代表任务入队。若不同步就回拷，设备可能尚未把结果写入输出缓冲，Host 会读到旧值。`synchronize`（[python/asc/lib/runtime/interface.py:L337](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/runtime/interface.py#L337)）保证流上任务全部完成后才开始回拷。

**练习 2**：把 01_add 的 `block_length` 挪到第一个参数位（即 `(block_length, x, y, z)`），blob 布局怎么变？
**答案**：int32 占第 0~3 字节；随后是指针，当前总长 4 不是 8 的倍数，插入 4 字节零；三个指针依次落在第 8、16、24 字节，总长 32。布局从「3 指针 + 1 标量」的 `P P P S` 变成 `S _ P P P`，总字节数不变，但每个指针的偏移都移了 8 字节——参数顺序是 ABI 的一部分。

**练习 3**：`FftsAddr` 参数需要用户传值吗？
**答案**：不需要。`run()` 遇到 `ir.KernelArgument.FftsAddr` 时自动追加 `rt.c2c_ctrl_addr()`（[python/asc/runtime/launcher.py:L138-L140](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/launcher.py#L138-L140)）；用户的实参只按 `Explicit` 项的顺序被消费。

### 4.4 MemoryHandle：Host/Device 之间的数据搬运与生命周期

#### 4.4.1 概念说明

所有「块状数据」参数（numpy 数组、bytes、CPU/NPU 上的 torch 张量、Struct 打包产物）都不直接进 blob——进 blob 的只是它们的**设备地址**。`MemoryHandle` 抽象了「把数据送上设备、执行完取回来、最后释放」的三段生命周期，不同来源的数据用不同实现：

| 实现 | 数据来源 | `copy_to_device` | `copy_from_device` | `release_memory` |
| --- | --- | --- | --- | --- |
| `ByteArrayHandle` | bytes / bytearray（含 Struct.pack） | 拷贝上设备 | 回拷 | 释放 |
| `NumpyArrayHandle` | numpy ndarray | C 序展平后拷贝 | 回拷 | 释放 |
| `TorchCpuTensorHandle` | CPU 上的 torch 张量 | 展平后拷贝 | 回拷 | 释放 |
| `TorchNpuTensorArgument` | NPU 上的 torch 张量 | 直接返回 `data_ptr()`（零拷贝） | 空操作 | 空操作 |

#### 4.4.2 核心流程

```text
resolve_memory_handle(obj):
    已是 MemoryHandle -> 原样返回
    bytes/bytearray    -> ByteArrayHandle
    numpy.ndarray      -> NumpyArrayHandle
    torch.Tensor       -> CPU 张量: TorchCpuTensorHandle；NPU 张量: TorchNpuTensorArgument
    其他               -> RuntimeError
```

设备侧地址的取得依赖 aclrt 原语：`copy_data_to_device` = `malloc`（HBM，>2048 字节走大页策略）+ `memcpy(HOST_TO_DEVICE)`（[python/asc/lib/runtime/interface.py:L303-L309](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/runtime/interface.py#L303-L309)）；`malloc` 内部把返回地址向上对齐到 512 字节（[python/asc/lib/runtime/interface.py:L269-L285](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/runtime/interface.py#L269-L285)）——所以设备地址天然满足 8 字节对齐，blob 里的指针槽只要对齐了，取出的就是可用的指针。

#### 4.4.3 源码精读

抽象基类约定三段生命周期：

```python
class MemoryHandle(abc.ABC):
    @abc.abstractmethod
    def copy_to_device(self) -> int: ...
    @abc.abstractmethod
    def copy_from_device(self) -> None: ...
    @abc.abstractmethod
    def release_memory(self) -> None: ...
```

见 [python/asc/runtime/memory_handle.py:L28-L40](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/memory_handle.py#L28-L40)，这段代码定义了「上设备 → 回 Host → 释放」协议，`launch_kernel` 正是按这个顺序调用三者。

numpy 数组实现的细节——先展平再拷贝：

```python
def copy_to_device(self) -> int:
    flat = self.array.ravel(order="C")
    self.handle = rt.copy_data_to_device(flat.ctypes.data_as(ctypes.c_void_p), flat.nbytes)
    return int(self.handle.value)
```

见 [python/asc/runtime/memory_handle.py:L60-L68](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/memory_handle.py#L60-L68)，这段代码把多维数组按 C 序（行优先）展平成线性缓冲再上设备——设备侧拿到的总是一段连续内存，多维形状信息不随数据下去（这正是 u2-l2 中 `ShapeInfo` 要进 IR 的原因）。

NPU 张量的零拷贝实现：

```python
class TorchNpuTensorArgument(MemoryHandle):
    def copy_to_device(self) -> int:
        return self.tensor.data_ptr()
    def copy_from_device(self) -> None:
        pass
    def release_memory(self) -> None:
        pass
```

见 [python/asc/runtime/memory_handle.py:L94-L106](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/memory_handle.py#L94-L106)，这段代码对已在 NPU 上的张量不做任何搬运：直接把张量的设备地址写进 blob，Kernel 写入的结果也直接落在原张量里，因此回拷与释放都是空操作。对比 `TorchCpuTensorHandle`（[L77-L91](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/memory_handle.py#L77-L91)）每个方向都要一次真实拷贝。

工厂函数按类型分发，torch 缺失时静默跳过：

```python
def resolve_memory_handle(obj) -> MemoryHandle:
    if isinstance(obj, MemoryHandle):
        return obj
    if isinstance(obj, (bytes, bytearray)):
        return ByteArrayHandle(obj)
    if isinstance(obj, numpy.ndarray):
        return NumpyArrayHandle(obj)
    try:
        import torch
        if isinstance(obj, torch.Tensor):
            if getattr(obj, "is_cpu", False):
                return TorchCpuTensorHandle(obj)
            if getattr(obj, "is_npu", False):
                return TorchNpuTensorArgument(obj)
    except ModuleNotFoundError:
        pass
    raise RuntimeError(f"Unsupported memory handle of type {obj.__class__.__name__}")
```

见 [python/asc/runtime/memory_handle.py:L109-L125](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/memory_handle.py#L109-L125)，这段代码是块状数据参数的统一入口，也解释了 01_add 在两种后端下的行为差异：`device="cpu"`（Model 模式）时走 `TorchCpuTensorHandle` 真实往返拷贝，`device="npu"` 时走零拷贝代理。

#### 4.4.4 代码实践

1. **实践目标**：观察 01_add 在 Model 模式下输出张量如何「回家」。
2. **操作步骤**：阅读 [examples/01_add/add.py:L72-L79](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/01_add/add.py#L72-L79)，注意 `z = torch.zeros_like(x)`（CPU 上）被直接传给 kernel；在 Model 模式运行示例。然后回答：`assert torch.allclose(z, x + y)` 之所以能通过，`z` 的数据经历了哪几步？
3. **需要观察的现象**：断言通过。
4. **预期结果**：`z` 在 `expand_kernel_args` 处变成 `TorchCpuTensorHandle`；`copy_to_device` 把零值拷上设备（blob 里记下设备地址）；Kernel 写入结果；`synchronize` 后 `copy_from_device` 把结果拷回 `z` 的 Host 内存；`release_memory` 释放设备缓冲。四步缺一不可。
5. 若在 NPU 模式运行（需真机），`z` 走零拷贝路径，断言同样成立，**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`TorchNpuTensorArgument.copy_from_device` 为什么是空操作？
**答案**：张量本身就在设备内存里，Kernel 通过 blob 里的 `data_ptr()` 直接读写这块内存；下发任务同步完成后结果已经「在原地」，无需也没有必要再拷回 Host。

**练习 2**：`ByteArrayHandle` 服务于哪类参数？它的数据在设备侧如何变成可用结构？
**答案**：服务于 `bytes`/`bytearray`，典型是 `Struct.pack()` 的产物（[python/asc/runtime/launcher.py:L77-L78](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/launcher.py#L77-L78)）。结构体整体作为一块设备内存传给 kernel，kernel 内部用 `create_local`（生成 `CopyStructOp`，[python/asc/language/core/struct.py:L242-L245](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/struct.py#L242-L245)）把它从 GM 拷到本地再读成员。

**练习 3**：为什么 `resolve_memory_handle` 里 torch 导入失败只是 `pass` 而不是报错？
**答案**：pyasc 不强制依赖 torch——只装 numpy 的用户也能用 ndarray 传参。只有当「传入对象既不是已知类型、又恰好需要 torch 判定」时，才落到末尾的 `RuntimeError` 给出明确报错。

## 5. 综合实践

**任务**：手算一个参数顺序为 `(int32, float32, torch.Tensor 指针)` 的 kernel 在 `launch_kernel` 中生成的 `input_blobs` 布局与补齐字节数，再用打印验证，最后解释「为什么指针参数前必须对齐到 8 字节」。

### 第一步：手算

设实参为 `n=16`（Python int）、`alpha=2.5`（Python float）、`t`（torch 张量）。对照 [python/asc/runtime/launcher.py:L89-L109](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/launcher.py#L89-L109) 逐步推演：

| 步骤 | 参数 | 规整结果 | 写入 blob | 累计字节 |
| --- | --- | --- | --- | --- |
| 1 | `16` | `np.int32`，itemsize=4 | 4 字节（`10 00 00 00`） | 4 |
| 2 | `2.5` | `np.float32`，itemsize=4 | 4 字节（`00 00 20 40`） | 8 |
| 3 | `t` | `MemoryHandle` | 8 % 8 == 0，**无需插入对齐零**；`copy_to_device()` 得设备地址，写 8 字节小端 uint64 | 16 |
| 4 | 尾部补齐 | 16 已是 8 的倍数 | 补 0 字节 | 16 |

结论：**共 16 字节、切 2 个 uint64 字、全程补齐 0 字节**。`word[0]` 的低 4 字节是 int32 的 `16`、高 4 字节是 float32 的 `2.5`（即 `0x4020000000000010`），`word[1]` 是张量的设备地址。

### 第二步：验证

方式 A——离线复算（无设备也能做，示例代码）：

```python
# 示例代码：独立复现 launch_kernel 的打包规则（不依赖 pyasc 运行环境）
import numpy as np

def pack(kernel_args):
    blobs = []
    def total():
        return sum(len(x) for x in blobs)
    for arg in kernel_args:
        if isinstance(arg, np.generic):
            blobs.append(arg.tobytes())
            if arg.itemsize < 4:
                blobs.append(b"\0" * (4 - arg.itemsize))
            elif 4 < arg.itemsize < 8:
                blobs.append(b"\0" * (8 - arg.itemsize))
        else:  # 指针参数以 8 字节占位
            if total() % 8 != 0:
                blobs.append(b"\0" * 4)
            blobs.append(np.uint64(0xDEADBEEF).tobytes())
    aligned = int(np.ceil(total() / 8)) * 8
    combined = bytes().join(blobs).ljust(aligned, b"\0")
    return blobs, combined

blobs, combined = pack([np.int32(16), np.float32(2.5), "PTR"])
for i, b in enumerate(blobs):
    print(f"blob[{i}] len={len(b)} hex={b.hex()}")
print("total =", len(combined), "bytes,", len(combined) // 8, "words")
```

方式 B——在真实链路上打点（不修改源码，示例代码）：

```python
# 示例代码：包一层 rt.launch_kernel，打印下发的 8 字节字（放在 add.py 导入 asc 之后）
import asc.lib.runtime as rt
_orig = rt.launch_kernel
def traced(fn_handle, block_num, args, **kwargs):
    print("core_num =", block_num)
    for i, w in enumerate(args):
        print(f"word[{i}] = 0x{w.value:016x}")
    return _orig(fn_handle, block_num, args, **kwargs)
rt.launch_kernel = traced
```

以 01_add 为例（参数为 3 个张量 + 1 个 int，[examples/01_add/add.py:L78](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/01_add/add.py#L78)，`size=8*2048`、`USE_CORE_NUM=8` 故 `block_length=2048`），预期打印：

- `word[0..2]`：三个设备地址（Model 模式下为仿真器分配的地址）；
- `word[3] = 0x0000000000000800`——2048 的 int32 落在低 4 字节，尾部补 4 字节零；
- `core_num = 8`。

若要复现第一步的三参数布局，可把 kernel 签名改写成 `(n: int, alpha: float, out: asc.GlobalAddress)` 形式（修改示例副本，不动仓库源码），预期 `word[0] = 0x4020000000000010`、`word[1]` 为地址、共 2 个字；该变体**待本地验证**。

### 第三步：解释 8 字节对齐

1. **运行时接口的粒度**：底层 `KernelLaunchWrapper` 接收的就是 `uint64` 数组及其「参数个数 × 8」的字节数（[python/asc/lib/runtime/interface.py:L323-L331](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/runtime/interface.py#L323-L331)），参数区天然按 8 字节字组织。
2. **设备侧取指方式**：kernel 桩代码按固定偏移读参数，指针必须一次 64 位load 读出。若指针跨在两个 8 字节字的中间，AI Core 上的非对齐 64 位访问是非法/未定义行为——这正是「指针参数前需要对齐到 8 字节」的直接原因。
3. **补 4 字节就够**：所有标量槽都是 4 或 8 字节，累计长度永远是 4 的倍数，失配只可能是「差 4 到 8 的倍数」，所以 L99-L100 固定插入一个 4 字节零字即可修复任意失配。
4. **地址本身也对齐**：`malloc` 把设备地址对齐到 512 字节（[python/asc/lib/runtime/interface.py:L283](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/lib/runtime/interface.py#L283)），blob 槽位对齐后取出的指针可直接解引用。

## 6. 本讲小结

- `LaunchOptions`（core_num、stream）只来自中括号、只影响下发，不参与缓存 key；`core_num <= 0` 在 `run()` 末段被拒绝。
- `expand_kernel_args` 把实参规整为「定宽 numpy 标量 + `MemoryHandle`」两类；Python int/float 固定按 int32/float32 传参，bool 因是 int 子类实际走 int32 分支（小端序下与设备侧 int8 槽位兼容）。
- 参数 ABI 是一条连续字节流：标量槽 4/8 字节、指针槽前对齐到 8 字节、尾部补齐到 8 的倍数，最终切成 `uint64` 字数组经 `KernelLaunchWrapper` 下发。
- `MemoryHandle` 三段生命周期（上设备 → 同步后回拷 → 释放）；NPU 张量走零拷贝代理，CPU 张量与 numpy 数组每方向一次真实拷贝；Struct 以 `pack()` 的 bytes 整块上设备。
- `enable_debug` 会在参数尾部追加 75 MiB 的 dump 缓冲（ndarray → `NumpyArrayHandle`），执行后对这块缓冲改走 `call_print_interface` 而不是回拷。
- 每次 `run` 结束调用 `rt.free_mem()` 注销全部已注册二进制并释放设备分配；`DRY_RUN=1` 可以只编译不下发。

## 7. 下一步学习建议

本讲结束后，主链路五步（选项分流 → 缓存 → codegen → compile → launch）已全部走完。下一讲 **u3-l7（acl runtime 封装：lib/runtime 的 ctypes 绑定）** 往下钻一层：本讲反复调用的 `rt.*` 原语（`_lazy_init`、`register_device_binary_kernel`、`KernelLaunchWrapper`、`copy_data_to_device`）在 `interface.py`/`state.py`/`support.py`/`build_utils.py` 中如何用 ctypes 声明并按 Model/NPU 模式路由。之后可带着「参数 blob 如何被设备读取」的问题进入 u4 单元（FunctionVisitor），或直接跳到 u6-l5（Ascend C 代码发射）对照 kernel 入口的参数声明。
