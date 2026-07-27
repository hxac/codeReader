# host/device 拆分、库生成与编译回调

## 1. 本讲目标

上一讲（u7-l1）解决了「`CompiledArtifact`（IR 与源码字符串）如何变成可调用对象」的问题，重点在 **execution_backend 与 adapter**。本讲往前一步，回答两个更底层的问题：

1. 一个 tilelang kernel 在编译流水线里，是怎么从「单个 PrimFunc」被切成 **host 部分（CPU 上的启动与参数打包）** 和 **device 部分（GPU 上的 `__global__` kernel）** 的？
2. 切开之后，这两半又是如何各自落地成「可被 adapter 加载执行的东西」？为什么有的后端走 TVM `rt_mod`，有的后端却要自己 `nvcc` 出一个 `.so`？

学完后你应当能够：

- 说清 `SplitHostDevice`、`MakePackedAPI`、`LowerDeviceKernelLaunch` 三个 Pass 各自的职责，以及 `CallingConv.DEVICE_KERNEL_LAUNCH` 是谁打的、给谁用的。
- 区分两条「落地」路径：tvm_ffi 走 `host_codegen` + device codegen 回调；cython/nvrtc/torch/cutedsl 走 `wrapper` + `libgen`。
- 解释 `tilelang_callback_cuda_compile` 如何把 CUDA 源码变成 cubin/fatbin 并写入 `CUDABinaryCache`，以及为什么编译选项必须进缓存键。
- 跟着 verbose 日志与 dump 出的 IR，亲手定位 host/device 的边界。

## 2. 前置知识

- **host 与 device**：在 CUDA/HIP 编程模型里，「host」指 CPU（负责分配显存、准备参数、发起 kernel 启动），「device」指 GPU（跑真正计算密集的 `__global__` 函数）。一个可运行 kernel = 一段 host 启动代码 + 一段 device 计算代码。
- **PrimFunc 与 IRModule**：tilelang 的中间表示（TIR）里，一个 kernel 就是一个 `PrimFunc`；`IRModule` 是若干 `PrimFunc` 的容器。详见 u4-l1。
- **calling convention（CallingConv）**：函数调用约定的标注，挂在 PrimFunc 属性 `tvm::attr::kCallingConv` 上。本讲涉及两种：
  - `kCPackedFunc`：host 函数被改写成 TVM 的 C-PackedFunc 签名 `(self_handle, args, num_args, result)`，供运行时统一调用。
  - `kDeviceKernelLaunch`：标记「这是一个 device kernel，调用它要发起一次 kernel launch」。device codegen 会断言每个函数都带这个约定。
- **注册表 + 惰性加载**：tilelang 反复使用的模式——按 `target.kind.name` 把实现注册进一个 dict，首次用到时才 `import` 对应后端模块，用 `supports_target` 谓词区分变体。u4-l4、u7-l1 已见过它在 target/device codegen/execution backend 上的应用，本讲会看到它在 **host codegen** 上的同款应用。
- **两条落地路径（承接 u7-l1）**：adapter 把编译产物变成可调用对象有两条策略——策略 A 薄包装 TVM `rt_mod`（靠 DLPack 互通，对应 `tvm_ffi`）；策略 B 在构造时自编译源码、用 ctypes/cuda-python 启动（对应 `cython`/`nvrtc`/`torch`/`cutedsl`）。本讲解释这两条路径在「拆分 + 落地」阶段的差异。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/transform/split_host_device.cc](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/split_host_device.cc) | **C++ Pass**：把含 device region 的 PrimFunc 物理拆成 host 调用 + 新建的 device 函数 |
| [src/transform/make_packed_api.cc](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/make_packed_api.cc) | **C++ Pass**：把 host 函数改写成 C-PackedFunc 签名，加入参数类型校验 |
| [src/transform/lower_device_kernel_launch.cc](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/lower_device_kernel_launch.cc) | **C++ Pass**：给 device 函数打 `kDeviceKernelLaunch`、提取 launch 参数、改写调用点 |
| [tilelang/backend/host_codegen.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/backend/host_codegen.py) | host codegen 注册表：按 host target kind 注册 `HostCodegen`，惰性加载 |
| [tilelang/engine/lower.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/engine/lower.py) | 编译总入口：`lower_to_host_device_ir` 做 Filter 拆分；`host_codegen` 收尾；`tilelang_callback_cuda_compile` 在此注册 |
| [src/cuda/codegen/rt_mod_cuda.cc](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/cuda/codegen/rt_mod_cuda.cc) | device codegen：`BuildTileLangCUDA` 生成 CUDA 源码并回调 Python 编译 |
| [tilelang/jit/adapter/libgen.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/adapter/libgen.py) | **库生成器**：拼装 nvcc/hipcc/g++ 命令，subprocess 编出 `.so` |
| [tilelang/jit/adapter/wrapper.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/adapter/wrapper.py) | **源码封装器**：把 device 源码包上 host 的 `init()`/`call()` 启动函数 |
| [tilelang/cache/cuda_binary_cache.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cache/cuda_binary_cache.py) | cubin/fatbin 的跨进程磁盘缓存 |

## 4. 核心概念与源码讲解

### 4.1 host/device 拆分：SplitHostDevice Pass

#### 4.1.1 概念说明

在前端（u5）与大多数 lowering Pass（u6）阶段，tilelang 把一个 kernel 表达成**单个 PrimFunc**：函数体里既有「会跑在 GPU 上的 device region」，也有「该 region 外的 host 可执行语句」。这种「混在一起」的形式便于做循环调度、布局推理等变换，但最终必须拆开——因为 device 部分要编译成 `__global__` 函数，host 部分要编译成 CPU 上的启动逻辑。

`SplitHostDevice` 就是那个「动刀切开」的 Pass。它的契约是：**找到一个 PrimFunc 里的 device region，把它整体搬到新建的 device PrimFunc 里，并在原（host）函数体里留下一句「调用那个 device 函数」的语句。**

需要特别强调：`SplitHostDevice` 只做**物理拆分**——它**不**设置 `CallingConv`。真正给 device 函数打上 `kDeviceKernelLaunch` 标记的是后面紧接的 `LowerDeviceKernelLaunch` Pass（见 4.1.3）。这是本讲学习目标「host/device 拆分与 `CallingConv.DEVICE_KERNEL_LAUNCH` 的关系」的核心要点。

#### 4.1.2 核心流程

整个 Pass 的入口是模块级的 [split_host_device.cc:L617-L649](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/split_host_device.cc#L617-L649)：遍历 IRModule 里的每个 PrimFunc，给 device kernel 取名 `<global_symbol>_kernel`，调用 `SplitHostDevice(func, &device_mod, var_supply)`，最后把新建的 device 函数合并回模块并跑一遍 `ConvertSSA`。

真正干活的是 `HostDeviceSplitter`，它是一个 `StmtMutator`，递归访问函数体直到命中第一个 device region：

```
访问语句：
  若是 AttrStmt 且 attr_key == "target"：   # 找到 device region
      found_device_region_ = true
      取出 device target (target.WithoutHost())
      调用 SplitDeviceFunc(body, device_target)  # 切！
  若是 AttrStmt 且 attr_key == tilelang_assume：  # 收集 host 侧 assume
      压入 host_assumes_（须在 device region 之前）
```

对应代码 [split_host_device.cc:L84-L101](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/split_host_device.cc#L84-L101)。命中 device region 后，[SplitDeviceFunc](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/split_host_device.cc#L401-L560) 做五件事：

1. **抽取 device 参数**：用 `VarUseDefAnalyzer` 分析 device body 里用到但未定义的变量（含 buffer handle 与 shape 符号），排序后作为新 device 函数的形参（[L417-L426](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/split_host_device.cc#L417-L426)）。
2. **变量重命名**：为 device 函数新建一批 `Var`，避免与 host 函数共享 `Var` 对象导致 `ConvertSSA` 误改（[L444-L477](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/split_host_device.cc#L444-L477)）。
3. **搬运 assume**：把 host 侧收集到的 `tilelang_assume` 按 `name_hint` 匹配变量后包到 device body 外层，作为优化器事实（[L363-L388](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/split_host_device.cc#L363-L388)）。
4. **构造 device PrimFunc**：带上 `kTarget`、`kIsGlobalFunc=true`、`kNoAlias=true` 等属性，加入 `device_mod`（[L518-L540](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/split_host_device.cc#L518-L540)）。
5. **留下 host 调用**：把原 body 替换成一句 `Evaluate(Call(... kernel_symbol_global, args))`（GPU 情况），或 `Bind(kernel_error_code, kernel_call)` + 断言（CPU/ext_dev/hexagon，可传播错误码，[L545-L559](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/split_host_device.cc#L545-L559)）。

注意第 5 步里 host 对 device 的调用还是**普通的 GlobalVar 调用**，并不是真正的 kernel launch——这条调用语句要等 `LowerDeviceKernelLaunch` 改写。

此外，`SplitHostDevice` 还负责把若干属性从 host 函数「搬到」device 函数：`kNonRestrictParams`、`cluster_dims`、`kSmemAlignmentMap`，因为它们只在 device codegen 时才用得上（[L570-L613](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/split_host_device.cc#L570-L613)）。

#### 4.1.3 与 CallingConv.DEVICE_KERNEL_LAUNCH 的关系

三个 Pass 在 CUDA pipeline 里紧挨着运行（顺序见 [tilelang/cuda/pipeline.py:L213,L246-L248](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cuda/pipeline.py#L213)）：

```
SplitHostDevice          # 物理拆分：建 device func（kIsGlobalFunc），host 留 GlobalVar 调用
MergeSharedMemoryAllocations
...
MakePackedAPI            # 把 host 函数改成 C-PackedFunc（kCPackedFunc）；device 函数跳过
Simplify
LowerDeviceKernelLaunch  # 给 device 函数打 kDeviceKernelLaunch + 提取 launch 参数 + 改写调用点
```

- **`SplitHostDevice`** 建出来的 device 函数此时**没有**特殊 calling convention。
- **`MakePackedAPI`** 用 `RequiresPackedAPI` 判断哪些函数要改写：device 函数因为没有 host target 而被跳过（`target->GetHost()` 为空即原样返回，见 [make_packed_api.cc:L314-L319](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/make_packed_api.cc#L314-L319)）；host 函数则被打成 `kCPackedFunc`（[make_packed_api.cc:L612-L617](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/make_packed_api.cc#L612-L617)）。
- **`LowerDeviceKernelLaunch`** 才是真正「盖章」的 Pass：在 [lower_device_kernel_launch.cc:L260-L296](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/lower_device_kernel_launch.cc#L260-L296) 里给 device 函数写上 `kCallingConv = kDeviceKernelLaunch`、`kKernelLaunchParams`、`kGlobalSymbol`，并记下 `thread_extent`、`dyn_shared_memory_buf`、`cluster_dims` 等 launch 信息；同时把 host 侧那条 GlobalVar 调用改写成跨 target 的 kernel launch / call_extern。

拆分完之后，整个 IRModule 里同时住着 host 函数（`kCPackedFunc`）和 device 函数（`kDeviceKernelLaunch`）。`lower()` 正是**按 calling convention 把它们 Filter 开**的：

```python
host_mod   = tirx.transform.Filter(_is_host_call)(mod)
device_mod = tirx.transform.Filter(_is_device_call)(mod)
```

其中 `_is_device_call` 就是「`calling_conv == DEVICE_KERNEL_LAUNCH`」（[lower.py:L28-L54](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/engine/lower.py#L28-L54)、Filter 调用在 [L291-L292](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/engine/lower.py#L291-L292)）。而 device codegen（如 [rt_mod_cuda.cc:L113](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/cuda/codegen/rt_mod_cuda.cc#L113)）会对每个函数 `ICHECK(calling_conv == kDeviceKernelLaunch)`，确保只发射真正的 device kernel。

> 一句话总结这条因果链：`SplitHostDevice` 切开 → `MakePackedAPI` 给 host 半边换签名 → `LowerDeviceKernelLaunch` 给 device 半边盖章 → `Filter` 用这枚章把两半分拣 → codegen 依据这枚章只认 device 函数。

#### 4.1.4 代码实践

1. **实践目标**：在 dump 出的 IR 里亲眼看到「拆分前一个函数、拆分后两个函数」，并确认 device 函数最终带上了 `kDeviceKernelLaunch`。
2. **操作步骤**：
   - 准备一个最小 GEMM kernel（可直接复用 `examples/quickstart.py` 的 `@tilelang.jit` 写法）。
   - 编译时开启 IR dump，例如 `pass_configs={PassConfigKey.TL_ENABLE_DUMP_IR: True}`，或设环境变量让 dump 落到 `./dump_ir`（参见 u6-l1）。
   - 在 dump 目录里定位三个相邻 Pass 的 IR 文件：`tl.SplitHostDevice`、`tl.MakePackedAPI`、`tl.LowerDeviceKernelLaunch`。
3. **需要观察的现象**：
   - `SplitHostDevice` **之前**：只有一个 `PrimFunc`，函数体里能看到 `attr [0] = "thread_extent"` 与带 `target` 的 device region。
   - `SplitHostDevice` **之后**：模块里多出一个 `PrimFunc`，名字形如 `<name>_kernel`；原（host）函数体塌缩成一句对 `<name>_kernel` 的 `Call`。
   - `MakePackedAPI` **之后**：host 函数形参变成 `(self_handle, args, num_args, result)` 四元，属性里出现 `calling_conv = 1`（即 `kCPackedFunc`）。
   - `LowerDeviceKernelLaunch` **之后**：`<name>_kernel` 的属性里出现 `calling_conv = 2`（即 `kDeviceKernelLaunch`），并带 `global_symbol`、`thread_extent`、`launch_params` 等。
4. **预期结果**：你能指着 IR 说出「这一行是 host 调用、这一段是 device kernel」，并能复述三个 Pass 谁负责盖章。
5. **无 GPU 时的替代**：上面纯属源码阅读型实践，dump IR 需要 `libtilelang.so` 加载成功但**不需要 GPU**（`lower` 只做 IR 变换）。若连 native 库都没有，则改为直接阅读本节引用的三个 `.cc` 文件，对照 4.1.2/4.1.3 的流程图自行推演。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `LowerDeviceKernelLaunch` 从 pipeline 里删掉，device codegen 会在哪里、以什么方式报错？
**答案**：`BuildTileLangCUDA` 会对每个函数执行 `ICHECK(calling_conv == CallingConv::kDeviceKernelLaunch)`（[rt_mod_cuda.cc:L112-L114](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/cuda/codegen/rt_mod_cuda.cc#L112-L114)）。没有这个 Pass 盖章，device 函数仍是默认 calling convention，ICHECK 直接失败。

**练习 2**：为什么 `SplitHostDevice` 要给 device 函数新建一批 `Var`，而不是复用 host 函数里的 `Var`？
**答案**：因为拆分后 host 和 device 是同一个 IRModule 里的两个独立函数。若共用 `Var` 对象，pipeline 末尾的 `ConvertSSA`（[split_host_device.cc:L644](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/split_host_device.cc#L644)）在处理多函数时会误判变量作用域、错误重命名。

### 4.2 host 侧收尾：MakePackedAPI 与 host_codegen 注册表

#### 4.2.1 概念说明

切开之后，host 函数还只是一段普通 TIR。要让它能被 TVM 运行时（以及最终的 adapter）以统一方式调用，需要两步：

1. **`MakePackedAPI`**：把 host 函数改写成 TVM 的 **C-PackedFunc** 调用约定——固定四参数签名 `(self_handle, args, num_args, result)`，函数体里加入「逐个从 `args` 取参数、校验类型、绑定 DLTensor / 标量」的模板代码。这是让任意 backend 的 host 函数都长得一样的「标准化」步骤。
2. **`host_codegen`**：再做一轮与 backend 无关的收尾 lowering（`LowerTVMBuiltin`、`LowerCustomDatatypes`、`LowerIntrin` 等），最后按 host target（llvm/c）落到一个 TVM runtime module（`rt_mod`）。

注意：`host_codegen` **只对 `tvm_ffi` 这一类 adapter 有用**——因为只有它需要 TVM 的 `rt_mod`。对 `cython`/`nvrtc` 等自己生成 `.so` 的后端，host 侧逻辑由 `wrapper` 另写（见 4.3），`enable_host_codegen` 为 `False`，`host_codegen` 根本不会被调用。

#### 4.2.2 核心流程

`MakePackedAPI` 的逐函数入口在 [make_packed_api.cc:L296-L663](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/make_packed_api.cc#L296-L663)，要点：

- `RequiresPackedAPI` 决定哪些函数要改：已有非默认 calling_conv 的跳过、source kernel 跳过、无 `global_symbol` 的（内部辅助函数）跳过（[L271-L294](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/make_packed_api.cc#L271-L294)）。
- 改写后的签名固定为 `(v_self_handle, v_packed_args, v_num_packed_args, v_result)`（[L588-L590](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/make_packed_api.cc#L588-L590)）。
- 对每个形参插入类型断言：handle 期望指针/Tensor、整数期望 int、浮点期望 float（且允许 int→float 容错），并处理 `Tensor` 句柄到 `DLTensor*` 的偏移（[L490-L586](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/make_packed_api.cc#L490-L586)）。
- 最后写上 `calling_conv = kCPackedFunc`、`global_symbol = __tvm_ffi_<name>`（[L612-L617](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/make_packed_api.cc#L612-L617)）——这个前缀就是运行时按名字找到 host 函数的依据。

`host_codegen` 的实现是一条固定的 lowering 链 + 一次注册表查询（[lower.py:L215-L239](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/engine/lower.py#L215-L239)）：

```python
host_mod = BindTarget(target_host)
host_mod = FP8StorageLegalize / BF16StorageLegalize / LowerTVMBuiltin / LowerCustomDatatypes
host_mod = tilelang.transform.LowerIntrin
host_mod = CombineContextCall
host_mod = apply_host_codegen_hooks(host_mod, target_host, target)  # 设备后端的钩子
return resolve_host_codegen(target_host).lower(host_mod, target_host)  # 真正产出 rt_mod
```

注册表 [host_codegen.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/backend/host_codegen.py) 与 device codegen / execution backend 完全同构：

- `HostCodegen` 是一个 `dataclass`，持有一个 `build(mod, target_host) -> IRModule` 回调与可选 `supports_target` 谓词（[L28-L41](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/backend/host_codegen.py#L28-L41)）。
- 按 `target_host_kind`（如 `"llvm"`、`"c"`）注册，`register_lazy_host_codegen` 记下「用到时再 import」的模块路径（[L67-L131](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/backend/host_codegen.py#L67-L131)）。
- `resolve_host_codegen(target_host)` 取首个 `matches` 的实现（[L166-L174](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/backend/host_codegen.py#L166-L174)）。

各 host target 的具体注册在 [tilelang/cpu/codegen.py:L31-L41](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cpu/codegen.py#L31-L41)：`llvm` 走 TVM 的 `target.build.llvm`，`c` 走 `target.build.tilelang_c_host`。`global_func_host_codegen(name)` 把这些 TVM 全局函数包成 `HostCodegen.build`（[host_codegen.py:L19-L25](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/backend/host_codegen.py#L19-L25)）。

此外还有一类 `HostCodegenHook`：在真正 `build` 之前，让**设备后端**插一手改 host IR（例如 Metal 后端往 host 里塞 MPS 同步逻辑），由 `apply_host_codegen_hooks` 按 device target kind 依次应用（[host_codegen.py:L43-L64](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/backend/host_codegen.py#L43-L64)、[lower.py:L238](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/engine/lower.py#L238)）。

#### 4.2.3 源码精读

host codegen 注册表是本讲最小模块 `tilelang.backend.host_codegen` 的全部内容。它的「注册 + 惰性加载 + 谓词匹配」三件套与 [tilelang/backend/device_codegen.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/backend/device_codegen.py) 几乎逐行对称——把这份文件和 device codegen 对照看，就能一眼看出 tilelang 后端抽象的统一套路。`DeviceCodegen.lower` 用 `compile_device` 开关在「真编译」与「只生成源码」之间切换（[device_codegen.py:L39-L44](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/backend/device_codegen.py#L39-L44)），这正是 4.3/4.4 两条路径的分叉点。

#### 4.2.4 代码实践

1. **实践目标**：确认 `host_codegen` 只在 `tvm_ffi` 路径下运行，且产出的 `rt_mod` 非空。
2. **操作步骤**：阅读 [tilelang/cuda/execution_backend.py:L36-L41](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cuda/execution_backend.py#L36-L41)，对比 `tvm_ffi`（`enable_host_codegen=True, enable_device_compile=True`）与其余后端（默认 `False`）。再阅读 [lower.py:L319-L333](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/engine/lower.py#L319-L333) 看 `enable_host_codegen` 如何决定 `CompiledArtifact.rt_mod` 是否非空。
3. **需要观察的现象**：只有 `enable_host_codegen=True` 时，`host_codegen` 才被调用，`CompiledArtifact` 的 `rt_mod` 才非 `None`；否则 `rt_mod=None`，`TVMFFIKernelAdapter` 会因为拿不到 `rt_mod` 而无法构造。
4. **预期结果**：理解「为什么默认的 `tvm_ffi` 需要 `host_codegen`，而 `cython` 不需要」。
5. **待本地验证**：运行时切换 `execution_backend` 的实际效果需在有 native 库的环境验证。

#### 4.2.5 小练习与答案

**练习 1**：`MakePackedAPI` 为什么要把 `global_symbol` 改成带 `__tvm_ffi_` 前缀？
**答案**：运行时按这个前缀 + 名字去 `rt_mod` 里查 PackedFunc。前缀统一了「任意 backend 的 host 函数」的命名空间，避免与 device kernel 的 `global_symbol` 冲突，也让 adapter 能用统一规则找到入口。

**练习 2**：`HostCodegenHook` 和 `HostCodegen` 有什么区别？
**答案**：`HostCodegen` 是「最终产出 host runtime module」的 builder，按 **host target kind**（llvm/c）注册，每个 host target 一个；`HostCodegenHook` 是设备后端在 build 之前插入的「预处理」，按 **device target kind**（cuda/metal…）注册，可有多个、依次叠加（典型用途：Metal 往 host 注入 MPS 同步）。

### 4.3 库生成：wrapper 封装 + libgen 编译

#### 4.3.1 概念说明

对 `cython`/`nvrtc`/`torch`/`cutedsl` 这些后端，tilelang **不走 TVM 的 `rt_mod`**——它们的 `enable_host_codegen` 与 `enable_device_compile` 都是 `False`（见 [tilelang/cuda/execution_backend.py:L46-L56](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cuda/execution_backend.py#L46-L56)）。于是 `lower()` 只用 `device_codegen_without_compile` 产出**设备源码字符串**（带一个占位 PTX，见 [rt_mod_cuda.cc:L140-L173](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/cuda/codegen/rt_mod_cuda.cc#L140-L173)），剩下的「把源码变成可执行物」交给两个 Python 组件：

- **`wrapper`（`TLWrapper`/`TLPyWrapper`）**：把裸 device 源码包上一层 **host 启动函数**——读取 `device_mod`/`host_mod` 提取出 grid/block/dynamic_smem/TMA descriptor 等信息，生成含 `init()`、`call()` 的 C/C++/Python 启动代码，拼成一整份「自包含」的源文件。
- **`libgen`（`LibraryGenerator`）**：把这整份源文件喂给 `nvcc`/`hipcc`/`g++`，subprocess 编出一个 `.so`，再由 adapter 用 ctypes / cuda-python 加载。

这与 4.4 的 callback 路径是**两条并行的「源码→二进制」通道**：libgen 直接编 `.so`（产物是带 host `call()` 的共享库）；callback 只编 device kernel 的 cubin（产物交给 TVM `CUDAModule`，host 侧由 `rt_mod` 负责）。

#### 4.3.2 核心流程

以 CUDA + cython 后端为例，adapter（[tilelang/jit/adapter/cython/adapter.py:L120-L150](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/adapter/cython/adapter.py#L120-L150)）把两件套串起来：

```
wrapper = TLWrapper(target)
libgen  = LibraryGenerator(target)

wrapper.assign_optimized_module(ir_module)
wrapper.assign_host_module(host_mod)          # 4.2 那条 pipeline 产出的 host_mod
wrapper.assign_device_module(device_mod)      # 4.1 拆出来的 device_mod
host_src = wrapper.wrap(device_source)        # 拼出 "device 源码 + init() + call()"

libgen.update_lib_code(host_src)
libgen.compile_lib()                           # nvcc → .so
lib = libgen.load_lib()                        # ctypes.CDLL
lib.init()                                     # 设置 dynamic smem 属性等
```

**wrapper 做了什么**：`TLWrapper.wrap` 按 target 选具体子类（`TLCUDASourceWrapper`/`TLHIPSourceWrapper`/`TLCPUSourceWrapper`/`TLMetalSourceWrapper`，见 [wrapper.py:L971-L991](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/adapter/wrapper.py#L971-L991)）。CUDA 子类在构造时调用 `parse_source_information` 从 `device_mod`/`host_mod` 里挖出每个 kernel 的 `block_info`/`grid_info`/`dynamic_smem_buf`/`cluster_dims`/`use_cooperative_groups`，以及 `host_mod` 里的 `tma_descriptor_args`/`l2_persistent_map`（[wrapper.py:L454-L530](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/adapter/wrapper.py#L454-L530)）。然后 `update_lib_code` 拼接三段（[wrapper.py:L577-L624](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/adapter/wrapper.py#L577-L624)）：

```
lib_code = device_source                       # 4.4/6.3 生成的 __global__ kernel
         + get_cuda_host_adapter_include()     # TMA 需要 cutlass/cuda_host_adapter.hpp
         + get_init_func()                     # extern "C" int init() { 设置 dynamic smem }
         + create_dispatch_func(...)           # extern "C" int call(...) { cudaLaunchKernelEx(...) }
```

- `init()`：对需要超过静态上限的 dynamic shared memory 的 kernel 调 `cudaFuncSetAttribute(...MaxDynamicSharedMemorySize...)`（模板 [wrapper.py:L26-L32](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/adapter/wrapper.py#L26-L32)、生成 [L565-L575](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/adapter/wrapper.py#L565-L575)）。
- `call()`：对每个 kernel 发射 `cudaLaunchKernelEx`，普通 launch 用 [KERNEL_LAUNCH_FUNC_CODE](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/adapter/wrapper.py#L161-L175)，SM90+ cluster launch 用 [KERNEL_CLUSTER_LAUNCH_FUNC_CODE](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/adapter/wrapper.py#L178-L199)（带 `cudaLaunchAttributeClusterDimension`），TMA 拷贝还在前面插入 `cuTensorMapEncodeTiled` 描述符初始化（[TMA_DESC_INIT_FUNC](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/adapter/wrapper.py#L111-L132)）。HIP 子类则改用 `<<<grid,block,smem,stream>>>` 语法、hipStream，并查询设备最大 smem（[wrapper.py:L708-L730](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/adapter/wrapper.py#L708-L730)）。

**libgen 做了什么**：[LibraryGenerator.compile_lib](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/adapter/libgen.py#L59-L208) 按 target 拼命令行：

- **CUDA**（[L62-L119](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/adapter/libgen.py#L62-L119)）：`nvcc --shared -std=c++20 -lcuda`，用 `-gencode arch=compute_<sm>,code=<gencode>` 指定架构（CUDA 13.1 起 `-arch=sm_90a --shared` 会经 sm_90 PTX 中转、拒绝 Hopper-only 指令，故强制用 `-gencode`），`-I CUTLASS_INCLUDE_DIR`、`-I TILELANG_TEMPLATE_PATH`；`TL_ENABLE_FAST_MATH` 加 `--use_fast_math`；`TL_PTXAS_REGISTER_USAGE_LEVEL` 加 `--ptxas-options=--register-usage-level=N`；verbose 加 `--ptxas-options=--verbose`。
- **HIP**（[L121-L143](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/adapter/libgen.py#L121-L143)）：`hipcc --shared --offload-arch=<mcpu> -std=c++17 -fPIC`，`-I COMPOSABLE_KERNEL_INCLUDE_DIR`。
- **CPU**（[L144-L155](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/adapter/libgen.py#L144-L155)）：`g++/clang++ -std=c++17 -fPIC -shared`。

随后 `subprocess.run(command)`，失败则把命令、stdout、源码一起抛 `RuntimeError`（[L190-L199](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/adapter/libgen.py#L190-L199)）。`load_lib` 用 `ctypes.CDLL` 加载 `.so`（[L52-L57](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/adapter/libgen.py#L52-L57)）。注意 libgen 路径**不经过** `CUDABinaryCache`——它的产物就是那个 `.so` 本身（其缓存由上层 `KernelCache` 负责，见 u4-l3）。

`compile_flags`（用户层）与 `TL_DEVICE_COMPILE_FLAGS`（pass config）都会被拼进命令（[L161-L162](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/adapter/libgen.py#L161-L162)），后者由 JIT 层搭进 `PassContext`（见 u6-l1）。

#### 4.3.3 源码精读

本讲最小模块 `tilelang.jit.adapter.libgen` 只有一个类 `LibraryGenerator`，但它是「让 tilelang 脱离 TVM runtime 也能跑」的关键——只要能拼出正确的 nvcc/hipcc/g++ 命令并 subprocess 执行，任何机器都能产出 `.so`。把它和 `wrapper.py` 的 `TLCUDASourceWrapper` 对照阅读，能看清「IR 里那些 `thread_extent`/`cluster_dims`/`tma_descriptor_args` 属性最终如何变成具体的 `cudaLaunchKernelEx` 调用参数」。

#### 4.3.4 代码实践

1. **实践目标**：用 verbose 模式看到 libgen 真正下发的 nvcc 命令，并理解命令里每个选项的来源。
2. **操作步骤**：
   - 用 cython 后端编译同一个 GEMM：`kernel = jit_kernel.compile(..., execution_backend="cython", verbose=True)`（或对应 API）。
   - 在 [libgen.py:L192](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/adapter/libgen.py#L192) 的 `print(f"compile_lib compilation command: ...")` 处观察输出。
3. **需要观察的现象**：命令行包含 `--shared`、`-std=c++20`、`-gencode arch=compute_<sm>,code=...`、`-lcuda`、`-I<CUTLASS>`、`-I<template>`；若开了 fast math 则多 `--use_fast_math`；verbose 则多 `--ptxas-options=--verbose`。
4. **预期结果**：能逐项解释命令里每个 flag 来自哪个 pass config 或环境。
5. **待本地验证**：实际 nvcc 输出与是否有 GPU 强相关；无 GPU 时可只读源码，对照 4.3.2 自行推演命令拼接。

#### 4.3.5 小练习与答案

**练习 1**：为什么 libgen 的 CUDA 分支强制用 `-gencode` 而不是 `-arch=sm_90a`？
**答案**：注释（[libgen.py:L79-L83](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/adapter/libgen.py#L79-L83)）说明：CUDA 13.1 会把 `nvcc --shared -arch=sm_90a` 经 sm_90 PTX 中转，从而拒绝 `setmaxnreg` 这类 Hopper-only 指令。显式 `-gencode` 让 shared-library 编译保留所请求的 accelerated target。

**练习 2**：`init()` 函数里那段 `cudaFuncSetAttribute(...MaxDynamicSharedMemorySize...)` 是干什么的？
**答案**：当一个 kernel 需要的 dynamic shared memory 超过硬件默认上限（通常是 48KB）时，必须在 launch 前调这个 API 申请提高上限。`init()` 在 `.so` 加载后由 ctypes 调用一次（[cython/adapter.py:L136-L140](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/adapter/cython/adapter.py#L136-L140)），失败会经 `get_last_error()` 回传错误信息。

### 4.4 编译回调：tilelang_callback_cuda_compile 与 binary cache

#### 4.4.1 概念说明

对默认的 `tvm_ffi` 后端，device 侧的二进制不是 libgen 编的，而是由 TVM 的 device codegen（C++ 侧 `BuildTileLangCUDA`）发起、**回调**到 Python 的 `tilelang_callback_cuda_compile` 完成的。这个回调做三件事：算架构选项、查/写 `CUDABinaryCache`、调 `nvcc.compile_cuda`。HIP 对应 `tilelang_callback_hip_compile`（走 `hipcc`）。

为什么用回调而不是 C++ 直接调 nvcc？因为 Python 侧手里有 `pass_config`（fast-math、ptxas 选项、device compile flags）、`CUDABinaryCache`（磁盘缓存）、`env`（verbose）等只在 Python 有现成实现的基础设施。C++ 把源码和 target 交给 Python，Python 编完把 cubin/fatbin 字节还回去，C++ 再包成 `CUDAModule`。

#### 4.4.2 核心流程

C++ 侧入口 [BuildTileLangCUDA](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/cuda/codegen/rt_mod_cuda.cc#L97-L138)（被 [tilelang/cuda/codegen.py:L16-L25](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cuda/codegen.py#L16-L25) 注册为 `target.build.tilelang_cuda`）：

```
CodeGenTileLangCUDA cg; cg.Init();          # 代码生成器
ValidateUniqueDeviceGlobalSymbols(mod);      # 校验 __global__ 名字唯一
tilelang_callback_cuda_validate(mod);        # Python 校验（source kernel 用）
for (gvar, func) in mod:
    ICHECK(func.calling_conv == kDeviceKernelLaunch)   # 4.1 那枚章
    cg.AddFunction(gvar, func)
code = cg.Finish()                           # 拼出 CUDA C++ 源码
tilelang_callback_cuda_postproc(code, target)  # 可选后处理
ptx = tilelang_callback_cuda_compile(code, target, pass_ctx->config)  # ★ 回调编译
fmt = (ptx[0] != '/') ? "cubin" : "ptx"      # 返回路径=外部文件(ptx)，字节=cubin
return CUDAModuleCreateWithFallback(ptx, fmt, func_info, source_map)
```

关键在 [rt_mod_cuda.cc:L123-L129](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/cuda/codegen/rt_mod_cuda.cc#L123-L129)：取出当前 `PassContext` 的 config，连同 code、target 一起传给回调。回调在 Python 侧用 `@tvm_ffi.register_global_func("tilelang_callback_cuda_compile")` 注册（[lower.py:L101-L176](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/engine/lower.py#L101-L176)）：

1. **算架构**：`get_target_arch_and_code(target)` 得 `target_arch`（如 `90a`）与 `target_code_list`；多 code 用 fatbin，单 code 用 cubin（[L103-L110](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/engine/lower.py#L103-L110)）。
2. **拼 options**：`-std=c++20 -I<Template> -I<CUTLASS>`；从 `pass_config` 读 `TL_ENABLE_FAST_MATH`（→`--use_fast_math`）、`TL_PTXAS_REGISTER_USAGE_LEVEL`（→`--ptxas-options=--register-usage-level=N`）、`TL_DEVICE_COMPILE_FLAGS`（shlex 拆分后追加）；verbose 加 `--ptxas-options=--verbose -w`（[L120-L150](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/engine/lower.py#L120-L150)）。
3. **查缓存**：`CUDABinaryCache.make_key(...)` 算键，`CUDABinaryCache.load(key, fmt)` 命中就直接返回字节，连 nvcc 都不调（[L154-L164](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/engine/lower.py#L154-L164)）。
4. **编译 + 落盘**：`nvcc.compile_cuda(code, fmt, arch, options)` 得 cubin/ptx 字节，`CUDABinaryCache.save(key, fmt, ptx)` 写盘，返回（[L166-L175](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/engine/lower.py#L166-L175)）。

**为什么编译选项必须进缓存键**：`CUDABinaryCache.make_key` 把 `options` 显式纳入键（[cuda_binary_cache.py:L104-L118](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cache/cuda_binary_cache.py#L104-L118)），代码注释写得很直白：`--use_fast_math` 这类 flag 会改变生成的 SASS 而**不**改变 CUDA 源码，如果只按源码哈希做键，一次 precise-math 编译会被之前 fast-math 的 cubin 命中（假命中），反之亦然。键里还含 `tilelang_version`、`target_kind`、`target_arch`、`target_code`、`compile_format`，以及（可选）原生库内容戳 `tilelang_lib`。

**磁盘布局与原子写**：缓存根目录按版本命名空间隔离（`<cache_dir>/<version>/cuda-binaries/<sha256>.<fmt>`，[L37-L44](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cache/cuda_binary_cache.py#L37-L44)）；`save` 先写到 `TMP_DIR` 下的临时文件再用 `os.replace` 原子替换（[L137-L148](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cache/cuda_binary_cache.py#L137-L148)），避免并发 autotune 时读到半成品；`load` 在 `env.is_cache_enabled()` 为假时直接返回 `None`（[L126-L134](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cache/cuda_binary_cache.py#L126-L134)），缓存总开关见 u4-l3。

HIP 的对应回调 [tilelang_callback_hip_compile](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/engine/lower.py#L178-L196) 更简单：取 `mcpu`、`hipcc.compile_hip` 出 hsaco 返回（目前没有独立的 hsaco binary cache，选项也固定）。

#### 4.4.3 源码精读

`tilelang_callback_cuda_compile` 把「编译选项从哪来」「缓存键怎么算」「nvcc 怎么调」三件事压在一个 Python 函数里，是理解 tilelang 设备编译可观测性与可配置性的最佳入口。把它和 4.3 的 `libgen` 对比，能看清两条通道的对称与不对称：

| 维度 | libgen（cython/nvrtc/torch/cutedsl） | callback（tvm_ffi） |
| --- | --- | --- |
| 触发者 | adapter（Python）直接 subprocess | device codegen（C++）回调 Python |
| 产物 | 含 host `call()` 的 `.so` | device kernel 的 cubin/fatbin 字节 |
| 编译范围 | device kernel + host 启动函数整份源 | 仅 device kernel 源 |
| 缓存 | 无独立二进制缓存（`.so` 由 `KernelCache` 管） | `CUDABinaryCache` 磁盘缓存 |
| options 来源 | `pass_configs` + `compile_flags` | `pass_ctx->config`（同一份 PassContext） |

#### 4.4.4 代码实践

1. **实践目标**：跟踪一次 cubin 编译，确认「源码→options→缓存键→nvcc→落盘→命中」全链路。
2. **操作步骤**：
   - 默认 `tvm_ffi` 后端编译一个 GEMM。设环境变量开启 verbose（让回调加 `--ptxas-options=--verbose`），并可在 [lower.py:L162](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/engine/lower.py#L162) 前后加日志（仅本地调试，勿提交）观察是否命中缓存。
   - 第一次编译后，到 `env.TILELANG_CACHE_DIR` 下的 `<version>/cuda-binaries/` 找到 `<sha256>.cubin`。
   - 删除该 cubin，第二次编译应能看到 nvcc 被重新调用；保留它则第二次直接命中（无 nvcc）。
   - 对比实验：用 `pass_configs={PassConfigKey.TL_ENABLE_FAST_MATH: True}` 编译一次，再不带 fast-math 编译，应得到**两个不同的** `<sha256>.cubin`（因为 options 进了键）。
3. **需要观察的现象**：命中缓存时无 nvcc 进程、返回极快；options 不同则键不同、文件不同。
4. **预期结果**：能复述「code + target + options 决定键，键决定命中与否」。
5. **待本地验证**：上述现象需有 GPU + native 库的环境验证；无 GPU 时改为阅读 [lower.py:L101-L176](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/engine/lower.py#L101-L176) 与 [cuda_binary_cache.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cache/cuda_binary_cache.py) 推演。

#### 4.4.5 小练习与答案

**练习 1**：回调返回的字节，C++ 怎么判断它是 cubin 还是 ptx？
**答案**：看首字符。若 `ptx[0] == '/'`，C++ 认为返回的是一个外部文件路径（按 `fmt="ptx"` 处理）；否则当作 cubin 字节流（`fmt="cubin"`）。见 [rt_mod_cuda.cc:L128-L130](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/cuda/codegen/rt_mod_cuda.cc#L128-L130)。

**练习 2**：`tilelang_callback_hip_compile` 为什么没有像 CUDA 那样的独立 binary cache？
**答案**：从 [lower.py:L178-L196](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/engine/lower.py#L178-L196) 可见，HIP 回调直接 `hipcc.compile_hip` 返回 hsaco，未接入任何 cache 类。这是当前实现的取舍（HIP 复用由上层 `KernelCache` 在 `.so`/artifact 层面兜底），而非架构限制。

**练习 3**：`TL_PTXAS_REGISTER_USAGE_LEVEL` 与 `TL_ENABLE_FAST_MATH` 这两个选项，谁会把它们真正塞进 nvcc 命令？
**答案**：在 callback 路径里由 [lower.py:L144-L149](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/engine/lower.py#L144-L149) 读取并 append；在 libgen 路径里由 [libgen.py:L69-L116](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/adapter/libgen.py#L69-L116) 读取。两条路径都从 `pass_configs`（即 `PassContext.config`）取值，故用户层用同一份 `pass_configs` 即可同时影响两者。

## 5. 综合实践

把本讲四节串起来，做一个「同 kernel、两路径」的对照实验。

**任务**：写一个最小 GEMM kernel（参考 `examples/quickstart.py`），分别用 `execution_backend="tvm_ffi"`（默认）和 `execution_backend="cython"` 编译，对照回答以下问题。

1. **拆分边界**：开启 `TL_ENABLE_DUMP_IR`，在 dump 里找到 `tl.SplitHostDevice`、`tl.MakePackedAPI`、`tl.LowerDeviceKernelLaunch` 三份 IR。指出：
   - 哪个 PrimFunc 是 host（`calling_conv=1`），哪个是 device（`calling_conv=2`）？
   - host 函数体里那行「调用 device」的语句，在 `LowerDeviceKernelLaunch` 前后分别长什么样？
2. **落地差异**：
   - `tvm_ffi` 路径下，`CompiledArtifact.rt_mod` 是否非空？为什么？（提示：`enable_host_codegen`）
   - `cython` 路径下，wrapper 拼出的 `lib_code` 包含哪三段？libgen 下发的 nvcc 命令里有没有 `-gencode` 与 `--use_fast_math`（若你开了 fast math）？
3. **cubin 来源**：
   - `tvm_ffi` 路径的 cubin 由谁编的？（`tilelang_callback_cuda_compile`）
   - `cython` 路径的「二进制」是什么？（整个 `.so`，由 libgen 编）
4. **缓存**：在 `TILELANG_CACHE_DIR` 下分别找两类产物：`<version>/cuda-binaries/*.cubin`（callback 路径）与 `KernelCache` 管理的 `.so`/artifact（libgen 路径）。说明为什么 fast-math 开关会改变前者却不一定改变后者的文件名。

**交付物**：一份简短笔记，包含 (a) 标注好 host/device 与 calling_conv 的 IR 截图或片段；(b) 两条路径各自的「源码→二进制」数据流草图；(c) 用一句话总结「`SplitHostDevice` 切开、`LowerDeviceKernelLaunch` 盖章、`host_codegen`/`wrapper+libgen` 各自落地、callback 编 cubin」的端到端链路。

> 无 GPU 时：(1)(2) 的 IR 与源码部分可仅靠 dump（不需要 GPU）完成；(3)(4) 的运行时现象标注「待本地验证」，主要靠阅读 4.3/4.4 的源码完成推理。

## 6. 本讲小结

- `SplitHostDevice` 做**物理拆分**：把含 `target` device region 的 PrimFunc 切成「host 留一句 GlobalVar 调用 + 新建 device 函数」，并搬运 `cluster_dims`/`smem_alignment_map`/`non_restrict_params` 等属性。它**不**设置 calling convention。
- `MakePackedAPI` 把 host 函数标准化为 C-PackedFunc（`kCPackedFunc`，`__tvm_ffi_<name>`）；device 函数因无 host target 被跳过。
- `LowerDeviceKernelLaunch` 才给 device 函数盖上 `kDeviceKernelLaunch`、提取 launch 参数、改写调用点。`lower()` 据此用 `Filter` 把模块分拣成 `host_mod`/`device_mod`，device codegen 再 `ICHECK` 这枚章。
- `host_codegen`（注册表 `tilelang.backend.host_codegen`，与 device codegen/execution backend 同构）只对 `tvm_ffi` 后端运行，产出 TVM `rt_mod`；`HostCodegenHook` 让设备后端在 build 前插手改 host IR。
- 对 `cython`/`nvrtc`/`torch`/`cutedsl` 后端，落地走 **wrapper + libgen**：wrapper 把 device 源码包上 `init()`/`call()`（`cudaLaunchKernelEx`/cluster launch/TMA 描述符），libgen 用 nvcc/hipcc/g++ 编出 `.so`，ctypes 加载。
- 对 `tvm_ffi` 后端，device cubin 由 C++ device codegen 回调 Python 的 `tilelang_callback_cuda_compile` 完成：算架构、查/写 `CUDABinaryCache`、调 nvcc。编译选项必须进缓存键，以防 fast-math 假命中。

## 7. 下一步学习建议

- **运行时执行细节**：本讲止步于「`.so`/`rt_mod` 产出」，真正调用时如何把 `torch.Tensor` 喂进去、stream/device 如何对齐，回到 u7-l1 的 adapter（尤其 `TVMFFIKernelAdapter._convert_torch_func` 与 cython wrapper 的 `forward`）。
- **Pass 内部**：若想深究 `LowerDeviceKernelLaunch` 如何把 GlobalVar 调用改写成 launch，阅读 [src/transform/lower_device_kernel_launch.cc](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/lower_device_kernel_launch.cc) 全文，并结合 u6 的 Pass 体系。
- **缓存全貌**：本讲的 `CUDABinaryCache` 是「设备二进制层」缓存，与 u4-l3 的 `KernelCache`（artifact 层）、`_kernel_cache`（会话层）叠加，建议重读 u4-l3 把四层缓存拼成一张图。
- **多后端移植**：若想给新后端（如新 GPU）接 host/device 拆分与落地，重点参考 `tilelang/<backend>/pipeline.py` 里 `SplitHostDevice`/`MakePackedAPI`/`LowerDeviceKernelLaunch` 的固定三连，以及 `tilelang/<backend>/codegen.py` 的 device/host codegen 注册——这正是 u10-l2「扩展新后端」的主题。
