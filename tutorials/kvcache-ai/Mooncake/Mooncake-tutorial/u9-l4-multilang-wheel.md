# 多语言绑定与 Wheel 打包

> 阶段：advanced ｜ 依赖讲义：[u1-l3 从源码构建](u1-l3-build-from-source.md)

## 1. 本讲目标

Mooncake 的核心是用 C++ 写的高性能传输引擎（Transfer Engine）与分布式 KV 存储（Store）。真实用户却来自四面八方：Python（vLLM 集成）、Rust、Go、甚至裸 C。本讲要回答三个问题：

1. Mooncake 如何让 Rust、Go、Python 三种语言共享同一套 C++ 实现？——多语言绑定。
2. 一个 `pip install` 下来的 wheel 里到底塞了什么？它为什么能“开箱即用”？——Wheel 打包。
3. CUDA / CUDA 13 / 非 CUDA / Ascend NPU 这四个变体是怎么从同一份脚本产出的？——多变体构建。

学完后，你应该能够：

- 说清 C ABI 为什么是“多语言绑定的共同底座”，并看懂 `transfer_engine_c.h` / `store_c.h` 这两个头文件的角色。
- 读懂 Rust（bindgen）、Go（CGo）、Python（pybind11）三种绑定各自的写法和生命周期管理。
- 顺着 `scripts/build_wheel.sh` 走完一遍 wheel 的诞生过程：编译产物拷贝 → 变体改名 → auditwheel 修复 → EP/PG 注入 → RPATH 收尾。
- 解释为什么 EP/PG 扩展要在 auditwheel **之后**才注入，以及 `RPATH=$ORIGIN` 为什么能让 wheel 自包含运行。

## 2. 前置知识

- **共享库与动态链接**：Linux 下 `.so` 文件在运行时被 `dlopen`/`ld.so` 加载。一个 `.so` “需要”的其它 `.so`（`NEEDED` 项）由动态链接器按 `RPATH` / `RUNPATH` / `LD_LIBRARY_PATH` / 系统缓存顺序查找。
- **ABI（Application Binary Interface）**：API 是源码层面的约定（函数名、参数类型），ABI 是二进制层面的约定（调用约定、结构体内存布局、名字修饰 mangling）。C++ 的名字修饰在不同编译器间不兼容，但 `extern "C"` 关闭修饰后导出的符号几乎是“通用语言”。
- **FFI（Foreign Function Interface）**：高级语言调用 C 函数的机制。Rust 用 bindgen，Go 用 CGo，Python 用 pybind11 / ctypes。
- **Python wheel 与 platform tag**：wheel 是 zip 包；`manylinux_2_28_x86_64` 这种 tag 表示“要求 glibc ≥ 2.28 的 x86_64 Linux”。auditwheel 的职责是把外部依赖打包进 wheel 并贴上正确的 tag。
- **RPATH / $ORIGIN**：ELF 文件里写死的“去哪里找依赖库”的搜索路径。`$ORIGIN` 是一个特殊变量，在运行时展开为“该 ELF 文件自身所在目录”。这是自包含 wheel 的关键。
- **CUDA fatbin**：CUDA 扩展 `.so` 里嵌着 GPU kernel 的二进制镜像（fat binary）。`patchelf`（auditwheel 内部使用）重写 ELF 段时可能破坏这些镜像，导致运行时 `cudaErrorInvalidKernelImage`。本讲会讲 Mooncake 如何绕开这个坑。

如果你对“从源码编译”的整体流程还不熟，先看 [u1-l3 从源码构建](u1-l3-build-from-source.md)。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [mooncake-transfer-engine/include/transfer_engine_c.h](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/include/transfer_engine_c.h) | Transfer Engine 的 **C ABI 头**，所有非 C++ 绑定的共同入口。 |
| [mooncake-store/include/store_c.h](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/store_c.h) | Store 的 **C ABI 头**，`mooncake_store_*` 系列函数。 |
| [mooncake-transfer-engine/rust/src/transfer_engine.rs](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/rust/src/transfer_engine.rs) | Rust 安全封装，包住 `transfer_engine_c.h`。 |
| [mooncake-transfer-engine/rust/build.rs](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/rust/build.rs) | Rust 构建脚本：链接静态库 + bindgen 生成绑定。 |
| [mooncake-store/rust/src/store.rs](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/rust/src/store.rs) / [build.rs](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/rust/build.rs) | Store 的 Rust 安全封装及其构建脚本。 |
| [mooncake-store/go/mooncakestore/store.go](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/go/mooncakestore/store.go) | Store 的 Go 绑定（CGo 包住 `store_c.h`）。 |
| [mooncake-p2p-store/src/p2pstore/transfer_engine.go](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-p2p-store/src/p2pstore/transfer_engine.go) | P2P Store 的 Go 绑定（CGo 包住 `transfer_engine_c.h`）。 |
| [mooncake-integration/CMakeLists.txt](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/CMakeLists.txt) | 用 pybind11 构建 `engine.so` / `store.so`，并设 `INSTALL_RPATH=$ORIGIN`。 |
| [scripts/build_wheel.sh](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/scripts/build_wheel.sh) | **打包总指挥**：拷贝产物 → 改名 → auditwheel → EP/PG 注入 → RPATH。 |
| [mooncake-wheel/setup.py](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-wheel/setup.py) / [pyproject.toml](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-wheel/pyproject.toml) | wheel 元信息、入口脚本、平台 tag 探测。 |
| [CMakeLists.txt](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/CMakeLists.txt) | 顶层选项：`WITH_EP`、`WITH_STORE_GO`、`WITH_RUST_EXAMPLE`、`WITH_STORE_RUST`，以及 EP/PG 暂存目录。 |

---

## 4. 核心概念与源码讲解

### 4.1 C ABI：多语言绑定的共同底座

#### 4.1.1 概念说明

Mooncake 是 C++ 项目，但 C++ 符号经名字修饰后形如 `_ZN12transferEngine...`，不同编译器/版本之间不可互通。为了让 Rust、Go、Python 都能调用同一份引擎，Mooncake 在 C++ 之上铺了一层**薄的 C 接口**（`extern "C"`），导出未修饰的符号（如 `createTransferEngine`、`mooncake_store_put`）。

这层 C 接口就是 **C ABI**。它的价值在于：几乎所有语言的 FFI 都默认能调 C。于是多语言绑定变成一个标准三件套：

```
        ┌──────────────────────────────────────────┐
        │   C++ 实现 (transfer_engine / store)     │
        └────────────────────┬─────────────────────┘
                             │  extern "C" 导出
        ┌────────────────────▼─────────────────────┐
        │   C ABI 头:  transfer_engine_c.h          │
        │              store_c.h                    │
        └─┬──────────────┬──────────────┬──────────┘
          │ Rust(bindgen) │ Go(CGo)      │ Python(pybind11)
       安全封装          安全封装         engine.so / store.so
```

无论上层是哪种语言，最终都落到同一组 C 函数上，因此**行为永远一致**——这点对 KV cache 这种对正确性极度敏感的系统至关重要。

#### 4.1.2 核心流程

1. C++ 引擎把能力封装成若干 `extern "C"` 函数，参数只用 C 兼容类型（`void*`、`int`、`char*`、纯 POD 结构体）。
2. 头文件用 `#ifdef __cplusplus extern "C"` 包裹，确保 C 与 C++ 都能包含。
3. 高级语言绑定读取头文件，生成或手写对应的 FFI 声明。
4. 调用时：高级语言对象 → 转成 C 类型 → 调 C 函数 → 拿返回码 → 转回高级语言。

Transfer Engine 的 C 头先定义“语言无关”的类型与常量：

[mooncake-transfer-engine/include/transfer_engine_c.h:L16-L40](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/include/transfer_engine_c.h#L16-L40) 定义了 `segment_id_t`、`batch_id_t`、`OPCODE_READ/WRITE`、`transfer_request_t`、`transfer_status_t` 等。注意所有字段都是定宽整数或指针，没有 C++ 对象，任何语言都能 1:1 映射。

Store 的 C 头同样如此，并用不透明句柄 `mooncake_store_t`（其实就是 `void*`）隐藏内部实现：

[mooncake-store/include/store_c.h:L25-L32](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/store_c.h#L25-L32) —— `typedef void *mooncake_store_t;` 和 `mooncake_replicate_config` 结构体。绑定层永远不直接操作 C++ 对象，只拿着这个 `void*` 句柄来回传。

#### 4.1.3 源码精读

Store 头里的函数签名几乎就是一份“CRUD 菜单”，绑定层逐个翻译：

[mooncake-store/include/store_c.h:L48-L126](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/store_c.h#L48-L126) 涵盖 `mooncake_store_create / destroy / setup / put / put_from / batch_put_from / get_into / batch_get_into / is_exist / get_size / remove / register_buffer …`。

一个值得注意的约定写在头文件注释里：“`char *` 参数在函数返回后不再使用，调用方可以立即释放。” 这条规则让 Go/Rust 绑定可以用 `defer C.free(...)` 干净地管理字符串生命周期，而不必担心 C 侧异步持有指针。

#### 4.1.4 代码实践

**目标**：直观感受“一份 C ABI，多种语言”的对称性。

1. 打开 `mooncake-store/include/store_c.h`，找到 `mooncake_store_put` 的签名。
2. 打开 Rust 绑定 `mooncake-store/rust/src/store.rs`，搜索 `mooncake_store_put`；再打开 Go 绑定 `mooncake-store/go/mooncakestore/store.go`，搜索同一符号。
3. 对比三者：C 签名、Rust `unsafe { ffi::mooncake_store_put(...) }`、Go `C.mooncake_store_put(...)`。

**观察现象**：三个语言的参数顺序、类型完全对应同一个 C 函数；差异只在于“如何把本语言的字符串/切片转成 `const char*`/`void*`”。

**预期结果**：你会确认无论用哪种语言，最终调用的是同一个 C 符号。运行结果待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 C ABI 用 `void*` 句柄（`mooncake_store_t`）而不是直接暴露 C++ 类？
**答案**：C 不认识 C++ 类，且不同编译器的 C++ ABI 不兼容；用 `void*` 句柄既跨语言又隐藏内部布局，绑定层只能通过配套的 C 函数操作它，避免了结构体布局被错误假设。

**练习 2**：`transfer_request_t` 里为什么用 `int32_t`、`uint64_t` 这类定宽类型，而不是 `int`、`long`？
**答案**：`int`/`long` 的位宽随平台变化（LP64/LLP64），定宽类型保证不同语言、不同架构下结构体内存布局一致，是跨语言 FFI 的基本要求。

---

### 4.2 Rust 绑定：bindgen + 安全封装

#### 4.2.1 概念说明

Rust 绑定分两块：Transfer Engine 的绑定（`mooncake-transfer-engine/rust`）是演示型 crate，Store 的绑定（`mooncake-store/rust`）是可被其它 Rust 项目当依赖用的 `rlib`。

两者都采用同一套模式：

- **`build.rs`** 在编译期做两件事：① 通过 `cargo:rustc-link-*` 告诉链接器去哪找静态/动态库；② 用 [bindgen](https://docs.rs/bindgen) 读 C 头，自动生成 Rust FFI 绑定到 `$OUT_DIR/bindings.rs`。
- **安全封装层** 把 `unsafe` 的原始 FFI 调用包进 `Result` 返回、管理 `CString` 生命周期、实现 `Drop`。

#### 4.2.2 核心流程

以 Transfer Engine 为例：

```
build.rs:
  read transfer_engine_c.h  ──bindgen──▶  $OUT_DIR/bindings.rs
  emit cargo:rustc-link-lib=static=transfer_engine (+ base, asio, ...)
       │
       ▼
transfer_engine.rs:
  mod bindings { include!(concat!(env!("OUT_DIR"), "/bindings.rs")); }
  pub struct TransferEngine { engine: bindings::transfer_engine_t }
  impl TransferEngine { ... unsafe { bindings::createTransferEngine(...) } ... }
```

bindgen 生成的代码全是 `unsafe` 原始指针；封装层负责在 Rust 侧重建类型安全。

#### 4.2.3 源码精读

**① 注入生成的绑定**。[mooncake-transfer-engine/rust/src/transfer_engine.rs:L17-L27](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/rust/src/transfer_engine.rs#L17-L27) 用 `include!` 宏把 build.rs 生成的 `bindings.rs` 嵌进 `mod bindings`，并用一堆 `#![allow(...)]` 关掉 bindgen 代码不可避免的 lint。

**② 构造与句柄持有**。[mooncake-transfer-engine/rust/src/transfer_engine.rs:L63-L92](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/rust/src/transfer_engine.rs#L63-L92)：`TransferEngine` 结构体只持有一个不透明句柄 `bindings::transfer_engine_t`；`new()` 把 `&str` 转成 `CString`，调 `createTransferEngine`，返回 `Result<Self>`（空指针→`bail!`）。这就是“安全封装”的典型形态。

**③ 提交传输**。[mooncake-transfer-engine/rust/src/transfer_engine.rs:L205-L231](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/rust/src/transfer_engine.rs#L205-L231)：把 Rust 的 `Vec<TransferRequest>` 逐个映射成 C 的 `transfer_request_t` 数组，再一次性传给 `submitTransfer`。注意 `opcode as i32` 这种枚举→C 整数的转换。

**④ 资源释放**。[mooncake-transfer-engine/rust/src/transfer_engine.rs:L299-L306](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/rust/src/transfer_engine.rs#L299-L306) 为 `TransferEngine` 实现 `Drop`，确保离开作用域时调 `destroyTransferEngine`，并且 `unsafe impl Send/Sync` 声明句柄可跨线程共享（底层 C++ 对象内部加锁）。

**⑤ build.rs 的链接指令**。[mooncake-transfer-engine/rust/build.rs:L19-L33](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/rust/build.rs#L19-L33) 指定链接静态库 `transfer_engine`、`base`，以及动态库 `asio`。第 [99-102 行](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/rust/build.rs#L99-L102) 用 bindgen 读 `../include/transfer_engine_c.h` 生成绑定。

Store 的 Rust 绑定结构完全对称：[mooncake-store/rust/src/store.rs:L22-L29](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/rust/src/store.rs#L22-L29) 的 `mod ffi` 同样 `include!` 生成的绑定；[store.rs:L142-L174](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/rust/src/store.rs#L142-L174) 的 `setup()` 把 7 个 `&str` 转成 `CString` 后调 `mooncake_store_setup`。它的 build.rs 更复杂——[mooncake-store/rust/build.rs:L166-L179](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/rust/build.rs#L166-L179) 通过 `MOONCAKE_STORE_LIB_DIR`（由 CMake 注入）定位 `libmooncake_store.a`，并用 `has_library()` 探测 CUDA/etcd/uring 等可选依赖，按需链接。

#### 4.2.4 代码实践

**目标**：通过 CMake 触发 Rust 绑定构建，验证 build.rs 与 CMake 的衔接。

参考 [mooncake-store/rust/README.md](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/rust/README.md)：

```bash
cmake -S . -B build -G Ninja -DWITH_STORE=ON -DWITH_STORE_RUST=ON
cmake --build build --target build_mooncake_store_rust
cmake --build build --target build_mooncake_store_rust_example
```

操作步骤：

1. 顶层 [CMakeLists.txt:L87-L92](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/CMakeLists.txt#L87-L92) 在 `WITH_STORE_RUST=ON` 时 `add_subdirectory(mooncake-store/rust)`。
2. [mooncake-store/rust/CMakeLists.txt](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/rust/CMakeLists.txt) 定义自定义 target，用 `cmake -E env` 把 `MOONCAKE_STORE_LIB_DIR`、`MOONCAKE_STORE_INCLUDE_DIR` 等传给 `cargo build`。
3. 观察 cargo 输出里 bindgen 是否成功生成 `bindings.rs`。

**观察现象**：CMake 先编译出 `libmooncake_store.a`（依赖项 `DEPENDS mooncake_store`），再触发 cargo；cargo 的 build.rs 读到 CMake 注入的环境变量后完成链接。

**预期结果**：`build/mooncake-store/rust/release/` 下出现 `libmooncake_store.rlib` 与 `basic_usage` 示例二进制。运行结果待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `TransferEngine` 要 `unsafe impl Send + Sync`？
**答案**：Rust 默认认为含裸指针的类型不能跨线程移动/共享。但底层 C++ 引擎内部已加锁、句柄可安全共享，因此手动声明 `Send/Sync` 来允许跨线程使用，把“线程安全保证”从编译器转移到开发者。

**练习 2**：Store 的 `ReplicateConfig::to_ffi` 为什么要同时返回 `CString` 的 `Vec` 和指针 `Vec`，并要求它们存活到 C 调用结束？
**答案**：C 端只持有 `*const c_char` 指针，不拥有字符串内存。必须由 Rust 侧的 `CString` 持有实际字节；返回它们是为了延长生命周期，防止 C 函数读到已释放的内存（典型 use-after-free 隐患）。

---

### 4.3 Go 绑定：CGo 包装 C ABI

#### 4.3.1 概念说明

Go 通过 **CGo** 调 C。写法是在 import `"C"` 之前用注释段 `//#include "xxx.h"` 嵌入 C 头，之后就能用 `C.函数名` 调用。Mooncake 的 Go 绑定有两处：

- `mooncake-store/go/mooncakestore`：Store 客户端绑定（`WITH_STORE_GO`）。
- `mooncake-p2p-store/src/p2pstore`：P2P Store 直接包 Transfer Engine 的 C ABI。

#### 4.3.2 核心流程

```
//#include "store_c.h"
import "C"
   │
   ▼
type Store struct { handle C.mooncake_store_t }
   │
   ▼  Put(key, value, config):
C.CString(key) ──▶ unsafe.Pointer(&value[0]) ──▶ C.mooncake_store_put(...)
defer C.free(...)                                  返回码 != 0 → 返回 error
```

CGo 的边界跨越有开销（指针要在 Go 堆外分配、GC 不跟踪 C 内存），所以绑定层都用 `C.CString` + `defer C.free` 显式管理。

#### 4.3.3 源码精读

**① CGo 头嵌入**。[mooncake-store/go/mooncakestore/store.go:L20-L27](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/go/mooncakestore/store.go#L20-L27) `#include "store_c.h"` 紧挨 `import "C"`——这是 CGo 识别 C 声明的硬性位置要求。`Store` 结构体只持有 `C.mooncake_store_t` 句柄。

**② 构造**。[store.go:L36-L42](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/go/mooncakestore/store.go#L36-L42) `New()` 调 `C.mooncake_store_create()`，空指针→返回 `ErrStoreNil`。

**③ Setup 的字符串转换**。[store.go:L54-L80](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/go/mooncakestore/store.go#L54-L80)：每个 Go `string` 用 `C.CString` 拷成 C 字符串，紧跟 `defer C.free(unsafe.Pointer(...))`。注意 `uint64` 直接转 `C.uint64_t`。返回码非 0→`ErrSetupFailed`。

**④ 配置结构体映射**。[mooncake-store/go/mooncakestore/config.go:L18-L23](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/go/mooncakestore/config.go#L18-L23) 的 `ReplicateConfig` 是 [store.go:L126-L157](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/go/mooncakestore/store.go#L126-L157) `toCConfig` 的输入，逐字段填到 `C.mooncake_replicate_config_t`，`bool`→`0/1`，切片→`&cSegs[0]` + count。

P2P 绑定包的是 Transfer Engine 头：[mooncake-p2p-store/src/p2pstore/transfer_engine.go:L26-L37](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-p2p-store/src/p2pstore/transfer_engine.go#L26-L37) 用相对路径 `#include "../../../mooncake-transfer-engine/include/transfer_engine_c.h"` 引入同一个 C ABI，`TransferEngine` 持有 `C.transfer_engine_t` 句柄——与 Rust 侧 4.2 的结构完全平行。

#### 4.3.4 代码实践

**目标**：跑通 Store 的 Go 示例，体会 CGo 调用链。

```bash
# 先确保 C++ Store 与 C ABI 已编译（libmooncake_store.so 可被链接）
cd mooncake-store/go/examples/basic
# 设置 CGO_* 与库搜索路径后（待本地验证环境变量）
go run main.go
```

**观察现象**：Go 程序经由 CGo 调到 `mooncake_store_setup` → C++ 实现 → 与 master/metadata 通信。

**预期结果**：示例输出 put/get 成功。由于示例依赖正在运行的 master 与 metadata 服务，具体环境变量（`LD_LIBRARY_PATH`、`CGO_LDFLAGS`）待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 Go 绑定里几乎每个函数都有 `defer C.free(...)`，而 C 头注释说“返回后即可释放”？
**答案**：因为 C 函数在返回瞬间就拷贝完所需数据，调用方提供的 `char*` 此后即可释放；`defer` 正好在 Go 函数返回时触发释放，既满足 C 侧的生命周期约定，又避免内存泄漏。

**练习 2**：`BatchPutFrom` 里为什么要把 `[]string` 展开成 `[]*C.char` 并传 `&cKeys[0]`？
**答案**：C 不认识 Go 切片，需要连续的指针数组；取底层数组首元素地址 `&cKeys[0]` 得到 `**C.char`，配合 `count` 就能让 C 端遍历所有 key。

---

### 4.4 C++/pybind11 绑定：生成 engine.so / store.so

#### 4.4.1 概念说明

Python 这条线不走“用户自己 FFI”，而是 Mooncake 在编译期就用 **pybind11** 把 C++ 类直接暴露成 Python 模块，产出 `engine.cpython-3xx-*.so` 与 `store.*.so`。这两个 `.so` 既是 Python 扩展（`import mooncake.engine`），又是普通 ELF 共享库（带 `RPATH`），是 wheel 里的核心组件。

#### 4.4.2 核心流程

```
mooncake-integration/CMakeLists.txt:
  pybind11_add_module(engine ... transfer_engine/transfer_engine_py.cpp)
      set INSTALL_RPATH "$ORIGIN"
  pybind11_add_module(store  ...)
      set INSTALL_RPATH "$ORIGIN"
          │  cmake --build
          ▼
  build/mooncake-integration/engine.cpython-3xx-x86_64-linux-gnu.so
  build/mooncake-integration/store.cpython-3xx-*.so
```

`$ORIGIN` 在这里被**编译期**写进 `.so` 的 `RPATH`，意思是“运行时去我这个 .so 自己所在目录找依赖”。这决定了 wheel 的整体自包含策略。

#### 4.4.3 源码精读

[mooncake-integration/CMakeLists.txt:L39-L49](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/CMakeLists.txt#L39-L49) 是关键：

- `set(CMAKE_INSTALL_RPATH_USE_LINK_PATH TRUE)` + `set(CMAKE_BUILD_WITH_INSTALL_RPATH TRUE)`：让构建产物直接带上 install RPATH，而不是等 `make install`。
- `pybind11_add_module(engine ...)` 用 pybind11 构建 Python 扩展 `engine`。
- `set_target_properties(engine PROPERTIES INSTALL_RPATH "$ORIGIN")`：把 `engine.so` 的依赖搜索路径锁定为自身目录。

Store 模块同理：[mooncake-integration/CMakeLists.txt:L105-L108](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/CMakeLists.txt#L105-L108) `pybind11_add_module(store ...)` 也设 `INSTALL_RPATH "$ORIGIN"`。

这两个 `.so` 加上一堆 `lib*.so`（transfer_engine、asio、mooncake_store 等）会被 `build_wheel.sh` 全部拷进 wheel 的 `mooncake/` 目录——因为 `$ORIGIN` 指向那里，它们能互相找到。

#### 4.4.4 代码实践

**目标**：验证 `engine.so` 的 RPATH 确实是 `$ORIGIN`。

1. 按依赖讲义构建项目（`USE_CUDA`/`WITH_EP` 视情况）。
2. 对产物执行（待本地验证）：

```bash
readelf -d build/mooncake-integration/engine.*.so | grep -E 'RUNPATH|RPATH|NEEDED'
```

**观察现象**：输出里应有 `RUNPATH`/`RPATH` 为 `$ORIGIN`；`NEEDED` 列出 `libtransfer_engine.so`、`libasio.so` 等兄弟库。

**预期结果**：只要把这些 `NEEDED` 库放进与 `engine.so` 同一目录，运行时无需设 `LD_LIBRARY_PATH` 即可加载。

#### 4.4.5 小练习与答案

**练习**：为什么用 `CMAKE_BUILD_WITH_INSTALL_RPATH=ON`？
**答案**：默认 CMake 只在 `install` 阶段写入 RPATH，构建阶段的 `.so` 用临时 RPATH。Mooncake 直接把构建产物拷进 wheel（不经 `make install`），所以必须让构建产物本身就带最终 RPATH，否则 `$ORIGIN` 不会生效。

---

### 4.5 Wheel 打包流程：build_wheel.sh 总览

#### 4.5.1 概念说明

`scripts/build_wheel.sh` 是把一整堆编译产物“装进一个 wheel”的总指挥。它假设 CMake 构建已经完成（`build/` 目录就绪），然后做四件事：**拷贝产物 → 按变体改名 → auditwheel 修复 → 注入 EP/PG + RPATH 收尾**。整个脚本只操作 `mooncake-wheel/` 这个“打包工作区”，不碰源码。

#### 4.5.2 核心流程

```
┌─ 1. 变量与清理 (L9-L25) ───────────── PYTHON_VERSION / OUTPUT_DIR / BUILD_DIR
│
├─ 2. 拷贝编译产物到 mooncake-wheel/mooncake/ (L29-L118)
│      engine.so, libasio.so, store.so, libtransfer_engine.so,
│      nvlink_allocator.so(CUDA) / ubshmem_fabric_allocator.so(NPU),
│      mooncake_master, mooncake_client, transfer_engine_bench, *.py ...
│
├─ 3. EP/PG 暂存定位 + CI 释放磁盘 (L120-L144)
│
├─ 4. 变体改名 pyproject.toml (L157-L199)  cuda / cuda13 / non-cuda / npu
│
├─ 5. 构 wheel + auditwheel repair (L294-L391)
│
├─ 6. 注入 EP/PG .so（auditwheel 之后） (L393-L415)
│
└─ 7. NPU 收尾 / 替换原 wheel / 还原 pyproject.toml (L422-L463)
```

#### 4.5.3 源码精读

**① 入口变量**。[scripts/build_wheel.sh:L9-L20](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/scripts/build_wheel.sh#L9-L20)：`PYTHON_VERSION`、`OUTPUT_DIR`、`BUILD_DIR` 都支持“环境变量 > 位置参数 > 默认值”三级回退；并把 `${BUILD_DIR_ABS}/mooncake-common` 加进 `LD_LIBRARY_PATH`，确保拷贝/检测阶段能加载到刚编出的库。

**② 清理工作区**。[L22-L25](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/scripts/build_wheel.sh#L22-L25) 删掉旧 `.so`、`build/`，避免上次构建残留污染本次 wheel。

**③ 产物拷贝（条件式）**。脚本的精髓在于“按编译开关条件拷贝”：

- [L32-L36](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/scripts/build_wheel.sh#L32-L36)：`engine.so`、`libasio.so` 必拷（Transfer Engine 运行时依赖）。
- [L39-L51](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/scripts/build_wheel.sh#L39-L51)：用 `compgen -G` 判断 `store.*.so` 是否存在，存在才拷 store 相关（`WITH_STORE=ON`）及 master/client 二进制和 `async_store.py`。
- [L65-L81](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/scripts/build_wheel.sh#L65-L81)：`libetcd_wrapper.so`（`USE_ETCD`）、`libtransfer_engine.so`（`BUILD_SHARED_LIBS`）、`ascend_transport.so`（`USE_ASCEND_DIRECT`）按需拷贝。
- [L83-L107](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/scripts/build_wheel.sh#L83-L107)：CUDA 分支拷 `nvlink_allocator.so` + `allocator.py`；NPU 分支拷 `ubshmem_fabric_allocator.so` + `allocator_ascend_npu.py`。两者互斥，正好对应不同硬件变体。

**④ 二进制与脚本入口**。[L109-L118](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/scripts/build_wheel.sh#L109-L118) 拷 `transfer_engine_bench`、可选的 `libascend_transport_mem.so`。这些可执行文件会在 [pyproject.toml:L28-L34](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-wheel/pyproject.toml#L28-L34) 注册成 `mooncake_master`、`transfer_engine_bench` 等命令行入口。

**⑤ setup.py 的平台探测**。在 `python -m build` 之前，[mooncake-wheel/setup.py:L152-L162](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-wheel/setup.py#L152-L162) 的 `BinaryDistribution`/`CustomBdistWheel` 把 wheel 标记为“非纯 Python”（`root_is_pure=False`），并用 [setup.py:L26-L103](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-wheel/setup.py#L26-L103) 的 `_detect_manylinux_tag()` 探测 glibc 版本，生成 `manylinux_X_Y_arch` 平台 tag——这与 4.5.3⑥ 里 `build_wheel.sh` 自己的 glibc 探测是双重保险。

#### 4.5.4 代码实践

**目标**：通读 `build_wheel.sh`，画出“产物 → wheel 内部路径”的映射表。

1. 读完脚本 L29-L118，列出每一类产物及其触发条件。
2. 对照 [pyproject.toml:L44-L45](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-wheel/pyproject.toml#L44-L45) 的 `package-data`，确认哪些文件类型会被打进 wheel。

**观察现象**：`package-data` 里声明了 `*.so`、`mooncake_master`、`mooncake_client`、`transfer_engine_bench`——只有这几类非 `.py` 文件会被 setuptools 收录；脚本拷贝的所有 `.so`/二进制都落在这个白名单内。

**预期结果**：你能预判“给定一组 CMake 开关，wheel 里会出现哪些 `.so`”。运行结果待本地验证。

#### 4.5.5 小练习与答案

**练习 1**：为什么拷贝用 `[ -f ... ]` / `compgen -G` 判断，而不是直接 `cp`？
**答案**：不同变体（CUDA/NPU/纯 TCP、是否带 Store/etcd）编译出的产物集合不同；条件判断让同一份脚本能适配所有变体，缺失的可选产物只打 log 跳过，而不是 `cp` 报错让整个构建失败。

**练习 2**：`LD_LIBRARY_PATH` 在脚本开头加入 `${BUILD_DIR_ABS}/mooncake-common` 是为了什么？
**答案**：让后续步骤（如 auditwheel 解析依赖、`python -m build` 加载扩展）能在系统路径之外找到刚编出的 `libasio.so` 等共享库。

---

### 4.6 多变体构建：cuda / cuda13 / non-cuda / npu

#### 4.6.1 概念说明

Mooncake 要同时支持“CUDA 12”、“CUDA 13”、“无 GPU（纯 CPU/TCP/RDMA）”和“华为 Ascend NPU”四类环境。如果各发一个不同名字的 wheel，用户就能用 `pip install mooncake-transfer-engine`（CUDA 默认）或 `mooncake-transfer-engine-cuda13` / `-non-cuda` / `-npu` 选到匹配的版本。

`build_wheel.sh` 用三个互斥环境变量 `NON_CUDA_BUILD` / `CU13_BUILD` / `NPU_BUILD` 选择变体，默认（都不设）即标准 CUDA wheel。变体差异体现在两处：**包名**（改 `pyproject.toml`）和**平台/依赖处理**。

#### 4.6.2 核心流程

```
互斥校验: NON_CUDA_BUILD / CU13_BUILD / NPU_BUILD 只能有一个=1
   │
   ├── 默认        → name = "mooncake-transfer-engine"
   ├── NON_CUDA_BUILD=1 → sed 改名 "...-non-cuda"
   ├── CU13_BUILD=1     → sed 改名 "...-cuda13"
   └── NPU_BUILD=1      → sed 改名 "...-npu"  + 额外 strip / RPATH 处理
```

#### 4.6.3 源码精读

**① 互斥校验**。[scripts/build_wheel.sh:L157-L167](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/scripts/build_wheel.sh#L157-L167) 统计三个变体标志，多于一个就报错退出——避免误把两个变体混进同一个 wheel。

**② 包名改名**。[L170-L199](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/scripts/build_wheel.sh#L170-L199)：每个变体先 `cp pyproject.toml pyproject.toml.backup` 备份，再用 `sed -i` 改 `name`、`description`、`keywords` 三处。例如 NPU 分支 [L188-L196](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/scripts/build_wheel.sh#L188-L196) 把名字改成 `mooncake-transfer-engine-npu`，关键词加上 `ascend`、`npu`。脚本结尾 [L463](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/scripts/build_wheel.sh#L463) 用备份恢复 `pyproject.toml`，保证工作区干净。

**③ NPU 的特殊预处理的预**。[L146-L151](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/scripts/build_wheel.sh#L146-L151)：NPU 变体在进 `mooncake-wheel` 前先用 `strip --strip-unneeded` 给所有 `.so` 瘦身（NPU 工具链库通常很大），并**预先**用 patchelf 把 RPATH 设成 `$ORIGIN`，让 auditwheel 能从 wheel 内部副本解析 `NEEDED`。

**④ 构建后端选择**。[L206-L234](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/scripts/build_wheel.sh#L206-L234) 与 [L294-L301](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/scripts/build_wheel.sh#L294-L301)：NPU 走 `python -m build --no-isolation` + `python -m auditwheel`（避免隔离环境装错 PyTorch/CANN），其它变体用 `auditwheel` 直接命令。

**⑤ glibc 与平台 tag**。[L240-L292](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/scripts/build_wheel.sh#L240-L292)：`detect_glibc_version()` 优先用 `getconf GNU_LIBC_VERSION`，回退 `ldd --version`，再回退保守的 `2_17`；结合 `uname -m` 得到 `manylinux_${GLIBC}_${ARCH}`。这个 tag 决定 wheel 能装在哪些 Linux 发行版上。

#### 4.6.4 代码实践

**目标**：体验“同名脚本、不同产物”。

在已配置好对应工具链的环境里分别执行（每个命令各自隔离运行，待本地验证）：

```bash
# 标准 CUDA wheel（默认）
./scripts/build_wheel.sh 3.10 dist

# CUDA 13 变体
CU13_BUILD=1 ./scripts/build_wheel.sh 3.10 dist-cu13

# 非 CUDA 变体
NON_CUDA_BUILD=1 ./scripts/build_wheel.sh 3.10 dist-nocuda

# Ascend NPU 变体
NPU_BUILD=1 ./scripts/build_wheel.sh 3.10 dist-npu
```

**观察现象**：四个产物文件名分别是 `mooncake_transfer_engine-*.whl`、`..._cuda13-*.whl`、`..._non_cuda-*.whl`、`..._npu-*.whl`；platform tag 反映宿主 glibc 与架构。

**预期结果**：NPU 产物体积相对小（经过 strip）；CUDA 产物里能找到 `nvlink_allocator.so`，NPU 产物里能找到 `ubshmem_fabric_allocator.so`，二者不会同时出现。

#### 4.6.5 小练习与答案

**练习 1**：为什么要在改 `pyproject.toml` 前备份、构建后恢复？
**答案**：`pyproject.toml` 是受版本管理的源文件；若不恢复，工作区会留下脏改动，且下一次默认构建会错误地使用上次的变体名。

**练习 2**：CUDA 与 CUDA 13 两个变体，源码层面的 CMake 开关主要差在哪？
**答案**：差别在 CUDA 主版本（`CUDAToolkit_VERSION_MAJOR`）及对应的 PyTorch/Torch CUDA arch；顶层 [CMakeLists.txt:L104-L107](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/CMakeLists.txt#L104-L107) 的 `find_package(CUDAToolkit)` 与 EP 扩展构建会按 CUDA 版本走不同 toolchain，wheel 名字据此区分以避免装错 GPU 运行时。

---

### 4.7 auditwheel 修复、EP/PG 注入与 RPATH=$ORIGIN

#### 4.7.1 概念说明

这一节是本讲的技术难点，也是 wheel“自包含”的真正奥秘。三个机制叠加：

1. **auditwheel repair**：扫描 wheel 里每个 `.so` 的 `NEEDED`，把那些“非系统库”拷进 wheel 的 `.libs` 目录，并改写 `RPATH` 指过去。但有一长串 `--exclude` 表示“这些库不打包，留给运行环境提供”（如 `libcuda.so`、`libtorch.so`、Ascend CANN 库等）。
2. **EP/PG 延迟注入**：Mooncake EP/PG 是含 CUDA kernel（fatbin）的 PyTorch 扩展。如果让 auditwheel 处理它们，auditwheel 内部的 patchelf 会**破坏 fatbin**，运行时报 `cudaErrorInvalidKernelImage`。所以它们被排在 auditwheel 之外，**之后**再 `wheel unpack` → `cp` → `wheel pack` 注入。
3. **RPATH=$ORIGIN**：所有 ELF 文件的依赖搜索路径都指向“自己所在目录”。于是只要把全部 `.so` 平铺在 `mooncake/` 下，它们就能互相找到，无需任何外部 `LD_LIBRARY_PATH`。

#### 4.7.2 核心流程

```
auditwheel repair *.whl  --exclude libcuda.so* libtorch.so* libascendcl.so* ...
        │  patchelf 重写非 EP .so 的 RPATH，vendored 依赖进 *.libs/
        ▼
repaired_wheels_3.10/*.whl
        │
        │  若 ep_pg_staging/*.so 存在（WITH_EP=ON 编出的 EP/PG 扩展）:
        ▼
wheel unpack → cp ep_pg/*.so 到 mooncake/ → wheel pack
        │  （EP/PG 的 RPATH=$ORIGIN 原封不动，patchelf 没碰过）
        ▼
NPU 变体额外: 把 *.libs/ 搬进 mooncake/，对所有 ELF 重设 RPATH=$ORIGIN 并校验
        ▼
mv 到 OUTPUT_DIR
```

为什么 `RPATH=$ORIGIN` 能让 wheel 自包含？设 wheel 安装后 `engine.so` 位于

```
<site-packages>/mooncake/engine.so
```

`$ORIGIN` 在运行时展开为 `<site-packages>/mooncake/`。`engine.so` 的 `NEEDED` 里有 `libtransfer_engine.so`，动态链接器就在 `<site-packages>/mooncake/` 里找到它；后者又有 `$ORIGIN`，继续在同一目录找 `libasio.so`……整条依赖链都在这个目录内闭环，无需系统级安装、无需 `LD_LIBRARY_PATH`。这就是“自包含”。

#### 4.7.3 源码精读

**① EP/PG 暂存目录**。[scripts/build_wheel.sh:L120-L126](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/scripts/build_wheel.sh#L120-L126) 把 `CUDA_EP_STAGING_DIR` 指向 `${BUILD_DIR_ABS}/ep_pg_staging`，并强调用绝对路径（脚本随后会 `cd mooncake-wheel/`，相对路径会失效）。这个目录由顶层 CMake 在 `WITH_EP=ON` 时创建：[CMakeLists.txt:L128-L149](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/CMakeLists.txt#L128-L149)，`mooncake_ep_ext` 自定义 target 把 EP/PG `.so` 产物放进 `EP_PG_STAGING_DIR`（注释明确写“在 auditwheel 之后注入，patchelf 不碰 fatbin”）。

**② CI 释放磁盘时保护 EP/PG**。[L128-L144](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/scripts/build_wheel.sh#L128-L144)：CI 模式会 `rm -rf build/` 省磁盘，但 EP/PG `.so` 就在里面，所以先 `mktemp -d` 拷出，删完再把 staging 指针指向临时副本。

**③ auditwheel repair 的排除清单**。[L303-L391](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/scripts/build_wheel.sh#L303-L391) 这一大段 `--exclude` 列出了几十个“留给运行环境”的库：网络栈（`libibverbs`、`libfabric`、`libefa`）、加密/HTTP（`libssl`、`libcurl`、`libgnutls`）、CUDA 运行时（`libcuda.so*`、`libcudart.so*`）、PyTorch（`libtorch*.so*`、`libc10*.so*`）、AMD ROCm（`libamdhip64.so*`）、以及一大串 Ascend CANN 库（`libascendcl.so*`、`libhccl.so*` …）。这些库由驱动 / PyTorch / CANN 提供，wheel 不该重复携带。

**④ EP/PG 延迟注入**。[L393-L415](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/scripts/build_wheel.sh#L393-L415) 是核心：

- 注释点明动机：patchelf 会破坏 CUDA fatbin → `cudaErrorInvalidKernelImage`。
- 流程：`wheel unpack` 修复后的 wheel → 把每个 `ep_pg_staging/*.so` `cp` 进 `mooncake/` → 删旧 wheel → `wheel pack` 重新打包。
- 关键：注入的 `.so` 从未被 patchelf 触碰，`RPATH=$ORIGIN` 保持原样，fatbin 完整。

**⑤ NPU 的统一 RPATH 收尾**。[L422-L455](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/scripts/build_wheel.sh#L422-L455)：NPU 变体在 auditwheel 之后额外做三步——把 auditwheel vendored 的 `.libs/` 目录搬进 `mooncake/`（让所有库同处一目录）、对所有 ELF 用 `patchelf --force-rpath --set-rpath '$ORIGIN'`、再用一段 `while` 循环 `patchelf --print-rpath` 逐个**校验** RPATH 必须等于 `$ORIGIN`，校验失败则 `exit 1`。这种“设完即验”的写法把 RPATH 错误挡在发布之前。

#### 4.7.4 代码实践（本讲主线任务）

**目标**：用一个 CUDA + EP 构建为背景，完整描述“编译 → auditwheel → 注入 EP/PG”三步，并解释 RPATH=$ORIGIN。

**操作步骤**：

1. 配置并编译（确保 GPU 环境，待本地验证）：

```bash
cmake -S . -B build -G Ninja \
  -DWITH_TE=ON -DWITH_STORE=ON -DUSE_CUDA=ON -DWITH_EP=ON
cmake --build build
```

2. 打包：

```bash
./scripts/build_wheel.sh 3.10 dist
```

3. 逐步对应脚本阶段：
   - **编译阶段**：CMake 经 [CMakeLists.txt:L138-L149](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/CMakeLists.txt#L138-L149) 的 `mooncake_ep_ext` target 调用 `BuildEpExt.cmake`，把 EP/PG 扩展 `.so` 放进 `build/ep_pg_staging/`；同时 `engine.so`/`store.so` 由 4.4 的 pybind11 目标产出（自带 `RPATH=$ORIGIN`）。
   - **拷贝阶段**：`build_wheel.sh` L29-L118 把 `engine.so`、各 `lib*.so`、`nvlink_allocator.so` 等拷进 `mooncake-wheel/mooncake/`；EP/PG `.so` **暂不拷贝**，只记录 staging 路径。
   - **auditwheel 阶段**：L303-L391 修复 wheel，把 `NEEDED` 里非系统、非排除的库 vendored 进 `.libs/`，并 patchelf 改写这些库的 RPATH；CUDA fatbin 此时**不在** wheel 内，安全。
   - **注入阶段**：L393-L415 把 `ep_pg_staging/*.so` 注入修复后的 wheel，fatbin 原封不动。
   - **结果**：`dist/` 下得到 `mooncake_transfer_engine-...-manylinux_..._x86_64.whl`。

4. 验证 RPATH（待本地验证）：

```bash
unzip -o dist/*.whl -d whl_out
for so in whl_out/mooncake/*.so; do
  echo "== $(basename $so) =="
  readelf -d "$so" | grep -E 'RUNPATH|RPATH'
done
```

**观察现象**：`engine.so`、EP/PG 扩展等的 `RUNPATH`/`RPATH` 都是 `$ORIGIN`；`NEEDED` 兄弟库都在同一 `mooncake/` 目录。

**预期结果**：在一个装好 CUDA 驱动 + PyTorch 的环境里 `pip install` 该 wheel，无需设 `LD_LIBRARY_PATH` 即可 `import mooncake.engine` 并运行 EP。

**回答主线问题——为什么 `RPATH=$ORIGIN` 让 wheel 自包含**：因为 `$ORIGIN` 在运行时展开为“ELF 文件自身所在目录”，而 `build_wheel.sh`（配合 4.4 编译期写入的 `INSTALL_RPATH` 与 4.7 的 NPU 收尾）保证所有相互依赖的 `.so` 都平铺在 `mooncake/` 同一目录。于是动态链接器对每条 `NEEDED` 都能在“当前目录”命中，依赖链在 wheel 内部闭环，不需要系统库搜索路径介入。

#### 4.7.5 小练习与答案

**练习 1**：如果把 EP/PG `.so` 放在 auditwheel **之前**一起处理，会发生什么？
**答案**：auditwheel 会用 patchelf 重写它们的 ELF 段，破坏其中嵌的 CUDA fatbin；运行时加载扩展会触发 `cudaErrorInvalidKernelImage`，kernel 无法启动。所以必须延迟到 auditwheel 之后注入。

**练习 2**：`--exclude libcuda.so*` 的意图是什么？如果把它打包进 wheel 会怎样？
**答案**：`libcuda.so` 是 NVIDIA 驱动用户态组件，必须与宿主驱动版本匹配、由系统提供。若打包进 wheel，会与用户已装驱动冲突或因版本不匹配导致加载失败。排除它意味着 wheel 假设“目标机器已装好对应 CUDA 驱动”。

**练习 3**：NPU 变体为什么要“设完 RPATH 再逐个校验”？
**答案**：NPU 依赖的 CANN 库数量多、依赖链深，任一 `.so` 的 RPATH 没设对都会在运行时“找不到符号”且报错位置离根因很远。设完立即 `patchelf --print-rpath` 校验等于把这类问题前移到构建期，失败即 `exit 1`，不让坏 wheel 流出。

---

## 5. 综合实践

**任务：为“纯 TCP、无 GPU”的场景，手工推演一次 wheel 的内容与运行时加载路径。**

1. **选变体**：确定应使用 `NON_CUDA_BUILD=1`。说明理由（无 GPU，避免携带 CUDA 依赖）。
2. **预判产物**：基于 [build_wheel.sh:L29-L118](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/scripts/build_wheel.sh#L29-L118)，列出该变体 wheel 的 `mooncake/` 目录里**会有**哪些 `.so`（如 `engine.so`、`libtransfer_engine.so`、`libasio.so`、可能的 `store.so`），以及**不会有**哪些（`nvlink_allocator.so`、`ubshmem_fabric_allocator.so`、EP/PG 扩展）。
3. **改名验证**：根据 [L170-L178](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/scripts/build_wheel.sh#L170-L178) 写出最终 wheel 文件名应包含 `non_cuda`。
4. **加载路径推演**：假设用户 `pip install` 后 `engine.so` 位于 `<site-packages>/mooncake/engine.so`，画出它在运行时解析 `libtransfer_engine.so` → `libasio.so` 的搜索路径，标出每一步 `$ORIGIN` 的展开值，得出“为什么不需要 `LD_LIBRARY_PATH`”。
5. **(可选) 实跑**：在无 GPU 的 Linux 机器上执行 `NON_CUDA_BUILD=1 ./scripts/build_wheel.sh 3.10 dist`，解包产物用 `readelf -d` 验证第 4 步的推演。待本地验证。

完成该实践后，你应该能向别人讲清：一个 Mooncake wheel 里有什么、为什么是这些文件、它们如何在安装后“零配置”地互相找到。

## 6. 本讲小结

- **C ABI 是多语言绑定的共同底座**：`transfer_engine_c.h` / `store_c.h` 用 `extern "C"` + 定宽类型 + 不透明句柄，让 Rust、Go、Python 共享同一份 C++ 实现，行为一致。
- **Rust 绑定**：`build.rs` 用 bindgen 读头生成 FFI、用 `cargo:rustc-link-*` 链接静态/动态库；安全封装层负责 `CString` 生命周期、`Result` 错误处理与 `Drop` 资源释放。
- **Go 绑定**：CGo 在 `import "C"` 前嵌入头文件，用 `C.CString` + `defer C.free` 管理跨边界内存，结构与 Rust 绑定平行。
- **Python 绑定**：pybind11 在编译期产出 `engine.so`/`store.so`，并在 [mooncake-integration/CMakeLists.txt](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/CMakeLists.txt) 里写死 `INSTALL_RPATH=$ORIGIN`。
- **build_wheel.sh 是打包总指挥**：条件拷贝产物 → 变体改名 → auditwheel 修复 → EP/PG 注入 → RPATH 收尾；条件判断让同一脚本适配所有变体。
- **多变体靠互斥环境变量**：`NON_CUDA_BUILD`/`CU13_BUILD`/`NPU_BUILD` 选择 cuda / cuda13 / non-cuda / npu 四类 wheel，差异体现在包名与硬件相关库。
- **EP/PG 延迟注入 + RPATH=$ORIGIN 是自包含的关键**：auditwheel 会破坏 CUDA fatbin，故 EP/PG 排在其后注入；`$ORIGIN` 让所有 `.so` 在 wheel 内部互相解析，无需外部库路径。

## 7. 下一步学习建议

- **深入 C ABI 设计**：读 [mooncake-transfer-engine/include/transfer_engine_c.h](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/include/transfer_engine_c.h) 全文，对照 C++ 侧 `transfer_engine_c.cpp`（在 `mooncake-transfer-engine/src` 下）看每个 `extern "C"` 函数如何桥接到 C++ 对象。
- **EP/PG 扩展构建**：精读 [mooncake-ep/BuildEpExt.cmake](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-ep/BuildEpExt.cmake) 与 [mooncake-pg/BuildPgExt.cmake](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-pg/BuildPgExt.cmake)，理解多 PyTorch 版本、多 CUDA arch 的矩阵构建，结合 [u8-l1 Mooncake EP](u8-l1-mooncake-ep.md) / [u8-l2 Mooncake PG](u8-l2-mooncake-pg.md)。
- **打包自动化**：看 `.github/workflows/` 下的 CI 如何为每个变体调用 `build_wheel.sh`，把本讲的“手动变体选择”放进发布流水线。
- **绑定实战**：参照 `mooncake-store/rust/examples/basic_usage.rs` 与 `mooncake-store/go/examples/basic/main.go`，分别用 Rust 和 Go 写一个最小的 put/get 程序，体会同一 C ABI 在两种语言里的手感差异。
