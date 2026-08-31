# 算子四层结构：C API → C++ 类 → priv 实现 → kernel

## 1. 本讲目标

前四个单元里，我们一直把算子当作「黑盒函数」调用：`cvcuda.flip(src, 1)` 进去，一张镜像图出来。从本讲起，我们打开这个黑盒。

学完本讲，你应当能够：

1. **跟踪完整链路**：说出 `cvcuda.flip(tensor, 1)` 从 Python 函数一路穿过 C API、公开 C++ 类、priv 私有实现，最终到达 CUDA kernel 的每一站，以及每一站所在的文件与函数名。
2. **理解句柄机制**：解释 `detail::OperatorHandle` 的 RAII 设计、`NVCVOperatorHandle` 这个不透明指针（opaque pointer）在 C 边界两侧如何被创建、传递与销毁。
3. **举一反三定位源码**：对仓库中任意一个算子（如 GaussianBlur、CvtColor），凭命名规律独立找到它这四层对应的全部文件。

本讲是整个第五单元的钥匙：一旦链路走通，后面读任何算子都只是「沿着同一条路换风景」。

## 2. 前置知识

本讲默认你已读过前四单元，特别是以下结论（忘记的话建议先回顾）：

- **u3-l3（两种变体）**：`cvcuda.flip` 是 allocating 变体（库创建输出张量并可能命中 Python 对象缓存），`cvcuda.flip_into` 是 `_into` 变体（写入调用者预分配的 `dst`）。源码上前者只是后者的薄包装。
- **u4-l1（Stream 模型）**：一切算子都异步提交到 CUDA 流上；未显式传 `stream=` 时由 `Stream::Current()` 兜底；流参数会一路穿透到 kernel 启动的第四个配置参数。
- **u2-l4（DLPack 互操作）**：`as_tensor` 只接受 CUDA 可访问的 buffer，CPU 数组会被拒绝。本讲会看到这个限制在 priv 层的「第二道关卡」。
- **u1-l1（分层架构）**：仓库四层为 Python 绑定 → C API → C++ 私有实现 → CUDA kernel，后三层在 `src/cvcuda`，数据类型层 nvcv 贯穿各层。

本讲新增的术语，先用人话解释一遍：

| 术语 | 通俗解释 |
|------|----------|
| **C ABI** | 应用二进制接口。C 语言的函数名、参数传递方式在不同编译器间最稳定，所以库的公开边界用 C 函数暴露，C++/Python/Rust 谁都能调 |
| **不透明指针（opaque handle）** | `typedef struct NVCVOperator *NVCVOperatorHandle;`——只声明结构体标签、不暴露结构体定义，调用者只能拿地址、不能解引用，内部布局对外不可见 |
| **RAII** | 资源获取即初始化。把资源生命周期绑定到对象生命周期：构造时获取、析构时释放。C++ 靠它替代手写的 `free`/`close` |
| **`extern "C"`** | 告诉 C++ 编译器别对这些函数名做名字修饰（name mangling），保证链接时符号就是 `cvcudaFlipSubmit` 这样的裸名 |
| **`dynamic_cast`** | 运行时把基类指针安全地转回派生类指针，类型不符返回 `nullptr`。C API 边界靠它把不透明句柄还原成具体算子对象 |
| **pybind11** | 把 C++ 函数/类包装成 Python 可调用对象的库，`m.def("flip", ...)` 就是注册动作 |
| **NVTX** | NVIDIA Tools Extension。给代码打命名区间标记，Nsight Systems 时间线上会显示这些区间，用于性能分析 |

## 3. 本讲源码地图

Flip 的完整链路共穿越 **7 份文件**。它们分属四个层，下表是本讲的「藏宝图」：

| 层 | 文件 | 作用 |
|----|------|------|
| ① Python 绑定 | `python/mod_cvcuda/operators/OpFlip.cpp` | 定义 `Flip`/`FlipInto`/`FlipVarShape`/`FlipVarShapeInto` 四个 C++ 函数，用 `m.def` 注册成 Python 的 `cvcuda.flip` / `cvcuda.flip_into` |
| ① 绑定辅助 | `python/mod_cvcuda/operators/Operators.hpp` | `PyOperator::submit` 转发与 `CreateOperator`（带缓存的算子对象工厂） |
| ② C API 声明 | `src/cvcuda/include/cvcuda/OpFlip.h` | 纯 C 头：三个函数声明 + Limitations 契约表；`extern "C"` 包裹 |
| ② C API 实现 | `src/cvcuda/OpFlip.cpp` | C 函数本体：异常翻译、句柄解包、转发给 priv 对象（注意在 `src/cvcuda` 根下，不在 include 也不在 priv） |
| ② 公开 C++ 类 | `src/cvcuda/include/cvcuda/OpFlip.hpp` + `IOperator.hpp` | 头文件内联的 RAII 包装类 `cvcuda::Flip`，每个方法一行，直接调 C 函数 |
| ③ priv 实现 | `src/cvcuda/priv/OpFlip.cpp` + `priv/OpFlip.hpp` | `cvcuda::priv::Flip`：exportData 导出数据视图、校验、调用 legacy 算子 |
| ④ CUDA kernel | `src/cvcuda/priv/legacy/flip.cu` + `legacy/CvCudaLegacy.h` | `nvcv::legacy::cuda_op::Flip`：参数校验、dtype×channels 分派表、kernel 启动与逐像素重映射 |

一个容易混淆的点先说破：链路上有**三个同名的 `Flip` 类**，分属三个命名空间——

1. `cvcuda::Flip` —— 公开 C++ 类（头文件里那几行内联代码）；
2. `cvcuda::priv::Flip` —— 私有实现类（编译进 `libcvcuda.so`，外部看不到）；
3. `nvcv::legacy::cuda_op::Flip` —— legacy 层的 kernel 宿主类（负责最终发射 kernel）。

它们不是重复，而是同一职责在四层协议下的三次「化身」。读源码时先看清命名空间，才不会串线。

## 4. 核心概念与源码讲解

先给出全链路总览图，后面四个小节逐层放大：

```text
cvcuda.flip(tensor, 1)                              ← 用户 Python 代码
  │  pybind11 按参数类型 (Tensor, int) 重载决议
  ▼
Flip()                        python/mod_cvcuda/operators/OpFlip.cpp:55   ┐
  ├─ Tensor::Create(shape, dtype)        # allocating 变体隐式分配输出    │ 第①层
  └─ FlipInto()                 OpFlip.cpp:35                             │ Python 绑定
       ├─ Stream::Current()               # 未传 stream 时兜底             │
       ├─ CreateOperator<cvcuda::Flip>(0) # 算子对象，走缓存              │
       ├─ ResourceGuard(READ in / WRITE out)                              │
       └─ guard.run → PyOperator::submit → cvcuda::Flip::operator()      ┘
                                             │  内联：直接调 C 函数
                                             ▼
cvcudaFlipSubmit(handle, stream, ...)        src/cvcuda/OpFlip.cpp:45     ┐
  ├─ CVCUDA_NVTX_RANGE("cvcudaFlipSubmit")                                │ 第②层
  ├─ nvcv::ProtectCall(...)                 # C++ 异常 → NVCVStatus      │ C API 边界
  ├─ TensorWrapHandle(in/out)               # 裸句柄 → C++ 包装          │
  └─ ToDynamicRef<priv::Flip>(handle)(...)  # dynamic_cast 回具体类型     ┘
                                             │
                                             ▼
priv::Flip::operator()            src/cvcuda/priv/OpFlip.cpp:39           ┐
  ├─ CVCUDA_NVTX_RANGE("cvcuda::Flip::operator()[Tensor]")               │ 第③层
  ├─ in/out.exportData<TensorDataStridedCuda>()  # 判空即抛异常          │ priv 实现
  └─ NVCV_CHECK_THROW(m_legacyOp->infer(*in, *out, flipCode, stream))    ┘
                                             │
                                             ▼
legacy::Flip::infer               src/cvcuda/priv/legacy/flip.cu:370      ┐
  ├─ 校验 dtype / 布局 / 通道数                                           │ 第④层
  ├─ funcs[6][4] 分派表按 (dataType, channels) 选函数实例                │ CUDA kernel
  └─ flipImpl<T> → runFlipKernel                                         │
                    flipHorizontal<<<grid, block, 0, stream>>>  flip.cu:138
                      └─ dst(x,y) = src(W-1-x, y)    # 逐像素重映射      ┘
```

注意一个精妙的结构：**编译时的依赖方向与运行时的调用方向是相反的**。从文件视角看，C++ 类（`OpFlip.hpp`）包含并包装 C API（`OpFlip.h`）；但从一次调用的视角看，流程是 Python → 公开 C++ 类 → C 函数 → 私有 C++ 对象。C API 是「转门」：异常不能穿越它，穿越时被翻译成状态码。

### 4.1 第一层：Python 绑定（python/mod_cvcuda/operators/OpFlip.cpp）

#### 4.1.1 概念说明

Python 绑定层是链路的「门面」，它负责四件 C++ 内核不关心的事：

1. **重载决议**：Python 里 `cvcuda.flip` 一个名字对应多个 C++ 函数（Tensor 版、变长批版）。pybind11 允许同名 `m.def` 多次注册，调用时按实参类型自动选择。
2. **输出分配**：allocating 变体在这里 `Tensor::Create`（u3-l3 讲过，这一步走对象缓存）。
3. **流兜底**：`stream` 参数缺省时取当前流（u4-l1 讲过的每线程流栈）。
4. **资源记账**：`ResourceGuard` 声明本次调用读了谁、写了谁，跨流场景自动插入事件等待（u4-l1/u4-l3 讲过）。

#### 4.1.2 核心流程

`cvcuda.flip(src, 1)` 的绑定层流程：

1. pybind11 发现实参是 `(Tensor, int)`，选中 `Flip`（Tensor 版 allocating 函数）；
2. `Tensor::Create(input.shape(), input.dtype())` 分配输出（查缓存）；
3. 委托 `FlipInto`：
   - 若未传 stream，`pstream = Stream::Current()`；
   - `CreateOperator<cvcuda::Flip>(0)` 从缓存取或新建公开 C++ 类对象；
   - 构造 `ResourceGuard`：输入加读锁、输出加写锁、算子对象无锁；
   - `guard.run(lambda)` 在守卫保护下执行 `Flip->submit(stream, in, out, flipCode)`；
4. 返回输出张量。

`submit` 不是公开类的方法，而是 `PyOperator` 的转发器——它把参数原样传给 `m_op(...)`，即 `cvcuda::Flip::operator()`。算子对象本身以构造参数为键进缓存（这里构造参数是常量 `0`，所以同一进程里所有 `cvcuda.flip` 调用共享同一个算子对象）。

#### 4.1.3 源码精读

`FlipInto` 是四连函数中真正干活的一个：

[python/mod_cvcuda/operators/OpFlip.cpp:35-53](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpFlip.cpp#L35-L53)

这段代码完成「取流 → 取算子 → 挂守卫 → 提交」四步。第 42 行 `CreateOperator<cvcuda::Flip>(0)` 拿到的是公开 C++ 类的包装对象；第 49-50 行 `guard.run` 里的 `Flip->submit(...)` 才真正进入下一层。

allocating 变体只是 `_into` 的两行薄包装：

[python/mod_cvcuda/operators/OpFlip.cpp:55-60](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpFlip.cpp#L55-L60)

第 57 行 `Tensor::Create(input.shape(), input.dtype())`：输出形状与 dtype 继承输入（flip 不改变这两者）。这与 u3-l1 讲过的「resize 输出形状由调用者指定」形成对比。

注册处，两个同名 `flip` 对应两种输入类型：

[python/mod_cvcuda/operators/OpFlip.cpp:96-113](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpFlip.cpp#L96-L113)

注意 `NvtxTrace("cvcuda.flip", &Flip)` 这个包装器：它给函数包上 NVTX 区间再交给 pybind11，所以 Nsight Systems 时间线上每个 Python 算子调用都有名字（第 4.4 节实践会用到）。第 131 行还注册了变长批版 `flip`，接受 `ImageBatchVarShape` 和张量形态的 `flipCode`（u3-l1 讲过参数张量化）。

`submit` 与 `CreateOperator` 的定义在绑定辅助头里：

[python/mod_cvcuda/operators/Operators.hpp:160-164](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/Operators.hpp#L160-L164)

[python/mod_cvcuda/operators/Operators.hpp:226-230](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/Operators.hpp#L226-L230)

`CreateOperator` 委托 `CreateOperatorEx`（第 195-224 行）：先用构造参数算哈希键查缓存，未命中才 `make_shared` 新建并加入缓存——这正是 u4-l2 讲过的「算子对象也走缓存」的实现处。

#### 4.1.4 代码实践

1. **实践目标**：验证绑定层注册的函数签名与文档，并确认三种 flipCode 的重映射语义。
2. **操作步骤**：

```python
# 示例代码：flip_semantics.py
import cvcuda
import numpy as np

# 1) 查看绑定层注册的签名与 docstring（docstring 就写在 OpFlip.cpp 的 R"pbdoc(...)" 里）
help(cvcuda.flip)

# 2) 构造 4x4 已知图案：每行灰度值 = 行号，方便肉眼判断翻转方向
a = np.arange(16, dtype=np.uint8).reshape(4, 4)
src = cvcuda.as_tensor(a, "HWC")   # 注意必须是 CUDA 可访问 buffer，见下
```

   由于 `as_tensor` 只接受 CUDA 显存（u2-l4），实际脚本应先把数组搬上 GPU（例如借 cupy：`cp.asarray(a)` 后再 `as_tensor`），然后：

```python
for code in (0, 1, -1):
    out = cvcuda.flip(src, code)
    # 把结果拷回 host 后打印，与手工公式对比：
    #   code >  0 : 左右翻   dst[y][x] = src[y][W-1-x]
    #   code == 0 : 上下翻   dst[y][x] = src[H-1-y][x]
    #   code <  0 : 双轴翻   dst[y][x] = src[H-1-y][W-1-x]
```

3. **需要观察的现象**：`help(cvcuda.flip)` 应显示两组签名（Tensor 版与变长批版）；三种 code 的输出逐像素符合上表公式。
4. **预期结果**：翻转语义与 OpenCV `cv2.flip` 完全一致（这是 CV-CUDA 的兼容性目标）。
5. 本环境无 GPU，以上运行结果**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：`Flip`（allocating）与 `FlipInto`（`_into`）这两个 C++ 函数，哪一个是另一个的包装？包装多了哪一步？

<details><summary>参考答案</summary>

`Flip` 是 `FlipInto` 的包装。它只多做一步：第 57 行的 `Tensor::Create(input.shape(), input.dtype())` 分配输出，然后把输出当作 `FlipInto` 的第一个参数传入。这正是 u3-l3「allocating = _into + 一次创建（查缓存）」的源码依据。
</details>

**练习 2**：`ResourceGuard` 对 input、output、算子对象分别加了什么锁？为什么算子对象是 `LOCK_MODE_NONE`？

<details><summary>参考答案</summary>

input 加 `LOCK_MODE_READ`，output 加 `LOCK_MODE_WRITE`，算子对象加 `LOCK_MODE_NONE`（第 45-47 行）。算子对象无锁是因为 `Flip::operator()` 是 `const` 的——算子本身无内部可变状态，可安全被并发调用；需要防的是「同一张量被两条流同时读写」，所以锁加在数据资源上。这与 u4-l3 的多线程测试行为一致。
</details>

**练习 3**：为什么不把 `Tensor::Create` 也放进 `FlipInto` 里？

<details><summary>参考答案</summary>

`_into` 变体的契约是「写入调用者预分配的 dst 并原样返回」，输出属于调用者；allocating 变体的契约才是「库创建输出」。把分配放进 `FlipInto` 会让两类变体的语义混淆，也剥夺了生产管线预分配输出、完全绕开缓存查询的能力（u3-l3 的性能结论）。
</details>

### 4.2 第二层：C API 边界与公开 C++ 类（OpFlip.h / OpFlip.hpp / IOperator.hpp / src/cvcuda/OpFlip.cpp）

#### 4.2.1 概念说明

这一层回答一个问题：**为什么 Python 不能直接调 priv 实现，中间要立一座 C 界碑？**

- **ABI 稳定性**：C++ 的类布局、虚表、名字修饰随编译器版本变化；C 函数符号永远稳定。把公开边界定为 C 函数，上层语言绑定（Python、未来的 Rust/Go）与下层实现可以各自演进。
- **异常边界**：C++ 异常不能穿越 `extern "C"` 边界（未定义行为）。所有 C 函数用 `nvcv::ProtectCall` 把异常翻译成 `NVCVStatus` 错误码返回；反过来，公开 C++ 类用 `CheckThrow` 把状态码再翻译回异常。于是一次 Python 调用经历了「异常 → 状态码 → 异常」的往返。
- **句柄机制**：C 侧看不到 C++ 对象，只能看到 `NVCVOperatorHandle`——一个指向不完整类型 `struct NVCVOperator` 的指针。创建在 C 函数里 `new` 出 priv 对象并转型为句柄；使用时 `dynamic_cast` 转回具体类型；销毁由 `nvcvOperatorDestroy` 统一完成。

而**公开 C++ 类**（`OpFlip.hpp`）是给 C++ 用户的糖：头文件内联，每个方法一行，直接调 C 函数。它不包含任何逻辑，逻辑全在 priv。

#### 4.2.2 核心流程

**句柄的一生**（这是本层最重要的图）：

```text
创建：cvcudaFlipCreate(&h, 0)                        (C 函数)
        └─ priv::CreateOperatorHandle<priv::Flip>(0) (make_unique + reinterpret_cast)
             priv::Flip 对象 ──────► NVCVOperatorHandle (不透明指针)

持有：cvcuda::Flip 公开类把它包进 detail::OperatorHandle (RAII)
      Python 侧则由 PyOperator 间接持有这个公开类对象（缓存）

使用：cvcudaFlipSubmit(handle, ...)
        └─ priv::ToDynamicRef<priv::Flip>(handle)    (判空 + dynamic_cast)

销毁：nvcvOperatorDestroy(handle)
        └─ priv::DestroyOperatorHandle → unique_ptr 析构 → delete
```

**RAII 要点**（`detail::OperatorHandle`，rule of five 全套）：

- 析构函数调用 `nvcvOperatorDestroy(m_handle)`，且 `m_handle` 初始为 `nullptr`（对空句柄 destroy 是安全的）；
- 拷贝构造/赋值被 `= delete`——两个所有者会双重释放；
- 移动构造把源指针置空；移动赋值先销毁自己已有的句柄再接管，并处理自赋值。

#### 4.2.3 源码精读

C 头文件声明三个函数，全部在 `extern "C"` 块内：

[src/cvcuda/include/cvcuda/OpFlip.h:37-54](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/OpFlip.h#L37-L54)

`cvcudaFlipCreate` 是唯一「生产句柄」的入口。注意 `@retval` 注释约定了错误码而非异常。紧随其后的是 Limitations 契约表——u3-l2 讲过它是支持矩阵的唯一权威：

[src/cvcuda/include/cvcuda/OpFlip.h:56-118](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/OpFlip.h#L56-L118)

细心的读者会发现：Tensor 版的表里「16bit Signed | No」，而后面 `cvcudaFlipVarShapeSubmit`（第 181-183 行）的表里是 Yes——同一算子的两个入口支持矩阵不同，第 4.4 节会在 kernel 分派表里看到对应的代码证据。

句柄类型本身只是一行 typedef：

[src/cvcuda/include/cvcuda/Operator.h:34-36](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/Operator.h#L34-L36)

`struct NVCVOperator` 从未有定义——这正是「不透明」的实现方式。

C API 实现文件在 `src/cvcuda` 根下（不在 include、不在 priv），三个函数都极短：

[src/cvcuda/OpFlip.cpp:30-43](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/OpFlip.cpp#L30-L43)

第 41 行 `priv::CreateOperatorHandle<priv::Flip>(...)` 完成「new 出 priv 对象 + 转型为句柄」。整个 lambda 被 `nvcv::ProtectCall` 包住——lambda 里抛出的任何 C++ 异常都在这里被捕获并压缩成一个 `NVCVStatus` 返回值。

[src/cvcuda/OpFlip.cpp:45-57](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/OpFlip.cpp#L45-L57)

`cvcudaFlipSubmit` 的三步：第 49 行打 NVTX 区间；第 53-54 行把裸张量句柄包成 `TensorWrapHandle`（轻量 C++ 视图，`.resource()` 取回 `nvcv::Tensor` 引用）；第 55 行一行完成「句柄还原成 `priv::Flip` 引用 + 立刻调用其 `operator()`」。

句柄两侧的辅助函数在 priv 头里：

[src/cvcuda/priv/IOperator.hpp:54-58](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/IOperator.hpp#L54-L58)

[src/cvcuda/priv/IOperator.hpp:73-89](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/IOperator.hpp#L73-L89)

`ToDynamicRef` 先判空（NULL 句柄抛 `ERROR_INVALID_ARGUMENT`），再 `dynamic_cast`（类型不符抛 `ERROR_NOT_COMPATIBLE`）——这保证把 resize 的句柄传给 flip 的 Submit 会得到明确报错而不是内存踩踏。

销毁路径闭环：

[src/cvcuda/Operator.cpp:26-29](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/Operator.cpp#L26-L29)

`nvcvOperatorDestroy` 里 `priv::DestroyOperatorHandle` 用 `unique_ptr` 接管指针，函数结束时自动 `delete`。

公开 C++ 类是纯内联的一行流：

[src/cvcuda/include/cvcuda/OpFlip.hpp:58-70](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/OpFlip.hpp#L58-L70)

构造函数调 `cvcudaFlipCreate` 后用 `CheckThrow` 把状态码翻译回异常（第 61 行）；`operator()` 调 `cvcudaFlipSubmit`（第 69 行）。两个 `operator()` 重载正好对应 C 头里两个 Submit 函数。类成员只有一个：

[src/cvcuda/include/cvcuda/OpFlip.hpp:43-56](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/OpFlip.hpp#L43-L56)

RAII 包装 `detail::OperatorHandle`：

[src/cvcuda/include/cvcuda/IOperator.hpp:37-79](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/IOperator.hpp#L37-L79)

注释写得很清楚：拥有句柄的包装类只需持有一个它，就免去了自己写全套 rule-of-five 样板。基类 `IOperator`（第 83-97 行）则只规定一个纯虚接口 `handle()` 并禁用拷贝——所有公开算子类长得都和 `Flip` 一模一样。

顺带一提，C API 宏 `CVCUDA_DEFINE_API(0, 2, ...)` 里的 `(0, 2)` 是符号版本号，用于跨版本 ABI 兼容（定义在 [src/cvcuda/priv/SymbolVersioning.hpp:23-24](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/SymbolVersioning.hpp#L23-L24)），细节留给 u6-l2 展开。

#### 4.2.4 代码实践

1. **实践目标**：不看本讲，独立建立「C 函数 ↔ C++ 内联调用 ↔ Python 入口」三列对照表。
2. **操作步骤**：
   - 打开 `src/cvcuda/include/cvcuda/OpFlip.h`，抄下三个 C 函数签名；
   - 打开 `src/cvcuda/include/cvcuda/OpFlip.hpp`，找出每个 C 函数被哪一行内联代码调用；
   - 打开 `python/mod_cvcuda/operators/OpFlip.cpp`，找出每个 Submit 最终被哪个 Python 函数触发；
   - 对另一个算子（如 `OpCvtColor.h`）重复一遍，验证规律。
3. **需要观察的现象**：每个算子的 C 函数个数 = Submit 入口个数；C++ 公开类方法与之一一对应；Python 侧函数个数 = 入口数 × 变体数。
4. **预期结果**：得到一张类似下面的表（Flip 的参考答案）：

| C 函数 | C++ 类调用处 | Python 触发处 |
|--------|--------------|----------------|
| `cvcudaFlipCreate` | `OpFlip.hpp:61`（构造） | `CreateOperator<cvcuda::Flip>(0)`（OpFlip.cpp:42） |
| `cvcudaFlipSubmit` | `OpFlip.hpp:69` | `FlipInto` → `cvcuda.flip_into` |
| `cvcudaFlipVarShapeSubmit` | `OpFlip.hpp:75-76` | `FlipVarShapeInto` → 变长批版 `flip_into` |

5. 这是纯源码阅读型实践，不依赖 GPU，可直接完成。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `NVCVOperatorHandle` 指向的 `struct NVCVOperator` 从不给出定义？

<details><summary>参考答案</summary>

不透明指针模式：调用者只能持有和传递地址，无法 `->` 解引用、无法 `sizeof`、无法栈上构造。这样 priv 层的对象布局（成员、虚表）可以随意改动而不破坏 ABI——反正外面看不见。类型安全靠 `ToDynamicRef` 里的 `dynamic_cast` 在使用时动态校验。
</details>

**练习 2**：如果用户把 `cvcuda::Resize` 对象的 `handle()` 传给了 `cvcudaFlipSubmit`，会发生什么？

<details><summary>参考答案</summary>

`ToDynamicRef<priv::Flip>` 里的 `dynamic_cast<priv::Flip*>(handle)` 返回 `nullptr`（priv::Resize 不是 priv::Flip 的派生类），于是抛出 `nvcv::Exception(ERROR_NOT_COMPATIBLE, "Handle doesn't correspond to the requested object...")`（priv/IOperator.hpp:80-88），经 `ProtectCall` 翻译成错误码返回，C++ 公开类的 `CheckThrow` 再抛回给调用者。不会发生内存踩踏。
</details>

**练习 3**：`detail::OperatorHandle` 的移动赋值运算符里，`if (this != &that)` 检查防的是什么？

<details><summary>参考答案</summary>

自移动：`h = h;`。若没有该检查，第一行 `nvcvOperatorDestroy(m_handle)` 会先销毁自己的句柄，随后 `m_handle = that.m_handle` 读到的正是已销毁的悬垂指针，第二次析构时双重释放。先判断再「销毁自己的、接管对方的、置空对方的」顺序保证了异常安全。
</details>

### 4.3 第三层：priv 私有实现（src/cvcuda/priv/OpFlip.cpp + priv/OpFlip.hpp）

#### 4.3.1 概念说明

priv 层是算子的「翻译官」：它把面向对象的 `nvcv::Tensor` 世界翻译成 legacy kernel 想要的扁平数据视图。它做三件事：

1. **exportData**：把抽象张量导出为 `TensorDataStridedCuda`（basePtr + stride 的 CUDA 显存视图）。若张量不是 CUDA 可访问的 pitch-linear 数据，导出返回 `nullptr`，priv 层立刻抛异常——这是 u2-l4「CPU 数组进不来」在算子侧的第二道关卡。
2. **NVTX 打点**：给 C++ 实现层也标上名字（与绑定层的 `cvcuda.flip` 不同，这里是 `cvcuda::Flip::operator()[Tensor]`）。
3. **委托 legacy**：调用 `m_legacyOp->infer(...)`，错误码经 `NVCV_CHECK_THROW` 翻译回异常。

注意 `cvcuda::priv::Flip` 与公开类同名但完全是两个类：公开类在 `include/cvcuda/`（给用户），priv 类编译进 `libcvcuda.so`（给 C API 内部转发）。私有类声明里能看到它真正的家底——两个 legacy 算子指针。

#### 4.3.2 核心流程

```
priv::Flip::operator()(stream, in, out, flipCode)
  ├─ CVCUDA_NVTX_RANGE("cvcuda::Flip::operator()[Tensor]")
  ├─ input  = in.exportData<nvcv::TensorDataStridedCuda>()   ── nullptr? 抛异常
  ├─ output = out.exportData<...>()                          ── nullptr? 抛异常
  └─ NVCV_CHECK_THROW(m_legacyOp->infer(*input, *output, flipCode, stream))
        └─ 进入第④层
```

构造函数则一次性造好两个 legacy 算子（Tensor 版与变长批版），此后每次调用复用。`maxVarShapeBatchSize` 参数被忽略（源码注释：maxIn/maxOut not used by op）——这是 legacy 体系的历史遗留，u5-l3 会展开。

#### 4.3.3 源码精读

私有类声明，成员就是两个 legacy 算子的 `unique_ptr`：

[src/cvcuda/priv/OpFlip.hpp:37-50](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpFlip.hpp#L37-L50)

构造与 Tensor 版实现：

[src/cvcuda/priv/OpFlip.cpp:29-57](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpFlip.cpp#L29-L57)

第 29 行 `namespace legacy = nvcv::legacy::cuda_op;` 起了别名，第 42 行 `exportData` 后立即判空抛 `ERROR_INVALID_ARGUMENT`，第 56 行 `NVCV_CHECK_THROW` 把 legacy 返回的 `ErrorCode` 包成异常。变长批版（第 59-87 行）多导出一个张量形态的 `flipCode`，且 `exportData` 多带一个 `stream` 参数（变长批的设备侧元数据需要按流调度拷贝，u2-l3 讲过）。

#### 4.3.4 代码实践

1. **实践目标**：数清 priv 层的「判空防线」，并理解每道防线挡住什么。
2. **操作步骤**：
   - 在 [priv/OpFlip.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpFlip.cpp#L39-L87) 里数一数 `== nullptr` 的判断：Tensor 版 2 处（in/out），变长批版 3 处（in/out/flipCode）；
   - 思考：什么样的张量会让 `exportData<TensorDataStridedCuda>()` 返回空？（提示：设备不对、内存类型不对、或不是 pitch-linear 布局）；
   - 写一个 5 行以内的思考实验脚本：用 `as_tensor` 包装一个 **CPU** numpy 数组直接调 `cvcuda.flip`，预测异常在哪一层、消息是什么。
3. **需要观察的现象**：异常消息应包含 "must be cuda-accessible" 字样（即 priv 层第 45-46 行的文案，经 ProtectCall/CheckThrow 两道翻译后呈现为 Python 异常）。
4. **预期结果**：失败发生在第③层 priv 的 exportData 判空处，而不会等到 kernel 层——错误被尽早拦截，这正是分层校验的意义。
5. 本环境无 GPU，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 priv 层每次调用都重新 `exportData`，而不是构造时导出一次缓存起来？

<details><summary>参考答案</summary>

priv 对象（经由 Python 对象缓存）可能被几十次调用复用，但每次传入的张量各不相同——张量是调用级资源，算子对象是进程级资源。exportData 只是把句柄翻译成轻量视图（拷贝指针和 stride，不搬数据），开销极小，换来的正确性是必须的。
</details>

**练习 2**：变长批版的 `exportData` 比 Tensor 版多一个 `stream` 参数（priv/OpFlip.cpp:63），为什么？

<details><summary>参考答案</summary>

`ImageBatchVarShape` 的每图尺寸/格式元数据在主机侧，kernel 需要在设备侧读到它们，因此导出时要把元数据列表异步拷到显存——这是一次流上的操作，必须绑定到 stream 保证与后续 kernel 的顺序（u2-l3 讲过的「exportData 会向流调度拷贝」）。普通 Tensor 的 shape/stride 编译期已知，无需设备侧拷贝。
</details>

**练习 3**：`NVCV_CHECK_THROW` 与绑定层的 `nvcv::ProtectCall` 方向相反，它们各自翻译什么？

<details><summary>参考答案</summary>

`NVCV_CHECK_THROW`：把 legacy 返回的 `ErrorCode`（C 风格错误码）翻译成 C++ 异常，让 priv 层代码可以用异常风格写。`ProtectCall`：在 C 边界把 C++ 异常翻译成 `NVCVStatus` 返回值。两者合起来构成「kernel 错误码 → C++ 异常 → C 状态码 →（Python 绑定再抛异常）」的完整错误通道。
</details>

### 4.4 第四层：legacy CUDA kernel（src/cvcuda/priv/legacy/flip.cu）

#### 4.4.1 概念说明

legacy 层是仓库中体量最大的内核形态（`priv/legacy` 下数十个 `.cu` 文件，u5-l3 会讲它与原生 `Op*.cu` 的取舍）。它的宿主类 `nvcv::legacy::cuda_op::Flip` 继承自 `CudaBaseOp`，核心是一个 `infer` 方法：接收上一步导出的 `TensorDataStridedCuda` 视图，完成「最后一公里校验 + kernel 选择 + kernel 发射」。

Flip 的 kernel 本体极其简单——**纯像素重映射**：输出像素 \((x, y)\) 从输入的某个镜像位置取值。三种翻转对应三个 kernel：

\[ \text{dst}(x,y) = \begin{cases} \text{src}(W-1-x,\; y) & \text{flipCode} > 0 \text{（左右翻）} \\ \text{src}(x,\; H-1-y) & \text{flipCode} = 0 \text{（上下翻）} \\ \text{src}(W-1-x,\; H-1-y) & \text{flipCode} < 0 \text{（双轴翻）} \end{cases} \]

但「简单」的算子恰恰被优化得最狠：源码里有两组针对访存的优化——

- **NIX（每线程处理多列）**：翻转是访存受限算子，一个线程一个像素时显存请求太少（注释里记了 ncu 实测 DRAM 利用率 45%）。让每线程以步长处理 NIX=4 列，既保持相邻线程访问相邻列（合并访存），又让每线程同时挂起 4 个独立访存（内存级并行）。
- **VEC 向量化**：单通道 uchar 每像素仅 1 字节，逐元素索引开销占比过高。当列数能被 4 整除且指针对齐时，把 4 列拼成一个宽向量一次读写；水平翻转时用 `flipLanes` 在寄存器里反转 4 个 lane。

#### 4.4.2 核心流程

```
legacy::Flip::infer(input, output, flipCode, stream)
  ├─ 校验：in.dtype == out.dtype？
  ├─ 校验：布局 ∈ {NHWC, HWC, NCHW, CHW} 且两者一致？
  ├─ 校验：dtype ∈ {U8, U16, S32, F32}？
  ├─ 校验：通道数 ∈ {1, 3, 4}？                     ← 2 通道显式拒绝
  ├─ isPlanar?  ── 是：把 N*C 个平面摊平成单通道样本，走列 0
  │                  （要求平面紧密排列、样本×通道 ≤ 65535 = grid.z 上限）
  └─ funcs[dataType][channels-1] ── 选出 flip<uchar3> 之类的具体实例
        └─ flipImpl<T,NIX>
             ├─ TensorDataAccessStridedImagePlanar::Create × 2
             ├─ 检查总字节数 ≤ int32 上限（用 32 位寻址的代价）
             ├─ cuda::CreateTensorWrapNHW × 2   ← 带类型的安全包装器
             └─ runFlipKernel
                  ├─ blockSize = (32, 8, 1)
                  ├─ grid = (⌈⌈W/NIX⌉/32⌉, ⌈H/8⌉, N)
                  └─ flipCode 符号三分支 → <<<grid, block, 0, stream>>>
                        ← 第 4 个配置参数就是一路穿透下来的流（u4-l1）
```

grid 维度计算用向上取整：\( \text{grid.x} = \lceil \lceil W/\text{NIX} \rceil / 32 \rceil \)，\( \text{grid.y} = \lceil H/8 \rceil \)，\( \text{grid.z} = N \)（批内每样本一个 z 层，kernel 里 `get_batch_idx()` 就是 `blockIdx.z`）。

#### 4.4.3 源码精读

kernel 宿主类声明（继承 `CudaBaseOp`，Limitations 注释同样齐全）：

[src/cvcuda/priv/legacy/CvCudaLegacy.h:459-522](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/legacy/CvCudaLegacy.h#L459-L522)

`infer` 的四道校验 + 分派表：

[src/cvcuda/priv/legacy/flip.cu:370-398](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/legacy/flip.cu#L370-L398)

第 373 行校验输入输出 dtype 一致；第 379-393 行用 `GetLegacyDataFormat` 把 nvcv 布局翻译回 legacy 的四选一并校验；第 397-402 行限定四种 dtype——注意这里没有 16bit Signed，正与 4.2 节 C 头契约表的「No」互相印证。

通道数校验与著名的分派表：

[src/cvcuda/priv/legacy/flip.cu:404-414](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/legacy/flip.cu#L404-L414)

[src/cvcuda/priv/legacy/flip.cu:425-432](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/legacy/flip.cu#L425-L432)

`funcs[6][4]` 是 6 种 legacy 数据类型 × 4 种通道数的函数指针矩阵：行序为 U8、8S、16U、16S、S32、F32，8S 与 16S 两行全零（未实现，调用会崩，所以前面校验先拦下）；第 0 列全部路由到 `flipSingleChannel`（内部再择优宽向量版）；F32 行的三/四通道走 `flipScalar`（NIX=1，float3/float4 单元素已够宽）。

最终调用一行（平面路径见 436-468 行，把 NCHW 摊平成单通道复用列 0）：

[src/cvcuda/priv/legacy/flip.cu:470](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/legacy/flip.cu#L470)

kernel 发射器，block/grid 配置与三分支：

[src/cvcuda/priv/legacy/flip.cu:130-151](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/legacy/flip.cu#L130-L151)

第 138 行 `<<<gridSize, blockSize, 0, stream>>>`：第三参数共享内存 0 字节，第四参数就是流——请回看 4.1 节总览图，这个 `stream` 正是 Python 侧 `Stream::Current()` 一路原样穿透的结果。

kernel 本体（左右翻）：

[src/cvcuda/priv/legacy/flip.cu:58-79](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/legacy/flip.cu#L58-L79)

第 61-62 行把线程坐标换算成 `dst_y` 与 `batch_idx`；第 71-78 行的 `#pragma unroll` 循环让每线程处理 NIX 列；第 76 行就是本算子的全部数学：`*dst.ptr(...) = *src.ptr(batch_idx, dst_y, width - 1 - dst_x)`。`get_batch_idx()` 是个宏：

[src/cvcuda/priv/legacy/CvCudaUtils.cuh:74](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/legacy/CvCudaUtils.cuh#L74)

NIX 优化的依据写在注释里（这是一段值得细读的「为什么」）：

[src/cvcuda/priv/legacy/flip.cu:36-43](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/legacy/flip.cu#L36-L43)

单通道向量化路径的入口与资格检查：

[src/cvcuda/priv/legacy/flip.cu:361-368](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/legacy/flip.cu#L361-L368)

[src/cvcuda/priv/legacy/flip.cu:296-312](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/legacy/flip.cu#L296-L312)

列数整除 4 且 basePtr/行距/样本距都对齐时走 `flipWideSingleChannel<T,4>`（每次搬 4 列），否则回退标量版。`flipImpl` 主体（含 int32 尺寸上限检查与 `CreateTensorWrapNHW` 包装）在 [flip.cu:153-186](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/legacy/flip.cu#L153-L186)。

#### 4.4.4 代码实践

1. **实践目标**：不运行程序，纯靠读源码推算一次具体调用的 kernel 配置，训练「从张量形状到 grid 维度」的推演能力。
2. **操作步骤**：
   - 设输入为形状 `(2, 1080, 1920, 4)` 的 `RGBA8`（即 `uchar4`）NHWC 张量，`flipCode = 1`；
   - 查分派表 `funcs`：dataType 是 U8（第 0 行）、channels=4（第 3 列）→ 选中 `flip<uchar4>`，NIX 用 `kFlipNIX = 4`；
   - 代入公式：\( W = 1920 \)，\( \text{grid.x} = \lceil \lceil 1920/4 \rceil / 32 \rceil = \lceil 480/32 \rceil = 15 \)；\( \text{grid.y} = \lceil 1080/8 \rceil = 135 \)；\( \text{grid.z} = 2 \)；
   - 写成脚本（`flip_code` 换成 0 与 -1 再推一遍）：

```python
# 示例代码：grid 计算，验证与手推一致
W, H, N, NIX, bx, by = 1920, 1080, 2, 4, 32, 8
import math
gx = math.ceil(math.ceil(W / NIX) / bx)
gy = math.ceil(H / by)
print(f"block=(32,8,1) grid=({gx},{gy},{N})")   # 预期 (15,135,2)
```

3. **需要观察的现象**：脚本输出 `(15,135,2)`；`flipCode` 取 0 或 -1 时 grid 不变（三个 kernel 共用同一套维度计算），只是选中不同 kernel 函数。
4. **预期结果**：理解「每线程 4 列 × 32 线程 = 每 block 行内 128 列；15 个 block 覆盖 1920 列」的映射关系。
5. 此脚本是纯 Python 计算示例（示例代码，可直接运行）；对应的 GPU 行为**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `funcs` 表里 8S 和 16S 两行全是 0，代码却不会在这里崩溃？

<details><summary>参考答案</summary>

因为 `infer` 在查表**之前**就做了校验：flip.cu:397-402 只放行 `kCV_8U || kCV_16U || kCV_32S || kCV_32F`，16S 会被拒并返回 `INVALID_DATA_TYPE`。空行是「未实现即不支持」的直接表达，与 OpFlip.h 契约表中「8bit Signed / 16bit Signed = No」一一对应。变长批版支持 16S 是因为它走 `FlipOrCopyVarShape`（flip_or_copy_var_shape.cu），契约表也相应写 Yes。
</details>

**练习 2**：把 `flipCode` 从 1 改成 -1，grid/block 维度会变吗？kernel 里哪一行代码变了？

<details><summary>参考答案</summary>

维度不变——三分支共用 `runFlipKernel` 里同一次 `blockSize`/`gridSize` 计算，变的只是选中 `flipHorizontal`、`flipVertical` 还是 `flipHorizontalVertical`（flip.cu:136-150）。kernel 内部，双轴版比左右版多算一个 `src_y = dstSize.h - 1 - dst_y`，读指针变成 `(src_y, width-1-dst_x)`（flip.cu:124）。
</details>

**练习 3**：`flipImpl` 里为什么要检查 `sampleStride * numSamples` 不超过 int32 最大值？

<details><summary>参考答案</summary>

因为它随后创建的是 `CreateTensorWrapNHW<T, int32_t>`——寻址用 32 位偏移的包装器（flip.cu:165-170）。32 位寻址在寄存器压力和指令开销上更便宜，但可表示范围约 2GB；超出则报 `INVALID_PARAMETER` 拒绝执行，而不是静默溢出算出错误地址。这是「用小类型换性能 + 显式护栏」的典型取舍。
</details>

## 5. 综合实践

**任务：画出 `cvcuda.flip(tensor, 1)` 的完整调用时序图，并用 NVTX 在 Nsight Systems 时间线上验证它与你的图一致。**

### 第一步：画时序图（纸面，约 30 分钟）

对照本讲 4.1 节的总览图，**合上讲义**自己画一张竖向时序图，要求：

- 参与者竖排 6 列：Python 调用方、绑定层（OpFlip.cpp）、公开 C++ 类（OpFlip.hpp）、C API（src/cvcuda/OpFlip.cpp）、priv 层（priv/OpFlip.cpp）、legacy 层（flip.cu）；
- 每个箭头标注**文件名 + 函数名 + 行号**（例如 `PyOperator::submit → cvcuda::Flip::operator()`，`Operators.hpp:161`）；
- 用不同颜色标出三类横切关注点：stream 参数的传递路径（从 `Stream::Current()` 到 `<<<...,stream>>>`）、异常/错误码的翻译往返（throw → ProtectCall → CheckThrow）、NVTX 区间的嵌套关系（`cvcuda.flip` ⊃ `cvcudaFlipSubmit` ⊃ `cvcuda::Flip::operator()[Tensor]`）。

### 第二步：运行并捕获 NVTX（需 GPU 与 Nsight Systems）

```python
# 示例代码：flip_trace.py —— 给时间线留足样本
import cvcuda, cupy as cp

a   = cp.random.randint(0, 255, (2, 1080, 1920, 4), dtype=cp.uint8)
src = cvcuda.as_tensor(a, "NHWC")
for _ in range(50):                 # 多跑几轮，时间线上区间更清晰
    dst = cvcuda.flip(src, 1)
cvcuda.Stream.default.sync()
```

```bash
nsys profile -o flip_trace --trace=cuda,nvtx python flip_trace.py
nsys stats flip_trace.nsys-rep       # 或用 nsys-ui 打开图形界面看时间线
```

### 第三步：对照验证

1. 在时间线上找到每次调用的 NVTX 区间链：`cvcuda.flip`（绑定层 NvtxTrace 打的）内嵌 `cvcudaFlipSubmit`（src/cvcuda/OpFlip.cpp:49 打的）内嵌 `cvcuda::Flip::operator()[Tensor]`（priv/OpFlip.cpp:41 打的）——三层名字一一对应链路的①②③层；
2. 确认 kernel 名字：左右翻应看到 `flipHorizontal` 系的 kernel 与 NVTX 区间对齐；
3. 检查 stream：50 次 flip 的 kernel 应在同一（默认）流上顺序排列，验证 u4-l1 的「流穿透」结论。

**需要观察的现象**：NVTX 区间嵌套顺序与图一致；每个区间下方对齐一个 kernel；相邻调用无交错。
**预期结果**：你手画的时序图与实测完全吻合；若 `cvcuda.flip` 区间与 kernel 之间出现空隙，思考一下异步提交与区间命名时机的含义（区间在 CPU 侧 push/pop，kernel 在流上异步执行）。
本环境无 GPU 与 nsys，以上**待本地验证**。

## 6. 本讲小结

- **四层六站七文件**：一次 `cvcuda.flip` 依次经过 Python 绑定（`python/mod_cvcuda/operators/OpFlip.cpp`）→ 公开 C++ 类（`include/cvcuda/OpFlip.hpp`）→ C API（`include/cvcuda/OpFlip.h` 声明、`src/cvcuda/OpFlip.cpp` 实现）→ priv 实现（`priv/OpFlip.cpp`）→ legacy kernel（`priv/legacy/flip.cu`）；链路中有三个同名 `Flip` 类分属三个命名空间，职责各异。
- **C ABI 是界碑**：C 函数符号稳定、异常不能穿越；`ProtectCall` 把异常压成 `NVCVStatus`，公开 C++ 类用 `CheckThrow` 还原成异常，形成「异常→状态码→异常」的往返。
- **句柄的一生**：`CreateOperatorHandle` 里 `make_unique` + `reinterpret_cast` 造出不透明句柄，`ToDynamicRef` 用判空 + `dynamic_cast` 还原，`detail::OperatorHandle` 以 RAII（禁拷贝、安全移动）保证恰好一次 `nvcvOperatorDestroy`。
- **priv 层是翻译官**：`exportData<TensorDataStridedCuda>()` 判空即抛异常，是「CPU 数据进不了算子」的第二道关卡；变长批的 exportData 还向流调度元数据拷贝。
- **kernel 层的模式**：先校验（dtype/布局/通道）、再查 `funcs[6][4]` 分派表选具体模板实例、最后按 `flipCode` 三分支发射 kernel；访存优化有 NIX（内存级并行）与 VEC（宽向量）两板斧。
- **横切关注点全程可见**：stream 从 `Stream::Current()` 原样穿透到 `<<<...,stream>>>` 第四参数；NVTX 在绑定、C API、priv 三层各打一个区间，嵌套关系即调用栈。

## 7. 下一步学习建议

本讲走通了「一个算子的骨架」，接下来按两条线深入：

1. **下一讲 u5-l2（GPU 数据访问：exportData 与 TensorDataAccess）**：本讲只说了 exportData「返回视图」，下一讲拆开看 `TensorDataAccess.hpp` 如何用 stride 算出任意像素的设备指针，以及 `TensorDataAccessStridedImagePlanar`、`CreateTensorWrapNHW` 这些 kernel 侧包装器的用法。
2. **u5-l3（legacy 与原生内核）**：本讲的 kernel 落在 `priv/legacy`，而 SIFT 等新算子直接写在 `priv/Op*.cu`；下一讲对比两种形态的由来与取舍。
3. **横向推广练习**：任选 `OpCvtColor` 或 `OpGaussianBlur`，不看讲义，独立走一遍四层（提示：Gaussian 模糊的 legacy kernel在 `filter.cu` 里，用 `rg "class Gaussian"` 定位——u1-l4 教过 Gaussian 与 GaussianNoise 是两个算子）。若你想知道「新增一个算子要写哪些文件」，直接跳到 u8-l1 的 mkop 脚手架一讲。
