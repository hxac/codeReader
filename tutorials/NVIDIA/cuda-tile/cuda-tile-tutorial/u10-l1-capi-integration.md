# C API 集成接口

> 讲义编号：u10-l1　学习阶段：advanced　依赖：u2-l2（CudaTile 方言定义）、u9-l3（cuda-tile-optimize 与优化器管线）

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 CUDA Tile 为什么需要一套 **C API**，以及它和 C++ 库、Python 绑定的层次关系。
- 掌握 `mlirCudaTileRegisterAllDialects` / `mlirCudaTileRegisterAllPasses` 这对注册函数的作用，并能用它们把 `cuda_tile` 方言装进一个 `MlirContext`。
- 读懂 `CudaTileDialect.h` 暴露的「类型 / 属性 / 字节码写入」三组 C 接口，以及 `CudaTileOptimizer.h` 暴露的 `mlirCudaTileOptConfig` 优化配置结构。
- 理解 C API 实现层「`wrap` / `unwrap`」的桥接套路，以及 `getCheckedType` 这类带校验的构造器为什么在失败时返回「空值」而不是抛异常。
- 通过 README 描述的「Option 1 预编译库 / Option 2 源码集成」两种方式，把 CUDA Tile 嵌入到自己的 C/C++ 项目中，并知道该链接哪些库。

## 2. 前置知识

在进入源码之前，先用通俗语言建立几个心智模型。

### 2.1 为什么要 C API

前面几讲我们都在和 CUDA Tile 的 **C++ 库**（`CudaTileDialect`、`CudaTileBytecodeReader` 等）打交道。C++ 库的问题是它的符号（类的名字、成员函数的签名）和 ABI 与具体编译器、C++ 标准版本强绑定——用 GCC 8 编出来的库，Clang 15 的程序不一定能链接或稳定调用。

**C API** 解决的就是这个「跨编译器、跨语言」的互操作问题。它只暴露 `extern "C"` 的函数，参数和返回值都是稳定的 C 类型（指针、整数、`bool`）。这样一来：

- 一个用纯 C 写的宿主程序（比如 CUDA Driver 的最小启动器）可以直接调用。
- 其他语言的绑定（Python、Rust、Go）可以先包一层 C API，再做各自的语言层封装。本项目的 Python 绑定（见 u10-l2）就是建立在 MLIR 的 C API 之上的。
- 只要 C API 的函数签名不变，底层 C++ 实现怎么重构都不影响外部调用方。

CUDA Tile 的 C API 集中在 [`include/cuda_tile-c/`](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile-c/) 目录下，实现则在 [`lib/CAPI/`](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/CAPI/) 下。注意目录名带 `-c` 后缀，这是 MLIR 项目的约定：`cuda_tile` 是 C++ 接口，`cuda_tile-c` 是对应的 C 接口。

### 2.2 MLIR C API 的「不透明句柄 + wrap/unwrap」套路

MLIR 的 C API 不直接暴露 C++ 对象，而是把每个 C++ 对象包成一个 **不透明句柄（opaque handle）**——本质上是一个只装了一个指针的 C 结构体。例如 `MlirContext`、`MlirType`、`MlirAttribute`、`MlirOperation` 都是这样的「盒子」。

C API 函数内部的真实工作方式是：

1. 收到一个 C 句柄（如 `MlirType type`）。
2. 用 `unwrap(type)` 把它拆出底层的 C++ 对象（如 `mlir::Type`）。
3. 调用 C++ 的真正实现。
4. 用 `wrap(result)` 把 C++ 结果重新装回 C 句柄返回。

这两个函数来自 MLIR 的头文件 `mlir/CAPI/IR.h`。你会在 CUDA Tile 几乎每个 C API 实现文件顶部看到 `#include "mlir/CAPI/IR.h"`，就是这个原因。理解了这一层，下面所有的 `.cpp` 都只是「拆盒子 → 干活 → 装盒子」的机械翻译。

### 2.3 与前序讲义的衔接

本讲假设你已经知道：

- `cuda_tile` 方言的命名空间是 `::mlir::cuda_tile`，它通过 `CudaTileDialect` 这个 C++ 类承载（u2-l2）。
- 优化器有一条「字节码进、字节码出」的管线，入口是 `optimizeTileIRModule`（u9-l3）。
- 一个 `cuda_tile` 模块可能是裸的 `cuda_tile.module`，也可能被标准 MLIR 的 `builtin.module` 包了一层，提取时要用 `extractCudaTileModuleOp` 兼容两种情况（u9-l3、u7-l1）。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [`include/cuda_tile-c/Registration.h`](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile-c/Registration.h) | 声明两个最常用的注册函数：注册全部方言、注册全部 Pass。 |
| [`include/cuda_tile-c/Dialect/CudaTileDialect.h`](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile-c/Dialect/CudaTileDialect.h) | 暴露类型（Pointer/Tile/Token/各种 View）、属性（舍入/原子/内存序/优化提示等）、字节码写入、Pass 注册的 C 接口。 |
| [`include/cuda_tile-c/Dialect/CudaTileOptimizer.h`](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile-c/Dialect/CudaTileOptimizer.h) | 定义 `mlirCudaTileOptConfig` 配置结构、优化标志位枚举与 `mlirCudaTileApplyOptimizations` 入口。 |
| [`lib/CAPI/Registration.cpp`](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/CAPI/Registration.cpp) | 两个注册函数的实现，是整个 C API 的「最小可用入口」。 |
| [`lib/CAPI/Dialect/CudaTileDialect.cpp`](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/CAPI/Dialect/CudaTileDialect.cpp) | 上一行头文件的实现，大量 `wrap/unwrap` 翻译。 |
| [`lib/CAPI/Dialect/CudaTileOptimizer.cpp`](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/CAPI/Dialect/CudaTileOptimizer.cpp) | 优化器 C API 实现，把 C 配置翻译成 C++ 的 `TileIROptimizerOptions`。 |
| [`lib/CAPI/CMakeLists.txt`](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/CAPI/CMakeLists.txt) / [`lib/CAPI/Dialect/CMakeLists.txt`](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/CAPI/Dialect/CMakeLists.txt) | 把 C API 编译成三个公共库。 |
| [`test/CAPI/register.c`](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/CAPI/register.c) / [`test/CAPI/CMakeLists.txt`](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/CAPI/CMakeLists.txt) | 最小 C 程序，验证方言确实能被注册并加载。 |
| [`README.md`](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/README.md) | 「Integrating CUDA Tile Into Your Project」章节给出两种集成方式。 |

---

## 4. 核心概念与源码讲解

### 4.1 注册入口：Registration.h / Registration.cpp

#### 4.1.1 概念说明

任何想用 CUDA Tile 的外部程序，第一件事都是「把 `cuda_tile` 方言和它的 Pass 注册到 MLIR 的全局/上下文注册表里」。MLIR 是一个「方言要按需注册」的框架——如果你不注册，`MlirContext` 在解析 IR 时就认不出 `cuda_tile.module`、`tile<4x8xf32>` 这些语法。

`Registration.h` 把这件最常用的事浓缩成两个函数：注册全部方言、注册全部 Pass。它故意做得极小、极稳定，因为这是第三方项目最常 include 的头文件——它的签名一旦变了，所有下游都要改。

#### 4.1.2 核心流程

```
外部 C 程序
   │  mlirCudaTileRegisterAllDialects(registry)
   ▼
Registration.cpp  ── unwrap(registry) ──▶ mlir::DialectRegistry
   │                                          .insert<mlir::cuda_tile::CudaTileDialect>()
   ▼
mlirCudaTileRegisterAllPasses()
   │  ──▶ mlir::cuda_tile::registerCudaTilePasses()
   ▼
全局 PassRegistry 现在认识 cuda_tile 的 FuseFMA / LoopSplit / SynthesizeDebugInfoScopes 等 Pass
```

注意「注册方言」和「注册 Pass」是两件事，分两步：

- **注册方言**解决的是「IR 里出现 `cuda_tile.xxx` 时，MLIR 知道去找哪个 C++ 类来解析它」。
- **注册 Pass**解决的是「`-fuse-fma`、`-loop-split` 这种 Pass 名字能被 PassManager 识别并实例化」。

二者都不可省，但本讲的 `register.c` 只演示了前者（它只做方言加载，不需要跑 Pass）。

#### 4.1.3 源码精读

头文件先声明这两个函数。注意它们都用 `MLIR_CAPI_EXPORTED` 标注（这是 MLIR 的导出宏，跨平台处理 `__attribute__((visibility("default")))` / `__declspec(dllexport)`），并且整个头被 `extern "C"` 包住，保证 C 链接：

- [include/cuda_tile-c/Registration.h:19-24](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile-c/Registration.h#L19-L24) — 声明 `mlirCudaTileRegisterAllDialects`（向给定 registry 注入 cuda_tile 方言）与 `mlirCudaTileRegisterAllPasses`（注册全部 cuda_tile Pass）。

实现极其简短，正好印证「C API 只是 C++ 库的薄壳」。`unwrap(registry)` 把 C 句柄 `MlirDialectRegistry` 拆成 C++ 的 `mlir::DialectRegistry *`，然后调用 MLIR 通用的 `insert<T>()` 模板把 `CudaTileDialect` 装进去：

- [lib/CAPI/Registration.cpp:17-19](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/CAPI/Registration.cpp#L17-L19) — `mlirCudaTileRegisterAllDialects` 的全部实现就是一行 `unwrap(registry)->insert<mlir::cuda_tile::CudaTileDialect>();`。
- [lib/CAPI/Registration.cpp:21-23](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/CAPI/Registration.cpp#L21-L23) — `mlirCudaTileRegisterAllPasses` 转调 C++ 侧的 `mlir::cuda_tile::registerCudaTilePasses()`（这个 C++ 函数由 u9 讲过的 `Passes.h` 经 `GEN_PASS_REGISTRATION` 宏生成）。

实现文件顶部 [lib/CAPI/Registration.cpp:10-15](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/CAPI/Registration.cpp#L10-L15) 只 include 了三样东西：自己的头、`mlir/CAPI/IR.h`（拿到 `unwrap`）、以及 C++ 侧的 `Dialect.h` 和 `Transforms/Passes.h`。这就是「桥接层」的典型 include 清单。

#### 4.1.4 代码实践

**实践目标**：亲手验证「注册方言」这一步确实生效——注册之后，`MlirContext` 能成功加载 `cuda_tile` 方言。

**操作步骤**（这是「源码阅读 + 跟跑」型实践，假定你已按 u1-l2 配置好带 `-DCUDA_TILE_ENABLE_CAPI=ON -DCUDA_TILE_ENABLE_TESTING=ON` 的构建）：

1. 阅读 [`test/CAPI/register.c`](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/CAPI/register.c)，对照上面的流程图理解它每一步。
2. 在构建目录运行 CAPI 测试可执行文件：
   ```bash
   cmake --build build --target test-cuda-tile-capi-register
   ./build/bin/test-cuda-tile-capi-register && echo "EXIT_CODE=$?"
   ```
3. 如果想看「不注册会怎样」，把 `register.c` 里的 `mlirCudaTileRegisterAllDialects(registry);` 那一行注释掉、重新编译运行（这是你本地的实验性改动，**不要提交**），观察 `mlirContextGetOrLoadDialect` 返回的方言是否变成空。

**需要观察的现象**：

- 正常情况下程序静默退出、退出码为 0。
- 注释掉注册后，`mlirDialectIsNull(cudaTile)` 为真，程序会向 stderr 打印 `failed to load cuda_tile dialect!` 并以非零码退出。

**预期结果**：退出码 `EXIT_CODE=0` 表示方言注册 + 加载成功。

> 待本地验证：上面第 2 步的具体可执行文件路径取决于你的构建目录布局（`build/bin` 或 `build/test`）。若不确定，可用 `find build -name test-cuda-tile-capi-register -type f` 定位。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `mlirCudaTileRegisterAllDialects` 需要接收一个 `MlirDialectRegistry` 参数，而 `mlirCudaTileRegisterAllPasses` 不需要任何参数？

**答案**：方言注册是「附着到某个 registry / context」的——你必须告诉它「往哪个 registry 里塞」，所以需要参数；而 Pass 注册是把 Pass 名字填进 MLIR 的**全局** `PassRegistry`（进程级单例），没有「往哪个对象里塞」的概念，因此无参。

**练习 2**：`Registration.cpp` 里的 `unwrap` 来自哪个头文件？如果没有它，C 句柄 `MlirDialectRegistry` 能直接调用 `.insert<...>()` 吗？

**答案**：来自 `mlir/CAPI/IR.h`。不能——`MlirDialectRegistry` 是 C 结构体，没有成员函数；必须先 `unwrap` 成 `mlir::DialectRegistry *` 才能调用 C++ 的 `insert<T>()`。

---

### 4.2 CudaTileDialect C API：类型、属性与字节码写入

#### 4.2.1 概念说明

`Registration.h` 解决「把方言装进 context」，但装进去之后，外部程序还想**构造和检查** CUDA Tile 的具体类型与属性（比如「给我造一个 `tile<4x8xf32>`」「这个属性是不是舍入模式」），以及把内存里的 module **写成字节码**。这些都由 `CudaTileDialect.h` 提供。

可以把这个头文件理解成「CUDA Tile 方言对外暴露的完整能力清单」，它大致分四块：

1. **方言句柄宏**：`MLIR_DECLARE_CAPI_DIALECT_REGISTRATION`，声明获取方言句柄的函数。
2. **类型 C API**：Pointer / Tile / Token / TensorView / PartitionView / StridedView，每类都有 `IsA`（是不是这种类型）、`GetTypeID`、`Get`（构造）、一组 getter。
3. **属性 C API**：舍入模式、比较谓词、内存序、内存作用域、原子模式、整数溢出、有符号性、PaddingValue、OptimizationHints 等——和 u6 讲过的属性一一对应。
4. **字节码写入与 Pass 注册**：把 module 写成字节码缓冲区、单独注册某个 Pass。

#### 4.2.2 核心流程

构造一个 `tile<4x8xf32>` 并查询它的形状，在 C 端的流程是：

```
MlirContext ctx ──┐
MlirType f32 ─────┼─ mlirCudaTileTileTypeGet(ctx, rank=2, shape={4,8}, f32)
                  ▼
              MlirType tile   ── mlirCudaTileTileTypeGetRank(tile)   ──▶ 2
                               ── mlirCudaTileTileTypeGetDimSize(tile,0) ──▶ 4
                               ── mlirCudaTileTileTypeGetElementType(tile) ──▶ f32
```

注意 C 端无法用 `tile.getDimSize(0)` 这种面向对象写法（C 没有方法），所以每个查询都被翻成了一个「第一个参数传对象、后面传参数」的自由函数——这是 C API 一贯的「函数式」风格。

构造带校验的类型（`...GetChecked`）走的是另一条路：它调用底层 `T::getChecked`，如果类型不合法（比如超过 u3-l1 讲过的 `maxTileNumElements` 上限），就打印一条错误并返回**空类型**（一个 `nullptr` 包装），而不是抛 C++ 异常——因为异常跨 C 边界是未定义行为。

#### 4.2.3 源码精读

**方言句柄宏**。这一行声明了「获取 cuda_tile 方言句柄」的标准入口，参数 `CUDATILE` 是大写前缀、`cuda_tile` 是方言字符串名：

- [include/cuda_tile-c/Dialect/CudaTileDialect.h:19](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile-c/Dialect/CudaTileDialect.h#L19) — `MLIR_DECLARE_CAPI_DIALECT_REGISTRATION(CUDATILE, cuda_tile);`，它会展开出一个形如 `mlirGetDialectHandle__cuda_tile()` 的函数声明。
- 对应的实现宏在 [lib/CAPI/Dialect/CudaTileDialect.cpp:30-31](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/CAPI/Dialect/CudaTileDialect.cpp#L30-L31) — `MLIR_DEFINE_CAPI_DIALECT_REGISTRATION(CUDATILE, cuda_tile, cuda_tile::CudaTileDialect)`，把句柄和 C++ 类 `CudaTileDialect` 绑定。

**Tile 类型**。这是最常用的一组，覆盖了「是不是、TypeID、构造、查元素类型、查秩、查某维大小、带校验构造」全套：

- [include/cuda_tile-c/Dialect/CudaTileDialect.h:40-65](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile-c/Dialect/CudaTileDialect.h#L40-L65) — TileType 的 7 个 C 接口声明。注意 `mlirCudaTileTileTypeGet` 用 `intptr_t rank` + `const int64_t *shape` 表示形状，这是 C 端传数组的通用做法（长度指针配对）。
- 实现 [lib/CAPI/Dialect/CudaTileDialect.cpp:74-78](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/CAPI/Dialect/CudaTileDialect.cpp#L74-L78) — 把 C 数组包成 `ArrayRef<int64_t>`，转调 `TileType::get(...)`，再 `wrap` 回去。这正是「拆盒子→干活→装盒子」。

**带校验的构造器**。`getCheckedType` 是个私有模板，所有 `...GetChecked` 函数都复用它。失败时返回「空类型」：

- [lib/CAPI/Dialect/CudaTileDialect.cpp:36-40](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/CAPI/Dialect/CudaTileDialect.cpp#L36-L40) — 模板定义：用 `emitError(UnknownLoc)` 作为错误发射器调用 `T::getChecked`，失败时 `getChecked` 返回空对象，自然就被 `wrap` 成空句柄。这就是头文件里 `GetChecked` 注释所说「Returns a null type if verification fails」的实现来源。

**字节码写入到缓冲区**。外部程序经常需要在内存里拿到 `.tilebc` 的字节序列（比如直接交给 CUDA Driver 做 JIT，参见 u1-l3）。`mlirCudaTileWriteBytecodeToBuffer` 就是干这个的，它返回一个 `MlirStringRef`（带指针+长度），调用方用完后**必须**用 `mlirCudaTileFreeBuffer` 释放：

- [include/cuda_tile-c/Dialect/CudaTileDialect.h:420-435](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile-c/Dialect/CudaTileDialect.h#L420-L435) — 声明 `mlirCudaTileWriteBytecode`（写文件描述符）、`mlirCudaTileWriteBytecodeToBuffer`（写内存）、`mlirCudaTileFreeBuffer`（释放）。这一组的头注释标着「Future CAPI Extensions」。
- 实现 [lib/CAPI/Dialect/CudaTileDialect.cpp:662-691](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/CAPI/Dialect/CudaTileDialect.cpp#L662-L691) — 先用 `extractCudaTileModuleOp` 兼容裸/被包两种 module（u9-l3 讲过的同一函数），再用 u7-l2 讲过的 `writeBytecode` 以当前版本 `BytecodeVersion::kCurrentVersion` 写入 `std::string`，最后 `malloc` 一块缓冲区拷出来返回。失败时返回空字符串。

> 说明：头文件里 `mlirCudaTileWriteBytecode`（写文件描述符那一版）虽有声明，但当前实现里真正可用的是「写内存」的 `mlirCudaTileWriteBytecodeToBuffer`，二者配套的释放函数都是 `mlirCudaTileFreeBuffer`。

**单独注册某个 Pass**。除了 4.1 讲的「一把注册全部」，头文件还允许逐个注册，方便只想要某一个 Pass 的下游精简依赖：

- [include/cuda_tile-c/Dialect/CudaTileDialect.h:451-463](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile-c/Dialect/CudaTileDialect.h#L451-L463) — 声明 `mlirCudaTileRegisterPasses`（全部）和 `mlirCudaTileRegisterSynthesizeDebugInfoScopesPass` / `mlirCudaTileRegisterFuseFMAPass` / `mlirCudaTileRegisterLoopSplitPass`（逐个），以及两个标准 MLIR Pass 的注册。
- 实现 [lib/CAPI/Dialect/CudaTileDialect.cpp:716-726](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/CAPI/Dialect/CudaTileDialect.cpp#L716-L726) — `mlirCudaTileRegisterPasses` 把 cuda_tile 的三个 Pass 加上标准 `canonicalize`/`cse`/`strip-debuginfo` 一起注册。

#### 4.2.4 代码实践

**实践目标**：用纯 C 构造一个 `tile<4x8xf32>`，打印它的秩和第一维大小，体会「函数式」风格。

**操作步骤**（需要链接 4.4 节讲到的 `CudaTileCAPIDialects` 库）：

```c
/* 示例代码：构造 tile 并查询形状（需链接 CudaTileCAPIDialects） */
#include "cuda_tile-c/Dialect/CudaTileDialect.h"
#include "cuda_tile-c/Registration.h"
#include <mlir-c/IR.h>
#include <stdio.h>

int main(void) {
  MlirContext ctx = mlirContextCreate();
  MlirDialectRegistry reg = mlirDialectRegistryCreate();
  mlirCudaTileRegisterAllDialects(reg);          /* 装上 cuda_tile */
  mlirContextAppendDialectRegistry(ctx, reg);
  mlirDialectRegistryDestroy(reg);

  MlirType f32 = mlirF32TypeGet(ctx);            /* MLIR 内建 f32 */
  int64_t shape[2] = {4, 8};
  MlirType tile = mlirCudaTileTileTypeGet(ctx, /*rank=*/2, shape, f32);

  printf("rank = %ld\n", (long)mlirCudaTileTileTypeGetRank(tile));
  printf("dim0 = %lld\n", (long long)mlirCudaTileTileTypeGetDimSize(tile, 0));
  /* 用 Checked 版本试一个非法形状（元素数超限），观察返回空类型 */
  int64_t huge[1] = {1ll << 30};
  MlirType bad = mlirCudaTileTileTypeGetChecked(ctx, 1, huge, f32);
  printf("bad is null? %d\n", mlirTypeIsNull(bad));

  mlirContextDestroy(ctx);
  return 0;
}
```

> 说明：上面的 `mlirF32TypeGet`、`mlirTypeIsNull` 来自 MLIR 自带的 `mlir-c/IR.h`（不是 CUDA Tile 的头），属于 MLIR C API 公共部分。

**需要观察的现象**：`rank` 打印 `2`、`dim0` 打印 `4`；`bad is null?` 打印 `1`（因为单维 `2^30` 个元素远超 u3-l1 讲过的 `maxTileNumElements = 2^24` 上限，校验失败返回空类型）。

**预期结果**：行内类型构造与查询成功；非法形状被 `GetChecked` 拒绝并返回空句柄。

> 待本地验证：`mlirF32TypeGet` 的确切名字以你构建时拉取的 MLIR 版本为准（本项目锁定到固定 LLVM commit，见 u2-l1）；若名字不同，改用 `mlirFloatTypeGet` 等价入口。

#### 4.2.5 小练习与答案

**练习 1**：`mlirCudaTileTileTypeGet` 和 `mlirCudaTileTileTypeGetChecked` 在行为上最关键的差别是什么？什么场景下应该用后者？

**答案**：前者直接构造、不做校验（即便形状非法也会返回一个对象，后续使用时才可能出问题）；后者构造时同步校验，失败返回空句柄。当形状来自不可信输入、或想立刻给用户一个明确错误反馈时，应该用 `GetChecked` 并检查 `mlirTypeIsNull`。

**练习 2**：`mlirCudaTileWriteBytecodeToBuffer` 返回的缓冲区由谁拥有？为什么不能让它返回一个「自动释放」的对象？

**答案**：缓冲区由**调用方**拥有，必须用 `mlirCudaTileFreeBuffer` 释放（内部 `malloc` 了一块内存）。C 没有 RAII/析构函数，无法做到「自动释放」；若返回 MLIR 自带的可托管对象又会限制缓冲区的生命周期，所以选择显式「谁申请谁释放」。

---

### 4.3 CudaTileOptimizer C API：优化管线配置

#### 4.3.1 概念说明

u9-l3 讲过 C++ 侧的优化器：`TileIROptimizerOptions` + `optimizeTileIRModule`，以及命令行工具 `cuda-tile-optimize`。C API 把这条管线也暴露出来，让一个纯 C 宿主程序能「拿到一个 module → 跑优化 → 继续用」，而不必非要走命令行或 C++。

它的设计要点是：**用一个普通的 C 结构体 `mlirCudaTileOptConfig` 承载配置，用一个位掩码 `flags` 表达开关，用一个回调处理诊断（错误/警告）信息**。这些都是 C 语言里最稳定、最好跨边界的构造——结构体、整数、函数指针。

#### 4.3.2 核心流程

```
mlirCudaTileOptConfig config;
mlirCudaTileOptFlagsInit(&config);          /* 1. 先填默认值 */
config.optLevel = 3;                         /* 2. 按需改字段 */
config.flags |= CUDATILE_OPT_FLAG_FUSE_FMA;  /*    打开 FMA 融合 */
/* 可选：挂一个诊断回调 */
config.diagnosticCallback = myHandler;

mlirCudaTileApplyOptimizations(moduleOp, &config);  /* 3. 跑优化 */
        │
        ▼  内部：
        extractCudaTileModuleOp(...)   ── 兼容两种 module（u9-l3）
        registerTileIROptPasses()      ── 注册管线所需 Pass
        optimizeTileIRModule(...)      ── 真正跑（u9-l3 讲过的同函数）
```

配置到 C++ 的翻译由一个内部函数 `toCpp` 完成：C 结构体 → C++ `TileIROptimizerOptions`。位掩码用按位与解读成布尔开关。

#### 4.3.3 源码精读

**标志位与配置结构**。标志用「位移枚举」定义，可以按位或组合；配置结构里 `flags`、`optLevel`、`loopSplitThreshold` 对应 u9-l3 讲过的 C++ 字段，多出的两个字段是为诊断回调准备的：

- [include/cuda_tile-c/Dialect/CudaTileOptimizer.h:20-42](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile-c/Dialect/CudaTileOptimizer.h#L20-L42) — 定义 `CUDATILE_OPT_FLAG_NONE/ENABLE_MULTITHREAD/FUSE_FMA`、诊断回调类型 `MlirDiagnosticCallback`、以及 `mlirCudaTileOptConfig` 结构体（`flags`/`optLevel`/`loopSplitThreshold`/`diagnosticCallback`/`diagnosticUserData`）。

  注意 `loopSplitThreshold` 默认 1、`optLevel` 默认 3，和 u9-l3 讲的 C++ 侧默认值一致；`enableFuseFMA` **不在默认里**——FMA 融合是「非数值保持」变换（u9-l1），必须显式打开。

**默认值初始化**。C 没有「默认构造函数」，所以提供了一个 init 函数，约定「先用它清零并填默认值，再按需覆盖」：

- [include/cuda_tile-c/Dialect/CudaTileOptimizer.h:44-45](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile-c/Dialect/CudaTileOptimizer.h#L44-L45) — 声明 `mlirCudaTileOptFlagsInit`。
- 实现 [lib/CAPI/Dialect/CudaTileOptimizer.cpp:24-36](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/CAPI/Dialect/CudaTileOptimizer.cpp#L24-L36) — 先判空、`memset` 清零、再逐字段设默认值（`flags=0`、`loopSplitThreshold=1`、`optLevel=3`、两个诊断字段 `nullptr`）。这种「memset 再覆盖」的写法保证了将来给结构体加新字段时，未设置字段也是干净的零值。

**C→C++ 翻译**。`toCpp` 把位掩码翻译成两个布尔，把数值字段直接搬过去：

- [lib/CAPI/Dialect/CudaTileOptimizer.cpp:40-47](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/CAPI/Dialect/CudaTileOptimizer.cpp#L40-L47) — `enableMultithread = (flags & ENABLE_MULTITHREAD) != 0`、`enableFuseFMA = (flags & FUSE_FMA) != 0`。C++ 侧的 `pipelinePreText/pipelinePostText`（u9-l3 讲过的 `--before/--after`）没有 C 暴露，因此这里不填。

**主入口**。`mlirCudaTileApplyOptimizations` 做四件事：拆出 module、注册 Pass、可选挂诊断回调、跑优化：

- [include/cuda_tile-c/Dialect/CudaTileOptimizer.h:47-52](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile-c/Dialect/CudaTileOptimizer.h#L47-L52) — 声明：接收 `MlirOperation moduleOp` 与配置指针，返回 `MlirLogicalResult`（success/failure）。
- 实现 [lib/CAPI/Dialect/CudaTileOptimizer.cpp:49-88](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/CAPI/Dialect/CudaTileOptimizer.cpp#L49-L88) — 关键步骤：
  - L52-59 三道空值/类型护栏：`unwrap` 失败、`config` 为空、`extractCudaTileModuleOp` 取不到 cuda_tile module，都直接返回 failure。
  - L62 `registerTileIROptPasses()` 注册管线所需 Pass（u9-l3 讲过的同一函数）。
  - L64-76 若提供了 `diagnosticCallback`，就用一个 lambda 把 C++ 的 `Diagnostic` 包成 `MlirDiagnostic` 回调给 C 端，并把返回的 `MlirLogicalResult` 转回 `success/failure`——这样宿主程序能用纯 C 决定「这条诊断是否算已处理」。
  - L79 调用 u9-l3 讲过的 `optimizeTileIRModule`，第三参数 `handlerID.has_value()` 对应其 `verbose` 形参（有回调时当作「要打印」）。
  - L83-84 用完回调后从 `DiagnosticEngine` 注销，避免泄漏。

#### 4.3.4 代码实践

**实践目标**：理解「位掩码 + 结构体 + 回调」这套 C 配置如何映射到 C++ 管线，不要求真的跑通（那需要一个合法的 moduleOp，构造它需要 u4/u5 全套操作，超出本讲范围）。

**操作步骤**（源码阅读型实践）：

1. 打开 [`lib/CAPI/Dialect/CudaTileOptimizer.cpp`](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/CAPI/Dialect/CudaTileOptimizer.cpp)，对照 [include/cuda_tile-c/Dialect/CudaTileOptimizer.h](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile-c/Dialect/CudaTileOptimizer.h)。
2. 画一张「字段映射表」：左列是 `mlirCudaTileOptConfig` 的字段，右列是它最终影响的 C++ `TileIROptimizerOptions` 字段或行为。例如 `flags & CUDATILE_OPT_FLAG_FUSE_FMA` → `enableFuseFMA=true` → 管线里会插入 FuseFMA Pass（u9-l1）。
3. 思考：如果将来要给 C API 暴露 u9-l3 的 `--before`/`--after`（即 `pipelinePreText`/`pipelinePostText`），需要改哪几个文件？（答案见下方小练习）

**需要观察的现象**：你会确认「C 配置结构里每一个字段，都能在 `toCpp` 和 `optimizeTileIRModule` 调用链上找到落点」，没有孤儿字段。

**预期结果**：得到一张完整的 C→C++ 配置映射表。

#### 4.3.5 小练习与答案

**练习 1**：如果要给 C API 增加「前置/后置管线文本」（对应 C++ 的 `pipelinePreText/pipelinePostText`），需要改动哪几个文件、各加什么？

**答案**：至少三处——(1) `CudaTileOptimizer.h` 给 `mlirCudaTileOptConfig` 加两个 `const char *` 字段；(2) `CudaTileOptimizer.cpp` 的 `toCpp` 里把它们拷进 `o.pipelinePreText/pipelinePostText`；(3) 若想让宿主方便构造，可考虑加配套的 setter。注意还要在 `mlirCudaTileOptFlagsInit` 里把新字段置空，保证向后兼容。

**练习 2**：为什么 `mlirCudaTileApplyOptimizations` 在跑完优化后要 `eraseHandler`？不删会怎样？

**答案**：因为诊断 handler 注册在 `MLContext` 上，它是进程内长期存活的对象；不删会让回调一直挂着，下一次别人用这个 context 时仍会调到你的（可能已经失效的）`userData` 指针，造成 use-after-free。`eraseHandler` 是「用完即拆」的标准卫生做法。

---

### 4.4 CMake 集成：三个公共库与两种集成方式

#### 4.4.1 概念说明

到目前为止我们知道 C API 头文件有哪些、实现长什么样。但外部项目要真正用上，还差最后一步：**把这些 C API 编出来的库链接到自己的可执行文件里**。这一节回答两个问题：(1) C API 一共编出哪几个库？(2) README 给出的两种集成方式分别是什么？

CUDA Tile 的 C API 被刻意拆成**三个独立的公共库**，而不是一个大库，这样下游可以只链接自己需要的那部分，减少依赖体积：

| 库名 | 来源 | 依赖 | 用途 |
| --- | --- | --- | --- |
| `CudaTileCAPIRegistration` | `Registration.cpp` | `CudaTileDialect`、`CudaTileTransforms`、`MLIRCAPIIR` | 只想注册方言/Pass 的最小场景 |
| `CudaTileCAPIDialects` | `CudaTileDialect.cpp` | `CudaTileDialect`、`CudaTileTransforms`、字节码 Writer/Common 等 | 要构造类型/属性、写字节码 |
| `CudaTileCAPIOptimizer` | `CudaTileOptimizer.cpp` | `CudaTileOptimizer` | 要在内存里跑优化管线 |

这三个库都受顶层 CMake 选项 `CUDA_TILE_ENABLE_CAPI`（默认 `ON`）控制。

#### 4.4.2 核心流程

**库的产出链**：

```
顶层 CMakeLists.txt: option(CUDA_TILE_ENABLE_CAPI ...)   ── 默认 ON
        │
        ▼
lib/CMakeLists.txt: if(CUDA_TILE_ENABLE_CAPI) add_subdirectory(CAPI)   ── 关掉就不编
        │
        ▼
lib/CAPI/CMakeLists.txt          ── add_mlir_public_c_api_library(CudaTileCAPIRegistration ...) + add_subdirectory(Dialect)
lib/CAPI/Dialect/CMakeLists.txt  ── CudaTileCAPIDialects + CudaTileCAPIOptimizer
```

**测试的接入链**：

```
test/CMakeLists.txt: if(CUDA_TILE_ENABLE_CAPI) add_subdirectory(CAPI)   ── 把 ${CAPI_TEST_TARGETS} 并入 MLIR_TEST_DEPENDS
test/CAPI/CMakeLists.txt: add_capi_test_executable(test-cuda-tile-capi-register ...) ── 链接 CudaTileCAPIRegistration
test/lit.cfg.py: capi_tests = ["test-cuda-tile-capi-register"]   ── 注册成 lit 的工具替换
```

#### 4.4.3 源码精读

**顶层开关**：

- [CMakeLists.txt:95-96](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/CMakeLists.txt#L95-L96) — `option(CUDA_TILE_ENABLE_CAPI "Enable CUDA Tile C API" ON)`。CAPI 默认开，是因为它轻量且是很多下游的基本入口（不像 Python 绑定默认关）。

**条件编入 CAPI**：

- [lib/CMakeLists.txt:1-5](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/CMakeLists.txt#L1-L5) — `if(CUDA_TILE_ENABLE_CAPI) add_subdirectory(CAPI) endif()`。关掉时整个 CAPI 目录被跳过，不产生这三个库。

**三个公共库的定义**。都用 MLIR 提供的 `add_mlir_public_c_api_library` 宏，它会自动处理 `INSTALL`、SOVERSION、导出符号等公共库该有的属性：

- [lib/CAPI/CMakeLists.txt:1-8](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/CAPI/CMakeLists.txt#L1-L8) — 定义 `CudaTileCAPIRegistration`，`LINK_LIBS PUBLIC` 列出它对外传递的依赖（下游链接这一个库，就自动拿到 `CudaTileDialect`、`CudaTileTransforms`、`MLIRCAPIIR`）。
- [lib/CAPI/Dialect/CMakeLists.txt:1-19](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/CAPI/Dialect/CMakeLists.txt#L1-L19) — 定义 `CudaTileCAPIDialects`（依赖字节码 Writer/Common，所以能实现 4.2 的 `WriteBytecodeToBuffer`）和 `CudaTileCAPIOptimizer`（依赖 `CudaTileOptimizer`）。两个都带 `PARTIAL_SOURCES_INTENDED`，因为它们各自只编译 `Dialect/` 子目录里的部分源文件。

**测试目标与 lit 接入**：

- [test/CAPI/CMakeLists.txt:1-14](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/CAPI/CMakeLists.txt#L1-L14) — 定义辅助函数 `add_capi_test_executable`（用 `add_llvm_executable` 建可执行文件、链接指定库、把目标名追加到 `CAPI_TEST_TARGETS`），并用它生成 `test-cuda-tile-capi-register`，链接 `CudaTileCAPIRegistration`。
- [test/CMakeLists.txt:26-40](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/CMakeLists.txt#L26-L40) — `if(CUDA_TILE_ENABLE_CAPI) add_subdirectory(CAPI)`，并把 `${CAPI_TEST_TARGETS}` 并入 `MLIR_TEST_DEPENDS`，于是 `check-cuda-tile` 目标会先构建这些可执行文件。
- [test/lit.cfg.py:33-35](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/lit.cfg.py#L33-L35) — `capi_tests = ["test-cuda-tile-capi-register"]` 并调用 `add_tool_substitutions`，这样 `register.c` 顶部 `// RUN: test-cuda-tile-capi-register` 这一行里的工具名就能被 lit 解析成真实路径来执行。

**README 的两种集成方式**：

- [README.md:145-169](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/README.md#L145-L169) — **Option 1：使用预编译库**。把 CUDA Tile 装到某处（`${CUDA_TILE_INSTALL_DIR}`），下游 `include_directories` 指向它的头，按需 `target_link_libraries` 链接 `CudaTileDialect`、`CudaTileBytecodeReader`、`CudaTileBytecodeWriter` 等（注意 README 这里举例的是 **C++ 库**；要用 C API 就改为链接 4.4.1 表里的三个 `CudaTileCAPI*` 库）。
- [README.md:171-211](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/README.md#L171-L211) — **Option 2：把 CUDA Tile 源码拉进自己的项目**。用 `FetchContent`（或 submodule）拉源码，预先 `set` 好 `CUDA_TILE_USE_LLVM_INSTALL_DIR`、`CUDA_TILE_ENABLE_BINDINGS_PYTHON`、`CUDA_TILE_ENABLE_TESTING` 等缓存变量，再 `FetchContent_MakeAvailable`，最后把头路径指向源码目录**和**构建目录（因为有些生成的头在构建目录里），链接库的方式与 Option 1 相同。

> 小贴士：两种方式的差别只在「CUDA Tile 这一份代码从哪来、头从哪 include」；一旦库链接上，下游代码（include 哪个头、调哪个函数）是完全一样的。

#### 4.4.4 代码实践（本讲的核心实践任务）

**实践目标**：参考 README「Integrating CUDA Tile Into Your Project」与 [`test/CAPI/register.c`](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/CAPI/register.c)，写一个最小 C 程序调用 `mlirCudaTileRegisterAllDialects` 注册方言并打印成功信息，并说明 CMake 中需要链接哪些库。

**操作步骤**：

1. 把下面的程序存成 `my_register.c`（它就是在官方 `register.c` 基础上加了一行成功打印）：

   ```c
   /* 示例代码：在官方 register.c 基础上加成功提示 */
   #include "cuda_tile-c/Registration.h"
   #include <stdio.h>

   int main(void) {
       MlirContext ctx = mlirContextCreate();
       MlirDialectRegistry registry = mlirDialectRegistryCreate();
       mlirCudaTileRegisterAllDialects(registry);
       mlirContextAppendDialectRegistry(ctx, registry);
       mlirDialectRegistryDestroy(registry);

       MlirDialect cudaTile = mlirContextGetOrLoadDialect(
           ctx, mlirStringRefCreateFromCString("cuda_tile"));
       if (mlirDialectIsNull(cudaTile)) {
           fprintf(stderr, "failed to load cuda_tile dialect!\n");
           return -1;
       }
       printf("cuda_tile dialect registered and loaded OK\n");
       mlirContextDestroy(ctx);
       return 0;
   }
   ```
   注意这套调用顺序和官方 [test/CAPI/register.c:13-30](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/CAPI/register.c#L13-L30) 完全一致：建 context → 建 registry → 注册方言 → 把 registry 并进 context → 销毁 registry → 按名字取出已加载的方言并判空。

2. 写一个最小 `CMakeLists.txt`（按 README Option 1 风格，假定 CUDA Tile 已安装到 `$ENV{CUDA_TILE_INSTALL_DIR}`）：

   ```cmake
   # 示例代码：最小集成 CMake
   cmake_minimum_required(VERSION 3.20)
   project(my_register C)

   set(CUDA_TILE_INSTALL_DIR $ENV{CUDA_TILE_INSTALL_DIR} CACHE PATH "")
   include_directories(${CUDA_TILE_INSTALL_DIR}/include)

   add_executable(my_register my_register.c)
   # 只用了 Registration.h 的两个函数，所以只链接这一个 CAPI 库即可：
   target_link_libraries(my_register PRIVATE CudaTileCAPIRegistration)
   ```
   **说明需要链接的库**：本程序只调用了 `mlirCudaTileRegisterAllDialects`（来自 `Registration.h`），因此**只需链接 `CudaTileCAPIRegistration`** 一个库即可——它的 `LINK_LIBS PUBLIC`（见 4.4.3）会自动把 `CudaTileDialect`、`CudaTileTransforms`、`MLIRCAPIIR` 传递过来。如果你还用了 4.2 的类型/字节码接口，再加 `CudaTileCAPIDialects`；用到 4.3 的优化器，再加 `CudaTileCAPIOptimizer`。

3. 配置并构建：
   ```bash
   cmake -S . -B build -DCMAKE_PREFIX_PATH=$CUDA_TILE_INSTALL_DIR
   cmake --build build
   ./build/my_register
   ```

**需要观察的现象**：程序打印 `cuda_tile dialect registered and loaded OK`，退出码 0。

**预期结果**：链接成功、运行通过，证明「Option 1 预编译库 + 链接 `CudaTileCAPIRegistration`」这条路可行。

> 待本地验证：第 3 步要求你已先 `cmake --install` 把 CUDA Tile 装到 `$CUDA_TILE_INSTALL_DIR`；若没有安装步骤，可改用 Option 2，或直接复用项目自带的 `test-cuda-tile-capi-register` 目标（即 4.1.4 实践里的跑法）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `CudaTileCAPIRegistration` 的 CMake 里写 `LINK_LIBS PUBLIC CudaTileDialect ...` 而不是 `PRIVATE`？

**答案**：用 `PUBLIC` 是为了让下游链接 `CudaTileCAPIRegistration` 时，**自动传递**地也链接上 `CudaTileDialect` 等。C API 的实现内部确实直接用了 `CudaTileDialect` 的 C++ 符号（`insert<CudaTileDialect>`），这些符号对下游在运行时也必须可见，因此用 `PUBLIC` 传递。若改成 `PRIVATE`，下游在解析某些间接依赖时可能链接失败。

**练习 2**：`CUDA_TILE_ENABLE_CAPI=OFF` 时，`check-cuda-tile` 目标会少做哪些事？

**答案**：`lib/CMakeLists.txt` 不会 `add_subdirectory(CAPI)`，三个 `CudaTileCAPI*` 库不编；`test/CMakeLists.txt` 也不会 `add_subdirectory(CAPI)`，`CAPI_TEST_TARGETS` 为空，于是 `check-cuda-tile` 不会构建也不会运行 `test-cuda-tile-capi-register`。

---

## 5. 综合实践

把本讲的四个模块串起来，完成一个「**注册 → 构造类型 → （可选）跑优化 → 写字节码**」的端到端 C 程序骨架。由于完整构造一个合法 `moduleOp` 需要 u4/u5 的全套操作，这里给出可读的骨架与待你填充的标记，重点是把四块 C API 的调用顺序理顺。

```c
/* 示例代码：端到端骨架（部分步骤需你结合后续学习填充） */
#include "cuda_tile-c/Registration.h"
#include "cuda_tile-c/Dialect/CudaTileDialect.h"
#include "cuda_tile-c/Dialect/CudaTileOptimizer.h"
#include <mlir-c/IR.h>
#include <stdio.h>

int main(void) {
  /* ① 注册：方言 + Pass */
  MlirContext ctx = mlirContextCreate();
  MlirDialectRegistry reg = mlirDialectRegistryCreate();
  mlirCudaTileRegisterAllDialects(reg);
  mlirContextAppendDialectRegistry(ctx, reg);
  mlirDialectRegistryDestroy(reg);
  mlirCudaTileRegisterAllPasses();

  /* ② 构造类型：tile<4x8xf32> */
  MlirType f32 = mlirF32TypeGet(ctx);
  int64_t shape[2] = {4, 8};
  MlirType tile = mlirCudaTileTileTypeGet(ctx, 2, shape, f32);
  printf("tile rank=%ld\n", (long)mlirCudaTileTileTypeGetRank(tile));

  /* ③ 跑优化（需要先有一个合法的 moduleOp，留待你用 u4/u5 的操作构造） */
  mlirCudaTileOptConfig cfg;
  mlirCudaTileOptFlagsInit(&cfg);
  cfg.optLevel = 3;
  cfg.flags |= CUDATILE_OPT_FLAG_FUSE_FMA;
  /* mlirCudaTileApplyOptimizations(moduleOp, &cfg); */  /* TODO: 你构造出 moduleOp 后启用 */

  /* ④ 写字节码到内存（同样需要 moduleOp） */
  /* MlirStringRef bc = mlirCudaTileWriteBytecodeToBuffer(moduleOp); */
  /* ... 用完 mlirCudaTileFreeBuffer(bc); */

  mlirContextDestroy(ctx);
  return 0;
}
```

**你要完成的任务**：

1. 编译并跑通 ①② 两步（链接 `CudaTileCAPIRegistration` + `CudaTileCAPIDialects`），确认 `tile rank=2` 打印出来。
2. 阅读官方 [`test/CAPI/register.c`](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/CAPI/register.c)，对比它和你的 ① 步骤，确认调用顺序一致。
3. 写一段不超过 150 字的说明：要把 ③ 启用，需要链接哪个库、为什么 `moduleOp` 不能直接用一个空 context 伪造。

**预期结果**：①② 步运行通过；说明里应点出「③ 需要 `CudaTileCAPIOptimizer` 库，且 `moduleOp` 必须是经 `extractCudaTileModuleOp` 能识别的合法 cuda_tile module，否则 `mlirCudaTileApplyOptimizations` 会在第一道护栏就返回 failure」。

> 待本地验证：第 1 步的链接库细节以你本地构建产物为准；若 `CudaTileCAPIDialects` 与 `CudaTileCAPIRegistration` 合并为单一库（未来可能演进），按实际目标名链接即可。

---

## 6. 本讲小结

- **C API 的定位**：`cuda_tile-c` 是 C++ 库的稳定 C 边界，解决跨编译器、跨语言互操作；Python 绑定等也建立在 MLIR 的 C API 之上。
- **注册入口**：`Registration.cpp` 用 `unwrap`/`wrap` 把「注册全部方言」「注册全部 Pass」翻译成对 `CudaTileDialect` 和 `registerCudaTilePasses()` 的两行调用，是第三方集成的最小起点。
- **方言能力清单**：`CudaTileDialect.h` 暴露类型（Pointer/Tile/Token/各 View）、属性（舍入/原子/内存序/优化提示等）、字节码写入与逐个 Pass 注册；`GetChecked` 版本在类型非法时返回空句柄而非抛异常。
- **优化器 C API**：`mlirCudaTileOptConfig` 用结构体 + 位掩码 + 诊断回调表达配置，`toCpp` 翻译成 C++ `TileIROptimizerOptions`，`mlirCudaTileApplyOptimizations` 内部走 u9-l3 讲过的 `optimizeTileIRModule`，并有三道空值/类型护栏。
- **CMake 集成**：C API 编成 `CudaTileCAPIRegistration` / `CudaTileCAPIDialects` / `CudaTileCAPIOptimizer` 三个公共库，受 `CUDA_TILE_ENABLE_CAPI`（默认 ON）控制；README 给出 Option 1 预编译库、Option 2 源码 `FetchContent` 两种集成方式。
- **测试落点**：`test/CAPI/register.c` 是官方最小示例，经 `test/CMakeLists.txt` 并入 `check-cuda-tile`、经 `lit.cfg.py` 注册成 lit 工具替换。

## 7. 下一步学习建议

- **u10-l2 Python 绑定架构**：Python 绑定正是建立在 MLIR C API 之上的更高层封装，理解了本讲的 `wrap/unwrap` 与方言句柄宏，再去读 `SiteInitializer.cpp`、`DialectCudaTile.cpp` 会非常顺手。
- **重读 u9-l3 cuda-tile-optimize**：本讲的 `mlirCudaTileApplyOptimizations` 与命令行工具共用同一个 `optimizeTileIRModule`，对照阅读能看清「同一管线、命令行 vs C API 两个入口」的设计。
- **动手扩展 C API**：试着按 4.3.5 练习 1 的思路，给 `mlirCudaTileOptConfig` 增加 `pipelinePreText`/`pipelinePostText` 字段，完整走一遍「改头文件 → 改实现 `toCpp` → 改 `init` → 重新构建」的流程，体会 C API 演进的成本与稳定性考量。
