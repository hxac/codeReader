# 转换 Pass 的 C++ 插件入口与骨架

## 1. 本讲目标

在 [u2-l3](u2-l3-compile-stages.md) 中我们看到，`make_tileir` 阶段会把 TTIR（triton 方言）lowering 成 `cuda_tile` 方言，这一步是整个 TileIR 后端编译链路的核心。本讲聚焦这一步的「骨架」：这批 MLIR 转换 Pass 是怎么从 C++ 走到 Python 的、它们的选项从哪里来、转换目标（legal/illegal）是怎么划定的。

学完本讲，你应当能够：

1. 说清 Python 里的 `tileir` 模块来自哪个 C++ 插件、经过哪几个环节被加载进 `libtriton`。
2. 看懂 `Passes.td` 里用 TableGen 定义一个 Pass 选项的标准写法，并把它和 C++ 字段、Python 参数一一对应。
3. 理解 `CudaTileConversionTarget` 是如何把「哪些方言合法、哪些非法」划分清楚的，以及 `CudaTileTypeConverter` 在其中扮演的角色。

本讲只讲「骨架与入口」，不逐个展开每个算子的 lowering 模式（那是 [u3-l2](u3-l2-core-conversion-pass.md) 的事）。

## 2. 前置知识

在继续前，请确认你理解以下几个 MLIR 基础概念。如果完全没接触过，也可以先读下去，遇到不懂再回查。

- **方言（Dialect）与操作（Op）**：MLIR 把 IR 按领域拆成多个方言，比如 `triton`、`arith`、`scf`、`cuda_tile`。每个方言定义一组 op（如 `tt.load`、`arith.addf`）。Triton 的 IR 是 `triton` 方言，TileIR 后端要把它转成 NVIDIA 的 `cuda_tile` 方言。
- **Pass 与 PassManager**：Pass 是对 IR 做一次遍历变换的单元（如「把所有 `tt.load` 换成 `cuda_tile.load`」），PassManager 负责按顺序驱动多个 Pass。Triton 在 Python 侧用 `ir.pass_manager(ctx)` 创建 PM，再用 `pm.addPass(...)` 挂载 pass。
- **DialectConversion 三件套**：MLIR 把「方言 A 转方言 B」标准化为一套框架：
  - `ConversionTarget`：声明哪些 op/方言是**合法**（legal，转换后允许保留）、哪些是**非法**（illegal，必须被改写掉）。
  - `TypeConverter`：声明类型如何随方言变化（如 `tensor<4xf32>` → `cuda_tile` 的 `TileType`）。
  - `ConversionPattern`：一条条具体的改写规则（一个 op → 另一个 op）。
  - 最后用 `applyFullConversion` 强制要求：转换结束后，IR 里不能残留任何「非法」的 op。
- **TableGen**：MLIR 用一种 `.td` 描述文件 + `mlir-tblgen` 代码生成器来自动生成 Pass 的样板代码（基类、选项字段、注册函数），避免手写大量重复代码。
- **pybind11**：C++ 用来把函数/类暴露给 Python 的绑定库。Triton 把所有 C++ 绑定编译成一个共享库 `libtriton`，Python 通过 `from triton._C.libtriton import ...` 导入。

## 3. 本讲源码地图

本讲涉及的关键文件及其职责：

| 文件 | 职责 |
|------|------|
| [third_party/tileir/triton_tileir.cc](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/triton_tileir.cc) | **pybind 插件入口**：把转换 Pass、`load_dialects`、`only_contain_legal_dialects`、`write_bytecode` 暴露给 Python 的 `tileir` 模块 |
| [third_party/tileir/include/TritonToTileIR/Passes.td](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/include/TritonToTileIR/Passes.td) | **TableGen 定义**：声明 `convert-triton-to-cuda-tile` Pass 及其全部选项 |
| [third_party/tileir/lib/TritonToTileIR/TritonToTileIRPass.cpp](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/TritonToTileIR/TritonToTileIRPass.cpp) | **Pass 实现**：`CudaTileConversionTarget`、所有 lowering pattern、`runOnOperation` 主流程 |
| [third_party/tileir/include/TritonToTileIR/Utils.h](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/include/TritonToTileIR/Utils.h) | `CudaTileTypeConverter` 声明、`ConvertGenericOp` 通用模板 |
| [third_party/tileir/lib/TritonToTileIR/Utils.cpp](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/TritonToTileIR/Utils.cpp) | `CudaTileTypeConverter` 构造函数（类型映射规则） |
| [third_party/tileir/backend/compiler.py](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/compiler.py) | Python 侧 `make_tileir`：调用 `tileir.passes.add_triton_to_cudatile(...)` |
| [python/src/main.cc](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/src/main.cc) | `libtriton` 主绑定：用宏把各后端插件注册成子模块 |

## 4. 核心概念与源码讲解

### 4.1 pybind 入口：`tileir` Python 模块从哪来

#### 4.1.1 概念说明

在 [u1-l3](u1-l3-repo-structure.md) 中我们说过，`tileir` 是一个 **in-tree（仓库内置）后端**：它的代码在 `third_party/tileir/` 下，构建时会被自动注册进 Triton。但 Python 代码里 `from triton._C.libtriton import ir, passes, tileir` 这一行（见 [compiler.py:6](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/compiler.py#L6)）能直接拿到 `tileir` 这个模块，说明它其实是 **C++ 编译出来的共享库里的一个 pybind 子模块**。

关键直觉是：**后端名字（`tileir`）一路贯穿「C++ 插件 → 绑定宏 → Python 子模块」**。Triton 用一套宏约定，让后端名 `tileir` 自动对应到 C++ 入口函数 `init_triton_tileir` 和 Python 子模块名 `tileir`。

#### 4.1.2 核心流程

从「装一个后端」到「Python 里能用 `tileir.passes.xxx`」，经过下面这条链：

```
third_party/tileir/  (目录名 = tileir)
        │  setup.py 扫描 third_party/，收集 in-tree 后端名
        ▼
setup.py  →  -DTRITON_CODEGEN_BACKENDS=tileir
        │  CMake 把后端名塞进一个宏
        ▼
CMakeLists.txt  →  add_compile_definitions(TRITON_BACKENDS_TUPLE=(tileir))
        │  +  add_triton_plugin(TritonTileIR triton_tileir.cc ...)  ← 仅用于链接
        ▼
python/src/main.cc  →  INIT_BACKEND(tileir)
        │  宏展开：init_triton_tileir(m.def_submodule("tileir"))
        ▼
third_party/tileir/triton_tileir.cc  →  init_triton_tileir()
        │  注册 tileir.passes.* / tileir.load_dialects / ...
        ▼
Python:  tileir.passes.add_triton_to_cudatile(pm, ...)
```

注意一个容易混淆的点：`add_triton_plugin(TritonTileIR ...)` 里的 `TritonTileIR` 只是 **CMake 对象库（object library）的名字，用于把 `triton_tileir.cc` 编译出来的目标文件链接进 `libtriton`**，它不影响 Python 里看到的名字。真正决定 Python 子模块名的是 `TRITON_CODEGEN_BACKENDS` 里的后端名 `tileir`。构建细节将在 [u4-l4](u4-l4-build-and-cuda-tile-deps.md) 详讲，这里只要记住这个「名字贯穿」的结论即可。

#### 4.1.3 源码精读

**① setup.py 把后端名传给 CMake。** [setup.py:284](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/setup.py#L284) 把所有 in-tree 后端的目录名拼成 `TRITON_CODEGEN_BACKENDS`（分号分隔）。因为存在 `third_party/tileir/`，所以 `tileir` 会出现在里面。

**② CMake 用后端名生成绑定宏。** [CMakeLists.txt:405-414](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/CMakeLists.txt#L405-L414) 把后端名拼成 `TRITON_BACKENDS_TUPLE=(tileir,...)` 并定义为编译期宏：

```cmake
string(JOIN "," TRITON_BACKENDS_TUPLE ${TRITON_CODEGEN_BACKENDS})
...
set(TRITON_BACKENDS_TUPLE "(${TRITON_BACKENDS_TUPLE})")
add_compile_definitions(TRITON_BACKENDS_TUPLE=${TRITON_BACKENDS_TUPLE})
```

而 `add_triton_plugin` 只负责把插件源文件登记为可链接对象（见 [CMakeLists.txt:265-268](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/CMakeLists.txt#L265-L268)），与子模块命名无关。

**③ main.cc 用宏把后端名展开成 init 调用。** [python/src/main.cc:35-37](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/src/main.cc#L35-L37) 定义了两个关键宏：

```cpp
#define DECLARE_BACKEND(name) void init_triton_##name(pybind11::module &&m);
#define INIT_BACKEND(name) init_triton_##name(m.def_submodule(#name));
```

把 `tileir` 代入 `INIT_BACKEND`，就得到 `init_triton_tileir(m.def_submodule("tileir"))`。它在 [main.cc:63](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/src/main.cc#L63) 通过 `FOR_EACH_P(INIT_BACKEND, TRITON_BACKENDS_TUPLE)` 对每个后端批量展开。这行是「魔法」所在：后端名 `tileir` 同时决定了函数名 `init_triton_tileir` 和子模块名 `"tileir"`。

**④ 插件入口函数实现。** [triton_tileir.cc:103-104](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/triton_tileir.cc#L103-L104) 定义这个被宏调用的函数：

```cpp
void init_triton_tileir(py::module &&m) {
  init_triton_to_cudatile_passes(m.def_submodule("passes"));
  m.def("load_dialects", ...);
  m.def("only_contain_legal_dialects", ...);
  m.def("write_bytecode", ...);
}
```

这里的 `m` 就是上面 `m.def_submodule("tileir")` 创建的模块。所以：

- `tileir.passes.*` ← `init_triton_to_cudatile_passes(m.def_submodule("passes"))`
- `tileir.load_dialects` / `tileir.only_contain_legal_dialects` / `tileir.write_bytecode` ← 直接挂在 `m` 上

**⑤ passes 子模块里挂了哪些函数。** [triton_tileir.cc:58-101](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/triton_tileir.cc#L58-L101) 的 `init_triton_to_cudatile_passes` 用一连串 `m.def(...)` 暴露了 `add_triton_to_cudatile`、`add_fma_fusion`、`add_loop_split`、`add_lift_tt_cf_to_scf`、`add_strip_debuginfo`、`add_assume_to_tileir`、`add_auto_gen_memtoken` 等。其中最重要的是 [triton_tileir.cc:62-67](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/triton_tileir.cc#L62-L67)：

```cpp
m.def("add_triton_to_cudatile", [](mlir::PassManager &pm, bool approx,
                                    bool ftz, int capability, int num_ctas,
                                    int simt_num_warps, int occupancy,
                                    std::optional<int> num_stages) {
  pm.addPass(mlir::triton::createConvertTritonToCudaTilePass(
      approx, ftz, capability, num_ctas, simt_num_warps, occupancy, num_stages));
});
```

它是一个「薄壳」：收 Python 传来的参数，转手调用 C++ 工厂函数 `createConvertTritonToCudaTilePass(...)` 构造真正的 Pass，再 `pm.addPass(...)` 挂到 PassManager 上。注意 lambda 把 PM 作为第一个参数按引用接收，其余参数顺序就是上一节 `Passes.td` 选项的顺序（详见 4.2）。

#### 4.1.4 代码实践

**实践目标**：亲手确认 `tileir` 模块的结构与加载来源。

**操作步骤**（前提：已按 [u1-l2](u1-l2-install-and-run.md) 装好本仓库）：

1. 启动 Python，设置 `ENABLE_TILE=1` 后导入 triton：

   ```python
   import os
   os.environ["ENABLE_TILE"] = "1"
   import triton
   from triton._C.libtriton import tileir
   ```

2. 用 `dir()` 查看 `tileir` 模块下有哪些名字：

   ```python
   print([n for n in dir(tileir) if not n.startswith("_")])
   print([n for n in dir(tileir.passes) if not n.startswith("_")])
   ```

**需要观察的现象**：`tileir` 下应出现 `load_dialects`、`only_contain_legal_dialects`、`write_bytecode`、`passes`；`tileir.passes` 下应出现 `add_triton_to_cudatile`、`add_fma_fusion`、`add_loop_split`、`add_lift_tt_cf_to_scf`、`add_strip_debuginfo`、`add_assume_to_tileir`、`add_auto_gen_memtoken`。

**预期结果**：这些名字与 [triton_tileir.cc:58-150](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/triton_tileir.cc#L58-L150) 里 `m.def(...)` 注册的完全一致。若环境不便运行，可只做源码核对（视为「源码阅读型实践」）。运行结果：待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：如果把后端目录从 `third_party/tileir/` 改名为 `third_party/foo/`（其它一切不变），Python 里导入的模块名会变成什么？C++ 入口函数名会变成什么？

**答案**：模块名变成 `foo`，C++ 入口函数变成 `init_triton_foo`，且 `triton_tileir.cc` 里的函数名也必须同步改为 `init_triton_foo` 才能被 `INIT_BACKEND(foo)` 正确链接——因为宏展开 `init_triton_##name` 是硬约定。

**练习 2**：为什么 `from triton._C.libtriton import tileir` 能直接拿到 `tileir`，而不是 `triton_tileir`？

**答案**：因为 Python 子模块名由 `INIT_BACKEND` 宏里的 `m.def_submodule(#name)` 决定，`#name` 就是后端名 `tileir`；`triton_tileir` 只是 `.cc` 源文件名和 C++ 函数名前缀，对 Python 不可见。

---

### 4.2 `Passes.td` 选项定义：TableGen 如何生成 Pass 选项

#### 4.2.1 概念说明

MLIR 不让你手写 Pass 的样板（基类、选项存储、命令行解析、注册名）。你只需写一个 `.td`（TableGen）文件描述「这个 Pass 叫什么、有哪些选项」，`mlir-tblgen` 就会生成一个 `Passes.h.inc` 头文件，里面有一个基类，把你的选项变成成员字段。你写的 C++ Pass 类继承这个基类，就能直接读到选项值。

对 `convert-triton-to-cuda-tile` 而言，选项就是那些在 [u2-l2](u2-l2-options-and-env-config.md) 讲过的编译旋钮（approx / ftz / capability / num_ctas / occupancy / num_stages 等）。

#### 4.2.2 核心流程

```
Passes.td  (人写)
   │  mlir-tblgen -gen-pass-decls
   ▼
Passes.h.inc  (自动生成：基类 ConvertTritonToCudaTileBase<...>，含选项字段)
   │  C++ 源文件 #define GEN_PASS_DEF_CONVERTTRITONTOCUDATILE
   │  然后 #include "TritonToTileIR/Passes.h.inc"
   ▼
ConvertTritonToCudaTile : impl::ConvertTritonToCudaTileBase<ConvertTritonToCudaTile>
   │  直接用 this->approxModifier 等字段读选项
   ▼
createConvertTritonToCudaTilePass(...)  (工厂函数，供 pybind 调用)
```

`.td` 里 `Option<"字段名","命令行名","类型","默认值","说明">`，`字段名` 就是生成的 C++ 成员变量名。

#### 4.2.3 源码精读

**① TableGen 定义。** [Passes.td:6-42](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/include/TritonToTileIR/Passes.td#L6-L42) 完整定义了这个 Pass：

```tablegen
def ConvertTritonToCudaTile : Pass<"convert-triton-to-cuda-tile", "mlir::ModuleOp"> {
    let summary = "Convert Triton to cuda_tile/triton dialect";
    let constructor = "mlir::triton::createConvertTritonToCudaTilePass()";
    let dependentDialects = ["mlir::arith::ArithDialect", ...];
    let options = [ ... ];
}
```

几个关键点：

- `Pass<"convert-triton-to-cuda-tile", "mlir::ModuleOp">`：第一项是 pass 的**注册名**（命令行/lit 测试里用 `-convert-triton-to-cuda-tile` 引用它，见 [u4-l1](u4-l1-opt-tool-and-lit-tests.md)），第二项是它操作的顶层 IR 类型 `mlir::ModuleOp`。
- `constructor` 指向无参工厂函数 `createConvertTritonToCudaTilePass()`（用于命令行工具），同时还存在一个带参重载（见 4.3）。
- `dependentDialects`：声明转换过程中会「产生」的方言，MLIR 会确保它们在 context 里被加载。
- [Passes.td:19-41](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/include/TritonToTileIR/Passes.td#L19-L41) 的 `options` 数组列出 7 个选项。

**② 7 个选项清单**（按 `.td` 出现顺序）：

| `.td` 字段名 | 命令行名 | 类型 | 默认值 | 含义 |
|---|---|---|---|---|
| `approxModifier` | `approx-modifier` | bool | false | 是否给支持的 op 加 approx（近似）修饰 |
| `flushToZeroModifier` | `flush-to-zero-modifier` | bool | false | 是否加 FTZ（flush-to-zero，刷零）修饰 |
| `computeCapability` | `compute-capability` | int | 100 | 目标算力（sm_100 = Blackwell） |
| `numCTAInCGA` | `num-cta-in-cga` | int | 1 | 一个 CGA（cluster）里有几个 CTA |
| `simtNumWarpsInCTA` | `num-warps-in-cta` | int | 4 | 一个 CTA 里有几个 SIMT warp |
| `occupancy` | `occupancy` | int | 1 | 一个 SM 上驻留几个 CTA |
| `numStages` | `num-stages` | int | ""(空) | 流水线级数 |

注意 `numStages` 的默认值是空字符串，对应一个特殊语义（未指定），在 C++ 侧用 `std::optional<int>` 表示。

**③ C++ 端「拉取」生成的基类。** [TritonToTileIRPass.cpp:22-27](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/TritonToTileIR/TritonToTileIRPass.cpp#L22-L27) 是标准套路：

```cpp
#define GEN_PASS_DEF_CONVERTTRITONTOCUDATILE
#include "TritonToTileIR/Passes.h.inc"
```

这个 `#define` + `#include` 让生成的基类（含上面 7 个字段作为成员）进入当前编译单元。随后 [TritonToTileIRPass.cpp:2776-2778](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/TritonToTileIR/TritonToTileIRPass.cpp#L2776-L2778) 的 Pass 类继承它：

```cpp
struct ConvertTritonToCudaTile
    : public ::mlir::triton::impl::ConvertTritonToCudaTileBase<
          ConvertTritonToCudaTile> { ... };
```

于是 `this->approxModifier`、`this->occupancy` 等「凭空」可用，无需自己声明。

#### 4.2.4 代码实践

**实践目标**：把 `Passes.td` 的 7 个选项、pybind lambda 的形参、Python 调用的实参三方对齐。

**操作步骤**（源码阅读型实践，无需运行）：

1. 打开 [Passes.td:19-41](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/include/TritonToTileIR/Passes.td#L19-L41)，记下 7 个选项及其顺序。
2. 打开 [triton_tileir.cc:62-67](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/triton_tileir.cc#L62-L67)，确认 lambda 形参顺序与之一致。
3. 打开 [compiler.py:305-314](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/compiler.py#L305-L314)，确认 Python 实参顺序。

**需要观察的现象**：三处的「顺序」完全一致，是一一对应的。

**预期结果**：下表是本实践的「答案」。`convert-triton-to-cuda-tile` 的全部选项，及其 Python 侧 `add_triton_to_cudatile` 对应参数：

| `.td` 选项（字段名） | pybind 形参 | Python 实参（compiler.py） |
|---|---|---|
| `approx-modifier` (`approxModifier`) | `approx` | `opt.enable_approx` |
| `flush-to-zero-modifier` (`flushToZeroModifier`) | `ftz` | `opt.enable_ftz` |
| `compute-capability` (`computeCapability`) | `capability` | `capability` |
| `num-cta-in-cga` (`numCTAInCGA`) | `num_ctas` | `metadata["num_ctas"]` |
| `num-warps-in-cta` (`simtNumWarpsInCTA`) | `simt_num_warps` | `metadata["num_warps"]` |
| `occupancy` (`occupancy`) | `occupancy` | `opt.occupancy` |
| `num-stages` (`numStages`) | `num_stages` | `metadata["num_stages"]` |

注意 `num_warps`/`num_ctas`/`num_stages` 来自 `metadata`，而 `enable_approx`/`enable_ftz`/`occupancy` 来自 `opt`（`TileIROptions`）——这正呼应了 [u2-l3](u2-l3-compile-stages.md) 讲的「有些旋钮走 metadata、有些走 opt」的分流。

#### 4.2.5 小练习与答案

**练习 1**：为什么 Pass 类里没有显式声明 `bool approxModifier;` 这个成员，却能用 `this->approxModifier`？

**答案**：因为它由 TableGen 生成的基类 `ConvertTritonToCudaTileBase` 提供。`.td` 里每个 `Option<...>` 的第一项 `approxModifier` 就成了基类的一个成员变量名。

**练习 2**：`numStages` 默认值是空字符串，C++ 侧用什么类型承接，为什么？

**答案**：用 `std::optional<int>`（见 [triton_tileir.cc:64](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/triton_tileir.cc#L64) 与 [TritonToTileIRPass.cpp:2783](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/TritonToTileIR/TritonToTileIRPass.cpp#L2783)）。因为 `num_stages` 是「可指定也可不指定」的，空默认值表示「未指定」，用 optional 能区分「用户没给」和「用户给了 0」。

---

### 4.3 转换目标骨架：`CudaTileConversionTarget` 与 TypeConverter

#### 4.3.1 概念说明

「转换」要成功，必须回答两个问题：**转换后哪些 op 是合法的？哪些 op 必须消失？** 这由 `ConversionTarget` 声明。同时，类型也得跟着变（`tensor<4xf32>` 在 triton 方言里和 cuda_tile 方言里是不同的类型表示），这由 `TypeConverter` 负责。

对 TileIR 后端，目标是「把 IR 里所有 `triton`/`arith`/`scf`/`cf`/`gpu`/`ub` 方言的 op，全部换成 `cuda_tile` 方言的 op」。`applyFullConversion` 会强制保证：转换结束后，任何一个被标记为「非法」的 op 都不能残留——如果有 op 没有对应的改写规则，转换直接失败。这正是 [u2-l3](u2-l3-compile-stages.md) 提到的「把失败前移」的机制。

#### 4.3.2 核心流程

`ConvertTritonToCudaTile::runOnOperation()`（Pass 主入口）大致做这些事：

```
1. 构造 CudaTileTypeConverter（定义类型映射规则）
2. 在 IR 顶部插入一个 cuda_tile::ModuleOp 容器，把原 IR 克隆进去
   （cuda_tile IR 必须包在 cuda_tile.module 里）
3. 预处理 Host TMA descriptor、axis 属性、num_stages
4. 预处理：展开 tt.map_elementwise（见 u3-l3）
5. 构造 CudaTileConversionTarget（声明 legal/illegal）
6. 注册全部 ConversionPattern（见 u3-l2）
7. applyFullConversion：强制把所有 illegal op 改写掉
8. 清理残留的 unrealized_conversion_cast，做一次合法性收尾
```

#### 4.3.3 源码精读

**① 合法/非法划分。** [TritonToTileIRPass.cpp:46-67](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/TritonToTileIR/TritonToTileIRPass.cpp#L46-L67) 是骨架的核心：

```cpp
class CudaTileConversionTarget : public ConversionTarget {
public:
  CudaTileConversionTarget(MLIRContext &context,
                           CudaTileTypeConverter &typeConverter)
      : ConversionTarget(context) {
    addLegalDialect<cuda_tile::CudaTileDialect>();              // 目标方言：合法
    addIllegalDialect<scf::SCFDialect, cf::ControlFlowDialect,  // 源方言：非法
                      mlir::gpu::GPUDialect, triton::TritonDialect,
                      ub::UBDialect>();
    addLegalOp<ub::PoisonOp>();
    addLegalOp<mlir::gpu::BarrierOp>();        // barrier 由后续 pass 处理
    addLegalOp<arith::IndexCastOp>();          // 暂未支持，先放行
    addLegalOp<UnrealizedConversionCastOp>();  // 转换期临时桥接
  }
};
```

逐行理解：

- `addLegalDialect<cuda_tile::CudaTileDialect>()`（[L52](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/TritonToTileIR/TritonToTileIRPass.cpp#L52)）：`cuda_tile` 方言是转换的**终点**，转换后允许保留。
- `addIllegalDialect<...>`（[L53-55](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/TritonToTileIR/TritonToTileIRPass.cpp#L53-L55)）：`scf`、`cf`、`gpu`、`triton`、`ub` 这几个方言的 op **必须被改写掉**。这就是「转换完整性」的硬约束——任何一个残留的 `tt.load` 都会让 full conversion 失败。
- 三个 `addLegalOp`（[L57-65](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/TritonToTileIR/TritonToTileIRPass.cpp#L57-L65)）是「例外白名单」，逐个 op 放行：
  - `ub::PoisonOp`：poison 值保留。
  - `gpu::BarrierOp`：注释明确「barrierOp 会在 `AutoGenMemoryTokenPass` 里被移除」，所以这里先放行，交给 [u3-l6](u3-l6-memory-token.md) 处理。
  - `arith::IndexCastOp`：注释 `TODO: support these arith/math ops in cuda_tile`，暂未支持，先放行不报错。
  - `UnrealizedConversionCastOp`：MLIR 方言转换的临时「桥接 op」，转换期允许出现，最后再清理。

**② 类型转换器。** `CudaTileTypeConverter` 声明在 [Utils.h:27-30](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/include/TritonToTileIR/Utils.h#L27-L30)，实现（构造函数）在 [Utils.cpp:396-466](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/TritonToTileIR/Utils.cpp#L396-L466)。它用一连串 `addConversion([](XType t){ return YType; })` 登记类型映射，要点有：

- `triton::TensorDescType` → `cuda_tile::PartitionViewType`（[Utils.cpp:399-431](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/TritonToTileIR/Utils.cpp#L399-L431)）：这是 [u2-l6](u2-l6-tma-tensor-descriptor.md) 讲的「host TMA 描述符降级」在类型层的体现。
- `FloatType`/`IntegerType` → 零维 `cuda_tile::TileType`（标量也用 TileType 表示，[Utils.cpp:435-438](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/TritonToTileIR/Utils.cpp#L435-L438)）。
- `triton::PointerType` → 元素为 `cuda_tile::PointerType` 的零维 TileType（[Utils.cpp:441-448](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/TritonToTileIR/Utils.cpp#L441-L448)）。
- `RankedTensorType` → 带形状的 `cuda_tile::TileType`（[Utils.cpp:456-465](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/TritonToTileIR/Utils.cpp#L456-L465)）。

直觉：**triton 的「张量」概念在 cuda_tile 里统一成了 `TileType`，标量被当成零维 tile**。

**③ 插入 cuda_tile.module 容器。** [TritonToTileIRPass.cpp:2803-2813](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/TritonToTileIR/TritonToTileIRPass.cpp#L2803-L2813)：cuda_tile 方言要求所有 op 必须位于一个 `cuda_tile::ModuleOp` 容器内，所以转换一开始先创建这个容器、把原 IR 克隆进去。这正是 [compiler.py:301-303](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/compiler.py#L301-L303) 注释说的「cuda-tile ir must under tileir_moduleOp」。这一步也为 [u2-l7](u2-l7-tileiras-invocation.md) 的 `write_bytecode` 定位嵌套 ModuleOp 铺路。

**④ pattern 注册与 full conversion。** [TritonToTileIRPass.cpp:2836-2849](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/TritonToTileIR/TritonToTileIRPass.cpp#L2836-L2849) 是把三件套拼到一起的地方：

```cpp
CudaTileConversionTarget target(*context, typeConverter);
RewritePatternSet patterns(context);
populateTTirToCudaTileConversionPatternsAndLegality(
    typeConverter, patterns, target, this->approxModifier, ...);

ConversionConfig config = ConversionConfig();
config.buildMaterializations = false;
if (failed(applyFullConversion(mod, target, std::move(patterns), config)))
  return signalPassFailure();
```

注意两点：

- `populateTTirToCudaTileConversionPatternsAndLegality`（[TritonToTileIRPass.cpp:2320-2447](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/TritonToTileIR/TritonToTileIRPass.cpp#L2320-L2447)）一次性把上百条改写规则（`ConvertLoadOp`、`ConvertStoreOp`、`ConvertDotOp`、`ConvertFuncOp`、大量 `ConvertGenericOp<...>` 等）注册进 `patterns`。这些规则就是 [u3-l2](u3-l2-core-conversion-pass.md) 的全部内容。
- `applyFullConversion`（注意是 **full** 不是 partial）：要求转换后**没有任何 illegal op 残留**。如果有 op 没规则可改，这里 `failed(...)` 为真，`signalPassFailure()` 让整个 pass 失败——这把「转换不彻底」的错误提前到了编译期。

**⑤ 工厂函数重载。** [TritonToTileIRPass.cpp:2874-2882](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/TritonToTileIR/TritonToTileIRPass.cpp#L2874-L2882) 是带参版 `createConvertTritonToCudaTilePass(...)`，它构造 Pass 时把 7 个参数赋给字段（[TritonToTileIRPass.cpp:2786-2797](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/TritonToTileIR/TritonToTileIRPass.cpp#L2786-L2797)）；无参版 [TritonToTileIRPass.cpp:2869-2872](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/TritonToTileIR/TritonToTileIRPass.cpp#L2869-L2872) 给命令行工具用（选项走命令行解析）。pybind 走的是带参版。

#### 4.3.4 代码实践

**实践目标**：通过 `only_contain_legal_dialects` 这个 Python 可调用函数，理解 full conversion 的「合法性」边界。

**操作步骤**（源码阅读型 + 可选运行）：

1. 阅读 [triton_tileir.cc:117-128](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/triton_tileir.cc#L117-L128) 的 `only_contain_legal_dialects`：它遍历模块里所有 op，只要发现一个既不是 `ModuleOp`、又不属于 `cuda_tile` 方言的 op，就返回 `false`。
2. 对照 [compiler.py:321-324](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/compiler.py#L321-L324)：转换后若返回 `false`，就抛 `RuntimeError("Triton ttir to tileir ir failed...")`。
3. 想一个问题：这与 `applyFullConversion` 的失败检测是否重复？

**需要观察的现象**：理论上 `applyFullConversion` 已经保证不残留 illegal op，这里又做一次「只允许 cuda_tile 方言」的二次校验。

**预期结果**：`only_contain_legal_dialects` 是一道**双保险**——`applyFullConversion` 关注「target 标记的 illegal op 没了」，但因为 target 里白名单放行了 `arith::IndexCastOp`、`UnrealizedConversionCastOp` 等（它们不是 cuda_tile 方言），full conversion 不会因它们失败。所以这道额外检查更像是一个面向「最终输出」的断言：除容器 `ModuleOp` 外，IR 里应该「只有 cuda_tile 方言」。运行验证：待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `gpu::BarrierOp` 被显式标记为 `addLegalOp`，而不是让它被改写掉？

**答案**：因为 barrier 不能在本 pass 里直接消除，它的语义要由后续的 `AutoGenMemoryTokenPass`（[u3-l6](u3-l6-memory-token.md)）转化为 memory token。所以这里先放行，留到后面的 pass 处理（代码注释也写明了）。

**练习 2**：假设你新增了一种 triton op `tt.foo`，但忘了给它写 `ConversionPattern`，会发生什么？

**答案**：`applyFullConversion` 发现 `tt.foo` 属于 `triton` 方言（illegal）却无规则可改，转换 `failed`，pass 调用 `signalPassFailure()` 失败，最终 Python 侧 `make_tileir` 抛错（或被 `only_contain_legal_dialects` 二次校验拦下）。这就是「把不支持算子的错误前移到编译期」的机制。

---

## 5. 综合实践

**任务**：画一张「参数从 Python 到 C++ 字段」的完整流转图，并解释「为什么改环境变量能触发重编译」。

**步骤**：

1. 从 [compiler.py:305-314](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/compiler.py#L305-L314) 出发，标注 7 个实参的来源（`opt` 还是 `metadata`）。
2. 顺着 [triton_tileir.cc:62-67](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/triton_tileir.cc#L62-L67) 的形参，到 [TritonToTileIRPass.cpp:2786-2797](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/TritonToTileIR/TritonToTileIRPass.cpp#L2786-L2797) 的字段赋值，再到 [TritonToTileIRPass.cpp:2838-2842](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/TritonToTileIR/TritonToTileIRPass.cpp#L2838-L2842) 把字段传进 pattern 注册函数。
3. 结合 [u2-l2](u2-l2-options-and-env-config.md) 的结论：`enable_ftz`/`enable_approx` 是 `@property`，实时读环境变量且计入 `hash()`。

**思考题（待本地验证你的理解）**：`TILEIR_ENABLE_FTZ=1` 改变的是 `opt.enable_ftz` 这个 Python 值，它最终如何变成 IR 上 op 的 `flush_to_zero` 修饰？提示：跟踪 `flushToZeroModifier` 这个布尔值在 [Utils.h:124-134](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/include/TritonToTileIR/Utils.h#L124-L134) 的 `ConvertGenericOp` 里如何被传给 `CudaTileOp::create(..., ftzModifier)`。

## 6. 本讲小结

- `tileir` Python 模块来自 C++ 插件 `TritonTileIR`（源文件 `triton_tileir.cc`），经 setup.py → `TRITON_CODEGEN_BACKENDS` → CMake 宏 → main.cc 的 `INIT_BACKEND` 宏，被注册为 `libtriton` 的 `tileir` 子模块；后端名 `tileir` 贯穿函数名（`init_triton_tileir`）与子模块名。
- `triton_tileir.cc` 通过 `m.def` 暴露 `tileir.passes.*`（一批 `add_xxx` 挂 pass 函数）以及 `load_dialects`/`only_contain_legal_dialects`/`write_bytecode`；`add_triton_to_cudatile` 只是个转手薄壳。
- `Passes.td` 用 TableGen 定义 `convert-triton-to-cuda-tile` 的 7 个选项（approx/ftz/capability/num_ctas/num_warps/occupancy/num_stages），`mlir-tblgen` 生成基类，Pass 类继承后直接用 `this->字段名` 读选项。
- 选项在 `.td` 字段名、pybind 形参、Python 实参三处一一对应、顺序一致；`num_warps/num_ctas/num_stages` 走 metadata，`enable_approx/enable_ftz/occupancy` 走 opt。
- `CudaTileConversionTarget` 把 `cuda_tile` 设为合法、`triton/scf/cf/gpu/ub` 设为非法，并对 `BarrierOp`/`IndexCastOp`/`UnrealizedConversionCastOp` 等开白名单；`applyFullConversion` 强制不残留 illegal op，把错误前移。
- `CudaTileTypeConverter` 把 triton 的张量/指针/标量/TensorDesc 统一映射成 `cuda_tile` 的 `TileType`/`PointerType`/`PartitionViewType`。

## 7. 下一步学习建议

本讲只搭好了「骨架与入口」。接下来：

- **[u3-l2 核心转换 convert-triton-to-cuda-tile](u3-l2-core-conversion-pass.md)**：深入 `populateTTirToCudaTileConversionPatternsAndLegality` 注册的上百条改写规则，看 `tt.load`/`tt.dot`/`scf.for` 等具体怎么变成 `cuda_tile` op。
- 想动手跑 pass：先读 [u4-l1](u4-l1-opt-tool-and-lit-tests.md)，用 `triton-cuda-tile-opt` 工具 + lit/FileCheck 在 IR 层面复现一次转换。
- 想理解构建细节（`add_triton_plugin`、cuda-tile 依赖链接）：见 [u4-l4](u4-l4-build-and-cuda-tile-deps.md)。
- 建议同步阅读源码：[triton_tileir.cc](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/triton_tileir.cc)（全文仅 150 行，适合通读）与 [Passes.td](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/include/TritonToTileIR/Passes.td)（仅 44 行）。
