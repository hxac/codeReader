# 融合算子：resize_crop_convert_reformat 与 crop_flip_normalize_reformat

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚「融合算子」到底融合了什么：它不是把多个 kernel 串起来，而是把多步几何、色彩、类型、布局变换折进**同一个 CUDA kernel**，对每个输出像素只做一次显存读（gather 源像素）和一次显存写。
2. 熟练调用 `cvcuda.resize_crop_convert_reformat` 与 `cvcuda.crop_flip_normalize_reformat`（含 `_into` 变体与变长批输入），并理解每个参数对应的融合步骤。
3. 打开这两个算子的 CUDA 源码，指出 crop 是在哪里「发生」的（答案可能和你的直觉不同：crop 从不被单独执行，它被折叠进了采样坐标的计算公式里）。
4. 拿到自己手头的预处理管线时，能对照 Limitations 契约表判断「能不能换成融合算子」，并说出替换后预期节省的开销。

本讲承接 u3-l3 的 allocating/`_into` 两变体模型：融合算子同样遵守这套约定，只是它一次干了五六个算子的活。

## 2. 前置知识

### 2.1 为什么「多算子串联」在高吞吐场景是问题

回顾 u3-l2/u3-l3：一条典型的推理预处理管线是

```
resize → crop → cvtcolor → convertto(F32) → reformat(NHWC→NCHW)
```

用 5 个独立算子实现时，GPU 上实际发生的事情是：

- **5 次 kernel 启动**。每次启动都有微秒级的 CPU 侧开销；当图像较小（如 224×224）时，启动开销占比会非常高。
- **4 个中间张量被完整写回显存再读出来**。算子 i 的输出是算子 i+1 的输入，数据在显存里来回搬运。GPU 视觉算子大多是带宽受限（memory-bound）的，显存流量几乎直接决定耗时。

设第 \(i\) 个中间结果占 \(M_i\) 字节，则未融合管线的显存流量约为

\[
\text{traffic}_{\text{unfused}} \;\approx\; 2\,(M_1 + M_2 + \dots + M_k)
\]

（每个中间结果被写一次、读一次，粗略估算）。而理想融合只需

\[
\text{traffic}_{\text{fused}} \;\approx\; M_{\text{src}} + M_{\text{dst}}
\]

源数据读一次、最终结果写一次，中间结果只存在于寄存器里，从不下显存。以 224×224×3 的 uint8 中间张量为例，单个中间结果约 145 KB/张；批越大，节省的绝对量越大。

这就是融合算子（fused operator）的动机：**为推理预处理这种「固定动作组合」定制一次性完成全部变换的 kernel**。

### 2.2 你需要 already 知道的（前几讲结论）

- `cvcuda.Tensor` 的 shape/dtype/layout 语义，NHWC（交错）与 NCHW（平面）的区别（u2-l1）。
- `ImageBatchVarShape`：批内每张图尺寸可以不同，但格式须统一（u2-l3）。
- allocating 变体（`cvcuda.op(...)` 隐式建输出）与 `_into` 变体（写入预分配 `dst`）的差异与取舍（u3-l3）。
- 绑定层的 ResourceGuard / `CreateOperator` / `NvtxTrace` 包装套路（u3-l1、u3-l3 已见，u5-l1 将深入）。

### 2.3 两个新术语

- **gather（聚集）访存**：输出像素主动「回头」找源像素的坐标（resize 的双线性插值要读 4 个源像素），区别于逐元素一一对应的流式访存。本讲的 kernel 注释里会明确出现这个词。
- **SaturateCast（饱和转换）**：类型转换时把越界值钳到目标类型的上下限（如 float→uint8 时 300 变 255），而非溢出回绕。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `src/cvcuda/include/cvcuda/OpResizeCropConvertReformat.h` | C API 头文件：官方六步定义 + Limitations 契约表（权威支持矩阵） |
| `python/mod_cvcuda/operators/OpResizeCropConvertReformat.cpp` | pybind11 绑定：4 个函数（Tensor/变长批 × allocating/`_into`），负责输出 shape 推导 |
| `src/cvcuda/priv/OpResizeCropConvertReformat.cu` | 私有实现：参数校验 + 模板分发 + 融合 CUDA kernel 本体 |
| `samples/operators/resize_crop_convert_reformat.py` | 官方样例：NHWC 张量输入的融合预处理 |
| `samples/operators/crop_flip_normalize_reformat.py` | 官方样例：变长批输入的 crop+flip+归一化+重排 |
| `python/mod_cvcuda/operators/OpCropFlipNormalizeReformat.cpp` | crop_flip_normalize_reformat 的绑定与 docstring |
| `src/cvcuda/include/cvcuda/OpCropFlipNormalizeReformat.h` | 该算子的 Limitations 契约表 |

阅读建议：先跑/读两个样例建立直觉（4.2、4.4），再进 `.cu` 看 kernel（4.3），最后回头看 Limitations 表做替换判断（4.5）。

## 4. 核心概念与源码讲解

### 4.1 融合算子融合了哪六步：官方定义

#### 4.1.1 概念说明

`ResizeCropConvertReformat` 的 C 头文件用一段注释给出了最权威的定义：它按顺序完成六件事——resize、crop、scale/offset、通道重排、类型转换、布局重排。这段注释是理解整个算子的「合同原文」，值得逐条读。

#### 4.1.2 核心流程

```
输入(HWC/NHWC/CHW/NCHW, uint8)
  │
  ├─ ① resize 到 resize_dim（NEAREST 或 LINEAR）
  ├─ ② 从 resize 结果中按 crop_rect 取区域
  ├─ ③ 对每个像素值 v 计算 scale·v + offset
  ├─ ④ manip 通道重排（如 REVERSE: RGB↔BGR）
  ├─ ⑤ 转换成输出 dtype（uint8 或 float32）
  └─ ⑥ 重排成输出 layout（如 NHWC → NCHW）
  │
输出(指定 layout 与 dtype)
```

注意数学上的合成公式：设 \(v\) 为某输出像素经 resize+crop 得到的源域插值结果，则最终写出值为

\[
\text{out} = \text{scale} \cdot v + \text{offset}
\]

头文件举的例子很实用：输入 uint8（0~255）、输出 float，取 \(\text{scale}=1/127.5,\ \text{offset}=-1\) 即把值域映射到 \([-1, 1]\)——正是许多视觉模型的输入约定。

还有一个精妙的可选开关 `srcCast`：独立 resize 算子的输入输出 dtype 必须相同，所以插值结果会被**转回源类型**（uint8），带来量化损失。融合算子允许你跳过这次回转（`srcCast=False`），让后续 scale/offset 直接在浮点插值结果上进行，避免量化误差——这是融合带来的**精度红利**，不只是速度红利。

#### 4.1.3 源码精读

官方六步定义（合同原文）：

[OpResizeCropConvertReformat.h:56-101](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/OpResizeCropConvertReformat.h#L56-L101) — 逐条列出 ①resize ②crop ③scale/offset ④通道重排 ⑤类型转换 ⑥布局重排，并在 NOTES 里解释了 `srcCast` 与独立算子结果一致性的关系（L94-L101）。

[OpResizeCropConvertReformat.h:87-89](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/OpResizeCropConvertReformat.h#L87-L89) — 关键的一句：变长批中所有图被 resize 到同一尺寸后，结果自然装进**一个张量**。这解释了为什么该算子输入可以是 `ImageBatchVarShape` 而输出永远是规则 Tensor（变长批在推理边界被「压平」）。

#### 4.1.4 代码实践

**实践目标**：在动手调用之前，先学会用纸笔估算融合的收益，建立「值不值得换」的数量级概念。

**操作步骤**：

1. 设想 batch=32、每图 resize 后 256×256×3 的 uint8 管线（后续 crop 到 224×224、cvtcolor、convertto F32、reformat NCHW）。
2. 用 2.1 的公式分别计算：未融合（4 个中间结果各读写一次）与融合（只读源、只写 F32 输出）的近似显存流量。
3. 把两个数相除，得到理论带宽节省比例。

**需要观察的现象**：中间结果越多、图越大，比值越高；同时思考：kernel 启动次数从 5 降到 1，对小图批的影响为什么更大？

**预期结果**：一份手工估算记录。数值本身待本地验证（下一节会真正计时）。

#### 4.1.5 小练习与答案

**练习 1**：融合算子把哪些中间结果「消灭」了？它们去了哪里？

**答案**：resize 结果、crop 结果、色彩转换结果、float 中间值都被消灭了；它们只存在于 kernel 内每个线程的寄存器/局部变量中，从不写回显存。唯一物化的是最终输出张量。

**练习 2**：`srcCast=True`（默认）与 `False` 在结果上有什么差别？什么时候应该选 `False`？

**答案**：`True` 时插值结果先饱和转回源类型（uint8）再做 scale/offset，与「独立 resize + 后续算子」的分步结果一致；`False` 时保留浮点插值精度，避免量化到 uint8 的误差（约 ±0.5 灰度级）。当输出是 float 且下游模型对精度敏感时选 `False`；当需要与旧的分步管线逐位对齐时选 `True`。

### 4.2 Python 视角：参数、输出 shape 推导与两个官方样例入口

#### 4.2.1 概念说明

Python 绑定层为该算子注册了 4 个同名/同族函数（重载决议，同 u3-l1 的「四连函数」套路）：

| 函数 | 输入 | 变体 |
|------|------|------|
| `resize_crop_convert_reformat` | Tensor | allocating |
| `resize_crop_convert_reformat_into` | Tensor | `_into` |
| `resize_crop_convert_reformat` | ImageBatchVarShape | allocating |
| `resize_crop_convert_reformat_into` | ImageBatchVarShape | `_into` |

allocating 变体多干一件事：**根据 crop_rect 与目标 layout/dtype 推导输出张量的 shape 并创建它**。这个推导逻辑就写在绑定文件里，是可以直接读懂的 Python 侧「shape 合同」。

#### 4.2.2 核心流程

allocating（Tensor 输入）版本的推导流程：

```
校验输入 layout ∈ {HWC, NHWC, CHW, NCHW}
  → dstLayout 未指定则复制 srcLayout；秩必须与输入一致
  → 把输入 shape 排成 NHWC 顺序
  → H ← cropRect.height, W ← cropRect.width   # 输出尺寸就是裁剪尺寸
  → 再从 NHWC 排成 dstLayout
  → Tensor::Create(dstShape, dataType)          # 走 u3-l3 讲过的对象缓存
  → 委托 _into 版本执行
```

`_into` 版本则完全不推导：**dst 张量自己声明了裁剪尺寸、输出 dtype 与 layout**，算子照着写。

#### 4.2.3 源码精读

allocating 版的输出 shape 推导（Tensor 输入）：

[OpResizeCropConvertReformat.cpp:93-102](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpResizeCropConvertReformat.cpp#L93-L102) — 先 `Permute` 到 NHWC 拿到统一坐标系，把第 2 维（W）设为 `cropRect.width`、第 1 维（H）设为 `cropRect.height`，再 `Permute` 到目标 layout，最后 `Tensor::Create`。注意：**输出的 H/W 由裁剪矩形决定，与 resize_dim 无关**（resize_dim 只决定中间尺寸）。

layout 合法性校验（输入与输出各一次，非法即抛 `nvcv::Exception`）：

[OpResizeCropConvertReformat.cpp:65-91](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpResizeCropConvertReformat.cpp#L65-L91) — 只接受 HWC/NHWC/CHW/NCHW 四种，且输出秩必须与输入相同（不想带 N 维就用 HWC→CHW）。

`_into` 版本的执行体（ResourceGuard + submit，与 u3-l1/u3-l3 见过的所有算子同构）：

[OpResizeCropConvertReformat.cpp:43-55](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpResizeCropConvertReformat.cpp#L43-L55) — `CreateOperator` 取缓存算子对象，guard 注册 src 读锁/dst 写锁，`guard.run` 内调用 C++ 的 `resize->submit(stream, src, dst, size_wh, interp, crop_xy, manip, scale, offset, srcCast)`。

导出签名与默认值：

[OpResizeCropConvertReformat.cpp:207-210](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpResizeCropConvertReformat.cpp#L207-L210) — `resize_crop_convert_reformat(src, resize_dim, interp, crop_rect, *, layout="", data_type=NONE, manip=NO_OP, scale=1.0, offset=0.0, srcCast=True, stream=None)`。docstring 明确写了 **interp 目前只支持 NEAREST 与 LINEAR**（[L218-L219](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpResizeCropConvertReformat.cpp#L218-L219)），与 `.cu` 里的显式抛异常一一对应。

官方样例的调用现场：

[resize_crop_convert_reformat.py:80-90](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/operators/resize_crop_convert_reformat.py#L80-L90) — 把单张 HWC 图 `reshape((1, *shape), layout="NHWC")` 套上批维（模拟深度学习管线的典型用法），再构造 `cvcuda.RectI(0, 0, crop_w, crop_h)` 作为左上角裁剪矩形。

[resize_crop_convert_reformat.py:97-107](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/operators/resize_crop_convert_reformat.py#L97-L107) — 一次调用完成 resize→crop→F32→NCHW→通道反转（BGR↔RGB）→scale/offset。

**一个值得留意的坐标约定细节**：`resize_dim` 的 docstring 说它是 (width, height)（绑定代码里 `std::get<0>` 被放进 `Size2D` 的 `w` 字段，见 [OpResizeCropConvertReformat.cpp:50-51](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpResizeCropConvertReformat.cpp#L50-L51) 与 [Size.h:33-37](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/Size.h#L33-L37)）；而样例注释写的是 `(H, W)`（[resize_crop_convert_reformat.py:87](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/operators/resize_crop_convert_reformat.py#L87)）。默认参数是正方形（224×224）所以二者数值相同、样例不受影响，但**非正方形目标时请以 docstring 的 (width, height) 为准**——这是读源码时「文档与注释打架」的真实案例，动手验证方法见 4.2.4。

#### 4.2.4 代码实践

**实践目标**：跑通官方样例，并用一个非正方形目标验证 `resize_dim` 的坐标顺序。

**操作步骤**：

1. 按 u1-l2 装好 cvcuda wheel，进入 `samples/operators/`：
   ```bash
   python resize_crop_convert_reformat.py --input <某张jpg> --output out_fused.jpg
   ```
2. 打开输出图，确认它是输入图先缩放、再从左上角裁剪的结果（通道还被反转过一次——输入若是 RGB 则输出等效 BGR 显示，颜色会偏）。
3. 把目标改为非正方形，例如 `--height 128 --width 256`，再运行一次，观察输出图的宽高比到底服从哪个参数（待本地验证；预期按 docstring，实际宽度应等于你传给 `--width` 的值）。

**需要观察的现象**：输出是 224×224（或你指定的尺寸）的裁剪结果；`nchw_f32_to_hwc_u8` 辅助函数（[resize_crop_convert_reformat.py:38-66](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/operators/resize_crop_convert_reformat.py#L38-L66)）在 CPU 上把 NCHW float32 转回 HWC uint8 才能编码保存——注意这只是**为了人眼查看**，推理管线里不需要这一步。

**预期结果**：得到一张正确尺寸的输出图；非正方形实验能让你确定 resize_dim 的顺序语义。

#### 4.2.5 小练习与答案

**练习 1**：调用 `resize_crop_convert_reformat(src, (256,256), LINEAR, RectI(32,32,224,224), layout="NCHW", data_type=F32)`，输入是 `(1, 720, 1280, 3)` 的 NHWC uint8，输出 shape 是什么？

**答案**：`(1, 3, 224, 224)`。H/W 取自裁剪矩形的 height/width（224×224），layout=NCHW 把通道维放到第 1 位，dtype=F32。720×1280 的原始尺寸与 256×256 的中间尺寸都不出现在输出里。

**练习 2**：为什么 `_into` 版本不需要 `layout`/`data_type` 参数？

**答案**：因为 dst 张量本身就是「输出合同」：它的 shape 决定裁剪尺寸、dtype 决定类型转换目标、layout 决定重排目标（docstring 原话见 [OpResizeCropConvertReformat.cpp:252-254](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpResizeCropConvertReformat.cpp#L252-L254)）。allocating 版本做的正是「替你推导并创建这份合同」。

**练习 3**：样例为什么要在调用前把 HWC reshape 成 NHWC？

**答案**：两个原因——(1) 深度学习推理的标准输入是带批维的 NHWC/NCHW，样例演示的就是这个真实用法；(2) 输出秩必须与输入一致（[L81-85](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpResizeCropConvertReformat.cpp#L81-L85)），用 NHWC 进、NCHW 出正好得到 `(N,C,H,W)`。

### 4.3 kernel 精读：六步是如何折进一个 CUDA kernel 的

#### 4.3.1 概念说明

这是本讲最核心的一节。关键认知：**crop 步骤在 kernel 里根本不存在**。没有任何线程先 resize 出一张中间图再裁剪它；相反，每个输出线程用「输出坐标 + 裁剪偏移」直接算出自己应该去源图的哪个位置采样。resize 与 crop 被合并成了一条坐标映射公式，写目标是「类型转换 + 通道重排 + 布局寻址」的同一个 `operator()` 调用。

#### 4.3.2 核心流程

每个 CUDA 线程负责一个输出像素：

```
线程 (dst_x, dst_y, batch=blockIdx.z):
  1. 计算源域浮点坐标：
        fx = (dst_x + crop.x + 0.5) · resize_x − 0.5
        fy = (dst_y + crop.y + 0.5) · resize_y − 0.5
     其中 resize_x = src_w / resize_dim.w（源宽 / 目标宽）
  2. NEAREST：取 (⌊fx'⌋, ⌊fy'⌋) 一个源像素
     LINEAR：取 (2×2) 四个源像素做双线性加权
  3. （srcCast=True 时）把插值结果饱和转回源类型
  4. out = scale · v + offset
  5. DstMap::operator()：按 manip 重排通道 → SaturateCast 到输出类型
     → 按 NCHW/NHWC 的 stride 算出输出地址并写入
```

双线性插值的加权公式为

\[
v = (1-f_y)\big[(1-f_x)\,v_{00} + f_x\,v_{01}\big] + f_y\big[(1-f_x)\,v_{10} + f_x\,v_{11}\big]
\]

其中 \(f_x, f_y\) 是源坐标的小数部分，\(v_{00}\dots v_{11}\) 是四个邻域像素。注意 crop 偏移只出现在第 1 步的坐标里——这就是「折叠」的全部秘密。

网格划分：`grid = (⌈dst_w/16⌉, ⌈dst_h/16⌉, numSamples)`，块 16×16=256 线程；**batch 维直接映射到 grid.z**，批内所有图共享同一次 kernel 启动。

#### 4.3.3 源码精读

融合的坐标映射（双线性版）：

[OpResizeCropConvertReformat.cu:289-307](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpResizeCropConvertReformat.cu#L289-L307) — `resizeCrop_bilinear` kernel 本体。L301-L302 一行完成 resize+crop 的坐标折叠：`float fx = (dst_x + crop.x + 0.5f) * resize.x - 0.5f;`（+0.5/−0.5 是半像素中心对齐约定）。L305 把后续全部工作（插值、srcCast、scale/offset、通道重排、类型转换、布局写出）交给 `WriteBilinearPixel` + `DstMap`。

NEAREST 版（更短，适合第一次读）：

[OpResizeCropConvertReformat.cu:268-286](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpResizeCropConvertReformat.cu#L268-L286) — L280-L281 同样的折叠公式取整后读一个源像素，L284 一行完成 `scale·v + offset` 与写出。读这个 kernel 就能看懂「一个输出像素 = 一次读 + 一次写」。

双线性采样与 srcCast 分支：

[OpResizeCropConvertReformat.cu:230-263](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpResizeCropConvertReformat.cu#L230-L263) — `WriteBilinearPixel` 读 2×2 邻域（L248-L251），L253-L262 是 `srcCast` 的两个分支：True 分支多包一层 `SaturateCast<SrcT>`（量化回源类型），False 分支直接在浮点空间乘 scale 加 offset。

「步骤④⑤⑥」的化身——DstMap：

[OpResizeCropConvertReformat.cu:63-123](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpResizeCropConvertReformat.cu#L63-L123) — 写端封装类。构造时接收输出的 N/H/W/C 四个 stride 与 `manip`；[L88-L107](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpResizeCropConvertReformat.cu#L88-L107) 的 `operator()` 一次完成通道重排（`m_mapC[]` 是预计算的通道→字节偏移表）与 `SaturateCast<DstT>` 类型转换；[L109-L116](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpResizeCropConvertReformat.cu#L109-L116) 的 `_init` 把 `manip`（如 REVERSE）翻译成偏移表。布局重排（NHWC/NCHW）不需要专门代码——它被吸收进 stride 的取值里。

通道重排表：

[OpResizeCropConvertReformat.cu:48-61](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpResizeCropConvertReformat.cu#L48-L61) — `remapC<N>`：`NVCV_CHANNEL_REVERSE` 时 3 通道返回 `{2,1,0}`（即 RGB↔BGR），否则恒等 `{0,1,2,...}`。这就是参数 `manip=cvcuda.ChannelManip.REVERSE` 的全部实现。

网格划分与「gather」注释：

[OpResizeCropConvertReformat.cu:451-458](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpResizeCropConvertReformat.cu#L451-L458) — 块 16×16（256 线程），`gridSize.z = samples`（L455），L457-L458 的注释直说了设计意图：*resize 本质是 gather 访存 + 少量计算，目标是最大化吞吐*。变长批版用 32×8 的块并注释「让每个 warp 落在同一输出行」（[L513-L517](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpResizeCropConvertReformat.cu#L513-L517)）——融合 kernel 的块形状是按访存模式调过的，改动前先测（u7-l3 的基准体系）。

平面（NCHW/CHW）输入的特殊处理：

[OpResizeCropConvertReformat.cu:460-469](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpResizeCropConvertReformat.cu#L460-L469) — 输入也是平面布局时，不能用普通的交错 TensorWrap（一次读出 uchar3），改用 `TensorPlanarSrc`（[L140-L169](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpResizeCropConvertReformat.cu#L140-L169)）：按 planeStride 从三个平面各取一个分量拼成像素。这兑现了「输入支持 CHW/NCHW」的合同。

priv 层的校验与模板分发：

[OpResizeCropConvertReformat.cu:543-690](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpResizeCropConvertReformat.cu#L543-L690) — `operator()` 先 `exportData` 拿 CUDA 视图（L549-L561），然后是一长串 CPU 侧前置校验：批数/通道数一致（L572-L585）、通道只允许 1 或 3（L587-L592）、输入必须 uint8 且输出 uint8/float（L597-L605）、layout 合法（L610-L626）、resize 维度 >1（L628-L633）、**裁剪矩形不得越出 resize 后的边界**（L635-L641）、interp 只允许 NEAREST/LINEAR（L649-L667）。最后 L669-L689 按「通道数 × 输出类型」四路实例化模板 `<uchar1|uchar3, uint8_t|float>`。这一段是 u5-l1 将讲的「priv 层先校验后执行」模式的典型样本。

#### 4.3.4 代码实践（源码阅读型）

**实践目标**：不运行任何代码，只靠阅读 kernel 回答三个问题，检验你真的看懂了「折叠」。

**操作步骤**：

1. 打开 [OpResizeCropConvertReformat.cu:268-286](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpResizeCropConvertReformat.cu#L268-L286)（NN 版）与 [L289-L307](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpResizeCropConvertReformat.cu#L289-L307)（LINEAR 版）。
2. 回答：(a) 裁剪发生在哪一行？(b) 一个 LINEAR 输出线程最多读几个源像素、写几个输出像素？(c) `resize.x`（映射比）是在 host 侧还是 device 侧算出来的，值是多少？
3. 再对照 [L438](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpResizeCropConvertReformat.cu#L438) 验证你第 3 问的答案。

**需要观察的现象**：你能在 30 秒内指出 L301（或 NN 版的 L280-L281）就是「crop 的实现位置」。

**预期结果**：(a) 折叠在采样坐标公式 `(dst_x + crop.x + 0.5f) * resize.x - 0.5f` 里，没有独立的 crop 步骤；(b) LINEAR 读 4 个源像素（2×2 邻域）、写 1 个输出像素；(c) host 侧，`resize = {src_w/resizeDim.w, src_h/resizeDim.h}`（L438），作为 kernel 参数传入。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `DstMap` 能同时支持 NHWC 与 NCHW 两种输出布局而不用分支判断？

**答案**：因为布局差异被编码进构造函数收到的四个 stride（`addN/addH/addW/addC`）。L442-L445 显示：平面输出时 `addC = planeStride`（跨平面）、`addW=1`；交错输出时 `addC=1`、`addW=channels`。`ptr()` 只做 `base + n·addN + y·addY + x·addX`，对两种布局是同一行代码。

**练习 2**：把 `resizeCrop_NN` 的 `__float2int_rd` 换成 `__float2int_rn`（四舍五入）会改变语义吗？

**答案**：会。`_rd`（向零取整）实现的是「左上邻域」最近邻约定，`_rn` 会把恰好落在两像素中间的采样点归到右/下侧，输出像素值可能与原实现不同（半像素偏移类差异）。改一个字符就是另一种插值语义——也说明为什么这类「看似简单」的算子需要 bit-exact 的黄金参考测试（u7-l1）。

**练习 3**：批维 `blockIdx.z` 的设计意味着什么？

**答案**：一次 kernel 启动处理整个批的所有图（Tensor 版每图同尺寸所以采样比相同；变长批版在 kernel 内按 `blockIdx.z` 逐图取宽高与采样比，见 [L312-L336](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpResizeCropConvertReformat.cu#L312-L336)）。批内不用循环、不用多次启动，这正是批处理场景下融合算子的又一重收益。

### 4.4 变长批与参数张量化：crop_flip_normalize_reformat

#### 4.4.1 概念说明

第二个融合算子面向另一类常见组合：**裁剪 + 翻转 + 归一化 + 布局重排**，且直接吃 `ImageBatchVarShape`（变长批，u2-l3）。它比上一个算子更进一步的地方在于**参数张量化**：crop 矩形、flip_code、归一化的 base/scale 都是形状很小的 GPU 张量，**每张图一份**。这让「批内第 i 张图裁这里、第 j 张图翻一下、第 k 张图用另一组均值」在一次调用里完成——这是数据增强（augmentation）管线的标准需求。

#### 4.4.2 核心流程

```
准备阶段（host）：
  batch = ImageBatchVarShape(N); batch.pushback([...])     # 变长批
  rect/base/scale/flip_code → 小张量上传 GPU（每图一份）

一次调用：
  crop_flip_normalize_reformat(batch, out_shape, out_dtype, out_layout,
                               rect, flip_code, base, scale,
                               globalscale, globalshift, epsilon, flags, ...)
  输出：规则 Tensor（NCHW float32 等）
```

归一化公式（`flags=cvcuda.NormalizeFlags.SCALE_IS_STDDEV` 时）：

\[
\text{out} = \frac{v \cdot \text{globalscale} - \text{base}}{\sqrt{\text{scale}^2 + \epsilon}}
\]

若不设该标志，分母直接用 scale（乘性缩放语义）。样例取 `globalscale = 1/255`、base/scale 为 ImageNet 的逐通道均值与标准差，正是 `(pixel/255 − mean)/std` 这一最经典的模型输入归一化。

#### 4.4.3 源码精读

样例的变长批与参数张量准备：

[crop_flip_normalize_reformat.py:49-52](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/operators/crop_flip_normalize_reformat.py#L49-L52) — `cvcuda.as_image(input.cuda(), cvcuda.Format.RGB8)` 把张量视图转成图像语义，再装进 `ImageBatchVarShape(1)`。

[crop_flip_normalize_reformat.py:56-71](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/operators/crop_flip_normalize_reformat.py#L56-L71) — 裁剪矩形张量形状 `(1,1,1,4)`（NHWC，最后一维存 `[x, y, w, h]`）、flip_code 张量 `(1,1)`；注意 flip_code 语义与 OpenCV 兼容（0 上下翻 / 1 左右翻 / −1 双轴 / 其他不翻），与 u3-l1 讲过的独立 flip 算子一致。

[crop_flip_normalize_reformat.py:75-86](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/operators/crop_flip_normalize_reformat.py#L75-L86) — base/scale 各为 `(1,1,1,3)` 的逐通道张量；输出 shape 显式声明为 `(N, C, crop_h, crop_w)`——这个算子连输出形状都是调用者给的（比上一个算子的推导合同更直接）。

融合调用现场：

[crop_flip_normalize_reformat.py:93-108](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/operators/crop_flip_normalize_reformat.py#L93-L108) — 一次调用完成裁剪中央 80% 区域 + 水平翻转 + ImageNet 归一化 + NCHW float32 重排；`border=cvcuda.Border.REPLICATE` 声明越界采样策略（翻转+裁剪组合可能触达边缘外像素）。

绑定签名：

[OpCropFlipNormalizeReformat.cpp:87-91](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpCropFlipNormalizeReformat.cpp#L87-L91) — `crop_flip_normalize_reformat(src, out_shape, out_dtype, out_layout, rect, flip_code, base, scale, globalscale, globalshift, epsilon, flags, border, bvalue, *, stream)`；[L96-L116](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpCropFlipNormalizeReformat.cpp#L96-L116) 的 docstring 写清了每个参数张量的形状合同（如 rect 为 `[batch_size,1,1,4]`）。

支持矩阵对比（替换判断的依据）：

[OpCropFlipNormalizeReformat.h:90-130](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/OpCropFlipNormalizeReformat.h#L90-L130) — 该算子的 Limitations：通道 1/3/4，输入输出 dtype 覆盖 8/16/32 位整型与 F32（比 `ResizeCropConvertReformat` 宽得多，后者仅 8U→8U/F32，见 [OpResizeCropConvertReformat.h:103-135](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/OpResizeCropConvertReformat.h#L103-L135)）。两个算子各查各的表，不能混用。

#### 4.4.4 代码实践

**实践目标**：跑通样例，并把「逐图参数」从 1 张图扩展到 2 张图，亲身体会参数张量化。

**操作步骤**：

1. 运行样例：
   ```bash
   python crop_flip_normalize_reformat.py --input <某张jpg> --output out_cfnr.jpg
   ```
2. 阅读输出回显逻辑 [crop_flip_normalize_reformat.py:111-122](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/operators/crop_flip_normalize_reformat.py#L111-L122)：host 端用 `raw = clip((normalized·std + mean)·255)` 反推回 uint8 只是为了保存查看。
3. 扩展实验：把批改成 2 张图（第二张可用同一张图的不同裁剪），`rect` 张量改成 `(2,1,1,4)` 且两行不同（第一张裁中央 80%，第二张裁左上 50%），`flip_code` 改成 `(2,1)` 且第一张 1（翻转）、第二张 2（不翻），输出 `out_shape=(2,3,...)`。运行并保存两张结果验证。（具体数值待本地验证。）

**需要观察的现象**：两张输出图的裁剪区域、翻转方向各自不同——一次调用、一个 kernel 完成了「每图不同参数」的数据增强组合。

**预期结果**：两组参数各自正确生效；若把某张图的 `crop_x+crop_w` 设成超过图宽，会在 CPU 侧收到参数校验异常。

#### 4.4.5 小练习与答案

**练习 1**：`globalscale` 与 `scale` 同时存在，为什么不合并成一个参数？

**答案**：`scale`（张量）逐通道、逐图，承载统计量（如 std）；`globalscale`（标量）全局统一，承载固定缩放（如 1/255）。二者职责正交，合并反而迫使你把 1/255 广播进每个通道张量，且无法独立于 base 更新。

**练习 2**：上一个算子输出 shape 由裁剪矩形推导，这个算子却要调用者传 `out_shape`，哪种设计更好？

**答案**：各有合理性。`crop_flip_normalize_reformat` 的 rect 是**每图一份的张量**，CPU 侧不下载它就无法推导统一输出尺寸，所以干脆让调用者声明；`resize_crop_convert_reformat` 的 crop_rect 是 host 常量，绑定层拿得到，替用户推导更省事。教训：读融合算子 API 时先弄清「参数在 host 还是 device」。

**练习 3**：样例为什么需要 `epsilon=1e-8`？

**答案**：`SCALE_IS_STDDEV` 语义下分母是 \(\sqrt{\text{scale}^2+\epsilon}\)；当某通道方差接近 0（如全黑通道）时避免除零，数值上等价于给方差加正则项（docstring 原文称之为 regularizing term）。

### 4.5 替换判断：你的管线能不能换成融合算子

#### 4.5.1 概念说明

融合算子不是万金油。它牺牲灵活性换取吞吐：步骤顺序固定、支持矩阵窄、参数空间有限。判断「能不能换、值不值得换」要对照 Limitations 契约表逐项核对。

#### 4.5.2 核心流程（决策清单）

```
1. 步骤匹配？你的变换序列能映射到融合算子的固定步骤序列吗
   （顺序不能调换；多出的步骤仍需独立算子）
2. dtype 匹配？ResizeCropConvertReformat: 输入必须 8bit 无符号，
   输出 8bit 无符号或 F32 —— 16bit/F16 输入直接出局
3. 通道匹配？1 或 3 通道（crop_flip 版允许 1/3/4）
4. layout 匹配？{H,W}×{C 先,C 后} 四种组合
5. 插值匹配？ResizeCropConvertReformat 只有 NEAREST/LINEAR
   （要 CUBIC/AREA 就得回独立 resize，见 .cu L649-L667 的显式拒绝）
6. 值得吗？图像小、批大、步骤多 → 收益大；
   单步或大图单算子 → 独立算子未必更慢
```

#### 4.5.3 源码精读

[OpResizeCropConvertReformat.h:103-146](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/OpResizeCropConvertReformat.h#L103-L146) — 完整契约表：输入 layout/通道/dtype 表（L105-L119）、输出表（L121-L135）、输入输出依赖表（L137-L146，注意「Number(批数) 与 Channels 必须相同，Layout/dtype/宽高可以不同」）。这些表与 `.cu` 里 L597-L667 的运行时校验一一对应——**头文件承诺什么，priv 层就检查什么**。

[OpCropFlipNormalizeReformat.h:90-130](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/OpCropFlipNormalizeReformat.h#L90-L130) — 第二张契约表（dtype 覆盖更宽、通道含 4、输出 layout 仅 NHWC/NCHW）。

#### 4.5.4 代码实践

**实践目标**：拿一条你自己的（或 u3-l2 练习写过的）管线过一遍决策清单。

**操作步骤**：

1. 写下你的管线步骤序列、每步的输入输出 dtype/layout/通道数。
2. 对照 4.5.3 的两张表逐项打勾/打叉。
3. 对不满足的项，标注「可改造」（如先 `convertto` 到 uint8）或「不可替换」。

**预期结果**：一张六行决策表；任何一项不可改造即结论为「保留独立算子管线，或只用融合算子替换其中的匹配子段」。

#### 4.5.5 小练习与答案

**练习 1**：管线是「resize(LINEAR) → centercrop → RGB2BGR → /255 → F32 → NCHW」，dtype 从 uint8 开始。能换吗？

**答案**：能，且是教科书式的匹配：`resize_crop_convert_reformat(src, resize_dim, LINEAR, crop_rect, layout="NCHW", data_type=F32, manip=REVERSE, scale=1/255)`（offset 取 0）。若想映射到 \([-1,1]\) 则 scale=1/127.5、offset=−1。

**练习 2**：同一管线但输入是 uint16 深度图，能换吗？

**答案**：不能。输入 dtype 表只允许 8bit 无符号；kernel 模板也只实例化了 `uchar1/uchar3` 源类型。只能先 `convertto` 成 uint8（有精度代价）或保留独立管线。

**练习 3**：为什么两个融合算子都不支持 CUBIC 插值，而独立 resize 支持？

**答案**：融合的收益来自「每像素一次读一次写」；CUBIC 需要 4×4 邻域共 16 次源读，gather 成本涨 4 倍，融合的带宽优势被稀释，且实现复杂度大增。工程取舍是先支持覆盖面最大的 NEAREST/LINEAR（.cu 中对 CUBIC/AREA 显式抛「not implemented」）。

## 5. 综合实践：三算子管线 vs 融合算子，一致性与耗时对比

这是本讲的主实践，把 4.1~4.5 的知识串成一次可量化的实验。

**实践目标**：对同一输入分别执行「独立算子五连」与「融合一次」，比较**数值一致性**与**耗时**，并解释差异来源。

**参考脚本框架**（示例代码，非仓库原有文件，保存为 `fused_vs_unfused.py` 放在任意可运行处）：

```python
import time
from pathlib import Path
import sys

import cvcuda
import numpy as np

sys.path.append(str(Path("samples")))  # 复用官方辅助函数
from common import read_image, download_tensor

ITER = 200
RESIZE_WH = (256, 256)          # (width, height)，见 4.2.3 的坐标约定讨论
CROP = cvcuda.RectI(32, 32, 224, 224)

src = read_image("samples/assets/tabby_tiger_cat.jpg")          # HWC uint8 RGB
src = src.reshape((1, *src.shape), layout="NHWC")               # -> (1,H,W,3)
stream = cvcuda.Stream()

def unfused():
    t = cvcuda.resize(src, (1, RESIZE_WH[1], RESIZE_WH[0], 3),      # resize 给完整输出 shape
                      cvcuda.Interp.LINEAR, stream=stream)
    t = cvcuda.customcrop(t, CROP, stream=stream)
    t = cvcuda.cvtcolor(t, cvcuda.ColorConversion.RGB2BGR, stream=stream)
    t = cvcuda.convertto(t, cvcuda.Type.F32, scale=1.0, offset=0.0, stream=stream)
    return cvcuda.reformat(t, "NCHW", stream=stream)

def fused():
    return cvcuda.resize_crop_convert_reformat(
        src, RESIZE_WH, cvcuda.Interp.LINEAR, CROP,
        layout="NCHW", data_type=cvcuda.Type.F32,
        manip=cvcuda.ChannelManip.REVERSE, scale=1.0, offset=0.0,
        srcCast=True,                 # 与独立 resize 的「回转源类型」行为对齐
        stream=stream)

# 一致性
a, b = unfused(), fused()
stream.sync()
ha, hb = download_tensor(a), download_tensor(b)   # 遵守 NCHW 行距，见 u2-l1
print("shape:", a.shape, b.shape)
print("max |diff|:", np.abs(ha - hb).max())

# 耗时（先预热再计时）
for _ in range(10):
    unfused(); fused()
stream.sync()
t0 = time.perf_counter()
for _ in range(ITER):
    unfused()
stream.sync()
t1 = time.perf_counter()
for _ in range(ITER):
    fused()
stream.sync()
t2 = time.perf_counter()
print(f"unfused: {(t1-t0)/ITER*1e3:.3f} ms/iter, fused: {(t2-t1)/ITER*1e3:.3f} ms/iter")
```

**操作步骤**：

1. 在有 GPU 与 cvcuda wheel 的环境运行上述脚本（`read_image` 的图片路径按你的仓库布局调整）。
2. 把结果填进表格：

| 版本 | kernel 启动次数/迭代 | 中间张量数 | 耗时 (ms/iter) | 最大像素差 |
|------|------------------|-----------|---------------|-----------|
| 独立五连（resize+crop+cvtcolor+convertto+reformat） | 5 | 4 | 待测 | — |
| 融合（resize_crop_convert_reformat） | 1 | 0 | 待测 | 待测 |

3. 附加实验 A：把融合版的 `srcCast` 改为 `False`，再看最大像素差——应该能看到 ±1 左右的量化级差异消失或变化。
4. 附加实验 B：把批维从 1 改成 32（同一张图 pushback 32 次或复制 32 份堆叠），重测耗时比。

**需要观察的现象与预期结果**（待本地验证）：

- 两版输出 shape 均为 `(1, 3, 224, 224)` float32。
- `srcCast=True` 时最大像素差应为 0 或极小（两版走同样的插值与饱和转换路径，仅浮点结合顺序可能引入微小差异）；`srcCast=False` 时会出现可测量的差异。
- 融合版每次迭代耗时显著低于独立版（减少 4 次 kernel 启动与 4 次中间结果往返），批越大优势越明显。

**思考题**（写给未来的你）：计时循环里为什么每轮结束要 `stream.sync()`，只在一个版本结束后同步行不行？（提示：算子只是把工作提交到流上，u4-l1 会给出完整答案。）

## 6. 本讲小结

- 融合算子把 resize、crop、scale/offset、通道重排、类型转换、布局重排六步折进**一个 CUDA kernel**：每输出像素一次 gather 读、一次写，中间结果只活在寄存器里。
- crop 没有独立步骤——它被折叠进采样坐标公式 \(s = (d + c + 0.5)\cdot r - 0.5\)；布局重排也没有专门代码——它被吸收进 `DstMap` 的四个 stride。
- `resize_crop_convert_reformat` 的 allocating 版在绑定层推导输出 shape（H/W 取自裁剪矩形），`_into` 版由 dst 张量自己声明合同；`resize_dim` 语义以 docstring 的 (width, height) 为准。
- `srcCast` 是融合带来的精度开关：`True` 与分步管线对齐，`False` 保留浮点插值精度避免量化。
- `crop_flip_normalize_reformat` 面向变长批与数据增强：rect/flip_code/base/scale 全部张量化、每图一份，配合 `SCALE_IS_STDDEV` 完成 `(pixel/255 − mean)/std` 经典归一化。
- 替换判断的唯一权威是各算子 C 头文件的 Limitations 契约表（输入 8U、通道 1/3、插值仅 NEAREST/LINEAR……），priv 层的运行时校验与之一一对应。

## 7. 下一步学习建议

- **u4-l1（Stream 执行模型）**：本讲所有调用都带 `stream=` 参数，综合实践里的 `sync()` 疑问将在那里解答——算子提交与完成是两件事。
- **u5-l1（算子四层结构）**：本讲已经预演了「绑定层 → priv 层 → kernel」的穿透式阅读；下一单元以 Flip 为例把这把钥匙形式化，你将能独立解剖任意算子。
- **u7-l3（基准测试）**：想把本讲的计时实验升级为可回归对比的正式基准（含 WarmupPolicy 与基线对比工具），读 [bench/python/ops/bench_resizecropconvertreformat.py](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/python/ops/bench_resizecropconvertreformat.py) 与 `bench/README.md`——官方已经有这个融合算子的基准脚本，可直接对比你的手写计时与正式基准的结论。
- 继续阅读源码的建议：对照 [OpResizeCropConvertReformat.cu:476-533](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpResizeCropConvertReformat.cu#L476-L533) 的变长批实现，观察它如何在 kernel 内逐图取宽高并重算采样比——这是 u2-l3「设备侧 imageList」概念的直接应用。
