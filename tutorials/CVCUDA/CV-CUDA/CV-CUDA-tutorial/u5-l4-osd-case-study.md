# 复杂算子案例一：OSD 屏幕叠加渲染

## 1. 本讲目标

学完本讲，你应该能够：

1. 用 Python 的 `cvcuda.Elements` 与一系列元素类（`BndBoxI`、`Label`、`Point`、`Line`、`Circle`、`PolyLine`、`Clock` 等）描述一帧图像上的标注，并调用 `cvcuda.osd` / `cvcuda.osd_into` 完成 GPU 合成。
2. 解释 OSD 与普通逐像素算子的本质区别：它的"参数"是一个异构的场景描述，因此内部走了一条 **CPU 收集命令 → 序列化上传 → 单 kernel 合成** 的三阶段流水线。
3. 读懂 `priv/legacy/osd.cu` 里命令字节流、文本位图（text bitmap）、2×2 像素块渲染与 alpha 混合的实现。
4. 理解 priv 层如何用一个 "planar 桥"（reformat 前后夹住 legacy kernel）让只认 NHWC 的老内核支持 NCHW/CHW 布局。
5. 了解 OSD 在 aarch64/Jetson 平台上的已知限制（文本渲染问题导致相关测试被跳过）。

## 2. 前置知识

### 2.1 什么是 OSD

OSD（On Screen Display，屏幕叠加显示）指的是把**标注信息直接画到画面上**：目标检测的边界框、类别标签文字、跟踪轨迹线、关键点、时间戳等。凡是做过可视化管线（比如 DeepStream）的人都在消费 OSD 的输出。

OSD 的计算量不大，但麻烦在于**参数形状异构**：

- `resize` 的参数是两个整数（宽、高）；
- `cvtcolor` 的参数是一个枚举（转换码）；
- 而 OSD 的参数是"这一帧上要画什么"——可能是一条线、一段文字、三个圆、五个点，每个元素又有各自的坐标、颜色、粗细、透明度。

常规张量参数装不下这种"变长的、类型各异的"描述，所以 OSD 必须引入一套**元素（Element）抽象**和一套**序列化协议**。

### 2.2 立即模式与保留模式

图形 API 有两种经典设计：

- **立即模式（immediate mode）**：调用一次画一次，API 不记住任何东西。
- **保留模式（retained mode）**：先把图元攒到一个场景结构里，再一次性的渲染。

CV-CUDA 的 OSD 是两者的混合：`cvcuda.osd(img, elements)` 一次调用内，先在 **CPU 侧把所有元素翻译成命令（command）对象**攒进上下文，再把命令**打包上传到 GPU**，最后**一个 CUDA kernel 遍历所有命令完成合成**。理解这条"命令流"是本讲的主线。

### 2.3 Alpha 混合（source-over 合成）

叠加层是半透明的：颜色 \( C_{src} \) 带透明度 \( \alpha_{src} \)（0 全透明，255 全不透明），底图像素是 \( (C_{dst}, \alpha_{dst}) \)。标准 source-over 合成为：

\[
\alpha_{out} = \alpha_{dst}\,(1-\alpha_{src}) + \alpha_{src}
\]

\[
C_{out} = \frac{C_{dst}\,\alpha_{dst}\,(1-\alpha_{src}) + C_{src}\,\alpha_{src}}{\alpha_{out}}
\]

工程上 \( \times(255-\alpha)/255 \) 用移位 `>> 8`（除以 256）近似。记住这个公式，后面读 `blend_single_color` 时会一一对应。

### 2.4 前置讲义回顾

本讲是第五单元"算子内部解剖"的第四讲，承接：

- **u5-l1** 建立的四层链路：Python 绑定 → C API → priv 实现 → legacy kernel；
- **u5-l2** 的 `exportData` / `TensorDataAccess` 机制（priv 层如何拿到 GPU 数据视图）；
- **u5-l3** 的 legacy 内核形态（`priv/legacy/*.cu`、`CudaBaseOp`、`DataShape`）。

OSD 是这条链路上**状态最复杂**的算子——它是少数在算子对象内部持有可变上下文（`cuOSDContext`）的 legacy 算子，因此是检验前几讲知识的绝佳案例。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [samples/operators/osd.py](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/operators/osd.py) | 官方示例：构造 7 种元素并调用 `cvcuda.osd` |
| [python/mod_cvcuda/OsdElement.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/OsdElement.cpp) | 元素类的 pybind11 绑定：`BndBoxI`/`Label`/`Point`/`Elements` 等 |
| [src/cvcuda/priv/Types.hpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/Types.hpp) | 元素的 C++ 数据结构：`NVCVText`、`NVCVElement`（variant）、`NVCVElementsImpl` |
| [python/mod_cvcuda/operators/OpOSD.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpOSD.cpp) | `cvcuda.osd` / `osd_into` 两个入口函数 |
| [src/cvcuda/priv/OpOSD.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpOSD.cpp) | priv 实现：布局校验 + NCHW→NHWC 的 "planar 桥" |
| [src/cvcuda/priv/legacy/osd.cu](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/legacy/osd.cu) | 核心：命令收集、序列化、文本位图、渲染 kernel（约 2000 行） |
| [src/cvcuda/priv/legacy/textbackend/](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/legacy/textbackend) | 文本后端：基于 stb_truetype 的字形光栅化 |
| [src/cvcuda/include/cvcuda/OpOSD.h](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/OpOSD.h) | C API 与 Limitations 契约表 |
| [tests/cvcuda/system/TestOpOSD.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/system/TestOpOSD.cpp) | C++ 系统测试（含 aarch64 跳过逻辑） |

一条调用在四层之间的穿越路径（复习 u5-l1 的四层结构）：

```
cvcuda.osd(tensor, elements)                  # Python
  └─ cvcudapy::OSD() / OSDInto()              # python/mod_cvcuda/operators/OpOSD.cpp
      └─ op->submit(stream, in, out, NVCVElements)   # C ABI 界碑
          └─ cvcuda::priv::OSD::operator()    # src/cvcuda/priv/OpOSD.cpp
              └─ legacy::OSD::infer(...)      # src/cvcuda/priv/legacy/osd.cu
                  ├─ cuosd_draw_elements      # CPU：元素 → 命令
                  ├─ cuosd_apply              # CPU：命令 → 字节流 → H2D
                  └─ cuosd_launch             # GPU：render_elements_kernel
```

## 4. 核心概念与源码讲解

### 4.1 Python 侧的元素体系：OsdElement.cpp 与 Types.hpp

#### 4.1.1 概念说明

用户在 Python 里看到的 OSD 接口是一组**值对象（value object）**：`cvcuda.BndBoxI(...)`、`cvcuda.Label(...)`、`cvcuda.Point(...)`……每个对象只是把构造参数原样存起来，本身不做任何计算。真正的容器是 `cvcuda.Elements`，它是一个**列表的列表**：外层长度等于批大小，第 n 个内层列表属于批中第 n 张图。

这套设计的关键点：

- 元素是**不可变的**——构造后属性只读（`def_property_readonly`），要改就重建一个；
- `Elements` 在构造时就把每个 Python 元素**翻译成 C++ 的 `NVCVElement`**（带类型标签的 variant），后续不再回 Python 拿数据；
- 少数元素（`PolyLine`、`Segment`）携带数组参数，构造时就会发生一次 `cudaMalloc` + 拷贝。

#### 4.1.2 核心流程

以 `cvcuda.BndBoxI(box, thickness, borderColor, fillColor)` 为例：

1. pybind11 的 lambda 构造器把 Python tuple 转成 C 结构（`pytobox` / `pytocolor`）；
2. 填入 `NVCVBndBoxI` POD 结构返回给 Python 持有；
3. 用户把它放进 `cvcuda.Elements(elements=[[...], ...])`；
4. `Elements` 构造器对每个 item 做 `pybind11::isinstance<T>` 链式判断，包装成 `NVCVElement(NVCVOSDType::NVCV_OSD_RECT, &rect)`；
5. 全部装进 `NVCVElementsImpl`（`vector<vector<shared_ptr<NVCVElement>>>`）。

#### 4.1.3 源码精读

先看 tuple → C 结构的三个翻译函数。颜色转换值得注意：**alpha 缺省为 255（完全不透明）**，且 RGB 三元组也会隐式得到 `a=255`：

[NVCVColorRGBA 转换：OsdElement.cpp:L99-L114](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/OsdElement.cpp#L99-L114)
这段代码把最多 4 个分量的 Python tuple 逐字节写进 `NVCVColorRGBA`，先置 `a = 255` 再覆盖——所以 `(255, 0, 0)` 是不透明红，而 `(255, 0, 0, 128)` 是半透明红。**alpha=0 表示该颜色完全不参与绘制**，后面 legacy 层会以此做短路判断。

`BndBoxI` 的绑定是所有元素类的模板——构造 lambda + 只读属性：

[BndBoxI 绑定：OsdElement.cpp:L167-L193](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/OsdElement.cpp#L167-L193)
`py::init` 里的 lambda 把 4 个 Python 参数翻译成 C++ POD；`def_property_readonly` 保证元素构造后不可变（这也让 pybind11-stubgen 能生成干净的 tuple 类型签名）。

文本元素 `Label` 多一个默认字体参数：

[Label 绑定：OsdElement.cpp:L195-L202](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/OsdElement.cpp#L195-L202)
`fontName` 默认 `"DejaVuSansMono"`，与 C++ 侧的常量一致：

[DEFAULT_OSD_FONT：Types.hpp:L49-L52](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/Types.hpp#L49-L52)
注释里给出了字体的安装方式（`apt-get install ttf-dejavu`）——**字体文件在运行时才加载，宿主机上没有该字体时文本渲染会失败**，这是部署时常见的坑。

`Elements` 构造器是本模块的核心——一条 `isinstance` 链完成 Python 对象到带标签 variant 的翻译：

[Elements 构造器：OsdElement.cpp:L372-L444](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/OsdElement.cpp#L372-L444)
遍历外层（批）与内层（元素），对每个 item 依次做 10 个 `isinstance` 判断，包装成 `NVCVElement(类型标签, &元素)`。注意两个细节：一是**元素按出现顺序保留**（这决定了后续绘制顺序，先画的先合成、后画的叠在上面）；二是无法识别的类型会变成 `NVCV_OSD_NONE` 空元素——它会在 legacy 层触发参数错误（见 4.4.3）。

C++ 侧的 `NVCVElement` 是一个 variant：

[NVCVElement：Types.hpp:L225-L239](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/Types.hpp#L225-L239)
`Data` 是 11 种可能的变体（`monostate` + 10 种元素）。这是"异构参数"在 C++ 里的标准表达：一个类型标签 + 一个 union 式载荷。

`NVCVElementsImpl` 则是纯二维数组的薄封装：

[NVCVElementsImpl：Types.hpp:L467-L481](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/Types.hpp#L467-L481)
它只提供 `batch()`、`numElementsAt(b)`、`elementAt(b, i)` 三个只读访问器——一个纯粹的"场景描述"容器。

有一个性能细节值得指出：**带数组参数的元素在构造时就动了 GPU**。以 `PolyLine` 为例：

[NVCVPolyLine 构造：Types.hpp:L161-L175](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/Types.hpp#L161-L175)
构造函数把点集拷到 host 的 `std::vector`，随即 `cudaMalloc` 设备内存并 `cudaMemcpy` H2D。也就是说，**每 new 一个 `PolyLine`/`Segment` 就发生一次同步的显存分配**；`Segment` 更进一步，其拷贝构造（用于 variant 装箱）还会做一次 D2D 复制。在循环里反复构造这类元素会带来可观的 CPU 开销——生产管线中应当复用元素对象或改用预分配方案。

#### 4.1.4 代码实践

**实践目标**：验证"元素是不可变值对象 + 颜色 alpha 语义"。

**操作步骤**：

1. 运行官方示例（需要 GPU 与 `cvcuda` wheel、`samples/` 里的 `common.py`）：
   ```bash
   cd samples/operators
   python3 osd.py -i ../../docs/sphinx/content/tabby_tiger_cat.jpg -o /tmp/osd_out.jpg
   ```
2. 在 Python 里检查属性只读性与颜色语义：
   ```python
   import cvcuda
   b = cvcuda.BndBoxI(box=(10, 10, 100, 80), thickness=3,
                       borderColor=(255, 0, 0), fillColor=(0, 128, 255, 64))
   print(b.box, b.thickness, b.borderColor, b.fillColor)
   b.thickness = 10   # 预期抛出 AttributeError
   ```
3. 把示例中 `Label` 的 `bgColor` 从 `(0, 0, 0, 180)` 改成 `(0, 0, 0, 0)`（alpha=0），重新运行。

**需要观察的现象**：步骤 2 打印出的 `borderColor` 是 4 元组 `(255, 0, 0, 255)`——3 元组输入被补上了 alpha=255；给只读属性赋值会抛 `AttributeError`。步骤 3 中文本背景矩形消失（alpha=0 的背景色不绘制）。

**预期结果**：元素表现为只读值对象；alpha 通道语义与 2.3 节公式一致。若本机无 GPU，步骤 1/3 标注「待本地验证」，但步骤 2 的属性检查在能 `import cvcuda` 的机器上即可完成。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `cvcuda.Elements` 的构造参数是"列表的列表"，而不是一个平的列表？

**答案**：OSD 支持批处理（NHWC 的 N 可以大于 1），批中**每张图有自己的一组元素**。外层列表的第 n 项对应第 n 张图。内核通过 `command_offsets[num_command + batch_idx]` 把命令按批划分区间（见 4.5.3），每个 block 只遍历自己那张图的命令。平的列表无法表达"哪个元素属于哪张图"。

**练习 2**：示例代码里 `elements` 内层列表的顺序有什么影响？

**答案**：决定合成顺序。kernel 按命令在数组中的出现顺序逐个叠加（[osd.cu:L1050-L1105](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/legacy/osd.cu#L1050-L1105) 的 for 循环），后处理的元素画在先处理的上面。想让文字不被方框盖住，就把 `Label` 放在 `BndBoxI` 之后。

**练习 3**：`cvcuda.Point(centerPos, radius, color)` 与 `cvcuda.Circle(...)` 有什么区别？

**答案**：`Point` 是关键点标注——一个实心小圆点，参数只有圆心、半径、颜色（[OsdElement.cpp:L221-L236](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/OsdElement.cpp#L221-L236)）；`Circle` 是几何图形——有边框色/填充色/线宽三分参数。二者在 C++ 侧映射到不同的 `NVCVOSDType`（`NVCV_OSD_POINT` vs `NVCV_OSD_CIRCLE`），内核里走不同的渲染函数。

### 4.2 绑定层入口：`cvcuda.osd` 与 `cvcuda.osd_into`

#### 4.2.1 概念说明

与所有 CV-CUDA 算子一样，OSD 有 allocating（`osd`）与 `_into`（`osd_into`）两个变体（见 u3-l3）。但 OSD 有两点与众不同：

1. **没有变长批（var-shape）版本**——OSD 只接受规则 Tensor（NHWC/NCHW），不接受 `ImageBatchVarShape`；
2. **第三个参数不是张量而是 `Elements` 对象**，跨 C ABI 时它被当作一个不透明指针（`NVCVElements` 实为 `void*`）传递。

#### 4.2.2 核心流程

`OSDInto(dst, src, elements, stream)` 的执行序列：

1. 流参数缺省时取当前流（`Stream::Current()`）；
2. `CreateOperator<cvcuda::OSD>()` 从算子缓存取/建 C++ 算子对象；
3. `ResourceGuard` 登记输入（读锁）、输出（写锁）与算子（无锁），跨流时自动插入事件等待（u4-l1 讲过的三层安全机制）；
4. 把 `shared_ptr<NVCVElementsImpl>` 裸转成 `NVCVElements`（`void*`）传给 `op->submit`；
5. 返回 `dst`。

allocating 版本先 `Tensor::Create(input.shape(), input.dtype())` 建输出——**输出与输入形状、dtype、layout 完全一致**（OSD 不改变图像几何），再委托 `_into`。

#### 4.2.3 源码精读

[OSDInto：operators/OpOSD.cpp:L34-L54](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpOSD.cpp#L34-L54)
注意 L49-L51：`elementsHandle` 通过两层 `static_cast` 把 `NVCVElementsImpl*` 变成 `NVCVElements`（其真身是 `void*`）。这是**跨 ABI 传 C++ 对象的常用桥接手法**：C API 不认识 C++ 类型，只透传指针，priv 层再转型回去。绑定层用 `shared_ptr` 保住 `Elements` 的生命周期，使其在 `guard.run` 执行期间不被回收。

[OSD allocating 变体：operators/OpOSD.cpp:L56-L61](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpOSD.cpp#L56-L61)
三行代码印证 u3-l3 的结论：allocating 只是 `_into` 的薄包装，多一次输出分配（该分配走 Python 对象缓存）。

两个入口的导出与文档：

[导出 osd / osd_into：operators/OpOSD.cpp:L69-L96](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpOSD.cpp#L69-L96)
`NvtxTrace` 包装器给每个调用自动加 NVTX 区间（u7-l4 会展开）；参数名 `src` / `elements` / `stream` 就是 Python 里看到的关键字参数。

#### 4.2.4 代码实践

**实践目标**：确认 `osd` 与 `osd_into` 输出等价、`Elements` 对象可跨调用复用。

**操作步骤**：

```python
import cvcuda, numpy as np
from samples.common import read_image, write_image  # 或按 u1-l2 的方式自建张量

img = read_image("input.jpg")            # HWC RGB8
h, w, c = img.shape
nhwc = img.reshape((1, h, w, c), "NHWC")

elements = cvcuda.Elements(elements=[[cvcuda.BndBoxI(box=(20, 20, w//3, h//3),
                                                     thickness=3,
                                                     borderColor=(255, 0, 0),
                                                     fillColor=(0, 0, 255, 64))]])

out1 = cvcuda.osd(nhwc, elements)                      # allocating
out2 = cvcuda.Tensor((1, h, w, c), np.uint8, "NHWC")   # 预分配
cvcuda.osd_into(out2, nhwc, elements)                  # _into

a = np.asarray(out1.cuda()) if hasattr(out1, "cuda") else None
```

**需要观察的现象**：`out1.shape == out2.shape == nhwc.shape`，dtype/layout 均为 RGB8/NHWC；两个输出的像素内容一致。可以再用 `elements` 连续调用多次 `osd`，确认同一 `Elements` 对象能重复使用。

**预期结果**：两变体输出逐像素一致；`Elements` 可复用。**待本地验证**（需要 GPU 环境）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 OSD 不提供 var-shape（`ImageBatchVarShape`）入口？

**答案**：OSD 的定位是**可视化输出**，通常发生在管线末端，此时数据大多已回到规则张量；而 legacy 内核以统一的行距（stride）寻址（`CreateTensorWrapNHWC`），变长批的逐图不同尺寸/行距模型与其命令渲染设计不匹配。若需处理变尺寸图像，可先 `padandstack`（u2-l3）成规则批再调用 OSD。

**练习 2**：`guard.add(LockMode::LOCK_MODE_NONE, {*op})` 中给算子加"无锁"登记的目的是什么？

**答案**：不是加锁，而是**保活**（keep-alive）。`ResourceGuard` 会保证在本次 submit 完成前算子对象不被析构——OSD 的 legacy 实现内部持有 `cuOSDContext` 等有状态资源，内核还在流上跑时对象不能先死。`LOCK_MODE_NONE` 表示不需要读写语义的记账，只要生命周期。

### 4.3 priv 层：布局桥接与 NCHW 支持

#### 4.3.1 概念说明

u5-l3 讲过：legacy 内核只认 **NHWC/HWC（交错）布局**。而 CV-CUDA 的仓库不变量要求图像算子默认同时支持 planar（NCHW/CHW）布局。OSD 的解法不是重写内核，而是在 priv 层加一个 **"planar 桥"（Planar Bridge）**：

```
NCHW 输入 ──Reformat──> NHWC 临时张量 ──OSD legacy kernel──> NHWC 临时张量 ──Reformat──> NCHW 输出
```

代价是两次布局转换（各一个 kernel）+ 两块临时显存。收益是 legacy 内核零改动。这是"适配层换兼容性"的典型取舍。

桥需要状态：两块 NHWC 临时张量必须**跨调用复用**（否则每次 submit 都要分配），而复用又引出**跨流安全问题**——上一次调用写入临时张量的 kernel 可能还没跑完。解决方案是每设备一份 workspace，用 mutex + CUDA event 管理待决（pending）状态。

#### 4.3.2 核心流程

`operator()` 的决策树：

```
exportData(in/out) 失败?           → 抛 ERROR_INVALID_ARGUMENT（非 CUDA 可访问数据）
in/out 是 NCHW/CHW?
 ├─ 是（且二者布局一致）
 │    取 per-device workspace（加锁）
 │    prepare:  需要时同步 → 重建临时张量 → reformat(in → NHWC临时)
 │    legacy infer(NHWC临时in, NHWC临时out, elements)
 │    finish:   reformat(NHWC临时out → out)，record event
 │    （异常路径也要 markPending，防止提前复用缓冲）
 └─ 否（都是 NHWC/HWC 且布局一致）
      直接 legacy infer(inData, outData, elements)
否则 → 抛异常（混合布局、或既非 planar 也非 interleaved）
```

workspace 的同步策略：

- 本次目标 shape/dtype 与上次相同 → 只做 `cudaStreamWaitEvent`（流上异步等待，零 CPU 阻塞）；
- shape/dtype 变了（要重建缓冲） → `cudaEventSynchronize`（真正等上一次渲染完成），因为重建意味着旧张量要被释放。

#### 4.3.3 源码精读

布局判定与形状推导：

[布局判定：OpOSD.cpp:L39-L47](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpOSD.cpp#L39-L47)
[NCHW→NHWC 形状推导：OpOSD.cpp:L49-L75](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpOSD.cpp#L49-L75)
`InterleavedShapeForOSD` 用 `TensorDataAccessStridedImagePlanar`（u5-l2 讲过的按标签寻址访问器）读出 N/H/W/C 四个维度，重排成 NHWC 形状。注意这个访问器同时兼容 NHWC 与 NCHW 输入——planar 与 interleaved 在它的抽象下统一。

主流程 `operator()`：

[OSD::operator()：OpOSD.cpp:L193-L246](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpOSD.cpp#L193-L246)
L197-L209 是 u5-l2 讲过的"exportData 判空即拦截"模式；L211-L236 走 planar 桥；L238-L245 是 interleaved 快路径——一行 `m_legacyOp->infer` 直通 legacy。

planar 桥的核心 try/catch 值得一读：

[异常安全的桥：OpOSD.cpp:L221-L235](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpOSD.cpp#L221-L235)
即使 legacy infer 抛异常，`catch` 里也先 `markPending(stream)` 再 re-throw——因为 reformat 输入的 kernel 已经入队，临时张量仍是"在用"状态，直接重建会撕裂数据。

workspace 的同步逻辑：

[synchronizeIfNeeded：OpOSD.cpp:L143-L167](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpOSD.cpp#L143-L167)
这正是 u8-l3 将展开的"用 Event 回收安全内存"模式的一个现场版：**shape 不变走事件等待（快路径），shape 变化走设备同步（慢路径）**。

算子持有 per-device workspace：

[OSD 成员：OpOSD.hpp:L90-L94](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpOSD.hpp#L90-L94)
`PerDeviceResource<OSDPlanarBridgeWorkspace>` 按 CUDA 设备惰性创建并缓存实例，保证多 GPU 场景下每张卡用自己的临时张量（分配跟随设备）。其契约见 [PerDeviceResource.hpp:L32-L45](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/PerDeviceResource.hpp#L32-L45) 的注释。

构造函数：

[OSD 构造：OpOSD.cpp:L183-L189](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpOSD.cpp#L183-L189)
成员只有两个算子对象：legacy OSD 与一个 Reformat（用于桥的两次布局转换）。`maxIn/maxOut` 这两个 `DataShape` 参数在 OSD 里并不使用（u5-l3 提过 legacy 接口的惯例）。

#### 4.3.4 代码实践（源码阅读型）

**实践目标**：验证"planar 路径 = 2 次 reformat + 1 次 OSD"的调用结构。

**操作步骤**：

1. 打开 [src/cvcuda/priv/legacy/osd.cu:L1870-L1880](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/legacy/osd.cu#L1870-L1880)，确认 `OSD::infer` 开头就拒绝非 NHWC/HWC 布局（`INVALID_DATA_FORMAT`）——这就是桥存在的原因。
2. 在纸上画出 NCHW 输入时的完整 kernel 序列：`reformat(NCHW→NHWC)` → `render_elements_kernel` → `reformat(NHWC→NCHW)`，并标注每一步消费/生产哪块内存（输入张量、两块桥张量、输出张量）。
3. 用 Nsight Systems（u7-l4 的方法）profile 下面的脚本，在时间线上数一数 kernel 个数：

```python
import cvcuda, numpy as np
n, h, w = 1, 480, 640
src = cvcuda.Tensor((n, 3, h, w), np.uint8, "NCHW")   # planar 输入
dst = cvcuda.Tensor((n, 3, h, w), np.uint8, "NCHW")
els = cvcuda.Elements(elements=[[cvcuda.BndBoxI(box=(50, 50, 200, 150),
                                                 thickness=2,
                                                 borderColor=(0, 255, 0),
                                                 fillColor=(0, 0, 0, 0))]])
cvcuda.osd_into(dst, src, els)
```

**需要观察的现象**：NCHW 路径的时间线上应出现 3 个 kernel（两次布局转换 + 一次渲染）；把输入输出换成 NHWC 再跑，只剩 1 个渲染 kernel。

**预期结果**：planar 桥确实引入两次额外 kernel 启动与显存搬运。对性能敏感的管线，**优先用 NHWC 布局调用 OSD**。步骤 3 需 GPU，待本地验证；步骤 1-2 纯源码阅读即可完成。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `OSDPlanarBridgeWorkspace` 要持有一个 `std::mutex`，而普通算子不需要？

**答案**：普通算子无状态，可以随意并发；而 workspace 持有**跨调用共享的可变资源**（两块 NHWC 临时张量 + event）。两个线程同时进入 prepare 会竞争同一块缓冲导致数据撕裂。`lock()` 返回的 `unique_lock` 保证同一 workspace 上一次只有一个 planar 路径在跑。

**练习 2**：如果输入是 NCHW 而输出是 NHWC，会发生什么？

**答案**：抛 `ERROR_INVALID_ARGUMENT`，见 [OpOSD.cpp:L213-L219](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpOSD.cpp#L213-L219)——桥要求 `!inPlanar || !outPlanar || layout 不同` 三者任一为真即拒绝。这与 C 头 Limitations 的 "Input == Output" 契约一致。

**练习 3**：`resizeBuffer`（[OpOSD.cpp:L169-L176](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpOSD.cpp#L169-L176)）在什么条件下会真正分配新张量？

**答案**：仅当临时张量不存在、shape 或 dtype 与目标不符时。同形状的重复调用（视频流场景：每帧同分辨率）零分配——只有第一次调用付分配成本，之后全部复用。

### 4.4 legacy 层 CPU 侧：元素 → 命令 → 设备字节流

#### 4.4.1 概念说明

`legacy::OSD::infer` 是 OSD 真正的执行体。它把"渲染"拆成三个 CPU 函数 + 一个 GPU kernel：

| 阶段 | 函数 | 位置 | 做什么 |
|------|------|------|--------|
| 收集 | `cuosd_draw_elements` | CPU | 遍历元素，按类型调用 `cuosd_draw_xxx`，生成命令对象 |
| 序列化 | `cuosd_apply` | CPU + H2D | 命令对象打包成字节流，连同偏移表上传 GPU |
| 文本 | `cuosd_text_prepare` | CPU + H2D | 光栅化字形到一张位图，生成字形位置表 |
| 合成 | `render_elements_kernel` | GPU | 每线程处理 2×2 像素，遍历命令做 alpha 混合 |

关键数据结构是**命令（Command）**。legacy 层把所有元素归一化为 5 种命令：`RectangleCommand`（矩形/线/旋转框最终都变成它）、`TextCommand`、`CircleCommand`、`SegmentCommand`、`PolyFillCommand`。每种大小不同，所以设备侧存放的是**变长结构体的字节流 + 偏移表**。

**为什么"线"也是矩形？** 因为带粗细的线段本质上是一个旋转的长方形——四个顶点可由端点、角度、半粗细解析算出。把线、边界框、填充框统一成"四边形 + 可选内四边形"，内核只需要一个四边形命中测试函数（`inbox_single_pixel`），这是很干净的设计。

#### 4.4.2 核心流程

`OSD::infer` 的完整流程（[osd.cu:L1870-L1971](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/legacy/osd.cu#L1870-L1971)）：

```
1. 校验：布局 ∈ {NHWC, HWC}，dtype == U8，通道 ∈ [3,4]，in/out 同 dtype 同形状
2. 校验：elements->batch() == 张量批大小
3. cuosd_draw_elements : 元素 → 命令对象（CPU 内存）
4. cuosd_apply         : 命令 → 字节流 + 偏移表，cudaMemcpy H2D（流式）
5. cuosd_launch        : 选分派表项，发射 render_elements_kernel
6. cuosd_clear         : 清空 CPU 侧命令列表（为下一次调用做准备）
```

注意第 6 步：**每次 infer 都从空命令表开始、用完即清**。这解释了 4.1.5 练习 2 中"参数改了就重画"的机制——命令表不跨调用保留。

#### 4.4.3 源码精读

`cuosd_draw_elements` 的类型分派：

[cuosd_draw_elements：osd.cu:L1760-L1833](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/legacy/osd.cu#L1760-L1833)
两层循环（批 × 元素），`switch (element->type())` 把 variant 里的数据取出来交给对应的 `cuosd_draw_xxx`。`NVCV_OSD_NONE`（未识别类型）直接返回 `INVALID_PARAMETER`——这就是 4.1.3 里 Elements 构造器中"无法识别类型"的下场。

线段如何变成矩形命令：

[cuosd_draw_line：osd.cu:L101-L144](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/legacy/osd.cu#L101-L144)
用 `atan2` 求线段角度，沿法线方向偏移 ±half_thickness，解析算出旋转长方形的 a/b/c/d 四个顶点（L115-L122），再算包围盒（L139-L142）。注意 L127-L128：需要插值的线会置 `context->have_rotate_msaa = true`——这个标志最终决定 kernel 选哪个模板实例（见 4.5.3）。

矩形命令的"填充 + 边框"两段式：

[cuosd_draw_rectangle：osd.cu:L146-L231](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/legacy/osd.cu#L146-L231)
`thickness == -1` 是"纯填充"约定（填充色直接用边框色）；否则生成两个矩形：外框（外接 + half_thickness）与内框（内缩 half_thickness），内核渲染时"在外框内且不在内框内"的像素即边框。`bgColor.a == 0` 时跳过填充命令——alpha=0 短路，与 4.1.3 呼应。

文本的两级结构：

[cuosd_draw_text：osd.cu:L233-L273](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/legacy/osd.cu#L233-L273)
先经文本后端把 UTF-8 切成词（glyph 序列），`measure_text` 量出文本尺寸；若有背景色则先压入一个背景矩形命令（复用矩形渲染）；最后压入 `TextHostCommand`——注意它此刻**不含像素数据**，只是"待渲染文本"的宿主侧记录。

真正的字形光栅化延后到 `cuosd_text_prepare`：

[cuosd_text_prepare：osd.cu:L275-L400](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/legacy/osd.cu#L275-L400)
遍历所有文本命令，`build_bitmap` 把全部需要的字形**光栅化进同一张大位图**（stb_truetype 后端，位于 [src/cvcuda/priv/legacy/textbackend/](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/legacy/textbackend)，字形字号上限 `MAX_FONT_SIZE = 200` 见 [backend.hpp:L33](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/legacy/textbackend/backend.hpp#L33)）；随后为每个字形生成一条 `TextLocation`（图像上的落点 `image_x/y` + 位图上的取材窗口 `text_x/w/h`），两张表（`text_location`、`line_location_base`）拷入设备内存（L398-L399）。**字形位图按需构建并驻留 GPU，重复文本近乎零成本**——这是"画 100 个相同标签几乎不比画 1 个贵"的原因。

命令字节流的序列化：

[cuosd_apply：osd.cu:L402-L499](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/legacy/osd.cu#L402-L499)
第一遍循环（L424-L451）累计每种命令的字节数、算全局包围盒、把文本行编号写入 `reserved` 字段；L453-L459 构造**按批划分的偏移表**：`cmd_offset[num_commands + batch_idx]` 记录批 batch_idx 的第一条命令下标；第二遍循环（L470-L495）用 `memcpy` 把每个命令对象平铺进字节流。最终三次 H2D 拷贝：命令字节流、偏移表（L496-L497），加上 prepare 里的两张文本表。

`OSD::infer` 本体与就地判断：

[OSD::infer 主流程：osd.cu:L1946-L1968](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/legacy/osd.cu#L1946-L1968)
L1959-L1960 用 `CreateTensorWrapNHWC<uint8_t>` 把导出数据包成内核友好的轻量 wrapper（u5-l2）；**L1961 `inplace = inData.basePtr() == outData.basePtr()` 用基址指针判断就地与否**——就地时内核只扫全局包围盒（改动只发生在这块区域），非就地时要全图扫描（未覆盖像素也要从 src 拷到 dst）。L1968 的 `cuosd_clear` 清空命令表，保证下一次调用干净开始。

构造与上下文：

[OSD::OSD：osd.cu:L1850-L1854](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/legacy/osd.cu#L1850-L1854)
构造只是 `new cuOSDContext()`——这个上下文持有命令列表、文本后端、设备缓冲等全部可变状态，是 OSD"有状态"的根源，也解释了为什么 priv 层的 planar workspace 需要加锁。

#### 4.4.4 代码实践（源码阅读型）

**实践目标**：亲手追踪"一个 `BndBoxI` 元素最终在设备内存里变成什么"。

**操作步骤**：

1. 从 [osd.py:L57-L62](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/operators/osd.py#L57-L62) 的 `BndBoxI(box, thickness=3, borderColor, fillColor)` 出发；
2. 在 [OsdElement.cpp:L167-L178](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/OsdElement.cpp#L167-L178) 确认它变成 `NVCVBndBoxI`（POD）；
3. 在 [osd.cu:L1777-L1781](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/legacy/osd.cu#L1777-L1781) 确认 `NVCV_OSD_RECT` 分支调用 `cuosd_draw_rectangle`；
4. 在 [osd.cu:L146-L231](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/legacy/osd.cu#L146-L231) 数一数：因为 `fillColor.a = 64 ≠ 0` 且 `thickness = 3 ≠ -1`，会压入 **2 个** `RectangleCommand`（一个填充框 + 一个 3 像素边框）；
5. 在 [osd.cu:L443-L444](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/legacy/osd.cu#L443-L444) 确认每个命令占 `sizeof(RectangleCommand)` 字节，被 `memcpy` 进字节流。

**需要观察的现象**（纯阅读，无需运行）：一个带填充与边框的 BndBoxI 对应设备字节流中的 2 条矩形命令；示例 osd.py 的 7 个元素展开后命令数 ≥ 7（Label 还会额外产生一个背景矩形命令，见 [osd.cu:L265-L269](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/legacy/osd.cu#L265-L269)）。

**预期结果**：画出一张表：Python 元素 → NVCVOSDType → 命令类型 → 命令条数。答案是：BndBoxI→RECT→2 条（fill+border）；Label→TEXT→1 条背景矩形 + 1 条文本；Line→LINE→1 条旋转矩形；Circle→CIRCLE→1 条；Arrow→ARROW→1 条线 + 箭头三角形命令；PolyLine→POLYLINE→若干；Clock→CLOCK→格式化成文本后按 Label 路径走。

#### 4.4.5 小练习与答案

**练习 1**：为什么命令字节流还需要一个 `cmd_offset` 偏移表，而不是直接用定长数组？

**答案**：命令类型有 5 种、大小各异（`TextCommand`、`RectangleCommand`……字节数不同），无法用 `命令号 × 固定大小` 寻址。偏移表逐条记录起点；同时表尾追加 `batch` 段，让内核能以 `command_offsets[num_command + batch_idx]` O(1) 定位每张图的命令区间。

**练习 2**：`cuosd_draw_clock` 画时间戳时，`time=0` 这样的参数是怎么变成屏幕上的字符的？

**答案**：时钟元素先按 `clockFormat`（如 `YYMMDD_HHMMSS`）把时间值格式化成 UTF-8 字符串，然后走与 `Label` 完全相同的文本路径：切词 → 光栅化进字形位图 → 生成 TextLocation 表 → 内核按位图 alpha 混合。也就是说 Clock 不是独立内核特性，而是"文本元素 + 时间格式化器"的组合。

**练习 3**：每次 `osd` 调用结束后 CPU 侧命令表被清空，但哪些 GPU 资源**不会**被清掉？

**答案**：字形位图与文本后端（`context->text_backend`）、`gpu_commands`/`gpu_commands_offset`/`text_location` 等 `Memory<>` 缓冲（只 resize 不释放）、以及 `PolyLine`/`Segment` 元素自带的设备内存。它们驻留以待复用——所以**第二次画相同文本的成本低得多**。

### 4.5 legacy 层 GPU 侧：render_elements_kernel 与 alpha 合成

#### 4.5.1 概念说明

设备侧渲染只有一个 kernel：`render_elements_kernel`。它的设计有两个鲜明特点：

1. **每线程 2×2 像素**：一个线程负责一个 2×2 的像素块（`ix, iy` 是块左上角），四个像素的颜色先在寄存器里累积（`uchar4 context_color[4]`），最后一次性写出。这提高了访存效率（一次读写 4 个像素的连续内存），也让 MSAA 抗锯齿（对 4 个采样点各自判断命中）有自然的落点。
2. **命令遍历在像素侧**：每个线程遍历自己那张图的全部命令，靠**包围盒测试**快速跳过与己无关的命令——`if (ix+1 < cmd->bounding_left || ... ) continue`。命令数通常不大（几十条），这个 O(像素×命令) 的暴力结构在实践中足够快，且天然支持任意命令叠层。

#### 4.5.2 核心流程

```
kernel 坐标 = (blockIdx*blockDim + threadIdx) * 2 + (bx, by)     # 每 thread 2x2
batch_idx   = blockIdx.z                                          # 批维放在 grid.z
命令区间    = [command_offsets[num_command + batch_idx],
               command_offsets[num_command + batch_command + batch_idx + 1])
context_color[4] = {0}                                            # 4 像素的累积色
for 每条命令:
    包围盒不含本 2x2 块? → 跳过（文本命令还要推进行号计数器）
    switch 命令类型:
        RECTANGLE → do_rectangle（填充/边框 × 有无 MSAA）
        TEXT      → 对该行每个字形调 render_text（采样位图 alpha）
        CIRCLE    → render_circle_interpolation（半径插值抗锯齿）
        SEGMENT   → render_segment_bilinear（掩码双线性采样）
        POLYFILL  → render_polyfill（射线法奇偶填充）
4 像素 alpha 全为 0?
    是 → inplace: 返回（不写）; 否则: 从 src 原样拷贝 4 像素到 dst
    否 → BlendingPixel::call 按 RGB/RGBA 语义做 source-over 混合写出
```

kernel 变体通过分派表选择：`格式(RGB/RGBA) × 是否 MSAA` 共 4 个模板实例。

#### 4.5.3 源码精读

单像素的 alpha 混合内核原语：

[blend_single_color：osd.cu:L75-L88](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/legacy/osd.cu#L75-L88)
与 2.3 节公式逐项对应：`blend_alpha = (bg*(255-fg))>>8 + fg` 即 \(\alpha_{out}\)（`>>8` 近似除 255），随后 \(C_{out}\) 的加权平均。`u8cast`（L43-L47）做 0-255 饱和。**整条路径全用整数运算**——GPU 上移位比浮点除法便宜。

主 kernel：

[render_elements_kernel：osd.cu:L1019-L1129](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/legacy/osd.cu#L1019-L1129)
L1030-L1031 左移一位实现"每线程 2 像素"；L1032 `get_batch_idx()` 从 `blockIdx.z` 取批号；L1034-L1035 用偏移表尾部的批段定位本图命令区间；L1050-L1105 是命令主循环与类型分派；L1107-L1128 是"无覆盖像素"的直通逻辑——**inplace 直接 return（零写入），非 inplace 从 src 拷贝**，这正解释了 Limitations 中"输出与输入完全相同"的契约：OSD 的输出就是"输入 + 叠加层"，未命中像素必须原样保留。

RGB 与 RGBA 的混合差异：

[BlendingPixel RGBA 特化：osd.cu:L549-L579](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/legacy/osd.cu#L549-L579)
RGBA 时背景有自己的 alpha（`in[3]`），完整执行 source-over；RGB 时（[L581-L610](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/legacy/osd.cu#L581-L610)）背景 alpha 恒为 255，公式退化。写回时 `out[3] = blend_alpha` 会把合成后的不透明度写进 alpha 通道——这就是为什么在 RGBA 图上画半透明框后，框内像素的 alpha 值也会变化。

文本渲染的设备侧：

[render_text：osd.cu:L501-L542](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/legacy/osd.cu#L501-L542)
先判断 2×2 块是否与该字形的矩形相交；再对块内 4 个像素各自从**字形位图**取 alpha（`text_bitmap[fy * width + bfx]`，乘命令颜色 alpha 后 `>>8`），命中即混合。文本因此被统一进与矩形相同的"alpha 场"模型——**任何元素最终都归结为"给某像素一个 (r,g,b,a)"**。

抗锯齿（MSAA 4x）：

[external/internal_msaa4x：osd.cu:L614-L648](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/legacy/osd.cu#L614-L648)
在像素中心四周取 4 个 ±0.25 的采样点，各自做点在四边形内的判断，命中数决定 alpha（`a * 命中数 * 0.25`）——边缘像素得到介于 0 与 a 之间的过渡值，边缘因此平滑。`have_rotate_msaa` 作为编译期模板参数（[do_rectangle：osd.cu:L976-L1017](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/legacy/osd.cu#L976-L1017)）让无旋转场景不付采样代价。

启动配置与分派表：

[cuosd_launch_kernel_impl：osd.cu:L1147-L1185](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/legacy/osd.cu#L1147-L1185)
block 固定 `16×8` 线程；grid.x/y 覆盖 `width/2 × height/2` 个 2×2 块（inplace 时只覆盖全局包围盒，且起点 `round_down2` 对齐到偶数——因为块坐标以 2 为粒度）；grid.z 是批大小。这是 u5-l3 讲过的"grid/block 划分"的一个直接例子。

[cuosd_launch_kernel 分派表：osd.cu:L1187-L1214](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/legacy/osd.cu#L1187-L1214)
静态函数指针数组 4 项（`格式(RGB/RGBA) × MSAA(否/是)`），`index = have_rotate_msaa*2 + format-1` 选出实例——与 u5-l1 见过的"按 dtype×通道查分派表"同款手法。

aarch64/Jetson 的已知限制：

[TestOpOSD.cpp:L786-L792](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/system/TestOpOSD.cpp#L786-L792)
`OpOSD_Smoke.text_element` 测试在 `__aarch64__` 下直接 `GTEST_SKIP()`，注释说明 Jetson 平台上文本渲染存在未解决的已知问题。**在 Jetson/aarch64 上部署带 `Label`/`Clock` 的 OSD 管线时，务必先做本地验证**；矩形、线、圆等非文本元素不受此影响。

#### 4.5.4 代码实践

**实践目标**：观察元素参数变化如何反映到输出（重绘行为），并验证 alpha 语义。

**操作步骤**：

1. 基于 [osd.py](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/operators/osd.py) 写一个扩展脚本（示例代码）：

   ```python
   # 示例代码：osd_extend.py
   import cvcuda
   from samples.common import read_image, write_image  # 路径按实际环境调整

   img = read_image("input.jpg")
   h, w, c = img.shape
   nhwc = img.reshape((1, h, w, c), "NHWC")

   def make_elements(rect_color, rect_alpha):
       return cvcuda.Elements(elements=[[
           cvcuda.BndBoxI(box=(w//10, h//10, w//3, h//4), thickness=4,
                          borderColor=rect_color,
                          fillColor=(0, 128, 255, rect_alpha)),
           cvcuda.BndBoxI(box=(w//2, h//2, w//4, h//5), thickness=2,
                          borderColor=(0, 255, 0),
                          fillColor=(0, 0, 0, 0)),           # 不填充
           cvcuda.Label(utf8Text="hello osd", fontSize=24,
                        tlPos=(w//10, h//10 - 30),
                        fontColor=(255, 255, 255),
                        bgColor=(0, 0, 0, 180)),
           *[cvcuda.Point(centerPos=(w//2 + dx*20, h//4), radius=4,
                          color=(255, 0, 0)) for dx in range(-2, 3)],
       ]])

   for alpha in (0, 64, 200):
       out = cvcuda.osd(nhwc, make_elements((255, 0, 0), alpha))
       write_image(out.reshape((h, w, c), "HWC"), f"/tmp/osd_alpha{alpha}.jpg")
   ```

2. 运行后对比三张 `/tmp/osd_alpha*.jpg`。
3. 再只改 `fontSize`（24 → 48）与第二个框的 `thickness`（2 → 10），观察输出随之变化。

**需要观察的现象**：alpha=0 时第一个框只剩边框无填充；alpha 增大填充越来越实。每次 `cvcuda.osd` 都基于当次传入的 `Elements` 全新绘制——修改参数立即生效，不存在残留。

**预期结果**：输出随参数单调变化；元素对象不可变，但每次调用重建 `Elements` 即可换参数。需要 GPU 与可用字体，待本地验证。

#### 4.5.5 小练习与答案

**练习 1**：为什么 kernel 让每个线程处理 2×2 像素而不是 1×1？

**答案**：三个收益——(1) 访存合并：RGB8 下 4 个像素共 12 字节，比单像素 3 字节的读写更接近总线宽度；(2) MSAA 采样点围绕 2×2 块的中心展开，四像素共享一次包围盒测试；(3) 命令遍历的循环次数除以 4（包围盒判断对整块一次完成）。代价是边缘块要处理奇数宽高（所以有 `round_down2` 对齐与 `ix >= width-1` 的边界检查）。

**练习 2**：inplace 与非 inplace 两种模式下，未命中任何命令的像素分别发生什么？

**答案**：inplace（`in.basePtr == out.basePtr`）时直接 return——像素本来就在输出里，无需动；非 inplace 时从 src 原样拷贝到 dst（[osd.cu:L1107-L1126](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/legacy/osd.cu#L1107-L1126)）。因此 `osd_into(dst, src, ...)` 且 `dst is src`（Python 传同一张量）时最省带宽——但注意 kernel 里 inplace 判断的是**设备基址相等**，Python 侧把同一张量同时作为 src/dst 传入即可触发。

**练习 3**：`have_rotate_msaa` 是运行时 if 还是模板参数？为什么这样设计？

**答案**：模板参数（[render_elements_kernel 的 bool 形参](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/legacy/osd.cu#L1021-L1028)）。MSAA 判断在每个像素每条命令上执行，若做成运行时分支会成为热点；编译期定死后编译器可彻底消除无用分支。context 里只需一个 bool，launch 时经分派表选出对应实例。

## 5. 综合实践

**任务：给一个 batch=3 的 NHWC 批做"检测可视化"**。把本讲全部知识串起来：

1. **构造数据**：读入 3 张图（可用同一张图复制），用 u2-l3 的 `stack`（或直接 `Tensor::Create` 后逐样本写入）拼成 `(3, H, W, 3)` 的 NHWC 张量。若三张图尺寸不同，用 `padandstack` 补齐——体会为什么 OSD 只吃规则批。
2. **构造元素**：为每张图准备不同的元素组合——第 1 张：2 个不同颜色的 `BndBoxI` + 1 个 `Label`；第 2 张：5 个 `Point`（模拟关键点）+ 1 条 `PolyLine`（模拟骨架连线）；第 3 张：1 个 `Circle` + 1 个 `Clock`。全部装进一个 `cvcuda.Elements`（外层 3 个内层列表）。
3. **调用与验证**：
   - 用 `osd_into` 预分配输出，确认输出 shape/dtype 与输入一致；
   - 用 `zero_copy_split`（u1-l2 讲过）把批拆回单图保存，肉眼检查每张图只画了**自己那份**元素——这是对"列表的列表 + 按批偏移表"最直接的验证；
   - 故意把第 2 张图的内层列表留空（`[]`），确认该图原样输出（内核因 `command_begin == command_end` 跳过循环，非 inplace 时全图直通拷贝）。
4. **加分项**：把同一 `Elements` 连续调用 `osd` 两次到同一输入，确认两次输出一致（命令表每次清空、无状态残留）；再在 Nsight Systems 里对比 NHWC 与 NCHW（transpose 一份输入）两种调用的 kernel 数量，验证 planar 桥的额外开销。

本实践覆盖：元素体系（4.1）、双变体（4.2）、布局适配（4.3）、命令模型（4.4）与渲染语义（4.5）。需要 GPU 环境，无法运行的部分标注「待本地验证」。

## 6. 本讲小结

- OSD 的参数是**异构场景描述**：Python 侧 10 种元素值对象 → `NVCVElement` variant → legacy 侧 5 种命令，一条"翻译链"贯穿三层。
- 执行是**三阶段流水**：CPU 收集命令（`cuosd_draw_elements`）→ 序列化上传（`cuosd_apply`，变长命令字节流 + 按批偏移表 + 文本字形位图）→ 单 kernel 合成（`render_elements_kernel`，每线程 2×2 像素，整数 alpha 混合）。
- **所有图元归一化为"给像素一个 (r,g,b,a)"**：线是旋转矩形、文本是位图 alpha 场、圆是半径插值——内核只需一套 source-over 混合原语（`blend_single_color`）。
- planar（NCHW/CHW）支持由 priv 层的**桥**实现：reformat 转 NHWC → legacy kernel → reformat 转回，per-device workspace 复用桥张量，event 保证跨流安全；性能敏感时优先 NHWC。
- OSD 是**有状态**算子（`cuOSDContext`），但状态按调用隔离（命令表用完即清）；字形位图与设备缓冲跨调用驻留复用。
- 已知限制：输入输出必须同形同 dtype、通道 3/4、仅 U8（[OpOSD.h:L53-L99](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/OpOSD.h#L53-L99)）；aarch64/Jetson 上文本渲染有未解决问题，相关测试被跳过。

## 7. 下一步学习建议

- **下一讲（u5-l5）**将解剖另一个复杂算子家族：HQResize 与 PillowResize——它们与 OSD 相反，是"纯像素重采样"型的复杂内核，适合对照理解"参数异构型复杂"与"算法异构型复杂"两种设计。
- 若你想深挖本讲的支线：
  - 文本后端的字形缓存与位图打包：[src/cvcuda/priv/legacy/textbackend/backend.hpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/legacy/textbackend/backend.hpp) 与 `stb.cpp`；
  - `PerDeviceResource` 的多 GPU 契约：[src/cvcuda/priv/PerDeviceResource.hpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/PerDeviceResource.hpp)，u4-l3 的多 GPU 主题在此落地；
  - OSD 系统测试的参数化写法：[tests/cvcuda/system/TestOpOSD.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/system/TestOpOSD.cpp)（u7-l1 的方法在这里实战）。
- u9-l2 会回到 OSD 的应用面：把目标检测的输出框用 `cvcuda.osd` 画到帧上，替代示例自带的可视化路径——届时你已是熟悉它内部机制的读者。
