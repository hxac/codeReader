# 端到端应用一:图像分类管线

## 1. 本讲目标

学完本讲,你应该能够:

1. 画出 `classification.py` 的完整数据流:解码 → 加批维 → 预处理五连 → TensorRT 推理 → Top-5 后处理,并说出每一步数据的形状、dtype 与布局。
2. 讲清 `samples/common.py` 中每个工具函数在管线里的角色,尤其是 `read_image` 的零拷贝纳管、`TRT` 类如何用裸设备指针把 `cvcuda.Tensor` 喂给 TensorRT。
3. 识别示例中的零拷贝技巧(`as_tensor` 纳管、`__cuda_array_interface__` 裸指针直传、`zero_copy_split` 切批)与分批技巧(`stack` 的两种用法)。
4. 把示例的预处理部分(resize + convertto + reformat)替换为 u3-l4 学过的融合算子 `resize_crop_convert_reformat`,并解释两版在内核数、吞吐与数值精度上的差异来源。
5. 把示例改造成自己的数据源(替换输入图片、修改目标尺寸、扩展到多图)。

## 2. 前置知识

本讲是"端到端应用"单元的第一讲,不再引入新的 CV-CUDA 机制,而是把前面八个单元的知识串成一条真实的推理管线。你需要先具备以下概念(均已在前文讲过,这里只做一句话唤醒):

- **张量与布局**(u2-l1):`cvcuda.Tensor` 是 GPU 显存中的 N 维数组,图像通常用 HWC(交错)或 NHWC 布局;stride 以字节计,行距可能被对齐填充。
- **as_tensor 零拷贝纳管**(u2-l4):任何提供 `__cuda_array_interface__`(CAI)的对象可以不拷贝地包装成 `cvcuda.Tensor`。
- **allocating 变体**(u3-l3):`cvcuda.resize(src, ...)` 会查对象缓存并返回新张量;`_into` 变体写入预分配的 `dst`。
- **融合算子**(u3-l4):`resize_crop_convert_reformat` 把缩放、裁剪、缩放偏移、通道重排、类型转换、布局重排折进单个 CUDA kernel,中间结果只存于寄存器。
- **流模型**(u4-l1):一切算子异步提交到流上;`cvcuda.Stream.current` 取当前流,`sync()` 等待完成。

本讲还会碰到三个**管线生态**概念,属于 CV-CUDA 之外但同样重要的伙伴库,初次接触的读者看这里:

- **ONNX**:开放的神经网络交换格式。PyTorch 模型导出为 ONNX 后,才能被 TensorRT 解析。
- **TensorRT**:NVIDIA 的高性能推理引擎。它把 ONNX 图编译成针对当前 GPU 优化的"引擎"(engine);推理时通过 `execute_async_v3` 在指定 CUDA 流上执行,输入输出用裸设备指针绑定。
- **nvimgcodec**:NVIDIA 的图像编解码库,能直接把 JPEG/PNG **解码到显存**,也能把显存中的图像编码写回磁盘——这是"像素不过 CPU"的第一环。

一个提醒:示例用 torchvision 的 **ResNet50**(ImageNet 预训练权重)做分类,1000 类输出。你不需要懂 ResNet 的内部结构,只需知道它吃 `(N, 3, 224, 224)` 的 NCHW float32 张量、吐 `(N, 1000)` 的 logits——这正是预处理五连要"喂"出的形状。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [samples/applications/classification.py](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/applications/classification.py) | 本讲主线:单图分类的完整管线脚本(仅 135 行) |
| [samples/common.py](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/common.py) | 全部 sample 共享的工具层:编解码、显存拷贝、ONNX 导出、TRT 包装类 |
| [samples/applications/hello_world.py](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/applications/hello_world.py) | 对照样本:多图解码、`stack` 合批、`zero_copy_split` 切批的示范 |
| [samples/operators/resize_crop_convert_reformat.py](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/operators/resize_crop_convert_reformat.py) | 综合实践的参照:融合算子的标准调用方式 |
| [src/cvcuda/include/cvcuda/OpResizeCropConvertReformat.h](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/OpResizeCropConvertReformat.h) | 融合算子的 Limitations 契约表(综合实践的权威依据) |
| [src/cvcuda/include/cvcuda/OpNormalize.h](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/OpNormalize.h) | normalize 的公式与参数张量形状契约 |
| [python/mod_cvcuda/operators/OpResizeCropConvertReformat.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpResizeCropConvertReformat.cpp) | 融合算子 Python 绑定的参数默认值 |
| [samples/README.md](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/README.md) | 安装与运行说明 |

## 4. 核心概念与源码讲解

本讲按数据流向拆成五个模块:管线总览 → 解码纳管 → 预处理五连 → 推理桥接 → 分批技巧。

### 4.1 管线总览:一条全程留在 GPU 上的数据流

#### 4.1.1 概念说明

传统 CPU 视觉管线的痛苦在于**搬运**:GPU 解码 → 拷回 CPU → CPU 缩放/归一化 → 再拷上 GPU 推理。每次跨 PCIe 往返都是延迟,批越大越痛。

`classification.py` 展示的是另一种形态:**像素从解码进入显存后,直到 logits 拷回 CPU 打印,中间绝不离开 GPU**。CPU 只经手压缩字节流(JPEG 文件)和最终 1000 个浮点数。

先交代一个如实的观察:大纲里"多图解码、变长批"的愿景,在**当前版本**的 `classification.py` 中是"单图 + 批维为 1"——它用 `cvcuda.stack([input_image])` 给单张图加批维,推理引擎也是按固定 batch=1 导出的。真正的多图合批/切批技巧由 `hello_world.py` 示范,本讲在 4.5 模块对照讲解。读懂单图版是改造多图版的前提。

#### 4.1.2 核心流程

```text
JPEG 文件 (CPU 磁盘)
   │  nvimgcodec.Decoder 直接解码到显存
   ▼
HWC U8 Tensor            ← read_image,零拷贝纳管
   │  cvcuda.stack([img])              加批维
   ▼
NHWC U8 (1,H,W,3)
   │  cvcuda.resize(LINEAR)            缩放
   ▼
NHWC U8 (1,224,224,3)
   │  cvcuda.convertto(F32, 1/255)     转浮点并压到 [0,1]
   ▼
NHWC F32
   │  cvcuda.normalize(mean, std)      ImageNet 标准化
   ▼
NHWC F32
   │  cvcuda.reformat("NCHW")          布局重排
   ▼
NCHW F32 (1,3,224,224)   ← 模型要的形状
   │  TRT: set_tensor_address + execute_async_v3 + sync
   ▼
NCHW F32 (1,1000)        ← logits(已含 softmax)
   │  cuda_memcpy_d2h
   ▼
numpy (1,1000) → argsort → Top-5 打印
```

整条链只有两处跨越 CPU/GPU 边界:读入的 JPEG 字节流与取回的 1000 个浮点数。

#### 4.1.3 源码精读

主函数按 `docs_tag` 注释分成六段,这是官方文档抽取用的锚点,也是我们阅读的天然目录:

[samples/applications/classification.py:42-45](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/applications/classification.py#L42-L45) — 入口:解析命令行参数(`--input`/`--output`/`--width`/`--height`,默认 224×224、内置的 tabby_tiger_cat.jpg)。

[samples/applications/classification.py:48-64](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/applications/classification.py#L48-L64) — 模型准备:首次运行时下载 ResNet50 权重导出 ONNX,再编译 TensorRT 引擎;两者都缓存在 `.cache` 目录,二次运行直接加载。

[samples/applications/classification.py:69](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/applications/classification.py#L69) — 解码:`read_image` 返回 RGB8 HWC 张量。

[samples/applications/classification.py:72-111](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/applications/classification.py#L72-L111) — 预处理五连(4.3 模块精读)。

[samples/applications/classification.py:114-118](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/applications/classification.py#L114-L118) — 推理:`TRT` 包装类接受张量列表、返回张量列表。

[samples/applications/classification.py:121-129](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/applications/classification.py#L121-L129) — 后处理:分配 `(1,1000)` numpy 数组,D2H 拷贝,`argsort` 取 Top-5 打印。

#### 4.1.4 代码实践

**实践目标**:跑通示例,亲眼看到 Top-5 分类结果。

**操作步骤**(依据 [samples/README.md:44-74](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/README.md#L44-L74)):

```bash
cd samples
./install_samples_dependencies.sh   # 建 venv_samples 并装齐依赖(含 torch/tensorrt/onnx)
source venv_samples/bin/activate
python3 applications/classification.py
```

首次运行会导出 ONNX 并编译 TensorRT 引擎(可能要几分钟),产物缓存在仓库根的 `cvcuda/.cache/`。

**需要观察的现象**:

1. 首次运行的耗时明显长于第二次(模型缓存生效)。
2. 输出的 Top-5 中,`Class 282` 应排在第一(虎斑猫是 ImageNet 第 282 类),置信度是 softmax 概率。

**预期结果**:打印 5 行 `Class <id>: <prob>`,第一个类别为 282 附近的猫科类别。若你换了 `--input` 图片,类别应随之改变。本实践依赖 GPU + TensorRT 环境,若无环境,标注**待本地验证**,改为纯阅读 4.1.3 的六段源码并手绘数据流图。

#### 4.1.5 小练习与答案

**练习 1**:这条管线里,数据有几次跨越 CPU↔GPU 边界?分别在哪两行代码?

答案:两次。第一次在解码阶段(nvimgcodec 把 JPEG 解码进显存,JPEG 压缩字节从磁盘经 CPU 到 GPU);第二次在 [classification.py:123-124](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/applications/classification.py#L123-L124) 的 `cuda_memcpy_d2h` 把 1000 个 logits 拷回 numpy。中间的 stack/resize/convertto/normalize/reformat/推理全部在显存内完成。另一个容易误判的点:第 76-86 行的 `cuda_memcpy_h2d` 上传 mean/std 参数,它发生在**管线开始前**(常量准备),不属于像素数据流。

**练习 2**:`--width 384 --height 384` 运行会发生什么?需要改其他代码吗?

答案:不需要。`args.width/height` 同时驱动三处:ONNX 导出的输入形状(L57)、resize 目标形状(L93)、输出文件名。但注意每次换尺寸都会**重新导出模型并重编引擎**(缓存文件名含尺寸,`resnet50_384x384.onnx` 不存在触发重建)。这是"模型输入形状固定"设计带来的代价。

### 4.2 解码与零拷贝纳管:read_image 的契约

#### 4.2.1 概念说明

管线的第一步决定后续一切:`read_image` 的返回值契约是 **RGB8 HWC 张量**。这个契约来自 nvimgcodec 默认解码为交错 RGB,再经 `cvcuda.as_tensor(nvc_img, "HWC")` 零拷贝纳管(u2-l4 讲过:元数据抄入、不搬像素,引用链保证生命周期)。

`common.py` 是所有 sample 的共享工具层,值得整体浏览一遍——它本身就是"CV-CUDA 与生态库互操作"的袖珍手册。

#### 4.2.2 核心流程

```text
read_image(file):
  decoder = nvimgcodec.Decoder()
  nvc_img = decoder.read(file)      # 解码,像素落显存
  return cvcuda.as_tensor(nvc_img, "HWC")   # 零拷贝包装

write_image(tensor, file):          # 反向:显存 → JPEG
  nvc_img = nvimgcodec.as_image(tensor.cuda())
  encoder.write(file, nvc_img)
```

注意 `tensor.cuda()`(u2-l4):它返回带双协议(CAI + DLPack)的 ExternalBuffer,同样不搬数据。`read_image` 吃 CAI,`write_image` 吐 CAI,一进一出都是零拷贝。

#### 4.2.3 源码精读

[samples/common.py:355-374](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/common.py#L355-L374) — `read_image`:两行核心 + 一份契约文档。docstring 明确说"下游需要显式格式的算子(如 `pillowresize` 的 `RGB8`)依赖此契约"——**契约写进文档**是这个工具层的风格。

[samples/common.py:377-396](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/common.py#L377-L396) — `write_image`:编码前的 `nvimgcodec.as_image(tensor.cuda())` 即零拷贝导出。

[samples/common.py:58-75](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/common.py#L58-L75) — `_cuda_memcpy`:对 `cuda.bindings.runtime.cudaMemcpy` 的薄封装,带错误检查。

[samples/common.py:78-99](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/common.py#L78-L99) — `_get_device_ptr`:从三种形态(裸整型指针 / CAI 对象 / CAI 字典)统一提取设备地址。这是 Python 侧拿**裸设备指针**的惯用法,4.4 模块的 TRT 类也靠它。

[samples/common.py:102-145](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/common.py#L102-L145) — `cuda_memcpy_h2d` / `cuda_memcpy_d2h`:假设**紧凑布局**的一维拷贝。classification.py 用它上传 mean/std(形状 `(1,1,1,3)` 的连续小张量)与下载 logits(同样连续),安全。

[samples/common.py:148-205](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/common.py#L148-L205) — `_tensor_copy_geometry`:处理**带行距填充**的张量拷贝。回忆 u2-l1:CV-CUDA 分配的行距默认对齐到设备纹理边界(通常 32 字节),若按 `shape` 乘积做一维拷贝会把图像"剪切错位"。这个函数从 CAI strides 推出 `cudaMemcpy2D` 的 (dpitch, width, num_rows) 几何。`download_tensor`/`upload_tensor`(L208-288)基于它提供尊重行距的上下传。

[samples/common.py:879-901](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/common.py#L879-L901) — `debug_helper`:打印任意张量的 shape/dtype/layout/strides,是后续实践的观察工具。

#### 4.2.4 代码实践

**实践目标**:亲手验证 `read_image` 的 RGB8 HWC 契约,并踩一次行距填充。

**操作步骤**(示例代码,非项目原有):

```python
import sys, cvcuda
sys.path.append("samples")
from common import read_image, debug_helper

t = read_image("samples/assets/images/tabby_tiger_cat.jpg")
debug_helper("decoded", t)
iface = t.cuda().__cuda_array_interface__
packed = t.shape[1] * t.shape[2] * t.dtype.itemsize   # 紧凑行宽(字节)
print("row stride:", iface["strides"][0], "packed row:", packed)
```

**需要观察的现象**:`shape` 为 `(H, W, 3)`、dtype uint8、layout HWC;`strides[0]`(行距)大于等于 `packed row`,且差值是填充。

**预期结果**:行距 ≥ 紧凑行宽,通常对齐到 32 的倍数(依 GPU 而异)。若两者相等,说明该尺寸恰好整除对齐边界。这一现象解释了为什么 `zero_copy_split` 和 `_tensor_copy_geometry` 都要小心翼翼地保留 strides——**待本地验证**(需要 GPU 环境)。

#### 4.2.5 小练习与答案

**练习 1**:为什么 classification.py 后处理能用 `cuda_memcpy_d2h`(假设紧凑布局)下载 logits,而下载图像必须用 `download_tensor`(尊重行距)?

答案:TRT 输出张量是 `cvcuda.Tensor((1,1000), F32)` 自己分配的一维张量(common.py L815),一维连续、无行距填充,一维拷贝正确。而图像类张量的行距按对齐属性填充,一维拷贝会按错误的总字节数搬运,导致行错位。判断标准:看 CAI 的 `strides` 是否等于按 shape 推出的紧凑步长。

**练习 2**:`read_image` 为什么显式传 `"HWC"` 而不是让 as_tensor 自动推断?

答案:u2-l4 讲过,包装张量的 layout 默认为 `None`(不猜测)。显式传 `"HWC"` 让下游按布局语义正确寻址;同时 docstring 把"解码即交错 RGB"写成契约,下游需要 `cvcuda.Format.RGB8` 的算子(如 pillowresize)才能配合。

### 4.3 预处理五连:从 U8 HWC 到 F32 NCHW

#### 4.3.1 概念说明

ImageNet 预训练模型的输入配方是固定的:RGB、224×224、float32、按通道标准化。`classification.py` 用五个算子逐步"烹饪"出这个输入。这是 CV-CUDA 最典型的使用场景——**每一步都是一次显存读写**,步数越多、中间张量越大,带宽浪费越多。这也正是 u3-l4 融合算子存在的理由,是本讲综合实践的伏笔。

#### 4.3.2 核心流程

每一步对张量三个属性(形状 / dtype / 布局)的改变如下表,这是本模块最重要的记忆物:

| 步骤 | 算子 | 输入 → 输出 | 改变了什么 |
|------|------|------------|-----------|
| 4.2 加批维 | `stack` | `(H,W,3)` → `(1,H,W,3)` U8 NHWC | 形状 |
| 4.3 缩放 | `resize` | `(1,H,W,3)` → `(1,224,224,3)` U8 NHWC | 形状(插值结果量化回 U8) |
| 4.4 转浮点 | `convertto` | U8 → F32,值 ×1/255 | dtype + 值域 \([0,255] \to [0,1]\) |
| 4.5 标准化 | `normalize` | F32 → F32 | 值域(逐通道减均值除标准差) |
| 4.6 重排 | `reformat` | NHWC → NCHW | 布局 |

标准化的数学定义(OpNormalize.h L60-75):

\[ \text{out}[\text{data\_idx}] = (\text{in}[\text{data\_idx}] - \text{base}[\text{param\_idx}]) \cdot \text{scale}[\text{param\_idx}] \cdot \text{global\_scale} + \text{shift} \]

带 `SCALE_IS_STDDEV` 标志时 scale 解释为标准差,系数取:

\[ m = \frac{1}{\sqrt{\text{stddev}^2 + \epsilon}} \]

参数张量的广播规则(OpNormalize.h L77-80):某一轴上参数形状为 1 则广播,否则逐元素对应。ImageNet 的 mean/std 是逐通道常数,所以参数张量形状取 `(1,1,1,3)`,在 C 轴上逐通道、在 N/H/W 轴上广播。

#### 4.3.3 源码精读

[samples/applications/classification.py:74-86](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/applications/classification.py#L74-L86) — 常量准备:ImageNet 的 mean = \((0.485, 0.456, 0.406)\)、std = \((0.229, 0.224, 0.225)\),各造一个 `(1,1,1,3)` F32 NHWC 张量并 H2D 上传。注释强调"只需一次、可复用于所有图片"——常量张量放循环外,是流水线化的基本意识。

[samples/applications/classification.py:88-89](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/applications/classification.py#L88-L89) — `cvcuda.stack([input_image])`:把单图列表堆成批张量。单元素 stack 的实际作用就是**加批维**(拷贝语义,hello_world 里对多图才是真合批,见 4.5)。

[samples/applications/classification.py:91-94](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/applications/classification.py#L91-L94) — `cvcuda.resize` 到 `(1,224,224,3)`,LINEAR 插值(u3-l1 讲过:输出形状由调用者给,dtype/布局继承输入)。注意:独立 resize 的输入输出类型必须一致,所以**插值的浮点中间结果被量化回 U8**——这个细节在综合实践里会成为两版精度差异的来源。

[samples/applications/classification.py:96-99](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/applications/classification.py#L96-L99) — `cvcuda.convertto(..., np.float32, scale=1/255)`:u3-l2 讲过的仿射转换,这里一步完成"转 F32 + 压到 [0,1]"。

[samples/applications/classification.py:101-107](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/applications/classification.py#L101-L107) — `cvcuda.normalize(float_tensor, scale_tensor, std_tensor, SCALE_IS_STDDEV)`:base 参数此处是 mean 张量,scale 参数是 std 张量(标志让库取其倒数)。

[samples/applications/classification.py:109-110](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/applications/classification.py#L109-L110) — `cvcuda.reformat(normalized_tensor, "NCHW")`:纯布局重排,值不变。ResNet 的第一层卷积按 NCHW 访问,这一步是给模型"摆盘"。

#### 4.3.4 代码实践

**实践目标**:用 `debug_helper` 逐行填出 4.3.2 的表格,把抽象配方变成亲眼所见。

**操作步骤**(示例代码):在 classification.py 的 L110 后面临时插入(或复制脚本做实验版):

```python
from common import debug_helper
for tag, t in [("stacked", input_tensor), ("resized", resized_tensor),
               ("float", float_tensor), ("normalized", normalized_tensor),
               ("nchw", tensor)]:
    debug_helper(tag, t)
```

**需要观察的现象**:五个张量的 shape、dtype、layout 逐步变化;前三个 layout 均为 NHWC,最后变为 NCHW;normalized 与 float 的属性完全相同(只变值)。

**预期结果**:与 4.3.2 表格逐行一致。特别注意 resized 仍是 uint8——这就是"量化回源类型"的实证。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**:把 4.4(convertto)和 4.5(normalize)交换顺序,结果还对吗?

答案:不对。convertto 的 `1/255` 把 U8 映射到 \([0,1]\),而 ImageNet 的 mean/std 是在 \([0,1]\) 值域上统计的。若先 normalize(在 \([0,255]\) 值域上减 0.485 量级的均值)再乘 1/255,数值完全错误。预处理算子的**顺序**和参数值域是强耦合的配方。

**练习 2**:mean/std 张量形状为什么是 `(1,1,1,3)` 而不是 `(3,)`?

答案:依据 OpNormalize.h 的广播规则(L77-80、L129-130),参数张量与数据张量逐轴对应:轴上_extent 为 1 则广播。`(1,1,1,3)` 在 N/H/W 轴广播、C 轴逐通道,与 NHWC 数据的语义严格对齐。一维 `(3,)` 的秩不匹配,无法按该规则对应。

**练习 3**:这五步里哪几步改变了像素值,哪几步只改变"摆放方式"?

答案:改变值:resize(重采样)、convertto(×1/255)、normalize(标准化);只改变元数据/摆放:stack(拷贝加轴,值不变)、reformat(布局重排,值不变)。

### 4.4 推理桥接:TRT 包装类与流语义

#### 4.4.1 概念说明

TensorRT 不认识 `cvcuda.Tensor`,只认识**裸设备指针 + CUDA 流**。`common.py` 的 `TRT` 类就是这二者之间的桥:它把 CV-CUDA 张量的显存地址直接交给 TRT 引擎,零拷贝完成推理。这一模块同时是 u4-l1 流模型的落地示范——你会看到 `execute_async_v3` 如何提交到 `cvcuda.Stream.current`。

#### 4.4.2 核心流程

```text
TRT(path):                       # 构造
  反序列化 engine → 创建 context
  为每个 OUTPUT 分配 cvcuda.Tensor,记录 dtype(labels/num_detections → S32,其余 F32)
  set_tensor_address(output_name, 输出张量裸指针)   # 输出地址固定

TRT(tensors):                    # 每次推理
  for 每个输入: set_tensor_address(name, tensor 裸指针)
  execute_async_v3(cvcuda.Stream.current.handle)   # 异步提交到当前流
  cvcuda.Stream.current.sync()                     # 立即同步
  return 输出张量列表
```

#### 4.4.3 源码精读

[samples/common.py:781-823](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/common.py#L781-L823) — 构造函数:遍历引擎的 IO 张量,输出侧用 `cvcuda.Tensor(shape, dtype)` 分配(依据名字猜 dtype:labels/num_detections 是 S32,其余 F32——这是为检测示例复用的痕迹),然后把**输出**的设备地址经 `set_tensor_address` 绑定一次。

[samples/common.py:825-836](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/common.py#L825-L836) — `__call__`:每次推理绑定**输入**地址(L826-829,从 `tensor.cuda().__cuda_array_interface__["data"][0]` 取裸指针——与 4.2 的 `_get_device_ptr` 同一惯用法);L831 把工作提交到当前流;L834 立即 `sync()`。

流语义解读(承接 u4-l1):`execute_async_v3` 本身不等待,同步责任在调用方。示例选择**每步立即同步**——正确性最稳(后续 D2H 拷贝必然看到完整结果),但牺牲了流水线重叠。生产管线会在循环外同步、用 `_into` 变体复用输出,让解码/预处理/推理三段在同一条流上自然重叠。这也是 u4-l1 讲过的"库管隐式同步、显式依赖归调用者"的直接体现:若删掉 L834 的 sync,L831 之后立刻 D2H 拷贝 logits,读到的是**未完成**的计算结果。

[samples/common.py:399-444](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/common.py#L399-L444) — `export_classifier_onnx`:用 `ClassifierEnd2End` 包装器把 **softmax 折进模型**再导出(opset 18,经 onnxslim 精简)。所以管线的输出已是概率,后处理只需排序。

[samples/common.py:719-778](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/common.py#L719-L778) — `engine_from_onnx`:TRT 引擎编译,含 8GB workspace 上限与 **timing cache**(跨次构建复用 tactic 计时,二次编译显著加速)。

#### 4.4.4 代码实践

**实践目标**:验证"删掉同步会读到脏数据"这一断言,理解流语义的边界。

**操作步骤**(源码阅读 + 思想实验,无需改库):

1. 复制 `common.py` 为 `common_exp.py`,把 L834 的 `cvcuda.Stream.current.sync()` 注释掉,并在 classification.py 里改 import。
2. 在 D2H 拷贝后打印 logits,与原版对比。

**需要观察的现象**:可能打印出全零或乱值 logits(取决于机器时序),也可能偶发正确——**非确定性**正是数据竞争的特征。

**预期结果**:结果不稳定或错误;恢复 sync 后恢复稳定正确。另外观察:输入地址每次 `__call__` 都重新绑定(L826-829),输出地址只在构造时绑定一次(L820-823)——因为输出张量由 TRT 类长期持有。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**:`TRT.__call__` 里为什么输入地址每次绑定,输出地址只绑一次?

答案:输入张量由调用方每次传入(可能是新分配的 allocating 变体输出,地址会变);输出张量是 TRT 类构造时自己分配并持有的固定缓冲,地址不变。绑定的是**地址**,不是内容。

**练习 2**:如果想把这条单图管线改造成流式服务(每请求一次推理),`TRT` 类有哪些天然契合与不足?

答案:契合——输出张量复用(零分配)、`__call__` 接口直接收发 cvcuda.Tensor。不足——内部立即 `sync()` 强制串行,无法与其他请求的预处理重叠;输出缓冲单一,并发请求会互相覆盖,需每请求独立 context 或加锁;固定 batch=1 引擎无法吃批量输入。

### 4.5 分批技巧:stack 与 zero_copy_split

#### 4.5.1 概念说明

classification.py 是单图,真实的预处理服务要处理**一批尺寸各异的图**。`hello_world.py` 示范了合批与切批的一对镜像操作:

- **合批 `cvcuda.stack`**:多张同尺寸张量 → 一个批张量(拷贝拼接,u2-l3 讲过要求同尺寸同格式)。
- **切批 `zero_copy_split`**:批张量 → 多张单图张量(**零拷贝**,只造视图)。

两者顺序上有讲究:hello_world 是"先各自 resize 到同尺寸,再 stack"(先统一尺寸,stack 才合法);classification 是"先 stack(单图加维),再 resize"。前者是多图合批的通用范式。

#### 4.5.2 核心流程

`zero_copy_split` 的原理(u2-l4 CAI 知识的逆运用):

```text
对批张量 (N,H,W,C):
  batch_stride = strides[0]                # 每张图在显存中的跨距(字节)
  第 i 张的视图:
    data   = 基址 + i × batch_stride       # 平移指针
    shape  = (H,W,C);strides = strides[1:] # 行距原样保留(尊重填充!)
    obj    = 原批张量的 CAI buffer          # 引用链保活
  → cvcuda.as_tensor(视图, "HWC")
```

不分配一字节显存,只是 N 个指向同一块缓冲不同偏移的"窗口"。**生命周期靠引用链**:视图持着 `obj = batch_tensor.cuda()`,批张量不释放,窗口就有效。

#### 4.5.3 源码精读

[samples/applications/hello_world.py:182-192](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/applications/hello_world.py#L182-L192) — 多图解码:N 次 `decoder.read` + N 次 `as_tensor`,得到尺寸可能互不相同的张量列表。

[samples/applications/hello_world.py:194-211](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/applications/hello_world.py#L194-L211) — 先逐图 resize 到统一尺寸(L197-204 的列表推导),**然后** `cvcuda.stack(resized_tensors)` 合批(L210)。顺序不可颠倒——尺寸不一 stack 会失败。 gaussian 直接吃批张量(L216-221)。

[samples/common.py:290-352](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/common.py#L290-L352) — `zero_copy_split` 全文:L315-330 取批张量的 CAI 接口与真实 strides(strides 为 None 时按紧凑布局兜底);L333-351 循环造偏移视图,`buffer_interface["strides"] = item_strides` 这一行(L345-346)是"尊重行距填充"的关键——漏掉它,切出的图会错位。`offset_buffer.obj = batch_tensor.cuda()`(L349)挂引用链保活。

[samples/applications/hello_world.py:224-235](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/applications/hello_world.py#L224-L235) — 切批后逐张 `nvimgcodec.as_image(tensor.cuda())` 编码写盘,又一处零拷贝导出。

#### 4.5.4 代码实践

**实践目标**:把 classification.py 改造成多图批推理,体会"模型形状"对分批的约束。

**操作步骤**:

1. 用 `--inputs img1.jpg img2.jpg`(或复制默认猫图为两份不同图)仿照 hello_world L186-191 读入 N 张图。
2. 逐图 resize 到 224×224 后 `cvcuda.stack` 成 `(N,224,224,3)`(注意:与 classification 的"先 stack 后 resize"不同,这里必须**先 resize 后 stack**)。
3. 后续 convertto/normalize/reformat 对 N 批张量同样适用(逐像素算子天然批感知);mean/std 形状 `(1,1,1,3)` 不变(N 轴广播)。
4. 推理前先确认引擎支持 N:当前 `export_classifier_onnx` 用 `torch.randn(1, *input_shape)` 固定导出(L433),**batch=1 引擎喂 N 批会失败**。需要把 dummy 输入改为 `torch.randn(N, *input_shape)`(或动态轴)重新导出并删除 `.cache` 中的旧引擎。

**需要观察的现象**:步骤 2 若顺序颠倒,stack 因尺寸不一报错;步骤 4 若不重导出模型,TRT 绑定/执行报形状不匹配。

**预期结果**:重导出后一次推理输出 `(N,1000)`,每行各自 argsort 出 Top-5。**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**:`zero_copy_split` 返回的视图张量,与用 `cvcuda.Tensor(...)` 新分配再拷贝相比,省了什么、冒险了什么?

答案:省——N 次显存分配与 N 次拷贝带宽。冒险——视图不拥有内存,生命周期完全依赖原批张量的引用链(`obj` 字段);若批张量被释放而视图还在用(尤其异步流上还有未完成工作),就是悬垂指针。hello_world 用完即走(同步编码),不踩这个坑。

**练习 2**:为什么 hello_world 选择"先 resize 后 stack",而 classification 可以"先 stack 后 resize"?

答案:stack 要求所有输入同尺寸。hello_world 的多张原图尺寸互不相同,必须先统一;classification 只有一张图,stack 只是加批维,先后无所谓。范式:**尺寸不一致时,统一尺寸的步骤必须前置于合批**。

## 5. 综合实践

**任务**:把 classification.py 的预处理(4.3 节五连中的 resize + convertto + reformat 三步)替换为 u3-l4 的融合算子 `resize_crop_convert_reformat`,对比两版的内核数、吞吐与数值精度,并解释差异来源。

### 第一步:确认融合算子的契约允许这次替换

替换前先查权威契约(这是 u3-l2 养成的习惯):

- [OpResizeCropConvertReformat.h:103-146](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/OpResizeCropConvertReformat.h#L103-L146) — Limitations:输入 U8、1 或 3 通道、HWC/NHWC/CHW/NCHW;输出允许 **F32 + NCHW**。`read_image` 的 RGB8 NHWC 完全落位。
- [python/mod_cvcuda/operators/OpResizeCropConvertReformat.cpp:207-210](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpResizeCropConvertReformat.cpp#L207-L210) — 绑定参数:`scale`/`offset` 是**标量 float**,`srcCast` 默认 `true`。
- 插值只支持 NEAREST/LINEAR(同文件 L218-219 的 docstring),与原版 LINEAR 一致。

**关键限制**:融合算子的 `scale`/`offset` 是标量,而 ImageNet 归一化需要**逐通道** mean/std——所以 `normalize` 不能折叠,必须保留为第二个 kernel。可行的替换是把 **resize + convertto + reformat 三个 kernel 折成一个**(4 kernel → 2 kernel)。

### 第二步:写融合版预处理

参照 [samples/operators/resize_crop_convert_reformat.py:80-107](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/operators/resize_crop_convert_reformat.py#L80-L107) 的调用方式,替换 classification.py L88-110(示例代码):

```python
# 融合版:resize + 转F32(×1/255) + 重排 NCHW 一步完成
input_tensor = cvcuda.stack([input_image])            # (1,H,W,3) U8 NHWC
crop_rect = cvcuda.RectI(0, 0, args.width, args.height)  # 全图 crop(不裁)

fused = cvcuda.resize_crop_convert_reformat(
    input_tensor,                      # U8 NHWC ✓ 契约
    (args.height, args.width),         # 注意参数序是 resize_dim
    cvcuda.Interp.LINEAR,
    crop_rect,
    layout="NCHW",                     # 一步完成布局重排
    data_type=cvcuda.Type.F32,
    scale=1 / 255,                     # 折叠原 convertto 的缩放
    offset=0.0,
    srcCast=False,                     # 保留插值浮点精度,见第三步
)

# normalize 仍需独立一步,且参数张量必须改为 NCHW 形状!
mean_nchw = cvcuda.Tensor((1, 3, 1, 1), np.float32, "NCHW")   # 原 (1,1,1,3) NHWC
std_nchw  = cvcuda.Tensor((1, 3, 1, 1), np.float32, "NCHW")
# ... H2D 上传同原版 ...
tensor = cvcuda.normalize(fused, mean_nchw, std_nchw,
                          cvcuda.NormalizeFlags.SCALE_IS_STDDEV)
```

**一个必须知道的坑**:normalize 的输入从 NHWC 变成了 NCHW,参数张量形状必须跟着变。依据 [OpNormalize.h:136-141](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/OpNormalize.h#L136-L141):对 planar(NCHW/CHW)输入,逐通道参数必须用 `[1,C,1,1]` 形状(标量参数 `[1,1,1,1]` 两种布局均可)。沿用 `(1,1,1,3)` 会与 NCHW 数据的语义错位。

### 第三步:对比吞吐与精度

1. **计时**:复制 `hello_world.py` 的 `timer` 上下文管理器(L77-90),分别包住两版的预处理段,循环 100 次取平均;更严谨的做法是用 u7-l4 的 NVTX + Nsight Systems 看时间线上 kernel 个数与间隙。预期:预处理从 4 个 kernel 降为 2 个,省掉两次全图显存往返(resize→convertto、normalize→reformat 的中间张量读写)。
2. **精度**:两版各跑一次,打印 Top-5 类别与概率,并 D2H 下载两版最终输入张量逐元素比较最大绝对误差。
3. **解释差异来源**(这是本实践的核心收获):依据 [OpResizeCropConvertReformat.h:90-99](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/OpResizeCropConvertReformat.h#L90-L99),独立 resize 因输入输出同类型,必须把 LINEAR 插值的浮点结果**量化回 U8**;融合算子设 `srcCast=False` 可让插值结果留在浮点空间,只量化一次(U8 输入本身就是量化的)。所以:
   - `srcCast=True`(默认):融合版刻意复刻独立执行的量化路径,两版结果应几乎一致;
   - `srcCast=False`:融合版少一次量化,数值上**更精确**,与原版有微小差异,Top-1 通常不变但概率尾数不同。
4. **记录结论**:写一张三列表格(原版 / 融合 srcCast=True / 融合 srcCast=False)× (kernel 数、平均耗时、Top-1 类别、与原版的最大像素误差)。

**预期结果**:融合版 kernel 数 4→2、预处理段耗时下降(幅度待测);`srcCast=True` 版 Top-5 与原版一致;`srcCast=False` 版 Top-1 一致、概率有细微差别、像素误差非零但量级远小于 1/255。全流程依赖 GPU + TensorRT 环境,标注**待本地验证**。

## 6. 本讲小结

- `classification.py` 是一条**全程留在 GPU** 的分类管线:nvimgcodec 解码进显存 → `stack` 加批维 → resize/convertto/normalize/reformat 预处理五连 → TRT 推理 → D2H 取回 1000 个 logits;CPU 只经手 JPEG 字节与最终浮点结果。
- 预处理五连的本质是逐步改造张量的三个属性:形状(resize)、dtype 与值域(convertto、normalize)、布局(reformat);每一步都是一次显存读写,步数就是带宽成本。
- `common.py` 是互操作工具层:`read_image` 的 RGB8 HWC 契约、CAI 裸指针提取、尊重行距填充的 2D 拷贝几何,这些细节决定了"像素不出 GPU"能否成立。
- `TRT` 类用 `set_tensor_address` + 裸设备指针把 cvcuda.Tensor 零拷贝喂给 TensorRT;`execute_async_v3` 提交到当前流,**同步责任在调用方**——示例选择每步立即 sync,正确但串行。
- 分批范式:尺寸不一的多图**先 resize 统一尺寸、再 stack 合批**;`zero_copy_split` 用 CAI 偏移视图零拷贝切批,生命周期靠引用链保活。
- 融合算子 `resize_crop_convert_reformat` 可把 resize+convertto+reformat 三步折成一步(4→2 kernel),但其 scale/offset 是标量,逐通道 normalize 折不进去;planar 输入下 normalize 参数张量须改用 `[1,C,1,1]` 形状。

## 7. 下一步学习建议

下一讲 **u9-l2《端到端应用二:目标检测与实例分割管线》** 将剖析 `samples/applications/object_detection.py` 与 `segmentation.py`:批推理、检测框/掩码后处理、以及与几何算子的组合。建议提前浏览 `common.py` 的 `export_retinanet_onnx`(L495-716,含 EfficientNMS 插件拼接)——你会发现 TRT 类里 "labels/num_detections → S32" 的 dtype 猜测正是为它准备的。

继续深挖的建议路径:

1. **源码**:对照阅读 [samples/applications/hello_world.py](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/applications/hello_world.py) 与本讲,体会"演示最小化"(hello_world)与"管线真实化"(classification)两种示例的取舍。
2. **性能**:用 u7-l4 的 Nsight Systems 给本讲综合实践的两版管线各捕一条时间线,数一数 kernel 行上的矩形数量,直观验证 4→2。
3. **工程化**:思考把 `TRT.__call__` 的立即 sync 改为流式(删 sync、输出改 `_into`、循环外统一同步)需要哪些配套改动——这正是 u4-l1/u4-l3 并发知识的综合应用。
