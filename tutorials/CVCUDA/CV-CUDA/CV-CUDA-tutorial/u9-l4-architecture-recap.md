# 总结与进阶路线：架构取舍回顾

## 1. 本讲目标

这是整套学习手册的收官之讲。前面八個单元（35 讲）已经带你从 `pip install cvcuda-cu12` 一路走到 CUDA kernel 的 grid 划分。本讲不再引入新机制，而是做三件事：

1. **把 36 讲装回一张图**：复述 CV-CUDA 四层架构（Python 绑定层 → C API 层 → priv 实现层 → kernel 层，外加贯穿各层的 nvcv 类型层），并给出每一层的核心文件位置。
2. **梳理三个关键设计取舍**：C ABI 稳定性、变长批抽象、Python 缓存策略——理解「为什么这样设计」，才能预测「这样做会发生什么」。
3. **为三类读者（使用者 / 集成者 / 贡献者）规划后续深入方向**，并沉淀一份性能与正确性方面的关键实践清单。

学完本讲，你应该能凭一张自己画的知识地图在仓库里自由导航，并在遇到诡异行为（显存不降、输出错位、偶发数据竞争）时快速定位到对应的层与机制。

## 2. 前置知识

本讲是总结性讲义，默认你已读完（或至少浏览过）前面单元的核心结论。复习要点：

- **四层调用链**（u5-l1）：一次 `cvcuda.flip(...)` 依次穿过 Python 绑定层（pybind11 重载决议、输出分配）、C API 层（`extern "C"` 函数与不透明句柄）、priv 实现层（`exportData` 导出 CUDA 视图）、kernel 层（`<<<grid, block, 0, stream>>>`）。
- **nvcv 类型层**（u2 系列）：`Tensor`、`TensorShape`、`DataType`、`ImageFormat`、`ImageBatchVarShape`、`TensorBatch`——数据与操作分离，类型层被所有算子共享。
- **流执行模型**（u4-l1）：一切算子异步提交到 `cudaStream_t`；算子可在同步前析构，但**张量释放必须等流同步之后**。
- **对象缓存**（u4-l2）：仅 Python 层存在的 Tensor/ImageBatch 复用机制，配额默认为设备总显存一半。
- **Limitations 契约表**（u3 系列）：每个算子公开 C 头文件中声明的支持矩阵（布局/通道/dtype/输入输出依赖），是唯一的权威依据。
- **符号版本与错误双轨**（u6-l2）：`CVCUDA_DEFINE_API` 生成带版本后缀的 ELF 符号；`NVCVStatus` 返回码与 C++ 异常经 `ProtectCall` 互译。

如果以上任何一条你已经记不清，建议先回到对应单元扫一眼小结再继续。

## 3. 本讲源码地图

本讲涉及的关键文件（也是整份手册的「骨架文件」）：

| 文件 | 作用 | 在架构中的位置 |
|------|------|----------------|
| `README.md` | 项目定位、兼容矩阵、已知限制 | 全仓库门面 |
| `AGENTS.md` | 九大目录职责表、权威文档索引、仓库不变量 | 全仓库导航 |
| `python/mod_cvcuda/operators/OpFlip.cpp` | Python 绑定层样板：四连函数 + NVTX 包装 | 第 1 层（绑定） |
| `src/cvcuda/include/cvcuda/OpFlip.h` | C API + Limitations 契约表 | 第 2 层（C ABI） |
| `src/cvcuda/include/cvcuda/OpFlip.hpp` | C++ RAII 类（C API 的薄糖） | 第 2 层（C ABI） |
| `src/cvcuda/Operator.cpp`、`src/cvcuda/priv/SymbolVersioning.hpp` | 符号版本化导出 | 第 2 层（C ABI） |
| `src/cvcuda/priv/OpFlip.cpp` | priv 实现：exportData + 委托内核 | 第 3 层（实现） |
| `src/cvcuda/priv/legacy/flip.cu` | CUDA kernel 与启动配置 | 第 4 层（kernel） |
| `src/nvcv/src/include/nvcv/ImageBatch.hpp`、`TensorBatch.hpp` | 变长批抽象 | nvcv 类型层 |
| `python/mod_cvcuda/nvcv/Cache.cpp` | Python 对象缓存实现 | 绑定层专属机制 |
| `docs/sphinx/advanced/operator_variants.rst` | allocating 与 `_into` 官方文档 | 使用层契约 |
| `docs/sphinx/advanced/object_cache.rst` | 对象缓存官方文档 | 使用层契约 |

## 4. 核心概念与源码讲解

### 4.1 四层架构知识地图：把 36 讲装回一张图

#### 4.1.1 概念说明

CV-CUDA 的全部代码可以用「一条调用链 + 一个类型层」概括。理解这条链的最大好处是：**任何一个行为异常，都必然发生在其中一层，你可以逐层排查**。

- **Python 绑定层**（`python/mod_cvcuda/`）：pybind11 封装，做重载决议、输出分配、流兜底、资源记账（ResourceGuard）、NVTX 打点。只有这里有对象缓存。
- **C API 层**（`src/cvcuda/include/cvcuda/Op*.h`）：`extern "C"` 函数 + 不透明句柄，是最稳定的公共契约。C++ 类（`Op*.hpp`）只是它的零逻辑内联包装。
- **priv 实现层**（`src/cvcuda/priv/Op*.cpp|cu`）：把抽象 Tensor/ImageBatch 经 `exportData` 导出为 POD 视图，校验后委托内核。
- **kernel 层**（`src/cvcuda/priv/legacy/*.cu` 与 `src/cvcuda/priv/Op*.cu`）：真正发射 CUDA kernel 的地方，分 legacy 与原生两种形态。
- **nvcv 类型层**（`src/nvcv/`）：Tensor、ImageBatch、DataType、ImageFormat、Allocator 等被上三层共享的数据抽象，不含任何图像处理算法。

#### 4.1.2 核心流程

一次 `cvcuda.flip(tensor, 1)` 的完整穿越（u5-l1 的结论压缩版）：

```text
cvcuda.flip(src, 1)                          # 用户代码
  └─ python/mod_cvcuda/operators/OpFlip.cpp  # ① 绑定层：流兜底、Tensor::Create（查缓存）、ResourceGuard
       └─ cvcuda::Flip::submit(...)          # ② C++ 类内联转发
            └─ cvcudaFlipSubmit(handle, ...) # ③ C API：ProtectCall 边界（异常 ⇄ 错误码）
                 └─ cvcuda::priv::Flip::operator()  # ④ priv：exportData 导出 CUDA 视图、判空校验
                      └─ legacy::Flip::infer(...)   # ⑤ 内核包装：dtype×通道分派表
                           └─ flipHorizontal<<<grid, block, 0, stream>>>(...)  # ⑥ kernel
```

每一层的「职责边界」就是排查问题时的「断点位置」。

#### 4.1.3 源码精读

先看项目自己的定位陈述——一句话说清 CV-CUDA 是什么：

> CV-CUDA is an open-source library of GPU-accelerated computer vision algorithms designed for speed and scalability.（[README.md:L29](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/README.md#L29)）

仓库官方导航表把九大顶层目录的职责写死在 [AGENTS.md:L23-L35](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/AGENTS.md#L23-L35)：`src/` 是「C++ core library, C API, private operator implementations, and nvcv types」，`python/` 是「pybind11-based Python bindings and wheel packaging」，`tests/`、`bench/`、`samples/` 分别回答对错、快慢、用法。

四层各自的代表性文件（以 Flip 为例，全部在当前 HEAD 下验证存在）：

| 层 | 文件 | 关键代码 |
|----|------|----------|
| ① 绑定 | `python/mod_cvcuda/operators/OpFlip.cpp` | [L55-L60](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpFlip.cpp#L55-L60)：allocating 变体 `Tensor::Create` 后委托 `FlipInto` |
| ② C ABI | `src/cvcuda/include/cvcuda/OpFlip.h` | [L54](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/OpFlip.h#L54)：`cvcudaFlipCreate`；[L117-L118](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/OpFlip.h#L117-L118)：`cvcudaFlipSubmit` |
| ② C++ 薄糖 | `src/cvcuda/include/cvcuda/OpFlip.hpp` | [L66-L70](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/OpFlip.hpp#L66-L70)：`operator()` 内联调用 `cvcudaFlipSubmit` |
| ③ priv | `src/cvcuda/priv/OpFlip.cpp` | [L39-L57](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpFlip.cpp#L39-L57)：`exportData` 导出并判空 |
| ④ kernel | `src/cvcuda/priv/legacy/flip.cu` | [L59](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/legacy/flip.cu#L59)：`__global__ void flipHorizontal`；[L344](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/legacy/flip.cu#L344)：`<<<gridSize, blockSize, 0, stream>>>` |

规模感（当前 HEAD `5ac8708b` 下用 `ls | wc -l` 实测）：

- `src/cvcuda/include/cvcuda/Op*.h`：**62** 个算子 C 头文件；
- `python/mod_cvcuda/operators/Op*.cpp`：**61** 个 Python 绑定文件；
- `src/cvcuda/priv/legacy/*.cu`：**57** 个 legacy 内核编译单元。

数字接近但不相等（例如 IOperator 等非算子头文件、部分内核合并编译），这本身就是「按命名规律检索」可靠的证据（u1-l4）。

#### 4.1.4 代码实践

**实践目标**：不看任何讲义，独立完成一次「四层定位」训练，检验知识地图是否真正内化。

**操作步骤**：

1. 任选一个你没读过的算子（建议 `OpErode` 或 `OpMorphology`，用 `rg -l "ExportOpErode" python/` 确认存在）。
2. 仅凭命名规律写出它的四层文件路径（绑定 .cpp、C 头 .h、C++ 头 .hpp、priv 实现、内核 .cu）。
3. 用 `ls` / `rg` 逐个验证路径是否存在；对不存在的（例如该算子可能没有独立内核文件，内核合并在形态学族文件里），记下实际位置。
4. 在每一层文件里用 `rg -n "Erode"` 找到该算子的第一个函数/类定义，记录行号。

**需要观察的现象**：哪些层可以纯靠命名直推（①②③），哪些层必须靠 `rg` 搜类名定位（④，legacy 目录按功能族组织）。

**预期结果**：得到一张 5 行速查表（层 × 文件 × 入口行号）。④ 层的内核大概率不在 `erode.cu` 里——这正是 u1-l4 讲过的「legacy 目录按功能族组织」的活例子。

#### 4.1.5 小练习与答案

**练习 1**：为什么说「数据与操作分离」是理解本仓库的钥匙？

**参考答案**：所有数据容器（Tensor、ImageBatchVarShape、TensorBatch、Array）定义在 `src/nvcv`，被绑定层、C API、priv、kernel 四层共享引用；算子（`src/cvcuda`）只消费这些类型而不拥有数据。因此改一个数据类型（如给 Tensor 加布局推导）会波及全部 61 个算子，而加一个算子不会动任何数据类型。掌握了类型层，读任何算子的签名都没有障碍。

**练习 2**：一次 `cvcuda.flip` 调用中，哪一层是异常与错误码的翻译边界？为什么必须是它？

**参考答案**：C API 层（`extern "C"` 函数体里的 `nvcv::ProtectCall`）。因为 C ABI 不允许异常穿越——C 调用方无法展开 C++ 栈。所以每个 C API 函数都用 `ProtectCall` 包住 lambda，把 C++ 异常翻译成 `NVCVStatus` 返回码并写入线程局部存储。

**练习 3**：`python/mod_cvcuda/operators/OpFlip.cpp` 里 `Flip`（allocating）与 `FlipInto`（`_into`）是什么关系？

**参考答案**：`Flip` 只是 `FlipInto` 的薄包装——先 `Tensor::Create(input.shape(), input.dtype())` 分配输出，再委托 `FlipInto`。这与文档 `operator_variants.rst` 的描述一一对应（见 4.4 节精读）。

### 4.2 设计取舍一：以 C ABI 为轴心的稳定性设计

#### 4.2.1 概念说明

CV-CUDA 最核心的工程决策是：**公共契约建立在 C ABI 之上，而不是 C++ API 之上**。原因：

1. C ABI 极其稳定——符号名、调用约定、结构体布局不随编译器版本和 STL 实现变化；
2. 任何语言（Python、Rust、Go、纯 C 项目）都能绑定 C ABI；
3. C++ 没有稳定的二进制 ABI，跨编译器版本链接 C++ 库是雷区。

代价是 C 侧的错误处理只能靠返回码（无异常）、生命周期靠手动句柄（无 RAII）。CV-CUDA 的解法是「双轨制 + 翻译边界 + 符号版本」：

- C 侧：`NVCVStatus` 返回码 + 线程局部的「最后错误」消息；
- C++ 侧：`nvcv::Exception`（携带 Status 与消息的 RAII 异常）；
- 边界：每个 `extern "C"` 函数体都是一层 `ProtectCall` 翻译；
- 版本：`CVCUDA_DEFINE_API` 让实现库升级不破坏老程序（ELF 符号版本化）。

#### 4.2.2 核心流程

错误从 kernel 附近产生到被用户看到的路径：

```text
priv 层抛 nvcv::Exception(Status, msg)
  │  （异常沿 C++ 栈向上传播，但不能穿越 extern "C"）
  ▼
C API 函数体：nvcv::ProtectCall([&]{ ... })
  │  捕获异常 → SetThreadError 写入 TLS → 返回 NVCVStatus
  ▼
┌─ C 调用方：检查返回码，需要详情时读 TLS 错误消息
└─ C++/Python 调用方：CheckThrow 把非 SUCCESS 的返回码再抛回异常 / pybind11 转成 Python 异常
```

符号版本化的效果（示意）：

\[ \texttt{cvcudaFlipSubmit} \;\longrightarrow\; \underbrace{\texttt{cvcudaFlipSubmit\_v0\_3}}_{\text{真实符号（带版本后缀）}} \;+\; \underbrace{\texttt{cvcudaFlipSubmit@@CVCUDA\_0.3}}_{\text{默认版本别名}} \]

新版本库改了签名时，旧签名以单 `@`（非默认版本）保留，链接到旧符号的老程序照常运行。

#### 4.2.3 源码精读

翻译边界的最简实例——整库唯一的通用析构函数，两行讲完「C ABI 纪律」：

[Operator.cpp:L26-L29](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/Operator.cpp#L26-L29) 用 `CVCUDA_DEFINE_API(0, 3, void, nvcvOperatorDestroy, ...)` 定义符号，函数体只有一句 `nvcv::ProtectCall([&handle]{ priv::DestroyOperatorHandle(handle); })`——版本化声明在前，异常翻译在内。

[SymbolVersioning.hpp:L23-L24](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/SymbolVersioning.hpp#L23-L24) 把项目级宏 `CVCUDA_DEFINE_API` / `CVCUDA_DEFINE_OLD_API` 委托给通用的 `NVCV_PROJ_DEFINE_API`，所有算子的 C API 共用同一套版本化机制。

`ProtectCall` 本体在 [Exception.hpp:L244-L253](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/Exception.hpp#L244-L253)：执行 lambda，捕获任何异常后调用 `SetThreadError` 记录并返回错误码（`bad_alloc → OUT_OF_MEMORY`，其余 → `INTERNAL`）。

稳定契约的「正文」则写在每个算子 C 头的文档注释里。以 Flip 为例，[OpFlip.h:L56-L118](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/OpFlip.h#L56-L118) 在 `cvcudaFlipSubmit` 的注释中给出完整 Limitations 契约表：允许的布局 `[kNHWC, kHWC, kNCHW, kCHW]`、通道 `[1, 3, 4]`、逐项列出每个 dtype 是否允许（U8 ✅ / S8 ❌ / U16 ✅ / F16 ❌ / F32 ✅…）、输入输出必须同形同 dtype，以及 `@retval` 错误码契约。**这张表同时是测试值表、金标实现范围与文档的唯一事实来源**（u7-l1、u8-l1 的结论）。

#### 4.2.4 代码实践

**实践目标**：用系统工具亲眼看到「符号版本」不是纸面概念。

**操作步骤**（在有 CV-CUDA 共享库的环境，或自行编译 `build-rel/lib/libcvcuda.so` 之后）：

```bash
# 1. 构建库（若已有构建产物可跳过）
cmake --preset dev && cmake --build --preset dev

# 2. 查看导出符号的版本信息
nm -D build-rel/lib/libcvcuda.so | grep cvcudaFlipSubmit
# 或更直观：
objdump -T build-rel/lib/libcvcuda.so | grep cvcudaFlipSubmit
```

**需要观察的现象**：`nm -D` 输出中 `cvcudaFlipSubmit` 附近出现带 `_v` 后缀的真实符号；`objdump -T` 的版本列出现 `CVCUDA_x.y` 字样。

**预期结果**：确认公开符号带版本标签。若你的环境中无 GPU / 未构建，此步为「待本地验证」，可改为纯阅读实践：`rg -n "CVCUDA_DEFINE_API" src/cvcuda/Op*.cpp src/cvcuda/Operator.cpp | head -20`，统计有多少 C API 函数套用了版本化宏，并抽查一个函数确认函数体是 `ProtectCall` 包装。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `cvcuda::Flip` 的 `operator()` 写在头文件里且只有一行？

**参考答案**：公开 C++ 类被定位为「零逻辑薄糖」：构造函数调用 `cvcudaFlipCreate` 并把句柄交给 RAII 的 `detail::OperatorHandle`，`operator()` 内联转发到 `cvcudaFlipSubmit`（见 [OpFlip.hpp:L66-L70](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/OpFlip.hpp#L66-L70)）。全部逻辑在 priv 层，头文件内联保证无额外调用开销，也让 `.h` 与 `.hpp` 可以互相推导。

**练习 2**：错误码枚举为什么「只能在末尾追加」？

**参考答案**：`NVCVStatus` 的数值是 ABI 的一部分——C 调用方按数值比较返回码。在中间插入或重排会使老二进制对同一错误得到不同解释。末尾追加则任何既有数值永远不变，这与 ELF 符号版本化是同一条纪律的两个侧面。

**练习 3**：Python 用户会看到 `NVCVStatus` 吗？

**参考答案**：通常不会。pybind11 绑定层调用 C++ 类，`CheckThrow` 把非 SUCCESS 返回码重新抛成 `nvcv::Exception`，再被 pybind11 翻译成 Python 异常。错误消息即 TLS 中记录的那条（`Peek/Get` 语义见 u6-l2）。

### 4.3 设计取舍二：变长批抽象——为不规则数据付出的代价与回报

#### 4.3.1 概念说明

真实视觉管线的输入天然不规则：视频帧尺寸不一、解码批内图片大小各异。如果只有规则 Tensor 批（NHWC），就必须把所有图 padding 到同一画布，浪费率随尺寸离散度急剧上升（u2-l3 的结论）。

CV-CUDA 的回答是 **ImageBatchVarShape**：一个「句柄容器」——不拥有像素，只持有若干 `Image` 的引用，逐图保留尺寸/格式元数据。回报是零 padding 流转、仅在推理边界付一次对齐代价；代价是：

1. kernel 不能再用统一的 `(N,H,W,C)` 寻址，必须先经 `exportData(stream)` 导出「主机侧尺寸表 + 设备侧每图基址表」的双面结构（且导出会向流调度拷贝，需要 stream 参数）；
2. allocating 变体的输出批要逐图重建（`CreateSameShapeImageBatch`），开销随批内图数线性增长——这是变长批从 `_into` 获益更大的根源（u3-l3）；
3. 逐图可变的参数（如 flipCode）必须「张量化」为 N 元素张量。

`TensorBatch` 则是同一思想在张量域的推广：元素形状可不同，但 rank/dtype/layout 是批级单一属性、不可混。

#### 4.3.2 核心流程

变长批数据从容器到 kernel 的路径：

```text
ImageBatchVarShape（句柄容器，逐图元数据）
  │ exportData(stream)          ← 注意：必须给流！导出会入队 H2D 元数据拷贝 + event 栅栏
  ▼
ImageBatchVarShapeDataStridedCuda（双面 POD 视图）
  ├─ 主机侧：hostFormatList / maxWidth  → 决定 kernel 启动配置
  └─ 设备侧：imageList（每图每平面基址+行距）→ kernel 按图索引寻址
  ▼
kernel：一图一（或多个）block，block 内按该图真实尺寸循环
```

对比规则 Tensor 批：`exportData` 无需 stream（元数据纯主机侧可导），kernel 用统一 grid 覆盖整个批。「是否需要 stream 参数」本身就是判断一个容器是否变长批的快捷信号。

#### 4.3.3 源码精读

变长批类的自我陈述：[ImageBatch.hpp:L157-L164](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/ImageBatch.hpp#L157-L164) 声明 `ImageBatchVarShape : public ImageBatch`，文档注释直说它「支持批内每张图像形状不同」。

它的两个关键查询方法：[ImageBatch.hpp:L226-L238](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/ImageBatch.hpp#L226-L238)——`maxSize()` 返回批内最大画布（stack/pad 的目标尺寸），`uniqueFormat()` 在批内格式统一时返回该格式、不统一时返回无效格式（这正是「容器层允许混格式、算子层多数要求 uniqueformat」约束的落点，u2-l3）。

张量域的对应物：[TensorBatch.hpp:L40-L45](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/TensorBatch.hpp#L40-L45) 声明 `TensorBatch` 同样是句柄式 `CoreResource`；而 [TensorBatch.hpp:L153](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/TensorBatch.hpp#L153) 的 `TensorBatchData exportData(CUstream stream) const;` 显式要求流参数——签名本身就印证了「导出会与流调度交互、流完成后结构才有效」（u2-l3）。

绑定层对变长批的「代价」体现在输出侧：[OpFlip.cpp:L83-L88](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpFlip.cpp#L83-L88) 中 `FlipVarShape`（allocating）必须调用 `CreateSameShapeImageBatch(input)` 逐图重建输出批；而 Tensor 版的 `Flip`（[L55-L60](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpFlip.cpp#L55-L60)）只需一次 `Tensor::Create`。同时注意变长批版 flipCode 的类型是 `Tensor &`（[L62-L64](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpFlip.cpp#L62-L64)）——「逐图参数张量化」的直接证据。

#### 4.3.4 代码实践

**实践目标**：用「签名考古」验证变长批的代价模型，纯源码阅读即可完成，无需 GPU。

**操作步骤**：

1. `rg -n "exportData" src/nvcv/src/include/nvcv/Tensor.hpp src/nvcv/src/include/nvcv/ImageBatch.hpp src/nvcv/src/include/nvcv/TensorBatch.hpp`，记录每个 `exportData` 是否带 stream 参数。
2. 任选三个算子的 priv 实现（如 `OpFlip.cpp`、`OpResize.cpp`），对比 Tensor 入口与 ImageBatchVarShape 入口各自调用 `exportData` 的写法差异（变长批要传 stream，Tensor 不传）。
3. `rg -n "CreateSameShapeImageBatch" python/mod_cvcuda/operators/ | head` 统计有多少绑定文件在 allocating 变长批入口做了逐图重建。

**需要观察的现象**：Tensor 的 `exportData` 无流参数；ImageBatchVarShape / TensorBatch 的有。几乎所有算子的变长批 allocating 入口都出现 `CreateSameShapeImageBatch`。

**预期结果**：得到「变长批三代价」的源码证据清单各一条（导出需流、双面结构、输出逐图重建）。全程可离线完成。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `ImageBatchVarShape::exportData` 需要 stream 而 `Tensor::exportData` 不需要？

**参考答案**：Tensor 的形状/stride 元数据在创建时就固定且在主机侧可读，导出只是把元数据抄进 POD 结构；变长批的逐图元数据（每图基址等）需要上传到设备侧供 kernel 索引，这次 H2D 拷贝被入队到流上并配 event 栅栏，因此导出本身成为流上的操作，必须给定流（导出结构的有效性依赖流完成）。

**练习 2**：批内一张 RGB8、一张灰度 Y8，`uniqueFormat()` 返回什么？这样的批能直接喂给 flip 吗？

**参考答案**：返回无效格式。容器层允许混格式，但算子的 Limitations 契约通常要求批内格式统一（校验在 priv 层），因此会被拒绝。应先统一格式（如 cvtcolor）再组批。

**练习 3**：同样用 `_into`，为什么变长批比规则 Tensor 批节省更多？

**参考答案**：规则 Tensor 的 allocating 只多一次缓存查找 + 可能一次分配；变长批的 allocating 还要逐图重建输出批（外壳对象 + 元数据组装），开销随批内图数线性增长。`_into` 把这部分整体消除。

### 4.4 设计取舍三：Python 缓存策略——便利与开销的分界线

#### 4.4.1 概念说明

三个设计取舍中，这一个最「厚此薄彼」：**对象缓存只存在于 Python 绑定层，C/C++ 完全没有**（[object_cache.rst:L25-L27](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/docs/sphinx/advanced/object_cache.rst#L25-L27) 的注意事项原话：「Only Python objects are cached, there is no C/C++ object caching」）。

为什么只有 Python 有？因为 Python 用户高频写 `out = cvcuda.op(src)` 这种隐式分配代码，若无复用机制，紧循环会反复 cudaMalloc/free。C/C++ 用户则被期望自己管理缓冲。设计要点：

- **只缓存「非包装」对象**（CV-CUDA 分配的内存）；包装外部内存的对象不占配额（[object_cache.rst:L36-L48](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/docs/sphinx/advanced/object_cache.rst#L36-L48)）；
- **`del` 不释放显存**：只剩缓存一个引用时对象进入待复用状态，所以「del 后 nvidia-smi 不降」是设计而非泄漏（[object_cache.rst:L53-L62](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/docs/sphinx/advanced/object_cache.rst#L53-L62)）；
- **配额触发整体清空**（非 LRU）：默认每设备半张卡，超限即清空该设备条目（[object_cache.rst:L79-L107](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/docs/sphinx/advanced/object_cache.rst#L79-L107)）；
- **线程局部表、全局共享账**（[object_cache.rst:L120-L129](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/docs/sphinx/advanced/object_cache.rst#L120-L129)）。

与它对偶的是 **allocating / `_into` 两变体**：allocating 每次调用都发生「缓存查找或分配」，`_into` 完全不触碰缓存（[operator_variants.rst:L36-L53](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/docs/sphinx/advanced/operator_variants.rst#L36-L53)）。两者合起来构成一条清晰的「便利 ↔ 开销」光谱。

#### 4.4.2 核心流程

两种变体在绑定层的分叉（以 flip 为例）：

```text
cvcuda.flip(src, code)            cvcuda.flip_into(dst, src, code)
  │                                      │
  ▼ Tensor::Create(shape, dtype)         │ （无分配、无缓存交互）
  │ └─ 查对象缓存：命中→复用；未命中→分配并入缓存
  ▼                                      ▼
  └──────────────► FlipInto(output, input, code, stream) ◄──────┘
                         │ 流兜底 / CreateOperator（算子对象也走缓存）
                         │ ResourceGuard 登记 READ/WRITE/NONE
                         └─ guard.run → Flip->submit(stream, ...)
```

使用决策（官方文档 [operator_variants.rst:L58-L70](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/docs/sphinx/advanced/operator_variants.rst#L58-L70) 的规则）：

| 场景 | 选择 | 理由 |
|------|------|------|
| 原型 / 一次性脚本 | allocating | 代码最短 |
| 输出形状逐次变化 | allocating | 缓存替你处理变形状 |
| 固定形状推理管线 | `_into` | 启动时分配一次，循环内零开销 |
| 紧循环 | `_into` | 消除每次迭代缓存开销 |
| 自管缓冲池 / 需确定性显存行为 | `_into` | 行为完全可预测 |

#### 4.4.3 源码精读

文档与代码的对应关系一目了然。文档说 allocating 变体内部调用 `Tensor.Create` 并查缓存（[operator_variants.rst:L36-L41](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/docs/sphinx/advanced/operator_variants.rst#L36-L41)），代码里 [OpFlip.cpp:L55-L60](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpFlip.cpp#L55-L60) 正是 `Tensor output = Tensor::Create(input.shape(), input.dtype()); return FlipInto(...)`——两行、零额外逻辑。

文档说 `_into` 不查缓存、返回值即传入的 `dst`（[operator_variants.rst:L46-L53](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/docs/sphinx/advanced/operator_variants.rst#L46-L53)、[L92-L102](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/docs/sphinx/advanced/operator_variants.rst#L92-L102)），代码里 [OpFlip.cpp:L35-L53](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpFlip.cpp#L35-L53) 的 `FlipInto` 全过程只有：流兜底 → `CreateOperator<cvcuda::Flip>(0)`（算子对象缓存）→ `ResourceGuard` 按 READ（input）/WRITE（output）/NONE（算子）登记 → `guard.run` 提交 → `return output`。**没有出现任何 Tensor::Create**。

缓存实现侧的两个关键证据（[Cache.cpp:L78-L94](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Cache.cpp#L78-L94)）：

- `isInUse()` 的判据是 `use_count() > 2`——只有缓存和本次查询两个引用时才算「闲置可复用」，这就是「del 后内存不释放但可被复用」的机制根源；
- `Impl` 中 `cache_limit_inbytes` / `current_size_inbytes` 是 `inline static` 且按 `int`（设备号）为键——配额跨线程共享、按设备记账，与官方文档「线程局部表、全局共享账」完全一致。

#### 4.4.4 代码实践

**实践目标**：用官方样例亲眼确认缓存三条行为：复用、del 不降、失控增长与配额。

**操作步骤**（需安装 cvcuda wheel，纯 Python、无需重新编译）：

```bash
cd samples/object_cache
python basic.py          # 非包装对象计入缓存
python basic_wrapped.py  # 包装对象不计入
python reuse.py          # 相同规格的新对象复用旧内存
python unbounded_growth.py   # 变 shape 循环 → 缓存无限增长
python control.py        # set_cache_limit_inbytes 设配额
python threads.py        # 线程局部缓存行为
```

配合观察：

```python
import cvcuda
print(cvcuda.cache_size(cvcuda.ThreadScope.LOCAL))   # 查询线程局部缓存大小
cvcuda.clear_cache()                                  # 手动清空
```

**需要观察的现象**：`reuse.py` 相同 shape/dtype 创建不产生新分配；`unbounded_growth.py` 中缓存大小持续爬升；`control.py` 设置限额后触顶自动清空。

**预期结果**：记录每个脚本的缓存字节数变化曲线（截图或打印即可）。若无 GPU 环境，此实践为「待本地验证」，可替代为源码阅读：对照 [Cache.cpp:L78-L84](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Cache.cpp#L78-L84) 解释 `use_count() > 2` 的三个引用分别是谁。

#### 4.4.5 小练习与答案

**练习 1**：固定形状的推理管线里，allocating 变体「稳态近零分配」的说法与「每次调用都有缓存查找开销」矛盾吗？

**参考答案**：不矛盾。稳态下形状不变，缓存命中，不再发生 cudaMalloc（分配近零）；但每次调用仍要付哈希查找 + 引用计数检查 + 可能的锁开销（查找非零）。`_into` 连查找也省掉，且行为确定。「分配」与「开销」是两个概念。

**练习 2**：为什么包装对象（as_tensor 包装的 numpy/torch 显存）不计入缓存配额？

**参考答案**：包装对象不拥有内存——显存属于原框架（torch 的缓存分配器 / cupy 内存池），CV-CUDA 只是登记指针与元数据。把它计入自己的配额等于替别人的账本记账。包装对象进缓存仅为异步流保活与外壳复用（u4-l2）。

**练习 3**：多线程服务里每个线程各建各的 Tensor，缓存配额会按线程数翻倍吗？

**参考答案**：表按线程隔离（每线程一张哈希表），但配额与已用字节数是全局静态共享的（`Impl::cache_limit_inbytes` 为 `inline static`，见 [Cache.cpp:L88-L94](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Cache.cpp#L88-L94)）。所以配额不会翻倍，但一个线程触顶会清空该设备条目，影响所有线程的命中率——这正是官方文档警告多线程需谨慎的原因。

### 4.5 三类读者的进阶路线与关键实践清单

#### 4.5.1 概念说明

同一份仓库，三类读者读法完全不同：

- **使用者（Python 应用工程师）**：目标是正确、快地搭管线。需要精通的是类型层语义（Tensor/布局/stride）、算子支持矩阵（Limitations 表）、流与缓存的使用纪律。几乎不需要读 C++。
- **集成者（框架/服务开发者）**：把 CV-CUDA 嵌进更大的系统（TensorRT/DeepStream/自研服务）。额外需要：C/C++ API 与句柄生命周期、DLPack/CAI 互操作、错误处理、多 GPU/多线程语义、基准与回归门禁。
- **贡献者（算子/内核开发者）**：新增或优化算子。额外需要：四层链路全通、mkop 脚手架与门禁、金标测试范式、nvbench 基线、NVTX/Nsight 分析、以及仓库不变量（SPDX、CUDA 12/13 配对）。

#### 4.5.2 核心流程

三类读者的「最小必读集」与手册单元的映射：

| 读者 | 必读单元 | 必读源码/文档 | 核心技能 |
|------|----------|---------------|----------|
| 使用者 | u1、u2、u3、u4-l1/l2、u9 | `docs/sphinx/advanced/operator_variants.rst`、`object_cache.rst`、samples/ | as_tensor 零拷贝、变体选择、流同步纪律、缓存配额 |
| 集成者 | 上述 + u4-l3、u5-l1/l2、u6 全部、u7-l3/l4 | `OpFlip.h/.hpp`、`Exception.hpp`、interoperability samples | C ABI 句柄生命周期、错误码、多流并发、基准回归 |
| 贡献者 | 全部，重点 u5、u7、u8 | `.agents/guidance/MAKE_OP_GUIDELINES.md`、`tools/mkop/`、`tools/make_op.py`、tests/、bench/ | 四层实现、金标测试、门禁工具链、优化纪律 |

贡献者的权威入口写在 [AGENTS.md:L37-L56](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/AGENTS.md#L37-L56) 的「Authoritative docs」一节：新算子看 `MAKE_OP_GUIDELINES.md`、覆盖评审看 `REVIEW_OP_GUIDELINES.md`、优化看 `OPTIMIZATION_GUIDELINES.md`、重构看 `REFACTOR_OP_GUIDELINES.md`。注意该文件同时声明 [AGENTS.md:L8-L10](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/AGENTS.md#L8-L10)：`AGENTS.md` 是规范入口、`CLAUDE.md` 是它的符号链接。

#### 4.5.3 源码精读

性能与正确性的「合同条款」沉淀在三份文档里，值得反复回看：

1. **支持矩阵合同**：每个算子 C 头的 Limitations 表（范本 [OpFlip.h:L56-L118](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/OpFlip.h#L56-L118)）。写管线前先查表，比看报错快。
2. **变体合同**：[operator_variants.rst:L92-L102](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/docs/sphinx/advanced/operator_variants.rst#L92-L102) 的约束条款——`_into` 的 dst 必须已具备算子将产出的正确 shape/layout/dtype，不兼容即抛异常；所有标准算子都有 `_into`，变长批重载同理。
3. **仓库不变量**：[AGENTS.md:L71-L88](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/AGENTS.md#L71-L88)——新文件必须带 NVIDIA Apache 2.0 SPDX 头；requirements 由模板生成不可手改；CUDA 12/13 改动须配对；图像类算子默认须同时支持交错与平面布局（不适用须在 Limitations 中声明 Not applicable 并给 Reason）。验证阶梯见 [AGENTS.md:L90-L115](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/AGENTS.md#L90-L115)：「用能证明改动的最窄验证，并如实说明跑了什么、没跑什么」。

平台与版本的红线则在 README：[README.md:L90-L99](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/README.md#L90-L99) 的已知限制——cu12 与 cu13 包不可共存（切换须先卸载）、samples 仅官方支持 CUDA 12、OSD 文本渲染在 Jetson/aarch64 有已知问题。

#### 4.5.4 代码实践

**实践目标**：为自己（明确身份：使用者/集成者/贡献者）生成一张「最小必读清单」，检验路线图的针对性。

**操作步骤**：

1. 确定你的读者身份（或选最接近的一个）。
2. 按上表勾选必读单元，删去与你无关的行。
3. 对每个必读源码文件，用 `rg -n` 找到一个你最想搞懂的函数，记下「文件:行号 + 一句话疑问」。
4. 把清单保存为 `my-cvcuda-roadmap.md`（放在本教程目录外自己的笔记区），下周回来检查疑问是否都被解答。

**需要观察的现象**：贡献者身份的清单里会出现大量 `tools/` 与 `tests/` 文件，而使用者身份的清单几乎全是 `samples/` 与 `docs/`——两份清单的差异就是「进阶方向」的具象化。

**预期结果**：一份 10~20 行的个人路线图，每行可执行（有明确文件与疑问），而非泛泛的「深入学习」。

#### 4.5.5 小练习与答案

**练习 1**：一个 Python 使用者报告「循环处理 1000 帧后 OOM」。按读者视角给出排查顺序（不读 C++ 源码）。

**参考答案**：① 输出形状是否逐帧变化（变 shape → 缓存无限增长，见 `unbounded_growth.py`）；② 是否忘了让中间结果离开作用域（引用计数 >2 则永不复用）；③ 缓存配额是否远小于工作集（`cache_size` / `set_cache_limit_inbytes`）；④ 是否本该用 `_into` 预分配。全部都是 u4-l2 的使用层知识。

**练习 2**：集成者要在纯 C 服务里用 CV-CUDA。生命周期上最容易被忽视的一条纪律是什么？

**参考答案**：算子句柄可以在流同步前析构（提交后库不再需要它），但**张量的释放必须等到流上的工作完成之后**——先 `cudaStreamSynchronize` 再 `nvcvTensorDecRef`。这是 u6-l1 的铁律，C 侧没有 RAII 提醒你。

**练习 3**：贡献者提交的 PR 改了某算子对 planar 布局的支持。仓库不变量里哪一条会被 review_op.py 拿来对账？

**参考答案**：「图像类算子默认须同时支持交错（NHWC/HWC）与平面（NCHW/CHW）布局；若不适用须在算子公开 C 头的 Limitations 契约中声明 Not applicable 并给 Reason」（[AGENTS.md:L83-L86](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/AGENTS.md#L83-L86)）。检查器会跨 support/test/bench/docs 四个表面对账这条契约。

## 5. 综合实践

**任务：编写属于你自己的《CV-CUDA 速查手册》**——这是全手册的毕业设计，产物是你未来真正会用的那份文档。

### 5.1 任务说明

1. **10 条易错清单**：下面给出参考范本（每条都来自前面单元的实测结论）。你的任务不是照抄，而是**逐条验证**：为每条写一个 10 行以内的最小复现脚本（或标注对应的官方 sample / 源码行号作为证据），并补上你自己的定位方法。验证不了的标「待本地验证」。
2. **3 个深读文件**：从全仓库选出 3 个你最想深入阅读的源码文件，各写 3~5 行理由（为什么是它、你期望从它学到什么、它连接了哪些层）。

### 5.2 参考范本：10 条最易犯的错误与定位方法

| # | 错误 | 现象 | 定位方法 | 出处 |
|---|------|------|----------|------|
| 1 | **CPU 侧读回前忘记流同步** | 读到旧数据/全零 | Nsight Systems 里 CUDA 行算子尚未执行完，CPU 侧读取区间在前；在读取前加 `stream.sync()` 验证 | u4-l1 |
| 2 | **张量在流同步前释放** | 偶发错值或非法访存 | 算子句柄可先析构、张量不行；检查 `nvcvTensorDecRef`/Python 引用归零是否早于同步点 | u6-l1 |
| 3 | **`del` 后显存不降，误判泄漏** | nvidia-smi 占用不变 | `cvcuda.cache_size()` 看缓存；`cvcuda.clear_cache()` 后对比；机制是 `use_count() > 2` 判据（Cache.cpp:L78-L84） | u4-l2 |
| 4 | **变 shape 循环导致缓存无限增长** | 显存持续爬升直至 OOM | 跑 `samples/object_cache/unbounded_growth.py` 复现；对策：形状分桶 / `_into` / 限额 | u4-l2 |
| 5 | **紧循环用 allocating 变体** | 吞吐低于预期 | 换 `_into` + 预分配对比（u3-l3 实践）；变长批差距更大（逐图重建输出批） | u3-l3 |
| 6 | **`_into` 的 dst 规格不符** | CPU 侧抛异常 | dst 必须与算子产出 shape/layout/dtype 完全一致（operator_variants.rst:L92-L102） | u3-l3 |
| 7 | **用 CPU 数组直接 `as_tensor`** | TypeError/被拒 | 设备白名单仅放行显存/托管/页锁定；先用 `.cuda()` / cupy / DLPack | u2-l4、u9-l3 |
| 8 | **忽略真实 stride 切片/切批** | 图像错位、绿边 | 打印 `tensor.stride()`；行距默认对齐到设备纹理对齐（常 32B），跨框架必须按真实行距切 | u2-l1、u1-l2 |
| 9 | **不查 Limitations 表用错 dtype/布局** | `ERROR_INVALID_ARGUMENT` | 查算子 C 头契约表（范本 OpFlip.h:L56-L118）；如 flip 的 S8/F16 不支持、resize LINEAR 要求源 ≥2×2 | u3-l1、u3-l2 |
| 10 | **变长批混格式 / 分析类算子输出当有序** | 算子拒批 / 比对随机失败 | 前者查 `uniqueFormat()`（ImageBatch.hpp:L226-L238）；后者是「容量+计数」契约，kernel 用 atomicAdd 竞争槽位，比对前须排序或集合化 | u2-l3、u5-l6 |

备选补充（如果你想凑成 12 条）：多线程不显式传 `stream=`（流栈是进程级单例，u4-l3）；PyNvVideoCodec 解码器复用内部缓冲，零拷贝张量必须先落地（u9-l3）。

### 5.3 参考范本：3 个深读文件（示例）

以下是一个「集成者倾向」的示例答案，**请换成你自己的选择**：

1. **`python/mod_cvcuda/nvcv/Cache.cpp`**——对象缓存是 Python 侧性能与显存行为的最终权柄，但它完全无文档承诺、仅靠 rst 描述。读懂哈希表、`use_count() > 2`、按设备记账三件事，就能源头解释 10 条清单里的第 3、4、5 条。
2. **`src/nvcv/src/include/nvcv/TensorDataAccess.hpp`**——四层之间的「数据交接协议」。所有 priv 实现与 kernel 都经它寻址；按布局标签查维、缺失维度 stride 归零的设计是「双布局默认支持」的落点（u5-l2）。
3. **`src/cvcuda/util/PerStreamCache.hpp`**——workspace 跨流安全复用的核心算法（`cudaEventQuery` 分流 + best-fit），是 u8-l3「ready 事件必须记在最后使用者流上」结论的出处，也是最值得借鉴到自己项目里的并发模式。

### 5.4 验收标准

- 每条错误都有：可复现现象 + 至少一种定位方法 + 证据（脚本/sample/源码行号）。
- 3 个文件的理由里至少出现两个不同的架构层。
- 全部完成后，把这份手册与 4.1 节的知识地图放在一起——它们就是你从本课程带走的全部「资产」。

## 6. 本讲小结

- **一条链 + 一个类型层**：Python 绑定（`python/mod_cvcuda`）→ C API（`src/cvcuda/include/cvcuda/Op*.h`）→ priv 实现（`src/cvcuda/priv/`）→ kernel（`priv/legacy/*.cu` 与 `Op*.cu`），nvcv 类型层（`src/nvcv`）贯穿全程；62 个 C 头 / 61 个绑定 / 57 个 legacy 内核，凭命名规律即可导航。
- **取舍一：C ABI 稳定性**——`extern "C"` + `ProtectCall` 异常翻译 + `CVCUDA_DEFINE_API` 符号版本 + Limitations 契约表，共同保证「实现库升级不破坏老程序、错误信息跨语言不丢」。
- **取舍二：变长批抽象**——`ImageBatchVarShape`/`TensorBatch` 以「导出需流、双面 POD 结构、输出逐图重建」三重代价换取不规则数据的零 padding 流转；规则批从 `_into` 获益小、变长批获益大。
- **取舍三：Python 缓存策略**——仅 Python 层有对象缓存（非包装对象入缓存占配额、`del` 不释放、超配额整体清空、线程局部表全局账）；allocating 每次付缓存查找，`_into` 零交互，选择规则写官方文档 operator_variants.rst。
- **三条读者路线**——使用者精通类型层与使用纪律、集成者加 C ABI 与并发语义、贡献者加工具链与门禁；权威入口都在 `AGENTS.md` 的 Authoritative docs 与 Repository invariants。
- **最后的资产**——一份 10 条易错清单（含定位方法）+ 3 个深读文件 + 一张四层知识地图；验证过的才写「已验证」，没跑过的标「待本地验证」。

## 7. 下一步学习建议

本手册到此完结，但仓库在持续演进。三个方向的建议：

1. **以官方文档为「第二教材」重走一遍**：`docs/sphinx/`（安装、perf_benchmark、advanced 三篇：operator_variants / object_cache / make_operator）与本手册互为对照——手册给你调用链视角，官方文档给你契约原文。特别注意版本号变化引起的差异（当前 HEAD 为 v0.17.0，见 [README.md:L18](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/README.md#L18)）。
2. **动手做一次完整贡献演练**：即使官方暂未开放外部贡献（[README.md:L101-L108](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/README.md#L101-L108)），也建议按 u8-l1 流程为假想算子走完「规格 → `tools/mkop/mkop.sh` 脚手架 → `tools/make_op.py --phase scaffold` → 金标测试 → nvbench 基线」全流程——这是检验你是否真正贯通四层架构的试金石。
3. **持续跟读三个信息源**：GitHub Releases（新算子与 ABI 变化）、NVIDIA 开发者博客（README References 列出的 Bing/腾讯实战案例）、以及 `samples/` 目录的演进（新的互操作示例往往预示 API 走向）。遇到行为不符时，第一反应是查当前 HEAD 的 Limitations 契约表，而不是搜索记忆。
