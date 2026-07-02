# Python 绑定架构

> 讲义编号：u10-l2　学习阶段：advanced　依赖：u10-l1（C API 集成接口）

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 CUDA Tile 的 Python 绑定是「**三层金字塔**」：最底层是 C API（u10-l1），中间层是 nanobind 暴露出来的原生扩展模块 `_cuda_tile` 与自动注册钩子 `_site_initialize_1`，最上层是手写的 `cuda_tile_ops.py` 高层 API。
- 掌握 `SiteInitializer.cpp` 中 `register_dialects` / `register_passes` 两个钩子的区别——前者在 import 时被 MLIR 的 site-initialization 机制**自动调用**，后者必须**用户手动调用**。
- 读懂 `DialectCudaTile.cpp` 如何用 `mlir_type_subclass` / `mlir_attribute_subclass` 两个适配器，把 C API 的「裸函数」包装成 Python 的「类」，从而暴露 `PointerType` / `TileType` / `TokenType` / `TensorViewType` 类型与 `applyTileIROptimizations` / `writeBytecode` 函数。
- 理解 `cuda_tile_ops.py` 中 `Tile` / `Pointer` / `TileView` 包装类如何通过运算符重载（`__add__` / `__mul__` 等）与 `cuda_tile_op` 装饰器，把「构造 SSA 值」这件事变成接近 NumPy 的写法。
- 用 Python 程序化地构造一段 IR：建 `Context`、注册方言、用 `make_tile_type` 造类型、用 `constant` + `add` 建操作、再把整段 IR 打印成 MLIR 文本。

## 2. 前置知识

在进入源码之前，先用通俗语言建立几个心智模型。

### 2.1 为什么 Python 绑定不能直接调 C++

CUDA Tile 的核心是用 C++ 写的 MLIR 方言。Python 想调用这些 C++ 代码，理论上可以直接用 `pybind11` / `nanobind` 把 C++ 类逐个暴露给 Python。但本项目（以及整个 MLIR 上游）没有这么做，而是**绕了一道 C API 的弯路**：

```
Python 代码
   │  (调用 _cuda_tile 模块里的 Python 类)
   ▼
nanobind 扩展模块 _cuda_tile   ← DialectCudaTile.cpp 编译产物
   │  (调用 mlirCudaTile* 这类 C 函数)
   ▼
C API (cuda_tile-c)            ← u10-l1 讲的 wrap/unwrap 桥
   │  (unwrap 后调真实实现)
   ▼
C++ 库 (CudaTileDialect 等)
```

为什么绕弯？因为 C API 是**稳定的 ABI 边界**：只要 `mlirCudaTileTileTypeGet` 这种函数签名不变，底层 C++ 怎么重构、换编译器，Python 侧都不用重新学。u10-l1 已经讲过这套 `wrap/unwrap` 套路，本讲只关注「C API 之上的两层 Python 封装」。

### 2.2 nanobind 是什么

`nanobind` 是一个轻量的 Python ↔ C++ 绑定库（`pybind11` 的继任者，体积更小、编译更快）。它用一个 `NB_MODULE(名字, m)` 宏来声明一个 Python 扩展模块，然后在宏的函数体里用 `m.def(...)` 注册函数、用 `nb::class_<...>(m, "名字")` 注册类。

MLIR 在 nanobind 之上又加了一层「适配器（adaptor）」工具，放在头文件 `mlir/Bindings/Python/NanobindAdaptors.h` 里。CUDA Tile 的两个 `.cpp` 文件开头都 `#include` 了它（见 [DialectCudaTile.cpp:10](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/python/Dialect/DialectCudaTile.cpp#L10)）。其中最关键的两个适配器是：

- `mlir_type_subclass(m, "TileType", isa_fn)`：在 Python 侧注册一个 `TileType` 类，它「是」MLIR 通用 `Type` 的子类，并且能用 `isa_fn` 判断某个 `Type` 到底是不是 `TileType`。
- `mlir_attribute_subclass(m, "RoundingModeAttr", isa_fn)`：同理，注册一个属性类。

这两个适配器让 Python 侧能写出 `TileType.get([4,4], F32())` 这种面向对象的写法，而背后只是去调对应的 C API 函数。

### 2.3 MLIR Python 的「site initialization」机制

这是本讲最容易困惑、但也最关键的一点。MLIR 的 Python 包支持「多个扩展模块在 import 时自动注册自己的方言」。机制是这样的：

- 每个扩展模块如果取名叫 `_site_initialize_*`（注意带数字后缀），MLIR 的 Python 运行时在加载 `_mlir_libs` 包时，会**自动 import** 这些模块、并自动调用其中名为 `register_dialects` 的函数。
- 这样，用户只要 `import cuda_tile._mlir.ir`，`cuda_tile` 方言就已经悄悄注册进了 MLIR 的全局方言注册表，无需用户手动调用任何注册函数。

> ⚠️ 注意：被自动调用的**只有约定名字 `register_dialects` 的函数**。注册 Pass 没有这种自动机制，所以 `register_passes` 必须用户显式调用——源码注释里特意强调了这一点（[SiteInitializer.cpp:25-27](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/python/SiteInitializer.cpp#L25-L27)）。

### 2.4 与前序讲义的衔接

本讲假设你已经知道：

- C API 提供了 `mlirCudaTileRegisterAllDialects` / `mlirCudaTileRegisterAllPasses`（u10-l1），以及一批 `mlirCudaTileTileTypeGetChecked`、`mlirCudaTilePointerTypeGet` 之类的类型/属性构造函数和 `mlirCudaTileApplyOptimizations` / `mlirCudaTileWriteBytecodeToBuffer` 功能函数。
- `TileType`、`PointerType`、`TokenType`、各类 View 类型、各种枚举属性的语义（u3、u4、u5、u6）。
- 优化器有一条「字节码进、字节码出」的管线（u9-l3），FuseFMA 是非数值保持变换（u9-l1）。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [`python/SiteInitializer.cpp`](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/python/SiteInitializer.cpp) | 编译成 `_site_initialize_1` 模块，提供 import 期自动注册方言的钩子 `register_dialects`，以及需手动调用的 `register_passes`。 |
| [`python/Dialect/DialectCudaTile.cpp`](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/python/Dialect/DialectCudaTile.cpp) | 编译成 `_cuda_tile` 模块，用 nanobind 适配器把 C API 的类型/属性/函数包成 Python 类与函数。 |
| [`python/cuda_tile/dialects/cuda_tile_ops.py`](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/python/cuda_tile/dialects/cuda_tile_ops.py) | 手写的高层 API：`Tile`/`Pointer`/`TileView` 包装类、运算符重载、`constant`/`iota`/`make_tile_type` 辅助函数、MMA 配置注册表。 |
| [`test/python/cuda_tile_public_bindings.py`](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/python/cuda_tile_public_bindings.py) | 公共绑定测试，是本讲实践任务与「合法用法」的权威样板。 |
| [`python/CMakeLists.txt`](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/python/CMakeLists.txt) | 把上面三个 `.cpp`/`.py` 声明为扩展模块、方言绑定、并组装成最终的 `cuda_tile._mlir` Python 包。 |

## 4. 核心概念与源码讲解

### 4.1 SiteInitializer 钩子：import 期的自动注册

#### 4.1.1 概念说明

`SiteInitializer.cpp` 是整个 Python 绑定里**最短**的文件，只有二十多行，但它承担了一个关键职责：**让 `cuda_tile` 方言在用户 import 时自动注册**，而不需要用户每次都写 `register_dialect(ctx)`。

它被编译成一个名为 `_site_initialize_1` 的 nanobind 模块。这个名字不是随便起的——MLIR 的 Python 运行时会扫描所有 `_site_initialize_N` 模块并自动加载它们，这是 u2.3 节讲的「site initialization」机制的落地。

#### 4.1.2 核心流程

1. CMake 把 `SiteInitializer.cpp` 声明为模块名 `_site_initialize_1`（[python/CMakeLists.txt:35-47](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/python/CMakeLists.txt#L35-L47)），并通过 `EMBED_CAPI_LINK_LIBS CudaTileCAPIRegistration` 把 C API 注册库静态嵌进来。
2. 用户在 Python 里 `import cuda_tile._mlir.ir`，触发 `_mlir_libs` 包的初始化。
3. MLIR 运行时发现 `_site_initialize_1` 模块，自动 import 它。
4. 该模块里名为 `register_dialects` 的函数被自动调用，把 `cuda_tile` 方言塞进传入的 `MlirDialectRegistry`。

注意第 4 步只对 `register_dialects` 生效；`register_passes` 不在自动调用名单里。

#### 4.1.3 源码精读

整个模块用一个 `NB_MODULE` 宏声明（[SiteInitializer.cpp:16](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/python/SiteInitializer.cpp#L16)），这个宏的语法是「模块名 + 一个 `m` 变量」，函数体里用 `m.def(...)` 往模块里注册 Python 函数。

```cpp
NB_MODULE(_site_initialize_1, m) {
  m.doc() = "All CUDA Tile IR related dialects (cuda_tile) and passes.";
```

这里把模块名定为 `_site_initialize_1`，正是 MLIR 约定的「自动初始化」命名模式。

接下来是第一个钩子 `register_dialects`（[SiteInitializer.cpp:21-23](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/python/SiteInitializer.cpp#L21-L23)）：

```cpp
// NB: This is a special API hook that will be automatically called during
// library initialization.
m.def("register_dialects", [](MlirDialectRegistry registry) {
  mlirCudaTileRegisterAllDialects(registry);
});
```

注释明确说这是一个「特殊的 API 钩子」，会在库初始化时**自动调用**。它的实现极简：直接转交给 C API 函数 `mlirCudaTileRegisterAllDialects`（u10-l1 讲过，它把 `cuda_tile` 方言 `insert` 进注册表）。注意参数是一个 `MlirDialectRegistry`——这是 MLIR Python 运行时在自动调用时传进来的。

第二个钩子 `register_passes`（[SiteInitializer.cpp:25-27](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/python/SiteInitializer.cpp#L25-L27)）：

```cpp
// NB: This is not a special API hook and must be invoked manually by a user
// in Python to register the passes.
m.def("register_passes", []() { mlirCudaTileRegisterAllPasses(); });
```

注释特意强调：这**不是**特殊钩子，必须由用户在 Python 里**手动调用**。它的实现是转交 `mlirCudaTileRegisterAllPasses`，注册 CUDA Tile 自家的变换 Pass（FuseFMA、LoopSplit 等）。

> 🔑 一句话记住两者的区别：`register_dialects` 靠「名字」被自动调，`register_passes` 靠「用户」被手动调。

#### 4.1.4 代码实践

**实践目标**：验证 `register_dialects` 确实在 import 时被自动调用，并对比「注册 vs 不注册」的差异。

**操作步骤**（需要先按 README 用 `-DCUDA_TILE_ENABLE_BINDINGS_PYTHON=ON` 构建一次，然后把 `build/python_packages` 加入 `PYTHONPATH`）：

1. 写一个最小脚本：
   ```python
   # test_site_init.py（示例代码）
   from cuda_tile._mlir.ir import Context
   with Context() as ctx:
       # 不显式调 register_dialect，直接 parse 一个 cuda_tile 类型
       from cuda_tile._mlir.ir import Type
       t = Type.parse("!cuda_tile.ptr<i32>", ctx)
       print(t)   # 期望能解析成功，说明方言已自动注册
   ```
2. 把脚本放在仓库根目录，运行 `PYTHONPATH=build/python_packages python test_site_init.py`。
3. 再写一个需要 Pass 的脚本，对比：
   ```python
   from cuda_tile._mlir._mlir_libs import _site_initialize_1
   _site_initialize_1.register_passes()   # 必须手动调，否则后续 cuda-tile-opt 风格的 Pass 用不了
   ```

**需要观察的现象**：

- 第 1 步即便**没有**显式调用任何 `register_*`，`Type.parse("!cuda_tile.ptr<i32>")` 也能成功——说明 `register_dialects` 被 import 机制自动调用了。
- 第 3 步如果**省略** `register_passes()`，后续若依赖 cuda_tile 自家 Pass 会报「未注册」类错误。

**预期结果**：第 1 步打印 `!cuda_tile.ptr<i32>`。第 3 步的 Pass 注册行为：待本地验证（取决于具体 Pass 调用方式）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `_site_initialize_1` 这个模块名末尾要带数字 `1`？

**参考答案**：这是 MLIR Python 运行时约定的命名模式 `_site_initialize_N`。运行时会按字典序扫描并加载所有匹配该模式的模块，从而允许多个不同的方言扩展模块各自独立注册，互不冲突。数字后缀只是为了让多个模块可共存。

**练习 2**：如果把 `register_dialects` 改名为 `register_my_dialects`，会发生什么？

**参考答案**：自动注册会失效。MLIR 运行时只自动调用**约定名字** `register_dialects` 的函数；改名后它就退化成一个普通函数，必须用户手动调用才能注册方言，和现在的 `register_passes` 一样。

---

### 4.2 DialectCudaTile 绑定：把 C API 包成 Python 类

#### 4.2.1 概念说明

`SiteInitializer.cpp` 只管「注册」，真正让用户能**创建 cuda_tile 的类型、属性、调用优化器**的，是 `DialectCudaTile.cpp`。它被编译成第二个 nanobind 模块 `_cuda_tile`（[python/CMakeLists.txt:62-75](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/python/CMakeLists.txt#L62-L75)），通过 `EMBED_CAPI_LINK_LIBS` 嵌入 `CudaTileCAPIDialects` 与 `CudaTileCAPIOptimizer` 两个 C API 库。

这个文件做的事情可以归为三类：

1. **函数绑定**：`register_dialect`、`applyTileIROptimizations`、`writeBytecode`、`addLoopSplitThresholdAttr` 等，把 C API 的功能函数暴露成 Python 函数。
2. **类型绑定**：用 `mlir_type_subclass` 把 `PointerType` / `TileType` / `TokenType` / `TensorViewType` / `PartitionViewType` / `StridedViewType` 包成 Python 类。
3. **属性绑定**：用 `mlir_attribute_subclass` 把 `RoundingModeAttr` / `OptimizationHintsAttr` / `MemoryOrderingSemanticsAttr` 等一批枚举/复合属性包成 Python 类。

#### 4.2.2 核心流程

类型绑定的套路高度统一，可以总结成一个「四件套」模板：

1. `mlir_type_subclass(m, "类名", isa判定函数)` —— 声明一个 Python 类，并告诉它「怎么判断一个 MLIR `Type` 是不是本类」。
2. `.def_classmethod("get", ...)` —— 注册一个类方法 `get`，内部调 C API 的 `*Get` / `*GetChecked` 构造函数造类型。
3. `.def_classmethod("upcast_type", ...)` —— 注册「把通用 `Type` 向上转型成本类」的方法（返回 `None` 表示转型失败）。
4. `.def_property_readonly("xxx", ...)` —— 注册只读属性（如 `shape`、`element_type`），内部调 C API 的 `getter`。

属性绑定的套路也一样，只是把 `mlir_type_subclass` 换成 `mlir_attribute_subclass`，把 `upcast_type` 换成对 `MlirAttribute` 的判定。

#### 4.2.3 源码精读

**函数绑定**——最简单的是 `register_dialect`（[DialectCudaTile.cpp:27-36](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/python/Dialect/DialectCudaTile.cpp#L27-L36)）：

```cpp
m.def(
    "register_dialect",
    [](MlirContext context, bool load) {
      MlirDialectHandle handle = mlirGetDialectHandle__cuda_tile__();
      mlirDialectHandleRegisterDialect(handle, context);
      if (load) {
        mlirDialectHandleLoadDialect(handle, context);
      }
    },
    nb::arg("context") = nb::none(), nb::arg("load") = true);
```

它接收一个 `MlirContext` 和一个 `load` 布尔（默认 `true`），先用 MLIR 通用句柄 API 拿到 `cuda_tile` 方言句柄，再「注册 + 可选加载」到 context。注意它和 `SiteInitializer` 里的 `register_dialects`（操作 `DialectRegistry`）作用对象不同——这个是直接把方言装进某个具体 `Context`。测试里就是这么用的：`register_dialect(ctx, load=True)`（[cuda_tile_public_bindings.py:28](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/python/cuda_tile_public_bindings.py#L28)）。

**优化器绑定**——`applyTileIROptimizations`（[DialectCudaTile.cpp:42-66](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/python/Dialect/DialectCudaTile.cpp#L42-L66)）是 C API 优化器在 Python 侧的入口。为了避免 nanobind 直接绑 C 结构体带来的符号问题，作者先用一个简单的 C++ `struct TileIROptimizationsOptsWrapper`（[DialectCudaTile.cpp:42-45](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/python/Dialect/DialectCudaTile.cpp#L42-L45)）承载 `opt_level` / `fuse_fma` 两个选项，再用 `nb::class_` 暴露成 Python 类 `TileIROptimizationsOpts`：

```cpp
m.def(
    "applyTileIROptimizations",
    [](nb::object &moduleOp, const TileIROptimizationsOptsWrapper &opts) {
      mlirCudaTileOptConfig config;
      mlirCudaTileOptFlagsInit(&config);
      config.optLevel = opts.opt_level;
      if (opts.fuse_fma)
        config.flags |= CUDATILE_OPT_FLAG_FUSE_FMA;
      MlirOperation mlirOp = nb::cast<MlirOperation>(moduleOp);
      return mlirLogicalResultIsSuccess(
          mlirCudaTileApplyOptimizations(mlirOp, &config));
    },
    ...);
```

这就是 u10-l1 讲过的 `mlirCudaTileOptConfig` + 位掩码 `CUDATILE_OPT_FLAG_FUSE_FMA` 的 Python 入口，对应 u9-l3 的优化器管线。

**字节码绑定**——`writeBytecode`（[DialectCudaTile.cpp:84-111](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/python/Dialect/DialectCudaTile.cpp#L84-L111)）用一个巧妙的跨平台手法：先用 C API 把字节码写进内存 buffer（`mlirCudaTileWriteBytecodeToBuffer`），再用 Python 的文件对象 `file_obj.attr("write")(...)` 落盘，从而避免在 C++ 里直接操作文件描述符（那样在 Windows 上很麻烦）。buffer 为空（非法模块）则返回 `False`，这正是测试 `test_write_tile_ir_bytecode_invalid_module` 验证的（[cuda_tile_public_bindings.py:359-378](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/python/cuda_tile_public_bindings.py#L359-L378)）。

**类型绑定**——以 `TileType` 为例（[DialectCudaTile.cpp:142-178](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/python/Dialect/DialectCudaTile.cpp#L142-L178)），它就是「四件套」模板的典型：

```cpp
mlir_type_subclass(
    m, "TileType",
    [](MlirType type) -> bool { return mlirCudaTileTypeIsATileType(type); })
    .def_classmethod(
        "get",
        [](const nb::object &cls, const std::vector<int64_t> &shape,
           MlirType elementType, MlirContext context) -> nb::object {
          MlirType type = mlirCudaTileTileTypeGetChecked(
              context, shape.size(), shape.data(), elementType);
          if (mlirTypeIsNull(type))
            return nb::none();        // 校验失败 → 返回 None，而非抛异常
          return cls(type);
        },
        ...)
    .def_property_readonly("shape", ...)
    .def_property_readonly("element_type", ...);
```

关键点有两个：

1. `get` 用的是 **`GetChecked` 版本**（带 `Checked` 后缀），它会触发 u3-l1 讲的 `verifyTileSize` 校验。校验失败时返回**空类型**（`mlirTypeIsNull` 为真），Python 侧就返回 `nb::none()`——这与 u10-l1 强调的「`getCheckedType` 失败返回空值而非抛异常」一脉相承。
2. 注意对比 `PointerType.get`（[DialectCudaTile.cpp:120-129](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/python/Dialect/DialectCudaTile.cpp#L120-L129)）：它用的是 `mlirCudaTilePointerTypeGet`（**不带** `Checked`），因为 `PointerType` 没有 verifier（u3-l2），所以不需要校验版。

**属性绑定**——以 `RoundingModeAttr`（[DialectCudaTile.cpp:461-486](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/python/Dialect/DialectCudaTile.cpp#L461-L486)）为例。注意它有一个值得学习的「容错」设计：如果传入的字符串非法，它不抛异常，而是**回退到默认值 `nearest_even`**：

```cpp
MlirAttribute attr = mlirCudaTileRoundingModeAttrGet(context, valueStr);
if (mlirAttributeIsNull(attr)) {
  // Fallback to default if invalid value
  MlirStringRef defaultStr = mlirStringRefCreateFromCString("nearest_even");
  attr = mlirCudaTileRoundingModeAttrGet(context, defaultStr);
}
return cls(attr);
```

但不是所有属性都这么宽容——`MemoryScopeAttr`、`PaddingValueAttr`、`AtomicRMWModeAttr` 等在遇到非法值时会**直接抛 `std::invalid_argument`**（如 [DialectCudaTile.cpp:566-568](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/python/Dialect/DialectCudaTile.cpp#L566-L568)），测试 `test_memory_scope_attr` 就验证了 `Invalid memory scope: invalid_scope` 的报错（[cuda_tile_public_bindings.py:217](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/python/cuda_tile_public_bindings.py#L217)）。这种「属性间容错策略不一致」是阅读本文件时值得留意的一个细节。

#### 4.2.4 代码实践

**实践目标**：直接使用 `_cuda_tile` 原生模块（不经 `cuda_tile_ops.py` 高层封装），体会「类型绑定四件套」的实际效果。

**操作步骤**（示例代码）：

```python
# test_native_binding.py（示例代码）
from cuda_tile._mlir._mlir_libs._cuda_tile import (
    PointerType, TileType, register_dialect,
)
from cuda_tile._mlir.extras import types as T
from cuda_tile._mlir.ir import Context, Type

with Context() as ctx:
    register_dialect(ctx, load=True)

    # 用原生类的 get 造类型
    ptr = PointerType.get(T.i32())
    print("ptr:", ptr, " pointee:", ptr.pointee_type)

    tile = TileType.get([64, 32], T.i32())
    print("tile:", tile, " shape:", tile.shape, " elem:", tile.element_type)

    # 也验证从文本 parse 出来的类型能被「向上转型」成子类
    parsed = Type.parse("!cuda_tile.tile<64x32xi32>", ctx)
    assert TileType(parsed) == parsed
```

**需要观察的现象**：

- `ptr.pointee_type` 打印出 `i32`，说明只读属性 `pointee_type` 内部调了 C API 的 `mlirCudaTilePointerTypeGetPointeeType`。
- `tile.shape` 打印出 `[64, 32]`（Python list），说明 C++ 侧 `std::vector<int64_t>` 被 nanobind 自动转成了 Python list。
- `TileType(parsed) == parsed` 成立，说明 `upcast_type` / `__eq__` 链路正常。

**预期结果**：上面三条断言全部通过。这段脚本其实就是 [cuda_tile_public_bindings.py:26-59](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/python/cuda_tile_public_bindings.py#L26-L59) 里 `test_pointer_type` / `test_tile_type` 的精简版。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `TileType.get` 用 `mlirCudaTileTileTypeGetChecked`，而 `PointerType.get` 用不带 `Checked` 的版本？

**参考答案**：`TileType` 有形状校验（维为正、每维 2 的幂、元素总数不超上限，见 u3-l1），需要 `Checked` 版本在构造时跑校验，失败返回空类型再被 Python 转成 `None`。`PointerType` 没有 verifier（u3-l2），任何合法 pointee 类型都能直接构造，所以用普通 `Get` 即可。

**练习 2**：如果用户调 `RoundingModeAttr.get("not_a_mode")` 会发生什么？对比 `MemoryScopeAttr.get("not_a_scope")` 又会怎样？

**参考答案**：`RoundingModeAttr.get("not_a_mode")` 不会报错，而是静默回退到默认值 `nearest_even`（容错策略）。而 `MemoryScopeAttr.get("not_a_scope")` 会抛 `ValueError("Invalid memory scope: not_a_scope")`（严格策略）。两者容错策略不同，使用时要注意。

---

### 4.3 cuda_tile_ops.py 高层 API：NumPy 式的 IR 构造

#### 4.3.1 概念说明

`_cuda_tile` 原生模块虽然能用，但写起来很「底层」：你得手动建 Operation、手动处理结果个数、手动把 Python 标量包成 constant。`cuda_tile_ops.py` 就是在它之上加的一层**高层 API**，目标是让构造 IR 像写 NumPy 一样自然——比如 `a + b` 直接生成一个加法操作。

这层文件很长（5000 多行），但它建立在三个清晰的支柱上：

1. **包装类** `Tile` / `Pointer` / `TileView` / `Token`：把裸 SSA `Value` 包成有类型信息的对象，并挂上运算符重载。
2. **`cuda_tile_op` 装饰器**：统一处理「源位置（location）」这件繁琐事。
3. **辅助函数** `make_tile_type` / `constant` / `iota` 等：把高频操作封成一行调用。

#### 4.3.2 核心流程

构造一段 IR 的典型流程是：

1. 用 `make_tile_type(Float32, [4,4])` 造出 `tile<4x4xf32>` 类型。
2. 用 `constant([1.0]*16, tile_type=...)` 或 `iota(n, Int32)` 造出初始 `Tile` 对象。
3. 用 `a + b` / `a * b` / `cuda_tile.add(a, b)` 等运算，自动生成对应的 cuda_tile 操作并返回新的 `Tile`。
4. 操作的真正「落盘」发生在调用 `_cuda_tile.XxxOp(...)` 时——高层函数最终都会去调底层生成的 `_cuda_tile_ops_gen` 模块里的操作类。

整层的关键依赖关系（[cuda_tile_ops.py:16-19](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/python/cuda_tile/dialects/cuda_tile_ops.py#L16-L19)）：

```python
from ._cuda_tile_ops_gen import _Dialect
from . import _cuda_tile_enum_gen as _cuda_tile_enum
from . import _cuda_tile_ops_gen as _cuda_tile            # ← TableGen 生成的操作类
from .._mlir_libs import _cuda_tile as _cuda_tile_capi    # ← DialectCudaTile.cpp 编译的原生模块
```

也就是说，`_cuda_tile`（生成的操作类）+ `_cuda_tile_capi`（原生类型/属性类）共同构成底层，`cuda_tile_ops.py` 在上面提供友好接口。这两个 `_cuda_tile_*_gen` 模块是由 [python/cuda_tile/dialects/CudaTileOps.td](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/python/cuda_tile/dialects/CudaTileOps.td) 经 MLIR 的 Python 绑定生成器产出的（CMake 里 `declare_mlir_dialect_python_bindings`，[python/CMakeLists.txt:48-60](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/python/CMakeLists.txt#L48-L60)）。

#### 4.3.3 源码精读

**`Tile` 包装类与运算符重载**——`Tile` 继承自 MLIR 的 `_ods_ir.Value`，内部持有一个 `tile_type` 和原始 `value`（[cuda_tile_ops.py:874-887](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/python/cuda_tile/dialects/cuda_tile_ops.py#L874-L887)）。它的精髓在于一堆双下划线方法把 Python 运算符映射到 cuda_tile 操作：

```python
def __add__(self, rhs):
    return add(self, rhs)

def __mul__(self, rhs):
    return mul(self, rhs)

def __abs__(self):
    if isinstance(self.element_type, _ods_ir.IntegerType):
        return absi(self)
    if isinstance(self.element_type, _ods_ir.FloatType):
        return absf(self)
    ...
```

注意 `__add__` 并不直接生成 `AddFOp`，而是调统一的 `add(self, rhs)` 函数——后者会根据元素类型自动分派到 `_addi` / `_addf` / `_offset`（整数加 / 浮点加 / 指针偏移，见 [cuda_tile_ops.py:1906-1942](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/python/cuda_tile/dialects/cuda_tile_ops.py#L1906-L1942)）。这种「一个符号、按类型分派」的设计让 `a + b` 在 int/float/pointer 上都能工作。

`Pointer` 是 `Tile` 的子类，但它是个**注解类**（[cuda_tile_ops.py:1038-1048](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/python/cuda_tile/dialects/cuda_tile_ops.py#L1038-L1048)）：构造时会校验「标量 + 指针元素类型」，但并非所有指针 tile 都是 `Pointer` 实例——docstring 明确说 "not all pointer tiles are of the Pointer class"。

**`cuda_tile_op` 装饰器**——每个高层操作函数都套了这个装饰器（[cuda_tile_ops.py:1208-1227](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/python/cuda_tile/dialects/cuda_tile_ops.py#L1208-L1227)），它目前只做一件事：**自动生成 source location**。如果调用者没传 `loc=`，它就用 `inspect.currentframe()` 拿到调用栈，把「Python 源文件名 + 行号 + 调用函数名」做成一个 MLIR `Location`，再透传给真正的操作构造：

```python
def wrapper(*args, **kwargs):
    loc = kwargs.pop("loc", None)
    if loc is None:
        frame = _inspect.currentframe().f_back
        file_loc = _ods_ir.Location.file(frame.f_code.co_filename, frame.f_lineno, 0)
        loc = _ods_ir.Location.name(frame.f_code.co_name, childLoc=file_loc)
    res_or_list = opFunc(*args, **kwargs, loc=loc)
    return res_or_list
```

这样生成的 IR 在调试时能回溯到 Python 源码位置，对前端 lowering 很有用。

**`make_tile_type` 与 `constant`**——`make_tile_type`（[cuda_tile_ops.py:1404-1425](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/python/cuda_tile/dialects/cuda_tile_ops.py#L1404-L1425)）接受「元素类型包装类（如 `Float32`）或裸 MLIR 类型」加形状，转交给原生 `TileType.get`。它把 `shape` 既可以是 `int` 也可以是 `list` 的输入统一成 list，并校验非负：

```python
def make_tile_type(el_type, shape=None) -> TileType:
    shape = [shape] if isinstance(shape, int) else shape if shape is not None else []
    ...
    mlir_type = _get_mlir_type(el_type)   # 把 Float32 这种包装类剥成裸 MLIR 类型
    tile_type = TileType.get(shape, mlir_type)
    if tile_type is None:
        raise RuntimeError(...)            # 校验失败（None）→ 转成明确异常
    return tile_type
```

注意这里把原生层返回的 `None`（校验失败）**升级成了 `RuntimeError`**——和 4.2 节 `TileType.get` 返回 `None` 的行为不同，高层 API 选择「立即报错」而非「静默返回 None」，对用户更友好。

`constant`（[cuda_tile_ops.py:4640-4666](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/python/cuda_tile/dialects/cuda_tile_ops.py#L4640-L4666)）接收标量或（嵌套）Python list，先用 `_flatten_constants` 把嵌套 list 摊平成 1D 值列表并推断形状，若没给 `tile_type` 就从第一个值推断元素类型，最后构造 `_ConstantOp`（这是对生成类 `_cuda_tile.ConstantOp` 的手写特化 [cuda_tile_ops.py:1351-1362](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/python/cuda_tile/dialects/cuda_tile_ops.py#L1351-L1362)，负责把 Python 标量包成 `FloatAttr`/`IntegerAttr`）。

**`return_results` 的智能分派**——这是个很关键的内部工具（[cuda_tile_ops.py:1270-1315](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/python/cuda_tile/dialects/cuda_tile_ops.py#L1270-L1315)）。因为 cuda_tile 操作的结果可能是：单个 Tile、单个 Token、(Tile, Token) 二元组、或多结果元组。`return_results` 根据结果个数和类型自动包成对应的 Python 对象：

- 1 个结果且是 Token → 返回 `Token`
- 1 个结果且是 Tile → 返回 `Tile`
- ≥2 个结果，第一个 Tile 第二个 Token → 返回 `(Tile, Token)`

这正是 u5-l1 里 `load_ptr_tko` 返回 `(data_tile, token)` 这种设计的 Python 侧落地。

**MMA 配置注册表（Python 侧校验）**——`cuda_tile_ops.py` 还内置了一套 `MMAConfig` 注册表（[cuda_tile_ops.py:455-682](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/python/cuda_tile/dialects/cuda_tile_ops.py#L455-L682)），用「子类自动发现」枚举所有支持的 MMA 类型组合（`f16xf16->f32`、`e4m3xe4m3->f32` 等）。`mma()` 函数调用前会先用 `find_mma_config` 匹配（[cuda_tile_ops.py:2803](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/python/cuda_tile/dialects/cuda_tile_ops.py#L2803)），匹配不到就把所有受支持配置拼进错误信息抛 `TypeError`。这是「Python 提前校验」的典型，与 C++ verifier（u4-l5）形成对偶——Python 侧把错误前置到构造期，给出更友好的报错。

#### 4.3.4 代码实践

**实践目标**：用 `cuda_tile_ops.py` 高层 API 构造一段 `constant + add` 的 IR，并 dump 成 MLIR 文本。这是本讲规格里指定的实践任务。

**操作步骤**（示例代码）：

```python
# test_highlevel_api.py（示例代码）
from cuda_tile._mlir.ir import Context, Location, Module, InsertionPoint
from cuda_tile.dialects import cuda_tile as ct
from cuda_tile.dialects.cuda_tile import Float32, make_tile_type, constant

with Context() as ctx:
    ct.register_dialect(ctx)        # 高层 API 暴露的注册入口

    with Location.unknown(ctx):
        module = Module.create()
        with InsertionPoint(module.body):
            # 1) 用 make_tile_type 造 tile<4x4xf32>
            t = make_tile_type(Float32, [4, 4])

            # 2) 用 constant 造两个常量 tile
            a = constant([1.0] * 16, tile_type=t)
            b = constant([2.0] * 16, tile_type=t)

            # 3) 用 + 运算符（等价于 cuda_tile.add）做加法
            c = a + b

        # 4) dump 出 MLIR 文本
        print(module.operation)
```

**需要观察的现象**：

- `c = a + b` 这一行的 `__add__` 会触发 `add` → `_addf` → `_cuda_tile.AddFOp(...)`，最终在 IR 里生成一条 `cuda_tile.addf` 操作。
- 因为 `cuda_tile_op` 装饰器的作用，生成的操作会带上指向本 `.py` 文件行号的 `loc`（用 `--mlir-print-debuginfo` 可见）。
- 打印出的 MLIR 文本里能看到 `cuda_tile.constant`（两个）和 `cuda_tile.addf`（一个）。

**预期结果**：终端打印一段合法的 MLIR，包含两个 `constant` 与一个 `addf`，三者元素类型均为 `f32`、形状均为 `4x4`。完整可运行的样板可参考 [test/python/cuda_tile_public_bindings.py](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/python/cuda_tile_public_bindings.py)；具体打印文本待本地验证（取决于 InsertionPoint 与 module 包装的细节）。

#### 4.3.5 小练习与答案

**练习 1**：在 `cuda_tile_ops.py` 里，为什么 `a + b`（`a`、`b` 是 float tile）最终生成的是 `addf` 而不是 `addi`？

**参考答案**：`Tile.__add__` 调的是统一的 `add(self, rhs)` 函数，该函数用 `isinstance(lhs.element_type, _ods_ir.FloatType)` 判断元素类型，浮点走 `_addf`（生成 `AddFOp`），整数走 `_addi`（生成 `AddIOp`），指针走 `_offset`。所以同一个 `+` 运算符会按元素类型自动分派到不同的具体操作。

**练习 2**：`return_results` 为什么要区分「单结果 Tile」「单结果 Token」「(Tile, Token) 元组」三种返回形态？

**参考答案**：因为 cuda_tile 的操作结果结构不统一。纯计算操作（如 `addf`）只返回一个 Tile；`make_token` 只返回一个 Token；而 token-ordered 访存操作（如 `load_ptr_tko`，u5-l1）同时返回「数据 Tile + 顺序 Token」。`return_results` 根据结果个数和类型智能包装，让 Python 侧调用者拿到形态自然的结果对象，而不必每次手动拆 `op.results`。

**练习 3**：`mma()` 在构造操作前为什么要先调 `find_mma_config`？

**参考答案**：这是 Python 侧的提前校验。`find_mma_config` 在 `MMAConfig` 子类注册表里查「lhs×rhs→acc 的类型组合」是否受支持；若不支持，立即抛 `TypeError` 并把所有受支持配置列进错误信息，给出比 C++ verifier 更早、更友好的诊断。它与 C++ 侧 `verifyMmaShapes`（u4-l5）形成纵深防御。

## 5. 综合实践

把本讲三个最小模块串起来，完成一个「**纯 Python 端到端构造 + 优化 + 序列化**」的小任务：

1. **注册**：用高层 API `ct.register_dialect(ctx)` 把方言装进 Context（对应 4.1 的 SiteInitializer 自动注册 + 4.2 的 `register_dialect`）。
2. **构造**：用 `make_tile_type` + `constant` 造三个 `tile<4x4xf32>`：`a`、`b`、`c0`（累加器初值）。
3. **计算**：先用 `a * b`（生成 `mulf`），再与 `c0` 相加（生成 `addf`），观察分离形态。
4. **优化**：调用原生模块的 `applyTileIROptimizations`（4.2 节），把 `opts.fuse_fma` 设为 `True`，观察 `(a*b)+c0` 是否被融合成单条 `fma`（对应 u9-l1 的 FuseFMA）。
5. **序列化**：用原生模块的 `writeBytecode(file, module)`（4.2 节）把优化后的模块写成 `.tilebc`，再用 `cuda-tile-translate --cudatilebc-to-mlir` 反翻译回文本确认内容。

**验收标准**：

- 第 3 步的 IR 里能看到 `mulf` + `addf` 两条。
- 第 4 步开启 `fuse_fma` 后，`mulf` + `addf` 被替换成一条 `fma`（注意 u9-l1 讲过这是**非数值保持**变换，结果位级会变）。
- 第 5 步生成的 `.tilebc` 文件大小 > 0，反翻译后能看到 `fma` 操作。

> 这个任务把「SiteInitializer 钩子 → DialectCudaTile 绑定 → cuda_tile_ops.py 高层 API」三层串成一条完整数据流，并衔接了 u9-l1（FuseFMA）与 u7（字节码序列化）的内容。具体融合与序列化的输出文本待本地验证。

## 6. 本讲小结

- CUDA Tile 的 Python 绑定是「**三层金字塔**」：底层 C API（u10-l1）→ 中层 nanobind 原生模块（`_site_initialize_1` + `_cuda_tile`）→ 上层手写高层 API（`cuda_tile_ops.py`）。
- `SiteInitializer.cpp` 编译成 `_site_initialize_1`，靠 MLIR 的 site-initialization 机制在 import 时**自动调用** `register_dialects`；`register_passes` 不是约定钩子，必须用户**手动调**。
- `DialectCudaTile.cpp` 编译成 `_cuda_tile`，用 `mlir_type_subclass` / `mlir_attribute_subclass` 两个适配器把 C API 包成 Python 类；类型绑定遵循「isa 判定 + `get` 类方法 + `upcast_type` + 只读属性」四件套模板，且 `TileType.get` 用 `Checked` 版本做校验、失败返回 `None`。
- 函数绑定层把优化器（`applyTileIROptimizations`，位掩码 `CUDATILE_OPT_FLAG_FUSE_FMA`）和字节码写入（`writeBytecode`，内存 buffer + Python 文件对象）暴露给 Python；后者用 buffer 中转实现跨平台文件 I/O。
- `cuda_tile_ops.py` 用 `Tile`/`Pointer`/`TileView` 包装类 + 运算符重载把 IR 构造做成 NumPy 式写法；`cuda_tile_op` 装饰器自动生成 source location；`make_tile_type`/`constant` 提供高频辅助；`return_results` 智能分派 Tile/Token/元组返回。
- 高层 API 的校验策略比原生层更友好：`make_tile_type` 把 `None` 升级成 `RuntimeError`，`mma()` 用 Python 侧 `MMAConfig` 注册表把错误前置到构造期，与 C++ verifier 形成纵深防御。

## 7. 下一步学习建议

- **u10-l3（测试基础设施）**：本讲反复引用的 [test/python/cuda_tile_public_bindings.py](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/python/cuda_tile_public_bindings.py) 就是 lit/FileCheck 测试体系的一例，下一讲会系统讲解 `test/` 目录组织、`lit.cfg.py` 与 `%PYTHON` 替换、`check-cuda-tile` 目标。
- **回头精读 u9-l1 / u9-l3**：本讲综合实践里用到的 `applyTileIROptimizations` 与 FuseFMA 融合，其语义与「非数值保持」结论都来自这两讲，建议结合 Python 实践再读一遍 C++ Pass 实现。
- **尝试扩展绑定**：阅读 [python/cuda_tile/dialects/CudaTileOps.td](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/python/cuda_tile/dialects/CudaTileOps.td)，理解「新增一个操作时，Python 操作类是如何由 TableGen 自动生成的」（呼应 u2-l3 的代码生成主题），并思考为何部分功能（如 `assume` 的谓词）目前还需要在 `cuda_tile_ops.py` 里用 `Attribute.parse` 文本拼接的变通手法（见 [cuda_tile_ops.py:1968-1976](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/python/cuda_tile/dialects/cuda_tile_ops.py#L1968-L1976)）。
