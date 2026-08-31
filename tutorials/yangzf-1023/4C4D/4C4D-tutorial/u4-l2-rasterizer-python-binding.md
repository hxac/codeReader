# 光栅化的 Python 绑定与 markVisible

## 1. 本讲目标

上一讲（u4-l1）我们把 `render()` 当成一个「参数组装器」读完了：它把相机几何量、高斯属性和 `pipe` 开关打包进 `GaussianRasterizationSettings`，然后交给 `rasterizer(...)` 就结束了。本讲往下钻一层，回答三个问题：

1. 当 `render()` 调用 `rasterizer(...)` 之后，数据是怎样穿过 Python、C++、CUDA 三层边界，最终到达 GPU kernel 的？这靠的是 `ext.cpp` 里 pybind11 注册的一张「函数登记表」。
2. 为什么整条链路必须包在一个 `torch.autograd.Function`（即 `_RasterizeGaussians`）里？前向 29 个参数、反向 36 个参数的「大元组」各自如何组织？
3. `markVisible` 是什么？它输出的掩码是什么类型、在 4C4D 的 opacity decay 中扮演什么角色？

学完本讲，你应该能独立追踪任意一个张量（比如 `opacities`）从 `render()` 到 CUDA 函数的完整传递路径，并且能看懂 `in_frustum` 的视锥剔除逻辑。

## 2. 前置知识

### 2.1 torch.autograd.Function：自定义可微操作

PyTorch 的自动微分默认能处理加、减、卷积这类「已有梯度定义」的操作。但可微光栅化是一个手写 CUDA 程序，PyTorch 不认识它。`torch.autograd.Function` 就是扩展接口：你写一个类，提供 `forward`（怎么算输出）和 `backward`（给了输出梯度，怎么算输入梯度），PyTorch 就把这两半接进自动微分图。调用方式不是直接调 `forward`，而是调 `MyFunction.apply(...)`——`apply` 负责建图，训练时 `loss.backward()` 会自动触发你写的 `backward`。

两个关键机制：

- **ctx（上下文）**：`forward(ctx, *args)` 的第一个参数，用 `ctx.save_for_backward(...)` 存起来的张量，会在 `backward(ctx, *grads)` 里通过 `ctx.saved_tensors` 原样取回。
- **梯度元组对齐**：`backward` 返回的梯度元组必须与 `forward` 的输入参数**一一对应、顺序相同**；对不需要梯度的输入（如配置对象）返回 `None`。

### 2.2 pybind11：C++ 函数的「登记处」

pybind11 是一个只含头文件的 C++ 库，`PYBIND11_MODULE(模块名, m)` 宏在编译出的 `.so` 里生成 Python 模块，`m.def("python 名", &C++ 函数)` 把一个 C++ 函数登记成 Python 可调用的函数。Python 侧 `import` 时看到的函数名、参数顺序，完全由这张登记表决定。

### 2.3 torch::Tensor 与裸内存

C++ 侧拿到 `torch::Tensor` 后，`.contiguous().data<float>()` 把它变成一个指向连续显存的裸 `float*` 指针——这是传给 CUDA kernel 的标准形态。`.contiguous()` 保证内存按行优先连续排列，`.data<T>()` 指定元素类型。反向追踪时，看到 `.data<float>()` 就意味着「从此进入 CUDA 世界」。

### 2.4 视锥（frustum）与近裁剪面

相机只能看到前方一个棱锥范围内的物体，这个棱锥叫视锥。`z` 是点在相机坐标系下的深度：`z <= 0` 表示点在相机背后（永远不可见）；`z` 太小表示点几乎贴着镜头，投影会数值爆炸。所以最快的剔除就是「深度检查」。更精细的左右上下边界检查（投影坐标是否超出 `[-1, 1]`）代价更高，可以留给后续流程。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
|---|---|---|
| `gaussian_renderer/__init__.py` | 渲染入口 `render()` | 调用 `rasterizer(...)` 与 `rasterizer.markVisible(...)` 的两处 |
| `diff-gaussian-rasterization/diff_gaussian_rasterization/__init__.py` | Python 包装层 | `_RasterizeGaussians`、`GaussianRasterizer.markVisible` |
| `diff-gaussian-rasterization/ext.cpp` | pybind11 登记表 | 三个函数如何注册给 Python |
| `diff-gaussian-rasterization/rasterize_points.h` | C++ 声明 | 三个包装函数的签名 |
| `diff-gaussian-rasterization/rasterize_points.cu` | C++ 实现 | 张量解包、转发给 `CudaRasterizer::Rasterizer` |
| `diff-gaussian-rasterization/cuda_rasterizer/rasterizer_impl.cu` | CUDA 启动层 | `checkFrustum` kernel 与 `Rasterizer::markVisible` |
| `diff-gaussian-rasterization/cuda_rasterizer/auxiliary.h` | 设备端工具 | `in_frustum` 的剔除判据 |
| `diff-gaussian-rasterization/setup.py` | 构建脚本 | `_C` 模块名与源文件清单 |

## 4. 核心概念与源码讲解

### 4.1 ext.cpp 绑定层次：从 import _C 到 CUDA kernel

#### 4.1.1 概念说明

「绑定（binding）」指把底层 C++/CUDA 代码暴露给 Python 的那层胶水。4C4D 的光栅化链路共有五层：

```text
第 1 层  Python 业务层      gaussian_renderer/__init__.py 的 render()
第 2 层  Python 包装层      diff_gaussian_rasterization/__init__.py
                              ├─ GaussianRasterizer.forward → rasterize_gaussians
                              ├─ _RasterizeGaussians (autograd.Function)
                              └─ GaussianRasterizer.markVisible
第 3 层  pybind 登记表      ext.cpp 的 PYBIND11_MODULE（编译进 .so）
第 4 层  C++ 包装函数       rasterize_points.cu 的 RasterizeGaussiansCUDA 等
第 5 层  纯 CUDA 实现       cuda_rasterizer/ 下的 Rasterizer::forward / markVisible
```

理解这张分层图的意义在于**排错定位**：一个 `CUDA error` 报错栈通常只从第 4 层开始显示，你必须知道第 2、3 层各自做了什么参数重排，才能把错误对应回 Python 侧的某个输入。

#### 4.1.2 核心流程

以一次渲染调用为例：

1. Python 侧 `import diff_gaussian_rasterization` 时，包内的 `from . import _C`（[diff-gaussian-rasterization/diff_gaussian_rasterization/__init__.py:15](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/diff_gaussian_rasterization/__init__.py#L15)）加载编译产物 `_C.<平台>.so`。
2. `.so` 里的模块内容由 `ext.cpp` 的登记表决定，共登记三个函数。
3. C++ 包装函数（`rasterize_points.cu`）做三件事：校验张量形状、分配输出张量、把张量转成裸指针转发给第 5 层。
4. 第 5 层不知道 `torch::Tensor` 的存在，只操作裸指针，因此可以独立编译与优化。

#### 4.1.3 源码精读

**登记表本体**。整个绑定只有 5 行——把两个 C++ 函数名各映射成一个 Python 名：

[diff-gaussian-rasterization/ext.cpp:15-19](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/ext.cpp#L15-L19)

```cpp
PYBIND11_MODULE(TORCH_EXTENSION_NAME, m) {
  m.def("rasterize_gaussians", &RasterizeGaussiansCUDA);
  m.def("rasterize_gaussians_backward", &RasterizeGaussiansBackwardCUDA);
  m.def("mark_visible", &markVisible);
}
```

这段代码把 `RasterizeGaussiansCUDA` 注册为 Python 名 `rasterize_gaussians`（前向）、`RasterizeGaussiansBackwardCUDA` 注册为 `rasterize_gaussians_backward`（反向）、`markVisible` 注册为 `mark_visible`（视锥查询）。注意命名约定的差异：C++ 侧用驼峰，Python 侧用蛇形，且 Python 名不带 `CUDA` 后缀——追踪调用时按名字对不上是常见困扰。

`TORCH_EXTENSION_NAME` 是编译期宏，其值来自构建脚本中声明的模块名：

[diff-gaussian-rasterization/setup.py:20-29](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/setup.py#L20-L29)

```python
CUDAExtension(
    name="diff_gaussian_rasterization._C",
    sources=[
    "cuda_rasterizer/rasterizer_impl.cu",
    "cuda_rasterizer/forward.cu",
    "cuda_rasterizer/backward.cu",
    "rasterize_points.cu",
    "ext.cpp"],
    ...
```

这里声明模块全名是 `diff_gaussian_rasterization._C`，与五个源文件一起编译。也就是说：第 3、4、5 层的代码被编进**同一个** `.so`，`ext.cpp` 只是这个 `.so` 对 Python 露出的门面。

**C++ 声明与实现的分离**。签名在头文件中声明（如 [diff-gaussian-rasterization/rasterize_points.h:89-93](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/rasterize_points.h#L89-L93) 声明 `markVisible`），实现写在 `rasterize_points.cu`。头文件是「参数总清单」——想知道 Python 传进来的张量在 C++ 侧叫什么、是什么类型，读头文件最快。

**C++ 包装函数的「薄壳」模式**。看前向包装的开头与 CUDA 调用点：

[diff-gaussian-rasterization/rasterize_points.cu:36-77](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/rasterize_points.cu#L36-L77)

```cpp
std::tuple<int, torch::Tensor, ..., torch::Tensor>
RasterizeGaussiansCUDA(
    const torch::Tensor& background,
    const torch::Tensor& means3D,
    const torch::Tensor& colors,
    ...
{
  if (means3D.ndimension() != 2 || means3D.size(1) != 3) {
    AT_ERROR("means3D must have dimensions (num_points, 3)");
  }
  const int P = means3D.size(0);
```

它先做唯一一处形状校验（`means3D` 必须是 `(N, 3)`），随后为输出分配张量、把每个输入转成裸指针：

[diff-gaussian-rasterization/rasterize_points.cu:104-140](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/rasterize_points.cu#L104-L140)

```cpp
rendered = CudaRasterizer::Rasterizer::forward(
    geomFunc, binningFunc, imgFunc,
    P, degree, degree_t, M,
    background.contiguous().data<float>(),
    ...
    opacity.contiguous().data<float>(),
    ts.contiguous().data_ptr<float>(),
    scales.contiguous().data_ptr<float>(),
    ...
```

注意一个细节：这份转发参数表里**没有 means2D**——屏幕坐标在 CUDA 内部重新计算（第 5 层的 `GeometryState::means2D`），Python 传进来的 `means2D` 从未进入前向 CUDA，它纯粹是反向梯度的载体（呼应 u4-l1 的「梯度容器」结论，这里给出代码证据）。

#### 4.1.4 代码实践

**实践目标**：亲手验证绑定表的内容，确认 Python 侧只暴露三个函数。

**操作步骤**：

1. 在已编译环境（参考 u1-l2 的安装步骤）中执行：

```bash
python -c "from diff_gaussian_rasterization import _C; print([n for n in dir(_C) if not n.startswith('_')])"
```

2. 对照 `ext.cpp` 的三行 `m.def`，确认输出恰好是 `rasterize_gaussians`、`rasterize_gaussians_backward`、`mark_visible` 三个名字。
3. 再执行 `python -c "from diff_gaussian_rasterization import _C; help(_C.rasterize_gaussians)"`，把打印出的参数签名与 `rasterize_points.h` 的声明逐个对齐。

**需要观察的现象**：`dir(_C)` 里除了三个登记的函数，还会有 `__doc__` 等模块属性（所以步骤 1 过滤了下划线开头的名字）。

**预期结果**：三个函数名与 `ext.cpp` 完全一致；`help` 显示的参数个数与 `RasterizeGaussiansCUDA` 声明的 29 个参数一致。无 GPU 环境下 `import` 仍可能成功（加载 `.so` 不需要 GPU），但调用会失败——本步骤的验证只依赖 import，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么第 5 层（`cuda_rasterizer/`）刻意不使用 `torch::Tensor`，而要第 4 层做转换？

**答案**：解耦与编译自由度。`cuda_rasterizer/` 只操作裸指针和原始数组，不依赖 PyTorch 的 ABI，可以独立调整内存布局、kernel 划分而不动绑定层；同时 `torch/extension.h` 编译很慢，纯 CUDA 文件不含它能显著缩短增量编译时间。这也解释了 `setup.py` 为什么把两类源文件分开列出却编进同一个 `.so`。

**练习 2**：如果我想给光栅化器新增一个 Python 可调用的接口（例如查询每个高斯覆盖的 tile 数），需要在哪几层各加什么？

**答案**：四处。① 第 5 层 `cuda_rasterizer/rasterizer_impl.cu` 写 kernel 与启动函数；② 第 4 层 `rasterize_points.h` 加声明、`rasterize_points.cu` 加张量解包实现；③ 第 3 层 `ext.cpp` 加一行 `m.def("新名字", &新函数)`；④ 第 2 层 Python 包装类（如 `GaussianRasterizer`）加一个方法调用 `_C.新名字`。之后重新 `pip install -e ./diff-gaussian-rasterization` 编译。

**练习 3**：`m.def("rasterize_gaussians", &RasterizeGaussiansCUDA)` 中两个名字分别在哪里被使用？

**答案**：`RasterizeGaussiansCUDA` 是 C++ 侧的函数符号，只在 `rasterize_points.cu/h` 中出现；`rasterize_gaussians` 是 Python 侧属性名，被 `_RasterizeGaussians.forward` 里的 `_C.rasterize_gaussians(*args)` 调用。跨语言追踪时要用这张映射表换名字。

### 4.2 _RasterizeGaussians：autograd.Function 的前向与反向

#### 4.2.1 概念说明

`_RasterizeGaussians` 是整个项目里最重要的一个类：它是 PyTorch 自动微分世界与手写 CUDA 光栅化世界之间**唯一的桥**。它要解决两个问题：

1. **前向**：把 Python 侧 13 个参数（12 个张量 + 1 个配置对象）重排成 C++ 期望的 29 个扁平参数，调用 CUDA，拿到 11 个返回值。
2. **反向**：把 6 个输出的上游梯度，连同前向缓存的 16 个张量，重排成 C++ 反向函数期望的 36 个参数，调用 CUDA 反向，把 12 个梯度按 forward 输入顺序重新排列返回。

「重排」是这里的主题词——autograd.Function 本身不做任何数值计算，它是一个**参数编舞师**。

#### 4.2.2 核心流程

```text
训练循环                      _RasterizeGaussians                    CUDA
────────                      ───────────────────                    ────
render() 组装输入
  └─ rasterizer(...)
       └─ rasterize_gaussians(...)
            └─ _RasterizeGaussians.apply(12 张量 + settings)
                 ├─ forward(ctx, ...):
                 │    ① 展平 settings → 29 元组 args
                 │    ② _C.rasterize_gaussians(*args) ──────────► RasterizeGaussiansCUDA
                 │    ③ ctx.save_for_backward(16 个张量)          (第 4 层)
                 │    ④ 返回 (color, radii, depth, 1-T, flow, covs_com)
                 ▼
loss = L1 + λ·SSIM(rendered_image, gt)
                 │
loss.backward()
                 ├─ backward(ctx, 6 个上游梯度):
                 │    ① 取回 ctx.saved_tensors
                 │    ② 拼 36 元组 args
                 │    ③ _C.rasterize_gaussians_backward(*args) ──► RasterizeGaussiansBackwardCUDA
                 │    ④ 12 个梯度重排成 13 元组（末尾 None）        (第 4 层)
                 │    ⑤ 梯度流入 means3D/opacity/scales/... 的 .grad
                 ▼
optimizer.step()
```

#### 4.2.3 源码精读

**入口只有 apply**。外部永不直接调 `forward`：

[diff-gaussian-rasterization/diff_gaussian_rasterization/__init__.py:21-50](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/diff_gaussian_rasterization/__init__.py#L21-L50)

```python
def rasterize_gaussians(means3D, means2D, sh, colors_precomp, flow_2d,
                        opacities, ts, scales, scales_t, rotations,
                        rotations_r, cov3Ds_precomp, raster_settings):
    return _RasterizeGaussians.apply(
        means3D, means2D, sh, colors_precomp, flow_2d, opacities,
        ts, scales, scales_t, rotations, rotations_r,
        cov3Ds_precomp, raster_settings)
```

`GaussianRasterizer.forward`（[同文件:242-295](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/diff_gaussian_rasterization/__init__.py#L242-L295)）在做完「SH 与预计算颜色二选一」「尺度旋转与预计算协方差二选一」等互斥检查后，把未提供的参数替换为空张量，再调用本函数。这就是为什么 CUDA 侧要用 `sh.size(0) != 0` 之类的判断来区分「空」与「有值」。

**前向：展开 settings，调用 CUDA，缓存反向所需**：

[diff-gaussian-rasterization/diff_gaussian_rasterization/__init__.py:71-102](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/diff_gaussian_rasterization/__init__.py#L71-L102)

```python
# Restructure arguments the way that the C++ lib expects them
args = (
    raster_settings.bg,
    means3D,
    colors_precomp,
    flow_2d,
    opacities,
    ts,
    scales,
    scales_t,
    rotations,
    rotations_r,
    raster_settings.scale_modifier,
    cov3Ds_precomp,
    raster_settings.viewmatrix,
    ...  # projmatrix、tanfovx、tanfovy、宽高、sh、两个 sh 阶数、
         # campos、timestamp、time_duration、rot_4d、gaussian_dim、
         # force_sh_3d、prefiltered、debug，共 29 个元素
)
```

注意重排规律：**动态张量在前**（按 CUDA 声明顺序），**`raster_settings` 的字段被逐个拍平穿插其中**。`GaussianRasterizationSettings` 是个 `NamedTuple`（[同文件:206-224](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/diff_gaussian_rasterization/__init__.py#L206-L224)），本身不可变也不含梯度，所以必须拆开传。

调用与缓存：

[diff-gaussian-rasterization/diff_gaussian_rasterization/__init__.py:104-122](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/diff_gaussian_rasterization/__init__.py#L104-L122)

```python
# Invoke C++/CUDA rasterizer
if raster_settings.debug:
    cpu_args = cpu_deep_copy_tuple(args)  # Copy them before they can be corrupted
    try:
        num_rendered, color, flow, depth, T, radii, geomBuffer, binningBuffer, \
            imgBuffer, covs_com, out_means3D = _C.rasterize_gaussians(*args)
    except Exception as ex:
        torch.save(cpu_args, "snapshot_fw.dump")
        ...
else:
    num_rendered, color, flow, depth, T, radii, geomBuffer, binningBuffer, \
        imgBuffer, covs_com, out_means3D = _C.rasterize_gaussians(*args)

ctx.raster_settings = raster_settings
ctx.num_rendered = num_rendered
ctx.save_for_backward(colors_precomp, means3D, out_means3D, scales, rotations,
                       cov3Ds_precomp, radii, sh, flow_2d, opacities, ts,
                       scales_t, rotations_r,
                       geomBuffer, binningBuffer, imgBuffer)
return color, radii, depth, 1-T, flow, covs_com
```

四个值得停下来的点：

1. **debug 模式的快照机制**：`cpu_deep_copy_tuple`（[同文件:17-19](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/diff_gaussian_rasterization/__init__.py#L17-L19)）先把所有参数拷到 CPU，一旦 CUDA 抛异常就把这份「犯罪现场」存成 `snapshot_fw.dump`，可以事后单独重放定位。代价是每次调用多一份全量拷贝，所以只在 `pipe.debug` 时启用。
2. **三个不透明字节缓冲**：`geomBuffer / binningBuffer / imgBuffer` 是前向在 GPU 上分配的原始字节张量（见 [rasterize_points.cu:86-93](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/rasterize_points.cu#L86-L93)），里面装着几何状态、tile 排序状态、逐像素状态。Python 不解释其内容，只是**原样持有、原样交还**给反向——这是跨层传递大规模中间状态的经典手法：不暴露结构，只传递句柄。
3. **`out_means3D`**：C++ 侧 `torch::Tensor out_means3D = means3D.clone();`（[rasterize_points.cu:84](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/rasterize_points.cu#L84)）克隆一份位置，CUDA 前向在渲染时刻 τ 把时间均值偏移（u3-l3 的 mean_offset）写进这份克隆，反向就基于**修正后的位置**求梯度（[rasterize_points.cu:217-218](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/rasterize_points.cu#L217-L218) 处可以看到被注释掉的 `means3D` 与实际使用的 `out_means3D` 相邻）。
4. **`1-T`**：CUDA 返回的是透射率 `T`（光线穿过所有高斯后剩余的能量），Python 立刻换成 alpha（不透明度遮罩）`1-T` 返回，正好是 `render()` 返回 dict 里的 `"alpha"`，供 env_map 背景混合使用（u4-l3 会用到）。

**反向：六进十三出**：

[diff-gaussian-rasterization/diff_gaussian_rasterization/__init__.py:124-132](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/diff_gaussian_rasterization/__init__.py#L124-L132)

```python
@staticmethod
def backward(ctx, grad_out_color, grad_radii, grad_depth,
             grad_alpha, grad_flow, grad_covs_com):
    num_rendered = ctx.num_rendered
    raster_settings = ctx.raster_settings
    (colors_precomp, means3D, out_means3D, scales, rotations, cov3Ds_precomp,
     radii, sh, flow_2d, opacities, ts, scales_t, rotations_r,
     geomBuffer, binningBuffer, imgBuffer) = ctx.saved_tensors
```

`backward` 的 6 个入参顺序 = `forward` 的 6 个返回值顺序。训练中损失通常只依赖 `rendered_image`，所以 `grad_radii/grad_depth/grad_alpha/grad_flow/grad_covs_com` 一般是零张量，但它们仍会被传进 CUDA（[同文件:135-170](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/diff_gaussian_rasterization/__init__.py#L135-L170) 把它们与 `dL_dout_color` 一起拼进 36 元组，其中的 `grad_out_color/grad_depth/grad_alpha/grad_flow` 对应 C++ 声明里的 `dL_dout_*` 系列）。

CUDA 反向返回 12 个梯度，Python 侧重排：

[diff-gaussian-rasterization/diff_gaussian_rasterization/__init__.py:188-204](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/diff_gaussian_rasterization/__init__.py#L188-L204)

```python
grads = (
    grad_means3D,        # ← forward 第 1 参 means3D
    grad_means2D,        # ← forward 第 2 参 means2D（梯度容器）
    grad_sh,             # ← 第 3 参 sh
    grad_colors_precomp, # ← 第 4 参 colors_precomp
    grad_flows,          # ← 第 5 参 flow_2d
    grad_opacities,      # ← 第 6 参 opacities
    grad_ts,             # ← 第 7 参 ts（时间中心梯度，供致密化统计）
    grad_scales,
    grad_scales_t,
    grad_rotations,
    grad_rotations_r,
    grad_cov3Ds_precomp,
    None,                # ← 第 13 参 raster_settings：非张量，无梯度
)
return grads
```

CUDA 返回顺序是 `(grad_means2D, grad_colors_precomp, grad_opacities, grad_means3D, ...)`（注意 means2D 在前），而这里按 forward 输入顺序换成了 means3D 在前——**顺序不能照抄**，这是编写 autograd.Function 最容易出错的地方。末尾的 `None` 对应 `raster_settings`，让 autograd 知道配置对象不需要梯度。

反向的 C++ 侧则把所有梯度张量预分配为零（[rasterize_points.cu:198-210](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/rasterize_points.cu#L198-L210)），交给 `Rasterizer::backward` 填充后原样返回。

#### 4.2.4 代码实践

**实践目标**：完成本讲规定的核心任务——追踪 `opacities` 张量从 `render()` 到 `_C.rasterize_gaussians` 的完整路径，并画成传递图。

**操作步骤**（纯源码阅读，无需 GPU）：

1. 起点，`render()` 取出激活后的不透明度：[gaussian_renderer/__init__.py:61](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L61) `opacity = pc.get_opacity`（sigmoid 后的 `(N,1)` 张量）。
2. 可能被改写一：opacity decay 分支 [gaussian_renderer/__init__.py:64-75](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L64-L75)，`opacity = pc.opacity_decay(...)`。
3. 可能被改写二：Python 协方差回退分支 [gaussian_renderer/__init__.py:91-93](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L91-L93)，`opacity = opacity * marginal_t`（仅 `pipe.compute_cov3D_python` 时）。
4. 关键字参数传给光栅化器：[gaussian_renderer/__init__.py:160-172](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L160-L172) 中的 `opacities=opacity`。
5. 进入包装层 `GaussianRasterizer.forward`，位置参数化后转调：[diff-gaussian-rasterization/diff_gaussian_rasterization/__init__.py:281-295](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/diff_gaussian_rasterization/__init__.py#L281-L295)。
6. 经 `rasterize_gaussians` 到 `apply`（[同文件:36-50](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/diff_gaussian_rasterization/__init__.py#L36-L50)）。
7. 在 forward 的 29 元组里排第 5 位：[同文件:77](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/diff_gaussian_rasterization/__init__.py#L77)。
8. 跨越语言边界：[ext.cpp:16](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/ext.cpp#L16) 进入 `RasterizeGaussiansCUDA`，对应形参 `opacity`（[rasterize_points.cu:42](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/rasterize_points.cu#L42)）。
9. 化为裸指针进入 CUDA：[rasterize_points.cu:116](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/rasterize_points.cu#L116) `opacity.contiguous().data<float>()`，此后由 `CudaRasterizer::Rasterizer::forward` 消费。

把以上 9 步画成如下传递图（建议手绘或用 mermaid 补全）：

```text
pc._opacity ──get_opacity(sigmoid)──► opacity (N,1)
  │
  ├─[opacity_decay 开启] opacity_decay() 改写
  ├─[compute_cov3D_python] ×marginal_t 改写
  ▼
render(): opacities=opacity
  ▼ GaussianRasterizer.forward (:281)
  ▼ rasterize_gaussians (:36 apply)
  ▼ _RasterizeGaussians.forward args[5] (:77)
  ▼ ext.cpp m.def「rasterize_gaussians」
  ▼ RasterizeGaussiansCUDA 形参 opacity (:42)
  ▼ opacity.contiguous().data<float>() (:116)
  ▼ CudaRasterizer::Rasterizer::forward（第 5 层）
```

**需要观察的现象**：沿途 `opacity` 这个张量本身始终是同一个 `(N,1)` float32 CUDA 张量（除非第 2、3 步改写生成了新张量），跨层传递只发生「重命名 + 位置参数化」，没有数值拷贝（`.contiguous()` 对已连续的张量是零开销）。

**预期结果**：得到一张 9 步传递图，并能对每一步说出文件、行号和该步发生的变化。此实践为源码阅读型，结论可直接从上述行号验证。

#### 4.2.5 小练习与答案

**练习 1**：`forward` 返回 6 个值，但训练损失只用 `rendered_image`。其余 5 个输出的梯度从哪来？

**答案**：PyTorch 规定 `backward` 的入参数量必须等于 `forward` 的返回值数量。未被损失使用（即未接入计算图下游）的输出，autograd 会以全零梯度张量调用 `backward`。所以 `grad_radii/grad_depth/grad_alpha/grad_flow/grad_covs_com` 通常是零张量，CUDA 反向据此自然忽略这些通路的贡献。

**练习 2**：为什么 `raster_settings` 不能整个作为一个参数直接传给 CUDA，而要拆成 29 个扁平参数？

**答案**：pybind11 的 `m.def` 注册的是 C++ 函数签名，`torch::Tensor`、`float`、`int`、`bool` 都有现成的类型转换器，而自定义的 Python `NamedTuple` 没有对应的 C++ 类型映射。拆平成基本类型后，pybind11 能自动完成 Python→C++ 的类型转换与校验；这也让 C++ 侧无需包含任何 Python 对象处理代码。

**练习 3**：如果我在 `render()` 里把 `opacities` 换成一个 `requires_grad=False` 的张量再训练，会发生什么？

**答案**：`_RasterizeGaussians.backward` 仍会返回 `grad_opacities`，但 autograd 只把梯度写到「要求梯度」的叶子/中间张量上；`requires_grad=False` 的输入会被跳过，`pc._opacity.grad` 得不到更新，不透明度（以及乘在它上面的衰减因子所影响的所有属性）训练停滞。反向传播的其他通路（位置、尺度等）不受影响。这也是 u5 训练循环要求所有属性张量默认 `requires_grad=True` 的原因。

### 4.3 markVisible：视锥剔除掩码

#### 4.3.1 概念说明

`markVisible` 回答一个二元问题：**给定一台相机的视图/投影矩阵，哪些高斯的中心在视锥内？** 它返回一个 `(N,)` 的 `torch.bool` 掩码——`True` 表示该高斯通过视锥测试。

4C4D 中它只有一个调用点：opacity decay 的 `time_aware` 分支（u6-l3 会展开）。直觉是：Neural Decaying Function 要给不透明度乘衰减因子，如果对当前视角根本看不见的高斯也衰减，就是无的放矢；所以先用 `markVisible` 得到**空间可见性**，再与 `get_marginal_t > 0.05` 的**时间可见性**取交集，只衰减「此刻、此视角真正活跃」的高斯。

注意它与 `visibility_filter`（u4-l1）的区别：`visibility_filter = radii > 0` 是**渲染后**的精确可见性（真正投到了屏幕上），而 `markVisible` 是**渲染前**的廉价粗筛（只看中心点深度），后者必须在拿到渲染结果之前使用，这正是 decay 需要的时序。

#### 4.3.2 核心流程

```text
render() 需要空间可见性（渲染前）
  ▼ GaussianRasterizer.markVisible(positions)     [Python, no_grad]
  ▼ _C.mark_visible(positions, viewmatrix, projmatrix)
  ▼ ext.cpp m.def「mark_visible」
  ▼ markVisible(means3D, viewmatrix, projmatrix)  [C++]
      └─ present = torch.full({P}, false, bool)   先全置 False
  ▼ Rasterizer::markVisible(P, ...)               [CUDA 启动]
      └─ checkFrustum<<<(P+255)/256, 256>>>       每线程一个高斯
          └─ present[idx] = in_frustum(点, view, proj, false, p_view)
  ▼ 返回 (P,) torch.bool 掩码
```

每个 CUDA 线程独立处理一个高斯：把中心点分别乘投影矩阵与视图矩阵，只做一次深度判断。

#### 4.3.3 源码精读

**Python 侧：两行核心，显式脱离梯度图**：

[diff-gaussian-rasterization/diff_gaussian_rasterization/__init__.py:231-240](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/diff_gaussian_rasterization/__init__.py#L231-L240)

```python
def markVisible(self, positions):
    # Mark visible points (based on frustum culling for camera) with a boolean 
    with torch.no_grad():
        raster_settings = self.raster_settings
        visible = _C.mark_visible(
            positions,
            raster_settings.viewmatrix,
            raster_settings.projmatrix)
    return visible
```

`with torch.no_grad()` 表明这是纯查询，不参与自动微分（掩码是离散的 0/1，本来也不可导）。复用了构造光栅化器时传入的同一份 `viewmatrix / projmatrix`，保证查询视角与后续渲染视角一致。调用点在 [gaussian_renderer/__init__.py:66](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L66)。

**C++ 侧：先造全 False 的掩码再填 True**：

[diff-gaussian-rasterization/rasterize_points.cu:268-287](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/rasterize_points.cu#L268-L287)

```cpp
torch::Tensor markVisible(
    torch::Tensor& means3D,
    torch::Tensor& viewmatrix,
    torch::Tensor& projmatrix)
{ 
  const int P = means3D.size(0);
  torch::Tensor present = torch::full({P}, false, means3D.options().dtype(at::kBool));
 
  if(P != 0)
  {
    CudaRasterizer::Rasterizer::markVisible(P,
        means3D.contiguous().data<float>(),
        viewmatrix.contiguous().data<float>(),
        projmatrix.contiguous().data<float>(),
        present.contiguous().data<bool>());
  }
  return present;
}
```

返回类型是 **`torch.bool` 张量**，形状 `(P,)`，语义是「通过粗筛 = True」。初始化为全 `False`、由 kernel 只把通过者写成 `True`，是 GPU 并行写的安全默认值。

**CUDA 启动层：一格 256 线程**：

[diff-gaussian-rasterization/cuda_rasterizer/rasterizer_impl.cu:141-154](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/rasterizer_impl.cu#L141-L154)

```cpp
// Mark Gaussians as visible/invisible, based on view frustum testing
void CudaRasterizer::Rasterizer::markVisible(
    int P, float* means3D, float* viewmatrix, float* projmatrix, bool* present)
{
    checkFrustum << <(P + 255) / 256, 256 >> > (
        P, means3D, viewmatrix, projmatrix, present);
}
```

`(P + 255) / 256` 是向上取整的标配写法：P 个点、每块 256 线程，多启的线程靠 kernel 内的越界检查退出。

**设备端：每线程一判**：

[diff-gaussian-rasterization/cuda_rasterizer/rasterizer_impl.cu:52-67](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/rasterizer_impl.cu#L52-L67)

```cpp
// Wrapper method to call auxiliary coarse frustum containment test.
// Mark all Gaussians that pass it.
__global__ void checkFrustum(int P,
    const float* orig_points,
    const float* viewmatrix,
    const float* projmatrix,
    bool* present)
{
    auto idx = cg::this_grid().thread_rank();
    if (idx >= P)
        return;
    float3 p_view;
    float3 orig_point = {orig_points[3*idx], orig_points[3*idx+1], orig_points[3*idx+2]};
    present[idx] = in_frustum(orig_point, viewmatrix, projmatrix, false, p_view);
}
```

**真正的判据在 `in_frustum`**：

[diff-gaussian-rasterization/cuda_rasterizer/auxiliary.h:140-163](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/auxiliary.h#L140-L163)

```cpp
__forceinline__ __device__ bool in_frustum(
    float3 p_orig, const float* viewmatrix, const float* projmatrix,
    bool prefiltered, float3& p_view)
{
    // Bring points to screen space
    float4 p_hom = transformPoint4x4(p_orig, projmatrix);
    float p_w = 1.0f / (p_hom.w + 0.0000001f);
    float3 p_proj = { p_hom.x * p_w, p_hom.y * p_w, p_hom.z * p_w };
    p_view = transformPoint4x3(p_orig, viewmatrix);

    if (p_view.z <= 0.2f) // || ((p_proj.x < -1.3 || ...))
    {
        if (prefiltered)
        {
            printf("Point is filtered although prefiltered is set. This shouldn't happen!");
            __trap();
        }
        return false;
    }
    return true;
}
```

仔细读这段能发现两个重要事实：

1. **只检查近裁剪面**。判据只有 `p_view.z <= 0.2f`：相机坐标系下深度不足 0.2（含相机背后的负深度）即剔除。而投影后 x/y 是否超出 `[-1.3, 1.3]` 屏幕边界的检查**被注释掉了**。所以 `markVisible` 是刻意保守的粗筛——屏幕外但深度合格的高斯也会标 `True`。对它的用途而言这是正确取舍：作为衰减掩码，误报（多衰减几个反正看不见的点）无害，漏报（漏掉贴边的可见点）才有害；而屏幕边界剔除这件事，前向渲染里由 `radii == 0` 机制（`duplicateWithKeys` 中 `if (radii[idx] > 0)` 才生成键值对，[rasterizer_impl.cu:85-87](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/rasterizer_impl.cu#L85-L87)）免费完成。
2. **`prefiltered=false` 传入**。`markVisible` 调用时硬编码 `false`，因此第 155-159 行的「已被预过滤却仍被剔除」的报警分支不会触发；该分支服务于前向渲染里 `prefiltered` 设置为真时的自检。

顺带一提：同一个 `in_frustum` 也被前向渲染复用（[cuda_rasterizer/forward.cu:440](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/forward.cu#L440)），保证「查询可见性」与「渲染可见性」使用同一套深度判据。

#### 4.3.4 代码实践

**实践目标**：验证 `markVisible` 输出的掩码类型，并用手工投影预测其结果。

**操作步骤**：

1. 阅读签名链并记录：Python `markVisible(positions) → _C.mark_visible(positions, viewmatrix, projmatrix) → bool 张量 (P,)`。
2. 构造一组测试点（示例代码，不属项目源码）：

```python
# 示例代码：预测 markVisible 结果（需 GPU 与已编译扩展）
import torch
from diff_gaussian_rasterization import GaussianRasterizationSettings, GaussianRasterizer

# 相机在原点看向 +z，视图矩阵取恒等，投影矩阵取简单透视（列主序 4x4，参考 u2-l3）
viewmatrix = torch.eye(4, device="cuda").float()
projmatrix = torch.eye(4, device="cuda").float()

settings = GaussianRasterizationSettings(
    image_height=100, image_width=100, tanfovx=1.0, tanfovy=1.0,
    bg=torch.zeros(3, device="cuda"), scale_modifier=1.0,
    viewmatrix=viewmatrix, projmatrix=projmatrix,
    sh_degree=0, sh_degree_t=0, campos=torch.zeros(3, device="cuda"),
    timestamp=0.0, time_duration=10.0, rot_4d=False, gaussian_dim=4,
    force_sh_3d=False, prefiltered=False, debug=False)
rasterizer = GaussianRasterizer(raster_settings=settings)

positions = torch.tensor([
    [0., 0., 5.],    # 相机正前方 5m → 预期 True
    [1000., 0., 5.], # 深度合格但远超屏幕 → 预期仍 True（粗筛不查边界）
    [0., 0., 0.1],   # 深度 < 0.2 → 预期 False
    [0., 0., -1.],   # 相机背后 → 预期 False
], device="cuda")
print(rasterizer.markVisible(positions))       # 期望 tensor([True, True, False, False])
print(rasterizer.markVisible(positions).dtype) # 期望 torch.bool
```

3. 对照 `in_frustum` 的判据 `p_view.z <= 0.2f`，逐点核对你的预测与实际输出。

**需要观察的现象**：第 2 个点明明在视锥外（x=1000 早已超出屏幕），掩码却是 `True`——亲眼确认「markVisible 不做屏幕边界剔除」这一结论。

**预期结果**：输出 `[True, True, False, False]`，dtype 为 `torch.bool`。本实践需要编译好的 CUDA 扩展与 GPU，**待本地验证**；无 GPU 时可退化为纯手工演算：用 `viewmatrix`（恒等）得到 `p_view = p`，逐点套用 `z <= 0.2` 判据即可得出同样结论。

#### 4.3.5 小练习与答案

**练习 1**：`markVisible` 的掩码与 `render()` 返回的 `visibility_filter` 有何本质区别？各自的产生时机？

**答案**：`markVisible` 在渲染**前**基于中心点深度做粗筛，输出 `torch.bool`，允许屏幕外的误报；`visibility_filter = radii_all > 0`（[gaussian_renderer/__init__.py:195](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/gaussian_renderer/__init__.py#L195)）在渲染**后**由屏幕半径精确判定，输出也是 `torch.bool`，但只有真正投上屏的高斯才为 `True`。前者用于渲染前干预（衰减掩码），后者用于渲染后统计（致密化时排除不可见高斯，见 u5-l4）。

**练习 2**：为什么 `markVisible` 的 CUDA kernel 只检查 `z <= 0.2` 而把 x/y 边界检查注释掉？打开注释会有什么问题？

**答案**：因为该掩码用于 opacity decay，宁可多报不可漏报——多衰减一个最终被精确剔除的高斯没有影响，漏掉一个贴着屏幕边缘的高斯会让它逃过衰减、干扰几何学习。此外 `in_frustum` 只测试高斯**中心点**，真实高斯是有半径的椭球，中心出界不代表整个椭球出界，用中心做边界判断本身就不准确。若强行打开注释，边界附近的高斯会被漏掉，且这份代码还被 `forward.cu` 的预处理路径复用，可能连带影响渲染剔除行为。

**练习 3**：`checkFrustum` 的启动配置 `<<<(P + 255) / 256, 256>>>` 中，为什么是 255 而不是 256？kernel 里哪一行处理了多出来的线程？

**答案**：`(P + 255) / 256` 是整数除法下的向上取整——P=257 时需要 2 块（257/256=1 余 1，(257+255)/256=2）。若写 `(P + 256) / 256`，P 恰为 256 的倍数时会多启一块（无害但浪费）；写 `P / 256` 则会漏掉尾部不足 256 的点。多启的线程由 `if (idx >= P) return;`（[rasterizer_impl.cu:61-62](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/cuda_rasterizer/rasterizer_impl.cu#L61-L62)）提前退出。

## 5. 综合实践

**任务：绘制「一次渲染调用」的完整跨层时序图，并标注梯度回程。**

把本讲三个模块串起来，画一张从 `render()` 出发、穿过五层、再从 `loss.backward()` 返回的完整时序图。要求：

1. **正向路径**至少包含这些节点（每节点标注 文件:行号）：
   - `render()` 构造 `GaussianRasterizationSettings`（gaussian_renderer/__init__.py:36-55）
   - `rasterizer.markVisible(means3D)`（gaussian_renderer/__init__.py:66，仅 opacity decay 开启时）
   - `rasterizer(...)`（gaussian_renderer/__init__.py:160-172）
   - `GaussianRasterizer.forward` 的三组互斥检查（diff_gaussian_rasterization/__init__.py:249-258）
   - `_RasterizeGaussians.apply`（:36）
   - forward 的 29 元组 args（:72-102）
   - `_C.rasterize_gaussians`（:108 或 :114）
   - ext.cpp 登记表（ext.cpp:16）
   - `RasterizeGaussiansCUDA` 的裸指针转发（rasterize_points.cu:104-140）
   - `CudaRasterizer::Rasterizer::forward`（第 5 层入口）
2. **反向路径**：`loss.backward()` → `backward(ctx, 6 个梯度)`（:124）→ `ctx.saved_tensors` 取回 16 张量（:130-132）→ 36 元组（:135-170）→ `_C.rasterize_gaussians_backward`（:178 或 :186）→ 12 梯度重排为 13 元组（:188-202）→ 梯度写入 `pc._opacity.grad` 等属性。
3. 在图上用两种颜色/线型区分「数据流」与「梯度流」，并把三个不透明缓冲（geomBuffer/binningBuffer/imgBuffer）画成一条从前向绕到反向的旁路。
4. 给图配 200 字说明，回答：为什么 means2D 明明传进了 forward，却不出现在 CUDA 前向参数表里？

**验收标准**：拿你画的图给同事（或对着源码自己）走一遍，每一跳都能在 30 秒内指出对应文件与行号，即算通过。本实践为源码阅读型，所有行号已在本讲正文中给出，可直接核对。

## 6. 本讲小结

- 光栅化调用链共五层：`render()` → Python 包装层（autograd.Function）→ `ext.cpp` 登记表 → `rasterize_points.cu` 的 C++ 薄壳 → `cuda_rasterizer/` 的纯 CUDA 实现；`ext.cpp` 只用 3 行 `m.def` 登记了 `rasterize_gaussians`、`rasterize_gaussians_backward`、`mark_visible` 三个入口。
- `_RasterizeGaussians` 不做数值计算，只做参数编舞：前向把 13 个 Python 参数展平成 29 元组调 CUDA、用 `ctx.save_for_backward` 缓存 16 个张量、返回 6 个输出；反向拼 36 元组调 CUDA 反向，把 12 个梯度按 forward 输入顺序重排成 13 元组（末位 `None` 对应 settings）。
- `geomBuffer/binningBuffer/imgBuffer` 三个字节张量是「不透明句柄」：前向在 GPU 上生成、Python 原样持有、反向原样交还，避免了在 Python 侧解释中间状态的结构；`out_means3D` 则携带时间均值偏移，让反向基于修正后的位置求梯度。
- `means2D` 传进 forward 但不进 CUDA 前向参数表——屏幕坐标在 CUDA 内部重算，它只是反向屏幕空间梯度的载体，这从 [rasterize_points.cu:104-140](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/rasterize_points.cu#L104-L140) 的转发表中可以实证。
- `markVisible` 输出 `(P,)` 的 `torch.bool` 掩码，判据只有近裁剪面 `p_view.z <= 0.2`（x/y 边界检查被注释），是渲染前的保守粗筛；4C4D 仅在 opacity decay 的 `time_aware` 分支用它做空间可见性，与时间可见性取交集后再决定衰减哪些高斯。

## 7. 下一步学习建议

- 下一讲 u4-l3（Python 回退路径与环境贴图背景）会回到 `render()` 的 `pipe` 分支：当 `compute_cov3D_python` 与 `convert_SHs_python` 打开时，本讲介绍的 CUDA 侧计算（协方差、SH 颜色）将被显式搬到 Python 里，两相对照能加深对本讲绑定层的理解。
- 若想继续向下钻，直接预习 u8-l1（CUDA 光栅化器内部实现）：本讲止步于第 5 层入口 `CudaRasterizer::Rasterizer::forward`，u8-l1 讲 tile 划分、键值排序与逐像素 alpha 混合。
- 建议顺带阅读 [diff-gaussian-rasterization/rasterize_points.h](https://github.com/yangzf-1023/4C4D/blob/ed6a3cb69782c4147151b3898944bc38132bae00/diff-gaussian-rasterization/rasterize_points.h) 全文——它只有 90 余行，是前向/反向/maskVisible 三组参数的权威清单，以后任何时候想确认参数含义都先查它。
- 结合 u6-l3 再回看 `markVisible` 的调用点，理解「空间可见 ∧ 时间可见」掩码如何保证 Neural Decaying Function 只作用于当前视角真正活跃的高斯。
