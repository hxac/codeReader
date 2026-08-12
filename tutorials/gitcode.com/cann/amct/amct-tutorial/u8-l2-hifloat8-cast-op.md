# hifloat8_cast 算子实现剖析

## 1. 本讲目标

学完本讲，读者应能：

- 说出 amct_ops 一个 NPU 算子的「三层目录结构」（`op_kernel` / `op_extension` / `python`）各自承担什么职责。
- 读懂 hifloat8_cast 的 device 端 Ascend C kernel：GM/UB/AIV 内存模型、tiling 分块、LUT 查表 encode/decode、半空间 LUT 优化。
- 理解 host 端 C++ binding 如何用 `TORCH_LIBRARY_FRAGMENT` 注册算子 schema、用 PrivateUse1/Meta 两套 dispatcher 分别挂 NPU 实现与形状推导实现。
- 追踪一次 `encode_to_hifloat8(x)` 从 Python 接口 → C++ binding → Ascend C kernel 的完整调用链。

## 2. 前置知识

承接 u8-l1，本讲假设读者已经知道：

- amct_ops 是 AMCT 独立打包的 NPU 自定义算子层（产含 `.so` 的平台相关 wheel），与做算法编排的 amct_pytorch 职责分离。
- `--soc` 映射到 `NPU_ARCH`（A2/A3 共用 `dav-2201`、A5 用 `dav-3510`），但同一个算子在不同平台编译产物可能相同，UB 大小差异留给运行时处理。
- 算子统一注册到 `amct` 命名空间，提供「模块导入」与 `torch.ops.amct.<op>` 两种等价接口。

本讲新引入、需要先建立的几个昇腾硬件与 PyTorch dispatcher 概念：

| 术语 | 通俗解释 |
|------|---------|
| **GM（Global Memory）** | NPU 片外大显存（HBM），容量大、速度慢，输入/输出张量驻留于此。 |
| **UB（Unified Buffer）** | 每个 AI 核内的片上 SRAM，容量小（A2 标称 256KB、A3 标称 512KB）但极快，kernel 计算时数据须先搬进 UB。 |
| **AIV 核（AI Vector core）** | 昇腾 AI 核中的向量计算单元，一个 block（`blockIdx`）对应一个 AIV 核。 |
| **tiling（分块）** | 把整张张量切成「每核处理多少 + 每次搬多少进 UB」的参数，由 host 端算好传给 device kernel。 |
| **MTE（Memory Transfer Engine）** | 负责搬运 GM↔UB 的硬件单元，与 Compute 流水重叠。 |
| **LUT（Lookup Table）** | 查表。把「逐元素算术转换」预计算成一张表，device 端只做 O(1) 取值。 |
| **dispatcher / backend** | PyTorch 算子分发机制。同一个算子名可挂多套实现，按输入张量的后端（CPU/CUDA/NPU/Meta）选一套执行。 |
| **PrivateUse1** | PyTorch 预留给第三方后端的「第五后端」槽位，torch_npu 占用它来代表 NPU。 |
| **Meta** | 一个特殊后端，只推导输出 shape/dtype、不真正计算，供 `torch.compile` / 图模式做形状推导。 |

HiFloat8 本身的数据格式（Dot 位、锥形精度）已在 u2-l2 讲过，本讲不重复，只在用到时点出「为什么用查表而不是算术」。

## 3. 本讲源码地图

hifloat8_cast 算子目录与文件职责：

| 文件 | 层 | 职责 |
|------|----|------|
| [`op_kernel/hifloat8_cast_tiling.h`](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/hifloat8_cast/op_kernel/hifloat8_cast_tiling.h) | device | TilingData 结构体、castMode 枚举、LUT 尺寸常量 |
| [`op_kernel/hifloat8_cast_kernel.cpp`](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/hifloat8_cast/op_kernel/hifloat8_cast_kernel.cpp) | device | Ascend C kernel（LUT encode/decode、半空间优化），含 device 入口函数 |
| [`op_extension/ops.h`](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/hifloat8_cast/op_extension/ops.h) | host | 声明 `AscendKernel::Hifloat8CastTorch` |
| [`op_extension/hifloat8_cast_torch.cpp`](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/hifloat8_cast/op_extension/hifloat8_cast_torch.cpp) | host | host 实现：校验、tiling 计算、LUT 预计算与缓存、调 kernel stub |
| [`op_extension/register.cpp`](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/hifloat8_cast/op_extension/register.cpp) | host | `TORCH_LIBRARY` 三段注册（schema / PrivateUse1 / Meta） |
| [`python/hifloat8_cast/__init__.py`](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/hifloat8_cast/python/hifloat8_cast/__init__.py) | python | 加载 `.so`、导入两个函数 |
| [`python/hifloat8_cast/ops.py`](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/hifloat8_cast/python/hifloat8_cast/ops.py) | python | `encode_to_hifloat8` / `decode_from_hifloat8` 薄包装 |
| [`CMakeLists.txt`](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/hifloat8_cast/CMakeLists.txt) | 构建 | 把三层源码编进同一个 `libhifloat8_cast_ops.so` |

一句话定位三层：**op_kernel 管「在 NPU 核上怎么算」，op_extension 管「host 侧怎么准备数据并注册给 PyTorch」，python 管「用户怎么调」**。

## 4. 核心概念与源码讲解

### 4.1 op_kernel：device 端 Ascend C kernel 与 tiling

#### 4.1.1 概念说明

device kernel 是真正跑在 NPU AIV 核上的代码，用昇腾的 Ascend C 语言（C++ 的超集，带 `__aicore__` 等扩展属性）编写。它要解决两件事：

1. **数据怎么搬**：输入在 GM（慢）、计算必须在 UB（快），所以要先 GM→UB、算完再 UB→GM。
2. **算什么**：HiFloat8 与 FP16/BF16 之间的逐元素格式转换。

由于 HiFloat8 有 Dot 位（动态重新分配指数/尾数位，见 u2-l2 的「锥形精度」），纯算术转换的位操作分支非常多（可对照 host 侧 `HostFp32MagnitudeToHif8` 的复杂度）。于是本算子改用 **LUT（查表）** 策略：在 host 侧一次性把全部映射预计算成表，device 侧每个元素只做一次查表。这样 device kernel 极简：encode 约 5 条指令、decode 仅 1 条。

四种转换模式由一个枚举区分，四种模式复用同一个 kernel：

[amct_ops/hifloat8_cast/op_kernel/hifloat8_cast_tiling.h:25-33](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/hifloat8_cast/op_kernel/hifloat8_cast_tiling.h#L25-L33)

这里定义了 LUT 尺寸常量（`LUT16_SIZE=32768`、`LUT8_SIZE=256`）与 `HiFloat8CastMode` 枚举（0/1 为 encode、2/3 为 decode）。kernel 多处用 `castMode <= BF16_TO_HIF8` 来判定 `isEncode`。

#### 4.1.2 核心流程

device kernel 采用 Ascend C 标准的「三段流水」模板：

```
每个 AIV 核（blockIdx）分到一段元素：
  for 每个 tile（大小 tileLength，末尾 tile 可能更小）:
      CopyIn(tile)    # MTE: GM → UB（inQueue）
      Compute(tile)   # Compute: 在 UB 上查表 encode/decode（outQueue）
      CopyOut(tile)   # MTE: UB → GM
```

每个核分到的元素数与 tile 大小，都来自 host 算好、经 `HiFloat8CastTilingData` 传进来的 tiling 参数。这个结构体就是 host 与 device 之间的「契约」：

[amct_ops/hifloat8_cast/op_kernel/hifloat8_cast_tiling.h:36-43](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/hifloat8_cast/op_kernel/hifloat8_cast_tiling.h#L36-L43)

字段含义：`blockNum`（用几个核）、`numPerCore` / `tailNumLastCore`（每核元素数，末核可能少一些）、`tileLength`（每 tile 元素数，运行时按平台 UB 算）、`castMode`（四种模式）。

device 入口是一个 `extern "C"` 函数，host 侧通过 ASC 编译器生成的 stub 调用它：

[amct_ops/hifloat8_cast/op_kernel/hifloat8_cast_kernel.cpp:219-225](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/hifloat8_cast/op_kernel/hifloat8_cast_kernel.cpp#L219-L225)

它构造一个 `TPipe`（UB 与队列的管理对象），实例化 `KernelHiFloat8CastLut`，依次调 `Init` 与 `Process`。

#### 4.1.3 源码精读

**Init：建 buffer、算每核 tiling、搬 LUT 进 UB。**

[amct_ops/hifloat8_cast/op_kernel/hifloat8_cast_kernel.cpp:60-98](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/hifloat8_cast/op_kernel/hifloat8_cast_kernel.cpp#L60-L98)

关键点：

- `inputBytes_` / `outputBytes_` 由 `isEncode_` 决定：encode 输入 2B（FP16/BF16）输出 1B（HiF8），decode 反之。
- `total_` 用 `blockIdx < blockNum - 1 ? numPerCore : tailNumLastCore` 取本核元素数（末核走 tail）。
- `tileNum_` / `tailTile_` 是对 `total_` 按 `tileLength_` 向上取整、再算末 tile 大小。
- `inQueue_` / `outQueue_` 用 `InitBuffer(..., 1, ...)` 设成**单缓冲**（depth=1）。注释解释原因：compute 是标量循环，与 MTE 流水重叠收益为 0，于是把队列另一半 UB 让给更大的 tile。
- encode 模式建 32KB 的 `lut16Buf_`，把 GM 里的 LUT 一次性 `DataCopyPad` 进 UB；decode 模式建 512B 的 `lut8Buf_`。

**Process：三段流水的总循环。**

[amct_ops/hifloat8_cast/op_kernel/hifloat8_cast_kernel.cpp:100-107](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/hifloat8_cast/op_kernel/hifloat8_cast_kernel.cpp#L100-L107)

逐 tile 调 CopyIn→Compute→CopyOut，末 tile 用 `tailTile_`。

**ComputeEncode16：encode 的核心，半空间 LUT + 符号分离。**

[amct_ops/hifloat8_cast/op_kernel/hifloat8_cast_kernel.cpp:165-181](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/hifloat8_cast/op_kernel/hifloat8_cast_kernel.cpp#L165-L181)

这里有一个关键优化——**半空间 LUT**。FP16/BF16 的位模式关于符号位对称：

\[
\text{encode}(-x) = \text{encode}(x) \mid 0x80 \quad (\text{当 } \text{encode}(x) \neq 0)
\]

所以 LUT 只存「正半空间」的 32768 条量级值（key = 位模式 `& 0x7FFF`），device 计算时先剥离最高符号位、查表得到量级 `mag`，再按原符号把 `0x80` 或回最高位：

- 读入 16 位值 `v`，`sign = v >> 15`，`mag = lut[v & 0x7FFF]`。
- 若 `mag == 0`（下溢/零），保持 `0x00` 不加符号位（保证 `-0` 也编码为 `0x00`）。
- 否则 `out = mag | (sign << 7)`。

这样 UB 里只需 32KB（而非 64KB 全表），且 LUT 的一次 `DataCopyPad` 从两次合并为一次。注释指出：encode 每元素约 5 条指令（AND + GetValue + SHR + compare + OR）。

**ComputeDecode：decode 的核心，直查无分支。**

[amct_ops/hifloat8_cast/op_kernel/hifloat8_cast_kernel.cpp:184-194](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/hifloat8_cast/op_kernel/hifloat8_cast_kernel.cpp#L184-L194)

decode 更简单：256 个 HiFloat8 字节各对应一个 16 位 FP16/BF16，直接 `lut.GetValue(xLocal.GetValue(i))` 写出，每元素 1 条查表指令、无分支。

**CopyIn 的大块拆分：**

[amct_ops/hifloat8_cast/op_kernel/hifloat8_cast_kernel.cpp:112-139](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/hifloat8_cast/op_kernel/hifloat8_cast_kernel.cpp#L112-L139)

`DataCopyParams.blockLen` 是 `uint16_t`（最大 65535），所以大 tile 要拆。注释列出三档策略（Case A 小块单次、Case B 整 tile 按 32768 拆、Case C encode 尾 tile 按 2 拆），保证 `blockLen` 不溢出。

> A2 平台约束（文件头注释 L45-49 提到）：`Cast<uint32_t,uint16_t>` 与 uint16/uint32 移位指令编译器不支持，故 encode 仍是标量循环——这也是为何用「每步约 5 条查表指令」而非向量化 Gather。

#### 4.1.4 代码实践（源码阅读型）

**实践目标**：搞清 tiling 结构体的每个字段在 device kernel 里被谁消费，验证它是 host→device 的单向数据契约。

**操作步骤**：

1. 打开 `hifloat8_cast_tiling.h`，列出 `HiFloat8CastTilingData` 的 6 个字段。
2. 在 `hifloat8_cast_kernel.cpp` 里搜索每个字段名，记录它在哪一行被读取：
   - `castMode` → `Init` 里判 `isEncode_`（L62、L65）；
   - `blockNum` → `Init` 里判末核（L70）；
   - `numPerCore` / `tailNumLastCore` → 决定 `total_` 与 `offset`（L70、L75）；
   - `tileLength` → 决定 `tileNum_` / `tailTile_` 与 `InitBuffer` 大小（L68、L71-72、L78-79）。

**需要观察的现象**：每个字段都有恰好一处 device 侧消费点，且 device kernel 从不写回这些字段。

**预期结果**：证明 tiling 是「host 算好、device 只读」的单向契约；若某字段在 device 侧无人读取，它就是多余的。

#### 4.1.5 小练习与答案

**练习 1**：为什么 encode 的 LUT 是 32768 项（半空间），而 decode 的 LUT 只有 256 项？

**参考答案**：encode 的输入是 16 位 FP16/BF16，全空间是 65536；利用符号对称性只存正半空间 32768 项、符号位在 device 单独处理，省一半 UB。decode 的输入是 8 位 HiFloat8，全空间就是 256，无需也无法再减半，直接全表查。

**练习 2**：`InitBuffer` 为何对 `inQueue_` / `outQueue_` 都传 `1`（单缓冲）？

**参考答案**：本算子的 compute 是标量查表循环，与 MTE 搬运重叠的收益为 0；与其用双缓冲占住 UB 却换不来流水收益，不如把省下的 UB 用来放大 `tileLength`、减少 tile 数与循环开销。

---

### 4.2 op_extension：host 端 C++ binding 与 TORCH_LIBRARY 注册

#### 4.2.1 概念说明

op_extension 跑在 CPU 上，是 device kernel 与 PyTorch 之间的「中间层」。它做三件事：

1. **注册算子**：用 `TORCH_LIBRARY*` 宏把算子名、schema、各后端实现登记进 PyTorch dispatcher。
2. **准备运行时数据**：算 tiling、预计算 LUT 并搬到 device、缓存复用。
3. **发启 kernel**：拿到 NPU stream，调 ASC 编译器从 `kernel.cpp` 生成的 host stub。

PyTorch dispatcher 的关键：同一个算子名（如 `amct::encode_to_hifloat8`）可以挂多套实现，PyTorch 根据输入张量的**后端**选一套。本算子挂了两套：

| dispatcher key | 触发场景 | 实现 |
|----------------|---------|------|
| `PrivateUse1` | 输入张量在 NPU 上（正常推理） | `EncodeImpl` → 真跑 kernel |
| `Meta` | `torch.compile` / 图模式做形状推导（不真算） | `EncodeMeta` → 只返回同 shape 的空张量 |

> 这正是 u8-l1 提到的「图模式下算子报 no Meta kernel 就要补 Meta 后端做形状推导」的现场：本算子的 `EncodeMeta` / `DecodeMeta` 就是为此而设。

#### 4.2.2 核心流程

host 侧一次 `encode_to_hifloat8(x)` 的内部流程（对应 `Hifloat8CastTorch` 函数）：

```
Hifloat8CastTorch(input, castMode):
  1. ValidateInput        # 校验 dtype / 在 NPU 上
  2. contiguous + numel   # 连续化、取元素总数
  3. AllocateOutput       # 按 castMode 分配输出（uint8）
  4. BuildTilingData      # 查平台 UB+核数 → tileLength/numBlocks
  5. UploadTilingToDevice # tiling 结构体 memcpy 后搬到 device
  6. GetOrBuildLutOnDevice# 首次 CPU 预算 LUT 并搬到 device，后续命中缓存
  7. getCurrentNPUStream  # 取当前流
  8. hifloat8_cast_kernel_lut(...)  # 调 host stub，真正发启 kernel
```

其中 LUT 预计算与 tiling 计算是两个值得单独看的机制。

#### 4.2.3 源码精读

**register.cpp：三段注册。**

第一段——schema 定义，用 `TORCH_LIBRARY_FRAGMENT`：

[amct_ops/hifloat8_cast/op_extension/register.cpp:24-27](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/hifloat8_cast/op_extension/register.cpp#L24-L27)

`m.def` 声明算子签名（用 TorchScript 类型系统）：`encode_to_hifloat8(Tensor) -> Tensor`、`decode_from_hifloat8(Tensor, ScalarType?) -> Tensor`。这是「接口契约」，与具体后端无关。

第二段——NPU 实现，挂 `PrivateUse1` dispatcher：

[amct_ops/hifloat8_cast/op_extension/register.cpp:31-49](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/hifloat8_cast/op_extension/register.cpp#L31-L49)

`EncodeImpl` 校验 dtype（须 half 或 bfloat16）、按 dtype 选 `FP16_TO_HIF8` / `BF16_TO_HIF8`，再调 `Hifloat8CastTorch`；`DecodeImpl` 校验输入须 uint8、输出 dtype 默认 bfloat16，选 `HIF8_TO_FP16` / `HIF8_TO_BF16`。随后 `TORCH_LIBRARY_IMPL(amct, PrivateUse1, m)` 把这两个函数绑到 NPU 后端。

第三段——Meta 实现：

[amct_ops/hifloat8_cast/op_extension/register.cpp:53-64](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/hifloat8_cast/op_extension/register.cpp#L53-L64)

`EncodeMeta` / `DecodeMeta` 只用 `at::empty` 返回同 shape 的目标 dtype 张量，不碰数据——纯形状推导。

**hifloat8_cast_torch.cpp：LUT 预计算与缓存。**

host 侧用一段不短的位运算（`HostFp32MagnitudeToHif8`、`HostHif8ToFpBits`）在 CPU 上把全表算好，然后搬到 device，并按 `(deviceIndex, castMode)` 缓存：

[amct_ops/hifloat8_cast/op_extension/hifloat8_cast_torch.cpp:348-371](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/hifloat8_cast/op_extension/hifloat8_cast_torch.cpp#L348-L371)

要点：用 `static std::mutex` + `static std::map` 做**进程级、按设备×模式**的缓存，首次构建后命中缓存直接返回 device 指针，整个进程内每种 castMode 只算一次。encode 调 `BuildLut16Cpu`（32768 项、半空间、只存量级），decode 调 `BuildLut8Cpu`（256 项 uint16）。

**tiling 计算：查平台、按 UB 推 tileLength。**

[amct_ops/hifloat8_cast/op_extension/hifloat8_cast_torch.cpp:259-292](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/hifloat8_cast/op_extension/hifloat8_cast_torch.cpp#L259-L292)

`GetUbSizeBytes` / `GetAivCoreNum` 通过 `platform_ascendc::PlatformAscendCManager` 查**当前实际平台**的 UB 与 AIV 核数（查不到则回退默认 256KB / 32 核）。`ComputeMaxTileLength` 推最大 tile：

\[
\text{maxTile} = \left\lfloor \frac{\text{UB} - \text{LUT}}{3} \right\rfloor
\]

分母 3 是因为每个元素在 UB 里占「输入 + 输出」字节之和（FP16/BF16 是 2B、HiF8 是 1B，encode/decode 都是 \(2+1=3\) B）。随后对齐：≥32768 时对齐到 32768（让 CopyIn 走大块 Case B），否则对齐到 32（向量指令粒度），上限 65536。这正是 u8-l1 说的「UB 大小差异由运行时平台 API 区分，自动选最优 tileLength」的实现处。

**总入口 Hifloat8CastTorch：**

[amct_ops/hifloat8_cast/op_extension/hifloat8_cast_torch.cpp:375-395](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/hifloat8_cast/op_extension/hifloat8_cast_torch.cpp#L375-L395)

按上面流程图的 8 步执行，最后调 `hifloat8_cast_kernel_lut`——这个 `extern "C"` 函数的声明在文件顶部（[hifloat8_cast_torch.cpp:30-31](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/hifloat8_cast/op_extension/hifloat8_cast_torch.cpp#L30-L31)），它其实是 ASC 编译器从 `kernel.cpp` 自动生成的 host stub，负责把参数排好、发启 device kernel。

#### 4.2.4 代码实践

**实践目标**：解释 `register.cpp` 为什么用 `TORCH_LIBRARY_FRAGMENT(amct, ...)` 而不是 `TORCH_LIBRARY(amct, ...)`。

**背景知识**：

- `TORCH_LIBRARY(ns, m)` 会**创建并拥有**命名空间 `ns`；它要求该命名空间此前未被创建，且整个进程里应只有一个「拥有者」。
- `TORCH_LIBRARY_FRAGMENT(ns, m)` 只是**往已存在（或尚不存在）的命名空间追加**一片定义，不要求所有权，多个编译单元可各贡献一个 fragment。

**操作步骤**：

1. 阅读 amct_ops 根 `README.md` 的「命名空间约束」一节（[README.md:145-151](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/README.md#L145-L151)），确认官方要求「所有算子必须注册到 `amct` 命名空间」。
2. 设想：hifloat8_cast 与 svd_quant 是两个算子，各自有独立的 `register.cpp`、被编进（或被加载进）同一进程。若每个都用 `TORCH_LIBRARY(amct, m)`，第二个会因「命名空间 amct 已被创建/拥有」而冲突。
3. 用 `TORCH_LIBRARY_FRAGMENT` 后，每个算子只往公共的 `amct` 追加自己的 schema / impl，互不抢所有权——这正是多算子共存所需的。

**需要观察的现象与预期结果**：`FRAGMENT` 是为「多个算子共享同一命名空间、各自独立注册」而设计的；amct_ops 选它，是为了让 hifloat8_cast、svd_quant 及未来新算子都能无冲突地挂到统一的 `amct` 名下。

> 待本地验证：若有 NPU 环境，可在 Python 里 `print(torch.ops.amct)` 看到 `amct` 下并列出现 `encode_to_hifloat8`、`decode_from_hifloat8`（及 svd_quant 的算子），证明它们同属一个命名空间。

#### 4.2.5 小练习与答案

**练习 1**：`EncodeImpl` 里为什么不接受 `castMode` 参数，而是根据 `input.dtype()` 自己选？

**参考答案**：用户侧的 Python 接口只暴露「输入张量」，castMode 是内部实现细节。`EncodeImpl` 按输入是 half 还是 bfloat16 自动映射到 `FP16_TO_HIF8` / `BF16_TO_HIF8`，让接口保持简洁，与 README「Python 接口根据输入 dtype 自动选择 castMode，无需手动指定」一致。

**练习 2**：`GetOrBuildLutOnDevice` 为什么按 `(deviceIndex, castMode)` 作缓存键，而不是只按 castMode？

**参考答案**：多卡场景下每张 NPU 卡是独立设备，LUT 必须搬到各卡自己的 GM 才能用；若只按 castMode 缓存，会把卡 0 的 device 指针误用到卡 1。加入 deviceIndex 保证「每卡每模式」各一份。

---

### 4.3 python：接口包装与 .so 加载

#### 4.3.1 概念说明

python 层是最薄的一层，只做两件事：

1. **加载编译产物 `.so`**，让里面注册的 `amct::*` schema 真正进入 PyTorch dispatcher。
2. **提供有文档字符串、可 IDE 补全的函数名**，内部转调 `torch.ops.amct.<op>`。

`torch.ops.load_library(path)` 是 PyTorch 的标准机制：对一个 `.so` 调用它，会触发 `.so` 里所有 `TORCH_LIBRARY*` 注册代码执行，从而把算子登记进 `torch.ops.amct`。这步是「惰性」的——只有 import 子包时才加载。

#### 4.3.2 核心流程

```
import amct_ops.hifloat8_cast
  → __init__.py 执行：
     import torch_npu            # 副作用：注册 PrivateUse1 后端为 NPU
     torch.ops.load_library(.so) # 触发 .so 里的 TORCH_LIBRARY_FRAGMENT，登记 schema
     from .ops import encode_to_hifloat8, decode_from_hifloat8
  → 此后 torch.ops.amct.encode_to_hifloat8 可用
```

之后两种调用等价：

- `encode_to_hifloat8(x)`（模块函数，带 docstring）
- `torch.ops.amct.encode_to_hifloat8(x)`（原生 ops 风格）

因为前者函数体就是一行 `return torch.ops.amct.encode_to_hifloat8(x)`。

#### 4.3.3 源码精读

**__init__.py：加载 .so。**

[amct_ops/hifloat8_cast/python/hifloat8_cast/__init__.py:40-49](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/hifloat8_cast/python/hifloat8_cast/__init__.py#L40-L49)

要点：

- `import torch_npu  # noqa: F401` 不是为了用它的符号，而是为了它的**副作用**——torch_npu 导入时会把自己注册成 PrivateUse1 后端；没有这一步，dispatcher 的 `PrivateUse1` key 不会被认作 NPU。
- `_lib_path` 指向同目录下的 `libhifloat8_cast_ops.so`（由 CMakeLists 编译产出，打包进 wheel）。
- `torch.ops.load_library(_lib_path)` 执行 `.so`，触发 `TORCH_LIBRARY_FRAGMENT(amct, ...)` 登记 schema 与 impl。

**ops.py：薄包装。**

[amct_ops/hifloat8_cast/python/hifloat8_cast/ops.py:20-37](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/hifloat8_cast/python/hifloat8_cast/ops.py#L20-L37)

`encode_to_hifloat8` 的实现就一行 `return torch.ops.amct.encode_to_hifloat8(x)`，函数体本身不做任何校验——校验全在 C++ 的 `EncodeImpl` 里（dtype 不对会抛 `RuntimeError`）。decode 同理，多接一个 `dtype` 参数透传。

> 这层包装的价值不在逻辑、在「人因工程」：提供 docstring、类型注解、IDE 补全，并让 `from amct_ops.hifloat8_cast import encode_to_hifloat8` 这种自然写法可用。

#### 4.3.4 代码实践

**实践目标**：验证两种调用方式确实指向同一个被 dispatcher 路由的算子，并确认「注册发生在 load_library 那一刻」。

**操作步骤**（需装好 amct_ops wheel 与 torch_npu；NPU 可选——Meta 后端可在 CPU 上验证 schema 存在）：

1. `import amct_ops.hifloat8_cast` 触发 `.so` 加载。
2. 内省命名空间：`print(dir(torch.ops.amct))`，应看到 `encode_to_hifloat8`、`decode_from_hifloat8`。
3. 读 docstring：`help(encode_to_hifloat8)`，应看到 `ops.py` 里写的说明。
4. 对比：在 `import amct_ops.hifloat8_cast` **之前**访问 `torch.ops.amct`，这两个名字不应存在；import 之后才出现。

**需要观察的现象**：模块函数与 `torch.ops.amct.<op>` 行为一致；注册时机与 `.so` 加载绑定。

**预期结果**：证明了「`.so` 的 TORCH_LIBRARY_FRAGMENT 在 `load_library` 时执行，才把算子登记进 dispatcher」。

> 待本地验证：第 2-4 步依赖实际安装的 wheel；无 NPU 时第 2、4 步仍可在 CPU 上验证 schema 是否登记（访问名字是否存在），但真正跑 kernel 需要 NPU。

#### 4.3.5 小练习与答案

**练习**：如果删掉 `__init__.py` 里的 `import torch_npu`，直接 `torch.ops.amct.encode_to_hifloat8(x_npu)` 会发生什么？

**参考答案**：`.so` 仍会被 `load_library` 加载、schema 仍会登记，但 PrivateUse1 后端没有被 torch_npu 注册成 NPU。于是当输入是 NPU 张量时，dispatcher 找不到对应的 NPU 后端实现（PrivateUse1 未与 NPU 关联），会报「找不到实现 / 后端未注册」一类的错误。所以 `import torch_npu` 是为它的副作用，不是为直接使用其符号。

---

## 5. 综合实践

把三层串起来：完整跟踪一次 `encode_to_hifloat8(x)`（x 为 NPU 上的 bfloat16 张量）的调用链。请在源码里逐层标注，画出下面这条链上每一步发生在哪个文件、哪一行。

```
[python]  encode_to_hifloat8(x)                         # ops.py:37
   │        return torch.ops.amct.encode_to_hifloat8(x)
   ▼
[dispatcher] 按 x 的后端 PrivateUse1(NPU) 选实现
   ▼
[register.cpp] EncodeImpl(x)                            # :31-36
   │   校验 dtype∈{half,bf16}；dtype==bf16 → castMode=BF16_TO_HIF8
   ▼
[hifloat8_cast_torch.cpp] Hifloat8CastTorch(x, 1)       # :375-395
   │   ValidateInput → contiguous → AllocateOutput(uint8)
   │   BuildTilingData(查 UB/核数 → tileLength)          # :321-338
   │   UploadTilingToDevice                              # :340-345
   │   GetOrBuildLutOnDevice(首次 BuildLut16Cpu→device)  # :348-371
   │   getCurrentNPUStream
   ▼
[host stub] hifloat8_cast_kernel_lut(blockNum,...)      # extern "C", :30-31 声明
   ▼
[kernel.cpp] hifloat8_cast_kernel_lut 入口              # :219-225
   │   new TPipe; KernelHiFloat8CastLut.Init(...); .Process()
   ▼
[每个 AIV 核] Process → 逐 tile CopyIn→ComputeEncode16→CopyOut  # :100-107, :165-181
   │   读 16 位 v = xU16[i]; sign=v>>15; mag=lut[v&0x7FFF]; out=mag|(sign<<7)
   ▼
[output] uint8 HiFloat8 张量（同 shape）
```

**任务**：

1. 在本地仓库打开这五个文件，沿上面的链把每一步的行号核对一遍，标出「数据从哪一层交到下一层」。
2. 回答两个问题：
   - 数据张量 `x` 在哪一层从 GM 进入 UB？（提示：device kernel 的 `CopyIn`，kernel.cpp:112-139）
   - `castMode=1(BF16_TO_HIF8)` 这个内部值在 Python 接口层完全不可见——它是在哪一层、依据什么被决定出来的？（提示：`EncodeImpl` 依据 `input.dtype()`，register.cpp:31-36）
3. （选做，需 NPU）对照 README「使用示例」跑一次 encode→decode roundtrip，把输出 dtype 与 shape 与你从源码推出的预期对照。

> 无 NPU 环境时，第 1、2 步是纯源码阅读，可独立完成；第 3 步标注「待本地验证」。

## 6. 本讲小结

- hifloat8_cast 体现了 amct_ops 算子的标准三层结构：**op_kernel**（device Ascend C kernel）、**op_extension**（host C++ binding + 注册）、**python**（薄包装 + 加载 `.so`），三层由 `CMakeLists.txt` 编进同一个 `libhifloat8_cast_ops.so`。
- device kernel 用 **LUT 查表**实现 HiFloat8↔FP16/BF16 转换，避开 HiFloat8 Dot 位带来的复杂位运算；encode 用「半空间 LUT + 符号分离」把 32KB 表压成一半，decode 用 256 项全表直查。
- **tiling 是 host→device 的单向数据契约**：host 查平台实际 UB 与核数算出 `tileLength` / `numBlocks`，device 据此做「每核分段 + 逐 tile CopyIn/Compute/CopyOut」三段流水。
- op_extension 用 `TORCH_LIBRARY_FRAGMENT(amct, m)` 登记到统一的 `amct` 命名空间（多算子各贡献 fragment、不抢所有权），并挂 **PrivateUse1**（NPU 真跑）与 **Meta**（形状推导）两套 dispatcher 实现。
- host 侧把昂贵的 LUT 预计算放 CPU 一次性完成，按 `(deviceIndex, castMode)` 进程级缓存搬到 device，整个进程每种模式只算一次。
- python 层 `torch.ops.load_library` 触发 `.so` 注册，`import torch_npu` 提供注册 PrivateUse1 后端的副作用；模块函数只是 `torch.ops.amct.<op>` 的带 docstring 薄包装。

## 7. 下一步学习建议

- **下一讲 u8-l3「新增 NPU 算子的开发流程」**：将以本讲的三层结构为模板，讲清新增算子的目录规范、命名空间约束与 svd_quant 这类带 `op_host`（独立 tiling）算子的组织方式，建议先把本讲的三层职责与注册套路吃透再看。
- **横向对照 svd_quant**：直接读 `amct_ops/svd_quant/`，看它如何复用「op_kernel / op_extension / python + CMakeLists」骨架、又如何因算子复杂度引入 `op_host` 独立 tiling 目录。
- **回看 u2-l2**：本讲的 LUT 策略之所以必要，根源是 HiFloat8 的 Dot 位与锥形精度；结合 u2-l2 的数据类型讲义，能更清楚「为什么这个算子值得专门写一个 NPU kernel」。
- **CANN 官方指南**：README 末尾给出的 [torch_extension_develop_guide](https://gitcode.com/cann/ops-nn/blob/master/docs/zh/develop/torch_extension_develop_guide.md) 是 Ascend C + torch extension 的权威开发指南，适合在动手新增算子前通读。
