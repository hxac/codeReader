# 生态互操作：编解码器、CUDA 框架与零拷贝集成

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `samples/interoperability` 下 8 个示例各自演示的框架（nvimgcodec、PyNvVideoCodec、cuda-python、PyCUDA、CuPy、PyTorch、NumPy）以及它们与 CV-CUDA 交换显存所走的协议。
2. 独立读懂并仿写三条主干互操作链路：图像解码/编码（nvimgcodec）、视频解码/编码（PyNvVideoCodec）、裸 CUDA 缓冲（cuda-python 手写 `__cuda_array_interface__`）。
3. 排查两类最常见的互操作失败：**设备不符**（CPU 数组被拒）与 **stride 不连续/非法**（CAI 字节 stride 不能被元素大小整除时被拒）。
4. 组装一条「解码 → resize → cvtcolor → 编码保存」的最小全 GPU 流水线，并解释为什么像素全程不离开显存。

本讲是学习手册倒数第二讲。前面 u2-l4 已经从**绑定层源码**的角度讲过 `as_tensor` 的三座桥（type_caster → CAI → DLPack），本讲的视角切换到**使用侧**：把仓库自带的 8 个互操作示例当成 8 份「官方答案」，看真实生态里的各个框架是怎么把显存递给 CV-CUDA、又是怎么拿回去的。

## 2. 前置知识

### 2.1 两份协议：CAI 与 DLPack（回顾）

u2-l4 讲过，`cvcuda.as_tensor(buffer)` 能接受的任何对象，必须至少实现下面两份协议之一：

| 协议 | 全称 | 关键字段 | stride 单位 | 代表框架 |
|------|------|----------|-------------|----------|
| CAI | CUDA Array Interface | `shape`、`typestr`、`data`、`strides`、`stream` | **字节** | CuPy、PyCUDA、nvimgcodec、本讲的 `CudaBuffer` |
| DLPack | Deep Learning Packager | `DLManagedTensorVersioned` 胶囊 | **元素** | PyTorch、NumPy（≥1.22） |

两者都不是 CUDA 官方标准，而是社区事实标准：对象只要暴露 `__cuda_array_interface__`（一个 dict）或 `__dlpack__()`（返回一个胶囊），消费方就能拿到「设备指针 + 形状 + 类型 + 步长」四要素，从而**零拷贝**地把它当作自己的数组使用。

CV-CUDA 在两份协议之间做翻译：CAI 的字节 stride 会被换算成元素 stride，DLPack 的设备类型会被检查是否 CUDA 可访问。翻译的落点就是 u2-l4 精读过的 `ExternalBuffer`，本讲 4.1 节只做一页速查，不重复展开。

### 2.2 编解码器在管线中的位置

u9-l1 的分类管线里，nvimgcodec 负责「JPEG 字节流 → GPU 上的 RGB 像素」。本讲把视野扩展到完整闭环：

```
压缩字节流(CPU/磁盘) ──解码──> GPU 像素 ──CV-CUDA 算子──> GPU 像素 ──编码──> 压缩字节流(CPU/磁盘)
     nvimgcodec / PyNvVideoCodec            resize、cvtcolor 等        nvimgcodec / PyNvVideoCodec(NVENC)
```

关键认识：**解码器和编码器本身就是 GPU 程序**（NVDEC/NVENC 是显卡上的专用硬件单元）。压缩数据必须经过 CPU 可见的内存（文件、页缓存），但解码之后的像素可以直接落在显存里。因此「全 GPU 管线」里 CPU 只经手压缩字节流——这句话在 u1-l2 的 hello_world 里出现过，本讲用源码把它落实。

### 2.3 NV12：视频编码的通用语言

视频编码器（NVENC）几乎只吃 **YUV 4:2:0** 家族的格式，最常见的是 NV12：先存一个 H×W 的亮度（Y）平面，再存一个 (H/2)×(W/2) 的 UV 交错平面。U8 数据下一帧 NV12 的字节数是：

\[
\text{bytes} = H \cdot W + \frac{H}{2}\cdot\frac{W}{2}\cdot 2 = H \cdot W \cdot \frac{3}{2}
\]

CV-CUDA 用一个技巧把 NV12 塞进普通张量：把双平面按高度拼接，即形状为 \((\frac{3}{2}H,\ W,\ 1)\) 的 U8 张量（u2-l2 讲过 NV12 无法转成规则的 `Image`，但作为张量可以按此约定传递）。本讲 4.5 节的源码会验证这个 3/2 系数。

### 2.4 术语表

- **CAI（CUDA Array Interface）**：Python GPU 数组的事实标准协议，v3 版本新增 `stream` 字段。
- **DLPack 胶囊**：`__dlpack__()` 返回的 `PyCapsule`，内含 `DLTensor` 结构。
- **pinned memory（页锁定内存）**：用 `cudaMallocHost` 分配的 CPU 内存，设备可通过 DMA 直接访问，DLPack 里记作 `kDLCUDAHost`。
- **NVDEC / NVENC**：GPU 上的硬件解码/编码单元。
- **zero-copy（零拷贝）**：只传递指针与元数据、不搬运像素数据。

## 3. 本讲源码地图

本讲涉及的关键文件（按阅读顺序）：

| 文件 | 作用 |
|------|------|
| [samples/interoperability/](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/interoperability) | 8 个互操作示例 + 1 个共享辅助模块 |
| `samples/interoperability/nvimgcodec_interop.py` | 图像 JPEG 解码 → resize → 编码（**重点模块一**） |
| `samples/interoperability/cuda_python_common.py` | 手写 `__cuda_array_interface__` 的 `CudaBuffer` 类 |
| `samples/interoperability/cuda_python_interop_1.py` | 外来缓冲 → cvcuda（**重点模块二**） |
| `samples/interoperability/cuda_python_interop_2.py` | cvcuda 分配 → cudaMemcpy 灌数据（反方向） |
| `samples/interoperability/pycuda_interop.py` | PyCUDA GPUArray 与 cvcuda 互转（**重点模块三**） |
| `samples/interoperability/cupy_interop.py`、`pytorch_interop.py`、`numpy_interop.py` | 其余计算框架的互转（4.4 节汇总） |
| `samples/interoperability/pynvvideocodec_interop.py` | 视频解码 → 处理 → NVENC 编码（**重点模块四**） |
| `python/mod_cvcuda/nvcv/ExternalBuffer.cpp` | 绑定层协议探测与校验（u2-l4 精读过，本讲引用） |
| `python/mod_cvcuda/nvcv/DLPackUtils.cpp` | `IsCudaAccessible` 设备白名单 |
| `python/mod_cvcuda/CvtColorUtil.cpp` | cvtcolor 输出形状推导（NV12 的 3/2 系数） |
| `samples/requirements.samples.common.template`、`requirements.samples.cu12.template` | 各框架依赖的安装来源 |

运行这些示例的依赖不是 pip 装 cvcuda 就够的——每个框架各有自己的包，来源见 4.1 节末尾的依赖表。

## 4. 核心概念与源码讲解

### 4.1 模块一：互操作全景——三座桥的落点与 8 个示例地图

#### 4.1.1 概念说明

8 个示例解决的问题可以用一张对称的表概括：**「别人 → cvcuda」用 `as_tensor`，「cvcuda → 别人」用 `tensor.cuda()`**。`tensor.cuda()` 这个名字容易误导——它并不搬运数据，而是返回一个实现了 CAI 与 DLPack **双协议**的 `ExternalBuffer` 对象（u2-l4 讲过导出路径），别的框架按各自习惯从中取指针。

| 示例文件 | 框架 | 进（→ cvcuda） | 出（cvcuda →） | 走的协议 |
|----------|------|----------------|----------------|----------|
| `nvimgcodec_interop.py` | nvimgcodec | `as_tensor(image, "HWC")` | `nvimgcodec.as_image(t.cuda())` | CAI/DLPack |
| `pynvvideocodec_interop.py` | PyNvVideoCodec | `as_tensor(frame, "HWC")` | `encoder.Encode(tensor)` | 适配 CAI/DLPack |
| `cuda_python_interop_1.py` | cuda-python | `as_tensor(cuda_buffer)` | `CudaBuffer.from_cuda(t.cuda())` | 手写 CAI v3 |
| `cuda_python_interop_2.py` | cuda-python | `cvcuda.Tensor` 自己分配 | `cudaMemcpy(t.cuda(), …)` | CAI |
| `pycuda_interop.py` | PyCUDA | `as_tensor(gpu_array)` | `GPUArray(gpudata=t.cuda().__cuda_array_interface__["data"][0])` | CAI |
| `cupy_interop.py` | CuPy | `as_tensor(cupy_array)` | `cupy.asarray(t.cuda())` | CAI（cupy 两者都有） |
| `pytorch_interop.py` | PyTorch | `as_tensor(torch_tensor)` | `torch.as_tensor(t.cuda())` | DLPack |
| `numpy_interop.py` | NumPy（CPU） | 经上面四种桥上 GPU | 各自读回 | ——（numpy 本身不能直接进） |

#### 4.1.2 核心流程

无论哪个框架，`as_tensor` 的探测流程都固定为：

```
传入对象
   │
   ├─ 有 __cuda_array_interface__ ? ──是──> 解析 CAI dict（字节 stride → 元素 stride，
   │                                        校验指针 CUDA 可访问，记录 stream 字段）
   ├─ 有 __dlpack__ ?               ──是──> 调 __dlpack_device__ 查设备类型（须 CUDA 系），
   │                                        再取 DLPack 胶囊
   └─ 都没有                        ──>    返回 false → pybind11 抛 TypeError
```

而 `tensor.cuda()` 的反向流程是：把张量的元数据与设备指针打包成 `ExternalBuffer`，同时把「这个张量最后一次被哪个流写入」写进 CAI 的 `stream` 字段，让下游框架（cupy/torch）能做跨流同步——这是 u4-l1 讲过的「生产者流记账」的导出侧。

#### 4.1.3 源码精读

**（1）协议探测入口**——[python/mod_cvcuda/nvcv/ExternalBuffer.cpp:L517-L536](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/ExternalBuffer.cpp#L517-L536)：这段 `load` 就是上图流程的源码化身——先用 `hasattr` 探测 CAI，再探测 DLPack，两者皆无则返回 `false`，pybind11 会把「类型转换失败」翻译成 `TypeError`。

**（2）设备白名单**——[python/mod_cvcuda/nvcv/DLPackUtils.cpp:L280-L291](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/DLPackUtils.cpp#L280-L291)：这段 `IsCudaAccessible` 定义了哪些 DLPack 设备类型能进 CV-CUDA——`kDLCUDA`（显存）、`kDLCUDAManaged`（托管内存）、`kDLCUDAHost`（页锁定的 CPU 内存，设备可 DMA 访问）三种放行，纯 CPU（`kDLCPU`）拒绝。注意 pinned host 内存是被放行的——这一点常被忽略。

**（3）CAI 指针实址校验**——[python/mod_cvcuda/nvcv/ExternalBuffer.cpp:L53-L67](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/ExternalBuffer.cpp#L53-L67)：`CheckValidCUDABuffer` 对指针调用 `cudaPointerGetAttributes`，若查询失败或类型是 `cudaMemoryTypeUnregistered`（未注册的普通 CPU 内存），抛出 `"Buffer is not CUDA-accessible"`。这就是「对象声称在 GPU、指针实际在 CPU」时的第二道防线。

**（4）stride 合法性**——[python/mod_cvcuda/nvcv/ExternalBuffer.cpp:L396-L410](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/ExternalBuffer.cpp#L396-L410)：CAI 的 stride 以**字节**计，这里逐维检查「字节 stride 必须能被元素大小整除」，然后除以 `itemSize` 换算成 DLPack 的元素 stride。若有人构造了字节不对齐的视图，会在这里抛 `"Stride must be a multiple of the element size in bytes"`。

**（5）DLPack 设备拒绝**——[python/mod_cvcuda/nvcv/ExternalBuffer.cpp:L598-L607](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/ExternalBuffer.cpp#L598-L607)：`loadDLPack` 先调对方的 `__dlpack_device__`，设备类型不在白名单里就抛 `"Only CUDA-accessible memory buffers can be wrapped"`。NumPy 2.x 的 CPU 数组实现了 `__dlpack__`，所以 CPU numpy 数组走的是这条路（综合实践会验证）。

**（6）导出侧注册**——[python/mod_cvcuda/nvcv/Tensor.cpp:L546-L548](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Tensor.cpp#L546-L548)：Python 侧的 `cvcuda.as_tensor` 就绑在这里——一个重载吃 `buffer`（ExternalBuffer），一个吃 `image`（nvimgcodec 风格的图像对象），都带可选的 `layout` 参数。另见 [Tensor.cpp:L386-L401](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Tensor.cpp#L386-L401)：`Tensor::cuda()` 导出时把 `getLastStreamHandle()` 的结果写进 CAI 的 `stream` 字段（从未被算子写过则不写该字段）。

**（7）依赖从哪来**——[samples/requirements.samples.common.template:L31-L33](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/requirements.samples.common.template#L31-L33) 与 [samples/requirements.samples.cu12.template:L30-L34](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/requirements.samples.cu12.template#L30-L34)：前者声明 `pycuda` 与 `PyNvVideoCodec`（后者仅限 x86_64、Python 3.10–3.12），后者从 NVIDIA 的 PyPI 源装 `cuda-python`、`nvidia-nvimgcodec-cu12`、`cupy-cuda12x`。8 个示例全部跑通要用 `samples/install_samples_dependencies.sh` 一键安装。

#### 4.1.4 代码实践

1. **实践目标**：在跑任何示例之前，先用三个 `hasattr` 探测手里对象走哪座桥。
2. **操作步骤**（纯 Python，无需 GPU 的部分标 ★）：

```python
# 示例代码：探测任意对象暴露的互操作协议
import numpy as np
candidates = {"numpy(CPU)": np.zeros((4, 4))}
try:
    import torch
    candidates["torch(CPU)"] = torch.zeros(4, 4)
except ImportError:
    pass
try:
    import cupy
    candidates["cupy(GPU)"] = cupy.zeros((4, 4))
except ImportError:
    pass

for name, obj in candidates.items():
    print(f"{name:14s} CAI={hasattr(obj,'__cuda_array_interface__')} "
          f"DLPack={hasattr(obj,'__dlpack__')} "
          f"dev={obj.__dlpack_device__() if hasattr(obj,'__dlpack_device__') else None}")
```

3. **需要观察的现象** ★：numpy(CPU) 与 torch(CPU) 都没有 CAI 但**有** DLPack（设备号 1=kDLCPU）；cupy 两者都有（设备号 2=kDLCUDA）。
4. **预期结果**：把输出与 4.1.2 的探测流程对照——numpy(CPU) 会进入 DLPack 分支并死在设备白名单上（`kDLCPU` 被拒），这正是综合实践要复现的报错。
5. 具体报错文本**待本地验证**（本讲义生成环境无 GPU/框架，只给出源码推理链）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `cvcuda.as_tensor(np_array)`（CPU numpy）不会被 `CheckValidCUDABuffer` 拦住，而是报「Only CUDA-accessible...」？

**答案**：`CheckValidCUDABuffer` 只在拿到指针之后才有机会运行（CAI 路径 L562、DLPack 构造 L483）。CPU numpy 数组没有 CAI，走 DLPack 路径时在更早的 `loadDLPack`（L598-607）就被 `__dlpack_device__` 返回的 `kDLCPU` 拦下了，根本走不到指针校验。

**练习 2**：一台机器上同时有 torch 和 cupy，两者都能包装给 cvcuda 吗？

**答案**：都能。torch 走 DLPack（无 CAI），cupy 走 CAI 优先、DLPack 兜底（u2-l4 的探测顺序）。两条路最终都汇合到 `Tensor::Wrap`（[Tensor.cpp:L168-L187](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Tensor.cpp#L168-L187)），只抄元数据、不分配显存。

**练习 3**：CAI 的 `strides` 字段填 `None` 意味着什么？

**答案**：按协议约定表示 C 连续（紧凑行主序），绑定层会按 `strides[i-1] = strides[i] * shape[i]` 从形状推出来（见 [ExternalBuffer.cpp:L418-L425](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/ExternalBuffer.cpp#L418-L425)）。4.3 节的 `CudaBuffer` 就是这么做的。

### 4.2 模块二：nvimgcodec 互操作——图像解码/编码零拷贝往返

#### 4.2.1 概念说明

nvimgcodec（pip 包名 `nvidia-nvimgcodec-cu12`）是 NVIDIA 的 GPU 图像编解码库。u1-l2 的 hello_world 和 u9-l1 的分类管线都请它做解码；本讲的示例补上了**反向**——用它的 `Encoder` 把 CV-CUDA 张量编码成 JPEG。它是互操作示例里唯一「双向都有官方对接」的图像编解码器：`decoder.read()` 返回的 Image 对象暴露互操作协议，`nvimgcodec.as_image()` 又能吃任何实现了协议的 GPU 缓冲。

#### 4.2.2 核心流程

示例的四步闭环：

```
JPEG 文件 ──decoder.read()──> nvimgcodec Image(GPU 显存)
                                    │ cvcuda.as_tensor(image, "HWC")   零拷贝
                                    v
                            cvcuda.Tensor (HWC U8)
                                    │ cvcuda.resize(..., (224,224,3))  GPU 上缩放
                                    v
                            resized Tensor
                                    │ .cuda() 导出双协议缓冲            零拷贝
                                    │ nvimgcodec.as_image(...)
                                    v
                            nvimgcodec Image ──encoder.write()──> JPEG 文件
```

全程两次跨越 CPU/GPU 边界，搬运的都是**压缩字节流**；像素数据从解码落地显存后就再没动过。

#### 4.2.3 源码精读

**（1）解码与零拷贝纳管**——[samples/interoperability/nvimgcodec_interop.py:L52-L54](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/interoperability/nvimgcodec_interop.py#L52-L54)：`decoder.read(str(img_path))` 让 NVDEC 把 JPEG 直接解码进显存；`cvcuda.as_tensor(nvimgcodec_image, "HWC")` 把返回的 Image 对象零拷贝包装成张量。第二个参数 `"HWC"` 是显式布局标注——解码出的交错 RGB 像素按 HWC 语义解释，这比让布局空着（`NONE`）更利于下游算子理解数据。

**（2）GPU 缩放**——[samples/interoperability/nvimgcodec_interop.py:L58-L60](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/interoperability/nvimgcodec_interop.py#L58-L60)：allocating 变体的 `cvcuda.resize`，输出形状 `(224, 224, 3)` 按 HWC 顺序书写（高在前、宽在后），插值用 LINEAR——这些语义 u3-l1 都讲过。

**（3）编码回写**——[samples/interoperability/nvimgcodec_interop.py:L65-L67](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/interoperability/nvimgcodec_interop.py#L65-L67)：`resized_cvcuda_tensor.cuda()` 导出双协议缓冲（不搬数据），`nvimgcodec.as_image(...)` 把它变成 nvimgcodec 的图像视图，`encoder.write` 完成 GPU 编码落盘。注意这里没有 `.cpu()` 之类的调用——编码器直接读显存。

**（4）解码器/编码器的构造**——[samples/interoperability/nvimgcodec_interop.py:L46-L48](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/interoperability/nvimgcodec_interop.py#L46-L48)：无参构造即可工作，设备与后端由库自动选择。

#### 4.2.4 代码实践

1. **实践目标**：跑通官方图像闭环，并确认输出确实是缩放后的图。
2. **操作步骤**：

   ```bash
   cd samples
   ./install_samples_dependencies.sh        # 或只装 cvcuda-cu12 + nvidia-nvimgcodec-cu12
   source venv_samples/bin/activate
   python3 interoperability/nvimgcodec_interop.py
   ls -la ../.cache/tabby_tiger_cat_224_224.jpg
   ```

3. **需要观察的现象**：`.cache` 目录下生成 `tabby_tiger_cat_224_224.jpg`；用任意看图工具打开，应是原图的 224×224 版本。
4. **预期结果**：脚本无输出正常退出；改 `L59` 的形状为 `(112, 112, 3)` 重跑，输出图随之变小。
5. 本机无 GPU 时跳过运行，做**源码阅读型实践**：把 `L53` 的 `"HWC"` 改成 `"CHW"`，说明会发生什么（答案见下面练习 3）。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `encoder.write` 之前要先调 `.cuda()`？

**答案**：`cvcuda.Tensor` 类本身不暴露 `__cuda_array_interface__` 也不暴露 `__dlpack__`（这两个属性挂在 `.cuda()` 返回的 `ExternalBuffer` 上，见 [ExternalBuffer.cpp:L768-L770](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/ExternalBuffer.cpp#L768-L770)）。`nvimgcodec.as_image` 需要协议字段才能拿到指针与形状。

**练习 2**：这条链路里像素数据一共被复制了几次？

**答案**：0 次。解码结果在显存，`as_tensor` 只登记指针；`resize` 是一次显存到显存的计算（产生新缓冲，但这属于计算而非互操作搬运）；`.cuda()`/`as_image` 仍是零拷贝视图。跨越 CPU/GPU 边界的只有读文件的 JPEG 字节与写文件的 JPEG 字节。

**练习 3**：把 `as_tensor(image, "HWC")` 错标成 `"CHW"` 会怎样？

**答案**：包装不会报错——布局只是元数据标注，绑定层无法验证语义；但后续 `resize` 输入的形状解释变成 (C=H_orig, H=W_orig, W=C_orig)，轻则形状断言失败，重则产出错乱的图。互操作的正确性责任在调用者：**布局标注错了不会当场爆炸，只会在下游显形**。

### 4.3 模块三：cuda-python 互操作——亲手实现 `__cuda_array_interface__`

#### 4.3.1 概念说明

PyTorch、CuPy 这些框架已经替你实现了协议；cuda-python（NVIDIA 官方的 CUDA Runtime Python 绑定）则只给你最原始的 `cudaMalloc`/`cudaMemcpy`。这个模块的价值在于**祛魅**：`samples/interoperability/cuda_python_common.py` 里的 `CudaBuffer` 类用不到 50 行 Python 展示了「一个对象要成为 CV-CUDA 可包装的 GPU 数组」到底需要什么。读懂它，任何自研缓冲（比如你自己的 C++ 扩展返回的显存块）都能照方抓药接入 CV-CUDA。

#### 4.3.2 核心流程

`CudaBuffer` 的协议实现要素：

```
class CudaBuffer:
    shape、dtype、size          ← 元数据
    ptr: cudaMalloc 返回的设备指针
    __cuda_array_interface__ → {            ← 协议五要素
        "version":  3,
        "shape":    self.shape,
        "typestr":  self.dtype.str,        ← numpy 风格类型串，如 "<f4"
        "data":     (int(self.ptr), False),← (指针, 是否只读)
        "strides":  None,                  ← None = 紧凑行主序
    }
```

两个方向的用法：

- **外来 → cvcuda**（interop_1）：`cudaMalloc` + `cudaMemcpy(H2D)` 灌数据，`as_tensor(cuda_buffer)` 零拷贝纳管；
- **cvcuda → 外来**（interop_1 反向）：`CudaBuffer.from_cuda(t.cuda())` 从 CAI dict 里抠出指针再包一层。

#### 4.3.3 源码精读

**（1）协议属性本体**——[samples/interoperability/cuda_python_common.py:L60-L69](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/interoperability/cuda_python_common.py#L60-L69)：这就是一份完整的 CAI v3 声明——`data` 是二元组 `(指针整数, 只读标志)`，`strides: None` 声明紧凑布局（由绑定层按形状推导，见 4.1.3 之（4））。

**（2）分配与所有权**——[samples/interoperability/cuda_python_common.py:L38-L45](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/interoperability/cuda_python_common.py#L38-L45)：`ptr=None` 时用 `cudart.cudaMalloc` 分配并记 `owns_memory=True`；传入指针则只登记不持有。[L71-L74](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/interoperability/cuda_python_common.py#L71-L74) 的 `__del__` 在拥有所有权时 `cudaFree`——零拷贝体系里**谁分配谁释放**，CV-CUDA 包装层从不 free 外来指针（u2-l4 讲过引用链保活机制）。

**（3）反向包装**——[samples/interoperability/cuda_python_common.py:L47-L58](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/interoperability/cuda_python_common.py#L47-L58)：`from_cuda` 是个静态方法，接受「实现了 CAI 的对象」或「裸 CAI dict」，取出 `shape/typestr/data[0]` 三样重建 `CudaBuffer`（不拥有内存）。任何框架的 GPU 数组都能这样被「降维」成裸指针。

**（4）搬运工具**——[samples/interoperability/cuda_python_common.py:L80-L104](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/interoperability/cuda_python_common.py#L80-L104)：`cuda_memcpy_h2d` 先把「对象/dict/裸指针」三种输入统一归一成整数指针（L91-96），再调 `cudart.cudaMemcpy` 做 H2D；姊妹函数 `cuda_memcpy_d2h`（[L110-L134](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/interoperability/cuda_python_common.py#L110-L134)）方向相反。这两个函数是「只有裸 CUDA、没有框架」时最朴素的上/下板方式。

**（5）正向示例**——[samples/interoperability/cuda_python_interop_1.py:L36-L47](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/interoperability/cuda_python_interop_1.py#L36-L47)：numpy 造数据 → `CudaBuffer` 分配 → H2D → `as_tensor` 包装。[L53](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/interoperability/cuda_python_interop_1.py#L53) 演示反向：`CudaBuffer.from_cuda(cvcuda_tensor.cuda())`。

**（6）反方向示例**——[samples/interoperability/cuda_python_interop_2.py:L36-L44](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/interoperability/cuda_python_interop_2.py#L36-L44)：这里展示了另一种常见姿势——**让 CV-CUDA 来分配**（`cvcuda.Tensor((10,10,3), dtype=cvcuda.Type.U8)`），然后直接把 `t.cuda()` 当 CAI 对象传给 `cuda_memcpy_h2d` 灌数据。与 interop_1 相比，方向颠倒了：不是把外来缓冲包进来，而是把数据灌进 cvcuda 自己的缓冲。

#### 4.3.4 代码实践

1. **实践目标**：体验 stride 声明对互操作的影响，理解「stride 不连续」何时成为问题。
2. **操作步骤**：在 `CudaBuffer` 基础上（示例代码）：

```python
# 示例代码：非连续 stride 的 CudaBuffer 变体
import numpy as np, cvcuda
from cuda_python_common import CudaBuffer, cuda_memcpy_h2d

host = np.arange(6, dtype=np.float32).reshape(2, 3)
buf = CudaBuffer(host.shape, host.dtype)
cuda_memcpy_h2d(host, buf)

t = cvcuda.as_tensor(buf)
print("strides(元素):", t.strides)      # 紧凑布局应为 (3, 1)
```

   再构造一个「带 padding 行」的变体：分配 `(2, 4)` 的缓冲、只填前 3 列有效，把 `"strides": (16, 4)`（字节）填进 `__cuda_array_interface__`，重新包装观察 `t.strides`。
3. **需要观察的现象**：紧凑版 strides 为 `(3, 1)`（元素单位）；带 padding 版为 `(4, 1)`——CAI 的字节 stride 被绑定层换算成了元素 stride。
4. **预期结果**：把带 padding 的张量直接交给 `cvcuda.flip`，输出正确（算子按 stride 寻址，u2-l1 讲过 strided 语义）；但若把它喂给只接受紧凑输入的第三方库，可能报错或产出错图。另做负例：把某个字节 stride 故意设成非元素大小整数倍（如 float32 却填 `strides: (14, 4)`），应触发 `"Stride must be a multiple of the element size in bytes"`。
5. 报错文本**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`CudaBuffer` 的 `__cuda_array_interface__` 里为什么没有 `stream` 字段？没有它安全吗？

**答案**：v3 里 `stream` 缺省表示「生产者在 legacy 默认流上」（见 [ExternalBuffer.cpp:L103-L110](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/ExternalBuffer.cpp#L103-L110) 的注释）。示例里的 `cudaMemcpy` 是同步调用，写完才返回，数据天然可见，安全。若生产者用非阻塞流写缓冲，就该填真实 stream 句柄，否则 CV-CUDA 第一次读它时可能读到未写完的数据（u4-l1 的跨流安全机制）。

**练习 2**：`cuda_python_interop_1.py` 与 `_2.py` 各适合什么场景？

**答案**：`_1` 适合「显存已经有人管」的场景（比如 C++ 扩展分配的缓冲），CV-CUDA 只做零拷贝客人；`_2` 适合「数据在 CPU、想让 CV-CUDA 全程做主」的场景，张量由 CV-CUDA 分配，后续还能享受对象缓存复用（u4-l2），代价是一次显式 H2D 拷贝。

**练习 3**：`numpy_interop.py` 第 39 行写 `from cuda_python_interop_1 import CudaBuffer, ...`，但 `CudaBuffer` 明明定义在 `cuda_python_common.py` 里，为什么能工作？

**答案**：Python 的模块即命名空间。`cuda_python_interop_1.py` 自己 `from cuda_python_common import CudaBuffer, ...`（[L25](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/interoperability/cuda_python_interop_1.py#L25)），这些名字随之进入它的模块命名空间，于是可以被二次导入。这是转发导入，能跑但可读性差——正式代码应直接从定义模块导入。

### 4.4 模块四：计算框架互操作——PyCUDA、CuPy、PyTorch 的三种导回姿势

#### 4.4.1 概念说明

把 CV-CUDA 张量**交还**给计算框架时，三个框架展示了三种由浅入深的姿势：

1. **裸指针**（PyCUDA）：从 `t.cuda().__cuda_array_interface__["data"][0]` 抠出整数指针，手工喂给 `GPUArray` 构造器——最底层，能体会协议就是一份描述符；
2. **.asarray 直取**（CuPy）：`cupy.asarray(t.cuda())` 一行完成——cupy 原生理解 CAI；
3. **as_tensor + clone**（PyTorch）：`torch.as_tensor(t.cuda())` 零拷贝建视图，再 `.clone()` 断开共享——因为 torch 张量生命周期管理需要独立拥有缓冲时必须显式复制。

`numpy_interop.py` 则是四合一对照实验：同一份 CPU 数据分别经四种桥上 GPU，验证殊途同归。

#### 4.4.2 核心流程

```
cvcuda.Tensor t
   ├─(PyCUDA)─> GPUArray(shape=t.shape, dtype=t.dtype, gpudata=CAI["data"][0])   裸指针
   ├─(CuPy)───> cupy.asarray(t.cuda())                                          协议原生
   └─(torch)──> torch.as_tensor(t.cuda()) ──clone()──> 独立副本                  视图+显式复制
```

注意三个姿势的共同前提：`t.cuda()` 先导出 `ExternalBuffer`。三者的差异只在「对方框架怎么消化这份描述符」。

#### 4.4.3 源码精读

**（1）PyCUDA 裸指针往返**——[samples/interoperability/pycuda_interop.py:L35-L46](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/interoperability/pycuda_interop.py#L35-L46)：正向 `gpuarray.to_gpu(numpy_array)` 分配并上传、`as_tensor(pycuda_array)` 纳管（PyCUDA 的 GPUArray 实现了 CAI）；反向最精彩——`GPUArray` 构造时把 `gpudata` 指定为 CAI dict 里 `data` 元组的第 0 个元素（裸设备指针），shape/dtype 也从张量元数据抄过来，一个不持有内存的 PyCUDA 视图就诞生了。第 51 行 `.get()` 拷回 CPU 断言数据未变。

**（2）PyTorch 的 clone 纪律**——[samples/interoperability/pytorch_interop.py:L33-L45](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/interoperability/pytorch_interop.py#L33-L45)：`torch.as_tensor(cvcuda_tensor.cuda())` 建立零拷贝视图后，示例立刻 `.clone()` 并断言 `data_ptr()` 不同。注释写明原因：不 clone 的话所有张量共享同一块 GPU 缓冲，一方改写全体可见。这是零拷贝的代价——**共享即耦合**。

**（3）CuPy 一行流**——[samples/interoperability/cupy_interop.py:L33-L44](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/interoperability/cupy_interop.py#L33-L44)：`cupy.asarray(cvcuda_tensor.cuda())` 反向、`cvcuda.as_tensor(cupy_array)` 正向，都是框架原生支持 CAI/DLPack 的最短路径。

**（4）NumPy 四桥对照**——[samples/interoperability/numpy_interop.py:L32-L58](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/interoperability/numpy_interop.py#L32-L58)：`numpy_to_cvcuda_via_cuda_python`（另有 via_torch L61-81、via_cupy L84-104、via_pycuda L107-132）把同一份 `np.random.randn(10,10)` 数据分别经 cuda-python/torch/cupy/pycuda 上 GPU 再读回，逐一 `allclose` 断言。它证明互操作路径不影响数值正确性——四座桥搬运的是同一批比特。

#### 4.4.4 代码实践

1. **实践目标**：验证「PyCUDA 视图与 CV-CUDA 张量共享同一块缓冲」。
2. **操作步骤**（示例代码，需 GPU + pycuda）：

```python
# 示例代码：证明 pycuda 视图与 cvcuda 张量共享缓冲
import numpy as np, pycuda.autoinit, pycuda.gpuarray as gpuarray, cvcuda

src = np.arange(12, dtype=np.float32).reshape(3, 4)
gpu = gpuarray.to_gpu(src)
t = cvcuda.as_tensor(gpu)

flipped = cvcuda.flip(t, 0)                 # 上下翻转，写进新缓冲
print("pycuda 侧看到的还是原数据?", np.allclose(gpu.get(), src))   # True：flip 没动原缓冲

view = gpuarray.GPUArray(shape=t.shape, dtype=t.dtype,
                         gpudata=t.cuda().__cuda_array_interface__["data"][0])
view2 = gpuarray.GPUArray(shape=t.shape, dtype=t.dtype,
                          gpudata=flipped.cuda().__cuda_array_interface__["data"][0])
print("view 与 view2 指针相同?", view.ptr == view2.ptr)            # False：不同缓冲
```

3. **需要观察的现象**：allocating 变体的 `flip` 不改写输入缓冲；两个视图指针不同。
4. **预期结果**：把 `flip(t, 0)` 换成 `flip_into(t, t, 0)`（原地翻转），此时 `gpu.get()` 应直接等于翻转后的数据——因为 pycuda 视图、cvcuda 张量、dst 是同一块显存。这一步是零拷贝共享最直接的证据。**待本地验证**。
5. 若无 pycuda，把同样的逻辑换成 cupy（`cp.asarray(t.cuda())`）重复实验。

#### 4.4.5 小练习与答案

**练习 1**：PyCUDA 反向包装出的 `GPUArray` 会不会在析构时 free 掉 CV-CUDA 的显存？

**答案**：会出问题——`GPUArray(gpudata=...)` 这种构造方式不接管指针所有权，PyCUDA 不会 free 它；CV-CUDA 侧张量的生命周期由 Python 引用链管理（u2-l4）。但正因如此，**必须保持 cvcuda 张量对象存活**，否则悬垂指针。安全写法是把 `t` 和视图放在同一作用域，或像示例那样在同一段代码里用完即弃。

**练习 2**：为什么 torch 示例强调 clone 而 cupy 示例不 clone？

**答案**：两者技术上都可以零拷贝。差别在用法意图：cupy 示例只做只读校验（`allclose`），共享无害；torch 示例意在演示「如果你想独立拥有数据该怎么做」。零拷贝视图的写共享是真实风险，clone 是显式断开共享的手段（另见 u9-l1 里 TRT 推理前后张量独立分配的场景）。

**练习 3**：`numpy_interop.py` 里四种桥最终都调用 `cvcuda.as_tensor`，它们分别包的什么对象？

**答案**：cuda-python 桥包自研 `CudaBuffer`（手写 CAI）；torch 桥包 `torch.cuda.Tensor`（DLPack）；cupy 桥包 `cupy.ndarray`（CAI/DLPack 皆可）；pycuda 桥包 `pycuda.gpuarray.GPUArray`（CAI）。对象不同，协议相同——这就是「三座桥」抽象的意义。

### 4.5 模块五：pynvvideocodec 互操作——视频全 GPU 流水线与缓冲复用陷阱

#### 4.5.1 概念说明

PyNvVideoCodec 是 NVIDIA 视频编解码 SDK（NVDEC/NVENC）的 Python 绑定。本示例是全手册**最完整的一条全 GPU 视频流水线**：MP4 硬解（NVDEC）→ CV-CUDA 缩放 → CV-CUDA 色彩转换 → H.264 硬编（NVENC）。它同时演示了互操作里最凶险的正确性陷阱：**解码器复用内部缓冲**。零拷贝包装的张量指向解码器的内部缓冲，下一批帧一到，同一块缓冲就会被新帧覆盖——如果不赶在覆盖前把数据「落地」，先前包装的张量就会悄悄变成别人的帧。

#### 4.5.2 核心流程

```
MP4 文件
  │ SimpleDecoder(use_device_memory=True, output_color_type=RGB)
  v
get_batch_frames(10) ──> DecodedFrame 列表（内部缓冲，会被复用！）
  │ as_tensor(frame, "HWC")            零拷贝（危险窗口开启）
  │ cvcuda.resize(..., (480,640,3))    allocating 变体 → 新缓冲（数据落地）
  │ cvcuda.cvtcolor(RGB2YUV_NV12)      → (720,640,1) 新缓冲
  v
processed_frames: list[Tensor]         安全持有
  │ CreateEncoder(fmt="NV12", usecpuinputbuffer=False)
  │ encoder.Encode(frame)              NVENC 直接读显存
  v
H.264 文件
```

危险窗口只有从 `as_tensor` 到 `resize` 之间那一小段——`resize`/`cvtcolor` 是 allocating 变体，产出**独立的新缓冲**（u3-l3），数据一旦写入新缓冲就与解码器内部缓冲解耦。

#### 4.5.3 源码精读

**（1）解码器构造**——[samples/interoperability/pynvvideocodec_interop.py:L68-L76](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/interoperability/pynvvideocodec_interop.py#L68-L76)：`SimpleDecoder` 的关键参数——`gpu_id=0` 绑定设备，`output_color_type=RGB` 让 NVDEC 的 NV12 原始输出在硬件路径上转成 RGB，`use_device_memory=True` 保证像素留在显存，`max_width/max_height` 预留最大帧尺寸的缓冲池（正是复用机制的来源）。

**（2）陷阱的官方注释**——[samples/interoperability/pynvvideocodec_interop.py:L81-L83](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/interoperability/pynvvideocodec_interop.py#L81-L83)：示例作者直接把陷阱写进了注释——"PyNvVideoCodec will re-use the same buffers, so we cannot use zero-copy as_tensor and maintain the original data (without copying)"。这是读示例代码时最有价值的一类注释：它告诉你这个 API 的隐含契约。

**（3）逐帧处理循环**——[samples/interoperability/pynvvideocodec_interop.py:L85-L103](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/interoperability/pynvvideocodec_interop.py#L85-L103)：按 10 帧一批读取（L89-90）；每帧 `as_tensor(frame, "HWC")` 包装（L93）后**立即** `resize`（L98）与 `cvtcolor`（L99）落地，产出的 `nv12_frame` 追加进 `processed_frames`（L101）。L95-97 的注释再次强调：必须在读下一批之前处理完当前帧。注意 `resize` 的目标形状 `(480, 640, 3)` 是 HWC 顺序（高 480、宽 640）。

**（4）NV12 形状系数**——[python/mod_cvcuda/CvtColorUtil.cpp:L191-L199](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/CvtColorUtil.cpp#L191-L199)：`RGB2YUV_NV12` 等转换码的输出高度被推导为 `(3 * height) / 2`——这正是 2.3 节公式的源码落点。480 高的 RGB 帧进、720 高的 NV12 张量出（宽 640、通道 1）。

**（5）编码器构造与写出**——[samples/interoperability/pynvvideocodec_interop.py:L107-L124](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/interoperability/pynvvideocodec_interop.py#L107-L124)：`CreateEncoder` 指定 `fmt="NV12"`、`codec="h264"`、`usecpuinputbuffer=False`（喂显存而非主机内存）；`Encode(frame)` 逐帧吃 CV-CUDA 张量对象（PyNvVideoCodec 侧对这类缓冲对象做了适配，具体适配细节**待确认**——通用做法是喂实现了 CAI/DLPack 的 `frame.cuda()`）；编码完调 `EndEncode()` 冲出残余码流。无 NVENC 硬件时（L115-117）优雅降级跳过。

**（6）依赖容错**——[samples/interoperability/pynvvideocodec_interop.py:L29-L35](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/interoperability/pynvvideocodec_interop.py#L29-L35)：PyNvVideoCodec 只发布到 Python 3.12（依赖模板 L33），示例用 try/except 在 3.13+ 上打印提示后跳过——大型仓库示例对「可选依赖」的标准写法。

#### 4.5.4 代码实践

1. **实践目标**：不跑代码，凭源码推断输出规格；再（有 GPU 时）实测验证。
2. **操作步骤**：
   - 阅读循环体，回答：输入视频 1080p（1920×1080）、共 100 帧，`processed_frames` 里有多少个张量？每个的 shape/dtype 是什么？
   - 有 GPU + x86_64 + Python ≤3.12 时运行：

   ```bash
   cd samples && source venv_samples/bin/activate
   python3 interoperability/pynvvideocodec_interop.py
   ffprobe ../.cache/pexels-chiel-slotman-640x480.h264   # 或用 ffmpeg -i 查看
   ```

3. **需要观察的现象**：输出文件是 640×480 的 H.264 裸流（Annex-B，无封装容器）；分辨率与 `resize` 的目标一致。
4. **预期结果**：100 个张量，每个 `(720, 640, 1)`、dtype U8——480 的 3/2 是 720，NV12 的 UV 平面拼在 Y 平面下方。
5. 运行结果**待本地验证**（本环境无 GPU、无 PyNvVideoCodec）。

#### 4.5.5 小练习与答案

**练习 1**：若把循环改成「先把所有帧都 `as_tensor` 存进列表，循环结束后再统一 resize」，会发生什么？

**答案**：全部废掉。解码器复用内部缓冲，第一批的 10 个张量指向的显存在读第二批时被覆盖——列表里所有张量最终都显示同一批（最后一批）的内容。示例注释 L82-83 明说零拷贝与「保持原始数据」不可兼得。正确做法要么逐帧即时落地（示例做法），要么解码后立即 `clone`/`resize`。

**练习 2**：为什么编码器选 NV12 而不是继续用 RGB？

**答案**：NVENC 的原生输入是 YUV 家族（通常 4:2:0 子采样），NV12 是其中最通用的双平面交错格式。用 RGB 喂 NVENC 需要库内部再转一次格式，白费带宽；示例在 GPU 上用 `cvtcolor(RGB2YUV_NV12)` 一次算到位，正符合 u3-l4 的融合思想——把格式转换安排在编码器最舒服的交界处。

**练习 3**：这条流水线里，哪几步在 CPU 上执行？

**答案**：读 MP4 容器字节、写 H.264 码流字节（`f.write`），以及 CPU 侧的循环控制与 `print`。像素数据的所有变换（解码输出、RGB 转换、缩放、色彩转换、编码读取）都在 GPU。这与 u1-l2 hello_world 的「数据落点」分析方法一致。

## 5. 综合实践

**任务**：亲手组装一条「nvimgcodec 解码 → cvcuda.resize + cvtcolor → 编码保存」的最小全 GPU 流水线，然后做一次故意的失败实验。

### 步骤一：最小流水线

参照 `nvimgcodec_interop.py`（4.2 节）与 `pynvvideocodec_interop.py`（4.5 节）的写法，编写（示例代码，非仓库文件）：

```python
# 示例代码：decode → resize + cvtcolor → encode 最小全 GPU 流水线
from pathlib import Path
import cvcuda
from nvidia import nvimgcodec

root     = Path(__file__).resolve().parents[2]          # 仓库根，按实际位置调整
img_path = root / "samples" / "assets" / "images" / "tabby_tiger_cat.jpg"
out_path = root / ".cache" / "tabby_240_320_bgr.jpg"
out_path.parent.mkdir(parents=True, exist_ok=True)

decoder, encoder = nvimgcodec.Decoder(), nvimgcodec.Encoder()

# 1) GPU 解码 + 零拷贝纳管（HWC 标注）
image = decoder.read(str(img_path))
t = cvcuda.as_tensor(image, "HWC")
print("解码:", t.shape, t.dtype)

# 2) GPU 缩放（高 240、宽 320）与通道重排
resized = cvcuda.resize(t, (240, 320, 3), cvcuda.Interp.LINEAR)
bgr     = cvcuda.cvtcolor(resized, cvcuda.ColorConversion.RGB2BGR)
print("处理:", bgr.shape, bgr.dtype)

# 3) 零拷贝交回 nvimgcodec 编码
encoder.write(str(out_path), nvimgcodec.as_image(bgr.cuda()))
print("已写出:", out_path)
```

**观察点**：两次 `print` 应分别输出解码尺寸（如 `(675, 1200, 3) uint8`）与 `(240, 320, 3) uint8`；输出图是 240×320 的 BGR（JPEG 存储时会按编码器规范处理，视觉上与 RGB 版本仅红蓝互换）。把 `RGB2BGR` 换成 `RGB2YUV_NV12` 再跑一次，第二步的形状应变成 `(360, 320, 1)`——3/2 系数亲测。

### 步骤二：CPU 数组包装失败实验

```python
# 示例代码：两类典型失败
import numpy as np, cvcuda

cpu = np.zeros((4, 4, 3), dtype=np.uint8)
try:
    cvcuda.as_tensor(cpu, "HWC")
except Exception as e:
    print(f"[numpy CPU] {type(e).__name__}: {e}")

try:
    cvcuda.as_tensor([1, 2, 3], "HWC")
except Exception as e:
    print(f"[list] {type(e).__name__}: {e}")
```

**需要记录的现象**（依据 4.1.3 的源码推理，文本**待本地验证**）：

| 输入 | 预期异常 | 源码依据 |
|------|----------|----------|
| CPU numpy 数组（有 `__dlpack__`，设备 `kDLCPU`） | `RuntimeError: Only CUDA-accessible memory buffers can be wrapped` | [ExternalBuffer.cpp:L598-L607](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/ExternalBuffer.cpp#L598-L607) + [DLPackUtils.cpp:L280-L291](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/DLPackUtils.cpp#L280-L291) |
| Python list（无任何协议） | `TypeError: incompatible function arguments ...` | [ExternalBuffer.cpp:L517-L536](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/ExternalBuffer.cpp#L517-L536) `load` 返回 false |

**解释**：numpy 2.x 数组实现了 DLPack 导出，于是走进了 DLPack 分支，但 `__dlpack_device__` 报告的是 CPU 设备，被 `IsCudaAccessible` 白名单（仅 `kDLCUDA`/`kDLCUDAHost`/`kDLCUDAManaged`）拒绝；list 连协议都没有，pybind11 的类型转换直接失败。两类报错的**类型**不同（RuntimeError vs TypeError），这本身就是判断「走没走进桥」的诊断线索。

### 步骤三（选做）：换一座桥

把步骤一中的解码端换成 `cuda_python_interop_2.py` 的姿势——用 `cvcuda.Tensor((H, W, 3), dtype=cvcuda.Type.U8)` 自己分配、`cuda_memcpy_h2d` 灌一张 CPU 图，再走同样的 resize + cvtcolor + 编码。对比两条路：nvimgcodec 路 CPU 只经手压缩字节；cuda-python 路要经手完整像素（H2D 拷贝）。用大图（≥4K）计时对比两种入板方式的耗时差，量化「解码直进显存」的收益。**待本地验证**。

## 6. 本讲小结

- **一套对称 API**：「别人 → cvcuda」用 `as_tensor`（CAI 优先、DLPack 兜底），「cvcuda → 别人」用 `tensor.cuda()`（导出双协议 `ExternalBuffer`，不搬数据）；`cvcuda.Tensor` 类本身不暴露协议属性。
- **设备与 stride 两道闸**：设备白名单只放行 `kDLCUDA`/`kDLCUDAHost`/`kDLCUDAManaged`（CPU 数组在 DLPack 分支被拒、报 RuntimeError）；CAI 的字节 stride 必须被元素大小整除（否则报 stride 错误）；指针实址还要过 `cudaPointerGetAttributes` 复核。
- **三个层次的导回姿势**：PyCUDA 抠裸指针（`gpudata=CAI["data"][0]`）、CuPy `asarray` 直取、PyTorch `as_tensor` 后按需 `clone` 断开共享——零拷贝意味着共享即耦合。
- **编解码器是全 GPU 管线的端点**：nvimgcodec 负责图像（解码直进显存、编码直读显存），PyNvVideoCodec 把 NVDEC/NVENC 串成视频闭环，中间像素永不落地。
- **最大陷阱是缓冲复用**：PyNvVideoCodec 解码器复用内部缓冲，零拷贝包装的张量必须在读下一批帧之前用 allocating 算子「落地」，否则数据被静默覆盖。
- **NV12 的张量表示**是高度拼接的 \((\frac{3}{2}H,\ W,\ 1)\) U8，3/2 系数的推导在 `CvtColorUtil.cpp` 源码中一目了然。

## 7. 下一步学习建议

- **下一讲（u9-l4）**：总结与进阶路线——把全手册的架构取舍（C ABI 稳定性、变长批抽象、Python 缓存策略）串成一张知识地图，并整理你的个人速查手册。本讲的 8 个示例正是那张地图里「生态集成」边的全部官方注脚。
- **继续阅读的源码**：若想深挖绑定层，回到 u2-l4 精读过的 [ExternalBuffer.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/ExternalBuffer.cpp) 重读 L103-L207 的两段 stream 解析（CAI `stream` 字段与 DLPack `stream` 实参），把本讲的协议视图与 u4-l1 的流模型对接。
- **动手方向**：把你常用的数据源（自研 C++ 扩展、DLStreamer、DeepStream）按 4.3 节 `CudaBuffer` 的模子写一个最小 CAI 适配器；再用 u7-l4 的 Nsight Systems 时间线验证你的流水线里 CPU↔GPU 边界穿越只剩压缩字节流。
