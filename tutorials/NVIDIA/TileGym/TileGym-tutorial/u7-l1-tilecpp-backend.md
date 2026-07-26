# CUDA Tile C++（tilecpp）后端

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 tilecpp 后端「`.cuh` 内核源 + `.py` 包装」的成对文件组织方式，以及它与 cuTile「单 `.py` 文件」的区别。
- 跟踪 `TileCppKernel` 类如何把一段 C++ 模板内核，经过「显式模板实例化 → nvcc 编译 → cubin 缓存 → 符号查找 → 启动」串成一次可调用的算子。
- 理解 tilecpp 后端为什么依赖 nvcc ≥ 13.3，以及它的可用性探测为何被设计成「廉价预筛 + 延迟且缓存的昂贵检查」。
- 说明同一个算子名 `"softmax"` 如何通过 `@register_impl` 让 cutile 与 tilecpp 平等地成为两个后端实现，并动手追踪 `set_backend("tilecpp")` 后 dispatch 是如何选中 tilecpp 实现的。

本讲是「多后端实现」单元的首篇。前置认知来自 **u2-l2（后端注册表与分发机制）** 和 **u3-l4（softmax 四种实现与 autograd 封装）**：你已经知道 `_REGISTRY` 是 `{算子名: {后端: 实现}}` 的嵌套字典，也知道 cuTile 版 softmax 的四种变体。本讲换一个角度——**换一种语言（C++ 代替 Python DSL）和一种编译方式（nvcc 代替 tileiras）来实现同一个算子**，并复用同一套分发机制。

## 2. 前置知识

本讲假设你已经理解 tile 编程模型、`@dispatch`/`register_impl` 分发机制（见 u2-l2）和 softmax 的数值算法（见 u3-l4）。下面补充几个本讲新出现的术语：

- **CUDA Tile C++**：CUDA 13.3+ 引入的 C++ tile 编程扩展。它和 cuTile 用的是同一套 tile 抽象（瓦片、bid、在线归约），但宿主语言是 C++ 而非 Python。内核用 `__tile_global__` 标记，tile API 在 `cuda::tiles` 命名空间下。
- **模板（template）**：C++ 的编译期泛型机制。`template<typename T, int BLOCK_SIZE>` 表示「元素类型 `T`」和「编译期常量 `BLOCK_SIZE`」是两个泛型参数，编译器要为每一组具体取值生成一份特化代码。这正好对应 cuTile 里的 `ConstInt`（见 u3-l1）。
- **显式模板实例化（explicit instantiation）**：一句形如 `template __tile_global__ void softmax_kernel<float, 1024>(...);` 的语句，命令编译器「现在就为这一组模板参数生成代码」。tilecpp 正是靠它在编译期把需要的特化内核烤进 cubin。
- **cubin（CUDA binary）**：GPU 能直接加载执行的二进制机器码文件。tilecpp 把 `.cuh` 编译成 `.cubin` 缓存起来，运行时直接加载，避免重复编译。
- **nvcc**：NVIDIA 的 CUDA C++ 编译器。tilecpp 后端用它（要求 ≥ 13.3）来开启 tile 扩展并产出 cubin。
- **mangled name（修饰名）**：C++ 因为支持重载和模板，编译后会把函数名「修饰」成包含参数类型的唯一符号串（如 `_Z13softmax_kernel...`）。运行时从 cubin 里取内核要按这个修饰名查。
- **block=1 启动模型**：tile 内核是「以瓦片为中心」的，并行度来自 grid（即 CTA/program 数量），每个 CTA 内部线程组织由 tile 运行时接管，因此启动时 `block` 维度恒为 1（见 README 的说明与本讲 4.2）。

> 一句话对照：cuTile = Python DSL（`@ct.kernel`）+ 运行时编译器 tileiras；tilecpp = C++ 模板（`__tile_global__`）+ 离线编译器 nvcc。两者产出同一类 GPU 代码，却走了完全不同的编译链路。

## 3. 本讲源码地图

本讲涉及的关键文件与职责：

| 文件 | 作用 |
| --- | --- |
| [src/tilegym/ops/tilecpp/softmax.cuh](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/tilecpp/softmax.cuh) | softmax 的 C++ tile 内核源，含 4 个模板内核（前向/反向 × 普通/online） |
| [src/tilegym/ops/tilecpp/softmax.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/tilecpp/softmax.py) | Python 包装：建 `TileCppKernel`、启动、autograd 封装、`@register_impl` |
| [src/tilegym/ops/tilecpp/utils/_cuda_utils.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/tilecpp/utils/_cuda_utils.py) | tilecpp 后端的「引擎」：`TileCppKernel` 类、nvcc 编译、cubin 缓存、启动 |
| [src/tilegym/ops/tilecpp/utils/_dump_types.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/tilecpp/utils/_dump_types.py) | 调试辅助：按环境变量导出内核实参类型 |
| [src/tilegym/ops/tilecpp/__init__.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/tilecpp/__init__.py) | 后端可用性门控下的批量子模块导入（注册的触发点） |
| [src/tilegym/backend/selector.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/selector.py) | `is_tilecpp_available`、nvcc 版本探测、`_AVAILABLE_BACKENDS` |
| [src/tilegym/backend/dispatcher.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/dispatcher.py) | `_REGISTRY`、`register_impl`、`dispatch` wrapper |
| [README.tilecpp.md](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/README.tilecpp.md) | 环境变量表、新增 tilecpp 内核指引、独立编译 `.cuh` 的方法 |
| [tests/ops/test_softmax.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/ops/test_softmax.py) | softmax 测试：在 tilecpp 可用时把它加入参数化后端列表 |

## 4. 核心概念与源码讲解

### 4.1 `.cuh` 内核源 + kernel_name：成对文件组织

#### 4.1.1 概念说明

tilecpp 后端的每个算子通常由**两个文件**组成（README 称之为 "two pieces"）：

1. 一份 C++ tile 内核源 `src/tilegym/ops/tilecpp/<op>.cuh`，里面是真正的 GPU 许算逻辑。
2. 一份 Python 包装 `src/tilegym/ops/tilecpp/<op>.py`，负责编译、缓存、启动、autograd 与注册。

这与 cuTile「一个算子一个 `.py`、内核直接写在 Python 里用 `@ct.kernel` 装饰」截然不同。为什么要拆成两半？因为 C++ 内核由 **nvcc** 离线编译，更接近硬件、可读性强，还能脱离 TileGym 框架独立编译验证（README 给了一份只含 `.cu` driver 的独立编译示例）。代价是：Python 侧必须自己写「编译 + 缓存 + 启动」这一整套胶水代码——这正是模块 4.2 的 `TileCppKernel` 要封装的东西。

`kernel_name` 是一个字符串，指向 `.cuh` 里**某个具体内核函数的名字**（模板实例化前的「基础名」）。一个 `.cuh` 可以装多个内核：`softmax.cuh` 就装了 4 个（普通/online × 前向/反向）。Python 侧为每个要用的内核各建一个 `TileCppKernel` 实例，用 `kernel_name` 把它们区分开。

#### 4.1.2 核心流程

- `.cuh` 用 `template<typename T, int BLOCK_SIZE>` 定义泛型内核，`__tile_global__` 标记它是 tile 内核。
- 内核体内 `namespace ct = cuda::tiles;` 给 tile API 起别名，然后用 `ct::tile<...>`、`ct::load_masked`、`ct::reduce_max` 等 C++ tile 原语写计算——这些与 cuTile 的 `ct.load`/`ct.reduce` 在概念上一一对应。
- Python 侧为每个内核构造一个 `TileCppKernel(source_path=..., kernel_name=...)`，把「源文件」和「内核名」绑死。

#### 4.1.3 源码精读

softmax 的基础前向内核是一个标准的 C++ tile 模板（[softmax.cuh:L35-L44](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/tilecpp/softmax.cuh#L35-L44)）：`T` 是元素类型，`BLOCK_SIZE` 是编译期瓦片大小，函数体才是真正的 softmax 计算。

```cpp
template<typename T, int BLOCK_SIZE>
__tile_global__ void softmax_kernel(
    T* __restrict__ _output,
    const T* __restrict__ _input,
    int input_row_stride, int output_row_stride,
    int n_rows, int n_cols, int num_programs
) {
    namespace ct = cuda::tiles;          // C++ tile DSL 命名空间别名
    ...
    int row_start = ct::bid().x;          // 等价于 cuTile 的 ct.bid(0)
    int row_step = num_programs;
    for (auto row_idx : ct::irange(row_start, n_rows, row_step)) { ... }
}
```

注意 [softmax.cuh:L57-L60](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/tilecpp/softmax.cuh#L57-L60)：`row_start = ct::bid().x` 加 `ct::irange(row_start, n_rows, row_step)` 正是 u3-l1 讲过的**静态持久化 grid-stride 调度**——每个 CTA 跨步处理多行。tilecpp 在 C++ 里用 `ct::bid().x`，cuTile 在 Python 里用 `ct.bid(0)`，语义完全相同。

Python 侧用 `kernel_name` 把这个内核绑成一个对象（[softmax.py:L37-L40](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/tilecpp/softmax.py#L37-L40)）：

```python
_softmax_kernel = TileCppKernel(
    source_path=Path(__file__).parent / "softmax.cuh",
    kernel_name="softmax_kernel",
)
```

同一个 `softmax.cuh` 里的 4 个内核各绑定一个对象（[softmax.py:L37-L55](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/tilecpp/softmax.py#L37-L55)）：`_softmax_kernel`、`_online_softmax_kernel`、`_softmax_kernel_backward`、`_online_softmax_kernel_backward`，`kernel_name` 分别是 `softmax_kernel` / `online_softmax_kernel` / `softmax_kernel_backward` / `online_softmax_kernel_backward`。

#### 4.1.4 代码实践

**实践目标**：建立「`.cuh` 内核名 ↔ Python `TileCppKernel` 对象」的一一映射。

**操作步骤**：

1. 打开 `src/tilegym/ops/tilecpp/softmax.cuh`，用搜索定位所有 `__tile_global__` 出现的位置，记下每个内核函数名。
2. 打开 `src/tilegym/ops/tilecpp/softmax.py` 顶部，列出 4 个 `TileCppKernel(...)` 的 `kernel_name`。

**需要观察的现象**：`.cuh` 里的内核函数名与 `.py` 里的 `kernel_name` 字符串应严格一一对应；任何一个拼错，运行时会在 cubin 里查不到匹配符号（见 4.2 的 `get_kernel_name_from_cubin`）。

**预期结果**：得到一张 4 行的对照表（前向普通 / 前向 online / 反向普通 / 反向 online）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `BLOCK_SIZE` 要做成 C++ 模板参数，而 `n_rows`、`n_cols` 却是普通运行期 `int` 形参？

**参考答案**：`BLOCK_SIZE` 决定了 tile 的形状（`ct::tile<T, ct::shape<BLOCK_SIZE>>`），进而决定寄存器分配与循环结构，必须在编译期确定；而 `n_rows`/`n_cols` 只是运行期数据规模，不影响内核的 tile 结构，用普通 `int` 传入即可。这与 cuTile 用 `ConstInt` 表达编译期常量、用张量表达运行期数据是同一个道理（见 u3-l1）。

**练习 2**：如果把 `softmax.cuh` 里新增了第 5 个内核 `fused_softmax_kernel`，Python 侧最小要改哪里才能用上它？

**参考答案**：新增一个 `TileCppKernel(source_path=Path(__file__).parent / "softmax.cuh", kernel_name="fused_softmax_kernel")` 实例，再写一个对应的 `_launch_*` 函数调用它的 `get_kernel`/`launch`。`.cuh` 文件本身不需要改路径，因为它已经被多个对象共享引用。

---

### 4.2 `TileCppKernel` 包装：编译、缓存与启动

#### 4.2.1 概念说明

`TileCppKernel` 是 tilecpp 后端最核心的类，它把「一个 `.cuh` 内核 + 一组模板参数」封装成「可查、可编译、可缓存、可启动」的对象。这是 tilecpp 相对 cuTile 最大的额外工程量：cuTile 的编译由 tileiras 在 `ct.launch` 内部悄悄完成，而 tilecpp 必须在 Python 侧显式地「生成实例化语句 → 调 nvcc → 存 cubin → 取符号 → 启动」。

对外的接口只有两个方法：

- `get_kernel(dtype, template_params, signature)` → 返回 `(kernel 对象, mangled_name, scalar_converter)`，负责「按需编译 + 多级缓存」。
- `launch(grid, kernel, args, stream=None)` → 把准备好的内核提交到 GPU。

#### 4.2.2 核心流程

**`get_kernel` 的决策流程**（已读源码 [_cuda_utils.py:L486-L587](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/tilecpp/utils/_cuda_utils.py#L486-L587)）：

1. 由 `dtype` 查 `DTYPE_MAP` 得到 C++ 类型名（如 `torch.float32 → "float"`）和标量转换函数。
2. 拼模板实参 `all_params = [cpp_type] + [模板参数]`，并据此生成 `template_key` 与 `cache_key`（`cache_key` 还带 `device_id`，因为内核句柄与 CUDA context 绑定）。
3. 查**内存缓存** `_global_kernel_cache`：若命中**且** CUDA context 未变，直接复用内核句柄；否则进入下一步。
4. 算 `source_hash = md5(源码哈希 + template_key)`，拼出**确定性 cubin 路径**（不含 PID，可跨进程共享）。
5. 在**文件锁**保护下：路径已存在则读盘上 cubin；否则调 `_compile_kernel` 编译并原子写入。
6. 用 `get_kernel_name_from_cubin` 从 cubin 枚举函数符号、找到含 `kernel_name` 的修饰名。
7. `ObjectCode.from_cubin(bytes)` + `mod.get_kernel(mangled_name)` 加载内核句柄，连同 context/bytes/converter 一起写回内存缓存。

**`launch` 的流程**（[_cuda_utils.py:L589-L619](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/tilecpp/utils/_cuda_utils.py#L589-L619)）：

1. 设置当前设备；
2. 取 PyTorch 当前 stream 并转成 `cuda.core.Stream`（保证与 PyTorch 算子流顺序一致、兼容 CUDA Graph）；
3. `LaunchConfig(grid=grid, block=1)`——**block 恒为 1**，并行度全靠 grid；
4. `launch(stream, config, kernel, *args)` 提交。

#### 4.2.3 源码精读

**编译的真正发生处**——`compile_cuda_to_cubin` 会生成一个临时 `.cu` 文件，内容只有「include 头文件 + 显式模板实例化语句」，然后调 nvcc（[_cuda_utils.py:L273-L299](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/tilecpp/utils/_cuda_utils.py#L273-L299)）：

```python
cmd = [
    NVCC_PATH,
    "-tilecubin",            # 产出 cubin 而非可执行文件
    f"-arch={gpu_arch}",     # 针对当前 GPU 架构（如 sm_100）
    "-std=c++20",
    "--tile-only",           # 只做 tile→cubin，不链接 host 代码
    "-o", str(output_path),
]
```

那条「显式模板实例化」语句由 `_compile_kernel` 拼出来（[_cuda_utils.py:L467-L469](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/tilecpp/utils/_cuda_utils.py#L467-L469)）：

```python
template_inst = f"template __tile_global__ void {self.kernel_name}<{template_args}>({sig_with_type});"
```

例如对 fp32、`BLOCK_SIZE=1024` 的 softmax，它生成的大致是 `template __tile_global__ void softmax_kernel<float, 1024>(float*, const float*, int, int, int, int, int);`——这句命令 nvcc「现在就为这组参数编译出机器码」。

**符号查找**——cubin 里可能有多个函数，要按 `kernel_name` 子串匹配出修饰名（[_cuda_utils.py:L362-L373](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/tilecpp/utils/_cuda_utils.py#L362-L373)）：用 `cuModuleEnumerateFunctions` 枚举所有函数，逐个 `cuFuncGetName`，返回第一个名字里含 `expected_kernel_name` 的修饰名。

**softmax 的调用样例**——`_launch_softmax_forward` 是「算 grid → get_kernel → launch」的典型三段式（[softmax.py:L98-L122](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/tilecpp/softmax.py#L98-L122)）：

```python
kernel, _, _ = _softmax_kernel.get_kernel(
    dtype=dtype,
    template_params=[block_size],                       # 只有 BLOCK_SIZE，T 由 dtype 推
    signature="{T}*, const {T}*, int, int, int, int, int",
)
...
_softmax_kernel.launch(
    grid=grid, kernel=kernel,
    args=[
        np.uint64(output_tensor.data_ptr()),             # 指针一律转 uint64
        np.uint64(input_tensor.data_ptr()),
        np.int32(input_tensor.stride(0)), ...            # 标量转 np.int32
    ],
)
```

注意 `signature` 里的 `{T}` 占位符会被替换成具体 C++ 类型（如 `float`），这正是模板实例化时签名要用的。参数列表里**指针转 `np.uint64`、标量转 `np.int32`**，是为了与底层 CUDA 启动 ABI 对齐。

**启动的 block=1 约定**（[_cuda_utils.py:L617-L619](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/tilecpp/utils/_cuda_utils.py#L617-L619)）：

```python
config = LaunchConfig(grid=grid, block=1)
launch(stream, config, kernel, *args)
```

这正是 README 反复强调的「tile 内核以瓦片为中心：启动恒用 block=1，内核用 `ct::bid()` 获取并行度」。

#### 4.2.4 代码实践

**实践目标**：完整跟踪一次 tilecpp softmax 从 Python 调用到 nvcc 编译的链路。

**操作步骤**：

1. 从 [softmax.py:L98](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/tilecpp/softmax.py#L98) 的 `_softmax_kernel.get_kernel(...)` 出发。
2. 跳到 [_cuda_utils.py:L486](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/tilecpp/utils/_cuda_utils.py#L486) 的 `get_kernel`，逐步走过：`get_dtype_info` → 拼 `template_key` → 查 `_global_kernel_cache` → `_make_cubin_path` → `_FileLock` → `_compile_kernel`。
3. 在 `_compile_kernel`（[L454](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/tilecpp/utils/_cuda_utils.py#L454)）里看清它如何生成 `template_inst` 字符串并交给 `compile_cuda_to_cubin`。
4. 最后看 `launch`（[L589](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/tilecpp/utils/_cuda_utils.py#L589)）如何把 grid 与 `block=1` 组成 `LaunchConfig`。

**需要观察的现象**：链路上「实例化语句生成」「nvcc 命令拼装」「cubin 符号查找」三段是分离的，分别由 `_compile_kernel`、`compile_cuda_to_cubin`、`get_kernel_name_from_cubin` 承担。

**预期结果**：能画出 `get_kernel → _compile_kernel → compile_cuda_to_cubin → nvcc → cubin → get_kernel_name_from_cubin → ObjectCode.from_cubin → launch` 的完整时序图。

> 待本地验证：在没有 nvcc ≥ 13.3 的机器上，这条链路会在 `compile_cuda_to_cubin` 的 `subprocess.run` 处失败；可用 `TILECPP_SAVE_SRC=1` 把生成的临时 `.cu` 存盘以便排查（见模块 4.3）。

#### 4.2.5 小练习与答案

**练习 1**：`get_kernel` 返回三元组里的第三个元素 `scalar_converter` 是做什么用的？

**参考答案**：它来自 `DTYPE_MAP`，是把「Python 标量」转成「内核能识别的位表示」的函数。例如 `torch.bfloat16` 对应 `_float_to_bfloat16_bits`（[_cuda_utils.py:L173](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/tilecpp/utils/_cuda_utils.py#L173)），因为 bfloat16 无法直接经 ABI 传递，要转成等价的 `uint16` 位模式。`make_kernel_args` 这个辅助函数（[L622](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/tilecpp/utils/_cuda_utils.py#L622)）会用到它。

**练习 2**：为什么内存缓存 `_global_kernel_cache` 在命中时还要额外校验 `cached_ctx == cur_ctx`？

**参考答案**：内核句柄（kernel handle）与具体的 CUDA context 绑定，不能跨 context 复用。多进程或 context 切换时，cubin 字节（与机器码绑定、可跨 context）依然有效，但句柄必须重新从 cubin 加载。所以校验失败时会用内存里的 cubin bytes 重新 `ObjectCode.from_cubin`（[_cuda_utils.py:L549-L555](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/tilecpp/utils/_cuda_utils.py#L549-L555)），避免再次读盘或重编译。

---

### 4.3 nvcc 版本要求与 cubin 缓存

#### 4.3.1 概念说明

tilecpp 后端依赖 **nvcc ≥ 13.3** 来开启 CUDA Tile C++ 扩展（cuTile 则靠运行时编译器 tileiras，见 u1-l2）。问题在于：探测 nvcc 版本要 fork 一个 `nvcc --version` 子进程，耗时数百毫秒，远比 cuTile 的「一次 import」昂贵。因此 selector 把 tilecpp 的可用性探测设计成**两段式**：

- **廉价预筛**（导入时，无子进程）：只检查 `_cuda_utils` 模块能否 import。
- **昂贵实检**（延迟且缓存）：fork `nvcc --version` 解析版本号，整个进程最多跑一次。

cubin 缓存则是 tilecpp 避免每次运行都重编译的关键，分**两层**：

- **磁盘层**：cubin 存在 `~/.cache/tilecpp/` 下，文件名是 `{kernel_name}_{模板键}_{arch}_{源码哈希}.cubin`，**不含 PID**，因此可跨进程、跨重启共享。
- **内存层**：`_global_kernel_cache` 在进程内缓存「context + cubin bytes + 修饰名 + 内核句柄」，命中后连文件 I/O 都省了。

两者辅以**文件锁**（`fcntl`，序列化多进程编译）与**原子写**（先写临时文件再 `rename`，杜绝别进程读到半成品 cubin）。

#### 4.3.2 核心流程

**nvcc 探测的两段**：

1. 导入 selector 时执行 `_check_tilecpp_module_importable()`（[selector.py:L89-L113](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/selector.py#L89-L113)），只尝试用 `importlib` 导入 `_cuda_utils` 模块、不 spawn 子进程，结果存入 `_TILECPP_MODULE_IMPORTABLE`（[selector.py:L116](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/selector.py#L116)）。`_AVAILABLE_BACKENDS` 里 tilecpp 的初始取值（[selector.py:L188-L195](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/selector.py#L188-L195)）用的就是这个廉价结果。
2. 真正要用 tilecpp 时（dispatch、`set_backend("tilecpp")`、`is_backend_available("tilecpp")`）才调 `is_tilecpp_available()`——它被 `@functools.cache` 装饰，故昂贵的 `_nvcc_version_supported()` 全进程至多执行一次。

**cubin 缓存的两层 + 并发安全**（`get_kernel` 内 [_cuda_utils.py:L536-L585](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/tilecpp/utils/_cuda_utils.py#L536-L585)）：

1. 先查内存缓存（含 context 校验）。
2. 未命中则按 `source_hash` 算确定性 cubin 路径。
3. `with _FileLock(cubin_path):` 进入临界区——路径存在则直接读盘，不存在才编译；编译结果经 `_atomic_write_bytes` 落盘。
4. 把 cubin bytes、修饰名、内核句柄写回内存缓存。

涉及的环境变量（见 [README.tilecpp.md](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/README.tilecpp.md) 表格）：`TILECPP_CACHE_DIR`（缓存目录）、`TILECPP_DISABLE_CACHE`（禁用缓存强制重编译）、`TILECPP_SAVE_SRC`（保留生成的 `.cu`）、`TILECPP_NVCC_PATH`（指定 nvcc 路径）、`TILECPP_VERBOSE_AUTOTUNE`（打印缓存命中/编译日志）。

#### 4.3.3 源码精读

**最低版本要求**写死在模块顶部（[selector.py:L54](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/selector.py#L54)）：

```python
_TILECPP_MIN_NVCC = (13, 3)
```

`_nvcc_version_supported` 解析 `nvcc --version` 输出里的 `release X.Y`，比较是否 ≥ 13.3（[selector.py:L57-L86](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/selector.py#L57-L86)）。它先看 `$TILECPP_NVCC_PATH`、再回退到 PATH 上的 `nvcc`。

**延迟且缓存的昂贵检查**——`is_tilecpp_available` 被 `@functools.cache` 装饰（[selector.py:L119-L146](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/selector.py#L119-L146)）：先看廉价预筛是否通过，再跑 nvcc 版本检查，失败时发一次 `UserWarning`（`stacklevel=2` 指向调用方）。

**dispatcher 里的安全网**——即便别处没触发，wrapper 在真正分发到 tilecpp 前还会再查一次（结果已被缓存，零开销）；不可用就回退到 fallback（[dispatcher.py:L91-L92](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/dispatcher.py#L91-L92)）：

```python
if current_backend == "tilecpp" and not is_tilecpp_available():
    current_backend = fallback_backend
```

> 准确性说明（基于实际代码）：`import tilegym` 会触发 `from . import ops`，而 `ops/__init__.py` 在 [L38](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/__init__.py#L38) 就调用了 `is_backend_available("tilecpp")`。因此在「tilecpp 模块可 import」的机器上，首次 `import tilegym` 即会触发一次 nvcc 探测；但 `@functools.cache` 保证全进程只此一次。而在「tilecpp 模块不可 import」的机器上，tilecpp 根本不进入 `_AVAILABLE_BACKENDS`，于是**永远不会 spawn nvcc 子进程**——这正是「导入廉价」的真正保证。

**确定性 cubin 路径**——文件名只由内核名、模板键、架构、源码哈希决定，不含 PID（[_cuda_utils.py:L443-L452](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/tilecpp/utils/_cuda_utils.py#L443-L452)），故多进程能共享同一份 cubin。源码哈希把「源码内容 + 模板键」一起喂给 md5（[_cuda_utils.py:L557](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/tilecpp/utils/_cuda_utils.py#L557)），所以你只要改一个字 `.cuh`，缓存自动失效。

**并发安全**——`_FileLock` 用 `fcntl.flock` 给 `<cubin>.lock` 加排他锁（[_cuda_utils.py:L88-L124](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/tilecpp/utils/_cuda_utils.py#L88-L124)），让 tensor-parallel 的多个 worker 只有一个编译、其余等待复用；`_atomic_write_bytes` 先写 `.tmp` 再 `os.replace`（[_cuda_utils.py:L126-L145](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/tilecpp/utils/_cuda_utils.py#L126-L145)），保证等待方永远不会读到写了一半的 cubin。

#### 4.3.4 代码实践

**实践目标**：用环境变量观察 tilecpp 的缓存行为。

**操作步骤**：

1. 先清掉缓存：`rm -rf ~/.cache/tilecpp/*`。
2. 第一次运行（强制重编译）：`TILECPP_DISABLE_CACHE=1 TILECPP_SAVE_SRC=1 python -c "import torch, tilegym; tilegym.set_backend('tilecpp'); x=torch.randn(256,2048,device='cuda'); print(tilegym.ops.softmax(x).shape)"`（**待本地验证**：需要本机有 nvcc ≥ 13.3 与支持 sm_80+ 的 GPU）。
3. 不禁用缓存再跑一次同样命令，对比启动耗时。

**需要观察的现象**：

- 第一次会看到 nvcc 编译耗时（数百毫秒到数秒）；`TILECPP_SAVE_SRC=1` 会在缓存目录留下生成的 `.cu` 文件，能直接看到那条 `template __tile_global__ void softmax_kernel<...>(...)` 实例化语句。
- 第二次因内存/磁盘缓存命中，应几乎无编译开销。

**预期结果**：第二次运行明显更快；缓存目录里出现形如 `softmax_kernel_float_1024_sm_100_<hash>.cubin` 的文件。

#### 4.3.5 小练习与答案

**练习 1**：为什么 cubin 文件名里要带上 `arch`（如 `sm_100`）？

**参考答案**：cubin 是针对特定 GPU 架构编译的二进制，换一个架构（如从 Hopper 的 sm_90 换到 Blackwell 的 sm_100）就要重新编译。把 `arch` 编进文件名，能让同一台多卡异构机器或不同机器复用缓存目录而不会串档。

**练习 2**：`set_backend("tilecpp")` 与 dispatch 里的延迟检查都对 nvcc 做了校验，是否冗余？

**参考答案**：不冗余，而是「快速失败」与「兜底」两种意图。`set_backend`（[selector.py:L240-L246](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/selector.py#L240-L246)）在用户显式选 tilecpp 时立即校验并抛清晰错误，避免后续静默回退；dispatch 里的检查（[dispatcher.py:L91](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/dispatcher.py#L91)）则覆盖「后端被环境变量设成 tilecpp」等未走 `set_backend` 的路径。两者都受益于 `@functools.cache`，实际只跑一次子进程。

---

### 4.4 后端无关注册：同一算子名 `register_impl`

#### 4.4.1 概念说明

前面三模块解决了「tilecpp 内核如何编译与启动」。最后一步，是把包装好的 Python 函数挂进全局注册表——这一步与 cuTile **完全对称**：

- cuTile：`@register_impl("softmax", backend="cutile")`
- tilecpp：`@register_impl("softmax", backend="tilecpp")`

两者都注册到同一个算子名 `"softmax"` 下，只是后端子键不同。这正体现了 u2-l2 讲过的核心设计：**算子名是全局键，后端是子键**。dispatch wrapper 不知道、也不关心某个后端是用 Python DSL 还是 C++ 写的——它只按「当前后端」查表。

#### 4.4.2 核心流程

**注册与门控**：

1. `ops.py` 用 `@dispatch("softmax")` 定义统一 stub（注意它没传 `fallback_backend`，故用默认值 `"pytorch"`，详见 u2-l1）。
2. `cutile/softmax.py` 的 `@register_impl("softmax", backend="cutile")` 把 cuTile 实现挂到 `_REGISTRY["softmax"]["cutile"]`。
3. `tilecpp/softmax.py` 的 `@register_impl("softmax", backend="tilecpp")` 把 tilecpp 实现挂到 `_REGISTRY["softmax"]["tilecpp"]`。
4. `tilecpp/__init__.py` 只在 `is_backend_available("tilecpp")` 为真时才 `from . import softmax`——**注册作为导入副作用**，只有这一刻才发生。

**`set_backend("tilecpp")` 后 dispatch 选中 tilecpp 的流程**：

1. `tilegym.ops.softmax(x)` 调到 wrapper。
2. 取 `current_backend = get_current_backend()` → `"tilecpp"`（由 `set_backend` 设定）。
3. 若 `is_tilecpp_available()` 为假 → `current_backend` 被改成 `fallback_backend`（`"pytorch"`）。
4. 若为真 → `_REGISTRY["softmax"]["tilecpp"]` 命中 → 调用 `tilecpp/softmax.py` 里的 `softmax()`。
5. 该函数按列数自动选 `Softmax.apply` 或 `OnlineSoftmax.apply`——两者都是带 forward+backward 的 `torch.autograd.Function`。

#### 4.4.3 源码精读

**统一 stub**（[ops.py:L225-L244](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/ops.py#L225-L244)）：

```python
@dispatch(
    "softmax",
)
def softmax(x, use_tma=False, **kwargs):
    ...
    raise NotImplementedError(f"softmax is not implemented for {get_current_backend()}")
```

**对称的两个注册**——cutile（[cutile/softmax.py:L356-L357](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py#L356-L357)）与 tilecpp（[tilecpp/softmax.py:L285-L286](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/tilecpp/softmax.py#L285-L286)），同一个名字、不同后端：

```python
@register_impl("softmax", backend="tilecpp")
def softmax(x, use_tma=False, use_online=None, **kwargs):
    ...
```

**门控导入**——注册只在可用时发生（[tilecpp/__init__.py:L10-L34](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/tilecpp/__init__.py#L10-L34)）：

```python
if is_backend_available("tilecpp"):
    ...
    from . import softmax        # 触发 softmax.py 顶层的 @register_impl
    from .softmax import softmax
```

**dispatch 的查表逻辑**（[dispatcher.py:L95-L97](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/dispatcher.py#L95-L97)）：

```python
if name in _REGISTRY and current_backend in _REGISTRY[name]:
    return _REGISTRY[name][current_backend](*args, **kwargs)
```

**tilecpp softmax 的自动选路**（[softmax.py:L301-L310](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/tilecpp/softmax.py#L301-L310)）：当列数 ≥ `COL_THRESHOLD`（32768）时走 online 两遍算法，否则走基础内核。

**一个值得注意的对比**——tilecpp 的 softmax 是**完整的 autograd**：`Softmax` 与 `OnlineSoftmax` 都定义了 `forward` 和 `backward`（[softmax.py:L232-L277](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/tilecpp/softmax.py#L232-L277)），对应 `.cuh` 里的 `softmax_kernel_backward` / `online_softmax_kernel_backward` 两个反向内核。这与本仓库当前 cuTile 版 `_Softmax` 仅定义 forward（见 u3-l4）形成对照——**是否实现反向是「逐后端、逐算子」的能力差异**，而非接口差异：两者共用同一个 `ops.softmax` 入口，调用方无感。

**测试如何纳入 tilecpp**（[test_softmax.py:L19-L22](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/ops/test_softmax.py#L19-L22)）：后端列表默认只有 `["cutile"]`，在 tilecpp 可用时才追加：

```python
_backends = ["cutile"]
if is_backend_available("tilecpp"):
    _backends = _backends + ["tilecpp"]
```

#### 4.4.4 代码实践（本讲核心任务）

**实践目标**：追踪 tilecpp softmax 如何与 cuTile softmax 注册到同一个 `"softmax"` 算子名，并说明 `set_backend("tilecpp")` 后 dispatch 如何选中 tilecpp 实现。

**操作步骤**：

1. 打开 [ops.py:L225](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/ops.py#L225)，确认 softmax stub 用的算子名是字符串 `"softmax"`，`fallback_backend` 取默认值 `"pytorch"`。
2. 打开 [cutile/softmax.py:L356](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py#L356) 与 [tilecpp/softmax.py:L285](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/tilecpp/softmax.py#L285)，确认两者 `@register_impl` 的第一个参数都是 `"softmax"`，只有 `backend=` 不同。
3. 打开 [tilecpp/__init__.py:L10](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/tilecpp/__init__.py#L10)，看清 `if is_backend_available("tilecpp"):` 是注册能否发生的总开关。
4. 打开 [dispatcher.py:L91-L97](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/dispatcher.py#L91-L97)，走一遍 `set_backend("tilecpp")` 后的查表路径。

**需要观察的现象 / 预期结果**（可用一段话回答）：

> `set_backend("tilecpp")` 把 `_CURRENT_BACKENDS` 置为 `"tilecpp"`。调 `tilegym.ops.softmax(x)` 时，wrapper 取 `current_backend="tilecpp"`；若 `is_tilecpp_available()` 为真（模块可 import 且 nvcc ≥ 13.3），由于 `tilecpp/__init__.py` 导入时已把 tilecpp 实现注册进 `_REGISTRY["softmax"]["tilecpp"]`，wrapper 在 `_REGISTRY["softmax"]["tilecpp"]` 命中，于是执行 `tilecpp/softmax.py` 的 `softmax()`，进而走 `Softmax.apply` 或 `OnlineSoftmax.apply`。若 tilecpp 不可用，`current_backend` 会被改成 fallback `"pytorch"`，但注册表里没有 `"pytorch"` 键，最终落到 stub 抛 `NotImplementedError`——这与 u2-l1 讲的「softmax 的 fallback 实为无降级」一致。

**可选的运行验证**（**待本地验证**，需 nvcc ≥ 13.3）：

```python
import tilegym, torch
print(tilegym.get_available_backends())          # 看 'tilecpp' 是否在内
tilegym.set_backend("tilecpp")
x = torch.randn(256, 2048, device="cuda")
y = tilegym.ops.softmax(x)
print(torch.allclose(y, torch.softmax(x, -1), atol=1e-5))   # 期望 True
```

#### 4.4.5 小练习与答案

**练习 1**：如果一台机器上 tilecpp 可用、cutile 不可用，`set_backend("tilecpp")` 后调 `tilegym.ops.softmax` 还能正常工作吗？为什么？

**参考答案**：能。dispatch 只查 `_REGISTRY["softmax"]["tilecpp"]`，与 cutile 是否注册无关。cutile 不可用只是意味着 `_REGISTRY["softmax"]["cutile"]` 这一项不存在，对当前后端是 tilecpp 的查询没有影响。这也说明了「逐后端注册」的好处——后端之间彼此解耦。

**练习 2**：为什么 `register_impl` 装饰器要「原样返回 func」而不是返回一个 wrapper？

**参考答案**：因为 `register_impl`（[dispatcher.py:L46-L52](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/dispatcher.py#L46-L52)）的唯一职责是「把实现登记进 `_REGISTRY`」，登记是导入时的副作用；返回原函数让模块里 `from .softmax import softmax` 等直接访问仍拿到未被包装的原始实现。真正负责「按后端选实现」的 wrapper 在 `@dispatch` 那一侧（stub），二者职责分离。

## 5. 综合实践

**任务**：画一张「双列对比表 + 调用链」综合图，把 cuTile 与 tilecpp 两条 softmax 实现路径并排呈现，并解释它们的 autograd 完整性差异。

建议步骤：

1. **接口层（相同）**：两者共享 `ops.py` 的 `@dispatch("softmax")` stub 与 `_REGISTRY["softmax"]` 这一项。在图顶画一个汇合点。
2. **注册层（对称）**：左列画 `cutile/softmax.py` 的 `@register_impl("softmax", backend="cutile")`，右列画 `tilecpp/softmax.py` 的 `@register_impl("softmax", backend="tilecpp")`。
3. **实现层（不同）**：
   - 左列（cuTile）：`@ct.kernel` Python DSL → tileiras JIT → `ct.launch`；当前 `_Softmax` 仅 forward（见 u3-l4）。
   - 右列（tilecpp）：`__tile_global__` C++ 模板 → `TileCppKernel.get_kernel` → nvcc 编译 cubin → `LaunchConfig(grid, block=1)`；`Softmax`/`OnlineSoftmax` 含 forward+backward。
4. **门控层**：在右列注册点上方标注 `if is_backend_available("tilecpp"):` 这个总开关，说明注册是导入副作用、且只在可用时发生。
5. **写一段说明**：为什么同一入口下，两个后端的 autograd 完整性可以不同？（提示：接口只规定统一签名，反向实现是各后端的独立能力；调用方通过 `tilegym.ops.softmax` 无感切换。）

**验收标准**：能对着图讲清楚「一次 `tilegym.ops.softmax` 调用，在 `set_backend` 切换后是如何从左列路径换到右列路径的」，并指出右列多出的「编译 + 缓存」环节。

## 6. 本讲小结

- tilecpp 后端采用 **`.cuh` 内核源 + `.py` 包装** 的成对文件组织：`.cuh` 用 `__tile_global__` 写 C++ tile 内核，`.py` 用 `kernel_name` 把它绑成 `TileCppKernel` 对象；一个 `.cuh` 可装多个内核。
- `TileCppKernel` 是 tilecpp 的引擎，对外只有 `get_kernel`（按需编译 + 内存/磁盘两级缓存 + context 校验）和 `launch`（取 torch stream、`block=1` 启动）两个方法；编译靠生成「显式模板实例化」语句再调 nvcc。
- tilecpp 依赖 **nvcc ≥ 13.3**；探测被设计成「廉价预筛（import 时无子进程）+ 昂贵实检（`@functools.cache` 全程一次）」，cubin 路径确定性命名 + 文件锁 + 原子写保证多进程安全共享。
- 注册是**后端无关**的：tilecpp 与 cutile 都用 `@register_impl("softmax", backend=...)` 挂到同一个算子名下；dispatch 只按当前后端查表，不关心实现语言。
- 注册受 `is_backend_available("tilecpp")` 门控，作为导入副作用发生；`set_backend("tilecpp")` 后 wrapper 在 `_REGISTRY["softmax"]["tilecpp"]` 命中即选中 tilecpp 实现。
- 是否实现反向是**逐后端能力**：tilecpp 的 softmax 提供完整 forward+backward，而本仓库当前 cuTile 版仅 forward——两者共用同一入口，差异对调用方透明。

## 7. 下一步学习建议

- **u7-l2（Triton 后端）** 与 **u7-l3（cutile-rs Rust FFI 后端）**：本讲建立了「同一算子名、多后端实现」的认知，接下来看另外两种后端如何各自完成编译/绑定，尤其 cutile-rs 用 Rust + cffi 的跨界方式。
- **横向阅读更多 tilecpp 内核**：`src/tilegym/ops/tilecpp/matmul.py` + `matmul.cuh`、`rms_norm.py` + `rms_norm.cuh`，体会更复杂的 tilecpp 内核如何复用本讲的 `TileCppKernel` 编译/缓存/启动骨架。
- **对比 cuTile 同名内核**：把 `ops/cutile/softmax.py` 与 `ops/tilecpp/softmax.cuh` 并排读，逐行对照「Python DSL 原语 ↔ C++ tile 原语」（如 `ct.load` ↔ `ct::load_masked`、`ct.reduce` ↔ `ct::reduce_max`），巩固 tile 编程模型与语言无关性。
- **README.tilecpp.md 的「Adding a New Kernel」**：照它的模板，尝试规划新增一个最简单的 tilecpp 算子（如 `relu`），把本讲四模块的知识用一遍。
