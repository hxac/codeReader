# 数据类型与图像格式：DataType、TensorLayout、ImageFormat

> 本讲是第二单元「核心数据类型」的第 2 讲。上一讲（u2-l1）我们搞清了 `Tensor` 的形状、布局与 stride；本讲往更深处走一步：**张量里的一个元素到底是什么、图像的一格像素又到底是什么**。这两个问题分别由 `DataType` 与 `ImageFormat` 回答，它们是 nvcv 类型层（`src/nvcv`）的两块基石。

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `DataType` 的两个组成轴 —— `DataKind`（这组比特如何解释成数）与 `Packing`（这些比特如何分组给各通道），并能推算出任意类型的位数、通道数、字节数与对齐。
2. 说出 `ImageFormat` 在 `DataType` 之上额外携带了什么：颜色模型 / 颜色规范、色度子采样、内存布局（pitch/block-linear）、通道顺序（swizzle）、多平面结构与 alpha 语义。
3. 掌握格式命名规律与组合规则：`RGB8` 与 `RGB8p` 的差异、`Y8` 与 `U8` 内存相同但语义不同、`NV12` 为什么不能直接转成 Tensor、交错格式对应 `NHWC` 而平面格式对应 `NCHW`。
4. 会用 Python API（`cvcuda.Type`、`cvcuda.Format`、`cvcuda.Tensor`）检查一个格式/类型的通道数、每通道位数与推断出的布局，并理解 `cvcuda.Type` 与 numpy dtype 的互通机制。

## 2. 前置知识

本讲假设你已学完 u2-l1（张量模型）。下面用通俗语言补几个新概念：

- **元素（element）与像素（pixel）**：张量是 N 维数组，数组里每个格子叫一个元素；图像是二维像素阵列，每个像素由一个或多个**通道（channel）**组成（如 RGB 三个通道）。CV-CUDA 允许"一个元素打包多个通道"，比如 `3U8` 类型的一个元素就是 R、G、B 三个字节。
- **打包（packing）**：描述"一个像素的比特如何切开分给各通道"。例如 `X8_Y8_Z8` 表示 3 个通道、每通道 8 比特、共 24 比特；`X8_Y8_Z8_W8` 是 4 通道 32 比特。也有非均匀切法，如 `X5Y6Z5`（16 位 RGB565）。
- **数据种类（data kind）**：描述"每组比特按什么数解释"——无符号整数、有符号整数、浮点、复数。同样 8 个比特，`UNSIGNED` 解释为 0~255，`SIGNED` 解释为 -128~127。
- **平面（plane）**：图像数据在显存中的分块。单平面交错（interleaved）格式把 RGB 紧挨着放（RGBRGB...）；平面（planar）格式把每个通道各放一块（RRR...GGG...BBB...）；YUV 格式常见双平面（Y 一块、UV 交错一块）。
- **色度子采样（chroma subsampling）**：人眼对亮度敏感、对色度不敏感，视频格式常把色度（UV）分辨率减半，如 4:2:0。`NV12` 就是 420 子采样的双平面格式。
- **swizzle（通道搅拌）**：描述内存中通道的物理顺序与逻辑顺序的对应。`S_XYZW` 表示内存顺序就是第 1/2/3/4 通道；`S_ZYXW` 表示内存里先放第 3 通道（B）再放第 2（G）、第 1（R）——这正是 BGR 与 RGB 的区别所在。
- **numpy dtype**：Python 里描述数组元素类型的对象（`np.uint8`、`np.float32`...）。CV-CUDA 的 Python 绑定把 `DataType` 与 numpy dtype 打通了，本讲 4.4 会讲实现。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/nvcv/src/include/nvcv/DataType.hpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/DataType.hpp) | C++ 类 `nvcv::DataType`：元素类型的公开 API 与全部 `TYPE_*` 常量 |
| [src/nvcv/src/include/nvcv/DataLayout.hpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/DataLayout.hpp) | `DataKind` / `Packing` / `Swizzle` / `MemLayout` 等枚举定义——DataType 与 ImageFormat 共用的"积木" |
| [src/nvcv/src/include/nvcv/ImageFormat.hpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/ImageFormat.hpp) | C++ 类 `nvcv::ImageFormat`：图像格式的公开 API 与全部 `FMT_*` 常量（带详尽注释） |
| [src/nvcv/src/include/nvcv/ImageFormat.h](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/ImageFormat.h) | C API 头：每个预定义格式的位域级 `#define`，是格式"解剖图"的第一现场 |
| [src/nvcv/src/priv/DataType.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/priv/DataType.cpp) | DataType 的私有实现：位数、字节数、对齐的真实计算公式 |
| [python/mod_cvcuda/nvcv/DataType.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/DataType.cpp) | `cvcuda.Type` 的 pybind11 导出，以及与 numpy dtype 的双向转换 |
| [python/mod_cvcuda/nvcv/ImageFormat.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/ImageFormat.cpp) | `cvcuda.Format` 枚举的 pybind11 导出 |
| [python/mod_cvcuda/gen_imgformat_list.sh](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/gen_imgformat_list.sh) | 从 C 头文件自动生成 Python 枚举清单的脚本 |
| [python/mod_cvcuda/nvcv/Tensor.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Tensor.cpp) | `cvcuda.Tensor` 的 Python 构造入口，含"按格式建张量"路径 |
| [tests/cvcuda/python/test_tensor.py](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_tensor.py) | 格式 → 形状/布局推断的官方测试断言（本讲的"标准答案"） |
| [samples/datatypes/conversions.py](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/datatypes/conversions.py) | Image ↔ Tensor 互转示例，含 NV12 转换失败的演示 |

## 4. 核心概念与源码讲解

### 4.1 模块一：DataType —— 一个元素有多少位、这些位如何分组

#### 4.1.1 概念说明

`DataType` 描述的是**张量元素**的类型：这个元素占多少比特、切成几路通道、每路多少比特、按什么数解释。它回答的是纯粹的"位级"问题，不携带任何颜色含义。

它由两个正交的轴组成：

- **`DataKind`**：比特的解释方式（无符号 / 有符号 / 浮点 / 复数）。
- **`Packing`**：比特的几何切分（哪些比特属于哪个通道）。

写成公式：

\[ \text{DataType} = \text{DataKind} \times \text{Packing} \]

\[ \text{bitsPerPixel} = \sum_{i=0}^{3} b_i,\quad b_i = \text{bitsPerChannel}[i] \]

比如 `3U8`：DataKind = UNSIGNED，Packing = `X8_Y8_Z8`（3 通道各 8 位），bitsPerPixel = 24，一个元素 3 字节。又如 `F32`：DataKind = FLOAT，Packing = `X32`，1 通道 32 位浮点。

为什么要把"通道"编码进 DataType 而不是全部靠张量的维度表达？因为 GPU 内核对"内存中连续交错的 3 字节"与"三个独立平面"的处理路径完全不同——前者可以一个线程一次读 3 字节，后者天然匹配 `NCHW`。CV-CUDA 同时支持两种表达（4.3 节展开），这是它能同时吃下 OpenCV 风格（HWC 交错）与 PyTorch 风格（NCHW 平面）数据的根本原因。

#### 4.1.2 核心流程

一个 `DataType` 从构造到使用的流程：

1. 用户给 `DataKind` + `Packing`，或直接使用预定义常量（如 `TYPE_U8`）。
2. 构造时把两个枚举编码进一个 32 位位域 `NVCVDataType`（C ABI 的 POD 类型，跨编译器稳定）。
3. 查询时（`bitsPerChannel`、`numChannels`、`strideBytes`、`alignment`...）经 C API 函数取出对应字段，C++ 包装层用 `CheckThrow` 把 C 错误码翻译成 C++ 异常。
4. 关键推导公式（私有实现里）：
   - `strideBytes = (bitsPerPixel + 7) / 8`（比特数向上取整到字节）；
   - `alignment = GetAlignment(packing())`（对齐也由 packing 决定）；
   - `numChannels` 居然是委托给 `ImageFormat` 的单平面通道数算出来的——`DataType` 在实现上就是"只有一个平面的 ImageFormat 特例"。

#### 4.1.3 源码精读

**类定义与访问器**。[DataType.hpp:L52-L109](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/DataType.hpp#L52-L109) 声明了 `nvcv::DataType`，其中 [L97-L105](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/DataType.hpp#L97-L105) 列出全部查询接口：

```cpp
Packing                packing() const;
std::array<int32_t, 4> bitsPerChannel() const;
DataKind               dataKind() const;
int32_t                numChannels() const;
DataType               channelType(int32_t channel) const;
int32_t                strideBytes() const;
int32_t                bitsPerPixel() const;
int32_t                alignment() const;
```

这 8 个访问器就是 DataType 的全部"词汇表"。注意 `channelType(channel)`——可以取出"第 k 个通道单独是什么类型"，如 `3U8` 的每个通道都是 `U8`。

**两个组成轴**。`DataKind` 定义在 [DataLayout.hpp:L172-L179](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/DataLayout.hpp#L172-L179)（UNSPECIFIED/UNSIGNED/SIGNED/FLOAT/COMPLEX 五种）；`Packing` 是一个很长的枚举，见 [DataLayout.hpp:L42-L168](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/DataLayout.hpp#L42-L168)，几个代表值：

```cpp
X8           = NVCV_PACKING_X8,           // 1 通道 8 位
X8_Y8_Z8     = NVCV_PACKING_X8_Y8_Z8,     // 3 通道各 8 位（24 位）
X8_Y8_Z8_W8  = NVCV_PACKING_X8_Y8_Z8_W8,  // 4 通道各 8 位（32 位）
```

枚举里还有 `X5Y6Z5` 这类非均匀打包，以及 `b6X10`（16 位字中低位填充 6 位、通道占 10 位）这种带填充位的写法——`b` 前缀表示 padding。所以 packing 完整描述了"一个像素的位如何打包"（u2-l1 提过的 Packing 与 TensorLayout 是两套体系，此处即前者本体）。

**预定义常量**。[DataType.hpp:L122-L234](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/DataType.hpp#L122-L234) 把常用组合全部定义成了常量，命名规律是 **`TYPE_[通道数][种类][位宽]`**：

- [DataType.hpp:L123](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/DataType.hpp#L123)：`TYPE_U8` —— 1 通道无符号 8 位；
- [DataType.hpp:L127](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/DataType.hpp#L127)：`TYPE_3U8` —— 3 通道无符号 8 位（交错 RGB 的典型 dtype）；
- [DataType.hpp:L186](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/DataType.hpp#L186)：`TYPE_F32` —— 1 通道 32 位浮点。

通道数前缀取 1~4，种类取 U/S/F/C（复数），位宽取 8/16/32/64。理解了这个构词法，看到 `TYPE_4F32` 就知道是 4 通道 float32（齐次坐标、 RGBA 浮点都常用它）。

**构造与编码**。[DataType.hpp:L236-L245](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/DataType.hpp#L236-L245) 展示了两条构造路径：普通构造函数调用 C 函数 `nvcvMakeDataType`（运行期校验、可抛异常），`ConstCreate` 则用宏 `NVCV_MAKE_DATA_TYPE` 在编译期完成位域编码：

```cpp
inline DataType::DataType(DataKind dataKind, Packing packing)
{
    detail::CheckThrow(
        nvcvMakeDataType(&m_type, static_cast<NVCVDataKind>(dataKind), static_cast<NVCVPacking>(packing)));
}

constexpr DataType DataType::ConstCreate(DataKind dataKind, Packing packing)
{
    return DataType{NVCV_MAKE_DATA_TYPE(static_cast<NVCVDataKind>(dataKind), static_cast<NVCVPacking>(packing))};
}
```

这个"`constexpr` 内联版 + C 函数版"的双轨模式是 nvcv 全库的惯例（u6-l2 讲 C ABI 时会再遇到）。

**关键公式在私有实现里**。[src/nvcv/src/priv/DataType.cpp:L41-L44](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/priv/DataType.cpp#L41-L44) 给出字节数的真实算法：

```cpp
int DataType::strideBytes() const noexcept
{
    return (this->bpp() + 7) / 8;
}
```

即"比特数向上取整到整字节"。而 [priv/DataType.cpp:L37-L39](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/priv/DataType.cpp#L37-L39) 的 `numChannels` 与 [priv/DataType.cpp:L80-L83](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/priv/DataType.cpp#L80-L83) 的 `alignment` 则揭示了实现秘密：

```cpp
int DataType::numChannels() const noexcept
{
    return ImageFormat{m_type}.planeNumChannels(0);   // DataType = 单平面 ImageFormat
}
// ...
int DataType::alignment() const noexcept
{
    return GetAlignment(packing());
}
```

`DataType` 与 `ImageFormat` 底层共享同一套位域编码，前者只是后者"颜色语义全空、只有一个平面"的特例。理解这一点，4.2 节的 ImageFormat 就不再陌生。

#### 4.1.4 代码实践

**实践目标**：在 Python 里验证 `cvcuda.Type` 的位宽/通道语义，并用"掩码实验"直观感受 U8 与 F32 的取值范围差异。

**操作步骤**（需要装好 `cvcuda`、`cupy`、`numpy`，见 u1-l2）：

```python
# explore_dtype.py —— 示例代码
import numpy as np
import cupy as cp
import cvcuda

# 1) Type 与 numpy dtype 的等价性（绑定层设计保证，见 4.4）
print(cvcuda.Type.U8  == np.dtype(np.uint8))    # 预期 True
print(cvcuda.Type.F32 == np.dtype(np.float32))  # 预期 True
print(cvcuda.Type._3U8 == np.dtype("(3,)u1"))   # 3 通道类型，预期 True（待本地验证）

# 2) 位宽与字节数：掩码实验
#    U8 只有 8 位 -> 值域 [0,255]，超出部分按位丢弃（掩码 0xFF）
#    F32 有 32 位浮点 -> 宽值域，300.0 原样保留
src = cp.full((4, 4), 300.0, dtype=cp.float32)
t_f32 = cvcuda.as_tensor(src, layout="HW")        # F32 张量
u8    = src.astype(cp.uint8)                      # 300 & 0xFF = 44
t_u8  = cvcuda.as_tensor(u8, layout="HW")         # U8 张量

print("F32 存 300.0 ->", float(src[0, 0]))        # 预期 300.0
print("U8  存 300.0 ->", int(u8[0, 0]))           # 预期 44（= 300 - 256）
print("t_u8.dtype  =", t_u8.dtype)                # 预期 uint8
print("t_f32.dtype =", t_f32.dtype)               # 预期 float32
```

**需要观察的现象**：`300` 进入 U8 后变成 `44`——因为 `DataType(U8)` 的 packing 是 `X8`，只有 8 位，等价于对 0xFF 取掩码；而 F32 完整保留。

**预期结果**：两组输出印证位宽差异；dtype 打印结果说明张量把 `DataType` 以 numpy dtype 的面貌呈现给 Python（机制见 4.4）。输出数值部分**待本地验证**（本讲义编写环境无 GPU，无法实际运行）。

#### 4.1.5 小练习与答案

**练习 1**：不查代码，推出 `TYPE_4F32` 的通道数、每通道位数、bitsPerPixel、strideBytes、alignment。
**答案**：4 通道；每通道 32 位；bitsPerPixel = 4×32 = 128；strideBytes = (128+7)/8 = 16 字节；alignment 由 packing `X32_Y32_Z32_W32` 决定（`GetAlignment` 取其字节对齐，32 位通道 → 4 字节对齐）。公式依据见 [priv/DataType.cpp:L41-L44](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/priv/DataType.cpp#L41-L44)、[priv/DataType.cpp:L80-L83](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/priv/DataType.cpp#L80-L83) 与 [DataType.hpp:L284-L289](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/DataType.hpp#L284-L289)。（alignment 的精确值以本地运行 `GetAlignment(packing())` 为准，待本地验证。）

**练习 2**：为什么同一个"3 字节 RGB 像素"既能用 `Tensor((H,W,3), U8, "HWC")` 表达，也能用 `Tensor((H,W), 3U8, "HW"...)` 表达？两者内核读写方式有何不同？
**答案**：前者把通道作为张量最后一维，dtype 是标量 `U8`，元素 = 1 字节，内核按 `w*3+c` 寻址；后者把 3 通道打包进 dtype `3U8`，元素 = 3 字节，内核可以用 `uchar3` 一次读一个完整像素。官方测试里有第二种用法的实例：[tests/cvcuda/python/test_opremap.py:L84](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_opremap.py#L84-L89) 用 `cvcuda.Type._3U8` 配 shape `(13,21,1)`、layout `"HWC"`。

**练习 3**：`DataType` 会告诉你"这是红色通道"吗？
**答案**：不会。DataType 只有位级信息（DataKind×Packing），颜色语义在 `ImageFormat` 里。这正是下一模块的主题。

### 4.2 模块二：ImageFormat —— 给像素加上颜色语义与平面结构

#### 4.2.1 概念说明

`ImageFormat` 在 DataType 的"积木"（DataKind、Packing、Swizzle）之上，又叠加了**图像专属**的语义维度。看它的构造参数就一目了然（[ImageFormat.hpp:L76-L79](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/ImageFormat.hpp#L76-L79)）：

| 维度 | 含义 | 典型取值 |
|------|------|----------|
| `ColorModel` | 颜色模型（通道的逻辑身份） | RGB / YCbCr / RAW(Bayer) / XYZ |
| `ColorSpec` | 颜色规范（色彩空间+白点+传递函数+值域） | BT601 / BT709，FULL / LIMITED |
| `ChromaSubsampling` | 色度子采样 | 4:4:4 / 4:2:0 / 4:2:2 |
| `MemLayout` | 内存排布 | PITCH_LINEAR / BLOCK_LINEAR |
| `DataKind` | 比特解释（同 DataType） | UNSIGNED / FLOAT |
| `Swizzle` | 通道物理顺序 | S_XYZW（RGB）/ S_ZYXW（BGR） |
| `Packing0..3` | **每个平面各自的打包** | 最多 4 个平面 |
| `AlphaType` | alpha 是否预乘 | ASSOCIATED / UNASSOCIATED |

两个直观例子：

- **`Y8` vs `U8`**：两者内存完全一样（单平面、1 通道、8 位无符号、pitch-linear），但 `U8` 是"无语义的单通道"，`Y8` 是"BT601 有限值域的亮度（灰度）"——头文件注释明确写了 Y8 值域 16~235，低于 16 视为黑、高于 235 视为白；全值域版本叫 `Y8_ER`（0~255）。算子做色彩转换时必须知道这件事。
- **`RGB8` vs `BGR8`**：两者的 packing 完全相同（`X8_Y8_Z8`），区别只记录在 swizzle 上（`S_XYZ1` vs `S_ZYX1`）。这就是"通道顺序"如何被格式系统精确表达。

#### 4.2.2 核心流程

ImageFormat 的构造按"颜色模型"分四条路：

1. **YCbCr 路**：`ImageFormat(ColorSpec, ChromaSubsampling, ...)` → C 函数 `nvcvMakeYCbCrImageFormat`（NV12、Y8 走这条）。
2. **彩色路**：`ImageFormat(ColorModel, ColorSpec, ...)` → `nvcvMakeColorImageFormat`（RGB/BGR/HSV 走这条）。
3. **非彩色路**：`ImageFormat(MemLayout, DataKind, Swizzle, Packing...)` → `nvcvMakeNonColorImageFormat`（纯数据格式如 F32 走这条）。
4. **RAW 路**：`ImageFormat(RawPattern, ...)` → `nvcvMakeRawImageFormat`（Bayer 阵列走这条）。

另有两个静态工厂：`FromFourCC`（从 FourCC 码换算，视频生态常用）与 `FromPlanes`（由最多 4 个平面格式组合出多平面格式）。

查询方向有两套 API：

- **整图级**：`numChannels` / `bitsPerChannel` / `numPlanes` / `colorModel` / `swizzle` / `fourCC`...
- **平面级**：`planeDataType(i)` / `planeNumChannels(i)` / `planePixelStrideBytes(i)` / `planeBitsPerPixel(i)` / `planeSize(imgSize, i)`——多平面格式（NV12 的 Y 平面与 UV 平面分辨率不同！）必须逐平面查询。

格式间比较用 `HasSameDataLayout(a, b)`：判断两个格式的数据排布是否一致（颜色规范不同但排布相同时，拷贝类算子可以安全直传）。

#### 4.2.3 源码精读

**解剖第一现场：C 头文件的位域宏**。预定义格式的"配方"全写在 [src/nvcv/src/include/nvcv/ImageFormat.h](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/ImageFormat.h) 里，一行一个格式，参数顺序就是 4.2.1 的表格：

```c
// ImageFormat.h:L128 —— Y8：YCbCr 模型、无子采样、pitch-linear、无符号、单通道
#define NVCV_IMAGE_FORMAT_Y8 NVCV_DETAIL_MAKE_YCbCr_FMT1(BT601, NONE, PL, UNSIGNED, X000, ASSOCIATED, X8)

// ImageFormat.h:L173 —— NV12：420 子采样、双平面（X8 的 Y 平面 + X8_Y8 的 UV 平面）
#define NVCV_IMAGE_FORMAT_NV12 NVCV_DETAIL_MAKE_YCbCr_FMT2(BT601, 420, PL, UNSIGNED, XYZ0, ASSOCIATED, X8, X8_Y8)

// ImageFormat.h:L315-L321 —— RGB 家族：只有 swizzle 与 packing 在变
#define NVCV_IMAGE_FORMAT_RGB8  NVCV_DETAIL_MAKE_COLOR_FMT1(RGB, UNDEFINED, PL, UNSIGNED, XYZ1, ASSOCIATED, X8_Y8_Z8)
#define NVCV_IMAGE_FORMAT_BGR8  NVCV_DETAIL_MAKE_COLOR_FMT1(RGB, UNDEFINED, PL, UNSIGNED, ZYX1, ASSOCIATED, X8_Y8_Z8)
#define NVCV_IMAGE_FORMAT_RGBA8 NVCV_DETAIL_MAKE_COLOR_FMT1(RGB, UNDEFINED, PL, UNSIGNED, XYZW, ASSOCIATED, X8_Y8_Z8_W8)
```

引用：[ImageFormat.h:L128](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/ImageFormat.h#L128)、[ImageFormat.h:L173](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/ImageFormat.h#L173)、[ImageFormat.h:L315-L321](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/ImageFormat.h#L315-L321)。对着这三行，4.2.1 的表格就"活"了：RGB8 与 BGR8 的唯一差别是 `XYZ1` → `ZYX1`；NV12 比 RGB8 多了一个平面 packing（`X8_Y8`）。

**C++ 类与流式 API**。[ImageFormat.hpp:L51-L230](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/ImageFormat.hpp#L51-L230) 是类本体；[L187-L226](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/ImageFormat.hpp#L187-L226) 提供成对的 getter / setter（setter 返回新对象，风格近似不可变值类型）：

```cpp
ImageFormat dataKind(DataKind dataKind) const;   // 改 dataKind，返回新格式
DataKind    dataKind() const;                    // 查 dataKind
// memLayout / colorSpec / chromaSubsampling / rawPattern / alphaType 同理
```

平面级查询在 [L215-L226](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/ImageFormat.hpp#L215-L226) 声明、[L901-L939](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/ImageFormat.hpp#L901-L939) 实现。其中 [planeDataType（L901-L906）](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/ImageFormat.hpp#L901-L906) 把平面打包转回 `DataType`——**这就是 ImageFormat 与 DataType 的正式接口**：给内核写代码时，`fmt.planeDataType(0)` 直接得到该平面元素该用什么类型读：

```cpp
inline DataType ImageFormat::planeDataType(int32_t plane) const
{
    NVCVDataType out;
    detail::CheckThrow(nvcvImageFormatGetPlaneDataType(m_format, plane, &out));
    return static_cast<DataType>(out);
}
```

而 [planeRowAlignment（L929-L932）](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/ImageFormat.hpp#L929-L932) 直接委托给 DataType：`return planeDataType(plane).alignment();`，再次印证两层共享一套机制。[planeSize（L934-L939）](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/ImageFormat.hpp#L934-L939) 则按子采样规则缩放平面尺寸——NV12 的 UV 平面会得到 (w/2, h/2)。

**构造实现**。[ImageFormat.hpp:L598-L608](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/ImageFormat.hpp#L598-L608) 是 YCbCr 构造函数实现——把 C++ 枚举逐个 cast 后交给 C 函数 `nvcvMakeYCbCrImageFormat`，并用 `CheckThrow` 包装：

```cpp
inline ImageFormat::ImageFormat(ColorSpec colorSpec, ChromaSubsampling chromaSub, MemLayout memLayout,
                                DataKind dataKind, Swizzle swizzle, Packing packing0, /*...*/
{
    detail::CheckThrow(nvcvMakeYCbCrImageFormat(
        &m_format, static_cast<NVCVColorSpec>(colorSpec), static_cast<NVCVChromaSubsampling>(chromaSub),
        /* ... */));
}
```

**预定义格式清单（带官方注释）**。[ImageFormat.hpp:L249-L596](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/ImageFormat.hpp#L249-L596) 定义了全部 `FMT_*` 常量，注释里写了值域与平面结构，是最好的"格式速查表"。几个关键锚点：`FMT_U8`（[L252](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/ImageFormat.hpp#L252)）、`FMT_Y8`（[L311](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/ImageFormat.hpp#L308-L311)，注释写明 16~235 有限值域）、`FMT_Y8_ER`（[L318-L321](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/ImageFormat.hpp#L318-L321)，全值域 0~255）、`FMT_NV12`（[L348-L356](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/ImageFormat.hpp#L348-L356)，注释完整描述双平面结构）、`FMT_RGB8/FMT_BGR8/FMT_RGBA8/FMT_RGB8p`（[L497-L513](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/ImageFormat.hpp#L497-L513)）、`FMT_RGBf16/FMT_RGBAf16`（[L521-L531](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/ImageFormat.hpp#L521-L531)）。

> **命名规律**：`RGB8` = 模型 + 每通道位数；`RGBf32` = 模型 + f + 位数（浮点用小写 `f` 前缀，如 `RGBAf16`——不是 "RGBA16F"！）；后缀 `p` = planar（`RGB8p`）；后缀 `_ER` = extended range 全值域；后缀 `_BL` = block-linear。带数字开头的（如 `2S16`）在 Python 里加下划线前缀（见 4.4）。

#### 4.2.4 代码实践

**实践目标**：用 Python 检查若干格式的通道数、平面数，并核对 Y8/Y8_ER 的语义差异来源。

**操作步骤**：

```python
# explore_format.py —— 示例代码
import cvcuda

for f in [cvcuda.Format.RGB8, cvcuda.Format.RGBA8, cvcuda.Format.RGB8p,
          cvcuda.Format.RGBAf16, cvcuda.Format.Y8, cvcuda.Format.NV12]:
    print(f"{str(f):24s} channels={f.channels} planes={f.planes}")
```

**需要观察的现象**：`RGB8` 与 `BGR8` 的 channels 同为 3（packing 相同）；`RGB8` planes=1 而 `NV12` planes=2；`Y8` channels=1。

**预期结果**（以源码注释与 C 头定义为准）：

```
nvcv.Format.RGB8         channels=3 planes=1
nvcv.Format.RGBA8        channels=4 planes=1
nvcv.Format.RGB8p        channels=3 planes=1
nvcv.Format.RGBAf16      channels=4 planes=1
nvcv.Format.Y8           channels=1 planes=1
nvcv.Format.NV12         channels=3 planes=2
```

注意两点：① Python 端目前只暴露 `channels` 与 `planes` 两个属性（见 4.4 的 [ImageFormat.cpp:L96-L100](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/ImageFormat.cpp#L96-L100)），"每通道位数"需借助张量的 numpy dtype 间接获得（4.3 实践演示）；② `RGB8p` 的 planes 仍为 1（三个通道子平面合在一个 plane 定义里，Layout 维度区分平面性），具体数值**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`Format.Y8` 和 `Format.U8` 在内存层面有区别吗？在语义层面呢？举例说明什么算子必须区分二者。
**答案**：内存层面无区别——都是单平面、`X000` swizzle、`X8` packing、无符号 8 位（对照 [ImageFormat.h:L128](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/ImageFormat.h#L128) 与 `FMT_U8` 定义）；语义层面 Y8 声明了自己是 BT601 有限值域（16~235）的亮度（[ImageFormat.hpp:L308-L311](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/ImageFormat.hpp#L308-L311)）。色彩转换类算子（如 YUV→RGB 的 cvtcolor）必须按 BT601 公式与值域处理 Y8，对 U8 则只当原始字节。

**练习 2**：`NV12` 有几个平面？每个平面的 dtype 是什么？为什么它无法用单个 Tensor 表达？
**答案**：2 个平面（[ImageFormat.hpp:L348-L356](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/ImageFormat.hpp#L348-L356)）：平面 0 是 `X8`（Y，全分辨率），平面 1 是 `X8_Y8`（UV 交错，宽高各一半）。Tensor 要求统一的 stride 规则，无法表达"两个平面分辨率不同"；官方文档 [docs/sphinx/datatypes.rst:L63-L64](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/docs/sphinx/datatypes.rst#L60-L64) 明确说明了这一限制。

**练习 3**：写出一个格式，它与 `RGB8` 的 packing、DataKind、MemLayout 全部相同，但 swizzle 不同。
**答案**：`BGR8`。两者唯一差异是 swizzle `XYZ1` vs `ZYX1`（[ImageFormat.h:L315-L318](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/ImageFormat.h#L315-L318)）。

### 4.3 模块三：从 ImageFormat 到 Tensor —— 组合规则与布局推断

#### 4.3.1 概念说明

上一讲我们学过 `TensorLayout` 管的是"张量维度是什么"（N/C/H/W 标签）。本讲补上另一半：**给定一个 ImageFormat，张量的 dtype 与布局从哪来？** 规则可以一句话概括：

> **交错（interleaved）格式 → 通道维在最后（HWC/NHWC）；平面（planar，带 `p` 后缀）格式 → 通道维在最前（CHW/NCHW）。**

因为交错格式的内存里 RGB 紧挨着，最内层（变化最快）的维度自然是通道；平面格式里最外层把 R/G/B 三块分开，通道自然落到第一维。`dtype` 则取自 `fmt.planeDataType(0)`——RGB8 给 `uint8`，RGBf32 给 `float32`。

另外三条转换纪律（官方文档 [datatypes.rst:L216-L229](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/docs/sphinx/datatypes.rst#L214-L229)）：

1. Image → Tensor 要求：pitch-linear、无色度子采样（4:4:4）、各平面 dtype 与尺寸一致；
2. 多平面但平面尺寸一致的格式（如 planar RGBA）可以转 Tensor——用最外维 stride 编码平面间距；
3. NV12 这类平面尺寸不一致的格式不能转 Tensor。

#### 4.3.2 核心流程

Python 里按格式建张量的调用链：

```
cvcuda.Tensor(nimages=N, imgsize=(W,H), format=FMT)
    → Tensor::CreateForImageBatch (python/mod_cvcuda/nvcv/Tensor.cpp)
        → nvcv::Tensor::CalcRequirements(N, size, fmt, align)   # C++ 侧由格式推断 shape/dtype/layout
            → 交错: shape=(N,H,W,C), layout=NHWC, dtype=planeDataType(0)
            → 平面: shape=(N,C,H,W), layout=NCHW
    → CreateFromReqs → Cache 查复用 → 真正分配显存
```

反向（Tensor → Image）则必须显式给出格式：`cvcuda.as_image(tensor.cuda(), format=cvcuda.Format.RGB8)`——因为 Tensor 只带 dtype（"3 个无符号字节"），不带颜色语义（"这是 RGB"），语义要由用户补上。

#### 4.3.3 源码精读

**Python 入口**。[python/mod_cvcuda/nvcv/Tensor.cpp:L62-L69](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Tensor.cpp#L62-L69) 是"按格式建批张量"的实现，它把格式交给 C++ 的 `CalcRequirements`：

```cpp
std::shared_ptr<Tensor> Tensor::CreateForImageBatch(int numImages, const Size2D &size, nvcv::ImageFormat fmt,
                                                    int rowalign)
{
    nvcv::Tensor::Requirements reqs
        = nvcv::Tensor::CalcRequirements(numImages, nvcv::Size2D{std::get<0>(size), std::get<1>(size)}, fmt,
                                         rowalign == 0 ? nvcv::MemAlignment{} : nvcv::MemAlignment{}.rowAddr(rowalign));
    return CreateFromReqs(reqs);
}
```

该构造函数在 [Tensor.cpp:L527](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Tensor.cpp#L527) 注册为 `py::init`，参数名 `nimages/imgsize/format/rowalign`。注意 [L87-L94](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Tensor.cpp#L85-L103) 的 `CreateFromReqs` 会先查对象缓存（u4-l2 专题）。

**官方标准答案**。推断结果由测试钉死，[tests/cvcuda/python/test_tensor.py:L23-L57](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_tensor.py#L23-L57) 参数化了两条黄金断言：

```python
# RGBA8（交错）→ NHWC，通道维在最后，dtype=uint8
(5, (32, 16), cvcuda.Format.RGBA8, cvcuda.TensorLayout.NHWC, (5, 16, 32, 4), np.uint8),
# RGB8p（平面）→ NCHW，通道维在最前
(2, (38, 7),  cvcuda.Format.RGB8p,  cvcuda.TensorLayout.NCHW, (2, 3, 7, 38), np.uint8),
```

注意 `imgsize=(宽,高)=(32,16)` 而形状里是 `(N,H,W,C)=(5,16,32,4)`——尺寸参数按 (W,H) 给，张量按行列排，别搞混。

**NV12 转换失败的官方演示**。[samples/datatypes/conversions.py:L21-L37](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/datatypes/conversions.py#L21-L37) 演示了合法与非法转换：

```python
image = cvcuda.Image((640, 480), cvcuda.Format.RGB8)
tensor = cvcuda.as_tensor(image)                 # 合法：无子采样

nv12 = cvcuda.Image((1920, 1080), cvcuda.Format.NV12)
tensor_nv12 = cvcuda.as_tensor(nv12)             # 非法！
# 抛 RuntimeError，错误信息含 "sub-sampled"
```

**示例汇总**。[samples/datatypes/tensor.py:L31-L34](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/datatypes/tensor.py#L31-L34) 展示按格式建批张量（注释明确说"从格式推断 NHWC"）；同文件 [L26-L29](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/datatypes/tensor.py#L26-L29) 则是"shape+dtype+layout"三件套的显式路径。两条路径殊途同归——格式路径只是把 dtype/layout 的选择交给了库。

#### 4.3.4 代码实践

**实践目标**：亲手验证"格式 → shape/layout/dtype"的推断规则，并拿到"每通道位数"（Python 端 Format 未直接暴露，经张量 dtype 间接获得）。

**操作步骤**：

```python
# fmt_to_tensor.py —— 示例代码
import numpy as np
import cvcuda

fmts = [
    ("RGB8",    cvcuda.Format.RGB8),
    ("RGB8p",   cvcuda.Format.RGB8p),
    ("RGBA8",   cvcuda.Format.RGBA8),
    ("Y8",      cvcuda.Format.Y8),
    ("RGBf32",  cvcuda.Format.RGBf32),
    ("RGBAf16", cvcuda.Format.RGBAf16),
]
for name, f in fmts:
    t = cvcuda.Tensor(2, (64, 48), f)     # nimages=2, imgsize=(W,H)
    print(f"{name:8s} shape={t.shape} layout={t.layout} "
          f"dtype={t.dtype} bits/chan={t.dtype.itemsize * 8}")
```

**需要观察的现象**：交错格式 shape 末位是通道数、layout 为 NHWC；`RGB8p` 的 layout 变为 NCHW 且 shape=(2,3,48,64)；`RGBAf16` 的 dtype 是 float16（itemsize=2 → 每通道 16 位）；`Y8` 的 dtype 是 uint8 且通道维不出现（shape=(2,48,64)，预期为 NHW，**待本地验证**）。

**预期结果**（对照 [test_tensor.py:L23-L57](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_tensor.py#L23-L57) 的断言推演）：

| 格式 | shape | layout | dtype | 每通道位数 |
|------|-------|--------|-------|-----------|
| RGB8 | (2,48,64,3) | NHWC | uint8 | 8 |
| RGB8p | (2,3,48,64) | NCHW | uint8 | 8 |
| RGBA8 | (2,48,64,4) | NHWC | uint8 | 8 |
| Y8 | (2,48,64) | NHW（待本地验证） | uint8 | 8 |
| RGBf32 | (2,48,64,3) | NHWC | float32 | 32 |
| RGBAf16 | (2,48,64,4) | NHWC | float16 | 16 |

**运行环境说明**：以上需要 GPU 与安装好的 cvcuda 包；无 GPU 时该表标注"待本地验证"，可改为阅读 test_tensor.py 断言作源码阅读型验证。

#### 4.3.5 小练习与答案

**练习 1**：`cvcuda.Tensor(2, (38, 7), cvcuda.Format.RGB8p)` 得到的 shape 是 (2,3,7,38)，为什么 H=7、W=38 看起来"反了"？
**答案**：`imgsize` 参数按 (宽,高)=(38,7) 传入，而张量 shape 按数组惯例（行在前）排为 (N,C,H,W)=(2,3,7,38)。依据：[test_tensor.py:L34-L41](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_tensor.py#L34-L41)。

**练习 2**：为什么 `as_image` 必须显式传 `format=`，而 `as_tensor` 不用？
**答案**：Image 携带 ImageFormat（颜色语义），Tensor 只携带 DataType（位级信息）。Tensor→Image 是"补语义"，必须由用户提供；Image→Tensor 是"丢语义"（保留位级信息），库自己就能完成（且仅当无子采样等条件满足，见 [conversions.py:L21-L37](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/datatypes/conversions.py#L21-L37)）。

**练习 3**：一个 `Tensor((480,640,3), cvcuda.Type.U8, layout="HWC")` 和 `Tensor(1, (640,480), cvcuda.Format.BGR8)` 在内存布局上等价吗？在语义上呢？
**答案**：内存层面二者都是 480 行×640 列×3 字节交错（BGR8 packing 为 X8_Y8_Z8、pitch-linear），as_tensor/as_image 可互转（conversions.py 正是这样做的）；语义层面后者声明了通道顺序是 B-G-R 与颜色模型 RGB，前者只是"3 个无符号字节"。做 cvtcolor 时这个差别是决定性的。

### 4.4 模块四：Python 绑定 —— cvcuda.Type 与 cvcuda.Format 是怎么暴露出来的

#### 4.4.1 概念说明

Python 用户见到的是 `cvcuda.Type.U8`、`cvcuda.Format.RGB8`。它们背后有两套不同的 pybind11 机制：

- **`cvcuda.Format` 是枚举（`py::enum_`）**：格式是有限闭集，枚举 + `export_values()` 把所有值导出到模块命名空间；额外挂了 `channels`/`planes` 两个只读属性。枚举成员清单**不是手写的**，而是构建时用 sed 脚本从 C 头文件自动生成，保证三个层（C 宏/C++ 常量/Python 枚举）永不脱节。
- **`cvcuda.Type` 是类（`py::class_`）**：因为 DataType 需要构造、比较、与 numpy 互转。最有意思的一点：**Type 的值到了 Python 侧会以 numpy dtype 的面貌出现**——`cvcuda.Type.U8` 和 `np.dtype('uint8')` 判等为 True、哈希一致，可以直接当字典键用。

#### 4.4.2 核心流程

Format 枚举的生成链（三方同步机制）：

```
C 头 ImageFormat.h 的 #define NVCV_IMAGE_FORMAT_XXX
    → gen_imgformat_list.sh 用 sed 提取 → NVCVPythonImageFormatDefs.inc（构建期生成）
    → ImageFormat.cpp 的 #include 展开 DEF(F)/DEF_NUM(F) 宏
    → py::enum_ 逐个 fmt.value("XXX", FMT_XXX)
```

DataType 的转换链（双向）：

```
numpy dtype → DataType：ToNVCVDataType → FindDataType<T> 逐类型试探
    （subdtype 的形状给出通道数 → 选 S_X000/S_XY00/S_XYZ0/S_XYZW → MakePacking）
DataType → numpy dtype：ToDType → FindDType 反查 SupportedBaseTypes 表
```

#### 4.4.3 源码精读

**Format 枚举导出**。[python/mod_cvcuda/nvcv/ImageFormat.cpp:L76-L105](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/ImageFormat.cpp#L76-L105)：

```cpp
void ExportImageFormat(py::module &m)
{
    py::enum_<nvcv::ImageFormat> fmt(m, "Format");
#define DEF(F)     fmt.value(#F, nvcv::FMT_##F);
#define DEF_NUM(F) fmt.value("_" #F, nvcv::FMT_##F);   // 数字开头 → 加下划线前缀
#include "NVCVPythonImageFormatDefs.inc"
    fmt.value("NONE", nvcv::FMT_NONE);                 // 哨兵值单独导出
    fmt.export_values()
        .def_property_readonly("planes", &nvcv::ImageFormat::numPlanes, ...)
        .def_property_readonly("channels", &nvcv::ImageFormat::numChannels, ...);
}
```

要点：① Python 端只有 `planes`/`channels` 两个属性（[L96-L100](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/ImageFormat.cpp#L96-L100)），bitsPerChannel 等要在 C++ 里用；② `DEF_NUM` 宏解决 `2S16` 这种以数字开头、不是合法 Python 标识符的名字（所以是 `cvcuda.Format._2S16`）；③ [L44-L74](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/ImageFormat.cpp#L44-L74) 的 `ImageFormatToString` 把 C 名字 `NVCV_IMAGE_FORMAT_RGB8` 改写成 `nvcv.Format.RGB8`，这就是 `repr()` 的来源；④ [L26-L32](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/ImageFormat.cpp#L26-L32) 特化 `underlying_type` 为 `uint64_t`，让 pybind11 把它当枚举导出。

**自动生成清单的脚本**。[gen_imgformat_list.sh:L27-L28](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/gen_imgformat_list.sh#L27-L28) 全部逻辑就两行 sed：

```bash
sed -n 's@^#define NVCV_IMAGE_FORMAT_\([0-9][^ ]\+\) NVCV_DETAIL.*@DEF_NUM(\1)@gp' $imgfmt_header
sed -n 's@^#define NVCV_IMAGE_FORMAT_\([^0-9][^ ]\+\) NVCV_DETAIL.*@DEF(\1)@gp' $imgfmt_header
```

构建系统在 [python/mod_cvcuda/CMakeLists.txt:L32-L38](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/CMakeLists.txt#L32-L38) 把它注册为 `add_custom_command`，以 C 头为依赖、生成 `NVCVPythonImageFormatDefs.inc`。这是 u1-l4 讲过的"生成式 requirements"哲学的又一实例：**单一事实来源，其余全部生成**。

**Type 类导出与 numpy 互通**。[python/mod_cvcuda/nvcv/DataType.cpp:L321-L364](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/DataType.cpp#L321-L364) 导出 `Type` 类：静态成员表同样由生成的 inc 提供（`DEF`/`DEF_NUM`，故 `cvcuda.Type._3U8`），[L335](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/DataType.cpp#L335) 挂了 `components` 属性（映射 `numChannels`），[L363](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/DataType.cpp#L363) 注册了 `numpy dtype → DataType` 的隐式转换。最关键的注释在 [L346-L348](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/DataType.cpp#L346-L348)：

```cpp
// Type values surface to Python as numpy.dtype (see the DataType type_caster
// below), so a Type constructed via Type(...) must hash identically to the
// equivalent numpy.dtype to stay consistent with __eq__ across both forms.
```

配套的 type_caster 双向实现在 [L372-L422](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/DataType.cpp#L372-L422)：`load` 把 Python 对象（原生 Type 或 numpy dtype）转成 C++ `DataType`，`cast` 把 C++ 返回值转回 numpy dtype。这就是 `tensor.dtype` 打印出来是 numpy dtype 的原因。

**numpy dtype → DataType 的推断算法**。[DataType.cpp:L132-L204](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/DataType.cpp#L132-L204) 的 `FindDataType` 是核心：先看 numpy 的 subdtype 形状确定通道数（[L137-L159](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/DataType.cpp#L137-L159)），再按通道数选 swizzle（[L174-L190](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/DataType.cpp#L174-L190)）：

```cpp
switch (nchannels)
{
case 1: pp.swizzle = nvcv::Swizzle::S_X000; break;
case 2: pp.swizzle = nvcv::Swizzle::S_XY00; break;
case 3: pp.swizzle = nvcv::Swizzle::S_XYZ0; break;
case 4: pp.swizzle = nvcv::Swizzle::S_XYZW; break;
}
```

最后逐通道填位数并 `MakePacking`（[L193-L202](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/DataType.cpp#L193-L202)）。支持的基类型白名单在 [L206-L216](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/DataType.cpp#L206-L216)：float16（用专门的 `Float16Tag` 标记，[L56-L59](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/DataType.cpp#L56-L59) 注释说明 C++ 没有原生 16 位浮点）、复数、float/double、各宽度整型——Python 端能用哪些 dtype 造张量，由这张表决定。

#### 4.4.4 代码实践

**实践目标**：验证 Type↔numpy 的等价性与哈希一致性；观察不支持的 dtype 会发生什么。

**操作步骤**：

```python
# type_numpy_bridge.py —— 示例代码
import numpy as np
import cvcuda

# 1) 等价性：Type 值以 numpy dtype 面貌出现
print(cvcuda.Type.U8 == np.dtype(np.uint8))      # 预期 True
d = {cvcuda.Type.U8: "unsigned 8-bit", np.dtype(np.uint8): "??"}
print(len(d), d[np.dtype(np.uint8)])             # 哈希一致 → 预期 1, "unsigned 8-bit"

# 2) 隐式转换：numpy dtype 直接当 Type 用
t = cvcuda.Tensor((4, 4), np.float32, layout="HW")
print(t.dtype)                                   # 预期 float32

# 3) 白名单之外的 dtype（预期失败）
try:
    cvcuda.Tensor((4, 4), np.dtype("S10"))       # 字节串，不在 SupportedBaseTypes
except TypeError as e:
    print("rejected:", e)
```

**需要观察的现象**：第 1 步两个"看起来不同"的键在字典里合并成一个——说明 `__eq__` 与 `__hash__` 都按 numpy dtype 对齐（正是 [DataType.cpp:L346-L361](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/DataType.cpp#L346-L361) 注释要求的行为）；第 3 步被拒绝，因为字节串不在 [L206-L216](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/DataType.cpp#L206-L216) 的白名单里。

**预期结果**：`True` / `1 unsigned 8-bit` / `float32` / 拒绝信息。具体异常类型与文案**待本地验证**（可能是 TypeError，由 type_caster::load 返回 false 触发）。

#### 4.4.5 小练习与答案

**练习 1**：`cvcuda.Format._2S16` 这个下划线前缀是哪来的？
**答案**：C 宏 `NVCV_IMAGE_FORMAT_2S16` 以数字开头，不是合法 Python 标识符；生成脚本 [gen_imgformat_list.sh:L27](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/gen_imgformat_list.sh#L27-L28) 用正则把这类名字归到 `DEF_NUM`，导出宏给它加 `_` 前缀（[ImageFormat.cpp:L83](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/ImageFormat.cpp#L80-L83)）。`cvcuda.Type._3U8` 同理。

**练习 2**：为什么 `cvcuda.Format` 用枚举而 `cvcuda.Type` 用普通类？
**答案**：ImageFormat 是编译期确定的有限集合（一个 uint64 位域值），枚举 + export_values 即可完整表达；DataType 需要运行期从 DataKind+Packing 构造、需要与 numpy dtype 双向隐式转换（type_caster）、需要 `components` 这类动态查询，普通 py::class_ 才装得下这些行为（对照 [ImageFormat.cpp:L78](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/ImageFormat.cpp#L78) 与 [DataType.cpp:L323](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/DataType.cpp#L323)）。

**练习 3**：如果你新增一个 C 头里的 `#define NVCV_IMAGE_FORMAT_XXX`，Python 侧要改几个文件才能导出它？
**答案**：一个都不用改。CMake 的 custom command（[CMakeLists.txt:L32-L38](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/CMakeLists.txt#L32-L38)）以 C 头为依赖重新生成 inc，重新构建后 `cvcuda.Format.XXX` 自动出现——前提是同时补上 C++ 侧的 `FMT_XXX` 常量（ImageFormat.hpp）供 `DEF` 宏引用。

## 5. 综合实践

**任务：制作一份「格式-类型速查表生成器」，并用掩码实验固化 U8/F32 的认识。**

把你在这讲学到的四件事串起来：Format 的通道/平面查询、格式→张量推断、dtype 的位宽语义、Type↔numpy 等价性。

```python
# format_cheatsheet.py —— 示例代码（需 GPU 环境，无 GPU 时对照测试断言填写）
import numpy as np
import cupy as cp
import cvcuda

def probe(name, fmt):
    t = cvcuda.Tensor(2, (64, 48), fmt)          # 4.3：格式→张量推断
    return {
        "name":    name,
        "channels": fmt.channels,                 # 4.2：格式自带通道数
        "planes":  fmt.planes,
        "layout":  str(t.layout),                 # 交错→NHWC，平面→NCHW
        "dtype":   str(t.dtype),                  # 4.4：以 numpy dtype 面貌出现
        "bits/ch": t.dtype.itemsize * 8,          # 每通道位数（经 dtype 间接获得）
    }

rows = [probe(n, f) for n, f in [
    ("RGB8",    cvcuda.Format.RGB8),
    ("BGR8",    cvcuda.Format.BGR8),
    ("RGB8p",   cvcuda.Format.RGB8p),
    ("RGBA8",   cvcuda.Format.RGBA8),
    ("Y8",      cvcuda.Format.Y8),
    ("RGBAf16", cvcuda.Format.RGBAf16),
    ("F32",     cvcuda.Format.F32),
]]
for r in rows:
    print("{name:8s} channels={channels} planes={planes} "
          "layout={layout:6s} dtype={dtype:8s} bits/ch={bits}".format(**r))

# 掩码实验：同一数值 300.0 在 U8 / F32 下的命运
src = cp.full((4, 4), 300.0, dtype=cp.float32)
print("F32:", float(src[0, 0]), " U8(300 & 0xFF):", int(src.astype(cp.uint8)[0, 0]))
print("Type.U8  == np.dtype(uint8):", cvcuda.Type.U8 == np.dtype(np.uint8))
```

**验收标准**：

1. 表格中 `RGB8` 与 `BGR8` 两行除了名字全部相同（印证"仅 swizzle 不同"，对照 [ImageFormat.h:L315-L318](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/ImageFormat.h#L315-L318)）；
2. `RGB8p` 的 layout 是 NCHW 而其余 RGB 格式是 NHWC（对照 [test_tensor.py:L23-L41](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_tensor.py#L23-L41)）；
3. `RGBAf16` 的 bits/ch 是 16（注意它叫 `RGBAf16` 不叫 "RGBA16F"）；
4. 掩码实验输出 `F32: 300.0` 与 `U8: 44`。

若本机没有 GPU：把第 1、2、4 条改为"源码阅读型验证"——分别读 ImageFormat.h 的宏定义、test_tensor.py 的黄金断言、priv/DataType.cpp 的 `(bpp+7)/8` 公式，把表格手工填出来，效果等同。

## 6. 本讲小结

- **DataType = DataKind × Packing**：只管位级问题（多少位、几通道、怎么切、按什么数解释），核心公式 `bitsPerPixel = Σ bitsPerChannel`、`strideBytes = (bpp+7)/8`（[priv/DataType.cpp:L41-L44](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/priv/DataType.cpp#L41-L44)），内部实现竟是"单平面 ImageFormat 特例"。
- **ImageFormat = DataType + 颜色语义 + 平面结构**：ColorModel/ColorSpec/ChromaSubsampling/MemLayout/Swizzle/多 Packing/AlphaType 七个维度（[ImageFormat.hpp:L76-L79](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/ImageFormat.hpp#L76-L79)）；每个预定义格式的"配方"可逐字段读自 [ImageFormat.h](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/ImageFormat.h#L315-L321) 的宏。
- **组合规则**：交错格式 → NHWC（通道维最后），平面格式（`p` 后缀）→ NCHW（通道维最前），dtype 取 `planeDataType(0)`；NV12 因色度子采样无法转 Tensor（[conversions.py:L31-L37](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/datatypes/conversions.py#L31-L37)）。
- **语义 vs 位级是设计主线**：`Y8` 与 `U8` 内存相同语义不同、`RGB8` 与 `BGR8` 位相同 swizzle 不同、`as_image` 必须补格式而 `as_tensor` 不用——都是"数据与语义分离"这一架构决策的体现。
- **Python 端**：`cvcuda.Format` 是自动生成的枚举（sed 脚本保证 C/C++/Python 三方同步），`cvcuda.Type` 的值以 numpy dtype 面貌出现且哈希一致；数字开头的名字加 `_` 前缀（`_2S16`、`_3U8`）。

## 7. 下一步学习建议

- **下一讲（u2-l3）**：变长批处理 `ImageBatchVarShape` 与 `TensorBatch`——你将看到一批不同格式、不同尺寸的 Image 如何进同一个容器，那是对本讲格式系统的第一次"压力测试"。
- **再往后（u2-l4）**：DLPack 零拷贝互操作，会用到本讲的"DataType↔numpy dtype"转换链。
- **源码阅读建议**：通读 [ImageFormat.hpp:L249-L596](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/ImageFormat.hpp#L249-L596) 的格式注释（尤其是 NV12 与 Y8 系列），这是全仓库文档密度最高的代码段之一；有余力再读 [src/nvcv/src/priv/DataLayout.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/priv/DataLayout.cpp)，看 `MakePacking` 如何把"位数数组+swizzle"编码成唯一的 Packing 枚举值。
- **查漏补缺**：如果对"为什么 GPU 内核在乎交错/平面"，建议预习 u5 单元的算子四层结构（u5-l1），带着这个问题去看 kernel 代码。
