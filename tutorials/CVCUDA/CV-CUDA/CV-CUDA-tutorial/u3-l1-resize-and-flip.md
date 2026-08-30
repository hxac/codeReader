# 第一个算子组：resize 与 flip（固定批与变长批）

## 1. 本讲目标

学完本讲，你应该能够：

1. 用 Python 调用 `cvcuda.resize` 与 `cvcuda.flip`，并说清楚 `interp` 插值参数的取值与含义。
2. 对 `cvcuda.Tensor`（固定批）与 `cvcuda.ImageBatchVarShape`（变长批）两种输入分别执行同一个算子，理解两套入口在参数上的差异。
3. 对照 OpenCV 的语义理解 `flipCode`（0 / 正数 / 负数）的翻转方向约定。
4. 顺着 Python 调用往下读到 priv 实现层，亲眼看到「2x2 以下小图 + LINEAR 插值会抛异常」这条显式检查，并解释它为什么存在。

本讲是第三单元的第一讲：前两单元我们掌握了数据类型层（Tensor、变长批、DLPack 互操作），从本讲开始正式进入**算子层**。

## 2. 前置知识

本讲默认你已读过 u2-l1（张量模型）与 u2-l3（变长批处理）。这里用三段话复习够用的部分：

- **Tensor（固定批张量）**：`cvcuda.Tensor` 是位于 GPU 显存、按 strided 寻址的 N 维数组。图像通常以 HWC（单图）或 NHWC（一批同尺寸图）布局存放——`H` 是行、`W` 是列、`C` 是通道（如 RGB 三通道）。「固定批」指批内所有样本必须同宽同高，因为它们被塞进同一个规则矩形里。
- **ImageBatchVarShape（变长批）**：一个「句柄容器」，批内每张图可以有自己的宽高（甚至格式），逐图携带尺寸元数据。视频流、网页爬取图等真实数据的尺寸天然离散，变长批让它们不必先 padding 成统一尺寸。
- **插值（interpolation）**：缩放图像时，输出像素的位置通常落在输入像素的「格子之间」，需要用周围输入像素估算出这个位置的值。不同的估算方法就是不同的插值：NEAREST 取最近的整数坐标像素（快但放大后有锯齿），LINEAR 用周围 2x2 像素加权（双线性，默认），CUBIC 用周围 4x4 像素的三次函数（更平滑但更贵），AREA 用覆盖区域均值（缩小效果好）。这个「周围有多大」也正是后面 2x2 检查的来源——双线性至少需要 2x2 的输入邻域可采样。

另外一个本讲会反复出现的概念是 **allocating 与 `_into` 两种变体**（详细对比在 u3-l3）：`cvcuda.resize(src, shape)` 由库内部创建输出张量；`cvcuda.resize_into(dst, src)` 由你预先分配好输出、库只负责往里写。本讲先认识它们的形态，性能取舍留到下一讲。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `samples/operators/resize.py` | 官方 resize 样例：读图 → 缩放 → 写图，含单图与批量两段 |
| `samples/operators/flip.py` | 官方 flip 样例：一行调用展示 flipCode 用法 |
| `samples/common.py` | 样例公共工具：`read_image` 返回 RGB8 HWC 张量，`write_image` 编码落盘 |
| `python/mod_cvcuda/operators/OpResize.cpp` | resize 的 pybind11 绑定：4 个函数（Tensor/变长批 × allocating/_into） |
| `python/mod_cvcuda/operators/OpFlip.cpp` | flip 的 pybind11 绑定：同样是 4 个函数 |
| `python/mod_cvcuda/operators/VarShapeUtils.hpp` | 绑定层共用工具：为变长批分配输出容器 |
| `python/mod_cvcuda/InterpolationType.cpp` | 把 C 枚举 `NVCVInterpolationType` 导出为 Python 的 `cvcuda.Interp` |
| `src/cvcuda/include/cvcuda/Types.h` | C 侧插值枚举定义 |
| `src/cvcuda/include/cvcuda/OpResize.h` | resize 的 C API 头文件，内含 Limitations 支持契约表 |
| `src/cvcuda/priv/OpResize.cpp` | resize 的 priv 实现：参数检查、分发到 kernel |
| `src/cvcuda/priv/OpResize.cu` | resize 的原生 CUDA kernel（Tensor 路径） |

## 4. 核心概念与源码讲解

### 4.1 模块一：从官方样例看算子的 Python 调用

#### 4.1.1 概念说明

CV-CUDA 的每个算子在上层看来就是一个 Python 函数：输入张量（或变长批）进，输出张量（或变长批）出，所有像素计算发生在 GPU 上。官方在 `samples/operators/` 下为每个算子准备了一个最小可运行样例，这是学习任何算子的第一入口——先跑通样例，再改参数观察行为，最后钻进绑定源码。

#### 4.1.2 核心流程

`resize.py` 的执行流程：

1. `parse_image_args` 解析命令行，默认输入是仓库自带的 `tabby_tiger_cat.jpg`。
2. `read_image` 用 nvimgcodec 解码，经 `as_tensor` 零拷贝包装成 **RGB8、HWC 布局**的 `cvcuda.Tensor`。
3. 调 `cvcuda.resize(input_image, (args.height, args.width, 3))`——注意输出形状是**三元组**，因为输入是 HWC 三维。
4. `write_image` 编码写盘。
5. 第二段演示批处理：`reshape((1, *input_image.shape), "NHWC")` 把 HWC 升维成 NHWC，输出形状相应变成四元组 `(1, H, W, 3)`。

#### 4.1.3 源码精读

样例主体（含官方教程注释标签 `docs_tag`，文档系统会抽取这些片段）：

[samples/operators/resize.py:39-55](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/operators/resize.py#L39-L55)

这段代码做了两次 resize：第 44 行是单图版（HWC → HWC），第 50-53 行先 `reshape` 出批维度再缩放（NHWC → NHWC）。`reshape` 只改形状描述与布局标签，`assert` 验证输出形状符合预期——**输出形状完全由你传入的 shape 参数决定**，库不会自作主张。

`read_image` 的契约值得单独记住：

[samples/common.py:355-374](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/common.py#L355-L374)

nvimgcodec 默认解码为交错 RGB，所以 `read_image` 返回的永远是 **RGB8 的 HWC 张量**。样例注释里「图像按 HWC 读入，输出 shape 必须保持同样维数」正来源于此。

flip 的样例则只有一行核心调用：

[samples/operators/flip.py:40-43](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/operators/flip.py#L40-L43)

`flipCode=1` 左右镜像；同注释还写明了 0 与 -1 的含义（详见 4.3）。

#### 4.1.4 代码实践

1. **实践目标**：跑通官方样例，确认输入输出形状与布局。
2. **操作步骤**（需已按 u1-l2 安装 cvcuda wheel 与样例依赖，GPU 环境待本地验证）：
   ```bash
   cd samples
   python3 operators/resize.py --output /tmp/cat_512x512.jpg --width 512 --height 512
   ```
   再在 Python 里检查张量属性（示例代码）：
   ```python
   import sys; sys.path.append("samples")
   from common import read_image
   t = read_image("samples/assets/images/tabby_tiger_cat.jpg")
   print(t.shape, t.dtype, t.layout)   # 期望: (H, W, 3) uint8 HWC
   ```
3. **需要观察的现象**：输出的 JPEG 分辨率变成 512x512；打印出的 shape 是三元组，layout 为 HWC。
4. **预期结果**：`cvcuda.resize` 的输出 shape 等于你传入的 shape 参数；HWC 进则 HWC 出。
5. 若本机没有 GPU / 未装样例依赖，以上为「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `resize.py` 第 44 行传的 shape 是 `(H, W, 3)` 三元组，而第 52 行是 `(1, H, W, 3)` 四元组？
答案：输出 shape 的维数必须与输入张量的秩一致（priv 层会检查输入输出布局相同）。`read_image` 返回 HWC 三维张量；`reshape` 升维成 NHWC 四维后，输出也要是四维。

**练习 2**：如果不传 `--width/--height`，样例会按什么尺寸输出？
答案：按 `parse_image_args` 的默认参数。可用 `python3 operators/resize.py --help` 查看默认值（帮助文本中会列出，具体数值待本地确认）。

**练习 3**：`read_image` 为什么能保证返回的是 HWC 而不是 CHW？
答案：nvimgcodec 解码默认输出交错（interleaved）RGB 像素，`as_tensor(nvc_img, "HWC")` 显式带上 HWC 布局标签做零拷贝包装（见 `samples/common.py:372-374`）。

### 4.2 模块二：Python 绑定层——每个算子的「四连函数」

#### 4.2.1 概念说明

打开 `python/mod_cvcuda/operators/` 下任何一个 `OpXxx.cpp`，你都会看到几乎相同的骨架：一个匿名命名空间里的 4 个 C++ 函数 + 一个 `ExportOpXxx` 导出函数。这是本仓库 Python 绑定的「标准四件套」：

| 函数 | 输入类型 | 变体 |
|------|----------|------|
| `Xxx` | `Tensor` | allocating（库建输出） |
| `XxxInto` | `Tensor` | `_into`（你给输出） |
| `XxxVarShape` | `ImageBatchVarShape` | allocating |
| `XxxVarShapeInto` | `ImageBatchVarShape` | `_into` |

绑定层用 pybind11 把这些 C++ 函数注册成 Python 的 `cvcuda.xxx` / `cvcuda.xxx_into`。由于 `resize` 这个名字被注册了两次（Tensor 版与变长批版），pybind11 在调用时按**参数类型自动重载决议**：传 `Tensor` 走前者，传 `ImageBatchVarShape` 走后者。

#### 4.2.2 核心流程

以 Tensor 版 `resize` 为例，一次调用的内部路径：

```
cvcuda.resize(src, shape, interp, stream)
  ├─ stream 未传?  → pstream = Stream::Current()     # 提交到当前流
  ├─ Tensor::Create(shape, src.dtype, src.layout)    # allocating 变体：建输出（进对象缓存）
  └─ ResizeInto(output, input, interp, pstream)
       ├─ CreateOperator<cvcuda::Resize>()           # 拿 C++ 算子实例
       ├─ ResourceGuard guard(stream)                # 登记输入(读锁)/输出(写锁)
       └─ guard.run(...) → resize->submit(stream.cudaHandle(), in, out, interp)
                                        ↓                    # 进入 src/cvcuda priv 层
```

`ResourceGuard` 是多线程安全的读写锁登记（u4-l3 深入）；`NvtxTrace` 给函数自动加性能分析埋点（u7-l4 深入）。本讲只需认出这两个「标配包装」。

#### 4.2.3 源码精读

Tensor 版的 allocating 与 `_into` 实现：

[python/mod_cvcuda/operators/OpResize.cpp:42-67](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpResize.cpp#L42-L67)

要点：
- 第 44-47 行：`stream` 参数缺省时取 `Stream::Current()`——这就是「不传流就提交到当前流」的出处。
- 第 51-57 行：`ResourceGuard` 登记输入为读锁、输出为写锁、算子本身不加锁，然后 `guard.run` 里调用 `resize->submit(...)`，把 cudaStream_t 一路传下去。
- 第 64 行：allocating 变体用 `Tensor::Create(out_shape, input.dtype(), input.shape().layout())` 建输出——**dtype 与 layout 都继承自输入**，只有形状由你指定。

变长批版的 allocating 有一个专属检查：

[python/mod_cvcuda/operators/OpResize.cpp:90-101](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpResize.cpp#L90-L101)

变长批的 resize 第二参数不再是单个 shape，而是 **sizes 列表（每张图一个 (h, w) 元组）**——因为批内每张图的输出尺寸可以不同。第 93-96 行检查列表长度必须等于批内图像数，随后 `CreateSizedImageBatch` 逐图分配输出（[python/mod_cvcuda/operators/VarShapeUtils.hpp:65-83](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/VarShapeUtils.hpp#L65-L83)），每张输出图继承对应输入图的格式。

pybind11 导出：两个同名 `resize` 各自注册一次：

[python/mod_cvcuda/operators/OpResize.cpp:109-124](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpResize.cpp#L109-L124)

注意默认值都写在这里：`"interp"_a = NVCV_INTERP_LINEAR`（默认线性插值）、`py::kw_only()` 之后的 `"stream"_a = nullptr`（stream 只能按关键字传）。变长批版的导出在 [python/mod_cvcuda/operators/OpResize.cpp:141-155](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpResize.cpp#L141-L155)，docstring 明确写着第二参数叫 `sizes`、类型是「Tuple vector」。

flip 的四件套结构与 resize 完全同构，但有一个**关键差异**——变长批版的 `flipCode` 不是标量而是 `Tensor`：

[python/mod_cvcuda/operators/OpFlip.cpp:62-88](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpFlip.cpp#L62-L88)

Tensor 版的 `Flip`（第 55-60 行）中 `flipCode` 是 `int32_t` 标量、整批同方向；而 `FlipVarShape`/`FlipVarShapeInto`（第 62-88 行）的 `flipCode` 是一个 `Tensor`——**批内每张图可以各自指定翻转方向**。输出则由 `CreateSameShapeImageBatch(input)` 建立：flip 不改变尺寸，输出容器与输入逐图同规格（[python/mod_cvcuda/operators/VarShapeUtils.hpp:29-63](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/VarShapeUtils.hpp#L29-L63)）。

这体现了变长批入口的一般设计：**凡是可以逐图变化的参数，在变长批入口都升级为「每图一个元素的张量」**。后面学 `OpReformat`、`OpNormalize` 等算子时会反复看到这个模式。

#### 4.2.4 代码实践

1. **实践目标**：验证绑定层注册的签名与 docstring 与源码一一对应。
2. **操作步骤**（示例代码，无需 GPU）：
   ```bash
   python3 -c "import cvcuda; help(cvcuda.resize); help(cvcuda.flip)"
   ```
3. **需要观察的现象**：help 输出中 `resize` 有两个重载（`src: cvcuda.Tensor` 与 `src: cvcuda.ImageBatchVarShape`），参数表 `src, shape/sizes, interp=cvcuda.Interp.LINEAR, *, stream=None`；`flip` 同样有两个重载，且变长批版的 `flipCode` 类型是 `cvcuda.Tensor`。
4. **预期结果**：help 文本与 `OpResize.cpp`/`OpFlip.cpp` 中 `m.def(...)` 的 `"src"_a`、docstring 完全一致——pybind11 的 docstring 就是 C++ 源码里 `R"pbdoc(...)pbdoc"` 的原文。
5. 若未安装 cvcuda，此实践「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`cvcuda.resize` 与 `cvcuda.resize_into` 各自的输出从哪里来？
答案：`resize` 在绑定层内部 `Tensor::Create(out_shape, input.dtype(), input.layout)` 新建输出（并进入 Python 对象缓存，见 u4-l2）；`resize_into` 直接使用调用者传入的 `dst`，写完原样返回。

**练习 2**：为什么变长批 resize 要求 `len(sizes) == numImages`，而 Tensor 版没有这个检查？
答案：变长批的 sizes 是「每张图一个输出尺寸」，长度不等就无法一一对应（[OpResize.cpp:93-96](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpResize.cpp#L93-L96) 会抛 `ResizeError`）；Tensor 版整批共享同一个 shape，不存在对应问题。

**练习 3**：同一个 `m.def("resize", ...)` 名字注册两次，Python 怎么知道该走哪个 C++ 函数？
答案：pybind11 的重载决议按实参类型匹配：第一参数是 `cvcuda.Tensor` 走 Tensor 版，是 `cvcuda.ImageBatchVarShape` 走变长批版（第二参数 shape 元组 / sizes 列表也随之区分）。

### 4.3 模块三：参数详解——Interp 插值枚举与 flipCode

#### 4.3.1 概念说明

`interp` 与 `flipCode` 是本算子组两个「有语义」的参数：前者从 C 枚举 `NVCVInterpolationType` 映射成 Python 枚举 `cvcuda.Interp`，后者沿用 OpenCV `cv2.flip` 的整数约定。搞清这两个参数，等于搞清了 CV-CUDA 参数体系的两个典型样例：**枚举型**与 **OpenCV 兼容型**。

#### 4.3.2 核心流程

插值的坐标映射：输出像素 \((x_{dst}, y_{dst})\) 反算回输入坐标

\[
x_{src} = x_{dst} \cdot \frac{W_{src}}{W_{dst}}, \qquad y_{src} = y_{dst} \cdot \frac{H_{src}}{H_{dst}}
\]

NEAREST 直接取 \((\lfloor x_{src} \rceil, \lfloor y_{src} \rceil)\) 处的像素；LINEAR 在 \(x_{src}, y_{src}\) 周围的 2x2 邻域做双线性加权：

\[
f(x, y) \approx \sum_{i \in \{0,1\}} \sum_{j \in \{0,1\}} w_x(i)\, w_y(j)\, f(\lfloor x \rfloor + i,\ \lfloor y \rfloor + j)
\]

CUBIC 则用 4x4 邻域。注意：**LINEAR 的采样邻域恰好是 2x2**——这就是后文「源图至少 2x2」检查的数学根源：一张 1x1 的图没有 2x2 邻域可采，双线性插值无定义。

#### 4.3.3 源码精读

C 侧枚举（每个算子 C 头都会引用它）：

[src/cvcuda/include/cvcuda/Types.h:39-52](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/Types.h#L39-L52)

共 10 个值：NEAREST/LINEAR/CUBIC/AREA/LANCZOS/GAUSSIAN/HAMMING/BOX 是插值方法，`NVCV_WARP_INVERSE_MAP` 是 warp 类算子的标志位（可与其他值按位或，所以枚举值故意跳到 16），`NVCV_INTERP_MAX=7` 是哨兵。**这是全库共享的一个枚举**——不同算子只支持其中子集（例如 resize 的 Tensor kernel 只处理 NEAREST/LINEAR/CUBIC/AREA 四种，见 4.4；PillowResize 支持 LINEAR/CUBIC/LANCZOS/BOX/HAMMING）。

Python 侧导出：

[python/mod_cvcuda/InterpolationType.cpp:24-37](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/InterpolationType.cpp#L24-L37)

`py::enum_<NVCVInterpolationType>(m, "Interp", ...)` 把 C 枚举原样搬成 `cvcuda.Interp`，所以你在 Python 里写 `cvcuda.Interp.CUBIC`，传到底层就是 `NVCV_INTERP_CUBIC = 2`。第 36 行还定义了 `__or__`，让 `Interp.LINEAR | Interp.WARP_INVERSE_MAP` 这类组合标志可用。

flipCode 的语义写在 flip 的 docstring 里（与 OpenCV 完全一致）：

[python/mod_cvcuda/operators/OpFlip.cpp:102-105](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpFlip.cpp#L102-L105)

- `0`：绕 x 轴翻转 = 上下颠倒（垂直镜像）；
- `正数`（如 `1`）：绕 y 轴翻转 = 左右镜像；
- `负数`（如 `-1`）：两轴同时翻转 = 旋转 180°。

对照 OpenCV：`cv2.flip(img, flipCode)` 的约定一字不差，从 OpenCV 迁移代码时 `flipCode` 可以原样照抄。

#### 4.3.4 代码实践

1. **实践目标**：直观感受不同插值的画质差异。
2. **操作步骤**（示例代码，需 GPU，待本地验证）：
   ```python
   import sys; sys.path.append("samples")
   import cvcuda
   from common import read_image, write_image

   img = read_image("samples/assets/images/tabby_tiger_cat.jpg")
   h, w = img.shape[0] * 4, img.shape[1] * 4      # 放大 4 倍让差异肉眼可见
   for name in ("NEAREST", "LINEAR", "CUBIC"):
       out = cvcuda.resize(img, (h, w, 3), getattr(cvcuda.Interp, name))
       write_image(out, f"/tmp/cat_{name}.jpg")
   ```
3. **需要观察的现象**：NEAREST 输出有明显块状锯齿；LINEAR 平滑；CUBIC 更平滑（边缘过渡更「锐」一些）。
4. **预期结果**：三张图的平滑度按 NEAREST < LINEAR < CUBIC 递增；这与插值邻域 1x1 / 2x2 / 4x4 的大小一致。
5. 运行结果与具体图像相关，标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`cvcuda.Interp.CUBIC` 传到 CUDA kernel 时是什么值？
答案：`NVCV_INTERP_CUBIC = 2`（[Types.h:44](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/Types.h#L44)），Python 枚举与 C 枚举同值同序。

**练习 2**：`flipCode=2` 与 `flipCode=1` 的翻转结果相同吗？
答案：相同。语义只区分「0 / 正 / 负」三类（OpFlip.cpp docstring：0 绕 x 轴、正数绕 y 轴、负数双轴），任何正数都等价于左右镜像。

**练习 3**：为什么 `NVCV_WARP_INVERSE_MAP` 的值是 16 而不是紧挨着排？
答案：它是可与其他插值标志按位组合的位标志（这也是 `InterpolationType.cpp:36` 定义 `__or__` 的原因），留出低位给插值方法、高位给标志位，按位或后仍能区分。

### 4.4 模块四：priv 实现层——显式检查、支持契约与 2x2 异常

#### 4.4.1 概念说明

绑定层把参数送进 `src/cvcuda/priv/` 后，算子实现做的第一件事不是启动 kernel，而是**校验**：输入输出是否 CUDA 可访问、dtype/layout 是否匹配、通道数是否在 1-4、尺寸是否合法。这些检查是理解算子「会在什么情况下报错」的权威来源。每个算子的 C 头文件还附带一张 **Limitations 契约表**，用注释形式声明支持矩阵——这是使用任何算子前都该先读的「说明书」。

#### 4.4.2 核心流程

resize 在 priv 层有两条路径（这个分流在 u5-l1 会被完整解剖，这里只看检查逻辑）：

- **Tensor 路径**：`operator()(stream, in, out, interp)` → `exportData<TensorDataStridedCuda>()` → `RunResize(...)`（原生 kernel，实现在 `OpResize.cu`）。
- **变长批路径**：`operator()(stream, in, out, interp)` → **LINEAR 时逐图检查源尺寸 ≥ 2x2** → `exportData<ImageBatchVarShapeDataStridedCuda>()` → 按整批缩放比例分类快速路径 → 交给 legacy kernel `ResizeVarShape::infer(...)`。

#### 4.4.3 源码精读

先看 C 头文件的契约表（每个算子都有，位于 Submit 函数的注释里）：

[src/cvcuda/include/cvcuda/OpResize.h:57-100](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/OpResize.h#L57-L100)

契约明确写着：输入输出支持 `[kNHWC, kHWC, kNCHW, kCHW]` 四种布局、通道 `[1, 3, 4]`、dtype 仅 U8/U16/S16/F32；输入输出之间 **dtype/layout/数量/通道必须相同，宽高可以不同**——这正好解释了绑定层 `Tensor::Create(out_shape, input.dtype(), input.shape().layout())` 为什么继承 dtype 与 layout。

变长批路径的 2x2 显式检查（本讲实践任务的目标代码）：

[src/cvcuda/priv/OpResize.cpp:63-78](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpResize.cpp#L63-L78)

第 67 行 `if (interpolation == NVCV_INTERP_LINEAR)` 说明**该检查只针对 LINEAR**：遍历批内每张源图，任一张宽或高小于 2 就抛 `nvcv::Exception(ERROR_INVALID_ARGUMENT, "Linear interpolation requires source dimensions of at least 2x2")`。理由即 4.3.2 的数学：双线性需要 2x2 采样邻域，1 像素宽/高的图无法提供；而 NEAREST 无此需求，不在检查之列。

Tensor 路径在 `RunResize` 里有同款检查：

[src/cvcuda/priv/OpResize.cu:1645-1649](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpResize.cu#L1645-L1649)

在它之前还有一串校验（[src/cvcuda/priv/OpResize.cu:1596-1626](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpResize.cu#L1596-L1626)）：输入输出 dtype 必须相同、layout 必须相同、必须是 (N)HWC 或 (N)CHW、样本数与通道数必须一致、通道数必须在 1-4——**实现代码与 OpResize.h 的契约表逐条对应**。校验通过后，`RunResize` 调用 `RunResizeInterpType` 按 dtype 查表（[src/cvcuda/priv/OpResize.cu:1552-1575](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpResize.cu#L1552-L1575)，宏展开成 U8/U16/S16/F32 及其 1/3/4 通道组合的 if-else 链），再进入 `RunResizeInterp`，其中的 `switch(interpolation)` 只覆盖 NEAREST/LINEAR/CUBIC/AREA 四个 case（[src/cvcuda/priv/OpResize.cu:1374](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpResize.cu#L1374)、[L1381](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpResize.cu#L1381)、[L1400](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpResize.cu#L1400)、[L1485](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpResize.cu#L1485)）——dtype 分发表与 interp switch 共同构成了「resize 实际支持哪些组合」的源码依据。

变长批路径在检查之后还有一段有趣的快速路径分类：

[src/cvcuda/priv/OpResize.cpp:92-126](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpResize.cpp#L92-L126)

它遍历批内每对 (源尺寸, 目标尺寸)，判断整批是否恰好全部是「精确 2 倍放大」「精确 2 倍缩小」「非整数比例缩小」，据此选择 `ResizeVarShapeScale` 枚举（kExpand2x / kContract2x / kFractionalZoomOut / kGeneric，定义在 [src/cvcuda/priv/legacy/CvCudaLegacy.h:1112-1118](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/legacy/CvCudaLegacy.h#L1112-L1118)），最终调用 legacy kernel（第 126 行）。92-95 行的注释解释了为什么用批句柄逐图查询：导出的 `imageList` 在设备内存，逐图尺寸只有主机侧可读。这是「常见缩放比例走特化 kernel」的优化套路，细节留到 u5。

最后是 Tensor 路径的入口（展示 exportData 检查模式）：

[src/cvcuda/priv/OpResize.cpp:42-61](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpResize.cpp#L42-L61)

`exportData<TensorDataStridedCuda>()` 失败（返回 nullptr）说明张量不是 CUDA 可访问的 pitch-linear 数据，立即抛异常——这就是「CPU 张量喂给算子」时报错的源头。u5-l2 会专门讲 exportData 体系。

#### 4.4.4 代码实践

1. **实践目标**：亲眼验证 2x2 检查的存在与豁免条件。
2. **操作步骤**（示例代码，需 GPU，待本地验证）：
   ```python
   import cvcuda
   import numpy as np
   import torch  # 用 cupy/torch 生成 GPU buffer 均可

   tiny = torch.zeros((1, 1, 3), dtype=torch.uint8, device="cuda")
   src  = cvcuda.as_tensor(tiny, "HWC")        # 1x1 的三通道小图

   batch = cvcuda.ImageBatchVarShape(1)
   batch.pushback(cvcuda.Image(cvcuda.Format.RGB8, (1, 1)))   # 占位：需用真实数据填充，见下方说明

   # 更简单的构造路径：用 as_image / Image.wrapdata 包装 src
   img   = src.as_image(cvcuda.Format.RGB8)
   batch = cvcuda.ImageBatchVarShape(1)
   batch.pushback(img)

   try:
       out = cvcuda.resize(batch, [(8, 8)], cvcuda.Interp.LINEAR)
       print("LINEAR 通过")
   except Exception as e:
       print("LINEAR 异常:", e)

   out = cvcuda.resize(batch, [(8, 8)], cvcuda.Interp.NEAREST)
   print("NEAREST 通过，输出尺寸:", out[0].size)
   ```
   （构造变长批的具体 API 以 `samples/datatypes/imagebatchvarshape.py` 为准；上面 `pushback` 的拼写与用法待本地验证。）
3. **需要观察的现象**：LINEAR 分支捕获到异常，消息包含 `Linear interpolation requires source dimensions of at least 2x2`；NEAREST 分支正常返回。
4. **预期结果**：与 [OpResize.cpp:67-78](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpResize.cpp#L67-L78) 的源码一致——检查仅针对 LINEAR。把小图换成 2x2 后，LINEAR 也应通过。异常在 Python 侧的具体类型（nvcv 异常类）待本地验证。
5. 同样的实验对 Tensor 输入也成立（检查点在 [OpResize.cu:1645-1649](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpResize.cu#L1645-L1649)）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 AREA 插值没有触发 2x2 检查？（从语义上想）
答案：检查只写在 `interpolation == NVCV_INTERP_LINEAR` 分支内（[OpResize.cpp:67](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpResize.cpp#L67)）。AREA 表示「目标像素覆盖源区域的均值」，缩小是主流场景，源小于 2x2 时语义仍可定义（大不了就是取那一个像素），因此实现未拦截；NEAREST 同理。

**练习 2**：把 F32 的 NHWC 张量 resize 成 NCHW 输出，会在哪一行报什么错？
答案：在 [OpResize.cu:1600-1603](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpResize.cu#L1600-L1603) 抛 `ERROR_INVALID_ARGUMENT, "Input and output data layout are different"`——layout 必须一致（契约表中 Input/Output dependency 的 Data Layout = Yes）。

**练习 3**：一个 2 通道（如 RG）图像能被 resize 吗？
答案：不能。契约表规定通道 `[1, 3, 4]`（[OpResize.h:61](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/OpResize.h#L61)），实现在 [OpResize.cu:1623-1626](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpResize.cu#L1623-L1626) 拒绝 1-4 之外的通道数。

## 5. 综合实践

把本讲四个模块串成一个任务：**同一输入、两种插值、两种容器**。

**任务 A（插值对比）**：修改 `samples/operators/resize.py`，对同一输入分别用 LINEAR 与 CUBIC 缩放到同一尺寸（如 256x256），保存两张对比图：

```python
# 示例代码：在 resize.py 的 main() 中替换第 44-47 行
out_linear = cvcuda.resize(input_image, (256, 256, 3), cvcuda.Interp.LINEAR)
write_image(out_linear, "/tmp/cat_linear.jpg")
out_cubic = cvcuda.resize(input_image, (256, 256, 3), cvcuda.Interp.CUBIC)
write_image(out_cubic, "/tmp/cat_cubic.jpg")
```

用放大镜工具（或再把输出 resize 放大 4 倍）观察边缘平滑度差异；预期 CUBIC 边缘过渡更平滑（具体观感待本地验证）。

**任务 B（变长批 + 异常观察）**：构造一个含 1x1 小图的 `ImageBatchVarShape`，先按 4.4.4 的示例用 LINEAR 触发异常，再换 NEAREST 验证通过；然后用 2x2 的图重试 LINEAR，确认也通过。把三次结果记入表格：

| 源图尺寸 | interp | 结果 |
|----------|--------|------|
| 1x1 | LINEAR | 异常：Linear interpolation requires source dimensions of at least 2x2 |
| 1x1 | NEAREST | 正常 |
| 2x2 | LINEAR | 正常 |

**任务 C（flip 三方向，选做）**：对同一张图分别用 `flipCode=0 / 1 / -1` 各输出一张，肉眼验证与 OpenCV 约定一致（0 上下翻、1 左右翻、-1 旋转 180°）。

完成后你应能回答：这条异常是**绑定层**还是 **priv 层**抛的？（priv 层，[OpResize.cpp:74-76](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpResize.cpp#L74-L76)；绑定层的变长批检查只管 sizes 数量。）

## 6. 本讲小结

- CV-CUDA 每个 Python 算子在绑定层是「四连函数」：Tensor/变长批 × allocating/`_into`，同名注册、按参数类型重载决议。
- `cvcuda.resize(src, shape)` 的输出 shape 完全由你指定，但 dtype 与 layout 继承输入（priv 层强制二者一致）；变长批版的第二参数是逐图 sizes 列表，长度必须等于批内图像数。
- 变长批入口中，可逐图变化的参数升级为张量——flip 的 `flipCode` 在 Tensor 入口是 int，在变长批入口是每图一值的 `Tensor`。
- `interp` 来自全库共享枚举 `cvcuda.Interp`（C 侧 `NVCVInterpolationType`）；resize 的 Tensor kernel 实际支持 NEAREST/LINEAR/CUBIC/AREA 四种，默认 LINEAR。
- `flipCode` 与 OpenCV `cv2.flip` 完全兼容：0 上下翻、正数左右翻、负数双轴。
- priv 层先校验后执行：CUDA 可访问性、dtype/layout 一致、通道 1-4 等检查与 C 头文件的 Limitations 契约表逐条对应；LINEAR 插值额外要求源图至少 2x2（双线性需要 2x2 采样邻域）。

## 7. 下一步学习建议

- **u3-l2（色彩与像素值算子族）**：继续按族认识算子，并学会查阅每个算子 C 头文件中的 Limitations 契约表——本讲 4.4.3 已经预演了 resize 的契约表读法。
- **u3-l3（allocating 与 `_into`）**：本讲只认识了两种变体的形态，下一讲用计时实验量化它们的性能与显存差异。
- 预习时可以打开 [python/mod_cvcuda/operators/OpCvtColor.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpCvtColor.cpp) 自行验证：它是否也是「四连函数」骨架？它的可变参数（颜色转换码）在变长批入口是什么形态？这是检验你是否掌握了本讲模式的好测验。
