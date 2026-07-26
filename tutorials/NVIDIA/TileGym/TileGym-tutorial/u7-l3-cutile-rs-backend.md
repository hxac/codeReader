# cuTile-rs Rust FFI 后端

## 1. 本讲目标

本讲是「多后端实现」单元的第三讲，承接 u7-l1（tilecpp 后端）和 u5-l3（cuTile 自动调优）。学完后你应该能够：

- 理解 cutile-rs 后端如何用 **Rust** 写 GPU 内核，并编译成一个 **cdylib**（C 动态库）`libcutile_kernels.so`。
- 看懂 Python 与 Rust 之间的 **cffi / cdef 跨语言边界**，以及数据如何以 `TensorDesc` 这个 `#[repr(C)]` 结构体跨界搬运。
- 理解「按需 autobuild」与后端可用性探测的设计取舍。
- 解释为什么 cutile-rs 的自动调优器用 **CUPTI**（`torch.profiler`）而非 `cuda.Event` 计时，以及由此引出的「`kernel_fn` 只能用 `torch.empty`」铁律。

一句话定位：cutile-rs 与 cuTile（默认后端）、tilecpp、triton 一样，都是挂在**同一个算子名**下的一种实现；它用 Rust 替代 Python DSL，用运行时 `cargo` 编译替代运行时 `tileiras`，复用同一套分发机制。理解本讲后，你会清楚「算子名是全局键、后端只是子键、实现语言无关」这条贯穿全库的结论。

## 2. 前置知识

本讲假设你已经掌握以下内容（来自前置讲义），这里只做最小回顾：

- **后端分发机制**（u2-l2）：`_REGISTRY` 是 `{算子名: {后端: 实现}}` 的嵌套字典，`@register_impl("<op>", backend="cutile-rs")` 把实现挂到同一算子名下，`dispatch` 按「当前后端」查表。后端名是**字符串**（这里是 `"cutile-rs"`，带连字符），与实现语言无关。
- **tilecpp 后端**（u7-l1）：一个 C++ 后端的完整范式——「`.cuh` 内核源 + `.py` 包装」成对出现，靠 `TileCppKernel` 引擎做「显式模板实例化 → nvcc 编译 → cubin 缓存 → 修饰名查找 → 加载」，并用 `is_backend_available` 门控注册。cutile-rs 的整体骨架与之高度同构，主要差异在**编译器**（cargo vs nvcc）与**加载方式**（cffi dlopen vs cubin）。
- **cuTile 自动调优**（u5-l3）：tune-once / cache / launch 模式——首次按 `cache_key` 遍历候选配置选最优，把结果缓存进模块级字典，之后零开销启动；并由全局开关 `TILEGYM_DISABLE_AUTOTUNE` 控制。cutile-rs 的调优器是**独立实现**，模式相同但计时手段不同。

此外需要一点 Rust 基础直觉（不懂也不影响主线）：

- **`#[repr(C)]`**：强制结构体按 C 语言内存布局排列，是跨语言 FFI 的前提。
- **`extern "C"`**：函数使用 C 调用约定（名称修饰、参数传递方式与 C 一致）。
- **`#[no_mangle]`**：禁止编译器对符号名做「修饰」（mangling），导出原始函数名供 C 端按名字 `dlsym`。
- **`ManuallyDrop`**：包装一个值使其析构函数（`Drop`）不运行——本讲用它实现「借用但不释放」PyTorch 显存。

## 3. 本讲源码地图

本讲涉及的关键文件与职责：

| 文件 | 职责 |
|------|------|
| [src/tilegym/ops/cutile_rs/cutile_kernels/Cargo.toml](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile_rs/cutile_kernels/Cargo.toml) | Rust crate 清单：`crate-type = ["cdylib"]`，声明把所有内核编译成一个共享库 |
| [src/tilegym/ops/cutile_rs/cutile_kernels/src/lib.rs](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile_rs/cutile_kernels/src/lib.rs) | crate 根，用 `mod + include!` 聚合每个算子的 `kernel.rs` / `ffi.rs` |
| [src/tilegym/ops/cutile_rs/ffi_util.rs](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile_rs/ffi_util.rs) | 共享 FFI 工具：`TensorDesc` 结构体、`borrow_tensor`、dtype 码、返回码、宏 |
| [src/tilegym/ops/cutile_rs/matmul_kernel/kernel.rs](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile_rs/matmul_kernel/kernel.rs) | Rust 版 matmul 设备内核（`#[cutile::module]`） |
| [src/tilegym/ops/cutile_rs/matmul_kernel/ffi.rs](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile_rs/matmul_kernel/ffi.rs) | matmul 的 C-ABI 导出 `cutile_matmul` |
| [src/tilegym/ops/cutile_rs/matmul.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile_rs/matmul.py) | Python 包装：校验、grid 计算、调用 `autotune_launch`、cffi 调用 |
| [src/tilegym/backend/cutile_rs/utils.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/cutile_rs/utils.py) | cffi 加载器、`TensorDesc` 打包、autobuild、小工具 |
| [src/tilegym/backend/cutile_rs/autotuner.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/cutile_rs/autotuner.py) | 基于 CUPTI 的自动调优器 `autotune_launch` |
| [src/tilegym/backend/selector.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/selector.py) | `is_cutile_rs_available()` 可用性探测 |

一个值得记住的目录分工：**基础设施**（autotuner、cffi 加载器）住在 `backend/cutile_rs/`，**算子包装与内核 crate** 住在 `ops/cutile_rs/`——这一点 `backend/cutile_rs/__init__.py` 的文档串明确说明。

## 4. 核心概念与源码讲解

### 4.1 Rust kernel crate + cdylib

#### 4.1.1 概念说明

cuTile 默认后端用 Python DSL（`@ct.kernel`）写内核，交给运行时编译器 `tileiras` 做 JIT。cutile-rs 走另一条路：**用 Rust 写内核**（同一个 cuTile 编程模型的 Rust 移植，由 crates.io 上的 `cutile` crate 提供），再用 Rust 工具链把它编译成一个**共享库**，供 Python 以 C 调用约定直接调用。

这里的核心概念是 **cdylib**（C dynamic library）：

- Rust 的 `crate-type = ["cdylib"]` 告诉 cargo：把这个 crate 编译成 `.so`（Linux）/ `.dylib`（macOS）/ `.dll`（Windows），并导出其中带 `#[no_mangle] extern "C"` 标记的函数为 C-ABI 符号。
- 编译产物 `libcutile_kernels.so` 就像一个普通的 C 共享库，任何能 `dlopen` 的语言（包括 Python 的 cffi）都能按符号名调用它。

关键设计取舍：**所有 cutile-rs 内核编译进同一个 cdylib**，而不是每个算子一个 crate。`Cargo.toml` 头部的注释把这层意图写得很清楚：

> Single cdylib for ALL cutile-rs kernels. ... One Cargo.toml + one (gitignored) Cargo.lock + one target/ for every kernel — no per-kernel crate, no shared cutile-rs checkout.

这样做的收益是：一次 `cargo build` 算完所有算子、依赖只编译一遍、缓存只维护一份 `.so`。

#### 4.1.2 核心流程

从 Rust 源码到一个可调用的符号，流程是：

1. 每个算子贡献一对纯 `.rs` 文件：`<op>_kernel/kernel.rs`（设备内核）与 `<op>_kernel/ffi.rs`（C-ABI 导出）。
2. crate 根 `src/lib.rs` 用 `mod <op> { include!(...) }` 把每对文件**文本嵌入**进 crate，并各自包进独立 `mod`，避免各算子的 `use` 语句在 crate 根冲突。
3. `#[path = "../../ffi_util.rs"] mod ffi_util;` 把共享的 FFI 工具纳入 crate，供每个 `ffi.rs` 通过 `crate::ffi_util::...` 使用。
4. `cargo build --release` 编译 crate，产出 `target/release/libcutile_kernels.so`，其中每个 `#[no_mangle]` 函数（如 `cutile_matmul`）都是一个全局可见的 C 符号。
5. 每个 `ffi.rs` 通过 `#[cutile::entry()]` 标注的设备内核函数，最终经 `cutile-compiler` 编译成 GPU cubin，由 cuda-core 启动到指定 stream 上。

#### 4.1.3 源码精读

**crate 清单**——注意 `crate-type = ["cdylib"]` 与依赖都钉死在 `=0.2.0` 以保证可复现（[src/tilegym/ops/cutile_rs/cutile_kernels/Cargo.toml:15-28](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile_rs/cutile_kernels/Cargo.toml#L15-L28)）：

```toml
[lib]
name = "cutile_kernels"
crate-type = ["cdylib"]
path = "src/lib.rs"

[dependencies]
cutile = "=0.2.0"
cutile-compiler = "=0.2.0"
cutile-macro = "=0.2.0"
cuda-core = "=0.2.0"
cuda-async = "=0.2.0"
```

`cutile` transitively 拉入 `cutile-ir`，`cuda-bindings` 经 `cuda-core` 引入——也就是说，Rust 端用的 Tile IR 与 cuTile Python 端是同一套。

**crate 根聚合**——`lib.rs` 用 `include!` 宏文本嵌入各算子源，并用独立 `mod` 隔离（[src/tilegym/ops/cutile_rs/cutile_kernels/src/lib.rs:16-42](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile_rs/cutile_kernels/src/lib.rs#L16-L42)）：

```rust
#[path = "../../ffi_util.rs"]
mod ffi_util;

mod matmul {
    include!("../../matmul_kernel/kernel.rs");
    include!("../../matmul_kernel/ffi.rs");
}
// ...bmm / silu_and_mul / swiglu / attention_sink 同构
```

注释点明「新增算子」的步骤：丢一对 `<op>_kernel/{kernel.rs,ffi.rs}` 文件，再加一个 `mod <op> { ... }` 块即可。

**Rust 设备内核**——仍是 tile 编程模型。matmul 的非持久化变体用 `#[cutile::module]` + `#[cutile::entry()]` 标注，const 泛型 `BM/BN/BK` 即编译期瓦片尺寸（[src/tilegym/ops/cutile_rs/matmul_kernel/kernel.rs:13-32](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile_rs/matmul_kernel/kernel.rs#L13-L32)）：

```rust
#[cutile::module]
pub mod matmul_module {
    use cutile::core::*;
    #[cutile::entry()]
    pub unsafe fn non_persistent_matmul_kernel<
        E: ElementType, const BM: i32, const BN: i32, const BK: i32, const CAST_TF32: i32,
    >(
        a: &Tensor<E, { [-1, -1] }>,
        b: &Tensor<E, { [-1, -1] }>,
        c: &Tensor<E, { [-1, -1] }>, // OUTPUT — 只读形参，靠 partition_full_mut 写回
    ) { ... }
}
```

内核体（`kernel.rs` 后半段）做的事情与 cuTile 版 matmul 一一对应：`get_tile_block_id` 取瓦片坐标、GROUP_SIZE_M=8 的 swizzle 重排、`partition(const_shape![BM,BK])` 切瓦片、`load_view_tko(..., tma::Enabled)` 走 TMA 加载、`mmaf` 张量核心乘加、`convert_tile` 在 fp32 累加器与 `E` 之间转换、`store_view_tko_mut` 写回。可见 **tile 编程模型与语言无关**——cuTile、tilecpp、cutile-rs 三者算法骨架相同。

**C-ABI 导出**——`ffi.rs` 把内核包装成一个返回 `i32` 返回码的 C 函数（[src/tilegym/ops/cutile_rs/matmul_kernel/ffi.rs:29-53](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile_rs/matmul_kernel/ffi.rs#L29-L53)）：

```rust
#[unsafe(no_mangle)]
pub unsafe extern "C" fn cutile_matmul(
    c: *const TensorDesc, a: *const TensorDesc, b: *const TensorDesc,
    bm: i32, bn: i32, bk: i32,
    group_size_m: i32, num_programs: i32,
    num_cta_in_cga: i32, occupancy: i32,
    persistent: i32, device_id: i32, raw_stream: u64,
) -> i32 { ... }
```

这就是 Python 端要 `dlsym` 的符号名 `cutile_matmul`。函数体（`ffi.rs:62-116`）用宏 `dispatch_by_dtype!` 按 dtype 分派到特化的 `<E>`，拼好 `.generics(...)`、`.grid(...)`、`.compile_options(...)`，最后 `op.sync_on(&stream)` 同步启动并把结果映射成返回码。

#### 4.1.4 代码实践

**实践目标**：在不开 GPU 的情况下，搞清楚「新增一个 cutile-rs 算子」要在 Rust 侧改哪些地方。

**操作步骤**：

1. 打开 `src/tilegym/ops/cutile_rs/cutile_kernels/src/lib.rs`，找到四个 `mod` 块（matmul / bmm / silu_and_mul / swiglu / attention_sink）。
2. 打开任一 `<op>_kernel/` 目录（如 `matmul_kernel/`），确认里面正好只有 `kernel.rs` 和 `ffi.rs` 两个文件。
3. 对照 `lib.rs` 注释「To add an op: drop its `<op>_kernel/{kernel.rs,ffi.rs}` and add a `mod <op> { ... }` block」，写出新增一个假想算子 `add` 所需的最小改动。

**预期结果**：你会得出「在 `lib.rs` 增加一个 `mod add { include!("../../add_kernel/kernel.rs"); include!("../../add_kernel/ffi.rs"); }` 块」这一结论，且不需要新建 crate、不需要改 `Cargo.toml`。

**待本地验证**：若机器上有 `cargo` 与 CUDA 头文件，可仿照 `matmul_kernel` 真建一对文件、`cd ops/cutile_rs/cutile_kernels && cargo build --release`，观察 `target/release/libcutile_kernels.so` 是否生成、`nm -D libcutile_kernels.so | grep cutile_add` 是否能查到新符号。

#### 4.1.5 小练习与答案

**练习 1**：为什么 cutile-rs 把所有算子编进一个 cdylib，而不是每算子一个 crate？

> 参考答案：一次 `cargo build` 编完所有算子、公共依赖（`cutile`/`cuda-core` 等）只编译一遍、只维护一个 `target/` 与一个 `.so` 缓存。每算子一个 crate 会带来重复编译与多份缓存，且 `cutile` 依赖体积不小，得不偿失。

**练习 2**：`lib.rs` 里为什么每个算子都要包进独立的 `mod <op> { ... }`，而不是直接把所有 `include!` 平铺在 crate 根？

> 参考答案：每个算子的 `kernel.rs`/`ffi.rs` 都有自己的 `use` 语句，平铺会让它们在 crate 根同名冲突（例如多个算子都 `use cutile::core::*`）。独立 `mod` 给每个算子一个独立命名空间；而 `#[no_mangle]` 导出的符号仍是全局的，不影响 Python 按名字查找。

---

### 4.2 cffi / cdef 与 TensorDesc 跨界

#### 4.2.1 概念说明

Rust 把内核编成了 `.so`，但 Python 怎么调用它？cutile-rs 选择了 **cffi 的 ABI 模式**：你把要调用的 C 函数签名写成一段字符串（`cdef`），cffi 在运行时 `dlopen` 这个 `.so` 并按符号名生成可调用对象。相比手写一长串 ctypes `argtypes`，cdef 只需声明一次、与 `.so` 的真实签名漂移更少（`utils.py:204-209` 的注释称之为「lighter, drift-resistant alternative」）。

数据跨界靠一个共同的 C 结构体 **`TensorDesc`**：Python 端把 `torch.Tensor` 的 `data_ptr / ndim / shape / strides / dtype` 打包成一个 `TensorDesc`，传指针给 Rust；Rust 端解包成借用视图。这是整个 FFI 的**边界契约**——Rust 的 `#[repr(C)] struct TensorDesc` 与 Python 的 `_TENSORDESC_CDEF` 字符串必须逐字段对齐，否则会读到错位的内存。

两个关键术语：

- **cffi ABI 模式 vs API 模式**：ABI 模式无需 C 编译器、纯靠 `cdef` 字符串 + `dlopen`；API 模式需让 cffi 调用 C 编译器生成绑定。cutile-rs 用 ABI 模式，部署更轻。
- **`ManuallyDrop` 与 FFI 所有权闸门**：PyTorch 才是显存的真正所有者，Rust 只是「借用」。若 Rust 端构造的 `Tensor<E>` 在作用域结束触发 `Drop`，就会误释放 PyTorch 的显存。`borrow_tensor` 返回 `ManuallyDrop<Tensor<E>>`，使其析构变成空操作，从而保证显存不被 Rust 释放（`ffi_util.rs:218-233`）。

#### 4.2.2 核心流程

一次跨界调用的数据流：

1. Python 包装器调 `bind_kernel_function_cffi(kernel, cdef)`：首次会触发 autobuild + `dlopen`，把 `(ffi, lib)` 缓存进 `_cffi_kernel_libs`；之后直接命中缓存。
2. `ffi.cdef(_TENSORDESC_CDEF + cdef)`：先注册共享的 `TensorDesc` typedef，再注册该算子的函数签名（所以每个算子的 `cdef` 里可以直接写 `const TensorDesc*` 形参）。
3. 对每个张量调 `make_tensor_desc(ffi, t)`：校验是 CUDA 张量、维度 ≤ 5、dtype 受支持，然后填充 `TensorDesc` 各字段，返回一个 cdata 指针（**调用方必须保活到 FFI 调用之后**，否则会被 GC 释放）。
4. `lib.cutile_<op>(cd, ad, bd, ...)` 触发真正的 C 调用，返回一个 `int` 返回码。
5. `check_rc(rc, fn_name)` 把非 0 返回码翻译成 `RuntimeError`。
6. Rust 端 `cutile_<op>` 用 `borrow_tensor::<E>(desc)` 把 `TensorDesc` 还原成一个借用的 `Tensor<E>`，喂给设备内核，全程不持有所有权。

#### 4.2.3 源码精读

**边界契约——Python 侧**。`_TENSORDESC_CDEF` 是 Python 端对结构体的声明（[src/tilegym/backend/cutile_rs/utils.py:219-240](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/cutile_rs/utils.py#L219-L240)）：

```python
_TENSORDESC_MAX_DIMS = 5
_TENSORDESC_CDEF = """
typedef struct {
    uint64_t ptr;
    int32_t  ndim;
    int64_t  shape[5];
    int64_t  strides[5];
    int32_t  dtype;
} TensorDesc;
"""
_DTYPE_CODE = {"torch.float32": 0, "torch.float16": 1, "torch.bfloat16": 2,
               "torch.int32": 3, "torch.int64": 4, "torch.float8_e5m2": 5}
```

注意两点：`strides` 的单位是**元素**而非字节；`dtype` 是一个整数码（与 Rust 端 `dtype_str` 一一对应）。这段注释明确「MUST stay in sync with `TensorDesc` in ops/cutile_rs/ffi_util.rs」。

**边界契约——Rust 侧**。`#[repr(C)]` 保证逐字段与上面 cdef 对齐（[src/tilegym/ops/cutile_rs/ffi_util.rs:21-34](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile_rs/ffi_util.rs#L21-L34)）：

```rust
pub const MAX_DIMS: usize = 5;
#[repr(C)]
pub struct TensorDesc {
    pub ptr: u64,
    pub ndim: i32,
    pub shape: [i64; MAX_DIMS],
    pub strides: [i64; MAX_DIMS],  // strides in ELEMENTS
    pub dtype: i32,                 // 0=f32, 1=f16, 2=bf16, 3=i32, 4=i64, 5=f8e5m2
}
```

**Python 打包器**。`make_tensor_desc` 把 `torch.Tensor` 填进 `TensorDesc`（[src/tilegym/backend/cutile_rs/utils.py:243-265](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/cutile_rs/utils.py#L243-L265)）：

```python
d = ffi.new("TensorDesc *")
d.ptr = t.data_ptr()
d.ndim = t.dim()
for i in range(t.dim()):
    d.shape[i] = int(t.shape[i])
    d.strides[i] = int(t.stride(i))
d.dtype = code
return d
```

**Rust 解包器（FFI 所有权闸门）**。`borrow_tensor` 返回 `ManuallyDrop<Tensor<E>>`，析构为空操作，绝不释放 PyTorch 显存（[src/tilegym/ops/cutile_rs/ffi_util.rs:229-233](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile_rs/ffi_util.rs#L229-L233)）：

```rust
pub unsafe fn borrow_tensor<E: DType>(d: &TensorDesc) -> ManuallyDrop<Tensor<E>> {
    ManuallyDrop::new(unsafe {
        Tensor::<E>::from_raw_parts(d.ptr, d.nbytes(), 0, d.shape_i32(), d.strides_i32())
    })
}
```

**cdef 声明与调用**。每个算子的 `cdef` 是其 FFI 签名的镜像，例如 matmul（[src/tilegym/ops/cutile_rs/matmul.py:39-46](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile_rs/matmul.py#L39-L46)）：

```python
_FFI_CDEF = """
int32_t cutile_matmul(
    const TensorDesc* c, const TensorDesc* a, const TensorDesc* b,
    int32_t bm, int32_t bn, int32_t bk,
    int32_t group_size_m, int32_t num_programs,
    int32_t num_cta_in_cga, int32_t occupancy,
    int32_t persistent, int32_t device_id, uint64_t raw_stream);
"""
```

注释提醒它必须与 Rust 端 `cutile_matmul` 签名同步。调用处 `_run_ffi` 打包三个张量并传入 stream（[src/tilegym/ops/cutile_rs/matmul.py:91-116](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile_rs/matmul.py#L91-L116)）：`raw_stream = torch.cuda.current_stream(device=_dev).cuda_stream` 把 PyTorch 当前 stream 的原始句柄以 `uint64_t` 传过界，Rust 端再用 `Stream::borrow_raw` 借用，保证内核跑在调用方的 stream 上。

**返回码**。Rust 端定义错误码常量 `rc::{OK, UNSUPPORTED_DTYPE, LAUNCH_FAILED, DEVICE_INIT_FAILED, NULL_PTR, INVALID_ARGS}`（[src/tilegym/ops/cutile_rs/ffi_util.rs:103-117](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile_rs/ffi_util.rs#L103-L117)），Python 端 `_RC_MESSAGES` 与之对应、`check_rc` 把非 0 码翻译成异常（[src/tilegym/backend/cutile_rs/utils.py:321-334](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/cutile_rs/utils.py#L321-L334)）。注意 `-1` 故意不是错误码，它是编译选项「auto/default」的哨兵值（见 4.4 节）。

#### 4.2.4 代码实践

**实践目标**：验证「TensorDesc 两边对齐」与「借用不释放」这两个契约，方法是阅读型实践。

**操作步骤**：

1. 并排打开 `utils.py` 的 `_TENSORDESC_CDEF`（L220-228）与 `ffi_util.rs` 的 `struct TensorDesc`（L26-34），逐字段对照：`ptr(u64)/ndim(i32)/shape[5](i64)/strides[5](i64)/dtype(i32)`。
2. 找到 `make_tensor_desc`（L243-265）里 `d.strides[i] = int(t.stride(i))`，确认 stride 用的是 `torch.Tensor.stride(i)`（单位是元素，不是字节），与 Rust 注释「Strides are in ELEMENTS」一致。
3. 读 `borrow_tensor`（L229-233），解释为何返回 `ManuallyDrop` 而非普通 `Tensor`。

**预期结果**：你能用自己的话说明「若把 Rust 端 `strides` 改成按字节解释，或漏掉 `ManuallyDrop`，分别会发生什么」——前者会算错瓦片偏移读到越界/错位数据，后者会在每次 FFI 调用结束后误释放 PyTorch 显存。

**待本地验证**：若有 GPU 与 cutile-rs 后端，可写脚本构造一个非连续张量（如 `t[::2]`），观察 `matmul.py` 的 `a = a.contiguous()`（L134）是否在 FFI 前把它连续化——这印证了「TensorDesc 只搬运紧密布局」的隐含前提。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `make_tensor_desc` 返回的 cdata 必须被调用方保活到 `lib.cutile_<op>(...)` 之后？

> 参考答案：cffi 分配的 `TensorDesc` 由 Python GC 管理，一旦被回收，其内存可能被释放或复用，传给 Rust 的指针就会变成野指针。把 `cd, ad, bd` 都保留到 FFI 调用之后，才能保证 Rust 读到完整的描述符。

**练习 2**：`TensorDesc` 把 dtype 编码成一个整数（0=f32…5=f8e5m2）而不是传字符串，这样做的好处是什么？

> 参考答案：C-ABI 边界传整数最廉价、无字符串生命周期问题；Rust 端再用 `dtype_str(code)` 把整数映回 cutile 的类型名字符串（`"f16"` 等）用于 `.generics(...)`。整数码只需在两端各维护一张小表并保持同步。

---

### 4.3 autobuild 与可用性探测

#### 4.3.1 概念说明

cuTile 与 tilecpp 都把编译缓存（cubin）当成一种「编译产物」来管理；cutile-rs 走得更彻底——整个 `.so` 都是**按需构建**的。`backend/cutile_rs/utils.py` 提供了一套 autobuild 机制：第一次真正用到某个 cutile-rs 算子时，才检查「`.so` 是否过期」，过期就跑 `cargo build --release` 重新编译。这套机制由环境变量 `CUTILE_RS_AUTOBUILD` 控制（默认开），可用 `CUTILE_RS_AUTOBUILD=0` 关闭以钉住一个预编译 `.so`。

与之配套的是**可用性探测** `is_cutile_rs_available()`。回顾 u7-l1：tilecpp 的探测刻意「延迟且缓存」，因为它要 fork `nvcc --version`，很贵。cutile-rs 的探测策略相反——**宽松探测（Rule 35）**：只要 `cargo` 在 `PATH` 上（能按需编译），或已有一份不过期的预编译 `.so`，就报告「可用」；真正昂贵的校验（libclang、CUDA 头、tileiras）推迟到第一次 dispatch 时才做。这样 import 期的探测保持轻量。

#### 4.3.2 核心流程

autobuild 的判定与执行：

1. `_kernels_crate_dir()` 定位 crate 目录（支持 `CUTILE_RS_KERNELS_DIR` 覆盖）。
2. `_so_stale(so_path)` 判定过期：`.so` 不存在，或 `ops/cutile_rs/` 下任一 `.rs/.toml` 的 mtime 比 `.so` 新（`target/` 跳过）。
3. `_ensure_built_and_path()`：若 autobuild 开启且过期，调 `_build_kernels()` 重建，并清空 cffi 句柄缓存 `_cffi_kernel_libs`。
4. `_build_kernels()` 用 `fcntl.flock` 加文件锁，**锁内再次检查过期**，避免多进程重复编译；解析 `cargo` 为绝对路径防 PATH 劫持；以白名单环境变量 + 修正过的 `CUDA_TOOLKIT_PATH` 跑 `cargo build --release`，900 秒超时。

可用性探测 `is_cutile_rs_available()`：

1. crate 目录不存在 → 不可用。
2. autobuild 关闭（`CUTILE_RS_AUTOBUILD=0`）→ 仅当 `.so` 存在才可用。
3. autobuild 开启 → `cargo` 在 PATH 上即可用（能按需编译）；否则仅当存在且不过期的预编译 `.so` 才可用。

#### 4.3.3 源码精读

**过期判定**——遍历 `ops/cutile_rs/` 下所有 `.rs/.toml`，任一比 `.so` 新即过期（[src/tilegym/backend/cutile_rs/utils.py:85-98](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/cutile_rs/utils.py#L85-L98)）：

```python
def _so_stale(so_path: str) -> bool:
    if not os.path.isfile(so_path):
        return True
    so_mtime = os.path.getmtime(so_path)
    for root, _, files in os.walk(_ops_src_root()):
        if "target" in root.split(os.sep):
            continue
        for f in files:
            if f.endswith((".rs", ".toml")) and os.path.getmtime(os.path.join(root, f)) > so_mtime:
                return True
    return False
```

**带锁的重建**——锁内复检，避免并发冗余编译（[src/tilegym/backend/cutile_rs/utils.py:118-175](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/cutile_rs/utils.py#L118-L175)，节选关键段）：

```python
with open(lock_path, "w") as lock_f:
    fcntl.flock(lock_f, fcntl.LOCK_EX)
    if not _so_stale(so_path):
        return
    ...
    subprocess.run([cargo_bin, "cargo build --release"[1]], cwd=crate_dir, env=env,
                   check=True, capture_output=True, text=True, timeout=900)
```

注意它特意把 `cargo` 解析成绝对路径（`shutil.which("cargo")`，L150）防 PATH 劫持，并在 `CUDA_TOOLKIT_PATH` 指向的目录没有 `include/cuda.h` 时回退到 `/usr/local/cuda`（L145-148）——因为运行时入口可能把 `CUDA_TOOLKIT_PATH` 指向一个无头文件的运行时 CUDA（供 tileiras 用）。

**按需构建入口**——重建后清空 cffi 句柄缓存（[src/tilegym/backend/cutile_rs/utils.py:178-201](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/cutile_rs/utils.py#L178-L201)）：

```python
if _autobuild_enabled() and _so_stale(so_path):
    _build_kernels(crate_dir)
    _cffi_kernel_libs.clear()
```

注释指出一个重要限制：清空缓存**不会**在本进程内热重载——OS 的 `dlopen` 对同一路径会复用既有映射，就地重建的 `.so` 只在**下一个进程**生效。

**可用性探测**——宽松策略（[src/tilegym/backend/selector.py:149-181](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/selector.py#L149-L181)）：

```python
if not _autobuild_enabled():
    return os.path.isfile(so_path)              # 钉死预编译 .so
if shutil.which("cargo") is not None:
    return True                                 # 能按需编译即可
return not _so_stale(so_path)                   # 否则要现成且不过期的 .so
```

这与 `_check_backends_availability()` 里把 `"cutile-rs": is_cutile_rs_available()` 一起探测（[src/tilegym/backend/selector.py:188-195](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/selector.py#L188-L195)）衔接，最终由 `ops/cutile_rs/__init__.py` 用 `if is_backend_available("cutile-rs"):` 门控算子注册（[src/tilegym/ops/cutile_rs/__init__.py:18-23](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile_rs/__init__.py#L18-L23)）——与 tilecpp「注册受 `is_backend_available` 门控、作为导入副作用」完全同构（u7-l1）。

#### 4.3.4 代码实践

**实践目标**：观察 autobuild 的两种模式（默认 vs 关闭）行为差异。

**操作步骤**：

1. 读 README 第 5 节「Enable the cuTile-rs (Rust) backend」（[README.md:159-208](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/README.md#L159-L208)），确认官方用法是 `tilegym.set_backend("cutile-rs")`，并强调「no manual build step is required」。
2. 对照 `is_cutile_rs_available()`（selector.py:174-181），回答：在一台**没装 cargo** 的机器上，分别当 `.so` 存在/不存在、过期/不过期时，后端可用性如何？
3. 设想设 `CUTILE_RS_AUTOBUILD=0`：此时探测与 dispatch 行为会发生什么变化？

**预期结果**：
- 无 cargo 且无 `.so` → 不可用，cutile-rs 测试被 skip（README L206-208）。
- 无 cargo 但有不过期 `.so` → 可用。
- 无 cargo 但 `.so` 过期 → 不可用（因为它无法被重建）。
- `CUTILE_RS_AUTOBUILD=0` 时：探测只看 `.so` 是否存在（selector.py:174-176），dispatch 时即便源码改了也不会重建，等于「钉死预编译 .so」。

**待本地验证**：在有 cargo 的机器上，删掉 `target/release/libcutile_kernels.so`，`set_backend("cutile-rs")` 后首次调用任一算子，观察日志是否出现 `cutile-rs: building libcutile_kernels.so (cargo build --release) ...`。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `_build_kernels` 要在拿到文件锁之后**再检查一次** `_so_stale`？

> 参考答案：多个进程可能同时发现 `.so` 过期并同时尝试编译。第一个进程拿到锁、编译完成、写出新 `.so`；后续进程拿到锁时，若不再检查就会重复编译。锁内复检让「等待者」发现 `.so` 已是最新而直接返回，避免冗余编译。

**练习 2**：对比 tilecpp 的 `is_tilecpp_available`（用 `@functools.cache` 缓存、fork nvcc），cutile-rs 的 `is_cutile_rs_available` 为什么可以做得这么轻？

> 参考答案：cutile-rs 只需 `shutil.which("cargo")`（纯 PATH 查找，无子进程）就能判断「能否按需编译」，真正昂贵的 libclang/CUDA 头校验推迟到 dispatch 时由 `cargo build` 本身暴露错误。tilecpp 则必须真正运行 nvcc 才知道版本是否达标，没法用便宜的方式预判，所以才需要延迟 + 缓存。

---

### 4.4 CUPTI autotune（vs cuda.Event）

#### 4.4.1 概念说明

cutile-rs 的每个算子包装都走自动调优（如 matmul 的 `autotune_launch`，[matmul.py:178-184](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile_rs/matmul.py#L178-L184)）。这套调优器与 u5-l3 讲的 cuTile 自动调优**模式相同**（tune-once / cache / launch：首次按 `cache_key` 遍历候选、缓存最优配置、之后零开销），但**计时手段完全不同**——这也是本模块的核心：它用 **CUPTI**（经 `torch.profiler`）而非 `cuda.Event`。

为什么要换计时器？cutile-rs 内核在**首次调用**时会把 MLIR JIT 编译成 cubin，这一步可能花 **50–500 ms**。`cuda.Event` 计时本质上测的是「主机端从 record 到 end 之间的墙钟时间」，它会把这个 JIT 开销、ctypes/cffi FFI 调用开销、Python 参数编组开销**全部算进去**，于是对一个本就很小的内核产生「幽灵差距」。而 CUPTI 测的是**纯 GPU 内核执行时间**，这才是与 cuTile-Python / Triton-TileIR 做苹果对苹果比较的唯一标尺。autotuner.py 的文档串用实测数据佐证：layer_norm 2D 用 `cuda.Event` 测出 1.5×，用 CUPTI 测只有 0.96×（rs 其实更快）。

由此引出一条**铁律（Rule 16-autotuner）**：在 `kernel_fn(cfg)` 内部，所有输出张量分配**必须**用 `torch.empty`，绝不能用 `.clone()`、`torch.zeros`、`torch.ones`、`.expand().contiguous()`。原因是这些操作本身会启动 GPU 内核（设备到设备拷贝、填充），而 CUPTI 会把这些内核时间也算进基准，制造幽灵差距。文档串的实测案例：layer_norm 里一次 `.clone()` 给一个 7μs 的内核额外加了 4.8μs 的 DtoD 拷贝，导致 CUPTI 报出 1.8× 的虚假差距。

#### 4.4.2 核心流程

`autotune_launch` 的调优循环：

1. 计算 `cache_key = (kernel_name, key)`（如 matmul 的 `key=(m, n, k, dtype, persistent)`）。
2. 命中缓存 → 直接用 `entry.best_config` 再跑一次 `kernel_fn` 返回结果（`cache_hit=True`）。
3. 未命中 → 对每个候选配置调 `_bench_config_cupti`：
   - **warmup**（默认 2 次）：触发 JIT 编译，**不计入**计时；区分「配置相关错误」（非法内存/OOM，返回 `inf` 跳过该配置）与「环境错误」（缺 libcupti 等，向上抛）。
   - **rep**（默认 10 次）：每次用 `with profile(activities=[ProfilerActivity.CUDA])` 包住 `kernel_fn`，对 `key_averages()` 里 `self_device_time_total > 0`（且匹配 `kernel_filter` 正则）的事件求和，得到这一次的设备总耗时（微秒）。
   - 取 10 次的**中位数**作为该配置的得分。
4. 选 `median_ms` 最小的配置为 `best_cfg`，连同 `tuning_record` 写入缓存。
5. 返回前 `torch.cuda.synchronize()` + `gc.collect()` 清空 profiler 状态（防止调用方再包一层 profiler 导致嵌套 abort），最后用最优配置再跑一次 `kernel_fn` 把输出交给调用方。

`kernel_filter`（正则）让你只计时关心的内核——当包装器会顺带启动辅助内核时很有用。

#### 4.4.3 源码精读

**为什么要用 CUPTI**——文档串把动机与实测写得很直白（[src/tilegym/backend/cutile_rs/autotuner.py:16-26](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/cutile_rs/autotuner.py#L16-L26)）：

```python
# WHY CUPTI (not torch.cuda.Event):
#   cutile-rs JIT-compiles MLIR → cubin on first call per (kernel, generics)
#   combo. That JIT can take 50-500 ms — torch.cuda.Event measures HOST-to-host
#   time which includes JIT + ctypes FFI + Python marshalling, producing
#   phantom 1.5–2.5x perf gaps that don't exist at the GPU kernel level.
#   ...
#   Verified empirically: layer_norm 2D showed 1.5x with cuda.Event but 0.96x
#   with CUPTI (rs actually faster).
```

**铁律（Rule 16-autotuner）**——`kernel_fn` 内只准用 `torch.empty`（[src/tilegym/backend/cutile_rs/autotuner.py:36-42](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/cutile_rs/autotuner.py#L36-L42)）：

```python
# LAMBDA RULE (Rule 16-autotuner — non-negotiable):
#     Inside `kernel_fn(cfg)` ALL output tensor allocations MUST use torch.empty.
#     NEVER `.clone()`, `torch.zeros`, `torch.ones`, `.expand().contiguous()` —
#     these launch GPU kernels (DtoD memcpy / fill) that CUPTI counts in the
#     benchmark and create phantom gaps. layer_norm `.clone()` once added 4.8μs
#     DtoD on a 7μs kernel → CUPTI reported 1.8x gap when kernel was at parity.
```

matmul 包装器正是这么做的——`launch_with_cfg` 里用 `torch.empty((m, n), ...)` 分配输出（[src/tilegym/ops/cutile_rs/matmul.py:155-173](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile_rs/matmul.py#L155-L173)），没有任何 clone/zeros。

**CUPTI 计时实现**——用 `torch.profiler.profile` 取设备侧 self time 求和（[src/tilegym/backend/cutile_rs/autotuner.py:134-158](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/cutile_rs/autotuner.py#L134-L158)）：

```python
for i in range(rep):
    with profile(activities=[ProfilerActivity.CUDA]) as prof:
        out = kernel_fn(cfg)
        stream.synchronize()
    stream.synchronize()   # 让 profiler 完全 flush，防 DeviceContext 竞争
    total_us = sum(
        evt.self_device_time_total
        for evt in prof.key_averages()
        if evt.self_device_time_total > 0 and (kernel_re is None or kernel_re.search(evt.key))
    )
    if i == 0 and total_us == 0:
        raise RuntimeError("CUPTI returned 0 device time ... Check libcupti ...")
    times_us.append(total_us)
median_ms = sorted(t / 1000.0 for t in times_us)[len(times_us) // 2]
```

注意两个细节：第一次若得到 0 设备时间直接报错（提示检查 libcupti），引导排查环境；每次 rep 之间额外 `sync` 是因为 cutile-rs 的 `DeviceContext` 会与 profiler 竞争。

**tune-once / cache**——模块级字典 `_cache` 以 `(kernel_name, key)` 为键，命中时直接复用最优配置（[src/tilegym/backend/cutile_rs/autotuner.py:197-209](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/cutile_rs/autotuner.py#L197-L209)），与 u5-l3 的 cuTile 调优同构。`clear_cache` / `get_cache_stats`（L266-291）供测试强制重调优或调试。

**返回码 `-1` 哨兵的呼应**。4.2 节提到 `-1` 不是错误码——它是编译选项「auto/默认」的哨兵。`autotune_launch` 的候选配置里 `num_cta_in_cga` / `occupancy` 若想表达「让编译器自己决定」，就传 `<= 0`（如 matmul 配置 `OCCUPANCY=1` 是显式值，但机制上 `compile_options!` 宏只在值 `> 0` 时才设——[ffi_util.rs:204-216](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile_rs/ffi_util.rs#L204-L216)）。这就是为什么 `-1` 不能被返回码占用。

#### 4.4.4 代码实践（本讲指定实践任务）

**实践目标**：用自己的话解释「`autotune_launch` 为什么要求 `kernel_fn` 内只用 `torch.empty`」以及「CUPTI 与 `cuda.Event` 在测量小内核时的差异」，并用源码证据支撑。

**操作步骤**：

1. 打开 `autotuner.py` 的 LAMBDA RULE（L36-42）与 WHY CUPTI 段（L16-26），找到两处实测数据：
   - layer_norm `.clone()` 给 7μs 内核加 4.8μs DtoD → CUPTI 报 1.8× 虚假差距；
   - layer_norm 2D：`cuda.Event` 报 1.5×，CUPTI 报 0.96×。
2. 在 `matmul.py` 的 `launch_with_cfg`（L155-173）确认输出分配用的是 `torch.empty`，没有 clone/zeros。
3. 写一段说明，覆盖以下两点（见下方「参考答案」）。
4. （可选）参照 README 给出的基准命令（[README.md:210-219](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/README.md#L210-L219)）：`CUPTI=1 pytest tests/ops/test_bmm.py -k "test_perf and cutile_rs" --print-record`，体会官方推荐的「CUPTI 测纯 GPU 时间」对比姿势。

**参考答案（这两点即本讲指定实践任务的答案）**：

- **为什么只能用 `torch.empty`**：`autotune_launch` 的计时器是 CUPTI，它会计入**所有**在 `kernel_fn(cfg)` 调用期间启动的 GPU 内核。`.clone()`、`torch.zeros`、`torch.ones`、`.expand().contiguous()` 这些看似只是「分配/初始化」的操作，其实会向 GPU 提交设备到设备拷贝或填充内核。这些额外内核的时间会被算进候选配置的得分，制造出与目标内核真实性能无关的「幽灵差距」——`layer_norm` 一次 `.clone()` 就给 7μs 的内核加了 4.8μs，使 CUPTI 报出 1.8× 的虚假劣势。`torch.empty` 只在主机侧分配显存、不启动任何 GPU 内核，因此是唯一不会污染计时的分配方式。
- **CUPTI 与 `cuda.Event` 的差异**：`cuda.Event` 测的是「主机端在 `record` 与 `end` 之间流逝的墙钟时间」，它**包含** cutile-rs 首次调用的 MLIR→cubin JIT（50–500 ms）、cffi/ctypes 的 FFI 调用、Python 参数编组等所有主机侧与启动开销；对小（亚微秒）内核，这些开销会主导测量，夸大差距（layer_norm 2D 实测 1.5×）。CUPTI 经 `torch.profiler` 直接读取 GPU 硬件计数器，测的是**纯内核执行时间**，剔除了 JIT 与主机开销，是与 cuTile-Python / Triton-TileIR 做公平比较的唯一标尺（同内核实测 0.96×，rs 其实更快）。

**待本地验证**：在 `kernel_fn` 里故意把 `torch.empty` 换成 `out.clone()` 跑一次 bmm 调优，对比 `tuning_record`（用 `get_cache_stats()`）中各配置的 `best_ms` 是否被系统性抬高——这能直接观察到「幽灵差距」。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `_bench_config_cupti` 要把 warmup（默认 2 次）排除在计时之外？

> 参考答案：cutile-rs 内核首次调用会 JIT 编译 MLIR→cubin，耗时几十到几百毫秒，与内核真实性能无关。warmup 先触发并完成编译，使随后的 `rep` 次计时只测稳态执行时间。同时 warmup 还用于把「配置相关错误」（非法内存/OOM）与「环境错误」区分开——前者返回 `inf` 跳过该配置，后者向上抛。

**练习 2**：调优结束后为什么要 `torch.cuda.synchronize()` + `gc.collect()`？

> 参考答案：清空 CUPTI/profiler 的内部状态。调用方很可能在自己代码里再包一层 profiler，嵌套 profiler 会导致 abort；先同步并回收，确保把调优期的 profiler 状态彻底排空，再返回给调用方。`gc.collect()` 还能释放 `kernel_fn` 反复分配的临时张量，避免它们影响后续测量或显存占用。

---

## 5. 综合实践

**任务**：完整追踪一次 `tilegym.set_backend("cutile-rs"); tilegym.ops.matmul(a, b)` 调用，把本讲四个最小模块（crate/cdylib、cffi/TensorDesc、autobuild、CUPTI autotune）串成一条端到端链路，并画出调用图。

**操作步骤**：

1. **注册与门控**：读 `ops/cutile_rs/__init__.py`（L18-23）——只有 `is_backend_available("cutile-rs")` 为真时才 `from . import matmul`，触发 `@register_impl("matmul", backend="cutile-rs")`（[matmul.py:119-120](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile_rs/matmul.py#L119-L120)），把实现挂进 `_REGISTRY["matmul"]["cutile-rs"]`。
2. **分发**：调用 `tilegym.ops.matmul(a, b)` 时，`dispatch` wrapper 查 `_REGISTRY`，按当前后端 `"cutile-rs"` 路由到上面的 `matmul` 函数（复习 u2-l2 的三级查找）。
3. **校验与连续化**：`matmul` 函数检查 CUDA/同卡/dtype/K 匹配，对 `a/b` 调 `.contiguous()`、`.detach()`（[matmul.py:120-145](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile_rs/matmul.py#L120-L145)）。
4. **调优**：按 `persistent` 选候选配置集，调 `autotune_launch(kernel_fn=..., configs=..., key=(m,n,k,dtype,persistent), ...)`（[matmul.py:178-184](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile_rs/matmul.py#L178-L184)）。首次：CUPTI 遍历配置选最优并缓存；之后：命中缓存零开销。
5. **cffi 加载**：`kernel_fn → launch_with_cfg → _run_ffi → bind_kernel_function_cffi("matmul", _FFI_CDEF)`。首次触发 `_ensure_built_and_path`（可能 autobuild）+ `dlopen`，之后命中 `_cffi_kernel_libs` 缓存（[utils.py:268-291](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/cutile_rs/utils.py#L268-L291)）。
6. **跨界**：`make_tensor_desc` 把 `out/a/b` 打包成 `TensorDesc*`，`lib.cutile_matmul(...)` 越过 FFI，返回码交给 `check_rc`（[matmul.py:91-116](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile_rs/matmul.py#L91-L116)）。
7. **Rust 侧**：`cutile_matmul` 用 `deref_descs!` / `resolve_dtype!` / `setup_device_stream!` / `borrow_tensor` 还原借用张量，拼 `.generics(...).grid(...).compile_options(...)`，`op.sync_on(&stream)` 启动内核（[matmul_kernel/ffi.rs:54-116](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile_rs/matmul_kernel/ffi.rs#L54-L116)）。

**预期产出**：一张从 Python 调用到 GPU 内核的调用图，标注每一步属于哪个模块、首次调用与命中缓存时的差异（首次多了 autobuild + JIT + CUPTI 全量调优；之后仅 cffi 缓存命中 + autotune 缓存命中）。

**待本地验证**：在有 cargo + GPU 的环境运行，首次调用应能在日志看到 autobuild（`building libcutile_kernels.so`）与调优（`cutile-rs autotune [matmul] ... benchmarking N configs (CUPTI)`）；第二次同形状调用应既无 autobuild 也无调优日志。

## 6. 本讲小结

- cutile-rs 用 **Rust**（`cutile` crate）写内核，所有算子编译进**一个 cdylib** `libcutile_kernels.so`；crate 根用 `mod + include!` 聚合各算子的 `kernel.rs`/`ffi.rs`，一个 `Cargo.toml` 管所有内核。
- Python 与 Rust 之间用 **cffi ABI 模式**跨界：`cdef` 声明 C 签名，`TensorDesc`（`#[repr(C)]` 与 `_TENSORDESC_CDEF` 必须逐字段同步）搬运张量；Rust 用 `ManuallyDrop` 的 `borrow_tensor` 借用显存、绝不释放。
- `.so` 是**按需 autobuild** 的：源码过期才 `cargo build --release`，文件锁内复检防并发冗余编译；`is_cutile_rs_available` 用宽松探测（`cargo` 在 PATH 或现成 `.so` 即可），重校验推迟到 dispatch。
- 与 cuTile/tilecpp 一样，注册受 `is_backend_available` 门控、作为导入副作用挂到同一算子名——**算子名是全局键、后端是子键、实现语言无关**。
- 自动调优沿用 tune-once/cache/launch 模式，但**用 CUPTI（`torch.profiler`）而非 `cuda.Event`** 计时，以剔除 JIT/FFI/编组开销；由此引出铁律：`kernel_fn` 内只能用 `torch.empty`，禁用 clone/zeros 等会启动 GPU 内核的操作。
- cutile-rs 当前是**仅前向**的后端（matmul 文档串明确 forward-only；测试对齐时无反向），且只覆盖一部分算子（matmul/bmm/silu_and_mul/swiglu/attention_sink）。

## 7. 下一步学习建议

- **回到 LLM 集成**：U8 把内核 monkey-patch 进 HuggingFace 模型。可对比 cutile-rs 的「仅前向」与 cuTile 的 autograd 能力（u4-l2），思考 cutile-rs 当前为何主要服务于推理/基准而非训练。
- **横向对比三个后端**：重读 u7-l1（tilecpp）与本讲，列出 cuTile（Python DSL + tileiras JIT）、tilecpp（C++ + nvcc 离线 + cubin 缓存）、cutile-rs（Rust + cargo autobuild + cffi）在「编译器、加载方式、可用性探测、调优计时」四个维度上的异同，巩固「同一算子名、多实现」的架构观。
- **贡献一个 cutile-rs 算子**：参考 u9-l2（新增算子工作流）与本讲 4.1，尝试把一个已有 cuTile 算子移植成 Rust（一对 `kernel.rs`/`ffi.rs` + 在 `lib.rs` 加 `mod`），用 `tests/ops/test_matmul.py` 的后端参数化（`is_backend_available("cutile-rs")`，L90-91）验证正确性。
- **深入 CUPTI**：阅读 `torch.profiler` 文档与 `_bench_config_cupti` 的 `kernel_filter` 用法，理解在多内核场景下如何只计时目标内核。
