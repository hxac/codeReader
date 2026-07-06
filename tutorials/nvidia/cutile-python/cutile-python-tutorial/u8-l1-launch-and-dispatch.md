# launch 与调度：C++ 扩展桥接与调用约定

## 1. 本讲目标

学完本讲后，你应该能够：

- 完整画出一次 `ct.launch(stream, grid, kernel, kernel_args)` 从 Python 进入 C++ 扩展、触发 JIT 编译、命中缓存、最终落到 `cuLaunchKernelEx` 的全链路。
- 解释 `TileDispatcher` 现在如何以 **参数注解树 `parameter_annotations`** 初始化，并用「profile 缓存 + kernel family 缓存」两级缓存把热路径压到只查表。
- 区分 `DispatchMode` 的两种模式（`NormalMode` / `StaticEvalMode`），理解 `@ct.function` 标注的 tile 函数为何不能从 host 直接调用、以及在 `static_eval` 期间的报错路径。
- 说清 **调用约定（calling convention）** 的三件事：二进制参数格式、支持的参数约束集合、名称修饰（mangling）；并能解释 `cutile_python_v1` 与 `cutile_python_v2` 的差异——为何 tuple 参数与静态形状必须要求 v2。
- 看懂 C++ 扩展 `_cext` 暴露的 `launch` / `TileDispatcher` / `TileContext` / `CallingConvention` 接口边界。

## 2. 前置知识

本讲是「运行时调度、导出与扩展」单元的首讲，假定你已经学过：

- **u5-l1（kernel 装饰器与 AnnotatedFunction）**：知道 `@ct.kernel` 产物是一个继承自 C++ `TileDispatcher` 的对象，它持有 `_annotated_function`（其中包含统一的 `ParameterAnnotationNode` 注解树）与 `_compiler_options`。
- **u7-l3（tileiras 编译器调用与 cubin 生成）**：知道 `compile_tile` 产出 cubin 字节流，`tileiras` 是真正生成机器码的外部编译器，以及 `get_sm_arch` / `compile_cubin` 的角色。

补充几个本讲要用到的术语：

- **JIT（just-in-time）编译**：内核在第一次被 `launch` 时才按实际参数编译；同一内核 + 同一参数形态第二次启动可直接复用缓存。
- **cuLaunchKernelEx**：CUDA Driver API 中真正把网格投放上 GPU 的函数。它接收一个 `CUfunction`（已加载的 cubin 句柄）、grid/block 维度、动态共享内存大小、流，以及一个「指向每个参数字节缓冲的指针数组」。
- **调用约定（calling convention）**：内核二进制接口的契约——它规定 Python 参数如何摊平成 `cuLaunchKernel` 那个指针数组里的字节、支持哪些参数约束、以及如何把签名编码成符号名。
- **profile（参数画像）**：一次启动中「每个参数的 Python 类型」序列，例如 `(Tensor, int)` 与 `(Tensor, Tensor)` 是两个不同的 profile。

## 3. 本讲源码地图

| 文件 | 角色 |
|------|------|
| [src/cuda/tile/_cext.pyi](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_cext.pyi) | C++ 扩展 `_cext` 的 Python 类型存根：声明 `launch`、`TileDispatcher`、`TileContext`、`CallingConvention` 的对外签名 |
| [src/cuda/tile/_dispatch_mode.py](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_dispatch_mode.py) | `DispatchMode` 体系：`NormalMode` 与 `StaticEvalMode`，控制 tile 函数能否从 host 调用 |
| [src/cuda/tile/_execution.py](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_execution.py) | `kernel` / `function` / `stub` 装饰器；`kernel.__init__` 把注解树交给 C++ `TileDispatcher`，`kernel._compile` 是 C++ 回调 Python 的编译入口 |
| [src/cuda/tile/compilation/_signature.py](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/compilation/_signature.py) | `KernelSignature` 与 `ParameterConstraint` 体系，`_validate_constraint_support` 按调用约定版本门控 tuple/静态形状 |
| [docs/source/compilation.rst](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/docs/source/compilation.rst) | 官方文档「Calling Conventions」一节，权威描述 v1/v2 的二进制参数格式 |
| [cext/tile_kernel.cpp](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/cext/tile_kernel.cpp) | C++ 扩展主体：`cuda_tile_launch` → `launch_impl` → `launch` → `prepare_launch` → `compile` → `cuLaunchKernelEx`，以及 `TileDispatcher` / `CallingConvention` 的 C++ 实现 |

## 4. 核心概念与源码讲解

### 4.1 launch：从 Python 到 cuLaunchKernel 的 JIT 总链路

#### 4.1.1 概念说明

用户写 `ct.launch(stream, grid, kernel, kernel_args)` 时，看似只是一次函数调用，背后其实是一条横跨 Python 与 C++ 的 JIT 链。这条链要解决三件事：

1. **推断签名**：从运行时 `kernel_args` 的实际类型与值，推断出这份内核应当被特化成什么样的 `KernelSignature`（哪些是标量、哪些是数组、哪些是常量）。
2. **编译 + 缓存**：按签名调用 `kernel._compile` 把内核编译成 cubin；同样的签名不重复编译。
3. **投放网格**：把摊平后的参数字节填进 `cuLaunchKernelEx` 的参数指针数组，在指定流上启动 grid。

关键直觉是：**Python 层只负责"声明"内核（装饰器阶段把注解树与编译选项固定下来），真正"实例化"内核发生在 `launch` 时刻**——因为只有这时才能看到运行时参数的真实类型与常量取值。

#### 4.1.2 核心流程

`ct.launch` 实际是 C++ 扩展函数（见下方 4.1.3 的存根），它在 C++ 侧的调用链如下（伪代码）：

```
cuda_tile_launch(args)                         # PyMethod METH_FASTCALL 入口
  └─ launch_impl(args, signature="launch(...)", with_block=False)
       ├─ parse_launch_args(...)               # 抽出 stream / grid / dispatcher / kernel_args
       ├─ parse_launch_kwargs(...)             # launch_extended 才有 cooperative 等属性
       └─ launch(driver, dispatcher, grid, block={1,1,1}, stream, attrs, pyargs, n)
            └─ prepare_launch(...)             # JIT 核心：签名推断 + 编译 + 缓存
                 ├─ flatten_pyargs(...)        # 递归摊平 tuple 参数（见 4.2）
                 ├─ arg_profiles 查表          # 第一级缓存：按参数类型序列
                 │    （命中 → 跳过签名推断）
                 ├─ extract_cuda_args(...)     # 把参数写进 arena 字节缓冲，收集常量
                 ├─ kernels_by_constants 查表  # 第二级缓存：按常量取值
                 │    （命中 → 跳过编译）
                 ├─ minimum_calling_convention # 选 v1 还是 v2（见 4.5）
                 ├─ make_signature(...)        # 构造 Python KernelSignature
                 └─ compile(...) ─ dispatcher._compile(signature, ctx)
                                   └─ compile_tile(...)  → cubin
            └─ cuLaunchKernelEx(config, kernel, make_launch_params(helper), NULL)
```

两个缓存尤其值得记住：**profile 缓存**（按"参数类型画像"复用签名推断结果）与 **kernel family 的常量缓存**（按常量取值复用已编译 cubin）。它们共同把热路径压到「两次查表 + 一次 `cuLaunchKernelEx`」。

#### 4.1.3 源码精读

**Python 侧入口**：`ct.launch` 是直接从 C++ 扩展再导出的，没有任何 Python 包装。

[src/cuda/tile/__init__.py:7](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/__init__.py#L7) 把 `launch` 从 `_cext` 直接导入并放进 `__all__`；它的签名在存根里声明：

[src/cuda/tile/_cext.pyi:13-18](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_cext.pyi#L13-L18) ——`launch(stream, grid, kernel, kernel_args, /)`，注意是**仅位置参数**（`/`），且 `kernel` 实参就是装饰器产出的 `TileDispatcher` 子类对象。

**C++ 顶层入口**：`cuda_tile_launch` 是一个 `METH_FASTCALL` 函数，只做一次转发。

[cext/tile_kernel.cpp:3237-3240](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/cext/tile_kernel.cpp#L3237-L3240) 把 `with_block=False`（普通 `launch` 不接收 thread_count/block 维度，块维度固定为 `{1,1,1}`——因为 cuTile 表达的是 block 级并行，单个线程维度无意义）传给 `launch_impl`。

[cext/tile_kernel.cpp:3209-3233](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/cext/tile_kernel.cpp#L3209-L3233) `launch_impl` 解析参数、解析 launch 属性，然后调用 `launch(driver, ...)`，成功则返回 `Py_None`。注意开头的 `PyCriticalSectionGuard`（仅自由线程构建）：launch 路径全程持锁，保证并发安全。

**真正投放网格**：`launch` 函数构造 `CUlaunchConfig` 并调用驱动 API。

[cext/tile_kernel.cpp:2580-2628](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/cext/tile_kernel.cpp#L2580-L2628) 先 `prepare_launch` 拿到 `PreparedLaunch`（含已编译的 `CUkernel` 与动态共享内存大小），再把 grid/block/sharedMem/stream/attrs 填进 `CUlaunchConfig`，最后：

```cpp
CUresult res = driver->cuLaunchKernelEx(
        &config,
        reinterpret_cast<CUfunction>(prep->kernel),
        make_launch_params(*prep->helper),   // 指向 arena 的参数指针数组
        nullptr);
```

[cext/tile_kernel.cpp:375-381](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/cext/tile_kernel.cpp#L375-L381) `make_launch_params` 把 `helper.cuarg_offsets`（每个参数在 arena 中的偏移）转成 `void**` 数组——这正是 `cuLaunchKernel` 期望的"每个元素指向一个参数字节缓冲"的格式。

**编译回调**：`prepare_launch` 在缓存未命中时调用 `compile`，后者回调 Python 的 `dispatcher._compile`。

[cext/tile_kernel.cpp:2112-2119](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/cext/tile_kernel.cpp#L2112-L2119) 用 `PyObject_CallMethod(dispatcher_pyobj, "_compile", "(OO)", signature, py_tile_context)` 调用 Python 方法，返回值是 `(cubin_bytes, cufunc_name, dyn_smem_prog, hoisted_tensor_maps)` 四元组。这个 `_compile` 就是 `kernel` 类上的方法：

[src/cuda/tile/_execution.py:125-130](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_execution.py#L125-L130) `kernel._compile` 调用 `compile_tile(self._annotated_function, (signature,), get_sm_arch(), self._compiler_options, context)`，取出 cubin 与符号名回传给 C++。C++ 再用 `load_cuda_kernel` 把 cubin 字节流加载成 `CUkernel` 句柄。

> 至此可以回答实践任务的第一问：从 Python 到 `cuLaunchKernel` 之间依次发生了——参数解析 → 摊平 tuple → profile 查表 → 抽取 CUDA 参数（写 arena + 收集常量）→ 常量查表 → （未命中时）选调用约定 → 构造 KernelSignature → 回调 `_compile`/`compile_tile` 生成 cubin → 加载 cubin → 两级缓存写入 → 计算 dyn_smem → 填 `CUlaunchConfig` → `cuLaunchKernelEx`。

#### 4.1.4 代码实践

**实践目标**：跟踪一次真实的 `ct.launch` 调用，把上面 4.1.2 的每一步对应到具体源码行。

**操作步骤**：

1. 打开 `test/test_tuple_arguments.py`，定位最简单的 `test_tuple_scalar_arg`（约 28-36 行），它启动 `kernel_scalar_tuple`，参数为 `(a, out, (3, 7))`。
2. 在该 `ct.launch` 处下断点（或在 `kernel._compile` 内打日志），单步观察调用顺序。
3. 对照本讲 4.1.2 的伪代码链，标注每一步实际命中的源码位置。

**需要观察的现象**：

- 第一次启动会走进 `prepare_launch` 的「慢路径」（profile 缓存未命中），触发 `_compile` → `compile_tile`。
- 第二次以**相同参数类型**启动，应命中 profile 缓存；若常量取值也相同，连 `_compile` 都不会再被调用。

**预期结果**：第二次 `ct.launch` 的耗时显著低于第一次（编译被跳过）。「待本地验证」：具体加速比取决于机器与内核大小。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `launch` 的 `kernel` 参数必须是 `TileDispatcher` 的子类对象，而不能是任意可调用对象？

<details><summary>参考答案</summary>

因为 C++ 侧 `prepare_launch` 通过 `py_unwrap<TileDispatcher>(dispatcher_pyobj)` 把这个对象**按 C++ `TileDispatcher` 内存布局**解包，读取其 `param_annotations` 字段来做签名推断（[cext/tile_kernel.cpp:2479-2480](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/cext/tile_kernel.cpp#L2479-L2480)）。`@ct.kernel` 装饰器让 Python `kernel` 类继承 C++ `TileDispatcher`，正是为了让对象同时承载 Python 元数据与 C++ 调度状态。任意可调用对象没有这块 C++ 内存，解包会崩溃。
</details>

**练习 2**：`launch_impl` 中 `with_block=False`（普通 launch）时，block 维度是什么？为什么 cuTile 不需要用户指定 thread_count？

<details><summary>参考答案</summary>

block 维度被填成 `{1,1,1}`（见 [cext/tile_kernel.cpp:2600-2611](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/cext/tile_kernel.cpp#L2600-L2611) 的 `block.dims` 来自 `launch_args.block`，普通 launch 不解析它故保持默认）。因为 cuTile 表达的是 **block 级并行**，不暴露单个线程（见 u2-l1）；具体的 warp/线程映射由编译器在 cubin 内部决定，对用户不可见，所以无需也不应让用户指定 thread_count。需要显式 block/grid cluster 控制时才用 `launch_extended`。
</details>

### 4.2 TileDispatcher：注解树初始化与两级调度缓存

#### 4.2.1 概念说明

`TileDispatcher` 是横跨 Python/C++ 的核心调度对象。它在 Python 侧是 `kernel` 的基类（[src/cuda/tile/_execution.py:61](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_execution.py#L61) `class kernel(TileDispatcher)`），在 C++ 侧持有两类状态：

1. **`param_annotations`**：参数注解树（`ParameterAnnotationNode` 列表），在 `__init__` 时由 Python 传入，描述每个参数是标量/数组/常量/列表，以及 tuple 的嵌套结构。这是 u5-l1 讲过的「统一注解树取代旧扁平布尔掩码」的落地。
2. **`default_context_dispatcher`**：调度缓存容器，内含 `arg_profiles`（profile → 推断结果）与 `kernel_families`（按参数形态分组的内核族）。

本模块的关键洞见是：**注解树是静态的（装饰期固定），而参数画像是动态的（每次 launch 才知道）**。`TileDispatcher` 把两者在「首次见到某画像」时缝合，之后命中缓存即可。

#### 4.2.2 核心流程

C++ `TileDispatcher` 结构非常精简，真正的状态都在 `TileContextDispatcher` 里：

```cpp
struct TileDispatcher {
    Vec<RefPtr<ParameterAnnotationNode>> param_annotations;   // 装饰期固定
    TileContextDispatcher default_context_dispatcher;          // 运行时增长的缓存
};
struct TileContextDispatcher {
    ProfileMap arg_profiles;            // 参数类型序列 → PythonArgProfile
    Vec<RefPtr<KernelFamily>> kernel_families;  // 按参数形态分组的内核族
};
```

每次 `prepare_launch` 的调度逻辑：

1. `flatten_pyargs` 把 Python 参数（含嵌套 tuple）递归摊平成一个类型序列，tuple 用哨兵 `kTupleEndType` 标记结构边界。
2. 用这个类型序列在 `arg_profiles` 里查表。
   - **命中（热路径）**：直接拿到预先算好的 `PythonArgProfile`（含 kernel family 引用、参数画像、摊平后的叶子注解），跳过所有签名推断。
   - **未命中（冷路径）**：校验参数个数 → `get_parameter_and_pyarg_kinds` 得到 `param_kinds`（含 `TupleBegin` 标记）→ `flatten_parameter_annotation_nodes` 把注解树按 param_kinds 结构摊平成叶子注解 → 写入 `arg_profiles`。
3. `extract_cuda_args` 用叶子注解逐个把参数写进 arena，并把 `Constant` 取值收集进 `helper.constants`。
4. 用 `helper.constants` 在 kernel family 的 `kernels_by_constants` 里查表；未命中才走 4.1 的编译路径。

#### 4.2.3 源码精读

**Python 侧初始化**：`kernel.__init__` 把注解树传给 C++ 基类。

[src/cuda/tile/_execution.py:101-123](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_execution.py#L101-L123) 先用 `get_annotated_function(function)` 解析出注解，构造 `CompilerOptions`，然后 `super().__init__(ann_func.parameter_annotations)`——只传注解树这一个参数（旧的多个布尔掩码已不存在，这是 u5-l1 重构的核心）。`kernel.__call__` 被显式禁止直接调用：

[src/cuda/tile/_execution.py:168-169](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_execution.py#L168-L169) 抛出 "Use cuda.tile.launch() instead"。

**C++ 侧 `TileDispatcher.__init__`**：接收并解析注解树。

[cext/tile_kernel.cpp:2979-2993](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/cext/tile_kernel.cpp#L2979-L2993) `TileDispatcher_init` 用 `PyArg_ParseTupleAndKeywords` 取出唯一的 `parameter_annotations` 对象，调 `parse_parameter_annotation_nodes_seq` 解析成 `Vec<RefPtr<ParameterAnnotationNode>>` 存入 `dispatcher.param_annotations`。这与存根声明一致：

[src/cuda/tile/_cext.pyi:55-57](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_cext.pyi#L55-L57) `TileDispatcher.__init__(self, parameter_annotations: Sequence)`。

**结构体定义**：

[cext/tile_kernel.cpp:2076-2081](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/cext/tile_kernel.cpp#L2076-L2081) C++ `TileDispatcher` 仅这两个字段。

**tuple 摊平**：

[cext/tile_kernel.cpp:2084-2110](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/cext/tile_kernel.cpp#L2084-L2110) `flatten_pyargs` 递归处理嵌套 tuple：遇到精确 `PyTuple_Type` 就递归摊平其元素，并在末尾压入 `kTupleEndType` 哨兵。注释指出 named tuple 子类不是精确 tuple，会落到叶子路径被 `classify_arg` 拒绝——这是为了把昂贵的 `PyTuple_Check()` 留在缓存未命中的冷路径，热路径只比 `Py_TYPE` 指针。

**prepare_launch 的双缓存查表**：

[cext/tile_kernel.cpp:2477-2549](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/cext/tile_kernel.cpp#L2477-L2549) 先 `flatten_pyargs`，再 `arg_profiles.find(helper->pyarg_types)`；命中则跳过整段慢路径。慢路径里先校验参数个数（[2484-2488](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/cext/tile_kernel.cpp#L2484-L2488)），再 `flatten_parameter_annotation_nodes` 把注解树按 param_kinds 摊平（[2500-2504](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/cext/tile_kernel.cpp#L2500-L2504)）。随后 `extract_cuda_args` 写 arena（[2522-2525](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/cext/tile_kernel.cpp#L2522-L2525)），最后 `kernels_by_constants.find(helper->constants)`（[2527-2549](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/cext/tile_kernel.cpp#L2527-L2549)）——只有常量取值也变了才会触发重新编译。

#### 4.2.4 代码实践

**实践目标**：观察两级缓存如何避免重复编译。

**操作步骤**：

1. 用以下最小内核（基于 `test_tuple_arguments.py` 的 `kernel_array_tuple` 模式）：

   ```python
   # 示例代码：观察 JIT 缓存
   import cuda.tile as ct, torch

   @ct.kernel
   def add_pair(pair, out):           # pair: tuple[Tensor, Tensor]
       a = ct.load(pair[0], (0, 0), (4, 4))
       b = ct.load(pair[1], (0, 0), (4, 4))
       ct.store(out, (0, 0), a + b)

   a = torch.ones(4, 4, device="cuda"); b = torch.full((4,4), 2.0, device="cuda")
   out = torch.zeros(4, 4, device="cuda")
   ```

2. 给 `kernel._compile` 打补丁计数（用 `unittest.mock.patch` 包一层计数器），或临时在 `_execution.py` 的 `_compile` 里加一行 `print`。
3. 连续启动三次：`ct.launch(torch.cuda.current_stream(), (1,), add_pair, ((a,b), out))`。

**需要观察的现象**：

- 第 1 次：profile 查表未命中 → 常量查表未命中 → `_compile` 被调用 1 次。
- 第 2、3 次：profile 命中且常量相同 → `_compile` 不再被调用。

**预期结果**：`_compile` 计数器最终为 1（同一参数形态 + 同一常量取值只编译一次）。「待本地验证」：具体打印实现取决于你选择的插桩方式。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `flatten_pyargs` 用精确类型比较 `ty == &PyTuple_Type`，而不是 `PyTuple_Check`？

<details><summary>参考答案</summary>

见 [cext/tile_kernel.cpp:2089-2093](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/cext/tile_kernel.cpp#L2089-L2093) 的注释：`flatten_pyargs` 在每次 dispatch 的热路径上运行，必须极快。`PyTuple_Check` 会匹配 named tuple 等子类（更贵），而 cuTile 只接受精确 `tuple`（named tuple 应被拒绝）。用精确指针比较把子类判定推迟到缓存未命中的 `classify_arg` 冷路径，热路径只花一次指针比较。
</details>

**练习 2**：profile 缓存与常量缓存是两层独立的，请举一个"profile 命中但常量未命中"的场景。

<details><summary>参考答案</summary>

内核 `def k(a, n: ct.Constant[int], out)`，先后用 `n=16` 与 `n=32` 启动。两次的参数类型画像都是 `(Tensor, int, Tensor)` → profile 命中；但常量取值不同（`helper.constants` 不同）→ `kernels_by_constants` 未命中 → 触发一次新的编译。这正是 u3-l5 讲过的"每个 Constant 取值单独编译一份 cubin"在调度层的体现。
</details>

### 4.3 DispatchMode：tile 函数从 host 调用的控制

#### 4.3.1 概念说明

`DispatchMode` 回答另一个问题：当一个用 `@ct.function` 标注的 tile 函数被**从 host 代码**直接调用时，该怎么办？这看似与 launch 无关，但它正是"执行空间"在运行时的 enforcement 机制，且与 static_eval 强相关。

cuTile 区分三种执行空间（u2-l1）：host code / SIMT code / tile code。`@ct.function(tile=True, host=False)`（默认）声明的函数只能在 tile code 里被翻译调用，不能在 host 真正运行。但 Python 层它仍是个普通函数对象，用户可能误调用——`DispatchMode` 就负责在这种"越界调用"时给出正确的错误。

它有两个模式：

- **`NormalMode`**（默认当前模式）：从 host 调用 tile 函数 → 报错 "Tile functions can only be called from tile code."
- **`StaticEvalMode`**：在 `static_eval`/`static_assert`/`static_iter`（u3-l5）的编译期求值期间被设为当前模式；此时若代码试图调用一个 tile 函数 → 抛 `TileStaticEvalError`，提示"不能在 static_eval 内调用 tile 函数"。

#### 4.3.2 核心流程

`DispatchMode` 用线程局部存储（`threading.local`）保存"当前模式"：

```
_current_mode : _CurrentModeTL (threading.local)
    .mode : DispatchMode = NormalMode()      # 默认

@ct.function(tile=True) def f(...):
    wrapped(*args, **kwargs):
        → DispatchMode.get_current().call_tile_function_from_host(wrapped, args, kwargs)

static_eval(expr) 进入时:
    with StaticEvalMode(kind).as_current():
        ... eval expr ...        # 期间 _current_mode.mode 是 StaticEvalMode
    # 退出时恢复 NormalMode
```

`as_current` 是个上下文管理器：进入时把当前模式压栈替换，退出时恢复旧值，保证嵌套与异常安全。

#### 4.3.3 源码精读

**`@ct.function` 的包装**：

[src/cuda/tile/_execution.py:25-58](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_execution.py#L25-L58) 当 `host=False`（默认）时，返回的 `wrapped` 在被调用时执行 `DispatchMode.get_current().call_tile_function_from_host(wrapped, args, kwargs)`。注意 `host=True` 的函数直接返回原函数（可在 host 调用，如 `ct.cdiv`）。

**`DispatchMode` 体系**：

[src/cuda/tile/_dispatch_mode.py:16-31](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_dispatch_mode.py#L16-L31) 基类 `DispatchMode` 提供 `get_current()`（读线程局部）与 `as_current()`（上下文管理器压栈），并把 `call_tile_function_from_host` 留作 `NotImplementedError`。

[src/cuda/tile/_dispatch_mode.py:34-36](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_dispatch_mode.py#L34-L36) `NormalMode` 给出默认报错。

[src/cuda/tile/_dispatch_mode.py:39-54](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_dispatch_mode.py#L39-L54) `StaticEvalMode` 携带 `StaticEvalKind`，对 `static_eval`/`static_assert`/`static_iter` 自身给出专门提示，其余 tile 函数给出带函数名的 `TileStaticEvalError`。

[src/cuda/tile/_dispatch_mode.py:57-61](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_dispatch_mode.py#L57-L61) 线程局部 `_CurrentModeTL` 默认实例化为 `NormalMode()`。

> 注意 `kernel` 类**不走** `DispatchMode`——它直接重写 `__call__` 抛 TypeError（[src/cuda/tile/_execution.py:168-169](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_execution.py#L168-L169)）。`DispatchMode` 只管 `@ct.function` 标注的 tile 函数。

#### 4.3.4 代码实践

**实践目标**：亲手触发两种模式的报错，理解执行空间的 enforcement。

**操作步骤**：

```python
# 示例代码：触发 DispatchMode 报错
import cuda.tile as ct

@ct.function
def helper(x):          # tile-only function（默认 host=False）
    return x + 1

# 1) NormalMode 下从 host 调用
try:
    helper(5)
except RuntimeError as e:
    print("NormalMode:", e)

# 2) StaticEvalMode 下从 host 调用
@ct.kernel
def k(out):
    # 在 static_eval 内试图调用一个 tile 函数
    ct.static_eval(helper(1))   # 期望抛 TileStaticEvalError
    ct.store(out, (0,), ct.load(out, (0,), (1,)))
```

**需要观察的现象**：

- 场景 1 抛 `RuntimeError: Tile functions can only be called from tile code.`（来自 `NormalMode`）。
- 场景 2 抛 `TileStaticEvalError`（来自 `StaticEvalMode`，提示不能在 static_eval 内调用 `helper`）。

**预期结果**：两次都报错，但异常类型与文案不同，对应两个不同的 `DispatchMode` 子类。「待本地验证」：场景 2 的确切触发点取决于 ast2hir 如何进入 static_eval 求值上下文。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `DispatchMode` 用 `threading.local` 而不是模块级全局变量？

<details><summary>参考答案</summary>

因为 static_eval / static_assert 可能在多线程并发编译不同内核时被同时进入。`threading.local` 让每个线程拥有独立的"当前模式"栈，互不干扰；模块级全局则会让一个线程的 `StaticEvalMode` 污染另一个线程的 `NormalMode` 判定。`as_current` 的压栈/恢复也因此在每线程内独立正确。
</details>

**练习 2**：`host=True` 的 `@ct.function` 函数还会经过 `DispatchMode` 检查吗？

<details><summary>参考答案</summary>

不会。[src/cuda/tile/_execution.py:45-46](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_execution.py#L45-L46) 显示 `if host: return func`——直接返回原函数，不包 `wrapped`，因此既不会走 `DispatchMode.get_current()`，也不会有 `_cutile_function_wrapper` 标志。这正是 `ct.cdiv` 这类函数能在 host 与 tile 两处都用的原因。
</details>

### 4.4 CallingConvention：v1 的二进制接口

#### 4.4.1 概念说明

调用约定（calling convention）是导出/启动内核的**二进制接口契约**。文档 [docs/source/compilation.rst:63-71](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/docs/source/compilation.rst#L63-L71) 明确它定义三件事：

1. **二进制参数格式与顺序**：传给 `cuLaunchKernel` 的指针数组里每个参数是什么类型、什么顺序。
2. **支持的参数约束集合**：哪些 `ParameterConstraint` 子类被允许。
3. **名称修饰算法**：如何从函数名 + 签名推导出 cubin 符号名（详见 u8-l2）。

cuTile 提供两套约定：`cutile_python_v1`（原始）与 `cutile_python_v2`（v1 的超集，新增 tuple 与静态形状）。**新代码应使用 v2**。

#### 4.4.2 核心流程

v1 的二进制参数规则（来自 [docs/source/compilation.rst:73-146](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/docs/source/compilation.rst#L73-L146) 的表格）：

- 参数按 Python 内核函数声明顺序传入，**`Constant` 参数被整体省略**（它的值已烘焙进 cubin）。
- `ScalarConstraint`：单个对应 C 类型参数（如 `int32` → `int32_t`，`float64` → `double`）。
- `ArrayConstraint`：摊成 \(1 + 2n\) 个参数——1 个 base 指针 + \(n\) 个 shape + \(n\) 个 strides（\(n\) 为 ndim），shape/stride 的 C 类型由 `index_dtype` 决定（`int32`/`uint32`/`int64`）。
- `ListConstraint`（元素为数组）：2 个参数——一个指向列表存储的设备指针 + 一个 `int32_t` 长度。
- `ConstantConstraint`：省略。

这些字节正是 `extract_cuda_args` 写进 `helper.arena` 的内容，`make_launch_params` 再把它们以指针数组形式交给 `cuLaunchKernel`。所以"调用约定填充 cuLaunchKernel 参数"的本质是：`extract_cuda_args` 按约定把每个参数编码成连续字节并记录偏移，`cuLaunchKernel` 拿着这些偏移的指针数组投放网格。

#### 4.4.3 源码精读

**文档权威定义**：

[docs/source/compilation.rst:63-79](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/docs/source/compilation.rst#L63-L79) 给出 v1/v2 关系：两者二进制参数顺序一致（按声明顺序，Constant 省略），v2 在 v1 基础上增加 tuple（`TupleConstraint`）与静态形状（`ArrayConstraint.shape_constant`）。

**`CallingConvention` Python 接口**：

[src/cuda/tile/_cext.pyi:77-100](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_cext.pyi#L77-L100) 暴露 `cutile_python_v1()` / `cutile_python_v2()` 静态工厂、`from_code(code)`、`name` / `code` / `version` 属性。`version` 是核心门控字段。

**C++ 实现**：

[cext/tile_kernel.cpp:125-127](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/cext/tile_kernel.cpp#L125-L127) 枚举 `CallConvVersion { CutilePython_V1 = 1, CutilePython_V2 = 2 }`。

[cext/tile_kernel.cpp:129-137](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/cext/tile_kernel.cpp#L129-L137) C++ `CallingConvention` 结构仅持 `version` 字段，相等比较也只比 version。

[cext/tile_kernel.cpp:139-152](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/cext/tile_kernel.cpp#L139-L152) 三个 getter：`name` 形如 `"cutile_python_v2"`、`code` 形如 `"t2"`（用于 mangled name 的版本标记，见 u8-l2）、`version` 返回整数 1/2。

[cext/tile_kernel.cpp:178-188](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/cext/tile_kernel.cpp#L178-L188) `cutile_python_v1`/`v2` 用函数局部 `static` 缓存单例，每次返回 `Py_NewRef`。

#### 4.4.4 代码实践

**实践目标**：用 `KernelSignature.from_kernel_args` 看清 v1 下参数如何摊平成约束。

**操作步骤**：

```python
# 示例代码：观察 v1 签名
import cuda.tile as ct
from cuda.tile.compilation import KernelSignature, CallingConvention
import torch

@ct.kernel
def k(a, s, out):     # a: Array, s: int scalar, out: Array
    t = ct.load(a, (0,), (8,)) + s
    ct.store(out, (0,), t)

a = torch.zeros(8, dtype=torch.float32, device="cuda")
out = torch.zeros(8, dtype=torch.float32, device="cuda")
sig = KernelSignature.from_kernel_args(
    k, (a, 3, out), CallingConvention.cutile_python_v1())
print(sig)             # 三个 ParameterConstraint：Array, Scalar, Array
print(sig.calling_convention.version)   # 1
```

**需要观察的现象**：签名含三个约束——两个 `ArrayConstraint`（每个将摊成 1+2·1=3 个 cuLaunchKernel 参数）与一个 `ScalarConstraint`（1 个参数）。`Constant` 无，故不被省略。

**预期结果**：`calling_convention.version == 1`，符号名由 `with_mangled_symbol` 自动生成。「待本地验证」：`from_kernel_args` 还可能从示例数组的对齐情况推出额外假设（文档警告），生产环境应手写约束。

#### 4.4.5 小练习与答案

**练习 1**：一个 `ArrayConstraint(dtype=float32, ndim=2, index_dtype=int32)` 在 `cuLaunchKernel` 的参数数组里占几个槽？分别是什么？

<details><summary>参考答案</summary>

占 \(1 + 2 \times 2 = 5\) 个槽：1 个 base 设备指针（指向数组数据起点）+ 2 个 `int32_t` shape + 2 个 `int32_t` strides。见 [docs/source/compilation.rst:97-101](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/docs/source/compilation.rst#L97-L101)。
</details>

**练习 2**：为什么 `Constant` 参数要"整体省略"而不是传一个占位？

<summary>参考答案</summary>

因为 Constant 的值在编译期已被 `loosely_typed_const` 烘焙进 cubin（u3-l5），运行时签名里根本没有这个参数的位置——cubin 的入口符号也不知道它。若传占位反而会错位后续参数。所以约定规定它从 `cuLaunchKernel` 参数列表中消失，这正是 `extract_cuda_args` 只把它收进 `helper.constants`（用作缓存键）而不写进 arena 的原因。
</details>

### 4.5 v2 的扩展：tuple 参数与静态形状的门控

#### 4.5.1 概念说明

`cutile_python_v2` 在 v1 基础上新增两类能力（[docs/source/compilation.rst:77-79](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/docs/source/compilation.rst#L77-L79)）：

1. **tuple 参数**（`TupleConstraint`）：把一组相关参数打包成一个 Python `tuple` 传入，元素可以是标量或数组。二进制上，tuple 的元素**像顶层参数一样连续展开**（[docs/source/compilation.rst:134-143](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/docs/source/compilation.rst#L134-L143)）。整组 `Constant[tuple]` 时整个 tuple 被省略。
2. **静态形状**（`ArrayConstraint.shape_constant`）：把数组某些维度特化为编译期常量（u3-l7）。

**为什么这两者需要新约定？** 因为 v1 的 mangling 算法与二进制布局不知道如何编码 tuple 的嵌套结构与静态形状标记——继续用 v1 会导致符号名冲突或参数错位。所以 v2 既扩展了二进制布局，也扩展了 mangling（u8-l2 的 `T` 前缀与 `s` 谓词）。

#### 4.5.2 核心流程

v2 的"必要性"在两个地方被强制：

1. **C++ 自动升级**：`minimum_calling_convention` 检测到参数里含 tuple 或静态形状时，直接返回 V2——JIT 路径自动选 v2，用户无需干预。
2. **Python 显式校验**：AOT 导出（`KernelSignature.__init__`）时，`_validate_constraint_support` 递归检查每个约束，若用了 tuple/静态形状但调用约定 version < 2，直接抛 `ValueError`。

两者协同：JIT 自动选对约定，AOT 则要求用户显式声明 v2 并校验一致性。

```
JIT 路径:
  minimum_calling_convention(param_kinds, flat_annotations)
      ├─ 任一 param_kind == TupleBegin           → V2
      ├─ 任一 leaf 的 static_shape_dims 非空      → V2
      └─ 否则                                     → V1

AOT 路径:
  KernelSignature(parameters, calling_convention)
      └─ 对每个 parameter 调 _validate_constraint_support(p, cconv)
            ├─ ArrayConstraint 且 shape_constant 非空且 version<2 → ValueError
            ├─ TupleConstraint 且 version<2                       → ValueError
            └─ （递归检查 List/Tuple 的子约束）
```

#### 4.5.3 源码精读

**C++ 自动选约定**：

[cext/tile_kernel.cpp:1735-1747](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/cext/tile_kernel.cpp#L1735-L1747) `minimum_calling_convention`：扫 `param_kinds` 若有 `TupleBegin` 返回 V2；再扫叶子注解，若任一 `static_shape_dims`（数组或列表元素）非空返回 V2；否则 V1。这个结果在 `prepare_launch` 编译未命中时被用来 `make_signature`（[cext/tile_kernel.cpp:2531-2539](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/cext/tile_kernel.cpp#L2531-L2539)）。

**Python 显式校验**：

[src/cuda/tile/compilation/_signature.py:511-529](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/compilation/_signature.py#L511-L529) `_validate_constraint_support`：

- `ArrayConstraint` 有非空 `shape_constant` 且 `cconv.version < 2` → 报 "Static array shapes are not supported ... version >= 2 is required"。
- `TupleConstraint` 且 `version < 2` → 报 "Tuple parameters are not supported ... version >= 2 is required"，并递归检查其 `items`。
- `ListConstraint` 递归检查其 `element`。

这个校验在 `KernelSignature.__init__` 里对每个参数调用：

[src/cuda/tile/compilation/_signature.py:321-338](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/compilation/_signature.py#L321-L338) 构造时 `for x in parameters: _validate_constraint_support(x, calling_convention)`。注意这一校验**移入了 `KernelSignature` 构造器**（见本仓库提交 `669ef7f`），意味着无论是 AOT 的 `KernelSignature(...)` 还是 JIT 经 `make_signature` 间接构造，都会经过同一道门。

**真实 tuple 测试**：

[test/test_tuple_arguments.py:21-64](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_tuple_arguments.py#L21-L64) 给出三种基本 tuple 用法：`tuple[int,int]`、`tuple[Tensor,Tensor]`、`tuple[Tensor,int]`，分别对应 `kernel_scalar_tuple`（21 行）、`kernel_array_tuple`（36 行）、`kernel_mixed_tuple`（52 行）。启动时直接把 Python `tuple` 作为单个参数传入，如 `((a, b), out)`。

#### 4.5.4 代码实践

**实践目标**：验证"使用 tuple 参数会自动要求 v2"，并对比 v1 下的报错。

**操作步骤**：

```python
# 示例代码：tuple 触发 v2
import cuda.tile as ct
from cuda.tile.compilation import KernelSignature, CallingConvention
import torch

@ct.kernel
def add_pair(pair, out):           # pair: tuple[Tensor, Tensor]
    a = ct.load(pair[0], (0, 0), (4, 4))
    b = ct.load(pair[1], (0, 0), (4, 4))
    ct.store(out, (0, 0), a + b)

a = torch.ones(4, 4, device="cuda"); b = torch.full((4,4), 2.0, device="cuda")
out = torch.zeros(4, 4, device="cuda")

# 1) JIT：直接 launch，C++ 会自动选 v2（无需声明）
ct.launch(torch.cuda.current_stream(), (1,), add_pair, ((a, b), out))
print("JIT launch OK")

# 2) AOT：故意用 v1 构造签名，期望被 _validate_constraint_support 拒绝
try:
    sig = KernelSignature.from_kernel_args(
        add_pair, ((a, b), out), CallingConvention.cutile_python_v1())
except ValueError as e:
    print("AOT v1 rejected:", e)

# 3) AOT：改用 v2，成功
sig2 = KernelSignature.from_kernel_args(
    add_pair, ((a, b), out), CallingConvention.cutile_python_v2())
print("AOT v2 OK, version =", sig2.calling_convention.version)
```

**需要观察的现象**：

- 场景 1：JIT 正常启动（C++ `minimum_calling_convention` 检测到 `TupleBegin` 自动选 v2）。
- 场景 2：抛 `ValueError: Tuple parameters are not supported by calling convention cutile_python_v1; version >= 2 is required`。
- 场景 3：成功，`version == 2`。

**预期结果**：三步行为如上。这正面回答实践任务第二问——**tuple 参数要求 v2，是因为 v1 的二进制布局与 mangling 不编码 tuple 结构，`_validate_constraint_support` 在签名构造期（AOT）或 `minimum_calling_convention` 在编译期（JIT）强制升级到 v2**。

> 综合实践任务回顾：`cuLaunchKernel` 所需参数由调用约定填充——`extract_cuda_args` 按约定把每个约束编码成连续字节写进 `arena`（标量→1 个 C 类型值；数组→base+shape+strides；tuple→元素连续展开；Constant→省略只进缓存键），`make_launch_params` 再以指针数组交给 `cuLaunchKernelEx`；而 tuple 要求 v2，因为只有 v2 的约定（含 `T` mangling 与连续展开语义）能正确表达 tuple 结构。

#### 4.5.5 小练习与答案

**练习 1**：JIT 路径下，用户需要手动指定调用约定吗？为什么？

<details><summary>参考答案</summary>

不需要。JIT 路径里 `prepare_launch` 调 `minimum_calling_convention(param_kinds, flat_annotations)` 自动推断：有 tuple 或静态形状就选 v2，否则 v1（[cext/tile_kernel.cpp:2531-2539](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/cext/tile_kernel.cpp#L2531-L2539)）。调用约定只在 AOT 导出（`export_kernel`/`KernelSignature`）时需要用户显式选择，因为 AOT 没有"运行时参数"可推断。
</details>

**练习 2**：一个 `tuple[Tensor, int]` 参数在 v2 下，`cuLaunchKernel` 参数数组里展开成什么？

<details><summary>参考答案</summary>

按 [docs/source/compilation.rst:134-138](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/docs/source/compilation.rst#L134-L138)，tuple 元素像顶层参数一样连续展开。假设 Tensor 是 1D、`index_dtype=int32`：第一个元素（数组）展开成 base 指针 + 1 个 shape + 1 个 stride（共 3 个），第二个元素（int 标量）展开成 1 个 `int32_t`。合计在该 tuple 位置占 4 个连续槽，紧接着才是下一个顶层参数。
</details>

## 5. 综合实践

把本讲全部最小模块串起来，画一张「一次 tuple 内核 launch 的完整时序图」，并标注每一步的源码位置与调用约定版本演化。

任务：以 `test_tuple_arguments.py` 的 `kernel_mixed_tuple`（`tuple[Tensor, int]`）为对象，按下面顺序产出一份说明文档：

1. **装饰阶段**：`@ct.kernel` 触发 `kernel.__init__`，把 `get_annotated_function` 产出的 `parameter_annotations`（含一个 `HeterogeneousTupleNode`）传给 C++ `TileDispatcher.__init__`（标注 [src/cuda/tile/_execution.py:114-121](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_execution.py#L114-L121) 与 [cext/tile_kernel.cpp:2979-2993](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/cext/tile_kernel.cpp#L2979-L2993)）。
2. **首次 launch**：画出 `cuda_tile_launch → launch_impl → launch → prepare_launch`，标注 `flatten_pyargs` 如何把 `((data, 5), out)` 摊平成 `[Tensor, int, kTupleEnd, Tensor]`（标注 [cext/tile_kernel.cpp:2084-2110](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/cext/tile_kernel.cpp#L2084-L2110)）。
3. **签名推断**：profile 未命中 → `minimum_calling_convention` 因 `TupleBegin` 选 **v2** → `make_signature` 构造 `KernelSignature`（此时 `_validate_constraint_support` 通过，因 version=2）→ 回调 `kernel._compile` → `compile_tile` 出 cubin（标注 [cext/tile_kernel.cpp:1735-1747](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/cext/tile_kernel.cpp#L1735-L1747)、[2531-2545](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/cext/tile_kernel.cpp#L2531-L2545)）。
4. **投放**：`extract_cuda_args` 按 v2 把 tuple 元素连续写入 arena，`make_launch_params` 生成指针数组，`cuLaunchKernelEx` 启动（标注 [cext/tile_kernel.cpp:375-381](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/cext/tile_kernel.cpp#L375-L381) 与 [2613-2617](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/cext/tile_kernel.cpp#L2613-L2617)）。
5. **二次 launch**：标注两级缓存（profile + constants）命中，跳过推断与编译。

最后用一句话回答：若把 `kernel_mixed_tuple` 的 `pair` 改成 `Constant[tuple[Tensor, int]]`，签名推断与 cuLaunchKernel 参数会有什么变化？（提示：整组常量被省略，且所有元素必须都是 `ConstantConstraint`，见 [docs/source/compilation.rst:140-143](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/docs/source/compilation.rst#L140-L143)；但数组不能做 Constant，所以这个标注本身会被拒绝——可借此检验理解。）

## 6. 本讲小结

- `ct.launch` 是 C++ 扩展函数，链路为 `cuda_tile_launch → launch_impl → launch → prepare_launch → compile(_compile/compile_tile) → cuLaunchKernelEx`，全程持锁保证并发安全。
- `prepare_launch` 用**两级缓存**（profile 缓存按参数类型序列、kernel family 的常量缓存按 Constant 取值）把热路径压到查表 + 投放。
- `TileDispatcher` 现以**单一参数注解树 `parameter_annotations`** 初始化（取代旧的多个布尔掩码）；Python `kernel` 继承它，C++ 侧解析注解树并维护调度缓存。
- `DispatchMode`（`NormalMode`/`StaticEvalMode`）用线程局部存储控制 `@ct.function` 标注的 tile 函数能否从 host 调用；`kernel` 不走它，直接在 `__call__` 报错。
- 调用约定定义二进制参数格式、支持的约束集合与 mangling；v1 与 v2 顺序一致，**v2 额外支持 tuple 与静态形状**，新代码应使用 v2。
- tuple/静态形状在 JIT 由 `minimum_calling_convention` 自动升级到 v2，在 AOT 由 `KernelSignature.__init__` 的 `_validate_constraint_support` 显式门控（version < 2 即抛 `ValueError`）。

## 7. 下一步学习建议

- **u8-l2（AOT 导出与内核签名/名称修饰）**：本讲只讲了调用约定的二进制接口与门控，下一讲深入 `export_kernel`、`mangle_kernel_name`（含 tuple 的 `T` 前缀与静态形状的 `s` 谓词编码）与可逆 demangle。
- **u8-l4（JAX / XLA FFI 互操作）**：`cutile_call` 同样基于 `parameter_annotations` 注解树按叶子映射 buffer/constraint，并对 tuple 参数显式抛 `NotImplementedError`——可对比本讲的门控逻辑。
- **重读 u5-l1 与 u7-l3**：本讲的 `TileDispatcher.param_annotations` 与 `kernel._compile → compile_tile` 正是这两讲的运行时出口，回头对照能加深"装饰期静态注解 + 运行时动态画像"的缝合理解。
- 想动手验证本讲结论，可直接跑 `pytest test/test_tuple_arguments.py -k "tuple"`，对照 4.5 的门控与 4.2 的缓存行为。
