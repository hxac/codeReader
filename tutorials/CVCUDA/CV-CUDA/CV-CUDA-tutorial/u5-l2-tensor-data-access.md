# GPU 数据访问：exportData 与 TensorDataAccess

## 1. 本讲目标

上一讲（u5-l1）我们打通了一个算子的四层链路：Python 绑定 → C API → priv 实现 → CUDA kernel。本讲把镜头对准链路中最关键的一次「交接」：priv 实现拿到的是抽象的 `nvcv::Tensor` / `nvcv::ImageBatchVarShape` 对象，而 CUDA kernel 需要的是赤裸裸的「基址 + 步长 + 形状」。完成这次交接的机制就是 `exportData` 与 `TensorDataAccess`。

学完本讲你应该能够：

1. 说清 `Tensor::exportData<TensorDataStridedCuda>()` 这一行背后发生了什么，以及它在什么条件下返回空（`NullOpt`）、priv 层又如何把这个空翻译成异常。
2. 使用 `TensorDataAccessStridedImage` 等访问器，手工计算任意像素 \((n,h,w,c)\) 的设备地址。
3. 解释 kernel 侧 `TensorWrap` 与主机侧 `TensorDataAccess` 的分工，以及 `ImageBatchVarShape` 的 `exportData` 为什么需要 `stream` 参数。

## 2. 前置知识

本讲假设你已读过：

- **u2-l1 张量模型**：stride 以字节为单位，地址公式 \(\text{addr} = \text{basePtr} + \sum_d i_d \cdot s_d\)，其中 \(i_d\) 是第 \(d\) 维坐标、\(s_d\) 是该维步长。本讲就是把这个公式「工程化」。
- **u2-l3 变长批处理**：`ImageBatchVarShape` 导出后是一个「双面结构」——主机侧有 `hostFormatList`/`maxWidth`，设备侧有 `imageList`。本讲讲这个结构是**怎么被导出来**的。
- **u5-l1 算子四层结构**：priv 层位于 C API 之下、kernel 之上；C++ 异常不能穿越 C ABI 边界。

再补充两个本讲要用的小概念：

- **`Optional<T>`**：nvcv 自带的「可能没有值」容器，语义类似 `std::optional`。它可以和 `nullptr` 比较——`opt == nullptr` 表示「没有值」。priv 代码里的 `if (inData == nullptr)` 判断的就是这个。
- **bufferType 标签**：每份导出的张量数据都带一个 `bufferType` 字段，声明「这块缓冲到底是什么形态」（当前只有一种合法形态：CUDA 上的 strided 布局）。它是 `exportData` 能否成功转型的唯一判据，本讲 4.2 节专门展开。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/nvcv/src/include/nvcv/Tensor.hpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/Tensor.hpp) | 公开 C++ 类 `nvcv::Tensor`，声明两个 `exportData` 重载 |
| [src/nvcv/src/include/nvcv/TensorData.h](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/TensorData.h) | C 结构 `NVCVTensorData`：跨 ABI 的数据视图载体，含官方地址公式注释 |
| [src/nvcv/src/include/nvcv/TensorData.hpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/TensorData.hpp) | C++ 视图类层次 `TensorData` → `TensorDataStrided` → `TensorDataStridedCuda` |
| [src/nvcv/src/include/nvcv/detail/TensorDataImpl.hpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/detail/TensorDataImpl.hpp) | 上述类的 inline 实现：`basePtr()`/`stride()`/`cast()` |
| [src/nvcv/src/include/nvcv/TensorDataAccess.hpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/TensorDataAccess.hpp) | 本讲主角：三层访问器，把 stride 公式包装成 `sampleData`/`rowData` 等安全接口 |
| [src/cvcuda/priv/OpFlip.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpFlip.cpp) / [src/cvcuda/priv/OpResize.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpResize.cpp) | priv 层标准三段式：导出 → 判空抛异常 → infer |
| [src/cvcuda/priv/legacy/flip.cu](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/legacy/flip.cu) | kernel 侧：主机访问器配 grid + `TensorWrap` 进 kernel |
| [src/cvcuda/include/cvcuda/cuda_tools/TensorWrap.hpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/cuda_tools/TensorWrap.hpp) | 设备端轻量寻址包装器（`cuda::CreateTensorWrapNHW`） |
| [src/nvcv/src/priv/ImageBatchVarShape.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/priv/ImageBatchVarShape.cpp) | 变长批导出实现：脏区间拷贝 + event 栅栏 |
| [src/nvcv/src/include/nvcv/ImageBatchData.hpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/ImageBatchData.hpp) | `ImageBatchVarShapeDataStridedCuda` 类声明 |

## 4. 核心概念与源码讲解

### 4.1 TensorData 体系：从「对象」到「数据视图」

#### 4.1.1 概念说明

`nvcv::Tensor` 是一个重量级对象：它拥有显存、挂接分配器、维护引用计数、可被 Python 缓存复用。而 kernel 不关心这些，kernel 只需要三样东西：

1. 一个基址 `basePtr`；
2. 每一维的字节步长 `strides[]`；
3. 形状 / dtype / 布局这些元数据。

`exportData` 就是「提纯」：把对象中与寻址相关的内容抽取成一份廉价的 POD 视图（`NVCVTensorData`），之后无论是传给 legacy 算子的 `infer`，还是进一步包装成设备端的 `TensorWrap`，用的都是这份视图。视图本身不拥有内存——原始 Tensor 对象活着，视图才有效。这就是为什么 priv 算子的 `operator()` 里导出视图只是几行栈上代码，绝不能把视图存起来跨调用使用。

C++ 侧的视图类是一个三层继承结构：

```
TensorData                      // 元数据：rank/shape/layout/dtype + bufferType 标签
└── TensorDataStrided           // 加上 basePtr() / stride(d)
    └── TensorDataStridedCuda   // 标记「这块 strided 缓冲位于 CUDA 设备」
```

注意这个继承**不添加任何数据成员**（后面会看到 `cast` 对此有 `static_assert`），三者的区别只在「承诺的类型」上：层级越深，承诺越多。

#### 4.1.2 核心流程

一次 `tensor.exportData<nvcv::TensorDataStridedCuda>()` 的完整流程：

```text
nvcv::Tensor 对象（C++ 公开层）
        │  调用 TensorData exportData() const        ← 无模板重载，永不失败
        ▼
priv::Tensor::exportData(NVCVTensorData&)            ← 真正的填充实现
        │  bufferType = NVCV_TENSOR_BUFFER_STRIDED_CUDA
        │  dtype / layout / rank / shape[] / buffer.strided{strides[], basePtr}
        ▼
TensorData（C++ 视图基类）
        │  cast<TensorDataStridedCuda>()
        │  检查 bufferType 是否 == NVCV_TENSOR_BUFFER_STRIDED_CUDA
        ├── 匹配   → Optional 里有值
        └── 不匹配 → Optional{NullOpt}               ← 唯一的「失败」出口
```

地址公式（来自 C 头文件的官方注释）：

\[
\text{pelem} = \text{basePtr} + i_0 \cdot \text{strides}[0] + \cdots + i_{r-1} \cdot \text{strides}[r-1]
\]

#### 4.1.3 源码精读

**① 导出的入口：两个重载。** [Tensor.hpp:L81-L93](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/Tensor.hpp#L81-L93) 中，无模板版 `exportData()` 返回基类 `TensorData`；模板版一行委托：先取基类视图，再做 `cast<DerivedTensorData>()`。所以「模板版可能失败、无模板版不会」这件事，完全由 `cast` 决定。

**② 真正的填充。** [priv/Tensor.cpp:L243-L255](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/priv/Tensor.cpp#L243-L255) 是库内分配张量的导出实现：把 `bufferType` 硬编码为 `NVCV_TENSOR_BUFFER_STRIDED_CUDA`，再从构造时记下的 `m_reqs`（requirements，分配需求单）里抄 dtype、layout、rank、shape 和 strides。注意它**不做任何设备查询、不做任何拷贝**——导出是纯 CPU 侧的元数据搬运，开销可忽略。

**③ 跨 ABI 的载体。** [TensorData.h:L31-L51](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/TensorData.h#L31-L51) 定义了 C 结构 `NVCVTensorBufferStrided`（`strides` 数组 + `basePtr`，注释里就是上面的地址公式）和 `NVCVTensorBufferType` 枚举。重点看枚举：**当前只有两个值**——`NONE`(0) 和 `STRIDED_CUDA`。这个「只有一种合法形态」的事实是理解 4.2 节失败条件的钥匙。

**④ cast 的实现。** [TensorDataImpl.hpp:L94-L115](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/detail/TensorDataImpl.hpp#L94-L115)：`IsCompatible()` 只是拿 `bufferType` 问派生类的 `IsCompatibleKind`；`cast()` 据此决定返回有值的 `Optional` 还是 `NullOpt`。两个 `static_assert` 很说明问题——派生类「不得添加新数据成员」，因为整个转型实际上只是**同一块 `NVCVTensorData` 换一个更具体的类型透镜**，零拷贝。

**⑤ 基址与步长的读取。** [TensorDataImpl.hpp:L119-L134](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/detail/TensorDataImpl.hpp#L119-L134)：`basePtr()` 从 C 结构里取出字节指针，`stride(d)` 带越界检查（越界抛异常，不是返回 0）。

**⑥ 一个真实的对照样本。** 官方测试 [TestTensorWrap.cpp:L170-L177](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/nvcv_types/cudatools_system/TestTensorWrap.cpp#L170-L177) 演示了标准用法：`nvcv::Tensor tensor({{123}, "N"}, dt);` 直接构造，`exportData<nvcv::TensorDataStridedCuda>()` 后断言非空。我们 4.1.4 的实践就以它为模板。

#### 4.1.4 代码实践

**实践目标**：亲手完成一次「构造 → 导出 → 转型」，并观察 `cast` 的两种结局。

**操作步骤**（示例代码，保存为 `export_demo.cpp`）：

```cpp
// 示例代码：不链接任何 nvcv 库即可编译（这些头全是 inline 实现）
#include <nvcv/TensorData.hpp>
#include <iostream>

int main()
{
    // --- 场景 A：bufferType 打上 CUDA strided 标签 ---
    nvcv::TensorDataStridedCuda::Buffer buf{};
    buf.strides[0] = 8;
    buf.basePtr    = reinterpret_cast<nvcv::Byte *>(0x1000); // 假地址，只看类型系统

    nvcv::TensorDataStridedCuda good{
        nvcv::TensorShape{{4}, "N"}, nvcv::DataType{NVCV_DATA_TYPE_U8}, buf};

    // 基类视图 -> 转型回 CUDA 视图：标签匹配，成功
    auto ok = good.cast<nvcv::TensorDataStridedCuda>();
    std::cout << "A: cast ok? " << (ok != nullptr)
              << ", basePtr offset = " << ok->stride(0) << "\n";

    // --- 场景 B：零初始化的 NVCVTensorData，bufferType == NONE ---
    NVCVTensorData raw{};                       // 零初始化 => bufferType = NONE
    nvcv::TensorData bad{raw};

    auto fail = bad.cast<nvcv::TensorDataStridedCuda>();
    std::cout << "B: cast ok? " << (fail != nullptr) << "\n";   // 预期打印 0

    // --- 对照：不走 cast、直接构造会怎样？ ---
    try
    {
        nvcv::TensorDataStridedCuda direct{raw};  // 直接构造，标签不匹配
        std::cout << "C: 直接构造竟然成功了\n";
    }
    catch (const nvcv::Exception &e)
    {
        std::cout << "C: 直接构造抛异常: " << e.message() << "\n";
    }
    return 0;
}
```

编译（只需头文件路径，路径以本地仓库为准）：

```bash
g++ -std=c++17 export_demo.cpp \
    -I src/nvcv/src/include -I <CUDA_HOME>/include \
    -o export_demo && ./export_demo
```

**需要观察的现象**：A 打印 `cast ok? 1`；B 打印 `cast ok? 0`（这就是 priv 层判空分支拦截的 `NullOpt`）；C 抛出 `Incompatible buffer type.` 异常。

**预期结果**：三条输出逐条印证 4.1.3 ④ 的两条路径——`cast` 先检查后转型所以只给 `NullOpt`，而直接构造（[TensorDataImpl.hpp:L152-L159](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/detail/TensorDataImpl.hpp#L152-L159)）把同样的不匹配升级成异常。另外注意场景 A 里 `basePtr` 是一个彻头彻尾的假地址，转型照样成功——**`cast` 只认 bufferType 标签，不验证指针真伪**；「CUDA 可访问」的承诺由构造路径的纪律保证。本实践为源码阅读型验证，具体输出待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `exportData()`（无模板版）永远不会返回空，模板版却可能？
**答**：无模板版返回基类 `TensorData`，其兼容条件是 `bufferType != NONE`（[TensorData.hpp:L104-L107](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/TensorData.hpp#L104-L107)）；模板版要过派生类的 `IsCompatibleKind` 关卡，要求标签精确等于 `NVCV_TENSOR_BUFFER_STRIDED_CUDA`。

**练习 2**：视图 `TensorDataStridedCuda` 能否比原 Tensor 对象活得久？
**答**：不能。视图不拥有 `basePtr` 指向的内存，只是同一份 `NVCVTensorData` 的类型透镜；原对象析构后视图悬空。这就是 priv 算子把导出写成栈上局部变量的原因。

### 4.2 失败条件与异常抛出路径：priv 层的第一道闸

#### 4.2.1 概念说明

每个 priv 算子的 `operator()` 开头都有同一段「导出 + 判空 + 抛异常」。它防御的是什么？把 4.1 的事实串起来：

- 库内分配的 `nvcv::Tensor` 导出时 `bufferType` 恒为 `STRIDED_CUDA`，`cast` **必然成功**；
- 经 C API 包装外部内存时，[Tensor.cpp:L109-L136](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/Tensor.cpp#L109-L136) 的 `nvcvTensorWrapDataConstruct` 只接受 `STRIDED_CUDA`，其余直接抛 `NVCV_ERROR_INVALID_ARGUMENT`——**「CPU 张量」在构造入口就被拒绝了**；
- Python 侧更早：CPU 数组连 `as_tensor` 的 `ExternalBuffer` 校验都过不去（u2-l4）。

所以「导出失败」在今天是一条**防御性**路径：`NVCVTensorBufferType` 是个可扩展的枚举，一旦将来出现新形态（比如某种压缩布局），所有 priv 算子的判空分支会立刻把新形态挡在门外并给出统一、明确的错误消息，而不是让 kernel 拿着错误假设的指针狂奔。这段代码是「现在多余、将来救命」的典型。

#### 4.2.2 核心流程

```text
priv 算子 operator()(stream, in, out, ...)
  ├─ auto inData = in.exportData<nvcv::TensorDataStridedCuda>();
  ├─ if (inData == nullptr)
  │     throw nvcv::Exception(ERROR_INVALID_ARGUMENT, "Input must be cuda-accessible, pitch-linear tensor")
  ├─ （对 out 重复同样两步）
  └─ NVCV_CHECK_THROW(legacyOp->infer(*inData, *outData, ..., stream))   ← 解引用进入 kernel 世界
```

异常的传播路径承接 u5-l1：C++ 异常在 priv 层抛出 → 向上穿过 C++ 类层 → 到达 C API 边界时被 `ProtectCall` 捕获并翻译成 `NVCVStatus` 错误码（本仓 `nvcvTensorExportData` 自己的 C API 包装见 [Tensor.cpp:L229-L242](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/Tensor.cpp#L229-L242)，同一个 `ProtectCall` 模式）→ Python 绑定层再把错误码转成 Python 异常。u6-l2 会专门解剖这条翻译链。

#### 4.2.3 源码精读

**① Flip 的 Tensor 版三段式。** [OpFlip.cpp:L42-L56](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpFlip.cpp#L42-L56)：导出输入、判空抛 `"Input must be cuda-accessible, pitch-linear tensor"`，对输出重复，最后 `NVCV_CHECK_THROW` 把 infer 的返回码也纳入异常翻译。这段是全仓库几十个算子的公共模板。

**② Resize 的同构代码 + 一个新知识点。** [OpResize.cpp:L46-L60](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpResize.cpp#L46-L60) 与 Flip 逐行同构。而变长批重载里的注释 [OpResize.cpp:L92-L95](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpResize.cpp#L92-L95) 值得细读：快速路径需要**逐图尺寸**，但导出结构里的 `imageList` 是设备内存，CPU 侧读不到，只能通过批句柄 `in[i].size()` 逐个拿——这是「双面结构」（u2-l3）带来的真实约束，不是设计缺陷。

**③ 变长批的导出多一个 stream 参数。** [OpFlip.cpp:L63-L77](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpFlip.cpp#L63-L77)：`in.exportData<nvcv::ImageBatchVarShapeDataStridedCuda>(stream)`。为什么 Tensor 版不要 stream、变长批版要？答案在 4.4.3。

#### 4.2.4 代码实践

**实践目标**：在 Python 层复现「非 CUDA 数据被拒」，并确认它死在比 `exportData` 更早的地方。

**操作步骤**：

1. `python -c "import cvcuda, numpy as np; cvcuda.flip(cvcuda.as_tensor(np.zeros((4,8,3), np.uint8)), 1)"`（CPU 数组）。
2. 再跑一次合法版本：用 `numpy-cuda` 风格的 GPU 数组或 `torch.cuda` 张量包装同样的形状。

**需要观察的现象**：第 1 步在 `as_tensor` 处即抛出类型/设备相关的 TypeError；第 2 步正常执行。

**预期结果**：CPU 数据在 Python 绑定层的 `ExternalBuffer` 设备校验就被拦截（u2-l4 讲过的入口三座桥），**根本轮不到** priv 层的 `exportData` 判空——这印证了 4.2.1 的分层防御描述。具体报错文案待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：既然当前枚举下 priv 判空分支「永远走不到」，可以删掉它吗？
**答**：不该删。`NVCVTensorBufferType` 是可扩展契约；判空分支是每个算子对新 buffer 形态的默认拒收策略，删除后新增形态会静默落入按 strided-CUDA 假设寻址的 kernel，产生越界读写。

**练习 2**：priv 层为什么选「抛异常」而不是「返回错误码」？
**答**：priv 层是纯 C++ 内部实现，异常是最自然的错误传播方式；到 C ABI 边界才由 `ProtectCall` 统一降级为 `NVCVStatus`。两种错误风格各守一层，这正是 u5-l1「C ABI 是异常不能穿越的界碑」的具体体现。

### 4.3 TensorDataAccess：安全地计算任意像素地址

#### 4.3.1 概念说明

拿到 `TensorDataStridedCuda` 后你当然可以手写 \(base + n s_N + h s_H + w s_W + c s_C\)，但这有四个坑：维度索引写错、布局是 NCHW 而不是 NHWC、某个维度（比如 N）根本不存在、stride 单位忘了是字节。`TensorDataAccess` 家族（[TensorDataAccess.hpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/TensorDataAccess.hpp)）把这些坑全部填掉：它按**布局标签**（u2-l1 讲过的 N/H/W/C 字母）而非数字下标去查 stride，并提供 `sampleData`/`rowData`/`chData`/`planeData` 等语义化寻址函数。

访问器也是三层结构，与视图类一一对应但多一层「图像语义」：

| 访问器 | 新增能力 | 典型问题域 |
|---|---|---|
| `TensorDataAccessStrided` | `numSamples`/`sampleStride`/`sampleData` | 任意批张量 |
| `TensorDataAccessStridedImage` | `numCols/numRows/numChannels`、`rowStride/colStride/chStride`、`rowData/chData` | 图像张量（HWC 或 CHW） |
| `TensorDataAccessStridedImagePlanar` | `numPlanes`/`planeStride`/`planeData` | 需要「平面」视角时 |

三层都是 `Create()` 工厂 + `Optional` 返回：先 `IsCompatible` 检查布局是否答得起这套问题（比如布局里没有 H/W/C 标签就当不了「图像」），答不起就返回 `NullOpt`。

#### 4.3.2 核心流程

用访问器定位一个 NHWC 像素 \((n, h, w, c)\)：

\[
\text{ptr} = \text{sampleData}(n) \;+\; \text{rowStride}\cdot h \;+\; \text{colStride}\cdot w \;+\; \text{chStride}\cdot c
\]

两个关键设计决策（读源码时最容易忽略、却最能体现工程质量的地方）：

1. **维度缺失时 stride 返回 0 而不是报错。** 比如 `HWC` 布局没有 N 维，`sampleStride()` 返回 0，于是 `sampleData(0)` 恰好落在 `basePtr` 上——同一套代码无需分支即可服务「带批/不带批」两种输入。
2. **每个 `xxxEData(i)` 都提供双参重载** `xxxEData(i, base)`：可以相对任意基址计算。kernel 里常见用法是先用 `sampleData(n)` 跳到某张图，再以它为基址算行——两层乘法分开做，避免一次性大偏移。

还有一个反直觉的点：**Planar 访问器同时接受 NHWC 和 NCHW**。看 [TensorShapeInfo.hpp:L357-L384](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/TensorShapeInfo.hpp#L357-L384)，兼容条件是 `isChannelFirst() || isChannelLast()`——两种都行。区别在运行时：交错（channel-last）时 `numPlanes()` 固定为 1、`planeStride()` 为 0（[TensorShapeInfo.hpp:L418-L438](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/TensorShapeInfo.hpp#L418-L438) 与 [TensorDataAccess.hpp:L443-L455](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/TensorDataAccess.hpp#L443-L455)）；平面（channel-first）时 `numPlanes()==C`、`planeStride()==stride(C 维)`。**同一个访问器类，交错时退化为纯 N/H/W 视图**——这正是 AGENTS.md 里「算子默认同时支持 NHWC/CHW」在访问层的落点。

#### 4.3.3 源码精读

**① 样本层。** [TensorDataAccess.hpp:L110-L121](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/TensorDataAccess.hpp#L110-L121) 的 `sampleStride()`：先 `infoLayout().idxSample()` 按标签找 N 维的位置，找不到返回 0。[L129-L145](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/TensorDataAccess.hpp#L129-L145) 的 `sampleData(n, base)` 就是 `base + sampleStride*n`，带 `assert` 越界检查（注意是 assert，发布构建下不设防——算子层必须自己保证 n 合法）。

**② 图像层。** [TensorDataAccess.hpp:L273-L320](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/TensorDataAccess.hpp#L273-L320) 是 `chStride/colStride/rowStride` 三兄弟，结构与 `sampleStride` 完全一致：按标签查维、缺失归零。注意 `colStride` 管的是**宽度方向**（名字里的 column=w），别与通道混淆。

**③ 平面层与工厂。** [TensorDataAccess.hpp:L443-L479](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/TensorDataAccess.hpp#L443-L479)：`planeStride()` 只在 channel-first 时返回真值，否则 0。[L617-L640](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/TensorDataAccess.hpp#L617-L640)：`TensorDataAccessStridedImagePlanar::Create` 先 `IsCompatible`（strided ✓ 且图像形状 ✓ 且平面形状 ✓）再构造，失败给 `NullOpt`。

**④ kernel 工厂里的真实调用。** [TensorWrap.hpp:L471-L480](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/cuda_tools/TensorWrap.hpp#L471-L480) 的 `CreateTensorWrapNHW`：内部正是 `TensorDataAccessStridedImagePlanar::Create`，抽出 `sampleStride` 和 `rowStride` 塞进设备端包装器。也就是说，**你在 kernel 里用的每一个包装器，出厂前都经过访问器的布局校验**。

#### 4.3.4 代码实践

**实践目标**：创建真实张量，用访问器计算 \((n{=}1,h{=}3,w{=}5,c{=}2)\) 的设备地址，读回验证与手写公式一致。

**操作步骤**（示例代码，保存为 `addr_demo.cpp`）：

```cpp
// 示例代码：需要 CUDA 运行时与 libnvcv_types（按 u1-l3 构建后链接）
#include <nvcv/Tensor.hpp>
#include <nvcv/TensorDataAccess.hpp>
#include <cuda_runtime.h>
#include <iostream>
#include <vector>

int main()
{
    const int N = 2, H = 4, W = 8, C = 3;
    nvcv::Tensor tensor({{N, H, W, C}, "NHWC"}, nvcv::DataType{NVCV_DATA_TYPE_U8});

    auto dev = tensor.exportData<nvcv::TensorDataStridedCuda>();
    if (dev == nullptr) { std::cerr << "export failed\n"; return 1; }

    auto acc = nvcv::TensorDataAccessStridedImage::Create(*dev);
    if (acc == nullptr) { std::cerr << "not an image tensor\n"; return 1; }

    // 主机侧按同一 stride 布局填一个可预测的字段
    const size_t span = acc->sampleStride() * (N - 1) + acc->rowStride() * (H - 1)
                      + acc->colStride() * (W - 1) + acc->chStride() * (C - 1) + 1;
    std::vector<uint8_t> host(span, 0xAA);               // padding 填 0xAA
    for (int n = 0; n < N; ++n)
        for (int h = 0; h < H; ++h)
            for (int w = 0; w < W; ++w)
                for (int c = 0; c < C; ++c)
                    host[n * acc->sampleStride() + h * acc->rowStride()
                       + w * acc->colStride() + c * acc->chStride()]
                        = static_cast<uint8_t>((n * 131 + h * 37 + w * 7 + c) & 0xFF);

    cudaMemcpy(dev->basePtr(), host.data(), span, cudaMemcpyHostToDevice);

    // 访问器路径：先跳到样本 1，再以它为基址走行/列/通道
    uint8_t *p = acc->rowData(3, acc->sampleData(1))
               + acc->colStride() * 5 + acc->chStride() * 2;

    // 期望值：用与填值循环相同的手写公式取同一字节
    const size_t off = 1 * acc->sampleStride() + 3 * acc->rowStride()
                     + 5 * acc->colStride()   + 2 * acc->chStride();

    uint8_t got = 0;
    cudaMemcpy(&got, p, 1, cudaMemcpyDeviceToHost);
    std::cout << "accessor=" << +got << " expect=" << +host[off] << "\n";
    return 0;
}
```

编译参考：

```bash
g++ -std=c++17 addr_demo.cpp -I src/nvcv/src/include -I <CUDA_HOME>/include \
    -L build-rel/lib -lnvcv_types -lcudart -o addr_demo && ./addr_demo
```

**需要观察的现象**：打印出的 `accessor=` 值等于手算下标在 `host` 里的值；再打印 `acc->rowStride()` 观察——`W*C = 24` 字节，但行距通常是 32（u2-l1 讲过的纹理对齐），说明 padding 字节真实存在且被跳过。

**预期结果**：两条寻址路径（访问器 vs 手写公式）给出同一字节，验证 \(\text{ptr} = \text{base} + s_N + 3 s_H + 5 s_W + 2 s_C\)。本程序未在本讲义写作环境中运行，输出待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：程序里 `expect` 用的是与填值循环完全相同的下标公式，这样比对岂不是「自证循环」？交叉验证到底验证了什么？
**答**：被验证的不是公式本身，而是**访问器路径**（`sampleData`+`rowData`+`colStride`+`chStride` 组合出的指针 `p`）确实落在我们定义的那个字节上——即访问器对各标签维度的语义解释与手写公式一致。若把布局换成 `NCHW` 而访问器内部查错了维，两条路径会立刻分叉。

**练习 2**：把布局改成 `"NCHW"`（形状 `{{2,3,4,8}}`）重跑，哪些 stride 会变？
**答**：`chStride` 从 1 变成一张平面的字节数（约 `rowStride*H`，含对齐），`colStride` 变为 1；`sampleStride` 数值也变。同一份访问器代码不用改一个字——这就是按标签查维的好处。

**练习 3**：对 `{{4,8,3},"HWC"}`（无 N 维）调用 `numSamples()` 和 `sampleData(0)` 各返回什么？
**答**：`numSamples()` 返回 1（[TensorShapeInfo.hpp:L125-L132](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/TensorShapeInfo.hpp#L125-L132) 的文档语义：非批张量视为 1 个样本）；`sampleStride()` 为 0，`sampleData(0)` 即 `basePtr`。

### 4.4 喂给 kernel：TensorWrap 与变长批的 stream 参数

#### 4.4.1 概念说明

访问器解决「主机侧怎么算地址」，但 `TensorDataAccess` 是重型 C++ 对象（内部还缓存 `TensorShape`），**不能直接传进 kernel**。kernel 需要的是能按值传递、只含基址和少量 stride 的 POD。这一层由 `cvcuda/cuda_tools/TensorWrap.hpp` 的 `TensorWrapT` 完成：主机侧用访问器校验并抽取 stride，kernel 里用 `src.ptr(n, y, x)` 解引用。变长批则再叠一层难题——每张图的基址/行距存在**设备端**数组里，导出动作本身包含异步拷贝，因此 `exportData` 需要 `stream`。

#### 4.4.2 核心流程

```text
Tensor 版（同步、纯元数据）:
  priv: exportData<TensorDataStridedCuda>()          ← 不碰 GPU
        │
        ├─ 主机: TensorDataAccessStridedImagePlanar   ← 校验布局、取 numCols/numRows、算 grid
        └─ 主机: cuda::CreateTensorWrapNHW<T,int32_t> ← 抽 basePtr/sampleStride/rowStride 进轻量 POD
                 │  按值传入 kernel
                 ▼
  device: dst.ptr(batch_idx, y, x)                   ← 乘加寻址

变长批版（导出本身可能入队 GPU 工作）:
  priv: exportData<ImageBatchVarShapeDataStridedCuda>(stream)
        ├─ 记录 event 栅栏（与上次写元数据的流同步）
        ├─ 若有脏区间: cudaMemcpyAsync 把 hostImagesBuffer/hostFormatsBuffer 的脏段 H2D
        └─ 填 imageList(设备指针)/hostFormatList(主机指针)/maxWidth/maxHeight/uniqueFormat
```

#### 4.4.3 源码精读

**① kernel 启动前的双保险。** [flip.cu:L153-L178](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/legacy/flip.cu#L153-L178)：`flipImpl` 先 `TensorDataAccessStridedImagePlanar::Create` 两份访问器（`NVCV_ASSERT` 兜底——布局在此前已被 priv 层保障），用 `outAccess->numCols()/numRows()` 定输出尺寸；随后一段容易被忽略的检查：`sampleStride * numSamples` 若超过 `int32_t` 上限就拒绝走窄位路径并报错。为什么执着于 32 位？看 [TensorWrap.hpp:L246-L259](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/cuda_tools/TensorWrap.hpp#L246-L259) 的注释——`doGetOffset` 先在窄位 `offset` 里累加坐标×步长，**推迟或避免 64 位乘法**，GPU 上 32 位整数乘加明显更便宜。这是「类型参数 `StrideType` 默认 `int64_t`、性能敏感处显式传 `int32_t`」的原因。

**② kernel 里的最终形态。** [flip.cu:L130-L149](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/legacy/flip.cu#L130-L149)：`runFlipKernel` 用访问器给的尺寸配 `blockSize(32,8,1)` 和 grid（`numSamples` 直接当 `gridDim.z`——每张图一个 z 切片），第四个启动参数正是贯穿全链路的 `stream`（u4-l1）。kernel 体（[flip.cu:L95-L103](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/legacy/flip.cu#L95-L103)）里寻址只剩一行：`*dst.ptr(batch_idx, dst_y, dst_x) = *src.ptr(batch_idx, src_y, dst_x);`——4.3 节的全部机制都被压缩进这个 `ptr`。

**③ TensorWrap 的构造：编译期/运行期混合 stride。** [TensorWrap.hpp:L151-L172](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/cuda_tools/TensorWrap.hpp#L151-L172)：从 `TensorDataStridedCuda` 抄 `basePtr`；模板参数包 `Strides...` 里填 `-1` 表示「运行期可变」（从张量抄进来），填具体数字则变成编译期常量（`assert(tensor.stride(i) == kStride[i])` 校验后直接用）。`__host__ __device__` 标记让它同一份代码两侧通用。

**④ 变长批导出全貌。** [priv/ImageBatchVarShape.cpp:L214-L263](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/priv/ImageBatchVarShape.cpp#L214-L263)：`ImageBatchVarShape::exportData(CUstream stream, NVCVImageBatchData &data)` 做四件事——填 `bufferType = NVCV_IMAGE_BATCH_VARSHAPE_BUFFER_STRIDED_CUDA` 与三个列表指针（`imageList`/`formatList` 在设备、`hostFormatList` 在主机，[ImageBatchData.hpp:L106-L156](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/ImageBatchData.hpp#L106-L156)）；对读取流做 event 等待（cvcuda 的流是非阻塞流，不与之前的宿主缓冲生产者隐式同步，[L226-L236](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/priv/ImageBatchVarShape.cpp#L226-L236)）；把脏区间内的每图 buffer/格式描述 `cudaMemcpyAsync` 到设备（[L238-L246](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/priv/ImageBatchVarShape.cpp#L238-L246)，记录 `m_evPostFence` 保护宿主缓冲）；最后填缓存的 `maxWidth/maxHeight/uniqueFormat`。**这就是导出需要 stream 的答案**：这次导出可能向该流入队 H2D 拷贝和 event 操作，必须与算子 kernel 同序。Tensor 的导出没有任何异步动作，自然不需要。

#### 4.4.4 代码实践

**实践目标**：源码阅读型实践——跟踪变长批 Flip 的元数据之旅，画出时序。

**操作步骤**：

1. 从 [OpFlip.cpp:L63-L86](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpFlip.cpp#L63-L86) 出发，列出 `exportData(stream)` 之后 `*input` 里四类字段（`imageList`/`hostFormatList`/`maxWidth`/`uniqueFormat`）各自被谁消费。提示：用 `rg "imageList\(\)|hostFormatList\(\)|maxWidth" src/cvcuda/priv/legacy/flip.cu`（或全 legacy 目录）找设备侧消费者。
2. 对照 [priv/ImageBatchVarShape.cpp:L238-L246](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/priv/ImageBatchVarShape.cpp#L238-L246)，回答：第二次对同一批调用 `exportData`（其间没有 `pushback`）还会发生 H2D 拷贝吗？
3. 把整条链画成时序图：Python 调用 → priv 导出（含 event 等待 + 脏段拷贝）→ infer → kernel 启动。

**需要观察的现象**：`imageList` 的消费者在 kernel/设备侧代码中；`hostFormatList` 与 `maxWidth` 的消费者在主机侧的启动配置代码中。

**预期结果**：问题 2 的答案是**不会**——`m_dirtyStartingFromIndex` 已推进到 `m_numImages`，脏区间为空，只剩 event 栅栏检查；这正是「同一批反复调用算子时导出开销趋近于零」的机制根源（u2-l3 说过 TensorBatch 的导出会向流调度拷贝，这里是同一思想在 ImageBatch 上的实现）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `CreateTensorWrapNHW` 只带 `sampleStride` 和 `rowStride` 两个 stride，通道和列去哪了？
**答**：`NHW` 包装器按「元素类型 `T`」寻址：`T` 往往是向量类型（如 `uchar3`），列方向一个 `T`、通道被打包进 `T` 内部，所以只需 N/H 两个步长（见 [TensorWrap.hpp:L471-L480](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/cuda_tools/TensorWrap.hpp#L471-L480) 的断言只查这两个）。需要显式按 C 寻址时改用 `CreateTensorWrapNHWC`（[L499-L510](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/cuda_tools/TensorWrap.hpp#L499-L510)）。

**练习 2**：`flipImpl` 里 `NVCV_ASSERT(inAccess)` 用 assert 而 priv 层用 throw，两道防线的分工是什么？
**答**：priv 层的 throw 面向用户输入（布局/dtype 不受支持，运行时必须拦截并给出可读错误）；`flipImpl` 的 assert 面向程序员的内部不变式（既然 priv 已放行，布局必然兼容），发布构建可关掉以省开销。

## 5. 综合实践

把本讲三条主线串成一个任务：**给「访问器 vs 手写公式 vs kernel」做一次三方对账**。

1. 用 4.3.4 的程序为基础，再增加第三条路径：不使用访问器，直接 `dev->stride(0..3)` 手写完整公式，三个结果并排打印（访问器 / 纯公式 / host 期望值）。
2. 把张量行距的人为影响加进来：对比 `W*C` 恰好对齐（如 `W=16, C=2`，`W*C=32`）与不对齐（`W=8, C=3`，`W*C=24`）两种形状下 `rowStride()` 的值，记录对齐规则与 u2-l1 的结论是否一致。
3. 进阶：仿照 `flipImpl` 的窄位检查，给你的程序加上 `sampleStride*N ≤ INT32_MAX` 的分支，并思考为什么超过后宁可报错也不静默换 64 位路径（提示：`CreateTensorWrapNHW` 的 `StrideType` 已经定死，类型收窄发生在校验之前会溢出）。
4. 全部通过后，回到 [OpFlip.cpp:L42-L56](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpFlip.cpp#L42-L56)，逐行标注你现在能说出每一行「为什么存在」。能全部说清，本讲目标达成。

本综合实践需要 CUDA 环境与按 u1-l3 完成的源码构建；在无 GPU 环境下可只完成第 4 步的源码标注。

## 6. 本讲小结

- `exportData` 是「对象 → 数据视图」的提纯：priv 实现里 `NVCVTensorData` 被填上 `basePtr`/`strides`/元数据，`bufferType` 由库内路径恒置为 `STRIDED_CUDA`，导出本身零 GPU 开销、不拥有内存。
- 模板版 `exportData<TensorDataStridedCuda>()` 的唯一失败出口是 `cast` 的 `IsCompatibleKind` 检查（返回 `NullOpt`）；直接构造不匹配视图则抛异常。当前枚举下这是防御未来 buffer 形态的闸门，priv 层判空后统一抛 `ERROR_INVALID_ARGUMENT`。
- `TensorDataAccess` 三层访问器按布局标签查维、维度缺失时 stride 归零，`sampleData/rowData/chData/planeData` 把地址公式包装成安全接口；`...ImagePlanar` 同时兼容 NHWC（`numPlanes=1`、`planeStride=0`）与 NCHW，是双布局支持在访问层的落点。
- kernel 侧由 `TensorWrap` 接棒：主机用访问器校验并抽取 stride 进轻量 POD，kernel 里 `ptr(n,y,x)` 一行完成寻址；窄位 `StrideType=int32_t` 是真实的性能取舍。
- `ImageBatchVarShape::exportData` 需要 `stream`：导出可能向流入队脏段 H2D 拷贝与 event 栅栏；脏区间机制让同批重复调用的导出开销趋近于零。

## 7. 下一步学习建议

本讲之后，你已经能从 priv 层一路读到 kernel 的寻址细节。接下来：

- **u5-l3（两种内核形态）**：本讲只解剖了 flip 一个 legacy kernel，下一讲系统对比 legacy `.cu` 与原生 `Op*.cu` 两种内核组织方式，`TensorWrap` 家族（`CreateTensorWrapNHWC/NCHW`）会在更多样本中出现。
- **提前翻阅 [src/cvcuda/include/cvcuda/cuda_tools/TensorWrap.hpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/cuda_tools/TensorWrap.hpp)**：连同同目录的 `MathWrappers.hpp`、`TypeTraits.hpp`，是读懂一切 kernel 的「词汇表」。
- **u6-l2（错误处理与符号版本）**：本讲 4.2 节埋下的 `ProtectCall`/`NVCVStatus` 翻译链在那里完整展开。
- 想看变长批设备侧 `imageList` 如何被 kernel 消费，可直接跳读 [src/cvcuda/priv/legacy/resize_var_shape.cu](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/legacy/resize_var_shape.cu)，把 4.4 的时序图补完。
