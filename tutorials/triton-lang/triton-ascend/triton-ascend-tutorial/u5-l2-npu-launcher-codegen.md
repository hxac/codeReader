# u5-l2 NPULauncher 与 C++ launcher 代码生成

## 1. 本讲目标

本讲承接 u5-l1（`NPUDriver`/`NPUUtils` 完成设备发现、架构探测，并通过 `load_binary` 把内核二进制注册成 CANN 的 `function` 句柄）。那个 `function` 句柄现在只是「设备侧的一段可执行代码」，但 Python 世界里的一次 `kernel[grid](...)` 调用还携带了大量运行时参数（grid 维度、stream、指针张量、标量、元数据字典……）。**谁来把这两端接起来？** 答案就是本讲的主角——**launcher**。

学完本讲，你应当能够：

1. 说清楚 `NPULauncher` 这个对象从「构造」到「被调用」经历了哪些步骤，以及它和 core Triton `CompiledKernel` 的衔接点在哪里。
2. 读懂 `make_launcher` 这个「代码生成器」：它如何根据 kernel 的签名（signature）和元数据（metadata），**字符串拼装**出一段完整的 C++ 源码。
3. 理解 `make_npu_launcher_stub` 如何把这段 C++ 源码编译成 `.so`、按内容哈希缓存，以及为什么要处理 **CXX11 ABI**。
4. 看懂生成的 C++ 里两个最关键的片段：`PyArg_ParseTuple`（从 Python 元组里拆参数）与 `getPointer`（从张量对象里提取设备指针）。
5. 亲手 dump 出某个 kernel 的 launcher `.cxx` 源码，定位上述片段。

> 本讲的统一视角：**launcher 是一座「按签名定制」的桥**。每个不同的 kernel 签名（参数个数、类型不同）都会生成一座不同的桥，编译成一个独立的 `.so`，再被 Python 动态加载。

---

## 2. 前置知识

在进入源码前，先建立几个本讲反复出现的概念。

### 2.1 为什么需要「代码生成」而不是写一个通用 launcher？

NPU 内核启动时，主机侧（host）需要把所有参数按特定**内存布局**拼成一段连续的字节缓冲区（`launch_args`），再连同 `function` 句柄一起交给 CANN 的 `rtKernelLaunch`。不同 kernel 的参数「个数、类型、顺序」千差万别。一个通用 launcher 必须在运行时反复判断「第 i 个参数是什么类型、多大、对齐到几字节」，既慢又容易出错。

Triton 的做法是：**为每一种签名专门生成一段 C++ 代码**。这段代码在编译期就把「第 0 个参数是 `int32_t`、第 1 个是 `void*`……」写死，运行时直接走最直接的拼装路径。代价是「签名变了就要重新生成、重新编译」，而这恰好可以用缓存兜住（同一签名只编译一次）。

### 2.2 PyArg_ParseTuple：Python ↔ C 的参数拆包

CPython 扩展模块（`.so`）里暴露给 Python 的函数，接收的是一个 `PyObject* args`（一个元组）。`PyArg_ParseTuple(args, "格式串", &c变量1, &c变量2, ...)` 是 CPython 的标准 API，它按「格式串」把元组里的 Python 对象逐个转换成 C 变量。例如 `"i"` 表示解析成 `int`，`"f"` 表示 `float`，`"O"` 表示「原样给我一个 `PyObject*`，不做转换」。本讲会反复看到这个格式串是如何**按签名自动拼出来的**。

### 2.3 设备指针与 data_ptr()

Ascend NPU 上的张量（如 `torch_npu` 的 tensor）在主机侧用一个**设备地址**（一个 64 位整数）表示它在设备显存里的位置。`tensor.data_ptr()` 返回这个地址。launcher 的核心职责之一就是：从 Python 张量对象里取出这个地址，塞进 `launch_args`。

### 2.4 CXX11 ABI 是什么，为什么要关心

`_GLIBCXX_USE_CXX11_ABI` 是 libstdc++ 的一个二进制接口开关（取值 0 或 1）。**如果 launcher `.so` 和它要链接的宿主库（torch_npu / mindspore）用了不同的 ABI，加载或调用时就会崩溃。** 因此 launcher 的缓存名里直接带上了 ABI 取值，保证「按 ABI 隔离缓存」。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [third_party/ascend/backend/driver.py](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py) | 本讲主战场：`NPULauncher` 类、`make_launcher`（C++ 源码生成）、`make_npu_launcher_stub`（编译+缓存）。 |
| [third_party/ascend/backend/utils.py](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/utils.py) | `_build_npu_ext`（编译 `.so` 的实际命令拼装）、`_check_cxx11_abi`（ABI 探测）。 |
| [third_party/ascend/backend/backend_register.py](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/backend_register.py) | `get_backend_func` 的策略注册表：torch_npu / mindspore 两套实现的分派。 |
| [python/triton/compiler/compiler.py](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/python/triton/compiler/compiler.py) | core Triton 的 `CompiledKernel`：实例化 launcher、拼装调用参数的**顺序**。 |
| [third_party/ascend/backend/compiler.py](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py) | `pack_metadata`：把运行时需要的元数据精简打包成 `packed_metadata` 字典。 |

---

## 4. 核心概念与源码讲解

### 4.1 NPULauncher：launcher 的生命周期与调用链

#### 4.1.1 概念说明

`NPULauncher` 是一个「**一次构造、反复调用**」的对象：

- **构造时**：根据 kernel 签名 + 元数据，**生成并编译**出该签名专属的 launcher `.so`，加载它，取出里面的 `launch` C 函数。
- **调用时**（每次 `kernel[grid](...)`）：把 Python 侧传来的实参原样转发给这个 C 函数。

它由 core Triton 的 `CompiledKernel` 在「懒初始化」阶段创建，类型登记在 `NPUDriver.launcher_cls` 上。

#### 4.1.2 核心流程

```text
core CompiledKernel._init_handles()
        │  driver.active.launcher_cls(src, metadata)
        ▼
NPULauncher.__init__(src, metadata)
        │
        ├─ _make_launcher_stub_path()
        │      ├─ generate_npu_header_src()        → header_src   (C++ 头文件模板)
        │      ├─ constants/signature 的 arg_name → index 归一化
        │      ├─ make_launcher(constants, signature, metadata)  → wrapper_src (整段 C++ 源码)
        │      └─ make_npu_launcher_stub(header_src, wrapper_src, debug)  → .so 路径
        │
        └─ importlib 动态加载该 .so，取出 launch 函数  →  self.launch

每次 kernel 调用:
NPULauncher.__call__(*args)
        ├─ compile_only ?  打印缓存路径后直接 return
        ├─ msprof 注册张量 ?  给 packed_metadata 注入 tensor_params_shape
        └─ self.launch(*args)   →  进入 C++ 的 launch(PyObject*, PyObject*)
```

注意：`NPULauncher` 自身**不碰硬件**，它只是「生成桥 + 调用桥」。真正启动内核的是它加载的那段 C++（见 4.3/4.4）。

#### 4.1.3 源码精读

`NPULauncher.__init__` 把生成 `.so` 的活儿交给 `_make_launcher_stub_path`，随后用 `importlib` 把 `.so` 当成一个名叫 `__triton_launcher` 的 Python 模块加载，并取出 `launch` 符号：

[third_party/ascend/backend/driver.py:152-167](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py#L152-L167) —— 构造时即生成 stub、加载 `.so`、取出 `launch`。注意它还顺带把 `mix_mode`、`shared` 缓存为成员，供后续运行时使用。

`_make_launcher_stub_path` 是「生成 + 编译」的入口，它先把签名里的参数名统一换成下标（因为 `make_launcher` 用下标 `_arg0/_arg1` 来命名 C 变量），再调用两个本讲的主角：

[third_party/ascend/backend/driver.py:169-176](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py#L169-L176) —— `cst_key` 把字符串参数名映射成下标；`make_launcher` 出 wrapper 源码，`make_npu_launcher_stub` 出 `.so` 路径。

`__call__` 是每次 kernel 调用的热路径。它处理两个分支后，把全部参数透传给 C 函数 `self.launch`：

[third_party/ascend/backend/driver.py:181-196](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py#L181-L196) —— `compile_only` 分支用于「只编译不运行」（如 costmodel/ubtuner 场景，见 u9）；`enable_msprof_register_tensor` 分支会把张量形状塞进 `packed_metadata`（即 `args[5]`）供性能打点上报；最后 `self.launch(*args)` 进入 C 侧。

那 `*args` 的顺序到底是什么？这由 core Triton 的 `runner` 闭包决定，理解它才能理解 4.4 里的格式串：

[python/triton/compiler/compiler.py:518-524](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/python/triton/compiler/compiler.py#L518-L524) —— 固定 9 个头部参数后跟可变长 kernel 实参：`(gridX, gridY, gridZ, stream, function, packed_metadata, launch_metadata, launch_enter_hook, launch_exit_hook, *kernel_args)`。这个顺序与 4.4 的 `"iiiKKOOOO"` 一一对应。

而 launcher 的实例化点就在 `_init_handles` 里：

[python/triton/compiler/compiler.py:477](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/python/triton/compiler/compiler.py#L477) —— `self._run = driver.active.launcher_cls(self.src, self.metadata)`，`launcher_cls` 正是 `NPUDriver` 里登记的 `NPULauncher`。

#### 4.1.4 代码实践

**实践目标**：确认 `NPULauncher` 的存在与它的「懒加载」时机。

1. 操作步骤：在你本地的 NPU 环境里，运行 vector-add（见 u1-l4），并在 `kernel[grid](...)` 之前加一句 `print(type(compiled_kernel._run))`（需先用 `triton.compile` 或借助 `@triton.jit` 的内部句柄拿到 `CompiledKernel`；最简单的方式是在 driver.py 的 `NPULauncher.__init__` 首行临时加 `print("NPULauncher init", metadata.name)`）。
2. 观察现象：`NPULauncher init` 只在**首次**调用该签名时打印一次；之后同签名的调用不再打印（因为 `CompiledKernel._init_handles` 有 `if self.module is not None: return` 的早退保护）。
3. 预期结果：launcher 是**按签名懒构造、构造一次后复用**的对象。
4. 若无法在 NPU 上运行，标注「待本地验证」，但「懒构造 + 复用」这一结论可直接从源码 `_init_handles` 的早退逻辑得出。

#### 4.1.5 小练习与答案

- **练习 1**：`NPULauncher.__call__` 里 `args[5]` 为什么必须是 `packed_metadata`？请结合 core `runner` 的参数顺序回答。
  - **答案**：core 的 `runner` 第 6 个参数（下标 5）正是 `self.packed_metadata`，C 侧 `PyArg_ParseTuple` 也把它解析到 `packedMetadata` 变量，随后从中读取 `kernel_name`、`tensor_kinds`。
- **练习 2**：如果两个 kernel 的参数类型完全相同但个数不同，它们会共用同一个 launcher `.so` 吗？
  - **答案**：不会。签名不同 → `make_launcher` 生成的源码不同 → `make_npu_launcher_stub` 的内容哈希不同 → 缓存到不同的 `.so`。

---

### 4.2 make_npu_launcher_stub：编译 .so、缓存键与 CXX11 ABI

#### 4.2.1 概念说明

`make_npu_launcher_stub(header_src, wrapper_src, debug)` 的职责单一而清晰：**给我一段 C++ 源码，我还你一个编译好、可加载的 `.so` 路径**。它解决三件事：

1. **缓存**：同一段源码不重复编译（内容寻址）。
2. **ABI 隔离**：缓存名带 CXX11 ABI，避免和宿主库 ABI 不匹配。
3. **调试可见**：`debug=True` 时把源码 dump 到磁盘，方便人读。

#### 4.2.2 核心流程

```text
make_npu_launcher_stub(header_src, wrapper_src, debug)
   │
   ├─ cache_key = sha256(header_src + "\0" + wrapper_src)   # 内容寻址
   ├─ use_cxx11_abi = _check_cxx11_abi()                     # 0 或 1
   ├─ so_name = "launcher_cxx11abi{abi}" + EXT_SUFFIX
   │
   ├─ if debug:  dump precompiled.h 与 {so_name 去后缀}.cxx 到 dump 目录
   │
   ├─ cache.get_file(so_name) 命中?  → 直接返回缓存路径
   │
   └─ 未命中:
        ├─ 把 wrapper_src 写到临时目录的 {name}.cxx
        ├─ _build_npu_ext(name, src_path, kernel_launcher="torch")  → 编译出 .so
        ├─ (debug 时再把 .so 二进制也 dump 一份)
        └─ cache.put(.so 字节, so_name)  → 返回缓存路径
```

缓存键的设计要点：用 `sha256(header + "\0" + wrapper)`。`"\0"` 是分隔符，防止「header 尾 + wrapper 头」拼凑出哈希碰撞。只要签名、元数据或环境相关的代码片段有任何变化，`wrapper_src` 就会变，缓存自然失效——这是「按签名定制」能成立的基石。

#### 4.2.3 源码精读

整段函数：

[third_party/ascend/backend/driver.py:276-310](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py#L276-L310) —— 缓存键计算、ABI 探测、命中检查、未命中则编译并回填缓存。

缓存键与 ABI 命名：

[third_party/ascend/backend/driver.py:280-285](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py#L280-L285) —— `so_cache_key` 来自两段源码的 sha256；`name` 直接把 ABI 取值写进文件名（`launcher_cxx11abi0` / `launcher_cxx11abi1`），`EXT_SUFFIX` 是平台相关的 `.so` 后缀（如 `.cpython-310-x86_64-linux-gnu.so`）。

调试 dump（本讲代码实践的核心入口）：

[third_party/ascend/backend/driver.py:287-292](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py#L287-L292) —— `debug=True` 时，`header_src` 存成 `precompiled.h`，`wrapper_src` 存成 `{name}.cxx`，并打印 dump 目录路径。这就是「想看生成的 launcher 源码」要打开的开关。

实际编译委托给 `_build_npu_ext`：

[third_party/ascend/backend/utils.py:389-445](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/utils.py#L389-L445) —— 拼出 `cxx src_path -w -I<python头> -I<backend目录> -I<ASCEND_HOME_PATH/include...> -I<pybind11> -L<ascend/lib64> -lruntime -lascendcl [+ torch_npu/mindspore 专用 flags] -std=c++17 -shared -fPIC -o xxx.so`，然后 `subprocess.run` 执行；失败则抛出包含完整命令的 `RuntimeError`。

ABI 探测走策略注册表：

[third_party/ascend/backend/utils.py:467-468](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/utils.py#L467-L468) —— `_check_cxx11_abi()` 实际是 `get_backend_func("cxx_abi")`。

[third_party/ascend/backend/backend_register.py:91-99](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/backend_register.py#L91-L99) —— torch_npu 取 `torch._C._GLIBCXX_USE_CXX11_ABI`（通常为 1），mindspore 固定返回 0。这正是缓存名要带 ABI 的原因：两种宿主库 ABI 不同，必须各自缓存。

顺带一提，`get_backend_func` 本身是一个「按 `TRITON_BACKEND`（或自动探测 torch_npu/mindspore）分派」的策略表，launcher 里很多 C++ 片段（`header_file`、`get_cc_cmd`、`allocate_memory`、`async_launch`…）都通过它来切换 torch_npu 与 mindspore 两套实现：

[third_party/ascend/backend/backend_register.py:42-47](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/backend_register.py#L42-L47) —— `execute_func(category, method, *args)` 按 `(category, method)` 二维键查表调用。

#### 4.2.4 代码实践

**实践目标**：亲手拿到某个 kernel 的 launcher `.cxx` 源码。

1. 操作步骤：`export TRITON_DEBUG=1`（该环境变量经 core `knobs.runtime.debug` 流向 `opt.debug` → `metadata.debug`，见 `python/triton/knobs.py` 与 `jit.py`），然后运行任意 tutorial kernel（如 01-vector-add）。
2. 观察现象：stdout 会打印类似 `Dumping launcher_cxx11abi1.cxx to /home/<user>/.triton/cache/.../<dump目录>`。进入该目录，能看到 `precompiled.h` 与 `launcher_cxx11abi1.cxx` 两个文件。
3. 预期结果：用编辑器打开 `launcher_cxx11abi1.cxx`，应能看到完整的 C++ 源码（含 `#include`、`getPointer`、`launch`、`PyInit___triton_launcher`）。本讲 4.4 的实践会进一步精读它。
4. 标注：实际目录名与 ABI 后缀依赖本地环境，运行结果「待本地验证」，但「dump 会发生、文件名形如 `launcher_cxx11abi{0,1}.cxx`」可从源码直接确认。

#### 4.2.5 小练习与答案

- **练习 1**：为什么缓存键是对**源码内容**做哈希，而不是对 kernel 名字做哈希？
  - **答案**：不同 kernel 名字可能撞签名，而 launcher 只依赖签名 + 元数据 + 环境；反之同名 kernel 改了签名也必须重编。内容哈希精确刻画了「这段源码是否变化」。
- **练习 2**：同一台机器上，先装 torch_npu、后换 mindspore，旧的 launcher `.so` 还能复用吗？
  - **答案**：不能。两者 ABI 取值不同（1 vs 0），`so_name` 不同；且 `header_src`/`wrapper_src` 里经由 `get_backend_func` 注入了不同的 C++ 片段，`cache_key` 也不同。

---

### 4.3 make_launcher：按签名生成 C++ launcher 源码

#### 4.3.1 概念说明

`make_launcher(constants, signature, metadata)` 是本讲最长的函数，但它本质只做一件事：**返回一个超长的 C++ 源码字符串**。这个字符串被 `f"""..."""` 模板拼接而成，其中所有 `{...}` 占位符都被签名/元数据/环境的具体值替换。可以把这个函数看成一个「带占位符的 C++ 模板渲染器」。

它产出的 C++ 大致包含这些区块（自上而下）：

| 区块 | 作用 |
| --- | --- |
| 头文件 `npu_headers` | `rt.h`、`acl.h`、torch_npu/mindspore 专用头（`get_backend_func("header_file")`）。 |
| `cpp_msprof_extern` | msprof 性能打点的外部声明。 |
| `cpp_npu_utils_dlopen` | （torch_npu）运行时 `dlopen` `npu_utils.so`，拿到 workspace/sync_block_lock 分配与异步启动函数。 |
| `cpp_device_pointer` | `getPointer`：从 `PyObject*` 提取设备指针（见 4.4）。 |
| `triton_launch_kernel` | `extern "C"` 的 **C API 路径**：接收扁平的「指针数组 + 大小数组」，自行 memcpy 拼装。 |
| `_launch` | **Python launcher 实际调用的路径**：接收已类型化的 C++ 参数，用 `packed struct` 打包。 |
| `launch(PyObject*, PyObject*)` | Python 模块入口：`PyArg_ParseTuple` 拆参 → 调 `_launch`。 |
| `ModuleMethods`/`PyInit` | 把 `launch` 注册为 Python 模块方法。 |

#### 4.3.2 核心流程

`make_launcher` 内部先定义一批**闭包辅助函数**，把签名转换成 C++ 需要的「类型、格式字符、变量名」，再用这些结果渲染模板：

```text
signature: {0: "*fp32", 1: "i32", 2: "constexpr", ...}   # 下标 → 类型字符串
   │
   ├─ _serialize_signature(sig)   : 元组展平成逗号串
   ├─ _extracted_type(ty)         : → C 变量声明类型 (指针/constexpr→PyObject*，标量→int32_t/float...)
   ├─ format_of(ty)               : → PyArg_ParseTuple 格式字符 (指针/constexpr→"O"，标量→"f"/"i"/"L"...)
   └─ ty_to_cpp(ty)               : → C++ 类型 (用于打包 struct)
   │
   ▼
args_format = ''.join(format_of(ty) for ty in signature.values())
format      = "iiiKKOOOO" + args_format          # 9 个固定头 + 可变参数
arg_decls   = "int32_t arg0, void* arg1, ..."     # _launch 的形参列表
ptr_decls   = "DevicePtrInfo ptr_info0 = getPointer(_arg0,0); ..."  # 指针提取
   │
   ▼
return f"""...整段 C++..."""                       # 模板渲染
```

固定头部 `"iiiKKOOOO"` 的含义（与 4.1.3 的 Python `runner` 参数顺序一一对应）：

| 格式 | 数量 | 含义 |
| --- | --- | --- |
| `i` | 3 | `gridX, gridY, gridZ`（int） |
| `K` | 2 | `stream, function`（`unsigned long long`，两个指针） |
| `O` | 4 | `packedMetadata, launch_metadata, launch_enter_hook, launch_exit_hook`（`PyObject*`，原样） |

其后追加的 `args_format`，则是按 kernel 每个参数的类型生成的格式字符。

#### 4.3.3 源码精读

函数签名与元数据提取：

[third_party/ascend/backend/driver.py:428-440](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py#L428-L440) —— 从 `metadata` 读出 `workspace_size`、`lock_num`/`lock_init_value`、`bs_task_type`、`mix_mode`、`compile_on_910_95`、`parallel_mode`，并据此推导 `enable_simt`（`"simt" in parallel_mode or force_simt_only`）。这些值会决定后面模板里多个条件分支。

三个核心辅助闭包：

[third_party/ascend/backend/driver.py:442-479](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py#L442-L479) —— `_serialize_signature` 处理元组类型；`_extracted_type` 决定每个 `_argN` 的 C 声明类型（指针/constexpr 一律 `PyObject*`，标量走 `ty_to_cpp`）；`format_of` 决定 `PyArg_ParseTuple` 的格式字符（指针/constexpr/`void*` 一律 `"O"`，标量查表如 `float→"f"`、`int64_t→"L"`）。

格式串与变量名列表的拼装：

[third_party/ascend/backend/driver.py:509-514](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py#L509-L514) —— `format = "iiiKKOOOO" + args_format`；`signature` 被重建成「下标 → 单个类型字符串」的字典，便于后续按 `_arg{i}` 命名。

[third_party/ascend/backend/driver.py:517-531](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py#L517-L531) —— 三个关键产物：`arg_decls`（`_launch` 形参声明，跳过 `constexpr`）、`internal_args_list`（调 `_launch` 时：指针传 `ptr_info{i}.dev_ptr`，标量传 `_arg{i}`）、`ptr_decls`（每个指针参数插一句 `getPointer` 调用，失败立即 `return NULL`）。

`ty_to_cpp` 类型映射表（标量 → C++ 类型，指针统一 `void*`）：

[third_party/ascend/backend/driver.py:405-424](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py#L405-L424) —— 例如 `i32→int32_t`、`fp32→float`、`fp64→double`；首字符为 `*` 的指针类型一律 `void*`。注意 `fp16`/`bf16` 在主机侧也按 `float` 处理（主机侧只用它来做形状上报，不直接参与计算）。

两条启动路径的分叉（普通 vs 950+SIMT）：

[third_party/ascend/backend/driver.py:809-832](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py#L809-L832) —— 默认用 `rtKernelLaunch(func, blockNum, args_ptr, args_size, NULL, stream)`；当 `compile_on_910_95 and enable_simt` 时改用 `rtKernelLaunchWithFlagV2`，并额外通过 `rtTaskCfgInfo_t.localMemorySize = shared_mem_dynamic_size` 携带 SIMT 模板所需的本地内存大小。这正是本讲与 u6（SIMT 路径）的衔接点之一。

`_launch` 用 `packed struct` 把所有参数铺成连续内存（Python launcher 实际走的路径）：

[third_party/ascend/backend/driver.py:1063-1079](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py#L1063-L1079) —— `struct __attribute__((packed))` 内依次放 `ffts_addr`（可选）、`syncBlockLock`、`workspace_addr`、各 kernel 参数（按类型对齐到 4 或 8 字节）、`gridX/Y/Z`、`DTData`（可选），再用聚合初始化 `{...}` 填值。`__attribute__((packed))` 保证「无填充紧凑布局」，与设备侧内核期望的参数布局对齐。

Python 模块入口与注册：

[third_party/ascend/backend/driver.py:1122-1190](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py#L1122-L1190) —— `launch(self, args)` 先声明所有 `_argN`，再 `PyArg_ParseTuple` 拆参，接着读 `packedMetadata` 拿 `kernel_name`/`tensor_kinds`，然后调 `_launch`。

[third_party/ascend/backend/driver.py:1192-1213](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py#L1192-L1213) —— 模块名 `__triton_launcher`（与 `NPULauncher.__init__` 里 `spec_from_file_location("__triton_launcher", ...)` 对应），`launch` 注册为 `METH_VARARGS`。

#### 4.3.4 代码实践

**实践目标**：观察「签名 → C++ 代码」的映射关系。

1. 操作步骤：在 4.2.4 dump 出的 `launcher_cxx11abi{abi}.cxx` 里搜索 `_arg0`、`_arg1`，找到形如 `{_extracted_type(ty)} _arg{i};` 的变量声明行，以及 `format` 字符串（在 `PyArg_ParseTuple(args, "...")` 里）。
2. 观察现象：对照你的 kernel 形参表，确认每个参数的类型与格式字符的对应关系（指针参数是 `PyObject* _argN` + 格式 `O`；`int32` 标量是 `int32_t _argN` + 格式 `i`）。
3. 预期结果：你能在 `.cxx` 里逐参数地「指认」出它来自 kernel 签名的哪一位，从而验证 `make_launcher` 确实是按签名定制的。
4. 此为源码阅读型实践，结论可从生成代码直接得出；若未开启 `TRITON_DEBUG`，则「待本地验证」。

#### 4.3.5 小练习与答案

- **练习 1**：`"iiiKKOOOO"` 一共描述了几个固定参数？分别对应 Python `runner` 里的哪些实参？
  - **答案**：9 个 = 3 个 `i`（gridX/Y/Z）+ 2 个 `K`（stream、function）+ 4 个 `O`（packedMetadata、launch_metadata、launch_enter_hook、launch_exit_hook），与 `runner` 的前 9 个固定实参一一对应。
- **练习 2**：为什么 `_launch` 用 `__attribute__((packed))` 的 struct，而不是逐个 `rtSetXXX` 设参数？
  - **答案**：NPU 内核期望一段连续、紧凑、对齐确定的参数内存；用 `packed struct` + 一次 `rtKernelLaunch(... &args, sizeof(args) ...)` 是最直接、零开销的拼装方式，避免了反复的运行时类型判断。

---

### 4.4 参数解析 PyArg_ParseTuple 与 getPointer 指针提取

#### 4.4.1 概念说明

C 侧 `launch(PyObject* self, PyObject* args)` 拿到的 `args` 是一个「纯 Python 元组」，里面既有 `int`（grid）、又有 `PyObject*`（张量、元数据字典）、还有标量。要把它变成 C 可用的形式，需要两步：

1. **`PyArg_ParseTuple`**：按格式串把元组拆成 C 变量。**关键点**：张量参数此时只是个 `PyObject*`（格式 `O`），还没有变成设备地址。
2. **`getPointer`**：再把这些 `PyObject*` 一个个转换成设备地址（`void*`），并校验有效性。

这两步合起来，就是「Python 对象 → 设备可用的连续参数缓冲区」的全过程。

#### 4.4.2 核心流程

```text
launch(self, args):
  声明 _arg0, _arg1, ... (类型由 _extracted_type 决定；指针/constexpr 都是 PyObject*)
  │
  ├─ PyArg_ParseTuple(args, "iiiKKOOOO" + args_format,
  │                   &gridX,&gridY,&gridZ,&stream,&function,
  │                   &packedMetadata,&launch_metadata,&enter,&exit,
  │                   &_arg0, &_arg1, ...)            # 失败返回 NULL
  │
  ├─ (msprof L1) 从指针型 _argN 抽取 tensorShapes
  ├─ 从 packedMetadata 读 kernel_name、tensor_kinds
  │
  ├─ ptr_decls:  对每个指针参数执行
  │     DevicePtrInfo ptr_infoN = getPointer(_argN, N);
  │     if (!ptr_infoN.valid) return NULL;            # 失败立即终止
  │
  └─ _launch(kernelName, function, stream, gridX,Y,Z, tensorShapes, tensorKinds,
             ..., ptr_info0.dev_ptr, _arg1, ptr_info2.dev_ptr, ...)
             │                         (internal_args_list: 指针→dev_ptr, 标量→_argN)
             ▼
          packed struct args = {...};  rtKernelLaunch(...)
```

#### 4.4.3 源码精读

变量声明 + `PyArg_ParseTuple`：

[third_party/ascend/backend/driver.py:1132-1141](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py#L1132-L1141) —— 第 1132 行声明所有 `_argN`（指针/constexpr 为 `PyObject*`，标量为对应 C 类型）；1133–1141 行用前面拼好的 `format` 串解析。注意指针参数也用 `&_argN` 取址，但格式是 `O`，所以拿到的是 `PyObject*`，**尚不是设备地址**。

`getPointer` 的三种取值路径（指针提取的核心）：

[third_party/ascend/backend/driver.py:620-663](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py#L620-L663) —— 返回 `DevicePtrInfo{dev_ptr, valid}`，三条分支：

1. **整数地址**：`PyLong_Check(obj)` 为真 → `PyLong_AsUnsignedLongLong` 直接当作设备地址（支持用户传裸整数地址）。
2. **None**：当作「合法的空指针」返回（`valid=true, dev_ptr=0`）。
3. **张量对象**：调用 `obj.data_ptr()`（用 `PyUnicode_InternFromString("data_ptr")` 缓存 intern 字符串以加速热路径），把返回值转成指针。返回非 `PyLong` 或无 `data_ptr` 方法则 `valid=false`。

这段代码有一个性能细节值得初学者注意：`static PyObject *data_ptr_str = PyUnicode_InternFromString("data_ptr")` 是**函数局部 static**，只在首次调用时构造一次 intern 字符串，后续 `getPointer` 直接复用，避免每次启动都重建临时字符串对象。

指针提取在 `launch` 里的实际插入点：

[third_party/ascend/backend/driver.py:527-531](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py#L527-L531) —— `ptr_decls` 列表为每个指针参数生成一句 `getPointer(_argN, N)`，并在 `launch` 函数体里「尽早」执行（raise exception asap），任何一个无效就 `return NULL`，不会继续往下走。

调用 `_launch` 时指针与标量的差异化传参：

[third_party/ascend/backend/driver.py:1178](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py#L1178) —— 这一行把 `internal_args_list` 拼进 `_launch(...)` 调用：指针参数传的是 `ptr_infoN.dev_ptr`（已提取的设备地址），标量参数传的是 `_argN`（C 值本身）。这正是 `getPointer` 的产物被消费的地方。

最后，对齐打包时还有一个「按大小推断对齐」的小算法（出现在 `triton_launch_kernel` 的 C API 路径里），其数学含义是经典的「向上取整到 2 的幂」：

\[ \text{align}(o, a) = (o + a - 1)\;\&\;\sim(a - 1) \]

即把偏移 \(o\) 向上 round 到 \(a\) 的整数倍（要求 \(a\) 为 2 的幂，本处只用 8/4/1）。`_launch` 的 packed struct 路径则用 `__attribute__((aligned(...)))` 让编译器在编译期完成等价对齐，运行期零开销。

> 补充：`packed_metadata` 里到底装了什么？由 `AscendBackend.pack_metadata` 决定，只挑运行时真正需要的 4 个字段：

[third_party/ascend/backend/compiler.py:1234-1252](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L1234-L1252) —— `kernel_name`（超 49 字符则取尾部，因 CANN 限制 ≤50）、`hash`、`debug`、`tensor_kinds`。`launch` 正是从这个字典里读 `kernel_name`/`tensor_kinds`（见 4.3.3 的 L1162–1173）。

#### 4.4.4 代码实践

**实践目标**：在真实生成的 `.cxx` 里定位 `PyArg_ParseTuple` 与 `getPointer`，验证它们与签名的关系。

1. 操作步骤（承接 4.2.4）：打开 `launcher_cxx11abi{abi}.cxx`。
   - 搜索 `PyArg_ParseTuple`，记下它的格式串，数一数 `iiiKKOOOO` 后面跟了几个字符，与你 kernel 的形参个数比较。
   - 搜索 `static inline DevicePtrInfo getPointer`，通读三条分支。
   - 搜索 `ptr_info`，确认每个张量形参都对应一次 `getPointer` 调用。
2. 观察现象：格式串后缀长度 = kernel 非 constexpr 形参个数；张量形参对应 `O`，标量形参对应类型字符；`_launch(...)` 调用里张量位置写的是 `ptr_infoN.dev_ptr`。
3. 预期结果：你能画出一张「kernel 形参 → 格式字符 → C 变量 → 进入 `_launch` 的形式」对照表。
4. 运行结果「待本地验证」（需 NPU 环境触发编译与 dump），但所有结论均可从源码生成逻辑直接推出。

#### 4.4.5 小练习与答案

- **练习 1**：一个 kernel 形参是 `x: tl.constexpr`。在生成的 `.cxx` 里，它会在 `PyArg_ParseTuple` 中占用一个格式字符吗？会进入 `_launch` 的实参吗？
  - **答案**：会占用一个格式字符（`O`，因为 `_extracted_type("constexpr")=="PyObject*"`、`format_of` 返回 `"O"`，且 `args_format` 来自 `signature.values()` 仍含 constexpr）。但它**不会**进入 `_launch` 实参：`arg_decls`、`internal_args_list`、packed struct 都用 `if ty != "constexpr"` 过滤掉了它（constexpr 在编译期已固化，运行时不需要传递）。
- **练习 2**：若用户给一个指针参数传了 `None`，`getPointer` 会怎样？内核会收到什么？
  - **答案**：走 `obj == Py_None` 分支，返回 `{valid=true, dev_ptr=0}`，即「合法的空指针」；内核收到地址 `0`（通常用于可选输出）。

---

## 5. 综合实践

把本讲四个模块串起来，完成一次「**从 Python 调用到 rtKernelLaunch**」的端到端追踪。

任务：选取 `third_party/ascend/tutorials/01-vector-add.py`（u1-l4 已跑通），开启 `TRITON_DEBUG=1` 运行，然后：

1. 在 stdout 里找到 `Dumping launcher_cxx11abi*.cxx to <dir>`，打开那个 `.cxx`。
2. 在 `.cxx` 里完成以下「打卡」并填写一张表：
   - 找到 `PyArg_ParseTuple` 的格式串，拆出 `iiiKKOOOO` 与后缀，把后缀逐字符对应到 vector-add kernel 的形参（`x_ptr, y_ptr, n_elements, BLOCK_SIZE: constexpr`）。
   - 找到 `getPointer`，确认 `x_ptr`/`y_ptr` 各对应一次 `ptr_infoN.dev_ptr`。
   - 找到 `_launch` 的 `packed struct args`，确认 `n_elements`（标量）与两个指针在其中各自的位置。
   - 找到 `rtKernelLaunch`（或 950+SIMT 下的 `rtKernelLaunchWithFlagV2`），确认它用的是 `&args`、`sizeof(args)`。
3. 把上述对应关系画成一张「Python 实参 → 格式字符 → C 变量 → packed struct 字段 → rtKernelLaunch」的流程图。
4. 进阶（可选）：修改 vector-add 的 `BLOCK_SIZE`（触发不同特化），重新运行，对比新旧 `.cxx` 的差异，验证「签名/constexpr 变化 → 源码变化 → 缓存失效重编」的结论。

> 说明：步骤 1–3 为源码阅读型实践，可在仅有 dump 文件时完成；是否真正在 NPU 上 `rtKernelLaunch` 则「待本地验证」。

---

## 6. 本讲小结

- `NPULauncher` 是「一次构造、反复调用」的桥：构造时按签名生成并编译专属 `.so`、加载取出 `launch`；调用时透传参数。它由 core `CompiledKernel._init_handles` 懒创建，参数顺序由 core `runner` 固定为 `(gridX,Y,Z, stream, function, packed_metadata, launch_metadata, enter_hook, exit_hook, *kernel_args)`。
- `make_npu_launcher_stub` 用 `sha256(header+wrapper)` 做内容寻址缓存，缓存名带 CXX11 ABI（torch_npu=1、mindspore=0）以隔离二进制接口；`debug=True` 时 dump `precompiled.h` 与 `launcher_cxx11abi{abi}.cxx`。
- `make_launcher` 是一个「C++ 模板渲染器」：用 `_extracted_type`/`format_of`/`ty_to_cpp` 把签名转成 C 类型、格式字符、变量名，再渲染出含 `triton_launch_kernel`（C API）、`_launch`（Python 走的 packed struct 路径）、`launch`（Python 入口）的整段源码。
- 参数解析分两步：`PyArg_ParseTuple` 按 `"iiiKKOOOO"+后缀` 把 Python 元组拆成 C 变量（指针此时只是 `PyObject*`）；再由 `getPointer` 把张量对象转成设备地址（整数/None/`data_ptr()` 三分支），失败即 `return NULL`。
- 启动分两条路径：普通 `rtKernelLaunch`，950+SIMT 用 `rtKernelLaunchWithFlagV2` 并携带 `localMemorySize`；这是与 u6（SIMT 双路径）的衔接点。
- torch_npu 与 mindspore 的差异通过 `get_backend_func` 策略表注入到生成的 C++ 片段中（头文件、内存分配、异步启动等）。

---

## 7. 下一步学习建议

- **下一讲 u5-l3**：把 `launch_args`、`workspace`、`sync_block_lock` 这些资源**在 `_launch` 内部的分配与初始化**讲透，并对比 `rtKernelLaunch` 与 `rtKernelLaunchWithFlagV2` 的运行时差异——本讲已铺好参数打包的底子，u5-l3 聚焦「资源」。
- **横向回看 u6-l1/l2**：本讲反复出现的 `enable_simt`、`force_simt_only`、`parallel_mode` 来自 `NPUOptions` 的 `compile_mode`；学完 u6 会更清楚这两条启动路径对应的编译侧分流。
- **建议精读的源码**：
  - `third_party/ascend/backend/driver.py` 的 `make_launcher` 全文（本讲只精读了关键片段，完整读一遍能建立全局模板观）。
  - `third_party/ascend/backend/backend_register.py` 的 torch_npu/mindspore 各策略函数（理解两套宿主库的差异注入点）。
  - `third_party/ascend/backend/npu_utils.cpp`（u5-l1 已涉及，可对照本讲的 `cpp_npu_utils_dlopen`，理解 launcher 为何要 `dlopen` 它来分配 workspace/sync_block_lock）。
