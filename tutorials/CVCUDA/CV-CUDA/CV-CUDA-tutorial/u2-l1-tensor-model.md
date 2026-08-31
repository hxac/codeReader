# 张量模型：Tensor、TensorShape 与 DataLayout

## 1. 本讲目标

学完本讲，你应该能够：

1. 用 Python 创建指定 `shape` / `dtype` / `layout` 的 `cvcuda.Tensor`，并读懂它背后的 C++ 类 `nvcv::Tensor`。
2. 说清 `TensorShape`、`TensorLayout`、`DataLayout`（Packing/Swizzle）三个名字相近但职责不同的概念。
3. 解释 stride（步长）的字节语义，理解 CV-CUDA 为什么默认把"行距"对齐到 32 字节。
4. 区分 HWC 与 CHW 布局，能手工计算任意像素在显存中的地址。

本讲是第二单元的地基：后面所有算子（resize、cvtColor……）的输入输出都是 Tensor，不理解张量模型就无法排查"图像错位""通道颠倒"这类经典问题。

## 2. 前置知识

### 2.1 承接上一讲

u1-l4 已经建立了仓库地图：算子有 C 头 / C++ 头 / priv 实现 / CUDA kernel 四层。本讲我们停在**数据类型层** `src/nvcv`——它贯穿所有层，是"数据与操作分离"原则中"数据"的那一半。

### 2.2 必备的几个基础概念

- **维度（dimension）与秩（rank）**：张量有几个下标就是几维。一张 RGB 图是 3 维（高、宽、通道），一个批次的 RGB 图是 4 维。
- **布局（layout）**：同样的 4 个数字 \((N, C, H, W)\)，通道维放最前是 NCHW（PyTorch 惯例），放最后是 NHWC（GPU 图像处理惯例，访存更友好）。**布局只是"每个下标的语义标签"，不改变数据本身**。
- **stride / 步长**：沿某一维移动一格，内存地址要前进多少**字节**。若 \(s_d\) 是第 \(d\) 维的 stride，则元素地址为：
  \[ \text{addr} = \text{basePtr} + \sum_{d=0}^{r-1} i_d \cdot s_d \]
  这个公式就是 CV-CUDA 源码注释里的原文，后面 4.3.3 会看到。
- **pitch-linear / 行距（row pitch）**：图像一整行占用的字节数。为了让显存访问对齐，GPU 库常把行距向上取整到 32 的倍数，此时行与行之间有"填充字节"，张量不再是紧密排列（compact）。
- **DLPack / `__cuda_array_interface__`**：跨框架共享显存的协议，u1-l2 已用过 `cvcuda.as_tensor`，本讲 4.1 会看到它在绑定层的实现位置。

> 术语澄清：本讲标题里的 **DataLayout** 是仓库里的一个真实头文件名（`DataLayout.hpp`），它定义的其实是**像素位打包**（Packing/Swizzle）体系，而张量维度布局定义在 `TensorLayout.hpp`。二者名字相近但不是一回事，4.2.3 会专门辨析。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [python/mod_cvcuda/nvcv/Tensor.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Tensor.cpp) | Python 绑定：`cvcuda.Tensor` 类、`as_tensor`、DLPack 导入导出、对象缓存接入 |
| [src/nvcv/src/include/nvcv/Tensor.hpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/Tensor.hpp) | C++ `nvcv::Tensor` 类声明（句柄式核心资源） |
| [src/nvcv/src/include/nvcv/TensorShape.hpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/TensorShape.hpp) | 形状 + 布局的绑定体 `TensorShape`，以及维度重排 `Permute` |
| [src/nvcv/src/include/nvcv/TensorLayout.hpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/TensorLayout.hpp) | 维度标签（N/C/F/D/H/W）与 `TensorLayout` 类、隐式布局表 |
| [src/nvcv/src/include/nvcv/DataLayout.hpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/DataLayout.hpp) | 像素位打包（Packing）、数据种类（DataKind）、Swizzle 等图像格式基础件 |
| [src/nvcv/src/priv/Tensor.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/priv/Tensor.cpp) | `nvcvTensorCalcStridedDataRequirements`：stride 与对齐的真实算法 |
| [src/nvcv/src/include/nvcv/TensorData.h](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/TensorData.h) | C 结构体 `NVCVTensorBufferStrided`：stride 数组 + 地址公式 |
| [samples/datatypes/tensor.py](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/datatypes/tensor.py) | 官方示例：6 种创建张量的写法 |

## 4. 核心概念与源码讲解

### 4.1 模块一：Tensor 是什么——从 Python 构造函数到 C++ 核心资源

#### 4.1.1 概念说明

`cvcuda.Tensor` 是 CV-CUDA 里最通用的数据容器：一块**位于 GPU 显存**、按 strided 方式寻址、带有形状/数据类型/（可选）布局元数据的 N 维数组。

它有三个要点值得先记住：

1. **数据永远在 GPU 上**。构造函数直接分配显存；想用 NumPy 读写，必须经由 DLPack 换到 torch/cupy（或拷回 CPU）。
2. **Python 侧不猜布局**。不传 `layout` 时布局是 `NONE`（`tensor.layout` 返回 `None`），而不是自动猜一个 NHWC。布局是**你告诉库**的语义信息。
3. **创建走缓存**。Python 端每次 `cvcuda.Tensor(...)` 会先查对象缓存（u4-l2 专题），命中就复用旧显存，这一步对用户完全透明。

#### 4.1.2 核心流程

一次 `cvcuda.Tensor((H, W, C), np.uint8, layout="HWC")` 的执行路径：

```text
Python: cvcuda.Tensor(shape, dtype, layout, rowalign)
   │
   ▼  pybind11
nvcvpy::priv::Tensor::Create()            # python/mod_cvcuda/nvcv/Tensor.cpp
   │  组装 nvcv::TensorShape（shape + layout）
   ▼
nvcv::Tensor::CalcRequirements()          # 计算 shape/dtype/stride/对齐/总字节数
   │
   ▼
nvcvpy::priv::Tensor::CreateFromReqs()    # 先查 Cache；未命中才 new + add
   │
   ▼
nvcv::Tensor(reqs, alloc)                 # C++ 侧真正分配显存
```

`nvcv::Tensor(nimages, imgsize, format)` 是另一条等价入口：由图像格式（如 RGB8）自动推出 NHWC 形状，适合"我就是想放一批等尺寸图片"的场景。

#### 4.1.3 源码精读

**Python 类的注册与构造函数**。[python/mod_cvcuda/nvcv/Tensor.cpp:526-551](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Tensor.cpp#L526-L551) 把 C++ 工厂函数注册成 Python 构造器，并暴露 `layout/shape/dtype/ndim/cuda()/reshape` 只读属性——这就是我们在 Python 里用的全部接口：

```cpp
py::class_<Tensor, std::shared_ptr<Tensor>, Container>(m, "Tensor", "Tensor")
    .def(py::init(&Tensor::CreateForImageBatch), "nimages"_a, "imgsize"_a, "format"_a, "rowalign"_a = 0, ...)
    .def(py::init(&Tensor::Create), "shape"_a, "dtype"_a, "layout"_a = std::nullopt, "rowalign"_a = 0, ...)
    .def_property_readonly("layout", ...)
    .def_property_readonly("shape", &Tensor::shape, ...)
    .def_property_readonly("dtype", &Tensor::dtype, ...)
    // numpy 和其他框架用 ndim，python 侧保持一致
    .def_property_readonly("ndim", &Tensor::rank, ...)
    .def("cuda", ..., "Reference to the Tensor on the CUDA device.")
```

注意第 539 行的注释：Python 用 `ndim`（与 NumPy 一致），C++ 用 `rank()`——同一事物两种语言各随其俗。

**创建逻辑**。[python/mod_cvcuda/nvcv/Tensor.cpp:71-83](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Tensor.cpp#L71-L83)：未指定布局时显式落到 `TENSOR_NONE`（这就是"不猜布局"的出处），然后统一交给 `CalcRequirements`：

```cpp
std::shared_ptr<Tensor> Tensor::Create(Shape shape, nvcv::DataType dtype,
                                       std::optional<nvcv::TensorLayout> layout, int rowalign)
{
    if (!layout)
    {
        layout = nvcv::TENSOR_NONE;   // 布局缺省 = 无布局，而不是猜测
    }
    nvcv::Tensor::Requirements reqs
        = nvcv::Tensor::CalcRequirements(CreateNVCVTensorShape(shape, *layout), dtype,
                                         rowalign == 0 ? nvcv::MemAlignment{}
                                                       : nvcv::MemAlignment{}.rowAddr(rowalign));
    return CreateFromReqs(reqs);
}
```

**缓存接入**。[python/mod_cvcuda/nvcv/Tensor.cpp:85-103](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Tensor.cpp#L85-L103)：`CreateFromReqs` 先按 `Key{reqs}`（形状+布局+dtype）查缓存，命中即复用，未命中才新建并登记。本讲只需知道"有这一层"，细节留给 u4-l2。

**按图像格式创建**。[python/mod_cvcuda/nvcv/Tensor.cpp:62-69](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Tensor.cpp#L62-L69) 是 `Tensor(nimages=5, imgsize=(640,480), format=Format.RGB8)` 的落点，它把 `rowalign` 翻译成 `MemAlignment{}.rowAddr(...)` 后同样走 `CalcRequirements`。布局从哪来？[src/nvcv/src/priv/Tensor.cpp:73-79](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/priv/Tensor.cpp#L73-L79) 先按 NCHW 组好形状，再经 `GetTensorLayoutFor(fmt, numImages)` 把维度重排到目标布局——**交错（interleaved）格式如 RGB8 得到 NHWC，平面（planar）格式得到 NCHW**，所以"由格式推布局"推的是符合该格式物理存放的布局，而非拍脑袋默认。

**C++ 侧的类**。[src/nvcv/src/include/nvcv/Tensor.hpp:41-93](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/Tensor.hpp#L41-L93)：`nvcv::Tensor` 继承自 `CoreResource<NVCVTensorHandle, Tensor>`——即它是 C 句柄 `NVCVTensorHandle` 的 RAII 包装（这正是 u1-l1 讲过的 C ABI 分层）：

```cpp
class Tensor : public CoreResource<NVCVTensorHandle, Tensor>
{
public:
    int         rank() const;         // 维度数
    TensorShape shape() const;        // 形状（含布局）
    DataType    dtype() const;        // 元素类型
    TensorLayout layout() const;      // 布局
    TensorData  exportData() const;   // 导出裸数据视图（u5-l2 专题）

    static Requirements CalcRequirements(const TensorShape &shape, DataType dtype,
                                         const MemAlignment &bufAlign = {});
    static Requirements CalcRequirements(int numImages, Size2D imgSize, ImageFormat fmt,
                                         const MemAlignment &bufAlign = {});
};
```

三个静态 `CalcRequirements` 重载（[Tensor.hpp:123-135](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/Tensor.hpp#L123-L135)）与三个构造函数（[Tensor.hpp:170-174](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/Tensor.hpp#L170-L174)）一一对应，是"先算需求、再按需求分配"的两步式设计。

**官方示例全览**。[samples/datatypes/tensor.py:26-58](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/datatypes/tensor.py#L26-L58) 覆盖了六种写法，值得一读：

```python
tensor1 = cvcuda.Tensor((224, 224, 3), np.uint8, layout="HWC")            # 单图
tensor2 = cvcuda.Tensor((10, 224, 224, 3), np.float32, layout="NHWC")     # 批
tensor3 = cvcuda.Tensor(nimages=5, imgsize=(640, 480), format=cvcuda.Format.RGB8)
tensor4 = cvcuda.Tensor((224, 224, 3), np.uint8, layout="HWC", rowalign=32)  # 行 32 字节对齐
tensor5 = cvcuda.Tensor((100, 50, 25), np.float32, layout="DHW")          # 通用 N 维
video   = cvcuda.Tensor((2, 30, 720, 1280, 3), np.uint8, layout="NDHWC")  # 视频
```

#### 4.1.4 代码实践

1. **实践目标**：亲手创建张量并确认元数据接口。
2. **操作步骤**（需要已安装 cvcuda wheel 与 numpy，GPU 环境）：

```python
# practice_meta.py
import numpy as np
import cvcuda

t1 = cvcuda.Tensor((224, 224, 3), np.uint8, layout="HWC")
t2 = cvcuda.Tensor((10, 224, 224, 3), np.float32, layout="NHWC")
t3 = cvcuda.Tensor(nimages=5, imgsize=(640, 480), format=cvcuda.Format.RGB8)
t4 = cvcuda.Tensor((100, 50, 25), np.float32, layout="DHW")   # 故意不给 layout 的对照组
t5 = cvcuda.Tensor((100, 50, 25), np.float32)

for t in (t1, t2, t3, t4, t5):
    print(t)                       # __repr__: <nvcv.Tensor shape=... dtype=...>
    print("  ndim =", t.ndim, "| layout =", t.layout)
```

3. **需要观察的现象**：`t3` 由格式推出的布局是什么？`t5` 的 `layout` 是否为 `None`？
4. **预期结果**：`t3.layout` 打印 `NHWC`（由 `Format.RGB8` 推得）；`t5.layout` 为 `None`。其余项的 shape/ndim 与构造参数一致。具体打印格式待本地验证。
5. 若无 GPU 环境，此实践无法运行，可改为纯阅读 `samples/datatypes/tensor.py` 并手写预测输出。

#### 4.1.5 小练习与答案

**练习 1**：`cvcuda.Tensor((4, 3, 256, 256), np.float32)`（不带 layout）创建的张量，`tensor.layout` 是什么？能把它直接喂给要求 NCHW 的算子吗？

> **答案**：是 `None`（布局 `TENSOR_NONE`）。布局是元数据而非数据排列，算子靠它判断各维语义，所以缺少布局时按布局分发的算子会拒绝或无法正确解释输入；应显式传 `layout="NCHW"`。

**练习 2**：`cvcuda.Tensor(nimages=5, imgsize=(640, 480), format=cvcuda.Format.RGB8)` 得到的 shape 是什么？

> **答案**：`(5, 480, 640, 3)`，布局 NHWC——`imgsize` 是 (宽, 高)，而 NHWC 形状中 H 在 W 前，注意别写反。

**练习 3**：Python 用 `ndim`，C++ 用什么？

> **答案**：`rank()`（[Tensor.hpp:53](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/Tensor.hpp#L53)）。绑定层特意在 [Tensor.cpp:536-539](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Tensor.cpp#L536-L539) 的注释里说明了这处语言习惯差异。

### 4.2 模块二：TensorShape 与 TensorLayout——形状和布局绑在一起

#### 4.2.1 概念说明

`TensorShape = Shape + TensorLayout`：一串维度长度，加上**等长**的语义标签串。例如 shape `(10, 224, 224, 3)` 配上标签串 `"NHWC"`，我们就知道第 0 维是批、第 3 维是通道。

六个标签字母是全仓库的通用语言（[TensorLayout.hpp:79-87](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/TensorLayout.hpp#L79-L87)）：

| 标签 | 含义 | 标签 | 含义 |
|------|------|------|------|
| `N` | Batch（批） | `H` | Height（高） |
| `C` | Channel（通道） | `W` | Width（宽） |
| `F` | Frame（帧） | `D` | Depth（深度） |

`TensorShape` 把两者绑定并强制**秩一致**，这解决了"shape 只是数字、不知道谁是通道"的歧义——算子实现里到处用 `layout.find('C')` 来定位通道维。

#### 4.2.2 核心流程

- 构造 `TensorShape(shape, layout)` 时校验：若布局不是 `NONE`，则 `shape.rank() == layout.rank()`，否则抛异常。
- 布局本身支持查询与裁剪：`find(label)` 定位某维、`startsWith/endsWith` 前后缀判断、`first(n)/last(n)/subRange` 取子布局——算子实现用它们判断"这个张量结尾是不是 HWC"。
- `Permute(src, dstLayout)` 按目标布局重排维度顺序（NCHW ↔ NHWC 的形状换算就靠它）。

#### 4.2.3 源码精读

**秩一致性校验**。[src/nvcv/src/include/nvcv/TensorShape.hpp:55-63](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/TensorShape.hpp#L55-L63)：

```cpp
TensorShape(ShapeType shape, TensorLayout layout)
    : m_shape(std::move(shape))
    , m_layout(std::move(layout))
{
    if (m_layout != TENSOR_NONE && m_shape.rank() != m_layout.rank())
    {
        throw Exception(Status::ERROR_INVALID_ARGUMENT, "Layout dimensions must match shape dimensions");
    }
}
```

**访问接口**。[TensorShape.hpp:126-160](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/TensorShape.hpp#L126-L160) 提供 `shape()` / `layout()` / `operator[](i)`（取第 i 维长度）/ `rank()`。打印格式见 [TensorShape.hpp:222-232](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/TensorShape.hpp#L222-L232)：`NHWC{10,224,224,3}`（无布局时只打印花括号部分）。

**维度重排**。[TensorShape.hpp:250-257](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/TensorShape.hpp#L250-L257) 的 `Permute` 调 C API `nvcvTensorShapePermute` 完成实际换算——注意它只重排**形状描述**，不搬数据。

**TensorLayout 类与标签查询**。[src/nvcv/src/include/nvcv/TensorLayout.hpp:98-189](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/TensorLayout.hpp#L98-L189)：`TensorLayout` 内部就是一段最多 `NVCV_TENSOR_MAX_RANK = 15`（[TensorLayout.h:34](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/TensorLayout.h#L34)）个字符的标签串。关键成员：

```cpp
constexpr char operator[](int idx) const;  // 第 idx 维的标签，如 layout[3] == 'C'
constexpr int  rank() const;               // 维度数
int find(char dimLabel, int start = 0) const;       // 定位标签，找不到返回 -1
bool startsWith(const TensorLayout &test) const;    // 例如 NHWC.startsWith(HWC) == true
TensorLayout last(int n) const;                     // 取最后 n 维的子布局
```

**预定义常量**。[TensorLayout.hpp:239-242](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/TensorLayout.hpp#L239-L242) 用宏展开 [TensorLayoutDef.inc](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/TensorLayoutDef.inc#L19-L58) 生成全套 `TENSOR_NHWC`、`TENSOR_NCHW`、`TENSOR_NDHWC`、`TENSOR_NCFDHW` 等常量——你能在 Python 里写 `layout="NDHWC"`，靠的正是这份清单。

**隐式布局表**。[TensorLayout.hpp:246-266](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/TensorLayout.hpp#L246-L266) 定义了按秩取"典型布局"的表 `{NONE, W, HW, NHW, NCHW, NCDHW, NCFDHW}`。注意两点：① 这是 C++ 侧 `GetImplicitTensorLayout` 用的表，**Python 构造张量并不经过它**（Python 走的是 4.1.3 里的 `TENSOR_NONE` 分支）；② 表中 3 维的典型是 `NHW` 而非 `HWC`——不要假设"3 维一定是 HWC"。

**Python 的 TensorLayout 对象**。[python/mod_cvcuda/nvcv/Tensor.cpp:482-520](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Tensor.cpp#L482-L520) 导出 `cvcuda.TensorLayout` 类：常量（`TensorLayout.NHWC` 等）做了驻留（interned，同一布局恒返回同一对象），支持 `==`/`hash`/`__repr__`；最后一行 `py::implicitly_convertible<py::str, nvcv::TensorLayout>()`（[Tensor.cpp:519](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Tensor.cpp#L519)）就是我们能直接传字符串 `"NHWC"` 的原因。`tensor.layout` 的 None 归一化见 [Tensor.cpp:296-307](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Tensor.cpp#L296-L307)。

> **辨析：DataLayout.hpp ≠ TensorLayout.hpp**
> [src/nvcv/src/include/nvcv/DataLayout.hpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/DataLayout.hpp) 回答的是另一个问题——**一个像素内部的比特如何打包**：
> - `Packing`（[DataLayout.hpp:42-168](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/DataLayout.hpp#L42-L168)）：如 `X8_Y8_Z8_W8`（四个 8 位通道）、`b4X4Y4Z4`（三个 4 位通道挤 16 位字）；
> - `DataKind`（[DataLayout.hpp:172-179](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/DataLayout.hpp#L172-L179)）：UNSIGNED/SIGNED/FLOAT/COMPLEX；
> - `Swizzle`（[DataLayout.hpp:219-278](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/DataLayout.hpp#L219-L278)）：通道顺序，如 `S_ZYXW` 表示 BGRX。
> 一句话总结：**TensorLayout 管"维度是什么"，Packing/Swizzle 管"一个像素的位是什么"**。后者主要被 ImageFormat（u2-l2）使用。

#### 4.2.4 代码实践

1. **实践目标**：验证布局元数据的查询能力与秩校验。
2. **操作步骤**：

```python
# practice_layout.py
import cvcuda

L = cvcuda.TensorLayout("NHWC")
print(L)                        # repr
print(L == cvcuda.TensorLayout("NCHW"), L == cvcuda.TensorLayout.NHWC)

t = cvcuda.Tensor((2, 8, 6, 3), __import__("numpy").uint8, layout="NHWC")
print(t.layout)                 # NHWC

# 触发 rank 不匹配（在底层构造 TensorShape 时抛出）—— 待本地验证
try:
    bad = cvcuda.Tensor((8, 6, 3), __import__("numpy").uint8, layout="NCHW")
except Exception as e:
    print("caught:", type(e).__name__, e)
```

3. **需要观察的现象**：`L == TensorLayout.NHWC` 是否为 True（驻留对象的同一性）；第 3 维布局标签；`bad` 是否抛异常及异常文案。
4. **预期结果**：`L` 打印 `NHWC`；`L == TensorLayout.NHWC` 为 `True`（常量驻留）；`bad` 因 3 维 shape 配 4 维布局而抛出错误（对应 4.2.3 的 `Layout dimensions must match shape dimensions`，具体异常类型待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：官方示例中 `cvcuda.Tensor((2, 30, 720, 1280, 3), np.uint8, layout="NDHWC")`（[tensor.py:56-58](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/datatypes/tensor.py#L56-L58)）的 720 和 1280 各是哪一维？

> **答案**：标签与维度按位置一一对应：N=2（批）、D=30（帧）、**H=720（高）、W=1280（宽）**、C=3（通道）。若把 shape 写成 `(2, 30, 1280, 720, 3)`，标签串不变，含义就变成"高 1280、宽 720"——布局只是贴标签，不校验数值的合理性。

**练习 2**：为什么算子实现里常用 `layout.find('C')` 而不是假设通道在最后一维？

> **答案**：CV-CUDA 的算子按仓库不变量需同时支持交错（HWC）与平面（CHW）布局，通道维位置因布局而异，`find('C')`（[TensorLayout.hpp:156](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/TensorLayout.hpp#L156)）才是通用写法。

**练习 3**：`TensorLayoutDef.inc` 里为什么没有 `HWC` 之外再定义一个 `CHW` 的镜像枚举，而是直接列出几十个常量？

> **答案**：布局本质是任意标签串，仓库只预定义常见组合（N、C、F、D、H、W 单标签 + 两两/多元组合，见 [TensorLayoutDef.inc:19-58](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/TensorLayoutDef.inc#L19-L58)），任意串可用 `TensorLayout("NWHC")` 这类构造自由创建（经 `nvcvTensorLayoutMake` 校验）。

### 4.3 模块三：stride 与内存布局——CalcRequirements 如何排布显存

#### 4.3.1 概念说明

Tensor 的显存不是简单地把元素紧密排平，而是由 `CalcRequirements` 算出一组**字节 stride**。两个关键设计：

1. **stride 以字节为单位**（最后一维 stride = `dtype.strideBytes()`），这使同一套机制能表达任意位宽的类型。
2. **行距对齐**：当布局以 `WC` 结尾（即"一行像素连续存放"）时，把行距向上取整到 2 的幂（默认取 GPU 纹理对齐属性，通常 32 字节）。对齐浪费一点显存，却换来 kernel 里更高效的合并访存（coalesced access）。

代价是：**张量可能是非紧凑的**。用 DLPack 把它交给别的框架时，对方拿到的就是一个带"空洞"的数组——如果对方代码假设连续内存就会出错。u1-l2 里"切批必须用真实行距"的伏笔正是来源于此。

#### 4.3.2 核心流程

`nvcvTensorCalcStridedDataRequirements` 的 stride 递推（自最后一维向前）：

```text
s[rank-1] = dtype.strideBytes()                      # 最后一维：一个元素的字节数
for d from rank-2 down to 0:
    if d == firstPacked - 1:                         # 该维是"一行"的行距
        s[d] = round_up_pow2( shape[d+1] * s[d+1], rowAlign )
    else:
        s[d] = shape[d+1] * s[d+1]                   # 紧密递推
总字节数 ≈ s[0] * shape[0]                            # 再按 baseAlign 对齐
```

其中 `firstPacked`：布局末两维是 `WC` 时取 `rank-2`，否则取 `rank-1`。用公式表达 HWC 布局 uint8 图像的行距：

\[ s_H = \lceil W \cdot C \rceil_{32} \quad(\text{字节}) \]

例如 \(W=6, C=3\)：一行 18 字节 → 行距取整为 32 字节，每行末尾填充 14 字节。

以 shape `(H=8, W=6, C=3)`、uint8 为例，两种布局的 stride（字节）：

| 布局 | shape | \(s_0\) | \(s_1\) | \(s_2\) | 缓冲区字节数 |
|------|-------|---------|---------|---------|--------------|
| HWC  | (8, 6, 3) | 32（对齐后行距） | 3 | 1 | 32×8 = 256 |
| CHW  | (3, 8, 6) | 8×32 = 256 | 32（对齐后行距） | 1 | 256×3 = 768 |

注意 CHW 里"行"是每个平面内的一行（H 方向），同样被对齐到 32 字节；紧凑排列本只需 144 字节，HWC 实际占 256 字节。

#### 4.3.3 源码精读

**地址公式的权威出处**。[src/nvcv/src/include/nvcv/TensorData.h:31-40](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/TensorData.h#L31-L40)：

```c
typedef struct NVCVTensorBufferStridedRec
{
    int64_t strides[NVCV_TENSOR_MAX_RANK];
    /** Pointer to memory buffer with tensor contents.
     * Element with type T is addressed by:
     * pelem = basePtr + shape[0]*strides[0] + ... + shape[rank-1]*strides[rank-1];
     */
    NVCVByte *basePtr;
} NVCVTensorBufferStrided;
```

这就是 2.2 节公式的源头。缓冲区类型（[TensorData.h:43-51](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/TensorData.h#L43-L51)）目前只有一种：`NVCV_TENSOR_BUFFER_STRIDED_CUDA`（GPU 上的 pitch-linear 等形平面）。

**对齐参数的来源**。[src/nvcv/src/priv/Tensor.cpp:124-143](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/priv/Tensor.cpp#L124-L143)：用户没给 `rowalign` 时，查询设备的 `cudaDevAttrTexturePitchAlignment`（注释写明"通常 32 字节"），再与 dtype 字节数的最小 2 的幂取最小公倍数；给了则必须是 2 的幂。基址对齐（[Tensor.cpp:145-172](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/priv/Tensor.cpp#L145-L172)）默认取 `cudaDevAttrTextureAlignment`（通常 512 字节）。

```cpp
if (userRowAlign == 0)
{
    // it usually returns 32 bytes
    NVCV_CHECK_THROW(cudaDeviceGetAttribute(&rowAlign, cudaDevAttrTexturePitchAlignment, dev));
    rowAlign = static_cast<int>(std::lcm(rowAlign, util::RoundUpNextPowerOfTwo(dtype.strideBytes())));
}
```

**stride 递推与 WC 判定**。[src/nvcv/src/priv/Tensor.cpp:174-189](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/priv/Tensor.cpp#L174-L189)：

```cpp
int firstPacked = CreateLast(reqs.layout, 2) == NVCV_TENSOR_WC ? std::max(0, rank - 2) : rank - 1;

reqs.strides[rank - 1] = dtype.strideBytes();
for (int d = rank - 2; d >= 0; --d)
{
    if (d == firstPacked - 1)
        reqs.strides[d] = util::RoundUpPowerOfTwo(reqs.shape[d + 1] * reqs.strides[d + 1], rowAlign);
    else
        reqs.strides[d] = reqs.strides[d + 1] * reqs.shape[d + 1];
}

AddBuffer(reqs.mem.cudaMem, reqs.strides[0] * reqs.shape[0], reqs.alignBytes);
```

读懂 `firstPacked` 这一行，就读懂了整段：`CreateLast(layout, 2)` 取布局末两维——若恰为 `WC`，说明"W×C 连续成一行"，于是把 **H 维的 stride**（即行距）对齐；否则（如 CHW）把每个平面内的行距对齐。

**DLPack 进出口的 stride 换算**。导入方向（`as_tensor` 包装外部 buffer）：[python/mod_cvcuda/nvcv/Tensor.cpp:146-156](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Tensor.cpp#L146-L156) 把 DLPack 的**元素 stride** 乘以每元素字节数换算成 nvcv 的**字节 stride**，并直接采用外部的 `basePtr + byte_offset`（零拷贝）：

```cpp
int elemStrideBytes = (tensor.dtype.bits * tensor.dtype.lanes + 7) / 8;
for (int d = 0; d < tensor.ndim; ++d)
    dataStrided.strides[d] = tensor.strides[d] * elemStrideBytes;
dataStrided.basePtr = reinterpret_cast<NVCVByte *>(tensor.data) + tensor.byte_offset;
```

同函数 [Tensor.cpp:137-144](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Tensor.cpp#L137-L144) 还有一道门槛：只接受 CUDA 可访问的 tensor，CPU 数组会被拒（`Only CUDA-accessible tensors are supported for now`）。导出方向（`tensor.cuda()`）：[Tensor.cpp:371-401](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Tensor.cpp#L371-L401) 把 strided 数据再打包成 DLPack 交还给 Python 生态，并且只允许 pitch-linear 数据。

#### 4.3.4 代码实践（本讲主实践）

> NumPy 本身无法直接访问显存，所以"用 numpy 涂渐变色"的路线是：**numpy 在 CPU 生成数据 → torch 上传 GPU → `as_tensor` 零拷贝纳管 → 处理后再读回 CPU 保存**。这也是官方示例 `tensor.py:44-53` 的标准姿势。

1. **实践目标**：创建 NHWC uint8 张量并涂渐变色导出成图；再用 NCHW 复刻，对比两种布局下的 shape 与 stride。
2. **操作步骤**：

```python
# practice_gradient.py
# 依赖: pip install cvcuda-cu12 numpy torch pillow   (CUDA 13 用户装 cvcuda-cu13)
import numpy as np
import torch
import cvcuda
from PIL import Image

H, W, C = 128, 256, 3

# ---- 第 1 步：numpy 在 CPU 生成渐变 ----
grad = np.zeros((H, W, C), dtype=np.uint8)
grad[..., 0] = np.linspace(0, 255, W, dtype=np.uint8)            # R 随 x 渐变
grad[..., 1] = np.linspace(0, 255, H, dtype=np.uint8)[:, None]   # G 随 y 渐变
grad[..., 2] = 128                                               # B 固定

# ---- 第 2 步：上传 GPU，零拷贝包装为 NHWC 张量 ----
t_nhwc = cvcuda.as_tensor(torch.from_numpy(grad).to("cuda"), "NHWC")
print("NHWC:", t_nhwc.shape, t_nhwc.layout, t_nhwc.dtype)

# ---- 第 3 步：经 DLPack 读回并保存 ----
out = torch.as_tensor(t_nhwc.cuda(), device="cuda")
Image.fromarray(out.cpu().numpy()).save("gradient_nhwc.png")

# ---- 第 4 步：NCHW 版本（同一幅图，通道前置） ----
torch_nchw = torch.from_numpy(grad).permute(2, 0, 1).contiguous().to("cuda")
t_nchw = cvcuda.as_tensor(torch_nchw, "NCHW")
print("NCHW:", t_nchw.shape, t_nchw.layout, t_nchw.dtype)
out2 = torch.as_tensor(t_nchw.cuda(), device="cuda")
Image.fromarray(out2.permute(1, 2, 0).cpu().numpy()).save("gradient_nchw.png")

# ---- 第 5 步：观察"行距对齐"—— 用小尺寸触发填充 ----
small_hwc = cvcuda.Tensor((8, 6, 3), np.uint8, layout="HWC")     # 紧凑只需 144B
small_chw = cvcuda.Tensor((3, 8, 6), np.uint8, layout="CHW")
a = torch.as_tensor(small_hwc.cuda(), device="cuda")             # uint8: 元素步长==字节步长
b = torch.as_tensor(small_chw.cuda(), device="cuda")
print("HWC shape/strides:", tuple(a.shape), a.stride())
print("CHW shape/strides:", tuple(b.shape), b.stride())
```

3. **需要观察的现象**：
   - 两张 PNG 内容是否一致（视觉上应为同一幅"左上暗、右下亮"的双向渐变）；
   - `t_nhwc.shape` 与 `t_nchw.shape` 分别是 `(128, 256, 3)` 与 `(3, 128, 256)`；
   - 第 5 步两个 `stride()` 的输出。
4. **预期结果**：
   - `t_nhwc.layout` 打印 `NHWC`，`t_nchw.layout` 打印 `NCHW`；
   - 小张量的 stride 预期（按 4.3.2 的公式，rowAlign=32 时）：HWC `(8,6,3)` → `(32, 3, 1)`；CHW `(3,8,6)` → `(256, 32, 1)`。若你的 GPU 纹理对齐属性不是 32 字节，数值会随之变化——这正是 4.3.3 中 `cudaDeviceGetAttribute` 的意义。以上数值待本地验证。
   - 附加思考：紧凑排列的 `(8,6,3)` 只需 \(8×6×3=144\) 字节，而 stride 显示每行 32 字节、共 256 字节——多出的就是行距填充。
5. 注意第 2、4 步用了 `contiguous()`：`permute` 后的 torch 张量是非连续的，DLPack 导入要求 stride 满足不重叠且覆盖完整（见 4.3.3 的导入路径），不连续视图常被拒绝——具体报错行为待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：一个 NHWC、float32、shape `(1, 480, 640, 3)` 的张量，紧凑字节数与按 32 字节行距对齐后的字节数各是多少？

> **答案**：紧凑 \(1×480×640×3×4 = 3{,}686{,}400\) 字节。对齐时行字节数 \(640×3×4=7680\) 已是 32 的倍数，无需填充，总字节数不变。**只有当行字节数不是 32 倍数时才会产生填充**。

**练习 2**：为什么 CHW 布局的 `(3, 8, 6)` 张量中 H 维 stride 也是 32 而不是 6？

> **答案**：布局末两维是 `HW` 而非 `WC`，`firstPacked = rank-1`，于是对 `d == firstPacked-1`（即 H 维）的 stride 做 `RoundUpPowerOfTwo(6, 32) = 32`（[priv/Tensor.cpp:174-187](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/priv/Tensor.cpp#L174-L187)）。对齐的是"每个平面内的行距"，与 HWC 的"跨通道行距"位置不同、目的一致。

**练习 3**：`as_tensor` 一个 CPU 上的 numpy 数组会发生什么？

> **答案**：抛错。导入路径在 [python/mod_cvcuda/nvcv/Tensor.cpp:137-144](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Tensor.cpp#L137-L144) 检查 `IsCudaAccessible`，否则抛 `Only CUDA-accessible tensors are supported for now`。必须先上传 GPU（如 `torch.from_numpy(x).to("cuda")`）。

## 5. 综合实践

**任务：手工按地址公式"裸算"像素，验证 stride 语义。**

写一个脚本 `addr_check.py`，把本讲三个模块串起来：

1. 用模块一的两种构造方式各建一个张量：`cvcuda.Tensor((4, 6, 3), np.uint8, layout="HWC")` 与 `cvcuda.Tensor(nimages=1, imgsize=(6, 4), format=cvcuda.Format.RGB8)`，打印二者的 shape/layout/dtype，确认它们语义等价。
2. 用模块二的知识回答：这两个张量的布局标签串分别是什么？`imgsize=(6,4)` 里 6 是 W 还是 H？（对照 4.1.5 练习 2 的答案自检。）
3. 用模块三的地址公式做验证：把张量经 `torch.as_tensor(t.cuda(), device="cuda")` 拿到可索引视图 `a`，先把内容清零、再给 `a[2, 1, 0]`（第 2 行第 1 列的 R 通道）写入 255；然后 `flat = a.view(-1)`（uint8 下每个元素 1 字节），检查 `flat[2 * a.stride(0) + 1 * a.stride(1)]` 是否恰好是 255。
   - 若成立，说明 torch 的元素索引与 `basePtr + Σ i_d·s_d` 的字节公式完全一致（uint8 时元素步长=字节步长）；
   - 再把布局换成 CHW 重做一遍，体会"同一像素 (y=2, x=1, c=0) 的线性偏移完全不同"。
4. 记录两张表的偏移量计算过程，作为你个人的"布局换算速查卡"。

预期：两种布局下写入的 255 都能被公式命中；HWC 的偏移为 \(2×32 + 1×3 = 67\)（W=6 时行距对齐到 32），CHW 的偏移为 \(0×s_C + 2×32 + 1×1 = 65\)。具体数值待本地验证（依赖设备的纹理对齐属性）。

## 6. 本讲小结

- `cvcuda.Tensor` = GPU 显存 + shape + dtype +（可选）layout；Python 构造统一走 `Tensor::Create → CalcRequirements → CreateFromReqs`，中间接入对象缓存（[python/mod_cvcuda/nvcv/Tensor.cpp:71-103](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Tensor.cpp#L71-L103)）。
- `TensorShape = Shape + TensorLayout`，构造时强制秩一致；六个标签 N/C/F/D/H/W 是全仓库的维度语言，`find('C')` 等查询是算子支持双布局的基础。
- **TensorLayout（维度是什么）与 DataLayout.hpp 里的 Packing/Swizzle（一个像素的位是什么）是两套体系**，别被文件名迷惑。
- stride 以**字节**为单位，地址公式 \(\text{addr} = \text{basePtr} + \sum i_d s_d\) 出自 [TensorData.h:31-40](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/TensorData.h#L31-L40)。
- 行距默认对齐到 GPU 纹理对齐属性（通常 32 字节，[priv/Tensor.cpp:124-143](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/priv/Tensor.cpp#L124-L143)），因此张量常是**非紧凑**的——跨框架传数据时必须尊重真实 stride。
- Python 不猜布局：不传 `layout` 就是 `NONE`；`as_tensor` 只接受 CUDA 可访问的 buffer，stride 在元素/字节两级之间换算。

## 7. 下一步学习建议

- **下一讲（u2-l2）**：`DataType` 与 `ImageFormat`——本讲的 `dtype` 参数背后的位宽/通道/打包体系（`DataLayout.hpp` 的真正主场），以及 `cvcuda.Format.RGB8` 这类格式常量如何由 Packing + Swizzle 组合而成。
- **u2-l4**：DLPack 互操作的完整地图（numpy/cupy/torch 三方零拷贝），本讲 4.3.3 只看了导入导出的 stride 换算函数。
- **源码延伸阅读**：通读 [src/nvcv/src/priv/Tensor.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/priv/Tensor.cpp) 的 `nvcvTensorCalcStridedDataRequirements` 全函数，再对照 [src/nvcv/src/priv/TensorWrapDataStrided.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/priv/TensorWrapDataStrided.cpp#L65-L101) 里对外部 buffer stride 的合法性检查（"pitch 必须 ≥ 上一维 stride×长度"），理解包装外部内存时的约束来源。
