# 变长批处理：ImageBatchVarShape 与 TensorBatch

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚「固定 shape 的 Tensor 批」与「变长批容器」的本质区别，以及为什么视频/训练数据预管线必须引入后者。
2. 用 Python 构建 `cvcuda.ImageBatchVarShape`（每张图尺寸、甚至格式都不同）与 `cvcuda.TensorBatch`（尺寸可变但 rank/dtype/layout 必须一致），并逐个元素读出属性。
3. 描述算子内核在 GPU 端是如何「按张」找到变长批里每张图的数据的（`exportData` 与 `NVCVImageBatchVarShapeBufferStrided`）。
4. 用 `cvcuda.padandstack` / `cvcuda.stack` 把变长批「合流」成规则的 NHWC Tensor，并定量比较两种表示的显存占用。

## 2. 前置知识

本讲建立在 u2-l1（Tensor、stride、布局）与 u2-l2（DataType、ImageFormat）之上。开始前请确认理解以下已建立的概念：

- **Tensor 是规则的长方体**：一个 NHWC Tensor 的四维长度对批内所有样本唯一，stride 以字节为单位，行距通常对齐到设备纹理对齐属性。
- **ImageFormat 描述一张图**：颜色模型 + 通道数 + swizzle + 平面结构，交错格式（如 RGB8）对应 HWC 语义。

在此之上，本讲需要两个新直觉：

1. **批（batch）的两种形态**。
   - 形态 A：把批塞进一个规则 Tensor 的第 0 维（N）。它要求批内所有样本同宽同高同通道——这就是 u1-l2 中 hello_world 用 `stack` 得到的形态，也是深度学习推理引擎的输入要求。
   - 形态 B：批是一个**容器对象**，里面逐个存放 `Image` 或 `Tensor`，每个元素自带自己的形状（甚至格式）元数据。CV-CUDA 为此提供了 `ImageBatchVarShape` 与 `TensorBatch`。
2. **「木桶效应」（padding 浪费）**。把尺寸不一的图硬塞进同一个 N×H×W 画布，每张小图都要按批内最大宽高占位，空余部分填 padding 值。浪费率随尺寸离散度急剧上升，本讲会给出公式并实测。

另外回顾 u1-l4 提过的一点：Python 侧的容器对象（非包装）会进入**对象缓存**复用，其缓存键就是 capacity——本讲会在绑定源码里指出这一行，完整机制留到 u4-l2 展开。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/nvcv/src/include/nvcv/ImageBatch.hpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/ImageBatch.hpp) | `ImageBatch` / `ImageBatchVarShape` C++ 类定义：容量、pushBack、maxSize、uniqueFormat、迭代器、exportData |
| [src/nvcv/src/include/nvcv/ImageBatchData.h](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/ImageBatchData.h) | C ABI 数据结构 `NVCVImageBatchVarShapeBufferStrided`：GPU 端按张访问变长批的元数据布局 |
| [src/nvcv/src/include/nvcv/TensorBatch.hpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/TensorBatch.hpp) | `TensorBatch` C++ 类定义：rank/dtype/layout 一致性约束、pushBack、exportData、setTensor |
| [python/mod_cvcuda/nvcv/ImageBatch.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/ImageBatch.cpp) | `cvcuda.ImageBatchVarShape` 的 pybind11 绑定与缓存创建、`cvcuda.as_images` |
| [python/mod_cvcuda/nvcv/TensorBatch.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/TensorBatch.cpp) | `cvcuda.TensorBatch` 的 pybind11 绑定与缓存创建、`cvcuda.as_tensors` |
| [samples/datatypes/imagebatchvarshape.py](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/datatypes/imagebatchvarshape.py) | 官方示例：混尺寸、混格式的变长图像批 |
| [samples/datatypes/tensorbatch.py](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/datatypes/tensorbatch.py) | 官方示例：同 rank/dtype/layout、不同 shape 的张量批 |
| [src/cvcuda/include/cvcuda/OpStack.h](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/OpStack.h) | `stack` 算子契约：TensorBatch→Tensor 与 变长批→Tensor（要求同尺寸同格式） |
| [src/cvcuda/include/cvcuda/OpPadAndStack.h](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/OpPadAndStack.h) | `padandstack` 算子契约：变长批→Tensor，逐图 top/left 填充 |
| [tests/cvcuda/python/test_oppadandstack.py](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_oppadandstack.py) | 官方测试：top/left 张量构造方式与输出 shape 断言 |

## 4. 核心概念与源码讲解

### 4.1 为什么需要变长批：规则 Tensor 的「木桶效应」

#### 4.1.1 概念说明

真实世界的图像流水线里，输入几乎从来不是整齐划一的：

- 视频转码：每路视频分辨率不同（1080p 混 720p 混 480p）。
- 训练数据预处理：数据集里的图片天然尺寸不一。
- 目标检测后处理：裁剪出的 ROI 框大小各异。

如果只有「规则 Tensor」这一种批形态，就只有两条路：

1. **先 pad 再批处理**：全部填到最大宽高。显存被 padding 大量浪费，且 padding 值可能污染后续统计类算子（如 normalize 的均值计算）。
2. **逐张处理**：批维度消失，GPU 利用率骤降，与 CV-CUDA 的批处理设计初衷相悖。

CV-CUDA 的答案是引入**变长批容器**：容器只保存「元素的句柄列表 + 每元素的元数据」，像素仍然各归各的 `Image`/`Tensor` 缓冲，需要规则批的场合（推理前一刻）再用 `padandstack` 这类算子一次性合流。这样「不整齐的阶段保持不整齐、需要整齐的时刻才付 padding 的代价」。

#### 4.1.2 核心流程

设批内有 \(N\) 张图，第 \(i\) 张宽高为 \(w_i \times h_i\)，每像素 \(c\) 通道、每通道 \(b\) 字节。两种表示的像素内存（忽略行距对齐与容器元数据开销）：

\[ M_{\text{varshape}} \approx \sum_{i=1}^{N} w_i h_i \, c \, b \]

\[ M_{\text{tensor}} = N \cdot W_{\max} H_{\max} \, c \, b, \quad W_{\max}=\max_i w_i,\ H_{\max}=\max_i h_i \]

padding 浪费率为：

\[ \rho = 1 - \frac{\sum_{i=1}^{N} w_i h_i}{N \cdot W_{\max} H_{\max}} \]

决策伪代码：

```
if 批内尺寸全部相同:
    直接用规则 Tensor（或 stack），零浪费
elif 下游算子/推理引擎只吃规则 Tensor:
    变长批流转到最后一步，再 padandstack（浪费只发生一次）
else:
    全程 ImageBatchVarShape（多数 CV-CUDA 算子原生支持变长批入口）
```

注意两点修正：精确字节数要按 u2-l1 讲过的 stride 对齐规则计算（行距对齐到设备纹理对齐属性，通常 32 字节）；变长批容器自身还有一小块**元数据**显存（见 4.2.3 中 `CalcRequirements` 的用途），量级远小于像素。

#### 4.1.3 源码精读

「规则批要求所有图同尺寸同格式」不是文档口头约定，而是写在 `stack` 算子的 C 头契约里：

- [src/cvcuda/include/cvcuda/OpStack.h:L122-L140](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/OpStack.h#L122-L140) — `cvcudaStackVarShapeSubmit` 的注释明确写道：输出是输入图像的拼接（concatenation），**所有源图像必须有相同 format（数据类型与通道数）和相同尺寸（宽高）**，输出为 NHWC/NCHW Tensor，N 等于图像数。
- [src/cvcuda/include/cvcuda/OpStack.h:L118-L119](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/OpStack.h#L118-L119) — TensorBatch 版本的 `cvcudaStackSubmit`：N 等于所有输入张量中样本总数。

而允许尺寸不齐的那个入口在 `padandstack`：

- [src/cvcuda/include/cvcuda/OpPadAndStack.h:L91-L100](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/OpPadAndStack.h#L91-L100) — 输入/输出依赖表中 `Width: No`、`Height: No`：输出宽高允许与输入不同（即输出画布是放大后的统一尺寸）。
- [src/cvcuda/include/cvcuda/OpPadAndStack.h:L102-L105](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/OpPadAndStack.h#L102-L105) — top/left 是两个 int32「向量张量」：必须是 NHWC 且 N=H=C=1、W 等于批内图像数，即第 i 个元素给出第 i 张图的上方/左侧填充量。

对比两份契约就能看出设计意图：**规则是默认，变长是显式选择，且变长→规则的转换点由用户掌控**。

#### 4.1.4 代码实践

**实践目标**：不写一行 GPU 代码，先用数学与文档验证「木桶效应」值得规避。

**操作步骤**：

1. 打开 `samples/datatypes/imagebatchvarshape.py`，注意官方示例刻意选了三个尺寸（640×480、1280×720、800×600）。
2. 手算（或用 Python 计算器）三张 RGB8 图的两种内存：
   - 变长批：\(640{\cdot}480{\cdot}3 + 1280{\cdot}720{\cdot}3 + 800{\cdot}600{\cdot}3 = 5{,}126{,}400\) 字节 ≈ 4.89 MB
   - pad 到 1280×720 的 NHWC Tensor：\(3 \cdot 720 \cdot 1280 \cdot 3 = 8{,}294{,}400\) 字节 = 7.91 MB
   - 浪费率 \(\rho \approx 38.2\%\)
3. 再算一个极端例子：1000 张 64×64 缩略图混入 1 张 1920×1080 图，\(\rho\) 接近 99.9%。

**需要观察的现象**：浪费率只取决于尺寸分布的离散度，与图片内容无关；批越大、尺寸越参差，pad 策略越不可接受。

**预期结果**：得出结论「尺寸离散的批应在变长容器中流转，仅在必要边界 pad」。（纯计算，无需 GPU，可直接验证。）

#### 4.1.5 小练习与答案

**练习 1**：4 张单通道 U8 图，(w,h) 分别为 (100,100)、(200,100)、(100,200)、(200,200)。全部 pad 成 maxsize 规则批的浪费率是多少？

**答案**：\(\sum w_i h_i = 10000+20000+20000+40000 = 90000\)；规则批 \(= 4 \times 200 \times 200 = 160000\)；\(\rho = 1 - 90000/160000 = 43.75\%\)。

**练习 2**：为什么「先各自 resize 到统一尺寸再组批」与「先组变长批再统一 pad」可能都不理想？各自的代价是什么？

**答案**：前者在组批前就永久改变了长宽比或分辨率（resize 有插值损失，且不可逆）；后者保留原始像素但引入 padding 值，若后续算子（如 normalize、直方图统计）不区分 padding 区，统计量会被污染。工程上常按下游需求取舍：几何敏感的用 letterbox（等比 pad），统计敏感的需确认算子是否感知有效区域。

**练习 3**：`OpStack.h` 中 TensorBatch 版与 VarShape 版 `stack` 对输入的要求差在哪一句？

**答案**：VarShape 版（L124-L125）要求所有源图像 format 与宽高都相同；TensorBatch 版没有逐张尺寸检查这一句，因为它假定成员张量已满足规则批语义（其元素本身 rank/dtype/layout 一致，见 4.4）。

### 4.2 ImageBatchVarShape：C++ 类与 Python API 双视角

#### 4.2.1 概念说明

`ImageBatchVarShape` 是「一批尺寸可变的图像」。要点：

- 它是**句柄容器**，不拥有像素：每张 `Image` 自己管理显存，批只持有它们的引用（这解释了为什么组批是廉价操作）。
- **每张图自带格式元数据**，因此甚至允许批内混用 RGB8、RGBA8、U8（官方示例就是这么演示的）。但注意：多数算子要求批内格式统一，混格式批通常只在「收集/传递」阶段有意义。
- 容量（capacity）在构造时一次声明，之后只能 `pushback`/`popback`/`clear`，不能动态扩容。
- `maxSize()` 给出批内最大宽高（pad 画布尺寸），`uniqueFormat()` 在格式一致时给出该格式、不一致时返回无效格式。

#### 4.2.2 核心流程

C++ 侧的生命周期：

```
声明容量 capacity ──► pushBack(img)×N ──► [算子提交: exportData(stream) ──► kernel] 
                                    └─► popback / clear 复用容器
```

Python 侧（绑定层多做两件事）：

```
cvcuda.ImageBatchVarShape(capacity)
   └─ 先查对象缓存（键=capacity）──命中──► clear() 后复用
                              └─未命中──► 新建并登记缓存
batch.pushback(img_or_list) / batch.popback() / batch.clear()
len(batch) / for img in batch / batch[i]（经迭代器与 __len__ 暴露）
```

#### 4.2.3 源码精读

C++ 类（先看骨架再对照绑定）：

- [src/nvcv/src/include/nvcv/ImageBatch.hpp:L164-L196](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/ImageBatch.hpp#L164-L196) — `class ImageBatchVarShape : public ImageBatch`：继承通用批接口，额外提供 `CalcRequirements(capacity)` 静态方法与「按容量构造」的构造函数。`Requirements` 描述创建该容量批所需的内存，供分配器协商（u6-l3 展开）。
- [src/nvcv/src/include/nvcv/ImageBatch.hpp:L201-L204](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcv/src/include/nvcv/ImageBatch.hpp#L201-L204) — `pushBack` 的三个重载：迭代器范围、单张 `Image`、以及回调式（回调持续产图直到返回空句柄，适合解码器边解边灌批的场景）。
- [src/nvcv/src/include/nvcv/ImageBatch.hpp:L224-L246](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcv/src/include/nvcv/ImageBatch.hpp#L224-L246) — `clear()`、`maxSize()`、`uniqueFormat()`、`operator[]` 与迭代器接口：这是一套完整的随机访问容器外观。
- [src/nvcv/src/include/nvcv/ImageBatch.hpp:L283-L286](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/ImageBatch.hpp#L283-L286) — `IsCompatibleKind`：仅当底层 C 句柄类型是 `NVCV_TYPE_IMAGEBATCH_VARSHAPE` 才兼容，这是 C++ 类型系统对 C ABI 句柄的守门检查。
- [src/nvcv/src/include/nvcv/ImageBatch.hpp:L290-L291](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/ImageBatch.hpp#L290-L291) — `ImageBatchVarShapeWrapHandle = NonOwningResource<...>`：非拥有的「包装」形态，与拥有型资源区分（与 u4-l2 要讲的包装对象缓存行为直接相关）。

Python 绑定层（注意属性命名）：

- [python/mod_cvcuda/nvcv/ImageBatch.cpp:L54-L73](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/ImageBatch.cpp#L54-L73) — `ImageBatchVarShape::Create(capacity)`：先 `Cache::Instance().fetch(Key{capacity})`，命中则 `clear()` 后复用，未命中才新建并 `add` 进缓存。**capacity 就是缓存键**——同容量的批在 Python 循环管线里会被反复复用（机制详解见 u4-l2）。
- [python/mod_cvcuda/nvcv/ImageBatch.cpp:L97-L110](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/ImageBatch.cpp#L97-L110) — 构造函数体：`m_impl(capacity)` 之外，还用 `CalcRequirements` + `nvcvMemRequirementsCalcTotalSizeBytes` 预先算出**容器自身**的显存字节数（`m_size_inbytes`）。这块是每图元数据数组的开销，与像素内存无关，量级很小。
- [python/mod_cvcuda/nvcv/ImageBatch.cpp:L173-L177](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/ImageBatch.cpp#L173-L177) — `pushBack`：一行推进 C++ 批（`m_impl.pushBack`），一行把 `Image` 的 shared_ptr 存进 `m_list`——后者是**为了延长元素生命周期**（防止 Python 侧图片对象被回收而批内悬空），印证「批不拥有像素，但 Python 绑定替你钉住元素」。
- [python/mod_cvcuda/nvcv/ImageBatch.cpp:L226-L244](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/ImageBatch.cpp#L226-L244) — pybind11 导出表。**易踩的命名坑**：属性是全小写无下划线的 `uniqueformat`、`maxsize`（不是 PEP8 风格的 `unique_format`/`max_size`）；方法为 `pushback`/`popback`/`clear`，并实现了 `__len__` 与 `__iter__`。
- [python/mod_cvcuda/nvcv/ImageBatch.cpp:L143-L154](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/ImageBatch.cpp#L143-L154) — `uniqueFormat()` 绑定实现：C++ 返回无效格式时转成 Python `None`。因此「格式不一致」与「空批」在 Python 里都表现为 `batch.uniqueformat is None`。
- [python/mod_cvcuda/nvcv/ImageBatch.cpp:L246-L248](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/ImageBatch.cpp#L246-L248) — 模块级函数 `cvcuda.as_images(buffers, format=...)`：把一列外部 buffer（须实现 `__cuda_array_interface__` 或 DLPack，见 L75-L95 的 `WrapExternalBufferVector`）零拷贝包装成变长批，并把 buffer 生命周期系在批上。

官方示例：

- [samples/datatypes/imagebatchvarshape.py:L24-L35](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/datatypes/imagebatchvarshape.py#L24-L35) — 第一组混**尺寸与格式**（RGB8/RGBA8/BGR8）；第二组连 **dtype 都混**（uint8/float32/灰度 U8），并注释 "Can even mix datatypes in same batch"。这两组批能通过容器检查，但喂给算子时多数会被 `uniqueformat` 检查拦下。

#### 4.2.4 代码实践

**实践目标**：亲手构建变长批，逐张读出尺寸，并观察混格式批的 `uniqueformat` 表现。

**操作步骤**：

1. 参考 [samples/datatypes/imagebatchvarshape.py:L24-L28](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/datatypes/imagebatchvarshape.py#L24-L28) 写脚本（示例代码，基于官方 sample 改写）：

   ```python
   import cvcuda

   batch = cvcuda.ImageBatchVarShape(capacity=10)
   imgs = [
       cvcuda.Image((640, 480), cvcuda.Format.RGB8),
       cvcuda.Image((1280, 720), cvcuda.Format.RGB8),
       cvcuda.Image((800, 600), cvcuda.Format.RGB8),
   ]
   batch.pushback(imgs)

   for i, img in enumerate(batch):        # __iter__ 逐张产出 Image
       print(i, img.size, img.width, img.height, img.format)
   print("len       =", len(batch))       # __len__ → numImages
   print("capacity  =", batch.capacity)
   print("maxsize   =", batch.maxsize)    # 期望 (1280, 720)
   print("format    =", batch.uniqueformat)  # 期望 RGB8
   ```

2. 再把三张图换成 RGB8/RGBA8/BGR8 各一（照抄官方示例第一组），重新打印 `batch.uniqueformat`。
3. 最后试对 `capacity=2` 的批 `pushback` 三张图。

**需要观察的现象**：

- 步骤 1 打印出三个不同的 `(w, h)`，`maxsize` 为其中的最大值。
- 步骤 2 中 `uniqueformat` 变为 `None`（格式不一致 → 绑定层 L143-L154 的转换路径）。
- 步骤 3 预期抛出容量不足的异常（**具体报错文案待本地验证**）。

**预期结果**：确认「容器层面允许异构、算子层面通常要求同构」的分层设计。运行需要 GPU 与已安装的 cvcuda wheel；本讲义编写环境无 GPU，以上现象标注为**待本地验证**（步骤 1/2 的行为有源码与官方示例直接支撑）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 Python 绑定的 `pushBack` 里除了 `m_impl.pushBack(img.impl())` 还要 `m_list.push_back(SharedContainerFrom(img))`？

**答案**：C++ 批只存 `Image` 的引用计数句柄的浅层引用关系由 C++ 侧管理，但 Python 对象可能被垃圾回收；`m_list` 持有 shared_ptr 把元素生命周期钉住，保证批存活期间元素不被释放（[ImageBatch.cpp:L173-L177](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/ImageBatch.cpp#L173-L177)）。

**练习 2**：`batch.maxsize` 返回的尺寸有什么用途？

**答案**：它是把该批 pad 成规则画布所需的最小宽高，`padandstack` 的 allocating 变体内部正是用 `Tensor::CreateForImageBatch(numImages, maxSize, fmt)` 按它分配输出（见 4.4.3）。

**练习 3**：如何判断一个批能否直接喂给 `cvcuda.stack`（VarShape 入口）？

**答案**：检查 `batch.uniqueformat is not None`（格式统一）且所有 `img.size` 相同；`stack` 的契约（[OpStack.h:L122-L133](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/OpStack.h#L122-L133)）要求二者同时满足。

### 4.3 GPU 端如何访问变长批：exportData 与 NVCVImageBatchVarShapeBufferStrided

#### 4.3.1 概念说明

规则 Tensor 喂给 kernel 很简单：基址 + 统一 stride，线程号线性映射到像素。变长批没有统一 stride，kernel 必须回答「第 i 张图在哪、多宽、什么格式」。CV-CUDA 的做法是 `exportData(stream)` 把批导出成一个**数据描述结构**：

- 一部分字段在**主机内存**（供 CPU 侧计算 kernel 启动配置：grid 多大、每图多宽）；
- 一部分数组在**设备内存**（供 kernel 内按图索引：第 i 张图各平面的基址与行距）。

这个「主机元数据 + 设备元数据」的双面结构是变长批算子的通用模式，也是 `NVCVImageBatchVarShapeBufferStrided` 存在的意义。

#### 4.3.2 核心流程

```
ImageBatchVarShape::exportData(stream)
        │
        ▼
NVCVImageBatchData { numImages, bufferType, buffer }
        │  bufferType == NVCV_IMAGE_BATCH_VARSHAPE_BUFFER_STRIDED_CUDA
        ▼
NVCVImageBatchVarShapeBufferStrided
 ├── uniqueFormat   : 批级格式（不一致则 NONE）          [值]
 ├── maxWidth/Height: 批内最大宽高                      [值]
 ├── formatList     : 每图格式数组                      [GPU 指针]
 ├── hostFormatList : 每图格式数组的主机副本             [主机指针]
 └── imageList      : 每图 NVCVImageBufferStrided(基址+stride) [GPU 指针]
```

kernel 侧访问第 i 张图像素的双址套路：

```
主机(启动前): 读 hostFormatList[i] / maxWidth / maxHeight → 决定 grid/block
设备(kernel 内): 读 imageList[i].planes[p] → 得到该图平面 p 的 basePtr 与 rowStride
                → 像素 (x,y) 地址 = basePtr + y*rowStride + x*(像素字节数)
```

#### 4.3.3 源码精读

- [src/nvcv/src/include/nvcv/ImageBatch.hpp:L264-L275](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/ImageBatch.hpp#L264-L275) — `ImageBatchVarShape::exportData<ImageBatchVarShapeData>` 的两个模板重载：请求 `ImageBatchVarShapeData` 时直接解引用返回（该类型对其必然兼容），请求其他类型则返回 `Optional`（可能为空）。这延续了 u2-l1 见过的「导出-尝试转换」风格。
- [src/nvcv/src/include/nvcv/ImageBatchData.h:L26-L51](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/ImageBatchData.h#L26-L51) — `NVCVImageBatchVarShapeBufferStrided` 的全部字段。逐条注释：`uniqueFormat`（批级，格式不一致或空批时为 `NVCV_IMAGE_FORMAT_NONE`）、`maxWidth/maxHeight`（注释特别警告：当值为 0 且图像数 ≥1 时**不可信赖**）、`formatList`（设备端每图格式数组）、`hostFormatList`（主机端副本）、`imageList`（设备端每图平面描述数组，元素个数为 `numPlanesPerImage*numImages`，第 i 张图第 p 平面记作 `imageList[i].planes[p]`）。
- [src/nvcv/src/include/nvcv/ImageBatchData.h:L83-L87](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/ImageBatchData.h#L83-L87) — 缓冲类型枚举：目前变长批只有一种 `NVCV_IMAGE_BATCH_VARSHAPE_BUFFER_STRIDED_CUDA`（GPU 可访问、pitch-linear、逐图 stride）。
- [src/nvcv/src/include/nvcv/ImageBatchData.hpp:L100-L155](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/ImageBatchData.hpp#L100-L155) — C++ 视图类 `ImageBatchVarShapeData` 及其 `IsCompatibleKind`：只接受上述那一种 buffer 类型；`formatList()/hostFormatList()/maxSize()/uniqueFormat()` 是对 C 结构的薄封装。
- [src/nvcv/src/include/nvcv/ImageBatchData.hpp:L158-L211](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/ImageBatchData.hpp#L158-L211) — 进一步的 `ImageBatchVarShapeDataStrided`（暴露 `imageList()`）与 `ImageBatchVarShapeDataStridedCuda`（CUDA 专属视图）。名字在 u5-l2 的 `TensorDataAccess` 一讲会再次出现，模式完全同构。

> 说明：`exportData` 返回的设备侧数组为什么既要有 `formatList` 又要有 `hostFormatList`？因为 kernel 启动配置必须在主机上决定，而主机不能同步读取设备内存（会破坏异步流水，甚至需要隐式同步）。主机读副本定 grid，设备读正本取数据，两侧各取所需。

#### 4.3.4 代码实践（源码阅读型）

**实践目标**：不用写 CUDA 代码，通过追踪一个真实算子的调用链，确认 4.3.2 的双面结构确实被这样使用。

**操作步骤**：

1. `rg "ImageBatchVarShapeDataStridedCuda" src/cvcuda/priv --files-with-matches` 找出使用该视图的算子实现文件。
2. 任选一个（例如某个支持变长批的几何算子），定位它调用 `exportData` 的行，确认它先取 `hostFormatList`/`maxSize` 之类的**主机侧**信息，再把 `imageList`（设备指针）作为 kernel 参数传入。
3. 在该 kernel（`.cu` 文件）中找到按 `imageList[i]` 索引平面基址的代码。

**需要观察的现象**：主机侧代码从不解引用设备指针；设备侧代码从不使用 `hostFormatList`。

**预期结果**：你会得到一条「主机元数据定 launch 配置、设备元数据定像素地址」的完整证据链。此实践为纯源码阅读，无需 GPU。具体算子文件的匹配结果**待确认**（取决于仓库当前版本），可从 `src/cvcuda/priv/legacy/resize_var_shape.cu` 一类文件入手。

#### 4.3.5 小练习与答案

**练习 1**：`NVCVImageBatchVarShapeBufferStrided::imageList` 指向的内存里存的是什么？第 i 张图的第 p 个平面如何访问？

**答案**：存的是 `numPlanesPerImage*numImages` 个 `NVCVImageBufferStrided`（每平面基址+stride）；第 i 张图第 p 平面是 `imageList[i].planes[p]`（[ImageBatchData.h:L45-L50](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/ImageBatchData.h#L45-L50)）。

**练习 2**：`uniqueFormat` 字段什么时候是 `NVCV_IMAGE_FORMAT_NONE`？

**答案**：批为空，或批内图像格式不一致时（字段注释 L28-L31）。

**练习 3**：为什么 `maxWidth/maxHeight` 的注释说「值为 0 且图像数 ≥1 时不可信赖」？这提示了什么实现细节？

**答案**：它提示这两个值可能来自设备侧的归约计算（求最大值），当没有显式同步把结果写回时，主机看到的可能是未更新的 0。这是「导出结构的字段并非全部随时可读」的一个警示，读别人代码时要看字段注释再决定信任哪些值。

### 4.4 TensorBatch 与「批 → 张量」的合流

#### 4.4.1 概念说明

`TensorBatch` 是「一批**形状可不同**、但 rank（维数）、dtype、layout 必须**一致**的张量」。它比 `ImageBatchVarShape` 约束更强（后者连格式都可混），又比规则 Tensor 松（各维长度可不同）。定位上的关键区别：

- `ImageBatchVarShape` 的元素是 `Image`（有图像语义：ImageFormat、平面结构）；
- `TensorBatch` 的元素是 `Tensor`（纯 N 维数组语义，布局标签来自 u2-l1 的六标签体系）。

两者常在流水线里前后衔接：解码器产出变长图像批 → 变长批算子处理 → （`stack`/`padandstack`）合流为规则 Tensor → 推理引擎。`TensorBatch` 则多用于「先各自处理一批形状相近但不同的张量，最后 stack」的中间收集场合。

#### 4.4.2 核心流程

```
cvcuda.TensorBatch(capacity=N)
   ├─ pushback(tensor)：要求与已有元素 rank/dtype/layout 一致（首个元素定基准）
   ├─ 属性：ndim / dtype / layout / capacity / len(batch)
   ├─ 索引：batch[i] 取回 Tensor；batch[i] = t 替换（setTensor）
   └─ exportData(stream)：把各张量的描述"汇集拷贝"到设备内存（异步，流完成后结构才有效）
```

合流算子的选择：

```
尺寸全部相同 ─────────────► cvcuda.stack(batch)      → 规则 Tensor（零拷贝式拼接）
尺寸不同、需统一画布 ──────► cvcuda.padandstack(batch, top, left) → maxsize 画布 Tensor
```

#### 4.4.3 源码精读

C++ 类：

- [src/nvcv/src/include/nvcv/TensorBatch.hpp:L36-L47](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/TensorBatch.hpp#L36-L47) — 类注释直说约束："can hold a list of non-uniformly shaped tensors. **Rank, data type and layout must be consistent** between the tensors."（可存形状不一的张量，但 rank/dtype/layout 必须一致。）
- [src/nvcv/src/include/nvcv/TensorBatch.hpp:L86-L111](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/TensorBatch.hpp#L86-L111) — 属性族：`capacity()`、`rank()`（**空批返回 -1**）、`numTensors()`、`dtype()`、`layout()`、`type()`。注意 rank/dtype/layout 是**批级单一属性**——这正是「不能混」的结构性原因：它们描述的是整个批，不是逐元素。
- [src/nvcv/src/include/nvcv/TensorBatch.hpp:L123-L143](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/TensorBatch.hpp#L123-L143) — `pushBack`（范围/单个）与 `popTensors/popTensor`。
- [src/nvcv/src/include/nvcv/TensorBatch.hpp:L146-L153](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/TensorBatch.hpp#L146-L153) — `exportData` 的注释值得逐字读：**"The necessary copies to GPU are scheduled on the given stream. The struct is valid after the scheduled work is finished."**（必要的 GPU 拷贝被调度到给定流上；结构体在调度的工作完成后才有效。）与 `ImageBatch` 不同，TensorBatch 的导出是**有副作用的异步操作**。
- [src/nvcv/src/include/nvcv/TensorBatch.hpp:L175-L180](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/TensorBatch.hpp#L175-L180) — `operator[]` 与 `setTensor`：可取可换。
- [src/nvcv/src/include/nvcv/TensorBatchData.hpp:L44-L71](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/TensorBatchData.hpp#L44-L71) — 导出后的 C++ 视图：`rank()/layout()/dtype()/numTensors()` 直接从底层 C 结构字段读出，说明导出结构本身就是一份「批级属性 + 逐张量描述」的快照。

Python 绑定：

- [python/mod_cvcuda/nvcv/TensorBatch.cpp:L246-L253](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/TensorBatch.cpp#L246-L253) — 类 docstring 把约束写给 Python 用户：容量须前置声明；张量形状可不同，但维数/dtype/layout 必须统一。
- [python/mod_cvcuda/nvcv/TensorBatch.cpp:L55-L73](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/TensorBatch.cpp#L55-L73) — `Create` 与 `ImageBatchVarShape::Create` 同款缓存逻辑（键 = capacity，命中即 `clear()` 复用）。
- [python/mod_cvcuda/nvcv/TensorBatch.cpp:L144-L168](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/TensorBatch.cpp#L144-L168) — `dtype()`/`layout()` 绑定实现：无效值转 `None`（空批时 `batch.dtype is None`、`batch.layout is None`）。
- [python/mod_cvcuda/nvcv/TensorBatch.cpp:L254-L273](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/TensorBatch.cpp#L254-L273) — 导出表：`layout/dtype/capacity/ndim` 属性、`__len__/__iter__/__getitem__/__setitem__`、`pushback/popback/clear`。同样注意是 `pushback`（无下划线）。
- [python/mod_cvcuda/nvcv/TensorBatch.cpp:L203-L215](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/TensorBatch.cpp#L203-L215) — `at()`：越界抛 `TensorBatchError`，错误信息带索引与批大小。
- [python/mod_cvcuda/nvcv/TensorBatch.cpp:L275-L277](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/TensorBatch.cpp#L275-L277) — `cvcuda.as_tensors(buffers, layout=...)`：把一列外部 buffer 包装成 TensorBatch（零拷贝，生命周期系于批）。

官方示例与合流算子：

- [samples/datatypes/tensorbatch.py:L25-L29](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/datatypes/tensorbatch.py#L25-L29) — 三个 shape 不同（100×100×3、150×200×3、200×150×3）但同为 uint8/HWC 的张量进同一批；L31-L39 演示「换 dtype 就要另开一个批」。
- [python/mod_cvcuda/operators/OpPadAndStack.cpp:L60-L72](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpPadAndStack.cpp#L60-L72) — `PadAndStack`（allocating 变体）的实现正文：先检查 `uniqueFormat`（混格式直接抛 "All images in the input must have the same format"），然后 `Tensor::CreateForImageBatch(input.numImages(), input.maxSize(), fmt)` 分配输出——**输出画布 = 批内最大宽高**，这正是 4.1 公式里 \(W_{\max} \times H_{\max}\) 的出处。
- [python/mod_cvcuda/operators/OpPadAndStack.cpp:L80-L96](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpPadAndStack.cpp#L80-L96) — Python 签名：`cvcuda.padandstack(src, top, left, border=CONSTANT, bvalue=0, *, stream=None)`。
- [tests/cvcuda/python/test_oppadandstack.py:L77-L83](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_oppadandstack.py#L77-L83) — 官方测试的 top/left 构造方式：`cvcuda.Tensor((1, 1, num_images, 1), np.int32, "NHWC")`，随后断言 `out.layout/out.shape/out.dtype`。
- [python/mod_cvcuda/operators/OpStack.cpp:L249-L306](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpStack.cpp#L249-L306) — Python 端 `cvcuda.stack` 注册了三个重载：`ImageBatchVarShape` 入参、`TensorBatch` 入参、`List[cvcuda.Tensor]` 入参。注意 TensorBatch 版 docstring 要求成员 "have the same data type **and shape**"——`stack` 不做任何 padding，形状不一的批只能走 `padandstack`。

三种批容器的能力对照表：

| 容器 | 元素类型 | 逐元素尺寸 | 逐元素 dtype/格式 | 批级统一属性 | 典型场景 |
|------|----------|-----------|------------------|--------------|----------|
| 规则 Tensor（N 维） | 像素 | 必须相同 | 必须相同 | shape/dtype/layout | 推理输入、融合算子输出 |
| `TensorBatch` | `Tensor` | 可不同 | 必须相同（rank/dtype/layout 一致） | rank/dtype/layout | 形状相近张量的中间收集，`stack` 前身 |
| `ImageBatchVarShape` | `Image` | 可不同 | **可不同**（但算子多要求统一） | capacity、（查询用）maxsize/uniqueformat | 解码输出、变长预处理全阶段 |

#### 4.4.4 代码实践

**实践目标**：体验 TensorBatch 的一致性约束，并用 `stack` 完成「批 → 张量」合流。

**操作步骤**：

1. 依 [samples/datatypes/tensorbatch.py:L25-L29](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/datatypes/tensorbatch.py#L25-L29) 构建批并打印属性（示例代码）：

   ```python
   import cvcuda
   import numpy as np

   batch = cvcuda.TensorBatch(capacity=10)
   batch.pushback([
       cvcuda.Tensor((100, 100, 3), np.uint8, "HWC"),
       cvcuda.Tensor((150, 200, 3), np.uint8, "HWC"),
       cvcuda.Tensor((200, 150, 3), np.uint8, "HWC"),
   ])
   print(len(batch), batch.ndim, batch.dtype, batch.layout)   # 3 3 uint8 HWC
   print(batch[1].shape)                                       # (150, 200, 3)
   ```

2. 向同一批 `pushback` 一个 `np.float32` 的张量，观察报错。
3. 对这三个同 layout 的张量调用 `out = cvcuda.stack(batch)`：TensorBatch 版 `stack` 要求成员 **shape 也相同**（[OpStack.cpp:L277-L288](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpStack.cpp#L277-L288)），因此先把三张张量改成同尺寸（如都是 100×100×3）再调用，打印 `out.shape` 与 `out.layout`。

**需要观察的现象**：步骤 1 中 `batch.ndim/dtype/layout` 是批级单值；步骤 2 触发一致性校验失败；步骤 3 得到带 N 维的规则 Tensor。

**预期结果**：步骤 2 的具体异常文案**待本地验证**；步骤 3 在同尺寸输入下 `out.shape` 的 N 等于成员样本总数（依据 [OpStack.h:L109-L112](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/OpStack.h#L109-L112) 的契约）。本实践需 GPU 环境。

#### 4.4.5 小练习与答案

**练习 1**：`cvcuda.stack` 与 `cvcuda.padandstack` 的本质区别是什么？

**答案**：`stack` 要求所有输入图/张量同尺寸同格式，做的是零 padding 拼接；`padandstack` 允许尺寸不同，按 top/left 向量逐图填充，输出画布为批内 maxsize（allocating 变体内 `Tensor::CreateForImageBatch(numImages, maxSize, fmt)`，[OpPadAndStack.cpp:L69](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpPadAndStack.cpp#L69)）。

**练习 2**：`TensorBatch::exportData` 与 `ImageBatchVarShape::exportData` 的一个重要行为差异是什么？

**答案**：`TensorBatch::exportData` 会把必要的拷贝**调度到给定流上**，结构体在流上工作完成后才有效（[TensorBatch.hpp:L146-L153](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/TensorBatch.hpp#L146-L153)）；使用它的 kernel 提交必须遵守流顺序，不能在主机直接同步读取。

**练习 3**：为什么 `TensorBatch` 无法像 `ImageBatchVarShape` 那样混合 dtype？从类的成员函数签名找证据。

**答案**：`TensorBatch::dtype()`/`layout()`/`rank()` 返回**单一** `DataType`/`TensorLayout`/`int32_t`（[TensorBatch.hpp:L91-L111](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/TensorBatch.hpp#L91-L111)）——dtype/layout 是批级属性而非逐元素数组；而 `ImageBatchVarShape` 的逐图信息存在 `formatList` 数组里（4.3.3），天然支持逐图不同。

## 5. 综合实践

**任务**：把 4.1 的内存公式落到真实对象上——构建含 3 张不同分辨率图片的 `ImageBatchVarShape`，逐张读出尺寸；再用 `cvcuda.padandstack` 把它们 pad 成同尺寸 NHWC Tensor，定量对比两种表示的显存占用。（即本讲规格指定的实践任务。）

**操作步骤**（示例代码，整合官方 sample 与官方测试的写法）：

```python
# varshape_vs_tensor.py —— 需要 GPU 与 cvcuda wheel
import cvcuda
import numpy as np

# ---- 第 1 步：变长批（三张不同分辨率、同格式 RGB8）----
sizes = [(640, 480), (1280, 720), (800, 600)]   # (w, h)
batch = cvcuda.ImageBatchVarShape(capacity=3)
batch.pushback([cvcuda.Image(sz, cvcuda.Format.RGB8) for sz in sizes])

# ---- 第 2 步：逐张读出尺寸 ----
for i, img in enumerate(batch):
    print(f"image[{i}]: size={img.size}, format={img.format}")
print("maxsize     =", batch.maxsize)          # (1280, 720)
print("uniqueformat=", batch.uniqueformat)     # Format.RGB8

# ---- 第 3 步：紧凑内存估算（按公式，忽略行距对齐）----
compact = sum(w * h * 3 for (w, h) in sizes)   # 每像素 3 字节
print(f"varshape 像素内存 ≈ {compact} B = {compact/2**20:.2f} MiB")

# ---- 第 4 步：pad 成同尺寸 NHWC Tensor ----
# top/left：int32 向量张量，形状 (1,1,N,1)（见 tests/cvcuda/python/test_oppadandstack.py L77-78）
n = len(batch)
top  = cvcuda.Tensor((1, 1, n, 1), np.int32, "NHWC")   # 全 0 → 图贴左上角
left = cvcuda.Tensor((1, 1, n, 1), np.int32, "NHWC")
out = cvcuda.padandstack(batch, top, left, bvalue=0)   # allocating 变体

# ---- 第 5 步：读取规则批内存并对比 ----
print("out.shape  =", out.shape)    # 期望 (3, 720, 1280, 3)
print("out.layout =", out.layout)   # 期望 NHWC
w_max, h_max = batch.maxsize
tensor_bytes = n * h_max * w_max * 3
print(f"tensor 像素内存   = {tensor_bytes} B = {tensor_bytes/2**20:.2f} MiB")
print(f"padding 浪费率    = {1 - compact/tensor_bytes:.1%}")
```

**需要观察的现象**：

1. 第 2 步打印出三个互不相同的尺寸，`maxsize` 恰为最大者。
2. 第 4 步 `out.shape` 的 H、W 等于 `maxsize` 的 h、w，N 等于图数，C=3；依据是 allocating 变体内部用 `Tensor::CreateForImageBatch(numImages, maxSize, fmt)` 建输出（[OpPadAndStack.cpp:L69](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpPadAndStack.cpp#L69)），且官方测试对 shape/layout/dtype 有同款断言（[test_oppadandstack.py:L80-L83](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_oppadandstack.py#L80-L83)）。
3. 浪费率约 38%（4.1.4 的手算值）。

**预期结果**（按公式预填，**待本地验证**）：

| 表示 | 字节数 | 折合 |
|------|--------|------|
| ImageBatchVarShape（紧凑） | 5,126,400 B | ≈ 4.89 MiB |
| pad 后 NHWC Tensor (3,720,1280,3) | 8,294,400 B | 7.91 MiB |
| padding 浪费 | 3,168,000 B | 38.2% |

**选做进阶**：给 `top`/`left` 填非零值让每张图在画布上**居中**（填充量 = (max - cur) // 2）。填充需要向 GPU 张量写值，最顺手的方式是经 `.cuda()` 视图或 cupy 包装（u2-l4 的 DLPack 互操作），写法**待本地验证**。官方测试中未初始化的 top/left 即按全 0 路径使用。

## 6. 本讲小结

- 规则 Tensor 批要求所有样本同宽同高，尺寸离散时 padding 浪费率为 \(\rho = 1 - \frac{\sum w_i h_i}{N W_{\max} H_{\max}}\)；变长批容器的意义是让「不整齐的阶段保持不整齐」。
- `ImageBatchVarShape` 是句柄容器：不拥有像素，逐图自带尺寸/格式元数据，甚至允许混格式（但算子多要求 `uniqueformat` 统一）；Python 属性名是全小写的 `maxsize`/`uniqueformat`/`pushback`。
- GPU 端访问变长批靠 `exportData` 导出的 `NVCVImageBatchVarShapeBufferStrided`：主机侧 `hostFormatList`/`maxWidth` 定启动配置，设备侧 `formatList`/`imageList`（每图每平面基址+stride）供 kernel 按图索引。
- `TensorBatch` 放宽尺寸、收紧类型：rank/dtype/layout 是批级单一属性，因此不可混；其 `exportData` 会向流调度拷贝，结构体在流完成后才有效。
- 合流算子二选一：`stack`（要求同尺寸同格式，零 padding 拼接）与 `padandstack`（逐图 top/left 填充，输出画布 = `maxsize`，由 `Tensor::CreateForImageBatch(numImages, maxSize, fmt)` 分配）。
- Python 侧两种批容器都以 capacity 为缓存键，从对象缓存取用复用（`Create` 里的 fetch/clear 逻辑），细节在 u4-l2 展开。

## 7. 下一步学习建议

- **下一讲 u2-l4（零拷贝互操作）**：综合实践中「向 top/left 填值」的伏笔在那里解决——`as_tensor` 与 DLPack 如何把 numpy/cupy/PyTorch 缓冲零拷贝接入 CV-CUDA。
- **u3-l1（resize 与 flip）**：把本讲的变长批喂给真实算子，观察同一算子的 Tensor 入口与 VarShape 入口两套 API。
- **u5-l2（GPU 数据访问）**：若你想写自己的 CUDA kernel 消费变长批，`TensorDataAccess`/`ImageBatchVarShapeDataStridedCuda` 的完整访问模式在那里展开。
- 继续阅读建议：对照 [samples/datatypes/](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/datatypes/tensorbatch.py) 目录其余示例，以及 [tests/cvcuda/python/test_oppadandstack.py](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_oppadandstack.py) 里更多格式/布局参数化组合。
