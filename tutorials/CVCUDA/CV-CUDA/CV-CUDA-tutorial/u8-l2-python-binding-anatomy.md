# Python 绑定解剖：如何把一个 C++ 算子暴露给 Python

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清一个算子的 Python 绑定文件（如 `OpFlip.cpp`）要经过哪「三站接线」才会出现在 `cvcuda` 模块里。
2. 解释 `Main.cpp` 中导出注册的两条顺序约束：基类先于派生类、被签名引用的 pybind11 类型先于使用它的方法注册。
3. 独立写出标准的「四连函数」绑定骨架：`Xxx` / `XxxInto`（Tensor 批）与 `XxxVarShape` / `XxxVarShapeInto`（变长批），并理解每种入口里流兜底、`ResourceGuard` 锁模式、`guard.run()` 的固定套路。
4. 解释 `CreateOperator` 如何借助 `PyOperator` 模板与缓存键（构造参数）复用算子对象，以及 `NvtxTrace` 如何在不改变函数签名的前提下给每个绑定入口自动打上 NVTX 埋点。
5. 完成综合实践：为假想算子 `invert3` 写出一个完整的绑定文件并接入三站接线。

本讲是第八单元「二次开发」的第二讲，承接 [u8-l1](u8-l1-make-new-operator.md) 的 mkop 脚手架：mkop 生成的 11 个文件里就有 `PythonBinding.cpp`，本讲就是读懂并手写这个文件。

## 2. 前置知识

### 2.1 pybind11 的最小心智模型

pybind11 是 C++ 与 CPython 之间的桥梁。核心三件事：

- **模块入口**：`PYBIND11_MODULE(模块名, m)` 宏展开成一个 `PyInit_模块名` 函数，Python `import` 时执行它，`m` 就是模块对象。
- **函数导出**：`m.def("python名", 函数指针, 参数标注..., 文档字符串)` 把一个 C++ 函数注册为模块级函数。同一个 `"python名"` 可以多次 `m.def`，pybind11 会按**参数类型**做重载决议——这正是 `flip` 既能吃 Tensor 又能吃 ImageBatchVarShape 的原理。
- **类型导出**：`py::class_<T, 基类...>(m, "Python名")` 把 C++ 类注册为 Python 类型，形成 Python 层的继承链。

参数标注 `"src"_a`（来自 `pybind11::literals`）声明关键字参数名；`py::kw_only()` 之后的参数只能用关键字传递；`"stream"_a = nullptr` 给默认值，对应 C++ 形参 `std::optional<Stream>`（Python 传 `None` 即「未指定」）。

### 2.2 一个容易迷惑的语法点：`Flip->submit()` 到底调的是谁

Python 绑定里写的 `Flip->submit(...)` **不是** C++ 算子类 `cvcuda::Flip` 的方法——`cvcuda::Flip` 只有 `operator()`。这里的 `Flip` 是 `CreateOperator<cvcuda::Flip>(0)` 返回的 `std::shared_ptr<PyOperator<...>>`，`submit` 是包装类 `PyOperator` 的模板方法，它内部转发 `m_op(...)`，即调用 C++ 算子类的函数调用运算符。本讲 4.4 会展开这条链。

### 2.3 回顾需要的旧知识

- **四层架构**（[u5-l1](u5-l1-op-four-layers.md)）：Python 绑定层 → C API → C++ priv 实现 → CUDA kernel。本讲只涉及最上面一层的内部构造。
- **两种变体与对象缓存**（[u3-l3](u3-l3-allocating-vs-into.md)、[u4-l2](u4-l2-object-cache.md)）：`flip` 隐式分配输出并查缓存，`flip_into` 写入调用者的 `dst`；本讲从源码看到这套语义是如何落地成代码的。
- **流模型与 ResourceGuard**（[u4-l1](u4-l1-stream-model.md)、[u4-l3](u4-l3-multi-stream-thread-gpu.md)）：算子全部异步提交到流；跨流安全靠 `ResourceGuard` 记账。
- **NVTX 埋点**（[u7-l4](u7-l4-nvtx-and-profiling.md)）：Python 侧 `cvcuda.flip` 区间包住 C++ 侧 `cvcudaFlipSubmit` 区间，两层嵌套。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [python/mod_cvcuda/Main.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/Main.cpp) | 模块唯一入口 `PYBIND11_MODULE(_cvcuda, m)`：先导出 nvcv 类型，再按顺序调用每个算子的 `ExportOpXxx(m)` |
| [python/mod_cvcuda/operators/Operators.hpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/Operators.hpp) | 三站接线的第二站：所有 `ExportOpXxx` 声明 + `PyOperator` 模板 + `CreateOperator`（含算子对象缓存） |
| [python/mod_cvcuda/operators/OpFlip.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpFlip.cpp) | 本讲的解剖标本：Flip 的四连函数与 4 个 `m.def` 注册 |
| [python/mod_cvcuda/NvtxRange.hpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/NvtxRange.hpp) | `NvtxTrace` 签名保持包装器与 `NvtxRange` RAII 类 |
| [python/common/PyUtil.hpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/common/PyUtil.hpp) | 绑定层公共工具：动态补方法、dtype 转换、float4 参数提取 |
| [python/mod_cvcuda/operators/VarShapeUtils.hpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/VarShapeUtils.hpp) | 变长批输出构造工具 `CreateSameShapeImageBatch` |
| [python/mod_cvcuda/include/nvcv/python/ResourceGuard.hpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/include/nvcv/python/ResourceGuard.hpp) | `ResourceGuard` 的锁模式登记与 `run()` 同步屏障 |
| [python/mod_cvcuda/CMakeLists.txt](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/CMakeLists.txt) | 三站接线的第三站：`SOURCES` 清单登记每个 `Op*.cpp` |

## 4. 核心概念与源码讲解

### 4.1 绑定层全景：一个算子绑定文件的「三站接线」

#### 4.1.1 概念说明

CV-CUDA 的 Python 扩展模块编译产物叫 `_cvcuda`（一个 `.so`），Python 包 `cvcuda` 在 `import` 时加载它。把一个 C++ 算子变成 `cvcuda.xxx` 函数，需要改动**三个互相独立的地方**，缺一不可：

1. **第一站·定义**：新建 `python/mod_cvcuda/operators/OpXxx.cpp`，实现入口函数并写 `void ExportOpXxx(py::module &m)`。
2. **第二站·声明**：在 `python/mod_cvcuda/operators/Operators.hpp` 里加一行 `void ExportOpXxx(py::module &m);` 声明。注意 `Operators.hpp` 第 18 行 `#include "../NvtxRange.hpp"`，这就是为什么每个 `OpXxx.cpp` 不必显式包含 `NvtxRange.hpp` 就能用 `NvtxTrace`。
3. **第三站·编译与调用**：把源文件加入 `python/mod_cvcuda/CMakeLists.txt` 的 `SOURCES` 清单；在 `Main.cpp` 的算子导出区调用 `ExportOpXxx(m);`。

漏掉第二站会链接失败（`Main.cpp` 找不到符号），漏掉第三站的 CMake 部分则该翻译单元根本不参与编译。

#### 4.1.2 核心流程

以 Flip 为例的接线全景：

```text
python/mod_cvcuda/operators/OpFlip.cpp   ← 实现四连函数 + ExportOpFlip
        │  (声明于)
        ▼
python/mod_cvcuda/operators/Operators.hpp ← void ExportOpFlip(py::module &m);   [L75]
        │  (调用于)                        ← 同时提供 PyOperator/CreateOperator/NvtxTrace
        ▼
python/mod_cvcuda/Main.cpp               ← ExportOpFlip(m);                      [L202]
        │  (编译于)
        ▼
python/mod_cvcuda/CMakeLists.txt         ← operators/OpFlip.cpp                  [L146]
        │
        ▼
_cvcuda.so  ──import──▶  cvcuda.flip / cvcuda.flip_into
```

#### 4.1.3 源码精读

CMake 目标定义。模块名 `_cvcuda`、装进 `cvcuda` 包，都由这个调用决定：[python/mod_cvcuda/CMakeLists.txt:L49-L53](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/CMakeLists.txt#L49-L53) —— 这里声明 `cvcuda_python_add_module` 目标与 `SOURCES` 清单的开头，后面约一百行逐个登记算子源文件。

Flip 源文件在清单中的登记行：[python/mod_cvcuda/CMakeLists.txt:L146](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/CMakeLists.txt#L146) —— `operators/OpFlip.cpp` 出现在模块源列表里，这是它能被编译链接的前提。

第二站声明：[python/mod_cvcuda/operators/Operators.hpp:L75](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/Operators.hpp#L75) —— `void ExportOpFlip(py::module &m);` 与其他 60 多个算子的声明排在一起，供 `Main.cpp` 调用。

`Operators.hpp` 顶部把 `NvtxRange.hpp` 带给所有算子绑定文件：[python/mod_cvcuda/operators/Operators.hpp:L18](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/Operators.hpp#L18) —— 因此 `OpFlip.cpp` 的 include 列表里看不到 `NvtxRange.hpp`，却能用 `NvtxTrace`。

第三站调用：[python/mod_cvcuda/Main.cpp:L202](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/Main.cpp#L202) —— `ExportOpFlip(m);` 位于 `cvcudapy` 命名空间块内、非算子辅助类型导出之后。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：用检索验证「三站接线」的存在与完整性。
2. **操作步骤**：
   ```bash
   rg -n "ExportOpFlip" python/            # 应命中 3 个文件
   rg -n "OpFlip.cpp" python/mod_cvcuda/CMakeLists.txt
   ```
3. **需要观察的现象**：`ExportOpFlip` 恰好出现在 `OpFlip.cpp`（定义）、`Operators.hpp`（声明）、`Main.cpp`（调用）三个文件中。
4. **预期结果**：三处各一条命中；任何一处缺失对应一种典型接线遗漏。换一个算子名（如 `ExportOpCLAHE`）重复一次，结论应一致。

#### 4.1.5 小练习与答案

**练习 1**：新写了一个 `OpFoo.cpp` 但忘了加进 `CMakeLists.txt`，构建会报什么错？为什么？
**答案**：不会有编译错误——该文件根本没被编译。链接 `Main.cpp` 时如果已加声明并在 `Main.cpp` 调用了 `ExportOpFoo`，会报**未定义符号**（undefined reference to `ExportOpFoo`）；若两处也没加，则构建完全成功但 Python 里没有 `cvcuda.foo`，属于静默遗漏。

**练习 2**：为什么 `OpFlip.cpp` 没有包含 `NvtxRange.hpp` 却能用 `NvtxTrace`？
**答案**：`OpFlip.cpp` 第一站就包含了 `Operators.hpp`，而 `Operators.hpp` 第 18 行包含了 `../NvtxRange.hpp`，传递可用。

---

### 4.2 Main.cpp：PYBIND11_MODULE 与导出顺序约束

#### 4.2.1 概念说明

`Main.cpp` 是整个 `_cvcuda` 模块的唯一入口。它做的事按顺序是：设置模块名与版本 → 开 `internal` 白盒子模块 → 导出 nvcv 核心类型 → 导出 CV-CUDA 辅助类型（枚举、OSD 元素）→ 逐个调用算子导出函数。

其中最有工程价值的是源码里**明文写下的两条顺序约束**：

- **(a) 基类先于派生类**：pybind11 的 Python 层继承关系要求父类型已注册。继承链是 `Resource`、`CacheItem` → `Container` → `Tensor` / `Image` / `ImageBatchVarShape` 等。
- **(b) 方法签名引用的类型先注册**：若某方法的参数或返回值是另一个 pybind11 类型，那个类型必须先注册，否则 pybind11 在文档字符串、stubs 与 `repr` 里会打印原始 C++ 类型名（如 `nvcvpy::priv::Stream`），破坏用户体验。

#### 4.2.2 核心流程

```text
PYBIND11_MODULE(_cvcuda, m)
 ├─ m.__name__ = "cvcuda"; m.__version__ = CVCUDA_VERSION_STRING
 ├─ def_submodule("internal")            ← 白盒观测，见 u7-l2
 ├─ 支撑类型：ColorSpec/ImageFormat/DataType/Rect/ThreadScope/TensorLayout
 ├─ 核心实体（顺序敏感）：
 │    ExportCAPI → Resource::Export → Cache::Export → Container::Export
 │    → ExternalBuffer::Export
 │    → Image::Export（先于 Tensor，因 as_tensor 取 Image&）
 │    → Tensor::Export → TensorBatch::Export → ImageBatchVarShape::Export
 │    → Stream::Export
 │    → Resource::ExportStreamMethods（Stream 已知后补挂流方法）
 ├─ def_submodule("_test")               ← 故障注入钩子
 └─ cvcudapy 块：
      ├─ 非算子辅助类型（各枚举、OsdElement…）
      └─ ExportOpXxx(m) × 61            ← 算子函数注册（无顺序依赖）
```

注意最后一层：**算子导出之间没有顺序约束**（都是模块级函数，不构成继承链），有约束的是类型区。所以新增算子只需在算子区追加一行。

#### 4.2.3 源码精读

模块入口与身份设置：[python/mod_cvcuda/Main.cpp:L62-L65](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/Main.cpp#L62-L65) —— `PYBIND11_MODULE(_cvcuda, m)` 是 `.so` 的入口；把 `__name__` 改写成 `"cvcuda"`，让 docstring 与 repr 里显示 `cvcuda.flip` 而不是 `_cvcuda.flip`。

两条顺序约束的原文注释：[python/mod_cvcuda/Main.cpp:L96-L100](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/Main.cpp#L96-L100) —— 注释明确写出 (a) 基类先于派生类、(b) 被签名引用的 pybind11 类型先注册，否则 stubs 与 repr 会出现裸 C++ 类型名。

约束 (b) 的一个具体实例：[python/mod_cvcuda/Main.cpp:L93](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/Main.cpp#L93) —— `ExportTensorLayout` 必须先于 `Image::Export`，因为 `Image::cpu/cuda` 的签名引用 `TensorLayout`。

约束 (a) 的落地次序：[python/mod_cvcuda/Main.cpp:L101-L105](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/Main.cpp#L101-L105) —— `Resource`（无流方法）→ `Cache`（导出 CacheItem）→ `Container`（依赖前两者）→ `ExternalBuffer`，严格按继承自底向上注册。

对象区的两个先后细节：[python/mod_cvcuda/Main.cpp:L107-L112](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/Main.cpp#L107-L112) —— `Image` 先于 `Tensor`（`as_tensor` 接受 `Image&`）；`Stream` 最后注册。

「延迟补挂」手法：[python/mod_cvcuda/Main.cpp:L114-L116](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/Main.cpp#L114-L116) —— `Resource` 的方法里引用了 `Stream`，而 `Stream` 当时还没注册，于是先注册 `Resource` 主体，等 `Stream` 注册完再调用 `Resource::ExportStreamMethods(m)` 补挂这些方法。这是解决「循环引用」的标准解法：拆成两次导出。

pybind 注册时声明的 Python 继承：[python/mod_cvcuda/nvcv/Tensor.cpp:L526-L530](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Tensor.cpp#L526-L530) —— `py::class_<Tensor, std::shared_ptr<Tensor>, Container>` 明确把 `Container` 列为 py 基类，这正是「基类必须先注册」的因果另一端。

#### 4.2.4 代码实践

1. **实践目标**：在运行中的 Python 里亲眼看到注册顺序约束的结果。
2. **操作步骤**：
   ```bash
   pip install cvcuda-cu12        # 或已装好
   python -c "import cvcuda; print(cvcuda.Tensor.__mro__)"
   python -c "import cvcuda; print(cvcuda.__name__, cvcuda.__version__)"
   ```
3. **需要观察的现象**：`__mro__`（方法解析顺序）里 `Tensor` 之后跟着 `Container` 及其祖先类型，最后是 `object`；模块名显示 `cvcuda`。
4. **预期结果**：MRO 链条印证「基类先注册」不是注释里的教条，而是 Python 层可见的真实继承。若手头无 GPU/wheel 环境，此步**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `ExportOpFlip(m)` 误挪到 `Container::Export` 之前会发生什么？
**答案**：不会出错。算子导出的是模块级函数，不依赖类型继承顺序；`OpFlip.cpp` 里用到的 `Tensor`/`Stream` 等 py 类型在函数**被调用**时才做参数转换，注册阶段只要 C++ 类型完整即可。顺序约束只约束类型区（`Resource`→`Container`→`Tensor` 这条链），这就是把算子导出集中放在末尾的原因——它们天然无序。

**练习 2**：`Resource::ExportStreamMethods` 为什么要单独存在，而不是把流方法直接写进 `Resource::Export`？
**答案**：`Resource` 的方法签名引用了 `Stream`，而 `Stream` 的注册又必须在 `Container` 体系之后（`Stream` 也是 CacheItem 派生），存在先有鸡还是先有蛋的循环。拆成「主体先注册、流方法延迟补挂」两步，让两个约束都被满足。

---

### 4.3 OpFlip.cpp 精读：四连函数与 ResourceGuard 锁模式

#### 4.3.1 概念说明

`OpFlip.cpp` 是所有算子绑定文件的模板。它的骨架是「**四连函数**」：同一算子按「输入类型 × 输出归属」组合出四个 C++ 入口，再通过 `m.def` 注册成两个 Python 名字（每个名字两个重载）：

| C++ 函数 | Python 名 | 输入 | 输出 | 语义 |
|----------|-----------|------|------|------|
| `Flip` | `cvcuda.flip` | `Tensor` | 新建 | allocating，Tensor 批 |
| `FlipInto` | `cvcuda.flip_into` | `Tensor` | 调用者 `dst` | in-place 目标，Tensor 批 |
| `FlipVarShape` | `cvcuda.flip` | `ImageBatchVarShape` | 新建 | allocating，变长批 |
| `FlipVarShapeInto` | `cvcuda.flip_into` | `ImageBatchVarShape` | 调用者 `dst` | in-place 目标，变长批 |

每个函数体都遵循同一个五步套路：**流兜底 → 取算子 → 建守卫 → 登记锁 → run 提交**。另一个要点是变长批入口中，可逐图变化的参数（这里是 `flipCode`）从标量 `int32_t` 升级为 `Tensor`（每图一个值），呼应 [u3-l1](u3-l1-resize-and-flip.md) 的结论。

#### 4.3.2 核心流程

以 `FlipInto` 为例的固定套路（伪代码）：

```text
函数 Into(output, input, 参数..., pstream):
    若 pstream 为空:                    # Python 未传 stream=
        pstream = Stream::Current()     # 兜底为当前流（每线程流栈顶）
    op = CreateOperator<cvcuda::Flip>(构造参数...)   # 查缓存或新建
    ResourceGuard guard(*pstream)       # 绑定到流
    guard.add(READ,  {input})           # 只读资源
    guard.add(WRITE, {output})          # 独占写资源
    guard.add(NONE,  {*op})             # 算子对象本身：只保活，不设屏障
    guard.run(λ: op->submit(stream句柄, input, output, 参数...))  # 屏障在前，kernel 在后
    return output                       # _into 变体原样返回 dst
```

`guard.run()` 的时序含义（详见 [u4-l1](u4-l1-stream-model.md)）：

```text
消费流: [等待各生产流的事件(SubmitSyncOnly)] → [kernel] → [登记资源保活(HoldResources)]
                ↑ 必须在 kernel 之前，因为 cudaStreamWaitEvent 只约束其后入队的命令
```

#### 4.3.3 源码精读

**文件头与依赖**：[python/mod_cvcuda/operators/OpFlip.cpp:L18-L30](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpFlip.cpp#L18-L30) —— 依次包含 `Operators.hpp`（拿到类型别名、`CreateOperator`、`NvtxTrace`）、`VarShapeUtils.hpp`（变长批输出构造）、C++ 算子头 `cvcuda/OpFlip.hpp`，以及 `ResourceGuard`/`Stream`/`Tensor` 等 python 类型头。

**`FlipInto`：整个仓库最典型的算子垫片（shim）**：[python/mod_cvcuda/operators/OpFlip.cpp:L35-L53](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpFlip.cpp#L35-L53) —— 五步套路逐一可见：L37-40 流兜底（`pstream` 为空则取 `Stream::Current()`）；L42 `CreateOperator<cvcuda::Flip>(0)` 取算子（0 是 `maxVarShapeBatchSize`，同时是缓存键的一部分）；L44-47 建守卫并按 READ/WRITE/NONE 三档登记；L49-50 `guard.run` 内调用 `Flip->submit(...)`；L52 原样返回 `output`。

**allocating 变体只是 `_into` 的薄包装**：[python/mod_cvcuda/operators/OpFlip.cpp:L55-L60](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpFlip.cpp#L55-L60) —— `Flip` 先 `Tensor::Create(input.shape(), input.dtype())` 分配输出（`shape()` 返回的 `TensorShape` 自带 layout，所以不用再传布局），再委托 `FlipInto`。`Tensor::Create` 背后正是 [u4-l2](u4-l2-object-cache.md) 讲过的对象缓存。

**变长批版：参数张量化 + 输出批重建**：[python/mod_cvcuda/operators/OpFlip.cpp:L62-L81](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpFlip.cpp#L62-L81) —— 三处关键差异：形参 `flipCode` 变成 `Tensor &`（每图一个翻转码）；L73 `flipCode` 与输入一起登记为 READ（它也是被 kernel 读取的 GPU 数据）；提交调用的是 C++ 类的另一个 `operator()` 重载（变长批版本）。

**变长批 allocating 的输出构造**：[python/mod_cvcuda/operators/OpFlip.cpp:L83-L88](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpFlip.cpp#L83-L88) —— 用 `CreateSameShapeImageBatch(input)` 逐图重建同尺寸同格式的输出批，这正是 [u3-l3](u3-l3-allocating-vs-into.md) 说「变长批 allocating 开销随批内图数线性增长」的代码出处。

**`CreateSameShapeImageBatch` 的实现**：[python/mod_cvcuda/operators/VarShapeUtils.hpp:L48-L63](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/VarShapeUtils.hpp#L48-L63) —— 循环 `pushBackImage(Image::Create(input[i].size(), input[i].format()))`，逐图拷贝尺寸与格式。

**注册区：同名重载 + 关键字参数 + docstring**：[python/mod_cvcuda/operators/OpFlip.cpp:L92-L111](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpFlip.cpp#L92-L111) —— 第一个 `m.def("flip", ...)`：函数包在 `NvtxTrace("cvcuda.flip", &Flip)` 里；`"src"_a, "flipCode"_a` 是位置或关键字参数；`py::kw_only()` 之后的 `"stream"_a = nullptr` 只能关键字传且默认 `None`；`R"pbdoc(...)pbdoc"` 是 Google 风格文档字符串，Sphinx 文档由此生成。

**同名第二个重载（变长批）**：[python/mod_cvcuda/operators/OpFlip.cpp:L131-L147](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpFlip.cpp#L131-L147) —— 同样注册成 `"flip"`，但指向 `&FlipVarShape`。Python 调用 `cvcuda.flip(x, 1)` 时 pybind11 按 `x` 的类型（Tensor 或 ImageBatchVarShape）决议到对应 C++ 函数，用户无感知。

**锁模式的取值**：[python/mod_cvcuda/include/nvcv/python/LockMode.hpp:L23-L29](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/include/nvcv/python/LockMode.hpp#L23-L29) —— `READ`/`WRITE` 是两个比特，`READWRITE` 是二者按位或，`NONE` 为 0。

**`run()` 为什么必须包住提交**：[python/mod_cvcuda/include/nvcv/python/ResourceGuard.hpp:L96-L126](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/include/nvcv/python/ResourceGuard.hpp#L96-L126) —— 注释说得直白：`cudaStreamWaitEvent` 只约束**在其之后**入队的命令，所以同步屏障（`Resources_SubmitSyncOnly`，L107）必须先于可调用对象执行（L121）；保活（`Stream_HoldResources`，L143）在其后。若把提交写在守卫 `run` 之外、屏障落在 kernel 之后，就保护不到它。

**更简的样板：`OpInvert.cpp`**：[python/mod_cvcuda/operators/OpInvert.cpp:L26-L47](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpInvert.cpp#L26-L47) —— 无额外参数的一元逐像素算子把「创建/守卫/提交」公共体抽进了 `UnaryElementwiseOp.hpp` 模板，四个入口只剩一行转发。写新绑定时若算子形态类似，优先套用这种模板化路线。

#### 4.3.4 代码实践

1. **实践目标**：从 Python 侧反向验证绑定层声明的签名与文档。
2. **操作步骤**：
   ```bash
   python - <<'EOF'
   import cvcuda, inspect
   print(cvcuda.flip.__doc__.split('Args:')[0])
   # 尝试关键字-only 约束：
   try:
       cvcuda.flip(None, 1, None)     # 第三个参数位置传法
   except TypeError as e:
       print("TypeError:", e)
   EOF
   ```
3. **需要观察的现象**：docstring 与 `OpFlip.cpp` 中 `R"pbdoc(...)` 完全一致；对 `stream` 用位置传参抛出 `TypeError`，提示不能作为位置参数。
4. **预期结果**：证明 `py::kw_only()` 生效、docstring 原样暴露给 Python。若无 wheel 环境，改为对照阅读 `m.def` 参数列表，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`FlipInto` 里为什么输入是 `READ`、输出是 `WRITE`、算子对象却是 `NONE`？
**答案**：锁模式描述的是**提交本算子时对资源的访问方式**，用于 ResourceGuard 生成跨流依赖与保活：输入只被 kernel 读取，登记 READ，使其上未完成的生产者工作先于本 kernel；输出被写入，登记 WRITE，独占该资源；算子对象本身没有数据竞争（它内部句柄只被提交使用），登记 NONE 仅仅为了把它的生命周期延长到流上的工作完成，不插入任何屏障。

**练习 2**：变长批入口为什么把 `flipCode` 也加进 READ 锁？如果忘加会有什么后果？
**答案**：变长批版 `flipCode` 是一个 Tensor（每图一个值），kernel 会从它读数据，因此它是与输入同等的 GPU 数据资源。忘加则守卫不会为它插入「等待生产者流」的事件：若用户在另一条流上刚刚填充该张量且未同步，本算子可能读到未就绪数据，产生偶发且难复现的错乱。

**练习 3**：`cvcuda.flip_into` 的返回值是什么？为什么要返回而不是返回 `None`？
**答案**：返回原样的 `dst`。这样 `dst = cvcuda.flip_into(dst, src, 1)` 与 `dst = cvcuda.flip(src, 1)` 在代码形态上对称，也允许链式书写；元数据契约测试（[u7-l2](u7-l2-python-tests.md)）正是断言「`_into` 返回的对象就是传入的 dst」。

---

### 4.4 CreateOperator 与 PyOperator：算子对象的缓存包装

#### 4.4.1 概念说明

[u4-l2](u4-l2-object-cache.md) 讲过 Tensor/ImageBatch 的对象缓存；算子对象走的是**同一套缓存基础设施**，入口就是绑定层随处可见的 `CreateOperator<cvcuda::Flip>(0)`。它做了两件事：

1. **包装**：把 C++ 算子类 `cvcuda::Flip` 包进 `PyOperator` 模板——一个继承 `nvcvpy::Container` 的缓存友好外壳，持有「缓存键 + 算子实例」。
2. **复用**：以「算子类型 + 构造参数」为键查 `nvcvpy::Cache`，命中直接复用，未命中才构造并登记。

为什么值得缓存？C++ 算子对象构造要跨 C ABI 调 `cvcudaFlipCreate`（分配句柄、可能预分配变长批缓冲），销毁也要走 destroy。稳态管线中每帧都调用同一算子，若无缓存，每帧都要建毁一次。

#### 4.4.2 核心流程

```text
CreateOperator<cvcuda::Flip>(0)
    └─ CreateOperatorEx<PyOperator<Flip, void(int32_t)>>(0)
         ├─ Key key{0}                        # 键 = 算子类型(在 IKey 里) + 构造参数元组
         ├─ vcont = nvcvpy::Cache::fetch(key) # 一级匹配：类型+参数
         ├─ 空 → make_shared<PyOP>(0)         # 构造：m_key(0), m_op(0)
         │        └─ nvcvpy::Cache::add(*op)  # 登记进缓存
         └─ 非空 → PyOP::fetch(vcont)         # 二级挑选：默认取第一个
                    └─ dynamic_pointer_cast<PyOP>

调用期：
Flip->submit(stream, in, out, code)   # PyOperator::submit
    └─ m_op(stream, in, out, code)    # cvcuda::Flip::operator()
```

键的匹配语义是**哈希 + 逐参数相等**：`doGetHash` 对构造参数元组做组合哈希，`doIsCompatible` 要求所有参数相等。算子类型本身参与在 `IKey` 层。因此 `CreateOperator<cvcuda::Flip>(0)` 与 `CreateOperator<cvcuda::Flip>(32)` 是两个不同的缓存条目（后者预留了更大的变长批容量）。

#### 4.4.3 源码精读

**PyOperator 模板声明与职责注释**：[python/mod_cvcuda/operators/Operators.hpp:L116-L124](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/Operators.hpp#L116-L124) —— 注释点明它是「通用 python 侧算子类」，模板参数是算子类型与构造签名；NOSONAR 注释说明所有通用包装共享同一缓存层级。

**缓存键 Key**：[python/mod_cvcuda/operators/Operators.hpp:L127-L152](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/Operators.hpp#L127-L152) —— `doGetHash`（L140-143）组合哈希构造参数；`doIsCompatible`（L145-149）逐参数相等比较。注释特别提醒：带内部负载（payload）的算子可能需要特化 Key，因为「构造参数相等」未必等于「可复用」。这与 [u5-l6](u5-l6-computer-vision-analysis-ops.md) 提到的「大算子服务小请求 vs 严格相等」两种缓存键语义相呼应。

**submit 转发**：[python/mod_cvcuda/operators/Operators.hpp:L160-L164](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/Operators.hpp#L160-L164) —— 这就是 2.2 节那个「语法疑惑」的答案：`Flip->submit(...)` 调的是这里，它转发给 `m_op(...)` 即 `cvcuda::Flip::operator()`。

**二级挑选 fetch**：[python/mod_cvcuda/operators/Operators.hpp:L176-L184](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/Operators.hpp#L176-L184) —— 注释解释了两级匹配：`Cache::fetch` 已按类型+参数筛过一遍，`PyOP::fetch` 在 survivors 里再挑一个，通用实现「取第一个」。

**CreateOperatorEx 的查-建-登记三段**：[python/mod_cvcuda/operators/Operators.hpp:L192-L224](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/Operators.hpp#L192-L224) —— L205 `Cache::fetch(key)` 查；空则 L211 `make_shared` 构造、L214 `Cache::add` 登记；非空则 L220 `dynamic_pointer_cast` 还原具体类型。

**CreateOperator 便捷包装**：[python/mod_cvcuda/operators/Operators.hpp:L226-L230](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/Operators.hpp#L226-L230) —— 把 `PyOperator<OP, void(CTOR_ARGS...)>` 的模板推导集中在一处，调用侧只写 `CreateOperator<cvcuda::Flip>(0)`。

**作为对照，C++ 侧算子类长什么样**：[src/cvcuda/include/cvcuda/OpFlip.hpp:L43-L50](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/OpFlip.hpp#L43-L50) —— 构造参数 `maxVarShapeBatchSize` 就是绑定层传的 `0`；两个 `operator()` 重载分别对应 Tensor 批与变长批提交。绑定层包装的正是这个类。

#### 4.4.4 代码实践

1. **实践目标**：观察算子对象的缓存复用（不写代码，跑官方样例 + 计数）。
2. **操作步骤**：
   ```bash
   python - <<'EOF'
   import cvcuda
   from cvcuda.internal import cache # 观测接口，名称以实际版本为准（见 u7-l2）
   src = cvcuda.Tensor((1, 64, 64, 3), cvcuda.Type.U8, cvcuda.TensorLayout.NHWC)
   for _ in range(5):
       out = cvcuda.flip(src, 1)   # 反复调用同一算子
   # 观察 internal 缓存计数是否只增 1 次 Flip 相关条目
   EOF
   ```
3. **需要观察的现象**：循环 5 次后，算子相关缓存条目数远小于调用次数（理想为 1）。
4. **预期结果**：印证 `CreateOperator` 的查-建-登记逻辑。`cvcuda.internal` 的具体接口名随版本可能变化，**待本地验证**（可参考 [u7-l2](u7-l2-python-tests.md) 中 `test_cache.py` 的用法）。

#### 4.4.5 小练习与答案

**练习 1**：`PyOperator::Key` 的 `doIsCompatible` 用「所有构造参数相等」判定兼容。对 `Flip` 这种构造参数只有一个 `maxVarShapeBatchSize` 的算子，这样安全吗？什么算子需要特化？
**答案**：对 Flip 安全——两个 `Flip(0)` 实例完全等价，无内部状态残留（每次提交参数都显式传入）。需要特化的是「带内部负载」的算子：例如构造时按 `maxShape` 预分配了大缓冲的算子，可以放宽兼容判定让大实例服务小请求（用更宽松的 `doIsCompatible`，如 `capacity >= 请求`），否则缓存命中率低。代价是必须保证大实例对小请求的语义正确。

**练习 2**：`CreateOperatorEx` 里 `Cache::fetch` 与 `PyOP::fetch` 为什么是两级？一级不够吗？
**答案**：一级 `Cache::fetch(key)` 是通用基础设施，按「类型+键相等」返回**所有**匹配条目（可能多个）；二级 `PyOP::fetch` 把「在同键条目中挑哪一个」的决定权交还算子作者——通用实现取第一个，特化实现可按负载状态挑最合适的。分离让缓存框架不必理解每种算子的复用策略。

---

### 4.5 NvtxTrace 签名保持包装与 PyUtil 公共工具

#### 4.5.1 概念说明

绑定层最后一块拼图是两个「包裹器」：

- **`NvtxTrace`**：一个模板函数，吃进「名字 + 函数指针」，吐出一个 lambda。lambda 的参数列表精确镜像原函数（`Args...` 而非转发引用），因此 pybind11 内省到的签名不变——`m.def` 处的参数标注、默认值、docstring 全部照常生效。lambda 体内先 `NvtxRange range(name)` 推入 NVTX 区间，再转发调用，析构时弹出。效果：每个 Python 算子调用在 Nsight Systems 时间线上留下 `cvcuda.flip` 区间，嵌套包住 C++ 侧 `cvcudaFlipSubmit` 区间（[u7-l4](u7-l4-nvtx-and-profiling.md) 的两层时间线即来源于此）。
- **`PyUtil.hpp`**：绑定层公共工具箱——给已注册的 pybind11 类**动态补方法**（`DefClassMethod` / `DefClassStaticMethod`）、清理回调注册、对象全名获取、PEP 3118 之外的 numpy typestr 兼容转换（`ToDType`），以及给带 `float4` 参数的算子绑定用的数组转换助手。

#### 4.5.2 核心流程

```text
m.def("flip", NvtxTrace("cvcuda.flip", &Flip), "src"_a, ...)
                 │
                 ▼ 编译期生成闭包，签名 = R(Args...)
Python 调用 cvcuda.flip(src, 1)
    └─ 闭包执行: NvtxRange range("cvcuda.flip")   # nvtxRangePushA
                 return Flip(static_cast<Args&&>(args)...)
                 # range 析构 → nvtxRangePop
```

关键设计点（源码注释原文的含义）：

- lambda 形参用 `Args...`（值语义复制签名）而不是 `Args&&...`（转发引用会折叠成万能引用、丢失精确签名），pybind11 才能继续内省出原始参数类型；
- 转发处用 `static_cast<Args &&>(args)...` 复刻 `std::forward` 的值类别转换，且当 `Args` 本身是引用类型时依然正确；
- 捕获的 `name` 只存指针，因此**必须是静态存储期**的字符串字面量。

#### 4.5.3 源码精读

**NvtxRange RAII**：[python/mod_cvcuda/NvtxRange.hpp:L29-L46](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/NvtxRange.hpp#L29-L46) —— 构造 `nvtxRangePushA`、析构 `nvtxRangePop`，拷贝移动全删；头注释说明它镜像 C++ 核心侧的 `src/cvcuda/priv/Nvtx.hpp`，因 Python 模块不能包含私有核心头而复制了一份。

**NvtxTrace 自由函数包装器**：[python/mod_cvcuda/NvtxRange.hpp:L55-L67](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/NvtxRange.hpp#L55-L67) —— 上面流程的完整落点；注释明确「返回的闭包保持 fn 的精确参数签名，m.def 处的标注与默认值继续适用」以及「name 必须静态存储期」。

**成员函数重载**：[python/mod_cvcuda/NvtxRange.hpp:L74-L84](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/NvtxRange.hpp#L74-L84) —— 给 `Tensor.cuda`、`Image.cpu` 这类 `cls.def` 注册的 const 成员函数用：闭包第一个参数接实例，正是 pybind11 把自由可调用对象绑成实例方法的方式。

**动态补方法**：[python/common/PyUtil.hpp:L34-L43](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/common/PyUtil.hpp#L34-L43) —— `DefClassMethod` 用 `py::type::of<T>()` 找到**已经注册过**的 pybind11 类型，按 `py::class_::def` 的等价参数手工构造 `cpp_function` 再挂上去。用途：类型已注册完毕后（比如 4.2 的延迟补挂场景）再补方法，不必回到 `py::class_` 声明处。紧随其后的 [L51-L55](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/common/PyUtil.hpp#L51-L55) 是静态方法版本。

**dtype 转换兼容层**：[python/common/PyUtil.hpp:L61-L70](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/common/PyUtil.hpp#L61-L70) —— 注释说明 pybind11 v2.9.1 处理不了 PEP 3118 之外的 numpy typestr，`ToDType` 统一兜底，服务于 buffer/数组到 dtype 的转换。

**float4 参数助手**：[python/common/PyUtil.hpp:L74-L81](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/common/PyUtil.hpp#L74-L81) —— `cvcudapy` 命名空间里的 `pyarray`（强制 C 连续 + 强制转换的 `array_t<float>`）与 `GetFloat4FromPyArray`：让 Python 传一个长度 4 的 list/array 就能变成 CUDA 的 `float4`（OSD 颜色等场景）。

#### 4.5.4 代码实践

1. **实践目标**：亲眼看到 `NvtxTrace` 产生的 Python 层 NVTX 区间。
2. **操作步骤**（需要 GPU 与 Nsight Systems，命令细节见 [u7-l4](u7-l4-nvtx-and-profiling.md)）：
   ```bash
   nsys profile -o /tmp/flip_trace --force-overwrite true \
       python -c "import cvcuda; t=cvcuda.Tensor((1,64,64,3),cvcuda.Type.U8,'NHWC'); cvcuda.flip(t,1)"
   nsys stats --report nvtx_gputrace /tmp/flip_trace.nsys-rep   # 或用 GUI 查看
   ```
3. **需要观察的现象**：时间线上出现名为 `cvcuda.flip` 的 NVTX 区间，且其内部嵌套着 C++ 侧 `cvcudaFlipSubmit` 区间（两层嵌套）。
4. **预期结果**：两层区间之差即绑定层开销（[u7-l4](u7-l4-nvtx-and-profiling.md) 的结论）。无 nsys/GPU 环境则改为源码阅读：在 `OpFlip.cpp` 中数一数 `NvtxTrace` 出现的次数（应为 4，与 `m.def` 一一对应），**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：`NvtxTrace` 的 lambda 为什么形参写 `Args... args` 再 `static_cast<Args &&>(args)...`，而不是直接 `Args&&... args` 加 `std::forward`？
**答案**：若写 `Args&&...`（配合模板推导时是转发引用），推导出的 `Args` 会退化为裸类型，pybind11 内省到的签名就不再是原函数的精确签名（引用性丢失），`m.def` 处的参数标注、默认值匹配可能失效。按值镜像参数类型 + `static_cast<Args&&>` 复刻转发，既保签名又保值类别——源码注释（L61-65）点明的正是这一点。

**练习 2**：如果把 `NvtxTrace("cvcuda.flip", &Flip)` 改成传一个 `std::string` 临时对象构造的名字会怎样？
**答案**：闭包只捕获 `const char*` 指针，临时 `std::string` 析构后指针悬空，NVTX 区间名将读到垃圾内存。所以源码注释要求 `name` 具有静态存储期——字符串字面量天然满足。

**练习 3**：`DefClassMethod` 解决了 pybind11 的什么限制？
**答案**：pybind11 的 `py::class_` 句柄在 `Export` 函数返回后就丢了，官方 API 不支持「事后给已注册类型加方法」。`DefClassMethod` 通过 `py::type::of<T>()` 拿到运行期类型对象，手工按 `class_::def` 的内部做法（`py::method_adaptor` + `py::is_method` + sibling 链）构造并挂载 `cpp_function`，从而支持延迟补挂（如 4.2 的 `Resource::ExportStreamMethods` 类场景）。

---

## 5. 综合实践

**任务**：为假想算子 `invert3`（把三通道图逐通道取反，即 `out = 255 - in`，语义上等价于对 RGB 数据应用现有的 Invert）编写完整的 Python 绑定，并接入三站接线。这是 [u8-l1](u8-l1-make-new-operator.md) mkop 流程中 `PythonBinding.cpp` 那一步的手工演练。

> 说明：仓库中不存在 `cvcuda/OpInvert3.hpp`。为了让绑定代码在语义上可落地，本实践的提交体直接复用现有 C++ 类 `cvcuda::Invert`（它恰好实现逐元素取反）；若走正规流程新增核心算子，应先用 mkop 生成 C++ 侧骨架。下面所有新代码均为**示例代码（练习产物，仓库中不存在）**。

### 步骤 1：创建 `python/mod_cvcuda/operators/OpInvert3.cpp`

对照 4.3 的五步套路，实现规格要求的三个入口（`Invert3` / `Invert3Into` / `Invert3VarShape`；真实算子通常还有第四个 `Invert3VarShapeInto`，作为选做）：

```cpp
// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0
// （示例代码：练习产物，仓库中不存在）

#include "Operators.hpp"
#include "VarShapeUtils.hpp"

#include <cvcuda/OpInvert.hpp>              // 复用现有 C++ 类 cvcuda::Invert
#include <nvcv/python/ImageBatchVarShape.hpp>
#include <nvcv/python/ResourceGuard.hpp>
#include <nvcv/python/Stream.hpp>
#include <nvcv/python/Tensor.hpp>

namespace cvcudapy {

namespace {

Tensor Invert3Into(Tensor &output, Tensor &input, std::optional<Stream> pstream)
{
    if (!pstream)
    {
        pstream = Stream::Current();                     // ① 流兜底
    }

    auto invert3 = CreateOperator<cvcuda::Invert>();     // ② 查缓存或新建算子

    ResourceGuard guard(*pstream);                       // ③ 绑定流的守卫
    guard.add(LockMode::LOCK_MODE_READ, {input});        // ④a 输入只读
    guard.add(LockMode::LOCK_MODE_WRITE, {output});      // ④b 输出独占写
    guard.add(LockMode::LOCK_MODE_NONE, {*invert3});     // ④c 算子只保活

    guard.run([&invert3, &pstream, &input, &output]()    // ⑤ 屏障在前、kernel 在后
              { invert3->submit(pstream->cudaHandle(), input, output); });

    return output;
}

Tensor Invert3(Tensor &input, std::optional<Stream> pstream)
{
    Tensor output = Tensor::Create(input.shape(), input.dtype());  // allocating：分配输出

    return Invert3Into(output, input, pstream);                    // 委托 _into
}

ImageBatchVarShape Invert3VarShape(ImageBatchVarShape &input, std::optional<Stream> pstream)
{
    if (!pstream)
    {
        pstream = Stream::Current();
    }

    auto invert3 = CreateOperator<cvcuda::Invert>();

    ImageBatchVarShape output = CreateSameShapeImageBatch(input);  // 变长批：逐图重建输出

    ResourceGuard guard(*pstream);
    guard.add(LockMode::LOCK_MODE_READ, {input});
    guard.add(LockMode::LOCK_MODE_WRITE, {output});
    guard.add(LockMode::LOCK_MODE_NONE, {*invert3});

    guard.run([&invert3, &pstream, &input, &output]()
              { invert3->submit(pstream->cudaHandle(), input, output); });

    return output;
}

} // namespace

void ExportOpInvert3(py::module &m)
{
    using namespace pybind11::literals;

    m.def("invert3", NvtxTrace("cvcuda.invert3", &Invert3), "src"_a, py::kw_only(), "stream"_a = nullptr,
          R"pbdoc(
        Executes the Invert3 operation (per-channel inverse for 3-channel images) on the given cuda stream.

        Args:
            src (cvcuda.Tensor): Input tensor containing one or more 3-channel images.
            stream (cvcuda.Stream, optional): CUDA Stream on which to perform the operation.

        Returns:
            cvcuda.Tensor: The output tensor (same shape, dtype, and layout as src).
    )pbdoc");

    m.def("invert3_into", NvtxTrace("cvcuda.invert3_into", &Invert3Into), "dst"_a, "src"_a, py::kw_only(),
          "stream"_a = nullptr,
          R"pbdoc(
        Executes the Invert3 operation on the given cuda stream.

        Args:
            dst (cvcuda.Tensor): Output tensor to store the result of the operation.
            src (cvcuda.Tensor): Input tensor containing one or more 3-channel images.
            stream (cvcuda.Stream, optional): CUDA Stream on which to perform the operation.

        Returns:
            cvcuda.Tensor: The output tensor (same as dst).
    )pbdoc");

    m.def("invert3", NvtxTrace("cvcuda.invert3", &Invert3VarShape), "src"_a, py::kw_only(), "stream"_a = nullptr,
          R"pbdoc(
        Executes the Invert3 operation on the given cuda stream.

        Args:
            src (cvcuda.ImageBatchVarShape): Input image batch containing one or more 3-channel images.
            stream (cvcuda.Stream, optional): CUDA Stream on which to perform the operation.

        Returns:
            cvcuda.ImageBatchVarShape: The output image batch (same formats and sizes as src).
    )pbdoc");
}

} // namespace cvcudapy
```

注意与 `OpFlip.cpp` 的三处差异：`cvcuda::Invert` 构造无参，所以 `CreateOperator<cvcuda::Invert>()` 不传实参（缓存键为空元组）；无 `flipCode` 这类额外参数，守卫 READ 列表只有 `input`；`invert3->submit(...)` 经 `PyOperator::submit` 转发到 `cvcuda::Invert::operator()`（Tensor 批与变长批两个重载自动匹配）。

### 步骤 2：接入第二站（声明）

在 [python/mod_cvcuda/operators/Operators.hpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/Operators.hpp) 的声明区（L54-L114 之间任意合适位置，建议按字母序或紧跟相关算子）追加一行：

```cpp
void ExportOpInvert3(py::module &m);   // 示例代码
```

### 步骤 3：接入第三站（编译登记 + 调用）

在 [python/mod_cvcuda/CMakeLists.txt](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/CMakeLists.txt) 的 `SOURCES` 清单（如 L146 `operators/OpFlip.cpp` 附近）加入 `operators/OpInvert3.cpp`；在 [python/mod_cvcuda/Main.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/Main.cpp) 的算子导出区（L202 `ExportOpFlip(m);` 附近）加入 `ExportOpInvert3(m);`。

### 步骤 4：验证

- 有 CUDA 工具链时：`cmake --preset dev-py && cmake --build --preset dev-py`，然后
  ```bash
  python -c "import cvcuda; t=cvcuda.Tensor((1,8,8,3),cvcuda.Type.U8,'NHWC'); \
             r=cvcuda.invert3(t); print((255 - t.cpu().numpy()).astype('uint8').sum(), r.cpu().numpy().sum())"
  ```
  两个 sum 应相等（CPU 上用 numpy 复算黄金参考，呼应 [u7-l1](u7-l1-cpp-system-tests.md) 的「金标」思想）。
- 无构建条件时：至少对照检查表逐项核对（下表）。运行结果**待本地验证**。

### 检查表（自查）

| 检查项 | 依据 |
|--------|------|
| SPDX 头为 2026 单年 | 仓库不变量（[AGENTS.md](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/AGENTS.md)） |
| 入口函数位于匿名命名空间 | `OpFlip.cpp` L34 |
| `_into` 原样返回 `output` | `OpFlip.cpp` L52 |
| allocating 不重复实现提交逻辑 | `OpFlip.cpp` L55-L60 |
| 锁模式 READ/WRITE/NONE 齐全 | `OpFlip.cpp` L45-L47 |
| `stream` 参数 `py::kw_only()` 且默认 `nullptr` | `OpFlip.cpp` L96 |
| 每个 `m.def` 都有 `R"pbdoc(...)pbdoc"` | `OpFlip.cpp` L96-L165 |
| 三站接线齐全 | 4.1 节 |

## 6. 本讲小结

- 一个算子的 Python 绑定要过**三站**：`OpXxx.cpp` 定义 → `Operators.hpp` 声明 → `CMakeLists.txt` 编译登记 + `Main.cpp` 调用，任何一站遗漏分别是链接错误或静默缺失。
- `Main.cpp` 的两条顺序约束只作用于**类型区**：基类先于派生类（Resource → CacheItem → Container → Tensor/Image）、被签名引用的 pybind11 类型先注册；循环依赖用「主体 + 延迟补挂方法」拆解；算子导出之间无序。
- 绑定入口的标准骨架是**四连函数**（allocating / `_into` × Tensor 批 / 变长批），每个函数五步套路：流兜底 → `CreateOperator` → `ResourceGuard` → 三档锁登记 → `guard.run` 提交；变长批中逐图可变参数升级为张量并计入 READ 锁。
- `Flip->submit(...)` 实为 `PyOperator::submit` → C++ 算子类的 `operator()`；`CreateOperator` 以「算子类型 + 构造参数」为缓存键复用算子对象，`PyOperator::fetch` 提供二级挑选的特化点。
- `NvtxTrace` 用「按值镜像参数 + `static_cast<Args&&>`」保持签名，让每个绑定入口自动获得 NVTX 区间且不影响 pybind11 内省；`PyUtil.hpp` 补齐动态挂方法、dtype 兼容与 float4 参数等公共需求。

## 7. 下一步学习建议

- 下一讲 [u8-l3](u8-l3-workspace-and-stream-cache.md)：绑定层里另一个关键缓存——`WorkspaceCache` 如何按流隔离复用算子临时内存，与本讲的算子对象缓存、[u4-l2](u4-l2-object-cache.md) 的张量缓存构成三层缓存全景。
- 想看「有状态 + 参数异构」的绑定特例，读 [python/mod_cvcuda/OsdElement.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/OsdElement.cpp) 与 [python/mod_cvcuda/operators/OpOSD.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpOSD.cpp)，对照 [u5-l4](u5-l4-osd-case-study.md)。
- 想看模板化彻底的绑定，读 `python/mod_cvcuda/operators/OpInvert.cpp` 引用的 `UnaryElementwiseOp.hpp`，思考哪些新算子适合走模板、哪些必须手写四连函数。
- 建议同步阅读 [python/mod_cvcuda/include/nvcv/python/ResourceGuard.hpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/include/nvcv/python/ResourceGuard.hpp) 全文，重点是 `run()` 与 `commit()` 的异常安全设计（quarantine 路径），它回答了「提交失败后资源为什么不释放」。
